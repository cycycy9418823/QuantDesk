"""Own paper ledger. Never shares identity with QuantDesk auto_paper_portfolio."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_trader.mandate import BOUNDARY, DEFAULT_MANDATE, TraderMandate
from ai_trader.paths import LEDGER_PATH, assert_not_quantdesk_ledger, ledger_id_for_path, ledger_id_for_profile
from quantdesk.paper.matching import POLICY_NEXT_OPEN, ensure_declared_fill_policy
from quantdesk.shared import _json_default, to_jsonable

LEDGER_SCHEMA = "ai_trader_ledger_v1"
# Rotating copies of the ledger after each successful save (charter §2: the
# book is the evidence; a torn or lost file must be recoverable).
LEDGER_BACKUP_DIRNAME = "ledger_backups"
LEDGER_BACKUP_KEEP = 7
# Files whose presence proves this book already had a history. A missing
# ledger.json next to any of them is a lost book, not a first run.
_HISTORY_MARKERS = ("journal.jsonl", "decision_log.jsonl", "daytime_fills.jsonl")


class LedgerMissingError(FileNotFoundError):
    """ledger.json is absent where a book must already exist; nothing was created."""


def session_days_held(acquired_on: str, as_of: str) -> int:
    """Trading sessions a position has been held for.

    Lives here rather than in ``overnight`` so ``trader`` can use it too: exit
    decisions and closed-trade attribution both need it, and importing back from
    ``overnight`` would be circular.
    """

    import pandas as pd

    try:
        start = pd.Timestamp(str(acquired_on)[:10])
        end = pd.Timestamp(str(as_of)[:10])
    except (TypeError, ValueError):
        return 0
    if pd.isna(start) or pd.isna(end) or end < start:
        return 0
    return max(int(len(pd.bdate_range(start, end)) - 1), 0)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def empty_ledger(
    mandate: TraderMandate = DEFAULT_MANDATE,
    *,
    created_at: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    stamp = created_at or _now()
    ledger = {
        "schema_version": LEDGER_SCHEMA,
        "ledger_id": ledger_id or ledger_id_for_profile() or mandate.ledger_id,
        "mandate": mandate.to_dict(),
        "created_at": stamp,
        "updated_at": stamp,
        "as_of": None,
        "initial_cash": mandate.initial_cash,
        "cash": mandate.initial_cash,
        "positions": [],
        "trades": [],
        "equity": mandate.initial_cash,
        "market_value": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "warnings": [],
        "boundary": dict(BOUNDARY),
    }
    return ensure_declared_fill_policy(ledger, policy_id=POLICY_NEXT_OPEN)


def ledger_backup_dir(path: Path) -> Path:
    return Path(path).parent / LEDGER_BACKUP_DIRNAME


def ledger_history_evidence(path: Path) -> list[str]:
    """What proves this directory already held a book (empty list = first run)."""

    target = Path(path)
    found: list[str] = []
    backups = ledger_backup_dir(target)
    if backups.is_dir() and any(backups.glob(f"{target.stem}.*.json")):
        found.append(str(backups))
    for name in _HISTORY_MARKERS:
        marker = target.parent / name
        try:
            if marker.is_file() and marker.stat().st_size > 0:
                found.append(str(marker))
        except OSError:
            continue
    evidence_dir = target.parent / "execution_evidence"
    if evidence_dir.is_dir() and any(evidence_dir.iterdir()):
        found.append(str(evidence_dir))
    return found


def assert_finite_json_numbers(value: Any) -> None:
    """Refuse JSON NaN/Infinity, including overflowed numeric literals."""
    pending, visited = [value], set()
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("ai_trader nonfinite JSON number")
        if isinstance(item, (dict, list, tuple)) and id(item) not in visited:
            visited.add(id(item))
            pending.extend(item.values() if isinstance(item, dict) else item)


def _assert_ledger_identity(payload: Any, target: Path) -> None:
    if not isinstance(payload, dict):
        raise ValueError("ai_trader ledger is not an object")
    if str(payload.get("ledger_id") or "") != ledger_id_for_path(target):
        raise ValueError("ai_trader ledger_id mismatch; refusing to use foreign book")


def load_ledger(path: Path | None = None, *, missing_ok: bool = True) -> dict[str, Any]:
    """Load the book. ``missing_ok=False`` raises instead of inventing an empty book.

    The default keeps the historical read-only behaviour (status pages show an
    empty, unsaved book). Any path that trades and then saves must pass
    ``missing_ok=False`` so a lost ledger.json can never be replaced by a fresh
    100k book.
    """

    target = assert_not_quantdesk_ledger(path or LEDGER_PATH)
    if not target.is_file():
        if not missing_ok:
            raise LedgerMissingError(f"ai_trader ledger missing: {target}")
        return empty_ledger(ledger_id=ledger_id_for_path(target))
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert_finite_json_numbers(payload)
    _assert_ledger_identity(payload, target)
    return ensure_declared_fill_policy(payload, policy_id=POLICY_NEXT_OPEN)


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":  # directories cannot be opened for fsync on Windows
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _durable_write_text(target: Path, text: str) -> None:
    """Write, flush, fsync, then atomically replace; the old file survives a crash."""

    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        # Same text mode as the previous Path.write_text writer, so the bytes on
        # disk (platform newlines) do not change -- only durability does.
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        _fsync_dir(target.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _rotate_backup(target: Path, text: str) -> Path:
    directory = ledger_backup_dir(target)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    backup = directory / f"{target.stem}.{stamp}.json"
    _durable_write_text(backup, text)
    copies = sorted(directory.glob(f"{target.stem}.*.json"), key=lambda item: item.name)
    for stale in copies[: max(0, len(copies) - LEDGER_BACKUP_KEEP)]:
        try:
            stale.unlink()
        except OSError:
            pass
    return backup


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> Path:
    target = assert_not_quantdesk_ledger(path or LEDGER_PATH)
    _assert_ledger_identity(ledger, target)
    assert_finite_json_numbers(ledger)
    payload = deepcopy(ledger)
    payload["updated_at"] = _now()
    payload["boundary"] = dict(BOUNDARY)
    ensure_declared_fill_policy(payload, policy_id=POLICY_NEXT_OPEN)
    text = json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, allow_nan=False,
                      default=_json_default) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    _durable_write_text(target, text)
    _rotate_backup(target, text)
    return target


def init_ledger(*, reset: bool = False, path: Path | None = None) -> dict[str, Any]:
    """Load the book, or create it on a genuine first run only.

    A missing ledger.json next to evidence of an earlier book (rotating backups,
    journal, decision log, fills, execution evidence) raises
    ``LedgerMissingError`` instead of silently starting a new 100k book. An
    explicit ``reset=True`` is the owner's deliberate re-initialisation.
    """

    target = assert_not_quantdesk_ledger(path or LEDGER_PATH)
    if target.is_file() and not reset:
        return load_ledger(target)
    if not reset:
        evidence = ledger_history_evidence(target)
        if evidence:
            raise LedgerMissingError(
                f"ai_trader ledger missing but history exists ({'; '.join(evidence)}); "
                "restore from ledger_backups instead of starting a new book"
            )
    ledger = empty_ledger(ledger_id=ledger_id_for_path(target))
    save_ledger(ledger, target)
    return ledger


def position_map(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = ledger.get("positions") if isinstance(ledger, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("symbol"):
            out[str(row["symbol"])] = dict(row)
    return out


def mark_to_market(
    ledger: dict[str, Any],
    prices: dict[str, float],
    *,
    as_of: str,
) -> dict[str, Any]:
    market_value = 0.0
    unrealized = 0.0
    warnings: list[str] = list(ledger.get("warnings") or [])
    next_positions: list[dict[str, Any]] = []
    for row in ledger.get("positions") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        qty = int(row.get("quantity") or 0)
        avg = float(row.get("avg_cost") or 0.0)
        price = prices.get(symbol)
        stale_mark = False
        if (price is None or price <= 0) and qty > 0:
            # A position missing from tonight's price dict has NOT become
            # worthless. Writing it to zero silently vaporises equity: a book of
            # cash 20,000 plus two holdings worth 53,000 reported equity 31,000
            # instead of 73,000, i.e. a 42,000 loss that never happened, in the
            # very number used to judge the trader. A suspended name carries its
            # last known mark, which is also ordinary accounting practice.
            fallback = float(row.get("market_price") or 0.0) or avg
            if fallback > 0:
                price = fallback
                stale_mark = True
        if price is None or price <= 0 or qty <= 0:
            warnings.append(f"missing_mark_price:{symbol}")
            marked = dict(row)
            marked["market_price"] = None
            marked["market_value"] = 0.0
            marked["unrealized_pnl"] = 0.0
            next_positions.append(marked)
            continue
        value = round(price * qty, 4)
        pnl = round((price - avg) * qty, 4)
        market_value += value
        unrealized += pnl
        marked = dict(row)
        marked["market_price"] = round(price, 4)
        marked["market_value"] = value
        marked["unrealized_pnl"] = pnl
        marked["weight"] = 0.0
        if stale_mark:
            # Surfaced rather than hidden: the value is carried, not observed.
            marked["stale_mark"] = True
            warnings.append(f"stale_mark_price:{symbol}")
        next_positions.append(marked)
    cash = float(ledger.get("cash") or 0.0)
    equity = round(cash + market_value, 4)
    for row in next_positions:
        value = float(row.get("market_value") or 0.0)
        row["weight"] = round(value / equity, 6) if equity > 0 else 0.0
    updated = deepcopy(ledger)
    updated["as_of"] = as_of
    updated["positions"] = next_positions
    updated["cash"] = round(cash, 4)
    updated["market_value"] = round(market_value, 4)
    updated["equity"] = equity
    updated["unrealized_pnl"] = round(unrealized, 4)
    updated["warnings"] = warnings[-50:]
    return updated
