"""QuantDesk Data Close Pipeline orchestrator.

Consolidates the previously scattered, lock-competing scheduled jobs into ONE
five-stage pipeline with checkpoints, per-stage retry, a single top-level OS
lock, and honest terminal states. Design contract: OPS-DATA-PIPELINE-
CONSOLIDATION-20260901 (see the task brief in the handoff report).

Stages (in order):
  1. universe      - approved-source A-share list refresh (degrades to the
                     last accepted list with a stale/degraded flag)
  2. market_refresh - full-A daily bars, suspension evidence, valuation
                     snapshot, benchmarks, news pool (the fast path; wraps
                     scripts/run_daily_data_refresh.py logic)
  3. research_chain - research pool deep maintenance (wraps the existing
                     daily chain entry with a reuse-upstream mode)
  4. reconcile     - read-only completeness audit with per-symbol gaps
                     (suspension-aware; no second daily writer)
  5. health_alert  - always publishes latest attempt / last successful /
                     per-stage states; never leaves a permanent "running"

Every old CLI stays independently runnable: this module CALLS them, they
never call back into it. Checkpoints bind run_id + target_date + input hash
+ config hash + stage version + output hash; changed inputs refuse resume.

Exit codes: 0 completed/completed_with_warnings/planned_offline,
            1 internal error, 2 invalid contract, 3 retry_pending/deferred,
            4 partial, 5 blocked.
"""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import json
import msvcrt
import os
import re
import runpy
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")

SCHEMA_VERSION = "data_close_pipeline_v1"
LOCK_RELATIVE = Path("data/.locks/data_close_pipeline.lock.json")
MARKET_WRITER_TOKEN_ENV = "QUANTDESK_PIPELINE_MARKET_TOKEN"
CHECKPOINT_DIR_RELATIVE = Path("results/data_close_pipeline/runs")
ATTEMPTS_DIR_RELATIVE = Path("results/data_close_pipeline/attempts")
LATEST_JSON_RELATIVE = Path("results/data_close_pipeline/data_close_pipeline_latest.json")

STAGE_ORDER = ("universe", "market_refresh", "research_chain", "reconcile", "health_alert")

EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_INVALID_CONTRACT = 2
EXIT_RETRY_PENDING = 3
EXIT_PARTIAL = 4
EXIT_BLOCKED = 5

STATUS_TO_EXIT = {
    "completed": EXIT_OK,
    "completed_all_executed": EXIT_OK,
    "completed_with_skips": EXIT_OK,
    "completed_with_warnings": EXIT_OK,
    "planned_offline": EXIT_OK,
    "retry_pending": EXIT_RETRY_PENDING,
    "partial": EXIT_PARTIAL,
    "blocked": EXIT_BLOCKED,
    "failed": EXIT_INTERNAL_ERROR,
}

DEFAULT_STAGE_TIMEOUTS = {
    "universe": 600,
    "market_refresh": 900,
    "research_chain": 5400,
    "reconcile": 2400,
    "health_alert": 300,
}

UNIVERSE_MAX_STALENESS_DAYS = 14


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def validate_run_id(run_id: str) -> str | None:
    """Strict run_id contract (Round-6 P0-B): only [A-Za-z0-9_.-], must start
    with an alphanumeric, no path separators, no traversal, no spaces.
    Returns the normalized id or None when invalid."""

    candidate = str(run_id or "").strip()
    if not candidate:
        return None
    if not _RUN_ID_RE.match(candidate):
        return None
    if ".." in candidate or "/" in candidate or "\\" in candidate:
        return None
    if candidate.endswith((".", " ")):
        return None
    if candidate.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        return None
    return candidate


def _is_reparse_point(path: Path) -> bool:
    """Return True for symlinks and Windows Junction/reparse points."""

    if not path.exists() and not path.is_symlink():
        return False
    try:
        attrs = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return True
    return path.is_symlink() or bool(
        attrs & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    )


def _require_plain_directory(path: Path, *, label: str) -> None:
    if _is_reparse_point(path):
        raise RuntimeError(f"{label}_reparse_point:{path}")
    path.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(path):
        raise RuntimeError(f"{label}_reparse_point:{path}")


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _resolve_target_date() -> str:
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from quantdesk.trading_calendar import resolve_target_complete_trade_date

        resolved = resolve_target_complete_trade_date()
        if isinstance(resolved, dict):
            raw = str(
                resolved.get("effective_target_date")
                or resolved.get("target_complete_trade_date")
                or ""
            ).strip()
        else:
            raw = str(resolved or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    except Exception:  # noqa: BLE001
        pass
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class PipelineBusy(RuntimeError):
    pass


class _LockContended(OSError):
    """An OS lock conflict, distinct from a failed file open."""


class _OSLockHandle:
    """Real OS-level file lock (msvcrt.locking) held for the whole critical
    section. The JSON metadata lives in a SEPARATE file. On acquire failure
    the half-open handle is closed BEFORE raising (Round-2 P2 fix: no fd
    leaks on lock contention)."""

    def __init__(self, path: Path, *, offset: int = 0, existing: bool = False):
        self._path = path
        self._fh = None
        self._offset = offset
        self._existing = existing

    def acquire(self) -> None:
        self._fh = open(self._path, "r+b" if self._existing else "a+b")
        self._fh.seek(self._offset)
        try:
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # contention: close OUR half-open handle before propagating
            try:
                self._fh.close()
            finally:
                self._fh = None
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise _LockContended(exc.errno, "pipeline_lock_contended") from exc
            raise

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(self._offset)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()
            self._fh = None


def _write_lock_metadata(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def acquire_pipeline_lock(root: Path, *, run_id: str) -> tuple[_OSLockHandle, Path]:
    """Acquire the REAL OS lock; JSON marker is advisory metadata in a
    sibling file. A crashed process releases the OS lock automatically, so a
    stale JSON 'running' marker never blocks a new run."""

    lock_path = root / LOCK_RELATIVE
    meta_path = lock_path.with_suffix(".meta.json")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = _OSLockHandle(lock_path)
    writer_probe = _OSLockHandle(lock_path, offset=1)
    try:
        handle.acquire()
        # A timed-out/crashed parent's child retains byte 1 until it exits.
        # Never reset its capability or begin another pipeline while it writes.
        writer_probe.acquire()
    except _LockContended as exc:
        handle.release()
        raise PipelineBusy(
            "data_close_pipeline_lock_held_by_live_process"
        ) from exc
    except BaseException:
        handle.release()
        raise
    try:
        _write_lock_metadata(meta_path, {
            "run_id": run_id, "pid": os.getpid(), "status": "running",
            "started_at": _now_iso(),
        })
    except BaseException:
        handle.release()
        raise
    finally:
        writer_probe.release()
    return handle, lock_path


def release_pipeline_lock(handle: _OSLockHandle, lock_path: Path, *, run_id: str) -> None:
    meta_path = lock_path.with_suffix(".meta.json")
    try:
        _write_lock_metadata(meta_path, {
            "run_id": run_id, "pid": os.getpid(), "status": "released",
            "released_at": _now_iso(),
        })
    except OSError:
        pass
    handle.release()


class MarketWriterUnauthorized(RuntimeError):
    """The refresh entrypoint is not the current Pipeline-owned writer."""


def _grant_market_writer(
    root: Path, *, run_id: str, target: str, owner: _OSLockHandle | None,
    scope: str = "market_refresh",
) -> dict[str, str]:
    lock_path = (root / LOCK_RELATIVE).resolve()
    if scope not in STAGE_ORDER or owner is None or owner._fh is None or owner._offset != 0 or owner._path.resolve() != lock_path:
        raise MarketWriterUnauthorized("pipeline_writer_required")
    token = secrets.token_hex(32)
    _write_lock_metadata(lock_path.with_suffix(".meta.json"), {
        "run_id": run_id, "pid": os.getpid(), "status": scope,
        "root": str(root.resolve()), "target_date": target,
        "scope": scope, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    })
    return {MARKET_WRITER_TOKEN_ENV: token}


def _revoke_market_writer(root: Path, run_id: str) -> None:
    _write_lock_metadata((root / LOCK_RELATIVE).with_suffix(".meta.json"), {
        "run_id": run_id, "pid": os.getpid(), "status": "running",
    })


class MarketWriterLease:
    """Hold byte 1 for all child writes; a run-id alone grants no authority.

    The parent reserves bytes 0+1 before resetting old credentials. Thus an
    orphan child either keeps new parents out, or sees revoked credentials
    after a new parent has acquired both bytes. No lock file is deleted.
    """

    def __init__(self, root: Path, *, run_id: str, target: str, scope: str = "market_refresh"):
        self.root = root.resolve()
        self.run_id = run_id
        self.target = target
        self.scope = scope
        self._token = os.environ.pop(MARKET_WRITER_TOKEN_ENV, "")
        self._handle = _OSLockHandle(self.root / LOCK_RELATIVE, offset=1, existing=True)

    def __enter__(self) -> "MarketWriterLease":
        if not self._token or validate_run_id(self.run_id) is None or self.scope not in STAGE_ORDER:
            raise MarketWriterUnauthorized("pipeline_writer_required")
        try:
            self._handle.acquire()
            self.assert_active()
        except BaseException:
            self._handle.release()
            raise
        return self

    def assert_active(self) -> None:
        if self._handle._fh is None:
            raise MarketWriterUnauthorized("pipeline_writer_lease_not_held")
        try:
            meta = json.loads((self.root / LOCK_RELATIVE).with_suffix(".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MarketWriterUnauthorized("pipeline_writer_metadata_unavailable") from exc
        expected = {
            "status": self.scope, "scope": self.scope, "run_id": self.run_id,
            "root": str(self.root), "target_date": self.target,
            "token_sha256": hashlib.sha256(self._token.encode()).hexdigest(),
        }
        if not isinstance(meta, dict) or any(meta.get(key) != value for key, value in expected.items()):
            raise MarketWriterUnauthorized("pipeline_writer_capability_invalid")
        # A missing/unreadable lock is not evidence of a living owner.
        probe = _OSLockHandle(self.root / LOCK_RELATIVE, existing=True)
        try:
            probe.acquire()
        except _LockContended:
            pass
        except OSError as exc:
            raise MarketWriterUnauthorized("pipeline_writer_lock_unverifiable") from exc
        else:
            probe.release()
            raise MarketWriterUnauthorized("pipeline_writer_owner_not_running")

    def __exit__(self, *_args: Any) -> None:
        self._handle.release()


# ---------------------------------------------------------------------------
# Stage bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    stage: str
    status: str  # completed | completed_with_warnings | failed | skipped | blocked
    started_at: str
    completed_at: str = ""
    elapsed_seconds: float = 0.0
    attempt: int = 1
    input_manifest: str = ""
    output_manifest: str = ""
    reason_codes: list[str] = field(default_factory=list)
    retryable: bool = False
    next_retry_at: str = ""
    network: bool = False
    canonical_write: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_stage_subprocess(
    root: Path,
    stage: str,
    command: list[str],
    *,
    timeout_seconds: int,
    env_overrides: dict[str, str] | None = None,
    on_timeout: Callable[[], None] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Bound stage runtime, revoke timed-out writers and report kill failures."""

    started = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    child_env = dict(os.environ)
    child_env.pop(MARKET_WRITER_TOKEN_ENV, None)
    child_env.update(env_overrides or {})
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            env=child_env,
        )
    except OSError as exc:
        return "failed", [f"stage_spawn_failed:{stage}:{type(exc).__name__}"], {}
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        elapsed = round(time.monotonic() - started, 1)
    except subprocess.TimeoutExpired:
        reasons = [f"stage_timeout:{stage}:limit={timeout_seconds}s:pid={proc.pid}"]
        if on_timeout is not None:
            try:
                on_timeout()
            except OSError:
                reasons.append("writer_capability_revoke_failed")
        try:
            if os.name == "nt":
                killed = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=30, check=False,
                )
                if killed.returncode != 0:
                    reasons.append(f"stage_tree_kill_failed:{killed.returncode}")
                    proc.kill()
            else:
                proc.kill()
        except (OSError, subprocess.SubprocessError):
            reasons.append("stage_tree_kill_error")
            try:
                proc.kill()
            except OSError:
                reasons.append("stage_process_kill_error")
        try:
            proc.communicate(timeout=15)
        except (OSError, subprocess.SubprocessError):
            reasons.append("stage_exit_unconfirmed")
        orphan_alive = proc.poll() is None
        if orphan_alive:
            reasons.append("stage_process_still_alive")
        return (
            "failed",
            reasons,
            {"timeout": True, "child_pid": proc.pid, "orphan_alive": orphan_alive},
        )
    rc = proc.returncode
    reason: list[str] = []
    if rc != 0:
        reason.append(f"stage_exit_code:{stage}:{rc}")
    status = "completed" if rc == 0 else "failed"
    payload: dict[str, Any] = {"exit_code": rc, "elapsed_seconds": elapsed}
    stdout_doc = _parse_json_stdout(stdout or "")
    if stdout_doc is not None:
        payload["stdout_json"] = stdout_doc
    tail = (stderr or "").strip().splitlines()[-3:]
    if tail:
        payload["stderr_tail"] = tail
    return status, reason, payload


# ---------------------------------------------------------------------------
# Pipeline-owned child entrypoints
# ---------------------------------------------------------------------------


_OWNED_SCRIPTS = {
    "universe": "refresh_universe_baseline.py",
    "market_refresh": "run_daily_data_refresh.py",
    "research_chain": "run_daily_data_chain.py",
    "reconcile": "run_full_a_daily_reconciliation.py",
    "health_alert": "run_data_health_alert_delivery.py",
}
_CHILD_JOB_HANDLE: Any = None


def _protect_child_process_tree() -> None:
    """Own descendants until this child exits, including a universe updater.

    A non-inheritable Job handle with KILL_ON_JOB_CLOSE prevents a killed
    wrapper from releasing byte 1 while its writer grandchildren survive.
    Assignment failure rejects the child before any stage code runs.
    The OS closes the handle at process exit; never close it mid-stage.
    """
    import ctypes
    from ctypes import wintypes

    global _CHILD_JOB_HANDLE
    if _CHILD_JOB_HANDLE is not None:
        return

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
            ("max_working", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", IOCounters),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "pipeline_child_job_create_failed")
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
    configured = api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    if not configured or not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
        error = ctypes.get_last_error()
        api.CloseHandle(job)
        raise OSError(error, "pipeline_child_job_assignment_failed")
    _CHILD_JOB_HANDLE = job


def _owned_stage_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Internal Pipeline child; a run-id alone grants no write access.")
    parser.add_argument("stage", choices=tuple(_OWNED_SCRIPTS))
    parser.add_argument("--pipeline-run-id", required=True)
    parser.add_argument("--target-date", required=True)
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    forwarded = argv[split + 1:]
    script = PROJECT_ROOT / "scripts" / _OWNED_SCRIPTS[args.stage]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    previous_argv = sys.argv
    try:
        _protect_child_process_tree()
        sys.argv = [str(script), *forwarded]
        if args.stage == "market_refresh":
            # This audited writer checks the capability again before each
            # atomic publication and retains its lease through final stdout.
            runpy.run_path(str(script), run_name="__main__")
        else:
            with MarketWriterLease(PROJECT_ROOT, run_id=args.pipeline_run_id, target=args.target_date, scope=args.stage):
                runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
    except (MarketWriterUnauthorized, OSError) as exc:
        print(json.dumps({"status": "blocked", "reason_codes": [str(exc)], "stage": args.stage}))
        return EXIT_BLOCKED
    finally:
        sys.argv = previous_argv
    return EXIT_OK


def _run_owned_subprocess(
    root: Path, stage: str, command: list[str], *, timeout: int,
    pipeline_run_id: str, pipeline_lock: _OSLockHandle | None, target: str,
) -> tuple[str, list[str], dict[str, Any]]:
    try:
        writer_env = _grant_market_writer(
            root, run_id=pipeline_run_id, target=target, owner=pipeline_lock, scope=stage,
        )
    except (MarketWriterUnauthorized, OSError):
        return "blocked", ["pipeline_writer_required"], {}
    wrapped = [
        sys.executable, "-B", str(root / "scripts" / "run_data_close_pipeline.py"),
        "--owned-stage", stage, "--pipeline-run-id", pipeline_run_id, "--target-date", target,
        "--", *command[3:],
    ]
    status, reasons, payload = _run_stage_subprocess(
        root, stage, wrapped, timeout_seconds=timeout, env_overrides=writer_env,
        on_timeout=lambda: _revoke_market_writer(root, pipeline_run_id),
    )
    try:
        _revoke_market_writer(root, pipeline_run_id)
    except OSError:
        status = "failed"
        reasons.append("writer_capability_revoke_failed")
    reported = (payload.get("stdout_json") or {}).get("status")
    if reported in {"failed", "partial", "blocked"} or (
        status == "completed" and reported in {"completed_with_warnings", "planned_offline"}
    ):
        status = str(reported)
    return status, reasons, payload


def _approved_universe_sources(root: Path) -> set[str]:
    spec = root / "config" / "weekly_daily_wide_pool_contract.json"
    try:
        payload = json.loads(spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    raw = (payload.get("rules") or {}).get("approved_market_universe_sources") or []
    return {str(item).strip() for item in raw if str(item).strip()}


def _universe_refresh_reports(art_root: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    if not art_root.is_dir():
        return docs
    for path in sorted(art_root.rglob("universe_baseline_refresh.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def _csv_bound_to_usable_universe_evidence(
    csv_sha: str,
    approved: set[str],
    docs: list[dict[str, Any]],
) -> bool:
    """Bind the on-disk CSV to evidence, not to 'latest attempt accepted'.

    Usable evidence is either an accepted refresh whose after.sha256 matches
    the CSV, or a failed attempt that restored those same approved bytes.
    A same-day rejected report must not hide a restored approved list.
    """

    for doc in docs:
        after = doc.get("after") if isinstance(doc.get("after"), dict) else {}
        after_sha = str(after.get("sha256") or "")
        if after_sha != csv_sha:
            continue
        after_sources = {
            str(item).strip() for item in (after.get("sources") or []) if str(item).strip()
        }
        if not after_sources or not after_sources.issubset(approved):
            continue
        if doc.get("accepted") is True:
            return True
        if doc.get("restored_after_failure") is True:
            return True
    return False


def _universe_staleness_days(root: Path) -> int | None:
    """Age in days of the on-disk APPROVED universe file; None if unusable.

    This run's latest refresh report may be accepted=false (provider flap)
    while the CSV was restored to the last approved bytes. Trust the CSV
    only when:
    - sources are in the approved-source contract (not local cache);
    - updated_at is parseable and retained as a freshness label;
    - sha256 matches accepted or restored-after-failure evidence.
    The 2026-09-01 15:30 lesson still holds: a local-cache fallback is
    never a usable market universe.
    """

    csv_path = root / "data" / "universe" / "a_share_symbols.csv"
    if not csv_path.is_file() or csv_path.stat().st_size < 100:
        return None
    approved = _approved_universe_sources(root)
    if not approved:
        return None
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.refresh_universe_baseline import inspect_universe

    try:
        info = inspect_universe(csv_path)
    except (OSError, UnicodeError, csv.Error, KeyError):
        return None
    sources = {str(item).strip() for item in (info.get("sources") or []) if str(item).strip()}
    if not sources or not sources.issubset(approved):
        return None
    stamps = [str(day)[:10] for day in (info.get("updated_at_dates") or []) if str(day).strip()]
    if not stamps:
        return None
    try:
        newest = max(datetime.strptime(day, "%Y-%m-%d") for day in stamps)
    except ValueError:
        return None
    age = max(0, (datetime.now(SHANGHAI).replace(tzinfo=None) - newest).days)
    csv_sha = str(info.get("sha256") or "")
    if not csv_sha:
        return None
    docs = _universe_refresh_reports(root / "results" / "universe_baseline_refresh")
    if not _csv_bound_to_usable_universe_evidence(csv_sha, approved, docs):
        return None
    return age


def _accepted_universe_proof(root: Path) -> dict[str, Any]:
    """Evidence for the on-disk approved universe used when refresh degrades.

    B2 requires the degradation to name the accepted roster: its path, hash,
    quality (approved sources + newest updated date) and age, plus the reason,
    instead of only a stale/degraded flag.
    """

    csv_path = root / "data" / "universe" / "a_share_symbols.csv"
    relative_path = str(csv_path.relative_to(root)).replace("\\", "/")
    try:
        from scripts.refresh_universe_baseline import inspect_universe

        info = inspect_universe(csv_path)
    except Exception:  # noqa: BLE001 - a proof must degrade, not crash the run
        return {
            "path": relative_path,
            "sha256": "",
            "quality": "unreadable",
            "approved_sources": [],
            "latest_updated_at": "",
            "age_days": None,
        }
    stamps = [str(day)[:10] for day in (info.get("updated_at_dates") or []) if str(day).strip()]
    try:
        newest = max(datetime.strptime(day, "%Y-%m-%d") for day in stamps) if stamps else None
    except ValueError:
        newest = None
    age = None if newest is None else max(0, (datetime.now(SHANGHAI).replace(tzinfo=None) - newest).days)
    sources = {str(item).strip() for item in (info.get("sources") or []) if str(item).strip()}
    accepted_evidence: dict[str, Any] = {}
    approved = _approved_universe_sources(root)
    for path in sorted((root / "results/universe_baseline_refresh").rglob("universe_baseline_refresh.json"), reverse=True):
        try:
            raw = path.read_bytes()
            doc = json.loads(raw)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and _csv_bound_to_usable_universe_evidence(str(info.get("sha256") or ""), approved, [doc]):
            accepted_evidence = {
                "path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(),
                "accepted": doc.get("accepted") is True,
                "restored_after_failure": doc.get("restored_after_failure") is True,
            }
            break
    return {
        "path": relative_path,
        "sha256": str(info.get("sha256") or ""),
        "quality": "approved_source" if sources and sources.issubset(_approved_universe_sources(root)) else "unapproved",
        "approved_sources": sorted(sources),
        "latest_updated_at": newest.isoformat() if newest else "",
        "age_days": age,
        "acceptance_evidence": accepted_evidence,
    }


def _verify_universe_artifact(artifact_dir: Path, payload: dict[str, Any]) -> bool:
    """The universe guard must have accepted the refresh (accepted=true).

    Exit code 0 alone is not enough: the guarded writer reports blocked with
    exit 1 when sources are unapproved (2026-09-01 15:30 case), and exit 0
    with accepted=false must still count as a failure here.
    """

    try:
        candidates = sorted(artifact_dir.rglob("universe_baseline_refresh.json"))
        if not candidates:
            return False
        doc = json.loads(candidates[-1].read_text(encoding="utf-8"))
        return bool(doc.get("accepted"))
    except (OSError, json.JSONDecodeError):
        return False


def stage_universe(
    root: Path, *, network: bool, timeout: int, pipeline_run_id: str = "",
    pipeline_lock: _OSLockHandle | None = None, target: str = "", **_kw: Any
) -> StageResult:
    """Stage 1: guarded universe baseline refresh.

    The old CLI has NO --enable-network flag (its network gating lives in the
    provider layer via the approved-source guard). Passing a non-existent
    flag would exit 2 on every real run - the P1 Codex caught. Correct
    contract: run it as-is; its own guard decides approve/reject.
    """

    started = _now_iso()
    command = [
        sys.executable, "-B", str(root / "scripts" / "refresh_universe_baseline.py"),
        "--json",
    ]
    status, reasons, payload = _run_owned_subprocess(
        root, "universe", command, timeout=timeout, pipeline_run_id=pipeline_run_id,
        pipeline_lock=pipeline_lock, target=target,
    )
    if status == "completed":
        # verify the actual artifact: the guard must have ACCEPTED the refresh
        artifact = root / "results" / "universe_baseline_refresh"
        accepted = _verify_universe_artifact(artifact, payload)
        if not accepted:
            status = "failed"
            reasons = reasons + ["universe_refresh_not_accepted"]
    return StageResult(
        stage="universe", status=status, started_at=started,
        reason_codes=reasons, network=network, canonical_write=True,
        payload=payload,
    )


def stage_market_refresh(
    root: Path,
    *,
    network: bool,
    timeout: int,
    target: str,
    pipeline_run_id: str = "",
    pipeline_lock: _OSLockHandle | None = None,
    **_kw: Any,
) -> StageResult:
    started = _now_iso()
    command = [
        sys.executable, "-B", str(root / "scripts" / "run_daily_data_refresh.py"),
        "--target-date", target, "--json", "true",
    ]
    if pipeline_run_id:
        command.extend(["--pipeline-run-id", pipeline_run_id])
    if network:
        command.append("--enable-network")
    status, reasons, payload = _run_owned_subprocess(
        root, "market_refresh", command, timeout=timeout, pipeline_run_id=pipeline_run_id,
        pipeline_lock=pipeline_lock, target=target,
    )
    doc = payload.get("stdout_json") or {}
    benchmark = doc.get("benchmark_evidence") or (doc.get("families") or {}).get("benchmarks") or {}
    evidence = {key: benchmark.get(key) for key in (
        "status", "target_date", "target_covered", "freshness_status", "coverage", "reason_codes"
    )}
    if evidence["target_date"] != target:
        evidence.update(target_covered=False, freshness_status="target_unverified")
        evidence["reason_codes"] = [*(evidence.get("reason_codes") or []), "benchmark_target_unverified"]
    payload["benchmark_evidence"] = evidence
    if evidence["target_covered"] is not True:
        reasons = [*reasons, *[f"benchmarks:{code}" for code in evidence.get("reason_codes") or ["target_not_covered"]]]
        if status == "completed":
            status = "completed_with_warnings"
    return StageResult(
        stage="market_refresh", status=status, started_at=started,
        reason_codes=reasons, network=network, canonical_write=True,
        payload=payload,
    )


def _maybe_write_upstream_reuse_manifest(
    root: Path,
    *,
    run_id: str,
    target_date: str,
    stages: dict[str, Any],
) -> Path | None:
    """Publish a bounded Stage 1/2 reuse contract after both stages succeed."""

    universe_status = str((stages.get("universe") or {}).get("status") or "")
    market_status = str((stages.get("market_refresh") or {}).get("status") or "")
    if universe_status not in {"completed", "completed_with_warnings"}:
        return None
    if market_status not in {"completed", "completed_with_warnings"}:
        return None
    universe_file = root / "data" / "universe" / "a_share_symbols.csv"
    refresh_file = root / "results" / "data_refresh" / "data_refresh_latest.json"
    if not universe_file.is_file() or not refresh_file.is_file():
        return None
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from quantdesk.data_close_upstream_contract import write_upstream_reuse_manifest

    destination = (
        root / "results" / "data_close_pipeline" / "runs" / run_id / "upstream_reuse.json"
    )
    try:
        return write_upstream_reuse_manifest(
            project_root=root,
            run_id=run_id,
            target_date=target_date,
            universe_status=universe_status,
            market_refresh_status=market_status,
            output_path=destination,
        )
    except (OSError, ValueError):
        return None


def stage_research_chain(
    root: Path,
    *,
    network: bool,
    timeout: int,
    target: str = "",
    trigger: str = "manual",
    reuse_upstream_manifest: str | Path | None = None,
    pipeline_run_id: str = "",
    pipeline_lock: _OSLockHandle | None = None,
    **_kw: Any,
) -> StageResult:
    started = _now_iso()
    command = [
        sys.executable, "-B", str(root / "scripts" / "run_daily_data_chain.py"),
        "--trigger", str(trigger or "manual"), "--json", "true",
        "--skip-analysis-pool-minute", "true",
        "--include-critical-minute", "false",
    ]
    if target:
        command.extend(["--target-date", str(target)])
    if reuse_upstream_manifest:
        command.extend(["--reuse-upstream-manifest", str(reuse_upstream_manifest)])
    if pipeline_run_id:
        command.extend(["--pipeline-run-id", pipeline_run_id])
    if network:
        command.append("--enable-network")
    status, reasons, payload = _run_owned_subprocess(
        root, "research_chain", command, timeout=timeout, pipeline_run_id=pipeline_run_id,
        pipeline_lock=pipeline_lock, target=target,
    )
    return StageResult(
        stage="research_chain", status=status, started_at=started,
        reason_codes=reasons, network=network, canonical_write=True,
        payload=payload,
    )


def stage_reconcile(
    root: Path,
    *,
    network: bool,
    timeout: int,
    target: str,
    pipeline_run_id: str = "",
    pipeline_lock: _OSLockHandle | None = None,
    alert_network: bool = False,
    alert_push: bool = False,
    **_kw: Any,
) -> StageResult:
    started = _now_iso()
    command = [
        sys.executable, "-B",
        str(root / "scripts" / "run_full_a_daily_reconciliation.py"),
        "--target-date", target, "--json", "true",
        "--audit-only",
    ]
    if pipeline_run_id:
        command.extend(["--pipeline-run-id", pipeline_run_id])
    status, reasons, payload = _run_owned_subprocess(
        root, "reconcile", command, timeout=timeout, pipeline_run_id=pipeline_run_id,
        pipeline_lock=pipeline_lock, target=target,
    )
    return StageResult(
        stage="reconcile", status=status, started_at=started,
        reason_codes=reasons, network=False, canonical_write=False,
        payload=payload,
    )


def stage_health_alert(
    root: Path,
    *,
    network: bool,
    timeout: int,
    push: bool = False,
    alert_network: bool = False,
    pipeline_run_id: str = "",
    pipeline_lock: _OSLockHandle | None = None,
    target: str = "",
    **_kw: Any,
) -> StageResult:
    started = _now_iso()
    # Health/alert always runs (even after upstream failures). Alert policy is
    # EXPLICIT: without push the delivery is preview-only and the stage says so
    # (P1 Codex #10: a silent preview must never read as "alerts wired").
    # Data --enable-network does not authorize Server酱; that is alert_network.
    command = [
        sys.executable, "-B",
        str(root / "scripts" / "run_data_health_alert_delivery.py"),
        "--json", "true",
    ]
    if pipeline_run_id:
        command.extend(["--pipeline-run-id", pipeline_run_id])
    if target:
        command.extend(["--target-date", str(target)])
    if alert_network:
        command.append("--enable-network")
    if push:
        command.extend(["--push", "true", "--dry-run", "false"])
    status, reasons, payload = _run_owned_subprocess(
        root, "health_alert", command, timeout=timeout, pipeline_run_id=pipeline_run_id,
        pipeline_lock=pipeline_lock, target=target,
    )
    payload = dict(payload or {})
    payload["alert_policy"] = "push" if push else "preview_only"
    return StageResult(
        stage="health_alert", status=status, started_at=started,
        reason_codes=reasons, network=network, canonical_write=False,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def _stage_version(stage: str) -> str:
    revision = 4 if stage in {"universe", "market_refresh"} else 3 if stage in {"research_chain", "reconcile"} else 2
    return f"{SCHEMA_VERSION}:{stage}:{revision}"


def _input_manifest_for(stage: str, root: Path, target: str) -> str:
    """Hash config and upstream read-only evidence. This stage's writers are outputs.

    Universe CSV is Stage 1 output. Suspension evidence is Stage 2 output.
    Hashing those as inputs made a successful write look like tampering.
    """

    inputs: dict[str, str] = {}
    config_default = root / "config" / "default.yaml"
    if stage == "universe":
        inputs["config_default"] = _file_hash(config_default)
    elif stage == "market_refresh":
        inputs["config_default"] = _file_hash(config_default)
        inputs["universe_csv"] = _file_hash(root / "data" / "universe" / "a_share_symbols.csv")
    elif stage == "research_chain":
        inputs["universe_csv"] = _file_hash(root / "data" / "universe" / "a_share_symbols.csv")
        inputs["refresh_latest"] = _file_hash(
            root / "results" / "data_refresh" / "data_refresh_latest.json"
        )
    elif stage == "reconcile":
        inputs["universe_csv"] = _file_hash(root / "data" / "universe" / "a_share_symbols.csv")
        inputs["suspension_evidence_dir"] = _dir_hash(
            root / "data" / "metadata" / "suspension_evidence"
        )
        inputs["refresh_latest"] = _file_hash(
            root / "results" / "data_refresh" / "data_refresh_latest.json"
        )
    elif stage == "health_alert":
        inputs["refresh_latest"] = _file_hash(
            root / "results" / "data_refresh" / "data_refresh_latest.json"
        )
        inputs["chain_latest"] = _file_hash(
            root / "results" / "data_chain" / "daily_data_chain_latest.json"
        )
        inputs["recon_latest"] = _file_hash(
            root / "results" / "data_chain" / "full_a_daily_reconciliation_latest.json"
        )
    else:
        return "inputs_unbound"
    return _stable_hash({"stage": stage, "stage_version": _stage_version(stage),
                         "target_date": target, "inputs": inputs})


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:32]


def _dir_hash(path: Path) -> str:
    if not path.is_dir():
        return "missing"
    combined = []
    for p in sorted(path.glob("*.json")):
        combined.append(p.name + ":" + _file_hash(p))
    return _stable_hash(combined)


def _output_manifest_for(stage: str, root: Path) -> str:
    """Hash files this stage is allowed to write."""

    outputs: dict[str, str] = {}
    if stage == "universe":
        outputs["universe_csv"] = _file_hash(root / "data" / "universe" / "a_share_symbols.csv")
    elif stage == "market_refresh":
        outputs["refresh_latest"] = _file_hash(
            root / "results" / "data_refresh" / "data_refresh_latest.json"
        )
        for filename in ("hs300.csv", "zz500.csv", "sh000001.csv"):
            outputs[f"benchmark:{filename}"] = _file_hash(root / "data/benchmark" / filename)
        outputs["suspension_evidence_dir"] = _dir_hash(
            root / "data" / "metadata" / "suspension_evidence"
        )
    elif stage == "research_chain":
        outputs["chain_latest"] = _file_hash(
            root / "results" / "data_chain" / "daily_data_chain_latest.json"
        )
    elif stage == "reconcile":
        outputs["recon_latest"] = _file_hash(
            root / "results" / "data_chain" / "full_a_daily_reconciliation_latest.json"
        )
    elif stage == "health_alert":
        outputs["alert_delivery"] = _file_hash(
            root / "results" / "data_health" / "data_health_alert_delivery_latest.json"
        )
    else:
        return "outputs_unbound"
    return _stable_hash({"stage": stage, "outputs": outputs})


def _read_json_artifact(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "malformed"
    if not isinstance(doc, dict):
        return None, "not_object"
    return doc, None


def _parse_json_stdout(text: str) -> dict[str, Any] | None:
    blob = str(text or "")
    start = blob.find("{")
    if start < 0:
        return None
    snippet = blob[start:]
    try:
        doc = json.loads(snippet)
    except json.JSONDecodeError:
        try:
            doc, _end = json.JSONDecoder().raw_decode(snippet)
        except json.JSONDecodeError:
            return None
    return doc if isinstance(doc, dict) else None


def _adapter_ok_statuses(stage: str) -> set[str]:
    ok = {"completed", "completed_with_warnings", "partial", "planned_offline"}
    if stage == "research_chain":
        return ok | {"blocked", "dry_run", "skipped_already_completed"}
    if stage == "reconcile":
        return ok | {"blocked", "deferred_upstream_busy"}
    if stage == "health_alert":
        return ok | {"preview_generated", "sent", "skipped", "skipped_duplicate"}
    if stage == "market_refresh":
        return ok | {"completed_with_failures"}
    return ok


def _validate_this_run_stdout_binding(
    stage: str,
    root: Path,
    *,
    target: str,
    run_id: str,
    result: StageResult,
) -> tuple[bool, list[str]]:
    """Reject leftover same-date JSON: identity comes from THIS subprocess stdout."""

    payload = result.payload or {}
    doc = payload.get("stdout_json")
    if not isinstance(doc, dict):
        return False, [f"{stage}_stdout_unbound"]
    if str(doc.get("pipeline_run_id") or "") != str(run_id):
        return False, [f"{stage}_pipeline_run_id_mismatch"]
    if str(doc.get("target_date") or "")[:10] != str(target)[:10]:
        return False, [f"{stage}_target_mismatch"]
    status = str(doc.get("status") or "")
    if status not in _adapter_ok_statuses(stage) and not doc.get("overall_ok"):
        return False, [f"{stage}_child_status:{status or 'empty'}"]
    rel = str(doc.get("artifact_path") or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or rel.startswith("..") or ".." in Path(rel).parts:
        return False, [f"{stage}_artifact_path_invalid"]
    art = (root / rel).resolve()
    try:
        art.relative_to(root.resolve())
    except ValueError:
        return False, [f"{stage}_artifact_path_escape"]
    if not art.is_file():
        return False, [f"{stage}_artifact_missing"]
    disk_sha = "sha256:" + hashlib.sha256(art.read_bytes()).hexdigest()
    if str(doc.get("artifact_sha256") or "") != disk_sha:
        return False, [f"{stage}_artifact_sha_mismatch"]
    return True, []


def validate_stage_artifact(
    stage: str,
    root: Path,
    *,
    target: str,
    run_id: str,
    result: StageResult,
) -> tuple[bool, list[str]]:
    """Gate a completed stage on THIS run's stdout-bound artifact.

    Injected test runners without a subprocess ``exit_code`` are not forced
    through this gate. Production subprocess stages always include exit_code.
    """

    if str(result.status) not in {"completed", "completed_with_warnings"}:
        return True, []
    payload = result.payload or {}
    if "exit_code" not in payload:
        return True, []

    if stage == "universe":
        art = root / "results" / "universe_baseline_refresh"
        if not _verify_universe_artifact(art, payload):
            return False, ["universe_artifact_missing_or_not_accepted"]
        return True, []
    if stage in {"market_refresh", "research_chain", "reconcile", "health_alert"}:
        return _validate_this_run_stdout_binding(
            stage, root, target=target, run_id=run_id, result=result
        )
    return True, []


def checkpoint_path(root: Path, run_id: str) -> Path:
    normalized = validate_run_id(run_id)
    if normalized is None:
        raise ValueError(f"invalid_run_id:{str(run_id)[:40]}")
    return root / CHECKPOINT_DIR_RELATIVE / normalized / "checkpoint.json"


def load_checkpoint(
    root: Path, run_id: str, *, target: str, config_hash: str,
    require_config_hash: bool = True,
) -> dict[str, Any] | None:
    path = checkpoint_path(root, run_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if payload.get("target_date") != target:
        return None
    if require_config_hash and payload.get("config_hash") != config_hash:
        return None  # inputs changed: refuse mid-run resume
    if str(payload.get("run_id") or "") != run_id:
        return None
    stages = payload.get("stages") or {}
    if not isinstance(stages, dict):
        return None
    from quantdesk.pipeline_terminal import classify_pipeline_status

    skipped = {
        row["stage"] for row in classify_pipeline_status(stages)["skipped_stages"]
    }
    # Round-2 P1 fix: re-hash INPUTS from disk (the Round-1 check only
    # re-hashed outputs - changed inputs were still silently reused).
    for name, meta in stages.items():
        if not isinstance(meta, dict):
            return None
        if name in skipped and str(meta.get("status") or "") not in {
            "completed", "completed_with_warnings", "completed_all_executed",
        }:
            continue
        if str(meta.get("stage_version") or "") != _stage_version(str(name)):
            stages[name] = {"status": "invalid_stage_version"}
            continue
        expected_in = str(meta.get("input_manifest") or "")
        actual_in = _input_manifest_for(str(name), root, target)
        if expected_in in ("", "inputs_unbound"):
            stages[name] = {"status": "unbound_input_refusing_resume"}
            continue
        if actual_in != expected_in:
            stages[name] = {"status": "input_changed_refusing_resume"}
            continue
        expected_out = str(meta.get("output_manifest") or "")
        actual_out = _output_manifest_for(str(name), root)
        if expected_out in ("", "outputs_unbound"):
            stages[name] = {"status": "unbound_output_refusing_resume"}
            continue
        if actual_out != expected_out:
            stages[name] = {"status": "output_tampered_or_missing"}
    return payload


def save_checkpoint(root: Path, run_id: str, payload: dict[str, Any]) -> None:
    path = checkpoint_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_attempt(root: Path, report: dict[str, Any], *, terminal: bool) -> None:
    """Persist only this invocation's progress; older attempts are immutable."""

    attempt_id = str(report.get("attempt_id") or "")
    if not re.fullmatch(r"[0-9a-f]{24}", attempt_id):
        raise ValueError("invalid_pipeline_attempt_id")
    invocation = dict(report.get("invocation") or {})
    invocation["status"] = report.get("status")
    if terminal:
        invocation["finished_at"] = invocation.get("finished_at") or _now_iso()
    report["invocation"] = invocation
    path = root / ATTEMPTS_DIR_RELATIVE / attempt_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _recovery_plan(
    root: Path, run_id: str, *, target: str, config_hash: str,
    selected_stages: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read-only recovery selection under exactly the requested configuration."""

    selected = selected_stages or list(STAGE_ORDER)
    checkpoint = load_checkpoint(root, run_id, target=target, config_hash=config_hash)
    plan: dict[str, Any] = {
        "source_run_id": run_id, "target_date": target, "config_hash": config_hash,
        "read_only": True, "status": "ready" if checkpoint is not None else "blocked",
        "stages_to_run": [], "reused_stages": [], "invalid_stages": [],
        "reason_codes": [] if checkpoint is not None else ["resume_checkpoint_missing_or_target_config_invalid"],
        "notification_policy": "reuse_verified_health_stage; no_new_push_permission",
    }
    for name in selected if checkpoint is not None else []:
        stage = (checkpoint.get("stages") or {}).get(name) or {}
        if stage.get("status") in {"completed", "completed_with_warnings"}:
            plan["reused_stages"].append(name)
        else:
            plan["stages_to_run"].append(name)
            plan["invalid_stages"].append({"stage": name, "reason": stage.get("status") or "missing"})
    # Upstream writes may invalidate later stages. Their hashes are checked
    # again immediately before reuse, rather than trusting this preview.
    plan["downstream_recheck"] = list(STAGE_ORDER)
    return plan, checkpoint


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _checkpoint_data_incomplete(doc: Mapping[str, Any]) -> bool:
    from quantdesk.pipeline_terminal import SUCCESS_STATUSES, classify_pipeline_status

    stages = doc.get("stages") or {}
    if not isinstance(stages, dict):
        return True
    # Historical completion is a claim to validate, never permission to rerun
    # missing/skipped stages and manufacture a new successful result.
    if str(doc.get("status") or "") in {*SUCCESS_STATUSES, "completed", "completed_with_warnings"}:
        return False
    if classify_pipeline_status(stages)["status"] in SUCCESS_STATUSES:
        return False
    for name in ("universe", "market_refresh", "research_chain", "reconcile"):
        meta = stages.get(name)
        status = str(meta.get("status") or "") if isinstance(meta, Mapping) else ""
        if status not in {"completed", "completed_with_warnings"}:
            return True
    return False


def _find_scheduled_checkpoint(
    root: Path, *, target: str, config_hash: str, incomplete: bool,
    require_config_hash: bool = True,
) -> str:
    runs = root / CHECKPOINT_DIR_RELATIVE
    if not runs.is_dir():
        return ""
    best_id = ""
    best_mtime = -1.0
    for path in runs.glob("*/checkpoint.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if str(doc.get("target_date") or "") != target:
            continue
        if require_config_hash and str(doc.get("config_hash") or "") not in {"", config_hash}:
            continue
        if bool(_checkpoint_data_incomplete(doc)) != incomplete:
            continue
        # A previous policy is evidence to reject or explicitly recover, not
        # permission to silently start every stage again (including alerts).
        rid = validate_run_id(str(doc.get("run_id") or path.parent.name) or "")
        if rid is None:
            continue
        mtime = path.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_id = rid
    return best_id


def _checkpoint_has_execution_proof(stages: Mapping[str, Any] | None) -> bool:
    from quantdesk.pipeline_terminal import SUCCESS_STATUSES, classify_pipeline_status

    incoming = dict(stages or {})
    classified = classify_pipeline_status(incoming)
    if classified["status"] not in SUCCESS_STATUSES:
        return False
    skipped = {row["stage"] for row in classified["skipped_stages"]}
    for name in STAGE_ORDER:
        stage = incoming.get(name)
        if not isinstance(stage, Mapping):
            return False
        status = str(stage.get("status") or "")
        if name in skipped:
            continue
        if status not in {"completed", "completed_with_warnings", "completed_all_executed"}:
            return False
        if (
            stage.get("input_manifest") in (None, "", "inputs_unbound")
            or stage.get("output_manifest") in (None, "", "outputs_unbound")
            or stage.get("stage_version") != _stage_version(name)
        ):
            return False
    return True


def run_pipeline(
    root: Path | None = None,
    *,
    network: bool = False,
    run_id: str | None = None,
    target_date: str = "",
    stage_timeouts: dict[str, int] | None = None,
    resume_run_id: str | None = None,
    only_stages: list[str] | None = None,
    stage_runner: dict[str, Callable[..., StageResult]] | None = None,
    dry_run: bool = False,
    alert_push: bool = False,
    alert_network: bool = False,
    trigger: str = "manual",
    mode: str = "normal",
    recovery_plan_only: bool = False,
) -> dict[str, Any]:
    root = Path(root or PROJECT_ROOT)
    target = target_date or _resolve_target_date()
    rid = run_id or f"data-close-{datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S')}"
    attempt_id = secrets.token_hex(12)
    invocation_started = _now_iso()
    timeouts = {**DEFAULT_STAGE_TIMEOUTS, **(stage_timeouts or {})}
    config_hash = _stable_hash(
        {"target_date": target, "network": network, "timeouts": timeouts,
         "schema": SCHEMA_VERSION,
         "stage_versions": {stage: _stage_version(stage) for stage in STAGE_ORDER},
         "minute_processing": "not_requested", "reconciliation": "audit_only"}
    )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "attempt_id": attempt_id,
        "invocation_run_id": rid,
        "invocation": {"requested_run_id": rid, "run_id": rid,
                       "started_at": invocation_started, "finished_at": ""},
        "target_date": target,
        "network": network,
        "config_hash": config_hash,
        "selected_stages": list(only_stages or STAGE_ORDER),
        "stage_timeouts": timeouts,
        "started_at": invocation_started,
        # P1 Round-2 fix: trigger is EXPLICIT. run_id presence says nothing
        # about who invoked the run. CLI default = manual; the scheduled task
        # action passes --trigger scheduled; shadow passes --trigger
        # scheduled --mode shadow.
        "trigger": trigger if trigger in ("manual", "scheduled", "shadow") else "manual",
        "mode": mode if mode in ("normal", "shadow") else "normal",
        "stages": {},
        "status": "planned_offline" if not network else "running",
        "reason_codes": [],
        "alert_policy": "push" if (alert_push and alert_network) else "preview_only",
        "research_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }

    # Round-6 P0-B: run_id validation happens at the SINGLE entry point,
    # BEFORE any branch (normal/shadow/dry-run) can write anywhere. A
    # path-like run_id ("..\\..\\runs\\escaped") previously traversed out of
    # the shadow namespace into normal runs/. An explicitly-passed EMPTY
    # string is also invalid (it is not the same as "not provided": the
    # CLI always passes the attribute, so default sentinel distinguishes).
    if run_id is not None:
        validated = validate_run_id(run_id)
        if validated is None:
            report["status"] = "blocked"
            report["reason_codes"].append(f"invalid_run_id:{run_id[:40]}")
            # blocked before ANY write: no publish, no lock, no namespace touch
            return report
        rid = validated
        report["run_id"] = rid

    if recovery_plan_only:
        validated_resume = validate_run_id(resume_run_id or "")
        if validated_resume is None:
            report.update(status="blocked", reason_codes=["valid_resume_run_id_required"])
            return report
        plan, _ = _recovery_plan(root, validated_resume, target=target,
                                 config_hash=config_hash, selected_stages=only_stages)
        report.update(status="planned_offline" if plan["status"] == "ready" else "blocked",
                      recovery_plan=plan, canonical_write=False)
        return report

    # Round-6 P0-A: SHADOW also routes around the legacy dry-run branch.
    # Shadow IS the safe dry-run (no stages, no network); a shadow run with
    # dry_run=True must never fall into the old dry_run path that writes the
    # NORMAL namespace (runs/ + latest). Shadow takes priority.
    if mode == "shadow":
        report["shadow_contract_version"] = "shadow_evidence_v1"
        report["normal_pipeline_state_touched"] = False
        if dry_run:
            report["reason_codes"].append("dry_run_flag_ignored_in_shadow_mode")
        report["stages"] = {
            name: StageResult(
                stage=name, status="skipped", started_at=_now_iso(),
                reason_codes=["shadow_readonly_skipped"],
                network=False, canonical_write=False,
            ).to_dict()
            for name in STAGE_ORDER
        }
        report["status"] = "completed_with_warnings"
        report["reason_codes"].append(
            "shadow_read_only:no_canonical_writes:no_stage_subprocess"
        )
        report["finished_at"] = _now_iso()
        try:
            _publish_shadow(root, report)
        except RuntimeError as exc:
            # junction/symlink escape: fail closed, write NOTHING anywhere
            report["status"] = "blocked"
            report["reason_codes"].append(f"shadow_publish_refused:{exc}")
        return report

    if dry_run:
        report["status"] = "planned_offline"
        report["reason_codes"].append("dry_run")
        report["stages"] = {
            name: StageResult(
                stage=name, status="skipped", started_at=_now_iso(),
            ).to_dict()
            for name in STAGE_ORDER
        }
        _publish(root, report)
        return report

    if not network:
        # Offline: no stage may run; report an honest planned_offline.
        report["stages"] = {
            name: StageResult(
                stage=name, status="skipped", started_at=_now_iso(),
                reason_codes=["network_not_authorized"],
            ).to_dict()
            for name in STAGE_ORDER
        }
        report["status"] = "planned_offline"
        report["reason_codes"].append("network_not_authorized")
        _publish(root, report)
        return report

    lock_handle: _OSLockHandle | None = None
    # A new invocation never rewrites the checkpoint of a previous run, even
    # when an explicit caller repeats its run_id.
    if checkpoint_path(root, rid).exists() or (root / CHECKPOINT_DIR_RELATIVE / rid / "manifest.json").exists():
        rid = f"{rid[:60]}-{attempt_id[:12]}"
        report["run_id"] = rid
        report["invocation_run_id"] = rid
        report["invocation"]["run_id"] = rid
    try:
        try:
            lock_handle, lock_path = acquire_pipeline_lock(root, run_id=rid)
        except PipelineBusy as exc:
            report["status"] = "blocked"
            report["reason_codes"].append(str(exc))
            _publish(root, report)
            return report

        _write_attempt(root, report, terminal=False)

        # Round-6b: resume_run_id gets the SAME validation as run_id - a
        # path-like resume id could read a forged checkpoint from outside the
        # runs/ namespace and skip stages.
        if not resume_run_id and trigger == "scheduled":
            incomplete_id = _find_scheduled_checkpoint(
                root, target=target, config_hash=config_hash, incomplete=True,
                require_config_hash=False,
            )
            if incomplete_id:
                resume_run_id = incomplete_id
                report["resumed_from_run_id"] = incomplete_id
                report["reason_codes"].append("scheduled_resume_incomplete")
            else:
                complete_id = _find_scheduled_checkpoint(
                    root, target=target, config_hash=config_hash,
                    incomplete=False, require_config_hash=False,
                )
                if complete_id:
                    resumed_done = load_checkpoint(
                        root, complete_id, target=target, config_hash=config_hash,
                        require_config_hash=False,
                    )
                    reused_stages = dict((resumed_done or {}).get("stages") or {})
                    provenance_fields = (
                        "config_hash", "selected_stages", "stage_timeouts", "network",
                        "trigger", "mode", "alert_policy", "started_at", "finished_at",
                    )
                    report["reentry_invocation"] = {
                        "run_id": report["run_id"],
                        **{field: report.get(field) for field in provenance_fields},
                    }
                    report["reentry_invocation"]["finished_at"] = _now_iso()
                    for field in provenance_fields:
                        report[field] = (resumed_done or {}).get(field)
                    report["run_id"] = complete_id
                    for name, stage in list(reused_stages.items()):
                        if isinstance(stage, dict):
                            reused_stages[name] = {**stage, "reused_from_checkpoint": True}
                    has_proof = _checkpoint_has_execution_proof(reused_stages)
                    if has_proof:
                        report["reason_codes"].append("scheduled_already_completed")
                    else:
                        report["reason_codes"].append("scheduled_checkpoint_lacks_execution_proof")
                    report["stages"] = reused_stages
                    report["status"] = _overall_status(report)
                    if not has_proof:
                        report["status"] = "failed"
                    _publish(root, report)
                    return report
        if resume_run_id:
            validated_resume = validate_run_id(resume_run_id)
            if validated_resume is None:
                report["status"] = "blocked"
                report["reason_codes"].append(f"invalid_resume_run_id:{resume_run_id[:40]}")
                _publish(root, report)
                return report
            resume_run_id = validated_resume
        resumed = None
        if resume_run_id:
            plan, resumed = _recovery_plan(root, resume_run_id, target=target,
                                           config_hash=config_hash, selected_stages=only_stages)
            report["recovery_plan"] = plan
            report["resumed_from_run_id"] = resume_run_id
            if resumed is None:
                report.update(status="blocked", reason_codes=plan["reason_codes"])
                _publish(root, report)
                return report
        completed: dict[str, dict[str, Any]] = dict((resumed or {}).get("stages") or {})

        runners = stage_runner or {
            "universe": stage_universe,
            "market_refresh": stage_market_refresh,
            "research_chain": stage_research_chain,
            "reconcile": stage_reconcile,
            "health_alert": stage_health_alert,
        }
        selected = only_stages or list(STAGE_ORDER)
        blocked_reasons: list[str] = []
        writer_still_alive = False

        for name in STAGE_ORDER:
            if name not in selected:
                report["stages"][name] = StageResult(
                    stage=name, status="skipped", started_at=_now_iso(),
                    reason_codes=["stage_not_selected"],
                ).to_dict()
                continue
            prev = completed.get(name)
            if prev and prev.get("status") in {"completed", "completed_with_warnings"}:
                if prev.get("input_manifest") != _input_manifest_for(name, root, target):
                    prev = {"status": "input_changed_refusing_resume"}
                elif prev.get("output_manifest") != _output_manifest_for(name, root):
                    prev = {"status": "output_tampered_or_missing"}
            if prev and prev.get("status") in ("output_tampered_or_missing",
                                               "invalid_stage_version",
                                               "input_changed_refusing_resume"):
                # checkpoint evidence rejected on re-hash: run the stage again
                pass
            elif prev and str(prev.get("input_manifest") or "") == "inputs_unbound":
                # Round-4 fix: unbound stages may NEVER be checkpoint-reused
                # (Codex Round-3 #5: research_chain resumed with zero calls).
                # Unknown inputs = unknown whether they changed = fail-closed.
                pass
            elif prev and prev.get("status") in ("completed", "completed_with_warnings"):
                prev = dict(prev)
                prev["reused_from_checkpoint"] = True
                report["stages"][name] = prev
                continue
            # upstream contract gates: any failed upstream stage blocks the
            # remaining DATA stages; health_alert always runs.
            if (name != "health_alert" and blocked_reasons) or writer_still_alive:
                report["stages"][name] = StageResult(
                    stage=name, status="blocked", started_at=_now_iso(),
                    reason_codes=list(blocked_reasons),
                ).to_dict()
                continue

            runner = runners[name]
            report["current_stage"] = name
            _write_attempt(root, report, terminal=False)
            input_before = _input_manifest_for(name, root, target)
            stage_kwargs: dict[str, Any] = {
                "network": network,
                "timeout": timeouts[name],
                "pipeline_run_id": rid,
                "pipeline_lock": lock_handle,
                "alert_network": alert_network,
                "alert_push": alert_push,
            }
            if name in ("market_refresh", "reconcile"):
                result = runner(root, target=target, **stage_kwargs)
            elif name == "research_chain":
                reuse_path = _maybe_write_upstream_reuse_manifest(
                    root,
                    run_id=rid,
                    target_date=target,
                    stages=report["stages"],
                )
                result = runner(
                    root,
                    target=target,
                    trigger=str(report.get("trigger") or "manual"),
                    reuse_upstream_manifest=str(reuse_path) if reuse_path else None,
                    **stage_kwargs,
                )
            elif name == "health_alert":
                result = runner(root, push=alert_push, target=target, **stage_kwargs)
            else:
                result = runner(root, target=target, **stage_kwargs)
            stage_dict = result.to_dict()
            if name == "market_refresh" and result.payload.get("benchmark_evidence"):
                report["benchmark_evidence"] = dict(result.payload["benchmark_evidence"])
            stage_dict["input_manifest"] = input_before
            if result.status in ("completed", "completed_with_warnings"):
                if _input_manifest_for(name, root, target) != input_before:
                    stage_dict["status"] = "failed"
                    stage_dict["reason_codes"] = list(result.reason_codes) + [
                        "input_mutated_during_stage"
                    ]
                    result = StageResult(**{**stage_dict, "payload": result.payload})
                else:
                    adapter_ok, adapter_reasons = validate_stage_artifact(
                        name, root, target=target, run_id=rid, result=result
                    )
                    if not adapter_ok:
                        stage_dict["status"] = "failed"
                        stage_dict["reason_codes"] = list(result.reason_codes) + adapter_reasons
                        result = StageResult(**{**stage_dict, "payload": result.payload})
                    else:
                        stage_dict["output_manifest"] = _output_manifest_for(name, root)
                        stage_dict["stage_version"] = _stage_version(name)
                        report["stages"][name] = stage_dict
                        completed[name] = dict(stage_dict)
                        save_checkpoint(root, rid, {
                            "schema_version": SCHEMA_VERSION,
                            "run_id": rid,
                            "target_date": target,
                            "config_hash": config_hash,
                            "stages": completed,
                        })
            report["stages"][name] = stage_dict
            if result.payload.get("orphan_alive"):
                writer_still_alive = True
                blocked_reasons.append("previous_stage_writer_still_alive")
            if name == "market_refresh" and stage_dict.get("status") not in {"completed", "completed_with_warnings"}:
                blocked_reasons.append("market_refresh_not_completed")
            if stage_dict.get("status") in {"failed", "blocked", "partial"}:
                if name == "universe" and not writer_still_alive:
                    # A verified old roster remains usable with an age label.
                    input_stable = _input_manifest_for(name, root, target) == input_before
                    stale = _universe_staleness_days(root) if input_stable else None
                    if stale is None:
                        report["stages"][name]["status"] = "blocked"
                        report["stages"][name]["reason_codes"].append(
                            "universe_unverified_or_missing:blocking_full_a"
                        )
                        if not input_stable:
                            report["stages"][name]["reason_codes"].append("input_mutated_during_stage")
                        blocked_reasons.append("universe_unusable")
                    else:
                        report["stages"][name]["status"] = "completed_with_warnings"
                        report["stages"][name]["reason_codes"].append(
                            f"universe_degraded_to_last_accepted:age={stale}d"
                        )
                        # A verified fallback is this stage's accepted output.
                        # Store the same proof fields consumed by load_checkpoint,
                        # not only a nested description that it cannot validate.
                        report["stages"][name]["stage_version"] = _stage_version(name)
                        report["stages"][name]["output_manifest"] = _output_manifest_for(name, root)
                        report["stages"][name]["completed_at"] = _now_iso()
                        report["stages"][name]["degraded_proof"] = {
                            "degraded": True,
                            "degradation_reason": "universe_refresh_failed_used_last_accepted",
                            "accepted_roster": _accepted_universe_proof(root),
                            "stage_version": _stage_version("universe"),
                            "run_id": rid,
                            "target_date": target,
                            "input_output_proof": {
                                "output_manifest": str(
                                    report["stages"][name].get("output_manifest") or ""
                                ),
                                "input_manifest": str(
                                    report["stages"][name].get("input_manifest") or ""
                                ),
                            },
                        }
                        completed[name] = report["stages"][name]
                elif name == "research_chain":
                    blocked_reasons.append("research_chain_failed")
                elif name == "reconcile":
                    blocked_reasons.append("reconcile_failed")

        report["status"] = _overall_status(report)
        report["finished_at"] = _now_iso()
        save_checkpoint(root, rid, {
            **report,
            "schema_version": SCHEMA_VERSION,
            "run_id": rid,
            "target_date": target,
            "config_hash": config_hash,
            "stages": dict(report.get("stages") or {}),
        })
        _publish(root, report)
        return report
    except BaseException as exc:
        report["status"] = "failed"
        report["finished_at"] = _now_iso()
        report["reason_codes"].append(f"pipeline_aborted:{type(exc).__name__}")
        report["invocation"]["aborted_stage"] = report.get("current_stage", "preflight")
        try:
            _publish(root, report)
        except OSError:
            pass  # Keep the original failure; an I/O failure cannot manufacture a receipt.
        raise
    finally:
        if lock_handle is not None:
            release_pipeline_lock(lock_handle, lock_path, run_id=rid)


def _overall_status(report: dict[str, Any]) -> str:
    from quantdesk.pipeline_terminal import classify_pipeline_status

    classified = classify_pipeline_status(report.get("stages") or {})
    skipped = classified.get("skipped_stages") or []
    report["skipped_stages"] = skipped
    failed = classified.get("failed_stages") or []
    report["failed_stages"] = failed
    stages = report.get("stages") if isinstance(report.get("stages"), dict) else {}
    benchmark = ((stages.get("market_refresh") or {}).get("payload") or {}).get("benchmark_evidence")
    if isinstance(benchmark, dict):
        report["benchmark_evidence"] = dict(benchmark)
    reused = [
        name
        for name in classified.get("executed_stages") or []
        if isinstance(stages.get(name), dict)
        and stages[name].get("reused_from_checkpoint")
    ]
    executed = [
        name
        for name in (classified.get("executed_stages") or [])
        if name not in reused
    ]
    report["reused_stages"] = reused
    report["executed_stages"] = executed
    return str(classified.get("status") or "failed")


def _publish_shadow(root: Path, report: dict[str, Any]) -> None:
    """Shadow-only namespace: never touches normal runs/, latest, or
    last_successful (Round-5 deliverable 1, contract 2/3/6).

    Round-6b: junction/symlink defense - the shadow runs dir is resolved to
    its REAL path before writing; a junction named e.g. "junc" inside
    shadow/runs/ pointing at normal runs/ would otherwise redirect the
    manifest into the normal namespace.
    """

    shadow_dir = root / CHECKPOINT_DIR_RELATIVE.parent / "shadow"
    runs_dir = shadow_dir / "runs"
    _require_plain_directory(shadow_dir, label="shadow_root")
    _require_plain_directory(runs_dir, label="shadow_runs")
    rid = validate_run_id(str(report.get("run_id") or ""))
    if rid is None:
        raise RuntimeError("invalid_shadow_run_id")
    run_dir = runs_dir / rid
    # Junction/symlink defense: after mkdir, verify the REAL path of the run
    # dir still lives under the REAL shadow runs dir. A junction named after
    # the run_id would resolve outside; refuse to write in that case.
    real_run = run_dir.resolve()
    real_runs = runs_dir.resolve()
    if not str(real_run).startswith(str(real_runs) + os.sep) and real_run != real_runs:
        raise RuntimeError(
            f"shadow_run_dir_escape:{rid}:resolved={real_run}:expected_under={real_runs}"
        )
    _require_plain_directory(run_dir, label="shadow_run")
    (run_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest = shadow_dir / "data_close_pipeline_shadow_latest.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, latest)
    last_ok = shadow_dir / "data_close_pipeline_shadow_last_successful.json"
    tmp2 = last_ok.with_suffix(".json.tmp")
    tmp2.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp2, last_ok)


def _publish(root: Path, report: dict[str, Any]) -> None:
    out_dir = root / CHECKPOINT_DIR_RELATIVE
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = str(report.get("run_id") or "unknown")
    archive_id = str(report.get("invocation_run_id") or rid)
    run_dir = out_dir / archive_id
    run_dir.mkdir(parents=True, exist_ok=True)
    latest_path = root / LATEST_JSON_RELATIVE
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    # last_successful lives in its OWN pointer file and is NEVER inferred from
    # the latest attempt - two consecutive failures must not erase the last
    # success (the P1 Codex caught).
    success_pointer = latest_path.parent / "data_close_pipeline_last_successful.json"
    from quantdesk.pipeline_terminal import SUCCESS_STATUSES

    payload = dict(report)
    reuse_only = bool(report.get("reused_stages")) and not report.get("executed_stages")
    payload["last_successful_run_id"] = ""
    payload["latest_attempt_is_successful"] = str(payload.get("status")) in SUCCESS_STATUSES
    if payload["latest_attempt_is_successful"] and (not reuse_only or not success_pointer.is_file()):
        payload["last_successful_run_id"] = rid
    elif success_pointer.is_file():
        try:
            prev = json.loads(success_pointer.read_text(encoding="utf-8"))
            payload["last_successful_run_id"] = str(prev.get("run_id") or "")
        except (OSError, json.JSONDecodeError):
            payload["last_successful_run_id"] = ""
    report["last_successful_run_id"] = payload["last_successful_run_id"]
    report["latest_attempt_is_successful"] = payload["latest_attempt_is_successful"]
    _write_attempt(root, report, terminal=True)
    payload = dict(report)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # The original run manifest is a historical receipt, never a latest pointer.
    # Each invocation also has a unique attempts/<id>/manifest.json, including
    # callers that explicitly reuse a run_id and concurrent busy attempts.
    try:
        with (run_dir / "manifest.json").open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    except FileExistsError:
        pass
    if payload["latest_attempt_is_successful"] and (
        not reuse_only or not success_pointer.is_file()
    ):
        tmp = success_pointer.with_suffix(".json.tmp")
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, success_pointer)
    tmp = latest_path.with_suffix(".json.tmp")
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, latest_path)
    report["last_successful_run_id"] = payload["last_successful_run_id"]
    report["latest_attempt_is_successful"] = payload["latest_attempt_is_successful"]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--owned-stage"]:
        return _owned_stage_main(arguments[1:])
    parser = argparse.ArgumentParser(description="QuantDesk Data Close Pipeline (consolidated).")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--network", default="false")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--resume-run-id", default="")
    parser.add_argument("--recovery-plan", action="store_true",
                        help="Read and explain a --resume-run-id checkpoint without writes or provider calls.")
    parser.add_argument("--only-stages", default="", help="Comma-separated stage subset.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--enable-alert-network",
        action="store_true",
        help="Authorize Server-chan network independently of data --enable-network. No SendKey in this CLI.",
    )
    parser.add_argument("--alert-push", action="store_true",
                        help="Explicit Server-chan push authorization for the health stage.")
    parser.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "shadow"],
                        help="Invocation source. Default manual; the scheduled task action passes scheduled.")
    parser.add_argument("--mode", default="normal", choices=["normal", "shadow"],
                        help="normal writes canonical; shadow is read-only evidence mode.")
    parser.add_argument("--json", default="true")
    args = parser.parse_args(argv)

    # Round-6b: the scheduled task launches this script as a bare file
    # (python -B <script>), so sys.path contains the SCRIPTS dir, not the repo
    # root - quantdesk imports fail. Insert the repo root explicitly (the
    # same pattern every other qd script uses).
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from quantdesk.shared import truthy

    network = args.enable_network or truthy(args.network)
    only = [s.strip() for s in args.only_stages.split(",") if s.strip()] or None
    report = run_pipeline(
        PROJECT_ROOT,
        network=network,
        run_id=args.run_id or None,  # "" from CLI = auto-generate (not "invalid")
        target_date=args.target_date,
        resume_run_id=args.resume_run_id or None,
        only_stages=only,
        dry_run=args.dry_run,
        alert_push=args.alert_push,
        alert_network=args.enable_alert_network,
        trigger=args.trigger,
        mode=args.mode,
        recovery_plan_only=args.recovery_plan,
    )
    if truthy(args.json):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return STATUS_TO_EXIT.get(str(report.get("status")), EXIT_INTERNAL_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
