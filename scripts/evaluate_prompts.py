"""고정된 가상 자료로 프롬프트를 점검한다. 실제 검색·영상 수집 정확도는 측정하지 않는다.

python scripts/evaluate_prompts.py                        # 목록만 확인
python scripts/evaluate_prompts.py --live --env-file .env # 유료 API 호출 명시
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "results/prompt-evaluation.json")
    args = parser.parse_args()
    cases = json.loads((ROOT / "tests/fixtures/prompt_cases.json").read_text())
    if not args.live:
        print(json.dumps({"cases": [c["id"] for c in cases], "network_calls": 0}))
        return 0
    if args.env_file:
        allowed = {"DEEPCHECK_LLM_PROVIDER", "DEEPCHECK_LLM_MODEL", "DEEPCHECK_LLM_BASE_URL",
                   "DEEPCHECK_LLM_API_KEY", "DEEPCHECK_LLM_TIMEOUT_SEC", "DEEPCHECK_OLLAMA_URL"}
        for line in args.env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in allowed:
                parts = shlex.split(value, comments=True)
                if len(parts) > 1:
                    raise ValueError("LLM 환경설정 값은 공백이 있으면 따옴표로 감싸야 한다")
                os.environ[key.strip()] = parts[0] if parts else ""
    from deepcheck import claims, llm
    from deepcheck.config import config
    from deepcheck.prompts import PROMPT_VERSION, VERDICT_SYSTEM
    provider = llm.get_provider()
    if provider is None:
        raise SystemExit("실제 LLM 제공자를 설정해야 한다")

    def run(case):
        started = time.monotonic()
        errors = []
        output = None
        try:
            if case["task"] == "extract":
                class ObservedProvider:
                    name = provider.name

                    def complete(self, system, user, max_tokens):
                        try:
                            raw = provider.complete(system, user, max_tokens)
                            # 폴백 결과가 기대값과 맞아도 실모델 성공으로 세지 않는다.
                            parsed = json.loads(raw)
                            if not isinstance(parsed, dict) or not isinstance(parsed.get("claims"), list):
                                errors.append("invalid_extraction_payload")
                            return raw
                        except Exception:
                            errors.append("extraction_provider_error")
                            raise

                found = claims.extract_claims_llm(case["transcript"], [], 0, ObservedProvider())
                output = [{"text": c.text, "context": c.context} for c in found]
                norm = claims._normalize_for_quote
                if {norm(c.text) for c in found} != {norm(t) for t in case["expected_texts"]}:
                    errors.append("extracted_claims")
                if case.get("required_context") and not any(
                        case["required_context"] in c.context for c in found):
                    errors.append("context")
            else:
                data = {"claim": case["claim"], "context": "", "video_published_at": "2022-08-01",
                        "evidence": case["evidence"]}
                output = llm.complete_json(provider, VERDICT_SYSTEM,
                    json.dumps(data, ensure_ascii=False), max_tokens=2048)
                if output.get("verdict") != case["expected_verdict"]:
                    errors.append("verdict")
                if case.get("expected_reason") and output.get("insufficient_reason") != case["expected_reason"]:
                    errors.append("insufficient_reason")
                cited = output.get("cited")
                if case["expected_verdict"] == "부족":
                    if cited != []:
                        errors.append("unexpected_citations")
                elif not isinstance(cited, list) or not cited:
                    errors.append("missing_citations")
                else:
                    for citation in cited:
                        index = citation.get("index")
                        quote = claims._normalize_for_quote(citation.get("quote", ""))
                        if (type(index) is not int or not 1 <= index <= len(case["evidence"])
                                or len(quote) < 8 or quote not in claims._normalize_for_quote(
                                    case["evidence"][index - 1]["content"])):
                            errors.append("invalid_citation")
        except Exception as exc:
            # 인증 실패 응답 본문이나 환경변수는 평가 산출물에 남기지 않는다.
            errors.append(type(exc).__name__)
        return {"id": case["id"], "passed": not errors, "errors": errors,
                "seconds": round(time.monotonic() - started, 2), "output": output}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, cases))
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(),
               "prompt_version": PROMPT_VERSION, "provider": config.llm_provider,
               "prompt_sha256": hashlib.sha256((ROOT / "deepcheck/prompts.py").read_bytes()).hexdigest(),
               "model": config.llm_model, "temperature": config.llm_temperature,
               "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
               "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
               "passed": sum(r["passed"] for r in results), "total": len(results), "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: payload[k] for k in ("prompt_version", "model", "passed", "total")}))
    for r in results:
        print(r["id"], "PASS" if r["passed"] else "FAIL", ",".join(r["errors"]))
    return 0 if payload["passed"] == payload["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
