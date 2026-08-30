"""Stage 4 forward-window maintenance evidence collector.

The collector cross-checks local forward-PIT continuity, daily shadow counts,
and frozen gate bytes.  From T0+8 onward it may attach count-only output from
the approved real-canonical tri-account replay adapter. Synthetic adapters,
failed quality gates, broker routes, and funded state writes remain forbidden.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from quantdesk.market_clock import is_cn_a_share_trading_day, load_cn_trading_calendar
from quantdesk.paper.stage4_forward_replay import (
    detect_t0_readiness,
    interim_evidence_pack,
)
from quantdesk.risk.risk_gate_shadow_weekly import load_shadow_daily_window
from quantdesk.utils import atomic_write_json


STAGE4_MAINTENANCE_SCHEMA_VERSION = "stage4_maintenance_evidence_v1"
MIN_INTERIM_ELIGIBLE_DAYS = 5
PROJECT_ROOT = Path(__file__).resolve().parents[2]
T0_GATE_DATE = "2026-07-27"
T0_GATE_BASELINE_SCHEMA_VERSION = "stage4_t0_gate_baseline_v1"
T0_GATE_CAPTURE_BASIS = "post_t0_freeze_with_pre_t0_mtime_check"
T0_REQUIRED_GATE_PATHS = frozenset(
    {
        "quantdesk/data_truth/sector_history_archive.py",
        "quantdesk/data_truth/forward_pit_readiness.py",
        "quantdesk/risk/risk_gate.py",
        "quantdesk/risk/pre_trade_gate.py",
        "config/default.yaml",
    }
)
SHADOW_COUNT_FIELDS = (
    "observed_count",
    "evaluation_count",
    "would_pass_count",
    "would_block_count",
    "insufficient_evidence_count",
    "legacy_blocked_count",
    "shadow_error_count",
)
SHADOW_ENFORCEMENT_FIELDS = ("risk_gate_enforced", "enforcement_applied")
T0_GATE_BASELINE_PATH = (
    PROJECT_ROOT / "config" / "stage4_t0_gate_baseline_20260727.json"
)


def stage4_terminal_disposition(
    *,
    terminal_reached: bool,
    maintenance_ready: bool,
    account_replay: str,
    missing_shadow_dates: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a finite Stage 4 lifecycle disposition.

    Before T0+18 the window remains open. At T0+18 it may become completed
    only when the maintenance gates and the real replay are both available.
    Otherwise it closes as inconclusive; missing historical observations are
    recorded, never backfilled or reclassified as observed.
    """

    missing = [str(day) for day in missing_shadow_dates if str(day).strip()]
    if not terminal_reached:
        return {
            "status": "in_progress",
            "maintenance_disposition": "ready" if maintenance_ready else "pending",
            "sample_disposition": (
                "available" if account_replay == "enabled_real" else "not_evaluable"
            ),
            "reason_codes": [],
        }
    if maintenance_ready and account_replay == "enabled_real":
        return {
            "status": "completed",
            "maintenance_disposition": "complete",
            "sample_disposition": "available",
            "reason_codes": [],
        }
    reasons = ["stage4_terminal_closed_inconclusive"]
    if missing:
        reasons.append("missing_shadow_observations_irrecoverable")
    if not maintenance_ready:
        reasons.append("stage4_maintenance_not_complete_at_terminal")
    if account_replay != "enabled_real":
        reasons.append("stage4_replay_not_formally_evaluable_at_terminal")
    return {
        "status": "closed_inconclusive",
        "maintenance_disposition": "inconclusive",
        "sample_disposition": "not_evaluable",
        "reason_codes": reasons,
    }

# Frozen on 2026-07-28 against the exact gate/config bytes that predated and
# produced T0=2026-07-27.  Future collector runs compare current bytes to this
# immutable T0 baseline; changing any gate is visible and fail-closed.
def _load_t0_gate_baseline(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid Stage 4 T0 gate baseline: {path}")
    files = payload.get("files")
    captured_at_text = str(payload.get("captured_at") or "").strip()
    try:
        captured_at = datetime.fromisoformat(captured_at_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid Stage 4 T0 gate baseline: {path}") from exc
    if (
        payload.get("schema_version") != T0_GATE_BASELINE_SCHEMA_VERSION
        or payload.get("t0") != T0_GATE_DATE
        or payload.get("capture_basis") != T0_GATE_CAPTURE_BASIS
        or not str(payload.get("note") or "").strip()
        or captured_at.tzinfo is None
        or captured_at.date() < date.fromisoformat(T0_GATE_DATE)
        or not isinstance(files, Mapping)
        or {str(name) for name in files} != T0_REQUIRED_GATE_PATHS
    ):
        raise ValueError(f"invalid Stage 4 T0 gate baseline: {path}")
    for name, item in files.items():
        if not isinstance(item, Mapping):
            raise ValueError(f"invalid Stage 4 T0 gate baseline: {path}")
        sha256 = str(item.get("sha256") or "").strip().lower()
        observed_mtime_text = str(item.get("observed_mtime_utc") or "").strip()
        try:
            int(sha256, 16)
            observed_mtime = datetime.fromisoformat(
                observed_mtime_text.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(f"invalid Stage 4 T0 gate baseline: {path}") from exc
        if (
            len(sha256) != 64
            or observed_mtime.tzinfo is None
            or observed_mtime.date() >= date.fromisoformat(T0_GATE_DATE)
            or item.get("mtime_predates_t0") is not True
        ):
            raise ValueError(f"invalid Stage 4 T0 gate baseline: {path}")
    return dict(payload)


T0_GATE_BASELINE = _load_t0_gate_baseline(T0_GATE_BASELINE_PATH)
T0_QUALIFICATION_GATE_SHA256: dict[str, str] = {
    str(name): str(item["sha256"])
    for name, item in dict(T0_GATE_BASELINE["files"]).items()
}

# Active (v2) baseline: the T0 record above stays the immutable anchor, but every
# run since the ruling-26 approved change reports ``qualification_gate_hash_mismatch``
# against it, which drowns a real drift in known noise. The v2 record carries the
# single approved change plus its predecessor lineage, so a run can say both
# "differs from T0" (always true now, by approval) and "differs from the active
# baseline" (the signal that actually matters). It never repasses history:
# ``applicability=prospective_only`` is enforced below.
ACTIVE_GATE_BASELINE_SCHEMA_VERSION = "stage4_t0_gate_baseline_v2"
ACTIVE_GATE_BASELINE_PATH = (
    PROJECT_ROOT / "config" / "stage4_t0_gate_baseline_v2_20260805.json"
)


def _load_active_gate_baseline(path: Path) -> dict[str, Any] | None:
    """Load the optional active baseline, failing closed on any inconsistency.

    Returns ``None`` when the file is absent so the collector keeps working on
    the T0-only contract. When present it must name the same gate set, declare
    ``prospective_only``, and carry a predecessor whose recorded SHA matches the
    sealed v1 file on disk -- otherwise the lineage claim is unverifiable and the
    record is rejected instead of being silently trusted.
    """

    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid Stage 4 active gate baseline: {path}")
    files = payload.get("files")
    predecessor = payload.get("predecessor")
    if (
        payload.get("schema_version") != ACTIVE_GATE_BASELINE_SCHEMA_VERSION
        or payload.get("t0") != T0_GATE_DATE
        or payload.get("applicability") != "prospective_only"
        or not isinstance(files, Mapping)
        or {str(name) for name in files} != T0_REQUIRED_GATE_PATHS
        or not isinstance(predecessor, Mapping)
    ):
        raise ValueError(f"invalid Stage 4 active gate baseline: {path}")
    declared_v1 = str(predecessor.get("file_sha256") or "").strip().lower()
    actual_v1 = hashlib.sha256(T0_GATE_BASELINE_PATH.read_bytes()).hexdigest()
    if len(declared_v1) != 64 or declared_v1 != actual_v1:
        raise ValueError(
            "Stage 4 active gate baseline predecessor SHA does not match the "
            f"sealed v1 file: {path}"
        )
    approved = payload.get("approved_changes")
    if not isinstance(approved, list) or not approved:
        raise ValueError(f"invalid Stage 4 active gate baseline: {path}")
    for item in approved:
        if not isinstance(item, Mapping):
            raise ValueError(f"invalid Stage 4 active gate baseline: {path}")
        name = str(item.get("path") or "")
        recorded = str(item.get("baseline_v1_sha256") or "").strip().lower()
        if (
            name not in T0_REQUIRED_GATE_PATHS
            or recorded != str(T0_QUALIFICATION_GATE_SHA256.get(name, "")).lower()
        ):
            raise ValueError(
                "Stage 4 active gate baseline approved change does not match the "
                f"T0 record it claims to supersede: {path}"
            )
    return dict(payload)


ACTIVE_GATE_BASELINE = _load_active_gate_baseline(ACTIVE_GATE_BASELINE_PATH)
ACTIVE_QUALIFICATION_GATE_SHA256: dict[str, str] = (
    {
        str(name): str(item["sha256"])
        for name, item in dict(ACTIVE_GATE_BASELINE["files"]).items()
    }
    if ACTIVE_GATE_BASELINE
    else {}
)

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice."
)


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def _snapshot_hash_valid(payload: Mapping[str, Any]) -> bool:
    declared = str(payload.get("snapshot_hash") or "")
    core = dict(payload)
    core.pop("snapshot_hash", None)
    try:
        computed = "sha256:" + hashlib.sha256(_canonical_bytes(core)).hexdigest()
    except (TypeError, ValueError):
        return False
    return declared == computed


def _archive_content_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(payload)
    for key in (
        "written",
        "recovery_reuse",
        "recovery_source_run_id",
        "original_snapshot_hash",
    ):
        projected.pop(key, None)
    return projected


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _iso_day(value: Any) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat() if text == parsed.isoformat() else None


def _expected_days(t0: str | None, as_of: str) -> list[str]:
    if t0 is None:
        return []
    start = date.fromisoformat(t0)
    end = date.fromisoformat(as_of)
    if end < start:
        return []
    calendar = load_cn_trading_calendar()
    out: list[str] = []
    current = start
    while current <= end:
        if is_cn_a_share_trading_day(current, calendar):
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _select_chain_manifests(
    chain_runs_dir: Path,
    *,
    project_root: Path,
    readiness_by_day: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[Path, dict[str, Any]]]:
    candidates: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    root = Path(chain_runs_dir)
    if not root.exists():
        return {}
    for path in sorted(root.glob("*/manifest.json")):
        payload = _read_json(path)
        target_date = _iso_day(payload.get("target_date"))
        if target_date is None:
            continue
        candidates.setdefault(target_date, []).append((path, payload))

    selected: dict[str, tuple[Path, dict[str, Any]]] = {}
    for day, rows in candidates.items():
        ranked: list[tuple[tuple[Any, ...], Path, dict[str, Any]]] = []
        for path, payload in rows:
            candidate_coverage = _coverage_rows(
                Path(project_root),
                [day],
                {day: (path, payload)},
                readiness_by_day,
            )[0]
            components = payload.get("components")
            archive_raw = (
                components.get("sector_history_archive")
                if isinstance(components, Mapping)
                else None
            )
            archive = dict(archive_raw) if isinstance(archive_raw, Mapping) else {}
            archive_eligible = bool(
                archive.get("status") == "passed"
                and archive.get("evidence_eligible") is True
                and archive.get("pit_eligible") is True
            )
            rank = (
                candidate_coverage.get("complete") is True,
                bool(payload.get("published_latest")),
                bool(payload.get("authoritative")),
                archive.get("written") is True,
                archive.get("recovery_reuse") is True,
                archive_eligible,
                str(payload.get("completed_at") or payload.get("generated_at") or ""),
                str(payload.get("run_id") or path.parent.name),
            )
            ranked.append((rank, path, payload))
        winner = max(ranked, key=lambda item: item[0])
        selected[day] = (winner[1], winner[2])
    return selected


def _recovery_reuse_evidence(
    *,
    manifest_path: Path | None,
    manifest: Mapping[str, Any],
    component: Mapping[str, Any],
    attempt: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    day: str,
) -> tuple[bool, list[str], Path | None, dict[str, Any]]:
    component_reuse = component.get("recovery_reuse") is True
    attempt_reuse = attempt.get("recovery_reuse") is True
    if not component_reuse and not attempt_reuse:
        return False, [], None, {}

    reasons: list[str] = []
    if component_reuse != attempt_reuse:
        reasons.append("recovery_reuse_flag_mismatch")
    source_id_values = [
        str(value or "")
        for value in (
            manifest.get("recovery_source_run_id"),
            component.get("recovery_source_run_id"),
            attempt.get("recovery_source_run_id"),
        )
    ]
    source_run_id = (
        source_id_values[0]
        if all(source_id_values) and len(set(source_id_values)) == 1
        else ""
    )
    if not all(source_id_values) or len(set(source_id_values)) != 1:
        reasons.append("recovery_source_run_id_mismatch")
    if (
        not source_run_id
        or Path(source_run_id).name != source_run_id
        or source_run_id in {".", ".."}
    ):
        reasons.append("recovery_source_run_id_invalid")

    snapshot_hash = str(snapshot.get("snapshot_hash") or "")
    original_hash_values = [
        str(value or "")
        for value in (
            component.get("original_snapshot_hash"),
            attempt.get("original_snapshot_hash"),
        )
    ]
    if (
        not all(original_hash_values)
        or len(set(original_hash_values)) != 1
        or original_hash_values[0] != snapshot_hash
    ):
        reasons.append("recovery_original_snapshot_hash_mismatch")

    source_attempt_path: Path | None = None
    source_attempt: dict[str, Any] = {}
    if manifest_path is not None and source_run_id and not reasons.count(
        "recovery_source_run_id_invalid"
    ):
        runs_dir = manifest_path.parent.parent.resolve()
        candidate = (
            runs_dir / source_run_id / "sector_history_archive_attempt.json"
        ).resolve()
        if candidate.parent.parent != runs_dir:
            reasons.append("recovery_source_attempt_path_invalid")
        else:
            source_attempt_path = candidate
            source_attempt = _read_json(candidate)
    if not source_attempt:
        reasons.append("recovery_source_attempt_missing")
    else:
        if source_attempt.get("written") is not True:
            reasons.append("recovery_source_attempt_not_written")
        if (
            source_attempt.get("status") != "passed"
            or source_attempt.get("evidence_eligible") is not True
            or source_attempt.get("pit_eligible") is not True
        ):
            reasons.append("recovery_source_attempt_not_evidence_eligible")
        if source_attempt.get("target_date") != day:
            reasons.append("recovery_source_attempt_target_date_mismatch")
        if str(source_attempt.get("snapshot_hash") or "") != snapshot_hash:
            reasons.append("recovery_source_attempt_snapshot_hash_mismatch")
        if str(source_attempt.get("content_identity") or "") != str(
            snapshot.get("content_identity") or ""
        ):
            reasons.append("recovery_source_attempt_content_identity_mismatch")
        if _archive_content_projection(source_attempt) != _archive_content_projection(
            snapshot
        ):
            reasons.append("recovery_source_attempt_content_mismatch")
    return (
        True,
        list(dict.fromkeys(reasons)),
        source_attempt_path,
        source_attempt,
    )


def _reason_codes(payload: Mapping[str, Any]) -> list[str]:
    values = list(payload.get("reason_codes") or []) + list(
        payload.get("degraded_reasons") or []
    )
    return [str(value) for value in values if str(value)]


def _readiness_integrity(
    readiness: Mapping[str, Any],
    *,
    expected_days: Sequence[str],
    reported_count: int,
) -> tuple[bool, list[str], dict[str, dict[str, Any]]]:
    raw_rows = readiness.get("eligible_forward_days")
    reasons: list[str] = []
    rows_by_day: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_rows, list):
        reasons.append("readiness_eligible_days_invalid")
        raw_rows = []
    observed_order: list[str] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            reasons.append("readiness_eligible_day_invalid")
            continue
        day = _iso_day(raw.get("date"))
        if day is None:
            reasons.append("readiness_eligible_day_invalid")
            continue
        if day in rows_by_day:
            reasons.append("readiness_eligible_day_duplicate")
            continue
        rows_by_day[day] = dict(raw)
        observed_order.append(day)
    if reported_count != len(rows_by_day):
        reasons.append("readiness_eligible_days_mismatch")
    expected_prefix = list(expected_days[: len(observed_order)])
    if observed_order != expected_prefix:
        reasons.append("readiness_eligible_days_not_consecutive")
    return not reasons, list(dict.fromkeys(reasons)), rows_by_day


def _coverage_rows(
    root: Path,
    days: Sequence[str],
    manifests: Mapping[str, tuple[Path, dict[str, Any]]],
    readiness_by_day: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day in days:
        snapshot_path = root / "data" / "sector_history" / day / "snapshot.json"
        revocation_path = snapshot_path.with_name("revocation.json")
        snapshot = _read_json(snapshot_path)
        manifest_row = manifests.get(day)
        manifest_path = manifest_row[0] if manifest_row else None
        manifest = manifest_row[1] if manifest_row else {}
        manifest_components = manifest.get("components")
        component_raw = (
            manifest_components.get("sector_history_archive")
            if isinstance(manifest_components, Mapping)
            else None
        )
        component = dict(component_raw) if isinstance(component_raw, Mapping) else {}
        attempt_path = manifest_path.with_name("sector_history_archive_attempt.json") if manifest_path else None
        attempt = _read_json(attempt_path) if attempt_path else {}
        reasons: list[str] = []
        readiness_row = dict(readiness_by_day.get(day) or {})
        (
            recovery_reuse,
            recovery_reuse_reasons,
            recovery_source_attempt_path,
            recovery_source_attempt,
        ) = _recovery_reuse_evidence(
            manifest_path=manifest_path,
            manifest=manifest,
            component=component,
            attempt=attempt,
            snapshot=snapshot,
            day=day,
        )
        recovery_reuse_verified = bool(
            recovery_reuse and not recovery_reuse_reasons
        )
        if not snapshot:
            reasons.append(
                "snapshot_unreadable" if snapshot_path.is_file() else "snapshot_missing"
            )
        elif (
            snapshot.get("status") != "passed"
            or snapshot.get("evidence_eligible") is not True
            or snapshot.get("pit_eligible") is not True
        ):
            reasons.extend(_reason_codes(snapshot) or ["snapshot_not_evidence_eligible"])
        if snapshot and snapshot.get("target_date") != day:
            reasons.append("snapshot_target_date_mismatch")
        if snapshot and not _snapshot_hash_valid(snapshot):
            reasons.append("snapshot_hash_invalid")
        if revocation_path.exists():
            reasons.append("snapshot_revoked")
        if not manifest:
            reasons.append("chain_manifest_missing")
        else:
            if manifest.get("task_type") != "daily_data_chain":
                reasons.append("chain_manifest_task_type_invalid")
            if manifest.get("authoritative") is not True:
                reasons.append("chain_manifest_not_authoritative")
            if manifest.get("published_latest") is not True:
                reasons.append("chain_manifest_not_published")
            if manifest_path is not None and (
                not str(manifest.get("run_id") or "")
                or str(manifest.get("run_id")) != manifest_path.parent.name
            ):
                reasons.append("chain_manifest_run_identity_mismatch")
            if (
                component.get("written") is not True
                and not recovery_reuse_verified
            ):
                reasons.extend(_reason_codes(component) or ["manifest_archive_not_written"])
            if (
                component.get("status") != "passed"
                or component.get("evidence_eligible") is not True
                or component.get("pit_eligible") is not True
            ):
                reasons.append("manifest_archive_not_evidence_eligible")
        if component and component.get("target_date") != day:
            reasons.append("manifest_target_date_mismatch")
        if not attempt:
            reasons.append("sector_archive_attempt_missing")
        else:
            if attempt.get("written") is not True and not recovery_reuse_verified:
                reasons.extend(_reason_codes(attempt) or ["attempt_archive_not_written"])
            if (
                attempt.get("status") != "passed"
                or attempt.get("evidence_eligible") is not True
                or attempt.get("pit_eligible") is not True
            ):
                reasons.append("attempt_archive_not_evidence_eligible")
        if attempt and attempt.get("target_date") != day:
            reasons.append("attempt_target_date_mismatch")
        if not readiness_row:
            reasons.append("readiness_day_missing")
        reasons.extend(recovery_reuse_reasons)

        if snapshot and component and attempt:
            snapshot_content = _archive_content_projection(snapshot)
            if (
                _archive_content_projection(component) != snapshot_content
                or _archive_content_projection(attempt) != snapshot_content
            ):
                reasons.append("archive_content_mismatch")

        count_names = (
            "membership_count",
            "aligned_membership_count",
            "sector_count",
        )
        for name in count_names:
            values = (
                snapshot.get(name),
                component.get(name),
                attempt.get(name),
                readiness_row.get(name),
            )
            if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
                reasons.append("archive_count_mismatch")
                break
            if len(set(values)) != 1:
                reasons.append("archive_count_mismatch")
                break

        snapshot_hashes = {
            str(value)
            for value in (
                snapshot.get("snapshot_hash"),
                component.get("snapshot_hash"),
                attempt.get("snapshot_hash"),
                readiness_row.get("snapshot_hash"),
            )
            if value
        }
        if len(snapshot_hashes) != 1 or any(
            not value
            for value in (
                snapshot.get("snapshot_hash"),
                component.get("snapshot_hash"),
                attempt.get("snapshot_hash"),
                readiness_row.get("snapshot_hash"),
            )
        ):
            reasons.append("snapshot_hash_mismatch")
        content_identities = {
            str(value)
            for value in (
                snapshot.get("content_identity"),
                component.get("content_identity"),
                attempt.get("content_identity"),
            )
            if value
        }
        if len(content_identities) != 1 or any(
            not value
            for value in (
                snapshot.get("content_identity"),
                component.get("content_identity"),
                attempt.get("content_identity"),
            )
        ):
            reasons.append("content_identity_mismatch")
        reasons = list(dict.fromkeys(reasons))
        pool = dict((manifest.get("universe") or {}).get("current_analysis_pool") or {})
        rows.append(
            {
                "date": day,
                "complete": not reasons,
                "reason_codes": reasons,
                "snapshot_path": _display_path(root, snapshot_path),
                "snapshot_exists": bool(snapshot),
                "snapshot_hash": snapshot.get("snapshot_hash"),
                "content_identity": snapshot.get("content_identity"),
                "readiness_snapshot_hash": readiness_row.get("snapshot_hash"),
                "manifest_path": (
                    _display_path(root, manifest_path) if manifest_path else None
                ),
                "attempt_path": (
                    _display_path(root, attempt_path) if attempt_path else None
                ),
                "run_id": manifest.get("run_id"),
                "chain_status": manifest.get("status"),
                "archive_written": component.get("written") is True,
                "attempt_written": attempt.get("written") is True,
                "archive_recovery_reuse": component.get("recovery_reuse") is True,
                "attempt_recovery_reuse": attempt.get("recovery_reuse") is True,
                "recovery_reuse_verified": recovery_reuse_verified,
                "recovery_source_run_id": (
                    str(component.get("recovery_source_run_id") or "") or None
                ),
                "recovery_source_attempt_path": (
                    _display_path(root, recovery_source_attempt_path)
                    if recovery_source_attempt_path is not None
                    else None
                ),
                "recovery_source_attempt_written": (
                    recovery_source_attempt.get("written") is True
                ),
                "chain_analysis_pool_count": (
                    int(pool.get("symbol_count") or 0) if pool else None
                ),
            }
        )
    return rows


def _daily_count_rows(
    root: Path,
    days: Sequence[str],
    coverage: Sequence[Mapping[str, Any]],
    *,
    shadow_archive_dir: Path,
    shadow_latest_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    window = load_shadow_daily_window(
        archive_dir=shadow_archive_dir,
        latest_path=shadow_latest_path,
        as_of=days[-1] if days else date.today().isoformat(),
        limit=None,
    )
    shadow_by_day = {str(row.get("as_of")): row for row in window.summaries}
    coverage_by_day = {str(row.get("date")): row for row in coverage}
    rejected = list(window.rejected)
    rejected_by_day: dict[str, list[str]] = {}
    rejected_missing_fields_by_day: dict[str, list[str]] = {}
    for item in rejected:
        rejected_day = _iso_day(item.get("as_of"))
        if rejected_day is not None:
            rejected_by_day.setdefault(rejected_day, []).append(
                str(item.get("reason") or "shadow_input_invalid")
            )
            rejected_missing_fields_by_day.setdefault(rejected_day, []).extend(
                str(name)
                for name in list(item.get("missing_fields") or [])
                if str(name)
            )
    rows: list[dict[str, Any]] = []
    for day in days:
        source = window.sources_by_as_of.get(day)
        summary = shadow_by_day.get(day)
        coverage_row = coverage_by_day.get(day, {})
        if summary is None:
            rejection_reasons = list(dict.fromkeys(rejected_by_day.get(day, [])))
            latest_only = rejection_reasons == [
                "latest_without_valid_dated_archive"
            ]
            invalid = bool(rejection_reasons) and not latest_only
            reason_codes = (
                ["shadow_archive_missing"]
                if latest_only
                else rejection_reasons or ["shadow_archive_missing"]
            )
            rows.append(
                {
                    "date": day,
                    "shadow_status": (
                        "latest_only"
                        if latest_only
                        else "invalid" if invalid else "missing"
                    ),
                    "reason_codes": reason_codes,
                    "shadow_source_path": None,
                    "candidate_count": None,
                    "evaluation_count": None,
                    "would_pass_count": None,
                    "would_block_count": None,
                    "insufficient_evidence_count": None,
                    "insufficient_evidence_ratio": None,
                    "missing_component_count": None,
                    "count_consistent": False,
                    "missing_required_fields": list(
                        dict.fromkeys(rejected_missing_fields_by_day.get(day, []))
                    ),
                    "chain_analysis_pool_count": coverage_row.get(
                        "chain_analysis_pool_count"
                    ),
                    "run_id": coverage_row.get("run_id"),
                }
            )
            continue
        source_path = Path(source).resolve() if source else None
        latest_only = source_path == Path(shadow_latest_path).resolve()
        missing_required_fields = [
            name
            for name in (*SHADOW_COUNT_FIELDS, *SHADOW_ENFORCEMENT_FIELDS)
            if name not in summary
        ]
        malformed_enforcement = any(
            name in summary and not isinstance(summary.get(name), bool)
            for name in SHADOW_ENFORCEMENT_FIELDS
        )
        enforced = any(summary.get(name) is True for name in SHADOW_ENFORCEMENT_FIELDS)

        parsed_counts: dict[str, int | None] = {}
        malformed_counts = False
        for name in SHADOW_COUNT_FIELDS:
            if name not in summary:
                parsed_counts[name] = None
                continue
            value = summary.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                malformed_counts = True
                parsed_counts[name] = None
            else:
                parsed_counts[name] = value
        candidate_count = parsed_counts["observed_count"]
        evaluation_count = parsed_counts["evaluation_count"]
        would_pass = parsed_counts["would_pass_count"]
        would_block = parsed_counts["would_block_count"]
        insufficient = parsed_counts["insufficient_evidence_count"]
        legacy_blocked = parsed_counts["legacy_blocked_count"]
        shadow_errors = parsed_counts["shadow_error_count"]
        all_counts_present = all(value is not None for value in parsed_counts.values())
        classified = (
            int(would_pass) + int(would_block) + int(insufficient)
            if all_counts_present
            else None
        )
        ratio = (
            int(insufficient) / int(evaluation_count)
            if all_counts_present and evaluation_count
            else None
        )
        missing_component_payload = summary.get("missing_component_counts")
        missing_component_values = (
            list(missing_component_payload.values())
            if isinstance(missing_component_payload, Mapping)
            else []
        )
        malformed_missing_components = any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in missing_component_values
        )
        missing_components = (
            sum(missing_component_values) if not malformed_missing_components else None
        )
        consistent = (
            not missing_required_fields
            and not malformed_counts
            and not malformed_enforcement
            and not malformed_missing_components
            and all_counts_present
            and candidate_count
            == int(evaluation_count) + int(legacy_blocked) + int(shadow_errors)
            and classified == evaluation_count
        )
        status = "observed"
        reason_codes: list[str] = []
        if latest_only:
            status = "latest_only"
            reason_codes.append("shadow_archive_missing")
        if missing_required_fields:
            status = "invalid"
            reason_codes.append("shadow_required_field_missing")
        if malformed_enforcement:
            status = "invalid"
            reason_codes.append("shadow_enforcement_field_malformed")
        if enforced:
            status = "invalid"
            reason_codes.append("shadow_enforcement_boundary_breach")
            rejected.append(
                {
                    "path": str(source or ""),
                    "reason": "shadow_enforcement_boundary_breach",
                    "as_of": day,
                }
            )
        if malformed_counts:
            status = "invalid"
            reason_codes.append("shadow_count_malformed")
        if malformed_missing_components:
            status = "invalid"
            reason_codes.append("shadow_missing_component_count_malformed")
        if not consistent:
            reason_codes.append("shadow_count_inconsistent")
        source_generated_at = str(summary.get("generated_at") or "")
        generated_day = _iso_day(source_generated_at[:10])
        rows.append(
            {
                "date": day,
                "shadow_status": status,
                "reason_codes": list(dict.fromkeys(reason_codes)),
                "shadow_source_path": (
                    _display_path(root, Path(source)) if source else None
                ),
                "observation_reason": summary.get("observation_reason"),
                "source_generated_at": source_generated_at or None,
                "archive_source": summary.get("archive_source") or "natural_daily_write",
                "as_of_semantics": "next_trading_day_decision_date",
                "captured_on_prior_calendar_day": bool(
                    generated_day is not None and generated_day < day
                ),
                "candidate_count": candidate_count,
                "evaluation_count": evaluation_count,
                "would_pass_count": would_pass,
                "would_block_count": would_block,
                "insufficient_evidence_count": insufficient,
                "insufficient_evidence_ratio": ratio,
                "missing_component_count": missing_components,
                "count_consistent": consistent,
                "missing_required_fields": missing_required_fields,
                "risk_gate_enforced": (
                    summary.get("risk_gate_enforced")
                    if isinstance(summary.get("risk_gate_enforced"), bool)
                    else None
                ),
                "enforcement_applied": (
                    summary.get("enforcement_applied")
                    if isinstance(summary.get("enforcement_applied"), bool)
                    else None
                ),
                "chain_analysis_pool_count": coverage_row.get(
                    "chain_analysis_pool_count"
                ),
                "run_id": coverage_row.get("run_id"),
            }
        )
    return rows, rejected


def _shadow_trend(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    series = [
        {
            "date": row.get("date"),
            "ratio": row.get("insufficient_evidence_ratio"),
            "evaluation_count": row.get("evaluation_count"),
            "observation_reason": row.get("observation_reason"),
        }
        for row in rows
        if row.get("shadow_status") == "observed"
    ]
    ratios = [row["ratio"] for row in series if row.get("ratio") is not None]
    latest = ratios[-1] if ratios else None
    if latest is None:
        direction = "not_observable"
        reasons = {str(row.get("observation_reason") or "") for row in series}
        if "stale_data_no_traffic" in reasons:
            diagnostic = "stale_data_no_traffic"
        elif series:
            diagnostic = "zero_evaluation_volume"
        else:
            diagnostic = "insufficient_shadow_evidence"
    elif float(latest) < 1.0:
        direction = "decreased"
        diagnostic = "decrease_observed_no_conclusion"
    else:
        direction = "unchanged"
        missing = sum(int(row.get("missing_component_count") or 0) for row in rows)
        diagnostic = (
            "evidence_assembly_gap_observed"
            if missing
            else "candidate_structure_or_zero_volume_unresolved"
        )
    return {
        "baseline_ratio": 1.0,
        "baseline_reference": "stage4_preregistered_historical_window",
        "series": series,
        "latest_observed_ratio": latest,
        "direction": direction,
        "diagnostic_classification": diagnostic,
        "conclusion_drawn": False,
    }


def _gate_evidence(
    root: Path,
    *,
    gate_paths: Mapping[str, Path] | None,
    t0_gate_hashes: Mapping[str, str] | None,
) -> dict[str, Any]:
    baseline = dict(t0_gate_hashes or T0_QUALIFICATION_GATE_SHA256)
    using_frozen_baseline = t0_gate_hashes is None
    paths = dict(
        gate_paths
        or {name: root / Path(name) for name in T0_QUALIFICATION_GATE_SHA256}
    )
    current = {name: _sha256(Path(path)) for name, path in paths.items()}
    mismatches = [
        {"path": name, "t0_sha256": baseline.get(name), "current_sha256": current.get(name)}
        for name in sorted(set(baseline) | set(current))
        if baseline.get(name) != current.get(name)
    ]
    evidence: dict[str, Any] = {
        "t0": T0_GATE_DATE,
        "baseline_source": (
            "config/stage4_t0_gate_baseline_20260727.json"
            if using_frozen_baseline
            else "injected_test_baseline"
        ),
        "baseline_captured_at": (
            T0_GATE_BASELINE.get("captured_at") if using_frozen_baseline else None
        ),
        "baseline_capture_basis": (
            T0_GATE_BASELINE.get("capture_basis") if using_frozen_baseline else None
        ),
        "t0_sha256": baseline,
        "current_sha256": current,
        "t0_bundle_sha256": _bundle_sha256(baseline),
        "current_bundle_sha256": _bundle_sha256(current),
        "matches_t0": not mismatches,
        "mismatches": mismatches,
    }
    # Additive active-baseline layer. Only meaningful for the real frozen
    # comparison: an injected test baseline is its own contract and must not be
    # silently re-judged against the production v2 record.
    if using_frozen_baseline and ACTIVE_GATE_BASELINE:
        active = dict(ACTIVE_QUALIFICATION_GATE_SHA256)
        active_mismatches = [
            {
                "path": name,
                "active_sha256": active.get(name),
                "current_sha256": current.get(name),
            }
            for name in sorted(set(active) | set(current))
            if active.get(name) != current.get(name)
        ]
        evidence.update(
            {
                "active_baseline_source": (
                    "config/stage4_t0_gate_baseline_v2_20260805.json"
                ),
                "active_baseline_applicability": ACTIVE_GATE_BASELINE.get(
                    "applicability"
                ),
                "active_baseline_captured_at": ACTIVE_GATE_BASELINE.get("captured_at"),
                "active_sha256": active,
                "active_bundle_sha256": _bundle_sha256(active),
                "matches_active_baseline": not active_mismatches,
                "active_baseline_mismatches": active_mismatches,
                "approved_changes": [
                    {
                        "path": str(item.get("path") or ""),
                        "baseline_v1_sha256": str(item.get("baseline_v1_sha256") or ""),
                        "active_sha256": str(item.get("active_sha256") or ""),
                        "added_bytes": item.get("added_bytes"),
                        "authority": str(item.get("authority") or ""),
                    }
                    for item in (ACTIVE_GATE_BASELINE.get("approved_changes") or [])
                    if isinstance(item, Mapping)
                ],
                "predecessor_lineage": {
                    "path": str(
                        (ACTIVE_GATE_BASELINE.get("predecessor") or {}).get("path") or ""
                    ),
                    "file_sha256": str(
                        (ACTIVE_GATE_BASELINE.get("predecessor") or {}).get(
                            "file_sha256"
                        )
                        or ""
                    ),
                    "bundle_sha256": str(
                        (ACTIVE_GATE_BASELINE.get("predecessor") or {}).get(
                            "bundle_sha256"
                        )
                        or ""
                    ),
                    "sealed": True,
                    "verified_against_disk": True,
                },
                # Historical artifacts are never repassed: the T0 verdict above is
                # authoritative for anything already produced.
                "retroactive_repass": False,
            }
        )
    return evidence


def _resolve_evidence_path(root: Path, value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def _birth_identity_matches(
    root: Path,
    birth: Mapping[str, Any],
    *,
    t0: str | None,
    coverage: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    if t0 is None:
        matches = bool(
            birth and birth.get("born") is False and birth.get("t0") is None
        )
        return matches, [] if matches else ["t0_birth_state_mismatch"]
    if (
        not birth
        or birth.get("schema_version") != "round8_t0_birth_evidence_v1"
        or birth.get("born") is not True
        or birth.get("t0") != t0
        or list(birth.get("reason_codes") or [])
    ):
        return False, ["t0_birth_state_mismatch"]
    coverage_row = next(
        (dict(row) for row in coverage if str(row.get("date") or "") == t0),
        {},
    )
    if not coverage_row:
        return False, ["t0_birth_coverage_missing"]
    cross_checks = birth.get("cross_checks")
    if not isinstance(cross_checks, Mapping):
        return False, ["t0_birth_cross_checks_missing"]
    snapshot = cross_checks.get("snapshot")
    chain = cross_checks.get("chain_manifest")
    readiness = cross_checks.get("readiness")
    if not all(isinstance(item, Mapping) for item in (snapshot, chain, readiness)):
        return False, ["t0_birth_cross_checks_missing"]

    reasons: list[str] = []
    if (
        snapshot.get("ok") is not True
        or snapshot.get("snapshot_hash") != coverage_row.get("snapshot_hash")
        or snapshot.get("content_identity") != coverage_row.get("content_identity")
    ):
        reasons.append("t0_birth_snapshot_identity_mismatch")
    if (
        chain.get("found") is not True
        or chain.get("archive_written") is not True
        or chain.get("archive_status") != "passed"
        or chain.get("run_id") != coverage_row.get("run_id")
        or chain.get("archive_snapshot_hash") != coverage_row.get("snapshot_hash")
        or chain.get("archive_content_identity")
        != coverage_row.get("content_identity")
    ):
        reasons.append("t0_birth_chain_identity_mismatch")
    birth_manifest_path = _resolve_evidence_path(root, chain.get("manifest_path"))
    coverage_manifest_path = _resolve_evidence_path(
        root, coverage_row.get("manifest_path")
    )
    if birth_manifest_path is not None and birth_manifest_path != coverage_manifest_path:
        reasons.append("t0_birth_manifest_path_mismatch")
    if readiness.get("ok") is not True or readiness.get("anchored_t0") != t0:
        reasons.append("t0_birth_readiness_identity_mismatch")
    return not reasons, reasons


def _assert_no_outcome_fields(payload: Mapping[str, Any]) -> None:
    forbidden_key_fragments = ("return", "pnl", "profit")

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(token in lowered for token in forbidden_key_fragments):
                    raise ValueError(f"forbidden Stage 4 maintenance field: {key}")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            lowered = value.lower()
            if (
                any(token in lowered for token in ("return", "pnl", "profit"))
                or "收益" in value
            ):
                raise ValueError("forbidden Stage 4 maintenance prose")

    if payload.get("conclusion_drawn") is not False:
        raise ValueError("Stage 4 maintenance evidence requires conclusion_drawn=false")
    walk(payload)


def build_stage4_maintenance_replay_section(
    adapter_result: Any,
    *,
    milestone: str,
    as_of: str,
    maintenance_gates: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Expose only real, quality-passed interim counts to maintenance evidence.

    ``maintenance_gates`` uses the adapter's closed gate vocabulary
    (``MAINTENANCE_REPLAY_GATE_NAMES``): a gate that is not yet true downgrades
    the section to ``observed_real_pending_maintenance_gates`` so the counts
    stay visible for review but are never counted as formal evidence.
    """

    from quantdesk.paper.stage4_replay_adapter import MAINTENANCE_REPLAY_GATE_NAMES

    quality_gate = getattr(adapter_result, "quality_gate", {})
    accounts = getattr(adapter_result, "account_results", {})
    real_eligible = bool(
        getattr(adapter_result, "maintenance_eligible", False)
        and isinstance(quality_gate, Mapping)
        and quality_gate.get("passed") is True
        and getattr(adapter_result, "fixture_or_synthetic", True) is False
        and isinstance(accounts, Mapping)
        and set(accounts) == {"A", "B", "C"}
    )
    if not real_eligible:
        return {
            "account_replay": "not_authorized",
            "synthetic_data_used": False,
            "reason_codes": ["stage4_replay_adapter_not_real_eligible"],
        }
    gate_failures = (
        [
            name
            for name in MAINTENANCE_REPLAY_GATE_NAMES
            if maintenance_gates.get(name) is not True
        ]
        if maintenance_gates is not None
        else []
    )
    if gate_failures:
        return {
            "account_replay": "observed_real_pending_maintenance_gates",
            "account_results": {"quality_gate_passed": True},
            "synthetic_data_used": False,
            "formal_evidence_eligible": False,
            "reason_codes": [
                f"stage4_maintenance_gate_failed:{name}" for name in gate_failures
            ],
        }

    pack = interim_evidence_pack(
        milestone if milestone in {"T0+4", "T0+8"} else "T0+8",
        accounts,
        as_of=as_of,
    ).to_dict()
    return {
        "account_replay": "enabled_real",
        "account_results": {
            "milestone": milestone,
            "as_of": as_of,
            "account_candidate_counts": dict(pack["account_candidate_counts"]),
            "account_gate_pass_counts": dict(pack["account_gate_pass_counts"]),
            "account_would_fill_counts": dict(pack["account_would_fill_counts"]),
            "quality_gate_passed": True,
        },
        "synthetic_data_used": False,
        "formal_evidence_eligible": True,
        "reason_codes": [],
    }


def collect_stage4_maintenance_evidence(
    project_root: Path,
    *,
    as_of: str,
    readiness_path: Path | None = None,
    t0_birth_path: Path | None = None,
    chain_runs_dir: Path | None = None,
    shadow_archive_dir: Path | None = None,
    shadow_latest_path: Path | None = None,
    expected_trading_dates: Sequence[str] | None = None,
    gate_paths: Mapping[str, Path] | None = None,
    t0_gate_hashes: Mapping[str, str] | None = None,
    replay_adapter_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Collect maintenance evidence and attach approved real replay counts."""

    root = Path(project_root).resolve()
    as_of_day = _iso_day(as_of)
    if as_of_day is None:
        raise ValueError("as_of must be YYYY-MM-DD")
    readiness_file = Path(
        readiness_path
        or root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    )
    birth_file = Path(
        t0_birth_path
        or root / "results" / "data_health" / "round8_t0_birth_evidence_latest.json"
    )
    runs_dir = Path(chain_runs_dir or root / "results" / "data_chain" / "runs")
    archive_dir = Path(
        shadow_archive_dir or root / "results" / "risk_gate_shadow_daily"
    )
    latest_path = Path(
        shadow_latest_path or root / "results" / "risk_gate_shadow_latest.json"
    )

    readiness_status = detect_t0_readiness(readiness_file)
    readiness = _read_json(readiness_file)
    birth = _read_json(birth_file)
    t0 = readiness_status.t0
    calendar_days = _expected_days(t0, as_of_day)
    supplied_days = (
        [str(day) for day in expected_trading_dates]
        if expected_trading_dates is not None
        else calendar_days
    )
    if any(_iso_day(day) is None for day in supplied_days):
        raise ValueError("expected_trading_dates must contain YYYY-MM-DD values")
    if supplied_days != calendar_days:
        raise ValueError(
            "expected_trading_dates must match the independent trading calendar"
        )
    days = calendar_days

    readiness_consistent, readiness_reasons, readiness_by_day = _readiness_integrity(
        readiness,
        expected_days=days,
        reported_count=readiness_status.current_eligible_forward_days,
    )
    manifests = _select_chain_manifests(
        runs_dir,
        project_root=root,
        readiness_by_day=readiness_by_day,
    )
    coverage = _coverage_rows(root, days, manifests, readiness_by_day)
    daily_counts, shadow_rejections = _daily_count_rows(
        root,
        days,
        coverage,
        shadow_archive_dir=archive_dir,
        shadow_latest_path=latest_path,
    )
    coverage_complete = all(row["complete"] for row in coverage) if days else False
    counts_complete = all(
        row["shadow_status"] == "observed" and row["count_consistent"]
        for row in daily_counts
    ) if days else False
    gate = _gate_evidence(
        root, gate_paths=gate_paths, t0_gate_hashes=t0_gate_hashes
    )
    gate_t0_matches = t0 is None or gate.get("t0") == t0
    gate["readiness_t0"] = t0
    gate["t0_matches_readiness"] = (
        None if t0 is None else bool(gate_t0_matches)
    )
    active_baseline_match = gate.get("matches_active_baseline")
    gate_matches_approved_baseline = (
        bool(active_baseline_match)
        if isinstance(active_baseline_match, bool)
        else bool(gate.get("matches_t0"))
    )
    gate["matches_approved_baseline"] = gate_matches_approved_baseline
    birth_matches, birth_identity_reasons = _birth_identity_matches(
        root,
        birth,
        t0=t0,
        coverage=coverage,
    )
    terminal_date = _iso_day(readiness_status.milestone_dates.get("T0+18"))
    terminal_reached = bool(
        readiness_status.ready_for_terminal
        or (terminal_date is not None and as_of_day >= terminal_date)
    )

    replay_section: dict[str, Any] = {
        "account_replay": "not_authorized",
        "synthetic_data_used": False,
        "reason_codes": [],
    }
    t0_plus8_date = _iso_day(readiness_status.milestone_dates.get("T0+8"))
    adapter_due = bool(
        t0 is not None
        and t0_plus8_date is not None
        and as_of_day >= t0_plus8_date
        and readiness_status.current_eligible_forward_days >= 9
        and days
    )
    if adapter_due:
        try:
            if replay_adapter_fn is None:
                from quantdesk.paper.stage4_replay_adapter import (
                    run_real_stage4_replay_adapter,
                )

                adapter_callable = run_real_stage4_replay_adapter
            else:
                adapter_callable = replay_adapter_fn
            adapter_result = adapter_callable(
                root,
                window_start=t0,
                window_end=days[-1],
                output_dir=root
                / "results"
                / "stage4_replay_adapter"
                / as_of_day,
            )
            replay_section = build_stage4_maintenance_replay_section(
                adapter_result,
                milestone=("T0+18" if terminal_reached else "T0+8"),
                as_of=as_of_day,
                maintenance_gates={
                    "shadow_counts": counts_complete,
                    "t0_birth_evidence": bool(birth) and birth_matches,
                    "forward_replay_window": (
                        readiness_consistent and coverage_complete
                    ),
                },
            )
            if replay_section.get("account_replay") != "enabled_real":
                gate_reasons = getattr(adapter_result, "quality_gate", {}).get(
                    "reason_codes", []
                )
                replay_section["adapter_quality_reason_codes"] = [
                    str(code) for code in gate_reasons if str(code)
                ]
        except Exception as exc:
            replay_section = {
                "account_replay": "not_authorized",
                "synthetic_data_used": False,
                "reason_codes": ["stage4_replay_adapter_error"],
                "adapter_error_type": type(exc).__name__,
            }
    replay_enabled = replay_section.get("account_replay") == "enabled_real"
    maintenance_ready = bool(
        readiness_consistent
        and coverage_complete
        and counts_complete
        and gate_matches_approved_baseline
        and gate_t0_matches
        and birth_matches
    )
    missing_shadow_dates = [
        str(row.get("date"))
        for row in daily_counts
        if row.get("shadow_status") != "observed" and row.get("date")
    ]
    terminal_disposition = stage4_terminal_disposition(
        terminal_reached=terminal_reached,
        maintenance_ready=maintenance_ready,
        account_replay=str(replay_section.get("account_replay") or ""),
        missing_shadow_dates=missing_shadow_dates,
    )

    if t0 is None:
        status = "blocked_pit_evidence_pending"
    elif terminal_reached:
        status = str(terminal_disposition["status"])
    elif readiness_status.current_eligible_forward_days < MIN_INTERIM_ELIGIBLE_DAYS:
        status = "accumulating"
    elif not maintenance_ready:
        status = "maintenance_window_incomplete"
    else:
        status = "maintenance_window_ready"

    reasons: list[str] = list(readiness_reasons)
    if t0 is None:
        reasons.append("t0_not_anchored")
    elif terminal_reached and not replay_enabled:
        reasons.append("t0_plus_18_account_replay_not_authorized")
    elif readiness_status.current_eligible_forward_days < MIN_INTERIM_ELIGIBLE_DAYS:
        reasons.append("eligible_forward_days_below_interim_minimum")
    if days and not coverage_complete:
        reasons.append("coverage_continuity_gap")
    if days and not counts_complete:
        reasons.append("shadow_daily_count_gap")
    if not gate_matches_approved_baseline:
        reasons.append("qualification_gate_hash_mismatch")
    if t0 is not None and not gate_t0_matches:
        reasons.append("qualification_gate_t0_mismatch")
    if not birth:
        reasons.append("t0_birth_evidence_missing")
    elif not birth_matches:
        reasons.append("t0_birth_cross_check_mismatch")
    if adapter_due and not replay_enabled:
        reasons.extend(replay_section.get("reason_codes") or [])
    if terminal_reached:
        reasons.extend(terminal_disposition.get("reason_codes") or [])
    reasons = list(dict.fromkeys(reasons))

    trend = _shadow_trend(daily_counts)
    payload: dict[str, Any] = {
        "schema_version": STAGE4_MAINTENANCE_SCHEMA_VERSION,
        "task_type": "stage4_maintenance_evidence",
        "generated_at": _now_text(),
        "as_of": as_of_day,
        "status": status,
        "lifecycle_status": status,
        "terminal_reached": terminal_reached,
        "terminal_disposition": terminal_disposition,
        "maintenance_disposition": terminal_disposition["maintenance_disposition"],
        "sample_disposition": terminal_disposition["sample_disposition"],
        "reason_codes": reasons,
        "t0": t0,
        "eligible_forward_day_count": readiness_status.current_eligible_forward_days,
        "eligible_forward_dates": [
            str(row.get("date"))
            for row in list(readiness.get("eligible_forward_days") or [])
            if row.get("date")
        ],
        "milestone_dates": dict(readiness_status.milestone_dates),
        "readiness_status": readiness_status.status,
        "readiness_integrity": {
            "consistent": readiness_consistent,
            "reported_eligible_day_count": readiness_status.current_eligible_forward_days,
            "listed_eligible_day_count": len(readiness_by_day),
            "reason_codes": readiness_reasons,
        },
        "readiness_path": _display_path(root, readiness_file),
        "t0_birth_path": _display_path(root, birth_file),
        "t0_birth_cross_check": {
            "found": bool(birth),
            "born": birth.get("born"),
            "t0": birth.get("t0"),
            "matches_readiness": bool(
                birth_matches
            ),
            "identity_reason_codes": birth_identity_reasons,
        },
        "coverage_days": coverage,
        "daily_counts": daily_counts,
        "shadow_input_rejections": shadow_rejections,
        "preregistered_checks": {
            "coverage_continuity": {
                "complete": coverage_complete,
                "expected_day_count": len(days),
                "complete_day_count": sum(bool(row["complete"]) for row in coverage),
            },
            "shadow_evidence_trend": trend,
            "daily_count_integrity": {
                "complete": counts_complete,
                "observed_day_count": sum(
                    row["shadow_status"] == "observed" for row in daily_counts
                ),
                "run_ids": list(
                    dict.fromkeys(
                        str(row.get("run_id"))
                        for row in daily_counts
                        if row.get("run_id")
                    )
                ),
            },
            "qualification_gate_frozen": gate,
            "outcome_conclusion_guard": {
                "status": "enforced",
                "conclusion_drawn": False,
            },
        },
        "account_replay": replay_section["account_replay"],
        "synthetic_data_used": replay_section["synthetic_data_used"],
        "conclusion_drawn": False,
        "risk_gate_enforced": False,
        "enforcement_applied": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }
    if "account_results" in replay_section:
        payload["account_results"] = replay_section["account_results"]
    if "formal_evidence_eligible" in replay_section:
        payload["formal_evidence_eligible"] = replay_section[
            "formal_evidence_eligible"
        ]
    if replay_section.get("adapter_quality_reason_codes"):
        payload["adapter_quality_reason_codes"] = replay_section[
            "adapter_quality_reason_codes"
        ]
    if replay_section.get("adapter_error_type"):
        payload["adapter_error_type"] = replay_section["adapter_error_type"]
    _assert_no_outcome_fields(payload)
    return payload


def write_stage4_maintenance_evidence(
    payload: Mapping[str, Any],
    *,
    output_dir: Path,
    results_root: Path,
    as_of: str,
) -> dict[str, Path]:
    """Write one dated evidence file plus latest, both under results only."""

    as_of_day = _iso_day(as_of)
    if as_of_day is None:
        raise ValueError("as_of must be YYYY-MM-DD")
    snapshot = dict(payload)
    _assert_no_outcome_fields(snapshot)
    directory = Path(output_dir).resolve()
    allowed_root = Path(results_root).resolve()
    try:
        directory.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("Stage 4 maintenance output must stay under results_root") from exc
    dated = directory / f"stage4_maintenance_evidence_{as_of_day}.json"
    latest = directory / "stage4_maintenance_evidence_latest.json"
    atomic_write_json(dated, snapshot)
    atomic_write_json(latest, snapshot)
    return {"dated": dated, "latest": latest}


__all__ = [
    "MIN_INTERIM_ELIGIBLE_DAYS",
    "STAGE4_MAINTENANCE_SCHEMA_VERSION",
    "T0_GATE_BASELINE",
    "T0_GATE_BASELINE_PATH",
    "T0_QUALIFICATION_GATE_SHA256",
    "build_stage4_maintenance_replay_section",
    "collect_stage4_maintenance_evidence",
    "stage4_terminal_disposition",
    "write_stage4_maintenance_evidence",
]
