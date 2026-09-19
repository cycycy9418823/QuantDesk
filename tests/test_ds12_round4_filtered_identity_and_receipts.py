"""Bounded follow-through checks for the revised C2/C4 public contracts."""
from __future__ import annotations

import hashlib
import json
import os
import warnings
from pathlib import Path

import pytest


def legacy_row(symbol: str, close: float) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash
    row = {"symbol": symbol, "date": "2026-06-02", "open": close, "high": close,
           "low": close, "close": close, "volume": 100.0, "amount": 1000.0,
           "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
           "source": "independent_filtered_fixture", "updated_at": "2026-06-02T16:00:00"}
    row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
    return row


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("where", ["close < 20", "symbol = '600000.SH'"], ids=["business_filter", "legacy_symbol_filter"])
@pytest.mark.parametrize("conflicting", [False, True], ids=["equivalent_positive", "conflicting_aliases"])
def test_filtered_query_cannot_hide_conflict_in_selected_identity(tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, where: str, conflicting: bool) -> None:
    from quantdesk.data_store.query import run_safe_query
    from quantdesk.data_store.repositories import daily_table_for_adjustment
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    path = tmp_path / "legacy.db"
    rows = [legacy_row("600000", 10.0), legacy_row("600000.SH", 99.0 if conflicting else 10.0)]
    db = QuantDeskDB(path)
    try:
        db.initialize()
        table = daily_table_for_adjustment("raw")
        db.upsert_many(table, rows, ["symbol", "date"])
        stored = db.execute(f"SELECT * FROM {table}")
        assert len(stored) == 2 and {r["close"] for r in stored} == ({10.0, 99.0} if conflicting else {10.0})
    finally:
        db.close()
    kwargs = {"symbol": "600000.SH"} if kind == "daily" else {"date": "2026-06-02"}
    caplog.clear()
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always")
        try:
            actual = run_safe_query(kind, where=where, adjustment="raw", db_path=str(path), **kwargs)
        except (ValueError, RuntimeError) as exc:
            assert conflicting and str(exc)
            return
    observation = {"kind": kind, "where": where, "conflicting": conflicting, "actual": actual,
                   "warnings": [str(n.message) for n in notices], "logs": [r.getMessage() for r in caplog.records]}
    (tmp_path / "filter_observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    if not conflicting:
        assert actual.get("ok") is True and actual["row_count"] == 1 and actual["rows"][0]["close"] == 10.0, observation
        return
    diagnostic = (actual.get("ok") is False or actual.get("identity_conflict") or actual.get("warnings_top")
                  or actual.get("warnings") or actual.get("warning_count") or notices
                  or any(r.levelno >= 30 for r in caplog.records)
                  or any(r.get("identity_conflict") or r.get("warnings") for r in actual.get("rows", [])))
    assert diagnostic, observation


def fingerprint(path: Path) -> dict | None:
    if not path.is_file():
        return None
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@pytest.mark.parametrize("failure_stage", [None, "refresh_md", "success_receipt"], ids=["normal_publication_positive", "markdown_failure", "receipt_failure"])
def test_receipt_declared_after_fingerprint_matches_its_persisted_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str | None) -> None:
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
        paths["page"].write_text(f"INDEPENDENT GENERATION {generation}\n", encoding="utf-8")
        return {"handoff": {"status": "completed"}}

    monkeypatch.setattr(refresh, "run_daily_open_workflow", workflow)
    monkeypatch.setattr(refresh, "write_dashboard_cards", lambda root: {"status": "ok"})
    first = refresh.refresh_research_outputs(tmp_path, network=False)
    assert first["refresh_success"] is True
    old_success = paths["success_receipt"].read_bytes()
    before = {"result_page": fingerprint(paths["page"]), "refresh_json": fingerprint(paths["refresh_json"]), "refresh_md": fingerprint(paths["refresh_md"])}
    injected = []
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if failure_stage is not None and Path(dst).resolve() == paths[failure_stage].resolve():
            injected.append(str(dst))
            raise OSError(5, "independent single-target publication fault", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    result = refresh.refresh_research_outputs(tmp_path, network=False)
    if failure_stage is None:
        assert result["refresh_success"] is True
        receipt_path = paths["success_receipt"]
    else:
        assert injected and result["refresh_success"] is False
        assert paths["success_receipt"].read_bytes() == old_success
        receipt_path = tmp_path / result["publish_failure_receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["attempt_id"] == result["publish_attempt_id"]
    mismatches = []
    observed = {}
    for name, artifact in receipt["artifacts"].items():
        path = tmp_path / artifact["relative_path"]
        actual = fingerprint(path)
        observed[name] = actual
        if artifact.get("before") != before[name]:
            mismatches.append({"artifact": name, "phase": "before", "declared": artifact.get("before"), "actual": before[name]})
        # Do not require a failed/unpublished artifact to have an after hash.
        # If the receipt declares one, its associated persisted path must match.
        if artifact.get("after") is not None and artifact["after"] != actual:
            mismatches.append({"artifact": name, "phase": "after", "declared": artifact["after"], "actual": actual})
    observation = {"failure_stage": failure_stage, "injected": injected, "result": result,
                   "receipt_path": str(receipt_path), "receipt": receipt, "observed": observed, "mismatches": mismatches}
    (tmp_path / "receipt_fingerprint_observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not mismatches, observation
