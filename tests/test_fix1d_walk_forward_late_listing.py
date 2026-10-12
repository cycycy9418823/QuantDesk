"""fix(1d) #1: a sleeve whose first bar arrives mid-window must not count as profit.

``run_single_window`` sums one independent equity sleeve per symbol. Before the
fix the per-symbol series were concatenated and ``.ffill().sum(axis=1)`` was
taken directly: a symbol that only starts trading part-way through the test
window is NaN before its first bar, ``sum`` treats that NaN as 0, and the whole
sleeve's capital is then "earned" on the day its first bar appears. A strategy
that never trades reported a positive annual return and Sharpe.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pandas as pd

from quantdesk import walk_forward
from quantdesk.strategy_registry import StrategyDefinition


def _write_flat_daily(path: Path, symbol: str, dates: pd.DatetimeIndex) -> None:
    frame = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "volume": 100000,
            "amount": 1_000_000.0,
            "pct_chg": 0.0,
            "adjust": "raw",
        }
    )
    frame.to_csv(path, index=False)


def _never_trading_strategy() -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id="never_trades",
        name="never trades",
        description="entry condition can never be met",
        entry_conditions=[{"field": "close", "op": ">", "value": 1e12}],
        exit_conditions=[],
    )


def test_late_first_bar_does_not_create_phantom_return(tmp_path: Path) -> None:
    full_year = pd.bdate_range("2021-01-04", "2021-12-31")
    late_half = full_year[full_year >= pd.Timestamp("2021-07-01")]
    _write_flat_daily(tmp_path / "600000.csv", "600000", full_year)
    _write_flat_daily(tmp_path / "600001.csv", "600001", late_half)

    window = {
        "train_start": "20180101",
        "train_end": "20201231",
        "test_start": "20210101",
        "test_end": "20211231",
    }
    with mock.patch.object(walk_forward, "_check_data_grade", return_value=(True, "")):
        result = walk_forward.run_single_window(
            daily_dir=tmp_path,
            symbols=["600000", "600001"],
            window=window,
            strategy=_never_trading_strategy(),
            cost_bps=0.0,
            slippage_bps=0.0,
            initial_cash=100000.0,
            bootstrap_iterations=10,
            deep_validate=False,
        )

    assert result.symbols_count == 2
    assert result.trades_count == 0
    # Nothing traded and every price is flat: the pooled book cannot have grown.
    assert abs(result.annual_return) < 1e-12, result.annual_return
    assert result.sharpe <= 0.0, result.sharpe
    assert result.max_drawdown == 0.0


def test_combined_equity_pads_each_sleeve_with_its_own_capital() -> None:
    dates = pd.to_datetime(["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"])
    early = pd.Series([50000.0, 50000.0, 50500.0, 50500.0], index=dates)
    late = pd.Series([50000.0, 49000.0], index=dates[2:])

    combined = walk_forward._combine_sleeve_equity([early, late], [50000.0, 50000.0])

    assert combined.index.tolist() == dates.tolist()
    # Before its first bar the late sleeve is idle cash (50000), not zero.
    assert combined.tolist() == [100000.0, 100000.0, 100500.0, 99500.0]
