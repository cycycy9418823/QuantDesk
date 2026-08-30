from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    pd.options.future.infer_string = False
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk import explanations as explanation_text  # noqa: E402
# Round 21 Part E: this legacy entrypoint doubles as a compatibility shim;
# `X as X` marks names as intentional re-exports for external callers.
from quantdesk.backtest import (  # noqa: E402
    BacktestConfig,
    BacktestMetrics,
    PortfolioBacktestConfig,
    backtest_cost_warning,
    backtest_signal_frame as backtest_signal_frame,
    benchmark_total_return as benchmark_total_return,
    default_backtest_assumptions,
    export_backtest_result,
    backtest as backtest,
    load_benchmark_series,
    simulate_backtest_result,
    simulate_single_asset_backtest as simulate_single_asset_backtest,
    simulate_portfolio_backtest,
    summary_rows_from_metrics,
)
from quantdesk.config import load_config  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.data_providers import (  # noqa: E402
    fetch_daily_eastmoney,
    fetch_daily_sina,
    fetch_news_eastmoney,
    fetch_spot_eastmoney,
    fetch_spot_sina,
)
from quantdesk.manual_confirmation_queue import list_manual_confirmation_items  # noqa: E402
from quantdesk.market_clock import (  # noqa: E402
    expected_settlement_date,
    load_cn_trading_calendar,
    previous_cn_a_share_trading_day,
    settlement_status_from_quote,
)
from quantdesk.news_status import (  # noqa: E402
    NewsFetchStatus,
    provider_not_configured_status,
    sample_mode_status as sample_mode_status,
    summarize_news_health_rows,
)
from quantdesk.news_status import (
    failure_status as news_failure_status,
)
from quantdesk.news_status import (
    frame_from_statuses as news_frame_from_statuses,
)
from quantdesk.news_status import (
    status_from_items as news_status_from_items,
)
from quantdesk.paper.paper_trading import load_paper_portfolio  # noqa: E402
from quantdesk.price_basis import validate_price_frame_set  # noqa: E402
from quantdesk.reporting import (  # noqa: E402
    build_backtest_audit_markdown,
    build_data_quality_markdown,
    fmt_pct,
    fmt_value,
    risk_summary_dict,
    risk_summary_note_lines,
    table_or_message,
)
from quantdesk.utils import atomic_write_json, read_json_safe, safe_float  # noqa: E402
from quantdesk.watchlist import list_watchlist  # noqa: E402
from quantdesk.word_report import (
    add_indicator_table as _word_add_indicator_table,
)
from quantdesk.word_report import (
    add_trade_checklist_table as _word_add_trade_checklist_table,
)
from quantdesk.word_report import (
    build_word_report as _build_word_report,
)

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DAILY_DIR = DATA_DIR / "daily"
NEWS_DIR = DATA_DIR / "news"
CACHE_DIR = DATA_DIR / "cache"
STATE_DIR = DATA_DIR / "state"
REPORT_DIR = PROJECT_ROOT / "reports"
LOG_DIR = PROJECT_ROOT / "logs"
LONG_POOL_PATH = STATE_DIR / "long_pool.csv"
SPOT_CACHE_CSV = CACHE_DIR / "spot_snapshot_latest.csv"
SPOT_CACHE_META = CACHE_DIR / "spot_snapshot_meta.json"
DEFAULT_FALLBACK_SYMBOLS = ["600000", "000001", "600519", "300750"]

NEWS_POSITIVE_KEYWORDS = [
    "中标",
    "签约",
    "订单",
    "合作",
    "增持",
    "回购",
    "业绩预增",
    "扭亏",
    "获批",
    "突破",
    "涨价",
    "扩产",
]
NEWS_NEGATIVE_KEYWORDS = [
    "减持",
    "立案",
    "处罚",
    "问询",
    "亏损",
    "下滑",
    "退市",
    "退市风险",
    "重大风险",
    "诉讼",
    "质押",
    "解禁",
    "监管",
    "终止",
]
NEWS_HEAT_KEYWORDS = [
    "涨停",
    "连板",
    "龙虎榜",
    "异动",
    "大涨",
    "爆发",
    "涨幅偏离",
    "封板",
    "开板",
]


NUMERIC_SPOT_COLUMNS = [
    "最新价",
    "涨跌幅",
    "涨跌额",
    "成交量",
    "成交额",
    "振幅",
    "最高",
    "最低",
    "今开",
    "昨收",
    "量比",
    "换手率",
    "市盈率-动态",
    "市净率",
    "总市值",
    "流通市值",
]

NUMERIC_DAILY_COLUMNS = [
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
]

# Data-health recorder was moved to quantdesk/data_health_recorder.py in
# v2.6.4-data-health-recorder-split.  The following names are re-exported
# here so existing scripts and tests that import them from
# ``scripts.daily_quant_report`` continue to work unchanged.
from quantdesk.data_health_recorder import (  # noqa: E402
    DATA_HEALTH_COLUMNS as DATA_HEALTH_COLUMNS,
    DATA_HEALTH_ROWS as DATA_HEALTH_ROWS,
    DATA_MIN_HISTORY_ROWS_WARNING,
    DATA_STALE_ERROR_DAYS as DATA_STALE_ERROR_DAYS,
    DATA_STALE_WARNING_DAYS,
    DataFetchStatus as DataFetchStatus,
    _health_reference_datetime as _health_reference_datetime,
    attach_data_health,
    classify_data_warning as classify_data_warning,
    data_health_frame,
    data_health_reason_summary,
    data_health_summary,
    data_freshness_status_from_quote as data_freshness_status_from_quote,
    data_quality_risk_note,
    data_risk_flag as data_risk_flag,
    expected_latest_trading_date as expected_latest_trading_date,
    record_data_health,
    reset_data_health,
)

NEWS_HEALTH_ROWS: list[NewsFetchStatus] = []
LOCAL_DAILY_CACHE_STATS: dict[str, object] = {
    "hit_count": 0,
    "miss_count": 0,
    "latest_dates": {},
    "warnings": [],
    "provider_used": {},
    "provider_errors": {},
    "invalid_symbols": [],
}


def reset_local_daily_cache_stats() -> None:
    LOCAL_DAILY_CACHE_STATS["hit_count"] = 0
    LOCAL_DAILY_CACHE_STATS["miss_count"] = 0
    LOCAL_DAILY_CACHE_STATS["latest_dates"] = {}
    LOCAL_DAILY_CACHE_STATS["warnings"] = []
    LOCAL_DAILY_CACHE_STATS["provider_used"] = {}
    LOCAL_DAILY_CACHE_STATS["provider_errors"] = {}
    LOCAL_DAILY_CACHE_STATS["invalid_symbols"] = []


def _set_local_daily_cache_latest(symbol: str, df: pd.DataFrame) -> None:
    if df is None or df.empty or "日期" not in df.columns:
        return
    latest = pd.to_datetime(df["日期"], errors="coerce").max()
    if pd.notna(latest):
        latest_dates = LOCAL_DAILY_CACHE_STATS.setdefault("latest_dates", {})
        if isinstance(latest_dates, dict):
            latest_dates[symbol] = latest.strftime("%Y-%m-%d")


def local_daily_cache_stats_payload() -> dict[str, object]:
    latest_dates = LOCAL_DAILY_CACHE_STATS.get("latest_dates")
    latest_values = list(latest_dates.values()) if isinstance(latest_dates, dict) else []
    provider_used = LOCAL_DAILY_CACHE_STATS.get("provider_used")
    provider_errors = LOCAL_DAILY_CACHE_STATS.get("provider_errors")
    invalid_symbols = LOCAL_DAILY_CACHE_STATS.get("invalid_symbols")
    return {
        "daily_data_source": "local_daily_cache" if int(LOCAL_DAILY_CACHE_STATS.get("hit_count") or 0) else "provider_or_spot_fallback",
        "local_daily_cache_hit_count": int(LOCAL_DAILY_CACHE_STATS.get("hit_count") or 0),
        "local_daily_cache_miss_count": int(LOCAL_DAILY_CACHE_STATS.get("miss_count") or 0),
        "local_daily_cache_latest_date": max(latest_values) if latest_values else "",
        "local_daily_cache_latest_dates": latest_dates if isinstance(latest_dates, dict) else {},
        "local_daily_cache_warning": "Local daily cache is missing or incomplete; AKShare update was attempted." if int(LOCAL_DAILY_CACHE_STATS.get("miss_count") or 0) else "",
        "daily_provider_used": provider_used if isinstance(provider_used, dict) else {},
        "daily_provider_errors": provider_errors if isinstance(provider_errors, dict) else {},
        "invalid_daily_bar_symbols": invalid_symbols if isinstance(invalid_symbols, list) else [],
    }


def ensure_dirs() -> None:
    for path in [RAW_DIR, DAILY_DIR, NEWS_DIR, CACHE_DIR, STATE_DIR, REPORT_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    ensure_dirs()
    log_path = LOG_DIR / f"daily_quant_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("Log file: %s", log_path)


def to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def quiet_io_enabled() -> bool:
    return os.environ.get("QUANTDESK_TEST_QUIET_IO") == "1"


def save_table(df: pd.DataFrame, path: Path, quiet: bool = False) -> Path:
    effective_quiet = quiet or quiet_io_enabled()
    try:
        df.to_parquet(path, index=False)
        return path
    except Exception as exc:  # pragma: no cover - fallback for local env issues
        fallback = path.with_suffix(".csv")
        if not effective_quiet:
            logging.warning("Could not write parquet %s; writing csv. Error: %s", path, exc)
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        return fallback


def read_table(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception as exc:  # pragma: no cover - depends on optional parquet engines
            csv_path = path.with_suffix(".csv")
            if csv_path.exists():
                if not quiet_io_enabled():
                    logging.warning("Could not read parquet %s; reading csv. Error: %s", path, exc)
                return read_csv_frame(csv_path)
            raise
    csv_path = path.with_suffix(".csv")
    if csv_path.exists():
        return read_csv_frame(csv_path)
    return pd.DataFrame()


def reset_news_health() -> None:
    NEWS_HEALTH_ROWS.clear()


def record_news_health(status: NewsFetchStatus) -> None:
    NEWS_HEALTH_ROWS.append(status)


def news_health_frame() -> pd.DataFrame:
    return news_frame_from_statuses(NEWS_HEALTH_ROWS)


def news_health_summary(health: pd.DataFrame) -> dict[str, int]:
    return summarize_news_health_rows(health.to_dict("records") if not health.empty else [])


def attach_news_health(features: pd.DataFrame, health: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return features
    features = features.copy()
    defaults = {
        "news_status": "消息字段缺失",
        "latest_news_title": "",
        "news_warning_level": "INFO",
        "news_warning_message": "消息字段缺失",
    }
    for column, default in defaults.items():
        if column not in features.columns:
            features[column] = default
    if health.empty or "symbol" not in health.columns:
        return features
    health_map = {
        str(row.get("symbol", "")).zfill(6): row
        for row in health.to_dict("records")
        if str(row.get("symbol", "")).strip()
    }
    for idx, row in features.iterrows():
        code = stock_code(row.get("code"))
        status = health_map.get(code)
        if not status:
            continue
        features.at[idx, "latest_news_title"] = status.get("latest_title", "")
        features.at[idx, "news_warning_level"] = status.get("warning_level", "")
        features.at[idx, "news_warning_message"] = status.get("warning_message", "")
        skip_reason = status.get("skip_reason", "")
        if skip_reason == "sample_mode_no_network":
            label = "样例模式未联网"
        elif skip_reason == "news_provider_not_configured":
            label = "未配置新闻源"
        elif str(status.get("fetch_success", "")).lower() == "true" and int(float(status.get("item_count", 0) or 0)) == 0:
            label = "暂无相关新闻"
        elif str(status.get("fetch_success", "")).lower() == "false" and str(status.get("attempted", "")).lower() == "true":
            label = "消息抓取失败"
        elif status.get("latest_title"):
            label = str(status.get("latest_title"))
        else:
            label = str(status.get("warning_message") or "消息字段缺失")
        features.at[idx, "news_status"] = label
    return features


def save_csv_safely(df: pd.DataFrame, path: Path) -> Path:
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}")
        logging.warning("CSV is locked, writing fallback: %s", fallback)
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        return fallback


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    match = re.search(r"(\d{6})", text)
    if match:
        return match.group(1)
    return text.zfill(6) if text.isdigit() else text


def _extract_symbol_name(row: dict[str, object] | pd.Series) -> tuple[str, str]:
    getter = row.get if hasattr(row, "get") else lambda key, default=None: default
    symbol = _normalize_symbol(
        getter("symbol")
        or getter("code")
        or getter("代码")
        or getter("股票代码")
        or getter("证券代码")
    )
    name = str(getter("name") or getter("名称") or getter("股票名称") or "").strip()
    return symbol, name


def _spot_cache_meta_defaults() -> dict[str, object]:
    return {
        "spot_snapshot_source": "",
        "universe_scope": "",
        "provider_errors": [],
        "data_freshness_warning": "",
        "latest_date": "",
        "cache_fetched_at": "",
        "fetched_at": "",
        "row_count": 0,
        "spot_snapshot_source_label": "",
        "spot_snapshot_mode": "",
        "universe_sources": [],
        "hist_fallback_symbols": [],
    }


def _load_spot_snapshot_cache() -> tuple[pd.DataFrame, dict[str, object]]:
    if not SPOT_CACHE_CSV.exists():
        return pd.DataFrame(), {}
    cached = read_csv_frame(SPOT_CACHE_CSV)
    for warning in cached.attrs.get("warnings", []):
        logging.warning("spot cache csv warning: %s", warning)
    meta = read_json_safe(SPOT_CACHE_META, {})
    if not isinstance(meta, dict):
        meta = {}
    return cached, meta


def _write_spot_snapshot_cache(spot: pd.DataFrame, meta: dict[str, object]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    save_csv_safely(spot, SPOT_CACHE_CSV)
    atomic_write_json(SPOT_CACHE_META, meta)


def _read_symbol_table(path: Path, source: str, limit: int = 120) -> list[dict[str, str]]:
    if not path.exists():
        return []
    table = read_csv_frame(path)
    for warning in table.attrs.get("warnings", []):
        logging.warning("fallback universe table warning %s: %s", path, warning)
    rows: list[dict[str, str]] = []
    for _, row in table.head(limit).iterrows():
        symbol, name = _extract_symbol_name(row)
        if symbol:
            rows.append({"symbol": symbol, "name": name or symbol, "source": source})
    return rows


def _collect_fallback_universe(loaded_config: dict | None = None) -> tuple[list[dict[str, str]], list[str]]:
    config = loaded_config if isinstance(loaded_config, dict) else {}
    data_config = config.get("data") if isinstance(config.get("data"), dict) else {}
    fallback_symbols = data_config.get("fallback_symbols") or config.get("fallback_symbols") or []
    candidates: list[dict[str, str]] = []
    sources: list[str] = []
    seen: set[str] = set()

    def add(symbol: object, name: object = "", source: str = "") -> None:
        normalized = _normalize_symbol(symbol)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append({"symbol": normalized, "name": str(name or normalized), "source": source or "fallback"})
        if source and source not in sources:
            sources.append(source)

    for item in list_watchlist():
        add(item.get("symbol"), item.get("name"), "watchlist")

    try:
        portfolio = load_paper_portfolio()
    except Exception as exc:
        logging.warning("Could not load paper portfolio for fallback universe: %s", exc)
        portfolio = {}
    for position in portfolio.get("positions", []) or []:
        if isinstance(position, dict):
            add(position.get("symbol") or position.get("code"), position.get("name"), "paper_portfolio")

    for item in list_manual_confirmation_items():
        add(item.get("symbol"), item.get("name"), "manual_confirmation_queue")

    add_paths = [
        LONG_POOL_PATH,
        REPORT_DIR / f"{datetime.now():%Y-%m-%d}_candidates.csv",
        REPORT_DIR / f"{datetime.now():%Y-%m-%d}_long_candidates.csv",
        REPORT_DIR / f"{datetime.now():%Y-%m-%d}_board_candidates.csv",
        REPORT_DIR / f"{datetime.now():%Y-%m-%d}_long_pool.csv",
    ]
    for path in add_paths:
        for item in _read_symbol_table(path, "recent_report"):
            add(item.get("symbol"), item.get("name"), item.get("source", "recent_report"))

    for raw_symbol in fallback_symbols:
        add(raw_symbol, "", "config_fallback_symbols")

    if not candidates:
        for raw_symbol in DEFAULT_FALLBACK_SYMBOLS:
            add(raw_symbol, "", "default_fallback_symbols")

    if not sources:
        sources.append("default_fallback_symbols")
    return candidates, sources


def _spot_row_from_history(symbol: str, name: str, hist: pd.DataFrame, source: str, universe_scope: str) -> dict[str, object] | None:
    if hist.empty:
        return None
    normalized = normalize_daily(hist, symbol)
    if normalized.empty:
        return None
    latest = normalized.iloc[-1]
    previous = normalized.iloc[-2] if len(normalized) > 1 else None
    close = safe_float(latest.get("收盘"), None)
    if close is None:
        return None
    prev_close = safe_float(previous.get("收盘"), None) if previous is not None else None
    high = safe_float(latest.get("最高"), None)
    low = safe_float(latest.get("最低"), None)
    open_price = safe_float(latest.get("开盘"), None)
    volume = safe_float(latest.get("成交量"), None)
    amount = safe_float(latest.get("成交额"), None)
    if amount is None or amount <= 0:
        amount = close * volume if volume and close else None
    pct_change = safe_float(latest.get("涨跌幅"), None)
    if (pct_change is None or pd.isna(pct_change)) and prev_close not in {None, 0}:
        pct_change = (close / prev_close - 1) * 100
    change = safe_float(latest.get("涨跌额"), None)
    if (change is None or pd.isna(change)) and prev_close is not None:
        change = close - prev_close
    turnover = safe_float(latest.get("换手率"), None)
    latest_date = pd.Timestamp(latest.get("日期")).strftime("%Y-%m-%d") if pd.notna(latest.get("日期")) else ""
    return {
        "代码原始": symbol,
        "代码": symbol,
        "名称": name or symbol,
        "最新价": close,
        "涨跌幅": pct_change,
        "涨跌额": change,
        "成交量": volume,
        "成交额": amount,
        "今开": open_price,
        "最高": high,
        "最低": low,
        "昨收": prev_close,
        "换手率": turnover,
        "数据源": source,
        "spot_snapshot_source": source,
        "universe_scope": universe_scope,
        "latest_date": latest_date,
        "quote_latest_time": latest_date,
        "provider_error": "",
    }


def build_spot_from_hist_fallback(
    symbols: list[dict[str, str]],
    start_date: str,
    end_date: str,
    adjust: str,
    sleep_seconds: float,
    universe_scope: str,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], list[str], list[str]]:
    rows: list[dict[str, object]] = []
    histories: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    provider_errors: list[str] = []
    for item in symbols:
        symbol = _normalize_symbol(item.get("symbol"))
        name = str(item.get("name") or symbol)
        if not symbol:
            continue
        hist = pd.DataFrame()
        try:
            hist = fetch_daily(symbol, start_date, end_date, adjust, 5, sleep_seconds, name=name)
            if not hist.empty:
                histories[symbol] = hist
        except Exception as exc:
            message = f"{symbol} {name}: {exc}"
            warnings.append(message)
            provider_errors.append(message)
            continue
        if hist.empty:
            message = f"{symbol} {name}: history empty"
            warnings.append(message)
            provider_errors.append(message)
            continue
        row = _spot_row_from_history(symbol, name, hist, "hist_fallback", universe_scope)
        if row is None:
            message = f"{symbol} {name}: unable to build fallback spot row"
            warnings.append(message)
            provider_errors.append(message)
            continue
        rows.append(row)
    if not rows:
        return pd.DataFrame(), histories, warnings, provider_errors
    spot = pd.DataFrame(rows)
    spot = to_numeric(spot, NUMERIC_SPOT_COLUMNS)
    return spot, histories, warnings, provider_errors


def _filter_spot_snapshot(spot: pd.DataFrame, limit: int, min_amount: float, max_price: float | None) -> pd.DataFrame:
    if spot.empty:
        return spot
    spot = spot.copy()
    if "代码原始" not in spot.columns and "代码" in spot.columns:
        spot["代码原始"] = spot["代码"].astype(str)
    if "代码" in spot.columns:
        spot["代码"] = (
            spot["代码"].astype(str)
            .str.extract(r"(\d{6})", expand=False)
            .fillna(spot["代码"].astype(str))
            .astype(str)
            .str.zfill(6)
        )
    if "名称" in spot.columns:
        spot["名称"] = spot["名称"].astype(str)
    if "数据源" not in spot.columns:
        spot["数据源"] = ""
    spot = to_numeric(spot, NUMERIC_SPOT_COLUMNS)
    mask_market = spot["代码"].str.startswith(("0", "3", "6")) if "代码" in spot.columns else pd.Series(True, index=spot.index)
    mask_name = ~spot["名称"].str.contains("ST|退", case=False, na=False) if "名称" in spot.columns else pd.Series(True, index=spot.index)
    mask_price = spot["最新价"].notna() & (spot["最新价"] > 0) if "最新价" in spot.columns else pd.Series(True, index=spot.index)
    if max_price is not None and max_price > 0 and "最新价" in spot.columns:
        mask_price = mask_price & (spot["最新价"] <= max_price)
    mask_amount = spot["成交额"].fillna(0) >= min_amount if "成交额" in spot.columns else pd.Series(True, index=spot.index)
    filtered = spot[mask_market & mask_name & mask_price & mask_amount].copy()
    if "成交额" in filtered.columns:
        filtered = filtered.sort_values("成交额", ascending=False)
    return filtered.head(limit)


def fetch_spot(
    limit: int,
    min_amount: float,
    max_price: float | None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    adjust: str = "qfq",
    sleep_seconds: float = 0.0,
    loaded_config: dict | None = None,
    spot_source: str = "auto",
) -> pd.DataFrame:
    logging.info("Fetching A-share spot snapshot")
    provider_errors: list[str] = []
    universe_scope = "full_market"
    spot_meta = _spot_cache_meta_defaults()
    histories: dict[str, pd.DataFrame] = {}
    source = "eastmoney_spot"
    spot = pd.DataFrame()
    if spot_source == "cache":
        cached_spot, cached_meta = _load_spot_snapshot_cache()
        if cached_spot.empty:
            raise RuntimeError("Local spot cache is missing; run an online update before cache-only report generation.")
        spot_meta.update(cached_meta if isinstance(cached_meta, dict) else {})
        spot_meta["spot_snapshot_source"] = "cache"
        spot_meta["spot_snapshot_mode"] = "cache_only"
        spot_meta["universe_scope"] = spot_meta.get("universe_scope") or "cache"
        spot_meta["data_freshness_warning"] = "使用本地 spot 缓存生成日报；未联网刷新全市场行情快照。"
        spot_meta["row_count"] = int(len(cached_spot))
        filtered = _filter_spot_snapshot(cached_spot, limit, min_amount, max_price)
        if filtered.empty:
            raise RuntimeError("Local spot cache exists but no rows matched the report filters.")
        filtered.attrs["spot_snapshot_meta"] = spot_meta
        filtered.attrs["spot_histories"] = histories
        logging.info("Loaded spot snapshot from cache-only mode: %s rows", len(filtered))
        return filtered
    try:
        spot = fetch_spot_eastmoney()
    except Exception as exc:
        provider_errors.append(f"Eastmoney: {exc}")
        logging.warning("Eastmoney spot snapshot failed; falling back to Sina. Error: %s", exc)
        source = "sina_spot"
        try:
            spot = fetch_spot_sina()
        except Exception as sina_exc:
            provider_errors.append(f"Sina: {sina_exc}")
            logging.warning("Sina spot snapshot failed; trying cache and hist fallback. Error: %s", sina_exc)
            source = "cache"
            cached_spot, cached_meta = _load_spot_snapshot_cache()
            if not cached_spot.empty:
                spot = cached_spot
                spot_meta.update(cached_meta if isinstance(cached_meta, dict) else {})
                spot_meta["spot_snapshot_source"] = "cache"
                spot_meta["spot_snapshot_mode"] = "cache"
                spot_meta["universe_scope"] = spot_meta.get("universe_scope") or "cache"
                spot_meta["data_freshness_warning"] = spot_meta.get(
                    "data_freshness_warning",
                    "使用本地 spot 缓存，不代表当前全市场实时扫描。",
                )
                spot_meta["cache_fetched_at"] = spot_meta.get("cache_fetched_at") or spot_meta.get("fetched_at") or ""
                spot_meta["provider_errors"] = list(spot_meta.get("provider_errors") or provider_errors)
                spot_meta["row_count"] = int(len(cached_spot))
                spot_meta.setdefault("latest_date", str(spot_meta.get("latest_date") or ""))
                spot = _filter_spot_snapshot(cached_spot, limit, min_amount, max_price)
                _write_spot_snapshot_cache(spot, spot_meta)
                spot.attrs["spot_snapshot_meta"] = spot_meta
                spot.attrs["spot_histories"] = histories
                logging.info("Loaded spot snapshot from cache: %s rows", len(spot))
                return spot
            source = "hist_fallback"
            universe_items, scope_parts = _collect_fallback_universe(loaded_config)
            universe_scope = "+".join(scope_parts) if scope_parts else "default_fallback_symbols"
            if not start_date:
                start_date = "20230101"
            if not end_date:
                end_date = datetime.now().strftime("%Y%m%d")
            hist_spot, histories, fallback_warnings, fallback_errors = build_spot_from_hist_fallback(
                universe_items,
                start_date,
                end_date,
                adjust,
                sleep_seconds,
                universe_scope,
            )
            provider_errors.extend(fallback_errors)
            if not hist_spot.empty:
                spot = hist_spot
                spot_meta.update(
                    {
                        "spot_snapshot_source": "hist_fallback",
                        "spot_snapshot_mode": "hist_fallback",
                        "universe_scope": universe_scope,
                        "provider_errors": provider_errors,
                        "data_freshness_warning": "全市场行情源失败，本次仅基于 fallback_symbols / 本地候选池生成，不代表全市场扫描。",
                        "hist_fallback_symbols": [item.get("symbol", "") for item in universe_items],
                        "universe_sources": scope_parts,
                    }
                )
                if fallback_warnings:
                    spot_meta["provider_errors"] = provider_errors + fallback_warnings
                spot = _filter_spot_snapshot(spot, limit, min_amount, max_price)
                if spot.empty:
                    raise RuntimeError(
                        "Hist fallback produced no rows after filtering; fallback universe was empty or too restrictive."
                    )
                _write_spot_snapshot_cache(spot, spot_meta)
                spot.attrs["spot_snapshot_meta"] = spot_meta
                spot.attrs["spot_histories"] = histories
                logging.info("Built hist fallback spot snapshot: %s rows", len(spot))
                return spot
            combined_errors = "; ".join(provider_errors) or "spot snapshot provider error"
            raise RuntimeError(
                "All full-market spot sources failed and hist fallback could not build any rows. "
                "Please check network access, cache availability, and fallback universe configuration. "
                f"Errors: {combined_errors}"
            )
    if spot.empty:
        raise RuntimeError("AKShare returned an empty spot snapshot")

    spot["代码原始"] = spot["代码"].astype(str)
    spot["代码"] = (
        spot["代码原始"]
        .str.extract(r"(\d{6})", expand=False)
        .fillna(spot["代码原始"])
        .astype(str)
        .str.zfill(6)
    )
    spot["名称"] = spot["名称"].astype(str)
    spot["数据源"] = source
    spot = to_numeric(spot, NUMERIC_SPOT_COLUMNS)

    filtered = _filter_spot_snapshot(spot, limit, min_amount, max_price)
    if filtered.empty:
        raise RuntimeError("No stocks matched the universe filters")

    spot_meta.update(
        {
            "spot_snapshot_source": source,
            "spot_snapshot_mode": "full_market",
            "universe_scope": universe_scope,
            "provider_errors": provider_errors,
            "data_freshness_warning": "",
            "row_count": int(len(filtered)),
        }
    )
    _write_spot_snapshot_cache(filtered, spot_meta)
    filtered.attrs["spot_snapshot_meta"] = spot_meta
    filtered.attrs["spot_histories"] = histories

    snapshot_path = RAW_DIR / f"spot_{datetime.now():%Y%m%d_%H%M%S}.parquet"
    saved = save_table(filtered, snapshot_path)
    logging.info("Saved spot snapshot: %s", saved)
    return filtered


def normalize_daily(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    rename_map = {
        "date": "日期",
        "open": "开盘",
        "high": "最高",
        "low": "最低",
        "close": "收盘",
        "volume": "成交量",
        "amount": "成交额",
        "turnover": "换手率",
    }
    df = df.rename(columns={key: value for key, value in rename_map.items() if key in df.columns})
    if "日期" not in df.columns:
        raise ValueError(f"{symbol} daily data has no 日期 column")
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    df["股票代码"] = symbol
    df = to_numeric(df, NUMERIC_DAILY_COLUMNS)
    if "换手率" in df.columns and df["换手率"].dropna().between(0, 1).all():
        df["换手率"] = df["换手率"] * 100
    df = df.dropna(subset=["日期", "收盘", "最高", "最低"])
    df = df.sort_values("日期").drop_duplicates("日期", keep="last")
    return df.reset_index(drop=True)


def prefixed_symbol(symbol: str) -> str:
    if symbol.startswith("6"):
        return f"sh{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz{symbol}"
    return symbol


def local_daily_cache_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol}.csv"


def read_local_daily_cache(symbol: str) -> pd.DataFrame:
    path = local_daily_cache_path(symbol)
    if not path.exists():
        return pd.DataFrame()
    try:
        raw = read_csv_frame(path)
        warnings = [str(item) for item in raw.attrs.get("warnings", []) if item]
        normalized = normalize_daily(raw, symbol)
        normalized.attrs["warnings"] = warnings
        return normalized
    except Exception as exc:
        logging.warning("Could not read local daily cache %s: %s", path, exc)
        return pd.DataFrame()


def save_local_daily_cache(symbol: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    path = local_daily_cache_path(symbol)
    output = normalize_daily(df, symbol)
    if output.empty:
        return
    output = output.copy()
    output["日期"] = pd.to_datetime(output["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    save_csv_safely(output, path)


def _local_daily_cache_target_date(end_date: str) -> pd.Timestamp | None:
    parsed = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(parsed):
        return None
    try:
        reference = datetime.combine(parsed.date(), dt_time(16, 0))
        expected = expected_settlement_date(reference)
        if expected is not None:
            return pd.Timestamp(expected)
    except Exception:
        pass
    return pd.Timestamp(parsed.date())


def local_daily_cache_covers_end_date(cached: pd.DataFrame, end_date: str) -> bool:
    if cached is None or cached.empty or "日期" not in cached.columns:
        return False
    latest = pd.to_datetime(cached["日期"], errors="coerce").max()
    target = _local_daily_cache_target_date(end_date)
    return bool(pd.notna(latest) and target is not None and latest.normalize() >= target.normalize())


def bounded_position(value: float, low: float, high: float) -> float:
    if not all(math.isfinite(x) for x in [value, low, high]) or high <= low:
        return np.nan
    return max(0.0, min(100.0, (value - low) / (high - low) * 100))


def distance_pct(value: float, base: float) -> float:
    if not all(math.isfinite(x) for x in [value, base]) or base == 0:
        return np.nan
    return (value / base - 1) * 100


def position_band_label(position_pct: float) -> str:
    if pd.isna(position_pct):
        return "位置未知"
    if position_pct < 25:
        return "低位"
    if position_pct < 55:
        return "中低位"
    if position_pct < 80:
        return "中高位"
    if position_pct < 92:
        return "高位"
    return "接近前高"


def fetch_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str,
    refresh_days: int,
    sleep_seconds: float,
    name: str = "",
) -> pd.DataFrame:
    legacy_cache_path = DAILY_DIR / f"{symbol}_{adjust}.parquet"
    local_cached = read_local_daily_cache(symbol)
    legacy_cached = normalize_daily(read_table(legacy_cache_path), symbol)
    cached = local_cached if not local_cached.empty else legacy_cached
    cache_present = not cached.empty
    local_cache_present = not local_cached.empty
    requested_source = "local_daily_cache_then_akshare_hist"
    effective_source = "none"
    fallback_used = False
    fresh_fetch_success = False
    cache_merged = False
    served_from_cache = False
    fallback_from = ""
    fallback_to = ""
    error_message = ""

    if local_cache_present:
        _set_local_daily_cache_latest(symbol, local_cached)
        if local_daily_cache_covers_end_date(local_cached, end_date):
            LOCAL_DAILY_CACHE_STATS["hit_count"] = int(LOCAL_DAILY_CACHE_STATS.get("hit_count") or 0) + 1
            if isinstance(LOCAL_DAILY_CACHE_STATS.get("provider_used"), dict):
                LOCAL_DAILY_CACHE_STATS["provider_used"][symbol] = "local_daily_cache"
            effective_source = "local_daily_cache"
            served_from_cache = True
            logging.info("Using local daily cache for %s", symbol)
            record_data_health(
                symbol=symbol,
                name=name,
                adjust=adjust,
                requested_source=requested_source,
                effective_source=effective_source,
                merged=local_cached,
                end_date=end_date,
                fresh_fetch_success=False,
                cache_present=True,
                cache_merged=False,
                served_from_cache=True,
                fallback_used=False,
                fallback_from="",
                fallback_to="",
                error_message="",
                min_history_rows=DATA_MIN_HISTORY_ROWS_WARNING,
            )
            return local_cached
    LOCAL_DAILY_CACHE_STATS["miss_count"] = int(LOCAL_DAILY_CACHE_STATS.get("miss_count") or 0) + 1

    fetch_start = start_date
    if not cached.empty:
        last_date = cached["日期"].max()
        refresh_from = last_date - timedelta(days=refresh_days)
        fetch_start = max(pd.Timestamp(start_date), refresh_from).strftime("%Y%m%d")

    logging.info("Fetching daily bars: %s from %s to %s", symbol, fetch_start, end_date)
    fresh = pd.DataFrame()
    try:
        fresh = fetch_daily_eastmoney(
            symbol=symbol,
            start_date=fetch_start,
            end_date=end_date,
            adjust=adjust,
        )
        if fresh.empty:
            raise ValueError("eastmoney returned empty daily data")
        fresh_fetch_success = True
        effective_source = "eastmoney"
    except Exception as exc:
        logging.warning("Eastmoney daily bars failed for %s; falling back to Sina. Error: %s", symbol, exc)
        fallback_used = True
        fallback_from = "eastmoney"
        fallback_to = "sina"
        error_message = f"eastmoney: {exc}"
        try:
            fresh = fetch_daily_sina(
                symbol=prefixed_symbol(symbol),
                start_date=fetch_start,
                end_date=end_date,
                adjust=adjust,
            )
            if fresh.empty:
                raise ValueError("sina returned empty daily data")
            fresh_fetch_success = True
            effective_source = "sina"
        except Exception as fallback_exc:
            logging.warning("Sina daily bars failed for %s. Error: %s", symbol, fallback_exc)
            error_message = f"{error_message}; sina: {fallback_exc}"
            fresh = pd.DataFrame()
    fresh = normalize_daily(fresh, symbol)

    if not cached.empty and not fresh.empty:
        merged = pd.concat([cached, fresh], ignore_index=True)
        merged = merged.sort_values("日期").drop_duplicates("日期", keep="last")
        cache_merged = True
    elif not fresh.empty:
        merged = fresh
    else:
        merged = cached
        if cache_present:
            effective_source = "local_daily_cache" if local_cache_present else "cache"
            served_from_cache = True

    if merged.empty:
        logging.warning("No daily data for %s", symbol)
        effective_source = "none"
        if isinstance(LOCAL_DAILY_CACHE_STATS.get("provider_errors"), dict):
            LOCAL_DAILY_CACHE_STATS["provider_errors"][symbol] = error_message or "no daily data"
        if isinstance(LOCAL_DAILY_CACHE_STATS.get("invalid_symbols"), list):
            LOCAL_DAILY_CACHE_STATS["invalid_symbols"].append(symbol)
        record_data_health(
            symbol=symbol,
            name=name,
            adjust=adjust,
            requested_source=requested_source,
            effective_source=effective_source,
            merged=merged,
            end_date=end_date,
            fresh_fetch_success=fresh_fetch_success,
            cache_present=cache_present,
            cache_merged=cache_merged,
            served_from_cache=served_from_cache,
            fallback_used=fallback_used,
            fallback_from=fallback_from,
            fallback_to=fallback_to,
            error_message=error_message or "no daily data",
            min_history_rows=DATA_MIN_HISTORY_ROWS_WARNING,
        )
        return merged

    save_table(merged, legacy_cache_path)
    save_local_daily_cache(symbol, merged)
    _set_local_daily_cache_latest(symbol, merged)
    if isinstance(LOCAL_DAILY_CACHE_STATS.get("provider_used"), dict):
        provider_label = "eastmoney_hist" if effective_source == "eastmoney" else "sina_daily" if effective_source == "sina" else effective_source
        LOCAL_DAILY_CACHE_STATS["provider_used"][symbol] = provider_label
    if error_message and isinstance(LOCAL_DAILY_CACHE_STATS.get("provider_errors"), dict):
        LOCAL_DAILY_CACHE_STATS["provider_errors"][symbol] = error_message
    record_data_health(
        symbol=symbol,
        name=name,
        adjust=adjust,
        requested_source=requested_source,
        effective_source=effective_source,
        merged=merged,
        end_date=end_date,
        fresh_fetch_success=fresh_fetch_success,
        cache_present=cache_present,
        cache_merged=cache_merged,
        served_from_cache=served_from_cache,
        fallback_used=fallback_used,
        fallback_from=fallback_from,
        fallback_to=fallback_to,
        error_message=error_message,
    )
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return merged

def compute_base_features(symbol: str, name: str, hist: pd.DataFrame, spot_row: pd.Series) -> dict | None:
    if hist.empty or len(hist) < 130:
        return None

    df = hist.copy()
    close = df["收盘"].astype(float)
    high = df["最高"].astype(float)
    low = df["最低"].astype(float)
    amount = df.get("成交额", pd.Series(np.nan, index=df.index)).astype(float)

    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()
    df["ma120"] = close.rolling(120).mean()
    df["ret20"] = close / close.shift(20) - 1
    df["ret60"] = close / close.shift(60) - 1
    df["high60_prev"] = high.shift(1).rolling(60).max()
    df["amount20"] = amount.rolling(20).mean()
    df["support20"] = low.rolling(20).min()
    df["resistance20"] = high.rolling(20).max()
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14"] = true_range.rolling(14).mean()
    df["vol20"] = close.ffill().pct_change(fill_method=None).rolling(20).std() * math.sqrt(252)
    df["drawdown60"] = close / close.rolling(60).max() - 1
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    df["kdj_k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    df["kdj_d"] = df["kdj_k"].ewm(alpha=1 / 3, adjust=False).mean()
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]
    df["boll_mid"] = df["ma20"]
    df["boll_std"] = close.rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * df["boll_std"]
    df["boll_lower"] = df["boll_mid"] - 2 * df["boll_std"]
    df["boll_position"] = (close - df["boll_lower"]) / (df["boll_upper"] - df["boll_lower"]).replace(0, np.nan) * 100
    df["close_in_day_pct"] = (close - low) / (high - low).replace(0, np.nan) * 100
    df["amplitude_pct"] = (high - low) / prev_close.replace(0, np.nan) * 100
    df["macd_dif"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_bar"] = 2 * (df["macd_dif"] - df["macd_dea"])

    latest = df.iloc[-1]
    previous = df.iloc[-2]
    required = ["ma5", "ma10", "ma20", "ma60", "ma120", "ret20", "ret60", "high60_prev", "amount20", "atr14"]
    if latest[required].isna().any():
        return None

    current_close = float(latest["收盘"])
    ma5 = float(latest["ma5"])
    ma10 = float(latest["ma10"])
    ma20 = float(latest["ma20"])
    ma60 = float(latest["ma60"])
    ma120 = float(latest["ma120"])
    ret20 = float(latest["ret20"])
    ret60 = float(latest["ret60"])
    amount_ratio = float(latest["成交额"] / latest["amount20"]) if latest["amount20"] else np.nan
    vol20 = float(latest["vol20"]) if pd.notna(latest["vol20"]) else np.nan
    drawdown60 = float(latest["drawdown60"]) if pd.notna(latest["drawdown60"]) else np.nan
    change_pct = float(spot_row.get("涨跌幅", np.nan))
    high250 = float(high.tail(250).max())
    low250 = float(low.tail(250).min())
    high60 = float(high.tail(60).max())
    low60 = float(low.tail(60).min())
    position250_pct = bounded_position(current_close, low250, high250)
    position60_pct = bounded_position(current_close, low60, high60)
    dist_ma20_pct = distance_pct(current_close, ma20)
    dist_ma60_pct = distance_pct(current_close, ma60)
    dist_ma120_pct = distance_pct(current_close, ma120)
    dist_high250_pct = distance_pct(current_close, high250)
    support20 = float(latest["support20"])
    resistance20 = float(latest["resistance20"])
    dist_support20_pct = distance_pct(current_close, support20)
    dist_resistance20_pct = distance_pct(current_close, resistance20)
    position_label = position_band_label(position250_pct)
    if pd.notna(position250_pct) and pd.notna(dist_high250_pct):
        position_note = (
            f"一年区间位置约 {position250_pct:.1f}%，"
            f"距离一年高点 {dist_high250_pct:.1f}%，属于{position_label}。"
        )
    else:
        position_note = "历史区间不足，暂时不判断高低位置。"
    macd_dif = float(latest["macd_dif"])
    macd_dea = float(latest["macd_dea"])
    macd_bar = float(latest["macd_bar"])
    rsi14 = float(latest["rsi14"]) if pd.notna(latest["rsi14"]) else np.nan
    kdj_k = float(latest["kdj_k"]) if pd.notna(latest["kdj_k"]) else np.nan
    kdj_d = float(latest["kdj_d"]) if pd.notna(latest["kdj_d"]) else np.nan
    kdj_j = float(latest["kdj_j"]) if pd.notna(latest["kdj_j"]) else np.nan
    boll_upper = float(latest["boll_upper"]) if pd.notna(latest["boll_upper"]) else np.nan
    boll_mid = float(latest["boll_mid"]) if pd.notna(latest["boll_mid"]) else np.nan
    boll_lower = float(latest["boll_lower"]) if pd.notna(latest["boll_lower"]) else np.nan
    boll_position = float(latest["boll_position"]) if pd.notna(latest["boll_position"]) else np.nan
    close_in_day_pct = float(latest["close_in_day_pct"]) if pd.notna(latest["close_in_day_pct"]) else np.nan
    amplitude_pct = float(latest["amplitude_pct"]) if pd.notna(latest["amplitude_pct"]) else np.nan
    prev_dif = float(previous["macd_dif"])
    prev_dea = float(previous["macd_dea"])
    prev_k = float(previous["kdj_k"]) if pd.notna(previous["kdj_k"]) else np.nan
    prev_d = float(previous["kdj_d"]) if pd.notna(previous["kdj_d"]) else np.nan
    if prev_dif <= prev_dea and macd_dif > macd_dea:
        macd_signal = "MACD金叉"
    elif prev_dif >= prev_dea and macd_dif < macd_dea:
        macd_signal = "MACD死叉"
    elif macd_dif > macd_dea and macd_bar > 0:
        macd_signal = "MACD多头区"
    elif macd_dif < macd_dea and macd_bar < 0:
        macd_signal = "MACD空头区"
    else:
        macd_signal = "MACD震荡"
    if current_close > ma20 > ma60:
        ma_signal = "价在20日线上，20日线高于60日线"
    elif current_close > ma20:
        ma_signal = "价在20日线上，但中期趋势待确认"
    elif current_close < ma60:
        ma_signal = "价低于60日线，趋势偏弱"
    else:
        ma_signal = "价在20日线附近震荡"
    if pd.isna(amount_ratio):
        volume_signal = "量能未知"
    elif amount_ratio >= 2:
        volume_signal = "明显放量"
    elif amount_ratio >= 1.1:
        volume_signal = "温和放量"
    elif amount_ratio >= 0.8:
        volume_signal = "量能正常"
    else:
        volume_signal = "量能偏弱"

    if current_close > ma5 > ma10 > ma20 > ma60:
        ma_stack_signal = "短中期均线多头排列"
    elif current_close > ma20 > ma60:
        ma_stack_signal = "价格站上20日线，趋势偏顺"
    elif current_close < ma20 < ma60:
        ma_stack_signal = "价格在20日线下方，趋势偏弱"
    else:
        ma_stack_signal = "均线纠缠，方向还不清晰"

    if pd.isna(rsi14):
        rsi_signal = "RSI未知"
    elif rsi14 >= 80:
        rsi_signal = "RSI极热"
    elif rsi14 >= 70:
        rsi_signal = "RSI偏热"
    elif rsi14 >= 50:
        rsi_signal = "RSI强势区"
    elif rsi14 >= 30:
        rsi_signal = "RSI弱势区"
    else:
        rsi_signal = "RSI超卖区"

    if pd.isna(kdj_k) or pd.isna(kdj_d):
        kdj_signal = "KDJ未知"
    elif pd.notna(prev_k) and pd.notna(prev_d) and prev_k <= prev_d and kdj_k > kdj_d:
        kdj_signal = "KDJ金叉"
    elif pd.notna(prev_k) and pd.notna(prev_d) and prev_k >= prev_d and kdj_k < kdj_d:
        kdj_signal = "KDJ死叉"
    elif kdj_k >= 80 or kdj_j >= 100:
        kdj_signal = "KDJ高位钝化"
    elif kdj_k <= 20 or kdj_j <= 0:
        kdj_signal = "KDJ低位"
    else:
        kdj_signal = "KDJ中性"

    if pd.isna(boll_position):
        boll_signal = "BOLL未知"
    elif boll_position >= 100:
        boll_signal = "突破布林上轨"
    elif boll_position >= 80:
        boll_signal = "靠近布林上轨"
    elif boll_position <= 0:
        boll_signal = "跌破布林下轨"
    elif boll_position <= 20:
        boll_signal = "靠近布林下轨"
    else:
        boll_signal = "布林中轨附近"

    if pd.isna(close_in_day_pct):
        candle_signal = "K线位置未知"
    elif close_in_day_pct >= 70:
        candle_signal = "收在日内高位"
    elif close_in_day_pct <= 30:
        candle_signal = "收在日内低位"
    else:
        candle_signal = "收在日内中部"
    if pd.notna(amplitude_pct) and amplitude_pct >= 8:
        candle_signal += "，振幅较大"

    score = 0.0
    score += 12 if current_close > ma20 else 0
    score += 12 if ma20 > ma60 else 0
    score += 10 if current_close > ma120 else 0
    score += 8 if ma60 > ma120 else 0
    score += 10 if ret20 > 0 else 0
    score += 5 if ret20 > 0.05 else 0
    score += 10 if ret60 > 0.05 else 0
    score += 10 if current_close >= float(latest["high60_prev"]) * 0.98 else 0
    if pd.notna(amount_ratio):
        score += 8 if 1.1 <= amount_ratio <= 3.5 else 4 if amount_ratio >= 0.8 else 0
    if pd.notna(vol20):
        score += 5 if vol20 < 0.45 else 2 if vol20 < 0.65 else 0
    if pd.notna(drawdown60):
        score += 5 if drawdown60 > -0.12 else 0
    if pd.notna(change_pct) and abs(change_pct) >= 9.5:
        score -= 10

    atr14 = float(latest["atr14"])
    stop = max(ma20 * 0.97, current_close - 2 * atr14)
    if not math.isfinite(stop) or stop >= current_close:
        stop = current_close * 0.92
    risk_pct = (current_close - stop) / current_close
    target1 = current_close + 2 * (current_close - stop)

    sell_warning = ""
    if current_close < ma20:
        sell_warning = "跌破20日均线"
    elif current_close < ma60:
        sell_warning = "跌破60日均线"

    return {
        "code": symbol,
        "name": name,
        "date": latest["日期"].strftime("%Y-%m-%d"),
        "close": round(current_close, 3),
        "change_pct": round(change_pct, 3) if pd.notna(change_pct) else np.nan,
        "amount_yuan": float(spot_row.get("成交额", latest.get("成交额", np.nan))),
        "turnover": float(spot_row.get("换手率", np.nan)),
        "high250": round(high250, 3),
        "low250": round(low250, 3),
        "position250_pct": position250_pct,
        "position60_pct": position60_pct,
        "dist_ma20_pct": dist_ma20_pct,
        "dist_ma60_pct": dist_ma60_pct,
        "dist_ma120_pct": dist_ma120_pct,
        "dist_high250_pct": dist_high250_pct,
        "position_label": position_label,
        "position_note": position_note,
        "ma5": round(ma5, 3),
        "ma10": round(ma10, 3),
        "ma20": round(ma20, 3),
        "ma60": round(ma60, 3),
        "ma120": round(ma120, 3),
        "support20": round(support20, 3),
        "resistance20": round(resistance20, 3),
        "dist_support20_pct": dist_support20_pct,
        "dist_resistance20_pct": dist_resistance20_pct,
        "ret20": ret20,
        "ret60": ret60,
        "amount_ratio": amount_ratio,
        "vol20": vol20,
        "drawdown60": drawdown60,
        "atr14": round(atr14, 3),
        "rsi14": rsi14,
        "kdj_k": kdj_k,
        "kdj_d": kdj_d,
        "kdj_j": kdj_j,
        "boll_upper": boll_upper,
        "boll_mid": boll_mid,
        "boll_lower": boll_lower,
        "boll_position": boll_position,
        "close_in_day_pct": close_in_day_pct,
        "amplitude_pct": amplitude_pct,
        "macd_dif": round(macd_dif, 4),
        "macd_dea": round(macd_dea, 4),
        "macd_bar": round(macd_bar, 4),
        "macd_signal": macd_signal,
        "ma_signal": ma_signal,
        "ma_stack_signal": ma_stack_signal,
        "volume_signal": volume_signal,
        "rsi_signal": rsi_signal,
        "kdj_signal": kdj_signal,
        "boll_signal": boll_signal,
        "candle_signal": candle_signal,
        "base_score": max(0, score),
        "stop": round(stop, 3),
        "risk_pct": risk_pct,
        "target1": round(target1, 3),
        "buy_trigger": f"次日收盘仍站上20日线 {ma20:.2f}，且成交额不明显萎缩",
        "sell_rule": sell_warning or f"跌破止损 {stop:.2f} 或跌破20日线 {ma20:.2f}",
    }


def compute_base_features_as_of(
    symbol: str,
    name: str,
    hist: pd.DataFrame,
    spot_row: pd.Series,
    as_of_date: str | datetime | pd.Timestamp,
) -> dict | None:
    """Compute the feature snapshot using only rows available by as_of_date."""
    if hist.empty or "日期" not in hist.columns:
        return None
    cutoff = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(cutoff):
        return None
    frame = hist.copy()
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
    frame = frame[frame["日期"] <= cutoff].sort_values("日期")
    return compute_base_features(symbol, name, frame, spot_row)


def finalize_scores(features: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return features
    features = features.copy()
    features["rs20"] = features["ret20"].rank(pct=True)
    features["rs60"] = features["ret60"].rank(pct=True)
    features["liquidity_rank"] = features["amount_yuan"].rank(pct=True)
    features["score"] = (
        features["base_score"]
        + features["rs20"].fillna(0) * 10
        + features["rs60"].fillna(0) * 10
        + features["liquidity_rank"].fillna(0) * 5
    ).clip(0, 100)
    features["score"] = features["score"].round(1)
    features["signal"] = np.select(
        [
            features["score"] >= 80,
            features["score"] >= 70,
            features["score"] >= 60,
        ],
        ["强关注", "观察触发点", "弱关注"],
        default="无",
    )
    long_score = (
        features["base_score"] * 0.55
        + features["rs60"].fillna(0) * 22
        + features["rs20"].fillna(0) * 12
        + features["liquidity_rank"].fillna(0) * 8
    )
    long_score += np.where(features["vol20"].fillna(9) < 0.45, 6, 0)
    long_score += np.where(features["drawdown60"].fillna(-9) > -0.10, 6, 0)
    long_score -= np.where(features["risk_pct"].fillna(9) > 0.12, 8, 0)
    long_score -= np.where(features["change_pct"].fillna(0).abs() >= 9.5, 8, 0)
    long_score -= np.where(features["ret20"].fillna(0) > 0.45, 6, 0)
    pos250 = features["position250_pct"].fillna(50)
    long_score += np.where((pos250 >= 25) & (pos250 <= 75) & (features["close"] > features["ma60"]), 4, 0)
    long_score -= np.where(pos250 > 88, 4, 0)
    features["long_score"] = long_score.clip(0, 100).round(1)

    pos60 = features["position60_pct"].fillna(50)
    board_score = (
        features["rs20"].fillna(0) * 18
        + features["liquidity_rank"].fillna(0) * 12
        + np.where(features["close"] > features["ma20"], 10, 0)
        + np.where(features["ma20"] > features["ma60"], 10, 0)
        + np.where(features["close"] >= features["ma60"], 8, 0)
        + np.where(features["amount_ratio"].fillna(0) >= 1.5, 15, 0)
        + np.where(
            (features["amount_ratio"].fillna(0) >= 1.05)
            & (features["amount_ratio"].fillna(0) < 1.5),
            8,
            0,
        )
        + np.where(features["change_pct"].fillna(0) >= 9.5, 25, 0)
        + np.where(
            (features["change_pct"].fillna(0) >= 6)
            & (features["change_pct"].fillna(0) < 9.5),
            16,
            0,
        )
        + np.where(
            (features["change_pct"].fillna(0) >= 3)
            & (features["change_pct"].fillna(0) < 6),
            8,
            0,
        )
        + np.where(features["ret20"].fillna(0) > 0.12, 8, 0)
    )
    board_score += np.where(pos60 >= 80, 5, 0)
    board_score -= np.where(pos60 < 35, 4, 0)
    board_score -= np.where(features["change_pct"].fillna(0) < 0, 10, 0)
    board_score -= np.where(features["risk_pct"].fillna(0) > 0.16, 8, 0)
    features["board_score"] = board_score.clip(0, 100).round(1)

    features["long_signal"] = np.select(
        [
            features["long_score"] >= 80,
            features["long_score"] >= 70,
            features["long_score"] >= 60,
        ],
        ["长期强候选", "长期观察", "长期弱关注"],
        default="无",
    )
    features["board_signal"] = np.select(
        [
            features["board_score"] >= 80,
            features["board_score"] >= 70,
            features["board_score"] >= 60,
        ],
        ["强势接力候选", "打板观察", "短线弱关注"],
        default="无",
    )

    features["long_buy_condition"] = (
        "分批观察：收盘价在20/60日线之上，回踩不破20日线，成交额不低于20日均额"
    )
    features["long_avoid_condition"] = (
        "避免：放量长阴、跌破60日线、距离止损超过12%、短期涨幅过热后追高"
    )
    features["board_buy_condition"] = (
        "仅限短线：次日竞价/开盘强于板块，回封有量，不能炸板放巨量；必须小仓位"
    )
    features["board_avoid_condition"] = (
        "避免：一字板买不到、炸板回落、量能异常失控、板块退潮、亏损后加仓"
    )
    features["risk_pct"] = (features["risk_pct"] * 100).round(2)
    features["ret20"] = (features["ret20"] * 100).round(2)
    features["ret60"] = (features["ret60"] * 100).round(2)
    features["amount_ratio"] = features["amount_ratio"].round(2)
    features["vol20"] = (features["vol20"] * 100).round(2)
    features["drawdown60"] = (features["drawdown60"] * 100).round(2)
    for column in [
        "position250_pct",
        "position60_pct",
        "dist_ma20_pct",
        "dist_ma60_pct",
        "dist_ma120_pct",
        "dist_high250_pct",
        "dist_support20_pct",
        "dist_resistance20_pct",
        "rsi14",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "boll_upper",
        "boll_mid",
        "boll_lower",
        "boll_position",
        "close_in_day_pct",
        "amplitude_pct",
    ]:
        features[column] = features[column].round(2)
    features["amount_yi"] = (features["amount_yuan"] / 100_000_000).round(2)
    return features.sort_values(["score", "amount_yuan"], ascending=False)



def market_summary(spot: pd.DataFrame) -> dict:
    pct = pd.to_numeric(spot["涨跌幅"], errors="coerce")
    up_ratio = float((pct > 0).mean()) if len(pct) else np.nan
    median_pct = float(pct.median()) if len(pct) else np.nan
    amount_sum = float(pd.to_numeric(spot["成交额"], errors="coerce").sum())
    if up_ratio >= 0.55 and median_pct > 0:
        regime = "偏强"
    elif up_ratio <= 0.4 and median_pct < 0:
        regime = "偏弱"
    else:
        regime = "震荡"
    return {
        "up_ratio": up_ratio,
        "median_pct": median_pct,
        "amount_sum": amount_sum,
        "regime": regime,
    }


def market_temperature(spot: pd.DataFrame, features: pd.DataFrame) -> dict:
    pct = pd.to_numeric(spot["涨跌幅"], errors="coerce")
    up_ratio = float((pct > 0).mean()) if len(pct) else 0.0
    limit_up_count = int((pct >= 9.8).sum()) if len(pct) else 0
    limit_down_count = int((pct <= -9.8).sum()) if len(pct) else 0
    ma20_ratio = float((features["close"] > features["ma20"]).mean()) if not features.empty else 0.0
    strong_ratio = float((features["score"] >= 80).mean()) if not features.empty else 0.0
    overheated_ratio = float(
        (
            (pd.to_numeric(features.get("rsi14"), errors="coerce") >= 70)
            | (pd.to_numeric(features.get("position250_pct"), errors="coerce") >= 88)
            | (features.get("news_badge", pd.Series("", index=features.index)).astype(str).eq("消息过热"))
        ).mean()
    ) if not features.empty else 0.0
    risk_ratio = float((pd.to_numeric(features.get("risk_pct"), errors="coerce") > 12).mean()) if not features.empty else 0.0
    temp_score = (
        up_ratio * 35
        + ma20_ratio * 30
        + min(strong_ratio, 0.35) / 0.35 * 20
        + min(limit_up_count / max(len(features), 1), 0.08) / 0.08 * 15
        - min(limit_down_count / max(len(features), 1), 0.05) / 0.05 * 12
    )
    temp_score = max(0.0, min(100.0, temp_score))
    if temp_score >= 75 and overheated_ratio >= 0.35:
        label = "偏热，防追高"
    elif temp_score >= 70:
        label = "偏强"
    elif temp_score >= 45:
        label = "震荡"
    elif temp_score >= 30:
        label = "偏弱"
    else:
        label = "冰点/谨慎"
    return {
        "temp_score": round(temp_score, 1),
        "label": label,
        "up_ratio": up_ratio,
        "ma20_ratio": ma20_ratio,
        "strong_ratio": strong_ratio,
        "overheated_ratio": overheated_ratio,
        "risk_ratio": risk_ratio,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
    }


def market_temperature_notes(temp: dict) -> list[str]:
    return [
        f"市场温度：{fmt_value(temp.get('temp_score'), 1)} / 100，状态：{temp.get('label', '-')}",
        f"上涨家数占比：{fmt_pct(temp.get('up_ratio'))}；样本中站上MA20比例：{fmt_pct(temp.get('ma20_ratio'))}",
        f"强关注比例：{fmt_pct(temp.get('strong_ratio'))}；过热比例：{fmt_pct(temp.get('overheated_ratio'))}",
        f"涨停样本数：{temp.get('limit_up_count', 0)}；跌停样本数：{temp.get('limit_down_count', 0)}",
        "用法：市场偏弱时少做打板和追高；市场偏热时看似机会多，但更要等回踩和二次确认。",
    ]


def stock_code(value: object) -> str:
    return explanation_text.stock_code(value)


def row_float(row: pd.Series, key: str, default: float = np.nan) -> float:
    return explanation_text.row_float(row, key, default)


def attention_type(row: pd.Series) -> str:
    return explanation_text.attention_type(row)


def operation_advice(row: pd.Series) -> str:
    return explanation_text.operation_advice(row)


def brief_action(row: pd.Series) -> str:
    return explanation_text.brief_action(row)


def beginner_reason(row: pd.Series) -> str:
    return explanation_text.beginner_reason(row)


def tonghuashun_steps(row: pd.Series) -> list[str]:
    return explanation_text.tonghuashun_steps(row)


def indicator_table_rows(row: pd.Series) -> list[tuple[str, str, str]]:
    return explanation_text.indicator_table_rows(row)


def stock_learning_notes(row: pd.Series) -> list[str]:
    return explanation_text.stock_learning_notes(row)


def trade_checklist(row: pd.Series, temp: dict) -> list[tuple[str, str, str]]:
    return explanation_text.trade_checklist(row, temp)


def trade_plan(row: pd.Series, temp: dict) -> str:
    return explanation_text.trade_plan(row, temp)


def action_script_lines(row: pd.Series, temp: dict) -> list[str]:
    return explanation_text.action_script_lines(row, temp)


def build_pre_trade_checklist(row: pd.Series, market_temp: dict | None = None, account_config: dict | None = None) -> list[str]:
    return explanation_text.build_pre_trade_checklist(row, market_temp, account_config)


def build_invalid_conditions(row: pd.Series, market_temp: dict | None = None) -> list[str]:
    return explanation_text.build_invalid_conditions(row, market_temp)


def explain_candidate(row: pd.Series) -> list[str]:
    return explanation_text.explain_candidate(row)


def explain_risk_flags(row: pd.Series) -> list[str]:
    return explanation_text.explain_risk_flags(row)


def explain_data_warning(row: pd.Series) -> list[str]:
    return explanation_text.explain_data_warning(row)


def build_position_notes(row: pd.Series, account_config: dict | None = None) -> list[str]:
    return explanation_text.build_position_notes(row, account_config)


def build_ths_learning_notes(row: pd.Series) -> list[str]:
    return explanation_text.build_ths_learning_notes(row)


def build_candidate_decision_card(row: pd.Series) -> dict:
    return explanation_text.build_candidate_decision_card(row)


def build_today_decision_brief(
    market_temp: dict | None,
    data_quality_summary: dict | None,
    backtest_risk_summary: dict | None,
    candidates: pd.DataFrame | list[pd.DataFrame] | None = None,
) -> dict:
    return explanation_text.build_today_decision_brief(
        market_temp,
        data_quality_summary,
        backtest_risk_summary,
        candidates,
    )


def render_compact_candidate_card(card: dict, max_bullets: int = 3) -> str:
    return explanation_text.render_compact_candidate_card(card, max_bullets)


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def clean_news_value(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return " ".join(str(value).replace("\u3000", " ").split())


def fetch_stock_news(symbol: str, max_items: int) -> tuple[list[dict[str, str]], str]:
    cache_path = NEWS_DIR / f"{datetime.now():%Y%m%d}_{symbol}.csv"
    try:
        news = fetch_news_eastmoney(symbol)
        if not news.empty:
            news.head(max_items).to_csv(cache_path, index=False, encoding="utf-8-sig")
        source = "实时新闻"
    except Exception as exc:
        logging.warning("News fetch failed for %s: %s", symbol, exc)
        if cache_path.exists():
            news = read_csv_frame(cache_path)
            for warning in news.attrs.get("warnings", []):
                logging.warning("news cache csv warning %s: %s", cache_path, warning)
            source = "新闻缓存"
        else:
            return [], "新闻获取失败"

    if news.empty:
        return [], "暂无新闻"

    items: list[dict[str, str]] = []
    for _, row in news.head(max_items).iterrows():
        items.append(
            {
                "title": clean_news_value(row.get("新闻标题")),
                "content": clean_news_value(row.get("新闻内容")),
                "published": clean_news_value(row.get("发布时间")),
                "source": clean_news_value(row.get("文章来源")),
                "url": clean_news_value(row.get("新闻链接")),
            }
        )
    return items, source


def analyze_news(items: list[dict[str, str]], source_status: str) -> dict[str, object]:
    if not items:
        return {
            "news_badge": source_status,
            "news_score": 0,
            "news_positive": 0,
            "news_negative": 0,
            "news_heat": 0,
            "news_risk": "没有抓到可用新闻，不能把消息面作为观察依据。",
            "news_titles": "",
        }

    full_text = " ".join(f"{item['title']} {item['content']}" for item in items)
    positive = keyword_hits(full_text, NEWS_POSITIVE_KEYWORDS)
    negative = keyword_hits(full_text, NEWS_NEGATIVE_KEYWORDS)
    heat = keyword_hits(full_text, NEWS_HEAT_KEYWORDS)
    news_score = max(-8, min(4, len(positive) - len(negative) * 2))

    if negative:
        badge = "消息有风险"
        risk = "标题或正文出现负面关键词：" + "、".join(negative[:5]) + "。需要先核实公告和原因。"
    elif len(heat) >= 2:
        badge = "消息过热"
        risk = "新闻集中在涨停、龙虎榜或异动，短线热度高，新手尤其不能追高。"
    elif positive:
        badge = "消息偏利好"
        risk = "出现利好类关键词，但仍要看股价位置、成交量和公告原文，不能只看标题。"
    else:
        badge = "消息中性"
        risk = "未发现明显利好或负面关键词，主要按技术位置和风控规则判断。"

    titles = []
    for item in items[:3]:
        date = item["published"][:10] if item["published"] else "日期未知"
        source = item["source"] or "来源未知"
        title = item["title"] or "无标题"
        titles.append(f"{date} {source}：{title}")

    return {
        "news_badge": badge,
        "news_score": news_score,
        "news_positive": len(positive),
        "news_negative": len(negative),
        "news_heat": len(heat),
        "news_risk": risk,
        "news_titles": "；".join(titles),
    }


def refresh_signal_labels(features: pd.DataFrame) -> pd.DataFrame:
    features["long_signal"] = np.select(
        [
            features["long_score"] >= 80,
            features["long_score"] >= 70,
            features["long_score"] >= 60,
        ],
        ["长期强候选", "长期观察", "长期弱关注"],
        default="无",
    )
    features["board_signal"] = np.select(
        [
            features["board_score"] >= 80,
            features["board_score"] >= 70,
            features["board_score"] >= 60,
        ],
        ["强势接力候选", "打板观察", "短线弱关注"],
        default="无",
    )
    return features


def enrich_with_news(
    features: pd.DataFrame,
    news_limit: int,
    news_items: int,
    sleep_seconds: float,
) -> pd.DataFrame:
    if features.empty:
        return features

    features = features.copy()
    defaults: dict[str, object] = {
        "news_badge": "消息字段缺失",
        "news_score": 0,
        "news_positive": 0,
        "news_negative": 0,
        "news_heat": 0,
        "news_risk": "本轮未确认消息状态。",
        "news_titles": "",
        "news_status": "消息字段缺失",
        "latest_news_title": "",
        "news_warning_level": "INFO",
        "news_warning_message": "消息字段缺失",
    }
    for column, default in defaults.items():
        features[column] = default

    if news_limit <= 0 or news_items <= 0:
        for _, row in features.iterrows():
            record_news_health(provider_not_configured_status(stock_code(row.get("code")), str(row.get("name", ""))))
        features = attach_news_health(features, news_health_frame())
        return features

    pool = features.copy()
    pool["_news_rank"] = pool[["score", "long_score", "board_score"]].max(axis=1)
    symbols = pool.sort_values(["_news_rank", "amount_yuan"], ascending=False)["code"].head(news_limit)
    for symbol in symbols:
        code = stock_code(symbol)
        items, source_status = fetch_stock_news(code, news_items)
        name = str(features.loc[features["code"].astype(str).str.zfill(6) == code, "name"].iloc[0]) if (features["code"].astype(str).str.zfill(6) == code).any() else ""
        if source_status == "新闻获取失败":
            record_news_health(news_failure_status(code, name, "eastmoney", source_status))
        else:
            record_news_health(news_status_from_items(code, name, source_status, items))
        analysis = analyze_news(items, source_status)
        mask = features["code"].astype(str).str.zfill(6) == code
        for column, value in analysis.items():
            features.loc[mask, column] = value
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    fetched = {status.symbol for status in NEWS_HEALTH_ROWS}
    for _, row in features.iterrows():
        code = stock_code(row.get("code"))
        if code not in fetched:
            record_news_health(provider_not_configured_status(code, str(row.get("name", ""))))
    features = attach_news_health(features, news_health_frame())

    news_score = pd.to_numeric(features["news_score"], errors="coerce").fillna(0)
    long_adjust = news_score.clip(-6, 3) * 0.7
    board_adjust = news_score.clip(-6, 3)
    board_adjust += np.where(features["news_badge"].eq("消息过热"), 1.5, 0)
    long_adjust -= np.where(features["news_badge"].eq("消息过热"), 2.0, 0)
    features["long_score"] = (features["long_score"] + long_adjust).clip(0, 100).round(1)
    features["board_score"] = (features["board_score"] + board_adjust).clip(0, 100).round(1)
    return refresh_signal_labels(features)


def position_summary(row: pd.Series) -> str:
    return explanation_text.position_summary(row)


def news_bullets(row: pd.Series) -> list[str]:
    return explanation_text.news_bullets(row)


def risk_flags(row: pd.Series) -> list[str]:
    return explanation_text.risk_flags(row)
def make_watchlist(features: pd.DataFrame, top_n: int) -> pd.DataFrame:
    if features.empty:
        return features
    watch = features.copy()
    watch["watch_score"] = watch[["long_score", "board_score"]].max(axis=1)
    watch["attention_type"] = watch.apply(attention_type, axis=1)
    watch = watch.sort_values(["watch_score", "amount_yuan"], ascending=False)
    return watch.drop_duplicates("code").head(top_n)


def update_long_pool(features: pd.DataFrame, args: argparse.Namespace, report_date: str) -> pd.DataFrame:
    ensure_dirs()
    columns = [
        "code",
        "name",
        "first_seen",
        "last_seen",
        "days_tracked",
        "pool_status",
        "pool_reason",
        "long_score",
        "close",
        "stop",
        "risk_pct",
        "position_label",
        "position250_pct",
        "ma_stack_signal",
        "news_badge",
        "ret20",
        "ret60",
        "amount_yi",
    ]
    if features.empty:
        return pd.DataFrame(columns=columns)

    current = features.copy()
    current["code"] = current["code"].astype(str).str.zfill(6)
    current = current.sort_values(["long_score", "amount_yuan"], ascending=False)

    if LONG_POOL_PATH.exists():
        pool = read_csv_frame(LONG_POOL_PATH, dtype={"code": str})
        pool["code"] = pool["code"].astype(str).map(_normalize_symbol)
        pool = pool[pool["code"].astype(str).str.fullmatch(r"\d{6}", na=False)].copy()
    else:
        pool = pd.DataFrame(columns=columns)

    records: dict[str, dict] = {}
    for _, row in pool.iterrows():
        code = str(row.get("code", "")).zfill(6)
        if code.strip("0"):
            records[code] = row.to_dict()

    top_add = current.head(max(args.long_pool_size, args.top_n))["code"].tolist()
    strong = current[current["long_score"] >= args.long_pool_add_score]["code"].tolist()
    keep_candidates = set(top_add + strong)

    for code in keep_candidates:
        row = current[current["code"] == code].iloc[0]
        existing = records.get(code, {})
        records[code] = {
            **existing,
            "code": code,
            "name": row.get("name"),
            "first_seen": existing.get("first_seen") or report_date,
            "last_seen": report_date,
            "days_tracked": int(existing.get("days_tracked", 0) or 0) + (0 if existing.get("last_seen") == report_date else 1),
        }

    updated = []
    current_by_code = {row["code"]: row for _, row in current.iterrows()}
    for code, record in records.items():
        row = current_by_code.get(code)
        if row is None:
            record["pool_status"] = "数据缺失"
            record["pool_reason"] = "今天没有进入样本池，暂不做长期判断。"
        else:
            score = float(row.get("long_score", 0) or 0)
            close = float(row.get("close", 0) or 0)
            stop = float(row.get("stop", 0) or 0)
            ma20 = float(row.get("ma20", 0) or 0)
            ma60 = float(row.get("ma60", 0) or 0)
            risk_pct = float(row.get("risk_pct", 0) or 0)
            if close < stop or close < ma60 or score < args.long_pool_drop_score:
                record["pool_status"] = "剔除观察"
                record["pool_reason"] = "跌破风控线/MA60或长期分明显走弱，不再按长期票处理。"
            elif close < ma20 or risk_pct > 13 or score < args.long_pool_keep_score:
                record["pool_status"] = "降级观察"
                record["pool_reason"] = "趋势或风险距离变差，暂时保留但不新增仓位。"
            elif code in keep_candidates and record.get("first_seen") == report_date:
                record["pool_status"] = "新增观察"
                record["pool_reason"] = "首次进入长期观察池，先观察回踩和持续性。"
            else:
                record["pool_status"] = "继续观察"
                record["pool_reason"] = "长期结构仍然可跟踪，重点看MA20/MA60和止损线是否守住。"
            for key in [
                "name",
                "long_score",
                "close",
                "stop",
                "risk_pct",
                "position_label",
                "position250_pct",
                "ma_stack_signal",
                "news_badge",
                "ret20",
                "ret60",
                "amount_yi",
            ]:
                record[key] = row.get(key)
            record["last_seen"] = report_date
        updated.append(record)

    result = pd.DataFrame(updated)
    if result.empty:
        return pd.DataFrame(columns=columns)
    active = result[~result["pool_status"].eq("剔除观察")].copy()
    removed = result[result["pool_status"].eq("剔除观察")].copy()
    active = active.sort_values(["long_score", "days_tracked"], ascending=False).head(args.long_pool_size)
    removed = removed.sort_values(["last_seen"], ascending=False).head(10)
    result = pd.concat([active, removed], ignore_index=True)
    result = result[[column for column in columns if column in result.columns]]
    result.to_csv(LONG_POOL_PATH, index=False, encoding="utf-8-sig")
    return result


def add_indicator_table(document, row: pd.Series) -> None:
    return _word_add_indicator_table(document, row, indicator_table_rows)


def add_trade_checklist_table(document, row: pd.Series, temp: dict) -> None:
    return _word_add_trade_checklist_table(document, row, temp, trade_checklist)


def word_report_helpers() -> dict:
    return {
        "market_summary": market_summary,
        "market_temperature": market_temperature,
        "make_watchlist": make_watchlist,
        "market_temperature_notes": market_temperature_notes,
        "stock_code": stock_code,
        "position_summary": position_summary,
        "brief_action": brief_action,
        "beginner_reason": beginner_reason,
        "news_bullets": news_bullets,
        "operation_advice": operation_advice,
        "trade_checklist": trade_checklist,
        "trade_plan": trade_plan,
        "action_script_lines": action_script_lines,
        "indicator_table_rows": indicator_table_rows,
        "risk_flags": risk_flags,
        "tonghuashun_steps": tonghuashun_steps,
        "stock_learning_notes": stock_learning_notes,
        "build_pre_trade_checklist": build_pre_trade_checklist,
        "build_invalid_conditions": build_invalid_conditions,
        "explain_candidate": explain_candidate,
        "explain_risk_flags": explain_risk_flags,
        "explain_data_warning": explain_data_warning,
        "build_candidate_decision_card": build_candidate_decision_card,
        "build_today_decision_brief": build_today_decision_brief,
        "today_decision_brief_lines": explanation_text.today_decision_brief_lines,
        "render_compact_candidate_card": render_compact_candidate_card,
        "report_toc_lines": explanation_text.report_toc_lines,
        "build_learning_appendix": explanation_text.build_learning_appendix,
        "risk_statement_markdown": explanation_text.risk_statement_markdown,
    }


def build_word_report(
    features: pd.DataFrame,
    spot: pd.DataFrame,
    metrics: BacktestMetrics,
    args: argparse.Namespace,
    report_date: str,
    backtest_files: dict[str, Path] | None = None,
) -> Path:
    return _build_word_report(
        features,
        spot,
        metrics,
        args,
        report_date,
        backtest_files,
        report_dir=REPORT_DIR,
        helpers=word_report_helpers(),
    )
def write_report(
    features: pd.DataFrame,
    spot: pd.DataFrame,
    metrics: BacktestMetrics,
    args: argparse.Namespace,
    backtest_files: dict[str, Path] | None = None,
    spot_meta: dict[str, object] | None = None,
) -> Path:
    run_stamp = datetime.now()
    report_date = run_stamp.strftime("%Y-%m-%d")
    features = features.copy()
    report_defaults: dict[str, object] = {
        "position_label": "位置未知",
        "position250_pct": np.nan,
        "position60_pct": np.nan,
        "dist_ma20_pct": np.nan,
        "dist_high250_pct": np.nan,
        "ma5": np.nan,
        "ma10": np.nan,
        "ma20": np.nan,
        "ma60": np.nan,
        "ma120": np.nan,
        "support20": np.nan,
        "resistance20": np.nan,
        "dist_support20_pct": np.nan,
        "dist_resistance20_pct": np.nan,
        "atr14": np.nan,
        "rsi14": np.nan,
        "kdj_k": np.nan,
        "kdj_d": np.nan,
        "kdj_j": np.nan,
        "boll_upper": np.nan,
        "boll_mid": np.nan,
        "boll_lower": np.nan,
        "boll_position": np.nan,
        "close_in_day_pct": np.nan,
        "amplitude_pct": np.nan,
        "ma_stack_signal": "未知",
        "rsi_signal": "未知",
        "kdj_signal": "未知",
        "boll_signal": "未知",
        "candle_signal": "未知",
        "position_note": "历史区间不足，暂时不判断高低位置。",
        "news_badge": "消息字段缺失",
        "news_score": 0,
        "news_positive": 0,
        "news_negative": 0,
        "news_heat": 0,
        "news_risk": "本轮未确认消息状态。",
        "news_titles": "",
        "news_status": "消息字段缺失",
        "latest_news_title": "",
        "news_warning_level": "INFO",
        "news_warning_message": "消息字段缺失",
        "data_warning_level": "OK",
        "data_risk_flag": "",
        "data_invalid": False,
        "data_effective_source": "",
        "data_latest_date": "",
    }
    for column, default in report_defaults.items():
        if column not in features.columns:
            features[column] = default
    health = data_health_frame()
    features = attach_data_health(features, health)
    news_health = news_health_frame()
    features = attach_news_health(features, news_health)
    data_grade_value = features.get("data_grade", pd.Series("SCORABLE", index=features.index))
    if isinstance(data_grade_value, pd.DataFrame):
        data_grade_value = data_grade_value.iloc[:, 0]
    data_grade = data_grade_value.astype(str).str.upper()
    valid_features = features[(~features["data_invalid"].fillna(False)) & ~data_grade.eq("UNAVAILABLE")].copy()
    valid_grade_value = valid_features.get("data_grade", pd.Series("SCORABLE", index=valid_features.index))
    if isinstance(valid_grade_value, pd.DataFrame):
        valid_grade_value = valid_grade_value.iloc[:, 0]
    scorable_features = valid_features[valid_grade_value.astype(str).str.upper().isin(["SCORABLE", "RESEARCH_READY", "BACKTEST_READY"])].copy()
    candidates = scorable_features[scorable_features["score"] >= args.score_threshold].copy()
    long_candidates = scorable_features[scorable_features["long_score"] >= args.score_threshold].copy()
    board_candidates = scorable_features[scorable_features["board_score"] >= args.score_threshold].copy()
    watchlist = make_watchlist(valid_features, args.top_n)
    long_pool = update_long_pool(scorable_features, args, report_date)
    candidate_path = REPORT_DIR / f"{report_date}_candidates.csv"
    long_candidate_path = REPORT_DIR / f"{report_date}_long_candidates.csv"
    board_candidate_path = REPORT_DIR / f"{report_date}_board_candidates.csv"
    watchlist_path = REPORT_DIR / f"{report_date}_watchlist.csv"
    long_pool_path = REPORT_DIR / f"{report_date}_long_pool.csv"
    candidate_path = save_csv_safely(candidates, candidate_path)
    long_candidate_path = save_csv_safely(
        long_candidates.sort_values(["long_score", "amount_yuan"], ascending=False),
        long_candidate_path,
    )
    board_candidate_path = save_csv_safely(
        board_candidates.sort_values(["board_score", "amount_yuan"], ascending=False),
        board_candidate_path,
    )
    watchlist_path = save_csv_safely(watchlist, watchlist_path)
    long_pool_path = save_csv_safely(long_pool, long_pool_path)

    top = candidates.head(args.top_n)
    long_top = long_candidates.sort_values(["long_score", "amount_yuan"], ascending=False).head(args.top_n)
    board_top = board_candidates.sort_values(["board_score", "amount_yuan"], ascending=False).head(args.top_n)
    summary = market_summary(spot)
    temperature = market_temperature(spot, features)
    latest_data_date = features["date"].max() if not features.empty else ""
    latest_data_date_text = pd.Timestamp(latest_data_date).strftime("%Y-%m-%d") if pd.notna(latest_data_date) else "N/A"

    columns = [
        "code",
        "name",
        "date",
        "close",
        "change_pct",
        "score",
        "signal",
        "position_label",
        "position250_pct",
        "data_warning_level",
        "data_grade",
        "data_grade_label",
        "data_risk_flag",
        "news_badge",
        "news_status",
        "stop",
        "target1",
        "risk_pct",
        "ret20",
        "ret60",
        "amount_yi",
        "buy_trigger",
        "sell_rule",
    ]
    table_md = table_or_message(top, columns, "No candidates.")

    long_columns = [
        "code",
        "name",
        "date",
        "close",
        "long_score",
        "long_signal",
        "position_label",
        "position250_pct",
        "data_warning_level",
        "data_risk_flag",
        "news_badge",
        "news_status",
        "stop",
        "target1",
        "risk_pct",
        "ret20",
        "ret60",
        "amount_yi",
        "long_buy_condition",
        "long_avoid_condition",
    ]
    board_columns = [
        "code",
        "name",
        "date",
        "close",
        "change_pct",
        "board_score",
        "board_signal",
        "position_label",
        "position60_pct",
        "data_warning_level",
        "data_risk_flag",
        "news_badge",
        "news_status",
        "stop",
        "risk_pct",
        "amount_ratio",
        "ret20",
        "amount_yi",
        "board_buy_condition",
        "board_avoid_condition",
    ]
    long_table_md = table_or_message(long_top, long_columns, "No long candidates.")
    board_table_md = table_or_message(board_top, board_columns, "No board candidates.")
    long_pool_columns = [
        "code",
        "name",
        "pool_status",
        "days_tracked",
        "long_score",
        "close",
        "stop",
        "risk_pct",
        "position_label",
        "pool_reason",
    ]
    active_long_pool = long_pool[~long_pool["pool_status"].eq("剔除观察")].head(args.top_n) if not long_pool.empty else long_pool
    long_pool_md = table_or_message(active_long_pool, long_pool_columns, "No long pool names.")
    health_summary = data_health_summary(health)
    reason_summary = data_health_reason_summary(health)
    news_summary = news_health_summary(news_health)
    health_path = save_csv_safely(health, REPORT_DIR / f"{report_date}_data_health.csv")
    news_health_path = save_csv_safely(news_health, REPORT_DIR / f"{report_date}_news_health.csv")
    data_risk_note = data_quality_risk_note(health_summary)
    data_risk_block = f"\n> {data_risk_note}\n" if data_risk_note else ""
    backtest_warning = backtest_cost_warning(metrics.assumptions)
    assumptions = metrics.assumptions or default_backtest_assumptions()
    backtest_files = backtest_files or {}
    risk_summary = risk_summary_dict(assumptions.get("backtest_risk_summary", {}))
    data_quality_md = build_data_quality_markdown(health_summary, DATA_STALE_WARNING_DAYS, health_path.name)
    calendar = load_cn_trading_calendar()
    calendar_status = settlement_status_from_quote(latest_data_date if latest_data_date != "N/A" else None, run_stamp, calendar)
    expected_market_date = expected_settlement_date(run_stamp, calendar)
    previous_market_date = previous_cn_a_share_trading_day(run_stamp.date(), calendar)
    sample_warning_note = (
        "样例模式下部分 WARNING 用于演示风险面板，不代表真实行情数据损坏。"
        if getattr(args, "config_path", "") == "sample-mode"
        else ""
    )
    backtest_audit_md = build_backtest_audit_markdown(
        metrics,
        assumptions,
        backtest_files,
        backtest_warning=backtest_warning,
        backtest_mode=getattr(args, "backtest_mode", "portfolio"),
        config_path=getattr(args, "config_path", "config/default.yaml"),
        risk_summary=risk_summary,
    )
    backtest_risk_summary_md = explanation_text.build_backtest_risk_summary_markdown(
        risk_summary_note_lines(risk_summary)
    )
    today_brief = explanation_text.build_today_decision_brief(
        temperature,
        health_summary,
        risk_summary,
        [top, long_top, board_top],
    )
    today_brief_md = explanation_text.build_today_decision_brief_markdown(today_brief)
    report_toc_md = explanation_text.build_report_toc_markdown()
    decision_card_top_n = int(getattr(args, "decision_card_top_n", 5) or 0)
    max_card_bullets = int(getattr(args, "max_card_bullets", 3) or 3)
    candidate_review_md = explanation_text.build_candidate_review_markdown(
        [
            ("Top Candidates", top),
        ],
        temperature,
        limit_per_section=min(decision_card_top_n, args.top_n),
        max_bullets=max_card_bullets,
    )
    learning_appendix_md = (
        explanation_text.build_learning_appendix(top, int(getattr(args, "learning_appendix_max_items", 20) or 20))
        if bool(getattr(args, "include_learning_appendix", True))
        else ""
    )
    risk_statement_md = explanation_text.risk_statement_markdown()
    report_config_md = "\n".join(
        [
            "## Report Configuration / 报告长度控制",
            "",
            f"- decision_card_top_n: {decision_card_top_n}",
            f"- max_card_bullets: {max_card_bullets}",
            f"- include_learning_appendix: {bool(getattr(args, 'include_learning_appendix', True))}",
            f"- learning_appendix_max_items: {int(getattr(args, 'learning_appendix_max_items', 20) or 20)}",
        ]
    )
    spot_meta = dict(spot_meta or {})
    provider_errors = [str(item) for item in (spot_meta.get("provider_errors") or []) if str(item).strip()]
    spot_snapshot_md = "\n".join(
        [
            "## Spot Snapshot Source / 行情快照来源",
            "",
            f"- spot_snapshot_source: {spot_meta.get('spot_snapshot_source', '') or 'N/A'}",
            f"- universe_scope: {spot_meta.get('universe_scope', '') or 'N/A'}",
            f"- latest_date: {spot_meta.get('latest_date', '') or 'N/A'}",
            f"- cache_fetched_at: {spot_meta.get('cache_fetched_at', '') or spot_meta.get('fetched_at', '') or 'N/A'}",
            f"- data_freshness_warning: {spot_meta.get('data_freshness_warning', '') or 'N/A'}",
            "- provider_errors:",
            *([f"  - {item}" for item in provider_errors] or ["  - N/A"]),
        ]
    )
    reading_guide_md = "\n".join(
        [
            "## 今日阅读指南",
            "",
            "1. 先检查 Data Quality：是否出现 WARNING / ERROR，以及原因是否只是休市、等待收盘或样例演示。",
            "2. 再看候选股准备状态：是否只是观察、虚拟验证，还是进入深度复核。",
            "3. 留意市场日历提示：今天是否为休市日、盘中估算或等待行情源更新。",
            "4. 最后查看风险摘要和虚拟持仓，确认是否只是观察记录。",
            "",
            "> 可信度评分不代表价格方向预测，准备状态不是买卖建议；本报告仅用于辅助观察和复盘。",
        ]
    )
    review_notes_md = "\n".join(
        [
            "## 今日复盘笔记",
            "",
            "*（请手动填写）*",
            "",
            "- 今天观察了什么：",
            "- 感到困惑的地方：",
            "- 明天打算观察：",
        ]
    )

    report = f"""# A-share Daily Quant Report

Generated: {run_stamp:%Y-%m-%d %H:%M:%S}

This report is for research only. It is not investment advice and it does not place trades.
{data_risk_block}

{reading_guide_md}

{report_toc_md}

{today_brief_md}

## Decision Page / 决策页

### Data Quality Summary / 数据质量摘要

- Total data checks: {health_summary["total"]}
- ERROR rows: {health_summary["error"]}
- WARNING rows: {health_summary["warning"]}
- Fallback used: {health_summary["fallback_used"]}
- Served from cache: {health_summary["served_from_cache"]}
- BACKTEST_READY: {health_summary.get("BACKTEST_READY", 0)}
- RESEARCH_READY: {health_summary.get("RESEARCH_READY", 0)}
- SCORABLE: {health_summary.get("SCORABLE", 0)}
- OBSERVE_ONLY: {health_summary.get("OBSERVE_ONLY", 0)}
- UNAVAILABLE: {health_summary.get("UNAVAILABLE", 0)}
- Backtest data gate: {assumptions.get("data_grade_gate_note", "数据等级门禁未提供。")}

### Market Calendar Status / 市场日历状态

- Market: CN_A_SHARE
- Market session: {calendar_status.get("market_session", "")}
- Is market holiday: {calendar_status.get("is_market_holiday", False)}
- Expected latest trading date: {expected_market_date.isoformat() if expected_market_date else ""}
- Previous trading day: {previous_market_date.isoformat()}
- Calendar note: {calendar_status.get("calendar_note", "")}
- Data freshness note: {calendar_status.get("message", "")}
- Sample note: {sample_warning_note or "N/A"}

{spot_snapshot_md}

### Data Health Reason Breakdown / 数据健康原因分布

- 假期休市，使用最近交易日: {reason_summary["market_holiday_using_last_trading_day"]}
- 等待今日收盘: {reason_summary["waiting_for_close"]}
- 等待行情源更新: {reason_summary["waiting_for_quote_update"]}
- 数据早于预期交易日: {reason_summary["stale"]}
- 使用缓存: {reason_summary["cache_only"]}
- fallback 数据: {reason_summary["fallback_used"]}
- 抓取失败: {reason_summary["fetch_failed"]}
- 无数据: {reason_summary["no_data"]}
- 其他: {reason_summary["other"]}

### News Quality Summary / 消息抓取状态摘要

- Total news checks: {news_summary["total"]}
- Attempted: {news_summary["attempted"]}
- Skipped: {news_summary["skipped"]}
- No news: {news_summary["no_news"]}
- Failed: {news_summary["failed"]}
- Sample mode skipped: {news_summary["sample_mode_skipped"]}
- WARNING rows: {news_summary["warning"]}
- ERROR rows: {news_summary["error"]}
- News health file: {news_health_path.name}

{backtest_risk_summary_md}

{candidate_review_md}

## Long / Board Candidate Tables / 候选简表

### Long-Term Candidates

{long_table_md}

### Board / Short-Term Candidates

{board_table_md}

## Watch / Long Pool Notes / 持仓或观察提醒

This is the stable long-term watch pool. It changes slower than the daily candidate list.

{long_pool_md}

## Market Context / 市场背景

- Data date: {latest_data_date_text}
- Selected universe size: {len(features)}
- Candidate count: {len(candidates)}
- Long candidate count: {len(long_candidates)}
- Board candidate count: {len(board_candidates)}
- Max price filter: {args.max_price if args.max_price else "None"} 元
- Market regime: {summary["regime"]}
- Market temperature: {temperature["temp_score"]} / 100 ({temperature["label"]})
- Advancing ratio: {fmt_pct(summary["up_ratio"])}
- Median change: {summary["median_pct"]:.2f}%
- Universe turnover: {summary["amount_sum"] / 100_000_000:.2f} 亿元
- Above MA20 ratio: {fmt_pct(temperature["ma20_ratio"])}
- Overheated ratio: {fmt_pct(temperature["overheated_ratio"])}

## Top Candidates Table / Top 候选简表

{table_md}

{backtest_audit_md}

## Data Quality Details / 数据质量详情

{data_quality_md}

{learning_appendix_md}

{risk_statement_md}

{report_config_md}

{review_notes_md}

## Method

- Data source: AKShare end-of-day A-share interfaces.
- Universe filter: Shanghai/Shenzhen stocks, non-ST, liquid names above the configured amount threshold.
- Signal score: trend, momentum, breakout proximity, volume confirmation, volatility, drawdown, and relative strength.
- Position filter: one-year and 60-day range percentile, distance from MA20/MA60/MA120, and distance from the one-year high.
- News filter: recent Eastmoney stock news via AKShare, with keyword checks for positive, negative, and overheated headlines.
- Long score: trend durability, 60-day strength, volatility control, drawdown, liquidity, and non-overheated entry.
- Board score: near-limit strength, short-term momentum, volume expansion, liquidity, and trend support.
- Buy trigger: wait for the next trading day to confirm the condition; do not chase limit-up names.
- Sell/risk rule: use the listed stop level, MA20 trend break, or score deterioration.
"""
    report_path = REPORT_DIR / f"{report_date}_quant_report.md"
    report_path.write_text(report, encoding="utf-8-sig")
    app_config = args.loaded_config if isinstance(getattr(args, "loaded_config", None), dict) else {}
    app_section = app_config.get("app") if isinstance(app_config.get("app"), dict) else {}
    report_meta = {
        "schema_version": str(app_section.get("schema_version") or "2.3.0"),
        "app_version": str(app_section.get("app_version") or "2.3.0-cn-a-share-market-rules"),
        "generated_at": run_stamp.isoformat(timespec="seconds"),
        "report_generated_at": run_stamp.isoformat(timespec="seconds"),
        "report_date": report_date,
        "latest_date": latest_data_date_text,
        "data_mode": "report",
        "spot_snapshot_meta": spot_meta,
        "spot_snapshot_source": spot_meta.get("spot_snapshot_source", ""),
        "universe_scope": spot_meta.get("universe_scope", ""),
        "provider_errors": spot_meta.get("provider_errors", []),
        "data_freshness_warning": spot_meta.get("data_freshness_warning", ""),
        "cache_fetched_at": spot_meta.get("cache_fetched_at", "") or spot_meta.get("fetched_at", ""),
        "payload_loaded_at": run_stamp.isoformat(timespec="seconds"),
        **local_daily_cache_stats_payload(),
    }
    atomic_write_json(REPORT_DIR / f"{report_date}_quant_report_meta.json", report_meta)
    atomic_write_json(REPORT_DIR / "latest_quant_report_meta.json", report_meta)
    docx_path = build_word_report(features, spot, metrics, args, report_date, backtest_files)
    logging.info("Saved candidates: %s", candidate_path)
    logging.info("Saved long candidates: %s", long_candidate_path)
    logging.info("Saved board candidates: %s", board_candidate_path)
    logging.info("Saved watchlist: %s", watchlist_path)
    logging.info("Saved long pool: %s", long_pool_path)
    logging.info("Saved data health: %s", health_path)
    logging.info("Saved report: %s", report_path)
    logging.info("Saved Word report: %s", docx_path)
    return report_path


def effective_backtest_config_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "initial_cash": args.initial_cash,
        "commission_rate": args.commission_rate,
        "min_commission": args.min_commission,
        "stamp_duty_rate": args.stamp_duty_rate,
        "slippage_bps": args.slippage_bps,
        "lot_size": args.lot_size,
        "max_positions": args.max_positions,
        "max_position_pct": args.max_position_pct,
        "max_turnover_per_rebalance": args.max_turnover_per_rebalance,
        "max_industry_pct": args.max_industry_pct,
        "industry_map_csv": args.industry_map_csv or None,
        "rebalance": args.rebalance,
        "execution_price": args.execution_price,
        "signal_timing": args.signal_timing,
        "limit_up_pct": args.limit_up_pct,
        "limit_down_pct": args.limit_down_pct,
        "backtest_mode": args.backtest_mode,
        "benchmark": args.benchmark_csv or None,
    }


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", default="", help="Optional YAML config path. Defaults to config/default.yaml.")
    pre_args, _ = pre_parser.parse_known_args()
    app_config = load_config(pre_args.config or None)
    backtest_config = app_config.get("backtest", {}) if isinstance(app_config.get("backtest"), dict) else {}
    report_config = app_config.get("report", {}) if isinstance(app_config.get("report"), dict) else {}

    parser = argparse.ArgumentParser(
        description="Generate an A-share daily quant report.",
        parents=[pre_parser],
    )
    parser.add_argument("--limit", type=int, default=80, help="Number of liquid stocks to analyze.")
    parser.add_argument("--top-n", type=int, default=report_config.get("top_n", 20), help="Number of candidates shown in the report.")
    parser.add_argument("--decision-card-top-n", type=int, default=report_config.get("decision_card_top_n", 5), help="Number of Top Candidates rendered as compact decision cards.")
    parser.add_argument("--max-card-bullets", type=int, default=report_config.get("max_card_bullets", 3), help="Maximum compact card items shown per section.")
    parser.add_argument("--learning-appendix-max-items", type=int, default=report_config.get("learning_appendix_max_items", 20), help="Maximum candidate learning items shown in the appendix.")
    parser.add_argument("--no-learning-appendix", dest="include_learning_appendix", action="store_false", help="Disable the learning appendix in generated reports.")
    parser.set_defaults(include_learning_appendix=bool(report_config.get("include_learning_appendix", True)))
    parser.add_argument("--min-amount", type=float, default=100_000_000, help="Minimum spot turnover in yuan.")
    parser.add_argument("--max-price", type=float, default=15.0, help="Maximum latest price in yuan. Use 0 to disable.")
    parser.add_argument("--start-date", default="20200101", help="Historical start date, YYYYMMDD.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="Historical end date, YYYYMMDD.")
    parser.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"], help="AKShare adjustment mode.")
    parser.add_argument("--refresh-days", type=int, default=15, help="Overlap days for incremental refresh.")
    parser.add_argument("--sleep", type=float, default=0.25, help="Delay between stock requests.")
    parser.add_argument("--spot-source", choices=["auto", "cache"], default="auto", help="Spot snapshot source: auto fetches providers; cache reads local spot cache only.")
    parser.add_argument("--news-limit", type=int, default=30, help="Number of top-ranked stocks to enrich with news.")
    parser.add_argument("--news-items", type=int, default=3, help="Recent news items kept per enriched stock.")
    parser.add_argument("--score-threshold", type=float, default=60.0, help="Minimum score saved as candidate.")
    parser.add_argument("--long-pool-size", type=int, default=20, help="Stable long-term watch pool size.")
    parser.add_argument("--long-pool-add-score", type=float, default=78.0, help="Score needed to add into the long-term pool.")
    parser.add_argument("--long-pool-keep-score", type=float, default=62.0, help="Score below this downgrades a long-term pool name.")
    parser.add_argument("--long-pool-drop-score", type=float, default=52.0, help="Score below this removes a long-term pool name.")
    parser.add_argument("--benchmark-csv", default="", help="Optional local benchmark CSV with columns: date,close. No network fetch is used.")
    parser.add_argument("--initial-cash", type=float, default=backtest_config.get("initial_cash", PortfolioBacktestConfig.initial_cash), help="Backtest initial cash.")
    parser.add_argument("--commission-rate", type=float, default=backtest_config.get("commission_rate", PortfolioBacktestConfig.commission_rate), help="Backtest commission rate for buys and sells.")
    parser.add_argument("--min-commission", type=float, default=backtest_config.get("min_commission", PortfolioBacktestConfig.min_commission), help="Backtest minimum commission per executed trade.")
    parser.add_argument("--stamp-duty-rate", type=float, default=backtest_config.get("stamp_duty_rate", PortfolioBacktestConfig.stamp_duty_rate), help="Backtest stamp duty rate for sells.")
    parser.add_argument("--slippage-bps", type=float, default=backtest_config.get("slippage_bps", PortfolioBacktestConfig.slippage_bps), help="Backtest slippage in basis points.")
    parser.add_argument("--lot-size", type=int, default=backtest_config.get("lot_size", PortfolioBacktestConfig.lot_size), help="Backtest board lot size.")
    parser.add_argument("--max-positions", type=int, default=backtest_config.get("max_positions", PortfolioBacktestConfig.max_positions), help="Portfolio backtest maximum concurrent positions.")
    parser.add_argument("--max-position-pct", type=float, default=backtest_config.get("max_position_pct", PortfolioBacktestConfig.max_position_pct), help="Portfolio backtest maximum value per position.")
    parser.add_argument("--max-turnover-per-rebalance", type=float, default=backtest_config.get("max_turnover_per_rebalance", PortfolioBacktestConfig.max_turnover_per_rebalance), help="Optional maximum gross turnover per rebalance as a share of equity.")
    parser.add_argument("--max-industry-pct", type=float, default=backtest_config.get("max_industry_pct", PortfolioBacktestConfig.max_industry_pct), help="Optional industry concentration warning threshold.")
    parser.add_argument("--industry-map-csv", default=backtest_config.get("industry_map_csv", "") or "", help="Optional local CSV with symbol,industry columns. No network fetch is used.")
    parser.add_argument("--rebalance", choices=["daily", "weekly", "monthly"], default=backtest_config.get("rebalance", PortfolioBacktestConfig.rebalance), help="Portfolio backtest rebalance frequency.")
    parser.add_argument("--execution-price", choices=["next_open", "close"], default=backtest_config.get("execution_price", PortfolioBacktestConfig.execution_price), help="Backtest execution price rule.")
    parser.add_argument("--signal-timing", choices=["close"], default=backtest_config.get("signal_timing", PortfolioBacktestConfig.signal_timing), help="Backtest signal timing rule.")
    parser.add_argument("--limit-up-pct", type=float, default=backtest_config.get("limit_up_pct", PortfolioBacktestConfig.limit_up_pct), help="Daily change threshold that blocks buys.")
    parser.add_argument("--limit-down-pct", type=float, default=backtest_config.get("limit_down_pct", PortfolioBacktestConfig.limit_down_pct), help="Daily change threshold that blocks sells.")
    parser.add_argument("--backtest-mode", choices=["portfolio", "independent"], default=backtest_config.get("backtest_mode", "portfolio"), help="Use capital-constrained portfolio backtest or independent average mode.")
    args = parser.parse_args()
    args.loaded_config = app_config
    args.config_path = pre_args.config or "config/default.yaml"
    return args


def _simulate_backtest_with_price_policy(
    histories: dict[str, pd.DataFrame],
    *,
    mode: str,
    config: BacktestConfig | PortfolioBacktestConfig,
    benchmark_series: pd.Series | pd.DataFrame | None,
    data_health: pd.DataFrame | None,
):
    """Validate observed daily-price basis before either report backtest path."""

    audit = validate_price_frame_set(histories, consumer="backtest")
    if not audit.get("eligible"):
        return None, audit
    if mode == "independent":
        result = simulate_backtest_result(histories, config, benchmark_series)
    else:
        result = simulate_portfolio_backtest(
            histories,
            config,
            benchmark_series,
            data_health,
        )
    return result, audit


def _validate_daily_report_feature_histories(
    histories: dict[str, pd.DataFrame],
) -> dict[str, object]:
    """Fail closed before the daily report computes or writes mixed-basis features."""

    return validate_price_frame_set(histories, consumer="feature")


def main() -> int:
    args = parse_args()
    setup_logging()
    reset_data_health()
    reset_news_health()
    reset_local_daily_cache_stats()
    logging.info("Project root: %s", PROJECT_ROOT)
    logging.info("Args: %s", vars(args))

    max_price = args.max_price if args.max_price and args.max_price > 0 else None
    spot = fetch_spot(
        args.limit,
        args.min_amount,
        max_price,
        start_date=args.start_date,
        end_date=args.end_date,
        adjust=args.adjust,
        sleep_seconds=args.sleep,
        loaded_config=args.loaded_config,
        spot_source=args.spot_source,
    )
    if spot.empty:
        raise RuntimeError("No stocks matched the universe filters")

    spot_meta = dict(getattr(spot, "attrs", {}).get("spot_snapshot_meta") or {})
    histories: dict[str, pd.DataFrame] = dict(getattr(spot, "attrs", {}).get("spot_histories") or {})
    for idx, row in spot.iterrows():
        symbol = str(row["代码"]).zfill(6)
        name = str(row["名称"])
        try:
            hist = histories.get(symbol)
            if hist is None or hist.empty:
                hist = fetch_daily(symbol, args.start_date, args.end_date, args.adjust, args.refresh_days, args.sleep, name=name)
                histories[symbol] = hist
        except Exception as exc:
            logging.exception("Failed to load history for %s %s: %s", symbol, name, exc)

    feature_histories = {
        str(row["代码"]).zfill(6): histories[str(row["代码"]).zfill(6)]
        for _, row in spot.iterrows()
        if str(row["代码"]).zfill(6) in histories
        and isinstance(histories[str(row["代码"]).zfill(6)], pd.DataFrame)
        and not histories[str(row["代码"]).zfill(6)].empty
    }
    feature_price_policy_validation = _validate_daily_report_feature_histories(
        feature_histories
    )
    if not feature_price_policy_validation.get("eligible"):
        reasons = ",".join(
            str(item)
            for item in feature_price_policy_validation.get("reason_codes") or []
        )
        raise RuntimeError(
            "price policy validation blocked daily report feature calculation before write:"
            + reasons
        )

    rows: list[dict] = []
    for idx, row in spot.iterrows():
        symbol = str(row["代码"]).zfill(6)
        name = str(row["名称"])
        try:
            hist = feature_histories.get(symbol)
            if hist is None or hist.empty:
                continue
            feature = compute_base_features(symbol, name, hist, row)
            if feature:
                rows.append(feature)
        except Exception as exc:
            logging.exception("Failed to process %s %s: %s", symbol, name, exc)

    features = finalize_scores(pd.DataFrame(rows))
    if features.empty:
        raise RuntimeError("No usable features were generated")
    latest_data_date = ""
    if not features.empty and "date" in features.columns:
        latest_value = features["date"].max()
        latest_data_date = pd.Timestamp(latest_value).strftime("%Y-%m-%d") if pd.notna(latest_value) else ""
    features = enrich_with_news(
        features,
        args.news_limit,
        args.news_items,
        sleep_seconds=min(args.sleep, 0.15),
    )

    feature_path = REPORT_DIR / f"{datetime.now():%Y-%m-%d}_features.csv"
    feature_path = save_csv_safely(features, feature_path)
    logging.info("Saved feature table: %s", feature_path)

    health_for_gate = data_health_frame()
    backtest_ready_symbols = set()
    if not health_for_gate.empty and "backtest_allowed" in health_for_gate.columns:
        backtest_ready_symbols = {
            str(row.get("symbol") or "").zfill(6)
            for row in health_for_gate.to_dict("records")
            if bool(row.get("backtest_allowed"))
        }
    backtest_histories = {symbol: hist for symbol, hist in histories.items() if str(symbol).zfill(6) in backtest_ready_symbols}
    backtest_gate_note = (
        f"Data grade gate: {len(backtest_histories)} of {len(histories)} histories are BACKTEST_READY. "
        "SCORABLE rows are limited to basic scoring, OBSERVE_ONLY rows are observation-only, and UNAVAILABLE rows are excluded."
    )
    benchmark_series = load_benchmark_series(args.benchmark_csv) if args.benchmark_csv else None
    backtest_kwargs = {
        "initial_cash": args.initial_cash,
        "commission_rate": args.commission_rate,
        "min_commission": args.min_commission,
        "stamp_duty_rate": args.stamp_duty_rate,
        "slippage_bps": args.slippage_bps,
        "lot_size": args.lot_size,
        "max_position_pct": args.max_position_pct,
        "execution_price": args.execution_price,
        "signal_timing": args.signal_timing,
        "limit_up_pct": args.limit_up_pct,
        "limit_down_pct": args.limit_down_pct,
        "benchmark": args.benchmark_csv or None,
    }
    if args.backtest_mode == "independent":
        backtest_config = BacktestConfig(**backtest_kwargs)
    else:
        backtest_config = PortfolioBacktestConfig(
            **backtest_kwargs,
            max_positions=args.max_positions,
            rebalance=args.rebalance,
            max_turnover_per_rebalance=args.max_turnover_per_rebalance,
            max_industry_pct=args.max_industry_pct,
            industry_map_csv=args.industry_map_csv or None,
        )
    backtest_result, price_policy_validation = _simulate_backtest_with_price_policy(
        backtest_histories,
        mode=args.backtest_mode,
        config=backtest_config,
        benchmark_series=benchmark_series,
        data_health=health_for_gate,
    )
    if backtest_result is None:
        reasons = ",".join(
            str(item) for item in price_policy_validation.get("reason_codes") or []
        )
        raise RuntimeError(
            "price policy validation blocked daily report backtest before simulation:"
            + reasons
        )
    effective_config = effective_backtest_config_from_args(args)
    backtest_result.assumptions["backtest_mode"] = args.backtest_mode
    backtest_result.assumptions["config_path"] = args.config_path
    backtest_result.assumptions["data_grade_gate_note"] = backtest_gate_note
    backtest_result.assumptions["backtest_ready_symbol_count"] = len(backtest_histories)
    backtest_result.assumptions["effective_backtest_config"] = effective_config
    backtest_result.assumptions["feature_price_policy_validation"] = (
        feature_price_policy_validation
    )
    backtest_result.assumptions["price_policy_validation"] = price_policy_validation
    backtest_result.metrics.assumptions = backtest_result.assumptions
    backtest_result.summary = summary_rows_from_metrics(
        backtest_result.metrics,
        backtest_result.assumptions,
        backtest_result.extra_metrics,
    )
    backtest_files = export_backtest_result(backtest_result, "portfolio", REPORT_DIR)
    metrics = backtest_result.metrics
    spot_meta.update(
        {
            "latest_date": latest_data_date or str(spot_meta.get("latest_date") or ""),
            "row_count": int(len(spot)),
            "report_generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    report_path = write_report(features, spot, metrics, args, backtest_files, spot_meta=spot_meta)
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
