from __future__ import annotations

import csv
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

from quantdesk.platform_common import PROJECT_ROOT, base_payload, clean_number, finalize_outputs, rel

DEFAULT_FEATURE_KEYS = [
    "return_1d",
    "intraday_amplitude",
    "volume_ratio_20d",
    "volatility_20d",
    "drawdown_20d",
    "trend_5d",
    "trend_20d",
]

DISCLAIMER = "Historical similarity does not imply future repetition. Research only."


def _resolve(path: str | Path | None) -> Path | None:
    if not path:
        return None
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _parse_horizons(value: Any) -> list[int]:
    if isinstance(value, list):
        raw = value
    elif value:
        raw = str(value).split(",")
    else:
        raw = [1, 5, 20]
    out: list[int] = []
    for item in raw:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out or [1, 5, 20]


def _synthetic_market_rows() -> list[dict[str, Any]]:
    start = date(2023, 1, 3)
    rows: list[dict[str, Any]] = []
    close = 100.0
    for idx in range(90):
        shock = math.sin(idx / 5.0) * 0.006 + (0.018 if idx in {30, 63} else 0.0) - (0.015 if idx in {45, 72} else 0.0)
        open_price = close * (1 + shock / 3)
        close = max(10.0, close * (1 + shock))
        high = max(open_price, close) * (1 + 0.006 + (idx % 3) * 0.001)
        low = min(open_price, close) * (1 - 0.006 - (idx % 4) * 0.001)
        rows.append({
            "date": (start + timedelta(days=idx)).isoformat(),
            "open": round(open_price, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": 1000000 + idx * 3500 + (idx % 7) * 25000,
            "amount": round(close * (1000000 + idx * 3500), 2),
        })
    return rows


def load_market_history(path: str | Path | None = None, *, config: dict | None = None) -> tuple[list[dict], list[str]]:
    cfg = dict(config or {})
    warnings: list[str] = []
    candidates = [
        path or cfg.get("index_csv"),
        cfg.get("similar_day", {}).get("index_csv") if isinstance(cfg.get("similar_day"), dict) else None,
        "data/index/000300.SH.csv",
        "data/index/000905.SH.csv",
        "data/index/399300.SZ.csv",
        "data/market/index_daily.csv",
    ]
    chosen: Path | None = None
    for candidate in candidates:
        target = _resolve(candidate)
        if target and target.exists():
            chosen = target
            break
    if not chosen:
        warnings.append("index_csv_missing")
        if cfg.get("synthetic_test_data") or cfg.get("synthetic_when_missing"):
            return _synthetic_market_rows(), warnings + ["synthetic_market_history_used"]
        return [], warnings
    try:
        with chosen.open("r", encoding="utf-8-sig", newline="") as handle:
            raw_rows = list(csv.DictReader(handle))
    except Exception as exc:
        warnings.append(f"index_csv_invalid:{type(exc).__name__}")
        return [], warnings
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        trade_date = raw.get("date") or raw.get("trade_date") or raw.get("datetime")
        close = _float(raw.get("close"))
        if not trade_date or close is None:
            continue
        row = {
            "date": str(trade_date).strip(),
            "open": _float(raw.get("open")),
            "high": _float(raw.get("high")),
            "low": _float(raw.get("low")),
            "close": close,
            "volume": _float(raw.get("volume") or raw.get("vol")),
            "amount": _float(raw.get("amount") or raw.get("turnover")),
        }
        rows.append(row)
    rows.sort(key=lambda item: str(item.get("date")))
    if len(rows) < 25:
        warnings.append("market_history_insufficient_rows")
    return rows, warnings


def compute_market_day_features(
    rows: list[dict],
    *,
    sector_payload: dict | None = None,
    lookback_volatility: int = 20,
    lookback_volume: int = 20,
) -> list[dict]:
    features: list[dict[str, Any]] = []
    closes = [_float(row.get("close")) for row in rows]
    volumes = [_float(row.get("volume")) for row in rows]
    sector_items = []
    if isinstance(sector_payload, dict):
        sector_items = sector_payload.get("sectors") or sector_payload.get("items") or []
    heat_values = [clean_number(item.get("heat_score"), 0.0) for item in sector_items if isinstance(item, dict)]
    for idx, row in enumerate(rows):
        close = closes[idx]
        if close is None:
            continue
        prev_close = closes[idx - 1] if idx > 0 else None
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        volume = volumes[idx]
        out: dict[str, Any] = {"date": row.get("date"), "close": close}
        out["return_1d"] = (close / prev_close - 1) if prev_close else None
        if high is not None and low not in (None, 0):
            out["intraday_amplitude"] = high / low - 1
            out["close_position"] = (close - low) / (high - low) if high != low else 0.5
        else:
            out["intraday_amplitude"] = None
            out["close_position"] = None
        vol_window = [v for v in volumes[max(0, idx - lookback_volume):idx] if v not in (None, 0)]
        out["volume_ratio_20d"] = volume / statistics.mean(vol_window) if volume is not None and vol_window else None
        return_window = [
            (closes[j] / closes[j - 1] - 1)
            for j in range(max(1, idx - lookback_volatility + 1), idx + 1)
            if closes[j] is not None and closes[j - 1] not in (None, 0)
        ]
        out["volatility_20d"] = statistics.pstdev(return_window) if len(return_window) >= 2 else None
        close_window = [v for v in closes[max(0, idx - 19):idx + 1] if v is not None]
        out["drawdown_20d"] = close / max(close_window) - 1 if close_window else None
        out["trend_5d"] = close / closes[idx - 5] - 1 if idx >= 5 and closes[idx - 5] not in (None, 0) else None
        out["trend_20d"] = close / closes[idx - 20] - 1 if idx >= 20 and closes[idx - 20] not in (None, 0) else None
        if heat_values:
            out["sector_dispersion"] = statistics.pstdev(heat_values) if len(heat_values) > 1 else 0.0
            out["top_sector_heat"] = max(heat_values)
            out["breadth_proxy"] = sum(1 for value in heat_values if value > 50) / len(heat_values)
        features.append(out)
    return features


def build_target_day_feature(
    rows: list[dict],
    target_date: str | None = None,
    *,
    sector_payload: dict | None = None,
) -> tuple[dict | None, list[str]]:
    warnings: list[str] = []
    feature_rows = compute_market_day_features(rows, sector_payload=sector_payload)
    if not feature_rows:
        return None, ["target_feature_missing"]
    if target_date and target_date != "latest":
        for row in feature_rows:
            if str(row.get("date")) == target_date:
                return row, warnings
        return None, [f"target_date_not_found:{target_date}"]
    return feature_rows[-1], warnings


def _feature_stats(rows: list[dict], keys: list[str]) -> dict[str, tuple[float, float]]:
    stats: dict[str, tuple[float, float]] = {}
    for key in keys:
        values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
        if values:
            mean = statistics.mean(values)
            stdev = statistics.pstdev(values) or 1.0
            stats[key] = (mean, stdev)
    return stats


def cosine_similarity(a: dict, b: dict, feature_keys: list[str]) -> float:
    keys = [key for key in feature_keys if isinstance(a.get(key), (int, float)) and isinstance(b.get(key), (int, float))]
    if not keys:
        return 0.0
    dot = sum(float(a[key]) * float(b[key]) for key in keys)
    norm_a = math.sqrt(sum(float(a[key]) ** 2 for key in keys))
    norm_b = math.sqrt(sum(float(b[key]) ** 2 for key in keys))
    if not norm_a or not norm_b:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def find_similar_days(
    feature_rows: list[dict],
    target_feature: dict,
    *,
    top_k: int = 5,
    exclude_recent_days: int = 5,
    feature_keys: list[str] | None = None,
) -> list[dict]:
    keys = feature_keys or DEFAULT_FEATURE_KEYS
    stats = _feature_stats(feature_rows, keys)

    def normalized(row: dict) -> dict[str, float]:
        return {
            key: (float(row[key]) - stats[key][0]) / stats[key][1]
            for key in keys
            if key in stats and isinstance(row.get(key), (int, float))
        }

    target_norm = normalized(target_feature)
    candidates = feature_rows[:-max(0, exclude_recent_days)] if exclude_recent_days else list(feature_rows)
    scored: list[dict[str, Any]] = []
    for row in candidates:
        if row.get("date") == target_feature.get("date"):
            continue
        row_norm = normalized(row)
        shared = [key for key in keys if key in target_norm and key in row_norm]
        if len(shared) < max(2, min(4, len(keys))):
            continue
        scored.append({
            "date": row.get("date"),
            "similarity": round(cosine_similarity(target_norm, row_norm, shared), 6),
            "features": {key: row.get(key) for key in keys if key in row},
        })
    scored.sort(key=lambda item: item["similarity"], reverse=True)
    return scored[: max(1, int(top_k or 5))]


def compute_forward_returns(
    rows: list[dict],
    similar_days: list[dict],
    *,
    horizons: Sequence[int] = (1, 5, 20),
) -> list[dict]:
    by_date = {str(row.get("date")): idx for idx, row in enumerate(rows)}
    closes = [_float(row.get("close")) for row in rows]
    enriched: list[dict] = []
    for item in similar_days:
        row = dict(item)
        idx = by_date.get(str(item.get("date")))
        base_close = closes[idx] if idx is not None else None
        fwd: dict[str, float | None] = {}
        for horizon in horizons:
            value = None
            if idx is not None and base_close not in (None, 0) and idx + horizon < len(closes) and closes[idx + horizon] is not None:
                value = round(float(closes[idx + horizon]) / float(base_close) - 1, 6)
            fwd[f"{horizon}d"] = value
        next_window = [v for v in closes[(idx or 0) + 1:(idx or 0) + 21] if v is not None] if idx is not None and base_close else []
        row["forward_returns"] = fwd
        row["max_drawdown_next_20d"] = round(min((v / base_close - 1 for v in next_window), default=0.0), 6) if base_close else None
        row["max_gain_next_20d"] = round(max((v / base_close - 1 for v in next_window), default=0.0), 6) if base_close else None
        enriched.append(row)
    return enriched


def _summary_stats(similar_days: list[dict]) -> dict[str, Any]:
    values = [item.get("forward_returns", {}).get("5d") for item in similar_days]
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return {
        "avg_forward_5d": round(statistics.mean(nums), 6) if nums else None,
        "median_forward_5d": round(statistics.median(nums), 6) if nums else None,
        "positive_ratio_5d": round(sum(1 for v in nums if v > 0) / len(nums), 6) if nums else None,
    }


def build_similar_day_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    rows, warnings = load_market_history(cfg.get("index_csv"), config=cfg)
    target, target_warnings = build_target_day_feature(rows, cfg.get("target_date") or cfg.get("date") or "latest")
    warnings.extend(target_warnings)
    feature_rows = compute_market_day_features(rows)
    horizons = _parse_horizons(cfg.get("horizons"))
    similar_days: list[dict] = []
    status = "passed"
    if not rows or not target:
        status = "warning"
    else:
        similar_days = find_similar_days(
            feature_rows,
            target,
            top_k=int(cfg.get("top_k") or cfg.get("top") or 5),
            exclude_recent_days=int(cfg.get("exclude_recent_days") or 5),
            feature_keys=cfg.get("feature_keys") or DEFAULT_FEATURE_KEYS,
        )
        similar_days = compute_forward_returns(rows, similar_days, horizons=horizons)
        if not similar_days:
            warnings.append("similar_days_empty")
            status = "warning"
    payload = base_payload("similar_day", status=status, warnings=warnings)
    payload.update({
        "disclaimer": DISCLAIMER,
        "synthetic_test_data": bool(cfg.get("synthetic_test_data")),
        "target_date": target.get("date") if target else "",
        "index_symbol": cfg.get("index_symbol") or "000300.SH",
        "input_status": {"row_count": len(rows), "latest_source": rel(_resolve(cfg.get("index_csv")) or Path("data/index/000300.SH.csv"))},
        "feature_keys": cfg.get("feature_keys") or DEFAULT_FEATURE_KEYS,
        "similar_days": similar_days,
        "items": similar_days,
        "similar_day_count": len(similar_days),
        "summary_stats": _summary_stats(similar_days),
    })
    return finalize_outputs(payload, "similar_day", render_similar_day_markdown(payload))


def render_similar_day_markdown(payload: dict) -> str:
    lines = [
        "# Similar Day",
        "",
        f"- status: `{payload.get('status')}`",
        f"- target_date: `{payload.get('target_date')}`",
        f"- index_symbol: `{payload.get('index_symbol')}`",
        "",
        "| date | similarity | forward_1d | forward_5d | forward_20d |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for item in payload.get("similar_days", [])[:20]:
        fwd = item.get("forward_returns") or {}
        lines.append(f"| {item.get('date')} | {item.get('similarity')} | {fwd.get('1d')} | {fwd.get('5d')} | {fwd.get('20d')} |")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])]])
    lines.extend(["", payload.get("disclaimer") or DISCLAIMER])
    return "\n".join(lines)
