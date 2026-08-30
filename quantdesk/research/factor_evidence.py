"""Fail-closed factor evidence contract for research-only consumers.

The module does not calculate a new factor, choose weights, update the default
screen, or write runtime state.  It validates already-computed factor frames
before cutoff/sort operations, applies the existing row-level PIT filter, and
joins only dated sector membership into formal neutralisation evidence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

import pandas as pd

from quantdesk.price_basis import PRICE_DATA_CONTRACT_VERSION, validate_price_frame_set
from quantdesk.research.point_in_time_universe import (
    filter_rows_for_pit_membership,
    summarize_pit_universe_status,
)
from quantdesk.shared import normalize_symbol


_BOUNDARY = {
    "research_only": True,
    "paper_only": True,
    "paper_account_only": True,
    "no_broker": True,
    "no_broker_route": True,
    "is_real_order": False,
    "no_real_orders": True,
    "not_investment_advice": True,
    "not_position_advice": True,
}


@dataclass(frozen=True)
class FactorEvidenceConfig:
    target_date: str
    factor_columns: tuple[str, ...]
    min_cross_section: int = 2
    synthetic_test_data: bool = False
    require_industry_for_neutralization: bool = True
    require_market_cap_for_neutralization: bool = True
    include_st: bool = False
    include_suspended: bool = False

    def __post_init__(self) -> None:
        if not str(self.target_date or "").strip():
            raise ValueError("target_date is required")
        if not isinstance(self.factor_columns, tuple):
            raise TypeError("factor_columns must be a tuple")
        if not self.factor_columns or any(not str(item).strip() for item in self.factor_columns):
            raise ValueError("factor_columns must contain non-empty names")
        if len(set(self.factor_columns)) != len(self.factor_columns):
            raise ValueError("factor_columns must be unique")
        if isinstance(self.min_cross_section, bool) or not isinstance(self.min_cross_section, int):
            raise TypeError("min_cross_section must be an integer")
        if self.min_cross_section < 1:
            raise ValueError("min_cross_section must be >= 1")
        for name in (
            "synthetic_test_data",
            "require_industry_for_neutralization",
            "require_market_cap_for_neutralization",
            "include_st",
            "include_suspended",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")


@dataclass(frozen=True)
class FactorEvidenceSnapshot:
    schema_version: str
    status: str
    evidence_eligible: bool
    as_of: str
    data_cutoff: str
    rows: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = deepcopy(dict(self.metadata))
        payload.update(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "evidence_eligible": self.evidence_eligible,
                "as_of": self.as_of,
                "data_cutoff": self.data_cutoff,
                "rows": [deepcopy(dict(row)) for row in self.rows],
            }
        )
        return payload


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def _append_reason(values: list[str], reason: str) -> None:
    if reason and reason not in values:
        values.append(reason)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def compute_factor_evidence_row_digest(
    panel: pd.DataFrame,
    columns: list[str] | tuple[str, ...],
) -> str:
    """Hash the exact semantic rows authorised by a factor evidence snapshot."""

    ordered_columns = [str(column) for column in columns]
    if panel is None or any(column not in panel.columns for column in ordered_columns):
        return ""
    frame = panel[ordered_columns].copy(deep=True)
    if "date" in frame.columns:
        parsed = pd.to_datetime(frame["date"], errors="coerce")
        if parsed.isna().any():
            return ""
        frame["date"] = parsed.dt.strftime("%Y-%m-%d")
    sort_columns = [column for column in ("date", "symbol") if column in frame.columns]
    if sort_columns:
        frame = frame.sort_values(sort_columns).reset_index(drop=True)
    records = [
        {column: _json_value(row.get(column)) for column in ordered_columns}
        for row in frame.to_dict(orient="records")
    ]
    payload = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _empty_sector_audit(status: str, reason_codes: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "evidence_kind": "static_map_proxy" if status == "proxy_non_pit" else "missing",
        "used_for_formal_fit": False,
        "requested_row_count": 0,
        "matched_row_count": 0,
        "missing_row_count": 0,
        "ambiguous_row_count": 0,
        "future_membership_used_count": 0,
        "coverage": 0.0,
        "reason_codes": list(reason_codes),
    }


def join_sector_membership_as_of(
    panel: pd.DataFrame,
    membership: pd.DataFrame | None,
    *,
    symbol_col: str = "symbol",
    date_col: str = "date",
    industry_col: str = "industry",
    sector_col: str = "sector",
    effective_from_col: str = "effective_from",
    effective_to_col: str = "effective_to",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach dated membership without projecting a future classification back."""

    frame = panel.copy(deep=True)
    for column in (industry_col, sector_col):
        if column in frame.columns:
            frame = frame.drop(columns=[column])
    requested = int(len(frame))
    if membership is None or membership.empty:
        audit = _empty_sector_audit("missing", ["sector_membership_missing"])
        audit["requested_row_count"] = requested
        audit["missing_row_count"] = requested
        return frame, audit

    source = membership.copy(deep=True)
    required = {symbol_col, effective_from_col}
    if not required.issubset(source.columns):
        audit = _empty_sector_audit("proxy_non_pit", ["static_sector_map_proxy_non_pit"])
        audit["requested_row_count"] = requested
        audit["missing_row_count"] = requested
        return frame, audit
    if industry_col not in source.columns and sector_col not in source.columns:
        audit = _empty_sector_audit("blocked", ["sector_membership_classification_missing"])
        audit["requested_row_count"] = requested
        audit["missing_row_count"] = requested
        return frame, audit

    source[symbol_col] = source[symbol_col].map(normalize_symbol)
    source[effective_from_col] = pd.to_datetime(source[effective_from_col], errors="coerce")
    if effective_to_col in source.columns:
        source[effective_to_col] = pd.to_datetime(source[effective_to_col], errors="coerce")
    else:
        source[effective_to_col] = pd.NaT
    invalid = source[effective_from_col].isna() | (
        source[effective_to_col].notna()
        & (source[effective_to_col] < source[effective_from_col])
    )
    if bool(invalid.any()):
        audit = _empty_sector_audit("blocked", ["sector_membership_invalid_effective_dates"])
        audit["requested_row_count"] = requested
        audit["missing_row_count"] = requested
        return frame, audit

    by_symbol = {
        symbol: rows.copy()
        for symbol, rows in source.groupby(symbol_col, sort=False)
        if symbol
    }
    industries: list[Any] = []
    sectors: list[Any] = []
    matched = 0
    missing = 0
    ambiguous = 0
    for symbol, value in frame[[symbol_col, date_col]].itertuples(index=False, name=None):
        normalized = normalize_symbol(symbol)
        target = pd.to_datetime(value, errors="coerce")
        choices = by_symbol.get(normalized)
        if choices is None or pd.isna(target):
            industries.append(pd.NA)
            sectors.append(pd.NA)
            missing += 1
            continue
        active = choices[
            (choices[effective_from_col] <= target)
            & (choices[effective_to_col].isna() | (choices[effective_to_col] >= target))
        ]
        if len(active) != 1:
            industries.append(pd.NA)
            sectors.append(pd.NA)
            if active.empty:
                missing += 1
            else:
                ambiguous += 1
            continue
        row = active.iloc[0]
        industries.append(row.get(industry_col, pd.NA))
        sectors.append(row.get(sector_col, pd.NA))
        matched += 1

    if industry_col in source.columns:
        frame[industry_col] = industries
    if sector_col in source.columns:
        frame[sector_col] = sectors
    reasons: list[str] = []
    if missing:
        reasons.append("sector_membership_missing_as_of_rows")
    if ambiguous:
        reasons.append("sector_membership_ambiguous_as_of_rows")
    used = not reasons and requested > 0
    audit = {
        "status": "passed" if used else "partial",
        "evidence_kind": "dated_as_of_membership",
        "used_for_formal_fit": used,
        "requested_row_count": requested,
        "matched_row_count": matched,
        "missing_row_count": missing,
        "ambiguous_row_count": ambiguous,
        "future_membership_used_count": 0,
        "coverage": round(matched / requested, 6) if requested else 0.0,
        "reason_codes": reasons,
    }
    return frame, audit


def _blocked_snapshot(
    *,
    config: FactorEvidenceConfig,
    provenance: Mapping[str, Any],
    price_policy: Mapping[str, Any],
    reason_codes: list[str],
) -> dict[str, Any]:
    metadata = {
        "task_type": "factor_evidence_snapshot",
        "ok": False,
        "source_path": str(provenance.get("source_path") or ""),
        "source_hash": str(provenance.get("source_hash") or ""),
        "provenance": deepcopy(dict(provenance)),
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": deepcopy(dict(price_policy)),
        "pit_status": {"status": "blocked", "pit_universe": False},
        "pit_audit": {},
        "sector_membership": _empty_sector_audit("missing", []),
        "coverage": {"symbol_count": 0, "date_count": 0, "row_count": 0},
        "factor_columns": list(config.factor_columns),
        "row_columns": [],
        "row_digest": "",
        "formal_factor_evaluation_eligible": False,
        "formal_neutralization_eligible": False,
        "synthetic_test_data": bool(config.synthetic_test_data),
        "excluded_future_row_count": 0,
        "reason_codes": list(reason_codes),
        "degraded_reasons": list(reason_codes),
        "weight_policy": {
            "missing_factor_reweighting": False,
            "missing_neutralization_field_fill": False,
        },
        "writes_default_screen": False,
        "changes_default_scoring": False,
        "writes_state": False,
        **_BOUNDARY,
    }
    return FactorEvidenceSnapshot(
        schema_version="1.0",
        status="blocked",
        evidence_eligible=False,
        as_of=str(config.target_date),
        data_cutoff="",
        rows=(),
        metadata=MappingProxyType(metadata),
    ).to_dict()


def build_factor_evidence_snapshot(
    frames: Mapping[str, pd.DataFrame],
    *,
    pit_listings: pd.DataFrame,
    sector_membership: pd.DataFrame | None,
    config: FactorEvidenceConfig,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic, no-write factor evidence snapshot."""

    source_provenance = deepcopy(dict(provenance or {}))
    original_frames = {
        str(symbol): frame
        for symbol, frame in dict(frames or {}).items()
        if isinstance(frame, pd.DataFrame)
    }
    # This must remain the first data operation: validate the complete original
    # frame set before copies, cutoffs, sorting, deduplication, or calculation.
    price_policy = validate_price_frame_set(original_frames, consumer="feature")
    blocking: list[str] = []
    reasons: list[str] = []
    degraded: list[str] = []
    if not price_policy.get("eligible"):
        for code in price_policy.get("reason_codes") or ["price_policy_validation_failed"]:
            _append_reason(blocking, str(code))
            _append_reason(reasons, str(code))
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )

    target = _parse_date(config.target_date)
    if target is None:
        _append_reason(blocking, "target_date_invalid")
    if not str(source_provenance.get("source_path") or ""):
        _append_reason(blocking, "factor_source_path_missing")
    if not str(source_provenance.get("source_hash") or ""):
        _append_reason(blocking, "factor_source_hash_missing")
    if not original_frames:
        _append_reason(blocking, "factor_frames_missing")
    for code in blocking:
        _append_reason(reasons, code)
    if blocking:
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )

    prepared: list[pd.DataFrame] = []
    invalid_date_rows = 0
    for symbol, original in original_frames.items():
        frame = original.copy(deep=True)
        expected_symbol = normalize_symbol(symbol)
        if "symbol" not in frame.columns:
            frame["symbol"] = expected_symbol
        else:
            frame["symbol"] = frame["symbol"].map(normalize_symbol)
            observed_symbols = {str(item) for item in frame["symbol"].dropna().unique() if str(item)}
            if observed_symbols != {expected_symbol}:
                _append_reason(blocking, f"factor_frame_symbol_mismatch:{expected_symbol}")
        if "date" not in frame.columns:
            _append_reason(blocking, f"factor_date_column_missing:{symbol}")
            continue
        parsed_dates = pd.to_datetime(frame["date"], errors="coerce")
        invalid_date_rows += int(parsed_dates.isna().sum())
        frame["date"] = parsed_dates
        prepared.append(frame)
    if invalid_date_rows:
        _append_reason(blocking, "factor_invalid_date_rows")
    if not prepared:
        _append_reason(blocking, "factor_rows_missing")
    if blocking:
        for code in blocking:
            _append_reason(reasons, code)
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )

    panel = pd.concat(prepared, ignore_index=True)
    if "close" not in panel.columns:
        _append_reason(reasons, "factor_close_column_missing")
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )
    panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
    if not bool(panel["close"].notna().all() and (panel["close"] > 0).all()):
        _append_reason(reasons, "factor_close_values_invalid")
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )
    duplicate_mask = panel.duplicated(["symbol", "date"], keep=False)
    if bool(duplicate_mask.any()):
        _append_reason(reasons, "duplicate_factor_symbol_date_rows")
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )
    future_mask = panel["date"].dt.date > target
    excluded_future = int(future_mask.sum())
    if excluded_future:
        _append_reason(degraded, "future_rows_excluded_after_price_validation")
    panel = panel.loc[~future_mask].copy()
    panel = panel.sort_values(["date", "symbol"]).reset_index(drop=True)

    missing_factor_columns = [column for column in config.factor_columns if column not in panel.columns]
    if missing_factor_columns:
        for column in missing_factor_columns:
            _append_reason(reasons, f"factor_column_missing:{column}")
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )
    for column in config.factor_columns:
        panel[column] = pd.to_numeric(panel[column], errors="coerce")
        if bool(panel[column].isin([math.inf, -math.inf]).any()):
            _append_reason(blocking, f"factor_nonfinite_values:{column}")
        if not panel[column].notna().any():
            _append_reason(blocking, f"factor_column_has_no_valid_values:{column}")
    if blocking:
        for code in blocking:
            _append_reason(reasons, code)
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )

    pit_summary = summarize_pit_universe_status(pit_listings)
    if pit_listings is None or pit_listings.empty or not pit_summary.get("pit_universe"):
        _append_reason(reasons, "pit_membership_evidence_unavailable")
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )
    pit_contract_complete = (
        _safe_number(pit_summary.get("list_date_coverage")) == 1.0
        and str(pit_summary.get("survivorship_bias_risk") or "") != "high"
    )
    pit_panel, pit_audit = filter_rows_for_pit_membership(
        panel,
        pit_listings,
        include_st=config.include_st,
        include_suspended=config.include_suspended,
        pit_status=pit_summary,
    )
    if pit_panel.empty:
        _append_reason(reasons, "pit_filter_removed_all_factor_rows")
        return _blocked_snapshot(
            config=config,
            provenance=source_provenance,
            price_policy=price_policy,
            reason_codes=reasons,
        )

    joined, sector_audit = join_sector_membership_as_of(pit_panel, sector_membership)
    if sector_audit.get("evidence_kind") == "dated_as_of_membership":
        sector_provenance = _as_mapping(source_provenance.get("sector_membership"))
        sector_pit_status = _as_mapping(sector_provenance.get("pit_status"))
        if (
            not str(sector_provenance.get("source_path") or "")
            or not str(sector_provenance.get("source_hash") or "")
            or str(sector_pit_status.get("status") or "") != "passed"
        ):
            sector_audit["status"] = "blocked"
            sector_audit["used_for_formal_fit"] = False
            sector_audit.setdefault("reason_codes", []).append(
                "sector_membership_provenance_missing"
            )
        sector_audit["provenance"] = deepcopy(dict(sector_provenance))
    for code in sector_audit.get("reason_codes") or []:
        _append_reason(reasons, str(code))
        _append_reason(degraded, str(code))

    latest_date = pd.Timestamp(joined["date"].max())
    latest_count = int((joined["date"] == latest_date).sum())
    cross_section_ok = latest_count >= int(config.min_cross_section)
    if not cross_section_ok:
        _append_reason(reasons, "insufficient_factor_cross_section")
    if not pit_contract_complete:
        _append_reason(reasons, "pit_membership_coverage_incomplete")

    industry_ok = True
    if config.require_industry_for_neutralization:
        industry_ok = (
            sector_audit.get("used_for_formal_fit") is True
            and "industry" in joined.columns
            and bool(joined["industry"].notna().all())
            and bool(joined["industry"].astype(str).str.strip().ne("").all())
        )
        if industry_ok:
            group_sizes = joined.groupby(["date", "industry"], dropna=False).size()
            industry_ok = bool(not group_sizes.empty and (group_sizes >= 2).all())
            if not industry_ok:
                _append_reason(reasons, "industry_cross_section_insufficient")
        if not industry_ok:
            _append_reason(reasons, "industry_missing_for_neutralization")
    market_cap_ok = True
    if config.require_market_cap_for_neutralization:
        if "market_cap" not in joined.columns:
            market_cap_ok = False
        else:
            caps = pd.to_numeric(joined["market_cap"], errors="coerce")
            market_cap_ok = bool(caps.notna().all() and (caps > 0).all())
            joined["market_cap"] = caps
            if market_cap_ok:
                cap_counts = joined.groupby("date")["market_cap"].nunique(dropna=True)
                market_cap_ok = bool(not cap_counts.empty and (cap_counts >= 2).all())
                if not market_cap_ok:
                    _append_reason(reasons, "market_cap_cross_section_insufficient")
        if not market_cap_ok:
            _append_reason(reasons, "market_cap_missing_for_neutralization")

    synthetic = config.synthetic_test_data is True
    if synthetic:
        _append_reason(reasons, "synthetic_test_data_not_formal_evidence")
    base_evidence = cross_section_ok and pit_contract_complete and not synthetic
    formal_neutral = base_evidence and industry_ok and market_cap_ok
    soft_reasons = {
        "static_sector_map_proxy_non_pit",
        "sector_membership_missing",
        "sector_membership_missing_as_of_rows",
        "sector_membership_ambiguous_as_of_rows",
        "industry_missing_for_neutralization",
        "market_cap_missing_for_neutralization",
        "industry_cross_section_insufficient",
        "market_cap_cross_section_insufficient",
        "sector_membership_provenance_missing",
    }
    blocking_reasons = [code for code in reasons if code not in soft_reasons]
    evidence_eligible = base_evidence and not [
        code for code in blocking_reasons if code != "synthetic_test_data_not_formal_evidence"
    ]
    if synthetic:
        evidence_eligible = False

    output_columns = ["date", "symbol"]
    if "close" in joined.columns:
        output_columns.append("close")
    output_columns.extend(config.factor_columns)
    for optional in ("market_cap", "industry", "sector"):
        if optional in joined.columns:
            output_columns.append(optional)
    rows = tuple(
        MappingProxyType({key: _json_value(value) for key, value in row.items()})
        for row in joined[output_columns].to_dict(orient="records")
    )
    row_digest = compute_factor_evidence_row_digest(joined, output_columns)
    pit_contract = deepcopy(dict(pit_summary))
    pit_contract["status"] = "passed" if pit_contract_complete else "blocked"
    coverage = {
        "symbol_count": int(joined["symbol"].nunique()),
        "date_count": int(joined["date"].nunique()),
        "row_count": int(len(joined)),
        "requested_row_count": int(pit_audit.get("requested_row_count") or 0),
        "pit_included_row_count": int(pit_audit.get("included_row_count") or 0),
        "pit_excluded_row_count": int(pit_audit.get("excluded_row_count") or 0),
        "latest_cross_section_count": latest_count,
        "min_cross_section": int(config.min_cross_section),
    }
    all_degraded = list(dict.fromkeys([*degraded, *[code for code in reasons if code in soft_reasons]]))
    if synthetic:
        status = "synthetic_test_only"
    elif not evidence_eligible:
        status = "blocked"
    elif reasons or all_degraded:
        status = "warning"
    else:
        status = "passed"
    metadata = {
        "task_type": "factor_evidence_snapshot",
        "ok": evidence_eligible,
        "source_path": str(source_provenance.get("source_path") or ""),
        "source_hash": str(source_provenance.get("source_hash") or ""),
        "provenance": source_provenance,
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": deepcopy(price_policy),
        "pit_status": pit_contract,
        "pit_audit": deepcopy(pit_audit),
        "sector_membership": deepcopy(sector_audit),
        "coverage": coverage,
        "factor_columns": list(config.factor_columns),
        "row_columns": list(output_columns),
        "row_digest": row_digest,
        "formal_factor_evaluation_eligible": evidence_eligible,
        "formal_neutralization_eligible": formal_neutral,
        "synthetic_test_data": synthetic,
        "excluded_future_row_count": excluded_future,
        "reason_codes": reasons,
        "degraded_reasons": all_degraded,
        "weight_policy": {
            "missing_factor_reweighting": False,
            "missing_neutralization_field_fill": False,
        },
        "writes_default_screen": False,
        "changes_default_scoring": False,
        "writes_state": False,
        **_BOUNDARY,
    }
    snapshot = FactorEvidenceSnapshot(
        schema_version="1.0",
        status=status,
        evidence_eligible=evidence_eligible,
        as_of=target.isoformat(),
        data_cutoff=latest_date.strftime("%Y-%m-%d"),
        rows=rows,
        metadata=MappingProxyType(metadata),
    )
    return snapshot.to_dict()


__all__ = [
    "FactorEvidenceConfig",
    "FactorEvidenceSnapshot",
    "build_factor_evidence_snapshot",
    "join_sector_membership_as_of",
    "compute_factor_evidence_row_digest",
]
