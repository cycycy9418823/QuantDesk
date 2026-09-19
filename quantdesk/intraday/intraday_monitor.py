"""Intraday strategy monitor — closed-loop polling for paper portfolio.

Polls Sina spot snapshots for watchlist and paper-portfolio symbols at a
configurable interval, evaluates strategy entry/exit conditions against
live prices merged with the latest daily indicators, and auto-executes
virtual (paper) trades when conditions trigger.

All trades are **paper only** — no broker connection, no real orders.

Flow per cycle:
    1. Fetch latest prices (Sina batch snapshot).
    2. For each paper-portfolio position → check ``exit_conditions``.
       If triggered → ``paper_sell`` and record signal.
    3. For each watchlist candidate not yet in portfolio → check
       ``entry_conditions``.  If triggered → ``paper_buy`` and record signal.
    4. Persist cycle log to ``state/intraday_monitor_log.json``.

The module exposes ``run_monitor_loop`` (blocking, runs until market close)
and ``run_single_cycle`` (one-shot, for testing and API usage).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_providers import fetch_spot_sina_batch
from quantdesk.market_clock import (
    AFTERNOON_END,
    MORNING_START,
    is_cn_a_share_trading_day,
    to_shanghai_datetime,
)
from quantdesk.paper.paper_trading import (
    load_paper_portfolio,
    paper_buy,
    paper_sell,
    save_paper_portfolio,
    update_paper_marks,
)
from quantdesk.signal_tracker import (
    append_signal,
    record_signal,
)
from quantdesk.strategy_conditions import evaluate_conditions
from quantdesk.strategy_registry import load_strategy_registry
from quantdesk.utils import safe_float
from quantdesk.watchlist import list_watchlist

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / "state"
MONITOR_LOG_PATH = STATE_DIR / "intraday_monitor_log.json"
SIGNAL_STORE_PATH = STATE_DIR / "signals.json"
DAILY_CACHE_DIR = PROJECT_ROOT / "data" / "daily"

MONITOR_DISCLAIMER = (
    "盘中监控为虚拟模拟，不接券商、不自动下单、不构成投资建议。"
    "所有买卖操作仅在虚拟组合中执行，用于策略研究和回测验证。"
)

DEFAULT_INTERVAL_SECONDS = 180  # 3 minutes
DEFAULT_BUY_SHARES = 100
DEFAULT_MAX_POSITIONS = 10
DEFAULT_POSITION_LIMIT_PCT = 0.10  # max 10% of portfolio per stock


@dataclass
class CycleResult:
    """Result of one monitor polling cycle."""

    checked_at: str = ""
    symbols_polled: int = 0
    prices_fetched: int = 0
    exit_triggers: list[dict[str, Any]] = field(default_factory=list)
    entry_triggers: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    disclaimer: str = MONITOR_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Daily indicator cache (read-only, loaded once per session)
# ---------------------------------------------------------------------------

def _load_daily_features(symbol: str) -> dict[str, Any]:
    """Load latest daily indicators for *symbol* from the features CSV.

    Falls back to reading the daily cache CSV and computing basic fields.
    """
    import pandas as pd

    features_dir = PROJECT_ROOT / "reports"
    # Try loading from the latest features CSV
    feature_files = sorted(features_dir.glob("*_features.csv"), reverse=True)
    for fpath in feature_files[:3]:
        try:
            df = read_csv_frame(fpath, dtype=str)
            read_warnings = list(df.attrs.get("warnings") or [])
            code_col = "code" if "code" in df.columns else ("symbol" if "symbol" in df.columns else None)
            if code_col is None:
                continue
            df[code_col] = df[code_col].astype(str).str.zfill(6)
            row = df[df[code_col] == symbol.zfill(6)]
            if row.empty:
                continue
            record = row.iloc[0].to_dict()
            # Convert numeric fields
            for key, val in record.items():
                fval = safe_float(val)
                if fval is not None:
                    record[key] = fval
            if read_warnings:
                record["csv_read_warnings"] = read_warnings
            return record
        except Exception:
            continue

    # Fallback: read daily cache and compute basic MA values
    cache_path = DAILY_CACHE_DIR / f"{symbol.zfill(6)}.csv"
    if not cache_path.exists():
        return {}
    try:
        df = read_csv_frame(cache_path)
        read_warnings = list(df.attrs.get("warnings") or [])
        if df.empty or len(df) < 5:
            return {}
        date_col = "date" if "date" in df.columns else df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.sort_values(date_col).dropna(subset=[date_col])
        last = df.iloc[-1]
        result: dict[str, Any] = {}
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                result[col] = safe_float(last.get(col)) or 0.0
        if "close" in df.columns:
            closes = pd.to_numeric(df["close"], errors="coerce")
            if len(closes) >= 5:
                result["ma5"] = float(closes.tail(5).mean())
            if len(closes) >= 10:
                result["ma10"] = float(closes.tail(10).mean())
            if len(closes) >= 20:
                result["ma20"] = float(closes.tail(20).mean())
            if len(closes) >= 60:
                result["ma60"] = float(closes.tail(60).mean())
            if len(closes) >= 2:
                result["previous_close"] = float(closes.iloc[-2])
        if read_warnings:
            result["csv_read_warnings"] = read_warnings
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

_MARKET_PREFIXES = ("sh", "sz", "bj")
# A quote identity is either a bare six-digit code or an explicit allowed
# market prefix + six-digit code. Nothing else is coerced.
_QUOTE_SYMBOL_PATTERN = re.compile(r"^(?:(sh|sz|bj))?([0-9]{6})$")


def _quote_symbol_key(raw_symbol: Any) -> str:
    """Normalize a provider quote symbol to its 6-digit consumption key.

    Only ``sh``/``sz``/``bj`` plus exactly six digits, or a bare six-digit code,
    are accepted. Malformed identities (``hk920010``, ``bad920010``,
    ``bj1920010``) are rejected instead of having their digits extracted, so a
    corrupt response can never overwrite a valid quote under the same key.
    """

    text = str(raw_symbol or "").strip().lower()
    match = _QUOTE_SYMBOL_PATTERN.fullmatch(text)
    if not match:
        return ""
    return match.group(2)


def _fetch_prices_detailed(
    symbols: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Fetch quotes and return ``(quotes, provider_errors, warnings)``.

    Keys are the normalized 6-digit consumption codes. Provider-level failures,
    requested-but-unquoted symbols, and malformed quote identities are passed
    back explicitly instead of being silently dropped.
    """

    if not symbols:
        return {}, [], []
    batch = fetch_spot_sina_batch(symbols, batch_size=50)
    quotes: dict[str, dict[str, Any]] = {}
    errors = [f"sina_provider_error: {item}" for item in list(getattr(batch, "provider_errors", []) or [])]
    warnings = [f"sina_warning: {item}" for item in list(getattr(batch, "warnings", []) or [])]
    for row in batch.rows:
        raw = row.get("symbol")
        code = _quote_symbol_key(raw)
        price = safe_float(row.get("price")) or 0.0
        if not code:
            warnings.append(f"无法识别报价标识，已忽略：{raw!r}")
            continue
        if price > 0:
            quotes[code] = dict(row)
    for raw in symbols:
        code = _quote_symbol_key(raw)
        if code and code not in quotes:
            warnings.append(f"{code}: 未返回可用行情")
    return quotes, errors, warnings


def _fetch_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch live Sina quotes for *symbols*, return dict keyed by 6-digit code."""
    quotes, _errors, _warnings = _fetch_prices_detailed(symbols)
    return quotes


def _merge_live_price(daily: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Overlay the live price onto daily indicator snapshot."""
    merged = dict(daily)
    price = safe_float(quote.get("price")) or 0.0
    merged["close"] = price
    merged["live_price"] = price
    open_val = safe_float(quote.get("open")) or 0.0
    if open_val > 0:
        merged["open_today"] = open_val
    high_val = safe_float(quote.get("high")) or 0.0
    if high_val > 0:
        merged["high_today"] = high_val
    low_val = safe_float(quote.get("low")) or 0.0
    if low_val > 0:
        merged["low_today"] = low_val
    prev_close = safe_float(quote.get("prev_close")) or 0.0
    if prev_close > 0:
        merged["previous_close"] = prev_close
        if price > 0:
            merged["change_pct"] = round((price - prev_close) / prev_close * 100, 2)
    return merged


# ---------------------------------------------------------------------------
# Condition evaluation wrappers
# ---------------------------------------------------------------------------

def _check_exit(
    position: dict[str, Any],
    strategy_def: dict[str, Any],
    live_data: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate exit conditions for a held position."""
    exit_conditions = list(strategy_def.get("exit_conditions") or [])
    if not exit_conditions:
        return {"triggered": False, "reason": "no_exit_conditions"}
    result = evaluate_conditions(live_data, exit_conditions, mode="any")
    triggered = result.get("triggered", False)
    return {
        "triggered": triggered,
        "matched": result.get("matched", []),
        "failed": result.get("failed", []),
        "missing": result.get("missing", []),
        "reason": "exit_conditions_met" if triggered else "conditions_not_met",
    }


def _check_entry(
    candidate_data: dict[str, Any],
    strategy_def: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate entry conditions for a candidate stock."""
    entry_conditions = list(strategy_def.get("entry_conditions") or [])
    if not entry_conditions:
        return {"triggered": False, "reason": "no_entry_conditions"}
    result = evaluate_conditions(candidate_data, entry_conditions)
    triggered = result.get("all_matched", False)
    return {
        "triggered": triggered,
        "matched": result.get("matched", []),
        "failed": result.get("failed", []),
        "missing": result.get("missing", []),
        "reason": "entry_conditions_met" if triggered else "conditions_not_met",
    }


# ---------------------------------------------------------------------------
# Single cycle
# ---------------------------------------------------------------------------

def _collect_monitor_symbols(
    portfolio: dict[str, Any],
    watchlist_items: list[dict[str, Any]],
) -> tuple[list[str], set[str], list[dict[str, Any]]]:
    """Return (all_symbols, held_symbols, watchlist_candidates)."""
    held: set[str] = set()
    for pos in portfolio.get("positions", []):
        symbol = str(pos.get("symbol") or "").zfill(6)
        shares = safe_float(pos.get("shares")) or 0
        if symbol and shares > 0:
            held.add(symbol)

    candidates: list[dict[str, Any]] = []
    for item in watchlist_items:
        symbol = str(item.get("symbol") or item.get("code") or "").zfill(6)
        if symbol and symbol not in held:
            candidates.append(item)

    all_symbols = list(held | {str(c.get("symbol") or c.get("code") or "").zfill(6) for c in candidates})
    return all_symbols, held, candidates


def run_single_cycle(
    portfolio_path: Path | str | None = None,
    strategy_name: str | None = None,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    buy_shares: int = DEFAULT_BUY_SHARES,
    position_limit_pct: float = DEFAULT_POSITION_LIMIT_PCT,
    dry_run: bool = False,
) -> CycleResult:
    """Execute one monitor cycle: fetch prices, check conditions, execute paper trades."""
    now_sh = to_shanghai_datetime()
    result = CycleResult(checked_at=now_sh.strftime("%Y-%m-%d %H:%M:%S"))

    # Check market hours
    if not is_cn_a_share_trading_day(now_sh.date()):
        result.skipped_reason = "今天不是交易日"
        result.warnings.append(result.skipped_reason)
        return result

    current_time = now_sh.time()
    if current_time < MORNING_START or current_time > AFTERNOON_END:
        result.skipped_reason = "当前不在交易时间（9:30-15:00）"
        result.warnings.append(result.skipped_reason)
        return result

    # Load state
    portfolio = load_paper_portfolio(portfolio_path)
    watchlist_data = list_watchlist()
    watchlist_items = watchlist_data.get("items", []) if isinstance(watchlist_data, dict) else []
    all_symbols, held_symbols, candidates = _collect_monitor_symbols(portfolio, watchlist_items)

    if not all_symbols:
        result.skipped_reason = "没有持仓或自选股需要监控"
        result.warnings.append(result.skipped_reason)
        return result

    result.symbols_polled = len(all_symbols)

    # Load strategies
    registry, registry_warning = load_strategy_registry()
    if registry_warning:
        result.warnings.append(registry_warning)
    default_strategy = strategy_name or "ma_breakout"
    strategy_def = registry.get(default_strategy, {})
    if not strategy_def:
        first_key = next(iter(registry), "")
        strategy_def = registry.get(first_key, {})
        if first_key:
            default_strategy = first_key

    # Fetch live prices
    try:
        quotes, price_errors, price_warnings = _fetch_prices_detailed(all_symbols)
        result.prices_fetched = len(quotes)
        result.errors.extend(price_errors)
        result.warnings.extend(price_warnings)
    except Exception as exc:
        result.errors.append(f"行情获取失败: {exc}")
        return result

    if not quotes:
        result.warnings.append("未获取到任何行情数据")
        return result

    # --- Check EXIT conditions for held positions ---
    for pos in list(portfolio.get("positions", [])):
        symbol = str(pos.get("symbol") or "").zfill(6)
        shares = safe_float(pos.get("shares")) or 0
        if not symbol or shares <= 0:
            continue
        quote = quotes.get(symbol)
        q_price = safe_float(quote.get("price")) if quote else 0.0
        if not quote or not q_price or q_price <= 0:
            continue

        daily = _load_daily_features(symbol)
        live_data = _merge_live_price(daily, quote)
        # Use position's strategy if recorded, otherwise default
        pos_strategy_name = str(pos.get("strategy") or pos.get("strategy_id") or default_strategy)
        pos_strategy_def = registry.get(pos_strategy_name, strategy_def)

        check = _check_exit(pos, pos_strategy_def, live_data)
        if check["triggered"]:
            trigger_info = {
                "action": "virtual_sell",
                "symbol": symbol,
                "name": str(pos.get("name") or ""),
                "price": q_price,
                "shares": int(shares),
                "strategy": pos_strategy_name,
                "matched_conditions": check["matched"],
                "checked_at": result.checked_at,
            }
            if not dry_run:
                sell_result = paper_sell(symbol, q_price, int(shares), path=portfolio_path)
                trigger_info["paper_result"] = {
                    "ok": sell_result.get("ok"),
                    "message": sell_result.get("message"),
                }
                if sell_result.get("ok"):
                    portfolio = sell_result.get("portfolio", portfolio)
                    # Record signal
                    signal = record_signal(
                        symbol=symbol,
                        name=str(pos.get("name") or ""),
                        strategy=pos_strategy_name,
                        signal_type="sell",
                        signal_price=q_price,
                        signal_date=result.checked_at[:10],
                    )
                    append_signal(signal, SIGNAL_STORE_PATH)
            result.exit_triggers.append(trigger_info)

    # --- Check ENTRY conditions for watchlist candidates ---
    current_position_count = len([
        p for p in portfolio.get("positions", [])
        if (safe_float(p.get("shares")) or 0) > 0
    ])
    account = portfolio.get("account", {})
    cash = safe_float(account.get("cash")) or 0.0
    initial_cash = safe_float(account.get("initial_cash")) or 10000.0

    for candidate in candidates:
        if current_position_count >= max_positions:
            result.warnings.append(f"持仓数已达上限 {max_positions}，跳过剩余候选")
            break

        symbol = str(candidate.get("symbol") or candidate.get("code") or "").zfill(6)
        quote = quotes.get(symbol)
        q_price = safe_float(quote.get("price")) if quote else 0.0
        if not quote or not q_price or q_price <= 0:
            continue
        q_name = str(quote.get("name") or "")

        # Position size check
        position_value = q_price * buy_shares
        if position_value > cash:
            result.warnings.append(f"{symbol} 资金不足: 需要 {position_value:.0f}, 可用 {cash:.0f}")
            continue
        if position_value > initial_cash * position_limit_pct:
            result.warnings.append(f"{symbol} 超过单只持仓比例上限 {position_limit_pct*100:.0f}%")
            continue

        daily = _load_daily_features(symbol)
        live_data = _merge_live_price(daily, quote)
        # Merge candidate metadata into live_data for condition evaluation
        for key in ("data_reliability_score", "trust_score", "activity_ratio_20",
                     "activity_value", "activity_avg_20", "ma5", "ma10", "ma20", "ma60",
                     "rsi14", "macd_dif", "macd_dea", "macd_histogram",
                     "boll_upper", "boll_lower", "vol20", "high_20d",
                     "previous_macd_histogram"):
            if key not in live_data and key in candidate:
                val = safe_float(candidate.get(key))
                if val is not None:
                    live_data[key] = val

        check = _check_entry(live_data, strategy_def)
        if check["triggered"]:
            trigger_info = {
                "action": "virtual_buy",
                "symbol": symbol,
                "name": str(candidate.get("name") or q_name or ""),
                "price": q_price,
                "shares": buy_shares,
                "strategy": default_strategy,
                "matched_conditions": check["matched"],
                "checked_at": result.checked_at,
            }
            if not dry_run:
                buy_result = paper_buy(
                    symbol=symbol,
                    name=str(candidate.get("name") or q_name or ""),
                    price=q_price,
                    shares=buy_shares,
                    path=portfolio_path,
                    metadata={"strategy": default_strategy, "monitor_entry": True},
                )
                trigger_info["paper_result"] = {
                    "ok": buy_result.get("ok"),
                    "message": buy_result.get("message"),
                }
                if buy_result.get("ok"):
                    portfolio = buy_result.get("portfolio", portfolio)
                    current_position_count += 1
                    cash -= position_value
                    signal = record_signal(
                        symbol=symbol,
                        name=str(candidate.get("name") or q_name or ""),
                        strategy=default_strategy,
                        signal_type="buy",
                        signal_price=q_price,
                        signal_date=result.checked_at[:10],
                    )
                    append_signal(signal, SIGNAL_STORE_PATH)
            result.entry_triggers.append(trigger_info)

    # Update paper marks with latest prices
    if not dry_run and quotes:
        price_map = {
            code: {"price": safe_float(q.get("price")), "name": str(q.get("name") or "")}
            for code, q in quotes.items()
            if (safe_float(q.get("price")) or 0) > 0
        }
        portfolio = update_paper_marks(portfolio, price_map)
        save_paper_portfolio(portfolio, portfolio_path)

    # Persist cycle log
    _append_cycle_log(result)

    return result


# ---------------------------------------------------------------------------
# Cycle log persistence
# ---------------------------------------------------------------------------

def _append_cycle_log(cycle: CycleResult) -> None:
    """Append cycle result to monitor log (keep last 200 entries)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if MONITOR_LOG_PATH.exists():
        try:
            entries = json.loads(MONITOR_LOG_PATH.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []
    entries.append(cycle.to_dict())
    entries = entries[-200:]
    MONITOR_LOG_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_monitor_log(limit: int = 50) -> list[dict[str, Any]]:
    """Load recent monitor cycle log entries."""
    if not MONITOR_LOG_PATH.exists():
        return []
    try:
        entries = json.loads(MONITOR_LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return []
        return entries[-limit:]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Blocking loop (for CLI and background thread)
# ---------------------------------------------------------------------------

_monitor_thread: threading.Thread | None = None
_monitor_stop_event = threading.Event()
_monitor_status: dict[str, Any] = {"running": False}


def run_monitor_loop(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    strategy_name: str | None = None,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    buy_shares: int = DEFAULT_BUY_SHARES,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    """Blocking loop: runs ``run_single_cycle`` every *interval_seconds*.

    Stops when the market closes (after 15:00 CST) or *stop_event* is set.
    """
    stop = stop_event or _monitor_stop_event
    _monitor_status["running"] = True
    _monitor_status["started_at"] = to_shanghai_datetime().strftime("%Y-%m-%d %H:%M:%S")
    _monitor_status["interval_seconds"] = interval_seconds
    _monitor_status["strategy"] = strategy_name or "ma_breakout"
    _monitor_status["cycle_count"] = 0
    _monitor_status["last_error"] = ""

    logger.info("Intraday monitor started: interval=%ds strategy=%s", interval_seconds, strategy_name)

    try:
        while not stop.is_set():
            now_sh = to_shanghai_datetime()
            # Stop after market close
            if now_sh.time() > AFTERNOON_END:
                logger.info("Market closed, stopping monitor.")
                break
            # Wait until market opens
            if now_sh.time() < MORNING_START:
                wait_seconds = min(60, interval_seconds)
                stop.wait(wait_seconds)
                continue
            if not is_cn_a_share_trading_day(now_sh.date()):
                logger.info("Not a trading day, stopping monitor.")
                break

            try:
                cycle = run_single_cycle(
                    strategy_name=strategy_name,
                    max_positions=max_positions,
                    buy_shares=buy_shares,
                    dry_run=dry_run,
                )
                _monitor_status["cycle_count"] += 1
                _monitor_status["last_cycle"] = cycle.to_dict()
                _monitor_status["last_error"] = ""
                if cycle.exit_triggers or cycle.entry_triggers:
                    logger.info(
                        "Cycle %d: %d exits, %d entries",
                        _monitor_status["cycle_count"],
                        len(cycle.exit_triggers),
                        len(cycle.entry_triggers),
                    )
            except Exception as exc:
                logger.warning("Cycle error: %s", exc)
                _monitor_status["last_error"] = str(exc)

            stop.wait(interval_seconds)
    finally:
        _monitor_status["running"] = False
        _monitor_status["stopped_at"] = to_shanghai_datetime().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("Intraday monitor stopped.")


def start_monitor_background(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    strategy_name: str | None = None,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    buy_shares: int = DEFAULT_BUY_SHARES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Start the monitor loop in a background thread."""
    global _monitor_thread
    if _monitor_status.get("running"):
        return {"ok": False, "message": "盘中监控已在运行中", "status": dict(_monitor_status)}

    _monitor_stop_event.clear()
    _monitor_thread = threading.Thread(
        target=run_monitor_loop,
        kwargs={
            "interval_seconds": interval_seconds,
            "strategy_name": strategy_name,
            "max_positions": max_positions,
            "buy_shares": buy_shares,
            "dry_run": dry_run,
            "stop_event": _monitor_stop_event,
        },
        daemon=True,
    )
    _monitor_thread.start()
    return {"ok": True, "message": "盘中监控已启动", "disclaimer": MONITOR_DISCLAIMER}


def stop_monitor_background() -> dict[str, Any]:
    """Signal the background monitor to stop."""
    if not _monitor_status.get("running"):
        return {"ok": False, "message": "盘中监控未在运行"}
    _monitor_stop_event.set()
    return {"ok": True, "message": "盘中监控正在停止…"}


def get_monitor_status() -> dict[str, Any]:
    """Return current monitor status."""
    return {
        **_monitor_status,
        "disclaimer": MONITOR_DISCLAIMER,
        "log_entries": load_monitor_log(limit=10),
    }


__all__ = [
    "CycleResult",
    "MONITOR_DISCLAIMER",
    "run_single_cycle",
    "run_monitor_loop",
    "start_monitor_background",
    "stop_monitor_background",
    "get_monitor_status",
    "load_monitor_log",
]
