import csv
import subprocess
import sys
from pathlib import Path

from quantdesk import full_market_catchup_updater, update_queue_builder
from quantdesk.full_market_catchup_updater import run_full_market_catchup


ROOT = Path(__file__).resolve().parents[1]


def _daily(path: Path, symbol: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,symbol,open,high,low,close,volume,amount,source,updated_at\n2026-05-13,%s,1,1,1,1,1,1,fixture,fixture\n" % symbol, encoding="utf-8")


def _session(root):
    return {"session": "post_close", "expected_latest_completed_trade_date": "2026-06-03", "latest_local_trade_date": "2026-05-13", "freshness_status": "stale"}


def test_v426_date_mismatch_does_not_write_daily(tmp_path, monkeypatch):
    path = tmp_path / "data/daily/000006.csv"
    _daily(path, "000006")
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(full_market_catchup_updater, "detect_market_session", _session)
    monkeypatch.setattr(update_queue_builder, "detect_market_session", _session)
    monkeypatch.setattr(full_market_catchup_updater, "coverage_record", lambda symbol, expected, **kwargs: {"symbol": symbol, "provider_attempted": True, "akshare_status": "ok", "tencent_status": "not_attempted", "selected_provider": "akshare_daily", "failure_reason": "", "data_date_returned": expected, "row_count_returned": 1, "usable_for_write": True, "skip_reason": ""})
    monkeypatch.setattr(full_market_catchup_updater, "fetch_bounded_daily_row", lambda symbol, expected, **kwargs: {"status": "ok", "provider_used": "akshare_daily", "row": {"date": "2026-06-02", "symbol": symbol, "open": "2", "high": "2", "low": "2", "close": "2", "volume": "1"}, "attempts": []})

    result = run_full_market_catchup(tmp_path, network=True, max_symbols=1, priority_only=False)

    assert result["execution"]["rows_written"] == 0
    assert path.read_text(encoding="utf-8") == before


def test_v426_cli_coverage_status_has_no_trade_advice():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/show_v426_update_coverage_status.py")], cwd=ROOT, text=True, capture_output=True, check=False)

    assert proc.returncode == 0
    text = proc.stdout.lower()
    assert "recommendation: buy" not in text
    assert "recommendation: sell" not in text
    assert "target price" not in text
    assert "broker" in text


def test_v426_canonical_equity_not_modified():
    proc = subprocess.run(["git", "status", "--porcelain=v1", "--", "results/portfolio_daily_equity_rebuild_latest.csv", "results/low_vol_canonical_daily_equity_latest.csv", "results/rev120_bcanonical_daily_equity_latest.csv"], cwd=ROOT, text=True, capture_output=True, check=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
