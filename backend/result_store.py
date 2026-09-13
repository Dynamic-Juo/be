"""Bounded terminal-job snapshot, atomically replaced before drain becomes idle.

Only completed records survive planned deployments. Running jobs are not resumed.
The parent directory must be an application-private persistent volume.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import PurePosixPath
import stat
import uuid

MAX_BYTES = 64 * 1024 * 1024


class ResultStore:
    def __init__(self, path: str):
        parts = PurePosixPath(path).parts
        if not path.startswith('/') or str(PurePosixPath(path)) != path or '..' in parts:
            raise ValueError('result store requires a canonical absolute path')
        self.directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        self.lock = None
        try:
            for index, part in enumerate(parts[1:-1], start=1):
                if index == len(parts) - 2:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=self.directory)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=self.directory)
                os.close(self.directory)
                self.directory = child
                meta = os.fstat(child)
                sticky_root = meta.st_uid == 0 and bool(meta.st_mode & stat.S_ISVTX)
                if meta.st_uid not in {0, os.geteuid()} or (meta.st_mode & 0o022 and not sticky_root):
                    raise ValueError('untrusted result store parent')
            meta = os.fstat(self.directory)
            if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o700:
                raise ValueError('result store directory must be owner-only (0700)')
            self.name = parts[-1]
            self.lock = os.open(self.name + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                                0o600, dir_fd=self.directory)
            self._check_file(self.lock)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check_file(fd: int):
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o600):
            raise ValueError('unsafe result store file')

    def read(self) -> list[dict]:
        try:
            fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, 'rb') as stream:
            self._check_file(stream.fileno())
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('result snapshot exceeds limit')
        data = json.loads(raw)
        if (type(data) is not dict or set(data) != {'version', 'jobs'}
                or data['version'] != 1 or type(data['jobs']) is not list):
            raise ValueError('unsupported result snapshot')
        return data['jobs']

    def write(self, jobs: list[dict]):
        raw = json.dumps({'version': 1, 'jobs': jobs}, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError('result snapshot exceeds limit')
        name = '.' + self.name + '.' + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None
