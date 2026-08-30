"""Tests for corporate-action repair of raw daily bars.

The load-bearing guarantees, in order of how much damage getting them wrong does:
1. the newest bar keeps its actually-quoted price (execution/valuation safety),
2. adjusted steps reproduce the provider's stated returns,
3. ordinary rounding noise never accumulates into a drift,
4. an unverifiable cliff is refused, not silently measured.
"""

from __future__ import annotations

import pandas as pd

from quantdesk.corporate_action_repair import (
    RAW_CLOSE_COLUMN,
    repair_corporate_actions,
)


def _frame(rows: list[tuple[str, float, float | None]]) -> pd.DataFrame:
    """(date, close, pct_chg) -> a frame shaped like data/daily files."""

    return pd.DataFrame(
        {
            "date": [pd.Timestamp(d) for d, _c, _p in rows],
            "open": [c for _d, c, _p in rows],
            "high": [c for _d, c, _p in rows],
            "low": [c for _d, c, _p in rows],
            "close": [c for _d, c, _p in rows],
            "pct_chg": [p for _d, _c, p in rows],
        }
    )


def test_repairs_split_cliff_and_reproduces_stated_returns() -> None:
    # The real 300259 2018-05-23 shape: price looks -58%, truth is -7.31%.
    frame = _frame([
        ("2018-05-21", 10.87, 1.0),
        ("2018-05-22", 10.64, -2.12),
        ("2018-05-23", 4.44, -7.31),
        ("2018-05-24", 4.50, 1.35),
    ])
    out, info = repair_corporate_actions(frame)
    assert info["ca_bars"] == 1
    assert info["unverifiable"] is False
    # The cliff bar and everything after keep their quoted prices.
    assert out["close"].iloc[2] == 4.44
    assert out["close"].iloc[3] == 4.50
    # History is restated so the step reproduces the stated -7.31%.
    step = out["close"].iloc[2] / out["close"].iloc[1] - 1.0
    assert abs(step - (-0.0731)) < 1e-6
    # The pre-cliff price is scaled down, not left at its unadjusted level.
    assert out["close"].iloc[1] < 5.0


def test_newest_bar_is_never_restated() -> None:
    """Execution, sizing, and mark-to-market all read the latest bar."""

    frame = _frame([
        ("2020-01-02", 40.0, 0.5),
        ("2020-01-03", 20.0, -1.0),   # 2-for-1 split
        ("2020-01-06", 20.5, 2.5),
    ])
    out, _info = repair_corporate_actions(frame)
    for column in ("open", "high", "low", "close"):
        assert out[column].iloc[-1] == frame[column].iloc[-1]


def test_raw_close_is_preserved_for_tradability_checks() -> None:
    frame = _frame([
        ("2021-06-01", 30.0, 0.2),
        ("2021-06-02", 3.0, -1.5),    # 10-for-1
        ("2021-06-03", 3.1, 3.3),
    ])
    out, info = repair_corporate_actions(frame)
    assert info["ca_bars"] == 1
    # The restated history is far below the price that was actually quoted, so a
    # penny-stock / tick-size gate must read raw_close instead.
    assert out[RAW_CLOSE_COLUMN].iloc[0] == 30.0
    assert out["close"].iloc[0] < 5.0


def test_rounding_noise_does_not_accumulate() -> None:
    """Sub-threshold disagreement must leave prices untouched."""

    rows = [("2022-01-03", 10.0, None)]
    price = 10.0
    days = pd.date_range("2022-01-04", periods=39, freq="B")
    for day in days:
        price = round(price * 1.001, 2)
        rows.append((day.strftime("%Y-%m-%d"), price, 0.1))  # stated 0.1% vs ~0.1% actual
    out, info = repair_corporate_actions(_frame(rows))
    assert info["ca_bars"] == 0
    pd.testing.assert_series_equal(
        out["close"], _frame(rows)["close"], check_names=False
    )


def test_unverifiable_large_move_is_refused() -> None:
    """A -50% bar with no pct_chg cannot be repaired, so the symbol is refused."""

    frame = _frame([
        ("2019-03-01", 20.0, 1.0),
        ("2019-03-04", 10.0, None),
        ("2019-03-05", 10.1, 1.0),
    ])
    out, info = repair_corporate_actions(frame)
    assert info["unverifiable"] is True
    assert info["reason"] == "unverifiable_large_move"
    # Refused, not silently rescaled.
    assert out["close"].iloc[0] == 20.0


def test_missing_pct_chg_column_warns_but_does_not_refuse() -> None:
    """No ground-truth column at all is not evidence of a corporate action.

    Refusing here would reject every data source lacking pct_chg — synthetic
    fixtures, and first-day listings where a large move is legitimate. Fail-closed
    is reserved for the case where the column exists and contradicts the prices.
    """

    frame = _frame([
        ("2019-03-01", 20.0, None),
        ("2019-03-04", 10.0, None),
        ("2019-03-05", 10.1, None),
    ]).drop(columns=["pct_chg"])
    out, info = repair_corporate_actions(frame)
    assert info["unverifiable"] is False
    assert info.get("unverified_large_move") is True
    assert info["ca_bars"] == 0
    # Prices pass through untouched — nothing was verified, so nothing is invented.
    assert out["close"].tolist() == [20.0, 10.0, 10.1]


def test_clean_frame_passes_through_unchanged() -> None:
    frame = _frame([
        ("2023-05-04", 12.0, 1.0),
        ("2023-05-05", 12.12, 1.0),
        ("2023-05-08", 12.24, 0.99),
    ])
    out, info = repair_corporate_actions(frame)
    assert info["ca_bars"] == 0
    assert info["unverifiable"] is False
    pd.testing.assert_series_equal(out["close"], frame["close"], check_names=False)


def test_empty_and_single_row_are_safe() -> None:
    out, info = repair_corporate_actions(pd.DataFrame())
    assert info["unverifiable"] is True
    assert out.empty
    single = _frame([("2024-01-02", 9.9, 1.0)])
    out, info = repair_corporate_actions(single)
    assert info["ca_bars"] == 0
    assert out["close"].iloc[0] == 9.9
    assert RAW_CLOSE_COLUMN in out.columns


def test_input_frame_is_not_mutated() -> None:
    frame = _frame([
        ("2020-07-01", 50.0, 0.5),
        ("2020-07-02", 25.0, -1.0),
        ("2020-07-03", 25.5, 2.0),
    ])
    before = frame["close"].tolist()
    repair_corporate_actions(frame)
    assert frame["close"].tolist() == before
    assert RAW_CLOSE_COLUMN not in frame.columns
