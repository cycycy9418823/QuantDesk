"""The AI trader's dual-track price boundary.

Indicators must see corporate-action-repaired prices, because ``researcher.py``
runs ``pct_change`` / ``rolling`` over the close panel and an unadjusted split
cliff would read as a genuine crash. Execution must see the price as actually
quoted, because the ledger records real fills. These tests pin both halves: if
either drifts, the trader either ranks on fake crashes or books fake prices.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ai_trader.market import load_close_panel, load_ohlc_row, load_symbol_frame

SYMBOL = "600001"
# A 10-for-1 split on 2024-03-04: the price column falls 90%, pct_chg says -1.0%.
ROWS = [
    ("2024-02-26", 100.0, 1.0),
    ("2024-02-27", 101.0, 1.0),
    ("2024-02-28", 102.0, 0.99),
    ("2024-02-29", 103.0, 0.98),
    ("2024-03-01", 104.0, 0.97),
    ("2024-03-04", 10.3, -1.0),
    ("2024-03-05", 10.4, 0.97),
    ("2024-03-06", 10.5, 0.96),
]


def _write_daily(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "date": [d for d, _c, _p in ROWS],
            "symbol": [SYMBOL] * len(ROWS),
            "open": [c for _d, c, _p in ROWS],
            "high": [c for _d, c, _p in ROWS],
            "low": [c for _d, c, _p in ROWS],
            "close": [c for _d, c, _p in ROWS],
            "volume": [1_000_000] * len(ROWS),
            "amount": [50_000_000] * len(ROWS),
            "pct_chg": [p for _d, _c, p in ROWS],
            "adjust": ["raw"] * len(ROWS),
        }
    )
    frame.to_csv(root / f"{SYMBOL}.csv", index=False, encoding="utf-8")


def test_close_panel_is_corporate_action_repaired(tmp_path: Path) -> None:
    """Ranking/momentum must not see the split as a -90% crash."""

    daily = tmp_path / "daily"
    _write_daily(daily)
    panel, _pct, warnings = load_close_panel([SYMBOL], daily, lookback=0)
    series = panel[SYMBOL]
    step = series.loc["2024-03-04"] / series.loc["2024-03-01"] - 1.0
    assert abs(step - (-0.01)) < 1e-6, "split still reads as a crash in the panel"
    # Pre-split history is restated down onto the post-split scale.
    assert series.loc["2024-02-26"] < 15.0
    assert any("corporate_actions_repaired" in str(w) for w in warnings)


def test_momentum_over_a_split_is_not_a_fake_crash(tmp_path: Path) -> None:
    """The exact failure mode: pct_change across the cliff drove the ranking."""

    daily = tmp_path / "daily"
    _write_daily(daily)
    panel, _pct, _warnings = load_close_panel([SYMBOL], daily, lookback=0)
    momentum = panel[SYMBOL].pct_change(5).iloc[-1]
    # Raw bars would give roughly -90%; the true 5-bar move is a few percent.
    assert momentum > -0.20, f"momentum still poisoned by the split: {momentum}"


def test_ohlc_row_keeps_the_actually_quoted_price(tmp_path: Path) -> None:
    """Fills, sizing and mark-to-market must use real quoted prices."""

    daily = tmp_path / "daily"
    _write_daily(daily)
    pre_split = load_ohlc_row(SYMBOL, "2024-02-26", daily)
    assert pre_split["close"] == 100.0, "execution price was restated"
    assert pre_split["open"] == 100.0
    # And the newest bar is identical on both tracks, since the repair is
    # anchored to it.
    latest_raw = load_ohlc_row(SYMBOL, "2024-03-06", daily)
    panel, _pct, _w = load_close_panel([SYMBOL], daily, lookback=0)
    assert latest_raw["close"] == panel[SYMBOL].loc["2024-03-06"] == 10.5


def test_repair_is_opt_in_on_the_shared_loader(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    _write_daily(daily)
    raw = load_symbol_frame(SYMBOL, daily, lookback=0)
    fixed = load_symbol_frame(SYMBOL, daily, lookback=0, adjust_corporate_actions=True)
    assert float(raw["close"].iloc[0]) == 100.0
    assert float(fixed["close"].iloc[0]) < 15.0
    # Both agree on the newest bar.
    assert float(raw["close"].iloc[-1]) == float(fixed["close"].iloc[-1]) == 10.5


def test_unverifiable_cliff_is_refused_not_ranked(tmp_path: Path) -> None:
    """A big move pct_chg cannot corroborate must drop out of the panel."""

    daily = tmp_path / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "date": ["2024-05-06", "2024-05-07", "2024-05-08"],
            "symbol": ["600002"] * 3,
            "open": [40.0, 20.0, 20.1],
            "high": [40.0, 20.0, 20.1],
            "low": [40.0, 20.0, 20.1],
            "close": [40.0, 20.0, 20.1],
            "pct_chg": [1.0, None, 0.5],
            "adjust": ["raw"] * 3,
        }
    )
    frame.to_csv(daily / "600002.csv", index=False, encoding="utf-8")
    panel, _pct, warnings = load_close_panel(["600002"], daily, lookback=0)
    assert "600002" not in panel.columns
    assert any("unverifiable_corporate_action" in str(w) for w in warnings)
