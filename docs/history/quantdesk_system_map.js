const pptxgen = require("pptxgenjs");
const pres = new pptxgen();

pres.layout = "LAYOUT_WIDE";
pres.title = "QuantDesk 全图梳理";
pres.author = "QuantDesk";
pres.subject = "每个板块用什么数据、有什么作用、能做到什么 — 2026-09-04";

const C = {
  cream: "F4EFE6",
  creamAlt: "EBE5D8",
  ink: "14110D",
  inkSoft: "3A342B",
  muted: "6B6357",
  dark: "0A0907",
  orange: "C46E00",
  red: "B31C1C",
  green: "2E6B3C",
  white: "FFFFFF",
  card: "FAF7F1",
};
const F = { face: "Microsoft YaHei" };
const W = 13.33;
const H = 7.5;
const TOTAL = "16";

function addFrame(slide, opts) {
  const bg = opts.bg || C.cream;
  const muted = opts.muted || C.muted;
  slide.background = { color: bg };
  slide.addShape(pres.shapes.LINE, {
    x: 0.5, y: 0.56, w: W - 1.0, h: 0,
    line: { color: muted, width: 0.5 },
  });
  slide.addText(opts.section || "", {
    x: 0.5, y: 0.28, w: 7.2, h: 0.28,
    fontFace: F.face, fontSize: 11, color: muted, charSpacing: 2, margin: 0, valign: "middle",
  });
  slide.addText("QUANTDESK  ·  研究 / 纸面", {
    x: W - 6.3, y: 0.28, w: 5.8, h: 0.28,
    fontFace: F.face, fontSize: 11, color: muted, charSpacing: 2, margin: 0, valign: "middle", align: "right",
  });
  slide.addShape(pres.shapes.LINE, {
    x: 0.5, y: H - 0.52, w: W - 1.0, h: 0,
    line: { color: muted, width: 0.5 },
  });
  slide.addText("No. " + opts.pageNum + " / " + TOTAL, {
    x: 0.5, y: H - 0.42, w: 3.5, h: 0.26,
    fontFace: F.face, fontSize: 10, color: muted, margin: 0, valign: "middle",
  });
  slide.addText(opts.tag || "", {
    x: W - 6.3, y: H - 0.42, w: 5.8, h: 0.26,
    fontFace: F.face, fontSize: 10, color: muted, margin: 0, valign: "middle", align: "right",
  });
}

function card(slide, x, y, w, h, fill) {
  slide.addShape(pres.shapes.RECTANGLE, {
    x, y, w, h,
    fill: { color: fill || C.card },
    line: { color: C.creamAlt, width: 1 },
  });
}

function accentBar(slide, x, y, h, color) {
  slide.addShape(pres.shapes.RECTANGLE, {
    x, y, w: 0.08, h,
    fill: { color: color || C.orange },
    line: { color: color || C.orange, width: 0 },
  });
}

// ============================================================
// 01 COVER
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "BRIEFING  No. 01", pageNum: "01", tag: "COVER" });
  s.addText("2026-09-04  ·  按代码接线，不按愿望", {
    x: 0.5, y: 1.15, w: 12, h: 0.32,
    fontFace: F.face, fontSize: 13, color: C.muted, charSpacing: 2, margin: 0,
  });
  s.addText("QuantDesk 全图。", {
    x: 0.5, y: 1.7, w: 12.2, h: 0.85,
    fontFace: F.face, fontSize: 44, color: C.ink, margin: 0,
  });
  s.addText("每一块用什么数据、干什么、能做到哪。", {
    x: 0.5, y: 2.55, w: 12.2, h: 0.7,
    fontFace: F.face, fontSize: 28, color: C.orange, margin: 0,
  });
  s.addText("本地 A 股研究台 + 纸面模拟。不接券商，不下真实单，不承诺收益。主界面端口 8000。", {
    x: 0.5, y: 3.5, w: 11.5, h: 0.7,
    fontFace: F.face, fontSize: 16, color: C.inkSoft, margin: 0,
  });
  const pills = [
    { t: "看市场", d: "全 A 日线 + 快照" },
    { t: "做研究", d: "仍锁在 914 低价池" },
    { t: "做纸面", d: "两套账、两种成交" },
    { t: "做决策", d: "AI 每晚 20:20" },
  ];
  pills.forEach((p, i) => {
    const x = 0.5 + i * 3.15;
    card(s, x, 4.55, 3.0, 1.55);
    accentBar(s, x, 4.55, 1.55);
    s.addText(p.t, {
      x: x + 0.22, y: 4.68, w: 2.65, h: 0.4,
      fontFace: F.face, fontSize: 16, color: C.ink, bold: true, margin: 0,
    });
    s.addText(p.d, {
      x: x + 0.22, y: 5.12, w: 2.65, h: 0.75,
      fontFace: F.face, fontSize: 14, color: C.inkSoft, margin: 0,
    });
  });
}

// ============================================================
// 02 BOUNDARY
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 01  ·  边界", pageNum: "02", tag: "BOUNDARY" });
  s.addText("先钉死：这是什么，不是什么", {
    x: 0.5, y: 0.75, w: 12, h: 0.5,
    fontFace: F.face, fontSize: 26, color: C.ink, margin: 0,
  });
  card(s, 0.5, 1.45, 6.0, 4.85);
  accentBar(s, 0.5, 1.45, 4.85, C.green);
  s.addText("现在能当工具用的", {
    x: 0.75, y: 1.62, w: 5.5, h: 0.38,
    fontFace: F.face, fontSize: 16, color: C.green, bold: true, margin: 0,
  });
  [
    "本机收盘后更新全 A 日线、估值、新闻池",
    "8000 上看盘、看候选、看纸面账户",
    "AI 每晚自己下纸面计划，次日开盘成交",
    "持仓、自选、计划、异动的 1 分钟（设计已有）",
    "研究输出带「未验证」标签，不当实盘指令",
  ].forEach((line, i) => {
    s.addText(line, {
      x: 0.75, y: 2.15 + i * 0.72, w: 5.5, h: 0.65,
      fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0, valign: "middle",
    });
  });
  card(s, 6.8, 1.45, 6.0, 4.85);
  accentBar(s, 6.8, 1.45, 4.85, C.red);
  s.addText("明确没有、也不该有的", {
    x: 7.05, y: 1.62, w: 5.5, h: 0.38,
    fontFace: F.face, fontSize: 16, color: C.red, bold: true, margin: 0,
  });
  [
    "券商、QMT、真实下单、自动实盘",
    "已验证的选股 Alpha（裁决：未验证）",
    "全 A 正式策略研究（范围仍是低价池）",
    "默认联网、默认大模型（要显式打开）",
    "用分钟假装看见了没抓到的成交",
  ].forEach((line, i) => {
    s.addText(line, {
      x: 7.05, y: 2.15 + i * 0.72, w: 5.5, h: 0.65,
      fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0, valign: "middle",
    });
  });
}

// ============================================================
// 03 TWO LOOPS
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 02  ·  两套闭环", pageNum: "03", tag: "LOOPS" });
  s.addText("乱，是因为脑子里有一套闭环，代码里跑的是另一套", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.45,
    fontFace: F.face, fontSize: 22, color: C.ink, margin: 0,
  });
  const loops = [
    { h: "你要的闭环", c: C.orange, items: ["看全市场", "按风格选策略", "找出票", "做计划", "纸面成交", "复盘学习"] },
    { h: "代码现在的闭环", c: C.ink, items: ["全 A 日线先落盘", "研究仍扫 914 低价池", "MA20 / 打板出候选", "914 全员 1 分钟过门", "AI 从持仓+涨跌幅里挑", "次日开盘价纸面成交"] },
  ];
  loops.forEach((loop, i) => {
    const x = 0.5 + i * 6.4;
    card(s, x, 1.4, 6.15, 5.15);
    s.addShape(pres.shapes.RECTANGLE, {
      x, y: 1.4, w: 6.15, h: 0.55,
      fill: { color: loop.c }, line: { color: loop.c, width: 0 },
    });
    s.addText(loop.h, {
      x: x + 0.2, y: 1.48, w: 5.75, h: 0.4,
      fontFace: F.face, fontSize: 16, color: C.white, bold: true, margin: 0,
    });
    loop.items.forEach((t, j) => {
      const y = 2.15 + j * 0.68;
      s.addShape(pres.shapes.OVAL, {
        x: x + 0.28, y: y + 0.08, w: 0.32, h: 0.32,
        fill: { color: loop.c }, line: { color: loop.c, width: 0 },
      });
      s.addText(String(j + 1), {
        x: x + 0.28, y: y + 0.08, w: 0.32, h: 0.32,
        fontFace: F.face, fontSize: 11, color: C.white, align: "center", valign: "middle", margin: 0,
      });
      s.addText(t, {
        x: x + 0.75, y: y, w: 5.1, h: 0.48,
        fontFace: F.face, fontSize: 16, color: C.ink, margin: 0, valign: "middle",
      });
    });
  });
}

// ============================================================
// 04 THREE DATA LAYERS
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 03  ·  数据三层", pageNum: "04", tag: "DATA LAYERS" });
  s.addText("先分清三层数据，后面所有板块都挂在这上面", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 22, color: C.ink, margin: 0,
  });
  const layers = [
    { n: "L1", t: "市场数据", d: "全 A 日线 · 约 5555 只", b: "收盘价、涨跌、估值、指数、新闻池", u: "看今天行情、给 AI 标价、给回测当原料", ok: "今天已经在写", color: C.green },
    { n: "L2", t: "研究宇宙", d: "低价学习池 · 914 只", b: "7 月 17 日冻住的 included 名单", u: "8000 候选、特征、研究链完成门", ok: "你已不用低价，代码还锁着", color: C.orange },
    { n: "L3", t: "关注分钟", d: "持仓 / 计划 / 自选 / 异动", b: "1 分钟 K + 全市场快照覆盖写", u: "盘中看见、打板/条件单纸面成交", ok: "设计有，5 分钟任务未装", color: C.ink },
  ];
  layers.forEach((L, i) => {
    const x = 0.5 + i * 4.2;
    card(s, x, 1.35, 4.0, 5.15);
    s.addShape(pres.shapes.RECTANGLE, {
      x, y: 1.35, w: 4.0, h: 0.12,
      fill: { color: L.color }, line: { color: L.color, width: 0 },
    });
    s.addText(L.n, {
      x: x + 0.22, y: 1.6, w: 3.55, h: 0.3,
      fontFace: F.face, fontSize: 12, color: L.color, charSpacing: 2, margin: 0,
    });
    s.addText(L.t, {
      x: x + 0.22, y: 1.95, w: 3.55, h: 0.4,
      fontFace: F.face, fontSize: 22, color: C.ink, bold: true, margin: 0,
    });
    s.addText(L.d, {
      x: x + 0.22, y: 2.4, w: 3.55, h: 0.4,
      fontFace: F.face, fontSize: 14, color: C.orange, margin: 0,
    });
    s.addText("里面是什么", {
      x: x + 0.22, y: 2.95, w: 3.55, h: 0.28,
      fontFace: F.face, fontSize: 11, color: C.muted, margin: 0,
    });
    s.addText(L.b, {
      x: x + 0.22, y: 3.25, w: 3.55, h: 0.85,
      fontFace: F.face, fontSize: 14, color: C.inkSoft, margin: 0,
    });
    s.addText("拿来干什么", {
      x: x + 0.22, y: 4.15, w: 3.55, h: 0.28,
      fontFace: F.face, fontSize: 11, color: C.muted, margin: 0,
    });
    s.addText(L.u, {
      x: x + 0.22, y: 4.45, w: 3.55, h: 0.85,
      fontFace: F.face, fontSize: 14, color: C.inkSoft, margin: 0,
    });
    s.addText(L.ok, {
      x: x + 0.22, y: 5.5, w: 3.55, h: 0.7,
      fontFace: F.face, fontSize: 13, color: L.color, bold: true, margin: 0,
    });
  });
}

// ============================================================
// 05 DAILY BARS
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 04  ·  日线", pageNum: "05", tag: "DAILY BARS" });
  s.addText("日线：今天行情的唯一正式底座", {
    x: 0.5, y: 0.75, w: 12, h: 0.45,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const facts = [
    { k: "写在哪", v: "data/daily/{代码}.csv" },
    { k: "谁来写", v: "盘后流水线第 2 段 market_refresh" },
    { k: "覆盖", v: "全 A 约 5555 只；停牌核销另记" },
    { k: "来源", v: "新浪收盘快照 sina_spot_close" },
    { k: "附带", v: "估值快照、指数、新闻池 38 只" },
    { k: "时间", v: "计划 15:40，失败则 18:30 续跑" },
  ];
  facts.forEach((f, i) => {
    const col = i % 3;
    const row = Math.floor(i / 3);
    const x = 0.5 + col * 4.2;
    const y = 1.4 + row * 1.55;
    card(s, x, y, 4.0, 1.4);
    s.addText(f.k, {
      x: x + 0.2, y: y + 0.18, w: 3.6, h: 0.3,
      fontFace: F.face, fontSize: 12, color: C.muted, margin: 0,
    });
    s.addText(f.v, {
      x: x + 0.2, y: y + 0.52, w: 3.6, h: 0.7,
      fontFace: F.face, fontSize: 16, color: C.ink, margin: 0,
    });
  });
  s.addText("能做到：全市场收盘价、涨跌、估值对齐。做不到：代替选股宇宙、代替盘中成交证据。", {
    x: 0.5, y: 4.7, w: 12.3, h: 0.7,
    fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0,
  });
  card(s, 0.5, 5.45, 12.3, 0.95);
  s.addText("2026-09-04：5547 只补了今天，8 只停牌核销。指数 15:40 时尚无今天，18:30 已补上沪深 300。", {
    x: 0.7, y: 5.6, w: 11.9, h: 0.65,
    fontFace: F.face, fontSize: 14, color: C.ink, margin: 0, valign: "middle",
  });
}

// ============================================================
// 06 MINUTES + SNAPSHOT
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 05  ·  分钟与快照", pageNum: "06", tag: "MINUTES" });
  s.addText("分钟不是第三份日线。它只证明「这一分钟发生过」", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 22, color: C.ink, margin: 0,
  });
  card(s, 0.5, 1.35, 6.15, 5.15);
  s.addText("A. 盘中眼睛（你后来定的）", {
    x: 0.72, y: 1.5, w: 5.7, h: 0.4,
    fontFace: F.face, fontSize: 16, color: C.orange, bold: true, margin: 0,
  });
  s.addText("全市场快照覆盖写 spot_snapshot_latest.csv（约 5555 行，不存历史）。1 分钟只给持仓、当晚计划、自选、研究候选，再加涨停/异动/成交额，上限约 100 只。\n\n能做到：盘中看见全场，不把 5500 只 1 分钟塞盘。现状：代码有，5 分钟任务还没装。", {
    x: 0.72, y: 2.15, w: 5.7, h: 3.9,
    fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0, valign: "top",
  });
  card(s, 6.85, 1.35, 6.0, 5.15);
  s.addText("B. 研究链完成门（仍在跑）", {
    x: 7.07, y: 1.5, w: 5.55, h: 0.4,
    fontFace: F.face, fontSize: 16, color: C.red, bold: true, margin: 0,
  });
  s.addText("低价池 914 只必须各有一段完整 raw 1 分钟，研究链才算 completed。差几只，候选和条件单整晚停。\n\n能做到：给可能被选中的低价票预留成交证据。做不到：这不是关注列表，也不是全市场。", {
    x: 7.07, y: 2.15, w: 5.55, h: 3.9,
    fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0, valign: "top",
  });
}

// ============================================================
// 07 PIPELINE
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 06  ·  盘后流水线", pageNum: "07", tag: "PIPELINE" });
  s.addText("唯一正式写手：Data Close Pipeline", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const stages = [
    { n: "1", t: "universe", d: "全 A 代码表", x: "约 5555 只名单" },
    { n: "2", t: "market_refresh", d: "日线 / 估值 / 新闻", x: "今天行情落盘" },
    { n: "3", t: "research_chain", d: "914 池 + 分钟门", x: "候选、特征、冻结" },
    { n: "4", t: "reconcile", d: "三方对账", x: "缺口可见" },
    { n: "5", t: "health_alert", d: "健康告警", x: "外推默认不开" },
  ];
  stages.forEach((st, i) => {
    const x = 0.5 + i * 2.52;
    card(s, x, 1.35, 2.38, 3.35);
    s.addShape(pres.shapes.OVAL, {
      x: x + 0.9, y: 1.55, w: 0.55, h: 0.55,
      fill: { color: C.orange }, line: { color: C.orange, width: 0 },
    });
    s.addText(st.n, {
      x: x + 0.9, y: 1.55, w: 0.55, h: 0.55,
      fontFace: F.face, fontSize: 16, color: C.white, align: "center", valign: "middle", margin: 0,
    });
    s.addText(st.t, {
      x: x + 0.12, y: 2.25, w: 2.14, h: 0.7,
      fontFace: F.face, fontSize: 13, color: C.ink, bold: true, align: "center", margin: 0,
    });
    s.addText(st.d, {
      x: x + 0.12, y: 2.95, w: 2.14, h: 0.7,
      fontFace: F.face, fontSize: 13, color: C.inkSoft, align: "center", margin: 0,
    });
    s.addText(st.x, {
      x: x + 0.12, y: 3.75, w: 2.14, h: 0.7,
      fontFace: F.face, fontSize: 13, color: C.orange, align: "center", margin: 0,
    });
  });
  s.addText("旧四条数据任务已停。早报 8:35、复盘 20:00、AI 20:20 只读这条流水线的产物。第 3 段不过，后面候选换不了。", {
    x: 0.5, y: 4.9, w: 12.3, h: 0.7,
    fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0,
  });
  card(s, 0.5, 5.65, 12.3, 0.85);
  s.addText("今天：15:40 日线完成，第 3 段因 7 只北交所分钟超时变 partial；18:30 续跑补上，研究链 completed。顶层 retry_pending 只因告警外推未开。", {
    x: 0.7, y: 5.78, w: 11.9, h: 0.6,
    fontFace: F.face, fontSize: 14, color: C.ink, margin: 0, valign: "middle",
  });
}

// ============================================================
// 08 RESEARCH DESK
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 07  ·  研究台 8000", pageNum: "08", tag: "RESEARCH DESK" });
  s.addText("8000 页面是观察台，不是第二套交易系统", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const desk = [
    { t: "读什么", v: "日线、板块缓存、新闻、候选 JSON、纸面账本快照、数据健康" },
    { t: "候选从哪来", v: "auto_strategy_sandbox 扫 914 低价池，写出 auto_long / auto_board" },
    { t: "长线规则", v: "收盘在 MA20 上方，偏离 0–20%，再加流动性分" },
    { t: "打板规则", v: "日线涨幅接近涨停。看不见封单、排队" },
    { t: "能做到", v: "看盘、看这几只推荐、点开详情、纸面账户展示" },
    { t: "做不到", v: "全市场选股、已验证 Alpha、真实下单、长期基本面研究" },
  ];
  desk.forEach((d, i) => {
    const y = 1.3 + i * 0.8;
    s.addShape(pres.shapes.RECTANGLE, {
      x: 0.5, y, w: 2.3, h: 0.68,
      fill: { color: C.ink }, line: { color: C.ink, width: 0 },
    });
    s.addText(d.t, {
      x: 0.6, y, w: 2.1, h: 0.68,
      fontFace: F.face, fontSize: 14, color: C.white, align: "center", valign: "middle", margin: 0,
    });
    card(s, 2.9, y, 9.9, 0.68);
    s.addText(d.v, {
      x: 3.1, y, w: 9.5, h: 0.68,
      fontFace: F.face, fontSize: 15, color: C.ink, valign: "middle", margin: 0,
    });
  });
}

// ============================================================
// 09 PICK vs FILL
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 08  ·  选股 vs 成交", pageNum: "09", tag: "PICK VS FILL" });
  s.addText("推荐不靠分钟。分钟管的是「能不能假装成交」", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.45,
    fontFace: F.face, fontSize: 22, color: C.ink, margin: 0,
  });
  card(s, 0.5, 1.4, 6.15, 4.55);
  s.addText("选谁 · 日线就够", {
    x: 0.75, y: 1.55, w: 5.7, h: 0.45,
    fontFace: F.face, fontSize: 20, color: C.green, bold: true, margin: 0,
  });
  s.addText("排序、MA20、涨跌幅、流动性、早报候选，全部吃日线。没有 1 分钟，照样可以列出「今天研究台看上谁」。对应：研究台推荐、AI 菜单、特征、回测默认 next_open。", {
    x: 0.75, y: 2.15, w: 5.7, h: 3.4,
    fontFace: F.face, fontSize: 16, color: C.inkSoft, margin: 0, valign: "top",
  });
  card(s, 6.85, 1.4, 6.0, 4.55);
  s.addText("成交 · 看你模拟哪一种", {
    x: 7.1, y: 1.55, w: 5.55, h: 0.45,
    fontFace: F.face, fontSize: 20, color: C.orange, bold: true, margin: 0,
  });
  s.addText("次日开盘买/卖：日线开盘价即可（AI 账本就是这样）。打板、盘中条件单、盘中止损：必须有该票的完整 1 分钟，否则记证据不足，不编成交。分钟给「今晚可能成交的票」，不是给 914 全员。", {
    x: 7.1, y: 2.15, w: 5.55, h: 3.4,
    fontFace: F.face, fontSize: 16, color: C.inkSoft, margin: 0, valign: "top",
  });
  s.addText("今天下午卡住的，是右边这扇门被焊到了左边的推荐刷新上。", {
    x: 0.5, y: 6.1, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 15, color: C.red, margin: 0,
  });
}

// ============================================================
// 10 TWO PAPER BOOKS
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 09  ·  两套纸面", pageNum: "10", tag: "PAPER BOOKS" });
  s.addText("纸面不是一本账。现在并排两本，不要混", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const books = [
    { t: "研究台纸面", p: "state/auto_paper_portfolio.json", d: "8000 页面上的纸面账户。低价沙盒、条件单冻结走这边。文档里仍称「唯一正式纸面账本」。" },
    { t: "AI 基金经理", p: "state/ai_trader/ledger.json", d: "每晚 20:20 自己决策。成交模型 next_open_only。你最近看的权益、8 只持仓，是这一本。" },
  ];
  books.forEach((b, i) => {
    const x = 0.5 + i * 6.4;
    card(s, x, 1.35, 6.15, 3.35);
    accentBar(s, x, 1.35, 3.35);
    s.addText(b.t, {
      x: x + 0.28, y: 1.5, w: 5.65, h: 0.45,
      fontFace: F.face, fontSize: 20, color: C.ink, bold: true, margin: 0,
    });
    s.addText(b.p, {
      x: x + 0.28, y: 2.0, w: 5.65, h: 0.4,
      fontFace: F.face, fontSize: 13, color: C.orange, margin: 0,
    });
    s.addText(b.d, {
      x: x + 0.28, y: 2.5, w: 5.65, h: 1.9,
      fontFace: F.face, fontSize: 16, color: C.inkSoft, margin: 0,
    });
  });
  card(s, 0.5, 4.9, 12.3, 1.6);
  s.addText("成交规则对照", {
    x: 0.7, y: 5.05, w: 12, h: 0.3,
    fontFace: F.face, fontSize: 13, color: C.muted, margin: 0,
  });
  s.addText("AI 账本：次日开盘价，一字涨停买不进。条件单内核：要 verified 1 分钟，用次日第一根分钟 open。回测默认也是 next_open。三套不要互相冒充。", {
    x: 0.7, y: 5.4, w: 11.9, h: 0.9,
    fontFace: F.face, fontSize: 15, color: C.ink, margin: 0,
  });
}

// ============================================================
// 11 AI TRADER
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 10  ·  AI 交易员", pageNum: "11", tag: "AI TRADER" });
  s.addText("20:20：唯一自动做完整决定的环节", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const ai = [
    { t: "输入菜单", v: "持仓 + 自选 + 8000 那几只推荐 + 全 A 日线里当天涨跌最大的，最多约 80 只" },
    { t: "它被允许的风格", v: "打板 / 短线 / 波段 / 中线 / 长期。没有人规定必须短线" },
    { t: "它自己怎么说", v: "9 月 2 日主张：长期 + 中线，少追涨。账上 8 只多数标中线 60 天或长期 250 天" },
    { t: "内部假说仍是短的", v: "reversal_5、low_vol_20、ma20_trend，检验窗口都是持有 5 日" },
    { t: "成交", v: "次日开盘价。盘中笔记只进 journal，不改账本" },
    { t: "能做到 / 不能", v: "能下纸面计划并次日成交。不能全市场选股，也不是已验证策略" },
  ];
  ai.forEach((row, i) => {
    const col = i % 2;
    const r = Math.floor(i / 2);
    const x = 0.5 + col * 6.4;
    const y = 1.3 + r * 1.7;
    card(s, x, y, 6.15, 1.55);
    s.addText(row.t, {
      x: x + 0.2, y: y + 0.12, w: 5.75, h: 0.32,
      fontFace: F.face, fontSize: 13, color: C.orange, margin: 0,
    });
    s.addText(row.v, {
      x: x + 0.2, y: y + 0.48, w: 5.75, h: 0.9,
      fontFace: F.face, fontSize: 14, color: C.ink, margin: 0,
    });
  });
}

// ============================================================
// 12 CONSUMERS
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 11  ·  读产物的人", pageNum: "12", tag: "CONSUMERS" });
  s.addText("早报、复盘、8000，都是读者，不是写手", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const cons = [
    { t: "早报 08:35", d: "读昨天流水线产物。流水线没完成就 warning / blocked。不推手机（除非显式打开）。" },
    { t: "复盘 20:00", d: "日终回顾。读同一套 latest。昨晚会因链不完整失败（LastResult 78）。" },
    { t: "8000 面板", d: "人眼看盘。数据来自 data/ 与 results/。不自己去拉全 A 日线。" },
    { t: "盘中笔记", d: "5 分钟观察只写 daytime_notes。20:20 才可能写进 journal。不改仓。" },
  ];
  cons.forEach((c, i) => {
    const col = i % 2;
    const r = Math.floor(i / 2);
    const x = 0.5 + col * 6.4;
    const y = 1.35 + r * 2.4;
    card(s, x, y, 6.15, 2.2);
    accentBar(s, x, y, 2.2);
    s.addText(c.t, {
      x: x + 0.28, y: y + 0.22, w: 5.65, h: 0.45,
      fontFace: F.face, fontSize: 18, color: C.ink, bold: true, margin: 0,
    });
    s.addText(c.d, {
      x: x + 0.28, y: y + 0.75, w: 5.65, h: 1.2,
      fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0,
    });
  });
}

// ============================================================
// 13 CLOCK
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 12  ·  一天的钟", pageNum: "13", tag: "CLOCK" });
  s.addText("交易日实际在几点动", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const clock = [
    { t: "08:35", a: "早报", s: "读，不写行情" },
    { t: "盘中", a: "快照 / 1 分钟", s: "设计有，任务未装" },
    { t: "15:40", a: "流水线", s: "唯一写手第一枪" },
    { t: "18:30", a: "流水线续跑", s: "补分钟、指数、过门" },
    { t: "20:00", a: "日复盘", s: "读 completed 包" },
    { t: "20:20", a: "AI 交易员", s: "当晚唯一完整决策" },
  ];
  s.addShape(pres.shapes.RECTANGLE, {
    x: 1.35, y: 1.64, w: 10.55, h: 0.07,
    fill: { color: C.creamAlt }, line: { color: C.creamAlt, width: 0 },
  });
  clock.forEach((c, i) => {
    const x = 0.5 + (i % 6) * 2.1;
    const y = 1.4;
    s.addShape(pres.shapes.OVAL, {
      x: x + 0.65, y: y, w: 0.55, h: 0.55,
      fill: { color: C.orange }, line: { color: C.cream, width: 2 },
    });
    s.addText(c.t, {
      x, y: y + 0.7, w: 2.0, h: 0.35,
      fontFace: F.face, fontSize: 14, color: C.orange, align: "center", margin: 0,
    });
    s.addText(c.a, {
      x, y: y + 1.05, w: 2.0, h: 0.4,
      fontFace: F.face, fontSize: 15, color: C.ink, align: "center", bold: true, margin: 0,
    });
    s.addText(c.s, {
      x, y: y + 1.45, w: 2.0, h: 0.7,
      fontFace: F.face, fontSize: 12, color: C.inkSoft, align: "center", margin: 0,
    });
  });
  card(s, 0.5, 4.15, 12.3, 2.3);
  s.addText("记住三句", {
    x: 0.75, y: 4.32, w: 11.8, h: 0.32,
    fontFace: F.face, fontSize: 13, color: C.muted, margin: 0,
  });
  s.addText("15:40 / 18:30 写数据。20:20 才改 AI 仓。盘中只许记笔记。周末没有这根钟。", {
    x: 0.75, y: 4.75, w: 11.8, h: 1.4,
    fontFace: F.face, fontSize: 20, color: C.ink, margin: 0,
  });
}

// ============================================================
// 14 MASTER TABLE
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 13  ·  总表", pageNum: "14", tag: "MAP" });
  s.addText("一张表看完：板块 · 数据 · 能做 · 不能做", {
    x: 0.5, y: 0.72, w: 12.3, h: 0.38,
    fontFace: F.face, fontSize: 22, color: C.ink, margin: 0,
  });
  const header = [
    { text: "板块", options: { fill: { color: C.ink }, color: C.white, bold: true, align: "center", valign: "middle" } },
    { text: "主要数据", options: { fill: { color: C.ink }, color: C.white, bold: true, align: "center", valign: "middle" } },
    { text: "能做到", options: { fill: { color: C.ink }, color: C.white, bold: true, align: "center", valign: "middle" } },
    { text: "做不到 / 现状", options: { fill: { color: C.ink }, color: C.white, bold: true, align: "center", valign: "middle" } },
  ];
  const rows = [
    ["全 A 日线", "data/daily 约 5555", "今天收盘价、估值", "不等于选股宇宙"],
    ["低价研究池", "914 只冻住名单", "出 8000 那几只推荐", "你已不用低价"],
    ["盘中快照", "覆盖写 5555 行", "看见全场报价", "5 分钟任务未装"],
    ["关注 1 分钟", "持仓/计划/自选/异动", "盘中纸面成交证据", "不是 914 全员"],
    ["研究链完成门", "914 完整 1 分钟", "放行候选与冻结", "差 1 只就停整晚"],
    ["8000 研究台", "日线 + 候选 JSON", "看盘、看推荐", "MA20/打板，非长期"],
    ["AI 交易员", "持仓+涨跌幅+推荐", "每晚纸面决策", "菜单偏短，假说 5 日"],
    ["研究台纸面", "auto_paper_portfolio", "条件单冻结", "与 AI 账本不是一本"],
    ["因子 / Alpha", "全 A 历史裁决", "披露「未验证」", "趋势 kill，反转不够成本"],
  ];
  const table = [header, ...rows.map((r) =>
    r.map((cell, j) => ({
      text: cell,
      options: {
        fill: { color: C.card },
        color: j === 0 ? C.ink : C.inkSoft,
        bold: j === 0,
        align: "left",
        valign: "middle",
      },
    }))
  )];
  s.addTable(table, {
    x: 0.45, y: 1.18, w: 12.4, h: 5.55,
    colW: [2.2, 3.3, 3.3, 3.6],
    border: { pt: 0.5, color: C.creamAlt },
    fontFace: F.face,
    fontSize: 12,
    valign: "middle",
  });
}

// ============================================================
// 15 TWISTS
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 14  ·  拧着的三处", pageNum: "15", tag: "GAPS" });
  s.addText("觉得乱，主要是这三处名实不符", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.4,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const gaps = [
    { n: "01", t: "宇宙", d: "你要看全市场、做波段/长期。代码研究范围仍是 only_low_price=True。914 文件 7 月冻住，里面还有浦发银行。" },
    { n: "02", t: "周期", d: "研究台「长线」是 MA20 过滤。AI 口头长期/中线，检验假说仍是持有 5 日。喂给它的菜单还有当日涨跌幅。" },
    { n: "03", t: "分钟门", d: "纸面盘中成交确实要分钟。但不该拿 914 全员完整会话去挡日线已经写好的推荐刷新。关注名单才是成交证据该覆盖的范围。" },
  ];
  gaps.forEach((g, i) => {
    const y = 1.3 + i * 1.7;
    card(s, 0.5, y, 12.3, 1.55);
    s.addShape(pres.shapes.RECTANGLE, {
      x: 0.5, y, w: 1.35, h: 1.55,
      fill: { color: C.orange }, line: { color: C.orange, width: 0 },
    });
    s.addText(g.n, {
      x: 0.5, y, w: 1.35, h: 1.55,
      fontFace: F.face, fontSize: 22, color: C.white, align: "center", valign: "middle", margin: 0,
    });
    s.addText(g.t, {
      x: 2.05, y: y + 0.15, w: 10.4, h: 0.4,
      fontFace: F.face, fontSize: 18, color: C.ink, bold: true, margin: 0,
    });
    s.addText(g.d, {
      x: 2.05, y: y + 0.58, w: 10.4, h: 0.8,
      fontFace: F.face, fontSize: 14, color: C.inkSoft, margin: 0,
    });
  });
}

// ============================================================
// 16 NEXT
// ============================================================
{
  const s = pres.addSlide();
  addFrame(s, { section: "§ 15  ·  接下来", pageNum: "16", tag: "NEXT" });
  s.addText("要理，按这个顺序，一次只动一层", {
    x: 0.5, y: 0.75, w: 12.3, h: 0.45,
    fontFace: F.face, fontSize: 24, color: C.ink, margin: 0,
  });
  const next = [
    { n: "1", t: "定研究宇宙", d: "全 A、行业、自选+异动，或别的。不要默认继续 914。" },
    { n: "2", t: "拆开选股门和成交门", d: "日线过了就能换推荐。分钟只要求今晚可能成交的票。" },
    { n: "3", t: "对齐持有周期", d: "要波段/长期，就不要用 5 日假说和当日涨跌幅当主菜单。" },
    { n: "4", t: "认账本", d: "8000 纸面和 AI 账本二选一当主账，或明确谁只读谁。" },
  ];
  next.forEach((n, i) => {
    const y = 1.35 + i * 1.15;
    s.addShape(pres.shapes.OVAL, {
      x: 0.55, y: y + 0.2, w: 0.7, h: 0.7,
      fill: { color: C.orange }, line: { color: C.orange, width: 0 },
    });
    s.addText(n.n, {
      x: 0.55, y: y + 0.2, w: 0.7, h: 0.7,
      fontFace: F.face, fontSize: 20, color: C.white, align: "center", valign: "middle", margin: 0,
    });
    s.addText(n.t, {
      x: 1.5, y: y, w: 11.2, h: 0.4,
      fontFace: F.face, fontSize: 18, color: C.ink, bold: true, margin: 0,
    });
    s.addText(n.d, {
      x: 1.5, y: y + 0.45, w: 11.2, h: 0.5,
      fontFace: F.face, fontSize: 15, color: C.inkSoft, margin: 0,
    });
  });
}
pres.writeFile({ fileName: "E:/codex/quant_new/docs/internal/QuantDesk_system_map_20260904.pptx" })
  .then(() => console.log("wrote pptx"))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
