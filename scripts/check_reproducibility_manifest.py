from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.reproducibility_manifest import (  # noqa: E402
    compare_manifests,
    validate_reproducibility_manifest,
)
from quantdesk.shared import read_json, truthy, write_json  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check a QuantDesk reproducibility manifest.")
    parser.add_argument("--manifest", required=True, help="Path to daily_retention_manifest.json.")
    parser.add_argument("--compare-other", default="", help="Optional second manifest path.")
    parser.add_argument("--strict", default="true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _load(path: str) -> dict:
    payload = read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest = _load(args.manifest)
    validation = validate_reproducibility_manifest(manifest)
    comparison = {}
    if args.compare_other:
        comparison = compare_manifests(manifest, _load(args.compare_other))
    payload = {
        "ok": bool(validation.get("ok")) and (not comparison or bool(comparison.get("ok")) or not truthy(args.strict)),
        "manifest": args.manifest,
        "validation": validation,
        "comparison": comparison,
        "network": False,
        "llm_used": False,
        "disclaimer": "仅用于本地复现检查，不联网、不调用 LLM、不构成投资建议。",
    }
    out_path = PROJECT_ROOT / "results" / "daily_retention" / "manifest_check_latest.json"
    write_json(out_path, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("QuantDesk 复现 manifest 校验")
        print(f"manifest: {args.manifest}")
        print(f"status: {validation.get('status')}")
        print(f"missing_fields: {', '.join(validation.get('missing_required_fields') or []) or '-'}")
        print(f"hash_mismatches: {len(validation.get('hash_mismatches') or [])}")
        if comparison:
            print(f"compare_status: {comparison.get('status')}")
            print(f"differences: {comparison.get('difference_count')}")
        for warning in validation.get("warnings") or []:
            print(f"warning: {warning}")
        print(f"output: {out_path.relative_to(PROJECT_ROOT).as_posix()}")
        print("不联网、不调用 LLM；manifest 不保存 API Key 或真实账户信息。")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

