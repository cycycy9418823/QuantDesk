"""Daily evidence, memory and the human-readable paper-trader report."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from brain import Brain
from engine import LABEL, REASONS, PaperEngine, money, write_json

DEFAULT_PLAYBOOK = '# 个人交易心得本\n\n目前没有经过盘后核对的条目。\n'
REJECTIONS = {
    'st_or_delisting': '名称含 ST 或退市标记', 'board_not_allowed': '板块不准入',
    'name_missing': '名称缺失', 'missing_reasons': '缺四项理由',
    'single_stock_over_25_percent': '单票仓位会超过四分之一',
    't_plus_one_not_sellable': 'T+1 可卖股数不足', 'limit_up_blocks_buy': '涨停拒买',
    'limit_down_blocks_sell': '跌停拒卖', 'insufficient_cash': '现金不足',
    'next_quote_not_after_decision': '下一档的个股报价早于或等于决策时间',
    'cannot_measure_current_equity': '部分持仓缺当前报价，无法精确核对仓位',
}
BASIS = {'official_raw_daily_close': '当日 raw 日线收盘', '1455_snapshot_proxy': '14:55 末档代理价'}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line] if path.exists() else []


def append_event(work: Path, day: str, event: dict) -> None:
    path = work / 'state' / 'day_events' / f'{day}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def load_memory(work: Path, day: str) -> tuple[str, list[str]]:
    state = work / 'state'
    path = state / 'watchlist.json'
    watch = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if watch.get('as_of', '') > day:
        raise ValueError('memory_newer_than_requested_day')
    path = state / 'playbook.md'
    return (path.read_text(encoding='utf-8')[:2000] if path.exists() else DEFAULT_PLAYBOOK,
            watch.get('symbols', [])[:15])


def save_watchlist(work: Path, day: str, watchlist: list[str]) -> None:
    write_json(work / 'state' / 'watchlist.json', {'as_of': day, 'symbols': watchlist[:15], 'label': LABEL})


def _save_playbook(work: Path, day: str, text: str) -> None:
    state = work / 'state'
    history = state / 'playbook_history' / f'{day}.md'
    history.parent.mkdir(parents=True, exist_ok=True)
    for target in (state / 'playbook.md', history):
        target.write_text(text.strip()[:1999] + '\n', encoding='utf-8')


def _review_rows(engine: PaperEngine, day: str, sessions: list[dict]) -> tuple[list[dict], dict]:
    market = engine.market
    journal = [item for item in read_jsonl(engine.journal_path) if item.get('day') == day]
    orders = [item for item in journal if item.get('result') == 'frozen']
    rejects = {item['order_id']: item for item in journal
               if item.get('event') in {'rejected', 'settlement_rejected'}}
    trades = {item['order_id']: item for item in engine.state()['trades'] if item['day'] == day}
    watch = {card['symbol'] for event in sessions for card in event['screen'].get('watchlist', [])}
    inspected = {code for event in sessions for code in event.get('inspect_symbols', [])}
    closing = market.close(day, [*sorted(watch | inspected), *(order['symbol'] for order in orders)])['symbols']
    rows = []
    for order in orders:
        identifier, code = order['order_id'], order['symbol']
        end = closing.get(code, {})
        frame = market.frame(day, order['slot'])
        start = frame.rows.get(code) if frame and frame.evidence()['sha256'] == order['snapshot_evidence']['sha256'] else None
        price = start.price if start and not market.quote_issue(start, day) else None
        fill = trades.get(identifier, {})
        rejection = rejects.get(identifier, {})
        next_frame = market.frame(day, order['expected_slot']) if rejection.get('event') == 'settlement_rejected' else None
        next_quote = next_frame.rows.get(code) if next_frame else None
        next_price = next_quote.price if next_quote and not market.quote_issue(next_quote, day) else None
        comparison_price = (
            Decimal(fill['fill_price']) if fill else
            next_price if rejection.get('event') == 'settlement_rejected' else price
        )
        change = (Decimal(end['price']) / comparison_price - 1) * 100 if comparison_price and end.get('price') else None
        stop = order.get('stop_price')
        rows.append({
            'symbol': code, 'slot': order['slot'], 'side': order['side'], 'qty': order['qty'],
            'reasons': order['reasons'], 'reject': rejection.get('reason'),
            'fill': {key: fill[key] for key in ('slot', 'raw_price', 'fill_price', 'total_fees') if key in fill},
            'decision_price': str(price) if price else None, 'close': end,
            'comparison_basis': 'fill_price' if fill else (
                'next_slot_raw' if rejection.get('event') == 'settlement_rejected' and next_price
                else 'decision_quote'
            ),
            'comparison_price': str(comparison_price) if comparison_price is not None else None,
            'price_change_pct': str(money(change)) if change is not None else None,
            'stop_price': stop,
            'close_at_or_below_stop': (Decimal(end['price']) <= Decimal(str(stop)))
            if stop is not None and end.get('price') else None,
            'stop_order_filled': bool(fill and order['side'] == 'sell' and stop is not None),
        })
    return rows, closing


def review_day(engine: PaperEngine, brain: Brain, day: str, watch_filter: Callable) -> dict:
    work, market = engine.work_dir, engine.market
    events = read_jsonl(work / 'state' / 'day_events' / f'{day}.jsonl')
    sessions_by_slot = {event['slot']: event for event in events if event['stage'] == 'session'}
    sessions = list(sessions_by_slot.values())
    archive = market.archive(day)
    write_json(work / 'reports' / f'{day}_快照指纹.json', archive)
    missing = [row['slot'] for row in archive if not row.get('available')]
    inventory = {'available': len(archive) - len(missing), 'missing': missing,
                 'rows': sum(row.get('row_count', 0) for row in archive)}
    rows, closing = _review_rows(engine, day, sessions)
    summaries = [{key: event[key] for key in ('slot', 'note', 'inspect_symbols')} for event in sessions]
    before, watch = load_memory(work, day)
    fills = sum(bool(row['fill']) for row in rows)
    rejected = sum(bool(row['reject']) for row in rows)
    fallback = f'程序核对了 {len(sessions)} 篇盘中手记、{fills} 笔纸面成交和 {rejected} 笔拒单。'
    if fills:
        fallback += '逐笔分开看决策价、下一档成交和收盘关口。单日涨跌不足以检验交易逻辑。'
    fixture = {
        'review': fallback,
        'playbook': before + '\n- 逐笔对照下一档的原始报价。仓位没法当天退出时，把退出条件记进次日计划。',
        'watchlist': list(dict.fromkeys([row['symbol'] for row in rows if row['fill']] + watch)),
    }
    response = brain.ask('review', {
        'day': day, 'items': rows, 'notes': summaries, 'closing': closing, 'archive_summary': inventory,
        'playbook': before, 'watchlist': watch, 'stub_responses': {'review': fixture},
    }) if brain.mode != 'dry-run' else {}
    status = response.get('llm_status', 'offline_replay' if brain.mode == 'replay' else 'offline_review')
    accepted = status in {'response_received', 'offline_replay', 'offline_review'}
    after, following = before, watch
    if accepted:
        after = (response.get('playbook') or fixture['playbook']).strip()[:1999] + '\n'
        following = watch_filter(market, day, '1455', response.get('watchlist', fixture['watchlist']))
        _save_playbook(work, day, after)
        save_watchlist(work, day, following)
    event = {'stage': 'review', 'status': status, 'review': response.get('review') or fallback,
             'archive': inventory, 'items': rows, 'closing': closing, 'rejections': rejected,
             'playbook_before': before, 'playbook_after': after,
             'watchlist_before': watch, 'watchlist_after': following}
    append_event(work, day, event)
    render(engine, day)
    return event


def _num(value: Any, places: int = 3) -> str:
    return f'{Decimal(str(value)):,.{places}f}' if value is not None else '缺数据'


def render(engine: PaperEngine, day: str, replay_destination: Path | None = None) -> Path:
    events = read_jsonl(engine.work_dir / 'state' / 'day_events' / f'{day}.jsonl')
    lines = [f'# {day} 交易员日记', '', f'**{LABEL}**', '']
    for event in events:
        stage, slot = event['stage'], event.get('slot')
        if stage == 'session':
            temp = event['screen']['temperature']
            lines += [f'## {slot[:2]}:{slot[2:]} 看盘', '',
                      f"涨 {temp['up']}、跌 {temp['down']}、平 {temp['flat']} 家。涨停 {temp['limit_up']}、跌停 {temp['limit_down']} 家。",
                      f"均幅 {_num(temp['average_pct'])}%，累计成交额 {_num(temp['amount'], 2)} 元，前一交易日同档比 {_num(temp['amount_ratio'])}。",
                      f"{event['note']}\n",
                      f"查股 {', '.join(event['inspect_symbols']) or '无'}。决策时间 {event['decision_at']}。", '']
            if warnings := event['screen'].get('warnings'):
                lines += [f"数据提示 {' / '.join(warnings)}。", '']
            for order, outcome in zip(event['orders'], event['order_outcomes']):
                reasons = order.get('reasons') or {}
                if not isinstance(reasons, dict):
                    reasons = {}
                reason = outcome.get('reason')
                action = '买' if order.get('side') == 'buy' else '卖'
                result = REJECTIONS.get(reason, reason) if reason else f"待 {outcome['next_slot']} 结算"
                lines.append(f"- {action} {order.get('symbol')} × {order.get('qty')}，{result}。")
                for label, key in zip(('信号', '计划', '认错条件', '风险'), REASONS):
                    lines.append(f"  - {label} — {reasons.get(key) or '缺失'}")
            lines.append('')
        elif stage == 'settle':
            lines += [f'## {slot[:2]}:{slot[2:]} 下一档撮合', '']
            if not event['evidence']:
                lines.append('快照未到。已有意向继续待成交。')
            elif not event['results']:
                lines.append('无待成交单。')
            for row in event['results']:
                if row['status'] == 'paper_fill':
                    fees = row['fees']
                    lines.append(f"- {row['symbol']} × {row['qty']} 股，raw {row['raw_price']} 元，滑点后 {row['fill_price']} 元。"
                                 f"佣金 {fees['commission']} 元，印花税 {fees['stamp_tax']} 元，过户费 {fees['transfer_fee']} 元。")
                elif row['status'] == 'rejected':
                    lines.append(f"- {row['symbol']} {REJECTIONS.get(row['reason'], row['reason'])}。")
            if account := event.get('account'):
                lines.append(f"\n现金 {account['cash']} 元，快照标记权益 {account['equity'] or '缺报价'} 元。")
            lines.append('')
        elif stage == 'review':
            lines += ['## 盘后复盘', '', event['review'], '', '### 每笔意向回看', '']
            for row in event['items']:
                close, fill = row['close'], row['fill']
                verdict = REJECTIONS.get(row['reject'], row['reject']) if row['reject'] else ('成交' if fill else '仍待结算')
                end = f"{close['price']} 元（{BASIS.get(close['basis'], close['basis'])}）" if close.get('price') else '缺有效收盘数据'
                lines.append(f"- {row['slot']} {row['symbol']} {row['side']}，{verdict}。收盘对照 {end}。")
                if row['price_change_pct'] is not None:
                    basis = {
                        'fill_price': '含滑点成交价', 'next_slot_raw': '下一档 raw 价',
                        'decision_quote': '决策时 raw 价',
                    }[row['comparison_basis']]
                    lines.append(f"  {basis} {row['comparison_price']} 元至收盘变化 {row['price_change_pct']}%。")
                if row['close_at_or_below_stop'] is not None:
                    trigger = '收盘低于或等于所列止损位' if row['close_at_or_below_stop'] else '收盘高于所列止损位'
                    execution = '，且卖出意向有成交' if row['stop_order_filled'] else '，未见对应卖出成交'
                    lines.append(f'  {trigger}{execution}。')
            watched = {code: close for code, close in event['closing'].items()
                       if code not in {row['symbol'] for row in event['items']}}
            for code, close in watched.items():
                basis = BASIS.get(close['basis'], close['basis'])
                lines.append(f"- {code} 仅观察，收盘 {close['price'] or '缺数据'}（{basis}）。")
            inv = event['archive']
            lines += ['', f"档位核对 {inv['available']} 档，共 {inv['rows']:,} 行。缺档 {', '.join(inv['missing']) or '无'}。",
                      '', '### 心得本变化', '', '**之前**', '', event['playbook_before'].strip(), '',
                      '**之后**', '', event['playbook_after'].strip(), '',
                      f"自选池 {'、'.join(event['watchlist_before']) or '空'} → {'、'.join(event['watchlist_after']) or '空'}。", '']
    state = engine.state()
    fills = [trade for trade in state['trades'] if trade['day'] == day]
    fees = sum((Decimal(trade['total_fees']) for trade in fills), Decimal(0))
    lines += ['## 影子账户', '', f"当天成交 {len(fills)} 笔，费用 {_num(fees, 2)} 元。当前现金 {state['cash']} 元。", '']
    directory = replay_destination or engine.work_dir / 'reports'
    directory.mkdir(parents=True, exist_ok=True)
    suffix = '回放样例日记' if replay_destination else '交易员日记'
    path = directory / f'{day}_{suffix}.md'
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path
