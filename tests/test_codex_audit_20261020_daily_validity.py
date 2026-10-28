"""Reject nonfinite prices at paper execution and valuation consumers."""
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from tests.test_codex_audit_20261020_ledger import TARGET, daily, isolated, state_with_position, update, dump


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 0])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_actual_daily_approx_rejects_nonfinite_fill_and_keeps_verified_mark(isolated: Path, monkeypatch, field: str, value: float, side: str) -> None:
    state_with_position(isolated)
    if side == "buy":
        dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, paper._empty_state(100000))
        monkeypatch.setattr(paper, "candidate_rows_from_strategy_payload", lambda *_a, **_k: [
            {"symbol": "600121", "strategy_type": "long", "close": 5.3}])
    daily(isolated, price=5.3 if side == "buy" else 4.95)
    path = isolated / "data/daily/600121.csv"
    frame = pd.read_csv(path)
    frame.loc[frame.index[-1], field] = value
    frame.to_csv(path, index=False)
    original = path.read_bytes()
    result = update()
    assert not any(row["action"] == side for row in result["actions"])
    assert path.read_bytes() == original
    if side == "sell":
        assert result["positions"][0]["quantity"] == 100
        assert result["positions"][0]["valuation_status"] != "normal"
        assert result["positions"][0]["latest_price"] == 5.86


def test_filter_preserves_rows_and_attributes_and_reports_exact_impact() -> None:
    from quantdesk.data_truth.daily_bar_validity import filter_invalid_daily_bars
    original = pd.DataFrame([dict(date="2026-10-16", open=10, high=10, low=10, close=10, volume=100),
        dict(date="2026-10-19", open=10, high=float("inf"), low=10, close=10, volume=100)])
    original.attrs["source_sha256"] = "a" * 64
    preserved = original.copy(deep=True)
    result, reasons = filter_invalid_daily_bars(original, symbol="600000")
    pd.testing.assert_frame_equal(original, preserved)
    assert len(result) == 1 and len(original) == 2
    assert result.attrs == original.attrs and any("high_not_numeric" in r for r in reasons)
