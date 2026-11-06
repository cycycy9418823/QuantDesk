"""Authoritative read-only payload for the active port-8000 decision board.

All paper metrics are owned by the backend.  This module never writes state,
creates orders, or turns missing evidence into a synthetic value.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from quantdesk.trader_profile import ResearchPaperProfile, validate_research_paper_profile
from quantdesk.paper.performance_report import finite_number, iso_date


REQUIRED_BOUNDARY_BADGES = (
    "Research Only",
    "Paper Account Only",
    "No Broker Route",
    "Live Execution Locked",
    "QMT Order Route Locked",
    "Not Investment Advice",
    "Not Position Advice",
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _int_value(value: Any) -> int | None:
    number = _finite(value)
    return int(number) if number is not None and number >= 0 else None


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) >= 10:
        candidate = text[:10]
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    if len(text) >= 8:
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return ""
    return ""


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _latest_as_of(*payloads: Mapping[str, Any]) -> str:
    candidates: list[str] = []
    for payload in payloads:
        for key in ("as_of", "generated_at", "updated_at", "last_trade_date"):
            value = str(payload.get(key) or "").strip()
            if value:
                candidates.append(value)
    return max(candidates) if candidates else ""


def _holding_days(
    item: Mapping[str, Any],
    *,
    as_of: str,
    calendar_days: Sequence[str],
) -> tuple[int | None, str]:
    entry = _date_text(item.get("entry_date") or item.get("buy_date") or item.get("added_at"))
    end = _date_text(item.get("latest_date") or item.get("latest_reference_date") or as_of)
    open_days = sorted({_date_text(day) for day in calendar_days if _date_text(day)})
    if entry and end and open_days:
        covered = [day for day in open_days if entry <= day <= end]
        if covered and covered[0] == entry:
            return max(0, len(covered) - 1), "a_share_calendar"
    supplied = _int_value(item.get("elapsed_trading_days"))
    if supplied is None:
        supplied = _int_value(item.get("holding_days"))
    return (supplied, "backend_source_field") if supplied is not None else (None, "missing")


def _account(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    summary = portfolio.get("summary") if isinstance(portfolio.get("summary"), Mapping) else {}

    def pick(name: str) -> float | None:
        value = summary.get(name) if isinstance(summary, Mapping) else None
        return _finite(value if value is not None else portfolio.get(name))

    account = {
        "valuation_date": _date_text(portfolio.get("valuation_date") or summary.get("valuation_date")),
        "initial_cash": pick("initial_cash"),
        "cash": pick("cash"),
        "market_value": pick("market_value"),
        "total_equity": pick("total_equity"),
        "total_return_pct": pick("total_return_pct"),
        "realized_pnl": pick("realized_pnl"),
        "unrealized_pnl": pick("unrealized_pnl"),
        "position_count": _int_value(
            summary.get("position_count") if isinstance(summary, Mapping) else portfolio.get("position_count")
        ),
    }
    required = ("initial_cash", "cash", "market_value", "total_equity", "total_return_pct")
    account["metric_status"] = "passed" if all(account[key] is not None for key in required) else "degraded"
    return account


def _position_rows(
    portfolio: Mapping[str, Any],
    *,
    as_of: str,
    calendar_days: Sequence[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in portfolio.get("positions") or []:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or "").strip()
        holding_days, holding_source = _holding_days(raw, as_of=as_of, calendar_days=calendar_days)
        pnl = _finite(raw.get("unrealized_pnl"))
        review_status = str(raw.get("review_status") or raw.get("management_state") or "").strip() or None
        review_reason = str(raw.get("review_reason") or raw.get("hold_reason") or "").strip() or None
        missing: list[str] = []
        if holding_days is None:
            missing.append("holding_days")
            warnings.append(f"position_holding_days_missing:{symbol or 'unknown'}")
        if pnl is None:
            missing.append("unrealized_pnl")
            warnings.append(f"position_unrealized_pnl_missing:{symbol or 'unknown'}")
        if review_status is None:
            missing.append("review_status")
            warnings.append(f"position_review_status_missing:{symbol or 'unknown'}")
        rows.append(
            {
                "symbol": symbol,
                "name": raw.get("name"),
                "entry_date": _date_text(raw.get("entry_date") or raw.get("buy_date")),
                "latest_date": _date_text(raw.get("latest_date") or as_of),
                "holding_days": holding_days,
                "holding_days_source": holding_source,
                "entry_price": _finite(raw.get("entry_price")),
                "historical_avg_cost": _finite(raw.get("avg_cost")),
                "cost_basis_label": "当前盈亏采用的账本成本；历史均价另列",
                "latest_price": _finite(raw.get("latest_price")),
                "quantity": _finite(raw.get("quantity")),
                "market_value": _finite(raw.get("market_value")),
                "unrealized_pnl": pnl,
                "unrealized_return_pct": _finite(
                    raw.get("unrealized_return_pct")
                    if raw.get("unrealized_return_pct") is not None
                    else raw.get("return_pct")
                ),
                "review_status": review_status,
                "review_label": raw.get("review_label") or review_status,
                "review_reason": review_reason,
                "strategy": raw.get("strategy") or raw.get("strategy_type"),
                "metric_status": "passed" if not missing else "degraded",
                "missing_fields": missing,
            }
        )
    return rows


def _watchlist_rows(
    watchlist: Mapping[str, Any],
    *,
    as_of: str,
    calendar_days: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in watchlist.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        holding_days, holding_source = _holding_days(raw, as_of=as_of, calendar_days=calendar_days)
        review_status = str(raw.get("review_status") or "").strip() or None
        return_pct = _finite(raw.get("return_since_added_pct"))
        missing = [
            name
            for name, value in (
                ("holding_days", holding_days),
                ("paper_return_pct", return_pct),
                ("review_status", review_status),
            )
            if value is None
        ]
        rows.append(
            {
                "symbol": str(raw.get("symbol") or "").strip(),
                "name": raw.get("name"),
                "entry_date": _date_text(raw.get("entry_date") or raw.get("added_at")),
                "latest_date": _date_text(raw.get("latest_reference_date") or as_of),
                "holding_days": holding_days,
                "holding_days_source": holding_source,
                "entry_price": _finite(raw.get("initial_reference_price") or raw.get("entry_price")),
                "latest_price": _finite(raw.get("latest_reference_price") or raw.get("latest_price")),
                "paper_return_pct": return_pct,
                "paper_pnl": None,
                "max_drawdown_pct": _finite(raw.get("max_drawdown_since_added")),
                "review_status": review_status,
                "review_label": raw.get("review_label") or review_status,
                "review_reason": raw.get("review_reason") or raw.get("expectation_fit_reason"),
                "strategy": raw.get("strategy") or raw.get("primary_action"),
                "metric_status": "passed" if not missing else "degraded",
                "missing_fields": missing,
            }
        )
    return rows


def _aligned_return_series(
    equity_history: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    *,
    calendar_days: Sequence[str] = (),
) -> dict[str, Any]:
    """Compare recorded equity and index points over one explicit date window."""

    evidence = equity_history.get("history_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}

    def unavailable(reason: str, message: str) -> dict[str, Any]:
        return {"status": "degraded", "available": False, "reason_code": reason,
                "benchmark_label": benchmark.get("label") or "benchmark", "items": [],
                "message_for_ui": message, "history_evidence": dict(evidence)}

    if evidence.get("invalid_count") or evidence.get("conflicting_dates") or evidence.get("structure_invalid"):
        return unavailable("paper_history_conflicting_or_invalid", "历史结构、金额或同日记录存在异常，暂不计算权益对照。")
    paper_by_date: dict[str, float] = {}
    invalid_paper = False
    undated = 0
    unconfirmed = set(evidence.get("unconfirmed_review_dates") or [])
    for raw in equity_history.get("items") or []:
        if not isinstance(raw, Mapping):
            invalid_paper = True
            continue
        # Public history items carry an explicit valuation date. Never use a
        # generation timestamp, even when a caller bypasses the usual reader.
        day = iso_date(raw.get("valuation_date") or raw.get("date"))
        if not day:
            undated += 1
            continue
        value = finite_number(raw.get("total_equity"))
        if value is None or value <= 0 or (day in paper_by_date and paper_by_date[day] != value):
            invalid_paper = True
        else:
            paper_by_date[day] = value
        if raw.get("portfolio_review_valid") is False:
            unconfirmed.add(day)
    if invalid_paper:
        return unavailable("paper_history_conflicting_or_invalid", "已记录权益缺失、无效或同日冲突，暂不计算对照。")
    if len(paper_by_date) < 2:
        return unavailable("insufficient_aligned_return_series", "明确估值日期的有效权益不足两条，暂不生成比较曲线。")

    start, end = min(paper_by_date), max(paper_by_date)
    benchmark_by_date: dict[str, float] = {}
    benchmark_conflicts: set[str] = set()
    for raw in benchmark.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        day = iso_date(raw.get("date"))
        if not day or not start <= day <= end:
            continue
        close = finite_number(raw.get("close"))
        if close is None or close <= 0 or (day in benchmark_by_date and benchmark_by_date[day] != close):
            benchmark_conflicts.add(day)
        else:
            benchmark_by_date[day] = close
    if benchmark_conflicts:
        return unavailable("benchmark_conflicting_or_invalid", "同一对照期间的指数记录无效或冲突，暂不计算比较曲线。")
    if start not in benchmark_by_date or end not in benchmark_by_date:
        return unavailable("benchmark_endpoint_missing", f"指数缺少与账面记录相同的起止日（{start} 至 {end}），不能缩短期间代替。")

    # Nulls are display gaps, not reconstructed observations. A known calendar
    # or saved index date exposes absent equity instead of connecting across it.
    dates = sorted(set(paper_by_date) | set(benchmark_by_date) |
                   {day for value in calendar_days if (day := iso_date(value)) and start <= day <= end})
    missing_paper = [day for day in dates if day not in paper_by_date]
    missing_benchmark = [day for day in dates if day not in benchmark_by_date]

    def change(value: float | None, base: float) -> float | None:
        result = finite_number((value / base - 1) * 100) if value is not None else None
        return round(result, 6) if result is not None else None

    items = [
        {
            "date": day,
            "paper_return_pct": change(paper_by_date.get(day), paper_by_date[start]),
            "benchmark_return_pct": change(benchmark_by_date.get(day), benchmark_by_date[start]),
        }
        for day in dates
    ]
    if any((day in paper_by_date and row["paper_return_pct"] is None) or
           (day in benchmark_by_date and row["benchmark_return_pct"] is None) for day, row in zip(dates, items)):
        return unavailable("comparison_not_finite", "权益或指数比例无法得到有效数字，暂不生成比较曲线。")
    undated += _int_value(evidence.get("undated_count")) or 0
    warnings = [reason for condition, reason in (
        (undated, "paper_history_valuation_dates_missing"),
        (missing_paper, "paper_observations_missing_in_period"),
        (missing_benchmark, "benchmark_observations_missing_in_period"),
        (unconfirmed, "paper_history_review_unconfirmed"),
        (evidence.get("duplicate_count"), "paper_history_duplicate_dates"),
    ) if condition]
    notes = [f"{start} 至 {end}，两条曲线均从共同首日归零；仅比较已记录权益与指数点位变化。"]
    if undated:
        notes.append(f"{undated} 条缺少估值日期的历史记录已排除。")
    if missing_paper:
        notes.append(f"缺权益记录 {len(missing_paper)} 天：{'、'.join(missing_paper)}；缺点未补。")
    if missing_benchmark:
        notes.append(f"缺指数记录 {len(missing_benchmark)} 天：{'、'.join(missing_benchmark)}。")
    if unconfirmed:
        notes.append(f"{len(unconfirmed)} 个估值日的复盘尚未确认。")
    notes.append("纸面 · 未验证；不代表完整核账后的策略收益。")
    return {
        "status": "degraded" if warnings else "passed",
        "available": True,
        "reason_code": "partial_recorded_history" if warnings else "",
        "benchmark_label": benchmark.get("label") or "benchmark",
        "items": items,
        "period_start": start,
        "period_end": end,
        "normalization": "common_first_recorded_equity_and_index",
        "observation_count": len(paper_by_date),
        "missing_valuation_dates": missing_paper,
        "missing_benchmark_dates": missing_benchmark,
        "warnings": warnings,
        "history_evidence": dict(evidence),
        "message_for_ui": " ".join(notes),
    }


def build_decision_board_snapshot(
    *,
    watchlist_payload: Mapping[str, Any],
    portfolio_payload: Mapping[str, Any],
    equity_history_payload: Mapping[str, Any],
    benchmark_payload: Mapping[str, Any],
    profile: ResearchPaperProfile,
    calendar_days: Sequence[str],
    strategy_version: str,
    source_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build one immutable, read-only decision-board response."""

    watchlist = dict(watchlist_payload or {})
    portfolio = dict(portfolio_payload or {})
    equity_history = dict(equity_history_payload or {})
    benchmark = dict(benchmark_payload or {})
    warnings: list[str] = []
    as_of = _latest_as_of(portfolio, watchlist, equity_history)
    account = _account(portfolio)
    if account["metric_status"] != "passed":
        warnings.append("paper_account_metrics_incomplete")
    positions = _position_rows(
        portfolio,
        as_of=as_of,
        calendar_days=calendar_days,
        warnings=warnings,
    )
    watch_rows = _watchlist_rows(watchlist, as_of=as_of, calendar_days=calendar_days)
    return_series = _aligned_return_series(equity_history, benchmark, calendar_days=calendar_days)
    if return_series["status"] != "passed":
        warnings.append(str(return_series["reason_code"]))

    profile_validation = validate_research_paper_profile(profile)
    if not profile_validation.get("ok"):
        warnings.append("research_paper_profile_invalid")
    profile_public = asdict(profile)
    for forbidden in ("broker_route", "target_price", "stop_loss_price"):
        profile_public.pop(forbidden, None)
    initial_cash = account.get("initial_cash")
    profile_public["capital_alignment"] = (
        "matched"
        if initial_cash is not None and abs(float(initial_cash) - float(profile.paper_capital)) <= 1e-9
        else "mismatch"
    )
    if profile_public["capital_alignment"] == "mismatch":
        warnings.append("profile_capital_mismatch")

    events = [dict(item) for item in portfolio.get("actions") or [] if isinstance(item, Mapping)]
    no_fill_count = sum(
        1
        for item in events
        if str(item.get("action") or "") == "no_fill" or str(item.get("fill_model") or "") == "missing_intraday"
    )
    summary = watchlist.get("paper_tracking_summary") if isinstance(watchlist.get("paper_tracking_summary"), Mapping) else {}
    review_count = _int_value(summary.get("needs_review_count")) or 0
    risk_status = "review_required" if review_count else "passed"
    sources = {
        key: {
            "path": str((source_paths or {}).get(key) or ""),
            "status": "available" if payload.get("ok", True) and payload else "missing",
        }
        for key, payload in (
            ("portfolio", portfolio),
            ("watchlist", watchlist),
            ("equity_history", equity_history),
            ("benchmark", benchmark),
        )
    }
    coverage = {
        "position_count": len(positions),
        "watchlist_count": len(watch_rows),
        "event_count": len(events),
        "aligned_return_point_count": sum(item["paper_return_pct"] is not None and item["benchmark_return_pct"] is not None
                                          for item in return_series.get("items") or []),
        "position_metric_passed_count": sum(1 for item in positions if item["metric_status"] == "passed"),
        "watchlist_metric_passed_count": sum(1 for item in watch_rows if item["metric_status"] == "passed"),
    }
    manifest_inputs = {
        "portfolio": portfolio,
        "watchlist": watchlist,
        "equity_history": equity_history,
        "benchmark": benchmark,
        "profile": profile_public,
        "strategy_version": strategy_version,
    }
    input_hashes = {key: _digest(value) for key, value in manifest_inputs.items()}
    manifest = {
        "manifest_id": _digest(input_hashes),
        "input_hashes": input_hashes,
        "deterministic": True,
        "read_only": True,
    }
    warnings = list(dict.fromkeys(item for item in warnings if item))
    envelope = {
        "as_of": {"value": as_of, "status": "available" if as_of else "missing", "source": "backend_artifacts"},
        "source": sources,
        "coverage": coverage,
        "strategy_version": {"value": strategy_version or "missing", "status": "available" if strategy_version else "missing"},
        "risk": {"status": risk_status, "review_count": review_count, "source": "backend_review_status"},
        "event": {"status": "available" if events else "missing", "count": len(events), "no_fill_count": no_fill_count},
        "profile": profile_public,
        "manifest": manifest,
    }
    status = "passed" if not warnings else "degraded"
    return {
        "ok": True,
        "status": status,
        "as_of": as_of,
        "account": account,
        "positions": positions,
        "watchlist": watch_rows,
        "watchlist_summary": dict(summary),
        "events": events,
        "return_series": return_series,
        "envelope": envelope,
        "warnings": warnings,
        "boundaries": {
            "badges": list(REQUIRED_BOUNDARY_BADGES),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "live_execution_locked": True,
            "qmt_order_route_locked": True,
            "not_investment_advice": True,
            "not_position_advice": True,
        },
    }


__all__ = ["REQUIRED_BOUNDARY_BADGES", "build_decision_board_snapshot"]
