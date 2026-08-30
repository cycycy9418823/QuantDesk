from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_data_retention import (  # noqa: E402
    build_retention_plan,
    collect_daily_retention_snapshot,
    summarize_retention_result,
    write_retention_manifest,
    write_retention_summary,
)
from quantdesk.shared import truthy  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run QuantDesk daily data retention and manifest workflow.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Empty means low-price included=true sample only.")
    parser.add_argument("--target-date", default="", help="Target date in YYYY-MM-DD.")
    parser.add_argument("--network", default="false", help="Defaults to false. true must be explicit.")
    parser.add_argument("--output-dir", default="results/daily_retention", help="Daily retention output directory.")
    parser.add_argument("--include-provider-smoke", default="true")
    parser.add_argument("--include-strategy-validation", default="true")
    parser.add_argument("--include-observation-report", default="true")
    parser.add_argument("--llm", default="false", help="Defaults to false.")
    parser.add_argument("--max-symbols", type=int, default=3)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = build_retention_plan(
        symbols=args.symbols,
        target_date=args.target_date,
        network=truthy(args.network),
        output_dir=args.output_dir,
        include_provider_smoke=truthy(args.include_provider_smoke),
        include_strategy_validation=truthy(args.include_strategy_validation),
        include_observation_report=truthy(args.include_observation_report),
        llm=truthy(args.llm),
        max_symbols=args.max_symbols,
        command_line_args=sys.argv,
    )
    snapshot = collect_daily_retention_snapshot(plan)
    manifest_path = Path(plan["manifest_path"])
    summary_path = Path(plan["summary_path"])
    write_retention_manifest(snapshot, manifest_path)
    write_retention_summary(snapshot, summary_path)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    provider_summary = payload.get("provider_smoke_summary") if isinstance(payload.get("provider_smoke_summary"), dict) else {}
    validation_summary = payload.get("strategy_validation_summary") if isinstance(payload.get("strategy_validation_summary"), dict) else {}
    feature_summary = payload.get("feature_quality_summary") if isinstance(payload.get("feature_quality_summary"), dict) else {}
    stats_summary = payload.get("statistical_validation_summary") if isinstance(payload.get("statistical_validation_summary"), dict) else {}
    data_health_summary = payload.get("data_health_summary") if isinstance(payload.get("data_health_summary"), dict) else {}
    parameter_summary = payload.get("parameter_sensitivity_summary") if isinstance(payload.get("parameter_sensitivity_summary"), dict) else {}
    print("QuantDesk 每日数据留存")
    print(f"target_date: {payload.get('target_date') or '-'}")
    print(f"symbols: {', '.join(payload.get('symbols') or []) or '-'}")
    print(f"network: {payload.get('network')}")
    print(f"llm_enabled: {payload.get('llm_enabled')}")
    print(f"calendar_quality: {payload.get('calendar_quality') or '-'}")
    print(
        "provider status: "
        f"ok={provider_summary.get('ok', 0)}, skipped={provider_summary.get('skipped', 0)}, failed={provider_summary.get('failed', 0)}"
    )
    print(
        "strategy validation: "
        f"lookahead={validation_summary.get('lookahead_status') or '-'}, "
        f"execution={validation_summary.get('execution_assumption_status') or '-'}"
    )
    print(
        "feature quality: "
        f"status={feature_summary.get('status') or 'missing'}, "
        f"high_missing={feature_summary.get('high_missing_feature_count', 0)}"
    )
    print(
        "statistical validation: "
        f"status={stats_summary.get('status') or 'missing'}, "
        f"p_value={stats_summary.get('p_value')}"
    )
    print(
        "data health: "
        f"status={data_health_summary.get('status') or 'missing'}, "
        f"freshness={data_health_summary.get('freshness_status') or 'missing'}, "
        f"intraday_coverage={data_health_summary.get('intraday_coverage_rate')}"
    )
    print(
        "parameter sensitivity: "
        f"status={parameter_summary.get('status') or 'missing'}, "
        f"robustness_score={parameter_summary.get('robustness_score')}, "
        f"combination_count={parameter_summary.get('combination_count', 0)}"
    )
    for warning in payload.get("warnings") or []:
        print(f"warning: {warning}")
    print(f"manifest path: {payload.get('manifest_path')}")
    print(f"summary path: {payload.get('summary_path')}")
    print(summarize_retention_result(payload))
    print("默认离线，不接券商、不真实下单；manifest 只记录版本、输入、输出和 hash，不保存 API Key，不构成投资建议。")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
