from __future__ import annotations

import ast
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "test_inventory_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "test_inventory_latest.md"
DISCLAIMER = "Read-only test inventory. This diagnostic does not rewrite, delete, skip, or deselect tests."

VERSION_RE = re.compile(r"test_(v\d{1,4}|rc\d+|daily|backtest|data|api|script|walk_forward)", re.IGNORECASE)
LEGACY_VERSIONED_ACCEPTANCE_RE = re.compile(r"test_v\d{1,4}", re.IGNORECASE)
LEGACY_TIER = "legacy_versioned_acceptance"


@dataclass(frozen=True)
class TestRecord:
    path: str
    name: str
    line_count: int
    test_count: int
    class_count: int
    version_label: str
    tier: str
    categories: list[str]
    uses_subprocess: bool
    uses_qd_cli: bool
    uses_network_guard: bool
    touches_data_or_backtest: bool
    touches_frontend: bool
    touches_filesystem: bool
    runner: str
    recommendation: str


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _version_label(name: str) -> str:
    match = VERSION_RE.search(name)
    return match.group(1).lower() if match else ""


def _ast_counts(text: str) -> tuple[int, int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0, 0
    test_count = 0
    class_count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_count += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            test_count += 1
    return test_count, class_count


def _runner_kind(text: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "unittest"
    has_unittest_class = False
    has_top_level_test = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                text_base = ast.unparse(base) if hasattr(ast, "unparse") else ""
                if "TestCase" in text_base:
                    has_unittest_class = True
                    break
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            has_top_level_test = True
    # Any module-level test function needs pytest: unittest discovery would
    # silently skip it, even when the same file also defines TestCase classes.
    if has_top_level_test:
        return "pytest"
    return "unittest"


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def _categories(path: Path, text: str) -> list[str]:
    name = path.name.lower()
    categories: set[str] = set()
    if _version_label(path.name):
        categories.add("versioned")
    if LEGACY_VERSIONED_ACCEPTANCE_RE.search(path.name):
        categories.add("legacy_acceptance")
    if _has_any(text, ("subprocess.run", "scripts/qd.py", "scripts\\qd.py", "sys.executable")):
        categories.add("cli_or_subprocess")
    if _has_any(text, ("self_check.py", "verify_clean_zip.py", "export_project", "release", "clean_zip")):
        categories.add("release_gate")
    if _has_any(text, ("backtest", "walk_forward", "factor_eval", "factor-combine", "strategy_viability")):
        categories.add("backtest_or_strategy")
    if _has_any(text, ("data/daily", "data\\daily", "data_health", "data-quality", "data_update", "daily csv")):
        categories.add("data_quality")
    if _has_any(text, ("network false", "network_disabled", "enable-network", "network", "provider")):
        categories.add("network_boundary")
    if _has_any(text, ("app/static", "app.js", "index.html", "playwright", "canvas", "frontend")):
        categories.add("frontend")
    if _has_any(text, ("tmp_path", "tempfile", "write_text", "mkdir", "test_artifact_dir", ".quantdesk_test_tmp")):
        categories.add("filesystem")
    if _has_any(text, ("paper", "no broker", "real orders", "qmt", "not investment advice")):
        categories.add("research_safety_boundary")
    if "script_inventory" in name or "maintenance" in text:
        categories.add("maintenance")
    if not categories:
        categories.add("unit")
    return sorted(categories)


def _tier(categories: list[str], test_count: int, line_count: int) -> str:
    category_set = set(categories)
    if "legacy_acceptance" in category_set:
        return LEGACY_TIER
    if "release_gate" in category_set or line_count > 1200 or test_count > 35:
        return "tier3_release_or_long"
    if "cli_or_subprocess" in category_set or "filesystem" in category_set:
        return "tier2_cli_or_filesystem"
    if category_set & {"backtest_or_strategy", "data_quality", "frontend", "network_boundary"}:
        return "tier1_focused_domain"
    return "tier0_fast_unit"


def _recommendation(tier: str, categories: list[str]) -> str:
    if tier == LEGACY_TIER:
        return "run_only_with_include_legacy_for_archival_acceptance"
    if tier == "tier0_fast_unit":
        return "run_in_default_fast_suite"
    if tier == "tier1_focused_domain":
        return "run_by_domain_or_changed_module"
    if tier == "tier2_cli_or_filesystem":
        return "run_in_cli_filesystem_gate"
    return "run_in_release_or_nightly_gate"


def _iter_test_files(project_root: Path) -> list[Path]:
    tests_dir = project_root / "tests"
    if not tests_dir.exists():
        return []
    return sorted(path for path in tests_dir.glob("test_*.py") if path.is_file())


def _test_command(paths: list[str]) -> str:
    if not paths:
        return ""
    return f"python scripts/qd.py maintenance test-tier-runner --limit {len(paths)} --dry-run false --json true --summary true"


def _tier_runner_commands(records: list[TestRecord], *, limit_per_tier: int = 40) -> dict[str, str]:
    commands: dict[str, str] = {}
    for tier in ("tier0_fast_unit", "tier1_focused_domain", "tier2_cli_or_filesystem", "tier3_release_or_long", LEGACY_TIER):
        paths = [
            record.path
            for record in sorted(records, key=lambda item: (item.path))
            if record.tier == tier
        ][:limit_per_tier]
        base = _test_command(paths)
        commands[tier] = base.replace("--limit", f"--tier {tier} --limit", 1) if base else ""
        if tier == "tier3_release_or_long" and commands[tier]:
            commands[tier] = commands[tier].replace("--dry-run false", "--include-tier3 true --dry-run false", 1)
        if tier == LEGACY_TIER and commands[tier]:
            commands[tier] = commands[tier].replace("--dry-run false", "--include-legacy true --dry-run false", 1)
    return commands


def build_test_inventory(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    project_root = Path(project_root)
    records: list[TestRecord] = []
    for path in _iter_test_files(project_root):
        text = _read_text(path)
        test_count, class_count = _ast_counts(text)
        line_count = len(text.splitlines()) if text else 0
        categories = _categories(path, text)
        tier = _tier(categories, test_count, line_count)
        records.append(
            TestRecord(
                path=_rel(path, root=project_root),
                name=path.name,
                line_count=line_count,
                test_count=test_count,
                class_count=class_count,
                version_label=_version_label(path.name),
                tier=tier,
                categories=categories,
                uses_subprocess="cli_or_subprocess" in categories,
                uses_qd_cli=_has_any(text, ("scripts/qd.py", "scripts\\qd.py")),
                uses_network_guard="network_boundary" in categories,
                touches_data_or_backtest=bool(set(categories) & {"data_quality", "backtest_or_strategy"}),
                touches_frontend="frontend" in categories,
                touches_filesystem="filesystem" in categories,
                runner=_runner_kind(text),
                recommendation=_recommendation(tier, categories),
            )
        )
    by_tier = Counter(record.tier for record in records)
    by_category = Counter(category for record in records for category in record.categories)
    by_version = Counter(record.version_label for record in records if record.version_label)
    large_file_candidates = [
        asdict(record)
        for record in sorted(records, key=lambda item: (-item.line_count, -item.test_count, item.path))
        if record.line_count > 1200 or record.test_count > 35
    ][:40]
    warnings: list[str] = []
    if by_tier.get("tier3_release_or_long", 0):
        warnings.append("release_or_long_tests_present")
    if by_tier.get(LEGACY_TIER, 0):
        warnings.append("legacy_versioned_acceptance_tests_present")
    if by_tier.get("tier2_cli_or_filesystem", 0):
        warnings.append("cli_or_filesystem_tests_present")
    if by_category.get("versioned", 0) > 100:
        warnings.append("large_versioned_test_surface")
    payload: dict[str, Any] = {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "test_inventory",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "test_file_count": len(records),
        "test_method_count": sum(record.test_count for record in records),
        "counts_by_tier": dict(sorted(by_tier.items())),
        "counts_by_category": dict(sorted(by_category.items())),
        "counts_by_version": dict(sorted(by_version.items())),
        "cli_or_subprocess_test_count": len([record for record in records if record.uses_subprocess]),
        "qd_cli_test_count": len([record for record in records if record.uses_qd_cli]),
        "data_or_backtest_test_count": len([record for record in records if record.touches_data_or_backtest]),
        "frontend_test_count": len([record for record in records if record.touches_frontend]),
        "network_boundary_test_count": len([record for record in records if record.uses_network_guard]),
        "counts_by_runner": dict(sorted(Counter(record.runner for record in records).items())),
        "top_long_tests": [
            asdict(record)
            for record in sorted(records, key=lambda item: (-item.line_count, item.path))[:30]
        ],
        "large_file_split_candidates": large_file_candidates,
        "tier_runner_commands": _tier_runner_commands(records),
        "tier3_release_or_long_tests": [
            asdict(record)
            for record in sorted(records, key=lambda item: (-item.test_count, item.path))
            if record.tier == "tier3_release_or_long"
        ][:80],
        "legacy_versioned_acceptance_tests": [
            asdict(record)
            for record in sorted(records, key=lambda item: (-item.test_count, item.path))
            if record.tier == LEGACY_TIER
        ][:120],
        "tier2_cli_or_filesystem_tests": [
            asdict(record)
            for record in sorted(records, key=lambda item: (-item.test_count, item.path))
            if record.tier == "tier2_cli_or_filesystem"
        ][:80],
        "records": [asdict(record) for record in sorted(records, key=lambda item: item.path)],
        "recommendations": [
            "Keep tier0 fast unit tests as the default local smoke suite.",
            "Run tier1 by changed domain: data, backtest, frontend, provider, or safety boundary.",
            "Run tier2 when CLI wrappers, subprocess behavior, or filesystem outputs change.",
            "Reserve tier3 for release, nightly, or explicit broad regression gates.",
            "Keep legacy versioned acceptance tests out of default gates; run them only with --include-legacy true.",
            "Do not delete tests only to reduce runtime; split, cache, or narrow setup first.",
        ],
        "warnings": warnings,
        "warning_count": len(warnings),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_test_inventory_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Test Inventory",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- test_file_count: `{payload.get('test_file_count')}`",
        f"- test_method_count: `{payload.get('test_method_count')}`",
        f"- cli_or_subprocess_test_count: `{payload.get('cli_or_subprocess_test_count')}`",
        f"- data_or_backtest_test_count: `{payload.get('data_or_backtest_test_count')}`",
        f"- frontend_test_count: `{payload.get('frontend_test_count')}`",
        "",
        "## Counts By Tier",
        "",
        "| tier | count |",
        "| --- | ---: |",
    ]
    for tier, count in (payload.get("counts_by_tier") or {}).items():
        lines.append(f"| `{tier}` | {count} |")
    lines.extend(["", "## Counts By Category", "", "| category | count |", "| --- | ---: |"])
    for category, count in (payload.get("counts_by_category") or {}).items():
        lines.append(f"| `{category}` | {count} |")
    lines.extend(["", "## Tier 3 Release Or Long Tests", ""])
    tier3 = payload.get("tier3_release_or_long_tests") or []
    if tier3:
        for item in tier3[:40]:
            lines.append(
                f"- `{item.get('path')}` tests={item.get('test_count')} lines={item.get('line_count')} "
                f"categories={item.get('categories')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Legacy Versioned Acceptance Tests", ""])
    legacy = payload.get("legacy_versioned_acceptance_tests") or []
    if legacy:
        for item in legacy[:40]:
            lines.append(
                f"- `{item.get('path')}` tests={item.get('test_count')} lines={item.get('line_count')} "
                f"categories={item.get('categories')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Large File Split Candidates", ""])
    split_candidates = payload.get("large_file_split_candidates") or []
    if split_candidates:
        for item in split_candidates[:20]:
            lines.append(
                f"- `{item.get('path')}` tests={item.get('test_count')} lines={item.get('line_count')} "
                f"recommendation={item.get('recommendation')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Tier Runner Commands", ""])
    commands = payload.get("tier_runner_commands") or {}
    if commands:
        for tier, command in commands.items():
            lines.append(f"- `{tier}`: `{command or 'no tests'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Tier 2 CLI Or Filesystem Tests", ""])
    tier2 = payload.get("tier2_cli_or_filesystem_tests") or []
    if tier2:
        for item in tier2[:40]:
            lines.append(
                f"- `{item.get('path')}` tests={item.get('test_count')} lines={item.get('line_count')} "
                f"categories={item.get('categories')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Recommendations", ""])
    lines.extend([f"- {item}" for item in payload.get("recommendations") or []] or ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    return "\n".join(lines) + "\n"


def write_test_inventory_outputs(
    payload: dict[str, Any] | None = None,
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload or build_test_inventory())
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_test_inventory_markdown(payload))
    return payload


def compact_test_inventory_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "test_file_count": payload.get("test_file_count"),
        "test_method_count": payload.get("test_method_count"),
        "counts_by_tier": payload.get("counts_by_tier"),
        "counts_by_category": payload.get("counts_by_category"),
        "cli_or_subprocess_test_count": payload.get("cli_or_subprocess_test_count"),
        "qd_cli_test_count": payload.get("qd_cli_test_count"),
        "data_or_backtest_test_count": payload.get("data_or_backtest_test_count"),
        "frontend_test_count": payload.get("frontend_test_count"),
        "network_boundary_test_count": payload.get("network_boundary_test_count"),
        "counts_by_runner": payload.get("counts_by_runner"),
        "large_file_split_candidates": payload.get("large_file_split_candidates"),
        "tier_runner_commands": payload.get("tier_runner_commands"),
        "warnings": payload.get("warnings"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
