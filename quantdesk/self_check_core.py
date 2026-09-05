"""Pure helpers used by ``scripts/self_check.py``.

This module hosts the rule constants and stateless functions that
``scripts/self_check.py`` uses for danger-phrase scanning, version
centralization checks, file-existence checks, and clean-zip auditing.

The goal of v2.6.3-codebase-slimming is to make these helpers easy to
unit-test without spinning up the CLI.  The script is still the entry
point and keeps its PASS / WARN / FAIL output format unchanged.

Conservative wording, no broker connection, no automated order
placement, and no investment advice are all guaranteed by keeping the
business logic identical to what already lived in ``self_check.py``.
"""

from __future__ import annotations

import fnmatch
import os
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Danger-phrase rules used by ``check_no_dangerous_frontend_text``.
# ---------------------------------------------------------------------------

STRICT_FORBIDDEN_OUTPUT_PHRASES: tuple[str, ...] = (
    "接下来预测",
    "小仓试一下",
    "可考虑买入",
    "建议买入",
    "推荐卖出",
    "交易建议",
    "操作建议",
    "目标价",
    "预测涨幅",
    "减仓或退出",
    "减仓/卖出",
    "AI操作剧本",
    "稳赚",
    "必涨",
    "保证收益",
    "无风险",
    "强烈买入",
    "满仓",
    "交易信号",
    "操作信号",
    "胜率",
    "年化收益",
    "买前检查",
    "信号点",
    "信号解释",
    "推荐区",
    "不建议主动参与",
    "短线可能很刺激",
)


CONTEXT_SENSITIVE_TERMS: tuple[str, ...] = (
    "买入",
    "卖出",
    "建仓",
    "离场",
    "持仓",
    "盈亏",
)


ALLOWED_CONTEXT_MARKERS: tuple[str, ...] = (
    "纸面",
    "虚拟",
    "复核",
    "复盘",
    "观察",
    "风险",
    "不构成投资",
    "不是真实",
    "不会真实",
    "回测",
    "模拟",
    "记录",
    "未持仓",
    "持仓中",
    "持仓数",
    "快照",
    "文件",
    "组合",
    "自动虚拟组合",
    "昨日",
    "昨天",
    "今日",
    "每日",
    "第一次",
    "连续复盘",
    "连续账本",
    "状态源",
    "保存",
    "浮动",
    "印花税",
    "手续费",
    "体检",
    "对比",
    "统计",
    "筛选",
    "计划",
    "导航",
)


SAFE_NEGATIVE_PHRASES: tuple[str, ...] = (
    "不构成投资建议",
    "不构成操作建议",
    "不构成交易建议",
    "不提供买卖建议",
    "不保证收益",
    "不预测价格走势",
    "不预测",
    "不计算胜率",
    "不返回胜率",
    "不会返回胜率",
    "不计算年化收益",
    "不返回年化收益",
    "不会返回年化收益",
    "不会真实下单",
    "不是真实交易",
)


RULE_DEFINITION_TOKENS: tuple[str, ...] = (
    "DANGEROUS_PHRASES",
    "STRICT_FORBIDDEN_OUTPUT_PHRASES",
    "禁止",
    "不得",
    "不允许示例",
    "FORBIDDEN",
)


def strip_safe_negative_contexts(text: str, phrases: Sequence[str] | None = None) -> str:
    """Remove safe negative phrases like ``不构成投资建议`` before scanning.

    The goal is to keep the danger-phrase scanner from flagging legitimate
    risk-disclaimer text such as ``不预测价格走势``.
    """
    cleaned = text
    for phrase in phrases or SAFE_NEGATIVE_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    return cleaned


def line_is_rule_definition(line: str, tokens: Sequence[str] | None = None) -> bool:
    """Return True for source lines that define the danger-phrase rule itself.

    These lines must be skipped so the rule definition does not flag itself.
    """
    return any(token in line for token in (tokens or RULE_DEFINITION_TOKENS))


def has_allowed_context(line: str, term: str, markers: Sequence[str] | None = None) -> bool:
    """Return True if ``term`` appears near an allowed context marker.

    A small symmetric window around the term is checked so that
    ``虚拟买入`` is treated as safe while a bare ``买入`` is flagged.
    """
    index = line.find(term)
    if index < 0:
        return True
    window = line[max(0, index - 18): index + len(term) + 18]
    return any(marker in window for marker in (markers or ALLOWED_CONTEXT_MARKERS))


def scan_text_for_dangerous_phrases(
    text: str,
    *,
    strict_phrases: Sequence[str] | None = None,
    context_terms: Sequence[str] | None = None,
    allowed_markers: Sequence[str] | None = None,
    rule_definition_tokens: Sequence[str] | None = None,
) -> list[tuple[int, str]]:
    """Scan multi-line ``text`` and return offending ``(line_no, marker)`` pairs.

    Line numbers are 1-based to match how CLI tools display them.
    """
    strict = tuple(strict_phrases or STRICT_FORBIDDEN_OUTPUT_PHRASES)
    terms = tuple(context_terms or CONTEXT_SENSITIVE_TERMS)
    markers = tuple(allowed_markers or ALLOWED_CONTEXT_MARKERS)
    rule_tokens = tuple(rule_definition_tokens or RULE_DEFINITION_TOKENS)
    hits: list[tuple[int, str]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if line_is_rule_definition(raw_line, rule_tokens):
            continue
        line = strip_safe_negative_contexts(raw_line)
        matched = False
        for phrase in strict:
            if phrase in line:
                hits.append((line_no, phrase))
                matched = True
                break
        if matched:
            continue
        for term in terms:
            if term in line and not has_allowed_context(line, term, markers):
                hits.append((line_no, term))
                break
    return hits


# ---------------------------------------------------------------------------
# File / version centralization helpers.
# ---------------------------------------------------------------------------


def missing_paths(paths: Iterable[Path]) -> list[Path]:
    """Return the subset of ``paths`` that do not exist on disk."""
    return [path for path in paths if not path.exists()]


def find_hardcoded_project_paths(
    root: Path,
    forbidden_fragments: Sequence[str],
    *,
    ignored_dirs: Iterable[str] = (
        ".venv",
        "data",
        "logs",
        "reports",
        "results",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tmp",
        ".tmp_tests",
        ".quantdesk_test_tmp",
        ".git",
        ".github",
        ".agents",
        ".playwright-cli",
        "dist",
        "build",
        "test_runtime",
        ".worktrees",
    ),
    allowed_suffixes: Iterable[str] = (
        ".py",
        ".ps1",
        ".cmd",
        ".vbs",
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".js",
        ".css",
        ".html",
    ),
    ignored_file_patterns: Iterable[str] = (
        "CODEX_TASK*.md",
        "REFACTOR*.md",
        "round12_rectification_appendix2_errata_20260801.md",
        # Self-referencing files: the checker itself must define forbidden
        # fragments, and its test file must exercise them. They are not
        # offenders.
        "self_check_core.py",
        "test_v263_codebase_slimming.py",
    ),
    excluded_fragments: Iterable[str] = (
        # The canonical tree path fragment is allowed: only the OLD repo path
        # is forbidden. Without this, "E:/codex/quant_new" (canonical) matches
        # the forbidden "E:/codex/quant" (old repo) as a substring.
        "E:" + "/" + "codex" + "/" + "quant_new",
        "E:" + "\\" + "codex" + "\\" + "quant_new",
    ),
) -> list[str]:
    """Walk ``root`` and return relative paths that contain any forbidden fragment."""
    ignored = set(ignored_dirs)
    suffixes = set(allowed_suffixes)
    ignored_patterns = tuple(ignored_file_patterns)
    offenders: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignored]
        current = Path(dirpath)
        for filename in filenames:
            if any(fnmatch.fnmatch(filename, pattern) for pattern in ignored_patterns):
                continue
            path = current / filename
            if path.suffix.lower() not in suffixes:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part in ignored for part in relative.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Strip allowed fragments (e.g., the canonical quant_new path) before
            # checking forbidden ones (the old quant path). Without this,
            # "E:/codex/quant_new" is a substring match for forbidden
            # "E:/codex/quant".
            for allowed_fragment in excluded_fragments:
                text = text.replace(allowed_fragment, "")
            if any(fragment in text for fragment in forbidden_fragments):
                offenders.append(str(relative))
    return offenders


def version_centralization_issues(
    *,
    version_text: str,
    server_text: str,
    config_text: str,
    readme_text: str,
    roadmap_text: str,
    docs_text: str,
    app_version: str,
    api_schema_version: str,
    release_title: str,
    required_version_tokens: Sequence[str] = (
        "APP_VERSION",
        "API_SCHEMA_VERSION",
        "RELEASE_CODENAME",
        "RELEASE_TITLE",
    ),
    required_server_tokens: Sequence[str] = (
        "from quantdesk.version import APP_VERSION",
    ),
) -> list[str]:
    """Return a list of human-readable issues found in version centralization."""
    missing: list[str] = []
    for token in required_version_tokens:
        if token not in version_text:
            missing.append(f"version.py missing {token}")
    if app_version not in config_text:
        missing.append("config/default.yaml missing APP_VERSION")
    if api_schema_version not in config_text:
        missing.append("config/default.yaml missing API_SCHEMA_VERSION")
    if "from quantdesk.version import" not in server_text:
        missing.append("quant_api_server.py missing quantdesk.version import")
    for token in required_server_tokens:
        if token not in server_text:
            missing.append(f"quant_api_server.py missing {token}")
    for label, text in (
        ("README.md", readme_text),
        ("ROADMAP.md", roadmap_text),
        ("docs/api_payload_schema.md", docs_text),
    ):
        if release_title not in text:
            missing.append(f"{label} missing {release_title}")
    return missing


# ---------------------------------------------------------------------------
# Clean-zip auditing helpers.
# ---------------------------------------------------------------------------


CLEAN_ZIP_BAD_TOKENS: tuple[str, ...] = (
    ".venv",
    "data/daily",
    "data/raw",
    "data/news",
    "state/",
    "paper_portfolio.json",
    "watchlist.json",
    "manual_confirmation_queue.json",
    "live_monitor_events.jsonl",
    "vectorbt/",
    "backtrader/",
    "qlib/",
    "FinRL/",
    "finrl/",
    "logs/",
    "reports/",
    "results/",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp_tests",
    ".quantdesk_test_tmp",
)


CLEAN_ZIP_BAD_SUFFIXES: tuple[str, ...] = (".log", ".tmp", ".bak")


def list_zip_entries(zip_path: Path) -> list[str]:
    """Return all entry names from ``zip_path``.

    Raises ``zipfile.BadZipFile`` if the file is not a valid zip archive.
    """
    with zipfile.ZipFile(zip_path) as archive:
        return archive.namelist()


def audit_clean_zip_names(
    names: Sequence[str],
    required: Sequence[str],
    *,
    bad_tokens: Sequence[str] = CLEAN_ZIP_BAD_TOKENS,
    bad_suffixes: Sequence[str] = CLEAN_ZIP_BAD_SUFFIXES,
) -> dict[str, list[str]]:
    """Audit zip entry names and return a categorized report.

    The returned dict has three keys: ``bad`` (runtime entries that should
    not appear), ``missing`` (required entries that are absent), and
    ``backslashes`` (entries that use Windows-style separators).
    """
    bad = [
        name
        for name in names
        if not name.startswith("templates/data/")
        and (any(token in name for token in bad_tokens) or name.endswith(tuple(bad_suffixes)))
    ]
    missing = [name for name in required if name not in names]
    backslashes = [name for name in names if "\\" in name]
    return {"bad": bad, "missing": missing, "backslashes": backslashes}


# ---------------------------------------------------------------------------
# Visible-text health helpers.
# ---------------------------------------------------------------------------


MOJIBAKE_PATTERN = r"\?{3,}|\uFFFD|\u9472|\u6D94|\u7EDB|\u95BE"


def contains_mojibake(text: str, pattern: str = MOJIBAKE_PATTERN) -> bool:
    """Return True if ``text`` contains any broken-encoding marker."""
    import re

    return re.search(pattern, text) is not None
