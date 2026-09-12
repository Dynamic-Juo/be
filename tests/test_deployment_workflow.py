from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

from scripts import release_manifest


ROOT = Path(__file__).resolve().parents[1]
DEV_WORKFLOW = ROOT / ".github/workflows/dev-deployment.yml"
CI_WORKFLOW = ROOT / ".github/workflows/backend-ci.yml"


def source(path):
    return path.read_text()


def job_source(path, job_name):
    text = source(path)
    marker = f"\n  {job_name}:\n"
    assert marker in text
    remainder = text.split(marker, 1)[1]
    next_job = re.search(r"\n  [A-Za-z0-9_-]+:\n", remainder)
    return remainder[:next_job.start()] if next_job else remainder


def inline_python(path, job_name):
    job = job_source(path, job_name)
    marker = "python -I - <<'PY'\n"
    assert marker in job
    raw = job.split(marker, 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(raw)


def test_CD는_수동요청만_받고_PR코드를_실행하지_않는다():
    text = source(DEV_WORKFLOW)
    trigger = text.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "pull_request" not in trigger
    assert "pull_request_target" not in trigger
    assert "\npush:" not in trigger
    assert "self-hosted" not in text
    assert set(re.findall(r"runs-on:\s*([^\n]+)", text)) == {"ubuntu-24.04"}


def test_CI와_CD는_GitHub_hosted_label만_사용한다():
    ci = source(CI_WORKFLOW)
    cd = source(DEV_WORKFLOW)
    assert "self-hosted" not in ci + cd
    assert set(re.findall(r"runs-on:\s*([^\n]+)", ci)) == {"ubuntu-24.04-arm"}
    assert set(re.findall(r"runs-on:\s*([^\n]+)", cd)) == {"ubuntu-24.04"}


def test_CD는_고정_environment와_동시실행방지를_사용한다():
    text = source(DEV_WORKFLOW)
    assert "group: conan-development-deployment" in text
    assert "cancel-in-progress: false" in text
    assert "name: conan-development" in text
    assert "CONAN_DEV_DEPLOY_ENABLED" in text
    assert "CONAN_DEV_ENVIRONMENT_CONFIGURED" in text
    assert text.count("ref: ${{ github.sha }}") == 1
    assert "ref: main" not in text
    assert (
        "environment-bound-conan-development-${{ inputs.source_sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    ) in text


def test_dispatch_source_SHA는_checkout과_request의_GitHub_SHA에_고정된다():
    text = source(DEV_WORKFLOW)
    assert "REQUEST_SHA: ${{ github.sha }}" in text
    assert "REQUEST_RUN_ATTEMPT: ${{ github.run_attempt }}" in text
    assert "SOURCE_SHA: ${{ inputs.source_sha }}" in text
    assert 'if [ "$SOURCE_SHA" != "$REQUEST_SHA" ]; then' in text
    assert text.count("ref: ${{ github.sha }}") == 1
    assert 'source_sha = os.environ["GITHUB_SHA"]' in text
    assert "DISPATCH_ACTOR_ID: ${{ github.actor_id }}" in text
    assert "EXPECTED_CI_RUN_ID: ${{ inputs.ci_run_id }}" in text
    assert "EXPECTED_CI_RUN_ATTEMPT: ${{ inputs.ci_run_attempt }}" in text
    assert 'positive("DISPATCH_ACTOR_ID")' in text
    assert 'if [ "$REQUEST_RUN_ATTEMPT" != "1" ]; then' in text
    assert "X-GitHub-Api-Version: 2026-03-10" in text


def test_CD는_서버정보를_input으로_받거나_서버명령을_실행하지_않는다():
    text = source(DEV_WORKFLOW)
    inputs = text.split("permissions:", 1)[0]
    for forbidden_input in ("host:", "compose_file:", "env_file:", "project:", "service:"):
        assert forbidden_input not in inputs
    for forbidden_command in ("dev_deploy.py", "docker compose", "ssh ", "scp ", "rsync "):
        assert forbidden_command not in text
    assert "did not deploy" in text


def test_Actions에는_호스트접속이나_배포컨트롤러_실행이_없다():
    text = (source(CI_WORKFLOW) + source(DEV_WORKFLOW)).lower()
    for forbidden in (
        "self-hosted",
        "dev_deploy.py",
        "docker compose",
        "docker context",
        "docker_host",
        "ssh ",
        "scp ",
        "rsync ",
        "tailscale",
        "cloudflared",
    ):
        assert forbidden not in text


def test_모든_action은_full_commit_SHA로_고정한다():
    for path in (DEV_WORKFLOW, CI_WORKFLOW):
        uses = re.findall(r"uses:\s*([^\s#]+)", source(path))
        assert uses
        for item in uses:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", item), item


def test_attest_action은_고정_SHA이고_분리된_signer와_approve에서만_권한을_받는다():
    ci = source(CI_WORKFLOW)
    cd = source(DEV_WORKFLOW)
    attest_uses = re.findall(r"uses:\s*(actions/attest@[^\s#]+)", ci + cd)
    assert len(attest_uses) == 3
    assert all(re.fullmatch(r"actions/attest@[0-9a-f]{40}", item) for item in attest_uses)

    publish = job_source(CI_WORKFLOW, "publish")
    signer = job_source(CI_WORKFLOW, "attest-release")
    approve = job_source(DEV_WORKFLOW, "approve-development")
    for privileged_job in (signer, approve):
        before_steps = privileged_job.split("steps:", 1)[0]
        assert "attestations: write" in before_steps
        assert "artifact-metadata: write" in before_steps
        assert "id-token: write" in before_steps

    publish_permissions = publish.split("steps:", 1)[0]
    assert "packages: write" in publish_permissions
    assert "attestations: write" not in publish_permissions
    assert "artifact-metadata: write" not in publish_permissions
    assert "id-token: write" not in publish_permissions
    assert "packages: write" not in signer.split("steps:", 1)[0]
    assert "attestations: write" not in job_source(CI_WORKFLOW, "test-build")
    assert "id-token: write" not in job_source(CI_WORKFLOW, "test-build")
    assert "attestations: write" not in job_source(DEV_WORKFLOW, "verify-release")
    assert "artifact-metadata: write" not in job_source(DEV_WORKFLOW, "verify-release")
    assert "id-token: write" not in job_source(DEV_WORKFLOW, "verify-release")
    assert ci.count("attestations: write") == ci.count("id-token: write") == 1
    assert cd.count("attestations: write") == cd.count("id-token: write") == 1
    assert ci.count("artifact-metadata: write") == 1
    assert cd.count("artifact-metadata: write") == 1


def test_CI는_테스트한_image를_게시한뒤_digest_manifest를_남긴다():
    text = source(CI_WORKFLOW)
    assert "needs: test-build" in text
    assert "Publish the same tested image without rebuilding" in text
    assert "org.opencontainers.image.revision" in text
    assert "Create canonical release metadata from the pinned workflow" in text
    assert '"workflow_path": ".github/workflows/backend-ci.yml"' in text
    assert "conan-release-${{ github.sha }}-${{ github.run_attempt }}" in text
    assert text.count("github.event_name == 'push' && github.ref == 'refs/heads/main'") == 4
    assert "needs.test-build.outputs.tested-image-artifact" in text
    assert "set -euo pipefail" in text


def test_image_release와_environment_request는_각각_Sigstore_bundle을_남긴다():
    ci = source(CI_WORKFLOW)
    cd = source(DEV_WORKFLOW)
    assert "steps.attest-image.outputs.bundle-path" in ci
    assert "conan-image.sigstore.json" in ci
    assert "steps.attest-release.outputs.bundle-path" in ci
    assert "conan-release.sigstore.json" in ci
    assert "steps.attest-request.outputs.bundle-path" in cd
    assert "conan-development-request.sigstore.json" in cd
    assert "subject-name: ${{ needs.publish.outputs.image-repository }}" in ci
    assert "subject-digest: ${{ needs.publish.outputs.image-digest }}" in ci
    assert "push-to-registry" not in ci
    assert "Require the exact release attestation set" in ci
    assert "Require the exact release attestation set" in cd
    assert "Require the exact five-file deployment artifact" in cd


def test_write권한_job은_checkout이나_저장소코드를_실행하지_않는다():
    publish = job_source(CI_WORKFLOW, "publish")
    signer = job_source(CI_WORKFLOW, "attest-release")
    approve = job_source(DEV_WORKFLOW, "approve-development")

    for privileged_job in (publish, signer, approve):
        assert "actions/checkout" not in privileged_job
        assert "scripts/release_manifest.py" not in privileged_job
    assert "python -I - <<'PY'" in signer
    assert "python -I - <<'PY'" in approve
    assert "GHCR_TOKEN" in publish
    assert "GHCR_TOKEN" not in signer
    assert "GHCR_TOKEN" not in approve


def test_pinned_CI생성기는_host_release계약과같은_canonical_JSON을만든다(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_REPOSITORY": "Dynamic-Juo/be",
        "GITHUB_REPOSITORY_ID": "1355990630",
        "GITHUB_REPOSITORY_OWNER_ID": "324487638",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "321",
        "GITHUB_RUN_ATTEMPT": "4",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "IMAGE_REPOSITORY": "ghcr.io/dynamic-juo/be",
        "IMAGE_DIGEST": "sha256:" + "b" * 64,
    }
    subprocess.run(
        [sys.executable, "-I", "-"],
        input=inline_python(CI_WORKFLOW, "attest-release"),
        text=True,
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )

    value = release_manifest.read_canonical_json(tmp_path / "conan-release.json")
    release_manifest.validate_manifest(
        value,
        repository="Dynamic-Juo/be",
        repository_id=1355990630,
        repository_owner_id=324487638,
        source_sha="a" * 40,
        ci_run_id="321",
        ci_run_attempt="4",
    )


def test_pinned_CD생성기는_host_request계약과같은_canonical_JSON을만든다(tmp_path):
    gated = tmp_path / "gated-request"
    gated.mkdir()
    release = release_manifest.build_manifest(
        repository="Dynamic-Juo/be",
        repository_id=1355990630,
        repository_owner_id=324487638,
        source_sha="a" * 40,
        ci_run_id="321",
        ci_run_attempt="4",
        image="ghcr.io/dynamic-juo/be@sha256:" + "b" * 64,
    )
    release_manifest.write_manifest(gated / "conan-release.json", release)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_REPOSITORY": "Dynamic-Juo/be",
        "GITHUB_REPOSITORY_ID": "1355990630",
        "GITHUB_REPOSITORY_OWNER_ID": "324487638",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "777",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "DISPATCH_ACTOR": "5dotseven",
        "DISPATCH_ACTOR_ID": "1234567",
        "EXPECTED_CI_RUN_ID": "321",
        "EXPECTED_CI_RUN_ATTEMPT": "4",
    }
    subprocess.run(
        [sys.executable, "-I", "-"],
        input=inline_python(DEV_WORKFLOW, "approve-development"),
        text=True,
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )

    request = release_manifest.read_canonical_json(
        gated / "conan-development-request.json"
    )
    release_manifest.validate_deployment_request(
        request,
        target_id="conan-development",
        repository="Dynamic-Juo/be",
        repository_id=1355990630,
        repository_owner_id=324487638,
        environment="conan-development",
        now=datetime.now(timezone.utc),
    )


def test_pinned_CD생성기는_승인입력과다른_CI_release를거부한다(tmp_path):
    gated = tmp_path / "gated-request"
    gated.mkdir()
    release = release_manifest.build_manifest(
        repository="Dynamic-Juo/be",
        repository_id=1355990630,
        repository_owner_id=324487638,
        source_sha="a" * 40,
        ci_run_id="321",
        ci_run_attempt="4",
        image="ghcr.io/dynamic-juo/be@sha256:" + "b" * 64,
    )
    release_manifest.write_manifest(gated / "conan-release.json", release)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_REPOSITORY": "Dynamic-Juo/be",
        "GITHUB_REPOSITORY_ID": "1355990630",
        "GITHUB_REPOSITORY_OWNER_ID": "324487638",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "777",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "DISPATCH_ACTOR": "5dotseven",
        "DISPATCH_ACTOR_ID": "1234567",
        "EXPECTED_CI_RUN_ID": "999",
        "EXPECTED_CI_RUN_ATTEMPT": "4",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        input=inline_python(DEV_WORKFLOW, "approve-development"),
        text=True,
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "approved CI input" in result.stderr
    assert not (gated / "conan-development-request.json").exists()


def test_최종_artifact는_request_release_image_bundle_다섯개만_명시한다():
    text = source(DEV_WORKFLOW)
    marker = (
        "name: environment-bound-conan-development-${{ inputs.source_sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )
    upload = text.split(marker, 1)[1]
    expected = {
        "gated-request/conan-development-request.json",
        "gated-request/conan-development-request.sigstore.json",
        "gated-request/conan-release.json",
        "gated-request/conan-release.sigstore.json",
        "gated-request/conan-image.sigstore.json",
    }
    path_block = upload.split("path: |", 1)[1].split("retention-days:", 1)[0]
    actual = {line.strip() for line in path_block.splitlines() if line.strip()}
    assert actual == expected

    verified_upload = text.split(
        "name: ${{ steps.verified-release-artifact.outputs.name }}", 1
    )[1].split("path: |", 1)[1].split("retention-days:", 1)[0]
    assert {line.strip() for line in verified_upload.splitlines() if line.strip()} == {
        "verified-release/conan-release.json",
        "verified-release/conan-release.sigstore.json",
        "verified-release/conan-image.sigstore.json",
    }


def test_backend_CI_release_artifact는_manifest와두_bundle만_명시한다():
    text = source(CI_WORKFLOW)
    marker = "name: conan-release-${{ github.sha }}-${{ github.run_attempt }}"
    upload = text.split(marker, 1)[1]
    path_block = upload.split("path: |", 1)[1].split("retention-days:", 1)[0]
    assert {line.strip() for line in path_block.splitlines() if line.strip()} == {
        "conan-release.json",
        "conan-release.sigstore.json",
        "conan-image.sigstore.json",
    }


def test_부분재실행도_선행_job이_기록한_artifact_identity를_사용한다():
    text = source(DEV_WORKFLOW)
    assert "steps.verified-release-artifact.outputs.name" in text
    assert "needs.verify-release.outputs.verified-release-artifact" in text


def test_호스트_예시는_기본적으로_비활성이다():
    text = source(ROOT / "deploy/dev-host-config.example.json")
    assert '"schema_version": 4' in text
    assert '"enabled": false' in text
    assert '"admission_control": "durable-api-drain-v1"' in text
    assert (
        '"request_authentication": '
        '"github-image-and-manifest-attestations-ci-and-environment-review-v3"'
        in text
    )
    assert '"bootstrap_active": null' in text
    assert '"repository_id": 1355990630' in text
    assert '"repository_owner_id": 324487638' in text
    assert '"backend_ci_workflow_id": 1' in text
    assert '"backend_ci_workflow_sha256": "000000000000' in text
    assert '"deployment_workflow_id": 1' in text
    assert '"deployment_workflow_sha256": "000000000000' in text
    assert '"environment_id": 1' in text
    assert '"allowed_reviewer_user_ids": [1]' in text
    assert '"trusted_compose_files"' in text
    assert '"gh_binary_sha256"' in text
    assert '"docker_binary_sha256"' in text
    assert '"compose_binary_sha256"' in text
    assert '"state_dir"' in text
    assert '"global_lock_file"' in text
    assert '"docker_binary": "/absolute/path/to/' in text
    assert '"docker_config_dir": "/absolute/path/to/' in text
    assert "/absolute/path/to/" in text


def test_CD전용_Compose는_flattened_image_only_startup_fence계약이다():
    text = source(ROOT / "deploy/compose.development.yml")
    assert "${CONAN_IMAGE:?" in text
    assert "DEEPCHECK_START_DRAINED" in text
    assert "DEEPCHECK_DEPLOYMENT_STATE_FILE" in text
    assert "--pull" not in text
    for forbidden in ("extends:", "include:", "build:", "ports:"):
        assert forbidden not in text
    assert "cap_drop:" in text
    assert "no-new-privileges:true" in text
