let currentCandidates = [];
let currentTodayRecommendations = [];
let currentObservationCandidates = [];
let activeCharts = [];
const homeCharts = {};
let realtimeRefreshTimer = null;
let startupStatusPollTimer = null;
let currentDetailChartData = null;
let currentDetailResearchReference = null;
let detailRequestSequence = 0;
let currentWorkbenchContext = null;
const workbenchSourceLists = Object.create(null);
let currentStockSearchRows = [];
let lastCandidatesReadSucceeded = false;
let lastMarketReadSucceeded = false;
let visiblePageReadSequence = 0;
let visiblePageReadTail = Promise.resolve();
let visiblePageDataTarget = "";
let decisionHistoryRequestSequence = 0;
let currentReviewBucket = "mismatch";
let agentChatHistory = [];
let agentStatusLoaded = false;
let pendingAgentCandidate = null;
let lastAgentResearchOpinion = "";
let lastAgentPaperDecision = null;
let startupRefreshApplied = false;
let startupRefreshTaskSeen = false;
let startupRefreshTaskId = "";
let startupRefreshPendingRequestId = "";
let startupStatusObserved = false;
let researchLoopRefreshManual = false;
const SIDEBAR_COLLAPSE_KEY = "quantdesk_daily_sidebar_collapsed";
// Detail-modal navigation. Each entry is a *group* shown as one tab; `members`
// lists the underlying content panes (built in renderDetailContent /
// renderRealtimeDetailContent) that are merged into that one tab. Grouping keeps
// related sections together instead of splitting the detail into many tabs.
const DETAIL_NAV_SECTIONS = {
    candidate: [
        { id: "charts", title: "看盘", summary: "大 K 线、成交量和当前判断。", members: ["charts", "brief"] },
        { id: "overview", title: "判断详情", summary: "状态、价位、优先级与风险。", members: ["overview", "risk", "scenario"] },
        { id: "logic", title: "入选逻辑", summary: "为什么选、指标解读，以及现在是否还成立。", members: ["signals", "tracking"] },
        { id: "context", title: "线索 / 操作", summary: "新闻行业对比，并决定保留 / 转观察 / 移出。", members: ["context", "actions"] },
    ],
    realtime: [
        { id: "overview", title: "实时总览", summary: "快照状态、正式日线边界与风险提示。", members: ["overview", "risk"] },
        { id: "analysis", title: "观察分析", summary: "盘中观察逻辑、技术指标与研究参考。", members: ["scenario"] },
        { id: "charts", title: "图表拆解", summary: "价格量能 MACD/KDJ/RSI 分开看。", members: ["charts"] },
        { id: "context", title: "线索 / 操作", summary: "新闻行业背景，并决定是否加入观察池。", members: ["context", "actions"] },
    ],
};

document.addEventListener("DOMContentLoaded", () => {
    setupRuntimeCapabilities();
    setupPersonalWatchlist();
    setupModal();
    setupCandidateUpdate();
    setupSectorRefresh();
    setupRealtimeObservation();
    setupLimitUpReview();
    setupThemeMapTools();
    setupSidebarCollapse();
    setupProjectStatus();
    prepareVisiblePagePlaceholders();
    setupModuleNavigation();
    setupAiFundManagerLifecycle();
    setupDataOps();
    setupDataHealthHome();
    setupForwardPitHome();
    setupPaperWatchlist();
    setupPerformance();
    setupStrategyLab();
    setupRiskNotifications();
    setupSmartStop();
    setupNotificationCenter();
    setupBacktest();
    setupAgentAssistant();
    setupAiTraderPanel();
    setupDutyStrip();
    window.addEventListener("resize", resizeActiveCharts, { passive: true });
    window.setInterval(() => {
        if (document.body.dataset.page === "watch" && document.hidden !== true) loadRealtimeMarket(false);
    }, 300000);
    // Opening the viewer only reads local state. The refresh button owns writes.
});

function prepareVisiblePagePlaceholders() {
    const notes = {
        kpiPrevBatchSub: "候选与两本账读取后，再核对成绩单；可进入复盘查看。",
        kpiDataHealthSub: "候选与两本账读取后，再核对数据健康。",
        homeSectorNote: "行业板块资料在看盘入口读取；行情日尚未核实。",
        homeSectorChartEmpty: "进入看盘后读取板块资料，已有日期与结果会保留。",
        productIntegrityPanel: "候选与两本账读取后，再核查账本和研究证据。",
    };
    for (const [id, value] of Object.entries(notes)) {
        const node = document.getElementById(id);
        if (node) node.textContent = value;
    }
    for (const node of document.querySelectorAll("[data-module-view] .empty-state, [data-module-view] .section-note, #forwardPitHomePanel .empty-state, #dataHealthHomePanel .empty-state")) {
        if (/^(?:正在(?:读取|加载)|读取中)/.test((node.textContent || "").trim())) node.textContent = "未读取；进入对应页面或点击刷新查看。";
    }
}

function visiblePageReadStatus(loader, started) {
    const key = loader === loadForwardPitHome ? "t0" : loader === loadDataHealth || loader === loadDataHealthHome ? "data"
        : loader === loadStartupUpdateStatus || loader === loadCandidates ? "batch" : loader === loadMarketBrainHome ? "brain"
            : loader === loadHomeEquity ? "paper" : "";
    if (key) {
        if (started) feedDutyLamp(key, "读取中", "正在读取本页本地资料", "warn");
        else if (dutyState[key]?.value === "读取中") feedDutyLamp(key, "未核实", "本次未取得状态；可进入对应页面重试", "warn");
    }
    if (started) {
        const id = loader === loadStrategyScorecard ? "kpiPrevBatchSub" : loader === loadDataHealth || loader === loadDataHealthHome ? "kpiDataHealthSub" : "";
        const node = id && document.getElementById(id);
        if (node && /读取后|未读取/.test(node.textContent || "")) node.textContent = "正在读取本地资料；失败后可进入对应页面重试。";
    }
}

function visiblePageLoaders(targetId = "") {
    const watch = [loadCandidates, loadMarketOverview, () => loadRealtimeMarket(false)];
    return {
        "": [loadProjectStatus, loadCandidates, loadHomeEquity, loadHomeAiLedger, loadStartupUpdateStatus,
            loadMarketBrainHome, loadProductIntegrity, loadDataHealthHome, loadStrategyScorecard, loadNewsStatus],
        __watch__: watch,
        "daily-focus-section": watch,
        __system__: [loadProjectStatus, loadDataHealth, loadForwardPitHome],
        "data-ops-section": [loadProjectStatus, loadDataHealth, loadForwardPitHome],
        "ai-log-section": [loadAiFundDailyLog],
        "decision-history-section": [loadReviewLearning],
        "candidate-pool-section": [loadCandidates],
        "quant-summary-section": [loadCandidates],
        "limit-review-section": [() => loadLimitUpReview(false)],
        "theme-map-section": [loadThemeMapStatus],
        "agent-section": [loadAgentAssistant],
        "ai-trader-section": [loadAiTraderPanel],
        "paper-watchlist-section": [loadPaperWatchlist],
        "strategy-lab-section": [loadStrategyLab],
        "performance-section": [loadStrategyScorecard, loadStrategyBrainShadow],
        "risk-notifications-section": [loadRiskNotifications],
        "smart-stop-section": [loadSmartStop],
        "notification-center-section": [loadNotificationCenter],
        "backtest-section": [loadBacktestModule],
    }[targetId] || [];
}

function runVisiblePageReads(loaders) {
    const sequence = ++visiblePageReadSequence;
    return (async () => {
        for (const loader of loaders) {
            if (sequence !== visiblePageReadSequence) return;
            const read = visiblePageReadTail.catch(() => {}).then(() => {
                if (sequence !== visiblePageReadSequence) return;
                visiblePageReadStatus(loader, true);
                return Promise.resolve().then(loader).finally(() => visiblePageReadStatus(loader, false));
            });
            visiblePageReadTail = read.catch(() => {});
            try { await read; }
            catch (error) { console.warn("[page] read failed; retry remains available", error); }
        }
    })();
}

function loadVisiblePageData(targetId = visiblePageDataTarget) {
    visiblePageDataTarget = targetId;
    return runVisiblePageReads(visiblePageLoaders(targetId));
}

const pendingReadRequests = new Map();
const pendingReadControllers = new Map();
function requestJson(url, options = {}) {
    let key = String(url);
    if (/^\/api\/data-health\/dashboard(?:\?|$)/.test(key) && !/[?&]details(?:=|&|$)/.test(key)) {
        key = key.replace(/([?&])summary=[^&]*&?/, "$1").replace(/[?&]$/, "");
        key += (key.includes("?") ? "&" : "?") + "summary=true";
    }
    const cacheKey = options.responseEnvelope === true ? `response:${key}` : key;
    if (!options.signal && pendingReadRequests.has(cacheKey)) return pendingReadRequests.get(cacheKey);
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const setReadTimer = typeof window.setTimeout === "function" ? window.setTimeout.bind(window)
        : typeof setTimeout === "function" ? setTimeout : null;
    const clearReadTimer = typeof window.clearTimeout === "function" ? window.clearTimeout.bind(window)
        : typeof clearTimeout === "function" ? clearTimeout : null;
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Math.max(1, options.timeoutMs) : 20000;
    let timer, rejectCancelled;
    const cancelled = new Promise((_, reject) => { rejectCancelled = reject; });
    const aborted = () => {
        rejectCancelled(new Error("读取已取消，可重新读取；上次结果不代表当前数据。"));
        controller?.abort();
    };
    if (options.signal?.aborted) aborted();
    else options.signal?.addEventListener("abort", aborted, { once: true });
    if (setReadTimer) timer = setReadTimer(() => {
        rejectCancelled(new Error(`读取超过 ${Math.ceil(timeoutMs / 1000)} 秒，请重试；上次结果的日期保持不变。`));
        controller?.abort();
    }, timeoutMs);
    const read = options.signal?.aborted ? cancelled : (async () => {
        const response = await fetch(key, { headers: { "Accept": "application/json", ...options.headers },
            signal: controller?.signal || options.signal });
        if (options.responseEnvelope === true) {
            let payload = null;
            try { payload = await response.json(); } catch (error) { payload = null; }
            return { ok: response.ok, status: response.status, payload };
        }
        if (!response.ok) throw new Error((await response.text()) || `请求失败：${response.status}`);
        return response.json();
    })();
    const promise = Promise.race([read, cancelled]);
    if (!options.signal) {
        pendingReadRequests.set(cacheKey, promise);
        pendingReadControllers.set(cacheKey, { abort: aborted });
    }
    promise.finally(() => {
        if (timer !== undefined && clearReadTimer) clearReadTimer(timer);
        options.signal?.removeEventListener("abort", aborted);
        if (pendingReadRequests.get(cacheKey) === promise) {
            pendingReadRequests.delete(cacheKey);
            pendingReadControllers.delete(cacheKey);
        }
    }).catch(() => {});
    return promise;
}

function cancelPendingReads() {
    for (const controller of pendingReadControllers.values()) controller.abort();
}

const readRenderSequences = new Map();
function beginReadRender(name) {
    const sequence = (readRenderSequences.get(name) || 0) + 1;
    readRenderSequences.set(name, sequence);
    return () => readRenderSequences.get(name) === sequence;
}

async function postJson(url, payload = {}) {
    assertRuntimeMutationAllowed(url);
    const response = await fetch(url, {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `请求失败：${response.status}`);
    }
    return response.json();
}

const RUNTIME_WRITE_SELECTOR = [
    "refreshResearchLoopButton", "globalUpdateButton", "refreshSectorButton", "refreshRealtimeButton",
    "refreshLimitReviewButton", "rebuildThemeMapLocalButton", "refreshThemeMapNetworkButton",
    "generateThemeMapDraftButton", "importThemeMapButton", "updateCandidatesButton",
    "generateRecommendationsButton", "executeStrategyButton", "addDiscussedCandidateToPaperButton",
    "sendAgentQuestionButton", "sendAiTraderChatButton", "updateAllDataButton", "updateCandidatesDailyButton",
    "syncPaperWatchlistButton", "refreshPaperWatchlistButton", "syncPaperCandidatesButton",
    "refreshPaperSimulationButton", "rebuildPerformanceButton", "refreshPaperRiskButton",
    "refreshSmartStopButton", "runBacktestButton", "runStrategyScreeningButton", "confirmPaperExitReviewButton",
].map((id) => "#" + id).join(",") + ",[data-paper-buy],[data-paper-buy-all],[data-paper-action],[data-candidate-action]:not([data-candidate-action=discuss]),[data-realtime-watch]";

function runtimeMutationAllowed(url) {
    const capabilities = window.quantdeskRuntimeCapabilities;
    if (/^\/api\/stock\/\d{6}\/quote-refresh$/.test(String(url))) return capabilities?.selected_quote_refresh === true;
    return capabilities?.mutations_enabled === true && capabilities?.read_only === false;
}

function assertRuntimeMutationAllowed(url) {
    if (!runtimeMutationAllowed(url)) throw new Error("当前为只读研究模式，这项写入操作未启用；可以继续查看已有资料。");
}

function applyRuntimeCapabilities() {
    const capabilities = window.quantdeskRuntimeCapabilities;
    const allowed = capabilities?.mutations_enabled === true && capabilities?.read_only === false;
    for (const button of document.querySelectorAll(RUNTIME_WRITE_SELECTOR)) {
        if (!allowed) {
            if (!button.disabled) button.disabled = true;
            if (button.dataset.runtimeDisabled !== "true") {
                button.dataset.runtimeDisabled = "true";
                button.dataset.runtimeOriginalTitle = button.title || "";
                button.title = "只读研究模式：此操作未启用，不会写入账本或重新计算报告。";
                button.setAttribute("aria-disabled", "true");
            }
        } else if (button.dataset.runtimeDisabled === "true") {
            button.disabled = false;
            button.title = button.dataset.runtimeOriginalTitle || "";
            delete button.dataset.runtimeDisabled;
            button.removeAttribute("aria-disabled");
        }
    }
    const note = document.getElementById("runtimeModeNote");
    const modeText = capabilities
        ? `${allowed ? "研究编辑模式" : "只读研究模式"} · 纸面未验证 · 不接券商 · ${capabilities.selected_quote_refresh === true ? "当前股票报价可由你开启" : "当前股票联网报价未启用"}`
        : "正在核实运行权限；已有资料可查看，写入操作暂不可用。";
    if (note && note.textContent !== modeText) note.textContent = modeText;
}

function setupRuntimeCapabilities() {
    window.quantdeskRuntimeCapabilities = null;
    applyRuntimeCapabilities();
    // Re-rendered action buttons and finally blocks must retain the service mode.
    if (typeof MutationObserver === "function") {
        const observer = new MutationObserver(applyRuntimeCapabilities);
        observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    }
    window.addEventListener("pagehide", cancelPendingReads);
    document.addEventListener("click", (event) => {
        const button = event.target.closest?.(RUNTIME_WRITE_SELECTOR);
        if (button && !(window.quantdeskRuntimeCapabilities?.mutations_enabled === true && window.quantdeskRuntimeCapabilities?.read_only === false)) {
            event.preventDefault(); event.stopImmediatePropagation();
        }
    }, true);
    requestJson("/api/runtime-capabilities").then((value) => {
        window.quantdeskRuntimeCapabilities = value;
        applyRuntimeCapabilities();
        stockWorkbenchController?.remount();
    }).catch(() => {
        const note = document.getElementById("runtimeModeNote");
        if (note) note.textContent = "运行权限读取失败；保留只读查看，写入操作未开启。";
    });
}

const PERSONAL_WATCHLIST_KEY = "quantdesk_personal_watchlist_v1";
function personalWatchlist() {
    try {
        const value = JSON.parse(localStorage.getItem(PERSONAL_WATCHLIST_KEY) || "[]");
        return Array.isArray(value) ? [...new Set(value.filter((symbol) => typeof symbol === "string" && /^\d{6}$/.test(symbol)))].slice(0, 50) : [];
    } catch { return []; }
}

function setPersonalWatch(symbol, keep = true) {
    if (!/^\d{6}$/.test(String(symbol))) return false;
    let values = personalWatchlist().filter((value) => value !== symbol);
    if (keep) values = [symbol, ...values].slice(0, 50);
    try { localStorage.setItem(PERSONAL_WATCHLIST_KEY, JSON.stringify(values)); }
    catch {
        const note = document.getElementById("personalWatchlistNote");
        if (note) note.textContent = "浏览器存储不可用，本次未保存；程序候选和账本均未改变。";
        return false;
    }
    renderPersonalWatchlist();
    return true;
}

function renderPersonalWatchlist() {
    const panel = document.getElementById("personalWatchlistItems");
    if (!panel) return;
    const rows = personalWatchlist();
    panel.innerHTML = rows.length ? rows.map((symbol) => `<span class="personal-watch-chip"><button type="button" data-personal-open="${symbol}">${symbol}</button><button type="button" data-personal-remove="${symbol}" aria-label="从本机自选移除${symbol}">×</button></span>`).join("")
        : '<span class="section-note">还没有本机自选，可输入六位代码或在个股页加入。</span>';
}

function setupPersonalWatchlist() {
    renderPersonalWatchlist();
    document.getElementById("personalWatchlistAddButton")?.addEventListener("click", () => {
        const symbol = document.getElementById("personalWatchlistInput")?.value.trim();
        const note = document.getElementById("personalWatchlistNote");
        if (!setPersonalWatch(symbol) && note && !/^\d{6}$/.test(symbol || "")) note.textContent = "请输入六位股票代码；这只保存本机关注偏好，不加入程序候选或纸面持仓。";
    });
    document.addEventListener("click", (event) => {
        const button = event.target.closest?.("[data-personal-open],[data-personal-add],[data-personal-remove]");
        if (!button) return;
        if (button.dataset.personalOpen) showRealtimeStockDetail(button.dataset.personalOpen, "");
        if (button.dataset.personalAdd) {
            if (setPersonalWatch(button.dataset.personalAdd)) button.textContent = "已加入本机自选";
        }
        if (button.dataset.personalRemove) setPersonalWatch(button.dataset.personalRemove, false);
    });
}

function productIntegrityHtml(data) {
    const report = data?.report || {};
    const history = report.history || {}, identity = report.pnl_identity || {};
    const corporate = report.corporate_action_evidence || {}, industry = report.industry_coverage || {};
    const warnings = report.warnings || {}, loss = report.loss_contribution || {};
    const value = (item) => item === null || item === undefined || item === "" ? "未核实" : String(item);
    const count = (items) => Array.isArray(items) ? items.length : "未核实";
    const names = (items) => Array.isArray(items) ? items.join("、") || "无已记录项" : "未核实";
    const money = (item) => typeof item === "number" && Number.isFinite(item) ? item.toFixed(2) + "元" : "未核实";
    const identityText = identity.status === "verified" ? "已按现有证据核对" : identity.status === "mismatch" ? "仍有差额，尚未核对" : "证据不足，尚未核对";
    const card = (label, main, detail) => `<article class="product-integrity-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(main)}</strong><p>${escapeHtml(detail)}</p></article>`;
    const evidence = data?.research_evidence || {};
    const losses = Array.isArray(loss.top_losses) ? loss.top_losses.slice(0, 5) : [];
    return `<p class="section-note">估值截至 ${escapeHtml(value(report.valuation_date || data?.as_of))} · 只读核查 · 纸面未验证</p><div class="product-integrity-grid">`
        + card("策略账核对", identityText, `纸面权益与盈亏差额 ${money(identity.difference)}；研究未验证，历史金额保持原样。`)
        + card("净值记录", `${value(history.unique_dates)}个日期 / ${value(history.row_count)}条`, `起于 ${value(history.first_date)}；缺日 ${count(history.missing_dates)}，重复日期 ${count(history.duplicate_dates)}；${history.continuous_statistics_eligible === true ? "区间记录符合连续统计条件" : "完整连续收益统计仍不可用"}。`)
        + card("行业与公司行动", `行业已知 ${value(industry.known)}/${value(industry.positions)}`, `复权证据陈旧 ${count(corporate.stale_symbols)}，未知 ${count(corporate.unknown_symbols)}；总回报尚未证明完整。`)
        + '</div><details class="owner-source-details"><summary>查看差额、损益贡献与研究证据</summary>'
        + `<p>缺失交易日：${escapeHtml(names(history.missing_dates))}；重复日期：${escapeHtml(names(history.duplicate_dates))}。</p>`
        + `<p>历史本金/成本差额：${escapeHtml(report.capital_cost_bridge?.cost_basis_explanation?.message || report.capital_cost_bridge?.message || "来源尚待追溯，不以调平项抹去。")}</p>`
        + capitalCostExplanationHtml(report.capital_cost_bridge?.cost_basis_explanation)
        + `<p>已平仓损益 ${escapeHtml(money(loss.closed_realized?.amount))}；持仓浮动损益 ${escapeHtml(money(loss.held_unrealized?.amount))}；历史调整 ${escapeHtml(money(loss.legacy_adjustment))}；原账待核差额 ${escapeHtml(money(loss.unexplained_difference))}。这是金额贡献，不是因果归因。</p>`
        + (losses.length ? `<p>主要已记录亏损：${losses.map((row) => `${escapeHtml(value(row.symbol))}（${escapeHtml(value(row.exit_date))}）${escapeHtml(money(row.realized_pnl))}`).join("；")}。</p>` : "")
        + `<p>当前警告 ${escapeHtml(value(warnings.current_count))}；历史修复记录 ${escapeHtml(value(warnings.historical_count))}。历史修复记录不代表今天新增故障。</p>`
        + historicalHoldingPeriodsHtml(report.closed_holding_periods)
        + researchEvidenceHtml(evidence)
        + `<p>${escapeHtml(report.learning?.summary || "复盘时逐笔对照原记录的理由、失效条件和实际结果。")}</p>`
        + '<p>复盘时逐笔对照事前理由、失效条件和实际结果；历史未记录的判断不能事后补成预判，样本不足时不总结成功模式。</p></details>';
}

function capitalCostExplanationHtml(explanation) {
    if (!explanation || typeof explanation !== "object") return "";
    const fact = (value) => recordedEvidenceText(value, "未记录");
    const money = (value) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) + "元" : "未核实";
    const cost = (value) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) + "元" : "未核实";
    const rows = Array.isArray(explanation.candidates) ? explanation.candidates.filter((row) => row && typeof row === "object") : [];
    const explained = explanation.status === "explains_reported_difference" && explanation.explains_reported_difference === true;
    const cashflow = explanation.full_historical_cashflow_verified === true
        ? "来源报告标注现金流核验；本页未独立复核" : "完整历史现金流仍未核验";
    const details = rows.map((row) => {
        const original = row.original_values || {}, fields = row.source_fields || {};
        const group = row.record_group === "positions" ? "当前持仓记录" : row.record_group === "closed_positions" ? "历史平仓记录" : "记录组未核实";
        return `<article class="weekly-loss-example"><strong>${escapeHtml(fact(row.symbol))} · ${group}</strong>`
            + `<p>起于 ${escapeHtml(fact(row.entry_date))}；退出日 ${escapeHtml(fact(row.exit_date))}。来源 ${escapeHtml(fact(row.record_group))}[${escapeHtml(fact(row.source_index))}]；数量字段 ${escapeHtml(fact(fields.quantity))}；金额字段 ${escapeHtml(fact(fields.amount))}；纸面盈亏字段 ${escapeHtml(fact(fields.pnl))}。</p>`
            + `<p>原数量 ${escapeHtml(fact(original.quantity))}；原entry_price ${escapeHtml(cost(original.entry_price))}；原avg_cost ${escapeHtml(cost(original.avg_cost))}；原金额 ${escapeHtml(money(original.amount))}；原纸面盈亏 ${escapeHtml(money(original.recorded_pnl))}。</p>`
            + `<p>按entry_price只读派生 ${escapeHtml(money(row.pnl_using_entry_price))}；按avg_cost只读派生 ${escapeHtml(money(row.pnl_using_avg_cost))}；候选口径差量 ${escapeHtml(money(row.alternative_pnl_delta))}。原账本金额不替换。</p></article>`;
    }).join("");
    return '<details class="owner-source-details"><summary>纸面成本字段冲突与差额来源</summary>'
        + `<p>${explained ? "已解释报告差额" : "报告差额仅有部分或未取得成本解释"}；${escapeHtml(cashflow)}。纸面研究 · 未验证，不代表账本已修正或已完成全账核对。</p>`
        + `<p>原报告差额 ${escapeHtml(money(explanation.reported_pnl_difference))}；候选口径差量合计 ${escapeHtml(money(explanation.candidate_adjustment_sum))}；相对该报告恒等式的剩余差额 ${escapeHtml(money(explanation.remaining_reported_difference))}。这不是完整现金流重建。</p>`
        + (details || '<p>成本冲突逐笔来源未提供，不能补造调整项。</p>')
        + '</details>';
}

function historicalHoldingPeriodsHtml(records) {
    const rows = Array.isArray(records) ? records.filter((row) => row && row.recorded_zero_conflicts_with_dates === true) : [];
    const fact = (value) => recordedEvidenceText(value, "未核实");
    const days = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? String(value) : "未核实";
    const details = rows.slice(0, 18).map((row) => {
        const calendarVerified = row.status === "derived_from_local_calendar"
            && typeof row.elapsed_trading_days === "number" && Number.isFinite(row.elapsed_trading_days) && row.elapsed_trading_days >= 0;
        const elapsed = calendarVerified ? `本地交易日历派生 ${days(row.elapsed_trading_days)}个交易日`
            : `交易日历未核实，交易日数未知；日期间隔 ${days(row.elapsed_calendar_days)}天`;
        return `<li><strong>${escapeHtml(fact(row.symbol))}</strong> · ${escapeHtml(fact(row.entry_date))} 至 ${escapeHtml(fact(row.exit_date))}`
            + `<p>原记录 ${escapeHtml(days(row.recorded_holding_days))}天；${escapeHtml(elapsed)}。</p>`
            + `<p>来源：策略账原平仓记录；${calendarVerified ? "交易日数按本地交易日历只读派生" : "仅保留原日期与日历间隔，交易日历来源尚未核实"}。</p></li>`;
    }).join("");
    return '<details class="owner-source-details"><summary>历史持有期核对'
        + (rows.length ? ` · ${rows.length}条已记录冲突${rows.length > 18 ? "（展示前18条）" : ""}` : '')
        + '</summary><p>纸面研究 · 未验证。这里是只读派生更正，原持有天数和原说明保留，不倒写历史记录；盈亏不能代替判断对错。</p>'
        + (details ? `<ul>${details}</ul>` : `<p>${Array.isArray(records) ? "当前来源未返回明确的持有期冲突项，不能据此认定全部历史已核对。" : "逐笔持有期资料未提供，暂无法核对。"}</p>`)
        + '</details>';
}

function researchEvidenceHtml(evidence) {
    const fact = (value, fallback = "未记录") => {
        if (typeof value === "string" || typeof value === "number" && Number.isFinite(value)) return String(value) || fallback;
        return fallback;
    };
    const rule = (value) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) return fact(value);
        return [value.rule_id || value.id || value.name || value.strategy, value.version || value.rule_version]
            .map((item) => fact(item, "")).filter(Boolean).join(" · ") || "未记录";
    };
    const validation = (value) => value && typeof value === "object"
        ? fact(value.validation_message || value.message || value.label || value.status, "未建立") : fact(value, "未建立");
    return '<div class="research-evidence-records">' + [["backtest", "回测"], ["walk_forward", "滚动验证"], ["dashboard", "研究面板"]].map(([key, label]) => {
        const item = evidence?.[key] || {};
        return `<p><strong>${label}</strong> · 业务日 ${escapeHtml(fact(item.business_date))} · 规则 ${escapeHtml(rule(item.rule_identity))}<br>`
            + `${escapeHtml(fact(item.validation_message || item.label || item.status, "当前方法的验证证据未提供"))}<br>`
            + `流程状态 ${escapeHtml(fact(item.process_status))}；当前规则验证 ${escapeHtml(validation(item.current_rule_validation))}。</p>`;
    }).join("") + '<p>流程完成不代表投资优势成立，演示和不同规则不能互相背书。</p></div>';
}

function weeklyLearningHtml(learning) {
    const review = learning?.weekly_review || {};
    const hasWeeks = Array.isArray(review.weeks);
    const weeks = hasWeeks ? review.weeks.filter((week) => week && typeof week === "object" && !Array.isArray(week)) : [];
    const fact = (value) => (typeof value === "string" && value !== "" || typeof value === "number" && Number.isFinite(value)) ? String(value) : "未记录";
    const money = (value) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) + "元" : "未记录";
    const amount = (value) => {
        const item = value && typeof value === "object" ? value : {};
        const completeness = item.complete === true ? "金额字段完整" : item.complete === false ? "金额字段不完整" : "金额完整性未核实";
        return `${money(item.amount)}（金额有记录 ${fact(item.known_count)}/${fact(item.total_count)}笔；${completeness}）`;
    };
    const reason = (value) => value && value.status !== "unknown" && typeof value.text === "string" && value.text.trim()
        ? `${fact(value.text)}〔原字段 ${fact(value.source_field)}〕` : "原记录未提供，不能事后补成预判";
    const status = review.status === "partial" ? "证据部分可用" : review.status === "available" ? "原记录可读取" : review.status === "missing" ? "记录缺失" : "覆盖状态未核实";
    const checks = Array.isArray(learning?.checks) ? learning.checks : [];
    const html = weeks.map((week) => {
        const coverage = week.evidence_coverage || {}, holding = week.holding_period || {};
        const examples = Array.isArray(week.top_loss_examples) ? week.top_loss_examples.filter((row) => row && typeof row === "object") : [];
        const followUp = Array.isArray(week.follow_up_checks) ? week.follow_up_checks : [];
        return `<details class="weekly-review-card"><summary>${escapeHtml(fact(week.week_start))} 至 ${escapeHtml(fact(week.week_end))} · 已平仓 ${escapeHtml(fact(week.closed_trade_count))}笔 · 已记录损益 ${escapeHtml(money(week.realized_pnl?.amount))}</summary>`
            + `<p>已实现损益 ${escapeHtml(amount(week.realized_pnl))}；原记录退出费用 ${escapeHtml(amount(week.recorded_exit_fees))}。费用字段不完整时不能称总成本完整。</p>`
            + `<p>持有期：跨日却记0天 ${escapeHtml(fact(holding.zero_day_conflicts))}笔；日期缺失/无效 ${escapeHtml(fact(holding.missing_or_invalid_dates))}笔；交易日历未核实 ${escapeHtml(fact(holding.calendar_unverified_count))}笔；原持有天数为负 ${escapeHtml(fact(holding.negative_recorded_days))}笔。</p>`
            + `<p>证据字段覆盖（分母 ${escapeHtml(fact(coverage.total_count))}笔）：原入场理由 ${escapeHtml(fact(coverage.entry_reason_known))}；原失效条件 ${escapeHtml(fact(coverage.invalidation_condition_known))}；价格证据字段 ${escapeHtml(fact(coverage.price_evidence_present))}；计划ID ${escapeHtml(fact(coverage.plan_id_present))}；判断核对记录 ${escapeHtml(fact(coverage.judgment_review_recorded))}。字段存在不等于本页独立验证。</p>`
            + (examples.length ? '<h4>从已记录亏损逐笔核对</h4>' + examples.map((row) => `<article class="weekly-loss-example"><strong>${escapeHtml(fact(row.symbol))}</strong> · ${escapeHtml(fact(row.entry_date))} 至 ${escapeHtml(fact(row.exit_date))} · 已实现 ${escapeHtml(money(row.realized_pnl))}`
                + `<p>原入场理由：${escapeHtml(reason(row.original_entry_reason))}</p><p>原失效条件：${escapeHtml(reason(row.invalidation_condition))}</p><p>原退出理由：${escapeHtml(reason(row.original_exit_reason))}</p>`
                + `<p>已有核对记录：${escapeHtml(fact(row.judgment_evidence?.recorded_status || row.judgment_evidence?.status))}；本页未独立验证判断对错，不能由盈亏代替判断核验（纸面研究 · 未验证）。</p></article>`).join("")
                : '<p>本周未提供可展示的亏损例，不据此归纳成功模式。</p>')
            + (followUp.length ? '<ul>' + followUp.map((item) => `<li>${escapeHtml(fact(item))}</li>`).join("") + '</ul>' : '')
            + '</details>';
    }).join("");
    const undated = Array.isArray(review.undated_or_future_records) ? review.undated_or_future_records.length : "未核实";
    const weekCoverage = hasWeeks ? `${weeks.length}周可读取` : "周记录覆盖未核实";
    return `<p>${escapeHtml(typeof learning?.summary === "string" && learning.summary || "仅根据已有策略账平仓记录回顾；原理由和失效条件缺失时保留未知。")}</p>`
        + `<p class="section-note">策略账独立范围 · 资料截至 ${escapeHtml(fact(review.as_of))} · ${escapeHtml(status)} · ${escapeHtml(weekCoverage)}；无日期或晚于核查日的记录 ${escapeHtml(String(undated))}笔。纸面研究 · 未验证；本页不计算胜率，分周记录不代表完整每周绩效。</p>`
        + (hasWeeks && weeks.length !== review.weeks.length ? '<p class="section-note">周列表含无法读取的记录，覆盖尚不完整。</p>' : '')
        + '<p class="section-note">先对原理由、失效条件与实际退出，再看结果。亏损不自动证明原判断错误，盈利不自动证明方法有效；本页不自动改规则。</p>'
        + (checks.length ? '<ul>' + checks.map((item) => `<li>${escapeHtml(fact(item))}</li>`).join("") + '</ul>' : '')
        + (html || '<p>尚无可读取的分周记录；不补造历史判断或收益。</p>');
}

async function loadProductIntegrity() {
    const panel = document.getElementById("productIntegrityPanel");
    const weeklyPanel = document.getElementById("productWeeklyLearningPanel");
    if (!panel && !weeklyPanel) return;
    try {
        const data = await requestJson("/api/product-integrity");
        if (panel) panel.innerHTML = productIntegrityHtml(data);
        if (weeklyPanel) weeklyPanel.innerHTML = weeklyLearningHtml(data?.report?.learning);
    } catch {
        if (panel) panel.innerHTML = '<p class="section-note">账本与研究证据核查暂时无法读取；缺口仍未解决，不能据此认定正常。</p>';
        if (weeklyPanel) weeklyPanel.innerHTML = '<p class="section-note">每周回顾暂不可读，不据此认定没有亏损或没有判断错误。</p>';
    }
}

function loadReviewLearning() {
    return Promise.allSettled([loadDecisionHistory(), loadProductIntegrity()]);
}

// Reference levels currently drawn on the price chart, plus a guard so a late
// response from a previously opened stock can never paint on this chart.
let currentDetailReferenceLevels = null;
let currentDetailReferenceIdentity = "";
// When the open detail was last re-fetched; stale content must not stay silent.
let currentDetailFetchedAt = 0;
const DETAIL_FRESH_MS = 30000;

const stockWorkbenchController = window.QuantDeskStockWorkbench?.createController({
    get: (url, options) => requestJson(url, options),
    post: (url, payload) => postJson(url, payload),
    getNode: () => document.getElementById("stockQuoteWorkbench"),
    isHidden: () => document.hidden,
    canRefresh: () => window.quantdeskRuntimeCapabilities?.selected_quote_refresh === true,
    onState: (state) => applyReferenceOverlay(state),
});

function applyReferenceOverlay(state) {
    const symbol = String(state?.symbol || "");
    if (!symbol || symbol !== String(currentWorkbenchContext?.symbol || "")) return;
    const reference = state.reference || {};
    const levels = reference.available && Array.isArray(reference.levels) ? reference.levels : [];
    const identity = levels.map((level) => `${level.kind}:${level.price}`).join("|") + "|" + String(reference.as_of || "");
    if (identity === currentDetailReferenceIdentity) return;
    currentDetailReferenceIdentity = identity;
    currentDetailReferenceLevels = levels;
    const legend = document.getElementById("referenceOverlayLegend");
    if (legend) {
        legend.textContent = levels.length
            ? "算法参考位（纸面 · 未验证）已叠加到价格 K 线："
                + levels.map((level) => `${level.label || level.kind} ${Number(level.price).toFixed(2)}`).join(" · ")
                + ` · 日线计算截至 ${reference.as_of || "未核实"} · 触发仍需量能与收盘确认`
            : "当前没有可叠加的算法参考位（" + (reference.message || "日线资料不足") + "）。";
    }
    if (currentDetailChartData && currentDetailChartData.kline_data?.length) renderPriceChart(
        currentDetailChartData.kline_data,
        currentDetailChartData.kline_data.map((row) => safeText(row.date)),
        currentDetailChartData.kline_data.map((row) => {
            const values = [row.open, row.close, row.low, row.high].map((v) => numericValue(v));
            return values.includes(null) ? null : values;
        }),
        normalizeMarkers(currentDetailChartData, currentDetailChartData.kline_data.map((row) => safeText(row.date))),
    );
}

function stockWorkbenchPlaceholder(symbol) {
    if (!stockWorkbenchController) return "";
    return '<section id="stockQuoteWorkbench" class="stock-quote-workbench" data-stock-symbol="'
        + escapeHtml(symbol) + '">正在读取当前股票报价与算法参考…</section>';
}

function mountStockWorkbench(body, symbol) {
    if (!stockWorkbenchController) return;
    const host = body.querySelector(".detail-panel-area") || body;
    host.insertAdjacentHTML("afterbegin", stockWorkbenchPlaceholder(symbol));
    stockWorkbenchController.remount();
}

document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stockWorkbenchController?.suspend(); return; }
    stockWorkbenchController?.resume();
    // A tab that comes back after a while must not keep showing the older
    // session's detail panels; the workbench alone cannot refresh them.
    if (isDetailModalOpen() && currentWorkbenchContext?.symbol
        && Date.now() - currentDetailFetchedAt > DETAIL_FRESH_MS) {
        const context = currentWorkbenchContext;
        showDetail({ symbol: context.symbol, name: context.symbol }, {
            context, source: context.source, profile: context.profile,
            returnHash: context.returnHash, updateUrl: false,
        });
    }
});

async function loadMinutePanel(symbol) {
    const chart = document.getElementById("minuteChart");
    const note = document.getElementById("minuteChartNote");
    if (!chart) return;
    chart.innerHTML = "";
    try {
        const data = await requestJson(`/api/stock/${encodeURIComponent(String(symbol))}/minute`);
        if (!data || String(data.symbol) !== String(currentWorkbenchContext?.symbol || "")) return;
        const rows = Array.isArray(data.rows) ? data.rows : [];
        const coverage = data.coverage || {};
        if (note) note.textContent = rows.length
            ? `本地缓存最近交易日 ${safeText(data.date)} · ${rows.length} 根1分钟 · 缓存区间 ${safeText(coverage.first_date)} 至 ${safeText(coverage.last_date)} · 来源 ${(data.source || []).join("/") || "未记录"} · 口径 ${(data.price_basis || []).join("/") || "未记录"}`
            : safeText(data.message || "本地没有该股的1分钟缓存。");
        if (!rows.length) {
            chart.innerHTML = missingBlock(safeText(data.message || "本地没有该股的1分钟缓存；不补齐、不编造。"));
            return;
        }
        if (typeof echarts !== "object") {
            chart.innerHTML = missingBlock("本地 ECharts 文件缺失，分钟图无法绘制。");
            return;
        }
        const times = rows.map((row) => row.time);
        const closes = rows.map((row) => numericValue(row.close));
        const draw = () => {
            if (!chart.clientWidth || !chart.clientHeight) return false;
            const existing = echarts.getInstanceByDom(chart);
            const instance = existing || echarts.init(chart, null, { renderer: "canvas" });
            if (!existing) activeCharts.push(instance);
            instance.setOption(baseChartOption(times, {
                legend: { show: false },
                tooltip: { trigger: "axis" },
                yAxis: { scale: true },
                xAxis: { type: "category", boundaryGap: false, data: times },
                series: [
                    { name: "收盘", type: "line", showSymbol: false, data: closes, lineStyle: { width: 1.6, color: "#2563EB" } },
                    { name: "成交量", type: "bar", yAxisIndex: 0, data: rows.map((row) => numericValue(row.volume)), itemStyle: { color: "rgba(100,116,139,0.35)" } },
                ],
            }), true);
            return true;
        };
        // A pane that was hidden still needs a layout pass; retry across a few
        // frames instead of assuming the first measurement is final.
        let attempts = 0;
        const attempt = () => {
            if (draw()) return;
            if (attempts++ < 60) window.requestAnimationFrame(() => window.setTimeout(attempt, 50));
        };
        attempt();
        if (typeof ResizeObserver === "function") {
            const observer = new ResizeObserver(() => {
                if (draw()) observer.disconnect();
            });
            observer.observe(chart);
        }
    } catch (error) {
        if (note) note.textContent = "本地分钟缓存读取失败";
        chart.innerHTML = missingBlock(`分钟缓存读取失败：${escapeHtml(errorText(error))}；不补齐、不编造。`);
    }
}

function escapeHtml(value) {
    return String(value ?? "数据缺失")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

// 固定门：默认只开 http://127.0.0.1:8000/（launcher 默认不漂端口）。
const PROJECT_STATUS_DOOR = "http://127.0.0.1:8000/";
const PROJECT_STATUS_SERVICE_DOWN_HINT =
    `主服务可能没在运行：先双击项目根目录的 start_ui.cmd（或运行 python scripts/qd.py app），`
    + `等窗口里出现 ${PROJECT_STATUS_DOOR} 再刷新本页；默认只开 8000，不漂到别的端口。`;

function looksLikeServerUnreachable(error) {
    const text = errorText(error).toLowerCase();
    return ["failed to fetch", "fetch failed", "networkerror", "network error", "load failed", "econnrefused"]
        .some((token) => text.includes(token));
}

let projectStatusLastOk = null;
let projectStatusFullOpen = false;

function setupProjectStatus() {
    const refresh = document.getElementById("projectStatusRefreshButton");
    const openFull = document.getElementById("projectStatusOpenFullButton");
    if (refresh) {
        refresh.addEventListener("click", () => {
            loadProjectStatus();
        });
    }
    if (openFull) {
        openFull.addEventListener("click", () => {
            const panel = document.getElementById("projectStatusFull");
            if (!panel) return;
            projectStatusFullOpen = !projectStatusFullOpen;
            panel.hidden = !projectStatusFullOpen;
            openFull.textContent = projectStatusFullOpen ? "收起完整状态" : "查看完整状态";
        });
    }
    const openSystem = document.getElementById("projectStatusOpenSystemButton");
    if (openSystem) {
        openSystem.addEventListener("click", () => {
            if (typeof window.quantdeskShowPage === "function") {
                window.quantdeskShowPage("system");
            }
        });
    }
}

function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value == null || value === "" ? "未提供" : String(value);
}

function renderOwnerRuntimeSummary(observations) {
    const host = document.getElementById("projectRuntimeSummary");
    if (!host) return;
    if (!observations || typeof observations !== "object") {
        host.innerHTML = '<p class="section-note">尚未读取日常运行记录；不能据此判断模型成功或行情完整。</p>';
        return;
    }
    const snapshot = observations.snapshot || {};
    const receivedSlots = snapshot.received_slots ?? snapshot.collected_slots;
    const slotsKnown = Number.isInteger(receivedSlots) && Number.isInteger(snapshot.expected_slots)
        && receivedSlots >= 0 && snapshot.expected_slots > 0 && receivedSlots <= snapshot.expected_slots;
    const ratioKnown = typeof snapshot.valid_quote_ratio === "number"
        && Number.isFinite(snapshot.valid_quote_ratio) && snapshot.valid_quote_ratio >= 0 && snapshot.valid_quote_ratio <= 1;
    const quoteText = (ratioKnown ? `有效报价 ${(snapshot.valid_quote_ratio * 100).toFixed(2)}%` : "有效报价比例未知")
        + ` · 严格时段合格 ${snapshot.strict_qualified_slots ?? "未核实"} · 字段完整 ${snapshot.field_complete_slots ?? "未核实"}`;
    const slotsText = slotsKnown
        ? `已收到 ${receivedSlots} / ${snapshot.expected_slots} 份${receivedSlots < snapshot.expected_slots ? "（文件未齐）" : ""}`
        : "收到文件数量未知";
    const card = (label, stamp, main, badge, tone, detail) => `<article class="owner-runtime-card">
        <div class="owner-runtime-card-head"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(stamp || "日期未知")}</span></div>
        <div class="owner-runtime-value">${escapeHtml(main)}</div>
        <span class="owner-runtime-badge ${tone}">${escapeHtml(badge)}</span>
        <p>${escapeHtml(detail || "尚无可确认记录")}</p>
    </article>`;
    const cards = [card("行情采集", snapshot.as_of, slotsText, quoteText, "owner-tone-data", snapshot.message)];
    for (const [profile, label] of [["default", "默认档"], ["glm", "GLM 档"]]) {
        const item = observations.profiles?.[profile] || {};
        const replied = item.model_replied;
        const nonModelText = item.decision_mode === "hold_only" ? "无新增交易意图"
            : [null, undefined, "", "rule_research"].includes(item.decision_mode) ? "固定规则接管" : "回应情况未知";
        const main = replied === true ? "模型有回应" : replied === false ? nonModelText : "回应情况未知";
        const badge = replied === true ? (item.decision_accepted === false ? "判断未接收" : item.model_label || "已取得模型回答")
            : replied === false ? "本轮不是模型判断" : "不能按成功统计";
        cards.push(card(label, item.as_of, main, badge,
            replied === true && item.decision_accepted !== false ? "owner-tone-replied" : "owner-tone-notice", item.message));
    }
    host.innerHTML = `<div class="owner-runtime-grid">${cards.join("")}</div>
        <p class="section-note">${escapeHtml(observations.source_label || "已有运行记录")}；各项日期分别列出。</p>`;
}

function renderProjectStatusFailure(message, keepPrevious, readAt = "", hint = "") {
    if (!keepPrevious) renderOwnerRuntimeSummary(null);
    const banner = document.getElementById("projectStatusBanner");
    if (banner) {
        banner.hidden = false;
        const prefix = keepPrevious ? "上次读取；本次失败。" : "";
        const tail = "这是状态文件问题，不是账本清空，也不是行情全部坏掉。";
        banner.textContent = `${prefix}${message} ${hint ? `${hint} ` : ""}${tail}`;
    }
    setText("projectStatusReadTime", readAt || "本次读取失败（服务器时间未提供）");
    const health = document.getElementById("projectStatusHealth");
    if (health) health.classList.add("has-red");
    if (!keepPrevious) {
        setText("projectStatusMeta", "来源：STATUS.md · 本次不可读 · 非实时核验");
        setText("projectStatusHealth", "无法读取 STATUS.md");
        setText("projectStatusExplain", "红格解释：本次没有可用记录");
        setText("projectStatusPhase", "当前阶段：未提供");
    }
}

function renderProjectStatusPayload(data, { stale = false } = {}) {
    renderOwnerRuntimeSummary(data.runtime_observations);
    const banner = document.getElementById("projectStatusBanner");
    const health = document.getElementById("projectStatusHealth");
    const sectionsHost = document.getElementById("projectStatusSections");
    const raw = document.getElementById("projectStatusRaw");
    const kicker = document.getElementById("projectStatusKicker");
    if (kicker) {
        const scope = data.page_scope_note ? ` · ${data.page_scope_note}` : "";
        kicker.textContent = `${data.source_label || "项目记录 · 非实时核验"} · ${data.disclaimer || "纸面 · 未验证"}${scope}`;
    }
    if (banner) {
        if (stale) {
            banner.hidden = false;
        } else if (data.page_scope_note) {
            banner.hidden = false;
            banner.textContent = data.page_scope_note;
        } else if (Array.isArray(data.parse_warnings) && data.parse_warnings.length) {
            banner.hidden = false;
            banner.textContent = "项目文档缺少部分章节，任务摘要不完整；现有行情与账本仍可分别查看，来源原文保留在下方。";
            banner.title = data.parse_warnings.join("；");
        } else {
            banner.hidden = true;
            banner.textContent = "";
        }
    }
    setText("projectStatusDocTime", data.document_updated_at_display || "未提供");
    setText("projectStatusFileTime", data.file_modified_at || "未提供");
    setText("projectStatusReadTime", data.read_at || "未提供");
    setText(
        "projectStatusMeta",
        `更新者：${data.document_updater || "未提供"} · 来源：${data.source_kind || "STATUS.md"} · 可读≠系统健康`
    );
    const healthLine = data.health_line || "未提供";
    const freshness = data.health_freshness || {};
    const freshnessWarning = ["stale", "missing", "unknown"].includes(freshness.status);
    const healthDisplay = freshnessWarning
        ? `${healthLine}\n${freshness.warning || "健康行日期或更新证据不足，请核对来源。"}` : healthLine;
    setText("projectStatusHealth", healthDisplay);
    if (health) {
        health.classList.toggle("has-red", /✗|红/.test(healthLine) || freshnessWarning);
    }
    setText("projectStatusExplain", `红格解释：${data.health_explanation || "未提供"}`);
    const phase = data.phase || {};
    setText(
        "projectStatusPhase",
        `当前阶段：${phase.summary || "未提供"} · 检查点 ${phase.checkpoint || "未提供"} · 剩余交易日 ${phase.remaining_trading_days || "未提供"}`
    );
    setText("projectStatusTradeDate", `健康行对应交易日：${data.health_trade_date_label || "记录未明确标注"}`);
    if (raw) raw.textContent = data.raw_text || "";
    if (sectionsHost) {
        sectionsHost.replaceChildren();
        const progress = data.line_progress || {};
        ["B", "C", "A0"].forEach((key) => {
            const item = progress[key] || {};
            const block = document.createElement("section");
            const title = document.createElement("h3");
            title.textContent = `${key} 线`;
            const next = document.createElement("p");
            next.textContent = `下一步：${item.next || "未提供"}`;
            const stuck = document.createElement("p");
            stuck.textContent = `卡住：${item.stuck || "未提供"}`;
            const recent = document.createElement("pre");
            recent.className = "project-status-pre";
            recent.textContent = item.recent || "未提供";
            block.append(title, next, stuck, recent);
            sectionsHost.append(block);
        });
        const extra = Array.isArray(data.sections) ? data.sections : [];
        extra.forEach((section) => {
            const titleText = section.title || "未命名段落";
            if (titleText === "B 线" || titleText === "C 线" || titleText === "A0 线") return;
            if (titleText === "今日健康行" || titleText === "当前阶段") return;
            const block = document.createElement("section");
            const title = document.createElement("h3");
            title.textContent = titleText;
            const body = document.createElement("pre");
            body.className = "project-status-pre";
            body.textContent = section.body || "";
            block.append(title, body);
            sectionsHost.append(block);
        });
    }
}

async function loadProjectStatus() {
    const isCurrent = beginReadRender("project-status");
    const refresh = document.getElementById("projectStatusRefreshButton");
    if (refresh) refresh.disabled = true;
    try {
        const data = await requestJson("/api/project-status");
        if (!isCurrent()) return;
        if (!data || data.readable !== true) {
            const message = `本次读取失败：${(data && data.error) || "unknown"}`;
            if (projectStatusLastOk) {
                renderProjectStatusPayload(projectStatusLastOk, { stale: true });
                renderProjectStatusFailure(message, true, data && data.read_at);
            } else {
                renderProjectStatusFailure(message, false, data && data.read_at);
            }
            return;
        }
        projectStatusLastOk = data;
        renderProjectStatusPayload(data, { stale: false });
    } catch (error) {
        if (!isCurrent()) return;
        const message = `本次读取失败：${errorText(error)}`;
        // 连不上服务器＝门没开（服务未运行或已退出）：给一句能照做的恢复指令。
        const hint = looksLikeServerUnreachable(error) ? PROJECT_STATUS_SERVICE_DOWN_HINT : "";
        if (projectStatusLastOk) {
            renderProjectStatusPayload(projectStatusLastOk, { stale: true });
            renderProjectStatusFailure(message, true, "", hint);
        } else {
            renderProjectStatusFailure(message, false, "", hint);
        }
    } finally {
        if (isCurrent() && refresh) refresh.disabled = false;
    }
}

function errorText(error) {
    // Single source of truth for turning a thrown value into display text.
    return String((error && error.message) || error || "unknown_error");
}

function hasValue(value) {
    if (value === null || value === undefined || value === "") return false;
    if (typeof value === "number") return Number.isFinite(value);
    const text = String(value).trim();
    return text !== "" && text !== "NaN" && text !== "undefined" && text !== "null";
}

function safeText(value) {
    return hasValue(value) ? String(value) : "数据缺失";
}

function researchOnlyDisclaimer(data, fallback) {
    if (data && hasValue(data.research_only_note)) return safeText(data.research_only_note);
    if (data && hasValue(data.boundary_note)) return safeText(data.boundary_note);
    if (data && typeof data.research_only === "string" && hasValue(data.research_only)) return safeText(data.research_only);
    return safeText(fallback);
}

const STATUS_LABELS = {
    research_healthy: "研究可用",
    research_watch: "需复核",
    research_ready_with_warnings: "研究可用（有警告）",
    definition_sensitive: "定义敏感",
    moderate_high: "中高",
    high: "高",
    low: "低",
    moderate: "中",
    completed_with_warnings: "完成（有警告）",
    interrupted: "已中断",
    aborted: "已中断",
    unreadable: "记录无法读取",
    unknown: "状态待核实",
    completed: "完成",
    failed: "失败",
    timeout: "超时",
    partial: "部分完成",
    proxy: "代理口径",
    degraded: "已降级",
    blocked: "已阻断",
    running_with_warnings: "运行中（有历史告警）",
    not_run: "未运行",
    skipped: "已跳过",
    passed: "通过",
    warning: "有警告",
    missing_intraday: "缺分钟线",
    pending: "待验证",
    ok: "正常",
    healthy: "健康",
    paper_watch: "纸面观察",
    pending_discussion: "待讨论",
    robustness_watch: "稳健性待复核",
    data_risk_visible: "数据风险已标注",
    data_risk_high: "数据风险高",
    data_risk_moderate: "数据风险中",
    missing: "缺失",
    idle: "空闲",
    running: "运行中",
    active: "活跃",
    stale: "已停滞",
    unknown: "未知",
    advancing: "推进中",
    no_change: "无进展",
    counter_reset_or_regressed: "计数回退（需复核）",
    download_queue: "下载队列门禁",
    download_queue_active: "下载队列活跃",
    download_queue_stale: "下载队列停滞",
    download_in_progress: "下载进行中",
    wait_for_download_completion: "等待下载完成",
    restart_queue_after_user_approval: "等你批准后重启队列",
    continue_monitoring: "继续只读监控",
    continue_monitoring_minute_download: "继续监控分钟下载",
    wait_for_queue_then_rerun_audit: "等队列结束后重跑审计",
    wait_for_queue_then_audit_and_import: "等队列结束后先审计再导入",
    use_completed_minute_pool_for_research: "已完成分钟池可用于研究",
    error_traceback: "已停止（出错回溯）",
    waiting_for_secure_token_input: "等待安全窗口输入令牌",
    ready_for_full_apply: "可全量导入（需你确认）",
    ready_for_bounded_apply: "可受限导入（需你确认）",
    manual_review: "需人工复核",
};

function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function humanStatus(value) {
    if (!hasValue(value)) return "—";
    const key = String(value).trim().toLowerCase();
    return STATUS_LABELS[key] || String(value);
}

function humanizeStatusTerms(value) {
    let text = safeText(value);
    Object.entries(STATUS_LABELS).forEach(([key, label]) => {
        text = text.replace(new RegExp(`\\b${escapeRegExp(key)}\\b`, "gi"), label);
    });
    return text;
}

function minutePoolSourceText(value) {
    const source = safeText(value);
    if (source === "last_passed") return "来源 最近一次通过质检的分钟池快照";
    if (source === "latest") return "来源 最新分钟池质检快照";
    return source !== "数据缺失" ? `来源 ${source}` : "";
}

function sanitizeTradingTerms(text) {
    if (!hasValue(text)) return "";
    return String(text)
        .replace(/止损指令/g, "观察退出参考线")
        .replace(/止盈条件/g, "观察兑现参考")
        .replace(/止损条件/g, "观察退出参考")
        .replace(/加仓/g, "增加纸面观察")
        .replace(/减仓/g, "减少纸面观察")
        .replace(/止盈/g, "观察兑现参考")
        .replace(/止损/g, "观察退出参考");
}

function missingBadge(label = "数据缺失") {
    return `<span class="badge-degraded">${escapeHtml(label)}</span>`;
}

function humanAgeText(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) return "";
    const value = Number(seconds);
    if (value < 60) return `${Math.round(value)} 秒`;
    if (value < 3600) return `${Math.round(value / 60)} 分钟`;
    if (value < 86400) return `${(value / 3600).toFixed(1)} 小时`;
    return `${(value / 86400).toFixed(1)} 天`;
}

function signedPercentText(number, digits = 1) {
    const sign = number > 0 ? "+" : "";
    return `${sign}${number.toFixed(digits)}%`;
}

function sanePercent(value, options = {}) {
    const num = numericValue(value);
    const digits = options.digits ?? 1;
    if (num === null || !Number.isFinite(num)) {
        return { text: "数据缺失", degraded: true, warning: "本地结果未返回可检验数值" };
    }
    const percent = options.mode === "points" ? num : num * 100;
    if (Math.abs(percent) > 10000) {
        return { text: "异常样本，口径待审计", degraded: true, warning: `原始值 ${num}` };
    }
    if (percent <= -99.9) {
        return { text: signedPercentText(percent, digits), degraded: true, warning: "样本归零或数据异常，需复核口径" };
    }
    if (Math.abs(percent) > 1000) {
        return { text: signedPercentText(percent, digits), degraded: true, warning: "极端样本，需复核口径" };
    }
    return { text: signedPercentText(percent, digits), degraded: false };
}

function sanePercentHTML(value, options = {}) {
    const result = sanePercent(value, options);
    if (result.degraded) {
        const title = result.warning ? ` title="${escapeHtml(result.warning)}"` : "";
        return `<span class="badge-degraded"${title}>${escapeHtml(result.text)}</span>`;
    }
    return escapeHtml(result.text);
}

function isoDateText(value) {
    if (!hasValue(value)) return "";
    const match = String(value).trim().match(/\d{4}-\d{2}-\d{2}/);
    return match ? match[0] : "";
}

function latestIsoDate(...values) {
    const dates = values.map((value) => isoDateText(value)).filter(Boolean);
    if (!dates.length) return "";
    return dates.sort().at(-1);
}

function preferredDateText(...values) {
    const latest = latestIsoDate(...values);
    if (latest) return latest;
    const first = values.find((value) => hasValue(value));
    return safeText(first);
}

function startupTargetDateText(data = {}, summary = {}, generation = {}, update = {}) {
    const explanation = data.startup_update_explanation || {};
    const recordedTarget = latestIsoDate(data.target_date) || latestIsoDate(explanation.target_date);
    if (recordedTarget) return recordedTarget;
    const freshness = data.data_freshness_gate || data.source_status?.auto_paper_portfolio?.data_freshness_gate || {};
    const target = latestIsoDate(
        summary.target_date,
        generation.target_date,
        data.target_complete_trade_date,
        data.target_date,
        update.target_complete_trade_date,
        update.target_date,
        explanation.target_date,
        freshness.target_complete_trade_date,
        freshness.target_date
    );
    if (target) return target;
    return preferredDateText(
        generation.signal_date,
        data.current_candidate_latest_date_max,
        update.latest_local_date_after,
        data.latest_local_date_after
    );
}

function displayText(value) {
    return safeText(value)
        .replaceAll("provider行业", "行业线索")
        .replaceAll("provider 行业", "行业线索")
        .replaceAll("Provider 行业", "行业线索")
        .replaceAll("provider", "外部数据源")
        .replaceAll("Provider", "外部数据源");
}

function providerDisplayName(value) {
    const name = safeText(value);
    return {
        local: "本地文件",
        user_import: "手工导入",
        eastmoney_stock_profile: "东方财富个股资料",
        akshare_em: "东方财富行业资料",
        akshare_ths: "同花顺行业资料",
        per_symbol_info: "逐股基础信息",
        "eastmoney.spot_direct": "东方财富实时快照",
        "sina.spot_direct": "新浪实时快照",
        "akshare.full_market_wrappers": "AKShare 全市场接口",
    }[name] || displayText(name);
}

function numericValue(value) {
    if (value === null || value === undefined || typeof value === "boolean") return null;
    if (typeof value !== "number" && typeof value !== "string") return null;
    if (typeof value === "string" && !value.trim()) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function formatNumber(value, digits = 2) {
    const number = numericValue(value);
    return number === null ? safeText(value) : number.toFixed(digits);
}

function formatChange(value) {
    const number = numericValue(value);
    if (number === null) return safeText(value);
    const sign = number > 0 ? "+" : "";
    return `${sign}${number.toFixed(2)}%`;
}

function changeClass(value) {
    const number = numericValue(value);
    if (number === null || number === 0) return "neutral";
    return number > 0 ? "positive" : "negative";
}

function riskClass(level) {
    if (level === "低") return "risk-low";
    if (level === "高") return "risk-high";
    return "risk-mid";
}

function riskTone(level) {
    if (level === "低") return "ok";
    if (level === "高") return "warn";
    return "watch";
}

function compactSentence(value, maxLength = 76) {
    const text = safeText(value).replace(/\s+/g, " ").trim();
    if (text.length <= maxLength) return text;
    return `${text.slice(0, maxLength)}…`;
}

function firstSentence(value, fallback = "数据缺失") {
    const text = safeText(value);
    const parts = text.split(/[。；;！!？?]/).map((item) => item.trim()).filter(Boolean);
    return parts[0] || fallback;
}

function highlightText(value) {
    return applyHighlightsToEscapedHtml(escapeHtml(safeText(value)));
}

function highlightHtmlText(html) {
    return applyHighlightsToEscapedHtml(safeHtml(html));
}

function applyHighlightsToEscapedHtml(escaped) {
    return escaped
        .replace(/(偏离观察假设|偏离入选假设|不一致|需要降级复盘|最大回撤\s*[-+]?[\d.]+%|数据缺失|关键缺口|低于20属于超卖区|高于80属于超买区|价格低于20日均线|短线动能偏弱|字段缺失|日线滞后|失效情景|量能字段降级)/g, '<span class="reason-mark reason-warning">$1</span>')
        .replace(/(符合观察假设|符合入选假设|阶段符合观察假设|数据完整|未发现关键缺口|价格高于20日均线|短线动能偏强|成交明显放大|优先关注|进攻情景|主线情景|价格\/K线已更新|偏强进攻观察|震荡突破观察)/g, '<span class="reason-mark reason-positive">$1</span>')
        .replace(/(量价突破观察|趋势修复观察|打板观察|长期观察|长期只观察|每日重点复核|趋势背景还需要修复|尚未确认|观察中|最近\d+天(?:上涨|下跌)[-+]?[\d.]+%|最近\d+日(?:上涨|下跌)[-+]?[\d.]+%|当前收盘价为[-+]?[\d.]+元|20日均线为[-+]?[\d.]+元|20日波动率为[-+]?[\d.]+%|DIF(?:\s|&nbsp;)*(?:低于|高于)(?:\s|&nbsp;)*DEA(?:\s|&nbsp;)*[-+]?[\d.]+|柱体为[-+]?[\d.]+|K(?:\s|&nbsp;)*值为[-+]?[\d.]+|RSI6?(?:\s|&nbsp;)*为[-+]?[\d.]+|5日均量是20日均量的[-+]?[\d.]+倍|当前判断|数值|含义|MACD|KDJ|RSI|成交量)/g, '<span class="reason-mark">$1</span>');
}

function missingBlock(message) {
    return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function formatLLMAnalysis(text) {
    if (!hasValue(text)) return "<p>分析结果为空</p>";
    const paragraphs = sanitizeTradingTerms(String(text))
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
            const tone = /风险|缺失|异常|警告|不可用|待验证|数据不足/.test(line)
                ? "llm-warning"
                : /可用|正常|完成|通过|符合/.test(line)
                    ? "llm-ok"
                    : "";
            return `<p${tone ? ` class="${tone}"` : ""}>${escapeHtml(line)}</p>`;
        });
    return paragraphs.join("") || "<p>分析结果为空</p>";
}

function setupSidebarCollapse() {
    const button = document.getElementById("sideCollapseButton");
    if (!button) return;
    const label = button.querySelector(".side-collapse-label");

    const applyCollapsed = (collapsed) => {
        document.body.classList.toggle("nav-collapsed", collapsed);
        button.setAttribute("aria-pressed", collapsed ? "true" : "false");
        button.setAttribute("aria-label", collapsed ? "展开导航" : "收起导航");
        button.title = collapsed ? "展开导航" : "收起导航";
        if (label) label.textContent = collapsed ? "展开导航" : "收起导航";
        resizeActiveCharts();
    };

    let savedCollapsed = false;
    try {
        savedCollapsed = localStorage.getItem(SIDEBAR_COLLAPSE_KEY) === "1";
    } catch (error) {
        savedCollapsed = false;
    }
    applyCollapsed(savedCollapsed);

    button.addEventListener("click", () => {
        const collapsed = !document.body.classList.contains("nav-collapsed");
        applyCollapsed(collapsed);
        try {
            localStorage.setItem(SIDEBAR_COLLAPSE_KEY, collapsed ? "1" : "0");
        } catch (error) {
            // localStorage can be disabled; the visible state still changes for this session.
        }
        window.dispatchEvent(new Event("resize"));
    });
}

function setupModuleNavigation() {
    const homeSections = Array.from(document.querySelectorAll("[data-home-section]"));
    const extraHomeBlocks = Array.from(document.querySelectorAll("[data-home-page]")).filter((node) => !node.matches("[data-home-section], [data-module-view]"));
    const moduleSections = Array.from(document.querySelectorAll("[data-module-view]"));
    const moduleButtons = Array.from(document.querySelectorAll("[data-module-target], [data-side-module-target]"));
    const pageButtons = Array.from(document.querySelectorAll("[data-page]"));
    const homeButtons = Array.from(document.querySelectorAll("[data-module-home]"));
    const backButtons = Array.from(document.querySelectorAll("[data-module-back]"));
    if (!moduleSections.length) { void loadVisiblePageData(""); return; }

    const moduleIds = new Set(moduleSections.map((section) => section.id));
    const HASH_ALIASES = {
        today: "",
        watch: "__watch__",
        ledgers: "paper-watchlist-section",
        "ledgers/strategy": "paper-watchlist-section",
        "ledgers/ai": "ai-trader-section",
        review: "decision-history-section",
        tools: "performance-section",
        system: "__system__",
        dashboard: "",
        "watch-pool": "paper-watchlist-section",
        "decision-history": "decision-history-section",
        "ai-log": "ai-log-section",
        "ai-trader": "ai-trader-section",
        "daily-focus": "daily-focus-section",
        "candidate-pool": "candidate-pool-section",
        "agent": "agent-section",
        "backtest": "backtest-section",
        "strategy-lab": "strategy-lab-section",
        "data-ops": "data-ops-section",
        "performance": "performance-section",
    };
    const titleMap = {
        "ai-log-section": "AI 日志",
        "decision-history-section": "复盘学习",
        "daily-focus-section": "每日研究候选",
        "candidate-pool-section": "观察列表",
        "quant-summary-section": "Quant 日总结",
        "limit-review-section": "涨停复盘",
        "theme-map-section": "板块/题材映射",
        "agent-section": "LLM 研究室",
        "ai-trader-section": "AI账",
        "data-ops-section": "设置",
        "paper-watchlist-section": "策略账",
        "strategy-lab-section": "因子分析",
        "performance-section": "策略成绩",
        "risk-notifications-section": "风险/通知看板",
        "smart-stop-section": "止损参考",
        "notification-center-section": "通知中心",
        "backtest-section": "回测",
        __watch__: "看盘",
        __system__: "系统状态",
    };
    const setTopbarTitle = (targetId) => {
        const title = document.getElementById("topbarTitle");
        if (!title) return;
        if (targetId && titleMap[targetId]) {
            title.textContent = titleMap[targetId];
            return;
        }
        title.textContent = "今天";
    };

    // Opening a module pulls that module's data fresh, so the view always
    // reflects the latest local files instead of whatever happened to load at
    // page open. loadCandidates() renders the candidate pool, daily-focus and
    // quant-summary panels together.
    const refreshModuleData = (targetId) => {
        return loadVisiblePageData(targetId);
    };

    const pageOf = (node) => String(node?.dataset?.homePage || "today").split(/\s+/).filter(Boolean);

    const applyHomePage = (pageName) => {
        homeSections.forEach((section) => {
            section.hidden = !pageOf(section).includes(pageName);
        });
        extraHomeBlocks.forEach((node) => {
            if (node.id === "newsStatusBanner") {
                if (pageName !== "today") node.hidden = true;
                return;
            }
            node.hidden = !pageOf(node).includes(pageName);
        });
    };

    const setActiveButton = (targetId, pageName = "") => {
        const isHome = !targetId && pageName !== "system" && pageName !== "watch";
        homeButtons.forEach((button) => {
            const active = isHome;
            button.setAttribute("aria-pressed", active ? "true" : "false");
            if (active) button.setAttribute("aria-current", "page");
            else button.removeAttribute("aria-current");
        });
        pageButtons.forEach((button) => {
            const page = button.dataset.page || "";
            const active = (pageName && page === pageName) || (targetId && (button.dataset.moduleTarget || button.dataset.sideModuleTarget) === targetId && page === pageName);
            if (!pageName && targetId) return;
            button.setAttribute("aria-pressed", active ? "true" : "false");
            if (active) button.setAttribute("aria-current", "page");
            else if (page && page !== "today") button.removeAttribute("aria-current");
        });
        moduleButtons.forEach((button) => {
            const active = (button.dataset.moduleTarget || button.dataset.sideModuleTarget) === targetId;
            button.setAttribute("aria-pressed", active ? "true" : "false");
            if (active) button.setAttribute("aria-current", "page");
            else button.removeAttribute("aria-current");
        });
    };

    const isDetailModalOpen = () => document.getElementById("detailModal")?.classList.contains("open") === true;

    const scrollToTop = () => {
        window.scrollTo({ top: 0, behavior: "auto" });
    };

    const setHash = (hash, updateUrl) => {
        if (!updateUrl) return;
        const next = hash ? `#${hash}` : `${window.location.pathname}${window.location.search}`;
        if (hash) history.pushState(null, "", next);
        else if (window.location.hash) history.replaceState(null, "", next);
    };

    const showHomeView = (updateUrl = true, scroll = true, refresh = true) => {
        document.body.dataset.view = "home";
        document.body.dataset.page = "today";
        applyHomePage("today");
        moduleSections.forEach((section) => {
            section.hidden = true;
        });
        setActiveButton("", "today");
        setTopbarTitle("");
        window.requestAnimationFrame(resizeHomeCharts);
        setHash("", updateUrl);
        if (scroll) scrollToTop();
        if (refresh) refreshModuleData("");
    };

    const showSystemView = (updateUrl = true, scroll = true, refresh = true) => {
        document.body.dataset.view = "system";
        document.body.dataset.page = "system";
        applyHomePage("system");
        moduleSections.forEach((section) => {
            section.hidden = section.id !== "data-ops-section";
        });
        setActiveButton("data-ops-section", "system");
        setTopbarTitle("__system__");
        setHash("system", updateUrl);
        if (scroll) scrollToTop();
        if (refresh) refreshModuleData("__system__");
        resizeActiveCharts();
    };

    const showWatchView = (updateUrl = true, scroll = true, refresh = true) => {
        document.body.dataset.view = "watch";
        document.body.dataset.page = "watch";
        applyHomePage("watch");
        moduleSections.forEach((section) => {
            section.hidden = section.id !== "daily-focus-section";
        });
        setActiveButton("daily-focus-section", "watch");
        setTopbarTitle("__watch__");
        setHash("watch", updateUrl);
        if (scroll) scrollToTop();
        if (refresh) refreshModuleData("__watch__");
        resizeActiveCharts();
    };

    const showModuleView = (targetId, updateUrl = true, scroll = true, refresh = true) => {
        if (targetId && typeof targetId === "object" && targetId.page === "__watch__") {
            if (isDetailModalOpen() && currentWorkbenchContext?.symbol === targetId.symbol
                && currentWorkbenchContext?.source === targetId.source
                && currentWorkbenchContext?.profile === targetId.profile
                && Date.now() - currentDetailFetchedAt < DETAIL_FRESH_MS) return;
            setAiTraderProfile(targetId.profile);
            const origin = targetFromHash(targetId.returnHash || "#watch");
            if (!origin || typeof origin === "string") showModuleView(origin, false, scroll, refresh);
            else showWatchView(false, scroll, refresh);
            showDetail({ symbol: targetId.symbol, name: targetId.symbol, detail_lane: targetId.lane, select_date: targetId.entryDate },
                { source: targetId.source, profile: targetId.profile, returnHash: targetId.returnHash, updateUrl: false });
            return;
        }
        if (targetId === "__system__") {
            showSystemView(updateUrl, scroll, refresh);
            return;
        }
        if (targetId === "__watch__") {
            showWatchView(updateUrl, scroll, refresh);
            return;
        }
        if (!moduleIds.has(targetId)) {
            showHomeView(updateUrl, scroll, refresh);
            return;
        }
        document.body.dataset.view = "module";
        const pageName = targetId === "paper-watchlist-section" || targetId === "ai-trader-section"
            ? "ledgers"
            : targetId === "decision-history-section" || targetId === "ai-log-section"
                ? "review"
                : targetId === "daily-focus-section"
                    ? "watch"
                    : targetId === "data-ops-section"
                        ? "system"
                        : "tools";
        document.body.dataset.page = pageName;
        applyHomePage(pageName === "watch" ? "watch" : "__none__");
        homeSections.forEach((section) => {
            if (pageName !== "watch") section.hidden = true;
        });
        extraHomeBlocks.forEach((node) => {
            if (pageName !== "watch") node.hidden = true;
        });
        moduleSections.forEach((section) => {
            section.hidden = section.id !== targetId;
        });
        setActiveButton(targetId, pageName);
        setTopbarTitle(targetId);
        setHash(targetId, updateUrl);
        if (scroll) scrollToTop();
        if (refresh) refreshModuleData(targetId);
        resizeActiveCharts();
    };
    window.quantdeskShowModuleView = showModuleView;
    window.quantdeskShowPage = (page) => {
        if (page === "system") showSystemView();
        else if (page === "watch") showWatchView();
        else if (page === "ledgers") showModuleView("paper-watchlist-section");
        else if (page === "review") showModuleView("decision-history-section");
        else if (page === "tools") showModuleView("performance-section");
        else showHomeView();
    };

    const targetFromHash = (value = window.location.hash) => {
        const raw = String(value || "").replace(/^#/, "");
        const question = raw.indexOf("?");
        const path = question < 0 ? raw : raw.slice(0, question);
        const params = new URLSearchParams(question < 0 ? "" : raw.slice(question + 1));
        let hash;
        try { hash = decodeURIComponent(path); } catch (_) { return ""; }
        if (!hash) return "";
        const watchStock = hash.match(/^watch\/([^/]+)$/);
        if (watchStock) {
            const from = params.get("from") || "#watch";
            return {
                page: "__watch__", symbol: watchStock[1], source: params.get("source") || "stock",
                profile: params.get("profile") === "glm" ? "glm" : "default",
                lane: params.get("lane") || "", entryDate: params.get("entry_date") || "",
                returnHash: from.startsWith("#") && !from.startsWith("#watch/") ? from : "#watch",
            };
        }
        if (Object.prototype.hasOwnProperty.call(HASH_ALIASES, hash)) return HASH_ALIASES[hash];
        if (moduleIds.has(hash)) return hash;
        const sectionId = `${hash}-section`;
        return moduleIds.has(sectionId) ? sectionId : "";
    };

    const openFromControl = (button) => {
        const page = button.dataset.page || "";
        if (page === "system") {
            showSystemView();
            return;
        }
        if (page === "watch") {
            showWatchView();
            return;
        }
        if (page === "today") {
            showHomeView();
            return;
        }
        if (page === "ledgers") {
            showModuleView("paper-watchlist-section");
            return;
        }
        if (page === "review") {
            showModuleView("decision-history-section");
            return;
        }
        if (page === "tools") {
            showModuleView("performance-section");
            return;
        }
        showModuleView(button.dataset.moduleTarget || button.dataset.sideModuleTarget || "");
    };

    moduleButtons.forEach((button) => {
        button.addEventListener("click", () => openFromControl(button));
    });
    pageButtons.forEach((button) => {
        if (button.matches("[data-module-target], [data-side-module-target], [data-module-home]")) return;
        button.addEventListener("click", () => openFromControl(button));
    });
    homeButtons.forEach((button) => {
        button.addEventListener("click", () => showHomeView());
    });
    backButtons.forEach((button) => {
        button.addEventListener("click", () => showHomeView());
    });
    document.addEventListener("keydown", (event) => {
        if (document.body.dataset.view === "home" || isDetailModalOpen()) return;
        if (event.key === "Escape" || (event.altKey && event.key === "ArrowLeft")) {
            event.preventDefault();
            showHomeView();
        }
    });
    const applyHash = (updateUrl, scroll, refresh) => {
        const targetId = targetFromHash();
        if (typeof targetId !== "object" && isDetailModalOpen()) closeModal({ restore: false });
        if (targetId) showModuleView(targetId, updateUrl, scroll, refresh);
        else showHomeView(updateUrl, scroll, refresh);
    };
    window.quantdeskRestoreView = () => applyHash(false, false, false);
    window.addEventListener("popstate", () => applyHash(false, false, true));
    window.addEventListener("hashchange", () => applyHash(false, false, true));
    const initialTargetId = targetFromHash();
    if (initialTargetId) {
        showModuleView(initialTargetId, false, false, true);
    } else {
        showHomeView(false, false);
    }
}

function setupAiFundManagerLifecycle() {
    const aiLogButton = document.getElementById("refreshAiFundDailyLogButton");
    const historyButton = document.getElementById("refreshDecisionHistoryButton");
    if (aiLogButton) aiLogButton.addEventListener("click", () => loadAiFundDailyLog());
    if (historyButton) historyButton.addEventListener("click", () => loadReviewLearning());
}

async function loadAiFundDailyLog() {
    const panel = document.getElementById("aiFundDailyLogPanel");
    const note = document.getElementById("aiFundDailyLogNote");
    if (!panel) return;
    panel.innerHTML = '<div class="empty-state">正在汇总市场、策略与纸面决策证据…（策略成绩单接口较慢，完整渲染约需 15-20 秒）</div>';
    if (note) note.textContent = "正在读取当天研究证据…";
    const settled = await Promise.allSettled([
        requestJson("/api/daily/overview"),
        requestJson("/api/decision-board"),
        requestJson("/api/strategy-scorecard"),
    ]);
    const market = settled[0].status === "fulfilled" ? settled[0].value : null;
    const board = settled[1].status === "fulfilled" ? settled[1].value : null;
    const scorecard = settled[2].status === "fulfilled" ? settled[2].value : null;
    renderAiFundDailyLog({ market, board, scorecard });
    const availableCount = [market, board, scorecard].filter(Boolean).length;
    if (note) note.textContent = `已读取 ${availableCount}/3 组后端证据；缺失项保持 missing/degraded。`;
}

function renderAiFundDailyLog(payload) {
    const panel = document.getElementById("aiFundDailyLogPanel");
    if (!panel) return;
    const market = payload?.market || {};
    const board = payload?.board || {};
    const scorecard = payload?.scorecard || {};
    const envelope = board.envelope || {};
    const strategyRows = Array.isArray(scorecard.by_strategy) ? scorecard.by_strategy : [];
    const primaryStrategy = strategyRows.length ? strategyRows[0] : null;
    const eventCount = numericValue(envelope.event?.count) ?? (Array.isArray(board.events) ? board.events.length : null);
    const watchCount = numericValue(envelope.coverage?.watchlist_count);
    const positionCount = numericValue(envelope.coverage?.position_count);
    const hs300IndexAvailable = hasValue(market.hs300?.value)
        && numericValue(market.hs300?.value) !== null
        && hasValue(market.hs300?.change)
        && numericValue(market.hs300?.change) !== null;
    const shIndexAvailable = hasValue(market.sh_index?.value)
        && numericValue(market.sh_index?.value) !== null
        && hasValue(market.sh_index?.change)
        && numericValue(market.sh_index?.change) !== null;
    const marketSentimentAvailable = hasValue(market.market_sentiment)
        && !String(market.market_sentiment).includes("数据缺失")
        && String(market.market_sentiment).trim().toLowerCase() !== "missing";
    const marketStatusPassed = marketSentimentAvailable
        && (hs300IndexAvailable || shIndexAvailable);
    const boardStatusPassed = board.status === "passed";
    const scorecardStatusPassed = scorecard.status === "passed" && strategyRows.length > 0;
    const evidenceStatus = marketStatusPassed && boardStatusPassed && scorecardStatusPassed
        ? "available"
        : "degraded";
    const marketEvidenceAt = safeText(market.hs300?.date || market.sh_index?.date || "missing");
    const scorecardEvidenceAt = safeText(scorecard.generated_at || "missing");
    const boardEvidenceAt = safeText(board.as_of || "missing");
    const stages = [
        {
            evidenceAt: marketEvidenceAt,
            title: "市场观察",
            status: safeText(market.market_sentiment || "missing"),
            detail: safeText(market.update_hint || "市场概览证据缺失，未生成替代结论。"),
        },
        {
            evidenceAt: scorecardEvidenceAt,
            title: "策略证据",
            status: primaryStrategy ? safeText(primaryStrategy.strategy || primaryStrategy.strategy_id) : "missing",
            detail: primaryStrategy
                ? `样本 ${safeText(primaryStrategy.sample_count)} · ${safeText(primaryStrategy.scorecard_label || primaryStrategy.status)}`
                : "策略成绩单没有返回可展示的首要策略。",
        },
        {
            evidenceAt: boardEvidenceAt,
            title: "Watch Pool",
            status: watchCount === null ? "missing" : `${watchCount} 个观察样本`,
            detail: positionCount === null ? "后端未返回 funded position 数量。" : `${positionCount} 个 funded paper position。`,
        },
        {
            evidenceAt: boardEvidenceAt,
            title: "风险检查",
            status: safeText(envelope.risk?.status || "missing"),
            detail: `后端复核项 ${safeText(envelope.risk?.review_count)}；LLM 无权覆盖风险否决。`,
        },
        {
            evidenceAt: boardEvidenceAt,
            title: "决策复盘",
            status: eventCount === null ? "missing" : `${eventCount} 条事件`,
            detail: `证据清单 ${safeText(envelope.manifest?.manifest_id)}；只读、可追溯。`,
        },
    ];
    panel.innerHTML = `
        <div class="metric-row">
            <article class="metric-box"><span>日志日期</span><strong>${escapeHtml(safeText(board.as_of || "missing"))}</strong></article>
            <article class="metric-box"><span>证据状态</span><strong>${escapeHtml(humanStatus(evidenceStatus))}</strong></article>
            <article class="metric-box"><span>策略版本</span><strong>${escapeHtml(safeText(envelope.strategy_version?.value || "missing"))}</strong></article>
            <article class="metric-box"><span>执行边界</span><strong>No Broker Route</strong></article>
        </div>
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>每日研究证据摘要（后端可审计快照，不是时间线或隐藏推理）</strong>
            <div class="section-note">证据时点只取后端返回日期；证据不足时显示 missing/degraded。</div>
        </div>
        <div class="paper-daily-report">
            ${stages.map((stage) => `
                <div class="report-row">
                    <span>${escapeHtml(stage.evidenceAt)}</span>
                    <strong>${escapeHtml(stage.title)}</strong>
                    <span>${escapeHtml(stage.status)}</span>
                    <span>${escapeHtml(stage.detail)}</span>
                </div>
            `).join("")}
        </div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route / Live Execution Locked / QMT Order Route Locked / Not Investment Advice / Not Position Advice.</p>
    `;
}

async function loadDecisionHistory() {
    const panel = document.getElementById("decisionHistoryPanel");
    const note = document.getElementById("decisionHistoryNote");
    if (!panel) return;
    const requestId = ++decisionHistoryRequestSequence;
    const profile = aiTraderProfile;
    const profileRevision = aiTraderProfileRevision;
    const profileLabel = aiTraderProfileLabel(profile);
    const profileQuery = aiTraderProfileQuery(profile);
    rememberWorkbenchSource("review", []);
    panel.onclick = null;
    panel.innerHTML = `<div class="empty-state">正在读取 ${escapeHtml(profileLabel)} 的复盘记录…</div>`;
    if (note) note.textContent = `${profileLabel} · 正在读取各来源；尚未确认记录数量。`;
    try {
        const settled = await Promise.allSettled([
            requestJson("/api/decision-board"),
            requestJson("/api/ai-trader/dashboard" + profileQuery),
            requestJson("/api/ai-trader/decisions" + profileQuery),
        ]);
        if (requestId !== decisionHistoryRequestSequence || profileRevision !== aiTraderProfileRevision) return;
        const sources = settled.map((result, index) => {
            const value = result.status === "fulfilled" ? result.value : null;
            const available = Boolean(value && typeof value === "object" && value.ok !== false
                && (index === 0 ? Array.isArray(value.watchlist) || Array.isArray(value.events)
                    : index === 1 ? Array.isArray(value.filled_trades) : value.available === true && Array.isArray(value.records)));
            return {
                label: ["策略观察与策略账事件", `${profileLabel} · AI 账近期成交`, `${profileLabel} · AI 判断核对`][index],
                path: ["/api/decision-board", "/api/ai-trader/dashboard", "/api/ai-trader/decisions"][index],
                warnings: Array.isArray(value?.warnings) ? value.warnings : [],
                available,
                degraded: available && (value.degraded === true || value.status === "degraded"),
                asOf: available ? value.as_of || value.account?.as_of || value.updated_at : "",
                value: available ? value : null,
            };
        });
        const board = sources[0].value;
        const ai = sources[1].value;
        const items = buildReviewItems(board, ai, sources[2].value, profile);
        const unavailable = sources.filter((source) => !source.available);
        const failure = unavailable.length === sources.length ? new Error("所有复盘来源读取失败，记录数量未知。") : null;
        renderDecisionHistory(board, failure, items, sources);
        if (note) note.textContent = [
            `${profileLabel} ·`,
            failure ? "复盘记录读取失败；数量未知。" : `当前已读 ${items.length} 条；仅统计本次可读取范围。`,
            ...sources.filter((source) => !source.available || source.degraded).map((source) => `${source.label}${source.available ? "降级" : "读取失败"}`),
            sources[2].available ? `AI 判断 ${sources[2].value.records.length} 条；核对截至 ${safeText(sources[2].asOf)}。` : "AI 判断数量未知。",
        ].join(" ");
    } catch (error) {
        if (requestId !== decisionHistoryRequestSequence || profileRevision !== aiTraderProfileRevision) return;
        renderDecisionHistory(null, error, []);
        if (note) note.textContent = `${profileLabel} · 复盘记录读取失败；未生成浏览器侧替代记录。`;
    }
}

function reviewBucketOf(item) {
    if (item.kind === "paper_fill") return "fill";
    if (item.kind === "ai_decision") {
        if (item.backfill === true || item.provenance === "historical_review") return "historical";
        if (item.review_context?.observation_status === "early_hit") return "early_hit";
        return ({ "错": "mismatch", "陈述与数据不符": "mismatch", "已到期 · 证据不足": "evidence_gap",
            "未到期": "pending", "对": "match", "不适用": "other" })[item.result] || "other";
    }
    // Only producer-owned status fields classify records. A reason mentioning
    // "未到期" or "兑现" is prose, not a verified outcome or maturity clock.
    const statement = String(item.statement_verification?.status || item.statement_status || "").toLowerCase();
    if (["mismatch", "statement_mismatch", "陈述与数据不符"].includes(statement)) return "mismatch";
    const status = String(item.outcome_status || item.result_status || item.review_status || "").toLowerCase();
    if (["statement_mismatch", "verified_incorrect", "condition_failed", "review_required", "hold_review", "needs_review", "陈述与数据不符", "错"].includes(status)) return "mismatch";
    if (["due_missing_evidence", "due_unverifiable", "到期缺证据"].includes(status)) return "evidence_gap";
    if (["missing_evidence", "unverifiable", "missing_intraday", "no_fill"].includes(status)) {
        return item.maturity_status === "due" ? "evidence_gap" : "data_gap";
    }
    if (["not_due", "pending_maturity", "未到期"].includes(status)) return "pending";
    if (["verified_correct", "condition_met", "对"].includes(status)) return "match";
    return "other";
}

function buildReviewItems(board, ai, decisions = null, profile = "default") {
    const items = [];
    const watchlist = Array.isArray(board && board.watchlist) ? board.watchlist : [];
    for (const row of watchlist) {
        const item = {
            ...row,
            symbol: row.symbol,
            name: row.name,
            date: row.latest_date || row.entry_date,
            ledger: "策略观察",
            action: row.review_label || row.review_status || "观察中",
            reason: row.review_reason || "missing/degraded",
            review_status: row.review_status,
            review_label: row.review_label,
            kind: "rule_signal",
            source_scope: "/api/decision-board · watchlist（策略观察，不是 C 线核验）",
        };
        item.bucket = reviewBucketOf(item);
        items.push(item);
    }
    const events = Array.isArray(board && board.events) ? board.events : [];
    for (const event of events) {
        const item = {
            ...event,
            symbol: event.symbol,
            name: event.name,
            date: event.date || event.trade_date || event.decision_date,
            ledger: "策略账",
            action: event.action || event.event_type || "missing",
            reason: event.reason || event.note || "missing/degraded",
            kind: event.action === "buy" || event.action === "sell" ? "paper_fill" : "other",
            source_scope: "/api/decision-board · events",
        };
        item.bucket = reviewBucketOf(item);
        items.push(item);
    }
    const fills = Array.isArray(ai && ai.filled_trades) ? ai.filled_trades : [];
    for (const fill of fills) {
        const item = {
            ...fill,
            symbol: fill.symbol,
            name: fill.symbol,
            date: fill.date || fill.settled_on,
            ledger: "AI账",
            profile,
            profile_label: aiTraderProfileLabel(profile),
            action: fill.side === "sell" ? "纸面卖出" : "纸面买入",
            reason: `成交价 ${safeText(fill.price)} · ${safeText(fill.status || "filled")}`,
            kind: "paper_fill",
            source_scope: `${aiTraderProfileLabel(profile)} · /api/ai-trader/dashboard · filled_trades（近期片段）`,
        };
        item.bucket = reviewBucketOf(item);
        items.push(item);
    }
    for (const record of Array.isArray(decisions?.records) ? decisions.records : []) {
        const item = { ...record, kind: "ai_decision", ledger: "AI 判断", date: record.decision_date,
            review_context: decisions?.review_summary?.record_context?.[record.decision_id],
            profile, profile_label: aiTraderProfileLabel(profile),
            action: record.result || "未分类", reason: record.judgment, source_scope: "AI 判断核对记录" };
        item.bucket = reviewBucketOf(item);
        items.push(item);
    }
    const rank = (item) => item.kind === "ai_decision" && item.bucket !== "historical"
        ? ({ "陈述与数据不符": 0, "错": 1, "已到期 · 证据不足": 2, "未到期": 3, "对": 4 })[item.result] ?? 5 : 6;
    return items.sort((left, right) => rank(left) - rank(right) || safeText(right.date).localeCompare(safeText(left.date)));
}

function decisionOriginLabel(row) {
    if (row.decision_origin === "rule" || row.llm_used === false) return "固定规则";
    if (row.llm_used !== true) return "来源未知";
    const model = String(row.llm_model || "").toLowerCase();
    const name = model.includes("kimi") || model.includes("moonshot") ? "Kimi"
        : model.includes("glm") ? "GLM" : model.includes("deepseek") ? "DeepSeek" : String(row.llm_model || "名称未知");
    return row.decision_origin === "model" ? `${name} 模型` : `${name} 调用 · 意图来源未知`;
}

function renderDecisionOutcomeSummary(source) {
    const summary = source?.value?.review_summary;
    if (!source?.available || !summary || typeof summary.groups !== "object") {
        return '<p class="section-note">观察期统计尚未提供；下面保留原记录，不把未知当作零成绩。</p>';
    }
    const numberText = (value) => Number.isInteger(value) && value >= 0 ? String(value) : "未知";
    const modelNames = [...new Set((source.value.records || []).filter((row) => row.llm_used === true).map(decisionOriginLabel))];
    const originNote = modelNames.length
        ? `<p class="section-note">本页模型记录：${escapeHtml(modelNames.join("、"))}。按当时实际模型标注，账本档位名称不代表每条记录的模型。</p>` : "";
    const rows = Object.values(summary.groups).map((group) => {
        const mature = numberText(group.mature_evaluable);
        const result = group.mature_evaluable > 0
            ? `其中对 ${numberText(group.mature_correct)} 条、错 ${numberText(group.mature_wrong)} 条`
            : "暂不计算正确率";
        const unknown = group.maturity_unknown > 0 ? `；另有 ${numberText(group.maturity_unknown)} 条观察日期不明` : "";
        return `<article class="owner-review-card"><strong>${escapeHtml(group.label || "来源未知")}</strong>
            <div class="owner-review-number">${mature}<small>条</small></div>
            <p>计入可信准确率分母的到期记录</p><p class="section-note">${result}${unknown}</p>
            <dl><dt>提前命中，还在观察</dt><dd>${numberText(group.early_hits)} 条</dd>
                <dt>证据条件不足，未计入</dt><dd>${numberText(group.excluded)} 条</dd>
                <dt>原判定到期记录（仅展示）</dt><dd>${numberText(group.diagnostic_mature)} 条</dd>
                <dt>尚在等待结果</dt><dd>${numberText(group.pending)} 条</dd>
                <dt>到期缺证据</dt><dd>${numberText(group.due_evidence_gap)} 条</dd>
                <dt>未接收 / 陈述不符</dt><dd>${numberText(group.unaccepted)} / ${numberText(group.statement_mismatch)} 条</dd>
                <dt>补证后重评（另计）</dt><dd>${numberText(group.rechecked)} 条</dd>
                <dt>重评对 / 错 / 仍缺证</dt><dd>${numberText(group.rechecked_correct)} / ${numberText(group.rechecked_wrong)} / ${numberText(group.recheck_gap)} 条</dd></dl>
                <p class="section-note">补证评价不改原判断，不混入原观察期正确率；仍缺证不算对或错。</p></article>`;
    });
    return `<div class="owner-review-summary">
        <p class="section-note">核对截至 ${escapeHtml(summary.as_of || "日期未知")}。
        提前满足条件只记观察证据，未到期不判“对”；历史原判定保留展示。模型和固定规则分开看。</p>
        <p class="section-note">${escapeHtml(summary.denominator_note || "可信分母需要模型来源、事前声明、实际纸面成交和完整到期证据；缺证记录不计成绩。")}</p>
        ${originNote}
        <div class="owner-review-grid">${rows.join("")}</div>
    </div>`;
}

function renderAiDecisionRecord(row, index) {
    const text = (value) => escapeHtml(safeText(value));
    const seen = row.seen && typeof row.seen === "object" ? row.seen : {};
    const verification = row.verification && typeof row.verification === "object" ? row.verification : {};
    const historical = row.backfill === true || row.provenance === "historical_review";
    const originLabel = decisionOriginLabel(row);
    const fields = [
        ["AI 档位", row.profile_label],
        ["判断来源", originLabel],
        ["观察进度", row.review_context?.label || "尚无观察期说明"],
        ["实际纸面成交", row.review_context?.fill_status === "confirmed" ? "已按计划编号核实" : "未确认"],
        ["计入可信分母", row.review_context?.in_accuracy_denominator === true ? "是" : "否"],
        ["未计入原因", (row.review_context?.exclusion_labels || []).join("；") || row.review_context?.label],
        ["当时判断", row.judgment], ["风格", row.style], ["计划持有交易日", row.planned_hold_days],
        ["验证条件", verification.text], ["验证价位", verification.level], ["到期日", verification.due_date],
        ["条件来源", verification.source === "derived_from_declared_horizon" ? "程序推导条件，非 AI 事前声明" : verification.source],
        ["当时数据日期", seen.as_of], ["当时收盘价", seen.close], ["当时涨跌幅（%）", seen.pct_chg],
        ["程序核对结果", row.result], ["结果截至", row.result_as_of],
        ["记录类型", historical ? "事后复盘" : "事前记录"], ["落盘时间", row.recorded_at],
        ["整理时间", row.structured_at], ["决策已接收", row.committed === true ? "是" : row.committed === false ? "否" : null],
        ["提示词版本", row.prompt_version], ["数据菜单版本", row.menu_version],
        ["提示词内容哈希", row.prompt_sha256], ["菜单内容哈希", row.menu_sha256],
        ["实际输入哈希", row.effective_prompt_sha256], ["记录编号", row.decision_id],
    ];
    return `<details class="review-decision">
        <summary class="report-row review-row">
            <span>${text(row.date)}</span><strong>${text(row.symbol || "市场判断")}</strong>
            <span>${text(row.review_context?.observation_status === "early_hit" ? "提前命中" : row.result)}<small>${text(originLabel)} · ${historical ? "事后复盘" : "事前记录"} · 纸面 · 未验证</small></span>
            <span class="review-judgment-preview">${text(row.judgment)}</span>
        </summary>
        <div class="review-decision-body">
            <dl>${fields.map(([label, value]) => `<dt>${text(label)}</dt><dd>${text(value)}</dd>`).join("")}</dl>
            <details><summary>核对证据</summary><pre>${text(JSON.stringify(row.result_evidence ?? [], null, 2))}</pre></details>
            ${Array.isArray(row.statement_unverified) && row.statement_unverified.length
                ? `<details><summary>未核验陈述（不算确定对错）</summary><pre>${text(JSON.stringify(row.statement_unverified, null, 2))}</pre></details>` : ""}
            ${row.recheck && typeof row.recheck === "object" ? `<section class="section-note">
                <strong>补证后重评 · 原判断及原结果保留</strong>
                <dl><dt>重评截至</dt><dd>${text(row.recheck.as_of)}</dd>
                    <dt>关联结果</dt><dd>${text(row.recheck.result || "未核验")}</dd>
                    <dt>证据状态</dt><dd>${text(row.recheck.evidence_status)}</dd></dl>
                <details><summary>补充证据</summary><pre>${text(JSON.stringify(row.recheck, null, 2))}</pre></details></section>` : ""}
            ${row.symbol ? `<button type="button" class="action-button" data-review-index="${index}" data-review-symbol="${text(row.symbol)}">查看 ${text(row.symbol)}</button>` : ""}
        </div>
    </details>`;
}

function renderDecisionHistory(board, error = null, items = [], sources = []) {
    const panel = document.getElementById("decisionHistoryPanel");
    if (!panel) return;
    rememberWorkbenchSource("review", items);
    const sourceScope = '<p class="section-note">来源范围：策略观察与策略账事件、当前 AI 档位的近期纸面成交和 AI 判断核对，只包含本次可读取记录。C 线独立核验源未接入本页，其完整记录不包含在本页计数中；来源读取失败时数量未知。</p>';
    if (error && !items.length) {
        panel.onclick = null;
        panel.innerHTML = sourceScope + missingBlock(`复盘来源读取失败，记录数量未知：${escapeHtml(errorText(error))}`);
        return;
    }
    const buckets = [
        { id: "mismatch", label: "需复核" },
        { id: "pending", label: "未到期" },
        { id: "early_hit", label: "观察中·提前命中" },
        { id: "evidence_gap", label: "到期缺证据" },
        { id: "match", label: "符合" },
        { id: "historical", label: "事后复盘" },
        { id: "fill", label: "纸面成交" },
        { id: "data_gap", label: "缺证据·到期未知" },
        { id: "other", label: "观察/未分类" },
        { id: "all", label: "全部" },
    ];
    const countOf = (id) => (id === "all" ? items.length : items.filter((item) => item.bucket === id).length);
    const renderRows = (bucketId) => {
        const rows = bucketId === "all" ? items : items.filter((item) => item.bucket === bucketId);
        if (!rows.length) return missingBlock("这一类目前没有记录。");
        return rows.map((row) => row.kind === "ai_decision" ? renderAiDecisionRecord(row, items.indexOf(row)) : `
            <button class="report-row review-row" type="button" data-review-index="${items.indexOf(row)}" data-review-symbol="${escapeHtml(safeText(row.symbol))}">
                <span>${escapeHtml(safeText(row.date))}</span>
                <strong>${escapeHtml(safeText([row.name, row.symbol].filter(Boolean).join(" ")))}</strong>
                <span>${escapeHtml(safeText(row.ledger))} · ${escapeHtml(safeText(row.action))}</span>
                <span>${escapeHtml(safeText(row.reason))}<small>${escapeHtml(safeText(row.source_scope))}</small></span>
            </button>
        `).join("");
    };
    if (!buckets.some((bucket) => bucket.id === currentReviewBucket)) currentReviewBucket = "mismatch";
    if (items.length && countOf(currentReviewBucket) === 0) {
        currentReviewBucket = ["mismatch", "evidence_gap", "pending", "early_hit", "all"]
            .find((bucket) => countOf(bucket) > 0) || "all";
    }
    panel.innerHTML = `
        <p class="section-note">纸面 · 未验证</p>
        ${sourceScope}
        ${renderDecisionOutcomeSummary(sources.find((source) => source.path === "/api/ai-trader/decisions"))}
        ${sources.map((source) => `<p class="section-note">${escapeHtml(source.label)} · ${source.available ? (source.degraded ? "降级，范围可能不完整" : "本次已读") : "读取失败，数量未知"} · 截至 ${escapeHtml(safeText(source.asOf))}${(source.warnings || []).map((warning) => ` · ${escapeHtml(safeText(warning))}`).join("")}</p>`).join("")}
        <details class="owner-review-raw"><summary>查看原记录结果计数（含提前命中）</summary><div class="metric-row">
            ${["陈述与数据不符", "错", "已到期 · 证据不足", "未到期", "对"].map((result) => {
                const source = sources.find((entry) => entry.path === "/api/ai-trader/decisions");
                const count = items.filter((item) => item.kind === "ai_decision" && item.bucket !== "historical" && item.result === result).length;
                return `<article class="metric-box"><span>原记录 · ${escapeHtml(result)}</span><strong>${source?.available ? `${count}${source.degraded ? "（已读）" : ""}` : "未知"}</strong></article>`;
            }).join("")}
        </div></details>
        <div class="review-filters" role="tablist">
            ${buckets.map((bucket) => `
                <button type="button" class="review-filter" data-review-bucket="${bucket.id}" ${bucket.id === currentReviewBucket ? 'aria-current="page"' : ""}>
                    ${escapeHtml(bucket.label)} ${countOf(bucket.id)}
                </button>
            `).join("")}
        </div>
        <div id="reviewRowHost" class="paper-daily-report">${renderRows(currentReviewBucket)}</div>
        <p class="panel-disclaimer">事后复盘另计；记录接收不代表成交。Research Only · 纸面 · 未验证。</p>
    `;
    const host = panel.querySelector("#reviewRowHost");
    panel.querySelectorAll("[data-review-bucket]").forEach((button) => {
        button.addEventListener("click", () => {
            currentReviewBucket = button.dataset.reviewBucket || "all";
            panel.querySelectorAll("[data-review-bucket]").forEach((other) => other.removeAttribute("aria-current"));
            button.setAttribute("aria-current", "page");
            if (host) host.innerHTML = renderRows(button.dataset.reviewBucket || "all");
        });
    });
    // This host survives reloads; assigning one handler avoids listener buildup.
    panel.onclick = (event) => {
        const row = event.target.closest("[data-review-symbol]");
        if (!row || !row.dataset.reviewSymbol) return;
        const item = items[Number(row.dataset.reviewIndex)];
        if (!item) return;
        showDetail({ symbol: item.symbol, name: item.name || item.symbol }, { source: "review", profile: item.profile || aiTraderProfile, items, eventId: item.event_id || item.id || "" });
    };
}

function setupAgentAssistant() {
    const refreshButton = document.getElementById("refreshAgentContextButton");
    const sendButton = document.getElementById("sendAgentQuestionButton");
    const clearButton = document.getElementById("clearAgentChatButton");
    const addPaperButton = document.getElementById("addDiscussedCandidateToPaperButton");
    const input = document.getElementById("agentQuestionInput");
    if (refreshButton) refreshButton.addEventListener("click", () => loadAgentAssistant(true));
    if (sendButton) sendButton.addEventListener("click", sendAgentQuestion);
    if (addPaperButton) addPaperButton.addEventListener("click", addDiscussedCandidateToPaperPool);
    if (clearButton) {
        clearButton.addEventListener("click", () => {
            agentChatHistory = [];
            lastAgentResearchOpinion = "";
            lastAgentPaperDecision = null;
            renderPendingAgentCandidateAction();
            const transcript = document.getElementById("agentTranscript");
            if (transcript) {
                transcript.innerHTML = `<div class="empty-state">对话已清空。可以继续基于当前本地证据提问。</div>`;
            }
        });
    }
    if (input) {
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendAgentQuestion();
            }
        });
    }
}

function renderPendingAgentCandidateAction() {
    const button = document.getElementById("addDiscussedCandidateToPaperButton");
    if (!button) return;
    if (!pendingAgentCandidate) {
        button.hidden = true;
        button.disabled = true;
        button.textContent = "带本次意见加入纸面模拟池";
        return;
    }
    button.hidden = false;
    button.disabled = !lastAgentResearchOpinion.trim();
    const name = pendingAgentCandidate.name || pendingAgentCandidate.symbol || "当前讨论标的";
    const decision = normalizeAgentPaperDecision(lastAgentPaperDecision);
    const label = decision.action_label || "确认加入纸面模拟池";
    button.textContent = lastAgentResearchOpinion.trim()
        ? `${label}：${name}`
        : `等待 ${name} 的研究意见`;
}

function normalizeAgentPaperDecision(decision) {
    if (!decision || typeof decision !== "object" || decision.available === false) {
        return {
            recommended_lifecycle_action: "observe",
            action_label: "仅观察讨论",
            decision_status: "数据缺失",
        };
    }
    const action = safeText(decision.recommended_lifecycle_action || "observe");
    const labels = {
        add_to_paper: "确认加入纸面模拟池",
        review: "确认降级复盘",
        continue: "继续纸面验证",
        archive: "归档保留",
        observe: "仅观察讨论",
    };
    return {
        ...decision,
        recommended_lifecycle_action: action,
        action_label: safeText(decision.action_label || labels[action] || "仅观察讨论"),
    };
}

function renderAgentPaperDecisionCard(decision) {
    if (!decision || typeof decision !== "object" || decision.available === false) return "";
    const normalized = normalizeAgentPaperDecision(decision);
    return `
        <div class="agent-decision-card">
            <strong>LLM 纸面池决策：${escapeHtml(safeText(normalized.decision_status))}</strong>
            <div class="agent-decision-grid">
                <span>主动作：${escapeHtml(safeText(normalized.primary_action))}</span>
                <span>置信度：${escapeHtml(safeText(normalized.confidence))}</span>
                <span>验证周期：${escapeHtml(safeText(normalized.validation_days_raw || normalized.validation_days))}</span>
                <span>研究观察：${escapeHtml(safeText(normalized.action_label))}</span>
            </div>
            <p>${highlightText(normalized.review_sentence || "等待用户确认是否进入纸面模拟流程。")}</p>
            <p>成功条件：${highlightText(normalized.success_condition || "数据缺失")}</p>
            <p>失败条件：${highlightText(normalized.failure_condition || "数据缺失")}</p>
            <p>需补数据：${highlightText(normalized.missing_data || "数据缺失")}</p>
        </div>
    `;
}

async function loadAgentAssistant(force = false) {
    const note = document.getElementById("agentStatusNote");
    const panel = document.getElementById("agentContextPanel");
    if (!force && agentStatusLoaded) return;
    if (note) note.textContent = "正在读取 Agent 配置和本地上下文…";
    if (panel) panel.innerHTML = `<div class="empty-state">正在读取本地上下文…</div>`;
    try {
        const [skillsPayload, contextPayload] = await Promise.all([
            requestJson("/api/agent/skills"),
            requestJson("/api/agent/context"),
        ]);
        renderAgentSkills(skillsPayload);
        renderAgentContext(contextPayload.context || {}, skillsPayload);
        const llm = skillsPayload.llm || {};
        const cfg = skillsPayload.config || {};
        const provider = safeText(cfg.default_provider || "offline_stub");
        const status = agentLlmStatus(skillsPayload);
        if (note) {
            note.textContent = `${status.summary} · 外部数据源：${provider} · 本地上下文已读取`;
        }
        agentStatusLoaded = true;
    } catch (error) {
        if (note) note.textContent = `Agent 初始化失败：${escapeHtml(errorText(error))}`;
        if (panel) panel.innerHTML = `<div class="empty-state">无法读取 Agent 上下文：${escapeHtml(errorText(error))}</div>`;
    }
}

function renderAgentSkills(payload) {
    const select = document.getElementById("agentSkillSelect");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = `<option value="">自由问答</option>`;
    (payload.skills || []).forEach((name) => {
        const option = document.createElement("option");
        option.value = String(name);
        option.textContent = agentSkillLabel(name);
        select.appendChild(option);
    });
    if ([...select.options].some((option) => option.value === previous)) {
        select.value = previous;
    }
}

function agentSkillLabel(name) {
    const labels = {
        candidate_explainer: "个股入选解释",
        daily_review: "每日复盘",
        paper_simulation_decision: "纸面模拟池决策",
        risk_audit: "风险复核",
    };
    return labels[String(name)] || String(name);
}

function agentLlmStatus(payload) {
    const llm = payload?.llm || {};
    const cfg = payload?.config || {};
    const provider = safeText(cfg.default_provider || "offline_stub");
    const errors = Array.isArray(llm.errors) ? llm.errors.filter(Boolean) : [];
    const warnings = Array.isArray(llm.warnings) ? llm.warnings.filter(Boolean) : [];
    if (!llm.llm_enabled) {
        return {
            summary: "外部 LLM 未启用，本地证据分析可用",
            detail: "不会调用外部 API；仍可基于当前候选池做本地复盘。",
            tone: "watch",
        };
    }
    if (llm.ok === false || errors.length) {
        const reason = errors[0] || "配置未通过校验";
        return {
            summary: "外部 API 未就绪，本地证据分析可用",
            detail: `${provider}: ${reason}`,
            tone: "warn",
        };
    }
    if (warnings.length) {
        return {
            summary: "外部 API 可用，但存在配置提醒",
            detail: `${provider}: ${warnings[0]}`,
            tone: "watch",
        };
    }
    return {
        summary: "外部 API 已就绪，本地证据分析可用",
        detail: `${provider}: 配置校验通过；只有勾选 API 调用时才会联网。`,
        tone: "ok",
    };
}

function renderAgentContext(context, skillsPayload = null) {
    const panel = document.getElementById("agentContextPanel");
    if (!panel) return;
    const preview = Array.isArray(context.candidates_preview) ? context.candidates_preview : [];
    const paperPreview = Array.isArray(context.paper_simulation_preview) ? context.paper_simulation_preview : [];
    const scorecard = context.strategy_scorecard_summary || {};
    const warnings = Array.isArray(context.warnings) ? context.warnings : [];
    const llmStatus = skillsPayload ? agentLlmStatus(skillsPayload) : null;
    const items = [
        ["工作流状态", `${safeText(context.workflow_status || "数据缺失")} / ${context.workflow_ok ? "ok" : "未确认"}`],
        ["目标日期", safeText(context.target_date || context.latest_local_date || "数据缺失")],
        ["候选数量", safeText(context.candidate_count ?? currentCandidates.length ?? "数据缺失")],
        ["页面今日新候选", safeText(currentTodayRecommendations.length)],
        ["页面观察池", safeText(currentObservationCandidates.length)],
        ["纸面模拟池", safeText(context.paper_simulation_watchlist_count ?? scorecard.paper_pool_count ?? "数据缺失")],
        ["需降级复盘", safeText(scorecard.review_needed_count ?? "数据缺失")],
        ["Agent API", llmStatus ? llmStatus.detail : "配置状态未读取"],
        ["关键告警", warnings.length ? warnings.slice(0, 3).map((item) => safeText(item)).join("；") : "暂无关键告警"],
    ];
    const candidateHtml = preview.length
        ? `<div class="agent-context-item"><span>候选预览</span><strong>${preview.slice(0, 5).map((item) => `${safeText(item.name || item.symbol)} ${safeText(item.symbol || "")}`).join(" / ")}</strong></div>`
        : `<div class="agent-context-item"><span>候选预览</span><strong>后端上下文暂无候选预览，发送问题时会附带当前页面候选摘要</strong></div>`;
    panel.innerHTML = `
        ${items.map(([label, value]) => `
            <div class="agent-context-item">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value)}</strong>
            </div>
        `).join("")}
        ${candidateHtml}
        ${paperPreview.length ? `<div class="agent-context-item"><span>纸面池预览</span><strong>${paperPreview.slice(0, 4).map((item) => `${safeText(item.name || item.symbol)} ${safeText(item.expectation_fit_label || item.review_label || "")}`).join(" / ")}</strong></div>` : ""}
    `;
}

function buildAgentClientContext() {
    const compactCandidate = (item) => ({
        symbol: item.symbol || item.ts_code || "",
        name: item.name || item.stock_name || "",
        strategy_type: item.strategy_type || item.strategy || item.lane || "",
        priority_rank: item.priority_rank || item.rank || "",
        action_state: item.action_state || item.action || "",
        reason: item.reason || item.selection_reason || item.human_reason || "",
    });
    return {
        ui_candidate_count: currentCandidates.length,
        ui_today_recommendations: currentTodayRecommendations.slice(0, 8).map(compactCandidate),
        ui_observation_candidates: currentObservationCandidates.slice(0, 8).map(compactCandidate),
        ui_paper_loop_intent: "今日重点 -> LLM 研究意见 -> 用户确认 -> 纸面模拟池 -> 原始预期验证 -> 降级复盘/策略成绩单",
        ui_research_only: true,
        ui_no_broker: true,
        ui_not_investment_advice: true,
        focused_candidate: pendingAgentCandidate ? {
            symbol: pendingAgentCandidate.symbol || "",
            name: pendingAgentCandidate.name || "",
            entry_date: pendingAgentCandidate.entry_date || "",
            lane: pendingAgentCandidate.lane || "",
            source: "ui_candidate_discussion",
        } : null,
    };
}

// ==================== AI 纸面交易员（独立纸面账本，只读展示） ====================
// 数据全部来自 /api/ai-trader/dashboard 与 /api/ai-trader/chat 后端字段；
// 前端不重算纸面权益、收益、持仓或回撤。聊天只能解释，不能下单。

let aiTraderProfile = "default";
let aiTraderProfileRevision = 0;
let aiTraderRequestSequence = 0;

function aiTraderProfileQuery(profile = aiTraderProfile) {
    return profile === "default" ? "" : `?profile=${encodeURIComponent(profile)}`;
}

function aiTraderProfileLabel(profile = aiTraderProfile) {
    return profile === "default" ? "默认 AI" : "GLM";
}

function setAiTraderProfile(profile) {
    const nextProfile = profile === "glm" ? "glm" : "default";
    if (nextProfile !== aiTraderProfile) {
        // A -> B -> A must invalidate the first A request as well.
        aiTraderProfileRevision += 1;
        decisionHistoryRequestSequence += 1;
        aiTraderRequestSequence += 1;
    }
    aiTraderProfile = nextProfile;
    for (const button of document.querySelectorAll("[data-ai-trader-profile]")) {
        button.setAttribute("aria-pressed", button.dataset.aiTraderProfile === aiTraderProfile ? "true" : "false");
    }
    const chatDisabled = aiTraderProfile !== "default";
    const input = document.getElementById("aiTraderChatInput");
    const sendButton = document.getElementById("sendAiTraderChatButton");
    if (input) input.disabled = chatDisabled;
    if (sendButton) sendButton.disabled = chatDisabled;
}

function setupAiTraderPanel() {
    const refreshButton = document.getElementById("refreshAiTraderButton");
    if (refreshButton) refreshButton.addEventListener("click", () => loadAiTraderPanel());
    for (const button of document.querySelectorAll("[data-ai-trader-profile]")) {
        button.addEventListener("click", () => {
            setAiTraderProfile(button.dataset.aiTraderProfile);
            loadAiTraderPanel();
            loadDecisionHistory();
        });
    }
    const sendButton = document.getElementById("sendAiTraderChatButton");
    const input = document.getElementById("aiTraderChatInput");
    if (sendButton) sendButton.addEventListener("click", () => sendAiTraderChat());
    if (input) {
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                sendAiTraderChat();
            }
        });
    }
}

async function fetchAiTraderJson(url, options = {}) {
    if (!options.method || options.method.toUpperCase() === "GET") {
        return requestJson(url, { ...options, responseEnvelope: true });
    }
    assertRuntimeMutationAllowed(url);
    const response = await fetch(url, options);
    let payload = null;
    try {
        payload = await response.json();
    } catch (error) {
        payload = null;
    }
    return { ok: response.ok, status: response.status, payload };
}

function aiTraderKv(label, value, valueClass = "") {
    return `<div class="ai-trader-kv"><span>${escapeHtml(label)}</span><span class="${valueClass}">${value}</span></div>`;
}

function aiTraderNum(value, digits = 2) {
    const n = numericValue(value);
    if (n === null) return "—";
    return n.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function aiTraderPnlClass(value) {
    const n = numericValue(value);
    if (n === null || n === 0) return "";
    return n > 0 ? "ai-pnl-up" : "ai-pnl-down";
}

async function loadAiTraderPanel() {
    const note = document.getElementById("aiTraderStatusNote");
    const profile = aiTraderProfile;
    const profileRevision = aiTraderProfileRevision;
    const requestId = ++aiTraderRequestSequence;
    const isCurrent = () => requestId === aiTraderRequestSequence && profileRevision === aiTraderProfileRevision;
    const label = aiTraderProfileLabel(profile);
    if (note) note.textContent = `正在读取 ${label} 的 AI 纸面交易员状态…`;
    try {
        const result = await fetchAiTraderJson("/api/ai-trader/dashboard" + aiTraderProfileQuery(profile), { headers: { Accept: "application/json" } });
        if (!isCurrent()) return;
        const payload = result.payload;
        if (!payload) {
            if (note) note.textContent = `${label} · AI 纸面交易员数据读取失败（HTTP ${result.status}）。`;
            return;
        }
        renderAiTraderDashboard(payload);
        await loadAiTraderChatHistory();
        if (note && isCurrent()) {
            note.textContent = payload.degraded
                ? `${label} · 已降级展示：${(payload.warnings || []).slice(0, 2).join("；") || "部分数据缺失"}`
                : `${label} · 已加载 · 更新时间 ${safeText(payload.updated_at)}`;
        }
    } catch (error) {
        if (note && isCurrent()) note.textContent = `${label} · 数据读取失败：${errorText(error)}`;
    }
}

function renderAiTraderDashboard(payload) {
    renderAiTraderWarnings(payload.warnings || [], payload.degraded === true);
    renderAiTraderAccount(payload.account || {});
    renderAiTraderSchedule(payload.schedule_status || {}, payload.llm || {}, payload.source_availability?.plan === false);
    renderAiTraderLlmRuntime(payload.llm_runtime || {});
    renderAiTraderPositions(payload.positions || [], payload.source_availability?.positions === false || (payload.degraded === true && numericValue(payload.account?.equity) === null));
    renderAiTraderJournal(payload.latest_journal || {}, payload.llm || {});
    renderAiTraderProposed(payload);
    renderAiTraderFills(payload);
    renderAiTraderPhilosophy(payload);
    renderAiTraderSource(payload);
}

function renderAiTraderLlmRuntime(runtime) {
    const runtimeHost = document.getElementById("aiTraderLlmRuntimePanel");
    const disclosureHost = document.getElementById("aiTraderLlmDisclosurePanel");
    if (!runtimeHost || !disclosureHost) return;
    const providerLabel = runtime.provider === "openai" ? "OpenAI-compatible" : "Anthropic-compatible";
    const external = runtime.is_external === true;
    const hostHtml = [
        aiTraderKv("LLM 服务类型", external ? "External LLM" : "Local LLM"),
        aiTraderKv("Provider", escapeHtml(providerLabel)),
        aiTraderKv("Model", escapeHtml(safeText(runtime.model))),
        aiTraderKv("Endpoint host", escapeHtml(safeText(runtime.endpoint_host))),
        aiTraderKv("已配置", runtime.configured ? "是 configured" : "否（未配置）"),
        aiTraderKv("凭据存在", runtime.token_present ? "是（token 不展示）" : "否"),
        aiTraderKv("外部端点已明确授权", runtime.external_allowed ? "是" : "否（未授权时不会联网）"),
    ].join("");
    runtimeHost.innerHTML = hostHtml;
    if (external) {
        const disclosure = Array.isArray(runtime.context_disclosure) ? runtime.context_disclosure : [];
        disclosureHost.innerHTML = `
            <div class="badge-row" style="justify-content:flex-start;margin:8px 0 4px">
                <span class="badge-degraded">Data Leaves This Device</span>
            </div>
            <p class="section-note" style="margin:2px 0 8px;color:#B45309;">
                该功能会把纸面账户摘要、持仓、候选、主张、近期复盘及聊天上下文发送到外部模型服务。
                不包含券商账户、真实订单或正式 QuantDesk 账本写权限。
            </p>
            <p class="section-note" style="margin:0;">将发送的上下文字段：${escapeHtml(disclosure.join("、") || "-")}</p>
        `;
    } else {
        disclosureHost.innerHTML = "";
    }
}

function renderAiTraderWarnings(warnings, degraded) {
    const host = document.getElementById("aiTraderWarningsPanel");
    if (!host) return;
    if (!warnings.length && !degraded) {
        host.innerHTML = "";
        return;
    }
    const items = warnings.length ? warnings : ["degraded"];
    host.innerHTML = `
        <div class="badge-row" style="justify-content:flex-start;margin-bottom:10px">
            ${degraded ? `<span class="badge-degraded">已降级 degraded</span>` : ""}
            ${items.slice(0, 8).map((item) => `<span class="badge-degraded">${escapeHtml(String(item))}</span>`).join("")}
        </div>
    `;
}

function renderAiTraderAccount(account) {
    const host = document.getElementById("aiTraderAccountPanel");
    if (!host) return;
    host.innerHTML = [
        aiTraderKv("权益（模拟）", aiTraderNum(account.equity)),
        aiTraderKv("现金", aiTraderNum(account.cash)),
        aiTraderKv("已实现盈亏（纸面）", aiTraderNum(account.realized_pnl)),
        aiTraderKv("未实现盈亏（纸面）", aiTraderNum(account.unrealized_pnl)),
        aiTraderKv("持仓数", aiTraderNum(account.positions_count, 0)),
        aiTraderKv("账本更新时间", safeText(account.updated_at)),
        aiTraderKv("数据日期", safeText(account.as_of)),
        aiTraderKv("初始资金", aiTraderNum(account.initial_cash)),
    ].join("");
}

function renderAiTraderSchedule(schedule, llm, planUnavailable = false) {
    const host = document.getElementById("aiTraderSchedulePanel");
    if (!host) return;
    host.innerHTML = [
        aiTraderKv("最近复盘日期", safeText(schedule.last_journal_date)),
        aiTraderKv("本次判断方式", escapeHtml(llm.decision_label || (llm.mode === "model" ? "模型判断" : "未取得模型判断"))),
        aiTraderKv("请求模型", escapeHtml(llm.requested_model || "未知")),
        aiTraderKv("实际响应模型", escapeHtml(llm.actual_response_model || "未知（无可核实响应）")),
        aiTraderKv("请求结果", escapeHtml(({provider_gone: "服务商返回 410，模型已不可用", response_headers_timeout: "等待响应头超时", connect_timeout: "建立连接超时", answer_rejected: "回答未通过核对"})[llm.failure_category] || llm.failure_category || "未知")),
        aiTraderKv("请求时点", escapeHtml(llm.request_started_at || "未知")),
        aiTraderKv("记录来源", escapeHtml(llm.model_source || "未知")),
        aiTraderKv("不可用原因", llm.unavailable_reason ? escapeHtml(String(llm.unavailable_reason)) : "—"),
        aiTraderKv("恢复状态", escapeHtml(llm.recovery_status || "未记录")),
        aiTraderKv("复用原判断", llm.raw_response_reused === true ? "是，保留原记录时间" : "未记录复用"),
        aiTraderKv("计划状态", humanStatus(schedule.plan_status)),
        aiTraderKv("是否提交", planUnavailable ? "计划缺失，记录状态未知" : schedule.plan_committed ? "已记录计划（不等于成交）" : "计划尚未记录"),
        aiTraderKv("目标交易日", safeText(schedule.trade_date)),
        aiTraderKv("数据是否陈旧", numericValue(schedule.age_days) === null || typeof schedule.stale !== "boolean"
            ? "未知（未提供可核对的时间状态）"
            : schedule.stale ? `陈旧（${safeText(schedule.age_days)} 天未更新）` : `正常（${safeText(schedule.age_days)} 天内）`),
        aiTraderKv("调度入口", "晚间 20:20 计划任务（本页只读，无启动/停止按钮）"),
    ].join("");
}

function renderAiTraderPositions(positions, unavailable = false) {
    rememberWorkbenchSource("ai_positions", positions);
    const host = document.getElementById("aiTraderPositionsPanel");
    if (!host) return;
    if (!positions.length) {
        host.innerHTML = unavailable
            ? '<div class="empty-state">AI 纸面账缺失或不可读，纸面持仓状态未知；不能据此认定纸面账户空仓。</div>'
            : '<div class="empty-state">当前没有纸面持仓（空仓也是它的决定之一）。</div>';
        return;
    }
    const rows = positions.map((p) => `
        <tr>
            <td><button type="button" class="action-button secondary-action" data-ai-position-symbol="${escapeHtml(safeText(p.symbol))}">${escapeHtml(safeText(p.symbol))}</button></td>
            <td>${escapeHtml(safeText(p.name !== undefined ? p.name : ""))}</td>
            <td>${escapeHtml(aiTraderNum(p.quantity, 0))}</td>
            <td>${escapeHtml(aiTraderNum(p.avg_cost))}</td>
            <td>${p.stale_mark ? `${escapeHtml(aiTraderNum(p.market_price))}（沿用上次价）` : escapeHtml(aiTraderNum(p.market_price))}</td>
            <td class="${aiTraderPnlClass(p.unrealized_pnl)}">${escapeHtml(aiTraderNum(p.unrealized_pnl))}</td>
            <td>${escapeHtml(safeText(p.style) === "数据缺失" ? "—" : safeText(p.style))}</td>
            <td>${escapeHtml(safeText(p.planned_hold_days ?? "—"))}</td>
            <td>${escapeHtml(safeText(p.acquired_on ?? p.entry_date ?? "—"))}</td>
        </tr>
    `).join("");
    host.innerHTML = `
        <div class="paper-table-wrap">
            <table class="compact-table paper-positions-table">
                <thead><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>现价</th><th>浮动盈亏</th><th>做法</th><th>计划持有(天)</th><th>买入日</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
    host.querySelectorAll("[data-ai-position-symbol]").forEach((button) => {
        button.onclick = () => {
            const item = positions.find((position) => String(position.symbol) === button.dataset.aiPositionSymbol);
            if (item) return showDetail(item, { source: "ai_positions", items: positions });
        };
    });
}

function renderAiTraderJournal(journal, llm) {
    const host = document.getElementById("aiTraderJournalPanel");
    if (!host) return;
    const lessons = Array.isArray(journal.lessons) ? journal.lessons.filter(Boolean) : [];
    host.innerHTML = `
        <p style="margin:4px 0;font-weight:600;">${escapeHtml(safeText(journal.headline))}</p>
        <p class="section-note" style="margin:4px 0;">${escapeHtml(safeText(journal.thought))}</p>
        ${aiTraderKv("复盘日期", safeText(journal.as_of))}
        ${aiTraderKv("次日计划交易日", safeText(journal.trade_date))}
        ${llm.mode === "unknown" ? aiTraderKv("AI 大脑", "未评估 / 来源未知") : llm.used
            ? aiTraderKv("AI 大脑", `已使用 · ${escapeHtml(safeText(llm.model))}`)
            : aiTraderKv("AI 大脑", llm.unavailable_reason
                ? `未使用 · 原因：${escapeHtml(String(llm.unavailable_reason))}`
                : "未使用")}
        ${(llm.market_note ? aiTraderKv("盘面点评", escapeHtml(String(llm.market_note))) : "")}
        ${lessons.length ? `<ul class="section-note" style="margin:6px 0 0;padding-left:18px;">${lessons.map((l) => `<li>${escapeHtml(String(l))}</li>`).join("")}</ul>` : ""}
    `;
}

function renderAiTraderProposed(payload) {
    const host = document.getElementById("aiTraderProposedPanel");
    if (!host) return;
    if (payload.source_availability?.plan === false) {
        host.innerHTML = '<div class="empty-state">计划缺失或不可读，无法判断模型提议及提交状态。</div>';
        return;
    }
    const proposed = Array.isArray(payload.proposed_symbols) ? payload.proposed_symbols : [];
    const notCommitted = Array.isArray(payload.proposed_not_committed_symbols) ? payload.proposed_not_committed_symbols : [];
    // Only what the backend certified as committed shows here; a deferred or
    // unsubmitted plan yields an empty list and its raw intents surface in
    // uncommitted_intents below.
    const intents = Array.isArray(payload.committed_intents) ? payload.committed_intents : [];
    const uncommittedIntents = Array.isArray(payload.uncommitted_intents) ? payload.uncommitted_intents : [];
    const planCommitted = payload.latest_plan?.committed === true;
    const notCommittedReason = payload.latest_plan?.not_committed_reason || "";
    let html = "";
    html += `<p class="section-note" style="margin:4px 0;">LLM 提议（模型提出过）：`;
    html += proposed.length
        ? proposed.map((row) => {
            const committedStage = row.stage === "committed";
            const cls = committedStage ? "ai-trader-stage-committed" : "ai-trader-stage-proposed";
            return `<span class="${cls}">${escapeHtml(safeText(row.symbol))}（${escapeHtml(row.stage_zh || row.stage || "")}${row.style ? "·" + escapeHtml(String(row.style)) : ""}）</span>`;
        }).join("、")
        : "无记录";
    html += `</p>`;
    if (notCommitted.length) {
        html += `<p class="section-note" style="margin:4px 0;color:#B45309;">注意：${escapeHtml(notCommitted.join("、"))} 只是提议，未通过账户约束，没有写入次日纸面计划。</p>`;
    }
    html += `<p class="section-note" style="margin:8px 0 4px;">实际 committed intents（已写入次日纸面计划${planCommitted ? "" : "；注意：今晚计划未提交 committed=false"}）：${intents.length ? "" : "无"}</p>`;
    if (!planCommitted && notCommittedReason) {
        html += `<p class="section-note" style="margin:2px 0;color:#B45309;">未提交原因：${escapeHtml(String(notCommittedReason))}</p>`;
    }
    if (intents.length) {
        const rows = intents.map((intent) => `
            <tr>
                <td>${escapeHtml(safeText(intent.symbol))}</td>
                <td>${escapeHtml(intent.side === "buy" ? "纸面买入" : intent.side === "sell" ? "纸面卖出" : safeText(intent.side))}</td>
                <td>${escapeHtml(aiTraderNum(intent.quantity, 0))}</td>
                <td>${escapeHtml(safeText(intent.style) === "数据缺失" ? "—" : safeText(intent.style))}</td>
                <td>${escapeHtml(safeText(intent.planned_hold_days ?? "—"))}</td>
                <td>${escapeHtml(intent.pick_source_zh || intent.pick_source || "—")}</td>
            </tr>
        `).join("");
        html += `
            <div class="paper-table-wrap">
                <table class="compact-table paper-positions-table">
                    <thead><tr><th>代码</th><th>方向</th><th>股数</th><th>做法</th><th>计划持有(天)</th><th>来源</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
    }
    if (!planCommitted && uncommittedIntents.length) {
        const auditRows = uncommittedIntents.map((intent) => `
            <tr>
                <td>${escapeHtml(safeText(intent.symbol))}</td>
                <td>${escapeHtml(intent.side === "buy" ? "纸面买入" : intent.side === "sell" ? "纸面卖出" : safeText(intent.side))}</td>
                <td>${escapeHtml(aiTraderNum(intent.quantity, 0))}</td>
            </tr>
        `).join("");
        html += `<p class="section-note" style="margin:8px 0 4px;">保留但未提交的 intents（仅审计展示，从未写入次日纸面计划，也不是成交记录）：</p>`;
        html += `
            <div class="paper-table-wrap">
                <table class="compact-table paper-positions-table">
                    <thead><tr><th>代码</th><th>方向</th><th>股数</th></tr></thead>
                    <tbody>${auditRows}</tbody>
                </table>
            </div>
        `;
    }
    host.innerHTML = html;
}

function renderAiTraderFills(payload) {
    const host = document.getElementById("aiTraderFillsPanel");
    if (!host) return;
    const fills = Array.isArray(payload.filled_trades) ? payload.filled_trades : [];
    const rejected = Array.isArray(payload.rejected_or_abandoned) ? payload.rejected_or_abandoned : [];
    const recentTrades = Array.isArray(payload.recent_trades) ? payload.recent_trades : [];
    const reviewSummary = payload.review_summary || {};
    let html = "";
    if (reviewSummary && Object.keys(reviewSummary).length) {
        html += aiTraderKv(
            "最近一次验收",
            `意图 ${safeText(reviewSummary.intents ?? "—")} · 开盘成交 ${safeText(reviewSummary.open_fills ?? "—")} · 放弃 ${safeText(reviewSummary.abandoned ?? "—")}`
        );
    }
    const fillRows = (fills.length ? fills : recentTrades).slice(0, 8).map((t) => `
        <tr>
            <td>${escapeHtml(safeText(t.date ?? t.settled_on ?? "—"))}</td>
            <td>${escapeHtml(safeText(t.symbol))}</td>
            <td>${escapeHtml(t.side === "buy" ? "纸面买入" : t.side === "sell" ? "纸面卖出" : safeText(t.side))}</td>
            <td>${escapeHtml(aiTraderNum(t.quantity, 0))}</td>
            <td>${escapeHtml(aiTraderNum(t.price))}</td>
            <td>${t.status ? escapeHtml(String(t.status)) : "filled"}${t.st_unknown ? `<small>身份未知 · ${escapeHtml(t.st_note || "缺少当时身份资料")}</small>` : ""}</td>
        </tr>
    `).join("");
    html += `<p class="section-note" style="margin:8px 0 4px;">已按历史开盘价完成的纸面成交：</p>`;
    html += fillRows
        ? `<div class="paper-table-wrap"><table class="compact-table paper-trades-table">
            <thead><tr><th>日期</th><th>代码</th><th>方向</th><th>股数</th><th>成交价</th><th>状态</th></tr></thead>
            <tbody>${fillRows}</tbody></table></div>`
        : `<div class="empty-state">${payload.source_availability?.trades === false && payload.source_availability?.actions === false
            ? "账本与成交记录缺失，无法判断是否成交。" : "本次可读取记录中没有纸面成交。"}</div>`;
    html += `<p class="section-note" style="margin:10px 0 4px;">被拒/放弃及原因：</p>`;
    html += rejected.length
        ? `<ul class="section-note" style="margin:0;padding-left:18px;">${rejected.slice(0, 6).map((r) => `<li>${escapeHtml(safeText(r.symbol))}：${escapeHtml(String(r.reason || r.status || "未知原因"))}${r.st_unknown ? " · 身份未知" : ""}</li>`).join("")}</ul>`
        : `<div class="empty-state">${payload.source_availability?.actions === false
            ? "复盘记录缺失，无法判断是否有被拒或放弃的委托。" : "本次可读取记录中没有被拒或放弃的委托。"}</div>`;
    host.innerHTML = html;
}

function renderAiTraderPhilosophy(payload) {
    const host = document.getElementById("aiTraderPhilosophyPanel");
    if (!host) return;
    if (payload.source_availability?.philosophy === false) {
        host.innerHTML = '<div class="empty-state">主张记录缺失或不可读，风格战绩及修订次数未知。</div>';
        return;
    }
    const stance = payload.stance || {};
    const styleLedger = payload.style_ledger || {};
    const notes = Array.isArray(payload.notes) ? payload.notes : [];
    let html = "";
    if (stance.summary) {
        html += aiTraderKv("当前投资主张", escapeHtml(String(stance.summary)));
    }
    if (Array.isArray(stance.preferred_styles) && stance.preferred_styles.length) {
        html += aiTraderKv("偏好风格", escapeHtml(stance.preferred_styles.join("、")));
    }
    if (stance.reasoning) {
        html += `<p class="section-note" style="margin:4px 0;">理由：${escapeHtml(String(stance.reasoning))}</p>`;
    }
    if (payload.philosophy_error) {
        html += `<p class="section-note" style="color:#B45309;margin:4px 0;">主张文件读取异常：${escapeHtml(String(payload.philosophy_error))}</p>`;
    }
    const ledgerEntries = Object.entries(styleLedger);
    html += `<p class="section-note" style="margin:8px 0 4px;">风格战绩（按它自己标注的做法归集的真实纸面结果）：</p>`;
    html += ledgerEntries.length
        ? `<ul class="section-note" style="margin:0;padding-left:18px;">${ledgerEntries.map(([style, stat]) => {
            const s = stat || {};
            return `<li>${escapeHtml(style)}：${safeText(s.trades ?? 0)} 笔 · 纸面盈利占比 ${escapeHtml(aiTraderNum(Number(s.win_rate ?? 0) * 100, 1))}% · 平均净 ${escapeHtml(aiTraderNum(Number(s.mean_net ?? 0) * 100, 2))}% · 平均持有 ${safeText(s.mean_hold_days ?? "—")} 天</li>`;
        }).join("")}</ul>`
        : '<div class="empty-state">还没有平仓记录，风格战绩暂时为空。</div>';
    if (notes.length) {
        html += `<p class="section-note" style="margin:8px 0 4px;">它自己记下的教训：</p>`;
        html += `<ul class="section-note" style="margin:0;padding-left:18px;">${notes.map((n) => `<li>${escapeHtml(String(n.on || ""))}：${escapeHtml(String(n.note || ""))}</li>`).join("")}</ul>`;
    }
    if (payload.revisions_count !== undefined) {
        html += aiTraderKv("主张修订次数", safeText(payload.revisions_count));
    }
    host.innerHTML = html;
}

function renderAiTraderSource(payload) {
    const host = document.getElementById("aiTraderSourcePanel");
    if (!host) return;
    const recView = payload.recommendation_view || {};
    const symbols = Array.isArray(recView.symbols) ? recView.symbols : [];
    const breakdown = recView.committed_source_breakdown || {};
    const intents = Array.isArray(payload.committed_intents) ? payload.committed_intents : [];
    let html = "";
    html += aiTraderKv(
        "研究台推荐池（仅线索）",
        symbols.length ? escapeHtml(symbols.join("、")) : "无（缺失时保持缺失，不编造）"
    );
    html += aiTraderKv("推荐池数量", safeText(recView.count ?? symbols.length));
    html += aiTraderKv("advisory_only", String(recView.advisory_only !== false));
    const breakdownText = Object.keys(breakdown).length
        ? Object.entries(breakdown).map(([key, count]) => `${escapeHtml(key)}: ${safeText(count)}`).join("；")
        : "暂无提交委托";
    html += aiTraderKv("已提交委托来源统计", breakdownText);
    const bySource = intents.map((i) => `${escapeHtml(safeText(i.symbol))}=${escapeHtml(i.pick_source_zh || i.pick_source || "—")}`).join("；");
    html += aiTraderKv("本次逐笔来源", bySource || "—");
    if (Array.isArray(recView.warnings) && recView.warnings.length) {
        html += `<p class="section-note" style="color:#B45309;margin:4px 0;">推荐池告警：${recView.warnings.map((w) => escapeHtml(String(w))).join("；")}</p>`;
    }
    html += `<p class="section-note" style="margin:6px 0 0;">来源说明：研究台推荐只作参考输入，AI 可以从里面挑，也可以完全自选；每笔提交都带 pick_source 归因。</p>`;
    host.innerHTML = html;
}

async function loadAiTraderChatHistory() {
    const host = document.getElementById("aiTraderChatLog");
    if (!host) return;
    const profileRevision = aiTraderProfileRevision;
    if (aiTraderProfile !== "default") {
        host.innerHTML = `<div class="empty-state">${escapeHtml(aiTraderProfile.toUpperCase())} 分支的聊天面板暂不可用（聊天仍只读默认账）；它自己的结论看上方账户、计划与决策页。</div>`;
        return;
    }
    const result = await fetchAiTraderJson("/api/ai-trader/chat", { headers: { Accept: "application/json" } });
    if (profileRevision !== aiTraderProfileRevision) return;
    const messages = result.payload?.messages;
    if (!Array.isArray(messages)) {
        host.innerHTML = '<div class="empty-state">聊天记录读取失败。</div>';
        return;
    }
    if (!messages.length) {
        host.innerHTML = '<div class="empty-state">还没聊过。可以问：600172 为什么还拿着？你现在怎么看中线做法？</div>';
        return;
    }
    host.innerHTML = messages.map((turn) => `
        <div class="ai-trader-chat-turn">
            <p class="ai-trader-chat-q">你：${escapeHtml(safeText(turn.question))}</p>
            <p class="ai-trader-chat-a">${escapeHtml(safeText(turn.answer))}</p>
        </div>
    `).join("");
    host.scrollTop = host.scrollHeight;
}

async function sendAiTraderChat() {
    const input = document.getElementById("aiTraderChatInput");
    const button = document.getElementById("sendAiTraderChatButton");
    const status = document.getElementById("aiTraderChatStatus");
    const question = String(input?.value || "").trim();
    if (aiTraderProfile !== "default") {
        if (status) status.textContent = "GLM 分支的聊天暂不可用：聊天面板只读默认账，两个大脑互看聊天记录。";
        return;
    }
    if (!question) {
        if (status) status.textContent = "先输入问题再发送。聊天不能下单，仓位只在晚间纸面流程中变化。";
        return;
    }
    if (button) {
        button.disabled = true;
        button.textContent = "它在想…";
    }
    if (status) status.textContent = "正在让它想（可能需要十几秒）……聊天不能下单。";
    try {
        const result = await fetchAiTraderJson("/api/ai-trader/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify({ question }),
        });
        const payload = result.payload || {};
        if (!result.ok || payload.ok !== true) {
            const reason = payload.reason || payload.error || `HTTP ${result.status}`;
            if (status) status.textContent = `它没能回答（${reason}）。聊天不能下单，仓位只在晚间纸面流程中变化。`;
            return;
        }
        if (input) input.value = "";
        if (status) status.textContent = "已回答。聊天只是解释，不能下单；仓位只在每晚纸面流程中变化。";
        await loadAiTraderChatHistory();
    } catch (error) {
        if (status) status.textContent = `请求失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "发送";
        }
    }
}

function appendAgentMessage(role, text, flags = "", options = {}) {
    const transcript = document.getElementById("agentTranscript");
    if (!transcript) return null;
    const first = transcript.querySelector(".empty-state");
    if (first) first.remove();
    const div = document.createElement("div");
    div.className = `agent-message ${role}`;
    const body = options.html === true
        ? String(text || "")
        : escapeHtml(text).replace(/\n/g, "<br>");
    div.innerHTML = `
        <div class="who">${role === "user" ? "你" : "研究助手"}</div>
        ${body}
        ${flags ? `<div class="agent-flags">${escapeHtml(flags)}</div>` : ""}
    `;
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
    return div;
}

async function sendAgentQuestion() {
    const input = document.getElementById("agentQuestionInput");
    const sendButton = document.getElementById("sendAgentQuestionButton");
    const networkToggle = document.getElementById("agentNetworkToggle");
    const skillSelect = document.getElementById("agentSkillSelect");
    const note = document.getElementById("agentStatusNote");
    const question = String(input?.value || "").trim();
    if (!question) return;
    if (input) input.value = "";
    if (sendButton) {
        sendButton.disabled = true;
        sendButton.textContent = "分析中…";
    }
    appendAgentMessage("user", question);
    agentChatHistory.push({ role: "user", content: question });
    const pending = appendAgentMessage("assistant", "正在读取本地证据并生成回答…");
    try {
        const payload = await postJson("/api/agent/chat", {
            question,
            skill: skillSelect?.value || null,
            history: agentChatHistory.slice(0, -1),
            context: buildAgentClientContext(),
            network: Boolean(networkToggle?.checked),
        });
        if (pending) pending.remove();
        const grounding = payload.grounding || {};
        const flags = [];
        if (payload.llm_used === false) flags.push(safeText(payload.status || "未调用 LLM"));
        if (payload.policy_guard_status && !["passed", "not_checked"].includes(payload.policy_guard_status)) {
            flags.push("已过滤建议类表达");
        }
        if (grounding && grounding.ok === false) flags.push("部分内容缺少本地证据支撑");
        const replyText = safeText(payload.reply || "没有返回内容。");
        const decision = payload.paper_decision && payload.paper_decision.available ? payload.paper_decision : null;
        const replyHtml = `${escapeHtml(replyText).replace(/\n/g, "<br>")}${renderAgentPaperDecisionCard(decision)}`;
        appendAgentMessage("assistant", replyHtml, flags.join("；"), { html: true });
        if (replyText.trim()) {
            lastAgentResearchOpinion = replyText;
            lastAgentPaperDecision = decision;
            renderPendingAgentCandidateAction();
        }
        if (payload.ok && payload.llm_used) {
            agentChatHistory.push({ role: "assistant", content: replyText });
        } else {
            agentChatHistory.pop();
        }
        if (note) {
            if (payload.llm_used) {
                note.textContent = "Agent 已调用外部 API，并基于本地证据返回分析。";
            } else if (payload.local_analysis) {
                note.textContent = "Agent 已返回本地证据分析；未调用外部 API。";
            } else {
                note.textContent = "Agent 未调用 LLM；请确认已配置并勾选 API 调用。";
            }
        }
    } catch (error) {
        if (pending) pending.remove();
        appendAgentMessage("assistant", `请求失败：${escapeHtml(errorText(error))}`);
        agentChatHistory.pop();
        lastAgentPaperDecision = null;
        if (note) note.textContent = `Agent 请求失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (sendButton) {
            sendButton.disabled = false;
            sendButton.textContent = "发送";
        }
        if (input) input.focus();
    }
}

async function addDiscussedCandidateToPaperPool() {
    const button = document.getElementById("addDiscussedCandidateToPaperButton");
    const note = document.getElementById("agentStatusNote");
    if (!pendingAgentCandidate) {
        if (note) note.textContent = "当前没有从详情页带入的讨论标的。";
        return;
    }
    if (!lastAgentResearchOpinion.trim()) {
        if (note) note.textContent = "请先让研究助手生成本次研究意见，再确认加入纸面模拟池。";
        return;
    }
    const decision = normalizeAgentPaperDecision(lastAgentPaperDecision);
    const lifecycleAction = decision.recommended_lifecycle_action;
    if (lifecycleAction === "observe") {
        if (note) note.textContent = "LLM 结论是仅观察讨论；没有写入纸面模拟池。你也可以继续追问或从详情页手动调整。";
        return;
    }
    const original = button ? button.textContent : "";
    if (button) {
        button.disabled = true;
        button.textContent = "写入确认…";
    }
    try {
        let payload;
        if (lifecycleAction === "add_to_paper") {
            payload = await postJson("/api/paper-simulation/add-candidate", {
                symbol: pendingAgentCandidate.symbol,
                entry_date: pendingAgentCandidate.entry_date || "",
                lane: pendingAgentCandidate.lane || "",
                note: `从 LLM 研究室讨论后，用户确认：${safeText(decision.decision_status)}。${safeText(decision.review_sentence)}`,
                llm_decision_status: safeText(decision.decision_status || "user_confirmed_after_llm"),
                llm_research_opinion: lastAgentResearchOpinion,
                llm_primary_action: safeText(decision.primary_action || ""),
                llm_confidence: safeText(decision.confidence || ""),
                llm_validation_days: decision.validation_days || decision.validation_days_raw || "",
                llm_success_condition: safeText(decision.success_condition || ""),
                llm_failure_condition: safeText(decision.failure_condition || ""),
                llm_missing_data: safeText(decision.missing_data || ""),
                llm_review_sentence: safeText(decision.review_sentence || ""),
            });
        } else {
            const actionMap = { review: "review", continue: "continue", archive: "archive" };
            payload = await postJson("/api/paper-simulation/action", {
                symbol: pendingAgentCandidate.symbol,
                action: actionMap[lifecycleAction] || "continue",
                note: `LLM 研究室意见：${safeText(decision.review_sentence || decision.decision_status)}。用户确认执行纸面研究动作。`,
            });
        }
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message || "已写入纸面模拟池。");
        await Promise.all([loadPaperWatchlist(true), loadCandidates(), loadStrategyScorecard()]);
        renderPendingAgentCandidateAction();
    } catch (error) {
        if (note) note.textContent = `写入纸面模拟池失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = original || "带本次意见加入纸面模拟池";
        }
        renderPendingAgentCandidateAction();
    }
}

function setupCandidateUpdate() {
    const updateButton = document.getElementById("updateCandidatesButton");
    const generateButton = document.getElementById("generateRecommendationsButton");
    const globalUpdateButton = document.getElementById("globalUpdateButton");
    const loopRefreshButton = document.getElementById("refreshResearchLoopButton");
    const executeStrategyButton = document.getElementById("executeStrategyButton");
    if (updateButton) updateButton.addEventListener("click", updateCurrentCandidates);
    if (generateButton) generateButton.addEventListener("click", generateDailyRecommendations);
    if (globalUpdateButton) globalUpdateButton.addEventListener("click", runGlobalDataUpdate);
    if (loopRefreshButton) loopRefreshButton.addEventListener("click", () => refreshResearchLoop(true));
    if (executeStrategyButton) executeStrategyButton.addEventListener("click", handleExecuteStrategy);
    document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-research-loop-refresh]") : null;
        if (target) {
            event.preventDefault();
            refreshResearchLoop(true);
        }
    });
}

function setupSectorRefresh() {
    const button = document.getElementById("refreshSectorButton");
    if (!button) return;
    button.addEventListener("click", refreshSectorBoard);
}

function setupRealtimeObservation() {
    const refreshButton = document.getElementById("refreshRealtimeButton");
    if (refreshButton) {
        refreshButton.addEventListener("click", () => loadRealtimeMarket(true));
    }
    const searchButton = document.getElementById("stockSearchButton");
    const input = document.getElementById("stockSearchInput");
    if (searchButton) {
        searchButton.addEventListener("click", () => searchRealtimeStock());
    }
    if (input) {
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") searchRealtimeStock();
        });
    }
}

function setupLimitUpReview() {
    const button = document.getElementById("refreshLimitReviewButton");
    if (button) {
        button.addEventListener("click", () => loadLimitUpReview(true));
    }
}

function setupThemeMapTools() {
    const statusButton = document.getElementById("refreshThemeMapStatusButton");
    const localButton = document.getElementById("rebuildThemeMapLocalButton");
    const networkButton = document.getElementById("refreshThemeMapNetworkButton");
    const draftButton = document.getElementById("generateThemeMapDraftButton");
    const importButton = document.getElementById("importThemeMapButton");
    if (statusButton) statusButton.addEventListener("click", () => loadThemeMapStatus());
    if (localButton) localButton.addEventListener("click", () => refreshThemeMap("local"));
    if (networkButton) networkButton.addEventListener("click", () => refreshThemeMap("auto"));
    if (draftButton) draftButton.addEventListener("click", generateThemeMapDraft);
    if (importButton) importButton.addEventListener("click", importThemeMapDraft);
}

// ============================================================
// Data ops / Performance / Backtest modules — wired to quantdesk engines.
// ============================================================

function pctText(value, digits = 1) {
    return sanePercent(value, { digits }).text;
}

function numText(value, digits = 2) {
    if (value === null || value === undefined || value === "") return "数据缺失";
    const num = Number(value);
    return Number.isFinite(num) ? num.toFixed(digits) : safeText(value);
}

function moneyText(value, digits = 2) {
    const num = Number(value);
    if (!Number.isFinite(num)) return safeText(value);
    return `${num.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })} 元`;
}

function paperWatchlistStatusClass(value) {
    const text = safeText(value).toLowerCase();
    if (text.includes("trigger") || text.includes("review") || text.includes("warn")) return "health-warn";
    if (text.includes("failed") || text.includes("exit")) return "health-bad";
    return "health-ok";
}

function renderPaperConditionList(title, values, fallback) {
    const items = Array.isArray(values) ? values.map((item) => safeText(item)).filter((item) => item !== "数据缺失") : [];
    const content = items.length
        ? `<ul>${items.slice(0, 3).map((item) => `<li>${highlightText(item)}</li>`).join("")}</ul>`
        : `<p>${highlightText(fallback)}</p>`;
    return `
        <div class="paper-condition-list">
            <span>${escapeHtml(title)}</span>
            ${content}
        </div>
    `;
}

// ---- 数据维护 / 新鲜度看板 ----
let dataOpsAutoTimer = 0;

function setupDataHealthHome() {
    const button = document.getElementById("refreshDataHealthHomeButton");
    if (button) button.addEventListener("click", () => loadDataHealthHome());
}

function setupForwardPitHome() {
    const button = document.getElementById("refreshForwardPitHomeButton");
    if (button) button.addEventListener("click", () => loadForwardPitHome());
}

function setupDataOps() {
    const refresh = document.getElementById("refreshDataHealthButton");
    const updAll = document.getElementById("updateAllDataButton");
    const auto = document.getElementById("dataOpsAutoCheck");
    if (refresh) refresh.addEventListener("click", () => loadDataHealth());
    if (updAll) updAll.addEventListener("click", () => runDataOpsUpdate("all"));
    if (auto) {
        auto.addEventListener("change", () => {
            if (dataOpsAutoTimer) { window.clearInterval(dataOpsAutoTimer); dataOpsAutoTimer = 0; }
            if (auto.checked) dataOpsAutoTimer = window.setInterval(() => loadDataHealth(), 300000);
        });
    }
}

async function loadDataHealthHome() {
    const isCurrent = beginReadRender("health");
    const panel = document.getElementById("dataHealthHomePanel");
    if (!panel) return;
    panel.innerHTML = `<div class="empty-state">正在加载数据健康状态...</div>`;
    try {
        const data = await requestJson("/api/data-health/dashboard");
        if (!isCurrent()) return;
        renderDataHealthHome(data);
    } catch (error) {
        if (!isCurrent()) return;
        panel.innerHTML = missingBlock(`数据健康状态加载失败，可点击刷新重试：${escapeHtml(errorText(error))}`);
        renderHomeKpiDataHealth(null);
    }
}

function renderDataHealthHome(data) {
    try {
        renderHomeKpiDataHealth(data);
    } catch (error) {
        console.warn("[home-dashboard] data-health KPI render failed", error);
    }
    const panel = document.getElementById("dataHealthHomePanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "数据健康状态暂时不可用。"));
        return;
    }
    const cards = data.cards || {};
    const daily = cards.daily_data || {};
    const intraday = cards.intraday_data || {};
    const theme = cards.theme_map || {};
    const portfolio = cards.portfolio_review || {};
    const sourceStatus = data.source_status || {};
    const dailyChain = data.daily_data_chain || sourceStatus.daily_data_chain || {};
    const formalChain = data.formal_daily_chain_status || {};
    const minuteUniverse = data.minute_universe_coverage || {};
    const minuteTiers = minuteUniverse.tiers || {};
    const criticalTier = minuteTiers.critical || {};
    const analysisTier = minuteTiers.current_analysis_pool || {};
    const paper = sourceStatus.auto_paper_portfolio || {};
    const freshness = paper.data_freshness_gate || {};
    const latestDailyDate = latestIsoDate(
        freshness.latest_complete_daily_date_for_scope,
        freshness.local_daily_latest_date,
        daily.value,
        daily.detail
    ) || preferredDateText(daily.value, daily.detail);
    const verifiedMinutePool = intraday.verified_minute_pool || {};
    const verifiedMinutePoolUsable = numericValue(verifiedMinutePool.usable_count);
    const verifiedMinutePoolTotal = numericValue(verifiedMinutePool.total_symbols);
    const verifiedMinutePoolStatus = safeText(verifiedMinutePool.status || "missing");
    const verifiedMinutePoolMissing = numericValue(verifiedMinutePool.missing_count);
    const verifiedMinutePoolSource = safeText(verifiedMinutePool.source_snapshot || "");
    const verifiedMinutePoolLatestQuality = safeText(verifiedMinutePool.latest_quality_status || "");
    const verifiedMinutePoolSourceText = minutePoolSourceText(verifiedMinutePoolSource);
    const verifiedMinutePoolLatestText = verifiedMinutePoolLatestQuality !== "数据缺失"
        ? `最新质检状态 ${humanStatus(verifiedMinutePoolLatestQuality)}`
        : "";
    const themeUnknown = firstMatchedNumber(theme.detail, /仍有\s*(\d+)\s*只未知/);
    const paperIntradayMissing = numericValue(freshness.intraday_missing_symbol_count)
        ?? numericValue(paper.conditional_review?.missing_intraday_count)
        ?? numericValue(paper.conditional_execution_summary?.missing_intraday_count);
    const intradayLocalMissing = numericValue(intraday.local_missing_count);
    const intradayMissing = intradayLocalMissing ?? paperIntradayMissing;
    const missingParts = [];
    if (themeUnknown !== null) missingParts.push(`题材未知 ${themeUnknown}`);
    if (intradayMissing !== null && intradayMissing > 0) {
        missingParts.push(`分钟线 ${intradayMissing}`);
    } else if (verifiedMinutePoolStatus === "passed" && verifiedMinutePoolMissing !== null && verifiedMinutePoolMissing > 0) {
        missingParts.push(`验证池未命中 ${verifiedMinutePoolMissing}`);
    }
    const missingValue = missingParts.length ? missingParts.join(" / ") : "缺口未分类或未返回，不代表完整";
    const csvCount = findNamedNumber(data, /(^|_)(csv|file)_?(count|total|num)$/i);
    const verifiedMinutePoolValue = verifiedMinutePoolStatus === "passed" && verifiedMinutePoolUsable !== null
        ? `已验证 ${verifiedMinutePoolUsable}/${verifiedMinutePoolTotal || verifiedMinutePoolUsable}`
        : humanStatus(verifiedMinutePoolStatus);
    const verifiedMinutePoolDetail = verifiedMinutePoolStatus === "passed"
        ? [
            `已完成的历史验证子池可用于相应日期的纸面回放；目标日 ${safeText(verifiedMinutePool.target_date || "—")}。`,
            [verifiedMinutePoolSourceText, verifiedMinutePoolLatestText].filter(Boolean).join("；"),
            "独立扩展历史池继续下载，未覆盖股票仍按缺分钟线降级。",
        ].filter(Boolean).join(" ")
        : "尚未发现通过质量门的已完成分钟池；缺分钟线时不会用日线假装成交。";
    const ratioLabel = (value) => {
        const ratio = numericValue(value);
        return ratio === null ? "—" : `${(ratio * 100).toFixed(1)}%`;
    };
    const chainRunId = safeText(minuteUniverse.run_id || dailyChain.run_id || "");
    const chainTargetDateRaw = minuteUniverse.target_trade_date || dailyChain.target_date || "";
    const chainTargetDate = hasValue(chainTargetDateRaw) ? safeText(chainTargetDateRaw) : "—";
    const chainStatus = safeText(minuteUniverse.status || dailyChain.status || "not_run");
    const chainIsRunning = chainStatus === "running";
    const previousTerminal = dailyChain.previous_terminal || {};
    const chainValue = hasValue(minuteUniverse.run_id || dailyChain.run_id)
        ? `${humanStatus(chainStatus)} · ${chainTargetDate}`
        : "尚未运行";
    const chainDetail = hasValue(minuteUniverse.run_id || dailyChain.run_id)
        ? (chainIsRunning
            ? [
                `run_id ${chainRunId}`,
                `目标日 ${chainTargetDate || "—"}`,
                "任务正在更新；terminal coverage 将在终态清单发布后显示，请稍候。",
                hasValue(previousTerminal.run_id)
                    ? `上一终态 ${humanStatus(previousTerminal.status || "unknown")} · ${safeText(previousTerminal.target_date || "—")} · run_id ${safeText(previousTerminal.run_id)}`
                    : "",
            ].filter(Boolean).join("；")
            : [
                `run_id ${chainRunId}`,
                `日线 ${ratioLabel(minuteUniverse.daily_coverage?.coverage_ratio)}`,
                `关键分钟 ${ratioLabel(criticalTier.coverage_ratio)}（${numericValue(criticalTier.complete_session_count) ?? 0}/${numericValue(criticalTier.requested_count) ?? "?"}）`,
                `914 分析池 ${ratioLabel(analysisTier.coverage_ratio)}（${numericValue(analysisTier.complete_session_count) ?? 0}/${numericValue(analysisTier.requested_count) ?? "?"}）`,
                `池版本 ${safeText(analysisTier.pool_version || "—")}`,
                safeText(minuteUniverse.next_action || dailyChain.next_action || ""),
            ].filter((item) => item && item !== "数据缺失").join("；"))
        : "尚无 authoritative 每日批次；下游不得把零散 latest 文件标成最新真实状态。";
    const dailyChainHomeCardHtml = dataHealthHomeCard(
        "每日数据链",
        chainValue,
        chainDetail,
        chainStatus === "completed" ? "" : "warn"
    );
    const minutePoolHomeCardHtml = dataHealthHomeCard("历史验证分钟池", verifiedMinutePoolValue, verifiedMinutePoolDetail);
    const queueStage = safeText(data.extended_data_queue_stage_progress || "—");
    const queueLiveness = safeText(data.extended_data_queue_liveness || data.extended_data_current_gate || "unknown");
    const formalScheduler = formalChain.scheduler || data.daily_data_scheduler || sourceStatus.daily_data_scheduler || {};
    const formalFreezeReasons = Array.isArray(formalChain.plan_freeze_reason_codes)
        ? formalChain.plan_freeze_reason_codes.map((item) => safeText(item)).filter(Boolean)
        : [];
    const formalRetrySymbols = Array.isArray(formalChain.missing_or_retry_symbols)
        ? formalChain.missing_or_retry_symbols.map((item) => safeText(item)).filter(Boolean)
        : [];
    const formalFreezeStatus = safeText(formalChain.plan_freeze_status || "not_run");
    const formalSchedulerEnabled = Boolean(formalScheduler.enabled);
    const formalSchedulerPending = Boolean(formalScheduler.first_run_pending);
    const formalPoolValue = [
        safeText(formalChain.pool_status || "unknown_pool"),
        formalChain.analysis_pool_complete !== undefined && formalChain.analysis_pool_requested !== undefined
            ? `${safeText(formalChain.analysis_pool_complete)}/${safeText(formalChain.analysis_pool_requested)}`
            : "",
        safeText(formalChain.target_date || ""),
    ].filter(Boolean).join(" / ");
    const formalGapDetail = formalRetrySymbols.length
        ? `missing/retry ${formalRetrySymbols.slice(0, 12).join(", ")}${formalRetrySymbols.length > 12 ? " ..." : ""}`
        : "no explicit missing/retry symbol in latest formal chain";
    const formalChainExtraCardHtml = dataHealthHomeCard(
        "Formal 914 Chain",
        formalPoolValue || humanStatus(formalChain.status || "not_run"),
        [
            formalChain.run_id ? `run_id ${safeText(formalChain.run_id)}` : "",
            formalChain.contract_version ? `contract v${safeText(formalChain.contract_version)}` : "",
            formalChain.authoritative ? "authoritative" : "not authoritative",
            `daily ${humanStatus(formalChain.daily_status || "unknown")}`,
            `critical daily ${humanStatus(formalChain.critical_daily_status || "unknown")}`,
            `critical minute ${humanStatus(formalChain.critical_minute_status || "unknown")}`,
            formalGapDetail,
        ].filter(Boolean).join("; "),
        formalChain.status === "completed" && formalChain.pool_status === "full_pool" ? "" : "warn"
    );
    const dailyFreezeHomeCardHtml = dataHealthHomeCard(
        "T+1 Freeze Gate",
        humanStatus(formalFreezeStatus),
        [
            formalChain.plan_freeze_written ? "written" : "not written",
            formalFreezeReasons.length ? `reason ${formalFreezeReasons.slice(0, 4).join(", ")}` : "",
        ].filter(Boolean).join("; ") || "not run",
        formalFreezeStatus === "completed" ? "" : "warn"
    );
    const dailySchedulerHomeCardHtml = dataHealthHomeCard(
        "Daily Auto Update",
        formalSchedulerEnabled ? "enabled" : "disabled",
        [
            `status ${humanStatus(formalScheduler.status || "unknown")}`,
            formalSchedulerPending ? "first run pending" : "",
            formalScheduler.last_task_result ? `last ${safeText(formalScheduler.last_task_result)}` : "",
            formalScheduler.next_run_time ? `next ${safeText(formalScheduler.next_run_time)}` : "",
        ].filter(Boolean).join("; "),
        formalSchedulerEnabled && !formalSchedulerPending ? "" : "warn"
    );
    const queueIsStale = String(data.extended_data_queue_liveness || "").trim().toLowerCase() === "stale";
    const queueProgressState = safeText(data.extended_data_queue_progress_state || "");
    const queueOk = numericValue(data.extended_data_queue_ok_count) ?? 0;
    const queueFail = numericValue(data.extended_data_queue_fail_count) ?? 0;
    const queueTraceback = numericValue(data.extended_data_queue_traceback_count) ?? 0;
    const queueRemaining = Array.isArray(data.extended_data_queue_stage_remaining_keys)
        ? data.extended_data_queue_stage_remaining_keys.map((item) => safeText(item)).filter(Boolean).join(", ")
        : "";
    const queueCardValue = `${humanStatus(queueLiveness)} ${queueStage}`;
    const queueCardDetail = [
        queueIsStale ? "队列日志超过 15 分钟未更新，可能已卡住；本页只做只读监控，是否重启由你决定" : "",
        queueProgressState ? `进度 ${humanStatus(queueProgressState)}` : "",
        queueRemaining ? `剩余 ${queueRemaining}` : "",
        `OK ${queueOk} / FAIL ${queueFail} / traceback ${queueTraceback}`,
    ].filter(Boolean).join("；");
    const minuteDone = numericValue(data.tushare_minute_symbol_done_count) ?? 0;
    const minuteTotal = numericValue(data.tushare_minute_symbol_count) ?? 0;
    const minuteTargetTotal = numericValue(data.tushare_minute_target_symbol_count) ?? minuteTotal;
    const minuteCurrentAvailable = numericValue(data.tushare_minute_current_available_count) ?? minuteDone;
    const minuteCurrentRatio = numericValue(data.tushare_minute_current_available_ratio);
    const minuteRatioText = minuteCurrentRatio !== null
        ? `完成约 ${(minuteCurrentRatio * 100).toFixed(1)}%`
        : "";
    const minuteCanonicalCurrent = numericValue(data.tushare_minute_canonical_current_available_count);
    const minuteStagedCurrent = numericValue(data.tushare_minute_staged_current_not_imported_count);
    const latestCompletedJobTotal = numericValue(data.tushare_minute_latest_completed_job_symbol_count);
    const latestCompletedJobCurrent = numericValue(data.tushare_minute_latest_completed_job_canonical_current_count);
    const latestCompletedJobText = latestCompletedJobTotal
        ? `Top200 固化覆盖 ${latestCompletedJobCurrent ?? 0}/${latestCompletedJobTotal}`
        : "";
    const minuteLastSymbol = safeText(data.tushare_minute_last_symbol || "—");
    const minuteStatus = safeText(data.tushare_minute_status || "unknown");
    const minuteTraceback = numericValue(data.tushare_minute_traceback_count) ?? 0;
    const minuteCoverageAction = safeText(
        data.tushare_minute_suggested_next_action
        || data.tushare_minute_coverage_suggested_next_action
        || "continue_monitoring_minute_download"
    );
    const minuteWarningDetail = minuteStatus === "running_with_warnings"
        ? `状态 ${humanStatus(minuteStatus)}；历史 traceback ${minuteTraceback}，但任务仍在 running，继续监控，不自动重启。`
        : `状态 ${humanStatus(minuteStatus)}；traceback ${minuteTraceback}。`;
    const minuteAvailabilityDetail = [
        minuteRatioText,
        `当前可用 ${minuteCurrentAvailable}/${minuteTargetTotal || minuteTotal || "?"}`,
        minuteCanonicalCurrent !== null ? `canonical ${minuteCanonicalCurrent}` : "",
        minuteStagedCurrent !== null && minuteStagedCurrent > 0 ? `staging待导入 ${minuteStagedCurrent}` : "",
        latestCompletedJobText,
    ].filter(Boolean).join("；");
    const minuteExpansionValue = minuteTotal > 0 ? `${minuteDone}/${minuteTargetTotal || minuteTotal}` : "未发现运行中的扩展池";
    const minuteExpansionDetail = minuteTotal > 0
        ? `扩展历史 1m 队列下载中；${minuteAvailabilityDetail}；last ${minuteLastSymbol}；动作 ${humanStatus(minuteCoverageAction)}。${minuteWarningDetail} 该队列不作为当天 914 分析池质量门。`
        : "未发现扩展历史分钟下载；若需要扩大历史验证范围，请先运行只读 minute status/coverage 检查。";
    const importGate = safeText(data.extended_import_preapply_gate || data.extended_import_gate || data.extended_data_current_gate || "unknown");
    const waitReason = safeText(data.extended_data_operator_wait_reason || data.extended_data_gate_suggested_next_action || data.suggested_next_action || "—");
    const importCardValue = importGate === "wait_for_download_completion"
        ? "等待下载完成"
        : humanStatus(importGate);
    const importCardDetail = `当前门禁 ${humanStatus(importGate)}；原因 ${humanStatus(waitReason)}；扩展历史分钟池 ${minuteDone}/${minuteTotal || "?"}。未完成前只做只读监控，不导入 staged 数据。`;
    const stagedDomains = data.extended_import_domain_counts && typeof data.extended_import_domain_counts === "object"
        ? data.extended_import_domain_counts
        : {};
    const stagedDomainText = Object.entries(stagedDomains)
        .filter(([, value]) => numericValue(value) !== null)
        .map(([key, value]) => `${safeText(key)} ${numericValue(value)}`)
        .slice(0, 4)
        .join(" / ");
    const stagedLimited = data.extended_import_source_scan_limit_applied === true;
    const stagedLimitedDomains = Array.isArray(data.extended_import_source_scan_limited_domains)
        ? data.extended_import_source_scan_limited_domains.map((item) => safeText(item)).filter(Boolean)
        : [];
    const stagedMaxFiles = numericValue(data.extended_import_max_files_per_rule);
    const stagedFullAuditCommand = String(
        data.extended_import_full_audit_command
        || "python scripts/qd.py audit-extended-import --summary true --max-files-per-rule 0"
    );
    const stagedCardValue = stagedDomainText || "暂无暂存计数";
    const stagedCardDetail = stagedLimited
        ? `当前是 bounded 暂存计数${stagedMaxFiles ? `（每规则最多扫描 ${stagedMaxFiles} 个文件）` : ""}，不代表完整审计；受限领域 ${stagedLimitedDomains.slice(0, 5).join(", ") || "—"}。完整审计命令（等队列结束后运行）：${stagedFullAuditCommand}`
        : "暂存计数未受抽样限制；正式导入前仍需只读 audit 并等待确认。";
    const safeNextCommands = Array.isArray(data.extended_data_safe_next_commands)
        ? data.extended_data_safe_next_commands.map((item) => safeText(item)).filter(Boolean)
        : [];
    const blockedCommands = Array.isArray(data.extended_data_blocked_without_confirmation)
        ? data.extended_data_blocked_without_confirmation.map((item) => safeText(item)).filter(Boolean)
        : [];
    const manualReviewRequired = data.extended_data_manual_review_required === true;
    const manualReviewReason = String(data.extended_data_manual_review_reason || "").trim();
    const requiresUserConfirmation = data.extended_data_requires_user_confirmation === true;
    const confirmationRequiredCommand = String(data.extended_data_confirmation_required_command || "").trim();
    const safeNextValue = safeNextCommands[0] || "等待 data-gates 状态";
    const safeNextParts = [];
    if (manualReviewRequired) {
        safeNextParts.push(`需人工复核：${humanStatus(manualReviewReason || "manual_review")}`);
    }
    if (requiresUserConfirmation && confirmationRequiredCommand) {
        safeNextParts.push(`需你明确确认后才可执行：${confirmationRequiredCommand}`);
    }
    if (safeNextCommands.length > 1) {
        safeNextParts.push(`其余安全命令 ${safeNextCommands.length - 1} 条：${safeNextCommands.slice(1).join("；")}`);
    }
    if (blockedCommands.length) {
        safeNextParts.push(`禁止未确认执行（共 ${blockedCommands.length} 条）：${blockedCommands.slice(0, 2).join("；")}${blockedCommands.length > 2 ? " 等" : ""}`);
    }
    if (!safeNextParts.length) {
        safeNextParts.push("当前没有额外禁止命令；仍需遵守 research-only / no broker / no canonical writes");
    }
    const operatorStageDetail = "允许阶段: monitor -> audit -> dry-run -> apply；当前 gate 决定可进入哪一步，apply 必须人工确认";
    const safeNextDetail = `${operatorStageDetail}。${safeNextParts.join("。")}。`;
    const gatesSnapshotGeneratedAt = safeText(data.extended_data_gates_snapshot_generated_at || "");
    const gatesSnapshotPath = safeText(data.extended_data_gates_snapshot_path || "results/data_health/extended_data_gates_latest.json");
    const gatesSnapshotStatus = safeText(data.extended_data_gates_snapshot_status || data.extended_data_current_gate || "unknown");
    const gatesSnapshotExists = data.extended_data_gates_snapshot_exists !== false;
    const gatesSnapshotAgeSeconds = numericValue(data.extended_data_gates_snapshot_age_seconds);
    const gatesSnapshotAgeText = humanAgeText(gatesSnapshotAgeSeconds);
    const gatesSnapshotStale = gatesSnapshotAgeSeconds !== null && gatesSnapshotAgeSeconds > 3600;
    const gatesSnapshotValue = gatesSnapshotExists
        ? `${gatesSnapshotGeneratedAt || "已读取 latest"}${gatesSnapshotAgeText ? `（${gatesSnapshotAgeText}前）` : ""}`
        : "尚未生成";
    let gatesSnapshotDetail;
    if (!gatesSnapshotExists) {
        gatesSnapshotDetail = `未找到 ${gatesSnapshotPath}；请先只读运行 python scripts/qd.py data-gates --summary true。`;
    } else if (gatesSnapshotStale) {
        gatesSnapshotDetail = `快照已 ${gatesSnapshotAgeText} 未刷新，可能不代表当前实时状态；请先只读运行 python scripts/qd.py data-gates --summary true。来源 ${gatesSnapshotPath}；状态 ${humanStatus(gatesSnapshotStatus)}。`;
    } else {
        gatesSnapshotDetail = `来源 ${gatesSnapshotPath}；状态 ${humanStatus(gatesSnapshotStatus)}。${gatesSnapshotAgeText ? `快照生成于 ${gatesSnapshotAgeText}前。` : "若时间明显滞后，请先只读运行 data-gates。"}`;
    }
    const HEALTH_LEVEL_LABELS = { green: "良好", yellow: "注意", red: "异常" };
    const healthLevelKey = String(data.health_level || "yellow").trim().toLowerCase();
    const healthLevelLabel = HEALTH_LEVEL_LABELS[healthLevelKey] || safeText(data.health_level || "yellow");
    const healthLevelTone = healthLevelKey === "green" ? "" : "warn";
    const minuteTone = /error|stale|traceback/.test(minuteStatus) ? "warn" : "";
    const capabilitySummary = data.data_capability_summary || {};
    const availableNow = Array.isArray(capabilitySummary.available_now) ? capabilitySummary.available_now : [];
    const waitingOn = Array.isArray(capabilitySummary.waiting_on) ? capabilitySummary.waiting_on : [];
    const forbiddenNow = Array.isArray(capabilitySummary.currently_forbidden) ? capabilitySummary.currently_forbidden : [];
    panel.innerHTML = [
        dataHealthHomeCard("数据等级", healthLevelLabel, data.summary_text, healthLevelTone),
        dataHealthHomeCard("最新日期", latestDailyDate || "数据缺失", [daily.scope_label || "范围未声明", `状态快照 ${daily.snapshot_generated_at || "未提供"}`, daily.detail || freshness.message].filter(Boolean).join("；")),
        dataHealthHomeCard(
            "现在可用",
            capabilityLabels(availableNow) || "等待状态快照",
            capabilityDetails(availableNow, "数据健康接口尚未返回 data_capability_summary；先按只读监控和现有门禁执行。")
        ),
        dataHealthHomeCard(
            "等待下载",
            capabilityLabels(waitingOn) || "暂无等待项",
            capabilityDetails(waitingOn, "如果队列已完成，下一步仍是只读 audit，再 dry-run，最后等你确认。")
        ),
        dataHealthHomeCard(
            "当前禁止",
            capabilityLabels(forbiddenNow) || "遵守研究边界",
            capabilityDetails(forbiddenNow, "不写正式市场数据、不自动 apply、不连接券商、不真实下单。"),
            "warn"
        ),
        dataHealthHomeCard("缺失/未知", missingValue, [theme.scope_label || "题材范围未声明", intraday.scope_label || "分钟范围未声明", `分钟状态快照 ${intraday.snapshot_generated_at || "未提供"}`, theme.detail, intraday.detail].filter(Boolean).join("；")),
        dailyChainHomeCardHtml,
        formalChainExtraCardHtml,
        dailyFreezeHomeCardHtml,
        dailySchedulerHomeCardHtml,
        minutePoolHomeCardHtml,
        dataHealthHomeCard("历史分钟扩展池", minuteExpansionValue, minuteExpansionDetail, minuteTone),
        dataHealthHomeCard("下载队列", queueCardValue, queueCardDetail, queueIsStale ? "warn" : ""),
        dataHealthHomeCard("导入门禁", importCardValue, importCardDetail),
        dataHealthHomeCard("暂存扩展数据", stagedCardValue, stagedCardDetail),
        dataHealthHomeCard("安全下一步", safeNextValue, safeNextDetail),
        dataHealthHomeCard("门禁快照", gatesSnapshotValue, gatesSnapshotDetail, gatesSnapshotStale ? "warn" : ""),
        dataHealthHomeCard(
            "CSV 数量",
            csvCount === null ? "接口未返回" : `${csvCount}`,
            csvCount === null ? "当前数据健康接口未提供本地 CSV 文件总数；详情请看设置页完整数据健康。" : "来自数据健康接口的本地文件数量字段。"
        ),
    ].join("");
    attachHealthDetails(panel, data);
}

function capabilityLabels(items) {
    return items
        .map((item) => safeText(item && item.label))
        .filter((label) => label && label !== "数据缺失")
        .slice(0, 4)
        .join(" / ");
}

function capabilityDetails(items, fallback) {
    const details = items
        .map((item) => {
            const label = safeText(item && item.label);
            const detail = safeText(item && item.detail);
            if (!label || label === "数据缺失") return "";
            return detail && detail !== "数据缺失" ? `${label}：${detail}` : label;
        })
        .filter(Boolean)
        .slice(0, 4);
    return details.length ? details.join("；") : fallback;
}

function dataHealthHomeCard(label, value, detail, tone) {
    const toneClass = tone === "warn" ? " card-tone-warn" : "";
    return `
        <article class="data-health-home-card${toneClass}">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(safeText(value))}</strong>
            <p class="paper-desk-note">${escapeHtml(safeText(detail))}</p>
        </article>
    `;
}

function firstMatchedNumber(value, pattern) {
    if (!hasValue(value)) return null;
    const match = String(value).match(pattern);
    if (!match) return null;
    const number = Number(match[1]);
    return Number.isFinite(number) ? number : null;
}

async function loadForwardPitHome() {
    const panel = document.getElementById("forwardPitHomePanel");
    if (!panel) return;
    panel.innerHTML = `<div class="empty-state">正在加载 Forward PIT 状态...</div>`;
    try {
        const data = await requestJson("/api/forward-pit/readiness");
        renderForwardPitHome(data);
    } catch (error) {
        panel.innerHTML = missingBlock(`Forward PIT 状态加载失败：${escapeHtml(errorText(error))}`);
    }
}

function renderForwardPitHome(data) {
    const panel = document.getElementById("forwardPitHomePanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "Forward PIT 状态暂时不可用。"));
        feedDutyLamp("t0", "缺失", "readiness 工件不可读", "bad");
        return;
    }
    // The API presents an absent T0 as the string "数据缺失", so truthiness of
    // data.t0 cannot mean "born" — only a real ISO date does.
    const t0Born = typeof data.t0 === "string" && /^\d{4}-\d{2}-\d{2}$/.test(data.t0.trim());
    // Display layer speaks Chinese: the API keeps "unknown" as its contract
    // value, but an unborn T0 must never render as a bare English "unknown".
    const t0Display = t0Born ? safeText(data.t0.trim()) : "未诞生";
    const t0Tone = t0Born ? "" : "warn";
    const forwardDays = numericValue(data.current_eligible_forward_days) ?? 0;
    const statusText = humanStatus(safeText(data.status || "unknown"));
    const archive = data.latest_sector_archive || {};
    const archiveStatus = safeText(archive.status || "unknown");
    const archiveWritten = archive.written === true;
    const archiveReasonCodes = Array.isArray(archive.reason_codes) ? archive.reason_codes.filter(Boolean) : [];
    const archiveDegraded = archive.degraded === true || archiveStatus === "degraded" || archive.written === false;
    const coverageScope = data.coverage_scope || {};
    const sectorScope = coverageScope.sector_archive || {};
    const analysisPoolScope = coverageScope.current_analysis_pool || {};
    const scopeNumber = (value) => hasValue(value) ? numericValue(value) : null;
    const scopeCount = (value) => {
        const count = scopeNumber(value);
        return count === null ? "缺失" : String(count);
    };
    const poolSymbolCount = scopeNumber(analysisPoolScope.symbol_count);
    const alignmentRatio = scopeNumber(sectorScope.membership_board_alignment_ratio);
    const alignmentRatioText = alignmentRatio === null ? "缺失" : `${(alignmentRatio * 100).toFixed(1)}%`;
    const archiveValue = archiveWritten
        ? "已归档 written=true"
        : (archiveStatus === "unknown" ? "尚无当日归档" : `${humanStatus(archiveStatus)}`);
    const analysisRateDistinction = poolSymbolCount === null
        ? "不是当前分析池通过率"
        : `不是当前分析池通过率（${poolSymbolCount} 只）`;
    const archiveDetailParts = [
        `当前 provider 截面；${analysisRateDistinction}、不是全 A、不是历史 PIT`,
        `板块 ${scopeCount(sectorScope.sector_count)} / 成员输入 ${scopeCount(sectorScope.membership_input_count)} / 成员 ${scopeCount(sectorScope.membership_count)} / 对齐 ${scopeCount(sectorScope.aligned_membership_count)} / 未知 ${scopeCount(sectorScope.membership_unknown_count)} / 板块对齐率 ${alignmentRatioText}`,
    ];
    if (archive.target_date) archiveDetailParts.push(`目标日 ${safeText(archive.target_date)}`);
    if (archiveDegraded && archiveReasonCodes.length) {
        archiveDetailParts.push(`原因码：${archiveReasonCodes.map(safeText).join(" / ")}`);
    } else if (archiveDegraded) {
        archiveDetailParts.push("降级（无具体原因码）");
    }
    const archiveDetail = archiveDetailParts.join("；");
    const archiveTone = archiveWritten && coverageScope.degraded !== true ? "" : "warn";
    const analysisPoolValue = poolSymbolCount === null ? "规模缺失" : `${poolSymbolCount} 只`;
    const analysisPoolMeta = [];
    if (analysisPoolScope.target_date) analysisPoolMeta.push(`目标日 ${safeText(analysisPoolScope.target_date)}`);
    if (analysisPoolScope.version) analysisPoolMeta.push(`版本 ${safeText(analysisPoolScope.version)}`);
    const analysisPoolDetail = [
        "正式日线分析池；与板块归档属于不同 population，禁止交叉相除。",
        analysisPoolMeta.length ? analysisPoolMeta.join("；") : "正式链 scope 缺失或不可读。",
    ].join(" ");

    const milestones = Array.isArray(data.milestones) ? data.milestones : [];
    const milestoneCards = milestones.map((m) => {
        const label = safeText(m.milestone || m.relative_to_t0 || "里程碑");
        const cal = m.calendar_date ? safeText(m.calendar_date) : "待锚定";
        const detail = safeText(m.purpose || m.status || "");
        const tone = m.calendar_date ? "" : "warn";
        return dataHealthHomeCard(label, cal, detail, tone);
    }).join("");

    const cards = [
        dataHealthHomeCard("T0 日期", t0Display, t0Born ? "首个通过全部资格门的归档交易日。" : "T0 尚未诞生：需计划任务自然运行且当日板块归档 written=true。", t0Tone),
        dataHealthHomeCard("有效 forward 天数", String(forwardDays), `连续合格归档交易日，不是股票覆盖数量；readiness 状态：${statusText}`, forwardDays > 0 ? "" : "warn"),
        dataHealthHomeCard("板块归档口径", archiveValue, archiveDetail, archiveTone),
        dataHealthHomeCard("当前分析池口径", analysisPoolValue, analysisPoolDetail, coverageScope.degraded === true ? "warn" : ""),
    ].join("");

    panel.innerHTML = cards + milestoneCards;
    feedDutyLamp(
        "t0",
        t0Born ? t0Display : "未诞生",
        t0Born ? `forward ${forwardDays} 天` : "等自然链归档 written=true",
        t0Born ? "" : "warn"
    );
}

function findNamedNumber(value, keyPattern, depth = 0) {
    if (!value || typeof value !== "object" || depth > 5) return null;
    if (Array.isArray(value)) {
        for (const item of value) {
            const found = findNamedNumber(item, keyPattern, depth + 1);
            if (found !== null) return found;
        }
        return null;
    }
    for (const [key, item] of Object.entries(value)) {
        if (keyPattern.test(key)) {
            const number = numericValue(item);
            if (number !== null) return number;
        }
        if (item && typeof item === "object") {
            const found = findNamedNumber(item, keyPattern, depth + 1);
            if (found !== null) return found;
        }
    }
    return null;
}

async function loadDataHealth() {
    const isCurrent = beginReadRender("health");
    const note = document.getElementById("dataOpsNote");
    const panel = document.getElementById("dataHealthPanel");
    if (note) note.textContent = "正在读取数据健康状态…";
    try {
        const data = await requestJson("/api/data-health/dashboard");
        if (!isCurrent()) return;
        renderDataHealth(data);
        renderDataHealthHome(data);
        await renderSettingsEnvironment(data);
    } catch (error) {
        if (!isCurrent()) return;
        if (panel) panel.innerHTML = missingBlock(`数据健康看板加载失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "数据健康看板加载失败。";
    }
}

function renderDataHealth(data) {
    const note = document.getElementById("dataOpsNote");
    const panel = document.getElementById("dataHealthPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "数据健康看板暂时不可用。"));
        return;
    }
    const levelMap = { green: ["health-ok", "良好"], yellow: ["health-warn", "注意"], red: ["health-bad", "需补数据"] };
    const cards = data.cards || {};
    const cardHtml = Object.keys(cards).map((key) => {
        const card = cards[key] || {};
        const [cls, label] = levelMap[safeText(card.status)] || ["health-warn", safeText(card.status)];
        return `
            <div class="health-card">
                <h4>${escapeHtml(safeText(card.title || key))}<span class="health-badge ${cls}">${escapeHtml(label)}</span></h4>
                <div class="info-value">${escapeHtml(safeText(card.value))}</div>
                <p class="section-note">范围 ${escapeHtml(card.scope_label || "未声明")} · 状态快照 ${escapeHtml(card.snapshot_generated_at || "未提供")}；记录日期与当前目标日分别核对。</p>
                <p>${escapeHtml(safeText(card.detail))}</p>
                <p style="color:var(--text-muted)">下一步：${escapeHtml(safeText(card.next_action))}</p>
            </div>`;
    }).join("");
    const steps = Array.isArray(data.beginner_next_steps) ? data.beginner_next_steps : [];
    const [topCls, topLabel] = levelMap[safeText(data.health_level)] || ["health-warn", safeText(data.health_level)];
    // The dashboard payload dropped the legacy top-level `cards` schema; when it
    // is absent, synthesize a core-summary grid from fields the new schema does
    // provide instead of showing an empty panel.
    const chainInfo = data.daily_data_chain || {};
    const summaryInfo = data.data_health_summary || {};
    const gradeTrend = data.data_grade_trend || {};
    const fallbackCards = [
        { title: "每日数据链", value: `${humanStatus(safeText(chainInfo.status || summaryInfo.daily_data_chain_status || "missing"))} · ${safeText(chainInfo.target_date || "—")}`, detail: `run_id ${safeText(chainInfo.run_id || summaryInfo.daily_data_chain_run_id || "—")}`, next: "链每交易日 15:45/18:30 自动运行；周末无排班。" },
        { title: "分析池规模", value: `${safeText(data.analysis_pool_count ?? "missing")} 只`, detail: "正式日线分析池（学习名册口径），非全市场。", next: "名册变更须走显式闸门。" },
        { title: "扩展数据闸", value: humanStatus(safeText(summaryInfo.extended_data_current_gate || "missing")), detail: safeText(summaryInfo.extended_data_gate_suggested_next_action || "—"), next: safeText(summaryInfo.extended_data_operator_wait_reason || "按建议动作执行。") },
        { title: "数据等级趋势", value: safeText(gradeTrend.latest || gradeTrend.status || gradeTrend.summary || "见首页明细"), detail: "完整逐项明细见首页「数据健康」卡片（同一数据源）。", next: "本页为核心摘要视图。" },
    ];
    const fallbackHtml = fallbackCards.map((card) => `
            <div class="health-card">
                <h4>${escapeHtml(card.title)}</h4>
                <div class="info-value">${escapeHtml(card.value)}</div>
                <p>${escapeHtml(card.detail)}</p>
                <p style="color:var(--text-muted)">下一步：${escapeHtml(card.next)}</p>
            </div>`).join("");
    const scopeHealth = data.scope_health || {};
    const scopeRows = scopeHealth.scopes || {};
    const scopeCards = Object.values(scopeRows).map((scope) => {
        const status = safeText(scope && scope.status).toLowerCase();
        const tone = status === "ok" ? "health-ok" : (status === "blocked" ? "health-bad" : "health-warn");
        const label = safeText(scope && scope.label || scope && scope.scope || "数据范围");
        const total = Number(scope && scope.total_symbols || 0);
        const covered = Number(scope && scope.covered_symbols || 0);
        const stale = Number(scope && scope.stale_symbols || 0);
        const missing = Number(scope && scope.missing_symbols || 0);
        const asOf = safeText(scope && scope.as_of || "未知");
        const target = safeText(scope && scope.target_date || "未知");
        const detail = safeText(scope && scope.detail || "该范围暂无可解释的健康细节。");
        return `
            <div class="health-card">
                <h4>${escapeHtml(label)}<span class="health-badge ${tone}">${escapeHtml(status || "unknown")}</span></h4>
                <div class="info-value">${escapeHtml(`${covered}/${total}`)}</div>
                <p>${escapeHtml(`观测日期：${asOf}；目标日期：${target}；滞后 ${stale}；缺失 ${missing}`)}</p>
                <p style="color:var(--text-muted)">${escapeHtml(detail)}</p>
            </div>`;
    }).join("");
    const scopeSummaryHtml = `
        <div class="quant-summary-head">
            <strong>范围化数据状态</strong>
            <div class="section-note">全量当前名册、当前研究池、AI 纸面范围与分钟/调度分别显示，不使用一个“最新日期”代替全部范围。</div>
        </div>
        ${scopeCards ? `<div class="health-grid">${scopeCards}</div>` : missingBlock("范围化数据状态尚未生成；当前不会把旧快照伪装成完整覆盖。")}`;
    const dataAlerts = data.data_alerts || {};
    const currentAlerts = Array.isArray(dataAlerts.current) ? dataAlerts.current : [];
    const alertCounts = dataAlerts.counts || {};
    const criticalAlertCount = Number(alertCounts.critical || 0);
    const alertTone = criticalAlertCount > 0 ? "health-bad" : (currentAlerts.length ? "health-warn" : "health-ok");
    const alertLabel = criticalAlertCount > 0 ? "需处理" : (currentAlerts.length ? "注意" : "暂无");
    const alertCards = currentAlerts.slice(0, 6).map((alert) => {
        const severity = safeText(alert && alert.severity).toLowerCase();
        const tone = severity === "critical" ? "health-bad" : (severity === "warning" ? "health-warn" : "health-ok");
        const scope = safeText(alert && alert.scope || "数据范围");
        const reason = safeText(alert && alert.reason_code || "状态异常");
        const detail = safeText(alert && alert.detail || "数据健康出现需要核对的状态。");
        const asOf = safeText(alert && alert.as_of || "日期未知");
        return `
            <div class="health-card">
                <h4>${escapeHtml(scope)}<span class="health-badge ${tone}">${escapeHtml(severity || "warning")}</span></h4>
                <div class="info-value">${escapeHtml(reason)}</div>
                <p>${escapeHtml(detail)}</p>
                <p style="color:var(--text-muted)">观测日期：${escapeHtml(asOf)}</p>
            </div>`;
    }).join("");
    const alertDelivery = dataAlerts.delivery || {};
    const alertDeliveryNote = alertDelivery.external_push === false
        ? "仅本地告警，未发送外部通知。"
        : "告警投递状态未知；请以本地数据健康状态为准。";
    const alertSummaryHtml = `
        <div class="quant-summary-head">
            <strong>数据告警 <span class="health-badge ${alertTone}">${escapeHtml(alertLabel)}</span></strong>
            <div class="section-note">${escapeHtml(alertDeliveryNote)}</div>
        </div>
        ${alertCards ? `<div class="health-grid">${alertCards}</div>` : ""}`;
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>数据健康总览 <span class="health-badge ${topCls}">${escapeHtml(topLabel)}</span></strong>
            <div class="section-note">${escapeHtml(safeText(data.summary_text || "后端已启用新版数据健康结构；以下为核心摘要，完整明细见首页「数据健康」卡片。"))}</div>
        </div>
        ${scopeSummaryHtml}
        ${alertSummaryHtml}
        <div class="health-grid">${cardHtml || fallbackHtml || missingBlock("暂无数据卡片。")}</div>
        ${steps.length ? `<ol class="next-steps">${steps.map((s) => `<li>${escapeHtml(safeText(s))}</li>`).join("")}</ol>` : ""}
        <p class="panel-disclaimer">${escapeHtml(safeText(data.message || "只读聚合；未联网、未更新数据、未改变虚拟组合。"))}</p>`;
    attachHealthDetails(panel, data);
    if (note) note.textContent = `数据健康：${topLabel} · ${safeText(data.generated_at || "")}`;
}

function attachHealthDetails(panel, data) {
    if (!data.summary_only || !data.details_url || !panel.insertAdjacentHTML) return;
    panel.insertAdjacentHTML("beforeend", `
        <details class="health-card" data-health-details>
            <summary>查看完整证据与缺口明细</summary>
            <p class="section-note">当前展示摘要；完整记录可逐页查看。记录更新后请重新刷新。</p>
            <div data-health-detail-body></div>
        </details>`);
    const disclosure = panel.querySelector("[data-health-details]");
    const body = disclosure.querySelector("[data-health-detail-body]");
    let sequence = 0;
    async function readPage(pointer = "", offset = 0) {
        const token = ++sequence;
        body.innerHTML = missingBlock("正在读取完整证据…");
        const url = new URL(data.details_url, window.location?.href || "http://127.0.0.1:8000/");
        url.searchParams.set("pointer", pointer);
        url.searchParams.set("offset", String(offset));
        url.searchParams.set("limit", "100");
        try {
            const page = await requestJson(url.pathname + url.search);
            if (token !== sequence || !panel.contains(disclosure)) return;
            const items = Array.isArray(page.items) ? page.items : [];
            const pagination = page.pagination || {};
            const rows = items.map((item, index) => item.deferred
                ? `<p>${escapeHtml(String(item.key))} · ${escapeHtml(String(item.total))} 项 <button class="secondary-button" type="button" data-detail-item="${index}">展开</button></p>`
                : `<p>${escapeHtml(String(item.key))}：${escapeHtml(JSON.stringify(item.value))}</p>`).join("");
            const text = typeof page.items === "string" ? page.items
                : page.kind === "scalar" ? JSON.stringify(page.items) : "";
            body.innerHTML = `<p>${escapeHtml(pointer || "完整证据")} · ${offset + (pagination.total ? 1 : 0)}–${Math.min(offset + 100, pagination.total || 0)} / ${pagination.total || 0}</p>
                ${pointer ? '<button class="secondary-button" type="button" data-detail-back>返回上级</button>' : ""}
                ${rows}${text ? `<pre style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(text)}</pre>` : ""}
                ${offset > 0 ? '<button class="secondary-button" type="button" data-detail-prev>上一页</button>' : ""}
                ${pagination.next_offset !== null && pagination.next_offset !== undefined ? '<button class="secondary-button" type="button" data-detail-next>下一页</button>' : ""}`;
            body.querySelectorAll("[data-detail-item]").forEach(button => {
                button.addEventListener("click", () => readPage(items[Number(button.dataset.detailItem)].pointer));
            });
            body.querySelector("[data-detail-back]")?.addEventListener("click", () => readPage(pointer.slice(0, pointer.lastIndexOf("/"))));
            body.querySelector("[data-detail-prev]")?.addEventListener("click", () => readPage(pointer, Math.max(0, offset - 100)));
            body.querySelector("[data-detail-next]")?.addEventListener("click", () => readPage(pointer, pagination.next_offset));
        } catch (error) {
            if (token !== sequence || !panel.contains(disclosure)) return;
            body.innerHTML = missingBlock("完整证据读取失败或记录已更新，请刷新数据健康后重试。");
        }
    }
    disclosure.addEventListener("toggle", () => {
        if (disclosure.open && !body.innerHTML) readPage();
    });
}

async function renderSettingsEnvironment(data = {}) {
    const panel = document.getElementById("settingsEnvironmentPanel");
    if (!panel) return;
    let buildId = "数据缺失";
    try {
        const build = await requestJson("/api/launcher/build-id");
        buildId = safeText(build.build_id);
    } catch (error) {
        buildId = "读取失败";
    }
    panel.innerHTML = `
        <article class="settings-info-card"><span>服务版本</span><strong>${escapeHtml(safeText(data.app_version || data.schema_version || buildId))}</strong></article>
        <article class="settings-info-card"><span>Build ID</span><strong>${escapeHtml(buildId)}</strong></article>
        <article class="settings-info-card"><span>主服务</span><strong>127.0.0.1:8000 / Flask</strong></article>
        <article class="settings-info-card"><span>网络策略</span><strong>${data.network ? "显式联网" : "默认本地只读"}</strong></article>
        <article class="settings-info-card"><span>账户边界</span><strong>Paper Account Only</strong></article>
        <article class="settings-info-card"><span>交易边界</span><strong>No Broker Route</strong></article>
        <article class="settings-info-card"><span>执行边界</span><strong>Live Execution Locked</strong></article>
        <article class="settings-info-card"><span>QMT 边界</span><strong>QMT Order Route Locked</strong></article>
    `;
}


async function runDataOpsUpdate(kind) {
    const isAll = kind === "all";
    const button = document.getElementById(isAll ? "updateAllDataButton" : "updateCandidatesDailyButton");
    const note = document.getElementById("dataOpsNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "收盘更新中…"; }
    if (note) note.textContent = "正在运行全 A 收盘更新…";
    try {
        const payload = await postJson("/api/data/update-all", { explicit_network: true });
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message || "更新完成。");
        await loadDataHealth();
    } catch (error) {
        if (note) note.textContent = `更新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

// ---- 绩效复盘 / 纸面组合 ----
// ---- 纸面观察名单 ----
function setupPaperWatchlist() {
    const refresh = document.getElementById("refreshPaperWatchlistButton");
    const sync = document.getElementById("syncPaperWatchlistButton");
    const reset = document.getElementById("resetPaperAccountButton");
    if (refresh) refresh.addEventListener("click", refreshPaperWatchlistTracking);
    if (sync) sync.addEventListener("click", syncPaperWatchlistCandidates);
    if (reset) reset.addEventListener("click", handleResetAccount);
    document.addEventListener("click", (event) => {
        const buyButton = event.target instanceof Element ? event.target.closest("[data-paper-buy]") : null;
        if (buyButton) {
            event.preventDefault();
            handlePaperBuyFromButton(buyButton);
            return;
        }
        const sellButton = event.target instanceof Element ? event.target.closest("[data-paper-sell-review]") : null;
        if (sellButton) {
            event.preventDefault();
            handlePaperSellFromButton(sellButton);
            return;
        }
        const oneClick = event.target instanceof Element ? event.target.closest("[data-paper-buy-all]") : null;
        if (oneClick) {
            event.preventDefault();
            handleOneClickBuyAll(currentTodayRecommendations);
            return;
        }
        const button = event.target instanceof Element ? event.target.closest("[data-paper-action]") : null;
        if (!button) return;
        event.preventDefault();
        handlePaperLifecycleAction(button);
    });
}

async function loadPaperWatchlist(force = false) {
    const note = document.getElementById("paperWatchlistNote");
    const panel = document.getElementById("paperWatchlistPanel");
    if (note) note.textContent = force ? "正在刷新纸面模拟池..." : "正在读取纸面模拟池...";
    if (panel && force) panel.innerHTML = `<div class="empty-state">正在刷新纸面模拟池...</div>`;
    try {
        const decisionBoard = await requestJson("/api/decision-board");
        renderDecisionBoardEnvelope(decisionBoard);
        renderPaperTradingDesk(adaptDecisionBoardToPaperDesk(decisionBoard));
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`纸面模拟池读取失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "纸面模拟池读取失败。";
        renderDecisionBoardEnvelope(null, error);
    }
}

function renderDecisionBoardEnvelope(board, error = null) {
    const panel = document.getElementById("decisionBoardEnvelope");
    if (!panel) return;
    if (!board || board.ok === false) {
        panel.innerHTML = `<article class="metric-box"><span>决策看板</span><strong>degraded</strong></article><article class="metric-box"><span>原因</span><strong>${escapeHtml(safeText(error?.message || board?.message_for_ui || "后端证据缺失"))}</strong></article>`;
        return;
    }
    const envelope = board.envelope || {};
    const asOf = envelope.as_of || {};
    const coverage = envelope.coverage || {};
    const version = envelope.strategy_version || {};
    const risk = envelope.risk || {};
    const event = envelope.event || {};
    const profile = envelope.profile || {};
    const manifest = envelope.manifest || {};
    const sources = envelope.source || {};
    const sourceSummary = Object.values(sources).map((item) => safeText(item?.status || "missing")).join(" / ") || "missing";
    panel.innerHTML = `
        <article class="metric-box"><span>As-of</span><strong>${escapeHtml(safeText(asOf.value || "missing"))}</strong></article>
        <article class="metric-box"><span>来源状态</span><strong>${escapeHtml(sourceSummary)}</strong></article>
        <article class="metric-box"><span>覆盖</span><strong>${escapeHtml(safeText(coverage.position_count ?? "missing"))} 纸面持仓 / ${escapeHtml(safeText(coverage.watchlist_count ?? "missing"))} 观察</strong></article>
        <article class="metric-box"><span>策略版本</span><strong>${escapeHtml(safeText(version.value || "missing"))}</strong></article>
        <article class="metric-box"><span>风险复核</span><strong>${escapeHtml(humanStatus(risk.status || "missing"))}</strong></article>
        <article class="metric-box"><span>事件证据</span><strong>${escapeHtml(humanStatus(event.status || "missing"))} · ${escapeHtml(safeText(event.count ?? "missing"))}</strong></article>
        <article class="metric-box"><span>纸面初始资金</span><strong>${escapeHtml(profile.paper_capital == null ? "资金缺失" : moneyText(profile.paper_capital))}</strong><small>${profile.capital_source === "existing_strategy_ledger" ? "来自现有策略账本" : escapeHtml(safeText(profile.profile_id || "资金档缺失"))}${safeText(profile.capital_alignment) === "mismatch" ? " · 资金档与账本待核对" : ""}</small></article>
        <article class="metric-box"><span>Manifest</span><strong title="${escapeHtml(safeText(manifest.manifest_id || ""))}">${escapeHtml(safeText(manifest.manifest_id ? String(manifest.manifest_id).slice(0, 12) : "missing"))}</strong></article>
    `;
}

function adaptDecisionBoardToPaperDesk(board) {
    const account = board?.account || {};
    const portfolioPositions = Array.isArray(board?.positions) ? board.positions : [];
    const watchRows = Array.isArray(board?.watchlist) ? board.watchlist : [];
    return {
        ok: board?.ok !== false,
        status: board?.status || "degraded",
        updated_at: board?.as_of || "",
        active_watchlist_count: watchRows.length,
        items: watchRows.map((item) => ({
            ...item,
            elapsed_trading_days: item.holding_days,
            initial_reference_price: item.entry_price,
            latest_reference_price: item.latest_price,
            latest_reference_date: item.latest_date,
            return_since_added_pct: item.paper_return_pct,
            max_drawdown_since_added: item.max_drawdown_pct,
        })),
        paper_tracking_summary: board?.watchlist_summary || {},
        auto_paper_portfolio: {
            summary: account,
            positions: portfolioPositions,
            actions: Array.isArray(board?.events) ? board.events : [],
            message: "后端权威纸面指标",
        },
        decision_board: board,
    };
}

function renderPaperTradingDesk(data) {
    const note = document.getElementById("paperWatchlistNote");
    const panel = document.getElementById("paperWatchlistPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "纸面模拟池暂不可用。"));
        return;
    }
    if (!document.getElementById("paperAccountSummary")) {
        panel.innerHTML = `
            <div id="paperAccountSummary" class="metric-row"></div>
            <div class="paper-desk-grid">
                <div id="paperStrategyPanel" class="paper-strategy-panel"></div>
                <div id="paperPositionsPanel" class="paper-positions-panel"></div>
            </div>
            <div id="paperDailyReport" class="paper-daily-report">
                <h3>纸面复核提醒</h3>
                <p class="paper-desk-note" id="paperDailyReportDate"></p>
                <div id="paperDailyBuySection" class="paper-report-section">
                    <h4>纸面入池记录</h4>
                    <div id="paperDailyBuyList"></div>
                </div>
                <div id="paperDailySellSection" class="paper-report-section">
                    <h4>待验证复核</h4>
                    <div id="paperDailySellList"></div>
                </div>
            </div>
            <div id="llmDailyReview" class="llm-analysis-card" style="margin-top:14px">
                <div class="llm-analysis-header">
                    <h3>LLM 复盘点评</h3>
                    <span id="llmReviewStatus" class="section-note">分析中...</span>
                </div>
                <div id="llmReviewContent" class="llm-analysis-content">
                    <div class="empty-state">正在生成今日复盘...</div>
                </div>
            </div>
            <div id="paperTradesPanel" class="paper-trades-panel"></div>
        `;
    }
    const items = Array.isArray(data.items) ? data.items : [];
    const activeItems = items.filter((item) => safeText(item.paper_lifecycle_state || "") !== "archived");
    const account = buildPaperAccountSnapshot(data, activeItems);
    const actionRows = buildPaperActionRows(items, data.auto_paper_portfolio);
    renderAccountSummary(account);
    renderDailyReport(account, actionRows);
    loadLLMDailyReview(account, actionRows);
    renderPaperStrategyPanel(data, account);
    renderPositionsTable(account.positions, account);
    renderTradesTable(actionRows);
    if (note) {
        const summary = data.paper_tracking_summary || {};
        note.textContent = `纸面观察池：活跃 ${safeText(data.active_watchlist_count ?? activeItems.length)} 只，需复核观察项 ${safeText(summary.needs_review_count ?? 0)} 只，最新复核 ${safeText(summary.updated_at || data.updated_at || "数据缺失")}。`;
    }
}

function buildPaperAccountSnapshot(data, items) {
    const active = Array.isArray(items) ? items : [];
    const summary = data && data.paper_tracking_summary ? data.paper_tracking_summary : {};
    const paper = data?.auto_paper_portfolio && typeof data.auto_paper_portfolio === "object" ? data.auto_paper_portfolio : {};
    const paperSummary = paper.summary && typeof paper.summary === "object" ? paper.summary : {};
    const conditionalReview = paper.conditional_review && typeof paper.conditional_review === "object" ? paper.conditional_review : {};
    const conditionalExecutionSummary = paper.conditional_execution_summary && typeof paper.conditional_execution_summary === "object"
        ? paper.conditional_execution_summary
        : ((paper.conditional_execution && typeof paper.conditional_execution.summary === "object") ? paper.conditional_execution.summary : {});
    const intradayQualityGate = conditionalReview.intraday_quality_gate && typeof conditionalReview.intraday_quality_gate === "object"
        ? conditionalReview.intraday_quality_gate
        : (conditionalExecutionSummary.intraday_quality_gate && typeof conditionalExecutionSummary.intraday_quality_gate === "object" ? conditionalExecutionSummary.intraday_quality_gate : {});
    const paperPositions = Array.isArray(paper.positions) ? paper.positions : [];
    const hasPortfolio = Boolean(paperSummary && Object.keys(paperSummary).length);
    const latestDates = active.map((item) => isoDateText(item.latest_reference_date)).filter(Boolean).sort();
    const portfolioPositions = paperPositions.map((item) => ({
        ...item,
        entry_price: numericValue(item.entry_price ?? item.avg_cost),
        reported_avg_cost: numericValue(item.avg_cost),
        latest_price: numericValue(item.latest_price),
        quantity: numericValue(item.quantity),
        market_value: numericValue(item.market_value),
        unrealized_pnl: numericValue(item.unrealized_pnl),
        paper_return_pct: numericValue(item.unrealized_return_pct ?? item.return_pct),
        management_state: item.management_state || item.hold_reason || "",
        row_kind: "portfolio",
    }));
    const watchPositions = active.map((item) => {
        const entry = numericValue(item.initial_reference_price) ?? numericValue(item.entry_price);
        const latest = numericValue(item.latest_reference_price) ?? entry;
        return {
            ...item,
            entry_price: entry,
            latest_price: latest,
            paper_return_pct: numericValue(item.return_since_added_pct),
            max_gain_pct: numericValue(item.max_gain_since_added),
            max_drawdown_pct: numericValue(item.max_drawdown_since_added),
            row_kind: "watchlist",
        };
    });
    return {
        initialCash: numericValue(paperSummary.initial_cash),
        accountScale: safeText(paperSummary.account_scale || ""),
        accountScaleNote: safeText(paperSummary.account_scale_note || ""),
        cash: numericValue(paperSummary.cash),
        totalEquity: numericValue(paperSummary.total_equity),
        marketValue: numericValue(paperSummary.market_value),
        totalReturnPct: numericValue(paperSummary.total_return_pct),
        unrealizedPnl: numericValue(paperSummary.unrealized_pnl),
        realizedPnl: numericValue(paperSummary.realized_pnl),
        todayWatchExitCount: numericValue(paperSummary.today_watch_exit_count),
        hasPortfolio,
        portfolioStatus: hasPortfolio ? safeText(paper.message || paper.task_type || "本地纸面组合摘要") : "本地纸面组合账本未返回",
        portfolioPath: paper.report_path || "",
        intradayQualityGate,
        verifiedCompletedIntradayCount: numericValue(conditionalReview.verified_completed_intraday_count ?? conditionalExecutionSummary.verified_completed_intraday_count) ?? 0,
        localUnverifiedIntradayCount: numericValue(conditionalReview.local_unverified_intraday_count ?? conditionalExecutionSummary.local_unverified_intraday_count) ?? 0,
        missingIntradayCount: numericValue(conditionalReview.missing_intraday_count ?? conditionalExecutionSummary.missing_intraday_count) ?? 0,
        activeCount: numericValue(data?.active_watchlist_count) ?? active.length,
        needsReviewCount: numericValue(summary.needs_review_count) ?? 0,
        dataGapCount: numericValue(summary.data_gap_count) ?? 0,
        updatedAt: summary.updated_at || data?.updated_at || "",
        valuationDate: paper.valuation_date || paperSummary.valuation_date || "",
        latestReferenceDate: latestDates.at(-1) || "",
        positions: hasPortfolio ? portfolioPositions : watchPositions,
        portfolioPositions: hasPortfolio ? portfolioPositions : [],
        watchPositions,
    };
}

function paperMinuteGateText(portfolio = {}) {
    const gate = portfolio.intradayQualityGate || {};
    const poolStatus = safeText(gate.pool_status || "missing");
    const poolPassed = gate.pool_passed === true || poolStatus === "passed";
    const target = safeText(gate.quality_target_date || "—");
    return {
        status: poolPassed ? "已验证分钟池" : humanStatus(poolStatus),
        counts: `验证 ${safeText(portfolio.verifiedCompletedIntradayCount || 0)} / 本地 ${safeText(portfolio.localUnverifiedIntradayCount || 0)} / 缺失 ${safeText(portfolio.missingIntradayCount || 0)}`,
        detail: poolPassed
            ? `Top200/已完成分钟池通过质量门；目标日 ${target}。未命中股票仍按缺分钟线降级；本地未验证数据不会假装通过。`
            : "尚未命中通过质量门的已完成分钟池；缺分钟线时不会用日线假装成交。",
    };
}

function renderAccountSummary(portfolio) {
    const panel = document.getElementById("paperAccountSummary");
    if (!panel) return;
    const minuteGate = paperMinuteGateText(portfolio);
    panel.innerHTML = `
        <article class="metric-box"${portfolio.accountScaleNote ? ` title="${escapeHtml(portfolio.accountScaleNote)}"` : ""}><span>初始资金${portfolio.accountScale === "legacy_10k" ? "（教学规模，不与 10 万回测比收益）" : ""}</span><strong>${escapeHtml(portfolio.initialCash === null ? "账本未返回" : moneyText(portfolio.initialCash))}</strong></article>
        <article class="metric-box"><span>当前总资产</span><strong>${escapeHtml(portfolio.totalEquity === null ? "账本未返回" : moneyText(portfolio.totalEquity))}</strong></article>
        <article class="metric-box"><span>可用现金</span><strong>${escapeHtml(portfolio.cash === null ? "账本未返回" : moneyText(portfolio.cash))}</strong></article>
        <article class="metric-box"><span>纸面持仓市值</span><strong>${escapeHtml(portfolio.marketValue === null ? "账本未返回" : moneyText(portfolio.marketValue))}</strong></article>
        <article class="metric-box"><span>累计收益率</span><strong class="${changeClass(portfolio.totalReturnPct)}">${escapeHtml(portfolio.totalReturnPct === null ? "账本未返回" : formatSignedPercent(portfolio.totalReturnPct))}</strong></article>
        <article class="metric-box"><span>账本状态</span><strong>${escapeHtml(portfolio.hasPortfolio ? "本地纸面组合" : "研究池降级")}</strong></article>
        <article class="metric-box"><span>活跃纸面样本</span><strong>${escapeHtml(safeText(portfolio.activeCount))}</strong></article>
        <article class="metric-box"><span>待复核样本（退出待验证口径）</span><strong>${escapeHtml(safeText(portfolio.needsReviewCount))}</strong></article>
        <article class="metric-box"><span>数据缺口</span><strong>${escapeHtml(safeText(portfolio.dataGapCount))}</strong></article>
        <article class="metric-box"><span>最新参考日</span><strong>${escapeHtml(safeText(portfolio.latestReferenceDate || portfolio.updatedAt))}</strong></article>
    `;
    panel.innerHTML += `
        <article class="metric-box" title="${escapeHtml(minuteGate.detail)}"><span>分钟池质量</span><strong>${escapeHtml(minuteGate.status)}</strong></article>
        <article class="metric-box" title="${escapeHtml(minuteGate.detail)}"><span>分钟线来源</span><strong>${escapeHtml(minuteGate.counts)}</strong></article>
    `;
}

function paperReportDate(rows, account = {}) {
    const dates = (Array.isArray(rows) ? rows : [])
        .map((item) => isoDateText(item.date))
        .filter(Boolean)
        .sort();
    return dates.at(-1) || isoDateText(account.valuationDate) || isoDateText(account.latestReferenceDate) || "日期未核实";
}

function paperActionKind(row) {
    const text = `${safeText(row?.action)} ${safeText(row?.status)} ${safeText(row?.source)}`.toLowerCase();
    if (row?.is_pending || text.includes("exit") || text.includes("review") || text.includes("退出") || text.includes("降级")) return "exit";
    if (text.includes("buy") || text.includes("加入") || text.includes("入池") || text.includes("纸面建仓") || text.includes("建仓")) return "entry";
    return "other";
}

function renderReportRows(rows, kind) {
    const list = (Array.isArray(rows) ? rows : []).filter((row) => paperActionKind(row) === kind);
    if (!list.length) {
        return missingBlock(kind === "entry" ? "该日期没有新的纸面入池记录。" : "该日期没有退出复核记录。");
    }
    return list.map((row) => {
        const pending = row.is_pending === true;
        const statusLabel = pending ? "待验证，未模拟成交" : humanStatus(row.status || row.action || "纸面记录");
        const badgeClass = kind === "entry" ? "badge-ok" : "badge-exit";
        const price = pending
            ? "缺分钟线"
            : hasValue(row.entry_price)
                ? numText(row.entry_price, 2)
                : "参考价缺失";
        const latest = pending
            ? "触发待验证"
            : hasValue(row.latest_price)
                ? numText(row.latest_price, 2)
                : "最新参考缺失";
        const note = sanitizeTradingTerms(humanizeStatusTerms(row.note || ""));
        return `
            <div class="report-row">
                <div class="report-symbol">${escapeHtml(safeText(row.symbol))} ${escapeHtml(safeText(row.name || ""))}</div>
                <span class="badge ${badgeClass}">${escapeHtml(statusLabel)}</span>
                <div class="report-detail">${escapeHtml(price)} / ${escapeHtml(latest)}</div>
                <div class="report-reason">${escapeHtml(note || "本地纸面研究记录，未返回具体原因。")}</div>
            </div>
        `;
    }).join("");
}

function renderDailyReport(account, actionRows) {
    const panel = document.getElementById("paperDailyReport");
    const dateEl = document.getElementById("paperDailyReportDate");
    const buyList = document.getElementById("paperDailyBuyList");
    const sellList = document.getElementById("paperDailySellList");
    if (!panel || !buyList || !sellList) return;
    const rows = Array.isArray(actionRows) ? actionRows : [];
    const reportDate = paperReportDate(rows, account);
    const reportRows = rows.filter((row) => isoDateText(row.date) === reportDate);
    const pendingCount = reportRows.filter((row) => row.is_pending).length;
    const titleEl = panel.querySelector("h3");
    const buyTitle = document.querySelector("#paperDailyBuySection h4");
    const sellTitle = document.querySelector("#paperDailySellSection h4");
    if (titleEl) titleEl.textContent = pendingCount ? "纸面待验证复核提醒" : "纸面动作报告";
    if (buyTitle) buyTitle.textContent = "纸面入池记录";
    if (sellTitle) sellTitle.textContent = pendingCount ? "待验证复核（未模拟成交）" : "退出复核记录";
    if (dateEl) {
        dateEl.textContent = `报告日期：${reportDate}；本页只展示本地纸面研究记录。${pendingCount ? `其中 ${pendingCount} 条为待盘中验证提醒，不代表已经成交。` : "无待验证成交记录。"}`;
    }
    buyList.innerHTML = renderReportRows(reportRows, "entry");
    sellList.innerHTML = renderReportRows(reportRows, "exit");
}

async function loadLLMDailyReview(account, actionRows) {
    const statusEl = document.getElementById("llmReviewStatus");
    const contentEl = document.getElementById("llmReviewContent");
    if (!contentEl) return;
    const rows = Array.isArray(actionRows) ? actionRows : [];
    const reportDate = paperReportDate(rows, account);
    const reportRows = rows.filter((row) => isoDateText(row.date) === reportDate);
    if (!reportRows.length) {
        if (statusEl) statusEl.textContent = "暂无纸面动作";
        contentEl.innerHTML = missingBlock("当前没有可复盘的纸面入池或退出复核记录。");
        return;
    }
    const actionSummary = reportRows.slice(0, 16).map((row) => {
        const kind = paperActionKind(row) === "entry" ? "纸面入池" : paperActionKind(row) === "exit" ? "退出复核" : "纸面记录";
        const pending = row.is_pending ? "待验证未成交" : humanStatus(row.status || "已记录");
        return `${safeText(row.symbol)} ${safeText(row.name || "")}：${kind}，${pending}，${sanitizeTradingTerms(humanizeStatusTerms(row.note || ""))}`;
    }).join("\n");
    const question = `请复盘 ${reportDate} 的 QuantDesk 纸面研究动作，重点说明入池/退出复核理由、数据缺口、是否存在分钟线待验证或口径风险。不要给出买卖指令或真实配置指令。\n${actionSummary}`;
    renderAssistantDraft(contentEl, statusEl, question, "daily_review", `策略账 ${reportDate} 已读动作`);
}

function renderAssistantDraft(contentEl, statusEl, question, skill, sourceLabel) {
    if (statusEl) statusEl.textContent = "尚未请求模型";
    contentEl.innerHTML = `<p class="section-note">${escapeHtml(sourceLabel)}。查看页面只读取记录；可把这些内容带到研究室，检查后手动发送。</p><button type="button" class="action-button secondary-action" data-review-draft>带到研究室</button>`;
    const button = contentEl.querySelector("[data-review-draft]");
    if (!button) return;
    button.onclick = () => {
        if (typeof window.quantdeskShowModuleView === "function") window.quantdeskShowModuleView("agent-section");
        const input = document.getElementById("agentQuestionInput");
        const skillSelect = document.getElementById("agentSkillSelect");
        if (skillSelect && Array.from(skillSelect.options).some((option) => option.value === skill)) skillSelect.value = skill;
        if (input) { input.value = question; input.focus(); }
    };
}

function renderPaperStrategyPanel(data, account) {
    const panel = document.getElementById("paperStrategyPanel");
    if (!panel) return;
    const watchItems = Array.isArray(data.items) ? data.items : [];
    const today = currentTodayRecommendations.length ? currentTodayRecommendations : watchItems.slice(0, 7);
    const rows = today.slice(0, 10).map((item) => {
        const symbol = safeText(item.symbol);
        const price = numericValue(item.current_price ?? item.latest_price ?? item.latest_reference_price ?? item.close ?? item.initial_reference_price);
        const name = safeText(item.name || symbol);
        return `
            <div class="paper-strategy-row">
                <span class="number">${escapeHtml(symbol)}</span>
                <span>${escapeHtml(name)}<div class="paper-desk-note">Research Only · 不构成投资建议</div></span>
                <span>${missingBadge()}</span>
                <span>${price ? escapeHtml(numText(price, 2)) : missingBadge()}</span>
                <button class="action-button" type="button" data-paper-buy data-symbol="${escapeHtml(symbol)}" data-price="${escapeHtml(price || "")}" data-entry-date="${escapeHtml(safeText(item.select_date || item.entry_date || ""))}" data-lane="${escapeHtml(safeText(item.detail_lane || item.strategy_lane || ""))}">加入研究观察</button>
            </div>
        `;
    }).join("");
    panel.innerHTML = `
        <div class="section-head" style="margin-bottom:8px">
            <h3>今日候选池</h3>
            <button class="action-button secondary-action" type="button" data-paper-buy-all>一键加入研究观察</button>
        </div>
        ${rows || missingBlock("暂无可同步的今日策略候选。")}
        <p class="paper-desk-note">等权观察预算只作为视图基准，不代表真实配置指令；写入动作仅进入本地纸面研究池，不接券商、不形成交易指令。以下为研究模拟记录，不构成投资建议，不包含真实交易指令。</p>
    `;
}

function checkPositionStatus(item) {
    const rawStatusText = [
        item.management_state,
        item.hold_reason,
        item.expectation_fit_label,
        item.review_label,
        item.review_status,
    ].filter((value) => hasValue(value)).map((value) => String(value)).join(" ");
    const statusText = rawStatusText ? humanizeStatusTerms(sanitizeTradingTerms(rawStatusText)) : "";
    const metricStatus = safeText(item.metric_status || "missing").toLowerCase();
    const lowerText = statusText.toLowerCase();
    if (metricStatus !== "passed" || /缺|missing|数据不足|待补|缺分钟线|degraded/.test(lowerText)) {
        return { label: "数据待补", className: "badge-degraded", reason: statusText || "关键数据缺失，暂不生成结论。" };
    }
    if (/复核|偏离|退出|review|trigger|risk/.test(lowerText)) {
        return { label: "后端复核中", className: "badge badge-exit", reason: statusText || "后端返回纸面复核状态。" };
    }
    if (/符合|继续|健康|正常|可用/.test(statusText)) {
        return { label: "符合观察", className: "badge badge-ok", reason: statusText };
    }
    return { label: "观察中", className: "badge", reason: statusText || "等待更多本地样本验证。" };
}

function renderPositionsTable(positions, account = {}) {
    const panel = document.getElementById("paperPositionsPanel");
    if (!panel) return;
    const rows = Array.isArray(positions) ? positions : [];
    const portfolioRows = Array.isArray(account.portfolioPositions) && account.portfolioPositions.length ? account.portfolioPositions : (account.hasPortfolio ? rows : []);
    const watchRows = Array.isArray(account.watchPositions) ? account.watchPositions : (!account.hasPortfolio ? rows : []);
    rememberWorkbenchSource("strategy_positions", portfolioRows);
    rememberWorkbenchSource("strategy_watchlist", watchRows);
    const portfolioBody = portfolioRows.map((item) => {
        const status = checkPositionStatus(item);
        const entryDate = safeText(item.entry_date || item.buy_date || item.added_at || "");
        const holdingDays = numericValue(item.holding_days ?? item.elapsed_trading_days);
        const entryPrice = numericValue(item.entry_price ?? item.buy_price);
        const latestPrice = numericValue(item.latest_price ?? item.current_price);
        const quantity = numericValue(item.quantity);
        const pnlAmount = numericValue(item.unrealized_pnl);
        return `
            <tr>
                <td><button type="button" class="action-button secondary-action" data-position-source="strategy_positions" data-position-symbol="${escapeHtml(safeText(item.symbol))}">${escapeHtml(safeText(item.symbol))}</button><br><span style="font-size:11px;color:var(--text-muted)">${escapeHtml(safeText(item.name || ""))}</span></td>
                <td class="number">${escapeHtml(String(entryDate).slice(0, 10) || "—")}</td>
                <td class="number">${holdingDays === null ? missingBadge() : `${escapeHtml(String(holdingDays))}天`}</td>
                <td class="number" title="盈亏计算使用此成本；原记录均价另列，不在浏览器调平。">${entryPrice !== null ? escapeHtml(numText(entryPrice, 4)) : missingBadge()}${numericValue(item.reported_avg_cost) !== null && numericValue(item.reported_avg_cost) !== entryPrice ? `<br><small>原记录均价 ${escapeHtml(numText(item.reported_avg_cost, 4))}</small>` : ""}</td>
                <td class="number">${latestPrice !== null ? escapeHtml(numText(latestPrice, 2)) : missingBadge()}<br><small>估值 ${escapeHtml(isoDateText(item.latest_date) || account.valuationDate || "未核实")}</small></td>
                <td class="number">${quantity !== null ? escapeHtml(safeText(quantity)) : missingBadge()}</td>
                <td class="number ${changeClass(pnlAmount)}">${pnlAmount === null ? missingBadge() : escapeHtml(moneyText(pnlAmount))}</td>
                <td class="number ${changeClass(item.paper_return_pct)}">${sanePercentHTML(item.paper_return_pct, { mode: "points", digits: 2 })}</td>
                <td><span class="${escapeHtml(status.className)}">${escapeHtml(status.label)}</span></td>
            </tr>
        `;
    }).join("");
    const watchBody = watchRows.map((item) => {
        const status = checkPositionStatus(item);
        const entryDate = safeText(item.entry_date || item.added_at || "");
        const holdingDays = numericValue(item.holding_days ?? item.elapsed_trading_days);
        const entryPrice = numericValue(item.initial_reference_price ?? item.entry_price);
        const latestPrice = numericValue(item.latest_reference_price ?? item.latest_price);
        const pnlAmount = numericValue(item.paper_pnl);
        return `
            <tr>
                <td><button type="button" class="action-button secondary-action" data-position-source="strategy_watchlist" data-position-symbol="${escapeHtml(safeText(item.symbol))}">${escapeHtml(safeText(item.symbol))}</button><br><span style="font-size:11px;color:var(--text-muted)">${escapeHtml(safeText(item.name || ""))}</span></td>
                <td class="number">${escapeHtml(String(entryDate).slice(0, 10) || "—")}</td>
                <td class="number">${holdingDays === null ? missingBadge() : `${escapeHtml(String(holdingDays))}天`}<br><small>截至 ${escapeHtml(isoDateText(item.latest_reference_date || item.tracking_updated_at) || "未核实")}</small></td>
                <td class="number">${entryPrice !== null ? escapeHtml(numText(entryPrice, 2)) : missingBadge()}</td>
                <td class="number">${latestPrice !== null ? escapeHtml(numText(latestPrice, 2)) : missingBadge()}<br><small>行情 ${escapeHtml(isoDateText(item.latest_reference_date || item.latest_date) || "未核实")}</small></td>
                <td class="number ${changeClass(item.paper_return_pct)}">${sanePercentHTML(item.paper_return_pct, { mode: "points", digits: 2 })}</td>
                <td class="number ${changeClass(pnlAmount)}">${pnlAmount === null ? missingBadge() : escapeHtml(moneyText(pnlAmount))}</td>
                <td class="number ${changeClass(item.max_drawdown_pct)}">${sanePercentHTML(item.max_drawdown_pct, { mode: "points", digits: 2 })}</td>
                <td><span class="${escapeHtml(status.className)}">${escapeHtml(status.label)}</span></td>
            </tr>
        `;
    }).join("");
    panel.innerHTML = `
        <h3>持仓复核总表</h3>
        <p class="paper-desk-note">先看本地纸面组合，再看完整观察池。组合持仓有股数和纸面盈亏；观察池是你已选择/同步的研究样本，不套用真实持仓口径。</p>
        <div class="position-summary-strip">
            <span><b>${escapeHtml(safeText(portfolioRows.length))}</b> 只组合持仓</span>
            <span><b>${escapeHtml(safeText(watchRows.length))}</b> 只观察池样本</span>
            <span><b>${escapeHtml(safeText(account.todayWatchExitCount ?? 0))}</b> 条待验证复核</span>
        </div>
        <section class="position-subsection">
            <h4>纸面组合持仓（有股数 / 当前盈亏）</h4>
            <p class="paper-desk-note">持有天数、纸面盈亏和复核状态全部来自后端权威字段；字段缺失时只显示 missing/degraded。Research Only · 非止损指令。</p>
            <div class="paper-table-wrap">
                <table class="paper-positions-table">
                    <thead><tr><th>股票</th><th>选入日期</th><th>持有天数</th><th>纸面盈亏计算成本</th><th>最新价/估值日</th><th>股数</th><th>纸面盈亏</th><th>纸面盈亏率</th><th>策略状态</th></tr></thead>
                    <tbody>${portfolioBody || `<tr><td colspan="9">暂无本地纸面组合持仓。</td></tr>`}</tbody>
                </table>
            </div>
        </section>
        <section class="position-subsection">
            <h4>研究观察池 / 已选择样本（仅观察，不产生持仓与盈亏）</h4>
            <p class="paper-desk-note">这里显示全部活跃研究观察样本；旧池按记录中的行情日和跟踪截止日展示，不冒充今天。观察池不产生持仓；后端未返回的盈亏保持未知，不在浏览器估算。</p>
            <div class="paper-table-wrap">
                <table class="paper-positions-table">
                    <thead><tr><th>股票</th><th>选入日期</th><th>持有天数</th><th>入池参考价</th><th>最新参考价</th><th>入池以来</th><th>纸面盈亏（后端）</th><th>最大回撤</th><th>策略状态</th></tr></thead>
                    <tbody>${watchBody || `<tr><td colspan="9">暂无纸面观察池样本。</td></tr>`}</tbody>
                </table>
            </div>
        </section>
    `;
    panel.querySelectorAll("[data-position-symbol]").forEach((button) => {
        button.onclick = () => {
            const items = button.dataset.positionSource === "strategy_positions" ? portfolioRows : watchRows;
            const item = items.find((position) => String(position.symbol) === button.dataset.positionSymbol);
            if (item) return showDetail(item, { source: button.dataset.positionSource, items });
        };
    });
}

function buildPaperActionRows(items, paper = null) {
    const rows = [];
    const paperActions = Array.isArray(paper?.daily_actions) && paper.daily_actions.length
        ? paper.daily_actions
        : (Array.isArray(paper?.actions) ? paper.actions : []);
    paperActions.forEach((item) => {
        const isPending = (!item.quantity || Number(item.quantity) === 0)
            || item.fill_model === "missing_intraday"
            || item.trigger_type === "missing_intraday"
            || (item.action === "exit_watch" && !item.trigger_price);
        rows.push({
            date: item.decision_date || item.trade_date || item.plan_date || "",
            symbol: item.symbol,
            name: item.name,
            action: item.action_label || item.action,
            entry_price: item.price,
            latest_price: item.trigger_price,
            shares: item.quantity,
            status: item.fill_model || item.trigger_type || "",
            paper_return_pct: null,
            note: item.reason || item.order_source || "本地纸面组合动作",
            source: "portfolio",
            is_pending: isPending,
        });
    });
    (Array.isArray(items) ? items : []).forEach((item) => {
        rows.push({
            date: item.entry_date || item.added_at || "",
            symbol: item.symbol,
            name: item.name,
            action: "加入研究观察",
            entry_price: item.initial_reference_price,
            latest_price: item.latest_reference_price,
            shares: "",
            status: item.review_label || item.review_status,
            paper_return_pct: item.return_since_added_pct,
            note: item.candidate_origin_label || "纸面研究样本",
            source: "watchlist",
            is_pending: false,
        });
        if (item.review_label || item.review_status) {
            rows.push({
                date: item.updated_at || item.last_tracking_refresh_at || item.entry_date || "",
                symbol: item.symbol,
                name: item.name,
                action: item.review_label || item.review_status,
                entry_price: item.initial_reference_price,
                latest_price: item.latest_reference_price,
                shares: "",
                status: item.expectation_fit_label || item.review_status,
                paper_return_pct: item.return_since_added_pct,
                note: item.expectation_fit_reason || item.review_reason || "",
                source: "watchlist",
                is_pending: false,
            });
        }
    });
    return rows.sort((a, b) => safeText(b.date).localeCompare(safeText(a.date))).slice(0, 40);
}

function pendingDateText(value) {
    const date = isoDateText(value);
    return date ? `待验证（${date.slice(5)}）` : "待验证";
}

function renderTradesTable(trades) {
    const panel = document.getElementById("paperTradesPanel");
    if (!panel) return;
    const rows = Array.isArray(trades) ? trades : [];
    const pendingCount = rows.filter((item) => item.is_pending).length;
    const body = rows.map((item) => {
        const pending = item.is_pending === true;
        const date = pending ? pendingDateText(item.date) : safeText(item.date);
        const action = pending ? "保留观察" : sanitizeTradingTerms(safeText(item.action));
        const priceText = pending ? "缺分钟线" : (item.entry_price ? numText(item.entry_price, 2) : "—");
        const latestText = pending ? "缺分钟线" : (item.latest_price ? numText(item.latest_price, 2) : "—");
        const statusHtml = pending ? `<span class="badge-degraded">未模拟成交</span>` : escapeHtml(humanStatus(item.status || "—"));
        const noteText = sanitizeTradingTerms(humanizeStatusTerms(item.note || ""));
        return `
            <tr${pending ? ` class="pending-row"` : ""}>
                <td>${escapeHtml(date)}</td>
                <td>${escapeHtml(safeText(item.symbol))} ${escapeHtml(safeText(item.name || ""))}</td>
                <td>${escapeHtml(action)}</td>
                <td class="number">${escapeHtml(pending ? "—" : (hasValue(item.shares) ? safeText(item.shares) : "—"))}</td>
                <td class="number">${escapeHtml(priceText)}</td>
                <td class="number">${escapeHtml(latestText)}</td>
                <td>${statusHtml}</td>
                <td class="number ${changeClass(item.paper_return_pct)}">${escapeHtml(item.paper_return_pct === null || item.paper_return_pct === undefined ? "—" : formatSignedPercent(item.paper_return_pct))}</td>
                <td>${escapeHtml(safeText(item.source || "watchlist"))}</td>
                <td>${escapeHtml(noteText || "—")}</td>
            </tr>
        `;
    }).join("");
    panel.innerHTML = `
        <h3>纸面动作记录 ${pendingCount ? `<span class="badge-degraded">⚠ ${escapeHtml(safeText(pendingCount))} 条待验证</span>` : ""}</h3>
        <p class="paper-desk-note">待验证行表示下一交易日需盘中确认，当前未模拟成交。以下为研究模拟记录，不构成投资建议，不包含真实交易指令。</p>
        <div class="paper-table-wrap">
            <table class="paper-trades-table">
                <thead><tr><th>日期</th><th>股票</th><th>动作</th><th>股数</th><th>价格/参考</th><th>触发/最新</th><th>状态</th><th>入池以来</th><th>来源</th><th>说明</th></tr></thead>
                <tbody>${body || `<tr><td colspan="10">暂无纸面动作记录。</td></tr>`}</tbody>
            </table>
        </div>
        <p class="paper-desk-note">这里合并展示本地纸面组合动作和研究池生命周期记录；缺少分钟线时不会用日线价格假装成交，费用/净额只有后端返回权威字段时才展示。</p>
    `;
}

function renderPaperWatchlist(data) {
    return renderPaperTradingDesk(data);
}

function renderPaperWatchlistLegacy(data) {
    const note = document.getElementById("paperWatchlistNote");
    const panel = document.getElementById("paperWatchlistPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "纸面模拟池暂不可用。"));
        return;
    }
    const items = Array.isArray(data.items) ? data.items : [];
    const summary = data.paper_tracking_summary || {};
    const statusCounts = summary.status_counts || {};
    const reviewCount = numericValue(summary.needs_review_count) ?? items.filter((item) => /review|trigger/i.test(safeText(item.review_status))).length;
    const dataGapCount = numericValue(summary.data_gap_count) ?? 0;
    const fitCounts = summary.expectation_fit_counts || {};
    const statusTags = Object.entries(statusCounts).slice(0, 8).map(([key, value]) => `
        <span class="tag tag-watch">${escapeHtml(humanStatus(key))}: ${escapeHtml(safeText(value))}</span>
    `).join("");
    const fitTags = Object.entries(fitCounts).slice(0, 8).map(([key, value]) => `
        <span class="tag">${escapeHtml(humanizeStatusTerms(key))}: ${escapeHtml(safeText(value))}</span>
    `).join("");
    const itemCards = items.map((item) => {
        const stop = item.smart_stop || {};
        const stopStatus = safeText(stop.trigger_status || "watch");
        const reviewStatus = safeText(item.review_status || "paper_watch");
        const lifecycleState = safeText(item.paper_lifecycle_state || "active_tracking");
        const isArchived = lifecycleState === "archived";
        const tone = paperWatchlistStatusClass(`${reviewStatus} ${stopStatus}`);
        const ret = numericValue(item.return_since_added_pct);
        const gain = numericValue(item.max_gain_since_added);
        const drawdown = numericValue(item.max_drawdown_since_added);
        const evidence = firstSentence(item.evidence_summary || item.tracking_message || item.review_reason, "暂无复核摘要");
        const fitTone = paperWatchlistStatusClass(`${item.expectation_fit_status || ""} ${item.review_tone || ""}`);
        const validation = item.validation_cycle || {};
        const successConditions = Array.isArray(item.success_conditions) ? item.success_conditions : [];
        const failureConditions = Array.isArray(item.failure_conditions) ? item.failure_conditions : [];
        const tags = Array.isArray(item.strategy_tags) ? item.strategy_tags : [];
        const expectedPath = item.expected_path || item.selection_reason || "等待补齐原始研究假设。";
        const trackingPlan = item.daily_tracking_plan || "每天收盘后刷新纸面表现，并复核是否符合原始预期。";
        const llmOpinion = item.llm_research_opinion || "待在 LLM 研究室基于本地证据讨论；用户确认后仅进入纸面模拟跟踪。";
        const decision = item.research_decision || {};
        const decisionStatus = safeText(decision.decision_status || item.llm_decision_status || "pending_discussion");
        const decisionSourceLabel = safeText(decision.decision_source_label || (decision.llm_used ? "LLM 研究意见" : "本地证据兜底（非外部 LLM）"));
        const decisionConfidence = safeText(decision.confidence || "—");
        return `
            <div class="perf-card paper-card-compact" role="button" tabindex="0"
                data-symbol="${escapeHtml(safeText(item.symbol))}"
                data-name="${escapeHtml(safeText(item.name || item.symbol))}"
                data-lane="${escapeHtml(safeText(item.detail_lane || ""))}"
                data-entry-date="${escapeHtml(safeText(item.entry_date || item.select_date || ""))}">
                <div class="paper-summary-title">
                    <strong>${escapeHtml(safeText(item.name || item.symbol))}</strong>
                    <span class="badge">${escapeHtml(safeText(item.symbol))}</span>
                    <span class="health-badge ${tone}">${escapeHtml(humanizeStatusTerms(item.review_label || reviewStatus))}</span>
                </div>
                <div class="paper-summary-metrics">
                    <span>主动作 <b>${escapeHtml(safeText(item.primary_action || item.strategy || "观察"))}</b></span>
                    <span class="${fitTone}">${escapeHtml(humanizeStatusTerms(item.expectation_fit_label || "等待样本"))}</span>
                    <span>入池 <b class="${changeClass(ret)}">${formatSignedPercent(ret)}</b></span>
                    <span>回撤 <b class="${changeClass(drawdown)}">${sanePercentHTML(drawdown, { mode: "points", digits: 2 })}</b></span>
                    <span>跟踪 <b>${escapeHtml(safeText(item.elapsed_trading_days ?? 0))}</b> 天</span>
                    <span class="paper-summary-decision">${escapeHtml(humanStatus(decisionStatus))}</span>
                </div>
                <div class="paper-contract-grid">
                    <div class="paper-contract-item"><span>原始研究假设</span><p>${escapeHtml(sanitizeTradingTerms(safeText(expectedPath)))}</p></div>
                    <div class="paper-contract-item"><span>LLM 研究意见</span><p>${escapeHtml(sanitizeTradingTerms(safeText(llmOpinion)))}</p></div>
                    <div class="paper-contract-item"><span>每日跟踪计划</span><p>${escapeHtml(sanitizeTradingTerms(safeText(trackingPlan)))}</p></div>
                    <div class="paper-contract-item"><span>成功条件</span><p>${escapeHtml(sanitizeTradingTerms(successConditions.map(safeText).filter(Boolean).slice(0, 2).join("；") || "等待补充成功验证条件。"))}</p></div>
                    <div class="paper-contract-item"><span>失败/降级条件</span><p>${escapeHtml(sanitizeTradingTerms(failureConditions.map(safeText).filter(Boolean).slice(0, 2).join("；") || "等待补充失败或降级条件。"))}</p></div>
                    <div class="paper-contract-item"><span>${escapeHtml(decisionSourceLabel)}</span><p>${escapeHtml(`置信度：${decisionConfidence}`)}</p></div>
                </div>
                <div class="paper-summary-foot">
                    <span class="paper-open-hint">点击查看详情（K线 / 理由 / 契约）›</span>
                    <div class="paper-action-row" data-paper-symbol="${escapeHtml(safeText(item.symbol))}" data-paper-watchlist-id="${escapeHtml(safeText(item.watchlist_id || ""))}">
                        <button class="action-button" type="button" data-paper-action="continue">继续验证</button>
                        <button class="action-button is-danger" type="button" data-paper-action="review">降级复盘</button>
                        <button class="action-button is-muted" type="button" data-paper-action="archive">归档</button>
                        ${isArchived ? `<button class="action-button" type="button" data-paper-action="restore">恢复</button>` : ""}
                    </div>
                </div>
            </div>
        `;
    }).join("");
    const activeCount = numericValue(data.active_watchlist_count ?? summary.active_count) ?? items.filter((item) => safeText(item.paper_lifecycle_state || "") !== "archived").length;
    const archivedCount = numericValue(data.archived_watchlist_count ?? summary.archived_count) ?? Math.max(0, items.length - activeCount);
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>纸面模拟池 <span class="health-badge ${reviewCount ? "health-warn" : "health-ok"}">${escapeHtml(humanStatus(data.status || "passed"))}</span></strong>
            <div class="section-note">${escapeHtml(safeText(data.message_for_ui || data.disclaimer || "纸面观察名单只用于本地学习跟踪。"))}</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>活跃样本</span><strong>${escapeHtml(safeText(activeCount))}</strong></div>
            <div class="metric-box"><span>归档保留</span><strong>${escapeHtml(safeText(archivedCount))}</strong></div>
            <div class="metric-box"><span>已刷新</span><strong>${escapeHtml(safeText(summary.updated_count ?? 0))}</strong></div>
            <div class="metric-box"><span>需复核观察项</span><strong>${escapeHtml(safeText(reviewCount))}</strong></div>
            <div class="metric-box"><span>数据缺口</span><strong>${escapeHtml(safeText(dataGapCount))}</strong></div>
            <div class="metric-box"><span>更新时间</span><strong>${escapeHtml(safeText(summary.updated_at || data.updated_at || "数据缺失"))}</strong></div>
            <div class="metric-box"><span>边界</span><strong>Paper Only</strong></div>
        </div>
        <div class="tag-row">${fitTags || `<span class="tag">暂无预期兑现分布</span>`}</div>
        <div class="tag-row">${statusTags || `<span class="tag">暂无状态分布</span>`}</div>
        <div class="perf-strategy-grid">${itemCards || missingBlock("暂无纸面模拟样本。可以先同步今日重点到纸面池。")}</div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route / Live Execution Locked / QMT Order Route Locked / No Real Orders / Not Investment Advice / Not Position Advice.</p>
    `;
    if (note) note.textContent = `纸面观察池：活跃 ${safeText(activeCount)} 只，归档 ${safeText(archivedCount)} 只，需复核观察项 ${safeText(reviewCount)} 只。`;
    bindPaperCardClicks(panel);
}

function bindPaperCardClicks(root) {
    if (!root) return;
    const cards = Array.from(root.querySelectorAll(".paper-card-compact"));
    const items = cards.map((card) => ({
        symbol: card.dataset.symbol || "", name: card.dataset.name || card.dataset.symbol || "",
        detail_lane: card.dataset.lane || "", select_date: card.dataset.entryDate || "",
    }));
    cards.forEach((card, index) => {
        card.addEventListener("click", (event) => {
            // Let the lifecycle action buttons work without opening the detail.
            if (event.target instanceof Element && event.target.closest("[data-paper-action]")) return;
            showDetail(items[index], { source: "strategy_watchlist", items });
        });
    });
}

function handlePaperBuyFromButton(button) {
    const symbol = button.dataset.symbol || "";
    const price = numericValue(button.dataset.price);
    handlePaperBuy(symbol, price, {
        entry_date: button.dataset.entryDate || "",
        lane: button.dataset.lane || "",
        button,
    });
}

async function handlePaperBuy(symbol, price, options = {}) {
    const note = document.getElementById("paperWatchlistNote");
    const button = options.button;
    const original = button ? button.textContent : "";
    if (!symbol) {
        if (note) note.textContent = "加入研究观察失败：缺少股票代码。";
        return;
    }
    if (button) { button.disabled = true; button.textContent = "写入中…"; }
    try {
        const payload = await postJson("/api/paper-simulation/add-candidate", {
            symbol,
            entry_date: options.entry_date || "",
            lane: options.lane || "",
            note: `从纸面模拟复核台加入；界面参考价 ${safeText(price)}。Research Only，不构成投资建议。`,
        });
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message || "已加入纸面模拟池。");
        await loadPaperWatchlist(true);
    } catch (error) {
        if (note) note.textContent = `加入研究观察失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

function handlePaperSellFromButton(button) {
    handlePaperSell(button.dataset.symbol || "", numericValue(button.dataset.price));
}

function handlePaperSell(symbol, price) {
    const modal = document.getElementById("detailModal");
    const title = document.getElementById("modalTitle");
    const subtitle = document.getElementById("modalSubtitle");
    const body = document.getElementById("modalBody");
    if (!modal || !title || !body) return;
    title.textContent = "纸面退出复核";
    if (subtitle) subtitle.textContent = `${safeText(symbol)} · 仅更新本地纸面研究状态`;
    body.innerHTML = `
        <section class="detail-section">
            <h3>退出复核确认</h3>
            <div class="info-grid">
                ${infoItem("股票代码", symbol)}
                ${infoItem("最新参考价", price ? numText(price, 2) : "数据缺失")}
                ${infoItem("动作类型", "纸面退出复核")}
                ${infoItem("成交状态", "不会生成成交、费用或订单")}
            </div>
            <p class="panel-disclaimer">当前后端仅开放纸面研究池生命周期接口；确认后只把该样本标记为“降级/退出复核”，不会生成成交、费用、真实订单或券商订单。</p>
            <div class="section-actions" style="justify-content:flex-start;margin-top:12px">
                <button id="confirmPaperExitReviewButton" class="action-button is-danger" type="button">确认退出复核</button>
                <button id="cancelPaperExitReviewButton" class="action-button secondary-action" type="button">取消</button>
            </div>
        </section>
    `;
    modal.classList.add("open");
    document.body.classList.add("modal-open");
    document.getElementById("cancelPaperExitReviewButton")?.addEventListener("click", closeModal);
    document.getElementById("confirmPaperExitReviewButton")?.addEventListener("click", async () => {
        const note = document.getElementById("paperWatchlistNote");
        try {
            const payload = await postJson("/api/paper-simulation/action", {
                symbol,
                action: "review",
                note: "用户在纸面研究复核台确认退出复核；非真实卖出，不接券商。",
            });
            if (note) note.textContent = safeText(payload.message_for_ui || payload.message || "已进入退出复核。");
            closeModal();
            await loadPaperWatchlist(true);
        } catch (error) {
            if (note) note.textContent = `退出复核失败：${escapeHtml(errorText(error))}`;
        }
    });
}

async function handleOneClickBuyAll(candidates) {
    const note = document.getElementById("paperWatchlistNote");
    if (note) note.textContent = "正在把今日候选同步到纸面池…";
    await syncPaperWatchlistCandidates();
}

async function handleExecuteStrategy() {
    const button = document.getElementById("executeStrategyButton");
    const status = document.getElementById("strategyExecuteStatus");
    const original = button ? button.textContent : "";
    if (!currentTodayRecommendations.length && status) {
        status.textContent = "当前未读取到今日策略候选，仍会请求后端按最新候选批次同步。";
    }
    if (button) { button.disabled = true; button.textContent = "同步中..."; }
    if (status) status.textContent = "正在同步今日候选到本地纸面研究池；不会连接券商或生成真实订单。";
    try {
        const payload = await postJson("/api/paper-simulation/sync-candidates", {
            include_observation: false,
            max_candidates: 10,
        });
        if (status) status.textContent = safeText(payload.message || payload.message_for_ui || "今日候选已同步到纸面研究池。");
        agentStatusLoaded = false;
        await Promise.all([loadPaperWatchlist(true), loadCandidates(), loadRiskNotifications()]);
    } catch (error) {
        if (status) status.textContent = `同步纸面策略失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

async function handleResetAccount() {
    const note = document.getElementById("paperWatchlistNote");
    if (note) note.textContent = "当前后端仅开放纸面研究池视图刷新；已重新读取纸面模拟视图。";
    await loadPaperWatchlist(true);
}

async function handlePaperLifecycleAction(button) {
    const panel = button.closest("[data-paper-symbol]");
    const symbol = panel ? panel.getAttribute("data-paper-symbol") : "";
    const watchlistId = panel ? panel.getAttribute("data-paper-watchlist-id") : "";
    const action = button.getAttribute("data-paper-action") || "";
    const note = document.getElementById("paperWatchlistNote");
    const original = button.textContent;
    const actionLabels = {
        continue: "继续验证",
        review: "确认降级复盘",
        archive: "归档保留",
        restore: "恢复跟踪",
    };
    const noteText = action === "review"
        ? "用户确认偏离原始预期，进入降级复盘。"
        : action === "archive"
            ? "用户确认归档保留，只用于后续回看。"
            : action === "restore"
                ? "用户确认恢复为活跃纸面跟踪样本。"
                : "用户确认继续按原始研究假设纸面验证。";
    if (!symbol) {
        if (note) note.textContent = "纸面池动作失败：缺少股票代码。";
        return;
    }
    button.disabled = true;
    button.textContent = "处理中...";
    if (note) note.textContent = `正在执行：${safeText(actionLabels[action] || action)}。`;
    try {
        const payload = await postJson("/api/paper-simulation/action", {
            symbol,
            watchlist_id: watchlistId,
            action,
            note: noteText,
        });
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message || "纸面池状态已更新。");
        agentStatusLoaded = false;
        await Promise.all([loadPaperWatchlist(true), loadRiskNotifications(), loadStrategyScorecard()]);
    } catch (error) {
        if (note) note.textContent = `纸面池动作失败：${escapeHtml(errorText(error))}`;
    } finally {
        button.disabled = false;
        button.textContent = original;
    }
}

async function refreshPaperWatchlistTracking() {
    const button = document.getElementById("refreshPaperWatchlistButton");
    const note = document.getElementById("paperWatchlistNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "刷新中..."; }
    if (note) note.textContent = "正在用本地完整日线刷新纸面模拟池；不会连接券商或下单。";
    try {
        const payload = await postJson("/api/paper-simulation/refresh", {});
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui || "纸面模拟池已刷新。");
        agentStatusLoaded = false;
        await loadPaperWatchlist(true);
        await loadRiskNotifications();
    } catch (error) {
        if (note) note.textContent = `纸面模拟池刷新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

async function syncPaperWatchlistCandidates() {
    const button = document.getElementById("syncPaperWatchlistButton");
    const note = document.getElementById("paperWatchlistNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "同步中..."; }
    if (note) note.textContent = "正在把今日重点同步到纸面模拟池；历史观察默认不自动进入。";
    try {
        const payload = await postJson("/api/paper-simulation/sync-candidates", {
            include_observation: false,
            max_candidates: 10,
        });
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui || "今日重点已同步到纸面模拟池。");
        agentStatusLoaded = false;
        await loadPaperWatchlist(true);
        await loadRiskNotifications();
    } catch (error) {
        if (note) note.textContent = `同步纸面模拟池失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

function setupPerformance() {
    const rebuild = document.getElementById("rebuildPerformanceButton");
    const refreshPaper = document.getElementById("refreshPaperSimulationButton");
    const syncPaper = document.getElementById("syncPaperCandidatesButton");
    if (rebuild) rebuild.addEventListener("click", rebuildPerformance);
    if (refreshPaper) refreshPaper.addEventListener("click", refreshPaperSimulation);
    if (syncPaper) syncPaper.addEventListener("click", syncCandidatesToPaperSimulation);
}

async function loadPerformance() {
    const note = document.getElementById("performanceNote");
    const panel = document.getElementById("performancePanel");
    if (note) note.textContent = "正在读取绩效样本…";
    try {
        renderPerformance(await requestJson("/api/performance/summary"));
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`绩效复盘加载失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "绩效复盘加载失败。";
    }
}

async function loadStrategyScorecard() {
    const isCurrent = beginReadRender("scorecard");
    const note = document.getElementById("performanceNote");
    const panel = document.getElementById("performancePanel");
    if (note) note.textContent = "正在读取策略成绩单…";
    try {
        const data = await requestJson("/api/strategy-scorecard");
        if (!isCurrent()) return;
        renderStrategyScorecard(data);
    } catch (error) {
        if (!isCurrent()) return;
        if (panel) panel.innerHTML = missingBlock(`策略成绩单加载失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "策略成绩单加载失败，可进入策略成绩页面重试。";
        renderHomePrevBatch(null);
    }
}

// --- Stage 1: Market Brain / Strategy Brain shadow (read-only display) ---
// Backend computes everything; this layer only renders artifacts served by
// /api/market-brain/latest and /api/strategy-brain/shadow-latest.

const MARKET_BRAIN_REGIME_LABELS = {
    BULL: "牛市倾向",
    BEAR: "熊市倾向",
    SIDEWAYS: "震荡",
    CRASH: "急跌风险",
    RECOVERY: "修复观察",
    UNKNOWN: "证据不足",
};

async function loadMarketBrainHome() {
    try {
        renderHomeKpiMarketBrain(await requestJson("/api/market-brain/latest"));
    } catch (error) {
        renderHomeKpiMarketBrain(null, error);
    }
}

function renderHomeKpiMarketBrain(data, error = null) {
    const valueEl = document.getElementById("kpiMarketBrainValue");
    const subEl = document.getElementById("kpiMarketBrainSub");
    if (!valueEl || !subEl) return;
    if (error || !data || data.ok === false) {
        valueEl.textContent = "—";
        subEl.textContent = safeText((data && data.message_for_ui) || "市场状态快照缺失；运行每日工作流后生成。");
        feedDutyLamp("brain", "缺失", "快照未生成", "warn");
        return;
    }
    const snapshot = data.snapshot || {};
    const regime = safeText(snapshot.formal_regime || snapshot.regime || "UNKNOWN");
    const label = MARKET_BRAIN_REGIME_LABELS[regime] || regime;
    const eligible = snapshot.evidence_eligible === true;
    const confidence = numericValue(snapshot.confidence);
    valueEl.textContent = label;
    const freshness = data.freshness || {};
    const sourceDate = marketBusinessDate(freshness.source_date) || marketBusinessDate(snapshot.as_of);
    const targetDate = marketBusinessDate(freshness.target_date) || marketBusinessDate(snapshot.target_date);
    if (freshness.status !== "current" || !sourceDate) {
        const stateLabel = freshness.status === "stale" ? "历史判断已滞后" : freshness.status === "future" ? "日期晚于目标日" : "新鲜度未核实";
        subEl.textContent = `${stateLabel} · 源日期 ${sourceDate || "未知"} · 目标日 ${targetDate || "未知"} · 纸面 · 未验证`;
        feedDutyLamp("brain", stateLabel, `源 ${sourceDate || "未知"} / 目标 ${targetDate || "未知"}`, "warn");
    } else if (eligible && confidence !== null && Number.isFinite(confidence)) {
        subEl.textContent = `置信度 ${(confidence * 100).toFixed(0)}% · 研究观察 · 资料日 ${sourceDate} · 纸面 · 未验证`;
        feedDutyLamp("brain", label, `资料日 ${sourceDate} · 置信度 ${(confidence * 100).toFixed(0)}%`, "");
    } else {
        const reasons = Array.isArray(snapshot.reason_codes) ? snapshot.reason_codes.slice(0, 2).join(", ") : "";
        subEl.textContent = `证据不足，仅诊断展示${reasons ? `：${safeText(reasons)}` : ""} · 资料日 ${sourceDate} · 纸面 · 未验证`;
        feedDutyLamp("brain", label, "证据不足", "warn");
    }
}

async function loadStrategyBrainShadow() {
    const panel = document.getElementById("strategyBrainShadowPanel");
    if (!panel) return;
    panel.innerHTML = `<div class="empty-state">正在加载 Strategy Brain 影子选择快照…</div>`;
    try {
        renderStrategyBrainShadow(await requestJson("/api/strategy-brain/shadow-latest"));
    } catch (error) {
        panel.innerHTML = missingBlock(`影子选择快照加载失败：${escapeHtml(errorText(error))}`);
    }
}

function renderStrategyBrainShadow(data) {
    const panel = document.getElementById("strategyBrainShadowPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "影子选择快照尚未生成；运行每日工作流后可见。"));
        return;
    }
    const snapshot = data.snapshot || {};
    const decisions = Array.isArray(snapshot.candidate_decisions) ? snapshot.candidate_decisions : [];
    const eligibleCount = Number(snapshot.eligible_candidate_count || 0);
    const totalCount = Number(snapshot.candidate_count || decisions.length || 0);
    const rows = decisions.slice(0, 20).map((decision) => {
        const legacy = decision.legacy_recommendation || {};
        const eligible = decision.evidence_eligible === true;
        const statusText = eligible ? "影子拟合" : "排除待研究";
        const strategy = safeText(decision.canonical_strategy_id || legacy.strategy || "—");
        const reasons = Array.isArray(decision.reason_codes) ? decision.reason_codes.slice(0, 3).join(", ") : "";
        return `<tr>
            <td>${escapeHtml(safeText(decision.symbol || "—"))}</td>
            <td>${escapeHtml(statusText)}</td>
            <td>${escapeHtml(strategy)}</td>
            <td class="wrap-text">${escapeHtml(safeText(reasons || "—"))}</td>
        </tr>`;
    }).join("");
    panel.innerHTML = `
        <div class="metric-row">
            <div class="metric-box"><span class="metric-label">影子候选</span><span class="metric-value">${escapeHtml(String(totalCount))}</span></div>
            <div class="metric-box"><span class="metric-label">证据合格</span><span class="metric-value">${escapeHtml(String(eligibleCount))}</span></div>
            <div class="metric-box"><span class="metric-label">模式</span><span class="metric-value">shadow</span></div>
        </div>
        <p class="section-note">Strategy Brain 影子选择：仅展示"若按市况选择会怎样"，不改变实际候选、纸面池与账本。证据不足的候选如实标注排除原因。</p>
        ${rows ? `<table class="data-table"><thead><tr><th>标的</th><th>影子状态</th><th>策略</th><th>原因</th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="empty-state">今日无影子候选（候选来源缺失或证据不足）。</div>`}
        <p class="panel-disclaimer">Research Only · Paper Account Only · No Broker Route · 不构成投资建议</p>`;
}

function renderStrategyScorecard(data) {
    try {
        renderHomePrevBatch(data);
    } catch (error) {
        console.warn("[home-dashboard] prev-batch render failed", error);
    }
    const note = document.getElementById("performanceNote");
    const panel = document.getElementById("performancePanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "策略成绩单暂时不可用。"));
        return;
    }
    const fitCounts = data.paper_expectation_fit_counts || {};
    const strategies = Array.isArray(data.by_strategy) ? data.by_strategy : [];
    const reviewNeeded = Array.isArray(data.review_needed) ? data.review_needed : [];
    const researchDecisions = Array.isArray(data.paper_research_decisions) ? data.paper_research_decisions : [];
    const todayFocus = Array.isArray(data.today_focus) ? data.today_focus : [];
    const dataGap = data.data_gap_summary || {};
    const previous = data.previous_recommendation_performance || {};
    const previousItems = Array.isArray(previous.items) ? previous.items : [];
    const marketContext = data.market_context || {};
    const market = marketContext.background || {};
    const marketIndex = market.index || {};
    const marketBreadth = market.breadth || {};
    const marketGainers = Array.isArray(market.sector_top_gainers) ? market.sector_top_gainers : [];
    const marketLosers = Array.isArray(market.sector_top_losers) ? market.sector_top_losers : [];
    const marketNews = market.news || {};
    const newsAge = numericValue(marketNews.age_days);
    const sectorTag = (item, tone) => `<span class="tag ${tone}">${escapeHtml(safeText(item.name))} ${formatChange(item.change)}</span>`;
    const marketHtml = market.available ? `
        <div class="section-note" style="margin-bottom:8px">${highlightText(market.sentiment || "市场情绪数据缺失。")}</div>
        <div class="metric-row">
            <div class="metric-box"><span>沪深300</span><strong class="${changeClass(marketIndex.hs300 && marketIndex.hs300.change)}">${formatChange(marketIndex.hs300 && marketIndex.hs300.change)}</strong></div>
            <div class="metric-box"><span>上证指数</span><strong class="${changeClass(marketIndex.sh_index && marketIndex.sh_index.change)}">${formatChange(marketIndex.sh_index && marketIndex.sh_index.change)}</strong></div>
            <div class="metric-box"><span>上涨 / 下跌</span><strong>${escapeHtml(safeText(marketBreadth.advancing_count))} / ${escapeHtml(safeText(marketBreadth.declining_count))}</strong></div>
            <div class="metric-box"><span>样本成交额</span><strong>${escapeHtml(safeText(marketBreadth.total_amount))}</strong></div>
            <div class="metric-box"><span>新闻缓存</span><strong>${escapeHtml(safeText(marketNews.latest_date))}${newsAge === null ? "" : `（${newsAge}天前）`}</strong></div>
        </div>
        <div class="tag-row">${marketGainers.map((item) => sectorTag(item, "tag-market-up")).join("") || `<span class="tag">板块涨幅数据缺失</span>`}</div>
        <div class="tag-row">${marketLosers.map((item) => sectorTag(item, "tag-market-down")).join("") || `<span class="tag">板块跌幅数据缺失</span>`}</div>
    ` : missingBlock(safeText(market.hint || marketContext.news_and_sector_note || "市场/板块/新闻背景暂不可用。"));
    const fitTags = Object.entries(fitCounts).map(([key, value]) => `
        <span class="tag">${escapeHtml(key)}: ${escapeHtml(safeText(value))}</span>
    `).join("");
    const strategyHtml = strategies.map((item) => {
        const examples = Array.isArray(item.examples) ? item.examples : [];
        return `
            <div class="perf-card">
                <h4>${escapeHtml(safeText(item.strategy))}<span class="health-badge ${item.scorecard_label === "需要降权/复盘" ? "health-warn" : "health-ok"}">${escapeHtml(safeText(item.scorecard_label || "继续观察"))}</span></h4>
                <div class="metric-row" style="margin:0">
                    <div class="metric-box"><span>样本</span><strong>${escapeHtml(safeText(item.sample_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>符合预期</span><strong>${escapeHtml(safeText(item.fit_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>偏离预期</span><strong>${escapeHtml(safeText(item.deviated_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>等待样本</span><strong>${escapeHtml(safeText(item.pending_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>符合率</span><strong>${item.fit_rate === null || item.fit_rate === undefined ? "数据缺失" : `${escapeHtml(safeText(item.fit_rate))}%`}</strong></div>
                    <div class="metric-box"><span>平均变化</span><strong class="${changeClass(item.avg_return_since_added_pct)}">${formatSignedPercent(item.avg_return_since_added_pct)}</strong></div>
                    <div class="metric-box"><span>平均回撤</span><strong class="${changeClass(item.avg_max_drawdown_since_added_pct)}">${sanePercentHTML(item.avg_max_drawdown_since_added_pct, { mode: "points", digits: 2 })}</strong></div>
                    <div class="metric-box"><span>需复盘</span><strong>${escapeHtml(safeText(item.needs_review_count ?? 0))}</strong></div>
                </div>
                <div class="tag-row">
                    ${examples.map((example) => `<span class="tag tag-watch">${escapeHtml(safeText(example.name || example.symbol))} · ${escapeHtml(safeText(example.expectation_fit_label || "等待"))} · ${escapeHtml(safeText(example.decision_status || "研究判断缺失"))}</span>`).join("") || `<span class="tag">暂无样本示例</span>`}
                </div>
                <div class="tag-row">
                    ${Object.entries(item.decision_status_counts || {}).map(([key, value]) => `<span class="tag">${escapeHtml(key)}: ${escapeHtml(safeText(value))}</span>`).join("") || `<span class="tag">暂无研究决策分布</span>`}
                </div>
            </div>
        `;
    }).join("");
    const decisionHtml = researchDecisions.slice(0, 10).map((item) => {
        const toneClassName = item.alignment_tone === "warn" ? "health-warn" : item.alignment_tone === "missing" ? "health-warn" : "health-ok";
        return `
        <div class="perf-card">
            <h4>${escapeHtml(safeText(item.name || item.symbol))}<span class="health-badge ${toneClassName}">${escapeHtml(safeText(item.alignment_label || "等待验证"))}</span></h4>
            <div class="metric-row" style="margin:0">
                <div class="metric-box"><span>代码</span><strong>${escapeHtml(safeText(item.symbol))}</strong></div>
                <div class="metric-box"><span>当初判断</span><strong>${escapeHtml(safeText(item.decision_status || "待讨论"))}</strong></div>
                <div class="metric-box"><span>主动作</span><strong>${escapeHtml(safeText(item.primary_action || "观察"))}</strong></div>
                <div class="metric-box"><span>置信度</span><strong>${escapeHtml(safeText(item.confidence || "数据缺失"))}</strong></div>
                <div class="metric-box"><span>验证周期</span><strong>${escapeHtml(safeText(item.validation_days || "数据缺失"))} 天</strong></div>
                <div class="metric-box"><span>当前变化</span><strong class="${changeClass(item.return_since_added_pct)}">${formatSignedPercent(item.return_since_added_pct)}</strong></div>
                <div class="metric-box"><span>最大回撤</span><strong class="${changeClass(item.max_drawdown_since_added)}">${sanePercentHTML(item.max_drawdown_since_added, { mode: "points", digits: 2 })}</strong></div>
            </div>
            <div class="key-review-panel compact">
                ${keyReviewRow("当初一句话", item.review_sentence || item.llm_research_opinion_excerpt, "reason")}
                ${keyReviewRow("成功条件", item.success_condition, "status")}
                ${keyReviewRow("失败条件", item.failure_condition, item.alignment_tone === "warn" ? "risk" : "reason")}
                ${keyReviewRow("当前兑现", item.expectation_fit_reason || item.expectation_fit_label, item.alignment_tone === "warn" ? "risk" : "status")}
                ${keyReviewRow("缺失数据", item.missing_data || "暂无关键缺口", item.missing_data && item.missing_data !== "暂无关键缺口" ? "risk" : "data")}
            </div>
        </div>
        `;
    }).join("");
    const reviewHtml = reviewNeeded.slice(0, 8).map((item) => `
        <div class="perf-card">
            <h4>${escapeHtml(safeText(item.name || item.symbol))}<span class="health-badge health-warn">${escapeHtml(safeText(item.expectation_fit_label || item.review_label || "待复盘"))}</span></h4>
            <div class="metric-row" style="margin:0">
                <div class="metric-box"><span>代码</span><strong>${escapeHtml(safeText(item.symbol))}</strong></div>
                <div class="metric-box"><span>主动作</span><strong>${escapeHtml(safeText(item.primary_action || "观察"))}</strong></div>
                <div class="metric-box"><span>入池变化</span><strong class="${changeClass(item.return_since_added_pct)}">${formatSignedPercent(item.return_since_added_pct)}</strong></div>
                <div class="metric-box"><span>最大回撤</span><strong class="${changeClass(item.max_drawdown_since_added)}">${sanePercentHTML(item.max_drawdown_since_added, { mode: "points", digits: 2 })}</strong></div>
            </div>
            <p>${highlightText(item.expectation_fit_reason || "需要复盘偏离原因。")}</p>
            ${item.research_decision ? `<div class="tag-row"><span class="tag tag-watch">当初判断：${escapeHtml(safeText(item.research_decision.decision_status || "数据缺失"))}</span><span class="tag tag-watch">失败条件：${escapeHtml(safeText(item.research_decision.failure_condition || "数据缺失"))}</span></div>` : ""}
        </div>
    `).join("");
    const todayHtml = todayFocus.slice(0, 10).map((item) => `
        <span class="tag tag-watch">#${escapeHtml(safeText(item.priority_rank || "-"))} ${escapeHtml(safeText(item.name || item.symbol))} · ${escapeHtml(safeText(item.strategy_lane || "观察"))}</span>
    `).join("");
    const previousStatusTags = Object.entries(previous.status_counts || {}).map(([key, value]) => `
        <span class="tag">${escapeHtml(key)}: ${escapeHtml(safeText(value))}</span>
    `).join("");
    const previousItemsHtml = previousItems.slice(0, 8).map((item) => `
        <div class="perf-card">
            <h4>${escapeHtml(safeText(item.name || item.symbol))}<span class="health-badge ${paperWatchlistStatusClass(`${item.status_tone || ""} ${item.status_label || ""}`)}">${escapeHtml(safeText(item.status_label || "等待复盘"))}</span></h4>
            <div class="metric-row" style="margin:0">
                <div class="metric-box"><span>代码</span><strong>${escapeHtml(safeText(item.symbol))}</strong></div>
                <div class="metric-box"><span>策略分层</span><strong>${escapeHtml(safeText(item.strategy_lane || "观察"))}</strong></div>
                <div class="metric-box"><span>跟踪天数</span><strong>${escapeHtml(safeText(item.elapsed_trading_days ?? 0))}</strong></div>
                <div class="metric-box"><span>入选后变化</span><strong class="${changeClass(item.return_since_select)}">${formatSignedPercent(item.return_since_select)}</strong></div>
                <div class="metric-box"><span>最大回撤</span><strong class="${changeClass(item.max_drawdown_since_select)}">${sanePercentHTML(item.max_drawdown_since_select, { mode: "points", digits: 2 })}</strong></div>
                <div class="metric-box"><span>数据状态</span><strong>${escapeHtml(safeText(item.data_status || "数据缺失"))}</strong></div>
            </div>
            <p>${highlightText(item.review_note || item.expected_path || "等待本地日线继续验证。")}</p>
        </div>
    `).join("");
    const previousHtml = previous.available ? `
        <div class="metric-row">
            <div class="metric-box"><span>上一批日期</span><strong>${escapeHtml(safeText(previous.batch_date))}</strong></div>
            <div class="metric-box"><span>样本</span><strong>${escapeHtml(safeText(previous.sample_count ?? previousItems.length))}</strong></div>
            <div class="metric-box"><span>符合</span><strong>${escapeHtml(safeText(previous.fit_count ?? 0))}</strong></div>
            <div class="metric-box"><span>偏离/缺口</span><strong>${escapeHtml(safeText(previous.deviated_or_gap_count ?? 0))}</strong></div>
            <div class="metric-box"><span>平均变化</span><strong class="${changeClass(previous.avg_return_since_select_pct)}">${formatSignedPercent(previous.avg_return_since_select_pct)}</strong></div>
            <div class="metric-box"><span>平均回撤</span><strong class="${changeClass(previous.avg_max_drawdown_since_select_pct)}">${sanePercentHTML(previous.avg_max_drawdown_since_select_pct, { mode: "points", digits: 2 })}</strong></div>
        </div>
        <div class="tag-row">${previousStatusTags || `<span class="tag">暂无上一批状态分布</span>`}</div>
        <div class="perf-strategy-grid">${previousItemsHtml || missingBlock("上一批候选暂无可展示样本。")}</div>
    ` : missingBlock(previous.message_for_ui || "上一批候选表现暂时不可用。");
    const conclusions = data.strategy_conclusions || {};
    const verdictLines = Array.isArray(conclusions.verdict_lines) ? conclusions.verdict_lines : [];
    const conclusionsHtml = (conclusions.conclusion_text || verdictLines.length) ? `
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>策略复盘结论</strong>
            <div class="section-note">${highlightText(safeText(conclusions.conclusion_text || "暂无策略结论。"))}</div>
        </div>
        <div class="key-review-panel compact">
            ${verdictLines.map((line) => `<p>${highlightText(safeText(line))}</p>`).join("") || `<p>暂无逐策略复盘。</p>`}
        </div>
    ` : "";
    const effectiveness = data.strategy_effectiveness || {};
    const effStrategies = Array.isArray(effectiveness.strategies) ? effectiveness.strategies : [];
    const fmtPct01 = (v) => (v === null || v === undefined || isNaN(v)) ? "数据缺失" : `${(v * 100).toFixed(0)}%`;
    const effHtml = effStrategies.length ? `
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>研究排序权重（不代表已验证有效）</strong>
            <div class="section-note">纸面研究 · 未验证。后端按已有回测与纸面统计生成研究排序权重（有界 0.6–1.4）。分数和权重只表示研究排序，不能解释为未来盈利概率；不同规则、演示和小样本不能为当前候选背书。</div>
        </div>
        <div class="perf-strategy-grid">
            ${effStrategies.map((s) => {
                const w = numericValue(s.weight);
                const tone = w > 1.02 ? "health-ok" : (w < 0.98 ? "health-warn" : "");
                return `
                <div class="perf-card">
                    <h4>${escapeHtml(safeText(s.bucket || s.strategy))}<span class="health-badge ${tone}">权重 ${escapeHtml(safeText(s.weight))} ${escapeHtml(safeText(s.weight_direction || ""))}</span></h4>
                    <div class="metric-row" style="margin:0">
                        <div class="metric-box"><span>纸面样本</span><strong>${escapeHtml(safeText(s.sample_count ?? 0))}</strong></div>
                        <div class="metric-box"><span>纸面符合率</span><strong>${s.paper_fit_rate === null || s.paper_fit_rate === undefined ? "数据缺失" : `${escapeHtml(safeText(s.paper_fit_rate))}%`}</strong></div>
                        <div class="metric-box"><span>平均纸面</span><strong class="${changeClass(s.paper_avg_return_pct)}">${formatSignedPercent(s.paper_avg_return_pct)}</strong></div>
                        <div class="metric-box"><span>回测盈利占比</span><strong>${escapeHtml(fmtPct01(s.backtest_win_rate))}</strong></div>
                        <div class="metric-box"><span>回测夏普</span><strong>${escapeHtml(safeText(s.backtest_sharpe ?? "数据缺失"))}</strong></div>
                        <div class="metric-box"><span>样本置信</span><strong>${escapeHtml(safeText(s.confidence))}</strong></div>
                    </div>
                    <p>${highlightText(safeText(s.confidence_label))}</p>
                    <p>${highlightText(safeText(s.explanation))}</p>
                </div>`;
            }).join("")}
        </div>
    ` : "";
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>策略成绩单 <span class="health-badge ${reviewNeeded.length ? "health-warn" : "health-ok"}">${escapeHtml(safeText(data.status || "passed"))}</span></strong>
            <div class="section-note">${escapeHtml(safeText(data.scorecard_message || "本地策略成绩单。"))}</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>今日重点</span><strong>${escapeHtml(safeText(data.today_focus_count ?? 0))}</strong></div>
            <div class="metric-box"><span>纸面模拟池</span><strong>${escapeHtml(safeText(data.paper_pool_count ?? 0))}</strong></div>
            <div class="metric-box"><span>需降级/复盘（原始预期未兑现口径）</span><strong>${escapeHtml(safeText(data.review_needed_count ?? 0))}</strong></div>
            <div class="metric-box"><span>数据缺口</span><strong>${escapeHtml(safeText(dataGap.gap_count ?? 0))}</strong></div>
            <div class="metric-box"><span>更新时间</span><strong>${escapeHtml(safeText(data.generated_at || "数据缺失"))}</strong></div>
            <div class="metric-box"><span>边界</span><strong>Paper Only</strong></div>
        </div>
        <div class="tag-row">${fitTags || `<span class="tag">暂无预期兑现分布</span>`}</div>
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>市场 / 板块 / 新闻背景</strong>
            <div class="section-note">本地快照与缓存的市场情绪、板块强弱、新闻新鲜度；缺失时降级显示，不伪造、不预测。</div>
        </div>
        ${marketHtml}
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>今日重点 Top 3-10</strong>
            <div class="section-note">进入详情可看 K 线、MACD/KDJ/RSI、入选理由、新闻/板块线索和情景推演。</div>
        </div>
        <div class="tag-row">${todayHtml || `<span class="tag">暂无今日重点</span>`}</div>
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>上一批候选表现</strong>
            <div class="section-note">${escapeHtml(safeText(previous.message_for_ui || "对照上一批候选的入选后表现，检验原始策略假设是否有效。"))}</div>
        </div>
        ${previousHtml}
        ${conclusionsHtml}
        ${effHtml}
        <div class="quant-summary-head" style="margin-top:14px"><strong>分策略表现</strong></div>
        <div class="perf-strategy-grid">${strategyHtml || missingBlock("暂无纸面模拟池样本；先从今日重点或 LLM 研究室确认加入。")}</div>
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>纸面研究决策复盘</strong>
            <div class="section-note">对照当初 LLM/本地研究判断、验证周期、成功/失败条件和当前纸面表现。</div>
        </div>
        <div class="perf-strategy-grid">${decisionHtml || missingBlock("暂无可复盘的纸面研究决策。")}</div>
        <div class="quant-summary-head" style="margin-top:14px"><strong>降级 / 复盘清单</strong></div>
        <div class="perf-strategy-grid">${reviewHtml || missingBlock("暂无需要降级复盘的纸面样本。")}</div>
        <div class="quant-summary-head" style="margin-top:14px"><strong>明日优先观察</strong></div>
        <div class="tag-row">${todayHtml || `<span class="tag">等待今日重点生成</span>`}</div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route / Live Execution Locked / QMT Order Route Locked / No Real Orders / Not Investment Advice / Not Position Advice.</p>
    `;
    if (note) note.textContent = `策略成绩单：今日重点 ${safeText(data.today_focus_count ?? 0)} 只，纸面池 ${safeText(data.paper_pool_count ?? 0)} 只，需复盘 ${safeText(data.review_needed_count ?? 0)} 只。`;
}

function strategyLabel(value) {
    const map = { da_ban: "打板", board: "打板", long: "长期", observe: "观察" };
    return map[safeText(value)] || safeText(value);
}

function renderPaperWatchlistSummary(watchlist) {
    const data = watchlist || {};
    const items = Array.isArray(data.items) ? data.items : [];
    const count = safeText(data.watchlist_count ?? items.length ?? 0);
    const summary = data.paper_tracking_summary || data.tracking_summary || {};
    const preview = items.slice(0, 6).map((item) => {
        const ret = numericValue(item.return_since_added_pct);
        const retHtml = ret === null ? "" : `<strong class="${changeClass(ret)}">${formatSignedPercent(ret)}</strong>`;
        const label = item.review_label || item.review_status || "纸面跟踪中";
        const stopStatus = item.smart_stop && item.smart_stop.trigger_status ? ` · 风险线${safeText(item.smart_stop.trigger_status)}` : "";
        return `
            <span class="tag tag-watch">${escapeHtml(safeText(item.name || item.symbol))} ${escapeHtml(safeText(item.symbol || ""))}${retHtml ? ` · ${retHtml}` : ""} · ${escapeHtml(safeText(label))}${escapeHtml(stopStatus)}</span>
        `;
    }).join("");
    const summaryLine = summary.message
        ? `<p class="panel-disclaimer" style="margin-top:8px">${escapeHtml(safeText(summary.message))}${summary.updated_at ? ` · ${escapeHtml(safeText(summary.updated_at))}` : ""}</p>`
        : "";
    const evidence = data.research_evidence || {};
    const backtest = evidence.backtest_summary || {};
    const factor = evidence.factor_health || {};
    const lab = evidence.strategy_lab || {};
    const factorSummary = factor.summary || {};
    const labTop = Array.isArray(lab.top_strategies) ? lab.top_strategies : [];
    const labTopHtml = labTop.slice(0, 3).map((item) => {
        const rank = safeText(item.research_rank || "-");
        const strategy = safeText(item.strategy_id || "strategy_lab");
        const health = humanStatus(item.strategy_health || item.research_readiness || "research_watch");
        const candidates = safeText(item.candidate_count ?? "-");
        const signals = safeText(item.signal_count ?? "-");
        const coverage = numericValue(item.mapping_coverage);
        const coverageText = coverage === null ? "" : ` · 映射${(coverage * 100).toFixed(1)}%`;
        return `<span class="tag tag-watch">#${escapeHtml(rank)} ${escapeHtml(strategyLabLabel(strategy))} · ${escapeHtml(health)} · 候选${escapeHtml(candidates)} / 信号${escapeHtml(signals)}${escapeHtml(coverageText)}</span>`;
    }).join("");
    const labHtml = lab.available ? `
        <div class="quant-summary-head" style="margin-top:12px">
            <strong>策略实验室快照</strong>
            <div class="section-note">${escapeHtml(humanizeStatusTerms(lab.handoff_status || lab.status || "research snapshot"))}${lab.generated_at ? ` · ${escapeHtml(safeText(lab.generated_at))}` : ""}</div>
        </div>
        <div class="tag-row">${labTopHtml || `<span class="tag">策略实验室暂无可展示排行</span>`}</div>
        <p class="panel-disclaimer">策略实验室只作为纸面复盘证据展示；不自动改评分、不接券商、不下真实订单。</p>
    ` : "";
    const backtestMetrics = backtest.metrics || {};
    const backtestReturn = numericValue(backtestMetrics.total_return);
    const backtestReturnText = backtestReturn === null ? "数据缺失" : `${(backtestReturn * 100).toFixed(2)}%`;
    const evidenceHtml = (backtest.available || factor.available || lab.available) ? `
        <div class="quant-summary-head" style="margin-top:12px">
            <strong>纸面池研究证据</strong>
            <div class="section-note">只读证据：用于复盘候选逻辑，不代表未来收益，也不构成买卖或真实配置指令。</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>回测来源</span><strong>${escapeHtml(humanizeStatusTerms(backtest.scope_source || backtest.status || "缺失"))}</strong></div>
            <div class="metric-box"><span>回测策略</span><strong>${escapeHtml(safeText(backtest.strategy || "缺失"))}</strong></div>
            <div class="metric-box"><span>交易数</span><strong>${escapeHtml(safeText(backtest.trade_count ?? "缺失"))}</strong></div>
            <div class="metric-box"><span>回测收益</span><strong>${escapeHtml(backtestReturnText)}</strong></div>
            <div class="metric-box"><span>可用因子</span><strong>${escapeHtml(safeText(factorSummary.usable_count ?? "缺失"))}</strong></div>
            <div class="metric-box"><span>因子用途</span><strong>${factor.used_for_scoring ? "评分已接入" : "仅作证据"}</strong></div>
        </div>
        ${labHtml}
    ` : "";
    return `
        <div class="quant-summary-head" style="margin-top:14px">
            <strong>纸面模拟池（确认/同步 / 本地跟踪）</strong>
            <div class="section-note">${escapeHtml(safeText(data.message || data.message_for_ui || "候选加入后会在这里形成纸面观察清单；不接券商、不下真实订单。"))}</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>模拟池数量</span><strong>${escapeHtml(count)}</strong></div>
            <div class="metric-box"><span>来源</span><strong>候选池确认/同步</strong></div>
            <div class="metric-box"><span>边界</span><strong>Paper Only</strong></div>
        </div>
        <div class="tag-row">${preview || `<span class="tag">暂无纸面模拟样本</span>`}</div>
        ${summaryLine}
        ${evidenceHtml}
    `;
}

function renderPerformance(data) {
    const note = document.getElementById("performanceNote");
    const panel = document.getElementById("performancePanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "绩效复盘暂时不可用。"));
        return;
    }
    const auditEvidence = data.evidence_freshness || {};
    const auditWarningHtml = auditEvidence.metrics_trustworthy === false
        ? missingBlock(safeText(auditEvidence.message_for_ui || data.message_for_ui || "审计证据不足，当前绩效指标不可信。"))
        : "";
    const paperWatchlistHtml = renderPaperWatchlistSummary(data.paper_watchlist || {});
    if (!data.sample_count) {
        panel.innerHTML = `
            ${auditWarningHtml}
            ${missingBlock("暂无绩效样本，请点击“重建绩效样本”。")}
            ${paperWatchlistHtml}
        `;
        if (note) note.textContent = "暂无绩效样本。";
        return;
    }
    const o = data.overall || {};
    const hitMaturity = Number.isFinite(Number(o.hit_rate_sample_count))
        ? ` (${safeText(o.hit_rate_sample_count)}/${safeText(data.sample_count)} 已到期)`
        : "";
    const return20dText = o.forward_return_20d_status === "insufficient_sample"
        ? `样本不足 (${safeText(o.sample_count_20d || 0)}/20)`
        : pctText(o.forward_return_20d_mean);
    const overallHtml = `
        <div class="metric-row">
            <div class="metric-box"><span>样本数</span><strong>${safeText(data.sample_count)}</strong></div>
            <div class="metric-box"><span>命中率(hit)</span><strong>${pctText(o.hit_rate)}${hitMaturity}</strong></div>
            <div class="metric-box"><span>5日平均收益</span><strong>${pctText(o.forward_return_5d_mean)}</strong></div>
            <div class="metric-box"><span>20日平均收益</span><strong>${return20dText}</strong></div>
            <div class="metric-box"><span>正收益占比</span><strong>${pctText(o.positive_sample_ratio)}</strong></div>
            <div class="metric-box"><span>中位数收益</span><strong>${pctText(o.median_return)}</strong></div>
            <div class="metric-box"><span>最大回撤</span><strong>${sanePercentHTML(o.max_drawdown)}</strong></div>
            <div class="metric-box"><span>Sortino</span><strong>${numText(o.sortino_ratio)}</strong></div>
            <div class="metric-box"><span>Calmar</span><strong>${numText(o.calmar_ratio)}</strong></div>
        </div>`;
    const groups = Array.isArray(data.by_strategy) ? data.by_strategy : [];
    const groupHtml = groups.map((g) => {
        const hm = g.hold_means || {};
        const groupHitMaturity = Number.isFinite(Number(g.hit_rate_sample_count))
            ? ` (${safeText(g.hit_rate_sample_count)}/${safeText(g.sample_count)} 已到期)`
            : "";
        const group20dText = g.forward_return_20d_status === "insufficient_sample"
            ? `样本不足 (${safeText(g.sample_count_20d || 0)}/20)`
            : pctText(hm.hold_20d_ret);
        return `
            <div class="perf-card">
                <h4>${escapeHtml(strategyLabel(g.strategy_type))}<span class="health-badge health-ok">${safeText(g.sample_count)} 样本</span></h4>
                <div class="metric-row" style="margin:0">
                    <div class="metric-box"><span>命中率</span><strong>${pctText(g.hit_rate)}${groupHitMaturity}</strong></div>
                    <div class="metric-box"><span>1日</span><strong>${pctText(hm.hold_1d_ret)}</strong></div>
                    <div class="metric-box"><span>5日</span><strong>${pctText(hm.hold_5d_ret)}</strong></div>
                    <div class="metric-box"><span>10日</span><strong>${pctText(hm.hold_10d_ret)}</strong></div>
                    <div class="metric-box"><span>20日</span><strong>${group20dText}</strong></div>
                    <div class="metric-box"><span>最大回撤</span><strong>${sanePercentHTML(g.max_drawdown)}</strong></div>
                </div>
            </div>`;
    }).join("");
    const portfolio = data.paper_portfolio || {};
    const portfolioHtml = portfolio.exists
        ? `<div class="quant-summary-head" style="margin-top:14px"><strong>虚拟组合快照（纸面，不接券商）</strong><div class="section-note">${escapeHtml(safeText(portfolio.message || portfolio.summary || portfolio.broker_boundary || ""))}</div></div>`
        : `<p class="panel-disclaimer">${escapeHtml(safeText(portfolio.message || "尚未初始化自动虚拟组合。"))}</p>`;
    panel.innerHTML = `
        ${auditWarningHtml}
        <div class="quant-summary-head"><strong>选股结果复盘（全部样本）</strong></div>
        ${overallHtml}
        <div class="perf-strategy-grid">${groupHtml || missingBlock("暂无分策略样本。")}</div>
        ${paperWatchlistHtml}
        ${portfolioHtml}
        <p class="panel-disclaimer">${escapeHtml(researchOnlyDisclaimer(data, "历史样本统计，不构成投资建议。"))}</p>`;
    if (note) note.textContent = auditEvidence.metrics_trustworthy === false
        ? safeText(data.message_for_ui || "审计证据不足，当前绩效指标不可信。")
        : `绩效样本：${safeText(data.sample_count)} 条` + (data.excluded_outliers ? `（已剔除异常 ${safeText(data.excluded_outliers)} 条）` : "");
}

async function rebuildPerformance() {
    const button = document.getElementById("rebuildPerformanceButton");
    const note = document.getElementById("performanceNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "重建中…"; }
    if (note) note.textContent = "正在基于账本和本地日线重建绩效样本…";
    try {
        const payload = await postJson("/api/performance/rebuild", {});
        if (note) note.textContent = safeText(payload.message_for_ui || "重建完成。");
        await loadPerformance();
    } catch (error) {
        if (note) note.textContent = `重建失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

async function refreshPaperSimulation() {
    const button = document.getElementById("refreshPaperSimulationButton");
    const note = document.getElementById("performanceNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "刷新中…"; }
    if (note) note.textContent = "正在用本地日线刷新纸面模拟池表现…";
    try {
        const payload = await postJson("/api/paper-simulation/refresh", {});
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui);
        agentStatusLoaded = false;
        await loadPerformance();
    } catch (error) {
        if (note) note.textContent = `纸面模拟池刷新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

async function syncCandidatesToPaperSimulation() {
    const button = document.getElementById("syncPaperCandidatesButton");
    const note = document.getElementById("performanceNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "同步中…"; }
    if (note) note.textContent = "正在按今日候选和策略分层同步到纸面模拟池…";
    try {
        const payload = await postJson("/api/paper-simulation/sync-candidates", {
            include_observation: false,
            max_candidates: 10,
        });
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui);
        agentStatusLoaded = false;
        await loadPerformance();
    } catch (error) {
        if (note) note.textContent = `同步纸面模拟池失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

// ---- 策略实验室证据（只读） ----
function setupStrategyLab() {
    const refresh = document.getElementById("refreshStrategyLabButton");
    if (refresh) refresh.addEventListener("click", () => loadStrategyLab(true));
}

async function loadStrategyLab(force = false) {
    const note = document.getElementById("strategyLabNote");
    const panel = document.getElementById("strategyLabPanel");
    if (note) note.textContent = force ? "正在刷新策略实验室证据..." : "正在读取策略实验室证据...";
    if (panel && force) panel.innerHTML = `<div class="empty-state">正在刷新策略实验室证据...</div>`;
    try {
        renderStrategyLab(await requestJson("/api/strategy-lab/summary"));
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`策略实验室证据读取失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "策略实验室证据读取失败。";
    }
}

function strategyLabLabel(strategyId) {
    const id = safeText(strategyId);
    return {
        limit_up_family: "打板/涨停复核",
        long_term_family: "长期观察",
        low_price_family: "低价弹性",
        sector_rotation_family: "板块轮动",
        sandbox_long: "沙箱长期",
        sandbox_board: "沙箱打板",
        strategy_lab_low_price: "策略实验室-低价",
        strategy_lab_long_term: "策略实验室-长期",
        strategy_lab_limit_up: "策略实验室-打板",
        strategy_lab_sector_rotation: "策略实验室-板块",
    }[id] || id;
}

function percentNoSign(value, digits = 1) {
    const number = numericValue(value);
    return number === null ? safeText(value) : `${(number * 100).toFixed(digits)}%`;
}

function renderStrategyLab(data) {
    const note = document.getElementById("strategyLabNote");
    const panel = document.getElementById("strategyLabPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "策略实验室证据暂不可用。"));
        return;
    }
    const top = Array.isArray(data.top_strategies) ? data.top_strategies : [];
    const warnings = Array.isArray(data.warnings_top) ? data.warnings_top : [];
    const statusTone = data.available === false ? "health-warn" : (warnings.length ? "health-warn" : "health-ok");
    const cards = top.map((item) => {
        const strategyId = safeText(item.strategy_id || "strategy_lab");
        const rank = safeText(item.research_rank || "-");
        const healthRaw = safeText(item.strategy_health || item.research_readiness || "research_watch");
        const readinessRaw = safeText(item.research_readiness || "research_watch");
        const health = humanStatus(healthRaw);
        const readiness = humanStatus(readinessRaw);
        const healthClass = /healthy|ready/i.test(healthRaw + readinessRaw) ? "health-ok" : "health-warn";
        const coverage = numericValue(item.mapping_coverage);
        const coverageHtml = coverage === null ? missingBadge() : escapeHtml(`${(coverage * 100).toFixed(1)}%`);
        return `
            <div class="perf-card">
                <h4>#${escapeHtml(rank)} ${escapeHtml(strategyLabLabel(strategyId))}<span class="health-badge ${healthClass}">${escapeHtml(health)}</span></h4>
                <div class="metric-row" style="margin:0">
                    <div class="metric-box"><span>候选数</span><strong>${escapeHtml(safeText(item.candidate_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>信号数</span><strong>${escapeHtml(safeText(item.signal_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>稳健分</span><strong>${numText(item.robustness_score)}</strong></div>
                    <div class="metric-box"><span>映射覆盖</span><strong>${coverageHtml}</strong></div>
                    <div class="metric-box"><span>年化样本</span><strong>${sanePercentHTML(item.annualized_return)}</strong></div>
                    <div class="metric-box"><span>最大回撤</span><strong>${sanePercentHTML(item.max_drawdown)}</strong></div>
                    <div class="metric-box"><span>夏普</span><strong>${hasValue(item.sharpe) ? escapeHtml(numText(item.sharpe)) : missingBadge()}</strong></div>
                    <div class="metric-box"><span>纸面盈利占比</span><strong>${sanePercentHTML(item.win_rate)}</strong></div>
                </div>
                <div class="tag-row" style="margin-top:10px">
                    <span class="tag tag-watch">${escapeHtml(strategyLabLabel(strategyId))}</span>
                    <span class="tag tag-watch">${escapeHtml(readiness)}</span>
                    <span class="tag tag-watch">${escapeHtml(humanStatus(item.robustness_label || "robustness_watch"))}</span>
                    <span class="tag tag-watch">${escapeHtml(humanStatus(item.data_risk_level || "data_risk_visible"))}</span>
                </div>
                <p class="panel-disclaimer">${escapeHtml(sanitizeTradingTerms(humanizeStatusTerms(item.limitation_note || "研究排行榜仅作证据展示，不构成交易指令。")))}</p>
            </div>
        `;
    }).join("");
    const warningHtml = warnings.length ? `
        <div class="gap-panel">
            ${warnings.map((item) => `
                <div class="key-review-row">
                    <span class="key-review-label risk">告警</span>
                    <span>${highlightText(humanizeStatusTerms(item))}</span>
                </div>
            `).join("")}
        </div>
    ` : "";
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>策略家族排行 <span class="health-badge ${statusTone}">${escapeHtml(humanStatus(data.status || "missing"))}</span></strong>
            <div class="section-note">${escapeHtml(humanizeStatusTerms(data.message_for_ui || "读取本地策略实验室排行榜；仅作研究证据展示。"))}</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>可用</span><strong>${data.available === false ? "否" : "是"}</strong></div>
            <div class="metric-box"><span>排行数量</span><strong>${escapeHtml(safeText(data.top_strategy_count ?? top.length))}</strong></div>
            <div class="metric-box"><span>研究快照状态</span><strong>${escapeHtml(humanStatus(data.handoff_status || "missing"))}</strong></div>
            <div class="metric-box"><span>警告</span><strong>${escapeHtml(safeText(data.warning_count ?? warnings.length))}</strong></div>
            <div class="metric-box"><span>评分用途</span><strong>${data.used_for_scoring ? "已接入" : "证据展示"}</strong></div>
            <div class="metric-box"><span>边界</span><strong>Paper Only</strong></div>
        </div>
        <div class="perf-strategy-grid">${cards || missingBlock(safeText(data.message || "暂无策略实验室排行榜。请先运行策略实验室流程。"))}</div>
        <div class="gap-panel">
            <strong>因子评估 / 组合结果</strong>
            <div>当前策略实验室接口未返回 IC、Rank IC、decay 或因子组合明细；这里保留为前端降级占位。请先运行现有 factor-eval / factor-combine 流程生成本地结果后再展示，不新建后端 API。</div>
        </div>
        ${warningHtml}
        <div class="tag-row">
            <span class="tag research-path-badge">排行榜文件：${escapeHtml(safeText(data.leaderboard_path || "missing"))}</span>
            <span class="tag research-path-badge">证据快照文件：${escapeHtml(safeText(data.handoff_path || "missing"))}</span>
            <span class="tag research-path-badge">统计文件：${escapeHtml(safeText(data.statistics_path || "missing"))}</span>
        </div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route / Live Execution Locked / QMT Order Route Locked / No Real Orders / Not Investment Advice / Not Position Advice.</p>
    `;
    if (note) note.textContent = `策略实验室：${humanStatus(data.status)}，排行 ${safeText(data.top_strategy_count ?? top.length)} 条，警告 ${safeText(data.warning_count ?? warnings.length)} 条。`;
}

// ---- 策略回测 ----
// ---- 风险/通知看板（只读） ----
function setupRiskNotifications() {
    const refresh = document.getElementById("refreshRiskNotificationsButton");
    const refreshPaper = document.getElementById("refreshPaperRiskButton");
    if (refresh) refresh.addEventListener("click", () => loadRiskNotifications(true));
    if (refreshPaper) refreshPaper.addEventListener("click", refreshPaperRiskLines);
}

function setupSmartStop() {
    const refresh = document.getElementById("refreshSmartStopButton");
    if (refresh) refresh.addEventListener("click", refreshSmartStopBoard);
}

function setupNotificationCenter() {
    const refresh = document.getElementById("refreshNotificationCenterButton");
    if (refresh) refresh.addEventListener("click", () => loadNotificationCenter(true));
}

async function loadRiskNotifications(force = false) {
    const note = document.getElementById("riskNotificationsNote");
    const panel = document.getElementById("riskNotificationsPanel");
    if (note) note.textContent = force ? "正在刷新本地风险/通知状态..." : "正在读取本地风险/通知状态...";
    if (panel && force) panel.innerHTML = `<div class="empty-state">正在刷新风险/通知看板...</div>`;
    try {
        const data = await requestJson("/api/risk-notifications/summary");
        renderRiskNotifications(data);
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`风险/通知看板加载失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "风险/通知看板加载失败。";
    }
}

async function loadSmartStop(force = false) {
    const note = document.getElementById("smartStopNote");
    const panel = document.getElementById("smartStopPanel");
    if (note) note.textContent = force ? "正在刷新观察退出参考状态..." : "正在读取观察退出参考状态...";
    if (panel && force) panel.innerHTML = `<div class="empty-state">正在刷新观察退出参考状态...</div>`;
    try {
        renderSmartStop(await requestJson("/api/risk-notifications/summary"));
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`观察退出参考状态读取失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "观察退出参考状态读取失败。";
    }
}

async function loadNotificationCenter(force = false) {
    const note = document.getElementById("notificationCenterNote");
    const panel = document.getElementById("notificationCenterPanel");
    if (note) note.textContent = force ? "正在刷新通知中心..." : "正在读取通知中心...";
    if (panel && force) panel.innerHTML = `<div class="empty-state">正在刷新通知中心...</div>`;
    try {
        renderNotificationCenter(await requestJson("/api/risk-notifications/summary"));
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`通知中心读取失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "通知中心读取失败。";
    }
}

function renderRiskNotifications(data) {
    const note = document.getElementById("riskNotificationsNote");
    const panel = document.getElementById("riskNotificationsPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "风险/通知看板暂不可用。"));
        return;
    }
    const paper = data.paper_watchlist || {};
    const smart = data.smart_stop || {};
    const risk = data.risk_alerts || {};
    const notify = data.notification_center || {};
    const localAlerts = data.local_alerts || {};
    const guardrails = data.guardrails || {};
    const diagnostics = guardrails.active_diagnostics || {};
    const smartItems = Array.isArray(smart.triggered_items) && smart.triggered_items.length
        ? smart.triggered_items
        : (Array.isArray(smart.items) ? smart.items.slice(0, 8) : []);
    const smartHtml = smartItems.map((item) => {
        const triggered = item.trigger_status === "triggered" || item.paper_exit_candidate;
        const tone = triggered ? "health-bad" : "health-warn";
        return `
            <div class="perf-card">
                <h4>${escapeHtml(safeText(item.name || item.symbol))}<span class="health-badge ${tone}">${escapeHtml(humanStatus(item.trigger_status || item.review_status || "watch"))}</span></h4>
                <p>${highlightText(item.trigger_reason || item.review_label || "纸面风险线暂无触发，仅保留本地观察。")}</p>
                <div class="tag-row">
                    <span class="tag">代码 ${escapeHtml(safeText(item.symbol))}</span>
                    <span class="tag">强制复核规则 ${escapeHtml(safeText(item.forced_exit_rule_count ?? 0))}</span>
                    <span class="tag">Paper Only</span>
                </div>
            </div>
        `;
    }).join("");
    const alertRows = Array.isArray(risk.top_alerts) ? risk.top_alerts : [];
    const alertHtml = alertRows.slice(0, 8).map((alert) => `
        <div class="key-review-row">
            <span class="key-review-label risk">${escapeHtml(safeText(alert.severity || alert.alert_type || "alert"))}</span>
            <span>${highlightText(alert.message || alert.rationale || alert.source || "风险告警需要复核。")}</span>
        </div>
    `).join("");
    const guardWarnings = Array.isArray(guardrails.warnings) ? guardrails.warnings : [];
    const localAlertRows = Array.isArray(localAlerts.alerts) ? localAlerts.alerts : [];
    const localAlertHtml = localAlertRows.slice(0, 6).map((alert) => `
        <span class="tag tag-watch">${escapeHtml(safeText(alert.title || alert.message || alert.kind || "local alert"))}</span>
    `).join("");
    const notificationWarnings = Array.isArray(notify.warnings) ? notify.warnings : [];
    const guardWarningHtml = guardWarnings.slice(0, 6).map((item) => `
        <div class="key-review-row">
            <span class="key-review-label risk">guardrail</span>
            <span>${highlightText(item)}</span>
        </div>
    `).join("");
    const notificationWarningHtml = notificationWarnings.slice(0, 5).map((item) => `
        <div class="key-review-row">
            <span class="key-review-label">notice</span>
            <span>${highlightText(item)}</span>
        </div>
    `).join("");
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>纸面风险状态</strong>
            <div class="section-note">${escapeHtml(safeText(data.message_for_ui || "只读风险/通知汇总；不会连接券商或下单。"))}</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>纸面观察</span><strong>${escapeHtml(safeText(paper.watchlist_count ?? 0))}</strong></div>
            <div class="metric-box"><span>观察退出参考样本</span><strong>${escapeHtml(safeText(smart.item_count ?? 0))}</strong></div>
            <div class="metric-box"><span>触发复核</span><strong>${escapeHtml(safeText(smart.triggered_count ?? 0))}</strong></div>
            <div class="metric-box"><span>纸面退出复核</span><strong>${escapeHtml(safeText(smart.paper_exit_review_count ?? 0))}</strong></div>
            <div class="metric-box"><span>风险告警</span><strong>${escapeHtml(safeText(risk.alert_count ?? 0))}</strong></div>
            <div class="metric-box"><span>严重告警</span><strong>${escapeHtml(safeText(risk.critical_count ?? 0))}</strong></div>
        </div>
        <div class="perf-strategy-grid">${smartHtml || missingBlock("暂无观察退出参考触发样本。可先同步/刷新纸面模拟池，再回到这里查看。")}</div>

        <div class="quant-summary-head" style="margin-top:14px">
            <strong>风险告警与通知中心</strong>
            <div class="section-note">通知中心默认只读试运行或本地文件输出；这里不发送外部通知，不连接任何交易通道。</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>风险告警</span><strong>${escapeHtml(humanStatus(risk.status || "missing"))}</strong></div>
            <div class="metric-box"><span>通知中心</span><strong>${escapeHtml(humanStatus(notify.status || "missing"))}</strong></div>
            <div class="metric-box"><span>只读试运行</span><strong>${notify.dry_run === false ? "关闭" : "开启"}</strong></div>
            <div class="metric-box"><span>消息数</span><strong>${escapeHtml(safeText(notify.message_count ?? 0))}</strong></div>
            <div class="metric-box"><span>本地提醒</span><strong>${escapeHtml(safeText((localAlerts.summary || {}).total ?? localAlertRows.length ?? 0))}</strong></div>
        </div>
        <div class="gap-panel">${alertHtml || "暂无最新风险告警行。"}${notificationWarningHtml}</div>
        <div class="tag-row">${localAlertHtml || `<span class="tag">暂无本地提醒记录</span>`}</div>

        <div class="quant-summary-head" style="margin-top:14px">
            <strong>Daily 防护诊断</strong>
            <div class="section-note">每日流程开启防护诊断时会主动运行特征审计 / 统计校验；结果只作为治理提示，不改策略参数。</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>防护诊断</span><strong>${escapeHtml(humanStatus(guardrails.status || "missing"))}</strong></div>
            <div class="metric-box"><span>特征审计</span><strong>${escapeHtml(humanStatus(guardrails.feature_quality_status || "missing"))}</strong></div>
            <div class="metric-box"><span>统计校验（当前样本）</span><strong>${escapeHtml(humanStatus(guardrails.statistical_validation_status || "missing"))}</strong></div>
            <div class="metric-box"><span>诊断股票</span><strong>${escapeHtml(safeText((diagnostics.symbols || []).length ?? 0))}</strong></div>
            <div class="metric-box"><span>警告</span><strong>${escapeHtml(safeText(guardrails.warning_count ?? 0))}</strong></div>
            <div class="metric-box"><span>失败项</span><strong>${escapeHtml(safeText(guardrails.failed_count ?? 0))}</strong></div>
        </div>
        ${renderResearchSampleScope(diagnostics.stats_validation || {})}
        <div class="gap-panel">${guardWarningHtml || "暂无防护诊断警告；若状态为缺失，请先运行每日流程并开启防护诊断。"}</div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route / Live Execution Locked / QMT Order Route Locked / Not Investment Advice / Not Position Advice.</p>
    `;
    if (note) note.textContent = `风险/通知状态：${humanStatus(data.status)}，纸面观察 ${safeText(paper.watchlist_count ?? 0)} 只，观察退出参考触发 ${safeText(smart.triggered_count ?? 0)} 条。`;
}

function renderSmartStop(data) {
    const note = document.getElementById("smartStopNote");
    const panel = document.getElementById("smartStopPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "观察退出参考状态暂不可用。"));
        return;
    }
    const smart = data.smart_stop || {};
    const items = Array.isArray(smart.triggered_items) && smart.triggered_items.length
        ? smart.triggered_items
        : (Array.isArray(smart.items) ? smart.items : []);
    const cards = items.map((item) => {
        const triggered = item.trigger_status === "triggered" || item.paper_exit_candidate;
        const tone = triggered ? "health-warn" : "health-ok";
        return `
            <div class="perf-card">
                <h4>${escapeHtml(safeText(item.name || item.symbol))}<span class="health-badge ${tone}">${escapeHtml(humanStatus(item.trigger_status || item.review_status || "watch"))}</span></h4>
                <div class="metric-row" style="margin:0">
                    <div class="metric-box"><span>代码</span><strong>${escapeHtml(safeText(item.symbol))}</strong></div>
                    <div class="metric-box"><span>纸面退出复核</span><strong>${item.paper_exit_candidate ? "是" : "否"}</strong></div>
                    <div class="metric-box"><span>强制复核规则</span><strong>${escapeHtml(safeText(item.forced_exit_rule_count ?? 0))}</strong></div>
                    <div class="metric-box"><span>边界</span><strong>Paper Only</strong></div>
                </div>
                <p>${highlightText(item.trigger_reason || item.review_label || "观察退出参考观察线未触发；继续纸面跟踪。")}</p>
                <div class="tag-row">
                    <span class="tag">Research Only</span>
                    <span class="tag">No Broker Route</span>
                    <span class="tag">Live Execution Locked</span>
                    <span class="tag">QMT Order Route Locked</span>
                    <span class="tag">No Real Orders</span>
                </div>
            </div>
        `;
    }).join("");
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>观察退出参考状态 <span class="health-badge ${smart.triggered_count ? "health-warn" : "health-ok"}">${escapeHtml(humanStatus(data.status || "passed"))}</span></strong>
            <div class="section-note">触发后只进入纸面退出复核，不会自动卖出、不会连接券商、不会更新真实账户。</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>样本数</span><strong>${escapeHtml(safeText(smart.item_count ?? 0))}</strong></div>
            <div class="metric-box"><span>触发复核</span><strong>${escapeHtml(safeText(smart.triggered_count ?? 0))}</strong></div>
            <div class="metric-box"><span>纸面退出候选</span><strong>${escapeHtml(safeText(smart.paper_exit_review_count ?? 0))}</strong></div>
            <div class="metric-box"><span>来源</span><strong>${escapeHtml(safeText(smart.source || "state/paper_watchlist.json"))}</strong></div>
        </div>
        <div class="perf-strategy-grid">${cards || missingBlock("暂无观察退出参考样本。请先同步或刷新纸面观察名单。")}</div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route / Live Execution Locked / QMT Order Route Locked / No Real Orders / Not Investment Advice / Not Position Advice.</p>
    `;
    if (note) note.textContent = `观察退出参考：样本 ${safeText(smart.item_count ?? 0)}，触发复核 ${safeText(smart.triggered_count ?? 0)}，纸面退出候选 ${safeText(smart.paper_exit_review_count ?? 0)}。`;
}

function renderNotificationCenter(data) {
    const note = document.getElementById("notificationCenterNote");
    const panel = document.getElementById("notificationCenterPanel");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "通知中心暂不可用。"));
        return;
    }
    const risk = data.risk_alerts || {};
    const notify = data.notification_center || {};
    const localAlerts = data.local_alerts || {};
    const riskRows = Array.isArray(risk.top_alerts) ? risk.top_alerts : [];
    const localRows = Array.isArray(localAlerts.alerts) ? localAlerts.alerts : [];
    const warnings = []
        .concat(Array.isArray(notify.warnings) ? notify.warnings : [])
        .concat(Array.isArray(risk.warnings) ? risk.warnings : []);
    const riskHtml = riskRows.slice(0, 8).map((alert) => `
        <div class="key-review-row">
            <span class="key-review-label risk">${escapeHtml(safeText(alert.severity || alert.alert_type || "alert"))}</span>
            <span>${highlightText(alert.message || alert.rationale || alert.source || "风险告警需要复核。")}</span>
        </div>
    `).join("");
    const localHtml = localRows.slice(0, 10).map((alert) => `
        <div class="key-review-row">
            <span class="key-review-label">${escapeHtml(safeText(alert.alert_label || alert.alert_type || alert.kind || "local"))}</span>
            <span>${highlightText(alert.title || alert.message || alert.plan_action_label || "本地提醒")}</span>
        </div>
    `).join("");
    const warningHtml = warnings.slice(0, 8).map((item) => `
        <div class="key-review-row">
            <span class="key-review-label risk">warning</span>
            <span>${highlightText(item)}</span>
        </div>
    `).join("");
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>通知中心 <span class="health-badge ${risk.critical_count ? "health-warn" : "health-ok"}">${escapeHtml(safeText(notify.status || "missing"))}</span></strong>
            <div class="section-note">默认只读试运行或本地文件输出；本页不发送外部消息，不触发交易。</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>只读试运行</span><strong>${notify.dry_run === false ? "关闭" : "开启"}</strong></div>
            <div class="metric-box"><span>消息数</span><strong>${escapeHtml(safeText(notify.message_count ?? 0))}</strong></div>
            <div class="metric-box"><span>已跳过</span><strong>${escapeHtml(safeText(notify.skipped_count ?? 0))}</strong></div>
            <div class="metric-box"><span>风险告警</span><strong>${escapeHtml(safeText(risk.alert_count ?? 0))}</strong></div>
            <div class="metric-box"><span>严重告警</span><strong>${escapeHtml(safeText(risk.critical_count ?? 0))}</strong></div>
            <div class="metric-box"><span>本地提醒</span><strong>${escapeHtml(safeText((localAlerts.summary || {}).total ?? localRows.length ?? 0))}</strong></div>
        </div>
        <div class="gap-panel">${riskHtml || "当前没有风险告警行。"}</div>
        <div class="gap-panel">${localHtml || "暂无本地提醒记录。"}</div>
        ${warningHtml ? `<div class="gap-panel">${warningHtml}</div>` : ""}
        <p class="panel-disclaimer">Research Only / Local Dry Run / No Broker Route / Live Execution Locked / QMT Order Route Locked / No Real Orders / Not Investment Advice / Not Position Advice.</p>
    `;
    if (note) note.textContent = `通知中心：${safeText(notify.status || "missing")}，风险告警 ${safeText(risk.alert_count ?? 0)}，本地提醒 ${safeText((localAlerts.summary || {}).total ?? localRows.length ?? 0)}。`;
}

async function refreshPaperRiskLines() {
    const button = document.getElementById("refreshPaperRiskButton");
    const note = document.getElementById("riskNotificationsNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "刷新中..."; }
    if (note) note.textContent = "正在用本地日线刷新纸面模拟池风险线；不会下单。";
    try {
        const payload = await postJson("/api/paper-simulation/refresh", {});
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui || "纸面风险线已刷新。");
        agentStatusLoaded = false;
        await loadRiskNotifications(true);
    } catch (error) {
        if (note) note.textContent = `纸面风险线刷新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

async function refreshSmartStopBoard() {
    const button = document.getElementById("refreshSmartStopButton");
    const note = document.getElementById("smartStopNote");
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "刷新中..."; }
    if (note) note.textContent = "正在刷新纸面观察名单的观察退出参考状态；不会下单。";
    try {
        const payload = await postJson("/api/paper-simulation/refresh", {});
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui || "观察退出参考状态已刷新。");
        agentStatusLoaded = false;
        await loadSmartStop(true);
        await loadRiskNotifications();
    } catch (error) {
        if (note) note.textContent = `观察退出参考刷新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

let backtestStrategiesLoaded = false;
let strategyScreeningPollTimer = null;

function setupBacktest() {
    const run = document.getElementById("runBacktestButton");
    const runScreening = document.getElementById("runStrategyScreeningButton");
    const refreshScreening = document.getElementById("refreshStrategyScreeningButton");
    if (run) run.addEventListener("click", runBacktest);
    if (runScreening) runScreening.addEventListener("click", runStrategyScreening);
    if (refreshScreening) refreshScreening.addEventListener("click", () => loadStrategyScreeningStatus(true));
}

async function loadBacktestModule() {
    await loadBacktestStrategies();
    await loadBacktestLatest();
    await loadStrategyScreeningStatus(false);
}

async function loadBacktestStrategies() {
    const select = document.getElementById("backtestStrategy");
    if (!select || backtestStrategiesLoaded) return;
    try {
        const data = await requestJson("/api/backtest/strategies");
        const list = Array.isArray(data.strategies) ? data.strategies : [];
        if (list.length) {
            select.innerHTML = list.map((s) => {
                const id = safeText(s.strategy_id || s.id || s.name);
                const name = safeText(s.name || s.title || id);
                return `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`;
            }).join("");
            backtestStrategiesLoaded = true;
        }
    } catch (error) {
        console.warn("[backtest] strategies load failed", error);
    }
}

async function loadBacktestLatest() {
    try {
        renderBacktest(await requestJson("/api/backtest/latest"));
    } catch (error) {
        const panel = document.getElementById("backtestPanel");
        if (panel) panel.innerHTML = missingBlock(`回测状态加载失败：${escapeHtml(errorText(error))}`);
    }
}

async function runBacktest() {
    const button = document.getElementById("runBacktestButton");
    const note = document.getElementById("backtestNote");
    const select = document.getElementById("backtestStrategy");
    const maxInput = document.getElementById("backtestMaxSymbols");
    const startInput = document.getElementById("backtestStartDate");
    const endInput = document.getElementById("backtestEndDate");
    const strategy = select && select.value ? select.value : "ma_breakout";
    let maxSymbols = parseInt(maxInput && maxInput.value, 10);
    if (!Number.isFinite(maxSymbols)) maxSymbols = 20;
    const startDate = startInput && startInput.value ? startInput.value : "2015-01-01";
    const endDate = endInput && endInput.value ? endInput.value : "";
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "运行中…（请稍候）"; }
    if (note) note.textContent = `正在对策略 ${strategy} 跑本地历史回测：${startDate} → ${endDate || "最新"}…`;
    try {
        const data = await postJson("/api/backtest/run", {
            strategy,
            max_symbols: maxSymbols,
            start_date: startDate,
            end_date: endDate,
        });
        renderBacktest(data);
        if (note) note.textContent = safeText(data.message_for_ui || "回测完成。");
    } catch (error) {
        if (note) note.textContent = `回测失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

function renderResearchSampleScope(data) {
    const source = data && typeof data === "object" ? data : {};
    const evidence = source.research_evidence || {};
    const sampleRole = source.sample_role || evidence.sample_role;
    const validation = source.out_of_sample_validation || evidence.out_of_sample_validation;
    const sampleText = sampleRole === "in_sample" ? "样本内研究" : "样本范围未声明";
    const validationText = validation === "not_established"
        ? "样本外验证未建立" : "样本外验证状态未知，尚未建立";
    const limitation = source.sample_limitation || evidence.sample_limitation
        || "尚未建立样本外验证；历史筛选或统计通过不代表策略已验证。";
    const demo = source.synthetic_test_data === true || source.synthetic === true || source.data_type === "synthetic" || evidence.synthetic_test_data === true;
    return `<div class="section-note research-sample-scope">${demo ? "合成演示数据 · 不作为正式成绩 · " : ""}${escapeHtml(sampleText)} · ${escapeHtml(validationText)}<br>${escapeHtml(safeText(limitation))}<br>规则身份：${escapeHtml(source.rule_version || source.strategy_version || source.strategy || "未记录")}；样本：${escapeHtml(safeText(source.symbol_count))}只，${escapeHtml(safeText(source.metrics?.number_of_trades ?? source.trade_count))}笔。流程完成和统计通过均不等于已验证盈利优势。</div>`;
}

function renderBacktest(data) {
    const panel = document.getElementById("backtestPanel");
    const note = document.getElementById("backtestNote");
    if (!panel) return;
    if (!data || data.ok === false) {
        panel.innerHTML = missingBlock(safeText((data && data.message_for_ui) || "回测暂时不可用。"));
        return;
    }
    if (data.exists === false) {
        panel.innerHTML = `<div class="empty-state">${escapeHtml(safeText(data.message_for_ui || "尚未运行回测。"))}</div>`;
        if (note) note.textContent = "尚未运行回测。";
        return;
    }
    const m = data.metrics || {};
    const startInput = document.getElementById("backtestStartDate");
    const endInput = document.getElementById("backtestEndDate");
    const params = data.params || data.request || {};
    const curvePoints = Array.isArray(data.equity_curve) ? data.equity_curve : [];
    const curvePointDate = (pt) => safeText(Array.isArray(pt) ? pt[0] : ((pt && (pt.date || pt.day)) || ""));
    const curveStart = curvePoints.length ? curvePointDate(curvePoints[0]) : "";
    const curveEnd = curvePoints.length ? curvePointDate(curvePoints[curvePoints.length - 1]) : "";
    const startDate = safeText(curveStart || data.start_date || data.period_start || params.start_date || (startInput && startInput.value) || "2015-01-01");
    const endDate = safeText(curveEnd || data.end_date || data.period_end || params.end_date || (endInput && endInput.value) || "最新");
    const warn = data.assumptions && data.assumptions.warning
        ? `<div class="gap-panel" style="margin:0 0 12px">${escapeHtml(safeText(data.assumptions.warning))}</div>`
        : "";
    const priceBasisHtml = renderPriceBasisBanner(data);
    const metricsHtml = `
        <div class="metric-row">
            <div class="metric-box"><span>回测年化</span><strong>${sanePercentHTML(m.annual_return)}</strong></div>
            <div class="metric-box"><span>累计收益</span><strong>${sanePercentHTML(m.total_return)}</strong></div>
            <div class="metric-box"><span>最大回撤</span><strong>${sanePercentHTML(m.max_drawdown)}</strong></div>
            <div class="metric-box"><span>夏普</span><strong>${numText(m.sharpe)}</strong></div>
            <div class="metric-box"><span title="上涨交易日占比，非盈利交易占比">上涨日占比</span><strong>${pctText(m.daily_positive_rate ?? m.win_rate)}</strong></div>
            <div class="metric-box"><span>交易笔数</span><strong>${safeText(m.number_of_trades)}</strong></div>
            <div class="metric-box"><span>超额收益</span><strong>${pctText(m.excess_return)}</strong></div>
            <div class="metric-box"><span>样本股票</span><strong>${safeText(data.symbol_count)}</strong></div>
        </div>`;
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>历史研究回测 · ${escapeHtml(safeText(data.strategy))}</strong>
            <div class="section-note">区间：${escapeHtml(startDate)} → ${escapeHtml(endDate)}；${escapeHtml(safeText(data.message_for_ui || ""))}（${escapeHtml(safeText(data.generated_at || ""))}）</div>
            ${renderResearchSampleScope(data)}
        </div>
        ${warn}
        ${priceBasisHtml}
        ${metricsHtml}
        <div class="chart-card">
            <div class="chart-card-head"><h4>${data.mode === "single" || data.not_a_capital_constrained_portfolio_backtest === true ? "单股研究收益曲线（不代表资金约束组合）" : "研究模拟曲线（组合口径以报告声明为准）"}</h4><span class="chart-note">本地完整日线模拟，不含真实撮合，不代表未来</span></div>
            <div id="backtestEquityChart" class="equity-chart"></div>
        </div>
        ${renderBacktestTradesTable(Array.isArray(data.trades) ? data.trades : [])}
        <p class="panel-disclaimer">${escapeHtml(researchOnlyDisclaimer(data, "历史回测不代表未来收益，不构成投资建议。"))}</p>`;
    renderEquityChart(Array.isArray(data.equity_curve) ? data.equity_curve : []);
    if (note) note.textContent = `回测：${safeText(data.strategy)} · ${safeText(data.symbol_count)} 只`;
}

const PRICE_BASIS_LABELS_CN = {
    raw: "原始日线价格",
    forward_adjusted: "前复权",
    back_adjusted: "后复权",
    mixed_or_unknown: "混合或未知口径",
};

const PRICE_BASIS_WARNING_CN = {
    requested_forward_adjusted_but_using_flat_daily_raw_candidate:
        "请求前复权，但本地无前复权日线，实际使用原始 data/daily 价格（口径回退）。",
    "divergent_adjustment_factor_alias:data/metadata/adjustment_factor.csv":
        "检测到 adjustment_factor.csv 与权威 adjustment_factors.csv 内容不一致；别名仅作审计，不作为数据源。",
};

function describePriceBasisCn(basis) {
    return PRICE_BASIS_LABELS_CN[String(basis || "")] || PRICE_BASIS_LABELS_CN.mixed_or_unknown;
}

function priceBasisWarningCn(item) {
    const key = String(item || "");
    return PRICE_BASIS_WARNING_CN[key] || key;
}

// Option C: surface the explicit price_basis / adjustment_basis / adjustment_warnings
// labels so backtest results never hide which price convention produced them.
function renderPriceBasisBanner(data) {
    if (!data) return "";
    const assumptions = data.assumptions && typeof data.assumptions === "object" ? data.assumptions : {};
    const basis = data.price_basis || assumptions.price_basis || "";
    const warnings = []
        .concat(Array.isArray(data.adjustment_warnings) ? data.adjustment_warnings : [])
        .concat(Array.isArray(assumptions.adjustment_warnings) ? assumptions.adjustment_warnings : []);
    const uniqueWarnings = Array.from(new Set(warnings.filter(Boolean).map((item) => String(item))));
    if (!basis && !uniqueWarnings.length) return "";
    const basisLine = basis
        ? `<div class="section-note">价格口径：<strong>${escapeHtml(describePriceBasisCn(basis))}</strong>（${escapeHtml(safeText(basis))}）</div>`
        : "";
    const warnLines = uniqueWarnings.length
        ? `<ul style="margin:6px 0 0 18px;padding:0">${uniqueWarnings
              .map((item) => `<li>${escapeHtml(priceBasisWarningCn(item))}</li>`)
              .join("")}</ul>`
        : "";
    const cls = uniqueWarnings.length ? "gap-panel" : "section-note";
    return `<div class="${cls}" style="margin:0 0 12px">${basisLine}${warnLines}</div>`;
}

function renderBacktestTradesTable(trades) {
    if (!trades.length) return missingBlock("暂无交易记录。");
    const sideMap = { BUY: "回测入组合", SELL: "回测出组合", buy: "回测入组合", sell: "回测出组合" };
    const reasonMap = {
        "BUY because symbol is in ranked target set": "历史模拟入选（排名进入目标组合）",
        "SELL because symbol left target set": "历史模拟退出（离开目标组合）",
    };
    const rows = trades.slice(0, 80).map((item) => {
        const reason = reasonMap[item.reason] || item.reason || item.blocked_reason || "";
        return `
            <tr>
                <td>${escapeHtml(safeText(item.date || item.execution_date))}</td>
                <td>${escapeHtml(safeText(item.symbol))}</td>
                <td>${escapeHtml(sideMap[item.side] || humanizeStatusTerms(item.side || "—"))}</td>
                <td class="number">${escapeHtml(safeText(item.shares))}</td>
                <td class="number">${escapeHtml(numText(item.executed_price ?? item.requested_price, 3))}</td>
                <td class="number">${escapeHtml(moneyText(item.gross_amount || 0))}</td>
                <td class="number">${escapeHtml(moneyText(item.commission || 0))}</td>
                <td class="number">${escapeHtml(moneyText(item.stamp_duty || 0))}</td>
                <td class="number">${escapeHtml(moneyText(item.slippage_cost || 0))}</td>
                <td>${escapeHtml(sanitizeTradingTerms(humanizeStatusTerms(reason)))}</td>
            </tr>
        `;
    }).join("");
    return `
        <div class="paper-trades-panel" style="margin-top:14px">
            <h3>回测交易记录</h3>
            <p class="paper-desk-note">以下为历史回测模拟事件，不构成操作建议。</p>
            <div class="paper-table-wrap">
                <table class="paper-trades-table">
                    <thead><tr><th>日期</th><th>股票</th><th>历史事件</th><th>股数</th><th>成交价</th><th>金额</th><th>佣金</th><th>印花税</th><th>滑点</th><th>原因</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>
    `;
}

function renderEquityChart(points) {
    const el = document.getElementById("backtestEquityChart");
    if (!el) return;
    if (typeof echarts !== "object" || !points.length) {
        el.innerHTML = missingBlock("权益曲线数据缺失。");
        return;
    }
    const existing = echarts.getInstanceByDom(el);
    if (existing) existing.dispose();
    const chart = initChart("backtestEquityChart");
    if (!chart) return;
    chart.setOption({
        tooltip: { trigger: "axis" },
        grid: { left: 64, right: 18, top: 18, bottom: 40 },
        xAxis: { type: "category", data: points.map((p) => safeText(p.date)), axisLabel: { color: "#807C6D" } },
        yAxis: { type: "value", scale: true, axisLabel: { color: "#807C6D" } },
        series: [{
            type: "line",
            data: points.map((p) => (typeof p.equity === "number" ? p.equity : null)),
            showSymbol: false,
            smooth: true,
            lineStyle: { color: "#2563C9", width: 2 },
            areaStyle: { color: "rgba(37,99,201,0.10)" },
        }],
    });
}

function readBoundedIntegerInput(id, fallback, min, max) {
    const input = document.getElementById(id);
    let value = parseInt(input && input.value, 10);
    if (!Number.isFinite(value)) value = fallback;
    value = Math.max(min, Math.min(value, max));
    if (input) input.value = String(value);
    return value;
}

async function loadStrategyScreeningStatus(force = false) {
    const note = document.getElementById("strategyScreeningNote");
    const panel = document.getElementById("strategyScreeningPanel");
    if (force && note) note.textContent = "正在刷新策略筛选漏斗状态…";
    try {
        const data = await requestJson("/api/strategy-screening/status");
        renderStrategyScreeningStatus(data);
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`策略筛选漏斗状态读取失败：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "策略筛选漏斗状态读取失败。";
    }
}

async function runStrategyScreening() {
    const button = document.getElementById("runStrategyScreeningButton");
    const note = document.getElementById("strategyScreeningNote");
    const featureCache = document.getElementById("strategyScreeningFeatureCache");
    const fullScan = document.getElementById("strategyScreeningFullScan");
    const original = button ? button.textContent : "";
    const payload = {
        max_symbols: readBoundedIntegerInput("strategyScreeningMaxSymbols", 600, 20, 1500),
        max_strategies: readBoundedIntegerInput("strategyScreeningMaxStrategies", 8, 1, 30),
        feature_cache: featureCache && featureCache.checked ? "true" : "false",
        full_scan: Boolean(fullScan && fullScan.checked),
    };
    if (button) { button.disabled = true; button.textContent = "已提交后台…"; }
    if (note) note.textContent = payload.full_scan
        ? "已显式请求全量扫描，后台运行可能较久；页面可继续使用。"
        : "正在提交后台策略筛选漏斗；默认限样本并启用缓存。";
    try {
        const data = await postJson("/api/strategy-screening/run", payload);
        renderStrategyScreeningStatus(data);
        scheduleStrategyScreeningPoll();
    } catch (error) {
        if (note) note.textContent = `策略筛选漏斗启动失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) { button.disabled = false; button.textContent = original; }
    }
}

function scheduleStrategyScreeningPoll() {
    if (strategyScreeningPollTimer) window.clearTimeout(strategyScreeningPollTimer);
    strategyScreeningPollTimer = window.setTimeout(() => loadStrategyScreeningStatus(false), 5000);
}

function renderStrategyScreeningStatus(data) {
    const note = document.getElementById("strategyScreeningNote");
    const panel = document.getElementById("strategyScreeningPanel");
    const runButton = document.getElementById("runStrategyScreeningButton");
    if (!panel) return;
    const status = safeText(data && data.status ? data.status : "idle");
    const running = Boolean(data && data.running);
    const latest = data && data.latest_result ? data.latest_result : {};
    const params = data && data.params ? data.params : {};
    const summary = latest.exists ? latest : data || {};
    const topStrategies = Array.isArray(summary.top_strategies) ? summary.top_strategies : [];
    const warnings = Array.isArray(summary.warnings) ? summary.warnings : [];
    const statusTone = running ? "health-warn" : (summary.ok === false ? "health-bad" : "health-ok");
    const cacheStatus = safeText(summary.feature_cache_status || params.feature_cache || "数据缺失");
    const fullScanText = params.full_scan_requested || params.full_scan ? "显式全量" : "有界样本";
    const topHtml = topStrategies.slice(0, 5).map((item, index) => `
        <tr>
            <td>#${index + 1}</td>
            <td>${escapeHtml(safeText(item.strategy || "策略缺失"))}</td>
            <td>${escapeHtml(humanStatus(item.status || "状态缺失"))}</td>
            <td>${sanePercentHTML(item.annualized_return_net)}</td>
            <td>${sanePercentHTML(item.max_drawdown)}</td>
            <td>${numText(item.sharpe)}</td>
        </tr>
    `).join("");
    const warningHtml = warnings.length
        ? `<div class="gap-panel"><strong>漏斗告警</strong><div>${warnings.map((item) => escapeHtml(safeText(item))).join("；")}</div></div>`
        : "";
    const message = safeText((data && data.message_for_ui) || summary.message_for_ui || "策略筛选漏斗状态已读取。");
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>研究筛选漏斗状态 <span class="health-badge ${statusTone}">${escapeHtml(status)}</span></strong>
            <div class="section-note">${escapeHtml(message)}</div>
        </div>
        <div class="metric-row">
            <div class="metric-box"><span>运行模式</span><strong>${escapeHtml(fullScanText)}</strong></div>
            <div class="metric-box"><span>本地 CSV</span><strong>${escapeHtml(safeText(summary.scanned_csv ?? params.max_symbols ?? "数据缺失"))} / ${escapeHtml(safeText(summary.daily_csv_available ?? "数据缺失"))}</strong></div>
            <div class="metric-box"><span>可用 CSV</span><strong>${escapeHtml(safeText(summary.usable_csv ?? "数据缺失"))}</strong></div>
            <div class="metric-box"><span>策略数</span><strong>${escapeHtml(safeText(summary.strategies_tested ?? params.max_strategies ?? "数据缺失"))}</strong></div>
            <div class="metric-box"><span>缓存</span><strong>${escapeHtml(cacheStatus)}</strong></div>
            <div class="metric-box"><span>耗时</span><strong>${numText(summary.elapsed_seconds)}s</strong></div>
            <div class="metric-box"><span>候选</span><strong>${escapeHtml(safeText(summary.candidate_count ?? "数据缺失"))}</strong></div>
            <div class="metric-box"><span>纸面观察</span><strong>${escapeHtml(safeText(summary.watchlist_count ?? "数据缺失"))}</strong></div>
        </div>
        ${warningHtml}
        <div class="theme-map-subpanel">
            <h3>策略排行预览 · 历史研究筛选</h3>
            ${renderResearchSampleScope(summary)}
            <table class="compact-table">
                <thead><tr><th>排名</th><th>策略</th><th>状态</th><th>年化</th><th>回撤</th><th>夏普</th></tr></thead>
                <tbody>${topHtml || `<tr><td colspan="6">暂无排行结果；后台完成后刷新可见。</td></tr>`}</tbody>
            </table>
        </div>
        <p class="panel-disclaimer">Research Only / Paper Account Only / No Broker Route。该漏斗只为候选池、回测和纸面模拟池提供研究证据，不生成真实买卖或仓位指令。</p>
    `;
    if (note) {
        note.textContent = running
            ? "策略筛选漏斗正在后台运行；页面可继续使用，稍后自动刷新状态。"
            : message;
    }
    if (runButton) runButton.disabled = running;
    if (running) scheduleStrategyScreeningPoll();
    if (!running && strategyScreeningPollTimer) {
        window.clearTimeout(strategyScreeningPollTimer);
        strategyScreeningPollTimer = null;
    }
}

async function loadRealtimeMarket(forceRefresh) {
    const button = document.getElementById("refreshRealtimeButton");
    const note = document.getElementById("realtimeStatusNote");
    // A plain GET is a read-only cache view; an explicit refresh is a POST that
    // carries the intent (B6), so a page load or a cross-site embed can never
    // trigger a provider refresh through a query string.
    const url = "/api/realtime/market";
    if (button && forceRefresh) {
        button.disabled = true;
        button.textContent = "正在刷新实时观察…";
    }
    if (note && forceRefresh) note.textContent = "正在刷新实时快照和行业板块；这些数据不会覆盖正式日线。";
    try {
        const data = forceRefresh
            ? await postJson(url, { refresh: true, network: true })
            : await requestJson(url);
        renderRealtimeMarket(data);
    } catch (error) {
        const metrics = document.getElementById("realtimeMetrics");
        const lists = document.getElementById("realtimeLists");
        if (metrics) metrics.innerHTML = missingBlock(`实时观察层暂时无法加载：${escapeHtml(errorText(error))}`);
        if (lists) lists.innerHTML = "";
        if (note) note.textContent = "实时观察层加载失败，正式研究层仍可继续使用完整日线。";
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "刷新实时观察";
        }
    }
}

function renderRealtimeMarket(data) {
    const metrics = document.getElementById("realtimeMetrics");
    const lists = document.getElementById("realtimeLists");
    const note = document.getElementById("realtimeStatusNote");
    if (!metrics || !lists) return;
    const freshness = safeText(data.data_freshness_label || "实时快照");
    const statusText = `${safeText(data.session_label)} · ${freshness} · 最后成功快照 ${safeText(data.last_success_at || data.updated_at)} · ${displayText(data.message_for_ui || data.boundary)}`;
    if (note) note.textContent = statusText;
    scheduleRealtimeFollowup(data.refresh_pending === true);
    if (!data.ok) {
        metrics.innerHTML = missingBlock(displayText(data.message_for_ui || "实时快照缺失。"));
        lists.innerHTML = missingBlock("可以继续使用下方正式研究层；实时观察层不影响完整日线。");
        return;
    }
    metrics.innerHTML = [
        realtimeFreshnessCard(data),
        realtimeMetric("市场时段", safeText(data.session_label), "临时快照"),
        realtimeMetric("快照样本", `${safeText(data.sample_count)}只`, data.network_authorized ? "已授权联网刷新" : "缓存只读"),
        realtimeMetric("涨跌家数", `${safeText(data.advancing_count)}涨 / ${safeText(data.declining_count)}跌`, `${safeText(data.flat_count)}平`),
        realtimeMetric("快照成交额", safeText(data.total_amount), "收盘前仍会变化"),
    ].join("");
    lists.innerHTML = `
        <article class="realtime-card">
            <div class="realtime-card-label">个股涨幅 Top5</div>
            ${renderMiniStockList(data.top_gainers || [])}
        </article>
        <article class="realtime-card">
            <div class="realtime-card-label">板块涨幅 Top5</div>
            ${renderMiniSectorList(data.sector_top_gainers || [])}
        </article>
        <article class="realtime-card">
            <div class="realtime-card-label">成交额活跃 Top5</div>
            ${renderMiniStockList(data.active_amount || [], true)}
        </article>
    `;
}

function scheduleRealtimeFollowup(pending) {
    if (realtimeRefreshTimer) {
        window.clearTimeout(realtimeRefreshTimer);
        realtimeRefreshTimer = null;
    }
    if (!pending) return;
    realtimeRefreshTimer = window.setTimeout(() => {
        realtimeRefreshTimer = null;
        loadRealtimeMarket(false);
    }, 4000);
}

function realtimeFreshnessCard(data) {
    const stale = data.is_cache_stale === true;
    const liveLagging = data.is_live_lagging === true;
    const hasSectorRows = Array.isArray(data.sector_top_gainers) && data.sector_top_gainers.length > 0;
    const error = data.provider_error || (hasSectorRows ? "" : data.sector_refresh?.message_for_ui || "");
    const note = typeof data.provider_note === "string" ? data.provider_note.trim() : "";
    const source = safeText(data.snapshot_source || "local_cache");
    const status = safeText(data.data_freshness_label || (stale ? "本地快照待更新" : "可用快照"));
    const hint = hasValue(data.freshness_hint)
        ? safeText(data.freshness_hint)
        : `最后成功快照 ${safeText(data.last_success_at || data.updated_at)}；来源 ${source}。`;
    const detail = error ? `<p class="metric-hint realtime-provider-error">${escapeHtml(displayText(error))}</p>` : "";
    const providerNote = note ? `<p class="metric-hint">${escapeHtml(displayText(note))}</p>` : "";
    return `
        <article class="realtime-card ${liveLagging ? "realtime-card-stale" : "realtime-card-fresh"}">
            <div class="realtime-card-label">数据新鲜度</div>
            <div class="realtime-card-value">${escapeHtml(status)}</div>
            <p class="metric-hint">${escapeHtml(hint)}</p>
            <p class="metric-hint">快照日期 ${escapeHtml(safeText(data.snapshot_quote_date))} · 预期复盘日 ${escapeHtml(safeText(data.expected_snapshot_date))}</p>
            ${providerNote}
            ${detail}
        </article>
    `;
}

function realtimeMetric(label, value, hint) {
    return `
        <article class="realtime-card">
            <div class="realtime-card-label">${escapeHtml(label)}</div>
            <div class="realtime-card-value">${escapeHtml(value)}</div>
            <p class="metric-hint">${escapeHtml(hint)}</p>
        </article>
    `;
}

function renderMiniStockList(items, showAmount = false) {
    if (!items.length) return `<div class="empty-state">暂无实时快照数据。</div>`;
    return `<ol class="mini-list">${items.slice(0, 5).map((item) => `
        <li>
            <span>
                ${escapeHtml(item.name)} <small>${escapeHtml(item.symbol)}</small>
            </span>
            <strong class="${showAmount ? "neutral" : changeClass(item.pct_chg)}">${escapeHtml(showAmount ? safeText(item.amount) : formatChange(item.pct_chg))}</strong>
        </li>
    `).join("")}</ol>`;
}

function renderMiniSectorList(items) {
    if (!items.length) return `<div class="empty-state">暂无板块快照数据。</div>`;
    return `<ol class="mini-list">${items.slice(0, 5).map((item) => `
        <li>
            <span>
                ${escapeHtml(item.name)}
                <small>${escapeHtml(item.hint || "实时板块观察")}</small>
            </span>
            <strong class="${changeClass(item.change)}">${escapeHtml(formatChange(item.change))}</strong>
        </li>
    `).join("")}</ol>`;
}

async function searchRealtimeStock() {
    const input = document.getElementById("stockSearchInput");
    const results = document.getElementById("stockSearchResults");
    const query = input ? input.value.trim() : "";
    if (!results) return;
    if (!query) {
        results.innerHTML = missingBlock("请输入股票代码或名称。");
        return;
    }
    results.innerHTML = `<div class="empty-state">正在搜索 ${escapeHtml(query)}…</div>`;
    try {
        const data = await requestJson(`/api/realtime/stock-search?q=${encodeURIComponent(query)}&limit=8`);
        renderStockSearchResults(data.rows || []);
    } catch (error) {
        results.innerHTML = missingBlock(`搜索失败：${escapeHtml(errorText(error))}`);
    }
}

function renderStockSearchResults(rows) {
    currentStockSearchRows = Array.isArray(rows) ? rows.slice() : [];
    rememberWorkbenchSource("search", currentStockSearchRows);
    const results = document.getElementById("stockSearchResults");
    if (!results) return;
    if (!rows.length) {
        results.innerHTML = missingBlock("没有找到匹配股票。可以直接输入6位代码再试。");
        return;
    }
    results.innerHTML = rows.map((row) => `
        <button class="search-result-button" type="button" data-symbol="${escapeHtml(row.symbol)}" data-name="${escapeHtml(row.name)}">
            <span>
                <strong>${escapeHtml(row.name)}</strong>
                <small>${escapeHtml(row.symbol)} · ${escapeHtml(row.source)}</small>
            </span>
            <span class="${changeClass(row.pct_chg)}">${escapeHtml(formatChange(row.pct_chg))}</span>
        </button>
    `).join("");
    results.querySelectorAll(".search-result-button").forEach((button) => {
        button.addEventListener("click", () => showRealtimeStockDetail(button.dataset.symbol, button.dataset.name));
    });
}

async function showRealtimeStockDetail(symbol, name) {
    if (!symbol) return;
    return showDetail({ symbol, name: name || symbol }, { source: "search", items: currentStockSearchRows });
}

async function addManualObservation(symbol, name) {
    const button = document.getElementById("addManualObservationButton");
    const note = document.getElementById("manualObservationNote");
    if (button) {
        button.disabled = true;
        button.textContent = "正在加入观察池…";
    }
    try {
        const payload = await postJson("/api/realtime/watchlist", {
            symbol,
            name,
            note: "从实时观察层手动加入；未进入正式研究候选。",
        });
        if (note) note.textContent = safeText(payload.message);
    } catch (error) {
        if (note) note.textContent = `加入失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "加入手动观察池";
        }
    }
}

async function refreshSectorBoard() {
    const button = document.getElementById("refreshSectorButton");
    const note = document.getElementById("marketDataNote");
    if (button) {
        button.disabled = true;
        button.textContent = "正在联网拉取行业板块…";
    }
    if (note) note.textContent = "正在从东方财富拉取行业板块涨跌榜（仅本地缓存，不接券商）…";
    try {
        const payload = await postJson("/api/sector/refresh", {});
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message);
        await loadMarketOverview();
    } catch (error) {
        if (note) note.textContent = `板块刷新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "刷新板块";
        }
    }
}

async function loadLimitUpReview(forceRefresh, refreshAttribution = false) {
    const button = document.getElementById("refreshLimitReviewButton");
    const note = document.getElementById("limitReviewNote");
    const panel = document.getElementById("limitReviewPanel");
    if (button && forceRefresh) {
        button.disabled = true;
        button.textContent = "正在刷新涨停复盘…";
    }
    if (note) note.textContent = forceRefresh ? "正在显式联网拉取涨停池；仅用于研究复盘。" : "正在读取本地涨停复盘缓存…";
    try {
        const payload = forceRefresh
            ? await postJson("/api/limit-up/refresh", { network: true })
            : await requestJson(refreshAttribution ? "/api/limit-up/review?refresh_attribution=1" : "/api/limit-up/review");
        renderLimitUpReview(payload);
        if (note) note.textContent = safeText(payload.message_for_ui);
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`涨停复盘暂时无法加载：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "涨停复盘刷新失败；页面继续保留其他研究层。";
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "刷新涨停复盘";
        }
    }
}

function renderLimitUpReview(payload) {
    const panel = document.getElementById("limitReviewPanel");
    if (!panel) return;
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    const themes = Array.isArray(payload.theme_summary) ? payload.theme_summary : [];
    const intersections = Array.isArray(payload.quant_intersections) ? payload.quant_intersections : [];
    const attribution = payload.attribution_summary || {};
    if (!rows.length) {
        panel.innerHTML = missingBlock(safeText(payload.message_for_ui || "本地涨停复盘缓存缺失。点击刷新涨停复盘可显式联网拉取。"));
        return;
    }
    panel.innerHTML = `
        <div class="limit-review-head">
            <div class="tag-row">
                <span class="tag">日期 ${escapeHtml(safeText(payload.date))}</span>
                <span class="tag">来源 ${escapeHtml(safeText(payload.source))}</span>
                <span class="tag tag-watch">Research Only</span>
                <span class="tag tag-watch">不代表真实可成交</span>
            </div>
            <div class="limit-review-metrics">
                ${limitMetric("涨停池", `${safeText(payload.limit_up_count)}只`)}
                ${limitMetric("连板股", `${safeText(payload.connected_count)}只`)}
                ${limitMetric("封板未遂", `${safeText(payload.broken_count)}只`)}
                ${limitMetric("封板率", formatPercentValue(payload.seal_rate))}
                ${limitMetric("Quant交集", `${intersections.length}只`)}
                ${limitMetric("追溯线索", countWithUnit(attribution.traceable_count, "只"))}
            </div>
            <div class="section-note">${escapeHtml(displayText(attribution.message || "题材归因证据链来自本地映射、缓存公告/新闻和行业线索。"))}</div>
            <div class="section-note">${escapeHtml(safeText(payload.boundary))}</div>
        </div>
        <div class="limit-review-grid">
            <section class="limit-review-panel">
                <h3>涨停样本 Top20</h3>
                ${renderLimitUpTable(rows.slice(0, 20))}
            </section>
            <aside class="limit-review-panel">
                <h3>题材归因 Top</h3>
                ${renderLimitThemeList(themes)}
                <h3 style="margin-top:16px">Quant 候选交集</h3>
                ${renderLimitIntersectionList(intersections)}
            </aside>
        </div>
    `;
}

function limitMetric(label, value) {
    return `<div class="limit-review-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function formatPercentValue(value) {
    const number = numericValue(value);
    if (number === null) return safeText(value);
    return `${number.toFixed(2)}%`;
}

function formatYiAmount(value) {
    const number = numericValue(value);
    if (number === null) return safeText(value);
    if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`;
    if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(2)}万`;
    return number.toFixed(0);
}

function renderLimitUpTable(rows) {
    if (!rows.length) return missingBlock("涨停样本为空。");
    return `
        <div style="overflow:auto">
            <table class="limit-table">
                <thead>
                    <tr>
                        <th>股票</th>
                        <th>题材/行业</th>
                        <th>连板</th>
                        <th>首次封板</th>
                        <th>封板资金</th>
                        <th>追溯证据</th>
                        <th>复盘线索</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows.map((row) => `
                        <tr>
                            <td><strong>${escapeHtml(safeText(row.name))}</strong><br><span class="candidate-code">${escapeHtml(safeText(row.symbol))}</span></td>
                            <td>${escapeHtml(safeText(row.primary_theme))}<br><span class="section-note">${escapeHtml(safeText(row.industry))}</span></td>
                            <td>${escapeHtml(safeText(row.consecutive_boards))}</td>
                            <td>${escapeHtml(safeText(row.first_seal_time))}</td>
                            <td>${escapeHtml(formatYiAmount(row.seal_amount))}</td>
                            <td>${renderAttributionEvidence(row)}</td>
                            <td>${escapeHtml(safeText(row.reason_reference))}</td>
                        </tr>
                    `).join("")}
                </tbody>
            </table>
        </div>
    `;
}

function renderLimitThemeList(themes) {
    if (!themes.length) return missingBlock("题材归因暂不可用。");
    return `<div class="limit-theme-list">${themes.slice(0, 8).map((theme) => `
        <div class="limit-theme-item">
            <strong>${escapeHtml(safeText(theme.primary_theme))}</strong>
            <div>${escapeHtml(safeText(theme.count))} 只涨停；最高 ${escapeHtml(safeText(theme.max_consecutive_boards))} 连板。</div>
            <div class="section-note">${escapeHtml(renderSourceMixText(theme.source_counts || {}, theme.news_evidence_count))}</div>
            <div class="section-note">${escapeHtml((theme.representatives || []).join("、"))}</div>
        </div>
    `).join("")}</div>`;
}

function renderAttributionEvidence(row) {
    const sources = Array.isArray(row.attribution_sources) ? row.attribution_sources : [];
    if (!sources.length) return `<span class="tag tag-missing">待核验</span>`;
    return `
        <div class="evidence-stack">
            ${sources.slice(0, 3).map((source) => `
                <span class="evidence-pill evidence-${sourceClass(source.source_type)}" title="${escapeHtml(safeText(source.evidence_text))}">
                    ${escapeHtml(displayText(source.source_label || source.source_type))} · ${escapeHtml(displayText(source.theme))}
                </span>
            `).join("")}
            <span class="section-note">${escapeHtml(displayText(row.attribution_summary))}</span>
        </div>
    `;
}

function sourceClass(sourceType) {
    if (sourceType === "announcement_keyword") return "news";
    if (sourceType === "manual_mapping" || sourceType === "mapping_import") return "mapped";
    if (sourceType === "provider_industry") return "provider";
    return "unknown";
}

function renderSourceMixText(sourceCounts, newsCount) {
    const parts = [];
    if (sourceCounts.manual_mapping || sourceCounts.mapping_import) parts.push(`映射${(sourceCounts.manual_mapping || 0) + (sourceCounts.mapping_import || 0)}`);
    if (sourceCounts.announcement_keyword) parts.push(`公告/新闻${sourceCounts.announcement_keyword}`);
    if (sourceCounts.provider_industry) parts.push(`行业线索${sourceCounts.provider_industry}`);
    if (Number(newsCount) > 0 && !sourceCounts.announcement_keyword) parts.push(`公告/新闻${newsCount}`);
    return parts.length ? `来源：${parts.join("，")}` : "来源：待核验";
}

async function loadThemeMapStatus() {
    const note = document.getElementById("themeMapNote");
    const panel = document.getElementById("themeMapPanel");
    if (note) note.textContent = "正在读取本地 stock_theme_map.csv 状态；不会联网。";
    try {
        const [status, preview, draft] = await Promise.all([
            requestJson("/api/theme-map/status"),
            requestJson("/api/theme-map/preview?limit=10"),
            requestJson("/api/theme-map/import-draft/preview?limit=8"),
        ]);
        renderThemeMapPanel(status, preview, draft);
        if (note) note.textContent = safeText(status.message_for_ui || status.message);
    } catch (error) {
        if (panel) panel.innerHTML = missingBlock(`板块/题材映射状态暂时无法加载：${escapeHtml(errorText(error))}`);
        if (note) note.textContent = "板块/题材映射状态读取失败；不影响日线研究层继续使用。";
    }
}

function renderThemeMapPanel(status, preview, draft) {
    const panel = document.getElementById("themeMapPanel");
    if (!panel) return;
    const current = status.current_candidate_theme_status || {};
    const visible = status.visible_candidate_theme_status || {};
    const mapped = numericValue(status.mapped_count ?? status.total_mapped) ?? 0;
    const total = numericValue(status.total_rows ?? status.total) ?? 0;
    const pending = numericValue(status.pending_count) ?? 0;
    const currentMapped = numericValue(current.mapped_current_candidates) ?? 0;
    const currentTotal = numericValue(current.candidate_count) ?? 0;
    const currentUnknown = numericValue(current.unknown_current_candidates) ?? Math.max(0, currentTotal - currentMapped);
    const visibleMapped = numericValue(visible.mapped_visible_candidates) ?? 0;
    const visibleTotal = numericValue(visible.candidate_count) ?? 0;
    const visibleUnknown = numericValue(visible.unknown_visible_candidates) ?? Math.max(0, visibleTotal - visibleMapped);
    const networkProviderCount = numericValue(status.network_provider_count ?? status.akshare_count) ?? 0;
    const providerHint = `东方财富 ${safeText(status.eastmoney_count ?? 0)}；AKShare/逐股 ${safeText(status.akshare_count ?? 0)}；上次来源：${displayText(status.last_update_source)}`;
    const previewRows = Array.isArray(preview.rows) ? preview.rows : [];
    const draftRows = Array.isArray(draft.rows) ? draft.rows : [];
    const currentMissingText = themeMapMissingSymbolsText(current.missing_symbols);
    const visibleMissingText = themeMapMissingSymbolsText(visible.missing_symbols);
    panel.innerHTML = `
        <div class="tag-row">
            <span class="tag tag-watch">Research Only</span>
            <span class="tag">No Broker Route</span>
            <span class="tag">Not Investment Advice</span>
            <span class="tag">显式联网才刷新外部数据源</span>
        </div>
        <div class="theme-map-metrics">
            ${themeMapMetric("正式映射", `${mapped}/${total}`, themeCoverageText(mapped, total))}
            ${themeMapMetric("待补", `${pending}只`, "写入 stock_theme_map_pending.csv")}
            ${themeMapMetric("当前重点覆盖", `${currentMapped}/${currentTotal}`, safeText(current.reason))}
            ${themeMapMetric("页面候选覆盖", `${visibleMapped}/${visibleTotal}`, safeText(visible.reason))}
            ${themeMapMetric("联网来源", `${networkProviderCount}只`, providerHint)}
            ${themeMapMetric("本地/手工导入", `${safeText(status.manual_import_count)}只`, safeText(status.import_path))}
        </div>
        ${status.source_note ? `<div class="gap-panel"><strong>来源说明</strong><div>${escapeHtml(displayText(status.source_note))}</div></div>` : ""}
        ${currentUnknown ? `<div class="gap-panel"><strong>当前重点题材缺口</strong><div>当前重点正式题材映射 ${escapeHtml(safeText(currentMapped))} / ${escapeHtml(safeText(currentTotal))}，仍有 ${escapeHtml(safeText(currentUnknown))} 只未知${currentMissingText ? `：${escapeHtml(currentMissingText)}` : ""}。这是题材映射缺口，不代表日线行情缺失；请生成导入草稿后导入正式映射，或显式联网补全。</div></div>` : ""}
        ${visibleUnknown ? `<div class="gap-panel"><strong>页面候选池题材缺口</strong><div>${escapeHtml(displayText(visible.message || visible.reason))}${visibleMissingText ? ` 未覆盖样本：${escapeHtml(visibleMissingText)}。` : ""}</div></div>` : ""}
        ${status.provider_warnings && !status.failed_reason ? `<div class="gap-panel"><strong>数据源诊断</strong><div>本轮部分外部数据源有超时/不可用记录，但正式映射已补齐；这些记录仅作为数据源稳定性诊断。${escapeHtml(displayText(status.provider_warnings))}</div></div>` : ""}
        ${status.failed_reason ? `<div class="gap-panel"><strong>映射缺口</strong><div>${escapeHtml(displayText(status.failed_reason))}</div></div>` : ""}
        <div class="theme-map-grid">
            <section class="theme-map-subpanel">
                <h3>正式映射预览</h3>
                ${compactRowsTable(previewRows, [
                    ["symbol", "代码"],
                    ["name", "名称"],
                    ["primary_theme", "主线题材"],
                    ["industry", "行业"],
                    ["source", "来源"],
                ], "暂无正式映射预览。")}
            </section>
            <aside class="theme-map-subpanel">
                <h3>数据源结果</h3>
                ${renderThemeProviderResults(status.provider_results || [])}
            </aside>
        </div>
        <div class="theme-map-subpanel">
            <h3>导入草稿预览</h3>
            ${compactRowsTable(draftRows, [
                ["symbol", "代码"],
                ["name", "名称"],
                ["primary_theme", "主线题材"],
                ["source", "来源"],
                ["notes", "备注"],
            ], draft.exists === false ? "导入草稿尚不存在。点击“生成导入草稿”后会创建 stock_theme_map_import.csv。" : "暂无导入草稿内容。")}
        </div>
    `;
}

function themeMapMetric(label, value, hint) {
    return `
        <article class="theme-map-metric">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(safeText(value))}</strong>
            <p class="metric-hint">${escapeHtml(safeText(hint))}</p>
        </article>
    `;
}

function themeMapMissingSymbolsText(symbols) {
    if (!Array.isArray(symbols) || !symbols.length) return "";
    const sample = symbols.filter(Boolean).slice(0, 8).map((item) => safeText(item)).join("、");
    const more = symbols.length > 8 ? ` 等 ${symbols.length} 只` : "";
    return `${sample}${more}`;
}

function themeCoverageText(mapped, total) {
    if (!total) return "尚无可统计股票。";
    return `覆盖率 ${((mapped / total) * 100).toFixed(1)}%；映射越完整，题材归因越少退回行业线索。`;
}

function renderThemeProviderResults(results) {
    if (!Array.isArray(results) || !results.length) {
        return `<div class="empty-state">暂无数据源结果。点击“本地重建”或“显式联网补全”后会显示。</div>`;
    }
    return `<div class="provider-list">${results.slice(0, 8).map((item) => `
        <div class="provider-list-item">
            <strong>${escapeHtml(providerDisplayName(item.provider))} · ${escapeHtml(safeText(item.status))}</strong>
            <span>映射 ${escapeHtml(safeText(item.mapped ?? 0))} 只</span>
            <span class="section-note">${escapeHtml(displayText(item.failed_reason || item.message))}</span>
        </div>
    `).join("")}</div>`;
}

function compactRowsTable(rows, columns, emptyText) {
    if (!Array.isArray(rows) || !rows.length) return missingBlock(emptyText);
    return `
        <div style="overflow:auto">
            <table class="compact-table">
                <thead>
                    <tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>
                </thead>
                <tbody>
                    ${rows.map((row) => `
                        <tr>
                            ${columns.map(([key]) => `<td>${escapeHtml(safeText(row[key]))}</td>`).join("")}
                        </tr>
                    `).join("")}
                </tbody>
            </table>
        </div>
    `;
}

async function refreshThemeMap(source) {
    const isNetwork = source === "auto" || source === "akshare";
    const button = document.getElementById(isNetwork ? "refreshThemeMapNetworkButton" : "rebuildThemeMapLocalButton");
    const note = document.getElementById("themeMapNote");
    if (button) {
        button.disabled = true;
        button.textContent = isNetwork ? "正在联网补全…" : "正在本地重建…";
    }
    if (note) {
        note.textContent = isNetwork
            ? "正在显式联网补全概念/题材映射；只写入本地 CSV，不接券商。"
            : "正在从本地快照、低价池和已有映射中重建题材映射。";
    }
    try {
        const payload = await postJson("/api/theme-map/refresh", { source, network: isNetwork });
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message);
        await loadThemeMapStatus();
        await loadLimitUpReview(false, true);
        await loadCandidates();
    } catch (error) {
        if (note) note.textContent = `题材映射刷新失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = isNetwork ? "显式联网补全" : "本地重建";
        }
    }
}

async function generateThemeMapDraft() {
    const button = document.getElementById("generateThemeMapDraftButton");
    const note = document.getElementById("themeMapNote");
    if (button) {
        button.disabled = true;
        button.textContent = "正在生成草稿…";
    }
    if (note) note.textContent = "正在生成本地导入草稿；默认不联网。";
    try {
        const payload = await postJson("/api/theme-map/import-draft", { source: "current", overwrite: true });
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message);
        await loadThemeMapStatus();
    } catch (error) {
        if (note) note.textContent = `导入草稿生成失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "生成导入草稿";
        }
    }
}

async function importThemeMapDraft() {
    const button = document.getElementById("importThemeMapButton");
    const note = document.getElementById("themeMapNote");
    if (button) {
        button.disabled = true;
        button.textContent = "正在导入草稿…";
    }
    if (note) note.textContent = "正在导入本地 stock_theme_map_import.csv；不联网，不接券商。";
    try {
        const payload = await postJson("/api/theme-map/import", { input: "data/universe/stock_theme_map_import.csv" });
        if (note) note.textContent = safeText(payload.message_for_ui || payload.message);
        await loadThemeMapStatus();
        await loadLimitUpReview(false, true);
        await loadCandidates();
    } catch (error) {
        if (note) note.textContent = `导入草稿失败：${escapeHtml(errorText(error))}`;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "导入草稿";
        }
    }
}

function renderLimitIntersectionList(items) {
    if (!items.length) return `<div class="empty-state">今日涨停池暂未命中当前 Quant 候选。</div>`;
    return `<div class="limit-intersection-list">${items.map((item) => `
        <div class="limit-intersection-item">
            <strong>${escapeHtml(safeText(item.name))} ${escapeHtml(safeText(item.symbol))}</strong>
            <div class="tag-row">
                <span class="tag">${escapeHtml(safeText(item.strategy_lane))}</span>
                <span class="tag">${escapeHtml(safeText(item.limit_up_theme))}</span>
                <span class="tag tag-${toneClass(item.fit_label === "符合入选假设" ? "ok" : item.fit_label === "偏离入选假设" ? "warn" : "watch")}">${escapeHtml(safeText(item.fit_label))}</span>
            </div>
            <div class="section-note">${escapeHtml(safeText(item.reason_reference))}</div>
        </div>
    `).join("")}</div>`;
}

async function updateCurrentCandidates() {
    const button = document.getElementById("updateCandidatesButton");
    const panel = document.getElementById("candidateGapPanel");
    if (button) {
        button.disabled = true;
        button.textContent = "收盘更新中…";
    }
    if (panel) {
        panel.innerHTML = "<strong>全 A 收盘更新中</strong>";
    }
    try {
        const payload = await postJson("/api/data/update-all", { explicit_network: true });
        if (panel) {
            panel.innerHTML = `<strong>${payload.ok === true ? "更新完成" : "更新未完成"}</strong><div>${escapeHtml(safeText(payload.message_for_ui || payload.message))}</div>`;
        }
        await loadStartupUpdateStatus();
        await loadMarketOverview();
        await loadCandidates();
        return payload;
    } catch (error) {
        if (panel) {
            panel.innerHTML = `<strong>更新失败</strong><div>${escapeHtml(errorText(error))}</div>`;
        }
        return null;
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "收盘更新";
        }
    }
}

// The dock and settings share the same explicit Pipeline update.
async function runGlobalDataUpdate() {
    const button = document.getElementById("globalUpdateButton");
    const original = "收盘更新";
    if (button) {
        button.disabled = true;
        button.textContent = "更新中…";
    }
    try {
        const result = await updateCurrentCandidates();
        if (button) button.textContent = result?.ok === true ? "已更新" : "未完成";
    } finally {
        if (button) {
            button.disabled = false;
            window.setTimeout(() => { button.textContent = original; }, 2500);
        }
    }
}

async function generateDailyRecommendations() {
    const button = document.getElementById("generateRecommendationsButton");
    const panel = document.getElementById("candidateGenerationPanel");
    if (button) {
        button.disabled = true;
        button.textContent = "正在生成今日研究候选…";
    }
    if (panel) {
        panel.innerHTML = "<strong>正在生成今日研究候选</strong><div>读取本地完整日线和研究策略；不扫描全 A，不接券商，不下单。</div>";
    }
    try {
        const payload = await postJson("/api/daily/generate-recommendations", {
            max_scan_symbols: 300,
            max_candidates: 18,
            sync_paper: true,
        });
        if (panel) {
            panel.innerHTML = `<strong>生成完成</strong><div>${escapeHtml(safeText(payload.message_for_ui || payload.message))}</div>`;
        }
        agentStatusLoaded = false;
        await Promise.all([
            loadStartupUpdateStatus(),
            loadCandidates(),
            loadPaperWatchlist(true),
            loadStrategyScorecard(),
        ]);
    } catch (error) {
        if (panel) {
            panel.innerHTML = `<strong>生成失败</strong><div>${escapeHtml(errorText(error))}</div>`;
        }
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "生成今日研究候选";
        }
    }
}

/* ============================================================
   Duty status strip (dark cockpit): aggregates state already
   fetched by existing loaders — zero extra requests.
   tone: "" = normal (ink), "warn" = attention, "bad" = error.
   ============================================================ */
const dutyState = {};
let dutyCandidateGeneration = null;

function feedDutyLamp(key, value, sub, tone = "") {
    if (key === "batch" && dutyCandidateGeneration) {
        const generation = dutyCandidateGeneration;
        const current = generation.freshness_status === "current" && ["passed", "completed", "completed_with_warnings"].includes(generation.status);
        value = current ? `正式候选 ${generation.candidate_count ?? 0} 只` : "候选批次待核实";
        sub = `${generation.signal_date || "日期未知"} · ${generation.freshness_label || generation.status || "缺失"}`;
        tone = current ? "" : "warn";
    }
    dutyState[key] = { value: safeText(value), sub: safeText(sub), tone };
    renderStatusStrip();
}

function renderStatusStrip() {
    const strip = document.getElementById("dutyStatusStrip");
    if (!strip) return;
    for (const lamp of strip.querySelectorAll("[data-duty-lamp]")) {
        const state = dutyState[lamp.dataset.dutyLamp];
        if (!state) continue;
        lamp.classList.toggle("warn", state.tone === "warn");
        lamp.classList.toggle("bad", state.tone === "bad");
        const valueEl = lamp.querySelector(".duty-lamp-value");
        const subEl = lamp.querySelector(".duty-lamp-sub");
        if (valueEl) valueEl.textContent = state.value;
        if (subEl) subEl.textContent = state.sub;
    }
}

function setupDutyStrip() {
    const strip = document.getElementById("dutyStatusStrip");
    if (!strip) return;
    strip.addEventListener("click", (event) => {
        const lamp = event.target.closest("[data-duty-lamp]");
        if (!lamp) return;
        const target = document.getElementById(lamp.dataset.dutyTarget || "");
        if (!target) return;
        const jump = () => document.getElementById(lamp.dataset.dutyTarget || "")?.scrollIntoView({ behavior: "smooth", block: "start" });
        if (typeof window.quantdeskShowPage === "function" && document.body.dataset.page !== "system") {
            window.quantdeskShowPage("system");
            window.requestAnimationFrame(() => window.requestAnimationFrame(jump));
        } else {
            jump();
        }
    });
}

async function loadStartupUpdateStatus() {
    const panel = document.getElementById("startupUpdateStatus");
    if (!panel) return;
    try {
        const data = await requestJson("/api/startup/update-status");
        renderStartupUpdateStatus(data);
        syncStartupStatusPolling(data);
    } catch (error) {
        panel.className = "startup-update startup-update-warning";
        panel.innerHTML = `
            <strong>启动更新状态暂时无法读取</strong>
            <div>页面会继续使用本地缓存数据；请确认服务仍在运行。</div>
            <div class="startup-update-meta">
                <span>Research Only</span>
                <span>No Broker Route</span>
                <span>Not Investment Advice</span>
            </div>
        `;
        feedDutyLamp("batch", "缺失", "启动状态不可读", "bad");
    }
}

function startupFailureDetailsHtml(details) {
    const items = Array.isArray(details) ? details.filter((item) => item && hasValue(item.message)) : [];
    if (!items.length) return "";
    return `<div class="startup-update-explain"><strong>本轮未完成事项</strong><ul>${items.map((item) => `<li>${escapeHtml(safeText(item.message))}</li>`).join("")}</ul></div>`;
}

function renderStartupUpdateStatus(data) {
    const panel = document.getElementById("startupUpdateStatus");
    if (!panel) return;
    const explanation = data.startup_update_explanation || {};
    const diagnosis = data.provider_diagnosis || explanation.provider_diagnosis || {};
    const actionableReasons = new Set(["recommendation_stale", "daily_gap", "provider_partial_daily_gap", "provider_failed", "not_attempted", "network_not_authorized"]);
    const actionHtml = actionableReasons.has(safeText(explanation.reason_code)) ? `
        <div class="startup-update-actions">
            <button class="action-button" type="button" data-research-loop-refresh>刷新研究闭环</button>
            <span>查看全 A 收盘更新和当前研究结果；未完成的步骤会保留说明。</span>
        </div>
    ` : "";
    const failedSymbols = Array.isArray(diagnosis.failed_symbols_sample) ? diagnosis.failed_symbols_sample.filter(Boolean).slice(0, 8) : [];
    const diagnosisHtml = hasValue(diagnosis.summary) ? `
        <div class="startup-update-explain">
            <strong>${escapeHtml(safeText(diagnosis.label || "外部数据源刷新诊断"))}</strong>
            <span>${escapeHtml(safeText(diagnosis.summary))}</span>
            ${failedSymbols.length ? `<span>未更新样本：${failedSymbols.map((item) => escapeHtml(item)).join("、")}</span>` : ""}
            ${hasValue(diagnosis.next_action) ? `<span>处理方式：${escapeHtml(safeText(diagnosis.next_action))}</span>` : ""}
        </div>
    ` : "";
    const explanationHtml = `
        <div class="startup-update-explain">
            <strong>${escapeHtml(safeText(explanation.reason_label || "自动更新状态"))}</strong>
            <span>${escapeHtml(safeText(explanation.summary || data.current_message || data.message || "等待数据状态确认。"))}</span>
            <span>下一步：${escapeHtml(safeText(explanation.next_action || "查看数据状态或手动刷新。"))}</span>
            ${actionHtml}
        </div>
        ${diagnosisHtml}
        ${startupFailureDetailsHtml(data.failure_details)}
    `;
    if (data.current_view_override === true && ["completed", "completed_all_executed"].includes(safeText(data.status))) {
        const summary = data.current_candidate_gap_summary || {};
        const generation = data.candidate_generation || {};
        const title = safeText(data.current_status_label || "当前候选已补齐");
        const targetDate = startupTargetDateText(data, summary, generation, data.update_result || {});
        const tone = safeText(data.current_status_tone || "");
        const fieldGapLabel = safeText(summary.field_gap_label || "成交额/量能字段缺口");
        const generationFreshness = safeText(generation.freshness_label || "研究候选批次状态缺失");
        const generationMessage = safeText(generation.freshness_message || generation.message_for_ui || "");
        const runningHint = data.running === true ? `<div>后台刷新仍在运行；下方以当前候选池复核结果为准，完成后页面会重新读取候选、纸面池和策略成绩单。</div>` : "";
        panel.className = `startup-update ${tone === "ok" ? "startup-update-ok" : "startup-update-warning"}`;
        panel.innerHTML = `
            <div class="startup-update-line">
                <strong>当前候选数据：${escapeHtml(title)}</strong>
                <span class="startup-update-brief">${escapeHtml(safeText(data.current_message || summary.message))}</span>
            </div>
            <details class="startup-update-details">
                <summary>详情与处理建议</summary>
                ${runningHint}
                ${explanationHtml}
                <div>今日候选池：${escapeHtml(safeText(generation.signal_date || "未生成"))} · ${escapeHtml(safeText(generation.candidate_count ?? 0))} 只；${escapeHtml(generationFreshness)}。</div>
                ${generationMessage ? `<div>${escapeHtml(generationMessage)}</div>` : ""}
                <div class="section-note">以下为不同研究池的候选数量，非同一池子的重复计数。</div>
                <div class="startup-update-meta">
                    <span>目标交易日：${escapeHtml(targetDate)}</span>
                    <span>价格/K线范围：${escapeHtml(safeText(data.current_candidate_latest_date_min))} → ${escapeHtml(safeText(data.current_candidate_latest_date_max))}</span>
                    <span>研究候选批次：${escapeHtml(safeText(summary.candidate_count ?? 0))} 只</span>
                    <span>可读：${escapeHtml(safeText(summary.fresh_count ?? 0))}</span>
                    <span>日线问题：${escapeHtml(safeText(summary.gap_count ?? 0))}</span>
                    <span>${escapeHtml(fieldGapLabel)}（当前候选池口径）：${escapeHtml(safeText(summary.field_gap_count ?? 0))}</span>
                    <span>研究候选批次：${escapeHtml(generationFreshness)}</span>
                </div>
                <div class="startup-update-meta">
                    <span>Research Only</span>
                    <span>Paper Account Only</span>
                    <span>No Broker Route</span>
                    <span>Live Execution Locked</span>
                    <span>QMT Order Route Locked</span>
                    <span>Not Investment Advice</span>
                    <span>Not Position Advice</span>
                </div>
            </details>
        `;
        feedDutyLamp("batch", title, generationFreshness, tone === "ok" ? "" : "warn");
        return;
    }
    const status = safeText(data.status);
    const update = data.update_result || {};
    const updated = safeText(data.updated_symbols ?? update.updated_symbols ?? 0);
    const failed = safeText(data.failed_symbols ?? update.failed_symbols ?? 0);
    const stale = safeText(data.stale_count_after ?? update.stale_count_after ?? 0);
    const missing = safeText(data.missing_count_after ?? update.missing_count_after ?? 0);
    const summary = data.current_candidate_gap_summary || {};
    const generation = data.candidate_generation || {};
    const targetDate = startupTargetDateText(data, summary, generation, update);
    const latestDate = preferredDateText(
        data.current_candidate_latest_date_max,
        update.latest_local_date_after,
        data.latest_local_date_after,
        data.current_candidate_latest_date_min,
        data.startup_update_explanation?.latest_local_date
    );
    const attempted = data.startup_update_attempted ? "已尝试联网补全" : "未执行联网补全";
    const network = data.provider_network_authorized ? "本次启动已显式授权网络数据更新" : "本次启动未授权网络数据更新";
    const generationFreshness = safeText(generation.freshness_label || "研究候选批次状态缺失");
    const generationMessage = safeText(generation.freshness_message || generation.message_for_ui || "");
    const warning = !["completed", "completed_all_executed"].includes(status);
    panel.className = `startup-update ${warning ? "startup-update-warning" : "startup-update-ok"}`;
    panel.innerHTML = `
        <div class="startup-update-line">
            <strong>启动数据更新：${escapeHtml(humanStatus(status))}</strong>
            <span class="startup-update-brief">${escapeHtml(attempted)}。${escapeHtml(network)}；完整日线由全 A 收盘更新流程维护。</span>
        </div>
        <details class="startup-update-details">
            <summary>详情与处理建议</summary>
            ${explanationHtml}
            <div class="startup-update-meta">
                <span>目标交易日：${escapeHtml(targetDate)}</span>
                <span>本地最新：${escapeHtml(latestDate)}</span>
                <span>更新：${escapeHtml(updated)}</span>
                <span>失败：${escapeHtml(failed)}</span>
                <span>滞后：${escapeHtml(stale)}</span>
                <span>缺失：${escapeHtml(missing)}</span>
                <span>今日候选池：${escapeHtml(safeText(generation.candidate_count ?? 0))} 只</span>
                <span>研究候选批次：${escapeHtml(generationFreshness)}</span>
            </div>
            <div class="startup-update-meta">
                <span>Research Only</span>
                <span>Paper Account Only</span>
                <span>No Broker Route</span>
                <span>Live Execution Locked</span>
                <span>QMT Order Route Locked</span>
                <span>Not Investment Advice</span>
                <span>Not Position Advice</span>
            </div>
            ${hasValue(data.message) ? `<div>${escapeHtml(data.message)}</div>` : ""}
            ${generationMessage ? `<div>${escapeHtml(generationMessage)}</div>` : ""}
        </details>
    `;
    feedDutyLamp("batch", humanStatus(status), generationFreshness, warning ? "warn" : "");
}

function startStartupStatusPolling() {
    if (startupStatusPollTimer) return;
    startupStatusPollTimer = window.setInterval(() => loadStartupUpdateStatus(), 5000);
    window.setTimeout(() => stopStartupStatusPolling(), 180000);
}

function stopStartupStatusPolling() {
    if (!startupStatusPollTimer) return;
    window.clearInterval(startupStatusPollTimer);
    startupStatusPollTimer = null;
    // Safety net: if polling ends (e.g. the 180s cap) while a refresh we kicked
    // off was never observed completing, reload once anyway so the candidate
    // list still reflects the latest local data instead of staying stale.
    if (startupRefreshTaskSeen && !startupRefreshApplied) {
        startupRefreshTaskSeen = false;
        void refreshAfterStartupUpdate();
    }
}

function syncStartupStatusPolling(data) {
    const status = safeText(data?.status);
    const running = data?.running === true || status === "running";
    const taskId = safeText(data?.request_id || data?.task_id || data?.started_at || data?.updated_at || "");
    const firstObservation = !startupStatusObserved;
    startupStatusObserved = true;
    if (firstObservation && !running && !startupRefreshTaskSeen && !researchLoopRefreshManual) {
        // A saved terminal receipt is the initial baseline, not a completion
        // observed by this page. It must not fan out a second startup load.
        startupRefreshTaskId = taskId;
        stopStartupStatusPolling();
        return;
    }

    if (running) {
        // A refresh is in flight. Remember it and keep polling until it ends.
        startupRefreshTaskSeen = true;
        startupRefreshApplied = false;
        if (taskId) startupRefreshTaskId = taskId;
        startStartupStatusPolling();
        return;
    }

    // Terminal status. Is this the completion of the refresh we are waiting on?
    const matchesPending = !startupRefreshPendingRequestId || taskId === startupRefreshPendingRequestId;
    const isNewCompletion = Boolean(taskId) && taskId !== startupRefreshTaskId;

    if ((startupRefreshTaskSeen || isNewCompletion) && !startupRefreshApplied && matchesPending) {
        startupRefreshTaskSeen = false;
        if (taskId) startupRefreshTaskId = taskId;
        void refreshAfterStartupUpdate();
        stopStartupStatusPolling();
        return;
    }

    // Terminal but not yet our refresh (stale state before the worker flips to
    // running, or the request id hasn't propagated). Keep polling while we are
    // still expecting our refresh to land; never stop early — that was the bug
    // that left the page showing pre-refresh data.
    if (startupRefreshTaskSeen && !startupRefreshApplied) {
        startStartupStatusPolling();
    } else {
        stopStartupStatusPolling();
    }
}

async function triggerStartupRefresh(options = {}) {
    const manual = options.manual === true;
    if (!manual) {
        await loadStartupUpdateStatus();
        return;
    }
    try {
        startupRefreshApplied = false;
        startupRefreshTaskSeen = true;
        researchLoopRefreshManual = manual;
        const result = await postJson("/api/startup/refresh", { network: manual });
        startupRefreshPendingRequestId = safeText(result?.request_id || "");
        await loadStartupUpdateStatus();
        startStartupStatusPolling();
    } catch (error) {
        console.warn("[startup] background refresh trigger failed", error);
        startupRefreshTaskSeen = false;
        startupRefreshPendingRequestId = "";
        researchLoopRefreshManual = false;
        if (manual) finishResearchLoopRefreshButton("刷新失败");
    }
}

async function refreshResearchLoop(manual = false) {
    const button = document.getElementById("refreshResearchLoopButton");
    if (button) {
        button.disabled = true;
        button.textContent = "刷新中…";
    }
    researchLoopRefreshManual = Boolean(manual);
    await triggerStartupRefresh({ manual: Boolean(manual) });
}

function finishResearchLoopRefreshButton(label = "已刷新") {
    const button = document.getElementById("refreshResearchLoopButton");
    if (!button) return;
    button.textContent = label;
    window.setTimeout(() => {
        button.disabled = false;
        button.textContent = "刷新研究闭环";
    }, 2500);
}

async function refreshAfterStartupUpdate() {
    if (startupRefreshApplied) return;
    startupRefreshApplied = true;
    startupRefreshPendingRequestId = "";
    try {
        await loadVisiblePageData();
        agentStatusLoaded = false;
        if (researchLoopRefreshManual) finishResearchLoopRefreshButton("已刷新闭环");
    } catch (error) {
        console.warn("[startup] post-refresh UI reload failed", error);
        if (researchLoopRefreshManual) finishResearchLoopRefreshButton("刷新后读取失败");
    } finally {
        researchLoopRefreshManual = false;
    }
}

async function loadMarketOverview() {
    try {
        const data = await requestJson("/api/daily/overview");
        renderMarketOverview(data);
        lastMarketReadSucceeded = true;
    } catch (error) {
        if (lastMarketReadSucceeded) {
            const note = document.getElementById("marketDataNote");
            if (note) note.textContent = "本次读取失败，保留上次市场结果及其原证据日期；可重新读取。";
            return;
        }
        document.getElementById("marketOverview").innerHTML = missingBlock("市场数据暂时无法加载。请确认服务仍在运行，并检查本地 data/benchmark 与 data/cache。");
        document.getElementById("sectorBoard").innerHTML = missingBlock("板块数据暂时无法加载。");
        document.getElementById("marketSentiment").textContent = "市场情绪无法生成：本地接口没有返回有效数据。";
        renderHomeSectorChart([], []);
    }
}

function renderMarketOverview(data) {
    const overview = document.getElementById("marketOverview");
    const note = document.getElementById("marketDataNote");
    const hs300 = data.hs300 || {};
    const shIndex = data.sh_index || {};
    const breadth = data.market_breadth || {};
    const dates = marketEvidenceDates(data);
    const metrics = [
        {
            label: "沪深300",
            value: formatNumber(hs300.value, 2),
            change: hs300.change,
            hint: `行情日 ${dates.hs300 || "未核实"} · ${indexMetricHint(hs300)}`,
        },
        {
            label: "上证指数",
            value: formatNumber(shIndex.value, 2),
            change: shIndex.change,
            hint: `行情日 ${dates.sh_index || "未核实"} · ${indexMetricHint(shIndex)}`,
        },
        {
            label: "本地样本成交额",
            value: safeText(breadth.total_amount),
            change: `${safeText(breadth.advancing_count)}涨 / ${safeText(breadth.declining_count)}跌`,
            hint: `行情日 ${dates.breadth || "未核实"} · ${safeText(breadth.hint)}`,
        },
    ];
    overview.innerHTML = metrics.map((item) => `
        <article class="metric">
            <div class="metric-label">${escapeHtml(item.label)}</div>
            <div class="metric-value">${escapeHtml(item.value)}</div>
            <div class="metric-change ${changeClass(item.change)}">${escapeHtml(formatChange(item.change))}</div>
            <p class="metric-hint">${escapeHtml(item.hint)}</p>
        </article>
    `).join("");
    document.getElementById("marketSentiment").textContent = `${safeText(data.market_sentiment)} · ${dates.as_of ? `统一行情日 ${dates.as_of}` : "各项行情日未统一或未核实，见各卡片"}`;
    if (note) note.textContent = [safeText(data.update_hint), `本地样本来源 ${recordedEvidenceText(breadth.source)}`,
        `抓取时间 ${recordedEvidenceText(breadth.fetched_at)}（仅表示采集，不能当成行情日）`].join(" · ");
    const sectorSnapshot = { ...data.sector_snapshot, as_of: dates.sectors };
    renderSectorBoard(data.sector_top_gainers || [], data.sector_top_losers || [], sectorSnapshot);
    renderHomeSectorChart(data.sector_top_gainers || [], data.sector_top_losers || [], sectorSnapshot);
}

function recordedEvidenceText(value, fallback = "未记录") {
    return typeof value === "string" && value.trim() || typeof value === "number" && Number.isFinite(value)
        ? String(value) : fallback;
}

function marketBusinessDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "";
    const parsed = new Date(value + "T00:00:00Z");
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : "";
}

function marketEvidenceDates(data) {
    const dates = {
        hs300: marketBusinessDate(data?.market_dates?.hs300) || marketBusinessDate(data?.hs300?.date),
        sh_index: marketBusinessDate(data?.market_dates?.sh_index) || marketBusinessDate(data?.sh_index?.date),
        breadth: marketBusinessDate(data?.market_dates?.breadth) || marketBusinessDate(data?.market_breadth?.quote_date),
        sectors: marketBusinessDate(data?.market_dates?.sectors) || marketBusinessDate(data?.sector_snapshot?.as_of),
    };
    const values = Object.values(dates);
    return { ...dates, as_of: values.every(Boolean) && new Set(values).size === 1 ? values[0] : "" };
}

function sectorSnapshotText(snapshot = {}) {
    const freshness = snapshot.freshness_status === "current" ? "当前" : snapshot.freshness_status === "stale" ? "已滞后" : "未核实";
    return `行业板块行情日 ${marketBusinessDate(snapshot.as_of) || "未核实"} · 新鲜度 ${freshness} · 来源 ${recordedEvidenceText(snapshot.source)} · 缓存修改时间 ${recordedEvidenceText(snapshot.cache_modified_at)}（文件时间不能当成行情日）`;
}

function indexMetricHint(indexData) {
    if (indexData?.amount_status === "available") {
        return `成交额：${safeText(indexData.amount)}。${safeText(indexData.hint)}`;
    }
    return safeText(indexData?.hint || "指数点位/涨跌已读取；指数成交额源暂未接入。");
}

function renderSectorBoard(gainers, losers, snapshot = {}) {
    const board = document.getElementById("sectorBoard");
    if (!board) return;
    board.innerHTML = `
        <p class="market-evidence-date">${escapeHtml(sectorSnapshotText(snapshot))}</p>
        <article class="sector-panel">
            <h3>板块涨幅 Top5</h3>
            ${renderSectorList(gainers)}
        </article>
        <article class="sector-panel">
            <h3>板块跌幅 Top5</h3>
            ${renderSectorList(losers)}
        </article>
    `;
}

function sectorHeatClass(change) {
    const n = numericValue(change);
    if (n === null || n === 0) return "";
    const mag = Math.abs(n);
    const level = mag >= 5 ? 4 : mag >= 3 ? 3 : mag >= 1.5 ? 2 : 1;
    return n > 0 ? `heat-up-${level}` : `heat-down-${level}`;
}

function renderSectorList(items) {
    if (!items.length) return missingBlock("板块数据缺失，请补充本地板块映射后刷新。");
    return `<ol class="sector-list">${items.slice(0, 5).map((item) => `
        <li class="${sectorHeatClass(item.change)}">
            <span class="sector-name">${escapeHtml(item.name)}</span>
            <strong class="sector-change ${changeClass(item.change)}">${escapeHtml(formatChange(item.change))}</strong>
        </li>
    `).join("")}</ol>`;
}

async function loadCandidates() {
    const isCurrent = beginReadRender("candidates");
    try {
        const data = await requestJson("/api/daily/candidates");
        if (!isCurrent()) return;
        currentCandidates = data.all_candidates || data.candidates || [];
        currentTodayRecommendations = data.today_recommendations || [];
        currentObservationCandidates = data.observation_candidates || [];
        rememberWorkbenchSource("research_candidates", currentCandidates);
        document.getElementById("candidateDataNote").textContent = safeText(data.message);
        renderCandidateGenerationPanel(data.candidate_generation || {});
        renderCandidateConfidencePanel({ ...data.candidate_confidence_summary, batch_date: data.candidate_generation?.signal_date });
        renderDataGapPanel({ ...data.data_gap_summary, batch_date: data.candidate_generation?.signal_date });
        renderDailyFocus(data.daily_focus || []);
        renderQuantDailySummary(data.quant_daily_summary || {});
        renderCandidateGroups(data.candidate_groups || [], currentCandidates, data.candidate_pool_policy || {});
        renderHomeCandidates(data);
        lastCandidatesReadSucceeded = true;
    } catch (error) {
        if (!isCurrent()) return;
        if (lastCandidatesReadSucceeded) {
            const note = document.getElementById("candidateDataNote");
            if (note) note.textContent = "本次读取失败，保留上次候选及原批次日期；可重新读取，不作为新批次。";
            return;
        }
        document.getElementById("dailyFocusGrid").innerHTML = missingBlock("每日重点复核暂时无法加载。请确认本地候选追踪文件存在。");
        document.getElementById("candidatesGrid").innerHTML = missingBlock("候选股票池暂时无法加载。请确认 data/tracking/candidate_ledger.csv 存在。");
        const generationPanel = document.getElementById("candidateGenerationPanel");
        if (generationPanel) generationPanel.innerHTML = missingBlock("今日研究候选暂时无法加载。");
        document.querySelectorAll("[data-candidate-confidence-panel]").forEach((panel) => {
            panel.innerHTML = missingBlock("候选可信度总览暂时无法加载。");
        });
        document.getElementById("candidateGapPanel").innerHTML = `<strong>数据缺口状态</strong><div>候选接口暂时不可用，无法汇总缺失原因。</div>`;
        const summaryPanel = document.getElementById("quantSummaryPanel");
        if (summaryPanel) summaryPanel.innerHTML = missingBlock("Quant 日总结暂时无法加载。");
        renderHomeCandidates(null);
    }
}

function renderCandidateGenerationPanel(generation) {
    dutyCandidateGeneration = generation && generation.signal_date ? generation : null;
    feedDutyLamp("batch", humanStatus(generation.status || "missing"), generation.freshness_label || "研究候选批次缺失", "warn");
    const panel = document.getElementById("candidateGenerationPanel");
    if (!panel) return;
    const status = safeText(generation.status || "missing");
    const tone = status === "completed" ? "ok" : status === "completed_with_warnings" ? "warning" : "missing";
    const freshness = safeText(generation.freshness_label || "研究候选批次状态缺失");
    const freshnessMessage = safeText(generation.freshness_message || generation.message_for_ui || "尚未生成今日研究候选。");
    panel.innerHTML = `
        <div class="candidate-generation-head">
            <strong>今日候选池</strong>
            <span class="tag tag-${toneClass(tone)}">${escapeHtml(humanStatus(generation.status || "未生成"))}</span>
            <span class="tag tag-${toneClass(generation.freshness_status === "current" ? "ok" : "warning")}">${escapeHtml(freshness)}</span>
            <span class="tag">目标 ${escapeHtml(safeText(generation.target_date || "数据缺失"))}</span>
            <span class="tag">日期 ${escapeHtml(safeText(generation.signal_date || "数据缺失"))}</span>
            <span class="tag">今日候选池 ${escapeHtml(safeText(generation.candidate_count || 0))} 只</span>
            <span class="tag">历史观察 ${escapeHtml(safeText(currentObservationCandidates.length || 0))} 只</span>
        </div>
        <div class="section-note">${escapeHtml(freshnessMessage)}</div>
        <div class="section-note">今日候选：本批程序新筛选；历史观察：早前入选且继续跟踪；账本观察池：曾手动加入/同步的研究样本；纸面持仓：已模拟成交并占用资金。各池有独立日期和成员，同步观察不会直接成交。</div>
        <div class="section-note">本地扫描范围：${escapeHtml(safeText(generation.scope_label || "本地日线有界研究扫描"))}；长期 ${escapeHtml(safeText(generation.long_count || 0))} 只，打板 ${escapeHtml(safeText(generation.board_count || 0))} 只。</div>
    `;
}

function renderCandidateConfidencePanel(summary) {
    const panels = Array.from(document.querySelectorAll("[data-candidate-confidence-panel]"));
    if (!panels.length) return;
    const groups = Array.isArray(summary.status_groups) ? summary.status_groups : [];
    const gaps = Array.isArray(summary.top_data_gaps) ? summary.top_data_gaps : [];
    if (!safeText(summary.candidate_count || 0) || Number(summary.candidate_count || 0) <= 0) {
        const html = missingBlock("候选池为空，暂无可信度总览。系统不会为了凑结论而编造候选或数据。");
        panels.forEach((panel) => { panel.innerHTML = html; });
        return;
    }
    const verificationCounts = summary.verification_counts || {};
    const groupCount = (status) => {
        const group = groups.find((item) => item.status === status);
        return typeof group?.count === "number" ? group.count : "未核实";
    };
    const verificationText = Object.entries(verificationCounts)
        .slice(0, 4)
        .map(([label, count]) => `${safeText(label)} ${safeText(count)}`)
        .join("；") || "暂无验证状态";
    const html = `
        <div class="confidence-summary-head">
            <strong>资料完整度总览（纸面研究 · 未验证）</strong>
            <span class="tag">候选 ${escapeHtml(safeText(summary.candidate_count))} 只</span>
            <span class="tag">高可信资料 ${escapeHtml(safeText(groupCount("research_ready")))}</span>
            <span class="tag tag-warning">部分可用 ${escapeHtml(safeText(groupCount("partial")))}</span>
            <span class="tag tag-warning">降级 ${escapeHtml(safeText(groupCount("degraded")))} / 缺失 ${escapeHtml(safeText(groupCount("missing")))}</span>
        </div>
        <div class="section-note">本面板不计算胜率；资料完整度不能证明投资优势。候选批次日 ${escapeHtml(summary.batch_date || "未核实")}；包含本批及历史观察，成员以本页列表为准。</div>
        <div>${escapeHtml(safeText(summary.message))}</div>
        <div class="confidence-summary-grid">
            ${groups.map(renderConfidenceGroup).join("")}
        </div>
        <div class="section-note">验证状态：${escapeHtml(verificationText)}</div>
        <div class="section-note">${escapeHtml(safeText(summary.disclaimer || "可信度只说明本地证据强弱，不构成投资建议。"))}</div>
        <strong>最值得补的数据缺口（研究候选批次口径）</strong>
        <div class="confidence-gap-list">
            ${gaps.length ? gaps.map(renderConfidenceGap).join("") : `<div class="confidence-gap-item">未返回关键缺口清单，不据此认定资料完整。</div>`}
        </div>
    `;
    panels.forEach((panel) => { panel.innerHTML = html; });
}

function renderConfidenceGroup(group) {
    return `
        <article class="confidence-group">
            <strong>${escapeHtml(safeText(group.count ?? 0))}</strong>
            <span class="tag tag-${toneClass(group.tone || "watch")}">${escapeHtml(safeText(group.label))}</span>
            <div>${escapeHtml(safeText(group.explanation))}</div>
        </article>
    `;
}

function renderConfidenceGap(gap) {
    const symbols = Array.isArray(gap.symbols) ? gap.symbols.slice(0, 4).join("、") : "";
    const symbolText = symbols ? `影响：${symbols}` : "影响样本待确认";
    return `
        <article class="confidence-gap-item">
            <div class="confidence-gap-title">
                <span>${escapeHtml(safeText(gap.message || gap.label || gap.kind))}</span>
                <span class="tag tag-${toneClass(gap.severity === "error" ? "missing" : "warn")}">${escapeHtml(safeText(gap.count ?? 0))} 只</span>
            </div>
            <div class="confidence-gap-action">${escapeHtml(symbolText)}；${escapeHtml(safeText(gap.suggested_action))}</div>
        </article>
    `;
}

function renderDataGapPanel(summary) {
    const panel = document.getElementById("candidateGapPanel");
    if (!panel) return;
    const reasons = Array.isArray(summary.reasons) ? summary.reasons.slice(0, 4) : [];
    const fieldGapLabel = safeText(summary.field_gap_label || "成交额/量能字段缺口");
    const fieldGapCount = safeText(summary.field_gap_count ?? "未核实");
    const fieldGapExplain = safeText(summary.field_gap_explanation || "表示最新日线里缺 amount 等辅助字段，不等于价格/K线没更新。");
    const todayReady = summary.today_price_layer_ready === true ? "今日重点价格/K线可用" : "今日重点仍有日线缺口";
    panel.innerHTML = `
        <strong>日线问题：${escapeHtml(safeText(summary.gap_count ?? 0))} / ${escapeHtml(safeText(summary.candidate_count ?? 0))}</strong>
        <div class="section-note">批次日 ${escapeHtml(summary.batch_date || "未核实")}；范围：本页研究候选及历史观察，不是全A或持仓分钟覆盖。</div>
        <div>${escapeHtml(safeText(summary.message))}</div>
        <div>${escapeHtml(todayReady)}；今日重点 ${escapeHtml(safeText(summary.today_count ?? 0))} 只，历史观察 ${escapeHtml(safeText(summary.observation_count ?? 0))} 只。</div>
        <div>今日重点日线滞后 ${escapeHtml(safeText(summary.today_stale_count ?? 0))} / 缺少 ${escapeHtml(safeText(summary.today_missing_count ?? 0))}；历史观察日线滞后 ${escapeHtml(safeText(summary.observation_stale_count ?? 0))} / 缺少 ${escapeHtml(safeText(summary.observation_missing_count ?? 0))}。</div>
        <div>已可读价格/K线：${escapeHtml(safeText(summary.fresh_count ?? 0))}；全部日线滞后：${escapeHtml(safeText(summary.stale_count ?? 0))}；缺少日线：${escapeHtml(safeText(summary.missing_count ?? 0))}；${escapeHtml(fieldGapLabel)}：${escapeHtml(fieldGapCount)}（今日重点 ${escapeHtml(safeText(summary.today_field_gap_count ?? 0))}，历史观察 ${escapeHtml(safeText(summary.observation_field_gap_count ?? 0))}）。</div>
        <div>${escapeHtml(fieldGapExplain)}</div>
        ${reasons.length ? `<div>${reasons.map((item) => escapeHtml(item)).join("；")}</div>` : ""}
    `;
}

function candidateMatchedConditions(candidate) {
    if (Array.isArray(candidate.matched_conditions)) {
        return candidate.matched_conditions.map((item) => sanitizeTradingTerms(safeText(item))).filter(Boolean);
    }
    if (hasValue(candidate.matched_conditions)) {
        return String(candidate.matched_conditions).split(/[;；|]/).map((item) => sanitizeTradingTerms(item.trim())).filter(Boolean);
    }
    const fallback = firstSentence(candidate.reason_cn, candidate.select_reason || candidate.strategy_reason || "");
    return hasValue(fallback) ? [sanitizeTradingTerms(fallback)] : [];
}

function candidateReferencePrice(candidate) {
    const price = numericValue(candidate.suggested_entry_price ?? candidate.reference_price ?? candidate.current_price ?? candidate.close);
    return price === null ? "数据缺失" : numText(price, 2);
}

function candidatePaperState(candidate) {
    const raw = safeText(candidate.paper_status || candidate.paper_lifecycle_state || candidate.review_status || candidate.pool_status || "");
    const isInPool = candidate.in_paper_pool === true
        || hasValue(candidate.paper_watchlist_id)
        || hasValue(candidate.watchlist_id)
        || /paper|active|watch|观察|入池|跟踪/.test(raw);
    return {
        inPool: isInPool,
        label: raw && raw !== "数据缺失" ? humanizeStatusTerms(raw) : (isInPool ? "已入池" : "待入池"),
    };
}

function candidateMethodNote(candidate) {
    const bucket = candidateStrategyBucket(candidate);
    return bucket === "board" ? "日线近涨停近似 · 无封单队列证据"
        : bucket === "long" ? "MA20技术趋势 · 纸面未验证" : "";
}

function candidateExposureFacts(candidate) {
    const evidence = candidate?.exposure_evidence || {};
    const label = (value) => {
        const result = typeof value === "string" ? value.trim() : "";
        return /^(?:未知|未分组|数据缺失|无|unknown|none|null|nan|-)$/i.test(result) ? "" : result;
    };
    const industry = evidence.industry_status === "unknown" ? "" : label(candidate?.industry) || label(candidate?.sector);
    const theme = evidence.theme_status === "unknown" ? "" : [candidate?.primary_theme, candidate?.theme, candidate?.theme_tags]
        .map((value) => Array.isArray(value) ? value.map(label).filter(Boolean).join("、") : label(value)).find(Boolean) || "";
    return { industry, theme,
        industrySource: recordedEvidenceText(evidence.industry_source || candidate?.industry_source),
        industryDate: marketBusinessDate(evidence.industry_as_of),
        themeSource: recordedEvidenceText(evidence.theme_source || candidate?.theme_source),
        themeDate: marketBusinessDate(evidence.theme_as_of) };
}

function candidateExposureHtml(candidate) {
    const item = candidateExposureFacts(candidate);
    return `<div class="candidate-exposure">行业 ${escapeHtml(item.industry || "未分组")} · 来源 ${escapeHtml(item.industrySource)} · 资料日 ${item.industryDate || "未核实"}<br>`
        + `题材 ${escapeHtml(item.theme || "未分组")} · 来源 ${escapeHtml(item.themeSource)} · 资料日 ${item.themeDate || "未核实"}</div>`;
}

function candidateExposureSummaryHtml(rows) {
    const facts = (Array.isArray(rows) ? rows : []).map(candidateExposureFacts);
    if (!facts.length) return "";
    const industries = new Map();
    for (const item of facts) if (item.industry) industries.set(item.industry, (industries.get(item.industry) || 0) + 1);
    const industryText = [...industries].slice(0, 6).map(([name, count]) => `${name} ${count}条`).join("、");
    return `<p class="candidate-exposure-summary">本层 ${facts.length}条候选记录：行业未分组 ${facts.filter((item) => !item.industry).length}/${facts.length}；题材未分组 ${facts.filter((item) => !item.theme).length}/${facts.length}。`
        + (industryText ? `已记录行业：${escapeHtml(industryText)}${industries.size > 6 ? `，另${industries.size - 6}类` : ""}。` : "")
        + '未知行业/题材不能视为独立分散；行业记录也不代表相关性或风险分散已验证。</p>';
}

function candidateEvidenceDetails(candidate) {
    const score = candidate?.score_explanation;
    const hasScore = score && typeof score === "object" && !Array.isArray(score);
    const memberships = candidate?.pool_memberships;
    if (!hasScore && !Array.isArray(memberships)) return "";
    const scalar = (value, fallback = "未记录") => recordedEvidenceText(value, fallback);
    const listText = (value) => Array.isArray(value) ? value.map((item) => scalar(item, "")).filter(Boolean).join("、") || "未记录" : scalar(value);
    const scoreText = (value) => {
        const number = typeof value === "number" || typeof value === "string" ? numericValue(value) : null;
        return number === null ? scalar(value) : String(Number(number.toFixed(2)));
    };
    const sentenceText = (value) => listText(value).trim().replace(/[。．.]+$/, "");
    const components = Array.isArray(score?.components) ? score.components.filter((item) => item && typeof item === "object") : [];
    const scoreHtml = hasScore ? `<p>分数说明 ${escapeHtml(scalar(score.score_id))}；纸面研究 · 未验证，仅用于研究排序。本说明不计算胜率，也不输出盈利概率或收益承诺。</p>`
        + (components.length ? '<ul>' + components.map((item) => `<li>${escapeHtml(scalar(item.name))}：${escapeHtml(scoreText(item.points))}分</li>`).join("") + '</ul>' : '<p>分数组成未记录。</p>')
        + `<p>基础合计 ${escapeHtml(scoreText(score.base_total))}；策略倾斜 ${escapeHtml(scoreText(score.strategy_tilt))}；记录总分 ${escapeHtml(scoreText(score.total))}。同分处理：${escapeHtml(sentenceText(score.tie_breaker))}。</p>`
        + `<p>不能直接比较：${escapeHtml(sentenceText(score.not_comparable_to))}。微小分差不证明优势，本页不重新计算或调整排序。</p>` : "";
    const poolHtml = Array.isArray(memberships) ? '<p>同股在本次读取范围内的池子来源：</p>'
        + (memberships.length ? '<ul>' + memberships.map((item) => `<li>${escapeHtml(scalar(item?.label || item?.pool, "来源未记录"))} · 来源日 ${escapeHtml(scalar(item?.as_of, "未核实"))}</li>`).join("") + '</ul>'
            : '<p>本次来源列表为空，不能据此认定不在其他池中。</p>')
        + '<p>今日候选、焦点、历史观察和纸面持仓是不同范围；列在同一只股票下不代表新增持仓。</p>' : "";
    const displayScore = candidatePriorityScore(candidate);
    return `<details class="candidate-evidence-details"><summary>${hasScore ? `研究优先分 ${escapeHtml(displayScore === null ? "未记录" : scoreText(displayScore))} · ` : ""}查看分数与池子来源</summary>${scoreHtml}${poolHtml}</details>`;
}

function candidateRecordHtml(html, candidate) {
    const details = candidateEvidenceDetails(candidate);
    return details ? `<div class="candidate-record">${html}${details}</div>` : html;
}

function renderStrategyRecommendationRow(candidate) {
    const reasons = candidateMatchedConditions(candidate);
    const reasonHTML = reasons.length
        ? `<ul class="entry-reasons">${reasons.slice(0, 5).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
        : `<span class="section-note">入选原因缺失</span>`;
    const paper = candidatePaperState(candidate);
    const strategyName = safeText(candidate.strategy_name || candidate.strategy_lane || candidate.detail_lane || "策略");
    const html = `
        <button class="strategy-recommendation-row" type="button"
            data-symbol="${escapeHtml(safeText(candidate.symbol))}"
            data-lane="${escapeHtml(safeText(candidate.detail_lane || candidate.strategy_lane || ""))}"
            data-entry-date="${escapeHtml(safeText(candidate.select_date || candidate.signal_date || ""))}">
            <div class="strategy-recommendation-main">
                <strong>${escapeHtml(safeText(candidate.symbol))} ${escapeHtml(safeText(candidate.name || ""))}</strong>
                <span class="section-note">${escapeHtml(safeText(candidate.select_date || candidate.signal_date || ""))}</span>
            </div>
            <div>
                <span class="tag">${escapeHtml(strategyName)}</span>
                ${candidate.priority_label ? `<span class="tag tag-priority">${escapeHtml(safeText(candidate.priority_label))}</span>` : ""}
                <p class="candidate-method-note">${escapeHtml(candidateMethodNote(candidate))}</p>
            </div>
            <div>${reasonHTML}${candidateExposureHtml(candidate)}</div>
            <div>
                <span class="section-note">纸面参考价</span>
                <div class="number">${escapeHtml(candidateReferencePrice(candidate))}</div>
            </div>
            <div>
                <span class="badge ${paper.inPool ? "badge-ok" : ""}">${escapeHtml(paper.inPool ? "已入池" : "待入池")}</span>
                <div class="paper-desk-note">${escapeHtml(paper.label)}</div>
            </div>
        </button>
    `;
    return candidateRecordHtml(html, candidate);
}

function bindStrategyRecommendationClicks(root) {
    if (!root) return;
    root.querySelectorAll(".strategy-recommendation-row").forEach((button) => {
        button.addEventListener("click", () => {
            const candidate = findCandidateForButton(button);
            showDetail(candidate);
        });
    });
}

async function loadLLMSelectionAnalysis(candidates) {
    const statusEl = document.getElementById("llmAnalysisStatus");
    const contentEl = document.getElementById("llmAnalysisContent");
    if (!contentEl) return;
    const rows = Array.isArray(candidates) ? candidates : [];
    if (!rows.length) {
        if (statusEl) statusEl.textContent = "无候选可分析";
        contentEl.innerHTML = missingBlock("今日无策略候选。");
        return;
    }
    const candidateSummary = rows.slice(0, 10).map((candidate) => {
        const reasons = candidateMatchedConditions(candidate).slice(0, 3).join("；") || "入选原因缺失";
        return `${safeText(candidate.symbol)} ${safeText(candidate.name || "")}（${safeText(candidate.strategy_name || candidate.strategy_lane || "策略")}：${reasons}）`;
    }).join("\n");
    const question = `今日策略候选如下，请从研究合理性、数据质量风险、纸面跟踪注意点三个角度分析，不要给出买卖指令、确定价格结论或真实配置指令。\n${candidateSummary}`;
    renderAssistantDraft(contentEl, statusEl, question, "selection_analysis", "本次已读研究候选");
}

function renderDailyFocus(candidates) {
    const grid = document.getElementById("dailyFocusGrid");
    const summary = document.getElementById("dailyStrategySummary");
    if (!grid) return;
    const rows = sortCandidateRows(candidates);
    const signalDate = rows.map((candidate) => isoDateText(candidate.signal_date || candidate.select_date)).filter(Boolean).sort().at(-1) || "日期缺失";
    if (summary) {
        summary.innerHTML = `
            <div class="candidate-generation-head">
                <strong>今日策略候选 · ${escapeHtml(signalDate)} · 共 ${escapeHtml(safeText(rows.length))} 只候选</strong>
                <span class="tag tag-watch">Research Only</span>
                <span class="tag">Paper Account Only</span>
            </div>
            <div class="section-note">候选只用于本地研究复核和纸面池同步，不构成投资建议，不连接券商，不生成真实订单。</div>
            ${candidateExposureSummaryHtml(rows)}
        `;
    }
    if (!rows.length) {
        grid.innerHTML = missingBlock("当前没有可进入每日重点复核的候选。候选池仍可继续观察。");
        loadLLMSelectionAnalysis([]);
        return;
    }
    grid.innerHTML = `
        <div class="strategy-recommendation-list">
            ${rows.map(renderStrategyRecommendationRow).join("")}
        </div>
    `;
    bindStrategyRecommendationClicks(grid);
    loadLLMSelectionAnalysis(rows);
}

function renderQuantDailySummary(summary) {
    const panel = document.getElementById("quantSummaryPanel");
    if (!panel) return;
    const items = Array.isArray(summary.items) ? summary.items : [];
    if (!items.length) {
        panel.innerHTML = missingBlock("当前没有可生成日总结的 Quant 候选。");
        return;
    }
    const counts = summary.fit_counts || {};
    panel.innerHTML = `
        <div class="quant-summary-head">
            <strong>${escapeHtml(safeText(summary.title || "Quant 日总结"))} · ${escapeHtml(safeText(summary.date))}</strong>
            <div>${escapeHtml(safeText(summary.summary))}</div>
            <div class="quant-summary-metrics">
                ${summaryMetric("阶段符合", counts["符合入选假设"] ?? 0)}
                ${summaryMetric("偏离假设", counts["偏离入选假设"] ?? 0)}
                ${summaryMetric("等待确认", counts["等待更多交易日"] ?? 0)}
                ${summaryMetric("数据不足", counts["数据不足"] ?? 0)}
                ${summaryMetric("可回测", summary.backtest_ready_count ?? 0)}
            </div>
            <div class="section-note">${escapeHtml(safeText(summary.report_note))}</div>
        </div>
        ${renderQuantLimitReviewDigest(summary.limit_up_review || {})}
        <div class="quant-summary-grid">
            ${items.map(renderQuantSummaryCard).join("")}
        </div>
    `;
}

function summaryMetric(label, value) {
    return `<span class="quant-summary-metric"><strong>${escapeHtml(String(value))}</strong>${escapeHtml(label)}</span>`;
}

function renderQuantLimitReviewDigest(review) {
    const themes = Array.isArray(review.top_themes) ? review.top_themes : [];
    const hits = Array.isArray(review.quant_intersections) ? review.quant_intersections : [];
    const errors = Array.isArray(review.provider_errors) ? review.provider_errors : [];
    const attribution = review.attribution_summary || {};
    return `
        <section class="quant-summary-limit">
            <div class="quant-summary-limit-head">
                <div>
                    <strong>涨停复盘摘要</strong>
                    <div class="section-note">${escapeHtml(safeText(review.report_line || "本地涨停复盘缓存缺失。"))}</div>
                </div>
                <div class="tag-row">
                    <span class="tag">日期 ${escapeHtml(safeText(review.date))}</span>
                    <span class="tag">来源 ${escapeHtml(safeText(review.source))}</span>
                    <span class="tag tag-watch">Research Only</span>
                </div>
            </div>
            <div class="quant-summary-metrics">
                ${summaryMetric("涨停池", countWithUnit(review.limit_up_count, "只"))}
                ${summaryMetric("连板", countWithUnit(review.connected_count, "只"))}
                ${summaryMetric("封板未遂", countWithUnit(review.broken_count, "只"))}
                ${summaryMetric("封板率", formatPercentValue(review.seal_rate))}
                ${summaryMetric("Quant交集", `${hits.length}只`)}
                ${summaryMetric("追溯线索", countWithUnit(attribution.traceable_count, "只"))}
            </div>
            <div class="section-note">${escapeHtml(safeText(attribution.message || "题材归因证据链读取本地缓存。"))}</div>
            <div class="quant-summary-limit-grid">
                <div>
                    <h4>题材归因 Top</h4>
                    ${themes.length ? themes.slice(0, 5).map((theme) => `
                        <div class="quant-summary-limit-item">
                            <strong>${escapeHtml(safeText(theme.primary_theme))}</strong>
                            <span>${escapeHtml(safeText(theme.count))} 只；最高 ${escapeHtml(safeText(theme.max_consecutive_boards))} 连板</span>
                            <div class="section-note">${escapeHtml((theme.representatives || []).join("、"))}</div>
                        </div>
                    `).join("") : missingBlock("题材归因暂不可用。")}
                </div>
                <div>
                    <h4>Quant 候选交集</h4>
                    ${hits.length ? hits.map((item) => `
                        <div class="quant-summary-limit-item">
                            <strong>${escapeHtml(safeText(item.name))} ${escapeHtml(safeText(item.symbol))}</strong>
                            <span>${escapeHtml(safeText(item.strategy_lane))} · ${escapeHtml(safeText(item.limit_up_theme))} · ${escapeHtml(safeText(item.fit_label))}</span>
                            <div class="section-note">${escapeHtml(safeText(item.reason_reference))}</div>
                        </div>
                    `).join("") : `<div class="empty-state">今日涨停池暂未命中当前 Quant 候选。</div>`}
                </div>
            </div>
            ${errors.length ? `<div class="section-note">数据提示：${errors.map((item) => escapeHtml(item)).join("；")}</div>` : ""}
            <div class="section-note">${escapeHtml(safeText(review.boundary))}</div>
        </section>
    `;
}

function countWithUnit(value, unit) {
    const text = safeText(value);
    return text === "数据缺失" ? text : `${text}${unit}`;
}

function renderQuantSummaryCard(item) {
    const readiness = item.backtest_readiness || {};
    const tone = safeText(item.fit_tone || "watch");
    return `
        <article class="quant-summary-card">
            <h3>${escapeHtml(safeText(item.name))} <span class="candidate-code">${escapeHtml(safeText(item.symbol))}</span></h3>
            <div class="tag-row">
                <span class="tag">${escapeHtml(safeText(item.strategy_lane))}</span>
                <span class="tag tag-${toneClass(tone)}">${escapeHtml(safeText(item.fit_label))}</span>
                <span class="tag tag-${backtestTone(readiness.status)}">${escapeHtml(safeText(readiness.label))}</span>
            </div>
            <p class="summary-line"><strong>当前状况：</strong>${escapeHtml(formatNumber(item.current_price, 2))}，<span class="${changeClass(item.pct_chg)}">${escapeHtml(formatChange(item.pct_chg))}</span>；${escapeHtml(safeText(item.current_status))}。</p>
            <p><strong>策略预期：</strong>${escapeHtml(safeText(item.strategy_expectation))}</p>
            <p><strong>结果复核：</strong>${escapeHtml(safeText(item.reason_analysis))}</p>
            <p><strong>后续观察：</strong>${escapeHtml(safeText(item.next_observation))}</p>
            <p><strong>回测可用性：</strong>${escapeHtml(safeText(readiness.reason))} ${escapeHtml(safeText(readiness.scope))}</p>
        </article>
    `;
}

function backtestTone(status) {
    if (status === "ready") return "ok";
    if (status === "partial") return "watch";
    return "missing";
}

function recommendationRank(candidate) {
    if (!hasValue(candidate.priority_rank)) return 9999;
    const rank = numericValue(candidate.priority_rank);
    return rank === null || rank <= 0 ? 9999 : rank;
}

function sortCandidateRows(rows) {
    return (Array.isArray(rows) ? rows : []).slice().sort((a, b) => {
        const rankA = recommendationRank(a);
        const rankB = recommendationRank(b);
        if (rankA !== rankB) return rankA - rankB;
        const scoreA = numericValue(a.priority_score) ?? -999;
        const scoreB = numericValue(b.priority_score) ?? -999;
        if (scoreA !== scoreB) return scoreB - scoreA;
        return safeText(a.symbol).localeCompare(safeText(b.symbol));
    });
}

function candidateStrategyBucket(candidate) {
    const text = [
        candidate.detail_lane,
        candidate.strategy_name,
        candidate.strategy_lane,
        candidate.strategy_bucket,
        candidate.group_key,
    ].map((item) => safeText(item)).join(" ");
    if (safeText(candidate.detail_lane) === "board" || text.includes("打板")) return "board";
    if (safeText(candidate.detail_lane) === "long" || text.includes("长期") || text.includes("波段")) return "long";
    return "other";
}

function renderCandidateStrategySubgroups(rows) {
    const sortedRows = sortCandidateRows(rows);
    if (!sortedRows.length) return missingBlock("这一层当前没有候选。");
    const definitions = [
        { key: "board", title: "打板观察", note: "日线近涨停与量价条件的近似样本；无封单队列证据，重点看入选后1到5个交易日的延续性。", order: 1 },
        { key: "long", title: "长期观察", note: "MA20技术趋势样本，重点看趋势、均线和验证结果；纸面未验证。", order: 2 },
        { key: "other", title: "其他观察", note: "暂未归入打板或长期的样本，作为研究补充保留。", order: 3 },
    ];
    const buckets = definitions
        .map((definition) => {
            const bucketRows = sortedRows.filter((candidate) => candidateStrategyBucket(candidate) === definition.key);
            const bestRank = bucketRows.reduce((best, candidate) => Math.min(best, recommendationRank(candidate)), 9999);
            return { ...definition, rows: bucketRows, bestRank };
        })
        .filter((bucket) => bucket.rows.length)
        .sort((a, b) => (a.bestRank - b.bestRank) || (a.order - b.order));
    return `
        <div class="strategy-subgroups">
            ${candidateExposureSummaryHtml(sortedRows)}
            ${buckets.map((bucket) => {
                return `
                    <section class="strategy-subgroup ${bucket.key}">
                        <div class="strategy-subgroup-head">
                            <span class="strategy-subgroup-title">${escapeHtml(bucket.title)} · ${escapeHtml(bucket.rows.length)}只</span>
                            <span class="strategy-subgroup-note">${escapeHtml(bucket.note)}</span>
                        </div>
                        <div class="candidate-grid">
                            ${bucket.rows.map((candidate) => renderCandidateCard(candidate, false)).join("")}
                        </div>
                    </section>
                `;
            }).join("")}
        </div>
    `;
}

function groupRows(group) {
    return Array.isArray(group?.rows) ? group.rows : [];
}

function findCandidateGroup(groups, key) {
    return (Array.isArray(groups) ? groups : []).find((group) => safeText(group.key) === key) || null;
}

function groupCandidateCount(group) {
    return groupRows(group).length || numericValue(group?.count) || 0;
}

function renderPoolGroup(group, options = {}) {
    const rows = groupRows(group);
    if (!rows.length && !options.showEmpty) return "";
    const key = safeText(group?.key || "pool");
    const title = safeText(group?.title || options.title || "候选分层");
    const note = safeText(group?.note || options.note || "");
    return `
        <section class="pool-group" data-group="${escapeHtml(key)}">
            <div class="pool-group-head">
                <h3>${escapeHtml(title)} <span class="section-note">${escapeHtml(rows.length)}只</span></h3>
                <p class="pool-group-note">${escapeHtml(note)}</p>
            </div>
            ${rows.length ? renderCandidateStrategySubgroups(rows) : missingBlock(options.emptyText || "这一层当前没有候选。")}
        </section>
    `;
}

function renderCandidateZone({ className, title, subtitle, groups, emptyText, footerHtml = "", showEmptyFirst = false }) {
    const renderedGroups = (groups || [])
        .map((group, index) => renderPoolGroup(group, { showEmpty: showEmptyFirst && index === 0, emptyText }))
        .filter(Boolean)
        .join("");
    const total = (groups || []).reduce((sum, group) => sum + groupCandidateCount(group), 0);
    return `
        <section class="candidate-zone ${className}">
            <div class="candidate-zone-head">
                <div>
                    <h3>${escapeHtml(title)} <span class="section-note">${escapeHtml(total)}只</span></h3>
                    <p>${escapeHtml(subtitle)}</p>
                </div>
            </div>
            ${renderedGroups || missingBlock(emptyText)}
            ${footerHtml}
        </section>
    `;
}

function renderCandidatePoolPolicy(policy, exitGroup) {
    const exitCount = numericValue(policy.exit_review_count) ?? groupCandidateCount(exitGroup);
    const autoExitCount = numericValue(policy.auto_exit_count) ?? 0;
    const autoDowngradeCount = numericValue(policy.auto_downgrade_count) ?? 0;
    const manualCount = numericValue(policy.manual_count) ?? 0;
    const steps = Array.isArray(policy.steps) && policy.steps.length
        ? policy.steps
        : [
            "自动分层：偏离原入选假设的历史样本会移到复盘层，不再和今日新候选混排。",
            "复盘查看：点开详情看入选理由、入选以来表现、偏离原因和后续观察线索。",
            "手动确认：你可以保留观察、转只观察、移出活跃池，或清除手动状态。",
        ];
    return `
        <div class="degrade-policy">
            <div class="degrade-policy-head">
                <strong>${escapeHtml(safeText(policy.title || "降级复盘机制"))}</strong>
                <span class="section-note">待复盘 ${escapeHtml(exitCount)} 只 · 自动转待移出 ${escapeHtml(autoExitCount)} 只 · 长期降级 ${escapeHtml(autoDowngradeCount)} 只 · 手动标记 ${escapeHtml(manualCount)} 只</span>
            </div>
            <p>${escapeHtml(safeText(policy.summary || "这里是真实的候选池分层队列：系统会按入选后表现自动调整展示层级，但不会自动删除样本，也不会替你做最终结论。"))}</p>
            <ol class="degrade-policy-list">
                ${steps.map((step) => `<li>${escapeHtml(safeText(step))}</li>`).join("")}
            </ol>
            <p class="section-note">${escapeHtml(safeText(policy.backtest_note || "专项回测需要在 Quant 日总结或回测模块里触发；候选池这里只做研究分层和复盘提醒。"))}</p>
        </div>
    `;
}

function renderCandidateGroups(groups, fallbackCandidates, policy = {}) {
    const grid = document.getElementById("candidatesGrid");
    if (!groups.length && !fallbackCandidates.length) {
        grid.innerHTML = missingBlock("本地候选股票数据缺失。请运行候选生成脚本后刷新页面。");
        return;
    }
    if (!groups.length) {
        grid.innerHTML = renderCandidateStrategySubgroups(fallbackCandidates);
        bindCandidateClicks(grid);
        return;
    }
    const todayGroup = findCandidateGroup(groups, "today_recommendation");
    const exitGroup = findCandidateGroup(groups, "exit_review");
    const archivedGroup = findCandidateGroup(groups, "manual_archived");
    const historyKeys = ["long_swing", "board", "long_observe", "manual_observe", "general"];
    const historyGroups = historyKeys.map((key) => findCandidateGroup(groups, key)).filter(Boolean);
    grid.innerHTML = [
        renderCandidateZone({
            className: "candidate-zone-today",
            title: "今日新候选",
            subtitle: "基于最新完整日线和本地策略生成，优先级已经写在卡片上；只放今天新筛出的研究候选。",
            groups: todayGroup ? [todayGroup] : [],
            emptyText: "今天没有新候选。可以先刷新数据，再点击生成今日研究候选。",
            showEmptyFirst: true,
        }),
        renderCandidateZone({
            className: "candidate-zone-history",
            title: "历史候选 / 观察池",
            subtitle: "之前入选过的样本留在这里做跟踪验证，不和今日新候选混排。",
            groups: historyGroups,
            emptyText: "历史观察池当前没有活跃样本。",
        }),
        renderCandidateZone({
            className: "candidate-zone-exit",
            title: "待移出 / 降级复盘",
            subtitle: "偏离原入选假设或你手动移出的样本放在这里，主要用于学习复盘和恢复确认。",
            groups: [exitGroup, archivedGroup].filter(Boolean),
            emptyText: "当前没有待移出或降级复盘样本。",
            footerHtml: renderCandidatePoolPolicy(policy, exitGroup),
        }),
    ].join("");
    bindCandidateClicks(grid);
}

function candidateLaneClass(candidate) {
    if (candidate && candidate.is_today_recommendation) return "lane-today";
    const lane = String((candidate && (candidate.strategy_lane || candidate.detail_lane)) || "").toLowerCase();
    if (lane.includes("board") || lane.includes("打板") || lane.includes("涨停")) return "lane-board";
    if (lane.includes("long") || lane.includes("长期") || lane.includes("波段")) return "lane-long";
    return "";
}

function candidateConfidence(candidate) {
    return candidate && typeof candidate.data_confidence === "object" && candidate.data_confidence !== null
        ? candidate.data_confidence
        : {};
}

function confidenceTone(status) {
    if (status === "research_ready") return "ok";
    if (status === "partial") return "watch";
    if (status === "degraded") return "warn";
    if (status === "missing") return "missing";
    return "watch";
}

function confidenceLabel(candidate) {
    const confidence = candidateConfidence(candidate);
    const label = confidence.label || candidate.data_confidence_status || "参考可信度待标注";
    const score = hasValue(confidence.score) ? confidence.score : candidate.data_confidence_score;
    return hasValue(score) ? `${safeText(label)} ${safeText(score)}` : safeText(label);
}

function verificationLabel(candidate) {
    return safeText(candidate.verification_status_label || candidate.verification_status || "待核验");
}

function missingEvidenceMessages(candidate) {
    const evidence = Array.isArray(candidate.missing_evidence) ? candidate.missing_evidence : [];
    return evidence.map((item) => {
        if (item && typeof item === "object") return safeText(item.message || item.kind);
        return safeText(item);
    }).filter((item) => item && item !== "数据缺失");
}

function missingEvidenceBrief(candidate) {
    const messages = missingEvidenceMessages(candidate);
    return messages.length ? messages.slice(0, 2).join("；") : "暂无关键缺失证据";
}

function renderCandidateCard(candidate, isFocus) {
    const tracking = candidate.tracking || {};
    const quality = candidate.data_quality || {};
    const confidence = candidateConfidence(candidate);
    const tone = safeText(tracking.status_tone || quality.tone || "watch");
    const gapReason = Array.isArray(quality.missing_reasons) ? safeText(quality.missing_reasons[0]) : safeText(candidate.data_hint);
    const confidenceBrief = `${confidenceLabel(candidate)} · ${verificationLabel(candidate)}`;
    const evidenceBrief = missingEvidenceBrief(candidate);
    const auxLabels = Array.isArray(candidate.auxiliary_strategy_labels) ? candidate.auxiliary_strategy_labels : [];
    const reasonBrief = firstSentence(candidate.reason_cn, candidate.select_reason);
    const statusBrief = safeText(tracking.status_label || candidate.data_status);
    const verifyBrief = `区间 ${formatSignedPercent(tracking.return_since_select)} / 回撤 ${sanePercent(tracking.max_drawdown_since_select, { mode: "points", digits: 2 }).text} / ${safeText(tracking.elapsed_trading_days ?? 0)} 天`;
    const poolAction = candidate.pool_action || {};
    const poolTone = toneClass(poolAction.tone || tone);
    const priorityRank = recommendationRank(candidate);
    const priorityTag = priorityRank < 9999
        ? `<span class="tag tag-priority">${escapeHtml(safeText(candidate.priority_label || `优先关注 #${priorityRank}`))}</span>`
        : "";
    const sw = numericValue(candidate.strategy_weight);
    const weightTag = (sw !== null && (sw < 0.98 || sw > 1.02))
        ? `<span class="tag ${sw < 0.98 ? "tag-warn" : "tag-ok"}" title="${escapeHtml(safeText(candidate.strategy_weight_note || ""))}">策略权重 ${escapeHtml(safeText(candidate.strategy_weight))} ${sw < 0.98 ? "↓" : "↑"}</span>`
        : "";
    const laneClass = candidateLaneClass(candidate);
    const html = `
        <button class="candidate-card${laneClass ? " " + laneClass : ""}" type="button"
            data-symbol="${escapeHtml(candidate.symbol)}"
            data-lane="${escapeHtml(candidate.detail_lane)}"
            data-entry-date="${escapeHtml(candidate.select_date)}">
            <div class="candidate-top">
                <div>
                    <div class="candidate-title">${escapeHtml(candidate.name)}</div>
                    <span class="candidate-code">${escapeHtml(candidate.symbol)}</span>
                </div>
                <div class="candidate-price">
                    <span>${escapeHtml(formatNumber(candidate.current_price, 2))}</span>
                    <div class="candidate-change ${changeClass(candidate.pct_chg)}">${escapeHtml(formatChange(candidate.pct_chg))}</div>
                </div>
            </div>
            <div class="tag-row">
                ${priorityTag}
                ${weightTag}
                ${candidate.candidate_origin_label ? `<span class="tag ${candidate.is_today_recommendation ? "tag-ok" : "tag-watch"}">${escapeHtml(candidate.candidate_origin_label)}</span>` : ""}
                <span class="tag">${escapeHtml(safeText(candidate.strategy_name || candidate.strategy_lane || "候选观察"))}</span>
                <span class="candidate-method-note">${escapeHtml(candidateMethodNote(candidate))}</span>
                <span class="tag">${escapeHtml(safeText(candidate.select_date || candidate.signal_date || ""))}</span>
            </div>
            ${candidateExposureHtml(candidate)}
            <div class="candidate-brief">
                <div class="candidate-brief-line">
                    <span class="brief-label">原因</span>
                    <span>${highlightText(compactSentence(reasonBrief, 64))}</span>
                </div>
                <div class="candidate-brief-line">
                    <span class="brief-label brief-label-status">状态</span>
                    <span>${highlightText(statusBrief)}</span>
                </div>
                <div class="candidate-brief-line">
                    <span class="brief-label brief-label-verify">验证</span>
                    <span>${highlightText(verifyBrief)}</span>
                </div>
                <div class="candidate-brief-line">
                    <span class="brief-label brief-label-data">可信</span>
                    <span>${highlightText(compactSentence(confidenceBrief, 68))}</span>
                </div>
                <div class="candidate-brief-line">
                    <span class="brief-label brief-label-data">数据</span>
                    <span>${highlightText(compactSentence(gapReason, 58))}</span>
                </div>
            </div>
            <div class="indicator-row">
                <span class="indicator">MACD：${escapeHtml(candidate.macd_val)}</span>
                <span class="indicator">KDJ：${escapeHtml(candidate.kdj_val)}</span>
                <span class="indicator">RSI：${escapeHtml(candidate.rsi_val)}</span>
            </div>
            <div class="tag-row">
                ${isFocus ? `<span class="tag tag-ok">每日重点复核</span>` : ""}
                <span class="tag">${escapeHtml(safeText(candidate.strategy_lane))}</span>
                <span class="tag tag-${toneClass(tone)}">${escapeHtml(safeText(tracking.status_label))}</span>
                <span class="tag tag-${poolTone}">${escapeHtml(safeText(poolAction.label || "正常观察"))}</span>
                <span class="tag tag-${toneClass(quality.tone)}">${escapeHtml(safeText(candidate.data_status))}</span>
                <span class="tag tag-${confidenceTone(confidence.status)}">${escapeHtml(confidenceLabel(candidate))}</span>
                <span class="tag">${escapeHtml(verificationLabel(candidate))}</span>
                ${auxLabels.map((label) => `<span class="tag">辅助：${escapeHtml(label)}</span>`).join("")}
            </div>
            <div class="tracking-line">
                入选：${escapeHtml(safeText(candidate.select_date))} · ${highlightText(candidate.select_reason)}
            </div>
            <div class="data-gap-line">数据：${escapeHtml(gapReason)}；缺失证据：${escapeHtml(evidenceBrief)} ${auxLabels.length ? ` ${escapeHtml(candidate.strategy_conflict_note)}` : ""}</div>
            <div class="risk-row">
                <span>${escapeHtml(safeText(candidate.source_label))} · ${escapeHtml(safeText(candidate.strategy_name))}</span>
                <span class="risk ${riskClass(candidate.risk_level)}">${escapeHtml(safeText(candidate.risk_level))}风险</span>
            </div>
        </button>
    `;
    return candidateRecordHtml(html, candidate);
}

function toneClass(tone) {
    if (tone === "ok") return "ok";
    if (tone === "field_gap") return "watch";
    if (tone === "warn") return "warn";
    if (tone === "missing") return "missing";
    return "watch";
}

function formatSignedPercent(value) {
    const number = numericValue(value);
    if (number === null) return safeText(value);
    const sign = number > 0 ? "+" : "";
    return `${sign}${number.toFixed(2)}%`;
}

function bindCandidateClicks(root) {
    root.querySelectorAll(".candidate-card").forEach((button) => {
        button.addEventListener("click", () => {
            const candidate = findCandidateForButton(button);
            showDetail(candidate);
        });
    });
}

function findCandidateForButton(button) {
    const symbol = button.dataset.symbol || "";
    const lane = button.dataset.lane || "";
    const entryDate = button.dataset.entryDate || "";
    return currentCandidates.find((candidate) =>
        String(candidate.symbol || "") === symbol &&
        String(candidate.detail_lane || "") === lane &&
        String(candidate.select_date || "") === entryDate
    ) || currentCandidates.find((candidate) => String(candidate.symbol || "") === symbol) || {
        symbol,
        name: symbol,
        detail_lane: lane,
        select_date: entryDate,
    };
}

function workbenchSourceLabel(source) {
    return {
        research_candidates: "研究候选", strategy_positions: "纸面策略账持仓",
        ai_positions: "纸面 AI 账持仓", strategy_watchlist: "策略观察名单",
        search: "股票搜索", review: "复盘记录", stock: "直接查看",
    }[source] || "直接查看";
}

function rememberWorkbenchSource(source, items) {
    workbenchSourceLists[source] = Array.isArray(items) ? items.slice() : [];
    const context = currentWorkbenchContext;
    if (!context || context.source !== source || context.sourceItemsReady) return;
    const matched = source === "research_candidates"
        ? [currentTodayRecommendations, currentObservationCandidates, currentCandidates]
            .find((list) => list.some((item) => String(item.symbol) === context.symbol))
        : null;
    context.items = (matched || workbenchSourceLists[source]).slice();
    context.sourceItemsReady = true;
    setupWorkbenchSwitcher(context.symbol);
}

function workbenchContext(candidate, options, wasOpen) {
    if (options.context) return options.context;
    const defaultLists = [currentTodayRecommendations, currentObservationCandidates, currentCandidates];
    const matching = defaultLists.find((items) => items.some((item) => String(item.symbol) === String(candidate.symbol)));
    const source = options.source || (matching ? "research_candidates" : "stock");
    const supplied = Array.isArray(options.items) && options.items.length ? options.items : null;
    const known = supplied || workbenchSourceLists[source] || (source === "research_candidates" ? matching : null);
    const items = known || [candidate];
    const fromHash = options.returnHash || (wasOpen ? currentWorkbenchContext?.returnHash : window.location.hash) || "#watch";
    return {
        source,
        profile: (options.profile || candidate.profile || aiTraderProfile) === "glm" ? "glm" : "default",
        sourceItemsReady: Boolean(known),
        items: items.slice(),
        returnHash: /^#watch\//.test(fromHash) ? "#watch" : fromHash,
        scrollY: wasOpen ? currentWorkbenchContext?.scrollY || 0 : window.scrollY || 0,
        eventId: options.eventId || "",
    };
}

async function showDetail(candidate, options = {}) {
    if (!candidate || !hasValue(candidate.symbol)) return;
    const requestId = ++detailRequestSequence;
    const modal = document.getElementById("detailModal");
    const body = document.getElementById("modalBody");
    const wasOpen = modal.classList.contains("open");
    currentWorkbenchContext = workbenchContext(candidate, options, wasOpen);
    currentWorkbenchContext.symbol = String(candidate.symbol);
    currentDetailChartData = null;
    currentDetailResearchReference = null;
    currentDetailReferenceLevels = null;
    currentDetailReferenceIdentity = "";
    disposeActiveCharts();
    const isCurrent = () => requestId === detailRequestSequence && modal.classList.contains("open");
    document.getElementById("modalTitle").textContent = `${candidate.name || candidate.symbol} ${candidate.symbol}`;
    document.getElementById("modalSubtitle").textContent = "正在读取本地详情…";
    body.innerHTML = stockWorkbenchPlaceholder(candidate.symbol) + missingBlock("正在加载详情…");
    document.body.classList.add("modal-open");
    modal.classList.add("open");
    stockWorkbenchController?.start(candidate.symbol);
    const params = new URLSearchParams();
    if (hasValue(candidate.detail_lane)) params.set("lane", candidate.detail_lane);
    if (hasValue(candidate.select_date)) params.set("entry_date", candidate.select_date);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const hashParams = new URLSearchParams(params);
    hashParams.set("source", currentWorkbenchContext.source);
    if (currentWorkbenchContext.profile === "glm") hashParams.set("profile", "glm");
    hashParams.set("from", currentWorkbenchContext.returnHash);
    const detailHash = `#watch/${encodeURIComponent(String(candidate.symbol))}?${hashParams.toString()}`;
    if (options.updateUrl !== false && window.location.hash !== detailHash) {
        const changeHistory = wasOpen ? "replaceState" : "pushState";
        history[changeHistory]({ quantdeskWorkbench: true }, "", detailHash);
    }
    setupWorkbenchSwitcher(candidate.symbol);
    let detail;
    try {
        detail = await requestJson(`/api/stock/${encodeURIComponent(candidate.symbol)}/detail${suffix}`);
    } catch (error) {
        if (!isCurrent()) return;
        const workbenchMessage = window.QuantDeskStockWorkbench?.detailErrorMessage(error?.message);
        body.innerHTML = missingBlock(workbenchMessage || `详情接口加载失败：${escapeHtml(errorText(error))}。请确认本地服务仍在运行，并检查 data/daily。`);
        if (workbenchMessage) document.getElementById("modalSubtitle").textContent = "日K详情暂不可用 · 报价独立展示 · 纸面 · 未验证";
        mountStockWorkbench(body, candidate.symbol);
        return;
    }
    if (!isCurrent()) return;
    try {
        document.getElementById("modalTitle").textContent = `${detail.basic_info?.name || candidate.name || candidate.symbol} ${candidate.symbol}`;
        document.getElementById("modalSubtitle").textContent = `${workbenchSourceLabel(currentWorkbenchContext.source)} · AI 图上记录：${aiTraderProfileLabel(currentWorkbenchContext.profile)} · ${safeText(detail.basic_info?.date)} · Research Only · 纸面 · 未验证`;
        currentDetailChartData = { ...(detail.chart_data || {}) };
        currentDetailResearchReference = detail.research_reference || {};
        body.innerHTML = renderDetailContent(detail);
        mountStockWorkbench(body, candidate.symbol);
        setupDetailNavigation("candidate");
        bindCandidateActionButtons(detail);
        // The detail payload already contains locally read news. A read must
        // not implicitly invoke the separate news/provider refresh endpoint.
        activateDetailPane("charts");
        // Render the minute chart only after the pane is visible, otherwise the
        // hidden container measures 0x0 and ECharts draws nothing.
        loadMinutePanel(candidate.symbol);
        const overlays = await loadChartOverlays(candidate.symbol, currentWorkbenchContext.profile);
        if (!isCurrent()) return;
        currentDetailChartData.markers = mergeChartMarkers(currentDetailChartData.markers, overlays);
        renderWorkbenchEventList(currentDetailChartData.markers, currentDetailChartData, overlays.warnings || []);
        renderChart(currentDetailChartData);
        loadMinutePanel(candidate.symbol);
        currentDetailFetchedAt = Date.now();
        setupWorkbenchSwitcher(candidate.symbol);
    } catch (error) {
        if (!isCurrent()) return;
        console.error("[daily_research] detail render failed", error);
        body.innerHTML = missingBlock(stockWorkbenchController
            ? "日K详情显示失败；当前报价和算法参考状态仍可单独查看。"
            : `详情渲染失败：页面脚本已拿到接口数据，但渲染时出错。${escapeHtml(errorText(error))}`);
        if (stockWorkbenchController) document.getElementById("modalSubtitle").textContent = "日K详情暂不可用 · 报价独立展示 · 纸面 · 未验证";
        mountStockWorkbench(body, candidate.symbol);
    }
}

async function refreshNewsForSymbol(symbol) {
    if (!symbol) return;
    try {
        const news = await requestJson(`/api/stock/${encodeURIComponent(String(symbol))}/news`);
        const items = Array.isArray(news.data) ? news.data : [];
        if (news.ok) {
            const newsStatus = document.getElementById("newsStatus");
            if (newsStatus) {
                const count = items.length;
                const mode = news.status === "fetched" ? "实时" : "缓存";
                newsStatus.textContent = count > 0 ? `已刷新 ${count} 条 ${mode}新闻` : `${mode}暂无新闻`;
                newsStatus.style.color = ""; // clear inline color so the CSS class wins
                newsStatus.classList.toggle("news-status-ok", count > 0);
                newsStatus.classList.toggle("news-status-empty", count === 0);
            }
            const panel = document.getElementById("detailNewsSummaryPanel");
            if (panel) {
                panel.outerHTML = renderNewsSummary({
                    available: items.length > 0,
                    items,
                    count: items.length,
                    hint: items.length > 0 ? "" : "暂无新闻线索缓存；可稍后重新打开详情或检查联网刷新。",
                });
            }
        }
    } catch (e) {
        console.warn("[news] background refresh failed, showing cached data", e);
        const panel = document.getElementById("detailNewsSummaryPanel");
        if (panel && !panel.dataset.hasNews) {
            panel.outerHTML = renderNewsSummary({
                available: false,
                items: [],
                hint: "新闻后台刷新失败，当前仅显示本地缓存状态；不影响日线研究层。",
            });
        }
    }
}

function renderResearchBrief(brief) {
    if (!brief || brief.available === false) {
        return missingBlock(safeText((brief && brief.message) || "暂无研究简报；请先更新数据。"));
    }
    const kl = brief.key_levels || {};
    return `
        <div class="research-brief">
            <div class="rb-line"><span class="rb-tag">现在</span><p>${highlightText(safeText(brief.current_state))}</p></div>
            <div class="rb-line"><span class="rb-tag rb-tag-lean">研究倾向</span><p>${highlightText(safeText(brief.research_lean))}</p></div>
            <div class="rb-line"><span class="rb-tag rb-tag-go">继续关注 · 若</span><p>${highlightText(safeText(brief.continue_if))}</p></div>
            <div class="rb-line"><span class="rb-tag rb-tag-stop">看错放弃 · 若</span><p>${highlightText(safeText(brief.invalidate_if))}</p></div>
            <div class="rb-levels">
                <span>支撑（60日低） <b>${safeText(kl.support_60d ?? "—")}</b></span>
                <span>压力（60日高） <b>${safeText(kl.resistance_60d ?? "—")}</b></span>
            </div>
            <div class="rb-line"><span class="rb-tag">下一步</span><p>${highlightText(safeText(brief.next_step))}</p></div>
            <p class="rb-boundary">${escapeHtml(safeText(brief.boundary))}</p>
        </div>
    `;
}

function renderResearchReference(ref) {
    if (!ref.available) {
        return `<div class="text-panel"><p>${escapeHtml(safeText(ref.message))}</p></div>`;
    }
    const band = ref.price_band || {};
    const sr = ref.support_resistance || {};
    const bt = ref.simple_backtest || {};
    const trend = ref.recent_trend || {};
    const projection = ref.scenario_projection || {};
    const band1 = band.reference_1d || {};
    const band5 = band.reference_5d || {};
    return `
        <div class="info-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
            <article class="info-item">
                <div class="info-label">统计情景区间</div>
                <div class="info-value" style="font-size:14px;line-height:1.6">
                    <p style="margin:0;color:var(--text-secondary);font-size:12px">当前 ${safeText(band.latest)} 元 · ${safeText(band.volatility_label)} (${safeText(band.daily_volatility_pct)}%/日)</p>
                    <p style="margin:4px 0 0;font-size:12px">1日参考：${safeText(band1.low)} - ${safeText(band1.high)} 元（±${safeText(band.reference_1d_pct)}%）</p>
                    <p style="margin:2px 0 0;font-size:12px">5日参考：${safeText(band5.low)} - ${safeText(band5.high)} 元（±${safeText(band.reference_5d_pct)}%）</p>
                    <p style="margin:4px 0 0;color:var(--text-muted);font-size:11px">${escapeHtml(band.note || "")}</p>
                </div>
            </article>
            <article class="info-item">
                <div class="info-label">研究情景推演</div>
                <div class="info-value" style="font-size:14px;line-height:1.6">
                    <p style="margin:0;color:var(--text-secondary);font-size:12px">当前倾向：${escapeHtml(safeText(projection.bias))}</p>
                    <p class="scenario-thesis">${escapeHtml(safeText(projection.thesis || "本地情景推演需要更多完整日线确认。"))}</p>
                    <p class="scenario-structure">${escapeHtml(safeText(projection.structure_note || projection.confidence_note || ""))}</p>
                    ${renderScenarioProjection(projection)}
                    <p style="margin:6px 0 0;color:var(--text-muted);font-size:11px">${escapeHtml(projection.confidence_note || projection.disclaimer || "")}</p>
                </div>
            </article>
            <article class="info-item">
                <div class="info-label">历史压力支撑</div>
                <div class="info-value" style="font-size:14px;line-height:1.6">
                    <p style="margin:0;font-size:12px">60日高点：${safeText(sr.high_60d)} 元（距当前 ${safeText(sr.pct_from_high)}%）</p>
                    <p style="margin:2px 0 0;font-size:12px">60日低点：${safeText(sr.low_60d)} 元（距当前 +${safeText(sr.pct_from_low)}%）</p>
                    <p style="margin:4px 0 0;color:var(--text-secondary);font-size:12px">${escapeHtml(sr.position || "")}</p>
                </div>
            </article>
            <article class="info-item">
                <div class="info-label">理想条件回看（MA交叉，样本内）</div>
                <div class="info-value" style="font-size:14px;line-height:1.6">
                    <p style="margin:0;font-size:12px">交易次数：${bt.total_trades || 0} 笔</p>
                    <p style="margin:2px 0 0;font-size:12px">盈利占比：<strong>${safeText(bt.win_rate_pct)}%</strong>（赢${bt.wins || 0}/输${bt.losses || 0}）</p>
                    <p style="margin:2px 0 0;font-size:12px">平均每笔毛收益：${safeText(bt.avg_return_pct)}%</p>
                    <p class="sq-warning">按同日收盘信号、同收盘价成交；未计费用、滑点、涨跌停、停牌、T+1和资金限制。仅说明该均线规则的理想历史表现，不能证明本页价位或当前候选有效。</p>
                    <p style="margin:4px 0 0;color:var(--text-muted);font-size:11px">${escapeHtml(bt.label || "")}</p>
                </div>
            </article>
            <article class="info-item">
                <div class="info-label">近期趋势</div>
                <div class="info-value" style="font-size:14px;line-height:1.6">
                    <p style="margin:0;font-size:12px">近5日：${trend.trend_5d || "-"}（${safeText(trend.return_5d_pct)}%）</p>
                    <p style="margin:2px 0 0;font-size:12px">近20日：${trend.trend_20d || "-"}（${safeText(trend.return_20d_pct)}%）</p>
                    <p style="margin:4px 0 0;color:var(--text-muted);font-size:11px">统计描述，非方向预测</p>
                </div>
            </article>
        </div>
        <div class="scenario-chart-panel">
            <div class="chart-card-head">
                <h4>研究情景图</h4>
                <span class="chart-note">条件路径区间带；学习复盘用，不是价格预测</span>
            </div>
            <div id="scenarioProjectionChart" class="scenario-chart"></div>
        </div>
        <p style="margin-top:8px;color:var(--text-muted);font-size:11px;text-align:right">${escapeHtml(ref.disclaimer || "")}</p>`;
}

function renderScenarioProjection(projection) {
    const scenarios = Array.isArray(projection.scenarios) ? projection.scenarios : [];
    if (!scenarios.length) {
        return `<p style="margin:4px 0 0;font-size:12px">情景数据缺失。</p>`;
    }
    return `
        <div class="scenario-list">
            ${scenarios.map((item) => `
                <div class="scenario-row ${scenarioToneClass(item.tone)}">
                    <div class="scenario-row-head">
                        <span>${escapeHtml(safeText(item.name))}</span>
                        <span class="tag">${escapeHtml(safeText(item.weight || "观察"))}</span>
                    </div>
                    <p><strong>区间带：</strong>${escapeHtml(safeText(item.range))} 元</p>
                    <p><strong>触发：</strong>${escapeHtml(safeText(item.trigger || "等待价格、量能和均线结构确认。"))}</p>
                    <p><strong>观察重点：</strong>${escapeHtml(safeText(item.watch))}</p>
                </div>
            `).join("")}
        </div>
    `;
}

function scenarioToneClass(tone) {
    const text = safeText(tone);
    if (text === "strong") return "strong";
    if (text === "risk") return "risk";
    return "neutral";
}

function scenarioColor(tone) {
    const klass = scenarioToneClass(tone);
    if (klass === "strong") return "rgba(200, 57, 45, 0.68)";
    if (klass === "risk") return "rgba(180, 83, 9, 0.62)";
    return "rgba(47, 92, 140, 0.62)";
}

function scenarioRangeFromText(rangeText) {
    const matches = safeText(rangeText).match(/[-+]?\d+(?:\.\d+)?/g);
    if (!matches || matches.length < 2) return null;
    const low = numericValue(matches[0]);
    const high = numericValue(matches[1]);
    if (low === null || high === null) return null;
    return { low: Math.min(low, high), high: Math.max(low, high) };
}

function scenarioChartRows(ref) {
    if (!ref || !ref.available) return [];
    const band = ref.price_band || {};
    const projection = ref.scenario_projection || {};
    const rows = [];
    const pushRange = (label, range, tone = "neutral") => {
        const low = numericValue(range?.low);
        const high = numericValue(range?.high);
        if (low === null || high === null) return;
        rows.push({ label, low: Math.min(low, high), high: Math.max(low, high), tone });
    };
    pushRange("1日参考", band.reference_1d || {}, "neutral");
    pushRange("5日参考", band.reference_5d || {}, "neutral");
    (Array.isArray(projection.scenarios) ? projection.scenarios : []).forEach((item) => {
        const parsed = scenarioRangeFromText(item.range);
        if (parsed) rows.push({ label: safeText(item.name), tone: safeText(item.tone || "neutral"), ...parsed });
    });
    return rows.slice(0, 6);
}

function renderScenarioProjectionChart(ref) {
    const element = document.getElementById("scenarioProjectionChart");
    if (!element) return;
    if (typeof echarts !== "object") {
        element.innerHTML = missingBlock("本地 ECharts 文件缺失，暂时无法绘制研究情景图。");
        return;
    }
    const rows = scenarioChartRows(ref);
    if (!rows.length) {
        element.innerHTML = missingBlock("情景区间数据缺失，暂时无法绘制研究情景图。");
        return;
    }
    const band = ref.price_band || {};
    const latest = numericValue(band.latest);
    const chart = initChart("scenarioProjectionChart");
    if (!chart) return;
    const labels = rows.map((row) => row.label);
    const lows = rows.map((row) => row.low);
    const spans = rows.map((row) => ({
        value: Number((row.high - row.low).toFixed(2)),
        itemStyle: { color: scenarioColor(row.tone), borderRadius: [4, 4, 4, 4] },
    }));
    const mids = rows.map((row) => Number(((row.low + row.high) / 2).toFixed(2)));
    chart.setOption({
        animation: false,
        tooltip: {
            trigger: "axis",
            confine: true,
            formatter: (params) => {
                const list = Array.isArray(params) ? params : [params];
                const rangeParam = list.find((item) => item.seriesName === "区间带") || list[0] || {};
                const index = rangeParam.dataIndex ?? 0;
                const row = rows[index] || {};
                return `${escapeHtml(row.label || "")}<br/>下沿：${formatNumber(row.low, 2)} 元<br/>上沿：${formatNumber(row.high, 2)} 元<br/>中位：${formatNumber(mids[index], 2)} 元`;
            },
        },
        legend: { show: false },
        grid: { left: 82, right: 42, top: 18, bottom: 36 },
        xAxis: {
            type: "value",
            scale: true,
            axisLabel: { color: "#64748B", formatter: "{value}" },
            axisLine: { lineStyle: { color: "#CBD5E1" } },
            splitLine: { lineStyle: { color: "#E2E8F0" } },
        },
        yAxis: {
            type: "category",
            data: labels,
            inverse: true,
            axisLabel: { color: "#475569", interval: 0 },
            axisLine: { lineStyle: { color: "#CBD5E1" } },
            splitLine: { show: false },
        },
        series: [
            {
                name: "下沿基准",
                type: "bar",
                stack: "range",
                data: lows,
                silent: true,
                itemStyle: { color: "transparent" },
                emphasis: { disabled: true },
            },
            {
                name: "区间带",
                type: "bar",
                stack: "range",
                data: spans,
                barWidth: 18,
                markLine: latest === null ? undefined : {
                    silent: true,
                    symbol: "none",
                    lineStyle: { color: "#C8392D", type: "dashed", width: 1.6 },
                    label: { color: "#C8392D", formatter: `当前 ${formatNumber(latest, 2)} 元` },
                    data: [{ xAxis: latest }],
                },
            },
        ],
    });
}

function renderRealtimeDetailContent(detail) {
    const info = detail.basic_info || {};
    const quote = detail.realtime_quote || {};
    const daily = detail.daily_layer || {};
    const analysis = detail.observation_analysis || {};
    const dailyRefresh = detail.daily_refresh || {};
    const quoteAvailable = quote.available === true;
    return renderDetailShell("realtime", [
        {
            id: "overview",
            content: `
        <section class="detail-section">
            <h3>实时快照（临时观察，不覆盖正式日线）</h3>
            <div class="info-grid">
                ${infoItem("股票代码", info.symbol)}
                ${infoItem("中文名称", info.name)}
                ${infoItem("当前价/快照价", `${formatNumber(info.price, 2)} 元`)}
                ${infoItem("今日变化", formatChange(info.pct_chg), changeClass(info.pct_chg))}
                ${infoItem("快照时间", quoteAvailable ? `${safeText(quote.quote_date)} ${safeText(quote.quote_time)}` : "数据缺失")}
                ${infoItem("快照来源", quoteAvailable ? safeText(quote.source) : "数据缺失")}
                ${infoItem("今开", `${formatNumber(quote.open, 2)} 元`)}
                ${infoItem("最高", `${formatNumber(quote.high, 2)} 元`)}
                ${infoItem("最低", `${formatNumber(quote.low, 2)} 元`)}
                ${infoItem("成交额", formatRealtimeAmount(quote.amount))}
            </div>
            <p class="observation-note">盘中/实时数据会随市场变化，不能替代收盘后的完整日线复核。</p>
        </section>
        <section class="detail-section">
            <h3>正式研究层状态</h3>
            <div class="info-grid">
                ${infoItem("完整日线最新日", daily.latest_complete_date)}
                ${infoItem("本地日线条数", `${safeText(daily.row_count)} 条`)}
                ${infoItem("日线补全", dailyRefresh.status || (dailyRefresh.attempted ? "已尝试" : "未尝试"))}
                ${infoItem("目标完整交易日", dailyRefresh.target_date || "数据缺失")}
            </div>
            <div class="text-panel">
                <p>${escapeHtml(safeText(daily.message))}</p>
                <p>${escapeHtml(safeText(dailyRefresh.message))}</p>
            </div>
        </section>`,
        },
        {
            id: "scenario",
            content: `
        <section class="detail-section">
            <h3>当前情况如何（研究性情景参考）</h3>
            <div class="text-panel">
                <p>${escapeHtml(safeText(analysis.summary))}</p>
                <p><strong>后续观察：</strong>${escapeHtml(safeText(analysis.scenario_reference))}</p>
                <p>${escapeHtml(safeText(analysis.disclaimer))}</p>
            </div>
        </section>
        <section class="detail-section">
            <h3>技术指标详解</h3>
            <div class="text-panel detail-emphasis">${highlightHtmlText(detail.indicators_exp)}</div>
        </section>
        <section class="detail-section">
            <h3>研究参考（统计观察，不构成投资建议）</h3>
            ${renderResearchReference(detail.research_reference || {})}
        </section>`,
        },
        {
            id: "charts",
            content: `
        <section class="detail-section">
            <h3>日线 / K线 / MACD / KDJ / RSI</h3>
            <div id="chartContainer" class="chart-stack">
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>价格 K 线 / 均线</h4>
                        <span class="chart-note">最后一根“临时”只来自实时快照，不写入正式完整日线</span>
                    </div>
                    <div id="priceChart" class="split-chart split-chart-price"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>成交量</h4>
                        <span class="chart-note">盘中量能会继续变化，收盘后再进入正式复核</span>
                    </div>
                    <div id="volumeChart" class="split-chart split-chart-volume"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>MACD</h4>
                        <span class="chart-note">基于完整日线计算；临时快照不参与指标计算</span>
                    </div>
                    <div id="macdChart" class="split-chart"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>KDJ</h4>
                        <span class="chart-note">基于完整日线计算；用于观察短线节奏</span>
                    </div>
                    <div id="kdjChart" class="split-chart"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>RSI</h4>
                        <span class="chart-note">基于完整日线计算；用于观察强弱和过热/偏冷</span>
                    </div>
                    <div id="rsiChart" class="split-chart"></div>
                </article>
            </div>
        </section>`,
        },
        {
            id: "signals",
            content: `
        <section class="detail-section">
            <h3>技术指标详解</h3>
            <div class="text-panel detail-emphasis">${highlightHtmlText(detail.indicators_exp)}</div>
        </section>`,
        },
        {
            id: "context",
            content: `
        ${renderNewsSummary(detail.news_summary || {})}
        ${renderIndustryComparison(detail.industry_comparison || {})}
        ${renderTrackingVsBenchmark(detail.tracking_vs_benchmark || {}, detail.basic_info || {})}`,
        },
        {
            id: "actions",
            content: `
        <section class="detail-section">
            <h3>观察操作</h3>
            <div class="observation-actions">
                <button id="addManualObservationButton" class="action-button" type="button">加入手动观察池</button>
            </div>
        <p id="manualObservationNote" class="observation-note">手动观察池不会自动进入正式研究候选；后续可以再由你决定是否纳入研究池。</p>
        </section>`,
        },
        {
            id: "risk",
            content: `
        <section class="detail-section">
            <h3>风险提示</h3>
            <div class="text-panel">${safeHtml(detail.risk_warning)}</div>
        </section>`,
        },
    ]);
}

function formatRealtimeAmount(value) {
    const number = numericValue(value);
    if (number === null) return safeText(value);
    return `${(number / 100000000).toFixed(2)} 亿元`;
}

function renderDetailContent(detail) {
    const info = detail.basic_info || {};
    const review = detail.strategy_review || {};
    const tracking = review.tracking || {};
    const quality = review.data_quality || {};
    const confidence = candidateConfidence(review);
    const trackingTone = toneClass(tracking.status_tone || quality.tone || "watch");
    const qualityText = Array.isArray(quality.missing_reasons) ? quality.missing_reasons.join("；") : quality.hint;
    const confidenceReasons = Array.isArray(confidence.reasons) ? confidence.reasons.join("；") : confidence.summary;
    const reviewEvidenceMessages = missingEvidenceMessages(review);
    const reviewEvidenceBrief = missingEvidenceBrief(review);
    return renderDetailShell("candidate", [
        {
            id: "brief",
            content: `
        <section class="detail-section detail-brief">
            <h3>研究简报 · 直接说人话（研究观察，不是买卖指令）</h3>
            ${renderResearchBrief(detail.research_brief || {})}
            <div id="workbenchEventList" class="workbench-event-box"></div>
        </section>`,
        },
        {
            id: "overview",
            content: `
        <section class="detail-section">
            <h3>基本信息</h3>
            <div class="info-grid">
                ${infoItem("股票代码", info.symbol)}
                ${infoItem("中文名称", info.name)}
                ${infoItem("当前价", `${formatNumber(info.price, 2)} 元`)}
                ${infoItem("涨跌幅", formatChange(info.pct_chg), changeClass(info.pct_chg))}
                ${infoItem("数据日期", info.date)}
                ${infoItem("风险等级", `${safeText(info.risk_level)}风险`, riskTone(info.risk_level))}
            </div>
        </section>`,
        },
        {
            id: "tracking",
            content: `
        <section class="detail-section">
            <h3>策略跟踪验证</h3>
            <div class="info-grid">
                ${infoItem("策略类型", review.strategy_name)}
                ${infoItem("所属分层", `${review.strategy_bucket || review.strategy_lane || "-"}`)}
                ${infoItem("入选日期", review.select_date)}
                ${infoItem("入选价", `${formatNumber(tracking.entry_price, 2)} 元`)}
                ${infoItem("最新价", `${formatNumber(tracking.latest_price, 2)} 元`)}
                ${infoItem("区间变化", formatSignedPercent(tracking.return_since_select), changeClass(tracking.return_since_select))}
                ${infoItem("最大回撤", sanePercentHTML(tracking.max_drawdown_since_select, { mode: "points", digits: 2 }), changeClass(tracking.max_drawdown_since_select), true)}
                ${infoItem("经过交易日", `${tracking.elapsed_trading_days || "-"} 天`)}
                ${infoItem("下次复核日", tracking.review_date || "-")}
                ${infoItem("观察状态", tracking.status_label, trackingTone)}
                ${infoItem("数据天数", `${quality.row_count || "-"} 条`)}
                ${infoItem("数据最新日", quality.latest_date || "-")}
                ${infoItem("参考可信度", confidenceLabel(review), confidenceTone(confidence.status))}
                ${infoItem("验证状态", verificationLabel(review))}
            </div>
            <div class="key-review-panel">
                ${keyReviewRow("选入原因", review.select_reason, "reason")}
                ${keyReviewRow("当前状态", tracking.status_label, "status")}
                ${keyReviewRow("复核结论", tracking.review_note, trackingTone === "warn" ? "risk" : "status")}
                ${keyReviewRow("跟踪路径", tracking.expected_path, "reason")}
                ${keyReviewRow("可信依据", confidenceReasons, "data")}
                ${keyReviewRow("缺失证据", reviewEvidenceBrief, reviewEvidenceMessages.length ? "risk" : "data")}
                ${keyReviewRow("数据质量", qualityText, quality.tone === "warn" || quality.tone === "missing" ? "risk" : "data")}
            </div>
        </section>`,
        },
        {
            id: "scenario",
            content: `
        <section class="detail-section">
            <h3>研究情景推演（学习用，不是价格预测）</h3>
            ${renderResearchReference(detail.research_reference || {})}
        </section>
        ${renderTrackingVsBenchmark(detail.tracking_vs_benchmark || {}, detail.basic_info || {})}`,
        },
        {
            id: "signals",
            content: `
        <section class="detail-section">
            <h3>为什么选这只</h3>
            <div class="text-panel detail-emphasis">${highlightHtmlText(detail.explanation)}</div>
        </section>
        <section class="detail-section">
            <h3>技术指标详解</h3>
            <div class="text-panel detail-emphasis">${highlightHtmlText(detail.indicators_exp)}</div>
        </section>`,
        },
        {
            id: "context",
            content: `
        ${renderNewsSummary(detail.news_summary || {})}
        ${renderIndustryComparison(detail.industry_comparison || {})}
        <section class="detail-section">
            <h3>小白学习区 — 怎么看这些指标？</h3>
            <div class="glossary-grid">
                <div class="glossary-card">
                    <h4><span class="glossary-term">MACD</span> 趋势动能指标</h4>
                    <p>DIF（快线）上穿 DEA（慢线）叫<span class="glossary-term">金叉</span>，通常表示短线转强；下穿叫<span class="glossary-term">死叉</span>，通常表示短线转弱。柱体（BAR）由绿变红说明动能增强。</p>
                    ${_glossaryStockNote()}
                </div>
                <div class="glossary-card">
                    <h4><span class="glossary-term">KDJ</span> 短线节奏指标</h4>
                    <p>K 值在 80 以上叫<span class="glossary-term">超买区</span>（涨太快容易回调），20 以下叫<span class="glossary-term">超卖区</span>（跌太多可能有修复）。J 值最敏感，极端值时变化最快。</p>
                    ${_glossaryStockStatus(info.symbol, "kdj")}
                </div>
                <div class="glossary-card">
                    <h4><span class="glossary-term">RSI</span> 相对强弱指标</h4>
                    <p>RSI 在 30-70 之间是正常波动区间。超过 70 说明短期买盘偏热，低于 30 说明卖压较重。RSI 背离（价格涨但 RSI 不涨）可能是趋势减弱的信号。</p>
                    ${_glossaryStockStatus(info.symbol, "rsi")}
                </div>
                <div class="glossary-card">
                    <h4><span class="glossary-term">均线 MA</span> 平均成本线</h4>
                    <p>MA5 是近5日平均价，MA20 是近20日平均价。股价在 MA20 上方说明中期偏强。短线均线上穿长线均线叫<span class="glossary-term">金叉</span>（和 MACD 金叉是两个概念）。</p>
                    ${_glossaryStockStatus(info.symbol, "ma")}
                </div>
            </div>
            <p style="margin-top:14px;color:var(--text-muted);font-size:12px;line-height:1.5">以上是通用概念解释。具体到本只股票，请参考上方<strong>技术指标详解</strong>中的数值和判断。</p>
        </section>`,
        },
        {
            id: "charts",
            content: `
        <section class="detail-section">
            <h3>交互 K 线图</h3>
            <div class="sq-overlay-legend" id="referenceOverlayLegend">算法参考位会以横线叠加在价格 K 线上（纸面 · 未验证）。</div>
            <div id="chartContainer" class="chart-stack">
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>价格 K 线 / 均线</h4>
                        <span class="chart-note">只保留价格、K 线与均线；标记入选观察和后续复盘点；参考位横线为算法观察价，不是买卖指令</span>
                    </div>
                    <div id="priceChart" class="split-chart split-chart-price"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>成交量</h4>
                        <span class="chart-note">只保留量能柱；观察放量或缩量变化</span>
                    </div>
                    <div id="volumeChart" class="split-chart split-chart-volume"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>分时 / 1 分钟（本地缓存）</h4>
                        <span class="chart-note" id="minuteChartNote">读取本地 1 分钟缓存；没有缓存就如实显示缺失</span>
                    </div>
                    <div id="minuteChart" class="split-chart"></div>
                </article>
                <details class="chart-extra-indicators">
                    <summary>更多指标副图（MACD / KDJ / RSI，默认收起）</summary>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>MACD</h4>
                        <span class="chart-note">只保留 DIF、DEA 与 MACD 柱</span>
                    </div>
                    <div id="macdChart" class="split-chart"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>KDJ</h4>
                        <span class="chart-note">只保留 K、D、J 三条线</span>
                    </div>
                    <div id="kdjChart" class="split-chart"></div>
                </article>
                <article class="chart-card">
                    <div class="chart-card-head">
                        <h4>RSI</h4>
                        <span class="chart-note">只保留 RSI 曲线</span>
                    </div>
                    <div id="rsiChart" class="split-chart"></div>
                </article>
                </details>
            </div>
        </section>`,
        },
        {
            id: "actions",
            content: `
        <section class="detail-section">
            <h3>候选池操作（本地研究状态）</h3>
            ${renderCandidateActionPanel(detail)}
        </section>`,
        },
        {
            id: "risk",
            content: `
        <section class="detail-section">
            <h3>具体风险提示</h3>
            <div class="text-panel">${safeHtml(detail.risk_warning)}</div>
        </section>`,
        },
    ]);
}

function renderDetailShell(mode, panes) {
    const sections = DETAIL_NAV_SECTIONS[mode] || [];
    const paneMap = new Map((Array.isArray(panes) ? panes : []).map((pane) => [pane.id, pane.content || ""]));
    const mergeMembers = (section) => {
        const members = Array.isArray(section.members) && section.members.length ? section.members : [section.id];
        const html = members.map((memberId) => paneMap.get(memberId) || "").filter(Boolean).join("\n");
        return html || missingBlock(`${section.title} 暂无内容。`);
    };
    return `
        <div class="detail-shell" data-detail-mode="${escapeHtml(mode)}">
            <aside class="detail-nav" aria-label="详情导航">
                <div class="detail-nav-head">
                    <strong>${mode === "realtime" ? "实时观察模块" : "研究详情模块"}</strong>
                    <span class="section-note">${mode === "realtime" ? "先看临时快照，再分块学习。" : "像外层一样按块看，不再整列往下拖。"}</span>
                </div>
                <div class="detail-nav-strip">
                    ${sections.map((section, index) => `
                        <button
                            class="detail-nav-button"
                            type="button"
                            data-detail-target="${escapeHtml(section.id)}"
                            aria-pressed="${index === 0 ? "true" : "false"}"
                            ${index === 0 ? 'aria-current="page"' : ""}
                        >
                            <span class="detail-nav-title">${escapeHtml(section.title)}</span>
                            <span class="detail-nav-summary">${escapeHtml(section.summary)}</span>
                        </button>
                    `).join("")}
                </div>
            </aside>
            <div class="detail-panel-area">
                ${sections.map((section, index) => `
                    <div class="detail-pane" data-detail-pane="${escapeHtml(section.id)}" ${index === 0 ? "" : "hidden"}>
                        <div class="detail-pane-grid">
                            ${mergeMembers(section)}
                        </div>
                    </div>
                `).join("")}
            </div>
        </div>
    `;
}

function setupDetailNavigation(mode) {
    const shell = document.querySelector(".detail-shell");
    if (!shell) return;
    const buttons = Array.from(shell.querySelectorAll("[data-detail-target]"));
    if (!buttons.length) return;
    shell.dataset.detailMode = mode;
    buttons.forEach((button) => {
        button.addEventListener("click", () => activateDetailPane(button.dataset.detailTarget || ""));
    });
    const extras = shell.querySelector(".chart-extra-indicators");
    if (extras) {
        extras.addEventListener("toggle", () => window.setTimeout(() => renderDetailCharts(mode), 0));
    }
}

function activateDetailPane(targetId) {
    const shell = document.querySelector(".detail-shell");
    if (!shell || !targetId) return;
    const mode = shell.dataset.detailMode || "candidate";
    const buttons = Array.from(shell.querySelectorAll("[data-detail-target]"));
    const panes = Array.from(shell.querySelectorAll("[data-detail-pane]"));
    buttons.forEach((button) => {
        const active = button.dataset.detailTarget === targetId;
        button.setAttribute("aria-pressed", active ? "true" : "false");
        if (active) {
            button.setAttribute("aria-current", "page");
        } else {
            button.removeAttribute("aria-current");
        }
    });
    panes.forEach((pane) => {
        pane.hidden = pane.dataset.detailPane !== targetId;
    });
    const panelArea = shell.querySelector(".detail-panel-area");
    if (panelArea) panelArea.scrollTop = 0;
    const navStrip = shell.querySelector(".detail-nav-strip");
    const activeButton = shell.querySelector(`[data-detail-target="${CSS.escape(targetId)}"]`);
    if (navStrip && activeButton && typeof activeButton.scrollIntoView === "function") {
        activeButton.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
    }
    // Render whichever charts live in the now-visible tab. Tabs are merged
    // groups, so a single tab may hold the K-line stack, the scenario chart, or
    // both — detect by container presence rather than by a fixed tab id.
    const activePane = shell.querySelector(`[data-detail-pane="${CSS.escape(targetId)}"]`);
    const hasKline = !!activePane?.querySelector("#chartContainer");
    const hasScenario = !!activePane?.querySelector("#scenarioProjectionChart");
    if (hasKline) {
        window.setTimeout(() => renderDetailCharts(mode), 0);
    }
    if (hasScenario) {
        window.setTimeout(() => renderDetailScenarioChart(), 0);
    }
    if (!hasKline && !hasScenario) {
        resizeActiveCharts();
    }
}

function renderDetailCharts(mode) {
    void mode;
    if (currentDetailChartData) renderChart(currentDetailChartData);
}

function renderDetailScenarioChart() {
    if (currentDetailResearchReference) renderScenarioProjectionChart(currentDetailResearchReference);
}

function _glossaryStockNote() {
    return `<p class="glossary-card" style="margin-top:6px;padding:5px 8px;font-size:12px;color:var(--text-muted);border-radius:4px;background:rgba(245,240,232,0.5)">具体到这只股票的表现，见上方<strong>技术指标详解</strong>的数值和判断。</p>`;
}

function _glossaryStockStatus(symbol, kind) {
    const candidate = currentCandidates.find((item) => String(item.symbol || "") === String(symbol || ""));
    const labelMap = {
        kdj: "KDJ",
        rsi: "RSI",
        ma: "均线",
    };
    const valueMap = {
        kdj: candidate?.kdj_val,
        rsi: candidate?.rsi_val,
        ma: candidate?.reason_cn,
    };
    const label = labelMap[kind] || "指标";
    const value = safeText(valueMap[kind]);
    const shortValue = value.length > 90 ? `${value.slice(0, 90)}...` : value;
    return `<p class="stock-status indicator-neutral">本股当前${escapeHtml(label)}参考：${escapeHtml(shortValue)}。仅用于学习观察，不构成投资建议。</p>`;
}

function renderNewsSummary(news) {
    const items = Array.isArray(news.items) ? news.items : [];
    if (!news.available || !items.length) {
        return `
        <section id="detailNewsSummaryPanel" class="detail-section" data-has-news="">
            <h3>新闻线索（仅作研究参考）</h3>
            <div class="text-panel">
                <p>${escapeHtml(news.hint || "暂无可用新闻线索；详情打开后会尝试后台刷新。")}</p>
                <p style="margin-top:6px;color:var(--text-muted);font-size:11px">新闻只作为研究线索，不构成买卖建议，也不会改变正式候选结论。</p>
            </div>
        </section>`;
    }
    return `
        <section id="detailNewsSummaryPanel" class="detail-section" data-has-news="1">
            <h3>新闻线索（仅作研究参考）</h3>
            <div class="text-panel">
                ${items.map(item => `<p style="margin:4px 0;line-height:1.5"><strong>${escapeHtml((item.published || "").slice(0, 10))}</strong> ${escapeHtml(item.title || "")}</p>`).join("")}
                <p style="margin-top:6px;color:var(--text-muted);font-size:11px">共 ${news.count || items.length} 条。新闻是研究线索，不构成买卖建议。</p>
            </div>
        </section>`;
}

function renderIndustryComparison(industry) {
    if (!industry.available) {
        return `
        <section class="detail-section">
            <h3>同行业对比</h3>
            <div class="text-panel">
                <p>${escapeHtml(industry.hint || "行业对比数据暂不可用。")}</p>
                <p style="margin-top:6px;color:var(--text-muted);font-size:11px">缺失时保持降级展示，不用行业均值替代真实板块数据。</p>
            </div>
        </section>`;
    }
    const vs = numericValue(industry.stock_vs_sector);
    const vsLabel = vs !== null ? (vs > 0 ? `跑赢行业 ${vs}%` : `跑输行业 ${Math.abs(vs)}%`) : "无对比数据";
    return `
        <section class="detail-section">
            <h3>同行业对比</h3>
            <div class="info-grid" style="grid-template-columns:repeat(3,minmax(160px,1fr))">
                <article class="info-item"><div class="info-label">所属行业</div><div class="info-value">${escapeHtml(industry.sector_name || "-")}</div></article>
                <article class="info-item"><div class="info-label">行业平均涨跌</div><div class="info-value ${changeClass(industry.sector_change)}">${numericValue(industry.sector_change) !== null ? formatChange(industry.sector_change) : "数据缺失"}</div></article>
                <article class="info-item"><div class="info-label">个股 vs 行业</div><div class="info-value">${escapeHtml(vsLabel)}</div></article>
            </div>
            <p style="margin-top:6px;color:var(--text-muted);font-size:11px">${escapeHtml(industry.hint || "")}</p>
        </section>`;
}

function renderTrackingVsBenchmark(tracking, info) {
    if (!tracking.available) {
        return `
        <section class="detail-section">
            <h3>入选以来 vs 上证指数</h3>
            <div class="text-panel">
                <p>${escapeHtml(tracking.hint || "入选以来指数对比暂不可用。")}</p>
                <p style="margin-top:6px;color:var(--text-muted);font-size:11px">该面板只做研究复盘，不作为收益承诺或持仓建议。</p>
            </div>
        </section>`;
    }
    return `
        <section class="detail-section">
            <h3>入选以来 vs 上证指数</h3>
            <div class="info-grid" style="grid-template-columns:repeat(3,minmax(160px,1fr))">
                <article class="info-item"><div class="info-label">入选以来（个股）</div><div class="info-value ${changeClass(tracking.stock_return_pct)}">${formatSignedPercent(tracking.stock_return_pct)}</div></article>
                <article class="info-item"><div class="info-label">同期上证指数</div><div class="info-value ${changeClass(tracking.benchmark_return_pct)}">${numericValue(tracking.benchmark_return_pct) !== null ? formatSignedPercent(tracking.benchmark_return_pct) : "数据缺失"}</div></article>
                <article class="info-item"><div class="info-label">超额收益</div><div class="info-value ${changeClass(tracking.excess_return_pct)}">${numericValue(tracking.excess_return_pct) !== null ? formatSignedPercent(tracking.excess_return_pct) : "数据缺失"}</div></article>
            </div>
            <p style="margin-top:4px;color:var(--text-muted);font-size:11px">${escapeHtml(tracking.verdict || "")} · 入选日 ${escapeHtml(tracking.entry_date || "-")}</p>
        </section>`;
}

function infoItem(label, value, extraClass = "", isHtml = false) {
    return `
        <article class="info-item">
            <div class="info-label">${escapeHtml(label)}</div>
            <div class="info-value ${extraClass}">${isHtml ? value : escapeHtml(safeText(value))}</div>
        </article>
    `;
}

function keyReviewRow(label, value, tone = "") {
    return `
        <div class="key-review-row">
            <span class="key-review-label ${escapeHtml(tone)}">${escapeHtml(label)}</span>
            <span>${highlightText(value)}</span>
        </div>
    `;
}

function renderCandidateActionPanel(detail) {
    const info = detail.basic_info || {};
    const review = detail.strategy_review || {};
    const pool = review.pool_action || {};
    const paper = detail.paper_simulation || {};
    const tone = toneClass(pool.tone || "watch");
    const paperAdded = paper.in_paper_pool === true;
    return `
        <div class="candidate-action-panel">
            <div class="candidate-action-head">
                <span class="candidate-action-status ${tone}">${escapeHtml(safeText(pool.label || "正常观察"))}</span>
                <span>${highlightText(pool.reason || "当前仍在候选池中按原分层观察。")}</span>
            </div>
            <div class="candidate-action-head">
                <span class="candidate-action-status ${paperAdded ? "ok" : "watch"}">${paperAdded ? "已在纸面模拟池" : "未进纸面模拟池"}</span>
                <span>${highlightText(paper.message || "你可以手动加入纸面模拟池，后续用于本地跟踪复盘。")}</span>
            </div>
            <div class="candidate-action-buttons" data-candidate-symbol="${escapeHtml(info.symbol)}" data-candidate-entry="${escapeHtml(review.select_date)}" data-candidate-lane="${escapeHtml(review.detail_lane || "")}">
                <button class="action-button" type="button" data-candidate-action="discuss">和 AI 讨论</button>
                <button class="action-button" type="button" data-candidate-action="paper">${paperAdded ? "更新纸面模拟池" : "加入纸面模拟池"}</button>
                <button class="action-button" type="button" data-candidate-action="keep">保留观察</button>
                <button class="action-button" type="button" data-candidate-action="observe">转只观察</button>
                <button class="action-button is-danger" type="button" data-candidate-action="review">降级复盘</button>
                <button class="action-button is-muted" type="button" data-candidate-action="hide">归档/忽略</button>
                <button class="action-button is-muted" type="button" data-candidate-action="clear">清除手动状态</button>
            </div>
            <div id="candidateActionNote" class="candidate-action-note">这些操作只改变本地研究/纸面模拟状态，不是买卖建议，也不会连接券商或下单。</div>
        </div>
    `;
}

function bindCandidateActionButtons(detail) {
    const panel = document.querySelector(".candidate-action-buttons");
    if (!panel) return;
    panel.querySelectorAll("[data-candidate-action]").forEach((button) => {
        button.addEventListener("click", () => handleCandidateAction(button, detail));
    });
}

async function handleCandidateAction(button, detail) {
    const panel = button.closest(".candidate-action-buttons");
    const note = document.getElementById("candidateActionNote");
    const action = button.dataset.candidateAction || "";
    const symbol = panel?.dataset.candidateSymbol || detail.basic_info?.symbol || "";
    const entryDate = panel?.dataset.candidateEntry || detail.strategy_review?.select_date || "";
    const lane = panel?.dataset.candidateLane || detail.strategy_review?.detail_lane || "";
    const labels = {
        discuss: "和 AI 讨论",
        paper: "加入纸面模拟池",
        keep: "手动保留观察",
        observe: "手动转只观察",
        review: "手动降级复盘",
        hide: "归档/忽略",
        clear: "清除手动状态",
    };
    if (action === "discuss") {
        openCandidateAgentDiscussion(detail);
        return;
    }
    button.disabled = true;
    if (note) note.textContent = `正在写入“${labels[action] || action}”…`;
    try {
        const payload = action === "paper"
            ? await postJson("/api/paper-simulation/add-candidate", {
                symbol,
                entry_date: entryDate,
                lane,
                note: "从候选详情手动加入纸面模拟池。",
            })
            : await postJson("/api/daily/candidate-action", {
                symbol,
                entry_date: entryDate,
                lane,
                action,
                note: labels[action] || "",
            });
        if (note) note.textContent = safeText(payload.message || payload.message_for_ui);
        if (action === "paper" && payload.agent_question_suggestion) {
            const input = document.getElementById("agentQuestionInput");
            if (input && !input.value.trim()) {
                input.value = safeText(payload.agent_question_suggestion);
            }
        }
        await loadCandidates();
    } catch (error) {
        if (note) note.textContent = `候选池操作失败：${escapeHtml(errorText(error))}`;
    } finally {
        button.disabled = false;
    }
}

function openCandidateAgentDiscussion(detail) {
    const info = detail.basic_info || {};
    const review = detail.strategy_review || {};
    const symbol = safeText(info.symbol || "");
    const name = safeText(info.name || symbol || "这只股票");
    const lane = safeText(review.strategy_lane || review.detail_lane || review.strategy_name || "当前策略");
    const reason = safeText(review.select_reason || detail.selection_reason || "");
    const input = document.getElementById("agentQuestionInput");
    const note = document.getElementById("candidateActionNote");
    const question = [
        `请按“纸面模拟池决策”框架分析 ${name}（${symbol}）。`,
        `当前分层：${lane}。`,
        reason ? `入选理由：${reason}。` : "",
        "请直接判断它更适合：建议进入纸面模拟池、暂缓进入、降级观察，还是进入降级复盘；并写清验证周期、成功条件、失败条件和需要补的数据。",
    ].filter(Boolean).join("\n");
    pendingAgentCandidate = {
        symbol,
        name,
        entry_date: safeText(review.select_date || ""),
        lane: safeText(review.detail_lane || ""),
    };
    lastAgentResearchOpinion = "";
    renderPendingAgentCandidateAction();
    if (input) input.value = question;
    closeDetail();
    if (typeof window.quantdeskShowModuleView === "function") {
        window.quantdeskShowModuleView("agent-section", true, true, true);
    }
    if (note) note.textContent = "已把这只股票带到 LLM 研究室；是否调用外部 API 仍由你手动勾选。";
    [80, 400, 900].forEach((delay) => {
        window.setTimeout(() => {
            const freshInput = document.getElementById("agentQuestionInput");
            const freshSkillSelect = document.getElementById("agentSkillSelect");
            const option = freshSkillSelect
                ? [...freshSkillSelect.options].find((item) => item.value === "paper_simulation_decision")
                : null;
            if (option) freshSkillSelect.value = "paper_simulation_decision";
            if (freshInput && !freshInput.value.trim()) freshInput.value = question;
            freshInput?.focus();
        }, delay);
    });
}

function safeHtml(html) {
    const text = safeText(html);
    const template = document.createElement("template");
    template.innerHTML = text;
    // Remove dangerous element types.
    template.content.querySelectorAll("script, iframe, object, embed, form, input, textarea, select, button, link, meta, img, picture, video, audio, source, svg, math, style").forEach((node) => node.remove());
    // Strip on* event-handler attributes, javascript: URIs, and inline styles
    // from every surviving element to close the remaining XSS vectors.
    template.content.querySelectorAll("*").forEach((el) => {
        for (const attr of [...el.attributes]) {
            const name = attr.name.toLowerCase();
            if (name.startsWith("on")) {
                el.removeAttribute(attr.name);
            } else if (["src", "srcset", "background", "poster"].includes(name)) {
                // Explanatory HTML is text, never permission to load resources.
                el.removeAttribute(attr.name);
            } else if ((name === "href" || name === "src" || name === "action") && /^\s*javascript\s*:/i.test(attr.value)) {
                el.removeAttribute(attr.name);
            }
        }
        // Inline style can carry expression() / url() payloads in older engines.
        if (el.hasAttribute("style")) el.removeAttribute("style");
    });
    return template.innerHTML;
}

function setupModal() {
    const modal = document.getElementById("detailModal");
    const closeButton = document.getElementById("closeModalButton");
    closeButton.addEventListener("click", closeModal);
    const returnButton = document.getElementById("workbenchReturn");
    if (returnButton) returnButton.addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
        if (event.target === modal) closeModal();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && modal.classList.contains("open")) closeModal();
    });
}

function closeModal(options = {}) {
    const context = currentWorkbenchContext;
    detailRequestSequence += 1;
    stockWorkbenchController?.stop();
    document.getElementById("detailModal").classList.remove("open");
    document.body.classList.remove("modal-open");
    currentDetailChartData = null;
    currentDetailResearchReference = null;
    currentWorkbenchContext = null;
    disposeActiveCharts();
    if (options.restore !== false && /^#watch\//.test(window.location.hash || "")) {
        history.replaceState(null, "", context?.returnHash || "#watch");
        if (typeof window.quantdeskRestoreView === "function") window.quantdeskRestoreView();
        window.scrollTo({ top: context?.scrollY || 0, behavior: "auto" });
    }
}

function workbenchCandidates() {
    return currentWorkbenchContext?.items || [];
}

function setupWorkbenchSwitcher(symbol) {
    const prev = document.getElementById("workbenchPrev");
    const next = document.getElementById("workbenchNext");
    const list = workbenchCandidates().filter((item) => hasValue(item && item.symbol));
    const index = list.findIndex((item) => String(item.symbol) === String(symbol));
    const go = (offset) => {
        if (!list.length || index < 0) return;
        const nextItem = list[(index + offset + list.length) % list.length];
        if (nextItem) return showDetail(nextItem, { context: currentWorkbenchContext });
    };
    if (prev) {
        prev.disabled = list.length < 2 || index < 0;
        prev.onclick = () => go(-1);
    }
    if (next) {
        next.disabled = list.length < 2 || index < 0;
        next.onclick = () => go(1);
    }
}

let workbenchOverlayCache = null;

function chartEventLayer(kind) {
    if (kind === "paper_fill") return "paper_fill";
    if (kind === "ai_plan") return "ai_plan";
    return ["rule_signal", "selection", "reason", "expected_review", "next_review"].includes(kind) ? "rule_signal" : "unclassified";
}

function chartEventMetadata(row) {
    const evidence = row.evidence ?? row.price_evidence ?? row.fill_evidence ?? row.result_evidence ?? null;
    return {
        ...row,
        event_id: row.event_id || row.fill_id || row.trade_id || row.intent_id || row.decision_id || row.id || "",
        source: row.source || row.price_source || evidence?.source || "",
        event_time: row.event_time || row.confirmed_at || row.executed_at || row.filled_at || row.price_timestamp || row.timestamp || evidence?.timestamp || "",
        price_basis: row.price_basis || evidence?.price_basis || "unknown",
        evidence,
    };
}

function chartFramePriceBasis(chartData) {
    const labels = [];
    if (hasValue(chartData.price_basis)) labels.push(String(chartData.price_basis).trim().toLowerCase());
    for (const row of chartData.kline_data || []) {
        if (hasValue(row.price_basis)) labels.push(String(row.price_basis).trim().toLowerCase());
        else if (!hasValue(chartData.price_basis)) return "unknown";
    }
    const unique = new Set(labels);
    const basis = labels[0];
    return unique.size === 1 && ["raw", "qfq", "hfq"].includes(basis) ? basis : "unknown";
}

function chartEventPlotState(marker, chartData, dates) {
    const reasons = [];
    const date = String(marker.date || "").slice(0, 10);
    const stamp = String(marker.event_time || "");
    const price = numericValue(marker.price);
    const basis = String(marker.price_basis || "unknown").toLowerCase();
    const chartBasis = chartFramePriceBasis(chartData);
    if (marker.plot === false) reasons.push(marker.unplotted_reason || "来源记录没有允许绘图的成交/计划状态");
    if (!hasValue(marker.event_id)) reasons.push("缺少来源记录 ID");
    if (!hasValue(marker.source)) reasons.push("缺少价格/事件来源");
    if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(stamp) || !Number.isFinite(Date.parse(stamp))) reasons.push("缺少有效事件时间戳");
    else {
        const eventDate = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(stamp)
            ? new Date(Date.parse(stamp) + 8 * 60 * 60 * 1000).toISOString().slice(0, 10)
            : stamp.slice(0, 10);
        if (date !== eventDate) reasons.push("记录日期与事件时间不一致，不倒填图点");
    }
    if (!dates.includes(date)) reasons.push("事件日期不在当前 K 线范围，未移到末根 K 线");
    if (chartBasis === "unknown") reasons.push("K 线价格口径未提供或混合");
    if (!["raw", "qfq", "hfq"].includes(basis)) reasons.push("事件价格口径缺失/未知");
    else if (chartBasis !== "unknown" && basis !== chartBasis) reasons.push(`价格口径不一致：事件 ${basis} / K 线 ${chartBasis}，未进行转换`);
    if (marker.kind === "paper_fill" && (price === null || price <= 0)) reasons.push("缺少有效成交价，不用收盘价代填");
    if (chartEventLayer(marker.kind) === "unclassified") reasons.push("此记录不是可绘图的规则信号、AI 计划或纸面成交");
    return { plot: reasons.length === 0, reasons };
}

async function loadChartOverlays(symbol, profile = "default") {
    const wanted = String(symbol || "");
    const overlays = [];
    if (!wanted) return overlays;
    if (!workbenchOverlayCache) workbenchOverlayCache = new Map();
    if (!workbenchOverlayCache.has(profile)) {
        // Cache the in-flight promise, never a half-filled object. Another
        // stock opened while this is loading must await the same evidence.
        workbenchOverlayCache.set(profile, Promise.allSettled([
            requestJson("/api/decision-board"),
            requestJson("/api/ai-trader/dashboard" + aiTraderProfileQuery(profile)),
        ]));
    }
    const settled = await workbenchOverlayCache.get(profile);
    const aiSource = "/api/ai-trader/dashboard" + aiTraderProfileQuery(profile);
    const aiLabel = profile === "default" ? "" : "GLM · ";
    overlays.warnings = settled.flatMap((result, index) => {
        const name = index === 0 ? "策略账事件" : `${aiLabel}AI 账计划/成交`;
        if (result.status !== "fulfilled" || !result.value || result.value.ok === false) return [`${name}读取失败，事件范围不完整。`];
        return result.value.degraded ? [`${name}降级展示，记录可能不完整。`] : [];
    });
    const board = settled[0].status === "fulfilled" && settled[0].value?.ok !== false ? settled[0].value || {} : {};
    for (const event of Array.isArray(board.events) ? board.events : []) {
        if (String(event.symbol || "") !== wanted) continue;
        const action = String(event.action || "");
        const date = event.date || event.trade_date || event.decision_date || "";
        const noFill = action === "no_fill" || String(event.fill_model || "") === "missing_intraday";
        overlays.push(chartEventMetadata({
            ...event,
            layer: "paper_fill",
            ledger: "策略账",
            kind: action === "buy" || action === "sell" ? "paper_fill" : "paper_event",
            panel: "price",
            date,
            label: noFill ? "策略账·未成交" : (action === "sell" ? "策略账·纸面卖" : action === "buy" ? "策略账·纸面买" : `策略账·${action || "事件"}`),
            text: safeText(event.reason || event.note || "纸面账本事件，不是真成交。"),
            side: action,
            price: event.price,
            price_basis: event.price_basis || "unknown",
            api_source: "/api/decision-board · events",
            plot: !noFill && Boolean(date) && (action === "buy" || action === "sell"),
            unplotted_reason: noFill ? "策略账记录为未成交" : "",
        }));
    }
    const ai = settled[1].status === "fulfilled" && settled[1].value?.ok !== false ? settled[1].value || {} : {};
    const plan = ai.latest_plan || {};
    const addIntent = (intent, committed) => {
        if (String(intent.symbol || "") !== wanted) return;
        const recordedAt = intent.recorded_at || intent.created_at || plan.recorded_at || plan.created_at || "";
        overlays.push(chartEventMetadata({
            ...intent,
            layer: "ai_plan",
            ledger: "AI账",
            kind: "ai_plan",
            panel: "price",
            date: recordedAt ? String(recordedAt).slice(0, 10) : plan.decision_date || "",
            event_time: recordedAt,
            target_date: plan.trade_date || "",
            recovery_status: ai.llm?.recovery_status,
            raw_response_reused: ai.llm?.raw_response_reused,
            label: aiLabel + (committed ? "AI·已提交" : "AI·拟议"),
            text: `${committed ? "已提交次日纸面计划" : "仅提议、未提交"}；${safeText(intent.side || "")} ${safeText(intent.quantity || "")}。不是成交。`,
            side: intent.side,
            api_source: `${aiSource} · ${committed ? "committed_intents" : "uncommitted_intents"}`,
        }));
    };
    (Array.isArray(ai.committed_intents) ? ai.committed_intents : []).forEach((row) => addIntent(row, true));
    (Array.isArray(ai.uncommitted_intents) ? ai.uncommitted_intents : []).forEach((row) => addIntent(row, false));
    const fills = Array.isArray(ai.filled_trades) ? ai.filled_trades : [];
    for (const fill of fills) {
        if (String(fill.symbol || "") !== wanted) continue;
        const date = fill.date || fill.settled_on || "";
        overlays.push(chartEventMetadata({
            ...fill,
            layer: "paper_fill",
            ledger: "AI账",
            kind: "paper_fill",
            panel: "price",
            date,
            label: aiLabel + (fill.side === "sell" ? "AI账·纸面卖" : "AI账·纸面买"),
            text: `账本记录成交价 ${safeText(fill.price)}；是否可叠图还需核对时间、来源和价格口径。`,
            side: fill.side,
            price: fill.price,
            api_source: `${aiSource} · filled_trades`,
        }));
    }
    return overlays;
}

function mergeChartMarkers(baseMarkers, overlays) {
    return [...(Array.isArray(baseMarkers) ? baseMarkers : []), ...(overlays || [])]
        .map((item) => chartEventMetadata({ ...item, panel: item.panel || "price" }));
}

function renderWorkbenchEventList(overlays, chartData = {}, warnings = []) {
    const host = document.getElementById("workbenchEventList");
    if (!host) return;
    if (!overlays.length) {
        host.innerHTML = `<p class="section-note">当前已读来源没有这只股票的规则观察 / AI 计划 / 纸面成交。${escapeHtml(warnings.join(" "))}</p>`;
        return;
    }
    host.innerHTML = `
        <h3>本图事件（三种身份）</h3>
        <p class="section-note">菱形=规则观察；空心圆=AI 计划；实心三角=纸面成交。入选观察不是买入。K 线口径：${escapeHtml(chartFramePriceBasis(chartData))}。缺证据的记录只列在下面。</p>
        ${warnings.map((warning) => `<p class="section-note">${escapeHtml(warning)}</p>`).join("")}
        <ul class="workbench-event-list">${overlays.map((item) => {
            const status = chartEventPlotState(item, chartData, (chartData.kline_data || []).map((row) => String(row.date)));
            return `<li data-chart-event-id="${escapeHtml(String(item.event_id || ""))}" data-chart-event-source="${escapeHtml(String(item.source || ""))}" data-chart-event-ledger="${escapeHtml(String(item.ledger || ""))}">
                <strong>${escapeHtml(safeText(item.label))}</strong> · ${escapeHtml(safeText(item.ledger))}
                · ${escapeHtml(safeText(item.event_time || item.date))}
                <div>${escapeHtml(status.plot ? "已画在图上" : `只在列表：${status.reasons.join("；")}`)}</div>
                <div>${escapeHtml(safeText(item.text))}</div>
                ${item.st_unknown ? `<p class="section-note">身份未知 · ${escapeHtml(item.st_note || "缺少当时身份资料")}</p>` : ""}
                ${item.recovery_status || item.raw_response_reused ? `<p class="section-note">恢复状态：${escapeHtml(item.recovery_status || "未记录")} ${item.raw_response_reused ? "· 复用原判断，原时间保留" : ""}</p>` : ""}
                <details><summary>记录与证据</summary>
                    <p>ID：${escapeHtml(safeText(item.event_id))} · 来源：${escapeHtml(safeText(item.source))} · ${escapeHtml(safeText(item.api_source))}</p>
                    <p>原价：${escapeHtml(safeText(item.price))} · 口径：${escapeHtml(safeText(item.price_basis))} · 目标日期：${escapeHtml(safeText(item.target_date))}</p>
                    <pre>${escapeHtml(item.evidence === null || item.evidence === undefined ? "未提供附加证据" : JSON.stringify(item.evidence, null, 2))}</pre>
                </details>
            </li>`;
        }).join("")}</ul>
    `;
}

function renderChart(chartData) {
    const container = document.getElementById("chartContainer");
    if (!container) return;
    disposeActiveCharts();
    if (typeof echarts !== "object") {
        container.innerHTML = missingBlock("本地 ECharts 文件缺失。请将 echarts.min.js 放到 app/static/echarts.min.js 后刷新页面。");
        return;
    }
    const rows = chartData.kline_data || [];
    if (!rows.length) {
        container.innerHTML = missingBlock("本地 K 线数据缺失，请更新 data/daily 后刷新。");
        return;
    }    const dates = rows.map((row) => safeText(row.date));
    const ohlc = rows.map((row) => {
        const values = [row.open, row.close, row.low, row.high].map((v) => numericValue(v));
        return values.includes(null) ? null : values;
    });
    const markers = normalizeMarkers(chartData, dates);
    renderPriceChart(rows, dates, ohlc, markers);
    renderVolumeChart(rows, dates, ohlc, markers);
    renderMacdChart(rows, dates, markers);
    renderKdjChart(rows, dates, markers);
    renderRsiChart(rows, dates, markers);
}

function normalizeMarkers(chartData, dates) {
    // Two events on one date are two records. Never deduplicate by panel/date,
    // clamp a future event onto today's bar, or fabricate fallback signals.
    return (Array.isArray(chartData.markers) ? chartData.markers : [])
        .filter((marker) => chartEventPlotState(marker, chartData, dates).plot);
}

function referenceLineSeries(level) {
    // Research observation price drawn as a horizontal line. It is a reference
    // level from the existing daily scenario, never an order or a target.
    const price = numericValue(level?.price);
    if (price === null || price <= 0) return null;
    const tone = level.kind === "buy" ? "#2563EB" : level.kind === "stop" ? "#B91C1C" : "#0F766E";
    const label = safeText(level.label || level.kind || "参考位") + " " + price.toFixed(2);
    return {
        name: label,
        type: "line",
        data: [],
        symbol: "none",
        silent: true,
        color: tone,
        markLine: {
            symbol: "none",
            silent: true,
            animation: false,
            precision: 2,
            label: { formatter: label, position: "insideEndTop", color: tone, fontSize: 10 },
            lineStyle: { color: tone, type: "dashed", width: 1 },
            data: [{ yAxis: price }],
        },
    };
}

function renderPriceChart(rows, dates, ohlc, markers) {
    const chart = initChart("priceChart");
    if (!chart) return;
    const levels = Array.isArray(currentDetailReferenceLevels) ? currentDetailReferenceLevels : [];
    chart.setOption(baseChartOption(dates, {
        legend: { top: 8, left: "center", type: "scroll" },
        yAxis: { scale: true },
        series: [
            {
                name: "K线",
                type: "candlestick",
                data: ohlc,
                itemStyle: {
                    color: "#E54848",
                    color0: "#3FBF6F",
                    borderColor: "#E54848",
                    borderColor0: "#3FBF6F",
                },
            },
            lineSeries("MA5", rows.map((row) => row.ma5)),
            lineSeries("MA10", rows.map((row) => row.ma10)),
            lineSeries("MA20", rows.map((row) => row.ma20)),
            ...markerSeries("price", markers, dates, rows, (row) => row.close),
            ...levels.map(referenceLineSeries).filter(Boolean),
        ].filter(Boolean),
    }), true);
}

function renderVolumeChart(rows, dates, ohlc, markers) {
    const chart = initChart("volumeChart");
    if (!chart) return;
    chart.setOption(baseChartOption(dates, {
        legend: { show: false },
        yAxis: { scale: true },
        series: [
            {
                name: "成交量",
                type: "bar",
                data: rows.map((row) => numericValue(row.volume)),
                itemStyle: {
                    color: (params) => {
                        const bar = ohlc[params.dataIndex];
                        return bar ? (bar[1] >= bar[0] ? "#DC2626" : "#15803D") : "#64748B";
                    },
                },
            },
            ...markerSeries("volume", markers, dates, rows, (row) => row.volume, "#a16207"),
        ].filter(Boolean),
    }));
}

function renderMacdChart(rows, dates, markers) {
    const chart = initChart("macdChart");
    if (!chart) return;
    chart.setOption(baseChartOption(dates, {
        legend: { top: 8, left: "center", type: "scroll" },
        yAxis: { scale: true },
        series: [
            lineSeries("DIF", rows.map((row) => row.macd_dif), { lineStyle: { width: 1.5, color: "#DC2626" } }),
            lineSeries("DEA", rows.map((row) => row.macd_dea), { lineStyle: { width: 1.5, color: "#15803D" } }),
            {
                name: "MACD柱",
                type: "bar",
                data: rows.map((row) => numericValue(row.macd_bar) ?? 0),
                itemStyle: { color: (params) => params.data >= 0 ? "#DC2626" : "#15803D" },
            },
            ...markerSeries("macd", markers, dates, rows, (row) => row.macd_dif),
        ].filter(Boolean),
    }));
}

function renderKdjChart(rows, dates, markers) {
    const chart = initChart("kdjChart");
    if (!chart) return;
    chart.setOption(baseChartOption(dates, {
        legend: { top: 8, left: "center", type: "scroll" },
        yAxis: { scale: true },
        series: [
            lineSeries("K", rows.map((row) => row.kdj_k), { markLine: thresholdLines([20, 80]) }),
            lineSeries("D", rows.map((row) => row.kdj_d)),
            lineSeries("J", rows.map((row) => row.kdj_j)),
            ...markerSeries("kdj", markers, dates, rows, (row) => row.kdj_k),
        ].filter(Boolean),
    }));
}

function renderRsiChart(rows, dates, markers) {
    const chart = initChart("rsiChart");
    if (!chart) return;
    chart.setOption(baseChartOption(dates, {
        legend: { show: false },
        yAxis: { min: 0, max: 100 },
        series: [
            lineSeries("RSI", rows.map((row) => row.rsi), { markLine: thresholdLines([30, 70]) }),
            ...markerSeries("rsi", markers, dates, rows, (row) => row.rsi),
        ].filter(Boolean),
    }));
}

function baseChartOption(dates, overrides = {}) {
    return {
        animation: false,
        backgroundColor: "transparent",
        color: ["#5B9BD5", "#B084EB", "#E0A030", "#8892A8", "#E54848", "#3FBF6F"],
        tooltip: { trigger: "axis", confine: true, axisPointer: { type: "cross", lineStyle: { color: "#4a4a5e" } }, backgroundColor: "#252535", borderColor: "#3a3a4e", textStyle: { color: "#d0d0e0" } },
        legend: { top: 8, left: "center", type: "scroll", textStyle: { color: "#a0a0b8" } },
        textStyle: { color: "#a0a0b8", fontFamily: "'Fira Sans', sans-serif", fontSize: 11 },
        grid: { left: 58, right: 28, top: 48, bottom: 52 },
        dataZoom: [
            { type: "inside", start: 45, end: 100 },
            { type: "slider", bottom: 8, height: 18, start: 45, end: 100, backgroundColor: "#222238", fillerColor: "rgba(91,155,213,0.15)", borderColor: "#3a3a4e", textStyle: { color: "#6a6a80" } },
        ],
        xAxis: {
            type: "category",
            data: dates,
            boundaryGap: true,
            axisLabel: { hideOverlap: true, color: "#6a6a80", fontSize: 10 },
            axisLine: { lineStyle: { color: "#3a3a4e" } },
        },
        yAxis: { scale: true, axisLabel: { color: "#6a6a80", fontSize: 10 }, splitLine: { lineStyle: { color: "#2a2a3e" } }, axisLine: { lineStyle: { color: "#3a3a4e" } } },
        ...overrides,
    };
}

function lineSeries(name, data, options = {}) {
    return {
        name,
        type: "line",
        data: data.map((value) => numericValue(value)),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.4 },
        ...options,
    };
}

function markerSeries(panel, markers, dates, rows, valueResolver, color = "#1D4ED8") {
    const styles = {
        rule_signal: { name: "入选观察", symbol: "diamond", color: "#2563C9" },
        ai_plan: { name: "AI计划", symbol: "emptyCircle", color: "#B45309" },
        paper_fill: { name: "纸面成交", symbol: "triangle", color: "#0F766E" },
    };
    return Object.entries(styles).map(([layer, style]) => {
        const data = markers
            .filter((marker) => marker.panel === panel && chartEventLayer(marker.kind) === layer)
            .map((marker) => {
                const date = safeText(marker.date);
                const index = findMarkerIndex(dates, date, marker.kind);
                if (index < 0 || !rows[index]) return null;
                const y = marker.kind === "paper_fill"
                    ? numericValue(marker.price)
                    : numericValue(marker.price) ?? numericValue(valueResolver(rows[index], index));
                if (y === null) return null;
                // ECharts reserves data.symbol for its marker shape. The
                // stock's source-owned symbol belongs in separate metadata.
                const { symbol: stockSymbol, ...eventMetadata } = marker;
                return {
                    ...eventMetadata,
                    stock_symbol: stockSymbol,
                    value: [dates[index], y],
                    date: dates[index],
                    label: safeText(marker.label),
                    markerText: safeText(marker.text),
                    kind: safeText(marker.kind),
                    itemStyle: { color: style.color },
                };
            })
            .filter(Boolean);
        if (!data.length) return null;
        return {
            name: style.name,
            type: "scatter",
            data,
            symbol: style.symbol,
            symbolSize: 18,
            tooltip: {
                trigger: "item",
                formatter: (params) => {
                    const item = params.data || {};
                    return `${escapeHtml(item.label)}<br/>${escapeHtml(safeText(item.event_time))}<br/>${escapeHtml(safeText(item.source))} · ${escapeHtml(safeText(item.price_basis))}<br/>ID ${escapeHtml(safeText(item.event_id))}<br/>${escapeHtml(item.markerText)}`;
                },
            },
            label: {
                show: true,
                formatter: (params) => params.data.label,
                position: "top",
                color: "#0F172A",
                fontSize: 11,
                backgroundColor: "rgba(255,255,255,.96)",
                borderColor: "#CBD5E1",
                borderWidth: 1,
                borderRadius: 4,
                padding: [2, 4],
            },
            z: 10,
        };
    }).filter(Boolean);
}

function thresholdLines(values) {
    return {
        silent: true,
        symbol: "none",
        lineStyle: { color: "#94a3b8", type: "dashed", width: 1 },
        label: { color: "#64748b", formatter: "{c}" },
        data: values.map((value) => ({ yAxis: value })),
    };
}

function initChart(id) {
    const element = document.getElementById(id);
    if (!element) return null;
    const chart = echarts.init(element, null, { renderer: "canvas" });
    chart.on("click", (params) => {
        const event = params.data;
        if (!event || !event.event_id) return;
        const host = document.getElementById("workbenchEventList");
        const row = Array.from(host?.querySelectorAll("[data-chart-event-id]") || []).find((node) =>
            node.dataset.chartEventId === String(event.event_id)
            && node.dataset.chartEventSource === String(event.source || "")
            && node.dataset.chartEventLedger === String(event.ledger || ""));
        if (!row) return;
        const details = row.querySelector("details");
        if (details) details.open = true;
        row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    activeCharts.push(chart);
    return chart;
}

function findMarkerIndex(dates, markerDate, kind) {
    const cleanDate = safeText(markerDate);
    const exact = dates.indexOf(cleanDate);
    if (exact >= 0) return exact;
    if (kind === "paper_fill" || kind === "ai_plan") return -1;
    return kind === "next_review" ? dates.length - 1 : findSelectIndex(dates, cleanDate);
}

function findSelectIndex(dates, selectDate) {
    const cleanDate = safeText(selectDate);
    if (cleanDate === "数据缺失") return dates.length - 1;
    const exact = dates.indexOf(cleanDate);
    if (exact >= 0) return exact;
    console.warn(`[daily_research] select date "${cleanDate}" not found in chart dates, falling back to last bar`);
    return dates.length - 1;
}

function resizeActiveCharts() {
    activeCharts.forEach((chart) => chart.resize());
    resizeHomeCharts();
}

async function loadNewsStatus() {
    try {
        const status = await requestJson("/api/news/status");
        const banner = document.getElementById("newsStatusBanner");
        const el = document.getElementById("newsStatus");
        if (!banner || !el) return;
        banner.hidden = false;
        const age = status.age_days;
        if (age !== null && age !== undefined && age > 7) {
            el.textContent = `新闻缓存距今 ${age} 天 · 当前股票公告/事件覆盖未核实`;
            el.className = "badge badge-warning";
        } else if (status.file_count === 0) {
            el.textContent = "新闻缓存为空 · 未完成消息面核查";
            el.className = "badge badge-warning";
        } else {
            el.textContent = `新闻缓存 ${status.file_count ?? "未知"} 文件 · ${age ?? "未知"}天前 · 不代表当前股票事件已检查`;
            el.className = "badge";
        }
    } catch (e) {
        // non-critical
    }
}

function disposeActiveCharts() {
    activeCharts.forEach((chart) => chart.dispose());
    activeCharts = [];
}

// ---- 今日速览首屏仪表盘 -------------------------------------------------
// Home-dashboard charts keep their own registry: the detail-modal lifecycle
// disposes everything in `activeCharts`, and these charts must survive it.

const HOME_CHART_TEXT = "#0F172A";
const HOME_CHART_MUTED = "#475569";
const HOME_CHART_GRID = "#D6CFC4";
const HOME_CHART_UP = "#DC2626";
const HOME_CHART_DOWN = "#15803D";
const HOME_STRATEGY_COLORS = ["#1D4ED8", "#7C3AED", "#B45309", "#0F766E", "#475569"];
const HOME_HEALTH_LABELS = { green: "良好", yellow: "注意", red: "异常" };

function ensureHomeChart(id) {
    const element = document.getElementById(id);
    if (!element || typeof echarts === "undefined") return null;
    const existing = homeCharts[id];
    if (existing && !existing.isDisposed()) return existing;
    const chart = echarts.init(element, null, { renderer: "canvas" });
    homeCharts[id] = chart;
    return chart;
}

function resizeHomeCharts() {
    Object.values(homeCharts).forEach((chart) => {
        if (chart && !chart.isDisposed()) chart.resize();
    });
}

function showHomeChart(id, visible, emptyMessage) {
    const element = document.getElementById(id);
    const empty = document.getElementById(`${id}Empty`);
    if (element) element.hidden = !visible;
    if (empty) {
        empty.hidden = Boolean(visible);
        if (!visible && emptyMessage) empty.textContent = emptyMessage;
    }
}

function homeCategoryAxis(labels) {
    return {
        type: "category",
        data: labels,
        axisLabel: { color: HOME_CHART_TEXT, fontSize: 12 },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: HOME_CHART_GRID } },
    };
}

function homeValueAxis(suffix = "") {
    return {
        type: "value",
        axisLabel: { color: HOME_CHART_MUTED, fontSize: 11, formatter: (value) => `${value}${suffix}` },
        splitLine: { lineStyle: { color: HOME_CHART_GRID } },
    };
}

function candidatePriorityScore(candidate) {
    const weighted = numericValue(candidate.weighted_priority_score);
    if (weighted !== null) return weighted;
    return numericValue(candidate.priority_score);
}

function renderHomeCandidates(data) {
    renderHomeKpiCandidates(data);
    renderHomeCandidateChart(data);
}

function homeCandidateBatch(data) {
    const generation = data?.candidate_generation || {};
    const signalDate = String(generation.signal_date || "");
    const targetDate = String(generation.target_date || "");
    const count = generation.candidate_count;
    const complete = ["completed", "completed_with_warnings", "passed", "passed_with_warnings"].includes(generation.status);
    const dated = /^\d{4}-\d{2}-\d{2}$/.test(signalDate) && signalDate === targetDate;
    const validCount = Number.isSafeInteger(count) && count >= 0;
    const current = complete && dated && validCount && generation.freshness_status === "current";
    return { generation, signalDate, targetDate, count: current ? count : null, current };
}

function renderHomeKpiCandidates(data) {
    const valueEl = document.getElementById("kpiCandidatesValue");
    const subEl = document.getElementById("kpiCandidatesSub");
    if (!valueEl || !subEl) return;
    if (!data) {
        valueEl.textContent = "—";
        subEl.textContent = "候选接口暂时不可用";
        return;
    }
    const batch = homeCandidateBatch(data);
    const focusCount = Array.isArray(data.daily_focus) ? data.daily_focus.length : 0;
    const observationCount = Array.isArray(data.observation_candidates) ? data.observation_candidates.length : null;
    valueEl.textContent = batch.count === null ? "—" : `${batch.count} 只`;
    const parts = [];
    if (batch.current) parts.push(`今日新生成 ${batch.count} · ${batch.signalDate}`);
    else parts.push(`今日批次未核验${batch.signalDate ? `；最近批次 ${batch.signalDate}` : ""}`);
    if (batch.targetDate) parts.push(`目标 ${batch.targetDate}`);
    if (focusCount) parts.push(`可读复核池 ${focusCount}`);
    if (observationCount !== null) parts.push(`历史观察 ${observationCount}`);
    subEl.textContent = parts.join(" · ");
}

function renderHomeCandidateChart(data) {
    const note = document.getElementById("homeCandidateNote");
    if (!data) {
        showHomeChart("homeCandidateChart", false, "候选接口暂时不可用，无法绘制优先分条形图。");
        return;
    }
    const todayRows = Array.isArray(data.today_recommendations) ? data.today_recommendations : [];
    const focusRows = Array.isArray(data.daily_focus) ? data.daily_focus : [];
    const usingFallback = !todayRows.length && focusRows.length > 0;
    const batch = homeCandidateBatch(data);
    const rows = todayRows.length ? todayRows : focusRows;
    const scored = rows
        .map((candidate) => ({ candidate, score: candidatePriorityScore(candidate) }))
        .filter((item) => item.score !== null)
        .sort((a, b) => b.score - a.score)
        .slice(0, 10);
    if (!scored.length) {
        showHomeChart("homeCandidateChart", false, "今日暂无带研究优先分的候选。生成今日候选后，此处会出现优先分条形图。");
        return;
    }
    if (note) {
        const sourceText = usingFallback
            ? `历史观察/复核池排序；${batch.current && batch.count === 0 ? "当日批次已核验为 0 只" : "今日批次数量未核验"}`
            : `${batch.current ? `今日批次 ${batch.signalDate}` : `最近候选批次 ${batch.signalDate || "日期未核验"}`}；可读 ${rows.length} 只，按研究优先分显示前 ${scored.length} 只`;
        note.textContent = `${sourceText}；点击条形打开个股详情。纸面研究 · 未验证；本图不计算胜率，分数只供排序，不表示盈利概率；微小分差不证明优势。`;
    }
    showHomeChart("homeCandidateChart", true);
    const chart = ensureHomeChart("homeCandidateChart");
    if (!chart) return;
    const ordered = [...scored].reverse();
    const buckets = [...new Set(ordered.map((item) => safeText(item.candidate.strategy_bucket_canonical || item.candidate.strategy_bucket || "其他")))];
    const colorFor = (candidate) => {
        const bucket = safeText(candidate.strategy_bucket_canonical || candidate.strategy_bucket || "其他");
        return HOME_STRATEGY_COLORS[buckets.indexOf(bucket) % HOME_STRATEGY_COLORS.length];
    };
    chart.setOption({
        grid: { left: 8, right: 48, top: 8, bottom: 8, containLabel: true },
        xAxis: homeValueAxis(),
        yAxis: homeCategoryAxis(ordered.map((item) => `${safeText(item.candidate.name)} ${safeText(item.candidate.symbol)}`)),
        tooltip: {
            trigger: "item",
            confine: true,
            formatter: (params) => {
                const entry = ordered[params.dataIndex] || {};
                const candidate = entry.candidate || {};
                const tracking = candidate.tracking || {};
                return [
                    `<strong>${escapeHtml(safeText(candidate.name))} ${escapeHtml(safeText(candidate.symbol))}</strong>`,
                    `研究优先分：${escapeHtml(safeText(entry.score))}`,
                    escapeHtml(safeText(candidate.strategy_weight_note || "策略权重中性")),
                    `策略：${escapeHtml(safeText(candidate.strategy_lane || candidate.strategy_name))}`,
                    `数据状态：${escapeHtml(humanStatus(candidate.data_status))} · 跟踪：${escapeHtml(safeText(tracking.status_label))}`,
                    "点击条形查看完整详情（Research Only）",
                ].join("<br/>");
            },
        },
        series: [{
            type: "bar",
            barMaxWidth: 18,
            cursor: "pointer",
            data: ordered.map((item) => ({
                value: item.score,
                itemStyle: { color: colorFor(item.candidate), borderRadius: [0, 4, 4, 0] },
            })),
            label: {
                show: true,
                position: "right",
                color: HOME_CHART_MUTED,
                fontSize: 11,
                formatter: (params) => safeText(ordered[params.dataIndex] ? ordered[params.dataIndex].score : ""),
            },
        }],
    }, true);
    chart.off("click");
    chart.on("click", (params) => {
        const entry = ordered[params.dataIndex];
        if (entry && entry.candidate && hasValue(entry.candidate.symbol)) showDetail(entry.candidate);
    });
    chart.resize();
}

function renderHomeSectorChart(gainers, losers, snapshot = {}) {
    const note = document.getElementById("homeSectorNote");
    if (note) note.textContent = `${sectorSnapshotText(snapshot)}。深红为涨、深绿为跌（A股约定）。`;
    const rows = [
        ...(Array.isArray(gainers) ? gainers : []).slice(0, 5),
        ...(Array.isArray(losers) ? losers : []).slice(0, 5),
    ]
        .map((item) => ({ name: safeText(item.name), change: numericValue(item.change) }))
        .filter((item) => item.change !== null && item.name !== "数据缺失")
        .sort((a, b) => a.change - b.change);
    if (!rows.length) {
        showHomeChart("homeSectorChart", false, "板块数据缺失。可点击“市场全景”里的“刷新板块”联网更新本地缓存。");
        return;
    }
    showHomeChart("homeSectorChart", true);
    const chart = ensureHomeChart("homeSectorChart");
    if (!chart) return;
    chart.setOption({
        grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
        xAxis: homeValueAxis("%"),
        yAxis: homeCategoryAxis(rows.map((row) => row.name)),
        tooltip: {
            trigger: "item",
            confine: true,
            formatter: (params) => {
                const row = rows[params.dataIndex] || {};
                return `${escapeHtml(safeText(row.name))}：${escapeHtml(formatChange(row.change))}`;
            },
        },
        series: [{
            type: "bar",
            barMaxWidth: 16,
            data: rows.map((row) => ({
                value: row.change,
                itemStyle: {
                    color: row.change >= 0 ? HOME_CHART_UP : HOME_CHART_DOWN,
                    borderRadius: row.change >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
                },
                label: {
                    show: true,
                    position: row.change >= 0 ? "right" : "left",
                    color: HOME_CHART_MUTED,
                    fontSize: 11,
                    formatter: () => formatChange(row.change),
                },
            })),
        }],
    }, true);
    chart.resize();
}

function renderHomePrevBatch(data) {
    const valueEl = document.getElementById("kpiPrevBatchValue");
    const subEl = document.getElementById("kpiPrevBatchSub");
    const note = document.getElementById("homePrevBatchNote");
    const previous = data && typeof data.previous_recommendation_performance === "object"
        ? (data.previous_recommendation_performance || {})
        : {};
    const items = Array.isArray(previous.items) ? previous.items : [];
    if (valueEl && subEl) {
        const reviewNeeded = Array.isArray(data && data.review_needed) ? data.review_needed.length : null;
        const reviewCount = numericValue(data && data.review_needed_count) ?? reviewNeeded;
        if (!data) {
            valueEl.textContent = "—";
            valueEl.className = "home-kpi-value";
            subEl.textContent = "策略成绩单暂时不可用；可进入策略成绩页面重试。";
        } else if (reviewCount !== null) {
            valueEl.textContent = `${reviewCount} 只`;
            valueEl.className = "home-kpi-value";
            subEl.textContent = reviewCount
                ? "成绩单标记需复核 · 不是实时对错证明"
                : "暂无到期复核样本";
        } else if (!previous.available || !items.length) {
            valueEl.textContent = "—";
            valueEl.className = "home-kpi-value";
            subEl.textContent = "暂无上一批候选复盘样本";
        } else {
            const avg = numericValue(previous.avg_return_since_select_pct);
            valueEl.textContent = avg === null ? "—" : formatSignedPercent(avg);
            valueEl.className = `home-kpi-value ${changeClass(avg)}`;
            subEl.textContent = `${safeText(previous.batch_date)} 批 ${safeText(previous.sample_count)} 只 · 符合预期 ${safeText(previous.fit_count ?? 0)}`;
        }
    }
    const rows = items
        .map((item) => ({ item, value: numericValue(item.return_since_select) }))
        .filter((row) => row.value !== null)
        .sort((a, b) => a.value - b.value);
    if (!rows.length) {
        showHomeChart("homePrevBatchChart", false, data
            ? "上一批候选暂无可复盘的涨跌数据（可能是新批次或数据缺口）。"
            : "策略成绩单暂时不可用，无法绘制复盘图。");
        return;
    }
    let display = rows;
    let truncationNote = "";
    if (rows.length > 16) {
        display = [...rows.slice(0, 8), ...rows.slice(-8)];
        truncationNote = `共 ${rows.length} 只，图中显示最好与最差各 8 只；`;
    }
    if (note) {
        note.textContent = `${truncationNote}上一批（${safeText(previous.batch_date)}）候选自入选以来的涨跌复盘；用于验证策略是否兑现，不代表推荐。`;
    }
    showHomeChart("homePrevBatchChart", true);
    const chart = ensureHomeChart("homePrevBatchChart");
    if (!chart) return;
    chart.setOption({
        grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
        xAxis: homeValueAxis("%"),
        yAxis: homeCategoryAxis(display.map((row) => `${safeText(row.item.name)} ${safeText(row.item.symbol)}`)),
        tooltip: {
            trigger: "item",
            confine: true,
            formatter: (params) => {
                const row = display[params.dataIndex] || {};
                const item = row.item || {};
                return [
                    `<strong>${escapeHtml(safeText(item.name))} ${escapeHtml(safeText(item.symbol))}</strong>`,
                    `入选以来：${escapeHtml(formatChange(row.value))} · 最大回撤：${escapeHtml(formatChange(item.max_drawdown_since_select))}`,
                    `状态：${escapeHtml(safeText(item.status_label))} · 经过 ${escapeHtml(safeText(item.elapsed_trading_days))} 个交易日`,
                ].join("<br/>");
            },
        },
        series: [{
            type: "bar",
            barMaxWidth: 16,
            data: display.map((row) => ({
                value: row.value,
                itemStyle: {
                    color: row.value >= 0 ? HOME_CHART_UP : HOME_CHART_DOWN,
                    borderRadius: row.value >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
                },
                label: {
                    show: true,
                    position: row.value >= 0 ? "right" : "left",
                    color: HOME_CHART_MUTED,
                    fontSize: 11,
                    formatter: () => formatChange(row.value),
                },
            })),
        }],
    }, true);
    chart.resize();
}

function renderHomeKpiDataHealth(data) {
    const valueEl = document.getElementById("kpiDataHealthValue");
    const subEl = document.getElementById("kpiDataHealthSub");
    if (!valueEl || !subEl) return;
    if (!data || data.ok === false) {
        valueEl.textContent = "—";
        subEl.textContent = "数据健康状态暂时不可用；可进入系统状态或点击刷新重试。";
        feedDutyLamp("data", "缺失", "健康看板不可读", "bad");
        return;
    }
    const level = String(data.health_level || "").trim().toLowerCase();
    valueEl.textContent = HOME_HEALTH_LABELS[level] || humanStatus(data.health_level || "unknown");
    valueEl.className = `home-kpi-value${level === "red" ? " health-bad" : level === "yellow" ? " health-warn" : ""}`;
    feedDutyLamp(
        "data",
        HOME_HEALTH_LABELS[level] || humanStatus(data.health_level || "unknown"),
        safeText(((data.cards || {}).daily_data || {}).value || "本地数据状态"),
        level === "green" ? "" : level === "red" ? "bad" : "warn"
    );
    const cards = data.cards || {};
    const daily = cards.daily_data || {};
    const pool = (cards.intraday_data || {}).verified_minute_pool || {};
    const parts = [];
    if (hasValue(daily.value)) parts.push(`日线 ${safeText(daily.value)}`);
    const usable = numericValue(pool.usable_count);
    const total = numericValue(pool.total_symbols);
    if (usable !== null && total !== null && total > 0) parts.push(`分钟池 ${usable}/${total}`);
    const minuteDone = numericValue(data.tushare_minute_symbol_done_count);
    const minuteTarget = numericValue(data.tushare_minute_target_symbol_count) ?? numericValue(data.tushare_minute_symbol_count);
    if (minuteDone !== null && minuteTarget !== null && minuteTarget > 0) parts.push(`分钟采集 ${minuteDone}/${minuteTarget}`);
    const reasons = Array.isArray(data.health_degraded_reason_codes) ? data.health_degraded_reason_codes.filter(Boolean) : [];
    if (reasons.includes("daily_data_scheduler_disabled")) {
        subEl.textContent = "日线计划任务是关掉的 · 健康快照偏旧";
    } else {
        subEl.textContent = parts.length ? parts.join(" · ") : safeText(data.summary_text);
    }
}

async function loadHomeEquity() {
    try {
        const decisionBoard = await requestJson("/api/decision-board");
        renderHomeEquity(decisionBoard);
    } catch (error) {
        console.warn("[home-dashboard] equity history unavailable", error);
        renderHomeEquity(null);
    }
}

async function loadHomeAiLedger() {
    const valueEl = document.getElementById("kpiAiValue");
    const subEl = document.getElementById("kpiAiSub");
    if (!valueEl || !subEl) return;
    try {
        const data = await requestJson("/api/ai-trader/dashboard");
        if (!data || data.ok === false) throw new Error("AI 账不可读");
        const account = (data && data.account) || {};
        const equity = numericValue(account.equity);
        const positions = numericValue(account.positions_count);
        valueEl.textContent = equity === null ? "—" : `¥${Math.round(equity).toLocaleString("zh-CN")}`;
        const parts = [];
        if (equity === null) parts.push("AI 账权益缺失，不能据此判定空仓或亏损");
        if (data.degraded === true) parts.push("AI 账降级展示");
        if (data.research_state?.records?.available === false) parts.push("判断记录不可读，数量未知");
        if (data.llm?.mode === "rule_fallback") parts.push(data.llm.decision_label || "未取得模型判断");
        if (data.llm?.raw_response_reused === true) parts.push("恢复原判断，原时间保留");
        if (positions !== null) parts.push(`纸面持仓 ${positions} 只`);
        if (hasValue(account.as_of)) parts.push(`截至 ${safeText(account.as_of)}`);
        subEl.textContent = parts.length ? parts.join(" · ") : "独立 AI 纸面账；数据日期未提供";
    } catch (error) {
        console.warn("[home-dashboard] AI ledger unavailable", error);
        valueEl.textContent = "—";
        subEl.textContent = "AI 账暂时不可读";
    }
}

function renderHomeEquity(decisionBoard) {
    const valueEl = document.getElementById("kpiPaperValue");
    const subEl = document.getElementById("kpiPaperSub");
    const unavailableText = "后端决策看板未返回权威净值序列。";
    if (!decisionBoard || decisionBoard.ok === false) {
        if (valueEl && subEl) {
            valueEl.textContent = "—";
            subEl.textContent = unavailableText;
        }
        feedDutyLamp("paper", "缺失", "净值序列不可读", "warn");
        showHomeChart("homeEquityChart", false, unavailableText);
        return;
    }
    const latest = decisionBoard.account || {};
    if (valueEl && subEl) {
        const equity = numericValue(latest.total_equity);
        const totalReturn = numericValue(latest.total_return_pct);
        valueEl.textContent = equity === null ? "—" : `¥${Math.round(equity).toLocaleString("zh-CN")}`;
        valueEl.className = `home-kpi-value ${changeClass(totalReturn)}`;
        const parts = [];
        if (totalReturn !== null) parts.push(`累计 ${formatChange(totalReturn)}`);
        if (numericValue(latest.position_count) !== null) parts.push(`纸面持仓 ${safeText(latest.position_count)} 只`);
        const valuationDate = latest.valuation_date || decisionBoard.envelope?.account?.valuation_date || decisionBoard.valuation_date;
        parts.push(hasValue(valuationDate) ? `估值截至 ${safeText(valuationDate)}` : "估值日期未提供");
        if (hasValue(latest.daily_pnl) && latest.daily_pnl_status === "verified") parts.push(`该日纸面盈亏 ¥${safeText(latest.daily_pnl)}`);
        if (latest.valuation_verified !== true || latest.pnl_identity?.status === "mismatch") parts.push("纸面估值与当日变化尚未完全核对（研究未验证）");
        subEl.textContent = parts.length ? parts.join(" · ") : "纸面模拟账户（不接券商）";
        feedDutyLamp(
            "paper",
            equity === null ? "—" : `¥${Math.round(equity).toLocaleString("zh-CN")}`,
            totalReturn === null ? "纸面模拟账户" : `累计 ${formatChange(totalReturn)}`,
            ""
        );
    }
    const returnSeries = decisionBoard.return_series || {};
    const items = (Array.isArray(returnSeries.items) ? returnSeries.items : [])
        .map((item) => ({
            date: safeText(item.date),
            paper_return_pct: numericValue(item.paper_return_pct),
            benchmark_return_pct: numericValue(item.benchmark_return_pct),
        }))
        .filter((item) => item.date !== "数据缺失" && item.paper_return_pct !== null && item.benchmark_return_pct !== null);
    if (items.length < 2) {
        showHomeChart("homeEquityChart", false, "后端对齐净值序列不足（需要至少 2 个共同交易日）。");
        return;
    }
    const paperDates = items.map((item) => item.date);
    showHomeChart("homeEquityChart", true);
    const chart = ensureHomeChart("homeEquityChart");
    if (!chart) return;
    const series = [{
        name: "Paper 累计收益",
        type: "line",
        data: items.map((item) => item.paper_return_pct),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: "#1D4ED8" },
        itemStyle: { color: "#1D4ED8" },
        areaStyle: { color: "rgba(29, 78, 216, 0.08)" },
        connectNulls: false,
    }, {
        name: `${safeText(returnSeries.benchmark_label || "沪深300")}（后端对齐）`,
        type: "line",
        data: items.map((item) => item.benchmark_return_pct),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.6, color: "#94a3b8", type: "dashed" },
        itemStyle: { color: "#94a3b8" },
        connectNulls: false,
    }];
    chart.setOption({
        grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
        legend: {
            top: 0,
            left: 0,
            textStyle: { color: HOME_CHART_MUTED, fontSize: 11 },
            itemWidth: 14,
        },
        xAxis: {
            type: "category",
            data: paperDates,
            axisLabel: { color: HOME_CHART_MUTED, fontSize: 10 },
            axisTick: { show: false },
            axisLine: { lineStyle: { color: HOME_CHART_GRID } },
        },
        yAxis: homeValueAxis("%"),
        tooltip: { trigger: "axis", confine: true, valueFormatter: (value) => (value === null || value === undefined ? "—" : `${value}%`) },
        series,
    }, true);
    chart.resize();
}
