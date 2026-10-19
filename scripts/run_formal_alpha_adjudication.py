"""Compatibility notice for the retired formal-alpha workflow; never publishes."""
from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source")
    parser.add_argument("--preregistration")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = {"ok": False, "status": "retired", "task_type": "formal_alpha_retired_notice",
               "publication_status": "disabled", "paths": {}, "network": False,
               "runtime_write": False, "research_only": True,
               "message": "Formal alpha adjudication is retired. Existing artifacts are historical only; use factor-eval or stats-validate for unvalidated research diagnostics."}
    print(json.dumps(payload, ensure_ascii=True) if args.json else payload["message"])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
