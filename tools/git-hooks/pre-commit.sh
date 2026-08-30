#!/bin/sh
# QuantDesk clean-repo pre-commit guard (installed by install.cmd).
# Prefer the project venv python, fall back to PATH python/py.
ROOT="$(git rev-parse --show-toplevel)"
PY="$ROOT/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
  PY="$(command -v python || command -v py)"
fi
if [ -z "$PY" ]; then
  echo "[pre-commit] no python found; commit blocked. See tools/git-hooks/check_staged.py" >&2
  exit 2
fi
exec "$PY" "$ROOT/tools/git-hooks/check_staged.py"
