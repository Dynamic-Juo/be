"""DeepCheck CLI: analyze a video URL end-to-end."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Callable

from . import downloader, transcriber, deepfake, analyzer, report


def analyze_url(url: str, workdir: str | None = None, model_size: str = "small",
                max_frames: int = 8, use_classifier: bool = False,
                vlm_model: str | None = None, keep: bool = False,
                save_transcript: str | None = None,
                progress_cb: Callable[[float, str], None] | None = None) -> dict:
    """Run the full pipeline and return a RiskReport dict.

    progress_cb(pct, message) is called at each stage (0..1) if provided.
    """
    def _p(pct: float, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)

    tmp = workdir or tempfile.mkdtemp(prefix="deepcheck_")
    os.makedirs(tmp, exist_ok=True)

    try:
        # 1) Download
        _p(0.10, "다운로드 중...")
        print(f"[1/3] 다운로드 중... {url}", file=sys.stderr)
        media = downloader.download(url, tmp)
        print(f"      → {media.title or ''} ({media.video_path})", file=sys.stderr)

        # 2) Frame sampling + deepfake / AI detection
        _p(0.35, "프레임 샘플링 및 AI 생성 분석 중...")
        print("[2/3] 프레임 샘플링 및 AI 생성 분석 중...", file=sys.stderr)
        frames_dir = os.path.join(tmp, "frames")
        frames = downloader.extract_frames(media.video_path, frames_dir, max_frames=max_frames)
        det = deepfake.DeepfakeDetector(
            use_classifier=use_classifier,
            vlm_model=vlm_model,
        )
        det_report = det.analyze(frames)

        # 3) Transcript + text analysis
        _p(0.70, "음성→텍스트(STT) 분석 중...")
        print("[3/3] 음성→텍스트(STT) 분석 중...", file=sys.stderr)
        audio_or_video = media.audio_path or media.video_path
        try:
            result = transcriber.transcribe(audio_or_video, model_size=model_size)
            text = result.text
            lang = result.language
            # Sanity check: did STT actually cover the whole video, or stop early?
            if media.duration and result.duration:
                pct = min(result.duration, media.duration) / media.duration * 100
                print(
                    f"      STT 커버리지: {result.duration:.1f}s 처리 / 영상 길이 {media.duration:.1f}s ({pct:.1f}%)",
                    file=sys.stderr,
                )
            # Full transcript used to be discarded after producing the crude
            # extractive summary — keep the raw text + timestamped segments
            # on disk so STT quality can actually be inspected/verified.
            if save_transcript:
                with open(save_transcript, "w", encoding="utf-8") as tf:
                    tf.write(f"[영상 제목] {media.title}\n")
                    tf.write(f"[영상 길이] {media.duration}s / [STT 처리 길이] {result.duration}s\n\n")
                    tf.write("=== 전체 transcript (원문 그대로) ===\n\n")
                    tf.write(text + "\n\n")
                    tf.write("=== 세그먼트 (타임스탬프) ===\n\n")
                    for seg in result.segments:
                        tf.write(f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}\n")
                print(f"      → 전체 transcript 저장: {save_transcript}", file=sys.stderr)
        except Exception as e:  # pragma: no cover
            print(f"      STT 실패: {e}", file=sys.stderr)
            text, lang = "", None
        txt_report = analyzer.analyze(text, lang, title=media.title, description=media.description)

        meta = {
            "url": url,
            "title": media.title,
            "uploader": media.uploader,
            "duration": media.duration,
            "language": lang,
        }

        # 4) Report
        _p(0.95, "레포트 생성 중...")
        final = report.build(meta, det_report.__dict__, txt_report.__dict__)
        _p(1.0, "완료")
        return final.to_dict()
    finally:
        if not keep and tmp and not workdir:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _boolish(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "y", "on")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deepcheck", description="AI 가짜영상 위험도 분석기")
    p.add_argument("command", choices=["analyze"])
    p.add_argument("url", help="YouTube/웹 영상 URL")
    p.add_argument("--model-size", default="small",
                   choices=["tiny", "base", "small", "medium", "large-v3"],
                   help="faster-whisper 모델 (기본 small)")
    p.add_argument("--max-frames", type=int, default=8, help="샘플링 프레임 수 (기본 8)")
    p.add_argument("--with-classifier", action="store_true", help="ViT 딥페이크 분류기 사용 (torch 필요)")
    p.add_argument("--vlm-model", default=None, help="Ollama 비전모델 (예: qwen2.5vl:7b)")
    p.add_argument("--keep", action="store_true", help="다운로드 파일 유지")
    p.add_argument("--save-transcript", nargs="?", const="deepcheck_transcript.txt", default=None,
                   help="STT 전체 텍스트(+타임스탬프)를 파일로 저장 (기본: deepcheck_transcript.txt)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.add_argument("--html", action="store_true", help="HTML 레포트 저장 (deepcheck_report.html)")
    p.add_argument("--workdir", default=None, help="작업 디렉토리 지정 (기본 임시)")

    args = p.parse_args(argv)

    payload = analyze_url(
        url=args.url,
        workdir=args.workdir,
        model_size=args.model_size,
        max_frames=args.max_frames,
        use_classifier=args.with_classifier,
        vlm_model=args.vlm_model,
        keep=args.keep,
        save_transcript=args.save_transcript,
    )

    if args.html:
        r = report.RiskReport(**payload)
        with open("deepcheck_report.html", "w", encoding="utf-8") as f:
            f.write(report.format_html(r))
        print(f"HTML 레포트 저장: deepcheck_report.html", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report.format_text(report.RiskReport(**payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
