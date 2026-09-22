"""DATA-FIX-0922: benchmark target coverage, list-minus-roster re-check, and the
closed-session minute summary.

Each test pins one of the three diagnosed gaps from
``E:/codex/claude/receipts/20260922-DATA-CAUSES-codex`` and stays offline: all
providers are injected, all fixtures live under ``tmp_path``.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk.intraday_refresh import collector
from scripts import run_data_close_pipeline as pipeline
from scripts import run_daily_data_refresh as refresh

TARGET = "2026-09-22"
SHANGHAI = refresh.SHANGHAI


# ---------------------------------------------------------------------------
# 1 · the afternoon benchmark claim decides whether the evening re-requests
# ---------------------------------------------------------------------------


def _benchmark_evidence(covered: bool) -> dict:
    return {
        "status": "up_to_date" if covered else "partial",
        "target_date": TARGET,
        "target_covered": covered,
        "freshness_status": "current" if covered else "behind_target",
        "coverage": {"requested": 3, "target_covered": 3 if covered else 0},
        "reason_codes": [] if covered else ["benchmark_target_not_covered"],
    }


def _stage(status: str, *, covered: bool | None = None) -> dict:
    payload = {"benchmark_evidence": _benchmark_evidence(covered)} if covered is not None else {}
    return {"status": status, "started_at": "", "reason_codes": [], "payload": payload}


def _runners(covered: bool, calls: list[str]) -> dict[str, object]:
    """Fake stage runners; market_refresh reports the benchmark target claim."""

    def make(name: str):
        def run(*_args, **_kwargs):
            calls.append(name)
            if name == "market_refresh":
                return pipeline.StageResult(
                    stage=name,
                    status="completed" if covered else "completed_with_warnings",
                    started_at=pipeline._now_iso(),
                    payload={"benchmark_evidence": _benchmark_evidence(covered)},
                )
            return pipeline.StageResult(stage=name, status="completed", started_at=pipeline._now_iso())

        return run

    return {name: make(name) for name in pipeline.STAGE_ORDER}


def _checkpoint_doc(market_refresh: dict) -> dict:
    stages = {name: _stage("completed") for name in pipeline.STAGE_ORDER}
    stages["market_refresh"] = market_refresh
    return {
        "surface": "checkpoint", "run_id": "data-close-20260922T154001",
        "target_date": TARGET, "config_hash": "fixture", "stages": stages,
    }


def test_afternoon_checkpoint_without_the_target_day_is_incomplete() -> None:
    doc = _checkpoint_doc(_stage("completed_with_warnings", covered=False))
    # Overall status still claims success; the benchmark phase is the gap.
    doc["status"] = "completed_with_skips"
    assert pipeline._checkpoint_data_incomplete(doc) is True


def test_afternoon_checkpoint_that_covers_the_target_day_stays_complete() -> None:
    doc = _checkpoint_doc(_stage("completed", covered=True))
    doc["status"] = "completed_all_executed"
    assert pipeline._checkpoint_data_incomplete(doc) is False


def test_stage_without_any_benchmark_claim_is_not_treated_as_a_gap() -> None:
    doc = _checkpoint_doc(_stage("completed"))
    doc["status"] = "completed_all_executed"
    assert pipeline._checkpoint_data_incomplete(doc) is False


def test_scheduled_evening_run_re_requests_the_benchmark(tmp_path: Path) -> None:
    calls: list[str] = []
    first = pipeline.run_pipeline(
        tmp_path, network=True, run_id="afternoon-1540", target_date=TARGET,
        stage_runner=_runners(False, calls),
    )
    assert first["status"] == "completed_all_executed"
    assert "market_refresh" in calls
    calls.clear()

    reentry = pipeline.run_pipeline(
        tmp_path, network=True, run_id="evening-1830", target_date=TARGET,
        stage_runner=_runners(True, calls), trigger="scheduled",
    )

    assert calls == ["market_refresh"]
    assert "market_refresh_benchmark_target_not_recorded" in reentry["reason_codes"]
    assert set(reentry["reused_stages"]) == set(pipeline.STAGE_ORDER) - {"market_refresh"}
    assert reentry["stages"]["market_refresh"]["status"] == "completed"


def test_scheduled_evening_run_does_not_re_request_a_covered_benchmark(tmp_path: Path) -> None:
    calls: list[str] = []
    pipeline.run_pipeline(
        tmp_path, network=True, run_id="afternoon-1540", target_date=TARGET,
        stage_runner=_runners(True, calls),
    )
    calls.clear()

    reentry = pipeline.run_pipeline(
        tmp_path, network=True, run_id="evening-1830", target_date=TARGET,
        stage_runner=_runners(True, calls), trigger="scheduled",
    )

    assert calls == []
    assert "scheduled_already_completed" in reentry["reason_codes"]
    assert set(reentry["reused_stages"]) == set(pipeline.STAGE_ORDER)


# ---------------------------------------------------------------------------
# 2 · a symbol the list drops gets one re-check and a written reason
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, relative in [
        ("PROJECT_ROOT", "."), ("DAILY_DIR", "data/daily"),
        ("META_DIR", "data/metadata"), ("VAL_DIR", "data/metadata/valuation_snapshots"),
    ]:
        monkeypatch.setattr(refresh, name, tmp_path / relative)
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    monkeypatch.setattr(refresh.requests, "Session", lambda: SimpleNamespace(close=lambda: None))
    lease = SimpleNamespace(assert_active=lambda: None, run_id="fixture-datafix")
    return tmp_path, lease


def _list_item(code: str) -> dict:
    return {
        "code": code, "name": "fixture", "open": 10.0, "high": 10.2, "low": 9.8,
        "trade": 10.1, "volume": 10000, "amount": 100500.0, "per": 12.0,
    }


def _list_quote(code: str) -> dict:
    return {
        "symbol": "sh" + code, "date": TARGET, "time": "15:00:00", "open": 10.0,
        "high": 10.2, "low": 9.8, "price": 10.1, "volume": 10000, "prev_close": 10.0,
    }


def _dropped_quote(**changes: object) -> dict:
    """The shape of a dated HQ quote for a symbol the list snapshot never carried."""

    return {
        "symbol": "sh689009", "name": "fixture", "open": 40.0, "prev_close": 39.5,
        "price": 40.2, "high": 40.5, "low": 39.4, "volume": 5000,
        "amount": 201000.0, "date": TARGET, "time": "15:00:00", **changes,
    }


def _review_payload(root: Path) -> dict:
    path = root / "data/metadata/daily_refresh_review" / f"{TARGET.replace('-', '')}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_dropped_symbol_is_re_checked_once_and_its_bar_is_written(
    setup_refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["600000", "689009"])
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [_list_item("600000")])
    seen: list[list[str]] = []

    def fetch(symbols: list[str]) -> dict:
        seen.append(list(symbols))
        if symbols == ["689009"]:
            return {"689009": _dropped_quote()}
        return {"600000": _list_quote("600000")}

    monkeypatch.setattr(refresh, "_dated_close_quotes", fetch)

    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    # Exactly one extra request pass, and only for the dropped code.
    assert seen == [["600000"], ["689009"]]
    assert result["bars"]["appended"] == 2
    rows = list(csv.DictReader(io.StringIO((refresh.DAILY_DIR / "689009.csv").read_text())))
    assert len(rows) == 1
    assert rows[0]["date"] == TARGET and rows[0]["symbol"] == "689009"
    assert rows[0]["adjust"] == "raw"
    assert rows[0]["source"] == "sina_dated_hq_universe_diff"
    assert float(rows[0]["pct_chg"]) == pytest.approx((40.2 / 39.5 - 1) * 100)
    review = _review_payload(root)
    assert review["missing_in_snapshot"] == ["689009"]
    assert review["universe_diff_requery"]["689009"]["attempted"] is True
    assert review["universe_diff_requery"]["689009"]["reason"] == ""
    assert review["universe_diff_requery_limit"] == refresh.UNIVERSE_DIFF_REQUERY_LIMIT
    assert result["universe_diff_requery"]["attempted"] == 1
    assert result["universe_diff_requery"]["appended"] == 1
    assert not [
        warning
        for warning in result["warnings"]
        if warning.startswith("universe_diff_requery_failed")
    ]


def test_dropped_symbol_recheck_without_a_quote_records_code_and_reason(
    setup_refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["600000", "689009"])
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [_list_item("600000")])
    monkeypatch.setattr(
        refresh, "_dated_close_quotes", lambda symbols: {"600000": _list_quote("600000")}
    )

    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    assert not (refresh.DAILY_DIR / "689009.csv").exists()
    review = _review_payload(root)
    entry = review["universe_diff_requery"]["689009"]
    assert entry["attempted"] is True
    assert entry["reason"] == "universe_diff_requery_no_quote"
    assert entry["note"]
    assert result["universe_diff_requery"]["failed"] == 1
    assert "universe_diff_requery_failed:1:689009" in result["warnings"]
    # The dropped code stays a visible gap instead of a silent absence.
    assert review["missing_in_snapshot"] == ["689009"]
    assert review["flagged_for_review"] == {}


def test_dropped_symbol_recheck_with_a_stale_quote_writes_nothing(
    setup_refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["600000", "689009"])
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [_list_item("600000")])

    def fetch(symbols: list[str]) -> dict:
        if symbols == ["689009"]:
            return {"689009": _dropped_quote(date="2026-09-19")}
        return {"600000": _list_quote("600000")}

    monkeypatch.setattr(refresh, "_dated_close_quotes", fetch)

    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    assert not (refresh.DAILY_DIR / "689009.csv").exists()
    entry = _review_payload(root)["universe_diff_requery"]["689009"]
    assert entry["reason"] == "universe_diff_requery:quote_date_mismatch"
    assert entry["quote_date"] == "2026-09-19"
    assert result["bars"]["appended"] == 1


def test_dropped_symbols_beyond_the_limit_are_recorded_as_not_attempted(
    setup_refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "UNIVERSE_DIFF_REQUERY_LIMIT", 1)
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["600000", "689009", "689010"])
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [_list_item("600000")])
    seen: list[list[str]] = []

    def fetch(symbols: list[str]) -> dict:
        seen.append(list(symbols))
        if symbols == ["600000"]:
            return {"600000": _list_quote("600000")}
        return {"689009": _dropped_quote()}

    monkeypatch.setattr(refresh, "_dated_close_quotes", fetch)

    refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    assert seen == [["600000"], ["689009"]]
    review = _review_payload(root)
    assert review["universe_diff_requery"]["689009"]["attempted"] is True
    capped = review["universe_diff_requery"]["689010"]
    assert capped["attempted"] is False
    assert capped["reason"] == "universe_diff_requery_over_limit"
    assert not (refresh.DAILY_DIR / "689010.csv").exists()


def test_empty_list_fallback_still_requests_the_roster_once(
    setup_refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lease = setup_refresh
    monkeypatch.setattr(refresh, "_load_universe_symbols", lambda: ["600000", "689009"])
    monkeypatch.setattr(refresh, "fetch_market_snapshot", lambda session: [])
    seen: list[list[str]] = []

    def fetch(symbols: list[str]) -> dict:
        seen.append(list(symbols))
        return {"600000": _list_quote("600000"), "689009": _dropped_quote()}

    monkeypatch.setattr(refresh, "_dated_close_quotes", fetch)

    result = refresh.family_bars_and_valuation(TARGET, True, lease=lease)

    # The fallback already asked for the roster: no second pass for the diff.
    assert seen == [["600000", "689009"]]
    assert result["universe_diff_requery"]["attempted"] == 0
    assert result["quote_source"] == "sina_dated_hq_fallback"


# ---------------------------------------------------------------------------
# 3 · the closed-session summary reports the unwritten symbols
# ---------------------------------------------------------------------------


def _frozen_round_detail() -> list[dict]:
    """The 2026-09-22 15:05 round's 90-symbol detail, reproduced in memory.

    Shape and counts mirror the frozen receipt
    (``20260922-DATA-CAUSES-codex/inputs/results/intraday/intraday_refresh_latest.json``,
    sha256 ``8fdf23e8…``): 82 ``ok``, two ``storage_blocked`` (000610, 000676)
    and six ``failed`` BSE codes.
    """

    ok = [
        {"symbol": f"{index:06d}", "status": "ok", "latest_bar_time": f"{TARGET} 15:00:00"}
        for index in range(82)
    ]
    blocked = [
        {"symbol": code, "status": "storage_blocked",
         "reason": f"minute_header_mismatch:{code}.csv", "error_stage": "storage"}
        for code in ("000610", "000676")
    ]
    failed = [
        {"symbol": code, "status": "failed", "error_stage": "request",
         "reason": "provider_failed:tdx:unpack requires a buffer of 4 bytes"}
        for code in ("920227", "920229", "920298", "920427", "920526", "920768")
    ]
    return ok + blocked + failed


def _manifest(status: str = "ready", detail: list[dict] | None = None) -> dict:
    return {
        "provider_status": _frozen_round_detail() if detail is None else detail,
        "fresh_count": 0, "partial_count": 0, "missing_count": 0, "stale_count": 0,
        "max_lag_seconds": 0, "status": status, "reason_codes": [],
    }


def test_closed_round_reports_the_eight_unwritten_symbols() -> None:
    manifest = _manifest()
    collector._evaluate_freshness(
        manifest, current=datetime(2026, 9, 22, 15, 5, tzinfo=SHANGHAI),
        critical=set(), session="closed",
    )

    assert manifest["fresh_count"] == 82
    assert manifest["missing_count"] == 8
    assert manifest["stale_count"] == 0
    assert manifest["status"] == "partial"
    # Nothing is invented: the closed session still never raises a stale verdict.
    assert manifest["reason_codes"] == []


def test_lunch_round_reports_missing_but_never_flags_stale() -> None:
    detail = [
        {"symbol": "600000", "status": "ok", "latest_bar_time": f"{TARGET} 11:30:00"},
        {"symbol": "600001", "status": "failed", "reason": "provider_failed:tdx:timeout"},
    ]
    manifest = _manifest(detail=detail)
    collector._evaluate_freshness(
        manifest, current=datetime(2026, 9, 22, 12, 30, tzinfo=SHANGHAI),
        critical=set(), session="lunch",
    )

    assert manifest["fresh_count"] == 1
    assert manifest["missing_count"] == 1
    assert manifest["stale_count"] == 0
    assert manifest["status"] == "partial"


def test_closed_round_does_not_relabel_a_terminal_status() -> None:
    manifest = _manifest(status="timed_out")
    collector._evaluate_freshness(
        manifest, current=datetime(2026, 9, 22, 15, 5, tzinfo=SHANGHAI),
        critical=set(), session="closed",
    )

    assert manifest["missing_count"] == 8
    assert manifest["status"] == "timed_out"


def test_closed_round_with_every_symbol_written_stays_ready() -> None:
    detail = [{"symbol": f"{index:06d}", "status": "ok", "latest_bar_time": f"{TARGET} 15:00:00"}
              for index in range(5)]
    manifest = _manifest(detail=detail)
    collector._evaluate_freshness(
        manifest, current=datetime(2026, 9, 22, 15, 5, tzinfo=SHANGHAI),
        critical=set(), session="closed",
    )

    assert manifest["fresh_count"] == 5
    assert manifest["missing_count"] == 0
    assert manifest["status"] == "ready"
