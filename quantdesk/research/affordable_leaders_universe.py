"""Affordable-leaders research universe (Stage 4 account B pool).

Research Only / Paper Account Only / No Broker Route.

This module builds a **proxy** leaders universe for the paper replay experiment.
It is explicitly liquidity/affordability driven, not fundamentals driven: it
ranks by 20-day average turnover (``amount_ma20``), keeps names whose one board
lot is affordable under a paper-capital budget, then takes the top-K per industry
as *proxy* sector leaders.  Every row carries ``proxy_non_fundamental=True`` and
``proxy_reason`` so downstream reports can never present it as a formal
fundamental-leader conclusion.  A fundamentals-based build is deferred to a
separate task after extended-data (``--apply``) import.

The build is read-only over ``data/daily`` and ``data/universe`` and never
fetches the network, writes canonical data, or touches a paper ledger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.paper.market_rules import lot_size_for_symbol, metadata_is_st
from quantdesk.shared import normalize_symbol


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
THEME_MAP_PATH = PROJECT_ROOT / "data" / "universe" / "stock_theme_map.csv"
SECURITY_MASTER_PATH = PROJECT_ROOT / "data" / "metadata" / "security_master.csv"

DEFAULT_LIQUIDITY_TOP_N = 2000
DEFAULT_MAX_LOT_COST_PCT = 0.30
DEFAULT_PER_INDUSTRY_K = 3
DEFAULT_AMOUNT_MA_WINDOW = 20
DEFAULT_PAPER_CAPITAL = 10_000.0

PROXY_REASON = (
    "liquidity_affordability_proxy_leader; not a fundamental leader; "
    "fundamentals build deferred to a post extended-data task"
)


@dataclass(frozen=True)
class AffordableLeaderRow:
    symbol: str
    name: str
    industry: str
    amount_ma20: float
    latest_close: float
    lot_size: int
    lot_cost: float
    lot_cost_pct: float
    industry_rank: int
    liquidity_rank: int
    proxy_non_fundamental: bool = True
    proxy_reason: str = PROXY_REASON

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "industry": self.industry,
            "amount_ma20": round(self.amount_ma20, 4),
            "latest_close": round(self.latest_close, 4),
            "lot_size": int(self.lot_size),
            "lot_cost": round(self.lot_cost, 4),
            "lot_cost_pct": round(self.lot_cost_pct, 6),
            "industry_rank": int(self.industry_rank),
            "liquidity_rank": int(self.liquidity_rank),
            "proxy_non_fundamental": bool(self.proxy_non_fundamental),
            "proxy_reason": self.proxy_reason,
            "included": True,
        }


@dataclass(frozen=True)
class AffordableLeadersUniverse:
    as_of: str
    paper_capital: float
    liquidity_top_n: int
    max_lot_cost_pct: float
    per_industry_k: int
    amount_ma_window: int
    rows: tuple[AffordableLeaderRow, ...] = ()
    warnings: tuple[str, ...] = ()
    scanned_symbol_count: int = 0
    liquidity_ranked_count: int = 0
    affordable_count: int = 0
    industry_count: int = 0
    daily_dir: str = ""
    theme_map_path: str = ""

    @property
    def symbols(self) -> list[str]:
        return [row.symbol for row in self.rows]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "task_type": "affordable_leaders_proxy_universe",
            "as_of": self.as_of,
            "paper_capital": float(self.paper_capital),
            "liquidity_top_n": int(self.liquidity_top_n),
            "max_lot_cost_pct": float(self.max_lot_cost_pct),
            "per_industry_k": int(self.per_industry_k),
            "amount_ma_window": int(self.amount_ma_window),
            "scanned_symbol_count": int(self.scanned_symbol_count),
            "liquidity_ranked_count": int(self.liquidity_ranked_count),
            "affordable_count": int(self.affordable_count),
            "industry_count": int(self.industry_count),
            "selected_count": len(self.rows),
            "proxy_non_fundamental": True,
            "proxy_reason": PROXY_REASON,
            "daily_dir": self.daily_dir,
            "theme_map_path": self.theme_map_path,
            "warnings": list(self.warnings),
            "rows": [row.to_dict() for row in self.rows],
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "is_real_order": False,
            "disclaimer": (
                "Research Only; Paper Account Only; No Broker Route; "
                "Proxy (non-fundamental) leaders; Not Investment Advice."
            ),
        }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_industry_map(path: str | Path | None = THEME_MAP_PATH) -> dict[str, str]:
    """Read symbol->industry from the local theme map (read-only)."""

    if not path:
        return {}
    theme_path = Path(path)
    if not theme_path.exists():
        return {}
    frame = read_csv_frame(theme_path, dtype=str).fillna("")
    if "symbol" not in frame.columns or "industry" not in frame.columns:
        return {}
    industry_map: dict[str, str] = {}
    for _, row in frame.iterrows():
        symbol = normalize_symbol(row.get("symbol"))
        industry = str(row.get("industry") or "").strip()
        if symbol and industry:
            industry_map[symbol] = industry
    return industry_map


def load_symbol_names(path: str | Path | None = SECURITY_MASTER_PATH) -> dict[str, str]:
    """Read symbol->name from the local security master (read-only)."""

    if not path:
        return {}
    master_path = Path(path)
    if not master_path.exists():
        return {}
    frame = read_csv_frame(master_path, dtype=str).fillna("")
    if "symbol" not in frame.columns or "name" not in frame.columns:
        return {}
    names: dict[str, str] = {}
    for _, row in frame.iterrows():
        symbol = normalize_symbol(row.get("symbol"))
        name = str(row.get("name") or "").strip()
        if symbol and name:
            names[symbol] = name
    return names


def _symbol_amount_and_close(
    frame: pd.DataFrame,
    *,
    as_of: str,
    amount_ma_window: int,
) -> tuple[float | None, float | None]:
    """Return (amount_ma20, latest_close) as of ``as_of`` from a daily frame.

    Only rows on or before ``as_of`` are considered so the ranking is
    point-in-time and cannot look ahead of the window start.
    """

    if frame.empty or "date" not in frame.columns:
        return None, None
    working = frame.copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce")
    working = working.dropna(subset=["date"])
    cutoff = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(cutoff):
        return None, None
    working = working[working["date"] <= cutoff].sort_values("date")
    if working.empty:
        return None, None
    if "amount" not in working.columns:
        return None, None
    amounts = pd.to_numeric(working["amount"], errors="coerce").dropna()
    if amounts.empty:
        return None, None
    window = amounts.tail(amount_ma_window)
    if window.empty:
        return None, None
    amount_ma = float(window.mean())
    latest_close = _finite(working.iloc[-1].get("close"))
    return amount_ma, latest_close


def build_affordable_leaders_universe(
    *,
    as_of: str,
    daily_dir: str | Path = DAILY_DIR,
    theme_map_path: str | Path = THEME_MAP_PATH,
    security_master_path: str | Path = SECURITY_MASTER_PATH,
    paper_capital: float = DEFAULT_PAPER_CAPITAL,
    liquidity_top_n: int = DEFAULT_LIQUIDITY_TOP_N,
    max_lot_cost_pct: float = DEFAULT_MAX_LOT_COST_PCT,
    per_industry_k: int = DEFAULT_PER_INDUSTRY_K,
    amount_ma_window: int = DEFAULT_AMOUNT_MA_WINDOW,
    symbols: list[str] | None = None,
) -> AffordableLeadersUniverse:
    """Build the affordable-leaders proxy universe as of ``as_of``.

    Pipeline: liquidity rank by ``amount_ma20`` -> keep top-N -> affordability
    filter (one lot cost <= capital * max_lot_cost_pct) -> per-industry top-K by
    liquidity as *proxy* leaders.  Read-only, no network, no ledger writes.
    """

    daily_root = Path(daily_dir)
    warnings: list[str] = []
    industry_map = load_industry_map(theme_map_path)
    name_map = load_symbol_names(security_master_path)

    if symbols is None:
        scan_symbols = sorted(
            normalize_symbol(path.stem)
            for path in daily_root.glob("*.csv")
            if path.stem
        )
    else:
        scan_symbols = sorted({normalize_symbol(sym) for sym in symbols if sym})

    ranked: list[dict[str, Any]] = []
    for symbol in scan_symbols:
        if not symbol:
            continue
        path = daily_root / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            frame = read_csv_frame(path)
        except Exception:
            warnings.append(f"daily_read_failed:{symbol}")
            continue
        amount_ma, latest_close = _symbol_amount_and_close(
            frame, as_of=as_of, amount_ma_window=amount_ma_window
        )
        if amount_ma is None or amount_ma <= 0 or latest_close is None or latest_close <= 0:
            continue
        name = name_map.get(symbol, symbol)
        # Skip ST names: proxy leaders should not include risk-flagged tickers.
        if metadata_is_st(None, name):
            continue
        ranked.append(
            {
                "symbol": symbol,
                "name": name,
                "industry": industry_map.get(symbol, "未分类"),
                "amount_ma20": amount_ma,
                "latest_close": latest_close,
            }
        )

    ranked.sort(key=lambda row: row["amount_ma20"], reverse=True)
    liquidity_ranked = ranked[: max(0, int(liquidity_top_n))]
    for index, row in enumerate(liquidity_ranked):
        row["liquidity_rank"] = index + 1

    budget = float(paper_capital) * float(max_lot_cost_pct)
    affordable: list[dict[str, Any]] = []
    for row in liquidity_ranked:
        symbol = row["symbol"]
        lot = lot_size_for_symbol(symbol, name=row["name"])
        lot_cost = float(row["latest_close"]) * int(lot)
        if lot_cost <= 0 or lot_cost > budget:
            continue
        row["lot_size"] = int(lot)
        row["lot_cost"] = lot_cost
        row["lot_cost_pct"] = lot_cost / float(paper_capital) if paper_capital else 0.0
        affordable.append(row)

    by_industry: dict[str, list[dict[str, Any]]] = {}
    for row in affordable:
        by_industry.setdefault(row["industry"], []).append(row)

    selected: list[AffordableLeaderRow] = []
    for industry, industry_rows in by_industry.items():
        industry_rows.sort(key=lambda row: row["amount_ma20"], reverse=True)
        for rank, row in enumerate(industry_rows[: max(0, int(per_industry_k))]):
            selected.append(
                AffordableLeaderRow(
                    symbol=row["symbol"],
                    name=row["name"],
                    industry=industry,
                    amount_ma20=float(row["amount_ma20"]),
                    latest_close=float(row["latest_close"]),
                    lot_size=int(row["lot_size"]),
                    lot_cost=float(row["lot_cost"]),
                    lot_cost_pct=float(row["lot_cost_pct"]),
                    industry_rank=rank + 1,
                    liquidity_rank=int(row["liquidity_rank"]),
                )
            )

    selected.sort(key=lambda row: (-row.amount_ma20, row.symbol))

    return AffordableLeadersUniverse(
        as_of=str(as_of),
        paper_capital=float(paper_capital),
        liquidity_top_n=int(liquidity_top_n),
        max_lot_cost_pct=float(max_lot_cost_pct),
        per_industry_k=int(per_industry_k),
        amount_ma_window=int(amount_ma_window),
        rows=tuple(selected),
        warnings=tuple(dict.fromkeys(warnings)),
        scanned_symbol_count=len(scan_symbols),
        liquidity_ranked_count=len(liquidity_ranked),
        affordable_count=len(affordable),
        industry_count=len(by_industry),
        daily_dir=str(daily_root),
        theme_map_path=str(Path(theme_map_path)),
    )


__all__ = [
    "AffordableLeaderRow",
    "AffordableLeadersUniverse",
    "PROXY_REASON",
    "build_affordable_leaders_universe",
    "load_industry_map",
    "load_symbol_names",
]
