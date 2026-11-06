"""Publish derived paper results and reports; execution decides when to call.

Paths, clocks, I/O and nested publishers are supplied at call time. This module
does not load the execution engine, write the ledger or commit a trade journal.
Individual output failures and the existing partial-publication behavior remain
visible to callers; publication is not an atomic multi-file transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

Payload = dict[str, Any]
Transform = Callable[[Payload], Payload]
JsonReader = Callable[[Path, Any], Any]
JsonWriter = Callable[[Path, Any], Any]


@dataclass(frozen=True)
class PaperPublicationPaths:
    """Derived-output paths only; no ledger, journal or market-data targets."""

    project_root: Path
    results_dir: Path
    reports_dir: Path
    latest_json: Path
    history_json: Path
    swing_plans_json: Path
    intraday_execution_json: Path


def write_auto_paper_latest(
    snapshot: Payload, *,
    paths: PaperPublicationPaths,
    unrecorded: list[str],
    reconcile: Transform,
    identity: Transform,
    write_json: JsonWriter,
) -> Payload:
    """Persist a canonical latest snapshot without mutating the source state.

    A snapshot is assembled with its money detail under ``summary``; without
    promoting the missing top-level fields here, ``reconcile_paper_account``
    would read absent ``cash`` as zero and publish a cash/equity that disagrees
    with the real ledger and the summary it carries.
    """

    prepared = dict(snapshot)
    summary = prepared.get("summary")
    if isinstance(summary, dict):
        for key in ("cash", "market_value", "total_equity", "realized_pnl", "unrealized_pnl", "initial_cash"):
            if prepared.get(key) is None and summary.get(key) is not None:
                prepared[key] = summary[key]
    if unrecorded:
        # Shadow-risk failures that could not reach their own sink (used to be
        # `except Exception: pass`). Published here, never in the ledger state.
        unrecorded = list(dict.fromkeys(unrecorded))
        prepared["shadow_warnings"] = list(dict.fromkeys([*(prepared.get("shadow_warnings") or []), *unrecorded]))
        prepared["warnings"] = list(dict.fromkeys([*(prepared.get("warnings") or []), *unrecorded]))
    normalized = reconcile(prepared)
    if isinstance(summary, dict):
        # The persisted summary declares this run's PnL. Its earlier check may
        # have used the ledger's prior totals while today's rows were assembled.
        # Recheck these declared values without filling missing fields or
        # replacing a conflict with detail-derived financial values.
        summary_identity_input = dict(summary)
        for key in ("credit_used", "simulated_interest"):
            if key not in summary_identity_input and key in prepared:
                summary_identity_input[key] = prepared[key]
        # Correction evidence in a summary refers to the original records.
        for key in ("closed_positions", "realized_reductions"):
            if key not in summary_identity_input and key in prepared:
                summary_identity_input[key] = prepared[key]
        summary_identity = identity(summary_identity_input)
        normalized["summary"] = {
            **summary,
            "pnl_identity": summary_identity,
            "accounting_status": "consistent" if summary_identity["status"] == "consistent" else "unverified",
        }
    write_json(paths.latest_json, normalized)
    return normalized


def append_history(
    latest: Payload, *,
    paths: PaperPublicationPaths,
    read_json: JsonReader,
    write_json: JsonWriter,
    timestamp: Callable[[], str],
    disclaimer: str,
) -> Payload:
    history = read_json(paths.history_json, {})
    if not isinstance(history, dict):
        history = {}
    items = history.get("items") if isinstance(history.get("items"), list) else []
    transaction = latest.get("strategy_journal_transaction")
    if transaction and any(item.get("strategy_journal_transaction") == transaction for item in items):
        return history
    summary = latest.get("summary") if isinstance(latest.get("summary"), dict) else {}
    items.append({
        "generated_at": latest.get("generated_at"),
        "valuation_date": latest.get("valuation_date"),
        "portfolio_review_valid": latest.get("portfolio_review_valid"),
        **({"strategy_journal_transaction": transaction} if transaction else {}),
        "mode": latest.get("mode"),
        "strategy": latest.get("strategy"),
        "cash": summary.get("cash"),
        "market_value": summary.get("market_value"),
        "total_equity": summary.get("total_equity"),
        "total_return_pct": summary.get("total_return_pct"),
        "action_counts": latest.get("decision_audit", {}).get("action_counts", {}),
    })
    payload = {"ok": True, "updated_at": timestamp(), "items": items[-500:], "disclaimer": disclaimer}
    write_json(paths.history_json, payload)
    return payload


def write_report(
    latest: Payload, *,
    paths: PaperPublicationPaths,
    clock: Callable[[], datetime],
    disclaimer: str,
    broker_boundary: str,
    next_open_fill_mode: str,
    render_shadow: Callable[[Payload], list[str]],
    record_shadow_error: Callable[..., Any],
    shadow_error_path: Callable[[], Path],
    unrecorded: list[str],
    risk_gate_shadow_summary: Payload | None = None,
) -> Path:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    path = paths.reports_dir / f"auto_paper_portfolio_report_{clock().strftime('%Y%m%d_%H%M%S')}.md"
    summary = latest.get("summary") or {}
    lines = [
        "# 自动虚拟组合复盘报告",
        "",
        disclaimer,
        broker_boundary,
        "",
        "## 组合摘要",
        "",
        *([
            "- 撮合方式：下一交易日 09:30 归档开盘价；开盘前须有已记录意向。",
            "- 验证状态：研究输出·未验证；原始开盘价是纸面代理成交价，未证明真实流动性。",
            f"- 待核验意向：{len(latest.get('pending_orders') or [])} 笔；成交与拒绝见追加流水。",
        ] if latest.get("strategy_fill_mode") == next_open_fill_mode else []),
        f"- 当前现金：{summary.get('cash', 0)}",
        f"- 持仓市值：{summary.get('market_value', 0)}",
        f"- 总权益：{summary.get('total_equity', 0)}",
        f"- 总收益率：{summary.get('total_return_pct', 0)}%",
        f"- 当前持仓数：{summary.get('position_count', 0)}",
        *([f"- 对账尚未完成：权益与已实现/未实现盈亏存在未解释差额 "
           f"{summary['pnl_identity']['difference']:+.2f} 元；原始金额保留。"]
          if (summary.get("pnl_identity") or {}).get("status") == "mismatch" else []),
        "",
        "## 昨日持仓复盘",
        "",
    ]
    yesterday_review = latest.get("yesterday_holding_review") if isinstance(latest.get("yesterday_holding_review"), dict) else {}
    lines.extend([
        f"- 昨日持仓数：{yesterday_review.get('yesterday_position_count', 0)}",
        f"- 今日仍持有：{yesterday_review.get('today_still_holding', 0)}",
        f"- 今日新增：{yesterday_review.get('today_new', 0)}",
        f"- 今日模拟卖出：{yesterday_review.get('today_sold', 0)}",
        f"- 今日风控阻断：{yesterday_review.get('today_blocked', 0)}",
        f"- 昨日持仓今日总盈亏：{yesterday_review.get('yesterday_position_unrealized_pnl', 0)}",
        "",
        "| 代码 | 名称 | 策略 | 昨日数量 | 昨日价格 | 今日价格 | 今日涨跌 | 浮动盈亏 | 复盘结论 | 原因 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ])
    for row in yesterday_review.get("items") or []:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('name', '')} | {row.get('strategy_type', '')} | "
            f"{row.get('yesterday_quantity', 0)} | {row.get('yesterday_price', '')} | {row.get('today_price', '')} | "
            f"{row.get('today_return_pct', '')} | {row.get('floating_pnl', '')} | {row.get('review_conclusion', '')} | {row.get('reason', '')} |"
        )
    conditional_review = latest.get("conditional_review") if isinstance(latest.get("conditional_review"), dict) else {}
    conditional_plan = latest.get("conditional_plan") if isinstance(latest.get("conditional_plan"), dict) else {}
    conditional_execution = latest.get("conditional_execution") if isinstance(latest.get("conditional_execution"), dict) else {}
    if conditional_review or conditional_plan or conditional_execution:
        plan_summary = conditional_plan.get("summary") if isinstance(conditional_plan.get("summary"), dict) else {}
        lines += [
            "",
            "## 条件交易计划与盘中执行",
            "",
            "系统采用条件交易计划进行纸面模拟：先根据历史数据生成计划，再根据盘中数据判断是否触发。缺少分钟线时不会用收盘价假装成交。",
            "加仓、减仓、止盈、止损均为本地纸面模拟，不构成投资建议。",
            "本工具不接券商 API，不真实下单，不保存真实账户信息，不承诺收益。",
            "",
            f"- 计划 ID：{conditional_plan.get('plan_id', conditional_execution.get('plan_id', '-'))}",
            f"- 计划日期：{conditional_plan.get('plan_date', '-')}",
            f"- 交易日期：{conditional_plan.get('trade_date', conditional_execution.get('trade_date', '-'))}",
            f"- 条件计划数量：{plan_summary.get('symbol_count', 0)}",
            f"- 可靠成交数量：{conditional_review.get('reliable_event_count', 0)}",
            f"- 计划未触发数量：{conditional_review.get('no_fill_count', 0)}",
            f"- 风控阻断数量：{conditional_review.get('blocked_count', 0)}",
            f"- 已验证分钟池命中数量：{conditional_review.get('verified_completed_intraday_count', 0)}",
            f"- 本地未验证分钟线命中数量：{conditional_review.get('local_unverified_intraday_count', 0)}",
            f"- 缺少分钟线数量：{conditional_review.get('missing_intraday_count', 0)}",
            "",
        ]
    if isinstance(risk_gate_shadow_summary, dict):
        try:
            lines.extend(render_shadow(risk_gate_shadow_summary))
        except Exception as exc:
            # A distinct header: the failure is visible, but is never mistaken
            # for a rendered shadow section.
            lines.extend(["", "## 影子风险门（未能生成）", "",
                          f"- 影子风险段生成失败：{type(exc).__name__}: {exc}"])
            try:
                record_shadow_error(
                    surface="auto_paper_report_shadow_section",
                    error=exc,
                    as_of=str(risk_gate_shadow_summary.get("as_of") or ""),
                    error_path=shadow_error_path(),
                )
            except Exception as log_exc:  # noqa: BLE001 - visible in the report
                lines.append(f"- 影子风险错误日志写入也失败：{type(log_exc).__name__}: {log_exc}")
                unrecorded.append(f"shadow_error_log_write_failed:auto_paper_report_shadow_section:{type(log_exc).__name__}")
    lines += [
        "",
        "## 今日动作",
        "",
        "| 动作 | 代码 | 名称 | 策略 | 数量 | 价格 | 原因 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for action in latest.get("actions") or []:
        lines.append(
            f"| {action.get('action_label') or action.get('action')} | {action.get('symbol')} | "
            f"{action.get('name', '')} | {action.get('strategy_type', '')} | {action.get('quantity', 0)} | "
            f"{action.get('price', '')} | {action.get('reason', '')} |"
        )
    lines += ["", "## 策略决策审计", "", "单日结果不能证明策略长期有效，需要累计样本继续观察。"]
    for note in latest.get("decision_audit", {}).get("strategy_learning_notes", []):
        lines.append(f"- {note}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_swing_execution_report(
    plans: list[Payload], executions: list[Payload], *,
    paths: PaperPublicationPaths,
    clock: Callable[[], datetime],
) -> Path:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    path = paths.reports_dir / f"swing_execution_report_{clock().strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        "# 长线波段计划与盘中触发回放",
        "",
        "本报告只用于本地策略学习与模拟验证，不构成投资建议。",
        "盘中回放按分钟线顺序检查触发，避免未来函数；缺少分钟线时不会用日线假装成交。",
        "",
        "## 波段计划",
        "",
        "| 代码 | 名称 | 计划日期 | 交易日期 | 低吸区间 | 不追高价格 | 计划原因 |",
        "|---|---|---|---|---|---:|---|",
    ]
    for plan in plans:
        buy_zones = plan.get("buy_zones") or []
        pullback = next((zone for zone in buy_zones if zone.get("type") == "pullback_buy"), {})
        zone_text = "-"
        if pullback:
            zone_text = f"{pullback.get('price_low', '-')}-{pullback.get('price_high', '-')}"
        lines.append(
            f"| {plan.get('symbol', '')} | {plan.get('name', '')} | {plan.get('plan_date', '')} | "
            f"{plan.get('trade_date', '')} | {zone_text} | {plan.get('no_chase_above', '')} | "
            f"{(buy_zones[0].get('reason') if buy_zones else plan.get('reason', ''))} |"
        )
    lines += [
        "",
        "## 盘中触发结果",
        "",
        "| 代码 | 动作 | 触发类型 | 触发时间 | 触发价格 | 数量 | 是否可靠 | 原因 |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for item in executions:
        lines.append(
            f"| {item.get('symbol', '')} | {item.get('action', '')} | {item.get('trigger_type', '')} | "
            f"{item.get('trigger_time', '')} | {item.get('trigger_price', '')} | {item.get('quantity', '')} | "
            f"{item.get('is_reliable', False)} | {item.get('reason', '')} |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_intraday_swing_artifacts(
    plans: list[Payload], executions: list[Payload], *,
    paths: PaperPublicationPaths,
    timestamp: Callable[[], str],
    disclaimer: str,
    write_json: JsonWriter,
    write_swing_report: Callable[[list[Payload], list[Payload]], Path],
    enabled: bool,
    source: str,
    allow_daily_approx: bool,
) -> Payload:
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    generated_at = timestamp()
    plan_payload = {
        "ok": True,
        "generated_at": generated_at,
        "enabled": bool(enabled),
        "intraday_source": source,
        "allow_daily_approx": bool(allow_daily_approx),
        "plan_count": len(plans),
        "items": plans,
        "disclaimer": disclaimer,
    }
    execution_payload = {
        "ok": True,
        "generated_at": generated_at,
        "enabled": bool(enabled),
        "intraday_source": source,
        "allow_daily_approx": bool(allow_daily_approx),
        "execution_count": len(executions),
        "triggered_count": sum(1 for row in executions if row.get("triggered")),
        "unreliable_count": sum(1 for row in executions if row.get("is_reliable") is False),
        "items": executions,
        "disclaimer": disclaimer,
    }
    write_json(paths.swing_plans_json, plan_payload)
    write_json(paths.intraday_execution_json, execution_payload)
    report_path = write_swing_report(plans, executions)
    execution_payload["report_path"] = str(report_path.relative_to(paths.project_root))
    write_json(paths.intraday_execution_json, execution_payload)
    return {"plans": plan_payload, "executions": execution_payload, "report_path": report_path}


def write_conditional_compat_artifacts(
    plan_payload: Payload, execution_payload: Payload, *,
    paths: PaperPublicationPaths,
    execution_rows: Callable[[Payload], list[Payload]],
    timestamp: Callable[[], str],
    write_swing_report: Callable[[list[Payload], list[Payload]], Path],
    write_json: JsonWriter,
    disclaimer: str,
    display_path: Callable[[Path], str],
) -> Payload:
    """Keep legacy swing artifact readers working while v2.8.4 uses conditional outputs."""

    plan_items = plan_payload.get("symbols") if isinstance(plan_payload.get("symbols"), list) else []
    execution_items = execution_rows(execution_payload)
    generated_at = timestamp()
    report_path = write_swing_report(plan_items, execution_items)
    write_json(paths.swing_plans_json, {
        "ok": True,
        "exists": True,
        "generated_at": generated_at,
        "compat_source": "conditional_trade_plans_latest",
        "plan_count": len(plan_items),
        "items": plan_items,
        "disclaimer": disclaimer,
    })
    write_json(paths.intraday_execution_json, {
        "ok": True,
        "exists": True,
        "generated_at": generated_at,
        "compat_source": "conditional_execution_latest",
        "execution_count": len(execution_items),
        "triggered_count": len(execution_payload.get("events") or []),
        "unreliable_count": sum(1 for row in execution_items if row.get("is_reliable") is False),
        "items": execution_items,
        "report_path": display_path(report_path),
        "disclaimer": disclaimer,
    })
    return {"report_path": report_path}


def publish_snapshot_result(
    result: Payload, *,
    paths: PaperPublicationPaths,
    write_latest: Transform,
    append_history: Transform,
    display_path: Callable[[Path], str],
    write_report: Callable[[Payload], Path],
) -> Payload:
    latest = write_latest(result)
    history = append_history(latest)
    latest["history_path"] = display_path(paths.history_json)
    latest["history_count"] = len(history.get("items") or [])
    latest["report_path"] = display_path(write_report(latest))
    return write_latest(latest)
