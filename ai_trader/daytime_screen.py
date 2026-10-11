"""Daytime screen builder for the AI paper trader (A1 · S1).

Read-only by construction: this module composes what a model looks at for one
5-minute slot.  It never writes the ledger, the plan, the watchlist, or any runtime
state, and it never claims a basis it cannot prove.

Input priority (printed in the header so a reader can never confuse them):

1. ``data/snapshots/<date>/<slot>.csv`` — the immutable A0 slot;
2. ``data/cache/spot_snapshot_latest.csv`` — the overwritable cache (labelled
   ``local_spot`` and marked as *not* fill evidence).

Columns the feed does not carry are reported as missing instead of invented; the
volume-ratio section is therefore an explicit proxy (this slot's cumulative volume
against the previous slot's) and says so in its own label.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_trader.ledger import load_ledger, position_map
from ai_trader.paths import PROJECT_ROOT, CyclePaths, assert_not_quantdesk_ledger, profile_paths
from ai_trader.watchlist import load_watchlist
from quantdesk.intraday_refresh.snapshot import SPOT_RELATIVE

SCHEMA = "ai_trader_daytime_screen_v1"
SLOTS_RELATIVE = Path("data/snapshots")
BENCHMARK_RELATIVE = Path("data/benchmark")
MAX_TEXT_CHARS = 3000
TOP_N = 20
INDEX_FILES = (("hs300.csv", "沪深300"), ("zz500.csv", "中证500"), ("sh000001.csv", "上证指数"))
INDEX_NAME_HINTS = ("指数", "成指", "创业板指")


def _fnum(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def _symbol(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "").strip()
    return symbol if len(symbol) == 6 and symbol.isdigit() else ""


def _valid_quote(row: dict[str, Any]) -> bool:
    price = _fnum(row.get("price"))
    return price is not None and price > 0


def slot_label(moment: datetime) -> str:
    """The A0 slot a capture at ``moment`` belongs to (start-of-slot label)."""

    minutes = moment.hour * 60 + moment.minute
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        if start <= minutes < end:
            floored = minutes - (minutes - start) % 5
            return f"{floored // 60:02d}{floored % 60:02d}"
    return ""


def _day_dir(root: Path, as_of: str) -> Path:
    return root / SLOTS_RELATIVE / str(as_of)[:10]


def latest_slot(root: Path, as_of: str, moment: datetime) -> tuple[str, Path]:
    """Newest immutable slot of the day that is not in the future."""

    day = _day_dir(root, as_of)
    if not day.is_dir():
        return "", day
    current = slot_label(moment)
    candidates = sorted(path.stem for path in day.glob("*.csv") if len(path.stem) == 4 and path.stem.isdigit())
    if current:
        candidates = [slot for slot in candidates if slot <= current] or candidates
    if not candidates:
        return "", day
    return candidates[-1], day / f"{candidates[-1]}.csv"


def _slot_meta(path: Path) -> dict[str, Any]:
    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _indices(rows: list[dict[str, Any]], root: Path) -> tuple[list[dict[str, Any]], str]:
    """Index levels from the slot itself, else the daily benchmark files (labelled)."""

    out: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "")
        if any(hint in name for hint in INDEX_NAME_HINTS):
            price = _fnum(row.get("price"))
            if price is not None:
                out.append({"symbol": str(row.get("symbol") or ""), "name": name, "price": price,
                            "pct_chg": _fnum(row.get("pct_chg")), "basis": "slot"})
    if out:
        return out[:5], "slot"
    for file_name, label in INDEX_FILES:
        bench = _read_rows(root / BENCHMARK_RELATIVE / file_name)
        if not bench:
            continue
        last = bench[-1]
        out.append({"symbol": file_name, "name": label, "price": _fnum(last.get("close")),
                    "date": str(last.get("date") or ""), "basis": "benchmark_daily"})
    return out, ("benchmark_daily" if out else "missing")


def _top(rows: list[dict[str, Any]], key: str, *, reverse: bool = True) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if not _symbol(row) or not _valid_quote(row):
            continue
        value = _fnum(row.get(key))
        if value is None:
            continue
        scored.append((value, row))
    scored.sort(key=lambda item: item[0], reverse=reverse)
    return [
        {"symbol": _symbol(row), "name": str(row.get("name") or ""), "price": _fnum(row.get("price")),
         "pct_chg": _fnum(row.get("pct_chg")), "value": value}
        for value, row in scored[:TOP_N]
    ]


def _volume_ratio_proxy(current: list[dict[str, Any]], previous: list[dict[str, Any]],
                        ) -> list[dict[str, Any]]:
    """Slot-over-slot cumulative-volume ratio; a proxy, since the feed has no 量比 column."""

    base: dict[str, float] = {}
    for row in previous:
        symbol = _symbol(row)
        volume = _fnum(row.get("volume"))
        if symbol and volume and volume > 0:
            base[symbol] = volume
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in current:
        symbol = _symbol(row)
        volume = _fnum(row.get("volume"))
        if not symbol or not volume or symbol not in base:
            continue
        scored.append((volume / base[symbol], row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"symbol": _symbol(row), "name": str(row.get("name") or ""), "value": ratio,
         "price": _fnum(row.get("price")), "pct_chg": _fnum(row.get("pct_chg"))}
        for ratio, row in scored[:TOP_N]
    ]


def _slot_movers(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Price move between two adjacent immutable slots."""

    base: dict[str, float] = {}
    for row in previous:
        symbol = _symbol(row)
        price = _fnum(row.get("price"))
        if symbol and price and price > 0:
            base[symbol] = price
    scored: list[tuple[float, float, float, str, str]] = []
    for row in current:
        symbol = _symbol(row)
        price = _fnum(row.get("price"))
        if not symbol or price is None or price <= 0 or symbol not in base:
            continue
        origin = base[symbol]
        move = (price - origin) / origin * 100
        scored.append((move, origin, price, symbol, str(row.get("name") or "")))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"symbol": symbol, "name": name, "from": origin, "to": price, "move_pct": move}
        for move, origin, price, symbol, name in scored[:TOP_N]
    ]


def _positions(ledger: dict[str, Any], rows_by_symbol: dict[str, dict[str, Any]],
               as_of: str) -> list[dict[str, Any]]:
    try:
        held = position_map(ledger)
    except Exception:  # pragma: no cover - defensive fallback
        held = {str(item.get("symbol")): item for item in (ledger.get("positions") or [])
                if isinstance(item, dict)}
    out: list[dict[str, Any]] = []
    for symbol, position in sorted(held.items()):
        quote = rows_by_symbol.get(symbol) or {}
        acquired = str(position.get("acquired_on") or "")[:10]
        try:
            held_days = (datetime.fromisoformat(as_of) - datetime.fromisoformat(acquired)).days
        except ValueError:
            held_days = None
        out.append({"symbol": symbol, "quantity": position.get("quantity"),
                    "avg_cost": position.get("avg_cost"), "price": _fnum(quote.get("price")),
                    "pct_chg": _fnum(quote.get("pct_chg")), "held_days": held_days,
                    "thesis": str(position.get("thesis") or "")[:80]})
    return out


def _lines_for_today(path: Path, as_of: str) -> list[dict[str, Any]]:
    target = assert_not_quantdesk_ledger(path)
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and (
            str(record.get("as_of") or record.get("date") or "")[:10] == str(as_of)[:10]
        ):
            out.append(record)
    return out


def build_screen(
    project_root: Path | None = None,
    *,
    profile: str | None = None,
    now: datetime | None = None,
    paths: CyclePaths | None = None,
    slot_rows: list[dict[str, Any]] | None = None,
    previous_rows: list[dict[str, Any]] | None = None,
    slot: str = "",
) -> dict[str, Any]:
    """Compose one slot's screen. Never writes anything."""

    root = Path(project_root or PROJECT_ROOT).resolve()
    runtime = paths or profile_paths(profile)
    current = now or datetime.now()
    as_of = current.strftime("%Y-%m-%d")
    warnings: list[str] = []

    if slot_rows is None:
        slot_guess, slot_path = latest_slot(root, as_of, current)
        rows = _read_rows(slot_path) if slot_guess else []
        if rows:
            slot, source, source_path = slot_guess, "cloud_slot", slot_path
            meta = _slot_meta(slot_path)
            snapshot_time = str(meta.get("fetched_at") or "")
            if not snapshot_time:
                snapshot_time = datetime.fromtimestamp(
                    slot_path.stat().st_mtime).isoformat(timespec="seconds")
        else:
            spot = root / SPOT_RELATIVE
            rows = _read_rows(spot)
            source, source_path = "local_spot", spot
            snapshot_time = (datetime.fromtimestamp(spot.stat().st_mtime).isoformat(timespec="seconds")
                             if spot.is_file() else "")
            if not rows:
                warnings.append("no_slot_and_no_local_spot")
    else:
        rows, source, source_path, snapshot_time = list(slot_rows), "injected", Path("<injected>"), ""

    previous_slot = ""
    if previous_rows is None and slot:
        day = _day_dir(root, as_of)
        earlier = sorted(path.stem for path in day.glob("*.csv")
                         if len(path.stem) == 4 and path.stem.isdigit() and path.stem < slot)
        if earlier:
            previous_rows = _read_rows(day / f"{earlier[-1]}.csv")
            previous_slot = earlier[-1]
    previous_rows = previous_rows or []

    rows_by_symbol = {_symbol(row): row for row in rows if _symbol(row)}
    valid_quotes = sum(1 for row in rows if _valid_quote(row))
    indices, index_basis = _indices(rows, root)
    if index_basis == "missing":
        warnings.append("index_rows_missing")
    ledger = _load_json(runtime.ledger)
    plan = _load_json(runtime.plan_latest)
    watch = _load_json(runtime.watchlist)
    watch_items = watch.get("items") if isinstance(watch.get("items"), list) else []
    trades_today = [trade for trade in (ledger.get("trades") or [])
                    if isinstance(trade, dict) and str(trade.get("date") or "")[:10] == as_of]
    intent_records = _lines_for_today(runtime.daytime_intents, as_of)
    journal = _lines_for_today(runtime.daytime_notes.parent / "daytime_journal.jsonl", as_of)
    blocked = [record for record in journal if record.get("blocked_by")]
    pending = [record for record in intent_records if str(record.get("status") or "") == "pending"]

    return {
        "schema_version": SCHEMA,
        "as_of": as_of,
        "slot": slot,
        "source": source,
        "source_path": str(source_path),
        "snapshot_time": snapshot_time,
        "coverage": {"rows": len(rows), "valid_quotes": valid_quotes,
                     "ratio": (valid_quotes / len(rows)) if rows else None},
        "indices": indices,
        "index_basis": index_basis,
        "positions": _positions(ledger, rows_by_symbol, as_of),
        "plan_intents": [item for item in (plan.get("intents") or []) if isinstance(item, dict)],
        "watchlist": [item for item in watch_items if isinstance(item, dict)],
        "top": {"gainers": _top(rows, "pct_chg", reverse=True),
                "losers": _top(rows, "pct_chg", reverse=False),
                "amount": _top(rows, "amount", reverse=True),
                "volume_ratio_proxy": _volume_ratio_proxy(rows, previous_rows)},
        "slot_movers": _slot_movers(rows, previous_rows),
        "previous_slot": previous_slot,
        "today": {"fills": trades_today, "pending_intents": pending, "blocked": blocked},
        "warnings": warnings,
    }


def _fmt(value: Any, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _render(screen: dict[str, Any], lists: int) -> str:
    head = (f"[屏] {screen['as_of']} 槽 {screen.get('slot') or '-'} 来源 {screen['source']}"
            f" 快照时间 {screen.get('snapshot_time') or '未知'}"
            f" 有效报价 {screen['coverage']['valid_quotes']}/{screen['coverage']['rows']}")
    lines = [head]
    if screen.get("indices"):
        parts = [f"{item.get('name')} {_fmt(item.get('price'))}({_fmt(item.get('pct_chg'))}%)"
                 for item in screen["indices"]]
        lines.append("指数(" + str(screen.get("index_basis")) + "): " + " ".join(parts))
    else:
        lines.append("指数: 缺失（本槽快照不含指数，且日线基准缺）")
    positions = screen.get("positions") or []
    lines.append("持仓: 无" if not positions else "持仓:")
    for item in positions:
        lines.append(f"  {item['symbol']} 成本{_fmt(item['avg_cost'])} 现价{_fmt(item['price'])}"
                     f" 今日{_fmt(item['pct_chg'])}% 持有{item.get('held_days')}天"
                     f" 理由:{item.get('thesis') or '-'}")
    plan = screen.get("plan_intents") or []
    lines.append(f"昨晚计划意图: {len(plan)} 条" + (" | ".join(
        f"{item.get('side')} {item.get('symbol')}" for item in plan[:lists]) if plan else " 无"))
    watch = screen.get("watchlist") or []
    lines.append(f"自选: {len(watch)} 只" + (" " + " ".join(str(item.get("symbol")) for item in watch[:lists])
                                          if watch else " 无"))
    top = screen.get("top") or {}
    for key, label in (("gainers", "涨幅前"), ("losers", "跌幅前"), ("amount", "成交额前"),
                       ("volume_ratio_proxy", "量比(代理:本槽/上一槽volume)前")):
        items = top.get(key) or []
        body = " ".join(f"{item['symbol']}({_fmt(item.get('pct_chg') if item.get('pct_chg') is not None else item.get('value'))})"
                        for item in items[:lists])
        lines.append(f"{label}: {body or '无'}")
    movers = screen.get("slot_movers") or []
    lines.append(f"上一槽({screen.get('previous_slot') or '-'})→本槽 5 分钟涨幅前: " + (" ".join(
        f"{item['symbol']}({_fmt(item['move_pct'])})" for item in movers[:lists]) or "无"))
    today = screen.get("today") or {}
    lines.append(f"今日已成交 {len(today.get('fills') or [])} 笔 | 待成交意图 "
                 f"{len(today.get('pending_intents') or [])} 条 | 被家规拦下 {len(today.get('blocked') or [])} 笔")
    for record in (today.get("blocked") or [])[:lists]:
        lines.append(f"  拦单 {record.get('symbol')} {record.get('side')} {record.get('qty')}"
                     f" 家规:{record.get('blocked_by')} AI理由:{record.get('ai_reason')}")
    if screen.get("warnings"):
        lines.append("警告: " + "; ".join(str(item) for item in screen["warnings"]))
    lines.append("本屏只读，不构成下单；缺失处标缺失，未编造。")
    return "\n".join(lines)


def render_text(screen: dict[str, Any], *, lists: int = TOP_N) -> str:
    """Render the screen within MAX_TEXT_CHARS by shrinking the list sizes."""

    text = _render(screen, lists)
    while len(text) > MAX_TEXT_CHARS and lists > 3:
        lists -= 5
        text = _render(screen, lists)
    return text


def build_and_render(project_root: Path | None = None, **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper: the screen dict plus its rendered text and length."""

    screen = build_screen(project_root, **kwargs)
    text = render_text(screen)
    return {"screen": screen, "text": text, "chars": len(text),
            "within_budget": len(text) <= MAX_TEXT_CHARS}
