"""검증 대상으로 전달된 개발계 요청을 한 Compose 서비스에 적용하고 실패하면 복구한다.

기본 동작은 plan이며 Docker를 호출하지 않는다. apply는 별도 호스트 설정에서
명시적으로 활성화하고 신규 접수를 외부에서 차단한 유지보수 창을 확인해야 한다.
이 스크립트는 Compose 파일이나 환경 파일의 내용을 만들거나 수정하지 않는다.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterator
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_manifest import (  # noqa: E402
    ManifestError,
    read_json,
    validate_deployment_request,
)


CONFIG_SCHEMA_VERSION = 1
EXPECTED_ENVIRONMENT = "conan-development"
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_PROBE = (
    "import json,urllib.request,sys;"
    "data=json.load(urllib.request.urlopen(sys.argv[1],timeout=5));"
    "print(json.dumps(data,separators=(',',':')))"
)


class DeploymentError(RuntimeError):
    """안전 조건 또는 배포 검증이 충족되지 않을 때 발생한다."""


@dataclass(frozen=True)
class DeploymentConfig:
    enabled: bool
    repository: str
    request_authentication: str
    docker_binary: Path
    docker_config_dir: Path
    docker_context: str
    docker_endpoint: str
    project: str
    compose_file: Path
    env_file: Path
    service: str
    lock_file: Path
    admission_control: str
    idle_checks: int
    idle_interval_seconds: float
    health_timeout_seconds: int
    health_interval_seconds: float
    stop_timeout_seconds: int
    command_timeout_seconds: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeploymentConfig":
        expected = {
            "schema_version", "enabled", "repository", "request_authentication",
            "docker_binary", "docker_config_dir", "docker_context", "docker_endpoint", "project",
            "compose_file", "env_file", "service", "lock_file", "admission_control",
            "idle_checks", "idle_interval_seconds", "health_timeout_seconds",
            "health_interval_seconds", "stop_timeout_seconds", "command_timeout_seconds",
        }
        missing = expected - value.keys()
        unknown = value.keys() - expected
        if missing or unknown:
            raise DeploymentError(
                f"호스트 설정 필드가 계약과 다르다: 누락={sorted(missing)}, 미지원={sorted(unknown)}"
            )
        if value["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise DeploymentError("지원하지 않는 호스트 설정 schema_version이다")
        if type(value["enabled"]) is not bool:
            raise DeploymentError("enabled는 boolean이어야 한다")
        if value["repository"] != "Dynamic-Juo/be":
            raise DeploymentError("호스트 설정 repository는 Dynamic-Juo/be여야 한다")
        if value["request_authentication"] != "unconfigured":
            raise DeploymentError("현재 지원하는 request_authentication은 unconfigured뿐이다")
        for key in ("docker_context", "project", "service"):
            if not isinstance(value[key], str) or not SAFE_NAME_RE.fullmatch(value[key]):
                raise DeploymentError(f"{key} 값이 안전한 Compose 식별자 형식이 아니다")
        paths = {}
        for key in (
            "docker_binary", "docker_config_dir", "compose_file", "env_file", "lock_file",
        ):
            path = Path(value[key]) if isinstance(value[key], str) else Path()
            if not path.is_absolute():
                raise DeploymentError(f"{key}는 승인된 절대 경로여야 한다")
            paths[key] = path
        endpoint = value["docker_endpoint"]
        if not isinstance(endpoint, str) or not re.fullmatch(
            r"(?:unix|ssh|tcp|npipe)://[^\s\x00-\x1f]{1,2000}", endpoint
        ):
            raise DeploymentError("docker_endpoint가 지원하는 절대 endpoint 형식이 아니다")
        if value["admission_control"] not in {"unconfigured", "external-maintenance"}:
            raise DeploymentError("admission_control 값이 지원 범위 밖이다")
        integers = {
            "idle_checks": (2, 20),
            "health_timeout_seconds": (10, 900),
            "stop_timeout_seconds": (10, 1800),
            "command_timeout_seconds": (60, 3600),
        }
        for key, (minimum, maximum) in integers.items():
            if type(value[key]) is not int or not minimum <= value[key] <= maximum:
                raise DeploymentError(f"{key}는 {minimum} ~ {maximum} 범위의 정수여야 한다")
        numbers = {
            "idle_interval_seconds": (0.1, 60.0),
            "health_interval_seconds": (0.1, 30.0),
        }
        for key, (minimum, maximum) in numbers.items():
            if type(value[key]) not in {int, float} or not minimum <= value[key] <= maximum:
                raise DeploymentError(f"{key}는 {minimum} ~ {maximum} 범위여야 한다")
        return cls(
            enabled=value["enabled"],
            repository=value["repository"],
            request_authentication=value["request_authentication"],
            docker_binary=paths["docker_binary"],
            docker_config_dir=paths["docker_config_dir"],
            docker_context=value["docker_context"],
            docker_endpoint=endpoint,
            project=value["project"],
            compose_file=paths["compose_file"],
            env_file=paths["env_file"],
            service=value["service"],
            lock_file=paths["lock_file"],
            admission_control=value["admission_control"],
            idle_checks=value["idle_checks"],
            idle_interval_seconds=float(value["idle_interval_seconds"]),
            health_timeout_seconds=value["health_timeout_seconds"],
            health_interval_seconds=float(value["health_interval_seconds"]),
            stop_timeout_seconds=value["stop_timeout_seconds"],
            command_timeout_seconds=value["command_timeout_seconds"],
        )


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""


class SubprocessRunner:
    def __init__(self, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds

    def run(self, args: list[str], *, env: dict[str, str]) -> CommandResult:
        try:
            completed = subprocess.run(
                args,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeploymentError(f"외부 명령 시간이 초과됐다: {_command_label(args)}") from exc
        except subprocess.CalledProcessError as exc:
            raise DeploymentError(
                f"외부 명령이 종료 코드 {exc.returncode}로 실패했다: {_command_label(args)}"
            ) from exc
        return CommandResult(stdout=completed.stdout)


def _command_label(args: list[str]) -> str:
    normalized = [Path(args[0]).name, *args[1:]] if args else []
    if normalized[:2] == ["docker", "compose"]:
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
        value = read_json(path)
    except ManifestError as exc:
        raise DeploymentError(str(exc)) from exc
    return DeploymentConfig.from_dict(value)


def _check_file_permissions(
    path: Path, *, secret: bool = False, trusted_owner: bool = True
) -> os.stat_result:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DeploymentError(f"필수 파일을 확인할 수 없다: {path}") from exc
    mode = metadata.st_mode & 0o777
    forbidden = 0o077 if secret else 0o022
    if mode & forbidden:
        requirement = "소유자만 접근" if secret else "그룹/기타 쓰기 금지"
        raise DeploymentError(f"파일 권한이 안전하지 않다({requirement}): {path}")
    if trusted_owner and metadata.st_uid not in {0, os.geteuid()}:
        raise DeploymentError(f"파일 소유자가 현재 계정 또는 root가 아니다: {path}")
    return metadata


def _require_regular_file(path: Path, *, secret: bool = False) -> os.stat_result:
    if path.is_symlink():
        raise DeploymentError(f"심볼릭 링크는 신뢰 파일로 사용할 수 없다: {path}")
    metadata = _check_file_permissions(path, secret=secret)
    if not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError(f"일반 파일이 아니다: {path}")
    return metadata


def _path_identity(path: Path) -> tuple[Any, ...]:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise DeploymentError(f"경로의 부모를 확인할 수 없다: {path}") from exc
        return ("path", str(parent / path.name))
    except OSError as exc:
        raise DeploymentError(f"경로를 확인할 수 없다: {path}") from exc
    return ("inode", metadata.st_dev, metadata.st_ino)


def validate_runtime(config_path: Path, request_path: Path, config: DeploymentConfig) -> None:
    if not config_path.is_absolute() or not request_path.is_absolute():
        raise DeploymentError("apply의 config와 request는 절대 경로여야 한다")
    for path in (config_path, request_path, config.compose_file):
        if not path.is_file():
            raise DeploymentError(f"필수 파일이 없다: {path}")
        _require_regular_file(path)
    if not config.env_file.is_file():
        raise DeploymentError(f"환경 파일이 없다: {config.env_file}")
    _require_regular_file(config.env_file, secret=True)
    if not config.docker_binary.is_file():
        raise DeploymentError(f"Docker 실행 파일이 없다: {config.docker_binary}")
    binary_metadata = _require_regular_file(config.docker_binary)
    if not binary_metadata.st_mode & 0o111:
        raise DeploymentError("Docker 실행 파일에 실행 권한이 없다")
    binary_dir_metadata = _check_file_permissions(config.docker_binary.parent)
    if not stat.S_ISDIR(binary_dir_metadata.st_mode):
        raise DeploymentError("Docker 실행 파일 부모가 디렉터리가 아니다")
    if config.docker_config_dir.is_symlink() or not config.docker_config_dir.is_dir():
        raise DeploymentError("Docker config 디렉터리가 없거나 심볼릭 링크다")
    config_dir_metadata = _check_file_permissions(config.docker_config_dir, secret=True)
    if not stat.S_ISDIR(config_dir_metadata.st_mode):
        raise DeploymentError("Docker config 경로가 디렉터리가 아니다")
    if not config.lock_file.parent.is_dir():
        raise DeploymentError(f"lock 파일 디렉터리가 없다: {config.lock_file.parent}")
    lock_dir_metadata = _check_file_permissions(config.lock_file.parent)
    if not stat.S_ISDIR(lock_dir_metadata.st_mode):
        raise DeploymentError("lock 파일 부모가 디렉터리가 아니다")
    if config.lock_file.is_symlink():
        raise DeploymentError("lock 파일은 심볼릭 링크일 수 없다")
    if config.lock_file.exists():
        lock_metadata = _check_file_permissions(config.lock_file, secret=True)
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise DeploymentError("lock 파일이 일반 파일이 아니다")
    protected_paths = {
        "config": config_path,
        "request": request_path,
        "compose": config.compose_file,
        "env": config.env_file,
        "docker_binary": config.docker_binary,
        "lock": config.lock_file,
    }
    identities: dict[tuple[Any, ...], str] = {}
    for label, path in protected_paths.items():
        identity = _path_identity(path)
        if identity in identities:
            raise DeploymentError(
                f"신뢰 파일 경로가 충돌한다: {identities[identity]}와 {label}"
            )
        identities[identity] = label


def _docker_env(config: DeploymentConfig, image: str | None = None) -> dict[str, str]:
    result = {
        "PATH": f"{config.docker_binary.parent}:/usr/bin:/bin",
        "DOCKER_CONFIG": str(config.docker_config_dir),
        "LANG": "C",
        "LC_ALL": "C",
        "DOCKER_CONTEXT": config.docker_context,
    }
    if image is not None:
        result["CONAN_IMAGE"] = image
    return result


def _docker(config: DeploymentConfig) -> str:
    return str(config.docker_binary)


def _compose(config: DeploymentConfig) -> list[str]:
    return [
        _docker(config), "compose", "-p", config.project,
        "--env-file", str(config.env_file),
        "-f", str(config.compose_file),
    ]


def _assert_compose_image(
    runner: Any, config: DeploymentConfig, *, image: str
) -> None:
    resolved = _single_line(
        _run(
            runner,
            _compose(config) + ["config", "--images"],
            config=config,
            image=image,
        ),
        "Compose image",
    )
    if resolved != image:
        raise DeploymentError(
            "Compose가 CONAN_IMAGE를 대상 서비스의 유일한 image로 해석하지 않았다"
        )


def _run(
    runner: Any,
    args: list[str],
    *,
    config: DeploymentConfig,
    image: str | None = None,
) -> str:
    return runner.run(args, env=_docker_env(config, image)).stdout.strip()


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


def _single_line(value: str, label: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DeploymentError(f"{label} 결과가 정확히 하나가 아니다")
    return lines[0]


def _inspect_image_id(runner: Any, config: DeploymentConfig, image: str) -> str:
    image_id = _single_line(
        _run(
            runner,
            [_docker(config), "image", "inspect", "--format", "{{.Id}}", image],
            config=config,
        ),
        "image ID",
    )
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise DeploymentError("Docker image ID 형식이 잘못됐다")
    return image_id


def _probe_json(runner: Any, config: DeploymentConfig, endpoint: str) -> dict[str, Any]:
    raw = _run(
        runner,
        _compose(config) + [
            "exec", "-T", config.service, "python", "-c", _PROBE,
            f"http://127.0.0.1:8000/{endpoint}",
        ],
        config=config,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"/{endpoint} 응답이 JSON이 아니다") from exc
    if not isinstance(value, dict):
        raise DeploymentError(f"/{endpoint} 응답이 객체가 아니다")
    return value


def _assert_idle(value: dict[str, Any], *, require_accepting: bool) -> None:
    if value.get("status") != "ready":
        raise DeploymentError("/ready 상태가 ready가 아니다")
    stats = value.get("harness")
    if not isinstance(stats, dict):
        raise DeploymentError("/ready에 harness 통계가 없다")
    if stats.get("inflight_urls") != 0 or stats.get("backlog_size") != 0:
        raise DeploymentError("진행 중이거나 대기 중인 분석이 있어 배포를 중단했다")
    if require_accepting and stats.get("accepting_jobs") is not True:
        raise DeploymentError("새 컨테이너가 분석 접수 가능 상태가 아니다")


def _wait_until_idle(
    runner: Any,
    config: DeploymentConfig,
    *,
    sleeper: Callable[[float], None],
) -> None:
    for index in range(config.idle_checks):
        _assert_idle(_probe_json(runner, config, "ready"), require_accepting=False)
        if index + 1 < config.idle_checks:
            sleeper(config.idle_interval_seconds)


def _wait_for_service(
    runner: Any,
    config: DeploymentConfig,
    *,
    expected_image_id: str,
    require_accepting: bool,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> str:
    deadline = monotonic() + config.health_timeout_seconds
    container_id = ""
    while monotonic() < deadline:
        container_id = _single_line(
            _run(
                runner,
                _compose(config) + ["ps", "-q", config.service],
                config=config,
            ),
            "Compose container",
        )
        state = _single_line(
            _run(runner, [
                _docker(config), "inspect", "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",
                container_id,
            ], config=config),
            "container health",
        )
        if state == "running|healthy":
            break
        if state.startswith("exited|") or state.startswith("dead|"):
            raise DeploymentError("컨테이너가 healthy 전에 종료됐다")
        sleeper(config.health_interval_seconds)
    else:
        raise DeploymentError("컨테이너 health 확인 시간이 초과됐다")
    running_image_id = _single_line(
        _run(
            runner,
            [_docker(config), "inspect", "--format", "{{.Image}}", container_id],
            config=config,
        ),
        "running image ID",
    )
    if running_image_id != expected_image_id:
        raise DeploymentError("실행 컨테이너가 요청한 image ID와 다르다")
    if _probe_json(runner, config, "health").get("status") != "ok":
        raise DeploymentError("/health 응답이 ok가 아니다")
    _assert_idle(_probe_json(runner, config, "ready"), require_accepting=require_accepting)
    return container_id


def _rollback(
    runner: Any,
    config: DeploymentConfig,
    *,
    rollback_tag: str,
    rollback_image_id: str,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    _run(
        runner,
        _compose(config) + [
            "up", "-d", "--no-build", "--no-deps", "--pull", "never", config.service,
        ],
        config=config,
        image=rollback_tag,
    )
    _wait_for_service(
        runner,
        config,
        expected_image_id=rollback_image_id,
        require_accepting=False,
        sleeper=sleeper,
        monotonic=monotonic,
    )


def apply_deployment(
    config: DeploymentConfig,
    request: dict[str, Any],
    *,
    runner: Any,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    release = request["release"]
    image = release["image"]
    source_sha = release["source_sha"]
    base = _compose(config)

    _verify_docker_target(runner, config)
    _run(runner, base + ["config", "--quiet"], config=config, image=image)
    _assert_compose_image(runner, config, image=image)
    current_container = _single_line(
        _run(runner, base + ["ps", "-q", config.service], config=config),
        "Compose container",
    )
    current_image_id = _single_line(
        _run(
            runner,
            [_docker(config), "inspect", "--format", "{{.Image}}", current_container],
            config=config,
        ),
        "current image ID",
    )
    if not IMAGE_ID_RE.fullmatch(current_image_id):
        raise DeploymentError("현재 image ID 형식이 잘못됐다")

    _run(runner, [_docker(config), "pull", image], config=config)
    target_image_id = _inspect_image_id(runner, config, image)
    platform = _single_line(
        _run(runner, [
            _docker(config), "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image,
        ], config=config),
        "image platform",
    )
    if platform != "linux/arm64":
        raise DeploymentError("대상 이미지가 linux/arm64가 아니다")
    revision = _single_line(
        _run(runner, [
            _docker(config), "image", "inspect", "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}', image,
        ], config=config),
        "image revision",
    )
    if revision != source_sha:
        raise DeploymentError("대상 이미지 revision 라벨이 source SHA와 다르다")

    if current_image_id == target_image_id:
        _wait_for_service(
            runner,
            config,
            expected_image_id=target_image_id,
            require_accepting=True,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        return {
            "status": "no-op",
            "image": image,
            "source_sha": source_sha,
            "rollback_tag": None,
        }

    _wait_until_idle(runner, config, sleeper=sleeper)
    rollback_tag = (
        f"conan-dev-rollback:{current_image_id.removeprefix('sha256:')[:12]}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    _run(runner, [_docker(config), "tag", current_image_id, rollback_tag], config=config)
    if _inspect_image_id(runner, config, rollback_tag) != current_image_id:
        raise DeploymentError("rollback 태그가 현재 image ID를 보존하지 못했다")
    _run(runner, base + ["config", "--quiet"], config=config, image=rollback_tag)
    _assert_compose_image(runner, config, image=rollback_tag)

    mutation_started = False
    try:
        # stop가 시간 초과나 오류를 반환해도 서비스 상태가 이미 바뀌었을 수 있다.
        # 호출 직전에 표시해 이후 모든 실패가 rollback을 시도하게 한다.
        mutation_started = True
        _run(
            runner,
            base + ["stop", "--timeout", str(config.stop_timeout_seconds), config.service],
            config=config,
        )
        _run(
            runner,
            base + [
                "up", "-d", "--no-build", "--no-deps", "--pull", "never", config.service,
            ],
            config=config,
            image=image,
        )
        _wait_for_service(
            runner,
            config,
            expected_image_id=target_image_id,
            require_accepting=True,
            sleeper=sleeper,
            monotonic=monotonic,
        )
    except (Exception, KeyboardInterrupt) as target_error:
        if not mutation_started:
            raise
        try:
            _rollback(
                runner,
                config,
                rollback_tag=rollback_tag,
                rollback_image_id=current_image_id,
                sleeper=sleeper,
                monotonic=monotonic,
            )
        except (Exception, KeyboardInterrupt) as rollback_error:
            raise DeploymentError(
                f"대상 이미지 검증과 이전 이미지 복구가 모두 실패했다: {rollback_error}"
            ) from target_error
        raise DeploymentError(
            "대상 이미지 검증이 실패해 이전 이미지로 복구했다"
        ) from target_error

    return {
        "status": "deployed",
        "image": image,
        "source_sha": source_sha,
        "previous_image_id": current_image_id,
        "rollback_tag": rollback_tag,
    }


@contextmanager
def deployment_lock(path: Path) -> Iterator[None]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_CLOEXEC"):
        raise DeploymentError("안전한 lock open flag를 지원하지 않는 플랫폼이다")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DeploymentError("전용 lock 파일을 안전하게 열 수 없다") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DeploymentError("lock 파일 descriptor가 일반 파일이 아니다")
        if metadata.st_uid != os.geteuid():
            raise DeploymentError("lock 파일이 현재 실행 계정 소유가 아니다")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DeploymentError("lock 파일은 소유자만 접근할 수 있어야 한다")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("다른 호스트 배포가 진행 중이다") from exc
        yield
    finally:
        os.close(descriptor)


def plan(config: DeploymentConfig, request: dict[str, Any]) -> dict[str, Any]:
    release = request["release"]
    blockers = []
    if not config.enabled:
        blockers.append("host config enabled=false")
    if config.admission_control != "external-maintenance":
        blockers.append("external maintenance admission control is not selected")
    if config.request_authentication != "verified-github-environment-artifact":
        blockers.append("GitHub environment artifact authentication is not implemented")
    return {
        "mode": "plan",
        "server_commands_executed": 0,
        "repository": config.repository,
        "environment": request["environment"],
        "source_sha": release["source_sha"],
        "image": release["image"],
        "project": config.project,
        "service": config.service,
        "blockers": blockers,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--confirm-maintenance-window", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = read_config(args.config)
    try:
        request = validate_deployment_request(
            read_json(args.request),
            repository=config.repository,
            environment=EXPECTED_ENVIRONMENT,
        )
    except ManifestError as exc:
        raise DeploymentError(f"배포 요청 형식 검증 실패: {exc}") from exc
    if args.command == "plan":
        print(json.dumps(plan(config, request), ensure_ascii=False, indent=2))
        return 0
    if not config.enabled:
        raise DeploymentError("호스트 설정 enabled가 false라 apply를 거부했다")
    if config.admission_control != "external-maintenance":
        raise DeploymentError("신규 접수 차단 방식이 확정되지 않아 apply를 거부했다")
    if not args.confirm_maintenance_window:
        raise DeploymentError("외부 신규 접수 차단 확인이 없어 apply를 거부했다")
    if config.request_authentication != "verified-github-environment-artifact":
        raise DeploymentError(
            "GitHub environment artifact 원본 인증이 구현되지 않아 apply를 거부했다"
        )
    validate_runtime(args.config, args.request, config)
    with deployment_lock(config.lock_file):
        result = apply_deployment(
            config,
            request,
            runner=SubprocessRunner(config.command_timeout_seconds),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as exc:
        raise SystemExit(f"개발계 배포 중단: {exc}") from exc
