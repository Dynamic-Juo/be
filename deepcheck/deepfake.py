"""AI-generated / deepfake frame detection.

Combines up to three signals, each optional so the tool degrades gracefully:

1. ViT-based deepfake frame classifier (HF `dima806/deepfake_vs_real_image_detection`)
2. Local VLM reasoning (Ollama `qwen2.5vl` etc.) producing natural-language evidence
3. Image-level heuristics (artifacts: excessive smoothing, low detail, ASCII-style text, etc.)
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from . import face, vlm
from .config import config

logger = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


@dataclass
class FrameResult:
    path: str
    fake_score: float | None  # 0..1 probability of being fake/AI-generated
    label: str | None
    heuristic_artifacts: list[str] = field(default_factory=list)
    face_cropped: bool = False  # True if the classifier ran on a cropped face, not the full frame


@dataclass
class DeepfakeReport:
    frames_analyzed: int
    frames_fake: int
    avg_fake_score: float  # 0..100
    evidence: list[str] = field(default_factory=list)
    method: str = "heuristics"
    vlm_summary: str | None = None
    # 얼굴 기반 분류기의 결과를 믿어도 되는지 판단하는 데 필요한 정보.
    # 얼굴을 한 명도 못 찾았는데 낮은 점수가 나온 것을 "정상 영상"으로 읽으면 안 된다.
    frames_with_face: int = 0
    face_model_available: bool = False
    # 프레임별 fake 확률(0..1). 집계 방식을 바꿔가며 튜닝하려면 원본 점수가 있어야
    # 하고, 결과가 이상할 때 "어느 프레임 때문인지"를 로그 없이도 볼 수 있다.
    # 사용자에게는 노출하지 않는다 — signals 안에만 남는다.
    frame_scores: list[float] = field(default_factory=list)


# Module-level cache: the ViT pipeline is loaded once per process and reused
# across every request/worker thread. Previously each DeepfakeDetector()
# instance (created fresh per analysis) reloaded this from scratch every time --
# same "reload on every request" issue fixed for the STT model in transcriber.py.
_classifier_cache: dict[str, object] = {}
_classifier_lock = threading.Lock()


def _get_classifier_pipeline(model_name: str):
    if model_name in _classifier_cache:
        return _classifier_cache[model_name]
    with _classifier_lock:
        if model_name in _classifier_cache:
            return _classifier_cache[model_name]
        try:
            from transformers import pipeline
            pipe = pipeline("image-classification", model=model_name, top_k=None)
            logger.info("딥페이크 분류기 로드 완료: %s", model_name)
        except Exception as e:
            pipe = None
            logger.warning("딥페이크 분류기 로드 실패(%s): %s — 휴리스틱으로 진행", model_name, e)
        _classifier_cache[model_name] = pipe
        return pipe


# 프레임 점수 집계 방식.
AGG_BLEND = "blend"
AGG_TRIMMED_MEAN = "trimmed_mean"


def aggregate_frame_scores(scores: list[float]) -> float:
    """프레임별 fake 확률을 영상 하나의 점수로 합친다.

    딥페이크는 영상 전체가 아니라 특정 구간에만 있을 수 있어서, 어떻게 합치느냐가
    결과를 크게 바꾼다. 두 방식은 정반대 위험을 감수한다.

    - `blend`(기본): `max(평균, 평균×0.6 + 최댓값×0.4)`. 강한 프레임 하나를 살린다.
      구간 딥페이크를 잡지만 프레임 하나의 오탐에 휘둘린다. 실측에서 8장 중 1장이
      98.4%일 때 단순 평균 13점이 47점으로 올라갔다.
    - `trimmed_mean`: 최솟값과 최댓값을 하나씩 버리고 평균. 프레임 하나의 오탐에는
      견디지만, **그 최댓값이 진짜 신호일 때 그것부터 버린다.**

    어느 쪽이 나은지는 정답을 아는 영상 세트(M-08 데모 점검표) 없이 정할 수 없다.
    그래서 설정으로 빼두고 실측 비교가 가능하게 했다.
    """
    if not scores:
        return 0.0
    mean = sum(scores) / len(scores)

    if config.frame_aggregation == AGG_TRIMMED_MEAN:
        # 양 끝을 버리려면 최소 3장은 있어야 한다. 그보다 적으면 평균 그대로 쓴다.
        if len(scores) < 3:
            logger.info("프레임이 %d장뿐이라 절사 없이 평균을 쓴다", len(scores))
            return mean
        trimmed = sorted(scores)[1:-1]
        result = sum(trimmed) / len(trimmed)
        logger.info("집계(절사평균): %d장 중 양 끝 제외 → 평균 %.3f → %.3f",
                    len(scores), mean, result)
        return result

    peak = max(scores)
    blended = mean * config.frame_mean_weight + peak * config.frame_peak_weight
    result = max(mean, blended)
    logger.info("집계(블렌드): 평균 %.3f, 최댓값 %.3f → %.3f", mean, peak, result)
    return result


class DeepfakeDetector:
    """Detector holder; each stage is lazy and enabled by kwargs."""

    def __init__(self, use_classifier: bool = True, vlm_model: str | None = None,
                 classifier_model: str | None = None):
        self.use_classifier = use_classifier
        self.vlm_model = vlm_model
        self.classifier_model = classifier_model or config.classifier_model

    # ---- classifier (torch + transformers) ----
    def _load_classifier(self):
        return _get_classifier_pipeline(self.classifier_model)

    def _classifier_score(self, frame_path: str) -> tuple[float | None, str | None]:
        pipe = self._load_classifier()
        if pipe is None:
            return None, None
        try:
            out = pipe(frame_path)
        except Exception as e:
            logger.warning("프레임 분류 실패(%s): %s", os.path.basename(frame_path), e)
            return None, None
        # Prefer a 'fake'-ish label; map label -> score.
        fake_pair = next((x for x in out if (x["label"] or "").lower().replace(" ", "") in
                          ("fake", "deepfake", "ai", "gans", "generated")), None)
        if fake_pair is not None:
            return float(fake_pair["score"]), fake_pair["label"]
        # If only real vs fake exists but no label matched, fall back to first.
        pair = out[0] if out else None
        if pair:
            return float(pair["score"]), pair["label"]
        return None, None

    # ---- heuristics ----
    def _heuristics(self, frame_path: str) -> list[str]:
        """Cheap image-statistics heuristics flagging common generating artifacts."""
        arts: list[str] = []
        if Image is None:
            return arts
        try:
            img = Image.open(frame_path).convert("RGB")
            small = img.resize((64, 64))
            px = list(small.getdata())
            n = len(px)
            import statistics
            # low local variance => overly smooth (blobby AI faces)
            grays = [(r + g + b) / 3 for r, g, b in px]
            for name, vals in (("luma", grays),):
                mean = statistics.mean(vals)
                var = statistics.pstdev(vals)
                if var < 10:
                    arts.append(f"{name} 분산 낮음({var:.1f}) — 과도한 스무딩/블러 의심")
            # color uniformity
            rs = statistics.mean(p[0] for p in px)
            gs = statistics.mean(p[1] for p in px)
            bs = statistics.mean(p[2] for p in px)
            if abs(rs - gs) < 6 and abs(gs - bs) < 6 and abs(rs - bs) < 6 and 90 < rs < 150:
                arts.append("전체 색상 편차 지나치게 낮음 — 합성 배경 의심")
        except Exception as e:
            logger.warning("휴리스틱 분석 실패(%s): %s", os.path.basename(frame_path), e)
            return arts
        return arts

    # ---- VLM (교체 가능, deepcheck/vlm.py) ----
    def _vlm_analyze(self, frames: list[str]) -> tuple[str | None, str | None, str | None]:
        """(요약, 사용 모델, 실패 사유)를 반환한다.

        VLM은 모델을 명시적으로 지정했을 때만 실행한다. 실패 사유는 버리지 않고
        돌려줘서, VLM을 켰는데 근거가 없을 때 왜 없는지 알 수 있게 한다.
        """
        provider = vlm.get_provider(self.vlm_model)
        if provider is None:
            return None, None, None
        # 비전 모델은 이미지 한 장씩 다루는 편이 안정적이라 대표 프레임만 보낸다.
        target = frames[:1]
        if not target:
            return None, None, "분석할 프레임이 없어 VLM을 건너뜀"

        try:
            note = provider.describe(target[0])
        except vlm.VLMUnavailable as e:
            logger.warning("VLM 사용 불가: %s", e)
            return None, None, str(e)

        logger.info("VLM(%s/%s) 분석 완료", provider.name, self.vlm_model)
        return note, self.vlm_model, None

    # ---- main entry ----
    def analyze(self, frames: list[str]) -> DeepfakeReport:
        if not frames:
            # 프레임이 없으면 "조작 없음"이 아니라 "판단할 재료가 없음"이다.
            # 이 구분은 report.build_face_manipulation이 unavailable로 처리한다.
            logger.warning("분석할 프레임이 0장이라 영상 분석을 건너뛴다")
            return DeepfakeReport(
                frames_analyzed=0, frames_fake=0, avg_fake_score=0.0,
                evidence=[], method="none", vlm_summary=None,
                face_model_available=face.available(),
            )

        results: list[FrameResult] = []
        for fp in frames:
            fr = FrameResult(path=fp, fake_score=None, label=None)
            if self.use_classifier:
                classify_path = fp
                crop_path = face.crop_face(fp)
                if crop_path:
                    classify_path = crop_path
                    fr.face_cropped = True
                score, label = self._classifier_score(classify_path)
                fr.fake_score, fr.label = score, label
            fr.heuristic_artifacts = self._heuristics(fp)
            results.append(fr)

        cropped = sum(1 for r in results if r.face_cropped)
        face_model_available = face.available()
        logger.info(
            "프레임 %d장 분석, 얼굴 검출 %d장 (얼굴 모델 %s)",
            len(results), cropped, "있음" if face_model_available else "없음",
        )
        if self.use_classifier and cropped == 0:
            # 설계 문서 원칙: 얼굴을 못 찾은 것을 정상 판정으로 처리하지 않는다.
            # 여기서는 사실만 기록하고, 판단 유보 여부는 report가 정한다.
            reason = ("얼굴 검출 모델이 없어" if not face_model_available else "영상에서 얼굴을 찾지 못해")
            logger.warning("%s 얼굴 기반 분류 결과를 신뢰할 수 없다", reason)

        scored = [r for r in results if r.fake_score is not None]
        frame_scores: list[float] = []
        if scored:
            frame_scores = [round(r.fake_score, 4) for r in scored]
            frames_fake = sum(1 for s in frame_scores if s >= config.fake_frame_threshold)
            avg = aggregate_frame_scores(frame_scores)
        else:
            # 분류기를 못 쓰면 휴리스틱만으로 임시 점수를 낸다. 신뢰도가 낮으므로
            # report 쪽에서 method를 보고 degraded 상태임을 표시한다.
            heur_penalty = sum(1 for r in results if r.heuristic_artifacts)
            avg = (heur_penalty / max(len(results), 1)) * config.heuristic_only_scale
            frames_fake = heur_penalty
            if self.use_classifier:
                logger.warning("분류기 점수를 얻지 못해 휴리스틱 점수(%.3f)로 대체", avg)

        evidence: list[str] = []
        for r in results:
            crop_tag = " [얼굴 crop 적용]" if r.face_cropped else ""
            if r.fake_score is not None and r.fake_score >= config.fake_frame_threshold:
                evidence.append(
                    f"{os.path.basename(r.path)}: fake {r.fake_score:.1%} (라벨 {r.label}){crop_tag}"
                )
            elif r.heuristic_artifacts:
                evidence.append(
                    f"{os.path.basename(r.path)}: " + "; ".join(r.heuristic_artifacts)
                )

        vlm_summary, method_vlm, vlm_error = self._vlm_analyze([r.path for r in results])
        if vlm_summary and "특이사항" not in vlm_summary:
            evidence.append(f"VLM: {vlm_summary}")
        if vlm_error:
            # 실패 사유를 조용히 버리지 않는다 — VLM을 켰는데 근거가 없으면 왜인지 알아야 한다.
            evidence.append(f"VLM 미적용: {vlm_error}")

        method_parts = ["heuristics"]
        if scored:
            method_parts.append("ViT-classifier")
            if cropped:
                method_parts.append("face-crop")
        if method_vlm:
            method_parts.append(f"VLM({method_vlm})")

        return DeepfakeReport(
            frames_analyzed=len(results),
            frames_fake=frames_fake,
            avg_fake_score=round(avg * 100, 1),
            evidence=evidence,
            method="+".join(method_parts),
            vlm_summary=vlm_summary,
            frames_with_face=cropped,
            face_model_available=face_model_available,
            frame_scores=frame_scores,
        )


