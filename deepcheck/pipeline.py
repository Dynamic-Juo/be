"""분석 파이프라인 오케스트레이션.

URL 하나를 받아 다운로드 → 프레임 분석 → STT → 텍스트 분석 → 리포트까지 실행한다.
CLI와 백엔드가 이 모듈을 함께 쓴다(예전에는 `cli.py`에 있어서 API 서버가 argparse를
같이 끌고 왔다).

각 단계는 성공/실패/건너뜀을 `stages`에 남긴다. 중간 단계가 실패해도 가능한 만큼은
결과를 돌려주되, 무엇을 못 했는지 응답에 드러낸다.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from . import analyzer, deepfake, downloader, report, transcriber
from .config import config, default_vlm_model
from .errors import DeepCheckError, UnsupportedURLError, as_error_dict
from .report import StageState, StageStatus

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]


@dataclass
class AnalysisOptions:
    """분석 옵션 한 벌.

    CLI·API·harness가 모두 이 기본값을 공유한다. 예전에는 세 곳이 각자 기본값을
    들고 있어서 `use_classifier`가 CLI에서는 False, API에서는 True로 갈렸고,
    같은 함수가 진입 경로에 따라 다른 알고리즘을 돌렸다.
    """

    model_size: str = field(default_factory=lambda: config.whisper_model_size)
    max_frames: int = field(default_factory=lambda: config.max_frames)
    use_classifier: bool = True
    vlm_model: str | None = field(default_factory=default_vlm_model)
    # 주장 사실성 검증은 아직 미구현이다. 팀 결정(docs PR #4) 전까지 스위치만 둔다.
    enable_claim_verification: bool = False
    workdir: str | None = None
    keep_workdir: bool = False
    save_transcript: str | None = None

    @classmethod
    def from_dict(cls, params: dict) -> "AnalysisOptions":
        """알 수 없는 키는 무시하고 옵션을 만든다(API 요청 본문 → 옵션)."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in params.items() if k in known and v is not None})


class StageTracker:
    """단계별 상태와 소요 시간을 모은다."""

    def __init__(self) -> None:
        self.stages: dict[str, StageStatus] = {}

    @contextmanager
    def run(self, name: str):
        started = time.monotonic()
        try:
            yield
        except Exception as e:
            error = as_error_dict(e, stage=name)
            self.stages[name] = StageStatus(
                status=StageState.FAILED.value,
                detail=error["message"],
                elapsed_sec=round(time.monotonic() - started, 2),
                error=error,
            )
            logger.error("단계 실패: %s (%s)", name, error["code"], exc_info=True)
            raise
        else:
            self.stages[name] = StageStatus(
                status=StageState.OK.value,
                elapsed_sec=round(time.monotonic() - started, 2),
            )

    def mark(self, name: str, status: StageState, detail: str | None = None,
             elapsed_sec: float | None = None, error: dict | None = None) -> None:
        self.stages[name] = StageStatus(status=status.value, detail=detail,
                                        elapsed_sec=elapsed_sec, error=error)

    def as_dict(self) -> dict:
        return {name: stage for name, stage in self.stages.items()}


def analyze_url(url: str, options: AnalysisOptions | None = None,
                progress_cb: ProgressCallback | None = None) -> dict:
    """전체 파이프라인을 실행하고 결과 dict를 반환한다."""
    opts = options or AnalysisOptions()
    tracker = StageTracker()

    def progress(pct: float, msg: str) -> None:
        logger.info("[%3d%%] %s", int(pct * 100), msg)
        if progress_cb:
            progress_cb(pct, msg)

    if not _is_supported_url(url):
        raise UnsupportedURLError(f"http(s) URL이 아닙니다: {url}", stage="input")

    tmp = opts.workdir or tempfile.mkdtemp(prefix="deepcheck_")
    os.makedirs(tmp, exist_ok=True)
    logger.info("분석 시작: %s (작업 디렉토리 %s)", url, tmp)

    try:
        progress(0.05, "영상 다운로드 중...")
        with tracker.run("download"):
            media = downloader.download(url, tmp)

        progress(0.30, "프레임 추출 중...")
        frames = _extract_frames(media, tmp, opts, tracker)

        progress(0.45, "영상 조작 분석 중...")
        det_report = _detect_manipulation(frames, opts, tracker)

        progress(0.60, "음성→텍스트(STT) 변환 중...")
        transcript_text, language, coverage_pct = _transcribe(media, opts, tracker)

        progress(0.85, "텍스트 신호 분석 중...")
        txt_report = analyzer.analyze(
            transcript_text, language, title=media.title, description=media.description
        )

        claim_result = _verify_claims(opts, tracker)

        progress(0.95, "결과 정리 중...")
        meta = {
            "url": url,
            "title": media.title,
            "uploader": media.uploader,
            "duration": media.duration,
            "video_id": media.video_id,
            "language": language,
            "stt_coverage_pct": coverage_pct,
        }
        final = report.build(
            meta, det_report.__dict__, txt_report.__dict__,
            stages=tracker.as_dict(), claim_verification=claim_result,
        )
        progress(1.0, "완료")
        logger.info(
            "분석 완료: 상태=%s, 미디어조작=%s(%s)",
            final.analysis_status, final.media_manipulation.status,
            final.media_manipulation.level,
        )
        return final.to_dict()
    except DeepCheckError:
        raise
    except Exception as e:
        # 예상 못 한 실패도 호출자가 같은 모양으로 다룰 수 있게 감싼다.
        raise DeepCheckError(f"분석 중 예상하지 못한 오류: {e}", cause=e) from e
    finally:
        if not opts.keep_workdir and not opts.workdir:
            shutil.rmtree(tmp, ignore_errors=True)


def _is_supported_url(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _extract_frames(media: downloader.VideoMedia, tmp: str, opts: AnalysisOptions,
                    tracker: StageTracker) -> list[str]:
    frames_dir = os.path.join(tmp, "frames")
    started = time.monotonic()
    try:
        frames = downloader.extract_frames(media.video_path, frames_dir,
                                           max_frames=opts.max_frames)
    except Exception as e:
        # 프레임을 못 뽑아도 텍스트 분석은 계속할 수 있으므로 여기서 예외를 흡수한다.
        # 대신 무엇이 왜 실패했는지는 stages에 구조화해서 남긴다.
        error = as_error_dict(e, stage="frames")
        tracker.mark("frames", StageState.FAILED, error["message"],
                     round(time.monotonic() - started, 2), error=error)
        logger.error("프레임 추출 실패 (%s)", error["code"], exc_info=True)
        return []

    elapsed = round(time.monotonic() - started, 2)
    if frames:
        tracker.mark("frames", StageState.OK, f"{len(frames)}장 추출", elapsed)
    else:
        tracker.mark("frames", StageState.FAILED, "프레임을 한 장도 추출하지 못함", elapsed)
    return frames


def _detect_manipulation(frames: list[str], opts: AnalysisOptions,
                         tracker: StageTracker) -> deepfake.DeepfakeReport:
    started = time.monotonic()
    if not frames:
        tracker.mark("media_manipulation", StageState.SKIPPED, "분석할 프레임이 없음")
        return deepfake.DeepfakeReport(frames_analyzed=0, frames_fake=0, avg_fake_score=0.0,
                                       method="none")
    detector = deepfake.DeepfakeDetector(
        use_classifier=opts.use_classifier,
        vlm_model=opts.vlm_model,
    )
    det_report = detector.analyze(frames)
    detail = None
    if opts.use_classifier and "ViT-classifier" not in det_report.method:
        detail = "분류기를 사용하지 못해 휴리스틱만으로 판단함"
    tracker.mark("media_manipulation", StageState.OK, detail,
                 round(time.monotonic() - started, 2))
    return det_report


def _transcribe(media: downloader.VideoMedia, opts: AnalysisOptions,
                tracker: StageTracker) -> tuple[str, str | None, float | None]:
    """(텍스트, 언어, STT 커버리지 %)를 반환한다. 실패해도 예외를 올리지 않는다."""
    started = time.monotonic()
    source = media.audio_path or media.video_path
    try:
        result = transcriber.transcribe(source, model_size=opts.model_size)
    except Exception as e:
        # STT가 실패해도 영상 분석 결과는 돌려줄 수 있다. 다만 "무음 영상"과
        # 구분되도록 실패 사실을 stages에 남긴다.
        error = as_error_dict(e, stage="transcript")
        tracker.mark("transcript", StageState.FAILED, error["message"],
                     round(time.monotonic() - started, 2), error=error)
        logger.error("STT 실패 (%s)", error["code"], exc_info=True)
        return "", None, None

    coverage_pct = None
    if media.duration and result.duration:
        coverage_pct = round(min(result.duration, media.duration) / media.duration * 100, 1)
        logger.info(
            "STT 커버리지: %.1fs / 영상 %.1fs (%.1f%%)",
            result.duration, media.duration, coverage_pct,
        )
        if coverage_pct < 90:
            logger.warning("STT가 영상 끝까지 처리하지 못했을 수 있다 (커버리지 %.1f%%)", coverage_pct)

    if opts.save_transcript:
        _save_transcript(opts.save_transcript, media, result)

    tracker.mark(
        "transcript", StageState.OK,
        f"{result.word_count}단어" + (f", 커버리지 {coverage_pct}%" if coverage_pct else ""),
        round(time.monotonic() - started, 2),
    )
    return result.text, result.language, coverage_pct


def _save_transcript(path: str, media: downloader.VideoMedia,
                     result: transcriber.Transcript) -> None:
    """STT 원문과 타임스탬프 세그먼트를 파일로 남긴다(STT 품질 검증용)."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"[영상 제목] {media.title}\n")
            f.write(f"[영상 길이] {media.duration}s / [STT 처리 길이] {result.duration}s\n\n")
            f.write("=== 전체 transcript (원문 그대로) ===\n\n")
            f.write(result.text + "\n\n")
            f.write("=== 세그먼트 (타임스탬프) ===\n\n")
            for seg in result.segments:
                f.write(f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}\n")
        logger.info("전체 transcript 저장: %s", path)
    except OSError as e:
        logger.warning("transcript 저장 실패(%s): %s", path, e)


def _verify_claims(opts: AnalysisOptions, tracker: StageTracker) -> report.ClaimVerification:
    """주장 사실성 검증 축.

    아직 구현하지 않았다. PRD의 R-03~R-05가 미정 상태이고(주장 선정 기준, 근거
    검색 수단, 판정 기준), 이는 팀 결정 사항이라 임의로 구현하지 않는다.
    스위치와 응답 자리만 먼저 만들어 둬서, 결정이 나면 이 함수만 채우면 된다.
    """
    if not opts.enable_claim_verification:
        tracker.mark("claim_verification", StageState.SKIPPED, "옵션이 꺼져 있음")
        return report.ClaimVerification(
            status=report.AxisStatus.NOT_IMPLEMENTED.value,
            detail="주장 사실성 검증은 아직 구현되지 않았다(팀 결정 대기).",
        )

    tracker.mark("claim_verification", StageState.NOT_IMPLEMENTED,
                 "요청했지만 파이프라인이 아직 없음")
    logger.warning("주장 사실성 검증을 요청했으나 아직 구현되지 않았다")
    return report.ClaimVerification(
        status=report.AxisStatus.NOT_IMPLEMENTED.value,
        detail="검증을 요청했지만 파이프라인이 아직 구현되지 않았다. 주장 선정·근거 검색·판정 "
               "기준이 PRD에서 미정 상태다.",
    )
