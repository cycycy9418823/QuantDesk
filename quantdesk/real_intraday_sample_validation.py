"""Real provider sample validation for scoped 1-minute intraday data.

This is a diagnostic harness for 1-2 symbols. It tries real providers, writes
only provider-returned 1-minute data, and then replays a sample conditional plan.
It never creates fake minute bars and does not update the main auto portfolio
unless explicitly requested.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.intraday.intraday_update_engine import INTRADAY_1M_DIR, update_intraday_1m_for_symbols
from quantdesk.paper.auto_paper_portfolio import AUTO_PAPER_PORTFOLIO_STATE_JSON
from quantdesk.paper.position_management_engine import (
    apply_conditional_execution_to_state,
    simulate_conditional_plan_file,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DAILY_DIR = PROJECT_ROOT / "data" / "daily"
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
CONDITIONAL_TRADE_PLANS_LATEST_JSON = RESULTS_DIR / "conditional_trade_plans_latest.json"
REAL_SAMPLE_VALIDATION_LATEST_JSON = RESULTS_DIR / "real_intraday_sample_validation_latest.json"
REAL_SAMPLE_EXECUTION_JSON = RESULTS_DIR / "real_intraday_sample_conditional_execution.json"
REAL_SAMPLE_UPDATE_JSON = RESULTS_DIR / "real_intraday_sample_update_latest.json"

DISCLAIMER = "本次是真实 provider 数据验证，不接券商、不真实下单、不构成投资建议。"
NO_FAKE_NOTE = "如果 provider 未返回分钟线，系统不会用日线价格假装成交。"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _as_symbols(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [normalize_symbol(item) for item in value.split(",") if normalize_symbol(item)]
    return [normalize_symbol(item) for item in value if normalize_symbol(item)]


def _latest_daily_date(symbols: list[str], daily_dir: Path = DATA_DAILY_DIR) -> str:
    dates: list[str] = []
    for symbol in symbols:
        path = daily_dir / f"{normalize_symbol(symbol)}.csv"
        if not path.exists():
            continue
        df = read_csv_frame(path, encoding="utf-8-sig")
        if any(item.startswith("csv_read_failed:") for item in list(df.attrs.get("warnings") or [])):
            continue
        if "date" in df.columns and not df.empty:
            values = [str(item)[:10] for item in df["date"].dropna().tolist() if str(item)[:10]]
            if values:
                dates.append(max(values))
    return max(dates) if dates else datetime.now().strftime("%Y-%m-%d")


def _frame_for_symbol(symbol: str, target_date: str, intraday_dir: Path) -> pd.DataFrame:
    payload = read_intraday_csv(intraday_dir / f"{normalize_symbol(symbol)}.csv", default_symbol=symbol, target_date=target_date)
    frame = payload.get("frame")
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def _sample_plan_item(symbol: str, target_date: str, frame: pd.DataFrame) -> dict[str, Any]:
    if isinstance(frame, pd.DataFrame) and not frame.empty:
        price = safe_float(frame["close"].iloc[0], 8.45) or 8.45
    else:
        price = 8.45
    return {
        "symbol": normalize_symbol(symbol),
        "name": normalize_symbol(symbol),
        "strategy_type": "long",
        "strategy_label": "长线",
        "position_state": "new_candidate",
        "plan_date": target_date,
        "trade_date": target_date,
        "source_data_cutoff": target_date,
        "current_quantity": 0,
        "sellable_quantity": 0,
        "frozen_quantity": 0,
        "reference_close": round(float(price), 4),
        "no_chase_above": round(float(price) * 10, 4),
        "rules": {
            "risk_guards": [],
            "entry_rules": [
                {
                    "action": "buy",
                    "trigger_type": "provider_sample_buy",
                    "price_low": round(float(price), 4),
                    "price_high": round(float(price), 4),
                    "quantity": 100,
                    "reason": "真实 provider 样本验证：分钟 close 命中样本计划价格时模拟买入。",
                }
            ],
            "add_rules": [],
            "exit_rules": [],
        },
        "disclaimer": DISCLAIMER,
    }


def _matching_latest_plan(symbols: list[str], target_date: str) -> dict[str, Any] | None:
    payload = read_json(CONDITIONAL_TRADE_PLANS_LATEST_JSON, {})
    if not isinstance(payload, dict) or not payload:
        return None
    items = []
    wanted = set(symbols)
    for row in payload.get("symbols") or []:
        if isinstance(row, dict) and normalize_symbol(row.get("symbol")) in wanted:
            if not target_date or str(row.get("trade_date") or payload.get("trade_date") or "")[:10] == target_date[:10]:
                items.append(row)
    if not items:
        return None
    return {
        **payload,
        "plan_id": f"real_sample_from_latest_{target_date.replace('-', '')}",
        "trade_date": target_date,
        "symbols": items,
    }


def _build_sample_plan(symbols: list[str], target_date: str, intraday_dir: Path, *, network: bool = False) -> dict[str, Any]:
    items = [_sample_plan_item(symbol, target_date, _frame_for_symbol(symbol, target_date, intraday_dir)) for symbol in symbols]
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "plan_id": f"real_intraday_sample_{target_date.replace('-', '')}",
        "plan_date": target_date,
        "trade_date": target_date,
        "source_data_cutoff": target_date,
        "network": bool(network),
        "is_real_order": False,
        "symbols": items,
        "disclaimer": DISCLAIMER,
    }


def _write_report(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# 真实分钟线 provider 样本验证报告",
        "",
        "本次是真实 provider 数据验证，不是合成自检。",
        "",
        NO_FAKE_NOTE,
        "",
        DISCLAIMER,
        "",
        "## 摘要",
        "",
        f"- 目标日期：{payload.get('target_date') or '-'}",
        f"- 样本股票：{', '.join(payload.get('symbols') or [])}",
        f"- 写入成功：{payload.get('summary', {}).get('updated_or_existing_count', 0)}",
        f"- 失败股票：{payload.get('summary', {}).get('failed_symbol_count', 0)}",
        f"- 可靠触发：{payload.get('conditional_execution_summary', {}).get('reliable_event_count', 0)}",
        f"- 未成交/未触发数量：{payload.get('conditional_execution_summary', {}).get('no_fill_count', 0)}",
        f"- 缺分钟线次数：{payload.get('conditional_execution_summary', {}).get('missing_intraday_count', 0)}",
        "",
        "## Provider 结果",
        "",
    ]
    for row in payload.get("provider_results") or []:
        lines.append(
            f"- {row.get('provider')}："
            f"状态：{row.get('status')}；"
            f"更新：{row.get('updated')}；"
            f"失败：{row.get('failed')}；"
            f"原因：{row.get('failed_reason') or '-'}"
        )
    lines.extend(["", "## 风险提示", "", "- 本工具不接券商、不真实下单、不构成投资建议。", "- 如果 provider 未返回分钟线，系统不会用日线价格假装成交。", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_real_intraday_sample_validation(
    *,
    symbols: str | list[str] | tuple[str, ...] = "600744,002115",
    target_date: str = "",
    provider_order: list[str] | None = None,
    update_portfolio: bool = False,
    intraday_dir: Path | None = None,
    output_path: Path | None = None,
    execution_output_path: Path | None = None,
    update_output_path: Path | None = None,
    network: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    symbol_list = _as_symbols(symbols) or ["600744", "002115"]
    target_date = (target_date or _latest_daily_date(symbol_list))[:10]
    intraday_dir = Path(intraday_dir or INTRADAY_1M_DIR)
    output_path = Path(output_path or REAL_SAMPLE_VALIDATION_LATEST_JSON)
    execution_output_path = Path(execution_output_path or REAL_SAMPLE_EXECUTION_JSON)
    update_output_path = Path(update_output_path or REAL_SAMPLE_UPDATE_JSON)
    order = provider_order or DEFAULT_INTRADAY_PROVIDER_ORDER

    update_payload = update_intraday_1m_for_symbols(
        symbols=symbol_list,
        target_date=target_date,
        provider_order=order,
        source="symbols",
        overwrite=False,
        append=True,
        dry_run=False,
        network=bool(network),
        execute_after_update=False,
        update_portfolio_after_execution=False,
        intraday_dir=intraday_dir,
        output_path=update_output_path,
    )
    available_symbols = list(dict.fromkeys((update_payload.get("updated_symbol_list") or []) + (update_payload.get("unchanged_symbol_list") or [])))
    failed_symbols = list(update_payload.get("failed_symbol_list") or [])

    plan = _matching_latest_plan(available_symbols, target_date) if available_symbols else None
    plan_source = "latest_conditional_plan" if plan else "sample_conditional_plan"
    plan = plan or _build_sample_plan(symbol_list, target_date, intraday_dir, network=network)
    execution = simulate_conditional_plan_file(
        plan,
        intraday_dir=intraday_dir.parent,
        output_path=execution_output_path,
        write=write,
    )
    portfolio_update = {"ok": False, "skipped": True, "reason": "update_portfolio=false"}
    if update_portfolio:
        state = read_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, {})
        portfolio_update = apply_conditional_execution_to_state(state if isinstance(state, dict) else {}, execution)
        if portfolio_update.get("state") and write:
            write_json(AUTO_PAPER_PORTFOLIO_STATE_JSON, portfolio_update["state"])

    report_path = REPORTS_DIR / f"real_intraday_sample_validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "real_intraday_sample_validation",
        "generated_at": now_text(),
        "symbols": symbol_list,
        "target_date": target_date,
        "network": bool(network),
        "full_scan": False,
        "is_real_order": False,
        "update_portfolio": bool(update_portfolio),
        "plan_source": plan_source,
        "summary": {
            "symbol_count": len(symbol_list),
            "updated_or_existing_count": len(available_symbols),
            "failed_symbol_count": len(failed_symbols),
            "event_count": execution.get("summary", {}).get("event_count", 0),
            "no_fill_count": execution.get("summary", {}).get("no_fill_count", 0),
            "missing_intraday_count": execution.get("summary", {}).get("missing_intraday_count", 0),
        },
        "updated_symbol_list": available_symbols,
        "failed_symbol_list": failed_symbols,
        "failed_symbol_reasons": update_payload.get("failed_symbol_reasons") or {},
        "provider_results": update_payload.get("provider_results") or [],
        "conditional_execution_summary": execution.get("summary", {}),
        "conditional_execution": {
            "events": execution.get("events") or [],
            "no_fill": execution.get("no_fill") or [],
            "missing_intraday": execution.get("missing_intraday") or [],
            "blocked": execution.get("blocked") or [],
            "path": _display_path(execution_output_path),
        },
        "portfolio_update": {
            "ok": bool(portfolio_update.get("ok")),
            "skipped": bool(portfolio_update.get("skipped", not update_portfolio)),
            "action_count": len(portfolio_update.get("actions") or []),
        },
        "artifacts": {
            "update_path": _display_path(update_output_path),
            "conditional_execution_path": _display_path(execution_output_path),
            "report_path": _display_path(report_path),
        },
        "message": "本次是真实 provider 数据验证，不是合成自检。",
        "no_fake_note": NO_FAKE_NOTE,
        "disclaimer": DISCLAIMER,
    }
    if write:
        _write_report(payload, report_path)
        write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def latest_real_intraday_sample_validation(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or REAL_SAMPLE_VALIDATION_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未运行真实分钟线 provider 样本验证。",
            "disclaimer": DISCLAIMER,
        }
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload


__all__ = [
    "REAL_SAMPLE_VALIDATION_LATEST_JSON",
    "latest_real_intraday_sample_validation",
    "run_real_intraday_sample_validation",
]
