from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def sample_context() -> dict:
    return {
        "schema_version": "3.2.1",
        "symbol": "600744",
        "date": "2026-05-19",
        "purpose": "learning_observation",
        "not_investment_advice": True,
        "data_health": {"status": "yellow", "summary_text": "日线可用，分钟线缺失。"},
        "provider_chain": ["tencent_daily"],
        "data_source_trace": [{"provider": "tencent_daily", "status": "skipped"}],
        "strategy_outputs": {"normalized_trigger_status": "cannot_verify_intraday_trigger"},
        "intraday_status": {"status": "missing_intraday", "missing_symbols": ["600744"]},
        "paper_portfolio_status": {"exists": False},
        "chanlun": {"fractals": {"exists": False}, "bi": {"exists": False}, "segments": {"exists": False}},
        "warnings": ["provider_skipped", "sample only"],
        "data_gaps": ["missing_intraday"],
        "secret_config": {"api_key": "secret-token-123"},
        "disclaimer": "仅用于本地学习研究，不构成投资建议。",
    }


def minute_pool_context() -> dict:
    context = sample_context()
    context["intraday_status"] = {
        "status": "available",
        "explanation": "verified minute pool available for covered symbols",
        "verified_completed_intraday_count": 200,
        "local_unverified_intraday_count": 0,
        "missing_intraday_count": 0,
        "verified_minute_pool": {
            "passed": True,
            "status": "passed",
            "available_count": 200,
            "missing_count": 0,
            "source_snapshot": "last_passed",
            "latest_quality_status": "partial",
            "latest_snapshot_path": "results/data_health/tushare_minute_quality_latest.json",
            "warnings": ["verified_pool_from_last_passed_snapshot"],
        },
    }
    context["data_gaps"] = []
    context["warnings"] = []
    return context


class AgentEvidenceGuardAndReportAuditTests(unittest.TestCase):
    def test_evidence_pack_extracts_core_facts(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack

        pack = build_agent_evidence_pack(sample_context())
        self.assertEqual(pack["schema_version"], "3.2.1")
        self.assertEqual(pack["symbol"], "600744")
        self.assertIn("2026-05-19", pack["allowed_dates"])
        self.assertIn("600744", pack["allowed_symbols"])
        self.assertIn("missing_intraday", pack["allowed_status_terms"])
        self.assertIn("tencent_daily", pack["allowed_provider_terms"])
        self.assertIn("missing_intraday", pack["data_gaps"])

    def test_evidence_pack_summarizes_verified_minute_pool_provenance(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack, build_evidence_summary_for_prompt

        pack = build_agent_evidence_pack(minute_pool_context())

        summary = pack["verified_minute_pool_summary"]
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["source_snapshot"], "last_passed")
        self.assertEqual(summary["latest_quality_status"], "partial")
        self.assertIn("verified_pool_from_last_passed_snapshot", summary["warnings"])
        prompt_summary = build_evidence_summary_for_prompt(minute_pool_context())
        self.assertIn("200", prompt_summary)
        self.assertNotIn("source_snapshot=", prompt_summary)
        self.assertNotIn("latest_quality_status=", prompt_summary)
        self.assertNotIn("latest_snapshot_path=", prompt_summary)
        self.assertNotIn("warnings=", prompt_summary)
        self.assertNotIn("last_passed", prompt_summary)

    def test_stub_report_mentions_minute_pool_provenance_when_last_passed_is_used(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_stub_observation_report

        result = generate_stub_observation_report(minute_pool_context())

        self.assertTrue(result["ok"], result)
        report = result["report"]
        self.assertIn("200", report)
        self.assertNotIn("source_snapshot=", report)
        self.assertNotIn("latest_quality_status=", report)
        self.assertNotIn("latest_snapshot_path=", report)
        self.assertNotIn("warnings=", report)
        self.assertNotIn("last_passed", report)

    def test_stub_report_humanizes_feature_and_statistical_diagnostics(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_stub_observation_report

        context = sample_context()
        context["feature_quality_summary"] = {
            "exists": True,
            "status": "warning",
            "high_missing_feature_count": 3,
        }
        context["statistical_validation_summary"] = {
            "status": "passed",
            "p_value": 0.18,
            "risk_notes": [],
        }

        result = generate_stub_observation_report(context)

        self.assertTrue(result["ok"], result)
        report = result["report"]
        self.assertIn("特征审计", report)
        self.assertIn("统计校验", report)
        self.assertIn("股票代码：600744", report)
        self.assertIn("观察日期：2026-05-19", report)
        self.assertIn("数据健康：", report)
        self.assertIn("策略观察：", report)
        self.assertNotIn("feature_quality:", report)
        self.assertNotIn("statistical_validation:", report)
        self.assertNotIn("data_health:", report)
        self.assertNotIn("data_health_summary:", report)
        self.assertNotIn("provider_chain:", report)
        self.assertNotIn("data_gaps:", report)
        self.assertNotIn("strategy_outputs:", report)
        self.assertNotIn("observation_only:", report)
        self.assertNotIn("status=", report)
        self.assertNotIn("p_value=", report)

    def test_evidence_pack_does_not_contain_api_key(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack

        serialized = json.dumps(build_agent_evidence_pack(sample_context()), ensure_ascii=False)
        self.assertNotIn("secret-token-123", serialized)
        self.assertNotIn("api_key", serialized.lower())

    def test_hallucination_guard_detects_unsupported_number(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import detect_unsupported_numbers

        issues = detect_unsupported_numbers("观察到价格 12.34。", build_agent_evidence_pack(sample_context()))
        self.assertTrue(any(item["type"] == "unsupported_number" for item in issues))

    def test_hallucination_guard_detects_unsupported_date(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import detect_unsupported_dates

        issues = detect_unsupported_dates("报告日期 2026-05-20。", build_agent_evidence_pack(sample_context()))
        self.assertTrue(any(item["type"] == "unsupported_date" for item in issues))

    def test_hallucination_guard_detects_unsupported_symbol(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import detect_unsupported_symbols

        issues = detect_unsupported_symbols("同时观察 002115。", build_agent_evidence_pack(sample_context()))
        self.assertTrue(any(item["type"] == "unsupported_symbol" for item in issues))

    def test_missing_intraday_contradiction_is_flagged(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import validate_report_grounding

        result = validate_report_grounding("分钟线可用，可以验证盘中触发。", build_agent_evidence_pack(sample_context()))
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["value"] == "intraday_available_claim" for item in result["issues"]))

    def test_provider_skipped_contradiction_is_flagged(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import validate_report_grounding

        result = validate_report_grounding("provider 成功，数据源成功。", build_agent_evidence_pack(sample_context()))
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["value"] == "provider_success_claim" for item in result["issues"]))

    def test_section_numbers_are_not_flagged(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import validate_report_grounding

        report = "1. 数据完整性\nsymbol: 600744\ndate: 2026-05-19"
        result = validate_report_grounding(report, build_agent_evidence_pack(sample_context()))
        self.assertEqual(result["issue_count"], 0, result)

    def test_version_number_is_not_flagged(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_hallucination_guard import validate_report_grounding

        result = validate_report_grounding("当前版本 v3.2.1。", build_agent_evidence_pack(sample_context()))
        self.assertEqual(result["issue_count"], 0, result)

    def test_safe_bridge_result_contains_hashes(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        result = generate_llm_observation_report(sample_context(), {"llm_enabled": False, "providers": {"offline_stub": {"enabled": True}}}, network=False, strict_grounding=True)
        self.assertIn("evidence_pack_hash", result)
        self.assertIn("context_hash", result)
        self.assertIn("prompt_hash", result)
        self.assertIn("report_hash", result)
        self.assertEqual(result["grounding_guard_status"], "passed")

    def test_observation_report_default_writes_audit_json(self) -> None:
        from scripts.run_strategy_observation_report import _parse_args, run

        with tempfile.TemporaryDirectory() as tmp:
            args = _parse_args(["--symbol", "600744", "--target-date", "2026-05-19", "--output-dir", tmp, "--network", "false", "--llm", "false"])
            payload = run(args)
            audit = Path(tmp) / "strategy_observation_report_audit_latest.json"
            self.assertTrue(audit.exists())
            audit_payload = json.loads(audit.read_text(encoding="utf-8"))
        self.assertTrue(payload["audit_enabled"])
        self.assertIn("grounding_score", audit_payload)

    def test_agent_report_audit_cli_exists(self) -> None:
        self.assertTrue((PROJECT_ROOT / "scripts" / "check_agent_report_audit.py").exists())

    def test_qd_help_contains_agent_report_audit(self) -> None:
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent-report-audit", result.stdout)

    def test_strict_grounding_true_changes_status(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "ollama",
            "providers": {"ollama": {"enabled": True, "type": "ollama", "model": "qwen"}, "offline_stub": {"enabled": True}},
        }
        with mock.patch("quantdesk.llm_providers.ollama_provider._http_post_json", return_value={"response": "价格 12.34。"}):
            result = generate_llm_observation_report(sample_context(), cfg, network=True, strict_grounding=True)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "guarded")
        self.assertGreater(result["unsupported_issue_count"], 0)

    def test_strict_grounding_false_only_warns(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "ollama",
            "providers": {"ollama": {"enabled": True, "type": "ollama", "model": "qwen"}, "offline_stub": {"enabled": True}},
        }
        with mock.patch("quantdesk.llm_providers.ollama_provider._http_post_json", return_value={"response": "价格 12.34。"}):
            result = generate_llm_observation_report(sample_context(), cfg, network=True, strict_grounding=False)
        self.assertTrue(result["ok"], result)
        self.assertGreater(result["unsupported_issue_count"], 0)

    def test_clean_zip_rules_exclude_audit_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("results", export_text)
        self.assertNotIn("strategy_observation_report_audit_latest.json", export_text)

    def test_clean_zip_rules_exclude_evidence_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("results", export_text)
        self.assertNotIn("strategy_observation_evidence_pack_latest.json", export_text)

    def test_docs_and_qd_are_wired(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        docs = (PROJECT_ROOT / "docs" / "agent_evidence_guard.md").read_text(encoding="utf-8")
        self.assertIn("Agent Evidence Guard And Report Quality Audit", readme)
        self.assertIn("agent-report-audit", readme + docs)


if __name__ == "__main__":
    unittest.main()


