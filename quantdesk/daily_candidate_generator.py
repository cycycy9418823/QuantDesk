"""Generate the daily research candidate pool from local daily bars.

The generator is intentionally bounded and research-only. It reads local
``data/daily`` files, runs the existing strategy-lab filters, writes a latest
candidate CSV, and appends new-date rows into the tracking ledger. It does not
fetch market data, scan full A-share by default, connect brokers, or produce
trading instructions.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.candidate_tracking import price_on_or_after, to_ts_code
from quantdesk.screening.candidate_filters import filter_candidate_rows
from quantdesk.shared import normalize_symbol, read_csv_rows, read_json, safe_float, write_csv_rows, write_json
from quantdesk.strategy_lab.limit_up_strategy import generate_limit_up
from quantdesk.strategy_lab.long_term_strategy import generate_long_term
from quantdesk.strategy_lab.low_price_strategy import generate_low_price
from quantdesk.strategy_lab.rolling_strategy_backtest import symbol_metrics

LEDGER_BASE_COLUMNS = [
    "entry_date",
    "ts_code",
    "stock_name",
    "strategy_type",
    "entry_price",
    "entry_signal",
    "data_source",
]

LEDGER_DAILY_COLUMNS = [
    "signal_date",
    "candidate_role",
    "generated_batch",
    "strategy_family",
    "strategy_id",
    "research_score",
    "source_file",
]

DAILY_CANDIDATE_COLUMNS = [
    "symbol",
    "name",
    "signal_date",
    "strategy_family",
    "strategy_id",
    "strategy_type",
    "research_score",
    "trigger_reason",
    "risk_flags",
    "research_signal_label",
    "historical_evidence",
    "limitation_note",
    "prohibited_usage_note",
    "candidate_role",
    "generated_batch",
]

BOUNDARY = {
    "research_only": True,
    "paper_only": True,
    "no_broker": True,
    "no_live_trading": True,
    "no_auto_execution": True,
    "no_buy_sell_advice": True,
    "no_position_advice": True,
    "no_real_money_allocation": True,
    "full_scan": False,
}


def _latest_date_from_daily_files(root: Path, *, max_files: int = 6000) -> str:
    dates: list[str] = []
    daily_dir = root / "data" / "daily"
    for idx, path in enumerate(sorted(daily_dir.glob("*.csv"))):
        if max_files and idx >= max_files:
            break
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                last = None
                for last in csv.DictReader(handle):
                    pass
            text = str((last or {}).get("date") or "")[:10]
            if text:
                dates.append(text)
        except Exception:
            continue
    return max(dates) if dates else ""


def _load_name_map(root: Path) -> dict[str, str]:
    rows = []
    for rel in ("data/cache/spot_snapshot_latest.csv", "data/universe/historical_listings.csv"):
        rows.extend(read_csv_rows(root / rel))
    names: dict[str, str] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("ts_code"))
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        if symbol and name and name.lower() != "nan":
            names.setdefault(symbol, name)
    return names


def _spot_market_universe(root: Path, *, limit: int) -> list[str]:
    """Build a bounded local market universe from the cached spot snapshot."""

    rows = read_csv_rows(root / "data" / "cache" / "spot_snapshot_latest.csv")
    ranked: list[tuple[float, str]] = []
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        name = str(row.get("name") or "").upper()
        if not symbol or "ST" in name or "退" in name:
            continue
        price = safe_float(row.get("price"), None)
        amount = safe_float(row.get("amount"), None)
        pct = safe_float(row.get("pct_chg"), 0.0) or 0.0
        if price is None or price <= 0 or amount is None or amount <= 0:
            continue
        if not (root / "data" / "daily" / f"{symbol}.csv").exists():
            continue
        # Prefer liquid and currently moving names, without turning this into
        # a full-market scan or a trading signal.
        score = amount / 100_000_000 + abs(pct) * 0.6
        ranked.append((score, symbol))
    ranked.sort(reverse=True)
    symbols: list[str] = []
    seen: set[str] = set()
    for _, symbol in ranked:
        if symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def _fallback_daily_universe(root: Path, *, limit: int) -> list[str]:
    symbols: list[str] = []
    for path in sorted((root / "data" / "daily").glob("*.csv")):
        symbol = normalize_symbol(path.stem)
        if symbol:
            symbols.append(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def _load_market_metrics_for_symbols(root: Path, symbols: list[str]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for symbol in symbols:
        item = symbol_metrics(root / "data" / "daily" / f"{symbol}.csv")
        if item:
            metrics.append(item)
    return metrics


def _score(value: Any) -> float:
    number = safe_float(value, None)
    return float(number) if number is not None else 0.0


def _strategy_type(strategy_family: str, strategy_id: str) -> str:
    text = f"{strategy_family} {strategy_id}".lower()
    if "limit_up" in text or "volume_breakout" in text:
        return "da_ban"
    return "long_term"


def _entry_signal(strategy_type: str, strategy_id: str) -> str:
    if strategy_type == "da_ban":
        return "量价突破观察信号"
    if "low_vol" in strategy_id:
        return "低波趋势观察信号"
    return "趋势修复观察信号"


def _row_score(row: dict[str, Any]) -> float:
    for key in ("research_score", "recent_return", "liquidity_score"):
        number = safe_float(row.get(key), None)
        if number is not None:
            return float(number)
    return 0.0


def _normalize_source_row(
    row: dict[str, Any],
    *,
    strategy_family: str,
    strategy_id: str,
    latest_signal_date: str,
    generated_batch: str,
    names: dict[str, str],
) -> dict[str, Any]:
    symbol = normalize_symbol(row.get("symbol") or row.get("stock_symbol"))
    sid = str(row.get("strategy_id") or row.get("pattern_detected") or strategy_id).strip()
    signal_date = str(row.get("signal_date") or latest_signal_date)[:10]
    strategy_type = _strategy_type(strategy_family, sid)
    name = str(row.get("name") or row.get("stock_name") or names.get(symbol) or symbol).strip()
    return {
        "symbol": symbol,
        "name": name,
        "signal_date": signal_date,
        "strategy_family": strategy_family,
        "strategy_id": sid,
        "strategy_type": strategy_type,
        "research_score": _row_score(row),
        "trigger_reason": str(row.get("trigger_reason") or row.get("pattern_detected") or row.get("research_reason") or ""),
        "risk_flags": str(row.get("risk_flags") or ""),
        "research_signal_label": str(row.get("research_signal_label") or "research_candidate_watch"),
        "historical_evidence": str(row.get("historical_evidence") or row.get("historical_distribution_note") or "local_daily_research_filter"),
        "limitation_note": str(row.get("limitation_note") or "local daily-bar research filter; metadata may be incomplete"),
        "prohibited_usage_note": str(row.get("prohibited_usage_note") or "research_only_review_not_trade_instruction"),
        "candidate_role": "daily_recommendation",
        "generated_batch": generated_batch,
    }


def _family_bucket(strategy_family: str) -> str:
    """Map a generator strategy_family to the canonical effectiveness bucket."""
    text = str(strategy_family or "").lower()
    if "limit_up" in text or "da_ban" in text or "打板" in text:
        return "打板复核"
    return "长期/波段复核"


def _strategy_weights(root: Path) -> dict[str, float]:
    """Read the soft strategy-effectiveness weights persisted by the server.

    Missing/unreadable -> empty, which makes ranking identical to the previous
    fixed-priority behavior. Research-only re-ranking signal; never raises.
    """
    weights, _warnings = _strategy_weights_with_warnings(root)
    return weights


#: Governance fields an effectiveness-weight artifact must carry before it may
#: tilt the default candidate ordering. Research weights are unvalidated by
#: default, so consuming them silently would let an unreviewed artifact change
#: the formal research pool.
_WEIGHT_GOVERNANCE_FIELDS = ("version", "as_of", "config_hash", "adjudication_id")


def _strategy_weights_with_warnings(
    root: Path, *, consume_research_weights: bool = False
) -> tuple[dict[str, float], list[str]]:
    """Read soft strategy weights and surface unreadable/ungoverned warnings.

    Neutral by default: the persisted weights are research output with no
    adjudication behind them, so the generator ignores them unless a caller
    explicitly opts in *and* the artifact carries full governance provenance
    (``version``, ``as_of``, ``config_hash``, ``adjudication_id``) plus
    ``publication_status == "approved"``. Anything else degrades to neutral with
    a visible warning rather than silently re-ranking the formal pool.
    """

    try:
        data = read_json(root / "results" / "strategy_effectiveness_weights.json", {})
    except Exception as exc:
        return {}, [f"strategy_weights_unreadable:{exc}"]
    if not isinstance(data, dict):
        return {}, []
    weights = data.get("weight_by_bucket")
    if not isinstance(weights, dict) or not weights:
        return {}, []

    if not consume_research_weights:
        return {}, ["strategy_weights_not_consumed:neutral_default_requires_explicit_opt_in"]

    missing = [field for field in _WEIGHT_GOVERNANCE_FIELDS if not str(data.get(field) or "").strip()]
    if missing:
        return {}, [f"strategy_weights_ungoverned:missing_{'+'.join(missing)}"]
    status = str(data.get("publication_status") or "").strip().lower()
    if status != "approved":
        return {}, [f"strategy_weights_not_approved:publication_status={status or 'missing'}"]

    out: dict[str, float] = {}
    for key, value in weights.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out, []


def _dedupe_and_rank(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    required_signal_date: str = "",
    strategy_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    strategy_weights = strategy_weights or {}
    priority = {"long_term_family": 3, "low_price_family": 2, "limit_up_family": 1}

    def _effective_priority(row: dict[str, Any]) -> float:
        family = str(row.get("strategy_family") or "")
        base = float(priority.get(family, 0))
        try:
            weight = float(strategy_weights.get(_family_bucket(family), 1.0) or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        # Soft tilt: a full [0.6,1.4] weight shifts family priority by ±0.8, so a
        # validating strategy can edge ahead and a failing one drift back, while
        # neutral/empty weights reproduce the original fixed priority exactly.
        return base + (weight - 1.0) * 2.0

    rows = [row for row in rows if row.get("symbol") and row.get("signal_date")]
    if required_signal_date:
        rows = [row for row in rows if str(row.get("signal_date") or "")[:10] == required_signal_date]
    rows.sort(
        key=lambda row: (
            str(row.get("signal_date") or ""),
            _effective_priority(row),
            _score(row.get("research_score")),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append(row)
        if limit and len(selected) >= limit:
            break
    return selected


def _entry_price(root: Path, symbol: str, signal_date: str) -> str:
    entry = price_on_or_after(root, symbol, signal_date)
    return f"{entry[1]:.2f}" if entry is not None else "数据缺失"


def _ledger_columns(existing_rows: list[dict[str, str]]) -> list[str]:
    columns: list[str] = []
    for key in LEDGER_BASE_COLUMNS + LEDGER_DAILY_COLUMNS:
        if key not in columns:
            columns.append(key)
    for row in existing_rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def append_daily_candidates_to_ledger(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ledger_path = root / "data" / "tracking" / "candidate_ledger.csv"
    existing = read_csv_rows(ledger_path)
    existing_keys = {
        (
            str(row.get("entry_date") or "")[:10],
            str(row.get("ts_code") or ""),
            str(row.get("strategy_type") or ""),
        )
        for row in existing
    }
    appended: list[dict[str, Any]] = []
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        signal_date = str(row.get("signal_date") or "")[:10]
        strategy_type = str(row.get("strategy_type") or "")
        ts_code = to_ts_code(symbol)
        key = (signal_date, ts_code, strategy_type)
        if not symbol or not signal_date or key in existing_keys:
            continue
        item = {
            "entry_date": signal_date,
            "ts_code": ts_code,
            "stock_name": row.get("name") or symbol,
            "strategy_type": strategy_type,
            "entry_price": _entry_price(root, symbol, signal_date),
            "entry_signal": _entry_signal(strategy_type, str(row.get("strategy_id") or "")),
            "data_source": "formal",
            "signal_date": signal_date,
            "candidate_role": "daily_recommendation",
            "generated_batch": row.get("generated_batch") or "",
            "strategy_family": row.get("strategy_family") or "",
            "strategy_id": row.get("strategy_id") or "",
            "research_score": row.get("research_score") or "",
            "source_file": "results/daily_research/v420_daily_stock_candidates_latest.csv",
        }
        appended.append(item)
        existing_keys.add(key)
    if appended or not ledger_path.exists():
        write_csv_rows(ledger_path, existing + appended, _ledger_columns(existing + appended), encoding="utf-8-sig")
    return {
        "ledger": "data/tracking/candidate_ledger.csv",
        "existing_count_before": len(existing),
        "appended_count": len(appended),
        "total_count": len(existing) + len(appended),
    }


def generate_daily_recommendations(
    root: Path,
    *,
    max_scan_symbols: int = 300,
    max_candidates: int = 18,
    append_ledger: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    max_scan_symbols = max(1, int(max_scan_symbols or 1))
    max_candidates = max(1, int(max_candidates or 1))
    names = _load_name_map(root)
    scan_symbols = _spot_market_universe(root, limit=max_scan_symbols)
    universe_source = "spot_snapshot_liquidity_momentum"
    if not scan_symbols:
        scan_symbols = _fallback_daily_universe(root, limit=max_scan_symbols)
        universe_source = "local_daily_alphabetical_fallback"
    raw_metrics = _load_market_metrics_for_symbols(root, scan_symbols)
    metrics, excluded_metrics = filter_candidate_rows(raw_metrics, name_map=names)
    low_summary, low_rows = generate_low_price(metrics)
    long_summary, long_rows = generate_long_term(metrics)
    limit_summary, limit_rows, _limit_dist = generate_limit_up(metrics)
    _row_signal_dates = [
        str(row.get("signal_date") or "")[:10]
        for row in [*low_rows, *long_rows, *limit_rows]
        if row.get("signal_date")
    ]
    # 日期来源分级：策略行 signal_date > 本地日线最后交易日 > 系统时钟兜底。
    # 系统时钟兜底属于「拿不到任何数据日期」的降级，绝不能静默冒充成数据日期，
    # 否则会写进账本 entry_date 并驱动入场价查找（日期口径/前视风险）。
    signal_date_source = ""
    if _row_signal_dates:
        latest_signal_date = max(_row_signal_dates)
        signal_date_source = "strategy_signal_date"
    else:
        _local_daily_date = _latest_date_from_daily_files(root)
        if _local_daily_date:
            latest_signal_date = _local_daily_date
            signal_date_source = "local_daily_last_trade_date"
        else:
            latest_signal_date = datetime.now().strftime("%Y-%m-%d")
            signal_date_source = "system_clock_fallback"
    generated_batch = f"daily-{latest_signal_date}"

    combined: list[dict[str, Any]] = []
    for row in long_rows:
        combined.append(
            _normalize_source_row(
                row,
                strategy_family="long_term_family",
                strategy_id=str(row.get("strategy_id") or "long_term_research"),
                latest_signal_date=latest_signal_date,
                generated_batch=generated_batch,
                names=names,
            )
        )
    for row in low_rows:
        combined.append(
            _normalize_source_row(
                row,
                strategy_family="low_price_family",
                strategy_id=str(row.get("strategy_id") or "low_price_research"),
                latest_signal_date=latest_signal_date,
                generated_batch=generated_batch,
                names=names,
            )
        )
    for row in limit_rows:
        combined.append(
            _normalize_source_row(
                row,
                strategy_family="limit_up_family",
                strategy_id=str(row.get("pattern_detected") or "limit_up_research"),
                latest_signal_date=latest_signal_date,
                generated_batch=generated_batch,
                names=names,
            )
        )

    strategy_weights, strategy_weight_warnings = _strategy_weights_with_warnings(root)
    candidates = _dedupe_and_rank(
        combined,
        limit=max_candidates,
        required_signal_date=latest_signal_date,
        strategy_weights=strategy_weights,
    )
    results_dir = root / "results" / "daily_research"
    write_csv_rows(results_dir / "v420_daily_stock_candidates_latest.csv", candidates, DAILY_CANDIDATE_COLUMNS, encoding="utf-8-sig")
    ledger_result = append_daily_candidates_to_ledger(root, candidates) if append_ledger else {
        "ledger": "data/tracking/candidate_ledger.csv",
        "existing_count_before": len(read_csv_rows(root / "data/tracking/candidate_ledger.csv")),
        "appended_count": 0,
        "total_count": len(read_csv_rows(root / "data/tracking/candidate_ledger.csv")),
    }
    warnings = list(strategy_weight_warnings)
    if not metrics:
        warnings.append("local_daily_sample_insufficient")
    if signal_date_source == "system_clock_fallback":
        warnings.append(
            "signal_date_from_system_clock_fallback: 无法从策略信号或本地日线推出交易日，"
            "已用系统时钟兜底，该日期不代表真实行情日期，入场价与账本日期仅供降级参考。"
        )
    payload = {
        "task_id": "daily_candidate_generation",
        "status": "completed" if candidates else "completed_with_warnings",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "signal_date": latest_signal_date,
        "generated_batch": generated_batch,
        "scope": "bounded_local_daily_research_scan",
        "universe_source": universe_source,
        "max_scan_symbols": max_scan_symbols,
        "metrics_scanned": len(metrics),
        "metrics_scanned_raw": len(raw_metrics),
        "excluded_candidate_count": len(excluded_metrics),
        "excluded_candidate_samples": [
            {
                "symbol": row.get("symbol") or row.get("code"),
                "name": row.get("name") or names.get(normalize_symbol(row.get("symbol") or row.get("code"))) or "",
                "exclude_reasons": row.get("exclude_reasons") or [],
            }
            for row in excluded_metrics[:20]
        ],
        "candidate_count": len(candidates),
        "new_ledger_rows": ledger_result.get("appended_count", 0),
        "ledger_result": ledger_result,
        "strategy_counts": {
            "long_term": sum(1 for row in candidates if row.get("strategy_type") == "long_term"),
            "da_ban": sum(1 for row in candidates if row.get("strategy_type") == "da_ban"),
        },
        "strategy_source_counts": {
            "long_term_family": len(long_rows),
            "low_price_family": len(low_rows),
            "limit_up_family": len(limit_rows),
        },
        "source_files": {
            "latest_candidates_csv": "results/daily_research/v420_daily_stock_candidates_latest.csv",
            "ledger": "data/tracking/candidate_ledger.csv",
        },
        "message_for_ui": (
            f"已基于本地完整日线生成 {latest_signal_date} 研究候选 {len(candidates)} 只；"
            f"新增写入观察账本 {ledger_result.get('appended_count', 0)} 条。"
        ),
        "warning": "; ".join(warnings),
        "warnings": warnings,
        **BOUNDARY,
    }
    write_json(results_dir / "daily_candidate_generation_latest.json", payload)
    return payload
