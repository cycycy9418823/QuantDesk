"""Full-market spot snapshot for the 5-minute refresh.

This is the cheap board view: one overwrite CSV of ~5500 last prices.
It is not per-symbol 1-minute history. Minute bars stay on a capped mover
list so disk and provider load stay bounded.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    "fetched_at",
)

LIMIT_PCT = 9.5
MOVER_PCT = 7.0
TOP_AMOUNT = 20
MAX_MOVERS = 80
# Match the existing spot/observation 300-second limit. This is not a change
# to the separate minute-bar freshness or paper execution rules.
SYNCED_SNAPSHOT_MAX_AGE_SECONDS = 300
SHANGHAI = ZoneInfo("Asia/Shanghai")

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
    # Sina Market_Center rows carry the provider's own clock time here (for
    # example "14:55:03", no date). It is the quote's time, not the fetch time.
    "ticktime": "quote_time",
    "quote_time": "quote_time",
    "日期": "quote_date",
    "date": "quote_date",
    "trade_date": "quote_date",
    "quote_date": "quote_date",
}

# What ``quote_time`` / ``quote_date`` mean in the cache.  They come from the
# provider or stay empty; the fetch moment is recorded separately in
# ``fetched_at`` and is never copied into them.
QUOTE_TIME_BASIS = "provider_quote_time_or_empty"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _provider_text(value: Any) -> str:
    """A provider field as text; null-like spellings are missing, not data."""

    text = str(value if value is not None else "").strip()
    return "" if text.lower() in {"", "nan", "nat", "none", "null", "<na>"} else text


def _as_shanghai(value: datetime) -> datetime:
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


def _row_from_mapping(
    raw: Mapping[str, Any], *, source: str, now: datetime, fetched_at: str = "",
) -> dict[str, Any] | None:
    mapped: dict[str, Any] = {}
    for key, value in raw.items():
        name = COLUMN_ALIASES.get(str(key).strip(), str(key).strip())
        # Several provider spellings can map to one name; never let a later
        # empty alias erase a value an earlier one supplied.
        if name in mapped and not _provider_text(value):
            continue
        mapped[name] = value
    symbol = normalize_symbol(mapped.get("symbol") or mapped.get("code") or "")
    if len(symbol) != 6 or not symbol.isdigit():
        return None
    pct = _num(mapped.get("pct_chg"))
    amount = _num(mapped.get("amount")) or 0.0
    # The quote's own time and date come from the provider or stay empty.
    # Sina's ``ticktime`` is a clock time without a date ("14:55:03"); it is
    # kept as sent, exactly like the A0 archive. The round's clock is never a
    # substitute: on 2026-10-09 every 15:00 cache row read "15:00:01" because
    # the missing provider time was filled with ``now``.
    quote_time = _provider_text(mapped.get("quote_time"))
    quote_date = _provider_text(mapped.get("quote_date"))[:10]
    stamp = _provider_text(mapped.get("fetched_at")) or fetched_at or _as_shanghai(now).isoformat(timespec="seconds")
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
        "quote_date": quote_date,
        "source": str(mapped.get("source") or source),
        # When this process received the row. Separate from the quote time.
        "fetched_at": stamp,
    }


def normalize_spot_rows(
    payload: Any,
    *,
    source: str,
    now: datetime,
    fetched_at: str = "",
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
        row = _row_from_mapping(raw, source=source, now=now, fetched_at=fetched_at)
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
        key=lambda row: _num(row.get("amount")) or 0,
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
            ranked.append((2, -(_num(row.get("amount")) or 0), symbol, "snapshot_top_amount"))
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
    evidence: Mapping[str, Any] | None = None,
) -> Path:
    path = root / SPOT_RELATIVE
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(SPOT_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in SPOT_COLUMNS})
    # One complete file or the previous one: a reader (or a killed round) can
    # never see a half-written cache.
    _atomic_replace_bytes(path, stream.getvalue().encode("utf-8-sig"))
    fetched = sorted({str(row.get("fetched_at") or "") for row in rows if str(row.get("fetched_at") or "")})
    meta = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": source,
        "row_count": len(rows),
        "fetched_at": fetched[-1] if fetched else "",
        "quote_time_basis": QUOTE_TIME_BASIS,
        "quote_time_missing_count": sum(1 for row in rows if not str(row.get("quote_time") or "").strip()),
        "quote_date_missing_count": sum(1 for row in rows if not str(row.get("quote_date") or "").strip()),
        "research_only": True,
        "temporary_quote_only": True,
        "message": "盘中全市场快照，覆盖写入；不替代正式日线，不代表全市场 1 分钟 K。"
                   "quote_time/quote_date 只来自行情源，缺失即留空；抓取时刻见 fetched_at。",
    }
    if evidence:
        meta.update(evidence)
    atomic_write_json(root / SPOT_META_RELATIVE, meta)
    return path


def _atomic_replace_bytes(path: Path, content: bytes, *, attempts: int = 5) -> None:
    """Write beside the target, fsync, then replace; retry a briefly-held target.

    Windows refuses ``os.replace`` while another process holds the target open
    without delete sharing (a reader in the middle of ``read_csv``).  A few
    short retries cover that; a persistent failure raises and the previous
    file stays intact.
    """

    import os
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(max(1, int(attempts))):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def _quote_stamp(row: Mapping[str, Any], trade_date: str) -> datetime | None:
    raw_time = str(row.get("quote_time") or "").strip()
    if not raw_time:
        return None
    stamp = raw_time if "-" in raw_time or "/" in raw_time else f"{row.get('quote_date') or trade_date} {raw_time}"
    try:
        parsed = datetime.fromisoformat(stamp.replace("/", "-"))
        if row.get("quote_date") and str(row["quote_date"])[:10] != parsed.date().isoformat():
            return None
        return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)
    except ValueError:
        return None


def _synced_snapshot(
    root: Path, current: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read a recent verified A0 slot, preserving the provider's original fields."""
    from quantdesk.snapshot_history import _quote_time_state, _slot_evidence, _valid_price

    reference = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    day = reference.date().isoformat()
    directory = root / "data" / "snapshots" / day
    reasons = []
    paths = sorted((path for path in directory.glob("[0-9][0-9][0-9][0-9].csv")
                    if path.stem <= reference.strftime("%H%M")), reverse=True)
    for path in paths:
        # Older slot names cannot conceal a fresh observation: reject them
        # before reading thousands of rows from the rest of the day's archive.
        try:
            slot = datetime.strptime(day + path.stem, "%Y-%m-%d%H%M").replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
        if (reference - slot).total_seconds() > SYNCED_SNAPSHOT_MAX_AGE_SECONDS + 300:
            break
        proof = _slot_evidence(root, day, path.stem)
        if proof.get("status") not in {"complete", "partial"}:
            reasons.append(f"synced_snapshot_invalid:{path.stem}")
            continue
        meta, quality = proof["metadata"], proof["quality"]
        if quality.get("fetch_outside_slot") or meta["source"] == "unknown_provider":
            reasons.append(f"synced_snapshot_capture_unverified:{path.stem}")
            continue
        fetched = datetime.fromisoformat(meta["fetched_at"])
        capture_age = (reference - fetched).total_seconds()
        if not 0 <= capture_age <= SYNCED_SNAPSHOT_MAX_AGE_SECONDS:
            reasons.append(f"synced_snapshot_stale:{path.stem}")
            continue
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != meta["csv_sha256"]:
                reasons.append(f"synced_snapshot_changed:{path.stem}")
                continue
            original = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        except (OSError, UnicodeError, csv.Error):
            reasons.append(f"synced_snapshot_read_failed:{path.stem}")
            continue
        rows = []
        for row in original:
            quote = _quote_stamp(row, day)
            if (_valid_price(row.get("price")) and quote is not None
                    and _quote_time_state(row, day, fetched_at=fetched) == "usable"
                    and 0 <= (reference - quote).total_seconds() <= SYNCED_SNAPSHOT_MAX_AGE_SECONDS):
                # No normalizer: a missing quote_date or pct_chg stays missing.
                rows.append({name: row.get(name, "") for name in SPOT_COLUMNS})
        if not rows:
            reasons.append(f"synced_snapshot_no_fresh_quotes:{path.stem}")
            continue
        partial = proof["status"] != "complete" or len(rows) != len(original)
        evidence = {
            "reused": True, "reused_at": reference.isoformat(timespec="seconds"),
            "updated_at": meta["fetched_at"], "source_fetched_at": meta["fetched_at"],
            "archive_path": path.relative_to(root).as_posix(), "archive_sha256": meta["csv_sha256"],
            "archive_slot": path.stem, "archive_row_count": len(original), "archive_quality": quality,
            "capture_age_seconds": capture_age, "max_quote_age_seconds": SYNCED_SNAPSHOT_MAX_AGE_SECONDS,
            "excluded_quote_count": len(original) - len(rows),
            "reason_codes": [*reasons, *meta.get("warnings", []), *(["synced_snapshot_partial"] if partial else [])],
        }
        return {"rows": rows, "source": meta["source"], "status": "partial" if partial else "reused",
                "ok": not partial, "evidence": evidence}, reasons
    return None, reasons


# A page that cannot even get this much of the round's remaining window is not
# started: half-started requests are what let a "bounded" round overrun.
SINA_SPOT_MINIMUM_PAGE_SECONDS = 0.5


class _SpotPageRows(list):
    """List-compatible provider rows with explicit pagination completion."""

    def __init__(self):
        super().__init__()
        self.pagination = {"complete": False, "pages_completed": 0, "failed_page": None, "reason_codes": []}


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
    rows = _SpotPageRows()
    for page in range(1, max_pages + 1):
        if deadline is not None and deadline - tick() <= SINA_SPOT_MINIMUM_PAGE_SECONDS:
            rows.pagination["reason_codes"].append(f"sina_spot_deadline_exhausted:page={page}")
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
                payload = None
                last_error = exc
                if attempt == 0:
                    time.sleep(0.3)
        if payload is None:
            if rows:
                rows.pagination.update(failed_page=page)
                error_type = type(last_error).__name__ if last_error is not None else "budget_exhausted"
                rows.pagination["reason_codes"].append(f"sina_spot_page_failed:page={page}:{error_type}")
                return rows
            raise last_error or RuntimeError("sina_spot_empty")
        if not isinstance(payload, list):
            rows.pagination.update(failed_page=page)
            rows.pagination["reason_codes"].append(f"sina_spot_invalid_page:page={page}")
            break
        rows.pagination["pages_completed"] += 1
        if not payload:
            rows.pagination["complete"] = True
            break
        invalid = 0
        for item in payload:
            symbol = normalize_symbol(item.get("symbol") or item.get("code") or "") if isinstance(item, dict) else ""
            if len(symbol) != 6 or not symbol.isdigit():
                invalid += 1
            else:
                rows.append(item)
        if invalid:
            rows.pagination.update(failed_page=page)
            rows.pagination["reason_codes"].append(f"sina_spot_invalid_rows:page={page}:count={invalid}")
            break
        if len(payload) < page_size:
            rows.pagination["complete"] = True
            break
        time.sleep(0.05)
    if not rows.pagination["complete"] and not rows.pagination["reason_codes"]:
        rows.pagination["reason_codes"].append("sina_spot_page_limit_reached")
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
    """Reuse an archived snapshot or fetch the latest full-A spot CSV.

    ``budget_seconds`` is the calling round's remaining budget.  It is passed
    down to the default provider chain so the snapshot itself is bounded by the
    round, not only checked after it returns (DSR-07).
    """

    root = Path(project_root).resolve()
    current = _as_shanghai(now or datetime.now(SHANGHAI))
    import time

    tick = clock or time.monotonic
    round_started = tick()
    deadline = None if budget_seconds is None else round_started + max(0.0, float(budget_seconds))

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
    reuse_reasons: list[str] = []
    if fetcher is None:
        reused, reuse_reasons = _synced_snapshot(root, current)
        if expired():
            return timed_out()
        if reused is not None:
            rows, evidence = reused["rows"], reused["evidence"]
            # The archive already owns its source/time evidence. Use it for
            # this round's scope, without copying clock-only timestamps into
            # the legacy spot cache whose readers may infer today's date.
            movers = select_mover_symbols(rows)
            return {
                "ok": reused["ok"], "status": reused["status"], "published": False, "row_count": len(rows),
                "source": reused["source"], "path": evidence["archive_path"],
                "movers": movers, "mover_count": len(movers), **evidence,
            }
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
            "reason_codes": [*reuse_reasons, f"spot_snapshot_failed:{type(exc).__name__}"],
            "error": f"{type(exc).__name__}:{exc}"[:400],
        }
    # One clock read serves both the deadline check and the fetch moment, so
    # the round's clock is read exactly as often as before.
    fetched_tick = tick()
    if deadline is not None and fetched_tick >= deadline:
        return timed_out()
    # The moment the provider's rows were in hand: the round's start clock
    # plus the time the fetch took. Recorded per row as ``fetched_at``.
    from datetime import timedelta

    fetch_elapsed = max(0.0, float(fetched_tick - round_started))
    fetched_at = (current + timedelta(seconds=fetch_elapsed)).isoformat(timespec="seconds")
    rows = normalize_spot_rows(payload, source=source, now=current, fetched_at=fetched_at)
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
    pagination = getattr(payload, "pagination", {})
    partial = bool(pagination) and pagination.get("complete") is not True
    reason_codes = [*reuse_reasons, *pagination.get("reason_codes", [])]
    if len(rows) < len(payload):
        partial = True
        reason_codes.append(f"spot_snapshot_normalization_dropped_rows:{len(payload) - len(rows)}")
    evidence = {"pagination": pagination, "reason_codes": reason_codes, "pagination_complete": pagination.get("complete") is True} if pagination else None
    path = write_spot_snapshot(root, rows, source=source, now=current, evidence=evidence)
    movers = select_mover_symbols(rows)
    return {
        "ok": not partial,
        "status": "partial" if partial else "refreshed",
        "published": True,
        "row_count": len(rows),
        "source": source,
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "movers": movers,
        "mover_count": len(movers),
        "pagination": pagination,
        "reason_codes": reason_codes,
    }
