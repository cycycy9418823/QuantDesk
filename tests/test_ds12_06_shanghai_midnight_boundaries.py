"""DS12-06 Section-C3: Shanghai-midnight handling for three named entries.

Offline only. Verifies naive/aware inputs at the Shanghai midnight boundary for:

- the intraday/in-session "today" decision (``detect_market_session``),
- the AI dashboard journal age (``ai_trader.dashboard``),
- the news status timestamp (``quantdesk.news_status.now_text``).

The warning from the task is respected: UTC receipt/audit clocks are NOT
rewritten here. Only the three local-day entries are normalized to Asia/Shanghai.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_trader.dashboard import _shanghai_date
from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.market_session_detector import detect_market_session

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 2026-06-03 16:30 UTC == 2026-06-04 00:30 Asia/Shanghai (just after midnight).
UTC_AFTER_SHANGHAI_MIDNIGHT = datetime(2026, 6, 3, 16, 30, tzinfo=timezone.utc)
SHANGHAI_LOCAL = datetime(2026, 6, 4, 0, 30, tzinfo=SHANGHAI)


def test_to_shanghai_datetime_treats_naive_as_local_and_converts_aware() -> None:
    assert to_shanghai_datetime(datetime(2026, 6, 4, 0, 30)).date().isoformat() == "2026-06-04"
    assert to_shanghai_datetime(UTC_AFTER_SHANGHAI_MIDNIGHT).date().isoformat() == "2026-06-04"
    assert to_shanghai_datetime(SHANGHAI_LOCAL).date().isoformat() == "2026-06-04"


@pytest.mark.parametrize(
    "now",
    [
        SHANGHAI_LOCAL,
        UTC_AFTER_SHANGHAI_MIDNIGHT,
        datetime(2026, 6, 4, 0, 30),  # naive, treated as Shanghai local
    ],
)
def test_market_session_uses_shanghai_day_at_midnight(tmp_path: Path, now: datetime) -> None:
    payload = detect_market_session(tmp_path, now=now)

    assert payload["session"] == "before_open"
    assert payload["is_trading_day"] is True
    # current_local_time is the normalized Shanghai instant, not the raw input.
    assert payload["current_local_time"].startswith("2026-06-04")


def test_market_session_aware_utc_close_time_maps_to_shanghai_afternoon(tmp_path: Path) -> None:
    # 2026-06-03 07:00 UTC == 2026-06-03 15:00 Shanghai → near_close window.
    payload = detect_market_session(tmp_path, now=datetime(2026, 6, 3, 7, 0, tzinfo=timezone.utc))

    assert payload["session"] == "near_close"
    assert payload["current_local_time"].startswith("2026-06-03")


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 6, 4, 0, 30),  # naive
        UTC_AFTER_SHANGHAI_MIDNIGHT,  # aware UTC
        SHANGHAI_LOCAL,  # aware Shanghai
    ],
)
def test_dashboard_journal_age_date_is_shanghai(now: datetime) -> None:
    assert _shanghai_date(now).isoformat() == "2026-06-04"


def test_dashboard_journal_age_does_not_mix_naive_and_aware() -> None:
    # Before the fix, an aware UTC ``now`` minus a naive strptime raised
    # TypeError. The Shanghai date subtraction must be safe now.
    reference = _shanghai_date(UTC_AFTER_SHANGHAI_MIDNIGHT)
    journal_date = datetime.strptime("2026-06-04", "%Y-%m-%d").date()

    assert (reference - journal_date).days == 0


def test_news_status_now_text_goes_through_shanghai_converter(monkeypatch) -> None:
    import quantdesk.news_status as news_status

    real = to_shanghai_datetime
    monkeypatch.setattr(
        news_status, "to_shanghai_datetime", lambda *args, **kwargs: real(UTC_AFTER_SHANGHAI_MIDNIGHT)
    )

    assert news_status.now_text() == "2026-06-04 00:30:00"
    assert news_status.news_status_sample_mode("600000").fetched_at == "2026-06-04 00:30:00"
