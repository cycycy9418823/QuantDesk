"""Evidence requirements for model accuracy; never changes a paper ledger.

A plan being accepted is not a fill. A derived price threshold is not a model
prediction. Published historical verdicts remain inspectable even when these
requirements exclude them from the model-accuracy denominator.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone, timedelta
from math import isfinite
from typing import Any

from quantdesk.trading_calendar import is_trade_day

SCHEMA = "ai_decision_evaluation.v1"
TZ_SHANGHAI = timezone(timedelta(hours=8))
EXCLUSION_LABELS = {
    "historical_review": "事后整理记录",
    "origin_not_model": "未证实为模型逐项判断",
    "model_identity_missing": "缺实际模型名称",
    "unaccepted": "提议未接收",
    "statement_unverified": "包含未核验或不符陈述",
    "verification_not_model_declared": "验证条件非模型事前声明",
    "verification_invalid": "验证条件缺失或不完整",
    "original_timestamp_missing": "缺带时区的原始记录时间",
    "declaration_not_before_window": "记录时间不早于观察期开盘",
    "filled_trade_unconfirmed": "未确认实际纸面成交",
    "verdict_before_maturity": "原判定早于观察期结束",
    "outcome_evidence_incomplete": "到期日线证据不完整",
    "decision_identity_missing": "缺原始判断或账户计划身份",
    "duplicate_decision_conflict": "同一判断编号有冲突记录",
}


def declared_verification(value: Any) -> dict[str, Any] | None:
    """Accept explicit raw-price conditions without inferring missing fields."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    sessions = value.get("n_sessions")
    level = value.get("level")
    if kind not in {"stand_above", "break_below"} or value.get("price_basis") != "raw":
        return None
    if isinstance(sessions, bool) or not isinstance(sessions, int) or not 1 <= sessions <= 750:
        return None
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    if not isfinite(level) or level <= 0:
        return None
    return {"kind": kind, "n_sessions": sessions, "level": float(level), "price_basis": "raw"}


def decision_origin(row: dict[str, Any]) -> str:
    """A successful plan-level model call alone cannot prove an intent's origin."""
    origin = str(row.get("decision_origin") or "")
    if origin == "model" and row.get("llm_used") is True:
        return "model"
    if origin == "rule" or row.get("llm_used") is False:
        return "rule"
    return "unknown"


def _positive_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and isfinite(value) and value > 0)


def _calendar_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _confirmed_fill(row: dict[str, Any]) -> bool:
    proof = row.get("fill_evidence")
    if not isinstance(proof, dict) or proof.get("status") != "confirmed" or proof.get("ledger_id") != row.get("ledger_id"):
        return False
    trades = proof.get("trades")
    if not isinstance(trades, list) or not trades:
        return False
    identity = {"plan_id": row.get("plan_id"), "symbol": row.get("symbol"),
                "side": row.get("side"), "date": row.get("trade_date"), "price_basis": "raw"}
    for trade in trades:
        if (not isinstance(trade, dict) or any(trade.get(key) != value for key, value in identity.items())
                or not _positive_number(trade.get("quantity")) or not float(trade["quantity"]).is_integer()
                or not _positive_number(trade.get("price")) or not isinstance(trade.get("evidence_id"), str)
                or not trade["evidence_id"].strip()):
            return False
    return True


def with_fill_evidence(records: list[dict[str, Any]], ledger: dict[str, Any] | None,
                       *, ledger_status: str = "ok") -> list[dict[str, Any]]:
    """Join booked trades by exact plan/symbol/side/day, never by resemblance."""
    output = []
    book = ledger or {}
    ledger_id = str(book.get("ledger_id") or "")
    trades = [item for item in book.get("trades", []) if isinstance(item, dict)]
    for original in records:
        row = deepcopy(original)
        plan_id = str(row.get("plan_id") or "")
        symbol, side = str(row.get("symbol") or ""), str(row.get("side") or "")
        day = str(row.get("trade_date") or "")
        proof: dict[str, Any] = {"status": "unconfirmed", "reason": "no_matching_booked_trade",
                                 "ledger_id": ledger_id, "trades": []}
        if ledger is None or ledger_status != "ok":
            proof["reason"] = f"ledger_{ledger_status}"
        elif not isinstance(row.get("ledger_id"), str) or not row["ledger_id"].strip():
            proof["reason"] = "exact_identity_missing"
        elif row["ledger_id"] != ledger_id:
            proof["reason"] = "ledger_identity_mismatch"
        elif not ledger_id or not plan_id or not symbol or side not in {"buy", "sell"} or _calendar_date(day) is None:
            proof["reason"] = "exact_identity_missing"
        else:
            matched = []
            for trade in trades:
                if (str(trade.get("plan_id") or ""), str(trade.get("symbol") or ""),
                    str(trade.get("side") or ""), str(trade.get("date") or "")) != (plan_id, symbol, side, day):
                    continue
                evidence = trade.get("price_evidence") if isinstance(trade.get("price_evidence"), dict) else {}
                quantity = trade.get("quantity")
                if (not _positive_number(quantity) or not float(quantity).is_integer()
                        or not _positive_number(trade.get("price"))
                        or ("ledger_id" in trade and trade["ledger_id"] != ledger_id)):
                    continue
                # A receipt in a plan is insufficient: this row must be in the
                # booked ledger, with original raw-price evidence for this fill.
                if (evidence.get("price_basis") != "raw" or evidence.get("trade_date") != day
                        or evidence.get("side") != side or evidence.get("price") != trade.get("price")
                        or not _positive_number(evidence.get("price"))
                        or not isinstance(evidence.get("evidence_id"), str) or not evidence["evidence_id"].strip()):
                    continue
                identity = {"ledger_id": ledger_id, "plan_id": plan_id, "symbol": symbol, "quantity": quantity}
                if (any(key in evidence and evidence[key] != value for key, value in identity.items())
                        or ("quantity" in evidence and not _positive_number(evidence["quantity"]))):
                    continue
                matched.append({"date": day, "plan_id": plan_id, "symbol": symbol, "side": side,
                                "quantity": trade["quantity"], "price": trade["price"],
                                "evidence_id": evidence["evidence_id"], "price_basis": "raw"})
            if matched:
                proof.update(status="confirmed", reason="booked_ledger_trade", trades=matched,
                             quantity=sum(item["quantity"] for item in matched))
        row["fill_evidence"] = proof
        output.append(row)
    return output


def eligibility(row: dict[str, Any]) -> dict[str, Any]:
    """Prerequisites for an accuracy sample, independent of whether it won."""
    reasons: list[str] = []
    if (any(not isinstance(row.get(key), str) or not row[key].strip()
            for key in ("decision_id", "ledger_id", "plan_id", "symbol"))
            or row.get("side") not in {"buy", "sell"} or _calendar_date(row.get("trade_date")) is None):
        reasons.append("decision_identity_missing")
    if row.get("backfill") or row.get("provenance") == "historical_review":
        reasons.append("historical_review")
    if decision_origin(row) != "model":
        reasons.append("origin_not_model")
    if not str(row.get("llm_model") or "").strip():
        reasons.append("model_identity_missing")
    if row.get("committed") is not True:
        reasons.append("unaccepted")
    if (row.get("statement_flags") or row.get("statement_unverified")
            or row.get("result") in {"未核验", "陈述与数据不符"}):
        reasons.append("statement_unverified")
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    if verification.get("source") != "model_declared":
        reasons.append("verification_not_model_declared")
    if declared_verification(verification) is None:
        reasons.append("verification_invalid")
    try:
        recorded = datetime.fromisoformat(str(row.get("recorded_at") or ""))
        if recorded.tzinfo is None or row.get("recorded_at_source") != "original":
            raise ValueError("original timezone-aware timestamp required")
        start = datetime.combine(datetime.strptime(str(row.get("trade_date") or ""), "%Y-%m-%d").date(),
                                 time(9, 30), tzinfo=TZ_SHANGHAI)
        if recorded >= start:
            reasons.append("declaration_not_before_window")
    except (ValueError, TypeError):
        reasons.append("original_timestamp_missing")
    if not _confirmed_fill(row):
        reasons.append("filled_trade_unconfirmed")
    return {"schema": SCHEMA, "eligible": not reasons, "exclusion_reasons": reasons,
            "exclusion_labels": [EXCLUSION_LABELS[reason] for reason in reasons]}


def complete_outcome_evidence(row: dict[str, Any]) -> bool:
    """Require the full declared observation window, even for an early hit."""
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    spec = declared_verification(verification)
    start, due = _calendar_date(row.get("trade_date")), _calendar_date(verification.get("due_date"))
    if spec is None or start is None or due is None or due < start:
        return False
    expected = []
    cursor = start
    # Match the evaluator's bounded trading-calendar window, without accepting
    # an arbitrary set of equally numerous dates as the declared observation.
    for _ in range(800):
        if is_trade_day(cursor):
            expected.append(cursor.isoformat())
        if cursor >= due or len(expected) > spec["n_sessions"]:
            break
        cursor += timedelta(days=1)
    if len(expected) != spec["n_sessions"] or not expected or expected[-1] != due.isoformat():
        return False
    evidence_rows = row.get("result_evidence")
    if not isinstance(evidence_rows, list):
        return False
    for evidence in evidence_rows:
        if not isinstance(evidence, dict) or evidence.get("kind") not in {"window_hit", "window_miss"}:
            continue
        observed = evidence.get("observed")
        if (evidence.get("price_basis") != "raw" or evidence.get("due_date") != due.isoformat()
                or evidence.get("level") != spec["level"] or not _positive_number(evidence.get("level"))
                or not isinstance(observed, (list, tuple)) or len(observed) != len(expected)):
            continue
        if not all(isinstance(item, (list, tuple)) and len(item) == 2
                   and item[0] == day and _positive_number(item[1]) for item, day in zip(observed, expected)):
            continue
        hit = any(close < spec["level"] if spec["kind"] == "break_below" else close >= spec["level"]
                  for _, close in observed)
        if (evidence["kind"] == ("window_hit" if hit else "window_miss")
                and row.get("result") == ("对" if hit else "错")):
            return True
    return False
