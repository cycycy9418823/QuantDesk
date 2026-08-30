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
"$PYTHON_BIN" scripts/self_check.py
