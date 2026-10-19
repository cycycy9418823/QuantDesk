"""Run the real 8001 classroom page script under Node with a stub DOM.

Shared by tests/test_fix2web_*.py. The stub keeps ``innerHTML`` as the raw
string the page assigned, so tests can see exactly what markup the page would
have injected (escaped or not), and ``fetch`` answers from a canned route table
so no server, no network and no model are involved.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "ai_trader" / "templates" / "index.html"

_RUNNER_JS = r"""
const fs = require("fs");
const vm = require("vm");
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const elements = {};
function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", value: "", disabled: false,
    className: "", scrollTop: 0, scrollHeight: 0, children: [], _listeners: {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, fn) { this._listeners[type] = fn; },
    set onclick(fn) { this._listeners.click = fn; },
    get onclick() { return this._listeners.click; },
  };
}
const document = {
  getElementById(id) {
    if (!cfg.ids.includes(id)) return null;
    if (!elements[id]) elements[id] = makeEl(id);
    return elements[id];
  },
  createElement(tag) { return makeEl("<" + tag + ">"); },
};
const calls = [];
async function fetch(url, opts) {
  const method = (opts && opts.method) || "GET";
  calls.push({ url, method });
  const route = cfg.routes[method + " " + url] || cfg.routes[url];
  if (!route) throw new TypeError("Failed to fetch");
  const text = route.text !== undefined ? route.text : JSON.stringify(route.json);
  return {
    ok: route.status >= 200 && route.status < 300,
    status: route.status,
    statusText: route.statusText || "",
    async json() { return JSON.parse(text); },
    async text() { return text; },
  };
}
const unhandled = [];
process.on("unhandledRejection", (err) => { unhandled.push(String(err)); });
const ctx = vm.createContext({ document, fetch, console });
(async () => {
  vm.runInContext(cfg.script, ctx, { filename: "index.html.js" });
  await new Promise((r) => setTimeout(r, 30));
  if (cfg.renderPayload !== undefined) {
    ctx.__payload = cfg.renderPayload;
    vm.runInContext("render(__payload)", ctx);
  }
  for (const [id, v] of Object.entries(cfg.values || {})) document.getElementById(id).value = v;
  for (const id of cfg.click || []) {
    const el = elements[id];
    if (el && el._listeners.click) await el._listeners.click();
    await new Promise((r) => setTimeout(r, 30));
  }
  const out = {};
  for (const [id, el] of Object.entries(elements)) {
    out[id] = { innerHTML: el.innerHTML, textContent: el.textContent, disabled: el.disabled,
      children: el.children.map((c) => ({ className: c.className, textContent: c.textContent,
        children: c.children.map((g) => g.textContent) })) };
  }
  process.stdout.write(JSON.stringify({ elements: out, calls, unhandled }));
})().catch((err) => { process.stdout.write(JSON.stringify({ fatal: String(err && err.stack || err) })); });
"""


def node_or_skip() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; page-script behaviour cannot be executed")
    return node


def page_html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def page_script(html: str | None = None) -> str:
    text = page_html() if html is None else html
    blocks = re.findall(r"<script>(.*?)</script>", text, flags=re.S)
    assert blocks, "classroom page has no inline <script>"
    return "\n".join(blocks)


def page_ids(html: str | None = None) -> list[str]:
    text = page_html() if html is None else html
    return sorted(set(re.findall(r'id="([^"]+)"', text)))


def run_page(
    tmp_path: Path,
    routes: dict[str, dict],
    *,
    click: list[str] | None = None,
    values: dict[str, str] | None = None,
    render_payload: object = None,
) -> dict:
    """Execute the page script once; return final element state and fetch calls."""

    node = node_or_skip()
    cfg = {
        "script": page_script(),
        "ids": page_ids(),
        "routes": routes,
        "click": click or [],
        "values": values or {},
    }
    if render_payload is not None:
        cfg["renderPayload"] = render_payload
    cfg_path = tmp_path / "page_cfg.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    runner = tmp_path / "page_runner.js"
    runner.write_text(_RUNNER_JS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(runner), str(cfg_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert "fatal" not in result, result.get("fatal")
    return result


def json_route(payload: object, status: int = 200) -> dict:
    return {"status": status, "json": payload}


def text_route(text: str, status: int) -> dict:
    return {"status": status, "text": text}
