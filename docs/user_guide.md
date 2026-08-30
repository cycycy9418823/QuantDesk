# v3.7.0 Data Backbone

## v3.7.0.4 Diagnostics

Run `python scripts/qd.py maintenance git-deletion-audit --json true --summary true` before continuing feature work in a dirty worktree. Run `python scripts/qd.py strategy-viability capital-lot-sensitivity --json true --summary true` to review low-price cash deployment and lot-size constraints.

Use `qd data-db init` to initialize the SQLite shadow cache. Use `qd data-db disable` to return to CSV-only behavior without deleting CSV files. External provider refreshes require `network=true`.

# v3.6.2 Research Panels

Similar Day, Stress Test, Style Attribution, and Decision Journal are local-data, research-only panels. They read latest JSON outputs when present, warn when local data is missing, and never create broker actions or real orders.

```powershell
python scripts\qd.py similar-day --synthetic-test-data true --summary true
python scripts\qd.py stress-test --synthetic-test-data true --summary true
python scripts\qd.py style-attribution --synthetic-test-data true --summary true
python scripts\qd.py decision-journal --synthetic-test-data true --summary true
```

# v3.6.1 Paper Risk Loop

Run the paper-only risk loop:

```powershell
python scripts\qd.py smart-stop --synthetic-test-data true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --notify true --dry-run true --summary true
```

`risk-alerts` can read local paper positions from `state/auto_paper_portfolio.json`, `state/paper_account.json`, `state/paper_watchlist.json`, `results/multi_strategy_account_book_latest.json`, `results/intraday_live_workbench_latest.json`, and `results/stock_picking_flow_latest.json`. Missing files are warnings, not failures.

Notifications default to dry-run and local file output. External channels remain disabled unless explicitly configured.

# QuantDesk User Guide

QuantDesk is a local research and review workbench. It does not connect to
brokers, does not place real orders, and does not provide investment advice.

## Daily Local Loop

```powershell
python scripts/qd.py status
python scripts/qd.py data-health --network false --summary true
python scripts/qd.py daily --network false --with-guardrails true --summary true
```

This loop reads local data and latest results. It does not fetch new market data
unless a command is run with explicit network permission.

`data-update、data-health、daily-retain` are separate workflow states: update or
import local data, check coverage, and retain reproducibility artifacts. Their
warnings are diagnostics and do not become trading advice.

## Updating Local Data

Use explicit network permission only when you intend to fetch data:

```powershell
python scripts/qd.py data-update --enable-network --scope current_analysis_pool
```

After importing or updating daily CSV files, run:

```powershell
python scripts/qd.py data-health --network false --json true --summary true
```

## Research Stack

The current research stack is:

1. Factor evaluation.
2. Factor combination.
3. Combined-score research backtest.
4. Sector rotation.
5. Factor research dashboard.
6. Point-in-Time Universe Lite.

Run the full research review entrypoint:

```powershell
python scripts/qd.py research-dashboard --network false --summary true
```

## 10-Minute Real-Use Path

```powershell
python scripts\qd.py data-integrity --synthetic-test-data true --json true --summary true
python scripts\qd.py sector-rotation --synthetic-test-data true --json true --summary true
python scripts\qd.py strategy-recommend --synthetic-test-data true --json true --summary true
python scripts\qd.py stock-picking-flow --synthetic-test-data true --json true --summary true
python scripts\qd.py paper-watchlist --add-from-strategy --top 10 --json true --summary true
python scripts\qd.py smart-stop --synthetic-test-data true --json true --summary true
python scripts\qd.py risk-alerts --synthetic-test-data true --json true --summary true
```

This path is local and research-only. Real-data mode reads files you provide
under `data/`, `results/`, and `state/`; it does not fetch market data unless a
specific command is run with explicit network permission. Combine strategy
recommendation with risk attribution, experiments, and the decision journal for
review, not for broker execution.

## Point-in-Time Universe Lite

PIT universe metadata is optional and lives at:

```text
data/universe/historical_listings.csv
```

Recommended columns:

```text
symbol,name,list_date,delist_date,is_active,st_start_date,st_end_date,suspend_start_date,suspend_end_date,market,board,source,updated_at
```

Inspect PIT status:

```powershell
python scripts/qd.py pit-universe --summary true
python scripts/qd.py pit-universe --date 2024-01-15 --json true --summary true
```

If the file is missing, the command exits successfully with a warning and
`pit_universe=false`. QuantDesk does not fabricate delisting, ST, or suspension
history.

## Summary Output

Use `--summary true` for readable stdout and `--json true --summary true` for
compact valid JSON. The complete payload is still saved under `results/`.

## Strategy Recommendation

Strategy recommendation is a research-only classification layer. It combines
sector status, local symbol features, and risk flags to label candidates as
`long_term`, `long_swing`, `board`, `short_swing`, `observe`, or `avoid`.
It never places orders and does not write broker or portfolio state.

```powershell
python scripts/qd.py strategy-recommend --synthetic-test-data true --summary true
python scripts/qd.py strategy-recommend --symbol 600744 --summary true
python scripts/qd.py strategy-recommend --sector 电力 --top 10 --summary true
python scripts/qd.py strategy-recommend --top 20 --json true --summary true
```

Read the recommendation together with risk attribution, backtest assumptions,
and experiment logs. `confidence` is an explainable rule score, not a
probability, and rationale text is not a buy/sell instruction.

## Clean Release Checks

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\export_project.ps1
python scripts/verify_clean_zip.py --zip dist\quantdesk_clean.zip --expected-version 3.4.3.5
```

The clean zip excludes runtime data, reports, local daily data, universe files,
logs, state, `test_runtime/`, and nested zip files.

## Safety Notes

- 不自动下单。
- 不真实下单。
- 不接券商。
- 不构成投资建议。
- 更新今日/最近缺失日线 requires explicit network permission.
- 生成日报 and research reports are diagnostics, not trading instructions.
- 重新读取本地结果 does not fetch data.
# v3.5.1.1 Release Build Notes

`QuantDesk_v3.5.1.1_SourcePortable.zip` remains Python-required. Release stage directories under `dist/` are temporary and are cleaned after successful builds by default. Use `build/build_portable_release.ps1 -KeepStage` only when debugging package contents, and use `scripts/clean_release_staging.ps1` to remove old retained stages.

# v3.5.1.1 Attribution And Packaging Notes

- `qd attribution --synthetic-test-data true` runs true Brinson-Fachler attribution and Lite linear factor contribution.
- `qd risk-observation` runs five-dimensional diagnostics and is not Brinson attribution.
- `qd risk-attribution` remains a compatibility alias for observation diagnostics.
- `QuantDesk_v3.5.1.1_SourcePortable.zip` requires Python 3.10+; it does not bundle a Python runtime.
- `QuantDesk.exe` requires a successful PyInstaller build.
- `QuantDesk_Setup_v3.5.1.1.exe` requires both `QuantDesk.exe` and NSIS `makensis`.
- Network remains false by default; no broker and no real orders are implemented.
