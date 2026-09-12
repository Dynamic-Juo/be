from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DEV_WORKFLOW = ROOT / ".github/workflows/dev-deployment.yml"
CI_WORKFLOW = ROOT / ".github/workflows/backend-ci.yml"


def source(path):
    return path.read_text()


def test_CD는_수동요청만_받고_PR코드를_실행하지_않는다():
    text = source(DEV_WORKFLOW)
    trigger = text.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "pull_request" not in trigger
    assert "pull_request_target" not in trigger
    assert "\npush:" not in trigger
    assert "self-hosted" not in text
    assert set(re.findall(r"runs-on:\s*([^\n]+)", text)) == {"ubuntu-24.04"}


def test_CD는_고정_environment와_동시실행방지를_사용한다():
    text = source(DEV_WORKFLOW)
    assert "group: conan-development-deployment" in text
    assert "cancel-in-progress: false" in text
    assert "name: conan-development" in text
    assert "CONAN_DEV_DEPLOY_ENABLED" in text
    assert "CONAN_DEV_ENVIRONMENT_CONFIGURED" in text
    assert text.count("ref: ${{ github.sha }}") == 2
    assert "ref: main" not in text
    assert (
        "environment-bound-conan-development-${{ inputs.source_sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    ) in text


def test_CD는_서버정보를_input으로_받거나_서버명령을_실행하지_않는다():
    text = source(DEV_WORKFLOW)
    inputs = text.split("permissions:", 1)[0]
    for forbidden_input in ("host:", "compose_file:", "env_file:", "project:", "service:"):
        assert forbidden_input not in inputs
    for forbidden_command in ("dev_deploy.py", "docker compose", "ssh ", "scp ", "rsync "):
        assert forbidden_command not in text
    assert "did not deploy" in text


def test_모든_action은_full_commit_SHA로_고정한다():
    for path in (DEV_WORKFLOW, CI_WORKFLOW):
        uses = re.findall(r"uses:\s*([^\s#]+)", source(path))
        assert uses
        for item in uses:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", item), item


def test_CI는_테스트한_image를_게시한뒤_digest_manifest를_남긴다():
    text = source(CI_WORKFLOW)
    assert "needs: test-build" in text
    assert "Publish the same tested image without rebuilding" in text
    assert "org.opencontainers.image.revision" in text
    assert "scripts/release_manifest.py create" in text
    assert "conan-release-${{ github.sha }}-${{ github.run_attempt }}" in text
    assert text.count("github.event_name == 'push' && github.ref == 'refs/heads/main'") == 4
    assert "needs.test-build.outputs.tested-image-artifact" in text
    assert "set -euo pipefail" in text


def test_부분재실행도_선행_job이_기록한_artifact_identity를_사용한다():
    text = source(DEV_WORKFLOW)
    assert "steps.verified-release-artifact.outputs.name" in text
    assert "needs.verify-release.outputs.verified-release-artifact" in text


def test_호스트_예시는_기본적으로_비활성이다():
    text = source(ROOT / "deploy/dev-host-config.example.json")
    assert '"enabled": false' in text
    assert '"admission_control": "unconfigured"' in text
    assert '"request_authentication": "unconfigured"' in text
    assert '"docker_binary": "/absolute/path/to/' in text
    assert '"docker_config_dir": "/absolute/path/to/' in text
    assert "/absolute/path/to/" in text
