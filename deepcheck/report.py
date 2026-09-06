"""분석 결과 조립과 출력(text/JSON/HTML).

결과는 두 축으로 분리한다. 실제 인물이 나온 영상에도 허위 주장이 있을 수 있고,
AI로 만든 영상의 발언이 사실일 수도 있으므로 두 결과를 하나의 진위 점수로 합치지
않는다(docs의 `project/prd.md` 핵심 설계 경계).

- media_manipulation: 영상·음성이 합성되었을 가능성
- claim_verification: 영상 속 주장의 사실성 (미구현, 팀 결정 대기)

또 하나의 원칙은 "분석하지 못한 것"과 "분석했더니 정상"을 구분하는 것이다.
프레임을 한 장도 못 뽑았을 때 위험도 0점("진짜일 가능성 높음")을 반환하면,
실패를 무죄 판정으로 바꿔 사용자를 오도하게 된다. 그런 경우 risk는 None,
status는 unavailable이다.
"""

from __future__ import annotations

import html
from dataclasses import asdict, dataclass, field
from enum import Enum

from .config import config


class StageState(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_IMPLEMENTED = "not_implemented"


class AxisStatus(str, Enum):
    ANALYZED = "analyzed"
    UNAVAILABLE = "unavailable"
    NOT_IMPLEMENTED = "not_implemented"


class AnalysisState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


LEVEL_HIGH = "매우 높음"
LEVEL_MODERATE = "상당함"
LEVEL_CAUTION = "주의 필요"
LEVEL_LOW = "낮음"
LEVEL_UNKNOWN = "판단 불가"


@dataclass
class StageStatus:
    status: str
    detail: str | None = None
    elapsed_sec: float | None = None


@dataclass
class MediaManipulation:
    status: str
    risk: float | None
    level: str
    frames_analyzed: int = 0
    frames_fake: int = 0
    method: str = ""
    detail: str | None = None
    signals: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


@dataclass
class ClaimVerification:
    status: str = AxisStatus.NOT_IMPLEMENTED.value
    claims: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass
class TranscriptInfo:
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    tone: str = ""
    language: str | None = None
    word_count: int | None = None
    coverage_pct: float | None = None
    # 클릭베이트·주장 강도는 미디어 조작의 근거가 아니므로 점수에 반영하지 않고
    # 참고 정보로만 노출한다. 자극적인 제목의 진짜 영상이 AI 가짜 쪽으로 밀리던
    # 개편 전 동작을 막기 위한 구분이다.
    signals: dict = field(default_factory=dict)


@dataclass
class AnalysisReport:
    url: str
    media: dict
    analysis_status: str
    stages: dict
    media_manipulation: MediaManipulation
    claim_verification: ClaimVerification
    transcript: TranscriptInfo

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AnalysisReport":
        """dict → 객체 복원. CLI가 출력 포매터에 넘길 때 쓴다.

        모르는 키는 무시한다. 개편 전에는 `RiskReport(**payload)`로 복원해서
        스키마에 필드를 하나만 추가해도 CLI가 TypeError로 죽었다.
        """
        return cls(
            url=payload.get("url", ""),
            media=payload.get("media", {}),
            analysis_status=payload.get("analysis_status", AnalysisState.PARTIAL.value),
            stages=payload.get("stages", {}),
            media_manipulation=MediaManipulation(**payload.get("media_manipulation", {
                "status": AxisStatus.UNAVAILABLE.value, "risk": None, "level": LEVEL_UNKNOWN,
            })),
            claim_verification=ClaimVerification(**payload.get("claim_verification", {})),
            transcript=TranscriptInfo(**payload.get("transcript", {})),
        )


def level_for(risk: float | None) -> str:
    if risk is None:
        return LEVEL_UNKNOWN
    if risk >= config.level_high:
        return LEVEL_HIGH
    if risk >= config.level_moderate:
        return LEVEL_MODERATE
    if risk >= config.level_caution:
        return LEVEL_CAUTION
    return LEVEL_LOW


def build_media_manipulation(deepfake: dict, text: dict) -> MediaManipulation:
    """영상 신호 + 자가표기·합성음성 신호를 미디어 조작 축 하나로 모은다."""
    frames_analyzed = int(deepfake.get("frames_analyzed", 0))
    visual_risk = float(deepfake.get("avg_fake_score", 0.0)) if frames_analyzed > 0 else None

    self_disclosure_risk = float(text.get("self_disclosure_risk", 0))
    tts_risk = float(text.get("tts_risk", 0))
    disclosed = self_disclosure_risk >= config.self_disclosure_gate

    signals = {
        "visual": {
            "available": visual_risk is not None,
            "risk": visual_risk,
            "frames_analyzed": frames_analyzed,
            "frames_fake": int(deepfake.get("frames_fake", 0)),
            "classifier_used": "ViT-classifier" in str(deepfake.get("method", "")),
        },
        "self_disclosure": {
            "risk": self_disclosure_risk,
            "evidence": list(text.get("self_disclosure_evidence", [])),
        },
        "tts": {"risk": tts_risk},
        "vlm": {"summary": deepfake.get("vlm_summary")},
    }

    # 영상 분석도 못 했고 자가표기도 없으면 판단할 근거가 없다. 합성 음성 의심만으로는
    # 판정하지 않는다(신호가 약해 단독 근거로 쓰기 어렵다).
    if visual_risk is None and not disclosed:
        return MediaManipulation(
            status=AxisStatus.UNAVAILABLE.value,
            risk=None,
            level=LEVEL_UNKNOWN,
            frames_analyzed=frames_analyzed,
            frames_fake=int(deepfake.get("frames_fake", 0)),
            method=str(deepfake.get("method", "")),
            detail="분석 가능한 영상 프레임을 확보하지 못했고 자가표기도 없어 판단을 유보한다.",
            signals=signals,
            evidence=list(deepfake.get("evidence", [])),
        )

    risk = visual_risk if visual_risk is not None else 0.0
    risk = max(risk, tts_risk * config.tts_signal_weight)
    # 자가표기는 우리 분류기의 픽셀 추론보다 신뢰도가 높으므로 하한선으로 작동시킨다.
    if disclosed:
        risk = max(risk, self_disclosure_risk * config.self_disclosure_floor_ratio)
    risk = min(round(risk, 1), 100.0)

    detail = None
    if visual_risk is None:
        detail = "영상 프레임 분석은 하지 못했고, 제목·설명의 자가표기만으로 판단한 점수다."
    elif not signals["visual"]["classifier_used"]:
        detail = "딥페이크 분류기를 사용하지 못해 휴리스틱만으로 영상을 판단했다."

    evidence = list(deepfake.get("evidence", [])) + list(text.get("self_disclosure_evidence", []))

    return MediaManipulation(
        status=AxisStatus.ANALYZED.value,
        risk=risk,
        level=level_for(risk),
        frames_analyzed=frames_analyzed,
        frames_fake=int(deepfake.get("frames_fake", 0)),
        method=str(deepfake.get("method", "")),
        detail=detail,
        signals=signals,
        evidence=evidence,
    )


def _is_degraded(stage_payload: dict, media_manipulation: MediaManipulation) -> bool:
    """이번 분석이 계획대로 다 돌았는지 판정한다.

    주장 사실성 검증은 MVP 범위 밖이라 기본적으로 꺼져 있다. 이걸 degraded로 세면
    모든 분석이 항상 partial이 되어 플래그가 무의미해지므로 제외한다.
    """
    if media_manipulation.status == AxisStatus.UNAVAILABLE.value:
        return True
    for name, stage in stage_payload.items():
        status = stage.get("status") if isinstance(stage, dict) else None
        if status == StageState.FAILED.value:
            return True
        if status == StageState.SKIPPED.value and name != "claim_verification":
            return True
    return False


def build(meta: dict, deepfake: dict, text: dict, stages: dict,
          claim_verification: ClaimVerification | None = None) -> AnalysisReport:
    media_manipulation = build_media_manipulation(deepfake, text)
    claims = claim_verification or ClaimVerification(
        status=AxisStatus.NOT_IMPLEMENTED.value,
        detail="주장 사실성 검증 파이프라인은 아직 구현되지 않았다(팀 결정 대기).",
    )

    stage_payload = {
        name: (asdict(s) if isinstance(s, StageStatus) else s) for name, s in stages.items()
    }
    degraded = _is_degraded(stage_payload, media_manipulation)

    return AnalysisReport(
        url=meta.get("url", ""),
        media={
            "title": meta.get("title"),
            "uploader": meta.get("uploader"),
            "duration": meta.get("duration"),
            "video_id": meta.get("video_id"),
        },
        analysis_status=(AnalysisState.PARTIAL if degraded else AnalysisState.COMPLETE).value,
        stages=stage_payload,
        media_manipulation=media_manipulation,
        claim_verification=claims,
        transcript=TranscriptInfo(
            summary=text.get("summary", ""),
            keywords=list(text.get("keywords", [])),
            tone=text.get("tone", ""),
            language=meta.get("language"),
            word_count=text.get("word_count"),
            coverage_pct=meta.get("stt_coverage_pct"),
            signals={
                "clickbait": text.get("clickbait_risk", 0),
                "claim_strength": text.get("claim_risk", 0),
            },
        ),
    )


def _bar(value: float | None, width: int = 10) -> str:
    if value is None:
        return "?" * width
    filled = round(min(max(value, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def format_text(r: AnalysisReport) -> str:
    mm = r.media_manipulation
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append("DeepCheck — 영상 분석 결과")
    lines.append("=" * 62)
    lines.append(f"URL      : {r.url}")
    if r.media.get("title"):
        lines.append(f"제목     : {r.media['title']}")
    if r.media.get("uploader"):
        lines.append(f"채널     : {r.media['uploader']}")
    if r.media.get("duration"):
        m, s = divmod(int(r.media["duration"]), 60)
        lines.append(f"길이     : {m:02d}:{s:02d}")
    if r.transcript.language:
        lines.append(f"언어     : {r.transcript.language}")
    lines.append(f"분석 상태: {r.analysis_status}")
    for name, stage in r.stages.items():
        if stage.get("status") != StageState.OK.value:
            lines.append(f"    · {name}: {stage.get('status')} — {stage.get('detail') or ''}")
    lines.append("")

    lines.append("[1] 미디어 조작 (딥페이크/AI 생성)")
    if mm.status == AxisStatus.UNAVAILABLE.value:
        lines.append(f"    판정   : {LEVEL_UNKNOWN}")
        lines.append(f"    사유   : {mm.detail or '분석 근거 부족'}")
    else:
        lines.append(f"    위험도 : {_bar(mm.risk)}  {mm.risk:.0f}/100  →  {mm.level}")
        if mm.detail:
            lines.append(f"    주의   : {mm.detail}")
    lines.append(f"    프레임 : {mm.frames_fake} / {mm.frames_analyzed} 의심")
    lines.append(f"    방법   : {mm.method}")
    for e in mm.evidence:
        lines.append(f"    · {e}")
    if mm.signals.get("vlm", {}).get("summary"):
        lines.append(f"    VLM    : {mm.signals['vlm']['summary']}")
    lines.append("")

    lines.append("[2] 주장 사실성 검증")
    lines.append(f"    상태   : {r.claim_verification.status}")
    if r.claim_verification.detail:
        lines.append(f"    사유   : {r.claim_verification.detail}")
    lines.append("")

    lines.append("[참고] 음성 텍스트")
    lines.append(f"    요약   : {r.transcript.summary}")
    if r.transcript.keywords:
        lines.append(f"    키워드 : {', '.join(r.transcript.keywords)}")
    if r.transcript.coverage_pct is not None:
        lines.append(f"    STT 커버리지: {r.transcript.coverage_pct:.1f}%")
    sig = r.transcript.signals
    lines.append(
        f"    (점수 미반영) 클릭베이트 {sig.get('clickbait', 0)}/100 · "
        f"주장 강도 {sig.get('claim_strength', 0)}/100"
    )
    lines.append("=" * 62)
    return "\n".join(lines)


def format_json(r: AnalysisReport) -> str:
    import json
    return json.dumps(r.to_dict(), ensure_ascii=False, indent=2)


def format_html(r: AnalysisReport) -> str:
    # 제목·설명·근거는 외부(영상 업로더)에서 온 값이므로 반드시 이스케이프한다.
    def esc(value: object) -> str:
        return html.escape(str(value if value is not None else ""))

    mm = r.media_manipulation
    risk = mm.risk
    color = "#8a8f98"
    if risk is not None:
        color = ("#e94d4d" if risk >= config.level_moderate
                 else "#e6a23c" if risk >= config.level_caution else "#3fb27f")
    badge = f"{risk:.0f}/100" if risk is not None else LEVEL_UNKNOWN
    bar_w = risk if risk is not None else 0
    ev = "".join(f"<li>{esc(e)}</li>" for e in mm.evidence)
    kw = esc(", ".join(r.transcript.keywords))
    detail_html = f"<p><b>주의:</b> {esc(mm.detail)}</p>" if mm.detail else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>DeepCheck</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}}
.card{{border:1px solid #ddd;border-radius:14px;padding:24px;margin-top:20px}}
.badge{{font-size:2rem;font-weight:800;color:{color}}}
.meter{{height:14px;background:#eee;border-radius:8px;overflow:hidden}}
.fill{{height:100%;width:{bar_w}%;background:{color}}}
li{{margin:6px 0;font-size:.95rem}}</style></head><body>
<h1>DeepCheck — 영상 분석 결과</h1>
<p><a href="{esc(r.url)}">{esc(r.media.get('title') or r.url)}</a></p>
<div class="card"><h3>1. 미디어 조작 (딥페이크/AI 생성)</h3>
<span class="badge">{esc(badge)}</span>
<div class="meter"><div class="fill"></div></div>
<p>{esc(mm.level)}</p>{detail_html}
<p><b>프레임:</b> {mm.frames_fake}/{mm.frames_analyzed} 의심 · <b>방법:</b> {esc(mm.method)}</p>
<ul>{ev}</ul></div>
<div class="card"><h3>2. 주장 사실성 검증</h3>
<p>상태: {esc(r.claim_verification.status)}</p>
<p>{esc(r.claim_verification.detail)}</p></div>
<div class="card"><h3>참고: 음성 텍스트</h3><p>{esc(r.transcript.summary)}</p>
<p><b>키워드:</b> {kw}</p>
<p><b>(점수 미반영)</b> 클릭베이트 {esc(r.transcript.signals.get('clickbait', 0))}/100 ·
주장 강도 {esc(r.transcript.signals.get('claim_strength', 0))}/100</p></div>
</body></html>"""
