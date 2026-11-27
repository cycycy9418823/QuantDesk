/* v6.1: local explanations only. No requests, chart handlers or financial calculations. */
(() => {
  'use strict';
  const entries={
    interval_return:['区间收益','选定区间内，资产相对起点的变化比例。','先看起止日，再看中间经历的起伏。','区间随缩放改变，纸面收益不等于实盘。'],
    excess_return:['相对沪深300','同一有效区间，AI账收益率减去沪深300收益率。','差值是百分点，不是盈利金额增幅。','价格指数与有限样本不能证明长期优势。'],
    cash_buffer:['现金缓冲','纸面账中未投入股票的现金及其占比。','结合总资产、持仓和资料日看余量。','现金比例没有通用最优值，不是买卖指令。'],
    daily_pipeline:['今日五节点','快照、早午盘、教练与晚间复盘的记录状态。','先看日期，再看各节点是否已保存。','有记录不代表内容正确或已经成交。'],
    attention:['最需要留意的持仓','按当前已存浮亏或净利下降信息选出的观察入口。','点开查看成本、财报期别和原持仓逻辑。','这是提示复查的排序，不是卖出信号或风险定论。'],
    portfolio_health:['收益与仓位健康','把现金比例与当前持仓浮盈亏放在一起看。','先核对账本和日期，再看各项持仓。','名称里的健康不等于完整风险评估。'],
    unrealized_ratio:['浮盈亏金额比','当前持仓浮盈总额除以浮亏绝对总额。','分母很小时比值会很大，须看实际金额。','不含已平仓收益，也不是历史胜率。'],
    paper_plan:['纸面次日开盘计划·未成交','已保存、拟在下一开盘时点处理的纸面意图。','核对适用日期、标的、股数和理由。','动作与理由可能矛盾，计划不等于成交。'],
    earnings_q3:['累计净利同比（如2026Q3）','财报所示累计期净利润相对上年同期的变化。','2026Q3为1—9月累计，先核期别与公告日。','累计并非单季，低基数会放大同比。'],
    dividend_ttm:['税前TTM股息率','近12个月每股现金分红除以标注日收盘价。','同时核对除息明细、分母日和税前口径。','过去分红不承诺未来，除息会影响价格。'],
    cost_position:['成本上方 / 下方','当前标注价格与这本纸面账持仓成本的相对位置。','结合成本记录、价格日期和浮盈亏金额看。','成本属于自己的账本，不等于公司的合理价值。'],
    exit_reference:['退出参考 / 触退出参考','退出参考是原研究标注价位；带“触”表示已到达。','先核对参考位来源、日期和适用条件。','参考位未验证，不保证成交，也不是自动卖出指令。'],
    compare_selected:['对比已选','把所选2～4只股票按同一组展示字段并排看。','同时核对各列的财报期、行情日与估值口径。','数值大小不等于优劣排序；缺项与不同口径不能硬比较。'],
    legacy_book:['旧策略纸面账对照','与AI蓝筹账相互独立的一本已保存纸面账。','分别看两本账的资金、持仓和估值日期。','不能把两本账合成同一个账户，也不是真实券商资产。'],
    moving_average:['MA5 / MA20','近5个或20个有效交易日收盘价的平均线。','观察价格在不同时间尺度上的变化。','均线有滞后性，不保证后续方向。'],
    price_map:['价位一眼地图','将现价、持仓成本与研究参考位放在同一轴。','买点确认、退出参考和成本各有不同含义。','价位不是路径预测，仍须满足原研究条件。'],
    roe:['ROE · 净资产收益率','财报期内利润相对股东净资产的比例。','先对齐期别、净资产口径及是否年化。','高ROE可能来自负债或净资产缩小。'],
    pb:['PB · 市净率','股价相对每股净资产的倍数。','对齐分母和日期，再看行业与资产质量。','低PB不一定便宜，不同分母不能硬拼。'],
    pe:['PE · 市盈率','股价相对每股盈利的倍数。','区分历史、H1×2近似与动态PE。','低PE不一定便宜，亏损或异口径会失真。'],
    valuation_seams:['三段估值核验与接缝','检查历史、派生与快照交界的数值和口径。','同时看来源、覆盖日期与接缝跳变。','数值连续不证明同口径，五年分位暂留空。'],
    preferred_dividend:['优先股股息提醒','公告可能是在向优先股股东支付股息。','先辨认证券类别与分红对象。','不能把优先股股息计入普通A股股息率。'],
    holding_diagnosis:['持仓三维体检','所选日期的财报、成本位置及涨跌等记录。','核对资料日，并与累计浮盈分开理解。','观察维度不是健康评分或收益预测。'],
    return_drawdown:['累计收益 / 回撤水位','收益对比初始金额，回撤对比此前最高值。','一个看涨了多少，一个看从高处退了多少。','缩放不重设基准或峰值，代理研究非实盘。'],
    cagr:['CAGR · 年化收益','把累计变化折算为每年复合增长的等效速度。','连同样本长度、费用和最大回撤一起看。','不代表每年收益，长期平均会掩盖波动。'],
    max_drawdown:['最大回撤','观察期内，从已有高点到后续低点的最大跌幅。','核对峰谷日期，了解曾经历的路径风险。','历史回撤不是未来损失上限，也未必修复。'],
    matured_sample:['可信已到期样本','已到观察期限、且记录可核验的模型判断。','先看样本量与规则，排除尚未到期项目。','可信是可核验，不代表小样本预测可靠。']
  };
  const glossary=Object.freeze(Object.fromEntries(Object.entries(entries).map(([key,[title,what,how,caution]])=>[key,Object.freeze({title,what,how,caution})])));
  // Only labels are annotated. No canvas or chart container is a tooltip target.
  const bindings=[
    ['#heroReturn','interval_return'],['#heroAlpha','excess_return'],
    ['.wp-step-text > strong','daily_pipeline'],
    ['#storyCards .story-card:nth-child(1) .story-k-left > span','attention'],
    ['#storyCards .story-card:nth-child(3) .story-k-left > span','portfolio_health'],
    ['#storyCards .story-card:nth-child(3) .story-v','cash_buffer'],
    ['#storyCards .story-card:nth-child(3) .story-d','unrealized_ratio'],
    ['#coachPlan h2, #storyCards .story-card:nth-child(2) .story-k-left > span','paper_plan'],
    ['.sig-row .sig-item:nth-child(1), .earnings .qd-insight-title, .wp-diag-cols > span:nth-child(2), #compareBody tbody tr:nth-child(7) th','earnings_q3'],
    ['.sig-row .sig-item:nth-child(2), #sDividendPanel .qd-insight-title, .v5-dividends summary, .v5-yield, #compareBody tbody tr:nth-child(5) th','dividend_ttm'],
    ['.sig-row .sig-item:nth-child(3)','cost_position',el=>!el.textContent.includes('触退出参考')],
    ['.sig-row .sig-item:nth-child(3)','exit_reference',el=>el.textContent.includes('触退出参考')],
    ['#compareOpen','compare_selected'],['.legacy-title','legacy_book'],
    ['.qd-chart-key > span:not(.qd-count)','moving_average'],['.qd-map h3','price_map'],
    ['.qd-map-values > span','price_map',el=>!el.textContent.includes('成本')&&!el.textContent.includes('退出')],
    ['.qd-map-values > span','cost_position',el=>el.textContent.includes('成本')],
    ['.qd-map-values > span','exit_reference',el=>el.textContent.includes('退出')],
    ['.earnings .v5-report-date, #compareBody tbody tr:nth-child(8) th','roe',el=>el.textContent.includes('ROE')],
    ['.v6-metric:first-child > b, #compareBody tbody tr:nth-child(4) th','pb'],
    ['.v6-metric:last-child > b, #compareBody tbody tr:nth-child(3) th','pe'],
    ['.valuation > .qd-insight-title, .v6-seams > b','valuation_seams'],
    ['.v6-announcement h4, .v6-announcement p','preferred_dividend',el=>el.textContent.includes('优先股')],
    ['.wp-section-title h2','holding_diagnosis',el=>el.textContent.includes('持仓三维体检')],
    ['[data-etf-mode]','return_drawdown'],['.v5-result-card > p','cagr'],
    ['.v5-result-card footer > span:first-child','max_drawdown'],
    ['.wp-metric > div > span','matured_sample',el=>el.textContent==='可信已到期样本']
  ];
  const storageKey='quantdesk.wealth.glossary.enabled';
  let enabled=true, pending=null, active=null, timer=0, returnFocus=null;
  try{enabled=localStorage.getItem(storageKey)!=='off';}catch{}
  const card=document.createElement('div');card.id='glossaryTip';card.className='qd-glossary-tip';
  card.setAttribute('role','tooltip');card.setAttribute('popover','manual');card.hidden=true;
  const guide=document.createElement('dialog');guide.id='guideDialog';guide.className='qd-guide';guide.setAttribute('aria-labelledby','guideTitle');
  guide.innerHTML='<header><div><span class="qd-guide-eyebrow">QUANTDESK · 使用指引</span><h2 id="guideTitle">每天5分钟 · 怎么看这套面板</h2></div><button id="guideClose" class="close-btn" aria-label="关闭使用指引">×</button></header>'+
    '<div class="qd-guide-setting"><div><strong>鼠标长停白话解释：<span id="tipState">开启</span></strong><p>觉得熟了随时一键关闭</p></div><label class="qd-tip-switch"><input id="tipToggle" type="checkbox" role="switch" aria-label="鼠标长停白话解释"><span aria-hidden="true"></span></label></div>'+
    '<div class="qd-guide-grid">'+[
      ['01','财富总览','2分钟','先看资产曲线与现金垫子，再扫一遍今日五节点。','看3张故事卡与次日纸面计划，分清待执行和已成交。'],
      ['02','个股抽屉与横向对比','1～2分钟','点任意股票，看「走势与计划 / 财报与估值 / 分红与公告」三栏。','勾选2～4只，可并排核对指标与资料日期。'],
      ['03','每日复盘','1分钟','切换历史日期，看当天保存的持仓浮盈柱图与三维体检。','再读核心洞察，留意当时的资料与判断边界。'],
      ['04','稳健研究 & 对错周报','每周1次','看看九年半的收益与回撤，把时间拉长一点。','去对错页核对AI过往判断，再读已保存的周报。']
    ].map(([n,title,time,first,second])=>`<article><span class="qd-guide-number">${n}</span><h3>${title}<small>${time}</small></h3><p>${first}</p><p>${second}</p></article>`).join('')+'</div>'+
    '<footer>所有解释均为指标含义科普，纸面 · 未验证 · 只读，不构成投资建议。</footer>';
  document.body.append(card,guide);

  function annotate(root){
    if(!(root instanceof Element)||root===card||root===guide||card.contains(root)||guide.contains(root))return;
    for(const [selector,key,accept] of bindings){
      const nodes=[...(root.matches(selector)?[root]:[]),...root.querySelectorAll(selector)];
      for(const el of nodes){
        if(accept&&!accept(el))continue;
        el.dataset.tip=key;
        // Keep existing button focus behaviour; ordinary text labels also support Tab.
        if(!el.closest('button,a[href],summary,input,select,textarea')&&!el.hasAttribute('tabindex')){el.tabIndex=0;el.dataset.tipTab='true';}
      }
    }
  }
  function restoreDescription(){
    if(!active)return;
    const ids=(active.getAttribute('aria-describedby')||'').split(/\s+/).filter(x=>x&&x!==card.id);
    if(ids.length)active.setAttribute('aria-describedby',ids.join(' '));else active.removeAttribute('aria-describedby');
    active=null;
  }
  function dismiss(){
    clearTimeout(timer);timer=0;pending=null;restoreDescription();
    if(card.matches(':popover-open'))card.hidePopover();card.hidden=true;
  }
  function place(el){
    const a=el.getBoundingClientRect(),w=card.offsetWidth,h=card.offsetHeight,gap=10,edge=12;
    const left=Math.max(edge,Math.min(a.left,innerWidth-w-edge));
    let top=a.bottom+gap;
    if(top+h>innerHeight-edge)top=a.top-h-gap;
    top=Math.max(edge,Math.min(top,innerHeight-h-edge));
    card.style.left=left+'px';card.style.top=top+'px';
  }
  function show(el){
    if(!enabled||pending!==el||!el.isConnected||!el.checkVisibility())return;
    const entry=glossary[el.dataset.tip];if(!entry)return;
    restoreDescription();card.replaceChildren();
    const heading=document.createElement('strong');heading.textContent=entry.title;card.append(heading);
    for(const [label,field] of [['是什么','what'],['怎么看','how'],['避坑提醒','caution']]){
      const p=document.createElement('p'),b=document.createElement('b');b.textContent=label+' ';p.append(b,document.createTextNode(entry[field]));card.append(p);
    }
    card.hidden=false;card.showPopover();active=el;
    const ids=new Set((el.getAttribute('aria-describedby')||'').split(/\s+/).filter(Boolean));ids.add(card.id);el.setAttribute('aria-describedby',[...ids].join(' '));
    place(el);
  }
  function target(node){
    if(!(node instanceof Element)||node.closest('canvas'))return null;
    const el=node.closest('[data-tip]');return el&&glossary[el.dataset.tip]?el:null;
  }
  function schedule(el){
    if(!enabled||!el||pending===el||active===el)return;
    dismiss();pending=el;timer=setTimeout(()=>show(el),350);
  }
  function setEnabled(next,save=true){
    enabled=!!next;dismiss();document.body.classList.toggle('qd-tips-enabled',enabled);
    document.getElementById('tipToggle').checked=enabled;document.getElementById('tipState').textContent=enabled?'开启':'关闭';
    document.querySelectorAll('[data-tip-tab]').forEach(el=>el.tabIndex=enabled?0:-1);
    if(save){try{localStorage.setItem(storageKey,enabled?'on':'off');}catch{}}
  }
  document.addEventListener('pointerover',e=>{if(e.pointerType!=='touch')schedule(target(e.target));},{passive:true});
  document.addEventListener('pointerout',e=>{
    const el=target(e.target);if(el&&el!==target(e.relatedTarget)&&(el===pending||el===active))dismiss();
  },{passive:true});
  document.addEventListener('focusin',e=>schedule(target(e.target)||e.target.querySelector?.('[data-tip]')));
  document.addEventListener('focusout',()=>{if(active||pending)dismiss();});
  document.addEventListener('pointerdown',dismiss,{capture:true,passive:true});
  document.addEventListener('scroll',dismiss,{capture:true,passive:true});
  window.addEventListener('resize',dismiss,{passive:true});
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&(pending||active)){dismiss();e.stopImmediatePropagation();e.preventDefault();}
  },true);
  function closeGuide(){if(guide.open)guide.close();}
  function openGuide(){dismiss();returnFocus=document.activeElement;guide.showModal();document.getElementById('guideClose').focus();}
  document.getElementById('guideBtn').addEventListener('click',openGuide);
  document.getElementById('guideClose').addEventListener('click',closeGuide);
  document.getElementById('tipToggle').addEventListener('change',e=>setEnabled(e.target.checked));
  guide.addEventListener('close',()=>{dismiss();if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true});});
  guide.addEventListener('click',e=>{if(e.target===guide){const r=guide.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeGuide();}});
  guide.addEventListener('keydown',e=>{
    if(e.key!=='Tab')return;
    const els=[...guide.querySelectorAll('button,input')].filter(el=>!el.disabled&&el.checkVisibility());
    if(e.shiftKey&&document.activeElement===els[0]){e.preventDefault();els.at(-1).focus();}
    else if(!e.shiftKey&&document.activeElement===els.at(-1)){e.preventDefault();els[0].focus();}
  });
  annotate(document.body);setEnabled(enabled,false);
  new MutationObserver(records=>{
    for(const record of records){
      if(record.target instanceof Element&&record.target.closest('canvas,#kHud,#etfHover,.qd-chart-cross,.v5-etf-cross,#glossaryTip,#guideDialog'))continue;
      for(const node of record.addedNodes){
      if(node.nodeType!==1)continue;annotate(node);
      if(!enabled){if(node.matches?.('[data-tip-tab]'))node.tabIndex=-1;node.querySelectorAll?.('[data-tip-tab]').forEach(el=>el.tabIndex=-1);}
      }
    }
    if((active&&!active.isConnected)||(pending&&!pending.isConnected))dismiss();
  }).observe(document.body,{childList:true,subtree:true});
  window.WealthGlossary=Object.freeze({entries:glossary,bindings:bindings.map(([selector,key])=>Object.freeze({selector,key})),dismiss,setEnabled,get enabled(){return enabled;}});
})();
