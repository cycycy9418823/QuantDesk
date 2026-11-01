"""Stock page sync: selected-quote scope, local minute view, no silent staleness.

Covers the 2026-10-27 reported defect where the page kept a saved quote without
a date and the viewer never allowed the selected-stock quote path to refresh.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.market_clock import to_shanghai_datetime
from scripts.server.routes.stock_workbench import StockWorkbenchDeps, create_stock_workbench_blueprint


def _minute_frame(dates: list[str]) -> pd.DataFrame:
    rows = []
    for day in dates:
        for minute in ("09:30", "09:31"):
            rows.append({"symbol": "600000", "datetime": f"{day} {minute}:00", "date": day,
                         "time": f"{minute}:00", "open": 9.5, "high": 9.6, "low": 9.4,
                         "close": 9.55, "volume": 1000.0, "amount": 9550.0,
                         "price_basis": "raw", "source": "fixture_minute"})
    return pd.DataFrame(rows)


def test_research_brief_names_the_daily_close_and_preserves_its_data_date():
    import scripts.quant_api_server as api
    reference = {"available": True, "price_band": {"latest": 9.85}}
    brief = api.build_stock_research_brief(reference, as_of="2026-10-28")
    assert brief["data_date"] == "2026-10-28"
    assert "日线收盘 9.85" in brief["current_state"]
    assert "现价" not in brief["current_state"]
    assert api.build_stock_research_brief(reference)["data_date"] == ""


def test_observed_script_has_own_version_and_no_store_header():
    import scripts.quant_api_server as api
    assert "paper_observed" in api.daily_research_asset_versions()
    response = api.app.test_client().get("/static/js/paper_observed.js")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_minute_view_returns_only_the_latest_local_session(tmp_path, monkeypatch):
    import scripts.quant_api_server as api
    minute_dir = tmp_path / "1m"
    minute_dir.mkdir()
    _minute_frame(["2026-10-08", "2026-10-09"]).to_csv(minute_dir / "600000.csv", index=False)
    monkeypatch.setattr(api, "INTRADAY_ONE_MINUTE_DIR", minute_dir)
    monkeypatch.setattr(api, "read_csv_frame", lambda path, **kwargs: pd.read_csv(path))

    payload = api.stock_minute_view("600000")

    assert payload["available"] is True
    assert payload["date"] == "2026-10-09"
    assert [row["time"] for row in payload["rows"]] == ["09:30", "09:31"]
    assert payload["coverage"]["first_date"] == "2026-10-08"
    assert payload["coverage"]["total_rows"] == 4
    assert payload["price_basis"] == ["raw"]


def test_minute_view_reports_missing_cache_instead_of_inventing_bars(tmp_path, monkeypatch):
    import scripts.quant_api_server as api
    monkeypatch.setattr(api, "INTRADAY_ONE_MINUTE_DIR", tmp_path)
    payload = api.stock_minute_view("920016")
    assert payload["available"] is False
    assert payload["rows"] == []
    assert "没有该股的1分钟缓存" in payload["message"]


def test_minute_view_degrades_when_the_file_cannot_be_read(tmp_path, monkeypatch):
    import scripts.quant_api_server as api
    (tmp_path / "600000.csv").write_text("not,a,minute,file\n", encoding="utf-8")
    monkeypatch.setattr(api, "INTRADAY_ONE_MINUTE_DIR", tmp_path)
    def boom(path, **kwargs):
        raise ValueError("unreadable")
    monkeypatch.setattr(api, "read_csv_frame", boom)
    payload = api.stock_minute_view("600000")
    assert payload["available"] is False
    assert "读取失败" in payload["message"]


def _deps(tmp_path: Path, *, network: bool, minute_payload=None) -> StockWorkbenchDeps:
    frame = pd.DataFrame({"date": pd.bdate_range(end="2026-10-27", periods=90),
                          "open": 9.4, "high": 9.6, "low": 9.3, "close": 9.5,
                          "volume": 1000.0, "adjust": "raw"})
    return StockWorkbenchDeps(
        load_original_daily=lambda symbol: {"frame": frame, "sha256": "a" * 64},
        cached_quote=lambda symbol: {"symbol": symbol, "price": 9.82, "available": True,
                                     "quote_time": "15:05:09", "price_basis": "raw",
                                     "source": "sina.spot_direct",
                                     "updated_at": "2026-10-27T15:05:02+08:00"},
        fetch_quote=lambda symbol: {"ok": False, "message_for_ui": "offline fixture"},
        research_builder=lambda data: {"available": False},
        load_calendar=lambda: {"is_open_by_date": {"2026-10-27": True, "2026-10-28": True}},
        network_enabled=lambda: network,
        refresh_allowed=lambda: True,
        now=lambda: datetime.fromisoformat("2026-10-27T20:00:00+08:00"),
        load_minute_bars=lambda symbol: minute_payload or {"available": False, "rows": [], "message": "fixture"},
    )


def _client(deps: StockWorkbenchDeps):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(create_stock_workbench_blueprint(deps))
    return app.test_client()


def test_view_keeps_time_only_cached_price_visible_but_not_verified():
    client = _client(_deps(Path("."), network=True))
    payload = client.get("/api/stock/600000/workbench").get_json()
    assert payload["network_enabled"] is True
    evidence = payload["quote_evidence"]
    assert payload["quote"]["price"] == 9.82
    assert evidence["usable_latest_quote"] is False
    assert evidence["status"] == "unverified_time"
    assert evidence["quote_timestamp_inferred"] is False
    assert evidence["quote_timestamp"] == ""
    assert evidence["collection_timestamp"] == "2026-10-27T15:05:02+08:00"
    assert evidence["comparable_to_raw_daily"] is False


def test_auto_refresh_stays_off_when_the_quote_scope_is_closed():
    client = _client(_deps(Path("."), network=False))
    payload = client.get("/api/stock/600000/workbench").get_json()
    assert payload["network_enabled"] is False


def test_minute_endpoint_serves_the_local_cache_and_labels_missing_data():
    client = _client(_deps(Path("."), network=True, minute_payload={
        "available": True, "date": "2026-10-08", "rows": [{"time": "09:30", "close": 9.6}],
        "coverage": {"first_date": "2025-06-24", "last_date": "2026-10-08"},
        "source": ["sina_intraday"], "price_basis": ["raw"], "message": "fixture"}))
    payload = client.get("/api/stock/600000/minute").get_json()
    assert payload["symbol"] == "600000"
    assert payload["available"] is True
    assert payload["date"] == "2026-10-08"
    assert payload["paper_only"] is True and payload["no_broker"] is True

    blank = _client(_deps(Path("."), network=True))
    missing = blank.get("/api/stock/920016/minute").get_json()
    assert missing["available"] is False


@pytest.mark.parametrize("symbol", ["600000", "000001", "920016"])
def test_minute_route_accepts_each_board_symbol(symbol):
    client = _client(_deps(Path("."), network=True))
    assert client.get(f"/api/stock/{symbol}/minute").status_code == 200


def test_viewer_hardens_the_quote_scope_but_never_the_whole_network():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "run_research_viewer.py").read_text(encoding="utf-8")
    assert "STOCK_QUOTE_NETWORK_ENABLED=guard.allow_quotes" in source
    assert "REALTIME_NETWORK_ENABLED=False" in source


def test_api_uses_the_selected_quote_scope_without_widening_the_default():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "quant_api_server.py").read_text(encoding="utf-8")
    assert 'app.config.get("STOCK_QUOTE_NETWORK_ENABLED", realtime_network_enabled())' in source


def test_live_quote_row_without_available_flag_is_not_rejected():
    # The live eastmoney single-quote row omits `available`; a missing flag must
    # not be read as "no quote", or every refresh shows a false failure.
    from quantdesk.quote_evidence import inspect_quote
    row = {"symbol": "600000", "price": 9.93, "quote_time": "2026-10-28 10:15:51",
           "quote_date": "2026-10-28", "quote_timestamp": None, "price_basis": "raw",
           "source": "eastmoney.single_quote", "updated_at": "2026-10-28T10:15:33+08:00"}
    checked = inspect_quote(row, symbol="600000", now=datetime.fromisoformat("2026-10-28T10:15:55+08:00"),
                            session="morning_session", expected_trade_date=date(2026, 10, 28))
    assert checked["price_valid"] is True
    assert checked["usable_latest_quote"] is True
    assert checked["status"] == "live"
    assert checked["comparable_to_raw_daily"] is True


def test_explicitly_unavailable_quote_is_still_rejected():
    from quantdesk.quote_evidence import inspect_quote
    row = {"symbol": "600000", "price": 9.93, "available": False, "price_basis": "raw",
           "quote_time": "2026-10-28 10:15:51", "source": "eastmoney.single_quote",
           "updated_at": "2026-10-28T10:15:33+08:00"}
    checked = inspect_quote(row, symbol="600000", now=datetime.fromisoformat("2026-10-28T10:15:40+08:00"),
                            session="morning_session", expected_trade_date=date(2026, 10, 27))
    assert checked["usable_latest_quote"] is False
    assert "quote_price_missing_or_invalid" in checked["reason_codes"]


def test_rejected_refresh_does_not_claim_a_successful_refresh():
    deps = _deps(Path("."), network=True)
    deps = StockWorkbenchDeps(**{**deps.__dict__,
                                 "fetch_quote": lambda symbol: {"ok": True, "row": {
                                     "symbol": symbol, "price": 9.9, "price_basis": "raw",
                                     "quote_time": "2026-10-27 09:00:00",
                                     "source": "fixture.provider",
                                     "updated_at": "2026-10-27T09:00:05+08:00"}}})
    result = _client(deps).post("/api/stock/600000/quote-refresh",
                                json={"network": True}).get_json()
    assert result["ok"] is False
    assert result["status"] == "provider_failed"
    assert "已刷新" not in str(result.get("message_for_ui"))
