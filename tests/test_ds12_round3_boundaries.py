"""Independent DS12 C2/C4 boundaries; all data lives in pytest tmp_path."""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest


def legacy_row(symbol: str, close: float) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash

    row = {
        "symbol": symbol, "date": "2026-06-02", "open": close, "high": close,
        "low": close, "close": close, "volume": 100.0, "amount": 1000.0,
        "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
        "source": "independent_legacy_fixture", "updated_at": "2026-06-02T16:00:00",
    }
    row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
    return row


def seed_database(path: Path, rows: list[dict], adjustment: str = "raw") -> None:
    from quantdesk.data_store.repositories import daily_table_for_adjustment
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    db = QuantDeskDB(path)
    try:
        db.initialize()
        table = daily_table_for_adjustment(adjustment)
        # Bypass the upgraded normalizer to reproduce an actual pre-upgrade DB.
        db.upsert_many(table, rows, ["symbol", "date"])
        persisted = db.execute(f"SELECT * FROM {table}")
        assert len(persisted) == len(rows)
        assert {(r["symbol"], r["close"], r["row_hash"]) for r in persisted} == {
            (r["symbol"], r["close"], r["row_hash"]) for r in rows
        }
    finally:
        db.close()


def assert_visible_conflict(action, caplog: pytest.LogCaptureFixture) -> None:
    caplog.clear()
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always")
        try:
            result = action()
        except (ValueError, RuntimeError) as exc:
            assert str(exc), "a refusal must explain the problem"
            return
    if isinstance(result, dict):
        rows = result.get("rows") or []
        reported = (result.get("ok") is False or result.get("warnings")
                    or result.get("warnings_top") or result.get("warning_count")
                    or result.get("failed_steps") or result.get("identity_conflict"))
    else:
        rows, reported = result, False
    row_diagnostics = any(r.get("warnings") or r.get("identity_conflict") or r.get("conflict") for r in rows)
    log_diagnostics = any(record.levelno >= 30 for record in caplog.records)
    assert reported or notices or row_diagnostics or log_diagnostics, {
        "returned": result, "python_warnings": [str(n.message) for n in notices],
        "log_messages": [r.getMessage() for r in caplog.records],
    }


@pytest.mark.parametrize("adjustment", ["raw", "forward", "back"])
def test_identical_legacy_aliases_are_one_readable_row(tmp_path: Path, adjustment: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    old_rows = [legacy_row("600000", 10.0), legacy_row("600000.SH", 10.0)]
    assert old_rows[0]["row_hash"] != old_rows[1]["row_hash"], "valid old hashes include the stored symbol"
    path = tmp_path / "identical.db"
    seed_database(path, old_rows, adjustment)
    db = QuantDeskDB(path)
    try:
        actual = DailyBarRepository(db).query_daily_bars("600000.SH", adjustment=adjustment)
    finally:
        db.close()
    assert len(actual) == 1 and actual[0]["close"] == 10.0, actual


@pytest.mark.parametrize("adjustment", ["raw", "forward", "back"])
def test_conflicting_legacy_aliases_with_null_hash_are_visible(tmp_path: Path, caplog: pytest.LogCaptureFixture, adjustment: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    rows = [legacy_row("600000", 10.0), legacy_row("600000.SH", 99.0)]
    for row in rows:
        row["row_hash"] = None  # Legal in the existing schema.
    path = tmp_path / "nullable_hash.db"
    seed_database(path, rows, adjustment)
    db = QuantDeskDB(path)
    try:
        assert_visible_conflict(lambda: DailyBarRepository(db).query_daily_bars("600000.SH", adjustment=adjustment), caplog)
    finally:
        db.close()


def test_claimed_uppercase_prefix_legacy_key_is_readable(tmp_path: Path) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    path = tmp_path / "uppercase.db"
    seed_database(path, [legacy_row("SH600000", 10.0)])
    db = QuantDeskDB(path)
    try:
        actual = DailyBarRepository(db).query_daily_bars("SH600000", adjustment="raw")
    finally:
        db.close()
    assert len(actual) == 1 and actual[0]["close"] == 10.0, actual


@pytest.mark.parametrize("stored_symbol", ["600000", "600000.SH"], ids=["plain_positive", "legacy_suffix"])
def test_public_daily_query_reads_original_legacy_key(tmp_path: Path, stored_symbol: str) -> None:
    from quantdesk.data_store.query import run_safe_query

    path = tmp_path / "public_query.db"
    seed_database(path, [legacy_row(stored_symbol, 10.0)])
    actual = run_safe_query("daily", symbol=stored_symbol, adjustment="raw", db_path=str(path))
    assert actual["row_count"] == 1 and actual["rows"][0]["close"] == 10.0, actual


def test_public_daily_query_does_not_hide_conflicting_aliases(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from quantdesk.data_store.query import run_safe_query

    path = tmp_path / "public_conflict.db"
    seed_database(path, [legacy_row("600000", 10.0), legacy_row("600000.SH", 99.0)])
    assert_visible_conflict(lambda: run_safe_query("daily", symbol="600000.SH", adjustment="raw", db_path=str(path)), caplog)


@pytest.mark.parametrize("entry", ["repository", "public_query"])
@pytest.mark.parametrize("with_duplicate", [False, True], ids=["distinct_stocks_positive", "equivalent_alias_duplicate"])
def test_cross_section_counts_securities_once(tmp_path: Path, entry: str, with_duplicate: bool) -> None:
    from quantdesk.data_store.query import run_safe_query
    from quantdesk.data_store.repositories import DailyBarRepository, normalize_symbol
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    rows = [legacy_row("600000", 10.0), legacy_row("000001", 20.0)]
    if with_duplicate:
        rows.append(legacy_row("600000.SH", 10.0))
    path = tmp_path / "cross_section.db"
    seed_database(path, rows)
    if entry == "repository":
        db = QuantDeskDB(path)
        try:
            actual = DailyBarRepository(db).query_cross_section("2026-06-02", adjustment="raw")
        finally:
            db.close()
    else:
        actual = run_safe_query("cross-section", date="2026-06-02", adjustment="raw", db_path=str(path))["rows"]
    assert len(actual) == 2, actual
    assert {(normalize_symbol(row["symbol"]), row["close"]) for row in actual} == {("600000", 10.0), ("000001", 20.0)}


@pytest.mark.parametrize("failure_stage", [None, "refresh_json", "refresh_md", "success_receipt"], ids=["successful_publish_positive", "json_replace_failure", "markdown_replace_failure", "receipt_replace_failure"])
def test_actual_publication_io_failure_has_receipt_and_no_false_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str | None) -> None:
    import quantdesk.research_outputs_refresh as refresh

    paths = {
        "page": tmp_path / "reports/daily_research/v421_daily_result_page_latest.md",
        "refresh_json": tmp_path / "results/data_update/v424_research_outputs_refresh_latest.json",
        "refresh_md": tmp_path / "reports/data_update/v424_research_outputs_refresh_latest.md",
        "success_receipt": tmp_path / "results/data_update/v424_research_outputs_publish_receipt_latest.json",
        "attempts": tmp_path / "results/data_update/v424_research_outputs_publish_attempts",
    }
    for rel in ("results/daily_research/v420_daily_stock_candidates_latest.csv",
                "results/daily_research/v420_daily_sector_candidates_latest.csv",
                "results/strategy_lab/v420_strategy_leaderboard_latest.csv"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("symbol\n", encoding="utf-8")
    paths["page"].parent.mkdir(parents=True, exist_ok=True)
    generation = 0

    def workflow(root, **kwargs):
        nonlocal generation
        assert Path(root) == tmp_path and kwargs["network"] is False
        generation += 1
        paths["page"].write_text(f"INDEPENDENT FIXTURE GENERATION {generation}\n", encoding="utf-8")
        return {"handoff": {"status": "completed"}}

    monkeypatch.setattr(refresh, "run_daily_open_workflow", workflow)
    monkeypatch.setattr(refresh, "write_dashboard_cards", lambda root: {"status": "ok"})
    first = refresh.refresh_research_outputs(tmp_path, network=False)
    assert first["refresh_success"] and first["result_page_updated"]
    old = {name: path.read_bytes() for name, path in paths.items() if name != "attempts"}
    old_success = json.loads(old["success_receipt"])
    assert old_success["status"] == "published"

    injected = []
    real_replace = os.replace

    def replace_with_one_unwritable_target(src, dst, *args, **kwargs):
        if failure_stage is not None and Path(dst).resolve() == paths[failure_stage].resolve():
            injected.append(str(dst))
            raise OSError(5, "independent deterministic publication replace failure", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    # The real JSON/Markdown writers run. Fail one final filesystem replace;
    # the independent attempt-receipt directory remains fully writable.
    monkeypatch.setattr(os, "replace", replace_with_one_unwritable_target)
    error = None
    result = None
    try:
        result = refresh.refresh_research_outputs(tmp_path, network=False)
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    receipt_files = list(paths["attempts"].glob("*.json"))
    receipts = [json.loads(path.read_text(encoding="utf-8")) for path in receipt_files]
    latest = json.loads(paths["refresh_json"].read_text(encoding="utf-8"))
    success = json.loads(paths["success_receipt"].read_text(encoding="utf-8"))
    observation = {
        "failure_stage": failure_stage, "injected": injected, "exception": error,
        "return_value": result, "latest_refresh": latest, "success_receipt": success,
        "failure_receipts": receipts,
        "old_success_receipt_preserved": paths["success_receipt"].read_bytes() == old["success_receipt"],
        "old_refresh_json_preserved": paths["refresh_json"].read_bytes() == old["refresh_json"],
        "old_refresh_md_preserved": paths["refresh_md"].read_bytes() == old["refresh_md"],
    }
    (tmp_path / "publication_observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    if failure_stage is None:
        assert error is None and result["refresh_success"] is True, observation
        assert latest["publish_attempt_id"] == success["attempt_id"] == result["publish_attempt_id"], observation
        assert success["status"] == "published", observation
        return
    assert injected, "the intended filesystem failure was not reached"
    current_failure_receipts = [receipt for receipt in receipts
                                if receipt.get("attempt_id") != first["publish_attempt_id"]
                                and receipt.get("status") == "failed" and receipt.get("failed_steps")]
    assert current_failure_receipts, observation
    assert observation["old_success_receipt_preserved"], observation
    assert latest.get("publish_attempt_id") == first["publish_attempt_id"] or latest.get("refresh_success") is False, observation
