import json
import os
from pathlib import Path
import stat

import pytest

from scripts import deployment_state


TARGET_ID = "conan-development"
SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/dynamic-juo/be@sha256:" + "b" * 64
IMAGE_ID = "sha256:" + "c" * 64
NOW = "2026-09-13T01:02:03+00:00"
TARGET_FINGERPRINT = "f" * 64
TRANSACTION_FINGERPRINT = "e" * 64


def state(**overrides):
    value = {
        "schema_version": 2,
        "target_id": TARGET_ID,
        "target_fingerprint": TARGET_FINGERPRINT,
        "active": {
            "image": IMAGE,
            "source_sha": SOURCE_SHA,
            "image_id": IMAGE_ID,
            "deployment_id": "tx-123",
        },
        "highest_request": {"run_id": 200, "run_attempt": 2},
        "highest_ci": {"run_id": 100, "run_attempt": 1},
        "last_outcome": "committed",
        "updated_at": NOW,
    }
    value.update(overrides)
    return value


def journal(**overrides):
    value = {
        "schema_version": 2,
        "target_id": TARGET_ID,
        "target_fingerprint": TARGET_FINGERPRINT,
        "transaction_fingerprint": TRANSACTION_FINGERPRINT,
        "transaction_id": "tx-124",
        "phase": "prepared",
        "request_run_id": 201,
        "request_run_attempt": 1,
        "ci_run_id": 101,
        "ci_run_attempt": 3,
        "target_image": IMAGE,
        "target_source_sha": SOURCE_SHA,
        "target_image_id": None,
        "previous_image": "conan-dev-rollback:tx-124",
        "previous_image_id": IMAGE_ID,
        "rollback_tag": "conan-dev-rollback:tx-124",
        "started_at": NOW,
        "updated_at": NOW,
        "error": None,
    }
    value.update(overrides)
    return value


def write_untrusted(path: Path, payload: bytes, mode: int = 0o600):
    path.write_bytes(payload)
    path.chmod(mode)


def test_state_write는_0600_temp_fsync_replace_directory_fsync_순서다(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    events = []
    original_fsync = os.fsync
    original_replace = os.replace

    def recording_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(("fsync", kind))
        return original_fsync(descriptor)

    def recording_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        events.append(("replace", source, destination, src_dir_fd, dst_dir_fd))
        return original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(deployment_state.os, "fsync", recording_fsync)
    monkeypatch.setattr(deployment_state.os, "replace", recording_replace)

    deployment_state.write_state(path, state(), TARGET_ID)

    assert events[0] == ("fsync", "file")
    assert events[1][0] == "replace"
    assert events[2] == ("fsync", "directory")
    assert events[1][3] == events[1][4]
    assert events[1][1].startswith(".deployment-state-")
    assert events[1][2] == "state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert deployment_state.load_state(path, TARGET_ID) == state()


def test_existing_state도_새_inode로_원자교체한다(tmp_path):
    path = tmp_path / "state.json"
    deployment_state.write_state(path, state())
    old_inode = path.stat().st_ino
    updated = state(updated_at="2026-09-13T01:03:00+00:00")

    deployment_state.write_state(path, updated)

    assert path.stat().st_ino != old_inode
    assert deployment_state.load_state(path, TARGET_ID) == updated


def test_atomic_write_bytes도_image_env를_0600으로_원자교체한다(tmp_path):
    path = tmp_path / "image.env"
    first = f"CONAN_IMAGE={IMAGE}\n".encode()
    deployment_state.atomic_write_bytes(path, first)
    old_inode = path.stat().st_ino

    updated = f"CONAN_IMAGE={IMAGE.replace('b', 'd')}\n".encode()
    deployment_state.atomic_write_bytes(path, updated)

    assert path.read_bytes() == updated
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(deployment_state.DeploymentStateError, match="bytes"):
        deployment_state.atomic_write_bytes(path, bytearray(updated))


def test_replace가_실패하면_이전_state를_보존하고_temp를_정리한다(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = state()
    deployment_state.write_state(path, original)

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(deployment_state.os, "replace", fail_replace)
    with pytest.raises(deployment_state.DeploymentStateError, match="원자적으로"):
        deployment_state.write_state(
            path,
            state(updated_at="2026-09-13T01:04:00+00:00"),
        )

    assert deployment_state.load_state(path, TARGET_ID) == original
    assert list(tmp_path.glob(".deployment-state-*.tmp")) == []


def test_state와_journal은_파일이_없을때만_none이다(tmp_path):
    assert deployment_state.load_state(tmp_path / "state.json", TARGET_ID) is None
    assert deployment_state.load_journal(tmp_path / "journal.json", TARGET_ID) is None


@pytest.mark.parametrize("payload", [
    b"{not-json",
    b"\xff\xfe",
    b"[]",
    b'{"schema_version":2,"schema_version":2}',
    b'{"outer":{"run_id":1,"run_id":2}}',
])
def test_corrupt_duplicate_nonobject_JSON을_거부한다(tmp_path, payload):
    path = tmp_path / "state.json"
    write_untrusted(path, payload)

    with pytest.raises(deployment_state.DeploymentStateError):
        deployment_state.load_state(path, TARGET_ID)


def test_크기제한을_read와_write에_모두_적용한다(tmp_path):
    path = tmp_path / "state.json"
    write_untrusted(path, b"x" * (deployment_state.MAX_JSON_BYTES + 1))
    with pytest.raises(deployment_state.DeploymentStateError, match="최대 크기"):
        deployment_state.load_state(path, TARGET_ID)

    too_large = journal(error="x" * deployment_state.MAX_JSON_BYTES)
    with pytest.raises(deployment_state.DeploymentStateError):
        deployment_state.write_journal(tmp_path / "journal.json", too_large)


def test_기존파일의_권한이_0600이_아니면_read와_write를_거부한다(tmp_path):
    path = tmp_path / "state.json"
    write_untrusted(path, json.dumps(state()).encode(), mode=0o640)

    with pytest.raises(deployment_state.DeploymentStateError, match="0600"):
        deployment_state.load_state(path, TARGET_ID)
    with pytest.raises(deployment_state.DeploymentStateError, match="0600"):
        deployment_state.write_state(path, state())


def test_symlink_state를_읽거나_덮어쓰지_않는다(tmp_path):
    real = tmp_path / "real.json"
    write_untrusted(real, json.dumps(state()).encode())
    link = tmp_path / "state.json"
    link.symlink_to(real)

    with pytest.raises(deployment_state.DeploymentStateError):
        deployment_state.load_state(link, TARGET_ID)
    with pytest.raises(deployment_state.DeploymentStateError, match="regular file"):
        deployment_state.write_state(link, state())
    assert link.is_symlink()


def test_hardlink_state를_읽거나_덮어쓰지_않는다(tmp_path):
    real = tmp_path / "real.json"
    write_untrusted(real, json.dumps(state()).encode())
    link = tmp_path / "state.json"
    os.link(real, link)

    with pytest.raises(deployment_state.DeploymentStateError, match="hard link"):
        deployment_state.load_state(link, TARGET_ID)
    with pytest.raises(deployment_state.DeploymentStateError, match="hard link"):
        deployment_state.write_state(link, state())
    assert real.read_bytes() == link.read_bytes()


def test_symlink_parent를_거부한다(tmp_path):
    real_parent = tmp_path / "real-state"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-state"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(deployment_state.DeploymentStateError, match="디렉터리"):
        deployment_state.load_state(linked_parent / "state.json", TARGET_ID)
    with pytest.raises(deployment_state.DeploymentStateError, match="디렉터리"):
        deployment_state.write_state(linked_parent / "state.json", state())


def test_group_or_world_writable_parent를_거부한다(tmp_path):
    insecure_parent = tmp_path / "insecure"
    insecure_parent.mkdir(mode=0o700)
    insecure_parent.chmod(0o770)
    try:
        with pytest.raises(deployment_state.DeploymentStateError, match="group/world writable"):
            deployment_state.write_state(insecure_parent / "state.json", state())
    finally:
        insecure_parent.chmod(0o700)


@pytest.mark.parametrize(("field", "bad"), [
    ("schema_version", True),
    ("schema_version", 1),
    ("target_id", "../other-target"),
    ("highest_request", {"run_id": 1, "run_attempt": 1, "extra": 1}),
    ("highest_ci", {"run_id": True, "run_attempt": 1}),
    ("highest_ci", {"run_id": 1, "run_attempt": 0}),
    ("last_outcome", "failed"),
    ("updated_at", "2026-09-13 01:02:03"),
])
def test_state의_schema와_type을_exact_validate한다(field, bad):
    with pytest.raises(deployment_state.DeploymentStateError):
        deployment_state.validate_state(state(**{field: bad}), TARGET_ID)


def test_state의_unknown_missing_nested_active_field를_거부한다():
    unknown = state(extra=True)
    with pytest.raises(deployment_state.DeploymentStateError, match="미지원=extra"):
        deployment_state.validate_state(unknown)

    missing = state()
    del missing["updated_at"]
    with pytest.raises(deployment_state.DeploymentStateError, match="누락=updated_at"):
        deployment_state.validate_state(missing)

    nested = state()
    nested["active"]["command"] = "docker system prune"
    with pytest.raises(deployment_state.DeploymentStateError, match="미지원=command"):
        deployment_state.validate_state(nested)


def test_state와_journal은_현재_controller_target_identity와_다르면_거부한다(tmp_path):
    state_path = tmp_path / "state.json"
    journal_path = tmp_path / "journal.json"
    deployment_state.write_state(state_path, state(), TARGET_ID, TARGET_FINGERPRINT)
    deployment_state.write_journal(
        journal_path,
        journal(),
        TARGET_ID,
        TARGET_FINGERPRINT,
        TRANSACTION_FINGERPRINT,
    )

    changed = "e" * 64
    with pytest.raises(deployment_state.DeploymentStateError, match="controller target"):
        deployment_state.load_state(state_path, TARGET_ID, changed)
    with pytest.raises(deployment_state.DeploymentStateError, match="controller target"):
        deployment_state.load_journal(journal_path, TARGET_ID, changed)


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (state, "target_fingerprint"),
        (journal, "target_fingerprint"),
        (journal, "transaction_fingerprint"),
    ],
)
def test_controller_계약지문은_exact_schema의_소문자_sha256이다(factory, field):
    validator = deployment_state.validate_state if factory is state else deployment_state.validate_journal
    missing = factory()
    del missing[field]
    with pytest.raises(deployment_state.DeploymentStateError, match=field):
        validator(missing)
    with pytest.raises(deployment_state.DeploymentStateError, match="SHA-256"):
        validator(factory(**{field: "F" * 64}))


def test_journal_transaction_계약도_선택적으로_exact_match를_요구한다(tmp_path):
    path = tmp_path / "journal.json"
    deployment_state.write_journal(path, journal())

    with pytest.raises(deployment_state.DeploymentStateError, match="transaction 계약"):
        deployment_state.load_journal(
            path, TARGET_ID, TARGET_FINGERPRINT, "d" * 64
        )


def test_state는_active_null과_exact_active를_구분한다():
    assert deployment_state.validate_state(state(active=None))["active"] is None
    bad = state()
    del bad["active"]["image_id"]
    with pytest.raises(deployment_state.DeploymentStateError, match="누락=image_id"):
        deployment_state.validate_state(bad)


@pytest.mark.parametrize("phase", sorted(deployment_state.JOURNAL_PHASES))
def test_journal은_명시한_phase만_허용한다(phase, tmp_path):
    value = journal(phase=phase)
    deployment_state.write_journal(tmp_path / f"{phase}.json", value)


@pytest.mark.parametrize("phase", ["authorized", "fenced", "failed", "COMMITTED", ""])
def test_journal의_unknown_phase를_거부한다(phase):
    with pytest.raises(deployment_state.DeploymentStateError, match="phase"):
        deployment_state.validate_journal(journal(phase=phase))


@pytest.mark.parametrize(("field", "bad"), [
    ("request_run_id", True),
    ("request_run_id", "201"),
    ("request_run_attempt", 0),
    ("ci_run_id", -1),
    ("ci_run_attempt", 1.0),
])
def test_journal_identity는_양의_integer만_허용한다(field, bad):
    with pytest.raises(deployment_state.DeploymentStateError, match=field):
        deployment_state.validate_journal(journal(**{field: bad}))


def test_journal의_exact_schema_target_timestamp_error를_검사한다():
    unknown = journal(command=["docker", "compose", "down"])
    with pytest.raises(deployment_state.DeploymentStateError, match="미지원=command"):
        deployment_state.validate_journal(unknown)

    missing = journal()
    del missing["rollback_tag"]
    with pytest.raises(deployment_state.DeploymentStateError, match="누락=rollback_tag"):
        deployment_state.validate_journal(missing)

    with pytest.raises(deployment_state.DeploymentStateError, match="배포 대상"):
        deployment_state.validate_journal(journal(), "other-target")
    with pytest.raises(deployment_state.DeploymentStateError, match="빠를"):
        deployment_state.validate_journal(
            journal(updated_at="2026-09-13T01:02:02+00:00")
        )
    with pytest.raises(deployment_state.DeploymentStateError, match="error"):
        deployment_state.validate_journal(journal(error={"secret": "must-not-be-recorded"}))


def test_request와_ci_tuple은_state_request_journal에서_같은순서로비교한다():
    current = state()
    request = {
        "request_run_id": 201,
        "request_run_attempt": 1,
        "release": {"ci_run_id": 101, "ci_run_attempt": 3},
    }
    transaction = journal()

    assert deployment_state.request_tuple(current) == (200, 2)
    assert deployment_state.request_tuple(request) == (201, 1)
    assert deployment_state.request_tuple(transaction) == (201, 1)
    assert deployment_state.request_tuple(request) > deployment_state.request_tuple(current)
    assert deployment_state.ci_tuple(current) == (100, 1)
    assert deployment_state.ci_tuple(request) == (101, 3)
    assert deployment_state.ci_tuple(transaction) == (101, 3)
    assert deployment_state.ci_tuple({"run_id": 101, "run_attempt": 4}) == (101, 4)


def test_journal_round_trip은_optional_image_fields와_error를_보존한다(tmp_path):
    path = tmp_path / "journal.json"
    value = journal(
        phase="rolling_back",
        target_image_id=IMAGE_ID,
        previous_image=None,
        previous_image_id=None,
        rollback_tag=None,
        updated_at="2026-09-13T01:03:00Z",
        error="새 이미지 readiness 검증 실패",
    )

    deployment_state.write_journal(path, value, TARGET_ID)

    assert deployment_state.load_journal(path, TARGET_ID) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
