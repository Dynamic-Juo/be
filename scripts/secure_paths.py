"""Host-side trusted path primitives anchored at the filesystem root.

The deployment controller re-opens binaries, Compose inputs, state, and its
Docker socket by pathname.  A check of only the immediate parent therefore is
not enough: an earlier symlink or writable directory could redirect the same
string after validation.  These helpers walk every directory component with
``openat`` + ``O_NOFOLLOW`` and apply one shared ownership policy.

The caller's effective uid and root are trusted owners.  Group/world-writable
directories are rejected, except for an intermediate root-owned sticky
directory such as canonical ``/private/tmp`` or Linux ``/tmp``.  Such a shared
directory is never accepted as the leaf parent; a private/non-writable child is
required below it.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Iterator


class SecurePathError(RuntimeError):
    """A path cannot be used as part of the host controller trust boundary."""


_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FIRST_ENTRY = 0
_ACL_NEXT_ENTRY = -1
_ACL_EXTENDED_ALLOW = 1
_ACL_EXTENDED_DENY = 2


def _darwin_acl_library() -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL(None, use_errno=True)
        library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        library.acl_get_fd_np.restype = ctypes.c_void_p
        library.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
        library.acl_get_link_np.restype = ctypes.c_void_p
        library.acl_valid.argtypes = [ctypes.c_void_p]
        library.acl_valid.restype = ctypes.c_int
        library.acl_get_entry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.acl_get_entry.restype = ctypes.c_int
        library.acl_get_tag_type.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        library.acl_get_tag_type.restype = ctypes.c_int
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise SecurePathError("macOS extended ACL 검사 API를 초기화할 수 없다") from exc
    return library


def _validate_darwin_acl(acl: int | None, *, missing_errno: int) -> None:
    """Reject authority-granting Darwin ACEs while permitting deny-only ACEs.

    macOS user home directories commonly carry a system-created deny-delete
    ACE.  A deny entry cannot expand authority, so it is safe to retain;
    every allow or unknown entry is rejected.
    """

    if sys.platform != "darwin":
        return
    if not acl:
        if missing_errno == errno.ENOENT:
            return
        raise SecurePathError("macOS extended ACL을 읽을 수 없다")
    library = _darwin_acl_library()
    assert library is not None
    try:
        if library.acl_valid(acl) != 0:
            raise SecurePathError("macOS extended ACL이 유효하지 않다")
        entry_id = _ACL_FIRST_ENTRY
        while True:
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = library.acl_get_entry(acl, entry_id, ctypes.byref(entry))
            if result != 0:
                if ctypes.get_errno() == errno.EINVAL:
                    break
                raise SecurePathError("macOS extended ACL entry를 읽을 수 없다")
            tag = ctypes.c_int()
            if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                raise SecurePathError("macOS extended ACL tag를 읽을 수 없다")
            if tag.value == _ACL_EXTENDED_ALLOW:
                raise SecurePathError("신뢰 경로에 권한을 부여하는 macOS ACL이 있다")
            if tag.value != _ACL_EXTENDED_DENY:
                raise SecurePathError("신뢰 경로에 알 수 없는 macOS ACL entry가 있다")
            entry_id = _ACL_NEXT_ENTRY
    finally:
        if library.acl_free(acl) != 0 and sys.exc_info()[0] is None:
            raise SecurePathError("macOS extended ACL 메모리를 안전하게 해제하지 못했다")


def _validate_darwin_fd_acl(descriptor: int) -> None:
    if sys.platform != "darwin":
        return
    library = _darwin_acl_library()
    assert library is not None
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(descriptor, _ACL_TYPE_EXTENDED)
    _validate_darwin_acl(acl, missing_errno=ctypes.get_errno())


def _validate_darwin_link_acl(path: Path) -> None:
    if sys.platform != "darwin":
        return
    library = _darwin_acl_library()
    assert library is not None
    ctypes.set_errno(0)
    acl = library.acl_get_link_np(os.fsencode(path), _ACL_TYPE_EXTENDED)
    _validate_darwin_acl(acl, missing_errno=ctypes.get_errno())


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    raw = os.fspath(path)
    if type(raw) is not str:
        raise SecurePathError("신뢰 경로는 canonical 절대 경로 문자열이어야 한다")
    value = Path(raw)
    if (
        not value.is_absolute()
        or value.anchor != "/"
        or ".." in value.parts
        or str(value) != raw
    ):
        raise SecurePathError("신뢰 경로는 상위 이동 없는 canonical 절대 경로여야 한다")
    return value


def canonical_absolute_path(path: str | os.PathLike[str]) -> Path:
    """Validate and return the one accepted textual form of an absolute path."""

    return _absolute_path(path)


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or not cloexec:
        raise SecurePathError("운영체제가 안전한 directory descriptor walk를 지원하지 않는다")
    return os.O_RDONLY | nofollow | directory | cloexec


def _allowed_owner(owner: int) -> bool:
    return owner in {0, os.geteuid()}


def _validate_directory(
    metadata: os.stat_result,
    *,
    leaf: bool,
    private: bool,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    shared_sticky_root = (
        not leaf
        and metadata.st_uid == 0
        and bool(metadata.st_mode & stat.S_ISVTX)
        and bool(mode & 0o022)
    )
    if not stat.S_ISDIR(metadata.st_mode):
        raise SecurePathError("신뢰 경로 component가 디렉터리가 아니다")
    if not _allowed_owner(metadata.st_uid):
        raise SecurePathError("신뢰 경로 디렉터리 소유자가 root 또는 실행 계정이 아니다")
    if mode & 0o022 and not shared_sticky_root:
        raise SecurePathError("신뢰 경로에 group/world writable 디렉터리가 있다")
    if leaf and private and mode & 0o077:
        raise SecurePathError("신뢰 경로의 최종 디렉터리는 owner-only여야 한다")


def open_trusted_directory(
    path: str | os.PathLike[str],
    *,
    private: bool = False,
) -> int:
    """Open and return a verified directory fd; the caller must close it."""

    value = _absolute_path(path)
    components = value.parts[1:]
    flags = _directory_flags()
    try:
        current_fd = os.open("/", flags)
    except OSError as exc:
        raise SecurePathError("filesystem root를 안전하게 열 수 없다") from exc
    try:
        _validate_directory(os.fstat(current_fd), leaf=not components, private=private)
        _validate_darwin_fd_acl(current_fd)
        for index, component in enumerate(components):
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise SecurePathError(
                    f"신뢰 경로 component를 no-follow로 열 수 없다: {value}"
                ) from exc
            try:
                _validate_directory(
                    os.fstat(next_fd),
                    leaf=index == len(components) - 1,
                    private=private,
                )
                _validate_darwin_fd_acl(next_fd)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def open_trusted_parent(
    path: str | os.PathLike[str],
    *,
    private: bool = False,
) -> tuple[int, str]:
    """Return a verified parent fd and the single leaf name."""

    value = _absolute_path(path)
    if value == Path("/") or not value.name:
        raise SecurePathError("신뢰 파일 경로에는 leaf 이름이 필요하다")
    return open_trusted_directory(value.parent, private=private), value.name


def _validate_regular_file(metadata: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecurePathError("신뢰 파일은 단일 hard link의 regular file이어야 한다")
    if not _allowed_owner(metadata.st_uid):
        raise SecurePathError("신뢰 파일 소유자가 root 또는 실행 계정이 아니다")
    forbidden = 0o077 if private else 0o022
    if stat.S_IMODE(metadata.st_mode) & forbidden:
        requirement = "owner-only" if private else "group/world non-writable"
        raise SecurePathError(f"신뢰 파일 권한은 {requirement}여야 한다")


def _open_verified_regular_at(
    parent_fd: int,
    name: str,
    path: str | os.PathLike[str],
    before: os.stat_result,
    *,
    private: bool,
) -> int:
    descriptor: int | None = None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        _validate_regular_file(opened, private=private)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise SecurePathError("신뢰 파일이 검사와 open 사이에 바뀌었다")
        _validate_darwin_fd_acl(descriptor)
        result = descriptor
        descriptor = None
        return result
    except OSError as exc:
        raise SecurePathError(f"신뢰 파일을 no-follow로 열 수 없다: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def stat_trusted_regular_file(
    path: str | os.PathLike[str],
    *,
    private: bool = False,
    missing_ok: bool = False,
) -> os.stat_result | None:
    """lstat a regular-file leaf below a fully verified parent chain."""

    parent_fd, name = open_trusted_parent(path, private=private)
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise SecurePathError(f"필수 신뢰 파일이 없다: {path}") from None
        except OSError as exc:
            raise SecurePathError(f"신뢰 파일 정보를 확인할 수 없다: {path}") from exc
        _validate_regular_file(metadata, private=private)
        descriptor = _open_verified_regular_at(
            parent_fd, name, path, metadata, private=private
        )
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _open_trusted_regular_file(
    path: str | os.PathLike[str],
    *,
    private: bool,
) -> int:
    parent_fd, name = open_trusted_parent(path, private=private)
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise SecurePathError(f"신뢰 파일 정보를 확인할 수 없다: {path}") from exc
        _validate_regular_file(before, private=private)
        return _open_verified_regular_at(
            parent_fd, name, path, before, private=private
        )
    finally:
        os.close(parent_fd)


def _read_chunks(descriptor: int, *, max_bytes: int) -> Iterator[bytes]:
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            return
        yield chunk
        remaining -= len(chunk)


def read_trusted_regular_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    private: bool = False,
    require_nonempty: bool = False,
) -> bytes:
    if type(max_bytes) is not int or max_bytes < 1:
        raise SecurePathError("신뢰 파일 크기 상한이 잘못됐다")
    descriptor = _open_trusted_regular_file(path, private=private)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > max_bytes:
            raise SecurePathError("신뢰 파일 크기가 허용 범위 밖이다")
        payload = b"".join(_read_chunks(descriptor, max_bytes=max_bytes))
    finally:
        os.close(descriptor)
    if len(payload) > max_bytes or (require_nonempty and not payload):
        raise SecurePathError("신뢰 파일 크기가 허용 범위 밖이다")
    return payload


def sha256_trusted_regular_file(path: str | os.PathLike[str]) -> str:
    descriptor = _open_trusted_regular_file(path, private=False)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def trusted_path_identity(path: str | os.PathLike[str]) -> tuple[object, ...]:
    """Return a collision identity without following the final component."""

    parent_fd, name = open_trusted_parent(path)
    try:
        parent = os.fstat(parent_fd)
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return ("path", parent.st_dev, parent.st_ino, name)
        except OSError as exc:
            raise SecurePathError(f"신뢰 경로 정보를 확인할 수 없다: {path}") from exc
        return ("inode", metadata.st_dev, metadata.st_ino)
    finally:
        os.close(parent_fd)


def _validate_unix_socket_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_nlink != 1:
        raise SecurePathError("Docker endpoint는 단일 hard link의 unix socket이어야 한다")
    if not _allowed_owner(metadata.st_uid):
        raise SecurePathError("Docker unix socket 소유자가 root 또는 실행 계정이 아니다")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o002:
        raise SecurePathError("Docker unix socket은 world-writable이면 안 된다")
    process_groups = {os.getegid(), *os.getgroups()}
    if mode & 0o020 and metadata.st_gid not in process_groups:
        raise SecurePathError(
            "Docker unix socket의 write group이 실행 계정 권한에 포함되지 않는다"
        )


def stat_trusted_unix_socket(path: str | os.PathLike[str]) -> os.stat_result:
    """Validate a Docker unix socket leaf and the authority of its path."""

    value = _absolute_path(path)
    parent_fd, name = open_trusted_parent(value)
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise SecurePathError(f"Docker unix socket을 확인할 수 없다: {path}") from exc
        _validate_unix_socket_metadata(metadata)
        _validate_darwin_link_acl(value)
        try:
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise SecurePathError(
                f"Docker unix socket ACL 검사 뒤 다시 확인할 수 없다: {path}"
            ) from exc
        _validate_unix_socket_metadata(after)
        if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
            raise SecurePathError("Docker unix socket이 ACL 검사 중 바뀌었다")
        return after
    finally:
        os.close(parent_fd)
