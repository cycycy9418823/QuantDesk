"""Full-market spot snapshot for the 5-minute refresh.

This is the cheap board view: one overwrite CSV of ~5500 last prices.
It is not per-symbol 1-minute history. Minute bars stay on a capped mover
list so disk and provider load stay bounded.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol
from quantdesk.utils import atomic_write_json

SPOT_RELATIVE = Path("data/cache/spot_snapshot_latest.csv")
SPOT_META_RELATIVE = Path("data/cache/spot_snapshot_meta.json")
SPOT_COLUMNS = (
    "symbol",
    "name",
    "price",
    "pct_chg",
    "change",
    "volume",
    "amount",
    "open",
    "pre_close",
    "high",
    "low",
    "quote_time",
    "quote_date",
    "source",
)

LIMIT_PCT = 9.5
MOVER_PCT = 7.0
TOP_AMOUNT = 20
MAX_MOVERS = 80

COLUMN_ALIASES = {
    "代码": "symbol",
    "股票代码": "symbol",
    "code": "symbol",
    "名称": "name",
    "股票名称": "name",
    "最新价": "price",
    "trade": "price",
    "close": "price",
    "current_price": "price",
    "涨跌幅": "pct_chg",
    "changepercent": "pct_chg",
    "涨跌额": "change",
    "pricechange": "change",
    "成交量": "volume",
    "成交额": "amount",
    "amount": "amount",
    "今开": "open",
    "开盘": "open",
    "昨收": "pre_close",
    "settlement": "pre_close",
    "最高": "high",
    "最低": "low",
    "时间戳": "quote_time",
    "time": "quote_time",
}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _row_from_mapping(raw: Mapping[str, Any], *, source: str, now: datetime) -> dict[str, Any] | None:
    mapped: dict[str, Any] = {}
    for key, value in raw.items():
        name = COLUMN_ALIASES.get(str(key).strip(), str(key).strip())
        mapped[name] = value
    symbol = normalize_symbol(mapped.get("symbol") or mapped.get("code") or "")
    if len(symbol) != 6 or not symbol.isdigit():
        return None
    pct = _num(mapped.get("pct_chg"))
    amount = _num(mapped.get("amount")) or 0.0
    quote_time = str(mapped.get("quote_time") or now.strftime("%Y-%m-%d %H:%M:%S"))
    quote_date = str(mapped.get("quote_date") or now.strftime("%Y-%m-%d"))
    return {
        "symbol": symbol,
        "name": str(mapped.get("name") or ""),
        "price": _num(mapped.get("price")) or 0.0,
        "pct_chg": pct if pct is not None else 0.0,
        "change": _num(mapped.get("change")) or 0.0,
        "volume": _num(mapped.get("volume")) or 0.0,
        "amount": amount,
        "open": _num(mapped.get("open")) or 0.0,
        "pre_close": _num(mapped.get("pre_close")) or 0.0,
        "high": _num(mapped.get("high")) or 0.0,
        "low": _num(mapped.get("low")) or 0.0,
        "quote_time": quote_time,
        "quote_date": quote_date[:10],
        "source": str(mapped.get("source") or source),
    }


def normalize_spot_rows(
    payload: Any,
    *,
    source: str,
    now: datetime,
) -> list[dict[str, Any]]:
    records: list[Mapping[str, Any]]
    if payload is None:
        return []
    if hasattr(payload, "to_dict"):
        records = list(payload.to_dict("records"))
    elif isinstance(payload, list):
        records = [row for row in payload if isinstance(row, Mapping)]
    else:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in records:
        row = _row_from_mapping(raw, source=source, now=now)
        if row is None or row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        rows.append(row)
    return rows


def select_mover_symbols(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_movers: int = MAX_MOVERS,
) -> list[dict[str, str]]:
    """Pick a bounded 1-minute expansion from a full-market snapshot."""

    ranked: list[tuple[int, float, str, str]] = []
    amount_sorted = sorted(
        rows,
        key=lambda row: float(row.get("amount") or 0),
        reverse=True,
    )
    top_amount = {
        str(row.get("symbol") or "")
        for row in amount_sorted[:TOP_AMOUNT]
        if str(row.get("symbol") or "")
    }
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        pct = _num(row.get("pct_chg"))
        if pct is None:
            continue
        if abs(pct) >= LIMIT_PCT:
            reason = "snapshot_limit_up" if pct > 0 else "snapshot_limit_down"
            ranked.append((0, -abs(pct), symbol, reason))
        elif abs(pct) >= MOVER_PCT:
            ranked.append((1, -abs(pct), symbol, "snapshot_mover"))
        elif symbol in top_amount:
            ranked.append((2, -float(row.get("amount") or 0), symbol, "snapshot_top_amount"))
    ranked.sort()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for _prio, _score, symbol, reason in ranked:
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append({"symbol": symbol, "reason": reason})
        if len(out) >= max_movers:
            break
    return out


def write_spot_snapshot(
    root: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    now: datetime,
) -> Path:
    path = root / SPOT_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SPOT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in SPOT_COLUMNS})
    meta = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": source,
        "row_count": len(rows),
        "research_only": True,
        "temporary_quote_only": True,
        "message": "盘中全市场快照，覆盖写入；不替代正式日线，不代表全市场 1 分钟 K。",
    }
    atomic_write_json(root / SPOT_META_RELATIVE, meta)
    return path


# A page that cannot even get this much of the round's remaining window is not
# started: half-started requests are what let a "bounded" round overrun.
SINA_SPOT_MINIMUM_PAGE_SECONDS = 0.5


def _fetch_sina_spot_pages(
    *,
    page_size: int = 100,
    max_pages: int = 80,
    timeout_seconds: float = 8.0,
    budget_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> list[dict[str, Any]]:
    """Sina market-center pages. Same source as the existing spot cache.

    ``budget_seconds`` is the caller's remaining round budget.  When it is set,
    it is an absolute deadline: no page starts without at least
    ``SINA_SPOT_MINIMUM_PAGE_SECONDS`` left, and every single request timeout is
    clamped to the time that is actually left, so one page cannot spend a
    timeout the round no longer has (DSR-07).
    """

    import time

    import requests

    tick = clock or time.monotonic
    deadline = None if budget_seconds is None else tick() + max(0.0, float(budget_seconds))

    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    headers = {"User-Agent": "Mozilla/5.0"}
    rows: list[dict[str, Any]] = []
    today = datetime.now().strftime("%Y-%m-%d")
    for page in range(1, max_pages + 1):
        if deadline is not None and deadline - tick() <= SINA_SPOT_MINIMUM_PAGE_SECONDS:
            break
        params = {
            "page": page,
            "num": page_size,
            "sort": "symbol",
            "asc": 1,
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "init",
        }
        payload: object | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            # The allowance is recomputed per attempt against the absolute
            # deadline: a slow first try must not hand the retry the budget the
            # first try already spent, and a retry that the remaining window
            # cannot pay for is never started (DSR-07).
            if deadline is not None:
                remaining = deadline - tick()
                if remaining <= SINA_SPOT_MINIMUM_PAGE_SECONDS:
                    break
                attempt_timeout = min(float(timeout_seconds), remaining)
            else:
                attempt_timeout = float(timeout_seconds)
            try:
                response = requests.get(
                    url, params=params, headers=headers, timeout=max(0.05, attempt_timeout)
                )
                response.raise_for_status()
                payload = response.json()
                if deadline is not None and tick() >= deadline:
                    raise TimeoutError("sina_spot_response_after_deadline")
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    time.sleep(0.3)
        if payload is None:
            if rows:
                return rows
            raise last_error or RuntimeError("sina_spot_empty")
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if isinstance(item, dict):
                rows.append(item)
        if len(payload) < page_size:
            break
        time.sleep(0.05)
    _ = today
    return rows


def _default_network_fetch(
    *, budget_seconds: float | None = None, clock: Callable[[], float] | None = None,
) -> tuple[Any, str]:
    import inspect
    import time

    tick = clock or time.monotonic
    errors: list[str] = []
    deadline = None if budget_seconds is None else tick() + max(0.0, float(budget_seconds))
    try:
        rows = _fetch_sina_spot_pages(budget_seconds=budget_seconds, clock=tick)
        if deadline is not None and tick() >= deadline:
            errors.append("sina.spot_direct:late_overrun")
        elif rows:
            return rows, "sina.spot_direct"
        else:
            errors.append("sina.spot_direct:empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"sina.spot_direct:{type(exc).__name__}")
    from quantdesk.data_providers import fetch_spot_eastmoney, fetch_spot_sina

    for name, fetcher in (
        ("eastmoney.spot_ak", fetch_spot_eastmoney),
        ("sina.spot_ak", fetch_spot_sina),
    ):
        try:
            parameters = inspect.signature(fetcher).parameters
        except (TypeError, ValueError):
            parameters = {}
        boundable = "budget_seconds" in parameters
        if deadline is not None and not boundable:
            # An adapter that cannot accept a remaining window would run
            # unbounded inside a bounded round (DSR-07): skip it with evidence
            # instead of starting it, exactly like the daily snapshot chain.
            errors.append(f"{name}:unbounded_skipped_no_budget")
            continue
        remaining = None if deadline is None else deadline - tick()
        if remaining is not None and remaining <= SINA_SPOT_MINIMUM_PAGE_SECONDS:
            errors.append(f"{name}:skipped_no_budget_left")
            continue
        try:
            payload = fetcher(budget_seconds=remaining) if boundable else fetcher()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}:{type(exc).__name__}")
            continue
        if deadline is not None and tick() >= deadline:
            # Data that arrives after the deadline is not published, and it is
            # not counted as an in-budget success (DSR-07).
            errors.append(f"{name}:late_overrun")
            continue
        if payload is None:
            errors.append(f"{name}:empty")
            continue
        return payload, name
    raise RuntimeError("spot_snapshot_providers_failed:" + ",".join(errors[:6]))


def refresh_full_market_snapshot(
    project_root: Path,
    *,
    network: bool,
    now: datetime | None = None,
    fetcher: Callable[[], tuple[Any, str]] | None = None,
    budget_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Overwrite the latest full-A spot CSV. Never writes per-symbol 1m files.

    ``budget_seconds`` is the calling round's remaining budget.  It is passed
    down to the default provider chain so the snapshot itself is bounded by the
    round, not only checked after it returns (DSR-07).
    """

    root = Path(project_root).resolve()
    current = now or datetime.now()
    import time

    tick = clock or time.monotonic
    deadline = None if budget_seconds is None else tick() + max(0.0, float(budget_seconds))

    def expired() -> bool:
        return deadline is not None and tick() >= deadline

    def timed_out() -> dict[str, Any]:
        return {"ok": False, "status": "timed_out", "row_count": 0, "movers": [],
                "reason_codes": ["snapshot_deadline_exhausted"], "published": False}
    if not network:
        return {
            "ok": False,
            "status": "planned_offline",
            "row_count": 0,
            "movers": [],
            "reason_codes": ["network_not_authorized"],
        }
    if expired():
        return timed_out()
    try:
        if fetcher is not None:
            payload, source = fetcher()
        else:
            payload, source = _default_network_fetch(
                budget_seconds=None if deadline is None else max(0.0, deadline - tick()), clock=tick,
            )
    except Exception as exc:  # noqa: BLE001 - snapshot failure must not crash minutes
        return {
            "ok": False,
            "status": "provider_failed",
            "row_count": 0,
            "movers": [],
            "reason_codes": [f"spot_snapshot_failed:{type(exc).__name__}"],
            "error": f"{type(exc).__name__}:{exc}"[:400],
        }
    if expired():
        return timed_out()
    rows = normalize_spot_rows(payload, source=source, now=current)
    if not rows:
        return {
            "ok": False,
            "status": "empty",
            "row_count": 0,
            "movers": [],
            "reason_codes": ["spot_snapshot_empty"],
        }
    if expired():
        return timed_out()
    path = write_spot_snapshot(root, rows, source=source, now=current)
    movers = select_mover_symbols(rows)
    return {
        "ok": True,
        "status": "refreshed",
        "row_count": len(rows),
        "source": source,
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "movers": movers,
        "mover_count": len(movers),
        "reason_codes": [],
    }
