# QuantDesk Script Governance And Entrypoint Inventory

Script governance is a safety workflow, not a mass delete. Normal operation should start from `python scripts/qd.py ...`. Raw scripts remain only as internal implementation files, compatibility adapters, historical diagnostics, or advanced troubleshooting tools.

## Recommended Commands

```powershell
python scripts/qd.py help
python scripts/qd.py status --json true --summary true
python scripts/qd.py maintenance script-inventory --json true --summary true
python scripts/qd.py maintenance git-deletion-audit --json true --summary true
python scripts/qd.py maintenance import-alias-reconciliation --json true --summary true
python scripts/qd.py maintenance strategy-regression-audit --json true --summary true
python scripts/qd.py maintenance test-inventory --json true --summary true
python scripts/qd.py maintenance test-discovery-diagnostics --json true --summary true
python scripts/qd.py maintenance test-tier-runner --tier tier0_fast_unit --dry-run true --json true --summary true
python scripts/qd.py maintenance pytest-suite-runner --suite all --dry-run true --json true --summary true
python scripts/qd.py maintenance frontend-api-contract --json true --summary true
python scripts/qd.py maintenance dependency-contract --json true --summary true
python scripts/qd.py maintenance data-backtest-contract --json true --summary true
python scripts/qd.py maintenance coverage-contract --json true --summary true
```

All maintenance commands are read-only by default. They do not enable network access, call LLMs, connect brokers, place real orders, or change strategy/backtest behavior.

## Inventory Outputs

`script-inventory` scans `scripts/` and writes:

- `results/maintenance/script_inventory_latest.json`
- `reports/maintenance/script_inventory_latest.md`

It classifies scripts as:

- `unified_cli`: the primary `qd.py` operator entrypoint.
- `qd_command_script`: a script already reachable through `qd.py`.
- `maintenance_subcommand_script`: a second-level maintenance command.
- `historical_versioned_raw_script`: a versioned stage script such as `run_v420_*.py` or `run_v5_*.py`.
- `raw_script_only`: a raw script that is not currently exposed through the unified CLI.

Historical raw script names that may still appear in old reviews or release notes
include `live_monitor.py`, `check_data_providers.py`, `daily_quant_report.py`,
`rebuild_cached_report.py`, and `generate_sample_report.py`. Treat these names
as compatibility/history references, not preferred operator entrypoints.

The inventory also scans source, docs, tests, frontend files, config, build files, and CI/tooling text for script path or filename references.

- `archive review candidate`: no textual reference was found in scanned source roots. This is a review queue, not automatic deletion approval.
- `referenced raw script`: the raw script is still referenced by docs, tests, scripts, package code, frontend code, config, build/CI files, or root files. Migrate or document those references before archiving.

## Dependency Contract

The project dependency boundary is already expressed in:

- `pyproject.toml`: package metadata, runtime dependencies, optional dependencies, dev dependencies, Ruff, and Mypy configuration.
- `requirements.txt`: runtime dependencies.
- `requirements-dev.txt`: test, lint, and type-check dependencies.
- `requirements-data.txt`: optional provider dependencies.

Do not hardcode one local machine's Python path as a project requirement. Documentation may use `python` as a placeholder for the active interpreter; Windows users may also use `py` or set `QUANTDESK_PYTHON`.

`dependency-contract` verifies this boundary stays aligned. It compares:

- `pyproject.toml` `[project].dependencies` with `requirements.txt`.
- `pyproject.toml` optional group `dev` with `requirements-dev.txt`.
- `pyproject.toml` optional group `data` with `requirements-data.txt`.

It also checks that optional provider packages such as `akshare` and `mootdx` do not drift into the runtime dependency file, and that dev tools such as `pytest`, `ruff`, and `mypy` stay out of runtime.

It writes:

- `results/maintenance/dependency_contract_latest.json`
- `reports/maintenance/dependency_contract_latest.md`

## Data Backtest Contract

`data-backtest-contract` is the read-only map for data quality and backtest evidence hygiene. It audits existing local outputs instead of rerunning a backtest:

- `results/backtest/backtest_latest.json`
- backtest artifact paths and file hashes.
- key assumptions such as cost fields, execution lag, PIT usage, survivorship-bias warning, and no-broker/no-real-order flags.
- daily retention manifest validation status when present.

It writes:

- `results/maintenance/data_backtest_contract_latest.json`
- `reports/maintenance/data_backtest_contract_latest.md`

## Coverage Contract

`coverage-contract` is a bounded `pytest-cov` gate for maintenance governance tests. It is intentionally not a full-suite coverage claim; it measures the current maintenance contract modules and writes a machine-readable XML report for CI review.

It runs only the governance files needed for the maintenance contracts and keeps the same research-only boundary: no market data refresh, no network provider calls, no broker route, no real orders, and no strategy/backtest behavior changes.

It writes:

- `results/maintenance/coverage_contract_latest.json`
- `reports/maintenance/coverage_contract_latest.md`
- `reports/coverage/coverage.xml`

Local runs warn with `pytest_cov_missing_install_requirements_dev` when `pytest-cov` is not installed. GitLab CI uses `--require-pytest-cov true` after installing `requirements-dev.txt`, so the CI gate must generate the XML report instead of silently degrading.

## Test Inventory And Tiers

Test inventory is the read-only map for splitting the large suite into practical local, domain, CLI/filesystem, and release gates.

`test-inventory` scans `tests/test_*.py` and writes:

- `results/maintenance/test_inventory_latest.json`
- `reports/maintenance/test_inventory_latest.md`

It groups tests into:

- `tier0_fast_unit`: default local smoke candidates.
- `tier1_focused_domain`: data, backtest, frontend, provider, network-boundary, or safety-boundary tests.
- `tier2_cli_or_filesystem`: subprocess, `qd.py`, temporary file, or artifact-output tests.
- `tier3_release_or_long`: release gates, very large files, or broad regression tests.
- `legacy_versioned_acceptance`: archived `test_v*` acceptance snapshots; excluded from default gates unless `--include-legacy true` is explicit.

`test-discovery-diagnostics` is the read-only probe for the common `unittest discover` / unittest discovery pain point. By default it excludes archived `test_v*` acceptance snapshots, then runs a subprocess import/discovery probe and samples priority files with per-file timeouts. Add `--include-legacy true` only when intentionally sweeping historical acceptance tests. Use it to distinguish:

- discovery/import problems: fix import-time side effects first.
- individual long files: keep them in tier3 release/nightly gates and run targeted tiers locally.
- normal large-suite cost: use `test-inventory` to choose the smallest relevant suite for the changed area.

It writes:

- `results/maintenance/test_discovery_diagnostics_latest.json`
- `reports/maintenance/test_discovery_diagnostics_latest.md`

`test-tier-runner` converts the inventory into a bounded command. It detects unittest-style and pytest-style files, chooses the matching runner, defaults to dry-run mode, refuses tier3 execution unless `--include-tier3 true` is explicit, and refuses legacy versioned acceptance tests unless `--include-legacy true` is explicit.

Examples:

```powershell
python scripts/qd.py maintenance test-tier-runner --tier tier0_fast_unit --limit 20 --dry-run true --json true --summary true
python scripts/qd.py maintenance test-tier-runner --tier tier0_fast_unit --category maintenance --limit 5 --dry-run false --json true --summary true
python scripts/qd.py maintenance test-tier-runner --tier tier3_release_or_long --include-tier3 true --limit 2 --dry-run true --json true --summary true
python scripts/qd.py maintenance test-tier-runner --tier legacy_versioned_acceptance --include-legacy true --limit 2 --dry-run true --json true --summary true
```

It writes:

- `results/maintenance/test_tier_runner_latest.json`
- `reports/maintenance/test_tier_runner_latest.md`

`pytest-suite-runner` splits the broad pytest surface into direct batches for debugging current red points without relying on one giant sweep:

- `core`
- `api`
- `research`
- `v3_v4_governance` (aliases: `v3-v4`, `governance`; includes versioned `test_v*` acceptance snapshots)
- `slow`

Examples:

```powershell
python scripts/qd.py maintenance pytest-suite-runner --suite all --dry-run true --json true --summary true
python scripts/qd.py maintenance pytest-suite-runner --suite core --dry-run false --timeout-seconds 120 --maxfail 10 --json true --summary true
python scripts/qd.py maintenance pytest-suite-runner --suite api --dry-run false --timeout-seconds 180 --maxfail 10 --json true --summary true
python scripts/qd.py maintenance pytest-suite-runner --suite research --dry-run false --timeout-seconds 240 --maxfail 10 --json true --summary true
python scripts/qd.py maintenance pytest-suite-runner --suite v3-v4 --dry-run false --timeout-seconds 240 --maxfail 10 --json true --summary true
python scripts/qd.py maintenance pytest-suite-runner --suite slow --dry-run false --timeout-seconds 300 --maxfail 10 --json true --summary true
```

It writes:

- `results/maintenance/pytest_suite_runner_latest.json`
- `reports/maintenance/pytest_suite_runner_latest.md`

## Frontend API Contract

`frontend-api-contract` is the read-only map for reducing hidden frontend/backend coupling. It statically scans the active daily research bundle/template (`app/static/js/daily_research.js`, `app/templates/daily_research.html`) and the active local server (`scripts/quant_api_server.py`) for `/api/...` references and routes. The frozen 8765 workbench files are intentionally outside the default gate, then reports:

- frontend endpoints that are missing on the server.
- frontend method gaps, such as a POST call with only a GET route.
- server routes not currently referenced by the frontend.
- dynamic or prefix route patterns that need manual review.

It writes:

- `results/maintenance/frontend_api_contract_latest.json`
- `reports/maintenance/frontend_api_contract_latest.md`

Unreferenced server routes are classified before any cleanup decision. The contract emits `server_unreferenced_route_reviews` with `usage_class`, `recommendation`, `keep_reason`, and `action_required` so health probes, legacy aliases, diagnostic/status endpoints, and report/artifact endpoints are not mixed with routes that truly need UI wiring or archive review. Dynamic frontend URLs also get `frontend_dynamic_api_reviews`; a dynamic route covered by a server prefix is recorded as `covered_by_server_prefix` instead of becoming a vague missing-endpoint warning.

For normal maintenance changes, run:

```powershell
python -m compileall -q quantdesk scripts tests
python -m unittest tests.test_script_inventory_governance -v
python -m unittest tests.test_test_inventory_governance -v
python -m unittest tests.test_test_discovery_diagnostics_governance -v
python -m unittest tests.test_test_tier_runner_governance -v
python -m unittest tests.test_frontend_api_contract_governance -v
python -m unittest tests.test_dependency_contract_governance -v
python -m unittest tests.test_data_backtest_contract_governance -v
python -m unittest tests.test_coverage_contract_governance -v
python scripts/qd.py maintenance coverage-contract --json true --summary true
python scripts/self_check.py
```

Before release gates, run bounded active tiers first and keep legacy acceptance explicit:

```powershell
python scripts/qd.py maintenance test-tier-runner --tier tier0_fast_unit --limit 40 --dry-run false --json true --summary true
python scripts/qd.py maintenance test-tier-runner --tier legacy_versioned_acceptance --include-legacy true --limit 2 --dry-run true --json true --summary true
```

The historical suite is large and slow. Do not remove valuable checks just to reduce runtime; keep old acceptance snapshots out of default gates until they are covered by focused active tests.

## Cleanup Rules

Use this order for script consolidation:

1. Run `script-inventory`.
2. Review `referenced_raw_scripts` first; migrate references or document why the script must stay.
3. Review `archive_review_candidates`; confirm tests, release packaging, docs, and operator workflows do not rely on them.
4. Promote still-useful scripts into `qd.py` or `maintenance`.
5. Mark compatibility-only scripts as legacy before archiving or deleting them.
6. Preserve Research Only, Paper Account Only, no broker route, no real orders, default network off, no default full-A scan, and no default LLM use.


## Authoritative Daily Data Chain

Normal post-close data maintenance uses the unified command:

```powershell
python scripts/qd.py daily-data-chain --enable-network --json true --summary true
```

The implementation entrypoint is `scripts/run_daily_data_chain.py`. It owns one
cross-process lock, one run ID, the daily/current-analysis-pool universe hash,
critical-minute priority, bounded minute batches, strict minute quality evidence,
feature rebuild, retry symbols, and the terminal manifest under
`results/data_chain/`. It never mutates the paper ledger.

`scripts/run_daily_data_chain.cmd` is a bounded Windows wrapper. It resolves
`QUANTDESK_PYTHON`, then the project `.venv`, then `py -3` / `python`.
Unknown arguments are rejected. Network access is explicit.

`scripts/install_daily_data_tasks.ps1` manages the single owned Windows task
`QuantDesk Daily Data Chain`. It is opt-in, requires `-EnableNetwork -Yes`,
uses 15:45 and 18:30 weekday triggers plus logon catch-up, queues a later
trigger when an earlier run is still active, keeps the Python OS lock as the
final overlap guard, and refuses to replace or remove an unowned same-name task.
