"""배포 상태와 진행 중 transaction journal을 안전하게 영속화한다.

이 모듈은 배포 명령을 실행하지 않는다. 신뢰된 배포 컨트롤러가 사용하는 두
개의 작은 JSON 계약만 검증하고, 같은 디렉터리 안에서 원자적으로 교체한다.
상태 디렉터리는 컨트롤러 전용 계정 또는 root가 소유하고 있어야 한다.
"""

from __future__ import annotations

from datetime import datetime
import errno
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, TypedDict, cast

from scripts.secure_paths import (
    SecurePathError,
    canonical_absolute_path,
    open_trusted_directory,
)


SCHEMA_VERSION = 2
MAX_JSON_BYTES = 64 * 1024

JOURNAL_PHASES = frozenset({
    "prepared",
    "draining",
    "switching",
    "verifying",
    "rolling_back",
    "committed",
    "rolled_back",
    "aborted",
})
LAST_OUTCOMES = frozenset({"committed", "rolled_back", "aborted"})

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_MAX_IDENTITY = (1 << 63) - 1
_MAX_REFERENCE_LENGTH = 2048
_MAX_ERROR_LENGTH = 8192


class Identity(TypedDict):
    run_id: int
    run_attempt: int


class ActiveDeployment(TypedDict):
    image: str
    source_sha: str
    image_id: str
    deployment_id: str


class DeploymentState(TypedDict):
    schema_version: int
    target_id: str
    target_fingerprint: str
    active: ActiveDeployment | None
    highest_request: Identity
    highest_ci: Identity
    last_outcome: str | None
    updated_at: str


class DeploymentJournal(TypedDict):
    schema_version: int
    target_id: str
    target_fingerprint: str
    transaction_fingerprint: str
    transaction_id: str
    phase: str
    request_run_id: int
    request_run_attempt: int
    ci_run_id: int
    ci_run_attempt: int
    target_image: str
    target_source_sha: str
    target_image_id: str | None
    previous_image: str | None
    previous_image_id: str | None
    rollback_tag: str | None
    started_at: str
    updated_at: str
    error: str | None


class DeploymentStateError(ValueError):
    """상태 계약 또는 상태 파일의 보안 조건이 맞지 않을 때 발생한다."""


def _require_plain_object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise DeploymentStateError(f"{label}은 JSON 객체여야 한다")
    return cast(dict[str, Any], value)


def _require_exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    missing = expected - actual
    unknown = actual - expected
    if not missing and not unknown:
        return
    details: list[str] = []
    if missing:
        details.append(f"누락={','.join(sorted(missing))}")
    if unknown:
        details.append(f"미지원={','.join(sorted(unknown))}")
    raise DeploymentStateError(f"{label} 필드가 계약과 다르다: {'; '.join(details)}")


def _require_schema_version(value: Any, label: str) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise DeploymentStateError(f"{label} schema_version은 {SCHEMA_VERSION}이어야 한다")
    return value


def _require_safe_id(value: Any, label: str) -> str:
    if type(value) is not str or not _SAFE_ID_RE.fullmatch(value):
        raise DeploymentStateError(f"{label} 형식이 잘못됐다")
    return value


def _require_source_sha(value: Any, label: str) -> str:
    if type(value) is not str or not _SOURCE_SHA_RE.fullmatch(value):
        raise DeploymentStateError(f"{label}는 소문자 40자리 Git SHA여야 한다")
    return value


def _require_fingerprint(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise DeploymentStateError(f"{label}는 소문자 SHA-256이어야 한다")
    return value


def _require_positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_IDENTITY:
        raise DeploymentStateError(f"{label}는 범위 내 양의 정수여야 한다")
    return value


def _require_reference(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not value or len(value) > _MAX_REFERENCE_LENGTH:
        raise DeploymentStateError(f"{label}는 비어 있지 않은 제한 길이 문자열이어야 한다")
    if value != value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DeploymentStateError(f"{label}에 공백 여백이나 제어 문자를 사용할 수 없다")
    return value


def _parse_timestamp(value: Any, label: str) -> tuple[str, datetime]:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        raise DeploymentStateError(f"{label}는 timezone을 포함한 ISO 8601 시각이어야 한다")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DeploymentStateError(f"{label}가 유효한 시각이 아니다") from exc
    if parsed.utcoffset() is None:
        raise DeploymentStateError(f"{label}에 timezone이 없다")
    return value, parsed


def _validate_identity(value: Any, label: str) -> Identity:
    identity = _require_plain_object(value, label)
    _require_exact_keys(identity, frozenset({"run_id", "run_attempt"}), label)
    return {
        "run_id": _require_positive_integer(identity["run_id"], f"{label}.run_id"),
        "run_attempt": _require_positive_integer(
            identity["run_attempt"], f"{label}.run_attempt"
        ),
    }


def _validate_active(value: Any) -> ActiveDeployment | None:
    if value is None:
        return None
    active = _require_plain_object(value, "state.active")
    _require_exact_keys(
        active,
        frozenset({"image", "source_sha", "image_id", "deployment_id"}),
        "state.active",
    )
    return {
        "image": cast(str, _require_reference(active["image"], "state.active.image")),
        "source_sha": _require_source_sha(active["source_sha"], "state.active.source_sha"),
        "image_id": cast(str, _require_reference(active["image_id"], "state.active.image_id")),
        "deployment_id": _require_safe_id(
            active["deployment_id"], "state.active.deployment_id"
        ),
    }


def validate_state(
    value: Any,
    target_id: str | None = None,
    target_fingerprint: str | None = None,
) -> DeploymentState:
    """상태 객체를 exact schema로 검증하고 plain dict 사본을 반환한다."""

    state = _require_plain_object(value, "state")
    _require_exact_keys(
        state,
        frozenset({
            "schema_version",
            "target_id",
            "target_fingerprint",
            "active",
            "highest_request",
            "highest_ci",
            "last_outcome",
            "updated_at",
        }),
        "state",
    )
    expected_target = _require_safe_id(target_id, "target_id") if target_id is not None else None
    actual_target = _require_safe_id(state["target_id"], "state.target_id")
    if expected_target is not None and actual_target != expected_target:
        raise DeploymentStateError("state.target_id가 요청한 배포 대상과 다르다")
    actual_fingerprint = _require_fingerprint(
        state["target_fingerprint"], "state.target_fingerprint"
    )
    expected_fingerprint = (
        _require_fingerprint(target_fingerprint, "target_fingerprint")
        if target_fingerprint is not None
        else None
    )
    if expected_fingerprint is not None and actual_fingerprint != expected_fingerprint:
        raise DeploymentStateError("state가 현재 controller target 계약과 다르다")

    outcome = state["last_outcome"]
    if outcome is not None and (type(outcome) is not str or outcome not in LAST_OUTCOMES):
        raise DeploymentStateError("state.last_outcome이 지원하는 종료 상태가 아니다")
    updated_at, _ = _parse_timestamp(state["updated_at"], "state.updated_at")
    return {
        "schema_version": _require_schema_version(state["schema_version"], "state"),
        "target_id": actual_target,
        "target_fingerprint": actual_fingerprint,
        "active": _validate_active(state["active"]),
        "highest_request": _validate_identity(state["highest_request"], "state.highest_request"),
        "highest_ci": _validate_identity(state["highest_ci"], "state.highest_ci"),
        "last_outcome": cast(str | None, outcome),
        "updated_at": updated_at,
    }


def validate_journal(
    value: Any,
    target_id: str | None = None,
    target_fingerprint: str | None = None,
    transaction_fingerprint: str | None = None,
) -> DeploymentJournal:
    """transaction journal을 exact schema로 검증하고 plain dict 사본을 반환한다."""

    journal = _require_plain_object(value, "journal")
    _require_exact_keys(
        journal,
        frozenset({
            "schema_version",
            "target_id",
            "target_fingerprint",
            "transaction_fingerprint",
            "transaction_id",
            "phase",
            "request_run_id",
            "request_run_attempt",
            "ci_run_id",
            "ci_run_attempt",
            "target_image",
            "target_source_sha",
            "target_image_id",
            "previous_image",
            "previous_image_id",
            "rollback_tag",
            "started_at",
            "updated_at",
            "error",
        }),
        "journal",
    )
    expected_target = _require_safe_id(target_id, "target_id") if target_id is not None else None
    actual_target = _require_safe_id(journal["target_id"], "journal.target_id")
    if expected_target is not None and actual_target != expected_target:
        raise DeploymentStateError("journal.target_id가 요청한 배포 대상과 다르다")
    actual_target_fingerprint = _require_fingerprint(
        journal["target_fingerprint"], "journal.target_fingerprint"
    )
    expected_target_fingerprint = (
        _require_fingerprint(target_fingerprint, "target_fingerprint")
        if target_fingerprint is not None
        else None
    )
    if (
        expected_target_fingerprint is not None
        and actual_target_fingerprint != expected_target_fingerprint
    ):
        raise DeploymentStateError("journal이 현재 controller target identity와 다르다")
    actual_transaction_fingerprint = _require_fingerprint(
        journal["transaction_fingerprint"], "journal.transaction_fingerprint"
    )
    expected_transaction_fingerprint = (
        _require_fingerprint(transaction_fingerprint, "transaction_fingerprint")
        if transaction_fingerprint is not None
        else None
    )
    if (
        expected_transaction_fingerprint is not None
        and actual_transaction_fingerprint != expected_transaction_fingerprint
    ):
        raise DeploymentStateError("journal이 현재 transaction 계약과 다르다")

    phase = journal["phase"]
    if type(phase) is not str or phase not in JOURNAL_PHASES:
        raise DeploymentStateError("journal.phase가 지원하는 transaction 단계가 아니다")
    error = journal["error"]
    if error is not None and (type(error) is not str or len(error) > _MAX_ERROR_LENGTH):
        raise DeploymentStateError("journal.error는 null 또는 제한 길이 문자열이어야 한다")
    started_at, started = _parse_timestamp(journal["started_at"], "journal.started_at")
    updated_at, updated = _parse_timestamp(journal["updated_at"], "journal.updated_at")
    if updated < started:
        raise DeploymentStateError("journal.updated_at은 started_at보다 빠를 수 없다")

    return {
        "schema_version": _require_schema_version(journal["schema_version"], "journal"),
        "target_id": actual_target,
        "target_fingerprint": actual_target_fingerprint,
        "transaction_fingerprint": actual_transaction_fingerprint,
        "transaction_id": _require_safe_id(
            journal["transaction_id"], "journal.transaction_id"
        ),
        "phase": phase,
        "request_run_id": _require_positive_integer(
            journal["request_run_id"], "journal.request_run_id"
        ),
        "request_run_attempt": _require_positive_integer(
            journal["request_run_attempt"], "journal.request_run_attempt"
        ),
        "ci_run_id": _require_positive_integer(journal["ci_run_id"], "journal.ci_run_id"),
        "ci_run_attempt": _require_positive_integer(
            journal["ci_run_attempt"], "journal.ci_run_attempt"
        ),
        "target_image": cast(
            str, _require_reference(journal["target_image"], "journal.target_image")
        ),
        "target_source_sha": _require_source_sha(
            journal["target_source_sha"], "journal.target_source_sha"
        ),
        "target_image_id": _require_reference(
            journal["target_image_id"], "journal.target_image_id", optional=True
        ),
        "previous_image": _require_reference(
            journal["previous_image"], "journal.previous_image", optional=True
        ),
        "previous_image_id": _require_reference(
            journal["previous_image_id"], "journal.previous_image_id", optional=True
        ),
        "rollback_tag": _require_reference(
            journal["rollback_tag"], "journal.rollback_tag", optional=True
        ),
        "started_at": started_at,
        "updated_at": updated_at,
        "error": cast(str | None, error),
    }


def _identity_from(value: Any, *, kind: str) -> tuple[int, int]:
    source = _require_plain_object(value, f"{kind} identity source")
    direct_run = f"{kind}_run_id"
    direct_attempt = f"{kind}_run_attempt"
    highest = f"highest_{kind}"
    if direct_run in source or direct_attempt in source:
        if direct_run not in source or direct_attempt not in source:
            raise DeploymentStateError(f"{kind} identity 필드가 일부만 있다")
        return (
            _require_positive_integer(source[direct_run], direct_run),
            _require_positive_integer(source[direct_attempt], direct_attempt),
        )
    if highest in source:
        identity = _validate_identity(source[highest], highest)
        return identity["run_id"], identity["run_attempt"]
    if kind == "ci" and "release" in source:
        return _identity_from(source["release"], kind=kind)
    if "run_id" in source or "run_attempt" in source:
        identity = _validate_identity(source, f"{kind} identity")
        return identity["run_id"], identity["run_attempt"]
    raise DeploymentStateError(f"{kind} identity 필드가 없다")


def request_tuple(value: Any) -> tuple[int, int]:
    """요청 객체, journal 또는 state의 request identity를 비교 가능한 tuple로 만든다."""

    return _identity_from(value, kind="request")


def ci_tuple(value: Any) -> tuple[int, int]:
    """release, 요청 객체, journal 또는 state의 CI identity tuple을 만든다."""

    return _identity_from(value, kind="ci")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentStateError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def _allowed_owner(owner: int) -> bool:
    return owner in {0, os.geteuid()}


def _state_path(path: str | os.PathLike[str]) -> Path:
    try:
        return canonical_absolute_path(path)
    except SecurePathError as exc:
        raise DeploymentStateError(f"상태 경로는 canonical 절대 경로여야 한다: {exc}") from exc


def _open_parent(path: Path) -> int:
    parent = path.parent
    try:
        # State writes need an owner-only leaf directory.  The shared helper
        # also verifies every ancestor from / with openat + O_NOFOLLOW.
        descriptor = open_trusted_directory(parent, private=True)
    except SecurePathError as exc:
        raise DeploymentStateError(
            f"상태 디렉터리를 안전하게 열지 못했다: {parent}: {exc}"
        ) from exc
    return descriptor


def _validate_file_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise DeploymentStateError("상태 파일은 단일 hard link의 regular file이어야 한다")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DeploymentStateError("상태 파일 권한은 정확히 0600이어야 한다")
    if not _allowed_owner(metadata.st_uid):
        raise DeploymentStateError("상태 파일 소유자가 root 또는 실행 계정이 아니다")


def _read_json(path: Path) -> dict[str, Any] | None:
    parent_descriptor = _open_parent(path)
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DeploymentStateError(f"상태 파일을 안전하게 열지 못했다: {path}") from exc

        metadata = os.fstat(descriptor)
        _validate_file_metadata(metadata)
        if metadata.st_size > MAX_JSON_BYTES:
            raise DeploymentStateError("상태 JSON이 최대 크기를 초과했다")

        chunks: list[bytes] = []
        remaining = MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_JSON_BYTES:
            raise DeploymentStateError("상태 JSON이 최대 크기를 초과했다")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)

    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_object_without_duplicate_keys)
    except DeploymentStateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise DeploymentStateError(f"상태 JSON을 해석하지 못했다: {path}") from exc
    return _require_plain_object(value, "상태 JSON 최상위 값")


def _existing_file_metadata(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DeploymentStateError("기존 상태 파일 정보를 확인하지 못했다") from exc
    _validate_file_metadata(metadata)
    return metadata


def _serialize_json(value: dict[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeploymentStateError("상태 값을 JSON으로 직렬화하지 못했다") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise DeploymentStateError("상태 JSON이 최대 크기를 초과했다")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "상태 파일 쓰기가 진행되지 않았다")
        view = view[written:]


def _atomic_write_payload(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_JSON_BYTES:
        raise DeploymentStateError("원자적으로 쓸 파일이 최대 크기를 초과했다")
    parent_descriptor = _open_parent(path)
    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    replaced = False
    try:
        _existing_file_metadata(parent_descriptor, path.name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        for _ in range(16):
            candidate = f".deployment-state-{os.getpid()}-{secrets.token_hex(12)}.tmp"
            try:
                temporary_descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise DeploymentStateError("임시 상태 파일을 만들지 못했다") from exc
            temporary_name = candidate
            break
        if temporary_descriptor is None or temporary_name is None:
            raise DeploymentStateError("충돌하지 않는 임시 상태 파일 이름을 만들지 못했다")

        try:
            os.fchmod(temporary_descriptor, 0o600)
            _write_all(temporary_descriptor, payload)
            os.fsync(temporary_descriptor)
        except OSError as exc:
            raise DeploymentStateError("임시 상태 파일을 durable하게 기록하지 못했다") from exc
        finally:
            os.close(temporary_descriptor)
            temporary_descriptor = None

        # 생성 이후 기존 파일이 바뀌었어도 비정상 유형/권한을 덮어쓰지 않는다.
        _existing_file_metadata(parent_descriptor, path.name)
        try:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            replaced = True
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise DeploymentStateError("상태 파일을 원자적으로 교체하지 못했다") from exc
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None and not replaced:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """작은 비밀 없는 설정 파일을 상태 파일과 같은 방식으로 durable하게 쓴다.

    ``image.env``처럼 컨트롤러가 이미 내용 계약을 검증한 파일을 위한 API다.
    호출자가 넘긴 타입을 암묵적으로 변환하지 않으며 JSON과 같은 크기 제한을
    적용한다.
    """

    if type(data) is not bytes:
        raise DeploymentStateError("원자 쓰기 데이터는 bytes여야 한다")
    _atomic_write_payload(_state_path(path), data)


def load_state(
    path: str | os.PathLike[str],
    target_id: str,
    target_fingerprint: str | None = None,
) -> DeploymentState | None:
    """상태를 읽는다. 안전하게 검증된 파일이 없을 때만 ``None``을 반환한다."""

    state = _read_json(_state_path(path))
    if state is None:
        return None
    return validate_state(state, target_id, target_fingerprint)


def write_state(
    path: str | os.PathLike[str],
    value: Any,
    target_id: str | None = None,
    target_fingerprint: str | None = None,
) -> None:
    """검증된 상태를 mode 0600 파일로 원자적·durable하게 기록한다."""

    _atomic_write_payload(
        _state_path(path),
        _serialize_json(validate_state(value, target_id, target_fingerprint)),
    )


def load_journal(
    path: str | os.PathLike[str],
    target_id: str,
    target_fingerprint: str | None = None,
    transaction_fingerprint: str | None = None,
) -> DeploymentJournal | None:
    """transaction journal을 읽고 대상까지 검증한다."""

    journal = _read_json(_state_path(path))
    if journal is None:
        return None
    return validate_journal(
        journal, target_id, target_fingerprint, transaction_fingerprint
    )


def write_journal(
    path: str | os.PathLike[str],
    value: Any,
    target_id: str | None = None,
    target_fingerprint: str | None = None,
    transaction_fingerprint: str | None = None,
) -> None:
    """검증된 transaction journal을 원자적·durable하게 기록한다."""

    _atomic_write_payload(
        _state_path(path),
        _serialize_json(
            validate_journal(
                value, target_id, target_fingerprint, transaction_fingerprint
            )
        ),
    )
