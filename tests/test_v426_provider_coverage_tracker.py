from quantdesk import provider_coverage_tracker


def test_v426_provider_coverage_records_fallback(monkeypatch):
    def fake_fetch(symbol, expected_date, *, provider="akshare", timeout_seconds=30):
        return {
            "status": "ok",
            "provider_used": "tencent_daily",
            "row": {"date": expected_date, "symbol": symbol},
            "attempts": [
                {"provider": "akshare_daily", "status": "failed", "error": "remote disconnect"},
                {"provider": "tencent_daily", "status": "ok", "warnings": []},
            ],
        }

    monkeypatch.setattr(provider_coverage_tracker, "fetch_bounded_daily_row", fake_fetch)
    row = provider_coverage_tracker.coverage_record("000006", "2026-06-03", network=True)

    assert row["akshare_status"] == "failed"
    assert row["tencent_status"] == "ok"
    assert row["selected_provider"] == "tencent_daily"
    assert row["usable_for_write"] is True


def test_v426_provider_coverage_network_false_skips_provider():
    row = provider_coverage_tracker.coverage_record("000006", "2026-06-03", network=False)

    assert row["provider_attempted"] is False
    assert row["akshare_status"] == "skipped_network_false"
