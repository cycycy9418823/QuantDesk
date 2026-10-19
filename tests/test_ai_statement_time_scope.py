"""Statement-gate regressions for factual dates and return periods."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from ai_trader.decision_log import apply_gate_to_plan, iter_daily_rows
from quantdesk import trading_calendar


_DAYS_AND_CLOSES = [
    ("2026-08-28", 100.0),
    ("2026-08-31", 102.0),
    ("2026-09-01", 104.0),
    ("2026-09-02", 108.0),
    ("2026-09-03", 116.0),
    ("2026-09-04", 120.0),
]


def _write_daily(root: Path, rows: list[tuple[str, float]], symbol: str = "600000") -> None:
    lines = ["date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"]
    previous = rows[0][1]
    for day, close in rows:
        pct_chg = (close / previous - 1.0) * 100.0
        lines.append(
            f"{day},{symbol},{close},{close},{close},{close},1000,10000,{pct_chg:.6f},raw"
        )
        previous = close
    (root / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _gate(daily: Path, reason: str):
    plan = {
        "decision_date": "2026-09-04",
        "intents": [
            {
                "symbol": "600000",
                "side": "buy",
                "ref_close": 120.0,
                "llm_reason": reason,
            }
        ],
    }
    result = apply_gate_to_plan(plan, daily_dir=daily)
    return plan, result


def test_today_claim_cannot_match_five_session_return(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "今天涨20%")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "claimed_pct_untagged"
    assert hit["actual"] == pytest.approx(3.448276, abs=1e-5)
    assert hit["evidence_date"] == "2026-09-04"


def test_genuine_today_claim_matches_decision_day_return(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "今天涨3.45%")

    assert len(plan["intents"]) == 1
    assert result.rejections == []


def test_explicit_five_session_claim_matches_five_session_return(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "近5个交易日涨20%")

    assert len(plan["intents"]) == 1
    assert result.rejections == []


def test_ambiguous_period_is_unverified_not_matched_to_five_day_metric(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "近期涨20%")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "claimed_pct_ambiguous_period"
    assert hit["evidence_status"] == "unverified"
    assert "时段" in hit["detail"]


def test_explicit_past_date_price_uses_that_dates_bar(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "2026-09-03收盘120元")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "claimed_close"
    assert hit["actual"] == 116.0
    assert hit["evidence_date"] == "2026-09-03"


def test_explicit_future_forecast_is_not_checked_as_observed_fact(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "预计明天涨99%，明天收盘999元")

    assert len(plan["intents"]) == 1
    assert result.rejections == []


@pytest.mark.parametrize(
    "reason",
    [
        "今天涨20%，预计明天涨5%",
        "预计明天涨5%，但今天涨20%",
    ],
)
def test_future_forecast_does_not_hide_false_today_fact(
    tmp_path: Path, reason: str
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, reason)

    assert plan["intents"] == []
    hits = result.rejections[0]["statement_rejections"]
    assert any(
        hit["kind"] == "claimed_pct_untagged"
        and hit.get("actual") == pytest.approx(3.448276, abs=1e-5)
        for hit in hits
    )


def test_missing_five_session_evidence_is_unverified(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES[1:])

    plan, result = _gate(daily, "近5个交易日涨20%")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "claimed_pct_missing_evidence"
    assert hit["evidence_status"] == "unverified"
    assert "证据不足" in hit["detail"]


@pytest.mark.parametrize(
    ("reason", "kind", "actual"),
    [
        ("2026-09-03涨3.45%", "claimed_pct_untagged", 7.407407),
        ("2026-09-03开盘120元", "claimed_open", 116.0),
        ("2026-09-03的收盘价120元", "claimed_close", 116.0),
        ("昨日收盘120元", "claimed_close", 116.0),
    ],
)
def test_historical_fact_cannot_borrow_the_decision_day_value(
    tmp_path: Path, reason: str, kind: str, actual: float
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, reason)

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == kind
    assert hit["actual"] == pytest.approx(actual, abs=1e-5)
    assert hit["evidence_date"] == "2026-09-03"


@pytest.mark.parametrize(
    "reason",
    [
        "2026-09-03涨7.41%",
        "2026-09-03开盘116元",
        "2026-09-03的收盘价116元",
        "昨日收盘116元",
        "近5个交易日累计上涨20%",
        "近5交易日涨20%",
    ],
)
def test_matching_explicit_date_and_period_remain_accepted(
    tmp_path: Path, reason: str
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, reason)

    assert len(plan["intents"]) == 1
    assert result.rejections == []


@pytest.mark.parametrize(
    "reason",
    [
        "预计2026-09-07收盘999元",
        "2026-09-07开盘999元",
        "预计明天在市场回暖之后上涨99%",
    ],
)
def test_explicit_forecast_scope_survives_dates_and_long_clauses(
    tmp_path: Path, reason: str
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, reason)

    assert len(plan["intents"]) == 1
    assert result.rejections == []


@pytest.mark.parametrize(
    "reason",
    [
        "预计明天涨5%，今天收盘999元",
        "预计明天涨5%，今天开盘999元",
        "预计明天涨5%，但收盘999元",
        "预计明天涨5%，今天累计上涨20%",
        "预计明天涨5%，近期累计上涨3.45%",
    ],
)
def test_forecast_clause_cannot_hide_a_later_observation(
    tmp_path: Path, reason: str
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, reason)

    assert plan["intents"] == []
    assert result.rejections[0]["statement_rejections"]


def test_missing_historical_price_is_explicitly_unverified(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "2026-08-27收盘120元")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["evidence_date"] == "2026-08-27"
    assert hit["actual"] is None
    assert hit["evidence_status"] == "unverified"


@pytest.mark.parametrize(
    ("note", "accepted"),
    [
        ("今天600000+20%", False),
        ("近5个交易日600000+20%", True),
        ("今天600000+3.45%，2026-09-03 600000+3.45%", False),
        ("预计明天600000+99%，今天600000+20%", False),
    ],
)
def test_market_note_uses_each_symbol_claims_own_period(
    tmp_path: Path, note: str, accepted: bool
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)
    plan = {
        "decision_date": "2026-09-04",
        "llm": {"used": True, "market_note": note},
        "intents": [{"symbol": "600000", "side": "buy", "ref_close": 120.0}],
    }

    result = apply_gate_to_plan(plan, daily_dir=daily)

    # 2AI-1：点评按每句自己的时段核对，结论只标注在点评上，不再拦下买入。
    assert len(plan["intents"]) == 1
    assert result.market_note_ok is accepted


def test_symbol_period_missing_history_cannot_fall_back_to_today(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES[1:])
    plan = {
        "decision_date": "2026-09-04",
        "llm": {"used": True, "market_note": "近5个交易日600000+3.45%"},
        "intents": [{"symbol": "600000", "side": "buy", "ref_close": 120.0}],
    }

    result = apply_gate_to_plan(plan, daily_dir=daily)

    # 2AI-1：点评缺历史只记"未核验"，不拦买入。
    assert len(plan["intents"]) == 1
    hit = result.market_note_rejections[0]
    assert hit["actual"] is None
    assert hit["evidence_status"] == "unverified"


def test_symbol_claim_is_not_checked_against_the_intent_symbol_twice(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, _DAYS_AND_CLOSES)
    _write_daily(daily, [("2026-09-03", 100.0), ("2026-09-04", 110.0)], "600001")

    plan, result = _gate(daily, "今天600001+10%")

    assert len(plan["intents"]) == 1
    assert result.rejections == []


@pytest.mark.parametrize("reason", ["近5个交易日涨20%", "近5个交易日600000+20%"])
def test_missing_session_is_not_replaced_by_an_older_available_row(
    tmp_path: Path, reason: str
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    rows = [("2026-08-27", 100.0)] + [
        row for row in _DAYS_AND_CLOSES if row[0] != "2026-09-02"
    ]
    _write_daily(daily, rows)

    plan, result = _gate(daily, reason)

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["evidence_status"] == "unverified"


def test_historical_period_cannot_end_on_the_decision_day(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, [("2026-08-27", 100.0)] + _DAYS_AND_CLOSES)

    plan, result = _gate(daily, "截至2026-09-03近5个交易日涨20%")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["actual"] == pytest.approx(16.0)
    assert hit["evidence_date"] == "2026-09-03"


@pytest.mark.parametrize("reason", ["截至2026-09-03近5个交易日涨20%", "截至2026-09-03近5个交易日600000+20%"])
def test_missing_period_endpoint_is_not_an_earlier_bar(tmp_path: Path, reason: str) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    rows = [("2026-08-26", 90.0), ("2026-08-27", 90.0)] + [
        row for row in _DAYS_AND_CLOSES if row[0] != "2026-09-03"
    ]
    _write_daily(daily, rows)

    plan, result = _gate(daily, reason)

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["evidence_date"] == "2026-09-03"
    assert hit["evidence_status"] == "unverified"


@pytest.mark.parametrize("close", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_close_cannot_verify_a_finite_reference(tmp_path: Path, close: float) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, [("2026-09-03", 100.0), ("2026-09-04", close)])

    plan, result = _gate(daily, "")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "ref_close"
    assert hit["actual"] is None
    assert hit["evidence_status"] == "unverified"


def test_missing_today_percentage_is_explicitly_unverified(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,pct_chg,adjust\n"
        "2026-09-04,600000,120,120,120,120,,raw\n",
        encoding="utf-8",
    )

    plan, result = _gate(daily, "今天涨3.45%")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["actual"] is None
    assert hit["evidence_status"] == "unverified"


def test_missing_previous_session_cannot_be_computed_as_one_day(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,pct_chg,adjust\n"
        "2026-09-02,600000,100,100,100,100,0,raw\n"
        "2026-09-04,600000,120,120,120,120,,raw\n",
        encoding="utf-8",
    )

    plan, result = _gate(daily, "今天涨20%")

    assert plan["intents"] == []
    hit = result.rejections[0]["statement_rejections"][0]
    assert hit["actual"] is None
    assert hit["evidence_status"] == "unverified"


@pytest.mark.parametrize("check_period", [False, True])
def test_long_history_resolves_calendar_once_per_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_period: bool
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    calendar_path = tmp_path / "calendar.csv"
    calendar_lines = ["date,is_open,source,note"]
    daily_lines = ["date,symbol,open,high,low,close,pct_chg,adjust"]
    special_sessions = {"2020-01-06": False, "2020-01-11": True}
    special_closes = {
        "2020-01-07": 110.0,
        "2020-01-08": 110.0,
        "2020-01-10": 121.0,
        "2020-01-11": 133.1,
        "2020-01-13": 146.41,
    }
    end_day = ""
    for offset in range(1600):
        day = date(2020, 1, 1) + timedelta(days=offset)
        day_text = day.isoformat()
        is_open = special_sessions.get(day_text, day.weekday() < 5)
        calendar_lines.append(f"{day_text},{str(is_open).lower()},deterministic_fixture,")
        if is_open and day_text != "2020-01-09":
            close = special_closes.get(day_text, 100.0)
            daily_lines.append(f"{day_text},600000,{close},{close},{close},{close},,raw")
            end_day = day_text
    calendar_path.write_text("\n".join(calendar_lines) + "\n", encoding="utf-8")
    (daily / "600000.csv").write_text("\n".join(daily_lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(trading_calendar, "DEFAULT_TRADING_CALENDAR_CSV", calendar_path)
    original_key = trading_calendar._calendar_cache_key
    resolutions: list[Path | None] = []

    def counted_key(path: Path | None = None) -> str:
        resolutions.append(path)
        return original_key(path)

    monkeypatch.setattr(trading_calendar, "_calendar_cache_key", counted_key)

    rows = iter_daily_rows("600000", daily)
    by_date = {row["date"]: row for row in rows}
    assert len(rows) > 1000
    assert by_date["2020-01-07"]["pct_chg"] == pytest.approx(10.0)
    assert by_date["2020-01-10"]["pct_chg"] is None
    assert by_date["2020-01-11"]["pct_chg"] == pytest.approx(10.0)
    assert by_date["2020-01-13"]["pct_chg"] == pytest.approx(10.0)
    if check_period:
        plan = {
            "decision_date": end_day,
            "intents": [
                {"symbol": "600000", "ref_close": 100.0, "llm_reason": "近60个交易日涨0%"}
            ],
        }
        result = apply_gate_to_plan(plan, daily_dir=daily)
        assert len(plan["intents"]) == 1
        assert result.rejections == []
    assert len(resolutions) <= (2 if check_period else 1)


def test_mixed_or_unknown_price_basis_is_unverified(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2026-09-03,600000,10,10,10,10,1000,10000,0,raw\n"
        "2026-09-04,600000,10,10,10,10.4,1000,10400,4,qfq\n",
        encoding="utf-8",
    )
    plan, result = _gate(daily, "今天涨4%")
    assert plan["intents"] == []
    assert result.rejections[0]["statement_rejections"][0]["kind"] == "claimed_price_basis_untrusted"
    assert result.rejections[0]["statement_rejections"][0]["evidence_status"] == "unverified"


def test_missing_adjust_column_is_unverified(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg\n"
        "2026-09-04,600000,10,10,10,10.4,1000,10400,4\n",
        encoding="utf-8",
    )
    plan, result = _gate(daily, "今天涨4%")
    assert plan["intents"] == []
    assert result.rejections[0]["statement_rejections"][0]["kind"] == "claimed_price_basis_untrusted"
