(function WealthApp(){
  'use strict';
  const $=id=>document.getElementById(id);
  const keys=['home','review','research','track'];
  const state={workspace:null,history:null,page:'home',list:'held',range:'all',visible:{ai:true,strategy:true,benchmark:true},query:'',chart:null,rangeInfo:null,hover:null,renderedAmount:null,animation:0};
  const icons={
    moon:'<path d="M20.8 13A9 9 0 0 1 11 3a9 9 0 1 0 9.8 10Z"/>',
    sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-9h.01"/>',
    external:'<path d="M13 5h6v6m0-6-9 9m6 0v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1h4"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    risk:'<path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
    plan:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4m8-4v4M3 11h18m-12 4 2 2 4-4"/>',
    shield:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
    check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    rise:'<path d="m3 17 6-6 4 4 8-10m-6 0h6v6"/>',
    fall:'<path d="m3 7 6 6 4-4 8 10m-6 0h6v-6"/>',
    dash:'<path d="M5 12h14"/>', arrow:'<path d="M4 12h15m-6-6 6 6-6 6"/>'
  };
  const icon=(name,size=17)=>`<svg aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round">${icons[name]||icons.dash}</svg>`;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const valid=Number.isFinite;
  const fmt=(n,d=2)=>valid(n)?n.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const signed=(n,s='%',d=2)=>valid(n)?`${n>0?'+':''}${fmt(n,d)}${s}`:'—';
  const tone=n=>!valid(n)||n===0?'flat':n>0?'up':'down';
  const held=()=>state.workspace?.stocks.filter(s=>s.ai_position)||[];
  function thumb(seg){
    const active=seg.querySelector('button.active'),slider=seg.querySelector(':scope > .seg-thumb');
    if(!active||!slider||!seg.offsetWidth)return;
    slider.style.width=`${active.offsetWidth}px`;
    slider.style.transform=`translateX(${active.offsetLeft-slider.offsetLeft}px)`;
  }
  function activate(seg,button){
    seg.querySelectorAll(':scope > button').forEach(b=>{
      b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));
      if(seg.id==='mainNav'){if(b===button)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}
    });thumb(seg);
  }
  function updateThumbs(){['mainNav','listSeg','rangeSeg'].forEach(id=>thumb($(id)));}
  function theme(dark,save=false){
    document.body.classList.toggle('dark',dark);document.body.classList.toggle('light',!dark);
    const label=dark?'切换到丝绒晨白':'切换到 Apple 纯黑 OLED';
    $('themeBtn').innerHTML=icon(dark?'sun':'moon');$('themeBtn').title=label;$('themeBtn').setAttribute('aria-label',label);$('themeBtn').setAttribute('aria-pressed',String(dark));
    if(save){try{localStorage.setItem('quantdesk.wealth.v4.theme',dark?'dark':'light');}catch{}}
    state.chart?.redraw();window.WealthDrawer?.redraw?.();
  }
  async function read(path){const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw Error(`HTTP ${r.status}`);return r.json();}
  function modal(title,html){
    $('infoTitle').textContent=title;$('infoBody').innerHTML=html;
    if(!$('infoDialog').open)$('infoDialog').showModal();$('closeInfo').focus();
  }
  function section(title,body,path=''){return `<section class="info-block"><strong>${escape(title)}</strong><p>${escape(body)}</p>${path?`<code>${escape(path)}</code>`:''}</section>`;}
  function sources(extra){
    const w=state.workspace||{},h=w.health||{},dates=state.rangeInfo;
    let html=extra&&typeof extra==='string'?section('当前股票',extra):'';
    html+=section('两本独立的纸面账',`AI 账估值日 ${w.accounts?.ai?.as_of??'—'}，旧策略账估值日 ${w.accounts?.legacy?.as_of??'—'}。持仓盈亏取各自账本的已保存值。`,'state/ai_trader/ledger.json · state/auto_paper_portfolio.json');
    html+=section('净值与同期收益',`各线按可视区间内本线首个有效值归零，纵轴为期间涨跌幅。缺档留断点。AI 与沪深300的超额按共同有效首尾日计算，单位为百分点。当前比较区间 ${dates?.comparisonStart??'—'} 至 ${dates?.comparisonEnd??'—'}。指数为不含分红价格指数。`,'state/ai_trader/journal.jsonl · results/auto_paper_portfolio_history.json · data/benchmark/hs300.csv');
    html+=section('日线、估值、三季报与分红',`日线只用全表已声明的原始价格；剔除盘中临时记录。估值快照 ${h.valuation_date??'—'}。核心26股优先使用2026Q3累计与公告日；其他股票旧Q2期别未核。股息率为2026-11-24已核税前TTM；五年分位仅用本地已有有效样本，历史最晚2026-07-02，缺口不补造。历史PE（静/代理）与当日动态PE可能不同口径。PE、PB 单位为倍，净利润同比与 ROE 单位为 %。`,'data/daily/<股票代码>.csv · data/metadata/valuation_snapshots/ · data/metadata/earnings_report_2026Q2.csv');
    html+=section('买卖计划与参考价','最新保存计划经账本身份、日期和状态校验后展示。算法买点确认、退出参考与上方观察位按接口有效值标示。买入条件和研究价位分开显示。AI 明确的数字止损价单独标示。','state/ai_trader/plan_latest.json · GET /api/stock/<股票代码>/workbench');
    html+=section('蓝筹与策略候选',`蓝筹名单 ${w.bluechip_count??'—'} 只；候选批次 ${w.candidates?.as_of??'—'}，跟踪日期 ${w.candidates?.tracking_as_of??'—'}。`,'ai_trader/prompts/candidates/bluechip_coach_v1/menu.json · results/candidate_refresh/formal_candidate_batch_latest.json · results/candidate_tracking/latest.json');
    html+=section('每日复盘、核对与周报','今日轨按今天的记录显示。复盘按所选日期读取，可信胜率只以已到期、可核验的模型判断为样本。盈亏比为当前 AI 账持仓浮盈总额除以浮亏绝对总额。','GET /api/daily-companion · state/daily_companion/ · data/snapshots/ · GET /api/ai-trader/decisions · reports/weekly_learning/');
    modal('口径与来源',html);
  }
  function amount(value){
    cancelAnimationFrame(state.animation);
    function paint(n){state.renderedAmount=n;const parts=fmt(n).split('.');$('heroInt').textContent=parts[0];$('heroDec').textContent=parts.length>1?'.'+parts[1]:'';}
    if(!valid(value)||!valid(state.renderedAmount)||matchMedia('(prefers-reduced-motion: reduce)').matches){paint(value);return;}
    const start=state.renderedAmount,at=performance.now();
    function step(now){const t=Math.min(1,(now-at)/135);paint(start+(value-start)*(1-Math.pow(1-t,3)));if(t<1)state.animation=requestAnimationFrame(step);}
    state.animation=requestAnimationFrame(step);
  }
  function displayHover(row){
    state.hover=row;const a=state.workspace?.accounts?.ai||{};
    amount(row?row.ai:a.equity);
    $('balanceDate').textContent=row?`${row.date} · ${valid(row.ai)?'历史估值':'当日缺档'}`:a.as_of?`截至 ${a.as_of} 收盘`:'估值日 —';
    $('cashCaption').textContent=row?`旧策略 ${valid(row.strategy??row.legacy)?'¥'+fmt(row.strategy??row.legacy):'—'}　沪深300 ${fmt(row.benchmark)} 点`:`现金缓冲 ${valid(a.cash_pct)?fmt(a.cash_pct,1)+'%':'—'}　${valid(a.cash)?'¥'+fmt(a.cash,0):'—'}`;
  }
  function displayRange(r){
    state.rangeInfo=r;
    const p=$('heroReturn'),a=$('heroAlpha');
    p.textContent=`区间收益 ${signed(r.aiReturn)}`;p.className=`chg-badge chg-${tone(r.aiReturn)}`;
    a.textContent=valid(r.alpha)?`${r.alpha<0?'跑输':r.alpha>0?'跑赢':'持平'}沪深300 ${fmt(Math.abs(r.alpha))} 个百分点`:'相对沪深300 —';a.className=`alpha-badge ${tone(r.alpha)}`;
    $('chartCaption').textContent=r.count?`${r.firstDate} 至 ${r.lastDate}　·　${r.count} 个交易日`:'暂无可核验的历史记录';
  }
  function spark(series){
    const pts=(series||[]).filter(p=>valid(p[1])).slice(-24);
    if(pts.length<2)return '<span class="spark t3">—</span>';
    const nums=pts.map(p=>p[1]), low=Math.min(...nums),spread=Math.max(...nums)-low;
    const color=tone(nums.at(-1)-nums[0]);const poly=nums.map((y,i)=>`${(i/(nums.length-1)*102+1).toFixed(1)},${(spread?27-(y-low)/spread*22:16).toFixed(1)}`).join(' ');
    return `<svg class="spark ${color}" viewBox="0 0 104 32" aria-label="最近${pts.length}个可用收盘点"><polyline points="${poly}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  }
  function sig(name,text,cls,title){return `<span class="sig-item ${cls}" title="${escape(title)}">${icon(name,14)}${escape(text)}</span>`;}
  const refs=new Map();
  function rows(items,book){
    return items.map(s=>{
      const p=book==='legacy'?s.legacy_position:s.ai_position, profit=s.earnings?.profit_yoy,day=s.pct_chg;
      const exit=refs.get(s.code)?.stop??null, validExit=typeof exit==='number' && valid(s.price);
      const touched=validExit&&s.price<=exit, cost=p?.pnl_pct, color=tone(cost);
      const diagnostics=sig(!valid(profit)?'dash':profit<0?'risk':'check',`${s.earnings?.period_label||'财报'} 净利 ${signed(profit,'%',1)}`,!valid(profit)?'dim':profit<0?'warn':'good',`${s.earnings?.period_label||'财报'} 累计 · 公告 ${s.earnings?.announcement_date||'—'}`)
        +sig('book',`税前股息 ${valid(s.dividend?.yield_pct)?fmt(s.dividend.yield_pct)+'%':'—'}`,'',`近12个月 · 截至 ${s.dividend?.as_of||'—'}`)
        +sig('shield',touched?'触退出参考':!valid(cost)?'成本 —':cost<0?'成本下方':cost===0?'平成本':'成本上方',touched||cost<0?'warn':'','持仓成本与最近有效退出参考')
        +sig(!valid(day)||day===0?'dash':day>0?'rise':'fall',signed(day,'%',2),tone(day),`收盘涨跌 · ${s.price_date??'—'}`);
      const showPnl=!!p;
      const candidate=state.list==='candidates'&&book!=='legacy'?s.candidate_info:null;
      return `<div class="v5-stock-wrap"><input class="v5-select" type="checkbox" data-compare-select="${s.code}" aria-label="选择${escape(s.name)}进行对比"><button class="asset-row" data-symbol="${s.code}" data-book="${book}" aria-label="查看${escape(s.name)} ${s.code}">
      <span class="avatar av${Number(s.code.slice(-2))%4}">${escape(s.name.slice(0,1))}</span>
      <span><span class="a-name">${escape(s.name)} <span class="a-code">${s.code}</span></span><span class="sig-row">${diagnostics}</span><span class="v5-period-mini">${escape(s.earnings?.period_label||'财报 —')}${s.earnings?.period_verified?' 累计':' · 期别未核'} · 公告 ${escape(s.earnings?.announcement_date||'—')}</span>${candidate?`<span class="a-sub">候选第 ${escape(candidate.rank??'—')} 名 · 跟踪${escape(trackingStatus(candidate.tracking?.sample_status))}</span>`:''}</span>
      ${spark(s.series)}<span class="row-price">${fmt(s.price)}<small>收盘价</small></span>
      <span class="row-pnl"><span class="a-pill ${showPnl?color:tone(day)}">${signed(showPnl?cost:day)}</span><small>${showPnl?'持仓浮动':'日涨跌'}</small></span></button></div>`;
    }).join('');
  }
  function trackingStatus(t){return ({pending:'待到期',pending_observation:'待观察',not_matured:'待到期',not_due:'待到期',completed:'已记录',matured:'已到期',missing:'缺记录',open:'观察中',partial:'部分记录'})[t]||(t?'已记录':'缺记录');}
  function list(type){
    let stocks=state.workspace?.stocks||[];
    if(type==='legacy')return stocks.filter(s=>s.legacy_position);
    if(type==='held')stocks=held().slice().sort((a,b)=>(b.ai_position.weight??-Infinity)-(a.ai_position.weight??-Infinity));
    if(type==='pool')stocks=stocks.filter(s=>s.bluechip);
    if(type==='candidates')stocks=stocks.filter(s=>s.candidate).sort((a,b)=>a.candidate_info.rank-b.candidate_info.rank);
    const q=state.query.trim().toLowerCase();return stocks.filter(s=>s.code.includes(q)||s.name.toLowerCase().includes(q));
  }
  function renderList(){
    const items=list(state.list),w=state.workspace;
    $('assetList').innerHTML=rows(items,'ai')||`<p class="empty-note">${state.query?'没有匹配的股票':'暂无对应记录'}</p>`;
    $('listDate').textContent=state.list==='candidates'?`批次 ${w?.candidates?.as_of??'—'}　·　跟踪 ${w?.candidates?.tracking_as_of??'—'}`:`${state.list==='held'?'AI 纸面账':'蓝筹观察池'}　·　收盘资料 ${w?.health?.daily?.latest_date??'—'}`;
  }
  function card(iconName,shade,title,tail,headline,desc,attribute='',color=''){
    const tag=attribute?'button':'article';
    return `<${tag} class="story-card${attribute?'':' static'}" ${attribute}><div class="story-k"><span class="story-k-left"><i class="icon-sq sq-${shade}">${icon(iconName)}</i><span>${escape(title)}</span></span><span class="tail t3">${escape(tail)}</span></div><div class="story-v ${color}">${escape(headline)}</div><div class="story-d">${escape(desc)}</div></${tag}>`;
  }
  function renderStories(){
    const stocks=held(), a=state.workspace.accounts?.ai||{},p=state.workspace.latest_plan||{};
    const losses=stocks.filter(s=>valid(s.ai_position.pnl)&&s.ai_position.pnl<0).sort((a,b)=>a.ai_position.pnl-b.ai_position.pnl);
    const weak=losses[0]||stocks.filter(s=>valid(s.earnings?.profit_yoy)&&s.earnings.profit_yoy<0).sort((a,b)=>a.earnings.profit_yoy-b.earnings.profit_yoy)[0];
    const gains=stocks.filter(s=>s.ai_position.pnl>0),g=gains.reduce((a,s)=>a+s.ai_position.pnl,0),l=-losses.reduce((a,s)=>a+s.ai_position.pnl,0);
    let lossDesc='当前没有已记录的浮亏或净利下降持仓。';
    if(weak){lossDesc=`${losses.length?'浮亏最大':'净利降幅最大'}，账面 ${signed(weak.ai_position.pnl,' 元')}。净利润同比 ${signed(weak.earnings?.profit_yoy,'%',1)}。`;}
    const verified=p.accepted_envelope===true;
    const planText=verified?(p.committed_intents_count===0?'暂无新增买卖':`已保存 ${p.committed_intents_count} 笔买卖`):'计划待核';
    $('storyCards').innerHTML=card('risk','down','最需要留意的持仓',weak?'看 K 线 →':'',weak?`${weak.name} ${signed(weak.ai_position.pnl_pct,'%',1)}`:'暂无提醒',lossDesc,weak?`data-risk="${weak.code}"`:'',weak?tone(weak.ai_position.pnl_pct):'')
      +card('plan','blue','明日买卖计划',p.trade_date?.slice(5)||'—',planText,verified?`${p.decision_date??'—'} 制定，适用 ${p.trade_date??'—'}。待开盘意图 ${p.pending_intents_count??'—'} 笔。`:'最近一次计划缺少可核验的保存记录。','data-plan="1"')
      +card('shield','up','收益与仓位健康',`${gains.length} 盈 / ${losses.length} 亏`,`现金缓冲 ${valid(a.cash_pct)?fmt(a.cash_pct,1)+'%':'—'}`,`持仓浮盈亏金额比 ${l>0?fmt(g/l,2)+' 倍':'—'}，已投仓位 ${valid(a.position_pct)?fmt(a.position_pct,1)+'%':'—'}。`);
  }
  function planDetails(){
    const p=state.workspace?.latest_plan||{},intents=p.accepted_envelope?p.intents||[]:[];
    const summary=p.accepted_envelope?p.summary:'计划缺少有效保存记录。';
    modal('已保存买卖计划',section(p.trade_date?`适用 ${p.trade_date}`:'适用日 —',summary)+intents.map(i=>section(`${i.symbol||'—'} · ${{buy:'买入',sell:'卖出',hold:'持有'}[i.side]||'其他'}`,`${valid(i.quantity)?fmt(i.quantity,0)+' 股 · ':''}${i.display_reason||i.reason||'理由 —'}`)).join(''));
  }
  function renderCoachPlan(){
    const p=state.workspace.latest_plan||{},items=p.accepted_envelope?p.intents||[]:[];
    const names=new Map(state.workspace.stocks.map(s=>[s.code,s.name]));
    $('coachPlan').innerHTML=`<header><div><span>BLUECHIP COACH · 已保存原件</span><h2>${escape(p.execution_label||'纸面计划待核')}</h2><p>适用 ${escape(p.trade_date||'—')} · ${escape(p.display_warning||'计划不可核实，留空')}</p></div><b>${p.pending_intents_count??0} 笔待开盘</b></header>`+
      items.map(i=>`<article><div class="v6-plan-top"><h3>${escape(names.get(i.symbol)||i.symbol)} <small>${escape(i.symbol)}</small></h3><span>${{sell:'待卖出',buy:'待买入',hold:'继续观察'}[i.side]||'待核'} ${valid(i.quantity)?fmt(i.quantity,0)+'股':''}</span></div><div class="v6-verified"><b>${escape(i.verified_clauses_label)}</b><ul>${(i.verified_clauses||[]).map(c=>`<li>${escape(c)}</li>`).join('')||'<li>没有可用的同标的已核短句</li>'}</ul></div><details class="v6-model-reason"><summary>${escape(i.display_reason_label)}</summary><p>${escape(i.display_reason||'理由缺失')}</p></details></article>`).join('');
  }
  function renderHome(){
    const w=state.workspace, a=w.accounts?.legacy||{};
    displayHover(null);renderStories();renderList();renderCoachPlan();
    $('listSeg').querySelector('[data-list=held]').textContent=`我的持仓 (${held().length})`;
    $('listSeg').querySelector('[data-list=pool]').textContent=`${w.bluechip_count??'—'} 蓝筹池`;
    $('listSeg').querySelector('[data-list=candidates]').textContent=`每日策略候选 (${w.candidates?.count??'—'})`;
    $('legacyCount').textContent=`${a.positions_count??'—'} 只持仓`;
    $('legacyBalance').innerHTML=`¥${fmt(a.equity)}　<span class="${tone(a.return_pct)}">${signed(a.return_pct)}</span>`;
    $('legacyList').innerHTML=rows(list('legacy'),'legacy')||'<p class="empty-note">暂无已保存的旧策略持仓。</p>';
    updateThumbs();
  }
  async function renderToday(day){
    let payload;
    try{payload=await read(`/api/review?date=${encodeURIComponent(day)}`);}catch{payload={day,pipeline:[]};}
    $('todayRibbon').innerHTML=window.WealthPages.ribbon(payload);
  }
  function chart(){
    state.chart?.destroy();
    state.chart=window.WealthCharts.equity($('eqCanvas'),{rows:state.history?.rows||[],range:state.range,visible:state.visible,onHover:displayHover,onRange:displayRange});
  }
  async function navigate(key){
    if(!keys.includes(key))return;
    state.page=key;
    keys.forEach(k=>{$(k+'Page').hidden=k!==key;});
    activate($('mainNav'),$('mainNav').querySelector(`[data-page="${key}"]`));
    window.scrollTo(0,0);
    if(key==='home'){updateThumbs();state.chart?.redraw();return;}
    if(key==='research'){await window.WealthResearch.mount($(key+'Page'));return;}
    await window.WealthPages.mount(key,$(key+'Page'));
  }
  function bind(){
    $('sourceBtn').innerHTML=icon('info');$('researchLink').innerHTML=icon('external');$('searchIcon').innerHTML=icon('search',15);
    let dark=false;try{dark=localStorage.getItem('quantdesk.wealth.v4.theme')==='dark';}catch{}theme(dark);
    $('themeBtn').addEventListener('click',()=>theme(!document.body.classList.contains('dark'),true));
    $('sourceBtn').addEventListener('click',()=>sources());$('closeInfo').addEventListener('click',()=>$('infoDialog').close());
    $('infoDialog').addEventListener('click',event=>{if(event.target!==$('infoDialog'))return;const b=event.target.getBoundingClientRect();if(event.clientX<b.left||event.clientX>b.right||event.clientY<b.top||event.clientY>b.bottom)event.target.close();});
    $('mainNav').addEventListener('click',e=>{const b=e.target.closest('[data-page]');if(b)navigate(b.dataset.page);});
    $('rangeSeg').addEventListener('click',e=>{const b=e.target.closest('[data-range]');if(!b)return;state.range=b.dataset.range;activate($('rangeSeg'),b);state.chart?.setRange(state.range);});
    document.querySelector('.leg-pills').addEventListener('click',e=>{const b=e.target.closest('[data-series]');if(!b)return;const k=b.dataset.series;state.visible[k]=!state.visible[k];b.setAttribute('aria-pressed',String(state.visible[k]));b.classList.toggle('off',!state.visible[k]);state.chart?.setVisible(state.visible);});
    $('listSeg').addEventListener('click',e=>{const b=e.target.closest('[data-list]');if(!b)return;state.list=b.dataset.list;activate($('listSeg'),b);renderList();});
    $('stockSearch').addEventListener('input',e=>{state.query=e.target.value;renderList();});
    $('homePage').addEventListener('click',e=>{
      if(e.target.closest('[data-plan]'))return planDetails();
      const risk=e.target.closest('[data-risk]');if(risk)return window.WealthDrawer.open(risk.dataset.risk,held());
      const s=e.target.closest('[data-symbol]');if(s){const legacy=s.dataset.book==='legacy';window.WealthDrawer.open(s.dataset.symbol,list(legacy?'legacy':state.list),{book:legacy?'legacy':'ai'});}
    });
    new ResizeObserver(updateThumbs).observe(document.querySelector('.topbar-inner'));
    document.fonts.ready.then(updateThumbs);
  }
  const app=window.WealthApp={workspace:null,ready:null,sources};
  bind();
  app.ready=read('/api/workspace').then(w=>{
    if(!Array.isArray(w.stocks))throw Error('workspace unavailable');
    w.stocks.forEach(s=>{s.symbol=s.code;});state.workspace=w;app.workspace=w;
    renderHome();renderToday(w.as_of);
    const risk=held().filter(s=>s.ai_position.pnl<0).sort((a,b)=>a.ai_position.pnl-b.ai_position.pnl)[0];
    if(risk)read('/api/reference?symbol='+risk.code).then(r=>{if(r.available&&r.reference_current!==false){refs.set(risk.code,r);renderList();}}).catch(()=>{});
    return w;
  }).catch(()=>{$('assetList').innerHTML='<p class="empty-note">本地账本暂不可读，请刷新重试。</p>';$('balanceDate').textContent='估值资料暂缺';return null;});
  Promise.all([app.ready,read('/api/equity-history').catch(()=>({rows:[]}))]).then(([,history])=>{state.history=history;chart();});
  updateThumbs();
})();
