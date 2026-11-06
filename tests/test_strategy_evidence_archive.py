"""Original bytes, entry/add/reduction/exit chains and capture failures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk.daily_evidence_contract import build_requirements, digest, json_bytes, load_ref
from quantdesk.strategy_evidence_archive import archive_strategy_review, inspect_strategy_archive
from test_daily_evidence_contract import CODE, TARGET, fixture_project, put, snapshot


def archive(root: Path, before: bytes, after: bytes, target=TARGET, run="synthetic-review", status="completed", stamp=None):
    return archive_strategy_review(root, target_date=target, run_id=run, before_raw=before, after_raw=after,
        started_at=(stamp or target)+"T20:00:00+08:00", recorded_at=(stamp or target)+"T20:01:00+08:00",
        process_status=status, requirements=build_requirements(root, target), assert_write=lambda: None)


def test_original_actions_books_and_price_bytes_survive_later_append(tmp_path):
    book = fixture_project(tmp_path, quantity=0)
    before = json_bytes(book)
    action = {"action": "buy", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10,
              "decision_date": TARGET, "original_naive_time": TARGET+"T20:00:00", "original_reason": "synthetic fixture"}
    book.update(positions=[{"symbol": CODE, "strategy_type": "long", "quantity": 100, "entry_date": TARGET}], actions=[action])
    after = put(tmp_path, "state/auto_paper_portfolio.json", book)
    prices = tmp_path / f"data/daily/{CODE}.csv"
    original_price_bytes = prices.read_bytes()
    result = archive(tmp_path, before, after)
    doc = json.loads((tmp_path / result["manifest_path"]).read_bytes())
    assert result["status"] == "ready"
    assert load_ref(tmp_path, doc["after_state"]) == after
    assert doc["trade_events"][0]["original_action"] == action
    prices.write_bytes(original_price_bytes + f"2026-11-02,{CODE},11,11,11,11,100,raw,synthetic_test_fixture\n".encode())
    assert load_ref(tmp_path, doc["price_sources"][CODE]) == original_price_bytes
    assert (tmp_path / "state/auto_paper_portfolio.json").read_bytes() == after
    assert doc["trade_events"][0]["recorded_at"].endswith("+08:00")
    assert "timezone" not in doc["trade_events"][0]["original_action"]


def test_open_add_partial_exit_and_terminal_exit_keep_original_links(tmp_path):
    previous = fixture_project(tmp_path, target="2026-10-26", quantity=0)
    manifests = []
    quantities = [100, 200, 100, 0]
    for index, (target, action_name, final_qty) in enumerate(zip(
            ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"], ["buy", "add", "reduce_half", "sell"], quantities)):
        before = (tmp_path / "state/auto_paper_portfolio.json").read_bytes()
        price_path = tmp_path / f"data/daily/{CODE}.csv"
        if target not in price_path.read_text():
            with price_path.open("a") as handle:
                handle.write(f"{target},{CODE},10,10,10,10,100,raw,synthetic_test_fixture\n")
        put(tmp_path, f"results/candidate_refresh/daily/{target}.json", {"target_date": target, "candidates": [{"symbol": CODE}]})
        action = {"action": action_name, "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": target}
        previous = {**previous, "valuation_date": target, "actions": [action], "positions":
            [{"symbol": CODE, "strategy_type": "long", "quantity": final_qty, "entry_date": "2026-10-26"}] if final_qty else []}
        after = put(tmp_path, "state/auto_paper_portfolio.json", previous)
        result = archive(tmp_path, before, after, target, f"synthetic-{index}")
        assert result["status"] == "ready"
        manifests.append(json.loads((tmp_path / result["manifest_path"]).read_bytes()))
    events = [doc["trade_events"][0] for doc in manifests]
    assert [event["link_kind"] for event in events] == ["open", "add", "partial_exit", "full_exit"]
    assert events[2]["linked_open_event_ids"] == [events[0]["event_id"], events[1]["event_id"]]
    assert events[3]["linked_open_event_ids"] == events[2]["linked_open_event_ids"]
    assert events[3]["linked_reduction_event_ids"] == [events[2]["event_id"]]
    assert all(event["linkage_verified"] for event in events)
    assert manifests[-1]["open_lots_after"] == {}


def test_same_run_is_idempotent_and_does_not_rewrite_record_time(tmp_path):
    book = fixture_project(tmp_path)
    raw = json_bytes(book)
    first = archive(tmp_path, raw, raw)
    original = snapshot(tmp_path)
    second = archive(tmp_path, raw, raw)
    assert first["reused"] is False and second["reused"] is True
    assert snapshot(tmp_path) == original


@pytest.mark.parametrize("interrupted_pointer", ["dated", "global"])
@pytest.mark.parametrize("prior_pointer", [False, True])
def test_same_run_recovers_interrupted_pointer_publication_without_rewriting_originals(tmp_path, monkeypatch, interrupted_pointer, prior_pointer):
    from quantdesk import strategy_evidence_archive as module
    book = fixture_project(tmp_path, quantity=0)
    raw = json_bytes(book)
    if prior_pointer:
        archive_strategy_review(tmp_path, target_date=TARGET, run_id="previous-review", before_raw=raw, after_raw=raw,
            started_at=TARGET+"T19:00:00+08:00", recorded_at=TARGET+"T19:01:00+08:00", process_status="completed",
            requirements=build_requirements(tmp_path, TARGET), assert_write=lambda: None)
    real_write = module.write_json
    pointer = tmp_path / "results/strategy_evidence" / (f"{TARGET}/latest.json" if interrupted_pointer == "dated" else "latest.json")

    def interrupt(path, value, *, assert_write):
        if path == pointer:
            raise OSError("synthetic pointer publication interruption")
        real_write(path, value, assert_write=assert_write)

    monkeypatch.setattr(module, "write_json", interrupt)
    with pytest.raises(OSError, match="publication interruption"):
        archive(tmp_path, raw, raw)
    manifests = list((tmp_path / f"results/strategy_evidence/{TARGET}").glob("*.json"))
    manifest = next(path for path in manifests if path.name != "latest.json" and json.loads(path.read_bytes())["run_id"] == "synthetic-review")
    original = manifest.read_bytes()
    monkeypatch.setattr(module, "write_json", real_write)
    resumed = archive_strategy_review(tmp_path, target_date=TARGET, run_id="synthetic-review", before_raw=raw, after_raw=raw,
        started_at=TARGET+"T21:00:00+08:00", recorded_at=TARGET+"T21:01:00+08:00", process_status="completed",
        requirements=build_requirements(tmp_path, TARGET), assert_write=lambda: None)
    assert resumed["reused"] is True
    assert inspect_strategy_archive(tmp_path, TARGET)["status"] == "ready"
    assert manifest.read_bytes() == original
    assert json.loads(original)["recorded_at"] == TARGET+"T20:01:00+08:00"
    assert json.loads((tmp_path / "results/strategy_evidence/latest.json").read_bytes())["capture_sha256"] == digest(original)


def test_same_run_reuse_does_not_regress_newer_archive_pointers(tmp_path):
    book = fixture_project(tmp_path, quantity=0)
    raw = json_bytes(book)
    archive(tmp_path, raw, raw)
    archive_strategy_review(tmp_path, target_date=TARGET, run_id="newer-review", before_raw=raw, after_raw=raw,
        started_at=TARGET+"T21:00:00+08:00", recorded_at=TARGET+"T21:01:00+08:00", process_status="failed",
        requirements=build_requirements(tmp_path, TARGET), assert_write=lambda: None)
    original = snapshot(tmp_path)
    archive(tmp_path, raw, raw)
    assert snapshot(tmp_path) == original
    assert inspect_strategy_archive(tmp_path, TARGET)["status"] == "incomplete"


def test_same_run_with_different_original_book_is_a_conflict(tmp_path):
    book = fixture_project(tmp_path)
    raw = json_bytes(book)
    archive(tmp_path, raw, raw)
    with pytest.raises(ValueError, match="version_conflict"):
        archive(tmp_path, raw, json_bytes({**book, "cash": 9999}))


def test_old_import_is_observed_later_and_cannot_be_backdated(tmp_path):
    book = fixture_project(tmp_path)
    raw = json_bytes(book)
    result = archive(tmp_path, raw, raw, stamp="2026-11-02")
    doc = json.loads((tmp_path / result["manifest_path"]).read_bytes())
    assert result["status"] == "incomplete"
    assert "archive_observed_later;not_original_decision_time" in doc["current_reason_codes"]
    assert doc["historical_records_reconstructed"] is False


def test_naive_capture_time_is_not_assigned_a_guessed_timezone(tmp_path):
    book = fixture_project(tmp_path)
    with pytest.raises(ValueError, match="timezone_missing"):
        archive_strategy_review(tmp_path, target_date=TARGET, run_id="bad-time", before_raw=json_bytes(book), after_raw=json_bytes(book),
            started_at=TARGET+"T20:00:00", recorded_at=TARGET+"T20:01:00+08:00", process_status="completed",
            requirements=build_requirements(tmp_path, TARGET), assert_write=lambda: None)


def test_exit_of_legacy_position_keeps_missing_open_link_visible(tmp_path):
    book = fixture_project(tmp_path)
    before = json_bytes(book)
    after = put(tmp_path, "state/auto_paper_portfolio.json", {**book, "positions": [], "actions": [
        {"action": "sell", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": TARGET}]})
    result = archive(tmp_path, before, after)
    doc = json.loads((tmp_path / result["manifest_path"]).read_bytes())
    assert doc["trade_events"][0]["linked_open_event_ids"] == []
    assert "legacy_open_event_missing" in doc["trade_events"][0]["reason_codes"]
    assert doc["legacy_history_gaps"]


def test_unexplained_quantities_do_not_become_a_verified_terminal_exit(tmp_path):
    book = fixture_project(tmp_path)
    before = json_bytes(book)
    after_book = {**book, "positions": [{"symbol": CODE, "strategy_type": "long", "entry_date": TARGET, "quantity": 50}], "actions": [
        {"action": "sell", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": TARGET}]}
    after = put(tmp_path, "state/auto_paper_portfolio.json", after_book)
    result = archive(tmp_path, before, after)
    doc = json.loads((tmp_path / result["manifest_path"]).read_bytes())
    assert result["status"] == "incomplete"
    assert doc["trade_events"][0]["link_kind"] == "unverified"
    assert not doc["trade_events"][0]["linkage_verified"]


def test_original_declared_price_hash_mismatch_is_visible(tmp_path):
    book = fixture_project(tmp_path, quantity=0)
    before = json_bytes(book)
    book.update(positions=[{"symbol": CODE, "strategy_type": "long", "entry_date": TARGET, "quantity": 100}], actions=[
        {"action": "buy", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": TARGET,
         "execution_price_source_sha256": "0" * 64}])
    after = put(tmp_path, "state/auto_paper_portfolio.json", book)
    assert archive(tmp_path, before, after)["status"] == "incomplete"


def test_bad_basis_in_a_requested_price_set_does_not_silently_drop_that_symbol(tmp_path):
    book = fixture_project(tmp_path, quantity=0)
    second = tmp_path / "data/daily/300018.csv"
    second.write_text((tmp_path / f"data/daily/{CODE}.csv").read_text().replace(CODE, "300018").replace(",raw,", ",qfq,"))
    put(tmp_path, f"results/candidate_refresh/daily/{TARGET}.json", {"target_date": TARGET, "candidates": [{"symbol": CODE}, {"symbol": "300018"}]})
    before = json_bytes(book)
    book.update(positions=[{"symbol": CODE, "strategy_type": "long", "quantity": 100, "entry_date": TARGET}], actions=[
        {"action": "buy", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": TARGET}])
    after = put(tmp_path, "state/auto_paper_portfolio.json", book)
    result = archive(tmp_path, before, after)
    doc = json.loads((tmp_path / result["manifest_path"]).read_bytes())
    assert result["status"] == "incomplete"
    assert set(doc["price_sources"]) == {CODE, "300018"}
    assert doc["trade_events"][0]["binding_status"] == "unverified"


def test_capture_failure_and_changed_current_book_are_never_green(tmp_path):
    book = fixture_project(tmp_path)
    raw = json_bytes(book)
    result = archive(tmp_path, raw, raw, status="failed")
    assert result["status"] == "incomplete"
    assert inspect_strategy_archive(tmp_path, TARGET)["status"] == "incomplete"


def test_archived_closed_position_does_not_become_legacy_again_on_next_day(tmp_path):
    previous = fixture_project(tmp_path, target="2026-10-28", quantity=0)
    for index, target in enumerate(["2026-10-28", "2026-10-29", TARGET]):
        before = (tmp_path / "state/auto_paper_portfolio.json").read_bytes()
        prices = tmp_path / f"data/daily/{CODE}.csv"
        if target not in prices.read_text():
            with prices.open("a") as handle: handle.write(f"{target},{CODE},10,10,10,10,100,raw,synthetic_test_fixture\n")
        put(tmp_path, f"results/candidate_refresh/daily/{target}.json", {"target_date": target, "candidates": [{"symbol": CODE}]})
        previous = {**previous, "valuation_date": target, "actions": [], "positions": []}
        if index == 0:
            previous["positions"] = [{"symbol": CODE, "strategy_type": "long", "quantity": 100, "entry_date": target}]
            previous["actions"] = [{"action": "buy", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": target}]
        elif index == 1:
            previous["actions"] = [{"action": "sell", "symbol": CODE, "strategy_type": "long", "quantity": 100, "price": 10, "decision_date": target}]
            previous["closed_positions"] = [{"symbol": CODE, "strategy_type": "long", "exit_date": target, "quantity": 100, "exit_price": 10}]
        after = put(tmp_path, "state/auto_paper_portfolio.json", previous)
        result = archive(tmp_path, before, after, target=target, run=f"closed-{index}")
        assert result["status"] == "ready"
    assert result["legacy_history_gaps"] == []


def test_new_closed_summary_cannot_substitute_for_an_original_exit_action(tmp_path):
    book = fixture_project(tmp_path, quantity=0)
    before = json_bytes(book)
    after = put(tmp_path, "state/auto_paper_portfolio.json", {**book, "closed_positions": [
        {"symbol": CODE, "strategy_type": "long", "exit_date": TARGET, "quantity": 100, "exit_price": 10}]})
    result = archive(tmp_path, before, after)
    assert result["status"] == "incomplete"
    assert "new_closed_record_without_original_exit_action" in result["reason_codes"]
    put(tmp_path, "state/auto_paper_portfolio.json", {**book, "cash": 123})
    assert inspect_strategy_archive(tmp_path, TARGET)["status"] == "incomplete"
