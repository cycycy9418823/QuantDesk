#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON:-python3}"
fi

echo "Project: $PROJECT_ROOT"
echo "Python: $($PYTHON_BIN -c 'import sys; print(sys.executable)')"

"$PYTHON_BIN" -m compileall -q quantdesk scripts tests
if command -v node >/dev/null 2>&1; then
  node --check app/static/js/daily_research.js
fi
"$PYTHON_BIN" scripts/qd.py maintenance test-tier-runner --tier tier0_fast_unit --limit 0 --timeout-seconds 300 --dry-run false --json true --summary true
"$PYTHON_BIN" -m pytest \
  tests/test_reported_quality_fixes.py \
  tests/test_rc14_mainline.py \
  tests/test_v21_strategy_core.py \
  tests/test_v241_research_validation.py \
  tests/test_v278_intraday_swing_execution.py \
  tests/test_v360_smart_stop_and_risk_alerts.py \
  tests/test_backtest_metrics.py \
  tests/test_data_audit.py \
  tests/test_walk_forward_hold_period_warning.py \
  -q
"$PYTHON_BIN" scripts/self_check.py

if command -v bash >/dev/null 2>&1; then
  bash ./export_project.sh
fi
