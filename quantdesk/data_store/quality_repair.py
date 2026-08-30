from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .repositories import DailyBarRepository, DataQualityRepository
from .sqlite_store import QuantDeskDB


def detect_missing_dates(symbol: str, adjustment: str = "forward", db_path: str = "data/quantdesk.db") -> list[str]:
    db = QuantDeskDB(db_path)
    db.initialize()
    rows = DailyBarRepository(db).query_daily_bars(symbol, adjustment=adjustment)
    db.close()
    return [] if rows else ["no_daily_rows"]


def detect_price_anomalies(threshold: float = 0.2, db_path: str = "data/quantdesk.db") -> list[dict]:
    db = QuantDeskDB(db_path)
    db.initialize()
    rows = db.execute("SELECT symbol, date, close FROM daily_bars_forward_adj ORDER BY symbol, date")
    issues: list[dict] = []
    prev: dict[str, float] = {}
    for row in rows:
        close = row.get("close")
        if close is None:
            continue
        old = prev.get(row["symbol"])
        if old and abs(close / old - 1) > threshold:
            issues.append({"symbol": row["symbol"], "table_name": "daily_bars_forward_adj", "date": row["date"], "issue_type": "price_jump", "severity": "warning", "message": "large close-to-close move", "source": "data_quality"})
        prev[row["symbol"]] = close
    db.close()
    return issues


def detect_volume_anomalies(z_threshold: float = 5, db_path: str = "data/quantdesk.db") -> list[dict]:
    """Flag per-symbol volume outliers (|z| >= threshold) and suspect zero-volume
    rows on otherwise-active series. Returns [] when the store has no data."""
    db = QuantDeskDB(db_path)
    db.initialize()
    try:
        rows = db.execute("SELECT symbol, date, volume FROM daily_bars_forward_adj ORDER BY symbol, date")
    except Exception:
        db.close()
        return []
    series: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        vol = row.get("volume")
        if vol is None:
            continue
        try:
            series[row["symbol"]].append({"date": row.get("date"), "volume": float(vol)})
        except (TypeError, ValueError):
            continue
    issues: list[dict] = []
    for symbol, points in series.items():
        vols = [p["volume"] for p in points]
        n = len(vols)
        if n < 20:
            continue
        mean = sum(vols) / n
        std = (sum((v - mean) ** 2 for v in vols) / n) ** 0.5
        if std <= 0:
            continue
        for p in points:
            z = (p["volume"] - mean) / std
            if abs(z) >= z_threshold:
                issues.append({"symbol": symbol, "table_name": "daily_bars_forward_adj", "date": p["date"], "issue_type": "volume_anomaly", "severity": "warning", "message": f"volume z-score {round(z, 2)} (mean={round(mean, 0)})", "source": "data_quality"})
            elif p["volume"] == 0 and mean > 0:
                issues.append({"symbol": symbol, "table_name": "daily_bars_forward_adj", "date": p["date"], "issue_type": "zero_volume", "severity": "watch", "message": "zero volume on active series (possible halt/missing)", "source": "data_quality"})
    db.close()
    return issues


def check_adjustment_consistency(forward: str = "forward", back: str = "back", raw: str = "raw", db_path: str = "data/quantdesk.db") -> list[dict]:
    """Cross-check the forward-adjusted table against raw: every raw (symbol,date)
    should have a forward row, with positive numeric closes. Returns [] when the
    store has no data."""
    table_map = {"forward": "daily_bars_forward_adj", "forward_adj": "daily_bars_forward_adj", "back": "daily_bars_back_adj", "back_adj": "daily_bars_back_adj", "raw": "daily_bars_raw"}
    fwd_t = table_map.get(forward, "daily_bars_forward_adj")
    raw_t = table_map.get(raw, "daily_bars_raw")
    db = QuantDeskDB(db_path)
    db.initialize()
    try:
        fwd_rows = db.execute(f"SELECT symbol, date, close FROM {fwd_t}")
        raw_rows = db.execute(f"SELECT symbol, date, close FROM {raw_t}")
    except Exception:
        db.close()
        return []
    fwd = {(r["symbol"], r["date"]): r.get("close") for r in fwd_rows}
    issues: list[dict] = []
    for r in raw_rows:
        key = (r["symbol"], r["date"])
        raw_close = r.get("close")
        fwd_close = fwd.get(key)
        if fwd_close is None:
            issues.append({"symbol": r["symbol"], "table_name": fwd_t, "date": r["date"], "issue_type": "adjustment_gap", "severity": "warning", "message": f"raw row present but missing in {fwd_t}", "source": "data_quality"})
            continue
        try:
            if float(raw_close) <= 0 or float(fwd_close) <= 0:
                issues.append({"symbol": r["symbol"], "table_name": fwd_t, "date": r["date"], "issue_type": "adjustment_nonpositive", "severity": "warning", "message": "non-positive close in raw/forward pair", "source": "data_quality"})
        except (TypeError, ValueError):
            issues.append({"symbol": r["symbol"], "table_name": fwd_t, "date": r["date"], "issue_type": "adjustment_invalid", "severity": "warning", "message": "non-numeric close in raw/forward pair", "source": "data_quality"})
    db.close()
    return issues


def flag_issues_to_sqlite(issues: list[dict], db_path: str = "data/quantdesk.db") -> dict:
    db = QuantDeskDB(db_path)
    db.initialize()
    result = DataQualityRepository(db).insert_issues(issues)
    db.close()
    return result


def repair_missing_dates(symbol: str, missing_dates: list[str], network: bool = False, db_path: str = "data/quantdesk.db") -> dict:
    db = QuantDeskDB(db_path)
    db.initialize()
    issues = []
    if not network:
        for date in missing_dates:
            issues.append({"symbol": symbol, "table_name": "daily_bars_forward_adj", "date": date, "issue_type": "missing_date", "severity": "watch", "message": "network=false; repair skipped", "repair_status": "skipped_network_false", "detected_at": datetime.now().isoformat(timespec="seconds"), "source": "data_quality_repair"})
        DataQualityRepository(db).insert_issues(issues)
        db.close()
        return {"ok": True, "status": "warning", "repaired_count": 0, "repair_status": "skipped_network_false", "warnings": ["network_false_no_refetch"]}
    db.close()
    return {"ok": True, "status": "warning", "repaired_count": 0, "warnings": ["single_day_refetch_adapter_not_configured"]}


def build_data_quality_report(config: dict | None = None) -> dict:
    cfg = config or {}
    db_path = cfg.get("db_path") or "data/quantdesk.db"
    symbol = cfg.get("symbol") or ""
    adjustment = cfg.get("adjustment") or "forward"
    warnings: list[str] = []
    issues = detect_price_anomalies(db_path=db_path)
    if symbol:
        missing = detect_missing_dates(symbol, adjustment, db_path)
        if missing:
            issues.append({"symbol": symbol, "table_name": "daily_bars_forward_adj", "date": "", "issue_type": "missing_dates", "severity": "watch", "message": ",".join(missing), "source": "data_quality"})
    if cfg.get("repair"):
        repair = repair_missing_dates(symbol or "000000", ["unknown"] if not symbol else detect_missing_dates(symbol, adjustment, db_path), network=bool(cfg.get("network")), db_path=db_path)
        warnings.extend(repair.get("warnings", []))
    if issues:
        flag_issues_to_sqlite(issues, db_path)
    status = "warning" if warnings or issues else "passed"
    return {
        "ok": True,
        "status": status,
        "task_type": "data_quality",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "issue_count": len(issues),
        "issues": issues[:50],
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "output_paths": {"db_path": db_path},
        "disclaimer": "Data quality repair marks issues by default and does not overwrite existing rows.",
    }
