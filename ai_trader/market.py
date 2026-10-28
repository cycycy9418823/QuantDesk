"""Read-only daily bars from QuantDesk ``data/daily`` or a test fixture dir."""

from __future__ import annotations

import math
import errno
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from ai_trader.paths import DEFAULT_DAILY_DIR, DEFAULT_MINUTE_DIR
from ai_trader.execution_evidence import fingerprint_file, source_reference
from quantdesk.corporate_action_repair import repair_corporate_actions
from quantdesk.csv_io import read_csv_frame
from quantdesk.price_basis import PRICE_BASIS_UNKNOWN, classify_frame_price_basis, validate_price_frame_set
from quantdesk.trading_calendar import resolve_target_complete_trade_date


class DailyPriceWriterBusy(TimeoutError):
    """The Pipeline or a surviving writer child still owns the daily files."""


@contextmanager
def locked_daily_price_reader(daily_dir: Path) -> Iterator[dict[str, Any]]:
    """Use both existing writer bytes; never create or rewrite its lock file.

    The Pipeline owns byte 0 and surviving writer children own byte 1. A reader
    needs both for a coherent local session. Detached fixture directories may
    have no writer lock. Contention is retryable; file/open errors stay visible.
    """

    daily = Path(daily_dir).resolve()
    lock_path = daily.parent / ".locks/data_close_pipeline.lock.json"
    if daily.name != "daily" or daily.parent.name != "data" or not lock_path.exists():
        yield {"status": "absent", "lock_path": str(lock_path)}
        return
    acquired: list[int] = []
    with lock_path.open("r+b") as handle:
        try:
            for offset in (0, 1):
                handle.seek(offset)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - Linux writer uses OFD locks
                        from quantdesk.pipeline_process_guard import acquire_linux_ofd_lock
                        acquire_linux_ofd_lock(handle.fileno(), offset)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise DailyPriceWriterBusy("daily_price_writer_active") from exc
                    raise
                acquired.append(offset)
            yield {"status": "locked", "lock_path": str(lock_path), "writer_bytes": [0, 1]}
        finally:
            if os.name == "nt":
                import msvcrt
                for offset in reversed(acquired):
                    handle.seek(offset)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            # Closing the fd releases Linux OFD locks, including a first byte
            # acquired before the second byte was found to be held by a child.


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


def _tail_lines(path: Path, *, window: int = 4000) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(size - window, 0))
        chunk = handle.read().decode("utf-8", errors="ignore")
    return [line for line in chunk.splitlines() if line.strip()]


def _header_columns(path: Path) -> list[str]:
    """真表头永远在第一行：长 CSV 的尾块里根本没有表头，不能拿尾块首行当表头。"""

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        first = handle.readline()
    return [part.strip().strip('"').lower() for part in first.split(",")]


def _looks_like_date(text: str) -> bool:
    return len(text) >= 10 and text[0:4].isdigit() and text[4] in "-/"


def _valid_day(text: str) -> str:
    """只有真正的日历日期才算有效行：``2026-99-99`` 这种外形像日期的不算。"""

    candidate = str(text or "").strip()[:10].replace("/", "-")
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return ""
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return ""


def _price_column_index(columns: list[str]) -> int | None:
    for key in ("pct_chg", "close"):
        if key in columns:
            return columns.index(key)
    return None


def _latest_dated_bars(path: Path, *, wanted: int = 2) -> list[tuple[str, list[str]]]:
    """最后 ``wanted`` 条**有有效日期**的行（倒序找）。

    - 这样长 CSV 不因为尾块没表头就丢字段；
    - 末尾有半截/残缺行也不会被当成真实最新行（它连日期都不成立）；
    - 但**不会**因为最新那行的涨跌缺失就退回更老的 bar：最新行就是最新行，
      读不到就如实返回未知，不能拿陈旧的一行冒充"今天"。
    """

    columns = _header_columns(path)
    found: list[tuple[str, list[str]]] = []
    for line in reversed(_tail_lines(path)):
        values = [part.strip().strip('"') for part in line.split(",")]
        if len(values) < 2:
            continue
        if columns and [value.lower() for value in values] == columns:
            continue  # 表头
        if len(columns) > 1 and len(values) != len(columns):
            continue  # 缺列或多列的坏行都不是真实 bar（只"不少于表头"不够）
        day = next((value for value in (_valid_day(item) for item in values[:4]) if value), "")
        if not day:
            continue
        found.append((day, values))
        if len(found) >= wanted:
            break
    return found


def last_bar_date(path: Path) -> str | None:
    if not Path(path).is_file():
        return None
    bars = _latest_dated_bars(path, wanted=1)
    return bars[0][0] if bars else None


def last_bar_pct_chg(path: Path) -> float | None:
    if not Path(path).is_file():
        return None
    bars = _latest_dated_bars(path, wanted=2)
    if not bars:
        return None
    columns = _header_columns(path)
    _day, values = bars[0]
    if "pct_chg" in columns and columns.index("pct_chg") < len(values):
        return _safe_float(values[columns.index("pct_chg")])
    if "close" in columns and columns.index("close") < len(values):
        close = _safe_float(values[columns.index("close")])
        if len(bars) < 2:
            return None
        prev_values = bars[1][1]
        prev = _safe_float(prev_values[columns.index("close")]) if columns.index("close") < len(prev_values) else None
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
    latest_day: dict[str, str] = {}
    if root.is_dir():
        for path in sorted(root.glob("*.csv")):
            symbol = path.stem.strip()
            if not (symbol.isdigit() and len(symbol) == 6):
                continue
            bar_date = last_bar_date(path)
            if not bar_date:
                continue
            # "今天"由**每个文件的最新有效日期**决定，与涨跌是否读得到无关。
            latest_day[symbol] = bar_date
            change = last_bar_pct_chg(path)
            if change is None:
                # 涨跌读不到的票不是"异动"：既不能给它名次，也不能凭文件名顺序
                # 冒充一份排名（T17）。它只会在真实异动之后按代码顺序垫底出现。
                continue
            dated.append((bar_date, abs(float(change)), symbol))
    days = set(latest_day.values())
    target = as_of if as_of in days else (max(days) if days else as_of)
    movers = [(change, symbol) for day, change, symbol in dated if day == target]
    # 排序只按真实涨跌；同值时用代码升序，保证结果与文件/glob 顺序无关。
    movers.sort(key=lambda item: (-item[0], item[1]))
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
    as_of: str | None = None,
    _source_input: tuple[pd.DataFrame, dict[str, Any]] | None = None,
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
    source_before = fingerprint_file(path) if _source_input is None else _source_input[1]
    frame = read_csv_frame(path) if _source_input is None else _source_input[0].copy()
    source_rows = len(frame)
    original_basis, original_reason = classify_frame_price_basis(frame)
    original_gate = validate_price_frame_set({symbol: frame}, consumer="feature")
    warnings = list(frame.attrs.get("warnings") or [])
    if frame.empty:
        frame.attrs["warnings"] = warnings
        frame.attrs["price_basis"] = PRICE_BASIS_UNKNOWN
        frame.attrs["price_policy_validation"] = original_gate
        frame.attrs["original_source_rows"] = 0
        return frame
    if not original_gate["eligible"]:
        empty = pd.DataFrame()
        empty.attrs.update(warnings=[*warnings, *original_gate["reason_codes"]],
            price_basis=original_basis, price_basis_reason=original_reason,
            price_policy_validation=original_gate, original_price_basis=original_basis,
            original_source_rows=source_rows)
        return empty
    frame.columns = [str(col).strip().lower() for col in frame.columns]
    if "date" not in frame.columns or "close" not in frame.columns:
        empty = pd.DataFrame()
        empty.attrs["warnings"] = warnings + [f"missing_ohlc_columns:{symbol}"]
        empty.attrs["price_basis"] = PRICE_BASIS_UNKNOWN
        empty.attrs.update(original_price_basis=original_basis, original_source_rows=source_rows,
                           price_policy_validation=original_gate)
        return empty
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if as_of is not None:
        cutoff = pd.to_datetime(as_of, errors="coerce")
        frame = frame.loc[frame["date"] <= cutoff].copy() if pd.notna(cutoff) else frame.iloc[:0].copy()
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "open" in frame.columns:
        frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    for col in ("high", "low"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["close"])
    ca_info: dict[str, Any] = {"ca_bars": 0}
    if adjust_corporate_actions and not frame.empty:
        # Only corporate actions already observable at the cutoff may restate history.
        frame, ca_info = repair_corporate_actions(frame)
        if ca_info.get("unverifiable"):
            empty = pd.DataFrame()
            empty.attrs["warnings"] = warnings + [
                f"unverifiable_corporate_action:{symbol}:{ca_info.get('reason') or ''}"
            ]
            empty.attrs["price_basis"] = PRICE_BASIS_UNKNOWN
            empty.attrs.update(original_price_basis=original_basis, original_source_rows=source_rows,
                               price_policy_validation=original_gate)
            return empty
        if ca_info.get("ca_bars"):
            warnings.append(f"corporate_actions_repaired:{symbol}:{ca_info['ca_bars']}")
    if lookback > 0:
        frame = frame.tail(int(lookback))
    basis, reason = original_basis, original_reason
    source = source_reference(source_before, fingerprint_file(path))
    if source.get("status") != "stable":
        empty = pd.DataFrame()
        empty.attrs.update(warnings=[*warnings, f"daily_source_unverified:{symbol}"],
            price_basis=original_basis, original_price_basis=original_basis,
            original_source_rows=source_rows, price_policy_validation=original_gate,
            source_evidence=source)
        return empty
    transformed_dates: list[str] = []
    if "raw_close" in frame.columns:
        for _, item in frame.iterrows():
            actual, original = _safe_float(item.get("close")), _safe_float(item.get("raw_close"))
            if actual is not None and original is not None and not math.isclose(actual, original, rel_tol=1e-12, abs_tol=1e-12):
                transformed_dates.append(item["date"].strftime("%Y-%m-%d"))
    if transformed_dates:
        frame["adjust"] = "qfq"
        if "price_basis" in frame.columns:
            frame["price_basis"] = "qfq"
        basis, reason = classify_frame_price_basis(frame)
    frame.attrs["warnings"] = warnings
    frame.attrs["price_basis"] = basis
    frame.attrs["original_price_basis"] = original_basis
    frame.attrs["original_source_rows"] = source_rows
    frame.attrs["price_basis_reason"] = reason
    frame.attrs["price_policy_validation"] = original_gate
    frame.attrs["information_cutoff"] = as_of or "latest_input"
    frame.attrs["symbol"] = symbol
    frame.attrs["source_evidence"] = source
    frame.attrs["transformed_dates"] = transformed_dates
    frame.attrs["calculation_basis"] = (
        "corporate_action_repaired_research_proxy" if transformed_dates else "source_prices"
    )
    return frame.reset_index(drop=True)


def load_close_panel(
    symbols: Iterable[str],
    daily_dir: Path | None = None,
    *,
    lookback: int = 180,
    as_of: str | None = None,
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
    series_evidence: dict[str, dict[str, Any]] = {}
    selected_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    original_frames: dict[str, pd.DataFrame] = {}
    source_inputs: dict[str, tuple[pd.DataFrame, dict[str, Any]]] = {}
    excluded: list[dict[str, str]] = []
    for symbol in selected_symbols:
        path = Path(daily_dir or DEFAULT_DAILY_DIR) / f"{symbol}.csv"
        source_before = fingerprint_file(path)
        original = read_csv_frame(path)
        source_inputs[symbol] = (original, source_before)
        if not original.empty:
            classify_frame_price_basis(original)
            original_frames[symbol] = original
    original_policy = validate_price_frame_set(original_frames, consumer="feature")
    if original_frames and not original_policy["eligible"]:
        panel = pd.DataFrame()
        panel.attrs.update(price_policy_validation=original_policy,
            warnings=list(original_policy["reason_codes"]),
            input_evidence={"requested_symbols": selected_symbols, "loaded_symbols": [],
                "excluded_symbols": [{"symbol": symbol, "reason_code": row["reason_code"]}
                    for symbol, row in original_policy["per_symbol"].items() if not row["eligible"]],
                "as_of": as_of, "observed_end": None, "status": "blocked"})
        return panel, pd.DataFrame(), list(original_policy["reason_codes"])
    for symbol in selected_symbols:
        frame = load_symbol_frame(
            symbol, daily_dir, lookback=lookback, adjust_corporate_actions=True, as_of=as_of,
            _source_input=source_inputs[symbol],
        )
        warnings.extend(str(item) for item in (frame.attrs.get("warnings") or []))
        basis = str(frame.attrs.get("original_price_basis") or frame.attrs.get("price_basis") or PRICE_BASIS_UNKNOWN)
        if frame.empty:
            warnings.append(f"empty_daily:{symbol}")
            policy = frame.attrs.get("price_policy_validation") or {}
            # Empty/missing files are data gaps. A nonempty original file with
            # bad basis must block the whole requested panel even after cutoff.
            reasons = policy.get("reason_codes") or []
            excluded.append({"symbol": symbol, "reason_code": ",".join(reasons) or "no_asof_data"})
            continue
        basis = str(frame.attrs.get("price_basis") or PRICE_BASIS_UNKNOWN)
        bases.add(basis)
        if basis == PRICE_BASIS_UNKNOWN:
            warnings.append(f"unlabeled_or_unknown_price_basis:{symbol}")
        indexed = frame.set_index(frame["date"].dt.strftime("%Y-%m-%d"))
        series_evidence[str(symbol)] = {
            "original_price_basis": frame.attrs.get("original_price_basis"),
            "calculation_price_basis": basis,
            "price_basis_reason": frame.attrs.get("price_basis_reason") or "",
            "source_evidence": dict(frame.attrs.get("source_evidence") or {}),
            "transformed_dates": list(frame.attrs.get("transformed_dates") or []),
            "calculation_basis": frame.attrs.get("calculation_basis"),
        }
        closes[symbol] = indexed["close"]
        if "pct_chg" in indexed.columns:
            pct[symbol] = indexed["pct_chg"]
    close_panel = pd.DataFrame(closes).sort_index()
    pct_panel = pd.DataFrame(pct).reindex(close_panel.index) if pct else pd.DataFrame(index=close_panel.index)
    if len(bases) > 1:
        warnings.append("cross_symbol_price_basis_mismatch")
    if not original_policy["eligible"]:
        warnings.extend(original_policy["reason_codes"])
        close_panel, pct_panel = pd.DataFrame(), pd.DataFrame()
    close_panel.attrs["warnings"] = warnings
    close_panel.attrs["price_bases"] = sorted(bases)
    close_panel.attrs["series_evidence"] = series_evidence
    close_panel.attrs["calculation_basis"] = (
        "corporate_action_repaired_research_proxy"
        if any(row.get("transformed_dates") for row in series_evidence.values()) else "source_prices"
    )
    close_panel.attrs["price_policy_validation"] = original_policy
    close_panel.attrs["input_evidence"] = {"requested_symbols": selected_symbols,
        "loaded_symbols": list(close_panel.columns), "excluded_symbols": excluded,
        "as_of": as_of, "observed_end": str(close_panel.index[-1]) if len(close_panel) else None,
        "status": "blocked" if not original_policy["eligible"] else "partial" if excluded else "ready",
        "sources": {symbol: row.get("source_evidence") or {} for symbol, row in series_evidence.items()},
        "price_policy_validation": original_policy}
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


def _status_from_raw(raw: Any) -> tuple[float | None, str]:
    if raw is None:
        return None, "missing"
    token = str(raw).strip()
    if token == "":
        return None, "missing"
    lowered = token.lower()
    if lowered in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        try:
            return float(token), "nonfinite"
        except (TypeError, ValueError):
            return float("nan"), "nonfinite"
    try:
        number = float(token)
    except (TypeError, ValueError):
        return None, "missing"
    if not math.isfinite(number):
        return number, "nonfinite"
    return number, "ok"


def load_ohlc_row(symbol: str, as_of: str, daily_dir: Path | None = None) -> dict[str, Any]:
    """One bar's OHLC plus fill-evidence fields. Does not guess a missing basis."""

    path = Path(daily_dir or DEFAULT_DAILY_DIR) / f"{symbol}.csv"
    # Keep raw tokens and provenance together; the research loader drops rows
    # with no close, which cannot determine whether a next-open fill existed.
    source_before = fingerprint_file(path)
    frame = read_csv_frame(path, dtype=str, keep_default_na=False, on_bad_lines="error")
    basis, basis_reason = classify_frame_price_basis(frame)
    source = source_reference(source_before, fingerprint_file(path))
    if source.get("status") != "stable":
        basis_reason = "daily_file_changed_or_missing_during_read"
    frame.columns = [str(col).strip().lower() for col in frame.columns]
    empty = {
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "pct_chg": None,
        "volume": None,
        "open_status": "missing",
        "volume_status": "missing",
        "price_basis": basis,
        "price_basis_reason": basis_reason,
        "source": "",
        "fetched_at": "",
        "updated_at": "",
        "is_suspended": None,
        "is_st": None,
        "source_evidence": source,
    }
    if frame.empty or "date" not in frame.columns:
        return empty
    dates = pd.to_datetime(frame["date"], errors="coerce")
    session = frame.loc[dates.dt.strftime("%Y-%m-%d") == as_of]
    if session.empty:
        return empty
    row = session.iloc[-1]
    open_px, open_status = _status_from_raw(row.get("open"))
    volume, volume_status = _status_from_raw(row.get("volume"))

    def positive_price(name: str) -> float | None:
        value, status = _status_from_raw(row.get(name))
        return value if status == "ok" and value is not None and value > 0 else None

    return {
        "open": open_px,
        "high": positive_price("high"),
        "low": positive_price("low"),
        "close": positive_price("close"),
        "pct_chg": None if "pct_chg" not in row.index else _safe_float(row.get("pct_chg")),
        "volume": volume,
        "open_status": open_status,
        "volume_status": volume_status,
        "price_basis": basis,
        "price_basis_reason": basis_reason,
        "source": row.get("source", ""),
        "fetched_at": row.get("fetched_at", ""),
        "updated_at": row.get("updated_at", ""),
        "is_suspended": row.get("is_suspended"),
        "is_st": row.get("is_st"),
        "date": as_of,
        "source_evidence": source,
        "original_row": {str(key): str(value) for key, value in row.to_dict().items()},
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
