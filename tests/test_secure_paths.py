import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

from scripts import deployment_state, dev_deploy, secure_paths


def _socket_metadata(mode: int, *, gid: int | None = None) -> os.stat_result:
    return os.stat_result((
        stat.S_IFSOCK | mode,
        1,
        1,
        1,
        os.geteuid(),
        os.getegid() if gid is None else gid,
        0,
        0,
        0,
        0,
    ))


def test_file_open은_중간_symlink_component를_거부한다(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    value = real / "value"
    value.write_bytes(b"trusted")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(secure_paths.SecurePathError, match="no-follow"):
        secure_paths.read_trusted_regular_file(alias / "value", max_bytes=64)


def test_file_open은_안전한_leaf아래라도_writable_ancestor를_거부한다(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    private = unsafe / "private"
    private.mkdir(mode=0o700)
    value = private / "value"
    value.write_bytes(b"trusted")
    unsafe.chmod(0o770)
    try:
        with pytest.raises(secure_paths.SecurePathError, match="group/world writable"):
            secure_paths.read_trusted_regular_file(value, max_bytes=64)
    finally:
        unsafe.chmod(0o700)


def test_path_reopen은_ancestor권한이_약해지면_fail_closed한다(tmp_path):
    parent = tmp_path / "trusted"
    parent.mkdir(mode=0o700)
    value = parent / "value"
    value.write_bytes(b"trusted")
    assert secure_paths.read_trusted_regular_file(value, max_bytes=64) == b"trusted"

    parent.chmod(0o777)
    try:
        with pytest.raises(secure_paths.SecurePathError, match="group/world writable"):
            secure_paths.read_trusted_regular_file(value, max_bytes=64)
    finally:
        parent.chmod(0o700)


def test_root_owned_sticky_shared_ancestor는_private_child가있을때만_허용한다():
    shared = Path("/tmp").resolve(strict=True)
    shared_metadata = shared.stat()
    if shared_metadata.st_uid != 0 or not shared_metadata.st_mode & stat.S_ISVTX:
        pytest.skip("root-owned sticky temporary directory가 없는 platform")
    directory = Path(tempfile.mkdtemp(prefix="conan-path-", dir=shared))
    value = directory / "value"
    try:
        value.write_bytes(b"trusted")
        value.chmod(0o600)
        assert secure_paths.read_trusted_regular_file(
            value, max_bytes=64, private=True
        ) == b"trusted"
    finally:
        value.unlink(missing_ok=True)
        directory.rmdir()


def test_root_owned_sticky_shared_directory자체는_leaf_parent로_거부한다():
    shared = Path("/tmp").resolve(strict=True)
    shared_metadata = shared.stat()
    if shared_metadata.st_uid != 0 or not shared_metadata.st_mode & stat.S_ISVTX:
        pytest.skip("root-owned sticky temporary directory가 없는 platform")
    descriptor, raw_path = tempfile.mkstemp(prefix="conan-path-leaf-", dir=shared)
    os.close(descriptor)
    value = Path(raw_path)
    try:
        with pytest.raises(secure_paths.SecurePathError, match="group/world writable"):
            secure_paths.read_trusted_regular_file(value, max_bytes=64)
    finally:
        value.unlink(missing_ok=True)


def test_private_directory는_group_read_execute도_거부한다(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o750)
    with pytest.raises(secure_paths.SecurePathError, match="owner-only"):
        descriptor = secure_paths.open_trusted_directory(private, private=True)
        os.close(descriptor)


def test_private_file검증은_leaf_parent도_owner_only를_요구한다(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o750)
    value = parent / "secret"
    value.write_bytes(b"trusted")
    value.chmod(0o600)

    with pytest.raises(secure_paths.SecurePathError, match="owner-only"):
        secure_paths.stat_trusted_regular_file(value, private=True)
    with pytest.raises(secure_paths.SecurePathError, match="owner-only"):
        secure_paths.read_trusted_regular_file(value, max_bytes=64, private=True)


@pytest.mark.parametrize("raw", ["/tmp//value", "/tmp/./value", "/tmp/value/"])
def test_secure_path는_문자열_alias를_거부한다(raw):
    with pytest.raises(secure_paths.SecurePathError, match="canonical"):
        secure_paths.stat_trusted_regular_file(raw, missing_ok=True)


def test_state_write도_symlink_ancestor를_따르지_않는다(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "state-alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(deployment_state.DeploymentStateError, match="안전하게"):
        deployment_state.atomic_write_bytes(alias / "image.env", b"CONAN_IMAGE=x\n")
    assert list(real.iterdir()) == []


def test_state_public_API도_raw_path_alias를_거부한다(tmp_path):
    aliased = f"{tmp_path}/./state.json"
    with pytest.raises(deployment_state.DeploymentStateError, match="canonical"):
        deployment_state.load_state(aliased, "conan-development")


def test_config는_parse전에_symlink_ancestor를_거부한다(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    config_path = real / "host.json"
    config_path.write_text("{}")
    alias = tmp_path / "config-alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(dev_deploy.DeploymentError, match="호스트 설정 경로"):
        dev_deploy.read_config(alias / "host.json")


def test_config_secure_snapshot은_duplicate_json_key를_거부한다(tmp_path):
    config_path = tmp_path / "host.json"
    config_path.write_text('{"schema_version":2,"schema_version":2}')
    with pytest.raises(dev_deploy.DeploymentError, match="중복 JSON 키"):
        dev_deploy.read_config(config_path)


def test_docker_endpoint_leaf는_nofollow_socket이어야한다(tmp_path):
    regular = tmp_path / "docker.sock"
    regular.write_bytes(b"not-a-socket")
    with pytest.raises(secure_paths.SecurePathError, match="unix socket"):
        secure_paths.stat_trusted_unix_socket(regular)

    target = tmp_path / "target"
    target.write_bytes(b"not-a-socket")
    symlink = tmp_path / "socket-link"
    symlink.symlink_to(target)
    with pytest.raises(secure_paths.SecurePathError, match="unix socket"):
        secure_paths.stat_trusted_unix_socket(symlink)


def test_docker_socket_world_write와_권한없는_write_group을_거부한다():
    with pytest.raises(secure_paths.SecurePathError, match="world-writable"):
        secure_paths._validate_unix_socket_metadata(_socket_metadata(0o662))

    unknown_gid = max({os.getegid(), *os.getgroups(), 0}) + 1000
    with pytest.raises(secure_paths.SecurePathError, match="write group"):
        secure_paths._validate_unix_socket_metadata(
            _socket_metadata(0o660, gid=unknown_gid)
        )


def test_docker_socket_current_process_write_group은_같은_authority로_허용한다():
    secure_paths._validate_unix_socket_metadata(_socket_metadata(0o660))


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL 전용")
def test_macos_allow_ACL은_mode_bits가_안전해도_거부한다(tmp_path):
    value = tmp_path / "trusted"
    value.write_bytes(b"trusted")
    value.chmod(0o600)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(value)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(secure_paths.SecurePathError, match="권한을 부여하는 macOS ACL"):
            secure_paths.read_trusted_regular_file(value, max_bytes=64, private=True)
    finally:
        subprocess.run(["chmod", "-N", str(value)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL 전용")
def test_macos_deny_only_ACL은_authority를_넓히지_않아_허용한다(tmp_path):
    directory = tmp_path / "trusted"
    directory.mkdir(mode=0o700)
    subprocess.run(
        ["chmod", "+a", "everyone deny delete", str(directory)],
        check=True,
        capture_output=True,
    )
    try:
        descriptor = secure_paths.open_trusted_directory(directory, private=True)
        os.close(descriptor)
    finally:
        subprocess.run(
            ["chmod", "-N", str(directory)], check=True, capture_output=True
        )
