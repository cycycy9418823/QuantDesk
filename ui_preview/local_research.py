"""Fixed local research inputs; no network, model, ledger writes or simulation."""
from pathlib import Path
import csv
import hashlib
import io
import json
import math
import os
import re
from functools import lru_cache

HERE = Path(__file__).resolve().parent
RESEARCH_ROOT = Path(os.environ.get('QUANTDESK_RESEARCH_ROOT', str(HERE / 'research_data')))
FUND = Path(os.environ.get('QUANTDESK_FUND_DIR', str(RESEARCH_ROOT / 'fundamentals')))
ETF = Path(os.environ.get('QUANTDESK_ETF_DIR', str(RESEARCH_ROOT / 'etf_backtest')))
FILES = {FUND / x for x in ('core26_earnings_2026Q3.csv','core26_dividends_ttm.csv','dividend_events.csv')}
FILES |= {ETF / x for x in ('X_base_逐日净值.csv','Y_base_逐日净值.csv','Z_base_逐日净值.csv','H00300_同窗基准.csv','考试结果.json')}
FILES.add(HERE / 'core26_valuation_percentiles.json')
ANNOUNCEMENTS = Path(os.environ.get('QUANTDESK_ANNOUNCEMENTS_CSV', str(RESEARCH_ROOT / 'announcements_30d.csv')))
FILES.add(ANNOUNCEMENTS)

def number(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None

@lru_cache(maxsize=32)
def _read(path: Path, size: int, mtime: int):
    if path not in FILES or path.resolve() != path or size > 15_000_000:
        raise ValueError('Research file outside fixed whitelist or oversized')
    raw = path.read_bytes()
    after = path.stat()
    if (size,mtime) != (after.st_size,after.st_mtime_ns):
        raise ValueError('Research source changed during read')
    value = json.loads(raw.decode('utf-8-sig')) if path.suffix == '.json' else list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    return value, hashlib.sha256(raw).hexdigest()

def read(path: Path):
    st = path.stat()
    return _read(path,st.st_size,st.st_mtime_ns)

def earnings(until: str) -> dict:
    rows, sha = read(FUND / 'core26_earnings_2026Q3.csv')
    return {r['symbol']: {'name':r['name'],'period_label':'2026Q3','period_verified':True,
        'report_period':r['report_period'],'cumulative':True,'announcement_date':r['ann_date'],
        'rev_yoy':number(r['revenue_yoy']),'profit_yoy':number(r['net_profit_yoy']),
        'roe':number(r['roe']),'gross_margin':number(r['gross_margin']),
        'available':True,'warnings':[],'source':str(FUND / 'core26_earnings_2026Q3.csv'),
        'source_detail':r['source'],'announcement_url':r['announcement_url'],'source_sha256':sha}
        for r in rows if r['report_period']=='2026-09-30' and r['ann_date'] and r['ann_date']<=until}

def dividends(until: str) -> dict:
    rows,sha = read(FUND / 'core26_dividends_ttm.csv')
    events,_ = read(FUND / 'dividend_events.csv')
    out = {}
    for r in rows:
        if not r['as_of'] or r['as_of']>until:
            continue
        selected = [e for e in events if e['symbol']==r['symbol'] and
                    r['window_start_inclusive']<=e['ex_dividend_date']<=r['as_of']]
        out[r['symbol']] = {'yield_pct':number(r['dividend_yield_ttm_pct']),
            'cash_per_share':number(r['ttm_cash_per_share_cny']),'denominator_close':number(r['close_cny']),
            'as_of':r['as_of'],'window_start':r['window_start_inclusive'],
            'event_count':int(r['ttm_event_count']),'events':sorted(selected,key=lambda e:e['ex_dividend_date'],reverse=True),
            'status':r['status'],'issue':r['issue'],'source':str(FUND / 'core26_dividends_ttm.csv'),
            'source_detail':r['source'],'source_sha256':sha}
    return out

def percentiles() -> dict:
    try:
        return read(HERE / 'core26_valuation_percentiles.json')[0]
    except (OSError,ValueError):
        return {'stocks':{},'warning':'离线估值缓存缺失或无效'}

def announcements() -> dict:
    rows,sha=read(ANNOUNCEMENTS)
    covered={'600032','600000','601318','600036','600018','600028','000333'}
    result={s:[] for s in covered}
    for r in rows:
        s=r.get('symbol');url=r.get('pdf_url','')
        if s not in covered or not re.fullmatch(r'https://static\.cninfo\.com\.cn/finalpage/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_.-]+',url):
            raise ValueError('Announcement sample identity or official URL invalid')
        if not '2026-10-25'<=r.get('published_date','')<='2026-11-25':
            raise ValueError('Announcement sample outside fixed date range')
        result[s].append({k:r[k] for k in ('published_date','title','categories','attention','notes','pdf_url','announcement_id')})
    return {'stocks':result,'source_sha256':sha,'source':str(ANNOUNCEMENTS),
            'sample_start':'2026-10-25','sample_end':'2026-11-25','realtime':False,
            'notice':'本地官方公告目录样本 · 10/25—11/25 · 非实时更新；正文与链接可达性未复验'}

def readable_plan_intent(row: dict) -> dict:
    """Display only stored verified same-symbol clauses; prose stays explicitly unverified."""
    symbol=str(row.get('symbol',''))
    if not re.fullmatch(r'\d{6}',symbol):return {}
    clauses=[];rejected=0
    for clause in row.get('verified_stock_fact_clauses',[]) if isinstance(row.get('verified_stock_fact_clauses'),list) else []:
        if (not isinstance(clause,str) or not clause.startswith(symbol) or len(clause)>500
                or any(code!=symbol for code in re.findall(r'(?<!\d)\d{6}(?!\d)',clause))
                or '<' in clause or '>' in clause):
            rejected+=1;continue
        if clause not in clauses:clauses.append(clause)
    original=row.get('original_reason') if isinstance(row.get('original_reason'),str) else row.get('reason','')
    original=original if isinstance(original,str) else ''
    prose=re.sub(r'\{\{stock:[^{}]+\}\}','〔已核短句见下〕',original)
    prose=re.sub(r'(?:〔已核短句见下〕[；;：:\s]*){2,}','〔已核短句见下〕。',prose)
    prose=re.sub(r'(?:已核本机资料[；;：:\s]*)+','〔已核短句见下〕。',prose)
    return {**{k:row.get(k) for k in ('symbol','side','status','quantity','reason')},
            'display_reason':prose,'display_reason_label':'模型判断 · 未验证（含原判断条件，不代表已核事实）',
            'verified_clauses':clauses,'verified_clauses_label':'当时已保存的已核资料 · 保留原报告期，不替换为今日财报',
            'rejected_clause_count':rejected}

def etf_research() -> dict:
    result,sha = read(ETF / '考试结果.json')
    scenarios = result['scenarios']
    if isinstance(scenarios,dict):
        scenarios = list(scenarios.values())
    series=[]
    for key,name,file in [('X','X 红利均衡','X_base_逐日净值.csv'),('Z','Z 防守优先','Z_base_逐日净值.csv'),
                          ('Y','Y 宽基平衡','Y_base_逐日净值.csv'),('H00300','沪深300全收益','H00300_同窗基准.csv')]:
        rows,fingerprint = read(ETF / file)
        points=[]; peak=100000.0
        for r in rows:
            amount=number(r['total_assets'])
            if amount is None or amount<=0:
                raise ValueError('Invalid research equity')
            peak=max(peak,amount)
            points.append([r['date'],amount,(amount/100000-1)*100,(amount/peak-1)*100])
        if len({p[0] for p in points})!=len(points) or points!=sorted(points) or points[0][0]!='2016-12-19' or points[-1][0]!='2026-06-30':
            raise ValueError('Invalid research calendar')
        metrics=result['benchmark'] if key=='H00300' else next(s for s in scenarios if s['case']==key and s['mode']=='base')
        if abs(points[-1][1]-metrics['final_assets'])>=0.01:
            raise ValueError('Research result does not match equity CSV')
        series.append({'key':key,'name':name,'points':points,'metrics':metrics,'source':str(ETF/file),'sha256':fingerprint})
    if any([p[0] for p in s['points']] != [p[0] for p in series[0]['points']] for s in series):
        raise ValueError('Research calendars differ')
    return {'available':True,'start':'2016-12-19','end':'2026-06-30','label':'研究输出 · 未验证 · 指数代理口径（非正式纸面账）',
        'series':series,'results_sha256':sha,'source':str(ETF/'考试结果.json'),
        'note':'直接读取已完成考试的基准费用列；不重跑回测。各套10万元独立起步，含基金年费和佣金。沪深300为未扣费全收益指数。',
        'bond_note':'中证现行通用债券细则为 V5.0（2026-10）、未另追溯 2016 历史版本 PDF'}
