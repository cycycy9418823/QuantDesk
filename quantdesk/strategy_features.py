from __future__ import annotations

from typing import Any, Mapping

from quantdesk.shared import safe_float

CORE_STRATEGY_FIELDS = ("close", "ma20", "activity_value", "activity_avg_20")
NORMAL_FRESHNESS_STATUSES = {"latest_complete_daily_bar", "ok", "sample_mode", "using_last_trading_day", "market_holiday_using_last_trading_day"}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested_get(data: Mapping[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        mapping = _as_mapping(current)
        if part not in mapping:
            return default
        current = mapping[part]
    return current


def _first_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[Any, str | None]:
    for key in keys:
        value = _nested_get(data, key) if "." in key else data.get(key)
        if value not in {None, ""}:
            return value, key
    return None, None


def _first_text(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    value, _source = _first_value(data, keys)
    return str(value or "")


def _is_sample_mode(data: Mapping[str, Any]) -> bool:
    mode = str(data.get("data_mode") or data.get("mode") or "").lower()
    return mode == "sample" or bool(data.get("sample_mode") or data.get("sample_payload") or data.get("is_sample_mode"))


def _history_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("daily_rows", "history", "price_history", "bars", "ohlcv"):
        rows = data.get(key)
        if isinstance(rows, list):
            clean_rows = [row for row in rows if isinstance(row, Mapping)]
            if clean_rows:
                return clean_rows
    return []


def _last_n_average(rows: list[Mapping[str, Any]], field: str, n: int = 20) -> float | None:
    values = [safe_float(row.get(field)) for row in rows if safe_float(row.get(field)) is not None]
    if not values:
        return None
    sample = values[-n:]
    return sum(sample) / len(sample)


def _set_numeric(
    output: dict[str, Any],
    coverage: dict[str, str],
    source_notes: list[str],
    target: str,
    data: Mapping[str, Any],
    keys: tuple[str, ...],
) -> None:
    raw, source = _first_value(data, keys)
    value = safe_float(raw)
    output[target] = value
    coverage[target] = "ok" if value is not None else "missing"
    if source and source != target:
        source_notes.append(f"{target} 使用 {source} 字段映射。")


def _amount_value(data: Mapping[str, Any]) -> tuple[float | None, str | None]:
    raw, source = _first_value(data, ("amount_yuan", "amount", "amount_value", "quote.amount", "technical_snapshot.amount"))
    value = safe_float(raw)
    if value is not None:
        return value, source
    yi_raw, yi_source = _first_value(data, ("amount_yi", "technical_snapshot.amount_yi"))
    yi_value = safe_float(yi_raw)
    if yi_value is not None:
        return yi_value * 100_000_000, yi_source
    return None, None


def build_strategy_feature_row(stock_data: dict[str, Any], data_mode: str = "sample") -> dict[str, Any]:
    """Build a strategy-demo row from candidate or analysis payload fields.

    This function never downloads data and never invents real market statistics.
    In sample mode only, it may add clearly marked demo fallback values so users
    can learn the rule-checking flow without mistaking them for real statistics.
    """

    data: Mapping[str, Any] = stock_data or {}
    effective_mode = str(data.get("data_mode") or data_mode or "").lower()
    is_sample_mode = effective_mode == "sample" or _is_sample_mode(data)
    output: dict[str, Any] = dict(data)
    coverage: dict[str, str] = {}
    notes: list[str] = []
    rows = _history_rows(data)

    output["symbol"] = str(data.get("symbol") or data.get("code") or "").zfill(6)
    output["name"] = str(data.get("name") or "")
    daily_bar_date = _first_text(data, ("daily_bar_date", "data_latest_date", "latest_date", "date", "quote.date"))
    quote_date = _first_text(data, ("quote_date", "quote.date", "technical_snapshot.quote_date", "intraday_estimate_date"))
    quote_time = _first_text(data, ("quote_latest_time", "quote.time", "technical_snapshot.quote_time", "intraday_estimate_time"))

    _set_numeric(
        output,
        coverage,
        notes,
        "close",
        data,
        ("close", "latest_close", "last_close", "price", "current_price", "settled_price", "estimated_price", "quote.close", "technical_snapshot.close"),
    )
    _set_numeric(output, coverage, notes, "ma5", data, ("ma5", "ma_5", "moving_average_5", "technical_snapshot.ma5", "technical_snapshot.ma_5"))
    _set_numeric(output, coverage, notes, "ma10", data, ("ma10", "ma_10", "moving_average_10", "technical_snapshot.ma10", "technical_snapshot.ma_10"))
    _set_numeric(
        output,
        coverage,
        notes,
        "ma20",
        data,
        ("ma20", "ma_20", "ma20_price", "moving_average_20", "technical_snapshot.ma20", "technical_snapshot.ma_20", "indicators.ma20"),
    )
    if output.get("ma20") is None and rows:
        ma20 = _last_n_average(rows, "close", 20)
        if ma20 is not None:
            output["ma20"] = ma20
            coverage["ma20"] = "derived"
            notes.append("ma20 根据历史日线最近20条收盘价计算。")
    _set_numeric(output, coverage, notes, "volume", data, ("volume", "latest_volume", "turnover_volume", "vol", "quote.volume", "technical_snapshot.volume"))

    avg_raw, avg_source = _first_value(
        data,
        ("avg_volume_20", "volume_ma20", "ma_volume_20", "avg_vol_20", "technical_snapshot.avg_volume_20", "technical_snapshot.volume_ma20", "indicators.avg_volume_20"),
    )
    avg_value = safe_float(avg_raw)
    if avg_value is not None:
        output["avg_volume_20"] = avg_value
        coverage["avg_volume_20"] = "ok"
        if avg_source and avg_source != "avg_volume_20":
            notes.append(f"avg_volume_20 使用 {avg_source} 字段映射。")
    else:
        if rows:
            history_avg = _last_n_average(rows, "volume", 20)
            if history_avg is not None:
                output["avg_volume_20"] = history_avg
                coverage["avg_volume_20"] = "derived"
                notes.append("avg_volume_20 根据历史日线最近20条成交量计算。")
        if coverage.get("avg_volume_20") == "derived":
            pass
        else:
            ratio_raw, ratio_source = _first_value(data, ("volume_ratio_20", "volume_ratio", "technical_snapshot.volume_ratio_20"))
            ratio = safe_float(ratio_raw)
            volume = safe_float(output.get("volume"))
            if volume is not None and ratio is not None and ratio > 0:
                output["avg_volume_20"] = volume / ratio
                coverage["avg_volume_20"] = "derived"
                notes.append(f"avg_volume_20 根据成交量与 {ratio_source or 'volume_ratio_20'} 推导。")
            elif is_sample_mode and volume is not None:
                output["avg_volume_20"] = volume / 1.2
                output["avg_volume_20_demo"] = output["avg_volume_20"]
                coverage["avg_volume_20"] = "demo_fallback"
                notes.append("20日均量为样例演示补全字段，仅用于学习规则检查流程，不代表真实行情统计。")
            else:
                output["avg_volume_20"] = None
                coverage["avg_volume_20"] = "missing"

    ratio = safe_float(data.get("volume_ratio_20") or data.get("volume_ratio"))
    if ratio is None:
        volume = safe_float(output.get("volume"))
        avg_volume = safe_float(output.get("avg_volume_20"))
        ratio = volume / avg_volume if volume is not None and avg_volume not in {None, 0} else None
    output["volume_ratio_20"] = ratio

    amount_value, amount_source = _amount_value(data)
    volume_value = safe_float(output.get("volume"))
    existing_activity_value = safe_float(data.get("activity_value") or data.get("activity") or data.get("turnover_activity"))
    activity_value: float | None = None
    activity_source = "unknown"
    if existing_activity_value is not None:
        activity_value = existing_activity_value
        activity_source = str(data.get("activity_source") or "activity")
        coverage["activity_value"] = "ok"
    elif amount_value is not None:
        activity_value = amount_value
        activity_source = "amount"
        coverage["activity_value"] = "ok"
        if amount_source and amount_source not in {"amount_yuan", "amount"}:
            notes.append(f"activity_value 使用 {amount_source} 字段映射为成交活跃度。")
    elif volume_value is not None:
        activity_value = volume_value
        activity_source = "volume"
        coverage["activity_value"] = "ok"
    else:
        coverage["activity_value"] = "missing"

    activity_ratio_raw, activity_ratio_source = _first_value(
        data,
        ("amount_ratio", "activity_ratio_20", "volume_ratio_20", "volume_ratio", "technical_snapshot.volume_ratio_20"),
    )
    activity_ratio = safe_float(activity_ratio_raw)
    if activity_ratio is None:
        activity_ratio = ratio

    activity_avg_raw, activity_avg_source = _first_value(
        data,
        (
            "avg_amount_20",
            "amount20",
            "amount_ma20",
            "activity_avg_20",
            "avg_volume_20",
            "volume_ma20",
            "ma_volume_20",
            "avg_vol_20",
            "technical_snapshot.avg_volume_20",
            "indicators.avg_volume_20",
        ),
    )
    activity_avg = safe_float(activity_avg_raw)
    if activity_avg is not None:
        coverage["activity_avg_20"] = "ok"
        if activity_avg_source and activity_avg_source != "activity_avg_20":
            notes.append(f"activity_avg_20 使用 {activity_avg_source} 字段映射。")
    elif activity_value is not None and activity_ratio is not None and activity_ratio > 0:
        activity_avg = activity_value / activity_ratio
        coverage["activity_avg_20"] = "derived"
        notes.append("20日均成交活跃度根据当前成交活跃度和相对比例推导。")
    elif is_sample_mode and activity_value is not None:
        activity_avg = activity_value / 1.2
        activity_source = "demo_fallback" if activity_source == "unknown" else activity_source
        coverage["activity_avg_20"] = "demo_fallback"
        notes.append("20日均成交活跃度为样例演示补全字段，仅用于学习规则检查流程，不代表真实行情统计。")
    else:
        coverage["activity_avg_20"] = "missing"

    output["activity_value"] = activity_value
    output["activity_avg_20"] = activity_avg
    output["activity_ratio_20"] = activity_ratio if activity_ratio is not None else (
        activity_value / activity_avg if activity_value is not None and activity_avg not in {None, 0} else None
    )
    output["activity_source"] = activity_source
    if output.get("volume") is None and activity_value is not None:
        output["volume"] = activity_value
        coverage["volume"] = "derived"
    if output.get("avg_volume_20") is None and activity_avg is not None:
        output["avg_volume_20"] = activity_avg
        coverage["avg_volume_20"] = coverage.get("activity_avg_20", "derived")
    if activity_value is not None and activity_avg is not None:
        notes.append("策略演示中的成交条件当前使用成交活跃度字段承接；若来源为成交额，则展示为成交额复核。")

    pct_change, pct_source = _first_value(data, ("pct_change", "change_pct", "technical_snapshot.pct_change"))
    output["pct_change"] = safe_float(pct_change)
    if pct_source and pct_source != "pct_change":
        notes.append(f"pct_change 使用 {pct_source} 字段映射。")

    trust = _as_mapping(data.get("trust_assessment"))
    trust_components = _as_mapping(trust.get("components"))
    output["data_reliability_score"] = safe_float(
        data.get("data_reliability_score")
        or trust.get("data_reliability_score")
        or trust.get("data_trust_score")
        or trust.get("data_quality_score")
        or trust_components.get("data_reliability_score")
        or data.get("trust_score")
        or data.get("data_quality_score"),
        None,
    )
    explicit_level = str(data.get("data_warning_level") or data.get("warning_level") or data.get("data_health_level") or "").upper()
    freshness_status = str(data.get("data_freshness_status") or data.get("freshness_status") or "").lower()
    if output["data_reliability_score"] is None and is_sample_mode and freshness_status in NORMAL_FRESHNESS_STATUSES and explicit_level not in {"ERROR", "WARNING"}:
        output["data_reliability_score"] = 95.0
        coverage["data_reliability_score"] = "demo_fallback"
        notes.append("数据可信度为样例演示补全字段，仅用于学习规则检查流程。")
    elif output["data_reliability_score"] is None:
        coverage["data_reliability_score"] = "unknown"
        notes.append("缺少数据可信度字段，需查看数据健康状态。")
    else:
        coverage["data_reliability_score"] = "ok"

    missing_required = any(coverage.get(field) == "missing" for field in CORE_STRATEGY_FIELDS)
    has_fallback = any(coverage.get(field) in {"derived", "fallback", "demo_fallback"} for field in CORE_STRATEGY_FIELDS)
    if missing_required:
        coverage_level = "missing_required"
    elif has_fallback:
        coverage_level = "partial"
    else:
        coverage_level = "complete"

    output["feature_coverage"] = coverage
    output["feature_notes"] = notes
    output["coverage_level"] = coverage_level
    output["data_date"] = daily_bar_date or quote_date
    output["daily_bar_date"] = daily_bar_date
    output["quote_date"] = quote_date
    output["quote_latest_time"] = quote_time
    if daily_bar_date and quote_date and daily_bar_date != quote_date:
        output["feature_freshness"] = "mixed"
        output["feature_freshness_label"] = "混合数据"
        output["data_category"] = "混合数据"
        output["feature_freshness_notes"] = [
            "部分趋势字段来自旧日线，部分观察字段来自盘中或估算数据。",
            "本结果仅用于学习规则检查流程，正式复盘需等待完整日线更新。",
        ]
    elif quote_date and not daily_bar_date:
        output["feature_freshness"] = "intraday_estimate_only"
        output["feature_freshness_label"] = "盘中估算"
        output["data_category"] = "等待完整日线"
        output["feature_freshness_notes"] = ["当前仅识别到盘中或估算日期，不能当作完整日线复盘。"]
    else:
        output["feature_freshness"] = "daily_bar"
        output["feature_freshness_label"] = f"基于完整日线：{daily_bar_date}" if daily_bar_date else "数据日期未知"
        output["data_category"] = "完整日线" if daily_bar_date else "数据日期未知"
        output["feature_freshness_notes"] = []
    return output
