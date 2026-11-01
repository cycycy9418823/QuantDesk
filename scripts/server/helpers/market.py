"""Market-data helpers: spot snapshot, realtime quotes, sector board, benchmark.

Split from quant_api_server.py; re-exported to preserve the attribute contract
(load_stock_data, load_spot_snapshot_frame, normalize_symbol, ...).
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from functools import lru_cache
from threading import Thread
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.server.constants import *  # noqa: F401,F403
from scripts.server.app_factory import app, LOG, project_root

from scripts.server.helpers.util import (
    finite_float, display_number, chart_number, read_json_file, cache_age_seconds, human_duration,
)

from quantdesk.csv_io import read_csv_frame
from quantdesk.market_clock import expected_settlement_date, get_market_session, to_shanghai_datetime
from quantdesk.column_aliases import find_column
from quantdesk.quote_evidence import provider_quote_datetime, quote_number

__all__ = ['spot_snapshot_updated_at', 'spot_snapshot_age_seconds', 'snapshot_quote_date', 'realtime_freshness_state', 'realtime_provider_error_summary', 'realtime_provider_note', 'sector_cache_has_usable_rows', 'realtime_network_enabled', 'normalize_symbol', '_load_stock_data_cached', 'load_stock_data', 'latest_pct_change', 'load_benchmark_index', 'load_benchmark_series', 'spot_market_breadth', '_canonical_spot_frame', 'load_spot_snapshot_frame', '_write_spot_meta', '_call_with_timeout', 'fetch_eastmoney_spot_direct', 'fetch_sina_spot_direct', '_cached_spot_refresh_payload', '_realtime_background_worker', 'start_realtime_background_refresh', 'realtime_background_snapshot', 'realtime_background_attempt_age_seconds', 'maybe_start_realtime_background_refresh', 'refresh_spot_snapshot_cache', 'eastmoney_secid', '_em_scaled', 'fetch_eastmoney_single_quote', 'upsert_spot_snapshot_row', 'refresh_single_quote_cache', 'spot_quote_for_symbol', '_amount_to_yi', 'realtime_market_summary', 'spot_rank_rows', 'market_session_label', 'read_theme_map', '_board_name', 'calculate_sector_stats', 'refresh_industry_board_cache']

def spot_snapshot_updated_at() -> str:
    meta = read_json_file(SPOT_SNAPSHOT_META)
    updated = str(meta.get("updated_at") or meta.get("fetched_at") or "").strip()
    if updated:
        return updated
    try:
        return datetime.fromtimestamp(SPOT_SNAPSHOT_CACHE.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def spot_snapshot_age_seconds() -> float | None:
    updated = spot_snapshot_updated_at()
    if updated:
        try:
            then = datetime.fromisoformat(updated)
            current = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
            return max(0.0, (current - then).total_seconds())
        except ValueError as exc:
            LOG.warning("spot_snapshot_updated_at_parse_failed: value=%r error=%s", updated, exc)
    return cache_age_seconds(SPOT_SNAPSHOT_CACHE)


def snapshot_quote_date(frame: pd.DataFrame, updated_at: Any = "") -> str:
    """Read a recorded quote date; collection time is not a quote date."""
    if not frame.empty:
        for col in ("quote_time", "quote_date", "date", "trade_date"):
            if col not in frame.columns:
                continue
            values = frame[col].astype(str)
            dated = values[values.str.match(r"^\d{4}-\d{2}-\d{2}")]
            parsed = pd.to_datetime(dated, errors="coerce").dropna()
            if not parsed.empty:
                return parsed.max().date().isoformat()
    return ""


def realtime_freshness_state(
    *,
    session: str,
    frame: pd.DataFrame,
    age_seconds: float | None,
    updated_at: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Translate cache age into user-facing market-session freshness.

    After the A-share close, a two-hour-old final snapshot is not "not latest";
    prices are no longer changing. During live sessions, the same age is a lag.
    """
    quote_date = snapshot_quote_date(frame, updated_at)
    current = to_shanghai_datetime(now)
    expected = expected_settlement_date(current)
    expected_text = expected.isoformat() if expected else ""
    age_stale = age_seconds is None or age_seconds > REALTIME_CACHE_MAX_AGE_SECONDS
    live_session = session in {"morning_session", "afternoon_session"}
    pause_session = session in {"pre_open", "lunch_break", "after_close", "non_trading_day", "market_holiday"}
    quote_matches_expected = bool(quote_date and expected_text and quote_date == expected_text)
    quote_is_today = bool(quote_date and quote_date == current.date().isoformat())

    if session == "after_close" and quote_matches_expected:
        label = "收盘后复盘快照"
        stale = False
        lagging = False
        hint = "A股已收盘，行情不会继续跳动；刷新只是重试数据源或更新本地缓存，不代表盘后价格还在变化。"
    elif session in {"non_trading_day", "market_holiday"} and quote_matches_expected:
        label = "最近交易日快照"
        stale = False
        lagging = False
        hint = "当前不是交易时段，使用最近完整交易日快照做复盘观察。"
    elif session == "pre_open" and quote_matches_expected:
        label = "盘前复盘快照"
        stale = False
        lagging = False
        hint = "盘前行情尚未开始，显示最近完整交易日快照。"
    elif session == "lunch_break" and quote_is_today:
        label = "午间临时快照"
        stale = False
        lagging = False
        hint = "午间休市阶段价格暂不连续变化；下午开盘后可继续刷新观察。"
    elif live_session:
        stale = age_stale
        lagging = age_stale
        label = "盘中快照待刷新" if stale else "盘中可用快照"
        hint = (
            f"交易中快照距今 {human_duration(age_seconds)}，刷新源暂未成功返回。"
            if stale
            else f"交易中快照距今 {human_duration(age_seconds)}，用于临时观察，不覆盖正式日线。"
        )
    elif quote_matches_expected:
        label = "可复盘快照"
        stale = False
        lagging = False
        hint = "当前交易时段已暂停或未开始，使用最近可用快照做复盘观察。"
    else:
        label = "本地快照待更新"
        stale = True
        lagging = False
        hint = "本地快照日期早于预期交易日；需要联网刷新或导入最新快照。"

    return {
        "data_freshness_label": label,
        "is_cache_stale": stale,
        "is_live_session": live_session,
        "is_live_lagging": lagging,
        "is_market_pause": pause_session,
        "snapshot_quote_date": quote_date,
        "expected_snapshot_date": expected_text,
        "freshness_hint": hint,
    }


def realtime_provider_error_summary(background: dict[str, Any], *, include_sector: bool = True) -> str:
    spot = background.get("spot") if isinstance(background.get("spot"), dict) else {}
    sector = background.get("sector") if isinstance(background.get("sector"), dict) else {}
    errors = spot.get("errors") if isinstance(spot.get("errors"), list) else []
    spot_status = str(spot.get("status") or "")
    spot_failed = spot_status in {"provider_failed", "provider_empty_after_normalize"} or spot.get("ok") is False
    parts = [str(item) for item in errors[:2] if item] if spot_failed else []
    sector_message = str(sector.get("message_for_ui") or "").strip()
    if include_sector and sector_message:
        parts.append(sector_message)
    return "；".join(parts)


def realtime_provider_note(background: dict[str, Any], *, sector_cache_usable: bool) -> str:
    spot = background.get("spot") if isinstance(background.get("spot"), dict) else {}
    sector = background.get("sector") if isinstance(background.get("sector"), dict) else {}
    notes: list[str] = []
    errors = spot.get("errors") if isinstance(spot.get("errors"), list) else []
    spot_status = str(spot.get("status") or "")
    if errors and spot.get("ok") is True and spot_status not in {"provider_failed", "provider_empty_after_normalize"}:
        notes.append("主实时源暂不可用时已自动切到备用源；当前快照仍来自已成功返回的数据源。")
    sector_message = str(sector.get("message_for_ui") or "").strip()
    if sector_message and sector_cache_usable:
        notes.append("行业板块后台刷新暂未成功，页面继续显示上一次可用板块缓存。")
    return "；".join(notes)


def sector_cache_has_usable_rows(sector_stats: dict[str, Any]) -> bool:
    rows = list(sector_stats.get("gainers") or []) + list(sector_stats.get("losers") or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or "数据缺失" in name:
            continue
        if finite_float(row.get("change")) is not None:
            return True
    return False


def realtime_network_enabled() -> bool:
    return bool(app.config.get("REALTIME_NETWORK_ENABLED", False))


def normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").split(".")[0].strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


@lru_cache(maxsize=512)
def _load_stock_data_cached(clean_symbol: str, _mtime_ns: int) -> pd.DataFrame:
    """Read + clean one daily CSV. Cached by (symbol, file mtime); the mtime in
    the cache key auto-invalidates the entry whenever a data update rewrites the
    file, so callers never get stale data after a refresh. The OHLC-filter
    warning therefore also fires once per (file, mtime) instead of on every read."""
    csv_path = DAILY_DIR / f"{clean_symbol}.csv"
    try:
        df = read_csv_frame(csv_path)
    except Exception as exc:
        LOG.warning("load_stock_data(%s): CSV read failed: %s", clean_symbol, exc)
        return pd.DataFrame()
    # Classify the original bytes before date/row cleaning. The chart must
    # report the original price basis, rather than infer it from a directory.
    from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set
    basis, basis_reason = classify_frame_price_basis(df)
    policy = validate_price_frame_set({clean_symbol: df}, consumer="feature")
    df.attrs["price_basis"] = basis
    df.attrs["price_basis_eligible"] = bool(policy.get("eligible"))
    df.attrs["price_basis_reason"] = basis_reason
    for warning in df.attrs.get("warnings", []) or []:
        LOG.warning("load_stock_data(%s): %s", clean_symbol, warning)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "close" not in df.columns:
        return pd.DataFrame()
    df = df.dropna(subset=["close"]).copy()
    ohlc_cols = {"open", "high", "low", "close"}
    if ohlc_cols.issubset(df.columns):
        before = len(df)
        positive = (df[["open", "high", "low", "close"]] > 0).all(axis=1)
        ordered = (
            (df["high"] >= df[["open", "close", "low"]].max(axis=1))
            & (df["low"] <= df[["open", "close", "high"]].min(axis=1))
        )
        df = df[positive & ordered].copy()
        dropped = before - len(df)
        if dropped:
            df.attrs["bad_ohlc_rows_filtered"] = dropped
            LOG.warning("load_stock_data(%s): filtered %s invalid OHLC rows", clean_symbol, dropped)
    return df


def load_stock_data(symbol: str) -> pd.DataFrame:
    clean_symbol = normalize_symbol(symbol)
    csv_path = DAILY_DIR / f"{clean_symbol}.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    try:
        mtime_ns = csv_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    # Return a copy so callers that add columns / mutate cannot corrupt the
    # shared cached frame. The copy is far cheaper than re-reading + re-parsing.
    return _load_stock_data_cached(clean_symbol, mtime_ns).copy()


def latest_pct_change(df: pd.DataFrame) -> float | None:
    if df.empty or len(df) < 2:
        return None
    latest = finite_float(df.iloc[-1].get("close"))
    previous = finite_float(df.iloc[-2].get("close"))
    if latest is None or previous in (None, 0):
        return None
    return (latest - previous) / previous * 100


def load_benchmark_index(kind: str) -> dict[str, Any]:
    candidates = {
        "hs300": [BENCHMARK_DIR / "hs300.csv"],
        "sh_index": [
            BENCHMARK_DIR / "sh000001.csv",
            BENCHMARK_DIR / "sh_index.csv",
            BENCHMARK_DIR / "000001_index.csv",
        ],
    }[kind]
    path = next((item for item in candidates if item.exists()), None)
    label = "沪深300" if kind == "hs300" else "上证指数"
    if path is None:
        LOG.warning("load_benchmark_index(%s): no file found in data/benchmark/", kind)
        return {
            "label": label,
            "value": "数据缺失",
            "change": "数据缺失",
            "amount": "数据缺失",
            "hint": f"缺少 {label} 本地指数文件，请补充 data/benchmark 后刷新页面。",
        }
    df = read_csv_frame(path)
    csv_read_warnings = list(df.attrs.get("warnings", []) or [])
    if df.empty and csv_read_warnings:
        return {
            "label": label,
            "value": "数据缺失",
            "change": "数据缺失",
            "amount": "数据缺失",
            "hint": f"{label} 文件读取失败：{'; '.join(csv_read_warnings[:2])}",
            "warnings": csv_read_warnings,
        }
    if df.empty or "close" not in df.columns:
        return {
            "label": label,
            "value": "数据缺失",
            "change": "数据缺失",
            "amount": "数据缺失",
            "hint": f"{label} 文件没有 close 列，请重新生成 data/benchmark。",
        }
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
    if df.empty:
        return {
            "label": label,
            "value": "数据缺失",
            "change": "数据缺失",
            "amount": "数据缺失",
            "hint": f"{label} 本地文件没有有效收盘价，请更新数据。",
        }
    latest = df.iloc[-1]
    change = finite_float(latest.get("pct_chg"))
    if change is None and len(df) >= 2:
        previous = finite_float(df.iloc[-2].get("close"))
        close = finite_float(latest.get("close"))
        if previous not in (None, 0) and close is not None:
            change = (close - previous) / previous * 100
    has_amount_column = "amount" in df.columns
    amount_value = finite_float(latest.get("amount")) if has_amount_column else None
    amount_status = "available" if amount_value is not None else "source_not_connected"
    if amount_status == "available":
        amount_text = display_number(amount_value, 2, "亿元")
        hint = "本地指数数据已读取。"
    else:
        amount_text = "源未接入"
        hint = "指数点位/涨跌已读取；指数成交额源暂未接入。市场成交额请看本地样本成交额。"
    return {
        "label": label,
        "value": chart_number(latest.get("close")),
        "change": chart_number(change, 0.0) if change is not None else "数据缺失",
        "amount": amount_text,
        "amount_status": amount_status,
        "date": latest.get("date").strftime("%Y-%m-%d") if isinstance(latest.get("date"), pd.Timestamp) else str(latest.get("date", "")),
        "hint": hint,
        "warnings": csv_read_warnings,
    }


def load_benchmark_series(kind: str, days: int = 120) -> dict[str, Any]:
    """Read-only local benchmark close series for dashboard comparison lines."""

    candidates = {
        "hs300": [BENCHMARK_DIR / "hs300.csv"],
        "sh_index": [
            BENCHMARK_DIR / "sh000001.csv",
            BENCHMARK_DIR / "sh_index.csv",
            BENCHMARK_DIR / "000001_index.csv",
        ],
    }.get(kind)
    label = "沪深300" if kind == "hs300" else "上证指数"
    if candidates is None:
        return {
            "ok": False,
            "index": kind,
            "label": kind,
            "items": [],
            "count": 0,
            "hint": "仅支持 index=hs300 或 index=sh_index 两个本地基准。",
        }
    path = next((item for item in candidates if item.exists()), None)
    if path is None:
        return {
            "ok": False,
            "index": kind,
            "label": label,
            "items": [],
            "count": 0,
            "hint": f"缺少 {label} 本地指数文件，请补充 data/benchmark 后刷新。",
        }
    df = read_csv_frame(path)
    if df.empty or "close" not in df.columns or "date" not in df.columns:
        return {
            "ok": False,
            "index": kind,
            "label": label,
            "items": [],
            "count": 0,
            "hint": f"{label} 文件缺少 date/close 列，无法生成序列。",
        }
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["close", "date"]).sort_values("date")
    if df.empty:
        return {
            "ok": False,
            "index": kind,
            "label": label,
            "items": [],
            "count": 0,
            "hint": f"{label} 本地文件没有有效收盘价，请更新数据。",
        }
    window = df.tail(max(int(days), 2))
    has_pct = "pct_chg" in window.columns
    items = [
        {
            "date": row["date"].strftime("%Y-%m-%d"),
            "close": finite_float(row.get("close")),
            "pct_chg": finite_float(row.get("pct_chg")) if has_pct else None,
        }
        for _, row in window.iterrows()
    ]
    return {
        "ok": True,
        "index": kind,
        "label": label,
        "items": items,
        "count": len(items),
        "latest_date": items[-1]["date"] if items else "",
        "hint": "本地基准指数序列（只读，不联网）。",
        "research_only": True,
    }


def spot_market_breadth() -> dict[str, Any]:
    path = SPOT_SNAPSHOT_CACHE
    if not path.exists():
        return {
            "advancing_count": 0,
            "declining_count": 0,
            "flat_count": 0,
            "sample_count": 0,
            "total_amount": "数据缺失",
            "hint": "缺少 data/cache/spot_snapshot_latest.csv，无法统计涨跌家数。",
        }
    df = read_csv_frame(path)
    csv_read_warnings = list(df.attrs.get("warnings", []) or [])
    if df.empty and csv_read_warnings:
        return {
            "advancing_count": 0,
            "declining_count": 0,
            "flat_count": 0,
            "sample_count": 0,
            "total_amount": "数据缺失",
            "hint": f"市场快照读取失败：{'; '.join(csv_read_warnings[:2])}",
            "warnings": csv_read_warnings,
        }
    change_col = find_column(df, "pct_chg")
    amount_col = find_column(df, "amount")
    changes = pd.to_numeric(df[change_col], errors="coerce").dropna() if change_col else pd.Series(dtype=float)
    amount = pd.to_numeric(df[amount_col], errors="coerce").sum() if amount_col else math.nan
    total_amount = f"{amount / 100000000:.2f}亿元" if math.isfinite(float(amount)) and amount > 0 else "数据缺失"
    meta = read_json_file(SPOT_SNAPSHOT_META)
    return {
        "advancing_count": int((changes > 0).sum()),
        "declining_count": int((changes < 0).sum()),
        "flat_count": int((changes == 0).sum()),
        "sample_count": int(len(changes)),
        "total_amount": total_amount,
        "hint": "涨跌家数来自本地 spot snapshot 样本。",
        "warnings": csv_read_warnings,
        "quote_date": snapshot_quote_date(df),
        "fetched_at": meta.get("fetched_at") or meta.get("updated_at") or "",
        "source": meta.get("source") or "data/cache/spot_snapshot_latest.csv",
        "date_note": "报价日只取行情字段；抓取时刻不证明报价属于当天。",
    }


def _canonical_spot_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    rename_map = {
        "代码": "symbol",
        "名称": "name",
        "最新价": "price",
        "涨跌额": "change",
        "涨跌幅": "pct_chg",
        "昨收": "pre_close",
        "今开": "open",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "时间戳": "quote_time",
        "time": "quote_time",
        "timestamp": "quote_time",
        "code": "symbol",
        "股票代码": "symbol",
        "股票名称": "name",
        "current_price": "price",
        "close": "price",
        "source": "source",
        "updated_at": "updated_at",
    }
    frame = raw.rename(columns={col: rename_map.get(str(col).strip(), str(col).strip()) for col in raw.columns}).copy()
    frame = frame.loc[:, ~frame.columns.duplicated()]
    # Preserve an actual per-row collection record. A snapshot's file mtime or
    # global update time is not proof of when this particular quote was read.
    if "fetched_at" in frame.columns:
        if "updated_at" not in frame.columns:
            frame["updated_at"] = frame["fetched_at"]
        else:
            recorded = frame["updated_at"].fillna("").astype(str)
            missing = recorded.str.strip().str.lower().isin({"", "nan", "none", "null"})
            frame["updated_at"] = recorded.where(~missing, frame["fetched_at"].fillna(""))
    for column in ("symbol", "name", "price", "pct_chg", "change", "pre_close", "open", "high", "low", "volume", "amount", "quote_time", "quote_timestamp", "quote_date", "price_basis", "source", "updated_at"):
        if column not in frame.columns:
            frame[column] = ""
    frame["symbol"] = frame["symbol"].astype(str).map(normalize_symbol)
    frame["name"] = frame["name"].astype(str).replace({"nan": ""})
    for column in ("price", "pct_chg", "change", "pre_close", "open", "high", "low", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["quote_time"] = frame["quote_time"].astype(str).replace({"nan": ""})
    frame["quote_timestamp"] = frame["quote_timestamp"].astype(str).replace({"nan": ""})
    frame["quote_date"] = frame["quote_date"].astype(str).replace({"nan": ""})
    frame["price_basis"] = frame["price_basis"].astype(str).replace({"nan": ""})
    frame["source"] = frame["source"].astype(str).replace({"nan": ""})
    frame["updated_at"] = frame["updated_at"].astype(str).replace({"nan": ""})
    frame = frame[frame["symbol"].astype(str).str.len() == 6]
    frame = frame.drop_duplicates("symbol", keep="last")
    return frame[["symbol", "name", "price", "pct_chg", "change", "pre_close", "open", "high", "low", "volume", "amount", "quote_time", "quote_timestamp", "quote_date", "price_basis", "source", "updated_at"]].reset_index(drop=True)


def load_spot_snapshot_frame() -> pd.DataFrame:
    if not SPOT_SNAPSHOT_CACHE.exists():
        return pd.DataFrame()
    try:
        raw = read_csv_frame(SPOT_SNAPSHOT_CACHE)
    except Exception as exc:  # noqa: BLE001 - stale snapshot cache should degrade, not break UI
        LOG.warning("spot_snapshot_cache_read_failed: path=%s error=%s", SPOT_SNAPSHOT_CACHE, exc)
        return pd.DataFrame()
    csv_read_warnings = list(raw.attrs.get("warnings", []) or [])
    for warning in csv_read_warnings:
        LOG.warning("spot_snapshot_cache_read_warning: path=%s warning=%s", SPOT_SNAPSHOT_CACHE, warning)
    frame = _canonical_spot_frame(raw)
    frame.attrs["warnings"] = csv_read_warnings
    return frame


def _write_spot_meta(payload: dict[str, Any]) -> None:
    SPOT_SNAPSHOT_META.parent.mkdir(parents=True, exist_ok=True)
    SPOT_SNAPSHOT_META.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _call_with_timeout(func: Any, *, timeout_seconds: float) -> Any:
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func)
    try:
        return future.result(timeout=timeout_seconds)
    except TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"provider timeout after {timeout_seconds:.0f}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def fetch_eastmoney_spot_direct(
    *,
    timeout_seconds: float = 8.0,
    page_size: int = 100,
    max_pages: int = 80,
) -> pd.DataFrame:
    """Fetch a full-market temporary quote snapshot without going through akshare."""
    try:
        import requests
    except ImportError:
        raise RuntimeError("requests is not installed")

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    }
    fields = "f12,f14,f2,f3,f4,f5,f6,f17,f18,f15,f16,f124"
    rows: list[dict[str, Any]] = []
    planned_pages = max_pages
    now = datetime.now()
    for page in range(1, max_pages + 1):
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": fields,
        }
        payload: object | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    time.sleep(0.3)
        if payload is None:
            if rows:
                partial = pd.DataFrame(rows)
                partial["source"] = "eastmoney.spot_direct_partial"
                partial.attrs["provider_status"] = "partial"
                partial.attrs["provider_warning"] = f"page {page} failed after retries: {last_error}"
                return partial
            raise last_error or RuntimeError("eastmoney direct returned no payload")
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            break
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not isinstance(diff, list) or not diff:
            break
        if page == 1:
            total = int(finite_float(data.get("total"), 0) or 0)
            if total > 0:
                planned_pages = min(max_pages, max(1, math.ceil(total / page_size)))
        for item in diff:
            if not isinstance(item, dict):
                continue
            quote_ts = finite_float(item.get("f124"))
            quote_dt = datetime.fromtimestamp(quote_ts) if quote_ts else now
            rows.append(
                {
                    "symbol": normalize_symbol(item.get("f12")),
                    "name": str(item.get("f14") or "").strip(),
                    "price": finite_float(item.get("f2")),
                    "pct_chg": finite_float(item.get("f3")),
                    "change": finite_float(item.get("f4")),
                    "volume": finite_float(item.get("f5")),
                    "amount": finite_float(item.get("f6")),
                    "open": finite_float(item.get("f17")),
                    "pre_close": finite_float(item.get("f18")),
                    "high": finite_float(item.get("f15")),
                    "low": finite_float(item.get("f16")),
                    "quote_time": quote_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "eastmoney.spot_direct",
                    "updated_at": quote_dt.isoformat(timespec="seconds"),
                }
            )
        if page >= planned_pages:
            break
        time.sleep(0.05)
    return pd.DataFrame(rows)


def fetch_sina_spot_direct(
    *,
    timeout_seconds: float = 8.0,
    page_size: int = 100,
    max_pages: int = 80,
) -> pd.DataFrame:
    """Fallback full-market temporary quote snapshot from Sina's market center."""
    try:
        import requests
    except ImportError:
        raise RuntimeError("requests is not installed")

    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    headers = {"User-Agent": "Mozilla/5.0"}
    rows: list[dict[str, Any]] = []
    today = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat(timespec="seconds")
    for page in range(1, max_pages + 1):
        params = {
            "page": page,
            "num": page_size,
            "sort": "symbol",
            "asc": 1,
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "init",
        }
        payload: object | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    time.sleep(0.3)
        if payload is None:
            if rows:
                partial = pd.DataFrame(rows)
                partial["source"] = "sina.spot_direct_partial"
                partial.attrs["provider_status"] = "partial"
                partial.attrs["provider_warning"] = f"page {page} failed after retries: {last_error}"
                return partial
            raise last_error or RuntimeError("sina direct returned no payload")
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = normalize_symbol(item.get("code") or item.get("symbol"))
            ticktime = str(item.get("ticktime") or "").strip()
            quote_time = f"{today} {ticktime}" if ticktime else ""
            rows.append(
                {
                    "symbol": code,
                    "name": str(item.get("name") or "").strip(),
                    "price": finite_float(item.get("trade")),
                    "pct_chg": finite_float(item.get("changepercent")),
                    "change": finite_float(item.get("pricechange")),
                    "volume": finite_float(item.get("volume")),
                    "amount": finite_float(item.get("amount")),
                    "open": finite_float(item.get("open")),
                    "pre_close": finite_float(item.get("settlement")),
                    "high": finite_float(item.get("high")),
                    "low": finite_float(item.get("low")),
                    "quote_time": quote_time,
                    "source": "sina.spot_direct",
                    "updated_at": now_iso,
                }
            )
        if len(payload) < page_size:
            break
        time.sleep(0.05)
    return pd.DataFrame(rows)


def _cached_spot_refresh_payload(*, network: bool, status: str, message: str) -> dict[str, Any]:
    frame = load_spot_snapshot_frame()
    age = spot_snapshot_age_seconds()
    return {
        "ok": not frame.empty,
        "status": status,
        "network_authorized": network,
        "row_count": int(len(frame)),
        "cache_age_seconds": int(age) if age is not None else "数据缺失",
        "updated_at": spot_snapshot_updated_at(),
        "message_for_ui": message,
    }


def _realtime_background_worker(*, force: bool, reason: str) -> None:
    started_at = datetime.now().isoformat(timespec="seconds")
    spot_result: dict[str, Any] = {}
    sector_result: dict[str, Any] = {}
    try:
        spot_result = refresh_spot_snapshot_cache(network=True, force=force)
        sector_age = cache_age_seconds(INDUSTRY_BOARD_CACHE)
        if force or sector_age is None or sector_age > SECTOR_CACHE_MAX_AGE_SECONDS:
            try:
                sector_result = _call_with_timeout(refresh_industry_board_cache, timeout_seconds=30.0)
            except Exception as exc:  # noqa: BLE001
                sector_result = {"ok": False, "message_for_ui": f"板块实时刷新失败：{exc}"}
        with REALTIME_BACKGROUND_LOCK:
            REALTIME_BACKGROUND_STATUS.update(
                {
                    "running": False,
                    "status": spot_result.get("status", "finished"),
                    "reason": reason,
                    "started_at": started_at,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "spot": spot_result,
                    "sector": sector_result,
                    "message_for_ui": spot_result.get("message_for_ui") or "实时观察层后台刷新完成。",
                }
            )
    except Exception as exc:  # noqa: BLE001
        with REALTIME_BACKGROUND_LOCK:
            REALTIME_BACKGROUND_STATUS.update(
                {
                    "running": False,
                    "status": "failed",
                    "reason": reason,
                    "started_at": started_at,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "spot": spot_result,
                    "sector": sector_result,
                    "message_for_ui": f"实时观察层后台刷新失败：{exc}",
                }
            )


def start_realtime_background_refresh(*, force: bool, reason: str) -> dict[str, Any]:
    if not realtime_network_enabled():
        return {
            "running": False,
            "status": "disabled",
            "message_for_ui": "实时观察层未授权联网，使用本地缓存。",
        }
    with REALTIME_BACKGROUND_LOCK:
        if REALTIME_BACKGROUND_STATUS.get("running"):
            return dict(REALTIME_BACKGROUND_STATUS)
        REALTIME_BACKGROUND_STATUS.update(
            {
                "running": True,
                "status": "running",
                "reason": reason,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "message_for_ui": "实时观察层正在后台刷新；页面先显示可用缓存。",
            }
        )
        snapshot = dict(REALTIME_BACKGROUND_STATUS)
    Thread(target=_realtime_background_worker, kwargs={"force": force, "reason": reason}, daemon=True).start()
    return snapshot


def realtime_background_snapshot() -> dict[str, Any]:
    with REALTIME_BACKGROUND_LOCK:
        return dict(REALTIME_BACKGROUND_STATUS)


def realtime_background_attempt_age_seconds(status: dict[str, Any]) -> float | None:
    stamp = status.get("finished_at") or status.get("started_at")
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return max(0.0, (datetime.now() - then).total_seconds())


def maybe_start_realtime_background_refresh(*, force: bool, reason: str) -> dict[str, Any]:
    status = realtime_background_snapshot()
    attempt_age = realtime_background_attempt_age_seconds(status)
    retry_ready = attempt_age is None or attempt_age > REALTIME_PROVIDER_RETRY_SECONDS
    if force or status.get("running") or retry_ready:
        return start_realtime_background_refresh(force=force, reason=reason)
    return status


def refresh_spot_snapshot_cache(*, network: bool, force: bool = False) -> dict[str, Any]:
    age = spot_snapshot_age_seconds()
    if not network:
        frame = load_spot_snapshot_frame()
        return {
            "ok": not frame.empty,
            "status": "cache_only" if not frame.empty else "missing",
            "network_authorized": False,
            "row_count": int(len(frame)),
            "cache_age_seconds": int(age) if age is not None else "数据缺失",
            "updated_at": spot_snapshot_updated_at(),
            "message_for_ui": "实时观察层未授权联网，显示本地快照缓存。" if not frame.empty else "缺少本地实时快照缓存。",
        }
    if not force and age is not None and age <= REALTIME_CACHE_MAX_AGE_SECONDS:
        frame = load_spot_snapshot_frame()
        return {
            "ok": not frame.empty,
            "status": "fresh_cache",
            "network_authorized": True,
            "row_count": int(len(frame)),
            "cache_age_seconds": int(age),
            "updated_at": spot_snapshot_updated_at(),
            "message_for_ui": "实时快照缓存仍新鲜，暂不重复请求外部数据源。",
        }
    raw = None
    source = ""
    errors: list[str] = []
    provider_chain: list[dict[str, str]] = [
        {"provider": "akshare.full_market_wrappers", "status": "skipped_timeout_risk"}
    ]
    provider_fetchers: list[tuple[str, Any, float]] = [
        ("eastmoney.spot_direct", fetch_eastmoney_spot_direct, 70.0),
        ("sina.spot_direct", fetch_sina_spot_direct, 70.0),
    ]
    for provider_name, fetcher, timeout_seconds in provider_fetchers:
        if fetcher is None:
            provider_chain.append({"provider": provider_name, "status": "missing"})
            continue
        try:
            candidate = _call_with_timeout(fetcher, timeout_seconds=timeout_seconds)
            if candidate is not None and not candidate.empty:
                raw = candidate
                provider_status = str(candidate.attrs.get("provider_status") or "ok")
                provider_warning = str(candidate.attrs.get("provider_warning") or "").strip()
                source = provider_name
                if "source" in candidate.columns:
                    source_values = [str(item).strip() for item in candidate["source"].dropna().unique() if str(item).strip()]
                    if source_values:
                        source = source_values[0]
                provider_chain.append({"provider": provider_name, "status": provider_status})
                if provider_warning:
                    errors.append(f"{provider_name}: {provider_warning}")
                break
            provider_chain.append({"provider": provider_name, "status": "empty"})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider_name}: {exc}")
            provider_chain.append({"provider": provider_name, "status": "failed"})
    if raw is None or raw.empty:
        frame = load_spot_snapshot_frame()
        return {
            "ok": not frame.empty,
            "status": "provider_failed",
            "network_authorized": True,
            "row_count": int(len(frame)),
            "cache_age_seconds": int(age) if age is not None else "数据缺失",
            "updated_at": spot_snapshot_updated_at(),
            "errors": errors[:6],
            "provider_chain": provider_chain,
            "message_for_ui": "实时快照数据源暂不可用，保留本地缓存显示。",
        }

    frame = _canonical_spot_frame(raw)
    if frame.empty:
        return {
            "ok": False,
            "status": "provider_empty_after_normalize",
            "network_authorized": True,
            "row_count": 0,
            "message_for_ui": "实时快照数据源返回数据，但缺少可识别的股票行。",
        }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(SPOT_SNAPSHOT_CACHE, index=False, encoding="utf-8-sig")
    meta = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "row_count": int(len(frame)),
        "research_only": True,
        "temporary_quote_only": True,
        "provider_chain": provider_chain,
        "provider_errors": errors[:6],
        "message": "实时观察层快照，仅用于盘中/实时观察，不覆盖正式完整日线。",
    }
    _write_spot_meta(meta)
    return {
        "ok": True,
        "status": "refreshed",
        "network_authorized": True,
        "source": source,
        "row_count": int(len(frame)),
        "cache_age_seconds": 0,
        "updated_at": meta["updated_at"],
        "errors": errors[:6],
        "provider_chain": provider_chain,
        "message_for_ui": f"已刷新实时快照 {len(frame)} 只；仅用于观察，不覆盖正式日线。",
    }


def eastmoney_secid(symbol: str) -> str:
    clean_symbol = normalize_symbol(symbol)
    market = "0" if clean_symbol.startswith(("4", "8", "92")) else "1" if clean_symbol.startswith(("5", "6", "9")) else "0"
    return f"{market}.{clean_symbol}"


def _em_scaled(value: Any, *, digits: int = 2) -> float | None:
    number = quote_number(value)
    if number is None or number <= -100000:
        return None
    return round(number / 100, digits)


def fetch_eastmoney_single_quote(symbol: str, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    clean_symbol = normalize_symbol(symbol)
    if not clean_symbol:
        return {"ok": False, "symbol": "", "message_for_ui": "股票代码为空。"}
    try:
        import requests
    except ImportError:
        return {"ok": False, "symbol": clean_symbol, "message_for_ui": "未安装 requests，无法走单股实时 fallback。"}

    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": eastmoney_secid(clean_symbol),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields": "f43,f57,f58,f169,f170,f46,f44,f45,f47,f48,f60,f86",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    # The provider closes connections intermittently; one bounded retry keeps a
    # single dropped socket from being reported as "source unavailable".
    payload = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        return {
            "ok": False,
            "symbol": clean_symbol,
            "source": "eastmoney.single_quote",
            "message_for_ui": f"单股实时 fallback 失败（已重试1次）：{last_error}",
        }
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or str(data.get("f57") or "").strip() == "":
        return {
            "ok": False,
            "symbol": clean_symbol,
            "source": "eastmoney.single_quote",
            "message_for_ui": "单股实时 fallback 返回空数据。",
        }
    timestamp = finite_float(data.get("f86"))
    # f86 is the provider's quote clock. Missing f86 must not become "now".
    from datetime import timezone
    try:
        quote_dt = to_shanghai_datetime(datetime.fromtimestamp(timestamp, timezone.utc)) if timestamp and timestamp > 0 else None
    except (ValueError, OverflowError, OSError):
        return {"ok": False, "status": "quote_timestamp_invalid", "source": "eastmoney.single_quote",
                "message_for_ui": "报价源返回无效时间戳，保留已有报价。"}
    collected_at = to_shanghai_datetime().isoformat(timespec="seconds")
    row = {
        "symbol": normalize_symbol(data.get("f57") or clean_symbol),
        "name": str(data.get("f58") or "").strip() or clean_symbol,
        "price": _em_scaled(data.get("f43")),
        "pct_chg": _em_scaled(data.get("f170")),
        "change": _em_scaled(data.get("f169")),
        "pre_close": _em_scaled(data.get("f60")),
        "open": _em_scaled(data.get("f46")),
        "high": _em_scaled(data.get("f44")),
        "low": _em_scaled(data.get("f45")),
        "volume": finite_float(data.get("f47")),
        "amount": finite_float(data.get("f48")),
        "volume_unit": "手",
        "amount_unit": "元",
        "quote_time": quote_dt.strftime("%Y-%m-%d %H:%M:%S") if quote_dt else "",
        "quote_date": quote_dt.date().isoformat() if quote_dt else "",
        "price_basis": "raw",
        "source": "eastmoney.single_quote",
        "updated_at": collected_at,
    }
    valid = quote_dt is not None and (row["price"] or 0) > 0 and row["symbol"] == clean_symbol
    return {
        "ok": valid,
        "status": "single_quote_refreshed" if valid else "quote_evidence_incomplete",
        "symbol": row["symbol"],
        "row": row,
        "source": "eastmoney.single_quote",
        "updated_at": row["updated_at"],
        "message_for_ui": (f"已刷新 {row['name']} {row['symbol']} 单股实时快照；不覆盖正式日线。"
                           if valid else "报价源缺少匹配证券的有效价格或时间，保留已有报价。"),
    }


def upsert_spot_snapshot_row(row: dict[str, Any]) -> None:
    if not row.get("symbol"):
        return
    frame = load_spot_snapshot_frame()
    row_frame = _canonical_spot_frame(pd.DataFrame([row]))
    if row_frame.empty:
        return
    if frame.empty:
        merged = row_frame
    else:
        merged = pd.concat([frame[frame["symbol"] != row_frame.iloc[0]["symbol"]], row_frame], ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    merged.to_csv(SPOT_SNAPSHOT_CACHE, index=False, encoding="utf-8-sig")


def refresh_single_quote_cache(symbol: str) -> dict[str, Any]:
    result = fetch_eastmoney_single_quote(symbol)
    if result.get("ok") and isinstance(result.get("row"), dict):
        upsert_spot_snapshot_row(result["row"])
    return result


def spot_quote_for_symbol(symbol: str) -> dict[str, Any]:
    clean_symbol = normalize_symbol(symbol)
    frame = load_spot_snapshot_frame()
    if frame.empty:
        return {
            "available": False,
            "symbol": clean_symbol,
            "message": "缺少实时快照缓存。",
        }
    hits = frame[frame["symbol"].astype(str).map(normalize_symbol) == clean_symbol]
    if hits.empty:
        return {
            "available": False,
            "symbol": clean_symbol,
            "message": "实时快照中暂未找到该股票。",
        }
    row = hits.iloc[0]
    raw_quote_time = str(row.get("quote_time") or "").strip()
    quote_dt = provider_quote_datetime(row.to_dict())
    quote_date = str(row.get("quote_date") or "").strip() or (quote_dt.date().isoformat() if quote_dt else "")
    quote_time = quote_dt.strftime("%H:%M:%S") if quote_dt else raw_quote_time
    row_updated_at = str(row.get("updated_at") or "").strip()
    row_source = str(row.get("source") or "").strip()
    price = finite_float(row.get("price"))
    return {
        "available": price is not None and price > 0,
        "symbol": clean_symbol,
        "name": str(row.get("name") or "").strip() or clean_symbol,
        "price": price,
        "pct_chg": finite_float(row.get("pct_chg")),
        "change": finite_float(row.get("change")),
        "pre_close": finite_float(row.get("pre_close")),
        "open": finite_float(row.get("open")),
        "high": finite_float(row.get("high")),
        "low": finite_float(row.get("low")),
        "volume": finite_float(row.get("volume")),
        "amount": finite_float(row.get("amount")),
        "quote_date": quote_date,
        "quote_time": quote_time,
        "quote_timestamp": str(row.get("quote_timestamp") or "").strip() or (quote_dt.isoformat(timespec="seconds") if quote_dt else ""),
        "price_basis": str(row.get("price_basis") or "").strip(),
        "updated_at": row_updated_at,
        "source": row_source or read_json_file(SPOT_SNAPSHOT_META).get("source", "local_cache"),
        "temporary": True,
        "label": "盘中/实时临时数据，不覆盖正式日线。",
    }


def _amount_to_yi(value: Any) -> float | None:
    amount = finite_float(value)
    if amount is None or amount <= 0:
        return None
    return amount if amount < 10000 else amount / 100000000


def realtime_market_summary(*, refresh: bool = False, explicit_network: bool = False) -> dict[str, Any]:
    background_refresh: dict[str, Any] = {}
    network_allowed = bool(realtime_network_enabled() or explicit_network)
    if realtime_network_enabled() and refresh:
        age = cache_age_seconds(SPOT_SNAPSHOT_CACHE)
        stale = age is None or age > REALTIME_CACHE_MAX_AGE_SECONDS
        current_background = realtime_background_snapshot()
        retry_age = realtime_background_attempt_age_seconds(current_background)
        retry_ready = retry_age is None or retry_age > REALTIME_PROVIDER_RETRY_SECONDS
        if refresh or current_background.get("running") or (stale and retry_ready):
            background_refresh = start_realtime_background_refresh(
                force=refresh or age is None,
                reason="manual_refresh" if refresh else "stale_cache",
            )
            refresh_result = _cached_spot_refresh_payload(
                network=True,
                status=background_refresh.get("status", "background_refresh"),
                message=background_refresh.get("message_for_ui", "实时观察层正在后台刷新；页面先显示可用缓存。"),
            )
        elif stale:
            background_refresh = current_background
            refresh_result = _cached_spot_refresh_payload(
                network=True,
                status="stale_cache_retry_deferred",
                message=(
                    current_background.get("message_for_ui")
                    or "实时快照缓存偏旧；刚尝试过 provider，自动轮询暂缓重复请求。"
                ),
            )
        else:
            background_refresh = current_background if current_background.get("status") != "idle" else {}
            refresh_result = _cached_spot_refresh_payload(
                network=True,
                status="fresh_cache",
                message="实时快照缓存仍新鲜，暂不重复请求外部数据源。",
            )
    elif explicit_network and refresh:
        refresh_result = refresh_spot_snapshot_cache(network=True, force=True)
        background_refresh = {
            "running": False,
            "status": refresh_result.get("status", "manual_network_refresh"),
            "reason": "manual_network_refresh",
            "spot": refresh_result,
            "message_for_ui": "本次由页面按钮显式联网刷新实时快照。",
        }
    else:
        background_refresh = realtime_background_snapshot()
        refresh_result = _cached_spot_refresh_payload(
            network=False, status="cache_only",
            message="当前显示已保存的快照；需要新报价时使用刷新按钮。",
        )
    sector_stats = calculate_sector_stats()
    sector_cache_usable = sector_cache_has_usable_rows(sector_stats)
    frame = load_spot_snapshot_frame()
    now = to_shanghai_datetime()
    session = get_market_session(now)
    age_seconds = spot_snapshot_age_seconds()
    age_text = human_duration(age_seconds)
    updated_at = refresh_result.get("updated_at") or spot_snapshot_updated_at()
    freshness = realtime_freshness_state(
        session=session,
        frame=frame,
        age_seconds=age_seconds,
        updated_at=updated_at,
        now=now,
    )
    is_stale = bool(freshness["is_cache_stale"])
    spot_meta = read_json_file(SPOT_SNAPSHOT_META)
    background_spot = background_refresh.get("spot") if isinstance(background_refresh.get("spot"), dict) else {}
    provider_error = (
        realtime_provider_error_summary(background_refresh, include_sector=not sector_cache_usable)
        if is_stale
        else ""
    )
    provider_note = realtime_provider_note(background_refresh, sector_cache_usable=sector_cache_usable)
    snapshot_source = (
        refresh_result.get("source")
        or spot_meta.get("source")
        or background_spot.get("source")
        or "local_cache"
    )
    provider_chain = (
        refresh_result.get("provider_chain")
        or background_spot.get("provider_chain")
        or spot_meta.get("provider_chain")
        or []
    )
    refresh_pending = bool(background_refresh.get("running"))
    if frame.empty:
        return {
            "ok": False,
            "status": refresh_result.get("status", "missing"),
            "network_authorized": network_allowed,
            "market_session": session,
            "session_label": market_session_label(session),
            "temporary_quote_only": True,
            "message_for_ui": refresh_result.get("message_for_ui") or "实时快照缺失。",
            "data_freshness_label": "实时快照缺失",
            "is_cache_stale": True,
            "is_live_session": freshness["is_live_session"],
            "is_live_lagging": freshness["is_live_lagging"],
            "is_market_pause": freshness["is_market_pause"],
            "snapshot_quote_date": freshness["snapshot_quote_date"],
            "expected_snapshot_date": freshness["expected_snapshot_date"],
            "freshness_hint": "本地没有可用快照；需要联网刷新或导入快照数据。",
            "cache_age_text": age_text,
            "provider_error": provider_error,
            "provider_note": provider_note,
            "snapshot_source": snapshot_source,
            "provider_chain": provider_chain,
            "refresh_pending": refresh_pending,
            "last_success_at": updated_at or "数据缺失",
            "boundary": "实时观察层仅用于盘中/实时观察，不覆盖正式完整日线，不构成投资建议。",
            "refresh_result": refresh_result,
            "background_refresh": background_refresh,
            "sector_refresh": background_refresh.get("sector", {}),
            "sector_cache_usable": sector_cache_usable,
            "sector_top_gainers": sector_stats.get("gainers", []),
            "sector_top_losers": sector_stats.get("losers", []),
        }
    changes = pd.to_numeric(frame["pct_chg"], errors="coerce")
    amount = pd.to_numeric(frame["amount"], errors="coerce")
    advancing = int((changes > 0).sum())
    declining = int((changes < 0).sum())
    flat = int((changes == 0).sum())
    total_amount = float(amount.dropna().sum()) if not amount.dropna().empty else 0.0
    top_gainers = spot_rank_rows(frame, ascending=False)
    top_losers = spot_rank_rows(frame, ascending=True)
    active_amount = frame.assign(_amount=pd.to_numeric(frame["amount"], errors="coerce")).sort_values("_amount", ascending=False).head(5)
    return {
        "ok": True,
        "status": refresh_result.get("status", "cache"),
        "network_authorized": network_allowed,
        "market_session": session,
        "session_label": market_session_label(session),
        "temporary_quote_only": True,
        "updated_at": updated_at,
        "last_success_at": updated_at or "数据缺失",
        "cache_age_seconds": refresh_result.get("cache_age_seconds", int(age_seconds or 0)),
        "cache_age_text": age_text,
        "is_cache_stale": is_stale,
        "data_freshness_label": freshness["data_freshness_label"],
        "is_live_session": freshness["is_live_session"],
        "is_live_lagging": freshness["is_live_lagging"],
        "is_market_pause": freshness["is_market_pause"],
        "snapshot_quote_date": freshness["snapshot_quote_date"],
        "expected_snapshot_date": freshness["expected_snapshot_date"],
        "freshness_hint": freshness["freshness_hint"],
        "provider_error": provider_error,
        "provider_note": provider_note,
        "snapshot_source": snapshot_source,
        "provider_chain": provider_chain,
        "refresh_pending": refresh_pending,
        "sample_count": int(len(frame)),
        "advancing_count": advancing,
        "declining_count": declining,
        "flat_count": flat,
        "total_amount": f"{total_amount / 100000000:.2f}亿元" if total_amount > 10000 else f"{total_amount:.2f}亿元",
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "sector_top_gainers": sector_stats.get("gainers", []),
        "sector_top_losers": sector_stats.get("losers", []),
        "active_amount": [
            {
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "price": chart_number(row.get("price")),
                "pct_chg": chart_number(row.get("pct_chg"), 0.0),
                "amount": f"{(_amount_to_yi(row.get('amount')) or 0):.2f}亿元",
            }
            for _, row in active_amount.iterrows()
        ],
        "message_for_ui": refresh_result.get("message_for_ui"),
        "boundary": "实时观察层仅用于盘中/实时观察，不覆盖正式完整日线，不构成投资建议。",
        "refresh_result": refresh_result,
        "background_refresh": background_refresh,
        "sector_refresh": background_refresh.get("sector", {}),
        "sector_cache_usable": sector_cache_usable,
    }


def spot_rank_rows(frame: pd.DataFrame, *, ascending: bool) -> list[dict[str, Any]]:
    ranked = frame.copy()
    ranked["_pct"] = pd.to_numeric(ranked["pct_chg"], errors="coerce")
    ranked = ranked.dropna(subset=["_pct"]).sort_values("_pct", ascending=ascending).head(5)
    rows: list[dict[str, Any]] = []
    for _, row in ranked.iterrows():
        rows.append(
            {
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "price": chart_number(row.get("price")),
                "pct_chg": chart_number(row.get("pct_chg"), 0.0),
                "amount": f"{(_amount_to_yi(row.get('amount')) or 0):.2f}亿元",
            }
        )
    return rows


def market_session_label(session: str) -> str:
    return {
        "pre_open": "盘前",
        "morning_session": "早盘交易中",
        "lunch_break": "午间休市",
        "afternoon_session": "午后交易中",
        "after_close": "收盘后",
        "non_trading_day": "非交易日",
        "market_holiday": "休市日",
    }.get(session, session or "未知时段")


def read_theme_map() -> pd.DataFrame:
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    csv_read_warnings: list[str] = []
    # Formal map wins. Import drafts are allowed to fill symbols not yet promoted.
    for path in (UNIVERSE_DIR / "stock_theme_map.csv", UNIVERSE_DIR / "stock_theme_map_import.csv"):
        if not path.exists():
            continue
        try:
            df = read_csv_frame(path, dtype=str)
        except Exception as exc:  # noqa: BLE001 - one bad theme-map file must not hide the other
            LOG.warning("theme_map_read_failed: path=%s error=%s", path, exc)
            continue
        warnings = list(df.attrs.get("warnings", []) or [])
        csv_read_warnings.extend(str(item) for item in warnings)
        for warning in warnings:
            LOG.warning("theme_map_read_warning: path=%s warning=%s", path, warning)
        if df.empty:
            continue
        for _, row in df.iterrows():
            symbol = normalize_symbol(str(row.get("symbol") or row.get("ts_code") or row.get("code") or ""))
            if not symbol or symbol in rows_by_symbol:
                continue
            item = dict(row)
            item["symbol"] = symbol
            rows_by_symbol[symbol] = item
    frame = pd.DataFrame(rows_by_symbol.values()) if rows_by_symbol else pd.DataFrame()
    frame.attrs["warnings"] = csv_read_warnings
    return frame


def _board_name(code: str) -> str:
    """Derive board/segment name from A-share code prefix."""
    code = code.strip().lower()
    if code.startswith("sh688") or code.startswith("688"):
        return "科创板"
    if code.startswith(("sh300", "sz300", "300", "301")):
        return "创业板"
    if code.startswith(("bj", "8", "9")) and len("".join(ch for ch in code if ch.isdigit())) == 6:
        digits = "".join(ch for ch in code if ch.isdigit())
        if digits.startswith(("8", "9")):
            return "北交所"
    digits = "".join(ch for ch in code if ch.isdigit())[-6:]
    if digits.startswith("6"):
        return "上海主板"
    if digits.startswith(("0", "2")):
        return "深圳主板"
    return "其他板块"


def calculate_sector_stats() -> dict[str, Any]:
    """Real industry-sector gainers/losers from the local industry-board cache.

    The cache (``data/cache/industry_board_latest.csv``) is populated on demand by
    ``POST /api/sector/refresh`` which calls ``akshare.stock_board_industry_name_em()``.
    When the cache is missing or empty the panel degrades to a clear hint instead of
    showing the misleading exchange-board breakdown (主板/创业板/科创板) it used to.
    """
    missing_hint = "缺少行业板块数据。点击右侧“刷新板块”按钮联网拉取东方财富行业板块涨跌榜（仅本地缓存，不接券商）。"
    if not INDUSTRY_BOARD_CACHE.exists():
        missing = [{"name": "板块数据缺失", "change": "数据缺失", "hint": missing_hint}]
        return {"gainers": missing, "losers": missing, "stale": True}

    board_df = read_csv_frame(INDUSTRY_BOARD_CACHE)
    csv_read_warnings = list(board_df.attrs.get("warnings", []) or [])
    if board_df.empty and csv_read_warnings:
        missing = [{"name": "板块数据缺失", "change": "数据缺失", "hint": f"行业板块缓存读取失败：{'; '.join(csv_read_warnings[:2])}"}]
        return {"gainers": missing, "losers": missing, "stale": True, "warnings": csv_read_warnings}

    name_col = find_column(board_df, "name") or find_column(board_df, "板块名称")
    change_col = find_column(board_df, "pct_chg") or find_column(board_df, "涨跌幅")
    amount_col = find_column(board_df, "amount") or find_column(board_df, "总成交额")
    if not name_col or not change_col:
        missing = [{"name": "板块数据缺失", "change": "数据缺失", "hint": "行业板块缓存缺少必要列，请重新刷新。"}]
        return {"gainers": missing, "losers": missing, "stale": True}

    rows: list[dict[str, Any]] = []
    for _, srow in board_df.iterrows():
        name = str(srow.get(name_col, "")).strip()
        if not name or name.lower() == "nan":
            continue
        change = finite_float(srow.get(change_col))
        if change is None:
            continue
        amount = finite_float(srow.get(amount_col)) if amount_col else None
        amount_yi = _amount_to_yi(amount)
        hint = f"成交额 {amount_yi:.0f}亿" if amount_yi and amount_yi > 0 else "东方财富行业板块"
        rows.append({"name": name, "change": round(change, 2), "hint": hint})

    if not rows:
        missing = [{"name": "板块数据缺失", "change": "数据缺失", "hint": missing_hint}]
        return {"gainers": missing, "losers": missing, "stale": True}

    rows.sort(key=lambda item: item["change"], reverse=True)
    as_of = snapshot_quote_date(board_df)
    return {
        "gainers": rows[:5],
        "losers": list(reversed(rows[-5:])),
        "stale": False,
        "as_of": as_of,
        "freshness_status": "recorded_date" if as_of else "unknown",
        "cache_modified_at": datetime.fromtimestamp(INDUSTRY_BOARD_CACHE.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        "source": "data/cache/industry_board_latest.csv",
        "warnings": csv_read_warnings,
    }


def _industry_board_cache_target() -> tuple[Path, str]:
    """Resolve and validate the board-cache destination before provider work.

    A staging caller can replace ``project_root`` and ``INDUSTRY_BOARD_CACHE``
    through its isolated dependency fixture.  Both values must describe the
    same root.  Validating only while formatting the response is too late: the
    CSV has already been written by then, which caused the Round 10 validation
    incident when a staging root accidentally retained the production cache
    global.
    """

    root = Path(project_root).resolve()
    target = Path(INDUSTRY_BOARD_CACHE).resolve()
    allowed_root = (root / "data" / "cache").resolve()
    try:
        relative = target.relative_to(root)
        target.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("industry_board_cache_outside_project_root") from exc
    return target, relative.as_posix()


def refresh_industry_board_cache() -> dict[str, Any]:
    """Fetch industry-board data and cache locally.

    Tries Eastmoney first (own delayed-host client, then akshare), falls back
    to THS/Tonghuashun (stock_board_industry_summary_ths) when EM is unavailable.

    Round 10 Part B: the first attempt goes through
    :mod:`quantdesk.data_truth.em_sector_provider`, which targets the
    ``push2delay`` hosts.  akshare's own Eastmoney helper hardcodes numbered
    ``push2`` hosts that currently answer ``502`` from Eastmoney's gateway, so it
    always failed here and silently demoted this cache to the THS taxonomy.  That
    demotion is what kept the forward PIT sector archive unaligned: board quotes
    were THS while membership capture needs the same taxonomy on both ends.
    akshare is retained as the second Eastmoney attempt so a future akshare fix
    or a delayed-host outage still has a path.
    """
    import time as _time

    cache_target, cache_relative = _industry_board_cache_target()
    raw = None
    source = ""

    # Eastmoney attempt 1: delayed hosts, which akshare cannot currently reach.
    try:
        from quantdesk.data_truth.em_sector_provider import fetch_industry_board_list

        raw = fetch_industry_board_list()
        if raw is not None and not raw.empty:
            source = "eastmoney"
    except Exception as exc:  # noqa: BLE001 - fall through to the akshare attempt
        LOG.warning("eastmoney_delayed_sector_refresh_failed: %s", exc)
        raw = None

    try:
        import akshare as ak
    except ImportError:
        if raw is not None and not raw.empty:
            ak = None  # type: ignore[assignment]
        else:
            return {"ok": False, "message_for_ui": "未安装 akshare，无法拉取行业板块。"}

    # Eastmoney attempt 2: akshare (3 retries)
    if ak is not None and (raw is None or (hasattr(raw, 'empty') and raw.empty)):
        for attempt in range(3):
            try:
                raw = ak.stock_board_industry_name_em()
                if raw is not None and not raw.empty:
                    source = "eastmoney"
                    break
            except Exception as exc:  # noqa: BLE001 - provider retry path records the failure then degrades
                LOG.warning("eastmoney_sector_refresh_attempt_failed: attempt=%s error=%s", attempt + 1, exc)
                if attempt < 2:
                    _time.sleep(2 * (attempt + 1))

    # Fallback to THS.  This is a different taxonomy from Eastmoney, so landing
    # here leaves the forward PIT sector archive unaligned by construction; the
    # ``source`` field is what lets the archive detect and report that.
    if ak is not None and (raw is None or (hasattr(raw, 'empty') and raw.empty)):
        try:
            raw = ak.stock_board_industry_summary_ths()
            if raw is not None and not raw.empty:
                source = "ths"
        except Exception as exc:  # noqa: BLE001 - fallback provider failure is visible to logs and UI result
            LOG.warning("ths_sector_refresh_failed: %s", exc)

    if raw is None or (hasattr(raw, 'empty') and raw.empty):
        return {"ok": False, "message_for_ui": "行业板块接口均不可用，请稍后重试或检查网络。"}

    # Normalize to canonical columns
    cache_target.parent.mkdir(parents=True, exist_ok=True)
    if source == "ths":
        rename_map = {
            "板块": "name", "涨跌幅": "pct_chg",
            "总成交量": "volume", "总成交额": "amount",
            "领涨股": "leading_stock", "领涨股-涨跌幅": "leading_pct_chg",
        }
        board_df = raw.rename(columns={k: v for k, v in rename_map.items() if k in raw.columns})
    else:
        rename_map = {
            "板块名称": "name", "板块代码": "code", "最新价": "close",
            "涨跌额": "change", "涨跌幅": "pct_chg",
            "总成交量": "volume", "总成交额": "amount",
            "领涨股票": "leading_stock", "领涨股票-涨跌幅": "leading_pct_chg",
        }
        board_df = raw.rename(columns={k: v for k, v in rename_map.items() if k in raw.columns})

    keep = [c for c in ("name", "code", "close", "pct_chg", "amount", "leading_stock") if c in board_df.columns]
    board_df = board_df[keep].copy()
    board_df.to_csv(cache_target, index=False, encoding="utf-8-sig")
    return {
        "ok": True,
        "source": source,
        "board_count": int(len(board_df)),
        "message_for_ui": f"已刷新 {len(board_df)} 个行业板块（{source}）。",
        "cache_path": cache_relative,
    }
