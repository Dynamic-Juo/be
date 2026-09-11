"""외부 모델 없이 검증하는 얼굴 분류 입력과 음성 지표의 회귀 테스트."""

import pytest

from deepcheck import deepfake, face, transcriber


def _detector(monkeypatch, crops, scores):
    detector = deepfake.DeepfakeDetector()
    monkeypatch.setattr(face, "crop_face", lambda path: crops.get(path))
    monkeypatch.setattr(face, "available", lambda: True)
    monkeypatch.setattr(detector, "_heuristics", lambda path: [])
    monkeypatch.setattr(detector, "_vlm_analyze", lambda paths: (None, None, None))
    calls = []

    def score(path):
        calls.append(path)
        return scores[path], "fake"

    monkeypatch.setattr(detector, "_classifier_score", score)
    return detector, calls


def test_faceless_frames_never_reach_face_classifier(monkeypatch):
    detector, calls = _detector(monkeypatch, {"face.jpg": "face_crop.jpg"},
                               {"face_crop.jpg": 0.9})
    result = detector.analyze(["blank1.jpg", "face.jpg", "blank2.jpg"])
    assert calls == ["face_crop.jpg"]
    assert result.frames_with_face == 1
    assert result.frame_scores == [0.9]
    assert result.avg_fake_score == 90.0


def test_no_faces_does_not_invent_classifier_score(monkeypatch):
    detector, calls = _detector(monkeypatch, {}, {})
    result = detector.analyze(["blank.jpg"])
    assert not calls
    assert result.frame_scores == []
    assert result.frames_fake == 0
    assert "ViT-classifier" not in result.method


@pytest.mark.parametrize("response", [
    [{"label": "real", "score": 0.99}],
    [{"label": "LABEL_0", "score": 0.99}],
    [{"label": "fake", "score": float("nan")}],
    [{"label": "fake", "score": float("inf")}],
    [{"label": "fake", "score": -0.1}],
    [{"label": "fake", "score": 1.1}],
    [{"label": "fake", "score": "invalid"}],
    [{"label": "fake"}], [], None,
])
def test_unknown_or_invalid_classifier_output_is_unavailable(monkeypatch, response):
    detector = deepfake.DeepfakeDetector()
    monkeypatch.setattr(detector, "_load_classifier", lambda: lambda path: response)
    assert detector._classifier_score("unused.jpg") == (None, None)


def test_known_fake_label_is_not_the_first_result(monkeypatch):
    detector = deepfake.DeepfakeDetector()
    monkeypatch.setattr(detector, "_load_classifier", lambda: lambda path: [
        {"label": "Real", "score": 0.99}, {"label": "Fake", "score": 0.01},
    ])
    assert detector._classifier_score("unused.jpg") == (0.01, "Fake")


def test_face_availability_reports_loaded_detector_not_file_presence(monkeypatch):
    monkeypatch.setattr(face, "_detector", None)
    monkeypatch.setattr(face.os.path, "exists", lambda path: True)
    assert face.available() is False


def test_segment_coverage_clamps_and_unions_nonempty_intervals():
    segments = [
        {"start": -3, "end": 2, "text": "앞"},
        {"start": 1, "end": 4, "text": "중복"},
        {"start": 8, "end": 20, "text": "뒤"},
        {"start": 4, "end": 8, "text": " "},
        {"start": 6, "end": 5, "text": "역순"},
        {"start": "bad", "end": 7, "text": "잘못된 값"},
        {"start": 5, "end": float("nan"), "text": "유효하지 않은 값"},
    ]
    assert transcriber.segment_coverage_pct(segments, 10.0) == 60.0
    assert transcriber.segment_coverage_pct([], 10.0) == 0.0


@pytest.mark.parametrize("duration", [None, 0, -1, float("nan"), float("inf")])
def test_unknown_duration_cannot_produce_segment_coverage(duration):
    assert transcriber.segment_coverage_pct([], duration) is None
