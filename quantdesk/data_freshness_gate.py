"""Data freshness gate for daily review workflows.

The gate is intentionally conservative: when the local complete daily bars are
behind the target complete trading day, QuantDesk may still show stale-data
observations, but it must not present them as a current-day review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from quantdesk.daily_feature_engine import daily_feature_status
from quantdesk.daily_price_providers import latest_date_for_symbol
from quantdesk.daily_price_update_engine import daily_price_status
from quantdesk.shared import normalize_symbol, read_json
from quantdesk.time_utils import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
STATE_DIR = PROJECT_ROOT / "state"
INTRADAY_1M_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"
LOW_PRICE_REPORT_JSON = RESULTS_DIR / "low_price_learning_report_latest.json"
AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = RESULTS_DIR / "auto_paper_portfolio_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"

WAITING_DATA_REASON = (
    "缺少目标完整日线，不能复盘昨日策略表现。"
)
STALE_REVIEW_MESSAGE = (
    "本次只能基于滞后数据观察，不能证明目标交易日策略表现。"
)


_now = now_text


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _less_than(left: str, right: str) -> bool:
    left = _date_text(left)
    right = _date_text(right)
    return bool(left and right and left < right)


def _normalize_symbol(value: Any) -> str:
    return normalize_symbol(value)


def _iter_positions(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    positions = payload.get("positions")
    if isinstance(positions, dict):
        yield from (row for row in positions.values() if isinstance(row, dict))
    elif isinstance(positions, list):
        yield from (row for row in positions if isinstance(row, dict))
    nested = payload.get("portfolio")
    if isinstance(nested, dict):
        yield from _iter_positions(nested)


def collect_current_review_symbols() -> list[str]:
    """Collect current candidates and auto-paper positions without scanning all A."""

    symbols: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        symbol = _normalize_symbol(value)
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)

    strategy = read_json(AUTO_STRATEGY_LATEST_JSON, {})
    for key in ("long_candidates", "board_candidates"):
        for row in strategy.get(key) or []:
            if isinstance(row, dict):
                add(row.get("symbol") or row.get("code"))

    for path in (AUTO_PAPER_PORTFOLIO_LATEST_JSON, AUTO_PAPER_PORTFOLIO_STATE_JSON):
        payload = read_json(path, {})
        for row in _iter_positions(payload):
            add(row.get("symbol") or row.get("code"))
    return symbols


def _has_intraday_for_date(symbol: str, trade_date: str) -> bool:
    path = INTRADAY_1M_DIR / f"{symbol}.csv"
    if not path.exists() or not trade_date:
        return False
    try:
        # Read the file TAIL, not head: intraday 1m CSVs are ~12KB/day, so for
        # multi-day files the latest trade_date lives at the end. Reading the
        # first 4096 bytes would miss it and falsely report the day as missing
        # (matches the seek-to-tail pattern in data_update_orchestrator).
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 4096))
            tail = handle.read().decode("utf-8-sig", errors="ignore")
    except Exception:
        return False
    return trade_date in tail


def _low_price_report_date() -> str:
    payload = read_json(LOW_PRICE_REPORT_JSON, {})
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return _date_text(summary.get("latest_complete_daily_date") or payload.get("latest_complete_daily_date"))


def evaluate_data_freshness(
    *,
    daily_price_payload: dict[str, Any] | None = None,
    daily_feature_payload: dict[str, Any] | None = None,
    target_date: str | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Return a conservative gate decision for current-day review validity."""

    daily_price = daily_price_payload if isinstance(daily_price_payload, dict) else daily_price_status(only_low_price=True, target_date=target_date)
    feature = daily_feature_payload if isinstance(daily_feature_payload, dict) else daily_feature_status()
    target = _date_text(target_date or daily_price.get("target_date"))
    max_local_date = _date_text(daily_price.get("max_local_date"))
    pool_latest = _date_text(daily_price.get("latest_local_date") or max_local_date)
    feature_date = _date_text(feature.get("latest_date"))
    low_report_date = _low_price_report_date()
    review_symbols = symbols if symbols is not None else collect_current_review_symbols()
    normalized_review_symbols = [
        normalized
        for normalized in (_normalize_symbol(item) for item in review_symbols)
        if normalized
    ]
    latest_by_symbol = daily_price.get("latest_by_symbol") if isinstance(daily_price.get("latest_by_symbol"), dict) else {}
    scope_mapping_available = bool(normalized_review_symbols and latest_by_symbol)
    if scope_mapping_available:
        review_latest_by_symbol = {
            symbol: _date_text(
                latest_by_symbol.get(symbol)
                if symbol in latest_by_symbol
                else latest_date_for_symbol(symbol)
            )
            for symbol in normalized_review_symbols
        }
        review_present_dates = [value for value in review_latest_by_symbol.values() if value]
        complete_scope_latest = min(review_present_dates) if review_present_dates else ""
        scope_missing_symbols = [symbol for symbol, value in review_latest_by_symbol.items() if not value]
        scope_stale_symbols = [
            symbol
            for symbol, value in review_latest_by_symbol.items()
            if value and _less_than(value, target)
        ]
        scope_missing_symbol_count = len(scope_missing_symbols)
        scope_stale_symbol_count = len(scope_stale_symbols) + scope_missing_symbol_count
    else:
        review_latest_by_symbol = {}
        complete_scope_latest = pool_latest
        scope_missing_symbols = []
        scope_stale_symbols = []
        scope_missing_symbol_count = int(daily_price.get("missing_count") or 0)
        scope_stale_symbol_count = int(daily_price.get("stale_count") or 0)
    local_daily_latest = complete_scope_latest
    explicit_symbol_count = len(symbols or []) if symbols is not None else 0
    total_symbols = int(daily_price.get("total_symbols") or daily_price.get("symbol_count") or explicit_symbol_count or 0)
    pool_stale_symbol_count = int(daily_price.get("stale_count") or 0)
    pool_missing_symbol_count = int(daily_price.get("missing_count") or 0)
    stale_symbol_count = scope_stale_symbol_count
    missing_symbol_count = scope_missing_symbol_count
    daily_pool_partial = bool(pool_stale_symbol_count or pool_missing_symbol_count)
    no_universe = total_symbols == 0
    no_data = bool(total_symbols) and not (local_daily_latest or max_local_date)
    is_daily_stale = bool(
        no_universe
        or no_data
        or stale_symbol_count
        or missing_symbol_count
        or _less_than(local_daily_latest, target)
        or (not scope_mapping_available and daily_price.get("is_stale"))
    )
    is_feature_stale = bool(not no_universe and not no_data and _less_than(feature_date, target))
    can_review_daily = not is_daily_stale and not missing_symbol_count and bool(local_daily_latest)
    intraday_missing = [
        symbol for symbol in review_symbols
        if not _has_intraday_for_date(symbol, target)
    ]
    can_verify_intraday = bool(review_symbols) and not intraday_missing and can_review_daily
    if is_daily_stale:
        reason = (
            f"本地完整日线仍停留在 {local_daily_latest or '-'}，目标完整交易日为 {target or '-'}，"
            "因此不能计算昨日持仓收益，也不能验证昨日策略效果。"
        )
    elif is_feature_stale:
        reason = (
            f"日线已可用于估值，但特征表仍基于 {feature_date or '-'}，目标完整交易日为 {target or '-'}；"
            "候选只能作为滞后数据观察。"
        )
    elif intraday_missing:
        reason = "完整日线可用于收益复盘，但缺少分钟线，无法验证盘中条件触发。"
    else:
        reason = "当前复核范围的完整日线和特征日期满足复盘门槛。"

    if no_universe:
        data_freshness_status = "no_universe"
        warning_kind = "no_universe"
        reason = "当前没有可分析股票，无法判断本地日线新鲜度；请先运行 data-bootstrap 或指定 --symbols。"
    elif no_data:
        data_freshness_status = "no_data"
        warning_kind = "data_missing"
        reason = "当前股票池没有可用本地日线，不能视为 fresh，也不能进入复盘通过状态。"
    elif is_daily_stale or is_feature_stale:
        data_freshness_status = "stale"
        warning_kind = "data_stale"
    else:
        data_freshness_status = "fresh"
        warning_kind = "intraday_missing" if intraday_missing else ""
    pool_partial_warning = (
        f"低价研究池仍有 {pool_stale_symbol_count} 只日期滞后、{pool_missing_symbol_count} 只缺失；"
        "它们不会进入当前日期候选，当前复核范围单独判定。"
        if daily_pool_partial and not is_daily_stale
        else ""
    )
    message = reason if data_freshness_status in {"no_universe", "no_data"} else (STALE_REVIEW_MESSAGE if (is_daily_stale or is_feature_stale) else reason)

    return {
        "ok": True,
        "report_generated_at": _now(),
        "target_complete_trade_date": target,
        "status": "warning" if data_freshness_status in {"no_universe", "no_data", "stale"} else "passed",
        "data_freshness_status": data_freshness_status,
        "warning_kind": warning_kind,
        "total_symbols": total_symbols,
        "local_daily_latest_date": local_daily_latest,
        "latest_complete_daily_date_for_scope": complete_scope_latest,
        "review_scope_symbol_count": len(normalized_review_symbols),
        "review_scope_latest_by_symbol": review_latest_by_symbol,
        "review_scope_stale_symbol_count": scope_stale_symbol_count,
        "review_scope_missing_symbol_count": scope_missing_symbol_count,
        "review_scope_stale_symbols": scope_stale_symbols[:50],
        "review_scope_missing_symbols": scope_missing_symbols[:50],
        "daily_pool_latest_date": pool_latest,
        "daily_pool_stale_symbol_count": pool_stale_symbol_count,
        "daily_pool_missing_symbol_count": pool_missing_symbol_count,
        "daily_pool_partial": daily_pool_partial,
        "daily_feature_date": feature_date,
        "low_price_report_latest_date": low_report_date,
        "is_daily_stale": bool(is_daily_stale),
        "is_feature_stale": bool(is_feature_stale),
        "stale_symbol_count": stale_symbol_count,
        "missing_symbol_count": missing_symbol_count,
        "can_run_strategy_screen": True,
        "can_run_current_day_review": bool(can_review_daily),
        "can_update_portfolio_valuation": bool(can_review_daily),
        "can_review_yesterday_positions": bool(can_review_daily),
        "can_verify_intraday_triggers": bool(can_verify_intraday),
        "intraday_missing_symbol_count": len(intraday_missing),
        "intraday_missing_symbols": intraday_missing[:50],
        "review_mode": "stale_observation" if (is_daily_stale or is_feature_stale) else ("daily_review_only" if intraday_missing else "current_review"),
        "portfolio_review_valid": bool(can_review_daily),
        "intraday_review_valid": bool(can_verify_intraday),
        "stale_mode": bool(is_daily_stale or is_feature_stale),
        "message": message,
        "reason": reason,
        "warnings": [item for item in (reason if warning_kind else "", pool_partial_warning) if item],
        "suggested_next_action": (
            "python scripts/qd.py data-bootstrap --preset low_price_demo --network false"
            if data_freshness_status == "no_universe"
            else "python scripts/qd.py data-health --network false"
        ),
        "boundary_note": "完整日线控制收益复盘，分钟线控制盘中触发复盘。",
        "disclaimer": "仅用于本地数据新鲜度检查；不接券商、不真实下单、不构成投资建议。",
    }


def waiting_decision_review(previous_actions: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for action in previous_actions:
        symbol = _normalize_symbol(action.get("symbol") or action.get("code"))
        if not symbol:
            continue
        items.append({
            "symbol": symbol,
            "name": action.get("name") or symbol,
            "strategy_type": action.get("strategy_type") or "",
            "previous_action": action.get("action") or "",
            "previous_price": action.get("price") or action.get("latest_price") or action.get("entry_price"),
            "today_price": None,
            "today_date": "",
            "today_return_pct": None,
            "review_result": "等待数据",
            "quality_category": "waiting_data",
            "reason": "暂无有效复盘：本地完整日线尚未更新到目标完整交易日，无法判断昨日买入/持有/卖出策略是否有效。",
        })
    return {
        "ok": False,
        "review_valid": False,
        "review_date": gate.get("target_complete_trade_date") or "",
        "items": items,
        "buy_correct_count": 0,
        "buy_failed_count": 0,
        "hold_correct_count": 0,
        "hold_failed_count": 0,
        "sell_correct_count": 0,
        "sell_failed_count": 0,
        "watch_exit_correct_count": 0,
        "watch_exit_failed_count": 0,
        "message": "暂无有效复盘：本地完整日线尚未更新到目标完整交易日。",
        "reason": gate.get("reason") or WAITING_DATA_REASON,
        "disclaimer": "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。",
    }


def waiting_yesterday_holding_review(previous_positions: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for prev in previous_positions:
        symbol = _normalize_symbol(prev.get("symbol") or prev.get("code"))
        rows.append({
            "symbol": symbol,
            "name": prev.get("name") or symbol,
            "strategy_type": prev.get("strategy_type") or "",
            "yesterday_quantity": prev.get("quantity") or 0,
            "yesterday_price": prev.get("latest_price") or prev.get("entry_price"),
            "today_price": None,
            "today_return_pct": None,
            "floating_pnl": None,
            "review_conclusion": "等待数据",
            "reason": "缺少目标完整日线，不能复盘昨日策略表现。",
        })
    return {
        "ok": False,
        "review_valid": False,
        "yesterday_position_count": len(previous_positions),
        "today_still_holding": len(previous_positions),
        "today_new": 0,
        "today_sold": 0,
        "today_blocked": 0,
        "today_watch_exit": 0,
        "yesterday_position_unrealized_pnl": None,
        "buy_correct_count": 0,
        "buy_failed_count": 0,
        "hold_correct_count": 0,
        "hold_failed_count": 0,
        "items": rows,
        "message": "暂无有效复盘：本地完整日线尚未更新到目标完整交易日。",
        "reason": gate.get("reason") or WAITING_DATA_REASON,
        "boundary_note": "完整日线控制收益复盘，分钟线控制盘中触发复盘。",
        "disclaimer": "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。",
    }


def dumps_gate(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
