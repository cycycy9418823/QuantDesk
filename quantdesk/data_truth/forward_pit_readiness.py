"""Round 7 Part B — forward-PIT replay readiness artifact (T0 + milestones).

This module turns the append-only ``data/sector_history`` tree into a single
readiness artifact that anchors T0 and projects the T0+4 / T0+8 / T0+18
milestones onto real A-share trading days.

T0 is defined as the earliest archived day whose snapshot passed every
qualification gate (``evidence_eligible=true``, ``status=passed``) and is not
revoked.  Until such a day exists, T0 stays ``None`` and the milestones remain
unanchored.  The artifact is regenerated after each daily chain run; it never
fabricates a T0 and never writes canonical/state data — only the one readiness
JSON under the ignored ``results/`` root.

No network, no ``--apply``.  Read-only against ``data/sector_history``.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from quantdesk.market_clock import (
    is_cn_a_share_trading_day,
    load_cn_trading_calendar,
    next_cn_a_share_trading_day,
)
from quantdesk.shared import now_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION


READINESS_SCHEMA_VERSION = "round7_forward_pit_replay_readiness_v3"

# Milestone offsets are expressed as *consecutive eligible trading days* counted
# inclusively from T0.  T0 itself is day 1; T0+4 sessions is the 5th day, etc.
_MILESTONES = (
    (1, "T0", "first_valid_forward_archive"),
    (5, "T0+4 trading sessions", "earliest_new_forward_control_window_equivalent"),
    (9, "T0+8 trading sessions", "earliest_new_forward_main_window_equivalent"),
    (
        19,
        "T0+18 trading sessions",
        "earliest_contiguous_forward_span_matching_prior_minute_audit_horizon",
    ),
)

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / "
    "Not Position Advice. Forward PIT readiness only; no historical backfill."
)


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _add_trading_sessions(t0: date, sessions_from_t0: int, calendar: Mapping[str, Any]) -> date:
    """Return the trading day that is ``sessions_from_t0`` sessions after T0.

    ``sessions_from_t0`` is a 0-based count of additional sessions: 0 → T0,
    4 → the 4th trading day strictly after T0.  T0 must itself be a trading day.
    """

    current = t0
    remaining = sessions_from_t0
    while remaining > 0:
        current = next_cn_a_share_trading_day(current, calendar)
        remaining -= 1
    return current


def scan_eligible_forward_days(history_root: Path) -> list[dict[str, Any]]:
    """Return every eligible, non-revoked forward snapshot, sorted by date.

    A day is eligible only when its snapshot parses, has ``evidence_eligible``
    true and ``status == 'passed'``, and has no ``revocation.json`` beside it.
    """

    root = Path(history_root)
    if not root.exists():
        return []
    eligible: list[dict[str, Any]] = []
    for snapshot_path in sorted(root.glob("*/snapshot.json")):
        day = _parse_day(snapshot_path.parent.name)
        if day is None:
            continue
        if (snapshot_path.parent / "revocation.json").exists():
            continue
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("evidence_eligible") is True and payload.get("status") == "passed":
            eligible.append(
                {
                    "date": day.isoformat(),
                    "snapshot_path": str(snapshot_path.resolve()),
                    "snapshot_hash": str(payload.get("snapshot_hash") or ""),
                    "membership_count": int(payload.get("membership_count") or 0),
                    "aligned_membership_count": int(
                        payload.get("aligned_membership_count") or 0
                    ),
                    "sector_count": int(payload.get("sector_count") or 0),
                }
            )
    eligible.sort(key=lambda item: item["date"])
    return eligible


def build_forward_pit_readiness(
    project_root: Path,
    *,
    calendar: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the readiness payload: T0 (or None) plus trading-day milestones."""

    root = Path(project_root).resolve()
    history_root = root / "data" / "sector_history"
    cal = calendar or load_cn_trading_calendar()
    eligible = scan_eligible_forward_days(history_root)

    t0_day = _parse_day(eligible[0]["date"]) if eligible else None
    t0_iso = t0_day.isoformat() if t0_day is not None else None

    milestones: list[dict[str, Any]] = []
    for required_days, label, purpose in _MILESTONES:
        entry: dict[str, Any] = {
            "required_consecutive_trading_days": required_days,
            "relative_to_t0": label,
            "calendar_date": None,
            "status": "unanchored_t0_unknown",
            "purpose": purpose,
        }
        if t0_day is not None:
            target = _add_trading_sessions(t0_day, required_days - 1, cal)
            entry["calendar_date"] = target.isoformat()
            achieved = len(eligible) >= required_days
            entry["status"] = "achieved" if achieved else "pending_forward_accumulation"
        milestones.append(entry)

    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "generated_at": str(generated_at or now_text()),
        "stage": "Stage 4",
        "round": "Round 7 Part B",
        "status": "t0_launched" if t0_day is not None else "blocked_pit_evidence_pending",
        "t0": t0_iso,
        "t0_is_trading_day": (
            bool(is_cn_a_share_trading_day(t0_day, cal)) if t0_day is not None else None
        ),
        "current_eligible_forward_days": len(eligible),
        "eligible_forward_days": eligible,
        "milestones": milestones,
        "calendar_source": str(cal.get("source") or "unknown"),
        "qualification": (
            "T0 is the earliest archived day whose snapshot passed every "
            "membership/heat/provenance/freshness/alignment gate and is not "
            "revoked. Milestones are computed on real A-share trading days and "
            "are achieved only when enough consecutive eligible archives exist. "
            "They do not repair the fixed historical windows."
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


def write_forward_pit_readiness(
    project_root: Path,
    *,
    output_path: Path | None = None,
    calendar: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and persist the readiness artifact under results/ (ignored root)."""

    root = Path(project_root).resolve()
    target = Path(
        output_path
        or root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    )
    payload = build_forward_pit_readiness(
        root, calendar=calendar, generated_at=generated_at
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["readiness_path"] = str(target.resolve())
    return payload


__all__ = [
    "build_forward_pit_readiness",
    "scan_eligible_forward_days",
    "write_forward_pit_readiness",
    "READINESS_SCHEMA_VERSION",
]
