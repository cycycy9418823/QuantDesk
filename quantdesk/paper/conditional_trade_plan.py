"""Conditional trade-plan builder for the automatic paper portfolio.

The builder freezes next-trading-day paper-trading plans from local T-day
data. It never connects to brokers, never places real orders, and never uses
T+1 data to build a T+1 intraday plan.
"""

from __future__ import annotations

import csv
import copy
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.candidate_contract import (
    candidate_rows_from_strategy_payload,
    normalize_strategy_type,
    strategy_label,
    summarize_candidates_by_strategy,
)
from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_session_evidence import load_verified_no_session_dates
from quantdesk.market_clock import next_cn_a_share_trading_day
from quantdesk.paper.market_rules import lot_size_for_symbol, paper_only_boundary, round_down_to_lot
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.price_basis import (
    PRICE_BASIS_RAW,
    classify_frame_price_basis,
    validate_price_frame_set,
)
from quantdesk.shared import normalize_symbol, now_text, truthy
from quantdesk.utils import atomic_write_json, read_json_safe, safe_float, safe_int
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
STATE_DIR = PROJECT_ROOT / "state"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
FEATURE_DIR = PROJECT_ROOT / "data" / "features"
UNIVERSE_DIR = PROJECT_ROOT / "data" / "universe"

AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"
DAILY_FEATURE_LATEST_CSV = FEATURE_DIR / "daily_features_latest.csv"
LOW_PRICE_UNIVERSE_CSV = UNIVERSE_DIR / "low_price_learning_symbols.csv"
CONDITIONAL_TRADE_PLANS_LATEST_JSON = RESULTS_DIR / "conditional_trade_plans_latest.json"

SCHEMA_VERSION = "2.8.4"
DISCLAIMER = "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。不接券商，不真实下单。"
SCOPE = "low_price_included_only"

# 单笔纸面持仓的最大可承受亏损地板。止损带由滚动 support 决定，价格持续下跌时
# support 会一起下移，使 min(entry*0.93, support*0.98) 成为一条无底的带子 ——
# 实测 601975 浮亏 -29.8% 仍从未触发。该常数把止损价钉在 entry*(1-此值) 之上，
# 保证亏损扩大到此幅度时必然认亏。它只做地板，不改变正常行情下更早触发的技术止损。
MAX_LOSS_FLOOR_PCT = 0.20

DAILY_ALIASES = {
    "日期": "date",
    "交易日期": "date",
    "date": "date",
    "开盘": "open",
    "开盘价": "open",
    "open": "open",
    "最高": "high",
    "最高价": "high",
    "high": "high",
    "最低": "low",
    "最低价": "low",
    "low": "low",
    "收盘": "close",
    "收盘价": "close",
    "close": "close",
    "preclose": "prev_close",
    "prev_close": "prev_close",
    "成交额": "amount",
    "amount": "amount",
    "成交量": "volume",
    "volume": "volume",
    "换手率": "turnover",
    "turn": "turnover",
    "turnover": "turnover",
}


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [{str(k or "").strip(): str(v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]
    except Exception:
        return []


def _read_low_price_allowed_symbols(path: Path = LOW_PRICE_UNIVERSE_CSV) -> set[str] | None:
    if not path.exists():
        return None
    allowed: set[str] = set()
    for row in _read_csv_rows(path):
        if truthy(row.get("included")):
            symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码"))
            if symbol:
                allowed.add(symbol)
    return allowed


def _candidate_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("symbol") or "")


def _candidate_price(row: dict[str, Any]) -> float | None:
    source = row.get("source_item") if isinstance(row.get("source_item"), dict) else {}
    for candidate in (row, source):
        for key in ("close", "latest_price", "price", "entry_price", "reference_close"):
            number = safe_float(candidate.get(key), None)
            if number is not None and number > 0:
                return number
    planned_amount = safe_float(row.get("planned_amount"), None)
    quantity = safe_float(row.get("quantity"), None)
    if planned_amount is not None and quantity and quantity > 0:
        return planned_amount / quantity
    return None


def _read_daily_frame(symbol: str, daily_dir: Path = DAILY_DIR, daily_cache: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    normalized = normalize_symbol(symbol)
    if daily_cache is not None and normalized in daily_cache:
        cached = daily_cache.get(normalized)
        frame = cached.copy() if isinstance(cached, pd.DataFrame) else pd.DataFrame()
        path = daily_dir / f"{normalized}.csv"
    else:
        path = daily_dir / f"{normalized}.csv"
        if not path.exists():
            return pd.DataFrame()
        frame = read_csv_frame(path)
    csv_read_warnings = list(frame.attrs.get("warnings") or [])
    price_basis, price_basis_reason = classify_frame_price_basis(frame)
    if frame.empty:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings
        empty.attrs["observed_price_basis"] = price_basis
        empty.attrs["price_basis_reason"] = price_basis_reason
        return empty
    renamed = frame.rename(columns={col: DAILY_ALIASES.get(str(col), str(col)) for col in frame.columns}).copy()
    if "date" not in renamed.columns or "close" not in renamed.columns:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = csv_read_warnings + [f"daily_required_columns_missing:{path}"]
        return empty
    renamed["date"] = pd.to_datetime(renamed["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "prev_close", "amount", "volume", "turnover"):
        if column in renamed.columns:
            renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    for column in ("open", "high", "low"):
        if column not in renamed.columns:
            renamed[column] = renamed["close"]
    renamed = renamed.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    renamed = renamed[renamed["close"] > 0].reset_index(drop=True)
    renamed.attrs["warnings"] = csv_read_warnings
    renamed.attrs["observed_price_basis"] = price_basis
    renamed.attrs["price_basis_reason"] = price_basis_reason
    return renamed


def _daily_until_cutoff(
    symbol: str,
    cutoff: str,
    daily_dir: Path = DAILY_DIR,
    lookback_days: int = 250,
    daily_cache: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    df = _read_daily_frame(symbol, daily_dir=daily_dir, daily_cache=daily_cache)
    if df.empty:
        return df
    original_attrs = dict(df.attrs)
    cutoff_ts = pd.to_datetime(cutoff, errors="coerce")
    if pd.notna(cutoff_ts):
        df = df[df["date"] <= cutoff_ts]
    if lookback_days > 0 and len(df) > lookback_days:
        df = df.tail(lookback_days)
    result = df.reset_index(drop=True)
    result.attrs.update(original_attrs)
    return result


def _price_data_evidence(
    *,
    symbol: str,
    target_date: str,
    daily_df: pd.DataFrame,
    price_policy_row: dict[str, Any],
    suspended_dates: set[str],
    session_evidence: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    price_basis = str(
        daily_df.attrs.get("observed_price_basis")
        or price_policy_row.get("price_basis")
        or "mixed_or_unknown"
    )
    policy_reason = str(
        price_policy_row.get("reason_code")
        or daily_df.attrs.get("price_basis_reason")
        or ""
    )
    if policy_reason:
        reasons.append(policy_reason)
    elif price_basis != PRICE_BASIS_RAW:
        reasons.append("daily_price_basis_not_raw")

    latest_date = ""
    if not daily_df.empty and "date" in daily_df.columns:
        latest = pd.to_datetime(daily_df["date"], errors="coerce").dropna()
        if not latest.empty:
            latest_date = latest.max().strftime("%Y-%m-%d")
    verified_target_suspension = bool(
        target_date and target_date in suspended_dates and session_evidence
    )
    if not latest_date:
        reasons.append("daily_price_frame_missing")
    elif latest_date != target_date and not verified_target_suspension:
        reasons.append("daily_target_date_missing")

    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    eligible = not reasons
    return {
        "symbol": symbol,
        "status": "passed" if eligible else "blocked",
        "formal_execution_eligible": eligible,
        "target_date": target_date,
        "latest_daily_date": latest_date,
        "price_basis": price_basis,
        "verified_target_suspension": verified_target_suspension,
        "session_evidence": {
            "source": str(session_evidence.get("source") or ""),
            "captured_at": str(session_evidence.get("captured_at") or ""),
            "records_sha256": str(session_evidence.get("records_sha256") or ""),
        }
        if verified_target_suspension
        else {},
        "reason_codes": reasons,
    }


def _read_daily_feature_map(path: Path = DAILY_FEATURE_LATEST_CSV, cutoff: str = "") -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    for row in _read_csv_rows(path):
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        latest_date = str(row.get("latest_date") or "")[:10]
        if cutoff and latest_date and latest_date > cutoff:
            # Future feature rows cannot shape a frozen T+1 plan.
            continue
        rows[symbol] = row
    return rows


def _infer_plan_date(auto_payload: dict[str, Any], feature_map: dict[str, dict[str, Any]], state: dict[str, Any], explicit: str = "") -> str:
    if explicit:
        return explicit[:10]
    for value in (auto_payload.get("generated_at"), auto_payload.get("latest_daily_date"), state.get("updated_at")):
        text = str(value or "")[:10]
        if len(text) == 10:
            return text
    feature_dates = sorted({str(row.get("latest_date") or "")[:10] for row in feature_map.values() if str(row.get("latest_date") or "")[:10]})
    if feature_dates:
        return feature_dates[-1]
    return datetime.now().strftime("%Y-%m-%d")


def _infer_trade_date(plan_date: str, explicit: str = "") -> str:
    if explicit:
        return explicit[:10]
    try:
        base = date.fromisoformat(plan_date[:10])
        return next_cn_a_share_trading_day(base).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _rolling_value(df: pd.DataFrame, column: str, window: int, fallback: float | None = None) -> float | None:
    if df.empty or column not in df.columns:
        return fallback
    series = pd.to_numeric(df[column], errors="coerce").dropna()
    if series.empty:
        return fallback
    if len(series) < window:
        return fallback if fallback is not None else float(series.iloc[-1])
    return float(series.tail(window).mean())


def _feature_float(feature: dict[str, Any], key: str) -> float | None:
    return safe_float(feature.get(key), None)


def _bool_feature(feature: dict[str, Any], key: str) -> bool:
    return str(feature.get(key) or "").strip().lower() in {"true", "1", "yes"}


def _stop_loss_ratchet(
    *,
    daily_df: pd.DataFrame,
    entry_price: float,
    entry_date: str | None,
) -> dict[str, Any]:
    """自入场以来"这个计划曾经宣布过的最高止损价"。

    问题背景（磁盘实测，2026-08-06）：止损带每次例行都按**当日**滚动
    `support = max(ma20, low_20 * 1.02)` 重算。价格一路下跌时 support 跟着下移，
    带子也随之下移；而 2026-05-14 至 08-05 的 59 个交易日里例行只跑了 18 天，
    漏跑日触发的退出信号等下次运行时带子已经移开、不再匹配。

    棘轮把止损价改成**价格历史的函数**，而不是**运行历史的函数**：取入场以来
    每一天各自算出的止损价的最大值。因此漏跑一天不再丢信号——带子留在原处，
    下一个真正被评估到的交易日只要价格仍在带下，就照样触发。

    只读推导，不落盘、不新增持仓字段：每次都能从日线重算出同一个值。
    仍然不凭日线成交——是否成交由分钟证据决定，本函数只决定带子在哪里。
    """

    empty: dict[str, Any] = {"price": None, "as_of": "", "days": 0, "interval_low": None}
    if daily_df.empty or not entry_price or entry_price <= 0:
        return empty
    if not {"close", "low", "date"}.issubset(set(daily_df.columns)):
        return empty
    close = pd.to_numeric(daily_df["close"], errors="coerce")
    low = pd.to_numeric(daily_df["low"], errors="coerce")
    ma20 = close.rolling(20, min_periods=20).mean()
    low20 = low.rolling(20, min_periods=20).min()
    support = pd.concat([ma20, low20 * 1.02], axis=1).max(axis=1)
    # 每一天各自的技术止损，上限仍是 entry * 0.93——棘轮只能托住带子，不能把它抬高。
    stops = (support * 0.98).clip(upper=entry_price * 0.93)
    frame = pd.DataFrame({"date": daily_df["date"], "stop": stops, "low": low}).dropna(
        subset=["stop"]
    )
    entry_ts = pd.to_datetime(entry_date, errors="coerce") if entry_date else pd.NaT
    if pd.notna(entry_ts):
        frame = frame[frame["date"] >= entry_ts]
    if frame.empty:
        return empty
    best_idx = frame["stop"].idxmax()
    return {
        "price": float(frame.loc[best_idx, "stop"]),
        "as_of": str(frame.loc[best_idx, "date"])[:10],
        "days": int(len(frame)),
        "interval_low": safe_float(frame["low"].min(), None),
    }


def _price_context(
    *,
    symbol: str,
    reference_close: float,
    entry_price: float | None,
    daily_df: pd.DataFrame,
    feature: dict[str, Any],
    entry_date: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = list(daily_df.attrs.get("warnings") or [])
    latest = daily_df.iloc[-1] if not daily_df.empty else {}
    yesterday_high = safe_float(latest.get("high") if isinstance(latest, pd.Series) else None, None)
    ma5 = _feature_float(feature, "ma5") or _rolling_value(daily_df, "close", 5, reference_close)
    ma10 = _feature_float(feature, "ma10") or _rolling_value(daily_df, "close", 10, reference_close)
    ma20 = _feature_float(feature, "ma20") or _rolling_value(daily_df, "close", 20, None)
    if ma20 is None:
        ma20 = reference_close * 0.97
        warnings.append("ma20 缺失，使用 reference_close * 0.97 作为粗略支撑。")
    recent_high_20 = _feature_float(feature, "high_20")
    recent_low_20 = _feature_float(feature, "low_20")
    if recent_high_20 is None and not daily_df.empty and "high" in daily_df.columns:
        recent_high_20 = safe_float(daily_df["high"].tail(20).max(), None)
    if recent_low_20 is None and not daily_df.empty and "low" in daily_df.columns:
        recent_low_20 = safe_float(daily_df["low"].tail(20).min(), None)
    if recent_low_20 is None:
        recent_low_20 = reference_close * 0.95
        warnings.append("recent_low_20 缺失，使用 reference_close * 0.95 估算支撑。")
    if recent_high_20 is None:
        warnings.append("recent_high_20 缺失，不生成突破规则。")
    recent_high_60 = None
    recent_low_60 = None
    if not daily_df.empty:
        if "high" in daily_df.columns:
            recent_high_60 = safe_float(daily_df["high"].tail(60).max(), None)
        if "low" in daily_df.columns:
            recent_low_60 = safe_float(daily_df["low"].tail(60).min(), None)
    pressure_price = recent_high_60 or recent_high_20 or reference_close * 1.06
    support_price = max(float(ma20 or reference_close * 0.97), float(recent_low_20 or reference_close * 0.95) * 1.02)
    no_chase_above = reference_close * 1.08
    breakout_price = None
    if recent_high_20 is not None:
        high_base = yesterday_high or recent_high_20
        breakout_price = min(high_base * 1.01, recent_high_20 * 1.005)
    if entry_price is None or entry_price <= 0:
        warnings.append("entry_price 缺失，不生成止盈半仓规则。")
        take_profit_half_price = None
        stop_loss_price = reference_close * 0.93
        stop_source = "reference_close * 0.93"
        ratchet = {"price": None, "as_of": "", "days": 0, "interval_low": None}
    else:
        take_profit_half_price = max(entry_price * 1.08, pressure_price * 0.995)
        # 硬下限：止损带原为 min(entry*0.93, support*0.98)，而 support 由滚动
        # max(ma20, low_20*1.02) 决定 —— 价格一路下跌时 support 跟着下移，止损带
        # 也随之下移，形成"永远差一点触发"的无底带。601975 自 2026-05-14 持仓至
        # 08-05 浮亏 -29.8%、002115 -18.7%、600744 -17.1%，三者均从未触发止损：
        # 逐日复算显示 close 始终落在当日重算后的带子上方。
        #
        # 加入 MAX_LOSS_FLOOR_PCT 后，无论 support 跌到哪里，止损价不会低于
        # entry*(1-0.20)。这是"必须认亏"的地板，不是收紧的信号线：
        # min() 仍在正常行情下给出更早的技术止损。
        #
        # 棘轮（_stop_loss_ratchet）补上另一半：带子不再随例行是否运行而漂移，
        # 漏跑日不再把退出信号弄丢。三者取最高，谁绑定就在 source 里写明。
        raw_stop = min(entry_price * 0.93, support_price * 0.98)
        floor_stop = entry_price * (1.0 - MAX_LOSS_FLOOR_PCT)
        ratchet = _stop_loss_ratchet(
            daily_df=daily_df, entry_price=entry_price, entry_date=entry_date
        )
        # 顺序即平局优先级：并列时报 technical，[*_binding] 只在严格更高时出现。
        # 棘轮的最后一天与 raw_stop 算的是同一个量，只是求和顺序不同
        # （rolling(20).mean() vs tail(20).mean()），会差 1e-12 量级。价格保留
        # 4 位小数，所以低于半个最小刻度的"更高"不算绑定，否则每天都会挂上
        # 一条毫无信息量的 ratchet_binding 警告。
        candidates: dict[str, float] = {"technical": raw_stop, "floor": floor_stop}
        ratchet_price = ratchet.get("price")
        if ratchet_price is not None and float(ratchet_price) > max(raw_stop, floor_stop) + 5e-5:
            candidates["ratchet"] = float(ratchet_price)
        binding = max(candidates, key=lambda key: candidates[key])
        stop_loss_price = candidates[binding]
        stop_source = (
            "max(min(entry_price * 0.93, support_price * 0.98), "
            f"entry_price * {1.0 - MAX_LOSS_FLOOR_PCT:.2f}"
            + (", ratchet_since_entry" if ratchet.get("price") is not None else "")
            + ")"
        )
        if binding == "floor":
            stop_source += " [floor_binding]"
            warnings.append(
                "stop_loss_floor_binding: 滚动 support 已把技术止损压到"
                f" {raw_stop:.4f}，低于最大可承受亏损地板 {floor_stop:.4f}；"
                f"改用地板价（最大亏损 {MAX_LOSS_FLOOR_PCT:.0%}）。"
            )
        elif binding == "ratchet":
            stop_source += " [ratchet_binding]"
            warnings.append(
                "stop_loss_ratchet_binding: 当日重算的技术止损"
                f" {raw_stop:.4f} 低于入场以来曾宣布过的最高止损"
                f" {stop_loss_price:.4f}（{ratchet.get('as_of') or '?'}，"
                f"覆盖 {ratchet.get('days')} 个交易日）；带子不随例行漏跑下移。"
            )
        interval_low = ratchet.get("interval_low")
        if interval_low is not None and float(interval_low) <= stop_loss_price:
            warnings.append(
                "stop_loss_breached_while_unevaluated: 入场以来日线最低"
                f" {float(interval_low):.4f} 已在止损带 {stop_loss_price:.4f} 之下；"
                "若该日例行未运行，其退出信号不可追溯——本行只作观测，"
                "不据日线补成交、不补放历史。"
            )
    return {
        "symbol": symbol,
        "ma5": round(float(ma5 or reference_close), 4),
        "ma10": round(float(ma10 or reference_close), 4),
        "ma20": round(float(ma20 or reference_close), 4),
        "recent_high_20": round(float(recent_high_20), 4) if recent_high_20 is not None else None,
        "recent_low_20": round(float(recent_low_20), 4) if recent_low_20 is not None else None,
        "recent_high_60": round(float(pressure_price), 4) if pressure_price is not None else None,
        "recent_low_60": round(float(recent_low_60), 4) if recent_low_60 is not None else None,
        "support_price": round(float(support_price), 4),
        "support_source": "max(ma20, recent_low_20 * 1.02)",
        "pressure_price": round(float(pressure_price), 4),
        "pressure_source": "recent_high_60",
        "breakout_price": round(float(breakout_price), 4) if breakout_price is not None else None,
        "breakout_source": "min(yesterday_high * 1.01, recent_high_20 * 1.005)" if breakout_price is not None else "",
        "stop_loss_price": round(float(stop_loss_price), 4),
        "stop_loss_source": stop_source,
        "stop_loss_ratchet_price": (
            round(float(ratchet["price"]), 4) if ratchet.get("price") is not None else None
        ),
        "stop_loss_ratchet_as_of": ratchet.get("as_of") or "",
        "stop_loss_ratchet_days": int(ratchet.get("days") or 0),
        "interval_low_since_entry": (
            round(float(ratchet["interval_low"]), 4)
            if ratchet.get("interval_low") is not None
            else None
        ),
        "take_profit_half_price": round(float(take_profit_half_price), 4) if take_profit_half_price is not None else None,
        "take_profit_source": "max(entry_price * 1.08, pressure_price * 0.995)" if take_profit_half_price is not None else "",
        "no_chase_above": round(float(no_chase_above), 4),
        "no_chase_source": "reference_close * 1.08",
        "is_limit_up_like": _bool_feature(feature, "is_limit_up_like"),
        "is_limit_down_like": _bool_feature(feature, "is_limit_down_like"),
        "warnings": warnings,
    }


def _risk_guards(feature: dict[str, Any], context: dict[str, Any], *, holding: bool) -> list[dict[str, Any]]:
    guards = []
    data_health = str(feature.get("data_health_status") or "").strip().lower()
    liquidity = str(feature.get("liquidity_status") or "").strip().lower()
    if data_health in {"blocked", "bad", "error"}:
        guards.append({
            "guard": "data_blocked",
            "effect": "block",
            "reason": "数据可信度 blocked 时不执行新增、加仓或卖出模拟成交。",
        })
    if liquidity in {"very_low", "blocked"}:
        guards.append({
            "guard": "very_low_liquidity",
            "effect": "block",
            "reason": "流动性 very_low 时不执行条件成交模拟。",
        })
    guards.append({
        "guard": "no_chase",
        "trigger_type": "too_high_to_add" if holding else "too_high_to_buy",
        "price_above": context["no_chase_above"],
        "effect": "block_add" if holding else "block_new_buy",
        "reason": "价格超过不追高线，不执行新增买入或加仓。",
    })
    return guards


def _entry_rules(context: dict[str, Any], quantity: int) -> list[dict[str, Any]]:
    rules = [{
        "action": "buy",
        "trigger_type": "pullback_buy",
        "price_low": context["support_price"],
        "price_high": round(context["support_price"] * 1.02, 4),
        "quantity": quantity,
        "reason": "盘中回踩计划低吸区间，模拟买入 100 股。",
    }]
    if context.get("breakout_price") is not None:
        rules.append({
            "action": "buy",
            "trigger_type": "breakout_buy",
            "price_low": context["breakout_price"],
            "price_high": context["no_chase_above"],
            "quantity": quantity,
            "reason": "盘中突破计划突破区间，且未超过不追高价格，模拟趋势跟随买入。",
        })
    return rules


def _add_rules(context: dict[str, Any], quantity: int) -> list[dict[str, Any]]:
    rules = [{
        "action": "add",
        "trigger_type": "pullback_add",
        "price_low": context["support_price"],
        "price_high": round(context["support_price"] * 1.02, 4),
        "quantity": quantity,
        "reason": "回踩支撑区但未跌破趋势，模拟加仓 100 股。",
    }]
    if context.get("breakout_price") is not None:
        rules.append({
            "action": "breakout_add",
            "trigger_type": "breakout_add",
            "price_low": context["breakout_price"],
            "price_high": context["no_chase_above"],
            "quantity": quantity,
            "reason": "盘中突破计划突破区间，且未超过不追高价格，模拟趋势跟随加仓。",
        })
    return rules


def _exit_rules(context: dict[str, Any]) -> list[dict[str, Any]]:
    rules = [{
        "action": "sell_all",
        "trigger_type": "stop_loss",
        "price_low": 0.0,
        "price_high": context["stop_loss_price"],
        "quantity_pct": 100,
        "reason": "跌破关键支撑，趋势假设失效，模拟清仓。",
    }]
    if context.get("take_profit_half_price") is not None:
        rules.append({
            "action": "reduce_half",
            "trigger_type": "take_profit_half",
            "price_low": context["take_profit_half_price"],
            "price_high": round(context["no_chase_above"], 4),
            "quantity_pct": 50,
            "reason": "冲高至压力区，模拟卖出一半锁定部分收益。",
        })
    return rules


def _position_key(row: dict[str, Any]) -> tuple[str, str]:
    return normalize_symbol(row.get("symbol")), normalize_strategy_type(row.get("strategy_type"), "long")


def _build_item(
    *,
    symbol: str,
    name: str,
    strategy_type: str,
    position_state: str,
    plan_date: str,
    trade_date: str,
    source_data_cutoff: str,
    quantity: int,
    position: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    daily_df: pd.DataFrame,
    feature: dict[str, Any],
    price_data_evidence: dict[str, Any],
) -> dict[str, Any]:
    reference_close = None
    if not daily_df.empty:
        reference_close = safe_float(daily_df.iloc[-1].get("close"), None)
    reference_close = reference_close or _candidate_price(candidate or {}) or safe_float((position or {}).get("latest_price"), None) or safe_float((position or {}).get("entry_price"), None) or 0.0
    entry_price = safe_float((position or {}).get("avg_cost"), None) or safe_float((position or {}).get("entry_price"), None)
    context = _price_context(
        symbol=symbol,
        reference_close=reference_close,
        entry_price=entry_price if position else reference_close,
        daily_df=daily_df,
        feature=feature,
        # 棘轮窗口起点。无 entry_date（例如新候选）时窗口退化为整段日线，
        # 但新候选没有 position、走的是 reference_close 分支，棘轮不参与。
        entry_date=str((position or {}).get("entry_date") or "")[:10] or None,
    )
    current_quantity = safe_int((position or {}).get("quantity"), 0) or 0
    sellable_quantity = safe_int((position or {}).get("sellable_quantity"), None)
    if sellable_quantity is None and current_quantity:
        sellable_quantity = current_quantity
    frozen_quantity = max(0, current_quantity - (sellable_quantity or 0))
    holding = bool(position)
    still_candidate = bool(candidate)
    board_lot = lot_size_for_symbol(symbol) or 100
    entry_below_board_lot = (not holding) and quantity < board_lot
    risk_guards = _risk_guards(feature, context, holding=holding)
    formal_execution_eligible = bool(
        price_data_evidence.get("formal_execution_eligible")
    )
    if not formal_execution_eligible:
        risk_guards = [
            *risk_guards,
            {
                "guard": "formal_daily_price_contract",
                "effect": "block_all",
                "reason": (
                    "Daily price evidence is not raw/current or lacks verified "
                    "suspension evidence; this row is observation-only."
                ),
            },
        ]
    if entry_below_board_lot:
        risk_guards = [
            *risk_guards,
            {
                "guard": "below_board_lot",
                "effect": "block_new_buy",
                "reason": (
                    f"计划买入数量 {quantity} 低于 {symbol} 的最小交易单位（整手 {board_lot} 股），"
                    "不生成买入规则，避免以不足整手的数量假装成交。"
                ),
            },
        ]
    entry_rules = (
        []
        if holding or entry_below_board_lot or not formal_execution_eligible
        else _entry_rules(context, quantity)
    )
    item = {
        "symbol": symbol,
        "name": name or symbol,
        "strategy_type": normalize_strategy_type(strategy_type, "long"),
        "strategy_label": strategy_label(strategy_type),
        "position_state": position_state,
        "plan_date": plan_date,
        "trade_date": trade_date,
        "source_data_cutoff": source_data_cutoff,
        "formal_execution_eligible": formal_execution_eligible,
        "price_data_evidence": dict(price_data_evidence),
        "current_quantity": current_quantity,
        "sellable_quantity": sellable_quantity or 0,
        "frozen_quantity": frozen_quantity,
        "lot_size": board_lot,
        "entry_price": round(float(entry_price), 4) if entry_price else None,
        "reference_close": round(float(reference_close), 4) if reference_close else None,
        **{key: value for key, value in context.items() if key not in {"warnings"}},
        "still_candidate": still_candidate,
        "management_state": (position or {}).get("management_state") or ("watch_exit" if holding and not still_candidate else "normal"),
        "rules": {
            "risk_guards": risk_guards,
            "entry_rules": entry_rules,
            "add_rules": _add_rules(context, quantity) if holding and still_candidate and formal_execution_eligible else [],
            "exit_rules": _exit_rules(context) if holding and formal_execution_eligible else [],
        },
        "warnings": context.get("warnings", []),
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }
    # A formal candidate is a closed evidence package.  Preserve it byte-for-
    # byte in the frozen plan so later minute replay cannot substitute trigger
    # prices or a mutable candidate source for the daily qualification record.
    candidate_envelope = candidate.get("candidate_envelope") if isinstance(candidate, dict) else None
    if isinstance(candidate_envelope, dict):
        item["candidate_envelope"] = copy.deepcopy(candidate_envelope)
    if holding and not still_candidate:
        item["watch_exit_reason"] = "不再进入今日候选，但尚未触发卖出条件，进入退出观察。"
    if not holding:
        item["planned_results"] = ["buy", "block", "no_fill"]
    else:
        item["planned_results"] = ["add", "breakout_add", "reduce_half", "take_profit", "sell_all", "stop_loss", "watch_exit", "hold", "block"]
    return item


def build_conditional_trade_plan(
    *,
    auto_strategy_payload: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    strategy: str = "all",
    quantity_per_stock: int = 100,
    plan_date: str = "",
    trade_date: str = "",
    source_data_cutoff: str = "",
    lookback_days: int = 250,
    write: bool = True,
    output_path: Path = CONDITIONAL_TRADE_PLANS_LATEST_JSON,
    auto_strategy_path: Path = AUTO_STRATEGY_LATEST_JSON,
    state_path: Path = AUTO_PAPER_PORTFOLIO_STATE_JSON,
    daily_dir: Path = DAILY_DIR,
    daily_cache: dict[str, pd.DataFrame] | None = None,
    daily_feature_path: Path = DAILY_FEATURE_LATEST_CSV,
    low_price_universe_path: Path = LOW_PRICE_UNIVERSE_CSV,
    session_evidence_metadata_dir: Path | None = None,
) -> dict[str, Any]:
    auto_payload = auto_strategy_payload if isinstance(auto_strategy_payload, dict) else read_json_safe(auto_strategy_path, {})
    if not isinstance(auto_payload, dict):
        auto_payload = {}
    state_recovered_from_backup = False
    state_recovery_source = ""
    state_recovery_warning = ""
    if isinstance(state, dict):
        portfolio_state = state
    else:
        # Read through PaperStateStore so a corrupt state JSON recovers from the
        # newest valid backup instead of silently becoming an empty portfolio
        # (which would drop real paper positions with no visible warning).
        store = PaperStateStore(state_path, schema_version=API_SCHEMA_VERSION, max_backups=7)
        portfolio_state = store.read(default={})
        if not isinstance(portfolio_state, dict):
            portfolio_state = {}
        state_recovered_from_backup = bool(portfolio_state.get("state_recovered_from_backup"))
        state_recovery_source = str(portfolio_state.get("state_recovery_source") or "")
        state_recovery_warning = str(portfolio_state.get("state_recovery_warning") or "")
    if not isinstance(portfolio_state, dict):
        portfolio_state = {}
    feature_map_initial = _read_daily_feature_map(daily_feature_path, cutoff="")
    plan_date_value = _infer_plan_date(auto_payload, feature_map_initial, portfolio_state, plan_date)
    cutoff = (source_data_cutoff or plan_date_value)[:10]
    feature_map = _read_daily_feature_map(daily_feature_path, cutoff=cutoff)
    trade_date_value = _infer_trade_date(plan_date_value, trade_date)
    plan_id = f"plan_{plan_date_value.replace('-', '')}_for_{trade_date_value.replace('-', '')}"
    quantity = round_down_to_lot(quantity_per_stock)
    allowed_symbols = _read_low_price_allowed_symbols(low_price_universe_path)
    candidates = []
    for row in candidate_rows_from_strategy_payload(auto_payload, strategy=strategy):
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        if allowed_symbols is not None and symbol not in allowed_symbols:
            continue
        candidates.append({**row, "symbol": symbol})
    positions = [row for row in portfolio_state.get("positions") or [] if isinstance(row, dict)]
    candidate_by_key = {_position_key(row): row for row in candidates}
    position_by_key = {_position_key(row): row for row in positions}
    planned_symbols = sorted(
        {symbol for symbol, _ in set(candidate_by_key) | set(position_by_key)}
    )
    plan_daily_cache = {
        symbol: _read_daily_frame(
            symbol,
            daily_dir=daily_dir,
            daily_cache=daily_cache,
        )
        for symbol in planned_symbols
    }
    price_policy_validation = validate_price_frame_set(
        plan_daily_cache,
        consumer="paper_execution",
    )
    suspended_by_symbol, session_evidence_by_symbol, session_warnings = (
        load_verified_no_session_dates(
            planned_symbols,
            metadata_dir=session_evidence_metadata_dir,
        )
    )
    price_evidence_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in planned_symbols:
        daily_df = _daily_until_cutoff(
            symbol,
            cutoff,
            daily_dir=daily_dir,
            lookback_days=lookback_days,
            daily_cache=plan_daily_cache,
        )
        price_evidence_by_symbol[symbol] = _price_data_evidence(
            symbol=symbol,
            target_date=cutoff,
            daily_df=daily_df,
            price_policy_row=dict(
                (price_policy_validation.get("per_symbol") or {}).get(symbol) or {}
            ),
            suspended_dates=set(suspended_by_symbol.get(symbol) or set()),
            session_evidence=dict(session_evidence_by_symbol.get(symbol) or {}),
        )
    symbols: list[dict[str, Any]] = []
    warnings: list[str] = list(session_warnings)
    if state_recovered_from_backup:
        warnings.append(
            "state_recovered_from_backup: 当前纸面持仓 state JSON 已损坏，已从最近的有效备份"
            f"{('（' + state_recovery_source + '）') if state_recovery_source else ''}恢复；"
            "请核对持仓后再执行条件计划。"
        )
    elif state_recovery_warning:
        warnings.append(f"state_recovery_warning: {state_recovery_warning}")
    for key, position in sorted(position_by_key.items()):
        symbol, strategy_type = key
        candidate = candidate_by_key.get(key)
        daily_df = _daily_until_cutoff(symbol, cutoff, daily_dir=daily_dir, lookback_days=lookback_days, daily_cache=plan_daily_cache)
        feature = feature_map.get(symbol, {})
        item = _build_item(
            symbol=symbol,
            name=str(position.get("name") or (candidate or {}).get("name") or symbol),
            strategy_type=str(position.get("strategy_type") or strategy_type),
            position_state="holding" if candidate else "watch_exit",
            plan_date=plan_date_value,
            trade_date=trade_date_value,
            source_data_cutoff=cutoff,
            quantity=quantity,
            position=position,
            candidate=candidate,
            daily_df=daily_df,
            feature=feature,
            price_data_evidence=price_evidence_by_symbol[symbol],
        )
        symbols.append(item)
        warnings.extend(f"{symbol}: {warning}" for warning in item.get("warnings") or [])
    for key, candidate in sorted(candidate_by_key.items()):
        if key in position_by_key:
            continue
        symbol, strategy_type = key
        daily_df = _daily_until_cutoff(symbol, cutoff, daily_dir=daily_dir, lookback_days=lookback_days, daily_cache=plan_daily_cache)
        feature = feature_map.get(symbol, {})
        item = _build_item(
            symbol=symbol,
            name=_candidate_name(candidate),
            strategy_type=str(candidate.get("strategy_type") or strategy_type),
            position_state="new_candidate",
            plan_date=plan_date_value,
            trade_date=trade_date_value,
            source_data_cutoff=cutoff,
            quantity=quantity,
            position=None,
            candidate=candidate,
            daily_df=daily_df,
            feature=feature,
            price_data_evidence=price_evidence_by_symbol[symbol],
        )
        symbols.append(item)
        warnings.extend(f"{symbol}: {warning}" for warning in item.get("warnings") or [])
    blocked_price_symbols = sorted(
        symbol
        for symbol, evidence in price_evidence_by_symbol.items()
        if evidence.get("formal_execution_eligible") is not True
    )
    price_reason_codes = list(
        dict.fromkeys(
            reason
            for evidence in price_evidence_by_symbol.values()
            for reason in list(evidence.get("reason_codes") or [])
        )
    )
    price_data_validation = {
        "status": "passed" if not blocked_price_symbols else "blocked",
        "target_date": cutoff,
        "formal_execution_eligible": not blocked_price_symbols,
        "blocked_symbols": blocked_price_symbols,
        "reason_codes": price_reason_codes,
        "price_policy_validation": price_policy_validation,
        "items": [price_evidence_by_symbol[symbol] for symbol in planned_symbols],
    }
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "plan_id": plan_id,
        "plan_date": plan_date_value,
        "trade_date": trade_date_value,
        "source_data_cutoff": cutoff,
        "scope": SCOPE,
        "network": False,
        "is_real_order": False,
        "generated_at": now_text(),
        "state_recovered_from_backup": bool(state_recovered_from_backup),
        "formal_execution_eligible": not blocked_price_symbols,
        "price_data_validation": price_data_validation,
        "symbols": symbols,
        "summary": {
            "symbol_count": len(symbols),
            "holding_plan_count": sum(1 for row in symbols if row.get("position_state") in {"holding", "watch_exit"}),
            "new_candidate_plan_count": sum(1 for row in symbols if row.get("position_state") == "new_candidate"),
            "candidate_strategy_counts": summarize_candidates_by_strategy(candidates),
            "warning_count": len(warnings),
        },
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }
    if write:
        atomic_write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def _persisted_plan_validation(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from quantdesk.intraday.intraday_gap_checker import (
            collect_intraday_required_symbols,
        )

        audit = collect_intraday_required_symbols(
            source="plans",
            target_trade_date="",
            plan_path=path,
        )
        sources = (audit.get("source_manifest") or {}).get("sources") or []
        evidence = dict(sources[0]) if sources and isinstance(sources[0], dict) else {}
    except Exception as exc:  # noqa: BLE001 - persisted plans fail closed
        evidence = {
            "alignment": "audit_failed",
            "formal_eligible": False,
            "reason_codes": ["persisted_plan_audit_failed"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
    temporal_reasons = [str(item) for item in (evidence.get("reason_codes") or [])]
    if evidence.get("formal_eligible") is not True and not temporal_reasons:
        temporal_reasons.append("persisted_plan_temporal_evidence_invalid")

    provenance_reasons: list[str] = []
    if str(payload.get("freeze_status") or "") != "completed":
        provenance_reasons.append("data_chain_freeze_status_missing")
    if not str(payload.get("data_chain_run_id") or "").strip():
        provenance_reasons.append("data_chain_run_id_missing")
    if str(payload.get("data_chain_target_date") or "")[:10] != str(
        payload.get("plan_date") or ""
    )[:10]:
        provenance_reasons.append("data_chain_target_date_mismatch")
    try:
        contract_version = int(payload.get("data_chain_contract_version") or 0)
    except (TypeError, ValueError):
        contract_version = 0
    if contract_version < 9:
        provenance_reasons.append("data_chain_contract_version_missing")
    if len(str(payload.get("data_chain_universe_sha256") or "")) != 64:
        provenance_reasons.append("data_chain_universe_sha256_missing")
    if len(str(payload.get("data_chain_feature_sha256") or "")) != 64:
        provenance_reasons.append("data_chain_feature_sha256_missing")

    formal_binding_fields = (
        "formal_candidate_batch_path",
        "formal_candidate_batch_provenance_hash",
        "formal_candidate_batch_source_mode",
    )
    if any(field in payload for field in formal_binding_fields):
        from quantdesk.daily_candidate_refresh import verify_candidate_envelope

        project_root = (
            path.parent.parent if path.parent.name == "results" else PROJECT_ROOT
        )

        def path_key(value: Any) -> str:
            text = str(value or "").strip()
            if not text:
                return ""
            try:
                candidate_path = Path(text)
                if not candidate_path.is_absolute():
                    candidate_path = project_root / candidate_path
                return str(candidate_path.resolve()).lower()
            except (OSError, RuntimeError, ValueError):
                return ""

        plan_date = str(payload.get("plan_date") or "")[:10]
        run_id = str(payload.get("data_chain_run_id") or "").strip()
        batch_path = str(payload.get("formal_candidate_batch_path") or "")
        batch_path_key = path_key(batch_path)
        batch_hash = str(
            payload.get("formal_candidate_batch_provenance_hash") or ""
        ).strip().lower()
        source_mode = str(
            payload.get("formal_candidate_batch_source_mode") or ""
        ).strip()
        dated_path_key = path_key(
            project_root
            / "results"
            / "candidate_refresh"
            / "daily"
            / f"{plan_date}.json"
        )
        run_path_key = path_key(
            project_root
            / "results"
            / "candidate_refresh"
            / "runs"
            / run_id
            / "formal_candidate_batch.json"
        )
        if not source_mode:
            if batch_path_key == dated_path_key:
                source_mode = "dated_snapshot"
            elif batch_path_key == run_path_key:
                source_mode = "run_scoped_conflict_recovery"
        if source_mode == "dated_snapshot":
            if batch_path_key != dated_path_key:
                provenance_reasons.append("formal_candidate_batch_path_mismatch")
        elif source_mode == "run_scoped_conflict_recovery":
            if batch_path_key != run_path_key:
                provenance_reasons.append("formal_candidate_batch_path_mismatch")
        else:
            provenance_reasons.append("formal_candidate_batch_source_mode_invalid")
        if len(batch_hash) != 64 or any(
            character not in "0123456789abcdef" for character in batch_hash
        ):
            provenance_reasons.append("formal_candidate_batch_hash_invalid")

        for plan_item in list(payload.get("symbols") or []):
            if not isinstance(plan_item, dict):
                continue
            if (
                str(plan_item.get("position_state") or "") != "new_candidate"
                and "candidate_envelope" not in plan_item
            ):
                continue
            symbol = normalize_symbol(plan_item.get("symbol")) or "unknown"
            envelope = plan_item.get("candidate_envelope")
            envelope_reasons = verify_candidate_envelope(
                envelope if isinstance(envelope, dict) else None
            )
            provenance_reasons.extend(
                f"formal_candidate_envelope_invalid:{symbol}:{reason}"
                for reason in envelope_reasons
            )
            envelope = envelope if isinstance(envelope, dict) else {}
            if path_key(envelope.get("batch_path")) != batch_path_key:
                provenance_reasons.append(
                    f"formal_candidate_envelope_path_mismatch:{symbol}"
                )
            if (
                str(envelope.get("batch_provenance_hash") or "").strip().lower()
                != batch_hash
            ):
                provenance_reasons.append(
                    f"formal_candidate_envelope_hash_mismatch:{symbol}"
                )
            envelope_run_id = str(envelope.get("source_run_id") or "").strip()
            if source_mode == "run_scoped_conflict_recovery":
                if envelope_run_id != run_id:
                    provenance_reasons.append(
                        f"formal_candidate_envelope_run_mismatch:{symbol}"
                    )
            elif envelope_run_id and envelope_run_id != run_id:
                provenance_reasons.append(
                    f"formal_candidate_envelope_run_mismatch:{symbol}"
                )
            candidate = envelope.get("candidate")
            candidate = candidate if isinstance(candidate, dict) else {}
            if normalize_symbol(candidate.get("symbol")) != symbol:
                provenance_reasons.append(
                    f"formal_candidate_envelope_symbol_mismatch:{symbol}"
                )
            if str(candidate.get("strategy_type") or "").strip() != str(
                plan_item.get("strategy_type") or ""
            ).strip():
                provenance_reasons.append(
                    f"formal_candidate_envelope_strategy_mismatch:{symbol}"
                )
        provenance_reasons = list(dict.fromkeys(provenance_reasons))

    price_reasons: list[str] = []
    price_validation = payload.get("price_data_validation")
    price_validation = (
        price_validation if isinstance(price_validation, dict) else {}
    )
    if (
        payload.get("formal_execution_eligible") is not True
        or str(price_validation.get("status") or "") != "passed"
        or price_validation.get("formal_execution_eligible") is not True
        or list(price_validation.get("blocked_symbols") or [])
    ):
        price_reasons.append("conditional_plan_price_data_not_formal")
    if str(price_validation.get("target_date") or "")[:10] != str(
        payload.get("plan_date") or ""
    )[:10]:
        price_reasons.append("conditional_plan_price_target_date_mismatch")
    price_items = price_validation.get("items")
    if not isinstance(price_items, list):
        price_reasons.append("conditional_plan_price_items_missing")
        price_items = []
    item_by_symbol = {
        normalize_symbol(item.get("symbol")): item
        for item in price_items
        if isinstance(item, dict) and normalize_symbol(item.get("symbol"))
    }
    for plan_item in list(payload.get("symbols") or []):
        if not isinstance(plan_item, dict):
            price_reasons.append("conditional_plan_symbol_item_invalid")
            continue
        symbol = normalize_symbol(plan_item.get("symbol"))
        evidence = item_by_symbol.get(symbol, {})
        if (
            not symbol
            or evidence.get("formal_execution_eligible") is not True
            or str(evidence.get("price_basis") or "") != PRICE_BASIS_RAW
        ):
            price_reasons.append(
                f"conditional_plan_symbol_price_not_formal:{symbol or 'unknown'}"
            )
    price_reasons = list(dict.fromkeys(price_reasons))

    terminal_reasons: list[str] = []
    run_id = str(payload.get("data_chain_run_id") or "").strip()
    plan_target = str(payload.get("data_chain_target_date") or "")[:10]
    universe_sha256 = str(payload.get("data_chain_universe_sha256") or "")
    feature_sha256 = str(payload.get("data_chain_feature_sha256") or "")
    project_root = path.parent.parent if path.parent.name == "results" else PROJECT_ROOT
    run_manifest_path = (
        project_root
        / "results"
        / "data_chain"
        / "runs"
        / run_id
        / "manifest.json"
    )
    latest_manifest_path = (
        project_root
        / "results"
        / "data_chain"
        / "daily_data_chain_latest.json"
    )
    run_manifest = read_json_safe(run_manifest_path, {}) if run_id else {}
    latest_manifest = read_json_safe(latest_manifest_path, {}) if run_id else {}

    def check_manifest(
        manifest: Any,
        *,
        missing_reason: str,
        prefix: str,
    ) -> None:
        if not isinstance(manifest, dict) or not manifest:
            terminal_reasons.append(missing_reason)
            return
        if str(manifest.get("status") or "") != "completed":
            terminal_reasons.append(f"{prefix}_not_completed")
        if manifest.get("authoritative") is not True:
            terminal_reasons.append(f"{prefix}_not_authoritative")
        if manifest.get("published_latest") is not True:
            terminal_reasons.append(f"{prefix}_not_published")
        if str(manifest.get("run_id") or "") != run_id:
            terminal_reasons.append(f"{prefix}_run_id_mismatch")
        if str(manifest.get("target_date") or "")[:10] != plan_target:
            terminal_reasons.append(f"{prefix}_target_date_mismatch")
        try:
            manifest_contract = int(
                manifest.get("daily_data_chain_contract_version") or 0
            )
        except (TypeError, ValueError):
            manifest_contract = 0
        if manifest_contract != contract_version:
            terminal_reasons.append(f"{prefix}_contract_version_mismatch")
        manifest_universe = manifest.get("universe")
        manifest_universe = (
            manifest_universe if isinstance(manifest_universe, dict) else {}
        )
        analysis_pool = manifest_universe.get("current_analysis_pool")
        analysis_pool = analysis_pool if isinstance(analysis_pool, dict) else {}
        if str(analysis_pool.get("sha256") or "") != universe_sha256:
            terminal_reasons.append(f"{prefix}_universe_sha256_mismatch")
        components = manifest.get("components")
        components = components if isinstance(components, dict) else {}
        features = components.get("features")
        features = features if isinstance(features, dict) else {}
        feature_gate = (manifest.get("coverage") or {}).get("features")
        feature_gate = feature_gate if isinstance(feature_gate, dict) else {}
        manifest_feature_sha = str(
            features.get("output_sha256")
            or feature_gate.get("output_sha256")
            or ""
        )
        if manifest_feature_sha != feature_sha256:
            terminal_reasons.append(f"{prefix}_feature_sha256_mismatch")
        freeze = components.get("conditional_plan_freeze")
        freeze = freeze if isinstance(freeze, dict) else {}
        if (
            str(freeze.get("status") or "") != "completed"
            or freeze.get("written") is not True
            or str(freeze.get("plan_id") or "")
            != str(payload.get("plan_id") or "")
        ):
            terminal_reasons.append(f"{prefix}_plan_freeze_mismatch")

    check_manifest(
        run_manifest,
        missing_reason="data_chain_terminal_manifest_missing",
        prefix="data_chain_terminal_manifest",
    )
    check_manifest(
        latest_manifest,
        missing_reason="data_chain_authoritative_latest_missing",
        prefix="data_chain_authoritative_latest",
    )

    temporal_eligible = not temporal_reasons
    formal_eligible = (
        temporal_eligible
        and not provenance_reasons
        and not price_reasons
        and not terminal_reasons
    )
    return {
        "status": "passed" if formal_eligible else "blocked",
        "temporal_eligible": temporal_eligible,
        "research_replay_eligible": temporal_eligible,
        "formal_data_chain_eligible": formal_eligible,
        "formal_execution_eligible": formal_eligible,
        "alignment": str(evidence.get("alignment") or "unknown"),
        "reason_codes": temporal_reasons + provenance_reasons + price_reasons + terminal_reasons,
        "temporal_reason_codes": temporal_reasons,
        "provenance_reason_codes": provenance_reasons,
        "price_reason_codes": price_reasons,
        "terminal_manifest_reason_codes": terminal_reasons,
        "terminal_manifest_status": (
            "matched_completed" if not terminal_reasons else "blocked"
        ),
        "run_manifest_path": _display_path(run_manifest_path),
        "authoritative_latest_path": _display_path(latest_manifest_path),
        "source_evidence": evidence,
    }


def conditional_trade_plan_latest(path: Path = CONDITIONAL_TRADE_PLANS_LATEST_JSON) -> dict[str, Any]:
    data = read_json_safe(path, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未生成条件交易计划。",
            "disclaimer": DISCLAIMER,
        }
    data["exists"] = True
    data.setdefault("path", _display_path(path))
    data.setdefault("disclaimer", DISCLAIMER)
    validation = _persisted_plan_validation(path, data)
    data["plan_validation"] = validation
    data["formal_execution_eligible"] = bool(
        validation.get("formal_execution_eligible")
    )
    data["research_replay_eligible"] = bool(
        validation.get("research_replay_eligible")
    )
    return data


def conditional_trade_plan_status(path: Path = CONDITIONAL_TRADE_PLANS_LATEST_JSON) -> dict[str, Any]:
    latest = conditional_trade_plan_latest(path)
    if not latest.get("exists"):
        return latest
    symbols = latest.get("symbols") if isinstance(latest.get("symbols"), list) else []
    stat = path.stat() if path.exists() else None
    return {
        "ok": True,
        "exists": True,
        "path": _display_path(path),
        "plan_id": latest.get("plan_id"),
        "plan_date": latest.get("plan_date"),
        "trade_date": latest.get("trade_date"),
        "source_data_cutoff": latest.get("source_data_cutoff"),
        "total": len(symbols),
        "holding_count": sum(1 for row in symbols if row.get("position_state") in {"holding", "watch_exit"}),
        "new_candidate_count": sum(1 for row in symbols if row.get("position_state") == "new_candidate"),
        "formal_execution_eligible": bool(latest.get("formal_execution_eligible")),
        "research_replay_eligible": bool(latest.get("research_replay_eligible")),
        "plan_validation": dict(latest.get("plan_validation") or {}),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds") if stat else latest.get("generated_at"),
        "network": False,
        "is_real_order": False,
        "disclaimer": DISCLAIMER,
    }


def conditional_trade_plan_preview(limit: int = 20, path: Path = CONDITIONAL_TRADE_PLANS_LATEST_JSON) -> dict[str, Any]:
    latest = conditional_trade_plan_latest(path)
    if not latest.get("exists"):
        return latest
    rows = latest.get("symbols") if isinstance(latest.get("symbols"), list) else []
    return {
        "ok": True,
        "exists": True,
        "path": _display_path(path),
        "plan_id": latest.get("plan_id"),
        "items": rows[: max(0, int(limit))],
        "total": len(rows),
        "disclaimer": DISCLAIMER,
    }
