from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from quantdesk.price_basis import PRICE_DATA_CONTRACT_VERSION
from quantdesk.research.factor_evidence import compute_factor_evidence_row_digest
from quantdesk.shared import now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PIT_UNIVERSE_WARNING = (
    "Backtest/research universe may have survivorship bias because point-in-time "
    "universe reconstruction is not enabled."
)
PIT_UNIVERSE_PARTIAL_WARNING = (
    "Point-in-time universe metadata is available for research diagnostics, but "
    "the research universe is still partially reconstructed; survivorship bias "
    "may remain."
)
PIT_UNIVERSE_AVAILABLE_ASSUMPTION = (
    "Point-in-time universe metadata is available for research diagnostics."
)
DISCLAIMER = "Research diagnostics only. Not investment advice. Factor evaluation does not place orders or change trading decisions."


@dataclass(frozen=True)
class FactorEvaluationConfig:
    horizons: tuple[int, ...] = (1, 5, 10, 20)
    quantiles: int = 5
    min_cross_section: int = 10
    date_col: str = "date"
    symbol_col: str = "symbol"
    price_col: str = "close"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if pd.isna(value) if not isinstance(value, (dict, list, tuple)) else False:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def pit_universe_assumption(pit_universe_status: dict[str, Any] | None = None) -> str:
    """Return the honest PIT-universe limitation sentence for reports/payloads."""

    pit_status = dict(pit_universe_status or {})
    if not pit_status.get("pit_universe"):
        return PIT_UNIVERSE_WARNING
    warnings = {str(item) for item in pit_status.get("warnings", []) if item}
    quality = str(pit_status.get("pit_quality") or "").strip().lower()
    if quality in {"degraded", "partial_metadata_enriched"} or any("degraded" in item for item in warnings):
        return PIT_UNIVERSE_PARTIAL_WARNING
    return PIT_UNIVERSE_AVAILABLE_ASSUMPTION


def pit_universe_warnings(pit_universe_status: dict[str, Any] | None = None) -> list[str]:
    """Return PIT warnings without claiming 'not enabled' when PIT metadata exists."""

    pit_status = dict(pit_universe_status or {})
    warnings = [str(item) for item in pit_status.get("warnings", []) if item]
    if not pit_status.get("pit_universe"):
        return list(dict.fromkeys([PIT_UNIVERSE_WARNING] + warnings))
    return list(dict.fromkeys(warnings))


def _pearson_corr(left: Any, right: Any) -> float:
    try:
        import numpy as np

        x = pd.to_numeric(pd.Series(left), errors="coerce").to_numpy(dtype=float)
        y = pd.to_numeric(pd.Series(right), errors="coerce").to_numpy(dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        x = x[mask]
        y = y[mask]
        if x.size < 2:
            return 0.0
        x = x - x.mean()
        y = y - y.mean()
        den = math.sqrt(float(np.dot(x, x))) * math.sqrt(float(np.dot(y, y)))
        if den == 0:
            return 0.0
        return _safe_float(float(np.dot(x, y)) / den)
    except Exception:
        pass
    pairs: list[tuple[float, float]] = []
    for a, b in zip(list(left), list(right), strict=False):
        try:
            fa = float(a)
            fb = float(b)
        except Exception:
            continue
        if math.isfinite(fa) and math.isfinite(fb):
            pairs.append((fa, fb))
    if len(pairs) < 2:
        return 0.0
    xs = [item[0] for item in pairs]
    ys = [item[1] for item in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return 0.0
    return _safe_float(num / (den_x * den_y))


def _prepare_panel(panel: pd.DataFrame, config: FactorEvaluationConfig) -> pd.DataFrame:
    frame = panel.copy()
    if config.date_col not in frame.columns or config.symbol_col not in frame.columns or config.price_col not in frame.columns:
        return pd.DataFrame(columns=list(frame.columns))
    frame[config.date_col] = pd.to_datetime(frame[config.date_col], errors="coerce")
    frame[config.price_col] = pd.to_numeric(frame[config.price_col], errors="coerce")
    frame = frame.dropna(subset=[config.date_col, config.symbol_col, config.price_col])
    frame = frame[frame[config.price_col] > 0]
    frame[config.symbol_col] = frame[config.symbol_col].astype(str)
    return frame.sort_values([config.symbol_col, config.date_col]).reset_index(drop=True)


def compute_forward_returns(
    panel: pd.DataFrame,
    horizons: Iterable[int] = (1, 5, 10, 20),
    *,
    date_col: str = "date",
    symbol_col: str = "symbol",
    price_col: str = "close",
) -> pd.DataFrame:
    """Append forward_return_Nd columns using future prices only as labels."""

    config = FactorEvaluationConfig(tuple(int(h) for h in horizons), date_col=date_col, symbol_col=symbol_col, price_col=price_col)
    frame = _prepare_panel(panel, config)
    if frame.empty:
        return frame
    grouped = frame.groupby(symbol_col, group_keys=False)[price_col]
    for horizon in config.horizons:
        frame[f"forward_return_{horizon}d"] = grouped.transform(lambda s, h=horizon: s.shift(-h) / s - 1)
    return frame


def compute_daily_ic(
    frame: pd.DataFrame,
    factor_col: str,
    forward_col: str,
    *,
    date_col: str = "date",
    min_cross_section: int = 10,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for date, group in frame.groupby(date_col):
        valid = group[[factor_col, forward_col]].replace([math.inf, -math.inf], pd.NA).dropna()
        if len(valid) < min_cross_section:
            continue
        if valid[factor_col].nunique(dropna=True) < 2 or valid[forward_col].nunique(dropna=True) < 2:
            continue
        ic = _pearson_corr(valid[factor_col], valid[forward_col])
        rank_ic = _pearson_corr(valid[factor_col].rank(method="average"), valid[forward_col].rank(method="average"))
        rows.append({"date": date, "ic": _safe_float(ic), "rank_ic": _safe_float(rank_ic), "sample_count": int(len(valid))})
    return pd.DataFrame(rows)


def compute_quantile_returns(
    frame: pd.DataFrame,
    factor_col: str,
    forward_col: str,
    *,
    date_col: str = "date",
    quantiles: int = 5,
    min_cross_section: int = 10,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for date, group in frame.groupby(date_col):
        valid = group[[factor_col, forward_col]].replace([math.inf, -math.inf], pd.NA).dropna()
        if len(valid) < min_cross_section or valid[factor_col].nunique(dropna=True) < 2:
            continue
        bins = min(int(quantiles), int(valid[factor_col].nunique(dropna=True)), int(len(valid)))
        if bins < 2:
            continue
        try:
            labels = pd.qcut(valid[factor_col].rank(method="first"), q=bins, labels=False, duplicates="drop")
        except ValueError:
            continue
        tmp = valid.assign(_quantile=labels.astype("float"))
        for quantile, q_group in tmp.groupby("_quantile"):
            rows.append({"date": date, "quantile": int(quantile) + 1, "return": _safe_float(q_group[forward_col].mean())})
    if not rows:
        return {
            "by_quantile": {},
            "long_short_return_mean": 0.0,
            "monotonicity_score": 0.0,
            "sample_count": 0,
        }
    q_frame = pd.DataFrame(rows)
    means = q_frame.groupby("quantile")["return"].mean().sort_index()
    top = _safe_float(means.iloc[-1]) if len(means) else 0.0
    bottom = _safe_float(means.iloc[0]) if len(means) else 0.0
    monotonicity = _pearson_corr(means.reset_index()["quantile"], means.reset_index()["return"]) if len(means) > 1 else 0.0
    return {
        "by_quantile": {str(int(k)): _safe_float(v) for k, v in means.items()},
        "long_short_return_mean": _safe_float(top - bottom),
        "monotonicity_score": _safe_float(monotonicity),
        "sample_count": int(len(q_frame)),
    }


def _compute_daily_ic_by_forward_col(
    frame: pd.DataFrame,
    factor_col: str,
    forward_cols: list[str],
    *,
    date_col: str = "date",
    min_cross_section: int = 10,
) -> dict[str, pd.DataFrame]:
    rows_by_forward = {column: [] for column in dict.fromkeys(forward_cols)}
    if not rows_by_forward or factor_col not in frame.columns:
        return {column: pd.DataFrame() for column in rows_by_forward}
    columns = [date_col, factor_col] + [column for column in rows_by_forward if column in frame.columns]
    if date_col not in frame.columns or len(columns) <= 2:
        return {column: pd.DataFrame() for column in rows_by_forward}
    work = frame[columns].copy()
    work[factor_col] = pd.to_numeric(work[factor_col], errors="coerce")
    for column in rows_by_forward:
        if column in work.columns:
            work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.replace([math.inf, -math.inf], pd.NA)
    for date, group in work.groupby(date_col, sort=False):
        factor_values = group[factor_col]
        if factor_values.notna().sum() < min_cross_section or factor_values.nunique(dropna=True) < 2:
            continue
        for forward_col in rows_by_forward:
            if forward_col not in group.columns:
                continue
            valid = pd.DataFrame({"factor": factor_values, "forward": group[forward_col]}).dropna()
            if len(valid) < min_cross_section:
                continue
            if valid["factor"].nunique(dropna=True) < 2 or valid["forward"].nunique(dropna=True) < 2:
                continue
            ic = _pearson_corr(valid["factor"], valid["forward"])
            rank_ic = _pearson_corr(valid["factor"].rank(method="average"), valid["forward"].rank(method="average"))
            rows_by_forward[forward_col].append({"date": date, "ic": _safe_float(ic), "rank_ic": _safe_float(rank_ic), "sample_count": int(len(valid))})
    return {column: pd.DataFrame(rows) for column, rows in rows_by_forward.items()}


def summarize_factor_health(metric: dict[str, Any]) -> str:
    coverage = _safe_float(metric.get("coverage"))
    sample_count = int(metric.get("sample_count") or 0)
    ic_date_count = int(metric.get("ic_date_count") or 0)
    rank_ic_mean = abs(_safe_float(metric.get("rank_ic_mean")))
    rank_icir = abs(_safe_float(metric.get("rank_icir")))
    positive_ratio = _safe_float(metric.get("rank_ic_positive_ratio"))
    if sample_count <= 0 or coverage <= 0 or ic_date_count <= 0:
        return "insufficient_data"
    if metric.get("invalid"):
        return "invalid"
    if rank_ic_mean >= 0.05 and rank_icir >= 0.5 and positive_ratio >= 0.55:
        return "strong"
    if rank_ic_mean >= 0.02 or rank_icir >= 0.2:
        return "usable"
    return "weak"


def _mean_std_ir(values: pd.Series) -> tuple[float, float, float, float]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return 0.0, 0.0, 0.0, 0.0
    mean = _safe_float(clean.mean())
    std = _safe_float(clean.std(ddof=1), 0.0) if len(clean) > 1 else 0.0
    ir = mean / std if std else 0.0
    pos = _safe_float((clean > 0).mean())
    return mean, std, _safe_float(ir), pos


def evaluate_factors(
    panel: pd.DataFrame,
    factor_columns: list[str],
    *,
    date_col: str = "date",
    symbol_col: str = "symbol",
    price_col: str = "close",
    horizons: tuple[int, ...] = (1, 5, 10, 20),
    quantiles: int = 5,
    min_cross_section: int = 10,
    pit_universe_status: dict[str, Any] | None = None,
    factor_evidence_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = FactorEvaluationConfig(tuple(int(h) for h in horizons), int(quantiles), int(min_cross_section), date_col, symbol_col, price_col)
    pit_status = dict(pit_universe_status or {})
    pit_available = bool(pit_status.get("pit_universe"))
    warnings: list[str] = pit_universe_warnings(pit_status)
    evidence = dict(factor_evidence_snapshot or {})
    evidence_reasons: list[str] = []
    if not evidence:
        evidence_reasons.append("factor_evidence_snapshot_missing")
    else:
        if evidence.get("evidence_eligible") is not True:
            evidence_reasons.append("factor_evidence_ineligible")
        if evidence.get("formal_factor_evaluation_eligible") is not True:
            evidence_reasons.append("formal_factor_evidence_ineligible")
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
        if not set(factor_columns).issubset(evidence_factors):
            evidence_reasons.append("factor_evidence_columns_mismatch")
        evidence_coverage = evidence.get("coverage")
        if not isinstance(evidence_coverage, dict) or int(evidence_coverage.get("row_count") or 0) != len(panel):
            evidence_reasons.append("factor_evidence_row_count_mismatch")
        panel_dates = pd.to_datetime(panel.get(date_col, pd.Series(dtype=object)), errors="coerce")
        panel_cutoff = panel_dates.max()
        panel_cutoff_text = panel_cutoff.strftime("%Y-%m-%d") if pd.notna(panel_cutoff) else ""
        if str(evidence.get("data_cutoff") or "") != panel_cutoff_text:
            evidence_reasons.append("factor_evidence_data_cutoff_mismatch")
        row_columns = evidence.get("row_columns")
        expected_digest = str(evidence.get("row_digest") or "")
        actual_digest = (
            compute_factor_evidence_row_digest(panel, row_columns)
            if isinstance(row_columns, (list, tuple))
            else ""
        )
        if not expected_digest or actual_digest != expected_digest:
            evidence_reasons.append("factor_evidence_row_digest_mismatch")
    frame = compute_forward_returns(panel, config.horizons, date_col=date_col, symbol_col=symbol_col, price_col=price_col)
    if frame.empty:
        warnings.append("no_daily_data: no valid panel rows with date, symbol, and close were available")
    factor_metrics: dict[str, Any] = {}
    skipped_factors: list[str] = []
    main_horizon = config.horizons[1] if len(config.horizons) > 1 else config.horizons[0]
    main_forward_col = f"forward_return_{main_horizon}d"

    universe = {
        "symbol_count": int(frame[symbol_col].nunique()) if not frame.empty and symbol_col in frame.columns else 0,
        "date_count": int(frame[date_col].nunique()) if not frame.empty and date_col in frame.columns else 0,
        "sample_count": int(len(frame)),
        "pit_universe": pit_available,
        "pit_status": {
            "available": bool(pit_status.get("available", False)),
            "pit_universe": pit_available,
            "symbol_count": int(pit_status.get("symbol_count") or 0),
            "list_date_coverage": pit_status.get("list_date_coverage") or 0.0,
            "delist_date_coverage": pit_status.get("delist_date_coverage") or 0.0,
        },
    }

    for factor in factor_columns:
        if factor not in frame.columns:
            skipped_factors.append(factor)
            factor_metrics[factor] = {
                "coverage": 0.0,
                "ic_mean": 0.0,
                "ic_std": 0.0,
                "icir": 0.0,
                "ic_positive_ratio": 0.0,
                "rank_ic_mean": 0.0,
                "rank_ic_std": 0.0,
                "rank_icir": 0.0,
                "rank_ic_positive_ratio": 0.0,
                "long_short_return_mean": 0.0,
                "monotonicity_score": 0.0,
                "decay": {f"{h}d": 0.0 for h in config.horizons},
                "sample_count": 0,
                "date_count": 0,
                "symbol_count": 0,
                "health_status": "insufficient_data",
            }
            continue
        frame[factor] = pd.to_numeric(frame[factor], errors="coerce")
        valid_factor = frame[[date_col, symbol_col, factor, main_forward_col]].replace([math.inf, -math.inf], pd.NA).dropna()
        coverage = _safe_float(frame[factor].notna().mean()) if len(frame) else 0.0
        if len(valid_factor) < config.min_cross_section:
            warnings.append(f"{factor}: insufficient cross-section for factor evaluation")
        forward_cols = [f"forward_return_{horizon}d" for horizon in config.horizons]
        daily_ic_by_forward = _compute_daily_ic_by_forward_col(
            frame,
            factor,
            forward_cols,
            date_col=date_col,
            min_cross_section=config.min_cross_section,
        )
        daily_ic = daily_ic_by_forward.get(main_forward_col, pd.DataFrame())
        if daily_ic.empty:
            warnings.append(f"{factor}: insufficient daily cross-section for IC calculation")
        ic_mean, ic_std, icir, ic_positive_ratio = _mean_std_ir(daily_ic.get("ic", pd.Series(dtype=float)))
        rank_ic_mean, rank_ic_std, rank_icir, rank_ic_positive_ratio = _mean_std_ir(daily_ic.get("rank_ic", pd.Series(dtype=float)))
        decay: dict[str, float] = {}
        for horizon in config.horizons:
            forward_col = f"forward_return_{horizon}d"
            horizon_ic = daily_ic_by_forward.get(forward_col, pd.DataFrame())
            decay[f"{horizon}d"] = _mean_std_ir(horizon_ic.get("rank_ic", pd.Series(dtype=float)))[0]
        quantile = compute_quantile_returns(
            frame,
            factor,
            main_forward_col,
            date_col=date_col,
            quantiles=config.quantiles,
            min_cross_section=config.min_cross_section,
        )
        metric = {
            "coverage": coverage,
            "ic_mean": ic_mean,
            "ic_std": ic_std,
            "icir": icir,
            "ic_positive_ratio": ic_positive_ratio,
            "rank_ic_mean": rank_ic_mean,
            "rank_ic_std": rank_ic_std,
            "rank_icir": rank_icir,
            "rank_ic_positive_ratio": rank_ic_positive_ratio,
            "long_short_return_mean": _safe_float(quantile.get("long_short_return_mean")),
            "monotonicity_score": _safe_float(quantile.get("monotonicity_score")),
            "quantile_returns": quantile.get("by_quantile") or {},
            "decay": decay,
            "sample_count": int(len(valid_factor)),
            "ic_date_count": int(len(daily_ic)),
            "date_count": int(valid_factor[date_col].nunique()) if not valid_factor.empty else 0,
            "symbol_count": int(valid_factor[symbol_col].nunique()) if not valid_factor.empty else 0,
        }
        metric["health_status"] = summarize_factor_health(metric)
        factor_metrics[factor] = metric

    health_values = [str(m.get("health_status")) for m in factor_metrics.values()]
    has_usable = any(item in {"strong", "usable"} for item in health_values)
    status = "passed" if has_usable and not warnings else "warning"
    if not factor_metrics:
        status = "warning"
        warnings.append("no_factor_columns: no factor columns were requested")

    result = {
        "ok": True,
        "status": status,
        "task_type": "factor_evaluation",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "local_panel",
        "target_date": frame[date_col].max().strftime("%Y-%m-%d") if not frame.empty and pd.notna(frame[date_col].max()) else "",
        "network": False,
        "config": {
            "horizons": list(config.horizons),
            "quantiles": config.quantiles,
            "min_cross_section": config.min_cross_section,
        },
        "universe": universe,
        "factor_metrics": factor_metrics,
        "formal_factor_evaluation_eligible": not evidence_reasons,
        "factor_evidence_source_hash": str(evidence.get("source_hash") or ""),
        "factor_evidence_snapshot_status": str(evidence.get("status") or "missing"),
        "reason_codes": list(dict.fromkeys(evidence_reasons)),
        "changes_default_screen": False,
        "skipped_dates": [],
        "skipped_factors": skipped_factors,
        "warnings": list(dict.fromkeys(warnings)),
        "assumptions": [pit_universe_assumption(pit_status)],
        "output_paths": {},
        "suggested_next_action": "Review the factor evaluation report before considering any research changes.",
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(result)


def build_factor_evaluation_report(payload: dict[str, Any]) -> str:
    metrics = payload.get("factor_metrics") if isinstance(payload.get("factor_metrics"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    lines = [
        "# QuantDesk Factor Evaluation Report",
        "",
        f"- generated_at: `{payload.get('generated_at') or '-'}`",
        f"- app_version: `{payload.get('app_version') or '-'}`",
        f"- data_source: `{payload.get('data_source') or '-'}`",
        f"- target_date: `{payload.get('target_date') or '-'}`",
        f"- symbols: `{universe.get('symbol_count', 0)}`",
        f"- dates: `{universe.get('date_count', 0)}`",
        f"- samples: `{universe.get('sample_count', 0)}`",
        f"- horizons: `{config.get('horizons') or []}`",
        f"- quantiles: `{config.get('quantiles')}`",
        "",
        "## PIT Universe Limitation",
        "",
        PIT_UNIVERSE_WARNING,
        "",
        "## Factor Overview",
        "",
        "| Factor | Coverage | IC Mean | ICIR | Rank IC Mean | Rank ICIR | LS Return | Health |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    if metrics:
        for factor, metric in metrics.items():
            lines.append(
                f"| {factor} | {_safe_float(metric.get('coverage')):.3f} | "
                f"{_safe_float(metric.get('ic_mean')):.4f} | {_safe_float(metric.get('icir')):.3f} | "
                f"{_safe_float(metric.get('rank_ic_mean')):.4f} | {_safe_float(metric.get('rank_icir')):.3f} | "
                f"{_safe_float(metric.get('long_short_return_mean')):.4f} | {metric.get('health_status') or 'unknown'} |"
            )
    else:
        lines.append("| none | 0.000 | 0.0000 | 0.000 | 0.0000 | 0.000 | 0.0000 | insufficient_data |")
    lines.extend(["", "## IC Decay", ""])
    for factor, metric in metrics.items():
        lines.append(f"- {factor}: `{metric.get('decay') or {}}`")
    lines.extend(["", "## Quantile Returns", ""])
    for factor, metric in metrics.items():
        lines.append(f"- {factor}: `{metric.get('quantile_returns') or {}}`")
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


def write_factor_evaluation_outputs(
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
    latest_json = output_path / "factor_health_latest.json"
    timestamp_json = output_path / f"factor_health_{stamp}.json"
    report = report_path / f"factor_evaluation_report_{stamp}.md"
    latest_report = report_path / "factor_evaluation_report_latest.md"
    payload = dict(payload)
    payload["output_paths"] = {
        "latest_json": str(latest_json).replace("\\", "/"),
        "timestamp_json": str(timestamp_json).replace("\\", "/"),
        "report": str(report).replace("\\", "/"),
        "latest_report": str(latest_report).replace("\\", "/"),
    }
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    report_text = build_factor_evaluation_report(payload)
    report.write_text(report_text, encoding="utf-8")
    latest_report.write_text(report_text, encoding="utf-8")
    try:
        from quantdesk.experiment_tracker import log_experiment_non_blocking

        factor_metrics = payload.get("factor_metrics") if isinstance(payload.get("factor_metrics"), dict) else {}
        statuses = [str(item.get("health_status") or "") for item in factor_metrics.values() if isinstance(item, dict)]
        tracking = log_experiment_non_blocking(
            "factor_evaluation",
            {"task_type": payload.get("task_type"), "universe": payload.get("universe", {})},
            {
                "factor_count": len(factor_metrics),
                "usable_factor_count": sum(1 for item in statuses if item in {"strong", "usable"}),
                "weak_factor_count": sum(1 for item in statuses if item == "weak"),
            },
            output_paths=payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {},
            tags=["auto_logged"],
        )
        if tracking.get("experiment_id"):
            payload["experiment_id"] = tracking.get("experiment_id")
        if tracking.get("warning") and not tracking.get("ok"):
            payload.setdefault("warnings", []).append(tracking["warning"])
    except Exception as exc:
        payload.setdefault("warnings", []).append(f"experiment_tracker_failed:{exc}")
    return payload
