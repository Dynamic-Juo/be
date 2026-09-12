"""옵션 기본값이 CLI·API·harness에서 갈리지 않는지 검증한다.

개편 전에는 같은 옵션의 기본값이 세 곳에 따로 적혀 있었고, use_classifier가
CLI에서는 False, API에서는 True여서 진입 경로에 따라 다른 알고리즘이 돌았다.
"""

from deepcheck.config import Config, load_config
from deepcheck.pipeline import AnalysisOptions


def test_알_수_없는_키는_무시한다():
    opts = AnalysisOptions.from_dict({"model_size": "tiny", "세션": "x", "url": "u"})
    assert opts.model_size == "tiny"


def test_None_값은_기본값을_덮어쓰지_않는다():
    opts = AnalysisOptions.from_dict({"max_frames": None})
    assert opts.max_frames == AnalysisOptions().max_frames


def test_API_요청_모델과_파이프라인_기본값이_같다():
    from backend.app import AnalyzeRequest

    api_defaults = AnalyzeRequest(url="https://example.com/v").model_dump(
        exclude={"session_id", "url"}
    )
    pipeline_defaults = AnalysisOptions()
    for key, value in api_defaults.items():
        assert getattr(pipeline_defaults, key) == value, f"기본값 불일치: {key}"


def test_주장검증은_기본적으로_켜져있다():
    # PRD가 정의한 두 축 중 하나이므로 기본 동작에 포함한다.
    assert AnalysisOptions().enable_claim_verification is True


def test_자막_정책_기본값은_미사용이다(monkeypatch):
    monkeypatch.delenv("DEEPCHECK_CAPTION_POLICY", raising=False)
    assert Config.caption_policy == "off"
    assert load_config().caption_policy == "off"
    assert AnalysisOptions().caption_policy == "off"


def test_compose_does_not_silently_override_the_caption_default():
    from pathlib import Path
    import re

    # Static file inspection only: never invoke Compose or read deployment env.
    source = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    match = re.search(r"DEEPCHECK_CAPTION_POLICY:\s*\"\$\{DEEPCHECK_CAPTION_POLICY:-([^}]+)\}", source)
    assert match and match.group(1) == Config.caption_policy


def test_명시적인_수동_CC_설정은_기본값보다_우선한다(monkeypatch):
    monkeypatch.setenv("DEEPCHECK_CAPTION_POLICY", "manual")
    assert load_config().caption_policy == "manual"


def test_API에서_자막을_옵션으로_선택할_수_있다():
    from backend.app import AnalyzeRequest

    for policy in ("off", "manual", "any"):
        request = AnalyzeRequest(url="https://www.youtube.com/watch?v=cYRkZmBuDqI",
                                 caption_policy=policy)
        assert AnalysisOptions.from_dict(request.model_dump()).caption_policy == policy


def test_명시적인_자동_CC_허용_설정도_유지한다(monkeypatch):
    monkeypatch.setenv("DEEPCHECK_CAPTION_POLICY", "any")
    assert load_config().caption_policy == "any"


def test_디버그_목록은_기본적으로_닫는다(monkeypatch):
    monkeypatch.delenv("DEEPCHECK_ENABLE_DEBUG_ENDPOINTS", raising=False)
    assert load_config().enable_debug_endpoints is False


def test_환경변수로_설정을_덮어쓸_수_있다(monkeypatch):
    monkeypatch.setenv("DEEPCHECK_MAX_FRAMES", "16")
    monkeypatch.setenv("DEEPCHECK_LEVEL_HIGH", "80")
    cfg = load_config()
    assert cfg.max_frames == 16
    assert cfg.level_high == 80.0


def test_잘못된_환경변수_값은_기본값으로_되돌아간다(monkeypatch):
    monkeypatch.setenv("DEEPCHECK_MAX_FRAMES", "여덟장")
    assert load_config().max_frames == Config.max_frames
