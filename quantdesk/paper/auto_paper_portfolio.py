"""Persistent local paper portfolio for the auto strategy sandbox.

The portfolio is a local simulation ledger only. It does not connect to
brokers, does not place real orders, and does not constitute investment
advice.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.candidate_contract import (
    candidate_rows_from_strategy_payload,
    normalize_strategy_type,
    strategy_label,
)
from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_candidate_refresh import (
    build_candidate_envelope,
    build_conditional_replay_receipt,
    enrich_legacy_due_plan_candidate_envelopes,
    formal_candidate_strategy_payload,
    load_archived_conditional_plan,
    load_formal_candidate_intake,
    publish_archived_conditional_plan,
)
from quantdesk.data_freshness_gate import (
    evaluate_data_freshness,
    evaluate_paper_review_freshness,
    waiting_decision_review,
    waiting_yesterday_holding_review,
)
from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row
from quantdesk.intraday.intraday_gap_checker import intraday_gap_status
from quantdesk.intraday.intraday_swing_strategy import build_swing_plan
from quantdesk.intraday_execution import simulate_intraday_plan
from quantdesk.board_rules import limit_fill_check
from quantdesk.paper.adjustment_policy import (
    AdjustmentFactorTable,
    apply_paper_adjustment_policy,
    apply_suspension_valuation_policy,
    load_adjustment_factor_table,
)
from quantdesk.paper.conditional_trade_plan import (
    CONDITIONAL_TRADE_PLANS_LATEST_JSON,
    build_conditional_trade_plan,
    conditional_trade_plan_latest,
)
from quantdesk.paper.delist_liquidation import apply_delisting_liquidation
from quantdesk.paper.matching import POLICY_DAILY_APPROX, POLICY_NEXT_OPEN, ensure_declared_fill_policy, policy_header
from quantdesk.paper.market_rules import lot_size_for_symbol, order_quantity_rule, round_down_to_lot
from quantdesk.paper import snapshot_open_fill as open_fill
from quantdesk.paper import strategy_fill_journal as fill_journal
from quantdesk.paper.portfolio_contracts import (
    ACCEPTANCE_CAPITAL,
    ACCEPTANCE_SCENARIO,
    resolve_paper_capital_profile,
)
from quantdesk.paper.position_management_engine import (
    CONDITIONAL_EXECUTION_LATEST_JSON,
    SHADOW_UNRECORDED as _SHADOW_UNRECORDED,
    _write_shadow_ledger_snapshot_once,
    apply_conditional_execution_to_state,
    simulate_conditional_plan_file,
)
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set
from quantdesk.plan_review_receipts import (
    bind_plan_replay_receipt,
    inspect_plan_minute_inputs,
    load_plan_review_snapshot,
    plan_review_snapshot,
    publish_plan_review_receipt,
)
from quantdesk.daily_price_update_engine import infer_target_date
from quantdesk.paper.trade_costs import buy_cash_outflow, compute_trade_costs, sell_cash_inflow
from quantdesk.risk.risk_gate_shadow import (
    build_legacy_blocked_shadow_record,
    evaluate_shadow_risk_order,
    record_shadow_error_best_effort,
    render_shadow_report_section,
    summarize_shadow_records,
    write_shadow_summary,
)
from quantdesk.shared import normalize_symbol, now_text, utc_now_iso
from quantdesk.trading_calendar import load_a_share_trading_calendar
from quantdesk.theme_sector_engine import THEME_MAP_PATH, load_theme_map
from quantdesk.theme_sector_engine import normalize_symbol as normalize_theme_symbol
from quantdesk.update_task_lock import UpdateLockHeld, acquire_update_lock, release_update_lock
from quantdesk.utils import atomic_write_json, read_json_safe, safe_float, safe_int
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
STATE_DIR = PROJECT_ROOT / "state"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"
DAILY_FEATURE_LATEST_CSV = PROJECT_ROOT / "data" / "features" / "daily_features_latest.csv"
LOW_PRICE_UNIVERSE_CSV = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"

AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = RESULTS_DIR / "auto_paper_portfolio_latest.json"
AUTO_PAPER_PORTFOLIO_HISTORY_JSON = RESULTS_DIR / "auto_paper_portfolio_history.json"
SWING_TRADE_PLANS_LATEST_JSON = RESULTS_DIR / "swing_trade_plans_latest.json"
INTRADAY_EXECUTION_LATEST_JSON = RESULTS_DIR / "intraday_execution_latest.json"
# CONDITIONAL_TRADE_PLANS_LATEST_JSON / CONDITIONAL_EXECUTION_LATEST_JSON come from
# their owning modules above (same RESULTS_DIR paths); local duplicates removed (Round 21 F811).
RISK_GATE_SHADOW_LATEST_JSON = RESULTS_DIR / "risk_gate_shadow_latest.json"
RISK_GATE_SHADOW_ERRORS_JSONL = RESULTS_DIR / "risk_gate_shadow_errors.jsonl"
SECTOR_ROTATION_LATEST_JSON = RESULTS_DIR / "sector_rotation_latest.json"


def _formal_batch_strategy_payload(
    batch: dict[str, Any] | None,
    batch_path: Path | str | None,
) -> dict[str, Any]:
    """Project a verified formal batch into the legacy plan-builder shape.

    The projection is deliberately local and lossless: each row carries a
    closed candidate envelope binding the candidate bytes to the dated batch
    path and batch provenance hash.  No mutable ``*_latest.json`` pointer is
    introduced here.
    """

    if not isinstance(batch, dict):
        return {
            "ok": False,
            "generated_at": now_text(),
            "standard_candidates": [],
            "long_candidates": [],
            "board_candidates": [],
        }
    provenance = batch.get("provenance") if isinstance(batch.get("provenance"), dict) else {}
    source_path = str(batch_path or "").strip()
    rows: list[dict[str, Any]] = []
    for raw in batch.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        candidate = copy.deepcopy(raw)
        envelope = candidate.get("candidate_envelope")
        if not isinstance(envelope, dict):
            envelope = build_candidate_envelope(
                batch,
                batch_path=source_path,
                candidate=candidate,
            )
        candidate["candidate_envelope"] = envelope
        rows.append(candidate)
    generated_at = str(provenance.get("captured_at") or batch.get("target_date") or now_text())
    return {
        "ok": batch.get("formal_eligible") is True
        and batch.get("status") in {"passed", "passed_with_warnings"},
        "generated_at": generated_at,
        "standard_candidates": rows,
        "long_candidates": [row for row in rows if str(row.get("strategy_type") or "") == "long"],
        "board_candidates": [row for row in rows if str(row.get("strategy_type") or "") == "board"],
        "formal_candidate_batch": copy.deepcopy(batch),
        "formal_candidate_batch_path": source_path,
    }

DISCLAIMER = "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。"
BROKER_BOUNDARY = "不接券商，不真实下单，不保存账户密码。"
PAPER_ACTION_BOUNDARY = {
    "is_real_order": False,
    "research_only": True,
    "paper_only": True,
    "no_broker": True,
}


def _paper_action_boundary() -> dict[str, bool]:
    return dict(PAPER_ACTION_BOUNDARY)


def _auto_paper_lock_path() -> Path:
    return AUTO_PAPER_PORTFOLIO_STATE_JSON.with_suffix(".lock")


def _auto_paper_state_store() -> PaperStateStore:
    return PaperStateStore(AUTO_PAPER_PORTFOLIO_STATE_JSON, schema_version=API_SCHEMA_VERSION, max_backups=7)


def _normalized_management_state(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    normalized = dict(row)
    management_state = str(normalized.get("management_state") or "").strip()
    watch_status = str(normalized.get("watch_status") or "").strip()
    canonical = management_state or watch_status
    if not canonical:
        canonical = "closed" if str(normalized.get("status") or "").strip() == "closed" else "normal"
    mismatch = bool(management_state and watch_status and management_state != watch_status)
    normalized["management_state"] = canonical
    normalized["watch_status"] = canonical
    return normalized, mismatch


_PAPER_POSITION_NUMERIC_FIELDS = ("quantity", "market_value", "unrealized_pnl")
_PAPER_CLOSED_NUMERIC_FIELDS = (
    "quantity",
    "market_value",
    "unrealized_pnl",
    "realized_pnl",
)
_PAPER_TOP_LEVEL_NUMERIC_FIELDS = (
    "initial_cash",
    "cash",
    "market_value",
    "unrealized_pnl",
    "realized_pnl",
    "total_equity",
    "legacy_realized_adjustment",
)


def _parseable_paper_number(value: Any) -> bool:
    """Reject booleans and non-finite/non-numeric ledger values."""

    return not isinstance(value, bool) and safe_float(value, None) is not None


def _paper_state_shape_errors(state: Any) -> list[str]:
    """Return deterministic shape/value errors without mutating a paper state."""

    if not isinstance(state, dict):
        return ["paper_state_not_object"]

    errors: list[str] = []
    for key, numeric_fields in (
        ("positions", _PAPER_POSITION_NUMERIC_FIELDS),
        ("closed_positions", _PAPER_CLOSED_NUMERIC_FIELDS),
        ("realized_reductions", _PAPER_CLOSED_NUMERIC_FIELDS),
    ):
        if key not in state:
            continue
        rows = state.get(key)
        if not isinstance(rows, list):
            errors.append(f"{key}_not_list")
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"{key}_row_{index}_not_object")
                continue
            for field in numeric_fields:
                if field in row and not _parseable_paper_number(row.get(field)):
                    errors.append(f"{key}_row_{index}_{field}_not_numeric")

    for field in _PAPER_TOP_LEVEL_NUMERIC_FIELDS:
        if field in state and not _parseable_paper_number(state.get(field)):
            errors.append(f"{field}_not_numeric")
    return errors


def reconcile_paper_account(state: dict[str, Any]) -> dict[str, Any]:
    """Return a detail-derived paper-account state without mutating the input.

    Historical ledgers may contain a realized-PnL amount that differs from the
    sum of closed-position rows.  That difference is retained explicitly as a
    legacy adjustment; all other account totals are rebuilt from current detail.
    """

    invalid_reason_codes = _paper_state_shape_errors(state)
    reconciled = copy.deepcopy(state if isinstance(state, dict) else {})
    positions: list[dict[str, Any]] = []
    status_mismatches: list[str] = []
    raw_positions = reconciled.get("positions")
    if not isinstance(raw_positions, list):
        raw_positions = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            continue
        row, mismatch = _normalized_management_state(raw)
        positions.append(row)
        if mismatch:
            status_mismatches.append(normalize_symbol(row.get("symbol")) or "unknown")

    raw_closed_positions = reconciled.get("closed_positions")
    if not isinstance(raw_closed_positions, list):
        raw_closed_positions = []
    closed_positions = [dict(row) for row in raw_closed_positions if isinstance(row, dict)]
    raw_reductions = reconciled.get("realized_reductions")
    if not isinstance(raw_reductions, list):
        raw_reductions = []
    realized_reductions = [dict(row) for row in raw_reductions if isinstance(row, dict)]
    cash = safe_float(reconciled.get("cash"), 0.0) or 0.0
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in positions)
    unrealized_pnl = sum(safe_float(row.get("unrealized_pnl"), 0.0) or 0.0 for row in positions)
    closed_realized_pnl = sum(
        safe_float(row.get("realized_pnl"), 0.0) or 0.0
        for row in closed_positions
    )
    partial_realized_pnl = sum(
        safe_float(row.get("realized_pnl"), 0.0) or 0.0
        for row in realized_reductions
    )
    detail_realized_pnl = closed_realized_pnl + partial_realized_pnl
    explicit_adjustment = safe_float(reconciled.get("legacy_realized_adjustment"), None)
    if explicit_adjustment is None:
        historical_realized = safe_float(reconciled.get("realized_pnl"), None)
        explicit_adjustment = (
            historical_realized - detail_realized_pnl
            if historical_realized is not None
            else 0.0
        )

    totals = {
        "market_value": round(market_value, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl": round(detail_realized_pnl + explicit_adjustment, 2),
        "total_equity": round(cash + market_value, 2),
    }
    drift_fields = [
        key
        for key, value in totals.items()
        if key in reconciled
        and abs((safe_float(reconciled.get(key), 0.0) or 0.0) - value) > 0.01
    ]

    warnings = [str(item) for item in reconciled.get("warnings") or []]
    if drift_fields:
        warnings.extend(
            [
                "paper_account_top_level_drift_detected",
                f"paper_account_top_level_drift_fields:{','.join(drift_fields)}",
            ]
        )
    if status_mismatches:
        warnings.append(
            "paper_position_status_mismatch_reconciled:"
            + ",".join(sorted(set(status_mismatches)))
        )
    if abs(explicit_adjustment) > 0.005:
        warnings.append("paper_account_legacy_realized_adjustment_preserved")
    if invalid_reason_codes:
        warnings.append("paper_account_invalid")
        warnings.extend(f"paper_account_invalid:{reason}" for reason in invalid_reason_codes)

    reconciled.update(totals)
    reconciled["cash"] = round(cash, 2)
    reconciled["positions"] = positions
    reconciled["closed_positions"] = closed_positions
    reconciled["realized_reductions"] = realized_reductions
    reconciled["position_count"] = len(positions)
    reconciled["legacy_realized_adjustment"] = round(explicit_adjustment, 2)
    reconciled["warnings"] = list(dict.fromkeys(warnings))
    reconciled["accounting_reconciliation"] = {
        "status": (
            "invalid"
            if invalid_reason_codes
            else "reconciled"
            if drift_fields or status_mismatches
            else "consistent"
        ),
        "reason": invalid_reason_codes[0] if invalid_reason_codes else "",
        "invalid_reason_codes": invalid_reason_codes,
        "detail_market_value": totals["market_value"],
        "detail_unrealized_pnl": totals["unrealized_pnl"],
        "closed_realized_pnl": round(closed_realized_pnl, 2),
        "partial_realized_pnl": round(partial_realized_pnl, 2),
        "legacy_realized_adjustment": round(explicit_adjustment, 2),
        "drift_fields": drift_fields,
        "status_mismatch_symbols": sorted(set(status_mismatches)),
    }
    return reconciled


def _write_auto_paper_state(state: dict[str, Any]) -> dict[str, Any]:
    ensure_declared_fill_policy(state, policy_id=_declared_strategy_policy(state))
    return _auto_paper_state_store().write(reconcile_paper_account(state))


def _declared_strategy_policy(state: dict[str, Any]) -> str:
    return (POLICY_NEXT_OPEN if state.get("strategy_fill_mode") == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT
            else POLICY_DAILY_APPROX)


def _write_auto_paper_latest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist a canonical latest snapshot without mutating the source state.

    A snapshot is assembled with its money detail under ``summary``; without
    promoting the missing top-level fields here, ``reconcile_paper_account``
    would read absent ``cash`` as zero and publish a cash/equity that disagrees
    with the real ledger and the summary it carries.
    """

    prepared = dict(snapshot)
    summary = prepared.get("summary")
    if isinstance(summary, dict):
        for key in ("cash", "market_value", "total_equity", "realized_pnl", "unrealized_pnl", "initial_cash"):
            if prepared.get(key) is None and summary.get(key) is not None:
                prepared[key] = summary[key]
    if _SHADOW_UNRECORDED:
        # Shadow-risk failures that could not reach their own sink (used to be
        # `except Exception: pass`). Published here, never in the ledger state.
        unrecorded = list(dict.fromkeys(_SHADOW_UNRECORDED))
        prepared["shadow_warnings"] = list(dict.fromkeys([*(prepared.get("shadow_warnings") or []), *unrecorded]))
        prepared["warnings"] = list(dict.fromkeys([*(prepared.get("warnings") or []), *unrecorded]))
    normalized = reconcile_paper_account(prepared)
    atomic_write_json(AUTO_PAPER_PORTFOLIO_LATEST_JSON, normalized)
    return normalized


def _load_auto_adjustment_factors(symbols: list[Any]) -> AdjustmentFactorTable:
    try:
        if Path(DAILY_DIR).resolve() != (PROJECT_ROOT / "data" / "daily").resolve():
            return AdjustmentFactorTable(warnings=["adjustment_factor_metadata_skipped_for_non_project_daily_dir"])
    except OSError:
        return AdjustmentFactorTable(warnings=["adjustment_factor_metadata_skipped_for_non_project_daily_dir"])
    return load_adjustment_factor_table(PROJECT_ROOT / "data" / "metadata", symbols=symbols)


def _auto_paper_lock_failed_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "exists": AUTO_PAPER_PORTFOLIO_STATE_JSON.exists(),
        "error": "auto_paper_portfolio_locked",
        "failed_reason": f"自动虚拟组合状态文件正被另一个任务写入，已拒绝本次写入以避免覆盖：{exc}",
        "state_path": str(AUTO_PAPER_PORTFOLIO_STATE_JSON),
        "lock_path": str(_auto_paper_lock_path()),
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
    }
    if isinstance(exc, UpdateLockHeld):
        payload["lock_info"] = dict(exc.lock_info)
    return payload


def _is_unknown_theme_value(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "nan", "none", "null", "-", "未知", "未知板块", "未知题材", "未知行业", "未知概念"}


def _enrich_positions_with_theme_map(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        mapping = load_theme_map(THEME_MAP_PATH)
    except Exception:
        mapping = {}
    if not mapping:
        return positions
    enriched: list[dict[str, Any]] = []
    for item in positions:
        row = dict(item)
        mapped = mapping.get(normalize_theme_symbol(row.get("symbol")))
        if mapped:
            if _is_unknown_theme_value(row.get("sector")) and not _is_unknown_theme_value(mapped.get("sector")):
                row["sector"] = mapped.get("sector")
            if _is_unknown_theme_value(row.get("industry")) and not _is_unknown_theme_value(mapped.get("industry")):
                row["industry"] = mapped.get("industry")
            if _is_unknown_theme_value(row.get("primary_theme")) and not _is_unknown_theme_value(mapped.get("primary_theme")):
                row["primary_theme"] = mapped.get("primary_theme")
            if _is_unknown_theme_value(row.get("theme_tags")) and not _is_unknown_theme_value(mapped.get("theme_tags")):
                row["theme_tags"] = mapped.get("theme_tags")
            if _is_unknown_theme_value(row.get("concept_tags")) and not _is_unknown_theme_value(mapped.get("concept_tags")):
                row["concept_tags"] = mapped.get("concept_tags")
        enriched.append(row)
    return enriched


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _conditional_plan_path() -> Path:
    configured = CONDITIONAL_TRADE_PLANS_LATEST_JSON
    return configured if configured.parent == RESULTS_DIR else RESULTS_DIR / "conditional_trade_plans_latest.json"


def _archive_project_root() -> Path:
    """归档读写用的根目录 —— 必须与本模块其他产物路径同源。

    `publish_archived_conditional_plan` / `load_archived_conditional_plan` 只接受
    一个 root，自己拼出 `results/conditional_trade_plans_daily/`。此处原先直接传
    模块级 `PROJECT_ROOT`，而 `RESULTS_DIR` 是测试会重定向的那一个：于是测试
    在临时目录里断言，发布器却读写**真实仓库**的 `results/`。它今天没造成损坏
    只是因为归档是幂等的、且测试计划的 trade_date 更旧而被
    `conditional_plan_latest_regression` 拦住 —— 换个日期顺序就会覆盖真实证据。

    从 `RESULTS_DIR` 反推根目录，让重定向对归档同样生效。
    """

    return RESULTS_DIR.parent


def _attach_bound_replay_review(
    payload: dict[str, Any], *, source_plan: dict[str, Any] | None,
    replay_receipt: dict[str, Any] | None, input_evidence: list[dict[str, Any]] | None,
    applied_ids: list[str], dry_run: bool,
) -> None:
    """Keep the v1 rollover receipt and separately archive plan-bound evidence."""
    if source_plan is None or replay_receipt is None:
        return
    root = _archive_project_root()
    try:
        original = load_archived_conditional_plan(root, trade_date=str(source_plan.get("trade_date") or ""))
        if original.get("status") != "passed" or original.get("plan") != source_plan:
            payload["plan_replay_receipt"] = {"status": "legacy_unbound", "reason_codes": ["replay_source_archive_not_proven"]}
            return
        checked_at = utc_now_iso()
        bound = bind_plan_replay_receipt(
            root, source_plan, replay_receipt, checked_at=checked_at,
            intraday_dir=INTRADAY_DIR / "1m", input_evidence=input_evidence,
        )
        payload["plan_replay_receipt"] = bound
        archive_reasons: list[str] = []
        if not dry_run:
            archived = publish_plan_review_receipt(root, bound, plan=source_plan)
            payload["plan_replay_receipt_archive"] = archived
            if archived.get("status") not in {"completed", "idempotent"}:
                archive_reasons = list(archived.get("reason_codes") or ["plan_replay_receipt_archive_failed"])
                payload.setdefault("warnings", []).extend(archive_reasons)
        snapshot = plan_review_snapshot(
            root, trade_date=str(source_plan["trade_date"]), checked_at=checked_at,
            applied_execution_ids=applied_ids, simulation_enabled=True,
            write=not dry_run, review_current=False,
            extra_replay_receipts=[bound] if dry_run else [],
            additional_reason_codes=archive_reasons,
        )
        payload.update(snapshot)
    except Exception as exc:  # noqa: BLE001 - preserve a failed evidence step
        reason = f"plan_replay_receipt_failed:{type(exc).__name__}:{exc}"
        payload["plan_replay_receipt"] = {"status": "blocked", "reason_codes": [reason]}
        payload.setdefault("warnings", []).append(reason)


def _conditional_execution_path() -> Path:
    configured = CONDITIONAL_EXECUTION_LATEST_JSON
    return configured if configured.parent == RESULTS_DIR else RESULTS_DIR / "conditional_execution_latest.json"


def _risk_gate_shadow_path() -> Path:
    configured = RISK_GATE_SHADOW_LATEST_JSON
    return configured if configured.parent == RESULTS_DIR else RESULTS_DIR / "risk_gate_shadow_latest.json"


def _risk_gate_shadow_error_path() -> Path:
    configured = RISK_GATE_SHADOW_ERRORS_JSONL
    return configured if configured.parent == RESULTS_DIR else RESULTS_DIR / "risk_gate_shadow_errors.jsonl"


def _sector_rotation_path() -> Path:
    configured = SECTOR_ROTATION_LATEST_JSON
    legacy = configured if configured.parent == RESULTS_DIR else RESULTS_DIR / "sector_rotation_latest.json"
    # Prefer the PIT-derived sector status when it exists. The research rotation
    # artifact is permanently evidence-ineligible by construction (it declares
    # static_sector_map_proxy_non_pit), so on its own the smart-stop component
    # can never assemble. The PIT artifact is built from archived same-day
    # sector captures and self-certifies only when archived depth supports the
    # window it reports; when it is absent or degraded the risk gate reads the
    # legacy artifact exactly as before and keeps failing closed.
    pit = RESULTS_DIR / "pit_sector_status_latest.json"
    return pit if pit.is_file() else legacy


def _record_shadow_failure(
    records: list[dict[str, Any]],
    *,
    surface: str,
    error: BaseException,
    as_of: str,
    write_error: bool = True,
) -> None:
    """Keep a shadow failure out of every legacy paper action -- but never lose it.

    The failure is recorded in the shadow records (summary, report, latest);
    a failure to append it or to write the error log is noted in
    ``_SHADOW_UNRECORDED`` and published as ``shadow_warnings``, not swallowed.
    The ledger state bytes stay untouched either way.
    """

    record = {
        "evaluated": False,
        "status": "shadow_error",
        "surface": surface,
        "as_of": as_of,
        "reason_codes": ["shadow_error"],
        "shadow_error": f"{type(error).__name__}:{error}",
        "would_pass": False,
        "would_block": False,
        "insufficient_evidence": False,
        "missing_components": [],
    }
    try:
        records.append(record)
    except Exception as append_exc:  # noqa: BLE001 - surfaced below
        _SHADOW_UNRECORDED.append(
            f"shadow_record_append_failed:{surface}:{type(error).__name__}:{type(append_exc).__name__}"
        )
    if write_error:
        try:
            record_shadow_error_best_effort(
                surface=surface,
                error=error,
                as_of=as_of,
                error_path=_risk_gate_shadow_error_path(),
            )
        except Exception as log_exc:  # noqa: BLE001 - surfaced below
            record["shadow_error_log_failed"] = f"{type(log_exc).__name__}:{log_exc}"
            _SHADOW_UNRECORDED.append(f"shadow_error_log_write_failed:{surface}:{type(log_exc).__name__}")


def _load_auto_strategy(path: Path = AUTO_STRATEGY_LATEST_JSON) -> dict[str, Any]:
    data = read_json_safe(path, {})
    return data if isinstance(data, dict) else {}


def _candidate_price(row: dict[str, Any]) -> float | None:
    source = row.get("source_item") if isinstance(row.get("source_item"), dict) else {}
    for candidate in (row, source):
        for key in ("close", "latest_price", "price", "entry_price"):
            number = safe_float(candidate.get(key), None)
            if number is not None and number > 0:
                return number
    planned_amount = safe_float(row.get("planned_amount"), None)
    quantity = safe_float(row.get("quantity"), None)
    if planned_amount is not None and quantity and quantity > 0:
        return planned_amount / quantity
    return None


def _candidate_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("symbol") or "")


def _is_long_strategy(row: dict[str, Any]) -> bool:
    strategy_type = normalize_strategy_type(row.get("strategy_type"), "long")
    return strategy_type in {"long", "long_swing"}


def _read_daily_bars(symbol: str) -> pd.DataFrame:
    path = DAILY_DIR / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = read_csv_frame(path, dtype={"symbol": str})
    rename = {
        "日期": "date",
        "收盘": "close",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
    }
    df = df.rename(columns={key: value for key, value in rename.items() if key in df.columns and value not in df.columns})
    # Classify the original frame before any date parse/sort/dedup or numeric
    # coercion, so a mixed/unknown basis cannot be silently treated as raw.
    try:
        basis, basis_reason = classify_frame_price_basis(df)
    except Exception:  # noqa: BLE001 - classification must never break a reader
        basis, basis_reason = "unknown", "unknown_price_basis"
    df.attrs["price_basis"] = basis
    df.attrs["price_basis_reason"] = basis_reason
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if "close" in df.columns:
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def _last_tradable_close(df: pd.DataFrame) -> float | None:
    """Most recent close whose row is a genuine trading bar (not a suspension)."""

    for _, row in df.iloc[::-1].iterrows():
        ok, _reason = daily_bar_validity_from_row(row.to_dict())
        if ok:
            price = safe_float(row.get("close"), None)
            if price is not None and price > 0:
                return price
    return None


def _previous_row_close(valid: pd.DataFrame) -> float | None:
    if valid is None or len(valid) < 2:
        return None
    value = safe_float(valid.iloc[-2].get("close"), None)
    return value if value is not None and value > 0 else None


def _quote_limit_base(quote: dict[str, Any]) -> tuple[float | None, str]:
    """Previous close the exchange used for today's limit (vendor evidence first)."""

    explicit = safe_float(quote.get("pre_close"), None)
    if explicit is not None and explicit > 0:
        return explicit, "daily_pre_close"
    close = safe_float(quote.get("close"), None)
    change = safe_float(quote.get("change"), None)
    if close is not None and close > 0 and change is not None and close - change > 0:
        return round(close - change, 4), "daily_close_minus_change"
    previous = safe_float(quote.get("previous_row_close"), None)
    if previous is not None and previous > 0:
        return previous, "previous_row_close"
    return None, "missing"


def _latest_daily_quote(symbol: str, *, decision_date: str | None = None) -> dict[str, Any]:
    df = _read_daily_bars(symbol)
    warnings = [str(item) for item in df.attrs.get("warnings", [])]
    code = normalize_symbol(symbol)
    if df.empty or "close" not in df.columns:
        warnings.append(f"missing_daily_quote:{code}")
        return {
            "price": None,
            "date": "",
            "source": "missing_daily",
            "missing": True,
            "warnings": warnings,
        }
    valid = df.dropna(subset=["close"])
    if valid.empty:
        warnings.append(f"missing_close:{code}")
        return {
            "price": None,
            "date": "",
            "source": "missing_close",
            "missing": True,
            "warnings": warnings,
        }
    row = valid.iloc[-1]
    date_value = row.get("date", "")
    if pd.notna(date_value):
        try:
            date_text = pd.Timestamp(date_value).strftime("%Y-%m-%d")
        except Exception:
            date_text = str(date_value)[:10]
    else:
        date_text = ""
    price = safe_float(row.get("close"), None)
    quote: dict[str, Any] = {
        "price": price,
        "date": date_text,
        "source": "daily_cache",
        "warnings": warnings,
        "price_basis": str(df.attrs.get("price_basis") or ""),
        "price_basis_reason": str(df.attrs.get("price_basis_reason") or ""),
        # Raw bar fields let a *fill* decision distinguish a genuine tradeable
        # session from a zero-volume suspension or a one-word limit board. They
        # are evidence about today's bar, never a substitute for a fill.
        "open": safe_float(row.get("open"), None),
        "high": safe_float(row.get("high"), None),
        "low": safe_float(row.get("low"), None),
        "close": safe_float(row.get("close"), None),
        "volume": safe_float(row.get("volume"), None),
        "pct_chg": safe_float(row.get("pct_chg"), None),
        # Limit-price base evidence: the vendor's own previous close (close -
        # change), else the previous row's close. Kept apart so an ex-rights
        # day (the two disagree) is visible instead of silently mixed.
        "pre_close": safe_float(row.get("pre_close"), None),
        "change": safe_float(row.get("change"), None),
        "previous_row_close": _previous_row_close(valid),
    }
    basis_reason = str(df.attrs.get("price_basis_reason") or "")
    if basis_reason:
        warnings.append(
            f"price_basis_untrusted:{code}:{quote['price_basis'] or 'unknown'}:{basis_reason}"
        )
        if basis_reason == "mixed_price_basis":
            quote["price_basis_untrusted"] = True
    # A zero-volume flat bar is how vendors encode "suspended today" (see
    # quantdesk/data_truth/daily_bar_validity.py). Mark it so the valuation
    # policy freezes at the last genuinely tradable price instead of treating a
    # non-trading row as a fresh mark.
    ok, reason = daily_bar_validity_from_row(row.to_dict())
    if not ok and "volume_not_positive" in reason:
        last_tradable = _last_tradable_close(df)
        if last_tradable is not None:
            quote["price"] = last_tradable
            quote["last_tradable_price"] = last_tradable
        quote.update(
            {
                "suspended": True,
                "is_suspended": True,
                "suspend_type": "zero_volume_flat",
                "suspend_reason": reason,
            }
        )
        warnings.append(f"suspended_daily_bar:{code}:{date_text}")
    if decision_date and date_text and str(decision_date)[:10] > date_text:
        quote["stale"] = True
        warnings.append(f"stale_daily_quote:{code}:{date_text}->{str(decision_date)[:10]}")
    return quote


def _quote_warnings(symbol: Any, quote: dict[str, Any]) -> list[str]:
    code = normalize_symbol(symbol)
    return [f"{code}: {warning}" for warning in quote.get("warnings") or []]


def _daily_price_basis(symbol: Any) -> tuple[str, str]:
    """Price basis of the symbol's original daily frame: ``(basis, reason)``.

    Mirrors the AI book's evidence requirement: a fill may not be booked when
    the local daily frame is ``mixed``/``unknown``. A known basis (raw/qfq/hfq)
    is returned with an empty reason.
    """

    df = _read_daily_bars(str(symbol or ""))
    return (
        str(df.attrs.get("price_basis") or ""),
        str(df.attrs.get("price_basis_reason") or ""),
    )


def _basis_fill_rejection(symbol: Any) -> str:
    """Human reason when the symbol's price basis must block a paper fill."""

    basis, reason = _daily_price_basis(symbol)
    if not reason:
        return ""
    return (
        f"价格口径不可用于成交（{reason or 'unknown_price_basis'}"
        f"{'; basis=' + basis if basis else ''}），拒绝模拟买入。"
    )


def _fill_tradability_rejection(
    quote: dict[str, Any],
    side: str,
    *,
    require_quote: bool = False,
    fill_price: Any = None,
    symbol: Any = "",
    name: Any = None,
) -> str:
    """Reason this bar cannot support a real paper fill, else ``""``.

    Mirrors the AI book's fill reality: a book entry needs today's *tradeable*
    bar, not a stale carried valuation. Zero-volume suspension and a one-word
    limit board (up for a buy, down for a sell) cannot be filled at the open.
    A missing quote only blocks when ``require_quote`` (exits must not fall back
    to yesterday's mark); buys may still price off the committed candidate.
    """

    label = "买入" if side == "buy" else "卖出"
    price = safe_float(quote.get("price"), None)
    if require_quote and (quote.get("missing") or price is None or price <= 0):
        return f"缺少当日成交证据（{quote.get('source') or 'missing'}），拒绝模拟{label}。"
    volume = safe_float(quote.get("volume"), None)
    if quote.get("suspended") or quote.get("is_suspended") or (volume is not None and volume <= 0):
        return f"当日零成交量（停牌或无成交），无法确认成交，拒绝模拟{label}。"
    basis_reason = str(quote.get("price_basis_reason") or "")
    if basis_reason in {"mixed_price_basis", "unknown_price_basis"} or quote.get("price_basis_untrusted"):
        return f"价格口径不可用于成交（{basis_reason or 'unknown_price_basis'}），拒绝模拟{label}。"
    # The booked price is compared with the cent-rounded limit price of the
    # quote's own session (shared rule in quantdesk.board_rules). The old check
    # only caught a one-word board with pct >= 9.8, so a close sealed at the
    # limit after trading opened -- and every low-priced limit (+9.6%) -- was
    # booked as an ordinary fill.
    booked = safe_float(fill_price, None) or safe_float(quote.get("close"), None) or price
    base, base_source = _quote_limit_base(quote)
    check = limit_fill_check(
        symbol, side, price=booked, trade_date=quote.get("date") or None,
        pre_close=base, pct_chg=quote.get("pct_chg"), name=name,
    )
    if check["ok"]:
        return ""
    limits = check["limits"]
    code = str(check["code"])
    if code == "limit_up":
        return (f"成交价 {booked:.2f} 已到当日涨停价 {limits.get('up_limit')}（前收 {limits.get('pre_close')}，"
                f"{base_source}），涨停不买，拒绝模拟买入。")
    if code == "limit_down":
        return (f"成交价 {booked:.2f} 已到当日跌停价 {limits.get('down_limit')}（前收 {limits.get('pre_close')}，"
                f"{base_source}），跌停不卖，拒绝模拟卖出。")
    if code == "price_outside_limit_band":
        return (f"成交价 {booked:.2f} 不在当日涨跌停区间 {limits.get('down_limit')}–{limits.get('up_limit')} 内，"
                f"价格证据矛盾，拒绝模拟{label}。")
    return f"缺少前收与涨跌幅，算不出当日涨跌停价（{check['reason']}），缺数据不成交，拒绝模拟{label}。"


def _read_intraday_bars(symbol: str, source: str = "1m") -> pd.DataFrame | None:
    path = INTRADAY_DIR / source / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return None
    frame = read_csv_frame(path, dtype={"symbol": str})
    return frame if not frame.empty else None


def _daily_plan_date(daily_df: pd.DataFrame) -> str:
    if daily_df is None or daily_df.empty:
        return ""
    if "date" in daily_df.columns:
        return str(daily_df["date"].iloc[-1])[:10]
    return ""


def _intraday_trade_date(intraday_df: pd.DataFrame | None) -> str:
    if intraday_df is None or intraday_df.empty:
        return ""
    for key in ("datetime", "time", "date"):
        if key in intraday_df.columns:
            return str(intraday_df[key].iloc[0])[:10]
    return ""


def _swing_action_from_execution(
    *,
    execution: dict[str, Any],
    row: dict[str, Any],
    decision_date: str,
    fallback_action: str,
) -> dict[str, Any]:
    symbol = normalize_symbol(row.get("symbol"))
    action = execution.get("action") if execution.get("triggered") else "no_fill"
    if action == "none":
        action = "no_fill"
    label_map = {
        "buy": "盘中触发模拟买入",
        "sell": "盘中触发模拟卖出",
        "no_fill": "盘中计划未触发",
    }
    return {
        "action": action,
        "action_label": label_map.get(action, "盘中计划"),
        "symbol": symbol,
        "name": _candidate_name(row),
        "strategy_type": normalize_strategy_type(row.get("strategy_type"), "long"),
        "strategy_label": strategy_label(row.get("strategy_type")),
        "sector": row.get("sector") or "未知板块",
        "primary_theme": row.get("primary_theme") or "未知题材",
        "quantity": safe_int(execution.get("quantity"), 0) or 0,
        "price": execution.get("trigger_price"),
        "decision_date": decision_date,
        "trigger_time": execution.get("trigger_time"),
        "trigger_price": execution.get("trigger_price"),
        "trigger_type": execution.get("trigger_type") or fallback_action,
        "is_reliable": bool(execution.get("is_reliable")),
        "reason": execution.get("reason") or "缺少盘中数据，无法确认计划价格是否真实出现",
        "order_source": "auto_strategy_sandbox",
        "execution_source": "intraday_swing",
        # Round 21: swing is a third fill path — force daily_approx provenance on ledger actions.
        "fill_model": str(execution.get("fill_model") or "daily_approx"),
        "fill_model_provenance_complete": True,
        **_paper_action_boundary(),
    }


def _simulate_swing_for_row(
    row: dict[str, Any],
    *,
    holding: bool,
    quantity: int,
    intraday_source: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    symbol = normalize_symbol(row.get("symbol"))
    daily_df = _read_daily_bars(symbol)
    plan = build_swing_plan(
        symbol,
        daily_df,
        name=_candidate_name(row),
        plan_date=_daily_plan_date(daily_df),
        trade_date="",
        quantity=quantity,
    )
    intraday_df = _read_intraday_bars(symbol, intraday_source)
    trade_date = _intraday_trade_date(intraday_df)
    if trade_date:
        plan["trade_date"] = trade_date
        plan["valid_for_date"] = trade_date
    if not plan.get("ok"):
        execution = {
            "symbol": symbol,
            "name": _candidate_name(row),
            "plan_date": plan.get("plan_date", ""),
            "trade_date": plan.get("trade_date", ""),
            "triggered": False,
            "action": "none",
            "quantity": quantity,
            "is_reliable": False,
            "reason": plan.get("reason") or "日线数据不足，无法生成长线波段计划",
            "disclaimer": DISCLAIMER,
        }
        return plan, execution
    execution = simulate_intraday_plan(plan, intraday_df, holding=holding)
    execution.update({
        "name": _candidate_name(row),
        "quantity": quantity if execution.get("quantity") is None else execution.get("quantity"),
        # Round 21: swing third path always tags daily_approx for ledger provenance.
        "fill_model": "daily_approx",
        "fill_model_provenance_complete": True,
    })
    if intraday_df is None or intraday_df.empty:
        execution["reason"] = "缺少盘中数据，无法确认计划价格是否真实出现"
    return plan, execution


def _position_key(row: dict[str, Any]) -> tuple[str, str]:
    return normalize_symbol(row.get("symbol")), normalize_strategy_type(row.get("strategy_type"), "long")


def _empty_state(initial_cash: float) -> dict[str, Any]:
    scenario_id = ACCEPTANCE_SCENARIO if float(initial_cash) == ACCEPTANCE_CAPITAL else "user_10k"
    profile = resolve_paper_capital_profile(
        scenario_id=scenario_id,
        requested_initial_capital=float(initial_cash),
    )
    payload = {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "created_at": now_text(),
        "updated_at": now_text(),
        "initial_cash": float(initial_cash),
        "cash": float(initial_cash),
        "profile_id": profile.profile_id,
        "scenario_id": profile.scenario_id,
        "capital_profile_label": profile.visible_label,
        "legacy_state_preserved": profile.legacy_state_preserved,
        "paper_credit_enabled": profile.paper_credit_enabled,
        "credit_limit": profile.credit_limit,
        "credit_used": 0.0,
        "simulated_interest": 0.0,
        "positions": [],
        "closed_positions": [],
        "actions": [],
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
    }
    return ensure_declared_fill_policy(payload, policy_id=POLICY_DAILY_APPROX)


def _load_state(initial_cash: float) -> dict[str, Any]:
    if not AUTO_PAPER_PORTFOLIO_STATE_JSON.exists():
        return reconcile_paper_account(_empty_state(initial_cash))
    data = _auto_paper_state_store().read(default={})
    if not isinstance(data, dict) or not data:
        return reconcile_paper_account(_empty_state(initial_cash))
    data.setdefault("initial_cash", float(initial_cash))
    if "cash" not in data:
        data["cash"] = 0.0
        data["warnings"] = list(dict.fromkeys([
            *(data.get("warnings") or []),
            "funded_ledger_cash_missing: existing paper state was not replenished from initial capital",
        ]))
    data.setdefault("positions", [])
    data.setdefault("closed_positions", [])
    data.setdefault("realized_reductions", [])
    data.setdefault("actions", [])
    data.setdefault("disclaimer", DISCLAIMER)
    data.setdefault("broker_boundary", BROKER_BOUNDARY)
    profile = resolve_paper_capital_profile(
        existing_state=data,
        existing_state_verified=True,
    )
    data.setdefault("profile_id", profile.profile_id)
    data.setdefault("scenario_id", profile.scenario_id)
    data.setdefault("capital_profile_label", profile.visible_label)
    data.setdefault("legacy_state_preserved", profile.legacy_state_preserved)
    data.setdefault("paper_credit_enabled", profile.paper_credit_enabled)
    data.setdefault("credit_limit", profile.credit_limit)
    data.setdefault("credit_used", 0.0)
    data.setdefault("simulated_interest", 0.0)
    ensure_declared_fill_policy(data, policy_id=_declared_strategy_policy(data))
    return reconcile_paper_account(data)


def _invalid_paper_account_payload(state: dict[str, Any]) -> dict[str, Any]:
    detail = state.get("accounting_reconciliation")
    detail = dict(detail) if isinstance(detail, dict) else {}
    invalid_reasons = [
        str(item)
        for item in detail.get("invalid_reason_codes") or []
        if str(item)
    ]
    return {
        "ok": False,
        "status": "failed",
        "exists": AUTO_PAPER_PORTFOLIO_STATE_JSON.exists(),
        "error": "paper_account_invalid",
        "reason_codes": list(dict.fromkeys(["paper_account_invalid", *invalid_reasons])),
        "message": "纸面账户结构或数值无效，已阻断本次更新；请先核对账本证据，系统未写回归一化结果。",
        "accounting_reconciliation": detail,
        "warnings": list(state.get("warnings") or []),
        "state_path": str(AUTO_PAPER_PORTFOLIO_STATE_JSON),
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }


def _holding_days(position: dict[str, Any], decision_date: str) -> int:
    entry = str(position.get("entry_date") or "")[:10]
    if not entry:
        return safe_int(position.get("holding_days"), 0) or 0
    try:
        return max(0, (datetime.fromisoformat(decision_date) - datetime.fromisoformat(entry)).days)
    except ValueError:
        return safe_int(position.get("holding_days"), 0) or 0


def _buy_action(candidate: dict[str, Any], quantity: int, price: float, decision_date: str, cash_after: float) -> dict[str, Any]:
    return {
        "action": "buy",
        "action_label": "模拟买入",
        "symbol": normalize_symbol(candidate.get("symbol")),
        "name": str(candidate.get("name") or candidate.get("symbol") or ""),
        "strategy_type": normalize_strategy_type(candidate.get("strategy_type"), "long"),
        "strategy_label": strategy_label(candidate.get("strategy_type")),
        "sector": candidate.get("sector") or "未知板块",
        "primary_theme": candidate.get("primary_theme") or "未知题材",
        "quantity": int(quantity),
        "price": round(price, 4),
        "amount": round(price * quantity, 2),
        "decision_date": decision_date,
        "entry_reason": "按自动策略沙盒候选进入自动虚拟组合，默认每只新候选按所在板块整手单位模拟买入。",
        "reason": str(candidate.get("reason") or "自动策略沙盒生成纸面组合观察计划。"),
        "cash_after": round(cash_after, 2),
        "order_source": "auto_strategy_sandbox",
        **_paper_action_boundary(),
    }


def _block_action(candidate: dict[str, Any], reason: str, decision_date: str) -> dict[str, Any]:
    return {
        "action": "block",
        "action_label": "风控阻断",
        "symbol": normalize_symbol(candidate.get("symbol")),
        "name": str(candidate.get("name") or candidate.get("symbol") or ""),
        "strategy_type": normalize_strategy_type(candidate.get("strategy_type"), "long"),
        "strategy_label": strategy_label(candidate.get("strategy_type")),
        "sector": candidate.get("sector") or "未知板块",
        "primary_theme": candidate.get("primary_theme") or "未知题材",
        "quantity": 0,
        "price": _candidate_price(candidate),
        "decision_date": decision_date,
        "blocked_reason": reason,
        "reason": reason,
        "order_source": "auto_strategy_sandbox",
        **_paper_action_boundary(),
    }


def _make_position(
    candidate: dict[str, Any],
    quantity: int,
    price: float,
    decision_date: str,
    cost_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cost = dict(cost_info or buy_cash_outflow(price=price, quantity=quantity))
    gross = safe_float(cost.get("gross"), price * quantity) or price * quantity
    total_fee = safe_float(cost.get("total_fee"), 0.0) or 0.0
    cash_out = safe_float(cost.get("cash_out"), gross + total_fee) or (gross + total_fee)
    cost_basis = safe_float(cost.get("cost_basis"), cash_out) or cash_out
    fee_inclusive_entry = cost_basis / quantity if quantity else price
    position = {
        "symbol": normalize_symbol(candidate.get("symbol")),
        "name": str(candidate.get("name") or candidate.get("symbol") or ""),
        "strategy_type": normalize_strategy_type(candidate.get("strategy_type"), "long"),
        "strategy_label": strategy_label(candidate.get("strategy_type")),
        "sector": candidate.get("sector") or "未知板块",
        "primary_theme": candidate.get("primary_theme") or "未知题材",
        "quantity": int(quantity),
        "entry_date": decision_date,
        "entry_price": round(fee_inclusive_entry, 4),
        "execution_price": round(price, 4),
        "entry_gross_amount": round(gross, 2),
        "entry_total_fee": round(total_fee, 2),
        "entry_cash_out": round(cash_out, 2),
        "entry_cost_basis_includes_buy_fee": True,
        "latest_price": round(price, 4),
        "latest_date": decision_date,
        "market_value": round(price * quantity, 2),
        "unrealized_pnl": round((price - fee_inclusive_entry) * quantity, 2),
        "unrealized_return_pct": round((price / fee_inclusive_entry - 1.0) * 100.0, 2) if fee_inclusive_entry > 0 else 0.0,
        "return_pct": round((price / fee_inclusive_entry - 1.0) * 100.0, 2) if fee_inclusive_entry > 0 else 0.0,
        "holding_days": 0,
        "entry_reason": "默认每只新候选按所在板块整手单位模拟买入，用于观察策略本身表现。",
        "hold_reason": "新进入自动虚拟组合，等待下一次日线更新后复盘。",
        "zero_pnl_explanation": "今日新建仓，最新价等于模拟买入价，因此当前浮动盈亏为 0。需等下一交易日更新行情后才能复盘收益。",
        "order_source": "auto_strategy_sandbox",
        **_paper_action_boundary(),
    }
    envelope = candidate.get("candidate_envelope")
    if isinstance(envelope, dict):
        position["candidate_envelope"] = copy.deepcopy(envelope)
    return position


def _mark_position(
    position: dict[str, Any],
    latest_price: float,
    decision_date: str,
    hold_reason: str,
    *,
    adjustment_factors: AdjustmentFactorTable | None = None,
    quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quantity = safe_int(position.get("quantity"), 0) or 0
    entry_price = safe_float(position.get("entry_price"), latest_price) or latest_price
    position["latest_price"] = round(latest_price, 4)
    position["market_value"] = round(latest_price * quantity, 2)
    position["unrealized_pnl"] = round((latest_price - entry_price) * quantity, 2)
    return_pct = round((latest_price / entry_price - 1.0) * 100.0, 2) if entry_price > 0 else 0.0
    position["unrealized_return_pct"] = return_pct
    position["return_pct"] = return_pct
    position["holding_days"] = _holding_days(position, decision_date)
    position["hold_reason"] = hold_reason
    position["sector"] = position.get("sector") or "未知板块"
    position["primary_theme"] = position.get("primary_theme") or "未知题材"
    quote = quote if isinstance(quote, dict) else _latest_daily_quote(position.get("symbol"), decision_date=decision_date)
    position["latest_date"] = quote.get("date") or position.get("latest_date") or decision_date
    quote_warnings = [str(item) for item in (quote.get("warnings") or [])]
    if quote_warnings:
        seen = list(position.get("valuation_warnings") or [])
        position["valuation_warnings"] = seen + [w for w in quote_warnings if w not in seen]
    position = apply_suspension_valuation_policy(position, quote)
    adjusted_price = safe_float(position.get("latest_price"), latest_price) or latest_price
    position = apply_paper_adjustment_policy(
        position,
        latest_price=float(adjusted_price),
        latest_date=str(position.get("latest_date") or decision_date),
        factors=adjustment_factors,
    )
    if str(position.get("entry_date") or "")[:10] == str(position.get("latest_date") or "")[:10]:
        position["zero_pnl_explanation"] = "今日新建仓，最新价等于模拟买入价，因此当前浮动盈亏为 0。需等下一交易日更新行情后才能复盘收益。"
    elif abs(safe_float(position.get("unrealized_pnl"), 0.0) or 0.0) < 1e-9:
        position["zero_pnl_explanation"] = "持仓估值仍使用买入日价格，可能是日线未更新或最新价未刷新。"
    else:
        position["zero_pnl_explanation"] = ""
    return position


def _hold_action(position: dict[str, Any], latest_price: float, decision_date: str, hold_reason: str) -> dict[str, Any]:
    quantity = safe_int(position.get("quantity"), 0) or 0
    entry_price = safe_float(position.get("entry_price"), latest_price) or latest_price
    return {
        "action": "hold",
        "action_label": "纸面持仓",
        "symbol": normalize_symbol(position.get("symbol")),
        "name": str(position.get("name") or position.get("symbol") or ""),
        "strategy_type": normalize_strategy_type(position.get("strategy_type"), "long"),
        "strategy_label": strategy_label(position.get("strategy_type")),
        "sector": position.get("sector") or "未知板块",
        "primary_theme": position.get("primary_theme") or "未知题材",
        "quantity": quantity,
        "price": round(latest_price, 4),
        "decision_date": decision_date,
        "hold_reason": hold_reason,
        "reason": hold_reason,
        "pnl": round((latest_price - entry_price) * quantity, 2),
        "order_source": "auto_strategy_sandbox",
        **_paper_action_boundary(),
    }


def _watch_exit_action(position: dict[str, Any], latest_price: float, decision_date: str, reason: str) -> dict[str, Any]:
    action = _hold_action(position, latest_price, decision_date, reason)
    action["action"] = "watch_exit"
    action["action_label"] = "退出观察"
    action["watch_status"] = "watch_exit"
    return action


def _sell_action(position: dict[str, Any], latest_price: float, decision_date: str, exit_reason: str) -> dict[str, Any]:
    quantity = safe_int(position.get("quantity"), 0) or 0
    entry_price = safe_float(position.get("entry_price"), latest_price) or latest_price
    gross_amount = float(latest_price) * quantity
    costs = compute_trade_costs(side="sell", gross=gross_amount)
    cash = sell_cash_inflow(price=latest_price, quantity=quantity, avg_cost=entry_price)
    return {
        "action": "sell",
        "action_label": "模拟卖出",
        "symbol": normalize_symbol(position.get("symbol")),
        "name": str(position.get("name") or position.get("symbol") or ""),
        "strategy_type": normalize_strategy_type(position.get("strategy_type"), "long"),
        "strategy_label": strategy_label(position.get("strategy_type")),
        "sector": position.get("sector") or "未知板块",
        "primary_theme": position.get("primary_theme") or "未知题材",
        "quantity": quantity,
        "price": round(latest_price, 4),
        "decision_date": decision_date,
        "exit_reason": exit_reason,
        "trigger_rule": "auto_portfolio_risk_review",
        "reason": exit_reason,
        "gross_amount": round(gross_amount, 2),
        "commission": round(float(costs["commission"]), 2),
        "stamp_duty": round(float(costs["stamp_duty"]), 2),
        "transfer_fee": round(float(costs["transfer_fee"]), 2),
        "slippage": round(float(costs["slippage"]), 2),
        "total_fee": round(float(costs["total_fee"]), 2),
        "net_amount": round(float(cash["cash_in"]), 2),
        "pnl": round(float(cash["realized_pnl"]), 2),
        "order_source": "auto_strategy_sandbox",
        **_paper_action_boundary(),
    }


def _close_position(position: dict[str, Any], latest_price: float, decision_date: str, exit_reason: str) -> dict[str, Any]:
    quantity = safe_int(position.get("quantity"), 0) or 0
    entry_price = safe_float(position.get("entry_price"), latest_price) or latest_price
    gross_amount = float(latest_price) * quantity
    costs = compute_trade_costs(side="sell", gross=gross_amount)
    cash = sell_cash_inflow(price=latest_price, quantity=quantity, avg_cost=entry_price)
    entry_basis_known = position.get("entry_cost_basis_includes_buy_fee")
    return {
        **position,
        "exit_date": decision_date,
        "exit_price": round(latest_price, 4),
        "gross_amount": round(gross_amount, 2),
        "commission": round(float(costs["commission"]), 2),
        "stamp_duty": round(float(costs["stamp_duty"]), 2),
        "transfer_fee": round(float(costs["transfer_fee"]), 2),
        "slippage": round(float(costs["slippage"]), 2),
        "total_fee": round(float(costs["total_fee"]), 2),
        "fee_model": str(costs["fee_model"]),
        "net_amount": round(float(cash["cash_in"]), 2),
        "realized_pnl": round(float(cash["realized_pnl"]), 2),
        "entry_cost_basis_includes_buy_fee": bool(entry_basis_known),
        "entry_cost_basis_status": "fee_inclusive" if entry_basis_known else "legacy_or_unknown_entry_price",
        "return_pct": round((latest_price / entry_price - 1.0) * 100.0, 2) if entry_price > 0 else 0.0,
        "exit_reason": exit_reason,
        "status": "closed",
        **_paper_action_boundary(),
    }


def build_decision_audit(actions: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(action.get("action") or "unknown") for action in actions)
    total = sum(counts.values())
    score = round(((total - counts.get("block", 0)) / total) * 100.0, 2) if total else 0.0
    notes = [
        "自动虚拟组合只用于本地纸面交易复盘，不构成投资建议。",
        "策略决策审计基于本次组合动作生成，单日结果不能证明策略长期有效。",
    ]
    if counts.get("buy"):
        notes.append(f"本次产生 {counts.get('buy')} 个模拟买入动作，后续需要用新日线继续验证。")
    if counts.get("sell"):
        notes.append(f"本次产生 {counts.get('sell')} 个模拟卖出动作，请观察是否降低后续回撤。")
    if counts.get("block"):
        notes.append(f"本次有 {counts.get('block')} 个动作被阻断，优先检查现金、数据和一手数量约束。")
    return {
        "action_counts": dict(counts),
        "decision_quality_score": score,
        "total_actions": total,
        "buy_count": counts.get("buy", 0),
        "hold_count": counts.get("hold", 0),
        "sell_count": counts.get("sell", 0),
        "block_count": counts.get("block", 0),
        "watch_exit_count": counts.get("watch_exit", 0),
        "strategy_learning_notes": notes,
    }


def _price_change_review(symbol: str, base_price: Any) -> tuple[float | None, str, float | None]:
    quote = _latest_daily_quote(symbol)
    today_price = safe_float(quote.get("price"), None)
    base = safe_float(base_price, None)
    if today_price is None or base is None or base <= 0:
        return today_price, quote.get("date") or "", None
    return today_price, quote.get("date") or "", round((today_price / base - 1.0) * 100.0, 2)


def build_decision_review(
    previous_actions: list[dict[str, Any]],
    decision_date: str,
    freshness_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if freshness_gate is not None and not freshness_gate.get("can_review_yesterday_positions", True):
        return waiting_decision_review(previous_actions, freshness_gate)
    items: list[dict[str, Any]] = []
    counters = Counter()
    for action in previous_actions:
        action_type = str(action.get("action") or "")
        if action_type not in {"buy", "hold", "sell", "watch_exit"}:
            continue
        symbol = normalize_symbol(action.get("symbol"))
        base_price = action.get("price") or action.get("latest_price") or action.get("entry_price")
        today_price, today_date, pct = _price_change_review(symbol, base_price)
        if action_type == "sell":
            if pct is None:
                result = "无法复盘"
                quality = "neutral"
            elif pct < 0:
                result = "avoided_loss"
                quality = "correct"
            else:
                result = "premature_exit"
                quality = "failed"
        elif action_type in {"buy", "hold"}:
            if pct is None:
                result = "无法复盘"
                quality = "neutral"
            elif pct >= 0:
                result = "决策暂时有效"
                quality = "correct"
            else:
                result = "决策暂时失败"
                quality = "failed"
        else:
            if pct is None:
                result = "退出观察待复盘"
                quality = "neutral"
            elif pct < 0:
                result = "退出观察有效"
                quality = "correct"
            else:
                result = "退出观察偏谨慎"
                quality = "failed"
        counters[f"{action_type}_{quality}"] += 1
        items.append({
            "symbol": symbol,
            "name": action.get("name") or symbol,
            "strategy_type": action.get("strategy_type") or "",
            "previous_action": action_type,
            "previous_price": base_price,
            "today_price": today_price,
            "today_date": today_date,
            "today_return_pct": pct,
            "review_result": result,
            "quality_category": quality,
            "reason": action.get("reason") or action.get("hold_reason") or action.get("exit_reason") or "",
        })
    return {
        "ok": True,
        "review_date": decision_date,
        "items": items,
        "buy_correct_count": counters.get("buy_correct", 0),
        "buy_failed_count": counters.get("buy_failed", 0),
        "hold_correct_count": counters.get("hold_correct", 0),
        "hold_failed_count": counters.get("hold_failed", 0),
        "sell_correct_count": counters.get("sell_correct", 0),
        "sell_failed_count": counters.get("sell_failed", 0),
        "watch_exit_correct_count": counters.get("watch_exit_correct", 0),
        "watch_exit_failed_count": counters.get("watch_exit_failed", 0),
        "disclaimer": DISCLAIMER,
    }


def build_yesterday_holding_review(
    previous_positions: list[dict[str, Any]],
    current_positions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    decision_review: dict[str, Any],
    freshness_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if freshness_gate is not None and not freshness_gate.get("can_review_yesterday_positions", True):
        return waiting_yesterday_holding_review(previous_positions, freshness_gate)
    current_by_key = {_position_key(row): row for row in current_positions}
    action_by_key = {
        _position_key(row): row
        for row in actions
        if row.get("action") in {"hold", "sell", "sell_all", "stop_loss", "reduce_half", "take_profit", "add", "breakout_add", "block", "watch_exit", "no_fill"}
    }
    rows: list[dict[str, Any]] = []
    total_pnl = 0.0
    for prev in previous_positions:
        key = _position_key(prev)
        action = action_by_key.get(key, {})
        current = current_by_key.get(key, {})
        quote = _latest_daily_quote(prev.get("symbol"))
        today_price = safe_float(quote.get("price"), None)
        if today_price is None:
            today_price = safe_float(action.get("price"), safe_float(current.get("latest_price"), safe_float(prev.get("latest_price"), prev.get("entry_price"))))
        previous_price = safe_float(prev.get("latest_price"), safe_float(prev.get("entry_price"), today_price)) or today_price or 0.0
        quantity = safe_int(prev.get("quantity"), 0) or 0
        pct = round((today_price / previous_price - 1.0) * 100.0, 2) if today_price and previous_price else None
        pnl = round(((today_price or previous_price) - previous_price) * quantity, 2)
        total_pnl += pnl
        conclusion = action.get("action_label") or action.get("action") or ("继续持有" if current else "未在当前持仓中")
        rows.append({
            "symbol": normalize_symbol(prev.get("symbol")),
            "name": prev.get("name") or prev.get("symbol"),
            "strategy_type": prev.get("strategy_type") or "",
            "yesterday_quantity": quantity,
            "yesterday_price": previous_price,
            "today_price": today_price,
            "today_return_pct": pct,
            "floating_pnl": pnl,
            "review_conclusion": conclusion,
            "reason": action.get("reason") or action.get("hold_reason") or current.get("hold_reason") or "",
        })
    counts = Counter(str(action.get("action") or "") for action in actions)
    return {
        "ok": True,
        "yesterday_position_count": len(previous_positions),
        "today_still_holding": len(current_positions),
        "today_new": counts.get("buy", 0),
        "today_sold": counts.get("sell", 0),
        "today_blocked": counts.get("block", 0),
        "today_watch_exit": counts.get("watch_exit", 0),
        "yesterday_position_unrealized_pnl": round(total_pnl, 2),
        "buy_correct_count": decision_review.get("buy_correct_count", 0),
        "buy_failed_count": decision_review.get("buy_failed_count", 0),
        "hold_correct_count": decision_review.get("hold_correct_count", 0),
        "hold_failed_count": decision_review.get("hold_failed_count", 0),
        "items": rows,
        "disclaimer": DISCLAIMER,
    }


def paper_account_scale(initial_cash: float | None) -> tuple[str, str]:
    """Label the ledger scale for UI/早报 (Round 21 本金配置化).

    The live 10k ledger stays untouched; it is labeled ``legacy_10k`` so its
    returns are never compared against 100k-backtest numbers.
    """

    value = safe_float(initial_cash, 0.0) or 0.0
    if abs(value - 10_000.0) <= 1e-9:
        return "legacy_10k", "教学规模（1 万本金），不与 10 万回测比收益。"
    return "configured", ""


def _summary(state: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    positions = state.get("positions") if isinstance(state.get("positions"), list) else []
    closed = state.get("closed_positions") if isinstance(state.get("closed_positions"), list) else []
    cash = safe_float(state.get("cash"), 0.0) or 0.0
    initial_cash = safe_float(state.get("initial_cash"), cash) or cash
    account_scale, account_scale_note = paper_account_scale(initial_cash)
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in positions)
    closed_realized_pnl = sum(safe_float(row.get("realized_pnl"), 0.0) or 0.0 for row in closed)
    reductions = state.get("realized_reductions") if isinstance(state.get("realized_reductions"), list) else []
    partial_realized_pnl = sum(safe_float(row.get("realized_pnl"), 0.0) or 0.0 for row in reductions if isinstance(row, dict))
    legacy_realized_adjustment = safe_float(state.get("legacy_realized_adjustment"), 0.0) or 0.0
    realized_pnl = closed_realized_pnl + partial_realized_pnl + legacy_realized_adjustment
    unrealized_pnl = sum(safe_float(row.get("unrealized_pnl"), 0.0) or 0.0 for row in positions)
    total_equity = cash + market_value
    counts = Counter(str(action.get("action") or "") for action in actions)
    return {
        "initial_cash": round(initial_cash, 2),
        "account_scale": account_scale,
        "account_scale_note": account_scale_note,
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "total_equity": round(total_equity, 2),
        "total_return_pct": round((total_equity / initial_cash - 1.0) * 100.0, 2) if initial_cash else 0.0,
        "cumulative_pnl": round(total_equity - initial_cash, 2),
        "daily_pnl": None,
        "daily_return_pct": None,
        "daily_pnl_status": "missing_previous_equity",
        "realized_pnl": round(realized_pnl, 2),
        "closed_realized_pnl": round(closed_realized_pnl, 2),
        "partial_realized_pnl": round(partial_realized_pnl, 2),
        "legacy_realized_adjustment": round(legacy_realized_adjustment, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "position_count": len(positions),
        "today_new_count": counts.get("buy", 0),
        "today_add_count": counts.get("add", 0) + counts.get("breakout_add", 0),
        "today_reduce_count": counts.get("reduce_half", 0) + counts.get("take_profit", 0),
        "today_sell_count": counts.get("sell", 0) + counts.get("sell_all", 0) + counts.get("stop_loss", 0),
        "today_no_fill_count": counts.get("no_fill", 0),
        "today_hold_count": counts.get("hold", 0),
        "today_block_count": counts.get("block", 0),
        "today_watch_exit_count": counts.get("watch_exit", 0),
    }


def _with_daily_pnl(
    summary: dict[str, Any], *, valuation_date: str,
    previous_state: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
    valuation_valid: bool = True,
) -> dict[str, Any]:
    """Separate cumulative PnL from an evidenced previous-session equity change.

    Same-day reruns always use the same previous session, never the equity
    already marked earlier today. Missing calendar/equity evidence stays null.
    This is reporting only; it never updates cash, fills, or valuation prices.
    """
    result = dict(summary)
    result.update(daily_pnl=None, daily_return_pct=None,
                  previous_total_equity=None, previous_equity_date="",
                  daily_pnl_status="missing_previous_equity", daily_pnl_source="")
    if not valuation_valid:
        result["daily_pnl_status"] = "current_valuation_unverified"
        return result
    try:
        target = date.fromisoformat(valuation_date)
    except (TypeError, ValueError):
        result["daily_pnl_status"] = "valuation_date_invalid"
        return result
    cal = calendar if calendar is not None else load_a_share_trading_calendar(
        _archive_project_root() / "data/calendar/a_share_trading_calendar.csv"
    )
    sessions = cal.get("is_open_by_date") or {}
    if sessions.get(target.isoformat()) is not True:
        result["daily_pnl_status"] = "previous_session_unverified"
        return result
    previous = target - timedelta(days=1)
    for _ in range(370):
        if previous.isoformat() not in sessions:
            result["daily_pnl_status"] = "previous_session_unverified"
            return result
        if sessions[previous.isoformat()] is True:
            break
        previous -= timedelta(days=1)
    else:
        result["daily_pnl_status"] = "previous_session_unverified"
        return result
    previous_date = previous.isoformat()
    result["previous_equity_date"] = previous_date

    def number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        value = safe_float(value, None)
        return value if value is not None and math.isfinite(value) else None

    prior = previous_state or {}
    reference = prior.get("daily_pnl_reference") or {}
    equity = None
    if (isinstance(reference, dict) and reference.get("valuation_date") == valuation_date
            and reference.get("previous_equity_date") == previous_date):
        equity = number(reference.get("previous_total_equity"))
        result["daily_pnl_source"] = "persisted_previous_session_reference"
    if equity is None:
        loaded = history if history is not None else read_json_safe(AUTO_PAPER_PORTFOLIO_HISTORY_JSON, {})
        items = loaded.get("items", []) if isinstance(loaded, dict) else []
        matches = [row for row in items if isinstance(row, dict)
                   and str(row.get("valuation_date") or row.get("generated_at") or "")[:10] == previous_date]
        matches.sort(key=lambda row: str(row.get("generated_at") or ""))
        if matches:
            # The latest record for that session is authoritative; do not skip
            # a damaged latest record to silently revive an older amount.
            row = matches[-1]
            recorded, cash, market = (number(row.get(key)) for key in ("total_equity", "cash", "market_value"))
            if (row.get("portfolio_review_valid") is not False and recorded is not None
                    and cash is not None and market is not None and abs(recorded - cash - market) <= 0.011):
                equity = recorded
                result["daily_pnl_source"] = "previous_session_history"
    if (equity is None and prior.get("valuation_date") == previous_date
            and prior.get("portfolio_review_valid") is not False):
        cash, market = number(prior.get("cash")), number(prior.get("market_value"))
        if cash is not None and market is not None:
            equity = cash + market
            result["daily_pnl_source"] = "previous_session_state"
    current_equity = number(result.get("total_equity"))
    if equity is None or current_equity is None:
        result["daily_pnl_source"] = ""
        return result
    result.update(previous_total_equity=round(equity, 2),
                  daily_pnl=round(current_equity - equity, 2), daily_pnl_status="verified",
                  daily_return_pct=round((current_equity / equity - 1.0) * 100.0, 4) if equity > 0 else None)
    return result


def _daily_pnl_reference(summary: dict[str, Any], valuation_date: str) -> dict[str, Any]:
    if summary.get("daily_pnl_status") != "verified":
        return {}
    return {"valuation_date": valuation_date, "previous_equity_date": summary["previous_equity_date"],
            "previous_total_equity": summary["previous_total_equity"]}


def _append_history(latest: dict[str, Any]) -> dict[str, Any]:
    history = read_json_safe(AUTO_PAPER_PORTFOLIO_HISTORY_JSON, {})
    if not isinstance(history, dict):
        history = {}
    items = history.get("items") if isinstance(history.get("items"), list) else []
    transaction = latest.get("strategy_journal_transaction")
    if transaction and any(item.get("strategy_journal_transaction") == transaction for item in items):
        return history
    summary = latest.get("summary") if isinstance(latest.get("summary"), dict) else {}
    items.append({
        "generated_at": latest.get("generated_at"),
        "valuation_date": latest.get("valuation_date"),
        "portfolio_review_valid": latest.get("portfolio_review_valid"),
        **({"strategy_journal_transaction": transaction} if transaction else {}),
        "mode": latest.get("mode"),
        "strategy": latest.get("strategy"),
        "cash": summary.get("cash"),
        "market_value": summary.get("market_value"),
        "total_equity": summary.get("total_equity"),
        "total_return_pct": summary.get("total_return_pct"),
        "action_counts": latest.get("decision_audit", {}).get("action_counts", {}),
    })
    payload = {"ok": True, "updated_at": now_text(), "items": items[-500:], "disclaimer": DISCLAIMER}
    atomic_write_json(AUTO_PAPER_PORTFOLIO_HISTORY_JSON, payload)
    return payload


def newest_auto_paper_report() -> Path | None:
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("auto_paper_portfolio_report_*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _write_report(
    latest: dict[str, Any],
    *,
    risk_gate_shadow_summary: dict[str, Any] | None = None,
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"auto_paper_portfolio_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    summary = latest.get("summary") or {}
    lines = [
        "# 自动虚拟组合复盘报告",
        "",
        DISCLAIMER,
        BROKER_BOUNDARY,
        "",
        "## 组合摘要",
        "",
        *([
            "- 撮合方式：下一交易日 09:30 归档开盘价；开盘前须有已记录意向。",
            "- 验证状态：研究输出·未验证；原始开盘价是纸面代理成交价，未证明真实流动性。",
            f"- 待核验意向：{len(latest.get('pending_orders') or [])} 笔；成交与拒绝见追加流水。",
        ] if latest.get("strategy_fill_mode") == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT else []),
        f"- 当前现金：{summary.get('cash', 0)}",
        f"- 持仓市值：{summary.get('market_value', 0)}",
        f"- 总权益：{summary.get('total_equity', 0)}",
        f"- 总收益率：{summary.get('total_return_pct', 0)}%",
        f"- 当前持仓数：{summary.get('position_count', 0)}",
        "",
        "## 昨日持仓复盘",
        "",
    ]
    yesterday_review = latest.get("yesterday_holding_review") if isinstance(latest.get("yesterday_holding_review"), dict) else {}
    lines.extend([
        f"- 昨日持仓数：{yesterday_review.get('yesterday_position_count', 0)}",
        f"- 今日仍持有：{yesterday_review.get('today_still_holding', 0)}",
        f"- 今日新增：{yesterday_review.get('today_new', 0)}",
        f"- 今日模拟卖出：{yesterday_review.get('today_sold', 0)}",
        f"- 今日风控阻断：{yesterday_review.get('today_blocked', 0)}",
        f"- 昨日持仓今日总盈亏：{yesterday_review.get('yesterday_position_unrealized_pnl', 0)}",
        "",
        "| 代码 | 名称 | 策略 | 昨日数量 | 昨日价格 | 今日价格 | 今日涨跌 | 浮动盈亏 | 复盘结论 | 原因 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ])
    for row in yesterday_review.get("items") or []:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('name', '')} | {row.get('strategy_type', '')} | "
            f"{row.get('yesterday_quantity', 0)} | {row.get('yesterday_price', '')} | {row.get('today_price', '')} | "
            f"{row.get('today_return_pct', '')} | {row.get('floating_pnl', '')} | {row.get('review_conclusion', '')} | {row.get('reason', '')} |"
        )
    conditional_review = latest.get("conditional_review") if isinstance(latest.get("conditional_review"), dict) else {}
    conditional_plan = latest.get("conditional_plan") if isinstance(latest.get("conditional_plan"), dict) else {}
    conditional_execution = latest.get("conditional_execution") if isinstance(latest.get("conditional_execution"), dict) else {}
    if conditional_review or conditional_plan or conditional_execution:
        plan_summary = conditional_plan.get("summary") if isinstance(conditional_plan.get("summary"), dict) else {}
        lines += [
            "",
            "## 条件交易计划与盘中执行",
            "",
            "系统采用条件交易计划进行纸面模拟：先根据历史数据生成计划，再根据盘中数据判断是否触发。缺少分钟线时不会用收盘价假装成交。",
            "加仓、减仓、止盈、止损均为本地纸面模拟，不构成投资建议。",
            "本工具不接券商 API，不真实下单，不保存真实账户信息，不承诺收益。",
            "",
            f"- 计划 ID：{conditional_plan.get('plan_id', conditional_execution.get('plan_id', '-'))}",
            f"- 计划日期：{conditional_plan.get('plan_date', '-')}",
            f"- 交易日期：{conditional_plan.get('trade_date', conditional_execution.get('trade_date', '-'))}",
            f"- 条件计划数量：{plan_summary.get('symbol_count', 0)}",
            f"- 可靠成交数量：{conditional_review.get('reliable_event_count', 0)}",
            f"- 计划未触发数量：{conditional_review.get('no_fill_count', 0)}",
            f"- 风控阻断数量：{conditional_review.get('blocked_count', 0)}",
            f"- 已验证分钟池命中数量：{conditional_review.get('verified_completed_intraday_count', 0)}",
            f"- 本地未验证分钟线命中数量：{conditional_review.get('local_unverified_intraday_count', 0)}",
            f"- 缺少分钟线数量：{conditional_review.get('missing_intraday_count', 0)}",
            "",
        ]
    if isinstance(risk_gate_shadow_summary, dict):
        try:
            lines.extend(render_shadow_report_section(risk_gate_shadow_summary))
        except Exception as exc:
            # A distinct header: the failure is visible, but is never mistaken
            # for a rendered shadow section.
            lines.extend(["", "## 影子风险门（未能生成）", "",
                          f"- 影子风险段生成失败：{type(exc).__name__}: {exc}"])
            try:
                record_shadow_error_best_effort(
                    surface="auto_paper_report_shadow_section",
                    error=exc,
                    as_of=str(risk_gate_shadow_summary.get("as_of") or ""),
                    error_path=_risk_gate_shadow_error_path(),
                )
            except Exception as log_exc:  # noqa: BLE001 - visible in the report
                lines.append(f"- 影子风险错误日志写入也失败：{type(log_exc).__name__}: {log_exc}")
                _SHADOW_UNRECORDED.append(f"shadow_error_log_write_failed:auto_paper_report_shadow_section:{type(log_exc).__name__}")
    lines += [
        "",
        "## 今日动作",
        "",
        "| 动作 | 代码 | 名称 | 策略 | 数量 | 价格 | 原因 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for action in latest.get("actions") or []:
        lines.append(
            f"| {action.get('action_label') or action.get('action')} | {action.get('symbol')} | "
            f"{action.get('name', '')} | {action.get('strategy_type', '')} | {action.get('quantity', 0)} | "
            f"{action.get('price', '')} | {action.get('reason', '')} |"
        )
    lines += ["", "## 策略决策审计", "", "单日结果不能证明策略长期有效，需要累计样本继续观察。"]
    for note in latest.get("decision_audit", {}).get("strategy_learning_notes", []):
        lines.append(f"- {note}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def newest_swing_execution_report() -> Path | None:
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("swing_execution_report_*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _write_swing_execution_report(plans: list[dict[str, Any]], executions: list[dict[str, Any]]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"swing_execution_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        "# 长线波段计划与盘中触发回放",
        "",
        "本报告只用于本地策略学习与模拟验证，不构成投资建议。",
        "盘中回放按分钟线顺序检查触发，避免未来函数；缺少分钟线时不会用日线假装成交。",
        "",
        "## 波段计划",
        "",
        "| 代码 | 名称 | 计划日期 | 交易日期 | 低吸区间 | 不追高价格 | 计划原因 |",
        "|---|---|---|---|---|---:|---|",
    ]
    for plan in plans:
        buy_zones = plan.get("buy_zones") or []
        pullback = next((zone for zone in buy_zones if zone.get("type") == "pullback_buy"), {})
        zone_text = "-"
        if pullback:
            zone_text = f"{pullback.get('price_low', '-')}-{pullback.get('price_high', '-')}"
        lines.append(
            f"| {plan.get('symbol', '')} | {plan.get('name', '')} | {plan.get('plan_date', '')} | "
            f"{plan.get('trade_date', '')} | {zone_text} | {plan.get('no_chase_above', '')} | "
            f"{(buy_zones[0].get('reason') if buy_zones else plan.get('reason', ''))} |"
        )
    lines += [
        "",
        "## 盘中触发结果",
        "",
        "| 代码 | 动作 | 触发类型 | 触发时间 | 触发价格 | 数量 | 是否可靠 | 原因 |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for item in executions:
        lines.append(
            f"| {item.get('symbol', '')} | {item.get('action', '')} | {item.get('trigger_type', '')} | "
            f"{item.get('trigger_time', '')} | {item.get('trigger_price', '')} | {item.get('quantity', '')} | "
            f"{item.get('is_reliable', False)} | {item.get('reason', '')} |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_intraday_swing_artifacts(
    plans: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    *,
    enabled: bool,
    source: str,
    allow_daily_approx: bool,
) -> dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = now_text()
    plan_payload = {
        "ok": True,
        "generated_at": generated_at,
        "enabled": bool(enabled),
        "intraday_source": source,
        "allow_daily_approx": bool(allow_daily_approx),
        "plan_count": len(plans),
        "items": plans,
        "disclaimer": DISCLAIMER,
    }
    execution_payload = {
        "ok": True,
        "generated_at": generated_at,
        "enabled": bool(enabled),
        "intraday_source": source,
        "allow_daily_approx": bool(allow_daily_approx),
        "execution_count": len(executions),
        "triggered_count": sum(1 for row in executions if row.get("triggered")),
        "unreliable_count": sum(1 for row in executions if row.get("is_reliable") is False),
        "items": executions,
        "disclaimer": DISCLAIMER,
    }
    atomic_write_json(SWING_TRADE_PLANS_LATEST_JSON, plan_payload)
    atomic_write_json(INTRADAY_EXECUTION_LATEST_JSON, execution_payload)
    report_path = _write_swing_execution_report(plans, executions)
    execution_payload["report_path"] = str(report_path.relative_to(PROJECT_ROOT))
    atomic_write_json(INTRADAY_EXECUTION_LATEST_JSON, execution_payload)
    return {"plans": plan_payload, "executions": execution_payload, "report_path": report_path}


def _execution_rows(execution_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(execution_payload.get("events") or []) + list(execution_payload.get("no_fill") or []) + list(execution_payload.get("blocked") or [])


def _total_equity_from_state(state: dict[str, Any]) -> float:
    cash = safe_float(state.get("cash"), safe_float(state.get("initial_cash"), 0.0)) or 0.0
    market_value = sum(safe_float(row.get("market_value"), 0.0) or 0.0 for row in state.get("positions") or [] if isinstance(row, dict))
    if market_value <= 0:
        market_value = safe_float(state.get("market_value"), 0.0) or 0.0
    total = safe_float(state.get("total_equity"), None)
    return float(total) if total is not None and total > 0 else cash + market_value


def _write_conditional_compat_artifacts(plan_payload: dict[str, Any], execution_payload: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy swing artifact readers working while v2.8.4 uses conditional outputs."""

    plan_items = plan_payload.get("symbols") if isinstance(plan_payload.get("symbols"), list) else []
    execution_items = _execution_rows(execution_payload)
    generated_at = now_text()
    report_path = _write_swing_execution_report(plan_items, execution_items)
    atomic_write_json(SWING_TRADE_PLANS_LATEST_JSON, {
        "ok": True,
        "exists": True,
        "generated_at": generated_at,
        "compat_source": "conditional_trade_plans_latest",
        "plan_count": len(plan_items),
        "items": plan_items,
        "disclaimer": DISCLAIMER,
    })
    atomic_write_json(INTRADAY_EXECUTION_LATEST_JSON, {
        "ok": True,
        "exists": True,
        "generated_at": generated_at,
        "compat_source": "conditional_execution_latest",
        "execution_count": len(execution_items),
        "triggered_count": len(execution_payload.get("events") or []),
        "unreliable_count": sum(1 for row in execution_items if row.get("is_reliable") is False),
        "items": execution_items,
        "report_path": _display_path(report_path),
        "disclaimer": DISCLAIMER,
    })
    return {"report_path": report_path}


def _conditional_review_summary(execution_payload: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _execution_rows(execution_payload)
    summary = execution_payload.get("summary") if isinstance(execution_payload.get("summary"), dict) else {}
    action_counts = Counter(str(row.get("action") or "") for row in rows)
    reliable = [row for row in rows if row.get("is_reliable")]
    return {
        "ok": True,
        "plan_id": execution_payload.get("plan_id"),
        "trade_date": execution_payload.get("trade_date"),
        "event_count": len(execution_payload.get("events") or []),
        "reliable_event_count": len([row for row in execution_payload.get("events") or [] if row.get("is_reliable")]),
        "no_fill_count": len(execution_payload.get("no_fill") or []),
        "blocked_count": len(execution_payload.get("blocked") or []),
        "missing_intraday_count": sum(1 for row in rows if row.get("fill_model") == "missing_intraday"),
        "verified_completed_intraday_count": int(summary.get("verified_completed_intraday_count") or 0),
        "local_unverified_intraday_count": int(summary.get("local_unverified_intraday_count") or 0),
        "intraday_quality_gate": summary.get("intraday_quality_gate") if isinstance(summary.get("intraday_quality_gate"), dict) else {},
        "reliable_fill_count": len(reliable),
        "action_counts": dict(action_counts),
        "daily_action_counts": dict(Counter(str(row.get("action") or "") for row in actions)),
        "explanation": "系统采用条件交易计划进行纸面模拟：先根据历史数据生成计划，再根据盘中数据判断是否触发。缺少分钟线时不会用收盘价假装成交。",
        "disclaimer": DISCLAIMER,
    }


def _run_conditional_auto_paper_update(
    *,
    initial_cash: float,
    quantity_per_stock: int,
    strategy: str,
    intraday_source: str,
    allow_daily_approx: bool,
    dry_run: bool,
    auto_strategy_path: Path | None,
    auto_strategy_payload: dict[str, Any] | None,
    plan_date: str,
    trade_date: str,
    lookback_days: int,
    allow_same_day_rerun: bool,
    risk_gate_shadow_enabled: bool,
    risk_gate_enforced: bool,
    due_plan_payload: dict[str, Any] | None = None,
    formal_candidate_batch_path: Path | str | None = None,
    formal_candidate_status: str = "",
    publish_next_plan: bool = True,
    review_target_date: str = "",
) -> dict[str, Any]:
    state = _load_state(initial_cash)
    candidate_input_source = (
        "formal_candidate_batch"
        if formal_candidate_status == "passed"
        else (
            f"formal_candidate_batch_{formal_candidate_status}"
            if formal_candidate_status
            else (
                "in_memory"
                if isinstance(auto_strategy_payload, dict)
                else "latest_file_fallback"
            )
        )
    )
    previous_positions = [dict(row) for row in state.get("positions") or []]
    previous_actions = list(state.get("daily_actions") or state.get("actions") or [])
    previous_total_equity = _total_equity_from_state(state)
    previous_valuation_state = copy.deepcopy(state)
    freshness_gate = (
        evaluate_paper_review_freshness(state, target_date=review_target_date, daily_dir=DAILY_DIR)
        if review_target_date else evaluate_data_freshness()
    )
    candidate_intake = dict((auto_strategy_payload or {}).get("candidate_intake") or {})
    if not freshness_gate.get("can_review_yesterday_positions", True):
        decision_date = str(freshness_gate.get("target_complete_trade_date") or now_text())[:10]
        actions: list[dict[str, Any]] = []
        decision_review = build_decision_review(previous_actions, decision_date, freshness_gate)
        enriched_positions = _enrich_positions_with_theme_map(state.get("positions") or [])
        state_for_latest = {**state, "positions": enriched_positions}
        yesterday_review = build_yesterday_holding_review(previous_positions, enriched_positions, actions, decision_review, freshness_gate)
        summary = _summary(state_for_latest, actions)
        summary["previous_total_equity"] = round(previous_total_equity, 2)
        summary["daily_pnl"] = None
        summary["daily_return_pct"] = None
        summary["daily_pnl_status"] = "current_valuation_unverified"
        latest = {
            "ok": True,
            "exists": True,
            "mode": "update",
            "strategy": strategy,
            "generated_at": now_text(),
            "quantity_per_stock": round_down_to_lot(quantity_per_stock),
            "source": "conditional_position_management",
            "input_source": candidate_input_source,
            "candidate_intake": candidate_intake,
            "warnings": list(candidate_intake.get("reason_codes") or []),
            "task_type": "conditional_position_management",
            "scope": "low_price_included_only",
            "network": False,
            "full_scan": False,
            "user_triggered": True,
            "skipped": True,
            "review_mode": "stale_observation",
            "message": "暂无有效复盘：本地完整日线尚未更新到目标完整交易日，不生成新的条件交易计划，不更新自动虚拟组合估值。",
            "summary": summary,
            "positions": enriched_positions,
            "closed_positions": state.get("closed_positions") or [],
            # 陈旧观察也要把主账的减仓明细与**显式**历史差额带全：少了这两把键，
            # _write_auto_paper_latest() 的 reconcile 会用“已实现盈亏 − 明细”反推历史
            # 差额，把刚发生的半卖利润写成 legacy、把 realized_reductions 清空，摘要与
            # 对账随之不一致（T24-LATEST-REDUCTIONS-20260929）。
            "realized_reductions": state.get("realized_reductions") or [],
            "legacy_realized_adjustment": state.get("legacy_realized_adjustment", 0.0),
            "actions": actions,
            "daily_actions": actions,
            "decision_review": decision_review,
            "yesterday_holding_review": yesterday_review,
            "decision_audit": build_decision_audit(actions),
            "auto_candidate_count": 0,
            "intraday_swing_enabled": True,
            "intraday_source": intraday_source,
            "allow_daily_approx": bool(allow_daily_approx),
            "conditional_plan": {"summary": {"symbol_count": 0}, "path": _display_path(_conditional_plan_path())},
            "conditional_execution": {"summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0}, "path": _display_path(_conditional_execution_path())},
            "conditional_review": {
                "ok": False,
                "review_valid": False,
                "missing_intraday_count": 0,
                "explanation": "完整日线控制收益复盘，分钟线控制盘中触发复盘。",
                "reason": freshness_gate.get("reason") or "",
            },
            "data_freshness_gate": freshness_gate,
            "portfolio_review_valid": False,
            "conditional_plan_summary": {"symbol_count": 0},
            "conditional_execution_summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0},
            "dry_run": bool(dry_run),
            "disclaimer": DISCLAIMER,
            "broker_boundary": BROKER_BOUNDARY,
        }
        stale_shadow_records: list[dict[str, Any]] = []
        stale_shadow_summary: dict[str, Any] | None = None
        if risk_gate_shadow_enabled:
            try:
                stale_shadow_summary = summarize_shadow_records(
                    stale_shadow_records,
                    as_of=decision_date,
                    risk_gate_enforced=risk_gate_enforced,
                )
                stale_shadow_summary["observation_reason"] = "stale_data_no_traffic"
            except Exception as exc:
                _record_shadow_failure(
                    stale_shadow_records,
                    surface="auto_paper_stale_shadow_summary",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        if not dry_run:
            if stale_shadow_summary is not None:
                try:
                    write_shadow_summary(
                        stale_shadow_summary,
                        output_path=_risk_gate_shadow_path(),
                    )
                except Exception as exc:
                    _record_shadow_failure(
                        stale_shadow_records,
                        surface="auto_paper_stale_shadow_output",
                        error=exc,
                        as_of=decision_date,
                        write_error=True,
                    )
            latest = _write_auto_paper_latest(latest)
            report_path = _write_report(
                latest,
                risk_gate_shadow_summary=stale_shadow_summary,
            )
            latest["report_path"] = _display_path(report_path)
            latest = _write_auto_paper_latest(latest)
        return latest
    conditional_plan_path = _conditional_plan_path()
    conditional_execution_path = _conditional_execution_path()
    # Round-13 ordering fix: ``build_conditional_trade_plan`` always targets the
    # NEXT trading day (plan_date = today, trade_date = tomorrow). Simulating
    # that freshly built plan immediately was the root cause of the historical
    # evaluation_count=0: the update looked for minute evidence on a date that
    # had not happened yet. The update must replay the DUE plan -- the one
    # archived under today's trade date -- and only build and archive the next
    # plan, never simulate it.
    plan_payload = build_conditional_trade_plan(
        auto_strategy_payload=auto_strategy_payload,
        state=state,
        strategy=strategy,
        quantity_per_stock=quantity_per_stock,
        plan_date=plan_date,
        trade_date=trade_date,
        lookback_days=lookback_days,
        write=False,
        output_path=conditional_plan_path,
        auto_strategy_path=Path(
            (formal_candidate_batch_path if formal_candidate_status == "passed" else None)
            or auto_strategy_path
            or AUTO_STRATEGY_LATEST_JSON
        ),
        state_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
        daily_dir=DAILY_DIR,
        daily_feature_path=DAILY_FEATURE_LATEST_CSV,
        low_price_universe_path=None,
        project_root=_archive_project_root(),
    )
    due_trade_date = str(plan_payload.get("plan_date") or "")[:10]
    archive_root = _archive_project_root()
    due_archive_path = (
        archive_root / "results" / "conditional_trade_plans_daily" / f"{due_trade_date}.json"
        if due_trade_date
        else None
    )
    due_loaded: dict[str, Any]
    if due_plan_payload is not None:
        # The caller (daily workflow orchestrator) already loaded the due plan
        # from the archive; trust its verified payload rather than re-reading.
        due_loaded = {
            "status": "passed",
            "exists": True,
            "plan_id": str(due_plan_payload.get("plan_id") or ""),
            "plan": due_plan_payload,
        }
    elif due_trade_date:
        due_loaded = load_archived_conditional_plan(archive_root, trade_date=due_trade_date)
    else:
        due_loaded = {
            "status": "blocked",
            "reason_codes": ["conditional_plan_archive_trade_date_invalid"],
        }
    due_plan_payload = (
        dict(due_loaded["plan"])
        if due_loaded.get("status") == "passed"
        and isinstance(due_loaded.get("plan"), dict)
        else None
    )
    due_source_plan = copy.deepcopy(due_plan_payload)
    replay_input_evidence = None
    due_plan_enrichment: dict[str, Any] = {
        "status": "not_applicable",
        "target_date": "",
        "formal_batch_path": "",
        "exact_bindable": 0,
        "enriched": 0,
        "existing_valid_envelope": 0,
        "rebound_existing_envelope": 0,
        "unchanged_unmatched": 0,
        "rejected_mismatch": 0,
        "rejections": [],
        "warnings": [],
        "reason_codes": [],
    }
    if due_plan_payload is not None:
        try:
            enriched = enrich_legacy_due_plan_candidate_envelopes(
                archive_root,
                due_plan_payload,
            )
        except Exception as exc:  # noqa: BLE001 - compatibility must fail closed
            enriched = {
                **due_plan_enrichment,
                "status": "blocked",
                "reason_codes": [
                    f"legacy_due_plan_enrichment_failed:{type(exc).__name__}:{exc}"
                ],
                "plan": copy.deepcopy(due_plan_payload),
            }
        due_plan_enrichment = {
            key: copy.deepcopy(value)
            for key, value in enriched.items()
            if key != "plan"
        }
        enriched_plan = enriched.get("plan")
        if isinstance(enriched_plan, dict):
            due_plan_payload = enriched_plan
    if due_plan_payload is not None:
        replay_input_evidence = inspect_plan_minute_inputs(archive_root, due_source_plan, intraday_dir=INTRADAY_DIR / "1m")
        execution_payload = simulate_conditional_plan_file(
            due_plan_payload,
            intraday_dir=INTRADAY_DIR,
            output_path=conditional_execution_path,
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            trigger_model="minute_close",
            write=not dry_run,
        )
    else:
        # No due plan in the archive: nothing is due today, so the honest result
        # is an empty execution (no fake fills), not a simulation of tomorrow.
        execution_payload = {
            "ok": True,
            "skipped": True,
            "status": "no_due_plan",
            "plan_id": str(due_loaded.get("plan_id") or ""),
            "trade_date": due_trade_date,
            "summary": {
                "plan_count": 0,
                "event_count": 0,
                "no_fill_count": 0,
                "blocked_count": 0,
            },
            "events": [],
            "no_fill": [],
            "blocked": [],
            "message": "归档中没有到期计划，未执行盘中模拟。",
            "disclaimer": DISCLAIMER,
        }
        # 这个空结果也必须落盘。不写等于让下游无法区分"这一步没跑"和"今天
        # 没有到期计划"——前者是故障，后者是正常，而缺失文件长得像故障。
        if not dry_run:
            atomic_write_json(conditional_execution_path, execution_payload)
    next_plan_publish: dict[str, Any] | None = None
    if not publish_next_plan:
        next_plan_publish = {
            "status": "skipped",
            "published_latest": False,
            "reason_codes": ["next_plan_publish_disabled_by_caller"],
        }
    elif not dry_run and plan_payload.get("ok") is True:
        # The next plan is archived through the archive-aware publisher, which
        # archives the previous latest and the new plan under its trade date
        # before replacing latest; a bare write over latest would lose the
        # append-only record.
        next_plan_publish = publish_archived_conditional_plan(archive_root, plan_payload)
    decision_date = str(
        execution_payload.get("trade_date")
        or plan_payload.get("trade_date")
        or now_text()
    )[:10]
    adjustment_factors = _load_auto_adjustment_factors([row.get("symbol") for row in state.get("positions") or [] if isinstance(row, dict)])
    risk_gate_shadow_records: list[dict[str, Any]] = []
    apply_result = apply_conditional_execution_to_state(
        state,
        execution_payload,
        daily_dir=DAILY_DIR,
        allow_same_day_rerun=allow_same_day_rerun,
        adjustment_factors=adjustment_factors,
        risk_gate_shadow_enabled=risk_gate_shadow_enabled,
        risk_gate_enforced=risk_gate_enforced,
        risk_gate_shadow_records=risk_gate_shadow_records,
        risk_gate_shadow_output_path=_risk_gate_shadow_path(),
        risk_gate_shadow_error_path=_risk_gate_shadow_error_path(),
        risk_gate_shadow_write_errors=not dry_run,
        sector_rotation_path=_sector_rotation_path(),
        ledger_source_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
        execution_source_path=conditional_execution_path,
        risk_gate_shadow_ledger_snapshot_dir=(
            RESULTS_DIR
            / "risk_gate_shadow_ledger_inputs"
            / decision_date
            / str(execution_payload.get("plan_id") or "unknown_plan")
            if not dry_run
            else None
        ),
    )
    if apply_result.get("already_updated_today"):
        latest_state = apply_result.get("state") if isinstance(apply_result.get("state"), dict) else state
        actions = list(latest_state.get("daily_actions") or [])
    else:
        latest_state = apply_result.get("state") if isinstance(apply_result.get("state"), dict) else state
        actions = list(apply_result.get("actions") or [])
    replay_receipt: dict[str, Any] | None = None
    minute_evidence: dict[str, Any] | None = None
    if due_plan_payload is not None and due_archive_path is not None:
        minute_evidence = intraday_gap_status(
            target_trade_date=due_trade_date,
            source="plans",
            plan_path=due_archive_path,
            write=False,
        )
        if dry_run:
            replay_receipt = build_conditional_replay_receipt(
                execution_payload=execution_payload,
                apply_result=apply_result,
                persisted_state=_load_state(initial_cash),
                minute_evidence=minute_evidence,
                captured_at=now_text(),
            )
        elif apply_result.get("already_updated_today"):
            replay_receipt = build_conditional_replay_receipt(
                execution_payload=execution_payload,
                apply_result=apply_result,
                persisted_state=_load_state(initial_cash),
                minute_evidence=minute_evidence,
                captured_at=now_text(),
            )
    decision_review = build_decision_review(previous_actions, decision_date, freshness_gate)
    latest_state["positions"] = _enrich_positions_with_theme_map(latest_state.get("positions") or [])
    yesterday_review = build_yesterday_holding_review(previous_positions, latest_state.get("positions") or [], actions, decision_review, freshness_gate)
    latest_state["decision_review"] = decision_review
    latest_state["yesterday_holding_review"] = yesterday_review
    latest_state["daily_actions"] = actions
    latest_state["actions"] = actions
    summary = _with_daily_pnl(_summary(latest_state, actions), valuation_date=decision_date,
                              previous_state=previous_valuation_state)
    latest = {
        "ok": True,
        "exists": True,
        "mode": "update",
        "strategy": strategy,
        "generated_at": now_text(),
        "quantity_per_stock": round_down_to_lot(quantity_per_stock),
        "source": "conditional_position_management",
        "valuation_date": decision_date,
        "input_source": candidate_input_source,
        "candidate_intake": candidate_intake,
        "warnings": list(candidate_intake.get("reason_codes") or []),
        "task_type": "conditional_position_management",
        "scope": "low_price_included_only",
        "network": False,
        "full_scan": False,
        "user_triggered": True,
        "already_updated_today": bool(apply_result.get("already_updated_today")),
        "due_plan_id": (
            str(due_plan_payload.get("plan_id") or "")
            if due_plan_payload is not None
            else ""
        ),
        "due_plan_enrichment": due_plan_enrichment,
        "replay_receipt": replay_receipt,
        "next_plan_publish": next_plan_publish,
        "summary": summary,
        "positions": latest_state.get("positions") or [],
        "closed_positions": latest_state.get("closed_positions") or [],
        "realized_reductions": latest_state.get("realized_reductions") or [],
        "legacy_realized_adjustment": latest_state.get("legacy_realized_adjustment", 0.0),
        "actions": actions,
        "daily_actions": actions,
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "decision_audit": build_decision_audit(actions),
        "auto_candidate_count": plan_payload.get("summary", {}).get("new_candidate_plan_count", 0),
        "intraday_swing_enabled": True,
        "intraday_source": intraday_source,
        "allow_daily_approx": bool(allow_daily_approx),
        "conditional_plan": {
            "plan_id": plan_payload.get("plan_id"),
            "plan_date": plan_payload.get("plan_date"),
            "trade_date": plan_payload.get("trade_date"),
            "source_data_cutoff": plan_payload.get("source_data_cutoff"),
            "summary": plan_payload.get("summary"),
            "path": _display_path(conditional_plan_path),
        },
        "conditional_execution": {
            "plan_id": execution_payload.get("plan_id"),
            "trade_date": execution_payload.get("trade_date"),
            "summary": execution_payload.get("summary"),
            "path": _display_path(conditional_execution_path),
        },
        "conditional_review": _conditional_review_summary(execution_payload, actions),
        "data_freshness_gate": freshness_gate,
        "portfolio_review_valid": bool(freshness_gate.get("can_review_yesterday_positions", True)),
        "conditional_plan_summary": plan_payload.get("summary", {}),
        "conditional_execution_summary": execution_payload.get("summary", {}),
        "dry_run": bool(dry_run),
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
    }
    risk_gate_shadow_summary: dict[str, Any] | None = None
    if risk_gate_shadow_enabled:
        try:
            risk_gate_shadow_summary = summarize_shadow_records(
                risk_gate_shadow_records,
                as_of=decision_date,
                risk_gate_enforced=risk_gate_enforced,
            )
        except Exception as exc:
            _record_shadow_failure(
                risk_gate_shadow_records,
                surface="auto_paper_shadow_summary",
                error=exc,
                as_of=decision_date,
                write_error=not dry_run,
            )
    persisted_apply_state: dict[str, Any] | None = None
    if dry_run:
        _attach_bound_replay_review(
            latest, source_plan=due_source_plan, replay_receipt=replay_receipt,
            input_evidence=replay_input_evidence,
            applied_ids=list(latest_state.get("applied_execution_ids") or []), dry_run=True,
        )
    if not dry_run:
        if risk_gate_shadow_summary is not None:
            try:
                write_shadow_summary(
                    risk_gate_shadow_summary,
                    output_path=_risk_gate_shadow_path(),
                )
            except Exception as exc:
                _record_shadow_failure(
                    risk_gate_shadow_records,
                    surface="auto_paper_shadow_output",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        compat_artifacts = _write_conditional_compat_artifacts(plan_payload, execution_payload)
        latest["swing_trade_plans_path"] = _display_path(SWING_TRADE_PLANS_LATEST_JSON)
        latest["intraday_execution_path"] = _display_path(INTRADAY_EXECUTION_LATEST_JSON)
        latest["swing_execution_report_path"] = _display_path(compat_artifacts["report_path"])
        if not apply_result.get("already_updated_today"):
            persisted_apply_state = _write_auto_paper_state(latest_state)
        if (
            replay_receipt is None
            and due_plan_payload is not None
            and due_archive_path is not None
            and minute_evidence is not None
        ):
            receipt_apply_result = dict(apply_result)
            if persisted_apply_state is not None:
                receipt_apply_result["state"] = persisted_apply_state
            replay_receipt = build_conditional_replay_receipt(
                execution_payload=execution_payload,
                apply_result=receipt_apply_result,
                persisted_state=_auto_paper_state_store().read(default={}),
                minute_evidence=minute_evidence,
                captured_at=now_text(),
            )
            latest["replay_receipt"] = replay_receipt
        _attach_bound_replay_review(
            latest, source_plan=due_source_plan, replay_receipt=replay_receipt,
            input_evidence=replay_input_evidence,
            applied_ids=list((persisted_apply_state or latest_state).get("applied_execution_ids") or []), dry_run=False,
        )
        if not apply_result.get("already_updated_today"):
            latest = _write_auto_paper_latest(latest)
            history = _append_history(latest)
            latest["history_path"] = _display_path(AUTO_PAPER_PORTFOLIO_HISTORY_JSON)
            latest["history_count"] = len(history.get("items") or [])
        else:
            latest["message"] = apply_result.get("message")
        report_path = _write_report(
            latest,
            risk_gate_shadow_summary=risk_gate_shadow_summary,
        )
        latest["report_path"] = _display_path(report_path)
        latest = _write_auto_paper_latest(latest)
    return latest


def _snapshot_daily_quotes(symbols: list[str], decision_date: str) -> dict[str, dict[str, Any]]:
    """Read original frames, validate the set, then select an exact as-of row."""

    frames: dict[str, pd.DataFrame] = {}
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    read_errors: dict[str, str] = {}
    for symbol in dict.fromkeys(symbols):
        path = DAILY_DIR / f"{normalize_symbol(symbol)}.csv"
        paths[symbol] = path
        if path.is_file():
            # The hash binds the exact bytes parsed here, not a later reread.
            import io

            try:
                raw = path.read_bytes()
                hashes[symbol] = hashlib.sha256(raw).hexdigest()
                frames[symbol] = pd.read_csv(io.BytesIO(raw), dtype={"symbol": str})
            except (OSError, ValueError) as exc:
                frames[symbol] = pd.DataFrame()
                read_errors[symbol] = f"snapshot_signal_daily_unreadable:{type(exc).__name__}"
        else:
            frames[symbol] = pd.DataFrame()
    if not frames:
        return {}
    # Use the executable raw-price gate for both signal references and marks;
    # the contract's paper_valuation entry describes adjustment metadata only.
    gate = validate_price_frame_set(frames, consumer="paper_execution")
    quotes = {}
    for symbol, frame in frames.items():
        quote: dict[str, Any] = {"source": str(paths[symbol]), "source_sha256": hashes.get(symbol, ""),
                                 "date": "", "price": None, "valid": False, "price_basis": "raw",
                                 "warnings": []}
        if not gate["eligible"]:
            quote["warnings"] = ["snapshot_signal_price_set_blocked", *gate["reason_codes"]]
            if symbol in read_errors:
                quote["warnings"].append(read_errors[symbol])
        elif "date" not in frame:
            quote["warnings"] = ["snapshot_signal_date_missing"]
        else:
            dates = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            selected = frame.loc[dates == decision_date]
            if len(selected) != 1:
                quote["warnings"] = ["snapshot_signal_daily_row_missing_or_ambiguous"]
            else:
                row = selected.iloc[0].to_dict()
                valid, reason = daily_bar_validity_from_row(row)
                valid = valid and all(safe_float(row.get(field), None) is not None
                                      for field in ("open", "high", "low", "close", "volume"))
                price = safe_float(row.get("close"), None)
                quote.update(date=decision_date, price=price,
                             valid=bool(valid and price is not None and price > 0),
                             source_row={key: (None if pd.isna(value) else value) for key, value in row.items()})
                if not quote["valid"]:
                    quote["warnings"] = [f"snapshot_signal_daily_invalid:{reason}"]
        quotes[symbol] = quote
    return quotes


def _snapshot_failure(reason: str) -> dict[str, Any]:
    return {"ok": False, "status": "blocked", "error": reason,
            "strategy_fill_mode": open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
            "ledger_write": False, "warnings": [reason], **_paper_action_boundary(),
            "disclaimer": DISCLAIMER, "broker_boundary": BROKER_BOUNDARY}


def _publish_snapshot_result(result: dict[str, Any]) -> dict[str, Any]:
    latest = _write_auto_paper_latest(result)
    history = _append_history(latest)
    latest["history_path"] = _display_path(AUTO_PAPER_PORTFOLIO_HISTORY_JSON)
    latest["history_count"] = len(history.get("items") or [])
    latest["report_path"] = _display_path(_write_report(latest))
    return _write_auto_paper_latest(latest)


def _run_next_open_snapshot_update(
    *, mode: str, initial_cash: float, quantity: int, strategy: str,
    decision_date: str, candidates: list[dict[str, Any]], intake: dict[str, Any],
    dry_run: bool, force_reset: bool, snapshot_root: Path | None, journal_path: Path | None,
) -> dict[str, Any]:
    """Opt-in after-close workflow; legacy matching never enters this path."""

    snapshot_root = Path(snapshot_root) if snapshot_root is not None else PROJECT_ROOT / "data/snapshots"
    journal_path = Path(journal_path) if journal_path is not None else AUTO_PAPER_PORTFOLIO_STATE_JSON.with_suffix(".fills.jsonl")
    ledger_existed = AUTO_PAPER_PORTFOLIO_STATE_JSON.exists()
    if force_reset and (ledger_existed or journal_path.exists()):
        return _snapshot_failure("snapshot_reset_requires_new_ledger_and_journal")
    try:
        # Do not let the legacy store recover a backup behind the journal's back.
        current = json.loads(AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8")) if ledger_existed else None
        if current is not None and (not isinstance(current, dict) or _paper_state_shape_errors(current)):
            return _snapshot_failure("snapshot_ledger_invalid")
        recovered = fill_journal.recoverable_state(journal_path, current)
        if not recovered["records"] and current and current.get("pending_orders"):
            return _snapshot_failure("snapshot_pending_orders_have_no_journal_proof")
        if current and current.get("strategy_fill_mode") == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT and not recovered["records"]:
            return _snapshot_failure("snapshot_journal_missing_for_active_ledger")
        state = copy.deepcopy(recovered["state"] if recovered["state"] is not None else reconcile_paper_account(_empty_state(initial_cash)))
        if any(str(row.get("entry_date") or row.get("buy_date") or "")[:10] > decision_date
               for row in state.get("positions") or []):
            return _snapshot_failure("snapshot_position_entry_is_in_future")
        transaction_id = f"update:{decision_date}"
        if recovered["records"] and recovered["records"][-1]["transaction_id"] == transaction_id:
            result = copy.deepcopy(recovered["records"][-1]["result"])
            result.update(idempotent_replay=True, journal_recovery_status=recovered["status"], dry_run=bool(dry_run))
            if not dry_run:
                if recovered["status"].startswith("recovered_"):
                    _auto_paper_state_store().write(state)
                return _publish_snapshot_result(result)
            return result
        if str(state.get("snapshot_decision_date") or "") > decision_date:
            return _snapshot_failure("snapshot_decision_date_would_go_backwards")
        recorded_at = utc_now_iso()
        observed = open_fill.aware_timestamp(recorded_at)
        calendar = load_a_share_trading_calendar(PROJECT_ROOT / "data/calendar/a_share_trading_calendar.csv")
        next_date = open_fill.next_trading_day(decision_date, calendar)
        lower = datetime.combine(date.fromisoformat(decision_date), time(15), open_fill.SHANGHAI)
        upper = datetime.combine(date.fromisoformat(next_date), time(9, 30), open_fill.SHANGHAI)
        if not lower <= observed < upper:
            return _snapshot_failure("snapshot_signal_recording_window_closed")
        if not dry_run and recovered["status"].startswith("recovered_"):
            _auto_paper_state_store().write(state)
    except (OSError, ValueError, TypeError) as exc:
        return _snapshot_failure(f"snapshot_journal_or_clock_invalid:{exc}")

    before = copy.deepcopy(state)
    positions = copy.deepcopy(state.get("positions") or [])
    closed = copy.deepcopy(state.get("closed_positions") or [])
    cash = float(state["cash"])
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    warnings = [*state.get("warnings", []), *intake.get("reason_codes", []),
                "next_open_snapshot_opt_in_unvalidated", "raw_open_paper_proxy_liquidity_unverified"]
    if state.get("strategy_fill_mode") != open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
        old_header = {key: value for key, value in state.items() if key.startswith("fill_policy")}
        transition = {"from": old_header, "to": POLICY_NEXT_OPEN, "recorded_at": recorded_at,
                      "opening_state_sha256": fill_journal.state_digest(before),
                      "scope": "future_fills_only", "inherited_history_status": "legacy_unverified" if ledger_existed else "empty_book"}
        # An explicit, journaled policy transition; old rows and opening state stay intact.
        state.update(policy_header(POLICY_NEXT_OPEN))
        state["fill_policy_transition"] = transition
        state["fill_policy_scope"] = "future_fills_since_opening_checkpoint"
        events.append({"event_type": "policy_transition", **transition})
        if ledger_existed:
            warnings.append("inherited_strategy_history_not_reconstructed_or_relabelled")
    state["strategy_fill_mode"] = open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT
    pending = copy.deepcopy(state.get("pending_orders") or [])
    remaining: list[dict[str, Any]] = []
    due = [row for row in pending if str(row.get("fill_date") or "") == decision_date]
    snapshot = open_fill.load_open_snapshot(snapshot_root, decision_date) if due else {}
    basis_gate = open_fill.snapshot_price_gate(snapshot, [row["symbol"] for row in due]) if due else {}
    # Preserve signal order within each side; opening sells release cash before buys.
    for order in sorted(pending, key=lambda item: 0 if item.get("side") == "sell" else 1):
        fill_date = str(order.get("fill_date") or "")
        if fill_date > decision_date:
            remaining.append(order)
            continue
        if fill_date < decision_date:
            evaluated = {"ok": False, "status": "expired", "reason_code": "missed_only_fill_session",
                         "reason": "已错过唯一成交日；不补做历史成交、不顺延。"}
        else:
            evaluated = open_fill.evaluate_open_fill(order, snapshot, decision_date,
                                                      calendar=calendar, observed_at=recorded_at)
            if evaluated["ok"] and not basis_gate.get("eligible"):
                evaluated.update(ok=False, status="rejected", reason_code="snapshot_universe_price_basis_blocked",
                                 reason="请求集合中存在未知或非原始价格；整组不成交。", price=None)
        key = _position_key(order)
        position = next((row for row in positions if _position_key(row) == key), None)
        requested = safe_float(order.get("quantity"), None)
        error = ""
        if evaluated["ok"]:
            if requested is None or isinstance(order.get("quantity"), bool) or requested != int(requested):
                error = "invalid_order_quantity"
            else:
                error = order_quantity_rule(order["symbol"], int(requested),
                                             held_shares=position.get("quantity") if position and order["side"] == "sell" else None,
                                             name=order.get("name"))
            if not error and order["side"] == "buy" and position:
                error = "already_held_at_open"
            if not error and order["side"] == "sell":
                entry = str((position or {}).get("entry_date") or (position or {}).get("buy_date") or "")[:10]
                if not position or int(requested) != safe_int(position.get("quantity"), 0):
                    error = "sell_quantity_does_not_match_holding"
                elif not entry or entry >= decision_date:
                    error = "t_plus_one_sell_blocked"
            if error:
                evaluated.update(ok=False, status="rejected", reason_code=error, reason=error, price=None)
        amounts: dict[str, Any] = {}
        price = evaluated.get("price") if evaluated["ok"] else None
        if evaluated["ok"]:
            q = int(requested)
            costs = compute_trade_costs(side=order["side"], gross=price * q)
            if order["side"] == "buy":
                flow = buy_cash_outflow(price=price, quantity=q)
                if flow["cash_out"] > cash:
                    evaluated.update(ok=False, status="rejected", reason_code="insufficient_open_cash",
                                     reason="开盘价与原费用合计超过可用现金，本单不成交。", price=None)
                    price = None
                else:
                    cash -= flow["cash_out"]
                    position = _make_position(order.get("candidate") or order, q, price, decision_date, flow)
                    position.update(entry_fill_policy=POLICY_NEXT_OPEN, entry_fill_mode=open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
                                    entry_fill_evidence=copy.deepcopy(evaluated["evidence"]),
                                    entry_order_id=order["order_id"], zero_pnl_explanation="")
                    positions.append(position)
                    action = _buy_action(order, q, price, decision_date, cash)
                    amounts = {**costs, "gross": flow["gross"], "cash_delta": -flow["cash_out"]}
            else:
                sold = _close_position(position, price, decision_date, order.get("reason") or "次日开盘纸面退出")
                sold.update(exit_fill_policy=POLICY_NEXT_OPEN, exit_fill_mode=open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
                            exit_fill_evidence=copy.deepcopy(evaluated["evidence"]), exit_order_id=order["order_id"])
                flow = sell_cash_inflow(price=price, quantity=q, avg_cost=float(position["entry_price"]))
                cash += flow["cash_in"]
                closed.append(sold)
                positions.remove(position)
                action = _sell_action(position, price, decision_date, order.get("reason") or "次日开盘纸面退出")
                amounts = {**costs, "gross": flow["gross"], "cash_delta": flow["cash_in"]}
            if evaluated["ok"]:
                action.update(order_id=order["order_id"], signal_date=order["signal_date"],
                              fill_date=decision_date, fill_model=open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
                              fill_policy=POLICY_NEXT_OPEN, fill_evidence=copy.deepcopy(evaluated["evidence"]),
                              total_fee=costs["total_fee"], fill_quality="raw_open_paper_proxy", liquidity_verified=False)
                actions.append(action)
        if not evaluated["ok"]:
            action = _block_action(order, str(evaluated.get("reason") or evaluated["reason_code"]), decision_date)
            action.update(order_id=order["order_id"], status=evaluated["status"], reason_code=evaluated["reason_code"])
            actions.append(action)
            warnings.append(f"{order['order_id']}:{evaluated['reason_code']}")
        events.append(open_fill.fill_journal_row(order, status=evaluated["status"], decision_date=decision_date,
                                                 reason_code=evaluated.get("reason_code", ""), reason=evaluated.get("reason", ""),
                                                 price=price, quantity=int(requested) if evaluated["ok"] else 0,
                                                 evidence=evaluated.get("evidence"), limits=evaluated.get("limits"),
                                                 amounts=amounts, recorded_at=recorded_at))

    symbols = [normalize_symbol(row.get("symbol")) for row in [*positions, *candidates]]
    quotes = _snapshot_daily_quotes(symbols, decision_date)
    factors = _load_auto_adjustment_factors(symbols)
    candidate_keys = {_position_key(row) for row in candidates}
    pending_keys = {_position_key(row) for row in remaining}
    calendar_evidence = {"is_open_by_date": {key: value for key, value in calendar["is_open_by_date"].items()
                                            if decision_date <= key <= next_date}}

    def queue(side: str, row: dict[str, Any], q: int, price: float, reason: str) -> None:
        order = open_fill.make_pending_order(side, row, signal_date=decision_date, fill_date=next_date,
                                              quantity=q, reason=reason, reference_price=price,
                                              candidate=row if side == "buy" else None, recorded_at=recorded_at)
        order["session_calendar_evidence"] = copy.deepcopy(calendar_evidence)
        order["signal_evidence"] = copy.deepcopy(quotes[normalize_symbol(row["symbol"])])
        remaining.append(order)
        pending_keys.add(_position_key(order))
        events.append({"event_type": "order_submitted", "order": copy.deepcopy(order)})
        actions.append({**_block_action(row, reason, decision_date), "action": f"pending_{side}",
                        "action_label": "待下一交易日开盘核验", "quantity": q,
                        "order_id": order["order_id"], "fill_date": next_date, "price": None,
                        "signal_reference_price": price, "status": "pending"})

    marked = []
    valuation_valid = True
    for position in positions:
        quote = quotes[_position_key(position)[0]]
        warnings.extend(quote["warnings"])
        if not quote["valid"]:
            valuation_valid = False
            marked.append(position)
            actions.append(_block_action(position, "当日原始价格缺失或未核验，保留旧估值；不生成卖出意向。", decision_date))
            continue
        price = float(quote["price"])
        reason = "仍在正式候选，继续纸面观察。" if _position_key(position) in candidate_keys else "未进入正式候选，保留退出观察。"
        marked_position = _mark_position(copy.deepcopy(position), price, decision_date, reason,
                                         adjustment_factors=factors, quote=quote)
        marked_position["zero_pnl_explanation"] = ""
        marked.append(marked_position)
        events.append({"event_type": "valuation", "symbol": position["symbol"],
                       "strategy_type": position["strategy_type"], "quote": quote})
        entry = safe_float(position.get("entry_price"), None)
        if entry and price <= entry * 0.92 and _position_key(position) not in pending_keys:
            queue("sell", position, int(position["quantity"]), price,
                  "沿用收盘回撤超过 8% 的复核规则；只记录下一交易日开盘卖出意向。")
        else:
            actions.append(_hold_action(marked_position, price, decision_date, reason))
    positions = marked
    held_keys = {_position_key(row) for row in positions}
    for candidate in candidates:
        key = _position_key(candidate)
        if key in held_keys or key in pending_keys:
            continue
        quote = quotes[key[0]]
        warnings.extend(quote["warnings"])
        rule = order_quantity_rule(candidate["symbol"], quantity, name=candidate.get("name"))
        reference = _candidate_price(candidate)
        if (not quote["valid"] or rule or reference is None
                or abs(reference - float(quote["price"] or 0)) > 0.0001):
            actions.append(_block_action(candidate, rule or "正式候选参考价与当日原始价格证据未对齐，不记录待成交意向。", decision_date))
            continue
        queue("buy", candidate, quantity, float(quote["price"]),
              "正式候选只生成待成交意向；现金与涨跌停于下一交易日真实开盘证据到达后检查。")

    state.update(cash=round(cash, 2), positions=positions, closed_positions=closed, pending_orders=remaining,
                 updated_at=recorded_at, snapshot_decision_date=decision_date, valuation_date=decision_date,
                 portfolio_review_valid=valuation_valid, actions=actions, daily_actions=actions,
                 warnings=list(dict.fromkeys(warnings)))
    state = reconcile_paper_account(state)
    summary = _with_daily_pnl(_summary(state, actions), valuation_date=decision_date, previous_state=before,
                              valuation_valid=valuation_valid, calendar=calendar)
    state["daily_pnl_reference"] = _daily_pnl_reference(summary, decision_date)
    if any(event.get("status") == "filled" for event in events):
        state["last_trade_date"] = decision_date
    result = {"ok": True, "exists": True, "mode": mode, "strategy": strategy, "generated_at": recorded_at,
              "valuation_date": decision_date, "strategy_fill_mode": open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
              "strategy_journal_transaction": transaction_id, "fill_journal_path": str(journal_path),
              "fill_policy": POLICY_NEXT_OPEN, "fill_policy_scope": state["fill_policy_scope"],
              "validation_status": "研究输出·未验证", "summary": summary, "positions": positions,
              "closed_positions": closed, "realized_reductions": state.get("realized_reductions", []),
              "legacy_realized_adjustment": state.get("legacy_realized_adjustment", 0),
              "pending_orders": remaining, "actions": actions, "daily_actions": actions,
              "decision_audit": build_decision_audit(actions), "warnings": state["warnings"],
              "portfolio_review_valid": valuation_valid, "source": "formal_candidate_daily_batch",
              "input_source": str(intake.get("input_source") or ""), "auto_candidate_count": len(candidates),
              "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
              "snapshot_outcomes": [event for event in events if event["event_type"] == "order_outcome"],
              "dry_run": bool(dry_run), "journal_recovery_status": recovered["status"],
              "disclaimer": DISCLAIMER, "broker_boundary": BROKER_BOUNDARY, **_paper_action_boundary()}
    # Even a preview must satisfy the same economic checks; it simply writes nothing.
    try:
        fill_journal.validate_transaction(before, state, events)
        if not dry_run:
            committed = fill_journal.commit_transaction(journal_path, before=before, after=state, events=events,
                                                         transaction_id=transaction_id, recorded_at=recorded_at,
                                                         result=result, ledger_existed=ledger_existed)
            _auto_paper_state_store().write(committed["state"])
            result = _publish_snapshot_result(result)
    except (OSError, ValueError, TypeError) as exc:
        failure = _snapshot_failure(f"snapshot_transaction_incomplete:{exc}")
        failure["journal_may_require_recovery"] = not dry_run
        return failure
    return result


def _run_auto_paper_portfolio_unlocked(
    *,
    mode: str = "init",
    initial_cash: float = 10_000.0,
    quantity_per_stock: int = 100,
    strategy: str = "all",
    enable_intraday_swing: bool = False,
    intraday_source: str = "1m",
    allow_daily_approx: bool = False,
    dry_run: bool = False,
    force_reset: bool = False,
    auto_strategy_path: Path | None = None,
    build_conditional_plan: bool = False,
    simulate_conditional_plan: bool = False,
    plan_date: str = "",
    trade_date: str = "",
    lookback_days: int = 250,
    allow_same_day_rerun: bool = False,
    auto_strategy_payload: dict[str, Any] | None = None,
    risk_gate_shadow_enabled: bool = True,
    risk_gate_enforced: bool = False,
    due_plan_payload: dict[str, Any] | None = None,
    formal_candidate_batch: dict[str, Any] | None = None,
    formal_candidate_batch_path: Path | str | None = None,
    formal_candidate_status: str = "",
    publish_next_plan: bool = True,
    review_target_date: str = "",
    candidate_mode: str = "formal",
    strategy_fill_mode: str | None = None,
    snapshot_root: Path | None = None,
    fill_journal_path: Path | None = None,
) -> dict[str, Any]:
    """Initialize or update the persistent automatic paper portfolio."""

    fill_mode, fill_mode_warnings = open_fill.resolve_strategy_fill_mode(strategy_fill_mode)
    if fill_mode == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT and (
        enable_intraday_swing or build_conditional_plan or simulate_conditional_plan or risk_gate_enforced
    ):
        return _snapshot_failure("snapshot_mode_incompatible_with_conditional_or_enforced_risk_path")
    _SHADOW_UNRECORDED.clear()  # one run's unrecorded shadow failures, not a previous run's
    mode = "update" if str(mode) == "update" else "init"
    quantity = round_down_to_lot(quantity_per_stock)
    if candidate_mode == "legacy_diagnostic":
        diagnostic = build_conditional_trade_plan(
            auto_strategy_payload=auto_strategy_payload, candidate_mode=candidate_mode,
            strategy=strategy, quantity_per_stock=quantity_per_stock,
            plan_date=plan_date, trade_date=trade_date, lookback_days=lookback_days,
            write=False, auto_strategy_path=auto_strategy_path or AUTO_STRATEGY_LATEST_JSON,
            state_path=AUTO_PAPER_PORTFOLIO_STATE_JSON, daily_dir=DAILY_DIR,
            daily_feature_path=DAILY_FEATURE_LATEST_CSV, low_price_universe_path=None,
            project_root=_archive_project_root(),
        )
        diagnostic.update({"mode": "diagnostic", "dry_run": True, "input_source": "legacy_diagnostic", "ledger_write": False})
        return diagnostic
    if candidate_mode != "formal":
        raise ValueError("candidate_mode must be formal or legacy_diagnostic")
    supplied = auto_strategy_payload if isinstance(auto_strategy_payload, dict) else {}
    formal_candidate_batch = formal_candidate_batch if formal_candidate_batch is not None else supplied.get("formal_candidate_batch")
    formal_candidate_batch_path = formal_candidate_batch_path or supplied.get("formal_candidate_batch_path")
    formal_candidate_status = formal_candidate_status or str(supplied.get("formal_candidate_status") or "")
    candidate_target = str(plan_date or review_target_date or (formal_candidate_batch or {}).get("target_date") or infer_target_date()[0])[:10]
    intake = load_formal_candidate_intake(
        _archive_project_root(), target_date=candidate_target, batch=formal_candidate_batch,
        batch_path=formal_candidate_batch_path, status=formal_candidate_status,
    )
    if supplied and "formal_candidate_batch" not in supplied:
        intake["reason_codes"] = [*intake.get("reason_codes", []), "legacy_candidate_payload_ignored"]
    auto_payload = formal_candidate_strategy_payload(intake)
    auto_strategy_input_source = str(intake["input_source"])
    formal_candidate_batch_path = intake.get("path") or None
    formal_candidate_status = str(intake.get("status") or "blocked")
    review_target_date = review_target_date or candidate_target
    plan_date = plan_date or candidate_target
    candidates = candidate_rows_from_strategy_payload(auto_payload, strategy=strategy)
    decision_date = str(auto_payload.get("generated_at") or now_text())[:10]
    candidate_source_for_risk = (
        formal_candidate_batch_path
        or auto_strategy_path
        or AUTO_STRATEGY_LATEST_JSON
    )

    def _risk_candidate(row: dict[str, Any]) -> dict[str, Any]:
        envelope = row.get("candidate_envelope") if isinstance(row, dict) else None
        original = envelope.get("candidate") if isinstance(envelope, dict) else None
        return original if isinstance(original, dict) else row
    if mode == "init" and AUTO_PAPER_PORTFOLIO_STATE_JSON.exists() and not force_reset and not dry_run:
        return {
            "ok": False,
            "exists": True,
            "error": "portfolio_exists",
            "message": "自动虚拟组合已存在。初始化会重置当前组合并丢失连续复盘历史。请改用‘更新今日虚拟组合’。如确需重置，请使用 --force-reset。",
            "state_path": str(AUTO_PAPER_PORTFOLIO_STATE_JSON),
            "disclaimer": DISCLAIMER,
            "broker_boundary": BROKER_BOUNDARY,
        }
    loaded_update_state: dict[str, Any] | None = None
    if fill_mode == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
        return _run_next_open_snapshot_update(
            mode=mode, initial_cash=initial_cash, quantity=quantity, strategy=strategy,
            decision_date=candidate_target, candidates=candidates, intake=intake,
            dry_run=dry_run, force_reset=force_reset, snapshot_root=snapshot_root, journal_path=fill_journal_path,
        )
    if mode == "update":
        loaded_update_state = _load_state(initial_cash)
        if loaded_update_state.get("strategy_fill_mode") == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
            return _snapshot_failure("snapshot_ledger_requires_explicit_snapshot_mode")
        reconciliation = loaded_update_state.get("accounting_reconciliation")
        if (
            isinstance(reconciliation, dict)
            and str(reconciliation.get("status") or "").lower() == "invalid"
        ):
            return _invalid_paper_account_payload(loaded_update_state)
    if build_conditional_plan:
        state_for_plan = loaded_update_state or _load_state(initial_cash)
        payload = build_conditional_trade_plan(
            auto_strategy_payload=auto_payload,
            state=state_for_plan,
            strategy=strategy,
            quantity_per_stock=quantity_per_stock,
            plan_date=plan_date,
            trade_date=trade_date,
            lookback_days=lookback_days,
            write=not dry_run,
            output_path=_conditional_plan_path(),
            auto_strategy_path=Path(
                (formal_candidate_batch_path if formal_candidate_status == "passed" else None)
                or auto_strategy_path
                or AUTO_STRATEGY_LATEST_JSON
            ),
            state_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
            daily_dir=DAILY_DIR,
            daily_feature_path=DAILY_FEATURE_LATEST_CSV,
            low_price_universe_path=None,
            project_root=_archive_project_root(),
        )
        payload.update({
            "task_type": "conditional_position_management",
            "scope": "formal_candidates_and_positions",
            "network": False,
            "full_scan": False,
            "user_triggered": True,
            "dry_run": bool(dry_run),
            "input_source": auto_strategy_input_source,
            "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
        })
        return payload
    if simulate_conditional_plan:
        plan_payload = conditional_trade_plan_latest(_conditional_plan_path())
        payload = simulate_conditional_plan_file(
            plan_payload,
            intraday_dir=INTRADAY_DIR,
            output_path=_conditional_execution_path(),
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            trigger_model="minute_close",
            write=not dry_run,
        )
        payload.update({
            "task_type": "conditional_position_management",
            "scope": "low_price_included_only",
            "network": False,
            "full_scan": False,
            "user_triggered": True,
            "dry_run": bool(dry_run),
        })
        return payload
    if mode == "update" and enable_intraday_swing:
        return _run_conditional_auto_paper_update(
            initial_cash=initial_cash,
            quantity_per_stock=quantity_per_stock,
            strategy=strategy,
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            dry_run=dry_run,
            auto_strategy_path=auto_strategy_path,
            auto_strategy_payload=auto_payload,
            plan_date=plan_date,
            trade_date=trade_date,
            lookback_days=lookback_days,
            allow_same_day_rerun=allow_same_day_rerun,
            risk_gate_shadow_enabled=risk_gate_shadow_enabled,
            risk_gate_enforced=risk_gate_enforced,
            due_plan_payload=due_plan_payload,
            formal_candidate_batch_path=formal_candidate_batch_path,
            formal_candidate_status=formal_candidate_status,
            publish_next_plan=publish_next_plan,
            review_target_date=review_target_date,
        )
    state = (
        _empty_state(initial_cash)
        if mode == "init"
        else loaded_update_state or _load_state(initial_cash)
    )
    previous_valuation_state = copy.deepcopy(state)
    delist_liquidation = {
        "ok": True,
        "liquidated": 0,
        "coverage": "not_checked",
        "warnings": [],
        "actions": [],
    }
    if mode == "update":
        liquidation_payload = apply_delisting_liquidation(state, as_of=decision_date)
        if isinstance(liquidation_payload, dict):
            state = liquidation_payload.get("state", state)
            delist_liquidation = {key: value for key, value in liquidation_payload.items() if key != "state"}
    positions = list(state.get("positions") or [])
    closed_positions = list(state.get("closed_positions") or [])
    previous_positions = [dict(row) for row in positions]
    previous_actions = list(state.get("daily_actions") or state.get("actions") or [])
    actions: list[dict[str, Any]] = []
    actions.extend(delist_liquidation.get("actions") or [])
    warnings: list[str] = [str(item) for item in state.get("warnings") or []] + list(intake.get("reason_codes") or [])
    warnings.extend(fill_mode_warnings)
    cash_value = safe_float(state.get("cash"), None)
    if cash_value is None:
        cash = 0.0
        warnings.append("funded_ledger_cash_invalid: paper cash was set to zero instead of initial-capital fallback")
    else:
        cash = float(cash_value)
    candidate_by_key = {_position_key(row): row for row in candidates}
    existing_keys = {_position_key(row) for row in positions}
    swing_plans: list[dict[str, Any]] = []
    intraday_executions: list[dict[str, Any]] = []
    adjustment_factors = _load_auto_adjustment_factors([row.get("symbol") for row in list(positions) + list(candidates) if isinstance(row, dict)])
    risk_gate_shadow_records: list[dict[str, Any]] = []
    shadow_ledger_event_ordinal = 0

    def bind_shadow_pretrade_ledger(
        shadow_state: dict[str, Any],
        *,
        surface: str,
        symbol: Any,
    ) -> Path:
        nonlocal shadow_ledger_event_ordinal
        if dry_run:
            return AUTO_PAPER_PORTFOLIO_STATE_JSON
        shadow_ledger_event_ordinal += 1
        return _write_shadow_ledger_snapshot_once(
            RESULTS_DIR
            / "risk_gate_shadow_ledger_inputs"
            / decision_date
            / surface,
            shadow_state,
            event_ordinal=shadow_ledger_event_ordinal,
            symbol=normalize_symbol(symbol),
            base_ledger_source_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
            execution_source_path=None,
        )

    def record_legacy_shadow_block(candidate: dict[str, Any], reason: str, surface: str) -> None:
        if not risk_gate_shadow_enabled:
            return
        try:
            risk_gate_shadow_records.append(
                build_legacy_blocked_shadow_record(
                    candidate=candidate,
                    as_of=decision_date,
                    reason=reason,
                    surface=surface,
                    risk_gate_enforced=risk_gate_enforced,
                )
            )
        except Exception as exc:
            _record_shadow_failure(
                risk_gate_shadow_records,
                surface=f"{surface}_legacy_block_record",
                error=exc,
                as_of=decision_date,
                write_error=not dry_run,
            )

    if mode == "update":
        updated_positions: list[dict[str, Any]] = []
        for position in positions:
            key = _position_key(position)
            candidate = candidate_by_key.get(key)
            if candidate:
                position["sector"] = position.get("sector") or candidate.get("sector") or "未知板块"
                position["primary_theme"] = position.get("primary_theme") or candidate.get("primary_theme") or "未知题材"
            entry_date_for_t1 = str(position.get("entry_date") or position.get("buy_date") or position.get("first_buy_date") or "")[:10]
            if entry_date_for_t1 and entry_date_for_t1 >= decision_date:
                quote = _latest_daily_quote(position.get("symbol"))
                warnings.extend(_quote_warnings(position.get("symbol"), quote))
                latest_price = (
                    safe_float(quote.get("price"), None)
                    or safe_float(position.get("latest_price"), None)
                    or safe_float(position.get("entry_price"), 0.0)
                    or 0.0
                )
                reason = "A股 T+1 规则：纸面持仓为当日新建，今日不假设卖出成交。"
                updated = _mark_position(
                    dict(position),
                    latest_price,
                    decision_date,
                    reason,
                    adjustment_factors=adjustment_factors,
                    quote=quote,
                )
                action = _hold_action(updated, latest_price, decision_date, reason)
                action["t_plus_one_sell_blocked"] = True
                actions.append(action)
                updated_positions.append(updated)
                continue
            if enable_intraday_swing and _is_long_strategy(position):
                swing_quantity = safe_int(position.get("quantity"), quantity) or quantity
                plan, execution = _simulate_swing_for_row(
                    {**position, **(candidate or {})},
                    holding=True,
                    quantity=swing_quantity,
                    intraday_source=intraday_source,
                )
                swing_plans.append(plan)
                intraday_executions.append(execution)
                swing_action = _swing_action_from_execution(
                    execution=execution,
                    row=position,
                    decision_date=decision_date,
                    fallback_action="sell_plan",
                )
                if execution.get("triggered") and execution.get("action") == "sell":
                    latest_price = safe_float(execution.get("trigger_price"), None) or safe_float(position.get("latest_price"), 0.0) or 0.0
                    reason = str(execution.get("reason") or "盘中长线波段计划触发模拟卖出")
                    action = _sell_action(position, latest_price, decision_date, reason)
                    action.update({
                        "trigger_time": execution.get("trigger_time"),
                        "trigger_price": execution.get("trigger_price"),
                        "trigger_type": execution.get("trigger_type"),
                        "is_reliable": bool(execution.get("is_reliable")),
                        "execution_source": "intraday_swing",
                        "fill_model": str(execution.get("fill_model") or "daily_approx"),
                        "fill_model_provenance_complete": True,
                    })
                    actions.append(action)
                    closed = _close_position(position, latest_price, decision_date, reason)
                    closed_positions.append(closed)
                    cash += safe_float(closed.get("net_amount"), latest_price * (safe_int(position.get("quantity"), 0) or 0)) or 0.0
                    continue
                actions.append(swing_action)
            quote = _latest_daily_quote(position.get("symbol"))
            warnings.extend(_quote_warnings(position.get("symbol"), quote))
            latest_price = (
                safe_float(quote.get("price"), None)
                or _candidate_price(candidate or {})
                or safe_float(position.get("latest_price"), None)
                or safe_float(position.get("entry_price"), 0.0)
                or 0.0
            )
            entry_price = safe_float(position.get("entry_price"), latest_price) or latest_price
            if latest_price <= 0:
                reason = "本地日线缺失或最新价无效，保留旧持仓并阻断今日估值更新。"
                actions.append(_block_action(position, reason, decision_date))
                updated_positions.append(dict(position))
                continue
            entry_date = str(position.get("entry_date") or "")[:10]
            if entry_date and entry_date >= decision_date and latest_price > 0 and entry_price > 0 and latest_price <= entry_price * 0.92:
                reason = "A股 T+1 规则：纸面持仓为当日新建，风险复核不假设同日卖出成交。"
                updated = _mark_position(
                    dict(position),
                    latest_price,
                    decision_date,
                    reason,
                    adjustment_factors=adjustment_factors,
                    quote=quote,
                )
                action = _hold_action(updated, latest_price, decision_date, reason)
                action["t_plus_one_sell_blocked"] = True
                actions.append(action)
                updated_positions.append(updated)
                continue
            if latest_price > 0 and entry_price > 0 and latest_price <= entry_price * 0.92:
                # An exit is a real fill: it needs today's tradeable bar. A
                # suspended / missing / limit-down / untrusted-basis day must
                # not be booked off the carried old mark.
                rejection = _fill_tradability_rejection(
                    quote, "sell", require_quote=True, fill_price=latest_price,
                    symbol=normalize_symbol(position.get("symbol")), name=position.get("name"),
                )
                if rejection:
                    actions.append(_block_action(position, rejection, decision_date))
                    updated_positions.append(dict(position))
                    continue
                reason = "触发自动虚拟组合风险复核：最新价较模拟买入价回撤超过 8%，执行纸面退出。"
                actions.append(_sell_action(position, latest_price, decision_date, reason))
                closed = _close_position(position, latest_price, decision_date, reason)
                closed_positions.append(closed)
                cash += safe_float(closed.get("net_amount"), latest_price * (safe_int(position.get("quantity"), 0) or 0)) or 0.0
                continue
            reason = "仍在自动策略沙盒候选中，已持仓不重复买入，继续纸面持仓。" if candidate else "未进入今日自动候选，进入退出观察，不直接删除持仓。"
            updated = _mark_position(
                dict(position),
                latest_price,
                decision_date,
                reason,
                adjustment_factors=adjustment_factors,
                quote=quote,
            )
            updated["latest_date"] = quote.get("date") or updated.get("latest_date") or decision_date
            if candidate:
                action = _hold_action(updated, latest_price, decision_date, reason)
                action["already_held"] = True
                actions.append(action)
            else:
                updated["watch_status"] = "watch_exit"
                actions.append(_watch_exit_action(updated, latest_price, decision_date, reason))
            updated_positions.append(updated)
        positions = updated_positions
        existing_keys = {_position_key(row) for row in positions}

    for candidate in candidates:
        key = _position_key(candidate)
        if key in existing_keys:
            continue
        # Structural check first: a quantity below the board's lot can never
        # fill, whatever the price evidence says (also covers the swing path,
        # whose own check only knew the 100-share main-board lot).
        early_lot = lot_size_for_symbol(candidate.get("symbol"), name=candidate.get("name"))
        if quantity < early_lot:
            reason = f"默认数量 {quantity} 股不足该股票板块整手（{early_lot} 股），未生成纸面持仓。"
            actions.append(_block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        # Evidence requirement shared with the AI book: never book a fill off a
        # mixed/unknown price basis. A known basis (raw/qfq/hfq) proceeds.
        basis_rejection = _basis_fill_rejection(candidate.get("symbol"))
        if basis_rejection:
            actions.append(_block_action(candidate, basis_rejection, decision_date))
            record_legacy_shadow_block(candidate, basis_rejection, "auto_paper_daily_buy")
            continue
        # A new book entry is a real fill too: a zero-volume suspension or a
        # one-word limit-up board cannot be bought at the open.
        buy_quote = _latest_daily_quote(candidate.get("symbol"))
        warnings.extend(_quote_warnings(candidate.get("symbol"), buy_quote))
        buy_rejection = _fill_tradability_rejection(
            buy_quote, "buy", fill_price=_candidate_price(candidate),
            symbol=normalize_symbol(candidate.get("symbol")), name=candidate.get("name"),
        )
        if buy_rejection:
            actions.append(_block_action(candidate, buy_rejection, decision_date))
            record_legacy_shadow_block(candidate, buy_rejection, "auto_paper_daily_buy")
            continue
        if enable_intraday_swing and mode == "update" and _is_long_strategy(candidate):
            plan, execution = _simulate_swing_for_row(
                candidate,
                holding=False,
                quantity=quantity,
                intraday_source=intraday_source,
            )
            swing_plans.append(plan)
            intraday_executions.append(execution)
            if not execution.get("triggered") or execution.get("action") != "buy":
                actions.append(_swing_action_from_execution(
                    execution=execution,
                    row=candidate,
                    decision_date=decision_date,
                    fallback_action="buy_plan",
                ))
                continue
            price = safe_float(execution.get("trigger_price"), None)
            if price is None or price <= 0:
                reason = "盘中计划触发但缺少有效触发价格，未生成模拟买入。"
                actions.append(_block_action(candidate, reason, decision_date))
                record_legacy_shadow_block(candidate, reason, "auto_paper_intraday_swing_buy")
                continue
            if quantity < 100:
                reason = "不足 100 股整数手，未生成模拟买入。"
                actions.append(_block_action(candidate, reason, decision_date))
                record_legacy_shadow_block(candidate, reason, "auto_paper_intraday_swing_buy")
                continue
            cost_info = buy_cash_outflow(price=price, quantity=quantity)
            cash_out = safe_float(cost_info.get("cash_out"), price * quantity) or price * quantity
            if cash_out > cash:
                reason = "现金不足以按盘中触发价模拟买入 100 股。"
                actions.append(_block_action(candidate, reason, decision_date))
                record_legacy_shadow_block(candidate, reason, "auto_paper_intraday_swing_buy")
                continue
            if risk_gate_shadow_enabled:
                try:
                    shadow_state = {
                        **state,
                        "cash": cash,
                        "positions": copy.deepcopy(positions),
                    }
                    shadow_ledger_source = bind_shadow_pretrade_ledger(
                        shadow_state,
                        surface="auto_paper_intraday_swing_buy",
                        symbol=candidate.get("symbol"),
                    )
                    risk_gate_shadow_records.append(
                        evaluate_shadow_risk_order(
                            state=shadow_state,
                            candidate=_risk_candidate(candidate),
                            side="buy",
                            reference_price=price,
                            requested_quantity=quantity,
                            as_of=decision_date,
                            daily_dir=DAILY_DIR,
                            candidate_source_path=candidate_source_for_risk,
                            ledger_source_path=shadow_ledger_source,
                            sector_rotation_path=_sector_rotation_path(),
                            price_source_path=(
                                INTRADAY_DIR
                                / str(intraday_source or "1m")
                                / f"{normalize_symbol(candidate.get('symbol'))}.csv"
                            ),
                            price_data_cutoff=str(
                                execution.get("trade_date") or decision_date
                            )[:10],
                            risk_gate_enforced=risk_gate_enforced,
                            surface="auto_paper_intraday_swing_buy",
                        )
                    )
                except Exception as exc:
                    _record_shadow_failure(
                        risk_gate_shadow_records,
                        surface="auto_paper_intraday_swing_buy",
                        error=exc,
                        as_of=decision_date,
                        write_error=not dry_run,
                    )
            cash -= cash_out
            position = _make_position(candidate, quantity, price, decision_date, cost_info)
            position.update({
                "entry_reason": "长线波段计划在分钟线中触发，按本地纸面交易规则模拟买入 100 股。",
                "trigger_time": execution.get("trigger_time"),
                "trigger_price": execution.get("trigger_price"),
                "trigger_type": execution.get("trigger_type"),
            })
            positions.append(position)
            existing_keys.add(key)
            action = _buy_action(candidate, quantity, price, decision_date, cash)
            action.update({
                "gross_amount": round(safe_float(cost_info.get("gross"), price * quantity) or price * quantity, 2),
                "total_fee": round(safe_float(cost_info.get("total_fee"), 0.0) or 0.0, 2),
                "cash_out": round(cash_out, 2),
                "cost_profile": str(cost_info.get("cost_profile") or "legacy"),
                "entry_reason": "长线波段计划在分钟线中触发，按本地纸面交易规则模拟买入 100 股。",
                "trigger_time": execution.get("trigger_time"),
                "trigger_price": execution.get("trigger_price"),
                "trigger_type": execution.get("trigger_type"),
                "is_reliable": bool(execution.get("is_reliable")),
                "execution_source": "intraday_swing",
                "fill_model": str(execution.get("fill_model") or "daily_approx"),
                "fill_model_provenance_complete": True,
            })
            actions.append(action)
            continue
        price = _candidate_price(candidate)
        if price is None or price <= 0:
            reason = "缺少可用模拟价格，未进入自动虚拟组合。"
            actions.append(_block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        board_lot = lot_size_for_symbol(candidate.get("symbol"), name=candidate.get("name"))
        if quantity < board_lot:
            reason = f"默认数量 {quantity} 股不足该股票板块整手（{board_lot} 股），未生成纸面持仓。"
            actions.append(_block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        cost_info = buy_cash_outflow(price=price, quantity=quantity)
        cash_out = safe_float(cost_info.get("cash_out"), price * quantity) or price * quantity
        if cash_out > cash:
            reason = f"现金不足以模拟买入 {quantity} 股，已阻断。"
            actions.append(_block_action(candidate, reason, decision_date))
            record_legacy_shadow_block(candidate, reason, "auto_paper_daily_buy")
            continue
        if risk_gate_shadow_enabled:
            try:
                shadow_state = {
                    **state,
                    "cash": cash,
                    "positions": copy.deepcopy(positions),
                }
                shadow_ledger_source = bind_shadow_pretrade_ledger(
                    shadow_state,
                    surface="auto_paper_daily_buy",
                    symbol=candidate.get("symbol"),
                )
                risk_gate_shadow_records.append(
                    evaluate_shadow_risk_order(
                        state=shadow_state,
                        candidate=_risk_candidate(candidate),
                        side="buy",
                        reference_price=price,
                        requested_quantity=quantity,
                        as_of=decision_date,
                        daily_dir=DAILY_DIR,
                        candidate_source_path=candidate_source_for_risk,
                        ledger_source_path=shadow_ledger_source,
                        sector_rotation_path=_sector_rotation_path(),
                        risk_gate_enforced=risk_gate_enforced,
                        surface="auto_paper_daily_buy",
                    )
                )
            except Exception as exc:
                _record_shadow_failure(
                    risk_gate_shadow_records,
                    surface="auto_paper_daily_buy",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        cash -= cash_out
        position = _make_position(candidate, quantity, price, decision_date, cost_info)
        positions.append(position)
        existing_keys.add(key)
        action = _buy_action(candidate, quantity, price, decision_date, cash)
        action.update({
            "gross_amount": round(safe_float(cost_info.get("gross"), price * quantity) or price * quantity, 2),
            "total_fee": round(safe_float(cost_info.get("total_fee"), 0.0) or 0.0, 2),
            "cash_out": round(cash_out, 2),
            "cost_profile": str(cost_info.get("cost_profile") or "legacy"),
        })
        actions.append(action)

    freshness_gate = (
        evaluate_paper_review_freshness(state, target_date=review_target_date, daily_dir=DAILY_DIR)
        if review_target_date else evaluate_data_freshness()
    )
    decision_review = build_decision_review(previous_actions, decision_date, freshness_gate)
    positions = _enrich_positions_with_theme_map(positions)
    yesterday_review = build_yesterday_holding_review(previous_positions, positions, actions, decision_review, freshness_gate)
    warnings = list(dict.fromkeys(str(item) for item in warnings))
    summary_for_state = _summary({**state, "cash": round(cash, 2), "positions": positions, "closed_positions": closed_positions}, actions)
    if mode == "update":
        summary_for_state = _with_daily_pnl(
            summary_for_state, valuation_date=decision_date,
            previous_state=previous_valuation_state,
            valuation_valid=bool(freshness_gate.get("can_review_yesterday_positions", True)),
        )
    trade_actions = {"buy", "add", "breakout_add", "reduce_half", "take_profit", "sell", "sell_all", "stop_loss"}
    state.update({
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "updated_at": now_text(),
        "valuation_date": decision_date,
        "daily_pnl_reference": _daily_pnl_reference(summary_for_state, decision_date),
        "initial_cash": safe_float(state.get("initial_cash"), initial_cash) or initial_cash,
        "cash": round(cash, 2),
        "total_return_pct": summary_for_state["total_return_pct"],
        "last_trade_date": (max((str(row.get("decision_date") or "")[:10] for row in actions if str(row.get("action") or "") in trade_actions), default="") or state.get("last_trade_date", "")),
        "positions": positions,
        "closed_positions": closed_positions,
        "actions": actions,
        "daily_actions": actions,
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "delist_liquidation": delist_liquidation,
        "data_freshness_gate": freshness_gate,
        "portfolio_review_valid": bool(freshness_gate.get("can_review_yesterday_positions", True)),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
    })
    latest = {
        "ok": True,
        "exists": True,
        "mode": mode,
        "strategy": strategy,
        "generated_at": now_text(),
        "quantity_per_stock": quantity,
        "source": "formal_candidate_daily_batch",
        "valuation_date": decision_date,
        "input_source": auto_strategy_input_source,
        "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
        "summary": summary_for_state,
        "positions": positions,
        "closed_positions": closed_positions,
        "realized_reductions": state.get("realized_reductions") or [],
        "legacy_realized_adjustment": state.get("legacy_realized_adjustment", 0.0),
        "actions": actions,
        "daily_actions": actions,
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "delist_liquidation": delist_liquidation,
        "data_freshness_gate": freshness_gate,
        "portfolio_review_valid": bool(freshness_gate.get("can_review_yesterday_positions", True)),
        "decision_audit": build_decision_audit(actions),
        "auto_strategy_generated_at": auto_payload.get("generated_at"),
        "auto_candidate_count": len(candidates),
        "intraday_swing_enabled": bool(enable_intraday_swing),
        "intraday_source": intraday_source,
        "allow_daily_approx": bool(allow_daily_approx),
        "swing_plan_count": len(swing_plans),
        "intraday_execution_count": len(intraday_executions),
        "warnings": warnings,
        "swing_plans": swing_plans,
        "intraday_executions": intraday_executions,
        "dry_run": bool(dry_run),
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
    }
    risk_gate_shadow_summary: dict[str, Any] | None = None
    if risk_gate_shadow_enabled:
        try:
            risk_gate_shadow_summary = summarize_shadow_records(
                risk_gate_shadow_records,
                as_of=decision_date,
                risk_gate_enforced=risk_gate_enforced,
            )
        except Exception as exc:
            _record_shadow_failure(
                risk_gate_shadow_records,
                surface="auto_paper_shadow_summary",
                error=exc,
                as_of=decision_date,
                write_error=not dry_run,
            )
    if not dry_run:
        if risk_gate_shadow_summary is not None:
            try:
                write_shadow_summary(
                    risk_gate_shadow_summary,
                    output_path=_risk_gate_shadow_path(),
                )
            except Exception as exc:
                _record_shadow_failure(
                    risk_gate_shadow_records,
                    surface="auto_paper_shadow_output",
                    error=exc,
                    as_of=decision_date,
                    write_error=not dry_run,
                )
        if enable_intraday_swing:
            swing_artifacts = _write_intraday_swing_artifacts(
                swing_plans,
                intraday_executions,
                enabled=enable_intraday_swing,
                source=intraday_source,
                allow_daily_approx=allow_daily_approx,
            )
            latest["swing_trade_plans_path"] = str(SWING_TRADE_PLANS_LATEST_JSON.relative_to(PROJECT_ROOT))
            latest["intraday_execution_path"] = str(INTRADAY_EXECUTION_LATEST_JSON.relative_to(PROJECT_ROOT))
            latest["swing_execution_report_path"] = str(swing_artifacts["report_path"].relative_to(PROJECT_ROOT))
        _write_auto_paper_state(state)
        latest = _write_auto_paper_latest(latest)
        if mode == "init" and force_reset and AUTO_PAPER_PORTFOLIO_HISTORY_JSON.exists():
            AUTO_PAPER_PORTFOLIO_HISTORY_JSON.unlink()
        history = _append_history(latest)
        report_path = _write_report(
            latest,
            risk_gate_shadow_summary=risk_gate_shadow_summary,
        )
        latest["history_path"] = _display_path(AUTO_PAPER_PORTFOLIO_HISTORY_JSON)
        latest["report_path"] = _display_path(report_path)
        latest["history_count"] = len(history.get("items") or [])
        latest = _write_auto_paper_latest(latest)
    return latest


def run_auto_paper_portfolio(
    *,
    mode: str = "init",
    initial_cash: float = 10_000.0,
    scenario_id: str | None = None,
    quantity_per_stock: int = 100,
    strategy: str = "all",
    enable_intraday_swing: bool = False,
    intraday_source: str = "1m",
    allow_daily_approx: bool = False,
    dry_run: bool = False,
    force_reset: bool = False,
    auto_strategy_path: Path | None = None,
    build_conditional_plan: bool = False,
    simulate_conditional_plan: bool = False,
    plan_date: str = "",
    trade_date: str = "",
    lookback_days: int = 250,
    allow_same_day_rerun: bool = False,
    auto_strategy_payload: dict[str, Any] | None = None,
    risk_gate_shadow_enabled: bool = True,
    risk_gate_enforced: bool = False,
    due_plan_payload: dict[str, Any] | None = None,
    formal_candidate_batch: dict[str, Any] | None = None,
    formal_candidate_batch_path: Path | str | None = None,
    formal_candidate_status: str = "",
    publish_next_plan: bool = True,
    review_target_date: str = "",
    candidate_mode: str = "formal",
    strategy_fill_mode: str | None = None,
    snapshot_root: Path | None = None,
    fill_journal_path: Path | None = None,
) -> dict[str, Any]:
    """Run the automatic paper portfolio with a local advisory state-file lock."""

    fill_mode, _ = open_fill.resolve_strategy_fill_mode(strategy_fill_mode)
    if candidate_mode not in {"formal", "legacy_diagnostic"}:
        raise ValueError("candidate_mode must be formal or legacy_diagnostic")
    dry_run = bool(dry_run or candidate_mode == "legacy_diagnostic")
    existing_profile_state: dict[str, Any] = {}
    if AUTO_PAPER_PORTFOLIO_STATE_JSON.exists() and not force_reset:
        if fill_mode == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT:
            try:
                existing_profile_state = json.loads(AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return _snapshot_failure("snapshot_ledger_unreadable")
        else:
            existing_profile_state = _auto_paper_state_store().read(default={})
    if isinstance(existing_profile_state, dict) and existing_profile_state.get("initial_cash"):
        capital_profile = resolve_paper_capital_profile(
            existing_state=existing_profile_state,
            existing_state_verified=True,
        )
    else:
        capital_profile = resolve_paper_capital_profile(
            scenario_id=scenario_id,
            requested_initial_capital=initial_cash,
        )

    lock_path = _auto_paper_lock_path()
    lock_info: dict[str, Any] | None = None
    if not dry_run:
        try:
            lock_info = acquire_update_lock(lock_path, command="auto_paper_portfolio")
        except Exception as exc:  # noqa: BLE001 - return a user-readable lock failure
            return _auto_paper_lock_failed_payload(exc)
    try:
        return _run_auto_paper_portfolio_unlocked(
            mode=mode,
            initial_cash=capital_profile.initial_capital,
            quantity_per_stock=quantity_per_stock,
            strategy=strategy,
            enable_intraday_swing=enable_intraday_swing,
            intraday_source=intraday_source,
            allow_daily_approx=allow_daily_approx,
            dry_run=dry_run,
            force_reset=force_reset,
            auto_strategy_path=auto_strategy_path,
            build_conditional_plan=build_conditional_plan,
            simulate_conditional_plan=simulate_conditional_plan,
            plan_date=plan_date,
            trade_date=trade_date,
            lookback_days=lookback_days,
            allow_same_day_rerun=allow_same_day_rerun,
            auto_strategy_payload=auto_strategy_payload,
            risk_gate_shadow_enabled=risk_gate_shadow_enabled,
            risk_gate_enforced=risk_gate_enforced,
            due_plan_payload=due_plan_payload,
            formal_candidate_batch=formal_candidate_batch,
            formal_candidate_batch_path=formal_candidate_batch_path,
            formal_candidate_status=formal_candidate_status,
            publish_next_plan=publish_next_plan,
            review_target_date=review_target_date,
            candidate_mode=candidate_mode,
            strategy_fill_mode=fill_mode,
            snapshot_root=snapshot_root,
            fill_journal_path=fill_journal_path,
        )
    finally:
        if lock_info is not None:
            release_update_lock(lock_path)


SHARED_PAPER_PLAN_LATEST_JSON = RESULTS_DIR / "shared_paper_plan_latest.json"


def shared_paper_plan_latest() -> dict[str, Any]:
    """Read-only view of the latest shared paper-capital plan.

    Stage-1 boundary: this only *reads* the plan produced by the shared
    allocation planner so the auto paper portfolio can display it. It never
    executes the plan, never mutates paper state, and never places orders.
    """

    data = read_json_safe(SHARED_PAPER_PLAN_LATEST_JSON, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "plan_executed": False,
            "message": "尚未生成共享纸面资金计划。请先运行 shared-paper-plan。",
            "disclaimer": DISCLAIMER,
            "broker_boundary": BROKER_BOUNDARY,
        }
    data["exists"] = True
    # Defensive: this accessor must never imply execution in stage 1.
    data["plan_executed"] = False
    data.setdefault("disclaimer", DISCLAIMER)
    data.setdefault("broker_boundary", BROKER_BOUNDARY)
    return data


def auto_paper_portfolio_latest() -> dict[str, Any]:
    data = read_json_safe(AUTO_PAPER_PORTFOLIO_LATEST_JSON, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "message": "尚未初始化自动虚拟组合。请先确认目标日期的正式候选批次，再初始化组合。",
            **load_plan_review_snapshot(_archive_project_root()),
            "disclaimer": DISCLAIMER,
            "broker_boundary": BROKER_BOUNDARY,
        }
    data["exists"] = True
    data.setdefault("disclaimer", DISCLAIMER)
    data.setdefault("broker_boundary", BROKER_BOUNDARY)
    data.update(load_plan_review_snapshot(_archive_project_root()))
    return data


def auto_paper_portfolio_equity_history(limit: int = 250) -> dict[str, Any]:
    """Return the local paper-equity history without mutating runtime state."""

    data = read_json_safe(AUTO_PAPER_PORTFOLIO_HISTORY_JSON, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "status": "missing",
            "items": [],
            "latest": {},
            "source_path": str(AUTO_PAPER_PORTFOLIO_HISTORY_JSON),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "message_for_ui": "尚未生成纸面净值历史。",
        }

    by_date: dict[str, dict[str, Any]] = {}
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        date_text = str(raw.get("date") or raw.get("generated_at") or "").strip()[:10]
        try:
            normalized_date = datetime.fromisoformat(date_text).date().isoformat()
        except ValueError:
            continue
        equity = safe_float(raw.get("total_equity"), None)
        total_return = safe_float(raw.get("total_return_pct"), None)
        if equity is None or total_return is None:
            continue
        by_date[normalized_date] = {
            "date": normalized_date,
            "total_equity": float(equity),
            "total_return_pct": float(total_return),
            "cash": safe_float(raw.get("cash"), None),
            "market_value": safe_float(raw.get("market_value"), None),
            "position_count": safe_int(raw.get("position_count"), 0),
            "mode": raw.get("mode"),
            "strategy": raw.get("strategy"),
            "source_generated_at": raw.get("generated_at"),
        }
    bounded_limit = max(2, min(2_000, safe_int(limit, 250)))
    items = [by_date[key] for key in sorted(by_date)][-bounded_limit:]
    return {
        "ok": True,
        "exists": True,
        "status": "passed" if len(items) >= 2 else "degraded",
        "items": items,
        "latest": items[-1] if items else {},
        "as_of": str(data.get("updated_at") or (items[-1]["date"] if items else "")),
        "source_path": str(AUTO_PAPER_PORTFOLIO_HISTORY_JSON),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "message_for_ui": (
            "纸面净值历史来自本地后端账本。"
            if len(items) >= 2
            else "纸面净值历史点数不足，暂不生成比较曲线。"
        ),
    }


def auto_paper_portfolio_artifacts() -> list[dict[str, Any]]:
    rows = [
        ("自动虚拟组合状态", "JSON", AUTO_PAPER_PORTFOLIO_STATE_JSON, "持续虚拟组合状态"),
        ("自动虚拟组合快照", "JSON", AUTO_PAPER_PORTFOLIO_LATEST_JSON, "最新组合快照"),
        ("自动虚拟组合历史", "JSON", AUTO_PAPER_PORTFOLIO_HISTORY_JSON, "组合权益历史曲线"),
        ("自动虚拟组合报告", "Markdown", newest_auto_paper_report(), "每日虚拟组合复盘报告"),
        ("条件交易计划", "JSON", _conditional_plan_path(), "T 日收盘后冻结的下一交易日条件交易计划"),
        ("盘中执行结果", "JSON", _conditional_execution_path(), "按本地 1 分钟线顺序触发的条件计划执行结果"),
        ("长线波段计划", "JSON", SWING_TRADE_PLANS_LATEST_JSON, "长线持仓和候选的次日波段计划"),
        ("盘中触发回放", "JSON", INTRADAY_EXECUTION_LATEST_JSON, "分钟线触发结果，不用日线假装成交"),
        ("波段执行报告", "Markdown", newest_swing_execution_report(), "长线波段计划与盘中触发复盘报告"),
    ]
    result = []
    for name, kind, path, purpose in rows:
        exists = bool(path and path.exists())
        stat = path.stat() if exists else None
        result.append({
            "name": name,
            "type": kind,
            "path": str(path.relative_to(PROJECT_ROOT)) if path else "",
            "exists": exists,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds") if stat else "",
            "file_size": stat.st_size if stat else 0,
            "purpose": purpose,
        })
    return result


def swing_plans_latest() -> dict[str, Any]:
    data = read_json_safe(SWING_TRADE_PLANS_LATEST_JSON, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "message": "尚未生成长线波段计划。请先运行自动虚拟组合 update 流程。",
        }
    data["exists"] = True
    return data


def intraday_execution_latest() -> dict[str, Any]:
    data = read_json_safe(INTRADAY_EXECUTION_LATEST_JSON, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "message": "尚未生成盘中触发回放结果。请先运行自动虚拟组合 update 流程。",
        }
    data["exists"] = True
    return data


def swing_execution_html_page() -> str:
    latest = intraday_execution_latest()
    title = "长线波段计划与盘中触发回放"
    if latest.get("exists"):
        body = html.escape(json.dumps(latest, ensure_ascii=False, indent=2, default=str))
        meta = f"生成时间：{html.escape(str(latest.get('generated_at') or '-'))}"
    else:
        body = html.escape(str(latest.get("message") or "尚未生成长线波段计划。"))
        meta = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:960px;margin:2em auto;padding:0 1em;line-height:1.6}}pre{{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:1em;border-radius:8px}}button{{padding:0.5em 1.2em;cursor:pointer}}.disclaimer{{color:#666;font-size:0.9em;margin:1em 0}}</style>
</head>
<body>
  <main>
    <button type="button" onclick="window.print()">打印 / 浏览器另存为 PDF</button>
    <h1>{html.escape(title)}</h1>
    <p>{meta}</p>
    <p class="disclaimer">本报告仅用于本地策略学习与模拟验证，不构成投资建议。盘中回放按分钟线顺序执行，避免未来函数；缺少分钟线时不会用日线假装成交。</p>
    <pre>{body}</pre>
  </main>
</body>
</html>"""


def auto_paper_portfolio_html_page() -> str:
    title = "自动虚拟组合报告"
    latest = auto_paper_portfolio_latest()
    if latest.get("exists"):
        body = html.escape(json.dumps(latest, ensure_ascii=False, indent=2, default=str))
        meta = f"生成时间：{html.escape(str(latest.get('generated_at') or '-'))}"
    else:
        body = html.escape(str(latest.get("message") or "尚未初始化自动虚拟组合。"))
        meta = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:960px;margin:2em auto;padding:0 1em;line-height:1.6}}pre{{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:1em;border-radius:8px}}button{{padding:0.5em 1.2em;cursor:pointer}}.disclaimer{{color:#666;font-size:0.9em;margin:1em 0}}</style>
</head>
<body>
  <main>
    <button type="button" onclick="window.print()">打印 / 另存为 PDF</button>
    <h1>{html.escape(title)}</h1>
    <p>{meta}</p>
    <p class="disclaimer">{html.escape(DISCLAIMER)} {html.escape(BROKER_BOUNDARY)}</p>
    <pre>{body}</pre>
  </main>
</body>
</html>"""
