"""Batch 1A-C: historical strategy-book fills at a limit price are labelled, never re-priced."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from quantdesk.paper.limit_fill_audit import annotate_state, audit_fills, main


def _daily(tmp_path: Path) -> Path:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600001.csv").write_text(
        "date,symbol,open,high,low,close,volume,change,pct_chg,adjust\n"
        "2026-09-18,600001,1.03,1.05,1.02,1.04,1000,0.01,0.97,raw\n"
        "2026-09-21,600001,1.08,1.14,1.08,1.14,1000,0.10,9.615,raw\n"
        "2026-09-22,600001,1.10,1.12,1.03,1.03,1000,-0.11,-9.649,raw\n",
        encoding="utf-8",
    )
    (daily / "600002.csv").write_text(
        "date,symbol,open,high,low,close,volume,change,pct_chg,adjust\n"
        "2026-09-18,600002,10.0,10.1,9.9,10.0,1000,0.0,0.0,raw\n"
        "2026-09-21,600002,10.0,10.6,9.9,10.5,1000,0.5,5.0,raw\n",
        encoding="utf-8",
    )
    return daily


def _state() -> dict:
    return {
        "cash": 1000.0,
        "positions": [{"symbol": "600002", "name": "样本二", "entry_date": "2026-09-21",
                       "execution_price": 10.5, "quantity": 100}],
        "closed_positions": [{"symbol": "600001", "name": "样本一", "entry_date": "2026-09-21",
                              "execution_price": 1.14, "exit_date": "2026-09-22", "exit_price": 1.03,
                              "quantity": 100, "realized_pnl": -11.0}],
    }


def test_low_priced_limit_up_buy_is_flagged_and_normal_fill_is_not(tmp_path: Path) -> None:
    audit = audit_fills(_state(), daily_dir=_daily(tmp_path))
    flagged = {(row["symbol"], row["kind"]) for row in audit["flagged"]}
    assert ("600001", "entry") in flagged          # 1.14 = limit-up of 1.04 (+9.615%)
    assert ("600001", "exit") in flagged           # 1.03 = limit-down of 1.14
    assert ("600002", "entry") not in flagged      # +5% is not a main-board limit
    assert audit["flagged_buy_at_limit_up"] == 1 and audit["flagged_sell_at_limit_down"] == 1


def test_annotation_only_appends_labels(tmp_path: Path) -> None:
    state = _state()
    original = copy.deepcopy(state)
    audit = audit_fills(state, daily_dir=_daily(tmp_path))
    labelled, added = annotate_state(state, audit)
    assert added == 2
    row = labelled["closed_positions"][0]
    assert row["fill_audit"]["entry"]["status"] == "filled_at_limit_up"
    assert row["fill_audit"]["exit"]["up_limit"] == 1.25 and row["fill_audit"]["exit"]["down_limit"] == 1.03
    for key, value in original["closed_positions"][0].items():
        assert row[key] == value
    assert labelled["cash"] == original["cash"]
    _again, added_again = annotate_state(labelled, audit)
    assert added_again == 0


def test_cli_is_dry_unless_apply(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()), encoding="utf-8")
    before = state_path.read_bytes()
    args = ["--state", str(state_path), "--daily-dir", str(_daily(tmp_path)),
            "--snapshots-dir", str(tmp_path / "none"), "--st-history", str(tmp_path / "none.csv"),
            "--out-dir", str(tmp_path / "out")]
    assert main(args) == 0
    assert state_path.read_bytes() == before
    assert list((tmp_path / "out").glob("strategy_limit_fill_audit_*.md"))
    assert main(args + ["--apply"]) == 0
    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["limit_fill_audit"]["flagged_count"] == 2
