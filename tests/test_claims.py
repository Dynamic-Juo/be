"""주장 추출·검증 테스트.

외부 네트워크는 호출하지 않는다. 근거 검색 제공자는 가짜로 주입한다.
검증하려는 핵심은 판정 태도다 — 근거가 없다고 거짓으로 몰지 않고, 전문 기관의
공개 판정이 있을 때만 지지·반박을 선언하는지.
"""

import time
from dataclasses import replace

from deepcheck import claims
from deepcheck.config import config


class FakeProvider:
    def __init__(self, name, results):
        self.name = name
        self._results = results
        self.queries = []

    def search(self, query, limit):
        self.queries.append(query)
        return self._results[:limit]


class BrokenProvider:
    name = "broken"

    def search(self, query, limit):
        raise RuntimeError("검색 서버 오류")


class TestExtraction:
    def test_수치가_있는_단정문을_주장으로_고른다(self):
        text = "2024년 실업률이 3.2% 감소했다고 정부가 발표했습니다."
        result = claims.extract_claims(text)
        assert len(result) == 1
        assert "실업률" in result[0].text

    def test_의견과_인사말은_제외한다(self):
        text = "안녕하세요 여러분. 저는 이게 좋다고 생각합니다. 구독 눌러주세요."
        assert claims.extract_claims(text) == []

    def test_수치도_고유명사도_없는_문장은_거른다(self):
        assert claims.extract_claims("그것은 아주 큰 문제입니다.") == []

    def test_최대_개수를_넘지_않는다(self):
        text = " ".join(
            f"20{10 + i}년에 {i + 1}만 명이 증가했다고 발표했다." for i in range(10)
        )
        assert len(claims.extract_claims(text, max_claims=3)) == 3

    def test_발언_위치를_붙인다(self):
        text = "2024년 실업률이 3.2% 감소했다고 발표했습니다."
        segments = [{"start": 12.5, "end": 18.0, "text": "2024년 실업률이 3.2% 감소했다고"}]
        result = claims.extract_claims(text, segments)
        assert result[0].start == 12.5

    def test_빈_텍스트는_주장이_없다(self):
        assert claims.extract_claims("") == []


class TestVerification:
    def _claim(self):
        return [claims.Claim(text="2024년 실업률이 3.2% 감소했다")]

    def test_근거를_못_찾아도_거짓으로_판정하지_않는다(self):
        result = claims.verify_claims(self._claim(), providers=[FakeProvider("wiki", [])])
        assert result[0].verdict == claims.UNVERIFIED
        assert result[0].status == claims.DONE
        assert result[0].insufficient_reason == claims.NO_SOURCE
        assert "거짓이라는 뜻은 아니다" in result[0].reason

    def test_관련_자료만_있으면_판단을_유보한다(self):
        provider = FakeProvider("wiki", [
            claims.Evidence(title="실업률 통계", url="https://x/1", source="wikipedia"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.UNVERIFIED
        assert result[0].evidence

    def test_전문기관_rating만으로_불일치를_확정하지_않는다(self):
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증 결과", url="https://x/2", source="factcheck",
                            snippet="2024년 실업률이 3.2% 감소했다", rating="False"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.UNVERIFIED
        assert result[0].evidence

    def test_전문기관_rating만으로_일치를_확정하지_않는다(self):
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증 결과", url="https://x/3", source="factcheck",
                            snippet="2024년 실업률이 3.2% 감소했다", rating="사실"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.UNVERIFIED

    def test_애매한_판정표기는_유보로_남긴다(self):
        # "대체로 사실"까지 지지로 옮기면 우리가 하지 않은 판단을 한 셈이 된다.
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증", url="https://x/4", source="factcheck",
                            rating="Mostly true"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.UNVERIFIED

    def test_제공자가_터져도_분석은_계속된다(self):
        result = claims.verify_claims(self._claim(), providers=[BrokenProvider()])
        assert result[0].verdict == claims.UNVERIFIED
        assert result[0].status == claims.FAILED
        assert result[0].insufficient_reason == claims.WEAK_SOURCE
        assert "확인할 수 없다" in result[0].reason

    def test_일부_제공자_실패와_빈_결과를_no_source로_단정하지_않는다(self):
        result = claims.verify_claims(
            self._claim(), providers=[FakeProvider("empty", []), BrokenProvider()]
        )
        assert result[0].status == claims.FAILED
        assert result[0].insufficient_reason == claims.WEAK_SOURCE
        assert result[0].insufficient_reason != claims.NO_SOURCE

    def test_일부_제공자_실패에도_성공한_근거는_보존한다(self):
        evidence = claims.Evidence(
            title="2024년 실업률 3.2% 통계",
            url="https://statistics.example/2024",
            source="statistics",
        )
        result = claims.verify_claims(
            self._claim(), providers=[BrokenProvider(), FakeProvider("working", [evidence])]
        )
        assert result[0].status == claims.DONE
        assert result[0].evidence == [evidence]
        assert "일부 근거 검색 제공자 조회에 실패" in result[0].reason

    def test_검색어는_문장을_그대로_넣지_않는다(self):
        provider = FakeProvider("wiki", [])
        claims.verify_claims(self._claim(), providers=[provider])
        query = provider.queries[0]
        assert "," not in query
        assert len(query.split()) <= 6

    def test_검색어에_수치와_핵심어가_남는다(self):
        provider = FakeProvider("wiki", [])
        claims.verify_claims(
            [claims.Claim(text="정부는 2024년에 실업률이 3.2% 감소했다고 발표했습니다")],
            providers=[provider],
        )
        query = provider.queries[0]
        assert "2024년" in query
        assert "실업률" in query
        # 조사가 붙은 형태로 검색하면 매칭률이 떨어진다.
        assert "실업률이" not in query.split()

    def test_검색어에서_흔한_낱말은_뺀다(self):
        provider = FakeProvider("wiki", [])
        claims.verify_claims(
            [claims.Claim(text="그런데 우리 여러분 매우 중요한 2024년 통계가 발표됐다")],
            providers=[provider],
        )
        query = provider.queries[0].split()
        assert "여러분" not in query and "매우" not in query


class TestProviderSelection:
    def _config(self, **overrides):
        return replace(config, **overrides)

    def test_기본_제공자에_gdelt는_없다(self):
        # 실측 16초 이상 + 429가 잦아 기본에서 뺐다. 주장 5건이면 80초를 버린다.
        names = [p.name for p in claims.default_providers()]
        assert "gdelt" not in names
        assert "wikipedia" in names

    def test_설정으로_제공자를_바꿀_수_있다(self):
        cfg = self._config(evidence_providers="gdelt")
        assert [p.name for p in claims.default_providers(cfg)] == ["gdelt"]

    def test_키가_없으면_팩트체크_제공자는_빠진다(self):
        cfg = self._config(evidence_providers="factcheck,wikipedia",
                           google_factcheck_api_key="")
        assert [p.name for p in claims.default_providers(cfg)] == ["wikipedia"]

    def test_키가_있으면_팩트체크를_먼저_조회한다(self):
        cfg = self._config(evidence_providers="factcheck,wikipedia",
                           google_factcheck_api_key="테스트키")
        assert [p.name for p in claims.default_providers(cfg)] == ["factcheck", "wikipedia"]

    def test_알_수_없는_이름은_무시한다(self):
        cfg = self._config(evidence_providers="없는제공자,wikipedia")
        assert [p.name for p in claims.default_providers(cfg)] == ["wikipedia"]

    def test_언어별_위키백과를_구분한다(self):
        cfg = self._config(evidence_providers="wikipedia,wikipedia_en")
        assert [p.name for p in claims.default_providers(cfg)] == ["wikipedia", "wikipedia_en"]


class TestTimeBudget:
    def test_예산을_넘기면_남은_주장은_검색하지_않는다(self):
        class SlowProvider:
            name = "slow"

            def search(self, query, limit):
                time.sleep(0.05)
                return [claims.Evidence(title="t", url="u", source="slow")]

        targets = [claims.Claim(text=f"20{10 + i}년에 {i}만 명이 증가했다") for i in range(4)]
        result = claims.verify_claims(targets, providers=[SlowProvider()], time_budget_sec=0.06)

        # 예산 안에서 처리된 주장은 검색을 했고, 넘어간 주장은 사유가 남는다.
        assert "예산" not in result[0].reason
        assert "예산" in result[-1].reason
        assert not result[-1].evidence
        assert all(c.verdict == claims.UNVERIFIED for c in result)
        assert result[-1].status == claims.TIMED_OUT
        assert result[-1].insufficient_reason == claims.TIMEOUT
        assert result[-1].insufficient_label == claims.INSUFFICIENT_LABELS[claims.TIMEOUT]
        assert result[-1].verdict_label == claims.VERDICT_LABELS[claims.UNVERIFIED]


class TestRelevanceFilter:
    """실측에서 물가 주장에 '주기율표'가 근거로 딸려왔다. 무관한 자료를 근거라고
    보여주는 것은 사실상 거짓 근거이므로 걸러낸다."""

    def _claim(self):
        return [claims.Claim(text="1998년 11월 이후 처음으로 6%대의 상승률입니다")]

    def test_주장과_무관한_검색결과는_근거로_쓰지_않는다(self):
        provider = FakeProvider("wiki", [
            claims.Evidence(title="주기율표", url="https://x/1", source="wikipedia"),
            claims.Evidence(title="동일본 대진재", url="https://x/2", source="wikipedia"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].evidence == []
        assert result[0].verdict == claims.UNVERIFIED
        assert "거짓이라는 뜻은 아니다" in result[0].reason

    def test_핵심어가_겹치면_근거로_남긴다(self):
        provider = FakeProvider("wiki", [
            claims.Evidence(title="대한민국의 소비자물가 상승률", url="https://x/3",
                            source="wikipedia", snippet="1998년 외환위기 당시 상승률은"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert len(result[0].evidence) == 1

    def test_전문기관_판정도_검토한_주장이_일치해야_참고자료로_남는다(self):
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증 결과", url="https://x/4", source="factcheck",
                            snippet="1998년 11월 이후 처음으로 6%대의 상승률입니다",
                            rating="False"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.UNVERIFIED
        assert len(result[0].evidence) == 1

    def test_다른_주장을_검토한_전문기관_rating은_제외한다(self):
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증 결과", url="https://x/5", source="factcheck",
                            snippet="2026년 성장률은 2.1%입니다", rating="False"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].evidence == []


class TestSpeculationFilter:
    def test_미래_전망은_주장으로_뽑지_않는다(self):
        # 아직 일어나지 않은 일은 어떤 근거로도 판정할 수 없다.
        text = "지금 같은 추세면 조만간 7%도 넘어설 것으로 보입니다."
        assert claims.extract_claims(text) == []

    def test_이미_일어난_일은_그대로_뽑는다(self):
        text = "6월 소비자물가 상승률이 6%를 기록했다고 통계청이 발표했다."
        assert len(claims.extract_claims(text)) == 1


class TestKoreanExtraction:
    """실측 사례: 2분짜리 물가 뉴스에서 검증 가능한 사실이 8개인데 1건만 뽑히던 문제."""

    NEWS = (
        "MBC 정오 뉴스입니다. "
        "소비자 물가 상승률이 IMF 외환위기때인 지난 1998년 이후 최고치를 기록했습니다. "
        "-(기자) 지난달 소비자 물가 상승률이 6.0%로 집계됐습니다. "
        "지난해 10월부터 3%대를 유지하던 소비자 물가 상승률은 지난 4월 4%를 넘어선 뒤 매달 뛰고 있습니다. "
        "공업 제품은 경유 50.7%, 휘발유 31.4% 등에 오르며 전년 대비 9.3% 상승했습니다. "
        "전기, 가스, 수도도 지난 4월과 5월 인상 영향으로 9.6%나 올랐습니다. "
        "MBC 뉴스 이덕영입니다."
    )

    def test_뉴스체_종결어미를_잡는다(self):
        # "집계됐습니다", "올랐습니다"처럼 -습니다로 끝나는 서술문을 놓치면
        # 한국어 뉴스에서 사실을 거의 못 잡는다.
        found = claims.extract_claims(self.NEWS, max_claims=5)
        assert len(found) >= 4
        joined = " ".join(c.text for c in found)
        assert "집계됐습니다" in joined
        assert "올랐습니다" in joined

    def test_화자_표기를_걷어낸다(self):
        found = claims.extract_claims(self.NEWS, max_claims=5)
        assert all(not c.text.startswith("-") for c in found)
        assert all("(기자)" not in c.text for c in found)

    def test_구어체_종결어미도_잡는다(self):
        # 실측(2026-09-08): 마침표가 있는 깨끗한 STT 텍스트에서 "-는데요"로 끝나는
        # 문장("...최대 50%까지 할인 받을 수 있는데요.")이 수치가 명확한데도 빠졌다.
        # 격식체(-다/-니다)만 서술문으로 인정하고 있었기 때문이다.
        text = "1인당 한도를 기존 만 원에서 2만 원으로 높였고 최대 50%까지 할인 받을 수 있는데요."
        found = claims.extract_claims(text)
        assert len(found) == 1
        assert "50%" in found[0].text

    def test_소수점이_있는_수치가_검색어에서_깨지지_않는다(self):
        # 사실 확인의 핵심이 수치인데 "6.0%"가 "6"과 "0%"로 쪼개지면 검색이 망가진다.
        query = claims._search_query("지난달 소비자 물가 상승률이 6.0%로 집계됐습니다")
        assert "6.0%" in query or "6.0" in query

    def test_한_대목에_쏠리지_않고_영상_전체에_분산된다(self):
        # 숫자가 몰린 문장이 슬롯을 다 차지하면 뒷부분 내용이 통째로 누락된다.
        found = claims.extract_claims(self.NEWS, max_claims=3)
        texts = [c.text for c in found]
        positions = [self.NEWS.index(t[:15]) for t in texts]
        assert positions == sorted(positions)  # 영상 순서대로 반환
        assert max(positions) > len(self.NEWS) * 0.4  # 뒷부분에서도 뽑혔다

    def test_같은_사실의_반복은_한_번만_센다(self):
        repeated = (
            "지난달 소비자 물가 상승률이 6.0%로 집계됐습니다. "
            "지난달 소비자 물가 상승률은 6.0%로 집계된 것으로 나타났습니다. "
            "전기, 가스, 수도도 인상 영향으로 9.6%나 올랐습니다."
        )
        found = claims.extract_claims(repeated, max_claims=5)
        assert len(found) == 2

    def test_화자_표기가_있어도_발언_위치를_찾는다(self):
        # 주장에서 "-(기자)"를 떼면 자막 원문과 글자가 안 맞아 타임스탬프가 비었다.
        # 발언 위치는 사용자가 원문을 확인하는 수단이라 비면 안 된다.
        text = "-(기자) 지난달 소비자 물가 상승률이 6.0%로 집계됐습니다."
        segments = [{"start": 31.0, "end": 35.0,
                     "text": "-(기자) 지난달 소비자 물가 상승률이  6.0%로 집계됐습니다"}]
        found = claims.extract_claims(text, segments)
        assert found[0].start == 31.0
