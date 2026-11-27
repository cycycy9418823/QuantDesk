(() => {
  'use strict';
  const glyphs = {
    pulse:'<path d="M2 12h5l2.6-7 4.1 14 2.4-7H22"/>',
    sunrise:'<path d="M3 17h18M6 14a6 6 0 0 1 12 0M12 2v3M3 9l2 1M21 9l-2 1"/>',
    sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    brain:'<path d="M12 4c-2-3-6-1.5-6 1.5-3 .2-3.8 3.2-2.5 5.3-2 2.5-.9 5.7 1.7 6.4-.2 3.8 4.5 5.2 6.8 2.4V4Zm0 0c2-3 6-1.5 6 1.5 3 .2 3.8 3.2 2.5 5.3 2 2.5.9 5.7-1.7 6.4.2 3.8-4.5 5.2-6.8 2.4"/><path d="M6 8c0 2 1.2 3 3 3m9 3c0-2-1.2-3-3-3"/>',
    moon:'<path d="M20.5 13.4A8.5 8.5 0 0 1 10.6 3.5a8.7 8.7 0 1 0 9.9 9.9Z"/>',
    calendar:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7.5 3v4m9-4v4M3 10h18"/>',
    layers:'<path d="m3 8 9-5 9 5-9 5-9-5Zm1 5 8 4 8-4m-16 5 8 4 8-4"/>',
    wallet:'<path d="M20 7H5a2.5 2.5 0 0 1 0-5h12v5M3 5v13a2 2 0 0 0 2 2h15V7M20 12h-5v4h5"/>',
    chart:'<path d="M3 19h18M5 15l5-5 3.5 2.5L20 6m-4 0h4v4"/>',
    book:'<path d="M3 4h5c2 0 3.5.6 4 2 .5-1.4 2-2 4-2h5v15h-5c-2 0-3.5.6-4 2-.5-1.4-2-2-4-2H3V4Zm9 2v15"/>',
    check:'<path d="m5 12 4 4 10-10"/>', warn:'<path d="m12 3 9.2 16H2.8L12 3Z"/><path d="M12 9v4m0 2v.3"/>',
    shield:'<path d="m12 3 8 3v5c0 4-3.5 8.1-8 10-4.5-1.9-8-6-8-10V6l8-3Z"/>',
    up:'<path d="m4 17 6-6 3.5 3L20 7m-6 0h6v6"/>',
    down:'<path d="m4 7 6 6 3.5-3L20 17m-6 0h6v-6"/>',
    dash:'<path d="M5 12h14"/>', chevron:'<path d="m9 6 6 6-6 6"/>',
    arrow:'<path d="M6 12h12m-5-5 5 5-5 5"/>', list:'<path d="M8 6h12M8 12h12M8 18h12M4 6h.1M4 12h.1M4 18h.1"/>',
    bulb:'<path d="M8.5 17c0-1.8-.6-2.4-1.8-4.1a6.5 6.5 0 1 1 10.6 0c-1.2 1.7-1.8 2.3-1.8 4.1h-7ZM9 20h6"/>'
  };
  const icon = (key,size=19) => `<svg aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round">${glyphs[key]||''}</svg>`;
  const badge = (key,color) => `<span class="wp-icon wp-${color}">${icon(key)}</span>`;
  const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = x => x!==null && x!==undefined && x!=='' && typeof x!=='boolean' && Number.isFinite(Number(x)) ? Number(x) : null;
  const fmt = (x,places=2) => numeric(x)===null ? '—' : Number(x).toLocaleString('zh-CN',{minimumFractionDigits:places,maximumFractionDigits:places});
  const signed = x => numeric(x)===null?'—':(Number(x)>0?'+':'')+fmt(x);
  const money = x => numeric(x)===null?'—':'¥'+fmt(x);
  const priceClass = x => numeric(x)===null || Number(x)===0?'flat':Number(x)>0?'up':'down';
  const value = x => typeof x==='string' && x.trim() && x!=='数据缺失'?x.trim():'';
  const codeOf = stock => String(stock?.symbol || stock?.code || '');
  const validSymbol = s => /^\d{6}$/.test(s);
  const array = x => Array.isArray(x)?x:[];
  const state={date:'',mode:'evening',trackMode:'judgments',renderId:0};

  function ribbon(payload={}) {
    const steps=[['09:30–15:00','盘中快照','pulse','mint'],['10:00','早盘观察','sunrise','amber'],['14:30','午盘观察','sun','amber'],['20:20','蓝筹教练','brain','blue'],['22:35','晚间复盘','moon','violet']];
    const snapshot=payload?.source?.runtime?.snapshot||{};
    return `<div class="wp-ribbon" aria-label="${esc(payload?.day||'当日')}五节点">${steps.map(([time,label,glyph,color],i)=>{
      const item=array(payload?.pipeline)[i]||{};
      let note=value(item.note)||'暂无记录';
      if(i===0 && snapshot.as_of===payload?.day && numeric(snapshot.collected_slots)!==null && numeric(snapshot.expected_slots)!==null) note=`已收 ${snapshot.collected_slots}/${snapshot.expected_slots} 份`;
      if(note==='已记未验证') note='观察已保存';
      const ok=item.status==='recorded';
      let detail=value(item.detail);
      if(!/[\u3400-\u9fff]/.test(detail)) detail='';
      return `<div class="wp-step"${detail?' title="'+esc(detail)+'"':''}>${badge(glyph,color)}<div class="wp-step-text"><small>${time}</small><strong>${label}</strong><span class="${ok?'wp-complete':''}">${ok?icon('check',12):''}${esc(note)}</span></div></div>`;
    }).join('')}</div>`;
  }
  function segment(action,active,items){
    return `<div class="wp-seg" style="--sel:${items.findIndex(i=>i[0]===active)}" role="group" aria-label="${action==='mode'?'复盘视图':'核对视图'}"><i></i>${items.map(([key,label])=>`<button type="button" data-${action}="${key}" aria-pressed="${active===key}">${label}</button>`).join('')}</div>`;
  }
  function metric(glyph,color,label,amount,meta){
    return `<div class="wp-metric"><div>${badge(glyph,color)}<span>${esc(label)}</span></div><strong>${esc(amount)}</strong><small>${esc(meta||'')}</small></div>`;
  }
  function heading(title,sub,controls){
    return `<header class="wp-head"><div><h1>${title}</h1><p>${esc(sub)}</p></div><div class="wp-controls">${controls||''}</div></header>`;
  }
  function sectionTitle(title,caption=''){
    return `<div class="wp-section-title"><h2>${title}</h2>${caption?`<span>${esc(caption)}</span>`:''}</div>`;
  }
  const empty=text=>`<p class="wp-empty">${esc(text)}</p>`;
  const safeDates=d=>array(d.dates).filter(x=>/^\d{4}-\d{2}-\d{2}$/.test(x));
  function rowsForDay(d,w){
    const stocks=array(w?.stocks);
    const byCode=new Map(stocks.map(s=>[codeOf(s),s]));
    const rows=new Map();
    // A full position snapshot is eligible only on its own valuation date.
    if(state.mode==='evening' && w?.accounts?.ai?.as_of===d.day){
      for(const s of stocks){
        const pos=s.ai_position;
        if(pos && (!pos.as_of||pos.as_of===d.day)) rows.set(codeOf(s),{symbol:codeOf(s),name:s.name,pnl:numeric(pos.pnl),pnl_pct:numeric(pos.pnl_pct)});
      }
    }
    const facts=state.mode==='intraday'?array(d.intraday).flatMap(r=>Object.values(r.facts||{})).filter(r=>r?.name==='AI账' && r.as_of===d.day && validSymbol(r.symbol)).map(r=>({code:r.symbol,name:r.stock_name,pnl:r.unrealized_pnl,as_of:r.as_of})):array(d.contributions);
    for(const fact of facts){
      if(fact.as_of!==d.day || !validSymbol(codeOf(fact)))continue;
      const code=codeOf(fact), prior=rows.get(code)||{};
      rows.set(code,{...prior,symbol:code,name:fact.name||prior.name||code,pnl:numeric(fact.pnl)??prior.pnl??null,pnl_pct:numeric(fact.pnl_pct)??prior.pnl_pct??null,profit_yoy:numeric(fact.profit_yoy),earnings:fact.earnings,dividend:fact.dividend,change:numeric(fact.pct_chg),derived:fact.derived===true});
    }
    return [...rows.values()].map(row=>{
      const s=byCode.get(row.symbol)||{};
      const e=row.earnings||(s.earnings?.announcement_date<=d.day?s.earnings:{});
      const yoy=e.announcement_date && e.announcement_date<=d.day?numeric(e.profit_yoy):null;
      const change=row.change ?? (state.mode==='evening' && s.price_date===d.day?numeric(s.pct_chg):null);
      const div=row.dividend||(s.dividend?.as_of<=d.day?s.dividend:{});
      return {...row,name:s.name||row.name,profit_yoy:row.profit_yoy??yoy,change,earnings:e,dividend:div};
    }).sort((a,b)=>(b.pnl??-Infinity)-(a.pnl??-Infinity));
  }
  function profitBars(rows){
    const saved=rows.filter(r=>r.pnl!==null);
    const max=Math.max(1,...saved.map(r=>Math.abs(r.pnl)));
    return saved.length?saved.map(r=>`<button type="button" class="wp-profit-row" data-stock="${esc(r.symbol)}" data-group="profit"><span>${esc(r.name)}</span><i class="wp-axis"><i class="wp-bar wp-bar-${priceClass(r.pnl)}" style="width:${Math.abs(r.pnl)/max*46}%"></i></i><b class="wp-ink-${priceClass(r.pnl)}">${signed(r.pnl)}</b></button>`).join(''):empty('这一天还没有持仓浮盈记录。');
  }
  function dimension(glyph,text,color,title){
    return `<span class="wp-dimension wp-ink-${color}" title="${esc(title)}" aria-label="${esc(title)}">${icon(glyph,15)}<span>${esc(text)}</span></span>`;
  }
  function diagnosis(rows){
    if(!rows.length)return empty('这一天还没有按股保存的体检数据。');
    return `<div class="wp-diag-cols" aria-hidden="true"><span>持仓 / 财报期</span><span>累计净利同比</span><span>税前股息率</span><span>成本位置</span><span>当日涨跌</span></div>${rows.map(r=>{
      const p=r.pnl_pct, y=r.profit_yoy, c=r.change;
      return `<button type="button" class="wp-diag-row" data-stock="${esc(r.symbol)}" data-group="diag"><span>${esc(r.name)}<small class="v5-period-mini">${esc(r.earnings?.period_label||'—')}${r.earnings?.period_verified?' 累计':' · 期别未核'}<br>公告 ${esc(r.earnings?.announcement_date||'—')}</small></span>${dimension(y===null?'dash':y>=0?'check':'warn',y===null?'—':signed(y)+'%',y===null?'flat':y<0?'amber':'up','净利润同比 '+(y===null?'缺项':signed(y)+'%'))}<span class="v5-yield">${numeric(r.dividend?.yield_pct)!==null?fmt(r.dividend.yield_pct)+'%':'—'}<small class="v5-period-mini">${esc(r.dividend?.as_of||'未覆盖')}</small></span>${dimension('shield',p===null?'—':p===0?'持平':p>0?'上方':'下方',priceClass(p),'相对持仓成本 '+(p===null?'缺项':signed(p)+'%'))}${dimension(c===null||c===0?'dash':c>0?'up':'down',c===null?'—':signed(c)+'%',priceClass(c),'当日涨跌 '+(c===null?'缺项':signed(c)+'%'))}</button>`;
    }).join('')}`;
  }
  function reviewContent(d,w){
    const rows=rowsForDay(d,w), evening=state.mode==='evening';
    const records=evening?array(d.raw_records).filter(r=>r.slot==='2235'):array(d.intraday);
    const texts=array(d.raw_texts).filter(r=>evening?r.slot==='2235':r.slot!=='2235').filter(r=>typeof r.text==='string' && r.text.trim());
    const recorded=records.filter(r=>r.status==='recorded' && r.answer && typeof r.answer==='object');
    const insight=recorded.map(r=>value(r.answer.summary)||value(r.answer.insight)).filter(Boolean).at(-1) || (evening?value(d.insight):'');
    const missing=records.some(r=>r.status==='model_failed')?'该时段模型未完成，暂无已保存的核心判断。':'该时段尚无已保存的核心判断。';
    const raw=texts.length?texts.map(r=>`<div class="wp-original"><b>${esc(r.title)}</b><pre>${esc(r.text)}</pre></div>`).join(''):empty('这个时段还没有已保存的 AI 原文。');
    const dates=safeDates(d);
    const dateSelect=`<label class="wp-date">${icon('calendar',16)}<select aria-label="复盘日期" data-date>${dates.map(day=>`<option value="${day}" ${day===d.day?'selected':''}>${day}</option>`).join('')}</select></label>`;
    const period=segment('mode',state.mode,[['evening','晚间复盘'],['intraday','盘中观察']]);
    const book=d.books?.ai||{},old=d.books?.legacy||{},bench=d.benchmark||{};
    const bookMeta=b=>b.as_of?'估值于 '+b.as_of:'估值待记录';
    const useLocal=state.mode==='evening';
    const metrics=useLocal?metric('wallet','blue','AI 纸面账',money(book.equity),bookMeta(book))+metric('layers','violet','旧策略纸面账',money(old.equity),bookMeta(old))+metric('chart','amber','沪深 300',fmt(bench.close),bench.as_of?'收盘于 '+bench.as_of:'收盘待记录'):intradayMetrics(d);
    return {rows,html:`<div class="wp-page">${heading('每日复盘',d.day+' · '+(evening?'晚间复盘':'盘中观察'),dateSelect+period)}
      <div class="wp-kpis">${metrics}</div>
      ${ribbon(d)}
      <div class="wp-review-grid"><section class="wp-card">${sectionTitle('持仓浮盈','AI 账 · 元')}${profitBars(rows)}<small class="wp-note">${rows.some(r=>r.derived)?'累计浮盈，部分按当日收盘与持仓成本计算':'所选日期保存的累计浮盈'}</small></section>
      <section class="wp-card">${sectionTitle('持仓三维体检',rows.length?'已记录 '+rows.length+' 只':'')}${diagnosis(rows)}</section></div>
      <div class="wp-insight">${badge('bulb','amber')}<div><strong>核心洞察</strong><p>${esc(insight||missing)}</p></div></div>
      <details class="wp-fold"><summary>${icon('book',18)} 展开 AI 完整原文 ${icon('chevron',16)}</summary>${raw}</details></div>`};
  }
  function intradayMetrics(d){
    const snap=d.source?.runtime?.snapshot||{},same=snap.as_of===d.day;
    const recs=array(d.intraday);
    const items=[['1000','sunrise','早盘观察'],['1430','sun','午盘观察']].map(([slot,glyph,title],i)=>{
      const record=recs.find(r=>r.slot===slot), node=array(d.pipeline)[i+1]||{};
      return metric(glyph,i?'amber':'blue',title,node.note||'尚无记录',record?.recorded_at?record.recorded_at.slice(11,16)+' 保存':slot.slice(0,2)+':'+slot.slice(2));
    });
    return metric('pulse','mint','盘中快照',same&&numeric(snap.collected_slots)!==null?fmt(snap.collected_slots,0)+' 份':'—',same&&numeric(snap.expected_slots)!==null?'全天预计 '+snap.expected_slots+' 份':'等待快照')+items.join('');
  }
  function weeklyContent(d){
    const actual=d.available===true && !!value(d.text);
    if(!actual) return `<section class="wp-week-empty">${badge('book','violet')}<h2>周五周报</h2><p>还没有已保存的周五周报。</p></section>`;
    return `<section class="wp-card wp-week">${sectionTitle(esc(value(d.title)||'周五周报'),d.date)}${array(d.sections).map(s=>`<div><h3>${esc(s.title)}</h3><p>${esc((s.text||'').trim())}</p></div>`).join('')}<details class="wp-fold"><summary>${icon('book',18)} 展开周报原文 ${icon('chevron',16)}</summary><pre>${esc(d.text)}</pre></details></section>`;
  }
  function judgmentRows(records,summary,stocks){
    return records.map(record=>{
      const context=summary.record_context?.[record.decision_id]||{},code=codeOf(record),stock=stocks.find(s=>codeOf(s)===code);
      const day=value(record.decision_date)||'日期未存', name=stock?.name||value(record.name)||code||'盘面判断';
      const click=stock&&validSymbol(code)?`<button type="button" class="wp-stock-link" data-stock="${code}" data-group="judgment">${esc(name)} ${icon('arrow',14)}</button>`:`<b>${esc(name)}</b>`;
      const result=typeof record.result==='string' ? record.result : record.result?.outcome;
      const due=context.due_date||record.verification?.due_date;
      return `<article class="wp-judgment"><div class="wp-judgment-meta"><span>${esc(day)}</span><span>${esc(context.origin_label||'来源待核')}</span></div><div class="wp-judgment-main">${click}<p>${esc(value(record.judgment)||'未保存判断正文')}</p></div><div class="wp-verdict"><b>${esc(value(context.label)||'核对状态待记')}</b><small>原判定 ${esc(value(result)||'—')}${due?' · 到期 '+esc(due):''}</small></div></article>`;
    }).join('');
  }
  function trackContent(score,week,w){
    const s=score.summary||{},cred=s.credible_model||{},mature=numeric(score.matured_count),correct=numeric(cred.correct);
    const list=array(score.records).slice().sort((a,b)=>String(b.decision_date||'').localeCompare(String(a.decision_date||'')));
    const stocks=array(w?.stocks),rows=stocks.filter(st=>list.some(r=>codeOf(r)===codeOf(st))).map(st=>({...st,symbol:codeOf(st)}));
    const controls=segment('trackmode',state.trackMode,[['judgments','历史判断'],['weekly','周五周报']]);
    const known=score.source?.available===true||score.available===true||Object.hasOwn(cred,'denominator');
    const metrics=metric('check','mint','可信已到期样本',mature===null?'—':fmt(mature,0)+' 笔',mature===0?'暂无可信命中率':'已完成观察与核对')+
      metric('list','blue','历史判断',known?fmt(list.length,0)+' 条':'—',correct===null?'可信命中数待核':'其中可信命中 '+fmt(correct,0)+' 笔')+
      metric('book','violet','周五周报',week.available?'已保存':'尚未生成',week.date||'最近一期');
    let body=weeklyContent(week);
    if(state.trackMode==='judgments'){
      body=`<section class="wp-card wp-history">${sectionTitle('历史判断明细',known?list.length+' 条记录':'')}${list.length?judgmentRows(list,s,stocks):empty(known?'暂无已保存的判断。':'暂时无法读取历史判断。')}</section>`;
    }
    const sub=score.as_of?'核对截至 '+score.as_of:'核对日期待记录';
    return {rows,html:`<div class="wp-page">${heading('对错与周报',sub,controls)}<div class="wp-kpis">${metrics}</div>${body}</div>`};
  }
  async function read(path){
    const r=await fetch(path,{method:'GET',cache:'no-store'});
    if(!r.ok)throw Error('读取失败');
    return r.json();
  }
  async function mount(page,element){
    const revision=++state.renderId;
    const current=()=>state.renderId===revision && element.isConnected;
    element.innerHTML='<div class="wp-page" aria-busy="true"><p class="wp-empty">正在读取本地记录…</p></div>';
    let payload,weekly,workspace;
    try{
      const pending=window.WealthApp?.workspace || window.WealthApp?.ready || {};
      [payload,weekly,workspace]=await Promise.all([page==='review'?read('/api/review'+(state.date?'?date='+encodeURIComponent(state.date):'')):read('/api/scorecard'),page==='track'?read('/api/weekly'):null,pending]);
      workspace=window.WealthApp?.workspace||workspace||{};
      if(!current())return;
      if(page==='review')state.date=payload.day;
    }catch{
      if(current())element.innerHTML='<div class="wp-page"><p class="wp-empty">本地记录暂时无法读取。</p><button type="button" class="wp-retry">重试</button></div>';
      element.querySelector('.wp-retry')?.addEventListener('click',()=>mount(page,element));
      return;
    }
    function render(){
      if(!current())return;
      const result=page==='review'?reviewContent(payload,workspace):trackContent(payload,weekly,workspace);
      element.innerHTML=result.html;
      element.querySelector('[data-date]')?.addEventListener('change',e=>{state.date=e.target.value;mount(page,element);});
      element.querySelectorAll('[data-mode],[data-trackmode]').forEach(b=>b.addEventListener('click',()=>{
        const originalSegment=b.closest('.wp-seg');
        if(b.dataset.mode)state.mode=b.dataset.mode;
        else state.trackMode=b.dataset.trackmode;
        render();
        const nextSegment=element.querySelector('.wp-seg');
        if(nextSegment && originalSegment){
          const nextThumb=nextSegment.querySelector('i');
          const width=nextThumb.clientWidth;
          const from=Number(originalSegment.style.getPropertyValue('--sel'))*width;
          const to=Number(nextSegment.style.getPropertyValue('--sel'))*width;
          if(from!==to && !window.matchMedia('(prefers-reduced-motion: reduce)').matches)
            nextThumb.animate([{transform:'translateX('+from+'px)'},{transform:'translateX('+to+'px)'}],{duration:360,easing:'cubic-bezier(.23,1.28,.34,1)'});
          nextSegment.querySelector('[aria-pressed=true]')?.focus({preventScroll:true});
        }
      }));
      element.querySelectorAll('[data-stock]').forEach(b=>b.addEventListener('click',()=>{
        const members=[...element.querySelectorAll('[data-stock]')].filter(el=>el.dataset.group===b.dataset.group).map(el=>el.dataset.stock);
        const distinct=[...new Set(members)];
        const items=distinct.map(code=>({...result.rows.find(r=>codeOf(r)===code),symbol:code}));
        window.WealthDrawer?.open(b.dataset.stock,items);
      }));
    }
    render();
  }
  window.WealthPages={ribbon,mount};
})();
