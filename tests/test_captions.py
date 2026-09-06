"""자막 우선 사용 테스트.

설계 문서의 분기("자막이 있으면 자막, 없으면 STT")가 실제로 지켜지는지,
그리고 자동 자막을 기본으로 끌어다 쓰지는 않는지 확인한다.
"""

from deepcheck import captions

VTT = """WEBVTT
Kind: captions
Language: ko

1
00:00:01.000 --> 00:00:04.000
<c>2024년</c> 실업률이 3.2% 감소했다고

2
00:00:04.000 --> 00:00:07.500
정부가 발표했습니다
"""


class TestParsing:
    def test_VTT를_세그먼트로_읽는다(self, tmp_path):
        path = tmp_path / "a.vtt"
        path.write_text(VTT, encoding="utf-8")
        segments = captions.parse_vtt(str(path))
        assert len(segments) == 2
        assert segments[0]["start"] == 1.0
        assert segments[1]["end"] == 7.5
        # 인라인 태그는 걷어낸다.
        assert "<c>" not in segments[0]["text"]
        assert "2024년" in segments[0]["text"]

    def test_트랙으로_만들면_전체_텍스트가_이어진다(self, tmp_path):
        path = tmp_path / "a.vtt"
        path.write_text(VTT, encoding="utf-8")
        track = captions.load_track(str(path), "ko", "manual")
        assert track is not None
        assert "실업률" in track.text and "발표했습니다" in track.text
        assert track.language == "ko"
        assert track.duration == 7.5

    def test_내용이_없는_자막은_None(self, tmp_path):
        path = tmp_path / "empty.vtt"
        path.write_text("WEBVTT\n\n", encoding="utf-8")
        assert captions.load_track(str(path), "ko", "manual") is None

    def test_없는_파일은_빈_목록(self):
        assert captions.parse_vtt("/없는/경로.vtt") == []


class TestTrackSelection:
    def test_수동_자막을_고른다(self):
        picked = captions.pick_track({"ko": [{"ext": "vtt"}]}, {}, "manual")
        assert picked == ("ko", "manual", [{"ext": "vtt"}])

    def test_기본값은_자동_자막을_쓰지_않는다(self):
        assert captions.pick_track({}, {"ko": [{"ext": "vtt"}]}, "manual") is None

    def test_any면_자동_자막까지_쓴다(self):
        picked = captions.pick_track({}, {"ko": [{"ext": "vtt"}]}, "any")
        assert picked[1] == "automatic"

    def test_off면_자막을_쓰지_않는다(self):
        assert captions.pick_track({"ko": [{"ext": "vtt"}]}, {}, "off") is None

    def test_한국어를_영어보다_먼저_고른다(self):
        picked = captions.pick_track({"en": [{"ext": "vtt"}], "ko": [{"ext": "vtt"}]}, {}, "manual")
        assert picked[0] == "ko"

    def test_지역_변형_코드도_인식한다(self):
        picked = captions.pick_track({"ko-KR": [{"ext": "vtt"}]}, {}, "manual")
        assert picked[0] == "ko-KR"
