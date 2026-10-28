"""AU-01 regressions exercise the public window and actual fill simulation."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quantdesk import walk_forward as walk
from quantdesk.strategy_registry import StrategyDefinition


def bars(*, basis: str = "raw", volume: float = 1000) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-10-12", "2026-10-13", "2026-10-14"]),
        "symbol": "600000", "open": [10., 10., 11.], "close": [10., 10., 11.],
        "high": [10.1, 10.1, 11.1], "low": [9.9, 9.9, 10.9],
        "volume": volume, "pct_chg": [0., 0., 10.], "adjust": basis,
    })


def signals(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"date": frame["date"].iloc[:2], "signal": ["entry", "exit"]})


def test_future_corporate_action_does_not_change_old_window(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "600000.csv"
    frame = bars()
    frame.to_csv(path, index=False)
    before = walk.load_daily_csv(path, as_of="2026-10-14")
    expected = walk._simulate_trades(before, signals(before), initial_cash=10000, cost_bps=1, slippage_bps=0)
    future = frame.iloc[-1:].copy()
    future["date"] = pd.Timestamp("2026-10-15")
    future[["open", "close", "high", "low"]] *= .5
    future["pct_chg"] = 0
    pd.concat([frame, future]).to_csv(path, index=False)
    after = walk.load_daily_csv(path, as_of="2026-10-14")
    pd.testing.assert_frame_equal(before, after)
    assert walk._simulate_trades(after, signals(after), initial_cash=10000, cost_bps=1, slippage_bps=0) == expected
    monkeypatch.setattr(walk, "_check_data_grade", lambda *_a, **_k: (True, ""))
    strategy = StrategyDefinition(strategy_id="test", name="test", description="test fixture", entry_conditions=[
        {"field": "close", "op": "<=", "value": 10}], exit_conditions=[{"field": "close", "op": ">", "value": 10}])
    window = {"train_start": "20260101", "train_end": "20261011", "test_start": "20261012", "test_end": "20261014"}
    with_future = walk.run_single_window(daily_dir=tmp_path, symbols=["600000"], window=window,
        strategy=strategy, min_test_days=2, min_trades_for_valid=0, deep_validate=False)
    frame.to_csv(path, index=False)
    without_future = walk.run_single_window(daily_dir=tmp_path, symbols=["600000"], window=window,
        strategy=strategy, min_test_days=2, min_trades_for_valid=0, deep_validate=False)
    assert with_future.to_dict() == without_future.to_dict()
    assert with_future.trades == without_future.trades


@pytest.mark.parametrize("bad", ["unknown", "mixed", "cross_basis"])
def test_entire_window_basis_gate_runs_before_any_simulation(tmp_path: Path, monkeypatch, bad: str) -> None:
    bars().to_csv(tmp_path / "600000.csv", index=False)
    second = bars(basis="qfq" if bad == "cross_basis" else "raw")
    if bad == "unknown":
        second = second.drop(columns="adjust")
    elif bad == "mixed":
        second.loc[2, "adjust"] = "qfq"  # a future row still belongs to the original input contract
    second.to_csv(tmp_path / "000001.csv", index=False)
    before = {p: p.read_bytes() for p in tmp_path.glob("*.csv")}
    monkeypatch.setattr(walk, "_simulate_trades", lambda *_a, **_k: pytest.fail("simulation before full basis gate"))
    monkeypatch.setattr(walk, "_check_data_grade", lambda *_a, **_k: (True, ""))
    with pytest.raises(ValueError, match="price_contract"):
        walk.run_single_window(daily_dir=tmp_path, symbols=["600000", "000001"],
            window={"train_start": "20260101", "train_end": "20261011", "test_start": "20261012", "test_end": "20261013"},
            strategy=StrategyDefinition(strategy_id="test", name="test", description="test fixture"), min_test_days=1, deep_validate=False)
    assert before == {p: p.read_bytes() for p in tmp_path.glob("*.csv")}


@pytest.mark.parametrize("basis", ["qfq", "hfq"])
def test_known_adjusted_research_frames_keep_proxy_labels(tmp_path: Path, basis: str) -> None:
    path = tmp_path / "600000.csv"
    bars(basis=basis).to_csv(path, index=False)
    loaded = walk.load_daily_csv(path, as_of="2026-10-14")
    assert loaded.attrs["real_execution_price"] is False
    assert loaded.attrs["execution_price_role"] == "same_basis_research_proxy"
    assert len(walk._simulate_trades(loaded, signals(loaded), cost_bps=0, slippage_bps=0)[0]) == 1


@pytest.mark.parametrize("volume", [0, -1, float("inf"), float("nan")])
def test_nontradable_volume_never_fills(volume: float) -> None:
    frame = bars(volume=volume)
    assert walk._simulate_trades(frame, signals(frame), cost_bps=1, slippage_bps=0)[0] == []


@pytest.mark.parametrize("capital,cap,bps,price", [
    (10000, 1, 1, 10), (10000, 1, 25, 10), (1000, 1, 1, 10),
    (1005, 1, 1, 10), (100000, .95, 1, 10), (10000, .5, 1, 3.333),
])
def test_quantity_and_fees_never_overdraw_cash(capital: float, cap: float, bps: float, price: float) -> None:
    frame = bars()
    frame[["open", "close"]] = price
    frame["high"], frame["low"], frame["pct_chg"] = price * 1.01, price * .99, 0
    trades, equity = walk._simulate_trades(frame, signals(frame), initial_cash=capital,
        max_position_pct=cap, cost_bps=bps, slippage_bps=0)
    if trades:
        trade = trades[0]
        gross = trade.quantity * price
        expected_buy_fee = max(5., round(gross * bps / 10000, 4))
        assert gross + expected_buy_fee <= capital + 1e-8
        assert trade.quantity % 100 == 0
        assert trade.quantity * price <= capital * cap + 1e-8
        assert equity[-1] == pytest.approx(capital + trade.pnl)
    assert min(equity) >= 0


def test_missing_volume_and_invalid_ohlc_cannot_supply_a_fill() -> None:
    frame = bars().drop(columns="volume")
    assert walk._simulate_trades(frame, signals(frame))[0] == []
    frame = bars()
    frame["high"] = 0
    assert walk._simulate_trades(frame, signals(frame))[0] == []


def test_forced_close_respects_t_plus_one_and_final_fee() -> None:
    frame = bars()
    entry = pd.DataFrame({"date": frame["date"].iloc[:1], "signal": ["entry"]})
    trades, equity = walk._simulate_trades(frame, entry, initial_cash=10000, cost_bps=1,
        slippage_bps=0, force_close_at_end=True)
    assert len(trades) == 1 and trades[0].forced_close
    assert equity[-1] == pytest.approx(10000 + trades[0].pnl)
    trades, _ = walk._simulate_trades(frame.iloc[:2], entry, force_close_at_end=True)
    assert trades == []

