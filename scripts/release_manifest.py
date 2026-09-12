"""CI가 게시한 불변 이미지 메타데이터를 만들고 검증한다.

이 파일은 이미지를 빌드하거나 배포하지 않는다. GitHub Actions의 main push run과
GHCR digest를 연결하는 작은 JSON 계약만 다룬다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
WORKFLOW_PATH = ".github/workflows/backend-ci.yml"
DEPLOYMENT_WORKFLOW_PATH = ".github/workflows/dev-deployment.yml"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ManifestError(ValueError):
    """배포 근거가 기대한 계약과 다를 때 발생한다."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(), object_pairs_hook=_object_without_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"JSON을 읽지 못했다: {path}") from exc
    if not isinstance(value, dict):
        raise ManifestError("JSON 최상위 값은 객체여야 한다")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"누락={','.join(sorted(missing))}")
        if unknown:
            details.append(f"미지원={','.join(sorted(unknown))}")
        raise ManifestError(f"{label} 필드가 계약과 다르다: {'; '.join(details)}")


def validate_identifiers(source_sha: str, ci_run_id: str, ci_run_attempt: str) -> None:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ManifestError("source_sha는 소문자 40자리 Git SHA여야 한다")
    if not RUN_ID_RE.fullmatch(ci_run_id):
        raise ManifestError("ci_run_id는 양의 정수여야 한다")
    if not RUN_ID_RE.fullmatch(ci_run_attempt):
        raise ManifestError("ci_run_attempt는 양의 정수여야 한다")


def expected_image_repository(repository: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ManifestError("repository는 owner/name 형식이어야 한다")
    return f"ghcr.io/{repository.lower()}"


def validate_image_reference(image: str, repository: str) -> tuple[str, str]:
    prefix = f"{expected_image_repository(repository)}@"
    if not image.startswith(prefix):
        raise ManifestError(f"이미지는 {prefix}sha256:<64자리> 형식이어야 한다")
    digest = image[len(prefix):]
    if not DIGEST_RE.fullmatch(digest):
        raise ManifestError("이미지 digest가 sha256 소문자 64자리 형식이 아니다")
    return prefix[:-1], digest


def build_manifest(
    *,
    repository: str,
    source_sha: str,
    ci_run_id: str,
    ci_run_attempt: str,
    image: str,
    event: str = "push",
    created_at: str | None = None,
) -> dict[str, Any]:
    validate_identifiers(source_sha, ci_run_id, ci_run_attempt)
    if event not in {"push", "workflow_dispatch"}:
        raise ManifestError("지원하지 않는 CI event다")
    image_repository, digest = validate_image_reference(image, repository)
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "source_sha": source_sha,
        "ci_run_id": int(ci_run_id),
        "ci_run_attempt": int(ci_run_attempt),
        "workflow_path": WORKFLOW_PATH,
        "event": event,
        "image_repository": image_repository,
        "image_digest": digest,
        "image": image,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }


def validate_manifest(
    value: dict[str, Any],
    *,
    repository: str,
    source_sha: str,
    ci_run_id: str,
    ci_run_attempt: str,
) -> dict[str, Any]:
    validate_identifiers(source_sha, ci_run_id, ci_run_attempt)
    _require_exact_keys(value, {
        "schema_version", "repository", "source_sha", "ci_run_id", "ci_run_attempt",
        "workflow_path", "event", "image_repository", "image_digest", "image", "created_at",
    }, "release manifest")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "source_sha": source_sha,
        "ci_run_id": int(ci_run_id),
        "ci_run_attempt": int(ci_run_attempt),
        "workflow_path": WORKFLOW_PATH,
        "event": "push",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ManifestError(f"release manifest의 {key} 값이 기대값과 다르다")
    image_repository, digest = validate_image_reference(value.get("image", ""), repository)
    if value.get("image_repository") != image_repository:
        raise ManifestError("image_repository와 image가 일치하지 않는다")
    if value.get("image_digest") != digest:
        raise ManifestError("image_digest와 image가 일치하지 않는다")
    created_at = value.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ManifestError("created_at이 비어 있다")
    return value


def validate_manifest_for_repository(
    value: dict[str, Any], *, repository: str
) -> dict[str, Any]:
    source_sha = value.get("source_sha")
    ci_run_id = value.get("ci_run_id")
    ci_run_attempt = value.get("ci_run_attempt")
    if type(ci_run_id) is not int or type(ci_run_attempt) is not int:
        raise ManifestError("CI run 식별자는 정수여야 한다")
    if not isinstance(source_sha, str):
        raise ManifestError("source_sha가 문자열이 아니다")
    return validate_manifest(
        value,
        repository=repository,
        source_sha=source_sha,
        ci_run_id=str(ci_run_id),
        ci_run_attempt=str(ci_run_attempt),
    )


def build_deployment_request(
    *,
    release: dict[str, Any],
    repository: str,
    environment: str,
    request_run_id: str,
    request_run_attempt: str,
    request_sha: str,
    dispatch_actor: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    validate_manifest_for_repository(release, repository=repository)
    if environment != "conan-development":
        raise ManifestError("지원하는 environment는 conan-development뿐이다")
    validate_identifiers(request_sha, request_run_id, request_run_attempt)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}(\[bot\])?", dispatch_actor):
        raise ManifestError("dispatch_actor가 GitHub actor 형식이 아니다")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "repository": repository,
        "environment": environment,
        "workflow_path": DEPLOYMENT_WORKFLOW_PATH,
        "request_ref": "refs/heads/main",
        "request_sha": request_sha,
        "request_run_id": int(request_run_id),
        "request_run_attempt": int(request_run_attempt),
        "dispatch_actor": dispatch_actor,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "release": release,
    }


def validate_deployment_request(
    value: dict[str, Any], *, repository: str, environment: str = "conan-development"
) -> dict[str, Any]:
    _require_exact_keys(value, {
        "schema_version", "repository", "environment", "workflow_path", "request_ref",
        "request_sha", "request_run_id", "request_run_attempt", "dispatch_actor", "created_at",
        "release",
    }, "deployment request")
    expected = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "repository": repository,
        "environment": environment,
        "workflow_path": DEPLOYMENT_WORKFLOW_PATH,
        "request_ref": "refs/heads/main",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ManifestError(f"deployment request의 {key} 값이 기대값과 다르다")
    request_sha = value.get("request_sha")
    run_id = value.get("request_run_id")
    run_attempt = value.get("request_run_attempt")
    if not isinstance(request_sha, str) or type(run_id) is not int or type(run_attempt) is not int:
        raise ManifestError("deployment request 식별자 형식이 잘못됐다")
    validate_identifiers(request_sha, str(run_id), str(run_attempt))
    dispatch_actor = value.get("dispatch_actor")
    if not isinstance(dispatch_actor, str) or not re.fullmatch(
        r"[A-Za-z0-9-]{1,64}(\[bot\])?", dispatch_actor
    ):
        raise ManifestError("dispatch_actor가 GitHub actor 형식이 아니다")
    created_at = value.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ManifestError("deployment request created_at이 비어 있다")
    release = value.get("release")
    if not isinstance(release, dict):
        raise ManifestError("deployment request release가 객체가 아니다")
    validate_manifest_for_repository(release, repository=repository)
    return value


def validate_workflow_run(
    value: dict[str, Any],
    *,
    repository: str,
    source_sha: str,
    ci_run_id: str,
    ci_run_attempt: str,
) -> None:
    validate_identifiers(source_sha, ci_run_id, ci_run_attempt)
    checks = {
        "id": int(ci_run_id),
        "run_attempt": int(ci_run_attempt),
        "head_branch": "main",
        "head_sha": source_sha,
        "path": WORKFLOW_PATH,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    for key, expected_value in checks.items():
        if value.get(key) != expected_value:
            raise ManifestError(f"CI run의 {key} 값이 기대값과 다르다")
    run_repository = value.get("repository")
    if not isinstance(run_repository, dict) or run_repository.get("full_name") != repository:
        raise ManifestError("CI run 저장소가 기대한 저장소와 다르다")


def write_manifest(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    identifiers = subparsers.add_parser("validate-identifiers")
    _add_identity_arguments(identifiers)

    create = subparsers.add_parser("create")
    _add_identity_arguments(create)
    create.add_argument("--image", required=True)
    create.add_argument("--event", required=True)
    create.add_argument("--output", type=Path, required=True)

    verify_run = subparsers.add_parser("verify-run")
    _add_identity_arguments(verify_run)
    verify_run.add_argument("--run-record", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    _add_identity_arguments(verify)
    verify.add_argument("--manifest", type=Path, required=True)

    create_request = subparsers.add_parser("create-request")
    create_request.add_argument("--repository", required=True)
    create_request.add_argument("--environment", required=True)
    create_request.add_argument("--manifest", type=Path, required=True)
    create_request.add_argument("--request-run-id", required=True)
    create_request.add_argument("--request-run-attempt", required=True)
    create_request.add_argument("--request-sha", required=True)
    create_request.add_argument("--dispatch-actor", required=True)
    create_request.add_argument("--output", type=Path, required=True)

    verify_request = subparsers.add_parser("verify-request")
    verify_request.add_argument("--repository", required=True)
    verify_request.add_argument("--environment", default="conan-development")
    verify_request.add_argument("--request", type=Path, required=True)
    return parser


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-run-attempt", required=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create-request":
        release = validate_manifest_for_repository(
            read_json(args.manifest), repository=args.repository
        )
        request = build_deployment_request(
            release=release,
            repository=args.repository,
            environment=args.environment,
            request_run_id=args.request_run_id,
            request_run_attempt=args.request_run_attempt,
            request_sha=args.request_sha,
            dispatch_actor=args.dispatch_actor,
        )
        write_manifest(args.output, request)
        return 0
    if args.command == "verify-request":
        request = validate_deployment_request(
            read_json(args.request),
            repository=args.repository,
            environment=args.environment,
        )
        print(request["release"]["image"])
        return 0
    identity = {
        "repository": args.repository,
        "source_sha": args.source_sha,
        "ci_run_id": args.ci_run_id,
        "ci_run_attempt": args.ci_run_attempt,
    }
    if args.command == "validate-identifiers":
        validate_identifiers(args.source_sha, args.ci_run_id, args.ci_run_attempt)
    elif args.command == "create":
        write_manifest(args.output, build_manifest(image=args.image, event=args.event, **identity))
    elif args.command == "verify-run":
        validate_workflow_run(read_json(args.run_record), **identity)
    elif args.command == "verify":
        value = validate_manifest(read_json(args.manifest), **identity)
        print(value["image"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        raise SystemExit(f"release manifest 검증 실패: {exc}") from exc
