"""Explicit dependencies supplied by the compatibility entrypoints per invocation.

Factories remain callbacks: constructing a port never reads or writes a file.
Frozen bindings do not freeze the shared shadow list or the injected modules.
No execution, storage, data-provider or legacy-engine imports belong here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from datetime import tzinfo


class StateStore(Protocol):
    def read(self, *, default: Any = None) -> Any: ...
    def write(self, payload: dict[str, Any]) -> Any: ...


class OpenFill(Protocol):
    FILL_MODE_NEXT_OPEN_SNAPSHOT: str
    SHANGHAI: tzinfo

    def resolve_strategy_fill_mode(self, *args: Any, **kwargs: Any) -> Any: ...
    def aware_timestamp(self, *args: Any, **kwargs: Any) -> Any: ...
    def next_trading_day(self, *args: Any, **kwargs: Any) -> Any: ...
    def load_open_snapshot(self, *args: Any, **kwargs: Any) -> Any: ...
    def snapshot_price_gate(self, *args: Any, **kwargs: Any) -> Any: ...
    def evaluate_open_fill(self, *args: Any, **kwargs: Any) -> Any: ...
    def fill_journal_row(self, *args: Any, **kwargs: Any) -> Any: ...
    def make_pending_order(self, *args: Any, **kwargs: Any) -> Any: ...


class FillJournal(Protocol):
    def recoverable_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def state_digest(self, *args: Any, **kwargs: Any) -> Any: ...
    def validate_transaction(self, *args: Any, **kwargs: Any) -> Any: ...
    def commit_transaction(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ConditionalPaths:
    history_path: Path
    state_path: Path
    strategy_path: Path
    daily_dir: Path
    daily_features_path: Path
    intraday_dir: Path
    intraday_execution_path: Path
    results_dir: Path
    swing_plans_path: Path
    archive_project_root: Callable[..., Any]
    conditional_execution_path: Callable[..., Any]
    conditional_plan_path: Callable[..., Any]
    display_path: Callable[..., Any]
    risk_gate_shadow_error_path: Callable[..., Any]
    risk_gate_shadow_path: Callable[..., Any]
    sector_rotation_path: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalInfo:
    broker_boundary: str
    disclaimer: str


@dataclass(frozen=True)
class ConditionalPublication:
    append_history: Callable[..., Any]
    write_auto_paper_latest: Callable[..., Any]
    write_conditional_compat_artifacts: Callable[..., Any]
    write_report: Callable[..., Any]
    atomic_write_json: Callable[..., Any]
    publish_archived_conditional_plan: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalReview:
    attach_bound_replay_review: Callable[..., Any]
    conditional_review_summary: Callable[..., Any]
    enrich_positions_with_theme_map: Callable[..., Any]
    build_conditional_replay_receipt: Callable[..., Any]
    build_decision_audit: Callable[..., Any]
    build_decision_review: Callable[..., Any]
    build_yesterday_holding_review: Callable[..., Any]
    evaluate_data_freshness: Callable[..., Any]
    evaluate_paper_review_freshness: Callable[..., Any]
    intraday_gap_status: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalState:
    auto_paper_state_store: Callable[[], StateStore]
    load_state: Callable[..., Any]
    write_auto_paper_state: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalValuation:
    load_auto_adjustment_factors: Callable[..., Any]
    paper_correction_fields: Callable[..., Any]
    summary: Callable[..., Any]
    total_equity_from_state: Callable[..., Any]
    with_daily_pnl: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalRisk:
    record_shadow_failure: Callable[..., Any]
    summarize_shadow_records: Callable[..., Any]
    write_shadow_summary: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalPlans:
    apply_conditional_execution_to_state: Callable[..., Any]
    build_conditional_trade_plan: Callable[..., Any]
    enrich_legacy_due_plan_candidate_envelopes: Callable[..., Any]
    inspect_plan_minute_inputs: Callable[..., Any]
    load_archived_conditional_plan: Callable[..., Any]
    simulate_conditional_plan_file: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalClock:
    now_text: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalTrading:
    round_down_to_lot: Callable[..., Any]


@dataclass(frozen=True)
class ConditionalPorts:
    paths: ConditionalPaths
    info: ConditionalInfo
    publication: ConditionalPublication
    review: ConditionalReview
    state: ConditionalState
    valuation: ConditionalValuation
    risk: ConditionalRisk
    plans: ConditionalPlans
    clock: ConditionalClock
    trading: ConditionalTrading


@dataclass(frozen=True)
class SnapshotPaths:
    state_path: Path
    project_root: Path


@dataclass(frozen=True)
class SnapshotInfo:
    broker_boundary: str
    disclaimer: str
    policy_next_open: str


@dataclass(frozen=True)
class SnapshotState:
    auto_paper_state_store: Callable[[], StateStore]
    empty_state: Callable[..., Any]
    paper_state_shape_errors: Callable[..., Any]


@dataclass(frozen=True)
class SnapshotTrading:
    block_action: Callable[..., Any]
    buy_action: Callable[..., Any]
    candidate_price: Callable[..., Any]
    close_position: Callable[..., Any]
    hold_action: Callable[..., Any]
    make_position: Callable[..., Any]
    paper_action_boundary: Callable[..., Any]
    position_key: Callable[..., Any]
    sell_action: Callable[..., Any]
    snapshot_failure: Callable[..., Any]
    buy_cash_outflow: Callable[..., Any]
    compute_trade_costs: Callable[..., Any]
    normalize_position_for_t1: Callable[..., Any]
    normalize_symbol: Callable[..., Any]
    order_quantity_rule: Callable[..., Any]
    policy_header: Callable[..., Any]
    safe_float: Callable[..., Any]
    safe_int: Callable[..., Any]
    sell_cash_inflow: Callable[..., Any]


@dataclass(frozen=True)
class SnapshotValuation:
    daily_pnl_reference: Callable[..., Any]
    load_auto_adjustment_factors: Callable[..., Any]
    mark_position: Callable[..., Any]
    paper_correction_fields: Callable[..., Any]
    snapshot_daily_quotes: Callable[..., Any]
    summary: Callable[..., Any]
    with_daily_pnl: Callable[..., Any]
    reconcile_paper_account: Callable[..., Any]


@dataclass(frozen=True)
class SnapshotPublication:
    publish_snapshot_result: Callable[..., Any]


@dataclass(frozen=True)
class SnapshotReview:
    build_decision_audit: Callable[..., Any]


@dataclass(frozen=True)
class SnapshotClock:
    date: type
    datetime: type
    load_a_share_trading_calendar: Callable[..., Any]
    time: type
    utc_now_iso: Callable[..., Any]


@dataclass(frozen=True)
class SnapshotPorts:
    paths: SnapshotPaths
    info: SnapshotInfo
    state: SnapshotState
    trading: SnapshotTrading
    valuation: SnapshotValuation
    publication: SnapshotPublication
    review: SnapshotReview
    clock: SnapshotClock
    journal: FillJournal
    open_fill: OpenFill


@dataclass(frozen=True)
class DailyInfo:
    api_schema_version: str
    app_version: str
    broker_boundary: str
    disclaimer: str


@dataclass(frozen=True)
class DailyPaths:
    history_path: Path
    state_path: Path
    strategy_path: Path
    daily_dir: Path
    daily_features_path: Path
    intraday_dir: Path
    intraday_execution_path: Path
    project_root: Path
    results_dir: Path
    swing_plans_path: Path
    archive_project_root: Callable[..., Any]
    conditional_execution_path: Callable[..., Any]
    conditional_plan_path: Callable[..., Any]
    display_path: Callable[..., Any]
    risk_gate_shadow_path: Callable[..., Any]
    sector_rotation_path: Callable[..., Any]


@dataclass(frozen=True)
class DailyRisk:
    unrecorded: list[str]
    record_shadow_failure: Callable[..., Any]
    write_shadow_ledger_snapshot_once: Callable[..., Any]
    build_legacy_blocked_shadow_record: Callable[..., Any]
    evaluate_shadow_risk_order: Callable[..., Any]
    summarize_shadow_records: Callable[..., Any]
    write_shadow_summary: Callable[..., Any]


@dataclass(frozen=True)
class DailyPublication:
    append_history: Callable[..., Any]
    write_auto_paper_latest: Callable[..., Any]
    write_intraday_swing_artifacts: Callable[..., Any]
    write_report: Callable[..., Any]


@dataclass(frozen=True)
class DailyTrading:
    basis_fill_rejection: Callable[..., Any]
    block_action: Callable[..., Any]
    buy_action: Callable[..., Any]
    candidate_price: Callable[..., Any]
    fill_tradability_rejection: Callable[..., Any]
    hold_action: Callable[..., Any]
    is_long_strategy: Callable[..., Any]
    make_position: Callable[..., Any]
    position_key: Callable[..., Any]
    sell_available_position: Callable[..., Any]
    simulate_swing_for_row: Callable[..., Any]
    snapshot_failure: Callable[..., Any]
    swing_action_from_execution: Callable[..., Any]
    watch_exit_action: Callable[..., Any]
    apply_delisting_liquidation: Callable[..., Any]
    buy_cash_outflow: Callable[..., Any]
    lot_size_for_symbol: Callable[..., Any]
    normalize_position_for_t1: Callable[..., Any]
    normalize_symbol: Callable[..., Any]
    order_quantity_rule: Callable[..., Any]
    round_down_to_lot: Callable[..., Any]
    safe_float: Callable[..., Any]


@dataclass(frozen=True)
class DailyValuation:
    daily_pnl_reference: Callable[..., Any]
    latest_daily_quote: Callable[..., Any]
    load_auto_adjustment_factors: Callable[..., Any]
    mark_position: Callable[..., Any]
    paper_correction_fields: Callable[..., Any]
    quote_warnings: Callable[..., Any]
    summary: Callable[..., Any]
    with_daily_pnl: Callable[..., Any]


@dataclass(frozen=True)
class DailyState:
    empty_state: Callable[..., Any]
    invalid_paper_account_payload: Callable[..., Any]
    load_state: Callable[..., Any]
    strategy_book_existed: Callable[..., Any]
    write_auto_paper_state: Callable[..., Any]


@dataclass(frozen=True)
class DailyReview:
    enrich_positions_with_theme_map: Callable[..., Any]
    build_decision_audit: Callable[..., Any]
    build_decision_review: Callable[..., Any]
    build_yesterday_holding_review: Callable[..., Any]
    evaluate_data_freshness: Callable[..., Any]
    evaluate_paper_review_freshness: Callable[..., Any]


@dataclass(frozen=True)
class DailyDispatch:
    conditional: Callable[..., Any]
    snapshot: Callable[..., Any]


@dataclass(frozen=True)
class DailyPlans:
    build_conditional_trade_plan: Callable[..., Any]
    conditional_trade_plan_latest: Callable[..., Any]
    simulate_conditional_plan_file: Callable[..., Any]


@dataclass(frozen=True)
class DailyCandidates:
    candidate_rows_from_strategy_payload: Callable[..., Any]
    formal_candidate_strategy_payload: Callable[..., Any]
    infer_target_date: Callable[..., Any]
    load_formal_candidate_intake: Callable[..., Any]


@dataclass(frozen=True)
class DailyClock:
    now_text: Callable[..., Any]


@dataclass(frozen=True)
class DailyPorts:
    info: DailyInfo
    paths: DailyPaths
    risk: DailyRisk
    publication: DailyPublication
    trading: DailyTrading
    valuation: DailyValuation
    state: DailyState
    review: DailyReview
    dispatch: DailyDispatch
    plans: DailyPlans
    candidates: DailyCandidates
    clock: DailyClock
    open_fill: OpenFill
