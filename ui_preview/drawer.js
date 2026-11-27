(() => {
  'use strict';
  const finite=Number.isFinite;
  const fmt=(v,d=2)=>finite(v)?v.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const signed=(v,d=2)=>finite(v)?(v>0?'+':'')+fmt(v,d):'—';
  const percent=(v,d=2)=>finite(v)?signed(v,d)+'%':'—';
  const id=k=>document.getElementById(k);
  const codeOf=s=>String(s?.symbol??s?.code??'');
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const iconPaths={
    close:'<path d="m6 6 12 12M18 6 6 18"/>',up:'<path d="m6 14 6-6 6 6"/>',down:'<path d="m6 10 6 6 6-6"/>',
    info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-9h.01"/>',
    scale:'<path d="M12 3v16m-5 2h10M4 7l8-2 8 2M5 8l-3 6h6Zm14 0-3 6h6Z"/>',
    report:'<path d="M5 4h9l5 5v11H5zM14 4v6h5M8 14h7M8 17h5"/>',
    trend:'<path d="M3 18h18M5 15l5-6 4 3 6-8m-6 0h6v6"/>',
    book:'<path d="M3 5c4-1 6-1 9 1 3-2 5-2 9-1v14c-4-1-6-1-9 1-3-2-5-2-9-1zm9 1v14"/>'
  };
  const icon=k=>'<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+iconPaths[k]+'</svg>';
  let mounted=false, current=null, items=[], book='ai', chart=null, history=null, ref=null, rows=[];
  let abort=null, focusBack=null, mode='candle', token=0, savedOverflow='', refLoading=false;
  function mount(){
    if(mounted)return;
    document.body.insertAdjacentHTML('beforeend',
      '<div id="sheetBackdrop" class="qd-shade" aria-hidden="true"></div>'+
      '<aside id="sheet" class="qd-sheet" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="sName" inert>'+
      '<header class="qd-sheet-head"><div><div class="qd-sheet-eyebrow"><span id="sBook"></span><span>纸面 · 未验证 · 只读</span></div>'+
      '<div class="qd-sheet-title"><h2 id="sName"></h2><span id="sCode"></span></div><div id="sSub" class="qd-sheet-sub"></div></div>'+
      '<div class="qd-head-trailing"><div class="qd-quote"><strong id="sPrice"></strong><small id="sDelta"></small></div>'+
      '<button id="sClose" class="qd-icon-btn" aria-label="关闭股票抽屉" title="关闭 (Esc)">'+icon('close')+'</button></div></header>'+
      '<div class="qd-sheet-scroll"><div class="qd-chart-head"><h3>日线走势</h3>'+
      '<div id="kModeSeg" class="qd-mode" aria-label="图表类型"><i aria-hidden="true"></i><button type="button" data-mode="candle" aria-pressed="true">蜡烛 K 线</button><button type="button" data-mode="line" aria-pressed="false">丝滑曲线</button></div></div>'+
      '<div class="qd-chart-box"><div id="kHud" class="qd-hud"></div><div class="qd-k-stage"><canvas id="kCanvas" aria-label="日线价格、均线与成交量。滚轮缩放、左右拖拽、双击复原。"></canvas><canvas class="qd-chart-cross" aria-hidden="true"></canvas></div>'+
      '<div class="qd-chart-key"><span><i class="ma5"></i>MA5</span><span><i class="ma20"></i>MA20</span><span class="qd-count" id="kCount"></span></div></div>'+
      '<section class="qd-map"><div class="qd-inner-title"><h3>价位一眼地图</h3><small>研究参考 · 未验证</small></div><div id="sSpectrum"></div></section>'+
      '<div class="qd-insights" id="sInsights"></div><section id="sTracking" class="qd-tracking" hidden></section><section class="qd-thesis"><div class="qd-inner-title"><h3><span class="qd-mini-icon">'+icon('book')+'</span><span id="sThesisTitle"></span></h3><button id="sSource" class="qd-source" type="button" title="查看本股口径与来源">'+icon('info')+' 口径与来源</button></div><div id="sThesis"></div></section></div>'+
      '<footer class="qd-sheet-foot"><span id="sNavState"></span><div><button type="button" id="sPrev" class="qd-icon-btn" aria-label="上一只股票" title="上一只 (↑)">'+icon('up')+'</button><button type="button" id="sNext" class="qd-icon-btn" aria-label="下一只股票" title="下一只 (↓)">'+icon('down')+'</button></div></footer></aside>');
    const scroll=id('sheet').querySelector('.qd-sheet-scroll');
    const tabs=document.createElement('nav');tabs.className='v6-tabs';tabs.setAttribute('role','tablist');
    tabs.innerHTML=[['trend','走势与计划'],['facts','财报与估值'],['news','分红与公告']].map(([key,label])=>`<button role="tab" data-detail-tab="${key}" aria-selected="${key==='trend'}">${label}</button>`).join('');
    scroll.before(tabs);
    for(const key of ['trend','facts','news']){const panel=document.createElement('section');panel.id='v6Panel_'+key;panel.className='v6-panel';panel.hidden=key!=='trend';panel.setAttribute('role','tabpanel');scroll.append(panel);}
    for(const selector of ['.qd-chart-head','.qd-chart-box','.qd-map','#sTracking','.qd-thesis'])id('v6Panel_trend').append(scroll.querySelector(selector));
    id('v6Panel_facts').append(id('sInsights'));
    id('v6Panel_news').innerHTML='<div id="sDividendPanel"></div><div id="sAnnouncements"></div>';
    tabs.addEventListener('click',e=>{const b=e.target.closest('[data-detail-tab]');if(b)setTab(b.dataset.detailTab);});
    id('sClose').onclick=close;id('sheetBackdrop').onclick=close;
    id('sPrev').onclick=()=>step(-1);id('sNext').onclick=()=>step(1);
    id('sSource').onclick=source;
    id('kCanvas').addEventListener('pointerenter',()=>document.body.classList.add('qd-inspecting'));
    id('kCanvas').addEventListener('pointerleave',()=>document.body.classList.remove('qd-inspecting'));
    id('kModeSeg').addEventListener('click',e=>{const btn=e.target.closest('[data-mode]');if(btn)setMode(btn.dataset.mode);});
    document.addEventListener('keydown',key);
    mounted=true;
  }
  function otherModal(){
    return document.querySelector('dialog[open]') || [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')].some(el=>
      el!==id('sheet') && el.getAttribute('aria-hidden')!=='true' && el.checkVisibility?.());
  }
  function key(e){
    if(!current || otherModal())return;
    if(e.key==='Escape'){e.preventDefault();close();return;}
    if(e.key==='ArrowUp'||e.key==='ArrowDown'){e.preventDefault();step(e.key==='ArrowUp'?-1:1);return;}
    if(e.key!=='Tab')return;
    const focusables=[...id('sheet').querySelectorAll('button:not(:disabled),a[href],canvas[tabindex="0"]')].filter(el=>el.checkVisibility());
    const first=focusables[0],last=focusables.at(-1);
    if(e.shiftKey && document.activeElement===first){e.preventDefault();last?.focus();}
    if(!e.shiftKey && document.activeElement===last){e.preventDefault();first?.focus();}
  }
  function setMode(next){
    mode=next; id('kModeSeg').dataset.mode=next;
    id('kModeSeg').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===next)));
    chart?.setMode(next);
  }
  function step(delta){
    const idx=items.findIndex(s=>codeOf(s)===codeOf(current)), next=items[idx+delta];
    if(next)open(codeOf(next),items,{book});
  }
  function position(){return (book==='legacy'?current?.legacy_position:current?.ai_position)||null;}
  function open(symbol, list=[], options={}){
    const code=String(symbol);if(!/^[0-9]{6}$/.test(code))return;
    mount();
    if(!current){focusBack=document.activeElement;savedOverflow=document.body.style.overflow;document.body.style.overflow='hidden';}
    abort?.abort();abort=new AbortController();const requestToken=++token;
    chart?.destroy();chart=null;ref=null;history=null;rows=[];refLoading=true;
    const workspace=window.WealthApp?.workspace?.stocks || [];
    const rawList=Array.isArray(list)?list:[];
    const seen=new Set();
    items=rawList.filter(s=>/^[0-9]{6}$/.test(codeOf(s))&&!seen.has(codeOf(s))&&seen.add(codeOf(s)));
    const own=items.find(s=>codeOf(s)===code);
    current={...own,...workspace.find(s=>codeOf(s)===code),symbol:code};
    if(!own)items.push(current);
    book=options.book==='legacy' || own?.book==='legacy'?'legacy':'ai';
    document.body.classList.add('qd-drawer-open');
    id('sheet').inert=false;id('sheet').setAttribute('aria-hidden','false');
    id('sheet').dataset.symbol=code;id('sheetBackdrop').classList.add('open');id('sheet').classList.add('open');
    id('sheet').querySelector('.qd-sheet-scroll').scrollTop=0;id('sClose').focus({preventScroll:true});
    setTab('trend');setMode('candle');id('sName').textContent=current.name||code;id('sCode').textContent=code;
    id('sBook').textContent=position()?(book==='legacy'?'旧策略纸面持仓':'AI 纸面持仓'):current.candidate?'策略候选':'个股观察';
    const idx=items.findIndex(s=>codeOf(s)===code);
    id('sNavState').textContent='当前列表　'+(idx+1)+' / '+items.length;
    id('sPrev').disabled=idx<=0;id('sNext').disabled=idx>=items.length-1;
    id('kCount').textContent='正在读取日线';id('kHud').textContent='正在读取开高低收与成交量';
    const canvas=id('kCanvas');canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
    updateHeader(null,null);details();map();
    const get=async route=>{const r=await fetch(route+'?symbol='+code,{signal:abort.signal,cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);return r.json();};
    get('/api/ohlcv').then(result=>{
      if(token!==requestToken)return;
      history=result;
      rows=(result.price_basis==='raw' && Array.isArray(result.rows)?result.rows:[]).filter(row=>Array.isArray(row)&&row.length===6&&
        typeof row[0]==='string'&&row.slice(1).every(finite)&&row[5]>=0&&row[1]>0&&row[2]>=Math.max(row[1],row[4])&&row[3]>0&&row[3]<=Math.min(row[1],row[4]));
      buildChart();map();
    }).catch(err=>{
      if(token!==requestToken || err.name==='AbortError')return;
      history={warnings:['日线读取失败，'+err.message]};buildChart();map();
    });
    get('/api/reference').then(result=>{
      if(token!==requestToken)return;ref=result;refLoading=false;
      chart?.setLevels(levels());map();
    }).catch(err=>{
      if(token!==requestToken || err.name==='AbortError')return;
      ref={available:false,warnings:['参考价读取失败，'+err.message]};refLoading=false;map();
    });
  }
  function close(){
    if(!current)return;
    token++;abort?.abort();chart?.destroy();chart=null;current=null;
    id('sheet').classList.remove('open');id('sheetBackdrop').classList.remove('open');
    document.body.classList.remove('qd-drawer-open','qd-inspecting');
    id('sheet').inert=true;id('sheet').setAttribute('aria-hidden','true');document.body.style.overflow=savedOverflow;
    if(focusBack?.isConnected)focusBack.focus({preventScroll:true});focusBack=null;
  }
  function source(){
    const e=current.earnings||{},v=current.valuation||{},p=position();
    const facts=[
      (current.name||codeOf(current))+' · '+codeOf(current),
      '收盘资料日 '+(current.price_date||'—')+'；账本估值日 '+(p?.as_of||'—'),
      '原始日线 '+(history?.source||current.daily_source||'—')+'；整表价格口径 '+(history?.price_basis||'—')+'，成交量沿用原文件 volume 的数值。移动均线为完整 5 或 20 根收盘价的算术平均。',
      '估值 '+(v.source||'—')+' · '+(v.trade_date||'—')+'；PE 定义 '+(v.pe_semantics||'—'),
      '市盈率 '+fmt(v.pe)+' 倍；市净率 '+fmt(v.pb)+' 倍；总市值 '+fmt(v.total_mv_yi)+' 亿元；换手率 '+(finite(v.turnover_rate)?fmt(v.turnover_rate)+'%':'—'),
      '财务 '+(e.source||'—')+'；公告 '+(e.announcement_date||'—')+'；'+(e.period_verified?e.period_label+' 累计已核报告期':'文件标注 2026Q2，报告期未独立核实'),
      window.WealthResearch.source(current),
      '行业 '+(e.industry||current.candidate_info?.industry||'—')+'；营收同比 '+percent(e.rev_yoy)+'；净利同比 '+percent(e.profit_yoy)+'；净资产收益率 '+percent(e.roe)+'；毛利率 '+percent(e.gross_margin),
      '工作台 '+(ref?.source||'—')+'；'+(ref?.message||'—')+'；参考日 '+(ref?.as_of||'—')+'，有效至 '+(ref?.valid_until||'—'),
      '算法确认单点 '+fmt(ref?.available?ref.buy:null)+' 元；买入研究区间下沿 '+fmt(ref?.available?ref.buy_low:null)+' 元、上沿 '+fmt(ref?.available?ref.buy_high:null)+' 元。区间是算法观察范围，不等于满足买入条件。',
      'AI 数字止损 '+(current.stop_source||'—')+'；'+(current.stop_status||'—'),
      ...(history?.warnings||[]),...(ref?.warnings||[])
    ];
    const candidate=current.candidate_info;
    if(candidate)facts.push('候选批次 '+(candidate.source||'—')+'；跟踪 '+(window.WealthApp?.workspace?.candidates?.tracking_source||'—')+'；信号资料日取批次行 data_latest_date。每个窗口从目标日原始收盘到第 N 个后续日线收盘，已存小数收益换算成百分比。');
    window.WealthApp?.sources?.(facts.join('\n'));
  }
  const styleColors={cost:'#9674c5',stop:'#16997d',buy:'#ce8a24',upper:'#408acb',aiStop:'#cb6696'};
  function levels(){
    const p=position(), calc=ref?.available===true && ref.reference_current===true && ref.price_basis==='raw';
    return [
      ['cost',book==='legacy'?'旧账成本':'持仓成本',p?.cost],
      ['stop','退出参考',calc?ref.stop:null],
      ['buy','买点确认',calc?ref.buy:null],
      ['upper','上方观察',calc?ref.upper:null],
      ['aiStop','AI 止损',book==='ai'?current?.stop_price:null]
    ].filter(([, ,v])=>finite(v)&&v>0).map(([key,label,value])=>({key,label,value,color:styleColors[key]}));
  }
  function updateHeader(row,i){
    const p=position(),currentDay=current.price_date||'—';
    id('sPrice').textContent=fmt(row?row[4]:current.price);
    let delta=null, deltaText='';
    if(row){
      const prior=i>0?rows[i-1][4]:null;
      delta=finite(prior)&&prior>0?(row[4]/prior-1)*100:null;
      deltaText='当日 '+percent(delta);
      id('sSub').textContent=row[0]+' · 历史收盘';
    }else{
      delta=p?p.pnl_pct:current.pct_chg;
      deltaText=p?'持仓 '+percent(delta):'当日 '+percent(delta);
      id('sSub').textContent='收盘 '+currentDay+(p?'　'+(book==='legacy'?'旧账':'AI 账')+'浮盈亏 '+signed(p.pnl)+' 元':'');
    }
    const badge=id('sDelta');badge.textContent=deltaText;
    badge.className=finite(delta)?(delta>0?'is-up':delta<0?'is-down':''):'';
  }
  function hud(row){
    if(!row){id('kHud').textContent='本地尚无完整日线';return;}
    const labels=['开','高','低','收','量'];
    id('kHud').innerHTML='<b>'+escape(row[0])+'</b>'+labels.map((label,i)=>'<span>'+label+' <strong>'+fmt(row[i+1],i===4?0:2)+'</strong></span>').join('');
  }
  function buildChart(){
    chart?.destroy();
    const last=rows.at(-1);
    hud(last);
    id('kCount').textContent=rows.length?rows.length+' 个交易日 · 截至 '+last[0]:'暂无完整日线';
    chart=window.WealthCharts.candles(id('kCanvas'),{rows,levels:levels(),mode,emptyText:'暂无可绘制的完整日线',onHover:(row,i)=>{
      if(!current)return;hud(row||last);updateHeader(row,i);
    }});
  }
  function map(){
    const points=levels();
    const quote=finite(current.price)?current.price:null;
    if(quote!==null)points.push({key:'now',label:'现价',value:quote,color:'var(--ink)'});
    if(rows.length){
      points.push({key:'min',label:'区间低',value:Math.min(...rows.map(r=>r[3])),color:'var(--text-3)'});
      points.push({key:'max',label:'区间高',value:Math.max(...rows.map(r=>r[2])),color:'var(--text-3)'});
    }
    if(!points.length){id('sSpectrum').textContent='暂无可用价位';return;}
    const vals=points.map(p=>p.value),min=Math.min(...vals),max=Math.max(...vals);
    const pos=v=>max===min?50:3+(v-min)/(max-min)*94;
    const sorted=points.toSorted?points.toSorted((a,b)=>a.value-b.value):[...points].sort((a,b)=>a.value-b.value);
    const pins=sorted.filter(p=>p.key!=='now').map(p=>'<i class="qd-pin" style="left:'+pos(p.value)+'%;--pin:'+p.color+'" title="'+p.label+' '+fmt(p.value)+'"></i>').join('');
    const now=quote!==null?'<i class="qd-current" style="left:'+pos(quote)+'%" title="当前收盘价 '+fmt(quote)+'"></i>':'';
    const labels=sorted.map(p=>'<span class="'+(p.key==='now'?'qd-current-lbl':'')+'"><i style="background:'+p.color+'"></i>'+p.label+' <b>'+fmt(p.value)+'</b></span>').join('');
    id('sSpectrum').innerHTML='<div class="qd-map-scale">'+pins+now+'</div><div class="qd-map-values">'+labels+'</div>'+
      (refLoading?'<small class="qd-map-state">正在读取算法参考位</small>':ref?.available!==true?'<small class="qd-map-state">当前无有效算法参考位</small>':
      ref.entry_setup_available===false?'<small class="qd-map-state">当前未满足算法买入条件</small>':'');
  }
  function tracking(){
    const box=id('sTracking'),candidate=current.candidate_info;
    box.hidden=!candidate;
    if(!candidate){box.replaceChildren();return;}
    const record=candidate.tracking||{}, windows=record.horizons||{};
    const stateLabel=s=>({pending:'待到期',mature:'已到期',complete:'全部到期',partial:'部分到期',blocked:'数据受限',missing:'资料缺失'}[s]??(s||'—'));
    const metrics=[
      ['批次日',candidate.as_of],['信号资料日',candidate.data_date],['批次排名',fmt(candidate.rank,0)],
      ['策略分数',fmt(candidate.score)],['策略',candidate.strategy_label],['跟踪起价',fmt(record.entry_price)]
    ].map(([label,value])=>'<span>'+label+' <b>'+escape(value||'—')+'</b></span>').join('');
    const terms=Object.entries(windows).sort(([a],[b])=>parseInt(a,10)-parseInt(b,10));
    const windowCells=terms.map(([term,w])=>{
      const label=/^(\d+)d$/.exec(term)?.[1], gain=w?.return;
      const style=finite(gain)?(gain<0?'down':gain>0?'up':''):'';
      return '<div><b>'+escape(label?label+' 日':term)+'</b><span>'+escape(stateLabel(w?.status))+'</span><strong class="'+style+'">'+(finite(gain)?percent(gain*100):'—')+'</strong><small>'+escape(w?.as_of||'—')+'</small></div>';
    }).join('');
    box.innerHTML='<div class="qd-inner-title"><h3>每日候选跟踪</h3><small>'+escape(stateLabel(record.sample_status))+'</small></div><div class="qd-tracking-meta">'+metrics+'</div>'+
      '<p>'+escape(candidate.reason||'尚无已保存的策略理由')+'</p><div class="qd-windows">'+(windowCells||'暂无跟踪窗口')+'</div>';
  }
  function setTab(key){
    for(const button of document.querySelectorAll('[data-detail-tab]'))button.setAttribute('aria-selected',String(button.dataset.detailTab===key));
    for(const k of ['trend','facts','news'])id('v6Panel_'+k).hidden=k!==key;
    id('sheet').querySelector('.qd-sheet-scroll').scrollTop=0;
    if(key==='trend')requestAnimationFrame(()=>chart?.redraw());
  }
  function details(){
    const v=current.valuation||{},e=current.earnings||{},p=position();
    const row=(glyph,h,t,sub,key)=>'<article class="qd-insight '+key+'"><span class="qd-insight-title"><i>'+icon(glyph)+'</i>'+h+
      '</span><strong>'+t+'</strong><span>'+sub+'</span></article>';
    const profit=e.profit_yoy,roe=e.roe;
    const peText=finite(v.pe)?(v.pe>0?'市盈率 '+fmt(v.pe,1)+' 倍':v.pe<0?'市盈率为负':'市盈率为零'):'市盈率 —';
    const earningsText=finite(profit)?'净利'+(profit>=0?'增长 ':'下降 ')+fmt(Math.abs(profit),1)+'%':'净利同比 —';
    const roeText=finite(roe)?'每百元'+(roe<0?'亏 ':'赚 ')+fmt(Math.abs(roe))+' 元':'净资产收益 —';
    const d=current.dividend||{};
    id('sInsights').innerHTML=row('report',e.period_label==='2026Q3'?'2026Q3 · 累计三季报':'旧中报 · 期别未核',earningsText,'营收同比 '+percent(e.rev_yoy,1)+'<small class="v5-report-date">公告 '+escape(e.announcement_date||'—')+'</small><small class="v5-report-date">ROE '+percent(roe)+' · 累计</small>','earnings')+
      row('scale','估值 · 三段核验','同口径才合并',window.WealthResearch.bars(current),'valuation');
    id('sDividendPanel').innerHTML=row('trend','真实税前股息率',finite(d.yield_pct)?fmt(d.yield_pct)+'%':'—','近12个月现金分红 · 截至 '+escape(d.as_of||'—')+'<small class="v5-report-date">每股现金 ¥'+fmt(d.cash_per_share,4)+' · 不预测未来分红</small>','roe')+window.WealthResearch.dividends(current);
    const news=current.announcements||{};
    id('sAnnouncements').innerHTML='<div class="v6-notice"><b>官方公告 · 本地样本</b><p>'+escape(news.notice||'采样范围 10/25—11/25 · 非实时更新')+'</p></div>'+(news.sampled?(news.items||[]).map(a=>'<a class="v6-announcement" href="'+escape(a.pdf_url)+'" target="_blank" rel="noopener noreferrer"><div><time>'+escape(a.published_date)+'</time><span>'+escape(a.categories)+'</span></div><h4>'+escape(a.title)+'</h4><p>'+escape(a.notes)+'</p><small>官方 PDF ↗ · 未联网验证正文</small></a>').join(''):'<p class="v6-empty">暂未采样，不代表近期无公告。</p>');
    tracking();
    id('sThesisTitle').textContent=book==='legacy'?'旧策略持仓说明':'AI 持仓逻辑与认错纪律';
    const thesis=book==='legacy'?p?.thesis:current.thesis||p?.thesis;
    const discipline=book==='legacy'?p?.invalidation:current.invalidation||p?.invalidation;
    const text=v=>typeof v==='string'&&v.trim()?escape(v):'—';
    id('sThesis').innerHTML='<p><b>持仓逻辑</b>'+text(thesis)+'</p><p><b>认错纪律</b>'+text(discipline)+'</p>';
  }
  window.WealthDrawer={open,close,redraw:()=>chart?.redraw()};
})();
