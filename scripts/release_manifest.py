"""CI release와 승인된 개발계 배포 요청의 엄격한 JSON 계약.

이 모듈은 이미지를 빌드하거나 서버를 변경하지 않는다. GitHub Actions가 만든
release/request의 식별자와 불변 digest를 연결하고, 호스트가 attestation 검증 전에
거부해야 할 형식·대상·유효시간을 fail-closed로 검사한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any
import uuid


SCHEMA_VERSION = 2
REQUEST_SCHEMA_VERSION = 3
WORKFLOW_PATH = ".github/workflows/backend-ci.yml"
DEPLOYMENT_WORKFLOW_PATH = ".github/workflows/dev-deployment.yml"
REQUEST_TTL_SECONDS = 60 * 60
REQUEST_CLOCK_SKEW_SECONDS = 60
MAX_JSON_BYTES = 64 * 1024

SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9-]{1,64}(\[bot\])?$")


class ManifestError(ValueError):
    """배포 근거가 기대한 계약과 다를 때 발생한다."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def _read_bytes(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    try:
        with path.open("rb") as stream:
            value = stream.read(max_bytes + 1)
    except OSError as exc:
        raise ManifestError(f"JSON을 읽지 못했다: {path}") from exc
    if len(value) > max_bytes:
        raise ManifestError(f"JSON 크기가 {max_bytes}바이트 상한을 넘었다")
    return value


def _decode_json(raw: bytes, *, path: Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        label = f": {path}" if path is not None else ""
        raise ManifestError(f"JSON을 읽지 못했다{label}") from exc
    if not isinstance(value, dict):
        raise ManifestError("JSON 최상위 값은 객체여야 한다")
    return value


def read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    return _decode_json(_read_bytes(path, max_bytes=max_bytes), path=path)


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """Workflow와 host가 공유하는 결정적 직렬화 형식."""
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def read_canonical_json(path: Path) -> dict[str, Any]:
    raw = _read_bytes(path)
    # 한 번 읽은 동일 byte snapshot을 해석하고 canonical 여부도 확인한다. 두 번
    # open하면 중간 파일 교체가 서로 다른 내용을 검증 대상으로 만들 수 있다.
    value = _decode_json(raw, path=path)
    if raw != canonical_json_bytes(value):
        raise ManifestError("JSON이 승인된 canonical 직렬화 형식이 아니다")
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


def _positive_integer(value: str | int, label: str) -> int:
    if type(value) is int:
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ManifestError(f"{label}는 양의 정수여야 한다")
    if not RUN_ID_RE.fullmatch(text):
        raise ManifestError(f"{label}는 양의 정수여야 한다")
    return int(text)


def validate_repository_identity(
    repository: str, repository_id: str | int, repository_owner_id: str | int
) -> tuple[int, int]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ManifestError("repository는 owner/name 형식이어야 한다")
    return (
        _positive_integer(repository_id, "repository_id"),
        _positive_integer(repository_owner_id, "repository_owner_id"),
    )


def validate_identifiers(source_sha: str, ci_run_id: str, ci_run_attempt: str) -> None:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ManifestError("source_sha는 소문자 40자리 Git SHA여야 한다")
    _positive_integer(ci_run_id, "ci_run_id")
    _positive_integer(ci_run_attempt, "ci_run_attempt")


def expected_image_repository(repository: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ManifestError("repository는 owner/name 형식이어야 한다")
    return f"ghcr.io/{repository.lower()}"


def validate_image_reference(image: str, repository: str) -> tuple[str, str]:
    prefix = f"{expected_image_repository(repository)}@"
    if not isinstance(image, str) or not image.startswith(prefix):
        raise ManifestError(f"이미지는 {prefix}sha256:<64자리> 형식이어야 한다")
    digest = image[len(prefix):]
    if not DIGEST_RE.fullmatch(digest):
        raise ManifestError("이미지 digest가 sha256 소문자 64자리 형식이 아니다")
    return prefix[:-1], digest


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label}이 비어 있다")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{label}이 ISO-8601 형식이 아니다") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError(f"{label}에 timezone이 없다")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def build_manifest(
    *,
    repository: str,
    repository_id: str | int,
    repository_owner_id: str | int,
    source_sha: str,
    ci_run_id: str,
    ci_run_attempt: str,
    image: str,
    event: str = "push",
    created_at: str | None = None,
) -> dict[str, Any]:
    repository_id_value, owner_id_value = validate_repository_identity(
        repository, repository_id, repository_owner_id
    )
    validate_identifiers(source_sha, ci_run_id, ci_run_attempt)
    if event not in {"push", "workflow_dispatch"}:
        raise ManifestError("지원하지 않는 CI event다")
    image_repository, digest = validate_image_reference(image, repository)
    timestamp = created_at or _timestamp(datetime.now(timezone.utc))
    _parse_timestamp(timestamp, "created_at")
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "repository_id": repository_id_value,
        "repository_owner_id": owner_id_value,
        "source_sha": source_sha,
        "ci_run_id": int(ci_run_id),
        "ci_run_attempt": int(ci_run_attempt),
        "workflow_path": WORKFLOW_PATH,
        "event": event,
        "image_repository": image_repository,
        "image_digest": digest,
        "image": image,
        "created_at": timestamp,
    }


def validate_manifest(
    value: dict[str, Any],
    *,
    repository: str,
    repository_id: str | int,
    repository_owner_id: str | int,
    source_sha: str,
    ci_run_id: str,
    ci_run_attempt: str,
) -> dict[str, Any]:
    repository_id_value, owner_id_value = validate_repository_identity(
        repository, repository_id, repository_owner_id
    )
    validate_identifiers(source_sha, ci_run_id, ci_run_attempt)
    _require_exact_keys(value, {
        "schema_version", "repository", "repository_id", "repository_owner_id",
        "source_sha", "ci_run_id", "ci_run_attempt", "workflow_path", "event",
        "image_repository", "image_digest", "image", "created_at",
    }, "release manifest")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "repository_id": repository_id_value,
        "repository_owner_id": owner_id_value,
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
    _parse_timestamp(value.get("created_at"), "created_at")
    return value


def validate_manifest_for_repository(
    value: dict[str, Any],
    *,
    repository: str,
    repository_id: str | int,
    repository_owner_id: str | int,
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
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        source_sha=source_sha,
        ci_run_id=str(ci_run_id),
        ci_run_attempt=str(ci_run_attempt),
    )


def build_deployment_request(
    *,
    release: dict[str, Any],
    target_id: str,
    repository: str,
    repository_id: str | int,
    repository_owner_id: str | int,
    environment: str,
    request_run_id: str,
    request_run_attempt: str,
    request_sha: str,
    dispatch_actor: str,
    dispatch_actor_id: str | int,
    request_id: str | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    repository_id_value, owner_id_value = validate_repository_identity(
        repository, repository_id, repository_owner_id
    )
    validate_manifest_for_repository(
        release,
        repository=repository,
        repository_id=repository_id_value,
        repository_owner_id=owner_id_value,
    )
    if not TARGET_ID_RE.fullmatch(target_id):
        raise ManifestError("target_id가 안전한 식별자 형식이 아니다")
    if environment != target_id:
        raise ManifestError("environment와 target_id는 정확히 같아야 한다")
    validate_identifiers(request_sha, request_run_id, request_run_attempt)
    if _positive_integer(request_run_attempt, "request_run_attempt") != 1:
        raise ManifestError(
            "Environment 승인 기록은 attempt별로 구분되지 않아 request_run_attempt는 1이어야 한다"
        )
    if release["source_sha"] != request_sha:
        raise ManifestError("배포 release는 승인 workflow의 exact main SHA여야 한다")
    if not ACTOR_RE.fullmatch(dispatch_actor):
        raise ManifestError("dispatch_actor가 GitHub actor 형식이 아니다")
    dispatch_actor_id_value = _positive_integer(
        dispatch_actor_id, "dispatch_actor_id"
    )

    request_id_value = request_id or str(uuid.uuid4())
    try:
        if str(uuid.UUID(request_id_value)) != request_id_value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ManifestError("request_id가 canonical UUID 형식이 아니다") from exc

    issued = _parse_timestamp(
        issued_at or _timestamp(datetime.now(timezone.utc)), "issued_at"
    )
    expires = _parse_timestamp(
        expires_at or _timestamp(issued + timedelta(seconds=REQUEST_TTL_SECONDS)),
        "expires_at",
    )
    _validate_request_window(issued, expires)
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id_value,
        "target_id": target_id,
        "operation": "deploy",
        "repository": repository,
        "repository_id": repository_id_value,
        "repository_owner_id": owner_id_value,
        "environment": environment,
        "workflow_path": DEPLOYMENT_WORKFLOW_PATH,
        "request_ref": "refs/heads/main",
        "request_sha": request_sha,
        "request_run_id": int(request_run_id),
        "request_run_attempt": int(request_run_attempt),
        "dispatch_actor": dispatch_actor,
        "dispatch_actor_id": dispatch_actor_id_value,
        "issued_at": _timestamp(issued),
        "expires_at": _timestamp(expires),
        "release": release,
    }


def _validate_request_window(issued: datetime, expires: datetime) -> None:
    lifetime = (expires - issued).total_seconds()
    if lifetime <= 0 or lifetime > REQUEST_TTL_SECONDS:
        raise ManifestError("배포 요청 유효시간이 허용 범위 밖이다")


def validate_deployment_request(
    value: dict[str, Any],
    *,
    target_id: str,
    repository: str,
    repository_id: str | int,
    repository_owner_id: str | int,
    environment: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    repository_id_value, owner_id_value = validate_repository_identity(
        repository, repository_id, repository_owner_id
    )
    _require_exact_keys(value, {
        "schema_version", "request_id", "target_id", "operation", "repository",
        "repository_id", "repository_owner_id", "environment", "workflow_path",
        "request_ref", "request_sha", "request_run_id", "request_run_attempt",
        "dispatch_actor", "dispatch_actor_id", "issued_at", "expires_at", "release",
    }, "deployment request")
    expected = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "target_id": target_id,
        "operation": "deploy",
        "repository": repository,
        "repository_id": repository_id_value,
        "repository_owner_id": owner_id_value,
        "environment": environment,
        "workflow_path": DEPLOYMENT_WORKFLOW_PATH,
        "request_ref": "refs/heads/main",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ManifestError(f"deployment request의 {key} 값이 기대값과 다르다")
    if environment != target_id:
        raise ManifestError("environment와 target_id가 일치하지 않는다")

    request_id = value.get("request_id")
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ManifestError("request_id가 canonical UUID 형식이 아니다") from exc

    request_sha = value.get("request_sha")
    run_id = value.get("request_run_id")
    run_attempt = value.get("request_run_attempt")
    if not isinstance(request_sha, str) or type(run_id) is not int or type(run_attempt) is not int:
        raise ManifestError("deployment request 식별자 형식이 잘못됐다")
    validate_identifiers(request_sha, str(run_id), str(run_attempt))
    if run_attempt != 1:
        raise ManifestError(
            "Environment 승인 기록은 attempt별로 구분되지 않아 request_run_attempt는 1이어야 한다"
        )
    dispatch_actor = value.get("dispatch_actor")
    if not isinstance(dispatch_actor, str) or not ACTOR_RE.fullmatch(dispatch_actor):
        raise ManifestError("dispatch_actor가 GitHub actor 형식이 아니다")
    _positive_integer(value.get("dispatch_actor_id"), "dispatch_actor_id")

    issued = _parse_timestamp(value.get("issued_at"), "issued_at")
    expires = _parse_timestamp(value.get("expires_at"), "expires_at")
    _validate_request_window(issued, expires)
    if now is not None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ManifestError("현재 시각에 timezone이 없다")
        current = now.astimezone(timezone.utc)
        if issued > current + timedelta(seconds=REQUEST_CLOCK_SKEW_SECONDS):
            raise ManifestError("배포 요청 issued_at이 현재보다 미래다")
        if expires < current:
            raise ManifestError("배포 요청이 만료됐다")

    release = value.get("release")
    if not isinstance(release, dict):
        raise ManifestError("deployment request release가 객체가 아니다")
    validate_manifest_for_repository(
        release,
        repository=repository,
        repository_id=repository_id_value,
        repository_owner_id=owner_id_value,
    )
    if release["source_sha"] != request_sha:
        raise ManifestError("release source SHA와 승인 workflow SHA가 다르다")
    return value


def validate_workflow_run(
    value: dict[str, Any],
    *,
    repository: str,
    repository_id: str | int,
    repository_owner_id: str | int,
    source_sha: str,
    ci_run_id: str,
    ci_run_attempt: str,
) -> None:
    repository_id_value, owner_id_value = validate_repository_identity(
        repository, repository_id, repository_owner_id
    )
    validate_identifiers(source_sha, ci_run_id, ci_run_attempt)
    checks = {
        "id": int(ci_run_id),
        "run_attempt": int(ci_run_attempt),
        "head_branch": "main",
        "head_sha": source_sha,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    for key, expected_value in checks.items():
        if value.get(key) != expected_value:
            raise ManifestError(f"CI run의 {key} 값이 기대값과 다르다")
    if value.get("path") not in {WORKFLOW_PATH, f"{WORKFLOW_PATH}@main"}:
        raise ManifestError("CI run의 path 값이 기대값과 다르다")
    run_repository = value.get("repository")
    if not isinstance(run_repository, dict):
        raise ManifestError("CI run 저장소 정보가 없다")
    owner = run_repository.get("owner")
    if (
        run_repository.get("full_name") != repository
        or run_repository.get("id") != repository_id_value
        or not isinstance(owner, dict)
        or owner.get("id") != owner_id_value
    ):
        raise ManifestError("CI run 저장소의 이름 또는 immutable ID가 기대값과 다르다")


def write_manifest(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


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
    _add_repository_arguments(create_request)
    create_request.add_argument("--target-id", required=True)
    create_request.add_argument("--environment", required=True)
    create_request.add_argument("--manifest", type=Path, required=True)
    create_request.add_argument("--request-run-id", required=True)
    create_request.add_argument("--request-run-attempt", required=True)
    create_request.add_argument("--request-sha", required=True)
    create_request.add_argument("--dispatch-actor", required=True)
    create_request.add_argument("--dispatch-actor-id", required=True)
    create_request.add_argument("--output", type=Path, required=True)

    verify_request = subparsers.add_parser("verify-request")
    _add_repository_arguments(verify_request)
    verify_request.add_argument("--target-id", required=True)
    verify_request.add_argument("--environment", required=True)
    verify_request.add_argument("--request", type=Path, required=True)
    return parser


def _add_repository_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--repository-owner-id", required=True)


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    _add_repository_arguments(parser)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-run-attempt", required=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository_identity = {
        "repository": args.repository,
        "repository_id": args.repository_id,
        "repository_owner_id": args.repository_owner_id,
    }
    if args.command == "create-request":
        release = validate_manifest_for_repository(
            read_json(args.manifest), **repository_identity
        )
        request = build_deployment_request(
            release=release,
            target_id=args.target_id,
            environment=args.environment,
            request_run_id=args.request_run_id,
            request_run_attempt=args.request_run_attempt,
            request_sha=args.request_sha,
            dispatch_actor=args.dispatch_actor,
            dispatch_actor_id=args.dispatch_actor_id,
            **repository_identity,
        )
        write_manifest(args.output, request)
        return 0
    if args.command == "verify-request":
        request = validate_deployment_request(
            read_canonical_json(args.request),
            target_id=args.target_id,
            environment=args.environment,
            now=datetime.now(timezone.utc),
            **repository_identity,
        )
        print(request["release"]["image"])
        return 0
    identity = {
        **repository_identity,
        "source_sha": args.source_sha,
        "ci_run_id": args.ci_run_id,
        "ci_run_attempt": args.ci_run_attempt,
    }
    if args.command == "validate-identifiers":
        validate_repository_identity(**repository_identity)
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
