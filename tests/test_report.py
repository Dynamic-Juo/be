"""리포트 조립 로직 테스트.

무거운 모델·네트워크는 건드리지 않고, 점수와 상태 판정 규칙만 검증한다.
특히 "분석 실패를 정상 판정으로 둔갑시키지 않는다"는 이번 개편의 핵심 규칙을
회귀로부터 지키는 것이 목적이다.
"""

from deepcheck import report
from deepcheck.config import config


def _deepfake(frames_analyzed=8, frames_fake=0, avg=0.0, method="heuristics+ViT-classifier",
              evidence=None, vlm_summary=None):
    return {
        "frames_analyzed": frames_analyzed,
        "frames_fake": frames_fake,
        "avg_fake_score": avg,
        "method": method,
        "evidence": evidence or [],
        "vlm_summary": vlm_summary,
    }


def _text(tts=0, clickbait=0, claim=0, self_disclosure=0, evidence=None):
    return {
        "summary": "요약",
        "keywords": ["a"],
        "tone": "중립",
        "tts_risk": tts,
        "clickbait_risk": clickbait,
        "claim_risk": claim,
        "self_disclosure_risk": self_disclosure,
        "self_disclosure_evidence": evidence or [],
        "word_count": 100,
        "facts": [],
    }


class TestUnavailable:
    def test_프레임이_없고_자가표기도_없으면_판단_불가(self):
        mm = report.build_media_manipulation(_deepfake(frames_analyzed=0, method="none"), _text())
        assert mm.status == report.AxisStatus.UNAVAILABLE.value
        assert mm.risk is None
        assert mm.level == report.LEVEL_UNKNOWN

    def test_프레임이_없어도_자가표기가_있으면_판정한다(self):
        mm = report.build_media_manipulation(
            _deepfake(frames_analyzed=0, method="none"),
            _text(self_disclosure=70, evidence=['제목/설명에 자가표기 발견: "ai generated"']),
        )
        assert mm.status == report.AxisStatus.ANALYZED.value
        assert mm.risk == 63.0  # 70 * 0.9 (자가표기 플로어)
        assert "자가표기" in (mm.detail or "")

    def test_합성음성_의심만으로는_판정하지_않는다(self):
        mm = report.build_media_manipulation(
            _deepfake(frames_analyzed=0, method="none"), _text(tts=80)
        )
        assert mm.status == report.AxisStatus.UNAVAILABLE.value
        assert mm.risk is None


class TestScoring:
    def test_자가표기가_영상점수보다_높으면_하한선으로_작동한다(self):
        mm = report.build_media_manipulation(
            _deepfake(avg=10.0), _text(self_disclosure=90)
        )
        assert mm.risk == 81.0  # 90 * 0.9

    def test_자가표기가_임계_미만이면_플로어가_발동하지_않는다(self):
        below = config.self_disclosure_gate - 1
        mm = report.build_media_manipulation(_deepfake(avg=10.0), _text(self_disclosure=below))
        assert mm.risk == 10.0

    def test_클릭베이트와_주장강도는_조작_점수에_반영되지_않는다(self):
        clean = report.build_media_manipulation(_deepfake(avg=20.0), _text())
        noisy = report.build_media_manipulation(
            _deepfake(avg=20.0), _text(clickbait=100, claim=100)
        )
        assert clean.risk == noisy.risk == 20.0

    def test_등급_경계(self):
        assert report.level_for(None) == report.LEVEL_UNKNOWN
        assert report.level_for(config.level_high) == report.LEVEL_HIGH
        assert report.level_for(config.level_moderate) == report.LEVEL_MODERATE
        assert report.level_for(config.level_caution) == report.LEVEL_CAUTION
        assert report.level_for(config.level_caution - 0.1) == report.LEVEL_LOW

    def test_분류기를_못쓰면_사유를_남긴다(self):
        mm = report.build_media_manipulation(_deepfake(avg=5.0, method="heuristics"), _text())
        assert "분류기" in (mm.detail or "")


class TestBuild:
    def _stages(self, **overrides):
        stages = {
            "download": report.StageStatus(status=report.StageState.OK.value),
            "frames": report.StageStatus(status=report.StageState.OK.value),
            "transcript": report.StageStatus(status=report.StageState.OK.value),
        }
        stages.update(overrides)
        return stages

    def test_모든_단계가_정상이면_complete(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        assert r.analysis_status == report.AnalysisState.COMPLETE.value

    def test_주장검증이_꺼져있는_것은_degraded가_아니다(self):
        # 기본 구성에서 항상 partial이 뜨면 이 플래그가 무의미해진다.
        stages = self._stages(
            claim_verification=report.StageStatus(
                status=report.StageState.SKIPPED.value, detail="옵션이 꺼져 있음"
            )
        )
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), stages)
        assert r.analysis_status == report.AnalysisState.COMPLETE.value

    def test_다른_단계가_건너뛰어지면_partial(self):
        stages = self._stages(
            media_manipulation=report.StageStatus(
                status=report.StageState.SKIPPED.value, detail="분석할 프레임이 없음"
            )
        )
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), stages)
        assert r.analysis_status == report.AnalysisState.PARTIAL.value

    def test_단계가_실패하면_partial(self):
        stages = self._stages(
            transcript=report.StageStatus(status=report.StageState.FAILED.value, detail="STT 실패")
        )
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), stages)
        assert r.analysis_status == report.AnalysisState.PARTIAL.value

    def test_조작축이_판단불가면_partial(self):
        r = report.build({"url": "u"}, _deepfake(frames_analyzed=0), _text(), self._stages())
        assert r.analysis_status == report.AnalysisState.PARTIAL.value

    def test_주장검증은_기본적으로_미구현_상태(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        assert r.claim_verification.status == report.AxisStatus.NOT_IMPLEMENTED.value

    def test_응답_스키마_필수_키(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        payload = r.to_dict()
        assert set(payload) == {
            "url", "media", "analysis_status", "stages",
            "media_manipulation", "claim_verification", "transcript",
        }
        assert set(payload["media_manipulation"]) >= {"status", "risk", "level", "signals"}

    def test_dict_왕복시_모르는_키가_있어도_죽지_않는다(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        payload = r.to_dict()
        payload["앞으로_추가될_필드"] = 1
        restored = report.AnalysisReport.from_dict(payload)
        assert restored.media_manipulation.risk == r.media_manipulation.risk


class TestFormatting:
    def test_HTML_출력이_제목을_이스케이프한다(self):
        r = report.build(
            {"url": "u", "title": '<script>alert(1)</script>'},
            _deepfake(avg=10.0), _text(), {},
        )
        html_out = report.format_html(r)
        assert "<script>alert(1)</script>" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_판단불가는_텍스트_출력에서도_점수로_보이지_않는다(self):
        r = report.build({"url": "u"}, _deepfake(frames_analyzed=0, method="none"), _text(), {})
        text_out = report.format_text(r)
        assert report.LEVEL_UNKNOWN in text_out
        # 조작 축에는 위험도 막대를 그리지 않는다(참고용 텍스트 신호 줄은 별개다).
        assert "위험도" not in text_out
