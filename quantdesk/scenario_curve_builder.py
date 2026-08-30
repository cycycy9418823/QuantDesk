"""Scenario observation curves for market-intelligence candidates.

Curves built here are not price predictions. They are simple historical and
hypothetical observation paths used to reason about paper scenarios.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.market_intelligence_utils import read_daily_tail, rel_path, write_markdown
from quantdesk.schema_contracts import build_base_result
from quantdesk.shared import safe_float, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_PATH = PROJECT_ROOT / "results" / "market_intelligence" / "scenario_curves_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "scenario_curves_report_latest.md"
DISCLAIMER = "该曲线不是价格预测，不构成投资建议。"


def build_observation_curve(
    symbol: str,
    *,
    project_root: str | Path | None = None,
    lookback: int = 20,
    cutoff_date: str | None = None,
) -> list[dict[str, Any]]:
    rows = read_daily_tail(symbol, project_root=project_root, limit=lookback, cutoff_date=cutoff_date)
    curve = []
    for row in rows:
        close = safe_float(row.get("close"), None)
        if close is None:
            continue
        curve.append({"date": row.get("date") or row.get("trade_date") or "", "close": float(close)})
    return curve


def build_risk_band_curve(base_price: float) -> list[dict[str, Any]]:
    return [
        {"scenario": "risk", "step": 0, "value": round(base_price, 4)},
        {"scenario": "risk", "step": 1, "value": round(base_price * 0.97, 4)},
        {"scenario": "neutral", "step": 1, "value": round(base_price, 4)},
        {"scenario": "optimistic", "step": 1, "value": round(base_price * 1.03, 4)},
    ]


def build_candidate_scenario_curve(
    candidate: dict[str, Any],
    *,
    project_root: str | Path | None = None,
    cutoff_date: str | None = None,
) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "")
    historical = build_observation_curve(symbol, project_root=project_root, cutoff_date=cutoff_date)
    base_price = (
        safe_float(historical[-1].get("close"), None)
        if cutoff_date and historical
        else (None if cutoff_date else safe_float(candidate.get("latest_close"), None))
    )
    warnings = []
    if len(historical) < 5:
        warnings.append("历史样本不足，情景观察曲线可靠性有限。")
    if base_price is None and historical:
        base_price = safe_float(historical[-1].get("close"), None)
    scenario = build_risk_band_curve(float(base_price)) if base_price is not None else []
    if not scenario:
        warnings.append("缺少基准价格，无法生成情景观察曲线。")
    return {
        "symbol": symbol,
        "name": candidate.get("name") or "",
        "curve_type": "scenario_observation_not_prediction",
        "historical_reference_curve": historical,
        "scenario_observation_curve": scenario,
        "risk_band": scenario,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def build_scenario_curves(
    *,
    candidates_payload: dict[str, Any],
    target_date: str,
    project_root: str | Path | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for sector in candidates_payload.get("sectors") or []:
        for item in sector.get("candidates") or []:
            if item.get("style") != "blocked":
                candidates.append(item)
    selected = candidates[: max(0, int(limit or 12))]
    curves = [
        build_candidate_scenario_curve(item, project_root=project_root, cutoff_date=target_date)
        for item in selected
    ]
    warnings = [warning for curve in curves for warning in curve.get("warnings") or []]
    if not selected:
        warnings.append("没有候选，情景观察曲线未生成。")
    return build_base_result(
        status="warning" if warnings else "passed",
        ok=True,
        target_date=target_date,
        warnings=warnings,
        suggested_next_action="先运行 market-intel 生成低价观察候选，再查看情景观察曲线。",
        disclaimer=DISCLAIMER,
        task_type="scenario_curve_observation",
        curve_type="scenario_observation_not_prediction",
        candidate_count=len(selected),
        curves=curves,
    )


def write_scenario_curve_outputs(
    payload: dict[str, Any],
    *,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    project_root: str | Path | None = None,
) -> tuple[Path, Path]:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    json_path = Path(output_path)
    report = Path(report_path)
    if not json_path.is_absolute():
        json_path = root / json_path
    if not report.is_absolute():
        report = root / report
    write_json(json_path, payload)
    lines = [
        "# QuantDesk 情景观察曲线",
        "",
        DISCLAIMER,
        "",
        f"- curve_type: `{payload.get('curve_type')}`",
        f"- candidate_count: `{payload.get('candidate_count')}`",
        "",
        "这些曲线是历史参考和假设情景，不是确定性价格预测，不包含交易点位。",
        "",
        "## 曲线清单",
        "",
    ]
    for curve in payload.get("curves") or []:
        lines.append(f"- {curve.get('symbol')} {curve.get('name')} | {curve.get('curve_type')}")
    if not payload.get("curves"):
        lines.append("- 暂无")
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {warning}" for warning in payload.get("warnings") or []] or ["- 无"])
    write_markdown(report, lines)
    payload["output_path"] = rel_path(json_path, project_root=root)
    payload["report_path"] = rel_path(report, project_root=root)
    write_json(json_path, payload)
    return json_path, report
