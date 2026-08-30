from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.chart_renderer import render_equity_curve, render_monthly_returns  # noqa: E402
from quantdesk.experiment_tracker import log_experiment_non_blocking  # noqa: E402
from quantdesk.intraday_data_policy import assert_no_partial_daily_for_research  # noqa: E402
from quantdesk.point_in_time_universe import (  # noqa: E402
    filter_rows_for_pit_membership,
    load_historical_listings,
    summarize_pit_universe_status,
)

from quantdesk.backtest import (  # noqa: E402
    BacktestConfig,
    PortfolioBacktestConfig,
    export_backtest_result,
    json_safe,
    simulate_backtest_result,
    simulate_portfolio_backtest,
)
from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.corporate_action_repair import repair_corporate_actions  # noqa: E402
from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.enriched.adjust import (  # noqa: E402
    ADJUST_RAW,
    ADJUST_UNKNOWN,
    AdjustConsistencyError,
    adjust_of_frame,
)
from quantdesk.price_basis import (  # noqa: E402
    PRICE_BASIS_RAW,
    PRICE_BASIS_UNKNOWN,
    classify_frame_price_basis,
    observed_price_basis_labels,
    validate_price_frame_set,
)
from quantdesk.scope_policy import resolve_scope, scope_fields  # noqa: E402
from quantdesk.shared import normalize_symbol, now_text, truthy, write_json  # noqa: E402
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

RESULT_JSON = PROJECT_ROOT / "results" / "backtest" / "backtest_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "backtest_report_latest.md"
ARTIFACT_DIR = PROJECT_ROOT / "results" / "backtest" / "artifacts"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DISCLAIMER = (
    "Local historical-data research and paper simulation only. QuantDesk does not "
    "connect to brokers, place real orders, or provide investment advice. Backtest "
    "results do not represent future performance."
)

_PRICE_POLICY_LOAD_REASONS = {
    "mixed_price_basis",
    "unknown_price_basis",
    "consumer_price_basis_not_allowed",
    "unknown_price_consumer",
}

DAILY_ALIASES = {
    "date": "date",
    "trade_date": "date",
    "datetime": "date",
    "symbol": "symbol",
    "code": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "preclose": "prev_close",
    "prev_close": "prev_close",
    "volume": "volume",
    "amount": "amount",
    "pct_chg": "pct_chg",
    "change_pct": "pct_chg",
    "turnover": "turnover",
}


def _json_arg(value: str | bool | None) -> bool:
    return truthy(value)


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(target).replace("\\", "/")


def _parse_symbols(value: Any) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else list(value or [])
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _synthetic_daily_histories(symbols: list[str], *, end_date: str) -> dict[str, pd.DataFrame]:
    end = pd.to_datetime(end_date or "2026-07-10", errors="coerce")
    if pd.isna(end):
        end = pd.Timestamp("2026-07-10")
    dates = pd.bdate_range(end=end, periods=160)
    histories: dict[str, pd.DataFrame] = {}
    for offset, symbol in enumerate(symbols):
        base = 8.0 + offset * 2.0
        closes = [round(base + idx * 0.015 + ((idx + offset) % 5) * 0.02, 4) for idx in range(len(dates))]
        frame = pd.DataFrame(
            {
                "date": dates,
                "symbol": symbol,
                "open": [round(value * 0.998, 4) for value in closes],
                "high": [round(value * 1.012, 4) for value in closes],
                "low": [round(value * 0.988, 4) for value in closes],
                "close": closes,
                "prev_close": [closes[idx - 1] if idx else closes[0] for idx in range(len(closes))],
                "volume": [1_000_000 + idx * 1000 for idx in range(len(closes))],
                "amount": [round(closes[idx] * (1_000_000 + idx * 1000), 2) for idx in range(len(closes))],
                "pct_chg": [0.0] + [round((closes[idx] / closes[idx - 1] - 1.0) * 100, 4) for idx in range(1, len(closes))],
                "turnover": 1.0,
                "adjust": "raw",
                "source": "synthetic_test_data",
            }
        )
        histories[symbol] = frame
    return histories


def _read_daily(symbol: str, daily_dir: Path, *, start_date: str = "", end_date: str = "") -> tuple[pd.DataFrame, str]:
    normalized = normalize_symbol(symbol)
    candidates = [daily_dir / f"{normalized}.csv", daily_dir / f"{normalized}_qfq.csv"]
    path = next((item for item in candidates if item.exists()), None)
    if path is None:
        return pd.DataFrame(), "daily_file_missing"
    frame = read_csv_frame(path)
    csv_read_warnings = [str(item) for item in frame.attrs.get("warnings", []) if item]
    read_warning = ";".join(csv_read_warnings)
    if frame.empty:
        return pd.DataFrame(), read_warning or "daily_file_empty"
    frame = frame.rename(columns={col: DAILY_ALIASES.get(str(col), str(col)) for col in frame.columns}).copy()
    frame = frame.loc[:, ~frame.columns.duplicated()]
    _observed_basis, basis_reason = classify_frame_price_basis(frame)
    if basis_reason:
        return pd.DataFrame(), ";".join(item for item in (basis_reason, read_warning) if item)
    if "date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame(), read_warning or "daily_required_columns_missing"
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "prev_close", "volume", "amount", "pct_chg", "turnover"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("open", "high", "low"):
        if column not in frame.columns:
            frame[column] = frame["close"]
    if "prev_close" not in frame.columns:
        frame["prev_close"] = frame["close"].shift(1)
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    frame = frame[frame["close"] > 0]
    # data/daily is adjust=raw: the price columns carry split/dividend cliffs that
    # are not real moves. Repair AFTER sorting (the back-adjustment anchors on the
    # latest bar) and BEFORE the date window is applied, so the factor covering a
    # corporate action outside the window is still accounted for.
    repaired, ca_info = repair_corporate_actions(frame)
    if ca_info.get("unverifiable"):
        return pd.DataFrame(), ";".join(
            item for item in ("corporate_action_unverifiable", read_warning) if item
        )
    frame = repaired
    if ca_info.get("ca_bars"):
        # prev_close was derived from the unrepaired series above; a cliff would
        # otherwise leave a fake gap in it.
        frame["prev_close"] = frame["close"].shift(1)
        read_warning = ";".join(
            item for item in (read_warning, f"corporate_action_repaired:{ca_info['ca_bars']}") if item
        )
    if start_date:
        frame = frame[frame["date"] >= pd.to_datetime(start_date, errors="coerce")]
    if end_date:
        frame = frame[frame["date"] <= pd.to_datetime(end_date, errors="coerce")]
    return frame.reset_index(drop=True), read_warning


def _execution_price_evidence(basis: str) -> dict[str, Any]:
    uses_raw = str(basis) == PRICE_BASIS_RAW
    return {
        "execution_price_basis": str(basis or PRICE_BASIS_UNKNOWN),
        "execution_price_role": (
            "raw_market_bar_research_proxy" if uses_raw else "same_basis_research_proxy"
        ),
        "uses_raw_execution_price": uses_raw,
        "real_execution_price": False,
    }


def _price_policy_validation_with_load_blocks(
    histories: dict[str, pd.DataFrame],
    load_blocks: dict[str, str],
) -> dict[str, Any]:
    audit = validate_price_frame_set(histories, consumer="backtest")
    if not load_blocks:
        return audit

    per_symbol = dict(audit.get("per_symbol") or {})
    reason_codes = [
        str(item)
        for item in (audit.get("reason_codes") or [])
        if str(item) != "no_price_frames"
    ]
    reason_counts = {
        str(key): int(value)
        for key, value in (audit.get("reason_counts") or {}).items()
        if str(key) != "no_price_frames"
    }
    blocked_symbols = [str(item) for item in (audit.get("blocked_symbols") or [])]
    for symbol, reason in load_blocks.items():
        per_symbol[symbol] = {
            "price_basis": PRICE_BASIS_UNKNOWN,
            "reason_code": reason,
            "eligible": False,
        }
        if symbol not in blocked_symbols:
            blocked_symbols.append(symbol)
        if reason not in reason_codes:
            reason_codes.append(reason)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        **audit,
        "status": "blocked",
        "eligible": False,
        "accepted_price_basis": PRICE_BASIS_UNKNOWN,
        "reason_codes": reason_codes,
        "reason_counts": dict(sorted(reason_counts.items())),
        "blocked_symbols": blocked_symbols,
        "per_symbol": per_symbol,
    }


def _resolve_price_adjustment(histories: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Report the actual price-adjustment convention of the loaded daily frames.

    Reads the per-symbol ``adjust`` column (via :func:`adjust_of_frame`) instead
    of assuming a fixed convention. Backtest limit-up/down math needs raw bars,
    so an unknown or mixed set is surfaced as a visible warning rather than
    silently mislabelled. This does not alter any PnL / cost / fill logic.
    """

    per_symbol: dict[str, str] = {}
    warnings: list[str] = []
    for symbol, frame in histories.items():
        try:
            per_symbol[symbol] = adjust_of_frame(frame)
        except AdjustConsistencyError as exc:
            per_symbol[symbol] = ADJUST_UNKNOWN
            warnings.append(f"{symbol}:adjust_conflict:{exc}")
    conventions = {value for value in per_symbol.values()}
    known = conventions - {ADJUST_UNKNOWN}
    if len(known) > 1:
        resolved = "mixed"
        warnings.append(f"price_adjustment_mixed:{sorted(known)}")
    elif known:
        resolved = next(iter(known))
    else:
        resolved = ADJUST_UNKNOWN
    if ADJUST_UNKNOWN in conventions:
        warnings.append(
            "price_adjustment_unknown_for_some_symbols:"
            "untagged daily bars; limit-up/down and ex-dividend math is not guaranteed correct"
        )
    if resolved not in (ADJUST_RAW, ADJUST_UNKNOWN, "mixed"):
        # Limit checks assume raw prices; a purely adjusted set can misfire on
        # ex-dividend days. Keep it visible instead of failing the whole run.
        warnings.append(
            f"price_adjustment_not_raw:{resolved}:"
            "limit-up/down checks assume raw bars"
        )
    return {
        "price_adjustment": resolved,
        "price_adjustment_by_symbol": per_symbol,
        "price_adjustment_known": resolved not in (ADJUST_UNKNOWN, "mixed"),
        "price_adjustment_warnings": warnings,
    }


def _metrics_dict(result: Any) -> dict[str, Any]:
    return json_safe(asdict(result.metrics))


def _equity_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"row_count": 0, "start_date": "", "end_date": "", "final_equity": None}
    dates = pd.to_datetime(frame.get("date"), errors="coerce").dropna()
    equity = pd.to_numeric(frame.get("equity"), errors="coerce").dropna()
    return {
        "row_count": int(len(frame)),
        "start_date": dates.min().strftime("%Y-%m-%d") if not dates.empty else "",
        "end_date": dates.max().strftime("%Y-%m-%d") if not dates.empty else "",
        "final_equity": float(equity.iloc[-1]) if not equity.empty else None,
    }


def _write_report(payload: dict[str, Any]) -> None:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    data_gate = payload.get("data_gate") if isinstance(payload.get("data_gate"), dict) else {}
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    lines = [
        "# QuantDesk Backtest Report",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- mode: `{payload.get('mode')}`",
        f"- strategy: `{payload.get('strategy')}`",
        f"- network: `{str(payload.get('network')).lower()}`",
        f"- date_range: `{payload.get('start_date') or '-'} -> {payload.get('end_date') or '-'}`",
        f"- loaded_symbols: `{', '.join(data_gate.get('loaded_symbols') or []) or '-'}`",
        f"- missing_symbols: `{', '.join(data_gate.get('missing_symbols') or []) or '-'}`",
        "",
        "## Metrics",
        "",
        "| metric | value |",
        "|:--|:--|",
    ]
    for key in ("total_return", "annual_return", "max_drawdown", "sharpe", "win_rate", "number_of_trades", "final_equity"):
        lines.append(f"| {key} | {metrics.get(key)} |")
    lines.extend([
        "",
        "## Trades",
        "",
        f"- trade_count: `{payload.get('trade_count', 0)}`",
        f"- trades_preview: `{payload.get('trades', [])[:3]}`",
        "",
        "## Data Gate And Warnings",
        "",
    ])
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- none")
    lines.extend(["", "## Charts", ""])
    charts = payload.get("charts") if isinstance(payload.get("charts"), dict) else {}
    if charts:
        for name, path in charts.items():
            lines.append(f"![{name}]({path})")
    else:
        lines.append("- charts unavailable")
    lines.extend([
        "",
        "## Assumptions",
        "",
        "This local backtest reads existing daily CSV files only. It does not fetch data, connect to brokers, place orders, or provide investment advice.",
    ])
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _attach_experiment_log(payload: dict[str, Any]) -> dict[str, Any]:
    tracking = log_experiment_non_blocking(
        "backtest",
        {
            "mode": payload.get("mode"),
            "strategy": payload.get("strategy"),
            "symbols": payload.get("symbols"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
        },
        payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
        output_paths=payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {},
        tags=["auto_logged"],
    )
    if tracking.get("experiment_id"):
        payload["experiment_id"] = tracking.get("experiment_id")
    if tracking.get("warning") and not tracking.get("ok"):
        payload.setdefault("warnings", []).append(tracking["warning"])
    return payload


def _warning_payload(
    args: argparse.Namespace,
    warning_kind: str,
    warnings: list[str],
    *,
    selected_symbols: list[str],
    missing_symbols: list[str],
    loaded_symbols: list[str] | None = None,
    price_policy_validation: dict[str, Any] | None = None,
    pit_filter_audit: dict[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    price_policy_validation = dict(price_policy_validation or {})
    observed_basis = str(
        price_policy_validation.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN
    )
    price_fields = (
        observed_price_basis_labels(
            observed_basis,
            reason_codes=[str(item) for item in price_policy_validation.get("reason_codes") or []],
        )
        | _execution_price_evidence(observed_basis)
        if price_policy_validation
        else {}
    )
    payload = {
        "ok": not blocked,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest",
        "status": "blocked" if blocked else "warning",
        "warning_kind": warning_kind,
        "mode": args.mode,
        "strategy": args.strategy,
        "network": False,
        "llm_enabled": False,
        "full_scan": False,
        "symbols": selected_symbols,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "metrics": {},
        "trades": [],
        "trade_count": 0,
        "equity_curve_summary": {"row_count": 0, "start_date": "", "end_date": "", "final_equity": None},
        "assumptions": {
            "network": False,
            "data_source": "local data/daily csv only",
            "pit_universe_filter": bool(truthy(getattr(args, "pit_universe", "false"))),
            "pit_universe_used": False,
        },
        "pit_filter_audit": dict(pit_filter_audit or {
            "pit_universe_used": False,
            "requested_count": len(selected_symbols),
            "included_count": 0,
            "excluded_count": len(selected_symbols),
        }),
        "charts": {},
        "data_gate": {
            "status": "blocked" if blocked else "warning",
            "requested_symbols": selected_symbols,
            "loaded_symbols": list(loaded_symbols or []),
            "blocked_symbols": list(price_policy_validation.get("blocked_symbols") or []),
            "missing_symbols": missing_symbols,
            "daily_dir": _rel(args.daily_dir),
        },
        **({"price_policy_validation": price_policy_validation} if price_policy_validation else {}),
        **price_fields,
        "warnings": warnings,
        "suggested_next_action": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool or manually import data/daily/{symbol}.csv, then rerun backtest",
        "output_paths": {"json": _rel(RESULT_JSON), "report": _rel(REPORT_MD)},
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    payload = _attach_experiment_log(payload)
    write_json(RESULT_JSON, payload)
    _write_report(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local QuantDesk backtest from existing data/daily CSV files.")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--scope", default="current_analysis_pool", choices=["current_analysis_pool", "low_price_included", "specified_symbols", "local_daily_cache"])
    parser.add_argument("--strategy", default="ma_breakout")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--mode", choices=["single", "portfolio"], default="single")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    parser.add_argument("--max-symbols", type=int, default=80)
    parser.add_argument("--daily-dir", default=str(DAILY_DIR.relative_to(PROJECT_ROOT)))
    parser.add_argument("--pit-universe", nargs="?", const="true", default="false", help="Explicitly enable point-in-time universe filtering. Defaults to false.")
    parser.add_argument("--official-limits", nargs="?", const="true", default="true", help="Use official limit-price/suspension metadata when data/metadata tables exist. Defaults to true; rows without coverage fall back to the heuristic with per-trade annotation.")
    parser.add_argument("--historical-listings-csv", default="data/universe/historical_listings.csv")
    parser.add_argument("--execution-realism", nargs="?", const="true", default="false")
    parser.add_argument("--max-participation-rate", type=float, default=0.01)
    parser.add_argument("--limit-up-fill-model", nargs="?", const="true", default="false")
    parser.add_argument("--dynamic-slippage", nargs="?", const="true", default="false")
    parser.add_argument("--synthetic-test-data", nargs="?", const="true", default="false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network_requested = bool(args.enable_network or truthy(args.network)) and not bool(args.no_network)
    target_info = resolve_target_complete_trade_date(args.end_date or None)
    if not args.end_date:
        args.end_date = str(target_info.get("target_complete_trade_date") or target_info.get("target_date") or "")
    warnings = [str(item) for item in (target_info.get("target_date_warnings") or []) if item]
    if network_requested:
        warnings.append("network flag was provided, but backtest is local-only and does not fetch data.")
    daily_dir = _project_path(args.daily_dir)
    assert_no_partial_daily_for_research(daily_dir, caller="backtest")
    args.daily_dir = str(daily_dir)
    synthetic_test_data = truthy(args.synthetic_test_data)
    if synthetic_test_data:
        selected_symbols = _parse_symbols(args.symbols) or ["000001", "600000"]
        selected_symbols = selected_symbols[: max(1, min(int(args.max_symbols or 2), 2))]
        scope = {
            "data_scope": "synthetic_test_data",
            "scope_label": "Synthetic test data",
            "scope_source": "explicit_synthetic_test_data_flag",
            "requested_count": len(selected_symbols),
            "analysis_pool_count": len(selected_symbols),
            "selected_count": len(selected_symbols),
            "max_symbols": len(selected_symbols),
            "truncated": False,
            "truncation_reason": "",
            "symbols": selected_symbols,
            "symbols_preview": selected_symbols,
            "symbols_omitted_count": 0,
        }
        warnings.append("synthetic_test_data: test/demo only; not formal market evidence")
    else:
        scope = resolve_scope(symbols=args.symbols, scope=args.scope, max_symbols=args.max_symbols, project_root=PROJECT_ROOT)
        selected_symbols = _parse_symbols(args.symbols) or list(scope.get("symbols") or [])
    pit_universe_requested = truthy(args.pit_universe)
    pit_listings: pd.DataFrame | None = None
    pit_status = {"pit_universe": False, "available": False, "warnings": []}
    pit_filter_audit = {
        "pit_universe_used": False,
        "requested_count": len(selected_symbols),
        "included_count": len(selected_symbols),
        "excluded_count": 0,
        "warnings": [],
    }
    if pit_universe_requested and synthetic_test_data:
        warnings.append("synthetic_test_data_pit_not_evaluated")
    elif pit_universe_requested:
        listings, pit_warnings = load_historical_listings(args.historical_listings_csv)
        pit_status = summarize_pit_universe_status(listings, args.historical_listings_csv)
        warnings.extend(pit_warnings)
        if pit_status.get("pit_universe"):
            # Keep the requested historical pool intact. Membership is enforced
            # on every in-memory price row below, before indicators or signals.
            pit_listings = listings
        else:
            warnings.append("pit_universe_requested_but_unavailable: historical listings missing or insufficient; universe unchanged")
    if not selected_symbols:
        payload = _warning_payload(
            args,
            "no_symbols_selected",
            warnings + ["No symbols selected. Bootstrap a universe or pass --symbols."],
            selected_symbols=[],
            missing_symbols=[],
        )
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="backtest")
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), default=str) if _json_arg(args.json) else format_human_summary(summary))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str) if _json_arg(args.json) else payload["suggested_next_action"])
        return 0

    histories: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    load_warnings: list[str] = []
    price_load_blocks: dict[str, str] = {}
    if synthetic_test_data:
        histories = _synthetic_daily_histories(selected_symbols, end_date=args.end_date)
    else:
        for symbol in selected_symbols:
            frame, reason = _read_daily(symbol, daily_dir, start_date=args.start_date, end_date=args.end_date)
            if frame.empty:
                missing.append(symbol)
                load_warnings.append(f"{symbol}:{reason or 'no_usable_rows'}")
                reason_parts = {item.strip() for item in str(reason or "").split(";") if item.strip()}
                price_reason = next(
                    (item for item in _PRICE_POLICY_LOAD_REASONS if item in reason_parts),
                    "",
                )
                if price_reason:
                    price_load_blocks[symbol] = price_reason
            else:
                histories[symbol] = frame
                if reason:
                    load_warnings.append(f"{symbol}:{reason}")

    if pit_universe_requested and pit_listings is not None and pit_status.get("pit_universe"):
        pit_parts: list[pd.DataFrame] = []
        original_columns: dict[str, list[str]] = {}
        for symbol, frame in histories.items():
            original_columns[symbol] = list(frame.columns)
            part = frame.copy()
            part["__pit_symbol"] = symbol
            pit_parts.append(part)
        pit_rows = (
            pd.concat(pit_parts, ignore_index=True)
            if pit_parts
            else pd.DataFrame(columns=["date", "__pit_symbol"])
        )
        included_rows, pit_filter_audit = filter_rows_for_pit_membership(
            pit_rows,
            pit_listings,
            symbol_column="__pit_symbol",
            date_column="date",
            pit_status=pit_status,
        )
        histories = {
            symbol: included_rows.loc[
                included_rows["__pit_symbol"] == symbol,
                original_columns[symbol],
            ].reset_index(drop=True)
            for symbol in original_columns
            if bool((included_rows["__pit_symbol"] == symbol).any())
        }
        warnings.extend(pit_filter_audit.get("warnings") or [])
        if pit_filter_audit.get("excluded_row_count"):
            warnings.append(
                f"pit_per_date_rows_excluded:{pit_filter_audit['excluded_row_count']}"
            )

    price_policy_validation = _price_policy_validation_with_load_blocks(
        histories,
        price_load_blocks,
    )
    if price_load_blocks or (
        histories and not bool(price_policy_validation.get("eligible"))
    ):
        blocked_reasons = [
            str(item) for item in price_policy_validation.get("reason_codes") or []
        ]
        payload = _warning_payload(
            args,
            "price_policy_blocked",
            warnings
            + [
                "price policy validation blocked the complete backtest before simulation:"
                + ",".join(blocked_reasons)
            ]
            + load_warnings[:20],
            selected_symbols=selected_symbols,
            missing_symbols=missing,
            loaded_symbols=list(histories),
            price_policy_validation=price_policy_validation,
            pit_filter_audit=pit_filter_audit,
            blocked=True,
        )
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="backtest")
            print(
                json.dumps(summary, ensure_ascii=True, separators=(",", ":"), default=str)
                if _json_arg(args.json)
                else format_human_summary(summary)
            )
        elif _json_arg(args.json):
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            print(
                "backtest status=blocked reason=price_policy_blocked "
                f"symbols={len(histories)}/{len(selected_symbols)}"
            )
        return 2

    if not histories:
        payload = _warning_payload(
            args,
            "no_daily_data",
            warnings + ["No usable local daily data found; backtest did not run.", *load_warnings[:20]],
            selected_symbols=selected_symbols,
            missing_symbols=missing,
            pit_filter_audit=pit_filter_audit,
        )
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="backtest")
            print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), default=str) if _json_arg(args.json) else format_human_summary(summary))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str) if _json_arg(args.json) else payload["suggested_next_action"])
        return 0

    config = PortfolioBacktestConfig() if args.mode == "portfolio" else BacktestConfig()
    official_limits_enabled = truthy(getattr(args, "official_limits", "true"))
    if official_limits_enabled:
        from quantdesk.research.official_limit_metadata import (
            attach_official_limit_metadata,
            load_official_limit_metadata,
        )

        official_metadata = load_official_limit_metadata(list(histories.keys()))
        for official_symbol in list(histories.keys()):
            frame = histories[official_symbol]
            date_column = "date" if "date" in frame.columns else ("日期" if "日期" in frame.columns else "")
            if date_column:
                histories[official_symbol] = attach_official_limit_metadata(
                    frame, official_symbol, official_metadata, date_column=date_column
                )
        config.use_official_limit_metadata = True
        warnings.extend(official_metadata.warnings[:5])
    if args.mode == "portfolio":
        result = simulate_portfolio_backtest(histories, config, strategy=args.strategy)
    else:
        result = simulate_backtest_result(histories, config, strategy=args.strategy)
    if result.equity_curve.empty:
        status = "warning"
        warning_kind = "insufficient_history"
        warnings.append("Local daily data exists, but usable history was insufficient for the existing backtest engine.")
    else:
        status = "passed"
        warning_kind = ""
    adjustment_info = _resolve_price_adjustment(histories)
    if adjustment_info["price_adjustment_warnings"]:
        warnings.extend(adjustment_info["price_adjustment_warnings"])
    artifact_paths = export_backtest_result(result, "portfolio" if args.mode == "portfolio" else "local", ARTIFACT_DIR)
    assumptions = json_safe(dict(result.assumptions))
    assumptions.update({
        "network": False,
        "data_source": "local data/daily csv only",
        "pit_universe_filter": bool(pit_universe_requested),
        "pit_universe_used": bool(pit_universe_requested and pit_status.get("pit_universe")),
        "pit_universe_status": pit_status,
        "not_investment_advice": True,
        "no_broker_connection": True,
        "no_real_orders": True,
    })
    charts: dict[str, str] = {}
    chart_warnings: list[str] = []
    try:
        equity_chart = render_equity_curve(result.equity_curve, "reports/backtest_charts/equity_curve_latest.png")
        monthly_chart = render_monthly_returns(result.equity_curve, "reports/backtest_charts/monthly_returns_latest.png")
        if equity_chart.get("path"):
            charts["equity_curve"] = str(equity_chart.get("path"))
        if monthly_chart.get("path"):
            charts["monthly_returns"] = str(monthly_chart.get("path"))
        chart_warnings.extend(str(item) for item in (equity_chart.get("warnings") or []))
        chart_warnings.extend(str(item) for item in (monthly_chart.get("warnings") or []))
    except Exception as exc:  # noqa: BLE001
        chart_warnings.append(f"chart_render_failed:{exc}")
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "backtest",
        "status": status,
        "warning_kind": warning_kind,
        "synthetic_test_data": bool(synthetic_test_data),
        "data_source": "synthetic_test_data" if synthetic_test_data else "local data/daily csv",
        "mode": args.mode,
        "strategy": args.strategy,
        "network": False,
        "llm_enabled": False,
        "full_scan": False,
        "symbols": selected_symbols,
        **scope_fields(scope),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "metrics": _metrics_dict(result),
        "trades": json_safe(result.trades[:200]),
        "trade_count": len(result.trades),
        "equity_curve_summary": _equity_summary(result.equity_curve),
        "assumptions": assumptions,
        "price_adjustment": adjustment_info["price_adjustment"],
        "price_adjustment_by_symbol": adjustment_info["price_adjustment_by_symbol"],
        "price_adjustment_known": adjustment_info["price_adjustment_known"],
        **observed_price_basis_labels(
            str(price_policy_validation.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN),
            reason_codes=[
                str(item) for item in price_policy_validation.get("reason_codes") or []
            ],
        ),
        **_execution_price_evidence(
            str(price_policy_validation.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN)
        ),
        "price_policy_validation": price_policy_validation,
        "execution_realism_enabled": truthy(args.execution_realism),
        "execution_realism_assumptions": {
            "default_backtest_unchanged": not truthy(args.execution_realism),
            "max_participation_rate": args.max_participation_rate,
            "limit_up_fill_model": truthy(args.limit_up_fill_model),
            "dynamic_slippage": truthy(args.dynamic_slippage),
        } if truthy(args.execution_realism) else {"default_backtest_unchanged": True, "enabled": False},
        "pit_filter_audit": pit_filter_audit,
        "data_gate": {
            "status": "passed" if histories else "warning",
            "requested_symbols": selected_symbols,
            "loaded_symbols": list(histories),
            "blocked_symbols": [],
            "missing_symbols": missing,
            "daily_dir": _rel(daily_dir),
        },
        "artifact_paths": {key: _rel(path) for key, path in artifact_paths.items()},
        "charts": charts,
        "warnings": warnings + load_warnings[:20] + chart_warnings,
        "suggested_next_action": "python scripts/qd.py walk-forward --network false" if status == "passed" else "Fill local data/daily CSV files, then rerun backtest",
        "output_paths": {"json": _rel(RESULT_JSON), "report": _rel(REPORT_MD)},
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    payload = _attach_experiment_log(payload)
    write_json(RESULT_JSON, json_safe(payload))
    _write_report(payload)
    if truthy(args.summary):
        summary = build_cli_summary(payload, task_type="backtest")
        print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), default=str) if _json_arg(args.json) else format_human_summary(summary))
    elif _json_arg(args.json):
        print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"backtest status={status} symbols={len(histories)}/{len(selected_symbols)} json={_rel(RESULT_JSON)}")
        for warning in payload.get("warnings") or []:
            print(f"warning: {warning}")
        print(DISCLAIMER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
