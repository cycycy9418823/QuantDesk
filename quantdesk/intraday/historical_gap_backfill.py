"""End-to-end audited recovery for the five Round 13 historical minute gaps."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols
from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch
from quantdesk.price_basis import (
    PRICE_BASIS_RAW,
    classify_frame_price_basis,
    validate_price_frame_set,
)
from quantdesk.shared import now_text
from quantdesk.utils import atomic_write_json


ROUND13_MINUTE_BACKFILL_ALLOWLIST = frozenset(
    {
        ("920000", "2026-07-28"),
        ("920204", "2026-07-28"),
        ("920496", "2026-07-28"),
        ("920627", "2026-07-28"),
        ("920694", "2026-07-29"),
    }
)
APPROVED_ROUND13_MINUTE_SOURCES = frozenset(
    {"akshare_intraday", "mootdx_intraday"}
)
APPROVED_ROUND13_DAILY_SOURCES = frozenset(
    {
        "akshare_daily",
        "baostock_daily",
        "mootdx_daily",
        "sina_daily",
        "tencent_daily",
    }
)
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice."
)
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_SAFE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_COMPARE_COLUMNS = (
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "price_basis",
)


@dataclass(frozen=True)
class DailyMinuteCrosscheckThresholds:
    ohlc_absolute: float = 1e-8
    volume_absolute: float = 100.0
    volume_relative: float = 0.001
    amount_absolute: float = 2000.0
    amount_relative: float = 0.001

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"invalid_crosscheck_threshold:{name}")

    def to_dict(self) -> dict[str, float]:
        return {
            "ohlc_absolute": self.ohlc_absolute,
            "volume_absolute": self.volume_absolute,
            "volume_relative": self.volume_relative,
            "amount_absolute": self.amount_absolute,
            "amount_relative": self.amount_relative,
        }


ROUND13_CROSSCHECK_THRESHOLDS = DailyMinuteCrosscheckThresholds()
ROUND13_CROSSCHECK_THRESHOLDS_SHA256 = hashlib.sha256(
    json.dumps(
        ROUND13_CROSSCHECK_THRESHOLDS.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
).hexdigest()


def _strict_symbol(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit() and len(text) <= 6:
        text = text.zfill(6)
    return text if _SYMBOL_PATTERN.fullmatch(text) else ""


def _strict_date(value: Any) -> str:
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _staging_root_bound_to_run(staging_root: Path, run_id: str) -> bool:
    """Require the staging root to be exactly ``<run_dir>/staging`` for this run.

    Membership tests such as ``run_id in resolved.parts`` are too weak: any path
    that merely happens to contain a component with that name would satisfy them
    (``.../round13-x/decoy/staging``), so the check proves nothing about which
    run actually owns the directory. Bind the structure instead.
    """

    if not run_id or _SAFE_RUN_ID_PATTERN.fullmatch(str(run_id)) is None:
        return False
    try:
        resolved = Path(staging_root).resolve(strict=True)
    except OSError:
        return False
    return resolved.name == "staging" and resolved.parent.name == str(run_id)


def _descendant(root: Path, value: Path, *, role: str) -> Path:
    raw_root = Path(root)
    resolved_root = raw_root.resolve(strict=True)
    if raw_root.is_symlink() or raw_root.absolute() != resolved_root:
        raise ValueError(f"allowed_root_reparse_or_alias:{role}:{raw_root}")
    candidate = value if value.is_absolute() else resolved_root / value
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path_outside_allowed_root:{role}:{resolved}") from exc
    return resolved


def validate_round13_backfill_scope(
    requested: Sequence[tuple[Any, Any]],
    *,
    require_complete_scope: bool = False,
) -> list[tuple[str, str]]:
    """Reject any pair outside the immutable five-pair authorization."""

    normalized: list[tuple[str, str]] = []
    for raw_symbol, raw_date in requested:
        pair = (_strict_symbol(raw_symbol), _strict_date(raw_date))
        if pair not in ROUND13_MINUTE_BACKFILL_ALLOWLIST:
            raise ValueError(f"backfill_scope_not_authorized:{raw_symbol}:{raw_date}")
        if pair in normalized:
            raise ValueError(f"duplicate_backfill_pair:{pair[0]}:{pair[1]}")
        normalized.append(pair)
    if require_complete_scope and set(normalized) != set(ROUND13_MINUTE_BACKFILL_ALLOWLIST):
        raise ValueError("backfill_scope_incomplete")
    return sorted(normalized)


def _expected_session(target_date: str) -> pd.DatetimeIndex:
    morning = pd.date_range(
        f"{target_date} 09:31:00", f"{target_date} 11:30:00", freq="min"
    )
    afternoon = pd.date_range(
        f"{target_date} 13:01:00", f"{target_date} 15:00:00", freq="min"
    )
    return morning.append(afternoon)


def _relative_difference(left: float, right: float) -> float:
    return abs(left - right) / max(abs(right), 1.0)


def _numeric_frame(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame | None:
    if set(columns) - set(frame.columns):
        return None
    numeric = frame[list(columns)].apply(pd.to_numeric, errors="coerce")
    return None if numeric.isna().any().any() else numeric


def _positive_source_and_time(
    frame: pd.DataFrame,
    *,
    approved_sources: frozenset[str],
) -> tuple[bool, list[str], list[str]]:
    sources = sorted(
        {
            str(item or "").strip()
            for item in frame.get("source", pd.Series(dtype=str))
            if str(item or "").strip()
        }
    )
    captured = sorted(
        {
            str(item or "").strip()
            for item in frame.get("updated_at", pd.Series(dtype=str))
            if str(item or "").strip()
        }
    )
    valid = (
        bool(sources)
        and set(sources) <= set(approved_sources)
        and bool(captured)
        and all(_parse_timestamp(item) is not None for item in captured)
    )
    return valid, sources, captured


def _minute_price_only_false(frame: pd.DataFrame) -> bool:
    if "price_only_intraday" not in frame.columns or frame.empty:
        return False
    values = {
        str(item).strip().lower()
        for item in frame["price_only_intraday"]
        if str(item).strip()
    }
    return bool(values) and values <= _FALSE_VALUES


def _daily_basis_preflight(symbol: str, frame: pd.DataFrame) -> dict[str, Any]:
    """Classify the untouched daily frame before any date filtering."""

    basis, reason = classify_frame_price_basis(frame)
    validation = validate_price_frame_set(
        {symbol: frame}, consumer="historical_intraday_replay"
    )
    eligible = (
        basis == PRICE_BASIS_RAW
        and not reason
        and validation.get("eligible") is True
        and validation.get("accepted_price_basis") == PRICE_BASIS_RAW
    )
    return {
        "status": "passed" if eligible else "blocked",
        "eligible": eligible,
        "price_basis": basis,
        "reason_code": reason,
        "validation": validation,
    }


def audit_round13_minute_backfill_item(
    symbol: str,
    target_date: str,
    *,
    minute_frame: pd.DataFrame,
    daily_frame: pd.DataFrame,
    staging_path: Path,
    daily_path: Path,
    staging_root: Path,
    daily_root: Path,
    staging_sha256: str,
    run_id: str,
    captured_at: str,
) -> dict[str, Any]:
    """Fail closed unless disk-bound staged evidence matches a raw daily bar."""

    normalized_symbol, target = validate_round13_backfill_scope(
        [(symbol, target_date)]
    )[0]
    limits = ROUND13_CROSSCHECK_THRESHOLDS
    minute = minute_frame.copy() if isinstance(minute_frame, pd.DataFrame) else pd.DataFrame()
    daily = daily_frame.copy() if isinstance(daily_frame, pd.DataFrame) else pd.DataFrame()

    # Price Data Contract ordering is deliberate: this call must remain before
    # every daily date filter, sort, dedupe, aggregate, or comparison below.
    daily_basis = _daily_basis_preflight(normalized_symbol, daily)
    reasons: list[str] = []
    if daily_basis["eligible"] is not True:
        reasons.append("daily_price_basis_not_raw")

    try:
        bound_path = _descendant(Path(staging_root), Path(staging_path), role="staging_path")
        bound_daily_path = _descendant(Path(daily_root), Path(daily_path), role="daily_path")
        roots_valid = True
    except (OSError, ValueError):
        bound_path = Path(staging_path)
        bound_daily_path = Path(daily_path)
        roots_valid = False
    path_valid = (
        roots_valid
        and bound_path.is_file()
        and not bound_path.is_symlink()
        and bound_path.stem == normalized_symbol
        and bound_daily_path.is_file()
        and not bound_daily_path.is_symlink()
        and bound_daily_path.stem == normalized_symbol
        and _staging_root_bound_to_run(Path(staging_root), str(run_id))
    )
    actual_stage_sha = _sha256_file(bound_path) if path_valid else ""
    hash_valid = (
        _SHA256_PATTERN.fullmatch(str(staging_sha256 or "").lower()) is not None
        and actual_stage_sha == str(staging_sha256).lower()
    )
    if not path_valid:
        reasons.append("staging_path_or_run_binding_invalid")
    if not hash_valid:
        reasons.append("staging_hash_binding_invalid")

    required_minute = {
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "price_basis",
        "source",
        "updated_at",
        "price_only_intraday",
    }
    if minute.empty or required_minute - set(minute.columns):
        reasons.append("minute_required_columns_missing")
    timestamps = pd.to_datetime(minute.get("datetime"), errors="coerce")
    valid_timestamps = isinstance(timestamps, pd.Series) and not timestamps.isna().any()
    if not valid_timestamps:
        reasons.append("invalid_minute_timestamp")
        timestamp_count = 0
        timestamp_unique = False
        exact_grid = False
    else:
        timestamp_count = int(timestamps.nunique())
        timestamp_unique = timestamp_count == len(minute)
        exact_grid = list(timestamps) == list(_expected_session(target))
        if not timestamp_unique:
            reasons.append("duplicate_minute_timestamp")
        if not exact_grid:
            reasons.append("minute_session_grid_mismatch")
    observed_symbols = {
        _strict_symbol(item) for item in minute.get("symbol", pd.Series(dtype=str))
    }
    if observed_symbols != {normalized_symbol}:
        reasons.append("minute_symbol_mismatch")
    observed_dates = set(timestamps.dt.strftime("%Y-%m-%d")) if valid_timestamps else set()
    if observed_dates != {target}:
        reasons.append("minute_target_date_mismatch")
    minute_basis = {
        str(item or "").strip().lower()
        for item in minute.get("price_basis", pd.Series(dtype=str))
    }
    raw_minute_basis = minute_basis == {"raw"}
    if not raw_minute_basis:
        reasons.append("minute_price_basis_not_raw")
    price_only_false = _minute_price_only_false(minute)
    if not price_only_false:
        reasons.append("price_only_intraday_not_false")

    minute_provenance, minute_sources, minute_times = _positive_source_and_time(
        minute, approved_sources=APPROVED_ROUND13_MINUTE_SOURCES
    )
    captured_binding = (
        _parse_timestamp(captured_at) is not None
        and bool(minute_times)
        and captured_at == minute_times[-1]
    )
    if not minute_provenance:
        reasons.append("minute_provider_provenance_not_approved")
    if not captured_binding:
        reasons.append("captured_at_binding_invalid")

    minute_numeric = _numeric_frame(
        minute, ("open", "high", "low", "close", "volume", "amount")
    )
    if minute_numeric is None:
        reasons.append("minute_ohlcva_invalid")
    elif (
        (minute_numeric[["open", "high", "low", "close"]] <= 0).any().any()
        or (minute_numeric[["volume", "amount"]] < 0).any().any()
        or (
            minute_numeric["high"]
            < minute_numeric[["open", "low", "close"]].max(axis=1)
        ).any()
        or (
            minute_numeric["low"]
            > minute_numeric[["open", "high", "close"]].min(axis=1)
        ).any()
    ):
        reasons.append("minute_ohlcva_invalid")

    # All daily filtering starts only after the original-frame price gate above.
    daily_dates = daily.get("date", pd.Series(dtype=str)).astype(str).str[:10]
    daily_target = daily.loc[daily_dates == target].copy()
    daily_row_unique = len(daily_target) == 1
    if not daily_row_unique:
        reasons.append("daily_target_row_not_unique")
    elif {
        _strict_symbol(item)
        for item in daily_target.get("symbol", pd.Series(dtype=str))
    } != {normalized_symbol}:
        reasons.append("daily_symbol_mismatch")
    daily_provenance, daily_sources, daily_times = _positive_source_and_time(
        daily_target, approved_sources=APPROVED_ROUND13_DAILY_SOURCES
    )
    if not daily_provenance:
        reasons.append("daily_provider_provenance_not_approved")
    daily_numeric = _numeric_frame(
        daily_target, ("open", "high", "low", "close", "volume", "amount")
    )

    ohlc_match = False
    volume_match = False
    amount_match = False
    metrics: dict[str, Any] = {
        "minute_row_count": int(len(minute)),
        "unique_timestamp_count": timestamp_count,
    }
    if minute_numeric is not None and daily_numeric is not None and daily_row_unique:
        minute_ohlc = {
            "open": float(minute_numeric.iloc[0]["open"]),
            "high": float(minute_numeric["high"].max()),
            "low": float(minute_numeric["low"].min()),
            "close": float(minute_numeric.iloc[-1]["close"]),
        }
        daily_ohlc = {
            key: float(daily_numeric.iloc[0][key])
            for key in ("open", "high", "low", "close")
        }
        ohlc_differences = {
            key: abs(minute_ohlc[key] - daily_ohlc[key]) for key in minute_ohlc
        }
        ohlc_match = all(
            value <= limits.ohlc_absolute for value in ohlc_differences.values()
        )
        minute_volume = float(minute_numeric["volume"].sum())
        daily_volume = float(daily_numeric.iloc[0]["volume"])
        volume_absolute = abs(minute_volume - daily_volume)
        volume_relative = _relative_difference(minute_volume, daily_volume)
        volume_match = (
            volume_absolute <= limits.volume_absolute
            and volume_relative <= limits.volume_relative
        )
        minute_amount = float(minute_numeric["amount"].sum())
        daily_amount = float(daily_numeric.iloc[0]["amount"])
        amount_absolute = abs(minute_amount - daily_amount)
        amount_relative = _relative_difference(minute_amount, daily_amount)
        amount_match = (
            amount_absolute <= limits.amount_absolute
            and amount_relative <= limits.amount_relative
        )
        metrics.update(
            {
                "minute_ohlc": minute_ohlc,
                "daily_ohlc": daily_ohlc,
                "ohlc_absolute_differences": ohlc_differences,
                "minute_volume": minute_volume,
                "daily_volume": daily_volume,
                "volume_absolute_difference": volume_absolute,
                "volume_relative_difference": volume_relative,
                "minute_amount": minute_amount,
                "daily_amount": daily_amount,
                "amount_absolute_difference": amount_absolute,
                "amount_relative_difference": amount_relative,
            }
        )
    if not ohlc_match:
        reasons.append("daily_minute_ohlc_mismatch")
    if not volume_match:
        reasons.append("daily_minute_volume_mismatch")
    if not amount_match:
        reasons.append("daily_minute_amount_mismatch")

    checks = {
        "exact_session_grid": exact_grid and len(minute) == 240,
        "timestamp_unique": timestamp_unique,
        "raw_price_basis": raw_minute_basis and daily_basis["eligible"] is True,
        "positive_provider_provenance": minute_provenance and daily_provenance,
        "price_only_intraday_false": price_only_false,
        "staging_path_hash_run_bound": path_valid and hash_valid,
        "captured_at_bound": captured_binding,
        "daily_row_unique": daily_row_unique,
        "ohlc_match": ohlc_match,
        "volume_match": volume_match,
        "amount_match": amount_match,
    }
    if not checks["exact_session_grid"] and "minute_session_grid_mismatch" not in reasons:
        reasons.append("minute_session_grid_mismatch")
    unique_reasons = list(dict.fromkeys(reasons))
    eligible = not unique_reasons
    return {
        "schema_version": "round13_minute_backfill_item_audit_v2",
        "symbol": normalized_symbol,
        "target_date": target,
        "run_id": run_id,
        "staging_path": str(bound_path),
        "daily_path": str(bound_daily_path),
        "staging_sha256": actual_stage_sha,
        "captured_at": captured_at,
        "minute_sources": minute_sources,
        "daily_sources": daily_sources,
        "daily_captured_at": daily_times[-1] if daily_times else "",
        "daily_price_basis_preflight": daily_basis,
        "status": "passed" if eligible else "blocked",
        "reason_codes": unique_reasons,
        "checks": checks,
        "metrics": metrics,
        "thresholds": limits.to_dict(),
        "thresholds_sha256": ROUND13_CROSSCHECK_THRESHOLDS_SHA256,
        "complete_session": eligible,
        "formal_execution_eligible": eligible,
        "price_basis": "raw" if checks["raw_price_basis"] else "mixed_or_unknown",
        "canonical_import_eligible": eligible,
        "disclaimer": DISCLAIMER,
    }


def audit_round13_minute_backfill_file(
    symbol: str,
    target_date: str,
    *,
    staging_path: Path,
    daily_path: Path,
    staging_root: Path,
    daily_root: Path,
    run_id: str,
    expected_staging_sha256: str | None = None,
) -> dict[str, Any]:
    """Read the actual staging and daily files and bind their hashes."""

    try:
        stage = _descendant(Path(staging_root), Path(staging_path), role="staging_path")
        daily = _descendant(Path(daily_root), Path(daily_path), role="daily_path")
    except (OSError, ValueError):
        return {
            "symbol": _strict_symbol(symbol),
            "target_date": _strict_date(target_date),
            "run_id": run_id,
            "staging_path": str(staging_path),
            "daily_path": str(daily_path),
            "staging_sha256": "",
            "status": "blocked",
            "reason_codes": ["staging_or_daily_path_escape"],
            "complete_session": False,
            "formal_execution_eligible": False,
            "price_basis": "mixed_or_unknown",
            "canonical_import_eligible": False,
        }
    stage_sha = _sha256_file(stage) if stage.is_file() and not stage.is_symlink() else ""
    if expected_staging_sha256 is not None and stage_sha != expected_staging_sha256:
        return {
            "symbol": _strict_symbol(symbol),
            "target_date": _strict_date(target_date),
            "run_id": run_id,
            "staging_path": str(stage),
            "staging_sha256": stage_sha,
            "status": "blocked",
            "reason_codes": ["expected_staging_hash_mismatch"],
            "complete_session": False,
            "formal_execution_eligible": False,
            "price_basis": "mixed_or_unknown",
            "canonical_import_eligible": False,
        }
    try:
        minute_frame = pd.read_csv(stage, encoding="utf-8-sig", dtype={"symbol": str})
    except (OSError, UnicodeError, pd.errors.ParserError):
        minute_frame = pd.DataFrame()
    try:
        daily_frame = pd.read_csv(daily, encoding="utf-8-sig", dtype={"symbol": str})
    except (OSError, UnicodeError, pd.errors.ParserError):
        daily_frame = pd.DataFrame()
    minute_times = sorted(
        {
            str(item or "").strip()
            for item in minute_frame.get("updated_at", pd.Series(dtype=str))
            if str(item or "").strip()
        }
    )
    return audit_round13_minute_backfill_item(
        symbol,
        target_date,
        minute_frame=minute_frame,
        daily_frame=daily_frame,
        staging_path=stage,
        daily_path=daily,
        staging_root=staging_root,
        daily_root=daily_root,
        staging_sha256=stage_sha,
        run_id=run_id,
        captured_at=minute_times[-1] if minute_times else "",
    )


def audit_round13_minute_backfill_batch(
    requested: Sequence[tuple[Any, Any]],
    *,
    staging_paths: Mapping[tuple[str, str], Path],
    daily_paths: Mapping[tuple[str, str], Path],
    staging_root: Path,
    daily_root: Path,
    run_id: str,
    require_complete_scope: bool = False,
) -> dict[str, Any]:
    """Audit all requested files; one blocked item blocks the whole batch."""

    pairs = validate_round13_backfill_scope(
        requested, require_complete_scope=require_complete_scope
    )
    items = [
        audit_round13_minute_backfill_file(
            symbol,
            target,
            staging_path=staging_paths.get((symbol, target), Path("")),
            daily_path=daily_paths.get((symbol, target), Path("")),
            staging_root=staging_root,
            daily_root=daily_root,
            run_id=run_id,
        )
        for symbol, target in pairs
    ]
    passed = sum(item.get("canonical_import_eligible") is True for item in items)
    blocked = len(items) - passed
    eligible = bool(items) and blocked == 0
    return {
        "schema_version": "round13_minute_backfill_batch_audit_v2",
        "task_type": "round13_historical_minute_gap_backfill",
        "run_id": run_id,
        "status": "passed" if eligible else "blocked",
        "requested_pairs": [
            {"symbol": symbol, "target_date": target} for symbol, target in pairs
        ],
        "summary": {"requested": len(items), "passed": passed, "blocked": blocked},
        "items": items,
        "thresholds": ROUND13_CROSSCHECK_THRESHOLDS.to_dict(),
        "thresholds_sha256": ROUND13_CROSSCHECK_THRESHOLDS_SHA256,
        "canonical_import_eligible": eligible,
        "apply_executed": False,
        "disclaimer": DISCLAIMER,
    }


def _group_quality(audit: Mapping[str, Any], target_date: str) -> dict[str, Any]:
    items = [
        dict(item)
        for item in audit.get("items") or []
        if str(item.get("target_date") or "")[:10] == target_date
    ]
    return {
        "status": "passed" if items and all(item.get("canonical_import_eligible") is True for item in items) else "blocked",
        "target_date": target_date,
        "items": items,
        "summary": {
            "complete_session": sum(item.get("complete_session") is True for item in items),
            "formal_execution_eligible": sum(item.get("formal_execution_eligible") is True for item in items),
        },
    }


def _comparable_rows(frame: pd.DataFrame) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for row in frame.to_dict(orient="records"):
        rows.append(tuple(str(row.get(column) or "").strip() for column in _COMPARE_COLUMNS))
    return sorted(rows)


def _row_set_sha256(frame: pd.DataFrame) -> str:
    ordered = frame[list(_COMPARE_COLUMNS)].copy()
    ordered["datetime"] = pd.to_datetime(
        ordered["datetime"], errors="coerce"
    ).dt.strftime("%Y-%m-%d %H:%M:%S")
    ordered = ordered.sort_values("datetime").reset_index(drop=True)
    payload = ordered.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _run_id(value: str | None) -> str:
    if value:
        text = str(value).strip()
        if not _SAFE_RUN_ID_PATTERN.fullmatch(text) or text in {".", ".."}:
            raise ValueError(f"invalid_run_id:{value}")
        return text
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"round13-minute-gap-backfill-{stamp}-{uuid4().hex[:10]}"


def _canonical_snapshot(paths: Mapping[tuple[str, str], Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol, _target in sorted(paths):
        path = paths[(symbol, _target)]
        if symbol in result:
            continue
        result[symbol] = {
            "path": str(path),
            "exists": path.is_file(),
            "sha256": _sha256_file(path) if path.is_file() else "",
            "row_count": int(len(pd.read_csv(path, encoding="utf-8-sig"))) if path.is_file() else 0,
        }
    return result


def _restore_snapshot(
    snapshot: Mapping[str, Mapping[str, Any]],
    backups_dir: Path,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for symbol, item in snapshot.items():
        target = Path(str(item.get("path") or ""))
        try:
            if item.get("exists") is True:
                backup = backups_dir / f"{symbol}.csv"
                restore = backups_dir / f"{symbol}.restore"
                shutil.copy2(backup, restore)
                os.replace(restore, target)
                if _sha256_file(target) != str(item.get("sha256") or ""):
                    raise ValueError("rollback_hash_mismatch")
            else:
                target.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}:{type(exc).__name__}:{exc}")
    return not errors, errors


def run_round13_minute_gap_backfill(
    project_root: Path,
    *,
    network: bool = False,
    apply: bool = False,
    run_id: str | None = None,
    provider_updater: Callable[..., Mapping[str, Any]] = update_intraday_1m_for_symbols,
    append_importer: Callable[..., Mapping[str, Any]] = apply_audited_intraday_batch,
) -> dict[str, Any]:
    """Stage and audit all five pairs, then import both dates as one transaction."""

    root = Path(project_root).resolve(strict=True)
    pairs = sorted(ROUND13_MINUTE_BACKFILL_ALLOWLIST)
    base = {
        "schema_version": "round13_minute_gap_backfill_run_v1",
        "task_type": "round13_historical_minute_gap_backfill",
        "network": bool(network),
        "apply": bool(apply),
        "requested_pairs": [
            {"symbol": symbol, "target_date": target} for symbol, target in pairs
        ],
        "canonical_write": False,
        "disclaimer": DISCLAIMER,
    }
    if apply and not network:
        return {**base, "status": "blocked", "reason_codes": ["apply_requires_network"]}
    if not network:
        return {**base, "status": "planned_offline", "reason_codes": ["network_disabled"]}
    current_run_id = _run_id(run_id)
    results_root = _descendant(
        root,
        root / "results" / "data_chain" / "round13_minute_gap_backfill" / "runs",
        role="results_root",
    )
    run_dir = results_root / current_run_id
    canonical_intraday = _descendant(
        root, root / "data" / "intraday" / "1m", role="canonical_intraday"
    )
    canonical_daily = _descendant(root, root / "data" / "daily", role="canonical_daily")
    lock_path = _descendant(root, root / "state" / "daily_data_chain.lock", role="daily_chain_lock")
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0):
            run_dir.mkdir(parents=True, exist_ok=False)
            provider_results: list[dict[str, Any]] = []
            staging_paths: dict[tuple[str, str], Path] = {}
            daily_paths: dict[tuple[str, str], Path] = {}
            for target in sorted({item[1] for item in pairs}):
                symbols = [symbol for symbol, item_target in pairs if item_target == target]
                staging_dir = run_dir / "staging" / target
                staging_dir.mkdir(parents=True, exist_ok=False)
                try:
                    provider_payload = dict(
                        provider_updater(
                            symbols,
                            target,
                            provider_order=list(DEFAULT_INTRADAY_PROVIDER_ORDER),
                            source="symbols",
                            overwrite=False,
                            append=True,
                            dry_run=False,
                            network=True,
                            execute_after_update=False,
                            update_portfolio_after_execution=False,
                            intraday_dir=staging_dir,
                            existing_intraday_dir=canonical_intraday,
                            materialize_local_existing=True,
                            output_path=run_dir / f"provider_{target}.json",
                            run_id=current_run_id,
                            require_raw_price_basis=True,
                            require_complete_session=True,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    provider_payload = {
                        "status": "failed",
                        "error": f"{type(exc).__name__}:{exc}"[:1000],
                    }
                provider_results.append({"target_date": target, "payload": provider_payload})
                for symbol in symbols:
                    staging_paths[(symbol, target)] = staging_dir / f"{symbol}.csv"
                    daily_paths[(symbol, target)] = canonical_daily / f"{symbol}.csv"
            audit = audit_round13_minute_backfill_batch(
                pairs,
                staging_paths=staging_paths,
                daily_paths=daily_paths,
                staging_root=run_dir / "staging",
                daily_root=canonical_daily,
                run_id=current_run_id,
                require_complete_scope=True,
            )
            atomic_write_json(run_dir / "quality_audit.json", audit)
            payload: dict[str, Any] = {
                **base,
                "run_id": current_run_id,
                "run_dir": str(run_dir),
                "provider_results": provider_results,
                "quality_audit": audit,
            }
            if audit.get("canonical_import_eligible") is not True:
                payload.update(
                    {
                        "status": "blocked",
                        "reason_codes": ["whole_batch_quality_gate_failed"],
                        "canonical_import": {"status": "not_applicable", "applied": False},
                    }
                )
                atomic_write_json(run_dir / "manifest.json", payload)
                return payload

            preflights: list[dict[str, Any]] = []
            for target in sorted({item[1] for item in pairs}):
                symbols = [symbol for symbol, item_target in pairs if item_target == target]
                preflight = dict(
                    append_importer(
                        staging_dir=run_dir / "staging" / target,
                        canonical_dir=canonical_intraday,
                        symbols=symbols,
                        target_date=target,
                        run_id=current_run_id,
                        quality_audit=_group_quality(audit, target),
                        output_path=run_dir / f"canonical_preflight_{target}.json",
                        apply=False,
                    )
                )
                preflights.append(preflight)
            if not all(item.get("status") == "ready" for item in preflights):
                payload.update(
                    {
                        "status": "blocked",
                        "reason_codes": ["canonical_import_preflight_failed"],
                        "canonical_import": {
                            "status": "blocked",
                            "applied": False,
                            "preflights": preflights,
                        },
                    }
                )
                atomic_write_json(run_dir / "manifest.json", payload)
                return payload
            if not apply:
                payload.update(
                    {
                        "status": "completed_staging_only",
                        "reason_codes": ["apply_not_requested"],
                        "canonical_import": {
                            "status": "ready",
                            "applied": False,
                            "preflights": preflights,
                        },
                    }
                )
                atomic_write_json(run_dir / "manifest.json", payload)
                return payload

            before = _canonical_snapshot(
                {(symbol, target): canonical_intraday / f"{symbol}.csv" for symbol, target in pairs}
            )
            backups_dir = run_dir / "outer_transaction_backups"
            backups_dir.mkdir(parents=True, exist_ok=True)
            for symbol, item in before.items():
                if item["exists"]:
                    shutil.copy2(Path(str(item["path"])), backups_dir / f"{symbol}.csv")
            imports: list[dict[str, Any]] = []
            post_import_row_hashes: dict[tuple[str, str], dict[str, str]] = {}
            error = ""
            try:
                for target in sorted({item[1] for item in pairs}):
                    symbols = [symbol for symbol, item_target in pairs if item_target == target]
                    current = dict(
                        append_importer(
                            staging_dir=run_dir / "staging" / target,
                            canonical_dir=canonical_intraday,
                            symbols=symbols,
                            target_date=target,
                            run_id=current_run_id,
                            quality_audit=_group_quality(audit, target),
                            output_path=run_dir / f"canonical_import_{target}.json",
                            apply=True,
                        )
                    )
                    imports.append(current)
                    if current.get("status") != "completed" or current.get("applied") is not True:
                        raise RuntimeError(f"canonical_import_not_completed:{target}")
                for symbol, target in pairs:
                    staged = pd.read_csv(staging_paths[(symbol, target)], encoding="utf-8-sig", dtype={"symbol": str})
                    canonical = pd.read_csv(canonical_intraday / f"{symbol}.csv", encoding="utf-8-sig", dtype={"symbol": str})
                    canonical_timestamps = pd.to_datetime(
                        canonical.get("datetime"), errors="coerce"
                    )
                    selected = canonical.loc[
                        canonical_timestamps.dt.strftime("%Y-%m-%d") == target
                    ]
                    if _comparable_rows(staged) != _comparable_rows(selected):
                        raise ValueError(f"post_import_content_mismatch:{symbol}:{target}")
                    staging_row_hash = _row_set_sha256(staged)
                    canonical_row_hash = _row_set_sha256(selected)
                    if staging_row_hash != canonical_row_hash:
                        raise ValueError(
                            f"post_import_row_bytes_hash_mismatch:{symbol}:{target}"
                        )
                    post_import_row_hashes[(symbol, target)] = {
                        "staging_target_row_set_sha256": staging_row_hash,
                        "canonical_target_row_set_sha256": canonical_row_hash,
                    }
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}:{exc}"[:1000]
            if error:
                rollback_ok, rollback_errors = _restore_snapshot(before, backups_dir)
                payload.update(
                    {
                        "status": "failed",
                        "reason_codes": ["whole_batch_canonical_import_failed"],
                        "canonical_import": {
                            "status": "failed",
                            "applied": False,
                            "error": error,
                            "imports": imports,
                            "rollback_performed": rollback_ok,
                            "rollback_verified": rollback_ok,
                            "rollback_errors": rollback_errors,
                        },
                    }
                )
                atomic_write_json(run_dir / "manifest.json", payload)
                return payload
            after = _canonical_snapshot(
                {(symbol, target): canonical_intraday / f"{symbol}.csv" for symbol, target in pairs}
            )
            provenance = [
                {
                    "symbol": symbol,
                    "target_date": target,
                    "canonical_path": before[symbol]["path"],
                    "canonical_sha256_before": before[symbol]["sha256"],
                    "canonical_sha256_after": after[symbol]["sha256"],
                    "canonical_row_count_before": before[symbol]["row_count"],
                    "canonical_row_count_after": after[symbol]["row_count"],
                    "staging_path": str(staging_paths[(symbol, target)]),
                    "staging_sha256": _sha256_file(staging_paths[(symbol, target)]),
                    **post_import_row_hashes[(symbol, target)],
                }
                for symbol, target in pairs
            ]
            payload.update(
                {
                    "status": "completed",
                    "reason_codes": [],
                    "canonical_write": True,
                    "canonical_import": {
                        "status": "completed",
                        "applied": True,
                        "preflights": preflights,
                        "imports": imports,
                        "provenance": provenance,
                        "rollback_performed": False,
                    },
                    "completed_at": now_text(),
                }
            )
            atomic_write_json(run_dir / "manifest.json", payload)
            return payload
    except TimeoutError:
        return {**base, "status": "busy", "reason_codes": ["daily_data_chain_lock_busy"]}


__all__ = [
    "ROUND13_MINUTE_BACKFILL_ALLOWLIST",
    "APPROVED_ROUND13_MINUTE_SOURCES",
    "APPROVED_ROUND13_DAILY_SOURCES",
    "DailyMinuteCrosscheckThresholds",
    "ROUND13_CROSSCHECK_THRESHOLDS",
    "ROUND13_CROSSCHECK_THRESHOLDS_SHA256",
    "validate_round13_backfill_scope",
    "audit_round13_minute_backfill_item",
    "audit_round13_minute_backfill_file",
    "audit_round13_minute_backfill_batch",
    "run_round13_minute_gap_backfill",
]
