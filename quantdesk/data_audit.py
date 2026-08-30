from __future__ import annotations

import json
import math
from bisect import bisect_left
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.trading_calendar import calendar_info, load_a_share_trading_calendar

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "daily"


@dataclass
class StockAudit:
    symbol: str
    rows: int
    date_min: str
    date_max: str
    nan_pct: float
    gap_days: int
    missing_trade_days: int
    adjustment_suspect: bool
    survivable: bool
    warnings: list[str]


DATE_ALIASES = ("date", "日期", "trade_date")
CRITICAL_ALIASES = {
    "open": ("open", "开盘"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "close": ("close", "收盘"),
    "volume": ("volume", "成交量"),
}


def _first_existing(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    existing = set(columns)
    for name in aliases:
        if name in existing:
            return name
    return None


def _critical_columns(df: pd.DataFrame) -> list[str]:
    cols: list[str] = []
    for aliases in CRITICAL_ALIASES.values():
        col = _first_existing(list(df.columns), aliases)
        if col:
            cols.append(col)
    return cols


def _calendar_gap_stats(dates: pd.Series, calendar_path: str | Path | None = None) -> tuple[int, int]:
    unique_dates = pd.Series(pd.to_datetime(dates, errors="coerce").dropna().unique()).sort_values()
    if len(unique_dates) < 2:
        return 0, 0
    gap_events = 0
    missing_trade_days = 0
    calendar = Path(calendar_path) if calendar_path else None
    calendar_data = load_a_share_trading_calendar(calendar)
    open_dates = sorted(
        day for day, is_open in (calendar_data.get("is_open_by_date") or {}).items() if is_open
    )
    calendar_start = open_dates[0] if open_dates else ""
    calendar_end = open_dates[-1] if open_dates else ""
    for previous, current in zip(unique_dates.iloc[:-1], unique_dates.iloc[1:], strict=False):
        if (current.date() - previous.date()).days <= 3:
            continue
        previous_text = previous.date().isoformat()
        current_text = current.date().isoformat()
        if open_dates and calendar_start <= previous_text <= calendar_end and calendar_start <= current_text <= calendar_end:
            left = bisect_left(open_dates, previous_text)
            right = bisect_left(open_dates, current_text)
            missing_count = max(0, right - left - 1)
        else:
            start = np.datetime64(previous.date().isoformat(), "D") + np.timedelta64(1, "D")
            end = np.datetime64(current.date().isoformat(), "D")
            busday_gap = int(np.busday_count(start, end))
            # The bundled exchange calendar currently starts in 2020, while
            # many local daily files start in 2015. Plain weekday counts would
            # incorrectly flag normal pre-2020 Spring Festival/National Day
            # closures as missing A-share sessions, so only count the excess
            # beyond a normal long exchange holiday when no exchange calendar
            # coverage is available.
            missing_count = max(0, busday_gap - 5)
        if missing_count > 0:
            gap_events += 1
            missing_trade_days += missing_count
    return gap_events, missing_trade_days


def audit_daily_csv(csv_path: Path, calendar_path: str | Path | None = None) -> StockAudit:
    symbol = csv_path.stem
    warnings: list[str] = []
    df = read_csv_frame(csv_path)
    warnings.extend(str(item) for item in df.attrs.get("warnings", []) if item)
    if df.empty and any("csv_read_failed" in item for item in warnings):
        return StockAudit(symbol, 0, "", "", 1.0, 999, 999, True, False, warnings)

    rows = int(len(df))
    date_col = _first_existing(list(df.columns), DATE_ALIASES)
    if date_col is None:
        return StockAudit(symbol, rows, "", "", 1.0, 999, 999, True, False, ["missing_date_column"])

    dates = pd.to_datetime(df[date_col], errors="coerce")
    if dates.isna().all():
        return StockAudit(symbol, rows, "", "", 1.0, 999, 999, True, False, ["invalid_dates"])

    work = df.assign(_audit_date=dates).dropna(subset=["_audit_date"]).sort_values("_audit_date")
    critical = _critical_columns(work)
    if not critical:
        nan_pct = 1.0
        warnings.append("missing_critical_price_columns")
    else:
        numeric = work[critical].apply(pd.to_numeric, errors="coerce")
        nan_pct = float(numeric.isna().mean().mean())
        if not math.isfinite(nan_pct):
            nan_pct = 1.0

    duplicated_dates = int(work["_audit_date"].duplicated().sum())
    if duplicated_dates:
        warnings.append(f"duplicate_dates:{duplicated_dates}")

    gap_days, missing_trade_days = _calendar_gap_stats(work["_audit_date"], calendar_path)

    close_col = _first_existing(list(work.columns), CRITICAL_ALIASES["close"])
    adjustment_suspect = True
    if close_col:
        close = pd.to_numeric(work[close_col], errors="coerce")
        rets = close.ffill().pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
        # A-share boards can have 10%, 20%, and IPO-specific limit regimes.
        # Treat only split-like discontinuities as adjustment suspects here;
        # board-limit moves are market events, not evidence of bad adjustment.
        spikes = rets.abs() > 0.35
        next_day = rets.shift(-1).abs()
        adjustment_suspect = bool(((spikes) & (next_day < 0.2)).sum() > 2)
    else:
        warnings.append("missing_close_column")

    survivable = rows >= 500 and nan_pct < 0.1 and gap_days < 10 and not adjustment_suspect
    if rows < 500:
        warnings.append("too_few_rows")
    if nan_pct >= 0.1:
        warnings.append("high_nan_ratio")
    if gap_days >= 10:
        warnings.append("many_large_date_gaps")
    if missing_trade_days >= 30:
        warnings.append("many_missing_trade_days")
    if adjustment_suspect:
        warnings.append("adjustment_suspect")

    return StockAudit(
        symbol=symbol,
        rows=rows,
        date_min=str(work["_audit_date"].min().date()),
        date_max=str(work["_audit_date"].max().date()),
        nan_pct=round(nan_pct, 6),
        gap_days=gap_days,
        missing_trade_days=missing_trade_days,
        adjustment_suspect=adjustment_suspect,
        survivable=survivable,
        warnings=warnings,
    )


def audit_all_daily_csvs(data_root: str | Path = DEFAULT_DATA_ROOT, calendar_path: str | Path | None = None) -> list[StockAudit]:
    root = Path(data_root)
    if not root.exists():
        return []
    return [audit_daily_csv(path, calendar_path=calendar_path) for path in sorted(root.glob("*.csv"))]


def build_data_audit_payload(data_root: str | Path = DEFAULT_DATA_ROOT, calendar_path: str | Path | None = None) -> dict:
    audits = audit_all_daily_csvs(data_root, calendar_path=calendar_path)
    total = len(audits)
    survivable = sum(1 for item in audits if item.survivable)
    survivable_ratio = survivable / total if total else 0.0
    warnings = sorted({warning for item in audits for warning in item.warnings})
    return {
        "task_id": "data_daily_csv_quality_audit",
        "status": "passed" if total and survivable_ratio >= 0.8 else "warning",
        "data_root": str(Path(data_root)),
        "calendar": calendar_info(Path(calendar_path) if calendar_path else None),
        "stock_count": total,
        "survivable_count": survivable,
        "survivable_ratio": round(survivable_ratio, 6),
        "warnings": warnings,
        "items": [asdict(item) for item in audits],
    }


def write_data_audit(
    data_root: str | Path = DEFAULT_DATA_ROOT,
    output_json: str | Path = PROJECT_ROOT / "results" / "data_audit.json",
    output_md: str | Path = PROJECT_ROOT / "reports" / "data_audit.md",
    calendar_path: str | Path | None = None,
) -> dict:
    payload = build_data_audit_payload(data_root, calendar_path=calendar_path)
    json_path = Path(output_json)
    md_path = Path(output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Data Daily CSV Quality Audit",
        "",
        "Research-only audit. It does not modify data/daily and does not generate trading advice.",
        "",
        f"- status: {payload['status']}",
        f"- stock_count: {payload['stock_count']}",
        f"- survivable_count: {payload['survivable_count']}",
        f"- survivable_ratio: {payload['survivable_ratio']:.2%}",
        f"- calendar_quality: {payload['calendar']['calendar_quality']}",
        f"- warning_types: {len(payload['warnings'])}",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload["files_written"] = [str(json_path), str(md_path)]
    return payload
