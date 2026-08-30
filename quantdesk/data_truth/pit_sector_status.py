"""PIT sector status derived from accumulated forward sector-history snapshots.

Round 14 fix for the dominant shadow blocker.

Why this module exists
----------------------
``quantdesk/research/sector_rotation_engine.py`` unconditionally emits
``evidence_eligible=False`` plus ``reason_codes=['static_sector_map_proxy_non_pit']``
because it derives sector membership from a *static* symbol->sector map. That
declaration is honest: applying today's membership to historical returns is
look-ahead for membership. The consequence, however, is structural — the risk
gate requires ``evidence_eligible=True`` for the smart-stop component, so
``would_pass``/``would_block`` could never be produced and every shadow
evaluation collapsed to ``insufficient_evidence`` regardless of how many
trading days accumulated.

The project already owns the asset that resolves this properly: the Forward PIT
sector snapshots under ``data/sector_history/<date>/snapshot.json`` (established
at T0=2026-07-27). Each snapshot is a same-day cross-section capture that is
already ``pit_eligible=True`` / ``evidence_eligible=True`` in its own right, and
carries per-sector ``current_rank`` and ``rotation_strength``.

What this module does
---------------------
Reads the accumulated daily snapshots (never a single snapshot's own history —
each date's membership and ranking come from that date's own capture) and
derives the fields the risk gate reads:

* ``rank_change_5d`` — today's rank versus the rank observed 5 archived
  trading days earlier. Positive means the sector moved *up* (rank number fell).
* ``heat_score`` — bounded normalisation of ``rank_change_5d`` against the
  observed sector count, on the same sign convention as the research engine.
* ``heat_status`` / ``sector_status`` — mapped with the *same* vocabulary and
  thresholds as ``quantdesk/research/sector_rotation_engine.py`` so downstream
  consumers see one language, not two.

PIT depth gating
----------------
Windows are only reported when the archive actually holds enough distinct dates
to compute them. Depth is measured in archived snapshot dates, not calendar
days. A window that lacks coverage is listed in ``windows_blocked`` and its
metric is *omitted* — never estimated, never extrapolated, never carried
forward from a shallower window. When even the shortest window is unavailable
the payload declares ``reason_codes=['pit_sector_depth_insufficient']`` and
``evidence_eligible=False`` so the risk gate keeps failing closed.

Boundaries
----------
Read-only with respect to canonical data: this module reads
``data/sector_history/`` and writes a single derived artifact under
``results/``. It never writes canonical data, never touches the ledger, and
never modifies a T0 qualification-gate baseline file.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from quantdesk.shared import now_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

__all__ = [
    "PIT_SECTOR_STATUS_LATEST_JSON",
    "SECTOR_HISTORY_DIR",
    "build_pit_sector_status",
    "write_pit_sector_status",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECTOR_HISTORY_DIR = PROJECT_ROOT / "data" / "sector_history"
PIT_SECTOR_STATUS_LATEST_JSON = PROJECT_ROOT / "results" / "pit_sector_status_latest.json"

TASK_TYPE = "pit_sector_status"
SCHEMA_VERSION = "pit_sector_status_v1"

# Depth (in archived snapshot dates) required to compute each window. A 5-day
# change needs the current date plus the date five archived days earlier.
WINDOW_DEPTH_REQUIRED: dict[int, int] = {5: 6, 20: 21, 60: 61}

# Same thresholds and vocabulary as quantdesk/research/sector_rotation_engine.py
# so the two producers never disagree about what "strengthening" means.
_HEAT_TO_SECTOR = {
    "hot": "leading",
    "improving": "strengthening",
    "neutral": "neutral",
    "weakening": "weakening",
    "cold": "weakening",
}

DISCLAIMER = (
    "Research diagnostics only. Not investment advice. PIT sector status is an "
    "observation layer derived from archived same-day sector captures; it does "
    "not generate orders and does not change screens or scoring weights."
)


def _heat_status(score: float) -> str:
    if score >= 0.06:
        return "hot"
    if score >= 0.015:
        return "improving"
    if score <= -0.06:
        return "cold"
    if score <= -0.015:
        return "weakening"
    return "neutral"


def _parse_iso_day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _snapshot_dates(history_dir: Path, *, as_of: str) -> list[date]:
    """Archived snapshot dates at or before ``as_of``, oldest first.

    A directory only counts when it holds a readable ``snapshot.json`` and is
    not revoked (a sibling ``revocation.json`` marks a withdrawn archive).
    """

    as_of_day = _parse_iso_day(as_of)
    if as_of_day is None or not history_dir.exists():
        return []
    days: list[date] = []
    for child in sorted(history_dir.iterdir()):
        if not child.is_dir():
            continue
        day = _parse_iso_day(child.name)
        if day is None or day > as_of_day:
            continue
        if not (child / "snapshot.json").is_file():
            continue
        if (child / "revocation.json").exists():
            continue
        days.append(day)
    return days


def _load_snapshot(history_dir: Path, day: date) -> Mapping[str, Any] | None:
    path = history_dir / day.isoformat() / "snapshot.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _eligible_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_day: date | None = None,
    as_of_day: date | None = None,
) -> bool:
    """A snapshot may only feed derived evidence when it self-certifies."""

    if not bool(
        payload.get("status") == "passed"
        and payload.get("evidence_eligible") is True
        and payload.get("pit_eligible") is True
        and not (payload.get("reason_codes") or [])
        and not (payload.get("degraded_reasons") or [])
    ):
        return False
    payload_day = _parse_iso_day(payload.get("target_date"))
    if expected_day is not None and payload_day != expected_day:
        return False
    if as_of_day is not None and (payload_day is None or payload_day > as_of_day):
        return False
    return True


def _ranks_by_sector(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("sector_rankings")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sector = str(row.get("sector") or row.get("sector_name") or "").strip()
        rank = _finite(row.get("current_rank"))
        if not sector or rank is None:
            continue
        out[sector] = {
            "current_rank": int(rank),
            "rotation_strength": _finite(row.get("rotation_strength")),
            "observed_pct_chg": _finite(row.get("observed_pct_chg")),
            "amount": _finite(row.get("amount")),
            "leading_stock": str(row.get("leading_stock") or "").strip(),
        }
    return out


def build_pit_sector_status(
    *,
    as_of: str,
    history_dir: Path | str = SECTOR_HISTORY_DIR,
    windows: tuple[int, ...] = (5, 20, 60),
) -> dict[str, Any]:
    """Derive per-sector status from archived PIT snapshots, fail-closed."""

    history = Path(history_dir)
    requested_windows = tuple(sorted({int(w) for w in windows if int(w) > 0}))
    payload: dict[str, Any] = {
        "ok": False,
        "status": "blocked",
        "task_type": TASK_TYPE,
        "schema_version": SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "generated_at": now_text(),
        "as_of": str(as_of)[:10],
        "target_date": "",
        "network": False,
        "history_dir": "data/sector_history",
        "requested_windows": list(requested_windows),
        "windows_eligible": [],
        "windows_blocked": list(requested_windows),
        "pit_depth": 0,
        "pit_depth_unit": "archived_snapshot_dates",
        "membership_source": "forward_pit_sector_snapshot",
        "sector_membership": {"status": "pit", "used_for_formal_fit": False},
        "pit_universe": True,
        "sector_rankings": [],
        "sector_count": 0,
        "derivation": {},
        "source_paths": [],
        "reason_codes": [],
        "degraded_reasons": [],
        "warnings": [],
        "evidence_eligible": False,
        "pit_eligible": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }

    days = _snapshot_dates(history, as_of=as_of)
    payload["pit_depth"] = len(days)
    if not days:
        payload["reason_codes"] = ["pit_sector_snapshots_missing"]
        payload["degraded_reasons"] = ["pit_sector_snapshots_missing"]
        return payload

    latest_day = days[-1]
    latest = _load_snapshot(history, latest_day)
    as_of_day = _parse_iso_day(as_of)
    if latest is None or not _eligible_snapshot(latest):
        payload["target_date"] = latest_day.isoformat()
        payload["reason_codes"] = ["pit_sector_latest_snapshot_not_eligible"]
        payload["degraded_reasons"] = ["pit_sector_latest_snapshot_not_eligible"]
        return payload
    if not _eligible_snapshot(
        latest,
        expected_day=latest_day,
        as_of_day=as_of_day,
    ):
        payload["target_date"] = str(latest.get("target_date") or latest_day.isoformat())[:10]
        payload["reason_codes"] = ["pit_sector_latest_snapshot_date_mismatch"]
        payload["degraded_reasons"] = ["pit_sector_latest_snapshot_date_mismatch"]
        return payload

    payload["target_date"] = str(latest.get("target_date") or latest_day.isoformat())[:10]
    latest_ranks = _ranks_by_sector(latest)
    if not latest_ranks:
        payload["reason_codes"] = ["pit_sector_rankings_missing"]
        payload["degraded_reasons"] = ["pit_sector_rankings_missing"]
        return payload

    source_paths = [f"data/sector_history/{latest_day.isoformat()}/snapshot.json"]

    # Which windows does the archive actually support? Depth is counted in
    # archived dates, so a gap in the archive shortens depth honestly.
    eligible_windows: list[int] = []
    blocked_windows: list[int] = []
    lookback_ranks: dict[int, dict[str, dict[str, Any]]] = {}
    for window in requested_windows:
        needed = WINDOW_DEPTH_REQUIRED.get(window, window + 1)
        if len(days) < needed:
            blocked_windows.append(window)
            continue
        prior_day = days[-needed]
        prior = _load_snapshot(history, prior_day)
        if prior is None or not _eligible_snapshot(
            prior,
            expected_day=prior_day,
            as_of_day=as_of_day,
        ):
            blocked_windows.append(window)
            continue
        prior_ranks = _ranks_by_sector(prior)
        if not prior_ranks:
            blocked_windows.append(window)
            continue
        lookback_ranks[window] = prior_ranks
        eligible_windows.append(window)
        source_paths.append(f"data/sector_history/{prior_day.isoformat()}/snapshot.json")

    payload["windows_eligible"] = eligible_windows
    payload["windows_blocked"] = blocked_windows
    payload["source_paths"] = source_paths

    if not eligible_windows:
        payload["sector_count"] = len(latest_ranks)
        payload["reason_codes"] = ["pit_sector_depth_insufficient"]
        payload["degraded_reasons"] = ["pit_sector_depth_insufficient"]
        payload["derivation"] = {
            "pit_depth": len(days),
            "window_depth_required": {str(k): v for k, v in WINDOW_DEPTH_REQUIRED.items()},
            "note": "no window had sufficient archived depth; metrics omitted rather than estimated",
        }
        return payload

    sector_total = float(len(latest_ranks)) or 1.0
    rankings: list[dict[str, Any]] = []
    for sector, current in sorted(latest_ranks.items(), key=lambda item: item[1]["current_rank"]):
        row: dict[str, Any] = {
            "sector": sector,
            "sector_name": sector,
            "current_rank": current["current_rank"],
            "rotation_strength": current["rotation_strength"],
            "observed_pct_chg": current["observed_pct_chg"],
            "amount": current["amount"],
            "leading_stock": current["leading_stock"],
        }
        primary_change: float | None = None
        for window in eligible_windows:
            prior = lookback_ranks[window].get(sector)
            if prior is None:
                # Sector absent from the earlier capture: a rank change cannot be
                # computed without inventing a prior position.
                continue
            change = float(prior["current_rank"] - current["current_rank"])
            row[f"rank_change_{window}d"] = change
            if window == min(eligible_windows):
                primary_change = change
        if primary_change is not None:
            score = max(-1.0, min(1.0, primary_change / sector_total))
            heat = _heat_status(score)
            row["heat_score"] = round(score, 6)
            row["heat_status"] = heat
            row["sector_status"] = _HEAT_TO_SECTOR.get(heat, "neutral")
            row["status"] = row["sector_status"]
        rankings.append(row)

    scored = [row for row in rankings if "sector_status" in row]
    payload["sector_rankings"] = rankings
    payload["sector_count"] = len(rankings)
    payload["derivation"] = {
        "pit_depth": len(days),
        "primary_window": min(eligible_windows),
        "windows_eligible": eligible_windows,
        "windows_blocked": blocked_windows,
        "rank_change": "prior_archived_rank - current_archived_rank (positive = moved up)",
        "heat_score": "clip(rank_change_primary_window / observed_sector_count, -1, 1)",
        "heat_status_thresholds": {"hot": ">=0.06", "improving": ">=0.015", "weakening": "<=-0.015", "cold": "<=-0.06"},
        "vocabulary_source": "quantdesk/research/sector_rotation_engine.py",
        "sectors_with_status": len(scored),
        "sectors_without_status": len(rankings) - len(scored),
        "membership_basis": "each date's own same-day capture; no membership carried across dates",
    }

    if not scored:
        payload["reason_codes"] = ["pit_sector_status_underived"]
        payload["degraded_reasons"] = ["pit_sector_status_underived"]
        return payload

    if blocked_windows:
        payload["warnings"] = [
            "pit_sector_window_depth_partial:"
            + ",".join(f"{w}d" for w in blocked_windows)
            + f"_pit_depth={len(days)}"
        ]

    payload["ok"] = True
    payload["status"] = "passed"
    payload["evidence_eligible"] = True
    payload["pit_eligible"] = True
    return payload


def write_pit_sector_status(
    payload: Mapping[str, Any],
    *,
    output_path: Path | str = PIT_SECTOR_STATUS_LATEST_JSON,
) -> Path:
    """Atomically publish the derived artifact under results/."""

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(payload), ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(text + "\n", encoding="utf-8")
    temp.replace(target)
    return target
