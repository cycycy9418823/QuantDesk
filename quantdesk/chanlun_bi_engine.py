"""Chanlun bi recognition engine.

This module implements only the second programmable Chanlun layer: building
"bi" observations from confirmed top/bottom fractals.  It does not identify
segments, zhongshu, divergence, buy/sell points, or trading actions.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.chanlun_fractal_engine import (
    PROJECT_ROOT,
    REPORTS_DIR,
    run_chanlun_fractal_analysis,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, safe_int, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

RESULTS_DIR = PROJECT_ROOT / "results"
CHANLUN_BI_RUNTIME_DIR = PROJECT_ROOT / "data" / "chanlun" / "bi"
CHANLUN_BI_LATEST_JSON = RESULTS_DIR / "chanlun_bi_latest.json"
DISCLAIMER = "缠论笔识别仅用于结构学习与纸面研究，不构成投资建议。"


def _fractal_key(row: dict[str, Any]) -> int:
    return safe_int(row.get("bar_index"), safe_int(row.get("index"), 0)) or 0


def _date_text(row: dict[str, Any]) -> str:
    return str(row.get("date") or row.get("datetime") or "").strip()


def _read_payload(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    payload = read_json(target, {})
    return payload if isinstance(payload, dict) else {}


def normalize_fractals_for_bi(fractals: Any, bars: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Normalize v3.0.2 fractal payloads for bi recognition."""

    payload = fractals if isinstance(fractals, dict) else None
    rows = payload.get("fractals") if isinstance(payload, dict) else fractals
    bars_payload = bars if bars is not None else payload.get("processed_bars") if isinstance(payload, dict) else []
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not isinstance(rows, list):
        rows = []
    for idx, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        ftype = str(raw.get("fractal_type") or "").strip().lower()
        if ftype not in {"top", "bottom"}:
            warnings.append(f"忽略未知分型类型：{raw.get('fractal_type')}")
            continue
        payload_symbol = payload.get("symbol") if isinstance(payload, dict) else ""
        payload_timeframe = payload.get("timeframe") if isinstance(payload, dict) else "daily"
        symbol = normalize_symbol(raw.get("symbol") or payload_symbol)
        timeframe = str(raw.get("timeframe") or payload_timeframe or "daily")
        bar_index = _fractal_key(raw)
        high = safe_float(raw.get("high"), None)
        low = safe_float(raw.get("low"), None)
        if high is None or low is None:
            warnings.append(f"忽略缺少 high/low 的分型：{raw.get('fractal_id') or idx}")
            continue
        normalized.append(
            {
                **raw,
                "symbol": symbol,
                "timeframe": timeframe,
                "fractal_id": raw.get("fractal_id") or f"{symbol}_{timeframe}_fractal_{idx:04d}",
                "fractal_type": ftype,
                "bar_index": bar_index,
                "date": raw.get("date") or "",
                "datetime": raw.get("datetime"),
                "high": float(high),
                "low": float(low),
                "confirmed": bool(raw.get("confirmed", True)),
                "provisional": bool(raw.get("provisional", False)),
            }
        )
    normalized = sorted(normalized, key=lambda item: (safe_int(item.get("bar_index"), 0) or 0, _date_text(item)))
    return {
        "ok": True,
        "status": "ok" if normalized else "no_fractals",
        "fractals": normalized,
        "bars": bars_payload or [],
        "raw_fractal_count": len(rows),
        "normalized_fractal_count": len(normalized),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def _is_better_same_type(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    ftype = candidate.get("fractal_type")
    if ftype == "top":
        cand_high = safe_float(candidate.get("high"), 0) or 0
        cur_high = safe_float(current.get("high"), 0) or 0
        if cand_high != cur_high:
            return cand_high > cur_high
        return _fractal_key(candidate) >= _fractal_key(current)
    cand_low = safe_float(candidate.get("low"), 0) or 0
    cur_low = safe_float(current.get("low"), 0) or 0
    if cand_low != cur_low:
        return cand_low < cur_low
    return _fractal_key(candidate) >= _fractal_key(current)


def select_effective_fractals(
    fractals: Any,
    *,
    prefer_extreme: bool = True,
    min_separation: int = 4,
    require_alternation: bool = True,
) -> dict[str, Any]:
    """Filter consecutive same-type fractals before building bi observations."""

    normalized = normalize_fractals_for_bi(fractals)
    rows = normalized.get("fractals") or []
    if not rows:
        return {
            "ok": True,
            "status": "no_fractals",
            "effective_fractals": [],
            "removed_fractals": [],
            "diagnostics": {"raw_fractal_count": 0, "effective_fractal_count": 0, "removed_fractal_count": 0, "warnings": ["有效分型不足，无法形成笔。"]},
            "disclaimer": DISCLAIMER,
        }
    warnings: list[str] = []
    effective: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for row in rows:
        if not effective:
            effective.append(row)
            continue
        last = effective[-1]
        same_type = row.get("fractal_type") == last.get("fractal_type")
        if same_type and require_alternation:
            if prefer_extreme and _is_better_same_type(row, last):
                removed.append({**last, "removed_reason": "same_type_replaced_by_more_extreme"})
                effective[-1] = row
                warnings.append("连续同类分型已合并，保留更极端分型。")
            else:
                removed.append({**row, "removed_reason": "same_type_less_extreme"})
                warnings.append("连续同类分型已合并，移除较弱分型。")
            continue
        effective.append(row)
    diagnostics = {
        "raw_fractal_count": len(rows),
        "effective_fractal_count": len(effective),
        "removed_fractal_count": len(removed),
        "min_separation": int(min_separation),
        "warnings": warnings,
        "removed_fractals": removed,
    }
    return {
        "ok": True,
        "status": "ok" if len(effective) >= 2 else "no_bi",
        "effective_fractals": effective,
        "removed_fractals": removed,
        "diagnostics": diagnostics,
        "disclaimer": DISCLAIMER,
    }


def _bi_direction(start: dict[str, Any], end: dict[str, Any]) -> str:
    pair = (start.get("fractal_type"), end.get("fractal_type"))
    if pair == ("bottom", "top"):
        return "up"
    if pair == ("top", "bottom"):
        return "down"
    return ""


def _bi_prices(direction: str, start: dict[str, Any], end: dict[str, Any]) -> tuple[float, float]:
    if direction == "up":
        return float(safe_float(start.get("low"), 0) or 0), float(safe_float(end.get("high"), 0) or 0)
    return float(safe_float(start.get("high"), 0) or 0), float(safe_float(end.get("low"), 0) or 0)


def _build_one_bi(symbol: str, timeframe: str, ordinal: int, start: dict[str, Any], end: dict[str, Any], direction: str, *, provisional: bool = False) -> dict[str, Any]:
    start_price, end_price = _bi_prices(direction, start, end)
    price_change = end_price - start_price
    pct = (price_change / start_price * 100) if start_price else 0.0
    bar_count = max(0, (_fractal_key(end) - _fractal_key(start)))
    slope = price_change / bar_count if bar_count else 0.0
    label = "上升笔观察" if direction == "up" else "下降笔观察"
    return {
        "bi_id": f"{symbol}_{timeframe}_bi_{ordinal:04d}",
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "direction_label": label,
        "start_fractal_type": start.get("fractal_type"),
        "end_fractal_type": end.get("fractal_type"),
        "start_date": start.get("date") or "",
        "end_date": end.get("date") or "",
        "start_datetime": start.get("datetime"),
        "end_datetime": end.get("datetime"),
        "start_bar_index": _fractal_key(start),
        "end_bar_index": _fractal_key(end),
        "start_price": round(start_price, 6),
        "end_price": round(end_price, 6),
        "price_change": round(price_change, 6),
        "price_change_pct": round(pct, 4),
        "bar_count": bar_count,
        "confirmed": not provisional,
        "provisional": bool(provisional),
        "start_fractal_id": start.get("fractal_id"),
        "end_fractal_id": end.get("fractal_id"),
        "strength": {
            "absolute_change": round(abs(price_change), 6),
            "percent_change": round(abs(pct), 4),
            "bar_count": bar_count,
            "slope": round(slope, 6),
        },
        "notes": f"{label}，仅作为结构学习信号。",
        "disclaimer": DISCLAIMER,
    }


def build_bi_from_fractals(
    fractals: Any,
    bars: list[dict[str, Any]] | None = None,
    *,
    min_separation: int = 4,
    strict: bool = True,
    allow_provisional: bool = False,
) -> dict[str, Any]:
    """Build bi observations from alternating top/bottom fractals."""

    normalized = normalize_fractals_for_bi(fractals, bars=bars)
    selected = select_effective_fractals(normalized.get("fractals") or [], min_separation=min_separation, require_alternation=True)
    effective = selected.get("effective_fractals") or []
    warnings = list(normalized.get("warnings") or []) + list(selected.get("diagnostics", {}).get("warnings") or [])
    rejected: list[dict[str, Any]] = []
    bi_list: list[dict[str, Any]] = []
    symbol = normalize_symbol((effective[0].get("symbol") if effective else "") or "")
    timeframe = str((effective[0].get("timeframe") if effective else "") or "daily")
    ordinal = 1
    for idx in range(len(effective) - 1):
        start = effective[idx]
        end = effective[idx + 1]
        direction = _bi_direction(start, end)
        if not direction:
            rejected.append({"start_fractal_id": start.get("fractal_id"), "end_fractal_id": end.get("fractal_id"), "rejected_reason": "same_type_not_alternating"})
            continue
        if _fractal_key(end) - _fractal_key(start) < int(min_separation):
            rejected.append({"start_fractal_id": start.get("fractal_id"), "end_fractal_id": end.get("fractal_id"), "direction": direction, "rejected_reason": "min_separation_not_met"})
            continue
        confirmed = bool(start.get("confirmed", True)) and bool(end.get("confirmed", True))
        if not confirmed and not allow_provisional:
            rejected.append({"start_fractal_id": start.get("fractal_id"), "end_fractal_id": end.get("fractal_id"), "direction": direction, "rejected_reason": "unconfirmed_fractal"})
            continue
        bi_list.append(_build_one_bi(symbol, timeframe, ordinal, start, end, direction, provisional=not confirmed))
        ordinal += 1
    up_count = sum(1 for item in bi_list if item.get("direction") == "up")
    down_count = sum(1 for item in bi_list if item.get("direction") == "down")
    if not bi_list:
        warnings.append("有效分型不足或间隔不足，无法形成正式笔。")
    elif up_count == 0 or down_count == 0:
        warnings.append("当前只识别到单方向笔，结构可能尚未完整。")
    if len(bi_list) > 0 and len(effective) and len(bi_list) / max(len(effective), 1) > 0.8:
        warnings.append("笔数量过密，可能由分型噪音导致。")
    diagnostics = {
        "raw_bar_count": len(bars or normalized.get("bars") or []),
        "processed_bar_count": len(bars or normalized.get("bars") or []),
        "raw_fractal_count": normalized.get("normalized_fractal_count", 0),
        "effective_fractal_count": len(effective),
        "removed_fractal_count": len(selected.get("removed_fractals") or []),
        "rejected_bi_count": len(rejected),
        "bi_count": len(bi_list),
        "up_bi_count": up_count,
        "down_bi_count": down_count,
        "min_separation": int(min_separation),
        "warnings": warnings,
        "errors": [],
        "removed_fractals": selected.get("removed_fractals") or [],
    }
    return {
        "ok": True,
        "status": "ok" if bi_list else "no_bi",
        "bi_list": bi_list,
        "rejected_bi": rejected,
        "effective_fractals": effective,
        "diagnostics": diagnostics,
        "message": "" if bi_list else "有效分型不足，无法形成笔。",
        "disclaimer": DISCLAIMER,
    }


def _write_bi_report(payload: dict[str, Any], report_path: Path) -> None:
    diagnostics = payload.get("diagnostics") or {}
    input_summary = payload.get("input_summary") or {}
    lines = [
        "# 缠论笔识别学习报告",
        "",
        f"- 分析股票：{payload.get('symbol') or '-'}",
        f"- 周期：{payload.get('timeframe') or '-'}",
        f"- 数据范围：{input_summary.get('start_date') or '-'} 至 {input_summary.get('end_date') or '-'}",
        f"- 原始 K线数量：{input_summary.get('raw_bar_count', 0)}",
        f"- 处理后 K线数量：{input_summary.get('processed_bar_count', 0)}",
        f"- 分型数量：{input_summary.get('raw_fractal_count', diagnostics.get('raw_fractal_count', 0))}",
        f"- 有效分型数量：{input_summary.get('effective_fractal_count', diagnostics.get('effective_fractal_count', 0))}",
        f"- 笔数量：{diagnostics.get('bi_count', 0)}",
        f"- 上升笔数量：{diagnostics.get('up_bi_count', 0)}",
        f"- 下降笔数量：{diagnostics.get('down_bi_count', 0)}",
        f"- 被过滤候选笔：{diagnostics.get('rejected_bi_count', 0)}",
        "",
        "## 最小间隔规则说明",
        "",
        f"- 本版本采用可配置的简化最小间隔规则，当前 min_separation={diagnostics.get('min_separation', '-') }，仅用于结构学习。",
        "- 笔是结构观察，不是交易动作；上升笔不是买入信号，下降笔不是卖出信号。",
        "",
        "## 主要笔列表",
        "",
    ]
    for item in (payload.get("bi_list") or [])[:30]:
        lines.append(
            f"- {item.get('start_date') or item.get('start_datetime')} -> {item.get('end_date') or item.get('end_datetime')}: "
            f"{item.get('direction_label')} {item.get('start_price')} -> {item.get('end_price')} "
            f"change={item.get('price_change_pct')}% span={item.get('bar_count')}"
        )
    lines.extend(["", "## 被过滤候选笔", ""])
    rejected = payload.get("rejected_bi") or []
    if rejected:
        for row in rejected[:30]:
            lines.append(f"- {row.get('start_fractal_id')} -> {row.get('end_fractal_id')}: {row.get('rejected_reason')}")
    else:
        lines.append("- 暂无被过滤候选笔。")
    lines.extend(["", "## 诊断 warning", ""])
    warnings = diagnostics.get("warnings") or []
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
            "- 若笔结果稳定，可在后续版本继续构建线段识别。",
            "",
            "## 风险提示",
            "",
            "- 本报告仅用于缠论结构学习和纸面研究，不构成投资建议。",
            "- 本模块不接券商、不真实下单，不参与自动虚拟组合和交易计划。",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def _runtime_fractal_path(symbol: str, timeframe: str) -> Path:
    return PROJECT_ROOT / "data" / "chanlun" / "fractals" / f"{normalize_symbol(symbol)}_{timeframe}_fractals.json"


def run_chanlun_bi_analysis(
    symbol: Any,
    *,
    timeframe: str = "daily",
    start_date: str | None = None,
    end_date: str | None = None,
    input_path: str | Path | None = None,
    fractal_path: str | Path | None = None,
    mode: str = "strict",
    min_separation: int = 4,
    allow_provisional: bool = False,
    output_dir: str | Path = "results",
    report: bool = True,
    write: bool = True,
) -> dict[str, Any]:
    symbol_text = normalize_symbol(symbol)
    output_base = Path(output_dir)
    if not output_base.is_absolute():
        output_base = PROJECT_ROOT / output_base
    warnings: list[str] = []
    source = ""
    fractal_payload: dict[str, Any] = {}
    if fractal_path:
        target = Path(fractal_path)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        fractal_payload = _read_payload(target)
        source = str(target)
    if not fractal_payload:
        fractal_payload = run_chanlun_fractal_analysis(
            symbol_text,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            input_path=input_path,
            mode=mode,
            include_resolved=True,
            write=False,
        )
        source = fractal_payload.get("input_summary", {}).get("source", str(input_path or ""))
        warnings.append("未读取到既有分型文件，已基于 OHLC 临时生成分型。")
    bars = fractal_payload.get("processed_bars") or []
    bi_payload = build_bi_from_fractals(
        fractal_payload,
        bars=bars,
        min_separation=min_separation,
        strict=(mode != "loose"),
        allow_provisional=allow_provisional,
    )
    diagnostics = bi_payload.get("diagnostics") or {}
    warnings.extend(diagnostics.get("warnings") or [])
    input_summary = {
        "source": source,
        "raw_bar_count": fractal_payload.get("input_summary", {}).get("raw_bar_count", diagnostics.get("raw_bar_count", 0)),
        "processed_bar_count": fractal_payload.get("input_summary", {}).get("processed_bar_count", diagnostics.get("processed_bar_count", 0)),
        "raw_fractal_count": diagnostics.get("raw_fractal_count", 0),
        "effective_fractal_count": diagnostics.get("effective_fractal_count", 0),
        "start_date": start_date or fractal_payload.get("input_summary", {}).get("start_date", ""),
        "end_date": end_date or fractal_payload.get("input_summary", {}).get("end_date", ""),
    }
    latest_report_path = REPORTS_DIR / f"chanlun_bi_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "chanlun_bi_analysis",
        "symbol": symbol_text,
        "timeframe": timeframe,
        "generated_at": now_text(),
        "status": bi_payload.get("status", "no_bi"),
        "message": bi_payload.get("message", ""),
        "input_summary": input_summary,
        "effective_fractals": bi_payload.get("effective_fractals") or [],
        "bi_list": bi_payload.get("bi_list") or [],
        "rejected_bi": bi_payload.get("rejected_bi") or [],
        "diagnostics": {**diagnostics, "warning_count": len(warnings), "warnings": warnings},
        "report_path": str(latest_report_path) if report else "",
        "disclaimer": DISCLAIMER,
    }
    if write:
        output_base.mkdir(parents=True, exist_ok=True)
        CHANLUN_BI_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        write_json(output_base / "chanlun_bi_latest.json", payload)
        write_json(CHANLUN_BI_RUNTIME_DIR / f"{symbol_text}_{timeframe}_bi.json", payload)
        if report:
            _write_bi_report(payload, latest_report_path)
    return payload


def latest_chanlun_bi(path: Path | None = None) -> dict[str, Any]:
    target = path or CHANLUN_BI_LATEST_JSON
    payload = read_json(target, {})
    if isinstance(payload, dict) and payload:
        return payload
    return {"ok": True, "exists": False, "message": "暂无缠论笔识别结果。", "bi_list": [], "diagnostics": {}, "disclaimer": DISCLAIMER}


def chanlun_bi_status(path: Path | None = None) -> dict[str, Any]:
    target = path or CHANLUN_BI_LATEST_JSON
    payload = read_json(target, {})
    return {
        "ok": True,
        "exists": bool(isinstance(payload, dict) and payload),
        "path": str(target),
        "symbol": payload.get("symbol", "") if isinstance(payload, dict) else "",
        "timeframe": payload.get("timeframe", "") if isinstance(payload, dict) else "",
        "bi_count": len(payload.get("bi_list", [])) if isinstance(payload, dict) else 0,
        "diagnostics": payload.get("diagnostics", {}) if isinstance(payload, dict) else {},
        "disclaimer": DISCLAIMER,
    }


def latest_chanlun_bi_report_html() -> str:
    reports = sorted(REPORTS_DIR.glob("chanlun_bi_report_*.md"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not reports:
        return "<html><body><p>暂无缠论笔识别报告。</p></body></html>"
    text = reports[0].read_text(encoding="utf-8", errors="ignore")
    body = html.escape(text).replace("\n", "<br>\n")
    return f"<html><head><meta charset='utf-8'><title>缠论笔识别报告</title></head><body><pre>{body}</pre></body></html>"


__all__ = [
    "CHANLUN_BI_LATEST_JSON",
    "DISCLAIMER",
    "build_bi_from_fractals",
    "chanlun_bi_status",
    "latest_chanlun_bi",
    "latest_chanlun_bi_report_html",
    "normalize_fractals_for_bi",
    "run_chanlun_bi_analysis",
    "select_effective_fractals",
]
