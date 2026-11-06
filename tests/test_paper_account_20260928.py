"""T24: partial-sale realized PnL must survive the real save/load path."""

from __future__ import annotations

import ast
import inspect
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper import position_management_engine as engine
from quantdesk.paper.adjustment_policy import AdjustmentFactorTable
from quantdesk.paper.execution import conditional as conditional_execution
from quantdesk.paper.execution import daily as daily_execution

ENTRY_DAY = "2026-09-21"
PARTIAL_DAY = "2026-09-22"
FINAL_DAY = "2026-09-23"
SYMBOL = "600000"


def _minutes(day: str, steps: list[tuple[str, float]]) -> list[dict[str, object]]:
    stamps = [
        *pd.date_range(f"{day} 09:31:00", f"{day} 11:30:00", freq="min"),
        *pd.date_range(f"{day} 13:01:00", f"{day} 15:00:00", freq="min"),
    ]
    rows: list[dict[str, object]] = []
    for stamp in stamps:
        time_text = stamp.strftime("%H:%M:%S")
        active = [price for start, price in steps if start <= time_text]
        price = float(active[-1])
        rows.append(
            {
                "symbol": SYMBOL,
                "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "open": price,
                "high": price + 0.01,
                "low": price - 0.01,
                "close": price,
                "volume": 1000,
                "amount": price * 1000,
                "adjust": "raw",
                "source": "synthetic_fixture",
            }
        )
    return rows


def _market(root: Path, day: str, steps: list[tuple[str, float]]) -> tuple[Path, Path, Path]:
    intraday = root / "data" / "intraday"
    minute_path = intraday / "1m" / f"{SYMBOL}.csv"
    minute_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_minutes(day, steps)).to_csv(minute_path, index=False, encoding="utf-8")
    daily = root / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    close = float(steps[-1][1])
    pd.DataFrame(
        [
            {
                "date": ENTRY_DAY,
                "symbol": SYMBOL,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1000,
                "amount": 10000,
                "adjust": "raw",
            },
            {
                "date": day,
                "symbol": SYMBOL,
                "open": float(steps[0][1]),
                "high": max(price for _, price in steps) + 0.1,
                "low": min(price for _, price in steps) - 0.1,
                "close": close,
                "volume": 240000,
                "amount": close * 240000,
                "adjust": "raw",
            },
        ]
    ).to_csv(daily / f"{SYMBOL}.csv", index=False, encoding="utf-8")
    window = root / "results" / "data_health" / "window.json"
    window.parent.mkdir(parents=True, exist_ok=True)
    window.write_text(
        json.dumps(
            {
                "ok": True,
                "task_type": "minute_pool_window_audit",
                "window_start": day,
                "window_end": day,
                "trading_days": [day],
                "trading_day_count": 1,
                "total_symbols": 1,
                "fully_usable_symbol_count": 1,
                "symbols": [
                    {
                        "symbol": SYMBOL,
                        "day_status": {day: "usable"},
                        "day_evidence": {day: {"formal_execution_eligible": True,
                                               "reliable_no_fill_eligible": True,
                                               "session_status": "complete_session"}},
                        "usable_day_count": 1,
                        "all_days_usable": True,
                    }
                ],
                "criteria": {"usable_standard": "existing_intraday_quality_audit_v1",
                             "eligibility_standard": "formal_session_evidence_v2"},
                "read_only": True,
                "network_used": False,
                "canonical_write": False,
                "fixture_kind": "synthetic_quality_metadata_prerequisite",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return intraday, daily, window


def _plan(day: str, action: str, price: float, quantity: int) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": SYMBOL,
        "name": "Synthetic A",
        "strategy_type": "long",
        "trade_date": day,
        "plan_date": ENTRY_DAY,
        "lot_size": 100,
        "position_state": "holding",
        "current_quantity": quantity,
        "sellable_quantity": quantity,
        "rules": {"risk_guards": []},
    }
    if action == "reduce_half":
        item["rules"]["exit_rules"] = [
            {
                "action": "reduce_half",
                "trigger_type": "take_profit_half",
                "price_low": price,
                "price_high": price + 1.0,
                "quantity_pct": 50,
                "reason": "synthetic partial exit fixture",
            }
        ]
    else:
        item["rules"]["exit_rules"] = [
            {
                "action": "sell_all",
                "trigger_type": "stop_loss",
                "price_low": 0.0,
                "price_high": price,
                "quantity_pct": 100,
                "reason": "synthetic full exit fixture",
            }
        ]
    return item


def _replay(root: Path, day: str, plan_id: str, item: dict[str, object]) -> dict:
    intraday, _, window = _market(
        root,
        day,
        [("09:31:00", 10.0), ("10:00:00", 11.0)]
        if day == PARTIAL_DAY
        else [("09:31:00", 12.5), ("14:00:00", 12.0)],
    )
    execution = engine.simulate_conditional_plan_file(
        {
            "plan_id": plan_id,
            "plan_date": ENTRY_DAY,
            "trade_date": day,
            "symbols": [deepcopy(item)],
        },
        intraday_dir=intraday,
        window_snapshot_path=window,
        include_risk_overlays=False,
        write=False,
    )
    assert execution.get("ok") is True
    assert len(execution["events"]) == 1
    assert execution["events"][0]["is_reliable"] is True
    return execution


def _apply(state: dict, execution: dict, daily: Path) -> dict:
    factors = AdjustmentFactorTable(
        rows_by_symbol={SYMBOL: [(20260921, 1.0), (20260922, 1.0), (20260923, 1.0)]},
        _loaded_all=True,
    )
    result = engine.apply_conditional_execution_to_state(
        deepcopy(state),
        execution,
        daily_dir=daily,
        adjustment_factors=factors,
        risk_gate_shadow_enabled=False,
        risk_gate_shadow_write_errors=False,
    )
    assert result.get("ok") is True
    assert result.get("already_updated_today") is False
    return result


def _opening_state(monkeypatch, path: Path) -> dict:
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_STATE_JSON", path)
    state = {
        "ledger_id": "synthetic_strategy_book_diagnostic",
        "initial_cash": 10000.0,
        "cash": 8000.0,
        "profile_id": "user_10k",
        "scenario_id": "user_10k",
        "positions": [
            {
                "symbol": SYMBOL,
                "name": "Synthetic A",
                "strategy_type": "long",
                "quantity": 200,
                "sellable_quantity": 200,
                "frozen_quantity": 0,
                "avg_cost": 10.0,
                "entry_price": 10.0,
                "entry_date": ENTRY_DAY,
                "last_buy_date": ENTRY_DAY,
                "latest_price": 10.0,
                "latest_date": ENTRY_DAY,
                "market_value": 2000.0,
                "unrealized_pnl": 0.0,
                "management_state": "normal",
                "status": "open",
            }
        ],
        "closed_positions": [],
        "actions": [],
        "realized_pnl": 0.0,
        "research_only": True,
        "paper_only": True,
        "is_real_order": False,
        "no_broker": True,
        "fixture_kind": "synthetic_only",
    }
    paper._write_auto_paper_state(state)
    loaded = paper._load_state(10000.0)
    assert loaded["legacy_realized_adjustment"] == 0.0
    assert loaded["cash"] == 8000.0
    return loaded


def test_partial_sale_realized_pnl_survives_save_and_load(tmp_path: Path, monkeypatch) -> None:
    loaded = _opening_state(monkeypatch, tmp_path / "state" / "auto_paper_portfolio.json")
    first = _apply(
        loaded,
        _replay(
            tmp_path / "partial",
            PARTIAL_DAY,
            "partial-sale",
            _plan(PARTIAL_DAY, "reduce_half", 11.0, 200),
        ),
        tmp_path / "partial" / "data" / "daily",
    )
    first_action = first["actions"][0]
    assert first_action["action"] == "reduce_half"
    assert first_action["quantity"] == 100
    assert round(float(first_action["pnl"]), 2) == 93.90
    assert round(float(first["state"]["realized_pnl"]), 2) == 93.90
    assert round(float(first["state"]["cash"]), 2) == 9093.90

    paper._write_auto_paper_state(first["state"])
    reloaded = paper._load_state(10000.0)
    assert reloaded["legacy_realized_adjustment"] == 0.0
    assert round(float(reloaded["cash"]), 2) == 9093.90
    assert round(float(reloaded["realized_pnl"]), 2) == 93.90
    assert sum(row["quantity"] for row in reloaded["positions"]) == 100

    final = _apply(
        reloaded,
        _replay(
            tmp_path / "final",
            FINAL_DAY,
            "final-sale",
            _plan(FINAL_DAY, "sell_all", 12.0, 100),
        ),
        tmp_path / "final" / "data" / "daily",
    )
    final_action = final["actions"][0]
    assert final_action["action"] == "stop_loss"
    assert final_action["quantity"] == 100
    assert round(float(final_action["pnl"]), 2) == 193.80
    paper._write_auto_paper_state(final["state"])
    closed = paper._load_state(10000.0)
    assert closed["legacy_realized_adjustment"] == 0.0
    assert round(float(closed["cash"]), 2) == 10287.70
    assert round(float(closed["realized_pnl"]), 2) == 287.70
    assert closed["positions"] == []
    assert len(closed["closed_positions"]) == 1


def test_full_exit_realized_pnl_survives_save_and_load(tmp_path: Path, monkeypatch) -> None:
    loaded = _opening_state(monkeypatch, tmp_path / "state" / "auto_paper_portfolio.json")
    result = _apply(
        loaded,
        _replay(
            tmp_path / "full",
            PARTIAL_DAY,
            "full-exit-control",
            _plan(PARTIAL_DAY, "sell_all", 11.0, 200),
        ),
        tmp_path / "full" / "data" / "daily",
    )
    action = result["actions"][0]
    assert action["action"] == "stop_loss"
    assert action["quantity"] == 200
    expected = round(float(action["pnl"]), 2)
    paper._write_auto_paper_state(result["state"])
    reloaded = paper._load_state(10000.0)
    assert round(float(reloaded["realized_pnl"]), 2) == expected
    assert round(float(reloaded["cash"]), 2) == round(float(result["state"]["cash"]), 2)
    assert reloaded["positions"] == []
    assert len(reloaded["closed_positions"]) == 1


def _production_latest_money(source_value: str, state: dict, actions: list[dict]) -> list[dict]:
    implementation = {
        "conditional_position_management": conditional_execution.run,
        "formal_candidate_daily_batch": daily_execution.run,
    }[source_value]
    syntax = ast.parse(Path(inspect.getsourcefile(implementation)).read_text(encoding="utf-8-sig"))
    function_name = implementation.__name__
    function = next(
        node for node in syntax.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    dictionaries = [node for node in ast.walk(function) if isinstance(node, ast.Dict)]
    publications = [
        node
        for node in dictionaries
        if any(
            isinstance(key, ast.Constant)
            and key.value == "source"
            and isinstance(value, ast.Constant)
            and value.value == source_value
            for key, value in zip(node.keys, node.values)
        )
    ]
    assert publications, f"{function_name} must publish {source_value}"
    fields = {
        "summary",
        "positions",
        "closed_positions",
        "realized_reductions",
        "legacy_realized_adjustment",
    }
    snapshots = []
    for real in publications:
        pairs = [
            (key, value)
            for key, value in zip(real.keys, real.values)
            if isinstance(key, ast.Constant) and key.value in fields
        ]
        expression = ast.Expression(ast.Dict(keys=[key for key, _ in pairs], values=[value for _, value in pairs]))
        ast.fix_missing_locations(expression)
        context = {
            "state": state,
            "state_for_latest": state,
            "actions": actions,
            "latest_state": state,
            "positions": state.get("positions") or [],
            "enriched_positions": state.get("positions") or [],
            "closed_positions": state.get("closed_positions") or [],
            "summary": paper._summary(state, actions),
            "summary_for_state": paper._summary(state, actions),
            "_summary": paper._summary,
            "ports": SimpleNamespace(valuation=SimpleNamespace(summary=paper._summary)),
        }
        snapshots.append(eval(compile(expression, "actual_latest_money_projection", "eval"), {}, context))
    return snapshots


def _assert_latest_keeps_sale_detail(state: dict, written: dict, *, partial: float) -> None:
    assert round(float(written["realized_pnl"]), 2) == round(float(state["realized_pnl"]), 2)
    assert written["legacy_realized_adjustment"] == state["legacy_realized_adjustment"]
    assert round(float(written["accounting_reconciliation"]["partial_realized_pnl"]), 2) == partial
    assert round(float(written["summary"]["partial_realized_pnl"]), 2) == partial
    assert written["realized_reductions"] == state["realized_reductions"]


def test_latest_publication_keeps_partial_reduction_detail(tmp_path: Path, monkeypatch) -> None:
    loaded = _opening_state(monkeypatch, tmp_path / "state" / "book.json")
    result = _apply(
        loaded,
        _replay(
            tmp_path / "market",
            PARTIAL_DAY,
            "publication-probe",
            _plan(PARTIAL_DAY, "reduce_half", 11.0, 200),
        ),
        tmp_path / "market" / "data" / "daily",
    )
    paper._write_auto_paper_state(result["state"])
    state = paper._load_state(10000.0)
    before = (tmp_path / "state" / "book.json").read_bytes()
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    for source in (
        "conditional_position_management",
        "formal_candidate_daily_batch",
    ):
        for snapshot in _production_latest_money(source, state, result["actions"]):
            written = paper._write_auto_paper_latest(snapshot)
            _assert_latest_keeps_sale_detail(state, written, partial=93.90)
    for stale in _production_latest_money("conditional_position_management", state, []):
        written_stale = paper._write_auto_paper_latest(stale)
        _assert_latest_keeps_sale_detail(state, written_stale, partial=93.90)
    assert (tmp_path / "state" / "book.json").read_bytes() == before


def _timed_market(root: Path, day: str, steps_by_symbol: dict[str, list[tuple[str, float]]]) -> tuple[Path, Path, Path]:
    intraday = root / "data" / "intraday"
    daily = root / "data" / "daily"
    (intraday / "1m").mkdir(parents=True, exist_ok=True)
    daily.mkdir(parents=True, exist_ok=True)
    for symbol, steps in steps_by_symbol.items():
        rows = []
        for start, price in steps:
            rows.append(
                {
                    "symbol": symbol,
                    "datetime": f"{day} {start}",
                    "open": price,
                    "high": price + 0.01,
                    "low": price - 0.01,
                    "close": price,
                    "volume": 1000,
                    "amount": price * 1000,
                    "adjust": "raw",
                    "source": "synthetic_fixture",
                }
            )
        pd.DataFrame(rows).to_csv(intraday / "1m" / f"{symbol}.csv", index=False, encoding="utf-8")
        close = float(steps[-1][1])
        pd.DataFrame(
            [
                {
                    "date": ENTRY_DAY,
                    "symbol": symbol,
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 1000,
                    "amount": 10000,
                    "adjust": "raw",
                },
                {
                    "date": day,
                    "symbol": symbol,
                    "open": float(steps[0][1]),
                    "high": max(price for _, price in steps) + 0.1,
                    "low": min(price for _, price in steps) - 0.1,
                    "close": close,
                    "volume": 240000,
                    "amount": close * 240000,
                    "adjust": "raw",
                },
            ]
        ).to_csv(daily / f"{symbol}.csv", index=False, encoding="utf-8")
    window = root / "results" / "data_health" / "window.json"
    window.parent.mkdir(parents=True, exist_ok=True)
    window.write_text(
        json.dumps(
            {
                "ok": True,
                "task_type": "minute_pool_window_audit",
                "window_start": day,
                "window_end": day,
                "trading_days": [day],
                "trading_day_count": 1,
                "total_symbols": len(steps_by_symbol),
                "fully_usable_symbol_count": len(steps_by_symbol),
                "symbols": [
                    {
                        "symbol": symbol,
                        "day_status": {day: "usable"},
                        "day_evidence": {day: {"formal_execution_eligible": True,
                                               "reliable_no_fill_eligible": True,
                                               "session_status": "complete_session"}},
                        "usable_day_count": 1,
                        "all_days_usable": True,
                    }
                    for symbol in steps_by_symbol
                ],
                "criteria": {"usable_standard": "existing_intraday_quality_audit_v1",
                             "eligibility_standard": "formal_session_evidence_v2"},
                "read_only": True,
                "network_used": False,
                "canonical_write": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return intraday, daily, window


def _buy_plan(symbol: str, day: str, price: float, quantity: int = 100) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"Synthetic {symbol}",
        "strategy_type": "long",
        "trade_date": day,
        "plan_date": ENTRY_DAY,
        "lot_size": 100,
        "position_state": "new_candidate",
        "rules": {
            "risk_guards": [],
            "entry_rules": [
                {
                    "action": "buy",
                    "trigger_type": "pullback_buy",
                    "price_low": price,
                    "price_high": price,
                    "quantity": quantity,
                    "reason": "synthetic entry fixture",
                }
            ],
        },
    }


def _holding_state(cash: float) -> dict[str, object]:
    return {
        "ledger_id": "synthetic_strategy_book_timing",
        "initial_cash": 10000.0,
        "cash": cash,
        "profile_id": "user_10k",
        "scenario_id": "user_10k",
        "positions": [
            {
                "symbol": SYMBOL,
                "name": "Synthetic A",
                "strategy_type": "long",
                "quantity": 200,
                "sellable_quantity": 200,
                "frozen_quantity": 0,
                "avg_cost": 10.0,
                "entry_price": 10.0,
                "entry_date": ENTRY_DAY,
                "last_buy_date": ENTRY_DAY,
                "latest_price": 10.0,
                "latest_date": ENTRY_DAY,
                "market_value": 2000.0,
                "unrealized_pnl": 0.0,
                "management_state": "normal",
                "status": "open",
            }
        ],
        "closed_positions": [],
        "realized_reductions": [],
        "actions": [],
        "realized_pnl": 0.0,
        "legacy_realized_adjustment": 0.0,
        "research_only": True,
        "paper_only": True,
        "is_real_order": False,
        "no_broker": True,
    }


def _replay_symbols(root: Path, day: str, plan_id: str, items: list[dict[str, object]]) -> dict:
    steps = {
        SYMBOL: [("09:31:00", 10.5), ("14:00:00", 10.0)],
        "000001": [("09:31:00", 9.5), ("10:00:00", 10.0)],
    }
    intraday, _, window = _timed_market(root, day, steps)
    execution = engine.simulate_conditional_plan_file(
        {
            "plan_id": plan_id,
            "plan_date": ENTRY_DAY,
            "trade_date": day,
            "symbols": deepcopy(items),
        },
        intraday_dir=intraday,
        window_snapshot_path=window,
        include_risk_overlays=False,
        write=False,
    )
    assert execution.get("ok") is True
    assert len(execution["events"]) == len(items)
    return execution


def _apply_timed(state: dict, execution: dict, daily: Path) -> dict:
    factors = AdjustmentFactorTable(
        rows_by_symbol={
            SYMBOL: [(20260921, 1.0), (20260922, 1.0)],
            "000001": [(20260921, 1.0), (20260922, 1.0)],
        },
        _loaded_all=True,
    )
    result = engine.apply_conditional_execution_to_state(
        deepcopy(state),
        execution,
        daily_dir=daily,
        adjustment_factors=factors,
        risk_gate_shadow_enabled=False,
        risk_gate_shadow_write_errors=False,
    )
    assert result.get("ok") is True
    return result


def test_later_sale_cannot_fund_earlier_buy(tmp_path: Path) -> None:
    sell = _plan(PARTIAL_DAY, "sell_all", 10.0, 200)
    buy = _buy_plan("000001", PARTIAL_DAY, 10.0)
    outcomes = []
    for name, items in (
        ("sell_listed_first", [sell, buy]),
        ("buy_listed_first", [buy, sell]),
    ):
        execution = _replay_symbols(tmp_path / name, PARTIAL_DAY, "timing-zero-cash", items)
        by_symbol = {event["symbol"]: event for event in execution["events"]}
        assert by_symbol[SYMBOL]["trigger_time"] == f"{PARTIAL_DAY} 14:00:00"
        assert by_symbol["000001"]["trigger_time"] == f"{PARTIAL_DAY} 10:00:00"
        result = _apply_timed(_holding_state(0.0), execution, tmp_path / name / "data" / "daily")
        actions = result["actions"]
        assert [row["action"] for row in actions if row["symbol"] == "000001"] == ["block"]
        assert [row["action"] for row in actions if row["symbol"] == SYMBOL] == ["stop_loss"]
        assert [row["symbol"] for row in result["state"]["positions"]] == []
        outcomes.append(round(float(result["state"]["cash"]), 2))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] > 0


def test_existing_cash_buy_is_independent_of_symbol_order(tmp_path: Path) -> None:
    sell = _plan(PARTIAL_DAY, "sell_all", 10.0, 200)
    buy = _buy_plan("000001", PARTIAL_DAY, 10.0)
    outcomes = []
    for name, items in (
        ("sell_listed_first", [sell, buy]),
        ("buy_listed_first", [buy, sell]),
    ):
        execution = _replay_symbols(tmp_path / name, PARTIAL_DAY, "timing-funded", items)
        result = _apply_timed(_holding_state(2000.0), execution, tmp_path / name / "data" / "daily")
        assert [row["action"] for row in result["actions"] if row["symbol"] == "000001"] == ["buy"]
        outcomes.append(
            (
                round(float(result["state"]["cash"]), 2),
                [(row["symbol"], row["quantity"]) for row in result["state"]["positions"]],
            )
        )
    assert outcomes[0] == outcomes[1]


def test_same_trigger_time_keeps_original_event_order(tmp_path: Path) -> None:
    first = _buy_plan("000001", PARTIAL_DAY, 10.0)
    second = _buy_plan("000002", PARTIAL_DAY, 10.0)
    root = tmp_path / "tie"
    steps = {
        "000001": [("10:00:00", 10.0)],
        "000002": [("10:00:00", 10.0)],
    }
    intraday, daily, window = _timed_market(root, PARTIAL_DAY, steps)
    execution = engine.simulate_conditional_plan_file(
        {
            "plan_id": "same-time",
            "plan_date": ENTRY_DAY,
            "trade_date": PARTIAL_DAY,
            "symbols": [first, second],
        },
        intraday_dir=intraday,
        window_snapshot_path=window,
        include_risk_overlays=False,
        write=False,
    )
    result = engine.apply_conditional_execution_to_state(
        {"initial_cash": 10000.0, "cash": 1005.5, "positions": [], "closed_positions": [], "realized_reductions": []},
        execution,
        daily_dir=daily,
        adjustment_factors=AdjustmentFactorTable(
            rows_by_symbol={"000001": [(20260922, 1.0)], "000002": [(20260922, 1.0)]},
            _loaded_all=True,
        ),
        risk_gate_shadow_enabled=False,
        risk_gate_shadow_write_errors=False,
    )
    assert [row["action"] for row in result["actions"]] == ["buy", "block"]
    assert [row["symbol"] for row in result["actions"]] == ["000001", "000002"]
    repeated = engine.apply_conditional_execution_to_state(
        result["state"],
        execution,
        daily_dir=daily,
        risk_gate_shadow_enabled=False,
        risk_gate_shadow_write_errors=False,
    )
    assert repeated["already_updated_today"] is True
    assert repeated["actions"] == []
    assert repeated["state"]["positions"] == result["state"]["positions"]


def _redirect_paper_outputs(monkeypatch, tmp_path: Path) -> None:
    """把纸面链路的**所有**落盘出口改到测试临时目录。

    跑全链路（``_run_auto_paper_portfolio_unlocked``）的用例此前只改了账本与最新快照的
    路径，**历史输出**（``results/auto_paper_portfolio_history.json``）和 ``reports/`` 下的
    报告仍写到真实目录：在只从源码还原的验收副本里那是越界写，独立验收因此少过一项。
    这里把 results/reports/state 三个根与派生常量一起重定向，测试结束即随 tmp_path 消失。
    """

    monkeypatch.setattr(paper, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(paper, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(paper, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_STATE_JSON",
                        tmp_path / "state" / "auto_paper_portfolio.json")
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON",
                        tmp_path / "results" / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON",
                        tmp_path / "results" / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(paper, "SWING_TRADE_PLANS_LATEST_JSON",
                        tmp_path / "results" / "swing_trade_plans_latest.json")
    monkeypatch.setattr(paper, "INTRADAY_EXECUTION_LATEST_JSON",
                        tmp_path / "results" / "intraday_execution_latest.json")
    monkeypatch.setattr(paper, "RISK_GATE_SHADOW_LATEST_JSON",
                        tmp_path / "results" / "risk_gate_shadow_latest.json")
    monkeypatch.setattr(paper, "RISK_GATE_SHADOW_ERRORS_JSONL",
                        tmp_path / "results" / "risk_gate_shadow_errors.jsonl")


def test_daily_save_refreshes_return_and_last_trade_date(tmp_path: Path, monkeypatch) -> None:
    _redirect_paper_outputs(monkeypatch, tmp_path)
    real_history = paper.PROJECT_ROOT / "results" / "auto_paper_portfolio_history.json"
    real_history_mtime = real_history.stat().st_mtime_ns if real_history.exists() else None
    state_path = tmp_path / "state" / "auto_paper_portfolio.json"
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path)
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    state = _holding_state(8000.0)
    state.update(total_return_pct=-0.69, last_trade_date=ENTRY_DAY)
    paper._write_auto_paper_state(state)
    loaded = paper._load_state(10000.0)
    assert loaded["total_return_pct"] == -0.69
    assert loaded["last_trade_date"] == ENTRY_DAY

    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    pd.DataFrame(
        [
            {"date": ENTRY_DAY, "symbol": SYMBOL, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1000, "amount": 10000, "adjust": "raw"},
            {"date": PARTIAL_DAY, "symbol": SYMBOL, "open": 9.5, "high": 9.5, "low": 9.5, "close": 9.5, "volume": 1000, "amount": 9500, "adjust": "raw"},
        ]
    ).to_csv(daily / f"{SYMBOL}.csv", index=False, encoding="utf-8")
    monkeypatch.setattr(paper, "DAILY_DIR", daily)
    monkeypatch.setattr(paper, "evaluate_data_freshness", lambda: {"ok": True, "target_complete_trade_date": PARTIAL_DAY, "can_review_yesterday_positions": True, "can_update_portfolio_valuation": True})
    monkeypatch.setattr(paper, "evaluate_paper_review_freshness", lambda *_args, **_kwargs: paper.evaluate_data_freshness())
    monkeypatch.setattr(paper, "infer_target_date", lambda target_date=None: (PARTIAL_DAY, "fixture"))
    monkeypatch.setattr(paper, "_load_auto_adjustment_factors", lambda _symbols: AdjustmentFactorTable(rows_by_symbol={SYMBOL: [(20260921, 1.0), (20260922, 1.0)]}, _loaded_all=True))
    monkeypatch.setattr(paper, "apply_delisting_liquidation", lambda current, **_kwargs: {"ok": True, "liquidated": 0, "coverage": "fixture", "warnings": [], "actions": [], "state": current})

    payload = paper._run_auto_paper_portfolio_unlocked(
        mode="update",
        enable_intraday_swing=False,
        risk_gate_shadow_enabled=False,
        auto_strategy_payload={"generated_at": f"{PARTIAL_DAY}T15:30:00", "long_candidates": [], "board_candidates": []},
    )
    saved = paper._load_state(10000.0)
    observed_actions = {str(row.get("action") or "") for row in payload["actions"]}
    assert "sell" not in observed_actions
    assert round(float(saved["total_return_pct"]), 2) == round(float(payload["summary"]["total_return_pct"]), 2)
    assert saved["last_trade_date"] == ENTRY_DAY
    assert saved["total_return_pct"] != -0.69

    unchanged = paper._run_auto_paper_portfolio_unlocked(
        mode="update",
        enable_intraday_swing=False,
        risk_gate_shadow_enabled=False,
        auto_strategy_payload={"generated_at": f"{FINAL_DAY}T15:30:00", "long_candidates": [], "board_candidates": []},
    )
    reloaded = paper._load_state(10000.0)
    assert reloaded["last_trade_date"] == ENTRY_DAY
    assert unchanged["summary"]["total_return_pct"] == saved["total_return_pct"]
    # 历史输出与报告必须落在测试临时目录，真实 results/reports 一个字节都不许动。
    assert (tmp_path / "results" / "auto_paper_portfolio_history.json").is_file()
    assert (tmp_path / "latest.json").is_file()
    real_history = paper.PROJECT_ROOT / "results" / "auto_paper_portfolio_history.json"
    assert real_history_mtime == (real_history.stat().st_mtime_ns if real_history.exists() else None), (
        "全链路跑完动了真实 results/auto_paper_portfolio_history.json"
    )


def test_latest_publication_keeps_full_exit_without_fake_reduction(tmp_path: Path, monkeypatch) -> None:
    loaded = _opening_state(monkeypatch, tmp_path / "state" / "book.json")
    result = _apply(
        loaded,
        _replay(
            tmp_path / "market",
            PARTIAL_DAY,
            "full-publication-control",
            _plan(PARTIAL_DAY, "sell_all", 11.0, 200),
        ),
        tmp_path / "market" / "data" / "daily",
    )
    paper._write_auto_paper_state(result["state"])
    state = paper._load_state(10000.0)
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    for snapshot in _production_latest_money("conditional_position_management", state, result["actions"]):
        written = paper._write_auto_paper_latest(snapshot)
        _assert_latest_keeps_sale_detail(state, written, partial=0.0)
        assert written["realized_reductions"] == []
        assert len(written["closed_positions"]) == 1
def _production_stale_projection(state: dict, actions: list[dict]) -> dict:
    """从生产源码里取出**陈旧观察**分支的 latest 投影（含资金字段），原样喂给发布 writer。

    与 ``_production_latest_money`` 的差别有两点，正是 T24 返修要看的地方：
    1. 按 ``review_mode == "stale_observation"`` 选中**陈旧观察**那一份生产字典；
    2. 取值集合包含 ``realized_pnl`` 等资金字段——少了它们，reconcile 不会去反推
       "旧历史差额"，缺口就被掩盖了。
    """

    syntax = ast.parse(Path(inspect.getsourcefile(conditional_execution.run)).read_text(encoding="utf-8-sig"))
    function = next(node for node in syntax.body if isinstance(node, ast.FunctionDef)
                    and node.name == conditional_execution.run.__name__)
    stale = next(
        node
        for node in (item for item in ast.walk(function) if isinstance(item, ast.Dict))
        if any(
            isinstance(key, ast.Constant)
            and key.value == "review_mode"
            and isinstance(value, ast.Constant)
            and value.value == "stale_observation"
            for key, value in zip(node.keys, node.values)
        )
    )
    fields = {
        "summary",
        "positions",
        "closed_positions",
        "realized_reductions",
        "legacy_realized_adjustment",
        "realized_pnl",
        "cash",
        "market_value",
        "unrealized_pnl",
        "total_equity",
        "position_count",
        "warnings",
    }
    pairs = [
        (key, value)
        for key, value in zip(stale.keys, stale.values)
        if isinstance(key, ast.Constant) and key.value in fields
    ]
    expression = ast.Expression(
        ast.Dict(keys=[key for key, _ in pairs], values=[value for _, value in pairs])
    )
    ast.fix_missing_locations(expression)
    return eval(
        compile(expression, "actual_stale_latest_projection", "eval"),
        {},
        {
            "state": state,
            "state_for_latest": state,
            "latest_state": state,
            "actions": actions,
            "positions": state.get("positions") or [],
            "enriched_positions": state.get("positions") or [],
            "closed_positions": state.get("closed_positions") or [],
            "summary": paper._summary(state, actions),
            "_summary": paper._summary,
            "ports": SimpleNamespace(valuation=SimpleNamespace(summary=paper._summary)),
            "candidate_intake": {},
        },
    )


def test_stale_projection_keeps_the_new_reduction_detail_in_the_published_latest(
    tmp_path: Path, monkeypatch
) -> None:
    """T24-LATEST-REDUCTIONS-20260929：陈旧观察的最新投影也要带全减仓明细与显式历史差额。

    修前：该投影只有 summary/positions/closed_positions（加资金字段），reconcile 于是
    用"已实现盈亏 − 明细"反推出 93.90 的历史差额，把刚发生的半卖利润写成 legacy、
    realized_reductions 清空。修后：两把键都在，明细 93.90 / 历史差额 0 如实落盘。
    """

    loaded = _opening_state(monkeypatch, tmp_path / "state" / "book.json")
    result = _apply(
        loaded,
        _replay(
            tmp_path / "market",
            PARTIAL_DAY,
            "stale-projection-probe",
            _plan(PARTIAL_DAY, "reduce_half", 11.0, 200),
        ),
        tmp_path / "market" / "data" / "daily",
    )
    paper._write_auto_paper_state(result["state"])
    state = paper._load_state(10000.0)

    projection = _production_stale_projection(state, result["actions"])

    assert "realized_reductions" in projection, "陈旧观察的最新投影必须带上减仓明细"
    assert "legacy_realized_adjustment" in projection, "陈旧观察的最新投影必须带上显式历史差额"
    assert projection["realized_reductions"] == state["realized_reductions"]
    monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    written = paper._write_auto_paper_latest(projection)
    _assert_latest_keeps_sale_detail(state, written, partial=93.90)
    assert written["legacy_realized_adjustment"] == 0.0, "新发生的半卖利润不得被写成旧历史差额"
    assert written["realized_reductions"], "最新快照必须留下这次减仓的明细"
def _basis_frame(*labels: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": SYMBOL,
                "datetime": f"{PARTIAL_DAY} 09:{31 + index:02d}:00",
                "open": 9.5,
                "high": 9.5,
                "low": 9.5,
                "close": 9.5,
                "volume": 1000,
                "amount": 9500,
                "adjust": label,
            }
            for index, label in enumerate(labels)
        ]
    )


def test_conditional_minute_rejects_unknown_or_nonraw_basis() -> None:
    plan = _plan(PARTIAL_DAY, "sell_all", 10.0, 200)
    plan["sellable_quantity"] = 200
    raw = engine.simulate_conditional_plan(plan, _basis_frame("raw"))
    assert raw["action"] == "stop_loss"
    assert raw["is_reliable"] is True
    assert raw["quantity"] == 200
    assert raw.get("price_basis") == "raw"

    for label in ("qfq", "hfq"):
        proxy = engine.simulate_conditional_plan(plan, _basis_frame(label))
        assert proxy["is_reliable"] is False
        assert proxy["action"] != "stop_loss"
        assert proxy.get("execution_price_label") == "same_basis_research_proxy"
        assert proxy.get("real_execution_price_claim") is False

    unknown = engine.simulate_conditional_plan(plan, _basis_frame("unknown"))
    mixed = engine.simulate_conditional_plan(plan, _basis_frame("raw", "qfq"))
    assert unknown["is_reliable"] is False
    assert mixed["is_reliable"] is False
    assert unknown["action"] != "stop_loss"
    assert mixed["action"] != "stop_loss"

    blocked = engine.apply_conditional_execution_to_state(
        _holding_state(8000.0),
        {
            "plan_id": "basis-gate",
            "trade_date": PARTIAL_DAY,
            "events": [],
            "blocked": [unknown, mixed],
            "no_fill": [],
        },
        daily_dir=Path("__missing_daily__"),
        risk_gate_shadow_enabled=False,
        risk_gate_shadow_write_errors=False,
    )
    assert blocked["state"]["cash"] == 8000.0
    assert blocked["state"]["positions"][0]["quantity"] == 200
def _dual_basis_frame(*columns: tuple[str, object]) -> pd.DataFrame:
    """构造**同时**声明两列口径的分钟帧；列顺序按传入顺序，用于验证判定与列序无关。"""

    size = 2
    payload: dict[str, object] = {
        "symbol": [SYMBOL] * size,
        "datetime": [f"{PARTIAL_DAY} 09:3{index + 1}:00" for index in range(size)],
        "open": [9.5] * size,
        "high": [9.5] * size,
        "low": [9.5] * size,
        "close": [9.5] * size,
        "volume": [1000] * size,
        "amount": [9500] * size,
    }
    for name, value in columns:
        payload[name] = value if isinstance(value, list) else [value] * size
    return pd.DataFrame(payload)


def _blocked_state_cash_and_quantity(blocked: dict) -> tuple[float, int]:
    """把被拦事件沿真实入口喂进纸面账，返回（现金、持仓数量）。"""

    result = engine.apply_conditional_execution_to_state(
        _holding_state(8000.0),
        {
            "plan_id": "dual-basis",
            "trade_date": PARTIAL_DAY,
            "events": [],
            "blocked": [blocked],
            "no_fill": [],
        },
        daily_dir=Path("__missing_daily__"),
        risk_gate_shadow_enabled=False,
        risk_gate_shadow_write_errors=False,
    )
    return float(result["state"]["cash"]), int(result["state"]["positions"][0]["quantity"])


def test_dual_basis_columns_conflict_is_rejected_and_order_independent() -> None:
    """T08-BASIS-ALIAS-CONFLICT-20260930：两列口径都必须检查，冲突/未知不得被另一列盖过。

    双列**一致 raw** 仍是正常对照（能模拟）；只要有一列不是 raw、或两列相互矛盾，一律拦下，
    且**列顺序不改变判断**；被拦事件沿真实入口不得改动现金与持仓。
    """

    plan = _plan(PARTIAL_DAY, "sell_all", 10.0, 200)
    plan["sellable_quantity"] = 200

    both_raw = engine.simulate_conditional_plan(
        plan, _dual_basis_frame(("adjust", "raw"), ("price_basis", "raw"))
    )
    assert both_raw["action"] == "stop_loss", "两列一致 raw 是正常对照，仍应能模拟"
    assert both_raw["is_reliable"] is True
    assert both_raw["price_basis"] == "raw"

    conflicts = (
        ("raw", "qfq"),
        ("raw", "unknown"),
        ("raw", ["raw", "qfq"]),
        ("qfq", "raw"),
        ("unknown", "raw"),
    )
    for adjust_value, price_basis_value in conflicts:
        for order in (0, 1):
            columns = (("adjust", adjust_value), ("price_basis", price_basis_value))
            frame = _dual_basis_frame(*(columns if order == 0 else columns[::-1]))
            blocked = engine.simulate_conditional_plan(plan, frame)

            assert blocked["action"] != "stop_loss", (adjust_value, price_basis_value, order)
            assert blocked["is_reliable"] is False
            assert blocked["real_execution_price_claim"] is False
            assert blocked["execution_price_label"] == "same_basis_research_proxy"
            cash, quantity = _blocked_state_cash_and_quantity(blocked)
            assert cash == 8000.0, "被拦的事件不得动现金"
            assert quantity == 200, "被拦的事件不得减仓"


def test_dual_basis_csv_entry_rejects_conflicting_declarations(tmp_path: Path) -> None:
    """双列表头且相互矛盾的 CSV：文件入口同样不得给出可靠成交价。"""

    plan = {
        "plan_id": "p_dual_csv",
        "plan_date": "2026-05-17",
        "trade_date": PARTIAL_DAY,
        "symbols": [_plan(PARTIAL_DAY, "sell_all", 10.0, 200)],
    }
    intraday = tmp_path / "data" / "intraday"
    (intraday / "1m").mkdir(parents=True)
    (intraday / "1m" / f"{SYMBOL}.csv").write_text(
        "symbol,datetime,open,high,low,close,volume,amount,adjust,price_basis\n"
        f"{SYMBOL},{PARTIAL_DAY} 09:31:00,9.5,9.5,9.5,9.5,1000,9500,raw,qfq\n",
        encoding="utf-8",
    )

    out = engine.simulate_conditional_plan_file(plan, intraday_dir=intraday, write=False)

    events = [row for row in (out.get("events") or []) if isinstance(row, dict)]
    blocked = [row for row in (out.get("blocked") or []) if isinstance(row, dict)]
    assert blocked, f"口径冲突应产生被拦行：{out.get('summary')}"
    for row in [*events, *blocked]:
        assert row.get("action") != "stop_loss", row
        assert row.get("is_reliable") is not True, row
    assert any(row.get("trigger_type") == "price_basis_gate" for row in blocked), blocked
import pytest  # noqa: E402  (追加段使用 parametrize)


@pytest.mark.parametrize("reason", ["unknown_price_basis", "mixed_price_basis"])
def test_preflight_error_reason_survives_dual_column_merge(reason: str) -> None:
    """T08-PREFLIGHT-ATTRS-REGRESSION-20260930：预检错误理由不能被列证据冲掉。

    列上写的是 raw，但**预检**已经把这一帧标成 unknown/mixed 时，仍必须拦下并保持账不变；
    重建分类帧合并两列证据时不得丢掉原帧的 ``price_basis_reason``。
    """

    plan = _plan(PARTIAL_DAY, "sell_all", 10.0, 200)
    plan["sellable_quantity"] = 200
    frame = _dual_basis_frame(("adjust", "raw"), ("price_basis", "raw"))
    frame.attrs["observed_price_basis"] = "raw"
    frame.attrs["price_basis_reason"] = reason

    blocked = engine.simulate_conditional_plan(plan, frame)

    assert blocked["action"] != "stop_loss", (reason, blocked)
    assert blocked["is_reliable"] is False
    assert blocked["real_execution_price_claim"] is False
    cash, quantity = _blocked_state_cash_and_quantity(blocked)
    assert cash == 8000.0, "预检错误帧不得动现金"
    assert quantity == 200, "预检错误帧不得减仓"
