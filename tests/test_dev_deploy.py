import fcntl
import json
import os

import pytest

from scripts import dev_deploy, release_manifest


SOURCE_SHA = "a" * 40
OLD_IMAGE_ID = "sha256:" + "1" * 64
TARGET_IMAGE_ID = "sha256:" + "2" * 64
TARGET_DIGEST = "sha256:" + "b" * 64
TARGET_IMAGE = f"ghcr.io/dynamic-juo/be@{TARGET_DIGEST}"


def config(tmp_path, **overrides):
    values = {
        "enabled": True,
        "repository": "Dynamic-Juo/be",
        "request_authentication": "unconfigured",
        "docker_binary": tmp_path / "docker",
        "docker_config_dir": tmp_path / "docker-config",
        "docker_context": "approved-context",
        "docker_endpoint": "unix:///approved/docker.sock",
        "project": "approved-development",
        "compose_file": tmp_path / "compose.yml",
        "env_file": tmp_path / ".env.home",
        "service": "deepcheck-api",
        "lock_file": tmp_path / "deployment.lock",
        "admission_control": "external-maintenance",
        "idle_checks": 2,
        "idle_interval_seconds": 0.1,
        "health_timeout_seconds": 10,
        "health_interval_seconds": 0.1,
        "stop_timeout_seconds": 660,
        "command_timeout_seconds": 1200,
    }
    values.update(overrides)
    return dev_deploy.DeploymentConfig(**values)


def request():
    release = release_manifest.build_manifest(
        repository="Dynamic-Juo/be",
        source_sha=SOURCE_SHA,
        ci_run_id="123",
        ci_run_attempt="1",
        image=TARGET_IMAGE,
    )
    return release_manifest.build_deployment_request(
        release=release,
        repository="Dynamic-Juo/be",
        environment="conan-development",
        request_run_id="456",
        request_run_attempt="1",
        request_sha="c" * 40,
        dispatch_actor="5dotseven",
    )


class FakeRunner:
    def __init__(
        self,
        *,
        busy=False,
        same_image=False,
        stop_fails=False,
        compose_image_mismatch=False,
        wrong_docker_endpoint=False,
        target_fails=False,
        rollback_fails=False,
    ):
        self.busy = busy
        self.same_image = same_image
        self.current_image_id = TARGET_IMAGE_ID if same_image else OLD_IMAGE_ID
        self.stop_fails = stop_fails
        self.compose_image_mismatch = compose_image_mismatch
        self.wrong_docker_endpoint = wrong_docker_endpoint
        self.target_fails = target_fails
        self.rollback_fails = rollback_fails
        self.calls = []
        self.tags = {}
        self.phase = "old"

    def run(self, args, *, env):
        self.calls.append((list(args), dict(env)))
        command = ["docker", *args[1:]] if os.path.basename(args[0]) == "docker" else args
        if command[:3] == ["docker", "context", "inspect"]:
            endpoint = (
                "unix:///wrong/docker.sock"
                if self.wrong_docker_endpoint
                else "unix:///approved/docker.sock"
            )
            return dev_deploy.CommandResult(endpoint + "\n")
        if command[:2] == ["docker", "compose"]:
            if "config" in command:
                if "--images" in command:
                    if self.compose_image_mismatch:
                        return dev_deploy.CommandResult("unexpected:image\n")
                    return dev_deploy.CommandResult(env["CONAN_IMAGE"] + "\n")
                return dev_deploy.CommandResult()
            if "ps" in command:
                return dev_deploy.CommandResult("container-id\n")
            if "exec" in command:
                endpoint = command[-1]
                if endpoint.endswith("/health"):
                    return dev_deploy.CommandResult('{"status":"ok"}\n')
                active = 1 if self.busy and self.phase == "old" else 0
                accepting = self.phase == "target" or self.same_image
                return dev_deploy.CommandResult(json.dumps({
                    "status": "ready",
                    "harness": {
                        "inflight_urls": active,
                        "backlog_size": 0,
                        "accepting_jobs": accepting,
                    },
                }))
            if "stop" in command:
                self.phase = "stopped"
                if self.stop_fails:
                    raise dev_deploy.DeploymentError("simulated stop timeout")
                return dev_deploy.CommandResult()
            if "up" in command:
                image = env.get("CONAN_IMAGE", "")
                self.phase = "target" if image == TARGET_IMAGE else "rollback"
                return dev_deploy.CommandResult()
        if command[:2] == ["docker", "pull"]:
            return dev_deploy.CommandResult()
        if command[:2] == ["docker", "tag"]:
            self.tags[command[3]] = command[2]
            return dev_deploy.CommandResult()
        if command[:3] == ["docker", "image", "inspect"]:
            fmt = command[4]
            image = command[5]
            if fmt == "{{.Id}}":
                image_id = TARGET_IMAGE_ID if image == TARGET_IMAGE else self.tags.get(image)
                if image_id is None:
                    raise AssertionError(f"unexpected image inspect: {image}")
                return dev_deploy.CommandResult(image_id + "\n")
            if fmt == "{{.Os}}/{{.Architecture}}":
                return dev_deploy.CommandResult("linux/arm64\n")
            if "org.opencontainers.image.revision" in fmt:
                return dev_deploy.CommandResult(SOURCE_SHA + "\n")
        if command[:2] == ["docker", "inspect"]:
            fmt = command[3]
            if fmt == "{{.Image}}":
                image_id = {
                    "old": self.current_image_id,
                    "stopped": self.current_image_id,
                    "target": TARGET_IMAGE_ID,
                    "rollback": OLD_IMAGE_ID,
                }[self.phase]
                return dev_deploy.CommandResult(image_id + "\n")
            if ".State.Health" in fmt:
                if self.phase == "target" and self.target_fails:
                    return dev_deploy.CommandResult("exited|unhealthy\n")
                if self.phase == "rollback" and self.rollback_fails:
                    return dev_deploy.CommandResult("exited|unhealthy\n")
                return dev_deploy.CommandResult("running|healthy\n")
        raise AssertionError(f"unexpected command: {args}")


def apply(runner, tmp_path):
    return dev_deploy.apply_deployment(
        config(tmp_path),
        request(),
        runner=runner,
        sleeper=lambda _: None,
    )


def test_정상_교체는_digest만_사용하고_rollback_tag를_남긴다(tmp_path):
    runner = FakeRunner()
    result = apply(runner, tmp_path)
    assert result["status"] == "deployed"
    assert result["image"] == TARGET_IMAGE
    assert result["rollback_tag"].startswith("conan-dev-rollback:")
    commands = [args for args, _ in runner.calls]
    assert any("stop" in args for args in commands)
    assert sum("up" in args for args in commands) == 1
    flat = "\n".join(" ".join(args) for args in commands)
    for forbidden in (" down ", " prune ", "remove-orphans", "down -v"):
        assert forbidden not in f" {flat} "
    for _, env in runner.calls:
        assert not any(key.startswith("DEEPCHECK_") for key in env)
        assert env["DOCKER_CONTEXT"] == "approved-context"
        assert env["DOCKER_CONFIG"].endswith("/docker-config")
        assert "DOCKER_HOST" not in env
        assert "HOME" not in env
        assert "XDG_CONFIG_HOME" not in env


def test_진행_작업이_있으면_stop_전에_중단한다(tmp_path):
    runner = FakeRunner(busy=True)
    with pytest.raises(dev_deploy.DeploymentError, match="진행 중"):
        apply(runner, tmp_path)
    assert not any("stop" in args or "up" in args for args, _ in runner.calls)


def test_Compose가_target_image를_해석하지_않으면_stop_전에_중단한다(tmp_path):
    runner = FakeRunner(compose_image_mismatch=True)
    with pytest.raises(dev_deploy.DeploymentError, match="CONAN_IMAGE"):
        apply(runner, tmp_path)
    assert not any("stop" in args or "up" in args for args, _ in runner.calls)


def test_Docker_context_endpoint가_다르면_Compose전에_중단한다(tmp_path):
    runner = FakeRunner(wrong_docker_endpoint=True)
    with pytest.raises(dev_deploy.DeploymentError, match="endpoint"):
        apply(runner, tmp_path)
    assert not any("compose" in args for args, _ in runner.calls)


def test_이미_같은_image_ID면_재생성하지_않는다(tmp_path):
    runner = FakeRunner(same_image=True)
    result = apply(runner, tmp_path)
    assert result["status"] == "no-op"
    assert not any("stop" in args or "up" in args for args, _ in runner.calls)


def test_새_컨테이너_검증_실패시_이전_image로_복구한다(tmp_path):
    runner = FakeRunner(target_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="이전 이미지로 복구"):
        apply(runner, tmp_path)
    up_images = [env.get("CONAN_IMAGE") for args, env in runner.calls if "up" in args]
    assert up_images[0] == TARGET_IMAGE
    assert up_images[1].startswith("conan-dev-rollback:")
    assert runner.phase == "rollback"


def test_stop_명령이_실패해도_변경가능성을_보고_복구한다(tmp_path):
    runner = FakeRunner(stop_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="이전 이미지로 복구"):
        apply(runner, tmp_path)
    assert runner.phase == "rollback"


def test_복구까지_실패하면_별도_오류로_드러낸다(tmp_path):
    runner = FakeRunner(target_fails=True, rollback_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="모두 실패"):
        apply(runner, tmp_path)


def test_plan은_Docker를_호출하지_않고_비활성_조건을_표시한다(tmp_path):
    cfg = config(tmp_path, enabled=False, admission_control="unconfigured")
    result = dev_deploy.plan(cfg, request())
    assert result["server_commands_executed"] == 0
    assert len(result["blockers"]) == 3


def test_비활성_호스트_설정은_runtime_조회전에_apply를_거부한다(tmp_path):
    config_path = tmp_path / "host.json"
    request_path = tmp_path / "request.json"
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "enabled": False,
        "repository": "Dynamic-Juo/be",
        "request_authentication": "unconfigured",
        "docker_binary": "/not/contacted/docker",
        "docker_config_dir": "/not/contacted/docker-config",
        "docker_context": "not-contacted",
        "docker_endpoint": "unix:///not/contacted/docker.sock",
        "project": "not-contacted",
        "compose_file": "/not/contacted/compose.yml",
        "env_file": "/not/contacted/.env.home",
        "service": "not-contacted",
        "lock_file": "/not/contacted/deploy.lock",
        "admission_control": "unconfigured",
        "idle_checks": 3,
        "idle_interval_seconds": 5,
        "health_timeout_seconds": 180,
        "health_interval_seconds": 3,
        "stop_timeout_seconds": 660,
        "command_timeout_seconds": 1200,
    }))
    request_path.write_text(json.dumps(request()))
    with pytest.raises(dev_deploy.DeploymentError, match="enabled"):
        dev_deploy.main([
            "--config", str(config_path),
            "--request", str(request_path),
            "apply", "--confirm-maintenance-window",
        ])


def test_request_원본인증이_구현되기전에는_enabled여도_apply를_거부한다(tmp_path):
    config_path = tmp_path / "host.json"
    request_path = tmp_path / "request.json"
    value = json.loads(
        (dev_deploy.ROOT / "deploy/dev-host-config.example.json").read_text()
    )
    value["enabled"] = True
    value["admission_control"] = "external-maintenance"
    config_path.write_text(json.dumps(value))
    request_path.write_text(json.dumps(request()))
    with pytest.raises(dev_deploy.DeploymentError, match="원본 인증"):
        dev_deploy.main([
            "--config", str(config_path),
            "--request", str(request_path),
            "apply", "--confirm-maintenance-window",
        ])


def test_미구현_authentication_표식을_설정만으로_열수없다():
    value = json.loads(
        (dev_deploy.ROOT / "deploy/dev-host-config.example.json").read_text()
    )
    value["request_authentication"] = "verified-github-environment-artifact"
    with pytest.raises(dev_deploy.DeploymentError, match="unconfigured뿐"):
        dev_deploy.DeploymentConfig.from_dict(value)


def test_호스트_설정은_절대경로와_안전한_식별자를_요구한다():
    value = {
        "schema_version": 1,
        "enabled": False,
        "repository": "Dynamic-Juo/be",
        "request_authentication": "unconfigured",
        "docker_binary": "/safe/docker",
        "docker_config_dir": "/safe/docker-config",
        "docker_context": "approved-context",
        "docker_endpoint": "unix:///safe/docker.sock",
        "project": "bad;docker-prune",
        "compose_file": "relative.yml",
        "env_file": "/safe/env",
        "service": "deepcheck-api",
        "lock_file": "/safe/lock",
        "admission_control": "unconfigured",
        "idle_checks": 3,
        "idle_interval_seconds": 5,
        "health_timeout_seconds": 180,
        "health_interval_seconds": 3,
        "stop_timeout_seconds": 660,
        "command_timeout_seconds": 1200,
    }
    with pytest.raises(dev_deploy.DeploymentError):
        dev_deploy.DeploymentConfig.from_dict(value)


def test_호스트_lock_충돌은_동시_배포를_거부한다(tmp_path):
    lock = tmp_path / "deploy.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(dev_deploy.DeploymentError, match="진행 중"):
            with dev_deploy.deployment_lock(lock):
                pass
    finally:
        os.close(descriptor)


def test_lock과_env가_같은_inode면_runtime검증에서_거부한다(tmp_path):
    config_path = tmp_path / "host.json"
    request_path = tmp_path / "request.json"
    compose_path = tmp_path / "compose.yml"
    env_path = tmp_path / ".env.home"
    docker_path = tmp_path / "docker"
    docker_config = tmp_path / "docker-config"
    for path in (config_path, request_path, compose_path, env_path, docker_path):
        path.write_text("placeholder")
    env_path.chmod(0o600)
    docker_path.chmod(0o700)
    docker_config.mkdir(mode=0o700)
    cfg = config(
        tmp_path,
        compose_file=compose_path,
        env_file=env_path,
        docker_binary=docker_path,
        docker_config_dir=docker_config,
        lock_file=env_path,
    )
    with pytest.raises(dev_deploy.DeploymentError, match="경로가 충돌"):
        dev_deploy.validate_runtime(config_path, request_path, cfg)


def test_lock_symlink는_target을_변경하지_않고_거부한다(tmp_path):
    target = tmp_path / "sensitive"
    target.write_text("keep-me")
    target.chmod(0o600)
    lock = tmp_path / "deploy.lock"
    lock.symlink_to(target)
    with pytest.raises(dev_deploy.DeploymentError, match="안전하게 열 수 없다"):
        with dev_deploy.deployment_lock(lock):
            pass
    assert target.read_text() == "keep-me"
