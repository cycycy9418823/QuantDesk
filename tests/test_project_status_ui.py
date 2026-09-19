"""Static wiring checks for the STATUS card on the existing 8000 home page."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

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


def _run_status_ui(script: str) -> dict:
    node = shutil.which("node")
    assert node is not None, "Node is required for the STATUS UI behavior test"
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const nodes = new Map();
class Node {
    constructor(id = "") {
        this.id = id;
        this.textContent = id === "projectStatusMeta" ? "正在读取 STATUS.md" : "";
        this.hidden = false;
        this.disabled = false;
        this.children = [];
        const values = new Set();
        this.classList = {
            add: value => values.add(value),
            remove: value => values.delete(value),
            contains: value => values.has(value),
            toggle: (value, force) => force ? values.add(value) : values.delete(value),
        };
    }
    addEventListener() {}
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
}
const document = {
    addEventListener() {},
    getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, new Node(id));
        return nodes.get(id);
    },
    createElement() { return new Node(); },
};
const responses = [];
const requests = [];
const context = {
    document,
    console,
    window: {},
    fetch: async (url, options) => {
        requests.push({url, method: options.method || "GET"});
        const next = responses.shift();
        if (next instanceof Error) throw next;
        return {ok: true, json: async () => next};
    },
    responses,
    requests,
    nodeText: id => document.getElementById(id).textContent,
    nodeHasClass: (id, value) => document.getElementById(id).classList.contains(value),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync("app/static/js/daily_research.js", "utf8"), context);
"""
    result = subprocess.run(
        [node],
        input=harness + "\n" + script,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_failed_refresh_retains_previous_content_and_new_attempt_time() -> None:
    result = _run_status_ui(r"""
vm.runInContext(`
    responses.push({
        readable: true,
        read_at: "first-read",
        document_updated_at_display: "document-time",
        file_modified_at: "file-time",
        health_line: "prior health",
        raw_text: "prior raw",
        phase: {raw: "B phase checkpoint checkpoint-date", summary: "B phase", checkpoint: "checkpoint-date"},
    });
    responses.push({readable: false, error: "missing_file", read_at: "failed-read"});
    (async () => {
        await loadProjectStatus();
        await loadProjectStatus();
        console.log(JSON.stringify({
            health: nodeText("projectStatusHealth"),
            raw: nodeText("projectStatusRaw"),
            documentTime: nodeText("projectStatusDocTime"),
            fileTime: nodeText("projectStatusFileTime"),
            readTime: nodeText("projectStatusReadTime"),
            banner: nodeText("projectStatusBanner"),
            phase: nodeText("projectStatusPhase"),
            failedTone: nodeHasClass("projectStatusHealth", "has-red"),
            requests,
        }));
    })();
`, context);
""")
    assert result["health"] == "prior health"
    assert result["raw"] == "prior raw"
    assert result["documentTime"] == "document-time"
    assert result["fileTime"] == "file-time"
    assert result["readTime"] == "failed-read"
    assert "上次读取；本次失败" in result["banner"]
    assert "这是状态文件问题" in result["banner"]
    assert result["phase"].count("checkpoint-date") == 1
    assert result["failedTone"] is True
    assert result["requests"] == [
        {"url": "/api/project-status", "method": "GET"},
        {"url": "/api/project-status", "method": "GET"},
    ]


def test_initial_failure_finishes_loading_and_does_not_claim_read_time() -> None:
    result = _run_status_ui(r"""
vm.runInContext(`
    responses.push(null);
    (async () => {
        await loadProjectStatus();
        console.log(JSON.stringify({
            meta: nodeText("projectStatusMeta"),
            readTime: nodeText("projectStatusReadTime"),
            health: nodeText("projectStatusHealth"),
        }));
    })();
`, context);
""")
    assert "正在读取" not in result["meta"]
    assert "服务器时间未提供" in result["readTime"]
    assert result["health"] == "无法读取 STATUS.md"


def test_status_load_is_not_on_shared_home_promise() -> None:
    assert "await Promise.all([\n            loadStartupUpdateStatus()" in JS or "await Promise.all([" in JS
    block = JS.split("async function refreshAfterStartupUpdate")[1].split("async function loadMarketOverview")[0]
    assert "loadProjectStatus" not in block
    assert JS.index("loadProjectStatus();") < JS.index("startStartupStatusPolling();")


def test_css_warns_with_text_not_color_only() -> None:
    assert "红格解释" in HTML
    assert "has-red" in JS
    assert "project-status-banner" in HTML
