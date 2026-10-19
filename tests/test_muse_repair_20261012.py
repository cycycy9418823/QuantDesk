"""Opening-slot failures must leave budget and evidence for a real fallback."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from quantdesk import data_providers, snapshot_history as history
from scripts import run_muse_intraday_trial as trial
from tests.test_muse_intraday_trial_20261010 import config_at, stamp


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def quote(symbol="600000", **changes):
    return {"symbol": symbol, "price": 10, "date": "2026-10-12", "time": "09:30:10",
            "price_basis": "raw", **changes}


@pytest.mark.parametrize("failure", ["empty", "timeout", "partial"])
def test_opening_failure_keeps_budget_for_another_endpoint(failure):
    clock = Clock()
    calls = []

    def primary(*, budget_seconds):
        calls.append(("hq", budget_seconds))
        clock.value += budget_seconds
        if failure == "timeout":
            raise TimeoutError("opening request timed out")
        return ([quote()] if failure == "partial" else []), "sina.hq_batch"

    def secondary(*, budget_seconds):
        calls.append(("list", budget_seconds))
        clock.value += 20
        return [quote("600000", price=11), quote("000001", price=12)], "sina.spot_direct"

    rows, source = history.resilient_network_fetch(
        ["600000", "000001"], clock=clock, budget_seconds=120, trade_date="2026-10-12",
        primary=primary, secondary=secondary,
    )
    assert calls == [("hq", 30), ("list", 90)]
    assert source == "sina.spot_direct" and [r["price"] for r in rows] == [11, 12]
    assert rows.provider_errors[0]["outcome"] != "ok"
    assert clock.value == 50


def test_two_incomplete_sources_preserve_one_capture_and_all_failures():
    clock = Clock()
    rows, source = history.resilient_network_fetch(
        ["600000", "000001", "600519"], clock=clock, budget_seconds=60, trade_date="2026-10-12",
        primary=lambda **kw: ([quote(), quote("000001")], "sina.hq_batch"),
        secondary=lambda **kw: ([quote("600519")], "sina.spot_direct"),
    )
    assert source == "sina.hq_batch"
    assert {r["symbol"] for r in rows} == {"600000", "000001"}
    assert len(rows.provider_errors) == 2
    assert all(e["outcome"] == "partial" for e in rows.provider_errors)


def test_no_budget_does_not_start_either_endpoint():
    def forbidden(**kwargs):
        pytest.fail("provider was called without budget")
    rows, _ = history.resilient_network_fetch(["600000"], budget_seconds=0, primary=forbidden, secondary=forbidden)
    assert not rows and len(rows.provider_errors) == 2


def test_known_empty_slot_can_retry_once_with_original_budget_and_failure_saved(tmp_path, monkeypatch):
    config, _, _ = config_at(tmp_path)
    finish = stamp("2026-10-12T09:30:30")
    monkeypatch.setattr(trial, "local_now", lambda: finish)
    slot = "2026-10-12T0930"
    first = trial.run_once(config, "snapshot", slot, now=stamp("2026-10-12T09:30:00"),
                           collector=lambda *a: {"status": "provider_empty", "row_count": 0})
    path = trial.receipt_path(config, "snapshot", slot)
    original = path.read_bytes()
    assert first["retryable"] and first["terminal"] is False
    budgets = []

    def succeed(conf, *_):
        budgets.append(conf["snapshot_budget_seconds"])
        return {"status": "written", "row_count": 2}

    finish = stamp("2026-10-12T09:31:20")
    second = trial.run_once(config, "snapshot", slot, now=stamp("2026-10-12T09:31:05"), collector=succeed)
    assert budgets == [85]
    assert second["attempt"] == 2 and second["terminal"] and second["collection_succeeded"]
    assert (path.parent / "attempts/snapshot_0930.1.json").read_bytes() == original
    assert trial.run_once(config, "snapshot", slot, now=finish, collector=succeed)["status"] == "skipped"
    assert budgets == [85]


def test_past_slot_and_existing_archive_are_never_retried(tmp_path, monkeypatch):
    config, _, _ = config_at(tmp_path)
    monkeypatch.setattr(trial, "local_now", lambda: stamp("2026-10-12T09:30:20"))
    slot = "2026-10-12T0930"
    first = trial.run_once(config, "snapshot", slot, now=stamp("2026-10-12T09:30:00"),
                           collector=lambda *a: {"status": "provider_empty", "row_count": 0})
    assert trial.snapshot_retry_budget(config, slot, first, stamp("2026-10-12T09:35:00")) == 0
    archive = tmp_path / "project/data/snapshots/2026-10-12/0930.csv"
    archive.parent.mkdir(parents=True)
    archive.write_text("old immutable capture", encoding="utf-8")
    assert trial.snapshot_retry_budget(config, slot, first, stamp("2026-10-12T09:30:30")) == 0


def test_generic_normalizer_never_claims_raw_or_supplies_a_date():
    rows = history.normalize_snapshot_rows(
        [{"symbol": "600000", "price": 10, "ticktime": "09:30:10"}], source="sina.spot_direct",
        fetched_at=stamp("2026-10-12T09:30:20"), trade_date="2026-10-12", slot_time="0930",
    )
    assert rows[0]["quote_time"] == "09:30:10"
    assert rows[0]["quote_date"] == rows[0]["price_basis"] == ""
    quality = history._quote_field_quality(rows, "2026-10-12")
    assert quality["quote_date_missing_count"] == quality["price_basis_unverified_count"] == 1
    assert quality["quality_status"] == "partial"


def test_live_quote_parser_declares_raw_without_manufacturing_dates():
    fields = ["fixture", "10", "9", "10", "10", "9"] + ["0"] * 24 + ["", "09:30:10", "00"]
    rows = data_providers.parse_sina_quote_text('var hq_str_sh600000="' + ",".join(fields) + '";')
    assert rows[0].to_dict()["price_basis"] == "raw"
    assert rows[0].date == "" and rows[0].time == "09:30:10"


def test_archive_quality_and_source_manifest_remain_visible(tmp_path):
    config, _, _ = config_at(tmp_path)
    result = history.collect_market_snapshot(
        config["root"], network=True, now=stamp("2026-10-12T09:30:00"),
        clock=lambda: stamp("2026-10-12T09:30:20"), symbols=["600000"],
        fetcher=lambda **kwargs: ([quote(date="")], "sina.spot_direct"),
        provenance={"source_sha256": config["source_sha256"]},
    )
    metadata = json.loads((tmp_path / "project/data/snapshots/2026-10-12/0930.json").read_text())
    assert metadata["quote_date_missing_count"] == 1
    assert metadata["quality_status"] == "partial"
    assert metadata["producer_provenance"]["source_sha256"] == config["source_sha256"]
    assert result["written"]
    assert result["day_summary"]["field_verified_complete_slot_count"] == 0
    assert result["day_summary"]["quote_date_missing_count"] == 1


def test_invalid_slot_is_rejected_before_any_receipt_path(tmp_path, monkeypatch):
    config, _, _ = config_at(tmp_path)
    monkeypatch.setattr(trial, "receipt_path", lambda *args: pytest.fail("invalid slot reached file access"))
    result = trial.run_once(config, "snapshot", "../../outside", now=stamp("2026-10-12T09:30:00"))
    assert result["status"] == "skipped" and result["network_attempted"] is False


def test_corrupt_receipt_cannot_authorize_replay(tmp_path):
    config, _, _ = config_at(tmp_path)
    path = trial.receipt_path(config, "snapshot", "2026-10-12T0930")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{broken previous attempt")
    result = trial.run_once(
        config, "snapshot", "2026-10-12T0930", now=stamp("2026-10-12T09:30:30"),
        collector=lambda *args: pytest.fail("corrupt receipt triggered collection"),
    )
    assert result["status"] == "already_attempted"
    assert path.read_bytes() == b"{broken previous attempt"


@pytest.mark.parametrize("primary_quote", [{"price": 0}, {"date": "2026-10-09"}])
def test_full_identity_coverage_with_no_usable_quote_still_tries_alternate(primary_quote):
    calls = []

    def secondary(*, budget_seconds):
        calls.append(budget_seconds)
        return [quote()], "sina.spot_direct"

    rows, source = history.resilient_network_fetch(
        ["600000"], trade_date="2026-10-12", clock=lambda: 0.0,
        primary=lambda **kw: ([{**quote(), **primary_quote}], "sina.hq_batch"), secondary=secondary,
    )
    assert calls and source == "sina.spot_direct" and rows
    assert rows.provider_errors[0]["outcome"] == "unusable"
