from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.candidate_tracking import (
    append_candidate_ledger,
    compute_indicators,
    read_daily_history,
)


def _write_daily(
    root: Path, rows: list[dict[str, object]], *, adjust: str | None = None
) -> None:
    daily = root / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    if adjust is not None:
        rows = [{**row, "adjust": adjust} for row in rows]
    pd.DataFrame(rows).to_csv(daily / "000001.csv", index=False)


def test_read_daily_history_keeps_missing_ohlc_none_and_warns(
    tmp_path: Path, caplog,
) -> None:
    _write_daily(
        tmp_path,
        [
            {
                "date": "2026-06-25",
                "open": 9.0,
                "high": 10.5,
                "low": 8.8,
                "close": 10.0,
                "volume": 1000,
            },
            {
                "date": "2026-06-26",
                "open": None,
                "high": "nan",
                "low": 10.8,
                "close": 12.0,
                "volume": 1000,
            },
        ],
    )

    with caplog.at_level(logging.WARNING):
        history = read_daily_history(tmp_path, "000001")

    row = history[1]
    assert row["open"] is None
    assert row["high"] is None
    assert row["low"] == 10.8
    assert "missing_daily_field:open" in row["warnings"]
    assert "invalid_daily_field:high" in row["reason_codes"]
    assert "candidate_tracking_daily_field_unavailable" in caplog.text


def test_kdj_is_unavailable_when_high_or_low_is_missing(tmp_path: Path) -> None:
    rows = []
    for idx in range(10):
        rows.append(
            {
                "date": f"2026-06-{idx + 1:02d}",
                "open": 10.0,
                "high": None if idx == 1 else 10.5,
                "low": 9.5,
                "close": 10.0,
            }
        )

    history = compute_indicators(rows)

    assert history[8]["kdj_k"] is None
    assert history[8]["kdj_d"] is None
    assert history[8]["kdj_j"] is None
    assert "indicator_unavailable:kdj_missing_high_low" in history[8]["warnings"]


def test_complete_ohlc_indicators_keep_existing_values(tmp_path: Path) -> None:
    rows = [
        {
            "date": f"2026-06-{idx + 1:02d}",
            "open": 10.0 + idx,
            "high": 10.5 + idx,
            "low": 9.5 + idx,
            "close": 10.0 + idx,
        }
        for idx in range(10)
    ]

    history = compute_indicators(rows)

    assert history[8]["kdj_k"] is not None
    assert history[8]["kdj_d"] is not None
    assert history[8]["warnings"] == []


def test_append_candidate_ledger_does_not_use_signal_close_without_next_open(
    tmp_path: Path,
) -> None:
    _write_daily(
        tmp_path,
        [
            {
                "date": "2026-06-25",
                "open": 9.0,
                "high": 10.5,
                "low": 8.8,
                "close": 10.0,
                "volume": 1000,
            },
            {
                "date": "2026-06-26",
                "open": None,
                "high": 12.5,
                "low": 10.8,
                "close": 12.0,
                "volume": 1000,
            },
        ],
    )
    candidate = tmp_path / "candidates.csv"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "name": "sample",
                "signal_date": "2026-06-25",
                "close": 10.0,
                "strategy_id": "long_test",
            }
        ]
    ).to_csv(candidate, index=False)

    payload = append_candidate_ledger(tmp_path, candidate)
    ledger = pd.read_csv(tmp_path / "data" / "tracking" / "candidate_ledger.csv", keep_default_na=False)

    assert payload["appended_count"] == 1
    assert ledger.loc[0, "entry_price"] == ""
    assert ledger.loc[0, "entry_price_status"] == "unavailable"
    assert "execution_price_unavailable:next_open_missing" in ledger.loc[0, "entry_price_reason"]
    assert "execution_price_unavailable:next_open_missing" in ledger.loc[0, "data_quality_warnings"]


def test_mixed_close_basis_is_flagged_without_rewriting_prices(tmp_path: Path) -> None:
    """adj_close rows and raw-close rows in one file must not look uniform."""

    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "adj_close": 9.2, "volume": 1000},
            {"date": "2026-06-26", "open": 9.3, "high": 9.9, "low": 9.1, "close": 9.8, "volume": 1000},
        ],
    )

    history = read_daily_history(tmp_path, "000001")

    assert [row["price_basis"] for row in history] == ["adjusted", "raw"]
    warning = "close_basis_mixed:adjusted+raw"
    assert all(warning in row["warnings"] for row in history)
    # The prices themselves are untouched: no re-basing, no averaging.
    assert [row["close"] for row in history] == [9.2, 9.8]


def test_uniform_close_basis_is_not_flagged(tmp_path: Path) -> None:
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "adj_close": 9.1, "volume": 1000},
            {"date": "2026-06-26", "open": 9.3, "high": 9.9, "low": 9.1, "close": 9.8, "adj_close": 9.7, "volume": 1000},
        ],
    )

    history = read_daily_history(tmp_path, "000001")

    assert {row["price_basis"] for row in history} == {"adjusted"}
    assert not any(
        warning.startswith("close_basis_mixed") for row in history for warning in row["warnings"]
    )


def _calendar(tmp_path: Path, days: list[str]) -> None:
    path = tmp_path / "data" / "calendar" / "a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{day},1,fixture" for day in days)
    path.write_text(f"date,is_open,source\n{body}\n", encoding="utf-8")


def test_execution_row_must_be_the_next_session(tmp_path: Path) -> None:
    """With an authoritative calendar, a gap is refused - not merely warned."""

    from quantdesk.candidate_tracking import _next_open_evidence

    _calendar(tmp_path, ["2026-06-25", "2026-06-26"])
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000},
            # 2026-06-26 (the verified next session) is missing.
            {"date": "2026-06-30", "open": 10.4, "high": 10.8, "low": 10.2, "close": 10.6, "volume": 1000},
        ],
    )

    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")

    # The later price is real, so it stays as a diagnostic, but it is not T+1
    # execution evidence and must not become an entry price.
    assert evidence["price"] is None
    assert evidence["diagnostic_price"] == 10.4
    assert any(
        reason.startswith("execution_price_unavailable:not_next_session:")
        for reason in evidence["reason_codes"]
    )


def test_next_session_is_accepted_when_the_calendar_confirms_it(tmp_path: Path) -> None:
    from quantdesk.candidate_tracking import _next_open_evidence

    _calendar(tmp_path, ["2026-06-25", "2026-06-26"])
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000},
            {"date": "2026-06-26", "open": 9.5, "high": 9.9, "low": 9.4, "close": 9.8, "volume": 1000},
        ],
        adjust="raw",
    )

    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")

    assert evidence["price"] == 9.5
    assert evidence["next_session_verified"] is True
    assert not evidence["reason_codes"]


def test_a_calendar_that_only_knows_the_signal_day_verifies_nothing(tmp_path: Path) -> None:
    """DSR-13: the named next session needs its own calendar evidence."""

    from quantdesk.candidate_tracking import _next_open_evidence

    # Only the signal day is in the calendar, so the next session is a weekday
    # guess and cannot be presented as verified.
    _calendar(tmp_path, ["2026-06-25"])
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000},
            {"date": "2026-06-26", "open": 9.5, "high": 9.9, "low": 9.4, "close": 9.8, "volume": 1000},
        ],
        adjust="raw",
    )

    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")

    assert evidence["next_session_verified"] is False
    assert any("next_session_unverified" in warning for warning in evidence["warnings"])


def test_a_file_without_any_adjust_evidence_is_not_read_as_raw(tmp_path: Path) -> None:
    """DSR-01: a missing ``adjust`` column is missing evidence, not raw."""

    from quantdesk.candidate_tracking import _next_open_evidence

    _calendar(tmp_path, ["2026-06-25", "2026-06-26"])
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000},
            {"date": "2026-06-26", "open": 9.5, "high": 9.9, "low": 9.4, "close": 9.8, "volume": 1000},
        ],
    )

    history = read_daily_history(tmp_path, "000001")

    assert {row["frame_price_basis"] for row in history} == {"mixed_or_unknown"}
    assert all(row["price_basis_usable"] is False for row in history)
    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")
    assert evidence["price"] is None
    assert any(
        reason.startswith("execution_price_unavailable:price_basis_unusable:")
        for reason in evidence["reason_codes"]
    )


def test_unverifiable_calendar_degrades_explicitly(tmp_path: Path) -> None:
    """No calendar asset must not be presented as a verified T+1 session."""

    from quantdesk.candidate_tracking import _next_open_evidence

    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000},
            {"date": "2026-06-30", "open": 10.4, "high": 10.8, "low": 10.2, "close": 10.6, "volume": 1000},
        ],
        adjust="raw",
    )

    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")

    assert evidence["price"] == 10.4  # a real price, kept
    assert evidence["next_session_verified"] is False  # never asserted as verified
    assert any("next_session_unverified" in warning for warning in evidence["warnings"])


def test_mixed_adjust_column_rejects_execution_evidence(tmp_path: Path) -> None:
    """PT-01a: the raw ``adjust`` column must govern, not the field name."""

    from quantdesk.candidate_tracking import _next_open_evidence

    _calendar(tmp_path, ["2026-06-25", "2026-06-26"])
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000, "adjust": "raw"},
            {"date": "2026-06-26", "open": 9.5, "high": 9.9, "low": 9.4, "close": 9.8, "volume": 1000, "adjust": "qfq"},
        ],
    )

    history = read_daily_history(tmp_path, "000001")

    assert {row["frame_price_basis"] for row in history} == {"mixed_or_unknown"}
    assert all(row["price_basis_usable"] is False for row in history)
    assert any(
        reason.startswith("price_basis_unusable:") for row in history for reason in row["reason_codes"]
    )
    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")
    assert evidence["price"] is None
    assert any(
        reason.startswith("execution_price_unavailable:price_basis_unusable:")
        for reason in evidence["reason_codes"]
    )


def test_adj_close_series_cannot_mix_with_a_raw_open(tmp_path: Path) -> None:
    """PT-01b: adjusted closes against a raw open is not a usable ratio."""

    from quantdesk.candidate_tracking import _next_open_evidence

    _calendar(tmp_path, ["2026-06-25", "2026-06-26"])
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "adj_close": 4.6, "volume": 1000},
            {"date": "2026-06-26", "open": 11.0, "high": 11.5, "low": 10.8, "close": 11.2, "adj_close": 5.6, "volume": 1000},
        ],
    )

    history = read_daily_history(tmp_path, "000001")

    assert {row["price_basis"] for row in history} == {"adjusted"}
    assert {row["open_basis"] for row in history} == {"raw"}
    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")
    assert evidence["price"] is None
    assert any(
        reason.startswith("execution_price_unavailable:basis_mismatch:")
        for reason in evidence["reason_codes"]
    )


def test_formal_tracking_row_never_writes_a_signal_close_as_entry_price() -> None:
    """PT-03: the projection must not manufacture an execution price."""

    from quantdesk.candidate_tracking import _formal_tracking_row

    row = _formal_tracking_row(
        {"symbol": "000001", "close": 10.0, "name": "sample"},
        target_date="2026-06-25",
        source_path="fixtures/candidates.csv",
        source_run_id="run-1",
        source_hash="hash-1",
    )

    assert row["entry_price"] == ""
    assert row["entry_price_status"] == "unavailable"
    assert "no_execution_evidence" in row["entry_price_reason"]


def test_missing_next_open_leaves_entry_price_blank(tmp_path: Path) -> None:
    """The signal close is not a substitute for the T+1 execution open."""

    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 9.5, "low": 8.9, "close": 9.2, "volume": 1000},
            {"date": "2026-06-26", "open": None, "high": 9.9, "low": 9.1, "close": 9.8, "volume": 1000},
        ],
    )

    from quantdesk.candidate_tracking import _next_open_evidence

    evidence = _next_open_evidence(tmp_path, "000001", "2026-06-25")

    assert evidence["price"] is None
    assert "execution_price_unavailable:next_open_missing" in evidence["reason_codes"]
    assert 9.8 not in (evidence["price"],)  # never the close


def test_a_verified_calendar_still_produces_the_ordinary_positive_return(tmp_path: Path) -> None:
    """The gate refuses missing evidence, not ordinary research results.

    The same raw series, a complete calendar and an empty legacy ``entry_price``
    must still produce the formal five-day return.  This is the positive control
    the refusal rules are calibrated against.
    """

    from quantdesk.candidate_tracking import build_candidate_performance

    _calendar(
        tmp_path,
        [
            "2026-06-25",
            "2026-06-26",
            "2026-06-29",
            "2026-06-30",
            "2026-07-01",
            "2026-07-02",
            "2026-07-03",
        ],
    )
    _write_daily(
        tmp_path,
        [
            {"date": "2026-06-25", "open": 9.0, "high": 10.1, "low": 8.9, "close": 10.0, "volume": 1000},
            {"date": "2026-06-26", "open": 11.0, "high": 11.2, "low": 10.9, "close": 11.0, "volume": 1000},
            {"date": "2026-06-29", "open": 12.0, "high": 12.2, "low": 11.9, "close": 12.0, "volume": 1000},
            {"date": "2026-06-30", "open": 13.0, "high": 13.2, "low": 12.9, "close": 13.0, "volume": 1000},
            {"date": "2026-07-01", "open": 14.0, "high": 14.2, "low": 13.9, "close": 14.0, "volume": 1000},
            {"date": "2026-07-02", "open": 15.0, "high": 15.2, "low": 14.9, "close": 15.0, "volume": 1000},
            {"date": "2026-07-03", "open": 16.0, "high": 16.2, "low": 15.9, "close": 16.0, "volume": 1000},
        ],
        adjust="raw",
    )
    tracking = tmp_path / "data" / "tracking"
    tracking.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "entry_date": "2026-06-25",
                "ts_code": "000001.SZ",
                "strategy_type": "audit_fixture",
                "entry_price": "11.00",
                "entry_price_status": "available",
            }
        ]
    ).to_csv(tracking / "candidate_ledger.csv", index=False)

    payload = build_candidate_performance(tmp_path)
    row = pd.read_csv(tracking / "candidate_performance.csv").iloc[0]

    assert row["entry_price_status"] == "available"
    assert float(row["entry_price"]) == 11.0
    assert float(row["hold_5d_ret"]) == pytest.approx((16.0 - 11.0) / 11.0, abs=1e-6)
    assert float(row["hold_1d_ret"]) == pytest.approx((12.0 - 11.0) / 11.0, abs=1e-6)
    assert payload.get("warning_count") == 0


