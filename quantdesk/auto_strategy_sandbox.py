"""Automatic local strategy sandbox for the low-price learning universe.

The sandbox is a local paper-trading workflow. It reads the low-price
learning universe, local daily bars, and the low-price cache report; then it
builds long / board simulation candidates and local paper orders.

It never connects to brokers, never places real orders, and does not
constitute investment advice.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.board_rules import classify_board
from quantdesk.candidate_contract import (
    attach_freshness_to_candidates,
    attach_theme_map_to_candidates,
    candidate_rows_from_strategy_payload,
    summarize_candidate_theme_coverage,
    summarize_candidates_by_strategy,
)
from quantdesk.corporate_action_repair import repair_corporate_actions
from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_feature_engine import apply_daily_features_to_item, daily_feature_status, load_daily_feature_map
from quantdesk.paper.market_rules import (
    lot_size_for_symbol,
    metadata_is_st,
    round_notional_down_to_lot_quantity,
)
from quantdesk.paper.trade_costs import buy_cash_outflow
from quantdesk.schema_alias import normalize_strategy_item_fields
from quantdesk.shared import normalize_symbol, now_text, truthy
from quantdesk.strategy_signals import board_signal_from_row, is_st_or_delisting, long_signal_from_row
from quantdesk.theme_sector_engine import (
    THEME_SECTOR_SUMMARY_JSON,
    apply_theme_to_item,
    load_theme_map,
    save_theme_sector_summary,
    summarize_theme_sector,
)
from quantdesk.utils import atomic_write_json, read_json_safe, safe_float
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
LOW_PRICE_REPORT_JSON = PROJECT_ROOT / "results" / "low_price_learning_report_latest.json"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
STATE_DIR = PROJECT_ROOT / "state"

AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_STRATEGY_UNIVERSE_JSON = RESULTS_DIR / "auto_strategy_universe_latest.json"
AUTO_LONG_CANDIDATES_JSON = RESULTS_DIR / "auto_long_candidates_latest.json"
AUTO_BOARD_CANDIDATES_JSON = RESULTS_DIR / "auto_board_candidates_latest.json"
AUTO_STRATEGY_REVIEW_JSON = RESULTS_DIR / "auto_strategy_review_latest.json"
THEME_SECTOR_SUMMARY_LATEST_JSON = THEME_SECTOR_SUMMARY_JSON
STRATEGY_DECISION_LOG_JSON = RESULTS_DIR / "strategy_decision_log_latest.json"
STRATEGY_DECISION_REVIEW_JSON = RESULTS_DIR / "strategy_decision_review_latest.json"
AUTO_PENDING_ORDERS_JSON = STATE_DIR / "auto_strategy_pending_orders.json"
PAPER_TRADING_LATEST_JSON = RESULTS_DIR / "paper_trading_latest.json"

DISCLAIMER = "仅用于策略学习与模拟验证，不构成投资建议。"
BROKER_BOUNDARY = "不接券商，不真实下单。"

# Paper-only / no-broker boundary applied to every pending and blocked order so
# no payload can be mistaken for a real order or a broker connection.
PAPER_ORDER_BOUNDARY = {
    "is_real_order": False,
    "research_only": True,
    "paper_only": True,
    "no_broker": True,
}


def _paper_order_boundary() -> dict[str, bool]:
    return dict(PAPER_ORDER_BOUNDARY)
SINGLE_DAY_REVIEW_NOTE = "单日验证只用于观察，不能证明策略长期有效。需要累计更多样本后再评估规则稳定性。"


def _read_universe(path: Path = UNIVERSE_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    frame = read_csv_frame(path, dtype=str)
    rows = []
    for _, row in frame.iterrows():
        item = {str(k or "").strip(): v for k, v in dict(row).items()}
        item = normalize_strategy_item_fields(item)
        item["symbol"] = normalize_symbol(item.get("symbol") or item.get("code"))
        item["name"] = str(item.get("name") or item["symbol"])
        item["included"] = truthy(item.get("included"))
        item["csv_read_warnings"] = [str(warning) for warning in frame.attrs.get("warnings", []) or []]
        rows.append(item)
    return rows


def _read_low_price_report(path: Path = LOW_PRICE_REPORT_JSON) -> dict[str, Any]:
    data = read_json_safe(path, {})
    return data if isinstance(data, dict) else {}


def _report_items_by_symbol(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = report.get("items") or report.get("rows") or []
    if not isinstance(rows, list):
        return {}
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if symbol:
            result[symbol] = normalize_strategy_item_fields(row)
    return result


def _daily_path(symbol: str, daily_dir: Path = DAILY_DIR) -> Path:
    return daily_dir / f"{normalize_symbol(symbol)}.csv"


def _read_daily(symbol: str, daily_dir: Path = DAILY_DIR) -> pd.DataFrame:
    path = _daily_path(symbol, daily_dir)
    if not path.exists():
        return pd.DataFrame()
    df = read_csv_frame(path)
    csv_read_warnings = [str(warning) for warning in df.attrs.get("warnings", []) or []]
    rename = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns and v not in df.columns})
    if "date" not in df.columns:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings
        return empty
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    # Repair unadjusted split/dividend cliffs before deriving anything backward
    # looking. On raw bars a 2-for-1 split reads as a -50% crash, which leaves
    # ma20 (and therefore close_vs_ma20_pct) wrong for 20 sessions and makes
    # _one_word_limit_down misfire on the cliff. The repair is anchored to the
    # newest bar, so the latest quoted price -- what candidates are scored and
    # displayed on -- is unchanged; only history is restated. raw_close is kept
    # for anything that needs the price as actually quoted.
    df, ca_info = repair_corporate_actions(df)
    if ca_info.get("unverifiable"):
        csv_read_warnings.append(f"unverifiable_corporate_action:{ca_info.get('reason') or ''}")
    elif ca_info.get("ca_bars"):
        csv_read_warnings.append(f"corporate_actions_repaired:{ca_info['ca_bars']}")
    if "close" in df.columns:
        df["ma20"] = df["close"].rolling(20, min_periods=20).mean()
        df["prev_close"] = df["close"].shift(1)
        df["close_vs_ma20_pct"] = (df["close"] / df["ma20"] - 1.0) * 100.0
    if "amount" in df.columns:
        df["amount_ma20"] = df["amount"].rolling(20, min_periods=20).mean()
    if "volume" in df.columns:
        df["volume_ma20"] = df["volume"].rolling(20, min_periods=20).mean()
    df.attrs["warnings"] = csv_read_warnings
    return df


def _latest_complete_date(report: dict[str, Any], items: list[dict[str, Any]]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    for key in ("expected_trade_date", "latest_complete_daily_date"):
        value = str(summary.get(key) or report.get(key) or "").strip()
        if value:
            return value[:10]
    dates = [str(item.get("latest_date") or "")[:10] for item in items if item.get("latest_date")]
    return max(dates) if dates else ""


def _skip(reason: str, symbol: str, name: str) -> dict[str, Any]:
    return {"symbol": symbol, "name": name, "simulation_ready": False, "skip_reason": reason}


def _price_by_symbol(universe_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("symbol")): item for item in universe_payload.get("items") or [] if item.get("symbol")}


def _load_existing_positions(path: Path = PAPER_TRADING_LATEST_JSON) -> list[dict[str, Any]]:
    data = read_json_safe(path, {})
    if not isinstance(data, dict):
        return []
    positions = data.get("positions")
    return positions if isinstance(positions, list) else []


def _pct_change(base: Any, current: Any) -> float | None:
    start = safe_float(base, None)
    end = safe_float(current, None)
    if start is None or end is None or start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _one_word_limit_down(
    item: dict[str, Any],
    *,
    symbol: Any = None,
    is_st: bool = False,
) -> bool:
    """Detect a one-word (locked) limit-down bar using board-aware thresholds.

    The limit-down percentage differs by board (main/ChiNext/STAR/BSE/ST), so
    the threshold comes from ``classify_board`` instead of a hard-coded main
    board -10%. A small tolerance keeps a bar sitting essentially at the limit
    from being treated as a reliable paper sell.
    """

    open_price = safe_float(item.get("open"), None)
    high = safe_float(item.get("high"), None)
    low = safe_float(item.get("low"), None)
    close = safe_float(item.get("close"), None)
    prev_close = safe_float(item.get("prev_close"), None)
    if None in (open_price, high, low, close, prev_close) or not prev_close:
        return False
    pct = (close / prev_close - 1.0) * 100.0
    try:
        rules = classify_board(str(symbol or "").strip(), is_st=is_st)
        limit_down_pct = float(rules.limit_down_pct)
    except Exception:
        limit_down_pct = -10.0
    # 0.2% (20bps) proximity tolerance mirrors the board-rule limit tolerance.
    at_limit_down = pct <= limit_down_pct + 0.2
    return abs(open_price - high) < 1e-6 and abs(high - low) < 1e-6 and at_limit_down


def select_simulation_universe(
    universe_path: Path | None = None,
    daily_dir: Path | None = None,
    report_json: Path | None = None,
    *,
    min_rows: int = 60,
    expected_latest_date: str | None = None,
) -> dict[str, Any]:
    """Select included low-price symbols that are eligible for paper simulation."""

    universe_path = universe_path or UNIVERSE_PATH
    daily_dir = daily_dir or DAILY_DIR
    report = _read_low_price_report(report_json or LOW_PRICE_REPORT_JSON)
    report_items = _report_items_by_symbol(report)
    included_rows = [row for row in _read_universe(universe_path) if row.get("included")]
    expected = expected_latest_date or _latest_complete_date(report, list(report_items.values()))
    theme_map = load_theme_map()
    feature_map = load_daily_feature_map()
    ready: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in included_rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        name = str(row.get("name") or symbol)
        report_item = normalize_strategy_item_fields(report_items.get(symbol, {}))
        if is_st_or_delisting(name):
            skipped.append(_skip("st_or_delisting", symbol, name))
            continue
        daily_path = _daily_path(symbol, daily_dir)
        if not daily_path.exists():
            skipped.append(_skip("missing_daily_cache", symbol, name))
            continue
        daily = _read_daily(symbol, daily_dir)
        csv_read_warnings = [str(warning) for warning in daily.attrs.get("warnings", []) or []]
        row_count = int(len(daily))
        if row_count < min_rows:
            skipped.append(_skip("insufficient_history", symbol, name))
            continue
        latest = daily.iloc[-1].to_dict()
        latest_date = pd.Timestamp(latest.get("date")).strftime("%Y-%m-%d")
        if expected and latest_date < expected:
            skipped.append(_skip("stale_daily_cache", symbol, name))
            continue
        confidence = str(report_item.get("data_confidence_status") or report_item.get("data_status") or "ok").lower()
        if confidence == "blocked":
            skipped.append(_skip("data_confidence_blocked", symbol, name))
            continue
        liquidity = str(report_item.get("liquidity_status") or latest.get("liquidity_status") or "unknown")
        amount_ma20 = safe_float(report_item.get("amount_ma20"), None)
        if amount_ma20 is None:
            amount_ma20 = safe_float(latest.get("amount_ma20"), None)
        if liquidity == "unknown" and amount_ma20 is not None:
            liquidity = "ok" if amount_ma20 >= 30_000_000 else "low" if amount_ma20 >= 10_000_000 else "very_low"
        if liquidity == "very_low":
            skipped.append(_skip("low_liquidity", symbol, name))
            continue
        close = safe_float(latest.get("close"), None)
        if close is None or close <= 0:
            skipped.append(_skip("invalid_close", symbol, name))
            continue
        if any(safe_float(latest.get(col), None) is None for col in ("open", "high", "low", "close")):
            skipped.append(_skip("missing_ohlc", symbol, name))
            continue
        ma20 = safe_float(report_item.get("ma20"), None)
        if ma20 is None:
            ma20 = safe_float(latest.get("ma20"), None)
        close_vs_ma20 = safe_float(report_item.get("close_vs_ma20_pct"), None)
        if close_vs_ma20 is None and ma20:
            close_vs_ma20 = (close / ma20 - 1.0) * 100.0
        base_item = {
                "symbol": symbol,
                "name": name,
                "simulation_ready": True,
                "latest_date": latest_date,
                "row_count": row_count,
                "close": close,
                "open": safe_float(latest.get("open"), None),
                "high": safe_float(latest.get("high"), None),
                "low": safe_float(latest.get("low"), None),
                "prev_close": safe_float(latest.get("prev_close"), None),
                "ma20": ma20,
                "close_vs_ma20_pct": close_vs_ma20,
                "amount_ma20": amount_ma20,
                "volume_ma20": safe_float(report_item.get("volume_ma20"), safe_float(latest.get("volume_ma20"), None)),
                "liquidity_status": liquidity,
                "data_confidence_status": confidence,
                "data_confidence_score": safe_float(report_item.get("data_confidence_score"), 80.0),
                "csv_read_warnings": csv_read_warnings + [str(warning) for warning in row.get("csv_read_warnings", []) or []],
                "source": "low_price_learning_universe",
            }
        base_item = apply_daily_features_to_item(base_item, feature_map.get(symbol))
        ready.append(apply_theme_to_item(base_item, theme_map))

    reasons = Counter(str(item.get("skip_reason") or "unknown") for item in skipped)
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "expected_latest_date": expected,
        "total_low_price": len(included_rows),
        "simulation_ready_count": len(ready),
        "skipped_count": len(skipped),
        "skip_reasons": dict(reasons),
        "items": ready,
        "skipped": skipped,
        "csv_read_warnings": sorted(
            {
                str(warning)
                for item in ready
                for warning in (item.get("csv_read_warnings") or [])
                if warning
            }
        ),
        "disclaimer": DISCLAIMER,
    }
    return payload


def _theme_diversified_candidates(
    candidates: list[dict[str, Any]],
    *,
    score_key: str,
    limit: int,
    per_theme_limit: int,
) -> list[dict[str, Any]]:
    """Pick candidates with a light local theme diversification rule."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        theme = str(row.get("primary_theme") or "未知题材")
        grouped.setdefault(theme, []).append(row)
    selected: list[dict[str, Any]] = []
    for rows in grouped.values():
        rows.sort(key=lambda item: safe_float(item.get(score_key), 0.0) or 0.0, reverse=True)
        selected.extend(rows[: max(1, int(per_theme_limit or 1))])
    selected.sort(key=lambda item: safe_float(item.get(score_key), 0.0) or 0.0, reverse=True)
    selected = selected[: max(0, int(limit or 0))]
    for idx, row in enumerate(selected, 1):
        row["rank"] = idx
    return selected


def _candidate_feature_fields(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "daily_feature_available",
        "daily_feature_latest_date",
        "return_5_pct",
        "return_20_pct",
        "return_60_pct",
        "relative_index_return_20_pct",
        "volatility_20_pct",
        "turnover",
        "turnover_ma20",
        "amount_ma20",
        "is_limit_up_like",
        "is_new_low_180",
        "consecutive_limit_up",
        "liquidity_status",
        "data_health_status",
    )
    return {key: item.get(key) for key in keys if key in item}


def select_long_candidates(universe_payload: dict[str, Any], *, limit: int = 20, per_theme_limit: int = 3) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for item in universe_payload.get("items") or []:
        signal = long_signal_from_row(item, symbol=str(item.get("symbol") or ""), name=str(item.get("name") or ""))
        if not signal:
            continue
        close = safe_float(signal.get("close"), None)
        ma20 = safe_float(signal.get("ma20"), None)
        close_vs = safe_float(signal.get("close_vs_ma20_pct"), None)
        amount_ma20 = safe_float(item.get("amount_ma20"), 0.0) or 0.0
        if close is None or ma20 is None or close_vs is None:
            continue
        liquidity_bonus = 25 if item.get("liquidity_status") == "ok" else 12
        confidence = safe_float(item.get("data_confidence_score"), 80.0) or 80.0
        distance_score = max(0.0, 25.0 - abs(close_vs - 6.0))
        history_bonus = min(20.0, safe_float(item.get("row_count"), 0.0) / 20.0)
        score = round(confidence * 0.35 + liquidity_bonus + distance_score + history_bonus + min(amount_ma20 / 50_000_000, 10), 2)
        candidates.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "strategy_type": "long",
                "long_score": score,
                "reason": signal.get("reason") or "收盘价位于 MA20 上方且偏离未超过 20%，用于长线纸面模拟观察。",
                "risk_notes": ["仅用于策略学习与模拟验证。"],
                "signal_type": "long_virtual_entry_plan",
                "planned_action": "生成本地纸面模拟计划",
                "sector": item.get("sector") or "未知板块",
                "industry": item.get("industry") or "未知行业",
                "primary_theme": item.get("primary_theme") or "未知题材",
                "theme_tags": item.get("theme_tags") or "",
                "theme_confidence": item.get("theme_confidence") or "unknown",
                **_candidate_feature_fields(item),
                "source_item": item,
            }
        )
    selected = _theme_diversified_candidates(candidates, score_key="long_score", limit=limit, per_theme_limit=per_theme_limit)
    return {"ok": True, "strategy_type": "long", "items": selected, "disclaimer": DISCLAIMER}


def select_board_candidates(universe_payload: dict[str, Any], *, limit: int = 10, per_theme_limit: int = 2) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for item in universe_payload.get("items") or []:
        signal = board_signal_from_row(item, symbol=str(item.get("symbol") or ""), name=str(item.get("name") or ""))
        if not signal:
            continue
        close = safe_float(signal.get("close"), None)
        prev_close = safe_float(signal.get("prev_close"), None)
        amount_ma20 = safe_float(item.get("amount_ma20"), 0.0) or 0.0
        if close is None or prev_close is None or prev_close <= 0:
            continue
        pct = safe_float(signal.get("pct_change"), None)
        if pct is None:
            continue
        limit_up_like = bool(signal.get("limit_up_like"))
        volume_boost = max(1.0, amount_ma20 / 30_000_000) if amount_ma20 else 1.0
        score = round(min(100.0, pct * 500 + min(volume_boost * 12, 35)), 2)
        candidates.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "strategy_type": "board",
                "board_score": score,
                "limit_up_like": limit_up_like,
                "volume_boost": round(volume_boost, 2),
                "reason": signal.get("reason") or "日线接近涨停或涨停，仅作为打板近似纸面模拟样本。",
                "risk_notes": ["打板模拟基于日线近似，无法判断真实盘中封单、排队成交和炸板细节。"],
                "signal_type": "board_virtual_entry_plan",
                "planned_action": "生成本地纸面模拟计划",
                "sector": item.get("sector") or "未知板块",
                "industry": item.get("industry") or "未知行业",
                "primary_theme": item.get("primary_theme") or "未知题材",
                "theme_tags": item.get("theme_tags") or "",
                "theme_confidence": item.get("theme_confidence") or "unknown",
                **_candidate_feature_fields(item),
                "source_item": item,
            }
        )
    selected = _theme_diversified_candidates(candidates, score_key="board_score", limit=limit, per_theme_limit=per_theme_limit)
    return {"ok": True, "strategy_type": "board", "items": selected, "disclaimer": DISCLAIMER}


def generate_auto_signals(long_payload: dict[str, Any], board_payload: dict[str, Any], *, strategy: str = "all") -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if strategy in {"all", "long"}:
        for item in long_payload.get("items") or []:
            signals.append({**item, "strategy_type": "long"})
    if strategy in {"all", "board"}:
        for item in board_payload.get("items") or []:
            signals.append({**item, "strategy_type": "board"})
    return signals


def create_auto_pending_orders(
    signals: list[dict[str, Any]],
    *,
    initial_cash: float = 10_000.0,
    max_position_pct: float = 10,
    max_long_positions: int = 5,
    max_board_positions: int = 2,
    sizing_mode: str = "fixed_100",
    existing_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    orders: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    cash_left = float(initial_cash)
    strategy_counts = {"long": 0, "board": 0}
    sizing_mode = "percent_cap" if str(sizing_mode) == "percent_cap" else "fixed_100"
    position_cap_amount = float(initial_cash) * float(max_position_pct) / 100.0
    held_symbols = {normalize_symbol(row.get("symbol")) for row in (existing_positions or []) if row.get("symbol")}
    for signal in signals:
        strategy_type = str(signal.get("strategy_type") or "long")
        symbol = normalize_symbol(signal.get("symbol"))
        if symbol in held_symbols:
            blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "already_holding", "sizing_mode": sizing_mode})
            continue
        if strategy_type == "long" and strategy_counts["long"] >= max_long_positions:
            blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "long_position_limit", "sizing_mode": sizing_mode})
            continue
        if strategy_type == "board" and strategy_counts["board"] >= max_board_positions:
            blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "board_position_limit", "sizing_mode": sizing_mode})
            continue
        source = signal.get("source_item") if isinstance(signal.get("source_item"), dict) else {}
        price = safe_float(source.get("close"), safe_float(signal.get("close"), None))
        if price is None or price <= 0:
            blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "invalid_price", "sizing_mode": sizing_mode})
            continue
        signal_is_st = metadata_is_st(source, signal.get("name")) or metadata_is_st(signal, signal.get("name"))
        lot_size = lot_size_for_symbol(symbol, metadata=source, name=signal.get("name")) if not signal_is_st else lot_size_for_symbol(symbol, metadata={"is_st": True}, name=signal.get("name"))
        if sizing_mode == "fixed_100":
            quantity = lot_size
            entry_reason = f"按各股票板块整手单位规则进入本地纸面组合：板块整手（{lot_size} 股）规则，便于观察策略本身表现。"
            quantity_rule = "board_lot"
        else:
            quantity = round_notional_down_to_lot_quantity(position_cap_amount, price, lot_size=lot_size)
            entry_reason = f"按仓位比例上限计算纸面买入数量，并对齐板块整手（{lot_size} 股）规则，用于进阶资金管理模拟。"
            quantity_rule = "percent_cap_lot"
            if quantity < lot_size:
                blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "less_than_one_lot", "sizing_mode": sizing_mode, "lot_size": lot_size})
                continue
        planned_amount = quantity * price
        cost_info = buy_cash_outflow(price=price, quantity=quantity)
        cash_out = float(cost_info["cash_out"])
        if cash_out > cash_left:
            blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "blocked_cash_fee_inclusive", "sizing_mode": sizing_mode, "lot_size": lot_size, "planned_amount": round(planned_amount, 2), "estimated_fee": cost_info["total_fee"], "cash_out": cash_out})
            continue
        if planned_amount > position_cap_amount:
            blocked.append({**_paper_order_boundary(), **signal, "status": "blocked", "blocked_reason": "blocked_position_cap", "sizing_mode": sizing_mode, "lot_size": lot_size, "planned_amount": round(planned_amount, 2), "position_cap_amount": round(position_cap_amount, 2)})
            continue
        strategy_counts[strategy_type] += 1
        cash_left -= cash_out
        orders.append(
            {
                "symbol": symbol,
                "name": signal["name"],
                "strategy_type": strategy_type,
                "order_source": "auto_strategy_sandbox",
                "signal_date": str(source.get("latest_date") or "")[:10],
                "planned_trade_date": "next_trading_day",
                "side": "virtual_entry",
                "quantity": quantity,
                "lot_size": lot_size,
                "planned_price_model": "next_open_or_conservative",
                "planned_amount": round(planned_amount, 2),
                "estimated_fee": cost_info["total_fee"],
                "cash_out": cash_out,
                "position_cap_amount": round(position_cap_amount, 2),
                "sizing_mode": sizing_mode,
                "quantity_rule": quantity_rule,
                "entry_reason": entry_reason,
                "reason": signal.get("reason", ""),
                "risk_notes": signal.get("risk_notes", []),
                "status": "pending",
                **_paper_order_boundary(),
            }
        )
    return {
        "orders": orders,
        "blocked_orders": blocked,
        "planned_order_count": len(orders),
        "blocked_count": len(blocked),
        "remaining_cash_after_plans": round(cash_left, 2),
        "sizing_mode": sizing_mode,
        "quantity_rule": "board_lot" if sizing_mode == "fixed_100" else "percent_cap_lot",
        **_paper_order_boundary(),
    }


def evaluate_exit_decision(
    position: dict[str, Any],
    current_item: dict[str, Any] | None,
    *,
    long_candidates: set[str] | None = None,
    board_candidates: set[str] | None = None,
    long_exit_missing_days: int = 2,
    long_stop_loss_pct: float = -8,
    long_take_profit_pct: float = 15,
    board_max_hold_days: int = 3,
    board_stop_loss_pct: float = -5,
    sell_on_data_blocked: bool = True,
    sell_on_very_low_liquidity: bool = True,
) -> dict[str, Any]:
    """Return hold / sell / watch_exit / block for an existing paper position."""

    symbol = normalize_symbol(position.get("symbol"))
    name = str(position.get("name") or symbol)
    strategy_type = str(position.get("strategy_type") or position.get("strategy") or "long")
    item = current_item or {}
    latest_close = safe_float(item.get("close"), safe_float(position.get("last_price"), safe_float(position.get("entry_price"), 0.0)))
    entry_price = safe_float(position.get("entry_price"), safe_float(position.get("price"), latest_close))
    pnl_pct = _pct_change(entry_price, latest_close) or 0.0
    decision_date = str(item.get("latest_date") or position.get("decision_date") or "")[:10]
    common = {
        "symbol": symbol,
        "name": name,
        "strategy_type": strategy_type,
        "decision_date": decision_date,
        "current_close": latest_close,
        "entry_price": entry_price,
        "pnl_pct": round(pnl_pct, 2),
        "risk_notes": [DISCLAIMER],
    }
    if not item:
        return {
            **common,
            "action": "watch_exit",
            "trigger_rule": "missing_current_daily",
            "reason": "缺少当前日线，进入退出观察。",
            "expected_effect": "避免在数据不足时做出纸面退出结论。",
        }
    data_status = str(item.get("data_confidence_status") or "ok").lower()
    liquidity_status = str(item.get("liquidity_status") or "unknown").lower()
    if sell_on_data_blocked and data_status == "blocked":
        return _sell_action(common, "data_confidence_blocked", "数据可信度不足，不适合继续模拟。", "按数据门禁退出纸面组合")
    if sell_on_very_low_liquidity and liquidity_status == "very_low":
        return _sell_action(common, "very_low_liquidity", "成交活跃度过低，模拟流动性风险上升。", "按流动性风险规则退出纸面组合")
    position_is_st = metadata_is_st(item, name) or metadata_is_st(position, position.get("name"))
    if strategy_type == "board":
        hold_days = int(safe_float(position.get("holding_days"), safe_float(position.get("hold_days"), 0)) or 0)
        if _one_word_limit_down(item, symbol=symbol, is_st=position_is_st):
            return {
                **common,
                "action": "block",
                "trigger_rule": "one_word_limit_down_blocked_sell",
                "reason": "疑似一字跌停，今日无法模拟卖出。",
                "expected_effect": "记录纸面退出受阻，而不是伪造成交。",
                "blocked_reason": "blocked_sell",
            }
        if hold_days >= int(board_max_hold_days):
            return _sell_action(common, "board_max_hold_days", "打板模拟超过最大持有天数，按短线高风险规则纸面退出。", "控制打板模拟持有周期")
        if pnl_pct <= float(board_stop_loss_pct):
            return _sell_action(common, "board_stop_loss_pct", f"触发打板模拟止损阈值 {board_stop_loss_pct}%。", "降低短线模拟继续回撤风险")
        prev_close = safe_float(item.get("prev_close"), None)
        if prev_close and latest_close <= prev_close * 1.02:
            return _sell_action(common, "failed_follow_through", "打板次日未继续强势，按短线模拟规则退出。", "避免短线逻辑失败后继续回撤")
        return {
            **common,
            "action": "hold",
            "trigger_rule": "board_follow_through_observe",
            "hold_reason": "打板模拟仍处于短线观察窗口，未触发止损或持有天数上限。",
            "reason": "继续纸面持有观察。",
            "expected_effect": "继续观察短线延续性",
        }

    ma20 = safe_float(item.get("ma20"), None)
    if pnl_pct <= float(long_stop_loss_pct):
        return _sell_action(common, "long_stop_loss_pct", f"当前浮亏达到 {long_stop_loss_pct}% 止损阈值。", "降低趋势失效后的继续下跌风险")
    if pnl_pct >= float(long_take_profit_pct):
        return _sell_action(common, "long_take_profit_pct", f"当前浮盈达到 {long_take_profit_pct}% 止盈阈值。", "锁定纸面收益并复盘规则")
    if ma20 and latest_close < ma20:
        return _sell_action(common, "close_below_ma20", "收盘价跌破 MA20，长线趋势条件失效。", "按长线策略规则退出纸面组合")
    long_candidates = long_candidates or set()
    missing_days = int(safe_float(position.get("missing_candidate_days"), 0) or 0)
    if symbol not in long_candidates:
        missing_days += 1
        if missing_days >= int(long_exit_missing_days):
            return _sell_action(common, "missing_long_candidate_days", f"连续 {missing_days} 天未进入长线模拟候选，退出观察确认。", "降低规则失效后的继续持有风险")
        return {
            **common,
            "action": "watch_exit",
            "trigger_rule": "not_long_candidate_watch_exit",
            "reason": "未进入长线模拟候选，但尚未达到退出确认天数。",
            "expected_effect": "等待下一日确认，避免因单日波动过早纸面退出",
            "missing_candidate_days": missing_days,
        }
    return {
        **common,
        "action": "hold",
        "trigger_rule": "still_long_candidate",
        "hold_reason": "仍在长线模拟候选中，未触发止损/止盈/趋势失效。",
        "reason": "继续纸面持有观察。",
        "expected_effect": "继续观察趋势延续",
        "missing_candidate_days": 0,
    }


def _sell_action(common: dict[str, Any], trigger_rule: str, trigger_detail: str, expected_effect: str) -> dict[str, Any]:
    exit_price = safe_float(common.get("current_close"), 0.0) or 0.0
    strategy_type = str(common.get("strategy_type") or "long")
    return {
        **common,
        "action": "sell",
        "trigger_rule": trigger_rule,
        "trigger_detail": trigger_detail,
        "exit_price": exit_price,
        "exit_reason": f"按{strategy_type}策略规则纸面退出组合",
        "expected_effect": expected_effect,
        "side": "virtual_exit",
    }


def build_strategy_decision_log(
    *,
    orders_payload: dict[str, Any],
    positions: list[dict[str, Any]] | None,
    universe_payload: dict[str, Any],
    long_candidates: list[dict[str, Any]] | None = None,
    board_candidates: list[dict[str, Any]] | None = None,
    decision_date: str | None = None,
    long_exit_missing_days: int = 2,
    long_stop_loss_pct: float = -8,
    long_take_profit_pct: float = 15,
    board_max_hold_days: int = 3,
    board_stop_loss_pct: float = -5,
    sell_on_data_blocked: bool = True,
    sell_on_very_low_liquidity: bool = True,
) -> dict[str, Any]:
    """Build daily strategy decision log for buy / hold / sell / skip / block."""

    price_map = _price_by_symbol(universe_payload)
    long_symbols = {str(row.get("symbol")) for row in (long_candidates or [])}
    board_symbols = {str(row.get("symbol")) for row in (board_candidates or [])}
    decision_date = decision_date or str(universe_payload.get("expected_latest_date") or "")[:10] or now_text()[:10]
    actions: list[dict[str, Any]] = []
    for order in orders_payload.get("orders") or []:
        actions.append(
            {
                "symbol": order.get("symbol"),
                "name": order.get("name"),
                "strategy_type": order.get("strategy_type"),
                "decision_date": decision_date,
                "action": "buy",
                "trigger_rule": "自动候选信号",
                "entry_reason": order.get("entry_reason") or order.get("reason") or "自动策略沙盒生成模拟买入计划。",
                "reason": order.get("reason") or "生成本地纸面模拟计划。",
                "entry_price": safe_float(order.get("planned_amount"), 0) / max(safe_float(order.get("quantity"), 1) or 1, 1),
                "expected_effect": "进入本地纸面交易观察，验证规则后续表现。",
                "risk_notes": order.get("risk_notes", []),
                "order_source": "auto_strategy_sandbox",
            }
        )
    for blocked in orders_payload.get("blocked_orders") or []:
        actions.append(
            {
                "symbol": blocked.get("symbol"),
                "name": blocked.get("name"),
                "strategy_type": blocked.get("strategy_type"),
                "decision_date": decision_date,
                "action": "block",
                "trigger_rule": str(blocked.get("blocked_reason") or "risk_block"),
                "reason": str(blocked.get("blocked_reason") or "被纸面风控阻断。"),
                "expected_effect": "避免在约束不足时生成纸面订单。",
                "risk_notes": blocked.get("risk_notes", [DISCLAIMER]),
                "order_source": "auto_strategy_sandbox",
            }
        )
    for position in positions or []:
        symbol = normalize_symbol(position.get("symbol"))
        decision = evaluate_exit_decision(
            position,
            price_map.get(symbol),
            long_candidates=long_symbols,
            board_candidates=board_symbols,
            long_exit_missing_days=long_exit_missing_days,
            long_stop_loss_pct=long_stop_loss_pct,
            long_take_profit_pct=long_take_profit_pct,
            board_max_hold_days=board_max_hold_days,
            board_stop_loss_pct=board_stop_loss_pct,
            sell_on_data_blocked=sell_on_data_blocked,
            sell_on_very_low_liquidity=sell_on_very_low_liquidity,
        )
        decision["decision_date"] = decision_date
        actions.append(decision)
    counts = Counter(str(action.get("action") or "unknown") for action in actions)
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "decision_date": decision_date,
        "actions": actions,
        "action_counts": dict(counts),
        "disclaimer": DISCLAIMER,
        "single_day_review_note": SINGLE_DAY_REVIEW_NOTE,
    }


def review_strategy_decisions(
    decision_log: dict[str, Any] | None,
    current_prices: dict[str, dict[str, Any]],
    *,
    review_date: str | None = None,
    threshold_pct: float = 1.0,
) -> dict[str, Any]:
    """Review previous strategy decisions using the next available daily close."""

    if not isinstance(decision_log, dict) or not isinstance(decision_log.get("actions"), list):
        return {
            "ok": True,
            "exists": False,
            "message": "暂无可验证的前一日策略决策日志。",
            "decision_quality_score": 0.0,
            "strategy_learning_notes": [SINGLE_DAY_REVIEW_NOTE],
        }
    reviews: list[dict[str, Any]] = []
    counters = Counter()
    sell_review = Counter()
    hold_review = Counter()
    buy_review = Counter()
    skip_review = Counter()
    block_review = Counter()
    for action in decision_log.get("actions") or []:
        symbol = normalize_symbol(action.get("symbol"))
        price_row = current_prices.get(symbol, {})
        next_close = safe_float(price_row.get("close"), None)
        result = "neutral"
        category = "neutral"
        note = "缺少次日收盘价，暂记为中性。"
        return_pct = None
        if next_close is not None:
            if action.get("action") == "sell":
                exit_price = safe_float(action.get("exit_price"), safe_float(action.get("current_close"), None))
                return_pct = _pct_change(exit_price, next_close)
                sell_review["sell_count"] += 1
                if return_pct is not None and return_pct < -threshold_pct:
                    result = "avoided_loss"
                    category = "correct"
                    sell_review["avoided_loss_count"] += 1
                    note = "纸面卖出后次日下跌，本次单日验证标记为 avoided_loss。"
                elif return_pct is not None and return_pct > threshold_pct:
                    result = "premature_exit_or_missed_gain"
                    category = "wrong"
                    sell_review["premature_exit_count"] += 1
                    note = "纸面卖出后次日上涨，本次单日验证标记为 premature_exit_or_missed_gain。"
                else:
                    note = "纸面卖出后次日变化较小，暂记为中性。"
            elif action.get("action") == "hold":
                base = safe_float(action.get("current_close"), safe_float(action.get("entry_price"), None))
                return_pct = _pct_change(base, next_close)
                hold_review["hold_count"] += 1
                if return_pct is not None and return_pct >= 0:
                    result = "good_hold"
                    category = "correct"
                    hold_review["good_hold_count"] += 1
                    note = "继续纸面持有后次日未下跌，本次单日验证标记为 good_hold。"
                elif return_pct is not None and return_pct <= -3:
                    result = "bad_hold"
                    category = "wrong"
                    hold_review["bad_hold_count"] += 1
                    note = "继续纸面持有后次日下跌超过 3%，需复核 hold 条件。"
                else:
                    note = "继续纸面持有后次日小幅波动，暂记为中性。"
            elif action.get("action") == "buy":
                entry = safe_float(action.get("entry_price"), None)
                return_pct = _pct_change(entry, next_close)
                buy_review["buy_count"] += 1
                if return_pct is not None and return_pct >= 0:
                    result = "buy_rule_temporarily_valid"
                    category = "correct"
                    buy_review["correct_buy_count"] += 1
                    note = "纸面买入计划次日未下跌，本次单日验证暂时符合预期。"
                elif return_pct is not None and return_pct <= -3:
                    result = "buy_rule_failed"
                    category = "wrong"
                    buy_review["wrong_buy_count"] += 1
                    note = "纸面买入计划次日明显下跌，需复核买入规则。"
            elif action.get("action") == "skip":
                skip_review["skip_count"] += 1
                result = "skip_needs_observation"
                note = "跳过样本的次日表现只用于观察过滤条件是否过严。"
            elif action.get("action") == "block":
                base = safe_float(action.get("current_close"), safe_float(action.get("entry_price"), next_close))
                return_pct = _pct_change(base, next_close)
                block_review["block_count"] += 1
                if return_pct is not None and return_pct < -threshold_pct:
                    result = "risk_block_effective"
                    category = "correct"
                    block_review["correct_block_count"] += 1
                    note = "风控阻断后次日下跌，本次单日验证显示阻断暂时有效。"
                elif return_pct is not None and return_pct > threshold_pct:
                    result = "risk_block_maybe_strict"
                    category = "wrong"
                    block_review["wrong_block_count"] += 1
                    note = "风控阻断后次日上涨，说明规则可能偏严，但不构成投资建议。"
        counters[f"{category}_decisions"] += 1
        reviews.append(
            {
                "symbol": symbol,
                "name": action.get("name"),
                "strategy_type": action.get("strategy_type"),
                "action": action.get("action"),
                "trigger_rule": action.get("trigger_rule"),
                "exit_price": action.get("exit_price"),
                "entry_price": action.get("entry_price"),
                "previous_close": action.get("current_close"),
                "next_close": next_close,
                "return_pct": round(return_pct, 2) if return_pct is not None else None,
                "decision_result": result,
                "quality_category": category,
                "note": note,
            }
        )
    total = len(reviews)
    correct = counters.get("correct_decisions", 0)
    wrong = counters.get("wrong_decisions", 0)
    neutral = counters.get("neutral_decisions", 0)
    score = round(correct / total * 100, 2) if total else 0.0
    payload = {
        "ok": True,
        "exists": True,
        "review_date": review_date or now_text()[:10],
        "review_target_date": decision_log.get("decision_date"),
        "total_decisions": total,
        "correct_decisions": correct,
        "wrong_decisions": wrong,
        "neutral_decisions": neutral,
        "decision_quality_score": score,
        "sell_review": dict(sell_review),
        "hold_review": dict(hold_review),
        "buy_review": dict(buy_review),
        "skip_review": dict(skip_review),
        "block_review": dict(block_review),
        "items": reviews,
        "strategy_learning_notes": build_strategy_learning_notes_from_review({"items": reviews, "decision_quality_score": score}),
        "single_day_review_note": SINGLE_DAY_REVIEW_NOTE,
        "disclaimer": DISCLAIMER,
    }
    return payload


def evaluate_strategy_performance(orders_payload: dict[str, Any], *, positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    positions = positions or []
    top_failure_reasons = Counter(str(row.get("blocked_reason") or "unknown") for row in orders_payload.get("blocked_orders") or [])
    notes = build_strategy_learning_notes(orders_payload)
    return {
        "ok": True,
        "generated_at": now_text(),
        "signal_count": orders_payload.get("planned_order_count", 0) + orders_payload.get("blocked_count", 0),
        "planned_order_count": orders_payload.get("planned_order_count", 0),
        "filled_count": 0,
        "blocked_count": orders_payload.get("blocked_count", 0),
        "skipped_count": 0,
        "position_count": len(positions),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_equity": 0.0,
        "total_return_pct": 0.0,
        "long_strategy_return_pct": 0.0,
        "board_strategy_return_pct": 0.0,
        "win_rate": 0.0,
        "avg_holding_days": 0.0,
        "max_drawdown_pct": 0.0,
        "top_failure_reasons": dict(top_failure_reasons),
        "strategy_learning_notes": notes,
        "disclaimer": DISCLAIMER,
    }


def build_strategy_learning_notes(orders_payload: dict[str, Any]) -> list[str]:
    blocked = orders_payload.get("blocked_orders") or []
    notes = [
        "本结果是策略规则复盘建议，仅用于学习，不构成投资建议。",
        "如低流动性或历史不足导致样本减少，可先完善本地日线缓存和低价报告。",
    ]
    reasons = {str(row.get("blocked_reason") or "") for row in blocked}
    if "less_than_one_lot" in reasons:
        notes.append("不足一手的样本较多时，可以降低单票价格上限或提高模拟账户资金后再观察规则表现。")
    if "cash_not_enough" in reasons:
        notes.append("现金不足导致计划阻断，说明当前单样本暴露或候选数量需要在纸面环境中复核。")
    if "board_position_limit" in reasons:
        notes.append("打板候选超过上限时，优先保留成交活跃度更高、数据更完整的样本。")
    return notes


def build_strategy_learning_notes_from_review(decision_review: dict[str, Any]) -> list[str]:
    items = decision_review.get("items") or []
    sell_items = [row for row in items if row.get("action") == "sell"]
    hold_items = [row for row in items if row.get("action") == "hold"]
    buy_items = [row for row in items if row.get("action") == "buy"]
    notes = [SINGLE_DAY_REVIEW_NOTE]
    if sell_items:
        avoided = sum(1 for row in sell_items if row.get("decision_result") == "avoided_loss")
        premature = sum(1 for row in sell_items if row.get("decision_result") == "premature_exit_or_missed_gain")
        notes.append(f"昨日 {len(sell_items)} 个纸面退出决策中，{avoided} 个避免了次日下跌，{premature} 个退出后次日继续上涨，说明卖出规则需要区分趋势失效与短期波动。")
    if hold_items:
        good = sum(1 for row in hold_items if row.get("decision_result") == "good_hold")
        bad = sum(1 for row in hold_items if row.get("decision_result") == "bad_hold")
        notes.append(f"昨日 {len(hold_items)} 个持有决策中，{good} 个次日表现暂时符合预期，{bad} 个出现较大回撤，应重点复核跌破 MA20 或流动性变化样本。")
    if buy_items:
        failed = sum(1 for row in buy_items if row.get("decision_result") == "buy_rule_failed")
        notes.append(f"昨日 {len(buy_items)} 个纸面买入计划中，{failed} 个次日明显走弱，需继续累计样本观察规则稳定性。")
    score = safe_float(decision_review.get("decision_quality_score"), None)
    if score is not None:
        notes.append(f"本次单日决策质量分为 {score:.2f}，仅作为学习观察指标，不代表策略长期有效。")
    return notes


def _markdown_report(payload: dict[str, Any], review: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    decision_log = payload.get("decision_log") if isinstance(payload.get("decision_log"), dict) else {}
    decision_review = payload.get("decision_review") if isinstance(payload.get("decision_review"), dict) else {}
    lines = [
        "# 自动策略沙盒报告",
        "",
        f"生成时间：{payload.get('generated_at', '')}",
        "",
        "本报告仅用于策略学习与模拟验证，不构成投资建议。不接券商，不真实下单。",
        "",
        "## 自动流程摘要",
        "",
        f"- 可模拟股票数：{summary.get('simulation_ready_count', 0)}",
        f"- 长线模拟候选：{summary.get('long_candidate_count', 0)}",
        f"- 打板模拟候选：{summary.get('board_candidate_count', 0)}",
        f"- 生成模拟订单：{summary.get('planned_order_count', 0)}",
        f"- 阻断订单：{summary.get('blocked_count', 0)}",
        f"- 买入数量模式：{summary.get('sizing_mode', '-')}",
        f"- 数量规则说明：{summary.get('sizing_note', '-')}",
        "",
        "## 策略学习笔记",
        "",
    ]
    for note in review.get("strategy_learning_notes") or []:
        lines.append(f"- {note}")
    lines.extend(["", "## 失败原因", ""])
    failures = review.get("top_failure_reasons") or {}
    if failures:
        for key, value in failures.items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- 暂无阻断原因。")
    lines.extend(
        [
            "",
            "## 策略决策审计",
            "",
            f"- 决策日期：{decision_log.get('decision_date', '-')}",
            f"- 决策动作数：{len(decision_log.get('actions') or [])}",
            f"- 决策质量分：{decision_review.get('decision_quality_score', 0)}",
            f"- 验证目标日期：{decision_review.get('review_target_date', '-')}",
            "",
            SINGLE_DAY_REVIEW_NOTE,
            "",
            "### 卖出决策复盘",
            "",
        ]
    )
    sell_rows = [row for row in decision_review.get("items", []) if row.get("action") == "sell"]
    if sell_rows:
        for row in sell_rows:
            lines.append(f"- {row.get('symbol')} {row.get('name')}: {row.get('trigger_rule')} / {row.get('decision_result')} / {row.get('note')}")
    else:
        lines.append("- 暂无纸面退出决策。")
    lines.extend(["", "### 策略决策学习笔记", ""])
    for note in decision_review.get("strategy_learning_notes") or []:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def run_auto_strategy_cycle(
    *,
    mode: str = "live",
    strategy: str = "all",
    initial_cash: float = 10_000.0,
    max_long_positions: int = 5,
    max_board_positions: int = 2,
    max_long_total: int = 20,
    max_board_total: int = 10,
    max_long_per_theme: int = 3,
    max_board_per_theme: int = 2,
    max_position_pct: float = 10,
    sizing_mode: str = "fixed_100",
    lookback_days: int = 120,
    long_exit_missing_days: int = 2,
    long_stop_loss_pct: float = -8,
    long_take_profit_pct: float = 15,
    board_max_hold_days: int = 3,
    board_stop_loss_pct: float = -5,
    sell_on_data_blocked: bool = True,
    sell_on_very_low_liquidity: bool = True,
    dry_run: bool = False,
    universe_path: Path | None = None,
    daily_dir: Path | None = None,
    report_json: Path | None = None,
    expected_latest_date: str | None = None,
) -> dict[str, Any]:
    universe_payload = select_simulation_universe(
        universe_path,
        daily_dir,
        report_json,
        expected_latest_date=expected_latest_date,
    )
    long_payload = select_long_candidates(universe_payload, limit=max_long_total, per_theme_limit=max_long_per_theme)
    board_payload = select_board_candidates(universe_payload, limit=max_board_total, per_theme_limit=max_board_per_theme)
    feature_status = daily_feature_status()
    freshness_payload = {
        "target_complete_trade_date": universe_payload.get("expected_latest_date") or feature_status.get("latest_date") or "",
        "local_daily_latest_date": universe_payload.get("expected_latest_date") or "",
        "daily_feature_date": feature_status.get("latest_date") or "",
        "is_daily_stale": bool(
            universe_payload.get("expected_latest_date")
            and feature_status.get("latest_date")
            and str(feature_status.get("latest_date"))[:10] < str(universe_payload.get("expected_latest_date"))[:10]
        ),
    }
    latest_theme_map = load_theme_map()
    long_payload["items"] = attach_freshness_to_candidates(
        attach_theme_map_to_candidates(long_payload.get("items", []), latest_theme_map),
        freshness_payload,
    )
    board_payload["items"] = attach_freshness_to_candidates(
        attach_theme_map_to_candidates(board_payload.get("items", []), latest_theme_map),
        freshness_payload,
    )
    standard_candidates = candidate_rows_from_strategy_payload(
        {
            "long_candidates": long_payload.get("items", []),
            "board_candidates": board_payload.get("items", []),
        },
        strategy=strategy,
    )
    theme_sector_summary = summarize_theme_sector(
        universe_payload.get("items") or [],
        long_payload.get("items") or [],
        board_payload.get("items") or [],
    )
    signals = generate_auto_signals(long_payload, board_payload, strategy=strategy)
    existing_positions = _load_existing_positions()
    orders_payload = create_auto_pending_orders(
        signals,
        initial_cash=initial_cash,
        max_position_pct=max_position_pct,
        max_long_positions=max_long_positions,
        max_board_positions=max_board_positions,
        sizing_mode=sizing_mode,
        existing_positions=existing_positions,
    )
    previous_decision_log = read_json_safe(STRATEGY_DECISION_LOG_JSON, {})
    decision_review = review_strategy_decisions(
        previous_decision_log if isinstance(previous_decision_log, dict) else {},
        _price_by_symbol(universe_payload),
        review_date=str(universe_payload.get("expected_latest_date") or "")[:10] or None,
    )
    decision_log = build_strategy_decision_log(
        orders_payload=orders_payload,
        positions=existing_positions,
        universe_payload=universe_payload,
        long_candidates=long_payload.get("items", []),
        board_candidates=board_payload.get("items", []),
        long_exit_missing_days=long_exit_missing_days,
        long_stop_loss_pct=long_stop_loss_pct,
        long_take_profit_pct=long_take_profit_pct,
        board_max_hold_days=board_max_hold_days,
        board_stop_loss_pct=board_stop_loss_pct,
        sell_on_data_blocked=sell_on_data_blocked,
        sell_on_very_low_liquidity=sell_on_very_low_liquidity,
    )
    review = evaluate_strategy_performance(orders_payload)
    review["decision_review"] = decision_review
    review["strategy_learning_notes"] = list(dict.fromkeys((review.get("strategy_learning_notes") or []) + (decision_review.get("strategy_learning_notes") or [])))
    summary = {
        "mode": mode,
        "strategy": strategy,
        "low_price_universe_path": str(universe_path or UNIVERSE_PATH),
        "latest_daily_date": universe_payload.get("expected_latest_date") or "",
        "simulation_ready_count": universe_payload.get("simulation_ready_count", 0),
        "long_candidate_count": len(long_payload.get("items") or []),
        "board_candidate_count": len(board_payload.get("items") or []),
        "candidate_strategy_counts": summarize_candidates_by_strategy(standard_candidates),
        "candidate_theme_coverage": summarize_candidate_theme_coverage(standard_candidates),
        "max_long_total": int(max_long_total),
        "max_board_total": int(max_board_total),
        "max_long_per_theme": int(max_long_per_theme),
        "max_board_per_theme": int(max_board_per_theme),
        "planned_order_count": orders_payload.get("planned_order_count", 0),
        "filled_count": 0,
        "blocked_count": orders_payload.get("blocked_count", 0),
        "lookback_days": int(lookback_days),
        "sizing_mode": orders_payload.get("sizing_mode") or sizing_mode,
        "quantity_rule": orders_payload.get("quantity_rule"),
        "sizing_note": "按各股票板块整手单位模拟买入（主板 100 股、科创板 200 股等），用于直观观察策略效果。max_position_pct 仅作为单票风险上限。" if (orders_payload.get("sizing_mode") or sizing_mode) == "fixed_100" else "按仓位比例计算纸面买入数量，并对齐板块整手单位，用于进阶资金管理模拟。",
    }
    paper_result = {
        "ok": True,
        "mode": mode,
        "strategy": strategy,
        "sizing_mode": orders_payload.get("sizing_mode") or sizing_mode,
        "generated_at": now_text(),
        "initial_cash": initial_cash,
        "cash": initial_cash,
        "market_value": 0.0,
        "total_equity": initial_cash,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_return_pct": 0.0,
        "positions": [],
        "pending_orders": orders_payload.get("orders", []),
        "trades": [],
        "signals_today": signals,
        "next_plan": orders_payload.get("orders", []),
        "blocked_orders": orders_payload.get("blocked_orders", []),
        "risk_notes": [BROKER_BOUNDARY, DISCLAIMER],
        "disclaimer": DISCLAIMER,
        "order_source": "auto_strategy_sandbox",
    }
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "mode": mode,
        "strategy": strategy,
        "dry_run": dry_run,
        "summary": summary,
        "sizing_mode": orders_payload.get("sizing_mode") or sizing_mode,
        "quantity_rule": orders_payload.get("quantity_rule"),
        "universe": universe_payload,
        "long_candidates": long_payload.get("items", []),
        "board_candidates": board_payload.get("items", []),
        "standard_candidates": standard_candidates,
        "candidate_strategy_counts": summarize_candidates_by_strategy(standard_candidates),
        "candidate_theme_coverage": summarize_candidate_theme_coverage(standard_candidates),
        "theme_sector_summary": theme_sector_summary,
        "daily_feature_status": feature_status,
        "pending_orders": orders_payload.get("orders", []),
        "blocked_orders": orders_payload.get("blocked_orders", []),
        "paper_trading": paper_result,
        "review": review,
        "decision_log": decision_log,
        "decision_review": decision_review,
        "strategy_learning_notes": review.get("strategy_learning_notes", []),
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
        "task_scheduler_command": "python scripts/run_auto_strategy_sandbox.py --mode live --strategy all --initial-cash 10000",
    }
    if not dry_run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORTS_DIR / f"auto_strategy_report_{stamp}.md"
        review_path = REPORTS_DIR / f"auto_strategy_review_{stamp}.md"
        markdown = _markdown_report(payload, review)
        report_path.write_text(markdown, encoding="utf-8")
        review_path.write_text(markdown, encoding="utf-8")
        payload["report_path"] = str(report_path)
        payload["review_report_path"] = str(review_path)
        atomic_write_json(AUTO_STRATEGY_UNIVERSE_JSON, universe_payload)
        atomic_write_json(AUTO_LONG_CANDIDATES_JSON, long_payload)
        atomic_write_json(AUTO_BOARD_CANDIDATES_JSON, board_payload)
        save_theme_sector_summary(theme_sector_summary, THEME_SECTOR_SUMMARY_LATEST_JSON)
        atomic_write_json(AUTO_PENDING_ORDERS_JSON, {"orders": orders_payload.get("orders", []), "updated_at": now_text(), "disclaimer": DISCLAIMER})
        atomic_write_json(AUTO_STRATEGY_REVIEW_JSON, review)
        atomic_write_json(STRATEGY_DECISION_LOG_JSON, decision_log)
        atomic_write_json(STRATEGY_DECISION_REVIEW_JSON, decision_review)
        atomic_write_json(PAPER_TRADING_LATEST_JSON, paper_result)
        atomic_write_json(AUTO_STRATEGY_LATEST_JSON, payload)
    return payload


def auto_strategy_artifacts() -> list[dict[str, Any]]:
    items = [
        ("自动策略沙盒 JSON", "JSON", AUTO_STRATEGY_LATEST_JSON, "自动沙盒最新结构化结果"),
        ("自动可模拟股票池", "JSON", AUTO_STRATEGY_UNIVERSE_JSON, "可模拟股票与跳过原因"),
        ("长线模拟候选", "JSON", AUTO_LONG_CANDIDATES_JSON, "长线候选排序结果"),
        ("打板模拟候选", "JSON", AUTO_BOARD_CANDIDATES_JSON, "打板候选排序结果"),
        ("题材板块汇总", "JSON", THEME_SECTOR_SUMMARY_LATEST_JSON, "按本地题材/板块映射汇总候选与可模拟股票"),
        ("自动策略复盘", "JSON", AUTO_STRATEGY_REVIEW_JSON, "失败原因和策略学习笔记"),
        ("策略决策日志", "JSON", STRATEGY_DECISION_LOG_JSON, "每日 buy / hold / sell / skip / block 策略动作"),
        ("策略决策验证", "JSON", STRATEGY_DECISION_REVIEW_JSON, "次日验证与决策质量评分"),
        ("自动模拟订单", "JSON", AUTO_PENDING_ORDERS_JSON, "本地纸面交易 pending orders"),
    ]
    rows = []
    for name, kind, path, purpose in items:
        exists = path.exists()
        stat = path.stat() if exists else None
        rows.append(
            {
                "name": name,
                "type": kind,
                "path": str(path.relative_to(PROJECT_ROOT)),
                "exists": exists,
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds") if stat else "",
                "file_size": stat.st_size if stat else 0,
                "purpose": purpose,
            }
        )
    return rows
