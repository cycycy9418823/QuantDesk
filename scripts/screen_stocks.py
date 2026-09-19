#!/usr/bin/env python3
"""CLI: local feature screener for QuantDesk.

The default qd.py screen path reads data/features/daily_features_latest.csv and
uses a conservative data-availability filter. It never downloads data, connects
to brokers, places orders, or provides investment advice.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.cli_summary import build_cli_summary, format_human_summary  # noqa: E402
from quantdesk.research.factor_research_dashboard import summarize_factor_health  # noqa: E402
from quantdesk.shared import now_text, truthy, write_json  # noqa: E402
from quantdesk.stock_screener import (  # noqa: E402
    SCREENER_DISCLAIMER,
    ScreenCondition,
    parse_condition,
    screen_result_to_markdown,
    screen_stocks,
)
from quantdesk.trading_calendar import resolve_target_complete_trade_date  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

DEFAULT_FEATURES = PROJECT_ROOT / "data" / "features" / "daily_features_latest.csv"
FACTOR_HEALTH_JSON = PROJECT_ROOT / "results" / "factor_health_latest.json"
COMBINED_SCORE_JSON = PROJECT_ROOT / "results" / "combined_score_latest.json"
RESULT_JSON = PROJECT_ROOT / "results" / "screen" / "screen_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "screen_report_latest.md"
DISCLAIMER = SCREENER_DISCLAIMER

STRING_FIELDS = {"symbol", "code", "name", "market", "data_grade", "industry", "sector", "liquidity_status", "data_health_status"}
NUMERIC_FIELDS = {
    "score", "trust_score", "pe", "pe_ttm", "pb", "volume_ratio", "volume_ratio_20", "amount_ratio",
    "amount_ratio_20", "activity_ratio", "activity_ratio_20", "pct_change", "change_pct", "pct_chg",
    "close", "price", "current_price", "ma5", "ma10", "ma20", "ma60", "rsi", "rsi14", "macd",
    "macd_histogram", "atr14", "vol20", "row_count", "amount", "amount_ma20", "turnover",
    "combined_score", "combined_score_rank", "combined_score_coverage", "coverage", "factor_coverage",
}


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(target).replace("\\", "/")


def _normalize_stock_code(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    lower = text.lower()
    if (lower.startswith("sh") or lower.startswith("sz")) and len(text) >= 8:
        text = text[-6:]
    if "." in text and text.replace(".", "", 1).isdigit():
        try:
            as_float = float(text)
            if as_float.is_integer():
                text = str(int(as_float))
        except ValueError:
            pass
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def _parse_csv_value(key: str, value: object) -> object:
    field = (key or "").strip()
    if field in {"symbol", "code"}:
        return _normalize_stock_code(value)
    if field in STRING_FIELDS:
        return "" if value is None else str(value).strip()
    if field in NUMERIC_FIELDS:
        try:
            num = float(value)
            return int(num) if num.is_integer() else num
        except (ValueError, TypeError):
            return value
    return value


def _normalize_candidate_row(row: dict[str, object]) -> dict[str, object]:
    parsed = {k: _parse_csv_value(k, v) for k, v in row.items()}
    if "symbol" not in parsed and "code" in parsed:
        parsed["symbol"] = parsed["code"]
    if "code" not in parsed and "symbol" in parsed:
        parsed["code"] = parsed["symbol"]
    return parsed


def _build_candidate_handoff(matched: list[dict[str, object]], *, generated_at: str) -> dict[str, Any]:
    symbols: list[str] = []
    for row in matched:
        if not isinstance(row, dict):
            continue
        symbol = _normalize_stock_code(row.get("symbol") or row.get("code"))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return {
        "schema_version": 1,
        "source": "screen_latest",
        "target": "backtest/current_analysis_pool",
        "candidate_count": len(symbols),
        "symbols": symbols,
        "generated_at": generated_at,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "message": "Local handoff for screen -> backtest/paper review. Research only; no broker route.",
    }


def _load_factor_health_context(path_value: str | Path) -> dict[str, Any]:
    path = _project_path(path_value)
    context: dict[str, Any] = {
        "available": False,
        "path": _rel(path),
        "status": "missing",
        "generated_at": "",
        "data_source": "",
        "summary": summarize_factor_health(None),
        "used_for_scoring": False,
        "evidence_only": True,
        "warnings": ["missing_factor_health: screen keeps existing conditions and does not fabricate factor evidence"],
    }
    if not path.exists() or not path.is_file():
        return context
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        context["status"] = "unreadable"
        context["warnings"] = [f"factor_health_unreadable:{exc}"]
        return context
    if not isinstance(payload, dict):
        context["status"] = "invalid"
        context["warnings"] = ["factor_health_invalid_payload"]
        return context
    warnings = [str(item) for item in (payload.get("warnings") or []) if item]
    if str(payload.get("data_source") or "") == "synthetic_test_data":
        warnings.append("synthetic_factor_health_evidence_only: do not use as production factor support")
    context.update(
        {
            "available": True,
            "status": str(payload.get("status") or ""),
            "generated_at": str(payload.get("generated_at") or ""),
            "data_source": str(payload.get("data_source") or ""),
            "summary": summarize_factor_health(payload),
            "warnings": list(dict.fromkeys(warnings)),
        }
    )
    return context


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_int(value: object) -> int | None:
    number = _finite_float(value)
    return int(number) if number is not None else None


def _combined_row_value(row: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _latest_combined_score_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    rows = payload.get("top_candidates")
    row_source = "top_candidates"
    if not isinstance(rows, list) or not rows:
        rows = payload.get("score_panel") or payload.get("scores") or payload.get("score_rows")
        row_source = "score_panel"
    if not isinstance(rows, list):
        return [], row_source, ""

    target_date = str(payload.get("target_date") or "").strip()
    if row_source == "score_panel":
        date_values = [
            str(_combined_row_value(row, "date", "trade_date") or "").strip()
            for row in rows
            if isinstance(row, dict)
        ]
        if not target_date:
            target_date = max([item for item in date_values if item], default="")
        if target_date:
            rows = [
                row
                for row in rows
                if isinstance(row, dict)
                and str(_combined_row_value(row, "date", "trade_date") or "").strip() == target_date
            ]

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _normalize_stock_code(_combined_row_value(row, "symbol", "code", "ts_code"))
        score = _finite_float(_combined_row_value(row, "combined_score", "score"))
        if not symbol or score is None:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "date": str(_combined_row_value(row, "date", "trade_date") or target_date or ""),
                "combined_score": score,
                "combined_score_rank": _finite_int(_combined_row_value(row, "combined_score_rank", "rank")),
                "combined_score_coverage": _finite_float(
                    _combined_row_value(row, "combined_score_coverage", "coverage", "factor_coverage")
                ),
            }
        )

    if not normalized:
        return [], row_source, target_date
    if all(row.get("combined_score_rank") is None for row in normalized):
        normalized = sorted(normalized, key=lambda row: float(row["combined_score"]), reverse=True)
        for index, row in enumerate(normalized, 1):
            row["combined_score_rank"] = index
    else:
        normalized = sorted(
            normalized,
            key=lambda row: (
                int(row["combined_score_rank"] or 10**9),
                -float(row["combined_score"]),
                str(row["symbol"]),
            ),
        )
    return normalized, row_source, target_date


def _load_combined_score_context(path_value: str | Path) -> dict[str, Any]:
    path = _project_path(path_value)
    context: dict[str, Any] = {
        "available": False,
        "path": _rel(path),
        "status": "missing",
        "generated_at": "",
        "data_source": "",
        "target_date": "",
        "row_source": "",
        "scored_symbol_count": 0,
        "matched_score_count": 0,
        "used_for_sorting": False,
        "evidence_only": True,
        "top_candidates_preview": [],
        "sorting_rule": "combined_score_rank asc, combined_score desc",
        "source_warning_count": 0,
        "source_warnings_preview": [],
        "warnings": ["missing_combined_score: screen keeps feature conditions and does not fabricate factor rankings"],
        "_scores_by_symbol": {},
    }
    if not path.exists() or not path.is_file():
        return context
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        context["status"] = "unreadable"
        context["warnings"] = [f"combined_score_unreadable:{exc}"]
        return context
    if not isinstance(payload, dict):
        context["status"] = "invalid"
        context["warnings"] = ["combined_score_invalid_payload"]
        return context

    source_warnings = [str(item) for item in (payload.get("warnings") or []) if item]
    warnings: list[str] = []
    task_type = str(payload.get("task_type") or "")
    if task_type and task_type not in {"factor_combination", "combined_score"}:
        warnings.append(f"incompatible_combined_score_task_type:{task_type}")
    rows, row_source, target_date = _latest_combined_score_rows(payload)
    scores_by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row["symbol"])
        current = scores_by_symbol.get(symbol)
        if current is None:
            scores_by_symbol[symbol] = row
            continue
        current_rank = int(current.get("combined_score_rank") or 10**9)
        row_rank = int(row.get("combined_score_rank") or 10**9)
        if row_rank < current_rank or (
            row_rank == current_rank and float(row.get("combined_score") or -math.inf) > float(current.get("combined_score") or -math.inf)
        ):
            scores_by_symbol[symbol] = row

    data_source = str(payload.get("data_source") or "")
    if data_source == "synthetic_test_data":
        warnings.append("synthetic_combined_score_evidence_only: ranking is visible but not used for screen sorting")
    used_for_sorting = bool(scores_by_symbol) and data_source != "synthetic_test_data" and task_type in {"", "factor_combination", "combined_score"}
    context.update(
        {
            "available": True,
            "status": str(payload.get("status") or ""),
            "generated_at": str(payload.get("generated_at") or ""),
            "data_source": data_source,
            "target_date": str(payload.get("target_date") or target_date or ""),
            "row_source": row_source,
            "scored_symbol_count": len(scores_by_symbol),
            "used_for_sorting": used_for_sorting,
            "evidence_only": not used_for_sorting,
            "top_candidates_preview": [
                {
                    "symbol": row.get("symbol"),
                    "combined_score": row.get("combined_score"),
                    "combined_score_rank": row.get("combined_score_rank"),
                    "combined_score_coverage": row.get("combined_score_coverage"),
                }
                for row in rows[:5]
            ],
            "source_warning_count": len(source_warnings),
            "source_warnings_preview": source_warnings[:5],
            "warnings": list(dict.fromkeys(warnings)),
            "_scores_by_symbol": scores_by_symbol if used_for_sorting else {},
        }
    )
    if not scores_by_symbol:
        context["warnings"] = list(dict.fromkeys([*context["warnings"], "missing_combined_score_rows"]))
    return context


def _public_combined_score_context(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if not key.startswith("_")}


def _attach_combined_scores(rows: list[dict[str, object]], context: dict[str, Any]) -> list[dict[str, object]]:
    scores_by_symbol = context.get("_scores_by_symbol")
    if not isinstance(scores_by_symbol, dict) or not scores_by_symbol:
        context["matched_score_count"] = 0
        return rows
    attached: list[dict[str, object]] = []
    matched_count = 0
    for row in rows:
        item = dict(row)
        symbol = _normalize_stock_code(item.get("symbol") or item.get("code"))
        score_row = scores_by_symbol.get(symbol)
        if isinstance(score_row, dict):
            matched_count += 1
            item["combined_score"] = score_row.get("combined_score")
            item["combined_score_rank"] = score_row.get("combined_score_rank")
            item["combined_score_coverage"] = score_row.get("combined_score_coverage")
        attached.append(item)
    context["matched_score_count"] = matched_count
    if matched_count == 0:
        context["warnings"] = list(dict.fromkeys([*(context.get("warnings") or []), "combined_score_no_overlap_with_screen_results"]))
    return attached


def _sort_by_combined_score(rows: list[dict[str, object]], context: dict[str, Any]) -> list[dict[str, object]]:
    if not context.get("used_for_sorting"):
        return rows

    def sort_key(indexed: tuple[int, dict[str, object]]) -> tuple[int, int, float, int]:
        index, row = indexed
        rank = _finite_int(row.get("combined_score_rank"))
        score = _finite_float(row.get("combined_score"))
        has_score = rank is not None or score is not None
        return (
            0 if has_score else 1,
            rank if rank is not None else 10**9,
            -(score if score is not None else -math.inf),
            index,
        )

    return [row for _, row in sorted(enumerate(rows), key=sort_key)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local QuantDesk screen from daily_features_latest.csv.")
    parser.add_argument("--conditions", "-c", nargs="*", default=[], help="Screen conditions, for example 'close > 0'.")
    parser.add_argument("--conditions-file", default="")
    parser.add_argument("--data", "-d", default=str(DEFAULT_FEATURES.relative_to(PROJECT_ROOT)))
    parser.add_argument("--factor-health-json", default=str(FACTOR_HEALTH_JSON.relative_to(PROJECT_ROOT)))
    parser.add_argument("--combined-score-json", default=str(COMBINED_SCORE_JSON.relative_to(PROJECT_ROOT)))
    parser.add_argument("--output", "-o", default=str(REPORT_MD.relative_to(PROJECT_ROOT)))
    parser.add_argument("--json-output", default=str(RESULT_JSON.relative_to(PROJECT_ROOT)))
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false", help="Print compact operator summary while still writing full latest JSON.")
    parser.add_argument("--network", default="false", help="Accepted for qd.py consistency; this command never fetches data.")
    parser.add_argument(
        "--target-date",
        default="",
        help="Target complete trading date. Defaults to the latest completed A-share trading day.",
    )
    parser.add_argument("--limit", type=int, default=50)
    return parser.parse_args(argv)


def load_candidates(path: Path) -> list[dict[str, object]]:
    if not path.exists() or not path.is_file():
        return []
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return [_normalize_candidate_row(dict(row)) for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(_normalize_candidate_row(dict(row)))
    return rows


def _target_complete_trade_date(value: object = "") -> str:
    resolved = resolve_target_complete_trade_date(str(value or "").strip() or None)
    return str(resolved.get("target_complete_trade_date") or resolved.get("target_date") or "")[:10]


def _filter_current_feature_rows(
    candidates: list[dict[str, object]],
    *,
    target_date: str,
    require_dates: bool = False,
) -> tuple[list[dict[str, object]], dict[str, Any]]:
    dated_count = sum(1 for row in candidates if str(row.get("latest_date") or "").strip())
    if not target_date or dated_count == 0:
        if require_dates:
            return [], {
                "freshness_enforced": True,
                "target_date": target_date,
                "fresh_row_count": 0,
                "stale_excluded_count": 0,
                "undated_excluded_count": len(candidates),
                "warnings": ["canonical_feature_dates_missing: current-date screen failed closed"],
            }
        return list(candidates), {
            "freshness_enforced": False,
            "target_date": target_date,
            "fresh_row_count": len(candidates),
            "stale_excluded_count": 0,
            "undated_excluded_count": 0,
            "warnings": ["feature_freshness_not_enforced: latest_date field is missing"],
        }

    fresh: list[dict[str, object]] = []
    stale_count = 0
    undated_count = 0
    for row in candidates:
        latest = str(row.get("latest_date") or "").strip()[:10]
        if not latest:
            undated_count += 1
        elif latest != target_date:
            stale_count += 1
        else:
            fresh.append(row)
    warnings: list[str] = []
    if stale_count:
        warnings.append(f"stale_feature_rows_excluded:{stale_count}; target_date={target_date}")
    if undated_count:
        warnings.append(f"undated_feature_rows_excluded:{undated_count}; target_date={target_date}")
    return fresh, {
        "freshness_enforced": True,
        "target_date": target_date,
        "fresh_row_count": len(fresh),
        "stale_excluded_count": stale_count,
        "undated_excluded_count": undated_count,
        "warnings": warnings,
    }


def _warning_payload(kind: str, message: str, *, args: argparse.Namespace) -> dict[str, Any]:
    generated_at = now_text()
    factor_health = _load_factor_health_context(args.factor_health_json)
    combined_score = _load_combined_score_context(args.combined_score_json)
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "screen",
        "status": "warning",
        "warning_kind": kind,
        "network": False,
        "llm_enabled": False,
        "full_scan": False,
        "data_path": _rel(args.data),
        "total_scanned": 0,
        "total_matched": 0,
        "matched": [],
        "candidate_handoff": _build_candidate_handoff([], generated_at=generated_at),
        "factor_health_context": factor_health,
        "combined_score_context": _public_combined_score_context(combined_score),
        "conditions": [],
        "warnings": list(dict.fromkeys([message, *(combined_score.get("warnings") or [])])),
        "suggested_next_action": "python scripts/qd.py build-features; if local daily data is missing, run data-update --enable-network or manually import data/daily CSV files.",
        "output_paths": {"json": _rel(args.json_output), "report": _rel(args.output)},
        "generated_at": generated_at,
        "disclaimer": DISCLAIMER,
    }
    write_json(_project_path(args.json_output), payload)
    report = _project_path(args.output)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# QuantDesk Screen Report\n\n" + DISCLAIMER + "\n\n" + message + "\n", encoding="utf-8")
    return payload


def _read_conditions(args: argparse.Namespace) -> list[ScreenCondition]:
    raw_conditions: list[str] = list(args.conditions or [])
    if args.conditions_file:
        path = _project_path(args.conditions_file)
        if path.exists():
            raw_conditions.extend(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if not raw_conditions:
        raw_conditions = ["close > 0"]
    conditions: list[ScreenCondition] = []
    for text in raw_conditions:
        cond = parse_condition(text)
        if cond:
            conditions.append(cond)
    return conditions


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_path = _project_path(args.data)
    candidates = load_candidates(data_path)
    input_candidate_count = len(candidates)
    if truthy(args.network):
        network_warning = "network flag ignored: screen only reads local feature files."
    else:
        network_warning = ""
    if not data_path.exists() or not candidates:
        message = "missing_features: data/features/daily_features_latest.csv is missing or empty; screen did not run."
        payload = _warning_payload("missing_features", message, args=args)
        if network_warning:
            payload["warnings"].append(network_warning)
            write_json(_project_path(args.json_output), payload)
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="screen")
            print(json.dumps(summary, ensure_ascii=True, indent=2, default=str) if truthy(args.json) else format_human_summary(summary))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2) if truthy(args.json) else payload["suggested_next_action"])
        return 0
    target_date = _target_complete_trade_date(args.target_date)
    candidates, freshness = _filter_current_feature_rows(
        candidates,
        target_date=target_date,
        require_dates=data_path.resolve() == DEFAULT_FEATURES.resolve(),
    )
    if not candidates:
        message = (
            f"no_fresh_features_for_target: no feature row covers target complete trading date {target_date or '-'}; "
            "stale or undated rows were excluded."
        )
        payload = _warning_payload("no_fresh_features_for_target", message, args=args)
        payload.update(
            {
                "target_date": target_date,
                "freshness_enforced": bool(freshness.get("freshness_enforced")),
                "input_candidate_count": input_candidate_count,
                "fresh_row_count": 0,
                "stale_excluded_count": int(freshness.get("stale_excluded_count") or 0),
                "undated_excluded_count": int(freshness.get("undated_excluded_count") or 0),
                "warnings": list(
                    dict.fromkeys(
                        [
                            *(payload.get("warnings") or []),
                            *(freshness.get("warnings") or []),
                            *([network_warning] if network_warning else []),
                        ]
                    )
                ),
            }
        )
        write_json(_project_path(args.json_output), payload)
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="screen")
            print(json.dumps(summary, ensure_ascii=True, indent=2, default=str) if truthy(args.json) else format_human_summary(summary))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2) if truthy(args.json) else payload["suggested_next_action"])
        return 0
    conditions = _read_conditions(args)
    if not conditions:
        payload = _warning_payload("invalid_conditions", "No valid screen conditions were parsed.", args=args)
        if truthy(args.summary):
            summary = build_cli_summary(payload, task_type="screen")
            print(json.dumps(summary, ensure_ascii=True, indent=2, default=str) if truthy(args.json) else format_human_summary(summary))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2) if truthy(args.json) else payload["suggested_next_action"])
        return 0
    combined_score = _load_combined_score_context(args.combined_score_json)
    requested_limit = max(1, int(args.limit or 50))
    screen_limit = len(candidates) if combined_score.get("used_for_sorting") else requested_limit
    result = screen_stocks(candidates, conditions, limit=max(1, screen_limit))
    result.matched = _sort_by_combined_score(_attach_combined_scores(result.matched, combined_score), combined_score)
    if len(result.matched) > requested_limit:
        result.warnings.append(f"combined_score_sorted_results_truncated_to_limit:{requested_limit}")
        result.matched = result.matched[:requested_limit]
    result.total_matched = len(result.matched)
    result_dict = result.to_dict()
    matched = result_dict.get("matched") or []
    generated_at = now_text()
    factor_health = _load_factor_health_context(args.factor_health_json)
    warnings = list(result_dict.get("warnings") or [])
    warnings.extend(freshness.get("warnings") or [])
    if network_warning:
        warnings.append(network_warning)
    if factor_health.get("available") and factor_health.get("data_source") == "synthetic_test_data":
        warnings.append("synthetic_factor_health_evidence_only: factor health is displayed but not used for screen scoring")
    warnings.extend(combined_score.get("warnings") or [])
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "screen",
        "status": "passed",
        "network": False,
        "llm_enabled": False,
        "full_scan": False,
        "data_path": _rel(data_path),
        "target_date": target_date,
        "freshness_enforced": bool(freshness.get("freshness_enforced")),
        "input_candidate_count": input_candidate_count,
        "fresh_row_count": int(freshness.get("fresh_row_count") or 0),
        "stale_excluded_count": int(freshness.get("stale_excluded_count") or 0),
        "undated_excluded_count": int(freshness.get("undated_excluded_count") or 0),
        "total_scanned": result.total_scanned,
        "total_matched": result.total_matched,
        "matched": matched,
        "candidate_handoff": _build_candidate_handoff(matched, generated_at=generated_at),
        "factor_health_context": factor_health,
        "combined_score_context": _public_combined_score_context(combined_score),
        "conditions": result_dict.get("conditions") or [],
        "warnings": list(dict.fromkeys(warnings)),
        "suggested_next_action": "python scripts/qd.py build-features; if local daily data is missing, run data-update --enable-network or manually import data/daily CSV files.",
        "output_paths": {"json": _rel(args.json_output), "report": _rel(args.output)},
        "generated_at": generated_at,
        "disclaimer": DISCLAIMER,
    }
    write_json(_project_path(args.json_output), payload)
    report = _project_path(args.output)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(screen_result_to_markdown(result), encoding="utf-8")
    if truthy(args.summary):
        summary = build_cli_summary(payload, task_type="screen")
        print(json.dumps(summary, ensure_ascii=True, indent=2, default=str) if truthy(args.json) else format_human_summary(summary))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"screen status=passed scanned={result.total_scanned} matched={result.total_matched} json={_rel(args.json_output)}")
        print(DISCLAIMER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
