"""Read-only accounting and evidence projections for the owner's dashboard.

No money, dates, historical prices or ledger records are repaired here. Missing
evidence stays missing. The caller supplies already loaded local artifacts.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from typing import Any, Mapping, Sequence

from quantdesk.paper.ledger_read_contract import day, number, strategy_pnl_identity

UNKNOWN_LABELS = {"", "unknown", "none", "null", "未知", "未知板块", "未知行业", "未知题材", "--"}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, (list, tuple)) else []


def _sum_known(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    values = [number(row.get(field)) for row in rows]
    return {"amount": round(sum(v for v in values if v is not None), 2) if any(v is not None for v in values) else None,
            "known_count": sum(v is not None for v in values), "total_count": len(rows),
            "complete": all(v is not None for v in values)}


def _open_dates(calendar: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(calendar, Mapping):
        return []
    dates = calendar.get("is_open_by_date", calendar)
    return sorted({day(key) for key, value in dates.items() if value is True and day(key)}) if isinstance(dates, Mapping) else []


def position_cost_evidence(position: Mapping[str, Any]) -> dict[str, Any]:
    """Identify the existing PnL cost without inventing a replacement basis."""
    entry, shown = number(position.get("entry_price")), number(position.get("avg_cost"))
    quantity, price, declared = (number(position.get(k)) for k in ("quantity", "latest_price", "unrealized_pnl"))
    computed = round((price - entry) * quantity, 2) if None not in (entry, quantity, price) else None
    delta = round(declared - computed, 2) if declared is not None and computed is not None else None
    return {
        "symbol": str(position.get("symbol") or ""), "pnl_cost_price": entry,
        "reported_avg_cost": shown, "reported_adjusted_avg_cost": number(position.get("adjusted_avg_cost")),
        "cost_source": "entry_price" if entry is not None else "missing",
        "cost_label": "当前浮盈亏使用成本（历史费用口径见记录）", "declared_unrealized_pnl": declared,
        "recomputed_unrealized_pnl": computed, "pnl_difference": delta,
        "display_cost_differs": entry is not None and shown is not None and abs(entry - shown) > 0.00005,
        "status": "missing" if computed is None or declared is None else ("mismatch" if abs(delta) > 0.01 else "consistent"),
        "buy_fee_included": position.get("entry_cost_basis_includes_buy_fee") if isinstance(position.get("entry_cost_basis_includes_buy_fee"), bool) else None,
        "read_only": True,
    }


def cost_basis_conflict_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    """Explain recorded PnL differences without selecting or rewriting a cost.

    A candidate must already match the recorded PnL under entry_price while
    disagreeing under the separately recorded avg_cost. Net sale proceeds
    already include the recorded sale fees; those fees are never deducted again.
    This field comparison does not establish original cash-flow completeness.
    """
    def decimal_number(value: Any) -> Decimal | None:
        if number(value) is None:
            return None
        try:
            return Decimal(str(value))
        except DecimalException:
            return None

    def money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def display(value: Decimal | None) -> float | None:
        return float(value) if value is not None else None

    items: list[dict[str, Any]] = []
    invalid_rows = 0
    collections_valid = True
    for group in ("positions", "closed_positions"):
        rows = state.get(group)
        if not isinstance(rows, list):
            collections_valid = False
            continue
        for index, record in enumerate(rows):
            if not isinstance(record, Mapping):
                invalid_rows += 1
                collections_valid = False
                continue
            quantity_field = "exit_quantity" if group == "closed_positions" and record.get("exit_quantity") is not None else "quantity"
            amount_field, pnl_field = ("market_value", "unrealized_pnl") if group == "positions" else ("net_amount", "realized_pnl")
            fields = {"quantity": quantity_field, "amount": amount_field, "pnl": pnl_field,
                      "entry_price": "entry_price", "avg_cost": "avg_cost"}
            values = {key: decimal_number(record.get(field)) for key, field in fields.items()}
            missing = [field for key, field in fields.items() if values[key] is None]
            if values["quantity"] is not None and values["quantity"] <= 0:
                missing.append(quantity_field + ":not_positive")
            for key in ("entry_price", "avg_cost"):
                if values[key] is not None and values[key] < 0:
                    missing.append(key + ":negative")
            entry_pnl = avg_pnl = delta = None
            entry_matches = avg_matches = None
            match = "unknown"
            if not missing:
                try:
                    with localcontext() as context:
                        context.prec = 40
                        entry_pnl = money(values["amount"] - values["entry_price"] * values["quantity"])
                        avg_pnl = money(values["amount"] - values["avg_cost"] * values["quantity"])
                        recorded = money(values["pnl"])
                        entry_matches, avg_matches = entry_pnl == recorded, avg_pnl == recorded
                        delta = money(avg_pnl - recorded)
                    match = ("matches_both" if entry_matches and avg_matches else "matches_entry_only" if entry_matches
                             else "matches_avg_only" if avg_matches else "matches_neither")
                except DecimalException:
                    missing.append("calculation_outside_supported_precision")
                    entry_pnl = avg_pnl = delta = None
                    entry_matches = avg_matches = None
            candidate = match == "matches_entry_only"
            items.append({"record_group": group, "source_index": index, "symbol": str(record.get("symbol") or ""),
                "entry_date": day(record.get("entry_date")) or None, "exit_date": day(record.get("exit_date")) or None,
                "source_fields": fields,
                "original_values": {"quantity": number(record.get(quantity_field)), "entry_price": number(record.get("entry_price")),
                    "avg_cost": number(record.get("avg_cost")), "amount": number(record.get(amount_field)),
                    "recorded_pnl": number(record.get(pnl_field))},
                "pnl_using_entry_price": display(entry_pnl), "pnl_using_avg_cost": display(avg_pnl),
                "alternative_pnl_delta": display(delta), "entry_matches_recorded": entry_matches,
                "avg_matches_recorded": avg_matches, "match_status": match,
                "explanation_candidate": candidate, "missing_fields": missing,
                "net_proceeds_include_recorded_exit_fees": group == "closed_positions",
                "read_only": True})
    candidates = [item for item in items if item["explanation_candidate"]]
    identity = strategy_pnl_identity(dict(state))
    reported = decimal_number(identity.get("difference"))
    candidate_sum = remainder = None
    try:
        with localcontext() as context:
            context.prec = 50
            candidate_sum = money(sum((Decimal(str(item["alternative_pnl_delta"])) for item in candidates), Decimal(0)))
            remainder = money(reported - candidate_sum) if reported is not None else None
    except DecimalException:
        collections_valid = False
    explains = bool(collections_valid and candidates and reported is not None and reported != 0 and remainder == 0)
    status = ("unavailable" if reported is None or not collections_valid else "explains_reported_difference" if explains
              else "partial_explanation" if candidates else "no_matching_conflicts")
    label = {"unavailable": "成本字段诊断资料不足", "explains_reported_difference": "成本字段冲突解释了报告差额",
             "partial_explanation": "成本字段冲突尚未完整解释报告差额", "no_matching_conflicts": "未发现匹配条件的成本差额解释"}[status]
    return {"status": status, "status_label": label, "reported_pnl_difference": display(reported),
        "candidate_adjustment_sum": display(candidate_sum) if items or collections_valid else None,
        "remaining_reported_difference": display(remainder), "explains_reported_difference": explains,
        "candidate_count": len(candidates), "candidates": candidates, "items": items,
        "unknown_count": sum(item["match_status"] == "unknown" for item in items), "invalid_record_count": invalid_rows,
        "source": "recorded positions and closed_positions; market_value or net_amount minus the recorded cost times quantity",
        "comparison_policy": "ROUND_HALF_EVEN to cents; candidate recorded PnL must match entry_price and differ from avg_cost",
        "scope": "known_cost_fields_only", "read_only": True, "authoritative_pnl_replaced": False,
        "full_historical_cashflow_verified": False,
        "message": label + "；这不替代原账本盈亏，完整历史现金流仍未核验。"}


def holding_period_evidence(record: Mapping[str, Any], *, calendar: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Recompute a display duration from declared dates, leaving raw fields intact."""
    entry, exit_day = day(record.get("entry_date")), day(record.get("exit_date"))
    valid = bool(entry and exit_day and entry <= exit_day)
    opens = _open_dates(calendar)
    covered = bool(valid and opens and opens[0] <= entry and opens[-1] >= exit_day)
    elapsed = (date.fromisoformat(exit_day) - date.fromisoformat(entry)).days if valid else None
    trading_days = sum(entry < value <= exit_day for value in opens) if covered else None
    return {"symbol": str(record.get("symbol") or ""), "entry_date": entry, "exit_date": exit_day,
            "recorded_holding_days": number(record.get("holding_days")), "elapsed_calendar_days": elapsed,
            "elapsed_trading_days": trading_days, "status": "derived_from_local_calendar" if covered else ("calendar_unverified" if valid else "invalid_or_missing_dates"),
            "recorded_zero_conflicts_with_dates": number(record.get("holding_days")) == 0 and elapsed is not None and elapsed > 0,
            "read_only": True}


def history_coverage(history: Mapping[str, Any] | Sequence[Any] | None, *, calendar: Mapping[str, Any] | None = None,
                     created_at: Any = "", target_date: Any = "") -> dict[str, Any]:
    """Describe dated evidence coverage; never fill missing equity observations."""
    rows = _rows(history.get("items", [])) if isinstance(history, Mapping) else _rows(history)
    row_dates = [day(row.get("valuation_date") or row.get("date") or row.get("generated_at")) for row in rows]
    counts = Counter(value for value in row_dates if value)
    start = min(counts) if counts else ""
    end = day(target_date) or (max(counts) if counts else "")
    opens = _open_dates(calendar)
    covered = bool(start and end and start <= end and opens and opens[0] <= start and opens[-1] >= end)
    expected = [value for value in opens if start <= value <= end] if covered else []
    missing = [value for value in expected if value not in counts]
    duplicate = sorted(value for value, count in counts.items() if count > 1)
    malformed = sum(not value for value in row_dates)
    malformed_rows = len(history.get("items", [])) - len(rows) if isinstance(history, Mapping) and isinstance(history.get("items"), list) else len(history) - len(rows) if isinstance(history, (list, tuple)) else 0
    after_target = sorted(value for value in counts if end and value > end)
    nontrading = sorted(value for value in counts if covered and start <= value <= end and value not in expected)
    equity_missing = [row_dates[i] or f"row:{i}" for i, row in enumerate(rows) if number(row.get("total_equity")) is None]
    recovered = [row_dates[i] for i, row in enumerate(rows) if str(row.get("mode") or "").lower().startswith(("recover", "valuation")) or row.get("portfolio_review_valid") is False]
    complete = bool(rows and covered and not missing and not duplicate and not malformed and not malformed_rows and not after_target and not nontrading and not equity_missing and not recovered)
    return {"status": "complete" if complete else ("partial" if rows else "missing"),
            "row_count": len(rows), "unique_dates": len(counts), "first_date": start, "target_date": end,
            "account_created_date": day(created_at), "before_first_history_unverified": bool(day(created_at) and start and day(created_at) < start),
            "calendar_verified_for_interval": covered, "expected_trading_days": len(expected) if covered else None,
            "missing_dates": missing, "duplicate_dates": duplicate, "invalid_date_rows": malformed,
            "invalid_record_rows": malformed_rows, "dates_after_target": after_target,
            "nontrading_dates": nontrading, "missing_equity_dates": equity_missing, "recovery_or_incomplete_review_dates": recovered,
            "continuous_statistics_eligible": complete, "missing_values_filled": False, "read_only": True}


def _original_text(record: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    for key in fields:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return {"status": "recorded", "text": value.strip(), "source_field": key}
    return {"status": "unknown", "text": None, "source_field": None}


def _judgment_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose existing review claims without deriving truth from monetary PnL."""
    for key in ("judgment_review", "hypothesis_validation", "prediction_review", "validation_result"):
        value = record.get(key)
        if isinstance(value, Mapping):
            return {"status": "recorded_unverified_here", "source_field": key,
                    "recorded_status": value.get("status") or value.get("result"),
                    "recorded_verified_flag": value.get("verified") if isinstance(value.get("verified"), bool) else None,
                    "recorded_at": value.get("reviewed_at") or value.get("validated_at"),
                    "independently_verified_here": False, "inferred_from_pnl": False}
        if isinstance(value, str) and value.strip():
            return {"status": "recorded_unverified_here", "source_field": key, "recorded_status": value,
                    "recorded_verified_flag": None, "recorded_at": None,
                    "independently_verified_here": False, "inferred_from_pnl": False}
    return {"status": "not_assessed", "source_field": None, "recorded_status": None,
            "recorded_verified_flag": None, "recorded_at": None,
            "independently_verified_here": False, "inferred_from_pnl": False}


def weekly_learning_review(closed_positions: Any, *, calendar: Mapping[str, Any] | None = None,
                           as_of: Any = "") -> dict[str, Any]:
    """Group original closed trades by ISO week, keeping missing hypotheses visible."""
    rows = _rows(closed_positions)
    original_indices = {id(record): index for index, record in enumerate(closed_positions) if isinstance(record, Mapping)} if isinstance(closed_positions, (list, tuple)) else {}
    cutoff = day(as_of)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    unassigned = []
    for index, record in enumerate(rows):
        exit_day = day(record.get("exit_date"))
        if not exit_day or (cutoff and exit_day > cutoff):
            unassigned.append({"source_row": original_indices.get(id(record), index), "symbol": str(record.get("symbol") or ""),
                               "exit_date": exit_day or None, "realized_pnl": number(record.get("realized_pnl")),
                               "reason": "exit_date_after_as_of" if exit_day else "exit_date_missing_or_invalid"})
            continue
        exit_date = date.fromisoformat(exit_day)
        monday = (exit_date - timedelta(days=exit_date.weekday())).isoformat()
        groups.setdefault(monday, []).append(record)
    weeks = []
    total_zero_conflicts = 0
    for monday, trades in sorted(groups.items(), reverse=True):
        monday_date = date.fromisoformat(monday)
        iso = monday_date.isocalendar()
        holds = [holding_period_evidence(row, calendar=calendar) for row in trades]
        zero_conflicts = sum(row["recorded_zero_conflicts_with_dates"] for row in holds)
        total_zero_conflicts += zero_conflicts
        reason_rows = [_original_text(row, ("entry_reason", "original_entry_reason")) for row in trades]
        invalidation_rows = [_original_text(row, ("invalidation_condition", "falsification_condition", "failure_condition", "invalid_if", "stop_condition")) for row in trades]
        judgements = [_judgment_evidence(row) for row in trades]
        fees = _sum_known([{"total_fee": number(row.get("total_fee")) if (number(row.get("total_fee")) is not None and number(row.get("total_fee")) >= 0) else None} for row in trades], "total_fee")
        coverage = {"entry_reason_known": sum(row["status"] == "recorded" for row in reason_rows),
            "invalidation_condition_known": sum(row["status"] == "recorded" for row in invalidation_rows),
            "price_evidence_present": sum(isinstance(row.get("price_evidence"), Mapping) and bool(row["price_evidence"]) for row in trades),
            "plan_id_present": sum(bool(row.get("plan_id")) for row in trades),
            "judgment_review_recorded": sum(row["status"] != "not_assessed" for row in judgements),
            "total_count": len(trades), "presence_is_not_verification": True}
        examples = []
        losses = sorted([(index, row) for index, row in enumerate(trades) if number(row.get("realized_pnl")) is not None and number(row["realized_pnl"]) < 0], key=lambda item: number(item[1]["realized_pnl"]))[:3]
        for index, record in losses:
            examples.append({"symbol": str(record.get("symbol") or ""), "name": str(record.get("name") or ""),
                "entry_date": day(record.get("entry_date")) or None, "exit_date": day(record.get("exit_date")),
                "realized_pnl": number(record.get("realized_pnl")), "original_entry_reason": reason_rows[index],
                "invalidation_condition": invalidation_rows[index],
                "original_exit_reason": _original_text(record, ("exit_reason",)),
                "holding_period": holds[index], "judgment_evidence": judgements[index],
                "review_note": "亏损仅用于选择复盘案例，不代表原判断已被判错；不得事后补写事前理由。"})
        checks = []
        if zero_conflicts:
            checks.append(f"核对{zero_conflicts}笔记录为0天但日期跨日的持有期；不要据此归纳超短线表现。")
        if not fees["complete"]:
            checks.append(f"本周{len(trades) - fees['known_count']}笔缺少有效费用记录，费用统计只覆盖已知部分。")
        if coverage["invalidation_condition_known"] < len(trades):
            checks.append(f"本周{len(trades) - coverage['invalidation_condition_known']}笔缺少原证伪条件，先查原计划，不把退出理由倒写成预判。")
        if coverage["entry_reason_known"] < len(trades):
            checks.append(f"本周{len(trades) - coverage['entry_reason_known']}笔缺少原入场理由，保持未知，不补造解释。")
        if coverage["judgment_review_recorded"] < len(trades):
            checks.append("逐笔把原假设与后续事实对照；当前只有盈亏的交易仍是判断未评估。")
        weeks.append({"week_id": f"{iso.year}-W{iso.week:02}", "week_start": monday,
            "week_end": (monday_date + timedelta(days=6)).isoformat(), "closed_trade_count": len(trades),
            "realized_pnl": _sum_known(trades, "realized_pnl"), "recorded_exit_fees": fees,
            "holding_period": {"zero_day_conflicts": zero_conflicts,
                "missing_or_invalid_dates": sum(row["status"] == "invalid_or_missing_dates" for row in holds),
                "calendar_unverified_count": sum(row["status"] == "calendar_unverified" for row in holds),
                "negative_recorded_days": sum((row["recorded_holding_days"] is not None and row["recorded_holding_days"] < 0) for row in holds)},
            "evidence_coverage": coverage, "top_loss_examples": examples, "follow_up_checks": checks,
            "fee_note": "已计入盈亏的费用不再次扣除；损益贡献不是因果归因。",
            "outcome_is_judgment": False})
    malformed = len(closed_positions) - len(rows) if isinstance(closed_positions, (list, tuple)) else None
    partial = bool(unassigned or malformed or any(not week["realized_pnl"]["complete"] or not week["recorded_exit_fees"]["complete"]
        or week["holding_period"]["zero_day_conflicts"]
        or any(week["evidence_coverage"][key] < week["closed_trade_count"] for key in ("invalidation_condition_known", "entry_reason_known", "price_evidence_present", "judgment_review_recorded")) for week in weeks))
    return {"summary": f"原记录{len(rows)}笔平仓，按真实平仓日分{len(weeks)}周回顾；{total_zero_conflicts}笔持有期字段与日期冲突。盈亏不等于判断对错。",
            "checks": ["先看每周前三个亏损案例的原理由、证伪条件与已有核验，缺失保持未知。",
                       "把持有期异常、费用与证据缺口分别核对；收益正负不自动成为判断对错。",
                       "复盘只读原记录，不事后补预测，不自动修改策略或参数。"],
            "weekly_review": {"as_of": cutoff or None, "status": "partial" if partial else ("available" if weeks else "missing"),
                "source": "closed_positions.exit_date", "week_count": len(weeks), "weeks": weeks,
                "undated_or_future_records": unassigned, "invalid_record_rows": malformed,
                "read_only": True, "strategy_changes": False, "judgment_inferred_from_pnl": False}}


def build_product_integrity(state: Mapping[str, Any], history: Mapping[str, Any] | Sequence[Any] | None = None,
                            calendar: Mapping[str, Any] | None = None,
                            ai_ledgers: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build explicit evidence gaps and loss contributions from passed artifacts."""
    positions, closed, reductions = (_rows(state.get(key)) for key in ("positions", "closed_positions", "realized_reductions"))
    collections = {key: {"valid": isinstance(state.get(key), list) and all(isinstance(row, Mapping) for row in state[key]),
        "invalid_rows": sum(not isinstance(row, Mapping) for row in state[key]) if isinstance(state.get(key), list) else None}
        for key in ("positions", "closed_positions", "realized_reductions")}
    identity = strategy_pnl_identity(dict(state))
    cost_explanation = cost_basis_conflict_evidence(state)
    held_costs = [number(row.get("entry_price")) * number(row.get("quantity"))
                  for row in positions if number(row.get("entry_price")) is not None and number(row.get("quantity")) is not None]
    cash, initial, realized = (number(state.get(key)) for key in ("cash", "initial_cash", "realized_pnl"))
    bridge = None
    if collections["positions"]["valid"] and len(held_costs) == len(positions) and None not in (cash, initial, realized):
        bridge = round(cash + sum(held_costs) - initial - realized, 2)
    closed_sum = _sum_known(closed, "realized_pnl")
    if not collections["closed_positions"]["valid"]:
        closed_sum.update(amount=None, complete=False)
    reduction_field = "realized_pnl" if all("realized_pnl" in row for row in reductions) else "pnl"
    reduction_sum = _sum_known(reductions, reduction_field)
    if not collections["realized_reductions"]["valid"]:
        reduction_sum.update(amount=None, complete=False)
    legacy = number(state.get("legacy_realized_adjustment"))
    # Empty collections have a known zero contribution; missing individual
    # fields do not. An absent legacy adjustment is not silently assumed zero.
    known_closed = (0.0 if not closed else closed_sum["amount"]) if closed_sum["complete"] else None
    known_reductions = (0.0 if not reductions else reduction_sum["amount"]) if reduction_sum["complete"] else None
    components = round(known_closed + known_reductions + legacy, 2) if None not in (known_closed, known_reductions, legacy) else None
    costs = [position_cost_evidence(row) for row in positions]
    holding = [holding_period_evidence(row, calendar=calendar) for row in closed]
    exposures: dict[str, dict[str, Any]] = {}
    unknown_sectors = []
    adjustment_unknown, adjustment_stale = [], []
    for row in positions:
        sector = next((str(row.get(key)).strip() for key in ("sector", "industry") if str(row.get(key) or "").strip().lower() not in UNKNOWN_LABELS), "")
        symbol, value = str(row.get("symbol") or ""), number(row.get("market_value"))
        if not sector:
            unknown_sectors.append(symbol)
        else:
            exposure = exposures.setdefault(sector, {"label": sector, "market_value": 0.0, "symbols": [], "unpriced_symbols": []})
            exposure["symbols"].append(symbol)
            if value is None:
                exposure["unpriced_symbols"].append(symbol)
            else:
                exposure["market_value"] = round(exposure["market_value"] + value, 2)
        adj_status = str(row.get("price_adjustment_status") or "")
        if "stale" in adj_status:
            adjustment_stale.append(symbol)
        elif not adj_status or adj_status in {"unknown", "missing"}:
            adjustment_unknown.append(symbol)
    warnings = [str(value) for value in state.get("warnings", [])] if isinstance(state.get("warnings"), list) else []
    historical_prefixes = ("paper_position_status_mismatch_reconciled:", "paper_account_top_level_drift_fields:", "paper_account_top_level_drift_detected", "paper_account_legacy_realized_adjustment_preserved")
    warning_history = [value for value in warnings if value.startswith(historical_prefixes)]
    warning_current = [value for value in warnings if value not in warning_history]
    ai_evidence = {}
    for profile, ledger in (ai_ledgers or {}).items():
        if not isinstance(ledger, Mapping):
            ai_evidence[str(profile)] = {"status": "invalid", "trades": None, "price_evidence_known": None,
                                        "archive_search_exhaustive": False}
            continue
        trades = _rows(ledger.get("trades"))
        missing_price = [i for i, row in enumerate(trades) if not isinstance(row.get("price_evidence"), Mapping) or not row["price_evidence"]]
        missing_plan = [i for i, row in enumerate(trades) if not row.get("plan_id")]
        ai_evidence[str(profile)] = {"trades": len(trades), "price_evidence_known": len(trades) - len(missing_price),
            "missing_price_evidence_indices": missing_price, "missing_plan_id_indices": missing_plan,
            "status": "complete" if trades and not missing_price and not missing_plan else ("partial" if trades else "missing"),
            "archive_search_exhaustive": False}
    unrealized_sum = _sum_known(positions, "unrealized_pnl")
    if not collections["positions"]["valid"]:
        unrealized_sum.update(amount=None, complete=False)
    fee_rows = [{"total_fee": row.get("total_fee")} for row in closed]
    return {
        "read_only": True, "label": "纸面 · 未验证", "valuation_date": day(state.get("valuation_date")),
        "collection_validity": collections,
        "pnl_identity": identity,
        "capital_cost_bridge": {"difference": bridge, "held_entry_cost": round(sum(held_costs), 2) if collections["positions"]["valid"] and len(held_costs) == len(positions) else None,
            "definition": "cash + sum(entry_price * quantity) - initial_cash - realized_pnl",
            "status": "unverified" if bridge is None else ("mismatch" if abs(bridge) > 0.01 else "consistent"),
            "source_of_difference": "cost_field_conflicts_explain_reported_pnl_difference" if cost_explanation["explains_reported_difference"] else ("unresolved" if bridge is None or abs(bridge) > 0.01 else "none"),
            "message": cost_explanation["message"] if cost_explanation["explains_reported_difference"] else "未以调整项抹平差额；历史全部资金流水尚未由此投影重建。",
            "cost_basis_explanation": cost_explanation},
        "realized_components": {"closed": known_closed, "reductions": known_reductions, "legacy_adjustment": legacy,
            "sum": components, "declared": realized, "difference": round(realized - components, 2) if realized is not None and components is not None else None},
        "position_costs": costs,
        "history": history_coverage(history, calendar=calendar, created_at=state.get("created_at"), target_date=state.get("valuation_date")),
        "closed_holding_periods": holding, "closed_zero_holding_conflicts": sum(row["recorded_zero_conflicts_with_dates"] for row in holding),
        "industry_coverage": {"positions": len(positions), "known": len(positions) - len(unknown_sectors), "unknown_symbols": unknown_sectors,
            "source_policy": "reported sector, falling back to reported industry; taxonomy not normalized", "exposures": list(exposures.values())},
        "corporate_action_evidence": {"stale_symbols": adjustment_stale, "unknown_symbols": adjustment_unknown,
            "complete_total_return_proven": False, "message": "原始收盘估值一致不证明分红送转与总回报证据完整。"},
        "loss_contribution": {"closed_realized": closed_sum, "held_unrealized": unrealized_sum,
            "legacy_adjustment": legacy, "partial_realized": reduction_sum, "recorded_exit_fees": _sum_known(fee_rows, "total_fee"),
            "reconciled_total": identity.get("status") == "consistent" and all(value["valid"] for value in collections.values())
                and closed_sum["complete"] and reduction_sum["complete"] and unrealized_sum["complete"]
                and all(row["status"] == "consistent" for row in costs),
            "unexplained_difference": identity.get("difference"),
            "causal_attribution": False, "fee_note": "费用仅作记录覆盖统计，已计入盈亏的费用不得再扣一次。",
            "top_losses": sorted([{"symbol": str(row.get("symbol") or ""), "exit_date": day(row.get("exit_date")), "realized_pnl": number(row.get("realized_pnl"))}
                                  for row in closed if number(row.get("realized_pnl")) is not None], key=lambda row: row["realized_pnl"])[:5]},
        "warnings": {"current": warning_current, "historical_reconciliation_records": warning_history,
                     "current_count": len(warning_current), "historical_count": len(warning_history)},
        "ai_evidence_coverage": ai_evidence,
        "learning": weekly_learning_review(state.get("closed_positions"), calendar=calendar, as_of=state.get("valuation_date")),
    }
