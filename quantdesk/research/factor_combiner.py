from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from quantdesk.price_basis import PRICE_DATA_CONTRACT_VERSION
from quantdesk.research.factor_evidence import compute_factor_evidence_row_digest
from quantdesk.factor_evaluation import (
    PIT_UNIVERSE_WARNING,
    pit_universe_assumption,
    pit_universe_warnings,
)
from quantdesk.shared import now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

DISCLAIMER = (
    "Research diagnostics only. Not investment advice. Combined score is not "
    "wired into default trading signals, screen, backtest, daily workflow, or real orders."
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    if not math.isfinite(number):
        return default
    return number


def _pearson_corr(left: Any, right: Any) -> float:
    pairs: list[tuple[float, float]] = []
    for a, b in zip(list(left), list(right), strict=False):
        try:
            fa, fb = float(a), float(b)
        except Exception:
            continue
        if math.isfinite(fa) and math.isfinite(fb):
            pairs.append((fa, fb))
    if len(pairs) < 2:
        return 0.0
    xs = [item[0] for item in pairs]
    ys = [item[1] for item in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return _safe_float(num / (dx * dy))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _with_warnings(frame: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    frame.attrs["warnings"] = warnings
    return frame


def _factor_output_names(factor_columns: Iterable[str], suffix: str) -> list[str]:
    return [f"{factor}{suffix}" for factor in factor_columns]


def winsorize_cross_section(
    panel: pd.DataFrame,
    factor_columns: list[str],
    date_col: str = "date",
    lower: float = 0.01,
    upper: float = 0.99,
) -> pd.DataFrame:
    frame = panel.copy()
    warnings: list[str] = []
    if date_col not in frame.columns:
        warnings.append(f"missing {date_col}; winsorize skipped")
        return _with_warnings(frame, warnings)
    for factor in factor_columns:
        if factor not in frame.columns:
            warnings.append(f"{factor}: missing factor column; winsorize skipped")
            continue
        frame[factor] = pd.to_numeric(frame[factor], errors="coerce")
        out_col = f"{factor}_winsorized"
        frame[out_col] = pd.to_numeric(frame[factor], errors="coerce").astype(float)
        for _, idx in frame.groupby(date_col).groups.items():
            series = frame.loc[idx, factor].dropna()
            if len(series) < 3:
                warnings.append(f"{factor}: insufficient cross-section for winsorize on one or more dates")
                continue
            lo = series.quantile(lower)
            hi = series.quantile(upper)
            numeric = pd.to_numeric(frame.loc[idx, factor], errors="coerce").astype(float)
            frame.loc[idx, out_col] = numeric.mask(numeric < lo, lo).mask(numeric > hi, hi)
    return _with_warnings(frame, list(dict.fromkeys(warnings)))


def standardize_cross_section(
    panel: pd.DataFrame,
    factor_columns: list[str],
    date_col: str = "date",
    method: str = "zscore",
) -> pd.DataFrame:
    frame = panel.copy()
    warnings: list[str] = []
    if date_col not in frame.columns:
        warnings.append(f"missing {date_col}; standardization skipped")
        return _with_warnings(frame, warnings)
    method = str(method or "zscore").lower()
    for factor in factor_columns:
        if factor not in frame.columns:
            warnings.append(f"{factor}: missing factor column; standardization skipped")
            continue
        frame[factor] = pd.to_numeric(frame[factor], errors="coerce")
        out_col = f"{factor}_{method}"
        frame[out_col] = 0.0
        for _, idx in frame.groupby(date_col).groups.items():
            series = frame.loc[idx, factor]
            valid = series.dropna()
            if valid.empty:
                warnings.append(f"{factor}: no valid values for standardization on one or more dates")
                continue
            if method == "percentile_rank":
                ranks = series.rank(pct=True)
                frame.loc[idx, out_col] = (ranks * 2 - 1).fillna(0.0)
            else:
                std = _safe_float(valid.std(ddof=0), 0.0)
                if std == 0:
                    warnings.append(f"{factor}: constant cross-section standardized to 0 on one or more dates")
                    frame.loc[idx, out_col] = 0.0
                else:
                    frame.loc[idx, out_col] = ((series - valid.mean()) / std).fillna(0.0)
    return _with_warnings(frame, list(dict.fromkeys(warnings)))


def neutralize_by_group(
    panel: pd.DataFrame,
    factor_columns: list[str],
    group_col: str = "industry",
    date_col: str = "date",
) -> pd.DataFrame:
    frame = panel.copy()
    warnings: list[str] = []
    if date_col not in frame.columns or group_col not in frame.columns:
        warnings.append(f"missing {group_col} or {date_col}; industry neutralization unavailable")
        return _with_warnings(frame, warnings)
    for factor in factor_columns:
        if factor not in frame.columns:
            warnings.append(f"{factor}: missing factor column; industry neutralization skipped")
            continue
        out_col = f"{factor}_industry_neutral"
        values = pd.to_numeric(frame[factor], errors="coerce")
        means = values.groupby([frame[date_col], frame[group_col]], dropna=False).transform("mean")
        counts = values.groupby([frame[date_col], frame[group_col]], dropna=False).transform("count")
        frame[out_col] = (values - means).where(counts >= 2, values).fillna(0.0)
        if (counts < 2).any():
            warnings.append(f"{factor}: one or more industry groups had fewer than 2 samples")
    return _with_warnings(frame, list(dict.fromkeys(warnings)))


def _ols_residuals(y: pd.Series, x: pd.Series) -> pd.Series:
    valid = pd.DataFrame({"y": y, "x": x}).replace([math.inf, -math.inf], pd.NA).dropna()
    out = pd.Series(0.0, index=y.index, dtype=float)
    if len(valid) < 3 or valid["x"].nunique() < 2:
        return pd.to_numeric(y, errors="coerce").fillna(0.0)
    x_mean = valid["x"].mean()
    y_mean = valid["y"].mean()
    denom = ((valid["x"] - x_mean) ** 2).sum()
    if denom == 0:
        return pd.to_numeric(y, errors="coerce").fillna(0.0)
    beta = ((valid["x"] - x_mean) * (valid["y"] - y_mean)).sum() / denom
    alpha = y_mean - beta * x_mean
    out.loc[valid.index] = valid["y"] - (alpha + beta * valid["x"])
    return out.fillna(0.0)


def neutralize_by_market_cap(
    panel: pd.DataFrame,
    factor_columns: list[str],
    market_cap_col: str = "market_cap",
    date_col: str = "date",
) -> pd.DataFrame:
    frame = panel.copy()
    warnings: list[str] = []
    if date_col not in frame.columns or market_cap_col not in frame.columns:
        warnings.append(f"missing {market_cap_col} or {date_col}; market-cap neutralization unavailable")
        return _with_warnings(frame, warnings)
    market_cap = pd.to_numeric(frame[market_cap_col], errors="coerce")
    frame["_log_market_cap_for_neutralize"] = market_cap.where(market_cap > 0).map(lambda x: math.log(x) if pd.notna(x) else pd.NA)
    for factor in factor_columns:
        if factor not in frame.columns:
            warnings.append(f"{factor}: missing factor column; market-cap neutralization skipped")
            continue
        out_col = f"{factor}_market_cap_neutral"
        frame[out_col] = 0.0
        for _, idx in frame.groupby(date_col).groups.items():
            y = pd.to_numeric(frame.loc[idx, factor], errors="coerce")
            x = pd.to_numeric(frame.loc[idx, "_log_market_cap_for_neutralize"], errors="coerce")
            valid_count = pd.DataFrame({"y": y, "x": x}).dropna().shape[0]
            if valid_count < 3:
                warnings.append(f"{factor}: insufficient market-cap samples on one or more dates")
            frame.loc[idx, out_col] = _ols_residuals(y, x)
    frame = frame.drop(columns=["_log_market_cap_for_neutralize"])
    return _with_warnings(frame, list(dict.fromkeys(warnings)))


def orthogonalize_factors(
    panel: pd.DataFrame,
    factor_columns: list[str],
    date_col: str = "date",
    method: str = "gram_schmidt",
) -> pd.DataFrame:
    frame = panel.copy()
    warnings: list[str] = []
    if date_col not in frame.columns:
        warnings.append(f"missing {date_col}; orthogonalization skipped")
        return _with_warnings(frame, warnings)
    for factor in factor_columns:
        frame[f"{factor}_orthogonalized"] = 0.0
    if method != "gram_schmidt":
        warnings.append(f"unsupported orthogonalization method {method}; using gram_schmidt")
    for _, idx in frame.groupby(date_col).groups.items():
        basis: list[pd.Series] = []
        for factor in factor_columns:
            out_col = f"{factor}_orthogonalized"
            if factor not in frame.columns:
                warnings.append(f"{factor}: missing factor column; orthogonalization skipped")
                continue
            vector = pd.to_numeric(frame.loc[idx, factor], errors="coerce").fillna(0.0).astype(float)
            if vector.nunique(dropna=True) < 2:
                warnings.append(f"{factor}: constant factor skipped during orthogonalization on one or more dates")
                frame.loc[idx, out_col] = 0.0
                continue
            residual = vector - vector.mean()
            for base in basis:
                denom = float((base * base).sum())
                if denom:
                    residual = residual - float((residual * base).sum()) / denom * base
            norm = math.sqrt(float((residual * residual).sum()))
            if norm < 1e-10:
                warnings.append(f"{factor}: collinear factor skipped during orthogonalization on one or more dates")
                frame.loc[idx, out_col] = 0.0
                continue
            ortho = residual / norm
            basis.append(ortho)
            frame.loc[idx, out_col] = ortho
    return _with_warnings(frame, list(dict.fromkeys(warnings)))


def build_factor_weights(
    factor_health: dict[str, Any] | None,
    factor_columns: list[str],
    method: str = "rank_icir",
    min_abs_ic: float = 0.0,
    allow_negative: bool = False,
) -> tuple[dict[str, float], list[str]]:
    details = build_factor_weight_details(
        factor_health,
        factor_columns,
        method=method,
        min_abs_ic=min_abs_ic,
        allow_negative=allow_negative,
    )
    return details["normalized_weights"], details["warnings"]


def build_factor_weight_details(
    factor_health: dict[str, Any] | None,
    factor_columns: list[str],
    method: str = "rank_icir",
    min_abs_ic: float = 0.0,
    allow_negative: bool = False,
) -> dict[str, Any]:
    warnings: list[str] = []
    method = str(method or "rank_icir").lower()
    metrics = factor_health.get("factor_metrics") if isinstance(factor_health, dict) else None
    raw: dict[str, float] = {}
    if method == "equal_weight" or not isinstance(metrics, dict):
        if method != "equal_weight":
            warnings.append("missing_factor_health: fallback to equal_weight")
        weight = 1.0 / max(1, len(factor_columns))
        weights = {factor: weight for factor in factor_columns}
        return {
            "raw_weights": {factor: 1.0 for factor in factor_columns},
            "normalized_weights": weights,
            "warnings": warnings,
            "fallback_reason": "missing_factor_health" if method != "equal_weight" else "",
        }
    key_map = {
        "ic_mean": "ic_mean",
        "rank_ic_mean": "rank_ic_mean",
        "icir": "icir",
        "rank_icir": "rank_icir",
    }
    metric_key = key_map.get(method)
    if metric_key is None:
        warnings.append(f"unsupported weight_method {method}; fallback to equal_weight")
        weight = 1.0 / max(1, len(factor_columns))
        weights = {factor: weight for factor in factor_columns}
        return {
            "raw_weights": {factor: 1.0 for factor in factor_columns},
            "normalized_weights": weights,
            "warnings": warnings,
            "fallback_reason": f"unsupported_weight_method:{method}",
        }
    for factor in factor_columns:
        metric = metrics.get(factor) if isinstance(metrics.get(factor), dict) else {}
        value = _safe_float(metric.get(metric_key), 0.0)
        if abs(value) < min_abs_ic:
            value = 0.0
        if not allow_negative and value < 0:
            value = 0.0
        raw[factor] = value
    total = sum(abs(v) for v in raw.values()) if allow_negative else sum(v for v in raw.values() if v > 0)
    if total == 0:
        warnings.append("factor_health weights were empty; fallback to equal_weight")
        weight = 1.0 / max(1, len(factor_columns))
        weights = {factor: weight for factor in factor_columns}
        return {
            "raw_weights": raw,
            "normalized_weights": weights,
            "warnings": warnings,
            "fallback_reason": "zero_effective_factor_health_weights",
        }
    return {
        "raw_weights": raw,
        "normalized_weights": {factor: _safe_float(value / total) for factor, value in raw.items()},
        "warnings": warnings,
        "fallback_reason": "",
    }


def build_combined_score(
    panel: pd.DataFrame,
    factor_columns: list[str],
    *,
    date_col: str = "date",
    symbol_col: str = "symbol",
    industry_col: str = "industry",
    market_cap_col: str = "market_cap",
    standardize_method: str = "zscore",
    winsorize: bool = True,
    winsorize_lower: float = 0.01,
    winsorize_upper: float = 0.99,
    neutralize_industry: bool = True,
    neutralize_market_cap: bool = True,
    orthogonalize: bool = False,
    weight_method: str = "rank_icir",
    factor_health: dict[str, Any] | None = None,
    allow_negative_weights: bool = False,
    manual_weights: dict[str, float] | None = None,
    pit_universe_status: dict[str, Any] | None = None,
    factor_evidence_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pit_status = dict(pit_universe_status or {})
    warnings: list[str] = pit_universe_warnings(pit_status)
    pit_available = bool(pit_status.get("pit_universe"))
    frame = panel.copy()
    if frame.empty or date_col not in frame.columns or symbol_col not in frame.columns:
        warnings.append("no_daily_data: no valid panel rows available for factor combination")
        return _empty_payload(
            factor_columns,
            warnings,
            standardize_method,
            neutralize_industry,
            neutralize_market_cap,
            orthogonalize,
            weight_method,
            pit_universe_status=pit_status,
        )
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame = frame.dropna(subset=[date_col, symbol_col]).sort_values([date_col, symbol_col]).reset_index(drop=True)
    available_factors = [factor for factor in factor_columns if factor in frame.columns]
    missing_factors = [factor for factor in factor_columns if factor not in frame.columns]
    for factor in missing_factors:
        warnings.append(f"{factor}: missing factor column; excluded from combined score")
    if not available_factors:
        warnings.append("no_factor_columns: no requested factor columns were available")
        return _empty_payload(factor_columns, warnings, standardize_method, neutralize_industry, neutralize_market_cap, orthogonalize, weight_method)

    working_factors = list(available_factors)
    if winsorize:
        win = winsorize_cross_section(frame, working_factors, date_col=date_col, lower=winsorize_lower, upper=winsorize_upper)
        warnings.extend(win.attrs.get("warnings", []))
        frame = win
        working_factors = _factor_output_names(working_factors, "_winsorized")

    std = standardize_cross_section(frame, working_factors, date_col=date_col, method=standardize_method)
    warnings.extend(std.attrs.get("warnings", []))
    frame = std
    working_factors = _factor_output_names(working_factors, f"_{standardize_method}")

    neutralization = {
        "industry_enabled": bool(neutralize_industry),
        "industry_status": "skipped",
        "market_cap_enabled": bool(neutralize_market_cap),
        "market_cap_status": "skipped",
    }
    if neutralize_industry:
        ind = neutralize_by_group(frame, working_factors, group_col=industry_col, date_col=date_col)
        warnings.extend(ind.attrs.get("warnings", []))
        frame = ind
        if industry_col in frame.columns:
            working_factors = _factor_output_names(working_factors, "_industry_neutral")
            neutralization["industry_status"] = "applied"
        else:
            neutralization["industry_status"] = "unavailable"
    if neutralize_market_cap:
        cap = neutralize_by_market_cap(frame, working_factors, market_cap_col=market_cap_col, date_col=date_col)
        warnings.extend(cap.attrs.get("warnings", []))
        frame = cap
        if market_cap_col in frame.columns:
            working_factors = _factor_output_names(working_factors, "_market_cap_neutral")
            neutralization["market_cap_status"] = "applied"
        else:
            neutralization["market_cap_status"] = "unavailable"

    if orthogonalize:
        ortho = orthogonalize_factors(frame, working_factors, date_col=date_col)
        warnings.extend(ortho.attrs.get("warnings", []))
        frame = ortho
        working_factors = _factor_output_names(working_factors, "_orthogonalized")

    raw_manual_weights = dict(manual_weights or {})
    if raw_manual_weights:
        unknown = sorted(set(raw_manual_weights) - set(available_factors))
        if unknown:
            warnings.append(f"unknown_manual_weight_factors:{','.join(unknown)}")
        weights = {factor: float(raw_manual_weights.get(factor, 0.0)) for factor in available_factors}
        total_abs = sum(abs(value) for value in weights.values())
        if total_abs == 0:
            warnings.append("manual_weights_sum_zero")
        else:
            weights = {factor: _safe_float(value / total_abs) for factor, value in weights.items()}
        weight_warnings = []
    else:
        weight_details = build_factor_weight_details(
            factor_health,
            available_factors,
            method=weight_method,
            allow_negative=allow_negative_weights,
        )
        weights = weight_details["normalized_weights"]
        raw_factor_weights = weight_details["raw_weights"]
        fallback_reason = weight_details.get("fallback_reason", "")
        weight_warnings = weight_details["warnings"]
        warnings.extend(weight_warnings)
    frame["factor_coverage"] = frame[working_factors].notna().mean(axis=1).fillna(0.0)
    frame["combined_score_raw"] = 0.0
    for original, processed in zip(available_factors, working_factors, strict=False):
        frame[processed] = pd.to_numeric(frame[processed], errors="coerce").fillna(0.0)
        frame["combined_score_raw"] += frame[processed] * float(weights.get(original, 0.0))
    frame["combined_score"] = pd.to_numeric(frame["combined_score_raw"], errors="coerce").fillna(0.0)
    frame["combined_score_rank"] = frame.groupby(date_col)["combined_score"].rank(ascending=False, method="first").astype(int)

    latest_date = frame[date_col].max()
    latest = frame[frame[date_col] == latest_date].sort_values("combined_score", ascending=False)
    score = pd.to_numeric(frame["combined_score"], errors="coerce").replace([math.inf, -math.inf], pd.NA).dropna()
    insufficient_cross_section = bool(len(latest) < max(2, len(available_factors)))
    if insufficient_cross_section:
        warnings.append(f"insufficient_cross_section:{len(latest)}")
    score_panel = [
        {
            "date": row[date_col].strftime("%Y-%m-%d") if hasattr(row[date_col], "strftime") else str(row[date_col]),
            "symbol": str(row[symbol_col]),
            "combined_score": _safe_float(row["combined_score"]),
            "factor_coverage": _safe_float(row["factor_coverage"]),
        }
        for _, row in frame[[date_col, symbol_col, "combined_score", "factor_coverage"]].dropna(subset=[date_col, symbol_col]).iterrows()
    ]
    top_candidates = [
        {
            "date": row[date_col].strftime("%Y-%m-%d") if hasattr(row[date_col], "strftime") else str(row[date_col]),
            "symbol": str(row[symbol_col]),
            "combined_score": _safe_float(row["combined_score"]),
            "combined_score_rank": int(row["combined_score_rank"]),
            "coverage": _safe_float(row["factor_coverage"]),
        }
        for _, row in latest.head(20).iterrows()
    ]
    evidence = dict(factor_evidence_snapshot or {})
    evidence_reasons: list[str] = []
    if not evidence:
        evidence_reasons.append("factor_evidence_snapshot_missing")
    else:
        if evidence.get("evidence_eligible") is not True:
            evidence_reasons.append("factor_evidence_ineligible")
        if evidence.get("formal_neutralization_eligible") is not True:
            evidence_reasons.append("factor_neutralization_evidence_ineligible")
        if evidence.get("synthetic_test_data") is True:
            evidence_reasons.append("synthetic_factor_evidence_not_formal")
        if not str(evidence.get("source_path") or "") or not str(evidence.get("source_hash") or ""):
            evidence_reasons.append("factor_evidence_provenance_missing")
        policy = evidence.get("price_policy_validation")
        if not isinstance(policy, dict) or policy.get("eligible") is not True:
            evidence_reasons.append("factor_evidence_price_policy_ineligible")
        elif (
            str(evidence.get("price_data_contract_version") or "")
            != PRICE_DATA_CONTRACT_VERSION
            or str(policy.get("contract_version") or "") != PRICE_DATA_CONTRACT_VERSION
        ):
            evidence_reasons.append("factor_evidence_price_contract_version_mismatch")
        evidence_pit = evidence.get("pit_status")
        if not isinstance(evidence_pit, dict) or str(evidence_pit.get("status") or "") != "passed":
            evidence_reasons.append("factor_evidence_pit_ineligible")
        evidence_factors = {str(item) for item in (evidence.get("factor_columns") or [])}
        if not set(available_factors).issubset(evidence_factors):
            evidence_reasons.append("factor_evidence_columns_mismatch")
        latest_text = latest_date.strftime("%Y-%m-%d") if pd.notna(latest_date) else ""
        if str(evidence.get("data_cutoff") or "") != latest_text:
            evidence_reasons.append("factor_evidence_data_cutoff_mismatch")
        evidence_coverage = evidence.get("coverage")
        if not isinstance(evidence_coverage, dict) or int(evidence_coverage.get("row_count") or 0) != len(frame):
            evidence_reasons.append("factor_evidence_row_count_mismatch")
        row_columns = evidence.get("row_columns")
        expected_digest = str(evidence.get("row_digest") or "")
        actual_digest = (
            compute_factor_evidence_row_digest(panel, row_columns)
            if isinstance(row_columns, (list, tuple))
            else ""
        )
        if not expected_digest or actual_digest != expected_digest:
            evidence_reasons.append("factor_evidence_row_digest_mismatch")
    if neutralize_industry and neutralization["industry_status"] != "applied":
        evidence_reasons.append("industry_neutralization_unavailable")
    if neutralize_market_cap and neutralization["market_cap_status"] != "applied":
        evidence_reasons.append("market_cap_neutralization_unavailable")
    formal_neutralized_result_eligible = not evidence_reasons
    status = "passed" if len(frame) and len(available_factors) else "warning"
    if len(warnings) > 1:
        status = "warning"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "factor_combination",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "local_panel",
        "target_date": latest_date.strftime("%Y-%m-%d") if pd.notna(latest_date) else "",
        "network": False,
        "config": {
            "factors": factor_columns,
            "standardize_method": standardize_method,
            "neutralize_industry": bool(neutralize_industry),
            "neutralize_market_cap": bool(neutralize_market_cap),
            "orthogonalize": bool(orthogonalize),
            "weight_method": weight_method,
            "manual_weights": bool(raw_manual_weights),
            "allow_negative_weights": bool(allow_negative_weights),
        },
        "universe": {
            "symbol_count": int(frame[symbol_col].nunique()),
            "date_count": int(frame[date_col].nunique()),
            "sample_count": int(len(frame)),
            "pit_universe": pit_available,
            "pit_status": {
                "available": bool(pit_status.get("available", False)),
                "pit_universe": pit_available,
                "pit_quality": pit_status.get("pit_quality") or "",
                "symbol_count": int(pit_status.get("symbol_count") or 0),
                "list_date_coverage": pit_status.get("list_date_coverage") or 0.0,
                "delist_date_coverage": pit_status.get("delist_date_coverage") or 0.0,
                "st_coverage": pit_status.get("st_coverage") or 0.0,
                "suspend_coverage": pit_status.get("suspend_coverage") or 0.0,
            },
        },
        "factor_weights": weights,
        "factor_names": available_factors,
        "raw_weights": raw_manual_weights or raw_factor_weights,
        "normalized_weights": weights,
        "fallback_reason": "" if raw_manual_weights else fallback_reason,
        "weight_diagnostics": {
            "method": weight_method,
            "allow_negative_weights": bool(allow_negative_weights),
            "normalization": "sum_abs" if allow_negative_weights else "positive_sum",
            "sum_abs_normalized_weights": _safe_float(sum(abs(v) for v in weights.values())),
            "fallback_reason": "" if raw_manual_weights else fallback_reason,
        },
        "cross_section_count": int(len(latest)),
        "insufficient_cross_section": insufficient_cross_section,
        "skipped_symbols": [],
        "factor_coverage": {
            factor: _safe_float(frame[factor].notna().mean()) if factor in frame.columns else 0.0
            for factor in available_factors
        },
        "score_distribution": {
            "count": int(score.count()),
            "mean": _safe_float(score.mean()),
            "std": _safe_float(score.std(ddof=0)),
            "min": _safe_float(score.min()),
            "max": _safe_float(score.max()),
        },
        "top_symbols": [item["symbol"] for item in top_candidates[:20]],
        "score_summary": {
            "score_count": int(score.count()),
            "latest_date": latest_date.strftime("%Y-%m-%d") if pd.notna(latest_date) else "",
            "latest_symbol_count": int(len(latest)),
            "score_mean": _safe_float(score.mean()),
            "score_std": _safe_float(score.std(ddof=0)),
            "score_min": _safe_float(score.min()),
            "score_max": _safe_float(score.max()),
        },
        "score_panel": score_panel,
        "top_candidates": top_candidates,
        "neutralization": neutralization,
        "formal_neutralized_result_eligible": formal_neutralized_result_eligible,
        "factor_evidence_source_hash": str(evidence.get("source_hash") or ""),
        "factor_evidence_snapshot_status": str(evidence.get("status") or "missing"),
        "reason_codes": list(dict.fromkeys(evidence_reasons)),
        "changes_default_screen": False,
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "assumptions": [pit_universe_assumption(pit_status)],
        "output_paths": {},
        "suggested_next_action": "Review the combined score report as research diagnostics only; do not treat rankings as trading advice.",
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def _empty_payload(
    factors: list[str],
    warnings: list[str],
    standardize_method: str,
    neutralize_industry: bool,
    neutralize_market_cap: bool,
    orthogonalize: bool,
    weight_method: str,
    *,
    pit_universe_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pit_status = dict(pit_universe_status or {})
    pit_available = bool(pit_status.get("pit_universe"))
    return {
        "ok": True,
        "status": "warning",
        "task_type": "factor_combination",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "none",
        "target_date": "",
        "network": False,
        "config": {
            "factors": factors,
            "standardize_method": standardize_method,
            "neutralize_industry": bool(neutralize_industry),
            "neutralize_market_cap": bool(neutralize_market_cap),
            "orthogonalize": bool(orthogonalize),
            "weight_method": weight_method,
        },
        "universe": {
            "symbol_count": 0,
            "date_count": 0,
            "sample_count": 0,
            "pit_universe": pit_available,
            "pit_status": {
                "available": bool(pit_status.get("available", False)),
                "pit_universe": pit_available,
                "pit_quality": pit_status.get("pit_quality") or "",
                "symbol_count": int(pit_status.get("symbol_count") or 0),
                "list_date_coverage": pit_status.get("list_date_coverage") or 0.0,
                "delist_date_coverage": pit_status.get("delist_date_coverage") or 0.0,
                "st_coverage": pit_status.get("st_coverage") or 0.0,
                "suspend_coverage": pit_status.get("suspend_coverage") or 0.0,
            },
        },
        "factor_weights": {},
        "factor_names": factors,
        "raw_weights": {},
        "normalized_weights": {},
        "cross_section_count": 0,
        "skipped_symbols": [],
        "factor_coverage": {},
        "insufficient_cross_section": True,
        "score_distribution": {"count": 0, "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0},
        "top_symbols": [],
        "score_summary": {
            "score_count": 0,
            "latest_date": "",
            "latest_symbol_count": 0,
            "score_mean": 0.0,
            "score_std": 0.0,
            "score_min": 0.0,
            "score_max": 0.0,
        },
        "score_panel": [],
        "top_candidates": [],
        "neutralization": {
            "industry_enabled": bool(neutralize_industry),
            "industry_status": "unavailable" if neutralize_industry else "skipped",
            "market_cap_enabled": bool(neutralize_market_cap),
            "market_cap_status": "unavailable" if neutralize_market_cap else "skipped",
        },
        "formal_neutralized_result_eligible": False,
        "factor_evidence_source_hash": "",
        "factor_evidence_snapshot_status": "missing",
        "reason_codes": ["factor_evidence_snapshot_missing"],
        "changes_default_screen": False,
        "warnings": list(dict.fromkeys(warnings)),
        "assumptions": [pit_universe_assumption(pit_status)],
        "output_paths": {},
        "suggested_next_action": "Import local daily data and factor columns, then rerun factor-combine.",
        "disclaimer": DISCLAIMER,
    }


def build_combined_score_report(payload: dict[str, Any]) -> str:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    weights = payload.get("factor_weights") if isinstance(payload.get("factor_weights"), dict) else {}
    summary = payload.get("score_summary") if isinstance(payload.get("score_summary"), dict) else {}
    neutral = payload.get("neutralization") if isinstance(payload.get("neutralization"), dict) else {}
    top = payload.get("top_candidates") if isinstance(payload.get("top_candidates"), list) else []
    lines = [
        "# QuantDesk Combined Score Report",
        "",
        f"- generated_at: `{payload.get('generated_at') or '-'}`",
        f"- app_version: `{payload.get('app_version') or '-'}`",
        f"- data_source: `{payload.get('data_source') or '-'}`",
        f"- target_date: `{payload.get('target_date') or '-'}`",
        f"- symbols: `{universe.get('symbol_count', 0)}`",
        f"- dates: `{universe.get('date_count', 0)}`",
        f"- samples: `{universe.get('sample_count', 0)}`",
        f"- standardize_method: `{config.get('standardize_method')}`",
        f"- orthogonalize: `{config.get('orthogonalize')}`",
        "",
        "## Factor Weights",
        "",
        "| Factor | Weight |",
        "|---|---:|",
    ]
    if weights:
        lines.extend(f"| {factor} | {_safe_float(weight):.6f} |" for factor, weight in weights.items())
    else:
        lines.append("| none | 0.000000 |")
    lines.extend([
        "",
        "## Neutralization",
        "",
        f"- industry: `{neutral.get('industry_status') or 'unknown'}`",
        f"- market_cap: `{neutral.get('market_cap_status') or 'unknown'}`",
        "",
        "## Score Summary",
        "",
        f"- score_count: `{summary.get('score_count', 0)}`",
        f"- latest_date: `{summary.get('latest_date') or '-'}`",
        f"- latest_symbol_count: `{summary.get('latest_symbol_count', 0)}`",
        f"- mean/std/min/max: `{summary.get('score_mean')}` / `{summary.get('score_std')}` / `{summary.get('score_min')}` / `{summary.get('score_max')}`",
        "",
        "## Top Candidates",
        "",
        "| Rank | Symbol | Combined Score | Coverage |",
        "|---:|---|---:|---:|",
    ])
    for item in top[:20]:
        lines.append(
            f"| {int(item.get('combined_score_rank') or 0)} | {item.get('symbol') or '-'} | "
            f"{_safe_float(item.get('combined_score')):.6f} | {_safe_float(item.get('coverage')):.3f} |"
        )
    if not top:
        lines.append("| 0 | none | 0.000000 | 0.000 |")
    lines.extend([
        "",
        "## PIT Universe Limitation",
        "",
    ])
    assumptions = payload.get("assumptions") if isinstance(payload.get("assumptions"), list) else []
    lines.extend([str(item) for item in assumptions if item] or [PIT_UNIVERSE_WARNING])
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    lines.extend([f"- {item}" for item in warnings] or ["- none"])
    lines.extend([
        "",
        "## Suggested Next Action",
        "",
        str(payload.get("suggested_next_action") or "-"),
        "",
        "## Disclaimer",
        "",
        str(payload.get("disclaimer") or DISCLAIMER),
    ])
    return "\n".join(lines) + "\n"


def write_combined_score_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    output_path = Path(output_dir)
    report_path = Path(report_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path.mkdir(parents=True, exist_ok=True)
    stamp = now_text().replace("-", "").replace(":", "").replace("T", "_").replace(" ", "_")
    latest_json = output_path / "combined_score_latest.json"
    timestamp_json = output_path / f"combined_score_{stamp}.json"
    report = report_path / f"combined_score_report_{stamp}.md"
    latest_report = report_path / "combined_score_report_latest.md"
    payload = dict(payload)
    payload["output_paths"] = {
        "latest_json": str(latest_json).replace("\\", "/"),
        "timestamp_json": str(timestamp_json).replace("\\", "/"),
        "report": str(report).replace("\\", "/"),
        "latest_report": str(latest_report).replace("\\", "/"),
    }
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    report_text = build_combined_score_report(payload)
    report.write_text(report_text, encoding="utf-8")
    latest_report.write_text(report_text, encoding="utf-8")
    return payload
