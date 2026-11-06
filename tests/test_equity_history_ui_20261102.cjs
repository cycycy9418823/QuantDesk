"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/daily_research.js"), "utf8");
const template = fs.readFileSync(path.join(__dirname, "../app/templates/daily_research.html"), "utf8");

function harness() {
    const nodes = new Map(["kpiPaperValue", "kpiPaperSub", "homeEquityEvidence", "homeEquityChart", "homeEquityChartEmpty"].map((id) => [id, {}]));
    const chart = { options: null, isDisposed: () => false, resize() {}, setOption(value) { this.options = value; } };
    const window = { addEventListener() {}, setTimeout() {}, clearTimeout() {} };
    const context = { window, console, AbortController,
        document: { addEventListener() {}, getElementById: (id) => nodes.get(id) || null,
            querySelector: () => null, querySelectorAll: () => [] },
        localStorage: { getItem: () => null, setItem() {} },
        echarts: { init: () => chart }, fetch: () => { throw new Error("no network in UI test"); } };
    vm.runInNewContext(source + "\nwindow.testEquity = { renderHomeEquity, productIntegrityHtml };", context);
    return { api: window.testEquity, nodes, chart };
}

function payload() {
    return { ok: true, account: { total_equity: 120, total_return_pct: 20, valuation_date: "2026-10-23" },
        return_series: { normalization: "common_first_recorded_equity_and_index", benchmark_label: "沪深300",
            message_for_ui: "两条曲线从共同首日归零；23 条缺少估值日期，1 天缺记录；纸面 · 未验证。",
            items: [
                { date: "2026-10-21", paper_return_pct: 0, benchmark_return_pct: 0 },
                { date: "2026-10-22", paper_return_pct: null, benchmark_return_pct: 5 },
                { date: "2026-10-23", paper_return_pct: 9.090909, benchmark_return_pct: 10 },
            ] } };
}

test("equity chart keeps date gaps and distinguishes interval changes from account lifetime return", () => {
    const h = harness(); h.api.renderHomeEquity(payload());
    assert.equal(h.nodes.get("homeEquityChart").hidden, false);
    assert.match(h.nodes.get("kpiPaperSub").textContent, /累计 \+20/);
    assert.match(h.nodes.get("homeEquityEvidence").textContent, /23 条缺少估值日期/);
    assert.deepEqual(Array.from(h.chart.options.xAxis.data), ["2026-10-21", "2026-10-22", "2026-10-23"]);
    const [paper, benchmark] = h.chart.options.series;
    assert.deepEqual(Array.from(paper.data), [0, null, 9.090909]);
    assert.equal(paper.name, "已记录权益变化");
    assert.equal(benchmark.name, "沪深300点位变化");
    assert.equal(paper.connectNulls, false);
    assert.equal(paper.smooth, false);
    assert.equal(paper.showSymbol, true);
    assert.equal(paper.showAllSymbol, true);
    assert.equal(benchmark.showSymbol, true);
});

test("a legacy cumulative-return payload cannot be relabelled as a common-period comparison", () => {
    const h = harness(), data = payload(); delete data.return_series.normalization;
    h.api.renderHomeEquity(data);
    assert.equal(h.chart.options, null);
    assert.equal(h.nodes.get("homeEquityChart").hidden, true);
});

test("unavailable evidence clears old chart context and missing points are not zero", () => {
    const h = harness(), data = payload();
    data.return_series.items[1].paper_return_pct = "数据缺失";
    h.api.renderHomeEquity(data);
    assert.equal(h.chart.options.series[0].data[1], null);
    h.api.renderHomeEquity(null);
    assert.equal(h.nodes.get("homeEquityChart").hidden, true);
    assert.match(h.nodes.get("homeEquityEvidence").textContent, /暂不可读/);
});

test("coverage displays confirmed observation count separately from undated raw records", () => {
    const h = harness();
    const html = h.api.productIntegrityHtml({ report: { history: { unique_dates: 9, row_count: 32,
        first_date: "2026-10-13", undated_rows: 23, invalid_amount_rows: 0,
        missing_dates: ["2026-10-15"], duplicate_dates: [], continuous_statistics_eligible: false } } });
    assert.match(html, /权益记录（明确估值日）/);
    assert.match(html, /9个日期 \/ 32条原记录/);
    assert.match(html, /无明确估值日期 23 条/);
    assert.match(html, /完整连续收益统计仍不可用/);
    assert.match(template, /id="homeEquityEvidence"/);
});
