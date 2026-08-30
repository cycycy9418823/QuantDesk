"""Chanlun segment recognition engine.

This module implements only the third programmable Chanlun layer: building
segment observations from confirmed bi observations.  It does not identify
zhongshu, divergence, buy/sell points, or trading actions.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.chanlun_bi_engine import PROJECT_ROOT, REPORTS_DIR, run_chanlun_bi_analysis
from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, safe_int, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

RESULTS_DIR = PROJECT_ROOT / "results"
CHANLUN_SEGMENT_RUNTIME_DIR = PROJECT_ROOT / "data" / "chanlun" / "segments"
CHANLUN_SEGMENT_LATEST_JSON = RESULTS_DIR / "chanlun_segment_latest.json"
DISCLAIMER = "缠论线段识别仅用于结构学习与纸面研究，不构成投资建议。"


def _read_payload(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    payload = read_json(target, {})
    return payload if isinstance(payload, dict) else {}


def _bi_key(row: dict[str, Any]) -> int:
    return safe_int(row.get("start_bar_index"), safe_int(row.get("bar_index"), 0)) or 0


def normalize_bi_for_segment(bi_list: Any, bars: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Normalize v3.0.3 bi payloads for segment recognition."""

    payload = bi_list if isinstance(bi_list, dict) else None
    rows = payload.get("bi_list") if isinstance(payload, dict) else bi_list
    bars_payload = bars if bars is not None else payload.get("processed_bars") if isinstance(payload, dict) else []
    warnings: list[str] = []
    normalized: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        rows = []
    for idx, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        direction = str(raw.get("direction") or "").strip().lower()
        if direction not in {"up", "down"}:
            warnings.append(f"忽略未知笔方向：{raw.get('direction')}")
            continue
        payload_symbol = payload.get("symbol") if isinstance(payload, dict) else ""
        payload_timeframe = payload.get("timeframe") if isinstance(payload, dict) else "daily"
        symbol = normalize_symbol(raw.get("symbol") or payload_symbol)
        timeframe = str(raw.get("timeframe") or payload_timeframe or "daily")
        start_price = safe_float(raw.get("start_price"), None)
        end_price = safe_float(raw.get("end_price"), None)
        if start_price is None or end_price is None:
            warnings.append(f"忽略缺少起止价格的笔：{raw.get('bi_id') or idx}")
            continue
        normalized.append(
            {
                **raw,
                "bi_id": raw.get("bi_id") or f"{symbol}_{timeframe}_bi_{idx:04d}",
                "symbol": symbol,
                "timeframe": timeframe,
                "direction": direction,
                "start_bar_index": safe_int(raw.get("start_bar_index"), idx) or idx,
                "end_bar_index": safe_int(raw.get("end_bar_index"), safe_int(raw.get("start_bar_index"), idx) or idx) or idx,
                "start_price": float(start_price),
                "end_price": float(end_price),
                "confirmed": bool(raw.get("confirmed", True)),
                "provisional": bool(raw.get("provisional", False)),
            }
        )
    normalized = sorted(normalized, key=lambda item: (_bi_key(item), safe_int(item.get("end_bar_index"), 0) or 0))
    return {
        "ok": True,
        "status": "ok" if normalized else "no_bi",
        "bi_list": normalized,
        "bars": bars_payload or [],
        "raw_bi_count": len(rows),
        "normalized_bi_count": len(normalized),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def _is_better_same_direction(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    direction = candidate.get("direction")
    cand_end = safe_float(candidate.get("end_price"), 0) or 0
    cur_end = safe_float(current.get("end_price"), 0) or 0
    if direction == "up":
        if cand_end != cur_end:
            return cand_end > cur_end
    else:
        if cand_end != cur_end:
            return cand_end < cur_end
    return (safe_int(candidate.get("end_bar_index"), 0) or 0) >= (safe_int(current.get("end_bar_index"), 0) or 0)


def select_effective_bi(
    bi_list: Any,
    *,
    require_confirmed: bool = True,
    min_bi_count: int = 3,
    allow_provisional: bool = False,
) -> dict[str, Any]:
    normalized = normalize_bi_for_segment(bi_list)
    rows = normalized.get("bi_list") or []
    warnings = list(normalized.get("warnings") or [])
    effective: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for row in rows:
        if require_confirmed and not row.get("confirmed", True):
            if allow_provisional:
                row = {**row, "provisional": True}
            else:
                removed.append({**row, "removed_reason": "unconfirmed_bi"})
                continue
        if effective and effective[-1].get("direction") == row.get("direction"):
            last = effective[-1]
            if _is_better_same_direction(row, last):
                removed.append({**last, "removed_reason": "same_direction_replaced_by_more_extreme"})
                effective[-1] = row
                warnings.append("连续同方向笔已合并，保留更极端的一笔。")
            else:
                removed.append({**row, "removed_reason": "same_direction_less_extreme"})
                warnings.append("连续同方向笔已合并，移除较弱的一笔。")
            continue
        effective.append(row)
    diagnostics = {
        "raw_bi_count": len(rows),
        "effective_bi_count": len(effective),
        "removed_bi_count": len(removed),
        "removed_bi": removed,
        "min_bi_count": int(min_bi_count),
        "allow_provisional": bool(allow_provisional),
        "warnings": warnings,
    }
    return {
        "ok": True,
        "status": "ok" if len(effective) >= int(min_bi_count) else "no_segment",
        "effective_bi": effective,
        "removed_bi": removed,
        "diagnostics": diagnostics,
        "disclaimer": DISCLAIMER,
    }


def _segment_prices(group: list[dict[str, Any]], direction: str) -> tuple[float, float, float, float]:
    start_price = safe_float(group[0].get("start_price"), 0) or 0
    end_price = safe_float(group[-1].get("end_price"), 0) or 0
    prices: list[float] = []
    for item in group:
        prices.extend([safe_float(item.get("start_price"), 0) or 0, safe_float(item.get("end_price"), 0) or 0])
    high = max(prices) if prices else max(start_price, end_price)
    low = min(prices) if prices else min(start_price, end_price)
    return start_price, end_price, high, low


def _check_break(segment: dict[str, Any], subsequent: list[dict[str, Any]]) -> dict[str, Any]:
    if not subsequent:
        return {"break_status": "unknown", "break_type": None, "break_date": None, "break_price": None}
    if segment.get("direction") == "up":
        key_low = safe_float(segment.get("low"), 0) or 0
        for item in subsequent:
            if item.get("direction") == "down" and (safe_float(item.get("end_price"), 0) or 0) < key_low:
                return {"break_status": "broken", "break_type": "down_break", "break_date": item.get("end_date") or item.get("end_datetime"), "break_price": item.get("end_price")}
    if segment.get("direction") == "down":
        key_high = safe_float(segment.get("high"), 0) or 0
        for item in subsequent:
            if item.get("direction") == "up" and (safe_float(item.get("end_price"), 0) or 0) > key_high:
                return {"break_status": "broken", "break_type": "up_break", "break_date": item.get("end_date") or item.get("end_datetime"), "break_price": item.get("end_price")}
    return {"break_status": "intact", "break_type": None, "break_date": None, "break_price": None}


def _make_segment(symbol: str, timeframe: str, ordinal: int, group: list[dict[str, Any]], subsequent: list[dict[str, Any]], *, break_check: bool) -> dict[str, Any]:
    direction = str(group[0].get("direction") or "")
    start_price, end_price, high, low = _segment_prices(group, direction)
    price_change = end_price - start_price
    pct = (price_change / start_price * 100) if start_price else 0.0
    if direction == "down" and price_change > 0:
        price_change = -abs(price_change)
        pct = (price_change / start_price * 100) if start_price else 0.0
    if direction == "up" and price_change < 0:
        price_change = abs(price_change)
        pct = (price_change / start_price * 100) if start_price else 0.0
    provisional = any(bool(item.get("provisional")) or not bool(item.get("confirmed", True)) for item in group)
    reliability = "low" if provisional else "high"
    break_payload = _check_break({"direction": direction, "high": high, "low": low}, subsequent) if break_check else {"break_status": "unknown", "break_type": None, "break_date": None, "break_price": None}
    label = "上升线段观察" if direction == "up" else "下降线段观察"
    slope = price_change / max(len(group), 1)
    return {
        "segment_id": f"{symbol}_{timeframe}_segment_{ordinal:04d}",
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "direction_label": label,
        "start_date": group[0].get("start_date") or "",
        "end_date": group[-1].get("end_date") or "",
        "start_datetime": group[0].get("start_datetime"),
        "end_datetime": group[-1].get("end_datetime"),
        "start_bar_index": group[0].get("start_bar_index"),
        "end_bar_index": group[-1].get("end_bar_index"),
        "start_price": round(start_price, 6),
        "end_price": round(end_price, 6),
        "high": round(high, 6),
        "low": round(low, 6),
        "price_change": round(price_change, 6),
        "price_change_pct": round(pct, 4),
        "bi_count": len(group),
        "bi_ids": [item.get("bi_id") for item in group],
        "confirmed": not provisional,
        "provisional": provisional,
        **break_payload,
        "reliability": reliability,
        "strength": {
            "absolute_change": round(abs(price_change), 6),
            "percent_change": round(abs(pct), 4),
            "bi_count": len(group),
            "slope": round(slope, 6),
        },
        "notes": f"{label}，仅作为结构学习信号。",
        "disclaimer": DISCLAIMER,
    }


def build_segments_from_bi(
    bi_list: Any,
    bars: list[dict[str, Any]] | None = None,
    *,
    min_bi_count: int = 3,
    require_confirmed: bool = True,
    allow_provisional: bool = False,
    break_check: bool = True,
) -> dict[str, Any]:
    normalized = normalize_bi_for_segment(bi_list, bars=bars)
    selected = select_effective_bi(normalized.get("bi_list") or [], require_confirmed=require_confirmed, min_bi_count=min_bi_count, allow_provisional=allow_provisional)
    effective = selected.get("effective_bi") or []
    warnings = list(normalized.get("warnings") or []) + list(selected.get("diagnostics", {}).get("warnings") or [])
    rejected: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    symbol = normalize_symbol((effective[0].get("symbol") if effective else "") or "")
    timeframe = str((effective[0].get("timeframe") if effective else "") or "daily")
    if len(effective) < int(min_bi_count):
        warnings.append("有效笔数量不足，无法形成线段。")
    else:
        ordinal = 1
        step = max(1, int(min_bi_count) - 1)
        for start in range(0, len(effective) - int(min_bi_count) + 1, step):
            group = effective[start : start + int(min_bi_count)]
            directions = [item.get("direction") for item in group]
            if any(directions[i] == directions[i - 1] for i in range(1, len(directions))):
                rejected.append({"bi_ids": [item.get("bi_id") for item in group], "rejected_reason": "direction_not_alternating"})
                continue
            segment = _make_segment(symbol, timeframe, ordinal, group, effective[start + int(min_bi_count) :], break_check=break_check)
            if segment["direction"] == "up" and segment["price_change"] <= 0:
                rejected.append({"bi_ids": segment.get("bi_ids"), "rejected_reason": "up_segment_price_not_rising"})
                continue
            if segment["direction"] == "down" and segment["price_change"] >= 0:
                rejected.append({"bi_ids": segment.get("bi_ids"), "rejected_reason": "down_segment_price_not_falling"})
                continue
            segments.append(segment)
            ordinal += 1
    up_count = sum(1 for item in segments if item.get("direction") == "up")
    down_count = sum(1 for item in segments if item.get("direction") == "down")
    if segments and (up_count == 0 or down_count == 0):
        warnings.append("当前只识别到单方向线段，走势结构可能尚未完整。")
    if len(segments) > 0 and len(segments) / max(len(effective), 1) > 0.7:
        warnings.append("线段数量过密，可能由笔识别噪音导致。")
    diagnostics = {
        "raw_bar_count": len(bars or normalized.get("bars") or []),
        "processed_bar_count": len(bars or normalized.get("bars") or []),
        "raw_bi_count": normalized.get("normalized_bi_count", 0),
        "effective_bi_count": len(effective),
        "removed_bi_count": len(selected.get("removed_bi") or []),
        "rejected_segment_count": len(rejected),
        "segment_count": len(segments),
        "up_segment_count": up_count,
        "down_segment_count": down_count,
        "min_bi_count": int(min_bi_count),
        "allow_provisional": bool(allow_provisional),
        "warnings": warnings,
        "errors": [],
        "removed_bi": selected.get("removed_bi") or [],
    }
    return {
        "ok": True,
        "status": "ok" if segments else "no_segment",
        "segments": segments,
        "rejected_segments": rejected,
        "effective_bi": effective,
        "diagnostics": diagnostics,
        "message": "" if segments else "有效笔数量不足，无法形成线段。",
        "disclaimer": DISCLAIMER,
    }


def _write_segment_report(payload: dict[str, Any], report_path: Path) -> None:
    diagnostics = payload.get("diagnostics") or {}
    input_summary = payload.get("input_summary") or {}
    lines = [
        "# 缠论线段识别学习报告",
        "",
        f"- 分析股票：{payload.get('symbol') or '-'}",
        f"- 周期：{payload.get('timeframe') or '-'}",
        f"- 数据范围：{input_summary.get('start_date') or '-'} 至 {input_summary.get('end_date') or '-'}",
        f"- 原始 K线数量：{input_summary.get('raw_bar_count', 0)}",
        f"- 处理后 K线数量：{input_summary.get('processed_bar_count', 0)}",
        f"- 笔数量：{input_summary.get('raw_bi_count', diagnostics.get('raw_bi_count', 0))}",
        f"- 有效笔数量：{input_summary.get('effective_bi_count', diagnostics.get('effective_bi_count', 0))}",
        f"- 线段数量：{diagnostics.get('segment_count', 0)}",
        f"- 上升线段数量：{diagnostics.get('up_segment_count', 0)}",
        f"- 下降线段数量：{diagnostics.get('down_segment_count', 0)}",
        f"- 被过滤候选线段：{diagnostics.get('rejected_segment_count', 0)}",
        "",
        "## 最小笔数量规则说明",
        "",
        f"- 本版本采用简化线段识别规则，当前 min_bi_count={diagnostics.get('min_bi_count', '-')}，仅用于结构学习，后续版本可继续细化不同流派的线段划分标准。",
        "- 线段是结构观察，不是交易动作；线段破坏观察不是买卖信号。",
        "",
        "## 主要线段列表",
        "",
    ]
    for item in (payload.get("segments") or [])[:30]:
        lines.append(
            f"- {item.get('start_date') or item.get('start_datetime')} -> {item.get('end_date') or item.get('end_datetime')}: "
            f"{item.get('direction_label')} {item.get('start_price')} -> {item.get('end_price')} "
            f"change={item.get('price_change_pct')}% bi_count={item.get('bi_count')} break={item.get('break_status')}"
        )
    lines.extend(["", "## 线段破坏观察", ""])
    for item in (payload.get("segments") or [])[:30]:
        lines.append(f"- {item.get('segment_id')}: {item.get('break_status')} {item.get('break_type') or ''} {item.get('break_date') or ''} {item.get('break_price') or ''}")
    lines.extend(["", "## 被过滤候选线段", ""])
    rejected = payload.get("rejected_segments") or []
    if rejected:
        for row in rejected[:30]:
            lines.append(f"- {','.join(str(x) for x in row.get('bi_ids') or [])}: {row.get('rejected_reason')}")
    else:
        lines.append("- 暂无被过滤候选线段。")
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
            "- 若线段结果稳定，可在后续版本继续构建中枢识别。",
            "",
            "## 风险提示",
            "",
            "- 本报告仅用于缠论结构学习和纸面研究，不构成投资建议。",
            "- 本模块不接券商、不真实下单，不参与自动虚拟组合和交易计划。",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_chanlun_segment_analysis(
    symbol: Any,
    *,
    timeframe: str = "daily",
    start_date: str | None = None,
    end_date: str | None = None,
    input_path: str | Path | None = None,
    bi_path: str | Path | None = None,
    fractal_path: str | Path | None = None,
    mode: str = "strict",
    min_separation: int = 4,
    min_bi_count: int = 3,
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
    bi_payload: dict[str, Any] = {}
    if bi_path:
        target = Path(bi_path)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        bi_payload = _read_payload(target)
        source = str(target)
    if not bi_payload:
        bi_payload = run_chanlun_bi_analysis(
            symbol_text,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            input_path=input_path,
            fractal_path=fractal_path,
            mode=mode,
            min_separation=min_separation,
            allow_provisional=allow_provisional,
            write=False,
        )
        source = bi_payload.get("input_summary", {}).get("source", str(input_path or ""))
        warnings.append("未读取到既有笔文件，已基于 OHLC/分型临时生成笔。")
    segment_payload = build_segments_from_bi(
        bi_payload,
        min_bi_count=min_bi_count,
        require_confirmed=True,
        allow_provisional=allow_provisional,
        break_check=True,
    )
    diagnostics = segment_payload.get("diagnostics") or {}
    warnings.extend(diagnostics.get("warnings") or [])
    input_summary = {
        "source": source,
        "raw_bar_count": bi_payload.get("input_summary", {}).get("raw_bar_count", diagnostics.get("raw_bar_count", 0)),
        "processed_bar_count": bi_payload.get("input_summary", {}).get("processed_bar_count", diagnostics.get("processed_bar_count", 0)),
        "raw_bi_count": diagnostics.get("raw_bi_count", 0),
        "effective_bi_count": diagnostics.get("effective_bi_count", 0),
        "start_date": start_date or bi_payload.get("input_summary", {}).get("start_date", ""),
        "end_date": end_date or bi_payload.get("input_summary", {}).get("end_date", ""),
    }
    latest_report_path = REPORTS_DIR / f"chanlun_segment_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "chanlun_segment_analysis",
        "symbol": symbol_text,
        "timeframe": timeframe,
        "generated_at": now_text(),
        "status": segment_payload.get("status", "no_segment"),
        "message": segment_payload.get("message", ""),
        "input_summary": input_summary,
        "effective_bi": segment_payload.get("effective_bi") or [],
        "segments": segment_payload.get("segments") or [],
        "rejected_segments": segment_payload.get("rejected_segments") or [],
        "diagnostics": {**diagnostics, "warning_count": len(warnings), "warnings": warnings},
        "report_path": str(latest_report_path) if report else "",
        "disclaimer": DISCLAIMER,
    }
    if write:
        output_base.mkdir(parents=True, exist_ok=True)
        CHANLUN_SEGMENT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        write_json(output_base / "chanlun_segment_latest.json", payload)
        write_json(CHANLUN_SEGMENT_RUNTIME_DIR / f"{symbol_text}_{timeframe}_segments.json", payload)
        if report:
            _write_segment_report(payload, latest_report_path)
    return payload


def latest_chanlun_segments(path: Path | None = None) -> dict[str, Any]:
    target = path or CHANLUN_SEGMENT_LATEST_JSON
    payload = read_json(target, {})
    if isinstance(payload, dict) and payload:
        return payload
    return {"ok": True, "exists": False, "message": "暂无缠论线段识别结果。", "segments": [], "diagnostics": {}, "disclaimer": DISCLAIMER}


def chanlun_segment_status(path: Path | None = None) -> dict[str, Any]:
    target = path or CHANLUN_SEGMENT_LATEST_JSON
    payload = read_json(target, {})
    return {
        "ok": True,
        "exists": bool(isinstance(payload, dict) and payload),
        "path": str(target),
        "symbol": payload.get("symbol", "") if isinstance(payload, dict) else "",
        "timeframe": payload.get("timeframe", "") if isinstance(payload, dict) else "",
        "segment_count": len(payload.get("segments", [])) if isinstance(payload, dict) else 0,
        "diagnostics": payload.get("diagnostics", {}) if isinstance(payload, dict) else {},
        "disclaimer": DISCLAIMER,
    }


def latest_chanlun_segment_report_html() -> str:
    reports = sorted(REPORTS_DIR.glob("chanlun_segment_report_*.md"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not reports:
        return "<html><body><p>暂无缠论线段识别报告。</p></body></html>"
    text = reports[0].read_text(encoding="utf-8", errors="ignore")
    body = html.escape(text).replace("\n", "<br>\n")
    return f"<html><head><meta charset='utf-8'><title>缠论线段识别报告</title></head><body><pre>{body}</pre></body></html>"


__all__ = [
    "CHANLUN_SEGMENT_LATEST_JSON",
    "DISCLAIMER",
    "build_segments_from_bi",
    "chanlun_segment_status",
    "latest_chanlun_segment_report_html",
    "latest_chanlun_segments",
    "normalize_bi_for_segment",
    "run_chanlun_segment_analysis",
    "select_effective_bi",
]
