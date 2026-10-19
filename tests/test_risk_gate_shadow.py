from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from quantdesk.data_truth import candidate_lifecycle_evidence as lifecycle
from quantdesk.risk.risk_gate import compute_risk_evidence_source_hash
from quantdesk.version import API_SCHEMA_VERSION


try:
    shadow = importlib.import_module("quantdesk.risk.risk_gate_shadow")
except ModuleNotFoundError:
    shadow = None


def test_shadow_module_exposes_stage3_api() -> None:
    assert shadow is not None, "quantdesk.risk.risk_gate_shadow must exist"
    for name in (
        "prepare_shadow_funded_ledger",
        "collect_local_risk_evidence",
        "evaluate_shadow_risk_order",
        "build_legacy_blocked_shadow_record",
        "summarize_shadow_records",
        "write_shadow_summary",
        "render_shadow_report_section",
        "record_shadow_error_best_effort",
    ):
        assert callable(getattr(shadow, name, None)), name


@pytest.fixture(autouse=True)
def _skip_until_shadow_module_exists(request: pytest.FixtureRequest) -> None:
    if shadow is None and request.node.name != "test_shadow_module_exposes_stage3_api":
        pytest.skip("dependent shadow-risk tests wait for the module RED")


def _ledger() -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "profile_id": "legacy_preserved",
        "scenario_id": "legacy_existing",
        "initial_cash": 100_000.0,
        "cash": 90_000.0,
        "positions": [
            {
                "symbol": "000001",
                "name": "existing funded position",
                "quantity": 100,
                "sellable_quantity": 100,
                "market_value": 1_000.0,
                "latest_price": 10.0,
                "entry_price": 9.5,
                "entry_date": "2026-01-05",
                "sector": "bank",
            }
        ],
    }


def _formal_candidate(symbol: str = "600000") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": "formal local candidate",
        "sector": "bank",
        "close": 10.0,
        "amount_ma20": 50_000_000.0,
        "data_latest_date": "2026-07-17",
        "evidence": {
            "eligible": True,
            "evidence_kind": "formal",
            "coverage": 1.0,
            "minimum_coverage": 1.0,
            "pit_eligible": True,
            "price_contract_eligible": True,
        },
    }


def _write_daily(path: Path, symbol: str, *, as_of: str = "2026-07-17") -> None:
    days = pd.bdate_range(end=as_of, periods=150)
    closes = [10.0 + math.sin(index / 7.0) * 0.35 + index * 0.001 for index in range(len(days))]
    frame = pd.DataFrame(
        {
            "date": [day.date().isoformat() for day in days],
            "open": [value - 0.03 for value in closes],
            "high": [value + 0.08 for value in closes],
            "low": [value - 0.08 for value in closes],
            "close": closes,
            "volume": [5_000_000 + index * 100 for index in range(len(days))],
            "amount": [50_000_000 + index * 10_000 for index in range(len(days))],
            # Canonical daily CSVs carry the Price Data Contract label in
            # ``adjust``.  The shadow adapter must classify this original
            # frame before any date filtering; it may not infer basis from a
            # provider, path, or test-only alias.
            "adjust": ["raw"] * len(days),
        }
    )
    path.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path / f"{symbol}.csv", index=False, encoding="utf-8-sig")


def _local_sources(tmp_path: Path) -> dict[str, Path]:
    daily_dir = tmp_path / "data" / "daily"
    _write_daily(daily_dir, "000001")
    _write_daily(daily_dir, "600000")
    candidate_path = tmp_path / "results" / "auto_strategy_latest.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps({"standard_candidates": [_formal_candidate()]}), encoding="utf-8")
    ledger_path = tmp_path / "state" / "auto_paper_portfolio.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(_ledger()), encoding="utf-8")
    sector_path = tmp_path / "results" / "sector_rotation_latest.json"
    sector_path.write_text(
        json.dumps(
            {
                "ok": True,
                "status": "passed",
                "evidence_eligible": True,
                "pit_eligible": True,
                "synthetic_test_data": False,
                "price_basis": "raw",
                "reason_codes": [],
                "target_date": "2026-07-17",
                "sector_membership": [
                    {"symbol": "000001", "sector": "bank"},
                    {"symbol": "600000", "sector": "bank"},
                ],
                "sector_rankings": [
                    {
                        # Current sector-rotation artifacts use sector_name;
                        # the adapter must not require a legacy alias.
                        "sector_name": "bank",
                        "status": "stable",
                        "heat_score": 60.0,
                        "rank_change_5d": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "daily_dir": daily_dir,
        "candidate_path": candidate_path,
        "ledger_path": ledger_path,
        "sector_path": sector_path,
    }


def test_prepare_shadow_ledger_injects_only_two_position_keys_in_deep_copy() -> None:
    source = _ledger()
    before = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    funded = shadow.prepare_shadow_funded_ledger(source)

    after = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert after == before
    assert funded is not source
    assert funded["positions"][0] is not source["positions"][0]
    assert funded["positions"][0]["price_basis"] == "raw"
    assert funded["positions"][0]["funded"] is True
    assert "paper_trading_only" not in funded
    assert "evidence" not in funded["positions"][0]
    assert set(funded["positions"][0]) - set(source["positions"][0]) == {
        "price_basis",
        "funded",
    }


def test_local_evidence_has_five_real_components_and_runtime_hashes(tmp_path: Path) -> None:
    paths = _local_sources(tmp_path)
    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert missing == {}
    assert set(evidence) == {"price", "sector", "liquidity", "returns", "smart_stop"}
    for component in evidence.values():
        assert component["data_cutoff"] <= "2026-07-17"
        assert component["source_path"]
        assert component["source_hash"] == compute_risk_evidence_source_hash(component)
    assert len(evidence["returns"]["frame"]) >= 100
    assert list(evidence["returns"]["frame"].columns) == ["000001", "600000"]
    assert len(evidence["smart_stop"]["daily_rows_by_symbol"]["000001"]) == 30


def test_smart_stop_requires_pit_membership_from_the_certified_status_payload(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    payload = json.loads(paths["sector_path"].read_text(encoding="utf-8"))
    payload.pop("sector_membership")
    paths["sector_path"].write_text(json.dumps(payload), encoding="utf-8")

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "smart_stop" not in evidence
    assert missing["smart_stop"] == ["smart_stop_local_evidence_missing"]


def test_missing_certified_membership_keeps_sector_component_fail_closed(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    ledger = _ledger()
    ledger["positions"][0].pop("sector")
    paths["ledger_path"].write_text(json.dumps(ledger), encoding="utf-8")
    payload = json.loads(paths["sector_path"].read_text(encoding="utf-8"))
    payload.pop("sector_membership")
    paths["sector_path"].write_text(json.dumps(payload), encoding="utf-8")

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(ledger),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "sector" not in evidence
    assert missing["sector"] == ["sector_asset_local_evidence_missing"]


def test_smart_stop_counts_unique_funded_symbols_not_strategy_rows(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    ledger = _ledger()
    duplicate = copy.deepcopy(ledger["positions"][0])
    duplicate["strategy_type"] = "board"
    ledger["positions"].append(duplicate)
    paths["ledger_path"].write_text(json.dumps(ledger), encoding="utf-8")

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(ledger),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "smart_stop" in evidence
    assert "smart_stop" not in missing
    assert set(evidence["smart_stop"]["daily_rows_by_symbol"]) == {"000001"}


def test_shadow_enriches_missing_legacy_position_taxonomy_from_certified_pit_only(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    state = _ledger()
    state["positions"][0].pop("sector")
    paths["ledger_path"].write_text(json.dumps(state), encoding="utf-8")
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)

    record = shadow.evaluate_shadow_risk_order(
        state=state,
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        requested_quantity=100,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
        price_source_path=paths["candidate_path"],
        price_data_cutoff="2026-07-17",
    )

    assert "funded_position_sector_missing" not in record["reason_codes"]
    assert "sector_asset_local_evidence_missing" not in record["reason_codes"]
    assert "portfolio_snapshot_digest_mismatch" not in record["reason_codes"]
    assert "paper_intent_portfolio_binding_mismatch" not in record["reason_codes"]
    assert record["ledger_source_binding"]["path"] == str(paths["ledger_path"])
    assert record["ledger_source_binding"]["file_sha256"].startswith("sha256:")
    assert len(record["ledger_source_binding"]["file_sha256"]) == 71
    assert json.dumps(state, ensure_ascii=False, sort_keys=True) == before


def test_shadow_normalizes_conflicting_duplicate_position_taxonomy_from_pit(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    state = _ledger()
    duplicate = copy.deepcopy(state["positions"][0])
    duplicate["strategy_type"] = "board"
    duplicate["sector"] = "legacy-bank-taxonomy"
    state["positions"].append(duplicate)
    paths["ledger_path"].write_text(json.dumps(state), encoding="utf-8")
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)

    record = shadow.evaluate_shadow_risk_order(
        state=state,
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        requested_quantity=100,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
        price_source_path=paths["candidate_path"],
        price_data_cutoff="2026-07-17",
    )

    assert "funded_position_sector_mismatch" not in record["reason_codes"]
    assert "portfolio_snapshot_digest_mismatch" not in record["reason_codes"]
    assert "paper_intent_portfolio_binding_mismatch" not in record["reason_codes"]
    assert json.dumps(state, ensure_ascii=False, sort_keys=True) == before


def test_local_evidence_reuses_caller_source_cache_for_repeated_candidate_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay round must read and parse each immutable source once."""
    paths = _local_sources(tmp_path)
    source_cache: dict[str, dict[str, Any]] = {}
    read_counts: dict[str, int] = {}
    parse_counts = {"csv": 0, "json": 0}
    original_read_bytes = Path.read_bytes
    original_read_csv = shadow.pd.read_csv
    original_json_loads = shadow.json.loads

    def counted_read_bytes(path: Path) -> bytes:
        resolved = str(path.resolve())
        if str(tmp_path) in resolved:
            read_counts[resolved] = read_counts.get(resolved, 0) + 1
        return original_read_bytes(path)

    def counted_read_csv(*args, **kwargs):
        parse_counts["csv"] += 1
        return original_read_csv(*args, **kwargs)

    def counted_json_loads(*args, **kwargs):
        parse_counts["json"] += 1
        return original_json_loads(*args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    monkeypatch.setattr(shadow.pd, "read_csv", counted_read_csv)
    monkeypatch.setattr(shadow.json, "loads", counted_json_loads)
    results = []
    for _ in range(2):
        results.append(shadow.collect_local_risk_evidence(
            funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
            candidate=_formal_candidate(),
            side="buy",
            reference_price=10.0,
            as_of="2026-07-17",
            daily_dir=paths["daily_dir"],
            candidate_source_path=paths["candidate_path"],
            ledger_source_path="memory://stage4-forward-replay/A",
            sector_rotation_path=paths["sector_path"],
            source_cache=source_cache,
        ))

    assert source_cache
    assert len(read_counts) == 4
    assert all(count == 1 for count in read_counts.values())
    assert parse_counts == {"csv": 2, "json": 2}
    assert results[0][1] == results[1][1] == {}
    for evidence, _missing in results:
        assert all(component["data_cutoff"] <= "2026-07-17" for component in evidence.values())
        assert all(
            component["source_hash"] == compute_risk_evidence_source_hash(component)
            for component in evidence.values()
        )


def test_unknown_sector_falls_back_to_real_industry_evidence(tmp_path: Path) -> None:
    paths = _local_sources(tmp_path)
    ledger = _ledger()
    ledger["positions"][0]["sector"] = "未知板块"
    ledger["positions"][0]["industry"] = "bank"
    candidate = _formal_candidate()
    candidate["sector"] = "未知板块"
    candidate["industry"] = "bank"
    paths["ledger_path"].write_text(json.dumps(ledger), encoding="utf-8")

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(ledger),
        candidate=candidate,
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "sector" not in missing
    assert evidence["sector"]["value"] == "bank"
    assert evidence["sector"]["by_symbol"] == {"000001": "bank", "600000": "bank"}


def test_price_provenance_can_bind_intraday_trigger_source_and_cutoff(tmp_path: Path) -> None:
    paths = _local_sources(tmp_path)
    minute_source = tmp_path / "data" / "intraday" / "1m" / "600000.csv"
    minute_source.parent.mkdir(parents=True, exist_ok=True)
    minute_source.write_text(
        "symbol,datetime,close\n600000,2026-07-17 09:30:00,10.25\n",
        encoding="utf-8",
    )

    evidence, _missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.25,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
        price_source_path=minute_source,
        price_data_cutoff="2026-07-17",
    )

    assert evidence["price"]["value"] == 10.25
    assert evidence["price"]["source_path"] == str(minute_source)
    assert evidence["price"]["data_cutoff"] == "2026-07-17"
    assert evidence["price"]["source_hash"] == compute_risk_evidence_source_hash(
        evidence["price"]
    )


def test_original_mixed_basis_after_as_of_still_fails_closed(tmp_path: Path) -> None:
    paths = _local_sources(tmp_path)
    target = paths["daily_dir"] / "600000.csv"
    frame = pd.read_csv(target)
    future = frame.iloc[-1].copy()
    future["date"] = "2026-07-20"
    future["adjust"] = "qfq"
    pd.concat([frame, pd.DataFrame([future])], ignore_index=True).to_csv(
        target,
        index=False,
        encoding="utf-8-sig",
    )

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "returns" not in evidence
    assert "smart_stop" not in evidence
    assert "returns_price_basis_not_raw" in missing["returns"]


def test_frame_set_validation_runs_for_complete_required_asset_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _local_sources(tmp_path)
    original = shadow.validate_price_frame_set
    calls: list[tuple[set[str], str]] = []

    def wrapped(frames, *, consumer):
        calls.append((set(frames), consumer))
        return original(frames, consumer=consumer)

    monkeypatch.setattr(shadow, "validate_price_frame_set", wrapped)
    shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert calls == [({"000001", "600000"}, "paper_execution")]


def test_degraded_or_synthetic_sector_snapshot_cannot_feed_smart_stop(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    payload = json.loads(paths["sector_path"].read_text(encoding="utf-8"))
    payload.update(
        {
            "ok": False,
            "status": "warning",
            "evidence_eligible": False,
            "synthetic_test_data": True,
            "reason_codes": ["synthetic_test_data"],
        }
    )
    paths["sector_path"].write_text(json.dumps(payload), encoding="utf-8")

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "smart_stop" not in evidence
    assert missing["smart_stop"] == ["smart_stop_local_evidence_missing"]


def test_future_authoritative_candidate_cutoff_cannot_be_laundered_by_nested_date(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    candidate = _formal_candidate()
    candidate["data_latest_date"] = "2026-07-20"
    candidate["source_item"] = {
        "latest_date": "2026-07-17",
        "close": 99.0,
        "amount_ma20": 99_000_000.0,
    }

    evidence, missing = shadow.collect_local_risk_evidence(
        funded_ledger=shadow.prepare_shadow_funded_ledger(_ledger()),
        candidate=candidate,
        side="buy",
        reference_price=99.0,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert "price" not in evidence
    assert "liquidity" not in evidence
    assert missing["price"] == ["price_local_evidence_missing"]
    assert missing["liquidity"] == ["liquidity_local_evidence_missing"]


def test_formal_buy_shadow_can_pass_without_changing_ledger(tmp_path: Path) -> None:
    paths = _local_sources(tmp_path)
    state = _ledger()
    before = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    record = shadow.evaluate_shadow_risk_order(
        state=state,
        candidate=_formal_candidate(),
        side="buy",
        reference_price=10.0,
        requested_quantity=100,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
        risk_gate_enforced=False,
    )

    assert json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == before
    assert record["status"] == "passed"
    assert record["would_pass"] is True
    assert record["would_block"] is False
    assert record["insufficient_evidence"] is False
    assert record["intent_digest"].startswith("sha256:")
    assert record["decision_digest"].startswith("sha256:")
    assert record["risk_gate_enforced"] is False
    assert record["enforcement_applied"] is False


def test_next_day_execution_validates_lifecycle_at_candidate_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_date = "2026-07-16"
    trade_date = "2026-07-17"
    paths = _local_sources(tmp_path)
    daily_path = paths["daily_dir"] / "600000.csv"
    frame = pd.read_csv(daily_path, encoding="utf-8-sig")
    frame.loc[frame.index[-2], "close"] = 10.0
    frame.loc[frame.index[-1], "close"] = 10.5
    frame["amount"] = 50_000_000.0
    frame.to_csv(daily_path, index=False, encoding="utf-8-sig")

    candidate = _formal_candidate()
    candidate.pop("evidence")
    candidate.update(
        {
            "data_latest_date": signal_date,
            "daily_feature_latest_date": signal_date,
            "is_target_date_covered": True,
        }
    )
    paths["candidate_path"].write_text(
        json.dumps({"standard_candidates": [candidate]}), encoding="utf-8"
    )

    history_dir = tmp_path / "data" / "sector_history"
    snapshot = history_dir / signal_date / "snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot_payload = {
        "status": "passed",
        "target_date": signal_date,
        "usable_from": signal_date,
        "captured_at": f"{signal_date}T15:46:00+08:00",
        "evidence_eligible": True,
        "pit_eligible": True,
        "sector_membership": [
            {
                "symbol": "600000",
                "sector": "bank",
                "effective_from": signal_date,
                "observed_at": f"{signal_date}T15:46:00+08:00",
                "source": "synthetic_fixture",
            }
        ],
    }
    sealed_bytes = (json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    snapshot_payload["snapshot_hash"] = "sha256:" + hashlib.sha256(sealed_bytes).hexdigest()
    snapshot.write_text(
        json.dumps(snapshot_payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(lifecycle, "SECTOR_HISTORY_DIR", history_dir)

    price_path = tmp_path / "results" / "conditional_execution_latest.json"
    price_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "symbol": "600000",
                        "trade_date": trade_date,
                        "trigger_price": 10.5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    record = shadow.evaluate_shadow_risk_order(
        state=_ledger(),
        candidate=candidate,
        side="buy",
        reference_price=10.5,
        requested_quantity=100,
        as_of=trade_date,
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
        price_source_path=price_path,
        price_data_cutoff=trade_date,
    )

    assert record["status"] in {"passed", "vetoed"}
    assert record["insufficient_evidence"] is False
    assert not any(
        reason.startswith("paper_intent_") or reason.startswith("lifecycle_")
        for reason in record["reason_codes"]
    )


def test_missing_candidate_evidence_is_reported_not_fabricated(tmp_path: Path) -> None:
    candidate = {"symbol": "600000", "name": "unproven", "data_latest_date": "2026-07-17"}
    record = shadow.evaluate_shadow_risk_order(
        state=_ledger(),
        candidate=candidate,
        side="buy",
        reference_price=10.0,
        requested_quantity=100,
        as_of="2026-07-17",
        daily_dir=tmp_path / "missing-daily",
        candidate_source_path=tmp_path / "missing-candidate.json",
        ledger_source_path=tmp_path / "missing-ledger.json",
        sector_rotation_path=tmp_path / "missing-sector.json",
    )

    assert record["status"] == "insufficient_evidence"
    assert record["would_pass"] is False
    assert record["would_block"] is False
    assert record["insufficient_evidence"] is True
    assert "lifecycle_evidence_not_eligible" in record["reason_codes"]
    assert record["missing_component_count"] >= 1


def test_sell_record_precisely_labels_reduced_scope_and_liquidity_still_applies(
    tmp_path: Path,
) -> None:
    paths = _local_sources(tmp_path)
    record = shadow.evaluate_shadow_risk_order(
        state=_ledger(),
        candidate={"symbol": "000001", "sector": "bank", "amount_ma20": 50_000_000.0},
        side="sell",
        reference_price=10.0,
        requested_quantity=100,
        as_of="2026-07-17",
        daily_dir=paths["daily_dir"],
        candidate_source_path=paths["candidate_path"],
        ledger_source_path=paths["ledger_path"],
        sector_rotation_path=paths["sector_path"],
    )

    assert record["reduced_scope_sell"] is True
    assert record["reduced_scope_sell_skipped_checks"] == [
        "pre_trade",
        "concentration_veto",
        "var_veto",
    ]
    assert record["liquidity_veto_still_applicable"] is True
    assert "sell always passes" not in record["reduced_scope_sell_note"].lower()


def test_legacy_blocked_is_recorded_without_gate_evaluation() -> None:
    record = shadow.build_legacy_blocked_shadow_record(
        candidate={"symbol": "600000"},
        as_of="2026-07-17",
        reason="cash is insufficient",
        surface="auto_paper_daily_buy",
        risk_gate_enforced=False,
    )

    assert record["evaluated"] is False
    assert record["status"] == "legacy_blocked"
    assert record["reason_codes"] == ["legacy_blocked"]
    assert record["would_pass"] is False
    assert record["would_block"] is False


def test_summary_distinguishes_veto_from_insufficient_and_writes_own_artifact(
    tmp_path: Path,
) -> None:
    records = [
        {"status": "passed", "would_pass": True, "would_block": False, "insufficient_evidence": False, "reason_codes": []},
        {"status": "vetoed", "would_pass": False, "would_block": True, "insufficient_evidence": False, "reason_codes": ["liquidity_veto"]},
        {"status": "insufficient_evidence", "would_pass": False, "would_block": False, "insufficient_evidence": True, "reason_codes": ["returns_evidence_missing"], "missing_components": ["returns"]},
        {"status": "legacy_blocked", "evaluated": False, "would_pass": False, "would_block": False, "insufficient_evidence": False, "reason_codes": ["legacy_blocked"]},
    ]
    payload = shadow.summarize_shadow_records(
        records,
        as_of="2026-07-17",
        risk_gate_enforced=True,
    )

    assert payload["observed_count"] == 4
    assert payload["evaluation_count"] == 3
    assert payload["would_pass_count"] == 1
    assert payload["would_block_count"] == 1
    assert payload["insufficient_evidence_count"] == 1
    assert payload["legacy_blocked_count"] == 1
    assert payload["insufficient_evidence_ratio"] == pytest.approx(1 / 3)
    assert payload["risk_gate_enforced"] is True
    assert payload["enforcement_applied"] is False

    output = tmp_path / "results" / "risk_gate_shadow_latest.json"
    shadow.write_shadow_summary(payload, output_path=output)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == payload
    assert "影子风险门" in "\n".join(shadow.render_shadow_report_section(payload))


def test_default_shadow_paths_follow_runtime_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    errors = tmp_path / "results" / "risk_gate_shadow_errors.jsonl"
    monkeypatch.setattr(shadow, "RISK_GATE_SHADOW_LATEST_JSON", latest)
    monkeypatch.setattr(shadow, "RISK_GATE_SHADOW_ERRORS_JSONL", errors)
    payload = shadow.summarize_shadow_records([], as_of="2026-07-17")

    assert shadow.write_shadow_summary(payload) == latest
    assert latest.exists()
    assert shadow.record_shadow_error_best_effort(
        surface="path-isolation",
        error=RuntimeError("fixture"),
        as_of="2026-07-17",
    ) is True
    assert errors.exists()


def test_shadow_source_has_no_hard_coded_evidence_digest_literal() -> None:
    source = Path(shadow.__file__).read_text(encoding="utf-8")
    assert '"sha256:' not in source
    assert "'sha256:" not in source


def test_default_config_reserves_but_does_not_enable_enforcement() -> None:
    text = (Path(__file__).resolve().parents[1] / "config" / "default.yaml").read_text(
        encoding="utf-8"
    )
    assert "risk_gate_enforced: false" in text
