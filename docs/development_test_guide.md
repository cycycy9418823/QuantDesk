# Development And Test Guide

QuantDesk is local-only, research-only, and paper-account-only. Development commands must keep network off by default and must never connect brokers, place real orders, or turn diagnostics into investment advice.

## Environment

Use Python 3.10 or newer. On Windows, `python` in docs means the active interpreter; `py` or `QUANTDESK_PYTHON` are also acceptable for helper scripts.

Runtime dependencies are declared in:

- `pyproject.toml`: package metadata and dependency groups.
- `requirements.txt`: runtime dependency snapshot.
- `requirements-dev.txt`: local test/lint/type-check tools.
- `requirements-data.txt`: optional provider dependencies such as AKShare or mootdx.

Recommended local setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Install `requirements-data.txt` only when explicitly testing optional data providers. Unit tests and maintenance diagnostics must not require network providers.

## App Entrypoints

Use `python scripts/qd.py app` for the current daily research app on port 8000. It launches `scripts/launcher.py` and does not run a startup provider refresh unless `--update-on-start` is passed explicitly. `python -m ai_trader app` is the supported 8001 compatibility classroom. The older 8765 workbench remains retired and is kept only for compatibility review.

## First Checks

```powershell
.\.venv\Scripts\python.exe -m compileall -q quantdesk scripts tests
.\.venv\Scripts\python.exe scripts\qd.py maintenance script-inventory --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-inventory --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-discovery-diagnostics --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-tier-runner --tier tier0_fast_unit --dry-run true --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite all --dry-run true --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance frontend-api-contract --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance dependency-contract --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance data-backtest-contract --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance coverage-contract --json true --summary true
.\.venv\Scripts\python.exe scripts\self_check.py
```

The maintenance commands are read-only. They write JSON/Markdown diagnostics under `results/maintenance` and `reports/maintenance`; the bounded coverage gate also writes `reports/coverage/coverage.xml`.

## Test Tiers

Use `test-inventory` to choose the smallest relevant suite:

- `tier0_fast_unit`: default local smoke candidates.
- `tier1_focused_domain`: data, backtest, frontend, provider, network-boundary, or safety-boundary tests.
- `tier2_cli_or_filesystem`: subprocess, `qd.py`, temporary file, or artifact-output tests.
- `tier3_release_or_long`: broad release gates, very large files, or long regression files.
- `legacy_versioned_acceptance`: archived `test_v*` acceptance snapshots; excluded from default gates unless `--include-legacy true` is explicit.

For maintenance-only changes:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_script_inventory_governance tests.test_test_inventory_governance tests.test_test_discovery_diagnostics_governance -v
.\.venv\Scripts\python.exe -m unittest tests.test_test_tier_runner_governance -v
.\.venv\Scripts\python.exe -m unittest tests.test_frontend_api_contract_governance -v
.\.venv\Scripts\python.exe -m unittest tests.test_dependency_contract_governance -v
.\.venv\Scripts\python.exe -m unittest tests.test_data_backtest_contract_governance -v
.\.venv\Scripts\python.exe -m unittest tests.test_coverage_contract_governance -v
.\.venv\Scripts\python.exe -m unittest tests.test_gitlab_ci_governance -v
```

To run a bounded inventory selection instead of hand-picking files:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-tier-runner --tier tier0_fast_unit --limit 20 --dry-run false --json true --summary true
```

The runner detects pytest-style top-level test functions and unittest `TestCase` files, then chooses the matching command automatically.

## Pytest Suite Runner

When a broad `python -m pytest` sweep becomes too slow or noisy, use `pytest-suite-runner` to split the project into direct pytest batches:

- `core`: small infrastructure and unit tests that do not match the larger domains.
- `api`: active port-8000 API, dashboard, frontend contract, route, news, and theme-map tests.
- `research`: backtest, factor, screen, candidate, strategy, paper, data, provider, metadata, and enriched-data tests.
- `v3_v4_governance`: versioned acceptance snapshots (`test_v*`) and governance/contract/inventory tests.
- `slow`: large, subprocess, release, clean-zip, or explicitly long-running tests.

Plan first:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite all --dry-run true --json true --summary true
```

Then run one layer at a time:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite core --dry-run false --timeout-seconds 120 --maxfail 10 --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite api --dry-run false --timeout-seconds 180 --maxfail 10 --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite research --dry-run false --timeout-seconds 240 --maxfail 10 --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite v3-v4 --dry-run false --timeout-seconds 240 --maxfail 10 --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance pytest-suite-runner --suite slow --dry-run false --timeout-seconds 300 --maxfail 10 --json true --summary true
```

The runner writes `results/maintenance/pytest_suite_runner_latest.json` and `reports/maintenance/pytest_suite_runner_latest.md`. It is read-only with respect to source, protected data, broker state, and release packaging; only diagnostic result/report artifacts are written.

Tier3 execution is intentionally gated:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-tier-runner --tier tier3_release_or_long --include-tier3 true --limit 2 --dry-run true --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-tier-runner --tier legacy_versioned_acceptance --include-legacy true --limit 2 --dry-run true --json true --summary true
```

Before a release or broad regression pass, use the tier runner first and only run legacy snapshots explicitly:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-tier-runner --tier tier0_fast_unit --limit 40 --dry-run false --json true --summary true
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-tier-runner --tier legacy_versioned_acceptance --include-legacy true --limit 2 --dry-run true --json true --summary true
```

If active discovery or a broad regression sweep feels stuck, run:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance test-discovery-diagnostics --json true --summary true --sample-limit 12 --per-file-timeout-seconds 8
```

Interpretation:

- default import discovery excludes archived `test_v*` snapshots; pass `--include-legacy true` only for archival sweeps.
- import discovery passes quickly: the active loader is healthy; slow files belong in tier3 or need targeted splitting.
- import discovery fails or times out: fix import-time side effects before running the selected suite.
- per-file probes time out: split that file later or keep it in release/nightly gates.

## Frontend API Contract

When touching the active daily research UI (`app/static/js/daily_research.js`, `app/templates/daily_research.html`, or `scripts/quant_api_server.py`), run:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance frontend-api-contract --json true --summary true
.\.venv\Scripts\python.exe -m unittest tests.test_frontend_api_contract_governance -v
```

This is a static local check. It does not start the app server or call any provider. Treat missing frontend routes as repair work, and treat unreferenced server routes as review candidates before removing anything.

The contract scans the active daily research bundle/template, then compares them with the active local server entrypoint (`scripts/quant_api_server.py`). The legacy 8765 workbench files are intentionally outside the default gate; if a new active page adds a static JS file or template with `/api/...` calls, it should appear in `frontend_sources` so opening the program cannot drift away from what the maintenance gate verifies.

When `server_unreferenced_by_frontend_count` is nonzero, use `server_unreferenced_route_reviews` instead of deleting routes directly. `action_required=true` means the route needs a UI/operator entry or an archive decision; operational probes, legacy aliases, diagnostics, and report/artifact routes may be intentionally kept with a visible `keep_reason`.

## Dependency Contract

When touching `pyproject.toml` or any `requirements*.txt` file, run:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance dependency-contract --json true --summary true
.\.venv\Scripts\python.exe -m unittest tests.test_dependency_contract_governance -v
```

The runtime file must match `pyproject.toml` core dependencies. Provider packages belong in `requirements-data.txt` / `.[data]`, and test or lint tools belong in `requirements-dev.txt` / `.[dev]`.

## Data Backtest Contract

When touching local data loaders, backtest outputs, PIT assumptions, transaction-cost logic, or retention manifests, run:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance data-backtest-contract --json true --summary true
.\.venv\Scripts\python.exe -m unittest tests.test_data_backtest_contract_governance -v
```

This check reads existing output metadata and file hashes only. It must not refresh data, rerun strategies, or hide missing PIT, benchmark, data-gate, or survivorship-bias warnings.

## Coverage Contract

When touching maintenance governance modules or CI, run:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py maintenance coverage-contract --json true --summary true
.\.venv\Scripts\python.exe -m unittest tests.test_coverage_contract_governance -v
```

This is a bounded `pytest-cov` pass for the maintenance contract modules, not a full-project coverage claim. It records `line_percent`, `branch_percent`, and `reports/coverage/coverage.xml` so GitLab review can see whether the maintenance safety layer was exercised.

If a local machine has not installed `requirements-dev.txt`, this command reports `pytest_cov_missing_install_requirements_dev` as an environment warning. GitLab CI runs the same gate with `--require-pytest-cov true` after installing dev dependencies, so CI must produce the XML artifact.

## GitLab CI

`.gitlab-ci.yml` runs offline validation for code exchange on GitLab:

- Python compile checks.
- frontend API contract, dependency contract, data/backtest contract, and bounded coverage contract.
- maintenance governance tests.
- `scripts/self_check.py`.
- `node --check app/static/js/daily_research.js`.
- `reports/coverage/coverage.xml` as a CI artifact.

The CI intentionally does not fetch market data, run full-A scans, package releases, push git branches, connect brokers, or place real orders.

## Release Hygiene

Do not commit runtime outputs, protected data, local caches, logs, release zips, or secrets. `dist/`, `results/`, `reports/`, `data/daily/`, `data/intraday/`, `data/universe/`, `data/features/`, `logs/`, and `state/` are generated/runtime areas.

For release packaging work only, run:

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\export_project.ps1
```

Use `-KeepStage` only for debugging failed packaging.
