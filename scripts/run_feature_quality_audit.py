from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_utf8_stdio()

from quantdesk.research.feature_quality_audit import audit_feature_quality, summarize_feature_quality  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk local feature quality audit.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols.")
    parser.add_argument("--target-date", default="", help="Target date in YYYY-MM-DD.")
    parser.add_argument("--input-dir", default="data/daily")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--network", default="false", help="Defaults to false. This command does not fetch data.")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    json_path = output_dir / "feature_quality_audit_latest.json"
    report_path = Path("reports") / "feature_quality_audit_report_latest.md"
    return audit_feature_quality(
        symbols=args.symbols,
        target_date=args.target_date,
        input_dir=args.input_dir,
        network=truthy(args.network),
        write=True,
        output_path=json_path,
        report_path=report_path,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    summary = summarize_feature_quality(payload)
    print("QuantDesk 特征质量审计")
    print(f"network: {payload.get('network')} (本命令不主动联网)")
    print(f"feature audit status: {payload.get('status')}")
    print(f"row_count: {payload.get('row_count')}")
    print(f"feature_count: {payload.get('feature_count')}")
    if payload.get("skipped_reason"):
        print(f"reason: {payload.get('skipped_reason')}")
    if payload.get("suggested_next_action"):
        print(f"next action: {payload.get('suggested_next_action')}")
    if payload.get("failed_reason"):
        print(f"failed_reason: {payload.get('failed_reason')}")
    print(f"high missing features: {summary.get('high_missing_feature_count')}")
    for feature in (summary.get("high_missing_features") or [])[:12]:
        print(f"missing_rate warning: {feature}")
    print("report path: reports/feature_quality_audit_report_latest.md")
    print(f"json path: {args.output_dir}/feature_quality_audit_latest.json")
    print("仅用于本地数据质量诊断，不构成投资建议。")
    return 0 if payload.get("ok_for_cli", payload.get("status") in {"passed", "warning"}) else 1


if __name__ == "__main__":
    raise SystemExit(main())
