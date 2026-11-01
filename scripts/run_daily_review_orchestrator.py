"""Run one offline, exactly-once evening research review event.

Research Only / Paper Account Only / No Broker Route / Live Execution Locked.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_PROJECT_ROOT))

from quantdesk.daily_review_orchestrator import (  # noqa: E402
    DISCLAIMER,
    run_evening_review,
    validate_stage4_evidence_pair,
    validate_stage4_terminal_evidence_pair,
)
from quantdesk.interprocess_lock import interprocess_file_lock  # noqa: E402
from quantdesk.daily_data_chain import _read_chain_progress  # noqa: E402
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402
from quantdesk.update_task_lock import is_pid_alive  # noqa: E402
from quantdesk.utils import atomic_write_json  # noqa: E402


class AbandonedDailyChainProducer(RuntimeError):
    """The metadata says running, but its producer process is gone."""

    def __init__(self, run_id: str, target_date: str) -> None:
        self.run_id = str(run_id or "")
        self.target_date = str(target_date or "")
        super().__init__(
            "daily_data_chain_producer_abandoned:"
            f"run_id={self.run_id or 'unknown'}:target_date={self.target_date or 'unknown'}"
        )


class DailyChainWaitTimeout(TimeoutError):
    """No terminal event for the frozen business date arrived within the wait."""

    def __init__(self, target_date: str, observed_date: str, reason: str,
                 producer_progress: dict[str, Any] | None = None) -> None:
        self.target_date = target_date
        self.observed_date = observed_date
        self.reason_code = reason
        self.producer_progress = dict(producer_progress or {})
        super().__init__(
            f"daily_data_chain_wait_timeout:target_date={target_date}:"
            f"observed_target_date={observed_date or 'missing'}:reason={reason}"
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(DEFAULT_PROJECT_ROOT))
    parser.add_argument("--chain-manifest", default="")
    parser.add_argument("--target-date", default="")
    parser.add_argument("--lease-seconds", type=int, default=7200)
    parser.add_argument("--chain-wait-seconds", type=float, default=7200.0)
    parser.add_argument("--json", dest="as_json", default="false")
    parser.add_argument("--summary", default="true")
    return parser.parse_args(argv)


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_json_object(path: Path, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{reason}:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{reason}:invalid_object")
    return payload


def _read_chain_manifest(path: Path) -> dict[str, Any]:
    payload = _read_json_object(path, "authoritative_chain_manifest_unreadable")
    if not isinstance(payload, dict) or payload.get("authoritative") is not True:
        raise RuntimeError("authoritative_chain_manifest_invalid")
    if not str(payload.get("run_id") or "").strip() or not str(
        payload.get("target_date") or ""
    ).strip():
        raise RuntimeError("authoritative_chain_event_identity_missing")
    return payload


def _iso_day(value: str) -> str:
    day = str(value or "").strip()
    if not day or datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError("target_date must be an ISO calendar date")
    return day


def _expected_chain_probe(
    metadata: dict[str, Any], manifest_path: Path, target_date: str,
    *, progress: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Read only: an earlier abandoned event is evidence, never a new review."""

    status = str(metadata.get("status") or "")
    if status in {"running", "abandoned"}:
        run_id = str(metadata.get("run_id") or "").strip()
        try:
            observed_date = _iso_day(str(metadata.get("target_date") or ""))
        except ValueError as exc:
            raise RuntimeError("daily_data_chain_abandoned_identity_invalid") from exc
        if not run_id:
            raise RuntimeError("daily_data_chain_abandoned_identity_invalid")
        if observed_date > target_date:
            raise RuntimeError("daily_data_chain_target_date_in_future")
        if status == "running":
            try:
                producer_pid = int(metadata["pid"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("daily_data_chain_running_pid_invalid") from exc
            if producer_pid <= 0:
                raise RuntimeError("daily_data_chain_running_pid_invalid")
            if is_pid_alive(producer_pid):
                raise RuntimeError("daily_data_chain_metadata_running_without_os_lock")
        return None, observed_date, "daily_data_chain_producer_abandoned"
    if metadata and status != "released":
        raise RuntimeError("daily_data_chain_lock_status_invalid")
    # A controlled interruption executes the lock's finally block, but it still
    # has no terminal event. A released lock alone does not mean completed.
    unfinished = bool(
        status == "released" and progress and progress.get("status") == "running"
        and progress.get("run_id") == metadata.get("run_id")
        and progress.get("pid") == metadata.get("pid")
    )
    interrupted_date = _iso_day(str(progress["target_date"])) if unfinished else ""
    if not manifest_path.is_file():
        if unfinished:
            if interrupted_date > target_date:
                raise RuntimeError("daily_data_chain_target_date_in_future")
            return None, interrupted_date, "daily_data_chain_producer_abandoned"
        return None, "", "authoritative_chain_manifest_missing"
    manifest = _read_chain_manifest(manifest_path)
    observed_date = _iso_day(str(manifest["target_date"]))
    if observed_date > target_date:
        raise RuntimeError("daily_data_chain_target_date_in_future")
    if status == "released" and str(metadata.get("run_id") or "") != str(manifest["run_id"]):
        if unfinished:
            if interrupted_date > target_date:
                raise RuntimeError("daily_data_chain_target_date_in_future")
            return None, interrupted_date, "daily_data_chain_producer_abandoned"
        raise RuntimeError("daily_data_chain_terminal_manifest_mismatch")
    if observed_date != target_date:
        return None, observed_date, "daily_data_chain_target_date_pending"
    if not metadata:
        return None, observed_date, "daily_data_chain_terminal_metadata_missing"
    return manifest, observed_date, ""


@contextmanager
def locked_authoritative_chain_event(
    *,
    project_root: Path,
    manifest_path: Path,
    timeout_seconds: float = 1800.0,
    poll_seconds: float = 2.0,
    expected_target_date: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Release the producer lock while waiting; hold it once the date is selected.

    Explicit historical callers may omit the expected date. Automatic launches
    freeze it before waiting so yesterday's event cannot satisfy today's run.
    """

    root = Path(project_root).resolve()
    os_lock = root / "state" / "daily_data_chain.lock"
    metadata_path = root / "state" / "daily_data_chain.lock.json"
    if expected_target_date is not None:
        expected = _iso_day(expected_target_date)
        wait_seconds, poll = float(timeout_seconds), float(poll_seconds)
        if not math.isfinite(wait_seconds) or wait_seconds < 0 or not math.isfinite(poll) or poll <= 0:
            raise ValueError("daily_data_chain_wait_parameters_invalid")
        deadline = time.monotonic() + wait_seconds
        observed, reason = "", "daily_data_chain_producer_busy"
        while True:
            selected = False
            producer_progress: dict[str, Any] = {}
            try:
                with interprocess_file_lock(os_lock, timeout_seconds=0):
                    metadata = _read_json_object(metadata_path, "daily_data_chain_lock_unreadable") if metadata_path.exists() else {}
                    producer_progress = _read_chain_progress(
                        root, str(metadata.get("run_id") or ""),
                        str(metadata.get("target_date") or "")[:10],
                    )
                    manifest, observed, reason = _expected_chain_probe(
                        metadata, Path(manifest_path), expected, progress=producer_progress,
                    )
                    if manifest is not None:
                        selected = True
                        yield manifest
                        return
            except TimeoutError:
                if selected:
                    raise
                reason = "daily_data_chain_producer_busy"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DailyChainWaitTimeout(expected, observed, reason, producer_progress)
            time.sleep(min(poll, remaining))
        return
    acquired = False
    try:
        with interprocess_file_lock(
            os_lock,
            timeout_seconds=max(0.0, float(timeout_seconds)),
            poll_seconds=max(0.001, float(poll_seconds)),
        ):
            acquired = True
            metadata = (
                _read_json_object(
                    metadata_path,
                    "daily_data_chain_lock_unreadable",
                )
                if metadata_path.exists()
                else {}
            )
            status = str(metadata.get("status") or "")
            if status == "running":
                try:
                    producer_pid = int(metadata.get("pid") or 0)
                except (TypeError, ValueError):
                    producer_pid = 0
                if not is_pid_alive(producer_pid):
                    reconciled = dict(metadata)
                    reconciled.update(
                        {
                            "status": "abandoned",
                            "abandoned_at": datetime.now(timezone.utc).isoformat(),
                            "abandoned_reason": "producer_pid_not_alive",
                            "recovery_required": True,
                        }
                    )
                    atomic_write_json(metadata_path, reconciled)
                    raise AbandonedDailyChainProducer(
                        str(metadata.get("run_id") or ""),
                        str(metadata.get("target_date") or ""),
                    )
                raise RuntimeError("daily_data_chain_metadata_running_without_os_lock")
            if status not in {"", "released"}:
                raise RuntimeError("daily_data_chain_lock_status_invalid")
            manifest = _read_chain_manifest(Path(manifest_path))
            if status == "released" and str(metadata.get("run_id") or "") != str(
                manifest.get("run_id") or ""
            ):
                raise RuntimeError("daily_data_chain_terminal_manifest_mismatch")
            yield manifest
    except TimeoutError as exc:
        if acquired:
            raise
        raise TimeoutError("daily_data_chain_wait_timeout") from exc


def _command_runner(command: list[str], root: Path):
    def run() -> int:
        completed = subprocess.run(command, cwd=root, check=False)
        return int(completed.returncode)

    return run


def run_stage4_and_validate(
    command: list[str],
    root: Path,
    target_date: str,
    *,
    process_runner=subprocess.run,
) -> int:
    milestones_path = (
        root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    )
    try:
        readiness = _read_json_object(
            milestones_path, "stage4_milestones_unreadable"
        )
        milestone_rows = readiness.get("milestones")
        terminal_date = next(
            str(item.get("calendar_date") or "")[:10]
            for item in milestone_rows
            if isinstance(item, dict)
            and str(item.get("relative_to_t0") or "").startswith("T0+18")
        )
    except (RuntimeError, StopIteration, TypeError):
        terminal_date = ""
    terminal_due = bool(terminal_date and target_date >= terminal_date)
    validation_date = terminal_date if terminal_due else target_date
    effective_command = list(command)
    try:
        as_of_index = effective_command.index("--as-of")
    except ValueError:
        effective_command.extend(["--as-of", validation_date])
    else:
        if as_of_index + 1 >= len(effective_command):
            effective_command.append(validation_date)
        else:
            effective_command[as_of_index + 1] = validation_date
    completed = process_runner(effective_command, cwd=root, check=False)
    if int(completed.returncode) != 0:
        return int(completed.returncode)
    validator = (
        validate_stage4_terminal_evidence_pair
        if terminal_due
        else validate_stage4_evidence_pair
    )
    valid, _reason = validator(root, validation_date)
    return 0 if valid else 78


def run_candidate_tracking_and_validate(
    root: Path,
    target_date: str,
    authoritative_chain_run_id: str,
    *,
    detailed: bool = False,
) -> int | dict[str, Any]:
    """Materialize only the verified dated formal batch, then rebuild views."""

    from quantdesk.candidate_tracking import (
        build_candidate_performance,
        build_tracking_summary,
        materialize_formal_candidate_tracking,
    )
    from quantdesk.evidence_freshness import refresh_persisted_evidence_freshness

    result = materialize_formal_candidate_tracking(
        root,
        target_date=target_date,
        authoritative_chain_run_id=authoritative_chain_run_id,
    )
    if result.get("status") not in {
        "completed",
        "completed_with_warnings",
        "idempotent",
    }:
        if detailed:
            return {
                "status": "skipped", "reason_codes": list(result.get("reason_codes") or [])
                or ["formal_candidate_tracking_unavailable"],
                "source_status": result.get("status"), "target_date": target_date,
                "source_run_id": authoritative_chain_run_id, "applied": False,
            }
        print(
            "daily-review-orchestrator: candidate-tracking-blocked: "
            + json.dumps(result, ensure_ascii=False),
            file=sys.stderr,
        )
        return 78
    try:
        provenance = {
            "trigger": "scheduled_daily_review",
            "authoritative_chain_run_id": str(
                result.get("source_run_id") or authoritative_chain_run_id
            ),
            "source_hash": str(result.get("source_hash") or ""),
            "formal_tracking_artifact_hash": str(result.get("artifact_hash") or ""),
        }
        build_candidate_performance(root, provenance=provenance)
        build_tracking_summary(root, provenance=provenance)
        refresh_persisted_evidence_freshness(
            root,
            as_of=target_date,
            provenance=provenance,
        )
    except Exception as exc:  # noqa: BLE001 - receipt must expose the failed step
        print(
            "daily-review-orchestrator: candidate-tracking-failed: "
            f"{type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        return 79
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.project_root).resolve()
    manifest_path = Path(args.chain_manifest).resolve() if args.chain_manifest else (
        root / "results" / "data_chain" / "daily_data_chain_latest.json"
    )
    expected_date: str | None = None
    try:
        if args.target_date:
            expected_date = _iso_day(args.target_date)
        elif not args.chain_manifest:
            expected_date = _iso_day(str(resolve_target_complete_trade_date(
                calendar_path=root / "data/calendar/a_share_trading_calendar.csv",
            )["target_date"]))
        with locked_authoritative_chain_event(
            project_root=root,
            manifest_path=manifest_path,
            timeout_seconds=args.chain_wait_seconds,
            expected_target_date=expected_date,
        ) as chain:
            target_date = str(chain["target_date"])
            daily_command = [
                sys.executable,
                "-B",
                str(root / "scripts" / "qd.py"),
                "daily", "--network", "false",
                "--target-date", target_date,
                "--with-guardrails", "true",
                "--summary", "true",
            ]
            result = run_evening_review(
                project_root=root,
                target_date=target_date,
                expected_target_date=expected_date,
                authoritative_chain_run_id=str(chain["run_id"]),
                trigger="scheduled_daily_review",
                daily_runner=_command_runner(daily_command, root),
                candidate_tracking_runner=lambda: run_candidate_tracking_and_validate(
                    root,
                    target_date,
                    str(chain["run_id"]),
                    detailed=True,
                ),
                lease_seconds=args.lease_seconds,
            )
    except TimeoutError as exc:
        if not isinstance(exc, DailyChainWaitTimeout) and str(exc) != "daily_data_chain_wait_timeout":
            print(f"daily-review-orchestrator: operation-timeout: {exc}", file=sys.stderr)
            return 2
        if _truthy(args.as_json):
            print(json.dumps({
                "status": "deferred", "state": "awaiting_daily_data",
                "target_date": expected_date,
                "observed_target_date": getattr(exc, "observed_date", ""),
                "reason_code": getattr(exc, "reason_code", "daily_data_chain_wait_timeout"),
                "producer_progress": getattr(exc, "producer_progress", {}),
                "daily_executed": False, "stage4_executed": False,
                "production_completed": False, "disclaimer": DISCLAIMER,
            }, ensure_ascii=False, indent=2))
        print(f"daily-review-orchestrator: wait-timeout: {exc}", file=sys.stderr)
        return 4
    except AbandonedDailyChainProducer as exc:
        print(f"daily-review-orchestrator: producer-abandoned: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError) as exc:
        print(f"daily-review-orchestrator: blocked: {exc}", file=sys.stderr)
        return 2

    if _truthy(args.as_json):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif _truthy(args.summary):
        print(
            "daily-review-orchestrator: "
            f"state={result.get('state')} event={result.get('event_key')} "
            f"daily_executed={result.get('daily_executed')} "
            f"stage4_executed={result.get('stage4_executed')}"
        )
        print(DISCLAIMER)
    if result.get("state") == "retryable_failed":
        return int(result.get("returncode") or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
