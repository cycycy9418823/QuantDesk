"""Read-only product evidence, with process completion separate from validity."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request


def read_evidence(path: Path, errors: list[dict[str, str]]) -> Any:
    """Do not initialize missing accounts or treat parse errors as no events."""
    try:
        raw = path.read_bytes()
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("evidence_too_large")
        return json.loads(raw.decode("utf-8-sig"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError, UnicodeError) as exc:
        errors.append({"path": path.as_posix(), "status": "missing" if not path.exists() else "unreadable", "reason": type(exc).__name__})
        return {}


def research_summary(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Report existing provenance; never promote a pass label to profitable."""
    def synthetic(node: Any) -> bool:
        if isinstance(node, Mapping):
            return any((key in {"synthetic_test_data", "synthetic", "is_synthetic"} and item is True)
                       or synthetic(item) for key, item in node.items())
        if isinstance(node, list):
            return any(synthetic(item) for item in node)
        return False
    demo = synthetic(value)
    business_date = next((str(value.get(key))[:10] for key in ("as_of", "target_date", "signal_date", "trade_date", "end_date", "data_date") if value.get(key)), "")
    return {"source": source, "available": bool(value), "source_type": "synthetic_demo" if demo else "real_or_unspecified",
            "synthetic": demo, "business_date": business_date,
            "process_status": value.get("status"), "original_allowed_for_trade_plan": value.get("allowed_for_trade_plan"),
            "current_rule_validation": "not_established", "rule_identity": value.get("rule_version") or value.get("strategy") or value.get("strategy_name"),
            "validation_message": "合成演示，不能作为当前规则成绩。" if demo else "流程状态与盈利有效性分开；尚未建立与当前参考规则同版本的真实样本外证据。",
            "limitations": ["历史股票池/退市/ST覆盖须独立核实", "单股平均收益不等于资金约束组合", "未匹配的策略成绩不能为当前参考价背书"]}


def register_product_review_routes(app: Flask, *, root: Callable[[], Path], capabilities: Callable[[], dict[str, Any]]) -> None:
    @app.route("/api/runtime-capabilities", methods=["GET"])
    def runtime_capabilities() -> Any:
        return jsonify(capabilities())

    @app.route("/api/morning-brief", methods=["GET"])
    def morning_brief() -> Any:
        from quantdesk.morning_brief_view import build_morning_brief_view
        result = build_morning_brief_view(root(), requested_date=request.args.get("date", ""))
        return jsonify(result), 200 if result.get("ok") else 400

    @app.route("/api/product-integrity", methods=["GET"])
    def product_integrity() -> Any:
        from quantdesk.product_integrity import build_product_integrity
        from quantdesk.market_clock import load_cn_trading_calendar
        project = root()
        errors: list[dict[str, str]] = []
        state = read_evidence(project / "state/auto_paper_portfolio.json", errors)
        history = read_evidence(project / "results/auto_paper_portfolio_history.json", errors)
        ledgers = {name: read_evidence(project / "state" / folder / "ledger.json", errors)
                   for name, folder in (("default", "ai_trader"), ("glm", "ai_trader_glm"))}
        try:
            calendar = load_cn_trading_calendar(project / "data/calendar/a_share_trading_calendar.csv")
        except (OSError, ValueError) as exc:
            errors.append({"path": "data/calendar/a_share_trading_calendar.csv", "status": "unreadable", "reason": type(exc).__name__})
            calendar = {}
        report = build_product_integrity(state if isinstance(state, Mapping) else {}, history=history,
                                         calendar=calendar, ai_ledgers=ledgers)
        evidence = {}
        for kind, rel in (("backtest", "results/backtest/backtest_latest.json"),
                          ("walk_forward", "results/walk_forward_latest.json"),
                          ("dashboard", "results/research_dashboard_latest.json")):
            payload = read_evidence(project / rel, errors)
            evidence[kind] = research_summary(payload if isinstance(payload, Mapping) else {}, source=rel)
        return jsonify(ok=True, as_of=state.get("valuation_date") if isinstance(state, Mapping) else None,
                       report=report, research_evidence=evidence, source_errors=errors,
                       read_only=True, label="纸面 · 未验证")
