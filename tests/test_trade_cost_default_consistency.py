from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace

from quantdesk.paper.trade_costs import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STAMP_TAX_RATE,
)
from quantdesk.research.strategy_viability_runner import CostConfig
from scripts import run_paper_trading_simulator
from scripts import run_strategy_screening_funnel
from scripts.server.routes import strategy_screening


def test_strategy_screening_cli_defaults_use_unified_trade_costs() -> None:
    args = run_strategy_screening_funnel.parse_args([])

    assert args.commission_rate == DEFAULT_COMMISSION_RATE
    assert args.stamp_tax_rate == DEFAULT_STAMP_TAX_RATE
    assert args.slippage_bps == DEFAULT_SLIPPAGE_BPS


def test_paper_trading_simulator_cli_defaults_use_unified_trade_costs() -> None:
    parser = run_paper_trading_simulator.build_arg_parser()
    args = parser.parse_args([])

    assert args.commission_rate == DEFAULT_COMMISSION_RATE
    assert args.stamp_tax_rate_sell == DEFAULT_STAMP_TAX_RATE
    assert args.slippage_rate == DEFAULT_SLIPPAGE_BPS / 10_000


def test_strategy_screening_route_defaults_use_unified_trade_costs(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_run_screening(args):
        captured["args"] = args
        return {"ok": True, "status": "completed", "performance": {}}

    def fake_write_outputs(payload, output_dir, report_dir):
        return {
            **payload,
            "ok": True,
            "status": "completed",
            "performance": {},
            "output_paths": {},
        }

    monkeypatch.setattr(run_strategy_screening_funnel, "run_screening", fake_run_screening)
    monkeypatch.setattr(run_strategy_screening_funnel, "write_outputs", fake_write_outputs)

    lock = threading.Lock()
    state = {}
    deps = strategy_screening.StrategyScreeningRouteDeps(
        project_root=lambda: tmp_path,
        task_json=lambda: tmp_path / "state" / "strategy_screening_task.json",
        state=lambda: state,
        state_lock=lambda: contextlib.nullcontext(),
        data_write_lock=lambda: lock,
        read_json_file=lambda path: {},
        request_truthy=lambda value: bool(value),
        make_request_id=lambda: "unit-test",
        start_worker=lambda name, payload: None,
    )

    strategy_screening.run_strategy_screening_worker(deps, "unit-test", {})

    args = captured["args"]
    assert args.commission_rate == DEFAULT_COMMISSION_RATE
    assert args.stamp_tax_rate == DEFAULT_STAMP_TAX_RATE
    assert args.slippage_bps == DEFAULT_SLIPPAGE_BPS


def test_strategy_screening_route_preserves_explicit_zero_trade_costs(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_run_screening(args):
        captured["args"] = args
        return {"ok": True, "status": "completed", "performance": {}}

    def fake_write_outputs(payload, output_dir, report_dir):
        return {
            **payload,
            "ok": True,
            "status": "completed",
            "performance": {},
            "output_paths": {},
        }

    monkeypatch.setattr(run_strategy_screening_funnel, "run_screening", fake_run_screening)
    monkeypatch.setattr(run_strategy_screening_funnel, "write_outputs", fake_write_outputs)

    lock = threading.Lock()
    state = {}
    deps = strategy_screening.StrategyScreeningRouteDeps(
        project_root=lambda: tmp_path,
        task_json=lambda: tmp_path / "state" / "strategy_screening_task.json",
        state=lambda: state,
        state_lock=lambda: contextlib.nullcontext(),
        data_write_lock=lambda: lock,
        read_json_file=lambda path: {},
        request_truthy=lambda value: bool(value),
        make_request_id=lambda: "unit-test",
        start_worker=lambda name, payload: None,
    )

    strategy_screening.run_strategy_screening_worker(
        deps,
        "unit-test",
        {"commission_rate": 0, "stamp_tax_rate": 0, "slippage_bps": 0},
    )

    args = captured["args"]
    assert args.commission_rate == 0
    assert args.stamp_tax_rate == 0
    assert args.slippage_bps == 0


def test_strategy_viability_cost_config_defaults_use_unified_trade_costs() -> None:
    cost = CostConfig()

    assert cost.commission_bps == DEFAULT_COMMISSION_RATE * 10_000
    assert cost.stamp_tax_bps == DEFAULT_STAMP_TAX_RATE * 10_000
    assert cost.slippage_bps == DEFAULT_SLIPPAGE_BPS
