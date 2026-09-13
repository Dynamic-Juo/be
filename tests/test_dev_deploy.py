import fcntl
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

import pytest

from scripts import deployment_state, dev_deploy, release_manifest


REPOSITORY = "Dynamic-Juo/be"
REPOSITORY_ID = 1_355_990_630
REPOSITORY_OWNER_ID = 324_487_638
TARGET_ID = "conan-development"
SOURCE_SHA = "a" * 40
OLD_SOURCE_SHA = "d" * 40
OLD_IMAGE_ID = "sha256:" + "1" * 64
TARGET_IMAGE_ID = "sha256:" + "2" * 64
UNKNOWN_IMAGE_ID = "sha256:" + "9" * 64
TARGET_DIGEST = "sha256:" + "b" * 64
OLD_DIGEST = "sha256:" + "d" * 64
TARGET_IMAGE = f"ghcr.io/dynamic-juo/be@{TARGET_DIGEST}"
OLD_IMAGE = f"ghcr.io/dynamic-juo/be@{OLD_DIGEST}"


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def config(tmp_path, **overrides):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    values = {
        "enabled": True,
        "target_id": TARGET_ID,
        "environment": TARGET_ID,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "backend_ci_workflow_id": 887766,
        "backend_ci_workflow_sha256": "8" * 64,
        "deployment_workflow_id": 998877,
        "deployment_workflow_sha256": "7" * 64,
        "environment_id": 161088068,
        "allowed_reviewer_user_ids": frozenset({7654321, 8888888}),
        "request_authentication": (
            "github-image-and-manifest-attestations-ci-and-environment-review-v3"
        ),
        "gh_binary": tmp_path / "gh",
        "gh_binary_sha256": "3" * 64,
        "gh_config_dir": tmp_path / "gh-config",
        "docker_binary": tmp_path / "docker",
        "docker_binary_sha256": "4" * 64,
        "compose_binary": tmp_path / "docker-compose",
        "compose_binary_sha256": "6" * 64,
        "docker_config_dir": tmp_path / "docker-config",
        "docker_context": "approved-context",
        "docker_endpoint": "unix:///approved/docker.sock",
        "project": "approved-development",
        "compose_file": tmp_path / "compose.yml",
        "trusted_compose_files": (
            dev_deploy.TrustedFile(tmp_path / "compose.yml", "5" * 64),
        ),
        "env_file": tmp_path / ".env.home",
        "service": "deepcheck-api",
        "global_lock_file": tmp_path / "deployment.lock",
        "state_dir": state_dir,
        "bootstrap_active": dev_deploy.BootstrapActive(
            image=OLD_IMAGE,
            source_sha=OLD_SOURCE_SHA,
            image_id=OLD_IMAGE_ID,
            deployment_id="bootstrap-20260913",
            request_run_id=400,
            request_run_attempt=1,
            ci_run_id=100,
            ci_run_attempt=1,
        ),
        "admission_control": "durable-api-drain-v1",
        "idle_checks": 2,
        "idle_timeout_seconds": 10,
        "idle_interval_seconds": 0.1,
        "health_timeout_seconds": 10,
        "health_interval_seconds": 0.1,
        "stop_timeout_seconds": 660,
        "command_timeout_seconds": 1200,
        "mutation_settle_seconds": 1.0,
        "mutation_poll_seconds": 0.5,
    }
    values.update(overrides)
    return dev_deploy.DeploymentConfig(**values)


def request(*, request_run_id=456, request_run_attempt=1, ci_run_id=123,
            ci_run_attempt=1, request_id=None):
    release = release_manifest.build_manifest(
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        source_sha=SOURCE_SHA,
        ci_run_id=str(ci_run_id),
        ci_run_attempt=str(ci_run_attempt),
        image=TARGET_IMAGE,
    )
    return release_manifest.build_deployment_request(
        release=release,
        target_id=TARGET_ID,
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        environment=TARGET_ID,
        request_run_id=str(request_run_id),
        request_run_attempt=str(request_run_attempt),
        request_sha=SOURCE_SHA,
        dispatch_actor="5dotseven",
        dispatch_actor_id="1234567",
        request_id=request_id or str(uuid.UUID(int=request_run_id)),
    )


class FakeRunner:
    """Docker의 관찰 가능한 경계만 흉내 내는 명령 allowlist fake."""

    def __init__(
        self,
        *,
        initial_phase="old",
        admission_state="accepting",
        idle_sequence=None,
        drain_fails=False,
        resume_fails=False,
        stop_fails=False,
        compose_image_mismatch=False,
        wrong_docker_endpoint=False,
        target_fails=False,
        rollback_fails=False,
        target_revision=SOURCE_SHA,
        target_platform="linux/arm64",
        admission_protocol="durable-api-drain-v1",
        ready_status=None,
        ready_probe_fails=False,
        state_dir=None,
    ):
        self.phase = initial_phase
        self.admission_state = admission_state
        self.idle_sequence = list(idle_sequence if idle_sequence is not None else [0, 0])
        self.last_idle = self.idle_sequence[-1] if self.idle_sequence else 0
        self.drain_fails = drain_fails
        self.resume_fails = resume_fails
        self.stop_fails = stop_fails
        self.compose_image_mismatch = compose_image_mismatch
        self.wrong_docker_endpoint = wrong_docker_endpoint
        self.target_fails = target_fails
        self.rollback_fails = rollback_fails
        self.target_revision = target_revision
        self.target_platform = target_platform
        self.admission_protocol = admission_protocol
        self.ready_status = ready_status
        self.ready_probe_fails = ready_probe_fails
        self.state_dir = state_dir
        self.calls = []
        self.tags = {}
        self.ready_samples = []
        self.control_actions = []
        self.up_events = []

    @staticmethod
    def _normalized(args):
        return [Path(args[0]).name, *args[1:]]

    def _active_count(self):
        if self.admission_state == "draining":
            if self.idle_sequence:
                self.last_idle = self.idle_sequence.pop(0)
            return self.last_idle
        return 0

    def _ready(self):
        accepting = self.admission_state == "accepting"
        active = self._active_count()
        status = self.ready_status or (
            "ready" if accepting else "draining" if self.admission_state == "draining"
            else "saturated"
        )
        result = {
            "status": status,
            "harness": {
                "inflight_urls": active,
                "backlog_size": 0,
                "accepting_jobs": accepting,
                "admission_state": self.admission_state,
                "admission_protocol": self.admission_protocol,
            },
        }
        self.ready_samples.append(result)
        return dev_deploy.CommandResult(json.dumps(result) + "\n")

    def run(self, args, *, env):
        self.calls.append((list(args), dict(env)))
        command = self._normalized(args)
        if command[:3] == ["docker", "context", "inspect"]:
            endpoint = (
                "unix:///wrong/docker.sock"
                if self.wrong_docker_endpoint
                else "unix:///approved/docker.sock"
            )
            return dev_deploy.CommandResult(endpoint + "\n")
        if command and command[0] in {"docker-compose", "compose"}:
            if "config" in command:
                if "--images" in command:
                    resolved = "unexpected:image" if self.compose_image_mismatch else env["CONAN_IMAGE"]
                    return dev_deploy.CommandResult(resolved + "\n")
                return dev_deploy.CommandResult()
            if "ps" in command:
                return dev_deploy.CommandResult("" if self.phase == "stopped" else "container-id\n")
            if "exec" in command:
                script = command[command.index("-c") + 1]
                if "/internal/deployment/" in script:
                    action, deployment_id = command[-2:]
                    self.control_actions.append((action, deployment_id))
                    if action == "drain":
                        self.admission_state = "draining"
                        if self.drain_fails:
                            raise dev_deploy.DeploymentError("simulated drain transport failure")
                        return dev_deploy.CommandResult('{"status":"draining","changed":true}\n')
                    if action == "resume":
                        if self.resume_fails:
                            raise dev_deploy.DeploymentError("simulated resume failure")
                        self.admission_state = "accepting"
                        return dev_deploy.CommandResult('{"status":"accepting","changed":true}\n')
                    raise AssertionError(f"unexpected control action: {action}")
                endpoint = command[-1]
                if endpoint.endswith("/health"):
                    return dev_deploy.CommandResult('{"status":"ok"}\n')
                if endpoint.endswith("/ready"):
                    if self.ready_probe_fails:
                        raise dev_deploy.DeploymentError("simulated transient ready failure")
                    return self._ready()
                raise AssertionError(f"unexpected probe endpoint: {endpoint}")
            if "stop" in command:
                self.phase = "stopped"
                if self.stop_fails:
                    raise dev_deploy.DeploymentError("simulated ambiguous stop failure")
                return dev_deploy.CommandResult()
            if "up" in command:
                image = env.get("CONAN_IMAGE", "")
                if image == TARGET_IMAGE:
                    self.phase = "target"
                elif image == OLD_IMAGE:
                    self.phase = "old"
                else:
                    self.phase = "rollback"
                self.admission_state = (
                    "draining" if env.get("DEEPCHECK_START_DRAINED") else "accepting"
                )
                snapshot = {
                    "image": image,
                    "start_drained": env.get("DEEPCHECK_START_DRAINED"),
                    "admission": self.admission_state,
                    "journal": None,
                    "state": None,
                }
                if self.state_dir is not None:
                    for label, name in (("journal", "journal.json"), ("state", "state.json")):
                        path = Path(self.state_dir) / name
                        if path.exists():
                            snapshot[label] = json.loads(path.read_text())
                self.up_events.append(snapshot)
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
                if image == TARGET_IMAGE:
                    image_id = TARGET_IMAGE_ID
                elif image == OLD_IMAGE:
                    image_id = OLD_IMAGE_ID
                else:
                    image_id = self.tags.get(image)
                if image_id is None:
                    raise AssertionError(f"unexpected image inspect: {image}")
                return dev_deploy.CommandResult(image_id + "\n")
            if fmt == "{{.Os}}/{{.Architecture}}":
                return dev_deploy.CommandResult(self.target_platform + "\n")
            if "org.opencontainers.image.revision" in fmt:
                revision = OLD_SOURCE_SHA if image == OLD_IMAGE else self.target_revision
                return dev_deploy.CommandResult(revision + "\n")
        if command[:2] == ["docker", "inspect"]:
            fmt = command[3]
            if fmt == "{{.Image}}":
                image_id = {
                    "old": OLD_IMAGE_ID,
                    "stopped": None,
                    "target": TARGET_IMAGE_ID,
                    "rollback": OLD_IMAGE_ID,
                    "unknown": UNKNOWN_IMAGE_ID,
                }[self.phase]
                if image_id is None:
                    raise AssertionError("stopped container must not be inspected")
                return dev_deploy.CommandResult(image_id + "\n")
            if ".State.Health" in fmt:
                if self.phase == "target" and self.target_fails:
                    return dev_deploy.CommandResult("exited|unhealthy\n")
                if self.phase == "rollback" and self.rollback_fails:
                    return dev_deploy.CommandResult("exited|unhealthy\n")
                return dev_deploy.CommandResult("running|healthy\n")
        raise AssertionError(f"unexpected command: {args}")


def apply(
    runner,
    tmp_path,
    *,
    cfg=None,
    deployment_request=None,
    clock=None,
    seed_state=True,
):
    clock = clock or Clock()
    cfg = cfg or config(tmp_path)
    runner.state_dir = cfg.state_dir
    if seed_state and deployment_state.load_state(cfg.state_file, TARGET_ID) is None:
        bootstrap = cfg.bootstrap_active
        assert bootstrap is not None
        deployment_state.write_state(
            cfg.state_file,
            state_value(
                cfg,
                request_run_id=bootstrap.request_run_id,
                request_run_attempt=bootstrap.request_run_attempt,
                ci_run_id=bootstrap.ci_run_id,
                ci_run_attempt=bootstrap.ci_run_attempt,
                active=bootstrap.as_state_value(),
            ),
            TARGET_ID,
        )
        dev_deploy._write_desired_image(cfg, bootstrap.image)
    return dev_deploy.apply_deployment(
        cfg,
        deployment_request or request(),
        runner=runner,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )


def state_value(cfg, *, request_run_id=455, request_run_attempt=1,
                ci_run_id=122, ci_run_attempt=1, active=None, outcome="committed"):
    return {
        "schema_version": 2,
        "target_id": TARGET_ID,
        "target_fingerprint": cfg.target_fingerprint,
        "active": active or {
            "image": OLD_IMAGE,
            "source_sha": OLD_SOURCE_SHA,
            "image_id": OLD_IMAGE_ID,
            "deployment_id": "previous-deployment",
        },
        "highest_request": {"run_id": request_run_id, "run_attempt": request_run_attempt},
        "highest_ci": {"run_id": ci_run_id, "run_attempt": ci_run_attempt},
        "last_outcome": outcome,
        "updated_at": "2026-09-13T01:02:03+00:00",
    }


def journal_value(
    cfg, deployment_request, *, phase, rollback_tag="conan-dev-rollback:crash"
):
    value = dev_deploy._journal(
        cfg,
        deployment_request,
        previous={
            "image": OLD_IMAGE,
            "source_sha": OLD_SOURCE_SHA,
            "image_id": OLD_IMAGE_ID,
            "deployment_id": "previous-deployment",
        },
        target_image_id=TARGET_IMAGE_ID,
        rollback_tag=rollback_tag,
    )
    value["phase"] = phase
    return value


def test_정상_교체는_drain_idle_digest만_사용하고_durable_commit한다(tmp_path):
    runner = FakeRunner(idle_sequence=[2, 1, 0, 0])
    clock = Clock()
    result = apply(runner, tmp_path, clock=clock)

    assert result["status"] == "deployed"
    assert result["image"] == TARGET_IMAGE
    assert runner.control_actions == [("drain", request()["request_id"])]
    assert [sample["harness"]["inflight_urls"] for sample in runner.ready_samples[:4]] == [2, 1, 0, 0]
    assert runner.phase == "target"
    cfg = config(tmp_path)
    assert cfg.desired_image_env.read_text() == f"CONAN_IMAGE={TARGET_IMAGE}\n"
    saved_state = deployment_state.load_state(cfg.state_file, TARGET_ID)
    saved_journal = deployment_state.load_journal(cfg.journal_file, TARGET_ID)
    assert saved_state["active"]["image_id"] == TARGET_IMAGE_ID
    assert saved_state["last_outcome"] == "committed"
    assert saved_journal["phase"] == "committed"
    assert [event["image"] for event in runner.up_events] == [TARGET_IMAGE, TARGET_IMAGE]
    fenced, published = runner.up_events
    assert fenced["start_drained"] == request()["request_id"]
    assert fenced["admission"] == "draining"
    assert fenced["journal"]["phase"] == "verifying"
    assert fenced["state"]["active"]["image_id"] == OLD_IMAGE_ID
    assert published["start_drained"] == ""
    assert published["admission"] == "accepting"
    assert published["journal"]["phase"] == "committed"
    assert published["state"]["active"]["image_id"] == TARGET_IMAGE_ID
    # accepting 공개 뒤에는 추가 settle/reprobe로 다음 mutation window를 만들지 않는다.
    assert clock.sleeps.count(0.5) == 2


def test_교체명령은_target_service와_보수적_flags로만_제한된다(tmp_path):
    runner = FakeRunner()
    apply(runner, tmp_path)
    commands = [FakeRunner._normalized(args) for args, _ in runner.calls]
    flat = "\n".join(" ".join(args) for args in commands)

    assert not any(word in f" {flat} " for word in (" down ", " prune ", " rm ", "--remove-orphans"))
    stop = next(command for command in commands if "stop" in command)
    up = next(command for command in commands if "up" in command)
    assert stop[-3:] == ["--timeout", "660", "deepcheck-api"]
    assert up[-6:] == ["--force-recreate", "--no-build", "--no-deps", "--pull", "never", "deepcheck-api"]
    assert all("deepcheck-api" in command for command in commands if "exec" in command)
    compose_commands = [command for command in commands if command[0] == "docker-compose"]
    assert compose_commands
    assert not any(command[:2] == ["docker", "compose"] for command in commands)


def test_Docker명령_env는_ambient_secret과_endpoint_override를_상속하지_않는다(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/attacker/home")
    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker:2375")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("DEEPCHECK_DEPLOYMENT_TOKEN", "host-must-not-see-this")
    runner = FakeRunner()
    apply(runner, tmp_path)

    base_keys = {
        "PATH", "DOCKER_CONFIG", "DOCKER_HOST", "CONAN_ENV_FILE",
        "DEEPCHECK_START_DRAINED", "LANG", "LC_ALL",
    }
    for _, environment in runner.calls:
        assert set(environment) in (
            base_keys,
            base_keys | {"CONAN_IMAGE"},
        )
        assert environment["DOCKER_HOST"] == "unix:///approved/docker.sock"
        assert environment["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
        assert "DOCKER_CONTEXT" not in environment
        assert "HOME" not in environment
        assert "GH_TOKEN" not in environment
        assert set(key for key in environment if key.startswith("DEEPCHECK_")) == {
            "DEEPCHECK_START_DRAINED"
        }
        assert "DEEPCHECK_DEPLOYMENT_TOKEN" not in environment


def test_drain후_busy가_timeout되면_resume하고_aborted로_소비한다(tmp_path):
    cfg = config(tmp_path, idle_interval_seconds=5.0)
    runner = FakeRunner(idle_sequence=[1, 1, 1, 1])
    with pytest.raises(dev_deploy.DeploymentError, match="교체 전 안전 조건"):
        apply(runner, tmp_path, cfg=cfg, clock=Clock())

    assert [action for action, _ in runner.control_actions] == ["drain", "resume"]
    assert runner.admission_state == "accepting"
    assert not any("stop" in args or "up" in args for args, _ in runner.calls)
    journal = deployment_state.load_journal(cfg.journal_file, TARGET_ID)
    state = deployment_state.load_state(cfg.state_file, TARGET_ID)
    assert journal["phase"] == "aborted"
    assert state["last_outcome"] == "aborted"
    assert state["highest_request"] == {"run_id": 456, "run_attempt": 1}


def test_drain_transport실패도_resume을_시도하고_aborted로_소비한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(drain_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="교체 전 안전 조건"):
        apply(runner, tmp_path, cfg=cfg)

    assert [action for action, _ in runner.control_actions] == ["drain", "resume"]
    assert runner.admission_state == "accepting"
    assert deployment_state.load_journal(cfg.journal_file, TARGET_ID)["phase"] == "aborted"


def test_resume까지_실패하면_draining_journal을_남겨_recover가능하게_한다(tmp_path):
    cfg = config(tmp_path, idle_interval_seconds=5.0)
    runner = FakeRunner(idle_sequence=[1, 1, 1], resume_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="recover가 필요"):
        apply(runner, tmp_path, cfg=cfg, clock=Clock())

    journal = deployment_state.load_journal(cfg.journal_file, TARGET_ID)
    assert journal["phase"] == "draining"
    assert "resume failure" in journal["error"]
    prior = deployment_state.load_state(cfg.state_file, TARGET_ID)
    assert prior["active"]["image_id"] == OLD_IMAGE_ID
    assert prior["highest_request"] == {"run_id": 400, "run_attempt": 1}


def test_target검증실패는_이전image로_rollback하고_terminal_state를_기록한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(target_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="이전 이미지에 복구"):
        apply(runner, tmp_path, cfg=cfg)

    up_images = [env.get("CONAN_IMAGE") for args, env in runner.calls if "up" in args]
    assert up_images[0] == TARGET_IMAGE
    assert up_images[1].startswith("conan-dev-rollback:")
    assert up_images[2] == OLD_IMAGE
    assert runner.phase == "old"
    assert deployment_state.load_journal(cfg.journal_file, TARGET_ID)["phase"] == "rolled_back"
    state = deployment_state.load_state(cfg.state_file, TARGET_ID)
    assert state["active"]["image_id"] == OLD_IMAGE_ID
    assert state["last_outcome"] == "rolled_back"
    assert cfg.desired_image_env.read_text() == f"CONAN_IMAGE={OLD_IMAGE}\n"
    target_fenced, rollback_fenced, rollback_published = runner.up_events
    assert target_fenced["image"] == TARGET_IMAGE
    assert target_fenced["start_drained"] == request()["request_id"]
    assert target_fenced["journal"]["phase"] == "verifying"
    assert rollback_fenced["image"].startswith("conan-dev-rollback:")
    assert rollback_fenced["start_drained"] == request()["request_id"]
    assert rollback_fenced["admission"] == "draining"
    assert rollback_fenced["journal"]["phase"] == "rolling_back"
    assert rollback_published["image"] == OLD_IMAGE
    assert rollback_published["start_drained"] == ""
    assert rollback_published["admission"] == "accepting"
    assert rollback_published["journal"]["phase"] == "rolled_back"
    assert rollback_published["state"]["last_outcome"] == "rolled_back"


def test_target과_rollback모두_실패하면_rolling_back_journal을_보존한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(target_fails=True, rollback_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="모두 실패"):
        apply(runner, tmp_path, cfg=cfg)

    journal = deployment_state.load_journal(cfg.journal_file, TARGET_ID)
    assert journal["phase"] == "rolling_back"
    assert journal["error"]
    prior = deployment_state.load_state(cfg.state_file, TARGET_ID)
    assert prior["active"]["image_id"] == OLD_IMAGE_ID
    assert prior["highest_request"] == {"run_id": 400, "run_attempt": 1}


def test_stop이_부분실패해도_rollback한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(stop_fails=True)
    with pytest.raises(dev_deploy.DeploymentError, match="이전 이미지에 복구"):
        apply(runner, tmp_path, cfg=cfg)
    assert runner.phase == "old"
    assert deployment_state.load_journal(cfg.journal_file, TARGET_ID)["phase"] == "rolled_back"


def test_동일image_ID_noop도_요청을_commit으로_소비하고_재실행은_idempotent다(tmp_path):
    deployment_request = request()
    target_active = dev_deploy.BootstrapActive(
        image=TARGET_IMAGE,
        source_sha=SOURCE_SHA,
        image_id=TARGET_IMAGE_ID,
        deployment_id="preexisting-target",
        request_run_id=400,
        request_run_attempt=1,
        ci_run_id=100,
        ci_run_attempt=1,
    )
    cfg = config(tmp_path, bootstrap_active=target_active)
    runner = FakeRunner(initial_phase="target")

    first = apply(runner, tmp_path, cfg=cfg, deployment_request=deployment_request)
    second = apply(runner, tmp_path, cfg=cfg, deployment_request=deployment_request)

    assert first["status"] == "no-op"
    assert second["status"] == "already-committed"
    assert not any("stop" in args or "up" in args for args, _ in runner.calls)
    assert deployment_state.load_state(cfg.state_file, TARGET_ID)["highest_request"] == {
        "run_id": 456, "run_attempt": 1,
    }


def test_apply는_bootstrap설정만으로_진행하지_않고_durable_state를_요구한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner()

    with pytest.raises(dev_deploy.DeploymentError, match="initialize"):
        apply(runner, tmp_path, cfg=cfg, seed_state=False)

    assert deployment_state.load_state(cfg.state_file, TARGET_ID) is None
    assert not any("pull" in args or "stop" in args or "up" in args for args, _ in runner.calls)


def test_initialize는_bootstrap_image와_네_watermark를_검증해_일회_state를_만든다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(initial_phase="old", state_dir=cfg.state_dir)
    clock = Clock()

    first = dev_deploy.initialize_deployment(
        cfg, runner=runner, sleeper=clock.sleep, monotonic=clock.monotonic
    )
    second = dev_deploy.initialize_deployment(
        cfg, runner=runner, sleeper=clock.sleep, monotonic=clock.monotonic
    )

    assert first["status"] == "initialized"
    assert second["status"] == "already-initialized"
    state = deployment_state.load_state(cfg.state_file, TARGET_ID)
    assert state["active"] == cfg.bootstrap_active.as_state_value()
    assert state["highest_request"] == {"run_id": 400, "run_attempt": 1}
    assert state["highest_ci"] == {"run_id": 100, "run_attempt": 1}
    assert cfg.desired_image_env.read_text() == f"CONAN_IMAGE={OLD_IMAGE}\n"
    assert not any("up" in args or "stop" in args for args, _ in runner.calls)


@pytest.mark.parametrize(
    "admission_protocol",
    [None, "unavailable", "api-drain-v1", "volatile-api-drain-v1"],
)
def test_initialize는_durable_admission_protocol을_증명하지_못한_bootstrap을_거부한다(
    tmp_path, admission_protocol
):
    cfg = config(tmp_path)
    runner = FakeRunner(
        initial_phase="old",
        admission_protocol=admission_protocol,
    )

    with pytest.raises(dev_deploy.DeploymentError, match="durable admission protocol"):
        dev_deploy.initialize_deployment(cfg, runner=runner)

    assert deployment_state.load_state(cfg.state_file, TARGET_ID) is None
    assert not any(
        "pull" in args or "tag" in args or "stop" in args or "up" in args
        for args, _ in runner.calls
    )


@pytest.mark.parametrize("ready_status", ["ready", "saturated"])
def test_terminal_target이_accepting이면_포화상태여도_recreate하지_않는다(
    tmp_path, ready_status
):
    cfg = config(tmp_path)
    runner = FakeRunner(
        initial_phase="target",
        admission_state="accepting",
        ready_status=ready_status,
    )
    clock = Clock()

    dev_deploy._converge_accepting_image(
        runner,
        cfg,
        image=TARGET_IMAGE,
        image_id=TARGET_IMAGE_ID,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert runner.up_events == []
    assert not any("up" in args for args, _ in runner.calls)


def test_terminal_target이_durable_draining으로_확인될때만_unfenced_recreate한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(initial_phase="target", admission_state="draining")
    clock = Clock()

    dev_deploy._converge_accepting_image(
        runner,
        cfg,
        image=TARGET_IMAGE,
        image_id=TARGET_IMAGE_ID,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert len(runner.up_events) == 1
    assert runner.up_events[0]["start_drained"] == ""
    assert runner.up_events[0]["admission"] == "accepting"
    assert clock.sleeps == [0.5, 0.5]


@pytest.mark.parametrize(
    "runner_options",
    [
        {"admission_state": "closed"},
        {"admission_state": "accepting", "ready_probe_fails": True},
        {"admission_state": "draining", "admission_protocol": "api-drain-v1"},
    ],
)
def test_terminal_target_readiness가_불명확하면_신규job보호를_위해_recreate하지_않는다(
    tmp_path, runner_options
):
    cfg = config(tmp_path)
    runner = FakeRunner(initial_phase="target", **runner_options)
    clock = Clock()

    with pytest.raises(dev_deploy.DeploymentError):
        dev_deploy._converge_accepting_image(
            runner,
            cfg,
            image=TARGET_IMAGE,
            image_id=TARGET_IMAGE_ID,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert runner.up_events == []
    assert not any("up" in args for args, _ in runner.calls)


def test_enabled_false여도_main_recover는_허용한다(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path, enabled=False)
    deployment_state.write_state(cfg.state_file, state_value(cfg), TARGET_ID)
    runner = FakeRunner(initial_phase="old")
    config_path = tmp_path / "host.json"
    config_path.write_text("{}")
    monkeypatch.setattr(dev_deploy, "read_config", lambda path: cfg)
    monkeypatch.setattr(dev_deploy, "validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(dev_deploy, "SubprocessRunner", lambda timeout: runner)

    assert dev_deploy.main(["--config", str(config_path), "recover"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "no-pending-transaction", "target_id": TARGET_ID}


def test_state없는_recover와_state없는_journal_recover를_모두_거부한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner(initial_phase="old")
    with pytest.raises(dev_deploy.DeploymentError, match="initialize"):
        dev_deploy.recover_deployment(cfg, runner=runner)

    deployment_state.write_journal(
        cfg.journal_file,
        journal_value(cfg, request(), phase="prepared"),
        TARGET_ID,
    )
    with pytest.raises(dev_deploy.DeploymentError, match="durable state"):
        dev_deploy.recover_deployment(cfg, runner=runner)


def test_소비한_request보다_오래된_request는_replay로_거부한다(tmp_path):
    cfg = config(tmp_path)
    runner = FakeRunner()
    apply(runner, tmp_path, cfg=cfg, deployment_request=request(request_run_id=500))

    with pytest.raises(dev_deploy.DeploymentError, match="오래된 배포 요청"):
        apply(runner, tmp_path, cfg=cfg, deployment_request=request(request_run_id=499))


def test_새승인요청이어도_현재보다_낮은_CI_run은_downgrade로_거부한다(tmp_path):
    cfg = config(tmp_path)
    deployment_state.write_state(
        cfg.state_file,
        state_value(cfg, request_run_id=500, ci_run_id=200),
        TARGET_ID,
    )
    runner = FakeRunner()
    with pytest.raises(dev_deploy.DeploymentError, match="downgrade"):
        apply(
            runner,
            tmp_path,
            cfg=cfg,
            deployment_request=request(request_run_id=501, ci_run_id=199),
        )
    assert not any("pull" in args or "stop" in args for args, _ in runner.calls)


@pytest.mark.parametrize(
    ("phase", "initial_phase", "admission_state", "expected", "controls"),
    [
        ("prepared", "old", "accepting", "recovered-aborted", []),
        ("draining", "old", "draining", "recovered-aborted", ["resume"]),
        ("switching", "target", "accepting", "recovered-rolled_back", []),
        ("verifying", "target", "accepting", "recovered-rolled_back", []),
    ],
)
def test_crash_journal은_commit전이면_이전image로_보수적으로_복구한다(
    tmp_path, phase, initial_phase, admission_state, expected, controls
):
    cfg = config(tmp_path)
    deployment_request = request()
    journal = journal_value(cfg, deployment_request, phase=phase)
    deployment_state.write_state(cfg.state_file, state_value(cfg), TARGET_ID)
    deployment_state.write_journal(cfg.journal_file, journal, TARGET_ID)
    runner = FakeRunner(initial_phase=initial_phase, admission_state=admission_state)
    runner.tags[journal["rollback_tag"]] = OLD_IMAGE_ID
    clock = Clock()

    result = dev_deploy.recover_deployment(
        cfg, runner=runner, sleeper=clock.sleep, monotonic=clock.monotonic
    )

    assert result["status"] == expected
    assert result["active_image"] == OLD_IMAGE
    assert [action for action, _ in runner.control_actions] == controls
    assert deployment_state.load_state(cfg.state_file, TARGET_ID)["active"]["image_id"] == OLD_IMAGE_ID


def test_committed_journal은_prior_state에서_target으로_forward_recover한다(tmp_path):
    cfg = config(tmp_path)
    journal = journal_value(cfg, request(), phase="committed")
    deployment_state.write_state(cfg.state_file, state_value(cfg), TARGET_ID)
    deployment_state.write_journal(cfg.journal_file, journal, TARGET_ID)
    runner = FakeRunner(initial_phase="old")
    clock = Clock()

    result = dev_deploy.recover_deployment(
        cfg, runner=runner, sleeper=clock.sleep, monotonic=clock.monotonic
    )

    assert result["status"] == "recovered-committed"
    assert result["active_image"] == TARGET_IMAGE
    assert runner.phase == "target"
    assert deployment_state.load_state(cfg.state_file, TARGET_ID)["active"]["image_id"] == TARGET_IMAGE_ID


def test_rollback_tag가_journal_previous_ID와_다르면_recover를_fail_closed한다(tmp_path):
    cfg = config(tmp_path)
    journal = journal_value(cfg, request(), phase="switching")
    deployment_state.write_state(cfg.state_file, state_value(cfg), TARGET_ID)
    deployment_state.write_journal(cfg.journal_file, journal, TARGET_ID)
    runner = FakeRunner(initial_phase="target")
    runner.tags[journal["rollback_tag"]] = "sha256:" + "9" * 64

    with pytest.raises(dev_deploy.DeploymentError, match="rollback tag"):
        dev_deploy.recover_deployment(cfg, runner=runner)
    assert runner.phase == "target"


def test_journal범위밖_실행image는_덮어쓰지_않고_recover를_fail_closed한다(tmp_path):
    cfg = config(tmp_path)
    deployment_state.write_state(cfg.state_file, state_value(cfg), TARGET_ID)
    dev_deploy._write_desired_image(cfg, OLD_IMAGE)
    pending = journal_value(cfg, request(), phase="switching")
    deployment_state.write_journal(cfg.journal_file, pending, TARGET_ID)
    runner = FakeRunner(initial_phase="unknown")
    runner.tags[pending["rollback_tag"]] = OLD_IMAGE_ID
    clock = Clock()

    with pytest.raises(dev_deploy.DeploymentError, match="이전/대상 identity"):
        dev_deploy.recover_deployment(
            cfg, runner=runner, sleeper=clock.sleep, monotonic=clock.monotonic
        )
    assert runner.phase == "unknown"
    assert runner.up_events == []


def test_state_watermark보다_오래된_stale_journal은_복구하지_않는다(tmp_path):
    cfg = config(tmp_path)
    deployment_state.write_state(
        cfg.state_file,
        state_value(cfg, request_run_id=500, ci_run_id=200),
        TARGET_ID,
    )
    stale = journal_value(
        cfg,
        request(request_run_id=501, ci_run_id=199),
        phase="prepared",
    )
    deployment_state.write_journal(cfg.journal_file, stale, TARGET_ID)
    runner = FakeRunner(initial_phase="old")

    with pytest.raises(dev_deploy.DeploymentError, match="replay watermark보다 오래"):
        dev_deploy.recover_deployment(cfg, runner=runner)

    assert not any("up" in args or "stop" in args for args, _ in runner.calls)


def test_global_lock_충돌은_다른_target이어도_동시배포를_거부한다(tmp_path):
    lock = tmp_path / "global-deployment.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(dev_deploy.DeploymentError, match="진행 중"):
            with dev_deploy.deployment_lock(lock):
                pass
    finally:
        os.close(descriptor)


def test_lock_symlink는_target을_변경하지_않고_거부한다(tmp_path):
    target = tmp_path / "sensitive"
    target.write_text("keep-me")
    target.chmod(0o600)
    lock = tmp_path / "deployment.lock"
    lock.symlink_to(target)
    with pytest.raises(dev_deploy.DeploymentError, match="안전하게 열 수 없다"):
        with dev_deploy.deployment_lock(lock):
            pass
    assert target.read_text() == "keep-me"


def config_dict(tmp_path, **overrides):
    compose = str(tmp_path / "compose.yml")
    value = {
        "schema_version": 4,
        "enabled": False,
        "target_id": TARGET_ID,
        "environment": TARGET_ID,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "backend_ci_workflow_id": 887766,
        "backend_ci_workflow_sha256": "8" * 64,
        "deployment_workflow_id": 998877,
        "deployment_workflow_sha256": "7" * 64,
        "environment_id": 161088068,
        "allowed_reviewer_user_ids": [7654321, 8888888],
        "request_authentication": (
            "github-image-and-manifest-attestations-ci-and-environment-review-v3"
        ),
        "gh_binary": str(tmp_path / "gh"),
        "gh_binary_sha256": "3" * 64,
        "gh_config_dir": str(tmp_path / "gh-config"),
        "docker_binary": str(tmp_path / "docker"),
        "docker_binary_sha256": "4" * 64,
        "compose_binary": str(tmp_path / "docker-compose"),
        "compose_binary_sha256": "6" * 64,
        "docker_config_dir": str(tmp_path / "docker-config"),
        "docker_context": "approved-context",
        "docker_endpoint": "unix:///approved/docker.sock",
        "project": "approved-development",
        "compose_file": compose,
        "trusted_compose_files": [{"path": compose, "sha256": "5" * 64}],
        "env_file": str(tmp_path / ".env.home"),
        "service": "deepcheck-api",
        "global_lock_file": str(tmp_path / "deployment.lock"),
        "state_dir": str(tmp_path / "state"),
        "bootstrap_active": {
            "image": OLD_IMAGE,
            "source_sha": OLD_SOURCE_SHA,
            "image_id": OLD_IMAGE_ID,
            "deployment_id": "bootstrap-20260913",
            "request_run_id": 400,
            "request_run_attempt": 1,
            "ci_run_id": 100,
            "ci_run_attempt": 1,
        },
        "admission_control": "durable-api-drain-v1",
        "idle_checks": 2,
        "idle_timeout_seconds": 10,
        "idle_interval_seconds": 0.1,
        "health_timeout_seconds": 10,
        "health_interval_seconds": 0.1,
        "stop_timeout_seconds": 660,
        "command_timeout_seconds": 1200,
        "mutation_settle_seconds": 1.0,
        "mutation_poll_seconds": 0.5,
    }
    value.update(overrides)
    return value


def contract_drift(cfg, tmp_path, field):
    changed_path = tmp_path / f"changed-{field}"
    changes = {
        "repository_identity": {"repository_id": cfg.repository_id + 1},
        "environment": {"environment": "other-development"},
        "backend_ci_workflow_id": {
            "backend_ci_workflow_id": cfg.backend_ci_workflow_id + 1,
        },
        "backend_ci_workflow_sha256": {"backend_ci_workflow_sha256": "9" * 64},
        "deployment_workflow_id": {
            "deployment_workflow_id": cfg.deployment_workflow_id + 1,
        },
        "deployment_workflow_sha256": {"deployment_workflow_sha256": "8" * 64},
        "environment_id": {"environment_id": cfg.environment_id + 1},
        "reviewer_ids": {
            "allowed_reviewer_user_ids": frozenset({9999999}),
        },
        "gh_binary_path": {"gh_binary": changed_path},
        "gh_binary_hash": {"gh_binary_sha256": "7" * 64},
        "gh_config_dir": {"gh_config_dir": changed_path},
        "docker_endpoint": {"docker_endpoint": "unix:///changed/docker.sock"},
        "docker_context": {"docker_context": "changed-context"},
        "project": {"project": "changed-development"},
        "service": {"service": "changed-api"},
        "compose_hash": {
            "trusted_compose_files": (
                dev_deploy.TrustedFile(cfg.compose_file, "7" * 64),
            ),
        },
        "compose_path": {
            "compose_file": changed_path,
            "trusted_compose_files": (
                dev_deploy.TrustedFile(changed_path, "5" * 64),
            ),
        },
        "env_path": {"env_file": changed_path},
        "docker_binary_path": {"docker_binary": changed_path},
        "compose_binary_hash": {"compose_binary_sha256": "7" * 64},
        "docker_config_dir": {"docker_config_dir": changed_path},
        "global_lock_file": {"global_lock_file": changed_path},
        "state_dir": {"state_dir": changed_path},
        "admission_control": {"admission_control": "durable-api-drain-v2"},
    }
    return replace(cfg, **changes[field])


@pytest.mark.parametrize("field", [
    "repository_identity", "environment", "backend_ci_workflow_id",
    "deployment_workflow_id",
    "environment_id", "docker_endpoint", "project", "service", "global_lock_file",
    "state_dir", "admission_control",
])
def test_장기_target_identity값만_target_fingerprint를_바꾼다(tmp_path, field):
    cfg = config(tmp_path)
    changed = contract_drift(cfg, tmp_path, field)

    assert len(cfg.target_fingerprint) == 64
    assert cfg.target_fingerprint != changed.target_fingerprint
    assert cfg.transaction_fingerprint == changed.transaction_fingerprint


@pytest.mark.parametrize("field", [
    "docker_context", "compose_hash", "compose_path", "env_path",
    "docker_binary_path", "compose_binary_hash", "docker_config_dir",
])
def test_교체도구와파일값만_transaction_fingerprint를_바꾼다(tmp_path, field):
    cfg = config(tmp_path)
    changed = contract_drift(cfg, tmp_path, field)

    assert len(cfg.transaction_fingerprint) == 64
    assert cfg.target_fingerprint == changed.target_fingerprint
    assert cfg.transaction_fingerprint != changed.transaction_fingerprint


@pytest.mark.parametrize("field", [
    "backend_ci_workflow_sha256", "deployment_workflow_sha256", "reviewer_ids",
])
def test_mutable_request_auth_policy는_durable_fingerprint에서_제외한다(tmp_path, field):
    cfg = config(tmp_path)
    changed = contract_drift(cfg, tmp_path, field)

    assert cfg.target_fingerprint == changed.target_fingerprint
    assert cfg.transaction_fingerprint == changed.transaction_fingerprint


def test_enabled_timeout_bootstrap은_controller_target_fingerprint에서_제외한다(tmp_path):
    cfg = config(tmp_path)
    changed = replace(
        cfg,
        enabled=False,
        bootstrap_active=None,
        idle_checks=20,
        idle_timeout_seconds=7200,
        idle_interval_seconds=60.0,
        health_timeout_seconds=1800,
        health_interval_seconds=30.0,
        stop_timeout_seconds=1800,
        command_timeout_seconds=3600,
        mutation_settle_seconds=120.0,
        mutation_poll_seconds=10.0,
    )

    assert changed.target_fingerprint == cfg.target_fingerprint
    assert changed.transaction_fingerprint == cfg.transaction_fingerprint


@pytest.mark.parametrize("field", ["gh_binary_path", "gh_binary_hash", "gh_config_dir"])
def test_GH검증기위치는_durable_recovery_fingerprint에서_제외한다(tmp_path, field):
    cfg = config(tmp_path)
    changed = contract_drift(cfg, tmp_path, field)

    assert changed.target_fingerprint == cfg.target_fingerprint
    assert changed.transaction_fingerprint == cfg.transaction_fingerprint


@pytest.mark.parametrize("operation", ["apply", "recover"])
@pytest.mark.parametrize(
    "field",
    [
        "repository_identity", "environment", "deployment_workflow_id",
        "backend_ci_workflow_id",
        "environment_id", "docker_endpoint", "project", "service", "global_lock_file",
        "state_dir", "admission_control",
    ],
)
def test_target_identity_drift는_apply와_recover의_Docker호출전에_fail_closed한다(
    tmp_path, field, operation
):
    original = config(tmp_path)
    deployment_state.write_state(
        original.state_file,
        state_value(original),
        TARGET_ID,
        original.target_fingerprint,
    )
    changed = contract_drift(original, tmp_path, field)
    if changed.state_dir != original.state_dir:
        changed.state_dir.mkdir(mode=0o700)
        # 운영자가 기존 state를 새 authority 경로로 복사해도 원래 계약 지문은
        # 유지되므로 새 target에 재사용할 수 없어야 한다.
        deployment_state.write_state(changed.state_file, state_value(original))
    runner = FakeRunner(initial_phase="old")

    with pytest.raises(dev_deploy.DeploymentError, match="controller target"):
        if operation == "apply":
            dev_deploy.apply_deployment(changed, request(), runner=runner)
        else:
            dev_deploy.recover_deployment(changed, runner=runner)

    assert runner.calls == []


@pytest.mark.parametrize("operation", ["apply", "recover"])
@pytest.mark.parametrize("field", [
    "docker_context", "compose_hash", "compose_path", "env_path",
    "docker_binary_path", "compose_binary_hash", "docker_config_dir",
])
def test_pending_transaction_drift는_apply와_recover의_Docker호출전에_fail_closed한다(
    tmp_path, field, operation
):
    original = config(tmp_path)
    deployment_state.write_state(original.state_file, state_value(original))
    deployment_state.write_journal(
        original.journal_file,
        journal_value(original, request(), phase="switching"),
    )
    changed = contract_drift(original, tmp_path, field)
    runner = FakeRunner(initial_phase="target")

    with pytest.raises(dev_deploy.DeploymentError, match="transaction 계약"):
        if operation == "apply":
            dev_deploy.apply_deployment(changed, request(), runner=runner)
        else:
            dev_deploy.recover_deployment(changed, runner=runner)

    assert runner.calls == []


def test_journal의_target_identity도_recover전에_검증한다(tmp_path):
    original = config(tmp_path)
    changed = contract_drift(original, tmp_path, "docker_endpoint")
    deployment_state.write_state(changed.state_file, state_value(changed))
    deployment_state.write_journal(
        changed.journal_file,
        journal_value(original, request(), phase="switching"),
    )
    runner = FakeRunner(initial_phase="target")

    with pytest.raises(dev_deploy.DeploymentError, match="controller target"):
        dev_deploy.recover_deployment(changed, runner=runner)

    assert runner.calls == []


def test_state미반영_terminal도_transaction_drift면_Docker호출전에_거부한다(tmp_path):
    original = config(tmp_path)
    deployment_state.write_state(original.state_file, state_value(original))
    deployment_state.write_journal(
        original.journal_file,
        journal_value(original, request(), phase="committed"),
    )
    changed = contract_drift(original, tmp_path, "compose_hash")
    runner = FakeRunner(initial_phase="target", admission_state="draining")

    with pytest.raises(dev_deploy.DeploymentError, match="transaction 계약"):
        dev_deploy.recover_deployment(changed, runner=runner)

    assert runner.calls == []


def test_state반영_terminal의_오래된_transaction은_read_only검증후_다음apply가교체한다(
    tmp_path
):
    original = config(tmp_path)
    deployment_request = request()
    active = {
        "image": TARGET_IMAGE,
        "source_sha": SOURCE_SHA,
        "image_id": TARGET_IMAGE_ID,
        "deployment_id": deployment_request["request_id"],
    }
    deployment_state.write_state(
        original.state_file,
        state_value(
            original,
            request_run_id=456,
            ci_run_id=123,
            active=active,
        ),
    )
    deployment_state.write_journal(
        original.journal_file,
        journal_value(original, deployment_request, phase="committed"),
    )
    dev_deploy._write_desired_image(original, TARGET_IMAGE)
    changed = contract_drift(original, tmp_path, "compose_hash")
    runner = FakeRunner(initial_phase="target", admission_state="accepting")

    recovered = dev_deploy.recover_deployment(changed, runner=runner)

    assert recovered["status"] == "recovered-committed"
    assert runner.control_actions == []
    assert not any(
        "pull" in args or "tag" in args or "stop" in args or "up" in args
        for args, _ in runner.calls
    )

    runner.calls.clear()
    result = dev_deploy.apply_deployment(
        changed,
        request(request_run_id=457),
        runner=runner,
    )
    assert result["status"] == "no-op"
    journal = deployment_state.load_journal(changed.journal_file, TARGET_ID)
    assert journal["transaction_fingerprint"] == changed.transaction_fingerprint


@pytest.mark.parametrize(
    "runner_options",
    [
        {"initial_phase": "old", "admission_state": "accepting"},
        {
            "initial_phase": "target",
            "admission_state": "accepting",
            "admission_protocol": "api-drain-v1",
        },
    ],
)
def test_state반영_terminal의_오래된transaction도_runtime증명이안되면_변경하지않는다(
    tmp_path, runner_options
):
    original = config(tmp_path)
    deployment_request = request()
    active = {
        "image": TARGET_IMAGE,
        "source_sha": SOURCE_SHA,
        "image_id": TARGET_IMAGE_ID,
        "deployment_id": deployment_request["request_id"],
    }
    deployment_state.write_state(
        original.state_file,
        state_value(
            original,
            request_run_id=456,
            ci_run_id=123,
            active=active,
        ),
    )
    deployment_state.write_journal(
        original.journal_file,
        journal_value(original, deployment_request, phase="committed"),
    )
    changed = contract_drift(original, tmp_path, "compose_hash")
    runner = FakeRunner(**runner_options)

    with pytest.raises(dev_deploy.DeploymentError):
        dev_deploy.recover_deployment(changed, runner=runner)

    assert runner.control_actions == []
    assert not any("stop" in args or "up" in args for args, _ in runner.calls)


def test_schema_v4_config는_exact_keys_identity_hash와_절대경로를_요구한다(tmp_path):
    parsed = dev_deploy.DeploymentConfig.from_dict(config_dict(tmp_path))
    assert parsed.target_id == TARGET_ID
    assert parsed.repository_id == REPOSITORY_ID
    assert parsed.backend_ci_workflow_id == 887766
    assert parsed.backend_ci_workflow_sha256 == "8" * 64
    assert parsed.deployment_workflow_id == 998877
    assert parsed.deployment_workflow_sha256 == "7" * 64
    assert parsed.environment_id == 161088068
    assert parsed.allowed_reviewer_user_ids == frozenset({7654321, 8888888})
    assert parsed.request_authentication == (
        "github-image-and-manifest-attestations-ci-and-environment-review-v3"
    )
    assert parsed.compose_binary == tmp_path / "docker-compose"
    assert parsed.bootstrap_active.request_run_id == 400
    assert parsed.bootstrap_active.request_run_attempt == 1
    assert parsed.bootstrap_active.ci_run_id == 100
    assert parsed.bootstrap_active.ci_run_attempt == 1
    assert parsed.mutation_settle_seconds == 1.0
    assert parsed.mutation_poll_seconds == 0.5
    assert parsed.admission_control == "durable-api-drain-v1"

    with pytest.raises(dev_deploy.DeploymentError, match="미지원"):
        dev_deploy.DeploymentConfig.from_dict(config_dict(tmp_path, command="docker system prune"))
    missing = config_dict(tmp_path)
    del missing["docker_endpoint"]
    with pytest.raises(dev_deploy.DeploymentError, match="누락"):
        dev_deploy.DeploymentConfig.from_dict(missing)

    missing_watermark = config_dict(tmp_path)
    del missing_watermark["bootstrap_active"]["ci_run_attempt"]
    with pytest.raises(dev_deploy.DeploymentError, match="bootstrap_active"):
        dev_deploy.DeploymentConfig.from_dict(missing_watermark)

    second_compose = str(tmp_path / "other.yml")
    multiple = config_dict(tmp_path)
    multiple["trusted_compose_files"].append({
        "path": second_compose,
        "sha256": "7" * 64,
    })
    with pytest.raises(dev_deploy.DeploymentError, match="flattened"):
        dev_deploy.DeploymentConfig.from_dict(multiple)


@pytest.mark.parametrize("updates", [
    {"environment": "other-target"},
    {"repository_id": True},
    {"backend_ci_workflow_id": True},
    {"backend_ci_workflow_sha256": "A" * 64},
    {"deployment_workflow_id": True},
    {"deployment_workflow_sha256": "A" * 64},
    {"environment_id": 0},
    {"allowed_reviewer_user_ids": []},
    {"allowed_reviewer_user_ids": [7654321, 7654321]},
    {"allowed_reviewer_user_ids": [True]},
    {"request_authentication": "trust-me"},
    {"admission_control": "api-drain-v1"},
    {"admission_control": "external-maintenance"},
    {"project": "bad;docker-prune"},
    {"compose_file": "relative.yml"},
    {"compose_file": "/approved//compose.yml"},
    {"env_file": "/approved/./env"},
    {"compose_binary": "relative-compose"},
    {"docker_binary_sha256": "A" * 64},
    {"compose_binary_sha256": "A" * 64},
    {"docker_endpoint": "unix:///approved/../docker.sock"},
    {"docker_endpoint": "unix:////approved/docker.sock"},
    {"docker_endpoint": "unix:///approved/./docker.sock"},
    {"mutation_settle_seconds": 0.5},
    {"mutation_poll_seconds": 0.01},
])
def test_config의_신뢰경계값이_다르면_fail_closed한다(tmp_path, updates):
    with pytest.raises(dev_deploy.DeploymentError):
        dev_deploy.DeploymentConfig.from_dict(config_dict(tmp_path, **updates))


def test_CLI_path도_parser에서_alias를_거부한다(tmp_path):
    with pytest.raises(SystemExit):
        dev_deploy._parser().parse_args([
            "--config", f"{tmp_path}/./host.json", "initialize",
        ])


def test_runtime은_Docker와_standalone_Compose_binary및_flattened_file_hash를_검증한다(
    tmp_path, monkeypatch
):
    compose_bytes = b"services: {}\n"
    docker_bytes = b"#!/bin/sh\nexit 1\n"
    compose_binary_bytes = b"#!/bin/sh\nexit 1\n"
    gh_bytes = b"#!/bin/sh\nexit 1\n"
    paths = {
        "config": tmp_path / "host.json",
        "compose": tmp_path / "compose.yml",
        "env": tmp_path / ".env.home",
        "docker": tmp_path / "docker",
        "compose_binary": tmp_path / "docker-compose",
        "gh": tmp_path / "gh",
    }
    paths["config"].write_text("{}")
    paths["compose"].write_bytes(compose_bytes)
    paths["env"].write_text("SECRET=value\n")
    paths["env"].chmod(0o600)
    for key, content in (
        ("docker", docker_bytes),
        ("compose_binary", compose_binary_bytes),
        ("gh", gh_bytes),
    ):
        paths[key].write_bytes(content)
        paths[key].chmod(0o700)
    for directory in (tmp_path / "docker-config", tmp_path / "gh-config", tmp_path / "state"):
        directory.mkdir(mode=0o700)
    real_socket_check = dev_deploy.stat_trusted_unix_socket
    monkeypatch.setattr(dev_deploy, "stat_trusted_unix_socket", lambda path: None)
    cfg = config(
        tmp_path,
        compose_file=paths["compose"],
        trusted_compose_files=(
            dev_deploy.TrustedFile(paths["compose"], _sha256_bytes(compose_bytes)),
        ),
        env_file=paths["env"],
        docker_binary=paths["docker"],
        docker_binary_sha256=_sha256_bytes(docker_bytes),
        compose_binary=paths["compose_binary"],
        compose_binary_sha256=_sha256_bytes(compose_binary_bytes),
        gh_binary=paths["gh"],
        gh_binary_sha256=_sha256_bytes(gh_bytes),
    )
    dev_deploy.validate_runtime(paths["config"], cfg, require_gh=True)

    paths["compose"].write_text("services:\n  attacker: {}\n")
    with pytest.raises(dev_deploy.DeploymentError, match="Compose 파일 hash"):
        dev_deploy.validate_runtime(paths["config"], cfg, require_gh=True)

    paths["compose"].write_bytes(compose_bytes)
    paths["compose_binary"].write_bytes(b"#!/bin/sh\ncompromised\n")
    paths["compose_binary"].chmod(0o700)
    with pytest.raises(dev_deploy.DeploymentError, match="Compose binary hash"):
        dev_deploy.validate_runtime(paths["config"], cfg, require_gh=True)

    paths["compose_binary"].write_bytes(compose_binary_bytes)
    paths["compose_binary"].chmod(0o700)
    fake_socket = tmp_path / "not-a-socket"
    fake_socket.write_bytes(b"regular file")
    monkeypatch.setattr(dev_deploy, "stat_trusted_unix_socket", real_socket_check)
    with pytest.raises(dev_deploy.DeploymentError, match="Docker unix endpoint"):
        dev_deploy.validate_runtime(
            paths["config"],
            replace(cfg, docker_endpoint=f"unix://{fake_socket}"),
            require_gh=True,
        )


@pytest.mark.parametrize("directive", ["include: ./other.yml", "    extends: base"])
def test_runtime은_hash가맞아도_include와_extends가있는_Compose를_거부한다(
    tmp_path, directive
):
    compose_bytes = f"services:\n  api:\n{directive}\n".encode()
    docker_bytes = b"#!/bin/sh\n"
    compose_binary_bytes = b"#!/bin/sh\n"
    config_path = tmp_path / "host.json"
    compose_path = tmp_path / "compose.yml"
    env_path = tmp_path / ".env.home"
    docker_path = tmp_path / "docker"
    compose_binary_path = tmp_path / "docker-compose"
    config_path.write_text("{}")
    compose_path.write_bytes(compose_bytes)
    env_path.write_text("SECRET=value\n")
    env_path.chmod(0o600)
    for path, content in (
        (docker_path, docker_bytes),
        (compose_binary_path, compose_binary_bytes),
    ):
        path.write_bytes(content)
        path.chmod(0o700)
    (tmp_path / "docker-config").mkdir(mode=0o700)
    cfg = config(
        tmp_path,
        compose_file=compose_path,
        trusted_compose_files=(
            dev_deploy.TrustedFile(compose_path, _sha256_bytes(compose_bytes)),
        ),
        env_file=env_path,
        docker_binary=docker_path,
        docker_binary_sha256=_sha256_bytes(docker_bytes),
        compose_binary=compose_binary_path,
        compose_binary_sha256=_sha256_bytes(compose_binary_bytes),
    )

    with pytest.raises(dev_deploy.DeploymentError, match="include/extends"):
        dev_deploy.validate_runtime(config_path, cfg)


def test_runtime은_protected_path_inode_alias를_거부한다(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "host.json"
    compose_path = tmp_path / "compose.yml"
    env_path = tmp_path / ".env.home"
    docker_path = tmp_path / "docker"
    compose_binary_path = tmp_path / "docker-compose"
    config_path.write_text("{}")
    compose_path.write_text("services: {}\n")
    env_path.write_text("SECRET=value\n")
    env_path.chmod(0o600)
    docker_path.write_text("#!/bin/sh\n")
    docker_path.chmod(0o700)
    compose_binary_path.write_text("#!/bin/sh\n")
    compose_binary_path.chmod(0o700)
    (tmp_path / "docker-config").mkdir(mode=0o700)
    monkeypatch.setattr(dev_deploy, "stat_trusted_unix_socket", lambda path: None)
    cfg = config(
        tmp_path,
        compose_file=compose_path,
        trusted_compose_files=(dev_deploy.TrustedFile(compose_path, _sha256_bytes(compose_path.read_bytes())),),
        env_file=env_path,
        docker_binary=docker_path,
        docker_binary_sha256=_sha256_bytes(docker_path.read_bytes()),
        compose_binary=compose_binary_path,
        compose_binary_sha256=_sha256_bytes(compose_binary_path.read_bytes()),
        global_lock_file=env_path,
    )
    with pytest.raises(dev_deploy.DeploymentError, match="경로가 충돌"):
        dev_deploy.validate_runtime(config_path, cfg)


def test_attestation검증은_세_bundle과_exact_workflow_identity를_고정하고_GH_env를_sanitize한다(
    tmp_path, monkeypatch
):
    calls = []

    def capture_verify(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(dev_deploy.attestation, "verify", capture_verify)
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    cfg = config(tmp_path)
    req = request()
    dev_deploy._verify_attestations(
        object(),
        cfg,
        req,
        request_path=tmp_path / "request.json",
        request_attestation_path=tmp_path / "request.sigstore.json",
        release_path=tmp_path / "release.json",
        release_attestation_path=tmp_path / "release.sigstore.json",
        image_attestation_path=tmp_path / "image.sigstore.json",
    )

    assert [call["bundle"].name for call in calls] == [
        "request.sigstore.json", "release.sigstore.json", "image.sigstore.json",
    ]
    assert [call["policy"].workflow_path for call in calls] == [
        release_manifest.DEPLOYMENT_WORKFLOW_PATH,
        release_manifest.WORKFLOW_PATH,
        release_manifest.WORKFLOW_PATH,
    ]
    assert [call["policy"].event for call in calls] == [
        "workflow_dispatch", "push", "push",
    ]
    assert calls[2]["subject"] == f"oci://{TARGET_IMAGE}"
    assert calls[1]["policy"] is calls[2]["policy"]
    for call in calls[:2]:
        assert call["policy"].repository_id == REPOSITORY_ID
        assert call["policy"].repository_owner_id == REPOSITORY_OWNER_ID
        assert set(call["env"]) == {
            "PATH", "GH_CONFIG_DIR", "GH_PROMPT_DISABLED", "NO_COLOR", "LANG", "LC_ALL",
        }
        assert "GH_TOKEN" not in call["env"]
        assert "DOCKER_CONFIG" not in call["env"]
    assert calls[2]["policy"].repository_id == REPOSITORY_ID
    assert calls[2]["policy"].repository_owner_id == REPOSITORY_OWNER_ID
    assert calls[2]["env"]["DOCKER_CONFIG"] == str(cfg.docker_config_dir)
    assert "GH_TOKEN" not in calls[2]["env"]


def test_GitHub_CI검증은_exact_run_workflow와_host_pin을_고정한다(
    tmp_path, monkeypatch
):
    observed = {}

    def capture_verify_ci(**kwargs):
        observed.update(kwargs)

    monkeypatch.setattr(dev_deploy.github_approval, "verify_ci", capture_verify_ci)
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    cfg = config(tmp_path)
    req = request(ci_run_id=321, ci_run_attempt=4)

    dev_deploy._verify_github_ci(object(), cfg, req)

    policy = observed["policy"]
    assert policy.repository == REPOSITORY
    assert policy.repository_id == REPOSITORY_ID
    assert policy.repository_owner_id == REPOSITORY_OWNER_ID
    assert policy.workflow_id == cfg.backend_ci_workflow_id
    assert policy.workflow_path == release_manifest.WORKFLOW_PATH
    assert policy.workflow_sha256 == cfg.backend_ci_workflow_sha256
    assert policy.source_sha == SOURCE_SHA
    assert policy.run_id == 321
    assert policy.run_attempt == 4
    assert observed["gh_binary"] == cfg.gh_binary
    assert set(observed["env"]) == {
        "PATH", "GH_CONFIG_DIR", "GH_PROMPT_DISABLED", "NO_COLOR", "LANG", "LC_ALL",
    }
    assert "GH_TOKEN" not in observed["env"]
    assert "DOCKER_CONFIG" not in observed["env"]


def test_GitHub_CI불일치는_DeploymentError로_fail_closed한다(tmp_path, monkeypatch):
    def reject(**kwargs):
        raise dev_deploy.github_approval.ApprovalError("rejected")

    monkeypatch.setattr(dev_deploy.github_approval, "verify_ci", reject)
    with pytest.raises(dev_deploy.DeploymentError, match="CI provenance 검증 실패"):
        dev_deploy._verify_github_ci(object(), config(tmp_path), request())


def test_GitHub_environment승인은_exact_run_actor와_host_allowlist를_고정한다(
    tmp_path, monkeypatch
):
    observed = {}

    def capture_verify(**kwargs):
        observed.update(kwargs)

    monkeypatch.setattr(dev_deploy.github_approval, "verify", capture_verify)
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    cfg = config(tmp_path)
    req = request()

    dev_deploy._verify_github_environment_approval(object(), cfg, req)

    policy = observed["policy"]
    assert policy.repository == REPOSITORY
    assert policy.repository_id == REPOSITORY_ID
    assert policy.repository_owner_id == REPOSITORY_OWNER_ID
    assert policy.workflow_id == cfg.deployment_workflow_id
    assert policy.workflow_path == release_manifest.DEPLOYMENT_WORKFLOW_PATH
    assert policy.workflow_sha256 == cfg.deployment_workflow_sha256
    assert policy.source_sha == SOURCE_SHA
    assert policy.run_id == req["request_run_id"]
    assert policy.run_attempt == 1
    assert policy.environment == TARGET_ID
    assert policy.environment_id == cfg.environment_id
    assert policy.dispatch_actor == "5dotseven"
    assert policy.dispatch_actor_id == 1234567
    assert policy.allowed_reviewer_user_ids == cfg.allowed_reviewer_user_ids
    assert observed["gh_binary"] == cfg.gh_binary
    assert set(observed["env"]) == {
        "PATH", "GH_CONFIG_DIR", "GH_PROMPT_DISABLED", "NO_COLOR", "LANG", "LC_ALL",
    }
    assert "GH_TOKEN" not in observed["env"]


def test_GitHub_environment승인_불일치는_DeploymentError로_fail_closed한다(
    tmp_path, monkeypatch
):
    def reject(**kwargs):
        raise dev_deploy.github_approval.ApprovalError("rejected")

    monkeypatch.setattr(dev_deploy.github_approval, "verify", reject)
    with pytest.raises(dev_deploy.DeploymentError, match="environment 승인 검증 실패"):
        dev_deploy._verify_github_environment_approval(
            object(), config(tmp_path), request()
        )


def test_apply_main은_exact_five_file_snapshot과검증순서를_Docker전에고정하고_정리한다(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    cfg_path = tmp_path / "host.json"
    cfg_value = config_dict(tmp_path, enabled=True)
    cfg_path.write_text(json.dumps(cfg_value))
    req = request()
    source_paths = {
        "request": tmp_path / "request.json",
        "request_attestation": tmp_path / "request.sigstore.json",
        "release": tmp_path / "release.json",
        "release_attestation": tmp_path / "release.sigstore.json",
        "image_attestation": tmp_path / "image.sigstore.json",
    }
    originals = {
        "request": release_manifest.canonical_json_bytes(req),
        "request_attestation": b'{"request-bundle":true}\n',
        "release": release_manifest.canonical_json_bytes(req["release"]),
        "release_attestation": b'{"release-bundle":true}\n',
        "image_attestation": b'{"image-bundle":true}\n',
    }
    for label, path in source_paths.items():
        path.write_bytes(originals[label])

    real_validated_inputs = dev_deploy._validated_inputs
    observed = {"order": []}

    def capture_validation(cfg, request_path, release_path):
        observed["validation_paths"] = (request_path, release_path)
        result = real_validated_inputs(cfg, request_path, release_path)
        # Source가 검증 직후 바뀌어도 attestation/apply는 staged bytes만 보아야 한다.
        for path in source_paths.values():
            path.write_bytes(b"tampered-after-snapshot\n")
        return result

    def capture_attestations(runner, cfg, deployment_request, **paths):
        observed["order"].append("attestations")
        observed["attestation_paths"] = paths
        observed["staged_bytes"] = {
            label.removesuffix("_path"): path.read_bytes()
            for label, path in paths.items()
        }
        staged = list(paths.values())
        assert len(staged) == 5
        assert len({path.parent for path in staged}) == 1
        assert stat.S_IMODE(staged[0].parent.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in staged)

    monkeypatch.setattr(dev_deploy, "validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(dev_deploy, "_validated_inputs", capture_validation)
    monkeypatch.setattr(dev_deploy, "_verify_attestations", capture_attestations)
    monkeypatch.setattr(
        dev_deploy,
        "_verify_github_ci",
        lambda runner, cfg, deployment_request: observed["order"].append("ci"),
    )
    monkeypatch.setattr(
        dev_deploy,
        "_verify_github_environment_approval",
        lambda runner, cfg, deployment_request: (
            observed["order"].append("approval"),
            observed.update({"approval_request": deployment_request}),
        ),
    )
    monkeypatch.setattr(
        dev_deploy,
        "apply_deployment",
        lambda *args, **kwargs: (
            observed["order"].append("docker"),
            {"status": "snapshot-bound"},
        )[1],
    )

    assert dev_deploy.main([
        "--config", str(cfg_path),
        "apply",
        "--request", str(source_paths["request"]),
        "--request-attestation", str(source_paths["request_attestation"]),
        "--release", str(source_paths["release"]),
        "--release-attestation", str(source_paths["release_attestation"]),
        "--image-attestation", str(source_paths["image_attestation"]),
    ]) == 0

    attested = observed["attestation_paths"]
    assert observed["validation_paths"] == (
        attested["request_path"], attested["release_path"]
    )
    assert observed["staged_bytes"] == originals
    assert observed["approval_request"] == req
    assert observed["order"] == ["attestations", "ci", "approval", "docker"]
    staged_parent = attested["request_path"].parent
    assert not staged_parent.exists()
    assert list(state_dir.glob(".verify-*")) == []


def test_five_file_snapshot은_중간예외에도_모든_staged_file을_정리한다(tmp_path):
    cfg = config(tmp_path)
    sources = []
    for index in range(5):
        path = tmp_path / f"artifact-{index}.json"
        path.write_bytes(f"artifact-{index}\n".encode())
        sources.append(path)

    staged_parent = None
    with pytest.raises(RuntimeError, match="injected"):
        with dev_deploy._artifact_snapshot(
            cfg,
            request=sources[0],
            request_attestation=sources[1],
            release=sources[2],
            release_attestation=sources[3],
            image_attestation=sources[4],
        ) as staged:
            staged_parent = staged["request"].parent
            raise RuntimeError("injected verification failure")

    assert staged_parent is not None
    assert not staged_parent.exists()
    assert list(cfg.state_dir.glob(".verify-*")) == []


def test_apply_main은_CI_API불일치시_승인과Docker전에_fail_closed한다(
    tmp_path, monkeypatch
):
    (tmp_path / "state").mkdir(mode=0o700)
    cfg_path = tmp_path / "host.json"
    cfg_path.write_text(json.dumps(config_dict(tmp_path, enabled=True)))
    req = request()
    sources = {
        "request": tmp_path / "request.json",
        "request_attestation": tmp_path / "request.sigstore.json",
        "release": tmp_path / "release.json",
        "release_attestation": tmp_path / "release.sigstore.json",
        "image_attestation": tmp_path / "image.sigstore.json",
    }
    sources["request"].write_bytes(release_manifest.canonical_json_bytes(req))
    sources["release"].write_bytes(release_manifest.canonical_json_bytes(req["release"]))
    for key in ("request_attestation", "release_attestation", "image_attestation"):
        sources[key].write_bytes(b'{"bundle":true}\n')

    order = []

    def reject_ci(*args, **kwargs):
        order.append("ci")
        raise dev_deploy.DeploymentError("GitHub CI provenance 검증 실패: mismatch")

    def forbidden(*args, **kwargs):
        raise AssertionError("CI 검증 실패 뒤 호출되면 안 된다")

    monkeypatch.setattr(dev_deploy, "validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dev_deploy, "_verify_attestations",
        lambda *args, **kwargs: order.append("attestations"),
    )
    monkeypatch.setattr(dev_deploy, "_verify_github_ci", reject_ci)
    monkeypatch.setattr(dev_deploy, "_verify_github_environment_approval", forbidden)
    monkeypatch.setattr(dev_deploy, "apply_deployment", forbidden)

    argv = [
        "--config", str(cfg_path), "apply",
        "--request", str(sources["request"]),
        "--request-attestation", str(sources["request_attestation"]),
        "--release", str(sources["release"]),
        "--release-attestation", str(sources["release_attestation"]),
        "--image-attestation", str(sources["image_attestation"]),
    ]
    with pytest.raises(dev_deploy.DeploymentError, match="CI provenance"):
        dev_deploy.main(argv)

    assert order == ["attestations", "ci"]


def test_apply_main은_image_attestation실패시_CI승인Docker전에_fail_closed한다(
    tmp_path, monkeypatch
):
    (tmp_path / "state").mkdir(mode=0o700)
    cfg_path = tmp_path / "host.json"
    cfg_path.write_text(json.dumps(config_dict(tmp_path, enabled=True)))
    req = request()
    sources = {
        "request": tmp_path / "request.json",
        "request_attestation": tmp_path / "request.sigstore.json",
        "release": tmp_path / "release.json",
        "release_attestation": tmp_path / "release.sigstore.json",
        "image_attestation": tmp_path / "image.sigstore.json",
    }
    sources["request"].write_bytes(release_manifest.canonical_json_bytes(req))
    sources["release"].write_bytes(release_manifest.canonical_json_bytes(req["release"]))
    for key in ("request_attestation", "release_attestation", "image_attestation"):
        sources[key].write_bytes(b'{"bundle":true}\n')

    subjects = []

    def reject_image(**kwargs):
        subjects.append(kwargs["subject"])
        if kwargs["subject"].startswith("oci://"):
            raise dev_deploy.attestation.AttestationError("image mismatch")

    def forbidden(*args, **kwargs):
        raise AssertionError("image attestation 실패 뒤 호출되면 안 된다")

    monkeypatch.setattr(dev_deploy, "validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(dev_deploy.attestation, "verify", reject_image)
    monkeypatch.setattr(dev_deploy, "_verify_github_ci", forbidden)
    monkeypatch.setattr(dev_deploy, "_verify_github_environment_approval", forbidden)
    monkeypatch.setattr(dev_deploy, "apply_deployment", forbidden)

    argv = [
        "--config", str(cfg_path), "apply",
        "--request", str(sources["request"]),
        "--request-attestation", str(sources["request_attestation"]),
        "--release", str(sources["release"]),
        "--release-attestation", str(sources["release_attestation"]),
        "--image-attestation", str(sources["image_attestation"]),
    ]
    with pytest.raises(dev_deploy.DeploymentError, match="artifact attestation"):
        dev_deploy.main(argv)

    assert len(subjects) == 3
    assert Path(subjects[0]).name == "conan-development-request.json"
    assert Path(subjects[1]).name == "conan-release.json"
    assert subjects[2] == f"oci://{TARGET_IMAGE}"


def test_plan은_server명령없이_활성화_blocker만_보고한다(tmp_path):
    cfg = config(tmp_path, enabled=False, bootstrap_active=None)
    result = dev_deploy.plan(cfg, request())
    assert result["server_commands_executed"] == 0
    assert result["artifact_attestations_verified"] is False
    assert result["github_environment_approval_verified"] is False
    assert result["blockers"] == [
        "host config enabled=false",
        "bootstrap_active is required before initialize",
    ]


def test_SubprocessRunner_timeout은_process_group을_종료하고_명령내용을_노출하지_않는다():
    runner = dev_deploy.SubprocessRunner(0.01)
    with pytest.raises(dev_deploy.DeploymentError, match="시간이 초과돼 process group") as error:
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(5)", "secret-argument"],
            env={"PATH": "/usr/bin:/bin"},
        )
    assert "secret-argument" not in str(error.value)


def test_SubprocessRunner는_stdout과_stderr_크기를_disk에서_제한한다(monkeypatch):
    monkeypatch.setattr(dev_deploy, "MAX_EXTERNAL_OUTPUT_BYTES", 32)
    runner = dev_deploy.SubprocessRunner(5)
    with pytest.raises(dev_deploy.DeploymentError, match="출력이 상한"):
        runner.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('x' * 33)"],
            env={"PATH": "/usr/bin:/bin"},
        )
