"""LLM 제공자.

주장 추출과 판정에 쓰는 선택적 단계다. 호출부는 `LLMUnavailable`을 작업 성격에
맞게 처리한다. 판정은 근거 부족으로 유보할 수 있지만, LLM 추출을 요청한 상태의
실패는 정상적인 "주장 없음"과 구분한다.

`vlm.py`와 같은 구조다 — 새 제공자를 추가하려면 `LLMProvider`를 구현하고
`_PROVIDERS`에 등록하면 되고, 호출부(`claims.py`)는 인터페이스만 안다.
지금은 DeepSeek(OpenAI 호환)과 Ollama 두 가지를 지원한다.

배포처가 바뀌어도 갈아끼울 수 있게 해두는 게 목적이다. 해커톤은 외부 API로
가되, 운영에서 호출 비용이 문제가 되면 호스트 Ollama로 옮길 수 있다.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .config import config

logger = logging.getLogger(__name__)
_TLS_CONTEXT = ssl.create_default_context()
_MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_HTTP_ERROR_BYTES = 4096


class LLMUnavailable(Exception):
    """LLM 호출 또는 응답을 사용할 수 없다. 호출부가 단계별 실패 정책을 적용한다."""


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        """프롬프트 한 쌍을 보내고 응답 문자열을 받는다. 실패하면 LLMUnavailable."""


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        kwargs = {"timeout": timeout}
        if urllib.parse.urlsplit(url).scheme.lower() == "https":
            # urllib의 암묵적 기본값에 기대지 않고 CA 검증·호스트명 검사를 고정한다.
            kwargs["context"] = _TLS_CONTEXT
        with urllib.request.urlopen(req, **kwargs) as resp:
            response_body = resp.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            if len(response_body) > _MAX_HTTP_RESPONSE_BYTES:
                raise LLMUnavailable("LLM 응답이 허용 크기를 초과했다")
            parsed = json.loads(response_body.decode())
            if not isinstance(parsed, dict):
                raise LLMUnavailable("LLM HTTP 응답이 JSON 객체가 아니다")
            return parsed
    except urllib.error.HTTPError as e:
        # 제공자 본문에는 요청 일부나 자격 정보가 되비칠 수 있다. 제한해서 읽고 버리며,
        # 사용자 결과와 로그로 전파되는 예외에는 상태 코드만 남긴다.
        e.read(_MAX_HTTP_ERROR_BYTES)
        raise LLMUnavailable(f"HTTP {e.code}") from e
    except LLMUnavailable:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LLMUnavailable("LLM HTTP 응답 JSON 형식이 올바르지 않다") from e
    except Exception as e:
        raise LLMUnavailable(f"LLM HTTP 요청 실패: {type(e).__name__}") from e


class OpenAICompatibleProvider:
    """OpenAI `/chat/completions` 규격을 따르는 서비스. DeepSeek이 여기 해당한다."""

    name = "openai_compatible"

    def __init__(self, model: str, base_url: str, api_key: str, timeout: int,
                 temperature: float):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        if not self.api_key:
            raise LLMUnavailable("API 키가 설정되지 않았다(DEEPCHECK_LLM_API_KEY)")
        data = _post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                # 지원하는 모델이면 JSON만 내도록 강제한다. 모르는 필드는 대체로
                # 무시되므로, 지원하지 않는 서비스에서도 요청 자체는 통과한다.
                "response_format": {"type": "json_object"},
            },
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            self.timeout,
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMUnavailable("예상과 다른 응답 구조") from e


class OllamaChatProvider:
    """로컬/호스트 Ollama 서버(`/api/chat`)."""

    name = "ollama"

    def __init__(self, model: str, base_url: str, timeout: int, temperature: float):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        data = _post_json(
            f"{self.base_url}/api/chat",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": self.temperature, "num_predict": max_tokens},
            },
            {"Content-Type": "application/json"},
            self.timeout,
        )
        try:
            return data["message"]["content"]
        except KeyError as e:
            raise LLMUnavailable("예상과 다른 응답 구조") from e


_PROVIDERS = {
    "deepseek": lambda cfg: OpenAICompatibleProvider(
        cfg.llm_model, cfg.llm_base_url, cfg.llm_api_key,
        cfg.llm_timeout_sec, cfg.llm_temperature),
    "openai_compatible": lambda cfg: OpenAICompatibleProvider(
        cfg.llm_model, cfg.llm_base_url, cfg.llm_api_key,
        cfg.llm_timeout_sec, cfg.llm_temperature),
    "ollama": lambda cfg: OllamaChatProvider(
        cfg.llm_model, cfg.ollama_url, cfg.llm_timeout_sec, cfg.llm_temperature),
}


def get_provider(cfg=None) -> LLMProvider | None:
    """설정된 제공자를 만든다. 꺼져 있거나 이름을 모르면 None."""
    cfg = cfg or config
    name = (cfg.llm_provider or "off").strip().lower()
    if name in ("", "off", "none"):
        return None
    factory = _PROVIDERS.get(name)
    if not factory:
        logger.warning("알 수 없는 LLM 제공자: %s (사용 안 함)", name)
        return None
    return factory(cfg)


# 모델이 코드펜스로 감싸서 주는 경우가 흔하다. 다만 앞뒤에 다른 문장이 붙은 응답은
# 그 안의 JSON만 골라 신뢰하지 않는다. 전체 응답이 JSON 또는 JSON 코드펜스여야 한다.
_FENCE_RE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.S)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"표준 JSON이 아닌 숫자 상수: {value}")


def complete_json(provider: LLMProvider, system: str, user: str,
                  max_tokens: int = 1024) -> dict:
    """JSON 응답을 기대하고 호출한다. 파싱까지 실패하면 LLMUnavailable.

    파싱 실패는 정상적인 빈 구조화 결과와 다르므로 예외로 올린다.
    """
    raw = provider.complete(system, user, max_tokens)
    text = (raw or "").strip()
    fenced = _FENCE_RE.fullmatch(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (json.JSONDecodeError, ValueError) as e:
        # 응답이 입력 발언·자료를 되비칠 수 있으므로 원문이나 키 이름을 로그에 남기지 않는다.
        logger.warning("LLM 응답 JSON 파싱 실패 (%s)", type(e).__name__)
        raise LLMUnavailable("JSON 응답 형식이 올바르지 않다") from e
    if not isinstance(parsed, dict):
        raise LLMUnavailable(f"객체가 아닌 JSON: {type(parsed).__name__}")
    return parsed
