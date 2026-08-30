from __future__ import annotations

import math
from typing import Any

DISCLAIMER = "Intraday provisional score based on partial_daily_snapshot. Research-only, not investment advice."


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def score_intraday_row(partial_row: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    row = dict(partial_row or {})
    warnings: list[str] = []
    last_price = _safe_float(row.get("last_price"), 0.0) or 0.0
    high = _safe_float(row.get("high_so_far"), None)
    low = _safe_float(row.get("low_so_far"), None)
    prev_close = _safe_float(row.get("prev_close"), None)
    volume = _safe_float(row.get("volume_so_far"), 0.0) or 0.0
    change_pct = _safe_float(row.get("change_pct"), None)
    if prev_close in (None, 0):
        warnings.append("missing_prev_close: change_pct uses provided value or 0")
        if change_pct is None:
            change_pct = 0.0
    elif change_pct is None:
        change_pct = (last_price / prev_close - 1.0) * 100.0
    if high is None or low is None or high <= low:
        warnings.append("missing_or_invalid_high_low: range_position downgraded")
        high = max(last_price, high or last_price)
        low = min(last_price, low or last_price)
    intraday_momentum = _clamp(50.0 + float(change_pct or 0.0) * 4.0)
    range_position = 50.0
    if high > low:
        range_position = _clamp((last_price - low) / (high - low) * 100.0)
    volume_activity = _clamp(math.log10(max(volume, 0.0) + 1.0) * 8.0)
    gap_or_change = _clamp(50.0 + float(change_pct or 0.0) * 3.0)
    components = {
        "intraday_momentum": round(intraday_momentum * 0.25, 4),
        "range_position": round(range_position * 0.35, 4),
        "volume_activity": round(volume_activity * 0.20, 4),
        "gap_or_change": round(gap_or_change * 0.20, 4),
    }
    score = _clamp(sum(float(value) for value in components.values()))
    return _json_safe({
        "symbol": str(row.get("symbol") or ""),
        "score": round(score, 4),
        "score_basis": "partial_daily_snapshot",
        "snapshot_time": row.get("snapshot_time") or "",
        "is_provisional": True,
        "inputs": {
            "last_price": last_price,
            "high_so_far": high,
            "low_so_far": low,
            "volume_so_far": volume,
            "prev_close": prev_close,
            "change_pct": change_pct,
        },
        "components": components,
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    })


def score_intraday_snapshot(rows: list[dict[str, Any]], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    scores = [score_intraday_row(row, config=config) for row in rows]
    warnings: list[str] = []
    for item in scores:
        warnings.extend(str(w) for w in item.get("warnings", []) if w)
    return {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "score_basis": "partial_daily_snapshot",
        "is_provisional": True,
        "score_count": len(scores),
        "scores": scores,
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }
