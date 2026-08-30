#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo ".venv not found. Run ./setup_linux.sh first."
  exit 1
fi

echo "Project: $PROJECT_ROOT"
echo "Python: $PYTHON_BIN"
echo "Starting QuantDesk through the unified 8000 launcher..."
exec "$PYTHON_BIN" scripts/launcher.py \
  --port 8000 \
  --update-on-start \
  --update-max-symbols 80 \
  --update-timeout 240 \
  --candidate-max-scan-symbols 300 \
  --candidate-max-candidates 18
