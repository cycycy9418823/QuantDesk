from __future__ import annotations

import json
import os
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
        "schema_version": "3.2.0",
        "symbol": "600744",
        "date": "2026-05-19",
        "purpose": "learning_observation",
        "not_investment_advice": True,
        "data_health": {"status": "yellow", "summary_text": "日线可用，分钟线缺失。"},
        "strategy_outputs": {"normalized_trigger_status": "cannot_verify_intraday_trigger"},
        "intraday_status": {"status": "cannot_verify_intraday_trigger", "missing_symbols": ["600744"]},
        "paper_portfolio_status": {"exists": False},
        "chanlun": {"fractals": {"exists": False}, "bi": {"exists": False}, "segments": {"exists": False}},
        "warnings": ["sample only"],
        "data_gaps": ["intraday"],
        "allowed_agent_actions": ["explain", "summarize", "risk_notes"],
        "forbidden_agent_actions": ["buy_recommendation", "sell_recommendation", "real_order"],
        "disclaimer": "仅用于本地学习研究，不构成投资建议。",
    }


class OptionalLlmObservationAgentTests(unittest.TestCase):
    def test_default_config_is_disabled_offline_stub(self) -> None:
        from quantdesk.llm_config import is_llm_enabled, load_llm_config

        cfg = load_llm_config(PROJECT_ROOT / "config" / "__missing_llm_config__.json")
        self.assertFalse(is_llm_enabled(cfg))
        self.assertEqual(cfg["default_provider"], "offline_stub")
        self.assertIn("offline_stub", cfg["providers"])

    def test_missing_config_does_not_error(self) -> None:
        from quantdesk.llm_config import validate_llm_config

        payload = validate_llm_config({"llm_enabled": False, "default_provider": "offline_stub", "providers": {"offline_stub": {"enabled": True}}})
        self.assertTrue(payload["ok"], payload)

    def test_network_false_openai_is_not_called(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "openai_compatible",
            "providers": {
                "openai_compatible": {"enabled": True, "type": "openai_compatible", "base_url": "http://example.invalid/v1", "model": "x", "api_key": "secret"},
                "offline_stub": {"enabled": True, "type": "offline_stub"},
            },
        }
        with mock.patch("quantdesk.llm_providers.openai_compatible_provider._http_post_json", side_effect=AssertionError("network called")):
            result = generate_llm_observation_report(sample_context(), cfg, network=False)
        self.assertFalse(result["llm_used"])
        self.assertTrue(result["fallback_used"])
        self.assertIn("network=false", result.get("skipped_reason", ""))

    def test_network_false_ollama_is_not_called(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "ollama",
            "providers": {
                "ollama": {"enabled": True, "type": "ollama", "base_url": "http://localhost:11434", "model": "qwen"},
                "offline_stub": {"enabled": True, "type": "offline_stub"},
            },
        }
        with mock.patch("quantdesk.llm_providers.ollama_provider._http_post_json", side_effect=AssertionError("network called")):
            result = generate_llm_observation_report(sample_context(), cfg, network=False)
        self.assertFalse(result["llm_used"])
        self.assertTrue(result["fallback_used"])

    def test_openai_missing_api_key_is_nonfatal(self) -> None:
        from quantdesk.llm_providers.openai_compatible_provider import provider_capability

        with mock.patch.dict(os.environ, {}, clear=True):
            cap = provider_capability({"enabled": True, "base_url": "http://example/v1", "model": "x", "api_key_env": "QUANTDESK_TEST_KEY"})
        self.assertEqual(cap["status"], "missing_api_key")
        self.assertFalse(cap["api_key_present"])

    def test_llm_true_network_false_falls_back_to_stub(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        result = generate_llm_observation_report(
            sample_context(),
            {"llm_enabled": True, "default_provider": "offline_stub", "providers": {"offline_stub": {"enabled": True}}},
            network=False,
        )
        self.assertFalse(result["llm_used"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["provider"], "offline_stub")

    def test_mock_openai_success_returns_policy_checked_report(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "openai_compatible",
            "providers": {
                "openai_compatible": {"enabled": True, "type": "openai_compatible", "base_url": "http://example/v1", "model": "x", "api_key_env": "QUANTDESK_TEST_KEY"},
                "offline_stub": {"enabled": True, "type": "offline_stub"},
            },
        }
        with mock.patch.dict(os.environ, {"QUANTDESK_TEST_KEY": "secret-token-123"}, clear=False):
            with mock.patch(
                "quantdesk.llm_providers.openai_compatible_provider._http_post_json",
                return_value={"choices": [{"message": {"content": "学习复盘报告：不构成投资建议。"}}]},
            ):
                result = generate_llm_observation_report(sample_context(), cfg, network=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["llm_used"])
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["provider"], "openai_compatible")
        self.assertEqual(result["policy_guard_status"], "passed")

    def test_mock_ollama_success_returns_report(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "ollama",
            "providers": {
                "ollama": {"enabled": True, "type": "ollama", "base_url": "http://localhost:11434", "model": "qwen"},
                "offline_stub": {"enabled": True, "type": "offline_stub"},
            },
        }
        with mock.patch("quantdesk.llm_providers.ollama_provider._http_post_json", return_value={"response": "本地学习复盘，不构成投资建议。"}):
            result = generate_llm_observation_report(sample_context(), cfg, network=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provider"], "ollama")
        self.assertTrue(result["llm_used"])

    def test_forbidden_terms_are_sanitized(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "ollama",
            "providers": {"ollama": {"enabled": True, "type": "ollama", "model": "qwen"}, "offline_stub": {"enabled": True}},
        }
        with mock.patch("quantdesk.llm_providers.ollama_provider._http_post_json", return_value={"response": "推荐买入，目标价 10，stop loss 8。"}):
            result = generate_llm_observation_report(sample_context(), cfg, network=True)
        self.assertTrue(result["ok"], result)
        self.assertIn(result["policy_guard_status"], {"sanitized", "passed"})
        report = result["report"]
        for forbidden in ("推荐买入", "目标价", "stop loss"):
            self.assertNotIn(forbidden, report)

    def test_cli_supports_llm_flags(self) -> None:
        text = (PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py").read_text(encoding="utf-8")
        self.assertIn("--llm", text)
        self.assertIn("--llm-provider", text)
        self.assertIn("strategy_observation_llm_result_latest.json", text)

    def test_observation_report_cli_llm_true_network_false(self) -> None:
        script = PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py"
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--symbol",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--output-dir",
                    tmp,
                    "--network",
                    "false",
                    "--llm",
                    "true",
                    "--llm-provider",
                    "openai_compatible",
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads((Path(tmp) / "strategy_observation_llm_result_latest.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["llm_used"])
        self.assertTrue(payload["fallback_used"])
        self.assertIn("network=false", payload.get("skipped_reason", ""))

    def test_config_example_exists_and_default_disabled(self) -> None:
        example = PROJECT_ROOT / "config" / "llm_config.example.json"
        payload = json.loads(example.read_text(encoding="utf-8"))
        self.assertFalse(payload["llm_enabled"])
        self.assertEqual(payload["default_provider"], "offline_stub")
        self.assertIn("openai_compatible", payload["providers"])

    def test_docs_describe_optional_llm_safety(self) -> None:
        docs = (PROJECT_ROOT / "docs" / "llm_observation_agent.md").read_text(encoding="utf-8")
        self.assertIn("default provider: `offline_stub`", docs)
        self.assertIn("不构成投资建议", docs)
        self.assertIn("agent_policy_guard", docs)

    def test_qd_help_contains_llm_smoke(self) -> None:
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "qd.py"), "help"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("llm-smoke", result.stdout)

    def test_llm_smoke_network_false_does_not_call_network(self) -> None:
        from scripts.check_llm_observation_agent import _parse_args, run

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "llm_smoke.json"
            args = _parse_args(["--network", "false", "--output", str(out)])
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
                payload = run(args)
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["network"])
        self.assertTrue(payload["provider_capabilities"])

    def test_clean_zip_excludes_new_llm_runtime_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("results", export_text)
        self.assertIn("llm_config.json", export_text)
        self.assertIn("config/llm_config.json", gitignore)

    def test_api_key_not_in_results_or_report(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_llm_observation_report

        cfg = {
            "llm_enabled": True,
            "default_provider": "openai_compatible",
            "providers": {
                "openai_compatible": {"enabled": True, "type": "openai_compatible", "base_url": "http://example/v1", "model": "x", "api_key_env": "QUANTDESK_TEST_KEY"},
                "offline_stub": {"enabled": True, "type": "offline_stub"},
            },
        }
        with mock.patch.dict(os.environ, {"QUANTDESK_TEST_KEY": "secret-token-123"}, clear=False):
            with mock.patch(
                "quantdesk.llm_providers.openai_compatible_provider._http_post_json",
                return_value={"choices": [{"message": {"content": "学习复盘报告：不构成投资建议。"}}]},
            ):
                result = generate_llm_observation_report(sample_context(), cfg, network=True)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-token-123", serialized)


if __name__ == "__main__":
    unittest.main()


