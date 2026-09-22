"""Pipeline-owned, date-verified full-A close refresh.

The live-list path only accepts today's completed session. Each symbol is
bound to its own dated Sina HQ quote before a raw bar or valuation is written.
If the list endpoint is unavailable, the existing dated HQ endpoint can supply
raw OHLCV for the local universe. Valuation fields remain explicitly missing.
List and quote requests are batched and paced; neither fetches stock history.

Families:
  1. bars_and_valuation - Sina list plus dated HQ batches; raw append-only
     history with atomic file publication. Anomalies are review labels, never
     inferred suspension evidence.
  2. benchmarks - reuses quantdesk.data_truth.benchmark_incremental_update.
  3. announcements - project news provider for the pool in
     config/data_refresh_news_pool.txt (one request per symbol, 2s pacing).
  4. quarterly - earnings (yjbb) + disclosure schedule, only in
     disclosure-season months (1,4,7,8,10), paced small pages.

Health report: results/data_refresh/data_refresh_latest.json (+ dated copy).
Direct invocation requires a current Pipeline writer lease in addition to
network authorization. Offline invocation writes nothing. Research only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row  # noqa: E402
from quantdesk.interprocess_lock import interprocess_file_lock  # noqa: E402
from scripts.run_data_close_pipeline import MarketWriterLease, MarketWriterUnauthorized  # noqa: E402

DAILY_DIR = PROJECT_ROOT / "data" / "daily"
META_DIR = PROJECT_ROOT / "data" / "metadata"
VAL_DIR = META_DIR / "valuation_snapshots"
EVIDENCE_DIR = META_DIR / "suspension_evidence"
RESULTS_DIR = PROJECT_ROOT / "results" / "data_refresh"
NEWS_POOL_FILE = PROJECT_ROOT / "config" / "data_refresh_news_pool.txt"
UNIVERSE_FILE = PROJECT_ROOT / "data" / "universe" / "a_share_symbols.csv"
DAILY_HEADER = "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at"

UA = "Mozilla/5.0 QuantDesk research client"
SINA_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
DISCLOSURE_MONTHS = {1, 4, 7, 8, 10}
UNIVERSE_MISMATCH_WARN_THRESHOLD = 30
# One bounded re-check per symbol the list snapshot silently dropped (9/21:
# 689009 sat in the local roster and in no HQ request at all).  The cap keeps a
# list outage from turning into a thousands-request sweep, which this project
# requires explicit authorization for; codes above the cap are recorded as not
# attempted instead of being skipped without a trace.
UNIVERSE_DIFF_REQUERY_LIMIT = 50
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _complete_live_session(target: str, now: datetime | None = None) -> bool:
    from quantdesk.market_clock import is_cn_a_share_trading_day, load_cn_trading_calendar

    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    return (
        target == current.date().isoformat()
        and (current.hour, current.minute) >= (15, 5)
        and is_cn_a_share_trading_day(current.date(), calendar=load_cn_trading_calendar())
    )


def _atomic_bytes(path: Path, content: bytes, *, lease: MarketWriterLease) -> None:
    lease.assert_active()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        lease.assert_active()
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class _DatedQuotes(dict[str, dict[str, Any]]):
    def __init__(self) -> None:
        super().__init__()
        self.batch_diagnostics: list[dict[str, Any]] = []


def _dated_close_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Sina list has no session date: bind each row to its own dated HQ quote.

    This adds one paced batch per 100 symbols; it never fetches per-stock
    history. The price/volume match below prevents transferring a date from
    an unrelated or changed quote onto the list snapshot.
    """
    from quantdesk.data_providers import fetch_spot_sina_batch
    from quantdesk.notifications.redaction import redact_text

    quotes = _DatedQuotes()
    for start in range(0, len(symbols), 100):
        pending = symbols[start:start + 100]
        # One bounded retry of missing rows; do not request successful rows twice.
        for attempt in (1, 2):
            requested = set(pending)
            result = fetch_spot_sina_batch(pending, batch_size=100, timeout=10)
            for row in result.rows:
                code = str(row.get("symbol") or "")[-6:]
                if code not in requested:
                    continue
                if code in quotes:
                    quotes[code] = {}  # ambiguous duplicate, never pick one
                else:
                    quotes[code] = row
            pending = sorted(requested - set(quotes))
            quotes.batch_diagnostics.append({
                "batch_start": start, "attempt": attempt,
                "requested_count": len(requested), "received_count": len(result.rows),
                "missing_symbols": pending,
                "provider_errors": [redact_text(str(error))[:500] for error in result.provider_errors],
                "warnings": [redact_text(str(warning))[:500] for warning in result.warnings],
            })
            if not pending or attempt == 2:
                break
            time.sleep(0.8)
        if start + 100 < len(symbols):
            time.sleep(0.8)
    return quotes


def _requery_universe_diff(
    symbols: list[str], *, lease: MarketWriterLease,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Re-check the symbols the list snapshot dropped, one bounded pass each.

    The list endpoint is the primary source, but a code it silently omits would
    otherwise never be requested by anything (9/21: 689009 was in the local
    roster and in no HQ request).  Each difference code goes through the
    existing dated-HQ path exactly once and whatever happens is written to the
    receipt, so a dropped symbol can never be silent again.  Codes above
    ``UNIVERSE_DIFF_REQUERY_LIMIT`` are recorded as not attempted rather than
    turned into an unauthorized full-universe sweep.
    """

    quotes = _DatedQuotes()
    outcomes: dict[str, dict[str, Any]] = {}
    for code in symbols[:UNIVERSE_DIFF_REQUERY_LIMIT]:
        lease.assert_active()
        single = _dated_close_quotes([code])
        for entry in getattr(single, "batch_diagnostics", None) or []:
            quotes.batch_diagnostics.append(
                {**entry, "scope": "universe_diff_requery", "symbol": code}
            )
        quote = single.get(code) or {}
        if quote:
            quotes[code] = quote
            outcomes[code] = {
                "attempted": True, "reason": "",
                "quote_date": quote.get("date"), "quote_time": quote.get("time"),
            }
        else:
            outcomes[code] = {
                "attempted": True, "reason": "universe_diff_requery_no_quote",
                "quote_date": "", "quote_time": "",
                "note": "列表未返回该代码，单只补查也未取得报价；不写日线、不作排除。",
            }
    for code in symbols[UNIVERSE_DIFF_REQUERY_LIMIT:]:
        outcomes[code] = {
            "attempted": False, "reason": "universe_diff_requery_over_limit",
            "quote_date": "", "quote_time": "",
            "note": f"差集超过单轮上限 {UNIVERSE_DIFF_REQUERY_LIMIT}，本轮不请求，缺口保留。",
        }
    return quotes, outcomes


def _dated_quote_target_reason(
    quote: dict[str, Any], target: str, *, observed_at: datetime | None = None,
) -> str:
    """Check a dated HQ quote is usable as the target day's closing evidence.

    This is the list-independent half of ``_close_quote_reason``: it enforces
    the date, completed-session and not-in-the-future rules.  The list-versus-HQ
    value comparison stays in ``_close_quote_reason``, because a symbol the list
    snapshot dropped has no list row to compare against.
    """

    if quote.get("date") != target:
        return "quote_date_missing" if not quote.get("date") else "quote_date_mismatch"
    try:
        quoted_at = datetime.fromisoformat(f"{quote['date']}T{quote['time']}")
    except (KeyError, TypeError, ValueError):
        return "quote_timestamp_invalid"
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=SHANGHAI)
    quoted_at = quoted_at.astimezone(SHANGHAI)
    if quoted_at.date().isoformat() != target:
        return "quote_date_mismatch"
    if (quoted_at.hour, quoted_at.minute) < (15, 0):
        return "quote_before_close"
    current = observed_at or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    # HQ timestamps describe quote updates, which continue after 15:30.
    # Keep same-day/after-close checks and reject future timestamps instead.
    if quoted_at > current:
        return "quote_timestamp_in_future"
    return ""


def _close_quote_reason(
    item: dict[str, Any], quote: dict[str, Any], target: str,
    *, observed_at: datetime | None = None,
) -> str:
    reason = _dated_quote_target_reason(quote, target, observed_at=observed_at)
    if reason:
        return reason
    for list_key, quote_key in (("open", "open"), ("high", "high"), ("low", "low"), ("trade", "price"), ("volume", "volume")):
        try:
            left, right = float(item[list_key]), float(quote[quote_key])
        except (KeyError, TypeError, ValueError):
            return f"quote_value_missing:{list_key}"
        if not math.isfinite(left) or not math.isfinite(right):
            return f"quote_value_nonfinite:{list_key}"
        if not math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-6):
            return f"quote_value_mismatch:{list_key}"
    return ""


def _close_quote_matches(item: dict[str, Any], quote: dict[str, Any], target: str) -> bool:
    return not _close_quote_reason(item, quote, target)


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _resolve_target_date() -> str:
    try:
        from quantdesk.trading_calendar import resolve_target_complete_trade_date

        resolved = resolve_target_complete_trade_date()
        if isinstance(resolved, dict):
            raw = str(resolved.get("effective_target_date") or resolved.get("target_complete_trade_date") or "").strip()
        else:
            raw = str(resolved or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    except Exception:  # noqa: BLE001
        pass
    return datetime.now().strftime("%Y-%m-%d")


def _fmt_price(value: Any) -> str:
    s = str(value).strip()
    if s in ("", "None", "null"):
        return ""
    try:
        number = float(s)
        return repr(number) if math.isfinite(number) else ""
    except (TypeError, ValueError):
        return ""


def _fmt_float1(value: Any) -> str:
    try:
        number = float(value)
        return "%.1f" % number if math.isfinite(number) else ""
    except (TypeError, ValueError):
        return ""


def _fnum(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _load_universe_symbols() -> list[str]:
    if not UNIVERSE_FILE.is_file():
        return []
    out: list[str] = []
    try:
        for line in UNIVERSE_FILE.read_text(encoding="utf-8-sig").splitlines()[1:]:
            sym = line.split(",")[0].strip()
            if sym.isdigit() and len(sym) == 6:
                out.append(sym)
    except OSError:
        return []
    return out


def fetch_market_snapshot(session: requests.Session) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while page <= 80:
        payload = None
        for attempt in range(1, 4):
            try:
                resp = session.get(
                    SINA_URL,
                    params={
                        "page": page,
                        "num": 100,
                        "sort": "symbol",
                        "asc": 1,
                        "node": "hs_a",
                        "symbol": "",
                        "_s_r_a": "init",
                    },
                    headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
                    timeout=25,
                )
                resp.encoding = "GBK"
                payload = resp.json()
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    payload = []
                    break
                time.sleep(3 * attempt)
        if not isinstance(payload, list) or not payload:
            break
        rows.extend(payload)
        if len(payload) < 100:
            break
        page += 1
        time.sleep(0.8)
    return rows


def _append_daily_row(
    path: Path, row: str, *, header: str, lease: MarketWriterLease,
) -> tuple[bool, str]:
    """Validate the complete original frame, then atomically append its bytes."""
    import pandas as pd
    from quantdesk.price_basis import classify_frame_price_basis

    lease.assert_active()
    columns = next(csv.reader([header]))
    parsed = next(csv.reader([row]))
    if len(parsed) != len(columns):
        return False, f"daily_row_column_mismatch:{path.name}"
    incoming = dict(zip(columns, parsed))
    target = incoming["date"]
    try:
        if datetime.strptime(target, "%Y-%m-%d").date().isoformat() != target:
            raise ValueError(target)
    except ValueError:
        return False, f"daily_target_invalid:{path.name}"
    if incoming.get("adjust") != "raw" or incoming.get("symbol") != path.stem:
        return False, f"daily_incoming_contract_invalid:{path.name}"
    valid, reason = daily_bar_validity_from_row(incoming)
    if not valid:
        return False, f"daily_incoming_invalid:{path.name}:{reason}"
    before = path.read_bytes() if path.exists() else b""
    if before:
        try:
            all_rows = list(csv.reader(io.StringIO(before.decode("utf-8-sig"))))
        except (UnicodeError, csv.Error):
            return False, f"daily_csv_unreadable:{path.name}"
        if not all_rows or all_rows[0] != columns:
            return False, f"daily_header_mismatch:{path.name}"
        if any(len(values) != len(columns) for values in all_rows[1:]):
            return False, f"daily_history_column_mismatch:{path.name}"
        frame = pd.DataFrame(all_rows[1:], columns=columns)
        basis, reason = classify_frame_price_basis(frame)
        if basis != "raw":
            return False, f"daily_history_basis_rejected:{path.name}:{basis}:{reason}"
        # No filtering, sorting or deduplication may hide a bad original row.
        dates = frame["date"].tolist()
        try:
            if any(datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day for day in dates):
                raise ValueError("noncanonical date")
        except ValueError:
            return False, f"daily_history_date_invalid:{path.name}"
        if len(set(dates)) != len(dates):
            return False, f"daily_history_duplicate_dates:{path.name}"
        if set(frame["symbol"]) != {path.stem}:
            return False, f"daily_history_symbol_mismatch:{path.name}"
        if target in dates:
            valid, reason = daily_bar_validity_from_row(frame.iloc[dates.index(target)].to_dict())
            if not valid:
                return False, f"daily_existing_target_invalid:{path.name}:{reason}"
            return False, f"daily_target_already_present:{path.name}:{target}"
        if any(day > target for day in dates):
            return False, f"daily_append_would_backdate:{path.name}:{target}"
    extra = io.StringIO(newline="")
    writer = csv.writer(extra, lineterminator="\n")
    if not before:
        writer.writerow(columns)
    writer.writerow(parsed)
    separator = b"\n" if before and not before.endswith((b"\n", b"\r")) else b""
    _atomic_bytes(path, before + separator + extra.getvalue().encode("utf-8"), lease=lease)
    return True, ""


def family_bars_and_valuation(
    target: str, network: bool, *, lease: MarketWriterLease,
) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    if not _complete_live_session(target):
        return {"ok": False, "status": "blocked", "reason": "live_snapshot_requires_today_completed_session"}
    session = requests.Session()
    try:
        rows = fetch_market_snapshot(session)
    finally:
        session.close()
    dated_only = not rows
    quotes: dict[str, dict[str, Any]] | None = None
    if dated_only:
        # The dated HQ endpoint supplies its own OHLCV and session timestamp.
        # A list outage must not prevent using that existing independent path.
        roster = _load_universe_symbols()
        if not roster:
            return {"ok": False, "status": "failed", "reason": "snapshot_empty_and_universe_missing", "fetched": 0}
        lease.assert_active()
        quotes = _dated_close_quotes(roster)
        rows = []
        for code in roster:
            quote = quotes.get(code) or {}
            if not quote:
                continue
            price, previous = _fnum(quote.get("price")), _fnum(quote.get("prev_close"))
            rows.append({
                "code": code, "name": quote.get("name"),
                "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
                "trade": quote.get("price"), "volume": quote.get("volume"), "amount": quote.get("amount"),
                "pricechange": price - previous if previous > 0 else None,
                "changepercent": (price / previous - 1) * 100 if previous > 0 else None,
            })
    from collections import Counter

    counts = Counter(str(item.get("code") or "").strip() for item in rows)
    symbols = sorted(code for code in counts if len(code) == 6 and code.isdigit())
    lease.assert_active()
    if quotes is None:
        quotes = _dated_close_quotes(symbols)
    now = _now_iso()
    stats = {"appended": 0, "already_had_bar": 0, "created_files": 0,
             "suspended_verified": 0, "flagged_for_review": 0, "append_failed": 0}
    review: dict[str, Any] = {}
    warnings: list[str] = (["snapshot_list_empty:using_dated_hq", "valuation_not_updated:list_unavailable"]
                           if dated_only else [])
    universe = _load_universe_symbols()
    missing = sorted(set(universe) - set(symbols))
    if not universe:
        warnings.append("universe_file_missing_or_unreadable:" + str(UNIVERSE_FILE))
    if missing:
        warnings.append(f"universe_snapshot_missing:{len(missing)}")
    if len(symbols) < 5000:
        warnings.append(f"full_a_snapshot_low_coverage:{len(symbols)}")

    # The list is the primary source; a code it silently drops (9/21: 689009)
    # ended up in no request at all.  Every difference code gets one bounded
    # re-check through the same dated-HQ path, and the outcome is kept.  The
    # empty-list fallback already requested the whole roster, so it needs none.
    diff_quotes: dict[str, dict[str, Any]] = {}
    diff_outcomes: dict[str, dict[str, Any]] = {}
    if missing and not dated_only:
        diff_quotes, diff_outcomes = _requery_universe_diff(missing, lease=lease)
        main_diagnostics = getattr(quotes, "batch_diagnostics", None)
        if isinstance(main_diagnostics, list):
            main_diagnostics.extend(getattr(diff_quotes, "batch_diagnostics", None) or [])
        failed_diff = sorted(code for code, row in diff_outcomes.items() if row.get("reason"))
        if failed_diff:
            warnings.append(
                "universe_diff_requery_failed:"
                f"{len(failed_diff)}:{','.join(failed_diff[:8])}"
            )

    val_fields = ["symbol", "name", "close", "pct_chg", "pe", "pb", "total_mv_raw", "circ_mv_raw",
                  "turnover_rate", "volume", "amount", "trade_date", "source", "provider",
                  "total_mv_unit", "pe_semantics", "quote_date", "quote_time"]
    valuations = io.StringIO(newline="")
    valuation_writer = csv.writer(valuations, lineterminator="\n")
    valuation_writer.writerow(val_fields)
    accepted_quotes = 0
    for item in rows:
        lease.assert_active()
        code = str(item.get("code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            continue
        quote = quotes.get(code) or {}
        reason = "duplicate_list_symbol" if counts[code] != 1 else (
            "duplicate_or_missing_hq_quote" if not quote else _close_quote_reason(item, quote, target))
        if reason:
            review[code] = {"reason": reason, "quote_date": quote.get("date"),
                            "quote_time": quote.get("time"),
                            "note": "未获得本交易日收盘证据，不写日线或估值，不认定停牌。"}
            if reason.startswith("quote_value_"):
                review[code]["compared_fields"] = {
                    left: {"list": str(item.get(left)), "dated_quote": str(quote.get(right))}
                    for left, right in (("open", "open"), ("high", "high"),
                                        ("low", "low"), ("trade", "price"), ("volume", "volume"))
                }
            continue
        accepted_quotes += 1
        if not dated_only:
            valuation_writer.writerow([
                code, str(item.get("name") or ""), _fmt_price(item.get("trade")),
                _fmt_price(item.get("changepercent")), _fmt_price(item.get("per")), _fmt_price(item.get("pb")),
                _fmt_price(item.get("mktcap")), _fmt_price(item.get("nmc")), _fmt_price(item.get("turnoverratio")),
                _fmt_float1(item.get("volume")), _fmt_float1(item.get("amount")), target,
                "sina.Market_Center.getHQNodeData+dated_hq", "sina_spot_direct", "wan_yuan", "dynamic",
                quote["date"], quote["time"],
            ])
        valid, reason = daily_bar_validity_from_row({
            "open": item.get("open"), "high": item.get("high"), "low": item.get("low"),
            "close": item.get("trade"), "volume": item.get("volume"),
        })
        if not valid:
            review[code] = {"reason": reason, "quote_date": quote["date"],
                            "note": "异常或零成交行情；无独立停牌证据，不写日线、不作排除。"}
            continue
        row = io.StringIO(newline="")
        csv.writer(row, lineterminator="").writerow([
            target, code, _fmt_price(item.get("open")), _fmt_price(item.get("high")),
            _fmt_price(item.get("low")), _fmt_price(item.get("trade")), _fmt_float1(item.get("volume")),
            _fmt_float1(item.get("amount")), _fmt_price(item.get("pricechange")),
            _fmt_price(item.get("changepercent")), "raw",
            "sina_dated_hq_close" if dated_only else "sina_spot_close", now,
        ])
        path = DAILY_DIR / f"{code}.csv"
        was_new = not path.exists()
        with interprocess_file_lock(DAILY_DIR / ".locks" / f"{code}.lock"):
            appended, reason = _append_daily_row(path, row.getvalue(), header=DAILY_HEADER, lease=lease)
        if not appended and reason.startswith("daily_target_already_present:"):
            stats["already_had_bar"] += 1
        elif not appended:
            stats["append_failed"] += 1
            warnings.append(reason)
            review[code] = {"reason": reason, "note": "原日线保留，未追加。"}
        else:
            stats["appended"] += 1
            stats["created_files"] += int(was_new)
    # Difference symbols have no list row to compare against, so their quotes are
    # validated on the date/close rules only and appended through the same
    # writer and per-symbol lock as every other daily row.
    for code in sorted(diff_quotes):
        lease.assert_active()
        quote = diff_quotes[code]
        outcome = diff_outcomes.setdefault(code, {"attempted": True, "reason": ""})
        reason = _dated_quote_target_reason(quote, target)
        if reason:
            outcome["reason"] = f"universe_diff_requery:{reason}"
            outcome["quote_date"] = quote.get("date")
            outcome["quote_time"] = quote.get("time")
            outcome["note"] = "补查报价不是目标日期的收盘证据；不写日线、不作排除。"
            continue
        valid, reason = daily_bar_validity_from_row({
            "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
            "close": quote.get("price"), "volume": quote.get("volume"),
        })
        if not valid:
            outcome["reason"] = f"universe_diff_requery:{reason}"
            outcome["note"] = "补查行情异常或零成交；不写日线、不作排除。"
            continue
        price, previous = _fnum(quote.get("price")), _fnum(quote.get("prev_close"))
        row = io.StringIO(newline="")
        csv.writer(row, lineterminator="").writerow([
            target, code, _fmt_price(quote.get("open")), _fmt_price(quote.get("high")),
            _fmt_price(quote.get("low")), _fmt_price(quote.get("price")),
            _fmt_float1(quote.get("volume")), _fmt_float1(quote.get("amount")),
            _fmt_price(price - previous if previous > 0 else None),
            _fmt_price((price / previous - 1) * 100 if previous > 0 else None), "raw",
            "sina_dated_hq_universe_diff", now,
        ])
        path = DAILY_DIR / f"{code}.csv"
        was_new = not path.exists()
        with interprocess_file_lock(DAILY_DIR / ".locks" / f"{code}.lock"):
            appended, reason = _append_daily_row(path, row.getvalue(), header=DAILY_HEADER, lease=lease)
        if appended:
            stats["appended"] += 1
            stats["created_files"] += int(was_new)
            outcome["appended"] = True
        elif reason.startswith("daily_target_already_present:"):
            stats["already_had_bar"] += 1
            outcome["appended"] = False
            outcome["note"] = "本地当日日线已存在，未重复写入。"
        else:
            stats["append_failed"] += 1
            outcome["reason"] = f"universe_diff_requery:{reason}"
            warnings.append(reason)
    stats["flagged_for_review"] = len(review)
    val_path = VAL_DIR / f"{target.replace('-', '')}.csv"
    if accepted_quotes and not dated_only:
        _atomic_bytes(val_path, valuations.getvalue().encode("utf-8"), lease=lease)
    if review or missing:
        warnings.append(f"unverified_symbols:{len(review)}:missing_snapshot:{len(missing)}")
    # Deliberately separate from suspension_evidence: this is not exclusion proof.
    review_path = META_DIR / "daily_refresh_review" / f"{target.replace('-', '')}.json"
    _atomic_bytes(review_path, json.dumps({
        "target_date": target, "generated_at": now, "pipeline_run_id": lease.run_id,
        "source": "sina_dated_hq_fallback" if dated_only else "sina_list_and_dated_hq", "verified_exclusions": {},
        "flagged_for_review": review, "missing_in_snapshot": missing,
        "universe_diff_requery": diff_outcomes,
        "universe_diff_requery_limit": UNIVERSE_DIFF_REQUERY_LIMIT,
        "quote_batch_diagnostics": getattr(quotes, "batch_diagnostics", []),
        "contract": "Missing or invalid data remains missing; no suspension is inferred.",
    }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), lease=lease)
    usable = stats["appended"] + stats["already_had_bar"]
    return {
        "ok": bool(usable), "status": "completed_with_warnings" if usable and warnings else "completed" if usable else "failed",
        "snapshot_rows": len(rows), "dated_close_quotes": accepted_quotes, "bars": stats,
        "valuation_snapshot": _display_path(val_path) if accepted_quotes and not dated_only else "",
        "quote_source": "sina_dated_hq_fallback" if dated_only else "sina_list_and_dated_hq",
        "review_evidence": _display_path(review_path), "suspension_evidence": "", "warnings": warnings,
        "universe_diff_requery": {
            "attempted": len([code for code, row in diff_outcomes.items() if row.get("attempted")]),
            "appended": len([code for code, row in diff_outcomes.items() if row.get("appended")]),
            "failed": len([code for code, row in diff_outcomes.items() if row.get("reason")]),
            "limit": UNIVERSE_DIFF_REQUERY_LIMIT,
        },
    }


def family_benchmarks(
    network: bool, *, lease: MarketWriterLease, target_date: str | None = None,
) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    try:
        from quantdesk.data_truth.benchmark_incremental_update import (
            BENCHMARK_SPECS,
            default_index_fetcher,
            update_benchmark_files,
        )
        from tempfile import TemporaryDirectory

        # The legacy updater writes internally. Publish its audited staged
        # bytes only while the Pipeline still owns this run.
        with TemporaryDirectory(prefix="quantdesk-benchmark-") as temp:
            staged_root = Path(temp)
            staged_dir = staged_root / "data" / "benchmark"
            staged_dir.mkdir(parents=True)
            for spec in BENCHMARK_SPECS:
                source = PROJECT_ROOT / "data" / "benchmark" / spec.filename
                if source.is_file():
                    (staged_dir / spec.filename).write_bytes(source.read_bytes())
            target = target_date or lease.target
            result = update_benchmark_files(
                staged_root, fetcher=default_index_fetcher, target_date=target,
            )
            lease.assert_active()
            for item in result.get("results", []):
                if item.get("status") == "appended":
                    filename = str(item["path"])
                    if filename not in {spec.filename for spec in BENCHMARK_SPECS}:
                        raise ValueError("unexpected_benchmark_filename")
                    _atomic_bytes(PROJECT_ROOT / "data" / "benchmark" / filename,
                                  (staged_dir / filename).read_bytes(), lease=lease)
        lease.assert_active()
        statuses = [str(r.get("status")) for r in result.get("results", [])]
        reasons = sorted({str(code) for item in result.get("results", [])
                          for code in item.get("reason_codes", [])})
        if result.get("target_covered") is not True:
            reasons.append("benchmark_target_not_covered")
        return {
            "ok": result.get("target_covered") is True and bool(statuses) and all(
                s in ("updated", "appended", "up_to_date", "skipped_existing_run") for s in statuses
            ),
            "status": result.get("status"),
            "target_date": result.get("target_date"),
            "target_covered": result.get("target_covered"),
            "freshness_status": result.get("freshness_status"),
            "reason_codes": reasons,
            "coverage": {"requested": len(result.get("results", [])),
                         "target_covered": sum(item.get("target_covered") is True for item in result.get("results", []))},
            "indices": [
                {**r, "appended": r.get("appended_count", 0)}
                for r in result.get("results", [])
            ],
        }
    except MarketWriterUnauthorized:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "error", "error_type": type(exc).__name__,
                "target_date": target_date or getattr(lease, "target", None),
                "target_covered": None, "freshness_status": "not_assessed",
                "reason_codes": [f"benchmark_update_failed:{type(exc).__name__}"], "indices": []}


def family_news(network: bool, *, lease: MarketWriterLease) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    if not NEWS_POOL_FILE.exists():
        return {"ok": False, "status": "news_pool_file_missing", "path": str(NEWS_POOL_FILE)}
    symbols = [
        line.strip() for line in NEWS_POOL_FILE.read_text(encoding="utf-8").splitlines() if line.strip().isdigit()
    ]
    if not symbols:
        return {"ok": False, "status": "news_pool_empty"}
    try:
        from quantdesk.providers.news_provider import _fetch_announce_eastmoney
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "import_error", "error": repr(exc)[:150]}
    refreshed = 0
    failures: list[str] = []
    details: list[dict[str, str]] = []
    for sym in symbols:
        lease.assert_active()
        try:
            items = _fetch_announce_eastmoney(sym, 50)
            lease.assert_active()
            if items:
                content = io.StringIO(newline="")
                writer = csv.DictWriter(content, fieldnames=["title", "content", "published", "source", "url"])
                writer.writeheader()
                writer.writerows(items)
                path = PROJECT_ROOT / "data" / "news" / f"{datetime.now(SHANGHAI):%Y%m%d}_{sym}.csv"
                _atomic_bytes(path, content.getvalue().encode("utf-8-sig"), lease=lease)
                refreshed += 1
            else:
                failures.append(sym)
                details.append({"symbol": sym, "reason": "no_fresh_news_rows"})
        except MarketWriterUnauthorized:
            raise
        except Exception as exc:  # noqa: BLE001
            failures.append(sym)
            details.append({"symbol": sym, "reason": type(exc).__name__})
        time.sleep(2.0)
    return {"ok": not failures, "status": "partial" if failures else "completed",
            "pool_size": len(symbols), "refreshed": refreshed, "failures": failures[:10],
            "failure_details": details, "existing_cache_preserved": True}


def family_quarterly(network: bool, force: bool, *, lease: MarketWriterLease) -> dict[str, Any]:
    if not network:
        return {"ok": False, "status": "skipped_network_false"}
    lease.assert_active()
    if not force and datetime.now(SHANGHAI).month not in DISCLOSURE_MONTHS:
        return {"ok": True, "status": "skipped_offseason"}
    try:
        import akshare as ak
    except ImportError:
        return {"ok": False, "status": "akshare_not_installed"}
    out: dict[str, Any] = {"ok": True}
    try:
        year = datetime.now(SHANGHAI).year
        period_candidates = [f"{year}0630", f"{year}0331", f"{year - 1}1231"]
        for period in period_candidates:
            try:
                lease.assert_active()
                df = ak.stock_yjbb_em(date=period)
                if df is not None and len(df) > 0:
                    dest = META_DIR / f"earnings_report_{period[:4]}Q{((int(period[4:6]) - 1) // 3) + 1}.csv"
                    _atomic_bytes(dest, df.to_csv(index=False).encode("utf-8-sig"), lease=lease)
                    out["earnings"] = {"rows": len(df), "file": str(dest.relative_to(PROJECT_ROOT))}
                    break
            except MarketWriterUnauthorized:
                raise
            except Exception:  # noqa: BLE001
                continue
        time.sleep(2)
        lease.assert_active()
        df2 = ak.stock_report_disclosure(market="沪深京", period=f"{year}半年报")
        if df2 is not None and len(df2) > 0:
            dest2 = META_DIR / f"disclosure_schedule_{year}H1.csv"
            _atomic_bytes(dest2, df2.to_csv(index=False).encode("utf-8-sig"), lease=lease)
            out["disclosure"] = {"rows": len(df2), "file": str(dest2.relative_to(PROJECT_ROOT))}
        if "earnings" not in out or "disclosure" not in out:
            out["ok"] = False
            out["status"] = "partial_or_missing"
    except MarketWriterUnauthorized:
        raise
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = repr(exc)[:200]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--network", default="false")
    parser.add_argument("--force-quarterly", action="store_true")
    parser.add_argument("--target-date", default="")
    # Retained syntax only; notification delivery belongs to the Pipeline.
    parser.add_argument("--enable-alert-network", action="store_true")
    parser.add_argument("--alert-push", action="store_true")
    parser.add_argument("--alert-dry-run", default="true")
    parser.add_argument("--no-alerts", action="store_true")
    parser.add_argument("--json", default="true")
    parser.add_argument("--pipeline-run-id", default="")
    args = parser.parse_args(argv)
    from quantdesk.shared import truthy

    network = bool(args.enable_network or truthy(args.network))
    target = args.target_date or _resolve_target_date()
    report: dict[str, Any] = {
        "task_id": "daily_data_refresh", "run_at": _now_iso(), "target_date": target,
        "pipeline_run_id": str(args.pipeline_run_id or "").strip(), "network": network,
        "research_only": True, "no_broker": True, "families": {}, "overall_ok": False,
        "status": "planned_offline" if not network else "blocked", "canonical_write": False,
        "notes": ["already_had_bar counts pre-existing target bars, not this run's downloads"],
    }
    printed = dict(report)
    if not network:
        report["reason_codes"] = ["network_not_authorized:no_writes"]
        printed = dict(report)
    else:
        started = time.monotonic()
        try:
            with MarketWriterLease(PROJECT_ROOT, run_id=report["pipeline_run_id"], target=target) as lease:
                if not _complete_live_session(target):
                    raise MarketWriterUnauthorized("live_snapshot_requires_today_completed_session")
                families = {
                    "bars_and_valuation": lambda: family_bars_and_valuation(target, True, lease=lease),
                    "benchmarks": lambda: family_benchmarks(True, lease=lease, target_date=target),
                    "news": lambda: family_news(True, lease=lease),
                    "quarterly": lambda: family_quarterly(True, args.force_quarterly, lease=lease),
                }
                for name, run in families.items():
                    lease.assert_active()
                    try:
                        report["families"][name] = run()
                    except MarketWriterUnauthorized:
                        raise
                    except Exception as exc:  # family failure is visible, never fabricated
                        report["families"][name] = {"ok": False, "status": "failed", "error_type": type(exc).__name__}
                bars = report["families"]["bars_and_valuation"]
                all_ok = all(v.get("ok") for v in report["families"].values())
                report.update({
                    "overall_ok": all_ok, "canonical_write": True,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "status": "failed" if not bars.get("ok") else "completed" if all_ok and not bars.get("warnings") else "completed_with_warnings",
                })
                benchmark = report["families"]["benchmarks"]
                report["benchmark_evidence"] = {
                    key: benchmark.get(key) for key in (
                        "status", "target_date", "target_covered", "freshness_status", "coverage", "reason_codes"
                    )
                }
                report["reason_codes"] = [f"benchmarks:{code}" for code in benchmark.get("reason_codes", [])]
                if args.enable_alert_network or args.alert_push or not truthy(args.alert_dry_run):
                    report["notes"].append("notification_flags_ignored:use_pipeline_health_alert")
                content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
                latest = RESULTS_DIR / "data_refresh_latest.json"
                _atomic_bytes(RESULTS_DIR / f"data_refresh_{target}.json", content, lease=lease)
                _atomic_bytes(latest, content, lease=lease)
                printed = {**report, "artifact_path": _display_path(latest),
                           "artifact_sha256": "sha256:" + hashlib.sha256(content).hexdigest()}
        except (MarketWriterUnauthorized, OSError) as exc:
            report.update({"status": "blocked", "overall_ok": False,
                           "reason_codes": [str(exc) if isinstance(exc, MarketWriterUnauthorized) else "writer_io_failure"]})
            printed = dict(report)
    if truthy(args.json):
        print(json.dumps(printed, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] in {"completed", "completed_with_warnings", "planned_offline"} else 5 if report["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
