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


# ---------------------------------------------------------------------------
# Canonical data/daily writer authority (charter s3 / handbook H7b)
# ---------------------------------------------------------------------------
#
# data/daily has one writer: the Data Close Pipeline. The lease used to be
# checked only by scripts/run_daily_data_refresh.py; update_daily_prices and
# write_daily accepted any caller (UI button, bootstrap, CLI fallback). The
# lease now registers itself here and the low-level daily writers refuse a
# canonical write without it. Process-global on purpose: worker threads must
# see it; spawned worker processes get an explicit proof (below) instead.

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_LOCK_RELATIVE = Path("data/.locks/data_close_pipeline.lock.json")
DAILY_WRITER_SCOPES = frozenset({"market_refresh", "research_chain"})


class CanonicalDailyWriteRefused(PermissionError):
    """A write to the canonical data/daily without the Pipeline writer lease."""


@dataclass(frozen=True)
class _DailyWriterAuthority:
    root: Path
    scope: str
    assert_active: Callable[[], None]
    proof: dict


_DAILY_AUTHORITY: _DailyWriterAuthority | None = None


def _norm(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def canonical_daily_dir(root: Path | None = None) -> Path:
    return (Path(root or PROJECT_ROOT) / "data" / "daily").resolve()


def is_canonical_daily_path(path: Path) -> bool:
    """True for the real project's data/daily (or anything inside it)."""

    target = _norm(path)
    base = _norm(canonical_daily_dir())
    return target == base or target.startswith(base + os.sep)


def register_daily_writer(root: Path, *, scope: str, assert_active: Callable[[], None], proof: dict) -> None:
    global _DAILY_AUTHORITY
    _DAILY_AUTHORITY = _DailyWriterAuthority(Path(root).resolve(), str(scope), assert_active, dict(proof))


def unregister_daily_writer(assert_active: Callable[[], None] | None = None) -> None:
    global _DAILY_AUTHORITY
    if assert_active is None or (_DAILY_AUTHORITY is not None and _DAILY_AUTHORITY.assert_active == assert_active):
        _DAILY_AUTHORITY = None


def daily_writer_proof() -> dict | None:
    """Serializable proof for spawned workers of the registered lease, or None."""

    return dict(_DAILY_AUTHORITY.proof) if _DAILY_AUTHORITY is not None else None


def assert_canonical_daily_write(path: Path) -> None:
    """Refuse a write into the canonical data/daily without a live lease.

    Writes outside the real project's data/daily (tests, staging, temp dirs)
    are not affected. Inside it, the registered Pipeline lease must still be
    valid at this moment (owner alive, capability unchanged).
    """

    if not is_canonical_daily_path(path):
        return
    authority = _DAILY_AUTHORITY
    if authority is None:
        raise CanonicalDailyWriteRefused("canonical_daily_writer_lease_required")
    if authority.scope not in DAILY_WRITER_SCOPES:
        raise CanonicalDailyWriteRefused(f"canonical_daily_writer_scope_not_allowed:{authority.scope}")
    if _norm(canonical_daily_dir(authority.root)) != _norm(canonical_daily_dir()):
        raise CanonicalDailyWriteRefused("canonical_daily_writer_root_mismatch")
    try:
        authority.assert_active()
    except Exception as exc:  # noqa: BLE001 - any lease failure refuses the write
        raise CanonicalDailyWriteRefused(f"canonical_daily_writer_lease_invalid:{exc}") from exc


def _probe_owner_byte(lock_path: Path) -> bool:
    """True when byte 0 of the pipeline lock is held by another open handle."""

    with open(lock_path, "r+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                acquire_linux_ofd_lock(handle.fileno(), 0)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return True
            raise
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False


def verify_daily_writer_proof(proof: dict) -> None:
    """Re-check a parent's lease from a spawned worker (no lock bytes held here)."""

    import hashlib
    import json

    try:
        root = Path(str(proof["root"])).resolve()
        scope, token = str(proof["scope"]), str(proof["token"])
        expected = {
            "status": scope, "scope": scope, "run_id": str(proof["run_id"]),
            "root": str(root), "target_date": str(proof["target"]),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }
    except (KeyError, TypeError) as exc:
        raise CanonicalDailyWriteRefused("canonical_daily_writer_proof_malformed") from exc
    if not token or scope not in DAILY_WRITER_SCOPES:
        raise CanonicalDailyWriteRefused("canonical_daily_writer_proof_invalid")
    lock_path = root / PIPELINE_LOCK_RELATIVE
    try:
        meta = json.loads(lock_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanonicalDailyWriteRefused("canonical_daily_writer_metadata_unavailable") from exc
    if not isinstance(meta, dict) or any(meta.get(key) != value for key, value in expected.items()):
        raise CanonicalDailyWriteRefused("canonical_daily_writer_capability_invalid")
    try:
        owner_alive = _probe_owner_byte(lock_path)
    except OSError as exc:
        raise CanonicalDailyWriteRefused("canonical_daily_writer_lock_unverifiable") from exc
    if not owner_alive:
        raise CanonicalDailyWriteRefused("canonical_daily_writer_owner_not_running")


class adopt_daily_writer_proof:
    """Context manager for a spawned worker: act under the parent's lease.

    ``None`` adopts nothing (the worker then has no authority, and a canonical
    write is refused). A previously registered authority is restored on exit.
    """

    def __init__(self, proof: dict | None):
        self._proof = proof
        self._previous: _DailyWriterAuthority | None = None

    def __enter__(self) -> "adopt_daily_writer_proof":
        global _DAILY_AUTHORITY
        self._previous = _DAILY_AUTHORITY
        if self._proof:
            proof = dict(self._proof)
            verify_daily_writer_proof(proof)
            _DAILY_AUTHORITY = _DailyWriterAuthority(
                Path(str(proof["root"])).resolve(), str(proof["scope"]),
                lambda: verify_daily_writer_proof(proof), proof,
            )
        return self

    def __exit__(self, *_args: Any) -> None:
        global _DAILY_AUTHORITY
        _DAILY_AUTHORITY = self._previous
