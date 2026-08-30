"""Deterministic intraday replay self-validation harness.

This harness uses synthetic 1-minute bars to prove the local chain works:
conditional plan -> minute-close replay -> trigger capture -> reliable fills
update the paper ledger while no_fill/missing_intraday do not. It is not a
strategy, not market data, and not investment advice.
"""

from __future__ import annotations

# Compatibility markers: run_intraday_replay_self_validation uses_synthetic_data.
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.intraday.canonical_write_guard import (
    ensure_directory_disjoint_from_canonical,
    ensure_path_outside_canonical,
)
from quantdesk.paper.position_management_engine import (
    apply_conditional_execution_to_state,
    simulate_conditional_plan_file,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, safe_float, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_RUNTIME_DIR = PROJECT_ROOT / "data" / "test_runtime" / "intraday_self_validation"
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
SELF_VALIDATION_LATEST_JSON = RESULTS_DIR / "intraday_replay_self_validation_latest.json"
SELF_VALIDATION_EXECUTION_JSON = RESULTS_DIR / "conditional_execution_self_validation.json"

DISCLAIMER = "本自检使用本地合成数据，仅验证程序逻辑；不接券商、不真实下单、不构成投资建议。"
SYNTHETIC_NOTE = "本报告使用合成分钟线，仅验证程序链路，不代表真实行情。"
TRADE_DATE = "2026-05-15"
PLAN_DATE = "2026-05-14"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _bar(symbol: str, time_text: str, close: float) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(symbol),
        "datetime": f"{TRADE_DATE} {time_text}",
        "date": TRADE_DATE,
        "time": time_text,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10000,
        "amount": round(close * 10000, 2),
        "source": "self_validation_synthetic",
        "updated_at": now_text(),
    }


def _write_bars(base_dir: Path, symbol: str, prices: list[tuple[str, float]]) -> Path:
    target = base_dir / "1m" / f"{normalize_symbol(symbol)}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([_bar(symbol, t, p) for t, p in prices]).to_csv(target, index=False, encoding="utf-8-sig")
    return target


def _new_plan(symbol: str, name: str, entry_rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(symbol),
        "name": name,
        "strategy_type": "long",
        "strategy_label": "长线",
        "position_state": "new_candidate",
        "plan_date": PLAN_DATE,
        "trade_date": TRADE_DATE,
        "source_data_cutoff": PLAN_DATE,
        "current_quantity": 0,
        "sellable_quantity": 0,
        "frozen_quantity": 0,
        "entry_price": None,
        "reference_close": 8.8,
        "no_chase_above": 99,
        "rules": {"risk_guards": [], "entry_rules": entry_rules, "add_rules": [], "exit_rules": []},
        "disclaimer": DISCLAIMER,
    }


def _holding_plan(
    symbol: str,
    name: str,
    *,
    exit_rules: list[dict[str, Any]] | None = None,
    add_rules: list[dict[str, Any]] | None = None,
    quantity: int = 200,
    sellable: int = 200,
) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(symbol),
        "name": name,
        "strategy_type": "long",
        "strategy_label": "长线",
        "position_state": "holding",
        "plan_date": PLAN_DATE,
        "trade_date": TRADE_DATE,
        "source_data_cutoff": PLAN_DATE,
        "current_quantity": quantity,
        "sellable_quantity": sellable,
        "frozen_quantity": 0,
        "entry_price": 8.8,
        "reference_close": 8.8,
        "no_chase_above": 99,
        "rules": {
            "risk_guards": [],
            "entry_rules": [],
            "add_rules": add_rules or [],
            "exit_rules": exit_rules or [],
        },
        "disclaimer": DISCLAIMER,
    }


def build_self_validation_plan() -> dict[str, Any]:
    pullback = {"action": "buy", "trigger_type": "pullback_buy", "price_low": 8.40, "price_high": 8.50, "quantity": 100}
    breakout = {"action": "buy", "trigger_type": "breakout_buy", "price_low": 9.50, "price_high": 9.80, "quantity": 100}
    take_profit = {"action": "reduce_half", "trigger_type": "take_profit_half", "price_low": 10.20, "price_high": 10.50, "quantity_pct": 50}
    stop_loss = {"action": "sell_all", "trigger_type": "stop_loss", "price_low": 0, "price_high": 7.90, "quantity_pct": 100}
    add = {"action": "add", "trigger_type": "pullback_add", "price_low": 8.40, "price_high": 8.50, "quantity": 100}
    return {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "plan_id": "self_validation_plan_20260515",
        "plan_date": PLAN_DATE,
        "trade_date": TRADE_DATE,
        "source_data_cutoff": PLAN_DATE,
        "symbols": [
            _new_plan("600744", "华银电力", [pullback]),
            _new_plan("002115", "三维通信", [breakout]),
            _holding_plan("300323", "华灿光电", exit_rules=[take_profit]),
            _holding_plan("600958", "东方证券", exit_rules=[stop_loss], quantity=100, sellable=100),
            _new_plan("601975", "招商南油", [pullback]),
            _new_plan("002266", "浙富控股", [pullback]),
            _holding_plan("600497", "驰宏锌锗", exit_rules=[stop_loss], add_rules=[add], quantity=100, sellable=100),
        ],
        "disclaimer": DISCLAIMER,
    }


def write_synthetic_intraday(base_dir: Path = TEST_RUNTIME_DIR) -> dict[str, Any]:
    base_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "600744": _write_bars(base_dir, "600744", [("09:31:00", 8.80), ("10:23:00", 8.45), ("10:24:00", 8.55)]),
        "002115": _write_bars(base_dir, "002115", [("09:31:00", 9.10), ("10:40:00", 9.56), ("10:41:00", 9.70)]),
        "300323": _write_bars(base_dir, "300323", [("09:31:00", 9.80), ("10:10:00", 10.25), ("10:11:00", 10.30)]),
        "600958": _write_bars(base_dir, "600958", [("09:31:00", 8.20), ("10:05:00", 7.88), ("10:06:00", 7.85)]),
        "601975": _write_bars(base_dir, "601975", [("09:31:00", 8.80), ("10:23:00", 8.70), ("14:50:00", 8.68)]),
        "600497": _write_bars(base_dir, "600497", [("09:31:00", 8.20), ("09:45:00", 7.88), ("10:23:00", 8.45)]),
    }
    return {"base_dir": base_dir, "files": {symbol: _display_path(path) for symbol, path in files.items()}}


def _initial_state() -> dict[str, Any]:
    def pos(symbol: str, name: str, qty: int, price: float) -> dict[str, Any]:
        return {
            "symbol": normalize_symbol(symbol),
            "name": name,
            "strategy_type": "long",
            "strategy_label": "长线",
            "quantity": qty,
            "sellable_quantity": qty,
            "frozen_quantity": 0,
            "avg_cost": price,
            "entry_price": price,
            "latest_price": price,
            "latest_date": PLAN_DATE,
            "management_state": "normal",
        }

    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "initial_cash": 100000.0,
        "cash": 100000.0,
        "positions": [
            pos("300323", "华灿光电", 200, 8.8),
            pos("600958", "东方证券", 100, 8.8),
            pos("600497", "驰宏锌锗", 100, 8.8),
        ],
        "closed_positions": [],
        "applied_execution_ids": [],
        "disclaimer": DISCLAIMER,
    }


def _scenario_expectations() -> list[dict[str, Any]]:
    return [
        {"name": "pullback_buy_trigger", "symbol": "600744", "expected_action": "buy", "expected_trigger_time": f"{TRADE_DATE} 10:23:00", "expected_trigger_price": 8.45},
        {"name": "breakout_buy_trigger", "symbol": "002115", "expected_action": "buy", "expected_trigger_time": f"{TRADE_DATE} 10:40:00", "expected_trigger_price": 9.56},
        {"name": "take_profit_reduce_half_trigger", "symbol": "300323", "expected_action": "reduce_half", "expected_trigger_time": f"{TRADE_DATE} 10:10:00", "expected_trigger_price": 10.25},
        {"name": "stop_loss_sell_all_trigger", "symbol": "600958", "expected_action": "stop_loss", "expected_trigger_time": f"{TRADE_DATE} 10:05:00", "expected_trigger_price": 7.88},
        {"name": "no_fill_not_triggered", "symbol": "601975", "expected_action": "no_fill"},
        {"name": "missing_intraday", "symbol": "002266", "expected_action": "no_fill", "expected_fill_model": "missing_intraday"},
        {"name": "stop_loss_priority_over_add", "symbol": "600497", "expected_action": "stop_loss", "expected_trigger_time": f"{TRADE_DATE} 09:45:00", "expected_trigger_price": 7.88},
    ]


def _event_by_symbol(execution: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = list(execution.get("events") or []) + list(execution.get("no_fill") or []) + list(execution.get("blocked") or [])
    return {normalize_symbol(row.get("symbol")): row for row in rows if isinstance(row, dict)}


def _same_price(actual: Any, expected: Any) -> bool:
    return round(float(safe_float(actual, 0) or 0), 4) == round(float(expected), 4)


def _validate_scenarios(execution: dict[str, Any], portfolio_update: dict[str, Any], initial_state: dict[str, Any]) -> list[dict[str, Any]]:
    by_symbol = _event_by_symbol(execution)
    scenarios: list[dict[str, Any]] = []
    for expected in _scenario_expectations():
        actual = by_symbol.get(normalize_symbol(expected["symbol"]), {})
        passed = str(actual.get("action") or "") == expected["expected_action"]
        if expected.get("expected_fill_model"):
            passed = passed and str(actual.get("fill_model") or "") == expected["expected_fill_model"]
        if expected.get("expected_trigger_time"):
            passed = passed and str(actual.get("trigger_time") or "") == expected["expected_trigger_time"]
        if expected.get("expected_trigger_price") is not None:
            passed = passed and _same_price(actual.get("trigger_price"), expected["expected_trigger_price"])
        scenarios.append({
            **expected,
            "actual_action": actual.get("action") or "",
            "actual_trigger_time": actual.get("trigger_time") or "",
            "actual_trigger_price": actual.get("trigger_price"),
            "actual_fill_model": actual.get("fill_model") or "",
            "is_reliable": bool(actual.get("is_reliable")),
            "passed": bool(passed),
            "reason": actual.get("reason") or "",
        })

    updated_state = portfolio_update.get("state") if isinstance(portfolio_update.get("state"), dict) else {}
    positions = {normalize_symbol(row.get("symbol")): row for row in updated_state.get("positions") or [] if isinstance(row, dict)}
    cash_changed = (safe_float(updated_state.get("cash"), 0) or 0) != (safe_float(initial_state.get("cash"), 0) or 0)
    buy_pos = positions.get("600744", {})
    scenarios.append({
        "name": "a_share_t1_frozen_quantity",
        "symbol": "600744",
        "expected_action": "buy",
        "actual_action": "buy",
        "expected_frozen_quantity": 100,
        "actual_frozen_quantity": buy_pos.get("frozen_quantity"),
        "expected_sellable_quantity": 0,
        "actual_sellable_quantity": buy_pos.get("sellable_quantity"),
        "passed": buy_pos.get("frozen_quantity") == 100 and buy_pos.get("sellable_quantity") == 0 and cash_changed,
    })

    no_fill_only = {"plan_id": "no_fill_only", "trade_date": TRADE_DATE, "events": [], "no_fill": [by_symbol.get("601975", {}), by_symbol.get("002266", {})], "blocked": []}
    no_fill_update = apply_conditional_execution_to_state(dict(initial_state), no_fill_only, daily_dir=PROJECT_ROOT / "__missing_daily__")
    no_fill_state = no_fill_update.get("state") if isinstance(no_fill_update.get("state"), dict) else {}
    scenarios.append({
        "name": "no_fill_and_missing_do_not_change_cash",
        "symbol": "601975,002266",
        "expected_action": "no_fill/missing_intraday",
        "actual_action": "no_fill/missing_intraday",
        "passed": safe_float(no_fill_state.get("cash"), None) == safe_float(initial_state.get("cash"), None)
        and len(no_fill_state.get("positions") or []) == len(initial_state.get("positions") or []),
    })
    return scenarios


_ACTION_LABELS = {
    "buy": "买入",
    "add": "加仓",
    "reduce_half": "减半",
    "take_profit_half": "止盈减半",
    "stop_loss": "止损",
    "sell_all": "清仓",
    "no_fill": "未成交/未触发",
    "missing_intraday": "缺分钟线",
    "no_fill/missing_intraday": "未成交/未触发 + 缺分钟线",
}


def _humanize_action(action: Any) -> str:
    text = str(action or "").strip()
    if not text:
        return "-"
    if text in _ACTION_LABELS:
        return _ACTION_LABELS[text]
    # composite like "a/b" -> humanize each part, avoiding raw token leakage
    parts = [p.strip() for p in text.split("/") if p.strip()]
    if len(parts) > 1:
        return " + ".join(_ACTION_LABELS.get(p, p) for p in parts)
    return text


def _write_report(payload: dict[str, Any], report_path: Path) -> None:
    lines = [
        "# 盘中回放自检报告",
        "",
        SYNTHETIC_NOTE,
        "",
        DISCLAIMER,
        "",
        "## 摘要",
        "",
        f"- 场景数：{payload['summary'].get('scenario_count')}",
        f"- 通过：{payload['summary'].get('passed')}",
        f"- 失败：{payload['summary'].get('failed')}",
        f"- 可靠成交：{payload['summary'].get('reliable_event_count')}",
        f"- 缺分钟线次数：{payload['summary'].get('missing_intraday_count')}",
        f"- 未成交/未触发数量：{payload['summary'].get('no_fill_count')}",
        "",
        "## 场景明细",
        "",
    ]
    for row in payload.get("scenarios") or []:
        status = "PASS" if row.get("passed") else "FAIL"
        lines.append(
            f"- {row.get('name')}（{status}）："
            f"执行动作：{_humanize_action(row.get('actual_action'))}；"
            f"触发时间：{row.get('actual_trigger_time') or '-'}；"
            f"触发价：{row.get('actual_trigger_price') or '-'}"
        )
    lines.extend([
        "",
        "## 边界说明",
        "",
        "- 只有可靠成交事件才能更新自动虚拟组合。",
        "- 未成交/未触发与缺分钟线时，现金和持仓保持不变。",
        "- 本自检不接券商、不真实下单、不构成投资建议。",
        "",
    ])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run_intraday_replay_self_validation(
    *,
    keep_artifacts: bool = True,
    write_latest: bool = False,
    update_portfolio: bool = False,
    verbose: bool = False,
    runtime_dir: Path | None = None,
    output_path: Path | None = None,
    execution_output_path: Path | None = None,
) -> dict[str, Any]:
    runtime_dir = Path(runtime_dir or TEST_RUNTIME_DIR)
    output_path = Path(output_path or SELF_VALIDATION_LATEST_JSON)
    execution_output_path = Path(execution_output_path or SELF_VALIDATION_EXECUTION_JSON)
    protected_intraday = PROJECT_ROOT / "data" / "intraday"
    ensure_directory_disjoint_from_canonical(
        runtime_dir,
        protected_intraday,
        role="self_validation_runtime_dir",
    )
    ensure_path_outside_canonical(
        output_path,
        protected_intraday,
        role="self_validation_output",
    )
    ensure_path_outside_canonical(
        execution_output_path,
        protected_intraday,
        role="self_validation_execution_output",
    )
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    artifacts = write_synthetic_intraday(runtime_dir)
    plan = build_self_validation_plan()
    execution = simulate_conditional_plan_file(
        plan,
        intraday_dir=runtime_dir,
        output_path=execution_output_path,
        write=True,
    )
    initial_state = _initial_state()
    portfolio_update = apply_conditional_execution_to_state(initial_state, execution, daily_dir=PROJECT_ROOT / "__missing_daily__")
    scenarios = _validate_scenarios(execution, portfolio_update, initial_state)
    failed = [row for row in scenarios if not row.get("passed")]
    report_path = REPORTS_DIR / f"intraday_replay_self_validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    summary = {
        "scenario_count": len(scenarios),
        "passed": len(scenarios) - len(failed),
        "failed": len(failed),
        "reliable_event_count": execution.get("summary", {}).get("reliable_event_count", 0),
        "missing_intraday_count": execution.get("summary", {}).get("missing_intraday_count", 0),
        "no_fill_count": execution.get("summary", {}).get("no_fill_count", 0),
    }
    payload = {
        "ok": not failed,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "intraday_replay_self_validation",
        "generated_at": now_text(),
        "is_real_order": False,
        "network": False,
        "uses_synthetic_data": True,
        "summary": summary,
        "scenarios": scenarios,
        "failed_scenarios": [row.get("name") for row in failed],
        "conditional_execution_summary": execution.get("summary", {}),
        "portfolio_update_summary": {
            "ok": bool(portfolio_update.get("ok")),
            "action_count": len(portfolio_update.get("actions") or []),
            "cash": (portfolio_update.get("state") or {}).get("cash") if isinstance(portfolio_update.get("state"), dict) else None,
            "position_count": len((portfolio_update.get("state") or {}).get("positions") or []) if isinstance(portfolio_update.get("state"), dict) else 0,
        },
        "artifacts": {
            "synthetic_intraday_dir": _display_path(runtime_dir),
            "conditional_execution_path": _display_path(execution_output_path),
            "report_path": _display_path(report_path),
            "synthetic_files": artifacts.get("files", {}),
        },
        "write_latest": bool(write_latest),
        "update_portfolio": bool(update_portfolio),
        "disclaimer": DISCLAIMER,
        "synthetic_note": SYNTHETIC_NOTE,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    _write_report(payload, report_path)
    write_json(output_path, payload)
    if write_latest:
        write_json(RESULTS_DIR / "conditional_execution_latest.json", execution)
    if not keep_artifacts and runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    if verbose:
        payload["execution"] = execution
        payload["portfolio_update"] = portfolio_update
    return payload


def latest_intraday_replay_self_validation(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or SELF_VALIDATION_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未运行盘中回放自检。",
            "disclaimer": DISCLAIMER,
        }
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload


__all__ = [
    "SELF_VALIDATION_LATEST_JSON",
    "build_self_validation_plan",
    "latest_intraday_replay_self_validation",
    "run_intraday_replay_self_validation",
    "write_synthetic_intraday",
]
