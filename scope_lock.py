"""Advisory locks for trusted processes sharing one knowledge scope."""

from __future__ import annotations

import errno
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import reject_symlink_components

LOCK_MODE = 0o660


@contextmanager
def scope_file_lock(path: Path) -> Iterator[None]:
    """Block on one stable lock inode; a leftover file is never a stale lock."""
    reject_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, LOCK_MODE)
    locked = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("scope lock must be a regular, single-linked file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != LOCK_MODE:
            os.fchmod(fd, LOCK_MODE)
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    time.sleep(0.02)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("scope lock file changed during acquisition")
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
