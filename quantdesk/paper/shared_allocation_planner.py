"""Shared paper-capital allocation planner (research-only, paper-only).

Stage 1 goal: turn existing research recommendations into an auditable
*shared paper-capital plan* over a small default budget (10,000 simulated CNY).
It only *plans* paper seats; it never places real orders, never connects a
broker, never mutates the existing paper-portfolio state, and never executes the
plan in this stage.

Design contract:

* Reuse the canonical candidate contract, the existing auto paper-portfolio
  state, and the completed-minute-pool capability gate. Do not invent a second
  account or bypass existing trade gates.
* Strategy attribution is restricted to ``long`` / ``swing`` / ``board``; a
  single symbol may belong to only one strategy per plan, with a recorded
  reason when several strategies hit it.
* Feasible, data-valid *long* research recommendations get at least one reserved
  100-share paper seat before the remaining cash is planned by
  ``board -> swing -> additional long`` priority.
* Nothing is fabricated. When price, cash, the 100-share lot, price basis, or
  data provenance is unusable, the symbol goes to ``blocked_or_waiting`` (or, for
  a ``board`` symbol missing verified minute coverage, to
  ``data_request_suggested``) instead of a paper seat.
* Every research recommendation is retained in the output; unselected ones must
  carry an explicit reason.
* Temporary / synthetic / proxy / unknown data roots degrade or block the plan
  and can never overwrite the canonical ``*_latest`` evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quantdesk.candidate_contract import (
    candidate_rows_from_strategy_payload,
    normalize_strategy_type,
)
from quantdesk.paper.market_rules import lot_size_for_symbol
from quantdesk.intraday.minute_pool_capability import minute_pool_capability_source_hash
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.paper.trade_costs import buy_cash_outflow
from quantdesk.price_basis import price_data_contract
from quantdesk.research.strategy_viability_runner import build_report_truthfulness
from quantdesk.shared import normalize_symbol, now_text, safe_float, safe_int
from quantdesk.utils import atomic_write_json, read_json_safe
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_RUNS_DIR = RESULTS_DIR / "runs"
REPORTS_DIR = PROJECT_ROOT / "reports"
STATE_DIR = PROJECT_ROOT / "state"

AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = RESULTS_DIR / "auto_paper_portfolio_latest.json"
SHARED_PAPER_PLAN_LATEST_JSON = RESULTS_DIR / "shared_paper_plan_latest.json"
SHARED_PAPER_PLAN_LATEST_MD = REPORTS_DIR / "shared_paper_plan_latest.md"

DEFAULT_INITIAL_CASH = 10_000.0
DEFAULT_LOT_SIZE = 100

DISCLAIMER = "仅用于本地策略研究与共享纸面资金规划模拟，不构成投资建议，也不代表真实收益。"
BROKER_BOUNDARY = "不接券商，不真实下单，不保存账户密码，本阶段只生成计划、不执行。"

# Strategy attribution allowed for the shared plan. long_swing/board_momentum
# collapse into the coarse swing/board buckets the plan speaks in.
PLANNER_STRATEGY_LABELS = {"long": "长线", "swing": "波段", "board": "打板"}
# Precedence used only as a stable tie-break when several strategies hit one
# symbol with equal research evidence.
_STRATEGY_TIEBREAK_ORDER = {"long": 0, "swing": 1, "board": 2}
# Order in which remaining cash is planned after the reserved long seat.
_REMAINING_FILL_ORDER = ("board", "swing", "long")

# Data-quality labels that are not trustworthy enough to plan a paper seat on.
_UNUSABLE_DATA_QUALITY = {
    "",
    "unknown",
    "missing",
    "temp",
    "temporary",
    "synthetic",
    "proxy",
    "proxy_only",
    "staged",
    "pending",
}
def normalize_planner_strategy(value: Any) -> str:
    """Collapse the canonical strategy vocabulary into long/swing/board."""

    canonical = normalize_strategy_type(value, "long")
    if canonical in {"long_swing"}:
        return "swing"
    if canonical in {"board", "board_momentum"}:
        return "board"
    if canonical == "swing":
        return "swing"
    return "long"


def _research_rank_key(candidate: dict[str, Any]) -> tuple[float, float]:
    """Sort key (descending) by research evidence: score first, then -rank."""

    score = safe_float(candidate.get("score"), None)
    rank = safe_int(candidate.get("rank"), None)
    score_component = score if score is not None else float("-inf")
    # Lower rank == stronger; convert so a larger key means stronger evidence.
    rank_component = (-float(rank)) if rank is not None else float("-inf")
    return (score_component, rank_component)


@dataclass
class SharedAllocationConfig:
    """Config for a single shared paper-capital planning run."""

    initial_cash: float = DEFAULT_INITIAL_CASH
    lot_size: int = DEFAULT_LOT_SIZE
    strategy: str = "all"
    data_root: str | Path | None = None
    write_latest: bool = True
    smoke_run: bool = False
    synthetic: bool = False


@dataclass
class _PlannerCandidate:
    """Normalized, plan-facing view of one research recommendation."""

    symbol: str
    name: str
    strategy: str
    score: float | None
    rank: int | None
    price: float | None
    price_basis: str
    adjustment_basis: str
    data_quality: str
    latest_date: str
    reason: str
    adjustment_warnings: list[str] = field(default_factory=list)
    conflict_dropped_strategies: list[str] = field(default_factory=list)

    def base_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "strategy_type": self.strategy,
            "strategy_label": PLANNER_STRATEGY_LABELS.get(self.strategy, self.strategy),
            "score": self.score,
            "rank": self.rank,
            "price": self.price,
            "price_basis": self.price_basis,
            "adjustment_basis": self.adjustment_basis,
            "data_quality": self.data_quality,
            "latest_date": self.latest_date,
            "adjustment_warnings": list(self.adjustment_warnings),
            "research_reason": self.reason,
        }


def _candidate_price(candidate: dict[str, Any]) -> float | None:
    for key in ("close", "latest_price", "price", "entry_price"):
        number = safe_float(candidate.get(key), None)
        if number is not None and number > 0:
            return number
    return None


def _clean_str(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_planner_candidate(candidate: dict[str, Any]) -> _PlannerCandidate:
    symbol = normalize_symbol(candidate.get("symbol") or candidate.get("code"))
    warnings_raw = candidate.get("adjustment_warnings")
    warnings = [str(item) for item in warnings_raw] if isinstance(warnings_raw, list) else []
    return _PlannerCandidate(
        symbol=symbol,
        name=_clean_str(candidate.get("name")) or symbol,
        strategy=normalize_planner_strategy(candidate.get("strategy_type") or candidate.get("strategy")),
        score=safe_float(candidate.get("score"), None),
        rank=safe_int(candidate.get("rank"), None),
        price=_candidate_price(candidate),
        price_basis=_clean_str(candidate.get("price_basis")).lower(),
        adjustment_basis=_clean_str(candidate.get("adjustment_basis")),
        data_quality=_clean_str(candidate.get("data_quality")).lower(),
        latest_date=_clean_str(candidate.get("latest_date") or candidate.get("data_latest_date"))[:10],
        reason=_clean_str(candidate.get("reason") or candidate.get("signal_reason")),
        adjustment_warnings=warnings,
    )


def _resolve_strategy_conflicts(
    candidates: list[_PlannerCandidate],
) -> list[_PlannerCandidate]:
    """Keep one strategy per symbol; record which strategies were dropped.

    Winner = strongest research evidence (score, then rank); ties break on a
    stable long -> swing -> board precedence so results are deterministic.
    """

    by_symbol: dict[str, list[_PlannerCandidate]] = {}
    order: list[str] = []
    for item in candidates:
        if item.symbol not in by_symbol:
            by_symbol[item.symbol] = []
            order.append(item.symbol)
        by_symbol[item.symbol].append(item)

    resolved: list[_PlannerCandidate] = []
    for symbol in order:
        group = by_symbol[symbol]
        if len(group) == 1:
            resolved.append(group[0])
            continue
        winner = sorted(
            group,
            key=lambda c: (
                _research_rank_key(c.base_row()),
                -_STRATEGY_TIEBREAK_ORDER.get(c.strategy, 99),
            ),
            reverse=True,
        )[0]
        dropped = [c.strategy for c in group if c is not winner]
        winner.conflict_dropped_strategies = sorted(set(dropped))
        resolved.append(winner)
    return resolved


def _assess_candidate_data(candidate: _PlannerCandidate) -> dict[str, Any]:
    """Judge whether the candidate's price/basis/quality can back a paper seat."""

    degrade_warnings: list[str] = []
    block_reason = ""

    if candidate.price is None or candidate.price <= 0:
        block_reason = "缺少可用价格，无法规划纸面席位。"
        return {"usable": False, "block_reason": block_reason, "degrade_warnings": degrade_warnings}

    canonical_basis = _canonical_price_basis(candidate.price_basis)
    if canonical_basis == "mixed_or_unknown":
        block_reason = "价格口径未知，无法确认成交价基准，暂不进入纸面计划。"
        return {"usable": False, "block_reason": block_reason, "degrade_warnings": degrade_warnings}

    if canonical_basis != "raw":
        block_reason = "纸面资金规划只接受 raw 未复权估值价格；复权价格仅保留为研究观察。"
        return {"usable": False, "block_reason": block_reason, "degrade_warnings": degrade_warnings}

    if candidate.data_quality in _UNUSABLE_DATA_QUALITY:
        block_reason = f"数据质量不可用（{candidate.data_quality or 'unknown'}），暂不进入纸面计划。"
        return {"usable": False, "block_reason": block_reason, "degrade_warnings": degrade_warnings}

    # Raw / unadjusted daily is usable for close-after research planning but must
    # surface the adjustment-basis degrade warning so it is never read as a
    # clean fill assumption.
    if candidate.data_quality in {"raw_unadjusted", "raw"} or candidate.price_basis in {"raw_close", "raw"}:
        degrade_warnings.append(
            f"{candidate.symbol}: 使用未复权日线价（{candidate.price_basis or 'raw'}）作为纸面规划基准，仅供收盘后研究计划参考。"
        )
    for warning in candidate.adjustment_warnings:
        degrade_warnings.append(f"{candidate.symbol}: {warning}")

    return {"usable": True, "block_reason": "", "degrade_warnings": degrade_warnings}


def _lot_cost(price: float, lot_size: int) -> float:
    info = buy_cash_outflow(price=price, quantity=lot_size)
    return float(safe_float(info.get("cash_out"), price * lot_size) or price * lot_size)


def _covered_by_minute_pool(
    symbol: str,
    minute_pool: dict[str, Any] | None,
    *,
    evidence_eligible: bool,
) -> bool:
    if (
        not evidence_eligible
        or not isinstance(minute_pool, dict)
        or not minute_pool.get("passed")
    ):
        return False
    symbols = minute_pool.get("symbols")
    if isinstance(symbols, set):
        return symbol in symbols
    if isinstance(symbols, (list, tuple)):
        return symbol in set(symbols)
    return False


def _existing_position_symbols(paper_state: dict[str, Any] | None) -> set[str]:
    symbols: set[str] = set()
    if not isinstance(paper_state, dict):
        return symbols
    for row in paper_state.get("positions") or []:
        if isinstance(row, dict):
            symbol = normalize_symbol(row.get("symbol") or row.get("code"))
            if symbol:
                symbols.add(symbol)
    return symbols


def _maintained_positions(paper_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(paper_state, dict):
        return rows
    for row in paper_state.get("positions") or []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        rows.append({
            "symbol": symbol,
            "name": _clean_str(row.get("name")) or symbol,
            "strategy_type": normalize_planner_strategy(row.get("strategy_type")),
            "quantity": safe_int(row.get("quantity"), 0) or 0,
            "entry_price": safe_float(row.get("entry_price"), None),
            "maintain_reason": "已在纸面组合中持有，本阶段维持不动，不自动加减仓，也不纳入新资金规划。",
            "no_auto_sell": True,
            "no_auto_add": True,
        })
    return rows


def _dataset_stats(candidates: list[_PlannerCandidate]) -> dict[str, Any]:
    dates = sorted({c.latest_date for c in candidates if c.latest_date})
    bases = sorted({c.price_basis for c in candidates if c.price_basis})
    qualities = sorted({c.data_quality for c in candidates if c.data_quality})
    return {
        "symbol_count": len(candidates),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "price_basis_values": bases,
        "data_quality_values": qualities,
    }


def _planning_cash(
    paper_state: dict[str, Any] | None,
) -> tuple[float, str]:
    """Resolve funded-ledger cash without falling back on capital when it is missing."""

    if not isinstance(paper_state, dict) or not paper_state:
        return 0.0, "funded_ledger_missing"
    candidate_markers = {
        "symbol",
        "code",
        "strategy_type",
        "score",
        "decision_origin",
        "effective_state",
        "requested_state",
        "funded",
    }
    if any(key in paper_state for key in candidate_markers) or not isinstance(
        paper_state.get("positions"),
        list,
    ):
        return 0.0, "funded_ledger_invalid_shape"
    account = paper_state.get("account")
    source = account if isinstance(account, dict) else paper_state
    ledger_id = str(
        paper_state.get("ledger_id") or paper_state.get("portfolio_id") or ""
    ).strip()
    schema_version = str(paper_state.get("schema_version") or "").strip()
    profile_id = str(source.get("profile_id") or "").strip()
    scenario_id = str(source.get("scenario_id") or "").strip()
    initial_capital = safe_float(
        source.get("initial_cash", source.get("initial_capital")),
        None,
    )
    legacy_identity = bool(
        initial_capital is not None
        and abs(float(initial_capital) - 100_000.0) <= 1e-9
        and paper_state.get("paper_trading_only") is True
        and not profile_id
        and not scenario_id
    )
    structured_identity = bool(
        schema_version
        and (
            paper_state.get("paper_trading_only") is True
            or (profile_id and scenario_id)
        )
    )
    if not (ledger_id or structured_identity or legacy_identity):
        return 0.0, "funded_ledger_identity_missing"
    for key in ("available_cash", "cash"):
        if key not in source:
            continue
        raw_value = source.get(key)
        value = safe_float(raw_value, None)
        if isinstance(raw_value, bool) or value is None or value < 0:
            return 0.0, f"funded_ledger.{key}_invalid"
        return float(value), f"funded_ledger.{key}"
    return 0.0, "funded_ledger.cash_missing"


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _iso_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _strategy_payload_hash(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("source_hash", None)
    return _canonical_hash(canonical)


def _strategy_evidence_reasons(
    payload: dict[str, Any],
    *,
    candidate_count: int,
) -> list[str]:
    reasons: list[str] = []
    contract = price_data_contract()
    contract_version = str(contract.get("version") or "")
    policy = payload.get("price_policy_validation")
    pit = payload.get("pit_status")
    coverage = payload.get("coverage")
    if payload.get("schema_version") != API_SCHEMA_VERSION:
        reasons.append("strategy_schema_version_invalid")
    if not str(payload.get("source_path") or "").strip():
        reasons.append("strategy_source_path_missing")
    if payload.get("source_hash") != _strategy_payload_hash(payload):
        reasons.append("strategy_source_hash_mismatch")
    if payload.get("price_data_contract_version") != contract_version:
        reasons.append("strategy_price_contract_version_invalid")
    if not isinstance(policy, dict) or not (
        policy.get("status") == "passed"
        and policy.get("eligible") is True
        and policy.get("contract_version") == contract_version
        and policy.get("consumer") == "paper_valuation"
        and policy.get("observed_basis") == "raw"
        and policy.get("accepted_price_basis") == "raw"
        and isinstance(policy.get("consumer_policy"), dict)
    ):
        reasons.append("strategy_price_policy_invalid")
    if not isinstance(pit, dict) or not (
        pit.get("status") == "passed" and pit.get("eligible") is True
    ):
        reasons.append("strategy_pit_evidence_invalid")
    if not isinstance(coverage, dict) or (
        safe_int(coverage.get("candidate_count"), -1) != candidate_count
    ):
        reasons.append("strategy_coverage_invalid")
    if payload.get("evidence_eligible") is not True:
        reasons.append("strategy_evidence_not_eligible")

    generated_day = _iso_day(payload.get("as_of") or payload.get("generated_at"))
    cutoff_day = _iso_day(payload.get("data_cutoff"))
    if generated_day is None or cutoff_day is None:
        reasons.append("strategy_evidence_date_invalid")
    elif cutoff_day > generated_day:
        reasons.append("future_strategy_data_cutoff")
    if cutoff_day is not None:
        for row in candidate_rows_from_strategy_payload(payload, strategy="all"):
            row_day = _iso_day(row.get("latest_date") or row.get("data_cutoff"))
            if row_day is not None and row_day > cutoff_day:
                reasons.append("candidate_after_strategy_data_cutoff")
                break
    if reasons:
        reasons.append("strategy_evidence_envelope_invalid")
    return list(dict.fromkeys(reasons))


def _minute_pool_evidence_reasons(
    minute_pool: dict[str, Any] | None,
    *,
    required: bool,
    strategy_cutoff: date | None,
) -> list[str]:
    if not required:
        return []
    reasons: list[str] = []
    pool = minute_pool if isinstance(minute_pool, dict) else {}
    symbols = pool.get("symbols")
    symbol_set = set(symbols) if isinstance(symbols, (set, list, tuple)) else set()
    if pool.get("schema_version") != API_SCHEMA_VERSION:
        reasons.append("minute_pool_schema_version_invalid")
    if pool.get("task_type") != "verified_minute_pool_snapshot":
        reasons.append("minute_pool_task_type_invalid")
    if not (
        pool.get("passed") is True
        and pool.get("status") == "passed"
        and pool.get("quality_status") == "passed"
        and pool.get("snapshot_available") is True
    ):
        reasons.append("minute_pool_quality_not_passed")
    if not str(pool.get("snapshot_path") or "").strip() or not str(
        pool.get("job_path") or ""
    ).strip():
        reasons.append("minute_pool_provenance_missing")
    if pool.get("source_snapshot") not in {"latest", "last_passed"}:
        reasons.append("minute_pool_source_snapshot_invalid")
    pool_day = _iso_day(pool.get("target_date"))
    if pool_day is None:
        reasons.append("minute_pool_target_date_invalid")
    elif strategy_cutoff is not None and pool_day < strategy_cutoff:
        reasons.append("minute_pool_target_date_stale")
    elif strategy_cutoff is not None and pool_day > strategy_cutoff:
        reasons.append("future_minute_pool_target_date")
    count_values = (
        safe_int(pool.get("total_symbols"), -1),
        safe_int(pool.get("usable_count"), -1),
        safe_int(pool.get("symbol_count"), -1),
    )
    if (
        not symbol_set
        or count_values[0] != len(symbol_set)
        or count_values[1] != len(symbol_set)
        or count_values[2] != len(symbol_set)
        or any(safe_int(pool.get(key), -1) != 0 for key in ("partial_count", "insufficient_count", "invalid_count", "missing_count"))
    ):
        reasons.append("minute_pool_count_mismatch")
    if not (
        pool.get("read_only") is True
        and pool.get("network_used") is False
        and pool.get("canonical_write") is False
    ):
        reasons.append("minute_pool_boundary_invalid")
    if pool.get("source_hash") != minute_pool_capability_source_hash(pool):
        reasons.append("minute_pool_source_hash_mismatch")
    if reasons:
        reasons.append("minute_pool_evidence_invalid")
    return list(dict.fromkeys(reasons))


def _plan_digest(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("plan_digest", None)
    canonical.pop("output_paths", None)
    return _canonical_hash(canonical)


def _canonical_price_basis(value: str) -> str:
    basis = str(value or "").strip().lower()
    if basis in {"raw", "raw_close", "unadjusted"}:
        return "raw"
    if basis in {"forward_adjusted", "qfq"}:
        return "forward_adjusted"
    if basis in {"back_adjusted", "hfq"}:
        return "back_adjusted"
    return "mixed_or_unknown"


def plan_shared_paper_allocation(
    *,
    config: SharedAllocationConfig | None = None,
    auto_strategy_payload: dict[str, Any] | None = None,
    paper_state: dict[str, Any] | None = None,
    minute_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared paper-capital plan payload (pure; no disk writes)."""

    config = config or SharedAllocationConfig()
    lot_size = max(1, int(config.lot_size))
    configured_initial_cash = float(config.initial_cash)
    initial_cash, cash_source = _planning_cash(paper_state)

    strategy_payload = auto_strategy_payload if isinstance(auto_strategy_payload, dict) else {}
    raw_rows = candidate_rows_from_strategy_payload(strategy_payload, strategy="all")
    planner_candidates = [_to_planner_candidate(row) for row in raw_rows]
    if config.strategy in PLANNER_STRATEGY_LABELS:
        planner_candidates = [c for c in planner_candidates if c.strategy == config.strategy]
    planner_candidates = _resolve_strategy_conflicts(planner_candidates)

    dataset_stats = _dataset_stats(planner_candidates)
    strategy_evidence_reasons = _strategy_evidence_reasons(
        strategy_payload,
        candidate_count=len(planner_candidates),
    )
    strategy_cutoff = _iso_day(strategy_payload.get("data_cutoff"))
    minute_pool_required = any(
        candidate.strategy == "board" for candidate in planner_candidates
    )
    minute_pool_reasons = _minute_pool_evidence_reasons(
        minute_pool,
        required=minute_pool_required,
        strategy_cutoff=strategy_cutoff,
    )
    minute_pool_evidence_eligible = not minute_pool_reasons
    normalized_price_bases = {
        _canonical_price_basis(candidate.price_basis)
        for candidate in planner_candidates
    }
    cross_symbol_basis_mismatch = len(normalized_price_bases) > 1
    non_raw_price_basis = bool(normalized_price_bases) and normalized_price_bases != {"raw"}
    funded_ledger_missing = cash_source in {
        "funded_ledger_missing",
        "funded_ledger.cash_missing",
        "funded_ledger.available_cash_invalid",
        "funded_ledger.cash_invalid",
        "funded_ledger_invalid_shape",
        "funded_ledger_identity_missing",
    }
    truthfulness = build_report_truthfulness(
        config.data_root,
        write_latest=config.write_latest,
        smoke_run=config.smoke_run,
        synthetic=config.synthetic,
        dataset_symbol_count=dataset_stats["symbol_count"],
        dataset_date_start=dataset_stats["date_start"],
        dataset_date_end=dataset_stats["date_end"],
    )

    existing_symbols = _existing_position_symbols(paper_state)
    maintained = _maintained_positions(paper_state)

    all_recommendations: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    blocked_or_waiting: list[dict[str, Any]] = []
    data_request_suggested: list[dict[str, Any]] = []
    degrade_warnings: list[str] = []
    lot_infeasible_count = 0

    remaining_cash = initial_cash

    # First classify every candidate into an intermediate feasibility record so
    # the two allocation phases can share the same evidence.
    eligible: list[dict[str, Any]] = []  # symbols that can take a paper seat
    for candidate in planner_candidates:
        base = candidate.base_row()
        candidate_lot_size = lot_size_for_symbol(candidate.symbol, name=candidate.name)
        base["lot_size"] = candidate_lot_size
        if candidate.conflict_dropped_strategies:
            base["strategy_conflict_resolution"] = (
                "同一股票命中多个策略，按研究证据（评分/排名）选择 "
                f"{PLANNER_STRATEGY_LABELS.get(candidate.strategy, candidate.strategy)}，"
                f"其余归属 {candidate.conflict_dropped_strategies} 未采用。"
            )
        all_recommendations.append(base)

        if funded_ledger_missing:
            blocked_or_waiting.append({
                **base,
                "status": "blocked_funded_ledger_missing",
                "reason": "缺少可信 funded ledger 可用现金，不能用配置本金替代。",
            })
            continue

        if cross_symbol_basis_mismatch:
            blocked_or_waiting.append({
                **base,
                "status": "blocked_cross_symbol_price_basis",
                "reason": "候选集合存在跨股票价格口径冲突，整批纸面资金规划已阻断。",
            })
            continue

        # Existing holdings are maintained, not re-planned.
        if candidate.symbol in existing_symbols:
            blocked_or_waiting.append({
                **base,
                "status": "already_held_maintained",
                "reason": "已在纸面组合中持有，本阶段维持不动，不纳入新资金规划。",
            })
            continue

        assessment = _assess_candidate_data(candidate)
        degrade_warnings.extend(assessment["degrade_warnings"])
        if not assessment["usable"]:
            blocked_or_waiting.append({
                **base,
                "status": "blocked_data",
                "reason": assessment["block_reason"],
            })
            continue

        # Board must have verified minute coverage to enter an intraday paper
        # plan; otherwise it becomes a data request, never a fabricated seat.
        if candidate.strategy == "board" and not _covered_by_minute_pool(
            candidate.symbol,
            minute_pool,
            evidence_eligible=minute_pool_evidence_eligible,
        ):
            data_request_suggested.append({
                **base,
                "status": "data_request_suggested",
                "requested_data": "verified_intraday_1m_coverage",
                "reason": "打板策略需要已验证分钟线覆盖才能进入盘中纸面计划；当前缺失，仅生成分钟线补齐请求。",
            })
            continue

        lot_cost = _lot_cost(float(candidate.price), candidate_lot_size)
        if lot_cost > initial_cash:
            lot_infeasible_count += 1
            blocked_or_waiting.append({
                **base,
                "status": "blocked_lot_infeasible",
                "lot_size": candidate_lot_size,
                "one_lot_cost": round(lot_cost, 2),
                "reason": f"单手（{candidate_lot_size} 股）成本 {round(lot_cost, 2)} 超过共享纸面资金总额 {round(initial_cash, 2)}，整手不可行。",
            })
            continue

        eligible.append({
            "candidate": candidate,
            "base": base,
            "lot_cost": lot_cost,
            "lot_size": candidate_lot_size,
        })

    selected_symbols: set[str] = set()

    def _select(entry: dict[str, Any], attribution_reason: str) -> None:
        nonlocal remaining_cash
        candidate: _PlannerCandidate = entry["candidate"]
        lot_cost: float = entry["lot_cost"]
        entry_lot_size: int = entry["lot_size"]
        remaining_cash -= lot_cost
        selected_symbols.add(candidate.symbol)
        selected.append({
            **entry["base"],
            "status": "selected",
            "planned_shares": entry_lot_size,
            "lot_size": entry_lot_size,
            "planned_price": round(float(candidate.price), 4),
            "occupied_cash": round(lot_cost, 2),
            "cash_after": round(remaining_cash, 2),
            "attribution_reason": attribution_reason,
            "is_real_order": False,
            "research_only": True,
            "paper_only": True,
        })

    # Phase 1: reserve at least one 100-share seat for the strongest feasible
    # long research recommendation.
    long_eligible = sorted(
        (e for e in eligible if e["candidate"].strategy == "long"),
        key=lambda e: _research_rank_key(e["base"]),
        reverse=True,
    )
    for entry in long_eligible:
        if entry["lot_cost"] <= remaining_cash:
            _select(entry, "长期研究推荐优先保留至少一个纸面席位。")
            break

    # Phase 2: plan remaining cash by board -> swing -> additional long, ordered
    # by research evidence, one integer lot each until cash runs out.
    for strat in _REMAINING_FILL_ORDER:
        strat_entries = sorted(
            (
                e
                for e in eligible
                if e["candidate"].strategy == strat and e["candidate"].symbol not in selected_symbols
            ),
            key=lambda e: _research_rank_key(e["base"]),
            reverse=True,
        )
        for entry in strat_entries:
            if entry["lot_cost"] <= remaining_cash:
                _select(entry, f"按 board→swing→additional long 优先级与研究证据规划：{PLANNER_STRATEGY_LABELS.get(strat, strat)}。")

    # Eligible-but-unfunded symbols become waiting entries (still not fabricated).
    for entry in eligible:
        candidate = entry["candidate"]
        if candidate.symbol in selected_symbols:
            continue
        blocked_or_waiting.append({
            **entry["base"],
            "status": "waiting_insufficient_cash",
            "lot_size": entry["lot_size"],
            "one_lot_cost": round(entry["lot_cost"], 2),
            "reason": f"剩余共享纸面资金 {round(remaining_cash, 2)} 不足以再买入一手（约 {round(entry['lot_cost'], 2)}），进入等待。",
        })

    occupied_cash = round(initial_cash - remaining_cash, 2)
    degrade_warnings = list(dict.fromkeys(degrade_warnings))

    plan_warnings: list[str] = list(degrade_warnings)
    plan_status = "planned"
    if not truthfulness["is_canonical_data_root"]:
        plan_status = "degraded_non_canonical_data"
        plan_warnings.insert(
            0,
            f"数据根为 {truthfulness['data_root_kind']}（非 canonical data/），本次计划仅作诊断，不写入正式 latest 证据。",
        )
    elif truthfulness["smoke_run"]:
        plan_status = "degraded_smoke_run"
        plan_warnings.insert(0, "smoke 冒烟运行结果不作为正式 latest 证据。")
    if not planner_candidates:
        plan_status = "no_research_recommendations" if plan_status == "planned" else plan_status
        plan_warnings.append("当前没有研究推荐候选，未生成纸面资金席位。")
    if funded_ledger_missing:
        plan_status = "blocked_funded_ledger_missing"
        plan_warnings.insert(0, "funded_ledger_missing: 缺少可信纸面账本现金，未使用配置本金回退。")
    elif cross_symbol_basis_mismatch:
        plan_status = "blocked_cross_symbol_price_basis"
        plan_warnings.insert(0, "cross_symbol_price_basis_mismatch: 候选集合价格口径不一致，整批阻断。")
    elif non_raw_price_basis:
        plan_status = "blocked_price_basis_not_raw"
        plan_warnings.insert(0, "consumer_price_basis_not_allowed: 纸面资金规划只接受 raw 估值价格。")
    elif strategy_evidence_reasons:
        plan_status = "blocked_strategy_evidence"
        plan_warnings.insert(0, "strategy_evidence_envelope_invalid: 策略候选证据封套不完整或不可验证。")
    elif minute_pool_reasons:
        plan_status = "blocked_minute_pool_evidence"
        plan_warnings.insert(0, "minute_pool_evidence_invalid: 打板分钟池证据不可验证。")

    as_of = now_text()
    data_date = _clean_str(strategy_payload.get("data_cutoff")) or dataset_stats["date_end"] or (
        _clean_str((strategy_payload.get("daily_feature_status") or {}).get("latest_date"))
        if isinstance(strategy_payload.get("daily_feature_status"), dict)
        else ""
    )
    contract = price_data_contract()
    price_reason_codes: list[str] = []
    if cross_symbol_basis_mismatch:
        price_reason_codes.append("cross_symbol_price_basis_mismatch")
    elif non_raw_price_basis:
        price_reason_codes.append("consumer_price_basis_not_allowed")
    elif not normalized_price_bases:
        price_reason_codes.append("unknown_price_basis")
    price_policy_ok = not price_reason_codes
    degraded_codes: list[str] = list(price_reason_codes)
    if funded_ledger_missing:
        degraded_codes.append("funded_ledger_missing")
    if not truthfulness["is_canonical_data_root"]:
        degraded_codes.append("non_canonical_data_root")
    if truthfulness["smoke_run"]:
        degraded_codes.append("smoke_run")
    if not planner_candidates:
        degraded_codes.append("no_research_recommendations")
    degraded_codes.extend(strategy_evidence_reasons)
    degraded_codes.extend(minute_pool_reasons)
    degraded_codes = list(dict.fromkeys(degraded_codes))
    evidence_eligible = bool(
        plan_status == "planned"
        and price_policy_ok
        and not funded_ledger_missing
        and not strategy_evidence_reasons
        and not minute_pool_reasons
        and truthfulness["latest_eligible"]
    )
    latest_eligible = bool(truthfulness["latest_eligible"] and evidence_eligible)
    latest_write_blocked_reason = (
        truthfulness["latest_write_blocked_reason"]
        if not truthfulness["latest_eligible"]
        else ("plan_evidence_not_eligible" if not evidence_eligible else "")
    )
    minute_symbols = minute_pool.get("symbols") if isinstance(minute_pool, dict) else []
    if isinstance(minute_symbols, (set, list, tuple)):
        canonical_minute_symbols = sorted(str(item) for item in minute_symbols)
    else:
        canonical_minute_symbols = []
    config_evidence = {
            "initial_cash": config.initial_cash,
            "lot_size": config.lot_size,
            "strategy": config.strategy,
            "data_root": str(config.data_root or ""),
            "write_latest": config.write_latest,
            "smoke_run": config.smoke_run,
            "synthetic": config.synthetic,
    }
    source_evidence = {
        "strategy_source_hash": strategy_payload.get("source_hash"),
        "strategy_payload_hash": _strategy_payload_hash(strategy_payload),
        "funded_ledger_source_hash": _canonical_hash(paper_state or {}),
        "minute_pool_source_hash": (
            minute_pool.get("source_hash") if isinstance(minute_pool, dict) else None
        ),
        "minute_pool_symbols": canonical_minute_symbols,
        "config_source_hash": _canonical_hash(config_evidence),
    }
    source_hash = _canonical_hash(source_evidence)

    provenance = {
        "data_root_kind": truthfulness["data_root_kind"],
        "is_canonical_data_root": truthfulness["is_canonical_data_root"],
        "resolved_data_root": truthfulness["resolved_data_root"],
        "latest_eligible": latest_eligible,
        "latest_write_blocked_reason": latest_write_blocked_reason,
        "dataset_symbol_count": truthfulness["dataset_symbol_count"],
        "dataset_date_start": truthfulness["dataset_date_start"],
        "dataset_date_end": truthfulness["dataset_date_end"],
        "smoke_run": truthfulness["smoke_run"],
        "auto_strategy_generated_at": strategy_payload.get("generated_at"),
    }

    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "shared_paper_allocation_plan",
        "generated_at": as_of,
        "as_of": as_of,
        "data_cutoff": data_date or None,
        "source_path": "memory://shared-paper-allocation",
        "source_hash": source_hash,
        "source_evidence": source_evidence,
        "price_data_contract_version": str(contract.get("version") or ""),
        "price_policy_validation": {
            "status": "passed" if price_policy_ok else "failed",
            "eligible": price_policy_ok,
            "contract_version": str(contract.get("version") or ""),
            "consumer": "paper_valuation",
            "consumer_policy": (contract.get("current_enforced") or {}).get("paper_valuation") or {},
            "observed_basis": next(iter(normalized_price_bases), "mixed_or_unknown")
            if len(normalized_price_bases) <= 1
            else "mixed_or_unknown",
            "accepted_price_basis": "raw" if price_policy_ok else None,
            "reason_codes": price_reason_codes,
        },
        "pit_status": {"status": "not_applicable_current_paper_planning"},
        "coverage": {
            "kind": "current_research_candidates",
            "candidate_count": len(planner_candidates),
            "selected_count": len(selected),
        },
        "evidence_eligible": evidence_eligible,
        "strategy_evidence_eligible": not strategy_evidence_reasons,
        "minute_pool_evidence_eligible": minute_pool_evidence_eligible,
        "funded_ledger_evidence_eligible": not funded_ledger_missing,
        "degraded_codes": tuple(degraded_codes),
        "data_date": data_date or None,
        "price_basis_values": dataset_stats["price_basis_values"],
        "data_quality_values": dataset_stats["data_quality_values"],
        "plan_status": plan_status,
        "strategy_scope": config.strategy,
        "initial_cash": round(initial_cash, 2),
        "configured_initial_cash": round(configured_initial_cash, 2),
        "available_cash_source": cash_source,
        "lot_size": lot_size,
        "remaining_cash": round(remaining_cash, 2),
        "occupied_cash": occupied_cash,
        "lot_infeasible_count": lot_infeasible_count,
        "selected_count": len(selected),
        "blocked_or_waiting_count": len(blocked_or_waiting),
        "data_request_suggested_count": len(data_request_suggested),
        "research_recommendation_count": len(all_recommendations),
        "all_research_recommendations": all_recommendations,
        "selected_for_paper_plan": selected,
        "blocked_or_waiting": blocked_or_waiting,
        "data_request_suggested": data_request_suggested,
        "maintained_existing_positions": maintained,
        "data_quality_warnings": plan_warnings,
        "report_provenance": provenance,
        "latest_eligible": latest_eligible,
        "latest_write_blocked_reason": latest_write_blocked_reason,
        "data_root_kind": truthfulness["data_root_kind"],
        # Boundary fields.
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "not_investment_advice": True,
        "live_execution_locked": True,
        "is_real_order": False,
        "plan_executed": False,
        "disclaimer": DISCLAIMER,
        "broker_boundary": BROKER_BOUNDARY,
    }
    payload["plan_digest"] = _plan_digest(payload)
    return payload


def _run_tag(payload: dict[str, Any]) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind = str(payload.get("data_root_kind") or "unknown")
    return f"{stamp}_{kind}"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 共享纸面资金规划报告",
        "",
        DISCLAIMER,
        BROKER_BOUNDARY,
        "",
        "> Research Only / Paper Only / No Broker Route / Live Execution Locked / Not Investment Advice",
        "",
        "## 计划摘要",
        "",
        f"- as_of：{payload.get('as_of', '-')}",
        f"- 数据日期：{payload.get('data_date') or '-'}",
        f"- 价格口径：{payload.get('price_basis_values') or '-'}",
        f"- 计划状态：{payload.get('plan_status', '-')}",
        f"- 共享纸面资金：{payload.get('initial_cash', 0)}",
        f"- 已占用资金：{payload.get('occupied_cash', 0)}",
        f"- 剩余现金：{payload.get('remaining_cash', 0)}",
        f"- 每手股数：{payload.get('lot_size', 0)}",
        f"- 整手不可行数量：{payload.get('lot_infeasible_count', 0)}",
        f"- latest 是否可写：{payload.get('latest_eligible')}"
        + (f"（{payload.get('latest_write_blocked_reason')}）" if not payload.get("latest_eligible") else ""),
        "",
    ]
    warnings = payload.get("data_quality_warnings") or []
    if warnings:
        lines += ["## 数据质量与降级提示", ""]
        lines += [f"- {item}" for item in warnings]
        lines += [""]

    lines += [
        "## 进入纸面资金计划 (selected_for_paper_plan)",
        "",
        "| 代码 | 名称 | 策略 | 模拟股数 | 价格 | 占用现金 | 归属原因 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in payload.get("selected_for_paper_plan") or []:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('name', '')} | {row.get('strategy_label', '')} | "
            f"{row.get('planned_shares', 0)} | {row.get('planned_price', '')} | {row.get('occupied_cash', '')} | "
            f"{row.get('attribution_reason', '')} |"
        )

    lines += [
        "",
        "## 未进入 / 等待 (blocked_or_waiting)",
        "",
        "| 代码 | 名称 | 策略 | 状态 | 原因 |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("blocked_or_waiting") or []:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('name', '')} | {row.get('strategy_label', '')} | "
            f"{row.get('status', '')} | {row.get('reason', '')} |"
        )

    lines += [
        "",
        "## 分钟线补齐请求 (data_request_suggested)",
        "",
        "| 代码 | 名称 | 策略 | 请求数据 | 原因 |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("data_request_suggested") or []:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('name', '')} | {row.get('strategy_label', '')} | "
            f"{row.get('requested_data', '')} | {row.get('reason', '')} |"
        )

    lines += [
        "",
        "## 已有纸面持仓（维持不动）",
        "",
        "| 代码 | 名称 | 策略 | 数量 | 说明 |",
        "|---|---|---|---:|---|",
    ]
    for row in payload.get("maintained_existing_positions") or []:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('name', '')} | {row.get('strategy_type', '')} | "
            f"{row.get('quantity', 0)} | {row.get('maintain_reason', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _latest_plan_validation_reasons(payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    contract_version = str(price_data_contract().get("version") or "")
    provenance = payload.get("report_provenance")
    policy = payload.get("price_policy_validation")
    source_evidence = payload.get("source_evidence")
    if payload.get("task_type") != "shared_paper_allocation_plan":
        reasons.append("plan_task_type_invalid")
    if payload.get("schema_version") != API_SCHEMA_VERSION:
        reasons.append("plan_schema_version_invalid")
    if payload.get("plan_digest") != _plan_digest(payload):
        reasons.append("plan_digest_mismatch")
    if not isinstance(source_evidence, dict) or payload.get("source_hash") != _canonical_hash(
        source_evidence if isinstance(source_evidence, dict) else {}
    ):
        reasons.append("plan_source_evidence_invalid")
    elif any(
        not isinstance(source_evidence.get(key), str)
        or not str(source_evidence.get(key)).startswith("sha256:")
        or len(str(source_evidence.get(key))) != 71
        for key in (
            "strategy_source_hash",
            "strategy_payload_hash",
            "funded_ledger_source_hash",
            "config_source_hash",
        )
    ):
        reasons.append("plan_source_hash_invalid")
    if not isinstance(provenance, dict) or not (
        provenance.get("is_canonical_data_root") is True
        and provenance.get("data_root_kind") == "canonical"
        and provenance.get("latest_eligible") is True
    ):
        reasons.append("plan_provenance_not_canonical")
    if payload.get("data_root_kind") != "canonical":
        reasons.append("plan_data_root_not_canonical")
    if not isinstance(policy, dict) or not (
        policy.get("status") == "passed"
        and policy.get("eligible") is True
        and policy.get("contract_version") == contract_version
        and policy.get("consumer") == "paper_valuation"
        and policy.get("observed_basis") == "raw"
        and policy.get("accepted_price_basis") == "raw"
    ):
        reasons.append("plan_price_policy_invalid")
    if payload.get("price_data_contract_version") != contract_version:
        reasons.append("plan_price_contract_version_invalid")
    as_of_day = _iso_day(payload.get("as_of"))
    cutoff_day = _iso_day(payload.get("data_cutoff"))
    if as_of_day is None or cutoff_day is None or cutoff_day > as_of_day:
        reasons.append("plan_data_cutoff_invalid")
    if not (
        payload.get("latest_eligible") is True
        and payload.get("evidence_eligible") is True
        and payload.get("strategy_evidence_eligible") is True
        and payload.get("minute_pool_evidence_eligible") is True
        and payload.get("funded_ledger_evidence_eligible") is True
        and payload.get("plan_status") == "planned"
    ):
        reasons.append("plan_evidence_not_eligible")
    if not (
        payload.get("research_only") is True
        and payload.get("paper_only") is True
        and payload.get("no_broker") is True
        and payload.get("live_execution_locked") is True
        and payload.get("is_real_order") is False
        and payload.get("plan_executed") is False
    ):
        reasons.append("plan_boundary_invalid")
    initial_cash = safe_float(payload.get("initial_cash"), None)
    occupied_cash = safe_float(payload.get("occupied_cash"), None)
    remaining_cash = safe_float(payload.get("remaining_cash"), None)
    selected = payload.get("selected_for_paper_plan")
    if (
        initial_cash is None
        or occupied_cash is None
        or remaining_cash is None
        or min(initial_cash, occupied_cash, remaining_cash) < 0
        or abs((occupied_cash + remaining_cash) - initial_cash) > 0.02
        or not isinstance(selected, list)
        or safe_int(payload.get("selected_count"), -1) != len(selected or [])
        or abs(
            sum(
                safe_float(row.get("occupied_cash"), 0.0) or 0.0
                for row in (selected or [])
                if isinstance(row, dict)
            )
            - occupied_cash
        )
        > 0.02
    ):
        reasons.append("plan_cash_reconciliation_invalid")
    return list(dict.fromkeys(reasons))


def write_shared_paper_plan_outputs(
    payload: dict[str, Any],
    *,
    results_dir: Path | None = None,
    reports_dir: Path | None = None,
) -> dict[str, Any]:
    """Write plan JSON/Markdown. Non-eligible plans never overwrite latest."""

    results_dir = Path(results_dir) if results_dir is not None else RESULTS_DIR
    reports_dir = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    latest_validation_reasons = _latest_plan_validation_reasons(payload)
    latest_eligible = not latest_validation_reasons

    if latest_eligible:
        json_path = results_dir / "shared_paper_plan_latest.json"
        md_path = reports_dir / "shared_paper_plan_latest.md"
        write_target = "canonical_latest"
    else:
        tag = _run_tag(payload)
        runs_dir = results_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        json_path = runs_dir / f"shared_paper_plan_{tag}.json"
        md_path = runs_dir / f"shared_paper_plan_{tag}.md"
        write_target = "run_specific"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(json_path, payload)
    md_path.write_text(_render_markdown(payload), encoding="utf-8")

    return {
        "latest_write_target": write_target,
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "latest_eligible": latest_eligible,
        "latest_validation_reason_codes": latest_validation_reasons,
    }


def run_shared_paper_allocation_plan(
    *,
    initial_cash: float = DEFAULT_INITIAL_CASH,
    lot_size: int = DEFAULT_LOT_SIZE,
    strategy: str = "all",
    data_root: str | Path | None = None,
    write_latest: bool = True,
    smoke_run: bool = False,
    synthetic: bool = False,
    auto_strategy_path: Path | None = None,
    paper_state_path: Path | None = None,
    minute_pool: dict[str, Any] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Load inputs from disk, build the shared plan, and optionally write it."""

    strategy_payload = read_json_safe(auto_strategy_path or AUTO_STRATEGY_LATEST_JSON, {})
    if not isinstance(strategy_payload, dict):
        strategy_payload = {}
    resolved_paper_state_path = Path(paper_state_path or AUTO_PAPER_PORTFOLIO_STATE_JSON)
    if resolved_paper_state_path.exists():
        paper_state = PaperStateStore(
            resolved_paper_state_path,
            schema_version=API_SCHEMA_VERSION,
            max_backups=7,
        ).read(default={})
    else:
        paper_state = {}
    if not isinstance(paper_state, dict):
        paper_state = {"state_recovery_warning": "paper_state_root_invalid"}
    funded_state_trustworthy = not bool(paper_state.get("state_recovery_warning"))
    if minute_pool is None:
        try:
            from quantdesk.intraday.minute_pool_capability import (
                load_completed_minute_pool_capability,
            )

            minute_pool = load_completed_minute_pool_capability()
        except Exception:
            minute_pool = None

    config = SharedAllocationConfig(
        initial_cash=initial_cash,
        lot_size=lot_size,
        strategy=strategy,
        data_root=data_root,
        write_latest=write_latest,
        smoke_run=smoke_run,
        synthetic=synthetic,
    )
    payload = plan_shared_paper_allocation(
        config=config,
        auto_strategy_payload=strategy_payload,
        paper_state=paper_state,
        minute_pool=minute_pool,
    )
    payload["funded_state_trustworthy"] = funded_state_trustworthy
    if not funded_state_trustworthy:
        payload["plan_status"] = "blocked_paper_state_untrusted"
        payload["data_quality_warnings"] = list(
            dict.fromkeys(
                [
                    "paper_state_untrusted: corrupt funded ledger has no verified recovery",
                    *(payload.get("data_quality_warnings") or []),
                ]
            )
        )
    elif paper_state.get("state_recovered_from_backup"):
        payload["data_quality_warnings"] = list(
            dict.fromkeys(
                [
                    "paper_state_recovered_from_backup: funded cash and positions came from a verified backup",
                    *(payload.get("data_quality_warnings") or []),
                ]
            )
        )
    if write:
        write_result = write_shared_paper_plan_outputs(payload)
        payload["output_paths"] = write_result
    return payload
