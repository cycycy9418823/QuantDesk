"""Parameter sensitivity and robustness audit for QuantDesk.

This module is diagnostic only. It explores a small, bounded set of parameter
perturbations and checks whether existing backtest-like results are fragile.
It never changes strategy defaults, never writes strategy config, and never
selects "best" parameters as trading advice.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any, Callable

from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_JSON_PATH = RESULTS_DIR / "parameter_sensitivity_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "parameter_sensitivity_report_latest.md"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "parameter_sensitivity.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "parameter_sensitivity.example.json"
DEFAULT_BACKTEST_RESULT_PATH = RESULTS_DIR / "backtest" / "backtest_latest.json"
# Canonical backtest output lives under results/backtest/. Older callers passed
# the flat results/backtest_latest.json path (which no longer exists), so the
# loader falls back across these known locations before declaring the result
# missing.
BACKTEST_RESULT_FALLBACK_PATHS = (
    DEFAULT_BACKTEST_RESULT_PATH,
    RESULTS_DIR / "backtest" / "latest.json",
    RESULTS_DIR / "backtest_latest.json",
)
DEFAULT_STAT_VALIDATION_PATH = RESULTS_DIR / "backtest_statistical_validation_latest.json"
DISCLAIMER = "参数敏感性审计仅用于评估历史样本中的稳定性，不构成投资建议。"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "3.2.9",
    "max_combinations": 50,
    "default_perturbation_pct": 0.2,
    "parameters": {
        "ma_short": [5, 7, 10],
        "volume_ratio_threshold": [1.2, 1.5, 1.8],
        "score_threshold": [60, 65, 70],
    },
}


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def _candidate_backtest_paths(backtest_result_path: str | Path) -> list[Path]:
    """Return the caller path plus canonical fallbacks.

    Older callers default to the flat ``results/backtest_latest.json`` path,
    which no longer exists; the canonical output lives under
    ``results/backtest/``. When the caller passes one of those default-like
    paths, walk the known fallback locations before giving up.
    """

    primary = _project_path(backtest_result_path)
    candidates = [primary]
    default_like = {
        _project_path(DEFAULT_BACKTEST_RESULT_PATH),
        _project_path(RESULTS_DIR / "backtest_latest.json"),
    }
    if primary in default_like:
        for path in BACKTEST_RESULT_FALLBACK_PATHS:
            candidate = _project_path(path)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _load_baseline_backtest(backtest_result_path: str | Path) -> dict[str, Any]:
    first_payload: dict[str, Any] = {}
    for path in _candidate_backtest_paths(backtest_result_path):
        payload = read_json(path, {})
        if not isinstance(payload, dict):
            continue
        if not first_payload and payload:
            first_payload = payload
        # A backtest result is usable when it carries metrics or trades.
        if payload.get("metrics") or payload.get("trades") or payload.get("equity_curve"):
            return payload
    return first_payload


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


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
        equity *= 1.0 + float(value)
        peak = max(peak, equity)
        if peak:
            worst = min(worst, equity / peak - 1.0)
    return worst


def _extract_returns(payload: dict[str, Any]) -> list[float]:
    candidates: list[Any] = []
    for key in ("trade_returns", "returns", "daily_returns", "window_returns"):
        if isinstance(payload.get(key), list):
            candidates = payload.get(key) or []
            break
    if not candidates:
        for key in ("trades", "results", "rows", "equity_curve"):
            rows = payload.get(key)
            if isinstance(rows, list):
                candidates = rows
                break
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
        if value is not None:
            returns.append(_safe_float(value))
    return returns


def _metric_from_returns(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "win_rate": 0.0,
            "trade_count": 0,
            "window_count": 0,
        }
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    mean = statistics.mean(returns)
    std = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    return {
        "annual_return": compounded - 1.0,
        "max_drawdown": _max_drawdown(returns),
        "sharpe": mean / std if std else 0.0,
        "win_rate": sum(1 for value in returns if value > 0) / len(returns),
        "trade_count": len(returns),
        "window_count": len(returns),
    }


def _load_config(config_path: str | Path | None = None, config_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(config_payload, dict) and config_payload:
        return {**DEFAULT_CONFIG, **config_payload, "parameters": config_payload.get("parameters") or DEFAULT_CONFIG["parameters"]}
    if config_path:
        payload = read_json(_project_path(config_path), {})
        if isinstance(payload, dict) and payload:
            return {**DEFAULT_CONFIG, **payload, "parameters": payload.get("parameters") or DEFAULT_CONFIG["parameters"]}
    payload = read_json(DEFAULT_CONFIG_PATH, {})
    if isinstance(payload, dict) and payload:
        return {**DEFAULT_CONFIG, **payload, "parameters": payload.get("parameters") or DEFAULT_CONFIG["parameters"]}
    return json.loads(json.dumps(DEFAULT_CONFIG))


def build_parameter_grid(
    parameters: dict[str, Any] | None = None,
    *,
    max_combinations: int = 50,
    random_seed: int = 42,
) -> dict[str, Any]:
    params = parameters if isinstance(parameters, dict) and parameters else DEFAULT_CONFIG["parameters"]
    clean: dict[str, list[Any]] = {}
    warnings: list[str] = []
    for key, values in params.items():
        if isinstance(values, list):
            options = values
        elif isinstance(values, tuple):
            options = list(values)
        else:
            options = [values]
        options = [item for item in options if item is not None]
        if options:
            clean[str(key)] = options
    keys = sorted(clean)
    combos = [dict(zip(keys, values)) for values in itertools.product(*(clean[key] for key in keys))]
    original_count = len(combos)
    limit = max(1, int(max_combinations or 50))
    if original_count > limit:
        rng = random.Random(random_seed)
        indexed = list(enumerate(combos))
        rng.shuffle(indexed)
        combos = [combo for _index, combo in sorted(indexed[:limit], key=lambda item: item[0])]
        warnings.append(f"parameter combination count {original_count} exceeds max_combinations={limit}; truncated for bounded audit.")
    return {
        "parameters": clean,
        "parameter_count": len(keys),
        "combination_count": len(combos),
        "original_combination_count": original_count,
        "combinations": combos,
        "warnings": warnings,
    }


def build_parameter_perturbations(
    base_parameters: dict[str, Any],
    *,
    perturbation_pct: float = 0.2,
) -> dict[str, list[Any]]:
    pct = max(0.0, float(perturbation_pct or 0.2))
    out: dict[str, list[Any]] = {}
    for key, value in (base_parameters or {}).items():
        number = _safe_float(value, 0.0)
        if number == 0:
            out[key] = [value]
            continue
        lo = number * (1 - pct)
        hi = number * (1 + pct)
        if isinstance(value, int):
            out[key] = sorted(set([max(1, int(round(lo))), int(value), max(1, int(round(hi)))]))
        else:
            out[key] = [round(lo, 6), number, round(hi, 6)]
    return out


def evaluate_parameter_result(result: dict[str, Any] | None, *, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else payload
    returns = _extract_returns(payload)
    derived = _metric_from_returns(returns)
    out = {
        "parameters": parameters or payload.get("parameters") or {},
        "annual_return": _safe_float(metrics.get("annual_return", metrics.get("total_return", derived["annual_return"]))),
        "max_drawdown": _safe_float(metrics.get("max_drawdown", derived["max_drawdown"])),
        "sharpe": _safe_float(metrics.get("sharpe", metrics.get("sharpe_ratio", derived["sharpe"]))),
        "win_rate": _safe_float(metrics.get("win_rate", derived["win_rate"])),
        "trade_count": int(_safe_float(metrics.get("trade_count", metrics.get("trades", derived["trade_count"])))),
        "window_count": int(_safe_float(metrics.get("window_count", metrics.get("sample_size", derived["window_count"])))),
        "source": payload.get("source") or "existing_result",
    }
    return out


def _proxy_result_from_baseline(baseline: dict[str, Any], combo: dict[str, Any], center: dict[str, float]) -> dict[str, Any]:
    base = evaluate_parameter_result(baseline)
    distances: list[float] = []
    for key, value in combo.items():
        current = _safe_float(value)
        mid = center.get(key, current) or current or 1.0
        distances.append(abs(current - mid) / max(abs(mid), 1.0))
    distance = statistics.mean(distances) if distances else 0.0
    penalty = min(0.35, distance * 0.25)
    return {
        "parameters": combo,
        "metrics": {
            "annual_return": base["annual_return"] * (1 - penalty),
            "max_drawdown": base["max_drawdown"] * (1 + penalty),
            "sharpe": base["sharpe"] * (1 - penalty),
            "win_rate": base["win_rate"],
            "trade_count": base["trade_count"],
            "window_count": base["window_count"],
        },
        "source": "diagnostic_proxy_from_existing_backtest",
    }


def _metric_stats(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [_safe_float(row.get(key)) for row in rows]
    if not values:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "robustness_status": "warning"}
    mean = statistics.mean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    abs_mean = abs(mean)
    unstable = std > max(0.05, abs_mean * 0.75)
    if key == "max_drawdown":
        unstable = std > max(0.03, abs_mean * 0.5)
    return {
        "mean": round(mean, 8),
        "std": round(std, 8),
        "min": round(min(values), 8),
        "max": round(max(values), 8),
        "robustness_status": "warning" if unstable else "passed",
    }


def detect_overfit_parameter_hotspots(
    evaluated_results: list[dict[str, Any]],
    *,
    primary_metric: str = "annual_return",
    min_trade_count: int = 8,
    min_window_count: int = 8,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if not evaluated_results:
        return [{"type": "insufficient_sample", "message": "当前样本不足，无法可靠判断参数鲁棒性。"}]
    metric_values = [_safe_float(row.get(primary_metric)) for row in evaluated_results]
    trade_counts = [int(row.get("trade_count") or 0) for row in evaluated_results]
    window_counts = [int(row.get("window_count") or 0) for row in evaluated_results]
    if max(trade_counts or [0]) < min_trade_count:
        warnings.append({"type": "insufficient_trades", "message": "当前样本交易次数不足，无法可靠判断参数鲁棒性。"})
    if max(window_counts or [0]) < min_window_count:
        warnings.append({"type": "insufficient_windows", "message": "当前样本窗口不足，无法可靠判断参数鲁棒性。"})
    if len(metric_values) >= 10:
        top_n = max(1, math.ceil(len(metric_values) * 0.05))
        top_values = sorted(metric_values, reverse=True)[:top_n]
        median = statistics.median(metric_values)
        std = statistics.pstdev(metric_values) if len(metric_values) > 1 else 0.0
        if top_values and min(top_values) > median + max(std, 0.03):
            warnings.append({"type": "narrow_peak", "message": "仅少数组合表现较好，可能存在参数挑选偏差。"})
    if len(metric_values) >= 3:
        ordered = sorted(evaluated_results, key=lambda row: json.dumps(row.get("parameters") or {}, sort_keys=True))
        jumps = [abs(_safe_float(ordered[i].get(primary_metric)) - _safe_float(ordered[i - 1].get(primary_metric))) for i in range(1, len(ordered))]
        if jumps and max(jumps) > max(0.12, (statistics.pstdev(metric_values) if len(metric_values) > 1 else 0.0) * 2.5):
            warnings.append({"type": "parameter_cliff", "message": "相邻参数组合结果跳变较大，存在 parameter cliff 风险。"})
    drawdowns = [_safe_float(row.get("max_drawdown")) for row in evaluated_results]
    if len(drawdowns) > 1 and statistics.pstdev(drawdowns) > max(0.04, abs(statistics.mean(drawdowns)) * 0.5):
        warnings.append({"type": "drawdown_sensitive", "message": "最大回撤对参数变化较敏感。"})
    if metric_values and max(metric_values) <= 0:
        warnings.append({"type": "no_robust_edge", "message": "当前样本所有组合表现都偏弱，不能据此推断策略有效。"})
    return warnings


def compute_robustness_score(metrics: dict[str, dict[str, Any]], warnings: list[dict[str, Any]] | None = None) -> float:
    score = 1.0
    for key, stats in metrics.items():
        mean = abs(_safe_float(stats.get("mean")))
        std = _safe_float(stats.get("std"))
        if key == "annual_return":
            score -= min(0.35, std / max(mean, 0.05) * 0.2)
        elif key == "sharpe":
            score -= min(0.2, std / max(mean, 0.2) * 0.1)
        elif key == "max_drawdown":
            score -= min(0.25, std / max(mean, 0.05) * 0.15)
    for warning in warnings or []:
        warning_type = str(warning.get("type") or "")
        score -= {
            "narrow_peak": 0.18,
            "parameter_cliff": 0.16,
            "insufficient_trades": 0.2,
            "insufficient_windows": 0.2,
            "drawdown_sensitive": 0.12,
            "no_robust_edge": 0.12,
            "statistical_validation_not_passed": 0.1,
        }.get(warning_type, 0.05)
    return round(max(0.0, min(1.0, score)), 6)


def summarize_parameter_sensitivity(payload: dict[str, Any]) -> dict[str, Any]:
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    overfit = payload.get("overfit_warnings") if isinstance(payload.get("overfit_warnings"), list) else []
    return {
        "status": payload.get("status") or "missing",
        "robustness_score": payload.get("robustness_score"),
        "combination_count": int(payload.get("combination_count") or 0),
        "warning_count": len(warnings) + len(overfit),
        "top_overfit_warnings": [
            item.get("message") if isinstance(item, dict) else str(item)
            for item in overfit[:5]
        ],
        "insufficient_sample": any(
            isinstance(item, dict) and str(item.get("type")) in {"insufficient_trades", "insufficient_windows", "insufficient_sample"}
            for item in overfit
        ),
        "disclaimer": DISCLAIMER,
    }


def run_parameter_sensitivity_audit(
    *,
    symbol: str = "",
    symbols: Any = None,
    target_date: str = "",
    network: bool = False,
    config_path: str | Path | None = None,
    config_payload: dict[str, Any] | None = None,
    max_combinations: int | None = None,
    seed: int = 42,
    strict: bool = True,
    backtest_result: dict[str, Any] | None = None,
    backtest_result_path: str | Path = DEFAULT_BACKTEST_RESULT_PATH,
    parameter_results: list[dict[str, Any]] | None = None,
    result_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    write: bool = False,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    config = _load_config(config_path, config_payload)
    limit = int(max_combinations if max_combinations is not None else config.get("max_combinations") or 50)
    grid = build_parameter_grid(config.get("parameters"), max_combinations=limit, random_seed=seed)
    warnings = [*grid.get("warnings", [])]
    selected_symbol = normalize_symbol(symbol or (str(symbols).split(",")[0] if symbols else ""))
    baseline = backtest_result if isinstance(backtest_result, dict) else _load_baseline_backtest(backtest_result_path)
    baseline = baseline if isinstance(baseline, dict) else {}
    if not baseline and not parameter_results and result_provider is None:
        warnings.append("backtest_result_missing")

    center: dict[str, float] = {}
    for key, values in (grid.get("parameters") or {}).items():
        nums = [_safe_float(item) for item in values]
        center[key] = statistics.median(nums) if nums else 0.0

    evaluated: list[dict[str, Any]] = []
    source_mode = "provided_parameter_results" if parameter_results else ("result_provider" if result_provider else "diagnostic_proxy_from_existing_backtest")
    if parameter_results:
        for item in parameter_results[: grid["combination_count"]]:
            params = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            evaluated.append(evaluate_parameter_result(item, parameters=params))
    else:
        for combo in grid["combinations"]:
            if result_provider is not None:
                result = result_provider(combo)
            else:
                result = _proxy_result_from_baseline(baseline, combo, center) if baseline else {"parameters": combo, "metrics": {}, "source": "missing_backtest_result"}
            evaluated.append(evaluate_parameter_result(result, parameters=combo))
        if baseline and result_provider is None:
            warnings.append("using diagnostic proxy from existing backtest result; this is not parameter optimization.")

    metrics = {
        "annual_return": _metric_stats(evaluated, "annual_return"),
        "max_drawdown": _metric_stats(evaluated, "max_drawdown"),
        "sharpe": _metric_stats(evaluated, "sharpe"),
    }
    overfit_warnings = detect_overfit_parameter_hotspots(evaluated)
    stat_validation = read_json(DEFAULT_STAT_VALIDATION_PATH, {})
    if isinstance(stat_validation, dict) and stat_validation and str(stat_validation.get("status") or "") != "passed":
        overfit_warnings.append(
            {
                "type": "statistical_validation_not_passed",
                "message": "统计验证未通过时，参数稳定性解释有限。",
            }
        )
    robustness_score = compute_robustness_score(metrics, overfit_warnings)
    if any(item.get("type", "").startswith("insufficient") for item in overfit_warnings if isinstance(item, dict)):
        status = "warning"
    elif robustness_score < 0.45:
        status = "failed" if strict else "warning"
    elif robustness_score < 0.7 or overfit_warnings or warnings:
        status = "warning"
    else:
        status = "passed"
    payload = {
        "ok": status != "failed",
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "parameter_sensitivity_audit",
        "status": status,
        "target_date": str(target_date or ""),
        "symbol": selected_symbol,
        "symbols": [selected_symbol] if selected_symbol else [],
        "network": bool(network),
        "full_scan": False,
        "strict": bool(strict),
        "parameter_count": int(grid.get("parameter_count") or 0),
        "combination_count": int(grid.get("combination_count") or 0),
        "original_combination_count": int(grid.get("original_combination_count") or 0),
        "max_combinations": limit,
        "random_seed": int(seed),
        "source_mode": source_mode,
        "parameters": grid.get("parameters") or {},
        "metrics": metrics,
        "evaluated_results": evaluated,
        "robustness_score": robustness_score,
        "overfit_warnings": overfit_warnings,
        "warnings": [short_error_text(item) for item in warnings if item],
        "summary": {},
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    payload["summary"] = summarize_parameter_sensitivity(payload)
    if write:
        write_parameter_sensitivity_outputs(payload, output_path=output_path, report_path=report_path)
    return payload


def build_parameter_sensitivity_report(payload: dict[str, Any]) -> str:
    summary = summarize_parameter_sensitivity(payload)
    overfit = payload.get("overfit_warnings") if isinstance(payload.get("overfit_warnings"), list) else []
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    lines = [
        "# QuantDesk 参数敏感性审计",
        "",
        DISCLAIMER,
        "",
        "本报告不是参数优化器，不寻找最高收益，不修改策略参数，也不会给出推荐参数。",
        "",
        f"- 状态：`{payload.get('status') or '-'}`",
        f"- 股票代码：`{payload.get('symbol') or '-'}`",
        f"- 目标日期：`{payload.get('target_date') or '-'}`",
        f"- 参数数量：`{payload.get('parameter_count')}`",
        f"- 组合数量：`{payload.get('combination_count')}`",
        f"- 稳健性分数：`{payload.get('robustness_score')}`",
        f"- 数据来源模式：`{payload.get('source_mode') or '-'}`",
        f"- 警告数量：`{summary.get('warning_count')}`",
        "",
        "## 参数稳定性指标",
    ]
    metric_labels = {
        "annual_return": "年化收益",
        "max_drawdown": "最大回撤",
        "sharpe": "夏普比率",
    }
    for key, stats in (payload.get("metrics") or {}).items():
        if isinstance(stats, dict):
            label = metric_labels.get(str(key), str(key))
            lines.append(
                f"- {label}：均值：`{stats.get('mean')}`；波动：`{stats.get('std')}`；"
                f"最小值：`{stats.get('min')}`；最大值：`{stats.get('max')}`；"
                f"状态：`{stats.get('robustness_status')}`"
            )
    lines.extend(["", "## 过拟合风险提示"])
    lines.extend(f"- {item.get('message') if isinstance(item, dict) else item}" for item in overfit[:20])
    lines.extend(["", "## 其他警告"])
    lines.extend(f"- {item}" for item in warnings[:20])
    lines.extend(["", "参数稳定也不代表未来有效；统计检验和数据健康仍需单独检查。"])
    return "\n".join(lines)


def write_parameter_sensitivity_outputs(
    payload: dict[str, Any],
    *,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, str]:
    json_path = write_json(_project_path(output_path), payload)
    report_target = _project_path(report_path)
    report_target.parent.mkdir(parents=True, exist_ok=True)
    report_target.write_text(build_parameter_sensitivity_report(payload), encoding="utf-8")
    return {"json_path": _rel(json_path), "report_path": _rel(report_target)}
