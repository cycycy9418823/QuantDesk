"""1-minute intraday provider fallback adapters.

These providers are only used when the user explicitly triggers an update from
CLI or POST API. They do not connect to brokers, do not place orders, and never
generate synthetic fills when market data is unavailable.
"""

from __future__ import annotations

# Compatibility markers: local_existing akshare_intraday tencent_intraday sina_intraday suggested_next_action skipped_reason mootdx_provider.
import importlib
import importlib.util
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from quantdesk.intraday.intraday_schema import (
    build_intraday_quality_gate,
    filter_intraday_trading_session,
    normalize_intraday_frame,
    read_intraday_csv,
    source_time_order_state,
)
from quantdesk.market_clock import CN_TIMEZONE
from quantdesk.shared import normalize_symbol, short_error_text
from quantdesk.version import PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTRADAY_1M_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"

DEFAULT_INTRADAY_PROVIDER_ORDER = [
    "local_existing",
    "mootdx_intraday",
    "tencent_intraday",
    "akshare_intraday",
    "sina_intraday",
]


def akshare_sina_symbol(symbol: Any) -> dict[str, Any]:
    """Convert an A-share code to the Sina-interface prefixed form.

    600000/688001 -> sh600000/sh688001, 000001/300001 -> sz000001/sz300001.
    Beijing Stock Exchange (4xx/8xx/9xx) cannot be reliably served by the Sina
    minute interface, so it is returned as ``unsupported`` rather than guessed.
    """

    normalized = normalize_symbol(symbol)
    if not normalized:
        return {"symbol": "", "supported": False, "market": "", "reason": "无法识别股票代码，未猜测前缀。"}
    if normalized.startswith(("600", "601", "603", "605", "688")):
        return {"symbol": f"sh{normalized}", "supported": True, "market": "SH", "reason": ""}
    if normalized.startswith(("000", "001", "002", "003", "300", "301")):
        return {"symbol": f"sz{normalized}", "supported": True, "market": "SZ", "reason": ""}
    if normalized.startswith(("4", "8", "9")):
        return {
            "symbol": "",
            "supported": False,
            "market": "BJ",
            "reason": "北交所代码无法可靠映射到新浪分钟接口，标记为 unsupported，不猜测前缀。",
        }
    return {"symbol": "", "supported": False, "market": "", "reason": "unsupported: 无法可靠判断交易所前缀，未猜测。"}


@dataclass(frozen=True)
class IntradayProviderSpec:
    name: str
    enabled: bool = True
    network_required: bool = True
    dependencies: tuple[str, ...] = ()
    description: str = ""
    supports_current_day: bool = True
    supports_historical_day: bool = False
    supported_periods: tuple[str, ...] = ("1m",)
    max_history_days: int = 0
    requires_sdk: bool = False


PROVIDER_SPECS: dict[str, IntradayProviderSpec] = {
    "local_existing": IntradayProviderSpec(
        name="local_existing",
        network_required=False,
        description="Read existing data/intraday/1m/{symbol}.csv.",
        supports_historical_day=True,
    ),
    "akshare_intraday": IntradayProviderSpec(
        name="akshare_intraday",
        network_required=True,
        dependencies=("akshare",),
        description="Optional AKShare A-share 1-minute data adapter.",
        supports_historical_day=True,
        max_history_days=30,
        requires_sdk=True,
    ),
    "mootdx_intraday": IntradayProviderSpec(
        name="mootdx_intraday",
        network_required=True,
        dependencies=("mootdx",),
        description="Optional mootdx quote/minute adapter.",
        supports_historical_day=True,
        max_history_days=30,
        requires_sdk=True,
    ),
    "ashare_intraday": IntradayProviderSpec(
        name="ashare_intraday",
        network_required=True,
        dependencies=("Ashare",),
        description="Optional Ashare get_price 1-minute adapter.",
        supports_historical_day=True,
        max_history_days=30,
        requires_sdk=True,
    ),
    "tencent_intraday": IntradayProviderSpec(
        name="tencent_intraday",
        network_required=True,
        description="Tencent HTTP minute-data fallback for current-day scope.",
        supports_current_day=True,
        supports_historical_day=False,
    ),
    "sina_intraday": IntradayProviderSpec(
        name="sina_intraday",
        network_required=True,
        description="Sina HTTP minute-data fallback for current-day scope.",
    ),
}


def _dependency_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def provider_registry_status(*, check_network: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in DEFAULT_INTRADAY_PROVIDER_ORDER:
        spec = PROVIDER_SPECS[name]
        missing = [dep for dep in spec.dependencies if not _dependency_available(dep)]
        status = "available"
        failed_reason = ""
        if missing:
            status = "not_available"
            failed_reason = "未安装依赖：" + ", ".join(missing)
        elif spec.network_required and not check_network:
            status = "unknown"
        rows.append({
            "name": name,
            "type": "intraday_1m_provider",
            "enabled": spec.enabled,
            "network_required": spec.network_required,
            "dependencies": list(spec.dependencies),
            "status": status,
            "failed_reason": failed_reason,
            "description": spec.description,
            "supports_current_day": spec.supports_current_day,
            "supports_historical_day": spec.supports_historical_day,
            "supported_periods": list(spec.supported_periods),
            "max_history_days": spec.max_history_days,
            "requires_sdk": spec.requires_sdk,
        })
    return rows


def _empty_result(provider: str, symbol: str, target_date: str, status: str, reason: str = "") -> dict[str, Any]:
    spec = PROVIDER_SPECS.get(provider)
    supports_current = bool(getattr(spec, "supports_current_day", False)) if spec else False
    supports_historical = bool(getattr(spec, "supports_historical_day", False)) if spec else False
    date_scope = "historical_supported" if supports_historical else ("current_day_only" if supports_current else "unknown")
    suggested = "请导入目标日期的 1 分钟线 CSV，然后运行导入并执行条件计划。"
    if provider == "local_existing":
        suggested = "请尝试 provider 或导入 CSV。"
    elif provider == "akshare_intraday":
        suggested = "检查网络，或改用 CSV 导入。"
    elif provider == "ashare_intraday":
        suggested = "安装依赖，或改用 CSV 导入。"
    elif provider in {"tencent_intraday", "sina_intraday"}:
        suggested = "请导入历史分钟线 CSV，或在目标交易日盘后尝试当日 provider。"
    return {
        "provider": provider,
        "status": status,
        "symbol": normalize_symbol(symbol),
        "target_date": target_date[:10],
        "supports_current_day": supports_current,
        "supports_historical_day": supports_historical,
        "supports_target_date": False,
        "date_scope": date_scope,
        "requires_network": bool(getattr(spec, "network_required", True)) if spec else True,
        "requires_dependency": bool(getattr(spec, "dependencies", ())) if spec else False,
        "dependency_available": status != "not_available",
        "rows": 0,
        "latest_datetime": "",
        "data": pd.DataFrame(),
        "failed_reason": short_error_text(reason),
        "skipped_reason": short_error_text(reason) if status in {"skipped", "not_available"} else "",
        "suggested_next_action": suggested,
        "warnings": [],
    }


def _source_time_order_valid(frame: pd.DataFrame | None) -> bool | None:
    """Tri-state source time-order check on the *pre-normalization* frame.

    Returns True (forward order), False (reverse order), or None (unknown -
    e.g. no recognizable time column or unparseable timestamps). It reuses the
    same column aliases as normalize_intraday_frame (datetime/day/时间/date+time/
    eob/bar_time/…) so a provider that streams rows in reverse under any of those
    header names is still caught, even though normalization later sorts the frame.
    None must be treated as fail-close: an unverifiable order is not signal-eligible.
    """

    return source_time_order_state(frame)


def _attach_quality_gate(
    result: dict[str, Any],
    *,
    provider: str,
    target_date: str,
    source: str,
    price_only: bool,
    session_filtered_count: int,
    source_time_order_valid: bool | None = None,
) -> dict[str, Any]:
    """Attach the uniform intraday quality-gate fields to a provider result.

    Runs on both ok and failed results so the provider trace always exposes the
    quality contract (ohlcv_complete / signal_capability / signal_eligible /
    volume_semantics / session_filtered_count) and downstream code can decide
    whether the minute data is signal-eligible or only a price trend.
    """

    frame = result.get("data") if result.get("status") == "ok" else None
    gate = build_intraday_quality_gate(
        frame if isinstance(frame, pd.DataFrame) else None,
        provider=provider,
        target_date=target_date,
        price_only=price_only,
        source=source,
        session_filtered_count=session_filtered_count,
        source_time_order_valid=source_time_order_valid,
        extra_warnings=list(result.get("warnings") or []),
    )
    result["ohlcv_complete"] = gate["ohlcv_complete"]
    result["price_only_intraday"] = gate["price_only_intraday"]
    result["volume_semantics"] = gate["volume_semantics"]
    result["signal_capability"] = gate["signal_capability"]
    result["signal_eligible"] = gate["signal_eligible"]
    result["ohlcv_signal_eligible"] = gate["ohlcv_signal_eligible"]
    result["data_quality"] = gate["data_quality"]
    result["source_time_order_valid"] = gate["source_time_order_valid"]
    result["session_filtered_count"] = int(session_filtered_count)
    basis_tokens = [
        str(item).strip().lower()
        for item in (
            frame.get("price_basis", pd.Series(dtype=str)).tolist()
            if isinstance(frame, pd.DataFrame)
            else []
        )
        if str(item).strip()
    ]
    known_basis_values = sorted(
        {item for item in basis_tokens if item in {"raw", "qfq", "hfq"}}
    )
    has_unknown_basis = any(
        item not in {"raw", "qfq", "hfq"} for item in basis_tokens
    )
    result["price_basis"] = (
        known_basis_values[0]
        if len(known_basis_values) == 1 and not has_unknown_basis
        else "mixed_or_unknown"
        if basis_tokens
        else str(result.get("price_basis") or "unknown")
    )
    gate["price_basis"] = result["price_basis"]
    result["quality_gate"] = gate
    result["warnings"] = list(gate.get("warnings") or result.get("warnings") or [])
    if not (gate.get("ohlcv_complete") and gate.get("signal_eligible")):
        result["suggested_next_action"] = (
            "not formal：分钟数据未通过质量门；请补抓完整 raw OHLCV 或改用其他 provider。"
        )
    return result


def _normalize_provider_frame(
    provider: str,
    symbol: str,
    target_date: str,
    frame: pd.DataFrame | None,
    *,
    source: str,
    filter_session: bool = True,
    price_basis: str = "unknown",
    price_only_intraday: bool | None = None,
) -> dict[str, Any]:
    # Capture the ORIGINAL provider order before normalize_intraday_frame sorts
    # by datetime, so a reverse-order source is still caught downstream.
    order_valid = _source_time_order_valid(frame)
    payload = normalize_intraday_frame(
        frame,
        default_symbol=symbol,
        source=source,
        target_date=target_date,
        price_basis=price_basis,
    )
    out = payload.get("frame")
    normalized_price_only = bool(payload.get("price_only_intraday"))
    # Provenance is monotonic: an upstream True can add evidence, but an
    # explicit False must never erase synthesis detected while normalizing a
    # price/close-only provider frame.
    price_only = normalized_price_only or bool(price_only_intraday)
    if not isinstance(out, pd.DataFrame) or out.empty:
        reason = "; ".join(payload.get("errors") or []) or "provider 未返回目标日期分钟线"
        result = _empty_result(provider, symbol, target_date, "failed", reason)
        result["warnings"] = list(payload.get("warnings") or [])
        return _attach_quality_gate(result, provider=provider, target_date=target_date, source=source, price_only=price_only, session_filtered_count=0, source_time_order_valid=order_valid)

    session_filtered_count = 0
    if filter_session:
        out, session_filtered_count = filter_intraday_trading_session(out, target_date=target_date)
    warnings = list(payload.get("warnings") or [])
    if session_filtered_count:
        warnings.append(f"已过滤 {session_filtered_count} 行盘外分钟记录（仅保留 09:30-11:30 / 13:00-15:00）。")
    if not isinstance(out, pd.DataFrame) or out.empty:
        result = _empty_result(provider, symbol, target_date, "failed", "provider 分钟线过滤盘外记录后没有目标日期数据")
        result["warnings"] = warnings
        return _attach_quality_gate(result, provider=provider, target_date=target_date, source=source, price_only=price_only, session_filtered_count=session_filtered_count, source_time_order_valid=order_valid)

    result = {
        "provider": provider,
        "status": "ok",
        "symbol": normalize_symbol(symbol),
        "target_date": target_date[:10],
        "supports_current_day": getattr(PROVIDER_SPECS.get(provider), "supports_current_day", True),
        "supports_historical_day": getattr(PROVIDER_SPECS.get(provider), "supports_historical_day", False),
        "supports_target_date": True,
        "date_scope": "historical_supported" if getattr(PROVIDER_SPECS.get(provider), "supports_historical_day", False) else "current_day_only",
        "requires_network": bool(getattr(PROVIDER_SPECS.get(provider), "network_required", True)),
        "requires_dependency": bool(getattr(PROVIDER_SPECS.get(provider), "dependencies", ())),
        "dependency_available": True,
        "rows": int(len(out)),
        "latest_datetime": str(out["datetime"].iloc[-1]) if "datetime" in out.columns else "",
        "data": out,
        "failed_reason": "",
        "skipped_reason": "",
        "suggested_next_action": "已获取目标日期分钟线，仍须通过 raw 口径及严格完整会话审计。",
        "warnings": warnings,
        "price_only_intraday": price_only,
        "price_basis": str(payload.get("price_basis") or "unknown"),
    }
    return _attach_quality_gate(result, provider=provider, target_date=target_date, source=source, price_only=price_only, session_filtered_count=session_filtered_count, source_time_order_valid=order_valid)


def _fetch_local_existing(symbol: str, target_date: str, *, intraday_dir: Path = INTRADAY_1M_DIR) -> dict[str, Any]:
    path = intraday_dir / f"{normalize_symbol(symbol)}.csv"
    payload = read_intraday_csv(path, default_symbol=symbol, target_date=target_date)
    frame = payload.get("frame")
    if bool(payload.get("ok")) and isinstance(frame, pd.DataFrame) and not frame.empty:
        # Local CSV is NOT a quality bypass: run the same unified quality gate as
        # the network providers. The read already normalized/sorted, so use the
        # source-order state from the raw read (payload frame is post-sort) and do
        # not re-filter the session (a curated local CSV is trusted for session).
        result = {
            "provider": "local_existing",
            "status": "ok",
            "symbol": normalize_symbol(symbol),
            "target_date": target_date[:10],
            "rows": int(len(frame)),
            "latest_datetime": str(frame["datetime"].iloc[-1]),
            "data": frame,
            "failed_reason": "",
            "skipped_reason": "",
            "supports_current_day": True,
            "supports_historical_day": True,
            "supports_target_date": True,
            "date_scope": "historical_supported",
            "requires_network": False,
            "requires_dependency": False,
            "dependency_available": True,
            "warnings": list(payload.get("warnings") or []),
            "path": str(path),
            "price_only_intraday": bool(payload.get("price_only_intraday")),
            "price_basis": str(payload.get("price_basis") or "unknown"),
        }
        result = _attach_quality_gate(
            result,
            provider="local_existing",
            target_date=target_date,
            source="local_existing",
            price_only=bool(payload.get("price_only_intraday")),
            session_filtered_count=0,
            source_time_order_valid=source_time_order_state(frame),
        )
        # Only a full, signal-eligible local frame may claim it is ready to run a
        # conditional plan; a degraded local frame gets an honest next action.
        local_basis = str(result.get("price_basis") or "unknown").strip().lower()
        if (
            result.get("ohlcv_complete")
            and result.get("signal_eligible")
            and local_basis == "raw"
        ):
            result["suggested_next_action"] = "本地已有目标日期 raw 分钟线，可执行纸面条件计划。"
        elif local_basis != "raw":
            result["suggested_next_action"] = (
                f"非正式可用（not formal）：本地分钟线 price_basis={local_basis or 'unknown'}；"
                "请改用经过验证的 raw provider。"
            )
        else:
            result["suggested_next_action"] = "本地分钟线未通过质量门（不完整/单行/price-only/目标日不足），请补全或改用其他 provider。"
        return result
    status = "skipped" if not path.exists() else "failed"
    reason = "本地分钟线文件不存在" if not path.exists() else "; ".join(payload.get("errors") or ["本地分钟线不覆盖目标日期"])
    result = _empty_result("local_existing", symbol, target_date, status, reason)
    if status == "skipped":
        result["skipped_reason"] = "本地没有目标日期分钟线"
        result["suggested_next_action"] = "请尝试 provider 或导入 CSV。"
    result["warnings"] = list(payload.get("warnings") or [])
    result["path"] = str(path)
    return result


def _akshare_frame_covers_target(frame: pd.DataFrame | None, target_date: str) -> bool:
    """True when a normalized AKShare frame actually has target-date rows.

    We normalize with the target date so out-of-scope dates are dropped; if the
    frame is then empty the source did not cover the target date and the caller
    must fall through to the next AKShare interface rather than pass off
    other-date data as the target date.
    """

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return False
    payload = normalize_intraday_frame(frame, source="akshare_intraday", target_date=target_date)
    out = payload.get("frame")
    return isinstance(out, pd.DataFrame) and not out.empty


def _fetch_akshare_intraday(symbol: str, target_date: str) -> dict[str, Any]:
    if not _dependency_available("akshare"):
        return _empty_result("akshare_intraday", symbol, target_date, "not_available", "未安装 akshare")

    code = normalize_symbol(symbol)
    reasons: list[str] = []
    try:
        ak = importlib.import_module("akshare")
    except Exception as exc:  # noqa: BLE001
        return _empty_result("akshare_intraday", symbol, target_date, "failed", f"导入 akshare 失败：{short_error_text(exc)}")

    # 1) East Money minute interface first (uses the plain 6-digit code).
    em_frame: pd.DataFrame | None = None
    if hasattr(ak, "stock_zh_a_hist_min_em"):
        try:
            raw = ak.stock_zh_a_hist_min_em(symbol=code, period="1", adjust="")
            em_frame = pd.DataFrame(raw)
            if em_frame.empty:
                reasons.append("stock_zh_a_hist_min_em 返回空数据")
            elif not _akshare_frame_covers_target(em_frame, target_date):
                reasons.append("stock_zh_a_hist_min_em 未覆盖目标日期")
                em_frame = None
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"stock_zh_a_hist_min_em 异常：{short_error_text(exc)}")
            em_frame = None
    else:
        reasons.append("akshare 无 stock_zh_a_hist_min_em 接口")

    if em_frame is not None and not em_frame.empty:
        result = _normalize_provider_frame("akshare_intraday", symbol, target_date, em_frame, source="akshare_intraday", price_basis="raw")
        result.setdefault("warnings", []).extend(reasons)
        return result

    # 2) Sina minute interface fallback (needs sh/sz prefixed symbol; BSE unsupported).
    sina_symbol = akshare_sina_symbol(code)
    if not sina_symbol.get("supported"):
        reasons.append(sina_symbol.get("reason") or "无法映射到新浪分钟接口")
        status = "skipped" if sina_symbol.get("market") == "BJ" else "failed"
        result = _empty_result("akshare_intraday", symbol, target_date, status, "; ".join(reasons))
        if status == "skipped":
            result["skipped_reason"] = "; ".join(reasons)
            result["suggested_next_action"] = "北交所暂不支持 AKShare 分钟接口，请改用本地 CSV 导入。"
        return result

    if not hasattr(ak, "stock_zh_a_minute"):
        reasons.append("akshare 无 stock_zh_a_minute 接口")
        return _empty_result("akshare_intraday", symbol, target_date, "failed", "; ".join(reasons))

    try:
        raw = ak.stock_zh_a_minute(symbol=sina_symbol["symbol"], period="1", adjust="")
        sina_frame = pd.DataFrame(raw)
        if sina_frame.empty:
            reasons.append("stock_zh_a_minute 返回空数据")
            return _empty_result("akshare_intraday", symbol, target_date, "failed", "; ".join(reasons))
        result = _normalize_provider_frame("akshare_intraday", symbol, target_date, sina_frame, source="akshare_intraday", price_basis="raw")
        result.setdefault("warnings", []).extend(reasons)
        if result.get("status") != "ok":
            result["failed_reason"] = "; ".join([r for r in [result.get("failed_reason", ""), *reasons] if r])
        return result
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"stock_zh_a_minute 异常：{short_error_text(exc)}")
        return _empty_result("akshare_intraday", symbol, target_date, "failed", "; ".join(reasons))


def _fetch_ashare_intraday(symbol: str, target_date: str) -> dict[str, Any]:
    module_name = "Ashare" if _dependency_available("Ashare") else ("ashare" if _dependency_available("ashare") else "")
    if not module_name:
        return _empty_result("ashare_intraday", symbol, target_date, "not_available", "未安装 Ashare")
    try:
        mod = importlib.import_module(module_name)
        getter = getattr(mod, "get_price", None)
        if getter is None:
            return _empty_result("ashare_intraday", symbol, target_date, "failed", "Ashare 模块没有 get_price")
        raw = getter(normalize_symbol(symbol), frequency="1m")
        return _normalize_provider_frame("ashare_intraday", symbol, target_date, pd.DataFrame(raw), source="ashare_intraday")
    except Exception as exc:  # noqa: BLE001
        return _empty_result("ashare_intraday", symbol, target_date, "failed", str(exc))


def _fetch_mootdx_intraday(symbol: str, target_date: str) -> dict[str, Any]:
    try:
        from quantdesk.providers.mootdx_provider import fetch_intraday_bars

        payload = fetch_intraday_bars(symbol, target_date, period="1m", network=True)
        if payload.get("status") in {"skipped", "not_available"}:
            result = _empty_result(
                "mootdx_intraday",
                symbol,
                target_date,
                payload.get("status") or "skipped",
                payload.get("skipped_reason") or payload.get("failed_reason") or "",
            )
            result["skipped_reason"] = payload.get("skipped_reason") or result.get("skipped_reason", "")
            result["failed_reason"] = payload.get("failed_reason") or result.get("failed_reason", "")
            result["suggested_next_action"] = payload.get("suggested_next_action") or result.get("suggested_next_action", "")
            result["date_scope"] = payload.get("date_scope") or result.get("date_scope", "unknown")
            result["dependency_available"] = bool(payload.get("dependency_available", result.get("dependency_available", False)))
            return result
        if not payload.get("ok"):
            return _empty_result("mootdx_intraday", symbol, target_date, "failed", payload.get("failed_reason") or "; ".join(payload.get("errors") or []))
        return _normalize_provider_frame(
            "mootdx_intraday",
            symbol,
            target_date or str(payload.get("target_date") or "")[:10],
            pd.DataFrame(payload.get("data") or []),
            source="mootdx_intraday",
            price_basis=str(payload.get("price_basis") or "unknown"),
            price_only_intraday=payload.get("price_only_intraday"),
        )
    except Exception as exc:  # noqa: BLE001
        return _empty_result("mootdx_intraday", symbol, target_date, "failed", str(exc))


def _market_prefix(symbol: str) -> str:
    sym = normalize_symbol(symbol)
    return ("sh" if sym.startswith("6") else "sz") + sym


def _http_get_text(url: str, timeout: int = 8) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"QuantDesk/{PACKAGE_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - explicit user-triggered provider
        return resp.read().decode("utf-8", errors="ignore")


def _today_only_guard(provider: str, symbol: str, target_date: str) -> dict[str, Any] | None:
    # Compare against the Asia/Shanghai calendar day, not the host-local clock,
    # so a UTC host just after Shanghai midnight does not skip today's request.
    today = datetime.now(CN_TIMEZONE).strftime("%Y-%m-%d")
    if target_date and target_date[:10] != today:
        result = _empty_result(provider, symbol, target_date, "skipped", f"{provider} 仅支持当日分钟线，不支持历史目标日期 {target_date[:10]}")
        result["date_scope"] = "current_day_only"
        result["skipped_reason"] = "仅支持当日分钟线，不支持历史目标日期"
        result["suggested_next_action"] = "请导入历史分钟线 CSV，或在目标交易日盘后尝试当日 provider。"
        return result
    return None


def _fetch_tencent_intraday(symbol: str, target_date: str) -> dict[str, Any]:
    try:
        from quantdesk.providers.tencent_provider import fetch_intraday_bars

        payload = fetch_intraday_bars(symbol, target_date, period="1m", network=True)
        if payload.get("status") == "skipped":
            result = _empty_result("tencent_intraday", symbol, target_date, "skipped", payload.get("skipped_reason") or "")
            result["skipped_reason"] = payload.get("skipped_reason") or "腾讯财经当前适配仅支持当日分钟线，不支持历史目标日期。"
            result["suggested_next_action"] = payload.get("suggested_next_action") or "请导入历史 1 分钟线 CSV，或在目标交易日盘后尝试当日 provider。"
            result["date_scope"] = payload.get("date_scope") or "current_day_only"
            return result
        if not payload.get("ok"):
            return _empty_result("tencent_intraday", symbol, target_date, "failed", payload.get("failed_reason") or "; ".join(payload.get("errors") or []))
        return _normalize_provider_frame(
            "tencent_intraday",
            symbol,
            target_date or str(payload.get("target_date") or "")[:10],
            pd.DataFrame(payload.get("data") or []),
            source="tencent_intraday",
            price_basis="raw",
            price_only_intraday=payload.get("price_only_intraday"),
        )
    except Exception as exc:  # noqa: BLE001
        return _empty_result("tencent_intraday", symbol, target_date, "failed", str(exc))


def _fetch_sina_intraday(symbol: str, target_date: str) -> dict[str, Any]:
    guarded = _today_only_guard("sina_intraday", symbol, target_date)
    if guarded:
        return guarded
    try:
        secid = _market_prefix(symbol)
        url = f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData?symbol={urllib.parse.quote(secid)}&scale=1&ma=no&datalen=240"
        text = _http_get_text(url)
        raw = json.loads(text)
        rows = []
        for item in raw if isinstance(raw, list) else []:
            rows.append({
                "symbol": symbol,
                "datetime": item.get("day") or item.get("datetime"),
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
            })
        return _normalize_provider_frame("sina_intraday", symbol, target_date, pd.DataFrame(rows), source="sina_intraday", price_basis="raw")
    except Exception as exc:  # noqa: BLE001
        return _empty_result("sina_intraday", symbol, target_date, "failed", str(exc))


PROVIDER_FETCHERS: dict[str, Callable[..., dict[str, Any]]] = {
    "local_existing": _fetch_local_existing,
    "mootdx_intraday": _fetch_mootdx_intraday,
    "akshare_intraday": _fetch_akshare_intraday,
    "ashare_intraday": _fetch_ashare_intraday,
    "tencent_intraday": _fetch_tencent_intraday,
    "sina_intraday": _fetch_sina_intraday,
}


def fetch_intraday_1m(
    provider: str,
    symbol: str,
    target_date: str,
    *,
    intraday_dir: Path = INTRADAY_1M_DIR,
    network: bool = False,
) -> dict[str, Any]:
    name = str(provider or "").strip()
    spec = PROVIDER_SPECS.get(name)
    if not spec or name not in PROVIDER_FETCHERS:
        return _empty_result(name or "unknown", symbol, target_date, "not_available", "未知分钟线 provider")
    if spec.network_required and not network:
        return _empty_result(name, symbol, target_date, "skipped", "未启用联网分钟线补全")
    if name == "local_existing":
        return _fetch_local_existing(symbol, target_date, intraday_dir=intraday_dir)
    return PROVIDER_FETCHERS[name](symbol, target_date)


def intraday_1m_provider_status(*, check_network: bool = False) -> list[dict[str, Any]]:
    """Return executable 1-minute intraday provider diagnostics."""

    return provider_registry_status(check_network=check_network)
