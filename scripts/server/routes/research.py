"""Read-only research summary routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from flask import Blueprint, Flask, jsonify

from quantdesk.csv_io import read_csv_frame
from scripts.server.helpers.util import clean_payload, read_json_file


@dataclass(frozen=True)
class ResearchRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    project_root: Callable[[], Path]
    paper_watchlist_json: Callable[[], Path]
    project_relative_path: Callable[[Path], str]
    latest_research_evidence_context: Callable[[], dict[str, Any]]
    load_paper_simulation_watchlist: Callable[[], dict[str, Any]]
    build_strategy_scorecard: Callable[[], dict[str, Any]]


_PERF_HOLD_COLS = ["hold_1d_ret", "hold_3d_ret", "hold_5d_ret", "hold_10d_ret", "hold_20d_ret"]
_MIN_20D_SAMPLE_COUNT = 20


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) and number not in (float("inf"), -float("inf")) else None


def _perf_hold_means(frame: pd.DataFrame) -> dict[str, float | None]:
    return {
        col: (_finite_float(pd.to_numeric(frame[col], errors="coerce").mean()) if col in frame.columns else None)
        for col in _PERF_HOLD_COLS
    }


def _valid_numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").replace([float("inf"), -float("inf")], pd.NA).dropna()


def create_research_blueprint(deps: ResearchRouteDeps) -> Blueprint:
    """Create read-only research/status routes used by the active 8000 UI."""

    bp = Blueprint("research_api", __name__)

    @bp.route("/api/strategy-scorecard", methods=["GET"])
    def get_strategy_scorecard() -> Any:
        """Read-only strategy scorecard for the active 8000 UI."""
        try:
            return jsonify(clean_payload(deps.build_strategy_scorecard()))
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "failed",
                        "message_for_ui": f"策略成绩单生成失败：{exc}",
                        "error": str(exc),
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                        "no_real_orders": True,
                    }
                )
            ), 500

    @bp.route("/api/performance/summary", methods=["GET"])
    def get_performance_summary() -> Any:
        """Post-selection outcome metrics from candidate_performance.csv + paper portfolio."""
        try:
            from quantdesk.evidence_freshness import build_evidence_freshness
            from quantdesk.paper.auto_paper_portfolio import auto_paper_portfolio_latest
            from quantdesk.research_metrics import research_metrics_to_dict, summarize_research_metrics

            # research_metrics expects forward_return_5d/20d + drawdown; map from hold_* columns.
            rename = {"hold_5d_ret": "forward_return_5d", "hold_20d_ret": "forward_return_20d"}
            project_root = deps.project_root()
            paper_watchlist_json = deps.paper_watchlist_json()
            perf_path = project_root / "data" / "tracking" / "candidate_performance.csv"
            overall: dict[str, Any] = {}
            by_strategy: list[dict[str, Any]] = []
            sample_count = 0
            excluded_outliers = 0
            total_rows = 0
            if perf_path.exists():
                df_all = read_csv_frame(perf_path)
                total_rows = int(len(df_all))
                # Some hold_*_ret values are corrupt (derived from daily closes that
                # spike to the millions), which destroys mean-based stats. A genuine
                # return over <=20 trading days cannot exceed a few hundred percent,
                # so treat any |holding return| > 500% as a data error and drop it.
                bad = pd.Series(False, index=df_all.index)
                for col in _PERF_HOLD_COLS:
                    if col in df_all.columns:
                        bad = bad | (pd.to_numeric(df_all[col], errors="coerce").abs() > 5.0)
                df = df_all[~bad].copy()
                excluded_outliers = int(bad.sum())
                sample_count = int(len(df))

                def _metrics_for(sub: pd.DataFrame) -> dict[str, Any]:
                    mapped = sub.rename(columns={k: v for k, v in rename.items() if k in sub.columns})
                    out = research_metrics_to_dict(summarize_research_metrics(mapped))
                    out["sample_count"] = int(len(sub))
                    hit_values = _valid_numeric_series(sub, "hit_flag")
                    hit_values = hit_values[hit_values.isin([0, 1])]
                    sample_count_5d = int(len(_valid_numeric_series(sub, "hold_5d_ret")))
                    sample_count_20d = int(len(_valid_numeric_series(sub, "hold_20d_ret")))
                    out["hit_rate"] = _finite_float((hit_values == 1).mean()) if len(hit_values) else None
                    out["hit_rate_sample_count"] = int(len(hit_values))
                    out["hit_rate_pending_count"] = max(0, int(len(sub)) - int(len(hit_values)))
                    out["sample_count_5d"] = sample_count_5d
                    out["sample_count_20d"] = sample_count_20d
                    out["forward_return_20d_status"] = (
                        "available" if sample_count_20d >= _MIN_20D_SAMPLE_COUNT else "insufficient_sample"
                    )
                    out["hold_means"] = _perf_hold_means(sub)
                    return out

                overall = _metrics_for(df)
                if "strategy_type" in df.columns:
                    for stype, sub in df.groupby("strategy_type"):
                        metrics = _metrics_for(sub)
                        metrics["strategy_type"] = str(stype)
                        by_strategy.append(metrics)
                    by_strategy.sort(key=lambda r: r.get("sample_count") or 0, reverse=True)
            paper_watchlist = deps.load_paper_simulation_watchlist()
            paper_research_evidence = deps.latest_research_evidence_context()
            evidence_freshness = build_evidence_freshness(project_root)
            audit_warning = ""
            if not evidence_freshness.get("metrics_trustworthy", False):
                audit_warning = "审计账本或绩效证据过期、缺失或内部不一致，当前绩效指标不可信；"
            payload = {
                "ok": True,
                "sample_count": sample_count,
                "total_rows": total_rows,
                "excluded_outliers": excluded_outliers,
                "performance_csv_exists": perf_path.exists(),
                "overall": overall,
                "by_strategy": by_strategy,
                "evidence_freshness": evidence_freshness,
                "paper_watchlist": {
                    "exists": paper_watchlist_json.exists(),
                    "watchlist_count": len(paper_watchlist.get("items") or []),
                    "items": paper_watchlist.get("items", [])[:12],
                    "paper_tracking_summary": paper_watchlist.get("paper_tracking_summary") or {},
                    "research_evidence": paper_research_evidence,
                    "path": deps.project_relative_path(paper_watchlist_json),
                    "message": "纸面模拟池来自你确认或同步的候选，用于后续跟踪复盘；不接券商、不下真实订单。",
                },
                "paper_portfolio": auto_paper_portfolio_latest(),
                "message_for_ui": (
                    audit_warning
                    +
                    (f"已剔除 {excluded_outliers} 条异常样本（日线收盘损坏导致的离谱收益）；" if excluded_outliers else "")
                    + "历史样本统计，不代表未来收益，不构成投资建议。"
                    if sample_count
                    else audit_warning + "暂无绩效样本，请点击“重建绩效样本”。"
                ),
                "research_only": "Research Only；历史样本统计，不构成投资建议。",
            }
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "message_for_ui": f"绩效复盘生成失败：{exc}",
                        "error": str(exc),
                    }
                )
            ), 500
        return jsonify(clean_payload(payload))

    @bp.route("/api/strategy-lab/summary", methods=["GET"])
    def get_strategy_lab_summary() -> Any:
        """Read-only strategy lab evidence for the active 8000 UI."""
        try:
            evidence = deps.latest_research_evidence_context()
            strategy_lab = dict(evidence.get("strategy_lab") or {})
            strategy_lab.setdefault("available", False)
            strategy_lab.setdefault("status", "missing")
            strategy_lab.setdefault("top_strategies", [])
            strategy_lab.setdefault("warnings_top", [])
            strategy_lab.update(
                {
                    "ok": True,
                    "research_only": True,
                    "paper_only": True,
                    "no_broker": True,
                    "no_live_trading": True,
                    "no_auto_execution": True,
                    "no_real_orders": True,
                    "not_investment_advice": True,
                    "not_position_advice": True,
                    "message_for_ui": (
                        "Strategy lab evidence is read-only and used for research review only. "
                        "It is not a trade instruction, broker route, or position suggestion."
                    ),
                }
            )
            return jsonify(clean_payload(strategy_lab))
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                clean_payload(
                    {
                        "ok": False,
                        "status": "failed",
                        "message_for_ui": f"strategy lab summary failed: {exc}",
                        "error": str(exc),
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                        "no_real_orders": True,
                    }
                )
            ), 500

    @bp.route("/api/risk-notifications/summary", methods=["GET"])
    def get_risk_notifications_summary() -> Any:
        """Read-only risk and notification summary for the active 8000 UI."""
        try:
            project_root = deps.project_root()
            paper_watchlist_json = deps.paper_watchlist_json()
            paper_watchlist = deps.load_paper_simulation_watchlist()
            items = [item for item in paper_watchlist.get("items") or [] if isinstance(item, dict)]
            smart_items: list[dict[str, Any]] = []
            for item in items:
                smart = item.get("smart_stop") if isinstance(item.get("smart_stop"), dict) else {}
                exit_plan = item.get("paper_exit_plan") if isinstance(item.get("paper_exit_plan"), dict) else {}
                condition_plan = exit_plan.get("condition_plan") if isinstance(exit_plan.get("condition_plan"), dict) else {}
                if not smart and not exit_plan:
                    continue
                smart_items.append(
                    {
                        "symbol": item.get("symbol") or "",
                        "name": item.get("name") or "",
                        "review_status": item.get("review_status") or "",
                        "review_label": item.get("review_label") or "",
                        "trigger_status": smart.get("trigger_status") or "",
                        "trigger_reason": smart.get("trigger_reason") or smart.get("message") or "",
                        "paper_exit_candidate": bool(exit_plan.get("paper_exit_candidate")),
                        "forced_exit_rule_count": len(condition_plan.get("forced_exit_rules") or []),
                        "paper_only": True,
                    }
                )
            triggered_items = [
                item
                for item in smart_items
                if item.get("trigger_status") == "triggered" or item.get("paper_exit_candidate")
            ]
            workflow = read_json_file(project_root / "results" / "daily_workflow_latest.json")
            workflow = workflow if isinstance(workflow, dict) else {}
            guardrails = workflow.get("guardrail_summary") if isinstance(workflow.get("guardrail_summary"), dict) else {}
            risk_snapshot = workflow.get("risk_alert_snapshot") if isinstance(workflow.get("risk_alert_snapshot"), dict) else {}
            risk_latest = read_json_file(project_root / "results" / "risk_alerts_latest.json")
            if not risk_snapshot and isinstance(risk_latest, dict):
                risk_snapshot = {
                    "status": risk_latest.get("status") or "missing",
                    "alert_count": risk_latest.get("alert_count") or len(risk_latest.get("alerts") or []),
                    "critical_count": risk_latest.get("critical_count") or 0,
                    "top_alerts": (risk_latest.get("alerts") or [])[:5],
                    "warnings": risk_latest.get("warnings") or [],
                }
            notify_latest = read_json_file(project_root / "results" / "notification_center_latest.json")
            notify_latest = notify_latest if isinstance(notify_latest, dict) else {}
            try:
                from quantdesk.local_alerts import local_alerts_payload

                local_alerts = local_alerts_payload(limit=20)
            except Exception as exc:  # noqa: BLE001
                local_alerts = {"status": "warning", "alerts": [], "summary": {}, "warnings": [str(exc)]}
            payload = {
                "ok": True,
                "status": "warning" if triggered_items or (risk_snapshot.get("critical_count") or 0) else "passed",
                "paper_watchlist": {
                    "exists": paper_watchlist_json.exists(),
                    "watchlist_count": len(items),
                    "tracking_summary": paper_watchlist.get("paper_tracking_summary") or {},
                    "updated_at": paper_watchlist.get("updated_at") or "",
                    "path": deps.project_relative_path(paper_watchlist_json),
                },
                "smart_stop": {
                    "item_count": len(smart_items),
                    "triggered_count": len(triggered_items),
                    "paper_exit_review_count": sum(1 for item in smart_items if item.get("paper_exit_candidate")),
                    "items": smart_items[:20],
                    "triggered_items": triggered_items[:10],
                    "source": "state/paper_watchlist.json",
                    "paper_only": True,
                },
                "risk_alerts": {
                    "status": risk_snapshot.get("status") or "missing",
                    "alert_count": risk_snapshot.get("alert_count") or 0,
                    "critical_count": risk_snapshot.get("critical_count") or 0,
                    "top_alerts": (risk_snapshot.get("top_alerts") or [])[:8],
                    "warnings": (risk_snapshot.get("warnings") or [])[:8],
                },
                "notification_center": {
                    "status": notify_latest.get("status") or "missing",
                    "dry_run": bool(notify_latest.get("dry_run", True)),
                    "message_count": notify_latest.get("message_count") or len(notify_latest.get("messages") or []),
                    "sent_count": notify_latest.get("sent_count") or 0,
                    "skipped_count": notify_latest.get("skipped_count") or 0,
                    "warnings": (notify_latest.get("warnings") or [])[:8],
                },
                "local_alerts": local_alerts,
                "guardrails": {
                    "enabled": bool(guardrails.get("enabled")),
                    "status": guardrails.get("status") or "missing",
                    "warning_count": guardrails.get("warning_count") or 0,
                    "failed_count": guardrails.get("failed_count") or 0,
                    "feature_quality_status": guardrails.get("feature_quality_status") or "",
                    "statistical_validation_status": guardrails.get("statistical_validation_status") or "",
                    "active_diagnostics": guardrails.get("active_diagnostics") or {},
                    "warnings": (guardrails.get("warnings") or [])[:8],
                },
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "no_live_trading": True,
                "no_auto_execution": True,
                "no_buy_sell_advice": True,
                "no_position_advice": True,
                "message_for_ui": (
                    "Read-only risk and notification board. No broker route, no real orders, no position advice."
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return jsonify(clean_payload({"ok": False, "status": "failed", "message_for_ui": str(exc), "error": str(exc)})), 500
        return jsonify(clean_payload(payload))

    return bp


def register_research_routes(app: Flask, deps: ResearchRouteDeps) -> None:
    app.register_blueprint(create_research_blueprint(deps))
