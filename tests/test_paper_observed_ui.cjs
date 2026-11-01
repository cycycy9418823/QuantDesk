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
