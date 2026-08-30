# QuantDesk Price Data Contract

Version: `1.0`

Status: `P0 fail-closed contract`
Scope: local research and paper-account paths only; no broker route and no real orders.

This is the single price-basis protocol for current and future QuantDesk modules,
including Market Brain, Strategy Brain, AI Memory, feature generation, backtest,
historical replay, and paper-account evidence. The machine-readable source is
`quantdesk.price_basis.price_data_contract()`.

## Basis vocabulary

- `raw`: unadjusted exchange price.
- `forward_adjusted`: qfq research view.
- `back_adjusted`: hfq research view.
- `mixed_or_unknown`: untrusted; formal calculation or simulation is blocked.

An absent `adjust` column, null/unknown tokens, known+unknown tokens, and more
than one known basis are never guessed from the file name, provider, source, or
directory. No validator converts or rewrites prices.

## Machine-readable policy

```yaml
price_data_contract:
  version: "1.0"
  enforcement_mode: fail_closed

  current_enforced:
    feature:
      price_role: research_signal_price
      required_basis: single_known_basis
      allowed_basis: [raw, forward_adjusted, back_adjusted]
      cross_symbol_consistency: required
      mixed_or_unknown: block_feature_row
      cross_symbol_mismatch: block_run_before_write

    backtest:
      signal_price_role: research_signal_price
      fill_price_role: research_simulation_price
      architecture: single_frame_compatibility
      required_basis: single_known_basis
      allowed_basis: [raw, forward_adjusted, back_adjusted]
      fill_price_basis: inherits_validated_input_basis
      cross_symbol_consistency: required
      mixed_or_unknown: block_before_simulation
      non_raw_fill_label: same_basis_research_proxy
      real_execution_price_claim: false

    rule_replay:
      price_role: historical_signal_price
      required_basis: single_known_basis
      allowed_basis: [raw, forward_adjusted, back_adjusted]
      cross_symbol_consistency: required
      execution: none
      mixed_or_unknown: block_formal_metrics

    historical_intraday_replay:
      required_basis: raw
      signal_threshold_basis: raw
      execution_price_basis: raw
      execution_price_field: minute.close
      mixed_or_unknown: block_before_plan
      adjusted_signal_to_raw_bridge: not_implemented

    trade_replay:
      architecture: single_frame
      required_basis: raw
      execution_price_fields: [close, next_open]
      adjusted_signal_to_raw_bridge: not_implemented

    paper_execution:
      price_role: real_execution_price
      required_basis: raw
      allowed_sources:
        - quality_gated_intraday_close
        - unadjusted_daily_open
        - unadjusted_daily_close
      slippage_application: after_raw_base_price
      broker_route: false
      real_order: false

    paper_valuation:
      latest_price_basis: raw
      corporate_action_policy: raw_price_plus_canonical_adjustment_factor_for_quantity_and_cost_basis

  future_target:
    feature: forward_adjusted
    backtest_signal: forward_adjusted
    backtest_research_return: forward_adjusted_total_return_view
    replay_signal: forward_adjusted
    execution: raw_real_execution_price
    simulation: dual_track_adjusted_signal_plus_raw_execution
    signal_to_execution_bridge:
      required: true
      factor_source: data/metadata/adjustment_factors.csv
      as_of_date_aware: true
      divergent_alias_allowed: false
```

## Current enforcement versus future target

The current backtest engine uses one validated frame for both signals and
simulated fills. A pure qfq/hfq run remains compatible, but its fills are
explicitly labelled `same_basis_research_proxy`; they are not claimed to be raw
real-execution prices. Changing that engine to qfq signals plus raw fills would
change simulation results and requires a separate approved dual-track migration.

The future target is therefore normative for new architecture, not a claim that
the bridge already exists. Market Brain, Strategy Brain, and AI Memory must
preserve these roles and provenance fields; they must not silently promote the
future target to current evidence.

## Validation order and stable reasons

Validation happens before date filtering, sorting, duplicate-date removal,
feature calculation, plan construction, or backtest simulation. The complete
requested backtest universe is blocked if any requested symbol has an untrusted
basis; a bad symbol is never silently removed to manufacture a smaller run.

Stable reason codes:

- `mixed_price_basis`
- `unknown_price_basis`
- `cross_symbol_price_basis_mismatch`
- `consumer_price_basis_not_allowed`
- `unknown_price_consumer`
- `no_price_frames`

Every formal consumer must use `classify_frame_price_basis()` and/or
`validate_price_frame_set()`. Observed qfq/hfq evidence must use
`observed_price_basis_labels()`; `price_basis_labels()` is only for requested
data-selection policy and may legitimately describe directory fallback.

## P0-2 implementation coverage

- Daily feature engine: invalid symbols are excluded with per-symbol reasons;
  known cross-symbol basis conflict blocks the run before output overwrite.
- CLI backtest: mixed, unknown, or cross-symbol mismatch blocks the complete run
  before either single or portfolio simulation.
- Active port-8000 backtest route: same pre-simulation fail-closed gate.
- Daily report direct backtest path: both modes go through one validation helper.
- Historical intraday replay: raw-only gate runs before conditional-plan creation
  because daily thresholds are compared with raw minute prices.
- The contract defines raw-only requirements for trade replay and paper
  execution. Wiring an adjusted-signal/raw-execution bridge is a later,
  result-changing migration and is not implemented by P0-2.

## Current canonical audit snapshot (2026-07-16)

The 2026-07-13 read-only audit found `5,824` pure-raw files and `40` files with
one raw-history / qfq-labelled row. Recorded local update evidence proved that
the 2026-07-08 bounded Tencent request received its unadjusted `day` fallback
for exactly those 40 BSE symbols. The provider-side prevention fix was already
in place; the separately authorised repair changed only the incorrect basis
label in the canonical files:

- Files checked: `5,864`
- Pure raw after repair: `5,864`
- Mixed raw+qfq after repair: `0`
- Unknown after repair: `0`
- Changed paths: exactly the fixed `40` `data/daily/920xxx.csv` targets
- Added or removed canonical files: `0`
- Before fingerprint: `efce3c459da51d355d5e9187e8762a2b8a8d845a6a66289a5e355bff18e17ce5`
- After fingerprint: `e3e68a47f35adafbc282b4466176a148c28255585baae5d97329ac0b8087a05e`
- OHLCV changes: `0`
- Filled missing sessions: `0`; known 2026-07-03/06/07 gaps remain visible
- Recovery copies: ignored runtime evidence under
  `results/release/bse_daily_basis_repair/efce3c459da51d35/before/`

The repair was not a price normalization or data backfill: each affected byte
sequence changed only from `,qfq,tencent_daily,` to `,raw,tencent_daily,`.
Formal consumers still classify the original complete frame and fail closed on
mixed/unknown evidence. Existing artifacts keep their own recorded provenance;
they are only refreshed by their normal explicit workflow commands.
