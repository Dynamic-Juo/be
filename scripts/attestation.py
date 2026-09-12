"""GitHub artifact attestation 결과에 host 배포 정책을 적용한다.

암호 검증은 고정된 GitHub CLI가 수행한다. 이 모듈은 CLI 성공만 믿지 않고 서명
인증서의 repository ID, workflow, ref, commit, trigger와 run/attempt를 모두 요청과
대조한다. Workflow가 마음대로 쓸 수 있는 provenance predicate는 정책 근거로 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class AttestationError(ValueError):
    """Attestation이 고정된 배포 정책을 충족하지 않을 때 발생한다."""


@dataclass(frozen=True)
class AttestationPolicy:
    repository: str
    repository_id: int
    repository_owner_id: int
    workflow_path: str
    source_sha: str
    event: str
    run_id: int
    run_attempt: int

    @property
    def repository_owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def signer_workflow(self) -> str:
        return f"{self.repository}/{self.workflow_path}"

    @property
    def signer_uri(self) -> str:
        return f"https://github.com/{self.signer_workflow}@refs/heads/main"

    @property
    def invocation_uri(self) -> str:
        return (
            f"https://github.com/{self.repository}/actions/runs/"
            f"{self.run_id}/attempts/{self.run_attempt}"
        )


def command(
    *,
    gh_binary: Path,
    subject: str,
    policy: AttestationPolicy,
    bundle: Path | None = None,
) -> list[str]:
    args = [
        str(gh_binary), "attestation", "verify", subject,
        "--repo", policy.repository,
        "--signer-workflow", policy.signer_workflow,
        "--cert-identity", policy.signer_uri,
        "--signer-digest", policy.source_sha,
        "--source-ref", "refs/heads/main",
        "--source-digest", policy.source_sha,
        "--deny-self-hosted-runners",
        "--format", "json",
        "--limit", "10",
    ]
    if bundle is not None:
        args.extend(["--bundle", str(bundle)])
    return args


def verify_output(raw: str, policy: AttestationPolicy) -> dict[str, Any]:
    try:
        results = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AttestationError("GitHub CLI attestation 결과가 JSON이 아니다") from exc
    if not isinstance(results, list) or not results or len(results) > 10:
        raise AttestationError("검증된 attestation 결과 수가 허용 범위 밖이다")

    failures: list[str] = []
    for result in results:
        try:
            certificate = _certificate(result)
            _verify_certificate(certificate, policy)
            return certificate
        except AttestationError as exc:
            failures.append(str(exc))
    raise AttestationError(
        "고정 정책과 일치하는 attestation 인증서가 없다: " + "; ".join(failures)
    )


def _certificate(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise AttestationError("attestation 결과 항목이 객체가 아니다")
    verification = result.get("verificationResult")
    if not isinstance(verification, dict):
        raise AttestationError("verificationResult가 없다")
    signature = verification.get("signature")
    if not isinstance(signature, dict):
        raise AttestationError("검증된 signature가 없다")
    certificate = signature.get("certificate")
    if not isinstance(certificate, dict):
        raise AttestationError("검증된 certificate가 없다")
    return certificate


def _verify_certificate(
    certificate: dict[str, Any], policy: AttestationPolicy
) -> None:
    expected = {
        "issuer": "https://token.actions.githubusercontent.com",
        "githubWorkflowRepository": policy.repository,
        "githubWorkflowRef": "refs/heads/main",
        "buildSignerURI": policy.signer_uri,
        "buildSignerDigest": policy.source_sha,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": f"https://github.com/{policy.repository}",
        "sourceRepositoryDigest": policy.source_sha,
        "sourceRepositoryRef": "refs/heads/main",
        "sourceRepositoryIdentifier": str(policy.repository_id),
        "sourceRepositoryOwnerURI": f"https://github.com/{policy.repository_owner}",
        "sourceRepositoryOwnerIdentifier": str(policy.repository_owner_id),
        "buildConfigURI": policy.signer_uri,
        "buildConfigDigest": policy.source_sha,
        "buildTrigger": policy.event,
        "runInvocationURI": policy.invocation_uri,
    }
    for key, expected_value in expected.items():
        if certificate.get(key) != expected_value:
            raise AttestationError(f"certificate {key}가 기대값과 다르다")

    # 구형 필드가 함께 있는 경우에도 상충하는 값을 허용하지 않는다.
    optional_expected = {
        "githubWorkflowTrigger": policy.event,
        "githubWorkflowSHA": policy.source_sha,
    }
    for key, expected_value in optional_expected.items():
        if key in certificate and certificate[key] != expected_value:
            raise AttestationError(f"certificate {key}가 기대값과 다르다")


def verify(
    *,
    runner: Any,
    gh_binary: Path,
    subject: str,
    policy: AttestationPolicy,
    env: dict[str, str],
    bundle: Path | None = None,
) -> dict[str, Any]:
    result = runner.run(
        command(
            gh_binary=gh_binary,
            subject=subject,
            policy=policy,
            bundle=bundle,
        ),
        env=env,
    )
    return verify_output(result.stdout, policy)
