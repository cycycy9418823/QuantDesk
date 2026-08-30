import csv
from pathlib import Path

from quantdesk import full_market_catchup_updater, update_queue_builder
from quantdesk.full_market_catchup_updater import run_full_market_catchup
from quantdesk.update_queue_builder import build_update_queue


def _daily(path: Path, symbol: str, date: str = "2026-05-13") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "symbol", "open", "high", "low", "close", "volume", "amount", "source", "updated_at"])
        writer.writeheader()
        writer.writerow({"date": date, "symbol": symbol, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "amount": "1", "source": "fixture", "updated_at": "fixture"})


def _session(root):
    return {"session": "post_close", "expected_latest_completed_trade_date": "2026-06-03", "latest_local_trade_date": "2026-05-13", "freshness_status": "stale"}


def test_v426_update_queue_can_be_generated(tmp_path, monkeypatch):
    _daily(tmp_path / "data/daily/000006.csv", "000006")
    monkeypatch.setattr(update_queue_builder, "detect_market_session", _session)

    payload = build_update_queue(tmp_path, max_symbols=5, priority_only=False)

    assert payload["selected_queue_symbols"] == 1
    assert payload["queue"][0]["update_required"] is True
    assert payload["queue"][0]["priority_group"] == "P2"


def test_v426_network_false_does_not_call_provider(tmp_path, monkeypatch):
    _daily(tmp_path / "data/daily/000006.csv", "000006")
    monkeypatch.setattr(full_market_catchup_updater, "detect_market_session", _session)
    monkeypatch.setattr(update_queue_builder, "detect_market_session", _session)

    result = run_full_market_catchup(tmp_path, network=False, max_symbols=1, priority_only=False)

    assert result["execution"]["symbols_attempted"] == 0
    assert result["execution"]["rows_written"] == 0
    assert result["coverage"]["records"][0]["provider_attempted"] is False


def test_v426_single_symbol_failure_does_not_abort_batch(tmp_path, monkeypatch):
    _daily(tmp_path / "data/daily/000004.csv", "000004")
    _daily(tmp_path / "data/daily/000006.csv", "000006")
    monkeypatch.setattr(full_market_catchup_updater, "detect_market_session", _session)
    monkeypatch.setattr(update_queue_builder, "detect_market_session", _session)

    def fake_coverage(symbol, expected, **kwargs):
        return {"symbol": symbol, "provider_attempted": True, "akshare_status": "failed", "tencent_status": "ok" if symbol == "000006" else "failed", "selected_provider": "tencent_daily" if symbol == "000006" else "", "failure_reason": "" if symbol == "000006" else "both failed", "data_date_returned": expected if symbol == "000006" else "", "row_count_returned": 1 if symbol == "000006" else 0, "usable_for_write": symbol == "000006", "skip_reason": ""}

    def fake_fetch(symbol, expected, **kwargs):
        return {"status": "ok", "provider_used": "tencent_daily", "row": {"date": expected, "symbol": symbol, "open": "2", "high": "2", "low": "2", "close": "2", "volume": "10", "amount": "20", "source": "tencent_daily", "updated_at": "test"}, "attempts": []}

    monkeypatch.setattr(full_market_catchup_updater, "coverage_record", fake_coverage)
    monkeypatch.setattr(full_market_catchup_updater, "fetch_bounded_daily_row", fake_fetch)
    result = run_full_market_catchup(tmp_path, network=True, max_symbols=2, priority_only=False)

    assert result["execution"]["symbols_attempted"] == 2
    assert result["execution"]["symbols_updated"] == 1
    assert result["execution"]["provider_both_failed"] == 1
    assert result["execution"]["status"] == "completed_with_warnings"
