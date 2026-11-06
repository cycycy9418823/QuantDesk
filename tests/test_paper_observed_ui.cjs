"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../app/static/js/paper_observed.js"), "utf8"), { window });
const ui = window.QuantDeskPaperObserved;
const payload = () => ({ ok: true, report: {
    points: [{ date: "2026-10-13", equity: 1000 }, { date: "2026-10-27", equity: 900 }],
    portfolio: { valuation_date: "2026-10-27", total_equity: 900, identity_difference: 3.03 },
    undated_count: 23, comparisons: {
        hs300: { label: "沪深300", available: true, start: "2026-10-13", end: "2026-10-27", recorded_equity_change_pct: -10, index_change_pct: -5 },
        zz500: { label: "中证500", available: false, reason: "endpoint_missing" },
    }, calendar: { status: "available", missing_dates: ["2026-10-15"] },
} });

test("same-date comparison shows missing records, identity difference and validation limit", () => {
    const html = ui.render(payload());
    for (const value of ["-10.00%", "-5.00%", "+3.03", "23 条", "2026-10-15", "指数缺少相同起止日", "尚不能作为", "纸面 · 未验证"]) assert.ok(html.includes(value), value);
    assert.doesNotMatch(html, /胜率|跑赢所有|保证盈利/);
    assert.match(html, /class="paper-table-wrap"/);
    assert.match(html, /class="compact-table"/);
});

test("conflicting history blocks headline change; untrusted labels are escaped", () => {
    const p = payload(); p.report.invalid_count = 1;
    p.report.comparisons = { bad: { available: false, label: '<img src=x onerror="attack">', reason: "history_conflicting_or_invalid" } };
    const html = ui.render(p);
    assert.match(html, /暂不可计算/);
    assert.doesNotMatch(html, /-10.00%|<img/);
    assert.match(html, /&lt;img/);
});

test("missing values stay missing instead of becoming zero", () => {
    const p = payload(); p.report.portfolio = {};
    assert.match(ui.render(p), /对账差额 未记录/);
    assert.match(ui.render({ ok: false }), /记录暂不可读/);
});

test("late request cannot overwrite a newer result and load only uses read endpoint", async () => {
    let release;
    const old = new Promise((resolve) => { release = resolve; });
    const panel = { innerHTML: "", querySelector: () => null };
    const calls = [];
    const first = ui.load((url) => { calls.push(url); return old; }, () => panel);
    await ui.load(async (url) => { calls.push(url); return { ok: false, message_for_ui: "新结果" }; }, () => panel);
    release(payload()); await first;
    assert.match(panel.innerHTML, /新结果/);
    assert.deepEqual(calls, ["/api/paper/observed-performance", "/api/paper/observed-performance"]);
});

function dailyUI() {
    const nodes = new Map();
    const node = (id) => {
        if (!nodes.has(id)) nodes.set(id, { innerHTML: "", textContent: "", querySelector: (q) => node(`${id} ${q}`) });
        return nodes.get(id);
    };
    const context = vm.createContext({ window: {}, document: {
        addEventListener() {}, getElementById: node, querySelector: node,
    }, console, URLSearchParams, AbortController, setTimeout, clearTimeout });
    // Load the actual page source. DOMContentLoaded remains uncalled: no reads,
    // quote refreshes, model requests or backend mutations are executed.
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../app/static/js/daily_research.js"), "utf8"), context);
    return { context, node, evaluate: (code) => vm.runInContext(code, context) };
}

test("paper rejection reasons are not reclassified as missing minutes", () => {
    const page = dailyUI();
    page.evaluate(`var actionRows = buildPaperActionRows([], {daily_actions: [
        {action: 'block', quantity: 0, symbol: '300438', price: 43.33, decision_date: '2026-11-06', reason: '现金不足以模拟买入100股'},
        {action: 'block', quantity: 0, symbol: '300686', price: 13.68, decision_date: '2026-11-06', reason: '涨停不买，拒绝模拟买入'},
    ]}); renderTradesTable(actionRows); renderDailyReport({valuationDate: '2026-11-06'}, actionRows);`);
    const html = page.node("paperTradesPanel").innerHTML;
    for (const word of ["2 条已拒绝", "现金不足", "涨停", "43.33", "13.68"]) assert.ok(html.includes(word), word);
    assert.doesNotMatch(html, /<td[^>]*>缺分钟线|待验证（11-06）/);
    assert.match(page.node("paperDailySellList").innerHTML, /已拒绝，未模拟成交/);
    assert.doesNotMatch(page.node("paperDailySellList").innerHTML, /缺分钟线|触发待验证/);
    assert.match(page.node("paperDailyReportDate").textContent, /0 条待验证，2 条已拒绝/);
});

test("actual minute gap stays pending and cannot become a fill", () => {
    const page = dailyUI();
    page.evaluate(`var actionRows = buildPaperActionRows([], {daily_actions: [
        {action: 'exit_watch', quantity: 0, symbol: '600000', fill_model: 'missing_intraday', decision_date: '2026-11-06', reason: '分钟证据缺失'},
    ]}); renderTradesTable(actionRows);`);
    const html = page.node("paperTradesPanel").innerHTML;
    assert.match(html, /1 条待验证 · 0 条已拒绝/);
    assert.match(html, /缺分钟线/);
    assert.match(html, /待验证，未模拟成交/);
});

test("explicit minute gap on a rejected action remains visible", () => {
    const page = dailyUI();
    page.evaluate(`var actionRows = buildPaperActionRows([], {daily_actions: [
        {action: 'block', quantity: 0, symbol: '600000', fill_model: 'missing_intraday', decision_date: '2026-11-06', reason: '分钟证据缺失，拒绝模拟成交'},
    ]}); renderTradesTable(actionRows);`);
    const html = page.node("paperTradesPanel").innerHTML;
    assert.match(html, /0 条待验证 · 1 条已拒绝/);
    assert.match(html, /<td[^>]*>缺分钟线/);
});

test("buy and sell labels appear in dated report while future plans stay separate", () => {
    const page = dailyUI();
    page.evaluate(`var actionRows = buildPaperActionRows([], {daily_actions: [
        {action: 'buy', action_label: '模拟买入', quantity: 100, symbol: '300085', price: 28.86, decision_date: '2026-11-06'},
        {action: 'sell', action_label: '模拟卖出', quantity: 100, symbol: '002376', price: 6.67, decision_date: '2026-11-06'},
        {action: 'exit_watch', quantity: 0, symbol: 'future', plan_date: '2026-11-09', fill_model: 'missing_intraday'},
    ]}); renderDailyReport({valuationDate: '2026-11-06'}, actionRows);`);
    assert.match(page.node("paperDailyBuyList").innerHTML, /300085/);
    assert.match(page.node("paperDailySellList").innerHTML, /002376/);
    assert.doesNotMatch(page.node("paperDailyBuyList").innerHTML + page.node("paperDailySellList").innerHTML, /future/);
    assert.match(page.node("paperDailyReportDate").textContent, /2026-11-06/);
});

test("unknown fill quantity remains unverified without invented minute gap; reasons are escaped", () => {
    const page = dailyUI();
    page.evaluate(`var actionRows = buildPaperActionRows([], {daily_actions: [
        {action: 'buy', quantity: null, symbol: '600000', price: 10, decision_date: '2026-11-06', reason: '<img src=x onerror=attack>'},
    ]}); renderTradesTable(actionRows);`);
    const html = page.node("paperTradesPanel").innerHTML;
    assert.match(html, /成交数量未核实/);
    assert.match(html, /&lt;img/);
    assert.doesNotMatch(html, /<img|<td[^>]*>缺分钟线/);
});
