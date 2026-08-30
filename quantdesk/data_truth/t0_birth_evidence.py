"""Round 8 Part C — T0 birth evidence pack builder.

When the daily chain first archives an eligible, non-revoked forward sector
snapshot (``written=true`` / ``status=passed``), that day is T0.  This module
assembles a single self-contained *birth evidence pack* proving the birth is
real and internally consistent, cross-checking three independent artifacts:

1. the append-only ``data/sector_history/<T0>/snapshot.json`` itself
   (provenance triple: ``source`` / ``captured_at`` / ``hash``);
2. the same day's daily-chain run ``manifest.json``
   (``components.sector_history_archive`` and ``target_date``); and
3. the refreshed readiness artifact (T0 anchored + T0+4/8/18 trading-day
   milestones).

Every cross-check must agree.  If any disagrees, or the day is not actually
eligible, the builder reports ``born=False`` with honest reason codes and
never fabricates a birth.  It is read-only against ``data/sector_history`` and
``results/`` and writes at most one evidence-pack JSON under the ignored
``results/`` root.  No network, no ``--apply``, no canonical/state write.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from quantdesk.data_truth.forward_pit_readiness import (
    build_forward_pit_readiness,
    scan_eligible_forward_days,
)
from quantdesk.shared import now_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION


EVIDENCE_SCHEMA_VERSION = "round8_t0_birth_evidence_v1"

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / "
    "Not Position Advice. Forward PIT T0 birth evidence only; no historical backfill."
)


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _find_chain_manifest_for(runs_root: Path, target_date: str) -> dict[str, Any]:
    """Return the newest completed chain manifest naming ``target_date``.

    Read-only scan.  The result carries the manifest path and the archive
    component so the pack can cross-check without trusting the snapshot alone.
    """

    result: dict[str, Any] = {
        "found": False,
        "manifest_path": "",
        "run_id": "",
        "status": "",
        "archive_written": None,
        "archive_status": "",
        "archive_content_identity": "",
        "archive_snapshot_hash": "",
    }
    if not runs_root.exists():
        return result
    candidates: list[tuple[str, Path]] = []
    for manifest_path in runs_root.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if str(payload.get("target_date") or "")[:10] != target_date:
            continue
        candidates.append((str(payload.get("generated_at") or ""), manifest_path))
    if not candidates:
        return result
    # Newest by generated_at (ISO strings sort chronologically).
    candidates.sort(key=lambda item: item[0])
    _, chosen = candidates[-1]
    payload = json.loads(chosen.read_text(encoding="utf-8"))
    components = payload.get("components") or {}
    archive = components.get("sector_history_archive") or {}
    result.update(
        {
            "found": True,
            "manifest_path": str(chosen.resolve()),
            "run_id": str(payload.get("run_id") or ""),
            "status": str(payload.get("status") or ""),
            "archive_written": archive.get("written"),
            "archive_status": str(archive.get("status") or ""),
            "archive_content_identity": str(archive.get("content_identity") or ""),
            "archive_snapshot_hash": str(archive.get("snapshot_hash") or ""),
        }
    )
    return result


def build_t0_birth_evidence(
    project_root: Path,
    *,
    calendar: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the T0 birth evidence pack (or an honest not-yet-born report).

    The pack is *derived*, read-only evidence; it never writes canonical data
    and never fabricates a T0.  When no eligible day exists yet it returns
    ``born=False`` with ``reason_codes=['t0_not_yet_born']`` so the daily chain
    can refresh it every run without side effects.
    """

    root = Path(project_root).resolve()
    history_root = root / "data" / "sector_history"
    runs_root = root / "results" / "data_chain" / "runs"

    eligible = scan_eligible_forward_days(history_root)
    readiness = build_forward_pit_readiness(
        root, calendar=calendar, generated_at=generated_at
    )

    base: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "generated_at": str(generated_at or now_text()),
        "stage": "Stage 4",
        "round": "Round 8 Part C",
        "born": False,
        "t0": None,
        "reason_codes": [],
        "cross_checks": {},
        "readiness_status": readiness.get("status"),
        "current_eligible_forward_days": readiness.get(
            "current_eligible_forward_days"
        ),
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "no_broker_route": True,
            "live_execution_locked": True,
            "not_investment_advice": True,
            "not_position_advice": True,
            "stage5_closed": True,
            "canonical_write": False,
            "apply_executed": False,
        },
        "disclaimer": DISCLAIMER,
    }

    if not eligible:
        base["reason_codes"] = ["t0_not_yet_born"]
        return base

    t0 = eligible[0]
    t0_date = t0["date"]
    snapshot_path = Path(t0["snapshot_path"])
    reasons: list[str] = []

    # Cross-check 1: the snapshot itself parses and carries the provenance triple.
    snapshot_ok = False
    provenance_triple: dict[str, Any] = {}
    snapshot_payload: dict[str, Any] = {}
    try:
        snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        reasons.append("t0_snapshot_unreadable")
    else:
        provenance = snapshot_payload.get("provenance") or {}
        provenance_triple = {
            "source": str(provenance.get("source") or ""),
            "captured_at": str(provenance.get("captured_at") or ""),
            "hash": str(provenance.get("hash") or ""),
        }
        if (
            snapshot_payload.get("evidence_eligible") is True
            and snapshot_payload.get("status") == "passed"
            and provenance_triple["source"]
            and provenance_triple["captured_at"]
            and provenance_triple["hash"]
            and _parse_day(provenance_triple["captured_at"]) == _parse_day(t0_date)
        ):
            snapshot_ok = True
        else:
            reasons.append("t0_snapshot_provenance_incomplete")

    # Cross-check 2: the same day's chain manifest agrees the archive was written.
    manifest_info = _find_chain_manifest_for(runs_root, t0_date)
    manifest_ok = False
    if not manifest_info["found"]:
        reasons.append("t0_chain_manifest_not_found")
    elif manifest_info["archive_written"] is not True:
        reasons.append("t0_chain_manifest_archive_not_written")
    elif (
        snapshot_payload
        and manifest_info["archive_content_identity"]
        and str(snapshot_payload.get("content_identity") or "")
        != manifest_info["archive_content_identity"]
    ):
        reasons.append("t0_content_identity_manifest_snapshot_mismatch")
    else:
        manifest_ok = True

    # Cross-check 3: readiness anchored T0 to exactly this day.
    readiness_ok = str(readiness.get("t0") or "") == t0_date
    if not readiness_ok:
        reasons.append("t0_readiness_anchor_mismatch")

    born = snapshot_ok and manifest_ok and readiness_ok and not reasons

    base.update(
        {
            "born": born,
            "t0": t0_date if born else None,
            "reason_codes": sorted(dict.fromkeys(reasons)),
            "cross_checks": {
                "snapshot": {
                    "ok": snapshot_ok,
                    "path": str(snapshot_path.resolve()),
                    "provenance_triple": provenance_triple,
                    "snapshot_hash": str(snapshot_payload.get("snapshot_hash") or ""),
                    "content_identity": str(
                        snapshot_payload.get("content_identity") or ""
                    ),
                    "membership_count": int(t0.get("membership_count") or 0),
                    "aligned_membership_count": int(
                        t0.get("aligned_membership_count") or 0
                    ),
                    "sector_count": int(t0.get("sector_count") or 0),
                },
                "chain_manifest": manifest_info,
                "readiness": {
                    "ok": readiness_ok,
                    "anchored_t0": readiness.get("t0"),
                    "milestones": readiness.get("milestones"),
                },
            },
        }
    )
    return base


def write_t0_birth_evidence(
    project_root: Path,
    *,
    output_path: Path | None = None,
    calendar: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and persist the T0 birth evidence pack under results/ (ignored root)."""

    root = Path(project_root).resolve()
    target = Path(
        output_path
        or root / "results" / "data_health" / "round8_t0_birth_evidence_latest.json"
    )
    payload = build_t0_birth_evidence(
        root, calendar=calendar, generated_at=generated_at
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["evidence_pack_path"] = str(target.resolve())
    return payload


__all__ = [
    "build_t0_birth_evidence",
    "write_t0_birth_evidence",
    "EVIDENCE_SCHEMA_VERSION",
]
