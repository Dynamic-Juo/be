"""자동 생성 요청 디렉토리만 정리하고 사용자 지정 작업 경로는 보존한다."""

from pathlib import Path

import pytest

from deepcheck import downloader, pipeline, report
from deepcheck.errors import DownloadError


def _wire(monkeypatch, tmp_path, failure=None):
    workdir = tmp_path / "generated-job"

    def create(prefix):
        workdir.mkdir()
        return str(workdir)

    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", create)

    def download(url, directory, caption_policy=None):
        target = Path(directory)
        # 전부 이 테스트가 생성한 파일이다. 실제 영상/배포 디렉토리는 만지지 않는다.
        (target / "video.mp4").write_bytes(b"test-video")
        (target / "audio.m4a").write_bytes(b"test-audio")
        (target / "frames").mkdir(exist_ok=True)
        (target / "frames" / "frame_face.jpg").write_bytes(b"test-crop")
        if failure:
            raise failure
        return downloader.VideoMedia(url=url, workdir=directory,
                                     video_path=str(target / "video.mp4"), duration=10.0)

    monkeypatch.setattr(downloader, "download", download)
    monkeypatch.setattr(pipeline, "_extract_frames", lambda *args: [])
    monkeypatch.setattr(pipeline, "_collect_transcript", lambda *args: pipeline.TranscriptResult())
    monkeypatch.setattr(pipeline, "_verify_claims", lambda *args, **kwargs:
                        report.ClaimVerification())
    return workdir


def test_success_removes_download_audio_frames_and_crops(monkeypatch, tmp_path):
    workdir = _wire(monkeypatch, tmp_path)
    result = pipeline.analyze_url("https://example.com/v")
    assert not workdir.exists()
    assert result["stages"]["cleanup"]["status"] == "ok"


def test_download_failure_also_removes_partial_files(monkeypatch, tmp_path):
    workdir = _wire(monkeypatch, tmp_path, DownloadError("test failure"))
    with pytest.raises(DownloadError):
        pipeline.analyze_url("https://example.com/v")
    assert not workdir.exists()


def test_unexpected_failure_also_removes_partial_files(monkeypatch, tmp_path):
    workdir = _wire(monkeypatch, tmp_path, RuntimeError("test failure"))
    with pytest.raises(pipeline.DeepCheckError):
        pipeline.analyze_url("https://example.com/v")
    assert not workdir.exists()


def test_explicit_workdir_is_never_recursively_deleted(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    explicit = tmp_path / "user-workdir"
    explicit.mkdir()
    marker = explicit / "preexisting.txt"
    marker.write_text("keep")
    pipeline.analyze_url("https://example.com/v", pipeline.AnalysisOptions(workdir=str(explicit)))
    assert marker.read_text() == "keep"
    assert (explicit / "video.mp4").exists()


def test_keep_option_preserves_only_this_test_job(monkeypatch, tmp_path):
    workdir = _wire(monkeypatch, tmp_path)
    pipeline.analyze_url("https://example.com/v", pipeline.AnalysisOptions(keep_workdir=True))
    assert (workdir / "video.mp4").exists()


def test_cleanup_failure_is_logged_without_hiding_original_error(monkeypatch, tmp_path, caplog):
    _wire(monkeypatch, tmp_path, DownloadError("original failure"))

    def fail_cleanup(path):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(pipeline.shutil, "rmtree", fail_cleanup)
    with pytest.raises(DownloadError, match="original failure"):
        pipeline.analyze_url("https://example.com/v")
    assert "임시 분석 디렉토리 정리 실패" in caplog.text


def test_cleanup_failure_is_reported_when_analysis_returned_results(monkeypatch, tmp_path):
    workdir = _wire(monkeypatch, tmp_path)

    def fail_cleanup(path):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(pipeline.shutil, "rmtree", fail_cleanup)
    result = pipeline.analyze_url("https://example.com/v")
    assert workdir.exists()
    assert result["stages"]["cleanup"]["status"] == "failed"
    assert "서버 관리자" in result["stages"]["cleanup"]["detail"]
    assert result["analysis_status"] == "partial"
