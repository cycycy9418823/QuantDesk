from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.market_clock import get_market_session, settlement_status_from_quote
from quantdesk.paper.market_rules import (
    board_type_for_symbol,
    is_valid_order_quantity,
    lot_size_for_symbol,
    paper_only_boundary,
    paper_trade_block_reason,
)
from quantdesk.paper.adjustment_policy import apply_suspension_valuation_policy
from quantdesk.paper.portfolio_contracts import resolve_paper_capital_profile
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs
from quantdesk.shared import safe_float_zero as safe_float
from quantdesk.shared import safe_int_zero as safe_int

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DIR = PROJECT_ROOT / "state"
DEFAULT_PORTFOLIO_PATH = DEFAULT_STATE_DIR / "paper_portfolio.json"
DEFAULT_SNAPSHOT_DIR = DEFAULT_STATE_DIR / "snapshots"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DISCLAIMER = "虚拟投资组合仅用于观察和复盘，不构成投资建议，也不代表真实收益。"
PAPER_COST_CONFIG = TradeCostConfig(slippage_bps=5.0)
PAPER_PORTFOLIO_SCHEMA_VERSION = "paper_portfolio_v1"
_PORTFOLIO_LOCK = threading.RLock()


PAPER_POSITION_COLUMNS = [
    "symbol",
    "name",
    "shares",
    "buy_price",
    "buy_date",
    "last_price",
    "estimated_price",
    "settled_price",
    "estimated_value",
    "settled_value",
    "market_value",
    "unrealized_pnl",
    "unrealized_pnl_pct",
    "quote_date",
    "settlement_date",
    "settlement_status",
    "settlement_message",
    "estimate_only",
    "data_source",
    "data_warning_level",
]


def safe_symbol(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else text


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _trade_date(metadata: dict[str, Any] | None = None) -> str:
    data = dict(metadata or {})
    return _normalize_daily_date(data.get("trade_date") or data.get("date") or now_text()[:10])


def _paper_fees(side: str, amount: float) -> dict[str, Any]:
    costs = compute_trade_costs(side=side, gross=amount, config=PAPER_COST_CONFIG)
    return {
        "commission": float(costs["commission"]),
        "stamp_duty": float(costs["stamp_duty"]),
        "transfer_fee": float(costs["transfer_fee"]),
        "slippage": float(costs["slippage"]),
        "total_fee": float(costs["total_fee"]),
        "fee_model": str(costs["fee_model"]),
    }


def portfolio_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_PORTFOLIO_PATH


def _paper_portfolio_store(path: Path | str | None = None) -> PaperStateStore:
    return PaperStateStore(portfolio_path(path), schema_version=PAPER_PORTFOLIO_SCHEMA_VERSION, max_backups=7)


def _backup_bad_json(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.stem}.bad_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.bad_{datetime.now():%Y%m%d_%H%M%S}_{counter}{path.suffix}")
        counter += 1
    try:
        path.replace(backup)
    except OSError:
        return None
    return backup


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _account_from_profile(profile: Any, cash: float | None = None) -> dict[str, Any]:
    balance = float(profile.initial_capital if cash is None else cash)
    return {
        "initial_cash": float(profile.initial_capital),
        "cash": balance,
        "currency": profile.currency,
        "profile_id": profile.profile_id,
        "scenario_id": profile.scenario_id,
        "capital_profile_label": profile.visible_label,
        "legacy_state_preserved": profile.legacy_state_preserved,
        "paper_credit_enabled": profile.paper_credit_enabled,
        "credit_limit": profile.credit_limit,
        "credit_used": 0.0,
        "simulated_interest": 0.0,
    }


def create_default_paper_portfolio(
    initial_cash: float | None = None,
    *,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    profile = resolve_paper_capital_profile(
        scenario_id=scenario_id,
        requested_initial_capital=initial_cash,
    )
    portfolio = {
        "account": _account_from_profile(profile),
        "positions": [],
        "trades": [],
        "paper_trading_only": True,
        "schema_version": PAPER_PORTFOLIO_SCHEMA_VERSION,
        "disclaimer": DISCLAIMER,
        "created_at": now_text(),
        "updated_at": "",
        **paper_only_boundary(),
    }
    portfolio["summary"] = summarize_paper_portfolio(portfolio)
    return portfolio


def load_paper_portfolio(path: Path | str | None = None) -> dict[str, Any]:
    target = portfolio_path(path)
    with _PORTFOLIO_LOCK:
        if not target.exists():
            return create_default_paper_portfolio()
        try:
            data = _paper_portfolio_store(target).read(default=create_default_paper_portfolio())
        except OSError:
            data = create_default_paper_portfolio()
            data["summary"]["error"] = "paper portfolio file is unreadable"
            return data
    if not isinstance(data, dict):
        data = create_default_paper_portfolio()
        data["summary"]["error"] = "paper portfolio file is invalid"
        return data
    data.setdefault("account", _account_from_profile(resolve_paper_capital_profile()))
    profile = resolve_paper_capital_profile(
        existing_state=data,
        existing_state_verified=True,
    )
    account = data["account"]
    account.setdefault("profile_id", profile.profile_id)
    account.setdefault("scenario_id", profile.scenario_id)
    account.setdefault("capital_profile_label", profile.visible_label)
    account.setdefault("legacy_state_preserved", profile.legacy_state_preserved)
    account.setdefault("paper_credit_enabled", profile.paper_credit_enabled)
    account.setdefault("credit_limit", profile.credit_limit)
    account.setdefault("credit_used", 0.0)
    account.setdefault("simulated_interest", 0.0)
    data.setdefault("positions", [])
    data.setdefault("trades", [])
    data.setdefault("schema_version", PAPER_PORTFOLIO_SCHEMA_VERSION)
    data.setdefault("paper_trading_only", True)
    data.setdefault("disclaimer", DISCLAIMER)
    # Hard-set (not setdefault) so a stale on-disk is_real_order=true from
    # legacy/corrupted state is force-corrected, never trusted.
    for _bk, _bv in paper_only_boundary().items():
        data[_bk] = _bv
    for _collection in ("positions", "trades"):
        for _row in data.get(_collection, []) or []:
            if isinstance(_row, dict):
                for _bk, _bv in paper_only_boundary().items():
                    _row[_bk] = _bv
    data["summary"] = summarize_paper_portfolio(data)
    if data.get("state_recovery_warning"):
        data["summary"]["error"] = "paper portfolio file is unreadable"
        data["summary"]["recovery_warning"] = data.get("state_recovery_warning")
    if data.get("state_recovered_from_backup"):
        data["summary"]["state_recovered_from_backup"] = True
        data["summary"]["state_recovery_source"] = data.get("state_recovery_source", "")
    return data


def save_paper_portfolio(portfolio: dict[str, Any], path: Path | str | None = None) -> None:
    target = portfolio_path(path)
    with _PORTFOLIO_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        portfolio["updated_at"] = now_text()
        portfolio["paper_trading_only"] = True
        portfolio.setdefault("schema_version", PAPER_PORTFOLIO_SCHEMA_VERSION)
        portfolio.setdefault("disclaimer", DISCLAIMER)
        portfolio["summary"] = summarize_paper_portfolio(portfolio)
        _paper_portfolio_store(target).write(portfolio)


def save_paper_snapshot(
    portfolio: dict[str, Any] | None = None,
    path: Path | str | None = None,
    snapshot_dir: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Save a local paper-portfolio snapshot for later review.

    Snapshots are stored under state/snapshots and are never part of the clean
    export. They are paper-review artifacts only, not trading records.
    """
    current = now or datetime.now()
    source = load_paper_portfolio(path) if portfolio is None else dict(portfolio)
    source["summary"] = summarize_paper_portfolio(source)
    source["snapshot_metadata"] = {
        "saved_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "market_session": get_market_session(current),
        "snapshot_date": current.date().isoformat(),
        "paper_trading_only": True,
        "disclaimer": DISCLAIMER,
    }
    target_dir = Path(snapshot_dir) if snapshot_dir is not None else DEFAULT_SNAPSHOT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"portfolio_{current:%Y%m%d_%H%M%S}.json"
    with _PORTFOLIO_LOCK:
        _atomic_write_json(target, source)
    return {
        "ok": True,
        "message": "虚拟持仓快照已保存；仅用于复盘观察，不是真实交易记录。",
        "snapshot_path": str(target),
        "snapshot_name": target.name,
        "summary": source.get("summary", {}),
        "disclaimer": DISCLAIMER,
    }


def list_paper_snapshots(snapshot_dir: Path | str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    target_dir = Path(snapshot_dir) if snapshot_dir is not None else DEFAULT_SNAPSHOT_DIR
    if not target_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(target_dir.glob("portfolio_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summary = data.get("summary") or summarize_paper_portfolio(data if isinstance(data, dict) else {})
        metadata = data.get("snapshot_metadata") or {}
        rows.append(
            {
                "name": path.name,
                "saved_at": metadata.get("saved_at") or datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "market_session": metadata.get("market_session", ""),
                "snapshot_date": metadata.get("snapshot_date", ""),
                "position_count": summary.get("position_count", 0),
                "total_equity": summary.get("total_equity", 0),
                "settlement_status": summary.get("settlement_status", ""),
                "paper_trading_only": True,
                "disclaimer": DISCLAIMER,
            }
        )
    return rows


def _trade(
    symbol: str,
    name: str,
    side: str,
    price: float,
    shares: int,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    timestamp = now_text()
    action_type = metadata.get("action_type") or ("virtual_entry" if side == "BUY" else "virtual_exit")
    action_label = metadata.get("action_label") or ("虚拟建仓" if side == "BUY" else "虚拟离场")
    amount = float(price) * int(shares)
    trade = {
        "trade_id": metadata.get("trade_id") or f"{timestamp.replace('-', '').replace(':', '').replace(' ', '_')}_{safe_symbol(symbol)}_{side}",
        "trade_time": timestamp,
        "date": timestamp,
        "symbol": safe_symbol(symbol),
        "name": str(name or ""),
        "side": side,
        "action_type": action_type,
        "action_label": action_label,
        "strategy_name": metadata.get("strategy_name", ""),
        "strategy_version": metadata.get("strategy_version", ""),
        "source": metadata.get("source", "manual_paper"),
        "price": float(price),
        "shares": int(shares),
        "quantity": int(shares),
        "amount": amount,
        "gross_amount": metadata.get("gross_amount", amount),
        "commission": metadata.get("commission", 0.0),
        "stamp_duty": metadata.get("stamp_duty", 0.0),
        "transfer_fee": metadata.get("transfer_fee", 0.0),
        "slippage": metadata.get("slippage", 0.0),
        "total_fee": metadata.get("total_fee", 0.0),
        "fee_model": metadata.get("fee_model", ""),
        "cash_change": metadata.get("cash_change", -amount if side == "BUY" else amount),
        "position_change": metadata.get("position_change", int(shares) if side == "BUY" else -int(shares)),
        "trade_date": metadata.get("trade_date", timestamp[:10]),
        "quote_date": metadata.get("quote_date", ""),
        "daily_bar_date": metadata.get("daily_bar_date", ""),
        "feature_freshness": metadata.get("feature_freshness", ""),
        "estimate_only": bool(metadata.get("estimate_only", True)),
        "settlement_status": metadata.get("settlement_status", ""),
        "matched_conditions": list(metadata.get("matched_conditions") or []),
        "failed_conditions": list(metadata.get("failed_conditions") or []),
        "missing_fields": list(metadata.get("missing_fields") or []),
        "risk_notes": list(metadata.get("risk_notes") or []),
        "market_regime": metadata.get("market_regime", ""),
        "fundamental_status": metadata.get("fundamental_status", ""),
        "data_health_status": metadata.get("data_health_status", ""),
        "manual_note": metadata.get("manual_note", ""),
        "notes": str(notes or ""),
        "paper_trading_only": True,
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }
    return trade


def _truthy_metadata(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "suspended", "停牌"}


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata_has_trade_gate_fields(metadata: dict[str, Any]) -> bool:
    return any(
        key in metadata and metadata.get(key) not in (None, "")
        for key in ("pct_chg", "change_pct", "suspended", "is_suspended")
    )


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _normalize_daily_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = text.replace("-", "").replace("/", "").replace(".", "")[:8]
    try:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _augment_trade_metadata_from_local_daily(symbol: str, metadata: dict[str, Any], trade_date: str) -> dict[str, Any]:
    if _metadata_has_trade_gate_fields(metadata):
        return metadata
    path = DAILY_DIR / f"{safe_symbol(symbol)}.csv"
    if not path.exists():
        return metadata
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return metadata
    dated_rows: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row_date = _normalize_daily_date(_first_present(row, ("date", "trade_date", "日期")))
        if not row_date:
            continue
        row["_qd_date"] = row_date
        dated_rows.append(row)
    if not dated_rows:
        return metadata
    target_date = _normalize_daily_date(trade_date)
    if str(trade_date or "").strip() and not target_date:
        return metadata
    dated_rows.sort(key=lambda item: str(item.get("_qd_date") or ""))
    if target_date:
        eligible_indexes = [idx for idx, row in enumerate(dated_rows) if str(row.get("_qd_date") or "") <= target_date]
        if not eligible_indexes:
            # Fail closed when the requested paper-trade date predates the
            # available local history. Falling back to the latest row here
            # would leak future suspension/limit information into the past.
            return metadata
        selected_index = eligible_indexes[-1]
    else:
        selected_index = len(dated_rows) - 1
    row = dated_rows[selected_index]
    pct_chg = _optional_float(_first_present(row, ("pct_chg", "change_pct", "涨跌幅", "涨跌幅%")))
    if pct_chg is None and selected_index > 0:
        close = _optional_float(_first_present(row, ("close", "收盘", "收盘价")))
        prev_close = _optional_float(_first_present(dated_rows[selected_index - 1], ("close", "收盘", "收盘价")))
        if close is not None and prev_close and prev_close > 0:
            pct_chg = (close / prev_close - 1.0) * 100.0
    if pct_chg is not None:
        metadata.setdefault("pct_chg", pct_chg)
        metadata.setdefault("daily_bar_date", row.get("_qd_date") or "")
    volume = _optional_float(_first_present(row, ("vol", "volume", "成交量")))
    if volume is not None and volume <= 0:
        metadata.setdefault("suspended", True)
        metadata.setdefault("daily_bar_date", row.get("_qd_date") or "")
    return metadata


def _find_position(portfolio: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    normalized = safe_symbol(symbol)
    for position in portfolio.get("positions", []) or []:
        if safe_symbol(position.get("symbol")) == normalized:
            return position
    return None


def _available_shares_for_sell(position: dict[str, Any], trade_date: str) -> int:
    held = safe_int(position.get("shares"))
    normalized_trade_date = _normalize_daily_date(trade_date)
    if not normalized_trade_date:
        return 0
    raw_buy_date = str(position.get("buy_date") or "").strip()
    buy_date = _normalize_daily_date(raw_buy_date)
    if raw_buy_date and not buy_date:
        return 0
    if buy_date and buy_date >= normalized_trade_date:
        return min(held, safe_int(position.get("available_shares")))
    return held


def _error(message: str, portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"ok": False, "message": message, "disclaimer": DISCLAIMER, **paper_only_boundary()}
    if portfolio is not None:
        payload["portfolio"] = portfolio
    return payload


def paper_buy(
    symbol: str,
    name: str,
    price: float,
    shares: int,
    stop_loss: float | None = None,
    notes: str | None = None,
    path: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    portfolio = load_paper_portfolio(path)
    symbol = safe_symbol(symbol)
    price = safe_float(price)
    shares = safe_int(shares)
    if not symbol:
        return _error("股票代码不能为空。", portfolio)
    if price <= 0:
        return _error("虚拟买入价格必须大于 0。", portfolio)
    board_lot = lot_size_for_symbol(symbol, metadata=metadata, name=name)
    board_type = board_type_for_symbol(symbol, metadata=metadata, name=name)
    if not is_valid_order_quantity(shares, lot_size=board_lot, board_type=board_type):
        return _error(f"A 股虚拟买入股数必须满足该股票板块整手规则（最低 {board_lot} 股）。", portfolio)

    metadata = dict(metadata or {})
    trade_date = _trade_date(metadata)
    if not trade_date:
        return _error("纸面交易日期无效，未修改纸面账户。", portfolio)
    metadata = _augment_trade_metadata_from_local_daily(symbol, metadata, trade_date)
    blocked_reason = paper_trade_block_reason("BUY", metadata, symbol=symbol, name=name)
    if blocked_reason:
        return _error(blocked_reason, portfolio)
    amount = price * shares
    fees = _paper_fees("BUY", amount)
    cash_out = amount + fees["total_fee"]
    account = portfolio.setdefault(
        "account",
        _account_from_profile(resolve_paper_capital_profile()),
    )
    cash = safe_float(account.get("cash"))
    if cash_out > cash:
        return _error("虚拟账户现金不足。", portfolio)

    existing = _find_position(portfolio, symbol)
    if existing:
        old_shares = safe_int(existing.get("shares"))
        old_cost = old_shares * safe_float(existing.get("buy_price"))
        available = _available_shares_for_sell(existing, trade_date)
        new_shares = old_shares + shares
        existing["shares"] = new_shares
        existing["buy_price"] = (old_cost + amount) / new_shares if new_shares else price
        existing["buy_date"] = trade_date
        existing["available_shares"] = min(available, new_shares)
        existing["name"] = name or existing.get("name", "")
        if stop_loss is not None:
            existing["stop_loss"] = safe_float(stop_loss)
    else:
        portfolio.setdefault("positions", []).append(
            {
                "symbol": symbol,
                "name": str(name or ""),
                "shares": shares,
                "buy_price": price,
                "buy_date": trade_date,
                "available_shares": 0,
                "stop_loss": safe_float(stop_loss) if stop_loss is not None else "",
                "notes": str(notes or ""),
                "estimate_only": True,
                "settlement_status": "no_quote",
                "settlement_message": "当前报告缺少该股票行情，无法更新虚拟持仓。",
            }
        )

    account["cash"] = cash - cash_out
    metadata.update({**fees, "gross_amount": amount, "cash_change": -cash_out, "trade_date": trade_date})
    portfolio.setdefault("trades", []).append(_trade(symbol, name, "BUY", price, shares, notes, metadata))
    save_paper_portfolio(portfolio, path)
    return {
        "ok": True,
        "message": "虚拟买入已记录；这不会真实下单，也不构成投资建议。",
        "portfolio": load_paper_portfolio(path),
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }


def paper_sell(
    symbol: str,
    price: float,
    shares: int | None = None,
    notes: str | None = None,
    path: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    portfolio = load_paper_portfolio(path)
    symbol = safe_symbol(symbol)
    price = safe_float(price)
    if not symbol:
        return _error("股票代码不能为空。", portfolio)
    if price <= 0:
        return _error("虚拟卖出价格必须大于 0。", portfolio)
    position = _find_position(portfolio, symbol)
    if not position:
        return _error("虚拟组合中没有该股票持仓。", portfolio)
    held = safe_int(position.get("shares"))
    sell_shares = held if shares is None else safe_int(shares)
    board_lot = lot_size_for_symbol(symbol, metadata=metadata, name=str(position.get("name") or ""))
    board_type = board_type_for_symbol(symbol, metadata=metadata, name=str(position.get("name") or ""))
    if not is_valid_order_quantity(sell_shares, lot_size=board_lot, board_type=board_type, held_shares=held):
        return _error(f"A 股虚拟卖出股数必须满足该股票板块整手规则（最低 {board_lot} 股，或一次性清掉不足一手的余额）。", portfolio)
    if sell_shares > held:
        return _error("不能虚拟卖出超过当前持仓的股数。", portfolio)
    metadata = dict(metadata or {})
    trade_date = _trade_date(metadata)
    if not trade_date:
        return _error("纸面交易日期无效，未修改纸面账户。", portfolio)
    metadata = _augment_trade_metadata_from_local_daily(symbol, metadata, trade_date)
    blocked_reason = paper_trade_block_reason("SELL", metadata, symbol=symbol, name=str(position.get("name") or ""))
    if blocked_reason:
        return _error(blocked_reason, portfolio)
    available_shares = _available_shares_for_sell(position, trade_date)
    if sell_shares > available_shares:
        return _error("A 股虚拟持仓按 T+1 规则，当日买入部分不可在同一交易日卖出。", portfolio)

    account = portfolio.setdefault(
        "account",
        _account_from_profile(resolve_paper_capital_profile()),
    )
    gross_amount = price * sell_shares
    fees = _paper_fees("SELL", gross_amount)
    cash_in = gross_amount - fees["total_fee"]
    account["cash"] = safe_float(account.get("cash")) + cash_in
    position["shares"] = held - sell_shares
    position["available_shares"] = max(0, available_shares - sell_shares)
    if position["shares"] <= 0:
        portfolio["positions"] = [
            item for item in portfolio.get("positions", []) if safe_symbol(item.get("symbol")) != symbol
        ]
    metadata.update({**fees, "gross_amount": gross_amount, "cash_change": cash_in, "trade_date": trade_date})
    portfolio.setdefault("trades", []).append(_trade(symbol, position.get("name", ""), "SELL", price, sell_shares, notes, metadata))
    save_paper_portfolio(portfolio, path)
    return {
        "ok": True,
        "message": "虚拟卖出已记录；这不会真实下单，也不构成投资建议。",
        "portfolio": load_paper_portfolio(path),
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }


def reset_paper_portfolio(
    initial_cash: float | None = None,
    path: Path | str | None = None,
    *,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    portfolio = create_default_paper_portfolio(initial_cash, scenario_id=scenario_id)
    save_paper_portfolio(portfolio, path)
    return {"ok": True, "message": "虚拟组合已重置。", "portfolio": portfolio, "disclaimer": DISCLAIMER}


def quote_map_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = safe_symbol(row.get("code") or row.get("symbol"))
        if not symbol:
            continue
        quotes[symbol] = {
            "price": row.get("close") or row.get("price") or row.get("last_price") or row.get("estimated_price"),
            "quote_date": row.get("date") or row.get("quote_date") or row.get("latest_quote_date"),
            "source": row.get("data_effective_source") or row.get("source") or row.get("data_source") or "report",
            "name": row.get("name"),
            "data_warning_level": row.get("data_warning_level") or row.get("warning_level"),
        }
    return quotes


def update_paper_marks(
    portfolio: dict[str, Any],
    latest_prices: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    updated_positions: list[dict[str, Any]] = []
    waiting_close = 0
    waiting_quote = 0
    settled = 0
    latest_quote_date = ""
    expected_settlement_date = ""

    for position in portfolio.get("positions", []) or []:
        row = dict(position)
        symbol = safe_symbol(row.get("symbol") or row.get("code"))
        row["symbol"] = symbol
        quote = latest_prices.get(symbol)
        shares = safe_int(row.get("shares"))
        buy_price = safe_float(row.get("buy_price"))

        if not quote:
            status = settlement_status_from_quote(None, now)
            row["settlement_status"] = "no_quote"
            row["settlement_message"] = "当前报告缺少该股票行情，无法更新虚拟持仓。"
            row["quote_date"] = ""
            row["estimate_only"] = True
            row["estimated_price"] = safe_float(row.get("estimated_price") or row.get("last_price") or buy_price)
            row["estimated_value"] = shares * safe_float(row.get("estimated_price"))
            row["settled_value"] = shares * safe_float(row.get("settled_price"), buy_price)
            row["market_value"] = row["estimated_value"]
            expected_settlement_date = str(status.get("expected_settlement_date") or expected_settlement_date)
            waiting_quote += 1
        else:
            price = safe_float(quote.get("price"), safe_float(row.get("last_price") or row.get("settled_price") or buy_price))
            status = settlement_status_from_quote(quote.get("quote_date"), now)
            latest_quote_date = max(latest_quote_date, str(status.get("quote_date") or ""))
            expected_settlement_date = str(status.get("expected_settlement_date") or expected_settlement_date)
            row["name"] = quote.get("name") or row.get("name") or ""
            row["quote_date"] = status.get("quote_date", "")
            row["data_source"] = quote.get("source") or row.get("data_source") or ""
            row["data_warning_level"] = quote.get("data_warning_level") or row.get("data_warning_level") or ""
            row["settlement_status"] = status["status"]
            row["settlement_message"] = status["message"]
            row["estimated_price"] = price
            row["estimated_value"] = shares * price
            if status["can_settle"]:
                row["estimate_only"] = False
                row["settled_price"] = price
                row["last_price"] = price
                row["settled_value"] = shares * price
                row["market_value"] = shares * price
                row["settlement_date"] = status.get("quote_date", "")
                settled += 1
            else:
                row["estimate_only"] = True
                row["last_price"] = row.get("last_price") or row.get("settled_price") or price
                row["settled_value"] = shares * safe_float(row.get("settled_price"), buy_price)
                row["market_value"] = row["estimated_value"]
                if status["status"] == "waiting_for_close":
                    waiting_close += 1
                else:
                    waiting_quote += 1

        if quote:
            row = apply_suspension_valuation_policy(row, quote)
            if row.get("valuation_status") == "suspended_frozen":
                row["settlement_status"] = "suspended_frozen"
                row["settlement_message"] = "Position is suspended; paper valuation is frozen at the last tradable price."
                row["estimate_only"] = True
                row["estimated_price"] = safe_float(row.get("latest_price") or row.get("last_price") or row.get("settled_price") or buy_price)
                row["estimated_value"] = round(shares * safe_float(row.get("estimated_price")), 2)
                row["settled_value"] = shares * safe_float(row.get("settled_price"), buy_price)
                row["market_value"] = row["estimated_value"]

        # A stale quote (quote_date older than the expected settlement date) must
        # never be presented as a normal, trustworthy valuation: label it as an
        # estimate and fail closed on tradeability so no paper fill is assumed.
        if row.get("settlement_status") == "quote_stale" and row.get("valuation_status") != "suspended_frozen":
            row["valuation_status"] = "stale_quote_estimate"
            row["valuation_price_source"] = "stale_quote_price"
            row["estimate_only"] = True
            row["tradeability_status"] = "quote_stale"
            row["tradable"] = False
            row["can_sell"] = False
            row.setdefault("valuation_warning", "quote_stale: valuation is a stale-quote estimate, not a live price")

        cost = shares * buy_price
        reference_value = safe_float(row.get("market_value"))
        row["unrealized_pnl"] = reference_value - cost
        row["unrealized_pnl_pct"] = (row["unrealized_pnl"] / cost * 100) if cost else 0.0
        updated_positions.append(row)

    portfolio["positions"] = updated_positions
    summary = summarize_paper_portfolio(portfolio)
    summary.update(
        {
            "latest_quote_date": latest_quote_date,
            "expected_settlement_date": expected_settlement_date,
            "positions_waiting_for_close": waiting_close,
            "positions_waiting_for_quote_update": waiting_quote,
            "positions_settled": settled,
        }
    )
    if waiting_close:
        summary["settlement_status"] = "waiting_for_close"
        summary["settlement_message"] = "已更新盘中估算值，等待今日收盘后再确认结算。"
    elif waiting_quote:
        summary["settlement_status"] = "waiting_for_quote_update"
        summary["settlement_message"] = "今日已收盘或需要最近交易日数据，但行情源尚未更新到预期结算日。"
    elif settled:
        summary["settlement_status"] = "settled"
        summary["settlement_message"] = "虚拟组合已按可用收盘行情更新结算值。"
    else:
        summary["settlement_status"] = "no_positions"
        summary["settlement_message"] = "虚拟组合暂无持仓。"
    portfolio["summary"] = summary
    portfolio["paper_trading_only"] = True
    portfolio["disclaimer"] = DISCLAIMER
    portfolio["updated_at"] = now_text()
    return portfolio


def summarize_paper_portfolio(portfolio: dict[str, Any]) -> dict[str, Any]:
    account = portfolio.get("account") or {}
    initial_cash = safe_float(account.get("initial_cash"), 10000.0)
    cash = safe_float(account.get("cash"), initial_cash)
    positions = portfolio.get("positions") or []
    market_value = sum(safe_float(position.get("market_value") or position.get("estimated_value")) for position in positions)
    total_equity = cash + market_value
    total_pnl = total_equity - initial_cash
    total_pnl_pct = (total_pnl / initial_cash * 100) if initial_cash else 0.0
    previous = dict(portfolio.get("summary") or {})
    return {
        "initial_cash": initial_cash,
        "cash": cash,
        "total_market_value": market_value,
        "total_equity": total_equity,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "position_count": len(positions),
        "updated_at": portfolio.get("updated_at", ""),
        "settlement_status": previous.get("settlement_status", "no_positions" if not positions else "not_refreshed"),
        "settlement_message": previous.get("settlement_message", "虚拟投资组合仅用于观察和复盘，不是真实交易。"),
        "latest_quote_date": previous.get("latest_quote_date", ""),
        "expected_settlement_date": previous.get("expected_settlement_date", ""),
        "positions_waiting_for_close": safe_int(previous.get("positions_waiting_for_close")),
        "positions_waiting_for_quote_update": safe_int(previous.get("positions_waiting_for_quote_update")),
        "positions_settled": safe_int(previous.get("positions_settled")),
        "paper_trading_only": True,
        "disclaimer": DISCLAIMER,
    }
