from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quantdesk.daily_session_evidence import (
    BSE_SOURCE,
    build_bse_session_interval_evidence,
    build_baostock_session_evidence,
    write_bse_session_interval_evidence,
    write_baostock_session_evidence,
)
from quantdesk.session_expectation import build_session_expectation


TARGET_DATE = "2026-07-15"


def _prepare_root(tmp_path: Path) -> tuple[Path, Path]:
    daily_dir = tmp_path / "data" / "daily"
    metadata_dir = tmp_path / "data" / "metadata"
    daily_dir.mkdir(parents=True)
    metadata_dir.mkdir(parents=True)
    return daily_dir, metadata_dir


def _write_daily(
    daily_dir: Path,
    symbol: str,
    *,
    date: str = TARGET_DATE,
    volume: float = 100.0,
    adjust: str = "raw",
) -> None:
    pd.DataFrame(
        [
            {
                "date": date,
                "symbol": symbol,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": volume,
                "amount": volume * 10.0,
                "adjust": adjust,
            }
        ]
    ).to_csv(daily_dir / f"{symbol}.csv", index=False, encoding="utf-8-sig")


def test_raw_target_bar_classifies_trade_zero_volume_and_unknown(tmp_path: Path) -> None:
    daily_dir, _metadata_dir = _prepare_root(tmp_path)
    _write_daily(daily_dir, "000001", volume=100)
    _write_daily(daily_dir, "000002", volume=0)
    _write_daily(daily_dir, "000003", volume=0, adjust="qfq")

    payload = build_session_expectation(
        ["SZ000001", "000002.SZ", "000003", "000004"],
        TARGET_DATE,
        project_root=tmp_path,
    )

    assert payload["per_symbol"]["000001"]["classification"] == "expected_trade"
    assert payload["per_symbol"]["000001"]["reason"] == "raw_target_bar_positive_volume"
    assert payload["per_symbol"]["000002"]["classification"] == "verified_full_day_suspension"
    assert payload["per_symbol"]["000002"]["reason"] == "raw_target_bar_zero_volume"
    assert payload["per_symbol"]["000003"]["classification"] == "unknown"
    assert payload["per_symbol"]["000003"]["reason"] == "target_bar_price_basis_not_raw"
    assert payload["per_symbol"]["000004"]["classification"] == "unknown"
    assert payload["per_symbol"]["000004"]["reason"] == "missing_target_bar_and_no_verified_no_session_evidence"
    assert payload["expected_trade_symbols"] == ["000001"]
    assert payload["legitimate_no_session_symbols"] == ["000002"]
    assert payload["unknown_symbols"] == ["000003", "000004"]

    daily_evidence = payload["source_evidence"]["daily_by_symbol"]["000001"]
    assert {"exists", "read_ok", "as_of", "sha256", "count", "error"} <= set(daily_evidence)
    assert daily_evidence["exists"] is True
    assert daily_evidence["read_ok"] is True
    assert daily_evidence["as_of"] == TARGET_DATE
    assert len(daily_evidence["sha256"]) == 64
    assert daily_evidence["count"] == 1


def test_exact_local_metadata_classifies_suspension_and_lifecycle(tmp_path: Path) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    pd.DataFrame(
        [
            {"trade_date": TARGET_DATE, "symbol": "000010", "is_suspended": True, "suspension_reason": "event"},
            {"trade_date": TARGET_DATE, "symbol": "000011", "is_suspended": False, "suspension_reason": "normal"},
            {"trade_date": "2026-07-14", "symbol": "000012", "is_suspended": True, "suspension_reason": "stale"},
        ]
    ).to_csv(metadata_dir / "suspension_status.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [
            {"trade_date": TARGET_DATE, "symbol": "000013", "suspend_timing": "09:30", "suspend_type": "full_day"},
            {"trade_date": "2026-07-14", "symbol": "000014", "suspend_timing": "09:30", "suspend_type": "full_day"},
        ]
    ).to_csv(metadata_dir / "suspension_history.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [
            {"symbol": "000020", "list_date": "2026-07-16", "delist_date": ""},
            {"symbol": "000021", "list_date": "2020-01-01", "delist_date": "2026-07-14"},
            {"symbol": "000022", "list_date": "2020-01-01", "delist_date": ""},
        ]
    ).to_csv(metadata_dir / "security_master.csv", index=False, encoding="utf-8-sig")

    symbols = ["000010", "000011", "000012", "000013", "000014", "000020", "000021", "000022"]
    payload = build_session_expectation(symbols, TARGET_DATE, project_root=tmp_path)

    assert payload["per_symbol"]["000010"]["classification"] == "verified_full_day_suspension"
    assert payload["per_symbol"]["000010"]["reason"] == "exact_date_suspension_status"
    assert payload["per_symbol"]["000013"]["classification"] == "verified_full_day_suspension"
    assert payload["per_symbol"]["000013"]["reason"] == "exact_date_suspension_history"
    assert payload["per_symbol"]["000020"]["classification"] == "not_yet_listed"
    assert payload["per_symbol"]["000021"]["classification"] == "delisted_before_date"
    for symbol in ("000011", "000012", "000014", "000022"):
        assert payload["per_symbol"][symbol]["classification"] == "unknown"

    assert payload["legitimate_no_session_symbols"] == ["000010", "000013", "000020", "000021"]
    assert payload["source_evidence"]["suspension_status"]["matched_count"] == 1
    assert payload["source_evidence"]["suspension_history"]["matched_count"] == 1
    assert payload["source_evidence"]["security_master"]["matched_count"] == 3
    for source in ("suspension_status", "suspension_history", "security_master"):
        evidence = payload["source_evidence"][source]
        assert {"exists", "read_ok", "as_of", "sha256", "count", "error"} <= set(evidence)


def test_refresh_suspension_json_classifies_halt_without_network(tmp_path: Path) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    evidence_dir = metadata_dir / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": TARGET_DATE,
                "source": "sina.Market_Center.getHQNodeData",
                "verified_exclusions": {
                    "600929": {
                        "signature": "zero_volume_with_last_close",
                        "open": "0.000",
                        "close": "6.040",
                        "volume": "0",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_session_expectation(
        ["600929", "000001"],
        TARGET_DATE,
        project_root=tmp_path,
        network=False,
    )

    assert payload["per_symbol"]["600929"]["classification"] == "verified_full_day_suspension"
    assert payload["per_symbol"]["600929"]["reason"]
    assert "600929" in payload["legitimate_no_session_symbols"]
    assert payload["unknown_symbols"] == ["000001"]
    assert payload["source_evidence"]["refresh_suspension_evidence"]["matched_count"] == 1


def test_null_refresh_exclusion_is_not_verified(tmp_path: Path) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    evidence_dir = metadata_dir / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": TARGET_DATE,
                "source": "test",
                "verified_exclusions": {"600929": None},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = build_session_expectation(
        ["600929"],
        TARGET_DATE,
        project_root=tmp_path,
        network=False,
    )
    assert payload["per_symbol"]["600929"]["classification"] != "verified_full_day_suspension"
    assert payload["source_evidence"]["refresh_suspension_evidence"]["verified_count"] == 0


def test_text_only_not_verified_reason_is_not_upgraded(tmp_path: Path) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    evidence_dir = metadata_dir / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": TARGET_DATE,
                "source": "test",
                "verified_exclusions": {"600929": {"reason": "not_verified"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = build_session_expectation(
        ["600929"],
        TARGET_DATE,
        project_root=tmp_path,
        network=False,
    )
    assert payload["per_symbol"]["600929"]["classification"] != "verified_full_day_suspension"
    assert payload["source_evidence"]["refresh_suspension_evidence"]["verified_count"] == 0


def test_local_suspension_metadata_requires_explicit_unconflicted_full_day_proof(
    tmp_path: Path,
) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    pd.DataFrame(
        [
            {
                "trade_date": TARGET_DATE,
                "symbol": "000081",
                "is_suspended": "maybe",
            },
            {
                "trade_date": TARGET_DATE,
                "symbol": "000082",
                "is_suspended": True,
            },
            {
                "trade_date": TARGET_DATE,
                "symbol": "000082",
                "is_suspended": False,
            },
            {
                "trade_date": TARGET_DATE,
                "symbol": "000083",
                "is_suspended": True,
            },
        ]
    ).to_csv(
        metadata_dir / "suspension_status.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(
        [
            {
                "trade_date": TARGET_DATE,
                "symbol": "000084",
                "suspend_timing": "09:30",
                "suspend_type": "intraday",
            },
            {
                "trade_date": TARGET_DATE,
                "symbol": "000085",
                "suspend_timing": "09:30",
                "suspend_type": "full_day",
            },
        ]
    ).to_csv(
        metadata_dir / "suspension_history.csv",
        index=False,
        encoding="utf-8-sig",
    )

    payload = build_session_expectation(
        ["000081", "000082", "000083", "000084", "000085"],
        TARGET_DATE,
        project_root=tmp_path,
    )

    assert payload["per_symbol"]["000081"]["classification"] == "unknown"
    assert payload["per_symbol"]["000082"]["classification"] == "unknown"
    assert payload["per_symbol"]["000083"]["classification"] == (
        "verified_full_day_suspension"
    )
    assert payload["per_symbol"]["000084"]["classification"] == "unknown"
    assert payload["per_symbol"]["000085"]["classification"] == (
        "verified_full_day_suspension"
    )
    assert payload["legitimate_no_session_symbols"] == [
        "000083",
        "000085",
    ]


def test_provider_success_retains_only_returned_normalized_requested_codes(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    calls: list[str] = []

    def provider(*, date: str) -> pd.DataFrame:
        calls.append(date)
        return pd.DataFrame(
            [
                {"\u4ee3\u7801": "SZ000031", "\u540d\u79f0": "A"},
                {"\u4ee3\u7801": "bad-code", "\u540d\u79f0": "bad"},
                {"\u4ee3\u7801": "600999", "\u540d\u79f0": "not-requested"},
            ]
        )

    payload = build_session_expectation(
        ["000031", "000032"],
        TARGET_DATE,
        project_root=tmp_path,
        network=True,
        provider=provider,
    )

    assert calls == ["20260715"]
    assert payload["per_symbol"]["000031"]["classification"] == "unknown"
    assert payload["per_symbol"]["000031"]["reason"] == "provider_halt_interval_unverified"
    assert payload["per_symbol"]["000032"]["classification"] == "unknown"
    evidence = payload["source_evidence"]["provider"]
    assert evidence["status"] == "success"
    assert evidence["exists"] is True
    assert evidence["read_ok"] is True
    assert evidence["count"] == 3
    assert evidence["matched_count"] == 1
    assert evidence["verified_count"] == 0
    assert set(evidence["records_by_symbol"]) == {"000031"}
    assert len(evidence["sha256"]) == 64


def test_provider_empty_and_failure_leave_symbols_unknown_with_evidence(tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    empty = build_session_expectation(
        ["000041"],
        TARGET_DATE,
        project_root=tmp_path,
        network=True,
        provider=lambda **_kwargs: pd.DataFrame(columns=["\u4ee3\u7801"]),
    )
    assert empty["per_symbol"]["000041"]["classification"] == "unknown"
    assert empty["expected_trade_symbols"] == []
    assert empty["legitimate_no_session_symbols"] == []
    assert empty["source_evidence"]["provider"]["status"] == "empty"
    assert empty["source_evidence"]["provider"]["read_ok"] is True
    assert empty["source_evidence"]["provider"]["count"] == 0

    def failing_provider(*, date: str) -> pd.DataFrame:
        raise RuntimeError(f"offline test failure for {date}")

    failed = build_session_expectation(
        ["000042"],
        TARGET_DATE,
        project_root=tmp_path,
        network=True,
        provider=failing_provider,
    )
    assert failed["per_symbol"]["000042"]["classification"] == "unknown"
    assert failed["source_evidence"]["provider"]["status"] == "failed"
    assert failed["source_evidence"]["provider"]["read_ok"] is False
    assert "RuntimeError" in failed["source_evidence"]["provider"]["error"]


def test_output_is_atomic_and_written_only_to_explicit_path(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    output_path = tmp_path / "explicit" / "session_expectation.json"

    without_output = build_session_expectation(["000051"], TARGET_DATE, project_root=tmp_path)
    assert not output_path.exists()
    assert without_output["output_path"] == ""

    written = build_session_expectation(
        ["000051"],
        TARGET_DATE,
        project_root=tmp_path,
        output_path=output_path,
    )

    assert output_path.exists()
    assert json.loads(output_path.read_text(encoding="utf-8")) == written
    assert written["output_path"] == str(output_path)
    assert list(output_path.parent.glob("*.tmp")) == []
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "reports").exists()

def test_fast_daily_status_fresh_raw_symbol_is_expected_trade(tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    payload = build_session_expectation(
        ["000061"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            "run_id": "fast-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["000061"],
            "raw_positive_volume_target_symbol_list": ["000061"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    assert payload["per_symbol"]["000061"]["classification"] == "expected_trade"
    assert payload["per_symbol"]["000061"]["reason"] == "raw_target_bar_positive_volume"


def test_fast_daily_status_reads_zero_volume_even_when_provider_misses(
    tmp_path: Path,
) -> None:
    daily_dir, _metadata_dir = _prepare_root(tmp_path)
    _write_daily(daily_dir, "920685", volume=0, adjust="raw")

    payload = build_session_expectation(
        ["920685"],
        TARGET_DATE,
        project_root=tmp_path,
        network=True,
        provider=lambda **_kwargs: pd.DataFrame(),
        fast_daily_status=True,
        daily_status={
            "run_id": "fast-provider-miss-zero-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["920685"],
            "raw_zero_volume_target_symbol_list": ["920685"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    row = payload["per_symbol"]["920685"]
    assert row["classification"] == "verified_full_day_suspension"
    assert row["reason"] == "raw_target_bar_zero_volume"
    assert payload["legitimate_no_session_symbols"] == ["920685"]
    assert payload["expected_trade_symbols"] == []


def test_fast_daily_status_rejects_mismatched_target_date(tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    payload = build_session_expectation(
        ["000061"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            "run_id": "stale-fast-unit",
            "target_date": "2026-07-14",
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["000061"],
            "raw_positive_volume_target_symbol_list": ["000061"],
        },
    )

    row = payload["per_symbol"]["000061"]
    assert row["classification"] == "unknown"
    assert row["reason"] == "missing_target_bar_and_no_verified_no_session_evidence"


def test_fast_daily_status_volume_classification_overlap_fails_closed(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)

    payload = build_session_expectation(
        ["000061"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            "run_id": "overlap-fast-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["000061"],
            "raw_positive_volume_target_symbol_list": ["000061"],
            "raw_zero_volume_target_symbol_list": ["000061"],
        },
    )

    row = payload["per_symbol"]["000061"]
    assert row["classification"] == "unknown"
    assert row["reason"] == "target_bar_volume_classification_conflict"
    assert any("target_volume_classification_conflict:000061" in item for item in payload["warnings"])


def test_fast_daily_status_digest_binds_volume_classification_lists(
    tmp_path: Path,
) -> None:
    _prepare_root(tmp_path)
    common = {
        "run_id": "digest-fast-unit",
        "target_date": TARGET_DATE,
        "status": "completed",
        "require_raw_price_basis": True,
        "fresh_symbol_list_after": ["000061"],
        "stale_symbol_list_after": [],
        "failed_symbol_list": [],
    }

    positive = build_session_expectation(
        ["000061"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            **common,
            "raw_positive_volume_target_symbol_list": ["000061"],
            "raw_zero_volume_target_symbol_list": [],
        },
    )
    zero = build_session_expectation(
        ["000061"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            **common,
            "raw_positive_volume_target_symbol_list": [],
            "raw_zero_volume_target_symbol_list": ["000061"],
        },
    )

    assert (
        positive["source_evidence"]["daily_by_symbol"]["000061"]["sha256"]
        != zero["source_evidence"]["daily_by_symbol"]["000061"]["sha256"]
    )


def test_fast_daily_status_reads_provider_suspension_zero_volume_target_bar(
    tmp_path: Path,
) -> None:
    daily_dir, _metadata_dir = _prepare_root(tmp_path)
    _write_daily(daily_dir, "920685", volume=0, adjust="raw")
    calls: list[str] = []

    def provider(*, date: str) -> pd.DataFrame:
        calls.append(date)
        return pd.DataFrame([{"代码": "BJ920685", "名称": "fixture"}])

    payload = build_session_expectation(
        ["920685"],
        TARGET_DATE,
        project_root=tmp_path,
        network=True,
        provider=provider,
        fast_daily_status=True,
        daily_status={
            "run_id": "fast-provider-suspension-zero-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["920685"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    assert calls == ["20260715"]
    row = payload["per_symbol"]["920685"]
    assert row["classification"] == "verified_full_day_suspension"
    assert row["reason"] == "raw_target_bar_zero_volume"
    assert payload["legitimate_no_session_symbols"] == ["920685"]
    assert payload["expected_trade_symbols"] == []
    daily_evidence = payload["source_evidence"]["daily_by_symbol"]["920685"]
    assert daily_evidence["target_count"] == 1
    assert payload["source_evidence"]["provider"]["matched_count"] == 1


def test_fast_daily_status_surfaces_provider_suspension_positive_volume_conflict(
    tmp_path: Path,
) -> None:
    daily_dir, _metadata_dir = _prepare_root(tmp_path)
    _write_daily(daily_dir, "920685", volume=100, adjust="raw")

    payload = build_session_expectation(
        ["920685"],
        TARGET_DATE,
        project_root=tmp_path,
        network=True,
        provider=lambda **_kwargs: pd.DataFrame(
            [{"代码": "BJ920685", "名称": "fixture"}]
        ),
        fast_daily_status=True,
        daily_status={
            "run_id": "fast-provider-suspension-conflict-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["920685"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    row = payload["per_symbol"]["920685"]
    assert row["classification"] == "expected_trade"
    assert row["reason"] == "raw_target_bar_positive_volume"
    assert any("suspension_conflict" in warning for warning in row["warnings"])
    assert any("suspension_conflict" in warning for warning in payload["warnings"])

def test_fast_daily_status_uses_persisted_baostock_target_suspension(
    tmp_path: Path,
) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    evidence = build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-01",
        query_end_date=TARGET_DATE,
        records=[
            {"trade_date": "2026-07-14", "tradestatus": "1"},
            {"trade_date": TARGET_DATE, "tradestatus": "0"},
        ],
        captured_at="2026-07-15T18:00:00+08:00",
    )
    write_baostock_session_evidence(
        evidence,
        metadata_dir=metadata_dir,
        expected_symbol="600405",
    )

    payload = build_session_expectation(
        ["600405"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            "run_id": "fast-suspension-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["600405"],
            "verified_no_session_symbol_list": ["600405"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    row = payload["per_symbol"]["600405"]
    assert row["classification"] == "verified_full_day_suspension"
    assert row["reason"] == "baostock_exact_date_tradestatus_zero"
    assert payload["legitimate_no_session_symbols"] == ["600405"]
    assert any(
        item.get("source") == "baostock.query_history_k_data_plus"
        for item in row["evidence"]
    )


def test_session_expectation_preserves_bse_interval_evidence_source(
    tmp_path: Path,
) -> None:
    _daily_dir, metadata_dir = _prepare_root(tmp_path)
    evidence = build_bse_session_interval_evidence(
        symbol="920685",
        suspension_start_date="2026-07-16",
        resumption_date="2026-07-30",
        documents=[
            {
                "fact_type": "suspension_start",
                "effective_date": "2026-07-16",
                "publish_date": "2026-07-15",
                "title": "suspension",
                "url": "https://www.bse.cn/disclosure/start.pdf",
                "sha256": "1" * 64,
            },
            {
                "fact_type": "resumption",
                "effective_date": "2026-07-30",
                "publish_date": "2026-07-29",
                "title": "resumption",
                "url": "https://www.bse.cn/disclosure/resume.pdf",
                "sha256": "2" * 64,
            },
        ],
        captured_at="2026-08-04T20:05:00+08:00",
    )
    write_bse_session_interval_evidence(
        evidence,
        metadata_dir=metadata_dir,
    )

    payload = build_session_expectation(
        target_date="2026-07-28",
        symbols=["920685"],
        project_root=tmp_path,
        daily_status={
            "target_date": "2026-07-28",
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["920685"],
            "verified_no_session_symbol_list": ["920685"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    row = payload["per_symbol"]["920685"]
    assert row["classification"] == "verified_full_day_suspension"
    assert row["reason"] == "exchange_exact_date_suspension_interval"
    assert any(item.get("source") == BSE_SOURCE for item in row["evidence"])


def test_fast_status_reads_real_target_bar_when_suspension_sidecar_conflicts(
    tmp_path: Path,
) -> None:
    daily_dir, metadata_dir = _prepare_root(tmp_path)
    _write_daily(
        daily_dir,
        "600405",
        date=TARGET_DATE,
        volume=100,
        adjust="raw",
    )
    evidence = build_baostock_session_evidence(
        symbol="600405",
        provider_code="sh.600405",
        query_start_date="2026-07-14",
        query_end_date=TARGET_DATE,
        records=[
            {"trade_date": "2026-07-14", "tradestatus": "1"},
            {"trade_date": TARGET_DATE, "tradestatus": "0"},
        ],
        captured_at="2026-07-15T18:00:00+08:00",
    )
    write_baostock_session_evidence(
        evidence,
        metadata_dir=metadata_dir,
    )

    payload = build_session_expectation(
        ["600405"],
        TARGET_DATE,
        project_root=tmp_path,
        fast_daily_status=True,
        daily_status={
            "run_id": "fast-conflict-unit",
            "target_date": TARGET_DATE,
            "status": "completed",
            "require_raw_price_basis": True,
            "fresh_symbol_list_after": ["600405"],
            "verified_no_session_symbol_list": ["600405"],
            "stale_symbol_list_after": [],
            "failed_symbol_list": [],
        },
    )

    row = payload["per_symbol"]["600405"]
    assert row["classification"] == "expected_trade"
    assert row["reason"] == "raw_target_bar_positive_volume"
    assert payload["legitimate_no_session_symbols"] == []
    assert any(
        "suspension_conflict" in warning
        for warning in row["warnings"]
    )
    assert any(
        "suspension_conflict" in warning
        for warning in payload["warnings"]
    )


def test_raw_target_bar_overrides_conflicting_delist_with_warning(tmp_path: Path) -> None:
    daily_dir, metadata_dir = _prepare_root(tmp_path)
    _write_daily(daily_dir, "000071", volume=100)
    _write_daily(daily_dir, "000072", volume=0)
    pd.DataFrame(
        [
            {"symbol": "000071", "list_date": "2020-01-01", "delist_date": "2026-07-14"},
            {"symbol": "000072", "list_date": "2020-01-01", "delist_date": "2026-07-14"},
        ]
    ).to_csv(metadata_dir / "security_master.csv", index=False, encoding="utf-8-sig")

    payload = build_session_expectation(
        ["000071", "000072"],
        TARGET_DATE,
        project_root=tmp_path,
    )

    positive = payload["per_symbol"]["000071"]
    zero = payload["per_symbol"]["000072"]
    assert positive["classification"] == "expected_trade"
    assert zero["classification"] == "verified_full_day_suspension"
    for row in (positive, zero):
        assert any("lifecycle_conflict" in item and "overridden" in item for item in row["warnings"])
        assert any(
            item.get("conflict") == "lifecycle_vs_raw_daily"
            for item in row["evidence"]
        )
    assert any("lifecycle_conflict" in item for item in payload["warnings"])
