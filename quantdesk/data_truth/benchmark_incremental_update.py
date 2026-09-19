"""Incremental, schema-preserving updates for the local benchmark index series.

Why this module exists
----------------------
``data/benchmark/`` held three index files that stopped at 2026-07-10 while the
daily chain kept running.  A forensic read of the chain showed the reason: no
code path in the repository ever *writes* ``data/benchmark``.  The chain only
*reads* it, via ``data_update_orchestrator.latest_benchmark_date``, to report
``benchmark_staleness_days``.  So the staleness was measured faithfully every
day and never acted on.

The pre-existing repair helper (``metadata_evidence_status.repair_benchmark``)
cannot fill this gap:

- it truncates every series at a hardcoded ``<= "2026-05-31"``;
- it rewrites the whole file in one narrow schema, which would destroy
  ``hs300.csv``'s OHLCV columns and ``sh000001.csv``'s columns outright;
- it does not know about ``sh000001.csv`` at all, even though the market-brain
  and mainline-radar modules read that file specifically.

The three files genuinely carry three different schemas, each with live
consumers, so a single canonical shape is not available to converge on.  This
module therefore appends rows *in each file's own schema* and never rewrites
history.

Safety properties
-----------------
- **Append-only.** Existing bytes are never rewritten.  Rows are only added for
  sessions strictly after the local last date.
- **Overlap-verified, fail-closed.** Before appending, every date present both
  locally and remotely is compared on ``close``.  Any mismatch beyond
  ``_CLOSE_TOLERANCE`` aborts that file with a reason code instead of writing.
- **Byte hygiene preserved.** The files are UTF-8-with-BOM and CRLF-terminated.
  Appends reuse the observed line terminator and never re-encode the header.
- **No fabrication.** A column the remote frame cannot supply is written empty.
  ``hs300.csv``'s ``amount`` column is empty in all 5946 existing rows and stays
  empty; inventing a value there would be a data-truth violation.
- **Research only.** Benchmark series are reference context.  They are not a
  broker route, not an order path, and not investment advice.
"""

from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pandas as pd

__all__ = [
    "BENCHMARK_SPECS",
    "BenchmarkSpec",
    "BenchmarkUpdateResult",
    "default_index_fetcher",
    "plan_benchmark_appends",
    "update_benchmark_files",
]

# Sina's index endpoint is the only one reachable: Eastmoney's ``index_zh_a_hist``
# fails with RemoteDisconnected against the same hardcoded push2 hosts that broke
# the sector board fetch (see em_sector_provider for that forensic trail).
_SOURCE = "akshare.stock_zh_index_daily"
_PROVIDER = "akshare"

# Index levels are quoted to 2-3 decimals; 1e-4 is far below quote resolution
# and still catches a genuine series substitution.
_CLOSE_TOLERANCE = 1e-4

# Sina drops TLS connections intermittently; one transient EOF must not abort a
# whole update run.
_FETCH_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = 1.5


@dataclass(frozen=True)
class BenchmarkSpec:
    """How one local benchmark file maps onto a fetched index frame."""

    label: str
    filename: str
    remote_symbol: str
    #: Columns copied straight from the remote frame when present locally.
    passthrough: tuple[str, ...] = ("open", "high", "low", "close", "volume")
    #: Constant-valued columns, resolved at write time.
    constants: dict[str, str] = field(default_factory=dict)


BENCHMARK_SPECS: tuple[BenchmarkSpec, ...] = (
    BenchmarkSpec(label="hs300", filename="hs300.csv", remote_symbol="sh000300"),
    BenchmarkSpec(label="zz500", filename="zz500.csv", remote_symbol="sh000905"),
    BenchmarkSpec(label="sh000001", filename="sh000001.csv", remote_symbol="sh000001"),
)


@dataclass
class BenchmarkUpdateResult:
    label: str
    path: str
    status: str
    local_last_date: str = ""
    remote_last_date: str = ""
    local_last_date_after: str = ""
    target_date: str | None = None
    target_covered: bool | None = None
    freshness_status: str = "not_assessed"
    fetch_status: str = "not_requested"
    failure_stage: str = ""
    planned_dates: list[str] = field(default_factory=list)
    appended_dates: list[str] = field(default_factory=list)
    overlap_rows: int = 0
    overlap_max_close_diff: float = 0.0
    reason_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": self.path,
            "status": self.status,
            "local_last_date": self.local_last_date,
            "remote_last_date": self.remote_last_date,
            "local_last_date_after": self.local_last_date_after,
            "target_date": self.target_date,
            "target_covered": self.target_covered,
            "freshness_status": self.freshness_status,
            "fetch_status": self.fetch_status,
            "failure_stage": self.failure_stage,
            "planned_count": len(self.planned_dates),
            "planned_dates": list(self.planned_dates),
            "appended_count": len(self.appended_dates),
            "appended_dates": list(self.appended_dates),
            "overlap_rows": self.overlap_rows,
            "overlap_max_close_diff": self.overlap_max_close_diff,
            "reason_codes": list(self.reason_codes),
        }


def default_index_fetcher(symbol: str) -> pd.DataFrame:
    """Fetch one index history frame from the reachable Sina endpoint.

    Sina intermittently drops the TLS connection mid-handshake
    (``SSLEOFError``).  A single transient failure must not abort the whole
    update, so each symbol gets a small retry budget with linear backoff.
    """

    import akshare as ak  # type: ignore

    last_error: Exception | None = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            return ak.stock_zh_index_daily(symbol=symbol)
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last_error = exc
            if attempt < _FETCH_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(
        f"index_fetch_failed:{symbol}:{type(last_error).__name__}:{last_error}"
    )


def _validate_target_date(target_date: str | None) -> str | None:
    """Accept an explicit ISO session date; never infer one from wall-clock time."""

    if target_date is None:
        return None
    try:
        valid = isinstance(target_date, str) and date.fromisoformat(target_date).isoformat() == target_date
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("target_date must be a valid YYYY-MM-DD date")
    return target_date


def _normalize_remote(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("remote index response is not a DataFrame")
    out = frame.copy()
    if out.empty:
        return pd.DataFrame(columns=["date", "close"])
    if "date" not in out.columns:
        raise ValueError("remote index frame missing date column")
    if "close" not in out.columns:
        raise ValueError("remote index frame missing close column")
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    # Preserve duplicate rows so neither a bad target row nor an overlap
    # disagreement can disappear through keep-last normalization.
    out = out.dropna(subset=["date"])
    return out.sort_values("date").reset_index(drop=True)


def _read_local(path: Path) -> tuple[list[str], pd.DataFrame, str]:
    """Return (header columns, frame, line terminator) for a local benchmark CSV."""

    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("local_csv_contains_nul")
    terminator = "\r\n" if b"\r\n" in raw else "\n"
    # Parse the exact bytes that were inspected; a second file read could see
    # different content. Do not let pandas infer an index, pad short records,
    # rename duplicate headers, or truncate NUL suffixes in damaged input.
    reader = csv.reader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    columns = next(reader, [])
    if not columns or any(not column.strip() for column in columns) or len(set(columns)) != len(columns):
        raise ValueError("local_csv_header_invalid")
    records: list[list[str]] = []
    for row in reader:
        if len(row) != len(columns):
            raise ValueError(
                f"local_csv_column_count_mismatch:line={reader.line_num}:"
                f"expected={len(columns)}:actual={len(row)}"
            )
        records.append(row)
    frame = pd.DataFrame(records, columns=columns, dtype=str)
    return columns, frame, terminator


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_number(value: Any) -> str:
    """Render a remote numeric like the surrounding rows do: no forced decimals."""

    number = _as_float(value)
    if number is None:
        return ""
    if number == int(number) and abs(number) < 1e18:
        return str(int(number))
    return repr(number)


def _date_keys(frame: pd.DataFrame) -> pd.Series:
    if "date" not in frame.columns:
        return pd.Series(index=frame.index, dtype=object)
    return pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")


def _usable_price_row(row: pd.Series, columns: Sequence[str]) -> bool:
    prices = {
        column: _as_float(row.get(column))
        for column in ("open", "high", "low", "close")
        if column in columns
    }
    if "close" not in prices or any(value is None or value <= 0 for value in prices.values()):
        return False
    if "high" in prices and prices["high"] < max(prices.values()):
        return False
    if "low" in prices and prices["low"] > min(prices.values()):
        return False
    return True


def _frame_freshness(frame: pd.DataFrame, target_date: str | None) -> tuple[str, str, bool | None]:
    dates = _date_keys(frame)
    last_date = max(dates.dropna(), default="")
    if target_date is None:
        return last_date, "target_not_supplied", None
    if "date" not in frame.columns or "close" not in frame.columns:
        return last_date, "not_assessed", None
    target_rows = frame.loc[dates == target_date]
    if not target_rows.empty:
        usable = len(target_rows) == 1 and _usable_price_row(target_rows.iloc[0], frame.columns)
        return last_date, "current" if usable else "target_unusable", usable
    return last_date, "target_missing" if last_date > target_date else "behind_target", False


def _record_local_evidence(result: BenchmarkUpdateResult, frame: pd.DataFrame, *, initial: bool = False) -> None:
    last_date, freshness, covered = _frame_freshness(frame, result.target_date)
    if initial:
        result.local_last_date = last_date
    result.local_last_date_after = last_date
    result.freshness_status = freshness
    result.target_covered = covered


def _record_failure(result: BenchmarkUpdateResult, stage: str, exc: Exception) -> None:
    result.status = "failed"
    result.failure_stage = result.failure_stage or stage
    result.reason_codes.append(f"{stage}:{type(exc).__name__}:{exc}")


def plan_benchmark_appends(
    spec: BenchmarkSpec,
    local_columns: Sequence[str],
    local_frame: pd.DataFrame,
    remote_frame: pd.DataFrame,
    *,
    fetched_at: str,
    target_date: str | None = None,
) -> tuple[list[list[str]], BenchmarkUpdateResult]:
    """Build the exact rows to append, or a fail-closed result explaining why not.

    Pure function: reads frames, writes nothing. ``remote_frame`` uses the
    normalized date column supplied by the updater. Planned rows never count
    as appended rows or improve the local target-day coverage.
    """

    target_date = _validate_target_date(target_date)
    result = BenchmarkUpdateResult(
        label=spec.label, path=spec.filename, status="pending", target_date=target_date
    )
    _record_local_evidence(result, local_frame, initial=True)
    if "date" not in local_columns or "close" not in local_columns:
        result.status = "skipped"
        result.reason_codes.append("local_schema_missing_date_or_close")
        return [], result

    local_dates = _date_keys(local_frame)
    result.remote_last_date, remote_freshness, _ = _frame_freshness(remote_frame, target_date)
    if remote_freshness in {"behind_target", "target_missing", "target_unusable"}:
        result.reason_codes.append(f"remote_{remote_freshness}")
    if local_dates.isna().any() or local_dates.duplicated().any():
        result.status = "blocked"
        result.reason_codes.append("local_dates_unusable_or_duplicated")
        return [], result
    if remote_frame.empty:
        # An empty successful fetch adds nothing. Local verified target-day
        # coverage still stands; a T-1 file remains explicitly incomplete.
        result.status = "up_to_date" if result.target_covered is True else "no_usable_rows"
        result.reason_codes.append("remote_no_rows")
        return [], result
    if remote_frame["date"].duplicated().any():
        result.status = "blocked"
        result.reason_codes.append("remote_duplicate_dates")
        return [], result
    local_close = {
        date: _as_float(value)
        for date, value in zip(local_dates, local_frame["close"].tolist())
    }
    remote_close = {
        str(row["date"]): _as_float(row["close"])
        for _, row in remote_frame.iterrows()
    }

    # Fail closed on any historical disagreement: a silently substituted series
    # would otherwise be stitched onto a different index's history.
    max_diff = 0.0
    mismatches: list[str] = []
    for date, local_value in local_close.items():
        remote_value = remote_close.get(date)
        if local_value is None or remote_value is None:
            continue
        result.overlap_rows += 1
        diff = abs(local_value - remote_value)
        max_diff = max(max_diff, diff)
        if diff > _CLOSE_TOLERANCE:
            mismatches.append(f"{date}:local={local_value}:remote={remote_value}")
    result.overlap_max_close_diff = max_diff
    if mismatches:
        result.status = "blocked"
        result.reason_codes.append("overlap_close_mismatch")
        result.reason_codes.extend(mismatches[:5])
        return [], result
    if not result.overlap_rows:
        result.status = "blocked"
        result.reason_codes.append("no_verifiable_overlap")
        return [], result

    new_dates = [date for date in remote_close if date > result.local_last_date]
    if not new_dates:
        result.status = "up_to_date" if result.target_covered is True else "unchanged"
        return [], result

    remote_by_date = {str(row["date"]): row for _, row in remote_frame.iterrows()}
    constants = {
        "benchmark_symbol": spec.remote_symbol,
        "source": _SOURCE,
        "provider": _PROVIDER,
        "fetched_at": fetched_at,
        **spec.constants,
    }

    rows: list[list[str]] = []
    previous_close = local_close.get(result.local_last_date)
    for date in sorted(new_dates):
        remote_row = remote_by_date[date]
        close_value = _as_float(remote_row.get("close"))
        if close_value is None or close_value <= 0:
            result.reason_codes.append(f"remote_close_unusable:{date}")
            continue
        # A narrow local schema must not hide invalid OHLC supplied remotely;
        # locally required price columns must also exist in the source row.
        if not _usable_price_row(remote_row, (*local_columns, *remote_frame.columns)):
            result.reason_codes.append(f"remote_ohlc_unusable:{date}")
            continue
        cells: list[str] = []
        for column in local_columns:
            if column == "date":
                cells.append(date)
            elif column == "pct_chg":
                # Derived from the immediately preceding close, matching how the
                # existing rows were produced. First-ever row stays blank.
                if previous_close in (None, 0):
                    cells.append("")
                else:
                    cells.append(repr((close_value / previous_close - 1.0) * 100.0))
            elif column in constants:
                cells.append(constants[column])
            elif column in spec.passthrough and column in remote_frame.columns:
                cells.append(_format_number(remote_row.get(column)))
            else:
                # No remote source for this column. Leave it empty rather than
                # invent a value (hs300.csv's ``amount`` is empty in all rows).
                cells.append("")
        rows.append(cells)
        previous_close = close_value

    result.planned_dates = [row[local_columns.index("date")] for row in rows]
    result.status = "planned" if rows else "no_usable_rows"
    return rows, result


def _append_rows(path: Path, rows: Iterable[Sequence[str]], terminator: str) -> None:
    """Append rows byte-exactly, preserving the file's existing terminator."""

    payload = "".join(",".join(row) + terminator for row in rows)
    if not payload:
        return
    existing = path.read_bytes()
    prefix = b"" if existing.endswith(terminator.encode("utf-8")) else terminator.encode("utf-8")
    with path.open("ab") as handle:
        handle.write(prefix + payload.encode("utf-8"))


def _append_and_verify(
    path: Path,
    columns: Sequence[str],
    rows: list[list[str]],
    terminator: str,
    result: BenchmarkUpdateResult,
) -> None:
    try:
        _append_rows(path, rows, terminator)
    except Exception as exc:  # noqa: BLE001 - an interrupted append may have written some rows
        _record_failure(result, "append", exc)
    try:
        actual_columns, actual_frame, _ = _read_local(path)
        _record_local_evidence(result, actual_frame)
        if list(columns) != actual_columns:
            raise ValueError("local_schema_changed_during_append")
        date_index = columns.index("date")
        for row in rows:
            matches = actual_frame.loc[actual_frame["date"] == row[date_index]]
            if len(matches) == 1 and matches.iloc[0].tolist() == row:
                result.appended_dates.append(row[date_index])
        if result.appended_dates != result.planned_dates:
            _record_failure(result, "verify", ValueError("appended_rows_not_fully_persisted"))
    except Exception as exc:  # noqa: BLE001 - post-write evidence is unknown, not successful
        result.local_last_date_after = ""
        result.target_covered = None
        result.freshness_status = "not_assessed" if result.target_date else "target_not_supplied"
        _record_failure(result, "verify", exc)
    if result.status != "failed":
        result.status = "appended"


def _summarize_results(
    results: Sequence[dict[str, Any]], target_date: str | None, dry_run: bool
) -> tuple[str, str, bool | None]:
    statuses = {item["status"] for item in results}
    coverage = [item["target_covered"] for item in results]
    if target_date is None:
        freshness, covered = "target_not_supplied", None
    elif not coverage or all(value is None for value in coverage):
        freshness, covered = "not_assessed", None
    elif all(value is True for value in coverage):
        freshness, covered = "current", True
    else:
        freshness_values = {item["freshness_status"] for item in results}
        freshness = next(iter(freshness_values)) if len(freshness_values) == 1 else "incomplete"
        covered = False if False in coverage else None

    if statuses & {"blocked", "failed", "no_usable_rows"}:
        overall = "degraded"
    elif not results or statuses == {"skipped"}:
        overall = "skipped"
    elif dry_run:
        overall = "dry_run"
    elif "skipped" in statuses or (target_date is not None and covered is not True):
        overall = "partial"
    elif "appended" in statuses:
        overall = "updated"
    elif covered is True:
        overall = "up_to_date"
    else:
        overall = "unchanged"
    return overall, freshness, covered


def update_benchmark_files(
    project_root: str | Path,
    *,
    specs: Sequence[BenchmarkSpec] = BENCHMARK_SPECS,
    fetcher: Callable[[str], pd.DataFrame] = default_index_fetcher,
    dry_run: bool = False,
    fetched_at: str | None = None,
    target_date: str | None = None,
) -> dict[str, Any]:
    """Append missing sessions to each local benchmark file.

    Returns a JSON-safe payload describing what was verified and what changed.
    ``target_date`` is supplied by the caller's calendar decision. Without it,
    an unchanged file is not evidence of freshness. ``appended`` still denotes
    actual writes even when the requested target remains missing.
    """

    target_date = _validate_target_date(target_date)
    root = Path(project_root).resolve()
    benchmark_dir = root / "data" / "benchmark"
    stamp = fetched_at or datetime.now().astimezone().isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []

    for spec in specs:
        path = benchmark_dir / spec.filename
        result = BenchmarkUpdateResult(
            label=spec.label, path=spec.filename, status="pending", target_date=target_date,
            freshness_status="not_assessed" if target_date else "target_not_supplied",
        )
        if not path.exists():
            result.status = "skipped"
            result.reason_codes.append("local_file_missing")
            results.append(result.as_dict())
            continue
        try:
            columns, local_frame, terminator = _read_local(path)
            _record_local_evidence(result, local_frame, initial=True)
        except Exception as exc:  # noqa: BLE001 - reason must reach the payload
            _record_failure(result, "local_read", exc)
            results.append(result.as_dict())
            continue
        if "date" not in columns or "close" not in columns:
            result.status = "skipped"
            result.reason_codes.append("local_schema_missing_date_or_close")
            results.append(result.as_dict())
            continue
        try:
            raw_remote = fetcher(spec.remote_symbol)
            result.fetch_status = "succeeded"
        except Exception as exc:  # noqa: BLE001 - keep the already-read local evidence
            result.fetch_status = "failed"
            _record_failure(result, "fetch", exc)
            results.append(result.as_dict())
            continue
        try:
            remote_frame = _normalize_remote(raw_remote)
        except Exception as exc:  # noqa: BLE001 - response quality is distinct from request failure
            _record_failure(result, "remote_validation", exc)
            results.append(result.as_dict())
            continue
        try:
            rows, result = plan_benchmark_appends(
                spec, columns, local_frame, remote_frame, fetched_at=stamp, target_date=target_date
            )
            result.fetch_status = "succeeded"
        except Exception as exc:  # noqa: BLE001 - one file must not abort the remaining indices
            _record_failure(result, "plan", exc)
            results.append(result.as_dict())
            continue
        if rows and not dry_run:
            _append_and_verify(path, columns, rows, terminator, result)
        elif rows and dry_run:
            result.status = "dry_run"
        results.append(result.as_dict())

    overall, freshness, covered = _summarize_results(results, target_date, dry_run)

    return {
        "task_id": "benchmark_incremental_update",
        "status": overall,
        "target_date": target_date,
        "target_covered": covered,
        "freshness_status": freshness,
        "research_only": True,
        "dry_run": bool(dry_run),
        "fetched_at": stamp,
        "source": _SOURCE,
        "results": results,
        "disclaimer": "基准指数仅作参考对照；不接券商、不真实下单、不构成投资建议。",
    }
