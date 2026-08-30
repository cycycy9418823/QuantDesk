from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_store.health import build_data_layer_health
from quantdesk.version import APP_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/quantdesk.db")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--network", default="false")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--target-date", default="")
    parser.add_argument("--scope", default="")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    payload = build_data_layer_health(vars(args))
    payload.setdefault("app_version", APP_VERSION)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
