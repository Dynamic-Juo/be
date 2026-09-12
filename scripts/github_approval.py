"""GitHub 서버가 기록한 CI provenance와 배포 승인을 검증한다.

Artifact attestation은 artifact와 workflow identity를 연결하지만 workflow bytes가
검토된 버전인지, required reviewer가 실제로 승인했는지는 보장하지 않는다. 호스트는
Docker에 접근하기 전에 Actions/Contents read-only API를 조회해 exact CI run과 workflow
bytes, deployment run과 environment review를 대조한다. API가 attempt별 approval
history를 제공하지 않으므로 deployment request는 첫 attempt만 허용한다.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


API_VERSION = "2026-03-10"
API_HOST = "github.com"
DEPLOYMENT_WORKFLOW_PATH = ".github/workflows/dev-deployment.yml"
BACKEND_CI_WORKFLOW_PATH = ".github/workflows/backend-ci.yml"
ACTOR_RE = re.compile(r"^[A-Za-z0-9-]{1,64}(\[bot\])?$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENVIRONMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]*$")
MAX_WORKFLOW_BYTES = 256 * 1024


class ApprovalError(ValueError):
    """GitHub server-side 승인 근거가 고정 정책과 다를 때 발생한다."""


@dataclass(frozen=True)
class CiPolicy:
    """호스트가 신뢰하는 backend CI run과 workflow bytes 정책."""

    repository: str
    repository_id: int
    repository_owner_id: int
    workflow_id: int
    workflow_path: str
    workflow_sha256: str
    source_sha: str
    run_id: int
    run_attempt: int

    def validate(self) -> None:
        if REPOSITORY_RE.fullmatch(self.repository) is None:
            raise ApprovalError("repository는 owner/name 형식이어야 한다")
        if self.workflow_path != BACKEND_CI_WORKFLOW_PATH:
            raise ApprovalError("backend CI workflow path가 고정값과 다르다")
        if SHA256_RE.fullmatch(self.workflow_sha256) is None:
            raise ApprovalError("backend CI workflow SHA-256 형식이 잘못됐다")
        if SOURCE_SHA_RE.fullmatch(self.source_sha) is None:
            raise ApprovalError("source SHA 형식이 잘못됐다")
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
            ("workflow_id", self.workflow_id),
            ("run_id", self.run_id),
            ("run_attempt", self.run_attempt),
        ):
            if type(value) is not int or value < 1:
                raise ApprovalError(f"{label}는 양의 정수여야 한다")


@dataclass(frozen=True)
class ApprovalPolicy:
    repository: str
    repository_id: int
    repository_owner_id: int
    workflow_id: int
    workflow_path: str
    workflow_sha256: str
    source_sha: str
    run_id: int
    run_attempt: int
    environment: str
    environment_id: int
    dispatch_actor: str
    dispatch_actor_id: int
    allowed_reviewer_user_ids: frozenset[int]

    def validate(self) -> None:
        if REPOSITORY_RE.fullmatch(self.repository) is None:
            raise ApprovalError("repository는 owner/name 형식이어야 한다")
        if self.workflow_path != DEPLOYMENT_WORKFLOW_PATH:
            raise ApprovalError("deployment workflow path가 고정값과 다르다")
        if SHA256_RE.fullmatch(self.workflow_sha256) is None:
            raise ApprovalError("deployment workflow SHA-256 형식이 잘못됐다")
        if SOURCE_SHA_RE.fullmatch(self.source_sha) is None:
            raise ApprovalError("source SHA 형식이 잘못됐다")
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
            ("workflow_id", self.workflow_id),
            ("run_id", self.run_id),
            ("environment_id", self.environment_id),
            ("dispatch_actor_id", self.dispatch_actor_id),
        ):
            if type(value) is not int or value < 1:
                raise ApprovalError(f"{label}는 양의 정수여야 한다")
        if self.run_attempt != 1:
            raise ApprovalError(
                "GitHub approval history는 attempt별 기록이 아니므로 첫 attempt만 허용한다"
            )
        if ACTOR_RE.fullmatch(self.dispatch_actor) is None:
            raise ApprovalError("dispatch actor 형식이 잘못됐다")
        if ENVIRONMENT_RE.fullmatch(self.environment) is None:
            raise ApprovalError("environment 이름 형식이 잘못됐다")
        reviewers = self.allowed_reviewer_user_ids
        if not isinstance(reviewers, frozenset) or not 1 <= len(reviewers) <= 6:
            raise ApprovalError("승인 reviewer ID allowlist는 1~6개여야 한다")
        if any(type(item) is not int or item < 1 for item in reviewers):
            raise ApprovalError("승인 reviewer ID는 양의 정수여야 한다")


def _api_command(gh_binary: Path, endpoint: str) -> list[str]:
    return [
        str(gh_binary),
        "api",
        "--hostname",
        API_HOST,
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
        endpoint,
    ]


def ci_run_command(*, gh_binary: Path, policy: CiPolicy) -> list[str]:
    """Manifest가 지목한 exact backend CI attempt를 조회한다."""

    policy.validate()
    return _api_command(
        gh_binary,
        (
            f"repos/{policy.repository}/actions/runs/{policy.run_id}/"
            f"attempts/{policy.run_attempt}"
        ),
    )


def ci_workflow_command(*, gh_binary: Path, policy: CiPolicy) -> list[str]:
    """CI source commit에 있던 backend workflow bytes를 조회한다."""

    policy.validate()
    return _api_command(
        gh_binary,
        (
            f"repos/{policy.repository}/contents/{policy.workflow_path}"
            f"?ref={policy.source_sha}"
        ),
    )


def run_command(*, gh_binary: Path, policy: ApprovalPolicy) -> list[str]:
    policy.validate()
    return _api_command(
        gh_binary,
        (
            f"repos/{policy.repository}/actions/runs/{policy.run_id}/"
            f"attempts/{policy.run_attempt}"
        ),
    )


def approvals_command(*, gh_binary: Path, policy: ApprovalPolicy) -> list[str]:
    policy.validate()
    return _api_command(
        gh_binary,
        f"repos/{policy.repository}/actions/runs/{policy.run_id}/approvals",
    )


def current_run_command(*, gh_binary: Path, policy: ApprovalPolicy) -> list[str]:
    """최신 attempt가 여전히 1인지 최종 확인하는 run-level 조회."""

    policy.validate()
    return _api_command(
        gh_binary,
        f"repos/{policy.repository}/actions/runs/{policy.run_id}",
    )


def workflow_command(*, gh_binary: Path, policy: ApprovalPolicy) -> list[str]:
    """서명 workflow의 source commit bytes를 가져오는 Contents API 조회."""

    policy.validate()
    return _api_command(
        gh_binary,
        (
            f"repos/{policy.repository}/contents/{policy.workflow_path}"
            f"?ref={policy.source_sha}"
        ),
    )


def environment_command(*, gh_binary: Path, policy: ApprovalPolicy) -> list[str]:
    """현재 protected environment 설정을 읽는 조회."""

    policy.validate()
    return _api_command(
        gh_binary,
        f"repos/{policy.repository}/environments/{policy.environment}",
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ApprovalError(f"GitHub API 응답에 중복 JSON 키가 있다: {key}")
        value[key] = item
    return value


def _parse_json(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ApprovalError(f"GitHub {label} API 응답이 JSON이 아니다") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApprovalError(f"GitHub {label} 값이 객체가 아니다")
    return value


def verify_run_output(raw: str, policy: ApprovalPolicy) -> dict[str, Any]:
    policy.validate()
    value = _mapping(_parse_json(raw, label="run"), "run")
    expected = {
        "id": policy.run_id,
        "run_attempt": 1,
        "head_branch": "main",
        "head_sha": policy.source_sha,
        "workflow_id": policy.workflow_id,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ApprovalError(f"GitHub run의 {key}가 기대값과 다르다")
    # GitHub.com의 실제 run API는 bare path를 반환하지만 API 문서의 일부
    # 응답 예시는 `@main` suffix를 포함한다. workflow_id가 immutable anchor이며
    # 두 형태 외의 ref/path 표기는 모두 거부한다.
    if value.get("path") not in {
        policy.workflow_path,
        f"{policy.workflow_path}@main",
    }:
        raise ApprovalError("GitHub run의 path가 기대값과 다르다")

    repository = _mapping(value.get("repository"), "run.repository")
    owner = _mapping(repository.get("owner"), "run.repository.owner")
    repository_expected = {
        "full_name": policy.repository,
        "id": policy.repository_id,
    }
    for key, expected_value in repository_expected.items():
        if repository.get(key) != expected_value:
            raise ApprovalError(f"GitHub run repository의 {key}가 기대값과 다르다")
    if owner.get("id") != policy.repository_owner_id:
        raise ApprovalError("GitHub run repository owner ID가 기대값과 다르다")

    actor = _mapping(value.get("actor"), "run.actor")
    if actor.get("id") != policy.dispatch_actor_id:
        raise ApprovalError("GitHub run actor ID가 요청과 다르다")
    login = actor.get("login")
    if not isinstance(login, str) or login.casefold() != policy.dispatch_actor.casefold():
        raise ApprovalError("GitHub run actor login이 요청과 다르다")
    if actor.get("type") != "User":
        raise ApprovalError("배포 요청 actor는 GitHub User여야 한다")
    return value


def verify_approvals_output(raw: str, policy: ApprovalPolicy) -> dict[str, Any]:
    policy.validate()
    value = _parse_json(raw, label="approval history")
    if not isinstance(value, list) or len(value) != 1:
        raise ApprovalError("단일 target run의 approval history는 정확히 한 건이어야 한다")

    matching: list[dict[str, Any]] = []
    for index, raw_review in enumerate(value):
        review = _mapping(raw_review, f"approval[{index}]")
        environments = review.get("environments")
        if not isinstance(environments, list):
            raise ApprovalError("GitHub approval environments가 배열이 아니다")
        for raw_environment in environments:
            environment = _mapping(raw_environment, "approval environment")
            if (
                environment.get("id") == policy.environment_id
                or environment.get("name") == policy.environment
            ):
                if not (
                    environment.get("id") == policy.environment_id
                    and environment.get("name") == policy.environment
                ):
                    raise ApprovalError("GitHub environment ID와 이름이 서로 일치하지 않는다")
                matching.append(review)

    if len(matching) != 1:
        raise ApprovalError("exact environment의 승인 기록이 정확히 한 건이 아니다")
    review = matching[0]
    environments = review["environments"]
    if len(environments) != 1:
        raise ApprovalError("한 review에서 여러 environment를 함께 승인할 수 없다")
    if review.get("state") != "approved":
        raise ApprovalError("GitHub environment review가 approved 상태가 아니다")
    user = _mapping(review.get("user"), "approval user")
    reviewer_id = user.get("id")
    if reviewer_id == policy.dispatch_actor_id:
        raise ApprovalError("배포 요청자의 self-approval은 허용하지 않는다")
    if type(reviewer_id) is not int or reviewer_id not in policy.allowed_reviewer_user_ids:
        raise ApprovalError("GitHub environment reviewer ID가 allowlist에 없다")
    if user.get("type") != "User":
        raise ApprovalError("GitHub environment reviewer는 User여야 한다")
    return review


def verify_workflow_output(raw: str, policy: ApprovalPolicy | CiPolicy) -> bytes:
    """Contents API의 base64 payload를 exact bytes로 복원해 host pin과 대조한다."""

    policy.validate()
    value = _mapping(_parse_json(raw, label="workflow contents"), "workflow contents")
    if value.get("type") != "file" or value.get("path") != policy.workflow_path:
        raise ApprovalError("GitHub workflow contents path/type이 기대값과 다르다")
    if value.get("name") != Path(policy.workflow_path).name:
        raise ApprovalError("GitHub workflow contents name이 기대값과 다르다")
    size = value.get("size")
    content = value.get("content")
    if (
        type(size) is not int
        or not 1 <= size <= MAX_WORKFLOW_BYTES
        or not isinstance(content, str)
        or len(content) > ((MAX_WORKFLOW_BYTES + 2) // 3 * 4 + 8192)
        or BASE64_RE.fullmatch(content) is None
        or value.get("encoding") != "base64"
    ):
        raise ApprovalError("GitHub workflow contents encoding/size가 잘못됐다")
    compact = "".join(content.splitlines())
    try:
        payload = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApprovalError("GitHub workflow contents base64가 잘못됐다") from exc
    if len(payload) != size:
        raise ApprovalError("GitHub workflow contents size가 실제 bytes와 다르다")
    if hashlib.sha256(payload).hexdigest() != policy.workflow_sha256:
        raise ApprovalError("GitHub workflow contents SHA-256이 host pin과 다르다")
    return payload


def verify_ci_run_output(raw: str, policy: CiPolicy) -> dict[str, Any]:
    """Exact attempt API record를 release와 host CI pin에 대조한다."""

    policy.validate()
    value = _mapping(_parse_json(raw, label="CI run"), "CI run")
    expected = {
        "id": policy.run_id,
        "run_attempt": policy.run_attempt,
        "head_branch": "main",
        "head_sha": policy.source_sha,
        "workflow_id": policy.workflow_id,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ApprovalError(f"GitHub CI run의 {key}가 기대값과 다르다")
    if value.get("path") not in {
        policy.workflow_path,
        f"{policy.workflow_path}@main",
    }:
        raise ApprovalError("GitHub CI run의 path가 기대값과 다르다")

    repository = _mapping(value.get("repository"), "CI run.repository")
    owner = _mapping(repository.get("owner"), "CI run.repository.owner")
    if (
        repository.get("full_name") != policy.repository
        or repository.get("id") != policy.repository_id
        or owner.get("id") != policy.repository_owner_id
    ):
        raise ApprovalError(
            "GitHub CI run repository 이름 또는 immutable ID가 기대값과 다르다"
        )
    return value


def verify_environment_output(raw: str, policy: ApprovalPolicy) -> dict[str, Any]:
    """Environment identity와 required-reviewer 설정을 현재 API에서 재검증한다."""

    policy.validate()
    value = _mapping(_parse_json(raw, label="environment"), "environment")
    if value.get("id") != policy.environment_id or value.get("name") != policy.environment:
        raise ApprovalError("GitHub environment ID/name이 host pin과 다르다")
    rules = value.get("protection_rules")
    if not isinstance(rules, list):
        raise ApprovalError("GitHub environment protection_rules가 배열이 아니다")
    required = [
        item for item in rules
        if isinstance(item, dict) and item.get("type") == "required_reviewers"
    ]
    if len(required) != 1:
        raise ApprovalError("GitHub environment required_reviewers rule이 정확히 하나가 아니다")
    rule = required[0]
    if rule.get("prevent_self_review") is not True:
        raise ApprovalError("GitHub environment prevent_self_review가 활성화되지 않았다")
    reviewers = rule.get("reviewers")
    if not isinstance(reviewers, list) or not 1 <= len(reviewers) <= 6:
        raise ApprovalError("GitHub environment reviewers 개수가 허용 범위 밖이다")
    reviewer_ids: list[int] = []
    for raw_reviewer in reviewers:
        item = _mapping(raw_reviewer, "environment reviewer")
        reviewer = _mapping(item.get("reviewer"), "environment reviewer identity")
        reviewer_id = reviewer.get("id")
        if item.get("type") != "User" or reviewer.get("type") not in {None, "User"}:
            raise ApprovalError("GitHub environment reviewer는 User만 허용한다")
        if type(reviewer_id) is not int or reviewer_id < 1:
            raise ApprovalError("GitHub environment reviewer ID가 잘못됐다")
        reviewer_ids.append(reviewer_id)
    if (
        len(set(reviewer_ids)) != len(reviewer_ids)
        or frozenset(reviewer_ids) != policy.allowed_reviewer_user_ids
    ):
        raise ApprovalError("GitHub environment reviewer ID set이 host allowlist와 다르다")
    return value


def verify_ci(
    *,
    runner: Any,
    gh_binary: Path,
    policy: CiPolicy,
    env: dict[str, str],
) -> dict[str, Any]:
    """CI attempt와 그 source commit의 reviewed workflow bytes를 검증한다."""

    run = runner.run(ci_run_command(gh_binary=gh_binary, policy=policy), env=env)
    verified_run = verify_ci_run_output(run.stdout, policy)
    workflow = runner.run(
        ci_workflow_command(gh_binary=gh_binary, policy=policy), env=env
    )
    verify_workflow_output(workflow.stdout, policy)
    return verified_run


def verify(
    *,
    runner: Any,
    gh_binary: Path,
    policy: ApprovalPolicy,
    env: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """GitHub service-side run과 review를 순서대로 조회해 검증한다."""

    run = runner.run(run_command(gh_binary=gh_binary, policy=policy), env=env)
    verified_run = verify_run_output(run.stdout, policy)
    workflow = runner.run(
        workflow_command(gh_binary=gh_binary, policy=policy), env=env
    )
    verify_workflow_output(workflow.stdout, policy)
    environment = runner.run(
        environment_command(gh_binary=gh_binary, policy=policy), env=env
    )
    verify_environment_output(environment.stdout, policy)
    approvals = runner.run(
        approvals_command(gh_binary=gh_binary, policy=policy), env=env
    )
    verified_review = verify_approvals_output(approvals.stdout, policy)
    # approvals는 attempt-scoped가 아니다. 승인 뒤 current run을 다시 읽어
    # attempt 2의 review를 attempt 1 request에 섞는 공격을 fail-closed로 막는다.
    current = runner.run(
        current_run_command(gh_binary=gh_binary, policy=policy), env=env
    )
    verify_run_output(current.stdout, policy)
    return verified_run, verified_review
