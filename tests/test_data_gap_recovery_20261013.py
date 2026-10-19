"""Regression cases for retaining, then verifying, same-day data gaps."""

from pathlib import Path

import pandas as pd
import pytest

from quantdesk.session_expectation import build_session_expectation


DAY = "2026-10-13"


def _classify(tmp_path: Path, rows: list[dict]) -> dict:
    return build_session_expectation(
        ["000016"], DAY, project_root=tmp_path, network=True,
        provider=lambda **_kwargs: pd.DataFrame(rows),
    )


def test_provider_code_alone_does_not_hide_a_missing_daily_bar(tmp_path: Path) -> None:
    result = _classify(tmp_path, [{"代码": "000016"}])
    assert result["per_symbol"]["000016"]["classification"] == "unknown"
    assert result["legitimate_no_session_symbols"] == []
    assert result["source_evidence"]["provider"]["matched_count"] == 1
    assert result["source_evidence"]["provider"]["verified_count"] == 0


@pytest.mark.parametrize("end", ["2026-10-13 10:00:00", "2026-10-13 14:59:59"])
def test_intraday_halt_cannot_be_a_full_day_exception(tmp_path: Path, end: str) -> None:
    result = _classify(tmp_path, [{
        "代码": "000016", "停牌时间": "2026-10-13 09:30:00",
        "停牌截止时间": end, "停牌期限": "盘中停牌",
    }])
    assert result["per_symbol"]["000016"]["classification"] == "unknown"
    assert result["legitimate_no_session_symbols"] == []


def test_full_session_interval_is_used_with_its_original_evidence(tmp_path: Path) -> None:
    result = _classify(tmp_path, [{
        "代码": "000016", "停牌时间": "2026-10-13 09:15:00",
        "停牌截止时间": "2026-10-13 15:00:00",
    }])
    assert result["legitimate_no_session_symbols"] == ["000016"]
    evidence = result["source_evidence"]["provider"]
    assert evidence["verified_count"] == 1
    records = evidence["records_by_symbol"]["000016"]
    assert records[0]["停牌截止时间"] == "2026-10-13 15:00:00"
    assert len(evidence["sha256"]) == 64


@pytest.mark.parametrize("updates", [
    {"停牌时间": "2026-10-13 09:31:00"},
    {"停牌时间": "2026-10-14 09:30:00", "停牌截止时间": "2026-10-14 15:00:00"},
    {"停牌截止时间": ""},
    {"停牌时间": "bad-date"},
    {"停牌截止时间": "bad-date"},
    {"停牌截止时间": "2026-10-13 15:00:00+00:00", "停牌时间": "2026-10-13 09:30:00+00:00"},
])
def test_incomplete_wrong_day_or_late_start_keeps_gap_pending(tmp_path: Path, updates: dict) -> None:
    row = {"代码": "000016", "停牌时间": "2026-10-13 09:15:00", "停牌截止时间": "2026-10-13 15:00:00"}
    row.update(updates)
    result = _classify(tmp_path, [row])
    assert result["legitimate_no_session_symbols"] == []


def test_planned_resumption_is_not_actual_end_evidence(tmp_path: Path) -> None:
    result = _classify(tmp_path, [{
        "代码": "000016", "停牌时间": "2026-10-13 09:30:00",
        "预计复牌时间": "2026-10-14 09:30:00",
    }])
    assert result["legitimate_no_session_symbols"] == []


def test_conflicting_provider_rows_do_not_pick_the_convenient_row(tmp_path: Path) -> None:
    result = _classify(tmp_path, [
        {"代码": "000016", "停牌时间": "2026-10-13 09:15:00", "停牌截止时间": "2026-10-13 15:00:00"},
        {"代码": "000016", "停牌时间": "2026-10-13 09:15:00", "停牌截止时间": "2026-10-13 10:00:00"},
    ])
    assert result["legitimate_no_session_symbols"] == []


def test_target_date_full_day_label_can_supply_explicit_session_evidence(tmp_path: Path) -> None:
    result = _classify(tmp_path, [{
        "代码": "000016", "停牌时间": "2026-10-13 09:30:00", "停牌期限": "停牌一天",
    }])
    assert result["legitimate_no_session_symbols"] == ["000016"]


def test_date_only_sdk_output_needs_a_full_day_label(tmp_path: Path) -> None:
    row = {"代码": "000016", "停牌时间": "2026-10-13", "停牌截止时间": "2026-10-13", "预计复牌时间": "2026-10-14"}
    assert _classify(tmp_path, [row])["legitimate_no_session_symbols"] == []
    row["停牌期限"] = "停牌一天"
    assert _classify(tmp_path, [row])["legitimate_no_session_symbols"] == ["000016"]


def test_full_day_label_does_not_override_earlier_actual_resumption(tmp_path: Path) -> None:
    result = _classify(tmp_path, [{
        "代码": "000016", "停牌时间": "2026-10-13 09:30:00", "停牌期限": "停牌一天",
        "停牌截止时间": "2026-10-13 10:00:00",
    }])
    assert result["legitimate_no_session_symbols"] == []


def test_positive_raw_bar_overrides_provider_claim(tmp_path: Path) -> None:
    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    pd.DataFrame([{
        "date": DAY, "symbol": "000016", "open": 10, "high": 11,
        "low": 9, "close": 10, "volume": 100, "amount": 1000, "adjust": "raw",
    }]).to_csv(daily / "000016.csv", index=False)
    result = _classify(tmp_path, [{
        "代码": "000016", "停牌时间": "2026-10-13 09:15:00", "停牌截止时间": "2026-10-13 15:00:00",
    }])
    assert result["per_symbol"]["000016"]["classification"] == "expected_trade"
    assert result["source_evidence"]["provider"]["verified_symbols"] == ["000016"]
    assert result["legitimate_no_session_symbols"] == []
