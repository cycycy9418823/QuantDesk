# QuantDesk remediation flow

## v3.4.2 command safety wording

Remediation distinguishes two data-update meanings:

- Offline check / refresh local state: `python scripts/qd.py data-update --target-date YYYY-MM-DD --network false --scope current_analysis_pool`. This only checks and does not fill data.
- Online daily-data repair: `python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool`. This requires explicit network authorization.

When daily data is stale, the primary repair command is the online `--enable-network` command and is marked as requiring explicit network authorization. Offline commands remain available for diagnosis, but they must not be described as data repair.

v3.3.5 adds a local remediation layer that turns common warning states into copyable repair commands. It is a workflow guide only: it does not connect to brokers, does not place orders, does not promise returns, and does not constitute investment advice.

## Entry points

- `python scripts/qd.py status` shows current local state plus the top remediation actions.
- `python scripts/qd.py fix-plan` shows only the repair plan and writes `results/fix_plan_latest.json`.
- `python scripts/qd.py daily --network false --with-guardrails true` runs the daily workflow and embeds `remediation_plan` in `results/daily_workflow_latest.json`.

All three commands are read-only unless the user explicitly runs a follow-up command. They do not enable network or LLM by default.

## Common warnings

- Stale daily data: run `python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool`, then `python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool`.
- Missing intraday data: import real `data/intraday/1m/{target_date}/{symbol}.csv`, then rerun `data-health`. QuantDesk never uses daily high/low to pretend that an intraday trigger happened.
- Statistical sample warning: run `stats-validate` if you need a focused check, but treat the result as learning context rather than proof.
- Parameter sensitivity warning: run `param-sensitivity` as an audit only. It is not an optimizer and does not choose parameters.
- Schema secret-like warning: high-risk count `0` usually means paths, examples, or masked values. Confirmed secret leaks are critical and must be removed.
- Provider skipped offline: this is the default safe offline behavior, not a provider failure.

## Output fields

Recent JSON payloads may include:

- `remediation_plan`
- `remediation_actions`
- `warning_kind`
- `severity`
- `network_required`
- `follow_up_command`

These fields are designed for command-center and artifact-center display. They are not trading signals.

## v3.3.6 UI copy and preview rules

The UI uses one user-facing phrase for warning recovery: “我现在该怎么修”. Command-center rows use “修复建议”, and artifact-center missing/stale files use “重新生成 / 修复命令”.

Remediation commands are grouped by safety:

- “离线可运行，不会联网”: commands such as `data-health`, `daily`, `schema-check`, and `fix-plan`.
- “需要显式联网授权”: commands such as `data-update --enable-network`. These commands are never run by the UI; the user must copy and run them.

Each displayed command has a copy button. Long commands are shortened on screen, but the copy payload keeps the full command. If a command has more than 10 symbols, the UI hides the long list and offers “查看完整命令”.

Artifact-center previews are read-only. JSON shows summary fields first (`schema_version`, `app_version`, `status`, `generated_at`, `target_date`, `warning_count`, `failed_count`, `remediation_actions` count) and can show raw first 30 lines. Markdown reports preview the first 80 lines and can copy the path or summary. Missing means “未生成”, not a failure. All remediation guidance is workflow guidance and is not investment advice.
# v3.3.7 quieter repair output

`data-update` and `data-health` now show short console summaries by default. Long `symbols` lists are previewed, complete lists remain in JSON, and `--json true` / `--verbose true` prints full details when debugging.

UI remediation cards still copy the full command even when the visible command is shortened. Offline commands are marked as safe to run without network; commands that update data must say `需要显式联网授权` and only run when the user explicitly copies and executes them. These are workflow suggestions, not investment advice.
