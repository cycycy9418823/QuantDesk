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
  4. reconcile     - three-way completeness check + gap-only retry (wraps
                     the existing reconciliation module, suspension-aware)
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
import hashlib
import json
import msvcrt
import os
import re
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
CHECKPOINT_DIR_RELATIVE = Path("results/data_close_pipeline/runs")
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


class _OSLockHandle:
    """Real OS-level file lock (msvcrt.locking) held for the whole critical
    section. The JSON metadata lives in a SEPARATE file. On acquire failure
    the half-open handle is closed BEFORE raising (Round-2 P2 fix: no fd
    leaks on lock contention)."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self._path, "a+b")
        self._fh.seek(0)
        try:
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            # contention: close OUR half-open handle before propagating
            try:
                self._fh.close()
            finally:
                self._fh = None
            raise

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()
            self._fh = None


def acquire_pipeline_lock(root: Path, *, run_id: str) -> tuple[_OSLockHandle, Path]:
    """Acquire the REAL OS lock; JSON marker is advisory metadata in a
    sibling file. A crashed process releases the OS lock automatically, so a
    stale JSON 'running' marker never blocks a new run."""

    lock_path = root / LOCK_RELATIVE
    meta_path = lock_path.with_suffix(".meta.json")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = _OSLockHandle(lock_path)
    try:
        handle.acquire()
    except OSError as exc:
        raise PipelineBusy(
            "data_close_pipeline_lock_held_by_live_process"
        ) from exc
    meta_path.write_text(
        json.dumps({"run_id": run_id, "pid": os.getpid(), "status": "running",
                    "started_at": _now_iso()}),
        encoding="utf-8",
    )
    return handle, lock_path


def release_pipeline_lock(handle: _OSLockHandle, lock_path: Path, *, run_id: str) -> None:
    meta_path = lock_path.with_suffix(".meta.json")
    try:
        meta_path.write_text(
            json.dumps({"run_id": run_id, "pid": os.getpid(), "status": "released",
                        "released_at": _now_iso()}),
            encoding="utf-8",
        )
    except OSError:
        pass
    handle.release()


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
) -> tuple[str, list[str], dict[str, Any]]:
    """Run one stage as a subprocess with a hard timeout. No orphans."""

    started = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
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
        )
    except OSError as exc:
        return "failed", [f"stage_spawn_failed:{stage}:{type(exc).__name__}"], {}
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        elapsed = round(time.monotonic() - started, 1)
    except subprocess.TimeoutExpired:
        # Hard timeout: kill the whole process tree, publish a terminal state.
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=30, check=False,
                )
            else:
                proc.kill()
        except Exception:  # noqa: BLE001 - best-effort tree kill
            proc.kill()
        try:
            proc.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        return (
            "failed",
            [f"stage_timeout:{stage}:limit={timeout_seconds}s:pid={proc.pid}"],
            {"timeout": True, "killed_pid": proc.pid},
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
# Stage implementations (call the OLD entry points - they never call back)
# ---------------------------------------------------------------------------


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
    - newest updated_at is within UNIVERSE_MAX_STALENESS_DAYS;
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
    if age > UNIVERSE_MAX_STALENESS_DAYS:
        return None
    csv_sha = str(info.get("sha256") or "")
    if not csv_sha:
        return None
    docs = _universe_refresh_reports(root / "results" / "universe_baseline_refresh")
    if not _csv_bound_to_usable_universe_evidence(csv_sha, approved, docs):
        return None
    return age


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
    root: Path, *, network: bool, timeout: int, pipeline_run_id: str = "", **_kw: Any
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
    status, reasons, payload = _run_stage_subprocess(
        root, "universe", command, timeout_seconds=timeout
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
    status, reasons, payload = _run_stage_subprocess(
        root, "market_refresh", command, timeout_seconds=timeout
    )
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
    **_kw: Any,
) -> StageResult:
    started = _now_iso()
    command = [
        sys.executable, "-B", str(root / "scripts" / "run_daily_data_chain.py"),
        "--trigger", str(trigger or "manual"), "--json", "true",
    ]
    if target:
        command.extend(["--target-date", str(target)])
    if reuse_upstream_manifest:
        command.extend(["--reuse-upstream-manifest", str(reuse_upstream_manifest)])
    if pipeline_run_id:
        command.extend(["--pipeline-run-id", pipeline_run_id])
    if network:
        command.append("--enable-network")
    status, reasons, payload = _run_stage_subprocess(
        root, "research_chain", command, timeout_seconds=timeout
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
    alert_network: bool = False,
    alert_push: bool = False,
    **_kw: Any,
) -> StageResult:
    started = _now_iso()
    command = [
        sys.executable, "-B",
        str(root / "scripts" / "run_full_a_daily_reconciliation.py"),
        "--target-date", target, "--json", "true",
    ]
    if pipeline_run_id:
        command.extend(["--pipeline-run-id", pipeline_run_id])
    if network:
        command.extend(["--enable-network", "--apply"])
    if alert_network:
        command.append("--enable-alert-network")
    if alert_push:
        command.append("--alert-push")
    status, reasons, payload = _run_stage_subprocess(
        root, "reconcile", command, timeout_seconds=timeout
    )
    return StageResult(
        stage="reconcile", status=status, started_at=started,
        reason_codes=reasons, network=network, canonical_write=True,
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
    status, reasons, payload = _run_stage_subprocess(
        root, "health_alert", command, timeout_seconds=timeout
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
    return f"{SCHEMA_VERSION}:{stage}:1"


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
    return _stable_hash({"stage": stage, "target_date": target, "inputs": inputs})


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


def load_checkpoint(root: Path, run_id: str, *, target: str, config_hash: str) -> dict[str, Any] | None:
    path = checkpoint_path(root, run_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if payload.get("target_date") != target or payload.get("config_hash") != config_hash:
        return None  # inputs changed: refuse resume
    if str(payload.get("run_id") or "") != run_id:
        return None
    stages = payload.get("stages") or {}
    if not isinstance(stages, dict):
        return None
    # Round-2 P1 fix: re-hash INPUTS from disk (the Round-1 check only
    # re-hashed outputs - changed inputs were still silently reused).
    for name, meta in stages.items():
        if not isinstance(meta, dict):
            return None
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _checkpoint_data_incomplete(doc: Mapping[str, Any]) -> bool:
    stages = doc.get("stages") or {}
    if not isinstance(stages, dict):
        return True
    for name in ("universe", "market_refresh", "research_chain", "reconcile"):
        status = str((stages.get(name) or {}).get("status") or "")
        if status not in {"completed", "completed_with_warnings"}:
            return True
    return False


def _find_scheduled_checkpoint(
    root: Path, *, target: str, config_hash: str, incomplete: bool
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
        if str(doc.get("config_hash") or "") not in {"", config_hash}:
            continue
        if bool(_checkpoint_data_incomplete(doc)) != incomplete:
            continue
        rid = validate_run_id(str(doc.get("run_id") or path.parent.name) or "")
        if rid is None:
            continue
        mtime = path.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_id = rid
    return best_id


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
) -> dict[str, Any]:
    root = Path(root or PROJECT_ROOT)
    target = target_date or _resolve_target_date()
    rid = run_id or f"data-close-{datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S')}"
    timeouts = {**DEFAULT_STAGE_TIMEOUTS, **(stage_timeouts or {})}
    config_hash = _stable_hash(
        {"target_date": target, "network": network, "timeouts": timeouts,
         "schema": SCHEMA_VERSION}
    )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "target_date": target,
        "network": network,
        "started_at": _now_iso(),
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
    try:
        try:
            lock_handle, lock_path = acquire_pipeline_lock(root, run_id=rid)
        except PipelineBusy as exc:
            report["status"] = "blocked"
            report["reason_codes"].append(str(exc))
            _publish(root, report)
            return report

        # Round-6b: resume_run_id gets the SAME validation as run_id - a
        # path-like resume id could read a forged checkpoint from outside the
        # runs/ namespace and skip stages.
        if not resume_run_id and trigger == "scheduled":
            incomplete_id = _find_scheduled_checkpoint(
                root, target=target, config_hash=config_hash, incomplete=True
            )
            if incomplete_id:
                resume_run_id = incomplete_id
                rid = incomplete_id
                report["run_id"] = rid
                report["reason_codes"].append("scheduled_resume_incomplete")
            else:
                complete_id = _find_scheduled_checkpoint(
                    root, target=target, config_hash=config_hash, incomplete=False
                )
                if complete_id:
                    resumed_done = load_checkpoint(
                        root, complete_id, target=target, config_hash=config_hash
                    )
                    report["run_id"] = complete_id
                    report["status"] = "completed"
                    report["reason_codes"].append("scheduled_already_completed")
                    report["stages"] = dict((resumed_done or {}).get("stages") or {})
                    report["finished_at"] = _now_iso()
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
        resumed = load_checkpoint(root, resume_run_id, target=target, config_hash=config_hash) \
            if resume_run_id else None
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

        for name in STAGE_ORDER:
            if name not in selected:
                report["stages"][name] = StageResult(
                    stage=name, status="skipped", started_at=_now_iso(),
                    reason_codes=["stage_not_selected"],
                ).to_dict()
                continue
            prev = completed.get(name)
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
            if name != "health_alert" and blocked_reasons:
                report["stages"][name] = StageResult(
                    stage=name, status="blocked", started_at=_now_iso(),
                    reason_codes=list(blocked_reasons),
                ).to_dict()
                continue

            runner = runners[name]
            input_before = _input_manifest_for(name, root, target)
            stage_kwargs: dict[str, Any] = {
                "network": network,
                "timeout": timeouts[name],
                "pipeline_run_id": rid,
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
                result = runner(root, **stage_kwargs)
            stage_dict = result.to_dict()
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
                        report["stages"][name] = stage_dict
                        completed[name] = dict(stage_dict)
                        completed[name]["stage_version"] = _stage_version(name)
                        save_checkpoint(root, rid, {
                            "schema_version": SCHEMA_VERSION,
                            "run_id": rid,
                            "target_date": target,
                            "config_hash": config_hash,
                            "stages": completed,
                        })
            report["stages"][name] = stage_dict
            if result.status == "failed" or stage_dict.get("status") == "failed":
                if name in ("universe",):
                    # universe failed: degrade ONLY if the last accepted list
                    # exists, is approved-source, and is within staleness.
                    # Otherwise block formal full-A conclusions (P2 Codex #11).
                    stale = _universe_staleness_days(root)
                    if stale is None or stale > UNIVERSE_MAX_STALENESS_DAYS:
                        report["stages"][name]["status"] = "blocked"
                        report["stages"][name]["reason_codes"].append(
                            "universe_stale_or_missing:blocking_full_a"
                            if stale is None else
                            f"universe_stale:{stale}d>max{UNIVERSE_MAX_STALENESS_DAYS}d"
                        )
                        blocked_reasons.append("universe_unusable")
                    else:
                        report["stages"][name]["status"] = "completed_with_warnings"
                        report["stages"][name]["reason_codes"].append(
                            f"universe_degraded_to_last_accepted:age={stale}d"
                        )
                        completed[name] = report["stages"][name]
                elif name == "research_chain":
                    blocked_reasons.append("research_chain_failed")
                elif name == "reconcile":
                    blocked_reasons.append("reconcile_failed")

        report["status"] = _overall_status(report)
        report["finished_at"] = _now_iso()
        _publish(root, report)
        return report
    finally:
        if lock_handle is not None:
            release_pipeline_lock(lock_handle, lock_path, run_id=rid)


def _overall_status(report: dict[str, Any]) -> str:
    from quantdesk.pipeline_terminal import classify_pipeline_status

    classified = classify_pipeline_status(report.get("stages") or {})
    skipped = classified.get("skipped_stages") or []
    if skipped:
        report["skipped_stages"] = skipped
    failed = classified.get("failed_stages") or []
    if failed:
        report["failed_stages"] = failed
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
    run_dir = out_dir / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest_path = root / LATEST_JSON_RELATIVE
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    # last_successful lives in its OWN pointer file and is NEVER inferred from
    # the latest attempt - two consecutive failures must not erase the last
    # success (the P1 Codex caught).
    success_pointer = latest_path.parent / "data_close_pipeline_last_successful.json"
    from quantdesk.pipeline_terminal import SUCCESS_STATUSES

    if str(report.get("status")) in SUCCESS_STATUSES:
        tmp = success_pointer.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, success_pointer)
    report = dict(report)
    report["last_successful_run_id"] = ""
    if success_pointer.is_file():
        try:
            prev = json.loads(success_pointer.read_text(encoding="utf-8"))
            report["last_successful_run_id"] = str(prev.get("run_id") or "")
        except (OSError, json.JSONDecodeError):
            report["last_successful_run_id"] = ""
    report["latest_attempt_is_successful"] = str(report.get("status")) in SUCCESS_STATUSES
    tmp = latest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, latest_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuantDesk Data Close Pipeline (consolidated).")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--network", default="false")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--resume-run-id", default="")
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
    )
    if truthy(args.json):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return STATUS_TO_EXIT.get(str(report.get("status")), EXIT_INTERNAL_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
