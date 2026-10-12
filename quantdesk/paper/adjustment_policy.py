"""Paper-account valuation policies for adjustment factors and suspensions.

This module is deliberately local-data only.  It does not fetch market data and
it does not infer missing factors.  Missing adjustment evidence is surfaced as a
warning on the paper position instead of being silently treated as clean data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Iterable

import pandas as pd
from pandas.errors import ParserError

from quantdesk.shared import normalize_symbol
from quantdesk.utils import safe_float, safe_int


def _symbol(value: Any) -> str:
    text = normalize_symbol(value)
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else text


def _date_key(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 8:
            try:
                return int(digits[:8])
            except ValueError:
                return None
        return None
    return int(pd.Timestamp(parsed).strftime("%Y%m%d"))


def _round_number(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


@dataclass
class AdjustmentFactorTable:
    """Lazy adjustment-factor lookup table.

    Small files are loaded eagerly.  Large canonical metadata files are kept as a
    path and scanned only for requested symbols, so the paper workflow does not
    pull a multi-GB CSV into memory merely to mark a small paper portfolio.
    """

    path: Path | None = None
    rows_by_symbol: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _loaded_all: bool = False

    def has_data(self) -> bool:
        return bool(self.rows_by_symbol) or bool(self.path and self.path.exists())

    def ensure_symbols(self, symbols: Iterable[Any]) -> None:
        wanted = {_symbol(item) for item in symbols if _symbol(item)}
        missing = {item for item in wanted if item not in self.rows_by_symbol}
        if not missing or self._loaded_all or not self.path or not self.path.exists():
            return
        self._load_from_csv(symbols=missing)

    def last_factor_date(self, symbol: Any) -> int | None:
        """Date key (YYYYMMDD) of the newest factor row for ``symbol``, if any."""

        code = _symbol(symbol)
        if not code:
            return None
        if code not in self.rows_by_symbol and not self._loaded_all:
            self.ensure_symbols([code])
        rows = self.rows_by_symbol.get(code) or []
        return rows[-1][0] if rows else None

    def factor_at_or_before(self, symbol: Any, date_value: Any) -> float | None:
        code = _symbol(symbol)
        key = _date_key(date_value)
        if not code or key is None:
            return None
        if code not in self.rows_by_symbol and not self._loaded_all:
            self.ensure_symbols([code])
        rows = self.rows_by_symbol.get(code) or []
        factor: float | None = None
        for row_date, value in rows:
            if row_date <= key:
                factor = value
            else:
                break
        return factor

    def _load_from_csv(self, *, symbols: set[str] | None = None) -> None:
        if not self.path or not self.path.exists():
            return
        wanted = set(symbols or [])
        state = {"loaded_any": False, "invalid_columns": False}

        def _ingest(chunk: pd.DataFrame) -> None:
            chunk = chunk.rename(columns={col: str(col).lstrip("\ufeff") for col in chunk.columns})
            if "symbol" not in chunk.columns and "ts_code" in chunk.columns:
                chunk["symbol"] = chunk["ts_code"]
            if "symbol" not in chunk.columns or "trade_date" not in chunk.columns or "adj_factor" not in chunk.columns:
                state["invalid_columns"] = True
                return
            if wanted:
                pattern = "|".join(re.escape(code) for code in sorted(wanted))
                raw_symbols = chunk["symbol"].astype("string").str.strip()
                chunk = chunk.loc[
                    raw_symbols.str.contains(pattern, regex=True, na=False)
                ].copy()
                if chunk.empty:
                    return
            chunk["symbol"] = chunk["symbol"].map(_symbol)
            if wanted:
                chunk = chunk[chunk["symbol"].isin(wanted)]
            if chunk.empty:
                return
            chunk["_date_key"] = chunk["trade_date"].map(_date_key)
            chunk["adj_factor"] = pd.to_numeric(chunk["adj_factor"], errors="coerce")
            chunk = chunk.dropna(subset=["_date_key", "adj_factor"])
            for code, group in chunk.groupby("symbol"):
                items = self.rows_by_symbol.setdefault(str(code), [])
                for _, row in group.iterrows():
                    factor = safe_float(row.get("adj_factor"), None)
                    date_key = safe_int(row.get("_date_key"), None)
                    if factor is not None and factor > 0 and date_key is not None:
                        items.append((int(date_key), float(factor)))
                        state["loaded_any"] = True

        def _finalize() -> None:
            # Dedup by date (last value wins) then sort ascending so
            # factor_at_or_before can scan in order. Dedup keeps a mid-stream
            # fallback re-ingest safe: the same (date -> factor) collapses.
            for code, items in list(self.rows_by_symbol.items()):
                unique = {date: factor for date, factor in items}
                self.rows_by_symbol[code] = sorted(unique.items())
            if not wanted:
                self._loaded_all = True
            if wanted and not state["loaded_any"]:
                self.warnings.append(f"missing_adjustment_factor_symbols:{','.join(sorted(wanted))}")

        read_kwargs = dict(
            dtype={"symbol": str, "ts_code": str, "trade_date": str},
            usecols=lambda col: str(col).lstrip("\ufeff") in {"symbol", "ts_code", "trade_date", "adj_factor"},
        )
        try:
            for chunk in pd.read_csv(self.path, chunksize=250_000, **read_kwargs):
                _ingest(chunk)
                if state["invalid_columns"]:
                    self.warnings.append(f"invalid_adjustment_factor_columns:{self.path}")
                    return
            _finalize()
        except ParserError as exc:
            # A malformed row must not discard the whole factor table. Keep the
            # rows that parse and skip the bad ones with a visible warning, using
            # the tolerant python engine (the C engine aborts on unterminated
            # quotes instead of skipping).
            self.warnings.append(f"csv_parse_failed:{self.path}:{exc}")
            try:
                frame = pd.read_csv(self.path, engine="python", on_bad_lines="skip", **read_kwargs)
                self.warnings.append(f"csv_bad_lines_skipped:{self.path}")
                _ingest(frame)
                if state["invalid_columns"]:
                    self.warnings.append(f"invalid_adjustment_factor_columns:{self.path}")
                    return
                _finalize()
            except Exception as retry_exc:  # noqa: BLE001 - warning must be visible, not fatal
                self.warnings.append(f"adjustment_factor_load_failed:{self.path}:{retry_exc}")
        except Exception as exc:  # noqa: BLE001 - warning must be visible, not fatal
            self.warnings.append(f"adjustment_factor_load_failed:{self.path}:{exc}")


def _candidate_factor_paths(meta_dir_or_root: Path) -> list[Path]:
    # Round 21 adjudication: only the plural canonical file may be read.
    # The singular ``adjustment_factor.csv`` alias diverged from canonical and
    # must never be a silent fallback data source (price data contract).
    base = Path(meta_dir_or_root)
    if base.is_file():
        return [base]
    return [
        base / "adjustment_factors.csv",
        base / "data" / "metadata" / "adjustment_factors.csv",
    ]


def _alias_factor_paths(meta_dir_or_root: Path) -> list[Path]:
    base = Path(meta_dir_or_root)
    if base.is_file():
        return []
    return [
        base / "adjustment_factor.csv",
        base / "data" / "metadata" / "adjustment_factor.csv",
    ]


def load_adjustment_factor_table(
    meta_dir_or_root: str | Path,
    *,
    symbols: Iterable[Any] | None = None,
    eager_max_bytes: int = 50_000_000,
) -> AdjustmentFactorTable:
    """Load or prepare a local adjustment-factor lookup.

    The returned object is safe for empty/missing metadata: callers should still
    run the valuation policy, which will mark `missing_adj_factor` explicitly.
    """

    for path in _candidate_factor_paths(Path(meta_dir_or_root)):
        if path.exists():
            table = AdjustmentFactorTable(path=path)
            size = path.stat().st_size
            if symbols:
                table.ensure_symbols(symbols)
            elif size <= eager_max_bytes:
                table._load_from_csv(symbols=None)
            return table
    warnings = [f"missing_adjustment_factor_file:{meta_dir_or_root}"]
    for alias in _alias_factor_paths(Path(meta_dir_or_root)):
        if alias.exists():
            warnings.append(
                f"adjustment_factor_alias_not_used:{alias}:"
                "singular alias is not a data source; provide canonical adjustment_factors.csv"
            )
    return AdjustmentFactorTable(warnings=warnings)


def _raw_valuation(position: dict[str, Any], latest_price: float) -> dict[str, Any]:
    quantity = safe_int(position.get("quantity") or position.get("shares"), 0) or 0
    entry_price = safe_float(position.get("entry_price") or position.get("avg_cost") or position.get("buy_price"), latest_price) or latest_price
    cost = entry_price * quantity
    market_value = latest_price * quantity
    pnl = market_value - cost
    row = dict(position)
    row["latest_price"] = _round_number(latest_price)
    row["market_value"] = round(market_value, 2)
    row["unrealized_pnl"] = round(pnl, 2)
    row["return_pct"] = round((pnl / cost) * 100.0, 2) if cost else 0.0
    row["unrealized_return_pct"] = row["return_pct"]
    return row


def apply_paper_adjustment_policy(
    position: dict[str, Any],
    *,
    latest_price: float,
    latest_date: str,
    factors: AdjustmentFactorTable | None,
) -> dict[str, Any]:
    """Mark a paper position using raw price plus adjustment-factor evidence.

    The policy keeps the paper ledger on current-share economic exposure:
    `factor_ratio = latest_adj_factor / entry_adj_factor`.
    """

    row = _raw_valuation(position, latest_price)
    row["latest_date"] = latest_date or row.get("latest_date") or ""
    quantity = safe_int(row.get("quantity") or row.get("shares"), 0) or 0
    entry_price = safe_float(row.get("avg_cost") or row.get("entry_price") or row.get("buy_price"), latest_price) or latest_price
    entry_date = row.get("entry_date") or row.get("buy_date") or row.get("first_buy_date") or ""
    symbol = row.get("symbol") or row.get("code")
    entry_factor = factors.factor_at_or_before(symbol, entry_date) if factors else None
    latest_factor = factors.factor_at_or_before(symbol, latest_date) if factors else None
    if entry_factor is None or latest_factor is None or entry_factor <= 0 or latest_factor <= 0:
        row["price_adjustment_status"] = "missing_adj_factor"
        row["price_adjustment_mode"] = "raw_price_without_adj_factor"
        row["price_adjustment_warning_level"] = "warning"
        row["price_adjustment_warning"] = "missing adjustment factor evidence; raw paper valuation retained"
        row["entry_adj_factor"] = entry_factor
        row["latest_adj_factor"] = latest_factor
        return row

    # A factor table that ends before the valuation date silently returns its
    # last row for every later date, so the ratio reads 1.0 and the position
    # used to be labelled "adjusted" although any ex-rights event after the
    # table's end is invisible. That is stale evidence, not an adjustment.
    last_key = factors.last_factor_date(symbol) if factors else None
    latest_key = _date_key(latest_date)
    if last_key is not None and latest_key is not None and latest_key > last_key:
        last_text = f"{str(last_key)[:4]}-{str(last_key)[4:6]}-{str(last_key)[6:]}"
        row["price_adjustment_status"] = "stale_adj_factor"
        row["price_adjustment_mode"] = "raw_price_factor_table_stale"
        row["price_adjustment_warning_level"] = "warning"
        row["price_adjustment_warning"] = (
            f"adjustment factor table ends {last_text}, before {latest_date}: "
            "ex-rights events after that date are not reflected; raw paper valuation retained"
        )
        row["adj_factor_table_last_date"] = last_text
        row["entry_adj_factor"] = float(entry_factor)
        row["latest_adj_factor"] = float(latest_factor)
        return row

    ratio = latest_factor / entry_factor
    adjusted_quantity = quantity * ratio
    adjusted_avg_cost = entry_price / ratio if ratio else entry_price
    market_value = latest_price * adjusted_quantity
    cost = entry_price * quantity
    pnl = market_value - cost
    row["price_adjustment_status"] = "adjusted"
    row["price_adjustment_mode"] = "raw_price_with_adj_factor_current_basis"
    row["price_adjustment_warning_level"] = "none"
    row["entry_adj_factor"] = float(entry_factor)
    row["latest_adj_factor"] = float(latest_factor)
    row["adjustment_factor_ratio"] = _round_number(ratio, 6)
    row["adjusted_avg_cost"] = _round_number(adjusted_avg_cost)
    row["adjusted_quantity_for_valuation"] = int(adjusted_quantity) if abs(adjusted_quantity - round(adjusted_quantity)) < 1e-9 else round(adjusted_quantity, 4)
    row["market_value"] = round(market_value, 2)
    row["unrealized_pnl"] = round(pnl, 2)
    row["return_pct"] = round((pnl / cost) * 100.0, 2) if cost else 0.0
    row["unrealized_return_pct"] = row["return_pct"]
    return row


def _is_suspended_quote(quote: dict[str, Any]) -> bool:
    flag = quote.get("suspended", quote.get("is_suspended"))
    text = str(flag).strip().lower()
    return bool(flag is True or text in {"1", "true", "yes", "y", "suspended", "停牌"} or quote.get("suspend_type"))


def apply_suspension_valuation_policy(position: dict[str, Any], quote: dict[str, Any] | None) -> dict[str, Any]:
    """Freeze paper valuation for a suspended position when no fresh price exists."""

    row = dict(position)
    quote = dict(quote or {})
    if not _is_suspended_quote(quote):
        row.setdefault("valuation_status", "normal")
        row.setdefault("tradeability_status", "tradable")
        row.setdefault("tradable", True)
        row.setdefault("can_sell", True)
        return row
    quote_price = safe_float(quote.get("price") or quote.get("close") or quote.get("latest_price"), None)
    frozen_price = (
        quote_price
        if quote_price is not None and quote_price > 0
        else safe_float(row.get("last_tradable_price"), None)
        or safe_float(row.get("settled_price"), None)
        or safe_float(row.get("latest_price"), None)
        or safe_float(row.get("last_price"), None)
        or safe_float(row.get("entry_price") or row.get("avg_cost") or row.get("buy_price"), 0.0)
        or 0.0
    )
    quantity = safe_int(row.get("quantity") or row.get("shares"), 0) or 0
    row["latest_price"] = _round_number(frozen_price)
    row["last_tradable_price"] = _round_number(frozen_price)
    row["market_value"] = round(frozen_price * quantity, 2)
    row["valuation_status"] = "suspended_frozen"
    row["tradeability_status"] = "suspended"
    row["tradable"] = False
    row["can_sell"] = False
    row["valuation_price_source"] = "last_tradable_price"
    row["suspend_type"] = quote.get("suspend_type") or row.get("suspend_type") or ""
    row["suspend_reason"] = quote.get("suspend_reason") or row.get("suspend_reason") or ""
    row["valuation_warning"] = "suspended: valuation frozen at last tradable price"
    return row
