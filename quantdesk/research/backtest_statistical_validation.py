"""Statistical validation for existing backtest outputs.

The validation is conservative and diagnostic.  It compares existing backtest
samples with random baselines and bootstrap intervals without changing strategy
parameters, score formulas, or backtest return logic.
"""

from __future__ import annotations

# Compatibility markers: permutation_test_signals bootstrap_metric_confidence_intervals backtest_statistical_validation_latest.json.
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

from quantdesk.shared import now_text, read_json, short_error_text, write_json
from quantdesk.research.evidence_identity import nonformal_mode
from quantdesk.research.sample_provenance import research_sample_context
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_JSON_PATH = RESULTS_DIR / "backtest_statistical_validation_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "backtest_statistical_validation_report_latest.md"
DEFAULT_BACKTEST_RESULT_PATH = Path("results") / "backtest" / "backtest_latest.json"
# There is deliberately NO fallback list. The loader used to try
# results/backtest/latest.json and results/backtest_latest.json whenever the
# requested file had no extractable returns, so a 2026-10-09 run validated a
# 2026-09-07 artefact and reported "passed". A result that cannot be measured is
# reported as "missing"; it is never replaced by an older file.
MIN_TRADES_FOR_VALIDATION = 30
MIN_RETURNS_FOR_VALIDATION = 8
# Return lists whose entries are one closed trade each; their length IS the trade
# count. Period series (daily/window returns, equity curves) are not.
_TRADE_LEVEL_RETURN_SOURCES = {"trade_returns", "trades"}
_TRADE_COUNT_KEYS = ("trades_count", "total_trades", "number_of_trades", "trade_count")
DISCLAIMER = "统计检验仅基于历史样本，不代表未来收益。"
NONFORMAL_INPUT_MESSAGE = "没有真实回测可用于本次检验：所选结果标记为合成、演示或试跑数据，统计检验未运行。"


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except Exception:
        return None
    return numeric if math.isfinite(numeric) else None


def _extract_return_values(candidates: list[Any]) -> list[float]:
    returns: list[float] = []
    for item in candidates:
        if isinstance(item, dict):
            value = None
            for key in ("return", "ret", "pnl_pct", "pct_return", "daily_return", "profit_pct"):
                if key in item:
                    value = item.get(key)
                    break
        else:
            value = item
        numeric = _as_float(value)
        if numeric is not None:
            returns.append(numeric)
    return returns


def _extract_equity_curve_returns(rows: Any) -> list[float]:
    if not isinstance(rows, list):
        return []
    values: list[float] = []
    for item in rows:
        value = None
        if isinstance(item, dict):
            for key in ("equity", "total_equity", "portfolio_value", "net_value", "account_value"):
                if key in item:
                    value = item.get(key)
                    break
        else:
            value = item
        numeric = _as_float(value)
        if numeric is not None and numeric > 0:
            values.append(numeric)
    returns: list[float] = []
    previous: float | None = None
    for value in values:
        if previous and previous > 0:
            returns.append(value / previous - 1.0)
        previous = value
    return returns


def _extract_returns_with_source(backtest_result: Any) -> tuple[list[float], str]:
    """Return (returns, key they came from); ``""`` when nothing is extractable."""

    payload = backtest_result if isinstance(backtest_result, dict) else {}
    if "trade_returns" in payload:
        rows = payload.get("trade_returns")
        # An explicitly empty closed-position series is evidence of zero
        # completed positions. It must not turn into another kind of sample.
        return (_extract_return_values(rows) if isinstance(rows, list) else []), "trade_returns"
    for key in ("trade_returns", "returns", "daily_returns", "window_returns"):
        rows = payload.get(key)
        if isinstance(rows, list):
            returns = _extract_return_values(rows)
            if returns:
                return returns, key
    for key in ("trades", "results", "rows"):
        rows = payload.get(key)
        if isinstance(rows, list):
            returns = _extract_return_values(rows)
            if returns:
                return returns, key
    returns = _extract_equity_curve_returns(payload.get("equity_curve"))
    return returns, ("equity_curve" if returns else "")


def _extract_returns(backtest_result: Any) -> list[float]:
    return _extract_returns_with_source(backtest_result)[0]


def _trade_count(payload: dict[str, Any], returns: list[float], source: str) -> tuple[int | None, str]:
    """Number of trades behind the sample, and where that number came from.

    A list of per-trade returns is its own count. Otherwise an explicit count
    field is required; a bare period series (daily returns, equity curve) does
    not say how many trades produced it, so the count stays unknown (``None``)
    and the sample cannot pass.
    """

    if "closed_trade_count" in payload:
        numeric = _as_float(payload.get("closed_trade_count"))
        return (int(numeric) if numeric is not None and numeric >= 0 and numeric.is_integer() else None), "closed_trade_count"
    if source in _TRADE_LEVEL_RETURN_SOURCES:
        return len(returns), source
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for label, container in (("metrics", metrics), ("payload", payload)):
        for key in _TRADE_COUNT_KEYS:
            value = container.get(key)
            if isinstance(value, bool):
                continue
            numeric = _as_float(value)
            if numeric is not None and numeric >= 0:
                return int(numeric), f"{label}.{key}"
    return None, ""


def _extract_metric(backtest_result: dict[str, Any], returns: list[float]) -> float:
    for container in (backtest_result, backtest_result.get("metrics") if isinstance(backtest_result.get("metrics"), dict) else {}):
        if not isinstance(container, dict):
            continue
        for key in ("annual_return", "total_return", "return", "mean_return"):
            value = _as_float(container.get(key))
            if value is not None:
                return value
    return statistics.mean(returns) if returns else 0.0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _max_drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak:
            worst = min(worst, equity / peak - 1.0)
    return worst


def _sharpe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    std = statistics.pstdev(values)
    if std == 0:
        return 0.0
    return statistics.mean(values) / std


def compute_metric_summary(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {"annual_return": None, "sample_compounded_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0, "win_rate": 0.0}
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    return {
        "annual_return": None,
        "sample_compounded_return": compounded - 1.0,
        "sharpe": _sharpe(returns),
        "max_drawdown": _max_drawdown(returns),
        "win_rate": sum(1 for value in returns if value > 0) / len(returns),
    }


def _compounded_return(values: list[float]) -> float:
    """复利累计收益（与 ``compute_metric_summary`` 的 sample_compounded_return 同口径）。"""
    compounded = 1.0
    for value in values:
        compounded *= 1.0 + value
    return compounded - 1.0


def permutation_test_signals(
    returns: list[float],
    *,
    iterations: int = 500,
    seed: int = 42,
    actual_metric: float | None = None,
) -> dict[str, Any]:
    rng = random.Random(seed)
    sample = [float(item) for item in returns]
    # actual 与基线必须同口径。默认（未显式传入 actual_metric）用复利累计收益，
    # 与 _extract_metric 返回的 annual_return / total_return 一致，避免"actual 用
    # 累计收益、基线用单期均值"的量纲不匹配导致 p 值退化为虚假显著性。
    actual = float(actual_metric if actual_metric is not None else _compounded_return(sample))
    if len(sample) < 8:
        return {
            "enabled": True,
            "iterations": int(iterations),
            "actual_metric": actual,
            "random_mean": 0.0,
            "random_p95": 0.0,
            "p_value": None,
            "status": "warning",
            "warnings": ["insufficient_sample"],
        }
    random_metrics: list[float] = []
    abs_values = [abs(value) for value in sample]
    signs = [1 if value >= 0 else -1 for value in sample]
    for _ in range(max(1, int(iterations or 1))):
        shuffled_signs = signs[:]
        rng.shuffle(shuffled_signs)
        paired = [abs_values[index] * shuffled_signs[index] for index in range(len(sample))]
        # 基线与 actual 同口径：对乱序序列做复利聚合，而非取单期均值。
        random_metrics.append(_compounded_return(paired))
    ge_count = sum(1 for value in random_metrics if value >= actual)
    p_value = (ge_count + 1) / (len(random_metrics) + 1)
    random_p95 = _percentile(random_metrics, 0.95)
    status = "passed" if p_value <= 0.05 and actual > random_p95 else "warning"
    return {
        "enabled": True,
        "iterations": int(iterations),
        "actual_metric": round(actual, 8),
        "random_mean": round(statistics.mean(random_metrics), 8),
        "random_p95": round(random_p95, 8),
        "p_value": round(p_value, 8),
        "status": status,
        "warnings": [] if status == "passed" else ["not_clearly_above_random_baseline"],
    }


def bootstrap_metric_confidence_intervals(
    returns: list[float],
    *,
    iterations: int = 500,
    seed: int = 42,
) -> dict[str, Any]:
    rng = random.Random(seed)
    sample = [float(item) for item in returns]
    if len(sample) < 8:
        return {
            "enabled": True,
            "iterations": int(iterations),
            "annual_return_ci": [None, None],
            "sample_compounded_return_ci": [None, None],
            "annualization_status": "unavailable_without_observation_frequency",
            "sharpe_ci": [0.0, 0.0],
            "max_drawdown_ci": [0.0, 0.0],
            "win_rate_ci": [0.0, 0.0],
            "status": "warning",
            "warnings": ["insufficient_sample"],
        }
    metrics: dict[str, list[float]] = {"sample_compounded_return": [], "sharpe": [], "max_drawdown": [], "win_rate": []}
    for _ in range(max(1, int(iterations or 1))):
        resample = [sample[rng.randrange(len(sample))] for _ in sample]
        summary = compute_metric_summary(resample)
        for key in metrics:
            metrics[key].append(float(summary.get(key) or 0.0))
    return {
        "enabled": True,
        "iterations": int(iterations),
        "annual_return_ci": [None, None],
        "sample_compounded_return_ci": [round(_percentile(metrics["sample_compounded_return"], 0.025), 8), round(_percentile(metrics["sample_compounded_return"], 0.975), 8)],
        "annualization_status": "unavailable_without_observation_frequency",
        "sharpe_ci": [round(_percentile(metrics["sharpe"], 0.025), 8), round(_percentile(metrics["sharpe"], 0.975), 8)],
        "max_drawdown_ci": [round(_percentile(metrics["max_drawdown"], 0.025), 8), round(_percentile(metrics["max_drawdown"], 0.975), 8)],
        "win_rate_ci": [round(_percentile(metrics["win_rate"], 0.025), 8), round(_percentile(metrics["win_rate"], 0.975), 8)],
        "status": "passed",
        "warnings": [],
    }


def _load_backtest_result_with_source(
    backtest_result: Any = None,
    backtest_result_path: str | Path = DEFAULT_BACKTEST_RESULT_PATH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load exactly the requested result; never substitute another file."""

    if isinstance(backtest_result, dict):
        payload = json.loads(json.dumps(backtest_result, ensure_ascii=False, default=str))
        return payload, {"kind": "in_memory", "path": "", "exists": True, "generated_at": str(payload.get("generated_at") or "")}
    path = _project_path(backtest_result_path)
    payload = read_json(path, {}) if path.exists() else {}
    if not isinstance(payload, dict):
        payload = {}
    return payload, {
        "kind": "file",
        "path": str(path),
        "exists": path.exists(),
        "generated_at": str(payload.get("generated_at") or ""),
        "fallback_used": False,
    }


def _load_backtest_result(backtest_result: Any = None, backtest_result_path: str | Path = DEFAULT_BACKTEST_RESULT_PATH) -> dict[str, Any]:
    return _load_backtest_result_with_source(backtest_result, backtest_result_path)[0]


def _nonformal_input_result(
    payload: dict[str, Any], source: dict[str, Any], *, data_mode: str,
    symbol: str, target_date: str, network: bool, strict: bool, seed: int,
) -> dict[str, Any]:
    """Keep source evidence visible without computing statistics on test data."""
    sample_context = research_sample_context(payload)
    return {
        "ok": False,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest_statistical_validation",
        **sample_context,
        "status": "missing",
        "input_data_mode": data_mode,
        "input_rejection_reason": "nonformal_backtest_input",
        "validation_run_status": "not_run",
        "symbol": str(symbol or payload.get("symbol") or ""),
        "target_date": str(target_date or payload.get("target_date") or payload.get("date") or ""),
        "network": bool(network),
        "strict": bool(strict),
        "sample_size": 0,
        "trade_count": None,
        "trade_count_source": "",
        "negative_return_count": None,
        "min_trades_for_validation": MIN_TRADES_FOR_VALIDATION,
        "insufficient_sample_reasons": [],
        "backtest_result_source": {**source, "data_mode": data_mode, "return_source": ""},
        "reported_metric": None,
        "actual_metric_definition": "not_computed_nonformal_input",
        "return_evidence": {},
        "metric_definitions": {},
        "iterations": 0,
        "seed": int(seed),
        "metric_summary": {key: None for key in (
            "annual_return", "sample_compounded_return", "sharpe", "max_drawdown", "win_rate"
        )},
        "permutation_test": {"enabled": False, "status": "not_run", "p_value": None},
        "bootstrap_ci": {"enabled": False, "status": "not_run"},
        "warnings": [sample_context["sample_limitation"], "nonformal_backtest_input:" + data_mode,
                     NONFORMAL_INPUT_MESSAGE],
        "interpretation": NONFORMAL_INPUT_MESSAGE,
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }


def run_backtest_statistical_validation(
    backtest_result: Any = None,
    *,
    symbol: str = "",
    target_date: str = "",
    backtest_result_path: str | Path = DEFAULT_BACKTEST_RESULT_PATH,
    iterations: int = 500,
    seed: int = 42,
    network: bool = False,
    strict: bool = True,
    write: bool = False,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    payload, source = _load_backtest_result_with_source(backtest_result, backtest_result_path)
    data_mode = nonformal_mode(payload)
    if data_mode != "formal":
        # Reject before extracting returns, including strict=False night runs.
        # A test artifact must not become a historical-strategy conclusion.
        result = _nonformal_input_result(
            payload, source, data_mode=data_mode, symbol=symbol, target_date=target_date,
            network=network, strict=strict, seed=seed,
        )
        if write:
            write_statistical_validation_outputs(result, output_path=output_path, report_path=report_path)
        return result
    sample_context = research_sample_context(payload)
    returns, return_source = _extract_returns_with_source(payload)
    source["return_source"] = return_source
    # Kept for reference only. The permutation test compares the compounded
    # return of THIS sample against the compounded return of its reshuffles; a
    # reported annual_return from the file is a different quantity (annualised,
    # possibly from a different run) and must never be the "actual" side.
    reported_metric = _extract_metric(payload, returns) if payload else None
    trade_count, trade_count_source = _trade_count(payload, returns, return_source)
    negative_count = sum(1 for value in returns if value < 0)
    warnings: list[str] = []
    if not payload:
        warnings.append("backtest_result_missing")
    if not returns:
        warnings.append("backtest_returns_missing")
    if len(returns) < MIN_RETURNS_FOR_VALIDATION:
        warnings.append("insufficient_sample")
    insufficient_reasons: list[str] = []
    selected_rows = payload.get(return_source)
    if return_source == "trade_returns" and not isinstance(selected_rows, list):
        insufficient_reasons.append("invalid_authoritative_trade_returns")
    elif isinstance(selected_rows, list):
        expected_count = len(selected_rows) - 1 if return_source == "equity_curve" else len(selected_rows)
        if len(returns) != max(0, expected_count):
            insufficient_reasons.append("invalid_return_observations_excluded")
    if "closed_trade_count" in payload:
        if trade_count is None:
            insufficient_reasons.append("invalid_closed_trade_count")
        elif return_source != "trade_returns" or trade_count != len(returns):
            insufficient_reasons.append("closed_trade_count_return_count_mismatch")
        closed_rows = payload.get("closed_trades")
        if isinstance(closed_rows, list) and trade_count != len(closed_rows):
            insufficient_reasons.append("closed_trade_records_count_mismatch")
    return_evidence = payload.get("return_evidence") if isinstance(payload.get("return_evidence"), dict) else {}
    if return_evidence.get("complete_fill_history") is False:
        insufficient_reasons.append("incomplete_fill_history")
    if return_evidence.get("cost_coverage_complete") is False:
        insufficient_reasons.append("incomplete_historical_cost_coverage")
    if insufficient_reasons:
        warnings.extend(str(item) for item in return_evidence.get("warnings", []))
    if returns:
        if len(returns) < MIN_RETURNS_FOR_VALIDATION:
            insufficient_reasons.append(f"return_count_below_{MIN_RETURNS_FOR_VALIDATION}")
        if negative_count == 0:
            # Sign-shuffling cannot produce a baseline from a sample with no
            # losses; "beating" it says nothing.
            insufficient_reasons.append("no_negative_return_in_sample")
        if trade_count is None:
            insufficient_reasons.append("trade_count_unknown")
        elif trade_count < MIN_TRADES_FOR_VALIDATION:
            insufficient_reasons.append(f"trade_count_below_{MIN_TRADES_FOR_VALIDATION}")
    permutation = permutation_test_signals(returns, iterations=iterations, seed=seed, actual_metric=_compounded_return(returns))
    bootstrap = bootstrap_metric_confidence_intervals(returns, iterations=iterations, seed=seed)
    warnings.extend(permutation.get("warnings") or [])
    warnings.extend(bootstrap.get("warnings") or [])
    if insufficient_reasons and "insufficient_sample" not in warnings:
        warnings.append("insufficient_sample")
    metric_summary = compute_metric_summary(returns)
    if not returns:
        status = "missing"
    elif insufficient_reasons:
        status = "insufficient_sample"
    elif permutation.get("status") == "passed" and bootstrap.get("status") == "passed":
        status = "passed"
    else:
        status = "warning"
    result = {
        "ok": status not in {"failed", "missing"},
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest_statistical_validation",
        **sample_context,
        "status": status,
        "symbol": str(symbol or payload.get("symbol") or ""),
        "target_date": str(target_date or payload.get("target_date") or payload.get("date") or ""),
        "network": bool(network),
        "strict": bool(strict),
        "sample_size": len(returns),
        "trade_count": trade_count,
        "trade_count_source": trade_count_source,
        "negative_return_count": negative_count,
        "min_trades_for_validation": MIN_TRADES_FOR_VALIDATION,
        "insufficient_sample_reasons": insufficient_reasons,
        "backtest_result_source": source,
        "reported_metric": reported_metric,
        "actual_metric_definition": "compounded_return_of_the_validated_sample",
        "return_evidence": return_evidence,
        "metric_definitions": {
            "sample_compounded_return": "compound of the sample observations, not annualized or portfolio-equivalent",
            "sharpe": "mean / standard deviation per sample observation, not annualized",
            "annual_return": "unavailable: trade observations have no fixed daily frequency",
            "resampling_assumption": "observations treated as exchangeable; overlap and serial dependence not controlled",
        },
        "iterations": int(iterations),
        "seed": int(seed),
        "metric_summary": metric_summary,
        "permutation_test": permutation,
        "bootstrap_ci": bootstrap,
        "warnings": [sample_context["sample_limitation"], *[short_error_text(item) for item in dict.fromkeys(warnings) if item]],
        "interpretation": _interpret_result(status, permutation, len(returns)),
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_statistical_validation_outputs(result, output_path=output_path, report_path=report_path)
    return result


def _interpret_result(status: str, permutation: dict[str, Any], sample_size: int) -> str:
    if status == "missing":
        return "回测结果里提不出收益序列，统计检验没有运行；不会改用旧文件代替。"
    if sample_size < 8 or status == "insufficient_sample":
        return "当前统计样本不足，不能据此判断策略稳定性。"
    if permutation.get("status") != "passed":
        return "当前样本中未显示出明显优于随机基线的证据。"
    if status == "passed":
        return "在当前历史样本中，统计表现优于本地随机基线；该结论不代表未来收益。"
    return "统计验证存在不确定性，仅可作为学习诊断。"


def summarize_statistical_validation(payload: dict[str, Any]) -> dict[str, Any]:
    permutation = payload.get("permutation_test") if isinstance(payload.get("permutation_test"), dict) else {}
    bootstrap = payload.get("bootstrap_ci") if isinstance(payload.get("bootstrap_ci"), dict) else {}
    return {
        **research_sample_context(payload),
        "status": payload.get("status") or "missing",
        "input_data_mode": payload.get("input_data_mode"),
        "input_rejection_reason": payload.get("input_rejection_reason"),
        "validation_run_status": payload.get("validation_run_status"),
        "sample_size": int(payload.get("sample_size") or 0),
        "trade_count": payload.get("trade_count"),
        "negative_return_count": payload.get("negative_return_count"),
        "insufficient_sample_reasons": list(payload.get("insufficient_sample_reasons") or []),
        "backtest_result_source": payload.get("backtest_result_source") if isinstance(payload.get("backtest_result_source"), dict) else {},
        "p_value": permutation.get("p_value"),
        "permutation_status": permutation.get("status"),
        "bootstrap_status": bootstrap.get("status"),
        "bootstrap_ci_summary": {
            "sample_compounded_return_ci": bootstrap.get("sample_compounded_return_ci") or [],
            "annual_return_ci": bootstrap.get("annual_return_ci") or [],
            "sharpe_ci": bootstrap.get("sharpe_ci") or [],
            "max_drawdown_ci": bootstrap.get("max_drawdown_ci") or [],
            "win_rate_ci": bootstrap.get("win_rate_ci") or [],
        },
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
        "interpretation": payload.get("interpretation") or "",
        "generated_at": payload.get("generated_at"),
        "disclaimer": DISCLAIMER,
    }


def build_statistical_validation_report(payload: dict[str, Any]) -> str:
    summary = summarize_statistical_validation(payload)
    return "\n".join(
        [
            "# QuantDesk Backtest Statistical Validation",
            "",
            DISCLAIMER,
            "",
            f"- status: `{payload.get('status') or '-'}`",
            f"- sample role: `{summary['sample_role']}`; out-of-sample: `{summary['out_of_sample_validation']}`",
            f"- sample window: `{summary['sample_window']}`",
            summary["sample_limitation"],
            f"- sample_size: `{payload.get('sample_size') or 0}`",
            f"- trade_count: `{payload.get('trade_count')}` (需 >= {MIN_TRADES_FOR_VALIDATION})",
            f"- negative_return_count: `{payload.get('negative_return_count')}`",
            f"- insufficient_sample_reasons: `{payload.get('insufficient_sample_reasons') or []}`",
            f"- backtest_result_source: `{(payload.get('backtest_result_source') or {}).get('path') or '-'}`"
            f" generated_at=`{(payload.get('backtest_result_source') or {}).get('generated_at') or '-'}`",
            f"- iterations: `{payload.get('iterations') or 0}`",
            f"- seed: `{payload.get('seed') or 0}`",
            f"- p_value: `{summary.get('p_value')}`",
            f"- sample_compounded_return_ci（非年化、非组合收益）: `{summary.get('bootstrap_ci_summary', {}).get('sample_compounded_return_ci')}`",
            f"- sharpe_ci: `{summary.get('bootstrap_ci_summary', {}).get('sharpe_ci')}`",
            f"- interpretation: {summary.get('interpretation') or '-'}",
            "",
            "## Warnings",
            *(f"- {item}" for item in (payload.get("warnings") or [])),
            "",
            "本报告只做统计诊断，不优化参数、不搜索组合、不构成投资建议。",
        ]
    )


def write_statistical_validation_outputs(
    payload: dict[str, Any],
    *,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, str]:
    json_path = _project_path(output_path)
    md_path = _project_path(report_path)
    write_json(json_path, payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(build_statistical_validation_report(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(md_path)}
