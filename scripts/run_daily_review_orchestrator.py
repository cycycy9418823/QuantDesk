"""Run one offline, exactly-once evening research review event.

Research Only / Paper Account Only / No Broker Route / Live Execution Locked.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(DEFAULT_PROJECT_ROOT))
    parser.add_argument("--chain-manifest", default="")
    parser.add_argument("--lease-seconds", type=int, default=7200)
    parser.add_argument("--chain-wait-seconds", type=float, default=1800.0)
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


@contextmanager
def locked_authoritative_chain_event(
    *,
    project_root: Path,
    manifest_path: Path,
    timeout_seconds: float = 1800.0,
    poll_seconds: float = 2.0,
) -> Iterator[dict[str, Any]]:
    """Hold the producer's OS lock while selecting and consuming its event."""

    root = Path(project_root).resolve()
    os_lock = root / "state" / "daily_data_chain.lock"
    metadata_path = root / "state" / "daily_data_chain.lock.json"
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
) -> int:
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
    try:
        with locked_authoritative_chain_event(
            project_root=root,
            manifest_path=manifest_path,
            timeout_seconds=args.chain_wait_seconds,
        ) as chain:
            target_date = str(chain["target_date"])
            daily_command = [
                sys.executable,
                "-B",
                str(root / "scripts" / "qd.py"),
                "daily", "--network", "false",
                "--with-guardrails", "true",
                "--summary", "true",
            ]
            stage4_command = [
                sys.executable,
                "-B",
                str(root / "scripts" / "qd.py"),
                "stage4-maintenance", "--network", "false",
                "--as-of", target_date,
                "--json", "true",
                "--summary", "true",
            ]
            result = run_evening_review(
                project_root=root,
                target_date=target_date,
                authoritative_chain_run_id=str(chain["run_id"]),
                trigger="scheduled_daily_review",
                daily_runner=_command_runner(daily_command, root),
                candidate_tracking_runner=lambda: run_candidate_tracking_and_validate(
                    root,
                    target_date,
                    str(chain["run_id"]),
                ),
                stage4_runner=lambda: run_stage4_and_validate(
                    stage4_command, root, target_date
                ),
                lease_seconds=args.lease_seconds,
            )
    except TimeoutError as exc:
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
