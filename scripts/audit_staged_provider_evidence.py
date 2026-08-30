from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.staged_provider_evidence_audit import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_REPORT_PATH,
    write_staged_provider_evidence_audit_outputs,
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only staged-provider evidence audit for mootdx / baostock vendor "
            "staging. Never fetches network data, never imports, never writes "
            "canonical data. Emits a wait gate while a download is still running."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root to inspect; mainly for tests.")
    parser.add_argument(
        "--provider",
        default="all",
        choices=["all", "mootdx", "baostock"],
        help="Restrict the audit to one provider family (default: all detected staging packs).",
    )
    parser.add_argument("--staging-dir", default="", help="Explicit staging directory to audit instead of auto-detection.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=40,
        help="Max CSV files to deep-scan per provider (0 = all; bounded scans are labelled).",
    )
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    parser.add_argument(
        "--network",
        default="false",
        help="Accepted for qd.py consistency; this command never fetches network data.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if _truthy(args.network):
        print("staged-provider-evidence audit is read-only and never fetches network data; ignoring --network true.", file=sys.stderr)
    payload = write_staged_provider_evidence_audit_outputs(
        project_root=args.project_root,
        provider=args.provider,
        staging_dir=args.staging_dir or None,
        output_path=args.output,
        report_path=args.report,
        sample_limit=args.sample_limit,
    )
    if _truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print("QuantDesk staged-provider evidence audit")
    print(f"status: {payload.get('status')}")
    print(f"recommended_action: {payload.get('recommended_action')}")
    print(f"any_download_running: {payload.get('any_download_running')}")
    for prov in payload.get("providers") or []:
        if not isinstance(prov, dict):
            continue
        print(
            f"- {prov.get('provider')}/{prov.get('pack_name')}: "
            f"status={prov.get('status')} "
            f"recommended_action={prov.get('recommended_action')} "
            f"reason_codes={sorted(prov.get('reason_code_counts', {}).keys())}"
        )
    if _truthy(args.summary):
        for prov in payload.get("providers") or []:
            if not isinstance(prov, dict):
                continue
            for warning in prov.get("warnings") or []:
                print(f"  warn[{prov.get('provider')}]: {warning}")
    print(f"output: {payload.get('output_path')}")
    print(f"report: {payload.get('report_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
