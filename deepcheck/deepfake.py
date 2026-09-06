"""AI-generated / deepfake frame detection.

Combines up to three signals, each optional so the tool degrades gracefully:

1. ViT-based deepfake frame classifier (HF `dima806/deepfake_vs_real_image_detection`)
2. Local VLM reasoning (Ollama `qwen2.5vl` etc.) producing natural-language evidence
3. Image-level heuristics (artifacts: excessive smoothing, low detail, ASCII-style text, etc.)
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from dataclasses import dataclass, field

from . import face

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
    vlm_note: str | None = None
    face_cropped: bool = False  # True if the classifier ran on a cropped face, not the full frame

    @property
    def score(self) -> float:
        return self.fake_score if self.fake_score is not None else 0.0


@dataclass
class DeepfakeReport:
    frames_analyzed: int
    frames_fake: int
    avg_fake_score: float  # 0..100
    evidence: list[str] = field(default_factory=list)
    method: str = "heuristics"
    vlm_summary: str | None = None

    @property
    def risk(self) -> float:
        return self.avg_fake_score


_CLASSIFIER_MODEL = "dima806/deepfake_vs_real_image_detection"

# Module-level cache: the ViT pipeline is loaded once per process and reused
# across every request/worker thread. Previously each DeepfakeDetector()
# instance (created fresh per analyze_url() call, see cli.py) reloaded this
# from scratch every time -- same "reload on every request" issue fixed for
# the STT model in transcriber.py.
_classifier_cache: dict[str, object] = {}
_classifier_lock = threading.Lock()


def _get_classifier_pipeline(model_name: str = _CLASSIFIER_MODEL):
    if model_name in _classifier_cache:
        return _classifier_cache[model_name]
    with _classifier_lock:
        if model_name in _classifier_cache:
            return _classifier_cache[model_name]
        try:
            from transformers import pipeline
            pipe = pipeline("image-classification", model=model_name, top_k=None)
        except Exception:
            pipe = None
        _classifier_cache[model_name] = pipe
        return pipe


class DeepfakeDetector:
    """Detector holder; each stage is lazy and enabled by kwargs."""

    def __init__(self, use_classifier: bool = False, vlm_endpoint: str | None = None,
                 vlm_model: str | None = None, ollama_url: str = "http://localhost:11434"):
        self.use_classifier = use_classifier
        self.vlm_endpoint = vlm_endpoint
        self.vlm_model = vlm_model
        self.ollama_url = ollama_url.rstrip("/")

    # ---- classifier (torch + transformers) ----
    def _load_classifier(self):
        return _get_classifier_pipeline(_CLASSIFIER_MODEL)

    def _classifier_score(self, frame_path: str) -> tuple[float | None, str | None]:
        pipe = self._load_classifier()
        if pipe is None:
            return None, None
        try:
            out = pipe(frame_path)
        except Exception:
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
        except Exception:
            return arts
        return arts

    # ---- VLM (Ollama) ----
    def _vlm_analyze(self, frames: list[str]) -> tuple[str | None, str | None]:
        # VLM only runs when a model is explicitly requested. (Fixes a bug where
        # the backend's default request -- use_classifier=True, vlm_model=None --
        # made this condition False, so every default API call silently tried to
        # call Ollama anyway, even though VLM was meant to be strictly opt-in.)
        if not self.vlm_model:
            return None, None
        if Image is None:
            return None, None

        model = self.vlm_model or "qwen2.5vl:7b"
        # Encodings: build a small multi-image prompt by concatenating the first frame
        # (Ollama vision handles one image per message conveniently).
        note_list = []
        for fp in frames[:1]:
            b64 = _encode_b64(fp)
            if not b64:
                continue
            prompt = (
                "이미지에서 AI가 생성했거나 딥페이크일 수 있는 시각적 흔적(얼굴 비대칭, 손/손가락 왜곡, "
                "사람 피부 불연속, 비정상 그림자, 공간 반복 등)이 있으면 한국어로 구체적으로 설명하세요. "
                "없으면 '특이사항 없음'이라고만 답하세요. 2문장 이내로."
            )
            try:
                payload = {"model": model, "prompt": prompt, "images": [b64], "stream": False}
                req = urllib.request.Request(
                    f"{self.ollama_url}/api/generate",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read().decode())
                note_list.append(data.get("response", "").strip())
            except Exception as e:
                note_list.append(f"(VLM 불가: {e})")
        if note_list:
            joined = "\n".join(n for n in note_list if n)
            # A note is "successful" if it is a real analysis we can use.
            ok = any(n and not n.startswith("(VLM") for n in note_list)
            return (joined if ok else None), (model if ok else None)
        return None, None

    # ---- main entry ----
    def analyze(self, frames: list[str], frames_dir: str | None = None) -> DeepfakeReport:
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

        scored = [r for r in results if r.fake_score is not None]
        if scored:
            avg = sum(r.fake_score for r in scored) / len(scored)
            frames_fake = sum(1 for r in scored if r.fake_score >= 0.5)
            # Don't let one very confident frame get diluted into nothing by a
            # simple average across all sampled frames (confirmed with a real
            # video: 1/8 frames at 98.4% still only averaged to 13/100). Blend
            # the mean with the single strongest frame, same "don't dilute a
            # strong signal" idea as the self-disclosure floor in report.py.
            # Weighting is a tunable judgment call, not a proven-optimal constant.
            peak = max(r.fake_score for r in scored)
            avg = max(avg, avg * 0.6 + peak * 0.4)
        else:
            # No classifier: fall back to heuristic-only pseudo score.
            heur_penalty = sum(1 for r in results if r.heuristic_artifacts)
            avg = (heur_penalty / max(len(results), 1)) * 0.55
            frames_fake = heur_penalty

        evidence: list[str] = []
        for r in results:
            crop_tag = " [얼굴 crop 적용]" if r.face_cropped else ""
            if r.fake_score is not None and r.score >= 0.5:
                evidence.append(
                    f"{os.path.basename(r.path)}: fake {r.score:.1%} (라벨 {r.label}){crop_tag}"
                )
            elif r.heuristic_artifacts:
                evidence.append(
                    f"{os.path.basename(r.path)}: " + "; ".join(r.heuristic_artifacts)
                )

        vlm_summary, method_vlm = self._vlm_analyze(results and [r.path for r in results] or [])
        if vlm_summary and "특이사항" not in vlm_summary and not vlm_summary.startswith("(VLM"):
            evidence.append(f"VLM: {vlm_summary}")

        method_parts = ["heuristics"]
        if scored:
            method_parts.append("ViT-classifier")
            if any(r.face_cropped for r in results):
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
        )


def _encode_b64(path: str) -> str | None:
    if Image is None:
        return None
    try:
        import base64
        import io
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


def classify(frames: list[str], use_classifier: bool = True,
             vlm_model: str | None = None, ollama_url: str = "http://localhost:11434") -> DeepfakeReport:
    det = DeepfakeDetector(
        use_classifier=use_classifier,
        vlm_model=vlm_model,
        ollama_url=ollama_url,
    )
    return det.analyze(frames)
