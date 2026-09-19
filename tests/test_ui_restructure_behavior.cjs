"use strict";

// Executes the production script in a small DOM harness. No HTTP server,
// provider, browser storage, or production data is used by these regressions.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app/static/js/daily_research.js"), "utf8");

class NodeStub {
    constructor(id = "") {
        this.id = id; this.innerHTML = ""; this.textContent = "";
        this.dataset = {}; this.style = {}; this.hidden = false; this.disabled = false;
        this.listeners = {}; this.attributes = {};
        const classes = new Set();
        this.classList = {
            add: (...names) => names.forEach((name) => classes.add(name)),
            remove: (...names) => names.forEach((name) => classes.delete(name)),
            contains: (name) => classes.has(name),
            toggle: (name, yes) => (yes ? classes.add(name) : classes.delete(name)),
        };
    }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    closest() { return this; }
    dispatch(name, event) {
        if (this[`on${name}`]) this[`on${name}`](event);
        for (const listener of this.listeners[name] || []) listener(event);
    }
}

function harness() {
    const nodes = new Map();
    const node = (id) => {
        if (!nodes.has(id)) nodes.set(id, new NodeStub(id));
        return nodes.get(id);
    };
    const listeners = {};
    const location = { pathname: "/", search: "", hash: "#watch" };
    const history = {
        state: null,
        replaceState(state, _, url) { this.state = state; location.hash = new URL(url, "http://127.0.0.1/").hash; },
        pushState(state, _, url) { this.replaceState(state, _, url); },
    };
    const document = {
        body: new NodeStub("body"),
        getElementById: node,
        querySelectorAll: () => [],
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    };
    const window = {
        location, history, scrollY: 0,
        scrollTo(value) { this.scrollY = value.top ?? 0; },
        requestAnimationFrame(fn) { fn(); },
        setTimeout: () => 0, setInterval: () => 0, clearInterval() {},
        addEventListener() {}, dispatchEvent() {},
    };
    const context = vm.createContext({
        document, window, history, URL, URLSearchParams, Element: NodeStub,
        Event: class Event {},
        console: { warn() {}, error() {}, log() {} },
        fetch() { throw new Error("Unexpected network access in UI behavior test"); },
        setTimeout: window.setTimeout, clearTimeout() {},
        localStorage: { getItem: () => null, setItem() {} },
    });
    vm.runInContext(source, context, { filename: "daily_research.js" });
    return { context, node, document, window, history, evaluate: (code) => vm.runInContext(code, context) };
}

function deferred() {
    let resolve;
    const promise = new Promise((finish) => { resolve = finish; });
    return { promise, resolve };
}

function detail(symbol) {
    return { basic_info: { symbol, date: "2026-09-04" }, chart_data: { symbol, price_basis: "qfq", kline_data: [] } };
}

function prepareDetail(h) {
    h.context.renderDetailContent = (data) => `detail:${data.basic_info.symbol}`;
    h.context.setupDetailNavigation = () => {};
    h.context.bindCandidateActionButtons = () => {};
    h.context.activateDetailPane = () => {};
    h.context.renderWorkbenchEventList = () => {};
    h.context.refreshNewsForSymbol = () => {};
    h.context.loadChartOverlays = async () => [];
}

function event(overrides = {}) {
    return {
        event_id: "fill-1", source: "fixture.daily", api_source: "/fixture",
        event_time: "2026-09-04T09:30:00+08:00", date: "2026-09-04",
        ledger: "AI账", panel: "price", kind: "paper_fill", layer: "paper_fill",
        label: "AI账·纸面买", text: "deterministic fixture", side: "buy",
        price: 12.34, price_basis: "qfq", plot: true,
        evidence: { source: "fixture.daily", timestamp: "2026-09-04T09:30:00+08:00", price: 12.34 },
        ...overrides,
    };
}

test("detail: late stock A response cannot replace stock B", async () => {
    const h = harness(); prepareDetail(h);
    const a = deferred();
    h.context.requestJson = (url) => url.includes("/A/") ? a.promise : Promise.resolve(detail("B"));
    const pending = h.context.showDetail({ symbol: "A", name: "A" });
    await h.context.showDetail({ symbol: "B", name: "B" });
    a.resolve(detail("A")); await pending;
    assert.equal(h.node("modalBody").innerHTML, "detail:B");
    assert.equal(h.evaluate("currentDetailChartData.symbol"), "B");
});

test("detail: late event overlays cannot contaminate the next stock", async () => {
    const h = harness(); prepareDetail(h);
    const a = deferred();
    h.context.requestJson = async (url) => detail(url.includes("/A/") ? "A" : "B");
    h.context.loadChartOverlays = (symbol) => symbol === "A" ? a.promise : Promise.resolve([event({ event_id: "B-fill" })]);
    const pending = h.context.showDetail({ symbol: "A", name: "A" });
    await Promise.resolve();
    await h.context.showDetail({ symbol: "B", name: "B" });
    a.resolve([event({ event_id: "A-fill" })]); await pending;
    assert.ok(!JSON.stringify(h.evaluate("currentDetailChartData.markers")).includes("A-fill"));
});

test("detail: closing invalidates requests and restores the source list", async () => {
    const h = harness(); prepareDetail(h);
    h.window.location.hash = "#ledgers/ai"; h.window.scrollY = 217;
    h.context.requestJson = async () => detail("A");
    await h.context.showDetail({ symbol: "A", name: "A" }, { source: "ai_positions", items: [{ symbol: "A" }] });
    h.context.closeModal();
    assert.equal(h.window.location.hash, "#ledgers/ai");
    assert.equal(h.window.scrollY, 217);
    const late = deferred(); h.context.requestJson = () => late.promise;
    const pending = h.context.showDetail({ symbol: "B", name: "B" });
    h.context.closeModal();
    late.resolve(detail("B")); await pending;
    assert.equal(h.evaluate("currentDetailChartData"), null);
});

test("detail: next stock stays in the explicitly selected source", async () => {
    const h = harness(); prepareDetail(h);
    h.evaluate('currentCandidates = [{symbol:"unrelated"}]; currentTodayRecommendations = [{symbol:"other"}];');
    const requests = [];
    h.context.requestJson = async (url) => { requests.push(url); return detail(url.includes("/B/") ? "B" : "A"); };
    await h.context.showDetail({ symbol: "A", name: "A" }, { source: "ai_positions", items: [{ symbol: "A" }, { symbol: "B" }] });
    assert.equal(h.node("workbenchNext").disabled, false);
    await h.node("workbenchNext").onclick();
    assert.ok(requests.some((url) => url.includes("/B/detail")));
    assert.equal(h.evaluate("currentWorkbenchContext.source"), "ai_positions");
});

test("detail: search opens the same local workbench with no implicit update", async () => {
    const h = harness(); prepareDetail(h);
    const requests = [];
    h.context.requestJson = async (url) => { requests.push(url); return detail("A"); };
    h.context.renderRealtimeDetailContent = () => "legacy-realtime";
    await h.context.showRealtimeStockDetail("A", "A");
    assert.ok(requests.every((url) => !/refresh_daily=1|refresh_quote=1|network=1/.test(url)));
    assert.ok(requests.some((url) => url.startsWith("/api/stock/A/detail")));
    assert.equal(h.node("modalBody").innerHTML, "detail:A");
});

test("detail: ordinary viewing uses embedded news and never auto-refreshes it", async () => {
    const h = harness(); prepareDetail(h);
    let refreshes = 0;
    h.context.refreshNewsForSymbol = () => { refreshes += 1; };
    h.context.requestJson = async () => detail("A");
    await h.context.showDetail({ symbol: "A", name: "A" });
    assert.equal(refreshes, 0);
});

test("detail: a restored ledger waits for its own list instead of borrowing candidates", async () => {
    const h = harness(); prepareDetail(h);
    h.evaluate('currentCandidates = [{symbol:"A"},{symbol:"B"}]; currentTodayRecommendations = currentCandidates;');
    const requests = [];
    h.context.requestJson = async (url) => { requests.push(url); return detail(url.includes("/C/") ? "C" : "A"); };
    await h.context.showDetail({ symbol: "A", name: "A" }, { source: "ai_positions", updateUrl: false });
    assert.equal(h.node("workbenchNext").disabled, true, "unknown ledger list must not use research candidates");
    h.context.renderAiTraderPositions([{ symbol: "A" }, { symbol: "C" }]);
    assert.equal(h.node("workbenchNext").disabled, false);
    await h.node("workbenchNext").onclick();
    assert.ok(requests.some((url) => url.includes("/C/detail")));
    assert.ok(!requests.some((url) => url.includes("/B/detail")));
});

test("detail: a hash-restored stock title uses the returned name", async () => {
    const h = harness(); prepareDetail(h);
    h.context.requestJson = async () => ({ ...detail("A"), basic_info: { symbol: "A", name: "source name" } });
    await h.context.showDetail({ symbol: "A", name: "A" });
    assert.equal(h.node("modalTitle").textContent, "source name A");
});

test("chart: merged events retain the actual price and original evidence", () => {
    const h = harness(); const original = event();
    const merged = h.context.mergeChartMarkers([], [original]);
    assert.equal(merged[0].price, 12.34);
    for (const key of ["event_id", "source", "event_time", "price_basis", "evidence"]) {
        assert.deepEqual(merged[0][key], original[key]);
    }
});

test("chart: separate fills on the same date and in two ledgers survive", () => {
    const h = harness();
    const rows = [event(), event({ event_id: "fill-2", price: 12.5 }), event({ event_id: "fill-3", ledger: "策略账" })];
    const markers = h.context.normalizeMarkers({ price_basis: "qfq", markers: rows }, ["2026-09-04"]);
    assert.equal(markers.length, 3);
});

test("chart: raw and unknown events cannot be plotted on qfq bars", () => {
    const h = harness();
    const rows = [event({ price_basis: "raw" }), event({ event_id: "other", price_basis: "unknown" })];
    const markers = h.context.normalizeMarkers({ price_basis: "qfq", markers: rows }, ["2026-09-04"]);
    assert.equal(markers.length, 0);
    assert.equal(h.context.normalizeMarkers({ price_basis: "qfq", markers: [] }, ["2026-09-04"]).length, 0);
});

test("chart: missing source, time, price or out-of-range date remains unplotted", () => {
    const h = harness();
    const rows = [event({ source: "" }), event({ event_time: "" }), event({ price: null }), event({ date: "2026-09-08" })];
    assert.equal(h.context.normalizeMarkers({ price_basis: "qfq", markers: rows }, ["2026-09-04"]).length, 0);
});

test("chart: OHLC gaps keep the same date index instead of shifting later prices", () => {
    const h = harness(); h.context.echarts = {};
    const rows = [{ date: "2026-09-03", open: 1, close: 2, low: 1, high: 2 }, { date: "2026-09-04", open: null, close: 3, low: 2, high: 3 }];
    let values;
    h.context.renderPriceChart = (_, dates, ohlc) => { values = { dates, ohlc }; };
    for (const fn of ["renderVolumeChart", "renderMacdChart", "renderKdjChart", "renderRsiChart"]) h.context[fn] = () => {};
    h.context.renderChart({ kline_data: rows });
    assert.equal(values.ohlc.length, values.dates.length);
    assert.equal(values.ohlc[1], null);
});

test("chart: a stock code is metadata, never an ECharts symbol-shape override", () => {
    const h = harness();
    const series = h.context.markerSeries("price", [event({ symbol: "990001" })], ["2026-09-04"], [{ close: 13 }], (row) => row.close);
    const point = series[0].data[0];
    assert.notEqual(point.symbol, "990001");
    assert.equal(point.stock_symbol, "990001");
    assert.equal(series[0].symbol, "triangle");
});

test("review: descriptive reasons cannot manufacture maturity or correctness", () => {
    const h = harness();
    assert.equal(h.context.reviewBucketOf({ reason: "未到期；此前偏离", kind: "rule_signal" }), "other");
    assert.equal(h.context.reviewBucketOf({ reason: "缺证据", kind: "paper_fill" }), "fill");
    assert.equal(h.context.reviewBucketOf({ review_status: "due_missing_evidence" }), "evidence_gap");
    assert.equal(h.context.reviewBucketOf({ review_status: "missing_evidence" }), "data_gap");
});

test("review: failed APIs are unavailable rather than successful zero records", async () => {
    const h = harness(); h.context.requestJson = async () => { throw new Error("fixture offline"); };
    await h.context.loadDecisionHistory();
    assert.match(h.node("decisionHistoryPanel").innerHTML, /读取失败|不可用/);
    assert.doesNotMatch(h.node("decisionHistoryNote").textContent, /^共 0 条/);
});

test("review: partial reads disclose source failure and the incomplete C scope", async () => {
    const h = harness();
    h.context.requestJson = async (url) => {
        if (url.includes("ai-trader")) throw new Error("AI fixture unavailable");
        return { as_of: "2026-09-04", watchlist: [], events: [] };
    };
    await h.context.loadDecisionHistory();
    const rendered = h.node("decisionHistoryPanel").innerHTML + h.node("decisionHistoryNote").textContent;
    assert.match(rendered, /AI.*(?:失败|不可用)/);
    assert.match(rendered, /C 线.*(?:未接入|不包含)/);
    assert.match(rendered, /来源范围/);
});

test("review: repeated renders do not accumulate stock click listeners", () => {
    const h = harness(); let opened = 0;
    h.context.showDetail = () => { opened += 1; };
    const rows = [{ symbol: "A", name: "A", bucket: "mismatch", date: "2026-09-04" }];
    for (let count = 0; count < 3; count += 1) h.context.renderDecisionHistory({ as_of: "2026-09-04" }, null, rows);
    const row = new NodeStub(); row.dataset.reviewSymbol = "A"; row.dataset.reviewIndex = "0";
    h.node("decisionHistoryPanel").dispatch("click", { target: row });
    assert.equal(opened, 1);
});

test("ledger: missing numbers stay missing while genuine zero remains zero", async () => {
    const h = harness();
    for (const missing of [null, undefined, "", "  ", false, [], {}, NaN, Infinity]) assert.equal(h.context.numericValue(missing), null);
    assert.equal(h.context.numericValue(0), 0); assert.equal(h.context.numericValue("0"), 0);
    h.context.requestJson = async () => ({ degraded: true, warnings: ["ledger_missing"], account: { equity: null, positions_count: null } });
    await h.context.loadHomeAiLedger();
    assert.equal(h.node("kpiAiValue").textContent, "—");
    assert.match(h.node("kpiAiSub").textContent, /缺失|不可读|降级/);
    h.context.renderAiTraderAccount({ equity: null, cash: null, positions_count: null });
    assert.doesNotMatch(h.node("aiTraderAccountPanel").innerHTML, /0\.00|>0</);
});

test("view: displaying candidate and paper reviews does not call the model", async () => {
    const h = harness(); let writes = 0;
    h.context.postJson = async () => { writes += 1; return { answer: "must not be requested" }; };
    await h.context.loadLLMSelectionAnalysis([{ symbol: "A", name: "fixture" }]);
    await h.context.loadLLMDailyReview({ updatedAt: "2026-09-04" }, [{ symbol: "A", action: "buy", date: "2026-09-04" }]);
    assert.equal(writes, 0);
    assert.match(h.node("llmAnalysisContent").innerHTML, /发送|研究室/);
});

module.exports = { harness, event };
