"""VLM(비전 언어 모델) 제공자.

"이 프레임이 왜 AI로 보이는지"를 자연어 근거로 받아오는 선택적 단계다.
지금 구현은 Ollama뿐이지만, 호출부(`deepfake.py`)는 이 인터페이스만 알기 때문에
다른 제공자로 갈아끼우거나 아예 끄는 데 호출부 수정이 필요 없다.

새 제공자를 추가하려면 `VLMProvider`를 구현하고 `_PROVIDERS`에 등록하면 된다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Protocol

from .config import config

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = (
    "이미지에서 AI가 생성했거나 딥페이크일 수 있는 시각적 흔적(얼굴 비대칭, 손/손가락 왜곡, "
    "사람 피부 불연속, 비정상 그림자, 공간 반복 등)이 있으면 한국어로 구체적으로 설명하세요. "
    "없으면 '특이사항 없음'이라고만 답하세요. 2문장 이내로."
)


class VLMProvider(Protocol):
    name: str

    def describe(self, image_path: str) -> str:
        """이미지 한 장을 설명한다. 실패하면 VLMUnavailable을 올린다."""


class VLMUnavailable(Exception):
    """VLM을 쓸 수 없다. 치명적이지 않으므로 호출부는 근거 없이 계속 진행한다."""


class OllamaProvider:
    """로컬 Ollama 서버(`/api/generate`)를 사용한다."""

    name = "ollama"

    def __init__(self, model: str, base_url: str | None = None, timeout: int | None = None):
        self.model = model
        self.base_url = (base_url or config.ollama_url).rstrip("/")
        self.timeout = timeout or config.vlm_timeout_sec

    def describe(self, image_path: str) -> str:
        try:
            with open(image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
        except OSError as e:
            raise VLMUnavailable(f"이미지를 읽지 못했습니다({os.path.basename(image_path)}): {e}")

        payload = {
            "model": self.model,
            "prompt": ANALYSIS_PROMPT,
            "images": [encoded],
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise VLMUnavailable(f"Ollama에 연결하지 못했습니다({self.base_url}): {e.reason}")
        except Exception as e:
            raise VLMUnavailable(f"Ollama 호출 실패({self.model}): {e}")

        note = (data.get("response") or "").strip()
        if not note:
            raise VLMUnavailable(f"Ollama({self.model})가 빈 응답을 반환했습니다")
        return note


_PROVIDERS = {
    OllamaProvider.name: OllamaProvider,
}


def get_provider(model: str | None, provider_name: str | None = None) -> VLMProvider | None:
    """모델을 지정했을 때만 제공자를 만든다. 지정하지 않으면 VLM 단계를 건너뛴다."""
    if not model:
        return None
    name = (provider_name or config.vlm_provider).lower()
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        logger.warning("알 수 없는 VLM 제공자 '%s' — VLM 단계를 건너뛴다 (사용 가능: %s)",
                       name, ", ".join(_PROVIDERS))
        return None
    return provider_cls(model=model)
