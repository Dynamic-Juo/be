import json

import pytest

from scripts import release_manifest


REPOSITORY = "Dynamic-Juo/be"
SOURCE_SHA = "a" * 40
RUN_ID = "123456"
RUN_ATTEMPT = "1"
DIGEST = "sha256:" + "b" * 64
IMAGE = f"ghcr.io/dynamic-juo/be@{DIGEST}"


def manifest(**overrides):
    value = release_manifest.build_manifest(
        repository=REPOSITORY,
        source_sha=SOURCE_SHA,
        ci_run_id=RUN_ID,
        ci_run_attempt=RUN_ATTEMPT,
        image=IMAGE,
        created_at="2026-09-13T00:00:00+00:00",
    )
    value.update(overrides)
    return value


def test_release_manifest는_main_push와_digest를_고정한다():
    value = manifest()
    assert value["event"] == "push"
    assert value["workflow_path"] == ".github/workflows/backend-ci.yml"
    assert value["image"] == IMAGE
    assert value["image_digest"] == DIGEST


def test_수동_CI_metadata를_main_push로_위장하지_않는다():
    value = release_manifest.build_manifest(
        repository=REPOSITORY,
        source_sha=SOURCE_SHA,
        ci_run_id=RUN_ID,
        ci_run_attempt=RUN_ATTEMPT,
        image=IMAGE,
        event="workflow_dispatch",
    )
    assert value["event"] == "workflow_dispatch"
    with pytest.raises(release_manifest.ManifestError, match="event"):
        release_manifest.validate_manifest(
            value,
            repository=REPOSITORY,
            source_sha=SOURCE_SHA,
            ci_run_id=RUN_ID,
            ci_run_attempt=RUN_ATTEMPT,
        )


@pytest.mark.parametrize("image", [
    "ghcr.io/dynamic-juo/be:sha-" + SOURCE_SHA,
    "ghcr.io/other/be@" + DIGEST,
    "ghcr.io/dynamic-juo/be@sha256:" + "B" * 64,
    IMAGE + "; docker system prune",
])
def test_tag와_다른_저장소와_명령삽입은_거부한다(image):
    with pytest.raises(release_manifest.ManifestError):
        release_manifest.build_manifest(
            repository=REPOSITORY,
            source_sha=SOURCE_SHA,
            ci_run_id=RUN_ID,
            ci_run_attempt=RUN_ATTEMPT,
            image=image,
        )


def test_manifest_필드끼리_다르면_거부한다():
    with pytest.raises(release_manifest.ManifestError, match="image_digest"):
        release_manifest.validate_manifest(
            manifest(image_digest="sha256:" + "c" * 64),
            repository=REPOSITORY,
            source_sha=SOURCE_SHA,
            ci_run_id=RUN_ID,
            ci_run_attempt=RUN_ATTEMPT,
        )


def test_main_push의_성공한_CI_run만_허용한다():
    value = {
        "id": int(RUN_ID),
        "run_attempt": int(RUN_ATTEMPT),
        "head_branch": "main",
        "head_sha": SOURCE_SHA,
        "path": ".github/workflows/backend-ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPOSITORY},
    }
    release_manifest.validate_workflow_run(
        value,
        repository=REPOSITORY,
        source_sha=SOURCE_SHA,
        ci_run_id=RUN_ID,
        ci_run_attempt=RUN_ATTEMPT,
    )


@pytest.mark.parametrize(("key", "bad"), [
    ("head_branch", "feature/untrusted"),
    ("event", "pull_request"),
    ("event", "workflow_dispatch"),
    ("conclusion", "failure"),
    ("path", ".github/workflows/other.yml"),
])
def test_신뢰할_수_없는_CI_run은_거부한다(key, bad):
    value = {
        "id": int(RUN_ID),
        "run_attempt": int(RUN_ATTEMPT),
        "head_branch": "main",
        "head_sha": SOURCE_SHA,
        "path": ".github/workflows/backend-ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPOSITORY},
    }
    value[key] = bad
    with pytest.raises(release_manifest.ManifestError, match=key):
        release_manifest.validate_workflow_run(
            value,
            repository=REPOSITORY,
            source_sha=SOURCE_SHA,
            ci_run_id=RUN_ID,
            ci_run_attempt=RUN_ATTEMPT,
        )


def test_중복_JSON_키를_거부한다(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version": 1, "schema_version": 2}')
    with pytest.raises(release_manifest.ManifestError, match="중복 JSON 키"):
        release_manifest.read_json(path)


def test_CLI가_manifest를_만들고_검증한다(tmp_path, capsys):
    path = tmp_path / "release.json"
    common = [
        "--repository", REPOSITORY,
        "--source-sha", SOURCE_SHA,
        "--ci-run-id", RUN_ID,
        "--ci-run-attempt", RUN_ATTEMPT,
    ]
    assert release_manifest.main([
        "create", *common, "--event", "push", "--image", IMAGE, "--output", str(path)
    ]) == 0
    assert json.loads(path.read_text())["image"] == IMAGE
    assert release_manifest.main(["verify", *common, "--manifest", str(path)]) == 0
    assert capsys.readouterr().out.strip() == IMAGE


def test_승인요청은_개발계와_main_ref만_허용한다():
    request = release_manifest.build_deployment_request(
        release=manifest(),
        repository=REPOSITORY,
        environment="conan-development",
        request_run_id="777",
        request_run_attempt="1",
        request_sha="c" * 40,
        dispatch_actor="5dotseven",
        created_at="2026-09-13T00:10:00+00:00",
    )
    assert request["environment"] == "conan-development"
    assert request["request_ref"] == "refs/heads/main"
    assert request["release"]["image"] == IMAGE
    release_manifest.validate_deployment_request(request, repository=REPOSITORY)


def test_승인요청_내부_release_변조를_거부한다():
    request = release_manifest.build_deployment_request(
        release=manifest(),
        repository=REPOSITORY,
        environment="conan-development",
        request_run_id="777",
        request_run_attempt="1",
        request_sha="c" * 40,
        dispatch_actor="5dotseven",
    )
    request["release"]["image"] = "ghcr.io/dynamic-juo/be:latest"
    with pytest.raises(release_manifest.ManifestError):
        release_manifest.validate_deployment_request(request, repository=REPOSITORY)
