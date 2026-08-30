"""Stage 4 tri-account historical replay driver (real local data).

This driver runs the three experiment accounts (A low-price / B affordable
proxy-leaders / C board) over a frozen window, forcing **every** buy through the
mandated new stack:

    PaperOrderIntent -> evaluate_paper_risk_gate -> simulate_paper_intent

It never touches the real paper ledger (``state/auto_paper_portfolio.json``);
each account state lives in an independent in-memory ledger and is written only
under ``results/experiments/exp_<date>/``.  It performs zero network I/O, makes
zero strategy-formula changes, and keeps ``risk_gate_enforced=false`` (shadow).

Honest-outcome note (Round 5, 2026-07-21): local sector-rotation evidence is
static / non-PIT, so the risk gate returns ``insufficient_evidence`` for every
buy and the kernel fills nothing.  That is a faithful data-coverage outcome, not
a driver bug; the driver records the full intent->gate chain per candidate so the
zero-fill conclusion is fully evidenced rather than asserted.  See
``test_runtime/round5_stage4_preflight_note_20260721.md`` and the Stage-4 report.

Boundary: Research Only / Paper Account Only / No Broker Route / not advice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.data_truth.candidate_lifecycle_evidence import derive_candidate_evidence
from quantdesk.paper.lifecycle import CandidateDecisionEvent
from quantdesk.paper.portfolio_contracts import (
    build_paper_exit_intent,
    build_paper_order_intent,
    build_portfolio_snapshot,
    resolve_paper_capital_profile,
)
from quantdesk.paper.simulation_kernel import (
    FillEvent,
    SettlementPolicy,
    _project_fill,
    simulate_paper_intent,
)
from quantdesk.paper.stage4_replay_evidence import build_execution_evidence
from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set
from quantdesk.risk.risk_gate import evaluate_paper_risk_gate
from quantdesk.risk.risk_gate_shadow import collect_local_risk_evidence
from quantdesk.risk.pre_trade_gate import PreTradeGateConfig
from quantdesk.shared import normalize_symbol
from quantdesk.strategy_signals import (
    add_signal_columns,
    board_signal_from_row,
    evaluate_exit_signals,
    long_signal_from_row,
)
from quantdesk.trading_calendar import next_trade_day, trade_days_between
from quantdesk.trader_profile import (
    ResearchPaperProfile,
    validate_research_paper_profile,
)
from quantdesk.version import API_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"
CALENDAR_PATH = PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
THEME_MAP_PATH = PROJECT_ROOT / "data" / "universe" / "stock_theme_map.csv"
LOW_PRICE_UNIVERSE = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
WINDOW_AUDIT_LATEST_JSON = PROJECT_ROOT / "results" / "data_health" / "minute_pool_window_audit_latest.json"

DEFAULT_PAPER_CAPITAL = 10_000.0
STRATEGY_LONG = "long"
STRATEGY_BOARD = "board"


def _day10(value: Any) -> str:
    return str(value or "")[:10]


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# account ledger (independent, never the real paper book)                      #
# --------------------------------------------------------------------------- #
def _fresh_account_ledger(account_id: str, *, initial_capital: float = DEFAULT_PAPER_CAPITAL) -> dict[str, Any]:
    """Build a fresh funded 10k ledger with a distinct identity per account."""

    return {
        "ledger_id": f"stage4_replay_{account_id}",
        "schema_version": API_SCHEMA_VERSION,
        "account": {
            "profile_id": "user_10k",
            "scenario_id": "user_10k",
            "initial_cash": float(initial_capital),
            "cash": float(initial_capital),
            "available_cash": float(initial_capital),
        },
        "cash": float(initial_capital),
        "initial_cash": float(initial_capital),
        "positions": [],
        "closed_positions": [],
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def _theme_industry_map(theme_map_path: Path) -> dict[str, str]:
    if not theme_map_path.exists():
        return {}
    frame = read_csv_frame(theme_map_path, dtype=str)
    out: dict[str, str] = {}
    if "symbol" not in frame.columns:
        return out
    industry_col = "industry" if "industry" in frame.columns else ("sector" if "sector" in frame.columns else None)
    for _, row in frame.iterrows():
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        industry = str(row.get(industry_col) or "").strip() if industry_col else ""
        if industry:
            out[symbol] = industry
    return out


# --------------------------------------------------------------------------- #
# signal generation (existing formulas, zero changes)                          #
# --------------------------------------------------------------------------- #
def _read_daily_enriched(symbol: str, daily_dir: Path) -> pd.DataFrame:
    path = daily_dir / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = read_csv_frame(path)
    observed_price_basis, price_basis_reason = classify_frame_price_basis(frame)
    rename = {
        "日期": "date", "交易日期": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
    }
    frame = frame.rename(columns={col: rename.get(str(col), str(col)) for col in frame.columns}).copy()
    frame = frame.loc[:, ~frame.columns.duplicated()]
    if "date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    enriched = add_signal_columns(frame)
    # Preserve the contract verdict from the untouched source frame. The
    # downstream validator reads these attrs before inspecting transformed
    # rows, so sorting/deduplication can never launder a mixed original file.
    enriched.attrs["observed_price_basis"] = observed_price_basis
    enriched.attrs["price_basis_reason"] = price_basis_reason
    return enriched


def _signals_for_symbol(
    symbol: str,
    name: str,
    strategy: str,
    signal_days: set[str],
    *,
    daily_dir: Path,
) -> list[dict[str, Any]]:
    """Return entry signals for one symbol whose signal_date is in ``signal_days``.

    Uses the existing ``long_signal_from_row`` / ``board_signal_from_row``
    formulas unchanged, row-by-row so each row only sees its own trailing
    window (the enriched frame's rolling columns are backward-looking).
    """

    frame = _read_daily_enriched(symbol, daily_dir)
    if frame.empty:
        return []
    out: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        row_day = _day10(row.get("date"))
        if row_day not in signal_days:
            continue
        row_dict = row.to_dict()
        row_dict["date"] = row_day
        if strategy == STRATEGY_LONG:
            sig = long_signal_from_row(row_dict, symbol=symbol, name=name)
        else:
            sig = board_signal_from_row(row_dict, symbol=symbol, name=name)
        if sig:
            sig["amount_ma20"] = float(row.get("amount_ma20")) if pd.notna(row.get("amount_ma20")) else None
            # These three fields feed derive_candidate_evidence criterion 3 (as_of_clean).
            # data_latest_date: row_day is the date of the enriched-frame row that produced
            #   this signal; it is the latest data point the signal was computed from.
            sig["data_latest_date"] = row_day
            # is_target_date_covered: the row was found in the enriched frame at exactly
            #   row_day (passed the signal_days filter), so target-date coverage is genuine.
            sig["is_target_date_covered"] = True
            # daily_feature_latest_date: rolling features (ma20, amount_ma20, …) come from
            #   the same enriched frame row, so their currency matches row_day.
            sig["daily_feature_latest_date"] = row_day
            out.append(sig)
    return out


# --------------------------------------------------------------------------- #
# per-candidate chain: snapshot -> intent -> gate -> (evidence -> kernel)       #
# --------------------------------------------------------------------------- #
@dataclass
class CandidateOutcome:
    account_id: str
    symbol: str
    signal_date: str
    execution_date: str
    stage: str  # intent | gate | evidence | fill | no_fill
    status: str
    reason_codes: tuple[str, ...] = ()
    intent_digest: str = ""
    decision_digest: str = ""
    fill_price: float | None = None
    quantity: int = 0
    evidence_ref: str = ""
    kernel_event: FillEvent | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "symbol": self.symbol,
            "signal_date": self.signal_date,
            "execution_date": self.execution_date,
            "stage": self.stage,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "intent_digest": self.intent_digest,
            "decision_digest": self.decision_digest,
            "fill_price": self.fill_price,
            "quantity": self.quantity,
            "evidence_ref": self.evidence_ref,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
        }


def _evaluate_candidate_buy(
    *,
    account_id: str,
    ledger: Mapping[str, Any],
    candidate: Mapping[str, Any],
    signal_date: str,
    industry: str,
    window_pool: Mapping[str, Any],
    st_delist: Any,
    daily_dir: Path,
    intraday_dir: Path,
    calendar_path: Path,
    ledger_source_path: str,
    sector_rotation_path: Path | None = None,
    candidate_source_path: str | Path | None = None,
    minute_snapshot_path: Path = WINDOW_AUDIT_LATEST_JSON,
    pre_trade_config: PreTradeGateConfig | None = None,
    source_cache: dict[str, dict[str, Any]] | None = None,
    lifecycle_source_cache: dict[str, Any] | None = None,
) -> CandidateOutcome:
    """Run one buy candidate through the full mandated stack, fail-closed."""

    symbol = normalize_symbol(candidate.get("symbol"))
    reference_price = candidate.get("close")
    try:
        execution_date = next_trade_day(signal_date, calendar_path=calendar_path)
    except Exception:
        return CandidateOutcome(account_id, symbol, signal_date, "", "intent", "no_execution_date", ("trading_calendar_lookup_failed",))

    profile = resolve_paper_capital_profile(existing_state=ledger, existing_state_verified=True)
    snapshot = build_portfolio_snapshot(ledger, profile, as_of=signal_date, valuation_price_basis="raw")
    if not snapshot.allocation_eligible or snapshot.evidence_eligible is not True:
        return CandidateOutcome(account_id, symbol, signal_date, execution_date, "snapshot", "snapshot_not_eligible", tuple(snapshot.reason_codes))

    # Candidate evidence is derived from canonical disk evidence, never
    # asserted. This previously hardcoded eligible/formal/coverage=1.0, which
    # meant this replay passed its own intent_not_fundable gate on evidence
    # that was declared rather than proven. The derivation proves each of five
    # criteria against canonical raw daily rows and that date's own dated
    # sector membership; anything unproven degrades to partial_formal and the
    # candidate is honestly recorded as not fundable below.
    derived_evidence = derive_candidate_evidence(
        candidate,
        as_of=signal_date,
        daily_dir=daily_dir,
        source_cache=lifecycle_source_cache,
    )
    evidence_derivation = derived_evidence.pop("derivation", {})
    candidate_evidence = derived_evidence
    event = CandidateDecisionEvent(
        event_id=f"stage4-{account_id}-{symbol}-{signal_date}",
        symbol=symbol,
        occurred_at=f"{signal_date}T15:00:00+08:00",
        requested_state="paper_watchlist",
        decision_origin="system",
        evidence=candidate_evidence,
        payload={
            "name": str(candidate.get("name") or ""),
            "strategy_type": str(candidate.get("strategy") or ""),
            "sector": industry,
            "industry": industry,
        },
    ).to_dict()

    effective_pretrade_config = pre_trade_config or PreTradeGateConfig()
    if not isinstance(effective_pretrade_config, PreTradeGateConfig):
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            execution_date,
            "intent",
            "intent_not_fundable",
            ("pre_trade_config_invalid",),
        )
    max_notional = max(
        0.0,
        float(snapshot.net_equity)
        * float(effective_pretrade_config.max_position_pct),
    )
    intent = build_paper_order_intent(
        event,
        snapshot,
        reference_price=reference_price,
        reference_price_basis="raw",
        max_notional=max_notional,
    )
    if intent.status != "pending_risk_review" or intent.reason_codes:
        # Carry the derivation's unproven criteria alongside the intent's own
        # codes, so a block names which evidence could not be proven instead of
        # only reporting the downstream lifecycle symptom.
        derivation_reasons = tuple(evidence_derivation.get("reason_codes") or ())
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            execution_date,
            "intent",
            "intent_not_fundable",
            tuple(dict.fromkeys((*intent.reason_codes, *derivation_reasons))),
            intent.intent_digest,
        )

    gate_candidate = {
        "symbol": symbol,
        "name": str(candidate.get("name") or ""),
        "sector": industry,
        "industry": industry,
        "amount_ma20": candidate.get("amount_ma20"),
        "data_latest_date": signal_date,
    }
    local_candidate_source = candidate_source_path or daily_dir / f"{symbol}.csv"
    evidence, missing = collect_local_risk_evidence(
        funded_ledger=ledger,
        candidate=gate_candidate,
        side="buy",
        reference_price=reference_price,
        as_of=signal_date,
        daily_dir=daily_dir,
        candidate_source_path=local_candidate_source,
        ledger_source_path=ledger_source_path,
        sector_rotation_path=sector_rotation_path,
        price_source_path=local_candidate_source,
        price_data_cutoff=signal_date,
        source_cache=source_cache,
    )
    decision = evaluate_paper_risk_gate(
        intent,
        snapshot,
        profile=profile,
        funded_ledger=ledger,
        evidence=evidence,
        as_of=signal_date,
        pre_trade_config=effective_pretrade_config,
        llm_review=None,
    )
    if not decision.passed:
        assembly_reasons = [r for reasons in missing.values() for r in reasons]
        codes = tuple(dict.fromkeys([*decision.reason_codes, *assembly_reasons]))
        return CandidateOutcome(
            account_id, symbol, signal_date, execution_date, "gate", decision.status,
            codes, intent.intent_digest, decision.decision_digest,
        )

    # Gate passed -> assemble real execution evidence and let the kernel judge.
    er = build_execution_evidence(
        symbol,
        signal_date,
        window_pool=window_pool,
        st_delist=st_delist,
        daily_dir=daily_dir,
        intraday_dir=intraday_dir,
        calendar_path=calendar_path,
        snapshot_path=minute_snapshot_path,
    )
    if not er.ok or er.evidence is None:
        return CandidateOutcome(
            account_id, symbol, signal_date, execution_date, "evidence", "no_fill_missing_evidence",
            er.reason_codes, intent.intent_digest, decision.decision_digest,
        )
    event_out = simulate_paper_intent(
        intent, decision, snapshot, ledger, er.evidence, SettlementPolicy(),
    )
    if isinstance(event_out, FillEvent):
        return CandidateOutcome(
            account_id, symbol, signal_date, execution_date, "fill", "filled",
            (), intent.intent_digest, decision.decision_digest,
            fill_price=event_out.fill_price, quantity=event_out.quantity,
            evidence_ref=event_out.evidence_digest,
            kernel_event=event_out,
        )
    return CandidateOutcome(
        account_id, symbol, signal_date, execution_date, "no_fill", "rejected_no_fill",
        tuple(getattr(event_out, "reason_codes", ()) or ()), intent.intent_digest, decision.decision_digest,
    )


# --------------------------------------------------------------------------- #
# account replay                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class AccountReplayResult:
    account_id: str
    universe_label: str
    strategy: str
    window_start: str
    window_end: str
    minute_window_start: str
    minute_window_end: str
    signal_day_count: int
    symbol_count: int
    candidate_count: int
    outcomes: list[CandidateOutcome] = field(default_factory=list)
    stage_counts: dict[str, int] = field(default_factory=dict)
    fill_count: int = 0
    warnings: list[str] = field(default_factory=list)
    exit_stage_counts: dict[str, int] = field(default_factory=dict)
    exit_outcomes: list[CandidateOutcome] = field(default_factory=list)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    nav_series: list[dict[str, Any]] = field(default_factory=list)
    effective_sell_sample: int = 0
    fixture_or_synthetic: bool = False
    price_contract: dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "task_type": "stage4_account_replay",
            "account_id": self.account_id,
            "universe_label": self.universe_label,
            "strategy": self.strategy,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "minute_window_start": self.minute_window_start,
            "minute_window_end": self.minute_window_end,
            "signal_day_count": self.signal_day_count,
            "symbol_count": self.symbol_count,
            "candidate_count": self.candidate_count,
            "fill_count": self.fill_count,
            "stage_counts": dict(self.stage_counts),
            "outcomes": [o.to_dict() for o in self.outcomes],
            "warnings": list(self.warnings),
            "exit_stage_counts": dict(self.exit_stage_counts),
            "exit_outcomes": [outcome.to_dict() for outcome in self.exit_outcomes],
            "closed_trades": list(self.closed_trades),
            "nav_series": list(self.nav_series),
            "effective_sell_sample": self.effective_sell_sample,
            "fixture_or_synthetic": self.fixture_or_synthetic,
            "price_contract": dict(self.price_contract),
            "risk_gate_enforced": False,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "disclaimer": "Research Only; Paper Account Only; No Broker Route; Not Investment Advice.",
        }


def replay_account(
    *,
    account_id: str,
    strategy: str,
    symbols: Sequence[str],
    names: Mapping[str, str],
    universe_label: str,
    minute_window_start: str,
    minute_window_end: str,
    window_pool: Mapping[str, Any],
    st_delist: Any,
    industry_map: Mapping[str, str],
    daily_dir: Path = DAILY_DIR,
    intraday_dir: Path = INTRADAY_DIR,
    calendar_path: Path = CALENDAR_PATH,
    initial_capital: float = DEFAULT_PAPER_CAPITAL,
) -> AccountReplayResult:
    """Replay one account over the minute-covered window through the new stack.

    Signal days are the trading days strictly *before* each covered execution
    day (next-open contract), i.e. the day whose signal produces a fill on a
    covered day.  Only signals whose covered execution day is inside the minute
    window are evaluated.
    """

    ledger = _fresh_account_ledger(account_id, initial_capital=initial_capital)
    ledger_source_path = f"memory://stage4-replay/{account_id}"

    covered_exec_days = [
        d for d in trade_days_between(minute_window_start, minute_window_end, calendar_path=calendar_path)
    ]
    # signal day = trade day immediately before a covered execution day
    signal_days: set[str] = set()
    for exec_day in covered_exec_days:
        prev_days = trade_days_between(
            (pd.to_datetime(exec_day) - pd.Timedelta(days=12)).date().isoformat(),
            exec_day,
            calendar_path=calendar_path,
        )
        idx = prev_days.index(exec_day) if exec_day in prev_days else -1
        if idx > 0:
            signal_days.add(prev_days[idx - 1])

    result = AccountReplayResult(
        account_id=account_id,
        universe_label=universe_label,
        strategy=strategy,
        window_start=min(signal_days) if signal_days else "",
        window_end=max(signal_days) if signal_days else "",
        minute_window_start=minute_window_start,
        minute_window_end=minute_window_end,
        signal_day_count=len(signal_days),
        symbol_count=len(symbols),
        candidate_count=0,
    )
    if not signal_days:
        result.warnings.append("no_signal_days_resolved_for_minute_window")
        return result

    for symbol in symbols:
        norm = normalize_symbol(symbol)
        name = str(names.get(norm) or names.get(symbol) or norm)
        industry = str(industry_map.get(norm) or "unknown_industry")
        candidates = _signals_for_symbol(norm, name, strategy, signal_days, daily_dir=daily_dir)
        for cand in candidates:
            sig_day = _day10(cand.get("signal_date"))
            result.candidate_count += 1
            outcome = _evaluate_candidate_buy(
                account_id=account_id,
                ledger=ledger,
                candidate=cand,
                signal_date=sig_day,
                industry=industry,
                window_pool=window_pool,
                st_delist=st_delist,
                daily_dir=daily_dir,
                intraday_dir=intraday_dir,
                calendar_path=calendar_path,
                ledger_source_path=ledger_source_path,
            )
            result.outcomes.append(outcome)
            result.stage_counts[outcome.stage] = result.stage_counts.get(outcome.stage, 0) + 1
            if outcome.stage == "fill":
                result.fill_count += 1

    return result


def _sync_ledger_cash(ledger: dict[str, Any]) -> None:
    account = ledger.get("account")
    if not isinstance(account, dict):
        return
    cash = float(account.get("cash") or 0.0)
    account["available_cash"] = cash
    ledger["cash"] = cash


def _settle_for_day(ledger: dict[str, Any], day: str) -> None:
    for position in ledger.get("positions") or []:
        if not isinstance(position, dict):
            continue
        last_buy = _day10(position.get("last_buy_date") or position.get("entry_date"))
        if last_buy and last_buy < day:
            frozen = int(position.get("frozen_quantity") or 0)
            position["sellable_quantity"] = int(position.get("sellable_quantity") or 0) + frozen
            position["frozen_quantity"] = 0


def _mark_ledger(
    ledger: dict[str, Any],
    *,
    day: str,
    frames: Mapping[str, pd.DataFrame],
    industry_by_symbol: Mapping[str, str],
) -> None:
    for position in ledger.get("positions") or []:
        if not isinstance(position, dict):
            continue
        symbol = normalize_symbol(position.get("symbol"))
        frame = frames.get(symbol)
        if frame is None or frame.empty:
            continue
        rows = frame[frame["date"].dt.strftime("%Y-%m-%d") == day]
        if rows.empty:
            continue
        close = float(rows.iloc[-1]["close"])
        position["latest_price"] = close
        position["latest_date"] = day
        position["market_value"] = round(close * int(position.get("quantity") or 0), 4)
        if industry_by_symbol.get(symbol):
            position["sector"] = industry_by_symbol[symbol]


def _ledger_nav(ledger: Mapping[str, Any]) -> float:
    account = ledger.get("account")
    cash = float(account.get("cash") or 0.0) if isinstance(account, Mapping) else float(ledger.get("cash") or 0.0)
    market_value = sum(
        float(row.get("latest_price") or row.get("execution_price") or 0.0)
        * int(row.get("quantity") or 0)
        for row in ledger.get("positions") or []
        if isinstance(row, Mapping)
    )
    return round(cash + market_value, 8)


def _evaluate_position_sell(
    *,
    account_id: str,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    signal_date: str,
    reference_price: float,
    amount_ma20: float | None,
    sector_rotation_path: Path,
    window_pool: Mapping[str, Any],
    st_delist: Any,
    daily_dir: Path,
    intraday_dir: Path,
    calendar_path: Path,
    minute_snapshot_path: Path,
    ledger_source_path: str,
    source_cache: dict[str, dict[str, Any]] | None = None,
) -> CandidateOutcome:
    symbol = normalize_symbol(position.get("symbol"))
    try:
        execution_date = next_trade_day(signal_date, calendar_path=calendar_path)
    except Exception:
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            "",
            "exit_intent",
            "no_execution_date",
            ("trading_calendar_lookup_failed",),
        )
    profile = resolve_paper_capital_profile(existing_state=ledger, existing_state_verified=True)
    snapshot = build_portfolio_snapshot(
        ledger, profile, as_of=signal_date, valuation_price_basis="raw"
    )
    quantity = int(position.get("sellable_quantity") or 0)
    intent = build_paper_exit_intent(
        ledger,
        snapshot,
        symbol=symbol,
        reference_price=reference_price,
        reference_price_basis="raw",
        requested_quantity=quantity,
    )
    if intent.status != "pending_risk_review" or intent.reason_codes:
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            execution_date,
            "exit_intent",
            "exit_intent_not_fundable",
            tuple(intent.reason_codes),
            intent.intent_digest,
        )
    source_path = daily_dir / f"{symbol}.csv"
    candidate = {
        "symbol": symbol,
        "sector": position.get("sector"),
        "industry": position.get("sector"),
        "amount_ma20": amount_ma20,
        "data_latest_date": signal_date,
    }
    evidence, missing = collect_local_risk_evidence(
        funded_ledger=ledger,
        candidate=candidate,
        side="sell",
        reference_price=reference_price,
        as_of=signal_date,
        daily_dir=daily_dir,
        candidate_source_path=source_path,
        ledger_source_path=ledger_source_path,
        sector_rotation_path=sector_rotation_path,
        price_source_path=source_path,
        price_data_cutoff=signal_date,
        source_cache=source_cache,
    )
    decision = evaluate_paper_risk_gate(
        intent,
        snapshot,
        profile=profile,
        funded_ledger=ledger,
        evidence=evidence,
        as_of=signal_date,
        llm_review=None,
    )
    if not decision.passed:
        assembly_reasons = [reason for values in missing.values() for reason in values]
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            execution_date,
            "exit_gate",
            decision.status,
            tuple(dict.fromkeys([*decision.reason_codes, *assembly_reasons])),
            intent.intent_digest,
            decision.decision_digest,
        )
    evidence_result = build_execution_evidence(
        symbol,
        signal_date,
        window_pool=window_pool,
        st_delist=st_delist,
        daily_dir=daily_dir,
        intraday_dir=intraday_dir,
        calendar_path=calendar_path,
        snapshot_path=minute_snapshot_path,
    )
    if not evidence_result.ok or evidence_result.evidence is None:
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            execution_date,
            "exit_evidence",
            "no_fill_missing_evidence",
            evidence_result.reason_codes,
            intent.intent_digest,
            decision.decision_digest,
        )
    event = simulate_paper_intent(
        intent,
        decision,
        snapshot,
        ledger,
        evidence_result.evidence,
        SettlementPolicy(),
    )
    if isinstance(event, FillEvent):
        return CandidateOutcome(
            account_id,
            symbol,
            signal_date,
            execution_date,
            "exit_fill",
            "filled",
            (),
            intent.intent_digest,
            decision.decision_digest,
            fill_price=event.fill_price,
            quantity=event.quantity,
            evidence_ref=event.evidence_digest,
            kernel_event=event,
        )
    return CandidateOutcome(
        account_id,
        symbol,
        signal_date,
        execution_date,
        "exit_no_fill",
        "rejected_no_fill",
        tuple(getattr(event, "reason_codes", ()) or ()),
        intent.intent_digest,
        decision.decision_digest,
    )


def replay_forward_account(
    *,
    account_id: str,
    strategy: str,
    symbols: Sequence[str],
    names: Mapping[str, str],
    universe_label: str,
    window_dates: Sequence[str],
    window_pool: Mapping[str, Any],
    sector_snapshots: Mapping[str, Mapping[str, Any]],
    sector_snapshot_paths: Mapping[str, Path],
    sector_status_paths: Mapping[str, Path] | None = None,
    st_delist: Any,
    minute_snapshot_path: Path,
    daily_dir: Path = DAILY_DIR,
    intraday_dir: Path = INTRADAY_DIR,
    calendar_path: Path = CALENDAR_PATH,
    initial_capital: float = DEFAULT_PAPER_CAPITAL,
    max_holding_days: int = 20,
    stop_loss_pct: float = -8.0,
    take_profit_pct: float = 15.0,
    frame_cache: Mapping[str, pd.DataFrame] | None = None,
    data_mode: str = "",
    research_profile: ResearchPaperProfile | None = None,
    source_cache: dict[str, dict[str, Any]] | None = None,
    lifecycle_source_cache: dict[str, Any] | None = None,
) -> AccountReplayResult:
    """Run one forward account through kernel fills using only local evidence."""

    dates = [str(day)[:10] for day in window_dates]
    result = AccountReplayResult(
        account_id=account_id,
        universe_label=universe_label,
        strategy=strategy,
        window_start=dates[0] if dates else "",
        window_end=dates[-1] if dates else "",
        minute_window_start=dates[1] if len(dates) > 1 else "",
        minute_window_end=dates[-1] if dates else "",
        signal_day_count=max(0, len(dates) - 1),
        symbol_count=len(symbols),
        candidate_count=0,
    )
    if research_profile is not None:
        profile_validation = validate_research_paper_profile(research_profile)
        if profile_validation.get("ok") is not True:
            result.warnings.append("research_profile_invalid")
            result.warnings.extend(
                f"research_profile:{reason}"
                for reason in profile_validation.get("reason_codes") or []
            )
            return result
        pre_trade_config = PreTradeGateConfig(
            max_position_pct=float(research_profile.max_single_position_pct),
            max_positions=int(research_profile.max_paper_holdings),
            max_total_exposure=float(research_profile.max_total_exposure_pct),
        )
    else:
        pre_trade_config = PreTradeGateConfig()
    ledger = _fresh_account_ledger(account_id, initial_capital=initial_capital)
    ledger_source_path = f"memory://stage4-forward-replay/{account_id}"
    account_source_cache = source_cache if source_cache is not None else {}
    account_lifecycle_source_cache = (
        lifecycle_source_cache if lifecycle_source_cache is not None else {}
    )
    normalized_symbols = list(dict.fromkeys(normalize_symbol(item) for item in symbols if normalize_symbol(item)))
    frames = {
        symbol: (
            frame_cache[symbol]
            if frame_cache is not None and symbol in frame_cache
            else _read_daily_enriched(symbol, daily_dir)
        )
        for symbol in normalized_symbols
    }
    # Price-basis contract is checked on the complete frame set *before* any
    # filtering, sorting, deduplication, feature work, or write: a frame set
    # whose basis cannot be believed must not produce a single candidate. In
    # real_canonical mode the execution consumer only accepts raw prices, so a
    # qfq/hfq frame or a mixed frame blocks the whole account at zero output.
    price_contract: dict[str, Any] = {"eligible": True, "reason_codes": []}
    if data_mode == "real_canonical" and normalized_symbols:
        price_contract = validate_price_frame_set(frames, consumer="execution")
    result.price_contract = price_contract
    if price_contract.get("eligible") is not True:
        result.warnings.append("price_contract_blocked_no_candidates")
        return result
    open_trades: dict[str, dict[str, Any]] = {}
    if dates:
        result.nav_series.append({"date": dates[0], "nav": float(initial_capital)})

    for signal_index, (signal_date, execution_date) in enumerate(zip(dates, dates[1:])):
        snapshot = sector_snapshots.get(signal_date)
        snapshot_path = sector_snapshot_paths.get(signal_date)
        sector_status_path = (sector_status_paths or {}).get(signal_date)
        if not isinstance(snapshot, Mapping) or snapshot_path is None:
            result.warnings.append(f"sector_snapshot_missing:{signal_date}")
            continue
        industry_by_symbol = {
            normalize_symbol(row.get("symbol")): str(row.get("industry") or row.get("sector") or "")
            for row in snapshot.get("sector_membership") or []
            if isinstance(row, Mapping) and normalize_symbol(row.get("symbol"))
        }
        _mark_ledger(
            ledger,
            day=signal_date,
            frames=frames,
            industry_by_symbol=industry_by_symbol,
        )
        _settle_for_day(ledger, execution_date)

        for position in list(ledger.get("positions") or []):
            if not isinstance(position, Mapping):
                continue
            symbol = normalize_symbol(position.get("symbol"))
            frame = frames.get(symbol)
            if frame is None or frame.empty:
                continue
            rows = frame[frame["date"].dt.strftime("%Y-%m-%d") == signal_date]
            if rows.empty:
                continue
            row = rows.iloc[-1].to_dict()
            open_trade = open_trades.get(symbol, {})
            entry_index = int(open_trade.get("entry_index") or 0)
            holding_days = max(1, signal_index - entry_index + 1)
            should_exit, exit_reason = evaluate_exit_signals(
                row,
                entry_price=float(position.get("avg_cost") or position.get("entry_price") or 0.0),
                holding_days=holding_days,
                max_holding_days=max_holding_days,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )
            if not should_exit:
                continue
            outcome = _evaluate_position_sell(
                account_id=account_id,
                ledger=ledger,
                position=position,
                signal_date=signal_date,
                reference_price=float(row.get("close") or 0.0),
                amount_ma20=(float(row["amount_ma20"]) if pd.notna(row.get("amount_ma20")) else None),
                sector_rotation_path=sector_status_path,
                window_pool=window_pool,
                st_delist=st_delist,
                daily_dir=daily_dir,
                intraday_dir=intraday_dir,
                calendar_path=calendar_path,
                minute_snapshot_path=minute_snapshot_path,
                ledger_source_path=ledger_source_path,
                source_cache=account_source_cache,
            )
            result.exit_outcomes.append(outcome)
            result.exit_stage_counts[outcome.stage] = result.exit_stage_counts.get(outcome.stage, 0) + 1
            if outcome.kernel_event is None:
                continue
            projected, projection_reasons = _project_fill(ledger, outcome.kernel_event)
            if projected is None:
                result.warnings.extend(projection_reasons)
                continue
            ledger = projected
            _sync_ledger_cash(ledger)
            entry = open_trades.pop(symbol, {})
            entry_cash_out = float(entry.get("cash_out") or 0.0)
            exit_cash_in = float(outcome.kernel_event.cash_delta)
            pnl_pct = (
                round(exit_cash_in / entry_cash_out - 1.0, 8)
                if entry_cash_out > 0
                else None
            )
            evidence_ref = _digest(
                {
                    "buy_evidence": entry.get("evidence_ref"),
                    "sell_evidence": outcome.evidence_ref,
                    "buy_decision": entry.get("decision_digest"),
                    "sell_decision": outcome.decision_digest,
                }
            )
            result.closed_trades.append(
                {
                    "trade_id": f"{account_id}-{symbol}-{entry.get('entry_date')}-{execution_date}",
                    "account_id": account_id,
                    "symbol": symbol,
                    "entry_date": entry.get("entry_date"),
                    "exit_date": execution_date,
                    "hold_days": holding_days,
                    "pnl_pct": pnl_pct,
                    "fees": round(float(entry.get("fee") or 0.0) + outcome.kernel_event.total_fee, 8),
                    "evidence_eligible": bool(entry.get("evidence_ref") and outcome.evidence_ref),
                    "evidence_ref": evidence_ref,
                    "entry_evidence_ref": entry.get("evidence_ref"),
                    "exit_evidence_ref": outcome.evidence_ref,
                    "entry_decision_digest": entry.get("decision_digest"),
                    "exit_decision_digest": outcome.decision_digest,
                    "exit_reason": exit_reason,
                    "price_basis": "raw",
                    "reason_codes": [],
                }
            )

        held_symbols = {
            normalize_symbol(row.get("symbol"))
            for row in ledger.get("positions") or []
            if isinstance(row, Mapping)
        }
        for symbol in normalized_symbols:
            frame = frames.get(symbol)
            if frame is None or frame.empty:
                continue
            rows = frame[frame["date"].dt.strftime("%Y-%m-%d") == signal_date]
            if rows.empty:
                continue
            row = rows.iloc[-1].to_dict()
            row["date"] = signal_date
            name = str(names.get(symbol) or symbol)
            candidate = (
                long_signal_from_row(row, symbol=symbol, name=name)
                if strategy == STRATEGY_LONG
                else board_signal_from_row(row, symbol=symbol, name=name)
            )
            if candidate is None:
                continue
            result.candidate_count += 1
            if symbol in held_symbols:
                outcome = CandidateOutcome(
                    account_id,
                    symbol,
                    signal_date,
                    execution_date,
                    "intent",
                    "position_already_present",
                    ("position_already_present",),
                )
            else:
                candidate["amount_ma20"] = (
                    float(row["amount_ma20"]) if pd.notna(row.get("amount_ma20")) else None
                )
                # These three fields feed derive_candidate_evidence criterion 3 (as_of_clean).
                # data_latest_date: signal_date is the date filter used to select this row from
                #   frames[symbol]; the row matched, so signal_date is the actual last data date.
                candidate["data_latest_date"] = signal_date
                # is_target_date_covered: the row was found by frame["date"] == signal_date,
                #   confirming the target date is genuinely present in the canonical CSV on disk.
                candidate["is_target_date_covered"] = True
                # daily_feature_latest_date: rolling features come from the same enriched frame
                #   row selected at signal_date, so their currency matches signal_date.
                candidate["daily_feature_latest_date"] = signal_date
                outcome = _evaluate_candidate_buy(
                    account_id=account_id,
                    ledger=ledger,
                    candidate=candidate,
                    signal_date=signal_date,
                    industry=industry_by_symbol.get(symbol, ""),
                    window_pool=window_pool,
                    st_delist=st_delist,
                    daily_dir=daily_dir,
                    intraday_dir=intraday_dir,
                    calendar_path=calendar_path,
                    ledger_source_path=ledger_source_path,
                    sector_rotation_path=sector_status_path,
                    candidate_source_path=daily_dir / f"{symbol}.csv",
                    minute_snapshot_path=minute_snapshot_path,
                    pre_trade_config=pre_trade_config,
                    source_cache=account_source_cache,
                    lifecycle_source_cache=account_lifecycle_source_cache,
                )
            result.outcomes.append(outcome)
            result.stage_counts[outcome.stage] = result.stage_counts.get(outcome.stage, 0) + 1
            if outcome.kernel_event is None:
                continue
            projected, projection_reasons = _project_fill(ledger, outcome.kernel_event)
            if projected is None:
                result.warnings.extend(projection_reasons)
                continue
            ledger = projected
            _sync_ledger_cash(ledger)
            for position in ledger.get("positions") or []:
                if isinstance(position, dict) and normalize_symbol(position.get("symbol")) == symbol:
                    position["sector"] = industry_by_symbol.get(symbol, "")
                    position["name"] = name
                    position["market_value"] = round(
                        float(position.get("latest_price") or 0.0)
                        * int(position.get("quantity") or 0),
                        4,
                    )
            result.fill_count += 1
            open_trades[symbol] = {
                "entry_date": execution_date,
                "entry_index": signal_index + 1,
                "cash_out": -outcome.kernel_event.cash_delta,
                "fee": outcome.kernel_event.total_fee,
                "evidence_ref": outcome.evidence_ref,
                "decision_digest": outcome.decision_digest,
            }
            held_symbols.add(symbol)

        _mark_ledger(
            ledger,
            day=execution_date,
            frames=frames,
            industry_by_symbol=industry_by_symbol,
        )
        result.nav_series.append({"date": execution_date, "nav": _ledger_nav(ledger)})

    result.effective_sell_sample = sum(
        trade.get("pnl_pct") is not None for trade in result.closed_trades
    )
    return result


__all__ = [
    "CandidateOutcome",
    "AccountReplayResult",
    "replay_account",
    "replay_forward_account",
    "_fresh_account_ledger",
    "_theme_industry_map",
    "_signals_for_symbol",
]
