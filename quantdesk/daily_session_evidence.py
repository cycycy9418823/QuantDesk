"""Strict persistent evidence for provider-reported A-share no-session dates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text
from quantdesk.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA_DIR = PROJECT_ROOT / "data" / "metadata"
SCHEMA_VERSION = "baostock_session_evidence.v1"
SOURCE = "baostock.query_history_k_data_plus"
ADJUSTFLAG_RAW = "3"
BSE_SCHEMA_VERSION = "bse_session_interval_evidence.v1"
BSE_SOURCE = "bse.official_disclosure"

_BSE_DOCUMENT_FIELDS = {
    "fact_type",
    "effective_date",
    "publish_date",
    "title",
    "url",
    "sha256",
}
_BSE_PAYLOAD_FIELDS = {
    "schema_version",
    "symbol",
    "source",
    "captured_at",
    "suspension_start_date",
    "resumption_date",
    "documents",
    "evidence_sha256",
}


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _provider_code(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    if normalized.startswith("6"):
        return f"sh.{normalized}"
    if normalized.startswith(("0", "3")):
        return f"sz.{normalized}"
    return ""


def _canonical_records(records: Any) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(records, list):
        return [], ["records_not_list"]
    by_date: dict[str, str] = {}
    errors: list[str] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            errors.append(f"record_not_object:{index}")
            continue
        trade_date = _iso_date(raw.get("trade_date"))
        status = str(raw.get("tradestatus") or "").strip()
        if not trade_date:
            errors.append(f"record_invalid_date:{index}")
            continue
        if status not in {"0", "1"}:
            errors.append(f"record_invalid_tradestatus:{trade_date}:{status or '-'}")
            continue
        previous = by_date.get(trade_date)
        if previous is not None and previous != status:
            errors.append(f"record_conflicting_tradestatus:{trade_date}:{previous},{status}")
            continue
        by_date[trade_date] = status
    canonical = [
        {"trade_date": trade_date, "tradestatus": by_date[trade_date]}
        for trade_date in sorted(by_date)
    ]
    if not canonical:
        errors.append("records_empty")
    return canonical, errors


def records_sha256(records: Any) -> str:
    canonical, errors = _canonical_records(records)
    if errors:
        return ""
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_baostock_session_evidence(
    *,
    symbol: str,
    provider_code: str,
    query_start_date: str,
    query_end_date: str,
    records: list[dict[str, str]],
    captured_at: str | None = None,
) -> dict[str, Any]:
    canonical, errors = _canonical_records(records)
    if errors:
        raise ValueError("invalid_baostock_session_records:" + ",".join(errors))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "symbol": normalize_symbol(symbol),
        "provider_code": str(provider_code or "").strip().lower(),
        "source": SOURCE,
        "adjustflag": ADJUSTFLAG_RAW,
        "query_start_date": _iso_date(query_start_date),
        "query_end_date": _iso_date(query_end_date),
        "provider_error_code": "0",
        "captured_at": str(captured_at or now_text()),
        "records": canonical,
    }
    payload["records_sha256"] = records_sha256(canonical)
    validated = validate_baostock_session_evidence(payload, expected_symbol=symbol)
    if not validated["valid"]:
        raise ValueError(
            "invalid_baostock_session_evidence:"
            + ",".join(validated["reason_codes"])
        )
    return payload


def validate_baostock_session_evidence(
    payload: Any,
    *,
    expected_symbol: str = "",
    expected_start_date: str = "",
    expected_end_date: str = "",
) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(payload, Mapping):
        return {
            "valid": False,
            "reason_codes": ["payload_not_object"],
            "suspended_dates": [],
            "traded_dates": [],
            "evidence": {},
        }

    symbol = normalize_symbol(payload.get("symbol"))
    expected = normalize_symbol(expected_symbol)
    provider_code = str(payload.get("provider_code") or "").strip().lower()
    start = _iso_date(payload.get("query_start_date"))
    end = _iso_date(payload.get("query_end_date"))
    captured_at = str(payload.get("captured_at") or "").strip()
    canonical, record_errors = _canonical_records(payload.get("records"))
    reasons.extend(record_errors)

    if str(payload.get("schema_version") or "") != SCHEMA_VERSION:
        reasons.append("invalid_schema_version")
    if str(payload.get("source") or "") != SOURCE:
        reasons.append("invalid_source")
    if str(payload.get("adjustflag") or "") != ADJUSTFLAG_RAW:
        reasons.append("invalid_adjustflag")
    if str(payload.get("provider_error_code") or "") != "0":
        reasons.append("provider_error_not_zero")
    if not symbol:
        reasons.append("invalid_symbol")
    if expected and symbol != expected:
        reasons.append("symbol_mismatch")
    expected_code = _provider_code(symbol)
    if not expected_code or provider_code != expected_code:
        reasons.append("provider_code_mismatch")
    if not start or not end or start > end:
        reasons.append("invalid_query_range")
    if expected_start_date and start != _iso_date(expected_start_date):
        reasons.append("query_start_mismatch")
    if expected_end_date and end != _iso_date(expected_end_date):
        reasons.append("query_end_mismatch")
    if not captured_at:
        reasons.append("captured_at_missing")
    else:
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            reasons.append("captured_at_invalid")

    if canonical and start and end:
        outside = [
            row["trade_date"]
            for row in canonical
            if row["trade_date"] < start or row["trade_date"] > end
        ]
        if outside:
            reasons.append("record_outside_query_range:" + ",".join(outside))
    supplied_hash = str(payload.get("records_sha256") or "").strip().lower()
    calculated_hash = records_sha256(canonical) if canonical and not record_errors else ""
    if not supplied_hash or supplied_hash != calculated_hash:
        reasons.append("records_sha256_mismatch")

    reasons = list(dict.fromkeys(reasons))
    suspended = [
        row["trade_date"] for row in canonical if row["tradestatus"] == "0"
    ]
    traded = [
        row["trade_date"] for row in canonical if row["tradestatus"] == "1"
    ]
    normalized_payload = dict(payload)
    normalized_payload.update(
        {
            "symbol": symbol,
            "provider_code": provider_code,
            "query_start_date": start,
            "query_end_date": end,
            "records": canonical,
            "records_sha256": calculated_hash,
        }
    )
    return {
        "valid": not reasons,
        "reason_codes": reasons,
        "suspended_dates": suspended if not reasons else [],
        "traded_dates": traded if not reasons else [],
        "evidence": normalized_payload if not reasons else {},
    }


def evidence_path(
    symbol: str,
    *,
    metadata_dir: str | Path | None = None,
) -> Path:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise ValueError(f"invalid_symbol:{symbol!r}")
    base = Path(metadata_dir) if metadata_dir is not None else DEFAULT_METADATA_DIR
    return (
        base
        / "provider_session_evidence"
        / "baostock"
        / f"{normalized}.json"
    )


def write_baostock_session_evidence(
    payload: Mapping[str, Any],
    *,
    metadata_dir: str | Path | None = None,
    expected_symbol: str = "",
    expected_start_date: str = "",
    expected_end_date: str = "",
) -> Path:
    validated = validate_baostock_session_evidence(
        payload,
        expected_symbol=expected_symbol,
        expected_start_date=expected_start_date,
        expected_end_date=expected_end_date,
    )
    if not validated["valid"]:
        raise ValueError(
            "invalid_baostock_session_evidence:"
            + ",".join(validated["reason_codes"])
        )
    normalized = validated["evidence"]
    target_path = evidence_path(
        normalized["symbol"],
        metadata_dir=metadata_dir,
    )
    existing = load_baostock_session_evidence(
        normalized["symbol"],
        metadata_dir=metadata_dir,
    )
    if existing.get("valid"):
        previous = existing["evidence"]
        previous_start = date.fromisoformat(previous["query_start_date"])
        previous_end = date.fromisoformat(previous["query_end_date"])
        current_start = date.fromisoformat(normalized["query_start_date"])
        current_end = date.fromisoformat(normalized["query_end_date"])
        overlaps = (
            current_start <= previous_end + timedelta(days=1)
            and previous_start <= current_end + timedelta(days=1)
        )
        if overlaps:
            previous_by_date = {
                row["trade_date"]: row["tradestatus"]
                for row in previous["records"]
            }
            current_by_date = {
                row["trade_date"]: row["tradestatus"]
                for row in normalized["records"]
            }
            retained_records = [
                row
                for row in previous["records"]
                if not (
                    normalized["query_start_date"]
                    <= row["trade_date"]
                    <= normalized["query_end_date"]
                )
            ]
            corrections = [
                {
                    "trade_date": trade_date,
                    "previous_tradestatus": previous_by_date[trade_date],
                    "corrected_tradestatus": current_status,
                    "captured_at": normalized["captured_at"],
                }
                for trade_date, current_status in sorted(
                    current_by_date.items()
                )
                if trade_date in previous_by_date
                and previous_by_date[trade_date] != current_status
            ]
            merged_records = retained_records + list(normalized["records"])
            merged = build_baostock_session_evidence(
                symbol=normalized["symbol"],
                provider_code=normalized["provider_code"],
                query_start_date=min(
                    previous["query_start_date"],
                    normalized["query_start_date"],
                ),
                query_end_date=max(
                    previous["query_end_date"],
                    normalized["query_end_date"],
                ),
                records=merged_records,
                captured_at=normalized["captured_at"],
            )
            correction_audit = list(
                previous.get("correction_audit") or []
            ) + corrections
            if correction_audit:
                merged["correction_audit"] = correction_audit[-100:]
            normalized = merged
    return atomic_write_json(target_path, normalized)


def load_baostock_session_evidence(
    symbol: str,
    *,
    metadata_dir: str | Path | None = None,
) -> dict[str, Any]:
    path = evidence_path(symbol, metadata_dir=metadata_dir)
    if not path.exists():
        return {
            "status": "missing",
            "valid": False,
            "path": str(path),
            "reason_codes": [],
            "suspended_dates": [],
            "traded_dates": [],
            "evidence": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt evidence must fail closed
        return {
            "status": "invalid",
            "valid": False,
            "path": str(path),
            "reason_codes": [
                f"read_failed:{type(exc).__name__}:{str(exc)[:160]}"
            ],
            "suspended_dates": [],
            "traded_dates": [],
            "evidence": {},
        }
    validated = validate_baostock_session_evidence(
        payload,
        expected_symbol=symbol,
    )
    validated["status"] = "valid" if validated["valid"] else "invalid"
    validated["path"] = str(path)
    return validated


def _canonical_bse_documents(
    documents: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(documents, list):
        return [], ["documents_not_list"]
    canonical: list[dict[str, str]] = []
    reasons: list[str] = []
    for index, raw in enumerate(documents):
        if not isinstance(raw, Mapping):
            reasons.append(f"document_not_object:{index}")
            continue
        unexpected = sorted(set(raw) - _BSE_DOCUMENT_FIELDS)
        if unexpected:
            reasons.append(
                f"document_unexpected_fields:{index}:{','.join(unexpected)}"
            )
        fact_type = str(raw.get("fact_type") or "").strip()
        effective_date = _iso_date(raw.get("effective_date"))
        publish_date = _iso_date(raw.get("publish_date"))
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        sha256 = str(raw.get("sha256") or "").strip().lower()
        if fact_type not in {
            "suspension_start",
            "suspension_progress",
            "resumption",
        }:
            reasons.append(f"document_invalid_fact_type:{index}")
        if not effective_date:
            reasons.append(f"document_invalid_effective_date:{index}")
        if not publish_date:
            reasons.append(f"document_invalid_publish_date:{index}")
        if not title:
            reasons.append(f"document_title_missing:{index}")
        if not (
            url.startswith("https://www.bse.cn/disclosure/")
            and url.lower().endswith(".pdf")
        ):
            reasons.append(f"document_invalid_bse_url:{index}")
        if len(sha256) != 64 or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            reasons.append(f"document_invalid_sha256:{index}")
        canonical.append(
            {
                "fact_type": fact_type,
                "effective_date": effective_date,
                "publish_date": publish_date,
                "title": title,
                "url": url,
                "sha256": sha256,
            }
        )
    canonical.sort(
        key=lambda item: (
            item["effective_date"],
            item["fact_type"],
            item["url"],
        )
    )
    if not canonical:
        reasons.append("documents_empty")
    return canonical, reasons


def _bse_evidence_sha256(payload: Mapping[str, Any]) -> str:
    bound = {
        key: payload.get(key)
        for key in sorted(_BSE_PAYLOAD_FIELDS - {"evidence_sha256"})
    }
    encoded = json.dumps(
        bound,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _interval_dates(start: str, end: str) -> list[str]:
    if not start or not end or start >= end:
        return []
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    values: list[str] = []
    while current < stop:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def build_bse_session_interval_evidence(
    *,
    symbol: str,
    suspension_start_date: str,
    resumption_date: str,
    documents: list[dict[str, str]],
    captured_at: str | None = None,
) -> dict[str, Any]:
    canonical_documents, document_reasons = _canonical_bse_documents(documents)
    if document_reasons:
        raise ValueError(
            "invalid_bse_session_interval_documents:"
            + ",".join(document_reasons)
        )
    payload: dict[str, Any] = {
        "schema_version": BSE_SCHEMA_VERSION,
        "symbol": normalize_symbol(symbol),
        "source": BSE_SOURCE,
        "captured_at": str(captured_at or now_text()),
        "suspension_start_date": _iso_date(suspension_start_date),
        "resumption_date": _iso_date(resumption_date),
        "documents": canonical_documents,
    }
    payload["evidence_sha256"] = _bse_evidence_sha256(payload)
    checked = validate_bse_session_interval_evidence(
        payload,
        expected_symbol=symbol,
    )
    if not checked["valid"]:
        raise ValueError(
            "invalid_bse_session_interval_evidence:"
            + ",".join(checked["reason_codes"])
        )
    return payload


def validate_bse_session_interval_evidence(
    payload: Any,
    *,
    expected_symbol: str = "",
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {
            "valid": False,
            "reason_codes": ["payload_not_object"],
            "no_session_dates": [],
            "evidence": {},
        }
    reasons: list[str] = []
    unexpected = sorted(set(payload) - _BSE_PAYLOAD_FIELDS)
    if unexpected:
        reasons.append("unexpected_fields:" + ",".join(unexpected))
    symbol = normalize_symbol(payload.get("symbol"))
    expected = normalize_symbol(expected_symbol)
    start = _iso_date(payload.get("suspension_start_date"))
    end = _iso_date(payload.get("resumption_date"))
    captured_at = str(payload.get("captured_at") or "").strip()
    documents, document_reasons = _canonical_bse_documents(
        payload.get("documents")
    )
    reasons.extend(document_reasons)
    if str(payload.get("schema_version") or "") != BSE_SCHEMA_VERSION:
        reasons.append("invalid_schema_version")
    if str(payload.get("source") or "") != BSE_SOURCE:
        reasons.append("invalid_source")
    if not symbol or not symbol.startswith("920"):
        reasons.append("invalid_bse_symbol")
    if expected and symbol != expected:
        reasons.append("symbol_mismatch")
    if not start or not end or start >= end:
        reasons.append("invalid_suspension_interval")
    if not captured_at:
        reasons.append("captured_at_missing")
    else:
        try:
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            reasons.append("captured_at_invalid")
    start_facts = [
        document
        for document in documents
        if document["fact_type"] == "suspension_start"
    ]
    resume_facts = [
        document
        for document in documents
        if document["fact_type"] == "resumption"
    ]
    if not start_facts:
        reasons.append("suspension_start_fact_missing")
    elif len(start_facts) != 1 or start_facts[0]["effective_date"] != start:
        reasons.append("suspension_start_fact_mismatch")
    if not resume_facts:
        reasons.append("resumption_fact_missing")
    elif len(resume_facts) != 1 or resume_facts[0]["effective_date"] != end:
        reasons.append("resumption_fact_mismatch")
    normalized = {
        "schema_version": str(payload.get("schema_version") or ""),
        "symbol": symbol,
        "source": str(payload.get("source") or ""),
        "captured_at": captured_at,
        "suspension_start_date": start,
        "resumption_date": end,
        "documents": documents,
    }
    calculated_hash = _bse_evidence_sha256(normalized)
    supplied_hash = str(payload.get("evidence_sha256") or "").strip().lower()
    if not supplied_hash or supplied_hash != calculated_hash:
        reasons.append("evidence_sha256_mismatch")
    reasons = list(dict.fromkeys(reasons))
    normalized["evidence_sha256"] = calculated_hash
    return {
        "valid": not reasons,
        "reason_codes": reasons,
        "no_session_dates": _interval_dates(start, end) if not reasons else [],
        "evidence": normalized if not reasons else {},
    }


def bse_evidence_path(
    symbol: str,
    *,
    metadata_dir: str | Path | None = None,
) -> Path:
    normalized = normalize_symbol(symbol)
    if not normalized or not normalized.startswith("920"):
        raise ValueError(f"invalid_bse_symbol:{symbol!r}")
    base = Path(metadata_dir) if metadata_dir is not None else DEFAULT_METADATA_DIR
    return (
        base
        / "provider_session_evidence"
        / "bse"
        / f"{normalized}.json"
    )


def write_bse_session_interval_evidence(
    payload: Mapping[str, Any],
    *,
    metadata_dir: str | Path | None = None,
    expected_symbol: str = "",
) -> Path:
    checked = validate_bse_session_interval_evidence(
        payload,
        expected_symbol=expected_symbol,
    )
    if not checked["valid"]:
        raise ValueError(
            "invalid_bse_session_interval_evidence:"
            + ",".join(checked["reason_codes"])
        )
    evidence = checked["evidence"]
    target = bse_evidence_path(
        evidence["symbol"],
        metadata_dir=metadata_dir,
    )
    return atomic_write_json(target, evidence)


def load_bse_session_interval_evidence(
    symbol: str,
    *,
    metadata_dir: str | Path | None = None,
) -> dict[str, Any]:
    path = bse_evidence_path(symbol, metadata_dir=metadata_dir)
    if not path.exists():
        return {
            "status": "missing",
            "valid": False,
            "path": str(path),
            "reason_codes": [],
            "no_session_dates": [],
            "evidence": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt evidence must fail closed
        return {
            "status": "invalid",
            "valid": False,
            "path": str(path),
            "reason_codes": [
                f"read_failed:{type(exc).__name__}:{str(exc)[:160]}"
            ],
            "no_session_dates": [],
            "evidence": {},
        }
    checked = validate_bse_session_interval_evidence(
        payload,
        expected_symbol=symbol,
    )
    checked["status"] = "valid" if checked["valid"] else "invalid"
    checked["path"] = str(path)
    return checked


def load_verified_no_session_dates(
    symbols: Iterable[Any],
    *,
    metadata_dir: str | Path | None = None,
) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]], list[str]]:
    dates_by_symbol: dict[str, set[str]] = {}
    evidence_by_symbol: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    normalized_symbols = sorted(
        {
            normalize_symbol(item)
            for item in symbols
            if normalize_symbol(item)
        }
    )
    for symbol in normalized_symbols:
        if symbol.startswith("920"):
            bse_loaded = load_bse_session_interval_evidence(
                symbol, metadata_dir=metadata_dir
            )
            if bse_loaded["status"] != "missing":
                if not bse_loaded["valid"]:
                    warnings.append(
                        "invalid_bse_session_interval_evidence:"
                        + symbol
                        + ":"
                        + ",".join(bse_loaded["reason_codes"])
                    )
                    continue
                no_session = set(bse_loaded["no_session_dates"])
                if no_session:
                    dates_by_symbol[symbol] = no_session
                evidence_by_symbol[symbol] = dict(bse_loaded["evidence"])
                continue
        loaded = load_baostock_session_evidence(
            symbol,
            metadata_dir=metadata_dir,
        )
        if loaded["status"] == "missing":
            loaded = None
        elif not loaded["valid"]:
            warnings.append(
                "invalid_baostock_session_evidence:"
                + symbol
                + ":"
                + ",".join(loaded["reason_codes"])
            )
            loaded = None
        if loaded is not None:
            suspended = set(loaded["suspended_dates"])
            if suspended:
                dates_by_symbol.setdefault(symbol, set()).update(suspended)
            evidence_by_symbol[symbol] = dict(loaded["evidence"])

    # Sina-snapshot suspension evidence produced by the daily refresh fast
    # path (data/metadata/suspension_evidence/<YYYYMMDD>.json). This is the
    # bridge that stops the chain from reporting a suspended symbol as a
    # failed update: the date is a verified no-session date, not a gap.
    snapshot_evidence = _load_sina_suspension_dates(normalized_symbols, metadata_dir=metadata_dir)
    for symbol, dates in snapshot_evidence.items():
        if dates:
            dates_by_symbol.setdefault(symbol, set()).update(dates)
            evidence_by_symbol.setdefault(
                symbol,
                {"source": "sina_snapshot_suspension_evidence", "dates": sorted(dates)},
            )
    return dates_by_symbol, evidence_by_symbol, warnings


def _load_sina_suspension_dates(
    symbols: list[str],
    *,
    metadata_dir: str | Path | None = None,
) -> dict[str, set[str]]:
    """Read sina-snapshot suspension evidence files for the given symbols.

    Scans ``data/metadata/suspension_evidence/*.json`` (one file per trade
    date) and returns ``{symbol: {verified dates}}``. Malformed files are
    skipped with no exception - the caller's warnings already cover the
    producer side, and a broken evidence file must not break the chain.
    """

    import json as _json

    base = Path(metadata_dir) if metadata_dir is not None else DEFAULT_METADATA_DIR
    evidence_dir = base / "suspension_evidence"
    if not evidence_dir.is_dir():
        return {}
    wanted = set(symbols)
    out: dict[str, set[str]] = {}
    for path in sorted(evidence_dir.glob("*.json")):
        try:
            payload = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, _json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        target_date = str(payload.get("target_date") or "").strip()
        digits = "".join(ch for ch in str(path.stem) if ch.isdigit())
        if len(digits) == 8:
            declared = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
            if target_date and target_date != declared:
                continue
        if not target_date or len(target_date) != 10:
            continue
        verified = payload.get("verified_exclusions")
        if not isinstance(verified, dict):
            continue
        for symbol, meta in verified.items():
            normalized = normalize_symbol(symbol)
            if normalized in wanted and isinstance(meta, dict):
                signature = str(meta.get("signature") or "")
                if signature in {
                    "zero_trade_with_estimates",
                    "zero_volume_with_last_close",
                }:
                    out.setdefault(normalized, set()).add(target_date)
    return out


__all__ = [
    "ADJUSTFLAG_RAW",
    "BSE_SCHEMA_VERSION",
    "BSE_SOURCE",
    "DEFAULT_METADATA_DIR",
    "SCHEMA_VERSION",
    "SOURCE",
    "bse_evidence_path",
    "build_bse_session_interval_evidence",
    "build_baostock_session_evidence",
    "evidence_path",
    "load_baostock_session_evidence",
    "load_bse_session_interval_evidence",
    "load_verified_no_session_dates",
    "records_sha256",
    "validate_baostock_session_evidence",
    "validate_bse_session_interval_evidence",
    "write_baostock_session_evidence",
    "write_bse_session_interval_evidence",
]
