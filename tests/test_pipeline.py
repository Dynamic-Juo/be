"""파이프라인 오케스트레이션 테스트.

무거운 의존성(다운로드·모델·네트워크)은 전부 가짜로 바꾼다. 검증하려는 건 로직이
아니라 순서다 — 미디어 조작 결과가 주장 검증을 기다리지 않고 먼저 오는지, 주장
카드가 pending으로 먼저 나타났다가 done으로 바뀌는지.
"""

from deepcheck import claims, deepfake, downloader, pipeline, transcriber


def _fake_media(**overrides) -> downloader.VideoMedia:
    """실제 `VideoMedia`로 가짜를 만든다.

    예전에는 필드를 손으로 흉내 낸 클래스를 썼는데, 실물에 필드가 추가되면
    가짜만 뒤처져서 테스트가 실물과 다른 것을 검증하게 됐다. 실제 dataclass를
    쓰면 필드가 늘어도 기본값으로 따라온다.
    """
    defaults = dict(
        url="https://www.youtube.com/watch?v=abc123",
        workdir="/tmp",
        video_path="/tmp/fake.mp4",
        audio_path="/tmp/fake.m4a",
        title="테스트 영상",
        description="설명",
        uploader="업로더",
        duration=30.0,
        video_id="abc123",
    )
    defaults.update(overrides)
    return downloader.VideoMedia(**defaults)


class _FakeDetector:
    def __init__(self, **kwargs):
        pass

    def analyze(self, frames):
        return deepfake.DeepfakeReport(
            frames_analyzed=2, frames_fake=0, avg_fake_score=10.0,
            method="heuristics+ViT-classifier", frames_with_face=2,
            face_model_available=True,
        )


def _fake_transcribe(source, model_size=None):
    # 사실 확인이 가능한 문장 하나(연도+퍼센트)를 준다 — extract_claims가 정확히
    # 1건을 뽑도록.
    text = "2024년 실업률이 3.2% 감소했다고 통계청이 발표했다."
    return transcriber.Transcript(
        text=text, language="ko", language_probability=0.99,
        segments=[{"start": 0.0, "end": 5.0, "text": text}], duration=5.0,
    )


def _wire_fast_pipeline(monkeypatch):
    """무거운 단계를 즉시 끝나는 가짜로 바꾼다."""
    monkeypatch.setattr(downloader, "download",
                        lambda url, tmp, caption_policy=None: _fake_media())
    monkeypatch.setattr(downloader, "extract_frames",
                        lambda video_path, frames_dir, max_frames=8: ["f1.jpg", "f2.jpg"])
    monkeypatch.setattr(deepfake, "DeepfakeDetector", _FakeDetector)
    monkeypatch.setattr(transcriber, "transcribe", _fake_transcribe)
    # 근거 검색 제공자를 비워 네트워크 호출 없이 즉시 "근거 없음"으로 끝나게 한다.
    monkeypatch.setattr(claims, "default_providers", lambda: [])


class TestIncrementalDelivery:
    def test_미디어_정보와_발언_출처가_필요한_단계_전에_전달된다(self, monkeypatch):
        _wire_fast_pipeline(monkeypatch)
        from deepcheck import llm
        monkeypatch.setattr(llm, "get_provider", lambda: None)
        seen = {}

        def progress(pct, message, stage=None):
            if stage == "transcribing":
                assert seen["media"]["title"] == "테스트 영상"
            if stage == "extracting_claims":
                assert seen["media"]["transcript_source"] == "stt"

        pipeline.analyze_url("https://example.com/v", progress_cb=progress,
                             on_partial=seen.update)

    def test_미디어_결과가_주장_결과보다_먼저_온다(self, monkeypatch):
        _wire_fast_pipeline(monkeypatch)
        events: list[dict] = []

        pipeline.analyze_url(
            "https://example.com/v",
            on_partial=lambda patch: events.append(patch),
        )

        keys_in_order = [set(e.keys()) for e in events]
        first_media_idx = next(i for i, k in enumerate(keys_in_order) if "face_manipulation" in k)
        first_claim_idx = next(
            i for i, k in enumerate(keys_in_order) if "claim_verification" in k
        )
        assert first_media_idx < first_claim_idx

    def test_주장_카드가_pending으로_먼저_나타났다가_done으로_바뀐다(self, monkeypatch):
        _wire_fast_pipeline(monkeypatch)
        claim_snapshots: list[dict] = []

        pipeline.analyze_url(
            "https://example.com/v",
            on_partial=lambda patch: (
                claim_snapshots.append(patch["claim_verification"])
                if "claim_verification" in patch else None
            ),
        )

        assert len(claim_snapshots) >= 2, "카드 생성 스냅샷과 완료 스냅샷 최소 2번은 와야 한다"
        first_card = claim_snapshots[0]["claims"][0]
        last_card = claim_snapshots[-1]["claims"][0]
        assert first_card["status"] == claims.PENDING
        assert last_card["status"] == claims.DONE

    def test_최종_반환값도_동일한_스키마다(self, monkeypatch):
        _wire_fast_pipeline(monkeypatch)
        result = pipeline.analyze_url("https://example.com/v")
        assert "face_manipulation" in result
        assert "whole_video_generation" in result
        assert result["claim_verification"]["claims"][0]["status"] == claims.DONE

    def test_콜백을_안_넘겨도_동작한다(self, monkeypatch):
        # CLI는 on_partial을 안 쓴다 — 기본값 None으로도 문제없이 끝까지 돌아야 한다.
        _wire_fast_pipeline(monkeypatch)
        result = pipeline.analyze_url("https://example.com/v")
        assert result["analysis_status"] in ("complete", "partial")


class TestSoftDeadline:
    def test_예산을_넘기면_남은_주장이_시간초과로_채워진다(self, monkeypatch):
        _wire_fast_pipeline(monkeypatch)

        # 이미 지난 시각을 데드라인으로 줘서, 검증 루프에 들어가자마자 넘긴 것으로 만든다.
        import time as time_module
        past_deadline = time_module.monotonic() - 1

        report_obj = pipeline._verify_claims(
            "2024년 실업률이 3.2% 감소했다고 통계청이 발표했다.",
            [{"start": 0.0, "end": 5.0, "text": "2024년 실업률이 3.2% 감소했다고 통계청이 발표했다."}],
            pipeline.AnalysisOptions(),
            pipeline.StageTracker(),
            deadline=past_deadline,
            progress=lambda *a, **k: None,
            partial=lambda patch: None,
        )
        assert report_obj.claims[0]["status"] == claims.TIMED_OUT
        assert report_obj.summary["timed_out"] == 1
