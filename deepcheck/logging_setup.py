"""로깅 설정.

백엔드는 워커 여러 개가 동시에 분석을 돌리므로, 로그 줄만 봐서는 어느 요청의
로그인지 알 수 없으면 디버깅이 불가능하다. job_id를 contextvar로 실어 모든
로그 레코드에 자동으로 붙인다.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from .config import config

current_job_id: ContextVar[str] = ContextVar("current_job_id", default="-")

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [job:%(job_id)s] %(name)s | %(message)s"
_configured = False


class _JobIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = current_job_id.get()
        return True


def setup_logging(level: str | None = None) -> None:
    """프로세스당 한 번만 핸들러를 붙인다. CLI와 백엔드 양쪽 진입점에서 호출한다."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_JobIdFilter())

    root = logging.getLogger("deepcheck")
    root.setLevel(level or config.log_level)
    root.addHandler(handler)
    root.propagate = False

    backend_root = logging.getLogger("backend")
    backend_root.setLevel(level or config.log_level)
    backend_root.addHandler(handler)
    backend_root.propagate = False

    _configured = True
