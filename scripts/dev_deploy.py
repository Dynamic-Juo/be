"""서명된 개발계 배포 요청을 한 Compose 서비스에 안전하게 적용한다.

GitHub Actions는 서버 명령을 실행하지 않는다. 이 호스트 helper가 GitHub-hosted
workflow의 Sigstore bundle 세 개를 검증하고, 로컬 allowlist에 고정된
Compose 서비스만 digest로 교체한다. 모든 mutation은 durable journal 뒤에 수행하며
중단된 transaction은 ``recover``가 commit 지점을 기준으로 수렴시킨다.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import attestation, github_approval  # noqa: E402
from scripts.deployment_state import (  # noqa: E402
    SCHEMA_VERSION as DEPLOYMENT_STATE_SCHEMA_VERSION,
    DeploymentStateError,
    atomic_write_bytes,
    ci_tuple,
    load_journal,
    load_state,
    request_tuple,
    write_journal,
    write_state,
)
from scripts.release_manifest import (  # noqa: E402
    DEPLOYMENT_WORKFLOW_PATH,
    WORKFLOW_PATH,
    ManifestError,
    canonical_json_bytes,
    validate_deployment_request,
    validate_image_reference,
    validate_manifest_for_repository,
    validate_repository_identity,
)
from scripts.secure_paths import (  # noqa: E402
    SecurePathError,
    open_trusted_directory,
    open_trusted_parent,
    read_trusted_regular_file,
    sha256_trusted_regular_file,
    stat_trusted_regular_file,
    stat_trusted_unix_socket,
    trusted_path_identity,
)


CONFIG_SCHEMA_VERSION = 4
MAX_EXTERNAL_OUTPUT_BYTES = 1024 * 1024
EXPECTED_PLATFORM = "linux/arm64"
DURABLE_ADMISSION_PROTOCOL = "durable-api-drain-v1"
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_PROBE = (
    "import json,urllib.request,sys;"
    "data=json.load(urllib.request.urlopen(sys.argv[1],timeout=5));"
    "print(json.dumps(data,separators=(',',':')))"
)
_CONTROL = (
    "import json,os,sys,urllib.request;"
    "token=os.environ.get('DEEPCHECK_DEPLOYMENT_TOKEN');"
    "assert token,'deployment control unavailable';"
    "body=json.dumps({'deployment_id':sys.argv[2]},separators=(',',':')).encode();"
    "req=urllib.request.Request('http://127.0.0.1:8000/internal/deployment/'+sys.argv[1],"
    "data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},"
    "method='POST');"
    "data=json.load(urllib.request.urlopen(req,timeout=5));"
    "print(json.dumps(data,separators=(',',':')))"
)


class DeploymentError(RuntimeError):
    """신뢰 조건, 배포 또는 복구 검증이 충족되지 않았을 때 발생한다."""


class _ConfirmedDrainingError(DeploymentError):
    """동일 image 서비스가 durable drain 상태임을 완전히 증명했을 때만 사용한다."""


@dataclass(frozen=True)
class TrustedFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class BootstrapActive:
    image: str
    source_sha: str
    image_id: str
    deployment_id: str
    request_run_id: int
    request_run_attempt: int
    ci_run_id: int
    ci_run_attempt: int

    def as_state_value(self) -> dict[str, str]:
        return {
            "image": self.image,
            "source_sha": self.source_sha,
            "image_id": self.image_id,
            "deployment_id": self.deployment_id,
        }


@dataclass(frozen=True)
class DeploymentConfig:
    enabled: bool
    target_id: str
    environment: str
    repository: str
    repository_id: int
    repository_owner_id: int
    backend_ci_workflow_id: int
    backend_ci_workflow_sha256: str
    deployment_workflow_id: int
    deployment_workflow_sha256: str
    environment_id: int
    allowed_reviewer_user_ids: frozenset[int]
    request_authentication: str
    gh_binary: Path
    gh_binary_sha256: str
    gh_config_dir: Path
    docker_binary: Path
    docker_binary_sha256: str
    compose_binary: Path
    compose_binary_sha256: str
    docker_config_dir: Path
    docker_context: str
    docker_endpoint: str
    project: str
    compose_file: Path
    trusted_compose_files: tuple[TrustedFile, ...]
    env_file: Path
    service: str
    global_lock_file: Path
    state_dir: Path
    bootstrap_active: BootstrapActive | None
    admission_control: str
    idle_checks: int
    idle_timeout_seconds: int
    idle_interval_seconds: float
    health_timeout_seconds: int
    health_interval_seconds: float
    stop_timeout_seconds: int
    command_timeout_seconds: int
    mutation_settle_seconds: float
    mutation_poll_seconds: float

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def journal_file(self) -> Path:
        return self.state_dir / "journal.json"

    @property
    def desired_image_env(self) -> Path:
        return self.state_dir / "image.env"

    @property
    def target_fingerprint(self) -> str:
        """명시적 migration 없이는 바꿀 수 없는 장기 배포 target identity."""

        contract = {
            "schema": "conan-development-controller-target-v3",
            "target_id": self.target_id,
            "environment": self.environment,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "repository_owner_id": self.repository_owner_id,
            "backend_ci_workflow_id": self.backend_ci_workflow_id,
            "deployment_workflow_id": self.deployment_workflow_id,
            "environment_id": self.environment_id,
            "docker_endpoint": self.docker_endpoint,
            "project": self.project,
            "service": self.service,
            "global_lock_file": str(self.global_lock_file),
            "state_dir": str(self.state_dir),
            "admission_control": self.admission_control,
            "required_admission_protocol": DURABLE_ADMISSION_PROTOCOL,
        }
        return _canonical_fingerprint(contract)

    @property
    def transaction_fingerprint(self) -> str:
        """진행 중 transaction 복구에 필요한 교체 도구·파일 계약."""

        trusted_files = sorted(
            (
                {"path": str(item.path), "sha256": item.sha256}
                for item in self.trusted_compose_files
            ),
            key=lambda item: (item["path"], item["sha256"]),
        )
        contract = {
            "schema": "conan-development-controller-transaction-v1",
            "docker_binary_path": str(self.docker_binary),
            "docker_binary_sha256": self.docker_binary_sha256,
            "compose_binary_path": str(self.compose_binary),
            "compose_binary_sha256": self.compose_binary_sha256,
            "docker_config_dir": str(self.docker_config_dir),
            "docker_context": self.docker_context,
            "compose_file": str(self.compose_file),
            "trusted_compose_files": trusted_files,
            "env_file": str(self.env_file),
        }
        return _canonical_fingerprint(contract)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeploymentConfig":
        expected = {
            "schema_version", "enabled", "target_id", "environment", "repository",
            "repository_id", "repository_owner_id", "backend_ci_workflow_id",
            "backend_ci_workflow_sha256", "deployment_workflow_id",
            "deployment_workflow_sha256", "environment_id",
            "allowed_reviewer_user_ids", "request_authentication",
            "gh_binary", "gh_binary_sha256", "gh_config_dir", "docker_binary",
            "docker_binary_sha256", "compose_binary", "compose_binary_sha256",
            "docker_config_dir", "docker_context",
            "docker_endpoint", "project", "compose_file", "trusted_compose_files",
            "env_file", "service", "global_lock_file", "state_dir",
            "bootstrap_active", "admission_control", "idle_checks",
            "idle_timeout_seconds", "idle_interval_seconds", "health_timeout_seconds",
            "health_interval_seconds", "stop_timeout_seconds", "command_timeout_seconds",
            "mutation_settle_seconds", "mutation_poll_seconds",
        }
        if type(value) is not dict:
            raise DeploymentError("호스트 설정은 JSON 객체여야 한다")
        missing = expected - value.keys()
        unknown = value.keys() - expected
        if missing or unknown:
            raise DeploymentError(
                f"호스트 설정 필드가 계약과 다르다: 누락={sorted(missing)}, "
                f"미지원={sorted(unknown)}"
            )
        if value["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise DeploymentError("지원하지 않는 호스트 설정 schema_version이다")
        if type(value["enabled"]) is not bool:
            raise DeploymentError("enabled는 boolean이어야 한다")

        target_id = _safe_name(value["target_id"], "target_id")
        environment = _safe_name(value["environment"], "environment")
        if target_id != environment:
            raise DeploymentError("target_id와 environment는 정확히 같아야 한다")
        repository = value["repository"]
        if type(repository) is not str:
            raise DeploymentError("repository는 문자열이어야 한다")
        try:
            repository_id, owner_id = validate_repository_identity(
                repository, value["repository_id"], value["repository_owner_id"]
            )
        except ManifestError as exc:
            raise DeploymentError(str(exc)) from exc
        immutable_ids: dict[str, int] = {}
        for key in (
            "backend_ci_workflow_id", "deployment_workflow_id", "environment_id"
        ):
            item = value[key]
            if type(item) is not int or not 1 <= item < 1 << 63:
                raise DeploymentError(f"{key}는 64-bit 범위의 양의 정수여야 한다")
            immutable_ids[key] = item
        raw_reviewers = value["allowed_reviewer_user_ids"]
        if type(raw_reviewers) is not list or not 1 <= len(raw_reviewers) <= 6:
            raise DeploymentError("allowed_reviewer_user_ids는 1~6개 ID 배열이어야 한다")
        if any(type(item) is not int or not 1 <= item < 1 << 63 for item in raw_reviewers):
            raise DeploymentError("allowed_reviewer_user_ids는 양의 64-bit 정수만 허용한다")
        allowed_reviewer_user_ids = frozenset(raw_reviewers)
        if len(allowed_reviewer_user_ids) != len(raw_reviewers):
            raise DeploymentError("allowed_reviewer_user_ids에 중복 ID가 있다")
        if value["request_authentication"] != (
            "github-image-and-manifest-attestations-ci-and-environment-review-v3"
        ):
            raise DeploymentError(
                "request_authentication은 "
                "github-image-and-manifest-attestations-ci-and-environment-review-v3"
                "여야 한다"
            )
        if value["admission_control"] != DURABLE_ADMISSION_PROTOCOL:
            raise DeploymentError(
                f"admission_control은 {DURABLE_ADMISSION_PROTOCOL}이어야 한다"
            )

        paths = {
            key: _absolute_path(value[key], key)
            for key in (
                "gh_binary", "gh_config_dir", "docker_binary", "compose_binary",
                "docker_config_dir",
                "compose_file", "env_file", "global_lock_file", "state_dir",
            )
        }
        hashes = {
            key: _sha256(value[key], key)
            for key in (
                "backend_ci_workflow_sha256", "gh_binary_sha256",
                "docker_binary_sha256", "compose_binary_sha256"
            )
        }
        trusted = _trusted_files(value["trusted_compose_files"])
        if sum(item.path == paths["compose_file"] for item in trusted) != 1:
            raise DeploymentError("compose_file은 trusted_compose_files에 정확히 한 번 있어야 한다")

        for key in ("docker_context", "project", "service"):
            _safe_name(value[key], key)
        endpoint = value["docker_endpoint"]
        if type(endpoint) is not str or not re.fullmatch(
            r"unix:///[A-Za-z0-9_./ -]{1,1900}", endpoint
        ):
            raise DeploymentError("docker_endpoint는 승인된 절대 unix socket이어야 한다")
        endpoint_path_text = endpoint.removeprefix("unix://")
        endpoint_path = Path(endpoint_path_text)
        if (
            endpoint_path.anchor != "/"
            or ".." in endpoint_path.parts
            or str(endpoint_path) != endpoint_path_text
        ):
            raise DeploymentError(
                "docker_endpoint는 alias·상위 이동 없는 canonical socket 경로여야 한다"
            )

        integers = {
            "idle_checks": (2, 20),
            "idle_timeout_seconds": (10, 7200),
            "health_timeout_seconds": (10, 1800),
            "stop_timeout_seconds": (10, 1800),
            "command_timeout_seconds": (30, 3600),
        }
        for key, (minimum, maximum) in integers.items():
            if type(value[key]) is not int or not minimum <= value[key] <= maximum:
                raise DeploymentError(f"{key}는 {minimum} ~ {maximum} 범위 정수여야 한다")
        numbers = {
            "idle_interval_seconds": (0.1, 60.0),
            "health_interval_seconds": (0.1, 30.0),
            "mutation_settle_seconds": (1.0, 120.0),
            "mutation_poll_seconds": (0.1, 10.0),
        }
        for key, (minimum, maximum) in numbers.items():
            if type(value[key]) not in {int, float} or not minimum <= value[key] <= maximum:
                raise DeploymentError(f"{key}는 {minimum} ~ {maximum} 범위여야 한다")

        bootstrap = _bootstrap(value["bootstrap_active"])
        if bootstrap is not None:
            try:
                validate_image_reference(bootstrap.image, repository)
            except ManifestError as exc:
                raise DeploymentError(
                    "bootstrap image는 repository의 immutable digest여야 한다"
                ) from exc
        return cls(
            enabled=value["enabled"], target_id=target_id, environment=environment,
            repository=repository, repository_id=repository_id,
            repository_owner_id=owner_id,
            backend_ci_workflow_id=immutable_ids["backend_ci_workflow_id"],
            backend_ci_workflow_sha256=hashes["backend_ci_workflow_sha256"],
            deployment_workflow_id=immutable_ids["deployment_workflow_id"],
            deployment_workflow_sha256=_sha256(
                value["deployment_workflow_sha256"], "deployment_workflow_sha256"
            ),
            environment_id=immutable_ids["environment_id"],
            allowed_reviewer_user_ids=allowed_reviewer_user_ids,
            request_authentication=value["request_authentication"],
            gh_binary=paths["gh_binary"], gh_binary_sha256=hashes["gh_binary_sha256"],
            gh_config_dir=paths["gh_config_dir"], docker_binary=paths["docker_binary"],
            docker_binary_sha256=hashes["docker_binary_sha256"],
            compose_binary=paths["compose_binary"],
            compose_binary_sha256=hashes["compose_binary_sha256"],
            docker_config_dir=paths["docker_config_dir"],
            docker_context=value["docker_context"], docker_endpoint=endpoint,
            project=value["project"], compose_file=paths["compose_file"],
            trusted_compose_files=trusted, env_file=paths["env_file"],
            service=value["service"], global_lock_file=paths["global_lock_file"],
            state_dir=paths["state_dir"], bootstrap_active=bootstrap,
            admission_control=value["admission_control"], idle_checks=value["idle_checks"],
            idle_timeout_seconds=value["idle_timeout_seconds"],
            idle_interval_seconds=float(value["idle_interval_seconds"]),
            health_timeout_seconds=value["health_timeout_seconds"],
            health_interval_seconds=float(value["health_interval_seconds"]),
            stop_timeout_seconds=value["stop_timeout_seconds"],
            command_timeout_seconds=value["command_timeout_seconds"],
            mutation_settle_seconds=float(value["mutation_settle_seconds"]),
            mutation_poll_seconds=float(value["mutation_poll_seconds"]),
        )


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""


def _canonical_fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SubprocessRunner:
    def __init__(self, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds

    def run(self, args: list[str], *, env: dict[str, str]) -> CommandResult:
        # stdout/stderr를 각각 제한해 memory와 temporary disk flood를 막는다.
        # 두 pipe를 동시에 비우고 새 process group 전체를 timeout/overflow에 종료한다.
        try:
            process = subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise DeploymentError(
                f"외부 명령을 시작하지 못했다: {_command_label(args)}"
            ) from exc

        overflow = threading.Event()
        capture_error = threading.Event()
        stdout_chunks: list[bytes] = []

        def terminate_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError):
                    return

        def capture(stream: Any, *, retain: bool) -> None:
            total = 0
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > MAX_EXTERNAL_OUTPUT_BYTES:
                        overflow.set()
                        terminate_group()
                        return
                    if retain:
                        stdout_chunks.append(chunk)
            except OSError:
                capture_error.set()
                terminate_group()

        assert process.stdout is not None and process.stderr is not None
        readers = (
            threading.Thread(
                target=capture, args=(process.stdout,), kwargs={"retain": True}, daemon=True
            ),
            threading.Thread(
                target=capture, args=(process.stderr,), kwargs={"retain": False}, daemon=True
            ),
        )
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            terminate_group()
            process.wait()
            for reader in readers:
                reader.join(timeout=5)
            raise DeploymentError(
                f"외부 명령 시간이 초과돼 process group을 종료했다: {_command_label(args)}"
            ) from exc
        finally:
            if process.poll() is None:
                terminate_group()
                process.wait()
        for reader in readers:
            reader.join(timeout=5)
        if overflow.is_set():
            raise DeploymentError(f"외부 명령 출력이 상한을 넘었다: {_command_label(args)}")
        if any(reader.is_alive() for reader in readers) or capture_error.is_set():
            raise DeploymentError(f"외부 명령 출력을 안전하게 수집하지 못했다: {_command_label(args)}")
        if returncode != 0:
            raise DeploymentError(
                f"외부 명령이 종료 코드 {returncode}로 실패했다: {_command_label(args)}"
            )
        try:
            output = b"".join(stdout_chunks).decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise DeploymentError(
                f"외부 명령 출력이 UTF-8이 아니다: {_command_label(args)}"
            ) from exc
        return CommandResult(stdout=output)


def _safe_name(value: Any, label: str) -> str:
    if type(value) is not str or SAFE_NAME_RE.fullmatch(value) is None:
        raise DeploymentError(f"{label} 값이 안전한 식별자 형식이 아니다")
    return value


def _safe_id(value: Any, label: str) -> str:
    if type(value) is not str or SAFE_ID_RE.fullmatch(value) is None:
        raise DeploymentError(f"{label} 값이 안전한 식별자 형식이 아니다")
    return value


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise DeploymentError(f"{label}는 소문자 SHA-256이어야 한다")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if type(value) is not str:
        raise DeploymentError(f"{label}는 절대 경로 문자열이어야 한다")
    path = Path(value)
    if (
        not path.is_absolute()
        or path.anchor != "/"
        or ".." in path.parts
        or str(path) != value
    ):
        raise DeploymentError(
            f"{label}는 alias·상위 이동 없는 canonical 절대 경로여야 한다"
        )
    return path


def _trusted_files(value: Any) -> tuple[TrustedFile, ...]:
    if type(value) is not list or len(value) != 1:
        raise DeploymentError(
            "배포는 include/extends 없는 단일 flattened Compose 파일만 허용한다"
        )
    result: list[TrustedFile] = []
    seen: set[Path] = set()
    for index, item in enumerate(value):
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise DeploymentError(f"trusted_compose_files[{index}] 계약이 잘못됐다")
        path = _absolute_path(item["path"], f"trusted_compose_files[{index}].path")
        if path in seen:
            raise DeploymentError("trusted_compose_files에 중복 경로가 있다")
        seen.add(path)
        result.append(TrustedFile(path, _sha256(item["sha256"], "compose sha256")))
    return tuple(result)


def _reference(value: Any, label: str) -> str:
    if type(value) is not str or not value or len(value) > 2048:
        raise DeploymentError(f"{label}가 비어 있거나 너무 길다")
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise DeploymentError(f"{label}에 공백 여백이나 제어 문자를 사용할 수 없다")
    return value


def _bootstrap(value: Any) -> BootstrapActive | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {
        "image", "source_sha", "image_id", "deployment_id",
        "request_run_id", "request_run_attempt", "ci_run_id", "ci_run_attempt",
    }:
        raise DeploymentError("bootstrap_active 계약이 잘못됐다")
    image = _reference(value["image"], "bootstrap_active.image")
    source_sha = value["source_sha"]
    image_id = value["image_id"]
    if type(source_sha) is not str or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise DeploymentError("bootstrap_active.source_sha 형식이 잘못됐다")
    if type(image_id) is not str or IMAGE_ID_RE.fullmatch(image_id) is None:
        raise DeploymentError("bootstrap_active.image_id 형식이 잘못됐다")
    identities: dict[str, int] = {}
    for key in ("request_run_id", "request_run_attempt", "ci_run_id", "ci_run_attempt"):
        item = value[key]
        if type(item) is not int or item < 1:
            raise DeploymentError(f"bootstrap_active.{key}는 양의 정수여야 한다")
        identities[key] = item
    return BootstrapActive(
        image=image, source_sha=source_sha, image_id=image_id,
        deployment_id=_safe_id(value["deployment_id"], "bootstrap deployment_id"),
        **identities,
    )


def _command_label(args: list[str]) -> str:
    normalized = [Path(args[0]).name, *args[1:]] if args else []
    if normalized[:3] == ["gh", "attestation", "verify"]:
        return "gh attestation verify"
    if normalized and normalized[0] in {"docker-compose", "compose"}:
        for verb in ("config", "ps", "exec", "stop", "up"):
            if verb in normalized:
                return f"docker compose {verb}"
        return "docker compose"
    if normalized[:2] == ["docker", "image"] and len(normalized) > 2:
        return f"docker image {normalized[2]}"
    if normalized[:2] == ["docker", "inspect"]:
        return "docker inspect"
    if normalized[:2] == ["docker", "pull"]:
        return "docker pull"
    if normalized[:2] == ["docker", "tag"]:
        return "docker tag"
    if normalized[:3] == ["docker", "context", "inspect"]:
        return "docker context inspect"
    return normalized[0] if normalized else "unknown"


def read_config(path: Path) -> DeploymentConfig:
    try:
        payload = read_trusted_regular_file(path, max_bytes=64 * 1024)
    except SecurePathError as exc:
        raise DeploymentError(f"호스트 설정 경로가 안전하지 않다: {exc}") from exc
    value = _decode_json_object(payload, label="호스트 설정")
    return DeploymentConfig.from_dict(value)


def _decode_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DeploymentError(f"{label}에 중복 JSON 키가 있다: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except DeploymentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise DeploymentError(f"{label} JSON을 해석하지 못했다") from exc
    if type(value) is not dict:
        raise DeploymentError(f"{label}은 JSON 객체여야 한다")
    return value


def _read_secure_canonical_json(path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_secure_bytes(path, max_bytes=64 * 1024)
    value = _decode_json_object(payload, label=label)
    if payload != canonical_json_bytes(value):
        raise DeploymentError(f"{label}이 승인된 canonical JSON 형식이 아니다")
    return value


def _check_directory(path: Path, *, private: bool) -> os.stat_result:
    descriptor: int | None = None
    try:
        descriptor = open_trusted_directory(path, private=private)
        return os.fstat(descriptor)
    except SecurePathError as exc:
        raise DeploymentError(f"필수 디렉터리의 신뢰 경로가 안전하지 않다: {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_regular_file(path: Path, *, private: bool = False) -> os.stat_result:
    try:
        metadata = stat_trusted_regular_file(path, private=private)
    except SecurePathError as exc:
        raise DeploymentError(f"필수 파일의 신뢰 경로가 안전하지 않다: {path}: {exc}") from exc
    assert metadata is not None
    return metadata


def _file_sha256(path: Path) -> str:
    try:
        return sha256_trusted_regular_file(path)
    except SecurePathError as exc:
        raise DeploymentError(f"신뢰 파일을 안전하게 hash할 수 없다: {path}: {exc}") from exc


def _read_secure_bytes(path: Path, *, max_bytes: int) -> bytes:
    """권한을 다시 확인한 단일 descriptor snapshot만 반환한다."""
    try:
        return read_trusted_regular_file(
            path, max_bytes=max_bytes, require_nonempty=True
        )
    except SecurePathError as exc:
        raise DeploymentError(f"artifact를 안전하게 읽을 수 없다: {path}: {exc}") from exc


@contextmanager
def _artifact_snapshot(
    config: DeploymentConfig,
    *,
    request: Path,
    request_attestation: Path,
    release: Path,
    release_attestation: Path,
    image_attestation: Path,
) -> Iterator[dict[str, Path]]:
    """검증과 실행이 같은 byte를 보도록 owner-only staging에 고정한다."""
    sources = {
        "request": (request, "conan-development-request.json", 64 * 1024),
        "request_attestation": (
            request_attestation, "conan-development-request.sigstore.json",
            MAX_EXTERNAL_OUTPUT_BYTES,
        ),
        "release": (release, "conan-release.json", 64 * 1024),
        "release_attestation": (
            release_attestation, "conan-release.sigstore.json",
            MAX_EXTERNAL_OUTPUT_BYTES,
        ),
        "image_attestation": (
            image_attestation, "conan-image.sigstore.json",
            MAX_EXTERNAL_OUTPUT_BYTES,
        ),
    }
    directory = config.state_dir / f".verify-{uuid.uuid4().hex}"
    try:
        directory.mkdir(mode=0o700)
    except OSError as exc:
        raise DeploymentError("owner-only artifact staging 디렉터리를 만들지 못했다") from exc
    staged: dict[str, Path] = {}
    cleanup_error: OSError | None = None
    try:
        for label, (source, name, limit) in sources.items():
            target = directory / name
            try:
                atomic_write_bytes(target, _read_secure_bytes(source, max_bytes=limit))
            except DeploymentStateError as exc:
                raise DeploymentError(f"artifact snapshot 기록 실패: {exc}") from exc
            staged[label] = target
        yield staged
    finally:
        for target in staged.values():
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        try:
            directory.rmdir()
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise DeploymentError("artifact staging 정리에 실패했다") from cleanup_error


def _path_identity(path: Path) -> tuple[Any, ...]:
    try:
        return trusted_path_identity(path)
    except SecurePathError as exc:
        raise DeploymentError(f"경로 identity를 안전하게 확인할 수 없다: {path}: {exc}") from exc


def validate_runtime(
    config_path: Path,
    config: DeploymentConfig,
    *,
    artifact_paths: tuple[Path, ...] = (),
    require_gh: bool = False,
) -> None:
    if not config_path.is_absolute() or any(not path.is_absolute() for path in artifact_paths):
        raise DeploymentError("config와 artifact는 절대 경로여야 한다")
    _require_regular_file(config_path)
    _check_directory(config_path.parent, private=False)
    for path in artifact_paths:
        metadata = _require_regular_file(path)
        _check_directory(path.parent, private=False)
        if metadata.st_size <= 0 or metadata.st_size > MAX_EXTERNAL_OUTPUT_BYTES:
            raise DeploymentError(f"artifact 크기가 허용 범위 밖이다: {path}")
    for item in config.trusted_compose_files:
        _require_regular_file(item.path)
        _check_directory(item.path.parent, private=False)
        payload = _read_secure_bytes(item.path, max_bytes=MAX_EXTERNAL_OUTPUT_BYTES)
        if hashlib.sha256(payload).hexdigest() != item.sha256:
            raise DeploymentError(f"신뢰 Compose 파일 hash가 다르다: {item.path}")
        if re.search(
            rb"(?mi)^[ \t]*(?:['\"]?(?:include|extends)['\"]?)[ \t]*:", payload
        ):
            raise DeploymentError(
                "Compose include/extends는 허용하지 않는다; flattened 파일이 필요하다"
            )
    _require_regular_file(config.env_file, private=True)
    _check_directory(config.env_file.parent, private=False)

    docker = _require_regular_file(config.docker_binary)
    if not docker.st_mode & 0o111:
        raise DeploymentError("Docker binary에 실행 권한이 없다")
    _check_directory(config.docker_binary.parent, private=False)
    if _file_sha256(config.docker_binary) != config.docker_binary_sha256:
        raise DeploymentError("Docker binary hash가 승인값과 다르다")
    compose = _require_regular_file(config.compose_binary)
    if not compose.st_mode & 0o111:
        raise DeploymentError("Compose binary에 실행 권한이 없다")
    _check_directory(config.compose_binary.parent, private=False)
    if _file_sha256(config.compose_binary) != config.compose_binary_sha256:
        raise DeploymentError("Compose binary hash가 승인값과 다르다")
    _check_directory(config.docker_config_dir, private=True)
    socket_path = Path(config.docker_endpoint.removeprefix("unix://"))
    try:
        stat_trusted_unix_socket(socket_path)
    except SecurePathError as exc:
        raise DeploymentError(
            f"Docker unix endpoint의 신뢰 경로가 안전하지 않다: {socket_path}: {exc}"
        ) from exc
    if require_gh:
        gh = _require_regular_file(config.gh_binary)
        if not gh.st_mode & 0o111:
            raise DeploymentError("GitHub CLI binary에 실행 권한이 없다")
        _check_directory(config.gh_binary.parent, private=False)
        if _file_sha256(config.gh_binary) != config.gh_binary_sha256:
            raise DeploymentError("GitHub CLI binary hash가 승인값과 다르다")
        _check_directory(config.gh_config_dir, private=True)

    _check_directory(config.state_dir, private=True)
    _check_directory(config.global_lock_file.parent, private=False)
    try:
        lock_metadata = stat_trusted_regular_file(
            config.global_lock_file, private=True, missing_ok=True
        )
    except SecurePathError as exc:
        raise DeploymentError(f"global lock 경로가 안전하지 않다: {exc}") from exc
    if lock_metadata is not None and stat.S_IMODE(lock_metadata.st_mode) != 0o600:
        raise DeploymentError("기존 global lock 파일 권한은 정확히 0600이어야 한다")
    for label, path in (
        ("state", config.state_file),
        ("journal", config.journal_file),
        ("desired image", config.desired_image_env),
    ):
        try:
            stat_trusted_regular_file(path, private=True, missing_ok=True)
        except SecurePathError as exc:
            raise DeploymentError(f"{label} 경로가 안전하지 않다: {exc}") from exc

    protected: dict[str, Path] = {
        "config": config_path, "env": config.env_file,
        "docker_binary": config.docker_binary, "compose_binary": config.compose_binary,
        "global_lock": config.global_lock_file,
        "state": config.state_file, "journal": config.journal_file,
        "desired_image": config.desired_image_env,
        **{f"artifact_{i}": path for i, path in enumerate(artifact_paths)},
        **{f"compose_{i}": item.path for i, item in enumerate(config.trusted_compose_files)},
    }
    if require_gh:
        protected["gh_binary"] = config.gh_binary
    identities: dict[tuple[Any, ...], str] = {}
    for label, path in protected.items():
        identity = _path_identity(path)
        if identity in identities:
            raise DeploymentError(f"신뢰 경로가 충돌한다: {identities[identity]}와 {label}")
        identities[identity] = label


def _docker(config: DeploymentConfig) -> str:
    return str(config.docker_binary)


def _docker_env(
    config: DeploymentConfig,
    image: str | None = None,
    start_drained: str | None = None,
) -> dict[str, str]:
    result = {
        "PATH": (
            f"{config.docker_binary.parent}:{config.compose_binary.parent}:/usr/bin:/bin"
        ),
        "DOCKER_CONFIG": str(config.docker_config_dir),
        # DOCKER_CONTEXT는 DOCKER_HOST보다 우선한다. 승인된 unix endpoint만이
        # 실제 명령의 authority가 되도록 context는 명령 인수로만 audit한다.
        "DOCKER_HOST": config.docker_endpoint,
        "CONAN_ENV_FILE": str(config.env_file),
        # env_file에 남은 stale fence가 terminal container로 전파되지 않도록
        # accepting 기동도 빈 값을 명시한다.
        "DEEPCHECK_START_DRAINED": "",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if image is not None:
        result["CONAN_IMAGE"] = image
    if start_drained is not None:
        result["DEEPCHECK_START_DRAINED"] = _safe_id(
            start_drained, "startup deployment_id"
        )
    return result


def _gh_env(
    config: DeploymentConfig, *, registry_auth: bool = False
) -> dict[str, str]:
    result = {
        "PATH": f"{config.gh_binary.parent}:/usr/bin:/bin",
        "GH_CONFIG_DIR": str(config.gh_config_dir),
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
        "LANG": "C",
        "LC_ALL": "C",
    }
    # gh는 bundle을 명시한 OCI digest 검증에서도 registry에서 subject를
    # resolve하므로, 그 한 호출에만 승인된 owner-only credential store를 준다.
    if registry_auth:
        result["DOCKER_CONFIG"] = str(config.docker_config_dir)
    return result


def _compose(config: DeploymentConfig) -> list[str]:
    args = [
        str(config.compose_binary), "-p", config.project,
        "--env-file", str(config.env_file),
    ]
    try:
        desired_exists = stat_trusted_regular_file(
            config.desired_image_env, private=True, missing_ok=True
        ) is not None
    except SecurePathError as exc:
        raise DeploymentError(f"desired image 경로가 안전하지 않다: {exc}") from exc
    if desired_exists:
        args.extend(["--env-file", str(config.desired_image_env)])
    args.extend(["-f", str(config.compose_file)])
    return args


def _run(
    runner: Any,
    args: list[str],
    *,
    config: DeploymentConfig,
    image: str | None = None,
    start_drained: str | None = None,
) -> str:
    return runner.run(
        args, env=_docker_env(config, image, start_drained)
    ).stdout.strip()


def _single_line(value: str, label: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DeploymentError(f"{label} 결과가 정확히 하나가 아니다")
    return lines[0]


def _optional_line(value: str, label: str) -> str | None:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 1:
        raise DeploymentError(f"{label} 결과가 둘 이상이다")
    return lines[0] if lines else None


def _verify_docker_target(runner: Any, config: DeploymentConfig) -> None:
    endpoint = _single_line(
        _run(
            runner,
            [
                _docker(config), "context", "inspect", "--format",
                "{{.Endpoints.docker.Host}}", config.docker_context,
            ],
            config=config,
        ),
        "Docker context endpoint",
    )
    if endpoint != config.docker_endpoint:
        raise DeploymentError("Docker context endpoint가 승인된 값과 다르다")


def _assert_compose_image(runner: Any, config: DeploymentConfig, *, image: str) -> None:
    _run(runner, _compose(config) + ["config", "--quiet"], config=config, image=image)
    resolved = _single_line(
        _run(
            runner,
            _compose(config) + ["config", "--images", config.service],
            config=config,
            image=image,
        ),
        "Compose image",
    )
    if resolved != image:
        raise DeploymentError("Compose가 target service의 CONAN_IMAGE를 정확히 해석하지 않았다")


def _inspect_image_id(runner: Any, config: DeploymentConfig, image: str) -> str:
    image_id = _single_line(
        _run(
            runner,
            [_docker(config), "image", "inspect", "--format", "{{.Id}}", image],
            config=config,
        ),
        "image ID",
    )
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        raise DeploymentError("Docker image ID 형식이 잘못됐다")
    return image_id


def _container_id(
    runner: Any,
    config: DeploymentConfig,
    *,
    compose_image: str | None = None,
) -> str | None:
    return _optional_line(
        _run(
            runner,
            _compose(config) + ["ps", "-q", config.service],
            config=config,
            image=compose_image,
        ),
        "Compose container",
    )


def _running_image_id(
    runner: Any,
    config: DeploymentConfig,
    *,
    compose_image: str | None = None,
) -> str | None:
    container_id = _container_id(runner, config, compose_image=compose_image)
    if container_id is None:
        return None
    value = _single_line(
        _run(
            runner,
            [_docker(config), "inspect", "--format", "{{.Image}}", container_id],
            config=config,
        ),
        "running image ID",
    )
    if IMAGE_ID_RE.fullmatch(value) is None:
        raise DeploymentError("실행 중 image ID 형식이 잘못됐다")
    return value


def _runtime_identity(runner: Any, config: DeploymentConfig) -> tuple[str, ...]:
    """Compose service의 container/image identity를 한 관측값으로 반환한다."""

    container_id = _container_id(runner, config)
    if container_id is None:
        return ("missing",)
    image_id = _single_line(
        _run(
            runner,
            [_docker(config), "inspect", "--format", "{{.Image}}", container_id],
            config=config,
        ),
        "stable running image ID",
    )
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        raise DeploymentError("안정화 중 실행 image ID 형식이 잘못됐다")
    return ("container", container_id, image_id)


def _wait_runtime_stable(
    runner: Any,
    config: DeploymentConfig,
    *,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[str, ...]:
    """중단된 CLI 뒤 Docker daemon의 늦은 mutation이 잦아들 때까지 기다린다."""

    deadline = monotonic() + max(
        config.health_timeout_seconds,
        config.mutation_settle_seconds * 3,
    )
    last: tuple[str, ...] | None = None
    stable_since: float | None = None
    while monotonic() < deadline:
        try:
            current = _runtime_identity(runner, config)
        except DeploymentError:
            current = None
        now = monotonic()
        if current is None:
            last = None
            stable_since = None
        elif current != last:
            last = current
            stable_since = now
        elif stable_since is not None and now - stable_since >= config.mutation_settle_seconds:
            return current
        sleeper(config.mutation_poll_seconds)
    raise DeploymentError("Docker mutation 상태가 안정화되지 않아 자동 수렴을 중단했다")


def _probe_json(
    runner: Any,
    config: DeploymentConfig,
    endpoint: str,
    *,
    compose_image: str | None = None,
) -> dict[str, Any]:
    raw = _run(
        runner,
        _compose(config) + [
            "exec", "-T", config.service, "python", "-c", _PROBE,
            f"http://127.0.0.1:8000/{endpoint}",
        ],
        config=config,
        image=compose_image,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"/{endpoint} 응답이 JSON이 아니다") from exc
    if type(value) is not dict:
        raise DeploymentError(f"/{endpoint} 응답이 객체가 아니다")
    return value


def _control(runner: Any, config: DeploymentConfig, action: str, deployment_id: str) -> None:
    if action not in {"drain", "resume"}:
        raise DeploymentError("지원하지 않는 admission control 동작이다")
    raw = _run(
        runner,
        _compose(config) + [
            "exec", "-T", config.service, "python", "-c", _CONTROL,
            action, _safe_id(deployment_id, "deployment_id"),
        ],
        config=config,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"admission {action} 응답이 JSON이 아니다") from exc
    expected_status = "draining" if action == "drain" else "accepting"
    if (
        type(value) is not dict
        or set(value) != {"status", "changed"}
        or value.get("status") != expected_status
        or type(value.get("changed")) is not bool
    ):
        raise DeploymentError(f"admission {action} 응답 계약이 다르다")


def _harness_stats(value: dict[str, Any]) -> dict[str, Any]:
    stats = value.get("harness")
    if type(stats) is not dict:
        raise DeploymentError("/ready에 harness 통계가 없다")
    for key in ("inflight_urls", "backlog_size"):
        if type(stats.get(key)) is not int or stats[key] < 0:
            raise DeploymentError(f"/ready {key} 형식이 잘못됐다")
    if type(stats.get("accepting_jobs")) is not bool:
        raise DeploymentError("/ready accepting_jobs 형식이 잘못됐다")
    if stats.get("admission_protocol") != DURABLE_ADMISSION_PROTOCOL:
        raise DeploymentError("/ready가 durable admission protocol을 증명하지 못했다")
    if stats.get("admission_state") not in {"accepting", "draining", "closed"}:
        raise DeploymentError("/ready admission_state 형식이 잘못됐다")
    return stats


def _assert_accepting_ready(value: dict[str, Any]) -> None:
    stats = _harness_stats(value)
    if (
        value.get("status") == "draining"
        and stats["accepting_jobs"] is False
        and stats["admission_state"] == "draining"
    ):
        raise _ConfirmedDrainingError("서비스가 검증된 durable draining 상태다")
    if (
        value.get("status") not in {"ready", "saturated"}
        or stats["accepting_jobs"] is not True
        or stats["admission_state"] != "accepting"
    ):
        raise DeploymentError("서비스가 분석 접수 가능 상태가 아니다")


def _assert_draining_ready(value: dict[str, Any]) -> None:
    stats = _harness_stats(value)
    if (
        value.get("status") != "draining"
        or stats["accepting_jobs"] is not False
        or stats["admission_state"] != "draining"
    ):
        raise DeploymentError("서비스가 검증용 draining 상태가 아니다")


def _wait_until_idle(
    runner: Any,
    config: DeploymentConfig,
    *,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + config.idle_timeout_seconds
    consecutive = 0
    while True:
        value = _probe_json(runner, config, "ready")
        stats = _harness_stats(value)
        if (
            value.get("status") != "draining"
            or stats["accepting_jobs"] is not False
            or stats["admission_state"] != "draining"
        ):
            raise DeploymentError("drain 이후 /ready가 신규 접수 차단을 증명하지 못했다")
        if stats["inflight_urls"] == 0 and stats["backlog_size"] == 0:
            consecutive += 1
            if consecutive >= config.idle_checks:
                return
        else:
            consecutive = 0
        if monotonic() >= deadline:
            raise DeploymentError("drain 뒤 진행 작업 종료 대기 시간이 초과됐다")
        sleeper(config.idle_interval_seconds)


def _wait_for_service(
    runner: Any,
    config: DeploymentConfig,
    *,
    expected_image_id: str,
    expected_admission: str = "accepting",
    compose_image: str | None = None,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> str:
    if expected_admission not in {"accepting", "draining"}:
        raise DeploymentError("지원하지 않는 expected admission 상태다")
    deadline = monotonic() + config.health_timeout_seconds
    while monotonic() < deadline:
        container_id = _container_id(runner, config, compose_image=compose_image)
        if container_id is None:
            sleeper(config.health_interval_seconds)
            continue
        state = _single_line(
            _run(
                runner,
                [
                    _docker(config), "inspect", "--format",
                    "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}"
                    "{{else}}missing{{end}}",
                    container_id,
                ],
                config=config,
            ),
            "container health",
        )
        if state == "running|healthy":
            break
        if state.startswith(("exited|", "dead|")):
            raise DeploymentError("컨테이너가 healthy 전에 종료됐다")
        sleeper(config.health_interval_seconds)
    else:
        raise DeploymentError("컨테이너 health 확인 시간이 초과됐다")
    running_image_id = _running_image_id(
        runner, config, compose_image=compose_image
    )
    if running_image_id != expected_image_id:
        raise DeploymentError("실행 컨테이너가 기대한 image ID와 다르다")
    if _probe_json(
        runner, config, "health", compose_image=compose_image
    ).get("status") != "ok":
        raise DeploymentError("/health 응답이 ok가 아니다")
    ready = _probe_json(runner, config, "ready", compose_image=compose_image)
    if expected_admission == "accepting":
        _assert_accepting_ready(ready)
    else:
        _assert_draining_ready(ready)
    return container_id


def _start_image(
    runner: Any,
    config: DeploymentConfig,
    *,
    image: str,
    image_id: str,
    expected_admission: str,
    startup_deployment_id: str | None,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    if (expected_admission == "draining") != (startup_deployment_id is not None):
        raise DeploymentError("startup fence와 expected admission 조합이 안전하지 않다")
    if _inspect_image_id(runner, config, image) != image_id:
        raise DeploymentError("기동할 image reference가 승인한 image ID와 다르다")
    _assert_compose_image(runner, config, image=image)
    _run(
        runner,
        _compose(config) + [
            "up", "-d", "--force-recreate", "--no-build", "--no-deps",
            "--pull", "never", config.service,
        ],
        config=config,
        image=image,
        start_drained=startup_deployment_id,
    )
    _wait_for_service(
        runner, config, expected_image_id=image_id,
        expected_admission=expected_admission,
        compose_image=image,
        sleeper=sleeper, monotonic=monotonic,
    )


def _converge_accepting_image(
    runner: Any,
    config: DeploymentConfig,
    *,
    image: str,
    image_id: str,
    allowed_existing_image_ids: tuple[str, ...] = (),
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    """terminal state 뒤 fence 없는 컨테이너로 수렴해 restart-safe하게 만든다."""

    actual = _running_image_id(runner, config)
    if actual is not None and actual not in {image_id, *allowed_existing_image_ids}:
        raise DeploymentError("실행 image가 transaction의 승인된 identity 범위 밖이다")
    if actual == image_id:
        try:
            _wait_for_service(
                runner,
                config,
                expected_image_id=image_id,
                expected_admission="accepting",
                sleeper=sleeper,
                monotonic=monotonic,
            )
            return
        except _ConfirmedDrainingError as readiness_error:
            # durable startup fence가 확인된 동일 image만 아래에서 재생성한다.
            print(
                f"terminal service의 durable fence 확인, 동일 image로 수렴: {readiness_error}",
                file=sys.stderr,
            )
    _wait_runtime_stable(
        runner, config, sleeper=sleeper, monotonic=monotonic
    )
    _start_image(
        runner,
        config,
        image=image,
        image_id=image_id,
        expected_admission="accepting",
        startup_deployment_id=None,
        sleeper=sleeper,
        monotonic=monotonic,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _journal(
    config: DeploymentConfig,
    request: dict[str, Any],
    *,
    previous: dict[str, str],
    target_image_id: str,
    rollback_tag: str | None,
) -> dict[str, Any]:
    release = request["release"]
    now = _timestamp()
    return {
        "schema_version": DEPLOYMENT_STATE_SCHEMA_VERSION,
        "target_id": request["target_id"],
        "target_fingerprint": config.target_fingerprint,
        "transaction_fingerprint": config.transaction_fingerprint,
        "transaction_id": request["request_id"],
        "phase": "prepared",
        "request_run_id": request["request_run_id"],
        "request_run_attempt": request["request_run_attempt"],
        "ci_run_id": release["ci_run_id"],
        "ci_run_attempt": release["ci_run_attempt"],
        "target_image": release["image"],
        "target_source_sha": release["source_sha"],
        "target_image_id": target_image_id,
        "previous_image": previous["image"],
        "previous_image_id": previous["image_id"],
        "rollback_tag": rollback_tag,
        "started_at": now,
        "updated_at": now,
        "error": None,
    }


def _set_phase(
    config: DeploymentConfig,
    journal: dict[str, Any],
    phase: str,
    *,
    error: BaseException | str | None = None,
) -> dict[str, Any]:
    updated = dict(journal)
    updated["phase"] = phase
    updated["updated_at"] = _timestamp()
    updated["error"] = None if error is None else str(error)[:8192]
    try:
        write_journal(
            config.journal_file,
            updated,
            config.target_id,
            config.target_fingerprint,
            config.transaction_fingerprint,
        )
    except DeploymentStateError as exc:
        raise DeploymentError(f"배포 journal 기록 실패: {exc}") from exc
    return updated


def _initial_active(config: DeploymentConfig, state: dict[str, Any] | None) -> dict[str, str]:
    if state is None:
        raise DeploymentError("durable state가 없다; apply 대신 initialize를 먼저 실행해야 한다")
    active = state.get("active")
    if type(active) is not dict:
        raise DeploymentError("기존 state에 active 배포가 없다")
    return dict(active)


def _state_for(
    config: DeploymentConfig,
    state: dict[str, Any] | None,
    journal: dict[str, Any],
    *,
    active: dict[str, str],
    outcome: str,
) -> dict[str, Any]:
    request_identity = request_tuple(journal)
    ci_identity = ci_tuple(journal)
    if state is not None:
        request_identity = max(request_identity, request_tuple(state))
        ci_identity = max(ci_identity, ci_tuple(state))
    return {
        "schema_version": DEPLOYMENT_STATE_SCHEMA_VERSION,
        "target_id": config.target_id,
        "target_fingerprint": config.target_fingerprint,
        "active": active,
        "highest_request": {
            "run_id": request_identity[0], "run_attempt": request_identity[1]
        },
        "highest_ci": {"run_id": ci_identity[0], "run_attempt": ci_identity[1]},
        "last_outcome": outcome,
        "updated_at": _timestamp(),
    }


def _write_desired_image(config: DeploymentConfig, image: str) -> None:
    reference = _reference(image, "desired image")
    try:
        atomic_write_bytes(config.desired_image_env, f"CONAN_IMAGE={reference}\n".encode())
    except DeploymentStateError as exc:
        raise DeploymentError(f"desired image 기록 실패: {exc}") from exc


def _read_desired_image(config: DeploymentConfig) -> str | None:
    path = config.desired_image_env
    try:
        metadata = stat_trusted_regular_file(path, private=True, missing_ok=True)
    except SecurePathError as exc:
        raise DeploymentError(f"desired image 상태 경로가 안전하지 않다: {exc}") from exc
    if metadata is None:
        return None
    try:
        raw = read_trusted_regular_file(path, max_bytes=4096, private=True)
    except SecurePathError as exc:
        raise DeploymentError(f"desired image 상태를 안전하게 읽지 못했다: {exc}") from exc
    if len(raw) > 4096 or not raw.startswith(b"CONAN_IMAGE=") or not raw.endswith(b"\n"):
        raise DeploymentError("desired image 상태 계약이 잘못됐다")
    try:
        value = raw[len(b"CONAN_IMAGE="):-1].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeploymentError("desired image 상태가 UTF-8이 아니다") from exc
    return _reference(value, "desired image")


def _persist_outcome(
    config: DeploymentConfig,
    state: dict[str, Any] | None,
    journal: dict[str, Any],
    *,
    active: dict[str, str],
    outcome: str,
    error: BaseException | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _write_desired_image(config, active["image"])
    terminal = _set_phase(config, journal, outcome, error=error)
    next_state = _state_for(config, state, terminal, active=active, outcome=outcome)
    try:
        write_state(
            config.state_file,
            next_state,
            config.target_id,
            config.target_fingerprint,
        )
    except DeploymentStateError as exc:
        raise DeploymentError(f"배포 state 기록 실패: {exc}") from exc
    return next_state, terminal


def _verify_attestations(
    runner: Any,
    config: DeploymentConfig,
    request: dict[str, Any],
    *,
    request_path: Path,
    request_attestation_path: Path,
    release_path: Path,
    release_attestation_path: Path,
    image_attestation_path: Path,
) -> None:
    release = request["release"]
    ci_policy = attestation.AttestationPolicy(
        repository=config.repository,
        repository_id=config.repository_id,
        repository_owner_id=config.repository_owner_id,
        workflow_path=WORKFLOW_PATH,
        source_sha=release["source_sha"],
        event="push",
        run_id=release["ci_run_id"],
        run_attempt=release["ci_run_attempt"],
    )
    policies = (
        (
            str(request_path),
            request_attestation_path,
            attestation.AttestationPolicy(
                repository=config.repository,
                repository_id=config.repository_id,
                repository_owner_id=config.repository_owner_id,
                workflow_path=DEPLOYMENT_WORKFLOW_PATH,
                source_sha=request["request_sha"],
                event="workflow_dispatch",
                run_id=request["request_run_id"],
                run_attempt=request["request_run_attempt"],
            ),
            False,
        ),
        (
            str(release_path),
            release_attestation_path,
            ci_policy,
            False,
        ),
        (
            f"oci://{release['image']}",
            image_attestation_path,
            ci_policy,
            True,
        ),
    )
    try:
        for subject, bundle, policy, registry_auth in policies:
            attestation.verify(
                runner=runner,
                gh_binary=config.gh_binary,
                subject=subject,
                policy=policy,
                env=_gh_env(config, registry_auth=registry_auth),
                bundle=bundle,
            )
    except attestation.AttestationError as exc:
        raise DeploymentError(f"artifact attestation 검증 실패: {exc}") from exc


def _verify_github_ci(
    runner: Any,
    config: DeploymentConfig,
    request: dict[str, Any],
) -> None:
    """Release signer인 exact CI attempt와 reviewed workflow bytes를 고정한다."""

    release = request["release"]
    policy = github_approval.CiPolicy(
        repository=config.repository,
        repository_id=config.repository_id,
        repository_owner_id=config.repository_owner_id,
        workflow_id=config.backend_ci_workflow_id,
        workflow_path=WORKFLOW_PATH,
        workflow_sha256=config.backend_ci_workflow_sha256,
        source_sha=release["source_sha"],
        run_id=release["ci_run_id"],
        run_attempt=release["ci_run_attempt"],
    )
    try:
        github_approval.verify_ci(
            runner=runner,
            gh_binary=config.gh_binary,
            policy=policy,
            env=_gh_env(config),
        )
    except github_approval.ApprovalError as exc:
        raise DeploymentError(f"GitHub CI provenance 검증 실패: {exc}") from exc


def _verify_github_environment_approval(
    runner: Any,
    config: DeploymentConfig,
    request: dict[str, Any],
) -> None:
    """GitHub 서버의 exact run과 protected-environment review를 고정한다."""

    policy = github_approval.ApprovalPolicy(
        repository=config.repository,
        repository_id=config.repository_id,
        repository_owner_id=config.repository_owner_id,
        workflow_id=config.deployment_workflow_id,
        workflow_path=DEPLOYMENT_WORKFLOW_PATH,
        workflow_sha256=config.deployment_workflow_sha256,
        source_sha=request["request_sha"],
        run_id=request["request_run_id"],
        run_attempt=request["request_run_attempt"],
        environment=config.environment,
        environment_id=config.environment_id,
        dispatch_actor=request["dispatch_actor"],
        dispatch_actor_id=request["dispatch_actor_id"],
        allowed_reviewer_user_ids=config.allowed_reviewer_user_ids,
    )
    try:
        github_approval.verify(
            runner=runner,
            gh_binary=config.gh_binary,
            policy=policy,
            env=_gh_env(config),
        )
    except github_approval.ApprovalError as exc:
        raise DeploymentError(f"GitHub environment 승인 검증 실패: {exc}") from exc


def _load_state_files(
    config: DeploymentConfig,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return (
            load_state(
                config.state_file, config.target_id, config.target_fingerprint
            ),
            load_journal(
                config.journal_file, config.target_id, config.target_fingerprint
            ),
        )
    except DeploymentStateError as exc:
        raise DeploymentError(f"배포 상태 검증 실패: {exc}") from exc


def _assert_target_binding(
    config: DeploymentConfig,
    state: dict[str, Any] | None,
    journal: dict[str, Any] | None,
) -> None:
    expected = config.target_fingerprint
    for label, value in (("state", state), ("journal", journal)):
        if value is None:
            continue
        actual = value.get("target_fingerprint")
        if type(actual) is not str or SHA256_RE.fullmatch(actual) is None:
            raise DeploymentError(f"{label}에 유효한 controller target identity 지문이 없다")
        if actual != expected:
            raise DeploymentError(f"{label}가 현재 controller target identity와 다르다")


def _ensure_previous_accepting(
    runner: Any,
    config: DeploymentConfig,
    *,
    journal: dict[str, Any],
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    ready = _probe_json(runner, config, "ready")
    stats = _harness_stats(ready)
    if stats["admission_state"] == "draining":
        _control(runner, config, "resume", journal["transaction_id"])
    elif stats["admission_state"] != "accepting" or stats["accepting_jobs"] is not True:
        raise DeploymentError("이전 서비스 admission 상태가 모호해 자동 복구를 중단했다")
    _wait_for_service(
        runner, config, expected_image_id=journal["previous_image_id"],
        sleeper=sleeper, monotonic=monotonic,
    )


def _prior_for_journal(
    config: DeploymentConfig,
    state: dict[str, Any] | None,
    journal: dict[str, Any],
) -> dict[str, str]:
    prior = _initial_active(config, state)
    # committed journal이 먼저 durable해진 뒤 state 기록이 끝났을 수도 있다.
    if journal["phase"] == "committed" and prior["image_id"] == journal["target_image_id"]:
        return prior
    if (
        prior["image"] != journal["previous_image"]
        or prior["image_id"] != journal["previous_image_id"]
    ):
        raise DeploymentError("journal의 이전 이미지가 durable state/bootstrap과 다르다")
    return prior


def _journal_is_ahead_of_state(
    state: dict[str, Any] | None,
    journal: dict[str, Any],
) -> bool:
    """journal/state 세대 관계를 검증하고 journal이 미반영이면 True를 반환한다."""

    if state is None:
        raise DeploymentError("journal은 있지만 durable state가 없어 자동 복구를 거부했다")
    journal_request = request_tuple(journal)
    state_request = request_tuple(state)
    journal_ci = ci_tuple(journal)
    state_ci = ci_tuple(state)
    if journal_request < state_request or journal_ci < state_ci:
        raise DeploymentError("journal이 durable replay watermark보다 오래돼 복구를 거부했다")
    if journal_request > state_request:
        return True
    if journal_ci != state_ci:
        raise DeploymentError("같은 request 세대의 CI identity가 state와 다르다")
    phase = journal["phase"]
    if phase not in {"committed", "rolled_back", "aborted"}:
        raise DeploymentError("state에 이미 반영된 journal이 non-terminal 상태다")
    if state.get("last_outcome") != phase:
        raise DeploymentError("terminal journal과 state outcome이 다르다")
    active = state.get("active")
    if type(active) is not dict:
        raise DeploymentError("terminal state에 active identity가 없다")
    if phase == "committed":
        expected = {
            "image": journal["target_image"],
            "source_sha": journal["target_source_sha"],
            "image_id": journal["target_image_id"],
            "deployment_id": journal["transaction_id"],
        }
        if active != expected:
            raise DeploymentError("committed journal과 active state가 다르다")
    elif (
        active.get("image") != journal["previous_image"]
        or active.get("image_id") != journal["previous_image_id"]
    ):
        raise DeploymentError("rollback journal과 active state가 다르다")
    return False


def _validate_reconcile_binding(
    config: DeploymentConfig,
    state: dict[str, Any] | None,
    journal: dict[str, Any] | None,
) -> bool | None:
    """Docker 접근 전에 target과 복구 transaction 계약을 판정한다."""

    _assert_target_binding(config, state, journal)
    if journal is None:
        return None
    journal_ahead = _journal_is_ahead_of_state(state, journal)
    if (
        journal.get("transaction_fingerprint") != config.transaction_fingerprint
        and (
            journal_ahead
            or journal["phase"] not in {"committed", "rolled_back", "aborted"}
        )
    ):
        raise DeploymentError(
            "진행 중이거나 state 미반영 journal의 transaction 계약이 현재 설정과 다르다"
        )
    return journal_ahead


def _verify_reflected_terminal_without_mutation(
    runner: Any,
    config: DeploymentConfig,
    state: dict[str, Any],
    *,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    """오래된 transaction 계약을 실행하지 않고 현재 active service만 증명한다."""

    active = state.get("active")
    if type(active) is not dict:
        raise DeploymentError("terminal state에 검증할 active identity가 없다")
    desired = _read_desired_image(config)
    if desired is not None and desired != active["image"]:
        raise DeploymentError("desired image 상태와 terminal active state가 다르다")
    _assert_compose_image(runner, config, image=active["image"])
    if _inspect_image_id(runner, config, active["image"]) != active["image_id"]:
        raise DeploymentError("terminal active image reference가 state image ID와 다르다")
    _wait_for_service(
        runner,
        config,
        expected_image_id=active["image_id"],
        expected_admission="accepting",
        compose_image=active["image"],
        sleeper=sleeper,
        monotonic=monotonic,
    )


def _reconcile(
    runner: Any,
    config: DeploymentConfig,
    state: dict[str, Any] | None,
    journal: dict[str, Any] | None,
    *,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    journal_ahead = _validate_reconcile_binding(config, state, journal)
    if journal is None:
        return state, None
    assert journal_ahead is not None
    phase = journal["phase"]
    if journal.get("transaction_fingerprint") != config.transaction_fingerprint:
        assert state is not None
        _verify_reflected_terminal_without_mutation(
            runner,
            config,
            state,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        return state, journal
    target_id = journal.get("target_image_id")
    previous_id = journal.get("previous_image_id")
    if (
        type(target_id) is not str or IMAGE_ID_RE.fullmatch(target_id) is None
        or type(previous_id) is not str or IMAGE_ID_RE.fullmatch(previous_id) is None
        or type(journal.get("previous_image")) is not str
    ):
        raise DeploymentError("journal에 복구 가능한 image identity가 없다")

    if phase == "committed":
        active = {
            "image": journal["target_image"],
            "source_sha": journal["target_source_sha"],
            "image_id": target_id,
            "deployment_id": journal["transaction_id"],
        }
        state, journal = _persist_outcome(
            config, state, journal, active=active, outcome="committed"
        )
        _converge_accepting_image(
            runner,
            config,
            image=journal["target_image"],
            image_id=target_id,
            allowed_existing_image_ids=(previous_id,),
            sleeper=sleeper,
            monotonic=monotonic,
        )
        return state, journal

    prior = _prior_for_journal(config, state, journal)
    if phase in {"committed", "rolled_back", "aborted"}:
        # terminal rollback state를 먼저 durable하게 만든 뒤 fence 없는 service만
        # accepting으로 공개한다.
        state, journal = _persist_outcome(
            config, state, journal, active=prior, outcome=phase,
            error=journal.get("error"),
        )
        _converge_accepting_image(
            runner,
            config,
            image=journal["previous_image"],
            image_id=previous_id,
            allowed_existing_image_ids=(target_id,),
            sleeper=sleeper,
            monotonic=monotonic,
        )
        return state, journal

    if phase in {"switching", "verifying", "rolling_back"}:
        _wait_runtime_stable(
            runner, config, sleeper=sleeper, monotonic=monotonic
        )
    actual = _running_image_id(runner, config)
    if actual is not None and actual not in {previous_id, target_id}:
        raise DeploymentError("실행 image가 journal의 이전/대상 identity 어느 쪽도 아니다")
    if phase in {"prepared", "draining"} and actual == previous_id:
        _ensure_previous_accepting(
            runner, config, journal=journal, sleeper=sleeper, monotonic=monotonic
        )
        state, journal = _persist_outcome(
            config, state, journal, active=prior, outcome="aborted",
            error=journal.get("error"),
        )
        return state, journal

    rollback_tag = journal.get("rollback_tag")
    if type(rollback_tag) is not str:
        raise DeploymentError("복구할 rollback tag가 없어 수동 확인이 필요하다")
    if _inspect_image_id(runner, config, rollback_tag) != previous_id:
        raise DeploymentError("rollback tag가 journal의 이전 image ID와 다르다")
    journal = _set_phase(config, journal, "rolling_back", error=journal.get("error"))
    _start_image(
        runner,
        config,
        image=rollback_tag,
        image_id=previous_id,
        expected_admission="draining",
        startup_deployment_id=journal["transaction_id"],
        sleeper=sleeper,
        monotonic=monotonic,
    )
    state, journal = _persist_outcome(
        config, state, journal, active=prior, outcome="rolled_back",
        error=journal.get("error"),
    )
    _converge_accepting_image(
        runner,
        config,
        image=journal["previous_image"],
        image_id=previous_id,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    return state, journal


def _terminal_result(journal: dict[str, Any]) -> dict[str, Any]:
    status = {
        "committed": "already-committed",
        "rolled_back": "already-rolled-back",
        "aborted": "already-aborted",
    }[journal["phase"]]
    return {
        "status": status,
        "deployment_id": journal["transaction_id"],
        "image": journal["target_image"],
        "source_sha": journal["target_source_sha"],
    }


def initialize_deployment(
    config: DeploymentConfig,
    *,
    runner: Any,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """승인한 현재 image identity로 replay watermark를 일회 초기화한다."""

    bootstrap = config.bootstrap_active
    if bootstrap is None:
        raise DeploymentError("initialize에는 검증된 bootstrap_active가 필요하다")
    state, journal = _load_state_files(config)
    _assert_target_binding(config, state, journal)
    if journal is not None:
        raise DeploymentError("journal이 존재해 initialize할 수 없다; recover가 필요하다")
    _verify_docker_target(runner, config)

    expected_state = {
        "schema_version": DEPLOYMENT_STATE_SCHEMA_VERSION,
        "target_id": config.target_id,
        "target_fingerprint": config.target_fingerprint,
        "active": bootstrap.as_state_value(),
        "highest_request": {
            "run_id": bootstrap.request_run_id,
            "run_attempt": bootstrap.request_run_attempt,
        },
        "highest_ci": {
            "run_id": bootstrap.ci_run_id,
            "run_attempt": bootstrap.ci_run_attempt,
        },
        "last_outcome": "committed",
        "updated_at": state["updated_at"] if state is not None else _timestamp(),
    }
    if state is not None:
        comparable = dict(state)
        comparable["updated_at"] = expected_state["updated_at"]
        if comparable != expected_state:
            raise DeploymentError("기존 state가 bootstrap identity와 달라 재초기화를 거부했다")

    desired = _read_desired_image(config)
    if desired is not None and desired != bootstrap.image:
        raise DeploymentError("기존 desired image가 bootstrap과 다르다")
    actual = _running_image_id(
        runner, config, compose_image=bootstrap.image
    )
    if actual != bootstrap.image_id:
        raise DeploymentError("실행 중 image ID가 bootstrap과 다르다")
    if _inspect_image_id(runner, config, bootstrap.image) != bootstrap.image_id:
        raise DeploymentError("bootstrap image reference가 승인한 image ID를 가리키지 않는다")
    platform = _single_line(
        _run(
            runner,
            [_docker(config), "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", bootstrap.image],
            config=config,
        ),
        "bootstrap image platform",
    )
    if platform != EXPECTED_PLATFORM:
        raise DeploymentError(f"bootstrap image가 {EXPECTED_PLATFORM}가 아니다")
    revision = _single_line(
        _run(
            runner,
            [
                _docker(config), "image", "inspect", "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                bootstrap.image,
            ],
            config=config,
        ),
        "bootstrap image revision",
    )
    if revision != bootstrap.source_sha:
        raise DeploymentError("bootstrap image revision이 승인한 source SHA와 다르다")
    _assert_compose_image(runner, config, image=bootstrap.image)
    _wait_for_service(
        runner, config, expected_image_id=bootstrap.image_id,
        expected_admission="accepting",
        compose_image=bootstrap.image,
        sleeper=sleeper, monotonic=monotonic,
    )
    _write_desired_image(config, bootstrap.image)
    if state is None:
        try:
            write_state(
                config.state_file,
                expected_state,
                config.target_id,
                config.target_fingerprint,
            )
        except DeploymentStateError as exc:
            raise DeploymentError(f"bootstrap state 기록 실패: {exc}") from exc
    return {
        "status": "initialized" if state is None else "already-initialized",
        "target_id": config.target_id,
        "active_image": bootstrap.image,
    }


def apply_deployment(
    config: DeploymentConfig,
    request: dict[str, Any],
    *,
    runner: Any,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """이미 인증된 request를 적용한다. 호출자는 global lock을 보유해야 한다."""

    release = request["release"]
    image = release["image"]
    source_sha = release["source_sha"]
    state, existing_journal = _load_state_files(config)
    _validate_reconcile_binding(config, state, existing_journal)
    _verify_docker_target(runner, config)
    state, existing_journal = _reconcile(
        runner, config, state, existing_journal,
        sleeper=sleeper, monotonic=monotonic,
    )

    if existing_journal is not None and existing_journal["transaction_id"] == request["request_id"]:
        if (
            request_tuple(existing_journal) != request_tuple(request)
            or ci_tuple(existing_journal) != ci_tuple(request)
            or existing_journal["target_image"] != image
        ):
            raise DeploymentError("같은 deployment ID가 다른 identity로 재사용됐다")
        if existing_journal["phase"] in {"committed", "rolled_back", "aborted"}:
            return _terminal_result(existing_journal)
    if state is not None:
        if request_tuple(request) <= request_tuple(state):
            raise DeploymentError("이미 소비했거나 더 오래된 배포 요청이다")
        if ci_tuple(request) < ci_tuple(state):
            raise DeploymentError("현재보다 오래된 CI image로의 downgrade를 거부했다")

    previous = _initial_active(config, state)
    desired = _read_desired_image(config)
    if desired is not None and desired != previous["image"]:
        raise DeploymentError("desired image 상태와 active state/bootstrap이 다르다")
    actual = _running_image_id(runner, config)
    if actual != previous["image_id"]:
        raise DeploymentError("실행 중 image가 durable state/bootstrap과 다르다")

    _assert_compose_image(runner, config, image=image)
    _run(runner, [_docker(config), "pull", image], config=config)
    target_image_id = _inspect_image_id(runner, config, image)
    platform = _single_line(
        _run(
            runner,
            [_docker(config), "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
            config=config,
        ),
        "image platform",
    )
    if platform != EXPECTED_PLATFORM:
        raise DeploymentError(f"대상 이미지가 {EXPECTED_PLATFORM}가 아니다")
    revision = _single_line(
        _run(
            runner,
            [
                _docker(config), "image", "inspect", "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}', image,
            ],
            config=config,
        ),
        "image revision",
    )
    if revision != source_sha:
        raise DeploymentError("대상 이미지 revision 라벨이 source SHA와 다르다")

    if actual == target_image_id:
        prepared = _journal(
            config, request, previous=previous,
            target_image_id=target_image_id, rollback_tag=None
        )
        prepared = _set_phase(config, prepared, "prepared")
        _wait_for_service(
            runner, config, expected_image_id=target_image_id,
            sleeper=sleeper, monotonic=monotonic,
        )
        active = {
            "image": image, "source_sha": source_sha, "image_id": target_image_id,
            "deployment_id": request["request_id"],
        }
        _persist_outcome(config, state, prepared, active=active, outcome="committed")
        return {
            "status": "no-op", "deployment_id": request["request_id"],
            "image": image, "source_sha": source_sha, "rollback_tag": None,
        }

    rollback_tag = (
        f"conan-dev-rollback:{actual.removeprefix('sha256:')[:12]}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    _run(runner, [_docker(config), "tag", actual, rollback_tag], config=config)
    if _inspect_image_id(runner, config, rollback_tag) != actual:
        raise DeploymentError("rollback tag가 현재 image ID를 보존하지 못했다")
    _assert_compose_image(runner, config, image=rollback_tag)
    journal = _journal(
        config, request, previous=previous, target_image_id=target_image_id,
        rollback_tag=rollback_tag,
    )
    journal = _set_phase(config, journal, "prepared")

    mutation_started = False
    committed = False
    try:
        journal = _set_phase(config, journal, "draining")
        _control(runner, config, "drain", request["request_id"])
        _wait_until_idle(runner, config, sleeper=sleeper, monotonic=monotonic)
        journal = _set_phase(config, journal, "switching")
        # stop가 실패해도 실제 프로세스 상태는 이미 바뀌었을 수 있다.
        mutation_started = True
        _run(
            runner,
            _compose(config) + [
                "stop", "--timeout", str(config.stop_timeout_seconds), config.service
            ],
            config=config,
        )
        journal = _set_phase(config, journal, "verifying")
        _start_image(
            runner,
            config,
            image=image,
            image_id=target_image_id,
            expected_admission="draining",
            startup_deployment_id=request["request_id"],
            sleeper=sleeper,
            monotonic=monotonic,
        )
        active = {
            "image": image, "source_sha": source_sha, "image_id": target_image_id,
            "deployment_id": request["request_id"],
        }
        _write_desired_image(config, image)
        # 이 journal write가 commit point다. 이후 오류는 rollback하지 않고 recover한다.
        journal = _set_phase(config, journal, "committed")
        committed = True
        next_state = _state_for(config, state, journal, active=active, outcome="committed")
        try:
            write_state(
                config.state_file,
                next_state,
                config.target_id,
                config.target_fingerprint,
            )
        except DeploymentStateError as exc:
            raise DeploymentError(f"commit 뒤 state 기록 실패: {exc}") from exc
        # commit/state가 durable해진 뒤에만 fence 없는 동일 image로 재생성한다.
        # 이후 실패는 rollback하지 않고 committed recovery로 수렴한다.
        _converge_accepting_image(
            runner,
            config,
            image=image,
            image_id=target_image_id,
            sleeper=sleeper,
            monotonic=monotonic,
        )
    except (Exception, KeyboardInterrupt) as target_error:
        if committed:
            raise DeploymentError(
                "이미 commit된 배포의 state 마무리가 실패했다; recover가 필요하다"
            ) from target_error
        if not mutation_started:
            # drain 요청은 서버에서 반영된 뒤 응답만 유실될 수 있다. 로컬
            # boolean이 아니라 실제 /ready 상태를 보고 resume 여부를 결정한다.
            try:
                _ensure_previous_accepting(
                    runner, config, journal=journal,
                    sleeper=sleeper, monotonic=monotonic,
                )
            except (Exception, KeyboardInterrupt) as resume_error:
                _set_phase(config, journal, "draining", error=resume_error)
                raise DeploymentError(
                    "배포 전 중단 뒤 admission 복구에 실패해 recover가 필요하다"
                ) from target_error
            _persist_outcome(
                config, state, journal, active=previous, outcome="aborted",
                error=target_error,
            )
            raise DeploymentError("서비스 교체 전 안전 조건 실패로 요청을 중단했다") from target_error
        try:
            journal = _set_phase(config, journal, "rolling_back", error=target_error)
            _wait_runtime_stable(
                runner, config, sleeper=sleeper, monotonic=monotonic
            )
            _start_image(
                runner,
                config,
                image=rollback_tag,
                image_id=actual,
                expected_admission="draining",
                startup_deployment_id=request["request_id"],
                sleeper=sleeper,
                monotonic=monotonic,
            )
        except (Exception, KeyboardInterrupt) as rollback_error:
            try:
                journal = _set_phase(
                    config, journal, "rolling_back", error=rollback_error
                )
            except DeploymentError as journal_error:
                raise DeploymentError(
                    "rollback과 실패 journal 보존이 모두 실패해 수동 복구가 필요하다"
                ) from journal_error
            raise DeploymentError(
                "대상 이미지 검증과 이전 이미지 자동 복구가 모두 실패했다"
            ) from target_error
        try:
            state, journal = _persist_outcome(
                config, state, journal, active=previous, outcome="rolled_back",
                error=target_error,
            )
        except (Exception, KeyboardInterrupt) as persist_error:
            raise DeploymentError(
                "이전 이미지는 검증됐지만 rollback terminal 기록에 실패해 recover가 필요하다"
            ) from persist_error
        try:
            _converge_accepting_image(
                runner,
                config,
                image=previous["image"],
                image_id=actual,
                sleeper=sleeper,
                monotonic=monotonic,
            )
        except (Exception, KeyboardInterrupt) as accepting_error:
            raise DeploymentError(
                "rollback은 terminal이지만 접수 가능 상태 수렴에 실패해 recover가 필요하다"
            ) from accepting_error
        raise DeploymentError("대상 이미지 검증 실패로 이전 이미지에 복구했다") from target_error

    return {
        "status": "deployed", "deployment_id": request["request_id"],
        "image": image, "source_sha": source_sha, "previous_image_id": actual,
        "rollback_tag": rollback_tag,
    }


def recover_deployment(
    config: DeploymentConfig,
    *,
    runner: Any,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """로컬 durable journal만 사용해 중단된 transaction을 수렴시킨다."""

    state, journal = _load_state_files(config)
    _validate_reconcile_binding(config, state, journal)
    _verify_docker_target(runner, config)
    if journal is None:
        if state is None:
            raise DeploymentError("durable state가 없어 initialize가 필요하다")
        return {"status": "no-pending-transaction", "target_id": config.target_id}
    state, journal = _reconcile(
        runner, config, state, journal, sleeper=sleeper, monotonic=monotonic
    )
    assert journal is not None
    return {
        "status": f"recovered-{journal['phase']}", "target_id": config.target_id,
        "deployment_id": journal["transaction_id"],
        "active_image": state["active"]["image"] if state and state["active"] else None,
    }


@contextmanager
def deployment_lock(path: Path) -> Iterator[None]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_CLOEXEC"):
        raise DeploymentError("안전한 lock open flag를 지원하지 않는 플랫폼이다")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_descriptor: int | None = None
    try:
        parent_descriptor, name = open_trusted_parent(path)
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    except (OSError, SecurePathError) as exc:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise DeploymentError("global lock 파일을 안전하게 열 수 없다") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DeploymentError("global lock descriptor의 종류·소유자·권한이 안전하지 않다")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("다른 호스트 배포가 진행 중이다") from exc
        yield
    finally:
        os.close(descriptor)
        assert parent_descriptor is not None
        os.close(parent_descriptor)


def plan(config: DeploymentConfig, request: dict[str, Any]) -> dict[str, Any]:
    release = request["release"]
    blockers = []
    if not config.enabled:
        blockers.append("host config enabled=false")
    try:
        if config.state_dir.exists():
            durable_state = load_state(
                config.state_file, config.target_id, config.target_fingerprint
            )
            durable_journal = load_journal(
                config.journal_file, config.target_id, config.target_fingerprint
            )
        else:
            durable_state = None
            durable_journal = None
    except DeploymentStateError:
        blockers.append("durable deployment state is invalid")
    else:
        if durable_state is None:
            if config.bootstrap_active is None:
                blockers.append("bootstrap_active is required before initialize")
            else:
                blockers.append("durable state is missing; initialize is required")
        if durable_journal is not None and durable_journal["phase"] not in {
            "committed", "rolled_back", "aborted"
        }:
            blockers.append("non-terminal journal exists; recover is required")
    return {
        "mode": "plan", "server_commands_executed": 0,
        "artifact_attestations_verified": False,
        "github_environment_approval_verified": False,
        "target_id": config.target_id,
        "repository": config.repository, "environment": request["environment"],
        "deployment_id": request["request_id"], "source_sha": release["source_sha"],
        "image": release["image"], "project": config.project,
        "service": config.service, "blockers": blockers,
    }


def _canonical_path_argument(value: str) -> Path:
    try:
        return _absolute_path(value, "명령 경로")
    except DeploymentError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=_canonical_path_argument, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("--request", type=_canonical_path_argument, required=True)
        command.add_argument(
            "--request-attestation", type=_canonical_path_argument, required=True
        )
        command.add_argument("--release", type=_canonical_path_argument, required=True)
        command.add_argument(
            "--release-attestation", type=_canonical_path_argument, required=True
        )
        command.add_argument(
            "--image-attestation", type=_canonical_path_argument, required=True
        )
    subparsers.add_parser("initialize")
    subparsers.add_parser("recover")
    return parser


def _validated_inputs(
    config: DeploymentConfig,
    request_path: Path,
    release_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        request = validate_deployment_request(
            _read_secure_canonical_json(request_path, label="배포 request"),
            target_id=config.target_id,
            repository=config.repository,
            repository_id=config.repository_id,
            repository_owner_id=config.repository_owner_id,
            environment=config.environment,
            now=datetime.now(timezone.utc),
        )
        release = validate_manifest_for_repository(
            _read_secure_canonical_json(release_path, label="release manifest"),
            repository=config.repository,
            repository_id=config.repository_id,
            repository_owner_id=config.repository_owner_id,
        )
    except ManifestError as exc:
        raise DeploymentError(f"배포 artifact 형식 검증 실패: {exc}") from exc
    if request["release"] != release:
        raise DeploymentError("request에 포함된 release와 독립 release artifact가 다르다")
    return request, release


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = read_config(args.config)
    if args.command == "plan":
        request, _ = _validated_inputs(config, args.request, args.release)
        print(json.dumps(plan(config, request), ensure_ascii=False, indent=2))
        return 0
    if args.command == "apply" and not config.enabled:
        raise DeploymentError("호스트 설정 enabled가 false라 mutation을 거부했다")

    runner = SubprocessRunner(config.command_timeout_seconds)
    if args.command in {"initialize", "recover"}:
        validate_runtime(args.config, config)
        with deployment_lock(config.global_lock_file):
            if args.command == "initialize":
                result = initialize_deployment(config, runner=runner)
            else:
                result = recover_deployment(config, runner=runner)
    else:
        artifact_paths = (
            args.request, args.request_attestation, args.release,
            args.release_attestation, args.image_attestation,
        )
        validate_runtime(
            args.config, config, artifact_paths=artifact_paths, require_gh=True
        )
        with deployment_lock(config.global_lock_file):
            with _artifact_snapshot(
                config,
                request=args.request,
                request_attestation=args.request_attestation,
                release=args.release,
                release_attestation=args.release_attestation,
                image_attestation=args.image_attestation,
            ) as staged:
                request, _ = _validated_inputs(
                    config, staged["request"], staged["release"]
                )
                _verify_attestations(
                    runner, config, request,
                    request_path=staged["request"],
                    request_attestation_path=staged["request_attestation"],
                    release_path=staged["release"],
                    release_attestation_path=staged["release_attestation"],
                    image_attestation_path=staged["image_attestation"],
                )
                _verify_github_ci(runner, config, request)
                _verify_github_environment_approval(runner, config, request)
                result = apply_deployment(config, request, runner=runner)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as exc:
        raise SystemExit(f"개발계 배포 중단: {exc}") from exc
