"""Aggregate all signals into a 0..100 risk score and format output (text/JSON/HTML)."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field


@dataclass
class RiskReport:
    url: str
    title: str | None
    uploader: str | None
    duration: float | None
    language: str | None
    video_risk: float  # 0..100 from deepfake detector
    text_risk: float  # 0..100 from text signals
    final_risk: float  # weighted 0..100
    verdict: str
    summary: str
    keywords: list[str]
    deepfake: dict
    text: dict
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _video_risk(det: dict) -> float:
    if "risk" in det:
        return float(det["risk"])
    return float(det.get("avg_fake_score", 0.0))


def _text_risk(t: dict) -> float:
    if "risk" in t:
        return float(t["risk"])
    # Fallback only -- analyzer.TextSignals now computes this itself (single
    # source of truth via __post_init__), so this path should rarely be hit.
    tts = float(t.get("tts_risk", 0))
    cb = float(t.get("clickbait_risk", 0))
    claim = float(t.get("claim_risk", 0))
    sd = float(t.get("self_disclosure_risk", 0))
    return round(tts * 0.35 + cb * 0.15 + claim * 0.10 + sd * 0.40, 1)


def _verdict(score: float) -> str:
    if score >= 70:
        return "AI가짜영상 의심 매우 높음"
    if score >= 45:
        return "AI가짜영상 의심 상당함"
    if score >= 25:
        return "일부 AI 생성 흔적 / 주의 필요"
    return "진짜일 가능성 높음"


def build(meta: dict, deepfake: dict, text: dict) -> RiskReport:
    video_risk = _video_risk(deepfake)
    text_risk = _text_risk(text)
    final = round(video_risk * 0.65 + text_risk * 0.35, 1)

    # An explicit self-disclosure ("이 영상은 AI로 만든 패러디입니다") is closer to
    # ground truth than anything our own classifiers infer from pixels/audio, so
    # a strong hit acts as a floor on the final score instead of being diluted
    # by the 0.35 text-weight in the blend above. This is what actually fixes
    # the known "AI 표기 영상인데 탐지 실패" gap end-to-end, not just the signal
    # existing in the data.
    self_disclosure_risk = float(text.get("self_disclosure_risk", 0))
    if self_disclosure_risk >= 50:
        final = max(final, round(self_disclosure_risk * 0.9, 1))
    final = min(final, 100.0)

    return RiskReport(
        url=meta["url"],
        title=meta.get("title"),
        uploader=meta.get("uploader"),
        duration=meta.get("duration"),
        language=meta.get("language"),
        video_risk=video_risk,
        text_risk=text_risk,
        final_risk=final,
        verdict=_verdict(final),
        summary=text.get("summary", ""),
        keywords=text.get("keywords", []),
        deepfake=deepfake,
        text=text,
        evidence=list(deepfake.get("evidence", [])) + list(text.get("self_disclosure_evidence", [])) + list(text.get("facts", [])),
    )


def _bar(value: float, width: int = 10) -> str:
    filled = round(min(max(value, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def format_text(r: RiskReport) -> str:
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append("DeepCheck — AI 가짜영상 위험도 분석")
    lines.append("=" * 62)
    lines.append(f"URL      : {r.url}")
    if r.title:
        lines.append(f"제목     : {r.title}")
    if r.uploader:
        lines.append(f"채널     : {r.uploader}")
    if r.duration:
        m, s = divmod(int(r.duration), 60)
        lines.append(f"길이     : {m:02d}:{s:02d}")
    if r.language:
        lines.append(f"언어     : {r.language}")
    lines.append("")
    lines.append(f"[영상] 분석 항목      : {r.deepfake.get('frames_analyzed', 0)}프레임 분석")
    lines.append(f"[영상] AI 의심 프레임 : {r.deepfake.get('frames_fake', 0)} / {r.deepfake.get('frames_analyzed', 0)}")
    lines.append(f"[영상] 방법           : {r.deepfake.get('method', '')}")
    lines.append(f"[영상] 위험도         : {_bar(r.video_risk)}  {r.video_risk:.0f}/100")
    for e in r.deepfake.get("evidence", []):
        if "VLM" not in e:
            lines.append(f"    · {e}")
    if r.deepfake.get("vlm_summary"):
        lines.append(f"[영상] VLM 근거       : {r.deepfake['vlm_summary']}")
    lines.append("")
    if r.text.get("self_disclosure_evidence"):
        lines.append(f"[텍스트] 자가표기 감지 : " + " / ".join(r.text["self_disclosure_evidence"]))
    lines.append(f"[텍스트] 요약      : {r.summary}")
    if r.keywords:
        lines.append(f"[텍스트] 키워드    : {', '.join(r.keywords)}")
    lines.append(f"[텍스트] TTS/AI음성 의심 : {_bar(r.text.get('tts_risk', 0))}  {r.text.get('tts_risk', 0)}/100")
    lines.append(f"[텍스트] 클릭베이트 : {_bar(r.text.get('clickbait_risk', 0))}  {r.text.get('clickbait_risk', 0)}/100")
    lines.append(f"[텍스트] 주장 강도  : {_bar(r.text.get('claim_risk', 0))}  {r.text.get('claim_risk', 0)}/100")
    lines.append("")
    lines.append("─" * 62)
    lines.append(f"[최종 위험도]  {_bar(r.final_risk)}  {r.final_risk:.0f}/100   →  {r.verdict}")
    lines.append("=" * 62)
    return "\n".join(lines)


def format_json(r: RiskReport) -> str:
    import json
    return json.dumps(r.to_dict(), ensure_ascii=False, indent=2)


def format_html(r: RiskReport) -> str:
    color = "#e94d4d" if r.final_risk >= 45 else "#e6a23c" if r.final_risk >= 25 else "#3fb27f"
    ev = "".join(f"<li>{e}</li>" for e in r.evidence)
    vlm = r.deepfake.get("vlm_summary") or ""
    kw = ", ".join(r.keywords)
    sd = r.text.get("self_disclosure_evidence") or []
    sd_html = ("<p><b>자가표기 감지:</b> " + " / ".join(sd) + "</p>") if sd else ""
    bar_w = r.final_risk
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>DeepCheck</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}}
.card{{border:1px solid #ddd;border-radius:14px;padding:24px;margin-top:20px}}
.badge{{font-size:2rem;font-weight:800;color:{color}}}
.meter{{height:14px;background:#eee;border-radius:8px;overflow:hidden}}
.fill{{height:100%;width:{bar_w}%;background:{color}}}
li{{margin:6px 0;font-size:.95rem}}</style></head><body>
<h1>🕵️ DeepCheck — 영상 위험도 분석</h1>
<p><a href="{r.url}">{r.title or r.url}</a></p>
<div class="card"><span class="badge">{r.final_risk:.0f}/100</span>
<div class="meter"><div class="fill"></div></div>
<p>{r.verdict}</p>
<p><b>제목:</b> {r.title or '-'} · <b>채널:</b> {r.uploader or '-'} · <b>언어:</b> {r.language or '-'}</p>
<p><b>영상(딥페이크):</b> {r.video_risk:.0f}/100 ({r.deepfake.get('frames_fake',0)}/{r.deepfake.get('frames_analyzed',0)} 프레임) ·
<b>텍스트(AI):</b> {r.text_risk:.0f}/100</p>
</div>
<div class="card"><h3>요약</h3><p>{r.summary}</p><p><b>키워드:</b> {kw}</p>{sd_html}</div>
<div class="card"><h3>근거</h3>{ev}{('<p><b>VLM:</b> '+vlm+'</p>') if vlm else ''}</div>
</body></html>"""
