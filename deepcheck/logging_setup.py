"""로깅 설정.

두 가지를 해결한다.

1. 동시 실행 구분: 워커 여러 개가 동시에 분석하므로 로그 줄만 봐서는 어느 요청의
   로그인지 알 수 없으면 디버깅이 불가능하다. request_id와 job_id를 contextvar로
   실어 모든 레코드에 자동으로 붙인다.
2. 나중에 수집 가능한 형태: `DEEPCHECK_LOG_FORMAT=json`이면 한 줄짜리 JSON으로
   출력해서, 배포 후 로그를 파일·수집기에서 파싱할 수 있게 한다. 기본은 사람이
   읽기 좋은 text다.

로그는 표준 출력(stderr)으로만 내보낸다. 컨테이너에서는 이게 표준 인터페이스이고,
파일 회전·보관은 도커 로그 드라이버(docker-compose.yml)가 맡는다.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

from .config import config

current_job_id: ContextVar[str] = ContextVar("current_job_id", default="-")
current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")

_TEXT_FORMAT = "%(asctime)s %(levelname)-7s [req:%(request_id)s job:%(job_id)s] %(name)s | %(message)s"
_LOGGER_ROOTS = ("deepcheck", "backend", "uvicorn.error", "uvicorn.access")

# 표준 LogRecord가 항상 갖는 필드. JSON 출력 시 추가 컨텍스트만 골라내는 데 쓴다.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName", "request_id", "job_id",
}

_configured = False


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        record.job_id = current_job_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """한 줄 JSON 출력. 수집기가 파싱할 수 있게 필드를 평평하게 유지한다."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "job_id": getattr(record, "job_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # logger.info("...", extra={"stage": "download"}) 같은 추가 컨텍스트를 살린다.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, log_format: str | None = None) -> None:
    """프로세스당 한 번만 핸들러를 붙인다. CLI와 백엔드 진입점에서 호출한다."""
    global _configured
    if _configured:
        return

    fmt = (log_format or config.log_format).lower()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT))
    handler.addFilter(_ContextFilter())

    for name in _LOGGER_ROOTS:
        logger = logging.getLogger(name)
        logger.setLevel(level or config.log_level)
        # uvicorn은 자기 핸들러를 미리 붙여두므로 교체해야 포맷이 섞이지 않는다.
        logger.handlers = [handler]
        logger.propagate = False

    _configured = True
