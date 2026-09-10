"""LLM 제공자.

주장 추출과 판정에 쓰는 선택적 단계다. 없으면 규칙 기반으로 떨어지므로,
호출부는 `LLMUnavailable`을 잡아서 폴백만 하면 된다.

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
import urllib.error
import urllib.request
from typing import Protocol

from .config import config

logger = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """LLM을 쓸 수 없다. 치명적이지 않으므로 호출부는 규칙 기반으로 폴백한다."""


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        """프롬프트 한 쌍을 보내고 응답 문자열을 받는다. 실패하면 LLMUnavailable."""


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 본문에 원인이 들어 있는 경우가 많아서(키 오류, 잔액 부족 등) 같이 남긴다.
        detail = e.read().decode(errors="replace")[:300]
        raise LLMUnavailable(f"HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise LLMUnavailable(str(e)) from e


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
            raise LLMUnavailable(f"예상과 다른 응답 구조: {str(data)[:200]}") from e


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
            raise LLMUnavailable(f"예상과 다른 응답 구조: {str(data)[:200]}") from e


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


# 모델이 코드펜스로 감싸서 주는 경우가 흔하다. JSON 본문만 꺼낸다.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def complete_json(provider: LLMProvider, system: str, user: str,
                  max_tokens: int = 1024) -> dict:
    """JSON 응답을 기대하고 호출한다. 파싱까지 실패하면 LLMUnavailable.

    파싱 실패를 예외로 올리는 이유는, 호출부가 "LLM을 못 썼다"와 "LLM이 판정을
    거부했다"를 구분할 필요가 없기 때문이다. 둘 다 폴백 대상이다.
    """
    raw = provider.complete(system, user, max_tokens)
    text = (raw or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("LLM 응답 JSON 파싱 실패: %s", text[:200])
        raise LLMUnavailable(f"JSON 파싱 실패: {e}") from e
    if not isinstance(parsed, dict):
        raise LLMUnavailable(f"객체가 아닌 JSON: {type(parsed).__name__}")
    return parsed
