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
        assert "거짓이라는 뜻은 아니다" in result[0].reason

    def test_관련_자료만_있으면_판단을_유보한다(self):
        provider = FakeProvider("wiki", [
            claims.Evidence(title="실업률 통계", url="https://x/1", source="wikipedia"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.UNVERIFIED
        assert result[0].evidence

    def test_전문기관이_거짓으로_판정하면_반박이다(self):
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증 결과", url="https://x/2", source="factcheck",
                            rating="False"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.REFUTED
        assert "전문 기관 판정" in result[0].reason

    def test_전문기관이_사실로_판정하면_지지다(self):
        provider = FakeProvider("factcheck", [
            claims.Evidence(title="검증 결과", url="https://x/3", source="factcheck",
                            rating="사실"),
        ])
        result = claims.verify_claims(self._claim(), providers=[provider])
        assert result[0].verdict == claims.SUPPORTED

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

        # 예산 안에서 처리된 주장에는 근거가 붙고, 넘어간 주장은 사유가 남는다.
        assert result[0].evidence
        assert not result[-1].evidence
        assert "예산" in result[-1].reason
        assert all(c.verdict == claims.UNVERIFIED for c in result)
