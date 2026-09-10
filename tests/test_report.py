"""리포트 조립 로직 테스트.

무거운 모델·네트워크는 건드리지 않고, 점수와 상태 판정 규칙만 검증한다.
특히 "분석 실패를 정상 판정으로 둔갑시키지 않는다"는 규칙과, 얼굴 합성·변형과
영상 전체 AI 생성 두 축이 끝까지 분리된 채로 남는다는 규칙을 회귀로부터 지킨다.
"""

from deepcheck import report
from deepcheck.config import config


def _deepfake(frames_analyzed=8, frames_fake=0, avg=0.0, method="heuristics+ViT-classifier",
              evidence=None, vlm_summary=None, frames_with_face=None,
              face_model_available=True):
    return {
        "frames_analyzed": frames_analyzed,
        "frames_fake": frames_fake,
        "avg_fake_score": avg,
        "method": method,
        "evidence": evidence or [],
        "vlm_summary": vlm_summary,
        # 기본은 "모든 프레임에서 얼굴을 찾았다"로 둔다.
        "frames_with_face": frames_analyzed if frames_with_face is None else frames_with_face,
        "face_model_available": face_model_available,
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


class TestCategorize:
    def test_경계값(self):
        assert report.categorize(None) == report.ManipulationState.UNAVAILABLE.value
        assert report.categorize(config.level_high) == report.ManipulationState.SUSPECTED.value
        assert (report.categorize(config.level_moderate)
                == report.ManipulationState.INCONCLUSIVE.value)
        assert (report.categorize(config.level_moderate - 0.1)
                == report.ManipulationState.NO_CLEAR_SIGNS.value)

    def test_라벨이_4단계_전부_있다(self):
        for state in report.ManipulationState:
            assert state.value in report.MANIPULATION_LABELS


class TestFaceManipulationUnavailable:
    def test_프레임이_없고_자가표기도_없으면_판단_불가(self):
        axis = report.build_face_manipulation(_deepfake(frames_analyzed=0, method="none"), _text())
        assert axis.status == report.ManipulationState.UNAVAILABLE.value
        assert "risk" not in axis.__dict__  # 숫자 점수는 최상위에 없다

    def test_프레임이_없어도_자가표기가_있으면_판정한다(self):
        axis = report.build_face_manipulation(
            _deepfake(frames_analyzed=0, method="none"),
            _text(self_disclosure=70, evidence=['제목/설명에 자가표기 발견: "ai generated"']),
        )
        # 70 * 0.9 = 63 → level_moderate(45) 이상 level_high(70) 미만이라 판단 보류 범주.
        assert axis.status != report.ManipulationState.UNAVAILABLE.value
        assert "자가표기" in (axis.detail or "")
        assert axis.signals["combined_risk"] == 63.0

    def test_합성음성_의심만으로는_판정하지_않는다(self):
        # 음성 합성 탐지는 MVP 범위 밖이다(PRD M-03) — tts_risk가 이 축에 영향을 주면 안 된다.
        axis = report.build_face_manipulation(
            _deepfake(frames_analyzed=0, method="none"), _text(tts=80)
        )
        assert axis.status == report.ManipulationState.UNAVAILABLE.value

    def test_얼굴을_못_찾으면_분류기_점수를_믿지_않는다(self):
        axis = report.build_face_manipulation(_deepfake(avg=5.0, frames_with_face=0), _text())
        assert axis.status == report.ManipulationState.UNAVAILABLE.value
        assert "얼굴을 찾지 못해" in axis.detail

    def test_얼굴_검출_모델이_없을_때는_사유가_다르다(self):
        axis = report.build_face_manipulation(
            _deepfake(avg=5.0, frames_with_face=0, face_model_available=False), _text()
        )
        assert axis.status == report.ManipulationState.UNAVAILABLE.value
        assert "얼굴 검출 모델이 없어" in axis.detail

    def test_일부_프레임에서만_얼굴을_찾으면_경고를_남기고_판정한다(self):
        axis = report.build_face_manipulation(_deepfake(avg=40.0, frames_with_face=3), _text())
        assert axis.status != report.ManipulationState.UNAVAILABLE.value
        assert "3장에서 얼굴을 찾았다" in axis.detail
        assert "정확도가 낮을 수 있다" in axis.detail

    def test_분석_범위는_정상일_때도_남긴다(self):
        """화면이 이 축에 "분석한 구간·프레임 범위"를 함께 보여준다. 무엇을 봤는지
        모르면 사용자가 결과의 범위를 가늠할 수 없다."""
        axis = report.build_face_manipulation(_deepfake(avg=10.0), _text())
        assert axis.detail and "프레임" in axis.detail
        # 한계가 없으면 경고 문구는 붙지 않는다
        assert "정확도가 낮을 수 있다" not in axis.detail


class TestFaceManipulationScoring:
    def test_자가표기가_영상점수보다_높으면_하한선으로_작동한다(self):
        axis = report.build_face_manipulation(_deepfake(avg=10.0), _text(self_disclosure=90))
        assert axis.signals["combined_risk"] == 81.0  # 90 * 0.9

    def test_자가표기가_임계_미만이면_플로어가_발동하지_않는다(self):
        below = config.self_disclosure_gate - 1
        axis = report.build_face_manipulation(_deepfake(avg=10.0), _text(self_disclosure=below))
        assert axis.signals["combined_risk"] == 10.0

    def test_클릭베이트와_주장강도는_조작_점수에_반영되지_않는다(self):
        clean = report.build_face_manipulation(_deepfake(avg=20.0), _text())
        noisy = report.build_face_manipulation(
            _deepfake(avg=20.0), _text(clickbait=100, claim=100)
        )
        assert clean.signals["combined_risk"] == noisy.signals["combined_risk"] == 20.0

    def test_분류기를_못쓰면_사유를_남긴다(self):
        axis = report.build_face_manipulation(_deepfake(avg=5.0, method="heuristics"), _text())
        assert "분류기" in (axis.detail or "")


class TestWholeVideoGeneration:
    def test_자가표기_없으면_모델_미선정으로_판단_불가(self):
        axis = report.build_whole_video_generation(_text())
        assert axis.status == report.ManipulationState.UNAVAILABLE.value
        assert "모델이 아직 선정되지 않았다" in axis.detail

    def test_자가표기_있으면_조작_의심(self):
        axis = report.build_whole_video_generation(
            _text(self_disclosure=90, evidence=['제목/설명에 자가표기 발견: "ai generated"'])
        )
        assert axis.status == report.ManipulationState.SUSPECTED.value
        assert axis.evidence

    def test_얼굴축과_독립적이다(self):
        # 얼굴 축이 unavailable이어도 영상전체 축은 자가표기만으로 판정할 수 있다.
        text = _text(self_disclosure=90, evidence=["표기"])
        face = report.build_face_manipulation(_deepfake(frames_analyzed=0, method="none"), text)
        whole = report.build_whole_video_generation(text)
        assert whole.status == report.ManipulationState.SUSPECTED.value
        assert face.status != report.ManipulationState.UNAVAILABLE.value  # 자가표기 플로어로 판정됨


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
        stages = self._stages(
            claim_verification=report.StageStatus(
                status=report.StageState.SKIPPED.value, detail="옵션이 꺼져 있음"
            )
        )
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), stages)
        assert r.analysis_status == report.AnalysisState.COMPLETE.value

    def test_다른_단계가_건너뛰어지면_partial(self):
        stages = self._stages(
            frames=report.StageStatus(
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

    def test_얼굴축이_판단불가면_partial(self):
        r = report.build({"url": "u"}, _deepfake(frames_analyzed=0), _text(), self._stages())
        assert r.analysis_status == report.AnalysisState.PARTIAL.value

    def test_영상전체축_모델미선정은_degraded가_아니다(self):
        # 자가표기 없이 face만 정상 분석되면, whole_video_generation이 unavailable이어도
        # 그건 "아직 모델이 없다"는 정상 상태지 분석 실패가 아니다.
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        assert r.whole_video_generation.status == report.ManipulationState.UNAVAILABLE.value
        assert r.analysis_status == report.AnalysisState.COMPLETE.value

    def test_주장검증_결과를_안_넘기면_수행하지_않은_상태로_남는다(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        assert r.claim_verification.status == report.AxisStatus.UNAVAILABLE.value

    def test_주장검증_결과가_그대로_실린다(self):
        cv = report.ClaimVerification(
            status=report.AxisStatus.ANALYZED.value,
            claims=[{"text": "2024년 실업률이 3.2% 감소했다", "verdict": "refuted",
                     "reason": '전문 기관 판정: "False"', "evidence": []}],
            summary={"total": 1, "supported": 0, "refuted": 1, "unverified": 0},
        )
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages(),
                         claim_verification=cv)
        assert r.claim_verification.summary["refuted"] == 1
        # 두 축은 끝까지 분리된 채로 남는다. 주장 검증 결과가 조작 축에 섞이지 않는다.
        assert r.face_manipulation.signals["combined_risk"] == 10.0

    def test_응답_스키마_필수_키(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        payload = r.to_dict()
        assert set(payload) == {
            "url", "media", "analysis_status", "stages",
            "face_manipulation", "whole_video_generation",
            "claim_verification", "transcript",
        }
        for axis_key in ("face_manipulation", "whole_video_generation"):
            assert set(payload[axis_key]) >= {"status", "status_label", "detail", "signals"}
            # 사용자에게 숫자 점수를 최상위로 노출하지 않는다.
            assert "risk" not in payload[axis_key]

    def test_dict_왕복시_모르는_키가_있어도_죽지_않는다(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), self._stages())
        payload = r.to_dict()
        payload["앞으로_추가될_필드"] = 1
        restored = report.AnalysisReport.from_dict(payload)
        assert restored.face_manipulation.status == r.face_manipulation.status
        assert restored.whole_video_generation.status == r.whole_video_generation.status


class TestFormatting:
    def test_HTML_출력이_제목을_이스케이프한다(self):
        r = report.build(
            {"url": "u", "title": '<script>alert(1)</script>'},
            _deepfake(avg=10.0), _text(), {},
        )
        html_out = report.format_html(r)
        assert "<script>alert(1)</script>" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_판단불가는_텍스트_출력에서도_숫자로_보이지_않는다(self):
        r = report.build({"url": "u"}, _deepfake(frames_analyzed=0, method="none"), _text(), {})
        text_out = report.format_text(r)
        assert report.MANIPULATION_LABELS[report.ManipulationState.UNAVAILABLE.value] in text_out
        # 조작 축 섹션([1],[2])에는 위험도 숫자를 안 보여준다. 참고용 텍스트 신호
        # 섹션([참고])의 "/100"은 의도된 것이라 잘라내고 그 앞부분만 검사한다.
        axes_section = text_out.split("[참고]")[0]
        assert "/100" not in axes_section

    def test_두_축이_각각_출력된다(self):
        r = report.build({"url": "u"}, _deepfake(avg=10.0), _text(), {})
        text_out = report.format_text(r)
        assert "얼굴 합성·변형" in text_out
        assert "영상 전체 AI 생성" in text_out


class TestFrameAggregation:
    """프레임 점수 집계. 방식에 따라 같은 영상이 다른 등급을 받는다."""

    def _aggregate(self, mode, scores):
        from dataclasses import replace
        from deepcheck import deepfake
        original = deepfake.config
        deepfake.config = replace(config, frame_aggregation=mode)
        try:
            return deepfake.aggregate_frame_scores(scores)
        finally:
            deepfake.config = original

    def test_절사평균은_오탐_한두장에_휘둘리지_않는다(self):
        # 실측: 진짜 뉴스 영상의 얼굴 crop 4장 중 2장이 분류기 오탐이었다.
        real_news = [0.997, 0.835, 0.002, 0.002]
        assert self._aggregate("trimmed_mean", real_news) * 100 < config.level_moderate
        assert self._aggregate("blend", real_news) * 100 > config.level_moderate

    def test_여러_장이_높으면_두_방식이_모두_잡는다(self):
        ai_video = [0.999, 0.998, 0.997, 0.997, 0.996, 0.909, 0.061]
        assert self._aggregate("trimmed_mean", ai_video) * 100 > config.level_high
        assert self._aggregate("blend", ai_video) * 100 > config.level_high

    def test_프레임이_세_장_미만이면_절사하지_않는다(self):
        # 양 끝을 버리면 남는 게 없다. 평균을 그대로 쓴다.
        assert self._aggregate("trimmed_mean", [0.8, 0.2]) == 0.5
        assert self._aggregate("trimmed_mean", [0.6]) == 0.6

    def test_점수가_없으면_0이다(self):
        assert self._aggregate("trimmed_mean", []) == 0.0
        assert self._aggregate("blend", []) == 0.0

    def test_기본값은_절사평균이다(self):
        """실측 근거로 정한 값이다. 바꾸려면 M-08 점검표로 재검증해야 한다."""
        from deepcheck import deepfake
        assert config.frame_aggregation == deepfake.AGG_TRIMMED_MEAN
