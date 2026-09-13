import base64
import hashlib
import json
from pathlib import Path

import pytest

from scripts import github_approval


WORKFLOW = b"name: Development deployment request\n"
CI_WORKFLOW = b"name: Backend CI and ARM64 image\n"
POLICY = github_approval.ApprovalPolicy(
    repository="Dynamic-Juo/be",
    repository_id=1355990630,
    repository_owner_id=324487638,
    workflow_id=998877,
    workflow_path=".github/workflows/dev-deployment.yml",
    workflow_sha256=hashlib.sha256(WORKFLOW).hexdigest(),
    source_sha="a" * 40,
    run_id=777,
    run_attempt=1,
    environment="conan-development",
    environment_id=161088068,
    dispatch_actor="5dotseven",
    dispatch_actor_id=1234567,
    allowed_reviewer_user_ids=frozenset({7654321, 8888888}),
)
CI_POLICY = github_approval.CiPolicy(
    repository="Dynamic-Juo/be",
    repository_id=1355990630,
    repository_owner_id=324487638,
    workflow_id=887766,
    workflow_path=".github/workflows/backend-ci.yml",
    workflow_sha256=hashlib.sha256(CI_WORKFLOW).hexdigest(),
    source_sha="a" * 40,
    run_id=321,
    run_attempt=4,
)


def run_record(**overrides):
    value = {
        "id": 777,
        "run_attempt": 1,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "path": ".github/workflows/dev-deployment.yml",
        "workflow_id": 998877,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "repository": {
            "full_name": "Dynamic-Juo/be",
            "id": 1355990630,
            "owner": {"id": 324487638},
        },
        "actor": {"login": "5dotseven", "id": 1234567, "type": "User"},
    }
    value.update(overrides)
    return value


def ci_run_record(**overrides):
    value = {
        "id": 321,
        "run_attempt": 4,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "path": ".github/workflows/backend-ci.yml",
        "workflow_id": 887766,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {
            "full_name": "Dynamic-Juo/be",
            "id": 1355990630,
            "owner": {"id": 324487638},
        },
    }
    value.update(overrides)
    return value


def review(*, state="approved", environment_id=161088068,
           environment="conan-development", reviewer_id=7654321,
           reviewer_type="User"):
    return {
        "state": state,
        "environments": [{"id": environment_id, "name": environment}],
        "user": {"login": "reviewer", "id": reviewer_id, "type": reviewer_type},
    }


def workflow_contents(*, payload=WORKFLOW, path=".github/workflows/dev-deployment.yml"):
    return {
        "type": "file",
        "name": Path(path).name,
        "path": path,
        "encoding": "base64",
        "size": len(payload),
        "content": base64.encodebytes(payload).decode("ascii"),
    }


def environment(*, environment_id=161088068, name="conan-development",
                prevent_self_review=True, reviewer_ids=(7654321, 8888888)):
    return {
        "id": environment_id,
        "name": name,
        "protection_rules": [{
            "id": 3755,
            "type": "required_reviewers",
            "prevent_self_review": prevent_self_review,
            "reviewers": [
                {"type": "User", "reviewer": {"id": item, "type": "User"}}
                for item in reviewer_ids
            ],
        }],
    }


def test_exact_completed_run과_allowlisted_environment_review를_허용한다():
    verified_run = github_approval.verify_run_output(json.dumps(run_record()), POLICY)
    verified_review = github_approval.verify_approvals_output(
        json.dumps([review()]), POLICY
    )
    assert verified_run["head_sha"] == "a" * 40
    assert verified_review["user"]["id"] == 7654321
    assert github_approval.verify_workflow_output(
        json.dumps(workflow_contents()), POLICY
    ) == WORKFLOW
    assert github_approval.verify_environment_output(
        json.dumps(environment()), POLICY
    )["id"] == 161088068


@pytest.mark.parametrize(("key", "bad"), [
    ("id", 778),
    ("run_attempt", 2),
    ("head_branch", "feature/untrusted"),
    ("head_sha", "b" * 40),
    ("path", ".github/workflows/other.yml"),
    ("workflow_id", 123),
    ("event", "push"),
    ("status", "in_progress"),
    ("conclusion", "failure"),
])
def test_run_identity나_성공상태가_다르면_거부한다(key, bad):
    with pytest.raises(github_approval.ApprovalError, match=key):
        github_approval.verify_run_output(json.dumps(run_record(**{key: bad})), POLICY)


def test_repository와_actor의_immutable_ID를_검증한다():
    bad_repository = run_record(repository={
        "full_name": "Dynamic-Juo/be",
        "id": 999,
        "owner": {"id": 324487638},
    })
    with pytest.raises(github_approval.ApprovalError, match="repository.*id"):
        github_approval.verify_run_output(json.dumps(bad_repository), POLICY)

    bad_actor = run_record(actor={"login": "5dotseven", "id": 999, "type": "User"})
    with pytest.raises(github_approval.ApprovalError, match="actor ID"):
        github_approval.verify_run_output(json.dumps(bad_actor), POLICY)


@pytest.mark.parametrize("reviews", [
    [],
    [review(state="rejected")],
    [review(environment_id=999)],
    [review(environment="other")],
    [review(reviewer_id=999)],
    [review(), review()],
    [{**review(), "environments": [
        {"id": 161088068, "name": "conan-development"},
        {"id": 2, "name": "other"},
    ]}],
])
def test_부정확하거나_중복된_environment_review를_거부한다(reviews):
    with pytest.raises(github_approval.ApprovalError):
        github_approval.verify_approvals_output(json.dumps(reviews), POLICY)


def test_self_approval과_User가_아닌_reviewer를_거부한다():
    with pytest.raises(github_approval.ApprovalError, match="self-approval"):
        github_approval.verify_approvals_output(
            json.dumps([review(reviewer_id=POLICY.dispatch_actor_id)]), POLICY
        )
    with pytest.raises(github_approval.ApprovalError, match="User"):
        github_approval.verify_approvals_output(
            json.dumps([review(reviewer_type="Bot")]), POLICY
        )


def test_API_command는_host_version_exact_run을_고정한다():
    command = github_approval.run_command(
        gh_binary=Path("/trusted/gh"), policy=POLICY
    )
    assert command[0] == "/trusted/gh"
    assert command[1:4] == ["api", "--hostname", "github.com"]
    assert "X-GitHub-Api-Version: 2026-03-10" in command
    assert command[-1].endswith("/actions/runs/777/attempts/1")
    assert github_approval.approvals_command(
        gh_binary=Path("/trusted/gh"), policy=POLICY
    )[-1].endswith("/actions/runs/777/approvals")
    assert github_approval.current_run_command(
        gh_binary=Path("/trusted/gh"), policy=POLICY
    )[-1].endswith("/actions/runs/777")
    assert github_approval.workflow_command(
        gh_binary=Path("/trusted/gh"), policy=POLICY
    )[-1].endswith(
        "/contents/.github/workflows/dev-deployment.yml?ref=" + "a" * 40
    )
    assert github_approval.environment_command(
        gh_binary=Path("/trusted/gh"), policy=POLICY
    )[-1].endswith("/environments/conan-development")


def test_attempt별_승인기록이_없으므로_재실행을_거부한다():
    policy = github_approval.ApprovalPolicy(
        **{**POLICY.__dict__, "run_attempt": 2}
    )
    with pytest.raises(github_approval.ApprovalError, match="첫 attempt"):
        github_approval.run_command(gh_binary=Path("/trusted/gh"), policy=policy)


def test_API_JSON의_중복키를_거부한다():
    with pytest.raises(github_approval.ApprovalError, match="중복 JSON 키"):
        github_approval.verify_run_output('{"id":777,"id":778}', POLICY)


def test_exact_CI_attempt와_reviewed_workflow_bytes를_허용한다():
    verified = github_approval.verify_ci_run_output(
        json.dumps(ci_run_record()), CI_POLICY
    )
    assert verified["id"] == 321
    assert github_approval.verify_workflow_output(
        json.dumps(workflow_contents(
            payload=CI_WORKFLOW,
            path=".github/workflows/backend-ci.yml",
        )),
        CI_POLICY,
    ) == CI_WORKFLOW


@pytest.mark.parametrize(("key", "bad"), [
    ("id", 322),
    ("run_attempt", 3),
    ("head_branch", "feature/untrusted"),
    ("head_sha", "b" * 40),
    ("path", ".github/workflows/other.yml"),
    ("workflow_id", 998877),
    ("event", "workflow_dispatch"),
    ("status", "in_progress"),
    ("conclusion", "failure"),
])
def test_CI_run_identity나_성공상태가_다르면_거부한다(key, bad):
    with pytest.raises(github_approval.ApprovalError, match=key):
        github_approval.verify_ci_run_output(
            json.dumps(ci_run_record(**{key: bad})), CI_POLICY
        )


@pytest.mark.parametrize("repository", [
    {
        "full_name": "other/be",
        "id": 1355990630,
        "owner": {"id": 324487638},
    },
    {
        "full_name": "Dynamic-Juo/be",
        "id": 999,
        "owner": {"id": 324487638},
    },
    {
        "full_name": "Dynamic-Juo/be",
        "id": 1355990630,
        "owner": {"id": 999},
    },
])
def test_CI_run_repository_immutable_identity가_다르면_거부한다(repository):
    with pytest.raises(github_approval.ApprovalError, match="repository"):
        github_approval.verify_ci_run_output(
            json.dumps(ci_run_record(repository=repository)), CI_POLICY
        )


def test_CI_API_commands는_exact_attempt와_source_SHA_contents를_조회한다():
    run = github_approval.ci_run_command(
        gh_binary=Path("/trusted/gh"), policy=CI_POLICY
    )
    workflow = github_approval.ci_workflow_command(
        gh_binary=Path("/trusted/gh"), policy=CI_POLICY
    )

    assert run[-1].endswith("/actions/runs/321/attempts/4")
    assert workflow[-1].endswith(
        "/contents/.github/workflows/backend-ci.yml?ref=" + "a" * 40
    )
    assert "X-GitHub-Api-Version: 2026-03-10" in run
    assert "X-GitHub-Api-Version: 2026-03-10" in workflow


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []

    def run(self, command, *, env):
        self.commands.append((command, env))
        return type("Result", (), {"stdout": self.outputs.pop(0)})()


def test_verify_CI는_exact_attempt뒤_workflow_bytes를_검증한다():
    runner = FakeRunner([
        json.dumps(ci_run_record()),
        json.dumps(workflow_contents(
            payload=CI_WORKFLOW,
            path=".github/workflows/backend-ci.yml",
        )),
    ])

    verified = github_approval.verify_ci(
        runner=runner,
        gh_binary=Path("/trusted/gh"),
        policy=CI_POLICY,
        env={"GH_CONFIG_DIR": "/trusted/config"},
    )

    assert verified["id"] == 321
    assert runner.commands[0][0][-1].endswith("/actions/runs/321/attempts/4")
    assert runner.commands[1][0][-1].endswith(
        "/contents/.github/workflows/backend-ci.yml?ref=" + "a" * 40
    )


@pytest.mark.parametrize("contents", [
    workflow_contents(
        payload=b"name: attacker CI\n",
        path=".github/workflows/backend-ci.yml",
    ),
    workflow_contents(
        payload=CI_WORKFLOW,
        path=".github/workflows/other.yml",
    ),
])
def test_verify_CI는_workflow_bytes나_path가다르면_fail_closed한다(contents):
    runner = FakeRunner([json.dumps(ci_run_record()), json.dumps(contents)])
    with pytest.raises(github_approval.ApprovalError):
        github_approval.verify_ci(
            runner=runner,
            gh_binary=Path("/trusted/gh"),
            policy=CI_POLICY,
            env={"GH_CONFIG_DIR": "/trusted/config"},
        )
    assert len(runner.commands) == 2


def test_승인_뒤_current_run을_다시_읽어_attempt_혼합을_막는다():
    current_attempt_two = run_record(run_attempt=2)
    runner = FakeRunner([
        json.dumps(run_record()),
        json.dumps(workflow_contents()),
        json.dumps(environment()),
        json.dumps([review()]),
        json.dumps(current_attempt_two),
    ])
    with pytest.raises(github_approval.ApprovalError, match="run_attempt"):
        github_approval.verify(
            runner=runner,
            gh_binary=Path("/trusted/gh"),
            policy=POLICY,
            env={"GH_CONFIG_DIR": "/trusted/config"},
        )
    assert len(runner.commands) == 5


@pytest.mark.parametrize("path", [
    ".github/workflows/dev-deployment.yml",
    ".github/workflows/dev-deployment.yml@main",
])
def test_API가_반환하는_두_exact_main_path형식을_허용한다(path):
    github_approval.verify_run_output(json.dumps(run_record(path=path)), POLICY)


@pytest.mark.parametrize("bad", [
    b"name: changed workflow\n",
    b"",
])
def test_workflow_bytes가_host_pin과_다르면_거부한다(bad):
    with pytest.raises(github_approval.ApprovalError):
        github_approval.verify_workflow_output(
            json.dumps(workflow_contents(payload=bad)), POLICY
        )


@pytest.mark.parametrize("value", [
    environment(environment_id=999),
    environment(name="other"),
    environment(prevent_self_review=False),
    environment(reviewer_ids=(7654321,)),
    environment(reviewer_ids=(7654321, 999)),
])
def test_environment_identity와_required_reviewer정책_drift를_거부한다(value):
    with pytest.raises(github_approval.ApprovalError):
        github_approval.verify_environment_output(json.dumps(value), POLICY)


def test_environment_team_reviewer와_중복_User_ID를_거부한다():
    team = environment()
    team["protection_rules"][0]["reviewers"][0]["type"] = "Team"
    with pytest.raises(github_approval.ApprovalError, match="User"):
        github_approval.verify_environment_output(json.dumps(team), POLICY)

    duplicate = environment(reviewer_ids=(7654321, 7654321))
    with pytest.raises(github_approval.ApprovalError, match="ID set"):
        github_approval.verify_environment_output(json.dumps(duplicate), POLICY)
