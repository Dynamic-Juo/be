"""One bounded pull iteration; launchd schedules it every 60 seconds.

The trusted installed copy authenticates CI releases, never checks out application
code. A durable attempt latch precedes artifact processing. Any interrupted or
failed attempt requires operator reconciliation before another automatic apply.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import attestation, dev_deploy as controller, github_approval
from scripts.deployment_state import atomic_write_bytes
from scripts.release_manifest import canonical_json_bytes, validate_manifest_for_repository
from scripts.secure_paths import stat_trusted_regular_file

MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
FILES = {
    "conan-release.json": 64 * 1024,
    "conan-release.sigstore.json": 1024 * 1024,
    "conan-image.sigstore.json": 1024 * 1024,
}
Error = controller.DeploymentError


def api(runner, config, suffix):
    command = github_approval._api_command(
        config.gh_binary, f"repos/{config.repository}/{suffix}"
    )
    raw = runner.run(command, env=controller._gh_env(config)).stdout
    return controller._decode_json_object(raw.encode(), label="GitHub API")


def main_sha(runner, config):
    record = api(runner, config, "git/ref/heads/main")
    obj = record.get("object", {})
    sha = obj.get("sha") if type(obj) is dict else None
    if (record.get("ref") != "refs/heads/main" or type(obj) is not dict or obj.get("type") != "commit"
            or type(sha) is not str or not controller.SOURCE_SHA_RE.fullmatch(sha)):
        raise Error("main ref identity is invalid")
    return sha


def positive(value):
    if type(value) is not int or not 1 <= value < 1 << 63:
        raise Error("invalid GitHub numeric identity")
    return value


def candidate(runner, config, sha):
    result = api(runner, config,
                 f"actions/workflows/{config.backend_ci_workflow_id}/runs"
                 f"?branch=main&event=push&head_sha={sha}&per_page=100")
    runs = result.get("workflow_runs")
    if type(runs) is not list or len(runs) > 100:
        raise Error("invalid workflow run list")
    if not runs:
        return None
    for run in runs:
        if type(run) is not dict:
            raise Error("invalid workflow run")
        positive(run.get("id"))
    # Never fall back to an older success when the latest run failed or is pending.
    run = max(runs, key=lambda item: item["id"])
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return None
    policy = ci_policy(config, sha, run["id"], positive(run.get("run_attempt")))
    github_approval.verify_ci_run_output(json.dumps(run), policy)
    return run


def ci_policy(config, sha, run_id, attempt):
    return github_approval.CiPolicy(
        repository=config.repository, repository_id=config.repository_id,
        repository_owner_id=config.repository_owner_id,
        workflow_id=config.backend_ci_workflow_id,
        workflow_path=github_approval.BACKEND_CI_WORKFLOW_PATH,
        workflow_sha256=config.backend_ci_workflow_sha256,
        source_sha=sha, run_id=run_id, run_attempt=attempt,
    )


def unpack_archive(payload):
    if not payload or len(payload) > MAX_ARCHIVE_BYTES:
        raise Error("release archive size exceeds limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(names) != len(FILES) or set(names) != set(FILES):
                raise Error("archive must contain exactly the three release files")
            result = {}
            for item in members:
                mode = item.external_attr >> 16
                if (item.is_dir() or item.flag_bits & 1
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or not 0 < item.file_size <= FILES[item.filename]):
                    raise Error("unsafe archive member")
                with archive.open(item) as stream:
                    data = stream.read(FILES[item.filename] + 1)
                if len(data) != item.file_size or len(data) > FILES[item.filename]:
                    raise Error("archive member size mismatch")
                result[item.filename] = data
            return result
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise Error("invalid release ZIP") from exc


def download(runner, config, run, sha):
    name = f"conan-release-{sha}-{run['run_attempt']}"
    listing = api(runner, config,
                  f"actions/runs/{run['id']}/artifacts?name={name}&per_page=100")
    artifacts = listing.get("artifacts")
    if listing.get("total_count") != 1 or type(artifacts) is not list or len(artifacts) != 1:
        raise Error("expected one immutable release artifact")
    item = artifacts[0]
    if type(item) is not dict or item.get("name") != name or item.get("expired") is not False:
        raise Error("invalid or expired release artifact")
    artifact_id = positive(item.get("id"))
    size = positive(item.get("size_in_bytes"))
    digest = item.get("digest")
    if size > MAX_ARCHIVE_BYTES or type(digest) is not str or not controller.IMAGE_ID_RE.fullmatch(digest):
        raise Error("invalid artifact size/digest")
    binding = item.get("workflow_run")
    if (type(binding) is not dict or binding.get("id") != run["id"]
            or binding.get("head_sha") != sha
            or binding.get("repository_id") != config.repository_id
            or binding.get("head_repository_id") != config.repository_id):
        raise Error("artifact run binding mismatch")
    # Ignore archive_download_url from the response. gh authenticates only the
    # fixed GitHub endpoint and handles its storage redirect; no URL from a ZIP
    # or release manifest is ever followed.
    payload = runner.run_bytes(github_approval._api_command(
        config.gh_binary, f"repos/{config.repository}/actions/artifacts/{artifact_id}/zip"
    ), env=controller._gh_env(config), max_bytes=MAX_ARCHIVE_BYTES)
    if len(payload) != size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise Error("downloaded artifact digest/size mismatch")
    return unpack_archive(payload)


@contextmanager
def stage(config, files):
    directory = config.state_dir / (".auto-verify-" + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    paths = {}
    try:
        for name, payload in files.items():
            if name not in FILES:
                raise Error("unexpected staging file")
            paths[name] = directory / name
            atomic_write_bytes(paths[name], payload)
        yield paths
    finally:
        for path in paths.values():
            path.unlink(missing_ok=True)
        directory.rmdir()


def authenticate(runner, config, run, sha, paths):
    path = paths["conan-release.json"]
    release = validate_manifest_for_repository(
        controller._read_secure_canonical_json(path, label="automatic release"),
        repository=config.repository, repository_id=config.repository_id,
        repository_owner_id=config.repository_owner_id,
    )
    if (release["source_sha"] != sha or release["ci_run_id"] != run["id"]
            or release["ci_run_attempt"] != run["run_attempt"]):
        raise Error("release does not match selected main CI")
    policy = attestation.AttestationPolicy(
        repository=config.repository, repository_id=config.repository_id,
        repository_owner_id=config.repository_owner_id,
        workflow_path=github_approval.BACKEND_CI_WORKFLOW_PATH,
        source_sha=sha, event="push", run_id=run["id"], run_attempt=run["run_attempt"],
    )
    for subject, bundle, registry in (
        (str(path), paths["conan-release.sigstore.json"], False),
        (f"oci://{release['image']}", paths["conan-image.sigstore.json"], True),
    ):
        attestation.verify(runner=runner, gh_binary=config.gh_binary, subject=subject,
                           policy=policy, env=controller._gh_env(config, registry_auth=registry),
                           bundle=bundle)
    policy_ci = ci_policy(config, sha, run["id"], run["run_attempt"])
    github_approval.verify_ci(runner=runner, gh_binary=config.gh_binary,
                              policy=policy_ci, env=controller._gh_env(config))
    current = api(runner, config, f"actions/runs/{run['id']}")
    github_approval.verify_ci_run_output(json.dumps(current), policy_ci)
    if main_sha(runner, config) != sha:
        raise Error("main advanced during release verification")
    # Internal transaction input, NOT a signed environment approval envelope.
    # The request watermark in the existing journal uses the CI run namespace
    # for this distinct target fingerprint and authentication mode.
    return {
        "authentication": controller.AUTO_AUTHENTICATION,
        "target_id": config.target_id, "release": release,
        "request_id": str(uuid.uuid5(uuid.NAMESPACE_URL,
            f"conan:{controller.AUTO_AUTHENTICATION}:{config.repository_id}:"
            f"{config.repository_owner_id}:{config.target_id}:{run['id']}:{run['run_attempt']}")),
        "request_run_id": run["id"], "request_run_attempt": run["run_attempt"],
    }


def read_latch(config):
    path = config.state_dir / "auto-attempt.json"
    if stat_trusted_regular_file(path, private=True, missing_ok=True) is None:
        return None
    value = controller._read_secure_canonical_json(path, label="automatic attempt")
    if (set(value) != {"schema_version", "target_fingerprint", "run_id", "run_attempt", "source_sha", "status"}
            or value["schema_version"] != 1
            or value["target_fingerprint"] != config.target_fingerprint
            or value["status"] not in {"attempting", "succeeded", "blocked"}
            or type(value["source_sha"]) is not str
            or not controller.SOURCE_SHA_RE.fullmatch(value["source_sha"])):
        raise Error("invalid automatic attempt state")
    positive(value["run_id"])
    positive(value["run_attempt"])
    return value


def tick(config_path, *, runner=None):
    config = controller.read_config(config_path)
    if config.request_authentication != controller.AUTO_AUTHENTICATION:
        raise Error("watcher requires explicit main-push authentication mode")
    if not config.enabled:
        return {"status": "disabled"}
    controller.validate_runtime(config_path, config, require_gh=True)
    runner = runner or controller.SubprocessRunner(config.command_timeout_seconds)
    with controller.deployment_lock(config.global_lock_file):
        latch = read_latch(config)
        if latch and latch["status"] != "succeeded":
            return {"status": "blocked", "reason": "operator reconciliation required"}
        state, journal = controller._load_state_files(config)
        if state is None:
            raise Error("initialize the automatic target before enabling the watcher")
        controller._validate_reconcile_binding(config, state, journal)
        if journal and journal["phase"] not in {"committed", "rolled_back", "aborted"}:
            raise Error("unfinished transaction requires operator recover")
        if journal and journal["phase"] != "committed":
            raise Error("failed transaction requires operator reconciliation")
        sha = main_sha(runner, config)
        if state["active"] and state["active"]["source_sha"] == sha:
            return {"status": "unchanged"}
        run = candidate(runner, config, sha)
        if run is None:
            return {"status": "waiting-for-ci"}
        if latch and (run["id"] <= latch["run_id"] or sha == latch["source_sha"]):
            return {"status": "already-consumed"}
        if (run["id"], run["run_attempt"]) <= (
            state["highest_ci"]["run_id"], state["highest_ci"]["run_attempt"]
        ):
            return {"status": "already-consumed"}
        latch = {"schema_version": 1, "target_fingerprint": config.target_fingerprint,
                 "run_id": run["id"], "run_attempt": run["run_attempt"],
                 "source_sha": sha, "status": "attempting"}
        latch_path = config.state_dir / "auto-attempt.json"
        atomic_write_bytes(latch_path, canonical_json_bytes(latch))
        try:
            with stage(config, download(runner, config, run, sha)) as paths:
                request = authenticate(runner, config, run, sha, paths)
                result = controller.apply_deployment(config, request, runner=runner)
                if result.get("status") not in {"deployed", "no-op"}:
                    raise Error("automatic transaction did not commit")
            latch["status"] = "succeeded"
            atomic_write_bytes(latch_path, canonical_json_bytes(latch))
            return result
        except Exception:
            latch["status"] = "blocked"
            atomic_write_bytes(latch_path, canonical_json_bytes(latch))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=controller._canonical_path_argument)
    args = parser.parse_args()
    try:
        print(json.dumps(tick(args.config), ensure_ascii=False))
    except Exception as exc:
        # Provider output and credentials are deliberately excluded from logs.
        raise SystemExit(f"automatic deployment stopped ({type(exc).__name__}); inspect local state") from None


if __name__ == "__main__":
    main()
