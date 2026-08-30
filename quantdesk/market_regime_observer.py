"""Local market regime observation for QuantDesk.

This module estimates a market environment snapshot from local samples only
unless the caller explicitly enables network handling in a future provider.
Missing full-market data is reported as a warning rather than fabricated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.market_intelligence_utils import load_scope_symbols, rel_path, write_markdown
from quantdesk.schema_contracts import build_base_result
from quantdesk.shared import safe_float, write_json
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_PATH = PROJECT_ROOT / "results" / "market_intelligence" / "market_regime_latest.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "market_regime_report_latest.md"
DISCLAIMER = "仅用于本地市场环境观察，不构成投资建议。"


def compute_index_trend_state(*, project_root: str | Path | None = None) -> dict[str, Any]:
    return {
        "sh_index": None,
        "sz_index": None,
        "chi_next": None,
        "trend_status": "unknown",
        "data_scope": "missing",
        "warning": "未发现本地指数 latest 文件；不会编造指数状态。",
    }


def compute_market_breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    up_count = 0
    down_count = 0
    flat_count = 0
    limit_up_count = 0
    limit_down_count = 0
    for row in rows:
        pct = safe_float(row.get("pct_chg"), None)
        if pct is None:
            continue
        if pct > 0:
            up_count += 1
        elif pct < 0:
            down_count += 1
        else:
            flat_count += 1
        if pct >= 9.8:
            limit_up_count += 1
        if pct <= -9.8:
            limit_down_count += 1
    return {
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "sample_count": len(rows),
    }


def compute_limit_up_temperature(breadth: dict[str, Any]) -> float:
    sample = max(1, int(breadth.get("sample_count") or 0))
    limit_up = int(breadth.get("limit_up_count") or 0)
    up_count = int(breadth.get("up_count") or 0)
    return min(100.0, round(limit_up * 10 + (up_count / sample) * 60, 2))


def compute_turnover_temperature(rows: list[dict[str, Any]]) -> dict[str, Any]:
    amounts = [safe_float(row.get("daily_amount") or row.get("amount"), None) for row in rows]
    clean = [float(value) for value in amounts if value is not None and float(value) > 0]
    if not clean:
        return {"market_amount": None, "amount_status": "unknown", "sample_amount": None}
    sample_amount = float(sum(clean))
    avg_amount = sample_amount / len(clean)
    if avg_amount >= 1_000_000_000:
        status = "hot"
    elif avg_amount >= 300_000_000:
        status = "normal"
    else:
        status = "cool"
    return {
        "market_amount": None,
        "amount_status": status,
        "sample_amount": round(sample_amount, 2),
        "sample_average_amount": round(avg_amount, 2),
    }


def classify_market_regime(breadth: dict[str, Any], turnover: dict[str, Any]) -> str:
    sample = max(1, int(breadth.get("sample_count") or 0))
    up_ratio = int(breadth.get("up_count") or 0) / sample
    limit_up = int(breadth.get("limit_up_count") or 0)
    amount_status = str(turnover.get("amount_status") or "unknown")
    if up_ratio >= 0.62 and (limit_up >= 2 or amount_status == "hot"):
        return "strong"
    if up_ratio <= 0.35 and amount_status in {"cool", "unknown"}:
        return "weak"
    return "neutral"


def summarize_market_regime(payload: dict[str, Any]) -> str:
    regime = payload.get("market_regime") or "unknown"
    temperature = payload.get("market_temperature")
    scope = payload.get("data_scope") or "unknown"
    return f"{scope} 样本市场状态为 {regime}，观察温度 {temperature}；该结论仅来自可用本地样本。"


def build_market_regime_snapshot(
    *,
    target_date: str,
    scope: str = "current_analysis_pool",
    symbols: list[str] | None = None,
    network: bool = False,
    max_symbols: int = 80,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    rows, data_scope, scope_warnings = load_scope_symbols(
        scope=scope,
        symbols=symbols,
        project_root=project_root,
        max_symbols=max_symbols,
        cutoff_date=target_date,
    )
    warnings = list(scope_warnings)
    if network:
        warnings.append("market regime online provider is planned; current run uses local sample only.")
    if not rows:
        warnings.append("未找到可用本地样本，无法估算市场环境。")
    if data_scope in {"current_analysis_pool", "low_price_included"}:
        warnings.append("缺少全市场数据，本次 market_regime 基于 local_sample，不代表全 A。")
        data_scope = "local_sample"
    index_trend = compute_index_trend_state(project_root=project_root)
    if index_trend.get("warning"):
        warnings.append(str(index_trend["warning"]))
    breadth = compute_market_breadth(rows)
    turnover = compute_turnover_temperature(rows)
    regime = classify_market_regime(breadth, turnover) if rows else "unknown"
    temperature = compute_limit_up_temperature(breadth) if rows else 0
    status = "warning" if warnings else "passed"
    payload = build_base_result(
        status=status,
        ok=True,
        target_date=target_date,
        warnings=warnings,
        suggested_next_action="python scripts/qd.py market-intel --network false",
        disclaimer=DISCLAIMER,
        task_type="market_regime_observation",
        network=bool(network),
        data_scope=data_scope,
        market_regime=regime,
        market_temperature=temperature,
        index_trend=index_trend,
        breadth=breadth,
        turnover=turnover,
        summary=summarize_market_regime(
            {
                "market_regime": regime,
                "market_temperature": temperature,
                "data_scope": data_scope,
            }
        ),
        app_version=APP_VERSION,
    )
    return payload


def write_market_regime_snapshot(
    payload: dict[str, Any],
    *,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    project_root: str | Path | None = None,
) -> tuple[Path, Path]:
    json_path = Path(output_path)
    report = Path(report_path)
    if not json_path.is_absolute():
        json_path = (Path(project_root).resolve() if project_root else PROJECT_ROOT) / json_path
    if not report.is_absolute():
        report = (Path(project_root).resolve() if project_root else PROJECT_ROOT) / report
    write_json(json_path, payload)
    lines = [
        "# QuantDesk 市场环境观察",
        "",
        DISCLAIMER,
        "",
        f"- target_date: `{payload.get('target_date')}`",
        f"- status: `{payload.get('status')}`",
        f"- data_scope: `{payload.get('data_scope')}`",
        f"- market_regime: `{payload.get('market_regime')}`",
        f"- market_temperature: `{payload.get('market_temperature')}`",
        "",
        "## 说明",
        "",
        payload.get("summary") or "暂无摘要。",
        "",
        "## Warnings",
        "",
    ]
    warnings = payload.get("warnings") or []
    lines.extend([f"- {warning}" for warning in warnings] or ["- 无"])
    write_markdown(report, lines)
    payload["output_path"] = rel_path(json_path, project_root=project_root)
    payload["report_path"] = rel_path(report, project_root=project_root)
    write_json(json_path, payload)
    return json_path, report
