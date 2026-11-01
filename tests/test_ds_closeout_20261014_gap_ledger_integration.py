"""D2 final-ledger regressions, using fake providers and temporary projects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from quantdesk import daily_gap_recovery as gap
from quantdesk.daily_session_evidence import build_baostock_session_evidence
from tests import test_ds_closeout_20261014_gap_ledger as history
from tests import test_target_daily_gap_recovery_20261013 as fixtures
from tests.test_daily_data_chain_universe_refresh import _install_minimal_chain_fakes
from tests.test_daily_data_chain_upstream_reuse_preflight import _valid_upstream_reuse

DAY = history.TARGET
DAYS = history.DAYS
fake_sources = fixtures.fake_sources


def _payload(symbol: str, latest: str) -> dict:
    return {"target_date": DAY, "processed_symbols": [symbol],
            "latest_by_symbol_after": {symbol: latest},
            "missing_symbol_list_after": [], "invalid_price_basis_symbols": []}


@pytest.mark.parametrize("mode", ["append", "no_session"])
def test_final_ledger_matches_same_run_recovery(tmp_path: Path, fake_sources, mode: str) -> None:
    _, responses = fake_sources
    path, before = fixtures._old_file(tmp_path, "000582", "2026-10-13")
    if mode == "append":
        responses[("baostock_daily", "000582")] = fixtures._bar("000582", DAY)
    else:
        frame = pd.DataFrame()
        frame.attrs["suspension_evidence"] = history._no_session("000582", DAY)
        responses[("baostock_daily", "000582")] = frame
    result = gap.recover_target_day_gaps(
        project_root=tmp_path, target_date=DAY, daily_payload=_payload("000582", "2026-10-13"),
        output_dir=tmp_path / "run", network=True,
        ledger_symbols=["000582"], ledger_trading_days=[DAY],
    )
    row = history._row(result["historical_ledger"], "000582")
    assert row["pending_dates"] == []
    assert row["recovered_dates"] == ([DAY] if mode == "append" else [])
    assert row["verified_no_session_dates"] == ([DAY] if mode == "no_session" else [])
    saved = json.loads((tmp_path / "run/receipt.json").read_text(encoding="utf-8"))
    assert saved["historical_ledger"] == result["historical_ledger"]
    assert path.read_bytes().startswith(before)
    if mode == "no_session":
        assert path.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"symbol": "000668"}, {"close": 0}, {"volume": float("inf")},
    {"open": True}, {"symbol": None}, "missing_prices",
])
def test_unusable_local_row_is_never_recovered(tmp_path: Path, change) -> None:
    path = tmp_path / "data/daily/000582.csv"
    path.parent.mkdir(parents=True)
    frame = (pd.DataFrame([{"date": DAY, "symbol": "000582", "adjust": "raw"}])
             if change == "missing_prices" else fixtures._bar("000582", DAY))
    if isinstance(change, dict):
        for key, value in change.items():
            frame[key] = value
    frame.to_csv(path, index=False)
    before = path.read_bytes()
    row = history._row(gap.build_symbol_date_gap_ledger(
        project_root=tmp_path, symbols=["000582"], trading_days=[DAY]), "000582")
    assert row["recovered_dates"] == []
    assert row["usable"] is False
    assert row["invalid_evidence"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["already_today", "append", "no_session"])
def test_real_chain_publishes_final_history_even_when_today_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_sources, mode: str,
) -> None:
    calls, responses = fake_sources
    days = ["2026-10-12", "2026-10-13"] + ([DAY] if mode == "already_today" else [])
    path = tmp_path / "data/daily/000582.csv"
    path.parent.mkdir(parents=True)
    pd.concat([fixtures._bar("000582", day) for day in days]).reindex(
        columns=fixtures.DAILY_HEADER.split(",")
    ).to_csv(path, index=False)
    history._daily(tmp_path, "000668", ["2026-10-13", DAY])
    reuse = _valid_upstream_reuse(tmp_path, target=DAY)
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.write_text("symbol,name,source,updated_at\n" + "".join(
        f"{s},TEST,stock_zh_a_spot,{DAY}\n" for s in ["000582", "000668"]), encoding="utf-8")
    reuse["stages"]["universe"]["sha256"] = hashlib.sha256(universe.read_bytes()).hexdigest()
    calendar = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("date,is_open,source\n" + "".join(
        f"{day},true,synthetic_test\n" for day in DAYS), encoding="utf-8")
    if mode == "append":
        responses[("baostock_daily", "000582")] = fixtures._bar("000582", DAY)
    elif mode == "no_session":
        frame = pd.DataFrame()
        frame.attrs["suspension_evidence"] = history._no_session("000582", DAY)
        responses[("baostock_daily", "000582")] = frame
    chain = _install_minimal_chain_fakes(monkeypatch, ["000582", "000668"])
    monkeypatch.setattr(chain, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(chain, "update_benchmark_files", lambda *a, **kw: {"status": "completed"})
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date=DAY, network=True, dry_run=False,
        rebuild_features=False, upstream_reuse=reuse, run_id="ledger-" + mode,
    )
    recovery = result["components"]["target_day_gap_recovery"]
    ledger = recovery["historical_ledger"]
    first, second = history._row(ledger, "000582"), history._row(ledger, "000668")
    assert first["pending_dates"] == DAYS[:2]
    assert second["pending_dates"] == DAYS[:3]
    assert (DAY in first["recovered_dates"]) == (mode != "no_session")
    assert first["verified_no_session_dates"] == ([DAY] if mode == "no_session" else [])
    assert result["status"] == "completed"  # Historical labels never add a chain gate.
    # The full ledger belongs to the run's recovery component. Daily and
    # critical summaries must not serialize two more copies of every date.
    manifest_path = tmp_path / result["manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert json.dumps(manifest).count('"historical_ledger":') == 1
    views = [manifest["components"]["daily_update"],
             manifest["components"]["critical_daily_update"],
             json.loads((manifest_path.parent / "daily_update.json").read_text(encoding="utf-8"))]
    for view in views:
        reference = view["target_day_gap_recovery"]["historical_ledger_ref"]
        assert reference["run_id"] == "ledger-" + mode
        assert reference["target_date"] == DAY
        evidence = json.loads((tmp_path / reference["path"]).read_text(encoding="utf-8"))
        for key in reference["json_pointer"].strip("/").split("/"):
            evidence = evidence[key]
        assert evidence == ledger
        assert "historical_ledger" not in view["target_day_gap_recovery"]
    if mode == "already_today":
        assert calls == []
    else:
        saved = json.loads((tmp_path / "results/data_chain/runs" / ("ledger-" + mode)
                            / "target_day_gap_recovery/receipt.json").read_text(encoding="utf-8"))
        assert saved["historical_ledger"] == ledger


def test_same_run_session_evidence_preserves_earlier_verified_dates(tmp_path: Path, fake_sources) -> None:
    _, responses = fake_sources
    fixtures._old_file(tmp_path, "000582", "2026-10-13")
    frame = pd.DataFrame()
    frame.attrs["suspension_evidence"] = history._no_session("000582", DAY)
    responses[("baostock_daily", "000582")] = frame
    result = gap.recover_target_day_gaps(
        project_root=tmp_path, target_date=DAY, daily_payload=_payload("000582", "2026-10-13"),
        output_dir=tmp_path / "run", network=True, ledger_symbols=["000582"],
        ledger_trading_days=[DAYS[0], DAY],
        ledger_session_evidence={"000582": history._no_session("000582", DAYS[0])},
    )
    row = history._row(result["historical_ledger"], "000582")
    assert row["verified_no_session_dates"] == [DAYS[0], DAY]
    assert row["pending_dates"] == []


def test_conflicting_verified_session_payloads_remain_pending(tmp_path: Path) -> None:
    traded = build_baostock_session_evidence(
        symbol="000582", provider_code="sz.000582", query_start_date=DAY, query_end_date=DAY,
        records=[{"trade_date": DAY, "tradestatus": "1"}],
    )
    row = history._row(gap.build_symbol_date_gap_ledger(
        project_root=tmp_path, symbols=["000582"], trading_days=[DAY],
        session_evidence={"000582": [history._no_session("000582", DAY), traded]},
    ), "000582")
    assert row["verified_no_session_dates"] == []
    assert row["pending_dates"] == [DAY]
    assert "session_evidence_trade_status_conflict" in row["session_evidence_rejected"]


def test_missing_calendar_scope_cannot_label_prices_verified(tmp_path: Path) -> None:
    history._daily(tmp_path, "000582", [DAY])
    ledger = gap.build_symbol_date_gap_ledger(project_root=tmp_path, symbols=["000582"], trading_days=[])
    row = history._row(ledger, "000582")
    assert row["usable"] is False
    assert row["price_validation_status"] == "not_assessed"
    assert row["recovered_dates"] == row["pending_dates"] == []
    assert ledger["not_assessed_symbol_count"] == 1


def test_price_validation_does_not_claim_outside_its_date_scope(tmp_path: Path) -> None:
    path = history._daily(tmp_path, "000582", [DAYS[0], DAY])
    frame = pd.read_csv(path, dtype={"symbol": str})
    frame.loc[0, "close"] = 0
    frame.to_csv(path, index=False)
    row = history._row(gap.build_symbol_date_gap_ledger(
        project_root=tmp_path, symbols=["000582"], trading_days=[DAY]), "000582")
    assert row["recovered_dates"] == [DAY]
    assert row["price_validation_scope"] == "requested_trading_dates_only"


def test_completion_and_deadline_include_final_ledger_scan(tmp_path: Path, monkeypatch, fake_sources) -> None:
    _, responses = fake_sources
    fixtures._old_file(tmp_path, "000582", "2026-10-13")
    responses[("baostock_daily", "000582")] = fixtures._bar("000582", DAY)
    ticks = [0.0]
    monkeypatch.setattr(gap.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(gap, "_now", lambda: f"synthetic-time-{ticks[0]}")
    original = gap.build_symbol_date_gap_ledger

    def delayed_ledger(**kwargs):
        result = original(**kwargs)
        ticks[0] += 121
        return result

    monkeypatch.setattr(gap, "build_symbol_date_gap_ledger", delayed_ledger)
    result = gap.recover_target_day_gaps(
        project_root=tmp_path, target_date=DAY, daily_payload=_payload("000582", "2026-10-13"),
        output_dir=tmp_path / "run", network=True, ledger_symbols=["000582"], ledger_trading_days=[DAY],
    )
    assert result["provider_deadline_exhausted"] is False
    assert result["deadline_exhausted"] is True
    assert result["completed_at"] == "synthetic-time-121.0"
