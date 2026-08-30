from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack  # noqa: E402
from quantdesk.agent.agent_report_audit import (  # noqa: E402
    audit_report_quality,
    build_report_audit_record,
    summarize_report_audit,
    write_report_audit,
)

DEFAULT_REPORT = PROJECT_ROOT / "results" / "strategy_observation_report_latest.md"
DEFAULT_CONTEXT = PROJECT_ROOT / "results" / "strategy_observation_context_latest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "results" / "strategy_observation_report_audit_latest.json"
CHECK_NAME = "check_agent_report_audit"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a QuantDesk observation report against its local context.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="Markdown report path.")
    parser.add_argument("--context", default=str(DEFAULT_CONTEXT), help="Observation context JSON path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Audit JSON output path.")
    return parser.parse_args(argv)


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else PROJECT_ROOT / value


def _read_context(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"warnings": [f"context_missing:{path}"], "data_gaps": ["context_file_missing"]}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"warnings": [f"context_read_failed:{exc}"], "data_gaps": ["context_read_failed"]}
    return payload if isinstance(payload, dict) else {"warnings": ["context_not_dict"], "data_gaps": ["context_not_dict"]}


def run(args: argparse.Namespace) -> dict[str, Any]:
    report_path = _resolve(args.report)
    context_path = _resolve(args.context)
    output_path = _resolve(args.output)
    report_text = report_path.read_text(encoding="utf-8", errors="ignore") if report_path.exists() else ""
    context = _read_context(context_path)
    evidence_pack = build_agent_evidence_pack(context)
    quality = audit_report_quality(report_text, context, evidence_pack)
    llm_result = {
        "provider": "existing_report",
        "llm_used": False,
        "fallback_used": True,
        "policy_guard_status": quality.get("policy_guard_status"),
        "grounding_guard_status": quality.get("grounding_guard_status"),
        "grounding_score": quality.get("grounding_score"),
        "unsupported_issue_count": quality.get("unsupported_issue_count"),
        "unsupported_issues": quality.get("unsupported_issues"),
        "context_hash": quality.get("context_hash"),
        "evidence_pack_hash": quality.get("evidence_pack_hash"),
        "report_hash": quality.get("report_hash"),
        "report": report_text,
    }
    record = build_report_audit_record(context, evidence_pack, llm_result)
    write_report_audit(record, output_path)
    return {
        "ok": bool(quality.get("ok")),
        "report": str(report_path),
        "context": str(context_path),
        "output": str(output_path),
        "record": record,
        "summary": summarize_report_audit(record),
    }


def main(argv: list[str] | None = None) -> int:
    payload = run(_parse_args(argv))
    record = payload["record"]
    print("QuantDesk Agent Report Audit")
    print(f"report: {payload.get('report')}")
    print(f"context: {payload.get('context')}")
    print(f"policy_guard_status: {record.get('policy_guard_status')}")
    print(f"grounding_guard_status: {record.get('grounding_guard_status')}")
    print(f"grounding_score: {record.get('grounding_score')}")
    print(f"unsupported_issue_count: {record.get('unsupported_issue_count')}")
    print(f"audit: {payload.get('output')}")
    print(payload.get("summary"))
    print("本审计仅读取本地 report/context，不联网、不调用 LLM。")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
