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
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import analyzer, captions, claims, deepfake, downloader, llm, report, transcriber
from .config import config, default_vlm_model
from .logging_setup import carry_context
from .errors import (DeepCheckError, UnsupportedURLError, UnsupportedVideoError,
                     as_error_dict)
from .report import StageState, StageStatus

logger = logging.getLogger(__name__)

# stage는 analysis-runtime.md의 job 상태값(processing:collecting 등)과 맞춘다.
ProgressCallback = Callable[[float, str, str | None], None]
# 완성된 하위 블록의 전체 스냅샷을 통째로 넘긴다(부분 필드 병합이 아니라 키
# 단위 덮어쓰기라 항상 일관된 상태를 유지한다).
PartialCallback = Callable[[dict], None]


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
    enable_claim_verification: bool = True
    # manual: 사람이 단 자막만 사용 / any: 자동 자막까지 / off: 항상 STT
    caption_policy: str = field(default_factory=lambda: config.caption_policy)
    # M-02의 지원 조건(길이 등)을 강제할지. CLI로 긴 영상을 실험할 때는 끌 수 있다.
    enforce_input_limits: bool = True
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
                progress_cb: ProgressCallback | None = None,
                on_partial: PartialCallback | None = None) -> dict:
    """전체 파이프라인을 실행하고 결과 dict를 반환한다.

    `on_partial`을 넘기면 완성되는 대로 하위 블록(미디어 조작 두 축, 주장 카드)을
    조각조각 통지한다 — result-ui.md가 요구하는 "폴링마다 결과가 조금씩 채워지는"
    동작을 위한 것이다. CLI처럼 최종 결과만 필요하면 생략하면 된다(기본 None).
    """
    opts = options or AnalysisOptions()
    tracker = StageTracker()
    started = time.monotonic()

    def progress(pct: float, msg: str, stage: str | None = None) -> None:
        logger.info("[%3d%%] %s", int(pct * 100), msg)
        if progress_cb:
            progress_cb(pct, msg, stage)

    def partial(patch: dict) -> None:
        if on_partial:
            on_partial(patch)

    if not _is_supported_url(url):
        raise UnsupportedURLError(f"http(s) URL이 아닙니다: {url}", stage="input")

    tmp = opts.workdir or tempfile.mkdtemp(prefix="deepcheck_")
    os.makedirs(tmp, exist_ok=True)
    logger.info("분석 시작: %s (작업 디렉토리 %s)", url, tmp)

    try:
        progress(0.05, "영상 다운로드 중...", "collecting")
        with tracker.run("download"):
            media = downloader.download(url, tmp, caption_policy=opts.caption_policy)
        _check_supported_video(media, opts)

        progress(0.25, "프레임 추출 중...", "collecting")
        frames = _extract_frames(media, tmp, opts, tracker)

        progress(0.35, "영상 조작 분석 중...", "collecting")
        det_report = _detect_manipulation(frames, opts, tracker)

        # 자가표기(self-disclosure)는 제목·설명만 보므로 발언 텍스트(STT)를 기다릴
        # 필요가 없다 — 다운로드 직후부터 이미 알 수 있다. 미디어 조작 두 축은
        # 주장 검증을 기다리지 않고 이 시점에 바로 통지한다.
        sd_risk, sd_evidence = analyzer.detect_self_disclosure(media.title, media.description)
        self_disclosure = {"self_disclosure_risk": sd_risk, "self_disclosure_evidence": sd_evidence}
        face_axis = report.build_face_manipulation(det_report.__dict__, self_disclosure)
        whole_video_axis = report.build_whole_video_generation(self_disclosure)
        partial({
            "face_manipulation": _asdict_axis(face_axis),
            "whole_video_generation": _asdict_axis(whole_video_axis),
        })

        progress(0.45, "발언 텍스트 확보 중...", "transcribing")
        transcript = _collect_transcript(media, opts, tracker)
        transcript_text = transcript.text
        segments = transcript.segments

        progress(0.65, "텍스트 신호 분석 중...", "transcribing")
        txt_report = analyzer.analyze(
            transcript_text, transcript.language,
            title=media.title, description=media.description
        )

        progress(0.70, "주장 추출 중...", "extracting_claims")
        claim_result = _verify_claims(
            transcript_text, segments, opts, tracker, deadline=started + config.max_processing_sec,
            progress=progress, partial=partial,
        )

        progress(0.95, "결과 정리 중...", "verifying")
        meta = {
            "url": url,
            "title": media.title,
            "uploader": media.uploader,
            "duration": media.duration,
            "video_id": media.video_id,
            "thumbnail": media.thumbnail,
            "upload_date": media.upload_date,
            "language": transcript.language,
            "stt_coverage_pct": transcript.coverage_pct,
            "transcript_source": transcript.source,
        }
        final = report.build(
            meta, det_report.__dict__, txt_report.__dict__,
            stages=tracker.as_dict(), claim_verification=claim_result,
        )
        progress(1.0, "완료")
        logger.info(
            "분석 완료: 상태=%s, 얼굴조작=%s, 영상전체AI=%s",
            final.analysis_status, final.face_manipulation.status,
            final.whole_video_generation.status,
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


def _asdict_axis(axis: report.ManipulationAxis) -> dict:
    return asdict(axis)


def _is_supported_url(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _check_supported_video(media: downloader.VideoMedia, opts: AnalysisOptions) -> None:
    """M-02의 지원 조건을 확인한다. 조건 밖이면 분석하지 않고 이유를 알려준다.

    비공개·삭제·연령 제한은 다운로드 단계에서 이미 실패하므로 여기까지 오지 않는다.
    여기서 보는 건 받아온 뒤에야 알 수 있는 것(길이)이다.

    한국어 여부는 분석 전에 확인할 방법이 없어서 검사하지 않는다. 인식된 언어는
    결과에 담겨 나가므로 화면에서 사후 안내로 처리한다(팀 결정).
    """
    if not opts.enforce_input_limits:
        return
    limit = config.max_video_sec
    if limit > 0 and media.duration and media.duration > limit:
        minutes = int(media.duration // 60)
        seconds = int(media.duration % 60)
        raise UnsupportedVideoError(
            f"{minutes}분 {seconds}초 영상이다. 지금은 {limit // 60}분 이하의 "
            "YouTube Shorts만 분석할 수 있다."
        )


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


@dataclass
class TranscriptResult:
    """발언 텍스트 확보 결과.

    항목이 다섯 개가 되면서 튜플로는 호출부에서 무엇이 무엇인지 읽기 어려워졌다.

    `source`는 화면에서 발언 위치 옆에 "음성 인식" / "자막"으로 표시한다.
    같은 타임스탬프라도 자막에서 온 것과 음성 인식에서 온 것은 정확도가 달라서,
    사용자가 원문을 확인하러 갈 때 알고 있어야 한다.
    """

    text: str = ""
    language: str | None = None
    coverage_pct: float | None = None
    segments: list[dict] = field(default_factory=list)
    source: str | None = None  # stt | caption


def _collect_transcript(media: downloader.VideoMedia, opts: AnalysisOptions,
                        tracker: StageTracker) -> TranscriptResult:
    """발언 텍스트를 확보한다.

    설계 문서의 분기를 그대로 따른다: 쓸 수 있는 자막이 있으면 자막을 쓰고,
    없을 때만 음성 인식으로 넘어간다. 자막이 있으면 STT 수십 초를 통째로 아낀다.
    """
    if media.caption_path:
        started = time.monotonic()
        track = captions.load_track(
            media.caption_path, media.caption_language or "", media.caption_source or "manual"
        )
        if track:
            coverage_pct = None
            if media.duration and track.duration:
                coverage_pct = round(min(track.duration, media.duration) / media.duration * 100, 1)
            tracker.mark(
                "transcript", StageState.OK,
                f"자막 사용({media.caption_source}/{media.caption_language}), "
                f"{track.word_count}단어",
                round(time.monotonic() - started, 2),
            )
            return TranscriptResult(track.text, track.language, coverage_pct,
                                   track.segments, source="caption")
        logger.info("자막을 읽지 못해 STT로 넘어간다")

    return _transcribe(media, opts, tracker)


def _transcribe(media: downloader.VideoMedia, opts: AnalysisOptions,
                tracker: StageTracker) -> TranscriptResult:
    """음성 인식으로 발언 텍스트를 확보한다. 실패해도 예외를 올리지 않는다."""
    started = time.monotonic()
    audio = media.audio_path or media.video_path
    try:
        result = transcriber.transcribe(audio, model_size=opts.model_size)
    except Exception as e:
        # STT가 실패해도 영상 분석 결과는 돌려줄 수 있다. 다만 "무음 영상"과
        # 구분되도록 실패 사실을 stages에 남긴다.
        error = as_error_dict(e, stage="transcript")
        tracker.mark("transcript", StageState.FAILED, error["message"],
                     round(time.monotonic() - started, 2), error=error)
        logger.error("STT 실패 (%s)", error["code"], exc_info=True)
        return TranscriptResult()

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
        f"STT {result.word_count}단어" + (f", 커버리지 {coverage_pct}%" if coverage_pct else ""),
        round(time.monotonic() - started, 2),
    )
    return TranscriptResult(result.text, result.language, coverage_pct,
                            result.segments, source="stt")


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


def _claim_verification_snapshot(claim_list: list[claims.Claim]) -> report.ClaimVerification:
    """지금까지의 카드 상태로 ClaimVerification 스냅샷을 만든다.

    pending/verifying 카드가 섞여 있어도 그대로 넣는다 — 아직 안 끝난 카드는
    verdict가 기본값(UNVERIFIED)이지만 status로 "아직 끝나지 않았다"는 걸 구분한다.
    """
    status_counts = Counter(c.status for c in claim_list)
    verdict_counts = Counter(c.verdict for c in claim_list if c.status == claims.DONE)
    return report.ClaimVerification(
        status=report.AxisStatus.ANALYZED.value,
        claims=[c.to_dict() for c in claim_list],
        summary={
            "total": len(claim_list),
            # 처리 상태별 개수. 화면 요약의 "완료 8 · 미완료 0 · 시간 초과 0"이 여기서 나온다.
            "pending": status_counts[claims.PENDING],
            "verifying": status_counts[claims.VERIFYING],
            "done": status_counts[claims.DONE],
            "failed": status_counts[claims.FAILED],
            "timed_out": status_counts[claims.TIMED_OUT],
            # 판정별 개수. 처리 상태와는 별개 축이라 섞지 않는다.
            "supported": verdict_counts[claims.SUPPORTED],
            "refuted": verdict_counts[claims.REFUTED],
            "unverified": verdict_counts[claims.UNVERIFIED],
        },
    )


def _verify_claims(text: str, segments: list[dict], opts: AnalysisOptions,
                   tracker: StageTracker, deadline: float,
                   progress: Callable[[float, str, str | None], None],
                   partial: Callable[[dict], None]) -> report.ClaimVerification:
    """주장 사실성 검증 축: 주장 추출 → 카드 생성(즉시 통지) → 하나씩 검증(통지).

    카드가 폴링마다 채워지는 게 목적이라, 여기서 리스트를 한 번에 처리하고 끝에
    돌려주지 않는다. 추출 직후 pending 카드를 전부 만들어 `partial`로 알리고,
    하나씩 검증할 때마다 그 시점의 전체 스냅샷을 다시 `partial`로 보낸다.
    """
    if not opts.enable_claim_verification:
        tracker.mark("claim_verification", StageState.SKIPPED, "옵션이 꺼져 있음")
        return report.ClaimVerification(
            status=report.AxisStatus.UNAVAILABLE.value,
            detail="주장 사실성 검증을 요청하지 않았다.",
        )

    started = time.monotonic()
    if not text:
        tracker.mark("claim_verification", StageState.SKIPPED, "발언 텍스트가 없음")
        return report.ClaimVerification(
            status=report.AxisStatus.UNAVAILABLE.value,
            detail="발언 텍스트를 확보하지 못해 검증할 주장을 뽑을 수 없다.",
        )

    # LLM 제공자는 주장당 새로 만들지 않고 한 번만 만들어 재사용한다.
    llm_provider = llm.get_provider()
    if llm_provider is not None:
        logger.info("LLM 제공자 활성: %s (%s)", config.llm_provider, config.llm_model)

    try:
        extract = claims.select_extractor(llm_provider)
        extracted = extract(text, segments, config.max_claims)
    except Exception as e:
        error = as_error_dict(e, stage="claim_verification")
        tracker.mark("claim_verification", StageState.FAILED, error["message"],
                     round(time.monotonic() - started, 2), error=error)
        logger.error("주장 추출 실패 (%s)", error["code"], exc_info=True)
        return report.ClaimVerification(
            status=report.AxisStatus.UNAVAILABLE.value,
            detail="주장 추출 중 오류가 발생해 판단을 유보한다.",
        )

    if not extracted:
        tracker.mark("claim_verification", StageState.OK, "검증 대상 주장 없음",
                     round(time.monotonic() - started, 2))
        return report.ClaimVerification(
            status=report.AxisStatus.ANALYZED.value,
            # 주장이 없어도 summary 모양은 유지한다. 소비하는 쪽이 키 유무로
            # 분기하지 않게.
            summary={"total": 0, "pending": 0, "verifying": 0, "done": 0,
                     "failed": 0, "timed_out": 0,
                     "supported": 0, "refuted": 0, "unverified": 0},
            detail="검증 가능한 사실 주장을 찾지 못했다. 의견이나 일상 대화 위주의 영상일 수 있다.",
        )

    # 전체 주장 수가 확정된 시점 — 카드를 전부 만들어 즉시 통지한다.
    logger.info("주장 %d건 추출, 검증 시작", len(extracted))
    partial({"claim_verification": asdict(_claim_verification_snapshot(extracted))})
    progress(0.75, f"주장 {len(extracted)}건 검증 중...", "verifying")

    active_providers = claims.default_providers()
    # 카드 갱신 통지가 서로 겹쳐서 반쯤 만들어진 스냅샷이 나가지 않도록 잠근다.
    notify_lock = threading.Lock()

    def verify_and_notify(claim: claims.Claim) -> bool:
        """주장 하나를 검증하고 그 시점의 전체 스냅샷을 통지한다. 시간 초과면 False."""
        if time.monotonic() > deadline:
            # 소프트 타임아웃: 이미 시작한 검증을 끊지는 않고, 아직 시작하지 않은
            # 주장만 시간 초과로 채운다.
            claim.status = claims.TIMED_OUT
            claim.insufficient_reason = claims.TIMEOUT
            claim.insufficient_label = claims.INSUFFICIENT_LABELS[claims.TIMEOUT]
            return False
        try:
            claims.verify_one_claim(claim, active_providers, llm_provider=llm_provider)
        except Exception:
            # 주장 하나가 실패해도 나머지는 계속한다(U-05).
            claim.status = claims.FAILED
            logger.exception("주장 검증 실패: %s", claim.text[:40])
        with notify_lock:
            partial({"claim_verification": asdict(_claim_verification_snapshot(extracted))})
        return True

    workers = max(1, min(config.claim_workers, len(extracted)))
    logger.info("주장 %d건 검증 시작 (동시 %d건)", len(extracted), workers)
    if workers == 1:
        for claim in extracted:
            verify_and_notify(claim)
    else:
        # job_id는 새 스레드로 자동 전파되지 않는다. 안 넘기면 병렬 구간의 로그가
        # 전부 job:- 로 남아 어느 작업의 로그인지 알 수 없게 된다.
        with_context = carry_context()

        def run_in_worker(claim: claims.Claim) -> bool:
            with with_context():
                return verify_and_notify(claim)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="claim") as pool:
            list(pool.map(run_in_worker, extracted))

    timed_out_count = sum(1 for c in extracted if c.status == claims.TIMED_OUT)
    if timed_out_count:
        logger.warning("전체 처리 시간(%.0fs) 초과 — 주장 %d건은 시간 초과로 처리",
                       config.max_processing_sec, timed_out_count)
    with notify_lock:
        partial({"claim_verification": asdict(_claim_verification_snapshot(extracted))})

    counts = Counter(c.verdict for c in extracted if c.status == claims.DONE)
    detail = None
    if timed_out_count:
        detail = f"전체 처리 시간 초과로 주장 {timed_out_count}건은 검증하지 못했다."
    elif not (counts[claims.REFUTED] or counts[claims.SUPPORTED]):
        detail = "관련 자료는 모았지만 이 주장들을 직접 검증한 판정을 찾지 못해 모두 판단을 유보했다."

    tracker.mark(
        "claim_verification", StageState.OK,
        f"주장 {len(extracted)}건 (지지 {counts[claims.SUPPORTED]}, "
        f"반박 {counts[claims.REFUTED]}, 유보 {counts[claims.UNVERIFIED]}, "
        f"시간초과 {timed_out_count})",
        round(time.monotonic() - started, 2),
    )
    return report.ClaimVerification(
        status=report.AxisStatus.ANALYZED.value,
        claims=[c.to_dict() for c in extracted],
        summary={
            "total": len(extracted),
            "supported": counts[claims.SUPPORTED],
            "refuted": counts[claims.REFUTED],
            "unverified": counts[claims.UNVERIFIED],
            "timed_out": timed_out_count,
        },
        detail=detail,
    )
