# Codebase Slimming Plan (v2.6.3)

Active launch/operator surface is 8000 (`scripts/quant_api_server.py`,
`app/templates/daily_research.html`, `app/static/js/daily_research.js`) plus
the supported 8001 AI trader classroom. Any references below to
`quant_app_server.py` or `app/static/app.js` are historical slimming inventory,
not current product instructions.

This document records the **future** split map for the four largest
modules in the project. The v2.6.3 release does **not** apply these
splits. The goal here is to make later refactors small, reviewable, and
behaviorally identical.

## v3.3.2.2 Repo Hygiene Note

- Root-level one-off verification JSON and temporary directories are excluded by `.gitignore` (`.tmp/`, `.tmp_tests/`, `.quantdesk_test_tmp/`, `tmp*/`, `*_verify.json`, `*_cli_verify.json`, `_maintenance/`) and should not be kept in clean zip.
- `pyproject.toml` now declares core runtime dependencies, while pytest remains dev-only and mootdx remains optional provider tooling.
- This patch deliberately avoids splitting large modules such as `app.js` or `backtest.py`; it only removes unreachable code and stabilizes tests.

## Guiding Principles

- **Behavior is frozen.** Public function names, CLI flags, API routes,
  and JSON shapes must not change in any single split commit.
- **Pure logic first.** Move only stateless helpers in early steps
  (parsing, formatting, math, validation). Stateful or I/O code stays
  put until a separate, minimal commit.
- **No new business features in slimming commits.** A slimming commit
  must not add a new strategy, factor, indicator, score, or trading
  capability.
- **Tests stay green.** `python -m unittest discover` and
  `python scripts/self_check.py` must pass before and after each split
  commit.
- **No broker, no live trading.** Every split keeps the project
  boundary: not connected to brokers, no automated orders, no predicted
  prices, not investment advice.

## Already-Done Slimming

### v2.6.3

- `quantdesk/self_check_core.py` extracts pure helpers used by
  `scripts/self_check.py` (danger-phrase rules, mojibake detection,
  hardcoded-path scanner, version centralization, clean-zip auditing).
- `quantdesk/server_payloads.py` extracts pure helpers used by
  `scripts/quant_app_server.py` (`safe_float`, `safe_int`,
  `normalize_code`, `parse_report_summary`, `risk_summary_rows_to_dict`,
  `market_temperature_from_summary`, `infer_report_date`, and the
  watchlist / manual-confirmation request normalizers).

### v2.6.4

- `quantdesk/data_health_recorder.py` extracts data-health recording
  and aggregation logic from `scripts/daily_quant_report.py`
  (`DataFetchStatus`, `DATA_HEALTH_ROWS`, `DATA_HEALTH_COLUMNS`,
  `DATA_STALE_WARNING_DAYS`, `DATA_STALE_ERROR_DAYS`,
  `DATA_MIN_HISTORY_ROWS_WARNING`, `reset_data_health`,
  `record_data_health`, `data_health_frame`, `data_health_summary`,
  `data_health_reason_summary`, `attach_data_health`,
  `data_quality_risk_note`, `data_risk_flag`,
  `expected_latest_trading_date`, `_health_reference_datetime`,
  `data_freshness_status_from_quote`, `classify_data_warning`).

All extractions kept the original public function names on the script
side as **thin shims**, so existing tests and callers continue to work.

## scripts/daily_quant_report.py (≈3200 lines)

Suggested target sub-modules (under `quantdesk/`):

| New module | Responsibility | Functions to move |
|---|---|---|
| `data_health_recorder.py` | Local data-health row recording, summary, reasons. | `reset_data_health`, `record_data_health`, `data_health_frame`, `data_health_summary`, `data_health_reason_summary`, `attach_data_health`, `data_quality_risk_note`, `data_risk_flag`, `expected_latest_trading_date`, `_health_reference_datetime`, `data_freshness_status_from_quote`, `classify_data_warning`. |
| `news_health_recorder.py` | News-health row recording and summary. | `reset_news_health`, `record_news_health`, `news_health_frame`, `news_health_summary`, `attach_news_health`. |
| `spot_snapshot_cache.py` | Read / write `data/cache/spot_snapshot_*` artifacts. | `_spot_cache_meta_defaults`, `_load_spot_snapshot_cache`, `_write_spot_snapshot_cache`, `_filter_spot_snapshot`. |
| `fallback_universe.py` | Deterministic fallback universe assembly. | `_read_symbol_table`, `_collect_fallback_universe`, `_normalize_symbol`, `_extract_symbol_name`. |
| `local_daily_cache_stats.py` | `LOCAL_DAILY_CACHE_STATS` accumulators. | `reset_local_daily_cache_stats`, `_set_local_daily_cache_latest`, `local_daily_cache_stats_payload`. |

Slimming order (each step is an independent commit):

1. Move data-health recorder. `daily_quant_report.py` re-exports the
   moved names so `tests/test_*` keep working.
2. Move news-health recorder.
3. Move spot-snapshot cache I/O (verify `update_market_data.py` and
   the report still read / write the same files).
4. Move fallback-universe helpers.
5. Move local daily cache stats.

After all five commits the script becomes mostly orchestration code:
read config, drive the helpers, and write the report.

## quantdesk/backtest.py (≈2050 lines)

Suggested target sub-modules:

| New module | Responsibility | Functions to move |
|---|---|---|
| `backtest_signals.py` | No-lookahead signal frame, strategy normalization. | `_ensure_trade_aliases`, `_normalize_strategy_name`, `_legacy_ma_trend_definition`, `_resolve_strategy_definition`, `_score_component_value`, `_score_components_for_row`, `backtest_signal_frame`. |
| `backtest_assumptions.py` | Cost / commission / assumption tables. | `default_backtest_assumptions`, `backtest_assumptions_from_config`, `portfolio_backtest_assumptions_from_config`, `backtest_cost_warning`, `commission_for`, `safe_number`. |
| `backtest_summary.py` | Summary / risk / blocked-reason tables. | `summary_rows_from_metrics`, `blocked_reason_counts`, `risk_summary_rows_from_result`, `metrics_from_equity`. |
| `backtest_loaders.py` | Local benchmark and industry-map loaders. | `load_benchmark_series`, `load_industry_map`, `benchmark_total_return`. |

`backtest.py` keeps the dataclasses (`BacktestConfig`,
`PortfolioBacktestConfig`, `BacktestMetrics`, `BacktestResult`) and the
two simulators (`simulate_single_asset_backtest`,
`simulate_portfolio_backtest`) so call sites stay unchanged.

Order: signals → assumptions → summary → loaders.  Each step keeps the
prior commit's API on `backtest.py` via `from .backtest_signals import *`.

## quantdesk/strategy_trade_replay.py (≈1470 lines)

Suggested target sub-modules:

| New module | Responsibility | Functions to move |
|---|---|---|
| `trade_replay_market_rules.py` | A-share market-rules adapter. | `_market_rule_config`, `_market_rule_block`, `_normalize_price_mode`, `_apply_slippage`, `_lot_quantity`, `_fee_amount`. |
| `trade_replay_execution.py` | Pure entry / exit execution helpers. | `_execute_on_signal_close`, `_execute_on_next_open`, `_infer_position_size`, `_risk_line_price`, `_close_price`, `_open_price`, `_num`, `_date_text`, `_days_between`, `_as_frame`. |
| `trade_replay_validation.py` | Parameter validation and empty-result builder. | `_validate_params`, `_empty_result`, `TradeReplayParameterError`, `_dedupe`, `_condition_strings`, `_append_unique`. |

`strategy_trade_replay.py` keeps `strategy_trade_replay()` and
`strategy_trade_replay_to_dict()` as the public surface the API uses.

## quantdesk/explanations.py (≈860 lines)

Suggested target sub-modules:

| New module | Responsibility | Functions to move |
|---|---|---|
| `explanations_card.py` | Decision-card / compact-card rendering. | `build_candidate_decision_card`, `render_compact_candidate_card`, `build_candidate_decision_card_markdown`, `_compact_items`, `_compact_text`, `_first_items`, `_data_status`, `_prioritize_risks`. |
| `explanations_text.py` | Conservative beginner-friendly bullets. | `beginner_reason`, `tonghuashun_steps`, `indicator_table_rows`, `stock_learning_notes`, `trade_checklist`, `trade_plan`, `action_script_lines`, `risk_flags`, `explain_candidate`, `explain_risk_flags`, `explain_data_warning`, `build_pre_trade_checklist`, `build_invalid_conditions`, `build_beginner_explanation`. |
| `explanations_helpers.py` | Tiny pure helpers and the `DANGEROUS_PHRASES` rule list. | `stock_code`, `row_float`, `attention_type`, `data_risk_flag`, `position_summary`, `news_bullets`, `brief_action`, `operation_advice`, `report_toc_lines`, `DANGEROUS_PHRASES`. |

`explanations.py` keeps `from .explanations_card import *` etc. so every
call-site (`reporting.py`, `word_report.py`, `quant_app_server.py`)
continues to import names from `quantdesk.explanations`.

## When to Apply

- Apply at most **one** sub-module split per release.
- Do not bundle a slimming commit with a new feature commit.
- Always run `python scripts/self_check.py` and
  `python -m unittest discover` between commits.
- If a split would change a public API, stop and write an ADR-style
  note in `docs/known_logic_issues.md` first.

## What This Plan Is Not

- It is not a promise of better signals, better backtests, or higher
  scores.
- It is not a switch to a new framework, ORM, or web stack.
- It does not enable broker integration, automated orders, or any form
  of trading advice.
