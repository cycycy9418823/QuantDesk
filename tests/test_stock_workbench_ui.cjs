"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app/static/js/stock_workbench.js"), "utf8");

function view(symbol = "600000", overrides = {}) {
    return {
        symbol, network_enabled: false, refresh_allowed: true, market_active: true,
        quote: { price: 10.6, quote_time: "2026-10-23 10:00:00", source: "fixture", pct_chg: 0 },
        quote_evidence: { price_valid: true, usable_latest_quote: true, comparable_to_raw_daily: true,
            quote_timestamp: "2026-10-23T10:00:00+08:00", label: "盘中报价", source: "fixture" },
        reference: { available: true, label: "纸面 · 未验证", as_of: "2026-10-23",
            current_assessment: symbol + " 等待收盘确认", validity_text: "适用至2026-10-26收盘",
            reasons: ["fixture reason"], levels: [
                { kind: "buy", label: "买入确认参考位", price: 10.5, condition: "量能和收盘确认" },
                { kind: "stop", label: "止损 / 退出观察线", price: 9 },
                { kind: "profit", label: "止盈观察位", price: 11 },
            ] },
        ...overrides,
    };
}
function harness(get = async () => view(), post = async () => ({ ok: true, view: view() })) {
    const timers = new Map(); let nextId = 0; let hidden = false;
    const button = {};
    const root = { dataset: { stockSymbol: "600000" }, innerHTML: "", querySelector: () => button };
    const window = { setTimeout(fn, delay) { const id = ++nextId; timers.set(id, { fn, delay }); return id; },
        clearTimeout(id) { timers.delete(id); } };
    vm.runInNewContext(source, { window });
    const toolkit = window.QuantDeskStockWorkbench;
    const controller = toolkit.createController({ get, post, getNode: () => root, isHidden: () => hidden });
    return { root, button, timers, controller, toolkit, setHidden: (value) => { hidden = value; } };
}

test("initial cache view does not call a quote source or create a timer", async () => {
    const calls = [];
    const h = harness(async (url) => { calls.push(["get", url]); return view(); },
        async (url) => { calls.push(["post", url]); return {}; });
    await h.controller.start("600000");
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "get");
    assert.equal(h.timers.size, 0);
    assert.match(h.root.innerHTML, /买入确认参考位/);
    assert.match(h.root.innerHTML, /10\.50 元/);
    assert.match(h.root.innerHTML, /纸面 · 未验证/);
});

test("owner toggle sends explicit intent only for the selected symbol", async () => {
    const calls = [];
    const h = harness(async () => view(), async (url, body) => { calls.push({ url, body }); return { ok: true, view: view() }; });
    await h.controller.start("600000");
    await h.button.onclick();
    assert.deepEqual(calls.map((call) => call.url), ["/api/stock/600000/quote-refresh"]);
    assert.equal(calls[0].body.network, true);
    assert.equal(h.timers.size, 1);
    assert.equal([...h.timers.values()][0].delay, 15000);
    await h.button.onclick();
    assert.equal(h.timers.size, 0);
});

test("late result for a previous stock cannot repaint the current stock", async () => {
    let release;
    const old = new Promise((resolve) => { release = resolve; });
    const h = harness(async (url) => url.includes("600000") ? old : view("000001"));
    const first = h.controller.start("600000");
    h.root.dataset.stockSymbol = "000001";
    await h.controller.start("000001");
    const current = h.root.innerHTML;
    release(view("600000"));
    await first;
    assert.equal(h.root.innerHTML, current);
    assert.match(current, /000001 等待收盘确认/);
});

test("closing clears timers and ignores an outstanding response", async () => {
    let release;
    const h = harness(() => new Promise((resolve) => { release = resolve; }));
    const pending = h.controller.start("600000");
    h.controller.stop();
    release(view());
    await pending;
    assert.equal(h.controller.value().state, null);
    assert.equal(h.root.innerHTML, "");
    assert.equal(h.timers.size, 0);
});

test("hidden page never invokes a provider and resumes when visible", async () => {
    let posts = 0;
    const h = harness(async () => view("600000", { network_enabled: true }),
        async () => { posts += 1; return { ok: true, view: view() }; });
    h.setHidden(true);
    await h.controller.start("600000");
    assert.equal(posts, 0);
    assert.equal(h.timers.size, 0);
    h.setHidden(false);
    await h.controller.resume();
    assert.equal(posts, 1);
    assert.equal(h.timers.size, 1);
});

test("market closed waits without polling the provider", async () => {
    let posts = 0;
    const h = harness(async () => view("600000", { network_enabled: true, market_active: false }),
        async () => { posts += 1; return {}; });
    await h.controller.start("600000");
    assert.equal(posts, 0);
    assert.equal([...h.timers.values()][0].delay, 60000);
    assert.match(h.root.innerHTML, /当前休市/);
});

test("provider failure retains the old clock and labels the old result", async () => {
    const h = harness(async () => view(), async () => ({ ok: false, message_for_ui: "fixture failure", view: view() }));
    await h.controller.start("600000");
    await h.button.onclick();
    assert.match(h.root.innerHTML, /2026-10-23T10:00:00/);
    assert.match(h.root.innerHTML, /更新失败 · 上次结果/);
    assert.match(h.root.innerHTML, /fixture failure/);
});

test("unverified quote and missing historical reference remain visible", () => {
    const h = harness();
    const html = h.toolkit.render(view("600000", {
        quote_evidence: { price_valid: false, usable_latest_quote: false, label: "报价日期未核实" },
        reference: { available: false, message: "日线不足60条" },
    }));
    assert.match(html, /报价日期未核实/);
    assert.match(html, /日线不足60条/);
    assert.doesNotMatch(html, /NaN|undefined|null/);
});

test("strings from providers and algorithm reasons are escaped", () => {
    const h = harness();
    const data = view();
    data.quote.source = "<img src=x onerror=alert(1)>";
    data.quote_evidence.source = "";
    data.reference.reasons = ["<script>bad</script>"];
    const html = h.toolkit.render(data);
    assert.doesNotMatch(html, /<img|<script>/);
    assert.match(html, /&lt;script&gt;/);
});

test("invalid code does not create an active selection or send a request", async () => {
    let calls = 0;
    const h = harness(async () => { calls += 1; return view(); });
    await h.controller.start("../bad");
    assert.equal(calls, 0);
    assert.equal(h.controller.value().symbol, "");
});

test("read-only preview disables quote controls", () => {
    const h = harness();
    assert.match(h.toolkit.render(view("600000", { refresh_allowed: false })), /data-stock-live disabled/);
});

test("hiding an open stock cancels the pending timer immediately", async () => {
    const h = harness(async () => view("600000", { network_enabled: true }));
    await h.controller.start("600000");
    assert.equal(h.timers.size, 1);
    h.setHidden(true);
    h.controller.suspend();
    assert.equal(h.timers.size, 0);
});

test("calendar and excluded-daily warnings are shown alongside prices", () => {
    const h = harness();
    const data = view("600000", { calendar_message: "交易日历未完整核实" });
    data.reference.warnings = ["已排除1条未完成或未来日期日线"];
    const html = h.toolkit.render(data);
    assert.match(html, /交易日历未完整核实/);
    assert.match(html, /已排除1条未完成或未来日期日线/);
    assert.match(html, /10\.50 元/);
});

test("a short-history detail error is explained without raw JSON or commands", () => {
    const h = harness();
    const message = h.toolkit.detailErrorMessage(JSON.stringify({ error: "本地数据不足",
        message: "股票 920285 的历史日线不足20条，无法生成详情。请运行 python scripts/qd.py" }));
    assert.match(message, /本地日线不足20条/);
    assert.match(message, /报价和算法参考状态仍可单独查看/);
    assert.doesNotMatch(message, /python|scripts|\{|&quot;|\\u/);
});

test("an unknown detail failure is not mislabeled as short history", () => {
    const h = harness();
    for (const error of ["connection failed", JSON.stringify({ error: "fixture failure" })]) {
        const message = h.toolkit.detailErrorMessage(error);
        assert.match(message, /详情暂时读取失败/);
        assert.doesNotMatch(message, /不足20条/);
    }
});
