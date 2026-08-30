"""Round 13 Part E: Stage 4 tri-account replay adapter contracts."""

from __future__ import annotations

import hashlib
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from quantdesk.paper.stage4_final_evaluator import evaluate_stage4_final
from quantdesk.paper.stage4_forward_replay import interim_evidence_pack
from quantdesk.paper.stage4_maintenance_evidence import (
    build_stage4_maintenance_replay_section,
)
from quantdesk.paper.stage4_replay_adapter import (
    ACCOUNT_REPLAY_CONTRACTS,
    EXPECTED_ACCOUNT_IDS,
    MAINTENANCE_REPLAY_GATE_NAMES,
    adapt_stage4_replay,
    build_stage4_replay_adapter_fixture,
    run_real_stage4_replay_adapter,
)
from quantdesk.paper.stage4_replay_evidence import build_verified_daily_history
from quantdesk.paper import stage4_replay_adapter as replay_adapter
from quantdesk.paper import stage4_tri_account_replay as tri_replay


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_real_runner_seals_source_manifest_before_adapting(
    monkeypatch, tmp_path: Path
) -> None:
    dates = ["2026-07-27", "2026-07-28"]
    daily = tmp_path / "data" / "daily" / "600000.csv"
    minute = tmp_path / "data" / "intraday" / "1m" / "600000.csv"
    calendar = tmp_path / "data" / "calendar" / "a_share_trading_calendar.csv"
    low_price = tmp_path / "data" / "universe" / "low_price_learning_symbols.csv"
    benchmark = tmp_path / "data" / "benchmark" / "sh000001.csv"
    profiles = {
        account_id: tmp_path
        / "config"
        / "experiment_profiles"
        / f"exp_{account_id}_10k.json"
        for account_id in EXPECTED_ACCOUNT_IDS
    }
    snapshots = {
        day: tmp_path / "data" / "sector_history" / day / "snapshot.json"
        for day in dates
    }
    for path, content in (
        (daily, "date,close,adjust\n2026-07-27,10,raw\n"),
        (minute, "datetime,close\n2026-07-28 09:31:00,10\n"),
        (calendar, "date,is_open\n2026-07-27,1\n2026-07-28,1\n"),
        (low_price, "symbol,name,included\n600000,Bank,1\n"),
        (benchmark, "date,close\n2026-07-27,100\n2026-07-28,101\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for path in profiles.values():
        _write_json(path, {"experiment_account": path.stem[4]})
    for day, path in snapshots.items():
        _write_json(path, {"target_date": day})

    accounts = {
        account_id: {"account_id": account_id, "closed_trades": []}
        for account_id in EXPECTED_ACCOUNT_IDS
    }
    known_evidence_ref = (
        "sha256:" + hashlib.sha256(b"runner-account-A-evidence").hexdigest()
    )
    accounts["A"]["closed_trades"] = [
        {"evidence_ref": known_evidence_ref}
    ]
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter._load_profiles",
        lambda _path: (profiles, []),
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.trade_days_between",
        lambda *args, **kwargs: dates,
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter._load_sector_snapshots",
        lambda *args, **kwargs: (
            {
                day: {
                    "sector_membership": [
                        {"symbol": "600000", "industry": "bank", "name": "Bank"}
                    ]
                }
                for day in dates
            },
            snapshots,
            [],
        ),
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter._load_low_price_universe",
        lambda _path: (["600000"], {"600000": "Bank"}),
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.build_affordable_leaders_universe",
        lambda **kwargs: SimpleNamespace(
            symbols=["600000"], rows=[SimpleNamespace(symbol="600000", name="Bank")]
        ),
    )

    def write_audit(**kwargs):
        _write_json(Path(kwargs["output_path"]), {"ok": True})

    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.audit_minute_pool_window",
        write_audit,
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.load_window_verified_minute_pool",
        lambda **kwargs: {
            "snapshot_valid": True,
            "passed": True,
            "usable_by_symbol": {"600000": set(dates)},
        },
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.load_st_delist_data",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter._read_daily_enriched",
        lambda *args, **kwargs: pd.DataFrame(),
    )
    replay_calls: list[dict[str, object]] = []

    def capture_replay(*, account_id: str, **kwargs: object):
        replay_calls.append({"account_id": account_id, **kwargs})
        return accounts[account_id]

    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.replay_forward_account",
        capture_replay,
    )
    observed: dict[str, object] = {}

    def capture_adapter(account_results, *, source_manifest, **kwargs):
        observed["account_results"] = account_results
        observed["source_manifest"] = source_manifest
        return SimpleNamespace(quality_gate={"passed": False})

    monkeypatch.setattr(
        "quantdesk.paper.stage4_replay_adapter.adapt_stage4_replay",
        capture_adapter,
    )

    run_real_stage4_replay_adapter(
        tmp_path,
        window_start=dates[0],
        window_end=dates[-1],
    )

    captured_accounts = observed["account_results"]
    captured_manifest = observed["source_manifest"]
    assert isinstance(captured_accounts, dict)
    assert isinstance(captured_manifest, dict)
    assert captured_accounts == accounts
    assert len(replay_calls) == 3
    for call in replay_calls:
        status_paths = call["sector_status_paths"]
        assert isinstance(status_paths, dict)
        assert set(status_paths) == set(dates)
        assert all(Path(path).is_file() for path in status_paths.values())
        assert all(
            Path(status_paths[day]) != snapshots[day]
            for day in dates
        )
    declared_status_paths = captured_manifest["sector_status_paths"]
    assert set(declared_status_paths) == set(dates)
    source_file_paths = {
        row["path"] for row in captured_manifest["source_files"]
    }
    assert set(declared_status_paths.values()) <= source_file_paths

    empty_evidence_digest = "sha256:" + hashlib.sha256(
        json.dumps([], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    account_a_evidence_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            [known_evidence_ref], sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    assert account_a_evidence_digest != empty_evidence_digest
    assert captured_manifest["account_evidence_digests"] == {
        "A": account_a_evidence_digest,
        "B": empty_evidence_digest,
        "C": empty_evidence_digest,
    }

    projection = {
        key: value for key, value in captured_manifest.items() if key != "seal"
    }
    expected_seal = "sha256:" + hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert captured_manifest["seal"] == expected_seal


def _real_source_manifest(
    tmp_path: Path,
    accounts: dict[str, dict[str, object]],
    *,
    arbitrary_daily: bool = False,
    empty_profile: str | None = None,
) -> dict[str, object]:
    daily = tmp_path / "data" / "daily" / "600000.csv"
    minute = tmp_path / "data" / "intraday" / "1m" / "600000.csv"
    calendar = tmp_path / "data" / "calendar" / "a_share_trading_calendar.csv"
    low_price = tmp_path / "data" / "universe" / "low_price_learning_symbols.csv"
    st_history = tmp_path / "data" / "metadata" / "st_history.csv"
    delist_history = tmp_path / "data" / "metadata" / "delist_history.csv"
    suspension = tmp_path / "data" / "metadata" / "suspension_status.csv"
    benchmark = tmp_path / "data" / "benchmark" / "sh000001.csv"
    audit = tmp_path / "results" / "stage4_replay_adapter" / "fixture" / "minute_window_audit.json"
    theme = audit.parent / "t0_sector_membership.csv"
    master = audit.parent / "t0_security_master.csv"
    profile_paths: dict[str, str] = {}
    for path in (
        daily,
        minute,
        calendar,
        low_price,
        st_history,
        delist_history,
        suspension,
        benchmark,
        audit,
        theme,
        master,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    daily.write_text(
        "fixture-but-real-shaped-local-file\n"
        if arbitrary_daily
        else (
            "date,symbol,open,high,low,close,volume,amount,adjust,source,updated_at\n"
            "2026-07-27,600000,10,11,9,10,1000,10000,raw,fixture,2026-07-27T15:01:00\n"
        ),
        encoding="ascii",
    )
    minute.write_text(
        "datetime,symbol,open,high,low,close,volume,amount\n"
        "2026-07-28 09:31:00,600000,10,10,10,10,100,1000\n",
        encoding="ascii",
    )
    calendar.write_text("date,is_open\n2026-07-27,1\n", encoding="ascii")
    low_price.write_text("symbol,name,included\n600000,Bank,1\n", encoding="ascii")
    st_history.write_text("symbol,start_date,end_date\n", encoding="ascii")
    delist_history.write_text("symbol,delist_date\n", encoding="ascii")
    suspension.write_text("symbol,date,is_suspended\n", encoding="ascii")
    benchmark.write_text("date,close\n2026-07-27,100\n", encoding="ascii")
    _write_json(audit, {"status": "passed", "passed": True})
    theme.write_text("symbol,industry\n600000,bank\n", encoding="ascii")
    master.write_text("symbol,name\n600000,Bank\n", encoding="ascii")
    for account_id in EXPECTED_ACCOUNT_IDS:
        path = tmp_path / "config" / "experiment_profiles" / f"exp_{account_id}_10k.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        contract = ACCOUNT_REPLAY_CONTRACTS[account_id]
        profile = {
            "profile_id": "user_10k",
            "paper_capital": 10000.0,
            "research_universe": [contract["universe_label"]],
            "max_total_exposure_pct": 0.8,
            "max_single_position_pct": 0.2,
            "max_paper_holdings": 5,
            "min_evidence_samples": 30,
            "review_horizon_days": 20,
            "intraday_paper_review_enabled": account_id == "C",
            "acceptance_scenario": False,
            "paper_credit_enabled": False,
            "credit_limit": 0.0,
            "network_enabled": False,
            "llm_enabled": False,
            "full_a_scan_enabled": False,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "experiment_account": account_id,
            "experiment_signal": contract["experiment_signal"],
        }
        _write_json(path, {} if empty_profile == account_id else profile)
        profile_paths[account_id] = str(path)
    sector_paths: dict[str, Path] = {}
    dates = ["2026-07-27", "2026-07-28", "2026-07-30"]
    for day in dates:
        path = tmp_path / "data" / "sector_history" / day / "snapshot.json"
        _write_json(path, {"target_date": day})
        sector_paths[day] = path
    role_paths = {
        "calendar": calendar,
        "universe:A": low_price,
        "metadata:st": st_history,
        "metadata:delist": delist_history,
        "metadata:suspension": suspension,
        "benchmark": benchmark,
        "minute_window_audit": audit,
        "derived:t0_theme_map": theme,
        "derived:t0_security_master": master,
        "daily:600000": daily,
        "minute:600000": minute,
        **{f"profile:{key}": Path(value) for key, value in profile_paths.items()},
        **{f"sector_snapshot:{day}": path for day, path in sector_paths.items()},
    }
    manifest: dict[str, object] = {
        "schema_version": "stage4_replay_source_manifest_v1",
        "data_mode": "real_canonical",
        "network_used": False,
        "project_root": str(tmp_path),
        "daily_root": str(tmp_path / "data" / "daily"),
        "minute_root": str(tmp_path / "data" / "intraday" / "1m"),
        "calendar_path": str(calendar),
        "profile_paths": profile_paths,
        "window_dates": dates,
        "artifact_path": str(audit.parent / "adapter_result.json"),
        "account_specs": {
            key: dict(value) for key, value in ACCOUNT_REPLAY_CONTRACTS.items()
        },
        "account_universes": {key: ["600000"] for key in EXPECTED_ACCOUNT_IDS},
        "source_files": [
            {"role": role, "path": str(path), "sha256": _sha256(path)}
            for role, path in sorted(role_paths.items())
        ],
    }
    return replay_adapter.seal_real_source_manifest(manifest, accounts)


def _real_account(account_id: str, *, trade_index: int) -> dict[str, object]:
    trade_id = f"{account_id}-real-{trade_index}"
    digest = "sha256:" + hashlib.sha256(trade_id.encode("ascii")).hexdigest()
    entry_digest = "sha256:" + hashlib.sha256((trade_id + ":entry").encode("ascii")).hexdigest()
    exit_digest = "sha256:" + hashlib.sha256((trade_id + ":exit").encode("ascii")).hexdigest()
    entry_decision = "sha256:" + hashlib.sha256((trade_id + ":entry-decision").encode("ascii")).hexdigest()
    exit_decision = "sha256:" + hashlib.sha256((trade_id + ":exit-decision").encode("ascii")).hexdigest()
    return {
        "account_id": account_id,
        "candidate_count": 2,
        "fill_count": 1,
        "stage_counts": {"fill": 1, "gate": 1},
        "universe_label": ACCOUNT_REPLAY_CONTRACTS[account_id]["universe_label"],
        "strategy": ACCOUNT_REPLAY_CONTRACTS[account_id]["strategy"],
        "outcomes": [
            {
                "symbol": "600000",
                "signal_date": "2026-07-27",
                "execution_date": "2026-07-28",
                "stage": "fill",
                "status": "filled",
                "reason_codes": [],
            },
            {
                "symbol": "600001",
                "signal_date": "2026-07-28",
                "execution_date": "2026-07-30",
                "stage": "gate",
                "status": "insufficient_evidence",
                "reason_codes": ["fixture_real_shape_gate_reason"],
            },
        ],
        "closed_trades": [
            {
                "trade_id": trade_id,
                "account_id": account_id,
                "symbol": "600000",
                "entry_date": "2026-07-28",
                "exit_date": "2026-07-30",
                "hold_days": 2,
                "pnl_pct": 0.01,
                "fees": 10.0,
                "evidence_eligible": True,
                "evidence_ref": digest,
                "entry_evidence_ref": entry_digest,
                "exit_evidence_ref": exit_digest,
                "entry_decision_digest": entry_decision,
                "exit_decision_digest": exit_decision,
                "price_basis": "raw",
                "reason_codes": [],
            }
        ],
        "nav_series": [
            {"date": "2026-07-27", "nav": 10_000.0},
            {"date": "2026-07-30", "nav": 10_100.0},
        ],
        "exit_stage_counts": {},
        "exit_outcomes": [],
        "total_fees": 10.0,
        "fixture_or_synthetic": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }


def test_normal_fixture_bridges_both_existing_stage4_contracts() -> None:
    adapted = build_stage4_replay_adapter_fixture(kind="normal_sample")

    assert adapted.quality_gate["passed"] is True
    assert set(adapted.account_results) == set(EXPECTED_ACCOUNT_IDS)
    interim = interim_evidence_pack(
        "T0+8", adapted.account_results, as_of="2026-08-06"
    ).to_dict()
    assert set(interim["account_candidate_counts"]) == set(EXPECTED_ACCOUNT_IDS)

    evaluation = evaluate_stage4_final(
        adapted.replay_product,
        benchmark_by_date=adapted.benchmark_by_date,
    ).to_dict()
    assert evaluation["sample_sufficient"] is True
    assert evaluation["fixture_or_synthetic"] is True
    assert evaluation["verdict"] == "synthetic_fixture_not_conclusion"
    assert adapted.maintenance_eligible is False


def test_insufficient_fixture_remains_thin_and_synthetic() -> None:
    adapted = build_stage4_replay_adapter_fixture(kind="insufficient_sample")
    evaluation = evaluate_stage4_final(
        adapted.replay_product,
        benchmark_by_date=adapted.benchmark_by_date,
    ).to_dict()

    assert evaluation["insufficient_sample"] is True
    assert evaluation["fixture_or_synthetic"] is True
    assert evaluation["evidence_eligible"] is False
    assert "insufficient_sample" in evaluation["reason_codes"]


def test_benchmark_missing_fixture_is_never_extrapolated() -> None:
    adapted = build_stage4_replay_adapter_fixture(kind="benchmark_missing")
    evaluation = evaluate_stage4_final(
        adapted.replay_product,
        benchmark_by_date=adapted.benchmark_by_date,
    ).to_dict()

    assert evaluation["benchmark"]["status"] == "benchmark_unavailable"
    assert evaluation["benchmark"]["extrapolated"] is False
    assert evaluation["benchmark"]["benchmark_return"] is None


def test_real_canonical_shape_is_maintenance_eligible_but_thin(tmp_path: Path) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(EXPECTED_ACCOUNT_IDS, start=1)
    }
    adapted = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(tmp_path, accounts),
        benchmark_by_date={
            "2026-07-27": 100.0,
            "2026-07-28": 101.0,
            "2026-07-30": 102.0,
        },
    )
    evaluation = evaluate_stage4_final(
        adapted.replay_product,
        benchmark_by_date=adapted.benchmark_by_date,
    ).to_dict()

    assert adapted.quality_gate["passed"] is True
    assert adapted.fixture_or_synthetic is False
    assert adapted.maintenance_eligible is True
    assert evaluation["fixture_or_synthetic"] is False
    assert evaluation["verdict"] == "insufficient_sample"
    assert evaluation["metrics"]["effective_sample"] == 3


def test_real_mode_rejects_any_synthetic_marker(tmp_path: Path) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(EXPECTED_ACCOUNT_IDS, start=1)
    }
    accounts["B"]["source_path"] = "synthetic://forbidden"
    accounts["B"]["fixture_or_synthetic"] = True

    adapted = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(tmp_path, accounts),
    )

    assert adapted.quality_gate["passed"] is False
    assert "real_path_contains_synthetic_marker" in adapted.quality_gate["reason_codes"]
    assert adapted.maintenance_eligible is False


def test_adapter_rejects_missing_account_and_inconsistent_counts(tmp_path: Path) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(("A", "B"), start=1)
    }
    accounts["A"]["candidate_count"] = 99

    adapted = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(tmp_path, accounts),
    )

    assert adapted.quality_gate["passed"] is False
    assert "tri_account_set_mismatch" in adapted.quality_gate["reason_codes"]
    assert "account_candidate_stage_count_mismatch" in adapted.quality_gate["reason_codes"]


def test_maintenance_section_enables_only_real_quality_passed_adapter(
    tmp_path: Path,
) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(EXPECTED_ACCOUNT_IDS, start=1)
    }
    real = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(tmp_path, accounts),
    )
    fixture = build_stage4_replay_adapter_fixture(kind="normal_sample")

    enabled = build_stage4_maintenance_replay_section(
        real,
        milestone="T0+8",
        as_of="2026-08-06",
        maintenance_gates={name: True for name in MAINTENANCE_REPLAY_GATE_NAMES},
    )
    rejected = build_stage4_maintenance_replay_section(
        fixture, milestone="T0+8", as_of="2026-08-06"
    )

    assert enabled["account_replay"] == "enabled_real"
    assert set(enabled["account_results"]["account_candidate_counts"]) == set(
        EXPECTED_ACCOUNT_IDS
    )
    assert enabled["synthetic_data_used"] is False
    assert rejected["account_replay"] == "not_authorized"
    assert "account_results" not in rejected
    assert "stage4_replay_adapter_not_real_eligible" in rejected["reason_codes"]


def test_real_adapter_is_observed_but_not_enabled_when_a_maintenance_gate_fails(
    tmp_path: Path,
) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(EXPECTED_ACCOUNT_IDS, start=1)
    }
    real = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(tmp_path, accounts),
    )
    gates = {name: True for name in MAINTENANCE_REPLAY_GATE_NAMES}
    gates["shadow_counts"] = False

    section = build_stage4_maintenance_replay_section(
        real,
        milestone="T0+8",
        as_of="2026-08-06",
        maintenance_gates=gates,
    )

    assert section["account_replay"] == "observed_real_pending_maintenance_gates"
    assert section["account_results"]["quality_gate_passed"] is True
    assert section["formal_evidence_eligible"] is False
    assert "stage4_maintenance_gate_failed:shadow_counts" in section["reason_codes"]


def test_real_mode_rejects_empty_profile_and_arbitrary_daily_fixture(
    tmp_path: Path,
) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(EXPECTED_ACCOUNT_IDS, start=1)
    }
    invalid = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(
            tmp_path,
            accounts,
            arbitrary_daily=True,
            empty_profile="B",
        ),
    )

    assert invalid.quality_gate["passed"] is False
    assert "profile_B_content_invalid" in invalid.quality_gate["reason_codes"]
    assert "unknown_price_basis" in invalid.quality_gate["reason_codes"]


def test_real_account_contract_and_outcome_histogram_are_strict(tmp_path: Path) -> None:
    accounts = {
        account_id: _real_account(account_id, trade_index=index)
        for index, account_id in enumerate(EXPECTED_ACCOUNT_IDS, start=1)
    }
    accounts["C"]["strategy"] = "long"
    accounts["A"]["stage_counts"] = {"fill": 1, "evidence": 1}
    adapted = adapt_stage4_replay(
        accounts,
        window_dates=["2026-07-27", "2026-07-28", "2026-07-30"],
        source_manifest=_real_source_manifest(tmp_path, accounts),
    )

    assert adapted.quality_gate["passed"] is False
    assert "account_strategy_mismatch:C" in adapted.quality_gate["reason_codes"]
    assert "account_outcome_stage_histogram_mismatch:A" in adapted.quality_gate[
        "reason_codes"
    ]


def test_price_contract_blocks_unknown_mixed_and_cross_symbol_frames(
    tmp_path: Path,
) -> None:
    raw = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-27"]),
            "close": [10.0],
            "amount": [10000.0],
            "adjust": ["raw"],
        }
    )
    qfq = raw.copy()
    qfq["adjust"] = "qfq"
    mixed = pd.concat([raw, qfq], ignore_index=True)

    cross = tri_replay.replay_forward_account(
        account_id="A",
        strategy="long",
        symbols=["600000", "600001"],
        names={"600000": "A", "600001": "B"},
        universe_label="low_price_learning_pool",
        window_dates=["2026-07-27", "2026-07-28"],
        window_pool={},
        sector_snapshots={},
        sector_snapshot_paths={},
        st_delist=None,
        minute_snapshot_path=tmp_path / "audit.json",
        frame_cache={"600000": raw, "600001": qfq},
        data_mode="real_canonical",
    )
    mixed_result = tri_replay.replay_forward_account(
        account_id="A",
        strategy="long",
        symbols=["600000"],
        names={"600000": "A"},
        universe_label="low_price_learning_pool",
        window_dates=["2026-07-27", "2026-07-28"],
        window_pool={},
        sector_snapshots={},
        sector_snapshot_paths={},
        st_delist=None,
        minute_snapshot_path=tmp_path / "audit.json",
        frame_cache={"600000": mixed},
        data_mode="real_canonical",
    )

    assert cross.price_contract["eligible"] is False
    assert "consumer_price_basis_not_allowed" in cross.price_contract["reason_codes"]
    assert mixed_result.price_contract["eligible"] is False
    assert "mixed_price_basis" in mixed_result.price_contract["reason_codes"]
    assert cross.candidate_count == mixed_result.candidate_count == 0


def test_verified_daily_history_does_not_claim_raw_for_unknown_input(
    tmp_path: Path,
) -> None:
    path = tmp_path / "600000.csv"
    path.write_text("date,close\n2026-07-27,10\n", encoding="ascii")

    daily, reasons = build_verified_daily_history(
        "600000", "2026-07-27", "2026-07-28", daily_dir=tmp_path
    )

    assert daily is None
    assert "unknown_price_basis" in reasons


def test_forward_account_projects_buy_sell_closed_trade_and_nav(
    monkeypatch, tmp_path: Path
) -> None:
    dates = ["2026-07-27", "2026-07-28", "2026-07-29"]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "close": [10.0, 11.0, 11.0],
            "amount_ma20": [50_000_000.0] * 3,
        }
    )

    def signal(row, *, symbol: str, name: str):
        if str(row.get("date"))[:10] != dates[0]:
            return None
        return {
            "symbol": symbol,
            "name": name,
            "strategy": "long",
            "close": float(row["close"]),
        }

    def buy(**kwargs):
        event = SimpleNamespace(
            side="buy",
            cash_delta=-1_005.0,
            total_fee=5.0,
            evidence_digest="sha256:" + "1" * 64,
            fill_price=10.0,
            quantity=100,
        )
        return tri_replay.CandidateOutcome(
            "A",
            "600000",
            dates[0],
            dates[1],
            "fill",
            "filled",
            decision_digest="sha256:" + "2" * 64,
            fill_price=10.0,
            quantity=100,
            evidence_ref=event.evidence_digest,
            kernel_event=event,
        )

    def sell(**kwargs):
        event = SimpleNamespace(
            side="sell",
            cash_delta=1_095.0,
            total_fee=5.0,
            evidence_digest="sha256:" + "3" * 64,
            fill_price=11.0,
            quantity=100,
        )
        return tri_replay.CandidateOutcome(
            "A",
            "600000",
            dates[1],
            dates[2],
            "exit_fill",
            "filled",
            decision_digest="sha256:" + "4" * 64,
            fill_price=11.0,
            quantity=100,
            evidence_ref=event.evidence_digest,
            kernel_event=event,
        )

    def project(ledger, event):
        projected = copy.deepcopy(ledger)
        projected["account"]["cash"] += event.cash_delta
        if event.side == "buy":
            projected["positions"] = [
                {
                    "symbol": "600000",
                    "quantity": 100,
                    "sellable_quantity": 0,
                    "frozen_quantity": 100,
                    "avg_cost": 10.05,
                    "entry_date": dates[1],
                    "last_buy_date": dates[1],
                    "latest_price": 10.0,
                    "funded": True,
                    "price_basis": "raw",
                }
            ]
        else:
            projected["positions"] = []
        return projected, ()

    monkeypatch.setattr(tri_replay, "long_signal_from_row", signal)
    monkeypatch.setattr(tri_replay, "evaluate_exit_signals", lambda *args, **kwargs: (True, "fixture_exit"))
    monkeypatch.setattr(tri_replay, "_evaluate_candidate_buy", buy)
    monkeypatch.setattr(tri_replay, "_evaluate_position_sell", sell)
    monkeypatch.setattr(tri_replay, "_project_fill", project)
    snapshots = {
        day: {
            "sector_membership": [
                {"symbol": "600000", "industry": "bank", "name": "Bank"}
            ]
        }
        for day in dates
    }

    result = tri_replay.replay_forward_account(
        account_id="A",
        strategy="long",
        symbols=["600000"],
        names={"600000": "Bank"},
        universe_label="low_price_learning_pool",
        window_dates=dates,
        window_pool={},
        sector_snapshots=snapshots,
        sector_snapshot_paths={day: tmp_path / day / "snapshot.json" for day in dates},
        st_delist=None,
        minute_snapshot_path=tmp_path / "minute_audit.json",
        daily_dir=tmp_path,
        intraday_dir=tmp_path,
        calendar_path=tmp_path / "calendar.csv",
        frame_cache={"600000": frame},
    )

    assert result.candidate_count == 1
    assert result.fill_count == 1
    assert result.effective_sell_sample == 1
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0]["pnl_pct"] > 0
    assert result.closed_trades[0]["evidence_eligible"] is True
    assert [row["date"] for row in result.nav_series] == dates
