"""Tests for v2.6.3 codebase-slimming extractions.

These tests cover the pure helpers introduced in
``quantdesk/self_check_core.py`` plus the shared ``tests/helpers.py``
module.  The ``quantdesk/server_payloads.py`` coercion, parsing and
request-normalizer tests moved to
``tests/test_legacy_8765_codebase_slimming_payloads.py`` with the R1
port-8765 closure.  They intentionally do not
exercise CLI output, server bind ports, or filesystem report
generation; the goal is to lock down the contracts that the new
modules promise.

The tests must keep working even when the project is bumped to a
later v2.6.x patch release because behaviour, not exact version
strings, is asserted.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import quantdesk.version as version_info  # noqa: E402
import scripts.quant_api_server as qas  # noqa: E402
import scripts.self_check as self_check_script  # noqa: E402
from quantdesk import self_check_core  # noqa: E402
from tests import helpers as test_helpers  # noqa: E402


class VersionSourceTests(unittest.TestCase):
    """The new release stays centralized on quantdesk/version.py."""

    def test_version_is_v263_codebase_slimming(self) -> None:
        # Tests verify continuity of the wiring rather than freezing a
        # specific version string.
        self.assertTrue(version_info.APP_VERSION, "APP_VERSION must be non-empty")
        self.assertTrue(version_info.API_SCHEMA_VERSION, "API_SCHEMA_VERSION must be non-empty")
        self.assertTrue(version_info.RELEASE_CODENAME)
        self.assertTrue(version_info.RELEASE_TITLE)

    def test_server_reuses_centralized_version(self) -> None:
        self.assertEqual(qas.APP_VERSION, version_info.APP_VERSION)

    def test_release_title_present_in_docs(self) -> None:
        for path in (
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "ROADMAP.md",
            PROJECT_ROOT / "docs" / "api_payload_schema.md",
        ):
            text = path.read_text(encoding="utf-8")
            self.assertIn(version_info.RELEASE_TITLE, text, msg=str(path))


class SelfCheckCoreTextScannerTests(unittest.TestCase):
    """Pure text-scanner helpers in quantdesk/self_check_core.py."""

    def test_strip_safe_negative_contexts_removes_disclaimers(self) -> None:
        cleaned = self_check_core.strip_safe_negative_contexts(
            "提示：本工具不构成投资建议，也不预测价格走势。",
        )
        self.assertNotIn("不构成投资建议", cleaned)
        self.assertNotIn("不预测价格走势", cleaned)

    def test_line_is_rule_definition_recognises_rule_tokens(self) -> None:
        self.assertTrue(
            self_check_core.line_is_rule_definition(
                'STRICT_FORBIDDEN_OUTPUT_PHRASES = ["接下来预测"]',
            )
        )
        self.assertFalse(self_check_core.line_is_rule_definition("普通的中文说明文本。"))

    def test_has_allowed_context_for_virtual_buy(self) -> None:
        self.assertTrue(
            self_check_core.has_allowed_context("虚拟买入仅用于复盘", "买入"),
        )
        self.assertFalse(
            self_check_core.has_allowed_context("现在可以买入了", "买入"),
        )

    def test_scan_text_for_dangerous_phrases_flags_strict_phrase(self) -> None:
        text = "标题\n建议买入这只股票\n备注"
        hits = self_check_core.scan_text_for_dangerous_phrases(text)
        self.assertEqual(hits, [(2, "建议买入")])

    def test_scan_text_for_dangerous_phrases_skips_safe_disclaimer(self) -> None:
        text = "提示：本工具不构成投资建议，不会真实下单。"
        self.assertEqual(self_check_core.scan_text_for_dangerous_phrases(text), [])

    def test_scan_text_skips_rule_definition_line(self) -> None:
        # The rule definition line must not flag itself.
        text = 'STRICT_FORBIDDEN_OUTPUT_PHRASES = ["建议买入", "推荐卖出"]'
        self.assertEqual(self_check_core.scan_text_for_dangerous_phrases(text), [])


class SelfCheckCoreVersionAndPathTests(unittest.TestCase):
    """Version-centralization and hardcoded-path helpers."""

    def test_version_centralization_issues_returns_empty_for_good_inputs(self) -> None:
        version_text = (
            "APP_VERSION = '2.6.3'\n"
            "API_SCHEMA_VERSION = '2.6.3'\n"
            "RELEASE_CODENAME = 'codebase-slimming'\n"
            "RELEASE_TITLE = 'v2.6.3 Codebase Slimming'\n"
        )
        server_text = (
            "from quantdesk.version import APP_VERSION as VERSION_APP_VERSION\n"
            "APP_VERSION = VERSION_APP_VERSION\n"
            "API_SCHEMA_VERSION = VERSION_API_SCHEMA_VERSION\n"
        )
        config_text = 'app_version: "2.6.3"\nschema_version: "2.6.3"\n'
        docs = readme = roadmap = "## v2.6.3 Codebase Slimming\n"
        issues = self_check_core.version_centralization_issues(
            version_text=version_text,
            server_text=server_text,
            config_text=config_text,
            readme_text=readme,
            roadmap_text=roadmap,
            docs_text=docs,
            app_version="2.6.3",
            api_schema_version="2.6.3",
            release_title="v2.6.3 Codebase Slimming",
        )
        self.assertEqual(issues, [])

    def test_version_centralization_issues_flags_missing_release_title(self) -> None:
        issues = self_check_core.version_centralization_issues(
            version_text="APP_VERSION='1' API_SCHEMA_VERSION='1' RELEASE_CODENAME='x' RELEASE_TITLE='t'",
            server_text="from quantdesk.version import APP_VERSION as VERSION_APP_VERSION\n"
            "APP_VERSION = VERSION_APP_VERSION\n"
            "API_SCHEMA_VERSION = VERSION_API_SCHEMA_VERSION\n",
            config_text="1",
            readme_text="empty",
            roadmap_text="empty",
            docs_text="empty",
            app_version="1",
            api_schema_version="1",
            release_title="EXPECTED_TITLE",
        )
        joined = " | ".join(issues)
        self.assertIn("README.md missing EXPECTED_TITLE", joined)
        self.assertIn("ROADMAP.md missing EXPECTED_TITLE", joined)
        self.assertIn("docs/api_payload_schema.md missing EXPECTED_TITLE", joined)

    def test_find_hardcoded_project_paths_skips_ignored_dirs(self) -> None:
        # The repo itself must not contain the developer-machine path.
        offenders = self_check_core.find_hardcoded_project_paths(
            PROJECT_ROOT,
            ["E:" + "\\" + "codex" + "\\" + "quant", "E:" + "/" + "codex" + "/" + "quant"],
        )
        self.assertEqual(offenders, [])

    def test_find_hardcoded_project_paths_prunes_ignored_dirs(self) -> None:
        tmp_parent = PROJECT_ROOT / ".quantdesk_test_tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_parent) as tmp:
            root = Path(tmp)
            (root / "quantdesk").mkdir(parents=True)
            (root / "quantdesk" / "ok.py").write_text("PROJECT_ROOT = Path(__file__).parent\n", encoding="utf-8")
            ignored = root / "results" / "nested"
            ignored.mkdir(parents=True)
            for idx in range(50):
                (ignored / f"runtime_{idx}.py").write_text("E:\\codex\\quant\n", encoding="utf-8")

            offenders = self_check_core.find_hardcoded_project_paths(root, ["E:\\codex\\quant"])

        self.assertEqual(offenders, [])


class SelfCheckCoreCleanZipTests(unittest.TestCase):
    """Clean-zip auditing helpers."""

    def test_audit_clean_zip_names_finds_runtime_pollution(self) -> None:
        report = self_check_core.audit_clean_zip_names(
            names=[
                "README.md",
                "data/daily/600000.csv",
                "logs/app.log",
                "docs/api_payload_schema.md",
                "scripts/run.exe",
            ],
            required=["README.md", "docs/api_payload_schema.md"],
        )
        self.assertIn("data/daily/600000.csv", report["bad"])
        self.assertIn("logs/app.log", report["bad"])
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["backslashes"], [])

    def test_audit_clean_zip_names_reports_missing_required(self) -> None:
        report = self_check_core.audit_clean_zip_names(
            names=["README.md"],
            required=["README.md", "docs/api_payload_schema.md"],
        )
        self.assertEqual(report["bad"], [])
        self.assertEqual(report["missing"], ["docs/api_payload_schema.md"])

    def test_audit_clean_zip_names_flags_backslashes(self) -> None:
        report = self_check_core.audit_clean_zip_names(
            names=["docs\\api_payload_schema.md"],
            required=["docs/api_payload_schema.md"],
        )
        self.assertEqual(report["backslashes"], ["docs\\api_payload_schema.md"])

    def test_list_zip_entries_round_trip(self) -> None:
        import io

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("README.md", "hello")
            archive.writestr("docs/api_payload_schema.md", "schema")
        buf.seek(0)
        # write to a temp on-disk path so list_zip_entries can open it
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(buf.read())
            tmp_path = Path(tmp.name)
        try:
            entries = self_check_core.list_zip_entries(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        self.assertEqual(
            sorted(entries),
            ["README.md", "docs/api_payload_schema.md"],
        )


class SelfCheckCoreMojibakeTests(unittest.TestCase):
    """Visible-text mojibake detector."""

    def test_contains_mojibake_for_question_marks(self) -> None:
        self.assertTrue(self_check_core.contains_mojibake("foo????bar"))

    def test_contains_mojibake_false_for_clean_text(self) -> None:
        self.assertFalse(self_check_core.contains_mojibake("欢迎使用本地工具。"))


class SelfCheckShimCompatibilityTests(unittest.TestCase):
    """The CLI script keeps re-exporting the rule constants and helpers."""

    def test_module_level_constants_match_core_rules(self) -> None:
        self.assertEqual(
            list(self_check_script.STRICT_FORBIDDEN_OUTPUT_PHRASES),
            list(self_check_core.STRICT_FORBIDDEN_OUTPUT_PHRASES),
        )
        self.assertEqual(
            list(self_check_script.CONTEXT_SENSITIVE_TERMS),
            list(self_check_core.CONTEXT_SENSITIVE_TERMS),
        )
        self.assertEqual(
            list(self_check_script.ALLOWED_CONTEXT_MARKERS),
            list(self_check_core.ALLOWED_CONTEXT_MARKERS),
        )

    def test_private_helpers_are_thin_shims(self) -> None:
        self.assertEqual(
            self_check_script._strip_safe_negative_contexts("不构成投资建议"),
            "",
        )
        self.assertTrue(
            self_check_script._line_is_rule_definition(
                'STRICT_FORBIDDEN_OUTPUT_PHRASES = ["建议买入"]'
            )
        )
        self.assertFalse(
            self_check_script._has_allowed_context("现在可以买入了", "买入"),
        )








class TestHelpersTests(unittest.TestCase):
    """The shared tests/helpers.py module."""

    def test_project_root_points_to_repo_root(self) -> None:
        root = test_helpers.project_root()
        self.assertTrue((root / "quantdesk").is_dir())
        self.assertTrue((root / "scripts" / "self_check.py").exists())

    def test_assert_centralized_app_version_passes(self) -> None:
        # Should not raise on a healthy repo.
        test_helpers.assert_centralized_app_version(self)

    def test_assert_no_mojibake_passes_for_readme(self) -> None:
        readme = test_helpers.project_root() / "README.md"
        # README is part of the strict UTF-8 set; it must be clean.
        test_helpers.assert_no_mojibake(self, readme)


class CodebaseSlimmingDocsTests(unittest.TestCase):
    """The slimming plan exists and references the four target modules."""

    def test_plan_doc_exists_and_mentions_targets(self) -> None:
        doc = PROJECT_ROOT / "docs" / "codebase_slimming_plan.md"
        self.assertTrue(doc.exists(), str(doc))
        text = doc.read_text(encoding="utf-8")
        # ``quantdesk/server_payloads.py`` is deliberately absent here: it is a
        # port-8765 closure member, so its plan-doc mention is asserted by
        # ``tests/test_legacy_8765_codebase_slimming_payloads.py`` instead.
        for target in (
            "scripts/daily_quant_report.py",
            "quantdesk/backtest.py",
            "quantdesk/strategy_trade_replay.py",
            "quantdesk/explanations.py",
            "quantdesk/self_check_core.py",
        ):
            self.assertIn(target, text, msg=f"plan doc must mention {target}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


