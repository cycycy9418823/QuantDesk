"""Cent-exact, isolated paper ledger and execution rules."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from market import NEXT_SLOT

CENT = Decimal('0.01')
INITIAL_CASH = Decimal('100000.00')
BEIJING = timezone(timedelta(hours=8))
REASONS = ('signal', 'plan', 'invalidation', 'risk')
LABEL = '纸面 · 未验证 · 不接券商'


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def slot_time(day: str, slot: str) -> datetime:
    return datetime.fromisoformat(f'{day}T{slot[:2]}:{slot[2:]}:00+08:00')


def _timestamp(value: str | None) -> datetime | None:
    try:
        result = datetime.fromisoformat(value or '')
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def fee_breakdown(side: str, price: Decimal, quantity: int) -> dict[str, Decimal]:
    notional = price * quantity
    return {
        'commission': max(Decimal('5.00'), money(notional * Decimal('0.00025'))),
        'stamp_tax': money(notional * Decimal('0.0005')) if side == 'sell' else Decimal('0.00'),
        'transfer_fee': money(notional * Decimal('0.00001')),
    }


def slippage_price(side: str, raw_price: Decimal) -> Decimal:
    if side == 'buy':
        return (raw_price * Decimal('1.0005')).quantize(CENT, rounding=ROUND_CEILING)
    return (raw_price * Decimal('0.9995')).quantize(CENT, rounding=ROUND_FLOOR)


def reasons_issue(reasons: Any) -> str | None:
    if not isinstance(reasons, Mapping):
        return 'missing_reasons'
    for key in REASONS:
        if not isinstance(reasons.get(key), str) or not reasons[key].strip():
            return f'missing_reason_{key}'
    return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


class PaperEngine:
    def __init__(self, work_dir: Path, market: Any) -> None:
        self.work_dir, self.market = work_dir, market
        self.ledger_path = work_dir / 'state' / 'shadow_ledger.json'
        self.journal_path = work_dir / 'state' / 'shadow_journal.jsonl'
        if not self.ledger_path.exists():
            self._save({
                'label': LABEL, 'initial_cash': str(INITIAL_CASH), 'cash': str(INITIAL_CASH),
                'lots': {}, 'pending': [], 'marks': {}, 'trades': [], 'realized_pnl': '0.00',
                'decisions': [], 'settlements': [],
            })

    def state(self) -> dict[str, Any]:
        return json.loads(self.ledger_path.read_text(encoding='utf-8'))

    def _save(self, state: dict[str, Any]) -> None:
        write_json(self.ledger_path, state)

    def _freeze(self, entry: dict[str, Any]) -> None:
        with self.journal_path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def _freeze_once(self, entry: dict[str, Any]) -> None:
        if self.journal_path.exists():
            for line in self.journal_path.read_text(encoding='utf-8').splitlines():
                try:
                    old = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if old.get('order_id') == entry.get('order_id') and old.get('event') == entry.get('event'):
                    return
        self._freeze(entry)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(BEIJING)

    @staticmethod
    def _sellable(lots: list[dict], day: str) -> int:
        return sum(lot['qty'] for lot in lots if lot['buy_date'] < day)

    @staticmethod
    def _lot_cost(lot: dict) -> Decimal:
        if lot.get('total_cost') is not None:
            return Decimal(str(lot['total_cost']))
        return money(Decimal(str(lot.get('unit_cost', '0'))) * lot['qty'])

    def _marks(self, state: dict, day: str, slot: str) -> tuple[dict, list[str]]:
        frame = self.market.frame(day, slot)
        marks, warnings = {}, []
        for symbol, lots in state['lots'].items():
            quote = frame.rows.get(symbol) if frame else None
            if self.market.quote_issue(quote, day) is None:
                price = quote.price
            else:
                warnings.append(symbol)
                prior = state['marks'].get(symbol)
                if isinstance(prior, dict) and prior.get('key', '') <= f'{day}/{slot}':
                    price = Decimal(prior['price'])
                elif prior and not isinstance(prior, dict):
                    price = Decimal(str(prior))
                else:
                    price = None
            marks[symbol] = price * sum(lot['qty'] for lot in lots) if price is not None else None
        return marks, warnings

    def account(self, day: str, slot: str) -> dict[str, Any]:
        state = self.state()
        marks, warnings = self._marks(state, day, slot)
        holdings = {}
        for symbol, lots in state['lots'].items():
            qty = sum(lot['qty'] for lot in lots)
            cost = sum((self._lot_cost(lot) for lot in lots), Decimal(0))
            value = marks[symbol]
            holdings[symbol] = {
                'qty': qty, 'sellable_qty': self._sellable(lots, day), 'total_cost': str(cost),
                'avg_cost': str(money(cost / qty)), 'market_value': str(money(value)) if value is not None else None,
                'pnl': str(money(value - cost)) if value is not None else None,
            }
        market_value = None if None in marks.values() else sum(marks.values(), Decimal(0))
        return {
            'cash': state['cash'], 'holdings': holdings, 'pending_count': len(state['pending']),
            'market_value': str(money(market_value)) if market_value is not None else None,
            'equity': str(money(Decimal(state['cash']) + market_value)) if market_value is not None else None,
            'mark_warnings': warnings,
        }

    def submit_decision(self, *, day: str, slot: str, note: str, orders: list[dict],
                        snapshot_evidence: Mapping, claims: list[dict] | None = None,
                        decision_at: str | None = None) -> list[dict]:
        if slot not in NEXT_SLOT:
            raise ValueError('invalid_decision_slot')
        frame = self.market.frame(day, slot)
        captured = _timestamp(getattr(frame, 'decision_at', None))
        submitted = _timestamp(decision_at) if decision_at else captured
        if captured is None or submitted is None or submitted < captured:
            raise ValueError('invalid_decision_timestamp')
        if submitted.astimezone(BEIJING).date().isoformat() != day:
            raise ValueError('decision_timestamp_day_mismatch')
        next_slot = NEXT_SLOT[slot]
        if submitted >= slot_time(day, next_slot):
            raise ValueError('decision_window_closed')
        if not orders:
            return []
        state = self.state()
        key = f'{day}/{slot}'
        if state['decisions'] and key <= state['decisions'][-1]:
            raise ValueError('decision_already_recorded_or_out_of_order')
        if self.journal_path.exists():
            for line in self.journal_path.read_text(encoding='utf-8').splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get('result') == 'frozen' and record.get('day') == day and record.get('slot') == slot:
                    raise ValueError('decision_already_frozen')
        outcomes = []
        for order in orders:
            record = {
                'order_id': str(uuid.uuid4()), 'day': day, 'slot': slot, 'expected_slot': next_slot,
                'submitted_at': submitted.isoformat(), 'decision_note': note, 'claims': claims or [],
                'side': str(order.get('side', '')).lower(), 'symbol': str(order.get('symbol', '')),
                'qty': order.get('qty'), 'reasons': order.get('reasons'),
                'stop_price': order.get('stop_price'), 'snapshot_evidence': dict(snapshot_evidence),
            }
            self._freeze({**record, 'result': 'frozen'})
            issue = self._order_issue(state, record, frame, order.get('allowed_prices', []))
            if issue:
                outcomes.append(self._reject(record, 'rejected', issue))
            else:
                state['pending'].append(record)
                self._freeze({'event': 'queued', 'order_id': record['order_id'], 'expected_slot': next_slot})
                outcomes.append({'order_id': record['order_id'], 'status': 'pending_next_slot', 'next_slot': next_slot})
        state['decisions'].append(key)
        self._save(state)
        return outcomes

    def _order_issue(self, state: dict, order: dict, frame: Any, allowed_prices: list) -> str | None:
        side, symbol, qty = (order[key] for key in ('side', 'symbol', 'qty'))
        if issue := reasons_issue(order['reasons']):
            return issue
        if side not in {'buy', 'sell'}:
            return 'invalid_side'
        if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
            return 'invalid_symbol'
        if type(qty) is not int or qty <= 0 or qty % 100:
            return 'quantity_must_be_positive_100_share_lot'
        if order['stop_price'] is not None:
            try:
                stop = Decimal(str(order['stop_price']))
                valid = stop.is_finite() and stop > 0 and stop in map(Decimal, allowed_prices)
            except (InvalidOperation, ValueError, TypeError):
                valid = False
            if not valid:
                return 'unverified_invalidation_price'
        if side == 'sell':
            return 't_plus_one_not_sellable' if self._sellable(state['lots'].get(symbol, []), order['day']) < qty else None
        quote = frame.rows.get(symbol)
        return self.market.eligibility(symbol, quote.name if quote is not None else None)

    def _reject(self, record: dict, event: str, reason: str) -> dict:
        outcome = {'order_id': record['order_id'], 'symbol': record['symbol'], 'status': 'rejected', 'reason': reason}
        self._freeze_once({**record, **outcome, 'event': event})
        return outcome

    def _fill_issue(self, state: dict, order: dict, quote: Any, fill: Decimal,
                    fees: Decimal, day: str, slot: str) -> str | None:
        lots = state['lots'].get(order['symbol'], [])
        side, qty = order['side'], order['qty']
        low, high = self.market.limits(quote) or (None, None)
        if low is None or high is None:
            return 'price_limit_unavailable'
        if side == 'sell':
            if quote.price <= low:
                return 'limit_down_blocks_sell'
            return 't_plus_one_not_sellable' if self._sellable(lots, day) < qty else None
        if quote.price >= high:
            return 'limit_up_blocks_buy'
        if issue := self.market.eligibility(order['symbol'], quote.name):
            return issue
        cash = Decimal(state['cash'])
        if fill * qty + fees > cash:
            return 'insufficient_cash'
        marks, warnings = self._marks(state, day, slot)
        if warnings or None in marks.values():
            return 'cannot_measure_current_equity'
        post_equity = cash + sum(marks.values(), Decimal(0)) - fees - (fill - quote.price) * qty
        post_position = quote.price * (qty + sum(lot['qty'] for lot in lots))
        if post_equity <= 0 or post_position > Decimal('0.25') * post_equity:
            return 'single_stock_over_25_percent'
        return None

    @staticmethod
    def _book(state: dict, order: dict, fill: Decimal, fees: Decimal, day: str) -> str:
        symbol, qty = order['symbol'], order['qty']
        notional, cash = fill * qty, Decimal(state['cash'])
        lots = state['lots'].setdefault(symbol, [])
        if order['side'] == 'buy':
            cost = notional + fees
            lots.append({'qty': qty, 'buy_date': day, 'total_cost': str(cost)})
            state['cash'] = str(money(cash - cost))
            return '0.00'
        cost = Decimal(0)
        for lot in lots:
            if lot['buy_date'] >= day or not qty:
                continue
            sold = min(qty, lot['qty'])
            lot_cost = PaperEngine._lot_cost(lot)
            allocation = money(lot_cost * sold / lot['qty'])
            cost += allocation
            lot['qty'] -= sold
            lot['total_cost'] = str(lot_cost - allocation)
            qty -= sold
        state['lots'][symbol] = [lot for lot in lots if lot['qty']]
        if not state['lots'][symbol]:
            del state['lots'][symbol]
        proceeds = notional - fees
        pnl = money(proceeds - cost)
        if cash + proceeds < 0:
            raise ValueError('negative_cash_after_fees')
        state['cash'] = str(money(cash + proceeds))
        state['realized_pnl'] = str(money(Decimal(state['realized_pnl']) + pnl))
        return str(pnl)

    def settle(self, day: str, slot: str) -> list[dict[str, Any]]:
        if slot not in NEXT_SLOT.values():
            raise ValueError('invalid_settlement_slot')
        state = self.state()
        key = f'{day}/{slot}'
        if key in state['settlements']:
            for trade in state['trades']:
                if trade['day'] == day and trade['slot'] == slot:
                    self._freeze_once({'event': 'paper_fill', **trade})
            return []
        if state['settlements'] and key < state['settlements'][-1]:
            raise ValueError('settlement_out_of_order')
        frame = self.market.frame(day, slot)
        available = _timestamp(getattr(frame, 'decision_at', None))
        if available is None or self._now() < available:
            return [{'status': 'no_snapshot', 'slot': slot}]
        matching = [order for order in state['pending']
                    if (order['day'], order['expected_slot']) == (day, slot)]
        if not matching:
            return []
        if any(f"{order['day']}/{order['expected_slot']}" < key for order in state['pending']):
            raise ValueError('earlier_settlement_pending')
        outcomes, remaining = [], []
        for order in state['pending']:
            if (order['day'], order['expected_slot']) != (day, slot):
                remaining.append(order)
                continue
            symbol, side, qty = order['symbol'], order['side'], order['qty']
            quote = frame.rows.get(symbol)
            issue = self.market.quote_issue(quote, day)
            if issue is None:
                quoted_at = _timestamp(f'{quote.quote_date}T{quote.quote_time}+08:00')
                submitted_at = _timestamp(order['submitted_at'])
                if quoted_at is None or submitted_at is None or quoted_at <= submitted_at:
                    issue = 'next_quote_not_after_decision'
            if issue is None:
                fill = slippage_price(side, quote.price)
                if fill <= 0:
                    issue = 'invalid_price'
                else:
                    fees = fee_breakdown(side, fill, qty)
                    total = sum(fees.values(), Decimal(0))
                    if side == 'sell' and Decimal(state['cash']) + fill * qty < total:
                        issue = 'insufficient_cash_for_fees'
                    else:
                        issue = self._fill_issue(state, order, quote, fill, total, day, slot)
            if issue:
                outcomes.append(self._reject(order, 'settlement_rejected', issue))
                continue
            realized = self._book(state, order, fill, total, day)
            state['marks'][symbol] = {'price': str(quote.price), 'key': key}
            trade = {
                'order_id': order['order_id'], 'day': day, 'slot': slot, 'decision_slot': order['slot'],
                'symbol': symbol, 'side': side, 'qty': qty, 'status': 'paper_fill',
                'raw_price': str(quote.price), 'fill_price': str(fill), 'price_basis': quote.basis,
                'quote_time': quote.quote_time, 'fetched_at': quote.fetched_at, 'source': quote.source,
                'fees': {name: str(amount) for name, amount in fees.items()}, 'total_fees': str(total),
                'notional': str(money(fill * qty)), 'realized_pnl': realized, 'reason': order['reasons'],
            }
            state['trades'].append(trade)
            outcomes.append(trade)
        state['pending'] = remaining
        state['settlements'].append(key)
        self._save(state)
        for trade in outcomes:
            if trade['status'] == 'paper_fill':
                self._freeze_once({'event': 'paper_fill', **trade})
        return outcomes

    def has_pending(self, day: str, slot: str | None = None) -> bool:
        return any(order['day'] == day and (slot is None or order['expected_slot'] == slot)
                   for order in self.state()['pending'])
