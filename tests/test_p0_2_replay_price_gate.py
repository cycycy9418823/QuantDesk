from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantdesk import historical_replay_engine as replay


def _write_daily(path: Path, adjustments: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = len(adjustments)
    pd.DataFrame(
        {
            "date": pd.bdate_range("2026-05-11", periods=count).strftime("%Y-%m-%d"),
            "symbol": [path.stem] * count,
            "open": [10.0 + index * 0.1 for index in range(count)],
            "high": [10.2 + index * 0.1 for index in range(count)],
            "low": [9.8 + index * 0.1 for index in range(count)],
            "close": [10.1 + index * 0.1 for index in range(count)],
            "volume": [100_000 + index for index in range(count)],
            "amount": [1_010_000 + index for index in range(count)],
            "adjust": adjustments,
        }
    ).to_csv(path, index=False, encoding="utf-8-sig")


def _run(tmp_path: Path, symbols: list[str], *, dry_run: bool = True) -> dict:
    return replay.run_historical_intraday_replay(
        "2026-05-15",
        "2026-05-15",
        strategy="long",
        symbols=symbols,
        only_low_price=False,
        dry_run=dry_run,
        daily_dir=tmp_path / "daily",
        intraday_dir=tmp_path / "intraday",
        output_path=tmp_path / "out.json",
        state_path=tmp_path / "state.json",
    )


def test_mixed_daily_basis_blocks_before_plan_and_state_write(tmp_path: Path, monkeypatch) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw", "raw", "qfq", "raw", "raw"])
    calls = {"plan": 0}

    def forbidden_plan(*_args, **_kwargs):
        calls["plan"] += 1
        raise AssertionError("mixed price basis reached replay plan construction")

    monkeypatch.setattr(replay, "build_conditional_trade_plan", forbidden_plan)

    payload = _run(tmp_path, ["000001"], dry_run=False)

    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["daily_results"] == []
    assert payload["summary"]["plan_count"] == 0
    assert payload["price_policy_validation"]["reason_codes"] == ["mixed_price_basis"]
    assert calls["plan"] == 0
    assert not (tmp_path / "out.json").exists()
    assert not (tmp_path / "state.json").exists()


def test_qfq_daily_is_blocked_before_raw_minute_execution_replay(tmp_path: Path) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["qfq"] * 5)

    payload = _run(tmp_path, ["000001"])

    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["price_policy_validation"]["reason_codes"] == [
        "consumer_price_basis_not_allowed"
    ]
    assert payload["price_policy_validation"]["per_symbol"]["000001"]["price_basis"] == "forward_adjusted"


def test_basis_is_classified_before_duplicate_dates_are_removed(tmp_path: Path) -> None:
    path = tmp_path / "daily" / "000001.csv"
    _write_daily(path, ["qfq", "raw", "raw", "raw", "raw"])
    frame = pd.read_csv(path)
    frame.loc[0, "date"] = frame.loc[1, "date"]
    frame.to_csv(path, index=False, encoding="utf-8-sig")

    payload = _run(tmp_path, ["000001"])

    assert payload["status"] == "blocked"
    assert payload["price_policy_validation"]["reason_codes"] == ["mixed_price_basis"]


def test_pure_raw_replay_keeps_existing_plan_execution_path(tmp_path: Path, monkeypatch) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw"] * 5)
    calls = {"plan": 0, "execution": 0}

    def plan(*_args, **_kwargs):
        calls["plan"] += 1
        return {"plan_id": "p0-2-raw", "symbols": [], "warnings": []}

    def execution(*_args, **_kwargs):
        calls["execution"] += 1
        return {"summary": {"event_count": 0, "reliable_event_count": 0}}

    monkeypatch.setattr(replay, "build_conditional_trade_plan", plan)
    monkeypatch.setattr(replay, "simulate_conditional_plan_file", execution)
    monkeypatch.setattr(replay, "apply_conditional_execution_to_state", lambda state, *_args, **_kwargs: {"state": state})

    payload = _run(tmp_path, ["000001"])

    assert payload["ok"] is True
    assert payload["price_basis"] == "raw"
    assert payload["price_policy_validation"]["status"] == "passed"
    assert payload["execution_price_basis"] == "raw"
    assert calls == {"plan": 1, "execution": 1}
