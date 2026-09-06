"""yt-dlp wrapper: download a video URL and expose metadata + media artifact paths."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover
    YoutubeDL = None


@dataclass
class VideoMedia:
    """Paths + metadata resulting from a download."""
    url: str
    workdir: str
    video_path: str
    audio_path: str | None = None
    title: str | None = None
    video_id: str | None = None
    duration: float | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    description: str | None = None
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return os.path.splitext(self.video_path)[0]


def _ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


def download(url: str, workdir: str, keep: bool = False, max_height: int | None = 720) -> VideoMedia:
    """Download best available mp4 (video+audio merged) for a URL.

    Uses the yt-dlp Python API so it stays resilient on a single process.
    Returns a VideoMedia dataclass. Raises RuntimeError on failure.
    """
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed. Run: uv pip install -r requirements.txt")

    os.makedirs(workdir, exist_ok=True)

    def inner() -> VideoMedia:
        # YouTube (and most sites) expose separate video-only and audio-only DASH
        # streams. Rather than relying on a JS runtime + ffmpeg to expose/merge a
        # combined format, we fetch each stream independently and feed it to the
        # stage that needs it (frames ← video, STT ← audio). This works with just
        # PyAV — no system ffmpeg required.
        base: dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "socket_timeout": 30,
            "nocheckcertificate": True,
        }

        video_tmpl = os.path.join(workdir, "%(id)s.v.%(ext)s")
        audio_tmpl = os.path.join(workdir, "%(id)s.a.%(ext)s")
        video_fmt = f"bv[ext=mp4][height<={max_height}]/bv[ext=mp4]/b[ext=mp4]/b"
        audio_fmt = "ba[ext=m4a]/ba[acodec!=none]/ba/b"

        info: dict[str, Any] = {}
        video_path = audio_path = None

        try:
            with YoutubeDL({**base, "format": video_fmt, "outtmpl": video_tmpl}) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            raise RuntimeError(f"video download failed: {e}")

        video_id = info.get("id")
        video_path = _find(workdir, video_id, marker=".v.")

        try:
            with YoutubeDL({**base, "format": audio_fmt, "outtmpl": audio_tmpl}) as ydl:
                ainfo = ydl.extract_info(url, download=True)
        except Exception:
            ainfo = info  # keep metadata; STT can fall back to video container

        audio_path = _find(workdir, ainfo.get("id") or video_id, marker=".a.")
        if audio_path is None and video_path:
            audio_path = video_path  # no separate audio stream available

        return VideoMedia(
            url=url,
            workdir=workdir,
            video_path=video_path,
            audio_path=audio_path,
            title=info.get("title"),
            video_id=video_id,
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=info.get("thumbnail"),
            description=info.get("description"),
            info=info,
        )

    return inner()


def _find(workdir: str, video_id: str | None, marker: str) -> str | None:
    """Locate a downloaded stream file by id + marker (e.g. '.v.' / '.a.').

    Strict: returns None unless the exact id+marker match exists, so audio and
    video streams are never confused with each other.
    """
    if not video_id:
        return None
    cands = [f for f in os.listdir(workdir) if f.startswith(video_id) and marker in f]
    cands.sort(key=lambda f: os.path.getmtime(os.path.join(workdir, f)), reverse=True)
    return os.path.join(workdir, cands[0]) if cands else None


def extract_audio(media: VideoMedia, fmt: str = "wav", sample_rate: int = 16000) -> str | None:
    """Convert the video to mono 16k wav using system ffmpeg if available.

    Returns the audio path, or None if ffmpeg is not installed (faster-whisper's
    PyAV can still decode the mp4 directly).
    """
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return None

    audio_path = os.path.join(media.workdir, f"{os.path.splitext(os.path.basename(media.video_path))[0]}.wav")
    if os.path.exists(audio_path):
        return audio_path

    cmd = [
        ffmpeg, "-y", "-i", media.video_path,
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", fmt, audio_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except subprocess.CalledProcessError:
        return None
    return audio_path


def extract_frames(video_path: str, frames_dir: str, max_frames: int = 8) -> list[str]:
    """Sample up to max_frames evenly spaced frames from a video into frames_dir.

    Uses ffmpeg if present, else falls back to PyAV. Returns sorted frame paths.
    """
    os.makedirs(frames_dir, exist_ok=True)
    ffmpeg = _ffmpeg_binary()

    # Try to get duration via ffprobe for even spacing.
    duration = None
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", video_path],
                capture_output=True, check=True, text=True, timeout=120,
            ).stdout
            import json
            probed = json.loads(out)
            duration = float(probed["format"].get("duration", 0))
        except Exception:
            duration = None

    if not duration:
        duration = _duration_pyav(video_path)

    if ffmpeg and duration:
        frames: list[str] = []
        for i in range(max_frames):
            ts = duration * i / max_frames if max_frames > 1 else duration / 2
            ts = max(ts, 0.0)
            out = os.path.join(frames_dir, f"frame_{i:03d}.jpg")
            cmd = [ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", video_path,
                   "-frames:v", "1", "-q:v", "2", "-vf", "scale=480:-2", out]
            probes: list[str] = []
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=120)
                frames.append(out)
            except subprocess.CalledProcessError:
                probes.append(out)
        if frames:
            return sorted(frames)

    # Fallback: PyAV (bundled with faster-whisper requirements) extraction.
    return _frames_pyav(video_path, frames_dir, max_frames)


def _duration_pyav(video_path: str) -> float | None:
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(video_path) as c:
            if c.duration:
                return c.duration / av.time_base
    except Exception:
        return None
    return None


def _frames_pyav(video_path: str, frames_dir: str, max_frames: int) -> list[str]:
    """PyAV fallback frame sampler.

    Previously this just grabbed the FIRST `max_frames` decoded frames, which
    (when ffmpeg wasn't installed, so this path was actually running) meant we
    only ever looked at roughly the first second of the video regardless of its
    length. Fixed to spread samples evenly across the whole clip, same as the
    ffmpeg seek path above.
    """
    try:
        import av
        import PIL.Image as Image
    except ImportError:
        return []

    duration = _duration_pyav(video_path)
    frames: list[str] = []

    if duration and duration > 0:
        try:
            container = av.open(video_path)
            targets = [duration * i / max_frames for i in range(max_frames)]
            target_idx = 0
            for frame in container.decode(video=0):
                if target_idx >= len(targets):
                    break
                t = float(frame.time) if frame.time is not None else None
                if t is None:
                    continue
                if t >= targets[target_idx]:
                    out = os.path.join(frames_dir, f"frame_{target_idx:03d}.jpg")
                    frame.to_image().save(out, "JPEG", quality=85)
                    frames.append(out)
                    target_idx += 1
            container.close()
        except Exception:
            frames = []
        if frames:
            return sorted(frames)

    # Duration unknown: sample every Nth decoded frame using the stream's
    # reported frame count, still spread across the clip rather than only
    # reading the very start.
    try:
        container = av.open(video_path)
        stream = container.streams.video[0]
        total = stream.frames or 0
        stride = max(total // max_frames, 1) if total else 1
        wanted = set(i * stride for i in range(max_frames))
        idx = 0
        for frame in container.decode(video=0):
            if idx in wanted and frame is not None:
                out = os.path.join(frames_dir, f"frame_{len(frames):03d}.jpg")
                frame.to_image().save(out, "JPEG", quality=85)
                frames.append(out)
            idx += 1
            if len(frames) >= max_frames or idx > (total or max_frames * 50):
                break
        container.close()
    except Exception:
        return frames
    return sorted(frames)
