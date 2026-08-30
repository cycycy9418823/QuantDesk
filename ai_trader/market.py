"""Read-only daily bars from QuantDesk ``data/daily`` or a test fixture dir."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ai_trader.paths import DEFAULT_DAILY_DIR, DEFAULT_MINUTE_DIR
from quantdesk.corporate_action_repair import repair_corporate_actions
from quantdesk.csv_io import read_csv_frame
from quantdesk.price_basis import PRICE_BASIS_UNKNOWN, classify_frame_price_basis
from quantdesk.trading_calendar import resolve_target_complete_trade_date


def list_symbols(daily_dir: Path | None = None) -> list[str]:
    root = Path(daily_dir or DEFAULT_DAILY_DIR)
    if not root.is_dir():
        return []
    symbols: list[str] = []
    for path in sorted(root.glob("*.csv")):
        stem = path.stem.strip()
        if stem.isdigit() and 6 <= len(stem) <= 6:
            symbols.append(stem)
    return symbols


def hunting_ground(symbols: list[str], *, limit: int = 240) -> list[str]:
    if len(symbols) <= limit:
        return list(symbols)
    step = max(len(symbols) // limit, 1)
    picked = symbols[::step][:limit]
    return picked


def last_bar_date(path: Path) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(size - 4000, 0))
        chunk = handle.read().decode("utf-8", errors="ignore")
    lines = [line for line in chunk.splitlines() if line.strip()]
    if not lines:
        return None
    for token in lines[-1].split(",")[:4]:
        text = token.strip().strip('"')
        if len(text) >= 10 and text[0:4].isdigit() and text[4] in "-/":
            return text[:10]
    return None


def last_bar_pct_chg(path: Path) -> float | None:
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(size - 4000, 0))
        chunk = handle.read().decode("utf-8", errors="ignore")
    lines = [line for line in chunk.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    header = [part.strip().lower() for part in lines[0].split(",")]
    values = lines[-1].split(",")
    if "pct_chg" in header and header.index("pct_chg") < len(values):
        return _safe_float(values[header.index("pct_chg")])
    if "close" in header and header.index("close") < len(values):
        close = _safe_float(values[header.index("close")])
        prev_values = lines[-2].split(",")
        prev = _safe_float(prev_values[header.index("close")]) if header.index("close") < len(prev_values) else None
        if close and prev and prev > 0:
            return (close / prev - 1.0) * 100.0
    return None


def focus_universe(
    daily_dir: Path | None,
    as_of: str,
    *,
    held: Iterable[str] = (),
    watch: Iterable[str] = (),
    recommended: Iterable[str] = (),
    limit: int = 80,
) -> list[str]:
    """Names a newbie would actually look at tonight: holdings, watchlist, research-desk picks, then today's movers."""

    root = Path(daily_dir or DEFAULT_DAILY_DIR)
    pinned: list[str] = []
    for symbol in list(held) + list(watch) + list(recommended):
        code = str(symbol or "")
        if code and code not in pinned:
            pinned.append(code)
    dated: list[tuple[str, float, str]] = []
    if root.is_dir():
        for path in root.glob("*.csv"):
            symbol = path.stem.strip()
            if not (symbol.isdigit() and len(symbol) == 6):
                continue
            bar_date = last_bar_date(path)
            if not bar_date:
                continue
            dated.append((bar_date, abs(last_bar_pct_chg(path) or 0.0), symbol))
    available = {row[0] for row in dated}
    target = as_of if as_of in available else (max(available) if available else as_of)
    movers = [(change, symbol) for day, change, symbol in dated if day == target]
    movers.sort(reverse=True)
    names = list(pinned)
    for _change, symbol in movers:
        if symbol not in names:
            names.append(symbol)
        if len(names) >= limit:
            break
    return names[:limit]


def load_symbol_frame(
    symbol: str,
    daily_dir: Path | None = None,
    *,
    lookback: int = 180,
    adjust_corporate_actions: bool = False,
) -> pd.DataFrame:
    """Read one symbol's daily bars.

    ``adjust_corporate_actions`` repairs unadjusted split/dividend cliffs (see
    ``quantdesk.corporate_action_repair``). Turn it ON for anything that looks
    backwards — moving averages, momentum, volatility — because a raw cliff makes
    a 2-for-1 split read as a -50% crash. Leave it OFF for execution, sizing and
    mark-to-market, which must use the price as actually quoted. The repair is
    anchored to the newest bar, so the latest price is identical either way; only
    history is restated.

    A symbol whose cliff cannot be corroborated by ``pct_chg`` is refused (empty
    frame + warning) rather than ranked on a move that may not have happened.
    """

    path = Path(daily_dir or DEFAULT_DAILY_DIR) / f"{symbol}.csv"
    frame = read_csv_frame(path)
    warnings = list(frame.attrs.get("warnings") or [])
    if frame.empty:
        frame.attrs["warnings"] = warnings
        frame.attrs["price_basis"] = PRICE_BASIS_UNKNOWN
        return frame
    frame.columns = [str(col).strip().lower() for col in frame.columns]
    if "date" not in frame.columns or "close" not in frame.columns:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = warnings + [f"missing_ohlc_columns:{symbol}"]
        empty.attrs["price_basis"] = PRICE_BASIS_UNKNOWN
        return empty
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "open" in frame.columns:
        frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    for col in ("high", "low"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["close"])
    if adjust_corporate_actions and not frame.empty:
        # Repair on full history so the back-adjust factors are complete, then tail.
        frame, ca_info = repair_corporate_actions(frame)
        if ca_info.get("unverifiable"):
            empty = pd.DataFrame()
            empty.attrs["warnings"] = warnings + [
                f"unverifiable_corporate_action:{symbol}:{ca_info.get('reason') or ''}"
            ]
            empty.attrs["price_basis"] = PRICE_BASIS_UNKNOWN
            return empty
        if ca_info.get("ca_bars"):
            warnings.append(f"corporate_actions_repaired:{symbol}:{ca_info['ca_bars']}")
    if lookback > 0:
        frame = frame.tail(int(lookback))
    basis, reason = (PRICE_BASIS_UNKNOWN, "unknown_price_basis")
    if "adjust" in frame.columns:
        basis, reason = classify_frame_price_basis(frame)
    frame.attrs["warnings"] = warnings
    frame.attrs["price_basis"] = basis
    frame.attrs["price_basis_reason"] = reason
    frame.attrs["symbol"] = symbol
    return frame.reset_index(drop=True)


def load_close_panel(
    symbols: Iterable[str],
    daily_dir: Path | None = None,
    *,
    lookback: int = 180,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Close/pct panels used for ranking, momentum, volatility and moving averages.

    Corporate actions are repaired here: ``researcher.py`` runs ``pct_change`` and
    ``rolling`` over this panel, and an unadjusted split cliff would otherwise
    read as a genuine crash and dominate every ranking that touches it.
    """

    closes: dict[str, pd.Series] = {}
    pct: dict[str, pd.Series] = {}
    warnings: list[str] = []
    bases: set[str] = set()
    for symbol in symbols:
        frame = load_symbol_frame(
            symbol, daily_dir, lookback=lookback, adjust_corporate_actions=True
        )
        warnings.extend(str(item) for item in (frame.attrs.get("warnings") or []))
        if frame.empty:
            warnings.append(f"empty_daily:{symbol}")
            continue
        basis = str(frame.attrs.get("price_basis") or PRICE_BASIS_UNKNOWN)
        bases.add(basis)
        if basis == PRICE_BASIS_UNKNOWN:
            warnings.append(f"unlabeled_or_unknown_price_basis:{symbol}")
        indexed = frame.set_index(frame["date"].dt.strftime("%Y-%m-%d"))
        closes[symbol] = indexed["close"]
        if "pct_chg" in indexed.columns:
            pct[symbol] = indexed["pct_chg"]
    close_panel = pd.DataFrame(closes).sort_index()
    pct_panel = pd.DataFrame(pct).reindex(close_panel.index) if pct else pd.DataFrame(index=close_panel.index)
    if len(bases) > 1:
        warnings.append("cross_symbol_price_basis_mismatch")
    close_panel.attrs["warnings"] = warnings
    close_panel.attrs["price_bases"] = sorted(bases)
    return close_panel, pct_panel, warnings


def latest_as_of(daily_dir: Path | None = None) -> dict[str, Any]:
    resolved = resolve_target_complete_trade_date()
    as_of = str(resolved.get("target_date") or "")[:10]
    root = Path(daily_dir or DEFAULT_DAILY_DIR)
    return {
        "as_of": as_of,
        "daily_dir": str(root),
        "calendar": resolved,
        "daily_dir_exists": root.is_dir(),
    }


def load_ohlc_row(symbol: str, as_of: str, daily_dir: Path | None = None) -> dict[str, float | None]:
    """One bar's OHLC at the price it was actually quoted.

    Deliberately NOT corporate-action adjusted: callers use this for execution
    fills, lot sizing, mark-to-market and "does a bar exist" checks, and the
    ledger must record real prices. (The repair is anchored to the newest bar, so
    for today's bar it would be a no-op anyway.)
    """

    frame = load_symbol_frame(symbol, daily_dir, lookback=0)
    if frame.empty or "date" not in frame.columns:
        return {"open": None, "high": None, "low": None, "close": None, "pct_chg": None}
    indexed = frame.set_index(frame["date"].dt.strftime("%Y-%m-%d"))
    if as_of not in indexed.index:
        return {"open": None, "high": None, "low": None, "close": None, "pct_chg": None}
    row = indexed.loc[as_of]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    def _num(name: str) -> float | None:
        if name not in row.index:
            return None
        try:
            value = float(row[name])
        except (TypeError, ValueError):
            return None
        return value if value == value and value > 0 else None
    close = _num("close")
    return {
        "open": _num("open"),
        "high": _num("high"),
        "low": _num("low"),
        "close": close,
        "pct_chg": None if "pct_chg" not in row.index else _safe_float(row.get("pct_chg")),
    }


def _safe_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def load_minute_session(
    symbol: str,
    as_of: str,
    minute_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(minute_dir or DEFAULT_MINUTE_DIR)
    path = root / f"{symbol}.csv"
    if not path.is_file():
        return {"status": "missing", "bars": 0, "reason": f"no_minute_file:{symbol}"}
    frame = read_csv_frame(path)
    if frame.empty:
        return {"status": "missing", "bars": 0, "reason": f"empty_minute_file:{symbol}"}
    frame.columns = [str(col).strip().lower() for col in frame.columns]
    date_col = "date" if "date" in frame.columns else None
    if date_col is None:
        return {"status": "missing", "bars": 0, "reason": "minute_missing_date_column"}
    text = frame[date_col].astype(str).str.slice(0, 10)
    session = frame.loc[text == as_of].copy()
    if session.empty:
        return {"status": "missing", "bars": 0, "reason": f"no_minute_bars:{symbol}:{as_of}"}
    for col in ("open", "high", "low", "close"):
        if col in session.columns:
            session[col] = pd.to_numeric(session[col], errors="coerce")
    bars = int(len(session))
    low = float(session["low"].min()) if "low" in session.columns else None
    high = float(session["high"].max()) if "high" in session.columns else None
    close = float(session["close"].iloc[-1]) if "close" in session.columns else None
    status = "complete" if bars >= 200 else "partial"
    location = None
    if low and high and close and high > low:
        location = round((close - low) / (high - low), 4)
    return {
        "status": status,
        "bars": bars,
        "low": low,
        "high": high,
        "close": close,
        "close_location": location,
        "reason": "",
    }
