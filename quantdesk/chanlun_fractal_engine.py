"""Chanlun fractal recognition engine.

This module implements only the first programmable Chanlun layer: OHLC
normalization, inclusion handling, and top/bottom fractal observation.  It does
not identify bi, segments, zhongshu, divergence, or trading actions.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
CHANLUN_FRACTAL_RUNTIME_DIR = PROJECT_ROOT / "data" / "chanlun" / "fractals"
CHANLUN_FRACTALS_LATEST_JSON = RESULTS_DIR / "chanlun_fractals_latest.json"
DISCLAIMER = "缠论分型仅用于结构学习与纸面研究，不构成投资建议。"


FIELD_ALIASES = {
    "symbol": ["symbol", "code", "ticker", "代码", "股票代码"],
    "date": ["date", "trade_date", "日期"],
    "datetime": ["datetime", "trade_time", "交易时间"],
    "time": ["time", "时间", "时刻"],
    "open": ["open", "开盘", "开盘价"],
    "high": ["high", "最高", "最高价"],
    "low": ["low", "最低", "最低价"],
    "close": ["close", "收盘", "收盘价", "price"],
    "volume": ["volume", "vol", "成交量"],
    "amount": ["amount", "money", "turnover", "成交额"],
}


def _canonical_column(name: Any) -> str:
    text = str(name or "").strip()
    lower = text.lower()
    for canonical, aliases in FIELD_ALIASES.items():
        if text in aliases or lower in {str(alias).lower() for alias in aliases}:
            return canonical
    return text


def _read_csv_auto(path: Path) -> pd.DataFrame:
    last_error: str = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        frame = read_csv_frame(path, encoding=encoding)
        warnings = list(frame.attrs.get("warnings") or [])
        if any(item.startswith("csv_read_failed:") for item in warnings):
            last_error = "; ".join(warnings)
            continue
        return frame
    raise RuntimeError(f"无法读取 K 线 CSV：{last_error}")


def _safe_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text[:10]
    return parsed.strftime("%Y-%m-%d")


def _safe_datetime_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def normalize_chanlun_ohlc(df: pd.DataFrame | list[dict[str, Any]], *, symbol: Any = None, timeframe: str = "daily") -> dict[str, Any]:
    """Normalize OHLC rows for Chanlun structure observation."""

    warnings: list[str] = []
    errors: list[str] = []
    if df is None:
        return {"ok": False, "status": "no_data", "bars": [], "warnings": ["没有输入 K 线数据。"], "errors": []}
    frame = pd.DataFrame(df).copy()
    raw_count = int(len(frame))
    if frame.empty:
        return {"ok": False, "status": "no_data", "bars": [], "raw_bar_count": 0, "warnings": ["输入 K 线为空。"], "errors": []}

    frame = frame.rename(columns={col: _canonical_column(col) for col in frame.columns})
    required = ["open", "high", "low", "close"]
    missing = [col for col in required if col not in frame.columns]
    if missing:
        return {
            "ok": False,
            "status": "invalid_schema",
            "bars": [],
            "raw_bar_count": raw_count,
            "warnings": [],
            "errors": [f"缺少 OHLC 字段：{', '.join(missing)}"],
            "disclaimer": DISCLAIMER,
        }

    if "symbol" not in frame.columns:
        frame["symbol"] = normalize_symbol(symbol)
    else:
        frame["symbol"] = frame["symbol"].map(normalize_symbol)
        if symbol:
            frame.loc[frame["symbol"] == "", "symbol"] = normalize_symbol(symbol)
    if not symbol and "symbol" in frame.columns:
        symbol = next((x for x in frame["symbol"].tolist() if x), "")
    symbol_text = normalize_symbol(symbol)
    if symbol_text:
        frame.loc[frame["symbol"] == "", "symbol"] = symbol_text

    if "datetime" not in frame.columns:
        if "date" in frame.columns and "time" in frame.columns:
            frame["datetime"] = frame["date"].astype(str).str.strip() + " " + frame["time"].astype(str).str.strip()
        elif "date" in frame.columns:
            frame["datetime"] = frame["date"].astype(str).str.strip()
        else:
            frame["datetime"] = [str(i) for i in range(len(frame))]
            warnings.append("缺少 date/datetime 字段，已使用行号排序。")
    if "date" not in frame.columns:
        frame["date"] = frame["datetime"].astype(str).str[:10]

    for col in required + ["volume", "amount"]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    invalid_number = frame[required].isna().any(axis=1)
    if invalid_number.any():
        bad_count = int(invalid_number.sum())
        errors.append(f"OHLC 存在非数字或空值：{bad_count} 行。")
        frame = frame.loc[~invalid_number].copy()
    if frame.empty:
        return {"ok": False, "status": "invalid_schema", "bars": [], "raw_bar_count": raw_count, "warnings": warnings, "errors": errors, "disclaimer": DISCLAIMER}

    invalid_ohlc = (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    )
    if invalid_ohlc.any():
        bad_count = int(invalid_ohlc.sum())
        warnings.append(f"OHLC 高低价关系异常：{bad_count} 行，已标记 invalid。")
        return {
            "ok": False,
            "status": "invalid_ohlc",
            "bars": [],
            "raw_bar_count": raw_count,
            "warnings": warnings,
            "errors": errors,
            "disclaimer": DISCLAIMER,
        }

    frame["_dt_sort"] = pd.to_datetime(frame["datetime"], errors="coerce")
    if frame["_dt_sort"].isna().all():
        frame["_dt_sort"] = range(len(frame))
        warnings.append("datetime 无法解析，已按原始行顺序处理。")
    frame = frame.sort_values("_dt_sort").reset_index(drop=True)

    bars: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        bar = {
            "index": int(idx),
            "symbol": normalize_symbol(row.get("symbol") or symbol_text),
            "timeframe": timeframe,
            "date": _safe_date_text(row.get("date") or row.get("datetime")),
            "datetime": _safe_datetime_text(row.get("datetime")) if timeframe != "daily" else None,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": safe_float(row.get("volume"), None),
            "amount": safe_float(row.get("amount"), None),
            "original_indices": [int(idx)],
            "source_count": 1,
            "include_resolved": False,
            "merge_direction": "",
        }
        bars.append(bar)

    return {
        "ok": True,
        "status": "ok",
        "symbol": symbol_text or (bars[0]["symbol"] if bars else ""),
        "timeframe": timeframe,
        "raw_bar_count": raw_count,
        "bar_count": len(bars),
        "bars": bars,
        "warnings": warnings,
        "errors": errors,
        "disclaimer": DISCLAIMER,
    }


def _contains(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        (safe_float(a.get("high"), 0) >= safe_float(b.get("high"), 0) and safe_float(a.get("low"), 0) <= safe_float(b.get("low"), 0))
        or (safe_float(b.get("high"), 0) >= safe_float(a.get("high"), 0) and safe_float(b.get("low"), 0) <= safe_float(a.get("low"), 0))
    )


def _infer_direction(processed: list[dict[str, Any]], current: dict[str, Any], incoming: dict[str, Any], requested: str) -> str:
    if requested in {"up", "down"}:
        return requested
    if len(processed) >= 2:
        prev = processed[-2]
        if safe_float(current.get("high"), 0) > safe_float(prev.get("high"), 0) and safe_float(current.get("low"), 0) > safe_float(prev.get("low"), 0):
            return "up"
        if safe_float(current.get("high"), 0) < safe_float(prev.get("high"), 0) and safe_float(current.get("low"), 0) < safe_float(prev.get("low"), 0):
            return "down"
    close_diff = safe_float(incoming.get("close"), 0) - safe_float(current.get("close"), 0)
    if close_diff > 0:
        return "up"
    if close_diff < 0:
        return "down"
    return "unknown"


def _merge_bars(current: dict[str, Any], incoming: dict[str, Any], direction: str) -> dict[str, Any]:
    merged = dict(current)
    if direction == "up":
        merged["high"] = max(safe_float(current.get("high"), 0), safe_float(incoming.get("high"), 0))
        merged["low"] = max(safe_float(current.get("low"), 0), safe_float(incoming.get("low"), 0))
    elif direction == "down":
        merged["high"] = min(safe_float(current.get("high"), 0), safe_float(incoming.get("high"), 0))
        merged["low"] = min(safe_float(current.get("low"), 0), safe_float(incoming.get("low"), 0))
    else:
        merged["high"] = max(safe_float(current.get("high"), 0), safe_float(incoming.get("high"), 0))
        merged["low"] = min(safe_float(current.get("low"), 0), safe_float(incoming.get("low"), 0))
    merged["close"] = safe_float(incoming.get("close"), merged.get("close"))
    merged["date"] = incoming.get("date") or current.get("date")
    merged["datetime"] = incoming.get("datetime") or current.get("datetime")
    merged["index"] = incoming.get("index", current.get("index"))
    merged["original_indices"] = list(current.get("original_indices") or []) + list(incoming.get("original_indices") or [])
    merged["source_count"] = int(current.get("source_count") or 1) + int(incoming.get("source_count") or 1)
    merged["include_resolved"] = True
    merged["merge_direction"] = direction
    return merged


def resolve_include_relationship(bars: list[dict[str, Any]], *, direction_mode: str = "auto", strict: bool = True) -> dict[str, Any]:
    """Resolve adjacent K-line inclusion relationships in an explainable way."""

    warnings: list[str] = []
    if not bars:
        return {"ok": False, "status": "no_data", "bars": [], "merged_count": 0, "warnings": ["没有 K 线可处理。"], "disclaimer": DISCLAIMER}
    processed: list[dict[str, Any]] = []
    merged_count = 0
    unknown_count = 0
    for raw in bars:
        bar = dict(raw)
        if not processed:
            processed.append(bar)
            continue
        current = processed[-1]
        if _contains(current, bar):
            direction = _infer_direction(processed, current, bar, direction_mode)
            if direction == "unknown":
                unknown_count += 1
                warnings.append("包含关系方向无法确认，已采用保守合并。")
            processed[-1] = _merge_bars(current, bar, direction)
            merged_count += 1
        else:
            processed.append(bar)
    if unknown_count:
        warnings.append(f"direction_unknown 次数：{unknown_count}")
    return {
        "ok": True,
        "status": "ok",
        "bars": processed,
        "raw_bar_count": len(bars),
        "processed_bar_count": len(processed),
        "merged_count": merged_count,
        "unknown_direction_count": unknown_count,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def _fractal_id(symbol: str, timeframe: str, date_text: str, kind: str, index: int) -> str:
    clean_date = str(date_text or index).replace("-", "").replace(" ", "_").replace(":", "")
    return f"{normalize_symbol(symbol)}_{timeframe}_{clean_date}_{kind}"


def detect_fractals(
    bars: list[dict[str, Any]],
    *,
    include_resolved: bool = True,
    mode: str = "strict",
    min_separation: int = 1,
    symbol: Any = None,
    timeframe: str = "daily",
) -> dict[str, Any]:
    """Detect confirmed top/bottom fractal observations."""

    warnings: list[str] = []
    if include_resolved:
        include_payload = resolve_include_relationship(bars)
        working = include_payload.get("bars") or []
        warnings.extend(include_payload.get("warnings") or [])
    else:
        working = list(bars or [])
    if len(working) < 3:
        return {
            "ok": True,
            "status": "insufficient_data",
            "bars": working,
            "fractals": [],
            "diagnostics": {
                "top_count": 0,
                "bottom_count": 0,
                "unconfirmed_count": 0,
                "warning_count": 1,
                "warnings": ["K 线数量不足，无法确认分型。"],
            },
            "disclaimer": DISCLAIMER,
        }
    mode_text = "loose" if mode == "loose" else "strict"
    reliability = "low" if mode_text == "loose" else "high"
    fractals: list[dict[str, Any]] = []
    skipped_dense = 0
    for idx in range(1, len(working) - 1):
        left, mid, right = working[idx - 1], working[idx], working[idx + 1]
        top_strict = mid["high"] > left["high"] and mid["high"] > right["high"] and mid["low"] > left["low"] and mid["low"] > right["low"]
        bottom_strict = mid["low"] < left["low"] and mid["low"] < right["low"] and mid["high"] < left["high"] and mid["high"] < right["high"]
        top_loose = mid["high"] > left["high"] and mid["high"] > right["high"]
        bottom_loose = mid["low"] < left["low"] and mid["low"] < right["low"]
        kind = ""
        if top_strict or (mode_text == "loose" and top_loose):
            kind = "top"
        elif bottom_strict or (mode_text == "loose" and bottom_loose):
            kind = "bottom"
        if not kind:
            continue
        if fractals and min_separation > 0 and idx - int(fractals[-1].get("bar_index", -999)) < min_separation:
            skipped_dense += 1
            continue
        date_text = mid.get("datetime") or mid.get("date") or str(idx)
        symbol_text = normalize_symbol(symbol or mid.get("symbol"))
        fractals.append(
            {
                "symbol": symbol_text,
                "timeframe": timeframe,
                "fractal_id": _fractal_id(symbol_text, timeframe, date_text, kind, idx),
                "fractal_type": kind,
                "fractal_label": "顶分型观察" if kind == "top" else "底分型观察",
                "date": mid.get("date"),
                "datetime": mid.get("datetime"),
                "bar_index": idx,
                "high": mid.get("high"),
                "low": mid.get("low"),
                "middle_close": mid.get("close"),
                "left_bar_index": idx - 1,
                "right_bar_index": idx + 1,
                "confirmed": True,
                "confirmation_bar_index": idx + 1,
                "include_resolved": bool(mid.get("include_resolved") or include_resolved),
                "mode": mode_text,
                "reliability": reliability,
                "notes": ("顶分型" if kind == "top" else "底分型") + "，仅作为结构观察信号。",
                "disclaimer": DISCLAIMER,
            }
        )
    top_count = sum(1 for row in fractals if row.get("fractal_type") == "top")
    bottom_count = sum(1 for row in fractals if row.get("fractal_type") == "bottom")
    consecutive_same = sum(1 for a, b in zip(fractals, fractals[1:]) if a.get("fractal_type") == b.get("fractal_type"))
    if skipped_dense:
        warnings.append(f"因 min_separation 过滤过密分型：{skipped_dense} 个。")
    if len(working) >= 20 and len(fractals) / max(len(working), 1) >= 0.70:
        warnings.append("分型过密，可能由噪音或包含关系处理不足导致。")
    if len(working) >= 100 and not fractals:
        warnings.append("未识别到分型，请检查数据或参数。")
    diagnostics = {
        "processed_bar_count": len(working),
        "top_count": top_count,
        "bottom_count": bottom_count,
        "fractal_count": len(fractals),
        "consecutive_same_type_count": consecutive_same,
        "unconfirmed_count": 0,
        "warning_count": len(warnings),
        "warnings": warnings,
    }
    return {
        "ok": True,
        "status": "ok",
        "bars": working,
        "fractals": fractals,
        "diagnostics": diagnostics,
        "mode": mode_text,
        "disclaimer": DISCLAIMER,
    }


def _default_input_path(symbol: str, timeframe: str) -> Path:
    if timeframe == "daily":
        return PROJECT_ROOT / "data" / "daily" / f"{normalize_symbol(symbol)}.csv"
    if timeframe == "1m":
        return PROJECT_ROOT / "data" / "intraday" / "1m" / f"{normalize_symbol(symbol)}.csv"
    return PROJECT_ROOT / "data" / "intraday" / timeframe / f"{normalize_symbol(symbol)}.csv"


def _filter_dates(frame: pd.DataFrame, start_date: str | None, end_date: str | None) -> pd.DataFrame:
    if frame.empty or (not start_date and not end_date):
        return frame
    renamed = frame.rename(columns={col: _canonical_column(col) for col in frame.columns})
    date_col = "datetime" if "datetime" in renamed.columns else "date" if "date" in renamed.columns else ""
    if not date_col:
        return frame
    dates = pd.to_datetime(renamed[date_col], errors="coerce")
    mask = pd.Series([True] * len(renamed), index=renamed.index)
    if start_date:
        mask &= dates >= pd.to_datetime(start_date)
    if end_date:
        mask &= dates <= pd.to_datetime(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    return frame.loc[mask].copy()


def _write_fractal_report(payload: dict[str, Any], report_path: Path) -> None:
    lines = [
        "# 缠论分型识别学习报告",
        "",
        f"- 分析股票：{payload.get('symbol') or '-'}",
        f"- 周期：{payload.get('timeframe') or '-'}",
        f"- 数据范围：{payload.get('input_summary', {}).get('start_date') or '-'} 至 {payload.get('input_summary', {}).get('end_date') or '-'}",
        f"- 原始 K线数量：{payload.get('input_summary', {}).get('raw_bar_count', 0)}",
        f"- 处理后 K线数量：{payload.get('input_summary', {}).get('processed_bar_count', 0)}",
        "",
        "## 包含关系处理",
        "",
        f"- 是否启用：{payload.get('include_relationship', {}).get('enabled')}",
        f"- 合并数量：{payload.get('include_relationship', {}).get('merged_count', 0)}",
        "- 说明：本轮采用可解释的简化包含关系处理，用于结构学习，不等同于完整流派实现。",
        "",
        "## 分型统计",
        "",
        f"- 顶分型数量：{payload.get('diagnostics', {}).get('top_count', 0)}",
        f"- 底分型数量：{payload.get('diagnostics', {}).get('bottom_count', 0)}",
        "",
        "## 主要分型列表",
        "",
    ]
    for row in (payload.get("fractals") or [])[:30]:
        lines.append(f"- {row.get('date') or row.get('datetime')}: {row.get('fractal_label')} high={row.get('high')} low={row.get('low')} confirmed={row.get('confirmed')}")
    warnings = payload.get("diagnostics", {}).get("warnings") or []
    lines.extend(["", "## 诊断 warning", ""])
    if warnings:
        for item in warnings:
            lines.append(f"- {item}")
    else:
        lines.append("- 暂无明显 warning。")
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "- 若分型结果稳定，可在后续版本继续构建笔识别。",
            "",
            "## 风险提示",
            "",
            "- 本报告仅用于缠论结构学习和纸面研究，不构成投资建议。",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_chanlun_fractal_analysis(
    symbol: Any,
    *,
    timeframe: str = "daily",
    start_date: str | None = None,
    end_date: str | None = None,
    input_path: str | Path | None = None,
    output_dir: str | Path = "results",
    report: bool = True,
    mode: str = "strict",
    include_resolved: bool = True,
    min_separation: int = 1,
    write: bool = True,
) -> dict[str, Any]:
    symbol_text = normalize_symbol(symbol)
    source_path = Path(input_path) if input_path else _default_input_path(symbol_text, timeframe)
    if not source_path.is_absolute():
        source_path = PROJECT_ROOT / source_path
    output_base = Path(output_dir)
    if not output_base.is_absolute():
        output_base = PROJECT_ROOT / output_base
    warnings: list[str] = []
    if not source_path.exists():
        payload = {
            "ok": False,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "task_type": "chanlun_fractal_analysis",
            "symbol": symbol_text,
            "timeframe": timeframe,
            "generated_at": now_text(),
            "status": "no_data",
            "input_summary": {"source": str(source_path), "raw_bar_count": 0, "processed_bar_count": 0, "start_date": start_date or "", "end_date": end_date or ""},
            "include_relationship": {"enabled": bool(include_resolved), "merged_count": 0, "warnings": ["输入文件不存在。"]},
            "fractals": [],
            "processed_bars": [],
            "diagnostics": {"top_count": 0, "bottom_count": 0, "warning_count": 1, "warnings": ["输入文件不存在，未执行分型识别。"]},
            "disclaimer": DISCLAIMER,
        }
        if write:
            output_base.mkdir(parents=True, exist_ok=True)
            write_json(output_base / "chanlun_fractals_latest.json", payload)
        return payload

    frame = _read_csv_auto(source_path)
    read_warnings = list(frame.attrs.get("warnings") or [])
    frame = _filter_dates(frame, start_date, end_date)
    normalized = normalize_chanlun_ohlc(frame, symbol=symbol_text, timeframe=timeframe)
    if read_warnings:
        normalized["warnings"] = read_warnings + list(normalized.get("warnings") or [])
    if not normalized.get("ok"):
        payload = {
            "ok": False,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "task_type": "chanlun_fractal_analysis",
            "symbol": symbol_text,
            "timeframe": timeframe,
            "generated_at": now_text(),
            "status": normalized.get("status", "invalid"),
            "input_summary": {"source": str(source_path), "raw_bar_count": normalized.get("raw_bar_count", 0), "processed_bar_count": 0, "start_date": start_date or "", "end_date": end_date or ""},
            "include_relationship": {"enabled": bool(include_resolved), "merged_count": 0, "warnings": normalized.get("warnings", [])},
            "fractals": [],
            "processed_bars": [],
            "diagnostics": {"top_count": 0, "bottom_count": 0, "warning_count": len(normalized.get("warnings", [])), "warnings": normalized.get("warnings", []), "errors": normalized.get("errors", [])},
            "disclaimer": DISCLAIMER,
        }
        if write:
            output_base.mkdir(parents=True, exist_ok=True)
            write_json(output_base / "chanlun_fractals_latest.json", payload)
        return payload

    bars = normalized.get("bars") or []
    include_payload = resolve_include_relationship(bars) if include_resolved else {"ok": True, "bars": bars, "merged_count": 0, "warnings": []}
    processed = include_payload.get("bars") or []
    detection = detect_fractals(processed, include_resolved=False, mode=mode, min_separation=min_separation, symbol=symbol_text, timeframe=timeframe)
    warnings.extend(normalized.get("warnings") or [])
    warnings.extend(include_payload.get("warnings") or [])
    warnings.extend(detection.get("diagnostics", {}).get("warnings") or [])
    raw_dates = [row.get("date") for row in bars if row.get("date")]
    latest_report_path = REPORTS_DIR / f"chanlun_fractal_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "chanlun_fractal_analysis",
        "symbol": symbol_text,
        "timeframe": timeframe,
        "generated_at": now_text(),
        "status": "ok",
        "input_summary": {
            "source": str(source_path),
            "raw_bar_count": normalized.get("raw_bar_count", len(bars)),
            "processed_bar_count": len(processed),
            "start_date": start_date or (raw_dates[0] if raw_dates else ""),
            "end_date": end_date or (raw_dates[-1] if raw_dates else ""),
        },
        "include_relationship": {
            "enabled": bool(include_resolved),
            "merged_count": include_payload.get("merged_count", 0),
            "warnings": include_payload.get("warnings", []),
        },
        "processed_bars": processed,
        "fractals": detection.get("fractals") or [],
        "diagnostics": {
            **(detection.get("diagnostics") or {}),
            "raw_bar_count": normalized.get("raw_bar_count", len(bars)),
            "processed_bar_count": len(processed),
            "merged_count": include_payload.get("merged_count", 0),
            "warning_count": len(warnings),
            "warnings": warnings,
        },
        "report_path": str(latest_report_path) if report else "",
        "disclaimer": DISCLAIMER,
    }
    if write:
        output_base.mkdir(parents=True, exist_ok=True)
        CHANLUN_FRACTAL_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        write_json(output_base / "chanlun_fractals_latest.json", payload)
        write_json(CHANLUN_FRACTAL_RUNTIME_DIR / f"{symbol_text}_{timeframe}_fractals.json", payload)
        if report:
            _write_fractal_report(payload, latest_report_path)
    return payload


def latest_chanlun_fractals(path: Path | None = None) -> dict[str, Any]:
    target = path or CHANLUN_FRACTALS_LATEST_JSON
    payload = read_json(target, {})
    if isinstance(payload, dict) and payload:
        return payload
    return {"ok": True, "exists": False, "message": "暂无缠论分型识别结果。", "fractals": [], "diagnostics": {}, "disclaimer": DISCLAIMER}


def chanlun_fractal_status(path: Path | None = None) -> dict[str, Any]:
    target = path or CHANLUN_FRACTALS_LATEST_JSON
    payload = read_json(target, {})
    return {
        "ok": True,
        "exists": bool(isinstance(payload, dict) and payload),
        "path": str(target),
        "symbol": payload.get("symbol", "") if isinstance(payload, dict) else "",
        "timeframe": payload.get("timeframe", "") if isinstance(payload, dict) else "",
        "fractal_count": len(payload.get("fractals", [])) if isinstance(payload, dict) else 0,
        "diagnostics": payload.get("diagnostics", {}) if isinstance(payload, dict) else {},
        "disclaimer": DISCLAIMER,
    }


def latest_chanlun_fractal_report_html() -> str:
    reports = sorted(REPORTS_DIR.glob("chanlun_fractal_report_*.md"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not reports:
        return "<html><body><p>暂无缠论分型报告。</p></body></html>"
    text = reports[0].read_text(encoding="utf-8", errors="ignore")
    body = html.escape(text).replace("\n", "<br>\n")
    return f"<html><head><meta charset='utf-8'><title>缠论分型报告</title></head><body><pre>{body}</pre></body></html>"


__all__ = [
    "CHANLUN_FRACTALS_LATEST_JSON",
    "DISCLAIMER",
    "chanlun_fractal_status",
    "detect_fractals",
    "latest_chanlun_fractal_report_html",
    "latest_chanlun_fractals",
    "normalize_chanlun_ohlc",
    "resolve_include_relationship",
    "run_chanlun_fractal_analysis",
]
