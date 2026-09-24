"""Atomic, no-clobber publication of a file within one directory."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path

AT_FDCWD = -100  # Linux fcntl.h; absolute paths make the directory FD irrelevant.
RENAME_NOREPLACE = 1  # Linux linux/fs.h.
UNSUPPORTED_ERRNOS = {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV}


class UnsupportedPublicationError(OSError):
    """The host cannot provide atomic no-replace publication."""


def _linux_rename_noreplace(temporary: Path, final: Path) -> None:
    try:
        operation = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise UnsupportedPublicationError(
            errno.ENOSYS, "libc does not expose renameat2", str(final)
        ) from exc
    operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    operation.restype = ctypes.c_int
    outcome = operation(
        AT_FDCWD, os.fsencode(temporary), AT_FDCWD, os.fsencode(final), RENAME_NOREPLACE
    )
    if outcome == 0:
        return
    code = ctypes.get_errno() or errno.EIO
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), str(final))
    if code in UNSUPPORTED_ERRNOS:
        raise UnsupportedPublicationError(
            code, f"atomic no-replace rename is unsupported: {os.strerror(code)}", str(final)
        )
    raise OSError(code, os.strerror(code), str(final))


def rename_noreplace(temporary: Path, final: Path) -> None:
    """Never fall back to replacing rename or link/unlink on Linux."""
    if (not temporary.is_absolute() or not final.is_absolute()
            or ".." in temporary.parts or ".." in final.parts
            or temporary.parent != final.parent):
        raise ValueError("publication paths must be absolute, safe, and in one directory")
    if sys.platform == "linux":
        _linux_rename_noreplace(temporary, final)
    elif os.name == "nt":
        # Windows os.rename fails with FileExistsError if final already exists.
        os.rename(temporary, final)
    else:
        raise UnsupportedPublicationError(
            errno.ENOSYS, "atomic no-replace rename is unavailable on this platform", str(final)
        )
