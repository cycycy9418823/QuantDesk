from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from quantdesk.data_audit import write_data_audit
from quantdesk.maintenance.v38_cleanup_audit import build_v38_cleanup_audit, write_v38_cleanup_audit
from quantdesk.research.factor_ic_report import build_factor_ic_report, write_factor_ic_report
from quantdesk.research.factor_panel_builder import build_factor_panel, write_factor_panel
from quantdesk.research.risk_stat_validation import build_risk_stat_validation, write_risk_stat_validation
from quantdesk.shared import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "results"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"
PROHIBITED_USAGE = [
    "live_trading",
    "broker_connection",
    "auto_execution",
    "buy_sell_recommendation",
    "real_money_allocation_instruction",
]


@dataclass(frozen=True)
class PipelineConfig:
    data_root: Path = PROJECT_ROOT / "data" / "daily"
    equity_csv: Path = PROJECT_ROOT / "results" / "portfolio_daily_equity_rebuild_latest.csv"
    results_dir: Path = DEFAULT_RESULTS_DIR
    reports_dir: Path = DEFAULT_REPORTS_DIR
    limit_symbols: int = 200
    max_dates: int = 36
    forward_days: int = 20
    daily_amount: float = 100_000_000.0
    order_notional: float = 100_000.0


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _coerce_status(value: Any) -> str:
    status = str(value or "warning").lower()
    return status if status in {"passed", "warning", "failed"} else "warning"


def _step_result(
    *,
    name: str,
    status: str,
    started_at: str,
    start_time: float,
    payload: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    payload = payload or {}
    files = payload.get("files_written") or []
    if isinstance(files, dict):
        files = list(files.values())
    return {
        "name": name,
        "status": _coerce_status(status),
        "started_at": started_at,
        "ended_at": now_text(),
        "duration_seconds": round(time.perf_counter() - start_time, 3),
        "warnings": [str(item) for item in (payload.get("warnings") or [])],
        "error": error,
        "output_files": [str(item) for item in files],
        "summary": _summarize_payload(payload),
    }


def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "task_id",
        "status",
        "stock_count",
        "survivable_count",
        "row_count",
        "symbol_count",
        "factor_count",
        "ic_period_count",
        "portfolio_name",
        "date_start",
        "date_end",
        "blockers",
    ]
    summary = {key: payload.get(key) for key in keys if key in payload}
    if "metrics" in payload:
        summary["metrics"] = payload.get("metrics")
    return summary


def _safe_run(name: str, runner: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = now_text()
    start_time = time.perf_counter()
    try:
        payload = runner()
        return payload, _step_result(
            name=name,
            status=str(payload.get("status") or "warning"),
            started_at=started_at,
            start_time=start_time,
            payload=payload,
        )
    except Exception as exc:  # pragma: no cover - exact failure messages vary
        payload = {
            "task_id": name,
            "status": "failed",
            "research_only": True,
            "warnings": [f"{name}_failed"],
            "error": f"{type(exc).__name__}: {exc}",
            "traceback_tail": traceback.format_exc(limit=4),
        }
        return payload, _step_result(
            name=name,
            status="failed",
            started_at=started_at,
            start_time=start_time,
            payload=payload,
            error=payload["error"],
        )


def run_research_daily_pipeline(config: PipelineConfig | None = None) -> dict[str, Any]:
    cfg = config or PipelineConfig()
    results_dir = _project_path(cfg.results_dir)
    reports_dir = _project_path(cfg.reports_dir)
    data_root = _project_path(cfg.data_root)
    equity_csv = _project_path(cfg.equity_csv)
    results_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    factor_panel_csv = results_dir / "factor_panel_latest.csv"
    factor_panel_json = results_dir / "factor_panel_latest.json"
    factor_panel_md = reports_dir / "factor_panel_latest.md"
    step_payloads: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []

    def data_audit_runner() -> dict[str, Any]:
        return write_data_audit(
            data_root,
            output_json=results_dir / "data_audit.json",
            output_md=reports_dir / "data_audit.md",
        )

    def factor_panel_runner() -> dict[str, Any]:
        payload, panel = build_factor_panel(
            data_root,
            limit_symbols=cfg.limit_symbols,
            max_dates=cfg.max_dates,
            forward_days=cfg.forward_days,
        )
        files = write_factor_panel(
            payload,
            panel,
            output_csv=factor_panel_csv,
            output_json=factor_panel_json,
            report_md=factor_panel_md,
        )
        payload["files_written"] = list(files.values())
        return payload

    def factor_ic_runner() -> dict[str, Any]:
        payload, ic_df = build_factor_ic_report(factor_panel_csv, forward_return_col="fwd_ret_1m")
        files = write_factor_ic_report(
            payload,
            ic_df,
            output_json=results_dir / "factor_ic_report_latest.json",
            output_csv=results_dir / "factor_ic_report_latest.csv",
            report_md=reports_dir / "factor_ic_report_latest.md",
        )
        payload["files_written"] = list(files.values())
        return payload

    def risk_stat_runner() -> dict[str, Any]:
        payload = build_risk_stat_validation(
            equity_csv,
            portfolio_name="combo_50_50",
            order_notional=cfg.order_notional,
            daily_amount=cfg.daily_amount,
        )
        files = write_risk_stat_validation(
            payload,
            output_json=results_dir / "risk_stat_validation_latest.json",
            report_md=reports_dir / "risk_stat_validation_latest.md",
        )
        payload["files_written"] = list(files.values())
        return payload

    def cleanup_runner() -> dict[str, Any]:
        payload = build_v38_cleanup_audit()
        files = write_v38_cleanup_audit(
            payload,
            output_json=results_dir / "v38_cleanup_audit_latest.json",
            report_md=reports_dir / "v38_cleanup_audit_latest.md",
        )
        payload["files_written"] = list(files.values())
        return payload

    runners: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("data_audit", data_audit_runner),
        ("factor_panel", factor_panel_runner),
        ("factor_ic", factor_ic_runner),
        ("risk_stat", risk_stat_runner),
        ("v38_cleanup_audit", cleanup_runner),
    ]
    for name, runner in runners:
        payload, result = _safe_run(name, runner)
        step_payloads[name] = payload
        steps.append(result)

    failed = [step["name"] for step in steps if step["status"] == "failed"]
    warning = [step["name"] for step in steps if step["status"] == "warning"]
    status = "failed" if len(failed) == len(steps) else "warning" if failed or warning else "passed"
    files_written = [
        path
        for step in steps
        for path in step.get("output_files", [])
    ]
    return {
        "task_id": "v38_research_daily_pipeline",
        "status": status,
        "research_only": True,
        "network_required": False,
        "no_broker": True,
        "no_execution": True,
        "no_trading_advice": True,
        "allowed_usage": ["research_dashboard", "paper_research", "risk_monitoring", "human_review"],
        "prohibited_usage": PROHIBITED_USAGE,
        "generated_at": now_text(),
        "config": {
            "data_root": str(data_root),
            "equity_csv": str(equity_csv),
            "limit_symbols": int(cfg.limit_symbols),
            "max_dates": int(cfg.max_dates),
            "forward_days": int(cfg.forward_days),
            "daily_amount": float(cfg.daily_amount),
            "order_notional": float(cfg.order_notional),
        },
        "steps": steps,
        "failed_steps": failed,
        "warning_steps": warning,
        "files_written": files_written,
        "step_payload_summaries": {name: _summarize_payload(payload) for name, payload in step_payloads.items()},
        "disclaimer": "Research-only daily diagnostics. No broker connection, no live trading, no auto execution, no buy/sell recommendation.",
    }


def render_research_daily_pipeline_markdown(payload: dict[str, Any]) -> str:
    status_labels = {
        "passed": "通过",
        "completed": "完成",
        "warning": "有警告",
        "completed_with_warnings": "完成但有警告",
        "failed": "失败",
        "missing": "缺失",
        "skipped": "已跳过",
    }

    def status_text(value: Any) -> str:
        raw = str(value or "missing").strip().lower()
        return status_labels.get(raw, str(value or "缺失"))

    prohibited_labels = {
        "broker_connection": "券商连接",
        "auto_execution": "自动执行",
        "live_trading": "实盘交易",
        "buy_sell_advice": "买卖建议",
    }
    lines = [
        "# v3.8 研究日流程",
        "",
        "本报告只编排本地研究诊断；不联网获取行情、不连接券商、不下订单，也不提供买卖建议。",
        "",
        f"- 状态：{status_text(payload.get('status'))}",
        f"- 使用边界：{'仅研究' if payload.get('research_only', True) else '边界缺失'}",
        f"- 失败步骤：{', '.join(payload.get('failed_steps') or []) or '无'}",
        f"- 警告步骤：{', '.join(payload.get('warning_steps') or []) or '无'}",
        "",
        "## 步骤明细",
    ]
    for step in payload.get("steps", []):
        lines.append(
            f"- `{step.get('name')}`：状态：{status_text(step.get('status'))}；"
            f"耗时：{step.get('duration_seconds')} 秒；警告数：{len(step.get('warnings') or [])}"
        )
        if step.get("error"):
            lines.append(f"  - 错误：`{step.get('error')}`")
    lines.extend(
        [
            "",
            "## 禁止用途",
            *(f"- {prohibited_labels.get(str(item), str(item))}" for item in payload.get("prohibited_usage", [])),
            "",
            "Research Only / Paper Account Only / No Broker Route / Not Investment Advice / Not Position Advice.",
            "No buy/sell recommendations.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_research_daily_pipeline(
    payload: dict[str, Any],
    *,
    output_json: str | Path = DEFAULT_RESULTS_DIR / "v38_daily_pipeline_latest.json",
    report_md: str | Path = DEFAULT_REPORTS_DIR / "v38_daily_pipeline_latest.md",
) -> dict[str, str]:
    json_path = _project_path(output_json)
    md_path = _project_path(report_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_research_daily_pipeline_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "report_path": str(md_path)}
