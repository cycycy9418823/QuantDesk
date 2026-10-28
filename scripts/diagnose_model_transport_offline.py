"""Read an existing AI plan/request receipt; never call a model or edit it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_trader.llm_transport import describe_model_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Existing plan or transport receipt JSON")
    args = parser.parse_args(argv)
    payload = json.loads(args.path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        parser.error("receipt must be an object")
    record = payload.get("llm") if isinstance(payload.get("llm"), dict) else payload
    print(json.dumps({"source": str(args.path), "diagnosis": describe_model_request(record),
        "network_calls": 0, "model_calls": 0, "disclaimer": "纸面 · 未验证"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
