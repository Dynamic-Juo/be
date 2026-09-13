from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import plistlib
import stat
import sys
import uuid
import zipfile

import pytest

from scripts import auto_deploy as auto, dev_deploy as controller, release_manifest
from scripts.deployment_state import atomic_write_bytes
from test_dev_deploy import config, FakeRunner, Clock, request, SOURCE_SHA
from test_github_approval import ci_run_record, CI_WORKFLOW, workflow_contents


def auto_config(tmp_path, **changes):
    cfg = config(tmp_path, request_authentication=controller.AUTO_AUTHENTICATION,
                  deployment_workflow_id=None, deployment_workflow_sha256=None,
                  environment_id=None, allowed_reviewer_user_ids=frozenset(),
                  backend_ci_workflow_sha256=hashlib.sha256(CI_WORKFLOW).hexdigest(), **changes)
    return replace(cfg, bootstrap_active=replace(cfg.bootstrap_active, request_run_id=100))


def archive(files=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in (files or {name: b"{}" for name in auto.FILES}).items():
            z.writestr(name, payload)
    return buffer.getvalue()


def test_exact_archive_and_path_traversal():
    assert set(auto.unpack_archive(archive())) == set(auto.FILES)
    for bad in ("../evil", "/evil", "nested/conan-release.json", "conan-release.json/", "a\\b"):
        files = {name: b"{}" for name in auto.FILES}
        files[bad] = files.pop("conan-release.json")
        with pytest.raises(auto.Error):
            auto.unpack_archive(archive(files))


def test_archive_rejects_symlink_duplicate_oversize_and_corruption():
    for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in auto.FILES:
                item = zipfile.ZipInfo(name)
                item.external_attr = (mode | 0o600) << 16
                z.writestr(item, b"{}")
        with pytest.raises(auto.Error):
            auto.unpack_archive(buf.getvalue())
    files = {name: b"{}" for name in auto.FILES}
    files["conan-release.json"] = b"x" * (64 * 1024 + 1)
    with pytest.raises(auto.Error):
        auto.unpack_archive(archive(files))
    with pytest.raises(auto.Error):
        auto.unpack_archive(b"not zip")
    with pytest.raises(auto.Error):
        auto.unpack_archive(b"x" * (auto.MAX_ARCHIVE_BYTES + 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in auto.FILES:
            z.writestr(name, b"{}")
        with pytest.warns(UserWarning):
            z.writestr("conan-release.json", b"{}")
    with pytest.raises(auto.Error):
        auto.unpack_archive(buf.getvalue())


def test_modes_are_explicit_and_have_distinct_state_binding(tmp_path):
    base = Path(__file__).resolve().parents[1]
    value = json.loads((base / "deploy/auto-host-config.example.json").read_text())
    parsed = controller.DeploymentConfig.from_dict(value)
    assert parsed.environment_id is None
    assert not parsed.allowed_reviewer_user_ids
    for field, bad in (("schema_version", 4), ("environment_id", 1),
                       ("deployment_workflow_id", 1), ("allowed_reviewer_user_ids", [1]),
                       ("deployment_workflow_sha256", "0" * 64)):
        with pytest.raises(controller.DeploymentError):
            controller.DeploymentConfig.from_dict(dict(value, **{field: bad}))
    cfg = config(tmp_path)
    assert cfg.target_fingerprint != replace(cfg, request_authentication=controller.AUTO_AUTHENTICATION).target_fingerprint


class ApiRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.run_record = ci_run_record(run_attempt=1)
        release = release_manifest.build_manifest(
            repository=cfg.repository, repository_id=cfg.repository_id,
            repository_owner_id=cfg.repository_owner_id, source_sha=SOURCE_SHA,
            ci_run_id="321", ci_run_attempt="1", image=request()["release"]["image"],
        )
        self.payload = archive({
            "conan-release.json": release_manifest.canonical_json_bytes(release),
            "conan-release.sigstore.json": b"{}", "conan-image.sigstore.json": b"{}",
        })
        self.sha_reads = 0
        self.advance = False
        self.calls = []
        self.artifact = {
            "id": 654, "name": f"conan-release-{SOURCE_SHA}-1", "expired": False,
            "size_in_bytes": len(self.payload),
            "digest": "sha256:" + hashlib.sha256(self.payload).hexdigest(),
            "archive_download_url": "https://attacker.invalid/ignored",
            "workflow_run": {"id": 321, "head_sha": SOURCE_SHA,
                             "repository_id": cfg.repository_id, "head_repository_id": cfg.repository_id},
        }

    def run(self, command, *, env):
        self.calls.append(command)
        endpoint = command[-1]
        if endpoint.endswith("git/ref/heads/main"):
            self.sha_reads += 1
            value = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": (
                "f" * 40 if self.advance and self.sha_reads > 1 else SOURCE_SHA)}}
        elif "/artifacts?" in endpoint:
            value = {"total_count": 1, "artifacts": [self.artifact]}
        elif "/contents/" in endpoint:
            value = workflow_contents(payload=CI_WORKFLOW, path=".github/workflows/backend-ci.yml")
        elif "/runs?" in endpoint:
            value = {"workflow_runs": [self.run_record]}
        else:
            value = self.run_record
        return controller.CommandResult(stdout=json.dumps(value))

    def run_bytes(self, command, *, env, max_bytes):
        self.calls.append(command)
        assert command[-1].endswith("/actions/artifacts/654/zip")
        assert max_bytes == auto.MAX_ARCHIVE_BYTES
        return self.payload


def setup_tick(tmp_path, monkeypatch):
    cfg = auto_config(tmp_path)
    monkeypatch.setattr(controller, "read_config", lambda _: cfg)
    monkeypatch.setattr(controller, "validate_runtime", lambda *a, **kw: None)
    state = {"target_fingerprint": cfg.target_fingerprint, "active": {"source_sha": "d" * 40},
             "highest_ci": {"run_id": 100, "run_attempt": 1}}
    monkeypatch.setattr(controller, "_load_state_files", lambda _: (state, None))
    attestations = []
    monkeypatch.setattr(auto.attestation, "verify", lambda **kw: attestations.append(kw))
    return cfg, ApiRunner(cfg), attestations


def test_tick_authenticates_two_subjects_and_consumes_once(tmp_path, monkeypatch):
    cfg, runner, attestations = setup_tick(tmp_path, monkeypatch)
    applied = []
    def apply(config, transaction, **kwargs):
        assert auto.read_latch(cfg)["status"] == "attempting"
        assert "dispatch_actor" not in transaction
        assert transaction["authentication"] == controller.AUTO_AUTHENTICATION
        assert str(uuid.UUID(transaction["request_id"])) == transaction["request_id"]
        applied.append(transaction)
        return {"status": "deployed"}
    monkeypatch.setattr(controller, "apply_deployment", apply)
    assert auto.tick(tmp_path / "config", runner=runner)["status"] == "deployed"
    assert auto.tick(tmp_path / "config", runner=runner)["status"] == "already-consumed"
    assert len(applied) == 1 and len(attestations) == 2
    assert attestations[1]["subject"].startswith("oci://ghcr.io/dynamic-juo/be@sha256:")
    assert all(item["policy"].event == "push" for item in attestations)
    assert all("environment" not in call[-1] for call in runner.calls)
    assert not list(cfg.state_dir.glob(".auto-verify-*"))


@pytest.mark.parametrize("failure", ["apply", "signature", "head-moved", "artifact-digest", "workflow", "rerun"])
def test_failure_latches_before_apply_and_never_retries(tmp_path, monkeypatch, failure):
    cfg, runner, _ = setup_tick(tmp_path, monkeypatch)
    applied = []
    def fail(*a, **kw):
        raise auto.Error("test failure")
    def apply(*a, **kw):
        applied.append(True)
        if failure == "apply":
            fail()
        return {"status": "deployed"}
    monkeypatch.setattr(controller, "apply_deployment", apply)
    if failure == "signature":
        monkeypatch.setattr(auto.attestation, "verify", fail)
    elif failure == "head-moved":
        runner.advance = True
    elif failure == "artifact-digest":
        runner.artifact["digest"] = "sha256:" + "0" * 64
    elif failure == "workflow":
        cfg = replace(cfg, backend_ci_workflow_sha256="0" * 64)
        monkeypatch.setattr(controller, "read_config", lambda _: cfg)
    elif failure == "rerun":
        original = runner.run
        def run(command, **kw):
            result = original(command, **kw)
            if command[-1].endswith("/actions/runs/321"):
                return controller.CommandResult(stdout=json.dumps(ci_run_record(run_attempt=2)))
            return result
        runner.run = run
    with pytest.raises(Exception):
        auto.tick(tmp_path / "config", runner=runner)
    assert auto.read_latch(cfg)["status"] == "blocked"
    calls = len(runner.calls)
    assert auto.tick(tmp_path / "config", runner=runner)["status"] == "blocked"
    assert len(runner.calls) == calls
    assert len(applied) == (1 if failure == "apply" else 0)


def test_crash_attempt_and_symlink_latch_fail_closed(tmp_path, monkeypatch):
    cfg, runner, _ = setup_tick(tmp_path, monkeypatch)
    latch = {"schema_version": 1, "target_fingerprint": cfg.target_fingerprint,
             "run_id": 321, "run_attempt": 1, "source_sha": SOURCE_SHA, "status": "attempting"}
    path = cfg.state_dir / "auto-attempt.json"
    atomic_write_bytes(path, release_manifest.canonical_json_bytes(latch))
    assert auto.tick(tmp_path / "config", runner=runner)["status"] == "blocked"
    assert not runner.calls
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(Exception):
        auto.tick(tmp_path / "config", runner=runner)
    assert not runner.calls


@pytest.mark.parametrize("field,value", [("head_sha", "f" * 40), ("event", "pull_request"),
                                        ("workflow_id", 9), ("head_branch", "feature")])
def test_wrong_ci_identity_rejected(tmp_path, field, value):
    cfg = auto_config(tmp_path)
    runner = ApiRunner(cfg)
    runner.run_record[field] = value
    with pytest.raises(Exception):
        auto.candidate(runner, cfg, SOURCE_SHA)


def test_newest_failed_run_does_not_fall_back(tmp_path, monkeypatch):
    cfg = auto_config(tmp_path)
    monkeypatch.setattr(auto, "api", lambda *a: {"workflow_runs": [
        ci_run_record(id=321), ci_run_record(id=322, conclusion="failure")]})
    assert auto.candidate(None, cfg, SOURCE_SHA) is None


def test_binary_capture_is_bounded_and_preserves_non_utf8():
    runner = controller.SubprocessRunner(2)
    env = {"PATH": "/usr/bin:/bin"}
    assert runner.run_bytes([sys.executable, "-c", "import sys;sys.stdout.buffer.write(bytes([255]))"], env=env) == b"\xff"
    with pytest.raises(controller.DeploymentError):
        runner.run_bytes([sys.executable, "-c", "print('x'*10000)"], env=env, max_bytes=100)


class DurableRunner(FakeRunner):
    capability = "durable-terminal-v1"
    def run(self, args, **kw):
        response = super().run(args, **kw)
        try:
            payload = json.loads(response.stdout)
        except ValueError:
            return response
        if type(payload) is dict and "harness" in payload:
            payload["harness"]["result_persistence"] = self.capability
            return controller.CommandResult(stdout=json.dumps(payload))
        return response


def test_auto_apply_requires_result_store_before_drain(tmp_path):
    cfg = auto_config(tmp_path)
    runner = FakeRunner()
    clock = Clock()
    with pytest.raises(controller.DeploymentError, match="durable-terminal-v1"):
        controller.apply_deployment(cfg, request(), runner=runner,
                                    sleeper=clock.sleep, monotonic=clock.monotonic)
    assert not runner.control_actions and not runner.up_events


def test_auto_apply_succeeds_with_result_store(tmp_path):
    cfg = auto_config(tmp_path)
    runner = DurableRunner()
    clock = Clock()
    controller.initialize_deployment(cfg, runner=runner,
                                     sleeper=clock.sleep, monotonic=clock.monotonic)
    result = controller.apply_deployment(cfg, request(), runner=runner,
                                         sleeper=clock.sleep, monotonic=clock.monotonic)
    assert result["status"] == "deployed"


def test_idle_and_target_health_require_result_store(tmp_path):
    cfg = auto_config(tmp_path)
    clock = Clock()
    with pytest.raises(controller.DeploymentError, match="durable-terminal-v1"):
        controller._wait_until_idle(FakeRunner(admission_state="draining"), cfg,
                                   sleeper=clock.sleep, monotonic=clock.monotonic)
    with pytest.raises(controller.DeploymentError, match="durable-terminal-v1"):
        controller._wait_for_service(FakeRunner(), cfg,
                                    expected_image_id=cfg.bootstrap_active.image_id,
                                    sleeper=clock.sleep, monotonic=clock.monotonic)


def test_launchd_is_periodic_fixed_interpreter_without_shell():
    path = Path(__file__).resolve().parents[1] / "deploy/com.conan.development-pull.plist.example"
    value = plistlib.loads(path.read_bytes())
    assert value["StartInterval"] == 60
    assert value["ProgramArguments"][1] == "-I"
    assert "KeepAlive" not in value
    assert "Sockets" not in value


@pytest.mark.parametrize("target_fails", [False, True])
def test_authenticated_transaction_real_controller_and_retry_latch(tmp_path, monkeypatch, target_fails):
    cfg = auto_config(tmp_path)
    docker = DurableRunner(target_fails=target_fails)
    clock = Clock()
    controller.initialize_deployment(cfg, runner=docker, sleeper=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(controller, "read_config", lambda _: cfg)
    monkeypatch.setattr(controller, "validate_runtime", lambda *a, **kw: None)
    monkeypatch.setattr(auto.attestation, "verify", lambda **kw: None)
    real_apply = controller.apply_deployment
    def apply(config, transaction, **kw):
        return real_apply(config, transaction, runner=docker, sleeper=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(controller, "apply_deployment", apply)
    runner = ApiRunner(cfg)
    if target_fails:
        with pytest.raises(controller.DeploymentError):
            auto.tick(tmp_path / "config", runner=runner)
        events = len(docker.up_events)
        assert auto.tick(tmp_path / "config", runner=runner)["status"] == "blocked"
        assert len(docker.up_events) == events
    else:
        assert auto.tick(tmp_path / "config", runner=runner)["status"] == "deployed"
        assert auto.tick(tmp_path / "config", runner=runner)["status"] == "unchanged"


def test_disabled_does_not_call_network_or_docker(tmp_path, monkeypatch):
    cfg = replace(auto_config(tmp_path), enabled=False)
    monkeypatch.setattr(controller, "read_config", lambda _: cfg)
    runner = ApiRunner(cfg)
    assert auto.tick(tmp_path / "config", runner=runner)["status"] == "disabled"
    assert not runner.calls
