"""Exercise the real review UI with separate, deterministic AI profile records."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
    constructor(id) {
        this.id = id; this.innerHTML = ''; this.textContent = '';
        this.dataset = {}; this.attributes = {}; this.listeners = {};
        this.disabled = false; this.hidden = false; this.style = {};
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    click() { for (const callback of this.listeners.click || []) callback({target: this}); }
}
const nodes = new Map();
const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Node(id));
    return nodes.get(id);
};
const html = fs.readFileSync('app/templates/daily_research.html', 'utf8');
const buttons = [...html.matchAll(/<button\b[^>]*data-ai-trader-profile="[^"]+"[^>]*>/g)].map(match => {
    const id = match[0].match(/\bid="([^"]+)"/)[1];
    const button = node(id);
    button.dataset.aiTraderProfile = match[0].match(/data-ai-trader-profile="([^"]+)"/)[1];
    return button;
});
const requests = [];
function decision(profile, marker = profile) {
    return {ok: true, available: true, profile, as_of: '2026-09-18', records: [
        {decision_id: marker, judgment: 'fixture-' + marker, result: '错',
         decision_date: '2026-09-17', recorded_at: '2026-09-17T20:20:00+08:00'}
    ]};
}
function reply(url) {
    const parsed = new URL(url, 'http://fixture.invalid');
    const profile = parsed.searchParams.get('profile') || 'default';
    if (parsed.pathname === '/api/decision-board') return {watchlist: [], events: [], as_of: '2026-09-18'};
    if (parsed.pathname === '/api/ai-trader/dashboard') return {
        ok: true, profile, updated_at: '2026-09-18T20:40:00+08:00', account: {},
        filled_trades: [{symbol: 'fixture-' + profile, date: '2026-09-18', side: 'buy', price: 10}],
    };
    if (parsed.pathname === '/api/ai-trader/decisions') return decision(profile);
    if (parsed.pathname === '/api/ai-trader/chat') return {ok: true, messages: []};
    throw new Error('Unexpected request: ' + url);
}
const context = vm.createContext({
    document: {
        addEventListener() {}, getElementById: node,
        querySelectorAll: selector => selector === '[data-ai-trader-profile]' ? buttons : [],
    },
    window: {}, URL, URLSearchParams, console, node, buttons, requests, assert, decision,
    reply,
    fetch: async (url, options = {}) => {
        const method = options.method || 'GET';
        requests.push({url, method});
        assert.equal(method, 'GET', 'Viewing or switching must never write');
        const body = await context.reply(url);
        return {ok: true, status: 200, json: async () => body};
    },
    flush: () => new Promise(resolve => setImmediate(resolve)),
});
vm.runInContext(fs.readFileSync('app/static/js/daily_research.js', 'utf8'), context);
function check(code) {
    return vm.runInContext('(async () => {' + code + '\n})()', context);
}
"""


def run_ui(scenario: str) -> None:
    node = shutil.which("node")
    assert node, "Node is required for the frontend behavior regression"
    result = subprocess.run(
        [node], cwd=ROOT, input=HARNESS + "\ncheck(" + json.dumps(scenario) + ").then(() => console.log('UI_SCENARIO_COMPLETE')).catch(error => { console.error(error); process.exitCode = 1; });\n",
        text=True, encoding="utf-8", capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UI_SCENARIO_COMPLETE" in result.stdout, "Scenario did not finish: " + result.stderr


def test_real_profile_buttons_reload_review_and_keep_both_selectors_in_sync() -> None:
    run_ui(r"""
        setupAiTraderPanel();
        await loadDecisionHistory();
        assert.match(node('decisionHistoryNote').textContent, /默认/);
        assert.equal(buttons.length, 4, 'Both AI ledger and review need profile selectors');
        node('reviewAiProfileGlm').click();
        await flush();
        for (const button of buttons) {
            assert.equal(button.attributes['aria-pressed'], button.dataset.aiTraderProfile === 'glm' ? 'true' : 'false');
        }
        assert(requests.some(row => row.url === '/api/ai-trader/dashboard?profile=glm'));
        assert(requests.some(row => row.url === '/api/ai-trader/decisions?profile=glm'));
        assert(requests.filter(row => row.url.startsWith('/api/decision-board')).every(row => row.url === '/api/decision-board'));
        assert.match(node('decisionHistoryPanel').innerHTML, /fixture-glm/);
        assert.doesNotMatch(node('decisionHistoryPanel').innerHTML, /fixture-default/);
        assert.match(node('decisionHistoryPanel').innerHTML, /GLM/);
        assert.match(node('decisionHistoryNote').textContent, /GLM/);
        assert.equal(node('aiTraderChatInput').disabled, true);
        const before = requests.length;
        node('aiTraderProfileDefault').click();
        await flush();
        assert(requests.slice(before).some(row => row.url === '/api/ai-trader/decisions'));
        assert.match(node('decisionHistoryPanel').innerHTML, /fixture-default/);
        assert.doesNotMatch(node('decisionHistoryPanel').innerHTML, /fixture-glm/);
        assert.match(node('decisionHistoryNote').textContent, /默认/);
        assert.equal(node('aiTraderChatInput').disabled, false);
        assert(requests.every(row => row.method === 'GET'));
    """)


@pytest.mark.parametrize("reject_old", [False, True], ids=["late-success", "late-failure"])
def test_late_previous_profile_does_not_overwrite_current_review(reject_old: bool) -> None:
    run_ui(r"""
        const realReply = reply;
        let release, reject;
        const delayed = new Promise((resolve, fail) => { release = resolve; reject = fail; });
        reply = url => url === '/api/ai-trader/decisions' ? delayed : realReply(url);
        const oldLoad = loadDecisionHistory();
        setAiTraderProfile('glm');
        await loadDecisionHistory();
        const rendered = node('decisionHistoryPanel').innerHTML;
        const note = node('decisionHistoryNote').textContent;
        assert.match(rendered, /fixture-glm/);
        if (REJECT_OLD) reject(new Error('late default failure'));
        else release(decision('default', 'late-default'));
        await oldLoad;
        assert.equal(node('decisionHistoryPanel').innerHTML, rendered);
        assert.equal(node('decisionHistoryNote').textContent, note);
    """.replace("REJECT_OLD", json.dumps(reject_old)))


def test_switch_away_and_back_invalidates_old_request_before_next_refresh() -> None:
    run_ui(r"""
        const realReply = reply;
        let release;
        const delayed = new Promise(resolve => { release = resolve; });
        reply = url => url === '/api/ai-trader/decisions' ? delayed : realReply(url);
        const oldLoad = loadDecisionHistory();
        setAiTraderProfile('glm');
        setAiTraderProfile('default');
        release(decision('default', 'obsolete-default'));
        await oldLoad;
        assert.doesNotMatch(node('decisionHistoryPanel').innerHTML, /obsolete-default/);
        reply = url => url === '/api/ai-trader/decisions' ? decision('default', 'fresh-default') : realReply(url);
        await loadDecisionHistory();
        assert.match(node('decisionHistoryPanel').innerHTML, /fresh-default/);
        assert.doesNotMatch(node('decisionHistoryPanel').innerHTML, /obsolete-default/);
    """)


def test_missing_current_profile_is_unknown_and_is_not_replaced_by_default() -> None:
    run_ui(r"""
        await loadDecisionHistory();
        const realReply = reply;
        reply = url => url.includes('/api/ai-trader/') && url.includes('profile=glm')
            ? {ok: false, available: false, profile: 'glm', warnings: ['fixture missing']} : realReply(url);
        setAiTraderProfile('glm');
        await loadDecisionHistory();
        assert.doesNotMatch(node('decisionHistoryPanel').innerHTML, /fixture-default/);
        assert.match(node('decisionHistoryNote').textContent, /GLM/);
        assert.match(node('decisionHistoryNote').textContent, /未知/);
        assert.match(node('decisionHistoryPanel').innerHTML, /未知/);
        reply = url => url.includes('/api/ai-trader/decisions')
            ? {ok: true, available: true, profile: 'glm', as_of: '2026-09-18', records: []} : realReply(url);
        await loadDecisionHistory();
        assert.match(node('decisionHistoryNote').textContent, /AI 判断 0 条/);
        assert.doesNotMatch(node('decisionHistoryPanel').innerHTML, />未知</);
    """)


def test_late_dashboard_cannot_replace_the_new_profile_panel() -> None:
    run_ui(r"""
        const realReply = reply;
        let release;
        const delayed = new Promise(resolve => { release = resolve; });
        reply = url => url === '/api/ai-trader/dashboard' ? delayed : realReply(url);
        const oldLoad = loadAiTraderPanel();
        setAiTraderProfile('glm');
        await loadAiTraderPanel();
        const rendered = node('aiTraderAccountPanel').innerHTML;
        const note = node('aiTraderStatusNote').textContent;
        assert.match(note, /GLM/);
        release({ok: true, profile: 'default', account: {equity: 987654}, filled_trades: []});
        await oldLoad;
        assert.equal(node('aiTraderAccountPanel').innerHTML, rendered);
        assert.equal(node('aiTraderStatusNote').textContent, note);
    """)


def test_late_default_chat_history_cannot_appear_in_glm_panel() -> None:
    run_ui(r"""
        const realReply = reply;
        let release;
        const delayed = new Promise(resolve => { release = resolve; });
        reply = url => url === '/api/ai-trader/chat' ? delayed : realReply(url);
        const oldLoad = loadAiTraderChatHistory();
        setAiTraderProfile('glm');
        await loadAiTraderChatHistory();
        const rendered = node('aiTraderChatLog').innerHTML;
        assert.match(rendered, /GLM.*暂不可用/);
        release({ok: true, messages: [{question: 'default-only', answer: 'private-default-answer'}]});
        await oldLoad;
        assert.equal(node('aiTraderChatLog').innerHTML, rendered);
        assert.doesNotMatch(node('aiTraderChatLog').innerHTML, /private-default-answer/);
    """)


@pytest.mark.parametrize("query,expected,status", [
    ("", "default", 200), ("?profile=default", "default", 200),
    ("?profile=glm", "glm", 200), ("?profile=unknown", None, 400),
])
def test_decision_api_profile_routing_reads_only_its_own_fixture(tmp_path, monkeypatch, query, expected, status) -> None:
    from ai_trader import paths
    from scripts.server.routes import ai_trader as routes

    sources = {}
    for profile in ("default", "glm"):
        path = tmp_path / profile / "decision_latest.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"schema": "ai_trader_decision_v1", "as_of": "2026-09-18", "count": 1,
                                    "records": [{"decision_id": profile, "judgment": "fixture-" + profile}]}), encoding="utf-8")
        sources[profile] = path
    before = {profile: path.read_bytes() for profile, path in sources.items()}
    monkeypatch.setattr(routes, "DECISION_LATEST_PATH", sources["default"])
    monkeypatch.setattr(paths, "profile_paths", lambda profile: SimpleNamespace(decision_latest=sources[profile]))
    app = Flask(__name__)
    app.register_blueprint(routes.create_ai_trader_blueprint())
    response = app.test_client().get("/api/ai-trader/decisions" + query)
    assert response.status_code == status
    body = response.get_json()
    if expected:
        assert body["profile"] == expected
        assert body["records"][0]["decision_id"] == expected
        assert response.headers["Cache-Control"] == "no-store"
    else:
        assert body["reason"] == "unknown_profile"
    assert {profile: path.read_bytes() for profile, path in sources.items()} == before
    assert len(list(tmp_path.rglob("*"))) == 4


def test_chart_overlay_cache_keeps_default_and_glm_evidence_separate() -> None:
    run_ui(r"""
        const realReply = reply;
        reply = url => {
            if (!url.startsWith('/api/ai-trader/dashboard')) return realReply(url);
            const profile = url.includes('profile=glm') ? 'glm' : 'default';
            return {ok: true, profile, filled_trades: [{symbol: '600000', date: '2026-09-18',
                side: 'buy', price: profile === 'glm' ? 20 : 10, source: 'fixture', price_basis: 'raw'}]};
        };
        const oldBook = await loadChartOverlays('600000', 'default');
        const glmBook = await loadChartOverlays('600000', 'glm');
        const oldBookAgain = await loadChartOverlays('600000', 'default');
        assert.equal(oldBook[0].price, 10);
        assert.equal(glmBook[0].price, 20);
        assert.equal(oldBookAgain[0].price, 10);
        assert.match(glmBook[0].label, /GLM/);
        assert.match(glmBook[0].api_source, /profile=glm/);
        assert.equal(requests.filter(row => row.url.startsWith('/api/ai-trader/dashboard')).length, 2);
    """)


def test_review_detail_click_preserves_its_profile_in_workbench_context() -> None:
    run_ui(r"""
        const rows = buildReviewItems(null, null, {records: [{decision_id:'glm-test', symbol:'600000',
            decision_date:'2026-09-18', result:'错', judgment:'fixture'}]}, 'glm');
        let opened;
        showDetail = (candidate, options) => { opened = {candidate, options}; };
        renderDecisionHistory(null, null, rows);
        const target = {dataset:{reviewIndex:'0', reviewSymbol:'600000'}, closest(){ return this; }};
        node('decisionHistoryPanel').onclick({target});
        assert.equal(opened.options.profile, 'glm');
        const context = workbenchContext(opened.candidate, {...opened.options, returnHash:'#review'}, false);
        assert.equal(context.profile, 'glm');
        assert.equal(workbenchContext({symbol:'600001'}, {context}, true).profile, 'glm');
    """)
