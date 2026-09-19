from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack  # noqa: E402
from quantdesk.agent.agent_report_audit import build_report_audit_record, write_report_audit  # noqa: E402
from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report  # noqa: E402

# v3.1.3 compatibility: generate_stub_observation_report is now reached via
# generate_llm_observation_report fallback when LLM is disabled or unavailable.
from quantdesk.llm_config import load_llm_config, safe_config_for_output  # noqa: E402
from quantdesk.shared import truthy, write_json  # noqa: E402
from quantdesk.strategy_observation_contract import (  # noqa: E402
    build_strategy_observation_context,
)

REPORT_LATEST_MD = PROJECT_ROOT / "results" / "strategy_observation_report_latest.md"
CONTEXT_LATEST_JSON = PROJECT_ROOT / "results" / "strategy_observation_context_latest.json"
LLM_RESULT_LATEST_JSON = PROJECT_ROOT / "results" / "strategy_observation_llm_result_latest.json"
AUDIT_LATEST_JSON = PROJECT_ROOT / "results" / "strategy_observation_report_audit_latest.json"
EVIDENCE_LATEST_JSON = PROJECT_ROOT / "results" / "strategy_observation_evidence_pack_latest.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a QuantDesk learning observation report.")
    parser.add_argument("--symbol", default="", help="A-share symbol, for example 600744.")
    parser.add_argument("--target-date", default="", help="Observation date in YYYY-MM-DD.")
    parser.add_argument("--output", default="", help="Optional output path for md/json report.")
    parser.add_argument("--output-dir", default="results", help="Directory for latest context/report/LLM result outputs.")
    parser.add_argument("--format", choices=["json", "md"], default="md")
    parser.add_argument("--network", default="false", help="Defaults to false. LLM calls require true and --llm true.")
    parser.add_argument("--llm", default="false", help="Enable optional LLM report generation. Defaults to false.")
    parser.add_argument("--llm-provider", choices=["offline_stub", "openai_compatible", "ollama"], default="")
    parser.add_argument("--llm-config", default="", help="Optional config/llm_config.json path.")
    parser.add_argument("--audit", default="true", help="Write report audit metadata. Defaults to true.")
    parser.add_argument("--strict-grounding", default="true", help="Guard unsupported facts strictly. Defaults to true.")
    parser.add_argument("--show-evidence", default="false", help="Write evidence pack JSON. Defaults to false.")
    return parser.parse_args(argv)


def _project_rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(path)


def run(args: argparse.Namespace) -> dict[str, Any]:
    network = truthy(args.network)
    llm_enabled = truthy(args.llm)
    output_dir = Path(args.output_dir or "results")
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    context_path = output_dir / "strategy_observation_context_latest.json"
    report_path = output_dir / "strategy_observation_report_latest.md"
    llm_result_path = output_dir / "strategy_observation_llm_result_latest.json"
    audit_path = output_dir / "strategy_observation_report_audit_latest.json"
    evidence_path = output_dir / "strategy_observation_evidence_pack_latest.json"

    context = build_strategy_observation_context(
        symbol=args.symbol,
        target_date=args.target_date,
        network=False,
        write=True,
        output_path=context_path,
    )
    config = load_llm_config(args.llm_config or None)
    config["llm_enabled"] = llm_enabled
    if args.llm_provider:
        config["default_provider"] = args.llm_provider

    strict_grounding = truthy(args.strict_grounding)
    audit_enabled = truthy(args.audit)
    show_evidence = truthy(args.show_evidence)
    report_payload = generate_llm_observation_report(
        context,
        config,
        network=network,
        strict_grounding=strict_grounding,
    )
    evidence_pack = build_agent_evidence_pack(context)
    output = Path(args.output) if args.output else (report_path if args.format == "md" else context_path)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)

    write_json(context_path, context)
    write_json(llm_result_path, {k: v for k, v in report_payload.items() if k != "raw_response"})
    if show_evidence:
        write_json(evidence_path, evidence_pack)
    audit_record: dict[str, Any] = {}
    if audit_enabled:
        audit_record = build_report_audit_record(context, evidence_pack, report_payload)
        write_report_audit(audit_record, audit_path)
    if args.format == "json":
        write_json(
            output,
            {
                "context": context,
                "report": report_payload,
                "evidence_pack": evidence_pack if show_evidence else {"written": False},
                "audit": audit_record,
                "llm_config": safe_config_for_output(config),
            },
        )
    else:
        output.write_text(str(report_payload.get("report") or ""), encoding="utf-8")

    return {
        "ok": bool(report_payload.get("ok")),
        "symbol": context.get("symbol"),
        "date": context.get("date"),
        "observation_only": context.get("observation_only"),
        "cannot_verify_intraday_trigger": context.get("cannot_verify_intraday_trigger"),
        "data_gaps": context.get("data_gaps") or [],
        "context_path": _project_rel(context_path),
        "report_path": _project_rel(output),
        "llm_result_path": _project_rel(llm_result_path),
        "network": network,
        "llm_enabled": llm_enabled,
        "provider": report_payload.get("provider"),
        "llm_used": bool(report_payload.get("llm_used")),
        "fallback_used": bool(report_payload.get("fallback_used")),
        "policy_guard_status": report_payload.get("policy_guard_status"),
        "grounding_guard_status": report_payload.get("grounding_guard_status"),
        "grounding_score": report_payload.get("grounding_score"),
        "unsupported_issue_count": report_payload.get("unsupported_issue_count"),
        "audit_enabled": audit_enabled,
        "audit_path": _project_rel(audit_path) if audit_enabled else "",
        "show_evidence": show_evidence,
        "evidence_path": _project_rel(evidence_path) if show_evidence else "",
        "strict_grounding": strict_grounding,
        "validation_summary": context.get("validation_summary") if isinstance(context.get("validation_summary"), dict) else {},
        "disclaimer": "仅用于本地学习研究，不构成投资建议。",
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = run(args)
    print("QuantDesk strategy observation report")
    print(f"symbol: {payload.get('symbol') or '-'}")
    print(f"date: {payload.get('date') or '-'}")
    print(f"observation_only: {payload.get('observation_only')}")
    print(f"cannot_verify_intraday_trigger: {payload.get('cannot_verify_intraday_trigger')}")
    print(f"data_gaps: {', '.join(payload.get('data_gaps') or []) or '-'}")
    print(f"llm_enabled: {payload.get('llm_enabled')}")
    print(f"provider: {payload.get('provider')}")
    print(f"llm_used: {payload.get('llm_used')}")
    print(f"fallback_used: {payload.get('fallback_used')}")
    print(f"policy_guard_status: {payload.get('policy_guard_status')}")
    print(f"grounding_guard_status: {payload.get('grounding_guard_status')}")
    print(f"grounding_score: {payload.get('grounding_score')}")
    print(f"unsupported_issue_count: {payload.get('unsupported_issue_count')}")
    validation_summary = payload.get("validation_summary") if isinstance(payload.get("validation_summary"), dict) else {}
    if validation_summary.get("status") == "failed":
        print("validation_warning: strategy validation risk is present; treat this as observation-only research.")
    print(f"context: {payload.get('context_path')}")
    print(f"report: {payload.get('report_path')}")
    print(f"llm_result: {payload.get('llm_result_path')}")
    print(f"audit: {payload.get('audit_path') or '-'}")
    print(f"evidence_pack: {payload.get('evidence_path') or '-'}")
    print("research_only=true; not_investment_advice=true; network=false means no LLM or external network call.")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
