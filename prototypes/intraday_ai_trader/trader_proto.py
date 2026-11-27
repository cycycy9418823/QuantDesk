"""Offline-first command line for an external paper account."""
from __future__ import annotations

import argparse
import json
import msvcrt
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from brain import Brain
from diary import DEFAULT_PLAYBOOK, append_event, load_memory, read_jsonl, render, review_day, save_watchlist
from engine import BEIJING, LABEL, PaperEngine, slot_time, write_json
from market import DECISION_SLOTS, NEXT_SLOT, Market

WORK_DIR = Path(__file__).resolve().parent
QUANTDESK_ROOT = Path(os.environ.get('QUANTDESK_ROOT', str(Path(__file__).resolve().parents[2])))


class OperatorError(ValueError):
    pass


@contextmanager
def writer_lock(work: Path):
    with (work / '.shadow_writer.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise OperatorError('另一个原型命令正在运行，请等它结束。') from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def now() -> datetime:
    return datetime.now(BEIJING)


def session(engine: PaperEngine, brain: Brain, day: str, slot: str, fixtures: dict | None = None) -> dict:
    market = engine.market
    frame = market.frame(day, slot)
    if frame is None or not frame.decision_at:
        raise OperatorError('本档快照尚未落地或缺少可信采集时间。')
    available = datetime.fromisoformat(frame.decision_at)
    deadline = slot_time(day, NEXT_SLOT[slot])
    if available > now() or (brain.mode == 'llm' and not available <= now() < deadline):
        raise OperatorError('盘中模型命令须在本档落地后、下一档开始前执行。')
    if engine.has_pending(day):
        raise OperatorError('本日仍有待成交单，请先结算其指定档位。')
    playbook, watchlist = load_memory(engine.work_dir, day)
    account = engine.account(day, slot)
    screen = market.screen(day, slot, account['holdings'], watchlist)
    context = {'screen': screen, 'watchlist': watchlist, 'stub_responses': fixtures or {}}
    first = brain.ask('inspect', context)
    # The model may point to radar, holdings, or its own watchlist. The replay
    # intentionally asks about a few excluded symbols to exercise the gates.
    permitted = set(watchlist) | set(account['holdings'])
    for group in screen.get('radar', {}).values():
        if isinstance(group, list):
            permitted.update(card['symbol'] for card in group)
    symbols = [code for code in first.get('inspect_symbols', []) if code in permitted or fixtures][:5]
    details = market.inspect(day, slot, symbols) if symbols else {'symbols': {}}
    context.update(inspection=details, account=account, playbook=playbook)
    first_failed = brain.mode == 'llm' and first.get('llm_status') != 'response_received'
    response = first if first_failed or (brain.mode == 'llm' and now() >= deadline) else brain.ask('decide', context)
    submitted = now() if brain.mode == 'llm' else available
    status = response.get('llm_status', 'ok')
    if brain.mode == 'llm' and submitted >= deadline:
        status = 'decision_window_closed'
    accepted = status in {'ok', 'response_received'}
    orders = response.get('orders', []) if accepted else []
    for order in orders:
        detail = details['symbols'].get(order.get('symbol'), {})
        quote = frame.rows.get(order.get('symbol'))
        history = detail.get('history', [])
        known = [detail.get('ma5'), detail.get('ma20')]
        if quote is not None and not market.quote_issue(quote, day):
            known.extend([quote.price, quote.pre_close, quote.high, quote.low])
        known.extend(row.get(key) for row in history for key in ('open', 'high', 'low', 'close') if isinstance(row, dict))
        known.extend(row.get('price') for row in detail.get('trajectory', []))
        order['allowed_prices'] = [str(value) for value in known if value is not None]
    note = response.get('note', '') if accepted else (
        '离线看盘画面已保存，本节点没有意向单。' if brain.mode == 'dry-run'
        else '模型没有在本档生成可用答复，本节点没有意向单。'
    )
    outcomes = engine.submit_decision(
        day=day, slot=slot, note=note, orders=orders, snapshot_evidence=frame.evidence(),
        claims=response.get('claims', []), decision_at=submitted.isoformat(),
    ) if orders else []
    if accepted:
        save_watchlist(engine.work_dir, day, eligible_watchlist(market, day, slot, response.get('watchlist', watchlist)))
    event = {
        'stage': 'session', 'day': day, 'slot': slot, 'decision_at': submitted.isoformat(),
        'screen': screen, 'inspection': details, 'inspect_symbols': symbols,
        'note': note, 'orders': [{key: value for key, value in order.items() if key != 'allowed_prices'} for order in orders],
        'order_outcomes': outcomes, 'account': account, 'brain_mode': brain.mode, 'status': status,
    }
    append_event(engine.work_dir, day, event)
    write_json(engine.work_dir / 'reports' / f'{day}_{slot}_看盘画面.json', event)
    render(engine, day)
    return event


def eligible_watchlist(market: Market, day: str, slot: str, symbols: list[str]) -> list[str]:
    frame = market.frame(day, slot)
    if frame is None:
        return []
    return [code for code in dict.fromkeys(symbols)
            if code in frame.rows and not market.eligibility(code, frame.rows[code].name)][:15]


def settle(engine: PaperEngine, day: str, slot: str) -> dict:
    results = engine.settle(day, slot)
    frame = engine.market.frame(day, slot)
    usable = frame and frame.decision_at and datetime.fromisoformat(frame.decision_at) <= now()
    event = {'stage': 'settle', 'slot': slot, 'results': results,
             'evidence': frame.evidence() if usable else None,
             'account': engine.account(day, slot) if usable else None}
    events = read_jsonl(engine.work_dir / 'state' / 'day_events' / f'{day}.jsonl')
    seen = any(item['stage'] == 'settle' and item['slot'] == slot and item['evidence'] for item in events)
    if not seen:
        append_event(engine.work_dir, day, event)
    render(engine, day)
    return event


def stub(slot: str) -> dict:
    reasons = {
        'signal': '演练意向，检查成交额活跃个股在下一档的报价和可交易性。',
        'plan': '短线试仓，观察到下一个交易日能否延续。',
        'invalidation': '若后续跌破决策前最近一档的分时低点，重新核查退出条件。',
        'risk': '盘中冲高回落与当日买入后无法退出。',
    }
    if slot == '1000':
        symbols = ['600000', '300001', '000158', '000056', '688001']
        specs = [('600000', 1000), ('300001', 300), ('000056', 100), ('688001', 100),
                 ('830799', 100), ('920799', 100), ('600000', 3000), ('600000', 100), ('000158', 100)]
        orders = [{'side': 'buy', 'symbol': code, 'qty': qty, 'reasons': reasons if index != 7 else None}
                  for index, (code, qty) in enumerate(specs)]
        note = '早盘试两只，顺手把追涨和不合规的样例都过一遍硬规则。'
    elif slot == '1330':
        symbols = ['600000', '300001']
        orders = [{'side': 'sell', 'symbol': '600000', 'qty': 100, 'reasons': {
            **reasons, 'signal': '午后拿早盘仓做一次退出检查。',
        }}]
        note = '下午试着减一手，看看早上新买的仓能不能动。'
    else:
        symbols, orders = ['600000', '300001', '000158'], []
        note = '尾盘盯着两笔试仓和早上没买成的涨停股，今天收手。'
    return {'inspect': {'inspect_symbols': symbols},
            'decide': {'note': note, 'orders': orders, 'watchlist': symbols[:2]}}


def execute(args: argparse.Namespace) -> dict[str, Any]:
    day, command = args.date, args.command
    market = Market(QUANTDESK_ROOT)
    if day > now().date().isoformat() or not market.is_open(day):
        raise OperatorError('日期是未来或本地日历未标为交易日。')
    if command in {'review', 'replay'} and now() < slot_time(day, '1500'):
        raise OperatorError('盘后命令须在北京时间 15:00 之后执行。')
    normal = PaperEngine(WORK_DIR, market)
    if command == 'replay':
        run = WORK_DIR / 'evidence' / 'replay_state' / day / uuid.uuid4().hex[:10]
        engine, brain = PaperEngine(run, market), Brain(run, 'replay')
        for slot in DECISION_SLOTS:
            session(engine, brain, day, slot, stub(slot))
            result = settle(engine, day, NEXT_SLOT[slot])
            if result['evidence'] is None:
                raise OperatorError('回放所需的下一档快照缺失。')
        review = review_day(engine, brain, day, eligible_watchlist)
        if len(review['archive']['missing']):
            raise OperatorError('回放所需的标准快照不齐，详见运行目录。')
        report = render(engine, day, replay_destination=WORK_DIR / 'reports')
        return {'report': str(report), 'evidence': str(run), 'archive': review['archive'],
                'trades': len(engine.state()['trades']), 'rejections': review['rejections'],
                'account': engine.account(day, '1455')}
    events = read_jsonl(normal.work_dir / 'state' / 'day_events' / f'{day}.jsonl')
    if command == 'settle':
        result = settle(normal, day, args.slot)
        return {key: result[key] for key in ('slot', 'results', 'account')}
    brain = Brain(WORK_DIR, 'llm' if args.enable_llm else 'dry-run')
    if command == 'review':
        if not any(event['stage'] == 'session' for event in events) or normal.has_pending(day):
            raise OperatorError('复盘前至少保存一篇盘中手记，并结清本日待成交单。')
        if any(
            event['stage'] == 'review'
            and event['status'] in {'response_received', 'offline_review', 'offline_replay'}
            for event in events
        ):
            raise OperatorError('当天深度复盘已经归档。')
        result = review_day(normal, brain, day, eligible_watchlist)
        return {key: result[key] for key in ('status', 'archive', 'watchlist_after')}
    same_slot = [
        event for event in events
        if event['stage'] == 'session' and event['slot'] == args.slot
    ]
    if same_slot and not (
        args.enable_llm
        and all(event['brain_mode'] == 'dry-run' and not event['order_outcomes'] for event in same_slot)
    ):
        raise OperatorError('该时点已有盘中画面或决定记录。')
    prior_sessions = [
        event for event in events
        if event['stage'] == 'session' and event['brain_mode'] != 'dry-run'
    ]
    if args.enable_llm and any(event['slot'] >= args.slot for event in prior_sessions):
        raise OperatorError('该时点已记录模型会话，或已有更晚时点的决定。')
    result = session(normal, brain, day, args.slot)
    return {key: result[key] for key in ('slot', 'status', 'order_outcomes')}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=f'仓外 AI 交易员 · {LABEL}')
    commands = root.add_subparsers(dest='command', required=True)
    for command in ('replay', 'session', 'settle', 'review'):
        sub = commands.add_parser(command)
        sub.add_argument('--date', required=True)
        if command in {'session', 'settle'}:
            slots = DECISION_SLOTS if command == 'session' else tuple(NEXT_SLOT.values())
            sub.add_argument('--slot', required=True, choices=slots)
        if command in {'session', 'review'}:
            sub.add_argument('--enable-llm', action='store_true', help='仅此命令允许使用现有模型配置')
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if str(date.fromisoformat(args.date)) != args.date:
            raise ValueError('invalid_date')
        with writer_lock(WORK_DIR):
            result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status', 'ok') in {'ok', 'response_received', 'offline_dry_run', 'offline_review'} else 2
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'任务未完成（{type(exc).__name__}）。请核对日期、本地输入和影子账。', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
