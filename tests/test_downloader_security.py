"""No external downloads: metadata policy and yt-dlp options are tested with fakes."""
from types import SimpleNamespace

import pytest

from deepcheck import downloader
from deepcheck.errors import UnsupportedURLError, UnsupportedVideoError


def metadata(**changes):
    return {"availability": "public", "age_limit": 0, "duration": 140, **changes}


@pytest.mark.parametrize("changes", [
    {"availability": "unlisted"}, {"availability": "private"}, {"availability": None},
    {"age_limit": 18}, {"age_limit": None}, {"age_limit": False}, {"is_live": True},
    {"live_status": "is_upcoming"}, {"duration": 181}, {"duration": None},
    {"duration": float("nan")}, {"duration": float("inf")}, {"duration": -1},
])
def test_reject_before_download(monkeypatch, changes):
    monkeypatch.setattr(downloader, "config", SimpleNamespace(max_video_sec=180))
    with pytest.raises(UnsupportedVideoError):
        downloader._check_download_metadata(metadata(**changes))


def test_complete_metadata_and_incomplete_callback(monkeypatch):
    monkeypatch.setattr(downloader, "config", SimpleNamespace(max_video_sec=180))
    assert downloader._check_download_metadata(metadata()) is None
    assert downloader._check_download_metadata({}, incomplete=True) is None


def test_arbitrary_url_rejected_before_downloader_or_directory(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("URL rejection must precede network work")
    monkeypatch.setattr(downloader, "YoutubeDL", forbidden)
    target = tmp_path / "not-created"
    with pytest.raises(UnsupportedURLError):
        downloader.download("http://127.0.0.1/admin", str(target))
    assert not target.exists()


def test_tls_and_pre_download_filter(monkeypatch, tmp_path):
    class FakeYDL:
        def __init__(self, options):
            assert options["nocheckcertificate"] is False
            assert options["cachedir"] is False
            assert options["allowed_extractors"] == ["youtube$"]
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            assert url == "https://www.youtube.com/watch?v=cYRkZmBuDqI"
            self.options["match_filter"](metadata(availability="unlisted"), incomplete=False)
            pytest.fail("Invalid metadata must abort before bytes are saved")
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    with pytest.raises(UnsupportedVideoError):
        downloader.download("https://youtu.be/cYRkZmBuDqI?si=tracking", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_missing_audio_metadata_falls_back_and_video_height_stays_capped(monkeypatch, tmp_path):
    class FakeYDL:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            if ".a." in self.options["outtmpl"]:
                return None
            assert all("height<=360" in choice for choice in self.options["format"].split("/"))
            return metadata(id="cYRkZmBuDqI")
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(downloader, "_find", lambda path, video_id, marker: "video.mp4" if marker == ".v." else None)
    monkeypatch.setattr(downloader, "_fetch_captions", lambda *args: (None, None, None))
    media = downloader.download("https://youtu.be/cYRkZmBuDqI", str(tmp_path), max_height=360)
    assert media.audio_path == media.video_path == "video.mp4"


@pytest.mark.parametrize("availability", ["public", "unlisted"])
def test_real_ytdlp_invokes_filter_without_network_or_media_files(monkeypatch, tmp_path, availability):
    yt_dlp = pytest.importorskip("yt_dlp")
    calls = []

    def check(info, *, incomplete=False):
        calls.append(incomplete)
        return downloader._check_download_metadata(info, incomplete=incomplete)

    # process_ie_result takes already extracted metadata. simulate+skip_download
    # avoid YouTube/CDN requests; the global fixture also rejects network sockets.
    payload = metadata(
        id="cYRkZmBuDqI", title="offline fixture", availability=availability,
        url="https://media.invalid/test.mp4", ext="mp4", extractor="youtube",
        extractor_key="Youtube", webpage_url="https://www.youtube.com/watch?v=cYRkZmBuDqI",
    )
    options = {"quiet": True, "no_warnings": True, "simulate": True,
               "skip_download": True, "cachedir": False, "match_filter": check,
               "outtmpl": str(tmp_path / "%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(options) as ydl:
        if availability == "public":
            assert ydl.process_ie_result(payload, download=True)["id"] == "cYRkZmBuDqI"
        else:
            with pytest.raises(UnsupportedVideoError):
                ydl.process_ie_result(payload, download=True)
    assert False in calls
    assert list(tmp_path.iterdir()) == []
