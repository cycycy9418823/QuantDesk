import csv
import json
import subprocess
import sys
from pathlib import Path
from tests._isolated_project import IsolatedProject

from quantdesk.bounded_market_data_provider import provider_config
import quantdesk.post_close_daily_updater as post_close_daily_updater
import quantdesk.intraday_snapshot_updater as intraday_snapshot_updater
from quantdesk.post_close_daily_updater import run_post_close_daily_update
from quantdesk.v425_provider_workflow import run_v425_provider_workflow


ROOT = Path(__file__).resolve().parents[1]


def _post_close_session(root):
    return {
        "session": "after_hours",
        "latest_local_trade_date": "2026-05-22",
        "expected_latest_completed_trade_date": "2026-06-03",
    }


def test_v425_provider_config_is_research_only():
    payload = provider_config(ROOT, max_symbols=1)

    assert payload["provider_type"] == "akshare"
    assert payload["research_only"] is True
    assert payload["no_broker"] is True
    assert payload["rate_limit_handling"]["max_symbols_per_run"] == 1


def test_v425_post_close_simulated_write_uses_staging_and_validation(tmp_path, monkeypatch):
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    path = daily / "000001.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "symbol", "open", "high", "low", "close", "volume", "amount", "source", "updated_at"])
        writer.writeheader()
        writer.writerow({"date": "2026-05-22", "symbol": "000001", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "amount": "1", "source": "fixture", "updated_at": "fixture"})

    monkeypatch.setattr(post_close_daily_updater, "detect_market_session", _post_close_session)
    payload = run_post_close_daily_update(tmp_path, network=True, max_symbols=1, provider="v425_simulated_provider", simulate=True, dry_run=False, write_mode="append_new_date")

    assert payload["rows_written"] == 1
    assert payload["symbols_updated"] == 1
    assert payload["protected_paths_status"] == "unchanged"
    rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
    assert rows[-1]["date"] == payload["expected_latest_completed_trade_date"]
    assert rows[-1]["source"] == "v425_simulated_provider"


def test_v425_post_close_real_provider_path_uses_staging_and_validation(tmp_path, monkeypatch):
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    path = daily / "000002.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "symbol", "open", "high", "low", "close", "volume", "amount", "source", "updated_at"])
        writer.writeheader()
        writer.writerow({"date": "2026-05-22", "symbol": "000002", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "amount": "1", "source": "fixture", "updated_at": "fixture"})

    def fake_fetch(symbol, expected_date, *, provider="akshare", timeout_seconds=30):
        return {
            "status": "ok",
            "provider_used": "akshare_daily",
            "row": {"date": expected_date, "symbol": symbol, "open": "2", "high": "2.1", "low": "1.9", "close": "2", "volume": "100", "amount": "200", "source": "akshare_daily", "updated_at": "test"},
            "attempts": [{"provider": "akshare_daily", "status": "ok"}],
        }

    monkeypatch.setattr(post_close_daily_updater, "fetch_bounded_daily_row", fake_fetch)
    monkeypatch.setattr(post_close_daily_updater, "detect_market_session", _post_close_session)
    payload = run_post_close_daily_update(tmp_path, network=True, max_symbols=1, provider="akshare", simulate=False, dry_run=False, write_mode="append_new_date")

    assert payload["rows_written"] == 1
    assert payload["real_provider_write"] is True
    assert payload["simulation"] is False
    rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
    assert rows[-1]["source"] == "akshare_daily"


def test_v424_post_close_update_respects_max_symbols_without_full_rescan(tmp_path, monkeypatch):
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    for symbol in ["000001", "000002", "000003"]:
        (daily / f"{symbol}.csv").write_text(f"date,symbol,close\n2026-06-09,{symbol},1\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        post_close_daily_updater,
        "detect_market_session",
        lambda root: {
            "session": "after_hours",
            "latest_local_trade_date": "2026-06-09",
            "expected_latest_completed_trade_date": "2026-06-09",
        },
    )

    def fake_read_last_date(path):
        calls.append(Path(path).name)
        return "2026-06-09"

    monkeypatch.setattr(post_close_daily_updater, "read_last_date", fake_read_last_date)

    payload = run_post_close_daily_update(tmp_path, network=True, max_symbols=1, provider="akshare", timeout_seconds=1)

    assert payload["status"] == "completed_no_new_rows"
    assert payload["symbols_attempted"] == 1
    assert payload["rows_skipped"] == 1
    assert len(calls) == 1


def test_v425_intraday_network_true_writes_snapshot_not_daily(tmp_path, monkeypatch):
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    (daily / "000002.csv").write_text("date,symbol,open,high,low,close,volume\n2026-06-02,000002,1,1,1,1,1\n", encoding="utf-8")

    monkeypatch.setattr(
        intraday_snapshot_updater,
        "detect_market_session",
        lambda root: {
            "session": "morning_session",
            "expected_latest_completed_trade_date": "2026-06-02",
            "latest_local_trade_date": "2026-06-02",
        },
    )
    monkeypatch.setattr(
        intraday_snapshot_updater,
        "fetch_realtime_quote",
        lambda symbols, network=True, timeout=30: {"status": "ok", "ok": True, "data": [{"symbol": "000002", "price": 3.21}], "errors": []},
    )

    payload = intraday_snapshot_updater.run_intraday_snapshot_update(tmp_path, network=True, max_symbols=1, provider="akshare")

    assert payload["status"] == "completed"
    assert payload["rows_written"] == 1
    assert payload["writes_data_daily"] is False
    assert (tmp_path / "data/intraday_snapshots/latest_snapshot.csv").exists()
    assert "3.21" in (tmp_path / "data/intraday_snapshots/latest_snapshot.csv").read_text(encoding="utf-8")
    assert (daily / "000002.csv").read_text(encoding="utf-8").count("\n") == 2


def test_v425_outputs_and_manifest_after_run(tmp_path):
    with IsolatedProject(ROOT, temp_parent=tmp_path) as project:
        root = project.root
        manifest_path = root / "dist/quantdesk_v4.2.5_provider_post_close_update_review_manifest.json"
        run_v425_provider_workflow(root, network=False, max_symbols=2)
        handoff = json.loads((root / "results/data_update/v425_provider_post_close_update_handoff_latest.json").read_text(encoding="utf-8-sig"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

        assert handoff["candidate_upgrade_allowed"] is False
        assert handoff["canonical_equity_overwritten"] is False
        assert handoff["generated_trade_advice"] is False
        assert manifest["missing_files"] == []
        assert manifest["research_only"] is True
