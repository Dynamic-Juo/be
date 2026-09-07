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

# 실제 YouTube 자동 자막(ko-orig)에서 그대로 가져온 발췌. "롤링" 방식이라 한 줄이
# 그대로 다음 큐의 첫 줄로 반복된 뒤 새 낱말이 이어 붙는다. 2026-09-07 실측:
# 이 패턴을 못 걸러내서 최종 텍스트에 같은 구절이 3번씩 겹쳐 나온 적이 있다.
ROLLING_VTT = """WEBVTT
Kind: captions
Language: ko

00:00:00.500 --> 00:00:02.410 align:start position:0%

민족<00:00:00.859><c> 최대</c><00:00:01.160><c> 명절</c><00:00:01.579><c> 추석이</c><00:00:01.969><c> 한달</c><00:00:02.000><c> 앞으로</c>

00:00:02.410 --> 00:00:02.420 align:start position:0%
민족 최대 명절 추석이 한달 앞으로


00:00:02.420 --> 00:00:05.679 align:start position:0%
민족 최대 명절 추석이 한달 앞으로
다가왔지만<00:00:03.220><c> 23년만에</c><00:00:04.220><c> 고물가</c><00:00:04.850><c> 예</c><00:00:05.419><c> 벌써</c>

00:00:05.679 --> 00:00:05.689 align:start position:0%
다가왔지만 23년만에 고물가 예 벌써

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

    def test_롤링_자동자막의_반복_구간을_걸러낸다(self, tmp_path):
        path = tmp_path / "rolling.vtt"
        path.write_text(ROLLING_VTT, encoding="utf-8")
        track = captions.load_track(str(path), "ko-orig", "automatic")
        assert track is not None
        # 실측 버그: "민족 최대 명절 추석이 한달 앞으로"가 3번 겹쳐 나왔다.
        assert track.text.count("민족") == 1
        assert track.text.count("앞으로") == 1
        assert track.text == "민족 최대 명절 추석이 한달 앞으로 다가왔지만 23년만에 고물가 예 벌써"


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
