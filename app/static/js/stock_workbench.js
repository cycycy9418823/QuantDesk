"use strict";

// Selected-stock display only: no trade, account or prediction submission.
(function () {
    const text = (value) => String(value ?? "")
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    const number = (value) => {
        if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
        const result = Number(value);
        return Number.isFinite(result) ? result : null;
    };
    const price = (value) => {
        const result = number(value);
        return result !== null && result > 0 ? result.toFixed(2) + " 元" : "—";
    };
    const metric = (label, value) => '<div class="sq-metric"><span>' + text(label)
        + '</span><strong>' + text(value) + '</strong></div>';
    const amount = (value, unit) => {
        const result = number(value);
        return result === null ? "—" : result.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + (unit || "（单位未记录）");
    };
    function detailErrorMessage(value) {
        try {
            const data = JSON.parse(String(value));
            if (data.error === "本地数据不足" && String(data.message || "").includes("历史日线不足20条")) {
                return "本地日线不足20条，暂不显示日K详情；当前报价和算法参考状态仍可单独查看。";
            }
        } catch { /* A technical error is not proof that history is missing. */ }
        return "日K详情暂时读取失败；当前报价和算法参考状态仍可单独查看。";
    }
    function historyQualityHtml(ref) {
        const quality = ref.history_quality;
        if (!quality || typeof quality !== "object") return '<p class="sq-note">具体日线检查窗口未记录，不能据此认定全历史或最近窗口正常。</p>';
        const count = (value) => {
            const result = number(value);
            return Number.isSafeInteger(result) && result >= 0 ? String(result) : "未记录";
        };
        const fact = (value) => typeof value === "string" && value.trim() ? value : "未记录";
        const fullInvalid = number(quality.ohlc_invalid_count);
        const windowInvalid = number(quality.current_window_ohlc_invalid_count);
        const guard = quality.full_history_guard_retained === true ? "全历史守卫保留"
            : quality.full_history_guard_retained === false ? "全历史守卫未保留，需核查" : "全历史守卫状态未核实";
        const labels = {
            daily_history_missing: "本地日线缺失", daily_price_basis_invalid: "日线价格口径未通过检查",
            daily_columns_missing: "日线日期或价格列缺失", daily_date_invalid: "日线日期无效",
            no_complete_daily_history: "没有已完成交易日的日线", daily_price_invalid: "历史价格缺失或无效",
            daily_ohlc_invalid: "历史高低开收关系异常", conflicting_daily_duplicates: "同日存在冲突记录",
            daily_history_insufficient: "完整历史条数不足", scenario_levels_invalid: "参考价位无效",
            daily_reference_stale: "参考日线已滞后", daily_reference_expired: "日线参考已过期",
            reference_levels_not_separated: "参考位没有形成有效间距", calendar_missing: "交易日历缺失",
            raw_daily_required: "参考价要求原始日线价格", complete_trade_date_unverified: "完整交易日尚未核实",
            next_trade_date_unverified: "下个交易日尚未核实", incomplete_or_future_daily_rows_excluded: "已排除未完成或未来日线",
        };
        const codes = Array.isArray(ref.reason_codes) ? ref.reason_codes : [];
        const reasons = [...new Set(codes.map((code) => Object.hasOwn(labels, code) ? labels[code] : "存在尚未解释的检查原因"))];
        return '<details class="sq-history-quality"' + (ref.available === false ? ' open' : '') + '><summary>日线检查范围与结果</summary>'
            + '<p>全历史 ' + text(count(quality.full_history_rows)) + ' 条；高低开收异常 ' + text(count(quality.ohlc_invalid_count))
            + ' 条；首个异常日 ' + text(fact(quality.first_invalid_date)) + '。</p>'
            + '<p>本次参考窗口自 ' + text(fact(quality.current_window_start)) + ' 起，共 ' + text(count(quality.current_window_rows))
            + ' 条；窗口高低开收异常 ' + text(count(quality.current_window_ohlc_invalid_count)) + ' 条。</p>'
            + '<p>' + text(guard) + '；最近窗口正常不能绕过全历史失败检查。</p>'
            + (fullInvalid > 0 && windowInvalid === 0 ? '<p class="sq-warning">最近窗口未发现此类异常，全历史仍有异常；算法参考继续受原守卫限制。</p>' : '')
            + (quality.message ? '<p>' + text(fact(quality.message)) + '</p>' : '')
            + (reasons.length ? '<p class="sq-warning">检查原因：' + reasons.map(text).join('；') + '。</p>' : '')
            + '</details>';
    }
    function render(state, options = {}) {
        const q = state.quote || {}, e = state.quote_evidence || {}, ref = state.reference || {};
        const error = options.error || "";
        const changed = number(q.pct_chg);
        const rawClock = (() => {
            const stamp = String(e.quote_timestamp || "");
            if (stamp) {
                const shown = stamp.replace("T", " ").slice(0, 19);
                return e.quote_timestamp_inferred ? shown + "（日期按采集记录推定）" : shown;
            }
            return q.quote_time ? "日期未核实 · " + q.quote_time : "日期和时间未记录";
        })();
        const quoteLabel = error ? "更新失败 · 上次结果" : e.label || "报价待核验";
        const warning = error || !e.usable_latest_quote;
        const levels = Array.isArray(ref.levels) ? ref.levels : [];
        const reason = (Array.isArray(ref.reasons) ? ref.reasons : []).filter(Boolean).map(text).join(" ");
        const disabled = state.refresh_allowed === false || (window.quantdeskRuntimeCapabilities !== undefined && window.quantdeskRuntimeCapabilities?.selected_quote_refresh !== true);
        const controlText = disabled ? "预览只读，实时报价待启用"
            : options.busy ? "正在获取报价…" : options.automatic ? "暂停自动报价" : "开启自动报价";
        const metrics = [
            metric(e.usable_latest_quote ? "报价" : "缓存报价", e.price_valid ? price(q.price) : "—"),
            metric("涨跌幅", changed === null ? "—" : (changed > 0 ? "+" : "") + changed.toFixed(2) + "%"),
            metric("当日高 / 低", price(q.high) + " / " + price(q.low)),
            metric("成交量", amount(q.volume, q.volume_unit)),
            metric("成交额", amount(q.amount, q.amount_unit)),
        ].join("");
        const cards = ref.available ? levels.map((level) =>
            '<article class="sq-level sq-level-' + text(level.kind) + '"><h4>' + text(level.label)
            + '</h4><strong>' + price(level.price) + '</strong><p>' + text(level.condition)
            + '</p><small>' + text(level.basis) + '</small></article>').join("")
            : '<p class="sq-warning">' + text(ref.message || "日线资料不足，报价仍可单独查看。") + '</p>';
        const economics = ref.trade_economics || {};
        const money = (value) => number(value) === null ? "未核实" : number(value).toFixed(2) + " 元";
        const economicsHtml = ref.available ? '<section class="sq-economics"><strong>资金与观察空间</strong><p>'
            + '最小研究手数 ' + text(economics.minimum_quantity ?? "未核实") + ' 股；含估计买入费用需 '
            + text(money(economics.minimum_lot_cash_with_estimated_buy_cost)) + '；可用现金 ' + text(money(economics.cash_available))
            + '（账本日 ' + text(economics.account_valuation_date || "未核实") + '）。</p><p>'
            + '从买入参考至上方观察位的毛空间 ' + text(money(economics.gross_observation_space))
            + '；至退出线的情景风险 ' + text(money(economics.loss_to_exit_line))
            + '；估计往返成本 ' + text(money(economics.estimated_roundtrip_cost_at_observation)) + '。</p>'
            + (economics.observation_space_below_cost === true ? '<p class="sq-warning">观察空间不足以覆盖估计交易成本；这里用于观察，不表示值得交易。</p>' : '')
            + (economics.minimum_lot_affordable === false ? '<p class="sq-warning">当前纸面账户现金不足最小手数所需资金。</p>' : '')
            + '<p>' + text(economics.settlement_note || "需遵守T+1及真实交易约束；缺少资料时不推定可成交。") + '</p><small>'
            + text(economics.space_note || "价位间距不是预期收益，风险金额也不保证能按退出线成交。") + '</small></section>' : '';
        const checks = Array.isArray(ref.event_checks) ? ref.event_checks : [];
        const eventsHtml = '<details class="sq-event-checks"><summary>重要事件检查状态</summary>'
            + (checks.length ? checks.map((item) => '<p>' + text(item.label) + '：'
                + text(item.status === "not_checked" ? "未检查" : item.status || "未核实")
                + ' · 检查时间 ' + text(item.checked_at || "未记录") + ' · 来源 ' + text(item.source || "未接入/未记录")
                + '。' + text(item.message || "") + '</p>').join('')
                : '<p>业绩、重大公告、停复牌、风险警示和分红除权覆盖均未核实；未检索到不等于没有风险。</p>')
            + '</details>';
        return '<div class="sq-heading"><h3>当前行情与算法买卖参考</h3><button type="button" class="sq-personal-add" data-personal-add="'
            + text(state.symbol || q.symbol || ref.symbol || "") + '">加入本机自选</button><span class="sq-badge">'
            + text(ref.label || "纸面 · 未验证") + '</span></div>'
            + '<div class="sq-quote-meta ' + (warning ? "sq-warning" : "") + '"><strong>' + text(quoteLabel)
            + '</strong><span' + (e.quote_timestamp ? ' title="' + text(e.quote_timestamp) + '"' : '')
            + '>报价时间 ' + text(rawClock) + '</span><span>来源 ' + text(e.source || q.source || "未记录")
            + '</span><span>采集时间 ' + text(e.collection_timestamp || q.updated_at || "未记录") + '</span></div>'
            + '<div class="sq-metrics">' + metrics + '</div>'
            + (error ? '<p class="sq-warning">' + text(error) + '</p>' : "")
            + (!e.comparable_to_raw_daily ? '<p class="sq-warning">报价时间、来源或价格口径尚未通过核验，暂不判定实时价格是否触发参考位。</p>' : "")
            + (state.calendar_message ? '<p class="sq-warning">' + text(state.calendar_message) + '</p>' : "")
            + '<div class="sq-controls"><button type="button" data-stock-live ' + (disabled || options.busy ? "disabled" : "")
            + '>' + text(controlText) + '</button><span>'
            + (options.automatic ? state.market_active ? "仅更新当前股票，约15秒一次。" : "当前休市，不自动请求报价源。"
                : "自动更新未开启；显示保存的报价及其原时间。")
            + '</span></div><p class="sq-assessment">' + text(ref.current_assessment || "观望：等待本地资料。") + '</p>'
            + '<div class="sq-levels">' + cards + '</div>'
            + economicsHtml
            + '<p class="sq-note">规则 ' + text(ref.research_identity?.rule_id || ref.rule_version || "未记录")
            + ' · 参考价未验证；历史波动区间不是预测置信区间。触价后仍需量能、收盘和有效期确认。</p>'
            + eventsHtml
            + historyQualityHtml(ref)
            + (Array.isArray(ref.warnings) ? ref.warnings.map((value) => '<p class="sq-warning">' + text(value) + '</p>').join("") : "")
            + (reason ? '<p class="sq-reasons"><b>为什么：</b>' + reason + '</p>' : "")
            + '<p class="sq-validity">日线计算截至 ' + text(ref.as_of || "未核实") + ' · '
            + text(ref.mode || "日线参考，盘中报价辅助") + ' · ' + text(ref.validity_text || "有效期未核实")
            + '</p><p class="sq-note">参考来自现有本地规则，价格触及不等于信号完成；不是已验证的盈利结论。</p>';
    }

    function createController(options) {
        let symbol = "", sequence = 0, timer = null, state = null;
        let automatic = null, busy = false, error = "", readAbort = null;
        const timeout = options.setTimeout || window.setTimeout.bind(window);
        const clear = options.clearTimeout || window.clearTimeout.bind(window);
        const node = () => {
            const root = options.getNode();
            return root && root.dataset.stockSymbol === symbol ? root : null;
        };
        function cancelTimer() {
            if (timer !== null) clear(timer);
            timer = null;
        }
        function paint() {
            const root = node();
            if (!root || !state) return;
            root.innerHTML = render(state, { automatic, busy, error });
            const button = root.querySelector("[data-stock-live]");
            if (button) button.onclick = async () => {
                automatic = !automatic; cancelTimer(); paint();
                if (automatic) await refresh(sequence);
            };
        }
        function schedule(token) {
            cancelTimer();
            if (!automatic || !symbol || !node() || options.isHidden?.()) return;
            timer = timeout(() => { timer = null; read(token); }, state?.market_active ? 15000 : 60000);
        }
        function readableError(value) {
            try {
                const data = JSON.parse(String(value));
                return String(data.message_for_ui || data.message || "更新暂不可用，保留上次结果。");
            } catch {
                return "更新暂不可用，保留上次结果与报价时间。";
            }
        }
        async function read(token) {
            try {
                if (typeof AbortController === "function") readAbort = new AbortController();
                const data = await options.get("/api/stock/" + encodeURIComponent(symbol) + "/workbench", { signal: readAbort?.signal });
                if (token !== sequence || !symbol) return;
                state = data; error = "";
                if (automatic === null) automatic = data.network_enabled === true;
                paint();
                options.onState?.(state);
                if (automatic && state.market_active && state.refresh_allowed !== false && !options.isHidden?.()) {
                    await refresh(token); return;
                }
            } catch (err) {
                if (token !== sequence) return;
                error = readableError(err?.message);
                const root = node();
                if (root && !state) root.innerHTML = '<p class="sq-warning">' + text(error) + '</p>';
                else paint();
            }
            schedule(token);
        }
        async function refresh(token) {
            if (token !== sequence || busy || !symbol || state?.refresh_allowed === false || options.canRefresh?.() === false || options.isHidden?.()) return;
            busy = true; paint();
            try {
                const result = await options.post("/api/stock/" + encodeURIComponent(symbol) + "/quote-refresh", { network: true });
                if (token !== sequence) return;
                if (result.view) state = result.view;
                error = result.ok === false ? String(result.message_for_ui || "报价源暂不可用，保留已有报价。") : "";
                options.onState?.(state);
            } catch (err) {
                if (token !== sequence) return;
                error = readableError(err?.message);
            } finally {
                if (token === sequence) { busy = false; paint(); schedule(token); }
            }
        }
        return {
            start(value) {
                this.stop(); symbol = String(value || "");
                if (!/^[0-9]{6}$/.test(symbol)) { symbol = ""; return Promise.resolve(); }
                return read(sequence);
            },
            stop() { sequence += 1; cancelTimer(); readAbort?.abort(); readAbort = null; symbol = ""; state = null; busy = false; error = ""; },
            remount: paint,
            suspend: cancelTimer,
            resume() { cancelTimer(); if (automatic && symbol) return read(sequence); },
            value: () => ({ symbol, state, automatic, busy, error }),
        };
    }
    window.QuantDeskStockWorkbench = { render, createController, detailErrorMessage };
})();
