from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from quantdesk.daily_price_providers import merge_daily_frames, normalize_daily_df
from quantdesk.data_providers import akshare_provider, fundamentals_chain
from quantdesk.providers import news_provider


def test_akshare_failure_paths_are_not_ok() -> None:
    payload = akshare_provider.fetch_daily_bars_akshare("000001", network=False)

    assert payload["ok"] is False
    assert payload["rows"] == []


def test_news_provider_keeps_live_fetch_error_visible(monkeypatch) -> None:
    monkeypatch.setattr(news_provider, "_fetch_announce_eastmoney", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(news_provider, "_read_local_news", lambda *_a, **_k: [])

    payload = news_provider.fetch_news("000001", network=True)

    assert "Live fetch failed: boom" in payload["failed_reason"]


def test_fundamentals_chain_uses_payload_report_date(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    class FakeDB:
        def __init__(self, _path: str) -> None:
            pass

        def initialize(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeRepo:
        def __init__(self, _db: FakeDB) -> None:
            pass

        def latest_fundamentals(self, _symbol: str) -> None:
            return None

        def upsert_fundamentals(self, rows: list[dict]) -> None:
            captured.extend(rows)

    monkeypatch.setattr(fundamentals_chain, "QuantDeskDB", FakeDB)
    monkeypatch.setattr(fundamentals_chain, "FundamentalRepository", FakeRepo)
    monkeypatch.setattr(
        fundamentals_chain,
        "fetch_fundamental_abstract_ths",
        lambda *_a, **_k: {"rows": [{"报告期": "2026-03-31", "eps": 1.23}], "warnings": [], "source": "akshare"},
    )

    payload = fundamentals_chain.load_fundamentals("000001", network=True, db_path=str(tmp_path / "q.db"))

    assert payload["rows"][0]["report_date"] == "2026-03-31"
    assert captured[0]["report_date"] == "2026-03-31"
    assert "report_date_fallback_to_now" not in payload["warnings"]


def test_fundamentals_chain_bad_report_date_falls_back_with_warning(monkeypatch, tmp_path: Path) -> None:
    class FakeDB:
        def __init__(self, _path: str) -> None:
            pass

        def initialize(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeRepo:
        def __init__(self, _db: FakeDB) -> None:
            pass

        def latest_fundamentals(self, _symbol: str) -> None:
            return None

        def upsert_fundamentals(self, _rows: list[dict]) -> None:
            pass

    monkeypatch.setattr(fundamentals_chain, "QuantDeskDB", FakeDB)
    monkeypatch.setattr(fundamentals_chain, "FundamentalRepository", FakeRepo)
    monkeypatch.setattr(
        fundamentals_chain,
        "fetch_fundamental_abstract_ths",
        lambda *_a, **_k: {"rows": [{"report_date": "2026-bad-date", "eps": 1.23}], "warnings": [], "source": "akshare"},
    )

    payload = fundamentals_chain.load_fundamentals("000001", network=True, db_path=str(tmp_path / "q.db"))

    assert payload["rows"][0]["report_date"] != "2026-bad-date"
    assert "report_date_fallback_to_now" in payload["warnings"]


def test_daily_price_provider_warns_when_ohlc_is_filled_from_close() -> None:
    frame = pd.DataFrame({"date": ["2026-06-26"], "close": [10.0]})

    normalized, warnings = normalize_daily_df(frame, "000001", source="akshare_daily")

    assert normalized[["open", "high", "low"]].isna().all().all()
    assert normalized.iloc[0]["close"] == 10.0
    assert "ohlc_missing:open:1_rows" in warnings
    assert "ohlc_missing:high:1_rows" in warnings
    assert "ohlc_missing:low:1_rows" in warnings


def test_merge_daily_frames_preserves_input_normalize_warnings() -> None:
    existing = pd.DataFrame({"date": ["2026-06-25"], "symbol": ["000001"], "open": [9.8], "high": [10.0], "low": [9.7], "close": [9.9]})
    fresh = pd.DataFrame({"date": ["2026-06-26"], "symbol": ["000001"], "close": [10.0]})

    merged = merge_daily_frames(existing, fresh)

    warnings = merged.attrs.get("warnings") or []
    assert "ohlc_missing:open:1_rows" in warnings
    assert "ohlc_missing:high:1_rows" in warnings
    assert "ohlc_missing:low:1_rows" in warnings
    assert merged.loc[merged["date"] == "2026-06-26", ["open", "high", "low"]].isna().all().all()


def test_merge_daily_frames_preserves_input_frame_attrs_warnings() -> None:
    existing = pd.DataFrame({"date": ["2026-06-25"], "symbol": ["000001"], "open": [9.8], "high": [10.0], "low": [9.7], "close": [9.9]})
    existing.attrs["warnings"] = ["local_daily_cache_read_warning:000001:csv_bad_lines_skipped:data/daily/000001.csv"]
    fresh = pd.DataFrame({"date": ["2026-06-26"], "symbol": ["000001"], "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0]})

    merged = merge_daily_frames(existing, fresh)

    assert "local_daily_cache_read_warning:000001:csv_bad_lines_skipped:data/daily/000001.csv" in (merged.attrs.get("warnings") or [])


def test_update_market_data_logs_bad_cache_read(monkeypatch, caplog, tmp_path: Path) -> None:
    import scripts.update_market_data as update_market_data

    path = tmp_path / "000001.csv"
    path.write_text("bad", encoding="utf-8")
    monkeypatch.setattr(update_market_data, "read_csv_frame", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad csv")))

    with caplog.at_level(logging.WARNING):
        frame = update_market_data.read_daily_cache(path, "000001")

    assert frame.empty
    assert "read_daily_cache failed for 000001: bad csv" in caplog.text
