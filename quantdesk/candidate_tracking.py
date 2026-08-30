from __future__ import annotations

import html
import json
import logging
import math
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from quantdesk.shared import read_csv_rows, safe_float, write_json
from quantdesk.shared import write_csv_rows as shared_write_csv_rows

LOG = logging.getLogger(__name__)
TRACKING_DIR = Path("data/tracking")
LEDGER_COLUMNS = [
    "entry_date",
    "ts_code",
    "stock_name",
    "strategy_type",
    "entry_price",
    "entry_signal",
    "data_source",
    "signal_date",
    "candidate_role",
    "generated_batch",
    "strategy_family",
    "strategy_id",
    "research_score",
    "source_file",
    "source_mode",
    "source_run_id",
    "source_hash",
]
PERFORMANCE_COLUMNS = LEDGER_COLUMNS + [
    "hold_1d_ret",
    "hold_3d_ret",
    "hold_5d_ret",
    "hold_10d_ret",
    "hold_20d_ret",
    "max_drawdown",
    "hit_flag",
]
FORBIDDEN_VISIBLE_TOKENS = [
    "".join(parts)
    for parts in [
        ("买", "入"),
        ("卖", "出"),
        ("建", "仓"),
        ("减", "仓"),
        ("止", "损"),
        ("平", "仓"),
        ("目标", "价"),
        ("仓", "位"),
        ("预计", "收益"),
        ("预期", "涨幅"),
        ("券商", "接口"),
        ("实盘", "账户"),
        ("QMT ", "实盘"),
    ]
]


def write_csv_rows(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    shared_write_csv_rows(path, rows, fieldnames=columns, lineterminator="\n")


def today_text() -> str:
    return datetime.now().date().isoformat()


def to_plain_symbol(value: str) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() else text


def to_ts_code(symbol: str) -> str:
    plain = to_plain_symbol(symbol)
    if plain.startswith(("6", "9")):
        suffix = "SH"
    elif plain.startswith(("4", "8")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{plain}.{suffix}"


def load_name_map(root: Path) -> dict[str, str]:
    rows = read_csv_rows(root / "data/universe/historical_listings.csv")
    names: dict[str, str] = {}
    for row in rows:
        symbol = to_plain_symbol(row.get("symbol", ""))
        name = str(row.get("name", "")).strip()
        if symbol and name:
            names[symbol] = name
    return names


def infer_strategy_type(row: dict[str, str]) -> str:
    source_file = str(row.get("_source_file", "")).lower()
    if "_long_" in source_file:
        return "long_term"
    if "_board_" in source_file:
        return "da_ban"
    text = " ".join(str(row.get(key, "")).lower() for key in ["strategy_type", "strategy_family", "strategy_id", "trigger_reason", "signal", "long_signal", "board_signal"])
    if any(token in text for token in ["trend", "long", "carry", "stability", "repair", "rev120"]):
        return "long_term"
    if any(token in text for token in ["breakout", "volume", "board", "limit", "da_ban"]):
        return "da_ban"
    long_score = safe_float(row.get("long_score"))
    board_score = safe_float(row.get("board_score"))
    if long_score is not None or board_score is not None:
        return "da_ban" if (board_score or 0.0) > (long_score or 0.0) else "long_term"
    return "long_term"


def readable_signal(row: dict[str, str]) -> str:
    strategy_id = str(row.get("strategy_id", "")).lower()
    reason = str(row.get("trigger_reason", "")).lower()
    source_file = str(row.get("_source_file", "")).lower()
    if "trend" in strategy_id or "repair" in strategy_id:
        return "趋势修复研究信号"
    if "_long_" in source_file:
        return "长期筛选研究信号"
    if "_board_" in source_file:
        return "量价强势研究信号"
    if "volume" in strategy_id or "breakout" in strategy_id or "liquidity" in reason:
        return "量价突破研究信号"
    return "候选池研究信号"


def infer_data_source(row: dict[str, str]) -> str:
    text = " ".join(str(row.get(key, "")).lower() for key in ["risk_flags", "historical_evidence", "limitation_note", "data_source"])
    if "proxy" in text:
        return "proxy"
    if "reconstructed" in text:
        return "reconstructed"
    return "formal"


def read_daily_history(root: Path, symbol: str) -> list[dict[str, Any]]:
    path = root / "data/daily" / f"{to_plain_symbol(symbol)}.csv"
    rows = read_csv_rows(path)
    out: list[dict[str, Any]] = []
    for row in rows:
        close_text = row.get("adj_close") or row.get("close") or ""
        try:
            close = float(close_text)
        except (TypeError, ValueError) as exc:
            LOG.warning("candidate_tracking_daily_close_parse_failed path=%s symbol=%s value=%r error=%s", path, symbol, close_text, exc)
            continue
        item = {"date": str(row.get("date") or "")[:10], "close": close}
        for key in ["open", "high", "low", "volume", "amount"]:
            try:
                item[key] = float(row.get(key) or close)
            except (TypeError, ValueError) as exc:
                LOG.warning("candidate_tracking_daily_field_parse_failed path=%s symbol=%s field=%s value=%r fallback_used=true error=%s", path, symbol, key, row.get(key), exc)
                item[key] = close if key in {"open", "high", "low"} else 0.0
        out.append(item)
    return [row for row in out if row["date"]]


def price_on_or_after(root: Path, symbol: str, entry_date: str) -> tuple[str, float] | None:
    for row in read_daily_history(root, symbol):
        if row["date"] > entry_date:
            price = row.get("open")
            price_value = safe_float(price)
            if price_value is None or price_value <= 0:
                price_value = safe_float(row.get("close"))
            if price_value is not None and price_value > 0:
                return row["date"], float(price_value)
    return None


def candidate_source_paths(root: Path, candidate_csv: Path | None) -> list[Path]:
    paths: list[Path] = []
    if candidate_csv:
        paths.append(candidate_csv)
    else:
        paths.append(root / "results/daily_research/v420_daily_stock_candidates_latest.csv")
        paths.extend(sorted((root / "reports").glob("20??-??-??_*candidates.csv")))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.resolve())
        if key not in seen and path.exists():
            unique.append(path)
            seen.add(key)
    return unique


def append_candidate_ledger(root: Path, candidate_csv: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    ledger_path = root / TRACKING_DIR / "candidate_ledger.csv"
    source_paths = candidate_source_paths(root, candidate_csv)
    source_rows: list[dict[str, str]] = []
    for path in source_paths:
        for row in read_csv_rows(path):
            row["_source_file"] = path.name
            source_rows.append(row)
    existing = read_csv_rows(ledger_path)
    dynamic_columns = list(LEDGER_COLUMNS)
    for row in existing:
        for key in row:
            if key not in dynamic_columns:
                dynamic_columns.append(key)
    existing_keys = {(row.get("entry_date", ""), row.get("ts_code", ""), row.get("strategy_type", "")) for row in existing}
    names = load_name_map(root)
    appended: list[dict[str, Any]] = []
    for row in source_rows:
        symbol = to_plain_symbol(row.get("symbol", "") or row.get("code", ""))
        if not symbol:
            continue
        entry_date = str(row.get("signal_date") or row.get("date") or today_text())[:10]
        strategy_type = infer_strategy_type(row)
        ts_code = to_ts_code(symbol)
        key = (entry_date, ts_code, strategy_type)
        if key in existing_keys:
            continue
        price_info = price_on_or_after(root, symbol, entry_date)
        close_from_source = safe_float(row.get("close"))
        entry_price = price_info[1] if price_info else (close_from_source if close_from_source is not None else "")
        stock_name = str(row.get("name") or names.get(symbol) or symbol).strip()
        item = {
            "entry_date": entry_date,
            "ts_code": ts_code,
            "stock_name": stock_name,
            "strategy_type": strategy_type,
            "entry_price": f"{entry_price:.4f}" if isinstance(entry_price, float) else "",
            "entry_signal": readable_signal(row),
            "data_source": infer_data_source(row),
            "signal_date": str(row.get("signal_date") or entry_date)[:10],
            "candidate_role": str(row.get("candidate_role") or "daily_recommendation"),
            "generated_batch": str(row.get("generated_batch") or ""),
            "strategy_family": str(row.get("strategy_family") or ""),
            "strategy_id": str(row.get("strategy_id") or ""),
            "research_score": row.get("research_score", ""),
            "source_file": str(row.get("_source_file") or row.get("source_file") or ""),
        }
        appended.append(item)
        existing_keys.add(key)
    all_rows = existing + appended
    write_csv_rows(ledger_path, all_rows, dynamic_columns)
    payload = {
        "task_id": "candidate_writer",
        "status": "completed",
        "branch": "B-alpha_existing_candidate_csv",
        "source_csv": str(source_paths[0].relative_to(root)).replace("\\", "/") if source_paths else "",
        "source_files": [str(path.relative_to(root)).replace("\\", "/") for path in source_paths],
        "ledger": str(ledger_path.relative_to(root)).replace("\\", "/"),
        "source_count": len(source_rows),
        "existing_count_before": len(existing),
        "appended_count": len(appended),
        "total_count": len(all_rows),
        "idempotent_key": ["entry_date", "ts_code", "strategy_type"],
    }
    write_json(root / TRACKING_DIR / "candidate_writer_latest.json", payload)
    return payload


def _formal_tracking_row(
    candidate: dict[str, Any],
    *,
    target_date: str,
    source_path: str,
    source_run_id: str,
    source_hash: str,
) -> dict[str, Any]:
    symbol = to_plain_symbol(candidate.get("symbol", ""))
    close = safe_float(candidate.get("close"))
    return {
        "entry_date": target_date,
        "ts_code": to_ts_code(symbol),
        "stock_name": str(candidate.get("name") or symbol),
        "strategy_type": str(candidate.get("strategy_type") or "formal_research"),
        "entry_price": f"{close:.4f}" if close is not None and close > 0 else "",
        "entry_signal": str(candidate.get("reason") or "正式候选研究信号"),
        "data_source": "formal_candidate_batch",
        "signal_date": target_date,
        "candidate_role": "daily_recommendation",
        "generated_batch": f"formal-candidate-{target_date}",
        "strategy_family": str(candidate.get("strategy_label") or ""),
        "strategy_id": str(candidate.get("strategy_type") or "formal_research"),
        "research_score": candidate.get("score", ""),
        "source_file": source_path,
        "source_mode": "dated_verified_formal_batch",
        "source_run_id": source_run_id,
        "source_hash": source_hash,
    }


def materialize_formal_candidate_tracking(
    root: Path,
    *,
    target_date: str,
    authoritative_chain_run_id: str,
) -> dict[str, Any]:
    """Project one verified dated formal batch into the legacy tracking view.

    This is an explicit compatibility adapter. It never reads the mutable latest
    pointer or legacy candidate CSVs, and it does not mutate the paper account.
    """

    from quantdesk.daily_candidate_refresh import load_formal_candidate_batch
    from quantdesk.formal_candidate_tracking import (
        materialize_formal_candidate_tracking as materialize_formal_evidence,
    )

    root = Path(root).resolve()
    # Produce the immutable, date-addressed evidence first. The compatibility
    # CSV below is only a projection for existing readers and must never become
    # the source of truth for the formal tracking result.
    formal_evidence = materialize_formal_evidence(
        root,
        target_date=target_date,
        authoritative_chain_run_id=authoritative_chain_run_id,
    )
    if formal_evidence.get("status") == "blocked":
        return {
            "status": "blocked",
            "applied": False,
            "target_date": target_date,
            "reason_codes": list(formal_evidence.get("reason_codes") or [])
            or ["formal_candidate_tracking_evidence_blocked"],
        }
    loaded = load_formal_candidate_batch(root, target_date=target_date)
    batch = loaded.get("batch")
    if loaded.get("status") != "passed" or not isinstance(batch, dict):
        return {
            "status": "blocked",
            "applied": False,
            "target_date": target_date,
            "reason_codes": list(loaded.get("reason_codes") or [])
            or ["formal_candidate_batch_not_passed"],
        }
    provenance = batch.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    source_run_id = str(provenance.get("source_run_id") or "")
    source_hash = str(provenance.get("hash") or "")
    if source_run_id != str(authoritative_chain_run_id or ""):
        return {
            "status": "blocked",
            "applied": False,
            "target_date": target_date,
            "reason_codes": ["formal_candidate_tracking_source_run_mismatch"],
        }
    source_path = str(Path(str(loaded.get("path") or "")).relative_to(root)).replace(
        "\\", "/"
    )
    candidates = [row for row in batch.get("candidates") or [] if isinstance(row, dict)]
    projected = [
        _formal_tracking_row(
            row,
            target_date=target_date,
            source_path=source_path,
            source_run_id=source_run_id,
            source_hash=source_hash,
        )
        for row in candidates
    ]
    ledger_path = root / TRACKING_DIR / "candidate_ledger.csv"
    existing = read_csv_rows(ledger_path)
    same_date = [row for row in existing if row.get("entry_date") == target_date]
    foreign = [
        row
        for row in same_date
        if row.get("source_mode") == "dated_verified_formal_batch"
        and (
            row.get("source_run_id") != source_run_id
            or row.get("source_hash") != source_hash
        )
    ]
    if foreign:
        return {
            "status": "blocked",
            "applied": False,
            "target_date": target_date,
            "reason_codes": ["formal_candidate_tracking_provenance_conflict"],
        }
    retained = [
        row
        for row in existing
        if not (
            row.get("entry_date") == target_date
            and row.get("source_mode") == "dated_verified_formal_batch"
        )
    ]
    rows = retained + projected
    write_csv_rows(ledger_path, rows, LEDGER_COLUMNS)

    # The formal adapter owns results/candidate_tracking/daily/<date>.json and
    # latest.json. This compatibility projection intentionally returns its
    # provenance without overwriting that canonical evidence.
    return {
        **formal_evidence,
        "target_date": target_date,
        "candidate_count": len(projected),
        "source_mode": "dated_verified_formal_batch",
        "source_path": source_path,
        "source_run_id": source_run_id,
        "source_hash": source_hash,
        "ledger_path": str(ledger_path.relative_to(root)).replace("\\", "/"),
        "paper_ledger_mutated": False,
        "compatibility_projection": "data/tracking/candidate_ledger.csv",
    }


def nan_text(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "NaN"
    return f"{value:.6f}"


def build_candidate_performance(
    root: Path,
    *,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    ledger_path = root / TRACKING_DIR / "candidate_ledger.csv"
    perf_path = root / TRACKING_DIR / "candidate_performance.csv"
    ledger = read_csv_rows(ledger_path)
    rows: list[dict[str, Any]] = []
    horizons = [1, 3, 5, 10, 20]
    for item in ledger:
        symbol = to_plain_symbol(item.get("ts_code", ""))
        signal_date = str(item.get("entry_date", ""))
        price_info = price_on_or_after(root, symbol, signal_date)
        performance_start = price_info[0] if price_info else signal_date
        history = [row for row in read_daily_history(root, symbol) if row["date"] >= performance_start]
        out = dict(item)
        try:
            entry_price = float(item.get("entry_price") or (price_info[1] if price_info else history[0]["close"] if history else "nan"))
        except (TypeError, ValueError, IndexError) as exc:
            LOG.warning("candidate_tracking_entry_price_parse_failed symbol=%s entry_date=%s value=%r error=%s", symbol, item.get("entry_date"), item.get("entry_price"), exc)
            entry_price = math.nan
        for horizon in horizons:
            key = f"hold_{horizon}d_ret"
            if len(history) > horizon and math.isfinite(entry_price) and entry_price != 0:
                ret = (float(history[horizon]["close"]) - entry_price) / entry_price
                out[key] = nan_text(ret)
            else:
                out[key] = "NaN"
        if history and math.isfinite(entry_price) and entry_price != 0:
            window = history[: min(len(history), 21)]
            peak = entry_price
            max_dd = 0.0
            for row in window:
                close = float(row["close"])
                peak = max(peak, close)
                if peak:
                    max_dd = min(max_dd, close / peak - 1.0)
            out["max_drawdown"] = nan_text(max_dd)
        else:
            out["max_drawdown"] = "NaN"
        try:
            hold_5d = float(out["hold_5d_ret"])
            out["hit_flag"] = "1" if math.isfinite(hold_5d) and hold_5d > 0 else ("0" if math.isfinite(hold_5d) else "NaN")
        except (TypeError, ValueError, KeyError) as exc:
            LOG.warning("candidate_tracking_hit_flag_parse_failed symbol=%s entry_date=%s value=%r error=%s", symbol, item.get("entry_date"), out.get("hold_5d_ret"), exc)
            out["hit_flag"] = "NaN"
        rows.append(out)
    write_csv_rows(perf_path, rows, PERFORMANCE_COLUMNS)
    payload = {
        "task_id": "tracking_engine",
        "status": "completed",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ledger_rows": len(ledger),
        "performance_rows": len(rows),
        "output": str(perf_path.relative_to(root)).replace("\\", "/"),
        "return_definition": "(close_after_n_trading_days_after_t_plus_1_entry - entry_price) / entry_price",
        "short_history_fields": "NaN",
    }
    if provenance:
        payload["provenance"] = dict(provenance)
    write_json(root / TRACKING_DIR / "tracking_engine_latest.json", payload)
    return payload


def period_label(entry_date: str) -> dict[str, str]:
    try:
        d = datetime.strptime(entry_date[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        LOG.warning("candidate_tracking_period_label_parse_failed entry_date=%r error=%s", entry_date, exc)
        d = datetime.now().date()
    iso = d.isocalendar()
    return {"week": f"{iso.year}-W{iso.week:02d}", "month": f"{d.year}-{d.month:02d}", "year": str(d.year), "all": "all_history"}


def summarize_rows(rows: list[dict[str, str]], period: str, strategy_type: str) -> dict[str, Any]:
    hit_values = [safe_float(row.get("hit_flag")) for row in rows]
    ret5 = [safe_float(row.get("hold_5d_ret")) for row in rows]
    ret20 = [safe_float(row.get("hold_20d_ret")) for row in rows]
    dd = [safe_float(row.get("max_drawdown")) for row in rows]
    hit_clean = [v for v in hit_values if v is not None]
    ret5_clean = [v for v in ret5 if v is not None]
    ret20_clean = [v for v in ret20 if v is not None]
    dd_clean = [v for v in dd if v is not None]
    return {
        "period": period,
        "strategy_type": strategy_type,
        "count": len(rows),
        "hit_rate_5d": round(mean(hit_clean), 3) if hit_clean else None,
        "avg_ret_5d": round(mean(ret5_clean), 6) if ret5_clean else None,
        "avg_ret_20d": round(mean(ret20_clean), 6) if ret20_clean else None,
        "avg_max_drawdown": round(mean(dd_clean), 6) if dd_clean else None,
    }


def build_tracking_summary(
    root: Path,
    *,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    perf_path = root / TRACKING_DIR / "candidate_performance.csv"
    rows = read_csv_rows(perf_path)
    today = datetime.now().date()
    current = {
        "week": f"{today.isocalendar().year}-W{today.isocalendar().week:02d}",
        "month": f"{today.year}-{today.month:02d}",
        "year": str(today.year),
        "all": "all_history",
    }
    summary: list[dict[str, Any]] = []
    for key, value in current.items():
        for strategy_type in ["long_term", "da_ban"]:
            selected = [row for row in rows if row.get("strategy_type") == strategy_type and (key == "all" or period_label(row.get("entry_date", ""))[key] == value)]
            summary.append(summarize_rows(selected, value, strategy_type))
    monthly: list[dict[str, Any]] = []
    months = sorted({period_label(row.get("entry_date", ""))["month"] for row in rows})
    for month in months:
        for strategy_type in ["long_term", "da_ban"]:
            selected = [row for row in rows if row.get("strategy_type") == strategy_type and period_label(row.get("entry_date", ""))["month"] == month]
            monthly.append(summarize_rows(selected, month, strategy_type))
    heatmap: dict[str, int] = defaultdict(int)
    for row in rows:
        heatmap[str(row.get("entry_date", ""))[:10]] += 1
    payload = {
        "task_id": "tracking_summary",
        "status": "completed",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "data/tracking/candidate_performance.csv",
        "summary": summary,
        "monthly": monthly,
        "daily_frequency": [{"date": key, "count": value} for key, value in sorted(heatmap.items()) if key],
        "strategy_types": ["long_term", "da_ban"],
    }
    if provenance:
        payload["provenance"] = dict(provenance)
    write_json(root / TRACKING_DIR / "tracking_summary.json", payload)
    return payload


def ema_with_sma_warmup(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2 / (n + 1)
    ema = mean(values[:n])
    out[n - 1] = ema
    for idx in range(n, len(values)):
        ema = values[idx] * alpha + ema * (1 - alpha)
        out[idx] = ema
    return out


def ema_nullable_with_sma_warmup(values: list[float | None], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    valid_positions = [idx for idx, value in enumerate(values) if value is not None]
    if len(valid_positions) < n:
        return out
    first_window = valid_positions[:n]
    ema = mean([float(values[idx]) for idx in first_window if values[idx] is not None])
    out[first_window[-1]] = ema
    alpha = 2 / (n + 1)
    for idx in valid_positions[n:]:
        value = float(values[idx]) if values[idx] is not None else ema
        ema = value * alpha + ema * (1 - alpha)
        out[idx] = ema
    return out


def compute_indicators(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes = [float(row["close"]) for row in history]
    highs = [float(row.get("high") or row["close"]) for row in history]
    lows = [float(row.get("low") or row["close"]) for row in history]
    ema12 = ema_with_sma_warmup(closes, 12)
    ema26 = ema_with_sma_warmup(closes, 26)
    diff: list[float | None] = []
    for a, b in zip(ema12, ema26, strict=False):
        diff.append(None if a is None or b is None else a - b)
    dea_raw = ema_nullable_with_sma_warmup(diff, 9)
    enriched: list[dict[str, Any]] = []
    gains = [0.0]
    losses = [0.0]
    for idx in range(1, len(closes)):
        change = closes[idx] - closes[idx - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    def rsi(n: int) -> list[float | None]:
        out: list[float | None] = [None] * len(closes)
        if len(closes) <= n:
            return out
        avg_gain = mean(gains[1 : n + 1])
        avg_loss = mean(losses[1 : n + 1])
        for idx in range(n, len(closes)):
            if idx > n:
                avg_gain = (avg_gain * (n - 1) + gains[idx]) / n
                avg_loss = (avg_loss * (n - 1) + losses[idx]) / n
            rs = avg_gain / avg_loss if avg_loss else math.inf
            out[idx] = 100 - 100 / (1 + rs)
        return out

    rsi6, rsi12, rsi24 = rsi(6), rsi(12), rsi(24)
    k_values: list[float | None] = [None] * len(closes)
    d_values: list[float | None] = [None] * len(closes)
    k_prev = 50.0
    d_prev = 50.0
    for idx in range(len(closes)):
        if idx >= 8:
            low_n = min(lows[idx - 8 : idx + 1])
            high_n = max(highs[idx - 8 : idx + 1])
            rsv = 50.0 if high_n == low_n else (closes[idx] - low_n) / (high_n - low_n) * 100
            k_prev = (2 / 3) * k_prev + (1 / 3) * rsv
            d_prev = (2 / 3) * d_prev + (1 / 3) * k_prev
            k_values[idx] = k_prev
            d_values[idx] = d_prev
    for idx, row in enumerate(history):
        item = dict(row)
        for n in [5, 10, 20, 60]:
            item[f"ma{n}"] = mean(closes[idx - n + 1 : idx + 1]) if idx + 1 >= n else None
        if idx + 1 >= 20:
            window = closes[idx - 19 : idx + 1]
            mid = mean(window)
            variance = mean([(value - mid) ** 2 for value in window])
            sd = math.sqrt(variance)
            item["boll_mid"] = mid
            item["boll_upper"] = mid + 2 * sd
            item["boll_lower"] = mid - 2 * sd
        else:
            item["boll_mid"] = item["boll_upper"] = item["boll_lower"] = None
        item["diff"] = diff[idx]
        item["dea"] = dea_raw[idx] if diff[idx] is not None else None
        item["macd_bar"] = None if item["diff"] is None or item["dea"] is None else (float(item["diff"]) - float(item["dea"])) * 2
        item["rsi6"], item["rsi12"], item["rsi24"] = rsi6[idx], rsi12[idx], rsi24[idx]
        item["kdj_k"], item["kdj_d"] = k_values[idx], d_values[idx]
        item["kdj_j"] = None if k_values[idx] is None or d_values[idx] is None else 3 * float(k_values[idx]) - 2 * float(d_values[idx])
        enriched.append(item)
    return enriched


def fmt_pct(value: Any) -> str:
    f = safe_float(value)
    return "--" if f is None else f"{f * 100:+.1f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    f = safe_float(value)
    return "--" if f is None else f"{f:.{digits}f}"


def label_strategy(value: str) -> str:
    return "长期" if value == "long_term" else "打板"


def render_tracking_summary_html(root: Path, summary: dict[str, Any]) -> None:
    rows = summary.get("summary", [])
    body = "".join(
        f"<tr><td>{html.escape(row['period'])}</td><td>{label_strategy(row['strategy_type'])}</td><td>{row['count']}</td><td>{fmt_pct(row.get('hit_rate_5d'))}</td><td>{fmt_pct(row.get('avg_ret_5d'))}</td><td>{fmt_pct(row.get('avg_ret_20d'))}</td><td>{fmt_pct(row.get('avg_max_drawdown'))}</td></tr>"
        for row in rows
    )
    text = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>策略追踪汇总</title><style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;background:#0f172a;color:#e5edf7;margin:0;padding:24px}}table{{border-collapse:collapse;width:100%;background:#111827}}td,th{{border:1px solid #334155;padding:10px}}th{{background:#1f2937}}</style></head><body><h1>策略追踪汇总</h1><table><thead><tr><th>周期</th><th>策略</th><th>入选数量</th><th>5日正收益占比</th><th>平均5日收益</th><th>平均20日收益</th><th>平均最大回撤</th></tr></thead><tbody>{body}</tbody></table></body></html>"""
    (root / TRACKING_DIR / "tracking_summary_latest.html").write_text(text, encoding="utf-8")


def latest_market_status(root: Path) -> dict[str, str]:
    candidates = [
        root / "results/data_update/v424_app_open_refresh_latest.json",
        root / "results/daily_research/v421_trading_day_freshness_latest.json",
    ]
    latest = ""
    expected = ""
    is_trading_day = "交易日"
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            page = payload.get("open_app_result_page") or payload
            session = (payload.get("gate") or {}).get("session") or payload
            latest = latest or str(page.get("current_data_date") or session.get("latest_local_trade_date") or "")
            expected = expected or str(page.get("expected_latest_completed_trade_date") or session.get("expected_latest_completed_trade_date") or session.get("latest_expected_trade_date") or "")
            if session.get("is_trading_day") is False:
                is_trading_day = "非交易日"
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            LOG.warning("candidate_tracking_market_status_read_failed path=%s error=%s", path, exc)
            continue
    latest = latest or today_text()
    expected = expected or latest
    try:
        days = (datetime.now().date() - datetime.strptime(latest[:10], "%Y-%m-%d").date()).days
    except ValueError as exc:
        LOG.warning("candidate_tracking_market_status_date_parse_failed latest=%r error=%s", latest, exc)
        days = 0
    return {"latest": latest[:10], "expected": expected[:10], "trading": is_trading_day, "days_ago": str(max(days, 0)), "today": today_text()}


def build_ui_payload(root: Path, limit: int = 12) -> dict[str, Any]:
    ledger = read_csv_rows(root / TRACKING_DIR / "candidate_ledger.csv")
    performance = read_csv_rows(root / TRACKING_DIR / "candidate_performance.csv")
    summary = json.loads((root / TRACKING_DIR / "tracking_summary.json").read_text(encoding="utf-8-sig"))
    latest_date = max([row.get("entry_date", "") for row in ledger] or [today_text()])
    latest_rows = [row for row in ledger if row.get("entry_date") == latest_date][:limit]
    perf_by_key = {(row.get("entry_date", ""), row.get("ts_code", ""), row.get("strategy_type", "")): row for row in performance}
    items: list[dict[str, Any]] = []
    for row in latest_rows:
        symbol = to_plain_symbol(row.get("ts_code", ""))
        history = compute_indicators(read_daily_history(root, symbol))
        series = []
        for item in history:
            series.append(
                {
                    "date": item["date"],
                    "ohlc": [item.get("open"), item.get("close"), item.get("low"), item.get("high")],
                    "close": round(float(item["close"]), 4),
                    "ma5": None if item.get("ma5") is None else round(float(item["ma5"]), 4),
                    "ma10": None if item.get("ma10") is None else round(float(item["ma10"]), 4),
                    "ma20": None if item.get("ma20") is None else round(float(item["ma20"]), 4),
                    "ma60": None if item.get("ma60") is None else round(float(item["ma60"]), 4),
                    "diff": None if item.get("diff") is None else round(float(item["diff"]), 3),
                    "dea": None if item.get("dea") is None else round(float(item["dea"]), 3),
                    "macd_bar": None if item.get("macd_bar") is None else round(float(item["macd_bar"]), 3),
                    "rsi6": None if item.get("rsi6") is None else round(float(item["rsi6"]), 3),
                    "rsi12": None if item.get("rsi12") is None else round(float(item["rsi12"]), 3),
                    "rsi24": None if item.get("rsi24") is None else round(float(item["rsi24"]), 3),
                    "kdj_k": None if item.get("kdj_k") is None else round(float(item["kdj_k"]), 3),
                    "kdj_d": None if item.get("kdj_d") is None else round(float(item["kdj_d"]), 3),
                    "kdj_j": None if item.get("kdj_j") is None else round(float(item["kdj_j"]), 3),
                    "boll_upper": None if item.get("boll_upper") is None else round(float(item["boll_upper"]), 3),
                    "boll_mid": None if item.get("boll_mid") is None else round(float(item["boll_mid"]), 3),
                    "boll_lower": None if item.get("boll_lower") is None else round(float(item["boll_lower"]), 3),
                }
            )
        perf = perf_by_key.get((row.get("entry_date", ""), row.get("ts_code", ""), row.get("strategy_type", "")), {})
        latest_close = series[-1]["close"] if series else safe_float(row.get("entry_price"))
        items.append({"ledger": row, "performance": perf, "series": series, "latest_close": latest_close})
    return {"status": latest_market_status(root), "items": items, "summary": summary}


def visible_safety_check(text: str) -> list[str]:
    system_tokens = ["".join(("research_only", ": true")), "".join(("external_execution_locked", ": true"))]
    return [token for token in FORBIDDEN_VISIBLE_TOKENS + system_tokens if token in text]


def render_daily_report_v2(root: Path) -> dict[str, Any]:
    payload = build_ui_payload(root)
    status = payload["status"]
    items = payload["items"]
    data_json = json.dumps(payload, ensure_ascii=False)
    cards = []
    for idx, item in enumerate(items):
        row = item["ledger"]
        score = fmt_pct(item.get("performance", {}).get("hold_5d_ret"))
        cards.append(
            f"""<button class="candidate-card" data-index="{idx}" data-strategy="{row['strategy_type']}"><span class="code">{html.escape(to_plain_symbol(row['ts_code']))}</span><strong>{html.escape(row['stock_name'])}</strong><span>{fmt_num(item['latest_close'])}</span><span class="score">{score}</span><em>{label_strategy(row['strategy_type'])}</em></button>"""
        )
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QuantDesk 今日日报</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    :root{{--bg:#0b1020;--panel:#121a2e;--panel2:#19233b;--line:#2c3854;--text:#eef4ff;--muted:#a6b3c8;--red:#ff2d2d;--red2:#ff9999;--green:#00aa00;--green2:#99cc99;--blue:#66a3ff;--yellow:#ffe066;--orange:#ffa500}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial,'Microsoft YaHei',sans-serif}} main{{max-width:1280px;margin:0 auto;padding:18px}}
    .top-status,.candidate-zone,.tracking-zone,.more-zone,.detail{{border:1px solid var(--line);background:var(--panel);border-radius:8px;margin-bottom:14px}}
    .top-status{{display:flex;gap:18px;align-items:center;justify-content:space-between;padding:14px 16px}} .top-status strong{{font-size:20px}} .muted{{color:var(--muted)}} .fresh{{color:#7dd3fc}}
    .candidate-zone{{padding:16px}} .section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}} h1,h2,h3{{margin:0}} select,.range button,.tabs button{{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px 10px}}
    .cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}} .candidate-card{{text-align:left;background:linear-gradient(180deg,#1c2742,#11192b);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:13px;min-height:140px;cursor:pointer}} .candidate-card:hover,.candidate-card.active{{border-color:var(--blue)}} .candidate-card .code{{display:block;font-size:20px;font-weight:800}} .candidate-card strong{{display:block;margin:8px 0;font-size:17px}} .candidate-card span{{display:block;margin:5px 0}} .candidate-card .score{{color:#7dd3fc}} .candidate-card em{{display:inline-block;margin-top:5px;font-style:normal;background:#24314f;border:1px solid var(--line);padding:4px 8px;border-radius:999px}}
    .tracking-zone{{padding:16px}} .tracking-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .metric{{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px}} .metric strong{{display:block;margin-bottom:8px}}
    details.more-zone{{padding:14px 16px}} summary{{cursor:pointer;font-weight:800}} .more-links{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}} .more-links a{{color:var(--text);border:1px solid var(--line);background:var(--panel2);border-radius:7px;padding:8px 10px;text-decoration:none}}
    .detail{{display:none;padding:16px}} .detail.open{{display:block}} .info-row{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:12px 0}} .info-row div,.source-row span,.indicator-grid div{{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px}} .chart{{height:320px;margin:12px 0;background:#090d18;border:1px solid var(--line);border-radius:8px}} .macd-chart{{height:240px}} .indicator-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:10px 0}} table{{width:100%;border-collapse:collapse}} th,td{{border:1px solid var(--line);padding:9px;text-align:left}} th{{background:#1d2943}} .source-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}} .source-row span{{display:inline-block}} @media(max-width:900px){{.cards,.tracking-grid,.info-row,.indicator-grid{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<main>
  <section class="top-status"><strong>今日状态栏</strong><span>日期 {html.escape(status['today'])}</span><span>{html.escape(status['trading'])}</span><span class="fresh">数据截至 {html.escape(status['latest'])}（{html.escape(status['days_ago'])}天前）</span></section>
  <section class="candidate-zone"><div class="section-head"><h1>今日候选股</h1><label>策略筛选: <select id="strategyFilter"><option value="all">全部</option><option value="long_term">长期</option><option value="da_ban">打板</option></select></label></div><div class="cards">{''.join(cards)}</div></section>
  <section class="tracking-zone"><div class="section-head"><h2>策略追踪概览</h2><div class="tabs"><button data-period="week">本周</button><button data-period="month">本月</button><button data-period="year">本年</button></div></div><div id="trackingOverview" class="tracking-grid"></div></section>
  <details class="more-zone"><summary>更多报告</summary><div class="more-links"><a href="tracking_report_latest.html">策略追踪报告</a><a href="daily_report_latest.html">原版日报 fallback</a><a href="data_gap_burndown_latest.html">数据缺口看板</a></div></details>
  <section id="detail" class="detail"><h2 id="detailTitle">单股详情</h2><div id="basicInfo" class="info-row"></div><div class="range"><button data-range="20">20</button><button data-range="60">60</button><button data-range="120">120</button><button data-range="all">全部</button></div><div id="klineChart" class="chart"></div><div id="macdChart" class="chart macd-chart"></div><div id="indicatorPanel" class="indicator-grid"></div><h3>策略追踪面板</h3><div id="trackingTable"></div><div id="sourceRow" class="source-row"></div></section>
</main>
<script>
const DATA = {data_json};
let currentIndex = 0; let currentRange = 120;
function fmtPct(v) {{ const n = Number(v); return Number.isFinite(n) ? (n*100).toFixed(1)+'%' : '追踪中'; }}
function fmtNum(v) {{ const n = Number(v); return Number.isFinite(n) ? n.toFixed(3) : '--'; }}
function strategyName(v) {{ return v === 'long_term' ? '长期策略' : '打板策略'; }}
function renderOverview(period='month') {{
  const key = period === 'week' ? 'W' : period === 'month' ? '-' : '';
  const current = period === 'week' ? DATA.status.today.slice(0,4) : period === 'month' ? DATA.status.today.slice(0,7) : DATA.status.today.slice(0,4);
  const rows = DATA.summary.summary.filter(r => period === 'week' ? r.period.includes('W') : period === 'month' ? r.period === current : period === 'year' ? r.period === current : false);
  document.getElementById('trackingOverview').innerHTML = rows.map(r => `<div class="metric"><strong>${{strategyName(r.strategy_type)}}</strong><div>${{period==='week'?'本周':period==='month'?'本月':'本年'}}入选 ${{r.count}} 只</div><div>5日正收益占比 ${{fmtPct(r.hit_rate_5d)}}</div><div>平均5日收益 ${{fmtPct(r.avg_ret_5d)}}</div></div>`).join('');
}}
function sliceSeries(series) {{ return currentRange === 'all' ? series : series.slice(-currentRange); }}
function macdColor(v, prev) {{
  if (v == null) return '#64748b';
  if (v > 0 && (prev == null || v > prev)) return '#FF2D2D';
  if (v > 0) return '#FF9999';
  if (v < 0 && (prev == null || Math.abs(v) > Math.abs(prev))) return '#00AA00';
  return '#99CC99';
}}
function renderDetail(index) {{
  currentIndex = index; const item = DATA.items[index]; if (!item) return;
  document.querySelectorAll('.candidate-card').forEach((b,i)=>b.classList.toggle('active', i===index));
  const row = item.ledger; const series = sliceSeries(item.series);
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailTitle').textContent = `${{row.ts_code}} ${{row.stock_name}}`;
  document.getElementById('basicInfo').innerHTML = `<div>代码<br><strong>${{row.ts_code}}</strong></div><div>名称<br><strong>${{row.stock_name}}</strong></div><div>板块<br><strong>本地映射</strong></div><div>入选日期<br><strong>${{row.entry_date}}</strong></div><div>入选时价格<br><strong>${{row.entry_price}}</strong></div>`;
  const dates = series.map(x=>x.date);
  const kline = echarts.init(document.getElementById('klineChart'));
  kline.setOption({{backgroundColor:'#090d18',tooltip:{{trigger:'axis'}},legend:{{textStyle:{{color:'#e5edf7'}},data:['K线','MA5','MA10','MA20']}},xAxis:{{type:'category',data:dates,axisLabel:{{color:'#a6b3c8'}}}},yAxis:{{scale:true,axisLabel:{{color:'#a6b3c8'}}}},series:[{{name:'K线',type:'candlestick',data:series.map(x=>x.ohlc)}},{{name:'MA5',type:'line',data:series.map(x=>x.ma5),showSymbol:false}},{{name:'MA10',type:'line',data:series.map(x=>x.ma10),showSymbol:false}},{{name:'MA20',type:'line',data:series.map(x=>x.ma20),showSymbol:false}}]}});
  const macd = echarts.init(document.getElementById('macdChart'));
  macd.setOption({{backgroundColor:'#090d18',tooltip:{{trigger:'axis'}},legend:{{textStyle:{{color:'#e5edf7'}},data:['DIFF','DEA','MACD_BAR']}},xAxis:{{type:'category',data:dates,axisLabel:{{color:'#a6b3c8'}}}},yAxis:{{scale:true,axisLabel:{{color:'#a6b3c8'}}}},series:[{{name:'MACD_BAR',type:'bar',data:series.map((x,i)=>({{value:x.macd_bar,itemStyle:{{color:macdColor(x.macd_bar, i?series[i-1].macd_bar:null)}}}}))}},{{name:'DIFF',type:'line',data:series.map(x=>x.diff),showSymbol:false,lineStyle:{{color:'#FFE066'}}}},{{name:'DEA',type:'line',data:series.map(x=>x.dea),showSymbol:false,lineStyle:{{color:'#FFA500'}}}}]}});
  const last = series[series.length-1] || {{}};
  document.getElementById('indicatorPanel').innerHTML = `<div>RSI6 / 12 / 24<br><strong>${{fmtNum(last.rsi6)}} / ${{fmtNum(last.rsi12)}} / ${{fmtNum(last.rsi24)}}</strong></div><div>KDJ(9,3,3)<br><strong>${{fmtNum(last.kdj_k)}} / ${{fmtNum(last.kdj_d)}} / ${{fmtNum(last.kdj_j)}}</strong></div><div>BOLL(20,2)<br><strong>${{fmtNum(last.boll_upper)}} / ${{fmtNum(last.boll_mid)}} / ${{fmtNum(last.boll_lower)}}</strong></div><div>均线<br><strong>MA5 ${{fmtNum(last.ma5)}} | MA20 ${{fmtNum(last.ma20)}}</strong></div>`;
  document.getElementById('trackingTable').innerHTML = `<table><thead><tr><th>入选日期</th><th>入选价</th><th>策略类型</th><th>入选信号</th><th>1日</th><th>5日</th><th>20日</th></tr></thead><tbody><tr><td>${{row.entry_date}}</td><td>${{row.entry_price}}</td><td>${{strategyName(row.strategy_type)}}</td><td>${{row.entry_signal}}</td><td>${{fmtPct(item.performance.hold_1d_ret)}}</td><td>${{fmtPct(item.performance.hold_5d_ret)}}</td><td>${{fmtPct(item.performance.hold_20d_ret)}}</td></tr></tbody></table>`;
  document.getElementById('sourceRow').innerHTML = `<span>价格字段: reconstructed</span><span>候选来源: ${{row.data_source}}</span><span>指标计算: reconstructed</span>`;
}}
document.getElementById('strategyFilter').addEventListener('change', e => document.querySelectorAll('.candidate-card').forEach(card => card.style.display = e.target.value === 'all' || card.dataset.strategy === e.target.value ? '' : 'none'));
document.querySelectorAll('.candidate-card').forEach((card, idx) => card.addEventListener('click', () => renderDetail(idx)));
document.querySelectorAll('.tabs button').forEach(btn => btn.addEventListener('click', () => renderOverview(btn.dataset.period)));
document.querySelectorAll('.range button').forEach(btn => btn.addEventListener('click', () => {{ currentRange = btn.dataset.range === 'all' ? 'all' : Number(btn.dataset.range); renderDetail(currentIndex); }}));
renderOverview('month'); if (DATA.items.length) renderDetail(0);
</script>
</body>
</html>"""
    bad = visible_safety_check(html_text)
    out = root / "reports/v5_daily_report/daily_report_v2_latest.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    return {"path": str(out.relative_to(root)).replace("\\", "/"), "visible_safety_violations": bad, "candidate_cards": len(items)}


def render_tracking_report(root: Path) -> dict[str, Any]:
    summary = json.loads((root / TRACKING_DIR / "tracking_summary.json").read_text(encoding="utf-8-sig"))
    monthly = summary.get("monthly", [])
    monthly_json = json.dumps(monthly, ensure_ascii=False)
    heatmap_json = json.dumps(summary.get("daily_frequency", []), ensure_ascii=False)
    rows = "".join(
        f"<tr><td>{html.escape(row['period'])}</td><td>{label_strategy(row['strategy_type'])}</td><td>{row['count']}</td><td>{fmt_pct(row.get('hit_rate_5d'))}</td><td>{fmt_pct(row.get('avg_ret_5d'))}</td><td>{fmt_pct(row.get('avg_ret_20d'))}</td><td>{fmt_pct(row.get('avg_max_drawdown'))}</td></tr>"
        for row in monthly
    )
    text = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>策略追踪报告</title><script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script><style>body{{margin:0;background:#0b1020;color:#edf3ff;font-family:Arial,'Microsoft YaHei',sans-serif}}main{{max-width:1280px;margin:auto;padding:22px}}section{{border:1px solid #2c3854;background:#121a2e;border-radius:8px;padding:16px;margin-bottom:14px}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #334155;padding:9px}}th{{background:#1d2943;cursor:pointer}}.chart{{height:320px;background:#090d18;border:1px solid #2c3854;border-radius:8px;margin-top:12px}}</style></head><body><main><section><h1>策略追踪报告</h1><p>按月回顾候选池研究表现，仅用于本地研究复盘。</p></section><section><h2>完整策略绩效表</h2><table id="perfTable"><thead><tr><th>月份</th><th>策略</th><th>入选数量</th><th>5日正收益占比</th><th>平均5日收益</th><th>平均20日收益</th><th>平均最大回撤</th></tr></thead><tbody>{rows}</tbody></table></section><section><h2>长期 vs 打板：按月5日正收益占比</h2><div id="barChart" class="chart"></div></section><section><h2>入选频次热力图</h2><div id="heatChart" class="chart"></div></section></main><script>const MONTHLY={monthly_json};const HEAT={heatmap_json};document.querySelectorAll('#perfTable th').forEach((th,i)=>th.addEventListener('click',()=>{{const tb=document.querySelector('#perfTable tbody');[...tb.rows].sort((a,b)=>a.cells[i].textContent.localeCompare(b.cells[i].textContent,'zh-CN',{{numeric:true}})).forEach(r=>tb.appendChild(r));}}));const months=[...new Set(MONTHLY.map(r=>r.period))];const strat=t=>MONTHLY.filter(r=>r.strategy_type===t).map(r=>r.hit_rate_5d==null?0:r.hit_rate_5d);echarts.init(document.getElementById('barChart')).setOption({{backgroundColor:'#090d18',tooltip:{{trigger:'axis'}},legend:{{textStyle:{{color:'#edf3ff'}}}},xAxis:{{type:'category',data:months,axisLabel:{{color:'#a6b3c8'}}}},yAxis:{{axisLabel:{{color:'#a6b3c8',formatter:v=>(v*100).toFixed(0)+'%'}}}},series:[{{name:'长期策略',type:'bar',data:strat('long_term')}},{{name:'打板策略',type:'bar',data:strat('da_ban')}}]}});echarts.init(document.getElementById('heatChart')).setOption({{backgroundColor:'#090d18',tooltip:{{}},xAxis:{{type:'category',data:HEAT.map(x=>x.date),axisLabel:{{color:'#a6b3c8'}}}},yAxis:{{type:'value',axisLabel:{{color:'#a6b3c8'}}}},series:[{{type:'bar',data:HEAT.map(x=>x.count),itemStyle:{{color:'#66a3ff'}}}}]}});</script></body></html>"""
    bad = visible_safety_check(text)
    out = root / "reports/v5_daily_report/tracking_report_latest.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {"path": str(out.relative_to(root)).replace("\\", "/"), "visible_safety_violations": bad}


def render_index(root: Path) -> dict[str, Any]:
    text = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>QuantDesk 研究入口</title><style>body{margin:0;background:#0b1020;color:#eef4ff;font-family:Arial,'Microsoft YaHei',sans-serif}main{max-width:980px;margin:auto;padding:34px}.hero{border:1px solid #2c3854;background:#121a2e;border-radius:8px;padding:26px}.primary{display:inline-block;margin-top:18px;background:#66a3ff;color:#07111f;text-decoration:none;font-weight:800;border-radius:8px;padding:13px 18px}details{margin-top:18px;border:1px solid #2c3854;background:#121a2e;border-radius:8px;padding:14px}a{color:#bfdbfe}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}.grid a{background:#19233b;border:1px solid #2c3854;border-radius:8px;padding:10px;text-decoration:none}@media(max-width:760px){.grid{grid-template-columns:1fr}}</style></head>
<body><main><section class="hero"><h1>今日日报</h1><p>每天从这里打开候选池、策略追踪和单股图表。页面只展示研究复盘信息，不提供交易执行入口。</p><a class="primary" href="daily_report_v2_latest.html">打开今日日报</a></section><details><summary>更多报告</summary><div class="grid"><a href="tracking_report_latest.html">策略追踪报告</a><a href="daily_report_latest.html">原版日报 fallback</a><a href="daily_command_center_latest.html">Command Center</a><a href="data_gap_burndown_latest.html">数据缺口看板</a><a href="pro_terminal_latest.html">研究终端</a><a href="help_center_latest.html">帮助中心</a></div></details></main></body></html>"""
    bad = visible_safety_check(text)
    out = root / "reports/v5_daily_report/index.html"
    out.write_text(text, encoding="utf-8")
    return {"path": str(out.relative_to(root)).replace("\\", "/"), "visible_safety_violations": bad}


def build_review_pack(root: Path, validation: dict[str, Any]) -> dict[str, Any]:
    files = [
        "codex_diagnosis_report.txt",
        "data/tracking/candidate_ledger.csv",
        "data/tracking/candidate_performance.csv",
        "data/tracking/tracking_summary.json",
        "data/tracking/tracking_summary_latest.html",
        "data/tracking/candidate_writer_latest.json",
        "data/tracking/tracking_engine_latest.json",
        "results/v5_daily_report/v660_ui_tracking_completion_summary.json",
        "reports/v5_daily_report/v660_ui_tracking_completion_summary.md",
        "reports/v5_daily_report/daily_report_v2_latest.html",
        "reports/v5_daily_report/tracking_report_latest.html",
        "reports/v5_daily_report/index.html",
        "candidate_writer.py",
        "tracking_engine.py",
        "tracking_summary.py",
        "dist/quantdesk_v660_ui_tracking_review_manifest.json",
    ]
    manifest_path = root / "dist/quantdesk_v660_ui_tracking_review_manifest.json"
    zip_path = root / "dist/quantdesk_v660_ui_tracking_review_pack.zip"
    included = [rel for rel in files if (root / rel).exists()]
    missing = [rel for rel in files if not (root / rel).exists()]
    manifest = {
        "pack_name": "quantdesk_v660_ui_tracking_review_pack",
        "status": "completed" if not missing and not validation.get("visible_safety_violations") else "completed_with_warnings",
        "missing_files": missing,
        "included_files": included,
        "validation": validation,
        "research_only": True,
        "not_a_release": True,
    }
    write_json(manifest_path, manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(set(included + [str(manifest_path.relative_to(root)).replace("\\", "/")])):
            zf.write(root / rel, arcname=rel)
    manifest["zip_size_bytes"] = zip_path.stat().st_size
    write_json(manifest_path, manifest)
    return manifest


def render_all_reports(root: Path) -> dict[str, Any]:
    summary = build_tracking_summary(root)
    render_tracking_summary_html(root, summary)
    daily = render_daily_report_v2(root)
    tracking = render_tracking_report(root)
    index = render_index(root)
    visible = daily["visible_safety_violations"] + tracking["visible_safety_violations"] + index["visible_safety_violations"]
    validation = {
        "daily_report_v2": daily,
        "tracking_report": tracking,
        "index": index,
        "visible_safety_violations": sorted(set(visible)),
        "summary_strategy_types": summary.get("strategy_types", []),
    }
    manifest = build_review_pack(root, validation)
    return {"summary": summary, "validation": validation, "manifest": manifest}
