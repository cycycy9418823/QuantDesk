"""Append-only audit of strategy-book fills that were booked at a limit price.

Batch 1A-C. Until 1A the strategy book filled at the signal day's close and
only refused a one-word board with pct >= 9.8, so some historical buys sit
exactly at that day's limit-up price (and some sells at limit-down). Those
fills cannot be re-priced (minute/snapshot history does not reach back far
enough), so this module only *labels* them:

- every recorded fill (open-position entries, closed entries and exits,
  partial reductions) is compared with the cent-rounded limit price of its
  own session (``quantdesk.board_rules``; previous close from the session's
  snapshot when one exists, else the daily file);
- a flagged row gets a ``fill_audit`` entry added; no recorded number
  (price, quantity, cash, PnL) is changed, and an existing label is kept;
- the list is written as JSON + Markdown for the owner.

Run (dry by default; ``--apply`` writes the labels through the state store,
which keeps a backup)::

    python -m quantdesk.paper.limit_fill_audit [--apply]
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from quantdesk.board_rules import (
    LIMIT_PRICE_EPSILON,
    MAIN_ST_10PCT_START,
    compute_limit_prices,
    st_status_from_name,
)
from quantdesk.data_truth.derived_limit_prices import clamp_open_only_stale, is_st_on, load_st_intervals
from quantdesk.paper.state_store import PaperStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCHEMA = "strategy_limit_fill_audit_v1"
AUDIT_KEY = "fill_audit"
BOARD_LABELS = {"main": "主板", "chinext": "创业板", "star": "科创板", "bse": "北交所", "st": "主板ST", "unknown": "未知"}


def _num(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


class _Daily:
    def __init__(self, daily_dir: Path) -> None:
        self.daily_dir = Path(daily_dir)
        self._cache: dict[str, list[dict[str, str]]] = {}

    def rows(self, symbol: str) -> list[dict[str, str]]:
        if symbol not in self._cache:
            rows = _read_csv(self.daily_dir / f"{symbol}.csv")
            rows.sort(key=lambda row: str(row.get("date") or "")[:10])
            self._cache[symbol] = rows
        return self._cache[symbol]

    def session(self, symbol: str, day: str) -> tuple[dict[str, str] | None, dict[str, str] | None]:
        rows = self.rows(symbol)
        for index, row in enumerate(rows):
            if str(row.get("date") or "")[:10] == day:
                return row, (rows[index - 1] if index else None)
        return None, None


class _Snapshots:
    """Previous close straight from the exchange feed, for days with a snapshot."""

    def __init__(self, snapshots_dir: Path | None) -> None:
        self.root = Path(snapshots_dir) if snapshots_dir else None
        self._cache: dict[str, dict[str, dict[str, str]]] = {}

    def row(self, day: str, symbol: str) -> tuple[dict[str, str] | None, str]:
        if self.root is None:
            return None, ""
        if day not in self._cache:
            index: dict[str, dict[str, str]] = {}
            path = ""
            for slot in ("1500", "1455", "0930"):
                candidate = self.root / day / f"{slot}.csv"
                if candidate.is_file():
                    for row in _read_csv(candidate):
                        index.setdefault(str(row.get("symbol") or "").zfill(6), {**row, "_path": str(candidate)})
                    path = str(candidate)
                    break
            self._cache[day] = index
            _ = path
        row = self._cache[day].get(symbol)
        return row, str((row or {}).get("_path") or "")


def _pre_close(symbol: str, day: str, daily: _Daily, snaps: _Snapshots) -> tuple[float | None, str]:
    snap, path = snaps.row(day, symbol)
    value = _num((snap or {}).get("pre_close"))
    if value and value > 0 and str((snap or {}).get("quote_date") or day)[:10] == day:
        return value, f"snapshot_pre_close:{path}"
    row, prev = daily.session(symbol, day)
    if row is not None:
        explicit = _num(row.get("pre_close"))
        if explicit and explicit > 0:
            return explicit, "daily_pre_close"
        close, change = _num(row.get("close")), _num(row.get("change"))
        if close and change is not None and close - change > 0:
            return round(close - change, 4), "daily_close_minus_change"
    if prev is not None:
        close = _num(prev.get("close"))
        if close and close > 0:
            return close, "daily_previous_row_close"
    return None, "missing"


def iter_fills(state: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every recorded fill in the strategy book, with where it lives."""

    def _entry_price(row: dict[str, Any]) -> float | None:
        for key in ("execution_price", "trigger_price"):
            value = _num(row.get(key))
            if value and value > 0:
                return value
        gross, qty = _num(row.get("entry_gross_amount")), _num(row.get("quantity"))
        return gross / qty if gross and qty else None

    for collection in ("positions", "closed_positions", "realized_reductions"):
        for index, row in enumerate(state.get(collection) or []):
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            symbol = str(row["symbol"]).zfill(6)
            if collection != "realized_reductions" and row.get("entry_date"):
                yield {"collection": collection, "index": index, "kind": "entry", "side": "buy",
                       "symbol": symbol, "date": str(row.get("entry_date"))[:10],
                       "price": _entry_price(row), "row": row}
            if collection != "positions" and row.get("exit_date"):
                yield {"collection": collection, "index": index, "kind": "exit", "side": "sell",
                       "symbol": symbol, "date": str(row.get("exit_date"))[:10],
                       "price": _num(row.get("exit_price")), "row": row}


def _st_history(st_path: Path | None, snapshots_dir: Path | None) -> tuple[dict, str]:
    if not st_path or not Path(st_path).is_file():
        return {}, ""
    current: set[str] = set()
    first_day = ""
    if snapshots_dir and Path(snapshots_dir).is_dir():
        days = sorted(p.name for p in Path(snapshots_dir).iterdir() if p.is_dir() and p.name[:2] == "20")
        if days:
            first_day = days[0]
            for row in _read_csv(Path(snapshots_dir) / days[-1] / "0930.csv"):
                if st_status_from_name(row.get("name")) == "st":
                    current.add(str(row.get("symbol") or "").zfill(6))
    st_map = load_st_intervals(Path(st_path), current_st_symbols=current or None)
    if current and first_day:
        clamp_open_only_stale(st_map, current_st_symbols=current, first_snapshot_date=first_day)
    export_end = ""
    for row in _read_csv(Path(st_path)):
        for key in ("st_start_date", "st_end_date"):
            value = str(row.get(key) or "")[:10]
            if value > export_end:
                export_end = value
    return st_map, export_end


def audit_fills(
    state: dict[str, Any],
    *,
    daily_dir: Path,
    snapshots_dir: Path | None = None,
    st_history_path: Path | None = None,
) -> dict[str, Any]:
    daily = _Daily(daily_dir)
    snaps = _Snapshots(snapshots_dir)
    st_map, export_end = _st_history(st_history_path, snapshots_dir)
    flagged: list[dict[str, Any]] = []
    unverifiable: list[dict[str, Any]] = []
    checked = 0
    for fill in iter_fills(state):
        symbol, day, side, price = fill["symbol"], fill["date"], fill["side"], fill["price"]
        name = str(fill["row"].get("name") or "")
        if export_end and day <= export_end:
            st_status = "st" if is_st_on(st_map.get(symbol, []), day) else "not_st"
            st_source = "st_history"
        else:
            st_status = st_status_from_name(name)
            st_source = "recorded_name" if st_status != "unknown" else "unknown"
        base, base_source = _pre_close(symbol, day, daily, snaps)
        limits = compute_limit_prices(symbol, trade_date=day, pre_close=base, st_status=st_status)
        item = {
            "symbol": symbol, "name": name, "date": day, "side": side, "kind": fill["kind"],
            "collection": fill["collection"], "index": fill["index"],
            "fill_price": None if price is None else round(price, 4),
            "up_limit": limits.up_limit, "down_limit": limits.down_limit,
            "pre_close": limits.pre_close, "pre_close_source": base_source,
            "board": limits.board_type, "board_label": BOARD_LABELS.get(limits.board_type, limits.board_type),
            "ratio_basis": limits.ratio_basis, "st_status": st_status, "st_source": st_source,
        }
        if price is None or limits.status != "ok":
            item["status"] = "unverifiable" if limits.status != "no_limit" else "no_limit"
            item["reason"] = limits.reason or "missing_fill_price"
            if item["status"] == "unverifiable":
                unverifiable.append(item)
            continue
        checked += 1
        if side == "buy" and price >= float(limits.up_limit) - LIMIT_PRICE_EPSILON:
            item["status"] = "filled_at_limit_up"
            flagged.append(item)
        elif side == "sell" and price <= float(limits.down_limit) + LIMIT_PRICE_EPSILON:
            item["status"] = "filled_at_limit_down"
            flagged.append(item)
    return {
        "schema": AUDIT_SCHEMA,
        "audited_at": datetime.now().isoformat(timespec="seconds"),
        "rule": "fill price vs the session's limit price = previous close x (1 +/- band), rounded half-up to the cent",
        "main_st_10pct_start": MAIN_ST_10PCT_START,
        "st_history_export_end": export_end,
        "fills_checked": checked,
        "flagged_count": len(flagged),
        "flagged_buy_at_limit_up": sum(1 for row in flagged if row["status"] == "filled_at_limit_up"),
        "flagged_sell_at_limit_down": sum(1 for row in flagged if row["status"] == "filled_at_limit_down"),
        "unverifiable_count": len(unverifiable),
        "flagged": flagged,
        "unverifiable": unverifiable,
    }


def annotate_state(state: dict[str, Any], audit: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Add ``fill_audit`` labels to flagged rows. Never changes a recorded value."""

    added = 0
    for item in audit.get("flagged") or []:
        rows = state.get(item["collection"]) or []
        if item["index"] >= len(rows) or not isinstance(rows[item["index"]], dict):
            continue
        row = rows[item["index"]]
        if str(row.get("symbol") or "").zfill(6) != item["symbol"]:
            continue
        labels = row.get(AUDIT_KEY) if isinstance(row.get(AUDIT_KEY), dict) else {}
        if item["kind"] in labels:
            continue
        labels[item["kind"]] = {
            "schema": AUDIT_SCHEMA, "status": item["status"], "fill_price": item["fill_price"],
            "up_limit": item["up_limit"], "down_limit": item["down_limit"], "pre_close": item["pre_close"],
            "pre_close_source": item["pre_close_source"], "ratio_basis": item["ratio_basis"],
            "audited_at": audit["audited_at"],
            "note_zh": ("历史成交价等于当日涨停价（涨停不买）" if item["status"] == "filled_at_limit_up"
                        else "历史成交价等于当日跌停价（跌停不卖）") + "；只标注，不改历史数值（宪章 §2）。",
        }
        row[AUDIT_KEY] = labels
        added += 1
    state["limit_fill_audit"] = {
        "schema": AUDIT_SCHEMA, "audited_at": audit["audited_at"], "flagged_count": audit["flagged_count"],
        "flagged_buy_at_limit_up": audit["flagged_buy_at_limit_up"],
        "flagged_sell_at_limit_down": audit["flagged_sell_at_limit_down"],
        "unverifiable_count": audit["unverifiable_count"],
    }
    return state, added


def render_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# 策略账历史涨跌停价成交清单（纸面 · 未验证）",
        "",
        f"- 检查时间：{audit['audited_at']}；检查成交 {audit['fills_checked']} 笔",
        f"- 买在涨停价：{audit['flagged_buy_at_limit_up']} 笔；卖在跌停价：{audit['flagged_sell_at_limit_down']} 笔",
        f"- 算不出涨跌停价（缺前收）：{audit['unverifiable_count']} 笔",
        "- 规则：成交价对比当日涨跌停价（前收 ×(1±幅度)，四舍五入到分）。只标注，不改历史数值。",
        "",
        "| 代码 | 名称 | 日期 | 方向 | 成交价 | 当日涨停价 | 当日跌停价 | 前收 | 板块 | 前收来源 |",
        "|---|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for row in audit["flagged"]:
        source = str(row["pre_close_source"]).split(":")[0]
        lines.append(
            f"| {row['symbol']} | {row['name']} | {row['date']} | {'买' if row['side'] == 'buy' else '卖'} | "
            f"{row['fill_price']} | {row['up_limit']} | {row['down_limit']} | {row['pre_close']} | "
            f"{row['board_label']} | {source} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Label strategy-book fills booked at a limit price (append-only).")
    parser.add_argument("--state", default=str(PROJECT_ROOT / "state" / "auto_paper_portfolio.json"))
    parser.add_argument("--daily-dir", default=str(PROJECT_ROOT / "data" / "daily"))
    parser.add_argument("--snapshots-dir", default=str(PROJECT_ROOT / "data" / "snapshots"))
    parser.add_argument("--st-history", default=str(PROJECT_ROOT / "data" / "metadata" / "st_history.csv"))
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "results" / "paper_audit"))
    parser.add_argument("--apply", action="store_true", help="write the labels into the state (backup kept)")
    args = parser.parse_args(argv)
    store = PaperStateStore(Path(args.state), schema_version=None, max_backups=7)
    state = store.read(default={})
    audit = audit_fills(state, daily_dir=Path(args.daily_dir), snapshots_dir=Path(args.snapshots_dir),
                        st_history_path=Path(args.st_history))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    (out_dir / f"strategy_limit_fill_audit_{stamp}.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"strategy_limit_fill_audit_{stamp}.md").write_text(render_markdown(audit), encoding="utf-8")
    added = 0
    if args.apply:
        state, added = annotate_state(state, audit)
        store.write(state)
    print(json.dumps({k: audit[k] for k in ("fills_checked", "flagged_count", "flagged_buy_at_limit_up",
                                            "flagged_sell_at_limit_down", "unverifiable_count")}
                     | {"applied": bool(args.apply), "labels_added": added, "out_dir": str(out_dir)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
