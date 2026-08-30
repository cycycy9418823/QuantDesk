from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .schema import get_required_indexes, get_required_tables
from .sqlite_store import QuantDeskDB

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V581_DIAGNOSIS_MAX_AGE_DAYS = 7


def build_trading_calendar_from_daily_bars(rows: list[dict]) -> list[str]:
    """Build a sorted trading calendar from complete daily bar rows only."""

    dates = {
        str(row.get("date") or "")[:10]
        for row in rows
        if row.get("date") and str(row.get("date"))[:10]
    }
    return sorted(dates)


def build_rebalance_events(calendar: list[str], *, interval: int = 20) -> list[dict]:
    """Build research-only rebalance events with signal_date before execution_date."""

    step = max(int(interval or 20), 1)
    events: list[dict] = []
    for idx in range(step, len(calendar), step):
        signal_date = calendar[idx - 1]
        execution_date = calendar[idx]
        if signal_date < execution_date:
            events.append({"signal_date": signal_date, "execution_date": execution_date})
    return events


def audit_daily_bar_coverage(rows: list[dict], *, min_dates: int = 60) -> dict:
    calendar = build_trading_calendar_from_daily_bars(rows)
    symbols = sorted({str(row.get("symbol") or "") for row in rows if row.get("symbol")})
    row_count = len(rows)
    expected_min_rows = len(symbols) * max(len(calendar), 1)
    missing_ratio = 0.0
    if expected_min_rows:
        missing_ratio = max(0.0, 1.0 - (row_count / expected_min_rows))
    return {
        "row_count": row_count,
        "symbol_count": len(symbols),
        "trading_day_count": len(calendar),
        "start_date": calendar[0] if calendar else None,
        "end_date": calendar[-1] if calendar else None,
        "missing_ratio_estimate": round(missing_ratio, 6),
        "coverage_status": "passed" if len(calendar) >= min_dates and row_count > 0 else "warning",
        "warnings": [] if len(calendar) >= min_dates and row_count > 0 else ["insufficient_daily_bar_coverage"],
    }


def build_strategy_readiness(db: QuantDeskDB) -> dict:
    rows = db.execute(
        "SELECT symbol, date, open, high, low, close, volume, amount "
        "FROM daily_bars_forward_adj ORDER BY date, symbol LIMIT 5000"
    )
    coverage = audit_daily_bar_coverage(rows)
    calendar = build_trading_calendar_from_daily_bars(rows)
    rebalance_events = build_rebalance_events(calendar)
    signal_before_execution = all(
        event["signal_date"] < event["execution_date"] for event in rebalance_events
    )
    warnings = list(coverage.get("warnings") or [])
    warnings.append("survivorship_bias_risk_high_without_delisted_or_st_history")
    return {
        "research_only": True,
        "ready_for_strategy_research": bool(
            coverage.get("coverage_status") == "passed" and signal_before_execution
        ),
        "daily_bar_coverage": coverage,
        "rebalance_event_count": len(rebalance_events),
        "rebalance_events_sample": rebalance_events[:5],
        "signal_before_execution": signal_before_execution,
        "survivorship_bias_risk": "high",
        "survivorship_bias_note": "No delisted/ST history is available in the shadow layer audit.",
        "partial_daily_isolated": True,
        "warnings": warnings,
    }


def load_v581_data_gap_diagnosis(
    root: Path = PROJECT_ROOT,
    *,
    now: datetime | None = None,
    max_age_days: int = V581_DIAGNOSIS_MAX_AGE_DAYS,
) -> dict:
    path = root / "reports" / "v581_data_gap_diagnosis" / "data_gap_diagnosis_latest.json"
    if not path.exists():
        return {
            "available": False,
            "status": "not_generated",
            "message": "v581 diagnosis has not been generated yet.",
            "path": str(path),
            "reason_code_counts": {},
            "source_status_counts": {},
            "field_status_counts": {},
            "freshness_status": "missing",
            "age_days": None,
            "warnings": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "available": False,
            "status": "read_error",
            "message": str(exc)[:300],
            "path": str(path),
            "reason_code_counts": {},
            "source_status_counts": {},
            "field_status_counts": {},
            "freshness_status": "read_error",
            "age_days": None,
            "warnings": ["v581_data_gap_diagnosis_read_error"],
        }
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    generated_at = str(payload.get("generated_at") or "").strip()
    freshness_status = "unknown"
    age_days: float | None = None
    diagnosis_warnings: list[str] = []
    if generated_at:
        try:
            generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.astimezone()
            observed_at = now or datetime.now().astimezone()
            if observed_at.tzinfo is None:
                observed_at = observed_at.astimezone()
            age_seconds = max(0.0, (observed_at - generated.astimezone(observed_at.tzinfo)).total_seconds())
            age_days = round(age_seconds / 86400.0, 3)
            if age_days > max(0, int(max_age_days)):
                freshness_status = "stale"
                diagnosis_warnings.append("v581_data_gap_diagnosis_stale")
            else:
                freshness_status = "fresh"
        except ValueError:
            freshness_status = "invalid_generated_at"
            diagnosis_warnings.append("v581_data_gap_diagnosis_generated_at_invalid")
    else:
        freshness_status = "missing_generated_at"
        diagnosis_warnings.append("v581_data_gap_diagnosis_generated_at_missing")
    return {
        "available": True,
        "status": payload.get("status", "completed_with_warnings"),
        "path": str(path),
        "generated_at": generated_at or None,
        "freshness_status": freshness_status,
        "age_days": age_days,
        "max_age_days": max(0, int(max_age_days)),
        "warnings": diagnosis_warnings,
        "network": payload.get("network"),
        "total_records": summary.get("total_records", 0),
        "status_counts": summary.get("status_counts", {}),
        "reason_code_counts": summary.get("reason_code_counts", {}),
        "source_status_counts": summary.get("source_status_counts", {}),
        "field_status_counts": summary.get("field_status_counts", {}),
        "html": str(root / "reports" / "v581_data_gap_diagnosis" / "data_gap_diagnosis_latest.html"),
    }


def classify_sqlite_shadow_status(
    *,
    db_existed_before_check: bool,
    missing_tables: list[str],
    table_row_counts: dict[str, int | None],
) -> tuple[str, str, list[str]]:
    """Classify whether the SQLite shadow layer is absent, empty, inactive, or active.

    The data backbone is intentionally a shadow layer.  This classifier does not
    import or mutate market data; it only makes an empty/inactive database visible
    so reports do not imply that SQLite already carries production data.
    """

    warnings: list[str] = []
    if not db_existed_before_check:
        warnings.extend(["sqlite_shadow_absent_before_health_check", "reconciliation_missing"])
        return "absent", "missing", warnings
    if missing_tables:
        warnings.extend(["sqlite_shadow_inactive_schema_incomplete", "reconciliation_missing"])
        return "inactive", "missing", warnings
    numeric_counts = [int(value or 0) for value in table_row_counts.values() if value is not None]
    total_rows = sum(numeric_counts)
    if total_rows <= 0:
        warnings.extend(["sqlite_shadow_empty", "reconciliation_missing"])
        return "empty", "missing", warnings
    if any(value is None for value in table_row_counts.values()):
        warnings.append("sqlite_shadow_inactive_table_stats_partial")
        return "inactive", "partial", warnings
    warnings.append("sqlite_shadow_active_without_full_reconciliation")
    return "active", "partial", warnings


def build_data_layer_health(config: dict | None = None) -> dict:
    cfg = config or {}
    db_path = Path(cfg.get("db_path") or "data/quantdesk.db")
    warnings: list[str] = []
    errors: list[str] = []
    initially_exists = db_path.exists()
    if not initially_exists:
        warnings.append("sqlite_db_missing")
    db = QuantDeskDB(db_path)
    db.initialize()
    existing_tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    existing_indexes = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_tables = [name for name in get_required_tables() if name not in existing_tables]
    missing_indexes = [name for name in get_required_indexes() if name not in existing_indexes]
    if missing_tables:
        errors.append("missing_tables:" + ",".join(missing_tables))
    if missing_indexes:
        errors.append("missing_indexes:" + ",".join(missing_indexes))
    pragma = db.execute_one("PRAGMA journal_mode")
    view_sql = db.execute_one("SELECT sql FROM sqlite_master WHERE type='view' AND name='daily_bars'")
    partial_isolated = "intraday_partial_daily" not in ((view_sql or {}).get("sql") or "")
    if not partial_isolated:
        errors.append("partial_daily_joined_into_daily_view")
    strategy_readiness = build_strategy_readiness(db)
    strategy_readiness["partial_daily_isolated"] = partial_isolated
    data_root = Path(cfg.get("data_root") or "data")
    csv_dirs = {name: (data_root / name).exists() for name in ["daily", "index", "factors", "intraday"]}
    for name, exists in csv_dirs.items():
        if not exists:
            warnings.append(f"csv_dir_missing:{name}")
    v581_diagnosis = load_v581_data_gap_diagnosis(PROJECT_ROOT)
    warnings.extend(item for item in v581_diagnosis.get("warnings", []) if item not in warnings)
    table_row_counts = db.table_stats()
    sqlite_shadow_status, reconciliation_status, shadow_warnings = classify_sqlite_shadow_status(
        db_existed_before_check=initially_exists,
        missing_tables=missing_tables,
        table_row_counts=table_row_counts,
    )
    warnings.extend(item for item in shadow_warnings if item not in warnings)
    status = "failed" if errors else ("warning" if warnings else "passed")
    payload = {
        "ok": not errors,
        "status": status,
        "task_type": "data_layer_health",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_exists": db_path.exists(),
        "schema_initialized": not missing_tables,
        "wal_enabled": (pragma or {}).get("journal_mode") == "wal",
        "busy_timeout_ms": (db.execute_one("PRAGMA busy_timeout") or {}).get("timeout"),
        "required_tables_missing": missing_tables,
        "required_indexes_missing": missing_indexes,
        "table_row_counts": table_row_counts,
        "sqlite_shadow_status": sqlite_shadow_status,
        "reconciliation_status": reconciliation_status,
        "sqlite_shadow_note": (
            "SQLite remains a shadow data layer; empty/inactive status means CSV "
            "fallback is still the authoritative compatibility path."
        ),
        "csv_dirs": csv_dirs,
        "partial_daily_isolated": partial_isolated,
        "strategy_readiness": strategy_readiness,
        "v581_data_gap_diagnosis": v581_diagnosis,
        "reason_code_counts": v581_diagnosis.get("reason_code_counts", {}),
        "source_status_counts": v581_diagnosis.get("source_status_counts", {}),
        "field_status_counts": v581_diagnosis.get("field_status_counts", {}),
        "warnings": warnings,
        "errors": errors,
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "output_paths": {"db_path": str(db_path)},
        "disclaimer": "SQLite is a shadow data layer. CSV remains the compatibility fallback.",
    }
    db.close()
    return payload
