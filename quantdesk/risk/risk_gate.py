"""Deterministic paper-account risk gate.

This module is a pure orchestration facade over the existing QuantDesk risk
checks.  It validates the evidence envelope before invoking those checks and
never creates, submits, or executes an order.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping

import numpy as np
import pandas as pd

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.paper.portfolio_contracts import (
    PaperCapitalProfile,
    PaperOrderIntent,
    PortfolioSnapshot,
    build_portfolio_snapshot,
    compute_paper_order_intent_digest,
)
from quantdesk.paper.trade_costs import buy_cash_outflow, sell_cash_inflow
from quantdesk.risk.liquidity_warning import evaluate_liquidity_warning
from quantdesk.risk.portfolio_concentration import evaluate_concentration
from quantdesk.risk.pre_trade_gate import PreTradeGateConfig, run_pre_trade_checks
from quantdesk.risk.smart_stop import build_smart_stop_for_position
from quantdesk.risk.var import portfolio_var
from quantdesk.version import API_SCHEMA_VERSION


_SHA256_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_UNFUNDED_STATES = {
    "paper_watchlist",
    "manual_observation",
    "excluded_pending_research",
    "research_candidate",
}


@dataclass(frozen=True)
class RiskGateDecision:
    """Serializable result of the deterministic paper risk gate."""

    status: str
    passed: bool
    veto: bool
    evidence_eligible: bool
    reason_codes: tuple[str, ...]
    check_results: Mapping[str, Any] = field(default_factory=dict)
    paper_exit_reviews: tuple[Mapping[str, Any], ...] = ()
    as_of: str = ""
    symbol: str = ""
    intent_id: str = ""
    execution_status: str = "not_submitted"
    llm_review_present: bool = False
    llm_override_applied: bool = False
    data_cutoff: str = ""
    source_path: str = ""
    source_hash: str = ""
    decision_digest: str = ""
    evidence_summary: Mapping[str, Any] = field(default_factory=dict)
    intent_digest: str = ""
    snapshot_digest: str = ""
    config_digest: str = ""
    ledger_source_hash: str = ""
    price_data_contract_version: str = ""
    price_policy_validation: Mapping[str, Any] = field(default_factory=dict)
    pit_status: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = API_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = tuple(self.reason_codes)
        payload["check_results"] = deepcopy(dict(self.check_results))
        payload["paper_exit_reviews"] = [
            deepcopy(dict(review)) for review in self.paper_exit_reviews
        ]
        payload.update(paper_only_boundary())
        return payload


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _strict_text(value: Any) -> str | None:
    """Accept real classification/provenance text, never null-like sentinels."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return None
    return text


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _canonical_evidence_value(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        csv_bytes = value.to_csv(
            index=True,
            date_format="%Y-%m-%dT%H:%M:%S.%f%z",
            lineterminator="\n",
        ).encode("utf-8")
        return {
            "kind": "dataframe",
            "rows": int(len(value)),
            "columns": [str(column) for column in value.columns],
            "dtypes": [str(dtype) for dtype in value.dtypes],
            "index_name": str(value.index.name or ""),
            "content_hash": f"sha256:{hashlib.sha256(csv_bytes).hexdigest()}",
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_evidence_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_evidence_value(item) for item in value]
    if isinstance(value, np.generic):
        return _canonical_evidence_value(value.item())
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return {"kind": "non_finite_float", "value": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"kind": type(value).__name__, "value": str(value)}


def compute_risk_evidence_source_hash(component_mapping: Mapping[str, Any]) -> str:
    """Digest every business/provenance field except the declared hash itself."""

    if not isinstance(component_mapping, Mapping):
        raise TypeError("component_mapping must be a mapping")
    payload = {
        str(key): value
        for key, value in component_mapping.items()
        if str(key) != "source_hash"
    }
    canonical = _canonical_evidence_value(payload)
    return _canonical_digest(canonical)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _validate_component(
    evidence: Mapping[str, Any],
    component: str,
    *,
    as_of_day: date | None,
    reasons: list[str],
) -> Mapping[str, Any]:
    value = evidence.get(component)
    if not isinstance(value, Mapping):
        reasons.append(f"{component}_evidence_missing")
        return {}
    if _strict_text(value.get("source_path")) is None:
        reasons.append(f"{component}_provenance_missing")
    source_hash = str(value.get("source_hash") or "").strip()
    if not source_hash:
        reasons.append(f"{component}_provenance_missing")
    elif not _SHA256_RE.fullmatch(source_hash):
        reasons.append(f"{component}_source_hash_invalid")
    elif source_hash.lower() != compute_risk_evidence_source_hash(value).lower():
        reasons.append(f"{component}_source_hash_mismatch")
    cutoff = _parse_day(value.get("data_cutoff"))
    if cutoff is None:
        reasons.append(f"{component}_provenance_missing")
    elif as_of_day is not None and cutoff > as_of_day:
        reasons.append(f"{component}_evidence_after_as_of")
    return value


def _funded_positions(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = ledger.get("positions")
    if not isinstance(rows, list):
        return []
    funded: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        state = str(raw.get("effective_state") or raw.get("lifecycle_state") or "").strip()
        if raw.get("funded") is False or state in _UNFUNDED_STATES:
            continue
        funded.append(dict(raw))
    return funded


def _position_value(position: Mapping[str, Any]) -> float | None:
    direct = _finite(position.get("market_value") or position.get("estimated_value"))
    if direct is not None and direct >= 0:
        return direct
    quantity = _finite(position.get("quantity") or position.get("shares"))
    price = _finite(position.get("latest_price") or position.get("mark_price"))
    if quantity is None or price is None or quantity < 0 or price < 0:
        return None
    return quantity * price


def _positions_for_checks(
    positions: list[dict[str, Any]],
    *,
    equity: float,
    sector_by_symbol: Mapping[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    current: dict[str, dict[str, Any]] = {}
    aggregates: dict[str, dict[str, Any]] = {}
    for position in positions:
        symbol = str(position.get("symbol") or position.get("code") or "").strip()
        value = _position_value(position)
        if not symbol or value is None:
            continue
        sector = str(sector_by_symbol[symbol])
        item = aggregates.setdefault(
            symbol,
            {"symbol": symbol, "market_value": 0.0, "sector": sector},
        )
        item["market_value"] += value
    for symbol, item in aggregates.items():
        value = float(item["market_value"])
        current[symbol] = {
            **item,
            "amount": value,
            "weight": value / equity,
            "industry": item["sector"],
        }
    return current, aggregates


def _returns_summary(frame: pd.DataFrame, required: list[str]) -> dict[str, Any]:
    selected = frame.loc[:, required].apply(pd.to_numeric, errors="coerce")
    csv_bytes = selected.to_csv(
        index=True,
        date_format="%Y-%m-%dT%H:%M:%S.%f%z",
        lineterminator="\n",
    ).encode("utf-8")
    return {
        "rows": int(len(selected)),
        "columns": list(required),
        "index_start": str(selected.index[0]) if len(selected) else "",
        "index_end": str(selected.index[-1]) if len(selected) else "",
        "content_hash": f"sha256:{hashlib.sha256(csv_bytes).hexdigest()}",
    }


def _build_decision(
    *,
    status: str,
    reasons: list[str],
    checks: Mapping[str, Any],
    reviews: list[Mapping[str, Any]],
    intent: PaperOrderIntent,
    as_of: str,
    evidence_eligible: bool,
    evidence_summary: Mapping[str, Any],
    llm_review: Mapping[str, Any] | None,
    snapshot: PortfolioSnapshot,
    ledger_source_hash: str,
    config_digest: str,
    data_cutoff: str,
) -> RiskGateDecision:
    reason_codes = _dedupe(reasons)
    source_binding = {
        "intent_digest": intent.intent_digest,
        "intent_source_hash": intent.source_hash,
        "portfolio_source_hash": intent.portfolio_source_hash,
        "portfolio_snapshot_digest": intent.portfolio_snapshot_digest,
        "snapshot_digest": snapshot.snapshot_digest,
        "snapshot_source_hash": snapshot.source_hash,
        "ledger_source_hash": ledger_source_hash,
        "config_digest": config_digest,
        "evidence_summary": evidence_summary,
    }
    source_hash = _canonical_digest(source_binding)
    decision_source_path = f"paper-risk-gate://{intent.intent_id or 'missing'}"
    bound_payload = {
        "schema_version": API_SCHEMA_VERSION,
        "status": status,
        "passed": status == "passed",
        "veto": status != "passed",
        "evidence_eligible": evidence_eligible,
        "reason_codes": reason_codes,
        "check_results": checks,
        "paper_exit_reviews": reviews,
        "as_of": as_of,
        "symbol": intent.symbol,
        "intent_id": intent.intent_id,
        "execution_status": "not_submitted",
        "intent_source_hash": intent.source_hash,
        "intent_digest": intent.intent_digest,
        "snapshot_digest": snapshot.snapshot_digest,
        "snapshot_source_hash": snapshot.source_hash,
        "ledger_source_hash": ledger_source_hash,
        "config_digest": config_digest,
        "source_hash": source_hash,
        "source_path": decision_source_path,
        "data_cutoff": data_cutoff,
        "price_data_contract_version": intent.price_data_contract_version,
        "price_policy_validation": (
            dict(intent.price_policy_validation)
            if isinstance(intent.price_policy_validation, Mapping)
            else {}
        ),
        "pit_status": dict(intent.pit_status) if isinstance(intent.pit_status, Mapping) else {},
        "coverage": dict(intent.coverage) if isinstance(intent.coverage, Mapping) else {},
        "evidence_summary": evidence_summary,
        "llm_review_present": isinstance(llm_review, Mapping),
        "llm_override_applied": False,
        **paper_only_boundary(),
    }
    decision_digest = _canonical_digest(bound_payload)
    return RiskGateDecision(
        status=status,
        passed=status == "passed",
        veto=status != "passed",
        evidence_eligible=evidence_eligible,
        reason_codes=reason_codes,
        check_results=deepcopy(dict(checks)),
        paper_exit_reviews=tuple(deepcopy(reviews)),
        as_of=as_of,
        symbol=intent.symbol,
        intent_id=intent.intent_id,
        llm_review_present=isinstance(llm_review, Mapping),
        llm_override_applied=False,
        data_cutoff=data_cutoff,
        source_path=decision_source_path,
        source_hash=source_hash,
        decision_digest=decision_digest,
        evidence_summary=deepcopy(dict(evidence_summary)),
        intent_digest=str(intent.intent_digest or ""),
        snapshot_digest=str(snapshot.snapshot_digest or ""),
        config_digest=config_digest,
        ledger_source_hash=ledger_source_hash,
        price_data_contract_version=str(intent.price_data_contract_version or ""),
        price_policy_validation=deepcopy(
            dict(intent.price_policy_validation)
            if isinstance(intent.price_policy_validation, Mapping)
            else {}
        ),
        pit_status=deepcopy(dict(intent.pit_status) if isinstance(intent.pit_status, Mapping) else {}),
        coverage=deepcopy(dict(intent.coverage) if isinstance(intent.coverage, Mapping) else {}),
    )


def compute_risk_gate_decision_source_hash(
    decision: RiskGateDecision,
    intent: PaperOrderIntent,
    snapshot: PortfolioSnapshot,
) -> str:
    """Recompute the source envelope sealed by a risk decision.

    This public verifier exists for the paper execution kernel.  It does not
    run risk checks or alter thresholds; it only proves that the already-made
    decision still belongs to the supplied intent, snapshot and funded ledger
    digest.
    """

    if not isinstance(decision, RiskGateDecision):
        raise TypeError("decision must be RiskGateDecision")
    return _canonical_digest(
        {
            "intent_digest": intent.intent_digest,
            "intent_source_hash": intent.source_hash,
            "portfolio_source_hash": intent.portfolio_source_hash,
            "portfolio_snapshot_digest": intent.portfolio_snapshot_digest,
            "snapshot_digest": snapshot.snapshot_digest,
            "snapshot_source_hash": snapshot.source_hash,
            "ledger_source_hash": decision.ledger_source_hash,
            "config_digest": decision.config_digest,
            "evidence_summary": decision.evidence_summary,
        }
    )


def compute_risk_gate_decision_digest(
    decision: RiskGateDecision,
    intent: PaperOrderIntent,
    snapshot: PortfolioSnapshot,
) -> str:
    """Recompute the immutable risk-decision content digest."""

    source_hash = compute_risk_gate_decision_source_hash(decision, intent, snapshot)
    payload = {
        "schema_version": decision.schema_version,
        "status": decision.status,
        "passed": decision.passed,
        "veto": decision.veto,
        "evidence_eligible": decision.evidence_eligible,
        "reason_codes": decision.reason_codes,
        "check_results": decision.check_results,
        "paper_exit_reviews": decision.paper_exit_reviews,
        "as_of": decision.as_of,
        "symbol": intent.symbol,
        "intent_id": intent.intent_id,
        "execution_status": decision.execution_status,
        "intent_source_hash": intent.source_hash,
        "intent_digest": intent.intent_digest,
        "snapshot_digest": snapshot.snapshot_digest,
        "snapshot_source_hash": snapshot.source_hash,
        "ledger_source_hash": decision.ledger_source_hash,
        "config_digest": decision.config_digest,
        "source_hash": source_hash,
        "source_path": decision.source_path,
        "data_cutoff": decision.data_cutoff,
        "price_data_contract_version": decision.price_data_contract_version,
        "price_policy_validation": decision.price_policy_validation,
        "pit_status": decision.pit_status,
        "coverage": decision.coverage,
        "evidence_summary": decision.evidence_summary,
        "llm_review_present": decision.llm_review_present,
        "llm_override_applied": False,
        **paper_only_boundary(),
    }
    return _canonical_digest(payload)


def validate_risk_gate_decision_binding(
    decision: RiskGateDecision,
    intent: PaperOrderIntent,
    snapshot: PortfolioSnapshot,
    *,
    ledger_source_hash: str,
) -> tuple[bool, tuple[str, ...]]:
    """Validate a risk PASS and all of its execution-facing bindings."""

    reasons: list[str] = []
    if not isinstance(decision, RiskGateDecision):
        return False, ("risk_decision_type_invalid",)
    if not (
        decision.status == "passed"
        and decision.passed is True
        and decision.veto is False
        and decision.evidence_eligible is True
        and decision.llm_override_applied is False
        and decision.execution_status == "not_submitted"
        and decision.source_path == f"paper-risk-gate://{intent.intent_id or 'missing'}"
    ):
        reasons.append("risk_decision_not_passed")
    if not (
        decision.intent_id == intent.intent_id
        and decision.symbol == intent.symbol
        and decision.intent_digest == intent.intent_digest
        and decision.snapshot_digest == snapshot.snapshot_digest
        and decision.ledger_source_hash == ledger_source_hash
        and intent.portfolio_source_hash == ledger_source_hash
        and snapshot.source_hash == ledger_source_hash
    ):
        reasons.append("risk_decision_binding_mismatch")
    expected_source = compute_risk_gate_decision_source_hash(decision, intent, snapshot)
    expected_digest = compute_risk_gate_decision_digest(decision, intent, snapshot)
    if decision.source_hash != expected_source or decision.decision_digest != expected_digest:
        reasons.append("risk_decision_digest_mismatch")
    return not reasons, tuple(dict.fromkeys(reasons))


def evaluate_paper_risk_gate(
    intent: PaperOrderIntent,
    snapshot: PortfolioSnapshot,
    *,
    profile: PaperCapitalProfile,
    funded_ledger: Mapping[str, Any],
    evidence: Mapping[str, Any],
    as_of: str,
    pre_trade_config: PreTradeGateConfig | None = None,
    llm_review: Mapping[str, Any] | None = None,
) -> RiskGateDecision:
    """Evaluate a paper intent using trusted, point-in-time risk evidence.

    All inputs are in-memory.  Missing or inconsistent evidence fails closed;
    the optional LLM review is recorded only as presence metadata and can never
    alter the deterministic result.
    """

    reasons: list[str] = []
    checks: dict[str, Any] = {}
    reviews: list[Mapping[str, Any]] = []
    as_of_text = str(as_of or "").strip()
    as_of_day = _parse_day(as_of_text)
    if as_of_day is None:
        reasons.append("as_of_invalid")

    if not isinstance(intent, PaperOrderIntent):
        raise TypeError("intent must be PaperOrderIntent")
    if not isinstance(snapshot, PortfolioSnapshot):
        raise TypeError("snapshot must be PortfolioSnapshot")
    if not isinstance(profile, PaperCapitalProfile):
        raise TypeError("profile must be PaperCapitalProfile")
    if pre_trade_config is not None and not isinstance(pre_trade_config, PreTradeGateConfig):
        reasons.append("pre_trade_config_invalid")
        effective_pretrade_config = PreTradeGateConfig()
    else:
        effective_pretrade_config = pre_trade_config or PreTradeGateConfig()
    config_digest = _canonical_digest(
        {"pre_trade_gate": effective_pretrade_config.to_dict()}
    )

    ledger = funded_ledger if isinstance(funded_ledger, Mapping) else {}
    rebuilt = build_portfolio_snapshot(
        ledger,
        profile,
        as_of=as_of_text,
        valuation_price_basis="raw",
    )
    if "funded_ledger_identity_missing" in rebuilt.reason_codes:
        reasons.append("funded_ledger_identity_missing")
    for code in rebuilt.reason_codes:
        if code != "funded_ledger_identity_missing":
            reasons.append(code)

    if snapshot.available_cash is None or _finite(snapshot.available_cash) is None:
        reasons.append("cash_evidence_missing")
    if snapshot.net_equity is None or _finite(snapshot.net_equity, positive=True) is None:
        reasons.append("equity_evidence_missing")
    if snapshot.snapshot_digest != rebuilt.snapshot_digest or snapshot != rebuilt:
        reasons.append("portfolio_snapshot_digest_mismatch")
    if not (
        intent.portfolio_source_hash
        and intent.portfolio_source_hash == rebuilt.source_hash
        and intent.portfolio_source_hash == snapshot.source_hash
        and intent.portfolio_snapshot_digest
        and intent.portfolio_snapshot_digest == rebuilt.snapshot_digest
        and intent.portfolio_snapshot_digest == snapshot.snapshot_digest
    ):
        reasons.append("paper_intent_portfolio_binding_mismatch")
    if not intent.intent_digest:
        reasons.append("paper_intent_digest_missing")
    elif (
        not _SHA256_RE.fullmatch(str(intent.intent_digest))
        or intent.intent_digest != compute_paper_order_intent_digest(intent)
    ):
        reasons.append("paper_intent_digest_mismatch")

    side = str(intent.side or "").strip().lower()
    if side not in {"buy", "sell"}:
        reasons.append("paper_intent_side_invalid")

    if (
        intent.status != "pending_risk_review"
        or intent.evidence_eligible is not True
        or intent.risk_gate_status != "pending"
        or intent.execution_status != "not_submitted"
        or intent.as_of != snapshot.as_of
        or intent.as_of != as_of_text
        or intent.data_cutoff != snapshot.data_cutoff
        or not _SHA256_RE.fullmatch(str(intent.source_hash or ""))
        or intent.reference_price_basis != "raw"
        or intent.available_cash_before != snapshot.available_cash
    ):
        reasons.append("paper_intent_envelope_invalid")
    intent_quantity = _finite(intent.quantity, positive=True)
    intent_price = _finite(intent.reference_price, positive=True)
    intent_gross = _finite(intent.estimated_gross, positive=True)
    intent_fee = _finite(intent.estimated_fee)
    intent_cash_out = _finite(intent.estimated_cash_out, positive=(side != "sell"))
    intent_cash_before = _finite(intent.available_cash_before)
    intent_cash_after = _finite(intent.available_cash_after)
    intent_cutoff = _parse_day(intent.data_cutoff)
    expected_cost = None
    cash_flow_valid = False
    if (
        intent_price is not None
        and intent_quantity is not None
        and intent_quantity.is_integer()
    ):
        if side == "buy":
            expected_cost = buy_cash_outflow(price=intent_price, quantity=int(intent_quantity))
            cash_flow_valid = bool(
                intent_cash_out is not None
                and intent_cash_before is not None
                and intent_cash_after is not None
                and math.isclose(intent_cash_out, float(expected_cost["cash_out"]), rel_tol=0.0, abs_tol=1e-6)
                and intent_cash_out <= intent_cash_before + 1e-9
                and math.isclose(
                    intent_cash_after,
                    max(intent_cash_before - intent_cash_out, 0.0),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            )
        elif side == "sell":
            avg_cost = 0.0
            coverage = intent.coverage if isinstance(intent.coverage, Mapping) else {}
            avg_value = _finite(coverage.get("avg_cost"))
            if avg_value is not None:
                avg_cost = avg_value
            expected_cost = sell_cash_inflow(
                price=intent_price,
                quantity=int(intent_quantity),
                avg_cost=avg_cost,
            )
            cash_in = float(expected_cost["cash_in"])
            cash_flow_valid = bool(
                intent_cash_out is not None
                and intent_cash_before is not None
                and intent_cash_after is not None
                and math.isclose(intent_cash_out, -cash_in, rel_tol=0.0, abs_tol=1e-6)
                and math.isclose(
                    intent_cash_after,
                    intent_cash_before + cash_in,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            )
    if not (
        intent_quantity is not None
        and intent_quantity.is_integer()
        and intent_price is not None
        and intent_gross is not None
        and intent_fee is not None
        and intent_fee >= 0
        and expected_cost is not None
        and math.isclose(intent_gross, float(expected_cost["gross"]), rel_tol=0.0, abs_tol=1e-6)
        and math.isclose(intent_fee, float(expected_cost["total_fee"]), rel_tol=0.0, abs_tol=1e-6)
        and cash_flow_valid
        and intent_cutoff is not None
        and as_of_day is not None
        and intent_cutoff <= as_of_day
        and bool(str(intent.source_path or "").strip())
        and not intent.reason_codes
    ):
        reasons.append("paper_intent_envelope_invalid")
    intent_policy = intent.price_policy_validation
    if not (
        intent.price_data_contract_version == rebuilt.price_data_contract_version
        and isinstance(intent_policy, Mapping)
        and intent_policy.get("status") == "passed"
        and intent_policy.get("eligible") is True
        and intent_policy.get("contract_version") == rebuilt.price_data_contract_version
        and intent_policy.get("consumer") == "paper_execution"
        and intent_policy.get("observed_basis") == "raw"
        and intent_policy.get("accepted_price_basis") == "raw"
    ):
        reasons.append("paper_intent_price_contract_invalid")
    if side == "sell":
        pit_ok = (
            isinstance(intent.pit_status, Mapping)
            and intent.pit_status.get("status") == "not_applicable_funded_position_exit"
        )
    else:
        pit_ok = isinstance(intent.pit_status, Mapping) and intent.pit_status.get("status") == "passed"
    if not pit_ok:
        reasons.append("paper_intent_pit_invalid")
    intent_coverage = intent.coverage
    coverage_ok = False
    if isinstance(intent_coverage, Mapping) and side == "buy":
        coverage_value = _finite(intent_coverage.get("coverage"))
        minimum_coverage = _finite(intent_coverage.get("minimum_coverage"))
        coverage_ok = bool(
            intent_coverage.get("kind") == "candidate_decision_event"
            and coverage_value is not None
            and minimum_coverage is not None
            and coverage_value >= minimum_coverage
        )
    elif isinstance(intent_coverage, Mapping) and side == "sell":
        held_quantity = _finite(intent_coverage.get("held_quantity"))
        sellable_quantity = _finite(intent_coverage.get("sellable_quantity"))
        coverage_ok = bool(
            intent_coverage.get("kind") == "funded_position_exit"
            and held_quantity is not None
            and sellable_quantity is not None
            and intent_quantity is not None
            and held_quantity >= intent_quantity
            and sellable_quantity >= intent_quantity
            and _strict_text(intent_coverage.get("position_digest")) is not None
        )
    if not coverage_ok:
        reasons.append("paper_intent_coverage_invalid")

    evidence_map = evidence if isinstance(evidence, Mapping) else {}
    if not isinstance(evidence, Mapping):
        reasons.append("risk_evidence_missing")
    price_evidence = _validate_component(
        evidence_map, "price", as_of_day=as_of_day, reasons=reasons
    )
    sector_evidence = _validate_component(
        evidence_map, "sector", as_of_day=as_of_day, reasons=reasons
    )
    liquidity_evidence = _validate_component(
        evidence_map, "liquidity", as_of_day=as_of_day, reasons=reasons
    )
    returns_evidence = _validate_component(
        evidence_map, "returns", as_of_day=as_of_day, reasons=reasons
    )
    smart_stop_evidence = _validate_component(
        evidence_map, "smart_stop", as_of_day=as_of_day, reasons=reasons
    )

    price = _finite(price_evidence.get("value"), positive=True)
    if price is None:
        reasons.append("execution_price_invalid")
    if str(price_evidence.get("symbol") or "") != intent.symbol:
        reasons.append("execution_price_symbol_mismatch")
    if str(price_evidence.get("price_basis") or "").strip().lower() != "raw":
        reasons.append("execution_price_basis_not_raw")
    if str(price_evidence.get("consumer") or "").strip() != "paper_execution":
        reasons.append("execution_price_consumer_invalid")
    if price is not None and not math.isclose(
        price, float(intent.reference_price), rel_tol=0.0, abs_tol=1e-9
    ):
        reasons.append("execution_price_mismatch")

    if (
        str(sector_evidence.get("symbol") or "") != intent.symbol
        or _strict_text(sector_evidence.get("value")) is None
    ):
        reasons.append("sector_evidence_invalid")
    amount = _finite(liquidity_evidence.get("amount"))
    score = _finite(liquidity_evidence.get("liquidity_score"))
    if (
        str(liquidity_evidence.get("symbol") or "") != intent.symbol
        or amount is None
        or amount < 0
        or score is None
        or score < 0
    ):
        reasons.append("liquidity_evidence_invalid")

    positions = _funded_positions(ledger)
    required_assets = sorted(
        {
            intent.symbol,
            *(str(row.get("symbol") or row.get("code") or "").strip() for row in positions),
        }
        - {""}
    )
    raw_sector_map = sector_evidence.get("by_symbol")
    sector_by_symbol: dict[str, str] = {}
    if not isinstance(raw_sector_map, Mapping):
        reasons.append("sector_asset_missing")
    else:
        for asset in required_assets:
            sector = _strict_text(raw_sector_map.get(asset))
            if sector is None:
                reasons.append("sector_asset_missing")
            else:
                sector_by_symbol[asset] = sector
    target_sector = sector_by_symbol.get(intent.symbol, "")
    target_label = _strict_text(sector_evidence.get("value"))
    if target_sector and target_sector != target_label:
        reasons.append("sector_target_mismatch")
    for position in positions:
        symbol = str(position.get("symbol") or position.get("code") or "").strip()
        ledger_sector = _strict_text(position.get("sector")) or _strict_text(
            position.get("industry")
        )
        if ledger_sector is None:
            reasons.append("funded_position_sector_missing")
        elif sector_by_symbol.get(symbol) and ledger_sector != sector_by_symbol[symbol]:
            reasons.append("funded_position_sector_mismatch")
    returns_frame = returns_evidence.get("frame")
    returns_summary: dict[str, Any] = {"columns": required_assets, "rows": 0}
    normalized_returns: pd.DataFrame | None = None
    if not isinstance(returns_frame, pd.DataFrame):
        reasons.append("returns_frame_missing")
    else:
        missing_assets = [asset for asset in required_assets if asset not in returns_frame.columns]
        if missing_assets:
            reasons.append("returns_asset_missing")
        elif returns_frame.columns.duplicated().any():
            reasons.append("returns_columns_duplicated")
        else:
            normalized_returns = returns_frame.loc[:, required_assets].apply(
                pd.to_numeric, errors="coerce"
            )
            values = normalized_returns.to_numpy(dtype=float, copy=True)
            if not np.isfinite(values).all():
                reasons.append("returns_non_finite")
            else:
                returns_summary = _returns_summary(returns_frame, required_assets)
            if returns_frame.index.has_duplicates:
                reasons.append("returns_index_duplicated")
            if not returns_frame.index.is_monotonic_increasing:
                reasons.append("returns_time_order_invalid")
            index_days = [_parse_day(value) for value in returns_frame.index]
            if any(day is None for day in index_days):
                reasons.append("returns_index_invalid")
            elif as_of_day is not None and any(day > as_of_day for day in index_days if day):
                reasons.append("returns_evidence_after_as_of")

    daily_rows_by_symbol = smart_stop_evidence.get("daily_rows_by_symbol")
    sector_status_by_symbol = smart_stop_evidence.get("sector_status_by_symbol")
    if not isinstance(daily_rows_by_symbol, Mapping):
        reasons.append("smart_stop_daily_rows_missing")
        daily_rows_by_symbol = {}
    if not isinstance(sector_status_by_symbol, Mapping):
        reasons.append("smart_stop_sector_status_missing")
        sector_status_by_symbol = {}
    for position in positions:
        symbol = str(position.get("symbol") or position.get("code") or "").strip()
        rows = daily_rows_by_symbol.get(symbol)
        sector_status = sector_status_by_symbol.get(symbol)
        if not isinstance(rows, list) or not rows:
            reasons.append("smart_stop_daily_rows_missing")
            continue
        if not isinstance(sector_status, Mapping) or not sector_status:
            reasons.append("smart_stop_sector_status_missing")
        for row in rows:
            row_day = _parse_day(row.get("date") if isinstance(row, Mapping) else None)
            if row_day is None:
                reasons.append("smart_stop_row_invalid")
                break
            if as_of_day is not None and row_day > as_of_day:
                reasons.append("smart_stop_evidence_after_as_of")
                break

    evidence_summary = {
        component: {
            "data_cutoff": value.get("data_cutoff"),
            "source_path": value.get("source_path"),
            "source_hash": value.get("source_hash"),
            "content_hash": compute_risk_evidence_source_hash(value),
        }
        for component, value in (
            ("price", price_evidence),
            ("sector", sector_evidence),
            ("liquidity", liquidity_evidence),
            ("returns", returns_evidence),
            ("smart_stop", smart_stop_evidence),
        )
    }
    evidence_summary["price"].update(
        {
            "symbol": price_evidence.get("symbol"),
            "value": price_evidence.get("value"),
            "price_basis": price_evidence.get("price_basis"),
            "consumer": price_evidence.get("consumer"),
        }
    )
    evidence_summary["sector"].update(
        {
            "symbol": sector_evidence.get("symbol"),
            "value": sector_evidence.get("value"),
            "by_symbol": deepcopy(dict(raw_sector_map))
            if isinstance(raw_sector_map, Mapping)
            else {},
        }
    )
    evidence_summary["liquidity"].update(
        {
            "symbol": liquidity_evidence.get("symbol"),
            "amount": liquidity_evidence.get("amount"),
            "liquidity_score": liquidity_evidence.get("liquidity_score"),
        }
    )
    evidence_summary["returns_frame"] = returns_summary
    cutoff_days = [
        day
        for day in (
            _parse_day(snapshot.data_cutoff),
            _parse_day(intent.data_cutoff),
            *(
                _parse_day(component.get("data_cutoff"))
                for component in (
                    price_evidence,
                    sector_evidence,
                    liquidity_evidence,
                    returns_evidence,
                    smart_stop_evidence,
                )
            ),
        )
        if day is not None
    ]
    decision_cutoff = min(cutoff_days).isoformat() if cutoff_days else ""

    if reasons:
        return _build_decision(
            status="insufficient_evidence",
            reasons=reasons,
            checks=checks,
            reviews=reviews,
            intent=intent,
            as_of=as_of_text,
            evidence_eligible=False,
            evidence_summary=evidence_summary,
            llm_review=llm_review,
            snapshot=snapshot,
            ledger_source_hash=rebuilt.source_hash,
            config_digest=config_digest,
            data_cutoff=decision_cutoff,
        )

    equity = float(snapshot.net_equity)
    order_amount = float(intent.estimated_gross)
    current_positions, aggregates = _positions_for_checks(
        positions,
        equity=equity,
        sector_by_symbol=sector_by_symbol,
    )
    if side == "buy":
        pretrade_passed, pretrade_results = run_pre_trade_checks(
            symbol=intent.symbol,
            order_amount=order_amount,
            industry=sector_by_symbol[intent.symbol],
            current_equity=equity,
            current_market_value=float(snapshot.market_value),
            current_positions=current_positions,
            current_position_count=len(current_positions),
            config=effective_pretrade_config,
        )
        checks["pre_trade"] = {
            "passed": pretrade_passed,
            "checks": [result.to_dict() for result in pretrade_results],
        }
        if not pretrade_passed:
            reasons.append("pre_trade_gate_veto")
    else:
        checks["pre_trade"] = {
            "passed": True,
            "side": "sell",
            "status": "not_applicable_exposure_reducing_intent",
            "checks": [],
        }

    target = aggregates.setdefault(
        intent.symbol,
        {
            "symbol": intent.symbol,
            "market_value": 0.0,
            "sector": sector_by_symbol[intent.symbol],
        },
    )
    if side == "buy":
        target["market_value"] += order_amount
    else:
        target["market_value"] = max(0.0, float(target["market_value"]) - order_amount)
    target["sector"] = sector_by_symbol[intent.symbol]
    post_positions = [
        {
            "symbol": symbol,
            "sector": row.get("sector") or "unknown",
            "weight": float(row["market_value"]) / equity,
        }
        for symbol, row in sorted(aggregates.items())
        if float(row["market_value"]) > 0
    ]
    concentration = evaluate_concentration(post_positions)
    checks["concentration"] = concentration
    if side == "buy" and concentration.get("alert_count"):
        reasons.append("concentration_veto")

    liquidity = evaluate_liquidity_warning(dict(liquidity_evidence))
    checks["liquidity"] = liquidity
    if liquidity.get("warnings"):
        reasons.append("liquidity_veto")

    weights = {row["symbol"]: float(row["weight"]) for row in post_positions}
    var_result = portfolio_var(weights, normalized_returns)
    checks["var"] = var_result
    if var_result.get("error") == "insufficient_observations":
        if side == "buy":
            reasons.append("returns_insufficient_observations")
            return _build_decision(
                status="insufficient_evidence",
                reasons=reasons,
                checks=checks,
                reviews=reviews,
                intent=intent,
                as_of=as_of_text,
                evidence_eligible=False,
                evidence_summary=evidence_summary,
                llm_review=llm_review,
                snapshot=snapshot,
                ledger_source_hash=rebuilt.source_hash,
                config_digest=config_digest,
                data_cutoff=decision_cutoff,
            )
    if var_result.get("error"):
        if side == "buy":
            reasons.append("var_evidence_invalid")
            return _build_decision(
                status="insufficient_evidence",
                reasons=reasons,
                checks=checks,
                reviews=reviews,
                intent=intent,
                as_of=as_of_text,
                evidence_eligible=False,
                evidence_summary=evidence_summary,
                llm_review=llm_review,
                snapshot=snapshot,
                ledger_source_hash=rebuilt.source_hash,
                config_digest=config_digest,
                data_cutoff=decision_cutoff,
            )

    for position in positions:
        symbol = str(position.get("symbol") or position.get("code") or "").strip()
        item = build_smart_stop_for_position(
            dict(position),
            daily_rows=list(daily_rows_by_symbol[symbol]),
            sector_status=dict(sector_status_by_symbol[symbol]),
            as_of=as_of_text,
        )
        reviews.append(
            {
                "symbol": symbol,
                "status": str(item.get("trigger_status") or "unknown"),
                "holding_days": int(item.get("holding_days") or 0),
                "reasons": [str(reason) for reason in item.get("rationale") or []],
            }
        )

    status = "vetoed" if reasons else "passed"
    return _build_decision(
        status=status,
        reasons=reasons,
        checks=checks,
        reviews=reviews,
        intent=intent,
        as_of=as_of_text,
        evidence_eligible=True,
        evidence_summary=evidence_summary,
        llm_review=llm_review,
        snapshot=snapshot,
        ledger_source_hash=rebuilt.source_hash,
        config_digest=config_digest,
        data_cutoff=decision_cutoff,
    )


__all__ = [
    "RiskGateDecision",
    "compute_risk_gate_decision_digest",
    "compute_risk_gate_decision_source_hash",
    "compute_risk_evidence_source_hash",
    "evaluate_paper_risk_gate",
    "validate_risk_gate_decision_binding",
]
