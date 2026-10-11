"""Linux pipeline OFD locks and the explicitly inherited universe writer lease.

Only audited subprocess call sites opt in through ``pass_fds``. This is not
general containment of arbitrary provider subprocesses or a Windows Job clone.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
import sys
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class _Flock(ctypes.Structure):
    _fields_ = [
        ("l_type", ctypes.c_short), ("l_whence", ctypes.c_short),
        ("l_start", ctypes.c_longlong), ("l_len", ctypes.c_longlong),
        ("l_pid", ctypes.c_int),
    ]


def require_linux_ofd_support() -> Any:
    """Reject unverified platforms/ABIs; the actual syscall also checks kernel support."""

    if (
        sys.platform != "linux"
        or platform.machine().lower() not in {"x86_64", "amd64", "aarch64", "arm64"}
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(_Flock) != 32
        or tuple(getattr(_Flock, name).offset for name in ("l_start", "l_len", "l_pid")) != (8, 16, 24)
    ):
        raise OSError(errno.ENOTSUP, "pipeline_linux_ofd_abi_unsupported")
    try:
        import fcntl
    except ImportError as exc:
        raise OSError(errno.ENOTSUP, "pipeline_linux_ofd_unavailable") from exc

    if not all(hasattr(fcntl, name) for name in ("F_OFD_SETLK", "F_OFD_GETLK")):
        raise OSError(errno.ENOTSUP, "pipeline_linux_ofd_unavailable")
    return fcntl


def _request(lock_type: int, offset: int) -> bytes:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise OSError(errno.EINVAL, "pipeline_lock_offset_invalid")
    return bytes(_Flock(lock_type, os.SEEK_SET, offset, 1, 0))


def acquire_linux_ofd_lock(fd: int, offset: int) -> None:
    """Acquire one byte without waiting; callers release by closing their fd only."""

    fcntl = require_linux_ofd_support()
    fcntl.fcntl(fd, fcntl.F_OFD_SETLK, _request(fcntl.F_WRLCK, offset))


def _validate_writer_fd(fd: int, lock_path: Path) -> tuple[int, int]:
    fcntl = require_linux_ofd_support()
    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
        raise OSError(errno.EBADF, "pipeline_writer_fd_invalid")
    current = os.fstat(fd)
    expected = lock_path.stat()
    identity = (current.st_dev, current.st_ino)
    if not stat.S_ISREG(current.st_mode) or identity != (expected.st_dev, expected.st_ino):
        raise OSError(errno.ESTALE, "pipeline_writer_fd_wrong_inode")
    if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
        raise OSError(errno.EBADF, "pipeline_writer_fd_not_read_write")
    request = _request(fcntl.F_WRLCK, 1)
    own = _Flock.from_buffer_copy(fcntl.fcntl(fd, fcntl.F_OFD_GETLK, request))
    owner = _Flock.from_buffer_copy(
        fcntl.fcntl(fd, fcntl.F_OFD_GETLK, _request(fcntl.F_WRLCK, 0))
    )
    # The owning OFD does not conflict with itself. A separate open must see
    # its byte-1 lock, so a byte-0 fd or an unlocked fd cannot be propagated.
    with lock_path.open("r+b") as probe:
        probe_stat = os.fstat(probe.fileno())
        if identity != (probe_stat.st_dev, probe_stat.st_ino):
            raise OSError(errno.ESTALE, "pipeline_writer_probe_wrong_inode")
        other = _Flock.from_buffer_copy(fcntl.fcntl(probe.fileno(), fcntl.F_OFD_GETLK, request))
    if own.l_type != fcntl.F_UNLCK or other.l_type != fcntl.F_WRLCK or other.l_pid != -1:
        raise OSError(errno.EACCES, "pipeline_writer_fd_lease_not_held")
    if owner.l_type != fcntl.F_WRLCK or owner.l_pid != -1:
        raise OSError(errno.EACCES, "pipeline_writer_fd_owner_not_separate")
    return identity


@dataclass(frozen=True)
class _ActiveWriter:
    fd: int
    lock_path: Path
    identity: tuple[int, int]
    assert_active: Callable[[], None]


_ACTIVE_WRITER: ContextVar[_ActiveWriter | None] = ContextVar("pipeline_active_writer", default=None)


def activate_market_writer_lease(
    fd: int, lock_path: Path, assert_active: Callable[[], None],
) -> Token[_ActiveWriter | None]:
    """Publish the validated byte-1 lease to runpy code in this execution context."""

    path = Path(lock_path).resolve()
    assert_active()
    identity = _validate_writer_fd(fd, path)
    return _ACTIVE_WRITER.set(_ActiveWriter(fd, path, identity, assert_active))


def reset_market_writer_lease(token: Token[_ActiveWriter | None]) -> None:
    _ACTIVE_WRITER.reset(token)


def market_writer_subprocess_kwargs(project_root: Path) -> dict[str, Any]:
    """Return explicit byte-1 inheritance only within a valid owned Linux lease."""

    if sys.platform != "linux":
        return {}
    active = _ACTIVE_WRITER.get()
    if active is None:
        return {}
    expected = (Path(project_root).resolve() / "data/.locks/data_close_pipeline.lock.json").resolve()
    if active.lock_path != expected:
        raise OSError(errno.EACCES, "pipeline_writer_subprocess_root_mismatch")
    active.assert_active()
    if _validate_writer_fd(active.fd, expected) != active.identity:
        raise OSError(errno.ESTALE, "pipeline_writer_fd_identity_changed")
    return {"pass_fds": (active.fd,)}
