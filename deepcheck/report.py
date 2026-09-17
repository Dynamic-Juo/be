"""분석 결과 조립과 출력(text/JSON/HTML).

결과는 세 축으로 분리한다. 실제 인물이 나온 영상에도 허위 주장이 있을 수 있고,
AI로 만든 영상의 발언이 사실일 수도 있으므로 결과를 하나의 진위 점수로 합치지
않는다(docs의 `project/prd.md` 핵심 설계 경계, `project/mvp-review.md` U-04).

- face_manipulation: 얼굴을 합성·변형했을 가능성 (ViT 분류기 + 자가표기)
- whole_video_generation: 영상 전체를 AI가 만들었을 가능성 (전용 모델 미선정, 자가표기만)
- claim_verification: 영상 속 주장의 사실성

미디어 조작 두 축은 숫자 점수를 사용자에게 노출하지 않는다. `조작 의심`/
`뚜렷한 조작 징후 없음`/`판단 보류`/`분석 불가` 네 범주로만 응답한다.

또 하나의 원칙은 "분석하지 못한 것"과 "분석했더니 정상"을 구분하는 것이다.
프레임을 한 장도 못 뽑았는데 "뚜렷한 조작 징후 없음"으로 응답하면, 실패를 무죄
판정으로 바꿔 사용자를 오도하게 된다. 그런 경우 status는 unavailable이다.
"""

from __future__ import annotations

import html
from dataclasses import asdict, dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from .config import config


class StageState(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class AxisStatus(str, Enum):
    ANALYZED = "analyzed"
    UNAVAILABLE = "unavailable"
    # 분석은 정상이었지만 검증할 주장이 하나도 없었다. U-03이 이 경우를 개별 주장
    # 판정이 아니라 화면 상태로 구분하라고 정했다 — 카드가 0개인 것과 분석을 못 한
    # 것은 사용자에게 완전히 다른 이야기다.
    NO_CLAIMS = "no_claims"


class AnalysisState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ManipulationState(str, Enum):
    """미디어 조작 축(얼굴 합성·변형 / 영상 전체 AI 생성)의 4단계 판정.

    docs의 결과 UI 설계(U-04)를 그대로 따른다. 숫자 위험도는 사용자에게 노출하지
    않는다 — "조작되지 않음"처럼 조작이 없다고 단정하는 표현도 쓰지 않는다.
    """

    SUSPECTED = "suspected"            # 조작 의심
    NO_CLEAR_SIGNS = "no_clear_signs"  # 뚜렷한 조작 징후 없음
    INCONCLUSIVE = "inconclusive"      # 판단 보류
    UNAVAILABLE = "unavailable"        # 분석 불가


MANIPULATION_LABELS = {
    ManipulationState.SUSPECTED.value: "조작 의심",
    ManipulationState.NO_CLEAR_SIGNS.value: "뚜렷한 조작 징후 없음",
    ManipulationState.INCONCLUSIVE.value: "판단 보류",
    ManipulationState.UNAVAILABLE.value: "분석 불가",
}

# 하위 호환용. 이전 5단계 텍스트 등급(연속 점수 표시)은 더 이상 사용자에게
# 노출하지 않지만, 내부 디버그 신호(signals)에는 여전히 참고용으로 남긴다.
LEVEL_HIGH = "매우 높음"
LEVEL_MODERATE = "상당함"
LEVEL_CAUTION = "주의 필요"
LEVEL_LOW = "낮음"
LEVEL_UNKNOWN = "판단 불가"

# 판정 이름은 claims.py가 유일한 정의처다. 여기서 복제하면 예전처럼 두 곳이
# 조용히 어긋난다(등급 경계가 텍스트 출력과 HTML에서 달랐던 사고와 같은 종류).
from .claims import INSUFFICIENT_LABELS, VERDICT_LABELS  # noqa: E402


@dataclass
class StageStatus:
    status: str
    detail: str | None = None
    elapsed_sec: float | None = None
    # 실패한 단계는 errors.py의 구조화된 에러(code/message/retryable)를 함께 남긴다.
    error: dict | None = None


@dataclass
class ManipulationAxis:
    """미디어 조작 판정 하나(얼굴 합성·변형, 또는 영상 전체 AI 생성)의 결과.

    두 축을 완전히 분리한다 — 하나가 조작 의심이라고 다른 하나까지 그렇게 보이면
    안 된다. status는 4단계 범주 하나만 쓰고, 연속 점수는 signals 안에만 남긴다
    (디버깅용이지 사용자에게 보여줄 값이 아니다).
    """

    status: str
    status_label: str
    detail: str | None = None
    evidence: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)


@dataclass
class ClaimVerification:
    status: str = AxisStatus.UNAVAILABLE.value
    claims: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    detail: str | None = None


@dataclass
class TranscriptInfo:
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    tone: str = ""
    language: str | None = None
    word_count: int | None = None
    coverage_pct: float | None = None
    coverage_basis: str | None = None
    segment_coverage_pct: float | None = None
    coverage_detail: str | None = None
    # 이 텍스트를 어디서 얻었는지(stt | caption). 발언 위치의 정확도가 달라서
    # 화면에서 "음성 인식" / "자막"으로 함께 보여준다.
    source: str | None = None
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
    face_manipulation: ManipulationAxis
    whole_video_generation: ManipulationAxis
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
        unavailable_axis = {
            "status": ManipulationState.UNAVAILABLE.value,
            "status_label": MANIPULATION_LABELS[ManipulationState.UNAVAILABLE.value],
        }
        return cls(
            url=payload.get("url", ""),
            media=payload.get("media", {}),
            analysis_status=payload.get("analysis_status", AnalysisState.PARTIAL.value),
            stages=payload.get("stages", {}),
            face_manipulation=ManipulationAxis(
                **payload.get("face_manipulation", unavailable_axis)
            ),
            whole_video_generation=ManipulationAxis(
                **payload.get("whole_video_generation", unavailable_axis)
            ),
            claim_verification=ClaimVerification(**payload.get("claim_verification", {})),
            transcript=TranscriptInfo(**payload.get("transcript", {})),
        )


def categorize(risk: float | None) -> str:
    """연속 위험도 점수를 4단계 범주로 접는다.

    사용자에게는 이 범주만 보이고 원래 점수(risk)는 signals 안에만 남는다.
    등급 경계는 기존 config.level_high/level_moderate를 그대로 재사용한다.
    """
    if risk is None:
        return ManipulationState.UNAVAILABLE.value
    if risk >= config.level_high:
        return ManipulationState.SUSPECTED.value
    if risk >= config.level_moderate:
        return ManipulationState.INCONCLUSIVE.value
    return ManipulationState.NO_CLEAR_SIGNS.value


def build_face_manipulation(deepfake: dict, text: dict) -> ManipulationAxis:
    """얼굴 합성·변형 축. 얼굴 crop + ViT 분류기 + 자가표기 신호를 쓴다.

    합성 음성(TTS) 의심은 여기 넣지 않는다 — 음성 합성 탐지는 MVP에서 제외하기로
    했다(PRD M-03). 이전 개편에서는 tts_risk를 이 점수에 섞고 있었다.
    """
    frames_analyzed = int(deepfake.get("frames_analyzed", 0))
    frames_with_face = int(deepfake.get("frames_with_face", 0))
    classifier_used = "ViT-classifier" in str(deepfake.get("method", ""))

    visual_risk = float(deepfake.get("avg_fake_score", 0.0)) if frames_analyzed > 0 else None
    visual_unavailable_reason = None
    if frames_analyzed == 0:
        visual_unavailable_reason = "분석할 영상 프레임을 확보하지 못했다."
    elif frames_with_face == 0:
        # 이 분류기는 얼굴 crop 이미지로 학습된 모델이다. 얼굴을 한 명도 못 찾았다면
        # 전체 프레임에 대한 점수는 근거로 쓸 수 없다. 설계 문서의 원칙대로
        # "얼굴을 못 찾음"을 정상 판정으로 바꾸지 않고 판단을 유보한다.
        visual_risk = None
        visual_unavailable_reason = (
            "영상에서 얼굴을 찾지 못해 얼굴 기반 분류 결과를 신뢰할 수 없다."
            if deepfake.get("face_model_available")
            else "얼굴 검출 모델이 없어 얼굴 기반 분석을 수행하지 못했다."
        )
    elif not classifier_used:
        visual_risk = None
        visual_unavailable_reason = "얼굴 분류기의 유효한 점수를 확보하지 못했다. 휴리스틱은 조작 판정 근거로 사용하지 않는다."

    self_disclosure_risk = float(text.get("self_disclosure_risk", 0))
    disclosed = self_disclosure_risk >= config.self_disclosure_gate

    signals = {
        "visual_risk": visual_risk,  # 내부 디버그용 원점수. 사용자에게 노출하지 않는다.
        "frames_analyzed": frames_analyzed,
        "frames_fake": int(deepfake.get("frames_fake", 0)),
        "classifier_used": classifier_used,
        "frames_with_face": frames_with_face,
        "face_model_available": bool(deepfake.get("face_model_available")),
        # 프레임별 원점수. 집계 방식을 튜닝하거나 결과가 이상할 때 어느 프레임
        # 때문인지 보려면 필요하다.
        "frame_scores": list(deepfake.get("frame_scores") or []),
        "frame_aggregation": config.frame_aggregation,
        "self_disclosure_risk": self_disclosure_risk,
        "vlm_summary": deepfake.get("vlm_summary"),
    }

    if visual_risk is None and not disclosed:
        reason = visual_unavailable_reason or "판단할 근거가 부족하다."
        return ManipulationAxis(
            status=ManipulationState.UNAVAILABLE.value,
            status_label=MANIPULATION_LABELS[ManipulationState.UNAVAILABLE.value],
            detail=f"{reason} 자가표기도 없어 판단을 유보한다.",
            signals=signals,
            evidence=list(deepfake.get("evidence", [])),
        )

    risk = visual_risk if visual_risk is not None else 0.0
    # 자가표기는 우리 분류기의 픽셀 추론보다 신뢰도가 높으므로 하한선으로 작동시킨다.
    if disclosed:
        risk = max(risk, self_disclosure_risk * config.self_disclosure_floor_ratio)
    risk = min(round(risk, 1), 100.0)
    signals["combined_risk"] = risk

    # 화면은 이 축에 "판단 근거 요약과 분석한 구간·프레임 범위"를 함께 보여준다.
    # 무엇을 봤는지 모르면 사용자가 결과의 범위를 가늠할 수 없으므로, 정상일 때도
    # 분석 범위를 남긴다. 한계가 있으면 그 뒤에 덧붙인다.
    if visual_risk is None:
        detail = f"{visual_unavailable_reason} 제목·설명의 자가표기만으로 판단했다."
    else:
        detail = f"프레임 {frames_analyzed}장을 분석해 그중 {frames_with_face}장에서 얼굴을 찾았다."
        if frames_with_face < frames_analyzed:
            detail += " 얼굴을 찾지 못한 프레임은 얼굴 기반 분류와 점수 집계에서 제외했다."

    evidence = list(deepfake.get("evidence", [])) + list(text.get("self_disclosure_evidence", []))
    status = categorize(risk)

    return ManipulationAxis(
        status=status,
        status_label=MANIPULATION_LABELS[status],
        detail=detail,
        signals=signals,
        evidence=evidence,
    )


def build_whole_video_generation(text: dict) -> ManipulationAxis:
    """영상 전체 AI 생성 탐지 축.

    전용 탐지 모델이 아직 없다(docs T-06: "설계 필요", 팀 결정 대기). 지금 가진
    유일한 근거는 업로더 본인의 자가표기뿐이라, 그것만 정직하게 쓴다. 모델이
    선정되면 이 함수만 교체하면 된다 — 응답 스키마는 이미 준비돼 있다.
    """
    self_disclosure_risk = float(text.get("self_disclosure_risk", 0))
    evidence = list(text.get("self_disclosure_evidence", []))
    signals = {"self_disclosure_risk": self_disclosure_risk, "model": None}

    if self_disclosure_risk >= config.self_disclosure_gate:
        return ManipulationAxis(
            status=ManipulationState.SUSPECTED.value,
            status_label=MANIPULATION_LABELS[ManipulationState.SUSPECTED.value],
            detail="제목·설명에 AI 생성 표기가 있다. 영상 자체를 분석하는 모델은 아직 없다.",
            signals=signals,
            evidence=evidence,
        )

    return ManipulationAxis(
        status=ManipulationState.UNAVAILABLE.value,
        status_label=MANIPULATION_LABELS[ManipulationState.UNAVAILABLE.value],
        detail=("영상 전체가 AI로 생성됐는지 판별하는 모델이 아직 선정되지 않았다. "
                "제목·설명의 자가표기만 확인했으며, 표기가 없다고 해서 AI 생성이 "
                "아니라는 뜻은 아니다."),
        signals=signals,
    )


def _is_degraded(stage_payload: dict, face_manipulation: ManipulationAxis,
                 whole_video_generation: ManipulationAxis,
                 claim_verification: ClaimVerification) -> bool:
    """이번 분석이 계획대로 다 돌았는지 판정한다.

    R-06/R-10에서 요구한 축을 구현하지 못한 것도 사용자가 받는 결과의 한계다.
    모델 미선정이나 옵션 해제로 축이 빠졌다고 전체 완료로 표시하지 않는다.
    """
    if any(axis.status == ManipulationState.UNAVAILABLE.value
           for axis in (face_manipulation, whole_video_generation)):
        return True
    if claim_verification.status == AxisStatus.UNAVAILABLE.value:
        return True
    unfinished = {"pending", "verifying", "failed", "timed_out"}
    if (any(claim_verification.summary.get(status, 0) for status in unfinished)
            or any(claim.get("status") in unfinished for claim in claim_verification.claims)):
        return True
    for name, stage in stage_payload.items():
        status = stage.get("status") if isinstance(stage, dict) else None
        if status == StageState.FAILED.value:
            return True
        if status == StageState.SKIPPED.value:
            return True
    return False


# 응답의 media 블록에 담을 항목. 파이프라인이 meta에 넣어 준 것 중 화면이 쓰는 것만.
_MEDIA_KEYS = (
    "title", "uploader", "duration", "video_id",
    "thumbnail", "upload_date", "language",
    "transcript_source", "stt_coverage_pct",
    "transcript_coverage_basis", "transcript_segment_coverage_pct", "transcript_coverage_detail",
)


def build(meta: dict, deepfake: dict, text: dict, stages: dict,
          claim_verification: ClaimVerification | None = None) -> AnalysisReport:
    face_manipulation = build_face_manipulation(deepfake, text)
    whole_video_generation = build_whole_video_generation(text)
    claims = claim_verification or ClaimVerification(
        status=AxisStatus.UNAVAILABLE.value,
        detail="주장 사실성 검증을 수행하지 않았다.",
    )

    stage_payload = {
        name: (asdict(s) if isinstance(s, StageStatus) else s) for name, s in stages.items()
    }
    degraded = _is_degraded(stage_payload, face_manipulation, whole_video_generation, claims)

    return AnalysisReport(
        url=meta.get("url", ""),
        # 화면에 필요한 영상 정보. 키를 하나씩 골라 담다가 나중에 추가한 필드가
        # 조용히 버려진 적이 있어, 무엇을 담는지 목록으로 두고 한 번에 채운다.
        media={key: meta.get(key) for key in _MEDIA_KEYS},
        analysis_status=(AnalysisState.PARTIAL if degraded else AnalysisState.COMPLETE).value,
        stages=stage_payload,
        face_manipulation=face_manipulation,
        whole_video_generation=whole_video_generation,
        claim_verification=claims,
        transcript=TranscriptInfo(
            summary=text.get("summary", ""),
            keywords=list(text.get("keywords", [])),
            tone=text.get("tone", ""),
            language=meta.get("language"),
            word_count=text.get("word_count"),
            coverage_pct=meta.get("stt_coverage_pct"),
            coverage_basis=meta.get("transcript_coverage_basis"),
            segment_coverage_pct=meta.get("transcript_segment_coverage_pct"),
            coverage_detail=meta.get("transcript_coverage_detail"),
            source=meta.get("transcript_source"),
            signals={
                "clickbait": text.get("clickbait_risk", 0),
                "claim_strength": text.get("claim_risk", 0),
                "tts": text.get("tts_risk", 0),  # 참고용. 어느 축 점수에도 반영되지 않는다
            },
        ),
    )


def _bar(value: float | None, width: int = 10) -> str:
    if value is None:
        return "?" * width
    filled = round(min(max(value, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _format_axis(title: str, axis: ManipulationAxis) -> list[str]:
    lines = [title, f"    판정   : {axis.status_label}"]
    if axis.detail:
        lines.append(f"    사유   : {axis.detail}")
    for e in axis.evidence:
        lines.append(f"    · {e}")
    if axis.signals.get("vlm_summary"):
        lines.append(f"    VLM    : {axis.signals['vlm_summary']}")
    if axis.signals.get("frames_analyzed") is not None:
        lines.append(
            f"    프레임 : {axis.signals.get('frames_fake', 0)} / "
            f"{axis.signals.get('frames_analyzed', 0)} 의심"
        )
    lines.append("")
    return lines


def format_text(r: AnalysisReport) -> str:
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

    lines += _format_axis("[1] 얼굴 합성·변형", r.face_manipulation)
    lines += _format_axis("[2] 영상 전체 AI 생성", r.whole_video_generation)

    cv = r.claim_verification
    lines.append("[3] 주장 사실성 검증")
    lines.append(f"    상태   : {cv.status}")
    if cv.summary:
        lines.append(
            f"    판정   : {VERDICT_LABELS['supported']} {cv.summary.get('supported', 0)} · "
            f"{VERDICT_LABELS['refuted']} {cv.summary.get('refuted', 0)} · "
            f"{VERDICT_LABELS['unverified']} {cv.summary.get('unverified', 0)} "
            f"(총 {cv.summary.get('total', 0)}건)"
        )
    if cv.detail:
        lines.append(f"    사유   : {cv.detail}")
    for claim in cv.claims:
        when = ""
        if claim.get("start") is not None:
            minutes, seconds = divmod(int(claim["start"]), 60)
            when = f"[{minutes:02d}:{seconds:02d}] "
        label = claim.get("verdict_label") or VERDICT_LABELS.get(
            claim.get("verdict"), "근거 부족")
        lines.append(f"    · ({label}) {when}{claim.get('text', '')[:80]}")
        if claim.get("reason"):
            lines.append(f"        사유: {claim['reason']}")
        if claim.get("insufficient_label"):
            lines.append(f"        근거 부족 사유: {claim['insufficient_label']}")
        if claim.get("quote"):
            lines.append(f"        인용: \"{claim['quote'][:100]}\"")
        for item in claim.get("evidence", [])[:2]:
            rating = f" — {item['rating']}" if item.get("rating") else ""
            kind = item.get("source_type_label") or item.get("source_type") or ""
            tag = f"{item.get('source')}/{kind}" if kind else str(item.get("source"))
            lines.append(f"        근거: [{tag}] {item.get('title', '')[:60]}{rating}")
            if item.get("url"):
                lines.append(f"              {item['url']}")
    lines.append("")

    lines.append("[참고] 음성 텍스트")
    lines.append(f"    요약   : {r.transcript.summary}")
    if r.transcript.keywords:
        lines.append(f"    키워드 : {', '.join(r.transcript.keywords)}")
    if r.transcript.coverage_pct is not None:
        lines.append(f"    입력 길이/자막 끝 시점 비율: {r.transcript.coverage_pct:.1f}%")
    if r.transcript.segment_coverage_pct is not None:
        lines.append(f"    텍스트 타임스탬프 구간 비율: {r.transcript.segment_coverage_pct:.1f}%")
    if r.transcript.coverage_detail:
        lines.append(f"    한계: {r.transcript.coverage_detail}")
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


def _safe_http_url(value: object) -> str:
    """HTML 속성 이스케이프와 별개로 실행 가능한 URL 스킴을 차단한다."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "#"
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname:
            return value
    except ValueError:
        pass
    return "#"


def _claims_html(cv: ClaimVerification, esc) -> str:
    """주장별 판정·근거를 목록으로 만든다. 근거 링크는 원문을 확인할 수 있게 남긴다."""
    if not cv.claims:
        return ""
    rows = []
    for claim in cv.claims:
        label = claim.get("verdict_label") or VERDICT_LABELS.get(
            claim.get("verdict"), "근거 부족")
        links = "".join(
            f'<li><a href="{esc(_safe_http_url(item.get("url")))}" rel="noopener noreferrer" '
            f'target="_blank">{esc(item.get("title"))}</a> '
            f'<small>({esc(item.get("source"))}{esc(" · " + item["rating"] if item.get("rating") else "")})</small></li>'
            for item in claim.get("evidence", [])[:3]
        )
        rows.append(
            f'<li><b>[{esc(label)}]</b> {esc(claim.get("text"))}'
            f'<br><small>{esc(claim.get("reason"))}</small>'
            + (f"<ul>{links}</ul>" if links else "")
            + "</li>"
        )
    summary = cv.summary or {}
    header = (f"<p>지지 {summary.get('supported', 0)} · 반박 {summary.get('refuted', 0)} · "
              f"판단 유보 {summary.get('unverified', 0)}</p>") if summary else ""
    return f"{header}<ul>{''.join(rows)}</ul>"


_AXIS_COLORS = {
    ManipulationState.SUSPECTED.value: "#e94d4d",
    ManipulationState.INCONCLUSIVE.value: "#e6a23c",
    ManipulationState.NO_CLEAR_SIGNS.value: "#3fb27f",
    ManipulationState.UNAVAILABLE.value: "#8a8f98",
}


def _axis_html(title: str, axis: ManipulationAxis, esc) -> str:
    color = _AXIS_COLORS.get(axis.status, "#8a8f98")
    ev = "".join(f"<li>{esc(e)}</li>" for e in axis.evidence)
    detail_html = f"<p><b>사유:</b> {esc(axis.detail)}</p>" if axis.detail else ""
    return f"""<div class="card"><h3>{esc(title)}</h3>
<span class="badge" style="color:{color}">{esc(axis.status_label)}</span>
{detail_html}<ul>{ev}</ul></div>"""


def format_html(r: AnalysisReport) -> str:
    # 제목·설명·근거는 외부(영상 업로더)에서 온 값이므로 반드시 이스케이프한다.
    def esc(value: object) -> str:
        return html.escape(str(value if value is not None else ""))

    kw = esc(", ".join(r.transcript.keywords))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>DeepCheck</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}}
.card{{border:1px solid #ddd;border-radius:14px;padding:24px;margin-top:20px}}
.badge{{font-size:1.4rem;font-weight:800}}
li{{margin:6px 0;font-size:.95rem}}</style></head><body>
<h1>DeepCheck — 영상 분석 결과</h1>
<p><a href="{esc(_safe_http_url(r.url))}">{esc(r.media.get('title') or r.url)}</a></p>
{_axis_html('1. 얼굴 합성·변형', r.face_manipulation, esc)}
{_axis_html('2. 영상 전체 AI 생성', r.whole_video_generation, esc)}
<div class="card"><h3>3. 주장 사실성 검증</h3>
<p>상태: {esc(r.claim_verification.status)}</p>
{_claims_html(r.claim_verification, esc)}
<p>{esc(r.claim_verification.detail)}</p></div>
<div class="card"><h3>참고: 음성 텍스트</h3><p>{esc(r.transcript.summary)}</p>
<p>{esc(r.transcript.coverage_detail)}</p>
<p><b>키워드:</b> {kw}</p>
<p><b>(점수 미반영)</b> 클릭베이트 {esc(r.transcript.signals.get('clickbait', 0))}/100 ·
주장 강도 {esc(r.transcript.signals.get('claim_strength', 0))}/100</p></div>
</body></html>"""
