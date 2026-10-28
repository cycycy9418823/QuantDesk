"""AU-03 raw-only buy/sell/valuation through the daily_approx entrypoint."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from tests.test_codex_audit_20261020_ledger import TARGET, daily, isolated, state_with_position, update


@pytest.mark.parametrize("basis", ["raw", "qfq", "hfq", "mixed", "unknown"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_daily_approx_execution_uses_only_raw(isolated: Path, monkeypatch, basis: str, side: str) -> None:
    state_with_position(isolated)
    if side == "buy":
        state = paper._empty_state(100000)
        from tests.test_codex_audit_20261020_ledger import dump
        dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, state)
        monkeypatch.setattr(paper, "candidate_rows_from_strategy_payload", lambda *_a, **_k: [
            {"symbol": "600121", "name": "test", "strategy_type": "long", "close": 5.3}])
    daily(isolated, price=5.3 if side == "buy" else 4.95, basis="raw" if basis in {"mixed", "unknown"} else basis)
    path = isolated / "data/daily/600121.csv"
    if basis in {"mixed", "unknown"}:
        frame = pd.read_csv(path)
        if basis == "unknown":
            frame = frame.drop(columns="adjust")
        else:
            frame.loc[0, "adjust"] = "qfq"
        frame.to_csv(path, index=False)
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    result = update()
    fills = [row for row in result["actions"] if row["action"] == side]
    assert len(fills) == (1 if basis == "raw" else 0)
    if basis != "raw":
        assert any("口径" in str(row.get("reason") or row.get("blocked_reason")) for row in result["actions"])
        if side == "sell":
            assert result["positions"][0]["quantity"] == 100
            assert result["positions"][0]["valuation_status"] != "normal"
    assert before == paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()


def test_changed_daily_source_cannot_be_booked_as_a_raw_fill(isolated: Path, monkeypatch) -> None:
    from tests.test_codex_audit_20261020_ledger import dump

    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, paper._empty_state(100000))
    daily(isolated, price=5.3)
    monkeypatch.setattr(paper, "candidate_rows_from_strategy_payload", lambda *_a, **_k: [
        {"symbol": "600121", "strategy_type": "long", "close": 5.3}])
    original_read = paper.read_csv_frame

    def changing_read(path, **kwargs):
        frame = original_read(path, **kwargs)
        if Path(path) == isolated / "data/daily/600121.csv":
            changed = frame.copy()
            changed.loc[changed.index[-1], "close"] += .01
            changed.to_csv(path, index=False)
        return frame

    monkeypatch.setattr(paper, "read_csv_frame", changing_read)
    result = update()
    assert not any(row["action"] == "buy" for row in result["actions"])
    assert any("口径" in str(row.get("reason") or "") for row in result["actions"])

