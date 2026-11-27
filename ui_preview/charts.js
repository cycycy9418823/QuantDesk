/* Interactive, date-aligned local chart views. All data enters via the read-only API. */
(() => {
  'use strict';
  const finite = Number.isFinite;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const number = (value, digits = 2) => finite(value) ? value.toFixed(digits) : '—';
  const keys = ['ai', 'benchmark', 'strategy'];
  const percent = (a, b) => finite(a) && a > 0 && finite(b) ? (b / a - 1) * 100 : null;

  function palette(canvas) {
    const style = getComputedStyle(canvas);
    const tokens = ['ink', 'text-2', 'text-3', 'up', 'down', 'bench', 'ma5', 'ma20',
      'canvas-grid', 'cross', 'up-grad-top', 'down-grad-top', 'surface', 'surface-soft', 'border-strong'];
    return Object.fromEntries(tokens.map(token => [token, style.getPropertyValue('--' + token).trim()]));
  }

  function stroke(ctx, points, color, width = 1.5, dash = []) {
    ctx.beginPath();
    points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash); ctx.stroke(); ctx.setLineDash([]);
  }

  function dot(ctx, x, y, radius, color) {
    ctx.beginPath(); ctx.arc(x, y, radius, 0, 2 * Math.PI); ctx.fillStyle = color; ctx.fill();
  }

  // Both controls stay between adjacent observations; the curve passes through every close.
  function smoothLine(ctx, points, color, width, dash, base, fill) {
    if(!points.length)return;
    ctx.beginPath();ctx.moveTo(...points[0]);
    for(let i=1;i<points.length;i++) {
      const [x0,y0]=points[i-1], [x1,y1]=points[i], mid=(x0+x1)/2;
      ctx.bezierCurveTo(mid,y0,mid,y1,x1,y1);
    }
    ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.stroke();ctx.setLineDash([]);
    if(fill){ctx.lineTo(points.at(-1)[0],base);ctx.lineTo(points[0][0],base);ctx.closePath();ctx.fillStyle=fill;ctx.fill();}
  }

  function extent(values, padding, minSpan) {
    const low = Math.min(...values), high = Math.max(...values);
    const span = Math.max(high - low, minSpan);
    return [low - span * padding, high + span * padding];
  }

  function axis(ctx, color, value, x, y, align = 'left') {
    ctx.textAlign = align; ctx.fillStyle = color; ctx.fillText(value, x, y);
  }

  // One redraw per animation frame, including pointer events, wheel, resize and theme changes.
  function interactive(canvas, total, defaultStart, draw, onHover, margins) {
    const ctx = canvas.getContext('2d');
    const controller = new AbortController();
    const opts = {signal: controller.signal};
    let initialized = false, frame = 0, dead = false;
    let left = 0, count = total, pointer = null, drag = null, lastHover = undefined;
    let width = 0, height = 0, colors = null, dimensions = null;
    const limit = n => clamp(n, 0, Math.max(0, total - 1));
    function reset() {
      left = limit(defaultStart()); count = total - left;
      pointer = null; redraw();
    }
    function render() {
      frame = 0;
      if (dead) return;
      const rect = canvas.getBoundingClientRect();
      width = rect.width; height = rect.height;
      if (width < 2 || height < 2) return;
      const dpr = window.devicePixelRatio || 1;
      const wpx = Math.round(width * dpr), hpx = Math.round(height * dpr);
      if (canvas.width !== wpx || canvas.height !== hpx) { canvas.width = wpx; canvas.height = hpx; }
      ctx.setTransform(wpx / width, 0, 0, hpx / height, 0, 0);
      if(!margins.layered)ctx.clearRect(0, 0, width, height);
      ctx.font = '11px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
      ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.textBaseline = 'middle';
      colors ||= palette(canvas);
      const L = margins.left, R = Math.max(L + 1, width - margins.right);
      const T = margins.top, B = Math.max(T + 1, height - margins.bottom);
      dimensions = {L, R, T, B};
      let hovered = null;
      if (count && pointer && pointer.x >= L && pointer.x <= R && pointer.y >= T && pointer.y <= B) {
        const fraction=(pointer.x-L)/(R-L);
        const hit=margins.centerBars?Math.floor(fraction*count):Math.round(fraction*(count-1));
        hovered = left + clamp(hit, 0, count - 1);
      }
      draw({ctx, c:colors, width, height, L, R, T, B, left, count, hovered, pointer});
      if (hovered !== lastHover) { lastHover = hovered; onHover?.(hovered); }
      canvas.dataset.viewStart = String(left); canvas.dataset.viewCount = String(count);
    }
    function redraw() { if (!dead && !frame) frame = requestAnimationFrame(render); }
    const local = e => {
      const box = canvas.getBoundingClientRect();
      return {x:e.clientX - box.left, y:e.clientY - box.top};
    };
    function pan(next) { left = clamp(Math.round(next), 0, Math.max(0, total - count)); redraw(); }
    function zoom(steps, anchor) {
      if (!total) return;
      const next = clamp(count + steps, Math.min(7, total), total);
      if (next === count) return;
      const start = left + (count - next) * clamp(anchor, 0, 1);
      count = next; pan(start);
    }
    canvas.addEventListener('wheel', e => {
      if (!dimensions || !total || !e.deltaY) return;
      e.preventDefault();
      pointer = local(e);
      const factor = Math.min(0.65, Math.max(0.15, Math.abs(e.deltaY) * (e.deltaMode ? 0.055 : 0.0018)));
      zoom(Math.sign(e.deltaY) * Math.max(1, Math.round(count * factor)),
           (pointer.x - dimensions.L) / (dimensions.R - dimensions.L));
    }, {...opts, passive:false});
    canvas.addEventListener('pointerdown', e => {
      if (e.button !== 0 || !dimensions || !total) return;
      pointer = local(e); drag = {x:pointer.x, left, id:e.pointerId};
      canvas.setPointerCapture(e.pointerId); canvas.style.cursor = 'grabbing'; redraw();
    }, opts);
    canvas.addEventListener('pointermove', e => {
      if (drag && e.pointerId !== drag.id) return;
      pointer = local(e);
      if (drag && dimensions) pan(drag.left + (drag.x - pointer.x) * (count - 1) / (dimensions.R - dimensions.L));
      redraw();
    }, opts);
    function end(e) {
      if (!drag || drag.id !== e.pointerId) return;
      drag = null; canvas.style.cursor = 'grab';
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
      if (e.pointerType === 'touch' || e.type === 'pointercancel') pointer = null;
      redraw();
    }
    ['pointerup','pointercancel','lostpointercapture'].forEach(event => canvas.addEventListener(event, end, opts));
    canvas.addEventListener('pointerleave', () => { if (!drag) { pointer = null; redraw(); } }, opts);
    canvas.addEventListener('dblclick', reset, opts);
    canvas.addEventListener('keydown', e => {
      if (!total || !['+','=','-','Home','ArrowLeft','ArrowRight'].includes(e.key)) return;
      e.preventDefault(); pointer = null;
      if (e.key === 'Home') reset();
      else if (e.key.startsWith('Arrow')) pan(left + (e.key === 'ArrowLeft' ? -1 : 1));
      else zoom((e.key === '-' ? 1 : -1) * Math.max(1, Math.round(count / 5)), 0.5);
    }, opts);
    canvas.tabIndex = 0; canvas.style.touchAction = 'pan-y'; canvas.style.cursor = 'grab';
    const ro = new ResizeObserver(() => { if (initialized) redraw(); }); ro.observe(canvas);
    const mo = new MutationObserver(() => { colors = null; redraw(); });
    mo.observe(document.body, {attributes:true, attributeFilter:['class','data-theme']});
    window.addEventListener('resize', redraw, opts);
    initialized = true; reset();
    return {reset, redraw() { colors = null; redraw(); }, schedule:redraw, destroy() {
      dead = true; controller.abort(); ro.disconnect(); mo.disconnect(); cancelAnimationFrame(frame);
    }};
  }

  function dateAxis(f, rows, dateOf) {
    const {ctx,c,L,R,B,left,count} = f;
    if (!count) return;
    const labels = Math.min(count, R - L > 500 ? 5 : 3);
    for (let i = 0; i < labels; i++) {
      const fraction = labels === 1 ? 0 : i / (labels - 1);
      const index = Math.round((count - 1) * fraction);
      const x = L + index / Math.max(1, count - 1) * (R - L);
      axis(ctx, c['text-2'], dateOf(rows[left + index]).slice(5), x, B + 16,
        i === 0 ? 'left' : i === labels - 1 ? 'right' : 'center');
    }
  }

  function equity(canvas, options) {
    const rows = (options.rows || []).map(row => ({...row, strategy:row.strategy ?? row.legacy ?? null}));
    let range = options.range || 'all', visible = {...options.visible}, lastWindow = '', slice = [], bases = {};
    function begin() {
      if (!rows.length || range === 'all') return 0;
      const last = new Date(rows[rows.length - 1].date + 'T00:00:00Z');
      if (range === '3w') last.setUTCDate(last.getUTCDate() - 20);
      else if (range === '1m') {
        const day = last.getUTCDate(); last.setUTCDate(1); last.setUTCMonth(last.getUTCMonth() - 1);
        const daysInMonth = new Date(Date.UTC(last.getUTCFullYear(), last.getUTCMonth()+1, 0)).getUTCDate();
        last.setUTCDate(Math.min(day, daysInMonth));
      }
      const target = last.toISOString().slice(0,10);
      return Math.max(0, rows.findIndex(row => row.date >= target));
    }
    function rangeChanged(left, count) {
      slice = rows.slice(left, left + count);
      bases = Object.fromEntries(keys.map(key => [key, slice.find(row => finite(row[key]) && row[key] > 0)?.[key]]));
      const actual = slice.filter(r => finite(r.ai) && r.ai > 0);
      const comparable = actual.filter(r => finite(r.benchmark) && r.benchmark > 0);
      const first = actual[0], last = actual.at(-1), start = comparable[0], end = comparable.at(-1);
      const change = actual.length >= 2 ? percent(first.ai, last.ai) : null;
      const bench = comparable.length >= 2 ? percent(start.benchmark, end.benchmark) : null;
      options.onRange?.({firstDate:slice[0]?.date, lastDate:slice.at(-1)?.date, count,
        aiReturn:change, aiStart:first?.date, aiEnd:last?.date, aiStartValue:first?.ai, aiEndValue:last?.ai,
        benchmarkReturn:bench, alpha:bench === null ? null : percent(start.ai, end.ai) - bench,
        comparisonStart:start?.date, comparisonEnd:end?.date});
    }
    function draw(f) {
      const {ctx,c,L,R,T,B,left,count,hovered}=f;
      const windowKey = left + '/' + count;
      if (windowKey !== lastWindow) { lastWindow = windowKey; rangeChanged(left,count); }
      const active = keys.filter(key => visible[key] !== false);
      const values = active.flatMap(key => slice.map(r => percent(bases[key], r[key])).filter(finite));
      if (!values.length) {
        axis(ctx,c['text-2'],active.length ? '这个区间暂无可比净值' : '选择要查看的净值曲线',(L+R)/2,(T+B)/2,'center'); return;
      }
      const [lo,hi] = extent([0,...values],0.12,1.2);
      const x = i => count < 2 ? (L + R) / 2 : L + i / (count - 1) * (R - L);
      const y = value => B - (value-lo) / (hi-lo) * (B-T);
      for (let i=0;i<=3;i++) {
        const py = T + (B-T)*i/3, val = hi - (hi-lo)*i/3;
        stroke(ctx,[[L,py],[R,py]],c['canvas-grid'],1,[3,5]);
        axis(ctx,c['text-2'],(val>0?'+':'')+number(val,1)+'%', R+8, py);
      }
      if (lo<0 && hi>0) stroke(ctx,[[L,y(0)],[R,y(0)]],c['border-strong'],1,[3,4]);
      const line = {ai:c.up,benchmark:c.bench,strategy:c.down};
      for (const key of [...active].reverse()) {
        let segment = [];
        function flush() {
          if (!segment.length) return;
          let grad=null;
          if(key==='ai' && segment.length>1){grad=ctx.createLinearGradient(0,T,0,B);grad.addColorStop(0,c['up-grad-top']);grad.addColorStop(1,'rgba(255,255,255,0)');}
          if (segment.length === 1) dot(ctx, ...segment[0],2,line[key]);
          else smoothLine(ctx,segment,line[key],key==='ai'?2.8:1.9,key==='benchmark'?[5,4]:[],B,grad);
          segment=[];
        }
        slice.forEach((row,i) => {
          const change=percent(bases[key],row[key]);
          if (change===null) flush();
          else segment.push([x(i),y(change)]);
        });
        if (segment.length) { const last=segment.at(-1); flush(); dot(ctx,...last,2.8,line[key]); }
      }
      dateAxis(f,rows,r=>r.date);
      if (hovered!==null) {
        const px=x(hovered-left);
        stroke(ctx,[[px,T],[px,B]],c.cross,1,[4,4]);
        active.forEach(key=>{
          const change=percent(bases[key],rows[hovered][key]);
          if(change!==null) {dot(ctx,px,y(change),5,c.surface);dot(ctx,px,y(change),3,line[key]);}
        });
      }
    }
    const view=interactive(canvas,rows.length,begin,draw,i=>options.onHover?.(i===null?null:rows[i]),{left:6,right:64,top:14,bottom:26});
    return {setRange(next){range=next;view.reset();},setVisible(next){visible={...next};view.schedule();},
      redraw:view.redraw,destroy:view.destroy};
  }

  function movingAverage(rows, period) {
    let sum = 0;
    return rows.map((row,i) => {
      sum += row[4]; if(i>=period) sum -= rows[i-period][4];
      return i+1>=period ? sum/period : null;
    });
  }

  function candles(canvas, {rows, levels = [], onHover, mode = 'candle', emptyText}) {
    const ma5 = movingAverage(rows,5), ma20 = movingAverage(rows,20);
    const overlay=canvas.parentElement.querySelector('.qd-chart-cross'), cross=overlay?.getContext('2d');
    let baseKey='',revision=0,geometry=null,oldColors=null;
    function draw(f) {
      const {ctx,c,L,R,T,B,left,count,hovered,pointer} = f;
      if(cross){
        if(overlay.width!==canvas.width || overlay.height!==canvas.height){overlay.width=canvas.width;overlay.height=canvas.height;}
        cross.setTransform(canvas.width/f.width,0,0,canvas.height/f.height,0,0);
        cross.clearRect(0,0,f.width,f.height);
      }
      const nextKey=[left,count,canvas.width,canvas.height,mode,revision].join('/');
      if(nextKey!==baseKey || oldColors!==c || !cross){
        baseKey=nextKey;oldColors=c;
        ctx.clearRect(0,0,f.width,f.height);
        geometry=drawBase(f);
      }
      if(geometry && hovered!==null){
        const target=cross||ctx,px=geometry.x(hovered-left),py=clamp(pointer.y,T,B);
        stroke(target,[[px,T],[px,B]],c.cross,1,[4,4]);stroke(target,[[L,py],[R,py]],c.cross,1,[4,4]);
        dot(target,px,geometry.y(rows[hovered][4]),4,c.surface);dot(target,px,geometry.y(rows[hovered][4]),2.5,c.ink);
      }
    }
    function drawBase(f){
      const {ctx,c,L,R,T,B,left,count}=f;
      if(!count){axis(ctx,c['text-2'],emptyText || '暂无可读日线',(L+R)/2,(T+B)/2,'center');return null;}
      const slice=rows.slice(left,left+count);
      const volTop=B-(B-T)*0.18, priceBottom=volTop-19;
      const step=(R-L)/count;
      const x=i=>L+(i+.5)*step, visible=levels.filter(level=>finite(level.value)&&level.value>0);
      const [lo,hi]=extent([...slice.flatMap(row=>[row[2],row[3]]),...visible.map(level=>level.value),
        ...ma20.slice(left,left+count).filter(finite),...ma5.slice(left,left+count).filter(finite)],0.11,0.1);
      const y=val=>priceBottom-(val-lo)/(hi-lo)*(priceBottom-T);
      for (let i=0;i<=3;i++) {
        const value=lo+(hi-lo)*i/3,py=y(value);
        stroke(ctx,[[L,py],[R,py]],c['canvas-grid'],1,[3,5]);axis(ctx,c['text-2'],number(value),R+6,py);
      }
      stroke(ctx,[[L,volTop-4],[R,volTop-4]],c['border-strong'],1);
      const maxVol=Math.max(...slice.map(row=>row[5]),0);
      slice.forEach((row,i)=>{
        const px=x(i),open=row[1],high=row[2],low=row[3],close=row[4],vol=row[5];
        const color=close>=open ? c.up : c.down;
        const bw=clamp(step*.65,1,13);
        if(mode==='candle'){
          stroke(ctx,[[px,y(high)],[px,y(low)]],color,1.2);
          const bh=Math.max(1.4,Math.abs(y(close)-y(open)));
          ctx.fillStyle=color;ctx.beginPath();ctx.roundRect(px-bw/2,Math.min(y(open),y(close)),bw,bh,Math.min(bh/2,1.8));ctx.fill();
        }
        if(maxVol){
          const vh=vol/maxVol*(B-volTop);
          ctx.fillStyle=color;ctx.globalAlpha=.44;ctx.fillRect(px-bw/2,B-vh,bw,vh);ctx.globalAlpha=1;
        }
      });
      if(mode==='line') {
        const pts=slice.map((r,i)=>[x(i),y(r[4])]);
        const rising=slice.at(-1)[4]>=slice[0][4];
        const grad=ctx.createLinearGradient(0,T,0,priceBottom);
        grad.addColorStop(0,c[rising?'up-grad-top':'down-grad-top']); grad.addColorStop(1,'rgba(255,255,255,0)');
        smoothLine(ctx,pts,rising?c.up:c.down,2.3,[],priceBottom,grad);
      }
      [[ma5,c.ma5],[ma20,c.ma20]].forEach(([average,color])=>{
        const points=average.slice(left,left+count).flatMap((val,i)=>finite(val)?[[x(i),y(val)]]:[]);
        if(points.length)stroke(ctx,points,color,1.7);
      });
      for (const level of visible) {
        stroke(ctx,[[L,y(level.value)],[R,y(level.value)]],level.color,1,[4,4]);
        dot(ctx,R-2,y(level.value),2,level.color);
      }
      dateAxis(f,rows,r=>r[0]);
      return {x,y};
    }
    const view=interactive(canvas,rows.length,()=>Math.max(0,rows.length-40),draw,i=>onHover?.(i===null?null:rows[i],i),
      {left:5,right:41,top:12,bottom:24,centerBars:true,layered:true});
    return {setMode(next){mode=next;view.schedule();},setLevels(next){levels=next;revision++;view.schedule();},redraw:view.redraw,destroy:view.destroy};
  }
  window.WealthCharts = {equity,candles,interactive};
})();
