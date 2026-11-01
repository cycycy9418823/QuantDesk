"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const daily = fs.readFileSync(path.join(__dirname, "../app/static/js/daily_research.js"), "utf8");
const stock = fs.readFileSync(path.join(__dirname, "../app/static/js/stock_workbench.js"), "utf8");
const template = fs.readFileSync(path.join(__dirname, "../app/templates/daily_research.html"), "utf8");

function harness(options = {}) {
    const nodes = new Map(), selectors = new Map(), timers = new Map(), storage = new Map();
    let id = 0, calls = [], impl = async () => ({ ok: true, json: async () => ({ ok: true }) });
    const window = {
        setTimeout(fn, delay) { const key = ++id; timers.set(key, { fn, delay }); return key; },
        clearTimeout(key) { timers.delete(key); }, addEventListener() {},
    };
    const context = {
        window, console, AbortController,
        document: { addEventListener() {}, getElementById: (key) => nodes.get(key) || null,
            querySelector: () => null,
            querySelectorAll: (key) => selectors.get(key) || [] },
        localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
        fetch(url, options) { calls.push({ url, options }); return impl(url, options); },
    };
    if (options.noAbortController) delete context.AbortController;
    if (options.noWindowTimers) { delete window.setTimeout; delete window.clearTimeout; }
    vm.runInNewContext(daily + `\nwindow.testApi = {requestJson, postJson, fetchAiTraderJson,
        runtimeMutationAllowed, assertRuntimeMutationAllowed, applyRuntimeCapabilities,
        buildPaperAccountSnapshot, renderPositionsTable, renderCandidateConfidencePanel,
        renderDataGapPanel, renderResearchSampleScope, renderHomeEquity, productIntegrityHtml,
        researchEvidenceHtml, weeklyLearningHtml, loadProductIntegrity, setupAiFundManagerLifecycle, loadDecisionHistory,
        marketEvidenceDates, renderMarketOverview, renderHomeKpiMarketBrain, candidateExposureHtml,
        candidateExposureSummaryHtml, candidateMethodNote, renderCandidateCard, renderStrategyRecommendationRow,
        candidateEvidenceDetails,
        historicalHoldingPeriodsHtml,
        runVisiblePageReads, visiblePageLoaders, loadVisiblePageData, syncStartupStatusPolling,
        prepareVisiblePagePlaceholders,
        startupReadState: () => ({ sequence: visiblePageReadSequence, taskId: startupRefreshTaskId, applied: startupRefreshApplied }),
        personalWatchlist, setPersonalWatch, paperReportDate, renderOwnerRuntimeSummary, dailyGapReviewHtml,
        loadCandidates, loadMarketOverview, RUNTIME_WRITE_SELECTOR, renderResearchBrief,
        retainReadFixtures: () => {lastCandidatesReadSucceeded = true; lastMarketReadSucceeded = true;}};`, context);
    return { api: window.testApi, window, nodes, selectors, timers, storage, calls,
        evaluate: (source) => vm.runInNewContext(source, context),
        fetch: (fn) => { impl = fn; } };
}

test("daily research brief labels its own date separately from live quote", () => {
    const h = harness();
    const html = h.api.renderResearchBrief({ available: true, data_date: "2026-10-28", current_state: "日线收盘 9.85", key_levels: {} });
    assert.match(html, /完整日线截至 2026-10-28/);
    assert.match(html, /盘中报价见上方/);
    assert.match(html, /日线观察/);
    assert.match(h.api.renderResearchBrief({ available: true }), /日期未核实/);
});

test("daily gap comparison links dated real rows and keeps unverified gaps visible", () => {
    const h = harness();
    const html = h.api.dailyGapReviewHtml({ available: true, target_date: "2026-10-30",
        reviewed_count: 3, review_total: 3, message: "1只待核实", by_symbol: {
            a: { symbol: "600621", name: "华鑫股份", status: "present", previously_flagged: true,
                data_date: "2026-10-30", close: 13.6, source: "sina_spot_close" },
            b: { symbol: "301139", status: "unresolved", data_date: "2026-10-27", close: null },
            c: { symbol: "002717", status: "verified_no_session", data_date: "2026-10-23", close: null },
        } });
    assert.match(html, /href="#watch\/600621"/);
    assert.match(html, /研究链曾标为缺失\/滞后/);
    assert.match(html, /2026-10-30 · 收盘 13\.60 元/);
    assert.match(html, /缺口待核实/);
    assert.match(html, /最后日线 2026-10-27/);
    assert.match(html, /已核实无交易/);
    assert.match(html, /无交易证据不等于存在当日K线/);
    assert.doesNotMatch(html, /收盘 0\.00 元/);
});

test("daily gap source errors and hostile labels cannot become a green result or HTML", () => {
    const h = harness();
    const unavailable = h.api.dailyGapReviewHtml({ available: false, message: "日期不一致" });
    assert.match(unavailable, /日期不一致/);
    assert.doesNotMatch(unavailable, /health-ok/);
    const html = h.api.dailyGapReviewHtml({ available: true, by_symbol: {
        x: { symbol: '600000\" onclick=\"bad()', name: "<script>bad()</script>", status: "unresolved" },
    } });
    assert.doesNotMatch(html, /<script>|href="#watch/);
    assert.match(html, /&lt;script&gt;/);
});

test("a hung fetch times out, clears single-flight and permits a fresh retry", async () => {
    const h = harness();
    h.fetch(() => new Promise(() => {}));
    const first = h.api.requestJson("/api/slow", { timeoutMs: 25 });
    assert.equal(h.calls.length, 1);
    [...h.timers.values()][0].fn();
    await assert.rejects(first, /读取超过/);
    await Promise.resolve();
    assert.equal(h.calls[0].options.signal.aborted, true);
    h.fetch(async () => ({ ok: true, json: async () => ({ answer: 42 }) }));
    assert.equal((await h.api.requestJson("/api/slow")).answer, 42);
    assert.equal(h.calls.length, 2);
    assert.equal(h.timers.size, 0);
});

test("shared GETs coalesce but an explicit subscriber can cancel", async () => {
    const h = harness(); let release;
    h.fetch(() => new Promise((resolve) => { release = resolve; }));
    const first = h.api.requestJson("/api/shared");
    assert.equal(h.api.requestJson("/api/shared"), first);
    release({ ok: true, json: async () => ({ answer: "shared" }) });
    await first;
    const abort = new AbortController();
    h.fetch(() => new Promise(() => {}));
    const second = h.api.requestJson("/api/cancel", { signal: abort.signal });
    abort.abort();
    await assert.rejects(second, /读取已取消/);
    assert.equal(h.timers.size, 0);
});

test("unknown or read-only mode rejects mutations before fetch; quote is a separate permission", async () => {
    const h = harness();
    await assert.rejects(h.api.postJson("/api/backtest/run"), /只读/);
    assert.equal(h.calls.length, 0);
    h.window.quantdeskRuntimeCapabilities = { read_only: true, mutations_enabled: false, selected_quote_refresh: true };
    assert.equal(h.api.runtimeMutationAllowed("/api/backtest/run"), false);
    assert.equal(h.api.runtimeMutationAllowed("/api/stock/600000/quote-refresh"), true);
    await h.api.postJson("/api/stock/600000/quote-refresh", { network: true });
    assert.equal(h.calls.length, 1);
    await assert.rejects(h.api.fetchAiTraderJson("/api/ai-trader/chat", { method: "POST" }), /只读/);
    assert.equal(h.calls.length, 1);
});

test("rendered write buttons stay disabled even after another handler re-enables them", () => {
    const h = harness();
    const button = { disabled: false, dataset: {}, title: "original", setAttribute() {}, removeAttribute() {} };
    h.selectors.set(h.api.RUNTIME_WRITE_SELECTOR, [button]);
    let paints = 0, current = "";
    h.nodes.set("runtimeModeNote", { get textContent() { return current; }, set textContent(value) { current = value; paints += 1; } });
    h.window.quantdeskRuntimeCapabilities = { read_only: true, mutations_enabled: false };
    h.api.applyRuntimeCapabilities();
    assert.equal(button.disabled, true);
    button.disabled = false;
    h.api.applyRuntimeCapabilities();
    assert.equal(button.disabled, true);
    assert.match(button.title, /只读研究模式/);
    assert.equal(paints, 1, "unchanged mode must not cause an endless MutationObserver repaint");
});

test("a failed candidate or market reload keeps the previous view and its date", async () => {
    const h = harness(); h.api.retainReadFixtures();
    const candidate = { innerHTML: "2026-10-23 original candidates" }, market = { innerHTML: "2026-10-23 original market" };
    const candidateNote = {}, marketNote = {};
    h.nodes.set("dailyFocusGrid", candidate); h.nodes.set("marketOverview", market);
    h.nodes.set("candidateDataNote", candidateNote); h.nodes.set("marketDataNote", marketNote);
    h.fetch(async () => { throw new Error("fixture disconnected"); });
    await h.api.loadCandidates(); await h.api.loadMarketOverview();
    assert.equal(candidate.innerHTML, "2026-10-23 original candidates");
    assert.equal(market.innerHTML, "2026-10-23 original market");
    assert.match(candidateNote.textContent, /保留上次候选及原批次日期/);
    assert.match(marketNote.textContent, /保留上次市场结果及其原证据日期/);
});

test("received files and strict quality counts remain distinct even when the latter is zero", () => {
    const h = harness(), panel = { innerHTML: "" }; h.nodes.set("projectRuntimeSummary", panel);
    h.api.renderOwnerRuntimeSummary({ snapshot: { received_slots: 48, collected_slots: 0, expected_slots: 48,
        strict_qualified_slots: 0, field_complete_slots: 0, valid_quote_ratio: 0.9990639, as_of: "2026-10-23" } });
    assert.match(panel.innerHTML, /已收到 48 \/ 48 份/);
    assert.match(panel.innerHTML, /严格时段合格 0/);
    assert.match(panel.innerHTML, /字段完整 0/);
    assert.match(panel.innerHTML, /有效报价 99\.91%/);
    h.api.renderOwnerRuntimeSummary({ snapshot: { received_slots: 0, collected_slots: 48, expected_slots: 48 } });
    assert.match(panel.innerHTML, /已收到 0 \/ 48 份/);
});

test("home account uses valuation date and leaves a mismatch visible", () => {
    const h = harness(), value = {}, note = {};
    h.nodes.set("kpiPaperValue", value); h.nodes.set("kpiPaperSub", note);
    h.api.renderHomeEquity({ account: { total_equity: 82515.64, total_return_pct: -17.48436,
        valuation_date: "2026-10-23", pnl_identity: { status: "mismatch" } }, return_series: { items: [] } });
    assert.match(note.textContent, /估值截至 2026-10-23/);
    assert.match(note.textContent, /尚未完全核对/);
});

test("unknown daily changes hide the amount and remain distinct from verified paper PnL", () => {
    const h = harness(), value = {}, note = {};
    h.nodes.set("kpiPaperValue", value); h.nodes.set("kpiPaperSub", note);
    const payload = { ok: true, account: { total_equity: 98000, total_return_pct: -2,
        position_count: 1, valuation_date: "2026-10-14", daily_pnl: -125.5, daily_pnl_status: "unknown" },
        return_series: { items: [] } };
    h.api.renderHomeEquity(payload);
    assert.match(note.textContent, /估值截至 2026-10-14/);
    assert.match(note.textContent, /累计 -2/);
    assert.match(note.textContent, /估值与当日变化尚未完全核对/);
    assert.doesNotMatch(note.textContent, /盈亏|-125\.5/);
    payload.account.daily_pnl_status = "verified";
    h.api.renderHomeEquity(payload);
    assert.match(note.textContent, /该日纸面盈亏 ¥-125\.5/);
    assert.match(note.textContent, /尚未完全核对/);
    assert.equal(h.calls.length, 0);
});

test("cost shown for PnL uses entry_price and preserves the different recorded average", () => {
    const h = harness();
    const source = { auto_paper_portfolio: { summary: { total_equity: 82515.64, valuation_date: "2026-10-23" },
        positions: [{ symbol: "600000", entry_price: 9.4047, avg_cost: 9.3186, quantity: 500,
            latest_price: 9.53, latest_date: "2026-10-23", unrealized_pnl: 62.65 }] } };
    const before = JSON.stringify(source);
    const account = h.api.buildPaperAccountSnapshot(source, []);
    assert.equal(account.positions[0].entry_price, 9.4047);
    assert.equal(account.positions[0].reported_avg_cost, 9.3186);
    assert.equal(account.positions[0].unrealized_pnl, 62.65);
    assert.equal(JSON.stringify(source), before);
    const panel = { innerHTML: "", querySelectorAll: () => [] }; h.nodes.set("paperPositionsPanel", panel);
    h.api.renderPositionsTable(account.positions, account);
    assert.match(panel.innerHTML, /9\.4047/);
    assert.match(panel.innerHTML, /原记录均价 9\.3186/);
    assert.match(panel.innerHTML, /估值 2026-10-23/);
});

test("old watchlist price and elapsed days show the original cutoff date", () => {
    const h = harness(), panel = { innerHTML: "", querySelectorAll: () => [] }; h.nodes.set("paperPositionsPanel", panel);
    h.api.renderPositionsTable([], { portfolioPositions: [], watchPositions: [{ symbol: "601398",
        initial_reference_price: 7.37, latest_reference_price: 7.37, latest_reference_date: "2026-07-08",
        elapsed_trading_days: 0, row_kind: "watchlist" }] });
    assert.match(panel.innerHTML, /截至 2026-07-08/);
    assert.match(panel.innerHTML, /行情 2026-07-08/);
    assert.equal(h.api.paperReportDate([], {}), "日期未核实");
});

test("partial candidates are not presented as fully reliable or as zero discounted", () => {
    const h = harness(), panel = { innerHTML: "" };
    h.selectors.set("[data-candidate-confidence-panel]", [panel]);
    h.api.renderCandidateConfidencePanel({ candidate_count: 18, usable_count: 18, degraded_count: 0,
        batch_date: "2026-10-23", status_groups: [{ status: "research_ready", count: 0 },
            { status: "partial", count: 18 }, { status: "degraded", count: 0 }, { status: "missing", count: 0 }] });
    assert.match(panel.innerHTML, /高可信资料 0/);
    assert.match(panel.innerHTML, /部分可用 18/);
    assert.doesNotMatch(panel.innerHTML, /可参考 18|需打折 0/);
    assert.match(panel.innerHTML, /本面板不计算胜率；资料完整度不能证明投资优势/);
});

test("missing amount gap count remains unknown and retains the batch scope", () => {
    const h = harness(), panel = { innerHTML: "" }; h.nodes.set("candidateGapPanel", panel);
    h.api.renderDataGapPanel({ batch_date: "2026-10-23", candidate_count: 18 });
    assert.match(panel.innerHTML, /成交额\/量能字段缺口：未核实/);
    assert.match(panel.innerHTML, /不是全A或持仓分钟覆盖/);
});

test("synthetic and unknown research evidence cannot become a verified return claim", () => {
    const h = harness();
    const html = h.api.renderResearchSampleScope({ synthetic_test_data: true, symbol_count: 2,
        metrics: { number_of_trades: 0 }, strategy: "fixture" });
    assert.match(html, /合成演示数据/);
    assert.match(html, /样本外验证未建立|样本外验证状态未知/);
    assert.match(html, /0笔/);
    assert.match(html, /不等于已验证盈利优势/);
});

test("integrity view preserves the real difference, missing history and unsafe strings", () => {
    const h = harness();
    const html = h.api.productIntegrityHtml({ as_of: "2026-10-23", report: {
        pnl_identity: { status: "mismatch", difference: 3.03 },
        history: { unique_dates: 28, row_count: 29, missing_dates: ["2026-10-21"], duplicate_dates: ["2026-09-10"] },
        capital_cost_bridge: { message: "<script>bad</script>" },
    } });
    assert.match(html, /3\.03元/);
    assert.match(html, /尚未核对/);
    assert.match(html, /2026-10-21/);
    assert.match(html, /&lt;script&gt;/);
    assert.doesNotMatch(html, /<script>/);
    const unknown = h.api.productIntegrityHtml({ report: {} });
    assert.match(unknown, /未核实个日期/);
    assert.doesNotMatch(unknown, /0个日期|已按现有证据核对/);
});

test("personal watchlist stays local, rejects junk and never sends a mutation", () => {
    const h = harness();
    assert.equal(h.api.setPersonalWatch("600000"), true);
    assert.equal(h.api.setPersonalWatch("600000"), true);
    assert.equal(h.api.setPersonalWatch("<script>"), false);
    assert.deepEqual(Array.from(h.api.personalWatchlist()), ["600000"]);
    assert.equal(h.api.setPersonalWatch("600000", false), true);
    assert.equal(h.api.personalWatchlist().length, 0);
    assert.equal(h.calls.length, 0);
    h.storage.set("quantdesk_personal_watchlist_v1", '["601398", "<img>", null]');
    assert.deepEqual(Array.from(h.api.personalWatchlist()), ["601398"]);
});

test("stock view explains insufficient economic space and unchecked events without predicting returns", () => {
    const window = {}; vm.runInNewContext(stock, { window });
    const html = window.QuantDeskStockWorkbench.render({ symbol: "600000", refresh_allowed: false,
        quote_evidence: {}, reference: { available: true, levels: [], rule_version: "fixture",
            trade_economics: { minimum_quantity: 100, gross_observation_space: 1, loss_to_exit_line: 51,
                estimated_roundtrip_cost_at_observation: 10, observation_space_below_cost: true,
                minimum_lot_affordable: false },
            event_checks: [{ label: "重大公告", status: "not_checked", checked_at: null, source: null }] } });
    assert.match(html, /不足以覆盖估计交易成本/);
    assert.match(html, /当前纸面账户现金不足/);
    assert.match(html, /1\.00 元/);
    assert.match(html, /重大公告：未检查/);
    assert.match(html, /历史波动区间不是预测置信区间/);
});

test("stock controller aborts the previous read and cannot refresh without capability", async () => {
    const window = { setTimeout() {}, clearTimeout() {} };
    vm.runInNewContext(stock, { window, AbortController });
    let signal, posts = 0;
    const root = { dataset: { stockSymbol: "600000" }, innerHTML: "", querySelector: () => ({}) };
    const controller = window.QuantDeskStockWorkbench.createController({ getNode: () => root,
        get: async (_, options) => { signal = options.signal; return { symbol: "600000", network_enabled: true,
            market_active: true, refresh_allowed: true }; }, post: async () => { posts += 1; }, canRefresh: () => false });
    await controller.start("600000");
    assert.equal(posts, 0);
    controller.stop();
    assert.equal(signal.aborted, true);
});

test("research evidence uses the actual summary fields and keeps process success separate", () => {
    const h = harness();
    const html = h.api.productIntegrityHtml({ report: {}, research_evidence: {
        backtest: { business_date: "2026-10-23", rule_identity: { rule_id: "current-rule", version: "v2" },
            validation_message: "旧样本已完成，当前规则未验证", process_status: "completed",
            current_rule_validation: "not_validated" },
        walk_forward: { business_date: "2026-10-22", rule_identity: "walk-rule-v1",
            validation_message: "缺少样本外区间", process_status: "partial",
            current_rule_validation: { status: "missing", message: "样本外证据缺失" } },
    } });
    assert.match(html, /业务日 2026-10-23 · 规则 current-rule · v2/);
    assert.match(html, /旧样本已完成，当前规则未验证/);
    assert.match(html, /流程状态 completed；当前规则验证 not_validated/);
    assert.match(html, /样本外证据缺失/);
    assert.match(html, /流程完成不代表投资优势成立/);
    assert.doesNotMatch(html, /\[object Object\]/);
});

test("unknown or unsafe research summary fields cannot become valid evidence", () => {
    const h = harness();
    const html = h.api.researchEvidenceHtml({ backtest: {
        business_date: {}, rule_identity: { rule_id: {}, version: "<img src=x>" },
        validation_message: "<script>claimed success</script>", process_status: {},
        current_rule_validation: { status: {} },
    } });
    assert.match(html, /业务日 未记录/);
    assert.match(html, /流程状态 未记录；当前规则验证 未建立/);
    assert.match(html, /&lt;script&gt;claimed success&lt;\/script&gt;/);
    assert.match(html, /&lt;img src=x&gt;/);
    assert.doesNotMatch(html, /<script>|<img |\[object Object\]/);
});

function weeklyFixture() {
    return { summary: "原记录2笔平仓，持有期字段存在矛盾。盈亏不等于判断对错。",
        checks: ["先核对原证伪条件"], weekly_review: { as_of: "2026-10-23", status: "partial",
            undated_or_future_records: [{}], weeks: [{ week_id: "2026-W43", week_start: "2026-10-19", week_end: "2026-10-25",
                closed_trade_count: 2, realized_pnl: { amount: -12.34, known_count: 1, total_count: 2, complete: false },
                recorded_exit_fees: { amount: 5, known_count: 1, total_count: 2, complete: false },
                holding_period: { zero_day_conflicts: 1, missing_or_invalid_dates: 0, calendar_unverified_count: 1, negative_recorded_days: 1 },
                evidence_coverage: { entry_reason_known: 1, invalidation_condition_known: 0, price_evidence_present: 1,
                    plan_id_present: 1, judgment_review_recorded: 0, total_count: 2, presence_is_not_verification: true },
                top_loss_examples: [{ symbol: "600000", entry_date: "2026-10-19", exit_date: "2026-10-23", realized_pnl: -12.34,
                    original_entry_reason: { status: "recorded", text: "原候选观察理由", source_field: "entry_reason" },
                    invalidation_condition: { status: "unknown", text: null, source_field: null },
                    original_exit_reason: { status: "recorded", text: "原记录止损复核", source_field: "exit_reason" },
                    judgment_evidence: { status: "not_assessed", recorded_status: null, independently_verified_here: false } }],
                follow_up_checks: ["核对跨日0天字段"] }] } };
}

test("weekly review displays original reasons, incomplete amount coverage and holding conflicts", () => {
    const h = harness(), data = weeklyFixture(), before = JSON.stringify(data);
    const html = h.api.weeklyLearningHtml(data);
    assert.match(html, /2026-10-19 至 2026-10-25 · 已平仓 2笔/);
    assert.match(html, /-12\.34元（金额有记录 1\/2笔；金额字段不完整）/);
    assert.match(html, /原记录退出费用 5\.00元/);
    assert.match(html, /跨日却记0天 1笔/);
    assert.match(html, /交易日历未核实 1笔；原持有天数为负 1笔/);
    assert.match(html, /原候选观察理由〔原字段 entry_reason〕/);
    assert.match(html, /原记录止损复核〔原字段 exit_reason〕/);
    assert.match(html, /原失效条件：原记录未提供，不能事后补成预判/);
    assert.match(html, /字段存在不等于本页独立验证/);
    assert.match(html, /本页未独立验证判断对错，不能由盈亏代替判断核验/);
    assert.match(html, /核对跨日0天字段/);
    assert.match(html, /无日期或晚于核查日的记录 1笔/);
    assert.equal(JSON.stringify(data), before);
});

test("missing weekly evidence stays unknown instead of becoming zero weeks or zero fees", () => {
    const h = harness();
    const missing = h.api.weeklyLearningHtml(null);
    assert.match(missing, /周记录覆盖未核实/);
    assert.match(missing, /无日期或晚于核查日的记录 未核实笔/);
    assert.doesNotMatch(missing, /0周|0\.00元/);
    const partial = h.api.weeklyLearningHtml({ weekly_review: { status: "__proto__", weeks: [null,
        { realized_pnl: null, recorded_exit_fees: null, evidence_coverage: null, holding_period: null,
            top_loss_examples: [null, { original_entry_reason: { text: {} } }] }] } });
    assert.match(partial, /金额完整性未核实/);
    assert.match(partial, /周列表含无法读取的记录/);
    assert.match(partial, /原入场理由 未记录/);
    assert.match(partial, /原记录未提供，不能事后补成预判/);
    assert.doesNotMatch(partial, /0\.00元|\[object Object\]/);
});

test("weekly source text is escaped and profit cannot be relabelled as a correct judgment", () => {
    const h = harness(), data = weeklyFixture();
    const row = data.weekly_review.weeks[0].top_loss_examples[0];
    data.summary = "<script>summary</script>";
    data.checks = ["<img src=x>"];
    row.symbol = "<svg/onload=bad>";
    row.realized_pnl = 12.34;
    row.original_entry_reason.text = "<b>entered</b>";
    row.original_entry_reason.source_field = "<script>field</script>";
    const html = h.api.weeklyLearningHtml(data);
    assert.match(html, /&lt;script&gt;summary&lt;\/script&gt;/);
    assert.match(html, /&lt;svg\/onload=bad&gt;/);
    assert.match(html, /&lt;b&gt;entered&lt;\/b&gt;/);
    assert.match(html, /&lt;script&gt;field&lt;\/script&gt;/);
    assert.match(html, /盈利不自动证明方法有效/);
    assert.match(html, /已有核对记录：not_assessed/);
    assert.doesNotMatch(html, /<script>|<img |<svg|<b>entered|判断正确/);
});

test("one integrity GET fills both panels and a read failure stays visible", async () => {
    const h = harness(), facts = { innerHTML: "" }, weekly = { innerHTML: "" };
    h.nodes.set("productIntegrityPanel", facts); h.nodes.set("productWeeklyLearningPanel", weekly);
    h.fetch(async () => ({ ok: true, json: async () => ({ report: { learning: weeklyFixture() } }) }));
    await h.api.loadProductIntegrity();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, "/api/product-integrity");
    assert.match(facts.innerHTML, /原记录2笔平仓/);
    assert.match(weekly.innerHTML, /原候选观察理由/);
    h.fetch(async () => { throw new Error("fixture unavailable"); });
    await h.api.loadProductIntegrity();
    assert.match(facts.innerHTML, /暂时无法读取/);
    assert.match(weekly.innerHTML, /不据此认定没有亏损或没有判断错误/);
});

test("the review refresh button reloads weekly learning even without an AI panel", async () => {
    const h = harness(), weekly = { innerHTML: "" }; let click;
    h.nodes.set("productWeeklyLearningPanel", weekly);
    h.nodes.set("refreshDecisionHistoryButton", { addEventListener(type, handler) { if (type === "click") click = handler; } });
    h.fetch(async () => ({ ok: true, json: async () => ({ report: { learning: weeklyFixture() } }) }));
    h.api.setupAiFundManagerLifecycle();
    assert.equal(typeof click, "function");
    await click();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, "/api/product-integrity");
    assert.match(weekly.innerHTML, /原记录2笔平仓/);

    const panel = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [] }, note = {};
    h.nodes.set("decisionHistoryPanel", panel); h.nodes.set("decisionHistoryNote", note);
    h.fetch(async (url) => {
        if (url.includes("ai-trader")) throw new Error("AI fixture unavailable");
        return { ok: true, json: async () => ({ as_of: "2026-09-04", watchlist: [], events: [] }) };
    });
    await h.api.loadDecisionHistory();
    assert.match(panel.innerHTML, /来源范围.*策略观察与策略账事件.*当前 AI 档位/);
    assert.match(panel.innerHTML, /C 线.*未接入.*不包含/);
    assert.match(panel.innerHTML, /读取失败，数量未知/);
    assert.match(panel.innerHTML, /原记录 · 对<\/span><strong>未知/);
    assert.match(note.textContent, /AI 判断数量未知/);
    assert.equal(h.calls.length, 4);
    assert.ok(h.calls.every((call) => !call.options?.method || call.options.method === "GET"));

    h.fetch(async () => { throw new Error("all fixture sources unavailable"); });
    await h.api.loadDecisionHistory();
    assert.match(panel.innerHTML, /来源范围/);
    assert.match(panel.innerHTML, /C 线.*未接入.*不包含/);
    assert.match(panel.innerHTML, /复盘来源读取失败，记录数量未知/);
    assert.match(note.textContent, /数量未知/);
    assert.doesNotMatch(note.textContent, /AI 判断 0 条/);
});

test("market dates never use a global date, fetch time or cache modification as missing quote evidence", () => {
    const h = harness();
    const dates = h.api.marketEvidenceDates({ as_of: "2026-10-23", trade_date: "2026-10-23",
        hs300: { date: "2026-10-23" }, sh_index: { date: "2026-10-22" },
        market_breadth: { quote_date: "", fetched_at: "2026-10-23T15:10:00+08:00" },
        sector_snapshot: { as_of: "", cache_modified_at: "2026-10-23T16:00:00+08:00" } });
    assert.equal(dates.hs300, "2026-10-23");
    assert.equal(dates.sh_index, "2026-10-22");
    assert.equal(dates.breadth, "");
    assert.equal(dates.sectors, "");
    assert.equal(dates.as_of, "");
    const invalid = h.api.marketEvidenceDates({ market_dates: { hs300: "2026-02-30", sh_index: "2026-10-23T10:00:00", breadth: {}, sectors: "<script>" } });
    assert.equal(invalid.as_of, "");
    assert.equal(invalid.hs300, "");
});

test("a shared market business date exists only when all four real dates agree", () => {
    const h = harness(), data = { market_dates: { hs300: "2026-10-23", sh_index: "2026-10-23", breadth: "2026-10-23", sectors: "2026-10-23" } };
    assert.equal(h.api.marketEvidenceDates(data).as_of, "2026-10-23");
    data.market_dates.sectors = "2026-10-22";
    assert.equal(h.api.marketEvidenceDates(data).as_of, "");
    data.market_dates.sectors = "";
    assert.equal(h.api.marketEvidenceDates(data).as_of, "");
});

test("market cards and both sector views display their own dates and distinguish collection clocks", () => {
    const h = harness();
    for (const key of ["marketOverview", "marketDataNote", "marketSentiment", "sectorBoard", "homeSectorNote"]) h.nodes.set(key, {});
    h.api.renderMarketOverview({ as_of: "2026-10-23", market_sentiment: "缓存观察", update_hint: "原提示",
        hs300: { date: "2026-10-23", value: 4000, change: 1 }, sh_index: { date: "2026-10-22", value: 3000, change: 0 },
        market_breadth: { quote_date: "", total_amount: "100万元", fetched_at: "2026-10-23T15:10:00", source: "fixture-snapshot" },
        sector_snapshot: { as_of: "", freshness_status: "unknown", cache_modified_at: "2026-10-23T16:00:00", source: "industry-cache" } });
    const overview = h.nodes.get("marketOverview").innerHTML;
    assert.match(overview, /行情日 2026-10-23/);
    assert.match(overview, /行情日 2026-10-22/);
    assert.match(overview, /行情日 未核实/);
    assert.match(h.nodes.get("marketDataNote").textContent, /抓取时间 2026-10-23T15:10:00（仅表示采集，不能当成行情日）/);
    assert.match(h.nodes.get("marketSentiment").textContent, /各项行情日未统一或未核实/);
    assert.match(h.nodes.get("sectorBoard").innerHTML, /行业板块行情日 未核实/);
    assert.match(h.nodes.get("sectorBoard").innerHTML, /文件时间不能当成行情日/);
    assert.match(h.nodes.get("homeSectorNote").textContent, /行业板块行情日 未核实/);
});

test("legacy market brain retains snapshot date and unknown freshness cannot display current confidence", () => {
    const h = harness(), main = {}, note = {};
    h.nodes.set("kpiMarketBrainValue", main); h.nodes.set("kpiMarketBrainSub", note);
    const snapshot = { regime: "BULL", evidence_eligible: true, confidence: 0.8, as_of: "2026-09-10", target_date: "2026-10-23" };
    h.api.renderHomeKpiMarketBrain({ ok: true, snapshot });
    assert.match(note.textContent, /新鲜度未核实 · 源日期 2026-09-10 · 目标日 2026-10-23/);
    assert.doesNotMatch(note.textContent, /80%|截至 2026-10-23/);
    h.api.renderHomeKpiMarketBrain({ ok: true, snapshot, freshness: { status: "stale", source_date: "2026-09-10", target_date: "2026-10-23" } });
    assert.match(note.textContent, /历史判断已滞后/);
    h.api.renderHomeKpiMarketBrain({ ok: true, snapshot: { ...snapshot, confidence: null }, freshness: { status: "current", source_date: "2026-09-10" } });
    assert.match(note.textContent, /证据不足/);
    assert.doesNotMatch(note.textContent, /置信度 0%/);
});

test("recorded industry and theme sources are visible while unknowns stay ungrouped", () => {
    const h = harness(), known = { industry: "银行", primary_theme: "未知", theme_tags: ["金融科技"], select_date: "2026-10-23",
        exposure_evidence: { industry_status: "recorded", industry_source: "candidate_record", industry_as_of: "", theme_status: "recorded", theme_source: "original_theme_tags" } };
    const unknown = { industry: "银行", exposure_evidence: { industry_status: "unknown", theme_status: "unknown" } };
    const before = JSON.stringify([known, unknown]);
    const html = h.api.candidateExposureHtml(known);
    assert.match(html, /行业 银行 · 来源 candidate_record · 资料日 未核实/);
    assert.match(html, /题材 金融科技 · 来源 original_theme_tags/);
    assert.doesNotMatch(html, /资料日 2026-10-23/);
    const summary = h.api.candidateExposureSummaryHtml([known, unknown]);
    assert.match(summary, /行业未分组 1\/2；题材未分组 1\/2/);
    assert.match(summary, /已记录行业：银行 1条/);
    assert.match(summary, /未知行业\/题材不能视为独立分散/);
    assert.equal(JSON.stringify([known, unknown]), before);
});

test("strategy names remain intact alongside MA20 or daily near-limit approximation labels", () => {
    const h = harness();
    const long = { symbol: "600000", name: "示例", strategy_name: "长期观察", detail_lane: "long" };
    const board = { ...long, strategy_name: "打板观察", detail_lane: "board" };
    assert.equal(h.api.candidateMethodNote(long), "MA20技术趋势 · 纸面未验证");
    assert.equal(h.api.candidateMethodNote(board), "日线近涨停近似 · 无封单队列证据");
    const longHtml = h.api.renderStrategyRecommendationRow(long);
    const boardHtml = h.api.renderCandidateCard(board, false);
    assert.match(longHtml, /长期观察/);
    assert.match(longHtml, /MA20技术趋势/);
    assert.match(boardHtml, /打板观察/);
    assert.match(boardHtml, /日线近涨停近似 · 无封单队列证据/);
});

test("stock daily diagnostics show the actual full history and current reference window", () => {
    const window = {}; vm.runInNewContext(stock, { window });
    const html = window.QuantDeskStockWorkbench.render({ reference: { available: true, levels: [], history_quality: {
        full_history_rows: 6412, ohlc_invalid_count: 0, first_invalid_date: null, current_window_start: "2026-07-24",
        current_window_rows: 60, current_window_ohlc_invalid_count: 0, full_history_guard_retained: true,
        message: "全历史检查保留" } } });
    assert.match(html, /全历史 6412 条；高低开收异常 0 条；首个异常日 未记录/);
    assert.match(html, /本次参考窗口自 2026-07-24 起，共 60 条；窗口高低开收异常 0 条/);
    assert.match(html, /全历史守卫保留；最近窗口正常不能绕过全历史失败检查/);
});

test("a clean recent stock window cannot conceal an old failing OHLC guard", () => {
    const window = {}; vm.runInNewContext(stock, { window });
    const html = window.QuantDeskStockWorkbench.render({ reference: { available: false, message: "历史价格关系异常，暂不计算参考",
        reason_codes: ["daily_ohlc_invalid"], history_quality: { full_history_rows: 6412, ohlc_invalid_count: 1,
            first_invalid_date: "2012-01-04", current_window_start: "2026-07-24", current_window_rows: 60,
            current_window_ohlc_invalid_count: 0, full_history_guard_retained: true } } });
    assert.match(html, /sq-history-quality" open/);
    assert.match(html, /首个异常日 2012-01-04/);
    assert.match(html, /最近窗口未发现此类异常，全历史仍有异常/);
    assert.match(html, /检查原因：历史高低开收关系异常/);
    assert.doesNotMatch(html, /sq-level-buy/);
});

test("score explanation and dated pool memberships are read-only details outside the stock button", () => {
    const h = harness(), row = { symbol: "600000", name: "示例", priority_score: 51, strategy_name: "长期观察", detail_lane: "long",
        score_explanation: { score_id: "ui-focus-score-v1", components: [{ name: "原条件", points: 50 }, { name: "补充", points: 0 }],
            base_total: 50, strategy_tilt: 1, total: 51, tie_breaker: "代码升序", ranking_only: true, not_comparable_to: ["胜率", "另一规则分数"] },
        pool_memberships: [{ pool: "today", label: "今日候选", as_of: "2026-10-23" }, { pool: "paper", label: "策略纸面持仓", as_of: "2026-10-22" }] };
    const before = JSON.stringify(row), html = h.api.renderCandidateCard(row, false);
    assert.match(html, /研究优先分 51 · 查看分数与池子来源/);
    assert.match(html, /补充：0分/);
    assert.match(html, /基础合计 50；策略倾斜 1；记录总分 51/);
    assert.match(html, /今日候选 · 来源日 2026-10-23/);
    assert.match(html, /策略纸面持仓 · 来源日 2026-10-22/);
    assert.match(html, /微小分差不证明优势/);
    assert.ok(html.indexOf("<details") > html.indexOf("</button>"));
    assert.equal(JSON.stringify(row), before);
    assert.equal(h.calls.length, 0);
    assert.equal(h.api.candidateEvidenceDetails({ symbol: "600000" }), "");
});

test("unsafe and malformed optional candidate evidence stays escaped or unknown", () => {
    const h = harness();
    const html = h.api.candidateEvidenceDetails({ priority_score: 0, score_explanation: { score_id: "<script>x</script>",
        components: [null, { name: "<img src=x>", points: {} }], strategy_tilt: null, total: 0, tie_breaker: {}, not_comparable_to: ["<svg>"] },
        pool_memberships: [null, { label: "<b>池子</b>" }] });
    assert.match(html, /研究优先分 0/);
    assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);
    assert.match(html, /&lt;b&gt;池子&lt;\/b&gt; · 来源日 未核实/);
    assert.match(html, /策略倾斜 未记录；记录总分 0/);
    assert.doesNotMatch(html, /<script>|<img |<b>|<svg>|\[object Object\]/);
    const exposure = h.api.candidateExposureHtml({ industry: "<script>行业</script>", theme: {}, industry_source: {} });
    assert.match(exposure, /&lt;script&gt;行业&lt;\/script&gt;/);
    assert.match(exposure, /题材 未分组/);
    assert.doesNotMatch(exposure, /<script>|\[object Object\]/);
});

test("score display rounds only visible values and adds one final full stop", () => {
    const h = harness(), row = { priority_score: 17.18814968814969, score_explanation: {
        components: [{ name: "原分项", points: -0.31185031185031137 }, { name: "原零值", points: 0 }],
        base_total: 16.18814968814969, strategy_tilt: 1, total: 17.18814968814969,
        tie_breaker: "按原代码升序。", not_comparable_to: "其他规则分数。。" } };
    const before = JSON.stringify(row), html = h.api.candidateEvidenceDetails(row);
    assert.match(html, /研究优先分 17\.19/);
    assert.match(html, /原分项：-0\.31分/);
    assert.match(html, /原零值：0分/);
    assert.match(html, /基础合计 16\.19；策略倾斜 1；记录总分 17\.19/);
    assert.match(html, /同分处理：按原代码升序。<\/p>/);
    assert.match(html, /不能直接比较：其他规则分数。微小分差/);
    assert.doesNotMatch(html, /\.311850|\.188149|。。/);
    assert.equal(JSON.stringify(row), before);
    assert.equal(h.calls.length, 0);
});

test("a minimal read-only host without optional abort or timer APIs still reads the requested profile", async () => {
    const h = harness({ noAbortController: true, noWindowTimers: true });
    h.fetch(async () => ({ ok: true, json: async () => ({ profile: "glm", records: ["fixture-glm"] }) }));
    const result = await h.api.requestJson("/api/ai-trader/decisions?profile=glm");
    assert.equal(result.profile, "glm");
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].url, "/api/ai-trader/decisions?profile=glm");
    assert.equal(h.calls[0].options.signal, undefined);
});

test("hosts without AbortController still bound reads with timers and honor a caller cancellation", async () => {
    const h = harness({ noAbortController: true });
    h.fetch(() => new Promise(() => {}));
    const timed = h.api.requestJson("/api/old-host-timeout", { timeoutMs: 25 });
    [...h.timers.values()][0].fn();
    await assert.rejects(timed, /读取超过/);
    await Promise.resolve();
    assert.equal(h.timers.size, 0);
    const abort = new AbortController();
    const pending = h.api.requestJson("/api/old-host-cancel", { signal: abort.signal });
    abort.abort();
    await assert.rejects(pending, /读取已取消/);
});

test("integrity details expose each recorded holding conflict with original days, dates and derived source", () => {
    const h = harness(), rows = Array.from({ length: 20 }, (_, i) => ({ symbol: String(600000 + i),
        entry_date: "2026-10-19", exit_date: "2026-10-21", recorded_holding_days: 0,
        elapsed_calendar_days: 2, elapsed_trading_days: 2, status: "derived_from_local_calendar",
        recorded_zero_conflicts_with_dates: true, read_only: true }));
    rows[1].status = "calendar_unverified";
    rows.push({ symbol: "600999", recorded_holding_days: 5, recorded_zero_conflicts_with_dates: false });
    const before = JSON.stringify(rows);
    const html = h.api.productIntegrityHtml({ report: { closed_holding_periods: rows } });
    assert.match(html, /历史持有期核对 · 20条已记录冲突（展示前18条）/);
    assert.match(html, /600000<\/strong> · 2026-10-19 至 2026-10-21/);
    assert.match(html, /原记录 0天；本地交易日历派生 2个交易日/);
    assert.match(html, /交易日历未核实，交易日数未知；日期间隔 2天/);
    assert.match(html, /来源：策略账原平仓记录/);
    assert.match(html, /只读派生更正，原持有天数和原说明保留/);
    assert.match(html, /600017/);
    assert.doesNotMatch(html, /600018|600019|600999/);
    assert.equal(JSON.stringify(rows), before);
    assert.equal(h.calls.length, 0);
});

test("empty, incomplete and unsafe holding-period records never invent zero days or verified calendar evidence", () => {
    const h = harness();
    for (const records of [[], undefined, null, [{ recorded_zero_conflicts_with_dates: false }]]) {
        const html = h.api.historicalHoldingPeriodsHtml(records);
        assert.match(html, /历史持有期核对/);
        assert.doesNotMatch(html, /原记录 0天|派生 0个交易日|已确认全部历史|全部历史核对通过/);
    }
    const html = h.api.historicalHoldingPeriodsHtml([{ symbol: "<script>600000</script>", entry_date: "<img src=x>",
        exit_date: null, recorded_holding_days: null, elapsed_calendar_days: null, elapsed_trading_days: 8,
        status: "calendar_unverified", recorded_zero_conflicts_with_dates: true, read_only: true }]);
    assert.match(html, /&lt;script&gt;600000&lt;\/script&gt;/);
    assert.match(html, /&lt;img src=x&gt; 至 未核实/);
    assert.match(html, /原记录 未核实天；交易日历未核实，交易日数未知/);
    assert.match(html, /日期间隔 未核实天/);
    assert.doesNotMatch(html, /<script>|<img |派生 8个交易日|原记录 0天|\[object Object\]/);
});

function fixtureViewLoaders(h, read) {
    h.window.fixtureViewRead = read;
    const names = ["loadProjectStatus", "loadCandidates", "loadHomeEquity", "loadHomeAiLedger", "loadStartupUpdateStatus",
        "loadMarketBrainHome", "loadProductIntegrity", "loadDataHealthHome", "loadStrategyScorecard", "loadNewsStatus",
        "loadMarketOverview", "loadRealtimeMarket", "loadThemeMapStatus", "loadForwardPitHome", "loadLimitUpReview", "loadStrategyBrainShadow"];
    h.evaluate(names.map((name) => `${name} = () => window.fixtureViewRead("${name}");`).join("\n"));
}
const flushView = () => new Promise((resolve) => setImmediate(resolve));

for (const stalled of ["dashboard", "chat"]) {
    test(`real AI ${stalled} GET expires so another page continues and the same AI read can retry`, async () => {
        const h = harness(), note = {}, log = { innerHTML: "" }; let stall = true, continued = 0;
        h.nodes.set("aiTraderStatusNote", note); h.nodes.set("aiTraderChatLog", log);
        h.window.afterAiRead = () => { continued += 1; };
        h.evaluate("renderAiTraderDashboard = () => {}; loadCandidates = async () => window.afterAiRead();");
        h.fetch(async (url) => {
            if (stall && url === `/api/ai-trader/${stalled}`) return new Promise(() => {});
            return { ok: true, status: 200, json: async () => url.endsWith("/chat") ? { messages: [] } : { updated_at: "fixture" } };
        });
        const aiPage = h.api.loadVisiblePageData("ai-trader-section");
        await flushView();
        assert.equal(h.calls.at(-1).url, `/api/ai-trader/${stalled}`);
        const nextPage = h.api.loadVisiblePageData("candidate-pool-section");
        await flushView();
        assert.equal(continued, 0);
        const timeout = [...h.timers.values()].find((timer) => timer.delay === 20000);
        assert.ok(timeout, "the actual AI GET must have the shared read deadline");
        timeout.fn();
        await Promise.all([aiPage, nextPage]);
        assert.equal(continued, 1);
        assert.equal(h.calls.findLast((call) => call.url === `/api/ai-trader/${stalled}`).options.signal.aborted, true);
        assert.match(note.textContent, /数据读取失败：读取超过 20 秒，请重试/);
        stall = false;
        await h.api.loadVisiblePageData("ai-trader-section");
        assert.equal(h.calls.filter((call) => call.url === `/api/ai-trader/${stalled}`).length, 2);
        assert.match(note.textContent, /已加载/);
        assert.equal(h.timers.size, 0);
    });
}

test("AI GET envelopes retain HTTP error and malformed JSON behavior without mixing plain payloads", async () => {
    const h = harness();
    h.fetch(async () => ({ ok: false, status: 503, json: async () => ({ reason: "fixture-unavailable" }) }));
    let response = await h.api.fetchAiTraderJson("/api/ai-trader/dashboard");
    assert.equal(response.ok, false); assert.equal(response.status, 503);
    assert.equal(response.payload.reason, "fixture-unavailable");
    h.fetch(async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError("fixture"); } }));
    response = await h.api.fetchAiTraderJson("/api/ai-trader/dashboard");
    assert.equal(response.ok, false); assert.equal(response.status, 502); assert.equal(response.payload, null);
    h.fetch(async () => ({ ok: true, status: 200, json: async () => new Promise(() => {}) }));
    const stalledBody = h.api.fetchAiTraderJson("/api/ai-trader/dashboard");
    await flushView();
    const bodyTimeout = [...h.timers.values()].find((timer) => timer.delay === 20000);
    assert.ok(bodyTimeout, "the deadline must also cover a stalled JSON body");
    bodyTimeout.fn();
    await assert.rejects(stalledBody, /读取超过 20 秒/);
    h.fetch(async () => ({ ok: true, status: 200, json: async () => ({ profile: "fixture" }) }));
    const [plain, envelope] = await Promise.all([
        h.api.requestJson("/api/ai-trader/dashboard"), h.api.fetchAiTraderJson("/api/ai-trader/dashboard"),
    ]);
    assert.equal(plain.profile, "fixture"); assert.equal(plain.payload, undefined);
    assert.equal(envelope.status, 200); assert.equal(envelope.payload.profile, "fixture");
    assert.equal(h.timers.size, 0);
});

test("AI POST keeps its original mutation guard, options and HTTP response envelope", async () => {
    const h = harness(), options = { method: "POST", headers: { "Content-Type": "application/json" }, body: '{"question":"fixture"}' };
    await assert.rejects(h.api.fetchAiTraderJson("/api/ai-trader/chat", options), /只读/);
    assert.equal(h.calls.length, 0);
    h.window.quantdeskRuntimeCapabilities = { read_only: false, mutations_enabled: true };
    h.fetch(async () => ({ ok: false, status: 409, json: async () => ({ ok: false, reason: "fixture-conflict" }) }));
    const result = await h.api.fetchAiTraderJson("/api/ai-trader/chat", options);
    assert.equal(h.calls[0].options, options);
    assert.equal(result.ok, false); assert.equal(result.status, 409); assert.equal(result.payload.reason, "fixture-conflict");
    assert.equal(h.timers.size, 0);
});

test("the today page loads candidates and both books before heavy diagnostics without hidden tool requests", async () => {
    const h = harness(), calls = []; let release, active = 0, maximum = 0;
    fixtureViewLoaders(h, async (name) => {
        calls.push(name); active += 1; maximum = Math.max(maximum, active);
        if (name === "loadCandidates") await new Promise((resolve) => { release = resolve; });
        active -= 1;
    });
    const pending = h.api.loadVisiblePageData("");
    await flushView();
    assert.deepEqual(calls, ["loadProjectStatus", "loadCandidates"]);
    assert.equal(active, 1);
    release(); await pending;
    for (const heavy of ["loadDataHealthHome", "loadStrategyScorecard"]) {
        for (const core of ["loadCandidates", "loadHomeEquity", "loadHomeAiLedger"]) assert.ok(calls.indexOf(heavy) > calls.indexOf(core));
    }
    assert.equal(maximum, 1);
    assert.equal(calls.some((name) => /ThemeMap|ForwardPit|LimitUp|MarketOverview|RealtimeMarket/.test(name)), false);
});

test("switching to watch cancels queued old-page reads and first loads watch candidates then market data", async () => {
    const h = harness(), calls = []; let release, active = 0, maximum = 0;
    fixtureViewLoaders(h, async (name) => {
        calls.push(name); active += 1; maximum = Math.max(maximum, active);
        if (name === "loadProjectStatus") await new Promise((resolve) => { release = resolve; });
        active -= 1;
    });
    const old = h.api.loadVisiblePageData("");
    await flushView();
    const current = h.api.loadVisiblePageData("__watch__");
    await flushView();
    assert.deepEqual(calls, ["loadProjectStatus"]);
    release(); await Promise.all([old, current]);
    assert.deepEqual(calls, ["loadProjectStatus", "loadCandidates", "loadMarketOverview", "loadRealtimeMarket"]);
    assert.equal(maximum, 1);
});

test("entering or refreshing a research tool loads its own sources and a failed page read can be retried", async () => {
    const h = harness(), calls = []; let failed = false;
    fixtureViewLoaders(h, async (name) => {
        calls.push(name);
        if (name === "loadStrategyScorecard" && !failed) { failed = true; throw new Error("fixture failed read"); }
    });
    await h.api.loadVisiblePageData("theme-map-section");
    assert.deepEqual(calls, ["loadThemeMapStatus"]);
    await h.api.loadVisiblePageData("performance-section");
    assert.deepEqual(calls.slice(1), ["loadStrategyScorecard", "loadStrategyBrainShadow"]);
    await h.api.loadVisiblePageData("performance-section");
    assert.deepEqual(calls.slice(3), ["loadStrategyScorecard", "loadStrategyBrainShadow"]);
    assert.equal(calls.includes("loadCandidates"), false);
});

test("the initial saved terminal startup receipt does not launch a duplicate page refresh", () => {
    const h = harness();
    h.api.syncStartupStatusPolling({ status: "completed", request_id: "old-saved-run", running: false });
    const state = h.api.startupReadState();
    assert.equal(state.taskId, "old-saved-run");
    assert.equal(state.sequence, 0);
    assert.equal(state.applied, false);
    assert.equal(h.calls.length, 0);
    assert.equal(h.timers.size, 0);
});

test("deferred module placeholders say unread and do not start a background fetch", () => {
    const h = harness(), placeholder = { textContent: "正在加载 Forward PIT 状态..." }, explanation = { textContent: "只读展示已有资料" };
    h.selectors.set("[data-module-view] .empty-state, [data-module-view] .section-note, #forwardPitHomePanel .empty-state, #dataHealthHomePanel .empty-state", [placeholder, explanation]);
    h.nodes.set("kpiDataHealthSub", {}); h.nodes.set("kpiPrevBatchSub", {});
    h.api.prepareVisiblePagePlaceholders();
    assert.match(placeholder.textContent, /未读取；进入对应页面或点击刷新查看/);
    assert.equal(explanation.textContent, "只读展示已有资料");
    assert.match(h.nodes.get("kpiDataHealthSub").textContent, /候选与两本账读取后/);
    assert.equal(h.calls.length, 0);
});

test("a direct non-home AI page leaves unrequested global startup and news status explicitly unread", async () => {
    const h = harness(), startup = { textContent: template.match(/<section id="startupUpdateStatus"[^>]*>([\s\S]*?)<\/section>/)[1].trim() },
        news = { textContent: template.match(/<span id="newsStatus"[^>]*>([^<]*)<\/span>/)[1].trim() };
    h.nodes.set("startupUpdateStatus", startup); h.nodes.set("newsStatus", news);
    let aiReads = 0;
    h.window.fixtureDirectAiRead = () => { aiReads += 1; };
    h.evaluate("loadAiTraderPanel = async () => window.fixtureDirectAiRead();");
    h.api.prepareVisiblePagePlaceholders();
    await h.api.loadVisiblePageData("ai-trader-section");
    assert.equal(aiReads, 1);
    assert.equal(startup.textContent, "启动更新状态未读取；今天页可查看");
    assert.equal(news.textContent, "新闻状态未读取；今天页可查看");
    assert.doesNotMatch(startup.textContent + news.textContent, /正在读取|加载中/);
    assert.equal(h.calls.length, 0, "opening a different page must not request these global statuses");
});

test("cost conflicts can explain a report difference without replacing original PnL or claiming full cashflow verification", () => {
    const h = harness(), source = { report: { pnl_identity: { status: "mismatch", difference: 3.03 },
        loss_contribution: { unexplained_difference: 3.03 }, capital_cost_bridge: {
        message: "旧摘要", cost_basis_explanation: { status: "explains_reported_difference", explains_reported_difference: true,
            reported_pnl_difference: 3.03, candidate_adjustment_sum: 3.03, remaining_reported_difference: 0,
            full_historical_cashflow_verified: false, authoritative_pnl_replaced: false, read_only: true,
            message: "成本字段冲突解释了报告差额；完整历史现金流仍未核验。", candidates: [
                { record_group: "positions", source_index: 0, symbol: "600000", entry_date: "2026-10-19", exit_date: null,
                    source_fields: { quantity: "quantity", amount: "market_value", pnl: "unrealized_pnl" },
                    original_values: { quantity: 500, entry_price: 9.4047, avg_cost: 9.3186, amount: 4765, recorded_pnl: 62.65 },
                    pnl_using_entry_price: 62.65, pnl_using_avg_cost: 105.70, alternative_pnl_delta: 43.05 },
                { record_group: "closed_positions", source_index: 1, symbol: "600611", entry_date: "2026-08-06", exit_date: "2026-09-24",
                    source_fields: { quantity: "quantity", amount: "net_proceeds", pnl: "realized_pnl" },
                    original_values: { quantity: 200, entry_price: 4.8024, avg_cost: 5.0025, amount: 876.12, recorded_pnl: -84.36 },
                    pnl_using_entry_price: -84.36, pnl_using_avg_cost: -124.38, alternative_pnl_delta: -40.02 },
            ] } } } };
    const before = JSON.stringify(source), html = h.api.productIntegrityHtml(source);
    assert.match(html, /3\.03元/);
    assert.match(html, /仍有差额，尚未核对/);
    assert.match(html, /已解释报告差额；完整历史现金流仍未核验/);
    assert.match(html, /600000 · 当前持仓记录/);
    assert.match(html, /来源 positions\[0\]/);
    assert.match(html, /候选口径差量 43\.05元/);
    assert.match(html, /600611 · 历史平仓记录/);
    assert.match(html, /候选口径差量 -40\.02元/);
    assert.match(html, /原纸面盈亏 -84\.36元/);
    assert.match(html, /相对该报告恒等式的剩余差额 0\.00元/);
    assert.match(html, /原账待核差额 3\.03元/);
    assert.doesNotMatch(html, /未解释差额 3\.03元/);
    assert.match(html, /不代表账本已修正或已完成全账核对/);
    assert.equal(JSON.stringify(source), before);
    assert.equal(h.calls.length, 0);
});
