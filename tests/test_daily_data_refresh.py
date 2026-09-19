"""Offline, append-only and alert-wiring contracts for the daily fast path."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts import run_daily_data_refresh as refresh
from scripts.run_data_close_pipeline import MarketWriterUnauthorized


class FakeLease:
    """Stand-in for MarketWriterLease.

    The family functions call ``lease.assert_active()`` and read ``lease.target``;
    a revoked lease must abort the write exactly like the real one does.
    """

    run_id = "test-refresh"

    def __init__(self, target: str = "2026-09-01") -> None:
        self.target = target
        self.active = True

    def assert_active(self) -> None:
        if not self.active:
            raise MarketWriterUnauthorized("test_lease_revoked")

    def __enter__(self) -> "FakeLease":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_daily_append_preserves_existing_bytes_and_header_meaning(tmp_path: Path):
    path = tmp_path / "600000.csv"
    original = refresh.DAILY_HEADER + "\n2026-08-31,600000,10,10,10,10,100,1000,0,0,raw,existing,2026-08-31T16:00:00\n"
    path.write_text(original, encoding="utf-8")

    ok, reason = refresh._append_daily_row(
        path,
        "2026-09-01,600000,11,11,11,11,200,2200,1,10,raw,sina_spot_close,2026-09-01T16:00:00",
        header=refresh.DAILY_HEADER,
        lease=FakeLease(),
    )

    assert ok and reason == ""
    after = path.read_text(encoding="utf-8")
    assert after.startswith(original)
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[-1]["source"] == "sina_spot_close"
    assert rows[-1]["updated_at"] == "2026-09-01T16:00:00"


def test_daily_append_rejects_unknown_header_without_rewrite(tmp_path: Path):
    path = tmp_path / "600000.csv"
    original = "date,symbol,unexpected\n2026-08-31,600000,x\n"
    path.write_text(original, encoding="utf-8")

    ok, reason = refresh._append_daily_row(
        path,
        "2026-09-01,600000,11,11,11,11,200,2200,1,10,raw,sina_spot_close,2026-09-01T16:00:00",
        header=refresh.DAILY_HEADER,
        lease=FakeLease(),
    )

    assert not ok
    assert reason == "daily_header_mismatch:600000.csv"
    assert path.read_text(encoding="utf-8") == original


def test_daily_append_rechecks_target_date_inside_symbol_lock(tmp_path: Path):
    path = tmp_path / "600000.csv"
    original = refresh.DAILY_HEADER + "\n2026-09-01,600000,10,10,10,10,100,1000,0,0,raw,existing,2026-09-01T16:00:00\n"
    path.write_text(original, encoding="utf-8")

    ok, reason = refresh._append_daily_row(
        path,
        "2026-09-01,600000,11,11,11,11,200,2200,1,10,raw,sina_spot_close,2026-09-01T16:01:00",
        header=refresh.DAILY_HEADER,
        lease=FakeLease(),
    )

    assert not ok
    assert reason == "daily_target_already_present:600000.csv:2026-09-01"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("snapshot_raises", [False, True])
def test_market_snapshot_session_is_always_closed(tmp_path, monkeypatch, snapshot_raises):
    """The requests session must be closed even when the snapshot blows up."""

    class FakeSession:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(refresh.requests, "Session", lambda: session)
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    # An empty roster keeps the empty-snapshot fallback offline and file-free.
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: [])

    def fake_snapshot(_session):
        if snapshot_raises:
            raise RuntimeError("snapshot boom")
        return []

    monkeypatch.setattr(refresh, "fetch_market_snapshot", fake_snapshot)
    if snapshot_raises:
        with pytest.raises(RuntimeError, match="snapshot boom"):
            refresh.family_bars_and_valuation("2026-09-01", True, lease=FakeLease())
    else:
        result = refresh.family_bars_and_valuation("2026-09-01", True, lease=FakeLease())
        # An empty list snapshot now falls back to the dated-HQ path; with no
        # roster it must stop instead of inventing bars.
        assert result["ok"] is False
        assert result["reason"] == "snapshot_empty_and_universe_missing"
    assert session.closed is True


def test_bars_family_writes_valid_bar_and_keeps_zero_trade_as_evidence(tmp_path, monkeypatch):
    daily = tmp_path / "data/daily"
    valuation = tmp_path / "data/metadata/valuation_snapshots"
    evidence = tmp_path / "data/metadata/suspension_evidence"
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    daily.mkdir(parents=True)
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n000635\n", encoding="utf-8")
    monkeypatch.setattr(refresh, "DAILY_DIR", daily)
    monkeypatch.setattr(refresh, "VAL_DIR", valuation)
    monkeypatch.setattr(refresh, "EVIDENCE_DIR", evidence)
    monkeypatch.setattr(refresh, "UNIVERSE_FILE", universe)
    monkeypatch.setattr(refresh, "META_DIR", tmp_path / "data/metadata")
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    # A dated-HQ close quote is now the mandatory close evidence: it must agree
    # with the list row, otherwise the symbol is not written at all.
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {
        "600000": {"date": "2026-09-01", "time": "15:00:03", "open": "10", "high": "10.2",
                   "low": "9.8", "price": "10.1", "volume": "1000"},
        "000635": {"date": "2026-09-01", "time": "15:00:03", "open": "0", "high": "0",
                   "low": "0", "price": "0", "volume": "0"},
    })

    valid = {
        "code": "600000",
        "name": "浦发银行",
        "open": "10",
        "high": "10.2",
        "low": "9.8",
        "trade": "10.1",
        "volume": "1000",
        "amount": "10100",
        "pricechange": "0.1",
        "changepercent": "1",
        "per": "5",
        "pb": "1",
        "mktcap": "1000",
    }
    zero_trade = {
        "code": "000635",
        "name": "停牌样本",
        "open": "0",
        "high": "0",
        "low": "0",
        "trade": "0",
        "volume": "0",
        "amount": "0",
        "per": "5",
        "pb": "1",
        "mktcap": "1000",
    }
    monkeypatch.setattr(
        refresh,
        "fetch_market_snapshot",
        lambda _session: [valid] + [zero_trade] * 4999,
    )

    result = refresh.family_bars_and_valuation("2026-09-01", True, lease=FakeLease())

    assert result["ok"] is True
    assert result["bars"]["appended"] == 1
    assert "2026-09-01,600000" in (daily / "600000.csv").read_text()
    assert not (daily / "000635.csv").exists()
    # A zero-trade row is evidence, never a bar and never a suspension verdict.
    review_dir = tmp_path / "data/metadata/daily_refresh_review"
    payload = json.loads((review_dir / "20260901.json").read_text())
    assert "000635" in payload["flagged_for_review"]
    assert payload["verified_exclusions"] == {}
    assert result["suspension_evidence"] == ""
    assert payload["contract"] == "Missing or invalid data remains missing; no suspension is inferred."


def test_invalid_ohlc_with_real_volume_is_reviewed_not_called_suspended(tmp_path, monkeypatch):
    daily = tmp_path / "data/daily"
    valuation = tmp_path / "data/metadata/valuation_snapshots"
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    daily.mkdir(parents=True)
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n", encoding="utf-8")
    monkeypatch.setattr(refresh, "DAILY_DIR", daily)
    monkeypatch.setattr(refresh, "VAL_DIR", valuation)
    monkeypatch.setattr(refresh, "META_DIR", tmp_path / "data/metadata")
    monkeypatch.setattr(refresh, "UNIVERSE_FILE", universe)
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    # Quote agrees with the list row, so the row reaches the OHLC validity check.
    monkeypatch.setattr(refresh, "_dated_close_quotes", lambda symbols: {
        "600000": {"date": "2026-09-01", "time": "15:00:03", "open": "10", "high": "9",
                   "low": "8", "price": "10", "volume": "1000"},
    })
    malformed = {
        "code": "600000",
        "name": "坏行情样本",
        "open": "10",
        "high": "9",
        "low": "8",
        "trade": "10",
        "volume": "1000",
        "amount": "10000",
        "per": "5",
        "pb": "1",
        "mktcap": "1000",
    }
    # 5000 distinct codes: the malformed one must be judged on its OHLC, not on
    # a duplicate-symbol rule.
    snapshot = [malformed] + [
        {**malformed, "code": f"{i:06d}", "name": f"样本{i}"} for i in range(1, 5000)
    ]
    monkeypatch.setattr(
        refresh,
        "fetch_market_snapshot",
        lambda _session: snapshot,
    )

    result = refresh.family_bars_and_valuation("2026-09-01", True, lease=FakeLease())

    review_dir = tmp_path / "data/metadata/daily_refresh_review"
    payload = json.loads((review_dir / "20260901.json").read_text())
    assert "600000" not in payload["verified_exclusions"]
    assert payload["flagged_for_review"]["600000"]["reason"].endswith("high_below_body")
    assert not (daily / "600000.csv").exists()
    assert result["suspension_evidence"] == ""


def test_daily_refresh_default_offline_writes_only_report(tmp_path, monkeypatch, capsys):
    """Offline default emits a report and touches no writer at all.

    Historical name: "report" now means the stdout payload. The offline path
    short-circuits before the lease is taken, so no family runs and no artifact
    is written -- that is the stronger invariant this test now pins.
    """

    results = tmp_path / "results/data_refresh"
    monkeypatch.setattr(refresh, "RESULTS_DIR", results)
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        refresh,
        "family_bars_and_valuation",
        lambda target, network, **kwargs: calls.append(("bars", network)) or {"ok": False, "status": "skipped_network_false"},
    )
    monkeypatch.setattr(
        refresh,
        "family_benchmarks",
        lambda network, **kwargs: calls.append(("benchmarks", network)) or {"ok": False, "status": "skipped_network_false"},
    )
    monkeypatch.setattr(
        refresh,
        "family_news",
        lambda network, **kwargs: calls.append(("news", network)) or {"ok": False, "status": "skipped_network_false"},
    )
    monkeypatch.setattr(
        refresh,
        "family_quarterly",
        lambda network, force, **kwargs: calls.append(("quarterly", network)) or {"ok": False, "status": "skipped_network_false"},
    )

    code = refresh.main(["--target-date", "2026-09-01", "--json", "true"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert calls == []
    assert payload["status"] == "planned_offline"
    assert payload["network"] is False
    assert payload["canonical_write"] is False
    assert not (results / "data_refresh_latest.json").exists()


def test_daily_refresh_alert_flags_carry_no_write_authority(tmp_path, monkeypatch, capsys):
    """Notification delivery moved to the Pipeline; these flags grant nothing here."""

    results = tmp_path / "results/data_refresh"
    monkeypatch.setattr(refresh, "RESULTS_DIR", results)
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    # Structural guard: this runner must not even be able to reach a notifier.
    assert not hasattr(refresh, "dispatch_intraday_alerts")
    monkeypatch.setattr(refresh, "family_bars_and_valuation", lambda target, network, *, lease: {"ok": True})
    monkeypatch.setattr(refresh, "family_benchmarks", lambda network, *, lease, target_date=None: {"ok": True})
    monkeypatch.setattr(refresh, "family_news", lambda network, *, lease: {"ok": True})
    monkeypatch.setattr(refresh, "family_quarterly", lambda network, force, *, lease: {"ok": True})
    monkeypatch.setattr(refresh, "MarketWriterLease", lambda *a, **k: FakeLease(str(k.get("target", "") or "")))
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)

    code = refresh.main(
        [
            "--enable-network",
            "--enable-alert-network",
            "--alert-push",
            "--alert-dry-run",
            "false",
            "--target-date",
            "2026-09-01",
            "--json",
            "true",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "notification_flags_ignored:use_pipeline_health_alert" in payload["notes"]
    assert payload["canonical_write"] is True
    # Preserve the DS side's assertion on the persisted artifact as well.
    persisted = json.loads((results / "data_refresh_latest.json").read_text())
    assert "notification_flags_ignored:use_pipeline_health_alert" in persisted["notes"]


def test_intraday_cli_keeps_alert_network_authority_separate(monkeypatch):
    from scripts import run_intraday_data_refresh as runner

    # The round also feeds the AI books; a test must never write real book state.
    monkeypatch.setattr(runner, "observe_all_profiles", lambda root: ([], {}, {}))
    monkeypatch.setattr(
        runner,
        "collect_intraday_refresh",
        lambda root, *, network, max_round_seconds: {"status": "ready", "network": network},
    )
    captured = {}
    monkeypatch.setattr(
        runner,
        "dispatch_intraday_alerts",
        lambda root, **kwargs: captured.update(kwargs) or {"status": "sent"},
    )

    code = runner.main(
        [
            "--enable-network",
            "--enable-alert-network",
            "--alert-push",
            "--alert-dry-run",
            "false",
            "--json",
            "false",
        ]
    )

    assert code == 0
    assert captured["network"] is True
    assert captured["push"] is True
    assert captured["dry_run"] is False
