"""Persistent local paper portfolio for the auto strategy sandbox.

The portfolio is a local simulation ledger only. It does not connect to
brokers, does not place real orders, and does not constitute investment
advice.
"""

from __future__ import annotations

import copy
import hashlib
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
    normalize_position_for_t1,
    simulate_conditional_plan_file,
)
from quantdesk.paper.state_store import PaperStateStore
# Keep legacy names for existing callers and execution-side injection points.
from quantdesk.paper.accounting.reconciliation import (
    _normalized_management_state,
    _parseable_paper_number,
    _paper_state_shape_errors,
    _paper_correction_fields,
    _PAPER_POSITION_NUMERIC_FIELDS,
    _PAPER_CLOSED_NUMERIC_FIELDS,
    _PAPER_TOP_LEVEL_NUMERIC_FIELDS,
    reconcile_paper_account,
)
from quantdesk.paper.queries import portfolio as paper_queries
from quantdesk.paper.queries.portfolio import DISCLAIMER, BROKER_BOUNDARY
from quantdesk.paper.reporting import html as paper_html
from quantdesk.paper import publication as paper_publication
from quantdesk.paper import valuation as paper_valuation
from quantdesk.paper.execution import conditional as execution_conditional
from quantdesk.paper.execution import snapshot as execution_snapshot
from quantdesk.paper.execution import daily as execution_daily
from quantdesk.paper.execution import ports as execution_ports
from quantdesk.paper.realized_corrections import inspect_realized_adjustment
from quantdesk.paper.ledger_read_contract import (
    IDENTITY_FIELDS,
    inspect_strategy_ledger,
    strategy_initialization_evidence,
    strategy_pnl_identity,
    strategy_state_errors,
)
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
    return PaperStateStore(AUTO_PAPER_PORTFOLIO_STATE_JSON, schema_version=API_SCHEMA_VERSION,
                           max_backups=7, validator=strategy_state_errors, identity_fields=IDENTITY_FIELDS)


def _write_auto_paper_state(state: dict[str, Any]) -> dict[str, Any]:
    ensure_declared_fill_policy(state, policy_id=_declared_strategy_policy(state))
    normalized = reconcile_paper_account(state)
    if normalized["accounting_reconciliation"]["status"] == "invalid":
        raise ValueError("paper_account_invalid")
    return _auto_paper_state_store().write(normalized)


def _declared_strategy_policy(state: dict[str, Any]) -> str:
    return (POLICY_NEXT_OPEN if state.get("strategy_fill_mode") == open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT
            else POLICY_DAILY_APPROX)


def _paper_publication_paths() -> paper_publication.PaperPublicationPaths:
    """Resolve the legacy engine's output overrides at the time of publication."""
    return paper_publication.PaperPublicationPaths(
        project_root=PROJECT_ROOT, results_dir=RESULTS_DIR, reports_dir=REPORTS_DIR,
        latest_json=AUTO_PAPER_PORTFOLIO_LATEST_JSON,
        history_json=AUTO_PAPER_PORTFOLIO_HISTORY_JSON,
        swing_plans_json=SWING_TRADE_PLANS_LATEST_JSON,
        intraday_execution_json=INTRADAY_EXECUTION_LATEST_JSON,
    )


def _write_auto_paper_latest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.write_auto_paper_latest(
        snapshot, paths=_paper_publication_paths(),
        unrecorded=_SHADOW_UNRECORDED,
        reconcile=reconcile_paper_account,
        identity=strategy_pnl_identity,
        write_json=atomic_write_json,
    )


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
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    df = read_csv_frame(path, dtype={"symbol": str})
    df.attrs["source_sha256"] = source_hash
    df.attrs["source_file"] = str(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != source_hash:
        df.attrs["price_basis_reason"] = "unknown_price_basis"
        df.attrs["warnings"] = [*(df.attrs.get("warnings") or []), "daily_source_changed_during_read"]
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
    if decision_date and "date" in df.columns:
        # Basis was classified on the full input by _read_daily_bars. A later
        # legitimate raw session is not price evidence for an earlier review.
        df = df.loc[df["date"] <= pd.Timestamp(str(decision_date)[:10])].copy()
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
        "source_file": str(df.attrs.get("source_file") or ""),
        "source_sha256": str(df.attrs.get("source_sha256") or ""),
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
    quote["bar_valid"] = ok
    quote["bar_validation_reason"] = reason
    if not ok and "volume_not_positive" in reason:
        last_tradable = _last_tradable_close(df)
        if last_tradable is not None:
            quote["price"] = last_tradable
            quote["last_tradable_price"] = last_tradable
            for _, prior in df.iloc[::-1].iterrows():
                if daily_bar_validity_from_row(prior.to_dict())[0]:
                    quote["last_tradable_date"] = pd.Timestamp(prior["date"]).strftime("%Y-%m-%d")
                    break
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

    frame = _read_daily_bars(str(symbol or ""))
    gate = validate_price_frame_set({str(symbol): frame}, consumer="paper_execution")
    basis, reason = _daily_price_basis(symbol)
    if gate["eligible"]:
        return ""
    reason = ",".join(gate["reason_codes"]) or reason
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
    if require_quote and quote.get("stale"):
        return f"成交价格日期 {quote.get('date') or '未知'} 已滞后，缺少目标日证据，拒绝模拟{label}。"
    volume = safe_float(quote.get("volume"), None)
    if quote.get("suspended") or quote.get("is_suspended") or (volume is not None and volume <= 0):
        return f"当日零成交量（停牌或无成交），无法确认成交，拒绝模拟{label}。"
    basis_reason = str(quote.get("price_basis_reason") or "")
    if (basis_reason in {"mixed_price_basis", "unknown_price_basis"} or quote.get("price_basis_untrusted")
            or (quote.get("price_basis") and quote.get("price_basis") != "raw")):
        basis_reason = basis_reason or "consumer_price_basis_not_allowed"
        return f"价格口径不可用于成交（{basis_reason or 'unknown_price_basis'}），拒绝模拟{label}。"
    if require_quote and quote.get("bar_valid") is False:
        return f"当日价格字段未通过核对（{quote.get('bar_validation_reason') or 'invalid_daily_bar'}），拒绝模拟{label}。"
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


def _strategy_book_existed() -> bool:
    return strategy_initialization_evidence(AUTO_PAPER_PORTFOLIO_STATE_JSON,
        latest_path=AUTO_PAPER_PORTFOLIO_LATEST_JSON, history_path=AUTO_PAPER_PORTFOLIO_HISTORY_JSON)


def _load_state(initial_cash: float) -> dict[str, Any]:
    """Read the funded account without initialization, quarantine or auto-restore."""

    expected = read_json_safe(AUTO_PAPER_PORTFOLIO_LATEST_JSON, {})
    expected = expected if isinstance(expected, dict) else {}
    identity = {key: expected[key] for key in IDENTITY_FIELDS if expected.get(key) is not None}
    result = _auto_paper_state_store().read_snapshot(expected_identity=identity or None)
    if result.status != "ready" or result.data is None:
        reason = "paper_ledger_missing_after_initialization" if _strategy_book_existed() else "paper_ledger_not_initialized"
        structural_errors: list[str] = []
        try:
            original = json.loads(AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8-sig"))
            structural_errors = _paper_state_shape_errors(original)
        except (OSError, ValueError, UnicodeError):
            pass
        return reconcile_paper_account({
            "cash": 0.0, "positions": [], "closed_positions": [], "actions": [],
            "ledger_read_status": result.status, "state_recovered_from_backup": False,
            "ledger_read_reason_codes": [*result.reason_codes, *structural_errors],
            "state_recovery_source": result.recovery_source,
            "state_recovery_sha256": result.recovery_sha256,
            "state_recovery_warning": "账本不可用；有备份也须显式核验恢复，读取未修改原件或补本金。",
            "warnings": [reason, *result.reason_codes],
        })
    data = result.data
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
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.holding_days(
        position, decision_date,
        datetime_type=datetime,
    )


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
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.mark_position(
        position, latest_price, decision_date, hold_reason, adjustment_factors=adjustment_factors, quote=quote,
        latest_quote=_latest_daily_quote,
        holding_days_for=_holding_days,
        suspension_policy=apply_suspension_valuation_policy,
        adjustment_policy=apply_paper_adjustment_policy,
    )


def _hold_action(position: dict[str, Any], latest_price: float, decision_date: str, hold_reason: str) -> dict[str, Any]:
    quantity = safe_int(position.get("quantity"), 0) or 0
    unit_cost = safe_float(position.get("avg_cost") or position.get("entry_price"), latest_price) or latest_price
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
        "pnl": round((latest_price - unit_cost) * quantity, 2),
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
    unit_cost = safe_float(position.get("avg_cost") or position.get("entry_price"), latest_price) or latest_price
    gross_amount = float(latest_price) * quantity
    costs = compute_trade_costs(side="sell", gross=gross_amount)
    cash = sell_cash_inflow(price=latest_price, quantity=quantity, avg_cost=unit_cost)
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
    unit_cost = safe_float(position.get("avg_cost") or position.get("entry_price"), latest_price) or latest_price
    gross_amount = float(latest_price) * quantity
    costs = compute_trade_costs(side="sell", gross=gross_amount)
    cash = sell_cash_inflow(price=latest_price, quantity=quantity, avg_cost=unit_cost)
    entry_basis_known = position.get("entry_cost_basis_includes_buy_fee")
    return {
        **position,
        "exit_quantity": quantity,
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
        "pnl_cost_basis": "weighted_average_cost" if (safe_float(position.get("avg_cost"), 0.0) or 0.0) > 0 else "entry_price_legacy_fallback",
        "pnl_unit_cost": round(unit_cost, 4),
        "return_pct_cost_basis": "weighted_average_cost" if (safe_float(position.get("avg_cost"), 0.0) or 0.0) > 0 else "entry_price_legacy_fallback",
        "return_pct": round((latest_price / unit_cost - 1.0) * 100.0, 2) if unit_cost > 0 else 0.0,
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
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.paper_account_scale(
        initial_cash,
    )


def _sell_available_position(
    position: dict[str, Any], latest_price: float, decision_date: str, reason: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Settle only documented sellable shares, preserving any frozen remainder."""
    normalized = normalize_position_for_t1(position, decision_date)
    quantity = normalized["quantity"]
    sellable = normalized["sellable_quantity"]
    if sellable <= 0 or sellable > quantity:
        raise ValueError("no_verified_sellable_quantity")
    quantity_error = order_quantity_rule(normalized.get("symbol"), sellable,
                                         held_shares=quantity, name=normalized.get("name"))
    if quantity_error:
        raise ValueError(quantity_error)
    sold_lot = {**normalized, "quantity": sellable, "sellable_quantity": sellable, "frozen_quantity": 0}
    action = _sell_action(sold_lot, latest_price, decision_date, reason)
    record = _close_position(sold_lot, latest_price, decision_date, reason)
    remaining = quantity - sellable
    action["remaining_quantity"] = remaining
    if remaining == 0:
        return action, record, None
    record.update(status="reduced", management_state="reduced")
    action["t_plus_one_partial_exit"] = True
    rest = {**normalized, "quantity": remaining, "sellable_quantity": 0,
            "frozen_quantity": remaining, "management_state": "reduced", "last_action_date": decision_date}
    return action, record, rest


def _summary(state: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.summary(
        state, actions,
        account_scale_for=paper_account_scale,
        inspect_adjustment=inspect_realized_adjustment,
        pnl_identity_for=strategy_pnl_identity,
        correction_fields_for=_paper_correction_fields,
    )


def _with_daily_pnl(
    summary: dict[str, Any], *, valuation_date: str,
    previous_state: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
    valuation_valid: bool = True,
) -> dict[str, Any]:
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.with_daily_pnl(
        summary, valuation_date=valuation_date, previous_state=previous_state, history=history, calendar=calendar, valuation_valid=valuation_valid,
        project_root=_archive_project_root,
        load_calendar=load_a_share_trading_calendar,
        read_history=read_json_safe,
        history_path=AUTO_PAPER_PORTFOLIO_HISTORY_JSON,
    )


def _daily_pnl_reference(summary: dict[str, Any], valuation_date: str) -> dict[str, Any]:
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.daily_pnl_reference(
        summary, valuation_date,
    )


def _append_history(latest: dict[str, Any]) -> dict[str, Any]:
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.append_history(
        latest, paths=_paper_publication_paths(),
        read_json=read_json_safe,
        write_json=atomic_write_json,
        timestamp=now_text,
        disclaimer=DISCLAIMER,
    )


def newest_auto_paper_report() -> Path | None:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.newest_auto_paper_report(REPORTS_DIR)


def _write_report(
    latest: dict[str, Any],
    *,
    risk_gate_shadow_summary: dict[str, Any] | None = None,
) -> Path:
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.write_report(
        latest, risk_gate_shadow_summary=risk_gate_shadow_summary, paths=_paper_publication_paths(),
        clock=datetime.now,
        disclaimer=DISCLAIMER,
        broker_boundary=BROKER_BOUNDARY,
        next_open_fill_mode=open_fill.FILL_MODE_NEXT_OPEN_SNAPSHOT,
        render_shadow=render_shadow_report_section,
        record_shadow_error=record_shadow_error_best_effort,
        shadow_error_path=_risk_gate_shadow_error_path,
        unrecorded=_SHADOW_UNRECORDED,
    )


def newest_swing_execution_report() -> Path | None:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.newest_swing_execution_report(REPORTS_DIR)


def _write_swing_execution_report(plans: list[dict[str, Any]], executions: list[dict[str, Any]]) -> Path:
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.write_swing_execution_report(
        plans, executions, paths=_paper_publication_paths(),
        clock=datetime.now,
    )


def _write_intraday_swing_artifacts(
    plans: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    *,
    enabled: bool,
    source: str,
    allow_daily_approx: bool,
) -> dict[str, Any]:
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.write_intraday_swing_artifacts(
        plans, executions, enabled=enabled, source=source, allow_daily_approx=allow_daily_approx, paths=_paper_publication_paths(),
        timestamp=now_text,
        disclaimer=DISCLAIMER,
        write_json=atomic_write_json,
        write_swing_report=_write_swing_execution_report,
    )


def _execution_rows(execution_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(execution_payload.get("events") or []) + list(execution_payload.get("no_fill") or []) + list(execution_payload.get("blocked") or [])


def _total_equity_from_state(state: dict[str, Any]) -> float:
    """Compatibility adapter; use current valuation dependencies."""
    return paper_valuation.total_equity_from_state(
        state,
    )


def _write_conditional_compat_artifacts(plan_payload: dict[str, Any], execution_payload: dict[str, Any]) -> dict[str, Any]:
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.write_conditional_compat_artifacts(
        plan_payload, execution_payload, paths=_paper_publication_paths(),
        execution_rows=_execution_rows,
        timestamp=now_text,
        write_swing_report=_write_swing_execution_report,
        write_json=atomic_write_json,
        disclaimer=DISCLAIMER,
        display_path=_display_path,
    )


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


def _conditional_execution_ports() -> execution_ports.ConditionalPorts:
    """Bind current paths and callbacks without invoking lazy dependencies."""
    return execution_ports.ConditionalPorts(
        paths=execution_ports.ConditionalPaths(
            history_path=AUTO_PAPER_PORTFOLIO_HISTORY_JSON,
            state_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
            strategy_path=AUTO_STRATEGY_LATEST_JSON,
            daily_dir=DAILY_DIR,
            daily_features_path=DAILY_FEATURE_LATEST_CSV,
            intraday_dir=INTRADAY_DIR,
            intraday_execution_path=INTRADAY_EXECUTION_LATEST_JSON,
            results_dir=RESULTS_DIR,
            swing_plans_path=SWING_TRADE_PLANS_LATEST_JSON,
            archive_project_root=_archive_project_root,
            conditional_execution_path=_conditional_execution_path,
            conditional_plan_path=_conditional_plan_path,
            display_path=_display_path,
            risk_gate_shadow_error_path=_risk_gate_shadow_error_path,
            risk_gate_shadow_path=_risk_gate_shadow_path,
            sector_rotation_path=_sector_rotation_path,
        ),
        info=execution_ports.ConditionalInfo(
            broker_boundary=BROKER_BOUNDARY,
            disclaimer=DISCLAIMER,
        ),
        publication=execution_ports.ConditionalPublication(
            append_history=_append_history,
            write_auto_paper_latest=_write_auto_paper_latest,
            write_conditional_compat_artifacts=_write_conditional_compat_artifacts,
            write_report=_write_report,
            atomic_write_json=atomic_write_json,
            publish_archived_conditional_plan=publish_archived_conditional_plan,
        ),
        review=execution_ports.ConditionalReview(
            attach_bound_replay_review=_attach_bound_replay_review,
            conditional_review_summary=_conditional_review_summary,
            enrich_positions_with_theme_map=_enrich_positions_with_theme_map,
            build_conditional_replay_receipt=build_conditional_replay_receipt,
            build_decision_audit=build_decision_audit,
            build_decision_review=build_decision_review,
            build_yesterday_holding_review=build_yesterday_holding_review,
            evaluate_data_freshness=evaluate_data_freshness,
            evaluate_paper_review_freshness=evaluate_paper_review_freshness,
            intraday_gap_status=intraday_gap_status,
        ),
        state=execution_ports.ConditionalState(
            auto_paper_state_store=_auto_paper_state_store,
            load_state=_load_state,
            write_auto_paper_state=_write_auto_paper_state,
        ),
        valuation=execution_ports.ConditionalValuation(
            load_auto_adjustment_factors=_load_auto_adjustment_factors,
            paper_correction_fields=_paper_correction_fields,
            summary=_summary,
            total_equity_from_state=_total_equity_from_state,
            with_daily_pnl=_with_daily_pnl,
        ),
        risk=execution_ports.ConditionalRisk(
            record_shadow_failure=_record_shadow_failure,
            summarize_shadow_records=summarize_shadow_records,
            write_shadow_summary=write_shadow_summary,
        ),
        plans=execution_ports.ConditionalPlans(
            apply_conditional_execution_to_state=apply_conditional_execution_to_state,
            build_conditional_trade_plan=build_conditional_trade_plan,
            enrich_legacy_due_plan_candidate_envelopes=enrich_legacy_due_plan_candidate_envelopes,
            inspect_plan_minute_inputs=inspect_plan_minute_inputs,
            load_archived_conditional_plan=load_archived_conditional_plan,
            simulate_conditional_plan_file=simulate_conditional_plan_file,
        ),
        clock=execution_ports.ConditionalClock(
            now_text=now_text,
        ),
        trading=execution_ports.ConditionalTrading(
            round_down_to_lot=round_down_to_lot,
        ),
    )


def _snapshot_execution_ports() -> execution_ports.SnapshotPorts:
    """Bind current paths and callbacks without invoking lazy dependencies."""
    return execution_ports.SnapshotPorts(
        paths=execution_ports.SnapshotPaths(
            state_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
            project_root=PROJECT_ROOT,
        ),
        info=execution_ports.SnapshotInfo(
            broker_boundary=BROKER_BOUNDARY,
            disclaimer=DISCLAIMER,
            policy_next_open=POLICY_NEXT_OPEN,
        ),
        state=execution_ports.SnapshotState(
            auto_paper_state_store=_auto_paper_state_store,
            empty_state=_empty_state,
            paper_state_shape_errors=_paper_state_shape_errors,
        ),
        trading=execution_ports.SnapshotTrading(
            block_action=_block_action,
            buy_action=_buy_action,
            candidate_price=_candidate_price,
            close_position=_close_position,
            hold_action=_hold_action,
            make_position=_make_position,
            paper_action_boundary=_paper_action_boundary,
            position_key=_position_key,
            sell_action=_sell_action,
            snapshot_failure=_snapshot_failure,
            buy_cash_outflow=buy_cash_outflow,
            compute_trade_costs=compute_trade_costs,
            normalize_position_for_t1=normalize_position_for_t1,
            normalize_symbol=normalize_symbol,
            order_quantity_rule=order_quantity_rule,
            policy_header=policy_header,
            safe_float=safe_float,
            safe_int=safe_int,
            sell_cash_inflow=sell_cash_inflow,
        ),
        valuation=execution_ports.SnapshotValuation(
            daily_pnl_reference=_daily_pnl_reference,
            load_auto_adjustment_factors=_load_auto_adjustment_factors,
            mark_position=_mark_position,
            paper_correction_fields=_paper_correction_fields,
            snapshot_daily_quotes=_snapshot_daily_quotes,
            summary=_summary,
            with_daily_pnl=_with_daily_pnl,
            reconcile_paper_account=reconcile_paper_account,
        ),
        publication=execution_ports.SnapshotPublication(
            publish_snapshot_result=_publish_snapshot_result,
        ),
        review=execution_ports.SnapshotReview(
            build_decision_audit=build_decision_audit,
        ),
        clock=execution_ports.SnapshotClock(
            date=date,
            datetime=datetime,
            load_a_share_trading_calendar=load_a_share_trading_calendar,
            time=time,
            utc_now_iso=utc_now_iso,
        ),
        journal=fill_journal,
        open_fill=open_fill,
    )


def _daily_execution_ports() -> execution_ports.DailyPorts:
    """Bind current paths and callbacks without invoking lazy dependencies."""
    return execution_ports.DailyPorts(
        info=execution_ports.DailyInfo(
            api_schema_version=API_SCHEMA_VERSION,
            app_version=APP_VERSION,
            broker_boundary=BROKER_BOUNDARY,
            disclaimer=DISCLAIMER,
        ),
        paths=execution_ports.DailyPaths(
            history_path=AUTO_PAPER_PORTFOLIO_HISTORY_JSON,
            state_path=AUTO_PAPER_PORTFOLIO_STATE_JSON,
            strategy_path=AUTO_STRATEGY_LATEST_JSON,
            daily_dir=DAILY_DIR,
            daily_features_path=DAILY_FEATURE_LATEST_CSV,
            intraday_dir=INTRADAY_DIR,
            intraday_execution_path=INTRADAY_EXECUTION_LATEST_JSON,
            project_root=PROJECT_ROOT,
            results_dir=RESULTS_DIR,
            swing_plans_path=SWING_TRADE_PLANS_LATEST_JSON,
            archive_project_root=_archive_project_root,
            conditional_execution_path=_conditional_execution_path,
            conditional_plan_path=_conditional_plan_path,
            display_path=_display_path,
            risk_gate_shadow_path=_risk_gate_shadow_path,
            sector_rotation_path=_sector_rotation_path,
        ),
        risk=execution_ports.DailyRisk(
            unrecorded=_SHADOW_UNRECORDED,
            record_shadow_failure=_record_shadow_failure,
            write_shadow_ledger_snapshot_once=_write_shadow_ledger_snapshot_once,
            build_legacy_blocked_shadow_record=build_legacy_blocked_shadow_record,
            evaluate_shadow_risk_order=evaluate_shadow_risk_order,
            summarize_shadow_records=summarize_shadow_records,
            write_shadow_summary=write_shadow_summary,
        ),
        publication=execution_ports.DailyPublication(
            append_history=_append_history,
            write_auto_paper_latest=_write_auto_paper_latest,
            write_intraday_swing_artifacts=_write_intraday_swing_artifacts,
            write_report=_write_report,
        ),
        trading=execution_ports.DailyTrading(
            basis_fill_rejection=_basis_fill_rejection,
            block_action=_block_action,
            buy_action=_buy_action,
            candidate_price=_candidate_price,
            fill_tradability_rejection=_fill_tradability_rejection,
            hold_action=_hold_action,
            is_long_strategy=_is_long_strategy,
            make_position=_make_position,
            position_key=_position_key,
            sell_available_position=_sell_available_position,
            simulate_swing_for_row=_simulate_swing_for_row,
            snapshot_failure=_snapshot_failure,
            swing_action_from_execution=_swing_action_from_execution,
            watch_exit_action=_watch_exit_action,
            apply_delisting_liquidation=apply_delisting_liquidation,
            buy_cash_outflow=buy_cash_outflow,
            lot_size_for_symbol=lot_size_for_symbol,
            normalize_position_for_t1=normalize_position_for_t1,
            normalize_symbol=normalize_symbol,
            order_quantity_rule=order_quantity_rule,
            round_down_to_lot=round_down_to_lot,
            safe_float=safe_float,
        ),
        valuation=execution_ports.DailyValuation(
            daily_pnl_reference=_daily_pnl_reference,
            latest_daily_quote=_latest_daily_quote,
            load_auto_adjustment_factors=_load_auto_adjustment_factors,
            mark_position=_mark_position,
            paper_correction_fields=_paper_correction_fields,
            quote_warnings=_quote_warnings,
            summary=_summary,
            with_daily_pnl=_with_daily_pnl,
        ),
        state=execution_ports.DailyState(
            empty_state=_empty_state,
            invalid_paper_account_payload=_invalid_paper_account_payload,
            load_state=_load_state,
            strategy_book_existed=_strategy_book_existed,
            write_auto_paper_state=_write_auto_paper_state,
        ),
        review=execution_ports.DailyReview(
            enrich_positions_with_theme_map=_enrich_positions_with_theme_map,
            build_decision_audit=build_decision_audit,
            build_decision_review=build_decision_review,
            build_yesterday_holding_review=build_yesterday_holding_review,
            evaluate_data_freshness=evaluate_data_freshness,
            evaluate_paper_review_freshness=evaluate_paper_review_freshness,
        ),
        dispatch=execution_ports.DailyDispatch(
            conditional=_run_conditional_auto_paper_update,
            snapshot=_run_next_open_snapshot_update,
        ),
        plans=execution_ports.DailyPlans(
            build_conditional_trade_plan=build_conditional_trade_plan,
            conditional_trade_plan_latest=conditional_trade_plan_latest,
            simulate_conditional_plan_file=simulate_conditional_plan_file,
        ),
        candidates=execution_ports.DailyCandidates(
            candidate_rows_from_strategy_payload=candidate_rows_from_strategy_payload,
            formal_candidate_strategy_payload=formal_candidate_strategy_payload,
            infer_target_date=infer_target_date,
            load_formal_candidate_intake=load_formal_candidate_intake,
        ),
        clock=execution_ports.DailyClock(
            now_text=now_text,
        ),
        open_fill=open_fill,
    )


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
    """Compatibility entrypoint; resolve dependencies afresh on each call."""
    return execution_conditional.run(
        ports=_conditional_execution_ports(),
        initial_cash=initial_cash,
        quantity_per_stock=quantity_per_stock,
        strategy=strategy,
        intraday_source=intraday_source,
        allow_daily_approx=allow_daily_approx,
        dry_run=dry_run,
        auto_strategy_path=auto_strategy_path,
        auto_strategy_payload=auto_strategy_payload,
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
    """Compatibility adapter; publish with current paths and dependencies."""
    return paper_publication.publish_snapshot_result(
        result, paths=_paper_publication_paths(),
        write_latest=_write_auto_paper_latest,
        append_history=_append_history,
        display_path=_display_path,
        write_report=_write_report,
    )


def _run_next_open_snapshot_update(
    *, mode: str, initial_cash: float, quantity: int, strategy: str,
    decision_date: str, candidates: list[dict[str, Any]], intake: dict[str, Any],
    dry_run: bool, force_reset: bool, snapshot_root: Path | None, journal_path: Path | None,
) -> dict[str, Any]:
    """Compatibility entrypoint; resolve dependencies afresh on each call."""
    return execution_snapshot.run(
        ports=_snapshot_execution_ports(),
        mode=mode,
        initial_cash=initial_cash,
        quantity=quantity,
        strategy=strategy,
        decision_date=decision_date,
        candidates=candidates,
        intake=intake,
        dry_run=dry_run,
        force_reset=force_reset,
        snapshot_root=snapshot_root,
        journal_path=journal_path,
    )


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
    """Compatibility entrypoint; resolve dependencies afresh on each call."""
    return execution_daily.run(
        ports=_daily_execution_ports(),
        mode=mode,
        initial_cash=initial_cash,
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
        strategy_fill_mode=strategy_fill_mode,
        snapshot_root=snapshot_root,
        fill_journal_path=fill_journal_path,
    )


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
            existing_profile_state = _auto_paper_state_store().read_snapshot().data or {}
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


def _paper_query_paths() -> paper_queries.PaperQueryPaths:
    """Resolve legacy overrides at call time; queries never import this engine."""
    return paper_queries.PaperQueryPaths(
        project_root=PROJECT_ROOT, review_root=_archive_project_root(), reports=REPORTS_DIR,
        state=AUTO_PAPER_PORTFOLIO_STATE_JSON, latest=AUTO_PAPER_PORTFOLIO_LATEST_JSON,
        history=AUTO_PAPER_PORTFOLIO_HISTORY_JSON, shared_plan=SHARED_PAPER_PLAN_LATEST_JSON,
        conditional_plan=_conditional_plan_path(), conditional_execution=_conditional_execution_path(),
        swing_plan=SWING_TRADE_PLANS_LATEST_JSON, intraday_execution=INTRADAY_EXECUTION_LATEST_JSON,
    )


def shared_paper_plan_latest() -> dict[str, Any]:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.shared_paper_plan_latest(
        _paper_query_paths(), disclaimer=DISCLAIMER, broker_boundary=BROKER_BOUNDARY,
        json_reader=read_json_safe,
    )


def auto_paper_portfolio_latest() -> dict[str, Any]:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.auto_paper_portfolio_latest(
        _paper_query_paths(), disclaimer=DISCLAIMER, broker_boundary=BROKER_BOUNDARY,
        json_reader=read_json_safe, review_loader=load_plan_review_snapshot,
        ledger_inspector=inspect_strategy_ledger,
    )


def auto_paper_portfolio_equity_history(limit: int = 250) -> dict[str, Any]:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.auto_paper_portfolio_equity_history(_paper_query_paths(), limit)


def auto_paper_portfolio_artifacts() -> list[dict[str, Any]]:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.auto_paper_portfolio_artifacts(
        _paper_query_paths(), auto_report=newest_auto_paper_report(),
        swing_report=newest_swing_execution_report(),
    )


def swing_plans_latest() -> dict[str, Any]:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.swing_plans_latest(_paper_query_paths())


def intraday_execution_latest() -> dict[str, Any]:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_queries.intraday_execution_latest(_paper_query_paths())


def swing_execution_html_page() -> str:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_html.render_swing_execution_html(intraday_execution_latest())


def auto_paper_portfolio_html_page() -> str:
    """Compatibility entry point; implementation lives in paper queries/reporting."""
    return paper_html.render_auto_paper_portfolio_html(
        auto_paper_portfolio_latest(), disclaimer=DISCLAIMER, broker_boundary=BROKER_BOUNDARY,
    )
