"""Regression: walk-forward limit checks must use the bar's trading date."""

from __future__ import annotations

import pandas as pd

from quantdesk.board_rules import get_limit_pcts, is_limit_down, is_limit_up
from quantdesk.walk_forward import _simulate_trades


def test_board_limit_pcts_are_date_aware() -> None:
    assert get_limit_pcts("300001", trade_date="2019-06-04") == (10.0, -10.0)
    assert get_limit_pcts("300001") == (20.0, -20.0)
    assert is_limit_up(change_pct=0.10, symbol="300001", trade_date="2019-06-04") is True
    assert is_limit_up(change_pct=0.10, symbol="300001") is False
    assert is_limit_down(change_pct=-0.10, symbol="300001", trade_date="2019-06-04") is True


def test_walk_forward_blocks_pre_reform_chinext_one_word_limit_up() -> None:
    dates = pd.to_datetime(["2019-06-03", "2019-06-04", "2019-06-05", "2019-06-06"])
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, 11.0, 11.2, 11.3],
            "high": [10.0, 11.0, 11.3, 11.4],
            "low": [10.0, 11.0, 11.1, 11.2],
            "close": [10.0, 11.0, 11.25, 11.35],
            "volume": [1_000_000] * 4,
        }
    )
    signals = pd.DataFrame({"date": [dates[0], dates[1]], "signal": ["entry", "exit"]})
    trades, _equity = _simulate_trades(
        frame, signals, cost_bps=0.0, slippage_bps=0.0,
        symbol="300001", strategy_name="probe",
    )
    assert trades == []


def test_walk_forward_still_blocks_post_reform_one_word_limit_up() -> None:
    dates = pd.to_datetime(["2021-06-03", "2021-06-04", "2021-06-07", "2021-06-08"])
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, 12.0, 12.2, 12.3],
            "high": [10.0, 12.0, 12.3, 12.4],
            "low": [10.0, 12.0, 12.1, 12.2],
            "close": [10.0, 12.0, 12.25, 12.35],
            "volume": [1_000_000] * 4,
        }
    )
    signals = pd.DataFrame({"date": [dates[0], dates[1]], "signal": ["entry", "exit"]})
    trades, _equity = _simulate_trades(
        frame, signals, cost_bps=0.0, slippage_bps=0.0,
        symbol="300001", strategy_name="probe",
    )
    assert trades == []


def test_walk_forward_does_not_block_ordinary_pre_reform_bar() -> None:
    dates = pd.to_datetime(["2019-06-03", "2019-06-04", "2019-06-05"])
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, 10.1, 10.2],
            "high": [10.1, 10.2, 10.3],
            "low": [9.9, 10.0, 10.1],
            "close": [10.0, 10.1, 10.2],
            "volume": [1_000_000] * 3,
        }
    )
    signals = pd.DataFrame({"date": [dates[0], dates[1]], "signal": ["entry", "exit"]})
    trades, _equity = _simulate_trades(
        frame, signals, cost_bps=0.0, slippage_bps=0.0,
        symbol="300001", strategy_name="probe",
    )
    assert [(trade.entry_date, trade.entry_price) for trade in trades] == [
        ("2019-06-04", 10.1)
    ]
