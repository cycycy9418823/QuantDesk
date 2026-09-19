"""Benchmark update actions must not impersonate verified target-day coverage."""

from __future__ import annotations

import socket
from unittest.mock import Mock

import pandas as pd
import pytest

from quantdesk.data_truth import benchmark_incremental_update as benchmark


SPEC = benchmark.BenchmarkSpec("fixture", "fixture.csv", "fixture")
TARGET = "2026-09-10"


@pytest.fixture(autouse=True)
def _forbid_live_network(monkeypatch):
    denied = Mock(side_effect=AssertionError("benchmark tests must stay offline"))
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    yield
    denied.assert_not_called()


def _frame(*rows: tuple[str, object]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["date", "close"])


def _project(tmp_path, frame: pd.DataFrame):
    root = tmp_path / "project"
    path = root / "data" / "benchmark" / SPEC.filename
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b"\xef\xbb\xbf" + frame.to_csv(index=False, lineterminator="\r\n").encode("utf-8")
    )
    return root, path


def _update(root, frame, **kwargs):
    def fetch(_symbol):
        return frame.copy()

    return benchmark.update_benchmark_files(
        root, specs=(SPEC,), fetcher=fetch, fetched_at="fixture", **kwargs
    )


def test_legacy_call_without_target_cannot_report_synced(tmp_path):
    frame = _frame(("2026-09-09", 100.0))
    root, path = _project(tmp_path, frame)
    before = path.read_bytes()

    payload = _update(root, frame)

    assert payload["status"] == "unchanged"
    item = payload["results"][0]
    assert item["status"] == "unchanged"
    assert item["target_date"] is None
    assert item["target_covered"] is None
    assert item["freshness_status"] == "target_not_supplied"
    assert item["fetch_status"] == "succeeded"
    assert path.read_bytes() == before


@pytest.mark.parametrize("specs", [(), (SPEC,)])
def test_empty_or_entirely_skipped_run_is_not_up_to_date(tmp_path, specs):
    fetch = Mock(side_effect=AssertionError("missing local files must not fetch"))
    payload = benchmark.update_benchmark_files(tmp_path, specs=specs, fetcher=fetch)
    assert payload["status"] == "skipped"
    assert payload["target_covered"] is None
    fetch.assert_not_called()


def test_dry_run_counts_only_planned_rows_as_planned(tmp_path):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()

    payload = _update(root, _frame(("2026-09-09", 100.0), (TARGET, 101.0)), dry_run=True)

    item = payload["results"][0]
    assert item["appended_count"] == 0
    assert item["appended_dates"] == []
    assert item["planned_dates"] == [TARGET]
    assert item["planned_count"] == 1
    assert payload["status"] == "dry_run"
    assert path.read_bytes() == before


def test_pure_plan_does_not_claim_any_actual_append():
    local = _frame(("2026-09-09", 100.0))
    rows, result = benchmark.plan_benchmark_appends(
        SPEC, list(local.columns), local,
        _frame(("2026-09-09", 100.0), (TARGET, 101.0)), fetched_at="fixture",
    )
    assert rows
    assert result.status == "planned"
    assert result.appended_dates == []
    assert result.as_dict()["planned_dates"] == [TARGET]


def test_successful_request_with_old_data_is_still_behind_target(tmp_path):
    frame = _frame(("2026-09-09", 100.0))
    root, path = _project(tmp_path, frame)
    before = path.read_bytes()

    payload = _update(root, frame, target_date=TARGET)

    item = payload["results"][0]
    assert payload["status"] == "partial"
    assert item["status"] == "unchanged"
    assert item["fetch_status"] == "succeeded"
    assert item["target_covered"] is False
    assert item["freshness_status"] == "behind_target"
    assert item["local_last_date_after"] == "2026-09-09"
    assert item["remote_last_date"] == "2026-09-09"
    assert "remote_behind_target" in item["reason_codes"]
    assert path.read_bytes() == before


def test_existing_valid_target_is_verified_current(tmp_path):
    frame = _frame(("2026-09-09", 100.0), (TARGET, 101.0))
    root, path = _project(tmp_path, frame)
    before = path.read_bytes()

    payload = _update(root, frame, target_date=TARGET)

    item = payload["results"][0]
    assert payload["status"] == item["status"] == "up_to_date"
    assert payload["target_covered"] is item["target_covered"] is True
    assert item["freshness_status"] == "current"
    assert item["appended_count"] == item["planned_count"] == 0
    assert path.read_bytes() == before


def test_nul_in_local_target_is_rejected_before_fetch(tmp_path):
    frame = _frame(("2026-09-09", 100.0), (TARGET, 101.0))
    root, path = _project(tmp_path, frame)
    damaged = path.read_bytes().replace(b",101.0\r\n", b",101.0\x00corrupt\r\n")
    assert b"\x00corrupt" in damaged
    path.write_bytes(damaged)
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        return frame.copy()

    payload = benchmark.update_benchmark_files(
        root, specs=(SPEC,), fetcher=fetch, target_date=TARGET
    )

    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["failure_stage"] == "local_read"
    assert item["fetch_status"] == "not_requested"
    assert item["target_covered"] is None
    assert item["freshness_status"] == "not_assessed"
    assert item["appended_count"] == item["planned_count"] == 0
    assert any("local_csv_contains_nul" in code for code in item["reason_codes"])
    assert payload["status"] == "degraded"
    assert calls == []
    assert path.read_bytes() == damaged


@pytest.mark.parametrize(
    "csv_text,reason",
    [
        pytest.param(
            "date,close\r\nextra,2026-09-09,100\r\nextra,2026-09-10,101\r\n",
            "local_csv_column_count_mismatch", id="extra-column-as-index",
        ),
        pytest.param(
            "date,close,volume\r\n2026-09-09,100,1000\r\n2026-09-10,101\r\n",
            "local_csv_column_count_mismatch", id="missing-trailing-column",
        ),
        pytest.param(
            "date,close,close\r\n2026-09-09,100,100\r\n2026-09-10,101,101\r\n",
            "local_csv_header_invalid", id="duplicate-header",
        ),
        pytest.param(
            "date,close,\r\n2026-09-09,100,1000\r\n2026-09-10,101,1001\r\n",
            "local_csv_header_invalid", id="empty-header",
        ),
    ],
)
def test_invalid_csv_structure_is_rejected_before_fetch(tmp_path, csv_text, reason):
    frame = _frame(("2026-09-09", 100.0), (TARGET, 101.0))
    root, path = _project(tmp_path, frame)
    damaged = b"\xef\xbb\xbf" + csv_text.encode("utf-8")
    path.write_bytes(damaged)
    fetch = Mock(return_value=frame)

    payload = benchmark.update_benchmark_files(
        root, specs=(SPEC,), fetcher=fetch, target_date=TARGET
    )

    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["failure_stage"] == "local_read"
    assert item["fetch_status"] == "not_requested"
    assert item["target_covered"] is None
    assert item["freshness_status"] == "not_assessed"
    assert item["appended_count"] == item["planned_count"] == 0
    assert any(reason in code for code in item["reason_codes"])
    assert payload["status"] == "degraded"
    fetch.assert_not_called()
    assert path.read_bytes() == damaged


def test_append_verifies_target_from_persisted_rows(tmp_path):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()

    payload = _update(
        root, _frame(("2026-09-09", 100.0), (TARGET, 101.0)), target_date=TARGET
    )

    item = payload["results"][0]
    assert payload["status"] == "updated"
    assert item["status"] == "appended"  # The staged publisher relies on this action.
    assert item["planned_dates"] == item["appended_dates"] == [TARGET]
    assert item["planned_count"] == item["appended_count"] == 1
    assert item["target_covered"] is True
    assert item["freshness_status"] == "current"
    assert item["local_last_date"] == "2026-09-09"
    assert item["local_last_date_after"] == TARGET
    assert path.read_bytes().startswith(before)
    assert pd.read_csv(path)["date"].tolist() == ["2026-09-09", TARGET]


def test_append_can_succeed_without_reaching_the_target(tmp_path):
    root, path = _project(tmp_path, _frame(("2026-09-08", 99.0)))
    payload = _update(
        root, _frame(("2026-09-08", 99.0), ("2026-09-09", 100.0)), target_date=TARGET
    )
    item = payload["results"][0]
    assert item["status"] == "appended"
    assert item["appended_dates"] == ["2026-09-09"]
    assert item["target_covered"] is False
    assert item["freshness_status"] == "behind_target"
    assert payload["status"] == "partial"
    assert pd.read_csv(path)["date"].tolist() == ["2026-09-08", "2026-09-09"]


def test_a_later_date_does_not_prove_the_target_row_exists(tmp_path):
    frame = _frame(("2026-09-09", 100.0), ("2026-09-11", 102.0))
    root, _ = _project(tmp_path, frame)
    payload = _update(root, frame, target_date=TARGET)
    item = payload["results"][0]
    assert item["target_covered"] is False
    assert item["freshness_status"] == "target_missing"
    assert "remote_target_missing" in item["reason_codes"]
    assert payload["status"] == "partial"


@pytest.mark.parametrize("close", ["NaN", "inf", "-inf", "0", "-2", "", "bad"])
def test_existing_invalid_target_price_is_not_coverage(tmp_path, close):
    frame = _frame(("2026-09-09", 100.0), (TARGET, close))
    root, path = _project(tmp_path, frame)
    before = path.read_bytes()
    payload = _update(root, frame, target_date=TARGET)
    item = payload["results"][0]
    assert item["target_covered"] is False
    assert item["freshness_status"] == "target_unusable"
    assert payload["status"] != "up_to_date"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad_values",
    [{"open": "inf"}, {"high": 99}, {"low": 102}, {"open": 0}, {"low": -1}],
)
def test_target_ohlc_must_be_finite_positive_and_consistent(tmp_path, bad_values):
    frame = pd.DataFrame([
        {"date": "2026-09-09", "open": 99, "high": 101, "low": 98, "close": 100},
        {"date": TARGET, "open": 100, "high": 102, "low": 99, "close": 101, **bad_values},
    ])
    root, _ = _project(tmp_path, frame)
    item = _update(root, frame, target_date=TARGET)["results"][0]
    assert item["target_covered"] is False
    assert item["freshness_status"] == "target_unusable"


@pytest.mark.parametrize("bad_values", [{"open": "inf"}, {"high": 99}, {"low": 102}])
def test_narrow_local_schema_cannot_hide_invalid_remote_target_ohlc(tmp_path, bad_values):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()
    remote = pd.DataFrame([
        {"date": "2026-09-09", "open": 99, "high": 101, "low": 98, "close": 100},
        {"date": TARGET, "open": 100, "high": 102, "low": 99, "close": 101, **bad_values},
    ])
    payload = _update(root, remote, target_date=TARGET)
    item = payload["results"][0]
    assert item["target_covered"] is False
    assert item["appended_dates"] == []
    assert "remote_target_unusable" in item["reason_codes"]
    assert payload["status"] == "degraded"
    assert path.read_bytes() == before


def test_duplicate_local_target_rows_are_not_verified_coverage(tmp_path):
    frame = _frame(("2026-09-09", 100.0), (TARGET, 101.0), (TARGET, 101.0))
    root, path = _project(tmp_path, frame)
    before = path.read_bytes()
    item = _update(root, frame, target_date=TARGET)["results"][0]
    assert item["target_covered"] is False
    assert item["freshness_status"] == "target_unusable"
    assert path.read_bytes() == before


@pytest.mark.parametrize("target_values", [("bad", 101.0), (101.0, "bad")])
def test_remote_deduplication_cannot_hide_a_bad_target_row(tmp_path, target_values):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()
    payload = _update(
        root,
        _frame(("2026-09-09", 100.0), (TARGET, target_values[0]), (TARGET, target_values[1])),
        target_date=TARGET,
    )
    item = payload["results"][0]
    assert item["status"] == "blocked"
    assert item["target_covered"] is False
    assert "remote_target_unusable" in item["reason_codes"]
    assert item["appended_dates"] == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("target_exists", [False, True])
def test_fetch_error_preserves_local_evidence_without_claiming_remote_delay(tmp_path, target_exists):
    frame = _frame(("2026-09-09", 100.0))
    if target_exists:
        frame = pd.concat([frame, _frame((TARGET, 101.0))], ignore_index=True)
    root, path = _project(tmp_path, frame)
    before = path.read_bytes()

    def fetch(_symbol):
        raise ConnectionError("fixture connection lost")

    payload = benchmark.update_benchmark_files(
        root, specs=(SPEC,), fetcher=fetch, target_date=TARGET
    )
    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["fetch_status"] == "failed"
    assert item["failure_stage"] == "fetch"
    assert item["local_last_date_after"] == (TARGET if target_exists else "2026-09-09")
    assert item["target_covered"] is target_exists
    assert not any(code.startswith("remote_") for code in item["reason_codes"])
    assert payload["status"] == "degraded"
    assert path.read_bytes() == before


@pytest.mark.parametrize("remote", [pd.DataFrame(), _frame()])
def test_successful_but_empty_response_is_not_synced(tmp_path, remote):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()
    payload = _update(root, remote, target_date=TARGET)
    item = payload["results"][0]
    assert item["fetch_status"] == "succeeded"
    assert item["target_covered"] is False
    assert payload["status"] == "degraded"
    assert path.read_bytes() == before


def test_unusable_new_rows_do_not_make_the_run_current(tmp_path):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()
    payload = _update(root, _frame(("2026-09-09", 100.0), (TARGET, "bad")), target_date=TARGET)
    item = payload["results"][0]
    assert item["status"] == "no_usable_rows"
    assert item["fetch_status"] == "succeeded"
    assert item["target_covered"] is False
    assert payload["status"] == "degraded"
    assert path.read_bytes() == before


def test_dry_run_target_coverage_comes_from_disk_not_the_plan(tmp_path):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()
    payload = _update(
        root, _frame(("2026-09-09", 100.0), (TARGET, 101.0)),
        target_date=TARGET, dry_run=True,
    )
    item = payload["results"][0]
    assert payload["status"] == "dry_run"
    assert item["planned_dates"] == [TARGET]
    assert item["appended_dates"] == []
    assert item["target_covered"] is False
    assert item["freshness_status"] == "behind_target"
    assert item["local_last_date_after"] == "2026-09-09"
    assert path.read_bytes() == before


@pytest.mark.parametrize("behavior", ["raise", "no_write"])
def test_write_failure_cannot_report_a_successful_append(tmp_path, monkeypatch, behavior):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()

    def append(*_args):
        if behavior == "raise":
            raise OSError("fixture write error")

    monkeypatch.setattr(benchmark, "_append_rows", append)
    payload = _update(root, _frame(("2026-09-09", 100.0), (TARGET, 101.0)), target_date=TARGET)
    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["failure_stage"] == ("append" if behavior == "raise" else "verify")
    assert item["fetch_status"] == "succeeded"
    assert item["planned_dates"] == [TARGET]
    assert item["appended_dates"] == []
    assert item["target_covered"] is False
    assert payload["status"] == "degraded"
    assert path.read_bytes() == before


def test_nul_in_appended_target_fails_post_write_verification(tmp_path, monkeypatch):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()

    def append_corrupted(path, rows, terminator):
        payload = "".join(",".join(row) + "\x00corrupt" + terminator for row in rows)
        with path.open("ab") as handle:
            handle.write(payload.encode("utf-8"))

    monkeypatch.setattr(benchmark, "_append_rows", append_corrupted)
    payload = _update(root, _frame(("2026-09-09", 100.0), (TARGET, 101.0)), target_date=TARGET)

    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["failure_stage"] == "verify"
    assert item["fetch_status"] == "succeeded"
    assert item["planned_dates"] == [TARGET]
    assert item["appended_dates"] == []
    assert item["local_last_date"] == "2026-09-09"
    assert item["local_last_date_after"] == ""
    assert item["target_covered"] is None
    assert item["freshness_status"] == "not_assessed"
    assert any("local_csv_contains_nul" in code for code in item["reason_codes"])
    assert payload["status"] == "degraded"
    assert path.read_bytes().startswith(before)
    assert b"\x00corrupt" in path.read_bytes()


def test_missing_appended_csv_column_fails_post_write_verification(tmp_path, monkeypatch):
    local = pd.DataFrame(
        [("2026-09-09", 100.0, 1000)], columns=["date", "close", "volume"]
    )
    root, path = _project(tmp_path, local)
    before = path.read_bytes()

    def append_truncated(path, rows, terminator):
        assert rows == [[TARGET, "101", ""]]
        payload = "".join(",".join(row[:-1]) + terminator for row in rows)
        with path.open("ab") as handle:
            handle.write(payload.encode("utf-8"))

    monkeypatch.setattr(benchmark, "_append_rows", append_truncated)
    payload = _update(root, _frame(("2026-09-09", 100.0), (TARGET, 101.0)), target_date=TARGET)

    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["failure_stage"] == "verify"
    assert item["fetch_status"] == "succeeded"
    assert item["planned_dates"] == [TARGET]
    assert item["appended_dates"] == []
    assert item["local_last_date"] == "2026-09-09"
    assert item["local_last_date_after"] == ""
    assert item["target_covered"] is None
    assert item["freshness_status"] == "not_assessed"
    assert any("local_csv_column_count_mismatch" in code for code in item["reason_codes"])
    assert payload["status"] == "degraded"
    assert path.read_bytes() == before + f"{TARGET},101\r\n".encode("utf-8")


def test_partial_write_records_only_rows_really_present(tmp_path, monkeypatch):
    root, path = _project(tmp_path, _frame(("2026-09-08", 99.0)))
    append_rows = benchmark._append_rows

    def append_then_fail(path, rows, terminator):
        append_rows(path, rows[:1], terminator)
        raise OSError("fixture interrupted write")

    monkeypatch.setattr(benchmark, "_append_rows", append_then_fail)
    payload = _update(
        root, _frame(("2026-09-08", 99.0), ("2026-09-09", 100.0), (TARGET, 101.0)),
        target_date=TARGET,
    )
    item = payload["results"][0]
    assert item["status"] == "failed"
    assert item["planned_dates"] == ["2026-09-09", TARGET]
    assert item["appended_dates"] == ["2026-09-09"]
    assert item["local_last_date_after"] == "2026-09-09"
    assert item["target_covered"] is False
    assert pd.read_csv(path)["date"].tolist() == ["2026-09-08", "2026-09-09"]


@pytest.mark.parametrize(
    "target", ["", "2026-9-10", "2026-09-31", "20260910", "2026-09-10T00:00:00", "bad", 20260910],
)
def test_invalid_target_is_rejected_before_fetch_or_write(tmp_path, target):
    root, path = _project(tmp_path, _frame(("2026-09-09", 100.0)))
    before = path.read_bytes()
    fetch = Mock(side_effect=AssertionError("invalid targets must not fetch"))
    with pytest.raises(ValueError, match="target_date"):
        benchmark.update_benchmark_files(root, specs=(SPEC,), fetcher=fetch, target_date=target)
    fetch.assert_not_called()
    assert path.read_bytes() == before
