"""Unified read-only evidence audit for staged vendor download packs.

This module audits local ``data/vendor_staging`` download packs produced by the
mootdx minute downloader and the BaoStock evidence-pack downloader. It is
deliberately conservative and offline:

* it never opens a socket, fetches network data, or imports anything;
* it never writes ``data/daily``, ``data/intraday``, ``data/metadata`` or
  ``data/benchmark``; it only emits an audit JSON/Markdown under ``results`` /
  ``reports`` when asked;
* while the download is still ``running`` it refuses to draw any completeness or
  importability conclusion and returns ``wait_for_download_completion``;
* it never presents staged vendor files as formal, clean, canonical strategy
  evidence. ``formal_eligible`` stays ``False`` and the only positive verdict it
  can reach is ``ready_for_manual_import_review`` (a human still has to run the
  separate, user-confirmed import step).

The three ``recommended_action`` outcomes are:

* ``wait_for_download_completion`` — the pack is still downloading;
* ``repair_staging`` — the pack finished but the audit found integrity problems;
* ``ready_for_manual_import_review`` — the pack finished and passed the audit,
  so a human may now review it for a user-confirmed import (never automatic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.providers.mootdx_provider import normalize_mootdx_zero_sentinels
from quantdesk.shared import normalize_symbol, now_text, read_json, write_json, write_text
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_STAGING_DIR = PROJECT_ROOT / "data" / "vendor_staging"
RESULTS_DIR = PROJECT_ROOT / "results" / "data_health"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_OUTPUT_PATH = RESULTS_DIR / "staged_provider_evidence_audit_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "staged_provider_evidence_audit_latest.md"

DISCLAIMER = (
    "仅用于本地暂存数据源证据审计，不接券商、不真实下单、不构成投资建议；"
    "暂存数据不是正式策略证据，导入前必须人工复核并由用户确认。"
)

# A running download's stdout log usually updates within this window. If the
# summary does not clearly say "completed" and a log was touched this recently,
# the pack is treated as still running (fail-closed to wait).
RUNNING_LOG_WINDOW_SECONDS = 15 * 60

_COMPLETED_STATES = {"completed", "complete", "done", "finished", "success", "ok"}
_RUNNING_STATES = {"running", "running_or_present", "in_progress", "downloading", "resuming"}

# A-share continuous-auction sessions. Minute bars are labelled at the close of
# each minute, so 09:31..11:30 and 13:01..15:00 are in-session; the opening
# 09:30 label and the 13:00 label are tolerated at the boundary.
_MORNING_START = "09:30"
_MORNING_END = "11:30"
_AFTERNOON_START = "13:00"
_AFTERNOON_END = "15:00"

_RECOMMENDED_ACTIONS = {
    "wait_for_download_completion",
    "repair_staging",
    "ready_for_manual_import_review",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _log_age_seconds(staging_dir: Path, now: datetime) -> float | None:
    """Return the age (seconds) of the most recently modified ``*.log`` file."""

    youngest: float | None = None
    for path in staging_dir.rglob("*.log"):
        if not path.is_file():
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        age = max(0.0, (now - modified).total_seconds())
        if youngest is None or age < youngest:
            youngest = age
    return youngest


def _classify_liveness(summary: dict[str, Any], staging_dir: Path, now: datetime) -> dict[str, Any]:
    """Decide whether the download is running, completed, or unknown.

    Fail-closed: an ambiguous state with a freshly-updated log is treated as
    running so the audit never claims completeness mid-download.
    """

    status = str(summary.get("status") or "").strip().lower()
    log_age = _log_age_seconds(staging_dir, now)
    log_fresh = log_age is not None and log_age <= RUNNING_LOG_WINDOW_SECONDS

    if status in _RUNNING_STATES:
        state = "running"
    elif status in _COMPLETED_STATES:
        # A summary can say "completed" while a resume process is still writing;
        # trust the explicit completed marker but surface the fresh-log note.
        state = "completed"
    elif not status:
        state = "running" if log_fresh else "unknown"
    else:
        # Unknown/other status word: only downgrade to running if a log is fresh.
        state = "running" if log_fresh else "unknown"

    return {
        "download_status": status or "unknown",
        "liveness": state,
        "log_age_seconds": None if log_age is None else int(log_age),
        "log_fresh": bool(log_fresh),
    }


def _read_csv(path: Path) -> tuple[pd.DataFrame, list[str]]:
    frame = read_csv_frame(path, dtype=str)
    warnings = list(frame.attrs.get("warnings") or [])
    return frame, warnings


def _to_float_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


@dataclass
class FileFinding:
    symbol: str
    status: str
    reason_codes: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "reason_codes": list(dict.fromkeys(self.reason_codes)),
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# provider detection
# --------------------------------------------------------------------------- #
def detect_staged_provider(summary: dict[str, Any], staging_dir: Path) -> str:
    """Infer the provider from summary metadata, then directory layout."""

    provider = str(summary.get("provider") or "").strip().lower()
    if provider in {"mootdx", "baostock"}:
        return provider
    name = staging_dir.name.lower()
    if "mootdx" in name:
        return "mootdx"
    if "baostock" in name:
        return "baostock"
    # Layout-based fallback: mootdx packs carry raw/1min, baostock packs carry
    # a reference/ directory with static tables.
    if (staging_dir / "reference").is_dir():
        return "baostock"
    if (staging_dir / "raw" / "1min").is_dir():
        return "mootdx"
    return "unknown"


def discover_staged_packs(
    root: Path,
    *,
    provider: str = "all",
) -> list[tuple[str, Path]]:
    """Discover mootdx/baostock staging packs under ``data/vendor_staging``.

    Returns a list of ``(provider, staging_dir)`` for directories that look like
    a mootdx or baostock pack (carry a summary.json and a recognizable layout).
    Purely filesystem-based; no network, no writes.
    """

    staging_root = root / "data" / "vendor_staging"
    if not staging_root.is_dir():
        return []
    packs: list[tuple[str, Path]] = []
    for child in sorted(staging_root.iterdir()):
        if not child.is_dir():
            continue
        summary_path = child / "summary.json"
        summary = read_json(summary_path, {}) if summary_path.exists() else {}
        if not isinstance(summary, dict):
            summary = {}
        # Auto-discovery trusts ONLY the summary provider field or an explicit
        # name match. The layout fallback in detect_staged_provider is too greedy
        # for a sweep (unrelated tushare_proxy minute packs also carry raw/1min),
        # so it is reserved for an explicitly-passed --staging-dir.
        summary_provider = str(summary.get("provider") or "").strip().lower()
        name = child.name.lower()
        if summary_provider in {"mootdx", "baostock"}:
            detected = summary_provider
        elif "mootdx" in name:
            detected = "mootdx"
        elif "baostock" in name:
            detected = "baostock"
        else:
            continue
        if provider != "all" and detected != provider:
            continue
        packs.append((detected, child))
    return packs


# --------------------------------------------------------------------------- #
# mootdx minute audit
# --------------------------------------------------------------------------- #
def _parse_session_bounds() -> tuple[int, int, int, int]:
    def _m(text: str) -> int:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)

    return _m(_MORNING_START), _m(_MORNING_END), _m(_AFTERNOON_START), _m(_AFTERNOON_END)


def _minute_of_day(value: pd.Timestamp) -> int:
    return int(value.hour) * 60 + int(value.minute)


def _audit_mootdx_csv(
    path: Path,
    symbol: str,
    *,
    start_date: str,
    end_date: str,
) -> FileFinding:
    finding = FileFinding(symbol=symbol, status="ok")
    frame, read_warnings = _read_csv(path)
    for warning in read_warnings:
        finding.reason_codes.append(f"csv_read_warning:{warning}")

    if frame.empty:
        finding.status = "invalid"
        finding.reason_codes.append("empty_csv")
        return finding

    frame, sentinel_diagnostics = normalize_mootdx_zero_sentinels(frame)
    finding.detail["mootdx_zero_sentinel_rows"] = int(
        sentinel_diagnostics.get("row_count") or 0
    )
    finding.detail["mootdx_zero_sentinel_fields"] = int(
        sentinel_diagnostics.get("field_count") or 0
    )

    required_cols = ["symbol", "datetime", "open", "high", "low", "close", "volume"]
    missing_cols = [col for col in required_cols if col not in frame.columns]
    if missing_cols:
        finding.status = "invalid"
        finding.reason_codes.append(f"missing_columns:{','.join(missing_cols)}")
        return finding

    dt = pd.to_datetime(frame["datetime"], errors="coerce")
    n_unparsable = int(dt.isna().sum())
    if n_unparsable:
        finding.reason_codes.append(f"unparsable_datetime_rows:{n_unparsable}")

    o = _to_float_series(frame["open"])
    h = _to_float_series(frame["high"])
    lo = _to_float_series(frame["low"])
    c = _to_float_series(frame["close"])
    v = _to_float_series(frame["volume"])

    # positive prices
    price_frame = pd.concat([o, h, lo, c], axis=1)
    nonpositive = int((price_frame <= 0).any(axis=1).sum()) + int(price_frame.isna().any(axis=1).sum())
    if nonpositive:
        finding.reason_codes.append(f"nonpositive_or_nan_price_rows:{nonpositive}")

    # high/low envelope: high must be >= low, and high>=max(open,close), low<=min(open,close)
    envelope_bad = (
        (h < lo)
        | (h < o)
        | (h < c)
        | (lo > o)
        | (lo > c)
    )
    n_envelope_bad = int(envelope_bad.fillna(True).sum() - envelope_bad.isna().sum())
    if n_envelope_bad > 0:
        finding.reason_codes.append(f"bad_high_low_envelope_rows:{n_envelope_bad}")

    # volume non-negative (0 is a legitimate no-trade minute)
    neg_vol = int((v < 0).sum()) + int(v.isna().sum())
    if neg_vol:
        finding.reason_codes.append(f"negative_or_nan_volume_rows:{neg_vol}")

    # duplicate datetime
    valid_dt = dt.dropna()
    dup = int(valid_dt.duplicated().sum())
    if dup:
        finding.reason_codes.append(f"duplicate_datetime_rows:{dup}")

    # reverse time order (raw source should stream forward)
    if len(valid_dt) >= 2:
        ordered = valid_dt.tolist()
        if any(ordered[i] > ordered[i + 1] for i in range(len(ordered) - 1)):
            finding.reason_codes.append("reverse_time_order")

    # out-of-session minutes
    ms, me, as_, ae = _parse_session_bounds()
    if not valid_dt.empty:
        minutes = valid_dt.map(_minute_of_day)
        in_morning = (minutes >= ms) & (minutes <= me)
        in_afternoon = (minutes >= as_) & (minutes <= ae)
        out_of_session = int((~(in_morning | in_afternoon)).sum())
        if out_of_session:
            finding.reason_codes.append(f"out_of_session_rows:{out_of_session}")

    # date range coverage vs requested window
    if not valid_dt.empty:
        first_date = valid_dt.min().strftime("%Y-%m-%d")
        last_date = valid_dt.max().strftime("%Y-%m-%d")
        finding.detail["first_date"] = first_date
        finding.detail["last_date"] = last_date
        if start_date and first_date > start_date:
            finding.reason_codes.append("start_date_not_reached")
        if end_date and last_date < end_date:
            finding.reason_codes.append("end_date_not_reached")

    finding.detail["rows"] = int(len(frame))
    finding.detail["file"] = path.name

    # Classify: hard structural problems -> invalid; softer coverage/order
    # problems -> partial; clean -> ok.
    hard_markers = ("missing_columns", "empty_csv", "bad_high_low_envelope_rows", "nonpositive_or_nan_price_rows", "negative_or_nan_volume_rows", "duplicate_datetime_rows", "reverse_time_order", "unparsable_datetime_rows")
    soft_markers = ("out_of_session_rows", "start_date_not_reached", "end_date_not_reached")
    has_hard = any(code.split(":")[0] in hard_markers for code in finding.reason_codes)
    has_soft = any(code.split(":")[0] in soft_markers for code in finding.reason_codes)
    if has_hard:
        finding.status = "invalid"
    elif has_soft:
        finding.status = "partial"
    else:
        finding.status = "ok"
    return finding


def _audit_mootdx(
    staging_dir: Path,
    summary: dict[str, Any],
    root: Path,
    *,
    max_files_deep_check: int,
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []

    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    symbols = summary.get("symbols") if isinstance(summary.get("symbols"), dict) else {}

    # status counts recomputed from per-symbol records
    recomputed: dict[str, int] = {}
    downloaded_symbols: list[tuple[str, dict[str, Any]]] = []
    for sym, rec in symbols.items():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "unknown")
        recomputed[status] = recomputed.get(status, 0) + 1
        if status in {"downloaded", "partial"}:
            downloaded_symbols.append((normalize_symbol(sym), rec))

    # summary counts vs recomputed per-symbol status counts
    count_mismatch = {}
    for status, n in counts.items():
        if int(recomputed.get(status, 0)) != int(n):
            count_mismatch[status] = {"summary": int(n), "recomputed": int(recomputed.get(status, 0))}
    for status, n in recomputed.items():
        if status not in counts:
            count_mismatch[status] = {"summary": 0, "recomputed": int(n)}
    if count_mismatch:
        errors.append(f"summary_count_mismatch:{count_mismatch}")

    # CSV file count on disk vs downloaded+partial (records that claim a file)
    minute_dir = staging_dir / "raw" / "1min"
    csv_files = sorted(minute_dir.glob("*.csv")) if minute_dir.is_dir() else []
    csv_count = len(csv_files)
    expected_files = int(recomputed.get("downloaded", 0)) + int(recomputed.get("partial", 0))
    if csv_count != expected_files:
        errors.append(
            f"csv_file_count_mismatch:disk={csv_count}:summary_downloaded_partial={expected_files}"
        )

    # preserve reason codes for partial/failed/unsupported
    non_ok_reason_codes: dict[str, dict[str, int]] = {}
    for sym, rec in symbols.items():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "unknown")
        if status in {"downloaded"}:
            continue
        reason = str(rec.get("reason_code") or "unspecified")
        bucket = non_ok_reason_codes.setdefault(status, {})
        bucket[reason] = bucket.get(reason, 0) + 1

    start_date = str(summary.get("start_date") or "")
    end_date = str(summary.get("end_date") or "")

    # bounded deep OHLCV check over a sample of downloaded/partial files
    deep_findings: list[dict[str, Any]] = []
    sample = downloaded_symbols
    sampled = False
    if max_files_deep_check and len(downloaded_symbols) > max_files_deep_check:
        sample = downloaded_symbols[:max_files_deep_check]
        sampled = True
    file_status_counts: dict[str, int] = {}
    mootdx_zero_sentinel_rows = 0
    mootdx_zero_sentinel_fields = 0
    for sym, rec in sample:
        file_ref = rec.get("file")
        path = Path(str(file_ref)) if file_ref else (minute_dir / f"{sym}.csv")
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            fnd = FileFinding(symbol=sym, status="invalid", reason_codes=["file_missing_on_disk"])
        else:
            fnd = _audit_mootdx_csv(path, sym, start_date=start_date, end_date=end_date)
        file_status_counts[fnd.status] = file_status_counts.get(fnd.status, 0) + 1
        mootdx_zero_sentinel_rows += int(fnd.detail.get("mootdx_zero_sentinel_rows") or 0)
        mootdx_zero_sentinel_fields += int(fnd.detail.get("mootdx_zero_sentinel_fields") or 0)
        if fnd.status != "ok":
            deep_findings.append(fnd.as_dict())

    if mootdx_zero_sentinel_rows:
        warnings.append(
            "mootdx_zero_decoder_sentinel_normalizable: "
            f"rows={mootdx_zero_sentinel_rows}; fields={mootdx_zero_sentinel_fields}"
        )

    bad_files = int(file_status_counts.get("invalid", 0))

    audit = {
        "provider": "mootdx",
        "data_kind": str(summary.get("data_kind") or "1m_ohlcv"),
        "start_date": start_date,
        "end_date": end_date,
        "summary_counts": {str(k): int(v) for k, v in counts.items()},
        "recomputed_status_counts": recomputed,
        "count_mismatch": count_mismatch,
        "csv_file_count_on_disk": csv_count,
        "expected_file_count": expected_files,
        "non_ok_reason_codes": non_ok_reason_codes,
        "deep_check": {
            "checked": len(sample),
            "sampled": sampled,
            "max_files_deep_check": int(max_files_deep_check),
            "file_status_counts": file_status_counts,
            "mootdx_zero_sentinel_rows": mootdx_zero_sentinel_rows,
            "mootdx_zero_sentinel_fields": mootdx_zero_sentinel_fields,
            "problem_files": deep_findings[:200],
            "problem_file_total": len(deep_findings),
        },
        "warnings": warnings,
        "errors": errors,
    }
    audit["integrity_ok"] = not errors and bad_files == 0
    return audit


# --------------------------------------------------------------------------- #
# baostock evidence audit
# --------------------------------------------------------------------------- #
_BAOSTOCK_STATIC_TABLES = (
    "trade_calendar",
    "security_master",
    "industry_classification",
    "hs300_constituents",
    "zz500_constituents",
    "sz50_constituents",
)

_BAOSTOCK_TABLE_REQUIRED_COLUMNS = {
    "trade_calendar": ["calendar_date", "is_trading_day"],
    "security_master": ["code", "code_name"],
    "industry_classification": ["code", "industry"],
    "hs300_constituents": ["code"],
    "zz500_constituents": ["code"],
    "sz50_constituents": ["code"],
}


def _audit_baostock_static_table(path: Path, name: str) -> FileFinding:
    finding = FileFinding(symbol=name, status="ok")
    if not path.exists():
        finding.status = "missing"
        finding.reason_codes.append("file_missing_on_disk")
        return finding
    frame, read_warnings = _read_csv(path)
    for warning in read_warnings:
        finding.reason_codes.append(f"csv_read_warning:{warning}")
    if frame.empty:
        finding.status = "invalid"
        finding.reason_codes.append("empty_table")
        return finding
    required = _BAOSTOCK_TABLE_REQUIRED_COLUMNS.get(name, [])
    missing_cols = [col for col in required if col not in frame.columns]
    if missing_cols:
        finding.status = "invalid"
        finding.reason_codes.append(f"missing_columns:{','.join(missing_cols)}")
    finding.detail["rows"] = int(len(frame))
    return finding


def _audit_baostock_adjustment_factor_csv(path: Path, symbol: str) -> FileFinding:
    finding = FileFinding(symbol=symbol, status="ok")
    frame, read_warnings = _read_csv(path)
    for warning in read_warnings:
        finding.reason_codes.append(f"csv_read_warning:{warning}")
    if frame.empty:
        finding.status = "invalid"
        finding.reason_codes.append("empty_csv")
        return finding

    required_cols = ["symbol", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"]
    missing_cols = [col for col in required_cols if col not in frame.columns]
    if missing_cols:
        finding.status = "invalid"
        finding.reason_codes.append(f"missing_columns:{','.join(missing_cols)}")
        return finding

    fore = _to_float_series(frame["foreAdjustFactor"])
    back = _to_float_series(frame["backAdjustFactor"])
    adj = _to_float_series(frame["adjustFactor"])
    factor_frame = pd.concat([fore, back, adj], axis=1)
    nonpositive = int((factor_frame <= 0).any(axis=1).sum()) + int(factor_frame.isna().any(axis=1).sum())
    if nonpositive:
        finding.reason_codes.append(f"nonpositive_or_nan_factor_rows:{nonpositive}")

    op_date = pd.to_datetime(frame["dividOperateDate"], errors="coerce")
    n_bad_date = int(op_date.isna().sum())
    if n_bad_date:
        finding.reason_codes.append(f"unparsable_operate_date_rows:{n_bad_date}")

    # duplicate (symbol, dividOperateDate)
    dup = int(frame.duplicated(subset=["symbol", "dividOperateDate"]).sum())
    if dup:
        finding.reason_codes.append(f"duplicate_symbol_operate_date_rows:{dup}")

    valid_dates = op_date.dropna()
    if not valid_dates.empty:
        finding.detail["first_operate_date"] = valid_dates.min().strftime("%Y-%m-%d")
        finding.detail["last_operate_date"] = valid_dates.max().strftime("%Y-%m-%d")
    finding.detail["rows"] = int(len(frame))

    hard = ("missing_columns", "empty_csv", "nonpositive_or_nan_factor_rows", "duplicate_symbol_operate_date_rows", "unparsable_operate_date_rows")
    if any(code.split(":")[0] in hard for code in finding.reason_codes):
        finding.status = "invalid"
    else:
        finding.status = "ok"
    return finding


def _audit_baostock(
    staging_dir: Path,
    summary: dict[str, Any],
    root: Path,
    *,
    max_files_deep_check: int,
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []

    reference_dir = staging_dir / "reference"
    static_summary = summary.get("static_tables") if isinstance(summary.get("static_tables"), dict) else {}

    # static tables
    static_findings: dict[str, dict[str, Any]] = {}
    for name in _BAOSTOCK_STATIC_TABLES:
        rec = static_summary.get(name) if isinstance(static_summary.get(name), dict) else {}
        file_ref = rec.get("file") if isinstance(rec, dict) else None
        path = Path(str(file_ref)) if file_ref else (reference_dir / f"{name}.csv")
        if not path.is_absolute():
            path = root / path
        fnd = _audit_baostock_static_table(path, name)
        # row count consistency vs summary
        if isinstance(rec, dict) and "rows" in rec and fnd.status == "ok":
            summary_rows = int(rec.get("rows") or 0)
            disk_rows = int(fnd.detail.get("rows") or 0)
            if summary_rows and disk_rows and summary_rows != disk_rows:
                fnd.reason_codes.append(f"row_count_mismatch:summary={summary_rows}:disk={disk_rows}")
                fnd.status = "invalid"
        static_findings[name] = fnd.as_dict()
        if fnd.status not in {"ok"}:
            errors.append(f"static_table_problem:{name}:{fnd.status}")

    # adjustment factors
    af_summary = summary.get("adjustment_factors") if isinstance(summary.get("adjustment_factors"), dict) else {}
    af_recomputed: dict[str, int] = {}
    downloaded_af: list[tuple[str, dict[str, Any]]] = []
    af_reason_codes: dict[str, dict[str, int]] = {}
    for sym, rec in af_summary.items():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "unknown")
        af_recomputed[status] = af_recomputed.get(status, 0) + 1
        if status == "downloaded":
            downloaded_af.append((normalize_symbol(sym), rec))
        else:
            reason = str(rec.get("reason_code") or status)
            bucket = af_reason_codes.setdefault(status, {})
            bucket[reason] = bucket.get(reason, 0) + 1

    af_dir = staging_dir / "raw" / "adjustment_factors"
    af_csv_files = sorted(af_dir.glob("*.csv")) if af_dir.is_dir() else []
    af_csv_count = len(af_csv_files)
    expected_af_files = int(af_recomputed.get("downloaded", 0))
    if af_csv_count != expected_af_files:
        errors.append(
            f"adjustment_factor_file_count_mismatch:disk={af_csv_count}:summary_downloaded={expected_af_files}"
        )

    # bounded deep check of adjustment factor CSVs
    sample = downloaded_af
    sampled = False
    if max_files_deep_check and len(downloaded_af) > max_files_deep_check:
        sample = downloaded_af[:max_files_deep_check]
        sampled = True
    af_status_counts: dict[str, int] = {}
    af_problem: list[dict[str, Any]] = []
    for sym, rec in sample:
        file_ref = rec.get("file")
        path = Path(str(file_ref)) if file_ref else (af_dir / f"{sym}.csv")
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            fnd = FileFinding(symbol=sym, status="invalid", reason_codes=["file_missing_on_disk"])
        else:
            fnd = _audit_baostock_adjustment_factor_csv(path, sym)
        af_status_counts[fnd.status] = af_status_counts.get(fnd.status, 0) + 1
        if fnd.status != "ok":
            af_problem.append(fnd.as_dict())

    bad_af = int(af_status_counts.get("invalid", 0))

    # BSE unsupported must be reported as unsupported, never failed/success.
    unsupported_bse = int(af_recomputed.get("unsupported_bse", 0))

    audit = {
        "provider": "baostock",
        "factor_start_date": str(summary.get("factor_start_date") or ""),
        "factor_end_date": str(summary.get("factor_end_date") or ""),
        "index_snapshot_date": str(summary.get("index_snapshot_date") or ""),
        "static_tables": static_findings,
        "adjustment_factors": {
            "summary_counts": {str(k): int(v) for k, v in af_recomputed.items()},
            "reason_codes": af_reason_codes,
            "csv_file_count_on_disk": af_csv_count,
            "expected_file_count": expected_af_files,
            "unsupported_bse": unsupported_bse,
            "deep_check": {
                "checked": len(sample),
                "sampled": sampled,
                "max_files_deep_check": int(max_files_deep_check),
                "status_counts": af_status_counts,
                "problem_files": af_problem[:200],
                "problem_file_total": len(af_problem),
            },
        },
        "warnings": warnings,
        "errors": errors,
    }
    audit["integrity_ok"] = not errors and bad_af == 0
    return audit


# --------------------------------------------------------------------------- #
# top-level audit
# --------------------------------------------------------------------------- #
def build_staged_provider_evidence_audit(
    staging_dir: str | Path,
    *,
    project_root: str | Path | None = None,
    max_files_deep_check: int = 40,
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """Audit a single staged vendor download pack (mootdx or baostock).

    Read-only. Never fetches network data, never imports, never writes canonical
    market data. Only writes an audit JSON/Markdown when ``write=True``.
    """

    root = _root(project_root)
    staging_dir = Path(staging_dir)
    if not staging_dir.is_absolute():
        staging_dir = root / staging_dir
    now = _now_utc()

    base: dict[str, Any] = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "staged_provider_evidence_audit",
        "generated_at": now_text(),
        "network": False,
        "is_real_order": False,
        "formal_eligible": False,
        "canonical_imported": False,
        "staging_dir": _rel(staging_dir, root),
        "disclaimer": DISCLAIMER,
    }

    if not staging_dir.is_dir():
        base.update(
            {
                "ok": False,
                "provider": "unknown",
                "recommended_action": "repair_staging",
                "failed_reason": "staging_dir_not_found",
                "errors": ["staging_dir_not_found"],
            }
        )
        return _finish(base, output_path, report_path, root, write)

    summary_path = staging_dir / "summary.json"
    summary = read_json(summary_path, {}) if summary_path.exists() else {}
    if not isinstance(summary, dict):
        summary = {}

    provider = detect_staged_provider(summary, staging_dir)
    base["provider"] = provider
    liveness = _classify_liveness(summary, staging_dir, now)
    base.update(liveness)
    base["summary_present"] = bool(summary)

    # Running gate: never draw completeness/importability conclusions.
    if liveness["liveness"] == "running":
        base.update(
            {
                "recommended_action": "wait_for_download_completion",
                "audit": {},
                "warnings": ["download_still_running_audit_deferred"],
                "errors": [],
                "message": "下载仍在进行中，暂不做完整性或可导入判断。",
            }
        )
        return _finish(base, output_path, report_path, root, write)

    if not summary:
        base.update(
            {
                "ok": False,
                "recommended_action": "repair_staging",
                "failed_reason": "summary_json_missing",
                "errors": ["summary_json_missing"],
            }
        )
        return _finish(base, output_path, report_path, root, write)

    if provider == "mootdx":
        audit = _audit_mootdx(staging_dir, summary, root, max_files_deep_check=max_files_deep_check)
    elif provider == "baostock":
        audit = _audit_baostock(staging_dir, summary, root, max_files_deep_check=max_files_deep_check)
    else:
        base.update(
            {
                "ok": False,
                "recommended_action": "repair_staging",
                "failed_reason": f"unsupported_provider:{provider}",
                "errors": [f"unsupported_provider:{provider}"],
            }
        )
        return _finish(base, output_path, report_path, root, write)

    integrity_ok = bool(audit.get("integrity_ok"))
    base["audit"] = audit
    base["warnings"] = list(audit.get("warnings") or [])
    base["errors"] = list(audit.get("errors") or [])
    base["recommended_action"] = (
        "ready_for_manual_import_review" if integrity_ok else "repair_staging"
    )
    base["ok"] = True
    return _finish(base, output_path, report_path, root, write)


def _finish(
    payload: dict[str, Any],
    output_path: str | Path | None,
    report_path: str | Path | None,
    root: Path,
    write: bool,
) -> dict[str, Any]:
    # Contract guard: recommended_action must be one of the three allowed values.
    action = payload.get("recommended_action")
    if action not in _RECOMMENDED_ACTIONS:
        payload["recommended_action"] = "repair_staging"
        payload.setdefault("errors", []).append(f"invalid_recommended_action:{action}")
    if write:
        out = Path(output_path) if output_path else (RESULTS_DIR / "staged_provider_evidence_audit_latest.json")
        if not out.is_absolute():
            out = root / out
        write_json(out, payload)
        payload["path"] = _rel(out, root)
        rep = Path(report_path) if report_path else (REPORTS_DIR / "staged_provider_evidence_audit_latest.md")
        if not rep.is_absolute():
            rep = root / rep
        write_text(rep, render_staged_provider_evidence_report(payload))
        payload["report_path"] = _rel(rep, root)
    return payload


# --------------------------------------------------------------------------- #
# markdown report
# --------------------------------------------------------------------------- #
def render_staged_provider_evidence_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 暂存数据源证据审计报告",
        "",
        payload.get("disclaimer", DISCLAIMER),
        "",
        "## 概要",
        "",
        f"- 暂存目录：{payload.get('staging_dir', '-')}",
        f"- provider：{payload.get('provider', '-')}",
        f"- 下载状态：{payload.get('download_status', '-')}",
        f"- 存活判定：{payload.get('liveness', '-')}",
        f"- recommended_action：{payload.get('recommended_action', '-')}",
        f"- formal_eligible：{payload.get('formal_eligible')}（暂存数据不是正式策略证据）",
        "",
    ]
    if payload.get("recommended_action") == "wait_for_download_completion":
        lines.extend(
            [
                "## 等待下载完成",
                "",
                "- 下载仍在进行中，本审计不做完整性或可导入判断。",
                "- 仅可只读监控 summary/log，不得暂停、重启或修改下载数据。",
                "",
            ]
        )
        return "\n".join(lines)

    audit = payload.get("audit") or {}
    errors = payload.get("errors") or []
    warnings = payload.get("warnings") or []
    lines.append("## 审计结果")
    lines.append("")
    if payload.get("provider") == "mootdx":
        lines.append(f"- 磁盘 CSV 数：{audit.get('csv_file_count_on_disk', '-')}")
        lines.append(f"- 预期文件数（downloaded+partial）：{audit.get('expected_file_count', '-')}")
        lines.append(f"- summary 计数：{audit.get('summary_counts', {})}")
        lines.append(f"- 重算状态计数：{audit.get('recomputed_status_counts', {})}")
        lines.append(f"- 非 downloaded 状态 reason_code：{audit.get('non_ok_reason_codes', {})}")
        deep = audit.get("deep_check", {})
        lines.append(
            f"- 抽样深检：checked={deep.get('checked')} sampled={deep.get('sampled')} "
            f"状态分布={deep.get('file_status_counts', {})} 问题文件数={deep.get('problem_file_total')}"
        )
    elif payload.get("provider") == "baostock":
        lines.append(f"- 静态表：{ {k: v.get('status') for k, v in (audit.get('static_tables') or {}).items()} }")
        af = audit.get("adjustment_factors", {})
        lines.append(f"- 复权因子状态计数：{af.get('summary_counts', {})}")
        lines.append(f"- 复权因子磁盘 CSV 数：{af.get('csv_file_count_on_disk', '-')}（预期 {af.get('expected_file_count', '-')}）")
        lines.append(f"- 北交所 unsupported：{af.get('unsupported_bse', 0)}（按 unsupported 记录，不误报成功/失败）")
        lines.append(f"- 复权因子 reason_code：{af.get('reason_codes', {})}")
        deep = af.get("deep_check", {})
        lines.append(
            f"- 抽样深检：checked={deep.get('checked')} sampled={deep.get('sampled')} "
            f"状态分布={deep.get('status_counts', {})} 问题文件数={deep.get('problem_file_total')}"
        )
    lines.append("")
    lines.append("## 问题与警告")
    lines.append("")
    if errors:
        for err in errors:
            lines.append(f"- 错误：{err}")
    else:
        lines.append("- 未发现阻断性完整性错误。")
    for warn in warnings:
        lines.append(f"- 警告：{warn}")
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 本审计只读，不写 data/daily、data/intraday、data/metadata、benchmark。",
            "- 不运行任何 import --apply；ready_for_manual_import_review 仅表示可进入人工复核，不代表已导入或数据正式化。",
            "- 暂存数据不构成正式策略证据。",
            "",
        ]
    )
    return "\n".join(lines)


def _aggregate_recommended_action(pack_payloads: list[dict[str, Any]]) -> str:
    """Aggregate per-pack verdicts into one conservative recommended_action.

    Fail-closed precedence: if any pack is still running -> wait; else if any
    pack needs repair -> repair_staging; else ready_for_manual_import_review.
    An empty set of packs is treated as repair_staging (nothing to review).
    """

    actions = {str(p.get("recommended_action") or "") for p in pack_payloads}
    if not pack_payloads:
        return "repair_staging"
    if "wait_for_download_completion" in actions:
        return "wait_for_download_completion"
    if "repair_staging" in actions:
        return "repair_staging"
    if actions == {"ready_for_manual_import_review"}:
        return "ready_for_manual_import_review"
    return "repair_staging"


def build_staged_provider_evidence_audit_report(
    *,
    project_root: str | Path | None = None,
    provider: str = "all",
    staging_dir: str | Path | None = None,
    sample_limit: int = 40,
) -> dict[str, Any]:
    """Audit all (or one) staged vendor packs and return an aggregate payload.

    Read-only. Discovers mootdx/baostock packs under ``data/vendor_staging`` (or
    audits a single explicit ``staging_dir``), runs the per-pack audit, and
    aggregates a conservative ``recommended_action``. Never fetches network data,
    never imports, never writes canonical market data.
    """

    root = _root(project_root)
    packs: list[tuple[str, Path]]
    if staging_dir:
        sdir = Path(staging_dir)
        if not sdir.is_absolute():
            sdir = root / sdir
        packs = [(detect_staged_provider(read_json(sdir / "summary.json", {}) if (sdir / "summary.json").exists() else {}, sdir), sdir)]
    else:
        packs = discover_staged_packs(root, provider=provider)

    pack_payloads: list[dict[str, Any]] = []
    for _prov, sdir in packs:
        pack = build_staged_provider_evidence_audit(
            sdir,
            project_root=root,
            max_files_deep_check=sample_limit,
            write=False,
        )
        pack["pack_name"] = sdir.name
        # surface a compact reason_code roll-up for the CLI/report
        pack["reason_code_counts"] = _rollup_reason_codes(pack)
        pack_payloads.append(pack)

    any_running = any(p.get("liveness") == "running" for p in pack_payloads)
    aggregate_action = _aggregate_recommended_action(pack_payloads)
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "staged_provider_evidence_audit_report",
        "generated_at": now_text(),
        "network": False,
        "is_real_order": False,
        "formal_eligible": False,
        "canonical_imported": False,
        "provider_filter": provider,
        "status": "ok",
        "any_download_running": bool(any_running),
        "recommended_action": aggregate_action,
        "pack_count": len(pack_payloads),
        "providers": pack_payloads,
        "disclaimer": DISCLAIMER,
    }


def _rollup_reason_codes(pack: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    audit = pack.get("audit") if isinstance(pack.get("audit"), dict) else {}
    # mootdx problem files
    deep = audit.get("deep_check") if isinstance(audit.get("deep_check"), dict) else {}
    for row in deep.get("problem_files") or []:
        for code in (row.get("reason_codes") or []):
            key = str(code).split(":")[0]
            counts[key] = counts.get(key, 0) + 1
    # baostock adjustment factor problem files
    af = audit.get("adjustment_factors") if isinstance(audit.get("adjustment_factors"), dict) else {}
    af_deep = af.get("deep_check") if isinstance(af.get("deep_check"), dict) else {}
    for row in af_deep.get("problem_files") or []:
        for code in (row.get("reason_codes") or []):
            key = str(code).split(":")[0]
            counts[key] = counts.get(key, 0) + 1
    for err in pack.get("errors") or []:
        key = str(err).split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


def render_staged_provider_evidence_aggregate_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 暂存数据源证据审计（汇总）",
        "",
        payload.get("disclaimer", DISCLAIMER),
        "",
        "## 概要",
        "",
        f"- provider 过滤：{payload.get('provider_filter', 'all')}",
        f"- 发现暂存包数量：{payload.get('pack_count', 0)}",
        f"- 是否有下载仍在进行：{payload.get('any_download_running')}",
        f"- 汇总 recommended_action：{payload.get('recommended_action')}",
        f"- formal_eligible：{payload.get('formal_eligible')}（暂存数据不是正式策略证据）",
        "",
        "## 各暂存包",
        "",
    ]
    for pack in payload.get("providers") or []:
        if not isinstance(pack, dict):
            continue
        lines.append(
            f"- {pack.get('provider')}/{pack.get('pack_name')}: "
            f"liveness={pack.get('liveness')} "
            f"recommended_action={pack.get('recommended_action')} "
            f"reason_codes={pack.get('reason_code_counts', {})}"
        )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 本审计只读，不写 data/daily、data/intraday、data/metadata、benchmark。",
            "- 不运行任何 import --apply；仍在下载的包一律 wait_for_download_completion。",
            "- 暂存数据不构成正式策略证据。",
            "",
        ]
    )
    return "\n".join(lines)


def write_staged_provider_evidence_audit_outputs(
    *,
    project_root: str | Path | None = None,
    provider: str = "all",
    staging_dir: str | Path | None = None,
    sample_limit: int = 40,
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the aggregate audit and write JSON + Markdown (convenience for CLI).

    Writes only under ``results``/``reports``; never touches canonical data dirs.
    """

    root = _root(project_root)
    payload = build_staged_provider_evidence_audit_report(
        project_root=root,
        provider=provider,
        staging_dir=staging_dir,
        sample_limit=sample_limit,
    )
    out = Path(output_path) if output_path else DEFAULT_OUTPUT_PATH
    if not out.is_absolute():
        out = root / out
    write_json(out, payload)
    payload["output_path"] = _rel(out, root)
    rep = Path(report_path) if report_path else DEFAULT_REPORT_PATH
    if not rep.is_absolute():
        rep = root / rep
    write_text(rep, render_staged_provider_evidence_aggregate_report(payload))
    payload["report_path"] = _rel(rep, root)
    return payload


__all__ = [
    "build_staged_provider_evidence_audit",
    "build_staged_provider_evidence_audit_report",
    "write_staged_provider_evidence_audit_outputs",
    "discover_staged_packs",
    "detect_staged_provider",
    "render_staged_provider_evidence_report",
    "render_staged_provider_evidence_aggregate_report",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_REPORT_PATH",
    "DISCLAIMER",
]
