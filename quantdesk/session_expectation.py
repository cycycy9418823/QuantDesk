"""Read-only, evidence-backed same-day session expectation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

import pandas as pd

from quantdesk.daily_session_evidence import (
    BSE_SOURCE,
    SOURCE as BAOSTOCK_SESSION_SOURCE,
    load_verified_no_session_dates,
)
from quantdesk.price_basis import PRICE_BASIS_RAW, PRICE_BASIS_UNKNOWN, classify_frame_price_basis, normalize_price_basis
from quantdesk.shared import normalize_symbol
from quantdesk.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATIONS = (
    "expected_trade",
    "verified_full_day_suspension",
    "not_yet_listed",
    "delisted_before_date",
    "unknown",
)
LEGITIMATE_NO_SESSION_CLASSIFICATIONS = frozenset(
    {"verified_full_day_suspension", "not_yet_listed", "delisted_before_date"}
)
_DATE_ALIASES = ("trade_date", "date", "datetime", "\u4ea4\u6613\u65e5\u671f", "\u65e5\u671f")
_SYMBOL_ALIASES = (
    "symbol", "ts_code", "code", "\u8bc1\u5238\u4ee3\u7801", "\u80a1\u7968\u4ee3\u7801", "\u4ee3\u7801"
)
_VOLUME_ALIASES = ("volume", "vol", "volume_qty", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u91cf(\u624b)")
_STATUS_ALIASES = (
    "is_suspended", "suspended", "suspend", "suspend_flag", "\u505c\u724c",
    "\u662f\u5426\u505c\u724c",
)
_LIST_DATE_ALIASES = ("list_date", "listing_date", "\u4e0a\u5e02\u65e5\u671f")
_DELIST_DATE_ALIASES = (
    "delist_date", "last_trade_date", "\u9000\u5e02\u65e5\u671f", "\u6700\u540e\u4ea4\u6613\u65e5"
)


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "<na>"}:
        return ""
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8 and text.replace(".", "").replace("-", "").replace("/", "").isdigit():
        text = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    else:
        text = text[:10]
    try:
        parsed = pd.Timestamp(text)
    except Exception:
        return ""
    return "" if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def _input_symbol(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("symbol") or value.get("ts_code") or value.get("code")
    return normalize_symbol(value)


def _target_date(value: Any) -> str:
    result = _date_text(value)
    if not result:
        raise ValueError(f"invalid target_date: {value!r}")
    return result


def _find_column(frame: pd.DataFrame, aliases: Iterable[str]) -> str | None:
    if frame is None:
        return None
    exact = {str(c).strip(): c for c in frame.columns}
    lowered = {str(c).strip().lower(): c for c in frame.columns}
    for alias in aliases:
        if alias in exact:
            return exact[alias]
        if str(alias).strip().lower() in lowered:
            return lowered[str(alias).strip().lower()]
    return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return bool(float(value))
        except Exception:
            return None
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on", "suspended", "\u505c\u724c", "\u662f", "\u771f"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off", "normal", "\u6b63\u5e38", "\u5426", "\u5047"}:
        return False
    return None

def _compact_error(value: Any, limit: int = 400) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except Exception:
        return ""
    return digest.hexdigest()


def _records_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    except Exception:
        encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _base_evidence(source: str, path: Path | None, target: str) -> dict[str, Any]:
    return {
        "source": source,
        "path": str(path) if path is not None else "",
        "exists": False,
        "read_ok": False,
        "as_of": target,
        "sha256": "",
        "count": 0,
        "matched_count": 0,
        "error": "missing",
    }


def _read_csv(path: Path, source: str, target: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    evidence = _base_evidence(source, path, target)
    if not path.exists() or not path.is_file():
        return pd.DataFrame(), evidence
    evidence["exists"] = True
    evidence["sha256"] = _file_hash(path)
    frame: pd.DataFrame | None = None
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            frame = pd.read_csv(path, encoding=encoding)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if frame is None:
        evidence["error"] = f"{type(last_error).__name__}: {_compact_error(last_error)}"
        return pd.DataFrame(), evidence
    evidence["read_ok"] = True
    evidence["count"] = int(len(frame))
    evidence["error"] = ""
    date_column = _find_column(frame, _DATE_ALIASES)
    if date_column is not None:
        dates = [_date_text(item) for item in frame[date_column].tolist()]
        if any(dates):
            evidence["as_of"] = max(item for item in dates if item)
    return frame, evidence


def _symbol_series(frame: pd.DataFrame) -> pd.Series | None:
    column = _find_column(frame, _SYMBOL_ALIASES)
    return None if column is None else frame[column].map(normalize_symbol)


def _date_series(frame: pd.DataFrame) -> pd.Series | None:
    column = _find_column(frame, _DATE_ALIASES)
    return None if column is None else frame[column].map(_date_text)


def _load_security_master(
    path: Path, target: str, symbols: set[str]
) -> tuple[dict[str, tuple[str, str, dict[str, Any]]], dict[str, Any]]:
    frame, evidence = _read_csv(path, "security_master", target)
    if frame.empty:
        return {}, evidence
    values = _symbol_series(frame)
    if values is None:
        evidence["error"] = "missing_symbol_column"
        return {}, evidence
    mask = values.isin(symbols)
    evidence["matched_count"] = int(mask.sum())
    list_col = _find_column(frame, _LIST_DATE_ALIASES)
    delist_col = _find_column(frame, _DELIST_DATE_ALIASES)
    result: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for index, row in frame.loc[mask].iterrows():
        symbol = str(values.loc[index] or "")
        if not symbol:
            continue
        listed = _date_text(row.get(list_col)) if list_col else ""
        delisted = _date_text(row.get(delist_col)) if delist_col else ""
        evidence_row = {
            "source": "security_master",
            "symbol": symbol,
            "list_date": listed,
            "delist_date": delisted,
        }
        if listed and target < listed:
            result[symbol] = (
                "not_yet_listed",
                "security_master_list_date_after_target",
                evidence_row,
            )
        elif delisted and target > delisted:
            result[symbol] = (
                "delisted_before_date",
                "security_master_delist_date_before_target",
                evidence_row,
            )
    evidence["lifecycle_matched_count"] = int(len(result))
    return result, evidence


def _load_suspension(
    path: Path,
    source: str,
    target: str,
    symbols: set[str],
    *,
    status_file: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    frame, evidence = _read_csv(path, source, target)
    if frame.empty:
        return {}, evidence
    values = _symbol_series(frame)
    dates = _date_series(frame)
    if values is None or dates is None:
        evidence["error"] = "missing_symbol_or_trade_date_column"
        return {}, evidence
    mask = dates.eq(target) & values.isin(symbols)
    target_frame = frame.loc[mask].copy()
    target_symbols = values.loc[mask]
    evidence["target_row_count"] = int(mask.sum())
    result: dict[str, dict[str, Any]] = {}
    invalid_symbols: set[str] = set()

    if status_file:
        status_col = _find_column(frame, _STATUS_ALIASES)
        if status_col is None:
            evidence["error"] = "missing_explicit_suspension_status_column"
            return {}, evidence
        for symbol in sorted(set(target_symbols.tolist())):
            if not symbol:
                continue
            symbol_rows = target_frame.loc[target_symbols.eq(symbol)]
            flags = [_as_bool(value) for value in symbol_rows[status_col].tolist()]
            if flags and all(flag is True for flag in flags):
                result[symbol] = {
                    "source": source,
                    "symbol": symbol,
                    "target_date": target,
                    "status": True,
                    "reason": "exact_date_suspension_status",
                }
            else:
                invalid_symbols.add(symbol)
    else:
        type_col = _find_column(
            frame,
            (
                "suspend_type",
                "suspension_type",
                "halt_type",
                "\u505c\u724c\u7c7b\u578b",
            ),
        )
        if type_col is None:
            evidence["error"] = "missing_full_day_suspension_type_column"
            return {}, evidence
        full_day_values = {
            "full_day",
            "full-day",
            "full day",
            "all_day",
            "all-day",
            "\u5168\u5929",
            "\u5168\u65e5",
            "\u5168\u5929\u505c\u724c",
            "\u5168\u65e5\u505c\u724c",
        }
        for symbol in sorted(set(target_symbols.tolist())):
            if not symbol:
                continue
            symbol_rows = target_frame.loc[target_symbols.eq(symbol)]
            types = [
                str(value or "").strip().lower()
                for value in symbol_rows[type_col].tolist()
            ]
            if types and all(value in full_day_values for value in types):
                result[symbol] = {
                    "source": source,
                    "symbol": symbol,
                    "target_date": target,
                    "status": True,
                    "reason": "exact_date_suspension_history",
                }
            else:
                invalid_symbols.add(symbol)

    evidence["verified_count"] = int(len(result))
    evidence["matched_count"] = int(len(result))
    evidence["invalid_or_conflicting_count"] = int(len(invalid_symbols))
    if invalid_symbols:
        evidence["error"] = (
            "invalid_or_conflicting_suspension_evidence:"
            + ",".join(sorted(invalid_symbols))
        )
    return result, evidence


def _load_refresh_suspension_exclusions(
    metadata_dir: Path,
    target: str,
    symbols: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Use same-day refresh JSON so a halt is not 'unknown' when network is off."""

    stamp = target.replace("-", "")
    path = Path(metadata_dir) / "suspension_evidence" / f"{stamp}.json"
    evidence: dict[str, Any] = {
        "source": "refresh_suspension_evidence_json",
        "path": str(path),
        "exists": path.is_file(),
        "read_ok": False,
        "as_of": target,
        "sha256": "",
        "count": 0,
        "matched_count": 0,
        "verified_count": 0,
        "error": "",
    }
    if not path.is_file():
        return {}, evidence
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        evidence["error"] = f"unreadable:{exc.__class__.__name__}"
        return {}, evidence
    if not isinstance(payload, dict):
        evidence["error"] = "invalid_payload"
        return {}, evidence
    file_date = _date_text(payload.get("target_date"))
    if file_date != target:
        evidence["error"] = (
            f"target_date_mismatch:{file_date}" if file_date else "missing_target_date"
        )
        return {}, evidence
    exclusions = payload.get("verified_exclusions")
    if not isinstance(exclusions, dict):
        evidence["error"] = "missing_verified_exclusions"
        return {}, evidence
    result: dict[str, dict[str, Any]] = {}
    for raw_symbol, row in exclusions.items():
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol not in symbols:
            continue
        if not isinstance(row, Mapping) or not row:
            continue
        if not _valid_refresh_suspension_row(payload, row, target):
            continue
        signature = str(row.get("signature") or "").strip()
        result[symbol] = {
            "source": str(payload.get("source") or row.get("source") or "refresh_suspension_evidence"),
            "symbol": symbol,
            "target_date": target,
            "status": True,
            "reason": f"refresh_verified_exclusion:{signature}",
        }
    evidence["read_ok"] = True
    evidence["count"] = len(exclusions)
    evidence["matched_count"] = len(result)
    evidence["verified_count"] = len(result)
    return result, evidence


def _valid_refresh_suspension_row(
    payload: Mapping[str, Any], row: Mapping[str, Any], target: str
) -> bool:
    """Require a same-day provider observation, not a free-form label.

    Refresh exclusions are a fallback for symbols with no daily bar.  A label
    or a single provider/close field cannot establish that the session was
    absent, so accepted rows need a known signature plus the raw quote facts
    used by that signature.
    """

    known_signatures = {"zero_trade_with_estimates", "zero_volume_with_last_close"}
    signature = str(row.get("signature") or "").strip()
    if signature not in known_signatures:
        return False

    # The current refresh producer writes this exact source at the file level.
    # Source identity is provenance only; quote facts below establish the halt.
    source = str(payload.get("source") or "").strip()
    if source != "sina.Market_Center.getHQNodeData":
        return False
    row_source = str(row.get("source") or "").strip()
    if row_source and row_source != source:
        return False

    # If a row carries its own date, it must agree with the file's date.
    for key in ("target_date", "trade_date", "date", "as_of"):
        if key in row and _date_text(row.get(key)) != target:
            return False

    # The producer derives these signatures from the Sina snapshot row.  Keep
    # the quote fields explicit so a label or a provider name cannot certify a
    # missing observation.
    if "open" not in row or "close" not in row or "volume" not in row:
        return False

    def finite_number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    close = finite_number(row.get("close"))
    opening = finite_number(row.get("open"))
    if close is None or opening is None:
        return False

    volume = finite_number(row.get("volume"))
    if volume is None or volume < 0:
        return False

    tradestatus = row.get("tradestatus")
    if tradestatus is not None and str(tradestatus).strip() not in {"", "0"}:
        return False

    # Match the producer's invalid-row signatures.  A zero-volume halt has a
    # positive last trade and an observed zero open; a zero-trade halt has no
    # positive trade and likewise no activity.  Any contradictory quote is a
    # gap, not a verified suspension.
    if signature == "zero_volume_with_last_close":
        return close > 0 and opening <= 0 and volume == 0
    if signature == "zero_trade_with_estimates":
        return close == 0 and opening <= 0 and volume == 0
    return False


def _resolve_dirs(
    project_root: str | Path | None,
    data_root: str | Path | None,
    daily_dir: str | Path | None,
    metadata_dir: str | Path | None,
) -> tuple[Path, Path, Path]:
    if data_root is not None:
        data = Path(data_root)
        if not data.is_absolute() and project_root is not None:
            data = Path(project_root) / data
        data = data.resolve()
        if data.name.lower() == "data":
            root = data.parent
        elif (data / "data").is_dir():
            root = data
            data = data / "data"
        else:
            root = data
    else:
        root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
        data = root / "data"
    daily = Path(daily_dir).resolve() if daily_dir is not None else data / "daily"
    metadata = Path(metadata_dir).resolve() if metadata_dir is not None else data / "metadata"
    return root, daily, metadata


def _classify_basis(frame: pd.DataFrame) -> tuple[str, str]:
    try:
        basis, reason = classify_frame_price_basis(frame)
    except Exception as exc:  # noqa: BLE001
        return PRICE_BASIS_UNKNOWN, f"price_basis_classification_failed:{type(exc).__name__}"
    if (
        not reason
        or "adjust" in frame.columns
        or frame.attrs.get("price_basis_reason") in {"mixed_price_basis", "unknown_price_basis"}
    ):
        return basis, reason
    markers: list[Any] = []
    marker_column = _find_column(frame, ("price_basis", "adjustment_basis"))
    if marker_column is not None:
        markers.extend(frame[marker_column].dropna().tolist())
    else:
        attrs = getattr(frame, "attrs", {}) or {}
        for key in ("price_basis", "observed_price_basis", "adjustment"):
            if attrs.get(key) not in (None, ""):
                markers.append(attrs.get(key))
                break
    if not markers:
        return basis, reason
    normalized = [normalize_price_basis(value) for value in markers]
    if normalized and len(set(normalized)) == 1 and normalized[0] != PRICE_BASIS_UNKNOWN:
        return normalized[0], ""
    return PRICE_BASIS_UNKNOWN, "mixed_or_unknown_explicit_price_basis"


def _daily_info(
    symbol: str,
    daily_dir: Path,
    daily_frames: Mapping[Any, pd.DataFrame] | None,
    target: str,
) -> dict[str, Any]:
    supplied: dict[str, pd.DataFrame] = {}
    for key, value in (daily_frames or {}).items():
        normalized = _input_symbol(key)
        if not normalized:
            continue
        if isinstance(value, pd.DataFrame):
            supplied[normalized] = value.copy()
        else:
            try:
                supplied[normalized] = pd.DataFrame(value)
            except Exception:
                continue
    if symbol in supplied:
        frame = supplied[symbol]
        evidence = _base_evidence("daily", None, target)
        evidence.update(
            {
                "exists": True,
                "read_ok": True,
                "as_of": target,
                "sha256": _records_hash(frame.to_dict(orient="records")),
                "count": int(len(frame)),
                "error": "",
            }
        )
    else:
        frame, evidence = _read_csv(daily_dir / f"{symbol}.csv", f"daily:{symbol}", target)
        evidence["source"] = "daily"
        evidence["symbol"] = symbol
    basis, basis_reason = _classify_basis(frame)
    dates = _date_series(frame)
    target_mask = (
        dates.eq(target)
        if dates is not None
        else pd.Series(False, index=frame.index, dtype=bool)
    )
    symbol_values = _symbol_series(frame)
    if symbol_values is not None and bool(target_mask.any()):
        nonempty = symbol_values.astype(str).str.len() > 0
        target_mask = target_mask & (~nonempty | symbol_values.eq(symbol))
    target_frame = frame.loc[target_mask]
    volume_col = _find_column(frame, _VOLUME_ALIASES)
    volumes: list[float] = []
    volume_valid_count = 0
    if volume_col is not None:
        for value in target_frame[volume_col].tolist():
            try:
                number = float(str(value).replace(",", "").strip())
            except Exception:
                continue
            if pd.notna(number):
                volumes.append(float(number))
                volume_valid_count += 1
    evidence.update(
        {
            "as_of": target if bool(target_mask.any()) else evidence.get("as_of", target),
            "target_count": int(target_mask.sum()),
            "price_basis": basis,
            "price_basis_reason": basis_reason,
            "volume_column": volume_col or "",
        }
    )
    return {
        "evidence": evidence,
        "basis": basis,
        "basis_reason": basis_reason,
        "target_count": int(target_mask.sum()),
        "volumes": volumes,
        "volume_valid_count": int(volume_valid_count),
    }


def _provider(
    target: str,
    symbols: set[str],
    *,
    network: bool,
    provider: Any,
) -> tuple[set[str], dict[str, Any]]:
    evidence: dict[str, Any] = {
        "source": "akshare.stock_tfp_em",
        "path": "",
        "exists": False,
        "read_ok": False,
        "as_of": target,
        "sha256": "",
        "count": 0,
        "matched_count": 0,
        "normalizable_count": 0,
        "error": "",
        "status": "skipped_network_disabled",
        "network_enabled": bool(network),
    }
    if not network:
        evidence["error"] = "network_disabled"
        return set(), evidence
    function = provider
    if function is not None and not callable(function):
        function = getattr(function, "stock_tfp_em", None)
    if function is None:
        try:
            import akshare as ak  # type: ignore
            function = getattr(ak, "stock_tfp_em")
            evidence["exists"] = True
        except Exception as exc:  # noqa: BLE001
            evidence["status"] = "failed"
            evidence["error"] = f"{type(exc).__name__}: {_compact_error(exc)}"
            return set(), evidence
    else:
        evidence["exists"] = True
    provider_date = target.replace("-", "")
    try:
        if isinstance(function, pd.DataFrame):
            result = function
        else:
            try:
                result = function(date=provider_date)
            except TypeError:
                result = function(provider_date)
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {_compact_error(exc)}"
        return set(), evidence
    try:
        frame = result.copy() if isinstance(result, pd.DataFrame) else pd.DataFrame(result)
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {_compact_error(exc)}"
        return set(), evidence
    evidence["read_ok"] = True
    evidence["count"] = int(len(frame))
    evidence["sha256"] = _records_hash(frame.to_dict(orient="records"))
    if frame.empty:
        evidence["status"] = "empty"
        return set(), evidence
    code_col = _find_column(frame, _SYMBOL_ALIASES)
    if code_col is None:
        evidence["status"] = "success"
        evidence["error"] = "missing_code_column"
        return set(), evidence
    returned = {
        normalize_symbol(value)
        for value in frame[code_col].tolist()
        if normalize_symbol(value)
    }
    matched = returned & symbols
    evidence["normalizable_count"] = int(len(returned))
    evidence["matched_count"] = int(len(matched))
    evidence["status"] = "success"
    return matched, evidence


def _lifecycle_conflict_record(
    symbol: str,
    lifecycle_detail: tuple[str, str, dict[str, Any]],
    overridden_by: str,
) -> tuple[str, dict[str, Any]]:
    lifecycle_classification, _reason, evidence = lifecycle_detail
    warning = (
        f"lifecycle_conflict:{symbol}:{lifecycle_classification}"
        f"_overridden_by_{overridden_by}"
    )
    conflict_evidence = {
        **dict(evidence),
        "conflict": "lifecycle_vs_raw_daily",
        "lifecycle_classification": lifecycle_classification,
        "overridden_by": overridden_by,
    }
    return warning, conflict_evidence


def _detail(
    symbol: str,
    classification: str,
    reason: str,
    evidence: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    detail = {
        "symbol": symbol,
        "classification": classification,
        "status": classification,
        "reason": reason,
        "reason_code": reason,
        "evidence": evidence,
    }
    if warnings:
        detail["warnings"] = list(dict.fromkeys(warnings))
    return detail


def build_session_expectation(
    symbols: Iterable[Any],
    target_date: Any,
    *,
    project_root: str | Path | None = None,
    data_root: str | Path | None = None,
    daily_dir: str | Path | None = None,
    metadata_dir: str | Path | None = None,
    daily_frames: Mapping[Any, pd.DataFrame] | None = None,
    daily_status: Mapping[str, Any] | None = None,
    fast_daily_status: bool = False,
    network: bool = False,
    provider: Any = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    target = _target_date(target_date)
    root, resolved_daily, resolved_metadata = _resolve_dirs(
        project_root, data_root, daily_dir, metadata_dir
    )
    if isinstance(symbols, (str, bytes, Mapping)):
        symbol_items = [symbols]
    else:
        symbol_items = symbols or []
    normalized_symbols = sorted(
        {
            normalized
            for item in symbol_items
            for normalized in [_input_symbol(item)]
            if normalized
        }
    )
    symbol_set = set(normalized_symbols)
    source_evidence: dict[str, Any] = {}
    lifecycle, source_evidence["security_master"] = _load_security_master(
        resolved_metadata / "security_master.csv", target, symbol_set
    )
    status, source_evidence["suspension_status"] = _load_suspension(
        resolved_metadata / "suspension_status.csv",
        "suspension_status",
        target,
        symbol_set,
        status_file=True,
    )
    history, source_evidence["suspension_history"] = _load_suspension(
        resolved_metadata / "suspension_history.csv",
        "suspension_history",
        target,
        symbol_set,
        status_file=False,
    )
    local_suspensions = dict(history)
    local_suspensions.update(status)
    refresh_exclusions, source_evidence["refresh_suspension_evidence"] = (
        _load_refresh_suspension_exclusions(
            resolved_metadata, target, symbol_set
        )
    )
    local_suspensions.update(refresh_exclusions)
    (
        baostock_no_session_dates,
        baostock_evidence_by_symbol,
        baostock_evidence_warnings,
    ) = load_verified_no_session_dates(
        normalized_symbols,
        metadata_dir=resolved_metadata,
    )
    baostock_suspensions: dict[str, dict[str, Any]] = {}
    for symbol, suspended_dates in baostock_no_session_dates.items():
        if target not in suspended_dates:
            continue
        evidence = baostock_evidence_by_symbol.get(symbol) or {}
        evidence_source = str(evidence.get("source") or BAOSTOCK_SESSION_SOURCE)
        # The legacy Sina snapshot loader only exposed a signature and date.
        # Refresh exclusions are validated above with the complete quote
        # facts; do not let the legacy path bypass that contract.
        if evidence_source == "sina_snapshot_suspension_evidence":
            continue
        is_bse_interval = evidence_source == BSE_SOURCE
        baostock_suspensions[symbol] = {
            "source": evidence_source,
            "symbol": symbol,
            "target_date": target,
            "tradestatus": "0",
            "reason": (
                "exchange_exact_date_suspension_interval"
                if is_bse_interval
                else "baostock_exact_date_tradestatus_zero"
            ),
            "query_start_date": evidence.get("query_start_date", ""),
            "query_end_date": evidence.get("query_end_date", ""),
            "records_sha256": evidence.get("records_sha256", ""),
        }
    local_suspensions.update(baostock_suspensions)
    source_evidence["baostock_session_evidence"] = {
        "source": "verified_persisted_session_evidence",
        "path": str(
            resolved_metadata
            / "provider_session_evidence"
            / "baostock"
        ),
        "exists": bool(baostock_evidence_by_symbol),
        "read_ok": not bool(baostock_evidence_warnings),
        "as_of": target,
        "sha256": _records_hash(baostock_evidence_by_symbol),
        "count": len(baostock_evidence_by_symbol),
        "matched_count": len(baostock_suspensions),
        "verified_count": len(baostock_suspensions),
        "error": ";".join(baostock_evidence_warnings),
    }
    provider_suspensions, source_evidence["provider"] = _provider(
        target, symbol_set, network=network, provider=provider
    )
    daily_status_payload = dict(daily_status or {})
    fresh_from_update = set(
        normalize_symbol(item)
        for item in list(daily_status_payload.get("fresh_symbol_list_after") or [])
        if normalize_symbol(item)
    )
    raw_positive_from_update = set(
        normalize_symbol(item)
        for item in list(
            daily_status_payload.get("raw_positive_volume_target_symbol_list")
            or []
        )
        if normalize_symbol(item)
    )
    raw_zero_from_update = set(
        normalize_symbol(item)
        for item in list(
            daily_status_payload.get("raw_zero_volume_target_symbol_list") or []
        )
        if normalize_symbol(item)
    )
    fast_trusted = bool(
        fast_daily_status
        and daily_status_payload.get("require_raw_price_basis") is True
        and str(daily_status_payload.get("target_date") or "")[:10] == target
        and str(daily_status_payload.get("status") or "")
        in {"completed", "partial", "warning"}
    )
    volume_classification_conflicts = (
        raw_positive_from_update & raw_zero_from_update
        if fast_trusted
        else set()
    )
    raw_positive_from_update -= volume_classification_conflicts
    raw_zero_from_update -= volume_classification_conflicts
    if fast_trusted:
        update_digest = _records_hash(
            {
                key: daily_status_payload.get(key)
                for key in (
                    "run_id",
                    "target_date",
                    "status",
                    "fresh_symbol_list_after",
                    "stale_symbol_list_after",
                    "failed_symbol_list",
                    "raw_positive_volume_target_symbols",
                    "raw_positive_volume_target_symbol_list",
                    "raw_zero_volume_target_symbols",
                    "raw_zero_volume_target_symbol_list",
                )
            }
        )
        daily_info = {}
        for symbol in normalized_symbols:
            if symbol in fresh_from_update and symbol in volume_classification_conflicts:
                daily_info[symbol] = {
                    "evidence": {
                        "source": "daily_update_payload",
                        "exists": True,
                        "read_ok": False,
                        "as_of": target,
                        "sha256": update_digest,
                        "count": 1,
                        "error": "target_volume_classification_conflict",
                        "symbol": symbol,
                        "target_count": 1,
                        "price_basis": PRICE_BASIS_RAW,
                    },
                    "basis": PRICE_BASIS_RAW,
                    "basis_reason": "target_volume_classification_conflict",
                    "target_count": 1,
                    "volumes": [],
                    "volume_valid_count": 0,
                    "volume_classification_conflict": True,
                }
            elif (
                symbol in fresh_from_update
                and (
                    symbol in local_suspensions
                    or symbol in provider_suspensions
                )
            ):
                # Read only the small conflict set so a suspension assertion
                # can be checked against the persisted raw target row.
                daily_info[symbol] = _daily_info(
                    symbol,
                    resolved_daily,
                    daily_frames,
                    target,
                )
            elif symbol in fresh_from_update and (
                symbol in raw_positive_from_update
                or symbol in raw_zero_from_update
            ):
                volume_classification = (
                    "raw_positive_volume"
                    if symbol in raw_positive_from_update
                    else "raw_zero_volume"
                )
                daily_info[symbol] = {
                    "evidence": {
                        "source": "daily_update_payload",
                        "exists": True,
                        "read_ok": True,
                        "as_of": target,
                        "sha256": update_digest,
                        "count": 1,
                        "error": "",
                        "symbol": symbol,
                        "target_count": 1,
                        "price_basis": PRICE_BASIS_RAW,
                        "target_volume_classification": volume_classification,
                    },
                    "basis": PRICE_BASIS_RAW,
                    "basis_reason": "",
                    "target_count": 1,
                    "volumes": [],
                    "volume_valid_count": 0,
                    "raw_positive_volume_verified": (
                        symbol in raw_positive_from_update
                    ),
                    "raw_zero_volume_verified": symbol in raw_zero_from_update,
                }
            else:
                # History coverage does not prove file absence or a target bar.
                # Re-read only symbols without conclusive target-volume evidence.
                daily_info[symbol] = _daily_info(
                    symbol, resolved_daily, daily_frames, target,
                )
    else:
        daily_info = {
            symbol: _daily_info(symbol, resolved_daily, daily_frames, target)
            for symbol in normalized_symbols
        }
    source_evidence["daily_by_symbol"] = {
        symbol: info["evidence"] for symbol, info in daily_info.items()
    }

    per_symbol: dict[str, dict[str, Any]] = {}
    lifecycle_conflict_warnings: list[str] = []
    suspension_conflict_warnings: list[str] = []
    for symbol in normalized_symbols:
        info = daily_info[symbol]
        daily_evidence = info["evidence"]
        refs: list[dict[str, Any]] = [
            {
                "source": "daily",
                "exists": bool(daily_evidence.get("exists")),
                "read_ok": bool(daily_evidence.get("read_ok")),
                "target_count": int(info["target_count"]),
                "price_basis": info["basis"],
            }
        ]
        if symbol in local_suspensions:
            refs.append(dict(local_suspensions[symbol]))
        if symbol in provider_suspensions:
            refs.append(
                {
                    "source": "akshare.stock_tfp_em",
                    "symbol": symbol,
                    "target_date": target,
                    "status": "returned_code",
                }
            )
        elif network:
            provider_evidence = source_evidence["provider"]
            refs.append(
                {
                    "source": "akshare.stock_tfp_em",
                    "target_date": target,
                    "status": provider_evidence.get("status", "unknown"),
                    "error": provider_evidence.get("error", ""),
                    "matched_count": int(provider_evidence.get("matched_count") or 0),
                }
            )
        lifecycle_detail = lifecycle.get(symbol)
        target_count = int(info["target_count"])
        volumes = list(info["volumes"])
        raw_positive = bool(
            info.get("raw_positive_volume_verified") is True
            or (
                target_count
                and info["basis"] == PRICE_BASIS_RAW
                and int(info.get("volume_valid_count") or 0) == target_count
                and volumes
                and all(value >= 0 for value in volumes)
                and any(value > 0 for value in volumes)
            )
        )
        raw_zero = bool(
            info.get("raw_zero_volume_verified") is True
            or (
                target_count
                and info["basis"] == PRICE_BASIS_RAW
                and volumes
                and int(info.get("volume_valid_count") or 0) == target_count
                and all(value == 0 for value in volumes)
            )
        )
        if raw_positive:
            row_warnings: list[str] = []
            suspension_evidence = (
                dict(local_suspensions[symbol])
                if symbol in local_suspensions
                else {
                    "source": "akshare.stock_tfp_em",
                    "symbol": symbol,
                    "target_date": target,
                    "status": "returned_code",
                    "reason": "akshare_exact_date_suspension",
                }
                if symbol in provider_suspensions
                else None
            )
            if suspension_evidence is not None:
                warning = (
                    f"suspension_conflict:{symbol}:"
                    "verified_suspension_overridden_by_"
                    "raw_target_bar_positive_volume"
                )
                row_warnings.append(warning)
                suspension_conflict_warnings.append(warning)
                refs.append(
                    {
                        **suspension_evidence,
                        "conflict": "suspension_vs_raw_daily",
                        "overridden_by": (
                            "raw_target_bar_positive_volume"
                        ),
                    }
                )
            if lifecycle_detail is not None:
                warning, conflict_ref = _lifecycle_conflict_record(
                    symbol, lifecycle_detail, "raw_target_bar_positive_volume"
                )
                row_warnings.append(warning)
                lifecycle_conflict_warnings.append(warning)
                refs.append(conflict_ref)
            per_symbol[symbol] = _detail(
                symbol,
                "expected_trade",
                "raw_target_bar_positive_volume",
                refs,
                row_warnings,
            )
            continue
        if raw_zero:
            row_warnings = []
            if lifecycle_detail is not None:
                warning, conflict_ref = _lifecycle_conflict_record(
                    symbol, lifecycle_detail, "raw_target_bar_zero_volume"
                )
                row_warnings.append(warning)
                lifecycle_conflict_warnings.append(warning)
                refs.append(conflict_ref)
            per_symbol[symbol] = _detail(
                symbol,
                "verified_full_day_suspension",
                "raw_target_bar_zero_volume",
                refs,
                row_warnings,
            )
            continue
        if info.get("volume_classification_conflict") is True:
            per_symbol[symbol] = _detail(
                symbol,
                "unknown",
                "target_bar_volume_classification_conflict",
                refs,
            )
            continue
        if lifecycle_detail is not None:
            classification, reason, lifecycle_evidence = lifecycle_detail
            refs.append(dict(lifecycle_evidence))
            per_symbol[symbol] = _detail(symbol, classification, reason, refs)
            continue
        if symbol in local_suspensions:
            per_symbol[symbol] = _detail(
                symbol,
                "verified_full_day_suspension",
                str(local_suspensions[symbol]["reason"]),
                refs,
            )
            continue
        if symbol in provider_suspensions:
            per_symbol[symbol] = _detail(
                symbol,
                "verified_full_day_suspension",
                "akshare_exact_date_suspension",
                refs,
            )
            continue
        if target_count and info["basis"] == PRICE_BASIS_RAW:
            reason = "target_bar_volume_missing_or_invalid"
        elif target_count:
            reason = "target_bar_price_basis_not_raw"
        else:
            reason = "missing_target_bar_and_no_verified_no_session_evidence"
        per_symbol[symbol] = _detail(symbol, "unknown", reason, refs)

    by_classification = {
        classification: sorted(
            symbol for symbol, row in per_symbol.items()
            if row["classification"] == classification
        )
        for classification in CLASSIFICATIONS
    }
    counts = {classification: len(items) for classification, items in by_classification.items()}
    legitimate = sorted(
        symbol for symbol, row in per_symbol.items()
        if row["classification"] in LEGITIMATE_NO_SESSION_CLASSIFICATIONS
    )
    unknown = by_classification["unknown"]
    warnings: list[str] = []
    for source_name in ("security_master", "suspension_status", "suspension_history"):
        source = source_evidence[source_name]
        if not source["exists"]:
            warnings.append(f"missing_local_source:{source_name}")
        elif not source["read_ok"]:
            warnings.append(f"unreadable_local_source:{source_name}")
        elif source.get("error"):
            warnings.append(
                f"invalid_local_source:{source_name}:{source['error']}"
            )
    if not network:
        warnings.append("provider_skipped:network_disabled")
    if unknown:
        warnings.append(f"unknown_session_expectation_symbols:{len(unknown)}")
    warnings.extend(baostock_evidence_warnings)
    warnings.extend(lifecycle_conflict_warnings)
    warnings.extend(suspension_conflict_warnings)
    warnings.extend(
        f"target_volume_classification_conflict:{symbol}"
        for symbol in sorted(volume_classification_conflicts)
    )
    payload: dict[str, Any] = {
        "schema_version": "session_expectation.v1",
        "research_only": True,
        "target_date": target,
        "provider_date": target.replace("-", ""),
        "network_enabled": bool(network),
        "project_root": str(root),
        "symbols": normalized_symbols,
        "classification_counts": counts,
        "counts": counts,
        "per_symbol": per_symbol,
        "expected_trade_symbols": by_classification["expected_trade"],
        "legitimate_no_session_symbols": legitimate,
        "unknown_symbols": unknown,
        "source_evidence": source_evidence,
        "sources": source_evidence,
        "provider_evidence": source_evidence["provider"],
        "warnings": list(dict.fromkeys(warnings)),
        "boundary": "research_only; paper_only; no_broker_route; not_investment_advice",
        "output_path": str(Path(output_path)) if output_path is not None else "",
    }
    if output_path is not None:
        atomic_write_json(output_path, payload)
    return payload


def evaluate_session_expectation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_session_expectation(*args, **kwargs)


def assess_session_expectation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_session_expectation(*args, **kwargs)


def build_session_expectation_payload(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_session_expectation(*args, **kwargs)


def classify_session_expectation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_session_expectation(*args, **kwargs)


__all__ = [
    "CLASSIFICATIONS",
    "LEGITIMATE_NO_SESSION_CLASSIFICATIONS",
    "build_session_expectation",
    "evaluate_session_expectation",
    "assess_session_expectation",
    "build_session_expectation_payload",
    "classify_session_expectation",
]
