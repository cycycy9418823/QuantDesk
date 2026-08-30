"""Round 9S Part A — read-only presentation payload for Forward PIT / T0 UI.

Assembles existing readiness + latest chain archive facts. Never recomputes T0,
never writes canonical/state, never hits the network.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from quantdesk.shared import now_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

READINESS_REL = Path("results") / "data_health" / "round7_forward_pit_readiness_latest.json"
CHAIN_LATEST_REL = Path("results") / "data_chain" / "daily_data_chain_latest.json"
UI_SCHEMA_VERSION = "round9s_forward_pit_ui_readiness_v1"

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / "
    "Not Position Advice. Forward PIT readiness display only; no historical backfill."
)

_BOUNDARY = {
    "research_only": True,
    "paper_account_only": True,
    "no_broker_route": True,
    "live_execution_locked": True,
    "not_investment_advice": True,
    "not_position_advice": True,
    "stage5_closed": True,
    "canonical_write": False,
    "apply_executed": False,
}

_MIN_FORMAL_CHAIN_CONTRACT_VERSION = 10
_FORMAL_TERMINAL_STATUSES = {"completed", "partial", "blocked"}


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"unreadable:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "invalid_payload_type"
    return payload, None


def _milestone_view(readiness: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    label_map = {
        "T0": "T0",
        "T0+4 trading sessions": "T0+4",
        "T0+8 trading sessions": "T0+8",
        "T0+18 trading sessions": "T0+18",
    }
    for entry in readiness.get("milestones") or []:
        if not isinstance(entry, Mapping):
            continue
        rel = str(entry.get("relative_to_t0") or "")
        short = label_map.get(rel, rel or "unknown")
        rows.append(
            {
                "milestone": short,
                "relative_to_t0": rel,
                "calendar_date": entry.get("calendar_date"),
                "status": entry.get("status") or "unknown",
                "purpose": entry.get("purpose") or "",
                "required_consecutive_trading_days": entry.get(
                    "required_consecutive_trading_days"
                ),
            }
        )
    return rows


def _formal_chain_error(
    chain: Mapping[str, Any] | None,
    chain_error: str | None,
) -> str | None:
    if chain is None:
        return f"formal_daily_chain_manifest_{chain_error or 'missing'}"
    if not (
        chain.get("task_type") == "daily_data_chain"
        and chain.get("authoritative") is True
        and chain.get("published_latest") is True
    ):
        return "formal_daily_chain_manifest_not_formal"
    contract_version = chain.get("daily_data_chain_contract_version")
    if (
        not isinstance(contract_version, int)
        or isinstance(contract_version, bool)
        or contract_version < _MIN_FORMAL_CHAIN_CONTRACT_VERSION
    ):
        return "formal_daily_chain_manifest_contract_too_old"
    if str(chain.get("status") or "") not in _FORMAL_TERMINAL_STATUSES:
        return "formal_daily_chain_manifest_not_terminal"

    target_date = str(chain.get("target_date") or "")[:10]
    try:
        date.fromisoformat(target_date)
    except ValueError:
        return "formal_daily_chain_manifest_target_date_invalid"

    universe = chain.get("universe")
    analysis_pool = (
        universe.get("current_analysis_pool")
        if isinstance(universe, Mapping)
        and isinstance(universe.get("current_analysis_pool"), Mapping)
        else {}
    )
    components = chain.get("components")
    archive = (
        components.get("sector_history_archive")
        if isinstance(components, Mapping)
        and isinstance(components.get("sector_history_archive"), Mapping)
        else {}
    )
    declared_dates = (
        analysis_pool.get("target_date"),
        archive.get("target_date"),
    )
    if any(
        isinstance(value, str) and value.strip() and value.strip()[:10] != target_date
        for value in declared_dates
    ):
        return "formal_daily_chain_manifest_target_date_mismatch"
    return None


def _latest_sector_archive(
    chain: Mapping[str, Any] | None,
    formal_chain_error: str | None,
) -> dict[str, Any]:
    if not chain:
        return {
            "status": "unknown",
            "written": None,
            "evidence_eligible": None,
            "pit_eligible": None,
            "target_date": None,
            "reason_codes": ["chain_manifest_missing"],
            "degraded": None,
        }
    if formal_chain_error:
        return {
            "status": "unknown",
            "written": None,
            "evidence_eligible": None,
            "pit_eligible": None,
            "target_date": None,
            "reason_codes": [formal_chain_error],
            "degraded": True,
            "run_id": "",
            "chain_status": str(chain.get("status") or ""),
        }
    components = chain.get("components") if isinstance(chain.get("components"), Mapping) else {}
    archive = (
        components.get("sector_history_archive")
        if isinstance(components.get("sector_history_archive"), Mapping)
        else {}
    )
    reason_codes = [
        str(code)
        for code in (archive.get("reason_codes") or [])
        if str(code).strip()
    ]
    status = str(archive.get("status") or "unknown")
    written = archive.get("written")
    degraded = status == "degraded" or written is False
    if degraded and not reason_codes:
        warnings = chain.get("warnings") if isinstance(chain.get("warnings"), list) else []
        if "sector_history_archive_degraded" in warnings:
            reason_codes = ["sector_history_archive_degraded"]
    return {
        "status": status,
        "written": written,
        "evidence_eligible": archive.get("evidence_eligible"),
        "pit_eligible": archive.get("pit_eligible"),
        "target_date": archive.get("target_date") or chain.get("target_date"),
        "reason_codes": reason_codes,
        "degraded": degraded,
        "run_id": chain.get("run_id") or "",
        "chain_status": chain.get("status") or "",
    }


def _coverage_scope(
    chain: Mapping[str, Any] | None,
    formal_chain_error: str | None,
) -> dict[str, Any]:
    sector_scope = {
        "population": "current_sector_provider_cross_section",
        "sector_count": None,
        "membership_input_count": None,
        "membership_count": None,
        "aligned_membership_count": None,
        "membership_unknown_count": None,
        "membership_board_alignment_ratio": None,
    }
    analysis_scope = {
        "population": "formal_daily_analysis_pool",
        "symbol_count": None,
        "target_date": None,
        "version": None,
    }
    scope = {
        "sector_archive": sector_scope,
        "current_analysis_pool": analysis_scope,
        "cross_scope_ratio_allowed": False,
        "cross_scope_reason": "different_population_counts_not_divisible",
        "degraded": True,
        "reason_code": formal_chain_error or "formal_daily_chain_manifest_not_formal",
    }
    if not chain or formal_chain_error:
        return scope

    components = chain.get("components")
    archive = (
        components.get("sector_history_archive")
        if isinstance(components, Mapping)
        and isinstance(components.get("sector_history_archive"), Mapping)
        else {}
    )
    universe = chain.get("universe")
    analysis_pool = (
        universe.get("current_analysis_pool")
        if isinstance(universe, Mapping)
        and isinstance(universe.get("current_analysis_pool"), Mapping)
        else {}
    )

    count_fields = (
        "sector_count",
        "membership_input_count",
        "membership_count",
        "aligned_membership_count",
        "membership_unknown_count",
    )
    for field in count_fields:
        value = archive.get(field)
        sector_scope[field] = (
            value if isinstance(value, int) and not isinstance(value, bool) else None
        )
    ratio = archive.get("membership_board_alignment_ratio")
    sector_scope["membership_board_alignment_ratio"] = (
        ratio
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
        else None
    )

    symbol_count = analysis_pool.get("symbol_count")
    analysis_scope["symbol_count"] = (
        symbol_count
        if isinstance(symbol_count, int) and not isinstance(symbol_count, bool)
        else None
    )
    for field in ("target_date", "version"):
        value = analysis_pool.get(field)
        analysis_scope[field] = (
            value.strip() if isinstance(value, str) and value.strip() else None
        )

    required_values = [
        *(sector_scope[field] for field in count_fields),
        sector_scope["membership_board_alignment_ratio"],
        analysis_scope["symbol_count"],
        analysis_scope["target_date"],
        analysis_scope["version"],
    ]
    scope["degraded"] = any(value is None for value in required_values)
    scope["reason_code"] = (
        "formal_daily_chain_scope_incomplete" if scope["degraded"] else None
    )
    return scope


def build_forward_pit_ui_readiness(
    project_root: Path,
    *,
    readiness_path: Path | None = None,
    chain_latest_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the UI presentation payload from existing artifacts only."""

    root = Path(project_root).resolve()
    readiness_file = Path(readiness_path or (root / READINESS_REL))
    chain_file = Path(chain_latest_path or (root / CHAIN_LATEST_REL))

    readiness, readiness_err = _read_json(readiness_file)
    chain, chain_err = _read_json(chain_file)
    formal_chain_error = _formal_chain_error(chain, chain_err)
    archive = _latest_sector_archive(chain, formal_chain_error)
    coverage_scope = _coverage_scope(chain, formal_chain_error)

    if readiness is None:
        return {
            "schema_version": UI_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "package_version": PACKAGE_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "generated_at": str(generated_at or now_text()),
            "ok": False,
            "status": "missing" if readiness_err == "missing" else "degraded",
            "reason_code": f"forward_pit_readiness_{readiness_err or 'missing'}",
            "message_for_ui": (
                "Forward PIT readiness 工件缺失：请先运行每日数据链。"
                if readiness_err == "missing"
                else f"Forward PIT readiness 工件不可读：{readiness_err}"
            ),
            "t0": None,
            "t0_display": "unknown",
            "current_eligible_forward_days": 0,
            "milestones": [],
            "latest_sector_archive": archive,
            "coverage_scope": coverage_scope,
            "readiness_path": str(readiness_file),
            "chain_latest_path": str(chain_file),
            "chain_load_error": chain_err,
            "boundaries": dict(_BOUNDARY),
            "disclaimer": DISCLAIMER,
        }

    t0 = readiness.get("t0")
    t0_iso = t0 if isinstance(t0, str) and t0.strip() else None
    days = int(readiness.get("current_eligible_forward_days") or 0)
    status = str(readiness.get("status") or "unknown")
    milestones = _milestone_view(readiness)

    return {
        "schema_version": UI_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "generated_at": str(generated_at or now_text()),
        "ok": True,
        "status": status,
        "t0": t0_iso,
        "t0_display": t0_iso or "unknown",
        "t0_is_trading_day": readiness.get("t0_is_trading_day"),
        "current_eligible_forward_days": days,
        "milestones": milestones,
        "latest_sector_archive": archive,
        "coverage_scope": coverage_scope,
        "readiness_generated_at": readiness.get("generated_at"),
        "readiness_path": str(readiness_file),
        "chain_latest_path": str(chain_file),
        "chain_load_error": chain_err,
        "qualification": readiness.get("qualification") or "",
        "boundaries": dict(_BOUNDARY),
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "build_forward_pit_ui_readiness",
    "READINESS_REL",
    "CHAIN_LATEST_REL",
    "UI_SCHEMA_VERSION",
]
