"""Independent original-contract probes; synthetic fixtures, no real providers."""
from __future__ import annotations

import json
from datetime import datetime as RealDatetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

UTC = timezone.utc
SH = ZoneInfo("Asia/Shanghai")
MIDNIGHT = RealDatetime(2026, 6, 3, 16, 30, tzinfo=UTC)
DAYTIME = RealDatetime(2026, 6, 4, 2, 30, tzinfo=UTC)
CASES = [(MIDNIGHT, UTC), (MIDNIGHT, SH), (DAYTIME, UTC)]
CASE_IDS = ["utc_host_shanghai_midnight", "shanghai_host_midnight_control", "utc_host_daytime_control"]


def seed_db(path: Path, adjustment: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    db = QuantDeskDB(path)
    db.initialize()
    try:
        DailyBarRepository(db).upsert_daily_bars([{
            "symbol": "600000", "date": "2026-06-02", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "amount": 1000,
        }], adjustment=adjustment)
    finally:
        db.close()


@pytest.mark.parametrize("adjustment", ["forward", "back", "raw"])
def test_c2_plain_identity_positive(tmp_path: Path, adjustment: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    path = tmp_path / "fixture.db"
    seed_db(path, adjustment)
    db = QuantDeskDB(path)
    try:
        rows = DailyBarRepository(db).query_daily_bars("600000", adjustment=adjustment)
        assert len(rows) == 1 and rows[0]["close"] == 10
    finally:
        db.close()


@pytest.mark.parametrize("adjustment", ["forward", "back", "raw"])
@pytest.mark.parametrize("alias", ["600000.SH", "sh600000"])
def test_c2_equivalent_alias_hits_same_sqlite_row(tmp_path: Path, adjustment: str, alias: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    path = tmp_path / "fixture.db"
    seed_db(path, adjustment)
    db = QuantDeskDB(path)
    try:
        repo = DailyBarRepository(db)
        assert len(repo.query_daily_bars("600000", adjustment=adjustment)) == 1
        rows = repo.query_daily_bars(alias, adjustment=adjustment)
    finally:
        db.close()
    assert len(rows) == 1 and rows[0]["close"] == 10, {"alias": alias, "adjustment": adjustment, "rows": rows}


@pytest.mark.parametrize("csv_present", [False, True])
def test_c2_csv_does_not_hide_alias_miss(tmp_path: Path, csv_present: bool) -> None:
    from quantdesk.data_store.dual_writer import read_daily_prefer_sqlite
    path = tmp_path / "fixture.db"
    seed_db(path, "raw")
    csv_path = tmp_path / "600000.csv" if csv_present else None
    if csv_path is not None:
        csv_path.write_text("date,close\n2026-06-02,20\n", encoding="utf-8")
    rows, meta = read_daily_prefer_sqlite("600000.SH", adjustment="raw", csv_path=csv_path,
                                        config={"db_path": str(path), "read_prefer_sqlite": True})
    assert meta["read_source"] == "sqlite", {"rows": rows, "meta": meta}
    assert meta["fallback_used"] is False
    assert len(rows) == 1 and rows[0]["close"] == 10


def host_clock(instant: RealDatetime, host_timezone: object) -> type[RealDatetime]:
    class FrozenDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromtimestamp(instant.timestamp(), tz=UTC)
            return value.astimezone(tz) if tz is not None else value.astimezone(host_timezone).replace(tzinfo=None)
    return FrozenDatetime


def dashboard_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    for name, payload in {
        "ledger.json": {"as_of": "2026-06-02", "cash": 1000, "equity": 1000, "positions": [], "trades": []},
        "journal_latest.json": {"as_of": "2026-06-02"},
        "plan_latest.json": {"intents": [], "committed": False},
        "philosophy.json": {}, "cycle_latest.json": {},
    }.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(module, "llm_runtime_descriptor", lambda: {})


@pytest.mark.parametrize("instant,host", CASES, ids=CASE_IDS)
def test_c3_original_minute_today_guard(monkeypatch: pytest.MonkeyPatch, instant, host) -> None:
    import quantdesk.intraday.intraday_providers as ip
    monkeypatch.setattr(ip, "datetime", host_clock(instant, host))
    result = ip._today_only_guard("sina_intraday", "600000", "2026-06-04")
    assert result is None, result
    assert ip._today_only_guard("sina_intraday", "600000", "2026-06-02")["status"] == "skipped"


@pytest.mark.parametrize("instant,host", CASES, ids=CASE_IDS)
def test_c3_dashboard_real_default_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instant, host) -> None:
    import ai_trader.dashboard as dash
    dashboard_fixture(tmp_path, monkeypatch, dash)
    monkeypatch.setattr(dash, "datetime", host_clock(instant, host))
    payload = dash.build_dashboard_payload(state_dir=tmp_path, results_dir=tmp_path)
    status = payload["schedule_status"]
    assert status["age_days"] == 2, status
    assert status["stale"] is True, status


@pytest.mark.parametrize("now", [MIDNIGHT, MIDNIGHT.astimezone(SH), RealDatetime(2026, 6, 4, 0, 30)])
def test_c3_dashboard_explicit_clock_positive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now) -> None:
    import ai_trader.dashboard as dash
    dashboard_fixture(tmp_path, monkeypatch, dash)
    status = dash.build_dashboard_payload(state_dir=tmp_path, results_dir=tmp_path, now=now)["schedule_status"]
    assert status["age_days"] == 2 and status["stale"] is True, status


@pytest.mark.parametrize("instant,host", CASES, ids=CASE_IDS)
def test_c3_original_news_status_get(monkeypatch: pytest.MonkeyPatch, instant, host) -> None:
    from flask import Flask
    import scripts.server.routes.news as news
    monkeypatch.setattr(news, "datetime", host_clock(instant, host))
    def no_external(*args, **kwargs):
        raise AssertionError("unexpected provider or refresh callback")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(news.create_news_blueprint(news.NewsRouteDeps(
        fetch_news=no_external, news_cache_stats=lambda: {"latest_date": "20260602"},
        refresh_single_news=no_external, normalize_symbol=str, request_truthy=bool,
    )))
    response = app.test_client().get("/api/news/status")
    assert response.status_code == 200
    assert response.get_json()["age_days"] == 2, response.get_json()


def sina_line(symbol: str, price: float) -> str:
    fields = ["0"] * 32
    fields[0] = "DETERMINISTIC_FIXTURE"
    for index in (1, 2, 3, 4, 5):
        fields[index] = str(price)
    fields[8], fields[9], fields[30], fields[31] = "100", "1000", "2026-06-04", "10:30:00"
    return f'var hq_str_{symbol}="{",".join(fields)}";'


@pytest.mark.parametrize("raw,plain", [("sh600000", "600000"), ("sz000001", "000001"), ("bj920010", "920010")])
def test_c1_real_parser_normal_quotes_positive(monkeypatch: pytest.MonkeyPatch, raw: str, plain: str) -> None:
    from quantdesk import data_providers as provider
    from quantdesk.intraday import intraday_monitor as monitor
    monkeypatch.setattr(provider, "_sina_http_get", lambda *a, **k: sina_line(raw, 5.0))
    quotes, errors, warnings = monitor._fetch_prices_detailed([plain])
    assert quotes[plain]["price"] == 5.0 and not errors


@pytest.mark.parametrize("malformed", ["hk920010", "bad920010", "bj1920010"])
def test_c1_malformed_response_cannot_overwrite_real_quote(monkeypatch: pytest.MonkeyPatch, malformed: str) -> None:
    from quantdesk import data_providers as provider
    from quantdesk.intraday import intraday_monitor as monitor
    raw_response = sina_line("bj920010", 5.0) + "\n" + sina_line(malformed, 99.0)
    monkeypatch.setattr(provider, "_sina_http_get", lambda *a, **k: raw_response)
    quotes, errors, warnings = monitor._fetch_prices_detailed(["920010"])
    assert quotes["920010"]["price"] == 5.0, {"malformed": malformed, "quotes": quotes, "warnings": warnings}


@pytest.mark.parametrize("symbol,expected", [
    ("600000", "sh600000"), ("000001", "sz000001"), ("920010", "bj920010"),
    ("sh000001", "sh000001"), ("900901.SH", "sh900901"),
])
def test_c5_public_realtime_preserves_explicit_market(monkeypatch: pytest.MonkeyPatch, symbol: str, expected: str) -> None:
    from quantdesk.providers import tencent_provider as provider
    urls = []
    def fixture_http(url, **kwargs):
        urls.append(url)
        return b""  # This probe verifies request routing, not remote quote availability.
    monkeypatch.setattr(provider, "_http_get_bytes", fixture_http)
    result = provider.fetch_realtime_quote([symbol], network=True)
    assert urls and expected in urls[0], {"symbol": symbol, "urls": urls, "result": result}


def test_c5_public_realtime_default_offline_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    from quantdesk.providers import tencent_provider as provider
    def forbidden(*args, **kwargs):
        raise AssertionError("offline caller attempted HTTP")
    monkeypatch.setattr(provider, "_http_get_bytes", forbidden)
    result = provider.fetch_realtime_quote(["600000"])
    assert result["status"] == "skipped" and result["failed_type"] == "network_disabled"
