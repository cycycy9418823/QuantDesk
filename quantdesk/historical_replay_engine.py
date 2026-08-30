"""Historical intraday replay for frozen conditional plans.

Historical replay is separate from the live automatic paper portfolio. It uses
walk-forward dates: T-day daily data creates a T+1 plan, and only T+1
intraday bars are used to verify triggers.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols
from quantdesk.intraday_data_policy import assert_no_partial_daily_for_research
from quantdesk.paper.conditional_trade_plan import build_conditional_trade_plan
from quantdesk.paper.position_management_engine import (
    apply_conditional_execution_to_state,
    simulate_conditional_plan_file,
)
from quantdesk.price_basis import (
    PRICE_BASIS_RAW,
    PRICE_BASIS_UNKNOWN,
    classify_frame_price_basis,
    observed_price_basis_labels,
    validate_price_frame_set,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, truthy, write_json
from quantdesk.trading_calendar import calendar_info, next_trade_day, trade_days_between
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"
UNIVERSE_PATH = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
STATE_DIR = PROJECT_ROOT / "state"

HISTORICAL_REPLAY_STATE_JSON = STATE_DIR / "historical_replay_account.json"
HISTORICAL_REPLAY_LATEST_JSON = RESULTS_DIR / "historical_intraday_replay_latest.json"

DISCLAIMER = "本报告为本地纸面历史回放，不接券商、不真实下单、不构成投资建议。"
NO_FAKE_FILL_NOTE = "系统不会用未来数据生成历史计划，也不会用日线收盘价、最高价或最低价假装盘中成交。"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _read_low_price_symbols(path: Path = UNIVERSE_PATH, csv_read_warnings: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        return [], {}
    rows = read_csv_frame(path, dtype=str).fillna("")
    if csv_read_warnings is not None:
        csv_read_warnings.extend(f"low_price_universe_csv:{item}" for item in rows.attrs.get("warnings") or [])
    symbols: list[str] = []
    names: dict[str, str] = {}
    for _, row in rows.iterrows():
        if not truthy(row.get("included")):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码"))
        if not symbol:
            continue
        symbols.append(symbol)
        names[symbol] = str(row.get("name") or row.get("名称") or symbol)
    return list(dict.fromkeys(symbols)), names


def _read_daily(symbol: str, daily_dir: Path = DAILY_DIR) -> pd.DataFrame:
    assert_no_partial_daily_for_research(daily_dir, caller="historical_replay_engine")
    path = daily_dir / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = read_csv_frame(path)
    csv_read_warnings = list(df.attrs.get("warnings") or [])
    rename = {
        "日期": "date",
        "交易日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    df = df.rename(columns={col: rename.get(str(col), str(col)) for col in df.columns}).copy()
    df = df.loc[:, ~df.columns.duplicated()]
    observed_basis, basis_reason = classify_frame_price_basis(df)
    if "date" not in df.columns or "close" not in df.columns:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings + [f"historical_replay_daily_required_columns_missing:{normalize_symbol(symbol)}"]
        return empty
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("open", "high", "low"):
        if col not in df.columns:
            df[col] = df["close"]
    out = df.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    out.attrs["warnings"] = csv_read_warnings
    out.attrs["observed_price_basis"] = observed_basis
    out.attrs["price_basis_reason"] = basis_reason
    return out


def _resolve_symbols(symbols: list[str] | None, *, only_low_price: bool, limit_symbols: int, daily_dir: Path, warnings: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
    explicit = [normalize_symbol(item) for item in symbols or [] if normalize_symbol(item)]
    low_symbols, names = _read_low_price_symbols(csv_read_warnings=warnings)
    if explicit:
        selected = explicit
    elif only_low_price:
        selected = low_symbols
    else:
        selected = [normalize_symbol(path.stem) for path in sorted(daily_dir.glob("*.csv"))]
    selected = [item for item in dict.fromkeys(selected) if item]
    if limit_symbols and limit_symbols > 0:
        selected = selected[:limit_symbols]
    for symbol in selected:
        names.setdefault(symbol, symbol)
    return selected, names


def slice_history_until_signal_date(frame: pd.DataFrame, signal_date: str, *, symbol: str = "") -> tuple[pd.DataFrame, list[str]]:
    """Return an in-memory daily slice capped at ``signal_date``.

    This is the replay safety boundary: a signal date can only see rows whose
    daily date is less than or equal to that signal date.
    """

    normalized = normalize_symbol(symbol) if symbol else ""
    prefix = f"{normalized}: " if normalized else ""
    df = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    warnings: list[str] = list(df.attrs.get("warnings") or [])
    if df.empty:
        return pd.DataFrame(), [f"{prefix}missing local daily data; historical replay skipped this symbol."]
    cutoff = pd.to_datetime(signal_date, errors="coerce")
    if pd.notna(cutoff):
        future_rows = df[df["date"] > cutoff]
        if not future_rows.empty:
            warnings.append(
                f"{prefix}daily history contains rows after {signal_date}; replay truncated in memory "
                "to avoid look-ahead（已按 signal_date 截断，避免未来函数）."
            )
        df = df[df["date"] <= cutoff]
    if df.empty:
        warnings.append(f"{prefix}no usable daily rows remain after truncating to signal_date.")
    return df.reset_index(drop=True), warnings


def get_daily_slice(symbol: str, signal_date: str, *, source_dir: Path = DAILY_DIR) -> tuple[pd.DataFrame, list[str]]:
    """Load one symbol and return an in-memory daily slice capped at ``signal_date``."""

    normalized = normalize_symbol(symbol)
    return slice_history_until_signal_date(_read_daily(normalized, source_dir), signal_date, symbol=normalized)


def _load_daily_histories(symbols: list[str], *, source_dir: Path = DAILY_DIR) -> tuple[dict[str, pd.DataFrame], list[str]]:
    histories: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        df = _read_daily(normalized, source_dir)
        if df.empty:
            warnings.append(f"{normalized}: missing local daily data; historical replay skipped this symbol.")
            continue
        histories[normalized] = df
    return histories, warnings


def _prepare_signal_daily_cache(
    symbols: list[str],
    signal_date: str,
    *,
    source_dir: Path = DAILY_DIR,
    histories: dict[str, pd.DataFrame] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    warnings: list[str] = []
    available: list[str] = []
    cache: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if histories is not None:
            if normalized not in histories:
                continue
            df, symbol_warnings = slice_history_until_signal_date(histories[normalized], signal_date, symbol=normalized)
        else:
            df, symbol_warnings = get_daily_slice(normalized, signal_date, source_dir=source_dir)
        warnings.extend(symbol_warnings)
        if not df.empty:
            cache[normalized] = df
            available.append(normalized)
    return cache, available, warnings

def _latest_close(df: pd.DataFrame) -> float:
    if df.empty or "close" not in df.columns:
        return 0.0
    return float(safe_float(df["close"].iloc[-1], 0.0) or 0.0)


def _strategy_snapshot(
    symbols: list[str],
    names: dict[str, str],
    signal_date: str,
    *,
    strategy: str,
    daily_dir: Path,
    daily_cache: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    long_candidates: list[dict[str, Any]] = []
    board_candidates: list[dict[str, Any]] = []
    for rank, symbol in enumerate(symbols, start=1):
        normalized = normalize_symbol(symbol)
        df = (daily_cache or {}).get(normalized)
        if df is None:
            df = _read_daily(normalized, daily_dir)
        if df.empty:
            continue
        close = _latest_close(df)
        row = {
            "symbol": symbol,
            "name": names.get(symbol, symbol),
            "strategy_type": "board" if strategy == "board" else "long",
            "strategy_label": "打板" if strategy == "board" else "长线",
            "candidate_source": "historical_intraday_replay",
            "rank": rank,
            "score": 0,
            "reason": "历史回放使用 signal_date 及之前本地日线生成冻结条件计划。",
            "close": close,
            "latest_price": close,
            "data_latest_date": signal_date,
            "is_target_date_covered": True,
            "data_freshness_label": "历史 signal_date 候选",
        }
        if strategy == "board":
            board_candidates.append(row)
        else:
            long_candidates.append(row)
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": now_text(),
        "mode": "historical_replay",
        "strategy": strategy,
        "latest_daily_date": signal_date,
        "long_candidates": long_candidates,
        "board_candidates": board_candidates,
        "standard_candidates": long_candidates + board_candidates,
        "summary": {
            "long_candidate_count": len(long_candidates),
            "board_candidate_count": len(board_candidates),
            "latest_daily_date": signal_date,
        },
        "disclaimer": DISCLAIMER,
    }


def _initial_account(initial_cash: float) -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "account_type": "historical_intraday_replay",
        "initial_cash": float(initial_cash),
        "cash": float(initial_cash),
        "positions": [],
        "closed_positions": [],
        "applied_execution_ids": [],
        "disclaimer": DISCLAIMER,
    }


def _build_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# 历史分钟线回放报告",
        "",
        DISCLAIMER,
        "",
        NO_FAKE_FILL_NOTE,
        "",
        "## 回放摘要",
        "",
        f"- 回放区间：{payload.get('start_date')} 至 {payload.get('end_date')}",
        f"- 交易日历质量：{summary.get('calendar_quality', '-')}",
        f"- calendar_coverage_warning: {summary.get('calendar_coverage_warning', '') or '-'}",
        f"- 交易日数量：{summary.get('trade_day_count', 0)}",
        f"- 计划数量：{summary.get('plan_count', 0)}",
        f"- 有分钟线股票数：{summary.get('available_intraday_count', 0)}",
        f"- 已验证分钟池命中次数：{summary.get('verified_completed_intraday_count', 0)}",
        "- 已验证分钟池可先用于覆盖内股票，覆盖外仍按本地未验证或缺分钟线处理。",
        f"- 本地未验证分钟线命中次数：{summary.get('local_unverified_intraday_count', 0)}",
        f"- 缺分钟线次数：{summary.get('missing_intraday_count', 0)}",
        f"- 可靠成交数量：{summary.get('reliable_event_count', 0)}",
        f"- 未成交/未触发数量：{summary.get('no_fill_count', 0)}",
        f"- 总收益率：{summary.get('total_return_pct', 0)}%",
        f"- 是否使用 provider：{'是' if payload.get('use_intraday_provider') else '否'}",
        "- 是否更新当前自动虚拟组合：否",
        "",
        "## Walk-forward 防未来函数",
        "",
        "历史回放采用 walk-forward：每一天只使用当日收盘前已知信息生成下一交易日计划，再用下一交易日分钟线验证触发。缺少分钟线时不会用日线价格假装成交。",
        "",
        "## 每日 signal_date / trade_date",
        "",
    ]
    for item in payload.get("daily_results") or []:
        es = item.get("execution_summary") or {}
        lines.extend([
            f"### {item.get('signal_date')} -> {item.get('trade_date')}",
            "",
            f"- source_data_cutoff：{item.get('source_data_cutoff')}",
            f"- anti_future_leak_check：{item.get('anti_future_leak_check')}",
            f"- 每日计划数量：{item.get('plan_count', 0)}",
            f"- 每日分钟线覆盖：{es.get('available_intraday_count', 0)} / {item.get('plan_count', 0)}",
            f"- 已验证分钟池：{es.get('verified_completed_intraday_count', 0)}",
            f"- 本地未验证分钟线：{es.get('local_unverified_intraday_count', 0)}",
            f"- 每日触发事件：{es.get('event_count', 0)}",
            f"- 缺分钟线次数：{es.get('missing_intraday_count', 0)}",
            f"- 未成交/未触发数量：{es.get('no_fill_count', 0)}",
            "",
        ])
    lines.extend([
        "## 风险提示",
        "",
        "- 本地分钟线可能缺失、延迟或字段变化。",
        "- provider 失败时只记录失败原因，不会假装成交。",
        "- 本报告仅用于策略学习观察，不接券商、不真实下单、不构成投资建议。",
        "",
    ])
    return "\n".join(lines)


def run_historical_intraday_replay(
    start_date: str,
    end_date: str,
    *,
    strategy: str = "all",
    symbols: list[str] | None = None,
    only_low_price: bool = True,
    initial_cash: float = 100000,
    use_intraday_provider: bool = False,
    network: bool = False,
    allow_imported_intraday: bool = True,
    update_state: bool = False,
    dry_run: bool = False,
    max_days: int = 0,
    limit_symbols: int = 0,
    daily_dir: Path | None = None,
    intraday_dir: Path | None = None,
    output_path: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    daily_dir = Path(daily_dir or DAILY_DIR)
    intraday_dir = Path(intraday_dir or INTRADAY_DIR)
    assert_no_partial_daily_for_research(daily_dir, caller="historical_replay_engine")
    output_path = Path(output_path or HISTORICAL_REPLAY_LATEST_JSON)
    state_path = Path(state_path or HISTORICAL_REPLAY_STATE_JSON)
    cal = calendar_info(start_date=start_date, end_date=end_date)
    trade_days = trade_days_between(start_date, end_date)
    if max_days and max_days > 0:
        trade_days = trade_days[:max_days]
    warnings: list[str] = []
    selected_symbols, names = _resolve_symbols(symbols, only_low_price=only_low_price, limit_symbols=limit_symbols, daily_dir=daily_dir, warnings=warnings)
    account = _initial_account(initial_cash)
    daily_results: list[dict[str, Any]] = []
    provider_results: list[dict[str, Any]] = []
    daily_histories, load_warnings = _load_daily_histories(selected_symbols, source_dir=daily_dir)
    warnings.extend(load_warnings)
    price_policy_validation = validate_price_frame_set(
        daily_histories,
        consumer="historical_intraday_replay",
    )
    if not price_policy_validation.get("eligible"):
        reason_codes = [
            str(item) for item in price_policy_validation.get("reason_codes") or []
        ]
        blocked_summary = {
            "calendar_quality": cal.get("calendar_quality"),
            "calendar_coverage_warning": cal.get("coverage_warning", ""),
            "trade_day_count": len(trade_days),
            "processed_symbol_count": len(selected_symbols),
            "daily_history_loaded_symbol_count": len(daily_histories),
            "daily_slice_mode": "in_memory_preloaded",
            "daily_history_load_mode": "preload_once",
            "plan_count": 0,
            "event_count": 0,
            "reliable_event_count": 0,
            "no_fill_count": 0,
            "missing_intraday_count": 0,
            "available_intraday_count": 0,
            "verified_completed_intraday_count": 0,
            "local_unverified_intraday_count": 0,
            "cash": account.get("cash", initial_cash),
            "total_equity": account.get("total_equity", initial_cash),
            "total_return_pct": account.get("total_return_pct", 0),
        }
        return {
            "ok": False,
            "status": "blocked",
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "task_type": "historical_intraday_replay",
            "generated_at": now_text(),
            "start_date": str(start_date)[:10],
            "end_date": str(end_date)[:10],
            "strategy": strategy,
            "scope": "low_price_included_only" if only_low_price else "selected_symbols",
            "only_low_price": bool(only_low_price),
            "network": False,
            "use_intraday_provider": bool(use_intraday_provider),
            "allow_imported_intraday": bool(allow_imported_intraday),
            "update_state": bool(update_state),
            "updated_current_auto_portfolio": False,
            "dry_run": bool(dry_run),
            "calendar": cal,
            "trade_days": trade_days,
            "symbols": selected_symbols,
            "daily_results": [],
            "provider_results": [],
            "daily_history_loaded_symbol_count": len(daily_histories),
            "historical_account": account,
            "summary": blocked_summary,
            "price_policy_validation": price_policy_validation,
            **observed_price_basis_labels(
                PRICE_BASIS_UNKNOWN,
                reason_codes=reason_codes,
            ),
            "execution_price_basis": PRICE_BASIS_UNKNOWN,
            "execution_price_role": "blocked_before_plan",
            "uses_raw_execution_price": False,
            "real_execution_price_claim": False,
            "warnings": list(
                dict.fromkeys(
                    [
                        *warnings,
                        "price policy validation blocked replay before plan construction:",
                        *reason_codes,
                    ]
                )
            ),
            "errors": ["price_policy_validation_blocked"],
            "anti_future_leak_check": "blocked_price_policy",
            "disclaimer": DISCLAIMER,
            "no_fake_fill_note": NO_FAKE_FILL_NOTE,
        }
    if use_intraday_provider and not network:
        warnings.append("use_intraday_provider requested but network=false; provider fetch skipped.")
    fast_dry_run_smoke = bool(dry_run and max_days and len(selected_symbols) >= 50 and not use_intraday_provider and not update_state)
    for signal_date in trade_days:
        trade_date = next_trade_day(signal_date)
        signal_daily_cache, available_symbols, day_warnings = _prepare_signal_daily_cache(
            selected_symbols,
            signal_date,
            source_dir=daily_dir,
            histories=daily_histories,
        )
        warnings.extend(day_warnings)
        strategy_payload = _strategy_snapshot(
            available_symbols,
            names,
            signal_date,
            strategy=strategy,
            daily_dir=daily_dir,
            daily_cache=signal_daily_cache,
        )
        if fast_dry_run_smoke:
            plan_symbols = list(strategy_payload.get("standard_candidates") or [])
            plan = {
                "plan_id": f"historical-replay-dry-run-{signal_date}",
                "symbols": plan_symbols,
                "warnings": ["dry_run_fast_path_skipped_execution_simulation"],
            }
            execution = {
                "summary": {
                    "event_count": 0,
                    "reliable_event_count": 0,
                    "no_fill_count": len(plan_symbols),
                    "missing_intraday_count": len(plan_symbols),
                    "available_intraday_count": 0,
                }
            }
            day_warnings = list(day_warnings) + ["dry_run_fast_path_skipped_execution_simulation"]
        else:
            plan = build_conditional_trade_plan(
                auto_strategy_payload=strategy_payload,
                state=account,
                strategy=strategy,
                plan_date=signal_date,
                trade_date=trade_date,
                source_data_cutoff=signal_date,
                write=False,
                daily_dir=daily_dir,
                daily_cache=signal_daily_cache,
                low_price_universe_path=UNIVERSE_PATH if only_low_price else PROJECT_ROOT / "__historical_replay_no_universe_filter__.csv",
            )
            if use_intraday_provider and network and plan.get("symbols"):
                update = update_intraday_1m_for_symbols(
                    symbols=[row.get("symbol") for row in plan.get("symbols") or [] if isinstance(row, dict)],
                    target_date=trade_date,
                    source="symbols",
                    dry_run=bool(dry_run),
                    network=True,
                    execute_after_update=False,
                    update_portfolio_after_execution=False,
                )
                provider_results.append({
                    "signal_date": signal_date,
                    "trade_date": trade_date,
                    "summary": {
                        "status": update.get("status"),
                        "updated_symbols": update.get("updated_symbols"),
                        "failed_symbols": update.get("failed_symbols"),
                    },
                    "provider_results": update.get("provider_results") or [],
                })
            execution = simulate_conditional_plan_file(
                plan,
                intraday_dir=intraday_dir if allow_imported_intraday else PROJECT_ROOT / "__missing_intraday__",
                write=False,
            )
            applied = apply_conditional_execution_to_state(account, execution, daily_dir=daily_dir, allow_same_day_rerun=True)
            if isinstance(applied.get("state"), dict):
                account = applied["state"]
        day_result = {
            "signal_date": signal_date,
            "trade_date": trade_date,
            "source_data_cutoff": signal_date,
            "plan_id": plan.get("plan_id"),
            "plan_count": len(plan.get("symbols") or []),
            "execution_summary": execution.get("summary") or {},
            "account_summary": {
                "cash": account.get("cash"),
                "total_equity": account.get("total_equity"),
                "total_return_pct": account.get("total_return_pct"),
                "position_count": len(account.get("positions") or []),
            },
            "anti_future_leak_check": "passed",
            "warnings": day_warnings,
        }
        daily_results.append(day_result)
    summary = {
        "calendar_quality": cal.get("calendar_quality"),
        "calendar_coverage_warning": cal.get("coverage_warning", ""),
        "trade_day_count": len(trade_days),
        "processed_symbol_count": len(selected_symbols),
        "daily_history_loaded_symbol_count": len(daily_histories),
        "daily_slice_mode": "in_memory_preloaded",
        "daily_history_load_mode": "preload_once",
        "plan_count": sum(int(row.get("plan_count") or 0) for row in daily_results),
        "event_count": sum(int((row.get("execution_summary") or {}).get("event_count") or 0) for row in daily_results),
        "reliable_event_count": sum(int((row.get("execution_summary") or {}).get("reliable_event_count") or 0) for row in daily_results),
        "no_fill_count": sum(int((row.get("execution_summary") or {}).get("no_fill_count") or 0) for row in daily_results),
        "missing_intraday_count": sum(int((row.get("execution_summary") or {}).get("missing_intraday_count") or 0) for row in daily_results),
        "available_intraday_count": sum(int((row.get("execution_summary") or {}).get("available_intraday_count") or 0) for row in daily_results),
        "verified_completed_intraday_count": sum(int((row.get("execution_summary") or {}).get("verified_completed_intraday_count") or 0) for row in daily_results),
        "local_unverified_intraday_count": sum(int((row.get("execution_summary") or {}).get("local_unverified_intraday_count") or 0) for row in daily_results),
        "cash": account.get("cash", initial_cash),
        "total_equity": account.get("total_equity", initial_cash),
        "total_return_pct": account.get("total_return_pct", 0),
    }
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "historical_intraday_replay",
        "generated_at": now_text(),
        "start_date": str(start_date)[:10],
        "end_date": str(end_date)[:10],
        "strategy": strategy,
        "scope": "low_price_included_only" if only_low_price else "selected_symbols",
        "only_low_price": bool(only_low_price),
        "full_scan": False,
        "network": bool(use_intraday_provider and network),
        "use_intraday_provider": bool(use_intraday_provider),
        "allow_imported_intraday": bool(allow_imported_intraday),
        "update_state": bool(update_state),
        "updated_current_auto_portfolio": False,
        "dry_run": bool(dry_run),
        "calendar": cal,
        "trade_days": trade_days,
        "symbols": selected_symbols,
        "daily_results": daily_results,
        "provider_results": provider_results,
        "daily_slice_mode": "in_memory_preloaded",
        "daily_history_load_mode": "preload_once",
        "daily_history_loaded_symbol_count": len(daily_histories),
        "historical_account": account,
        "summary": summary,
        "price_policy_validation": price_policy_validation,
        **observed_price_basis_labels(PRICE_BASIS_RAW),
        "execution_price_basis": PRICE_BASIS_RAW,
        "execution_price_role": "raw_minute_close_paper_execution",
        "uses_raw_execution_price": True,
        "real_execution_price_claim": False,
        "warnings": warnings,
        "errors": [],
        "anti_future_leak_check": "passed",
        "disclaimer": DISCLAIMER,
        "no_fake_fill_note": NO_FAKE_FILL_NOTE,
    }
    if not dry_run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"historical_intraday_replay_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        payload["report_path"] = _display_path(report_path)
        payload["latest_path"] = _display_path(output_path)
        report_path.write_text(_build_report(payload), encoding="utf-8")
        write_json(output_path, payload)
        write_json(state_path, account)
    return payload


def historical_replay_latest(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or HISTORICAL_REPLAY_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未生成历史分钟线回放结果。",
            "disclaimer": DISCLAIMER,
        }
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload


def historical_replay_status(path: Path | None = None) -> dict[str, Any]:
    latest = historical_replay_latest(path)
    if not latest.get("exists"):
        return latest
    summary = latest.get("summary") or {}
    return {
        "ok": True,
        "exists": True,
        "path": latest.get("path"),
        "generated_at": latest.get("generated_at"),
        "start_date": latest.get("start_date"),
        "end_date": latest.get("end_date"),
        "trade_day_count": summary.get("trade_day_count", 0),
        "plan_count": summary.get("plan_count", 0),
        "event_count": summary.get("event_count", 0),
        "missing_intraday_count": summary.get("missing_intraday_count", 0),
        "verified_completed_intraday_count": summary.get("verified_completed_intraday_count", 0),
        "local_unverified_intraday_count": summary.get("local_unverified_intraday_count", 0),
        "total_return_pct": summary.get("total_return_pct", 0),
        "anti_future_leak_check": latest.get("anti_future_leak_check"),
        "disclaimer": DISCLAIMER,
    }


def newest_historical_replay_report() -> Path | None:
    if not REPORTS_DIR.exists():
        return None
    rows = sorted(REPORTS_DIR.glob("historical_intraday_replay_report_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return rows[0] if rows else None


def historical_replay_report_html() -> str:
    latest = historical_replay_latest()
    report_path = None
    if latest.get("report_path"):
        candidate = PROJECT_ROOT / str(latest.get("report_path"))
        if candidate.exists():
            report_path = candidate
    report_path = report_path or newest_historical_replay_report()
    if report_path and report_path.exists():
        body = html.escape(report_path.read_text(encoding="utf-8"))
        meta = html.escape(str(report_path))
    else:
        body = "尚未生成历史分钟线回放报告。"
        meta = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>历史分钟线回放报告</title>
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
    <div class="toolbar"><div><h1>历史分钟线回放报告</h1><div class="meta">{meta}</div></div><button type="button" onclick="window.print()">HTML 预览 / 浏览器另存为 PDF</button></div>
    <div class="disclaimer">{html.escape(DISCLAIMER)}</div>
    <pre>{body}</pre>
  </main>
</body>
</html>"""


__all__ = [
    "HISTORICAL_REPLAY_LATEST_JSON",
    "HISTORICAL_REPLAY_STATE_JSON",
    "historical_replay_latest",
    "historical_replay_report_html",
    "historical_replay_status",
    "run_historical_intraday_replay",
]
