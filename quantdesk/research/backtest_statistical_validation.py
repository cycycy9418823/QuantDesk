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
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_JSON_PATH = RESULTS_DIR / "backtest_statistical_validation_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "backtest_statistical_validation_report_latest.md"
DEFAULT_BACKTEST_RESULT_PATH = Path("results") / "backtest" / "backtest_latest.json"
BACKTEST_RESULT_FALLBACK_PATHS = (
    DEFAULT_BACKTEST_RESULT_PATH,
    Path("results") / "backtest" / "latest.json",
    Path("results") / "backtest_latest.json",
)
DISCLAIMER = "统计检验仅基于历史样本，不代表未来收益。"


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


def _extract_returns(backtest_result: Any) -> list[float]:
    payload = backtest_result if isinstance(backtest_result, dict) else {}
    for key in ("trade_returns", "returns", "daily_returns", "window_returns"):
        rows = payload.get(key)
        if isinstance(rows, list):
            returns = _extract_return_values(rows)
            if returns:
                return returns
    for key in ("trades", "results", "rows"):
        rows = payload.get(key)
        if isinstance(rows, list):
            returns = _extract_return_values(rows)
            if returns:
                return returns
    return _extract_equity_curve_returns(payload.get("equity_curve"))


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


def compute_metric_summary(returns: list[float]) -> dict[str, float]:
    if not returns:
        return {"annual_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0, "win_rate": 0.0}
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    annual_return = compounded - 1.0
    return {
        "annual_return": annual_return,
        "sharpe": _sharpe(returns),
        "max_drawdown": _max_drawdown(returns),
        "win_rate": sum(1 for value in returns if value > 0) / len(returns),
    }


def _compounded_return(values: list[float]) -> float:
    """复利累计收益（与 ``compute_metric_summary`` 的 annual_return 同口径）。"""
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
            "annual_return_ci": [0.0, 0.0],
            "sharpe_ci": [0.0, 0.0],
            "max_drawdown_ci": [0.0, 0.0],
            "win_rate_ci": [0.0, 0.0],
            "status": "warning",
            "warnings": ["insufficient_sample"],
        }
    metrics: dict[str, list[float]] = {"annual_return": [], "sharpe": [], "max_drawdown": [], "win_rate": []}
    for _ in range(max(1, int(iterations or 1))):
        resample = [sample[rng.randrange(len(sample))] for _ in sample]
        summary = compute_metric_summary(resample)
        for key in metrics:
            metrics[key].append(float(summary.get(key) or 0.0))
    return {
        "enabled": True,
        "iterations": int(iterations),
        "annual_return_ci": [round(_percentile(metrics["annual_return"], 0.025), 8), round(_percentile(metrics["annual_return"], 0.975), 8)],
        "sharpe_ci": [round(_percentile(metrics["sharpe"], 0.025), 8), round(_percentile(metrics["sharpe"], 0.975), 8)],
        "max_drawdown_ci": [round(_percentile(metrics["max_drawdown"], 0.025), 8), round(_percentile(metrics["max_drawdown"], 0.975), 8)],
        "win_rate_ci": [round(_percentile(metrics["win_rate"], 0.025), 8), round(_percentile(metrics["win_rate"], 0.975), 8)],
        "status": "passed",
        "warnings": [],
    }


def _candidate_backtest_paths(backtest_result_path: str | Path) -> list[Path]:
    primary = _project_path(backtest_result_path)
    candidates = [primary]
    default_like = {
        _project_path(DEFAULT_BACKTEST_RESULT_PATH),
        _project_path(Path("results") / "backtest_latest.json"),
    }
    if primary in default_like:
        for path in BACKTEST_RESULT_FALLBACK_PATHS:
            candidate = _project_path(path)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _load_backtest_result(backtest_result: Any = None, backtest_result_path: str | Path = DEFAULT_BACKTEST_RESULT_PATH) -> dict[str, Any]:
    if isinstance(backtest_result, dict):
        return json.loads(json.dumps(backtest_result, ensure_ascii=False, default=str))
    first_payload: dict[str, Any] = {}
    for path in _candidate_backtest_paths(backtest_result_path):
        payload = read_json(path, {})
        if not isinstance(payload, dict):
            continue
        if not first_payload:
            first_payload = payload
        if _extract_returns(payload):
            return payload
    return first_payload


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
    payload = _load_backtest_result(backtest_result, backtest_result_path)
    returns = _extract_returns(payload)
    actual_metric = _extract_metric(payload, returns)
    warnings: list[str] = []
    if not payload:
        warnings.append("backtest_result_missing")
    if len(returns) < 8:
        warnings.append("insufficient_sample")
    permutation = permutation_test_signals(returns, iterations=iterations, seed=seed, actual_metric=actual_metric)
    bootstrap = bootstrap_metric_confidence_intervals(returns, iterations=iterations, seed=seed)
    warnings.extend(permutation.get("warnings") or [])
    warnings.extend(bootstrap.get("warnings") or [])
    metric_summary = compute_metric_summary(returns)
    if not returns:
        status = "warning"
    elif "insufficient_sample" in warnings:
        status = "warning"
    elif permutation.get("status") == "passed" and bootstrap.get("status") == "passed":
        status = "passed"
    else:
        status = "warning"
    result = {
        "ok": status != "failed",
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest_statistical_validation",
        "status": status,
        "symbol": str(symbol or payload.get("symbol") or ""),
        "target_date": str(target_date or payload.get("target_date") or payload.get("date") or ""),
        "network": bool(network),
        "strict": bool(strict),
        "sample_size": len(returns),
        "iterations": int(iterations),
        "seed": int(seed),
        "metric_summary": metric_summary,
        "permutation_test": permutation,
        "bootstrap_ci": bootstrap,
        "warnings": [short_error_text(item) for item in dict.fromkeys(warnings) if item],
        "interpretation": _interpret_result(status, permutation, len(returns)),
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_statistical_validation_outputs(result, output_path=output_path, report_path=report_path)
    return result


def _interpret_result(status: str, permutation: dict[str, Any], sample_size: int) -> str:
    if sample_size < 8:
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
        "status": payload.get("status") or "missing",
        "sample_size": int(payload.get("sample_size") or 0),
        "p_value": permutation.get("p_value"),
        "permutation_status": permutation.get("status"),
        "bootstrap_status": bootstrap.get("status"),
        "bootstrap_ci_summary": {
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
            f"- sample_size: `{payload.get('sample_size') or 0}`",
            f"- iterations: `{payload.get('iterations') or 0}`",
            f"- seed: `{payload.get('seed') or 0}`",
            f"- p_value: `{summary.get('p_value')}`",
            f"- annual_return_ci: `{summary.get('bootstrap_ci_summary', {}).get('annual_return_ci')}`",
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
