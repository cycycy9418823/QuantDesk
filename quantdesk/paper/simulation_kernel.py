"""Deterministic, paper-only execution simulation kernel.

The kernel accepts only already-approved paper intents, content-bound local
evidence and a verified completed minute pool.  It never opens a network
connection, routes an order or mutates canonical market data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Mapping

import pandas as pd

from quantdesk.intraday.intraday_schema import (
    build_intraday_quality_gate,
    filter_intraday_trading_session,
    normalize_intraday_frame,
    source_time_order_state,
)
from quantdesk.intraday.minute_pool_capability import minute_pool_capability_source_hash
from quantdesk.paper.market_rules import (
    board_type_for_symbol,
    is_valid_order_quantity,
    lot_size_for_symbol,
    paper_only_boundary,
    paper_trade_block_reason,
)
from quantdesk.paper.portfolio_contracts import (
    PaperOrderIntent,
    PortfolioSnapshot,
    compute_paper_order_intent_digest,
)
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.paper.trade_costs import TRADE_COST_MODEL, compute_trade_costs
from quantdesk.risk.risk_gate import RiskGateDecision, validate_risk_gate_decision_binding
from quantdesk.price_basis import classify_frame_price_basis
from quantdesk.shared import normalize_symbol
from quantdesk.version import API_SCHEMA_VERSION


@dataclass(frozen=True)
class ExecutionEvidence:
    symbol: str
    execution_date: str
    price_basis: str
    minute_frame: pd.DataFrame = field(compare=False, repr=False)
    minute_source_path: str = ""
    minute_source_hash: str = ""
    minute_pool: Mapping[str, Any] = field(default_factory=dict)
    daily_history: Mapping[str, Any] = field(default_factory=dict)
    trading_calendar: Mapping[str, Any] = field(default_factory=dict)
    market_metadata: Mapping[str, Any] = field(default_factory=dict)
    evidence_digest: str = ""


@dataclass(frozen=True)
class SettlementPolicy:
    execution_model: str = "next_open"
    price_basis: str = "raw"
    strict_t_plus_one: bool = True
    verified_minute_pool_required: bool = True
    verified_trading_calendar_required: bool = True
    fee_model: str = TRADE_COST_MODEL
    policy_version: str = "paper_execution_kernel_v1"


@dataclass(frozen=True)
class FillEvent:
    event_id: str
    content_digest: str
    intent_id: str
    intent_digest: str
    risk_decision_digest: str
    snapshot_digest: str
    ledger_source_hash: str
    evidence_digest: str
    policy_digest: str
    symbol: str
    side: str
    quantity: int
    execution_date: str
    execution_datetime: str
    fill_price: float
    price_basis: str
    gross_amount: float
    commission: float
    stamp_duty: float
    transfer_fee: float
    slippage: float
    total_fee: float
    cash_delta: float
    position_delta: int
    sellable_delta: int
    frozen_delta: int
    cash_before: float
    cash_after: float
    position_before: int
    position_after: int
    sellable_before: int
    sellable_after: int
    frozen_before: int
    frozen_after: int
    filled: bool = True
    fee_model: str = TRADE_COST_MODEL
    status: str = "filled"
    schema_version: str = API_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **paper_only_boundary()}


@dataclass(frozen=True)
class RejectionEvent:
    event_id: str
    content_digest: str
    intent_id: str
    intent_digest: str
    risk_decision_digest: str
    snapshot_digest: str
    ledger_source_hash: str
    evidence_digest: str
    policy_digest: str
    symbol: str
    side: str
    execution_date: str
    reason_codes: tuple[str, ...]
    cash_before: float = 0.0
    cash_after: float = 0.0
    position_before: int = 0
    position_after: int = 0
    sellable_before: int = 0
    sellable_after: int = 0
    frozen_before: int = 0
    frozen_after: int = 0
    filled: bool = False
    cash_delta: float = 0.0
    position_delta: int = 0
    sellable_delta: int = 0
    frozen_delta: int = 0
    status: str = "rejected_no_fill"
    schema_version: str = API_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **paper_only_boundary()}


PaperEvent = FillEvent | RejectionEvent


_LOCK_INDEX_GUARD = threading.Lock()
_LEDGER_LOCKS: dict[str, threading.RLock] = {}


def _in_process_ledger_lock(path: Any) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _LOCK_INDEX_GUARD:
        return _LEDGER_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _serialized_ledger_write(store: PaperStateStore):
    """Serialize the complete WAL/CAS/projection transaction per ledger path.

    The in-process lock protects threaded API use.  A one-byte OS file lock
    also protects separate local QuantDesk processes on the same machine.
    """

    local_lock = _in_process_ledger_lock(store.path)
    with local_lock:
        lock_path = store.path.with_name(f"{store.path.name}.kernel.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - Windows is the supported desktop target
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return {
            "kind": "dataframe",
            "columns": [str(item) for item in value.columns],
            "dtypes": [str(item) for item in value.dtypes],
            "rows": int(len(value)),
            "content_hash": compute_minute_frame_source_hash(value),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, tuple, list)):
        rows = [_canonical_value(item) for item in value]
        return sorted(rows, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)) if isinstance(value, set) else rows
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def compute_ledger_source_hash(ledger: Mapping[str, Any]) -> str:
    return _digest(dict(ledger) if isinstance(ledger, Mapping) else {})


def compute_minute_frame_source_hash(frame: pd.DataFrame) -> str:
    if not isinstance(frame, pd.DataFrame):
        return ""
    payload = frame.to_csv(index=True, lineterminator="\n").encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def compute_execution_component_source_hash(component: Mapping[str, Any]) -> str:
    return _digest({str(key): value for key, value in component.items() if str(key) != "source_hash"})


def compute_execution_evidence_digest(evidence: ExecutionEvidence) -> str:
    if not isinstance(evidence, ExecutionEvidence):
        raise TypeError("evidence must be ExecutionEvidence")
    return _digest(
        {
            "symbol": evidence.symbol,
            "execution_date": evidence.execution_date,
            "price_basis": evidence.price_basis,
            "minute_frame": evidence.minute_frame,
            "minute_source_path": evidence.minute_source_path,
            "minute_source_hash": evidence.minute_source_hash,
            "minute_pool": evidence.minute_pool,
            "daily_history": evidence.daily_history,
            "trading_calendar": evidence.trading_calendar,
            "market_metadata": evidence.market_metadata,
        }
    )


def compute_settlement_policy_digest(policy: SettlementPolicy) -> str:
    if not isinstance(policy, SettlementPolicy):
        raise TypeError("policy must be SettlementPolicy")
    return _digest(asdict(policy))


def _compute_snapshot_digest(snapshot: PortfolioSnapshot) -> str:
    payload = asdict(snapshot)
    payload.pop("snapshot_digest", None)
    return _digest(payload)


def _event_payload(event: PaperEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload.pop("event_id", None)
    payload.pop("content_digest", None)
    return payload


def compute_paper_event_content_digest(event: PaperEvent) -> str:
    return _digest(_event_payload(event))


def _event_id(content_digest: str) -> str:
    return f"paper_event_{content_digest.removeprefix('sha256:')[:24]}"


def _seal_event(event: PaperEvent) -> PaperEvent:
    digest = compute_paper_event_content_digest(event)
    return replace(event, content_digest=digest, event_id=_event_id(digest))


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _strict_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip().lower() not in {"", "nan", "none", "null"} else ""


def _cash(ledger: Mapping[str, Any]) -> float | None:
    account = ledger.get("account") if isinstance(ledger.get("account"), Mapping) else ledger
    return _finite(account.get("cash")) if isinstance(account, Mapping) else None


def _positions(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = ledger.get("positions")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _quantity(position: Mapping[str, Any]) -> int:
    value = position.get("quantity") if "quantity" in position else position.get("shares")
    number = _finite(value)
    return int(number) if number is not None and number.is_integer() and number >= 0 else 0


def _nonnegative_int(value: Any) -> int | None:
    number = _finite(value)
    return int(number) if number is not None and number.is_integer() and number >= 0 else None


def _position_balances(position: Mapping[str, Any] | None) -> tuple[int, int, int]:
    if not position:
        return 0, 0, 0
    held = _quantity(position)
    raw_sellable = position.get("sellable_quantity")
    if raw_sellable is None:
        raw_sellable = position.get("available_shares", 0)
    sellable = _nonnegative_int(raw_sellable) or 0
    raw_frozen = position.get("frozen_quantity")
    frozen = _nonnegative_int(raw_frozen) if raw_frozen is not None else max(0, held - sellable)
    return held, min(sellable, held), min(int(frozen or 0), held)


def _rejection(
    *,
    intent: PaperOrderIntent,
    risk: RiskGateDecision,
    snapshot: PortfolioSnapshot,
    ledger_hash: str,
    evidence: ExecutionEvidence,
    policy: SettlementPolicy,
    funded_ledger: Mapping[str, Any],
    reasons: list[str],
) -> RejectionEvent:
    position = next(
        (
            row for row in _positions(funded_ledger)
            if normalize_symbol(row.get("symbol") or row.get("code")) == normalize_symbol(intent.symbol)
        ),
        None,
    )
    held, sellable, frozen = _position_balances(position)
    cash = float(_cash(funded_ledger) or 0.0)
    event = RejectionEvent(
        event_id="",
        content_digest="",
        intent_id=str(intent.intent_id or ""),
        intent_digest=str(intent.intent_digest or ""),
        risk_decision_digest=str(risk.decision_digest or ""),
        snapshot_digest=str(snapshot.snapshot_digest or ""),
        ledger_source_hash=ledger_hash,
        evidence_digest=str(evidence.evidence_digest or ""),
        policy_digest=compute_settlement_policy_digest(policy),
        symbol=normalize_symbol(intent.symbol),
        side=str(intent.side or "").lower(),
        execution_date=str(evidence.execution_date or "")[:10],
        reason_codes=tuple(dict.fromkeys(reason for reason in reasons if reason)),
        cash_before=cash,
        cash_after=cash,
        position_before=held,
        position_after=held,
        sellable_before=sellable,
        sellable_after=sellable,
        frozen_before=frozen,
        frozen_after=frozen,
    )
    return _seal_event(event)


def simulate_paper_intent(
    intent: PaperOrderIntent,
    risk_decision: RiskGateDecision,
    snapshot: PortfolioSnapshot,
    funded_ledger: Mapping[str, Any],
    execution_evidence: ExecutionEvidence,
    settlement_policy: SettlementPolicy,
) -> PaperEvent:
    """Purely simulate one paper fill or a fail-closed no-fill rejection."""

    if not isinstance(intent, PaperOrderIntent):
        raise TypeError("intent must be PaperOrderIntent")
    if not isinstance(risk_decision, RiskGateDecision):
        raise TypeError("risk_decision must be RiskGateDecision")
    if not isinstance(snapshot, PortfolioSnapshot):
        raise TypeError("snapshot must be PortfolioSnapshot")
    if not isinstance(execution_evidence, ExecutionEvidence):
        raise TypeError("execution_evidence must be ExecutionEvidence")
    if not isinstance(settlement_policy, SettlementPolicy):
        raise TypeError("settlement_policy must be SettlementPolicy")

    reasons: list[str] = []
    ledger = funded_ledger if isinstance(funded_ledger, Mapping) else {}
    ledger_hash = compute_ledger_source_hash(ledger)
    if intent.intent_digest != compute_paper_order_intent_digest(intent):
        reasons.append("paper_intent_digest_mismatch")
    if snapshot.snapshot_digest != _compute_snapshot_digest(snapshot):
        reasons.append("portfolio_snapshot_digest_mismatch")
    risk_ok, risk_reasons = validate_risk_gate_decision_binding(
        risk_decision, intent, snapshot, ledger_source_hash=ledger_hash
    )
    if not risk_ok:
        reasons.extend(risk_reasons)
    if not (
        settlement_policy.execution_model == "next_open"
        and settlement_policy.price_basis == "raw"
        and settlement_policy.strict_t_plus_one is True
        and settlement_policy.verified_minute_pool_required is True
        and settlement_policy.verified_trading_calendar_required is True
        and settlement_policy.fee_model == TRADE_COST_MODEL
    ):
        reasons.append("settlement_policy_invalid")

    symbol = normalize_symbol(intent.symbol)
    execution_day = _day(execution_evidence.execution_date)
    signal_day = _day(intent.as_of)
    if not symbol or normalize_symbol(execution_evidence.symbol) != symbol:
        reasons.append("execution_symbol_mismatch")
    if execution_day is None or signal_day is None or execution_day <= signal_day:
        reasons.append("execution_not_after_signal_date")
    if execution_evidence.price_basis != "raw":
        reasons.append("execution_price_basis_not_raw")
    # Never trust the caller-supplied basis string alone: classify the original
    # minute frame and fail closed on mixed/unknown (or non-raw) evidence.
    frame_basis, frame_basis_reason = classify_frame_price_basis(execution_evidence.minute_frame)
    if frame_basis_reason:
        reasons.append(f"execution_minute_frame_basis_unverified:{frame_basis_reason}")
    elif frame_basis != "raw":
        reasons.append("execution_minute_frame_basis_not_raw")
    if execution_evidence.evidence_digest != compute_execution_evidence_digest(execution_evidence):
        reasons.append("execution_evidence_digest_mismatch")
    if not _strict_text(execution_evidence.minute_source_path):
        reasons.append("minute_source_path_missing")
    if execution_evidence.minute_source_hash != compute_minute_frame_source_hash(execution_evidence.minute_frame):
        reasons.append("minute_source_hash_mismatch")

    pool = execution_evidence.minute_pool if isinstance(execution_evidence.minute_pool, Mapping) else {}
    if not (
        pool.get("task_type") == "verified_minute_pool_snapshot"
        and pool.get("status") == "passed"
        and pool.get("passed") is True
        and pool.get("quality_status") == "passed"
        and pool.get("snapshot_available") is True
    ):
        reasons.append("minute_pool_not_verified")
    if pool.get("schema_version") != API_SCHEMA_VERSION:
        reasons.append("minute_pool_schema_invalid")
    if str(pool.get("target_date") or "")[:10] != str(execution_evidence.execution_date or "")[:10]:
        reasons.append("minute_pool_target_date_mismatch")
    pool_symbols = {normalize_symbol(value) for value in (pool.get("symbols") or [])}
    if symbol not in pool_symbols:
        reasons.append("minute_pool_symbol_missing")
    declared_pool_hash = str(pool.get("source_hash") or "")
    if declared_pool_hash != minute_pool_capability_source_hash(dict(pool)):
        reasons.append("minute_pool_source_hash_mismatch")
    if not _strict_text(pool.get("snapshot_path")) or not _strict_text(pool.get("job_path")):
        reasons.append("minute_pool_provenance_missing")
    if not (
        pool.get("read_only") is True
        and pool.get("network_used") is False
        and pool.get("canonical_write") is False
    ):
        reasons.append("minute_pool_boundary_invalid")
    pool_counts = {
        name: _nonnegative_int(pool.get(name))
        for name in (
            "symbol_count", "usable_count", "total_symbols", "partial_count",
            "insufficient_count", "invalid_count", "missing_count",
        )
    }
    if not (
        pool_counts["symbol_count"] == len(pool_symbols)
        and pool_counts["usable_count"] == len(pool_symbols)
        and pool_counts["total_symbols"] == len(pool_symbols)
        and all(pool_counts[name] == 0 for name in ("partial_count", "insufficient_count", "invalid_count", "missing_count"))
    ):
        reasons.append("minute_pool_count_inconsistent")
    if pool.get("source_snapshot") not in {"latest", "last_passed"}:
        reasons.append("minute_pool_source_snapshot_invalid")
    elif pool.get("source_snapshot") == "latest" and str(pool.get("latest_quality_status") or "passed") != "passed":
        reasons.append("minute_pool_latest_status_invalid")
    elif pool.get("source_snapshot") == "last_passed" and not (
        str(pool.get("latest_quality_status") or "") not in {"", "passed"}
        and bool(pool.get("warnings"))
    ):
        reasons.append("minute_pool_fallback_provenance_missing")

    raw_frame = execution_evidence.minute_frame
    order_state = source_time_order_state(raw_frame)
    if order_state is not True:
        reasons.append("minute_source_time_order_invalid")
    normalized = normalize_intraday_frame(
        raw_frame,
        default_symbol=symbol,
        source="paper_execution_kernel",
        target_date=str(execution_evidence.execution_date or "")[:10],
    )
    normalized_frame = normalized.get("frame")
    normalized_symbols = (
        {normalize_symbol(value) for value in normalized_frame["symbol"].tolist()}
        if isinstance(normalized_frame, pd.DataFrame) and "symbol" in normalized_frame.columns
        else set()
    )
    if normalized_symbols != {symbol}:
        reasons.append("minute_frame_symbol_mismatch")
    session_frame, filtered_count = filter_intraday_trading_session(
        normalized_frame,
        target_date=str(execution_evidence.execution_date or "")[:10],
    )
    gate = build_intraday_quality_gate(
        session_frame,
        provider="paper_execution_kernel",
        target_date=str(execution_evidence.execution_date or "")[:10],
        price_only=bool(normalized.get("price_only_intraday")),
        source=execution_evidence.minute_source_path,
        session_filtered_count=filtered_count,
        source_time_order_valid=order_state,
        extra_warnings=list(normalized.get("warnings") or []),
    )
    if not (
        normalized.get("ok") is True
        and not normalized.get("errors")
        and gate.get("ohlcv_complete") is True
        and gate.get("signal_eligible") is True
        and isinstance(session_frame, pd.DataFrame)
        and not session_frame.empty
    ):
        reasons.append("minute_quality_gate_failed")

    first_row: Mapping[str, Any] = session_frame.iloc[0] if isinstance(session_frame, pd.DataFrame) and not session_frame.empty else {}
    fill_price = _finite(first_row.get("open"), positive=True)
    first_volume = _finite(first_row.get("volume"))
    execution_datetime = str(first_row.get("datetime") or "")
    if fill_price is None:
        reasons.append("next_open_missing")
    if first_volume is None or first_volume <= 0:
        reasons.append("first_minute_no_volume")
    first_hms = execution_datetime[11:19]
    if first_hms not in {"09:30:00", "09:31:00"}:
        reasons.append("next_open_bar_missing")

    daily = execution_evidence.daily_history if isinstance(execution_evidence.daily_history, Mapping) else {}
    if not daily:
        reasons.append("daily_history_missing")
    daily_source_hash = str(daily.get("source_hash") or "")
    if daily and daily_source_hash != compute_execution_component_source_hash(daily):
        reasons.append("daily_history_source_hash_mismatch")
    if not (
        daily.get("status") == "passed"
        and daily.get("verified") is True
        and daily.get("evidence_kind") == "verified_daily_history"
    ):
        reasons.append("daily_history_not_verified")
    if daily.get("schema_version") != API_SCHEMA_VERSION:
        reasons.append("daily_history_schema_invalid")
    previous_close = _finite(daily.get("previous_close"), positive=True)
    previous_day = _day(daily.get("previous_trade_date"))
    if not (
        normalize_symbol(daily.get("symbol")) == symbol
        and daily.get("price_basis") == "raw"
        and previous_close is not None
        and _strict_text(daily.get("source_path"))
    ):
        reasons.append("daily_history_invalid")
    if daily.get("proxy_thresholds_used") is not False:
        reasons.append("daily_history_proxy_forbidden")
    if (
        execution_day is None
        or previous_day is None
        or previous_day >= execution_day
        or signal_day is None
        or previous_day != signal_day
    ):
        reasons.append("daily_history_date_invalid")

    calendar = execution_evidence.trading_calendar if isinstance(execution_evidence.trading_calendar, Mapping) else {}
    if not (
        calendar.get("status") == "passed"
        and calendar.get("verified") is True
        and calendar.get("schema_version") == API_SCHEMA_VERSION
        and calendar.get("evidence_kind") == "verified_a_share_trading_calendar"
        and _strict_text(calendar.get("source_path"))
    ):
        reasons.append("trading_calendar_not_verified")
    if str(calendar.get("source_hash") or "") != compute_execution_component_source_hash(calendar):
        reasons.append("trading_calendar_source_hash_mismatch")
    if (
        str(calendar.get("signal_date") or "")[:10] != str(intent.as_of or "")[:10]
        or str(calendar.get("next_trade_date") or "")[:10] != str(execution_evidence.execution_date or "")[:10]
    ):
        reasons.append("trading_calendar_next_session_mismatch")

    market_status = execution_evidence.market_metadata if isinstance(execution_evidence.market_metadata, Mapping) else {}
    if not (
        market_status.get("status") == "passed"
        and market_status.get("verified") is True
        and market_status.get("schema_version") == API_SCHEMA_VERSION
        and market_status.get("evidence_kind") == "verified_market_status"
        and normalize_symbol(market_status.get("symbol")) == symbol
        and isinstance(market_status.get("is_st"), bool)
        and isinstance(market_status.get("suspended"), bool)
        and _strict_text(market_status.get("source_path"))
    ):
        reasons.append("market_status_not_verified")
    if str(market_status.get("source_hash") or "") != compute_execution_component_source_hash(market_status):
        reasons.append("market_status_source_hash_mismatch")

    side = str(intent.side or "").strip().lower()
    if side not in {"buy", "sell"}:
        reasons.append("paper_intent_side_invalid")
    quantity_value = _finite(intent.quantity, positive=True)
    quantity = int(quantity_value) if quantity_value is not None and quantity_value.is_integer() else 0
    board_type = board_type_for_symbol(symbol, metadata=dict(execution_evidence.market_metadata))
    lot = lot_size_for_symbol(symbol, metadata=dict(execution_evidence.market_metadata))
    position = next((row for row in _positions(ledger) if normalize_symbol(row.get("symbol") or row.get("code")) == symbol), None)
    held = _quantity(position or {}) if position else None
    if not is_valid_order_quantity(quantity, lot_size=lot, board_type=board_type, held_shares=held if side == "sell" else None):
        reasons.append("board_lot_quantity_invalid")

    metadata = dict(execution_evidence.market_metadata)
    if fill_price is not None and previous_close is not None:
        metadata["pct_chg"] = (fill_price / previous_close - 1.0) * 100.0
    if paper_trade_block_reason(side.upper(), metadata, symbol=symbol):
        reasons.append("paper_market_rule_blocked")

    if side == "buy":
        cash = _cash(ledger)
        if cash is None:
            reasons.append("funded_cash_missing")
    else:
        if position is None:
            reasons.append("funded_position_missing")
        else:
            sellable = _quantity({"quantity": position.get("sellable_quantity", position.get("available_shares", 0))})
            last_buy_day = _day(position.get("last_buy_date") or position.get("entry_date") or position.get("buy_date"))
            if quantity > sellable:
                reasons.append("sellable_quantity_insufficient")
            if execution_day is None or last_buy_day is None or execution_day <= last_buy_day:
                reasons.append("t_plus_one_blocked")

    if reasons:
        return _rejection(
            intent=intent,
            risk=risk_decision,
            snapshot=snapshot,
            ledger_hash=ledger_hash,
            evidence=execution_evidence,
            policy=settlement_policy,
            funded_ledger=ledger,
            reasons=reasons,
        )

    gross = round(float(fill_price) * quantity, 4)
    costs = compute_trade_costs(side=side, gross=gross)
    total_fee = float(costs["total_fee"])
    cash_delta = -(gross + total_fee) if side == "buy" else gross - total_fee
    if side == "buy" and cash_delta + float(_cash(ledger) or 0.0) < -1e-9:
        return _rejection(
            intent=intent, risk=risk_decision, snapshot=snapshot, ledger_hash=ledger_hash,
            evidence=execution_evidence, policy=settlement_policy, funded_ledger=ledger,
            reasons=["funded_cash_insufficient"],
        )
    held_before, sellable_before, frozen_before = _position_balances(position)
    cash_before = float(_cash(ledger) or 0.0)
    position_after = held_before + quantity if side == "buy" else held_before - quantity
    sellable_after = sellable_before if side == "buy" else sellable_before - quantity
    frozen_after = frozen_before + quantity if side == "buy" else frozen_before
    event = FillEvent(
        event_id="",
        content_digest="",
        intent_id=intent.intent_id,
        intent_digest=intent.intent_digest,
        risk_decision_digest=risk_decision.decision_digest,
        snapshot_digest=snapshot.snapshot_digest,
        ledger_source_hash=ledger_hash,
        evidence_digest=execution_evidence.evidence_digest,
        policy_digest=compute_settlement_policy_digest(settlement_policy),
        symbol=symbol,
        side=side,
        quantity=quantity,
        execution_date=str(execution_evidence.execution_date)[:10],
        execution_datetime=execution_datetime,
        fill_price=round(float(fill_price), 4),
        price_basis="raw",
        gross_amount=gross,
        commission=float(costs["commission"]),
        stamp_duty=float(costs["stamp_duty"]),
        transfer_fee=float(costs["transfer_fee"]),
        slippage=float(costs["slippage"]),
        total_fee=total_fee,
        cash_delta=round(cash_delta, 4),
        position_delta=quantity if side == "buy" else -quantity,
        sellable_delta=0 if side == "buy" else -quantity,
        frozen_delta=quantity if side == "buy" else 0,
        cash_before=round(cash_before, 4),
        cash_after=round(cash_before + cash_delta, 4),
        position_before=held_before,
        position_after=position_after,
        sellable_before=sellable_before,
        sellable_after=sellable_after,
        frozen_before=frozen_before,
        frozen_after=frozen_after,
    )
    return _seal_event(event)


def _event_integrity_valid(event: PaperEvent) -> bool:
    expected = compute_paper_event_content_digest(event)
    return event.content_digest == expected and event.event_id == _event_id(expected)


def _event_log_integrity_reasons(log: Mapping[str, Any]) -> tuple[str, ...]:
    rows = log.get("events")
    digests = log.get("event_digests")
    if not isinstance(rows, list) or not isinstance(digests, Mapping):
        return ("event_log_schema_invalid",)
    expected_boundary = paper_only_boundary()
    boundary_keys = set(expected_boundary)
    seen: set[str] = set()
    reasons: list[str] = []
    if rows and any(log.get(key) is not expected for key, expected in expected_boundary.items()):
        reasons.append("event_log_boundary_mismatch")
    for row in rows:
        if not isinstance(row, Mapping):
            reasons.append("event_log_row_invalid")
            continue
        event_id = str(row.get("event_id") or "")
        declared = str(row.get("content_digest") or "")
        if any(row.get(key) is not expected for key, expected in expected_boundary.items()):
            reasons.append("event_log_boundary_mismatch")
        payload = {
            str(key): value
            for key, value in row.items()
            if str(key) not in {"event_id", "content_digest"} | boundary_keys
        }
        computed = _digest(payload)
        if (
            not event_id
            or event_id in seen
            or declared != computed
            or event_id != _event_id(computed)
            or str(digests.get(event_id) or "") != declared
        ):
            reasons.append("event_log_row_digest_mismatch")
        seen.add(event_id)
    if set(str(key) for key in digests) != seen:
        reasons.append("event_log_digest_index_mismatch")
    return tuple(dict.fromkeys(reasons))


def _project_fill(state: Mapping[str, Any], event: FillEvent) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    reasons: list[str] = []
    projected = deepcopy(dict(state))
    positions = [dict(row) for row in projected.get("positions") or [] if isinstance(row, Mapping)]
    position = next((row for row in positions if normalize_symbol(row.get("symbol") or row.get("code")) == event.symbol), None)
    held_before, sellable_before, frozen_before = _position_balances(position)
    expected_costs = compute_trade_costs(side=event.side, gross=event.gross_amount)
    if not (
        event.price_basis == "raw"
        and event.quantity > 0
        and math.isclose(event.gross_amount, event.fill_price * event.quantity, abs_tol=1e-6)
        and all(
            math.isclose(float(getattr(event, field)), float(expected_costs[cost_key]), abs_tol=1e-6)
            for field, cost_key in (
                ("commission", "commission"), ("stamp_duty", "stamp_duty"),
                ("transfer_fee", "transfer_fee"), ("slippage", "slippage"),
                ("total_fee", "total_fee"),
            )
        )
    ):
        reasons.append("fill_cost_integrity_invalid")
    board_type = board_type_for_symbol(event.symbol)
    lot = lot_size_for_symbol(event.symbol)
    if not is_valid_order_quantity(
        event.quantity,
        lot_size=lot,
        board_type=board_type,
        held_shares=held_before if event.side == "sell" else None,
    ):
        reasons.append("board_lot_quantity_invalid")
    cash = _cash(projected)
    if cash is None:
        reasons.append("funded_cash_missing")
    expected_position_after = held_before + event.quantity if event.side == "buy" else held_before - event.quantity
    expected_sellable_after = sellable_before if event.side == "buy" else sellable_before - event.quantity
    expected_frozen_after = frozen_before + event.quantity if event.side == "buy" else frozen_before
    if not (
        event.filled is True
        and event.status == "filled"
        and event.schema_version == API_SCHEMA_VERSION
        and event.fee_model == TRADE_COST_MODEL
        and math.isclose(event.cash_before, float(cash or 0.0), abs_tol=1e-6)
        and math.isclose(event.cash_after, float(cash or 0.0) + event.cash_delta, abs_tol=1e-6)
        and event.position_before == held_before
        and event.position_after == expected_position_after
        and event.sellable_before == sellable_before
        and event.sellable_after == expected_sellable_after
        and event.frozen_before == frozen_before
        and event.frozen_after == expected_frozen_after
        and event.position_delta == event.position_after - event.position_before
        and event.sellable_delta == event.sellable_after - event.sellable_before
        and event.frozen_delta == event.frozen_after - event.frozen_before
    ):
        reasons.append("fill_before_after_mismatch")
    if event.side == "buy":
        expected_delta = -(event.gross_amount + event.total_fee)
        if not math.isclose(event.cash_delta, expected_delta, abs_tol=1e-6) or float(cash or 0.0) + event.cash_delta < -1e-9:
            reasons.append("buy_cash_delta_invalid")
    elif event.side == "sell":
        expected_delta = event.gross_amount - event.total_fee
        if not math.isclose(event.cash_delta, expected_delta, abs_tol=1e-6):
            reasons.append("sell_cash_delta_invalid")
        if position is None:
            reasons.append("funded_position_missing")
        else:
            sellable = int(position.get("sellable_quantity", position.get("available_shares", 0)) or 0)
            last_buy = _day(position.get("last_buy_date") or position.get("entry_date") or position.get("buy_date"))
            execution_day = _day(event.execution_date)
            if event.quantity > sellable:
                reasons.append("sellable_quantity_insufficient")
            if execution_day is None or last_buy is None or execution_day <= last_buy:
                reasons.append("t_plus_one_blocked")
    else:
        reasons.append("fill_side_invalid")
    if reasons:
        return None, tuple(dict.fromkeys(reasons))

    new_cash = round(float(cash) + event.cash_delta, 4)
    if isinstance(projected.get("account"), Mapping):
        projected["account"] = dict(projected["account"])
        projected["account"]["cash"] = new_cash
    else:
        projected["cash"] = new_cash
    if event.side == "buy":
        cash_out = -event.cash_delta
        if position is None:
            position = {
                "symbol": event.symbol,
                "quantity": event.quantity,
                "sellable_quantity": 0,
                "frozen_quantity": event.quantity,
                "avg_cost": round(cash_out / event.quantity, 4),
                "entry_price": round(cash_out / event.quantity, 4),
                "execution_price": event.fill_price,
                "entry_date": event.execution_date,
                "last_buy_date": event.execution_date,
                "latest_price": event.fill_price,
                "latest_date": event.execution_date,
                "price_basis": "raw",
                "funded": True,
                **paper_only_boundary(),
            }
            positions.append(position)
        else:
            old_qty = held_before
            old_cost = float(position.get("avg_cost") or position.get("entry_price") or position.get("buy_price") or 0.0) * old_qty
            new_qty = old_qty + event.quantity
            had_shares = "shares" in position
            had_available_shares = "available_shares" in position
            position.update(
                {
                    "quantity": new_qty,
                    "sellable_quantity": sellable_before,
                    "frozen_quantity": frozen_before + event.quantity,
                    "avg_cost": round((old_cost + cash_out) / new_qty, 4),
                    "last_buy_date": event.execution_date,
                    "latest_price": event.fill_price,
                    "latest_date": event.execution_date,
                }
            )
            if had_shares:
                position["shares"] = new_qty
                position["buy_price"] = position["avg_cost"]
            if had_available_shares:
                position["available_shares"] = sellable_before
    else:
        held = _quantity(position or {})
        had_shares = bool(position is not None and "shares" in position)
        had_available_shares = bool(position is not None and "available_shares" in position)
        avg_cost = float(
            (position or {}).get("avg_cost")
            or (position or {}).get("entry_price")
            or (position or {}).get("buy_price")
            or 0.0
        )
        remaining = held - event.quantity
        position["quantity"] = remaining
        position["sellable_quantity"] = max(0, sellable_before - event.quantity)
        if had_shares:
            position["shares"] = remaining
        if had_available_shares:
            position["available_shares"] = position["sellable_quantity"]
        position["latest_price"] = event.fill_price
        position["latest_date"] = event.execution_date
        realized = (event.fill_price - avg_cost) * event.quantity - event.total_fee
        projected["realized_pnl"] = round(float(projected.get("realized_pnl") or 0.0) + realized, 4)
        position["realized_pnl"] = round(float(position.get("realized_pnl") or 0.0) + realized, 4)
        if remaining <= 0:
            positions = [row for row in positions if row is not position]
            closed = list(projected.get("closed_positions") or [])
            closed.append({**position, "exit_date": event.execution_date, "exit_price": event.fill_price, **paper_only_boundary()})
            projected["closed_positions"] = closed
    projected["positions"] = positions
    projected["total_fees"] = round(float(projected.get("total_fees") or 0.0) + event.total_fee, 4)
    projected["trades"] = list(projected.get("trades") or []) + [{**event.to_dict(), "shadow_projection": True}]
    applied = list(projected.get("applied_event_ids") or [])
    applied.append(event.event_id)
    projected["applied_event_ids"] = applied
    projected["last_kernel_event_id"] = event.event_id
    projected.update(paper_only_boundary())
    return projected, ()


def _apply_paper_event_locked(
    event: PaperEvent,
    *,
    event_store: PaperStateStore,
    legacy_store: PaperStateStore,
    expected_ledger_source_hash: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Shadow dual-write one immutable event with CAS and idempotency."""

    if not isinstance(event, (FillEvent, RejectionEvent)):
        raise TypeError("event must be FillEvent or RejectionEvent")
    if not isinstance(event_store, PaperStateStore) or not isinstance(legacy_store, PaperStateStore):
        raise TypeError("event_store and legacy_store must be PaperStateStore")

    log = event_store.read({"events": [], "event_digests": {}, "projection_status": {}})
    log_integrity_reasons = _event_log_integrity_reasons(log)
    if log_integrity_reasons:
        return {
            "status": "event_log_integrity_failed",
            "reason_codes": list(log_integrity_reasons),
            **paper_only_boundary(),
        }
    digests = dict(log.get("event_digests") or {})
    state = legacy_store.read({})
    already_logged = event.event_id in digests
    if event.event_id in digests:
        if digests[event.event_id] != event.content_digest:
            return {"status": "event_id_conflict", "reason_codes": ["event_id_digest_conflict"], **paper_only_boundary()}
    if not _event_integrity_valid(event):
        return {"status": "event_integrity_failed", "reason_codes": ["event_content_digest_mismatch"], **paper_only_boundary()}
    if isinstance(event, RejectionEvent) and not (
        event.filled is False
        and event.status == "rejected_no_fill"
        and event.schema_version == API_SCHEMA_VERSION
        and event.cash_delta == 0
        and event.position_delta == 0
        and event.sellable_delta == 0
        and event.frozen_delta == 0
        and event.cash_before == event.cash_after
        and event.position_before == event.position_after
        and event.sellable_before == event.sellable_after
        and event.frozen_before == event.frozen_after
    ):
        return {"status": "event_semantics_failed", "reason_codes": ["rejection_zero_delta_contract_invalid"], **paper_only_boundary()}
    if event.event_id in digests:
        applied_ids = set(state.get("applied_event_ids") or [])
        projection_status = dict(log.get("projection_status") or {})
        if isinstance(event, RejectionEvent):
            return {"status": "duplicate_noop", "event_id": event.event_id, **paper_only_boundary()}
        if event.event_id in applied_ids:
            if projection_status.get(event.event_id) != "applied":
                projection_status[event.event_id] = "applied"
                log["projection_status"] = projection_status
                event_store.write(log)
                return {"status": "reconciled_duplicate", "event_id": event.event_id, **paper_only_boundary()}
            return {"status": "duplicate_noop", "event_id": event.event_id, **paper_only_boundary()}

    actual_hash = compute_ledger_source_hash(state)
    if not (
        expected_ledger_source_hash
        and expected_ledger_source_hash == event.ledger_source_hash
        and expected_ledger_source_hash == actual_hash
    ):
        return {"status": "cas_mismatch", "reason_codes": ["funded_ledger_cas_mismatch"], **paper_only_boundary()}

    projected: dict[str, Any] | None = None
    if isinstance(event, FillEvent):
        projected, reasons = _project_fill(state, event)
        if projected is None:
            return {"status": "projection_blocked", "reason_codes": list(reasons), **paper_only_boundary()}
    if dry_run:
        return {"status": "dry_run", "event_id": event.event_id, "would_project_fill": isinstance(event, FillEvent), **paper_only_boundary()}

    projection_status = dict(log.get("projection_status") or {})
    if not already_logged:
        log["events"] = list(log.get("events") or []) + [event.to_dict()]
        digests[event.event_id] = event.content_digest
        log["event_digests"] = digests
        projection_status[event.event_id] = "logged"
    log["projection_status"] = projection_status
    log.update(paper_only_boundary())
    if not already_logged:
        event_store.write(log)

    if isinstance(event, RejectionEvent):
        return {"status": "rejection_logged", "event_id": event.event_id, **paper_only_boundary()}

    legacy_store.write(projected or state)
    projection_status[event.event_id] = "applied"
    log["projection_status"] = projection_status
    event_store.write(log)
    return {"status": "applied", "event_id": event.event_id, "state": projected, **paper_only_boundary()}


def apply_paper_event(
    event: PaperEvent,
    *,
    event_store: PaperStateStore,
    legacy_store: PaperStateStore,
    expected_ledger_source_hash: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Atomically serialize WAL, CAS and legacy projection for one ledger."""

    if not isinstance(legacy_store, PaperStateStore):
        raise TypeError("legacy_store must be PaperStateStore")
    with _serialized_ledger_write(legacy_store):
        return _apply_paper_event_locked(
            event,
            event_store=event_store,
            legacy_store=legacy_store,
            expected_ledger_source_hash=expected_ledger_source_hash,
            dry_run=dry_run,
        )


__all__ = [
    "ExecutionEvidence",
    "SettlementPolicy",
    "FillEvent",
    "RejectionEvent",
    "simulate_paper_intent",
    "apply_paper_event",
    "compute_execution_component_source_hash",
    "compute_execution_evidence_digest",
    "compute_ledger_source_hash",
    "compute_minute_frame_source_hash",
    "compute_paper_event_content_digest",
    "compute_settlement_policy_digest",
]
