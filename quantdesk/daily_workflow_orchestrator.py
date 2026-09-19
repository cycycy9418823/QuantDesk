"""Unified daily review workflow for QuantDesk.

This module coordinates local learning/simulation tasks only. It does not
connect to brokers, place real orders, save real account information, provide
investment advice, or promise returns.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.auto_strategy_sandbox import run_auto_strategy_cycle
from quantdesk.candidate_contract import (
    attach_theme_map_to_candidates,
    candidate_rows_from_strategy_payload,
    summarize_candidate_theme_coverage,
    summarize_candidates_by_strategy,
)
from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_experiment_tracker_snapshot import (
    load_experiment_tracker_snapshot,
    render_daily_experiment_tracker_snapshot_markdown,
)
from quantdesk.daily_feature_engine import build_daily_features, daily_feature_status
from quantdesk.daily_intraday_live_snapshot import (
    load_intraday_live_latest_snapshot,
    render_daily_intraday_live_snapshot_markdown,
)
from quantdesk.daily_output_summary import build_daily_console_summary
from quantdesk.daily_price_update_engine import daily_price_status, infer_target_date, update_daily_prices
from quantdesk.daily_research_snapshot import load_research_dashboard_snapshot, render_daily_research_snapshot_markdown
from quantdesk.daily_risk_attribution_snapshot import (
    load_risk_attribution_snapshot,
    render_daily_risk_attribution_snapshot_markdown,
)
from quantdesk.data_confidence import build_reference_confidence
from quantdesk.data_freshness_gate import (
    evaluate_data_freshness,
    evaluate_paper_review_freshness,
    paper_review_data_status,
    waiting_decision_review,
    waiting_yesterday_holding_review,
)
from quantdesk.intraday.intraday_gap_checker import intraday_gap_status
from quantdesk.learning_universe import (
    load_low_price_universe,
    low_price_universe_path,
    summarize_low_price_universe,
)
from quantdesk.multi_strategy_account_book import (
    load_multi_strategy_account_book_snapshot,
    render_daily_multi_strategy_snapshot_markdown,
)
from quantdesk.daily_candidate_refresh import (
    load_archived_conditional_plan,
    load_formal_candidate_batch,
    resolve_chain_bound_formal_candidate_batch,
    scan_unreplayed_archived_plans,
)
from quantdesk.data_truth.pit_sector_status import (
    build_pit_sector_status,
    write_pit_sector_status,
)
from quantdesk.paper.auto_paper_portfolio import (
    AUTO_PAPER_PORTFOLIO_STATE_JSON,
    run_auto_paper_portfolio,
)
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.plan_review_receipts import plan_review_snapshot
from quantdesk.remediation_advisor import build_remediation_plan
from quantdesk.research.daily_watchpool_step import build_daily_watchpool_step
from quantdesk.research.daily_research_snapshots import (
    load_daily_research_snapshots,
    render_daily_research_snapshots_markdown,
)
from quantdesk.research.daily_risk_alert_snapshot import (
    load_daily_risk_alert_snapshot,
    render_daily_risk_alert_snapshot_markdown,
)
from quantdesk.research.daily_strategy_recommendation_snapshot import (
    load_strategy_recommendation_snapshot,
    render_daily_strategy_recommendation_snapshot_markdown,
)
from quantdesk.research_workflow_cards import render_research_workflow_cards_markdown
from quantdesk.shared import normalize_symbol, now_text
from quantdesk.theme_current_candidate_validator import validate_theme_map_for_current_candidates
from quantdesk.theme_map_updater import theme_map_status, update_theme_map
from quantdesk.theme_sector_engine import THEME_MAP_PATH, load_theme_map
from quantdesk.utils import atomic_write_json, read_json_safe
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DAILY_WORKFLOW_LATEST_JSON = RESULTS_DIR / "daily_workflow_latest.json"
DAILY_REPORT_LATEST_MD = REPORTS_DIR / "daily_quant_report_latest.md"

DISCLAIMER = (
    "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。"
    "不接券商，不真实下单，不保存真实账户信息，不承诺收益。"
)

STEP_NAMES = [
    "low_price_pool_check",
    "daily_cache_check",
    "low_price_report",
    "daily_features",
    "theme_map_status",
    "auto_strategy_sandbox",
    "composite_watchpool",
    "strategy_lab",
    "auto_paper_portfolio_state",
    "conditional_trade_plan",
    "intraday_gap_check",
    "conditional_execution",
    "portfolio_update",
    "paper_vs_backtest_consistency",
    "market_brain_shadow",
    "final_report",
]


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _paper_initial_cash() -> float:
    """Paper-account seed from config (default 100000).

    Round 21: the previous hardcoded 10000 only ever seeds a NEW ledger; the
    live legacy 10k ledger keeps its recorded initial_cash via capital-profile
    resolution and is labeled account_scale=legacy_10k downstream.
    """

    from quantdesk.config import load_config

    try:
        value = float((load_config().get("paper") or {}).get("initial_cash") or 100000)
    except Exception:
        value = 100000.0
    return value if value > 0 else 100000.0


def _latest_daily_date() -> str:
    dates: list[str] = []
    for path in DAILY_DIR.glob("*.csv"):
        frame = read_csv_frame(
            path,
            usecols=lambda col: str(col).lower() in {"date", "日期", "交易日期"},
        )
        if frame.empty:
            continue
        parsed = pd.to_datetime(frame[frame.columns[0]], errors="coerce").dropna()
        if not parsed.empty:
            dates.append(parsed.max().strftime("%Y-%m-%d"))
    return max(dates) if dates else ""


def _start_step(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "running",
        "message": "",
        "started_at": now_text(),
        "finished_at": "",
        "outputs": {},
        "error": "",
    }


def _finish_step(
    step: dict[str, Any],
    status: str,
    message: str = "",
    *,
    outputs: dict[str, Any] | None = None,
    error: str = "",
    input_source: str = "in_memory",
) -> dict[str, Any]:
    step_outputs = dict(outputs or {})
    step_outputs.setdefault("input_source", input_source)
    step.update({
        "status": status,
        "ok": status == "ok",
        "message": message,
        "finished_at": now_text(),
        "outputs": step_outputs,
        "error": error,
        "input_source": input_source,
    })
    return step


def _run_daily_paper_consistency_check(
    *,
    target_date: str,
    input_dir: str | Path = RESULTS_DIR,
    output_dir: str | Path = RESULTS_DIR,
    report_dir: str | Path = REPORTS_DIR,
    paper_payload: dict[str, Any] | None = None,
    execution_payload: dict[str, Any] | None = None,
    intraday_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the local-only paper-vs-backtest consistency gate for daily workflow."""

    try:
        from quantdesk.paper.paper_vs_backtest_consistency import run_paper_vs_backtest_consistency_check

        payload = run_paper_vs_backtest_consistency_check(
            target_date=target_date,
            input_dir=input_dir,
            output_dir=output_dir,
            report_dir=report_dir,
            paper_payload=paper_payload,
            execution_payload=execution_payload,
            intraday_payload=intraday_payload,
            network=False,
            strict=False,
            write=True,
        )
        result = dict(payload if isinstance(payload, dict) else {})
        result["source"] = "daily_workflow_auto_gate"
        result.setdefault("ok", result.get("status") != "failed")
        return result
    except Exception as exc:  # noqa: BLE001 - daily should surface, not crash
        return {
            "ok": False,
            "status": "warning",
            "source": "daily_workflow_auto_gate",
            "error": str(exc),
            "issue_count": 0,
            "warning_count": 1,
            "warnings": [
                {
                    "type": "paper_consistency_check_failed",
                    "message": str(exc),
                    "severity": "warning",
                }
            ],
        }


def _latest_file_warning(label: str, warnings: list[str]) -> None:
    warnings.append(f"{label}: 本步骤使用已有 latest 文件，可能不是本轮刚生成结果。")


def _dedupe_warnings(items: list[Any]) -> list[str]:
    """Return warnings in first-seen order without duplicate noise.

    Daily aggregates warning lists from several read-only snapshots.  Some
    limitations, such as offline realtime quotes or missing minute bars, are
    shared by multiple snapshots and otherwise appear twice at the top level.
    Keep the child snapshot warnings intact, but make the top-level daily list
    a concise digest.
    """

    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _low_price_status() -> dict[str, Any]:
    path = low_price_universe_path()
    if not path.exists():
        return {
            "ok": False,
            "exists": False,
            "path": str(path),
            "included_count": 0,
            "message": "低价学习池不存在，请先构建低价学习池。",
        }
    frame = load_low_price_universe(path)
    summary = summarize_low_price_universe(frame, output_path=path)
    return {
        "ok": True,
        "exists": True,
        "path": str(path),
        "included_count": int(summary.get("included_count") or 0),
        "total_count": int(summary.get("total_count") or len(frame)),
        "message": "低价学习池状态只读检查完成。",
    }


def _run_low_price_report(limit: int = 0) -> dict[str, Any]:
    try:
        from scripts.generate_low_price_report import analyze_low_price_universe, write_outputs

        payload = analyze_low_price_universe(limit=limit)
        files = write_outputs(payload)
        payload["files"] = files
        return payload
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"低价学习池报告生成失败：{exc}"}


def _portfolio_valuation_date(portfolio_payload: dict[str, Any]) -> str:
    dates = [
        str(row.get("latest_date") or "")[:10]
        for row in portfolio_payload.get("positions") or []
        if isinstance(row, dict) and str(row.get("latest_date") or "")[:10]
    ]
    return max(dates) if dates else ""


def _positions_from_state() -> list[dict[str, Any]]:
    state = PaperStateStore(
        AUTO_PAPER_PORTFOLIO_STATE_JSON,
        schema_version=API_SCHEMA_VERSION,
        max_backups=7,
    ).read({})
    positions = state.get("positions")
    if isinstance(positions, list):
        return [dict(row) for row in positions if isinstance(row, dict)]
    return []


def _stale_portfolio_payload(freshness_gate: dict[str, Any]) -> dict[str, Any]:
    state = PaperStateStore(
        AUTO_PAPER_PORTFOLIO_STATE_JSON,
        schema_version=API_SCHEMA_VERSION,
        max_backups=7,
    ).read({})
    positions = [
        dict(row)
        for row in (state.get("positions") or [])
        if isinstance(row, dict)
    ]
    decision_review = waiting_decision_review([], freshness_gate)
    yesterday_review = waiting_yesterday_holding_review(positions, freshness_gate)
    return {
        "ok": True,
        "exists": AUTO_PAPER_PORTFOLIO_STATE_JSON.exists(),
        "skipped": True,
        "mode": "stale_observation",
        "message": "暂无有效复盘：本地完整日线尚未更新到目标完整交易日，不更新自动虚拟组合估值，不生成昨日策略正确/失败结论。",
        "summary": {
            "position_count": len(positions),
            "daily_pnl": None,
            "total_return_pct": None,
            "portfolio_review_valid": False,
        },
        "positions": positions,
        "actions": [],
        "daily_actions": [],
        "decision_review": decision_review,
        "yesterday_holding_review": yesterday_review,
        "data_freshness_gate": freshness_gate,
        "state_recovered_from_backup": bool(state.get("state_recovered_from_backup")),
        "state_recovery_source": state.get("state_recovery_source") or "",
        "state_recovery_warning": state.get("state_recovery_warning") or "",
        "portfolio_review_valid": False,
        "conditional_plan_summary": {"symbol_count": 0},
        "conditional_execution_summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0},
        "conditional_review": {
            "ok": False,
            "review_valid": False,
            "missing_intraday_count": 0,
            "explanation": "完整日线控制收益复盘，分钟线控制盘中触发复盘。",
            "reason": freshness_gate.get("reason") or "",
        },
        "disclaimer": "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。",
        "broker_boundary": "不接券商，不真实下单，不保存账户密码。",
    }


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _human_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {
        "passed": "通过",
        "ok": "通过",
        "warning": "注意",
        "partial": "部分完成",
        "missing": "缺失",
        "failed": "失败",
        "stale": "滞后",
        "skipped": "已跳过",
        "disabled": "未启用",
    }.get(text, str(value or "-") or "-")


def _extract_strategy_counts(strategy_payload: dict[str, Any]) -> tuple[int, int]:
    summary = strategy_payload.get("summary") if isinstance(strategy_payload.get("summary"), dict) else {}
    long_count = _int(summary.get("long_candidate_count"), len(strategy_payload.get("long_candidates") or []))
    board_count = _int(summary.get("board_candidate_count"), len(strategy_payload.get("board_candidates") or []))
    return long_count, board_count


def _build_summary(payload: dict[str, Any]) -> dict[str, Any]:
    theme = payload.get("theme_map_status") if isinstance(payload.get("theme_map_status"), dict) else {}
    strategy_payload = payload.get("auto_strategy") if isinstance(payload.get("auto_strategy"), dict) else {}
    long_count, board_count = _extract_strategy_counts(strategy_payload)
    plan_summary = payload.get("conditional_plan_summary") if isinstance(payload.get("conditional_plan_summary"), dict) else {}
    execution_summary = payload.get("conditional_execution_summary") if isinstance(payload.get("conditional_execution_summary"), dict) else {}
    portfolio_payload = payload.get("auto_paper_portfolio") if isinstance(payload.get("auto_paper_portfolio"), dict) else {}
    portfolio_summary = portfolio_payload.get("summary") if isinstance(portfolio_payload.get("summary"), dict) else {}
    dates = payload.get("dates") if isinstance(payload.get("dates"), dict) else {}
    daily_price = payload.get("daily_price_status") if isinstance(payload.get("daily_price_status"), dict) else {}
    intraday_status_payload = payload.get("intraday_status") if isinstance(payload.get("intraday_status"), dict) else {}
    research_snapshot = payload.get("research_dashboard_snapshot") if isinstance(payload.get("research_dashboard_snapshot"), dict) else {}
    multi_book_snapshot = payload.get("multi_strategy_account_book_snapshot") if isinstance(payload.get("multi_strategy_account_book_snapshot"), dict) else {}
    intraday_live_snapshot = payload.get("intraday_live_snapshot") if isinstance(payload.get("intraday_live_snapshot"), dict) else {}
    risk_attribution_snapshot = payload.get("risk_attribution_snapshot") if isinstance(payload.get("risk_attribution_snapshot"), dict) else {}
    strategy_recommendation_snapshot = payload.get("strategy_recommendation_snapshot") if isinstance(payload.get("strategy_recommendation_snapshot"), dict) else {}
    risk_alert_snapshot = payload.get("risk_alert_snapshot") if isinstance(payload.get("risk_alert_snapshot"), dict) else {}
    research_snapshots = payload.get("research_snapshots") if isinstance(payload.get("research_snapshots"), dict) else {}
    experiment_tracker_snapshot = payload.get("experiment_tracker_snapshot") if isinstance(payload.get("experiment_tracker_snapshot"), dict) else {}
    daily_price_update = payload.get("daily_price_update") if isinstance(payload.get("daily_price_update"), dict) else {}
    freshness = payload.get("data_freshness_gate") if isinstance(payload.get("data_freshness_gate"), dict) else {}
    scope_stale_count = (
        freshness.get("stale_symbol_count")
        if "stale_symbol_count" in freshness
        else daily_price.get("stale_count") or daily_price_update.get("stale_count_after")
    )
    scope_missing_count = (
        freshness.get("missing_symbol_count")
        if "missing_symbol_count" in freshness
        else daily_price.get("missing_count") or daily_price_update.get("missing_count_after")
    )
    daily_pool_stale_count = freshness.get("daily_pool_stale_symbol_count", daily_price.get("stale_count"))
    daily_pool_missing_count = freshness.get("daily_pool_missing_symbol_count", daily_price.get("missing_count"))
    daily_pool_partial = (
        bool(freshness.get("daily_pool_partial"))
        if "daily_pool_partial" in freshness
        else bool(_int(daily_pool_stale_count) or _int(daily_pool_missing_count))
    )
    return {
        "report_generated_at": payload.get("generated_at") or dates.get("report_generated_at") or "",
        "data_freshness_status": freshness.get("data_freshness_status") or ("stale" if freshness.get("is_daily_stale") else ("feature_stale" if freshness.get("is_feature_stale") else "fresh")),
        "target_complete_trade_date": freshness.get("target_complete_trade_date") or daily_price.get("target_date") or daily_price_update.get("target_date") or "",
        "stale_symbol_count": _int(scope_stale_count),
        "missing_symbol_count": _int(scope_missing_count),
        "review_scope_stale_symbol_count": _int(freshness.get("review_scope_stale_symbol_count", scope_stale_count)),
        "review_scope_missing_symbol_count": _int(freshness.get("review_scope_missing_symbol_count", scope_missing_count)),
        "daily_pool_partial": daily_pool_partial,
        "daily_pool_stale_symbol_count": _int(daily_pool_stale_count),
        "daily_pool_missing_symbol_count": _int(daily_pool_missing_count),
        "daily_pool_common_coverage_date": freshness.get("daily_pool_latest_date") or daily_price.get("latest_local_date") or "",
        "daily_pool_max_latest_date": daily_price.get("max_local_date") or daily_price.get("latest_date_max") or daily_price.get("latest_local_date") or "",
        "review_mode": freshness.get("review_mode") or ("stale_observation" if freshness.get("stale_mode") else "current_review"),
        "portfolio_review_valid": bool(freshness.get("portfolio_review_valid", True)),
        "intraday_review_valid": bool(freshness.get("intraday_review_valid", False)),
        "message": freshness.get("message") or "",
        "local_daily_latest_date": freshness.get("local_daily_latest_date") or dates.get("local_complete_daily_date") or "",
        "daily_feature_date": dates.get("daily_feature_date") or "",
        "daily_price_target_date": daily_price.get("target_date") or daily_price_update.get("target_date") or "",
        "daily_price_is_stale": bool(daily_price.get("is_stale")),
        "daily_price_stale_count": _int(daily_price.get("stale_count") or daily_price_update.get("stale_count_after")),
        "daily_price_missing_count": _int(daily_price.get("missing_count") or daily_price_update.get("missing_count_after")),
        "daily_price_updated_symbols": _int(daily_price_update.get("updated_symbols")),
        "daily_price_failed_symbols": _int(daily_price_update.get("failed_symbols")),
        "daily_price_update_status": daily_price_update.get("status") or "",
        "theme_mapped_count": _int(theme.get("mapped_count") or theme.get("total_mapped")),
        "theme_unknown_count": _int(theme.get("unknown_count")),
        "theme_pending_count": _int(theme.get("pending_count")),
        "theme_failed_reason": theme.get("failed_reason") or "",
        "long_candidate_count": long_count,
        "board_candidate_count": board_count,
        "legacy_research_status": strategy_payload.get("status", "requested"),
        "formal_candidate_intake": payload.get("formal_candidate_intake") or {},
        "conditional_plan_count": _int(plan_summary.get("symbol_count") or plan_summary.get("total_symbols")),
        "execution_event_count": _int(execution_summary.get("event_count")),
        "no_fill_count": _int(execution_summary.get("no_fill_count")),
        "missing_intraday_count": _int(execution_summary.get("missing_intraday_count")),
        "intraday_required_symbols": _int(intraday_status_payload.get("total_symbols")),
        "intraday_available_symbols": _int(intraday_status_payload.get("available_count")),
        "intraday_missing_symbols": _int(intraday_status_payload.get("missing_count")),
        "intraday_invalid_symbols": _int(intraday_status_payload.get("invalid_count")),
        "intraday_gap_review_valid": _int(intraday_status_payload.get("available_count")) > 0
        and _int(intraday_status_payload.get("missing_count")) == 0
        and _int(intraday_status_payload.get("invalid_count")) == 0,
        "portfolio_position_count": _int(portfolio_summary.get("position_count"), len(portfolio_payload.get("positions") or [])),
        "today_pnl": _float(portfolio_summary.get("daily_pnl")),
        "total_return_pct": _float(portfolio_summary.get("total_return_pct")),
        "account_scale": portfolio_summary.get("account_scale") or "",
        "account_scale_note": portfolio_summary.get("account_scale_note") or "",
        "research_dashboard_status": research_snapshot.get("status") or "",
        "research_readiness_level": research_snapshot.get("readiness_level") or "",
        "research_warning_count": _int(research_snapshot.get("warning_count")),
        "research_dashboard_report": research_snapshot.get("latest_report") or "",
        "pit_universe": bool(research_snapshot.get("pit_universe")),
        "pit_status": research_snapshot.get("pit_status") or "",
        "multi_strategy_book_status": multi_book_snapshot.get("status") or "",
        "multi_strategy_book_portfolio": multi_book_snapshot.get("portfolio_name") or "",
        "multi_strategy_book_sub_account_count": _int(multi_book_snapshot.get("sub_account_count")),
        "multi_strategy_book_report": multi_book_snapshot.get("latest_report") or "",
        "intraday_live_status": intraday_live_snapshot.get("status") or "",
        "intraday_live_snapshot_time": intraday_live_snapshot.get("snapshot_time") or "",
        "intraday_live_report": intraday_live_snapshot.get("latest_report") or "",
        "intraday_live_warning_count": len(intraday_live_snapshot.get("warnings") or []),
        "risk_attribution_status": risk_attribution_snapshot.get("status") or "",
        "risk_attribution_portfolio": risk_attribution_snapshot.get("portfolio_name") or "",
        "risk_attribution_report": risk_attribution_snapshot.get("latest_report") or "",
        "risk_attribution_warning_count": len(risk_attribution_snapshot.get("warnings") or []),
        "strategy_recommendation_status": strategy_recommendation_snapshot.get("status") or "",
        "strategy_recommendation_count": _int(strategy_recommendation_snapshot.get("recommendation_count")),
        "strategy_recommendation_report": strategy_recommendation_snapshot.get("latest_report") or "",
        "strategy_recommendation_warning_count": len(strategy_recommendation_snapshot.get("warnings") or []),
        "risk_alert_status": risk_alert_snapshot.get("status") or "",
        "risk_alert_count": _int(risk_alert_snapshot.get("alert_count")),
        "risk_alert_critical_count": _int(risk_alert_snapshot.get("critical_count")),
        "smart_stop_triggered_count": _int((risk_alert_snapshot.get("smart_stop_summary") or {}).get("triggered_count") if isinstance(risk_alert_snapshot.get("smart_stop_summary"), dict) else 0),
        "risk_alert_report": risk_alert_snapshot.get("latest_report") or "",
        "research_snapshots_status": research_snapshots.get("status") or "",
        "similar_day_status": ((research_snapshots.get("snapshots") or {}).get("similar_day") or {}).get("status") if isinstance(research_snapshots.get("snapshots"), dict) else "",
        "stress_scenarios_status": ((research_snapshots.get("snapshots") or {}).get("stress_scenarios") or {}).get("status") if isinstance(research_snapshots.get("snapshots"), dict) else "",
        "style_attribution_status": ((research_snapshots.get("snapshots") or {}).get("style_attribution") or {}).get("status") if isinstance(research_snapshots.get("snapshots"), dict) else "",
        "decision_journal_status": ((research_snapshots.get("snapshots") or {}).get("decision_journal") or {}).get("status") if isinstance(research_snapshots.get("snapshots"), dict) else "",
        "experiment_tracker_status": experiment_tracker_snapshot.get("status") or "",
        "experiment_tracker_count": _int(experiment_tracker_snapshot.get("experiment_count")),
        "experiment_tracker_report": experiment_tracker_snapshot.get("latest_index") or "",
    }


def _latest_status(path: str, default: str = "missing") -> tuple[str, list[str]]:
    payload = read_json_safe(PROJECT_ROOT / path, {})
    return _guardrail_payload_status(payload, default=default, missing_warning=f"{path} not generated")


def _guardrail_payload_status(
    payload: Any,
    *,
    default: str = "missing",
    missing_warning: str = "",
) -> tuple[str, list[str]]:
    if not isinstance(payload, dict) or not payload:
        return default, [missing_warning] if missing_warning else []
    status = str(payload.get("status") or payload.get("workflow_status") or default).strip().lower()
    if status in {"ok", "success", "completed"}:
        status = "passed"
    if status not in {"passed", "warning", "failed", "missing", "skipped", "unavailable", "partial"}:
        status = default
    warnings = [str(item) for item in (payload.get("warnings") or []) if item] if isinstance(payload.get("warnings"), list) else []
    return status, warnings[:5]


def _guardrail_symbol(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _guardrail_symbols_from_strategy(strategy_payload: dict[str, Any] | None, *, limit: int = 30) -> list[str]:
    symbols: list[str] = []

    def add(value: Any) -> None:
        symbol = _guardrail_symbol(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    def walk(value: Any) -> None:
        if len(symbols) >= limit:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
                if len(symbols) >= limit:
                    break
            return
        if not isinstance(value, dict):
            return
        for key in ("symbol", "code", "stock_symbol", "ts_code"):
            add(value.get(key))
            if len(symbols) >= limit:
                return
        for key in (
            "standard_candidates",
            "long_candidates",
            "board_candidates",
            "candidates",
            "candidate_pool",
            "items",
            "items_sample",
            "stock_candidates",
        ):
            if key in value:
                walk(value.get(key))
                if len(symbols) >= limit:
                    return

    walk(strategy_payload or {})
    return symbols[:limit]


def _run_quick_guardrail_diagnostics(
    *,
    target_date: str,
    strategy_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run lightweight local diagnostics used by daily quick guardrails."""

    diagnostics: dict[str, Any] = {
        "active": True,
        "mode": "quick",
        "network": False,
        "generated_at": now_text(),
        "symbols": [],
        "feature_quality": {"status": "missing"},
        "stats_validation": {"status": "missing"},
    }
    feature_payload: dict[str, Any] = {}
    stats_payload: dict[str, Any] = {}
    symbols = _guardrail_symbols_from_strategy(strategy_payload, limit=30)
    diagnostics["symbols"] = symbols

    try:
        from quantdesk.research.feature_quality_audit import audit_feature_quality

        feature_kwargs: dict[str, Any] = {
            "target_date": target_date,
            "network": False,
            "write": True,
            "input_dir": DAILY_DIR,
            "output_path": RESULTS_DIR / "feature_quality_audit_latest.json",
            "report_path": REPORTS_DIR / "feature_quality_audit_report_latest.md",
            "max_files": 5,
        }
        if symbols:
            feature_kwargs["symbols"] = ",".join(symbols)
        feature_payload = audit_feature_quality(**feature_kwargs)
        diagnostics["feature_quality"] = {
            "status": feature_payload.get("status") or "missing",
            "row_count": feature_payload.get("row_count", 0),
            "feature_count": feature_payload.get("feature_count", 0),
            "warning_count": len(feature_payload.get("warnings") or []),
            "source": "active_daily_guardrail",
        }
    except Exception as exc:  # noqa: BLE001
        feature_payload = {"status": "warning", "warnings": [f"feature-audit active run failed: {exc}"]}
        diagnostics["feature_quality"] = {"status": "warning", "error": str(exc), "source": "active_daily_guardrail"}

    try:
        from quantdesk.research.backtest_statistical_validation import run_backtest_statistical_validation

        stats_payload = run_backtest_statistical_validation(
            symbol=symbols[0] if symbols else "",
            target_date=target_date,
            backtest_result_path=RESULTS_DIR / "backtest" / "backtest_latest.json",
            iterations=200,
            seed=42,
            network=False,
            strict=False,
            write=True,
            output_path=RESULTS_DIR / "backtest_statistical_validation_latest.json",
            report_path=REPORTS_DIR / "backtest_statistical_validation_report_latest.md",
        )
        diagnostics["stats_validation"] = {
            "status": stats_payload.get("status") or "missing",
            "sample_size": stats_payload.get("sample_size", 0),
            "warning_count": len(stats_payload.get("warnings") or []),
            "source": "active_daily_guardrail",
        }
    except Exception as exc:  # noqa: BLE001
        stats_payload = {"status": "warning", "warnings": [f"stats-validate active run failed: {exc}"]}
        diagnostics["stats_validation"] = {"status": "warning", "error": str(exc), "source": "active_daily_guardrail"}

    return diagnostics, feature_payload, stats_payload


def build_daily_guardrail_summary(
    *,
    target_date: str = "",
    daily_price_payload: dict[str, Any] | None = None,
    feature_payload: dict[str, Any] | None = None,
    strategy_payload: dict[str, Any] | None = None,
    portfolio_payload: dict[str, Any] | None = None,
    intraday_status_payload: dict[str, Any] | None = None,
    mode: str = "quick",
) -> dict[str, Any]:
    """Build quick governance summary without changing daily workflow logic."""

    warnings: list[str] = []
    target = str(target_date or (daily_price_payload or {}).get("target_date") or "")
    try:
        from quantdesk.strategy_lookahead_guard import validate_strategy_no_lookahead

        lookahead = validate_strategy_no_lookahead(
            target_date=target,
            inputs=daily_price_payload or {},
            features=feature_payload or {},
            signals=strategy_payload or {},
            executions=(portfolio_payload or {}).get("conditional_execution") or {},
            intraday_status=intraday_status_payload or {},
            strict=False,
        )
        lookahead_status = str(lookahead.get("status") or "warning")
        warnings.extend(str(item.get("message") or item) for item in (lookahead.get("issues") or [])[:5])
        warnings.extend(str(item.get("message") or item) for item in (lookahead.get("warnings") or [])[:5])
    except Exception as exc:  # noqa: BLE001
        lookahead_status = "warning"
        warnings.append(f"lookahead quick check failed: {exc}")

    try:
        from quantdesk.execution_assumption_guard import validate_execution_assumptions

        execution = validate_execution_assumptions(
            (portfolio_payload or {}).get("conditional_execution") or {},
            strict=False,
        )
        execution_status = str(execution.get("status") or "warning")
        warnings.extend(str(item.get("message") or item) for item in (execution.get("issues") or [])[:5])
        warnings.extend(str(item.get("message") or item) for item in (execution.get("warnings") or [])[:5])
    except Exception as exc:  # noqa: BLE001
        execution_status = "warning"
        warnings.append(f"execution assumption quick check failed: {exc}")

    diagnostics, feature_guardrail_payload, stats_guardrail_payload = _run_quick_guardrail_diagnostics(
        target_date=target,
        strategy_payload=strategy_payload,
    )
    feature_status, feature_warnings = _guardrail_payload_status(
        feature_guardrail_payload,
        default="missing",
        missing_warning="results/feature_quality_audit_latest.json not generated",
    )
    stats_status, stats_warnings = _guardrail_payload_status(
        stats_guardrail_payload,
        default="missing",
        missing_warning="results/backtest_statistical_validation_latest.json not generated",
    )
    param_status, param_warnings = _latest_status("results/parameter_sensitivity_latest.json")
    warnings.extend(feature_warnings)
    warnings.extend(stats_warnings)
    warnings.extend(param_warnings)
    seen: set[str] = set()
    clean_warnings: list[str] = []
    for item in warnings:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            clean_warnings.append(text[:240])
    return {
        "mode": mode or "quick",
        "target_date": target,
        "status": "warning" if clean_warnings or "failed" in {lookahead_status, execution_status, feature_status, stats_status, param_status} else "passed",
        "lookahead": lookahead_status,
        "execution_assumption": execution_status,
        "feature_quality": feature_status,
        "stats_validation": stats_status,
        "parameter_sensitivity": param_status,
        "lookahead_status": lookahead_status,
        "execution_assumption_status": execution_status,
        "feature_quality_status": feature_status,
        "statistical_validation_status": stats_status,
        "parameter_sensitivity_status": param_status,
        "warning_count": len(clean_warnings),
        "failed_count": sum(1 for item in (lookahead_status, execution_status, feature_status, stats_status, param_status) if item == "failed"),
        "top_warnings": clean_warnings[:3],
        "warnings": clean_warnings,
        "active_diagnostics": diagnostics,
        "failed_is_blocking": False,
        "disclaimer": "quick guardrails 是流程和数据可信度检查，不是收益保证；不改变策略参数、评分公式或回测收益逻辑。",
    }


def _workflow_status(steps: list[dict[str, Any]], errors: list[str]) -> str:
    if errors and all(step.get("status") in {"failed", "skipped"} for step in steps):
        return "failed"
    incomplete = any(
        step.get("status") in {"warning", "failed"}
        or (
            step.get("status") == "skipped"
            and (step.get("outputs") or {}).get("reason")
            not in {"not_requested", "no_existing_positions", "no_execution_symbols"}
        )
        for step in steps
    )
    if errors or incomplete:
        return "partial"
    return "completed"


def _payload_status(workflow_status: str) -> str:
    if workflow_status == "completed":
        return "passed"
    if workflow_status == "failed":
        return "failed"
    return "warning"


def _candidate_intraday_required(candidate: dict[str, Any]) -> bool:
    lane_text = " ".join(
        str(candidate.get(key) or "")
        for key in ("strategy_type", "strategy_label", "strategy_name", "strategy_lane", "detail_lane", "group_key")
    )
    return "board" in lane_text.lower() or "da_ban" in lane_text.lower() or "打板" in lane_text or "涨停" in lane_text


def _attach_reference_confidence_to_candidates(
    candidates: list[dict[str, Any]],
    *,
    target_date: str | None = None,
) -> list[dict[str, Any]]:
    """Attach research-only evidence confidence fields to workflow candidates."""

    enriched: list[dict[str, Any]] = []
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        symbol = normalize_symbol(str(item.get("symbol") or item.get("ts_code") or ""))
        row_target_date = str(
            target_date
            or item.get("signal_date")
            or item.get("select_date")
            or item.get("entry_date")
            or ""
        )[:10]
        intraday_required = bool(item.get("intraday_required")) or _candidate_intraday_required(item)
        confidence = build_reference_confidence(
            {
                **item,
                "symbol": symbol,
                "latest_close": item.get("latest_close") or item.get("current_price") or item.get("close") or item.get("price"),
                "daily_amount": item.get("daily_amount") or item.get("amount_ma20") or item.get("amount"),
                "intraday_required": intraday_required,
                "minute_data_available": bool(item.get("minute_data_available")),
                "data_quality": item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {},
            },
            target_date=row_target_date,
            project_root=PROJECT_ROOT,
        )
        item.update(confidence)
        enriched.append(item)
    return enriched


def _reference_confidence_report_lines(candidates: list[dict[str, Any]]) -> list[str]:
    if not candidates:
        return [
            "## 参考可信度分层",
            "",
            "- 暂无候选样本可做参考可信度分层。",
            "",
        ]
    counts = {"research_ready": 0, "partial": 0, "degraded": 0, "missing": 0}
    labels = {
        "research_ready": "高可信参考",
        "partial": "部分可信参考",
        "degraded": "降级参考",
        "missing": "证据缺失",
    }
    gap_counts: dict[str, dict[str, Any]] = {}
    rows: list[str] = []
    for candidate in candidates:
        confidence = candidate.get("data_confidence") if isinstance(candidate.get("data_confidence"), dict) else {}
        status = str(confidence.get("status") or "missing")
        if status not in counts:
            status = "missing"
        counts[status] += 1
        evidence = candidate.get("missing_evidence") if isinstance(candidate.get("missing_evidence"), list) else []
        evidence_kinds = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "unknown_gap")
            evidence_kinds.append(kind)
            bucket = gap_counts.setdefault(
                kind,
                {
                    "kind": kind,
                    "severity": str(item.get("severity") or "warning"),
                    "message": str(item.get("message") or kind),
                    "suggested_action": str(item.get("suggested_action") or "检查本地数据后再复核。"),
                    "count": 0,
                },
            )
            bucket["count"] = int(bucket.get("count") or 0) + 1
        reasons = []
        for key in ("confidence_reasons", "degradation_reasons"):
            values = candidate.get(key) if isinstance(candidate.get(key), list) else []
            reasons.extend(str(value) for value in values[:2] if value)
        rows.append(
            f"- {candidate.get('symbol') or '-'} {candidate.get('name') or ''}："
            f"参考可信度={confidence.get('label') or labels[status]} / {confidence.get('score', '-')}; "
            f"验证状态={candidate.get('verification_status_label') or candidate.get('verification_status') or '-'}; "
            f"缺失证据={', '.join(evidence_kinds) if evidence_kinds else '暂无关键缺口'}; "
            f"为什么可信 / 哪里降级={'；'.join(reasons) if reasons else confidence.get('note', '-')}"
        )
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    top_gaps = sorted(
        gap_counts.values(),
        key=lambda item: (severity_rank.get(str(item.get("severity")), 3), -int(item.get("count") or 0), str(item.get("kind"))),
    )
    lines = [
        "## 参考可信度分层",
        "",
        f"- 参考可信度：高可信参考 {counts['research_ready']}；部分可信参考 {counts['partial']}；降级参考 {counts['degraded']}；证据缺失 {counts['missing']}。",
        "- 验证状态、缺失证据只说明本地证据是否足够复核，不构成买卖、仓位或目标价建议。",
        "",
        "### 候选证据摘要",
        "",
        *rows[:12],
        "",
        "### 最值得补的数据缺口",
        "",
    ]
    if top_gaps:
        for gap in top_gaps[:8]:
            lines.append(
                f"- {gap.get('kind')}（{gap.get('severity')}，{gap.get('count')} 个）："
                f"{gap.get('message')}；建议：{gap.get('suggested_action')}"
            )
    else:
        lines.append("- 暂无关键缺口。")
    lines.append("")
    return lines


def build_daily_workflow_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else _build_summary(payload)
    theme = payload.get("theme_map_status") if isinstance(payload.get("theme_map_status"), dict) else {}
    provider_results = theme.get("provider_results") if isinstance(theme.get("provider_results"), list) else []
    lines = [
        "# QuantDesk 今日一键复盘",
        "",
        DISCLAIMER,
        "",
        f"- 工作流状态：{payload.get('workflow_status', '-')}",
        f"- 报告生成时间：{summary.get('report_generated_at') or '-'}",
        f"- 本地完整日线最新日期：{summary.get('local_daily_latest_date') or '-'}",
        f"- 日线特征日期：{summary.get('daily_feature_date') or '-'}",
        "",
        "## 数据新鲜度门禁",
        "",
        f"- 数据新鲜度状态：{summary.get('data_freshness_status') or '-'}",
        f"- 目标完整交易日：{summary.get('target_complete_trade_date') or summary.get('daily_price_target_date') or '-'}",
        f"- 本地完整日线最新日期：{summary.get('local_daily_latest_date') or '-'}",
        f"- 当前复核范围滞后股票数：{summary.get('review_scope_stale_symbol_count', summary.get('stale_symbol_count', 0))}",
        f"- 当前复核范围缺失股票数：{summary.get('review_scope_missing_symbol_count', summary.get('missing_symbol_count', 0))}",
        f"- 低价研究池日期滞后股票数：{summary.get('daily_pool_stale_symbol_count', summary.get('daily_price_stale_count', 0))}",
        f"- 低价研究池缺失股票数：{summary.get('daily_pool_missing_symbol_count', summary.get('daily_price_missing_count', 0))}",
        f"- 复盘模式：{summary.get('review_mode') or '-'}",
        f"- 昨日持仓复盘有效：{'是' if summary.get('portfolio_review_valid') else '否'}",
        f"- 盘中触发验证有效：{'是' if summary.get('intraday_review_valid') else '否'}",
        f"- 说明：{summary.get('message') or '完整日线控制收益复盘，分钟线控制盘中触发复盘。'}",
        "",
        "如果本地日线仍停留在旧日期，系统不会假装做目标交易日复盘；候选仅作为滞后数据观察。",
        "缺少完整日线时显示暂无有效复盘；缺少分钟线时只影响盘中条件触发验证。",
        "",
        "## 板块题材状态",
        "",
        f"- 已映射：{summary.get('theme_mapped_count', 0)} 只。",
        f"- 未知：{summary.get('theme_unknown_count', 0)} 只。",
        f"- 待补：{summary.get('theme_pending_count', 0)} 只。",
        f"- 失败原因：{summary.get('theme_failed_reason') or '-'}。",
    ]
    daily_price_update = payload.get("daily_price_update") if isinstance(payload.get("daily_price_update"), dict) else {}
    daily_provider_results = daily_price_update.get("provider_results") if isinstance(daily_price_update.get("provider_results"), list) else []
    lines += [
        "",
        "## 日线数据状态",
        "",
        f"- 当前复核范围完整日线日期：{summary.get('local_daily_latest_date') or '-'}",
        f"- 低价研究池共同覆盖日期下限：{summary.get('daily_pool_common_coverage_date') or '-'}",
        f"- 低价研究池最晚覆盖日期：{summary.get('daily_pool_max_latest_date') or '-'}",
        f"- 理论目标完整交易日：{summary.get('daily_price_target_date') or '-'}",
        f"- 低价研究池是否存在日期滞后：{'是' if summary.get('daily_price_is_stale') else '否'}",
        f"- 低价研究池日期滞后股票数：{summary.get('daily_price_stale_count', 0)}",
        f"- 低价研究池缺失股票数：{summary.get('daily_price_missing_count', 0)}",
        f"- 是否尝试联网补全：{'是' if payload.get('allow_daily_price_online_update') else '否'}",
        f"- 更新成功数量：{summary.get('daily_price_updated_symbols', 0)}",
        f"- 更新失败数量：{summary.get('daily_price_failed_symbols', 0)}",
        f"- 本次日线更新状态：{summary.get('daily_price_update_status') or '-'}",
        "",
    ]
    if daily_provider_results:
        lines += ["### 日线 Provider 结果"]
        for row in daily_provider_results:
            lines.append(
                f"- {row.get('provider')}: {row.get('status')}，attempted={row.get('attempted', 0)}，updated={row.get('updated', 0)}，failed={row.get('failed', 0)}，原因：{row.get('failed_reason') or '-'}"
            )
        lines.append("")
    if payload.get("allow_daily_price_online_update") and _int(summary.get("daily_price_updated_symbols")) > 0:
        lines += [
            f"本次已将部分或全部低价池日线更新到 {summary.get('daily_price_target_date') or '-'}，并可基于更新后的完整日线重新生成特征和候选。",
            "",
        ]
    elif (
        summary.get("daily_price_is_stale")
        and summary.get("daily_pool_partial")
        and summary.get("data_freshness_status") == "fresh"
        and summary.get("portfolio_review_valid")
    ):
        lines += [
            (
                f"低价研究池仍有 {summary.get('daily_pool_stale_symbol_count', 0)} 只日期滞后、"
                f"{summary.get('daily_pool_missing_symbol_count', 0)} 只缺失；这些股票不会进入目标日候选。"
                f"当前复核范围已覆盖 {summary.get('local_daily_latest_date') or '-'}，可按新鲜度门禁继续复盘。"
            ),
            "",
        ]
    elif summary.get("daily_price_is_stale"):
        lines += [
            f"本次日线补全未完成，因此策略候选和特征可能仍基于 {summary.get('local_daily_latest_date') or '-'}。本结果仅用于观察，需注意数据滞后风险。",
            "",
        ]
    if provider_results:
        lines += ["", "### Provider 结果"]
        for row in provider_results:
            lines.append(
                f"- {row.get('provider')}: {row.get('status')}，mapped={row.get('mapped', 0)}，原因={row.get('failed_reason') or '-'}"
            )
    if _int(summary.get("theme_mapped_count")) == 0 and _int(summary.get("theme_unknown_count")) > 0:
        lines += [
            "",
            (
                "当前板块/题材映射不可用，因此候选暂归入未知题材。"
                "该问题影响题材分组和板块分散，不影响本地日线特征和纸面组合复盘继续运行。"
            ),
        ]
    lines += [
        "",
        "## 复盘摘要",
        "",
        f"- 策略候选数量：长线 {summary.get('long_candidate_count', 0)} 只，打板 {summary.get('board_candidate_count', 0)} 只。",
        f"- 条件计划数量：{summary.get('conditional_plan_count', 0)}。",
        f"- 盘中触发数量：{summary.get('execution_event_count', 0)}。",
        f"- 计划未触发数量：{summary.get('no_fill_count', 0)}。",
        f"- 缺少分钟线数量：{summary.get('missing_intraday_count', 0)}。",
        f"- 虚拟组合持仓数量：{summary.get('portfolio_position_count', 0)}。",
        f"- 今日盈亏：{summary.get('today_pnl', 0)}。",
        f"- 累计收益率：{summary.get('total_return_pct', 0)}%。",
        "",
        "## 工作流步骤",
        "",
    ]
    auto_strategy = payload.get("auto_strategy") if isinstance(payload.get("auto_strategy"), dict) else {}
    workflow_candidates = auto_strategy.get("standard_candidates") if isinstance(auto_strategy.get("standard_candidates"), list) else []
    if workflow_candidates and not any(isinstance(row, dict) and row.get("data_confidence") for row in workflow_candidates):
        workflow_candidates = _attach_reference_confidence_to_candidates(
            [row for row in workflow_candidates if isinstance(row, dict)],
            target_date=str(summary.get("target_complete_trade_date") or summary.get("daily_price_target_date") or "")[:10],
        )
    lines += _reference_confidence_report_lines([row for row in workflow_candidates if isinstance(row, dict)])

    guardrails = payload.get("guardrail_summary") if isinstance(payload.get("guardrail_summary"), dict) else {}
    if guardrails:
        lines += [
            "",
            "## 每日流程防护诊断",
            "",
            f"- 是否启用：{'是' if guardrails.get('enabled') else '否'}",
            f"- 检查模式：{'快速' if guardrails.get('mode') == 'quick' else guardrails.get('mode') or '-'}",
            f"- 前视检查：{_human_status(guardrails.get('lookahead_status'))}",
            f"- 执行假设：{_human_status(guardrails.get('execution_assumption_status'))}",
            f"- 特征审计：{_human_status(guardrails.get('feature_quality_status'))}",
            f"- 统计校验：{_human_status(guardrails.get('statistical_validation_status'))}",
            f"- 参数敏感性：{_human_status(guardrails.get('parameter_sensitivity_status'))}",
            f"- 警告数量：{guardrails.get('warning_count', 0)}",
            "",
            "防护诊断只追加治理提示，不改变策略参数、评分公式或回测收益逻辑。",
            "",
        ]
    paper_consistency = payload.get("paper_consistency_check") if isinstance(payload.get("paper_consistency_check"), dict) else {}
    if paper_consistency:
        lines += [
            "",
            "## 纸面账户与回测一致性检查",
            "",
            f"- 检查状态：{_human_status(paper_consistency.get('status'))}",
            f"- 问题数量：{paper_consistency.get('issue_count', 0)}",
            f"- 警告数量：{paper_consistency.get('warning_count', 0)}",
            f"- 来源：{paper_consistency.get('source') or '-'}",
            "",
            "本检查只比较本地纸面账户与回测结果，不接券商、不产生真实订单。",
            "",
        ]
    research_snapshot = payload.get("research_dashboard_snapshot") if isinstance(payload.get("research_dashboard_snapshot"), dict) else {}
    if research_snapshot:
        lines += ["", render_daily_research_snapshot_markdown(research_snapshot), ""]
    research_workflow_cards = payload.get("research_workflow_cards") if isinstance(payload.get("research_workflow_cards"), dict) else {}
    lines += ["", render_research_workflow_cards_markdown(research_workflow_cards), ""]
    multi_book_snapshot = payload.get("multi_strategy_account_book_snapshot") if isinstance(payload.get("multi_strategy_account_book_snapshot"), dict) else {}
    if multi_book_snapshot:
        lines += ["", render_daily_multi_strategy_snapshot_markdown(multi_book_snapshot), ""]
    intraday_live_snapshot = payload.get("intraday_live_snapshot") if isinstance(payload.get("intraday_live_snapshot"), dict) else {}
    if intraday_live_snapshot:
        lines += ["", render_daily_intraday_live_snapshot_markdown(intraday_live_snapshot), ""]
    risk_attribution_snapshot = payload.get("risk_attribution_snapshot") if isinstance(payload.get("risk_attribution_snapshot"), dict) else {}
    if risk_attribution_snapshot:
        lines += ["", render_daily_risk_attribution_snapshot_markdown(risk_attribution_snapshot), ""]
    strategy_recommendation_snapshot = payload.get("strategy_recommendation_snapshot") if isinstance(payload.get("strategy_recommendation_snapshot"), dict) else {}
    if strategy_recommendation_snapshot:
        lines += ["", render_daily_strategy_recommendation_snapshot_markdown(strategy_recommendation_snapshot), ""]
    risk_alert_snapshot = payload.get("risk_alert_snapshot") if isinstance(payload.get("risk_alert_snapshot"), dict) else {}
    if risk_alert_snapshot:
        lines += ["", render_daily_risk_alert_snapshot_markdown(risk_alert_snapshot), ""]
    research_snapshots = payload.get("research_snapshots") if isinstance(payload.get("research_snapshots"), dict) else {}
    if research_snapshots:
        lines += ["", render_daily_research_snapshots_markdown(research_snapshots), ""]
    experiment_tracker_snapshot = payload.get("experiment_tracker_snapshot") if isinstance(payload.get("experiment_tracker_snapshot"), dict) else {}
    if experiment_tracker_snapshot:
        lines += ["", render_daily_experiment_tracker_snapshot_markdown(experiment_tracker_snapshot), ""]
    plan_review = payload.get("plan_review_summary") or {}
    if plan_review:
        lines += ["", "## 到期计划证据", "",
                  f"- 已复核 {plan_review.get('reviewed_count', 0)}；复核通过 {plan_review.get('review_passed_count', 0)}；"
                  f"已模拟 {plan_review.get('replayed_count', 0)}；账本应用记录 {plan_review.get('applied_count', 0)}。",
                  f"- 证据不足 {plan_review.get('evidence_insufficient_count', 0)}；旧收据未绑定计划 {plan_review.get('legacy_unbound_count', 0)}；"
                  f"尚未闭合 {plan_review.get('unresolved_count', 0)}。"]
        for item in (payload.get("plan_review_items") or [])[:20]:
            reasons = "; ".join(str(value) for value in item.get("missing_evidence_reasons") or [])
            lines.append(f"- {item.get('trade_date', '')} / {item.get('plan_id', '')}: {item.get('status', 'unreviewed')}。{reasons}")
    for step in payload.get("steps") or []:
        lines.append(
            f"- {step.get('name')}: {step.get('status')}。{step.get('message') or ''}"
            + (f" 错误：{step.get('error')}" if step.get("error") else "")
        )
    lines += [
        "",
        "## 风险提示",
        "",
        "当前板块/题材映射不可用，因此题材分组不可用；候选仍可生成，但无法按真实题材分散筛选。",
        "如果存在当日分钟线，系统只用于验证盘中条件触发，不会把盘中数据当作完整日线。",
        "系统采用条件交易计划进行纸面模拟：先根据历史数据生成计划，再根据盘中数据判断是否触发。缺少分钟线时不会用收盘价假装成交。",
        "本工具不接券商 API，不真实下单，不保存真实账户信息，不构成投资建议，不承诺收益。",
    ]
    lines += [
        "",
        "系统采用条件交易计划进行纸面模拟：先根据历史数据生成计划，再根据盘中数据判断是否触发。缺少分钟线时不会用收盘价假装成交。",
        "本工具不接券商 API，不真实下单，不保存真实账户信息，不构成投资建议，不承诺收益。",
        "如果板块/题材补全失败，一键复盘仍会继续输出工作流步骤、失败原因、策略候选、条件计划和虚拟组合摘要。",
    ]
    intraday_status_payload = payload.get("intraday_status") if isinstance(payload.get("intraday_status"), dict) else {}
    verified_minute_pool = (
        intraday_status_payload.get("verified_minute_pool")
        if isinstance(intraday_status_payload.get("verified_minute_pool"), dict)
        else {}
    )
    lines += [
        "",
        "## 分钟线与盘中执行",
        "",
        f"- 目标交易日：{intraday_status_payload.get('target_trade_date') or '-'}",
        f"- 需要分钟线股票数：{summary.get('intraday_required_symbols', intraday_status_payload.get('total_symbols', 0))}",
        f"- 已有分钟线股票数：{summary.get('intraday_available_symbols', intraday_status_payload.get('available_count', 0))}",
        f"- 缺少分钟线股票数：{summary.get('intraday_missing_symbols', intraday_status_payload.get('missing_count', 0))}",
        f"- 触发事件数量：{summary.get('execution_event_count', 0)}",
        f"- 计划未触发数量：{summary.get('no_fill_count', 0)}",
        f"- 缺分钟线次数：{summary.get('missing_intraday_count', 0)}",
        "",
        "完整日线控制收益复盘；分钟线控制盘中触发验证。",
        "完整日线已经可以用于收益复盘，但盘中低吸、突破、加仓、减仓、止盈、止损必须依赖分钟线验证。缺少分钟线时，系统不会使用收盘价、最高价或最低价假装成交。",
    ]
    if verified_minute_pool:
        source_label = {
            "last_passed": "最近一次通过质检的快照",
            "latest": "最新质检快照",
        }.get(str(verified_minute_pool.get("source_snapshot") or ""), "本地质检快照")
        lines += [
            "",
            f"- 已验证分钟池：{_human_status(verified_minute_pool.get('status'))}，可用 {verified_minute_pool.get('usable_count', 0)} 只",
            f"- 来源：{source_label}",
            f"- 最新质检状态：{_human_status(verified_minute_pool.get('latest_quality_status'))}",
        ]
        warning_labels = {
            "verified_pool_from_last_passed_snapshot": "使用最近一次通过质检的分钟池快照",
        }
        for warning in verified_minute_pool.get("warnings") or []:
            lines.append(f"- 告警：{warning_labels.get(str(warning), str(warning))}")
    return "\n".join(lines)


def newest_daily_workflow_report() -> Path | None:
    files = sorted(
        REPORTS_DIR.glob("daily_workflow_report_*.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _load_formal_candidate_batch_for_review(
    project_root: Path,
    *,
    target_date: str,
) -> dict[str, Any]:
    """Prefer an exact completed-chain batch, preserving degraded observation mode."""

    root = Path(project_root).resolve()
    latest_path = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    latest = read_json_safe(latest_path, {})
    latest = latest if isinstance(latest, dict) else {}
    completed_current = bool(
        latest
        and str(latest.get("status") or "") == "completed"
        and latest.get("authoritative") is True
        and latest.get("published_latest") is True
        and str(latest.get("target_date") or "")[:10] == str(target_date or "")[:10]
    )
    if not completed_current:
        loaded = load_formal_candidate_batch(root, target_date=target_date)
        return {
            **loaded,
            "source_mode": "dated_observation_snapshot",
        }

    run_id = str(latest.get("run_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", run_id):
        return {
            "status": "blocked",
            "batch": None,
            "path": "",
            "source_mode": "completed_chain_invalid",
            "reason_codes": ["formal_candidate_data_chain_run_id_invalid"],
        }
    run_path = (
        root
        / "results"
        / "data_chain"
        / "runs"
        / run_id
        / "manifest.json"
    )
    terminal = read_json_safe(run_path, {})
    if not isinstance(terminal, dict) or not terminal:
        return {
            "status": "blocked",
            "batch": None,
            "path": "",
            "source_mode": "completed_chain_unverified",
            "reason_codes": ["formal_candidate_data_chain_terminal_missing"],
        }
    if terminal != latest:
        return {
            "status": "blocked",
            "batch": None,
            "path": "",
            "source_mode": "completed_chain_unverified",
            "reason_codes": ["formal_candidate_data_chain_terminal_mismatch"],
        }
    return resolve_chain_bound_formal_candidate_batch(
        root,
        data_chain_manifest=terminal,
    )


def daily_workflow_latest() -> dict[str, Any]:
    if not DAILY_WORKFLOW_LATEST_JSON.exists():
        return {
            "ok": True,
            "exists": False,
            "path": str(DAILY_WORKFLOW_LATEST_JSON),
            "message": "尚未运行今日一键复盘。",
        }
    payload = read_json_safe(DAILY_WORKFLOW_LATEST_JSON, {})
    payload["exists"] = True
    return payload


def _review_daily_status(target_date: str) -> dict[str, Any]:
    return paper_review_data_status(
        read_json_safe(AUTO_PAPER_PORTFOLIO_STATE_JSON, {}),
        target_date=target_date, daily_dir=DAILY_DIR,
    )


def daily_workflow_status() -> dict[str, Any]:
    theme = theme_map_status()
    theme["current_candidate_theme_status"] = validate_theme_map_for_current_candidates()
    feature = daily_feature_status()
    target_date, _ = infer_target_date()
    daily_price = _review_daily_status(target_date)
    freshness = evaluate_paper_review_freshness(
        read_json_safe(AUTO_PAPER_PORTFOLIO_STATE_JSON, {}),
        target_date=target_date, daily_dir=DAILY_DIR,
    )
    latest = daily_workflow_latest()
    low_price = _low_price_status()
    payload = {
        "ok": True,
        "exists": bool(latest.get("exists")),
        "path": str(DAILY_WORKFLOW_LATEST_JSON),
        "low_price_status": low_price,
        "daily_feature_status": feature,
        "daily_price_status": daily_price,
        "data_freshness_gate": freshness,
        "theme_map_status": theme,
        "auto_paper_portfolio_exists": AUTO_PAPER_PORTFOLIO_STATE_JSON.exists(),
        "local_complete_daily_date": daily_price.get("latest_local_date") or "",
        "message": "今日一键复盘状态只读，不会生成文件、不会联网、不会运行重任务。",
    }
    if latest.get("exists"):
        payload["latest_summary"] = latest.get("summary", {})
        payload["summary"] = latest.get("summary", {})
        payload["steps"] = latest.get("steps", [])
        payload["workflow_status"] = latest.get("workflow_status", "")
        payload["generated_at"] = latest.get("generated_at", "")
    return payload


def run_daily_workflow(
    *,
    strategy: str = "all",
    allow_auto_init: bool = False,
    enable_intraday: bool = False,
    allow_daily_approx: bool = False,
    max_long_total: int = 20,
    max_board_total: int = 10,
    allow_theme_map_online_update: bool = False,
    allow_daily_price_online_update: bool = False,
    with_guardrails: bool = True,
    guardrails_mode: str = "quick",
    run_strategy_lab: bool = False,
    run_legacy_research: bool = False,
    run_brain_shadow: bool = False,
    target_date: str | None = None,
) -> dict[str, Any]:
    review_target_date, _ = infer_target_date(target_date)
    generated_at = now_text()
    run_id = f"daily_workflow_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    artifacts: dict[str, Any] = {}
    input_artifacts: dict[str, Any] = {}
    review_symbols: set[str] = set()
    due_loaded: dict[str, Any] = {}
    portfolio_ran = False

    low_status: dict[str, Any] = {}
    low_report: dict[str, Any] = {}
    low_summary: dict[str, Any] = {}
    feature_payload: dict[str, Any] = {}
    feature_status: dict[str, Any] = {}
    daily_price_payload: dict[str, Any] = {}
    daily_price_update: dict[str, Any] = {}
    theme_status: dict[str, Any] = {}
    theme_update: dict[str, Any] = {}
    strategy_payload: dict[str, Any] = {}
    formal_candidate_batch: dict[str, Any] | None = None
    formal_candidate_batch_path = ""
    formal_candidate_status = "missing"
    watchpool_payload: dict[str, Any] = {}
    portfolio_payload: dict[str, Any] = {}
    plan_review_payload: dict[str, Any] = {}
    intraday_status_payload: dict[str, Any] = {}
    paper_consistency_payload: dict[str, Any] = {}
    freshness_gate: dict[str, Any] = {}
    guardrail_summary: dict[str, Any] = {
        "enabled": False,
        "mode": guardrails_mode or "quick",
        "status": "skipped",
        "warning_count": 0,
        "failed_count": 0,
        "warnings": [],
        "top_warnings": [],
    }
    local_daily_date = ""

    def add_step(
        name: str,
        status: str,
        message: str = "",
        outputs: dict[str, Any] | None = None,
        error: str = "",
        input_source: str = "in_memory",
    ) -> None:
        if status == "skipped":
            input_source = "not_run"
        elif input_source == "in_memory":
            input_source = {
                "auto_strategy_sandbox": "generated_in_memory",
                "current_candidate_theme_sync": "candidates_in_memory",
                "conditional_trade_plan": "dated_due_plan_archive",
                "conditional_execution": "dated_due_plan_in_memory",
                "portfolio_update": "dated_formal_candidate_batch_and_due_plan",
            }.get(name, input_source)
        step = _start_step(name)
        steps.append(_finish_step(step, status, message, outputs=outputs, error=error, input_source=input_source))
        if status == "warning" and message:
            warnings.append(message)
        if status == "failed" and (error or message):
            errors.append(error or message)

    try:
        if run_legacy_research:
            try:
                low_status = _low_price_status()
                add_step(
                    "low_price_pool_check",
                    "ok" if low_status.get("ok") else "warning",
                    low_status.get("message", ""),
                    {"included_count": low_status.get("included_count", 0), "path": low_status.get("path", "")},
                )
            except Exception as exc:  # noqa: BLE001
                low_status = {"ok": False, "included_count": 0, "message": str(exc)}
                add_step("low_price_pool_check", "warning", "低价策略输入检查失败；持仓复盘单独检查。", error=str(exc))
        else:
            low_status = {"status": "skipped", "reason": "not_requested"}
            add_step("low_price_pool_check", "skipped", "低价过滤器研究未请求；持仓复盘独立检查。", low_status)

        try:
            daily_price_payload = _review_daily_status(review_target_date)
            local_daily_date = str(daily_price_payload.get("latest_local_date") or "")
            review_symbols.update(
                normalize_symbol(symbol) for symbol in daily_price_payload.get("symbols", [])
                if normalize_symbol(symbol)
            )
            if daily_price_payload.get("is_stale") and allow_daily_price_online_update:
                daily_price_update = {"status": "skipped", "reason": "pipeline_writer_required"}
                add_step(
                    "daily_cache_check",
                    "warning",
                    "日线更新由收盘数据流程统一处理；本次复盘只读已有日线。",
                    {"target_date": review_target_date, "latest_local_date": local_daily_date},
                )
            elif daily_price_payload.get("is_stale"):
                stale_count = _int(daily_price_payload.get("stale_count"))
                missing_count = _int(daily_price_payload.get("missing_count"))
                fresh_count = _int(daily_price_payload.get("fresh_count"))
                pool_max_daily_date = str(
                    daily_price_payload.get("max_local_date")
                    or daily_price_payload.get("latest_date_max")
                    or local_daily_date
                )
                partially_current = bool(
                    fresh_count
                    and pool_max_daily_date
                    and pool_max_daily_date >= str(daily_price_payload.get("target_date") or "")
                )
                add_step(
                    "daily_cache_check",
                    "warning",
                    (
                        f"持仓复盘范围有 {stale_count} 只日期滞后、{missing_count} 只缺失；"
                        "缺少目标日价格时不生成有效收益复盘，也不假装成交。"
                        if partially_current
                        else "持仓日线未覆盖复盘日期；本次仅展示已有记录，不把旧价格当作当日估值。"
                    ),
                    {
                        "target_date": daily_price_payload.get("target_date", ""),
                        "pool_common_coverage_date": local_daily_date,
                        "pool_max_latest_date": pool_max_daily_date,
                        "latest_local_date": local_daily_date,
                        "fresh_count": fresh_count,
                        "stale_count": stale_count,
                        "missing_count": missing_count,
                    },
                )
            elif daily_price_payload.get("status") == "not_applicable":
                add_step("daily_cache_check", "skipped", daily_price_payload["message"], {"reason": "no_existing_positions"})
            else:
                add_step(
                    "daily_cache_check",
                    "ok",
                    f"本地日线已覆盖理论目标完整交易日 {daily_price_payload.get('target_date') or '-'}。",
                    {"local_daily_latest_date": local_daily_date, "target_date": daily_price_payload.get("target_date", "")},
                )
        except Exception as exc:  # noqa: BLE001
            daily_price_payload = {"ok": False, "message": str(exc), "target_date": "", "is_stale": True}
            local_daily_date = ""
            add_step(
                "daily_cache_check",
                "warning",
                "持仓日线检查失败；价格证据未通过时不更新估值或模拟成交。",
                {"local_daily_latest_date": local_daily_date},
                error=str(exc),
            )

        if run_legacy_research:
            try:
                low_report = _run_low_price_report()
                low_summary = low_report.get("summary") if isinstance(low_report.get("summary"), dict) else {}
                low_files = low_report.get("files") if isinstance(low_report.get("files"), dict) else {}
                report_path = low_report.get("report_path") or low_files.get("markdown_path") or low_files.get("report_path")
                if report_path:
                    artifacts["low_price_report"] = report_path
                add_step(
                    "low_price_report",
                    "ok" if low_report.get("ok", True) else "warning",
                    low_report.get("message", "低价学习池报告已生成。"),
                    {"report_path": report_path or "", "analyzed_count": low_summary.get("analyzed_count", 0)},
                )
            except Exception as exc:  # noqa: BLE001
                add_step("low_price_report", "warning", "低价策略报告生成失败；不改变持仓复盘日期。", error=str(exc))
        else:
            low_report = {"status": "skipped", "reason": "not_requested"}
            add_step("low_price_report", "skipped", "低价学习报告未请求。", low_report)

        try:
            feature_status = daily_feature_status()
            feature_payload = {
                **feature_status, "status": "read_only",
                "message": "读取收盘流程生成的日线特征；本次不重建研究子集。",
            }
            input_artifacts["daily_features"] = feature_status.get("path", "data/features/daily_features_latest.csv")
            add_step(
                "daily_features",
                "ok" if feature_payload.get("ok") else "warning",
                feature_payload.get("message", "本地日线量化特征已生成。"),
                {"feature_count": feature_status.get("feature_count", 0), "latest_date": feature_status.get("latest_date", "")},
            )
        except Exception as exc:  # noqa: BLE001
            feature_status = {"ok": False, "status": "missing", "error": str(exc)}
            add_step("daily_features", "warning", "研究特征未读到；持仓估值仍逐只检查价格。", error=str(exc))

        try:
            freshness_gate = evaluate_paper_review_freshness(
                read_json_safe(AUTO_PAPER_PORTFOLIO_STATE_JSON, {}),
                target_date=review_target_date, daily_dir=DAILY_DIR,
            )
            gate_status = "warning" if freshness_gate.get("stale_mode") or not freshness_gate.get("can_review_yesterday_positions") else "ok"
            add_step(
                "data_freshness_gate",
                gate_status,
                freshness_gate.get("reason") or freshness_gate.get("message") or "",
                {
                    "target_complete_trade_date": freshness_gate.get("target_complete_trade_date", ""),
                    "local_daily_latest_date": freshness_gate.get("local_daily_latest_date", ""),
                    "daily_feature_date": freshness_gate.get("daily_feature_date", ""),
                    "review_mode": freshness_gate.get("review_mode", ""),
                    "portfolio_review_valid": freshness_gate.get("portfolio_review_valid", False),
                    "intraday_review_valid": freshness_gate.get("intraday_review_valid", False),
                    "boundary_note": "完整日线控制收益复盘，分钟线控制盘中触发复盘。",
                },
            )
        except Exception as exc:  # noqa: BLE001
            freshness_gate = {
                "ok": False,
                "is_daily_stale": True,
                "stale_mode": True,
                "can_review_yesterday_positions": False,
                "portfolio_review_valid": False,
                "intraday_review_valid": False,
                "review_mode": "stale_observation",
                "reason": str(exc),
            }
            add_step("data_freshness_gate", "warning", "数据新鲜度门禁检查失败，按滞后数据观察处理。", error=str(exc))

        try:
            if allow_theme_map_online_update:
                theme_update = update_theme_map(source="auto", only_low_price=True)
                theme_status = theme_map_status()
                mapped = _int(theme_status.get("mapped_count"))
                status = "ok" if mapped > 0 else "warning"
                message = theme_update.get("message") or theme_update.get("failed_reason") or "板块/题材映射已检查。"
                if mapped == 0:
                    message = "板块/题材补全失败，候选仍可生成，但题材分组不可用。" + (f" 原因：{theme_status.get('failed_reason')}" if theme_status.get("failed_reason") else "")
                add_step(
                    "theme_map_status",
                    status,
                    message,
                    {
                        "mapped_count": mapped,
                        "unknown_count": theme_status.get("unknown_count", 0),
                        "provider_results": theme_status.get("provider_results", []),
                    },
                )
            else:
                theme_update = {"ok": True, "message": "默认不联网补全板块/题材，仅检查本地映射状态。"}
                theme_status = theme_map_status()
                mapped = _int(theme_status.get("mapped_count"))
                add_step(
                    "theme_map_status",
                    "ok" if mapped > 0 else "warning",
                    "默认不联网补全板块/题材，仅检查本地映射状态。"
                    if mapped > 0
                    else "板块/题材映射不可用，候选暂归入未知题材；不影响后续本地复盘继续运行。",
                    {
                        "mapped_count": mapped,
                        "unknown_count": theme_status.get("unknown_count", 0),
                        "failed_reason": theme_status.get("failed_reason", ""),
                        "provider_results": theme_status.get("provider_results", []),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            theme_status = theme_map_status()
            add_step("theme_map_status", "warning", "板块/题材补全失败，候选仍可生成，但题材分组不可用。", error=str(exc))

        long_count = 0
        board_count = 0
        if run_legacy_research:
            try:
                strategy_payload = run_auto_strategy_cycle(
                    mode="live",
                    strategy=strategy,
                    initial_cash=_paper_initial_cash(),
                    max_long_total=max_long_total,
                    max_board_total=max_board_total,
                )
                standard_candidates = candidate_rows_from_strategy_payload(strategy_payload, strategy=strategy)
                standard_candidates = attach_theme_map_to_candidates(standard_candidates, load_theme_map(THEME_MAP_PATH))
                reference_target_date = str(
                    (freshness_gate or {}).get("target_complete_trade_date")
                    or daily_price_payload.get("target_date")
                    or local_daily_date
                    or ""
                )[:10]
                standard_candidates = _attach_reference_confidence_to_candidates(
                    standard_candidates,
                    target_date=reference_target_date,
                )
                strategy_payload["standard_candidates"] = standard_candidates
                strategy_payload["candidate_strategy_counts"] = summarize_candidates_by_strategy(standard_candidates)
                strategy_payload["candidate_theme_coverage"] = summarize_candidate_theme_coverage(standard_candidates)
                long_count, board_count = _extract_strategy_counts(strategy_payload)
                add_step(
                    "auto_strategy_sandbox",
                    "ok" if strategy_payload.get("ok") else "warning",
                    f"自动策略沙盒已运行：长线 {long_count} 只，打板 {board_count} 只。",
                    {
                        "long_candidate_count": long_count,
                        "board_candidate_count": board_count,
                        "candidate_strategy_counts": strategy_payload["candidate_strategy_counts"],
                        "candidate_theme_coverage": strategy_payload["candidate_theme_coverage"],
                    },
                    input_source="generated_in_memory",
                )
                current_theme = validate_theme_map_for_current_candidates()
                theme_status["current_candidate_theme_status"] = current_theme
                add_step(
                    "current_candidate_theme_sync",
                    "ok" if current_theme.get("unknown_current_candidates", 0) == 0 else "warning",
                    current_theme.get("message") or "当前候选板块映射已检查。",
                    current_theme,
                    input_source="candidates_in_memory",
                )
                # 候选池 → 观察池：综合评分（技术面为主）选 top 写入本地观察池并复盘。
                try:
                    watchpool_payload = build_daily_watchpool_step(standard_candidates)
                    add_step(
                        "composite_watchpool",
                        "ok" if watchpool_payload.get("ok") else "warning",
                        "综合评分（技术面为主）已选 top 候选写入观察池并完成本地复盘；纯纸面观察，不成交。",
                        {
                            "candidate_count": watchpool_payload.get("candidate_count", 0),
                            "selected_count": watchpool_payload.get("selected_count", 0),
                            "added_count": watchpool_payload.get("added_count", 0),
                            "watchlist_count": watchpool_payload.get("watchlist_count", 0),
                            "added_symbols": watchpool_payload.get("added_symbols", []),
                        },
                        input_source="candidates_in_memory",
                    )
                except Exception as exc:  # noqa: BLE001
                    watchpool_payload = {"ok": False, "error": str(exc), "watchlist_count": 0}
                    add_step("composite_watchpool", "warning", "观察池综合评分接入失败，不影响后续本地复盘。", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                strategy_payload = {"ok": False, "summary": {}, "long_candidates": [], "board_candidates": []}
                add_step("auto_strategy_sandbox", "warning", "可选沙盒研究失败；持仓复盘继续。", error=str(exc))
        else:
            strategy_payload = {"status": "skipped", "reason": "not_requested"}
            watchpool_payload = {"status": "skipped", "reason": "not_requested"}
            for step_name in ("auto_strategy_sandbox", "current_candidate_theme_sync", "composite_watchpool"):
                add_step(step_name, "skipped", "旧沙盒研究未请求；正式纸面候选仍按目标日独立读取。", {"reason": "not_requested"}, input_source="not_run")

        # ---- Strategy Lab (v420) — optional research-only step ----
        # The formal, date-addressed batch is the only source allowed to feed
        # new paper candidates. Sandbox output remains research/watchpool data.
        formal_target_date = str(
            (freshness_gate or {}).get("target_complete_trade_date")
            or daily_price_payload.get("target_date")
            or local_daily_date
            or ""
        )[:10]
        try:
            pit_sector_payload = build_pit_sector_status(
                as_of=formal_target_date,
                history_dir=PROJECT_ROOT / "data" / "sector_history",
            )
            pit_sector_path = write_pit_sector_status(
                pit_sector_payload,
                output_path=RESULTS_DIR / "pit_sector_status_latest.json",
            )
            artifacts["pit_sector_status"] = _display_path(Path(pit_sector_path))
            add_step(
                "pit_sector_status",
                "ok" if pit_sector_payload.get("pit_eligible") is True else "warning",
                "已按本轮目标日派生 PIT 板块状态。"
                if pit_sector_payload.get("pit_eligible") is True
                else "PIT 板块状态证据不足；本轮 smart-stop 保持失败关闭。",
                {
                    "status": pit_sector_payload.get("status"),
                    "target_date": pit_sector_payload.get("target_date"),
                    "pit_eligible": pit_sector_payload.get("pit_eligible") is True,
                    "sector_count": pit_sector_payload.get("sector_count", 0),
                    "reason_codes": pit_sector_payload.get("reason_codes") or [],
                    "path": str(pit_sector_path),
                },
                input_source="forward_pit_sector_history",
            )
        except Exception as exc:  # noqa: BLE001 - risk evidence fails closed
            add_step(
                "pit_sector_status",
                "warning",
                "PIT 板块状态派生失败；本轮 smart-stop 保持失败关闭。",
                {"status": "blocked", "target_date": formal_target_date},
                error=str(exc),
                input_source="forward_pit_sector_history",
            )
        try:
            formal_loaded = _load_formal_candidate_batch_for_review(
                PROJECT_ROOT,
                target_date=formal_target_date,
            )
            formal_candidate_status = str(formal_loaded.get("status") or "blocked")
            formal_candidate_batch = (
                dict(formal_loaded.get("batch"))
                if isinstance(formal_loaded.get("batch"), dict)
                else None
            )
            formal_candidate_batch_path = str(formal_loaded.get("path") or "")
            add_step(
                "formal_candidate_batch",
                "ok" if formal_candidate_status == "passed" else "warning",
                "正式候选批次按目标日期读取并验证；纸面新增候选只使用该快照。"
                if formal_candidate_status == "passed"
                else "正式候选批次缺失或验证失败；本轮不接收新的纸面候选，已有到期计划仍可回放。",
                {
                    "status": formal_candidate_status,
                    "target_date": formal_target_date,
                    "path": formal_candidate_batch_path,
                    "candidate_count": (
                        formal_candidate_batch.get("candidate_count", 0)
                        if isinstance(formal_candidate_batch, dict)
                        else 0
                    ),
                    "reason_codes": formal_loaded.get("reason_codes") or [],
                    "source_mode": formal_loaded.get("source_mode") or "",
                },
                input_source=str(
                    formal_loaded.get("source_mode")
                    or "dated_observation_snapshot"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - formal intake fails closed
            formal_candidate_status = "blocked"
            formal_candidate_batch = None
            formal_candidate_batch_path = ""
            add_step(
                "formal_candidate_batch",
                "warning",
                "正式候选批次加载失败；不回退到 sandbox 候选。",
                {"status": formal_candidate_status},
                error=str(exc),
                input_source="dated_formal_candidate_batch",
            )

        strategy_lab_result: dict[str, Any] = {}
        if run_strategy_lab:
            try:
                from quantdesk.daily_research_pipeline import run_pipeline as _run_strategy_lab
                strategy_lab_result = _run_strategy_lab(PROJECT_ROOT, network=False)
                lab_status = strategy_lab_result.get("handoff", {}).get("status", "unknown")
                add_step(
                    "strategy_lab",
                    "ok" if "completed" in str(lab_status) else "warning",
                    f"策略实验室已完成（{lab_status}），结果写入 results/strategy_lab/ 和 results/daily_research/。",
                    {
                        "handoff_status": lab_status,
                        "preflight_status": strategy_lab_result.get("preflight", {}).get("status", ""),
                        "research_only_boundary_kept": strategy_lab_result.get("handoff", {}).get("research_only_boundary_kept", True),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                strategy_lab_result = {"ok": False, "error": str(exc)}
                add_step("strategy_lab", "warning", "可选策略实验室失败；持仓复盘继续。", error=str(exc))
        else:
            add_step(
                "strategy_lab",
                "skipped",
                "策略实验室为可选研究步骤，本次未开启；不影响 daily workflow 基础复盘。",
                {"enabled": False, "reason": "not_requested"},
            )

        portfolio_exists = AUTO_PAPER_PORTFOLIO_STATE_JSON.exists()
        add_step(
            "auto_paper_portfolio_state",
            "ok" if portfolio_exists else ("warning" if not allow_auto_init else "ok"),
            "自动虚拟组合存在。" if portfolio_exists else "自动虚拟组合不存在；默认不会自动建仓。",
            {"exists": portfolio_exists, "allow_auto_init": bool(allow_auto_init)},
        )

        if not portfolio_exists and not allow_auto_init:
            portfolio_payload = {
                "ok": True,
                "skipped": True,
                "message": "自动虚拟组合不存在，且 allow_auto_init=false；本次不自动建仓、不覆盖组合。",
                "summary": {
                    "position_count": 0,
                    "daily_pnl": 0,
                    "total_return_pct": 0,
                },
                "conditional_plan_summary": {"symbol_count": 0},
                "conditional_execution_summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0},
            }
            add_step("conditional_trade_plan", "skipped", "缺少自动虚拟组合，已跳过。")
            add_step("conditional_execution", "skipped", "缺少自动虚拟组合，已跳过。")
            add_step("portfolio_update", "skipped", portfolio_payload["message"])
        elif freshness_gate and not freshness_gate.get("can_review_yesterday_positions", True):
            portfolio_payload = _stale_portfolio_payload(freshness_gate)
            add_step(
                "conditional_trade_plan",
                "skipped",
                "滞后数据观察：缺少目标完整日线，暂不生成新的条件交易计划。",
                {"review_mode": freshness_gate.get("review_mode", "stale_observation")},
            )
            add_step(
                "conditional_execution",
                "skipped",
                "滞后数据观察：缺少目标完整日线；缺分钟线也不能验证盘中触发，本次不假装成交。",
                {"intraday_review_valid": False, "boundary_note": "完整日线控制收益复盘，分钟线控制盘中触发复盘。"},
            )
            add_step(
                "portfolio_update",
                "skipped",
                portfolio_payload["message"],
                {"portfolio_review_valid": False, "review_conclusion": "暂无有效复盘"},
            )
        else:
            try:
                if not portfolio_exists and allow_auto_init:
                    init_payload = run_auto_paper_portfolio(mode="init", strategy=strategy, initial_cash=_paper_initial_cash())
                    if not init_payload.get("ok"):
                        warnings.append(init_payload.get("message", "自动虚拟组合初始化未完成。"))
                due_trade_date = str(
                    review_target_date
                    or local_daily_date
                    or ""
                )[:10]
                due_loaded = (
                    load_archived_conditional_plan(
                        PROJECT_ROOT, trade_date=due_trade_date
                    )
                    if due_trade_date
                    else {
                        "status": "blocked",
                        "reason_codes": [
                            "conditional_plan_archive_trade_date_invalid"
                        ],
                    }
                )
                if due_loaded.get("status") == "passed" and isinstance(due_loaded.get("plan"), dict):
                    review_symbols.update(
                        normalize_symbol(row.get("symbol"))
                        for row in due_loaded["plan"].get("symbols", [])
                        if isinstance(row, dict) and normalize_symbol(row.get("symbol"))
                    )
                    if due_loaded.get("archive_path"):
                        input_artifacts["due_plan_archive"] = due_loaded["archive_path"]
                add_step(
                    "due_plan_archive",
                    "ok" if due_loaded.get("status") == "passed" else "warning",
                    (
                        "到期计划已从归档加载；本轮只读复核它，模拟是否运行另行记录。"
                        if due_loaded.get("status") == "passed"
                        else "归档中没有到期计划；本轮不模拟（不会假装成交）。"
                    ),
                    {
                        "trade_date": due_trade_date,
                        "status": due_loaded.get("status"),
                        "plan_id": due_loaded.get("plan_id") or "",
                        "archive_path": due_loaded.get("archive_path") or "",
                        "reason_codes": due_loaded.get("reason_codes") or [],
                    },
                )
                portfolio_payload = run_auto_paper_portfolio(
                    mode="update",
                    strategy=strategy,
                    initial_cash=_paper_initial_cash(),
                    enable_intraday_swing=enable_intraday,
                    allow_daily_approx=allow_daily_approx,
                    formal_candidate_batch=formal_candidate_batch,
                    formal_candidate_batch_path=formal_candidate_batch_path or None,
                    formal_candidate_status=formal_candidate_status,
                    publish_next_plan=False,
                    review_target_date=review_target_date,
                    due_plan_payload=(
                        dict(due_loaded["plan"])
                        if due_loaded.get("status") == "passed"
                        and isinstance(due_loaded.get("plan"), dict)
                        else None
                    ),
                )
                portfolio_ran = True
                execution_path = (portfolio_payload.get("conditional_execution") or {}).get("path")
                if execution_path and portfolio_payload.get("ok"):
                    artifacts["conditional_execution"] = execution_path
                due_enrichment = portfolio_payload.get("due_plan_enrichment")
                if isinstance(due_enrichment, dict):
                    enrichment_status = str(due_enrichment.get("status") or "blocked")
                    add_step(
                        "due_plan_candidate_enrichment",
                        "ok"
                        if enrichment_status in {"completed", "not_applicable"}
                        else "warning",
                        (
                            "Legacy due-plan candidate envelopes were enriched in memory; archive unchanged."
                            if enrichment_status == "completed"
                            else "Legacy due-plan candidate envelope enrichment was incomplete; fail-closed evidence remains visible."
                        ),
                        due_enrichment,
                        input_source="dated_formal_candidate_batch",
                    )
                plan_summary = portfolio_payload.get("conditional_plan_summary") or (portfolio_payload.get("conditional_plan") or {}).get("summary") or {}
                execution_summary = portfolio_payload.get("conditional_execution_summary") or (portfolio_payload.get("conditional_execution") or {}).get("summary") or {}
                add_step(
                    "conditional_trade_plan",
                    "ok" if _int(plan_summary.get("symbol_count")) > 0 or (portfolio_payload.get("conditional_plan") or {}).get("plan_id") else "warning",
                    "本轮使用已归档的到期条件计划；下一交易日计划由收盘流程发布。",
                    plan_summary,
                )
                add_step(
                    "conditional_execution",
                    "ok" if enable_intraday and portfolio_payload.get("conditional_execution") and not (portfolio_payload.get("conditional_execution") or {}).get("skipped") else "skipped",
                    "盘中触发模拟已运行；成交与缺证据结果见独立收据。"
                    if enable_intraday and portfolio_payload.get("conditional_execution") and not (portfolio_payload.get("conditional_execution") or {}).get("skipped")
                    else "本轮未执行盘中模拟；只读到期复核与账本应用分别记录。",
                    execution_summary,
                )
                add_step(
                    "portfolio_update",
                    "ok" if portfolio_payload.get("ok") else "warning",
                    portfolio_payload.get("message", "自动虚拟组合已更新。"),
                    portfolio_payload.get("summary", {}),
                )
            except Exception as exc:  # noqa: BLE001
                portfolio_payload = {
                    "ok": False,
                    "message": str(exc),
                    "summary": {"position_count": 0, "daily_pnl": 0, "total_return_pct": 0},
                    "conditional_plan_summary": {"symbol_count": 0},
                    "conditional_execution_summary": {"event_count": 0, "no_fill_count": 0, "missing_intraday_count": 0},
                }
                add_step("conditional_trade_plan", "failed", "条件交易计划或组合更新失败。", error=str(exc))
                add_step("conditional_execution", "skipped", "组合更新失败，未执行盘中模拟。")
                add_step("portfolio_update", "failed", "自动虚拟组合更新失败。", error=str(exc))

        # Expiry review is independent from simulation and portfolio valuation.
        # Review only this invocation's target plan; older gaps are scanned,
        # never silently backfilled or given applied execution IDs.
        try:
            ledger_state = PaperStateStore(AUTO_PAPER_PORTFOLIO_STATE_JSON, schema_version=API_SCHEMA_VERSION, max_backups=7).read({})
            plan_review_payload = plan_review_snapshot(
                PROJECT_ROOT, trade_date=review_target_date, checked_at=now_text(),
                applied_execution_ids=list(ledger_state.get("applied_execution_ids") or []),
                simulation_enabled=enable_intraday, write=True,
            )
        except Exception as exc:  # noqa: BLE001 - an evidence failure stays visible
            plan_review_payload = {
                "plan_review_summary": {"status": "blocked", "as_of": review_target_date,
                                        "reason_codes": [f"plan_review_failed:{type(exc).__name__}:{exc}"]},
                "plan_review_items": [],
            }
        portfolio_payload.update(plan_review_payload)
        review_summary = plan_review_payload["plan_review_summary"]
        add_step("replay_gap_scan", "ok" if review_summary.get("status") == "passed" else "warning",
                 "到期已复核、盘中已模拟、已应用到账本分别统计；缺证据与旧无绑定收据保留。",
                 {**review_summary, "items": plan_review_payload.get("plan_review_items", []), "recovery_authorized": False},
                 input_source="immutable_plan_review_receipts")

        try:
            if review_symbols:
                intraday_status_payload = intraday_gap_status(
                    target_trade_date=review_target_date,
                    source="symbols", symbols=sorted(review_symbols), write=False,
                    intraday_dir=PROJECT_ROOT / "data" / "intraday" / "1m",
                    plan_path=Path(due_loaded.get("archive_path") or (
                        RESULTS_DIR / "conditional_trade_plans_daily" / f"{review_target_date}.json"
                    )),
                )
                intraday_status_payload["scope"] = "paper_positions_and_due_plans"
            else:
                intraday_status_payload = {
                    "ok": True, "status": "not_applicable", "reason": "no_execution_symbols",
                    "target_trade_date": review_target_date, "source": "symbols",
                    "scope": "paper_positions_and_due_plans", "total_symbols": 0,
                    "available_count": 0, "missing_count": 0, "invalid_count": 0,
                    "stale_count": 0, "insufficient_count": 0,
                    "message": "本轮没有持仓或到期计划标的需要分钟证据。",
                }
            minute_ok = intraday_status_payload.get("ok") is True and not any(
                _int(intraday_status_payload.get(key))
                for key in ("missing_count", "invalid_count", "stale_count", "insufficient_count")
            ) and intraday_status_payload.get("source_manifest_formal_eligible") is not False
            add_step(
                "intraday_gap_check",
                "skipped" if not review_symbols else ("ok" if minute_ok else "warning"),
                intraday_status_payload.get("message") or "分钟线缺口状态已检查。",
                {
                    "target_trade_date": intraday_status_payload.get("target_trade_date", ""),
                    "total_symbols": intraday_status_payload.get("total_symbols", 0),
                    "available_count": intraday_status_payload.get("available_count", 0),
                    "missing_count": intraday_status_payload.get("missing_count", 0),
                    "invalid_count": intraday_status_payload.get("invalid_count", 0),
                    "stale_count": intraday_status_payload.get("stale_count", 0),
                    "insufficient_count": intraday_status_payload.get("insufficient_count", 0),
                    "reason": intraday_status_payload.get("reason", ""),
                    "missing_symbols": intraday_status_payload.get("missing_symbols", []),
                    "boundary_note": "完整日线控制收益复盘；分钟线控制盘中触发验证。",
                },
            )
        except Exception as exc:  # noqa: BLE001
            intraday_status_payload = {"ok": False, "message": str(exc), "available_count": 0, "missing_count": 0, "total_symbols": 0}
            add_step("intraday_gap_check", "warning", "分钟线缺口状态检查失败；缺少分钟线时仍不会假装成交。", error=str(exc))

        paper_consistency_payload = _run_daily_paper_consistency_check(
            target_date=review_target_date,
            input_dir=RESULTS_DIR,
            output_dir=RESULTS_DIR,
            report_dir=REPORTS_DIR,
            paper_payload=portfolio_payload if portfolio_ran else {},
            execution_payload=(portfolio_payload.get("conditional_execution") or {}) if portfolio_ran else {},
            intraday_payload=intraday_status_payload,
        )
        for key in ("json_path", "report_path"):
            if paper_consistency_payload.get(key):
                artifacts[f"paper_vs_backtest_consistency_{key}"] = paper_consistency_payload[key]
        if paper_consistency_payload.get("warnings"):
            warnings.extend(str(item) for item in (paper_consistency_payload.get("warnings") or [])[:5])
        add_step(
            "paper_vs_backtest_consistency",
            "ok" if paper_consistency_payload.get("ok") and paper_consistency_payload.get("status") == "passed" else "warning",
            (
                "paper-vs-backtest consistency gate passed."
                if paper_consistency_payload.get("status") == "passed"
                else "paper-vs-backtest consistency gate reported warnings; see latest consistency report."
            ),
            {
                "status": paper_consistency_payload.get("status", ""),
                "issue_count": paper_consistency_payload.get("issue_count", 0),
                "warning_count": paper_consistency_payload.get("warning_count", 0),
                "json_path": paper_consistency_payload.get("json_path", "results/paper_vs_backtest_consistency_latest.json"),
                "report_path": paper_consistency_payload.get("report_path", "reports/paper_vs_backtest_consistency_report_latest.md"),
            },
        )

        if with_guardrails:
            guardrail_summary = build_daily_guardrail_summary(
                target_date=str(daily_price_payload.get("target_date") or local_daily_date or ""),
                daily_price_payload=daily_price_payload,
                feature_payload=feature_payload,
                strategy_payload=strategy_payload,
                portfolio_payload=portfolio_payload,
                intraday_status_payload=intraday_status_payload,
                mode=guardrails_mode,
            )
            guardrail_summary["enabled"] = True
            if guardrail_summary.get("warnings"):
                warnings.extend(str(item) for item in (guardrail_summary.get("warnings") or [])[:8])
            add_step(
                "quick_guardrails",
                "warning" if int(guardrail_summary.get("warning_count") or 0) else "ok",
                "quick guardrails 已汇总；warning 只作为治理提示，不会直接让 daily failed。",
                {"guardrail_summary": guardrail_summary},
            )
        else:
            add_step(
                "quick_guardrails",
                "skipped",
                "用户传入 --with-guardrails false，已跳过 quick guardrails。",
                {"enabled": False, "mode": guardrails_mode, "reason": "not_requested"},
            )

        brain_payload: dict[str, Any] = {}
        if run_brain_shadow:
            try:
                from quantdesk.market_brain_daily import run_daily_brain_snapshots

                brain_payload = run_daily_brain_snapshots(project_root=RESULTS_DIR.parent)
                brain_snapshot = (brain_payload.get("market_brain") or {}).get("snapshot") or {}
                shadow_snapshot = (brain_payload.get("strategy_brain_shadow") or {}).get("snapshot") or {}
                brain_regime = str(brain_snapshot.get("formal_regime") or brain_snapshot.get("regime") or "UNKNOWN")
                brain_eligible = brain_snapshot.get("evidence_eligible") is True
                add_step(
                    "market_brain_shadow",
                    "ok" if brain_payload.get("ok") and brain_eligible else "warning",
                    f"Market Brain 研究快照：{brain_regime}（shadow，纸面未验证）。",
                    {
                        "regime": brain_regime,
                        "confidence": brain_snapshot.get("confidence"),
                        "evidence_eligible": brain_eligible,
                        "reason_codes": (brain_snapshot.get("reason_codes") or [])[:6],
                        "shadow_candidate_count": shadow_snapshot.get("candidate_count", 0),
                        "shadow_eligible_count": shadow_snapshot.get("eligible_candidate_count", 0),
                        "assembly_warnings": (brain_payload.get("assembly_warnings") or [])[:6],
                    },
                )
            except Exception as exc:  # noqa: BLE001 - optional research remains non-blocking
                brain_payload = {"ok": False, "error": str(exc)}
                add_step("market_brain_shadow", "warning", "影子研究失败。", error=str(exc))
        else:
            brain_payload = {"status": "skipped", "reason": "not_requested"}
            add_step(
                "market_brain_shadow", "skipped", "影子研究未请求。",
                {"enabled": False, "reason": "not_requested"},
            )

        plan_summary = portfolio_payload.get("conditional_plan_summary") or (portfolio_payload.get("conditional_plan") or {}).get("summary") or {}
        execution_summary = portfolio_payload.get("conditional_execution_summary") or (portfolio_payload.get("conditional_execution") or {}).get("summary") or {}
        dates = {
            "report_generated_at": generated_at,
            "local_complete_daily_date": local_daily_date,
            "daily_feature_date": feature_status.get("latest_date") or feature_payload.get("latest_date") or "",
            "conditional_plan_date": (portfolio_payload.get("conditional_plan") or {}).get("plan_date", ""),
            "conditional_trade_date": (portfolio_payload.get("conditional_plan") or {}).get("trade_date", ""),
            "execution_trade_date": (portfolio_payload.get("conditional_execution") or {}).get("trade_date", ""),
            "portfolio_valuation_date": _portfolio_valuation_date(portfolio_payload),
        }
        research_snapshot = load_research_dashboard_snapshot()
        if research_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (research_snapshot.get("warnings") or [])[:3])
        multi_book_snapshot = load_multi_strategy_account_book_snapshot()
        if multi_book_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (multi_book_snapshot.get("warnings") or [])[:3])
        intraday_live_snapshot = load_intraday_live_latest_snapshot()
        if intraday_live_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (intraday_live_snapshot.get("warnings") or [])[:3])
        risk_attribution_snapshot = load_risk_attribution_snapshot()
        if risk_attribution_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (risk_attribution_snapshot.get("warnings") or [])[:3])
        strategy_recommendation_snapshot = load_strategy_recommendation_snapshot()
        if strategy_recommendation_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (strategy_recommendation_snapshot.get("warnings") or [])[:3])
        risk_alert_snapshot = load_daily_risk_alert_snapshot()
        if risk_alert_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (risk_alert_snapshot.get("warnings") or [])[:3])
        research_snapshots = load_daily_research_snapshots()
        if research_snapshots.get("warnings"):
            warnings.extend(str(item) for item in (research_snapshots.get("warnings") or [])[:3])
        experiment_tracker_snapshot = load_experiment_tracker_snapshot()
        if experiment_tracker_snapshot.get("warnings"):
            warnings.extend(str(item) for item in (experiment_tracker_snapshot.get("warnings") or [])[:3])

        payload = {
            "ok": True,
            "run_id": run_id,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "generated_at": generated_at,
            "task_type": "daily_workflow",
            "target_date": review_target_date,
            **plan_review_payload,
            "scope": "paper_positions_and_due_plans",
            "network": bool(allow_theme_map_online_update),
            "full_scan": False,
            "user_triggered": True,
            "strategy": strategy,
            "allow_auto_init": bool(allow_auto_init),
            "enable_intraday": bool(enable_intraday),
            "allow_daily_approx": bool(allow_daily_approx),
            "allow_theme_map_online_update": bool(allow_theme_map_online_update),
            "allow_daily_price_online_update": bool(allow_daily_price_online_update),
            "with_guardrails": bool(with_guardrails),
            "guardrails_mode": guardrails_mode,
            "low_price_status": low_status,
            "low_price_report": {**low_report, "summary": low_summary},
            "run_legacy_research": bool(run_legacy_research),
            "formal_candidate_intake": {
                "status": formal_candidate_status, "path": formal_candidate_batch_path,
                "target_date": formal_target_date,
                "source_run_id": ((formal_candidate_batch or {}).get("provenance") or {}).get("source_run_id", ""),
                "candidate_count": (formal_candidate_batch or {}).get("candidate_count") if formal_candidate_status == "passed" else None,
                "candidates": list((formal_candidate_batch or {}).get("candidates") or []) if formal_candidate_status == "passed" else [],
            },
            "daily_price_status": daily_price_payload,
            "daily_price_update": daily_price_update,
            "daily_feature_status": feature_status,
            "data_freshness_gate": freshness_gate,
            "theme_map_status": theme_status,
            "theme_map_update": theme_update,
            "auto_strategy": strategy_payload,
            "composite_watchpool": watchpool_payload,
            "candidates_summary": {
                "candidate_strategy_counts": strategy_payload.get("candidate_strategy_counts") or {"long": long_count, "board": board_count},
                "candidate_theme_coverage": strategy_payload.get("candidate_theme_coverage") or {},
            },
            "auto_paper_portfolio": portfolio_payload,
            "intraday_status": intraday_status_payload,
            "paper_consistency_check": paper_consistency_payload,
            "conditional_plan_summary": plan_summary,
            "conditional_execution_summary": execution_summary,
            "guardrail_summary": guardrail_summary,
            "research_dashboard_snapshot": research_snapshot,
            "multi_strategy_account_book_snapshot": multi_book_snapshot,
            "intraday_live_snapshot": intraday_live_snapshot,
            "risk_attribution_snapshot": risk_attribution_snapshot,
            "strategy_recommendation_snapshot": strategy_recommendation_snapshot,
            "risk_alert_snapshot": risk_alert_snapshot,
            "research_snapshots": research_snapshots,
            "experiment_tracker_snapshot": experiment_tracker_snapshot,
            "market_brain_shadow": brain_payload,
            "strategy_lab": strategy_lab_result,
            "dates": dates,
            "steps": steps,
            "input_sources": {
                "auto_strategy": "generated_in_memory" if run_legacy_research else "not_run",
                "auto_strategy_sandbox": "generated_in_memory" if run_legacy_research else "not_run",
                "composite_watchpool": "candidates_in_memory" if run_legacy_research else "not_run",
                "auto_paper_portfolio": "dated_formal_candidate_batch_and_due_plan" if portfolio_ran else "not_run",
                "conditional_trade_plan": "dated_due_plan_archive" if portfolio_ran else "not_run",
                "conditional_execution": "dated_due_plan_in_memory" if portfolio_ran else "not_run",
                "theme_map_sync": "candidates_in_memory" if run_legacy_research else "not_run",
                "strategy_lab": "generated_in_memory" if strategy_lab_result else "not_run",
                "latest_json_role": "archive_and_fallback",
            },
            "warnings": _dedupe_warnings(warnings),
            "errors": errors,
            "artifacts": artifacts,
            "input_artifacts": input_artifacts,
            "disclaimer": DISCLAIMER,
        }
        payload["summary"] = _build_summary(payload)
        payload["workflow_status"] = _workflow_status(steps, errors)
        payload["status"] = _payload_status(payload["workflow_status"])
        payload["ok"] = payload["workflow_status"] != "failed"

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamped_report_path = REPORTS_DIR / f"daily_workflow_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        latest_report_path = REPORTS_DIR / "daily_quant_report_latest.md"
        payload["report_path"] = _display_path(latest_report_path)
        payload["timestamped_report_path"] = _display_path(timestamped_report_path)
        payload["latest_path"] = _display_path(DAILY_WORKFLOW_LATEST_JSON)
        payload["output_paths"] = {
            "latest_json": payload["latest_path"],
            "report": payload["report_path"],
            "timestamped_report": payload["timestamped_report_path"],
        }
        artifacts["daily_report"] = payload["report_path"]
        artifacts["daily_result"] = payload["latest_path"]
        artifacts["daily_workflow_report"] = payload["report_path"]
        artifacts["daily_workflow_report_timestamped"] = payload["timestamped_report_path"]
        payload["remediation_plan"] = build_remediation_plan(daily_payload=payload)
        payload["daily_console_summary"] = build_daily_console_summary(payload)
        add_step("final_report", "ok", f"统一报告已生成：{payload['report_path']}", {"report_path": payload["report_path"]})
        payload["steps"] = steps
        payload["warnings"] = _dedupe_warnings(warnings)
        warnings = payload["warnings"]
        payload["errors"] = errors
        payload["workflow_status"] = _workflow_status(steps, errors)
        payload["status"] = _payload_status(payload["workflow_status"])
        payload["ok"] = payload["workflow_status"] != "failed"
        report_text = build_daily_workflow_report(payload)
        timestamped_report_path.write_text(report_text, encoding="utf-8")
        latest_report_path.write_text(report_text, encoding="utf-8")
        atomic_write_json(DAILY_WORKFLOW_LATEST_JSON, payload)
        return payload
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        if not any(step.get("name") == "final_report" for step in steps):
            add_step("final_report", "failed", "统一报告生成失败。", error=str(exc))
        payload = {
            "ok": False,
            "run_id": run_id,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "generated_at": generated_at,
            "workflow_status": "failed",
            "status": "failed",
            "task_type": "daily_workflow",
            "scope": "paper_positions_and_due_plans",
            "network": bool(allow_theme_map_online_update),
            "full_scan": False,
            "user_triggered": True,
            "with_guardrails": bool(with_guardrails),
            "guardrails_mode": guardrails_mode,
            "guardrail_summary": guardrail_summary,
            "steps": steps,
            "summary": {},
            "warnings": _dedupe_warnings(warnings),
            "errors": errors,
            "artifacts": artifacts,
            "report_path": "reports/daily_quant_report_latest.md",
            "latest_path": "results/daily_workflow_latest.json",
            "message": f"今日一键复盘失败：{exc}",
            "disclaimer": DISCLAIMER,
        }
        payload["remediation_plan"] = build_remediation_plan(daily_payload=payload)
        payload["daily_console_summary"] = build_daily_console_summary(payload)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(DAILY_WORKFLOW_LATEST_JSON, payload)
        return payload


def daily_workflow_report_html() -> str:
    latest = daily_workflow_latest()
    report_path = None
    if latest.get("report_path"):
        candidate = PROJECT_ROOT / str(latest.get("report_path"))
        if candidate.exists():
            report_path = candidate
    report_path = report_path or newest_daily_workflow_report()
    if report_path and report_path.exists():
        body = html.escape(report_path.read_text(encoding="utf-8"))
        meta = html.escape(str(report_path))
    else:
        body = "尚未生成今日一键复盘报告。"
        meta = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>今日一键复盘</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; color: #1f2937; background: #f6f8fb; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 24px; background: #fff; min-height: 100vh; }}
    .toolbar {{ display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px solid #e5e7eb; padding-bottom: 16px; margin-bottom: 20px; }}
    button {{ border: 1px solid #2563eb; background: #2563eb; color: #fff; border-radius: 6px; padding: 9px 14px; cursor: pointer; }}
    .disclaimer {{ color: #92400e; background: #fff7ed; border: 1px solid #fed7aa; padding: 10px 12px; border-radius: 6px; margin: 12px 0; }}
    .meta {{ color: #667085; font-size: 13px; overflow-wrap: anywhere; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; font-size: 14px; }}
    @media print {{ button {{ display: none; }} body {{ background: #fff; }} main {{ padding: 0; }} }}
  </style>
</head>
<body>
  <main>
    <div class="toolbar"><div><h1>今日一键复盘</h1><div class="meta">{meta}</div></div><button type="button" onclick="window.print()">HTML 预览 / 浏览器另存为 PDF</button></div>
    <div class="disclaimer">{html.escape(DISCLAIMER)}</div>
    <pre>{body}</pre>
  </main>
</body>
</html>"""
