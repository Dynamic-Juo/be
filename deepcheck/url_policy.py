"""Public analysis input: canonical YouTube video URLs only.

This is an input boundary, not an outbound firewall. Shorts classification,
visibility, language and duration still require trusted video metadata checks.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .errors import UnsupportedURLError

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


def normalize_youtube_url(url: str) -> str:
    """Reject arbitrary network targets and discard user-controlled redirect options."""
    message = "YouTube 영상의 watch, shorts 또는 youtu.be 주소를 입력해주세요."
    try:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("invalid URL character")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("invalid URL authority")
        expected_port = 443 if parsed.scheme == "https" else 80
        if parsed.port not in {None, expected_port}:
            raise ValueError("non-default port")
        host = parsed.hostname
        if host == "youtu.be":
            video_id = parsed.path.removeprefix("/")
        elif host in _YOUTUBE_HOSTS and parsed.path == "/watch":
            ids = parse_qs(parsed.query).get("v", [])
            if len(ids) != 1:
                raise ValueError("ambiguous video ID")
            video_id = ids[0]
        elif host in _YOUTUBE_HOSTS and parsed.path.startswith("/shorts/"):
            video_id = parsed.path.removeprefix("/shorts/")
        else:
            raise ValueError("unsupported video URL")
        if not _VIDEO_ID.fullmatch(video_id):
            raise ValueError("invalid video ID")
    except (TypeError, ValueError) as exc:
        raise UnsupportedURLError(message, stage="input") from exc
    return f"https://www.youtube.com/watch?v={video_id}"
