from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_store.quality_repair import build_data_quality_report
from quantdesk.version import APP_VERSION


def parse_bool(value) -> bool:
    return str(value or "false").lower() in {"1", "true", "yes", "y"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="")
    parser.add_argument("--adjustment", default="forward")
    parser.add_argument("--db-path", default="data/quantdesk.db")
    parser.add_argument("--repair", nargs="?", const="true", default="false")
    parser.add_argument("--network", default="false")
    parser.add_argument("--json", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    args = parser.parse_args(argv)
    cfg = vars(args)
    cfg["repair"] = parse_bool(cfg["repair"])
    cfg["network"] = parse_bool(cfg["network"])
    payload = build_data_quality_report(cfg)
    payload.setdefault("app_version", APP_VERSION)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
