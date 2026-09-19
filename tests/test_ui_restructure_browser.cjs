"use strict";

// Run against the task-owned actual Flask preview. All API responses below are
// deterministic fixtures; non-GET requests and every external origin are denied.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const { createRequire } = require("node:module");

const root = path.resolve(__dirname, "..");
const previewRaw = process.env.QD_UI_PREVIEW_URL;
const evidenceRaw = process.env.QD_UI_EVIDENCE_DIR;
if (!previewRaw || !evidenceRaw) throw new Error("Set QD_UI_PREVIEW_URL and QD_UI_EVIDENCE_DIR to the approved isolated preview and external task evidence directory.");
const preview = new URL(previewRaw);
if (preview.protocol !== "http:" || preview.hostname !== "127.0.0.1" || preview.port === "8000" || !preview.port || preview.username || preview.password || preview.pathname !== "/" || preview.search || preview.hash) {
    throw new Error("Browser verification accepts only an explicit 127.0.0.1 fixture preview, never canonical port 8000 or an external site.");
}
if (!path.isAbsolute(evidenceRaw)) throw new Error("The evidence directory must be absolute and outside the repository.");
const evidence = fs.realpathSync(evidenceRaw);
const canonical = root.includes(`${path.sep}.worktrees${path.sep}`) ? root.split(`${path.sep}.worktrees${path.sep}`)[0] : root;
const under = (parent, child) => { const relative = path.relative(parent, child); return !relative || (!relative.startsWith("..") && !path.isAbsolute(relative)); };
if (under(canonical, evidence) || evidence !== path.resolve(evidenceRaw)) throw new Error("Evidence must be outside every repository path and cannot traverse a link/junction.");
for (let current = path.resolve(evidenceRaw); current !== path.dirname(current); current = path.dirname(current)) {
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error("Evidence paths cannot contain symlinks or junctions.");
}
const manifest = JSON.parse(fs.readFileSync(path.join(evidence, "preview-manifest.json"), "utf8"));
if (manifest.task_id !== "GROK-UI-REVIEW-20260907" || manifest.url !== preview.href || fs.realpathSync(manifest.worktree) !== fs.realpathSync(root)) throw new Error("Preview identity does not match this task's worktree and approved URL.");
const sha256 = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
for (const [file, expected] of Object.entries(manifest.source_hashes)) assert.equal(sha256(path.join(root, file)), expected, `restart preview after source changes: ${file}`);

function localPlaywright() {
    try { return require("playwright"); } catch (_) { /* use an already installed CLI cache */ }
    const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    const npmCache = path.join(localAppData, "npm-cache", "_npx");
    if (fs.existsSync(npmCache)) {
        for (const entry of fs.readdirSync(npmCache)) {
            const packageFile = path.join(npmCache, entry, "node_modules", "playwright", "package.json");
            if (fs.existsSync(packageFile)) return createRequire(packageFile)("playwright");
        }
    }
    throw new Error("No local Playwright installation found. This verification never installs or downloads dependencies.");
}

const { chromium } = localPlaywright();
const executablePath = [
    chromium.executablePath(),
    path.join(process.env.ProgramFiles || "C:/Program Files", "Google/Chrome/Application/chrome.exe"),
    path.join(process.env["ProgramFiles(x86)"] || "C:/Program Files (x86)", "Microsoft/Edge/Application/msedge.exe"),
].find((file) => fs.existsSync(file));
if (!executablePath) throw new Error("No existing Chromium/Chrome/Edge executable is available; no browser will be downloaded.");

const day = "2026-09-04";
const stamp = `${day}T09:30:00+08:00`;
const candidates = ["990001", "990002"].map((symbol, index) => ({
    symbol, name: `隔离夹具${index + 1}`, select_date: day, signal_date: day, detail_lane: "long",
    strategy_name: "确定性测试", strategy_lane: "长期研究", current_price: 12.5 + index,
    priority_score: 60 + index, matched_conditions: ["仅用于 UI 验收的夹具"],
    strategy_validation: { status: "research_only" },
}));
const event = (id, symbol, overrides = {}) => ({
    event_id: id, symbol, source: "fixture.daily", event_time: stamp,
    date: day, price: 12.34, price_basis: "qfq", side: "buy", action: "buy", status: "filled",
    evidence: { source: "fixture.daily", timestamp: stamp, price: 12.34 }, ...overrides,
});
const board = {
    ok: true, status: "passed", as_of: day,
    account: { initial_cash: 100000, total_equity: 101234, cash: 85000, market_value: 16234, total_return_pct: 1.234, unrealized_pnl: 1234, realized_pnl: 0 },
    positions: candidates.map((item) => ({ ...item, quantity: 100, entry_price: 12, latest_price: 12.5, holding_days: 2, entry_date: "2026-09-02", unrealized_pnl: 50, review_status: "hold_review" })),
    watchlist: [
        { symbol: "990001", name: "隔离夹具1", latest_date: day, review_status: "due_missing_evidence", review_reason: "测试：到期缺证据" },
        { symbol: "990002", name: "隔离夹具2", latest_date: day, review_status: "statement_mismatch", review_reason: '<img src="https://invalid.example/never.png" onerror="window.__uiInjected=true">不执行 HTML' },
        { symbol: "990003", name: "隔离夹具3", latest_date: day, review_status: "paper_tracking", review_reason: "文字提及未到期，不代表已核对到期状态" },
    ],
    events: [event("strategy-qfq", "990001"), event("strategy-raw", "990001", { price_basis: "raw" })],
    warnings: [], envelope: {},
};
const ai = {
    ok: true, degraded: false, warnings: [], updated_at: stamp,
    account: { equity: 120000, cash: 90000, initial_cash: 100000, positions_count: 2, as_of: day, updated_at: stamp },
    positions: ["990001", "990003"].map((symbol) => ({ symbol, name: `AI夹具${symbol}`, quantity: 100, avg_cost: 12, market_price: 12.5, unrealized_pnl: 50, acquired_on: "2026-09-02" })),
    latest_plan: { decision_date: day, trade_date: "2026-09-07", recorded_at: `${day}T08:00:00+08:00` },
    committed_intents: [{ intent_id: "plan-1", symbol: "990001", side: "buy", quantity: 100, source: "fixture.plan", price_basis: "qfq", price: 12.7, recorded_at: `${day}T08:00:00+08:00` }],
    uncommitted_intents: [],
    filled_trades: [event("ai-fill-1", "990001"), event("ai-fill-2", "990001", { price: 12.5 }), event("ai-unknown", "990001", { price_basis: "unknown" })],
    rejected_or_abandoned: [], latest_journal: {}, llm: {}, llm_runtime: {}, schedule_status: {},
};
const detail = (symbol) => {
    const rows = Array.from({ length: 26 }, (_, index) => {
        const date = new Date(Date.UTC(2026, 7, 10 + index)).toISOString().slice(0, 10);
        return { date, open: 12 + index / 100, close: 12.3 + index / 100, high: 12.8 + index / 100, low: 11.8 + index / 100, volume: 10000 + index * 100, ma5: 12.2, ma10: 12.1, ma20: 12, dif: 0.1, dea: 0.08, macd_bar: 0.04, k: 55, d: 50, j: 65, rsi6: 50, rsi12: 51, rsi24: 52, price_basis: "qfq" };
    });
    return {
        basic_info: { symbol, name: `隔离夹具${symbol}`, date: day, price: 12.5, pct_chg: 1 },
        chart_data: { symbol, price_basis: "qfq", kline_data: rows, markers: [{ ...event(`rule-${symbol}`, symbol), kind: "rule_signal", panel: "price", ledger: "规则研究", label: "入选观察", text: "事前记录测试夹具" }] },
        strategy_review: { select_date: day, detail_lane: "long", tracking: {}, data_quality: { missing_reasons: [] } },
        research_brief: { available: true, current_state: `夹具 ${symbol}：无真实研究结论`, research_lean: "仅验证页面", continue_if: "没有操作条件", invalidate_if: "不适用", key_levels: {}, boundary: "纸面 · 未验证 · 合成测试" },
        research_reference: { available: false, message: "夹具不提供模型推断" },
        news_summary: { available: false, items: [] }, industry_comparison: {}, tracking_vs_benchmark: {},
        explanation: '<p>隔离验收数据</p><img src="https://invalid.example/detail-never.png" onerror="window.__uiInjected=true"><svg><image href="https://invalid.example/svg-never.png"></image></svg>', indicators_exp: "隔离验收数据", risk_warning: "纸面 · 未验证", paper_simulation: {},
    };
};

const controls = { failBoard: false, failAi: false, missingAi: false, delayFirst: false, delayedDone: false };
const blocked = [], requests = [], pageErrors = [], screenshots = [];
const fallback = { ok: true, status: "missing", available: false, data: [], rows: [], items: [], warnings: [], strategies: [], messages: [], message: "隔离夹具未提供此模块数据", update_hint: "隔离夹具 · 未接真实行情" };
function fixture(url) {
    if (url.pathname === "/api/decision-board") return { code: controls.failBoard ? 503 : 200, value: board };
    if (url.pathname === "/api/ai-trader/dashboard") return { code: controls.failAi ? 503 : 200, value: controls.missingAi ? { ...ai, degraded: true, warnings: ["ledger_missing"], account: { equity: null, cash: null, positions_count: null }, positions: [] } : ai };
    if (url.pathname === "/api/daily/candidates") return { value: { ...fallback, all_candidates: candidates, candidates, today_recommendations: candidates, observation_candidates: [], daily_focus: candidates, candidate_groups: [], candidate_generation: { status: "completed", candidate_count: 2, signal_date: day }, message: "明确标记的 UI 合成夹具" } };
    if (url.pathname === "/api/realtime/stock-search") return { value: { rows: [{ symbol: "990003", name: "搜索夹具", source: "fixture", pct_chg: 0 }] } };
    const stock = url.pathname.match(/^\/api\/stock\/(\d+)\/detail$/);
    if (stock) return { value: detail(stock[1]), delay: stock[1] === "990001" && controls.delayFirst ? 500 : 0 };
    if (url.pathname === "/api/project-status") return { value: { readable: true, health_line: "隔离夹具：日线 ✓2/2 · 快照 未提供 · 两账仅测试", health_explanation: "全部为隔离验收数据", document_updated_at_display: day, file_modified_at: stamp, read_at: stamp, source_kind: "STATUS.md（fixture）", source_label: "项目记录 · 非实时核验", disclaimer: "纸面 · 未验证", document_updater: "UI fixture", phase: { summary: "B+C+A0 夹具演示", checkpoint: "2026-10-09" }, raw_text: "# STATUS fixture\n<img src=https://invalid.example/never.png>不会执行", line_progress: {}, sections: [] } };
    if (url.pathname.includes("startup")) return { value: { ...fallback, status: "idle", running: false, task: null } };
    if (url.pathname === "/api/data-health/dashboard") return { value: { ...fallback, health_level: "yellow", summary_text: "仅夹具，不做真实健康判断" } };
    return { value: fallback };
}

async function main() {
    const browser = await chromium.launch({ executablePath, headless: true, args: ["--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-first-run"] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
    await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
    await context.route("**/*", async (route) => {
        const request = route.request(), url = new URL(request.url());
        requests.push({ method: request.method(), path: url.pathname + url.search });
        if (url.origin !== preview.origin || request.method() !== "GET" || /(?:network|refresh_daily|refresh_quote)=1/.test(url.search)) {
            blocked.push({ method: request.method(), url: request.url() });
            return route.abort("blockedbyclient");
        }
        if (url.pathname.startsWith("/api/")) {
            const response = fixture(url);
            if (response.delay) await new Promise((resolve) => setTimeout(resolve, response.delay));
            await route.fulfill({ status: response.code || 200, contentType: "application/json", body: JSON.stringify(response.value) });
            if (response.delay) controls.delayedDone = true;
            return;
        }
        if (url.pathname !== "/" && url.pathname !== "/__ui_fixture_identity" && !url.pathname.startsWith("/static/")) return route.abort("blockedbyclient");
        return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    const snapshot = async (name) => {
        const file = `${name}.png`;
        await page.screenshot({ path: path.join(evidence, file), fullPage: false });
        fs.writeFileSync(path.join(evidence, `${name}.aria.txt`), await page.locator("body").ariaSnapshot(), "utf8");
        screenshots.push(file);
    };
    const stockIs = async (symbol) => {
        await page.waitForFunction((wanted) => typeof currentDetailChartData !== "undefined" && currentDetailChartData?.symbol === wanted && document.querySelector("#priceChart canvas"), symbol);
        assert.ok((await page.locator("#modalTitle").innerText()).includes(symbol));
    };
    const nav = (name) => page.getByRole("navigation", { name: "日常入口" }).getByRole("button", { name, exact: true });
    try {
        const identity = await context.request.get(`${preview.href}__ui_fixture_identity`);
        assert.deepEqual((await identity.json()).source_hashes, manifest.source_hashes);
        await page.goto(preview.href);
        await page.locator("#uiFixtureNotice").waitFor();
        for (const name of ["今天", "看盘", "两本账", "复盘学习"]) assert.equal(await nav(name).count(), 1);
        await page.waitForFunction(() => document.getElementById("kpiAiValue")?.textContent.includes("120,000"));
        await snapshot("today-1440x900");

        await nav("看盘").click();
        controls.delayFirst = true;
        await page.locator("#dailyFocusGrid").getByRole("button").filter({ hasText: "990001" }).click();
        await page.getByRole("button", { name: "下一只", exact: true }).click();
        await stockIs("990002");
        while (!controls.delayedDone) await page.waitForTimeout(25);
        await stockIs("990002");
        controls.delayFirst = false;
        await page.getByRole("button", { name: "上一只", exact: true }).click();
        await stockIs("990001");
        await page.waitForFunction(() => document.getElementById("workbenchEventList")?.textContent.includes("strategy-raw"));
        await page.reload();
        await stockIs("990001");
        await page.waitForFunction(() => document.getElementById("workbenchEventList")?.textContent.includes("strategy-raw"));
        await page.waitForFunction(() => !document.getElementById("workbenchNext").disabled);
        assert.match(await page.locator("#modalTitle").innerText(), /隔离夹具/);
        await page.getByRole("button", { name: "下一只", exact: true }).click();
        await stockIs("990002");
        await page.getByRole("button", { name: "上一只", exact: true }).click();
        await stockIs("990001");
        const plotted = await page.evaluate(() => echarts.getInstanceByDom(document.getElementById("priceChart")).getOption().series.filter((series) => series.type === "scatter").flatMap((series) => series.data));
        assert.equal(plotted.filter((item) => item.kind === "paper_fill").length, 3);
        assert.ok(plotted.some((item) => item.event_id === "ai-fill-1" && item.value[1] === 12.34));
        assert.ok(plotted.some((item) => item.kind === "ai_plan"));
        assert.ok(plotted.some((item) => item.kind === "rule_signal"));
        assert.ok(plotted.every((item) => item.symbol !== "990001"), "stock code must not override the marker shape");
        assert.ok(!plotted.some((item) => ["strategy-raw", "ai-unknown"].includes(item.event_id)));
        assert.match(await page.locator("#workbenchEventList").innerText(), /价格口径不一致/);
        await snapshot("watch-events-1440x900");
        await page.getByRole("button", { name: "返回列表", exact: true }).click();
        assert.equal(new URL(page.url()).hash, "#watch");

        await page.getByPlaceholder("例如 000802 或 北京文化").fill("990003");
        await page.getByRole("button", { name: "查询", exact: true }).click();
        await page.locator("#stockSearchResults").getByRole("button").click();
        await stockIs("990003");
        assert.match(await page.locator("#modalSubtitle").innerText(), /股票搜索/);
        await page.goBack();
        await page.locator("#detailModal").waitFor({ state: "hidden" });
        assert.equal(await page.locator("#stockSearchInput").inputValue(), "990003");
        await page.goForward();
        await stockIs("990003");
        await page.getByRole("button", { name: "返回列表", exact: true }).click();
        assert.equal(await page.locator("#stockSearchInput").inputValue(), "990003");

        await nav("两本账").click();
        await page.locator("#paperAccountSummary").filter({ hasText: "101,234" }).waitFor();
        await page.locator('[data-position-source="strategy_positions"][data-position-symbol="990001"]').click();
        await stockIs("990001");
        assert.match(await page.locator("#modalSubtitle").innerText(), /策略账持仓/);
        await page.getByRole("button", { name: "返回列表", exact: true }).click();
        assert.equal(new URL(page.url()).hash, "#paper-watchlist-section");
        await snapshot("strategy-ledger-1440x900");
        await page.locator("#paper-watchlist-section").getByRole("button", { name: "AI账", exact: true }).click();
        await page.locator("#aiTraderAccountPanel").filter({ hasText: "120,000" }).waitFor();
        await page.locator('[data-ai-position-symbol="990001"]').click();
        await stockIs("990001");
        await page.reload();
        await stockIs("990001");
        await page.waitForFunction(() => !document.getElementById("workbenchNext").disabled);
        await page.getByRole("button", { name: "下一只", exact: true }).click();
        await stockIs("990003");
        assert.match(await page.locator("#modalSubtitle").innerText(), /AI 账持仓/);
        await page.getByRole("button", { name: "返回列表", exact: true }).click();
        assert.equal(new URL(page.url()).hash, "#ai-trader-section");
        await snapshot("ai-ledger-1440x900");
        controls.missingAi = true;
        await page.locator("#ai-trader-section").getByRole("button", { name: "刷新数据", exact: true }).click();
        await page.locator("#aiTraderPositionsPanel").filter({ hasText: "持仓状态未知" }).waitFor();
        assert.ok(!(await page.locator("#aiTraderAccountPanel").innerText()).includes("120,000"));
        await snapshot("ai-ledger-missing-1440x900");
        controls.missingAi = false;

        await nav("复盘学习").click();
        await page.locator("#decisionHistoryPanel").filter({ hasText: "C 线完整决策核验记录未接入" }).waitFor();
        assert.equal(await page.locator("#decisionHistoryPanel img").count(), 0);
        await page.locator('[data-review-bucket="all"]').click();
        await page.locator('[data-review-symbol="990002"]').click();
        await stockIs("990002");
        await page.getByRole("button", { name: "返回列表", exact: true }).click();
        assert.equal(await page.locator('[data-review-bucket="all"]').getAttribute("aria-current"), "page");
        for (let i = 0; i < 3; i += 1) {
            await page.getByRole("button", { name: "刷新决策历史", exact: true }).click();
            await page.locator('[data-review-bucket="all"]').waitFor();
        }
        const beforeOpen = requests.filter((item) => item.path.startsWith("/api/stock/990002/detail")).length;
        await page.locator('[data-review-symbol="990002"]').click();
        await stockIs("990002");
        assert.equal(requests.filter((item) => item.path.startsWith("/api/stock/990002/detail")).length - beforeOpen, 1);
        await page.getByRole("button", { name: "返回列表", exact: true }).click();
        await snapshot("review-1440x900");
        controls.failAi = true; controls.failBoard = true;
        await page.getByRole("button", { name: "刷新决策历史", exact: true }).click();
        await page.locator("#decisionHistoryPanel").filter({ hasText: "记录数量未知" }).waitFor();
        await snapshot("review-unavailable-1440x900");
        controls.failAi = false; controls.failBoard = false;

        for (const [width, height] of [[1280, 800], [390, 844], [360, 800]]) {
            await page.setViewportSize({ width, height });
            await nav("今天").click();
            assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `page overflow at ${width}`);
            await snapshot(`today-${width}x${height}`);
            await nav("看盘").click();
            await page.locator("#dailyFocusGrid").getByRole("button").filter({ hasText: "990001" }).click();
            await stockIs("990001");
            assert.ok(await page.locator("#priceChart").evaluate((node) => node.getBoundingClientRect().width > 200), `chart readable at ${width}`);
            assert.ok(await page.locator("#detailModal").evaluate((node) => node.scrollWidth <= window.innerWidth + 1), `modal overflow at ${width}`);
            await snapshot(`watch-${width}x${height}`);
            await page.getByRole("button", { name: "返回列表", exact: true }).click();
        }
        assert.deepEqual(blocked, [], "ordinary viewing attempted a write or external request");
        assert.deepEqual(pageErrors, [], "uncaught browser errors");
        assert.equal(await page.evaluate(() => window.__uiInjected === true), false);
        fs.writeFileSync(path.join(evidence, "browser-result.json"), JSON.stringify({ status: "passed", fixture_only: true, manifest, executablePath, screenshots, requests, blocked, pageErrors }, null, 2));
        console.log(JSON.stringify({ status: "passed", screenshots: screenshots.length, requests: requests.length, blocked: blocked.length, pageErrors: pageErrors.length }));
    } catch (error) {
        await snapshot("browser-failure");
        fs.writeFileSync(path.join(evidence, "browser-result.json"), JSON.stringify({ status: "failed", error: String(error), manifest, requests, blocked, pageErrors, screenshots }, null, 2));
        throw error;
    } finally {
        await context.tracing.stop({ path: path.join(evidence, "browser-trace.zip") });
        await browser.close();
    }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
