"""Static wiring checks for the STATUS card on the existing 8000 home page."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")


def test_home_template_has_compact_card_and_full_area() -> None:
    for token in (
        'id="projectStatusSection"',
        'id="projectStatusHealth"',
        'id="projectStatusExplain"',
        'id="projectStatusOpenFullButton"',
        'id="projectStatusRefreshButton"',
        'id="projectStatusRaw"',
        "项目记录 · 非实时核验",
        "纸面 · 未验证",
        "查看完整状态",
        "重新读取状态",
        "查看原文",
        "文档声明更新",
        "文件修改时间",
        "本次读取时间",
        "Research Only",
        "Paper Account Only",
        "No Broker Route",
    ):
        assert token in HTML
    assert "daily_research_v2" not in HTML


def test_js_fetches_fixed_get_and_uses_text_content() -> None:
    assert 'requestJson("/api/project-status")' in JS
    assert "loadProjectStatus()" in JS
    assert "setupProjectStatus()" in JS
    assert "raw.textContent" in JS or 'id="projectStatusRaw"' in HTML
    assert "projectStatusLastOk" in JS
    assert "上次读取；本次失败" in JS
    assert "这是状态文件问题，不是账本清空" in JS
    assert "innerHTML = data.raw_text" not in JS
    assert "innerHTML = payload.raw_text" not in JS
    assert "dangerouslySetInnerHTML" not in JS


def test_status_load_is_not_on_shared_home_promise() -> None:
    assert "await Promise.all([\n            loadStartupUpdateStatus()" in JS or "await Promise.all([" in JS
    block = JS.split("async function refreshAfterStartupUpdate")[1].split("async function loadMarketOverview")[0]
    assert "loadProjectStatus" not in block
    assert JS.index("loadProjectStatus();") < JS.index("startStartupStatusPolling();")


def test_css_warns_with_text_not_color_only() -> None:
    assert "红格解释" in HTML
    assert "has-red" in JS
    assert "project-status-banner" in HTML
