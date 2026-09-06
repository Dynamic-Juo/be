"""DeepCheck CLI: 영상 URL 하나를 분석한다.

파이프라인 본체는 `pipeline.py`에 있고, 여기서는 인자 파싱과 출력만 맡는다.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import report
from .config import config
from .logging_setup import setup_logging
from .pipeline import AnalysisOptions, analyze_url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deepcheck", description="영상 조작 가능성 분석기")
    p.add_argument("command", choices=["analyze"])
    p.add_argument("url", help="YouTube/웹 영상 URL")
    p.add_argument("--model-size", default=config.whisper_model_size,
                   choices=["tiny", "base", "small", "medium", "large-v3"],
                   help=f"faster-whisper 모델 (기본 {config.whisper_model_size})")
    p.add_argument("--max-frames", type=int, default=config.max_frames,
                   help=f"샘플링 프레임 수 (기본 {config.max_frames})")
    p.add_argument("--no-classifier", action="store_true",
                   help="ViT 딥페이크 분류기를 쓰지 않고 휴리스틱만 사용")
    p.add_argument("--vlm-model", default=None, help="Ollama 비전모델 (예: qwen2.5vl:7b)")
    p.add_argument("--no-claim-verification", action="store_true",
                   help="주장 사실성 검증을 건너뛴다 (외부 검색을 하지 않아 더 빠름)")
    p.add_argument("--caption-policy", default=config.caption_policy,
                   choices=["manual", "any", "off"],
                   help=f"자막 사용 정책 (기본 {config.caption_policy}). "
                        "manual=수동 자막만, any=자동 자막까지, off=항상 STT")
    p.add_argument("--keep", action="store_true", help="다운로드 파일 유지")
    p.add_argument("--save-transcript", nargs="?", const="deepcheck_transcript.txt", default=None,
                   help="STT 전체 텍스트(+타임스탬프)를 파일로 저장")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.add_argument("--html", action="store_true", help="HTML 레포트 저장 (deepcheck_report.html)")
    p.add_argument("--workdir", default=None, help="작업 디렉토리 지정 (기본 임시)")
    p.add_argument("--log-level", default=None, help="로그 레벨 (기본 INFO)")

    args = p.parse_args(argv)
    setup_logging(args.log_level)

    payload = analyze_url(
        url=args.url,
        options=AnalysisOptions(
            model_size=args.model_size,
            max_frames=args.max_frames,
            use_classifier=not args.no_classifier,
            vlm_model=args.vlm_model,
            enable_claim_verification=not args.no_claim_verification,
            caption_policy=args.caption_policy,
            workdir=args.workdir,
            keep_workdir=args.keep,
            save_transcript=args.save_transcript,
        ),
    )

    result = report.AnalysisReport.from_dict(payload)

    if args.html:
        with open("deepcheck_report.html", "w", encoding="utf-8") as f:
            f.write(report.format_html(result))
        print("HTML 레포트 저장: deepcheck_report.html", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report.format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
