/* Read-only observations; never turn incomplete equity history into a backtest. */
(function (global) {
    "use strict";
    const escape = (value) => String(value ?? "未记录").replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const numeric = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
    const amount = (v) => numeric(v) ? Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "未记录";
    const pct = (v) => numeric(v) ? `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(2)}%` : "不可计算";
    const reasons = {
        insufficient_dated_observations: "明确估值日期的记录不足两条",
        history_conflicting_or_invalid: "历史记录有冲突或金额异常",
        endpoint_missing: "指数缺少相同起止日", endpoint_conflict: "指数日期有冲突",
    };

    function render(data) {
        if (!data || data.ok === false) return `<h3>策略账与指数：已有记录对照</h3><p>${escape(data?.message_for_ui || "记录暂不可读，不能据此认定没有亏损。")}</p>`;
        const r = data.report || {}, book = r.portfolio || {}, points = r.points || [];
        const first = points[0], last = points[points.length - 1];
        const valid = points.length >= 2 && !r.invalid_count && !(r.conflicting_dates || []).length;
        const change = valid && first.equity > 0 ? (last.equity / first.equity - 1) * 100 : null;
        const summary = valid
            ? `${escape(first.date)} 至 ${escape(last.date)}，已记录权益从 ${amount(first.equity)} 元变为 ${amount(last.equity)} 元，账面变化 ${pct(change)}。`
            : "合格的同日期记录不足或存在冲突，区间变化暂不可计算。";
        const rows = Object.values(r.comparisons || {}).map((c) => `<tr><td>${escape(c.label)}</td>`
            + (c.available ? `<td>${escape(c.start)} 至 ${escape(c.end)}</td><td>${pct(c.recorded_equity_change_pct)}</td><td>${pct(c.index_change_pct)}</td>`
                : `<td colspan="3">不可比较：${escape(reasons[c.reason] || "指数记录不可用")}</td>`) + "</tr>").join("");
        const gaps = r.calendar?.status === "available"
            ? `缺少的交易日记录：${escape((r.calendar.missing_dates || []).join("、") || "无")}。`
            : "交易日历不完整，缺少多少交易日尚不能判断。";
        const difference = numeric(book.identity_difference) ? `${Number(book.identity_difference) >= 0 ? "+" : ""}${amount(book.identity_difference)} 元` : "未记录";
        return `<h3>策略账与指数：已有记录对照 <span class="badge">纸面 · 未验证</span></h3>`
            + `<p>${summary}</p><p>最新账本估值日 ${escape(book.valuation_date || "未记录")}，权益 ${amount(book.total_equity)} 元；对账差额 ${difference}。`
            + "这是保存过的账面数值，尚不能作为完整对账后的投资收益率。</p>"
            + `<div class="paper-table-wrap"><table class="compact-table"><thead><tr><th>指数</th><th>共同起止日</th><th>账面变化</th><th>指数点位变化</th></tr></thead><tbody>${rows}</tbody></table></div>`
            + `<p class="section-note">${escape(r.undated_count ?? 0)} 条历史记录缺少明确估值日期；${gaps}`
            + ` 复权因子明确过期 ${escape(book.stale_adjustment_factors ?? "未知")} 个。未用生成日期补估值日期，未插值补净值。</p>`
            + '<details><summary>如何读这张表、接下来核对什么</summary><p>所有对照使用同一起止日。指数点位未包含分红再投资；入出金、费用、公司行动和完整成交流水仍需核对。'
            + '不能凭这段变化认定策略长期有效。先看亏损发生在哪些规则与持仓，再对比改法；改法须给出样本外日期、回撤、成本和失效条件，才能决定是否替换。</p>'
            + `<p>历史原件 SHA-256：${escape(r.history_sha256)}<br>账本原件 SHA-256：${escape(r.portfolio_sha256)}</p></details>`
            + '<button class="action-button" type="button" data-observed-refresh>刷新已有记录对照</button>';
    }

    let generation = 0;
    async function load(requestJson, getNode = () => document.getElementById("paperObservedPanel")) {
        const panel = getNode();
        if (!panel) return;
        const current = ++generation;
        try {
            const data = await requestJson("/api/paper/observed-performance");
            if (current !== generation) return;
            panel.innerHTML = render(data);
            const button = panel.querySelector("[data-observed-refresh]");
            if (button) button.onclick = () => load(requestJson, getNode);
        } catch (_) {
            if (current === generation) panel.innerHTML = render({ ok: false, message_for_ui: "已有记录对照读取失败，请重新打开策略成绩页。" });
        }
    }
    global.QuantDeskPaperObserved = { render, load };
})(window);
