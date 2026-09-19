"""Offline, temporary-directory checks for the retained close snapshot writer."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts import run_daily_data_refresh as refresh
from scripts import run_data_close_pipeline as pipeline

TARGET = "2026-09-08"


class FakeLease:
    run_id = "test-close"
    # Mirror MarketWriterLease: family_benchmarks reads ``lease.target`` when no
    # explicit target_date is passed, so the fixture must carry it too.
    target = TARGET

    def __init__(self) -> None:
        self.active = True

    def assert_active(self) -> None:
        if not self.active:
            raise pipeline.MarketWriterUnauthorized("test_revoked")


def bar(day: str = TARGET, *, adjust: str = "raw", **changes: object) -> str:
    values = dict(zip(refresh.DAILY_HEADER.split(","), [
        day, "000001", "10", "11", "9", "10", "1000", "10000", "0", "0",
        adjust, "test_fixture", "2026-09-08T15:10:00+08:00",
    ]))
    values.update(changes)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="").writerow(values.values())
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, relative in {
        "PROJECT_ROOT": ".", "DAILY_DIR": "data/daily", "META_DIR": "data/metadata",
        "VAL_DIR": "data/metadata/valuation_snapshots", "EVIDENCE_DIR": "data/metadata/suspension_evidence",
        "RESULTS_DIR": "results/data_refresh", "NEWS_POOL_FILE": "config/news_pool.txt",
        "UNIVERSE_FILE": "data/universe/a_share_symbols.csv",
    }.items():
        monkeypatch.setattr(refresh, name, tmp_path / relative)
    monkeypatch.delenv(pipeline.MARKET_WRITER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(refresh.requests.Session, "request", lambda *a, **k: pytest.fail("real network forbidden"))


def test_raw_append_preserves_every_historical_byte(tmp_path: Path) -> None:
    path = tmp_path / "000001.csv"
    before = ("\ufeff" + refresh.DAILY_HEADER + "\r\n" + bar("2026-09-07") + "\r\n").encode("utf-8")
    path.write_bytes(before)
    assert refresh._append_daily_row(path, bar(), header=refresh.DAILY_HEADER, lease=FakeLease()) == (True, "")
    assert path.read_bytes().startswith(before)
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert [r["date"] for r in rows] == ["2026-09-07", TARGET]
    assert rows[-1]["pct_chg"] == "0"


@pytest.mark.parametrize("basis", ["qfq", "hfq", "", "unknown"])
def test_entire_original_basis_is_checked_before_any_date_operation(tmp_path: Path, basis: str) -> None:
    path = tmp_path / "000001.csv"
    before = (refresh.DAILY_HEADER + "\n" + bar("2026-09-03", adjust=basis) + "\n" + bar("2026-09-07") + "\n").encode()
    path.write_bytes(before)
    ok, reason = refresh._append_daily_row(path, bar(), header=refresh.DAILY_HEADER, lease=FakeLease())
    assert not ok and "history_basis_rejected" in reason
    assert path.read_bytes() == before


@pytest.mark.parametrize(("history", "reason"), [
    ([bar(TARGET), bar("2026-09-07")], "target_already_present"),
    ([bar("2026-09-07"), bar("2026-09-07")], "history_duplicate_dates"),
    ([bar("2026-09-09")], "append_would_backdate"),
    ([bar("2026-09-07", symbol="000002")], "history_symbol_mismatch"),
    ([bar("2026-02-30")], "history_date_invalid"),
    ([bar(TARGET, close="0")], "existing_target_invalid"),
    ([], "history_basis_rejected"),
])
def test_rejected_append_never_rewrites_history(tmp_path: Path, history: list[str], reason: str) -> None:
    path = tmp_path / "000001.csv"
    before = (refresh.DAILY_HEADER + "\n" + "\n".join(history)).encode()
    path.write_bytes(before)
    ok, detail = refresh._append_daily_row(path, bar(), header=refresh.DAILY_HEADER, lease=FakeLease())
    assert not ok and reason in detail
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [{"adjust": "qfq"}, {"volume": "0"}, {"high": "8"}, {"symbol": "000002"}])
def test_bad_incoming_row_does_not_create_a_file(tmp_path: Path, changes: dict) -> None:
    path = tmp_path / "000001.csv"
    assert refresh._append_daily_row(path, bar(**changes), header=refresh.DAILY_HEADER, lease=FakeLease())[0] is False
    assert not path.exists()


def test_revocation_before_replace_preserves_destination(tmp_path: Path) -> None:
    path = tmp_path / "existing.csv"
    path.write_bytes(b"original")
    lease = FakeLease()
    count = 0

    def assert_active() -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise pipeline.MarketWriterUnauthorized("revoked")

    lease.assert_active = assert_active
    with pytest.raises(pipeline.MarketWriterUnauthorized):
        refresh._atomic_bytes(path, b"replacement", lease=lease)
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


def quote_item() -> tuple[dict, dict]:
    item = {"code": "000001", "name": "fixture", "open": 10, "high": 11, "low": 9,
            "trade": 10, "volume": 1000, "amount": 10000, "pricechange": 0, "changepercent": 0}
    quote = {"symbol": "sz000001", "date": TARGET, "time": "15:00:00", "open": 10,
             "high": 11, "low": 9, "price": 10, "volume": 1000}
    return item, quote


@pytest.mark.parametrize("change", [
    {"date": "2026-09-07"}, {"time": "14:59:59"},
    {"time": None}, {"price": 10.1}, {"volume": 100}, {"high": float("inf")}, {},
])
def test_quote_date_time_and_values_must_match(change: dict) -> None:
    item, quote = quote_item()
    assert refresh._close_quote_matches(item, {**quote, **change}, TARGET) is (not change)


def test_only_completed_same_day_market_session_is_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    import quantdesk.market_clock as market_clock
    monkeypatch.setattr(market_clock, "load_cn_trading_calendar", lambda: {})
    monkeypatch.setattr(market_clock, "is_cn_a_share_trading_day", lambda *a, **k: True)
    assert refresh._complete_live_session(TARGET, datetime(2026, 9, 8, 15, 5))
    assert not refresh._complete_live_session(TARGET, datetime(2026, 9, 8, 15, 4))
    assert not refresh._complete_live_session("2026-09-07", datetime(2026, 9, 8, 16))
    monkeypatch.setattr(market_clock, "is_cn_a_share_trading_day", lambda *a, **k: False)
    assert not refresh._complete_live_session(TARGET, datetime(2026, 9, 8, 16))


def test_small_coverage_remains_visible_and_no_suspension_is_fabricated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item, quote = quote_item()
    invalid = {**item, "code": "000002", "volume": 0}
    monkeypatch.setattr(refresh, "_complete_live_session", lambda _: True)
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda _: [item, invalid])
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda _: {
        "000001": quote, "000002": {**quote, "symbol": "sz000002", "volume": 0},
    })
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["000001", "000002", "000003"])
    result = refresh.family_bars_and_valuation(TARGET, True, lease=FakeLease())
    assert result["status"] == "completed_with_warnings"
    assert result["bars"]["appended"] == 1
    assert result["bars"]["suspended_verified"] == 0
    assert not (refresh.DAILY_DIR / "000002.csv").exists()
    assert not refresh.EVIDENCE_DIR.exists()
    review = json.loads((tmp_path / result["review_evidence"]).read_text(encoding="utf-8"))
    assert review["verified_exclusions"] == {}
    assert review["missing_in_snapshot"] == ["000003"]
    assert "000002" in review["flagged_for_review"]
    rows = list(csv.DictReader(io.StringIO((refresh.DAILY_DIR / "000001.csv").read_text())))
    assert float(rows[0]["pct_chg"]) == 0
    assert float(rows[0]["change"]) == 0


@pytest.mark.parametrize("network", [False, True])
def test_offline_or_run_id_alone_has_zero_network_and_writes(tmp_path: Path, capsys, network: bool) -> None:
    args = ["--pipeline-run-id", "looks-valid", "--target-date", TARGET]
    if network:
        args += ["--enable-network"]
    code = refresh.main(args)
    result = json.loads(capsys.readouterr().out)
    assert code == (5 if network else 0)
    assert result["status"] == ("blocked" if network else "planned_offline")
    assert result["canonical_write"] is False
    assert not list(tmp_path.iterdir())


def test_revoked_benchmark_keeps_original_bytes(tmp_path: Path, monkeypatch) -> None:
    from quantdesk.data_truth import benchmark_incremental_update as benchmark
    path = tmp_path / "data/benchmark/hs300.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    lease = FakeLease()

    def update(root, **kwargs):
        (root / "data/benchmark/hs300.csv").write_bytes(b"updated")
        lease.active = False
        return {"results": [{"path": "hs300.csv", "status": "appended"}]}

    monkeypatch.setattr(benchmark, "update_benchmark_files", update)
    with pytest.raises(pipeline.MarketWriterUnauthorized):
        refresh.family_benchmarks(True, lease=lease)
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("revoked", [False, True])
def test_failed_or_revoked_news_cannot_refresh_cache(tmp_path: Path, monkeypatch, revoked: bool) -> None:
    from quantdesk.providers import news_provider
    refresh.NEWS_POOL_FILE.parent.mkdir(parents=True)
    refresh.NEWS_POOL_FILE.write_text("000001\n")
    cached = tmp_path / "data/news/20260907_000001.csv"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old cache")
    lease = FakeLease()

    def fetch(*args):
        if not revoked:
            raise TimeoutError("offline test")
        lease.active = False
        return [{"title": "fixture", "content": "", "published": TARGET, "source": "fixture", "url": ""}]

    monkeypatch.setattr(news_provider, "_fetch_announce_eastmoney", fetch)
    monkeypatch.setattr(refresh.time, "sleep", lambda _: None)
    if revoked:
        with pytest.raises(pipeline.MarketWriterUnauthorized):
            refresh.family_news(True, lease=lease)
    else:
        result = refresh.family_news(True, lease=lease)
        assert result["status"] == "partial" and result["refreshed"] == 0
        assert result["failure_details"] == [{"symbol": "000001", "reason": "TimeoutError"}]
    assert cached.read_bytes() == b"old cache"
    assert list(cached.parent.iterdir()) == [cached]
