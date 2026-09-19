"""U1 navigation and hash-migration contract. Display-only; no network or writes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")

DAILY_ENTRIES = ("今天", "看盘", "两本账", "复盘学习")
SECONDARY_ENTRIES = ("研究工具", "系统状态")
REQUIRED_SIDE_TARGETS = (
    'data-side-module-target="daily-focus-section"',
    'data-side-module-target="paper-watchlist-section"',
    'data-side-module-target="agent-section"',
    'data-side-module-target="backtest-section"',
    'data-side-module-target="strategy-lab-section"',
    'data-side-module-target="data-ops-section"',
)
OLD_HASH_IDS = (
    "ai-log-section",
    "paper-watchlist-section",
    "decision-history-section",
    "performance-section",
    "daily-focus-section",
    "agent-section",
    "ai-trader-section",
    "backtest-section",
    "strategy-lab-section",
    "data-ops-section",
)


def test_four_daily_entries_and_secondary_nav_are_present() -> None:
    for label in DAILY_ENTRIES + SECONDARY_ENTRIES:
        assert f">{label}</span>" in HTML or f">{label}</" in HTML
    assert 'data-page="today"' in HTML
    assert 'data-page="watch"' in HTML
    assert 'data-page="ledgers"' in HTML
    assert 'data-page="review"' in HTML
    assert 'data-page="system"' in HTML
    assert "daily_research_v2" not in HTML


def test_legacy_side_targets_and_sections_remain() -> None:
    for token in REQUIRED_SIDE_TARGETS:
        assert token in HTML
    for section_id in OLD_HASH_IDS:
        assert f'id="{section_id}"' in HTML
    assert "data-module-home" in HTML
    assert "返回首页" in HTML
    assert "showHomeView" in JS
    assert "showModuleView" in JS


def test_old_hashes_have_explicit_aliases_or_section_ids() -> None:
    assert "const HASH_ALIASES" in JS
    for alias in (
        "today",
        "watch",
        "ledgers",
        "review",
        "tools",
        "system",
        "watch-pool",
        "decision-history",
        "ai-log",
        "daily-focus",
        "ai-trader",
        "data-ops",
    ):
        assert f"{alias}:" in JS or f'"{alias}":' in JS
    for section_id in OLD_HASH_IDS:
        assert section_id in JS


def test_today_page_keeps_status_card_without_second_reader() -> None:
    assert 'id="projectStatusSection"' in HTML
    assert "项目记录 · 非实时核验" in HTML
    assert "文档声明更新" in HTML
    assert "文件修改时间" in HTML
    assert "本次读取时间" in HTML
    assert 'requestJson("/api/project-status")' in JS
    assert "打开系统状态" in HTML
    assert "quantdeskShowPage" in JS
    assert 'id="kpiPaperCard"' in HTML
    assert 'id="kpiAiCard"' in HTML


def test_ledger_switch_does_not_merge_accounts() -> None:
    assert HTML.count("策略账") >= 2
    assert HTML.count("AI账") >= 2
    assert 'data-side-module-target="paper-watchlist-section"' in HTML
    assert 'data-side-module-target="ai-trader-section"' in HTML
    assert "ledger-switch" in HTML


def test_mobile_nav_wraps_instead_of_only_horizontal_scroll() -> None:
    assert "flex-wrap: wrap" in HTML
    assert "overflow-x: hidden" in HTML


def test_workbench_opens_on_charts_with_judgment() -> None:
    assert 'title: "看盘"' in JS
    assert 'members: ["charts", "brief"]' in JS
    assert 'activateDetailPane("charts")' in JS
    assert "workbenchPrev" in HTML
    assert "workbenchNext" in HTML
    assert "#watch/" in JS or "watch/${" in JS or "watch/${hashSymbol}" in JS


def test_today_kpi_does_not_treat_zero_new_generation_as_empty_pool() -> None:
    assert "今日未新生成" in JS
    assert "可读复核池" in JS
    assert "health-bad" in JS
    assert "成绩单仍在读" in JS


def test_chart_layers_keep_rule_ai_and_fill_separate() -> None:
    assert 'kind === "paper_fill"' in JS
    assert 'kind === "ai_plan"' in JS
    assert '"入选观察"' in JS
    assert "AI·已提交" in JS or "AI·拟议" in JS
    assert "策略账·纸面买" in JS
    assert "买入信号" not in JS


def test_review_page_has_mismatch_first_buckets() -> None:
    assert 'id: "mismatch"' in JS
    assert 'id: "evidence_gap"' in JS
    assert "需复核" in JS
    assert "到期缺证据" in JS
    assert 'items.filter((item) => item.bucket === id).length' in JS
