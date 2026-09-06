"""DeepCheck — AI fake video detector (다운로드 → STT → 딥페이크 탐지 → 위험도)."""

from . import downloader, transcriber, deepfake, analyzer, report

__all__ = ["downloader", "transcriber", "deepfake", "analyzer", "report"]
__version__ = "0.1.0"
