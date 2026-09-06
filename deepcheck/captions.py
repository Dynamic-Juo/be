"""영상에 이미 달려 있는 자막을 읽는다.

설계 문서(`design/ai-pipeline.md`)의 발언 텍스트 확보 흐름은 "사용 가능한 자막이
있으면 자막을 쓰고, 없을 때 음성 인식으로 넘어간다"이다. 자막이 있는데도 매번 STT를
돌리면 수십 초를 그냥 버린다.

기본값은 **수동 자막만** 사용한다. 자동 생성 자막은 결국 다른 STT의 출력이라
품질이 확실히 낫다고 보기 어렵기 때문이다. 속도가 더 급하면 `caption_policy`를
`any`로 두어 자동 자막까지 쓸 수 있다.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 수동 자막 우선, 그다음 자동 자막. 언어는 한국어 → 영어 순으로 찾는다.
PREFERRED_LANGUAGES = ("ko", "en")

_TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
# VTT 큐 안에 들어가는 인라인 태그(<c>, <00:00:01.000> 등)를 걷어낸다.
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class CaptionTrack:
    """자막에서 뽑은 발언 텍스트."""

    text: str
    language: str | None
    source: str  # "manual" | "automatic"
    segments: list[dict] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def duration(self) -> float | None:
        return self.segments[-1]["end"] if self.segments else None


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(path: str) -> list[dict]:
    """WebVTT/SRT를 세그먼트 목록으로 바꾼다.

    자막 하나 읽자고 의존성을 더하지 않는다. 두 형식 모두 "타임스탬프 줄 + 본문 줄"
    구조라 같은 파서로 처리한다.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("자막 파일을 읽지 못했습니다(%s): %s", os.path.basename(path), e)
        return []

    segments: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        match = _TIMESTAMP_RE.search(stripped)
        if match:
            if current and current["text"]:
                segments.append(current)
            current = {
                "start": round(_to_seconds(*match.groups()[:4]), 2),
                "end": round(_to_seconds(*match.groups()[4:]), 2),
                "text": "",
            }
            continue
        if current is None or not stripped:
            continue
        if stripped.isdigit() or stripped.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
            continue
        cleaned = _TAG_RE.sub("", stripped).strip()
        if not cleaned:
            continue
        # 자동 자막은 같은 문장을 겹쳐 반복 표시하는 경우가 많아 중복을 걸러낸다.
        if cleaned in current["text"]:
            continue
        current["text"] = f"{current['text']} {cleaned}".strip()

    if current and current["text"]:
        segments.append(current)
    return segments


def pick_track(subtitles: dict, automatic_captions: dict, policy: str = "manual"
               ) -> tuple[str, str, list[dict]] | None:
    """yt-dlp가 알려준 자막 목록에서 쓸 트랙을 고른다.

    반환값은 (언어, 종류, 포맷 목록)이며, 쓸 자막이 없으면 None이다.
    """
    if policy == "off":
        return None

    candidates: list[tuple[str, dict]] = [("manual", subtitles or {})]
    if policy == "any":
        candidates.append(("automatic", automatic_captions or {}))

    for source, table in candidates:
        for language in PREFERRED_LANGUAGES:
            for key, formats in table.items():
                # yt-dlp는 "ko", "en-US", "ko-KR" 같은 키를 준다.
                if key == language or key.startswith(f"{language}-"):
                    if formats:
                        return key, source, formats
    return None


def load_track(path: str, language: str, source: str) -> CaptionTrack | None:
    """내려받은 자막 파일을 CaptionTrack으로 만든다. 내용이 없으면 None."""
    segments = parse_vtt(path)
    if not segments:
        logger.warning("자막 파일에서 읽을 내용이 없습니다: %s", os.path.basename(path))
        return None

    text = " ".join(s["text"] for s in segments).strip()
    if not text:
        return None

    track = CaptionTrack(text=text, language=language.split("-")[0], source=source,
                         segments=segments)
    logger.info(
        "자막 사용: %s(%s), %d세그먼트 %d단어 — STT를 건너뛴다",
        language, source, len(segments), track.word_count,
    )
    return track
