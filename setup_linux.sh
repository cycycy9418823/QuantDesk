#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="${QUANTDESK_VENV_DIR:-$PROJECT_ROOT/.venv}"
DEPENDENCY_LOCK="$PROJECT_ROOT/requirements-cloud-aarch64.lock"
echo "Project: $PROJECT_ROOT"
echo "Python: $($PYTHON_BIN -c 'import sys; print(sys.executable)')"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
if [ -f "$DEPENDENCY_LOCK" ]; then
  "$VENV_DIR/bin/python" -m pip install -r "$DEPENDENCY_LOCK"
else
  "$VENV_DIR/bin/python" -m pip install -r requirements.txt
  "$VENV_DIR/bin/python" -m pip install -r requirements-data.txt
fi

echo "Setup complete. Start UI with ./start_ui.sh"
