# QuantDesk Start Here

QuantDesk is a local A-share research and paper-account tool. It does not
connect to brokers, place real orders, or provide investment or position advice.

## Current Application

From the repository root, run:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py app
```

Then open `http://127.0.0.1:8000/`.

On Windows, `start_ui.cmd` is the convenience launcher for the same offline
entry. It does not request provider data unless an update option is supplied
explicitly. `QuantDesk.cmd` and the generated static-report pages are retained
as compatibility helpers; they are not the canonical application entry.

## Read-Only Checks

These commands inspect the local environment and current status without
starting a second production data chain:

```powershell
.\.venv\Scripts\python.exe scripts\qd.py env-doctor --json true
.\.venv\Scripts\python.exe scripts\qd.py status --json true
.\.venv\Scripts\python.exe scripts\qd.py fix-plan --json true
```

Do not treat `warning`, `blocked`, `missing`, `degraded`, or `insufficient` as
successful evidence. They are valid, visible states describing incomplete data
or acceptance.

## Current Boundaries

- Research Only
- Paper Account Only
- No Broker Route
- Live Execution Locked
- QMT Order Route Locked
- Not Investment Advice
- Not Position Advice

Network data refreshes require explicit authorization. Do not manually replay
or backfill natural scheduler, sector, shadow, or Stage 4 evidence.

For the current evidence state and remaining acceptance gates, read
`docs/CURRENT_STATE.md`. For the concise operator guide, read
`docs/daily_usable_entry.md`. The single task queue remains
`docs/internal/NEXT_TASKS_AND_WORKTREE_RULES.md`.
