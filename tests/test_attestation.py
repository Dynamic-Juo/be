import json
from pathlib import Path

import pytest

from scripts import attestation


POLICY = attestation.AttestationPolicy(
    repository="Dynamic-Juo/be",
    repository_id=1355990630,
    repository_owner_id=324487638,
    workflow_path=".github/workflows/dev-deployment.yml",
    source_sha="a" * 40,
    event="workflow_dispatch",
    run_id=777,
    run_attempt=2,
)


def certificate(**overrides):
    value = {
        "issuer": "https://token.actions.githubusercontent.com",
        "githubWorkflowRepository": "Dynamic-Juo/be",
        "githubWorkflowRef": "refs/heads/main",
        "buildSignerURI": (
            "https://github.com/Dynamic-Juo/be/"
            ".github/workflows/dev-deployment.yml@refs/heads/main"
        ),
        "buildSignerDigest": "a" * 40,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": "https://github.com/Dynamic-Juo/be",
        "sourceRepositoryDigest": "a" * 40,
        "sourceRepositoryRef": "refs/heads/main",
        "sourceRepositoryIdentifier": "1355990630",
        "sourceRepositoryOwnerURI": "https://github.com/Dynamic-Juo",
        "sourceRepositoryOwnerIdentifier": "324487638",
        "buildConfigURI": (
            "https://github.com/Dynamic-Juo/be/"
            ".github/workflows/dev-deployment.yml@refs/heads/main"
        ),
        "buildConfigDigest": "a" * 40,
        "buildTrigger": "workflow_dispatch",
        "runInvocationURI": (
            "https://github.com/Dynamic-Juo/be/actions/runs/777/attempts/2"
        ),
    }
    value.update(overrides)
    return value


def output(cert=None):
    return json.dumps([{
        "attestation": {"untrusted": "workflow predicate is not policy"},
        "verificationResult": {
            "signature": {"certificate": cert or certificate()},
            "statement": {"predicate": {"environment": "attacker-controlled"}},
        },
    }])


def test_정확한_인증서만_허용한다():
    verified = attestation.verify_output(output(), POLICY)
    assert verified["runInvocationURI"].endswith("/777/attempts/2")


@pytest.mark.parametrize(("key", "bad"), [
    ("sourceRepositoryIdentifier", "999"),
    ("sourceRepositoryOwnerIdentifier", "999"),
    ("runnerEnvironment", "self-hosted"),
    ("buildTrigger", "pull_request"),
    ("sourceRepositoryDigest", "b" * 40),
    ("buildSignerURI", "https://github.com/other/workflow.yml@refs/heads/main"),
    ("runInvocationURI", "https://github.com/Dynamic-Juo/be/actions/runs/778/attempts/2"),
])
def test_이름이_같아도_ID_workflow_run이_다르면_거부한다(key, bad):
    with pytest.raises(attestation.AttestationError, match=key):
        attestation.verify_output(output(certificate(**{key: bad})), POLICY)


def test_gh_명령은_검증정책을_모두_고정한다(tmp_path):
    bundle = tmp_path / "request.sigstore.json"
    args = attestation.command(
        gh_binary=Path("/trusted/gh"),
        subject="/inbox/request.json",
        policy=POLICY,
        bundle=bundle,
    )
    joined = " ".join(args)
    assert "--repo Dynamic-Juo/be" in joined
    assert "--signer-workflow Dynamic-Juo/be/.github/workflows/dev-deployment.yml" in joined
    assert "--source-ref refs/heads/main" in joined
    assert f"--source-digest {'a' * 40}" in joined
    assert "--deny-self-hosted-runners" in args
    assert args[-2:] == ["--bundle", str(bundle)]


def test_OCI_image검증은_exact_digest에_oci_prefix와_bundle을_사용한다(tmp_path):
    image = "ghcr.io/dynamic-juo/be@sha256:" + "b" * 64
    bundle = tmp_path / "image.sigstore.json"
    args = attestation.command(
        gh_binary=Path("/trusted/gh"),
        subject=f"oci://{image}",
        policy=POLICY,
        bundle=bundle,
    )

    assert args[:4] == [
        "/trusted/gh", "attestation", "verify", f"oci://{image}",
    ]
    assert args.count(f"oci://{image}") == 1
    assert args[-2:] == ["--bundle", str(bundle)]


def test_predicate값은_정책판정에_사용하지_않는다():
    value = json.loads(output())
    value[0]["verificationResult"]["statement"]["predicate"] = {
        "repository": "evil/repo",
        "environment": "not-approved",
    }
    assert attestation.verify_output(json.dumps(value), POLICY)
