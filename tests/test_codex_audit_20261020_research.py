"""AU-11 original-source gate and as-of boundary through AI research readers."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ai_trader import market, researcher
from ai_trader.hypotheses import empty_book


def write_prices(root: Path, symbol: str, *, basis: str = "raw", count: int = 140) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=count)
    close = [10 + index * .03 for index in range(count)]
    frame = pd.DataFrame({"date": dates, "symbol": symbol, "open": close, "close": close,
                          "high": [x + .1 for x in close], "low": [x - .1 for x in close],
                          "volume": 1000, "adjust": basis})
    root.mkdir(parents=True, exist_ok=True)
    frame.to_csv(root / f"{symbol}.csv", index=False)
    return frame


def test_real_market_and_research_chain_ignore_future_rows(tmp_path: Path) -> None:
    frame = write_prices(tmp_path, "600000", count=90)
    as_of = str(frame["date"].iloc[-1].date())
    before, _, _ = market.load_close_panel(["600000"], tmp_path, lookback=0, as_of=as_of)
    result_before = researcher.research_ideas(before, empty_book(), as_of=as_of)
    extended = write_prices(tmp_path, "600000", count=140)
    extended.loc[extended["date"] > as_of, ["close", "open", "high", "low"]] *= .5
    extended["pct_chg"] = extended["close"].pct_change().fillna(0) * 100
    extended.loc[extended["date"] > as_of, "pct_chg"] = .1
    extended.to_csv(tmp_path / "600000.csv", index=False)
    after, _, _ = market.load_close_panel(["600000"], tmp_path, lookback=0, as_of=as_of)
    pd.testing.assert_frame_equal(before, after)
    result_after = researcher.research_ideas(after, empty_book(), as_of=as_of)
    assert result_before["evaluated"] == result_after["evaluated"]
    assert result_after["input_evidence"]["observed_end"] == as_of
    full, _, _ = market.load_close_panel(["600000"], tmp_path, lookback=0)
    # research_ideas also enforces the cutoff when the caller passed a longer panel.
    short_full = full.loc[full.index <= as_of].copy()
    assert researcher.research_ideas(full, empty_book(), as_of=as_of)["evaluated"] == researcher.research_ideas(short_full, empty_book(), as_of=as_of)["evaluated"]


@pytest.mark.parametrize("bad", ["unknown", "mixed", "cross_basis"])
def test_bad_original_basis_blocks_complete_panel(tmp_path: Path, bad: str) -> None:
    write_prices(tmp_path, "600000")
    frame = write_prices(tmp_path, "000001", basis="qfq" if bad == "cross_basis" else "raw")
    if bad == "unknown":
        frame = frame.drop(columns="adjust")
    elif bad == "mixed":
        frame.loc[len(frame) - 1, "adjust"] = "qfq"
    frame.to_csv(tmp_path / "000001.csv", index=False)
    before = {p: p.read_bytes() for p in tmp_path.glob("*.csv")}
    panel, _, warnings = market.load_close_panel(["600000", "000001"], tmp_path,
        as_of="2026-05-08", lookback=0)
    assert panel.empty
    assert panel.attrs["price_policy_validation"]["eligible"] is False
    assert warnings
    result = researcher.research_ideas(panel, empty_book(), as_of="2026-05-08")
    assert result["status"] == "blocked" and result["live"] is None
    assert all(row["holdout_mean_net"] is None for row in result["evaluated"])
    assert before == {p: p.read_bytes() for p in tmp_path.glob("*.csv")}


def test_future_only_data_does_not_roll_decision_into_future(tmp_path: Path) -> None:
    write_prices(tmp_path, "600000")
    panel, _, _ = market.load_close_panel(["600000"], tmp_path, as_of="2025-12-31")
    result = researcher.research_ideas(panel, empty_book(), as_of="2025-12-31")
    assert panel.empty and result["live"] is None
    assert result["input_evidence"]["observed_end"] is None


def test_unknown_manual_panel_has_no_formal_metrics() -> None:
    panel = pd.DataFrame({"600000": range(10, 130)}, index=pd.bdate_range("2026-01-02", periods=120).strftime("%Y-%m-%d"))
    result = researcher.research_ideas(panel, empty_book(), as_of=str(panel.index[-1]))
    assert result["status"] == "blocked" and result["live"] is None
    assert all(item["holdout_mean_net"] is None for item in result["evaluated"])


def test_changed_source_is_not_a_verified_research_panel(tmp_path: Path, monkeypatch) -> None:
    write_prices(tmp_path, "600000")
    original = market.source_reference
    monkeypatch.setattr(market, "source_reference", lambda before, after: {
        **original(before, after), "status": "unverified", "reason": "source_changed_or_missing_during_read"})
    panel, _, warnings = market.load_close_panel(["600000"], tmp_path, as_of="2026-05-08")
    assert panel.empty and warnings
    assert researcher.research_ideas(panel, empty_book(), as_of="2026-05-08")["live"] is None


def test_repaired_prices_are_labelled_as_research_proxy(tmp_path: Path) -> None:
    frame = write_prices(tmp_path, "600000", count=90)
    frame["pct_chg"] = frame["close"].pct_change().fillna(0) * 100
    frame.loc[80:, ["open", "high", "low", "close"]] *= .5
    frame.loc[80, "pct_chg"] = .1
    frame.to_csv(tmp_path / "600000.csv", index=False)
    prepared = market.load_symbol_frame("600000", tmp_path, lookback=0, adjust_corporate_actions=True)
    assert prepared.attrs["original_price_basis"] == "raw"
    assert prepared.attrs["price_basis"] == "forward_adjusted"
    assert set(prepared["adjust"]) == {"qfq"}
    assert prepared.attrs["calculation_basis"] == "corporate_action_repaired_research_proxy"

