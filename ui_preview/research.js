(() => {
  'use strict';
  const finite=Number.isFinite, $=id=>document.getElementById(id);
  const fmt=(v,d=2)=>finite(v)?v.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const pct=v=>finite(v)?`${v>0?'+':''}${fmt(v)}%`:'—';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const selected=new Set();
  const colors=['#c68738','#18a58b','#8b7dd8','#4c8ed8'];
  let chart=null, data=null, mode='return', visible=new Set(['X','Z','Y','H00300']);
  function bars(s){
    const p=s.percentiles||{},v=s.valuation||{},segments=p.segments||{};
    if(!p.as_of)return '<p>离线估值缓存未与最新快照匹配，分位留空。</p>';
    const stats=['pb','pe'].map(k=>{const r=p[k]||{};return `<div class="v6-metric"><b>${k.toUpperCase()} ${fmt(v[k])} 倍</b><span>五年同口径分位 —</span><p>${esc(r.join_note)}</p><small>最新快照段位置 ${fmt(r.segment_percentile,1)}% · ${r.count??0}/${r.expected_days??0} 日<br>${esc(r.first_date)} ～ ${esc(r.last_date)}（不是五年分位）</small></div>`;}).join('');
    const blocks=Object.values(segments).map(seg=>`<div class="v6-segment"><b>${esc(seg.label)} <span>${esc(seg.coverage_start)} — ${esc(seg.coverage_end)}</span></b><p>PB ${seg.pb.count}/${seg.pb.expected_days} 日 (${fmt(seg.pb.coverage_pct,1)}%) · PE ${seg.pe.count}/${seg.pe.expected_days} 日 (${fmt(seg.pe.coverage_pct,1)}%)</p><small>${esc(seg.pe_semantics)}${seg.label.includes('派生')?'；固定7/2股数锚点，不进入正式计算':''}</small></div>`).join('');
    const seams=(p.seams||[]).map(x=>`<span>${x.left_date.slice(5)} → ${x.right_date.slice(5)}：PB 去除价格影响后 ${pct(x.metrics.pb.price_normalized_jump_pct)}；PE ${pct(x.metrics.pe.price_normalized_jump_pct)}</span>`).join('');
    return `<div class="v6-metric-grid">${stats}</div><div class="v6-segments">${blocks}</div><div class="v6-seams"><b>两处接缝核验</b>${seams}<small>2%仅为跳变提示线。数值连续 ≠ 分母口径已经证实；不强行补线。</small></div>`;
  }
  function dividends(s){
    const d=s.dividend||{};
    if(!finite(d.yield_pct))return '<div class="v5-dividends">近12个月现金分红明细 —</div>';
    return `<details class="v5-dividends"><summary>近12个月现金分红 · ${d.event_count} 笔 <span>展开除息明细</span></summary><p>${esc(d.window_start)} ～ ${esc(d.as_of)}；合计每股 ¥${fmt(d.cash_per_share,4)} / 分母收盘 ¥${fmt(d.denominator_close)}。税前，不预测未来分红。</p><table><thead><tr><th>除息日</th><th>分红所属期</th><th>每10股现金（税前）</th></tr></thead><tbody>${(d.events||[]).map(e=>`<tr><td>${esc(e.ex_dividend_date)}</td><td>${esc(e.report_period)}</td><td>¥${fmt(Number(e.gross_cash_per_10_shares_cny),4)}</td></tr>`).join('')}</tbody></table><small>${esc(d.issue||'已保存的逐笔现金分红记录；不含送转股价值。')}</small></details>`;
  }
  function source(s){
    const p=s.percentiles||{},d=s.dividend||{};
    return `股息率：税前TTM，窗口 ${d.window_start||'—'} 至 ${d.as_of||'—'}；每股现金 ${fmt(d.cash_per_share,4)} 元 / 当日收盘 ${fmt(d.denominator_close)} 元。来源 ${d.source||'—'}。\n估值：三段独立展示，五年合并分位留空。近段位置只比较同一快照段的有效正值；非正值、缺失、冲突日期剔除，不插值。最新快照段覆盖 ${p.pb?.first_date||'—'} 至 ${p.pb?.last_date||'—'}，PB ${p.pb?.count??0} 日、PE ${p.pe?.count??0} 日。历史PE未声明口径、派生PE为H1×2近似、最新PE为动态；禁止混算五年分位。PB派生分母证据不足，同样不强拼。\n近30日涨跌：近30个有效交易日原始收盘价变化（不含现金分红），${s.change_30d_from||'—'} 至 ${s.change_30d_to||'—'}。`;
  }
  async function mount(element){
    chart?.destroy();chart=null;
    element.innerHTML='<p class="empty-note">正在读取已冻结的九年半考试记录…</p>';
    try{
      if(!data){const r=await fetch('/api/etf-research',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);data=await r.json();}
      if(!data.available||data.series?.length!==4)throw Error('数据不完整');
      element.innerHTML=`<header class="v5-research-head"><div><span class="v5-eyebrow">LONG VIEW · 2016—2026</span><h1>把时间拉长，看看稳不稳。</h1><p>${esc(data.label)}</p></div><span class="v5-period">2016.12.19 — 2026.06.30<br><small>九年半 · 每套独立起步 ¥100,000</small></span></header>
      <section class="v5-research-chart"><div class="v5-chart-toolbar"><div class="v5-legends">${data.series.map((s,i)=>`<button data-etf-key="${s.key}" aria-pressed="${visible.has(s.key)}"><i style="background:${colors[i]}"></i>${esc(s.name)}</button>`).join('')}</div><div class="v5-switch"><button data-etf-mode="return" aria-pressed="${mode==='return'}">累计收益</button><button data-etf-mode="drawdown" aria-pressed="${mode==='drawdown'}">回撤水位</button></div></div><div id="etfHover" class="v5-chart-values"></div><div class="v5-etf-stage"><canvas id="etfCanvas" aria-label="九年半跨资产收益与回撤。滚轮缩放，拖拽平移，双击复原"></canvas><canvas class="v5-etf-cross" aria-hidden="true"></canvas></div><div class="v5-chart-foot"><span id="etfRange"></span><span>滚轮缩放 · 拖拽漫游 · 双击复原</span></div></section>
      <section class="v5-result-grid">${data.series.slice(0,3).map((s,i)=>{const m=s.metrics;return `<article class="v5-result-card"><div><i style="background:${colors[i]}"></i><h2>${esc(s.name)}</h2><span>${m.pass_both?'两项及格':'未及格'}</span></div><small>基准费用后期末金额</small><strong>¥${fmt(m.final_assets)}</strong><p>年化 ${fmt(m.cagr_pct)}%</p><footer><span>最大回撤<b>${fmt(m.max_drawdown_pct)}%</b></span><span>年均出手<b>${fmt(m.annual_average_trades)} 笔</b></span></footer><small class="v5-dd-dates">回撤 ${m.drawdown_peak_date} → ${m.drawdown_trough_date}</small></article>`;}).join('')}</section>
      <section class="v5-yearly"><div><h2>每一年，都摊开看</h2><span>同一窗口 · 基准费用列 · 收益率 %</span></div><table><thead><tr><th>年度</th>${data.series.map(s=>`<th>${esc(s.name)}</th>`).join('')}</tr></thead><tbody>${data.series[0].metrics.annual.filter(a=>a.period!=='2016起步段').map(a=>`<tr><th>${esc(a.period)}</th>${data.series.map(s=>{const n=s.metrics.annual.find(x=>x.period===a.period)?.return_pct;return `<td class="${n>0?'up':n<0?'down':''}">${pct(n)}</td>`;}).join('')}</tr>`).join('')}</tbody></table><p>${esc(data.note)} 沪深300同期最大回撤 ${fmt(data.series[3].metrics.max_drawdown_pct)}%，${data.series[3].metrics.drawdown_peak_date} → ${data.series[3].metrics.drawdown_trough_date}。及格线：期末超过10万元且最大回撤≤20%。</p><details><summary>ⓘ 口径与来源</summary><p>收益始终以初始10万元为基数；缩放不会改变收益基准。回撤相对本线全历史已达峰值，含初始10万元；不是缩放区间回撤。不重新模拟交易、不插值补造净值。基金年费与每单最低5元佣金均已在原考试基准列扣除，本页不二次扣费。${esc(data.bond_note)}。</p><p>${esc(data.source)}</p><small>原件 SHA-256：${esc(data.results_sha256)}</small></details></section>`;
      element.querySelectorAll('[data-etf-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.etfMode;element.querySelectorAll('[data-etf-mode]').forEach(n=>n.setAttribute('aria-pressed',String(n===b)));draw();});
      element.querySelectorAll('[data-etf-key]').forEach(b=>b.onclick=()=>{const k=b.dataset.etfKey;if(visible.has(k)&&visible.size===1)return;if(visible.has(k))visible.delete(k);else visible.add(k);b.setAttribute('aria-pressed',String(visible.has(k)));draw();});
      draw();
    }catch(err){element.innerHTML='<p class="empty-note">研究原件暂不可读：'+esc(err.message)+'。不生成替代成绩。</p>';}
  }
  function draw(){
    chart?.destroy();
    const canvas=$('etfCanvas'),series=data.series,points=series[0].points,col=mode==='return'?2:3;
    let cacheKey='',lastGeom=null;
    const overlay=canvas.parentElement.querySelector('.v5-etf-cross'),cross=overlay.getContext('2d');
    $('etfHover').innerHTML='<span id="etfHoverDate"></span>'+series.map((s,j)=>`<b style="color:${colors[j]}"><span id="etfValue${j}"></span><small id="etfAmount${j}"></small></b>`).join('');
    function hover(i){const at=i===null?points.length-1:i;
      $('etfHoverDate').textContent=points[at][0];series.forEach((s,j)=>{$('etfValue'+j).textContent=`${s.key==='H00300'?'沪深300':s.key} ${pct(s.points[at][col])}`;$('etfAmount'+j).textContent='¥'+fmt(s.points[at][1]);});
    }
    chart=window.WealthCharts.interactive(canvas,points.length,()=>0,f=>{
      const {ctx,c,width,height,L,R,T,B,left,count,hovered}=f;
      const dpr=devicePixelRatio||1,key=[left,count,width,height,dpr,c.ink,mode,[...visible].join(',')].join('|');
      if(key!==cacheKey){
        cacheKey=key;ctx.clearRect(0,0,width,height);
        const g=ctx;g.font='11px -apple-system,"Microsoft YaHei",sans-serif';g.textBaseline='middle';
        let lo=0,hi=0;for(const s of series)if(visible.has(s.key))for(let i=left;i<left+count;i++){lo=Math.min(lo,s.points[i][col]);hi=Math.max(hi,s.points[i][col]);}
        const pad=Math.max(2,(hi-lo)*.1);lo-=pad;hi+=pad;
        const x=i=>L+(i-left)/Math.max(1,count-1)*(R-L),y=v=>B-(v-lo)/(hi-lo)*(B-T);
        for(let j=0;j<5;j++){const v=lo+(hi-lo)*j/4,py=y(v);g.strokeStyle=c['canvas-grid'];g.beginPath();g.moveTo(L,py);g.lineTo(R,py);g.stroke();g.fillStyle=c['text-2'];g.textAlign='right';g.fillText(fmt(v,1)+'%',R+54,py);}
        for(let j=0;j<5;j++){const i=left+Math.round((count-1)*j/4);g.fillStyle=c['text-2'];g.textAlign=j===0?'left':j===4?'right':'center';g.fillText(points[i][0],x(i),B+23);}
        series.forEach((s,j)=>{if(!visible.has(s.key))return;g.strokeStyle=colors[j];g.lineWidth=s.key==='H00300'?1.4:2;g.lineJoin='round';g.beginPath();for(let i=left;i<left+count;i++){const px=x(i),py=y(s.points[i][col]);if(i===left)g.moveTo(px,py);else{const xp=x(i-1),yp=y(s.points[i-1][col]),mid=(px+xp)/2;g.bezierCurveTo(mid,yp,mid,py,px,py);}}g.stroke();});
        lastGeom={x,y};$('etfRange').textContent=`${points[left][0]} — ${points[left+count-1][0]} · ${count.toLocaleString()} 个交易日`;
      }
      if(overlay.width!==canvas.width||overlay.height!==canvas.height){overlay.width=canvas.width;overlay.height=canvas.height;}
      cross.setTransform(canvas.width/width,0,0,canvas.height/height,0,0);cross.clearRect(0,0,width,height);
      if(hovered!==null){const x=lastGeom.x(hovered);cross.strokeStyle=c.cross;cross.setLineDash([3,4]);cross.beginPath();cross.moveTo(x,T);cross.lineTo(x,B);cross.stroke();cross.setLineDash([]);series.forEach((s,j)=>{if(!visible.has(s.key))return;cross.beginPath();cross.arc(x,lastGeom.y(s.points[hovered][col]),3.5,0,Math.PI*2);cross.fillStyle=colors[j];cross.fill();});}
    },hover,{left:4,right:58,top:12,bottom:34,layered:true});hover(null);
  }
  function updateSelection(){
    document.querySelectorAll('[data-compare-select]').forEach(el=>{el.checked=selected.has(el.dataset.compareSelect);el.disabled=selected.size>=4&&!el.checked;});
    const pill=$('comparePill');pill.hidden=selected.size===0;$('compareOpen').textContent=`对比已选 (${selected.size} 只)`;$('compareOpen').disabled=selected.size<2;
  }
  function compare(){
    const stocks=window.WealthApp.workspace.stocks.filter(s=>selected.has(s.code));
    if(stocks.length<2||stocks.length>4)return;
    const val=(v,suffix='')=>finite(v)?fmt(v)+suffix:'—';
    const metric=[['现价',s=>val(s.price,' 元')],['一手金额 · 100股',s=>finite(s.price)?val(s.price*100,' 元'):'—'],
      ['PE · 动态/五年分位',s=>`${val(s.valuation?.pe)}<small>${val(s.percentiles?.pe?.percentile,'% 分位')} · 五年口径不一致，留空</small>`],
      ['PB · 5年分位',s=>`${val(s.valuation?.pb)}<small>${val(s.percentiles?.pb?.percentile,'% 分位')}</small>`],
      ['真实税前股息率',s=>`${val(s.dividend?.yield_pct,'%')}<small>${esc(s.dividend?.as_of||'未覆盖')}</small>`],
      ['2026Q3 累计营收同比',s=>s.earnings?.period_label==='2026Q3'?pct(s.earnings.rev_yoy):'—'],
      ['2026Q3 累计净利同比',s=>s.earnings?.period_label==='2026Q3'?pct(s.earnings.profit_yoy):'—'],
      ['ROE · 财报口径',s=>`${val(s.earnings?.roe,'%')}<small>${esc(s.earnings?.period_label||'—')}${s.earnings?.period_verified?' 累计':' · 期别未核'}</small>`],
      ['近30交易日涨跌',s=>`${pct(s.change_30d)}<small>${esc(s.change_30d_to||'—')} · 原始收盘</small>`],
      ['财报公告日',s=>esc(s.earnings?.announcement_date||'—')]];
    $('compareBody').innerHTML=`<table><thead><tr><th>放在一起，慢慢比较</th>${stocks.map(s=>`<th>${esc(s.name)}<small>${s.code} · 收盘 ${esc(s.price_date)}</small></th>`).join('')}</tr></thead><tbody>${metric.map(([label,get])=>`<tr><th>${label}</th>${stocks.map(s=>`<td>${get(s)}</td>`).join('')}</tr>`).join('')}</tbody></table><p class="v5-muted">股息率保留11月24日已核税前TTM；行情与估值各自标注资料日。PB/PE五年分位未满足同口径条件，均留空；抽屉展示三段覆盖与近段位置。研究输出 · 未验证。</p>`;
    $('compareDialog').showModal();$('compareClose').focus();
  }
  document.addEventListener('DOMContentLoaded',()=>{
    document.body.insertAdjacentHTML('beforeend','<div id="comparePill" class="v5-compare-pill" hidden><button id="compareOpen">对比已选</button><button id="compareClear" aria-label="清空已选股票">×</button></div><dialog id="compareDialog" class="v5-compare"><header><div><span class="v5-eyebrow">SIDE BY SIDE</span><h2>好公司，放在一起看。</h2></div><button id="compareClose" class="close-btn" aria-label="关闭对比">×</button></header><div id="compareBody"></div></dialog>');
    $('compareOpen').onclick=compare;$('compareClear').onclick=()=>{selected.clear();updateSelection();};$('compareClose').onclick=()=>$('compareDialog').close();
    $('compareDialog').addEventListener('click',e=>{if(e.target===$('compareDialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
    document.addEventListener('change',e=>{const el=e.target.closest('[data-compare-select]');if(!el)return;const code=el.dataset.compareSelect;if(el.checked&&selected.size<4)selected.add(code);else selected.delete(code);updateSelection();});
    new MutationObserver(updateSelection).observe($('assetList'),{childList:true});
    new MutationObserver(updateSelection).observe($('legacyList'),{childList:true});
  });
  window.WealthResearch={mount,bars,dividends,source,updateSelection};
})();
