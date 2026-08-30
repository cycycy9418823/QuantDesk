from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

import quantdesk.daily_feature_engine as features


def _write_daily(path: Path, adjustments: list[object] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = len(adjustments) if adjustments is not None else 3
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-02", periods=count, freq="B").strftime("%Y-%m-%d"),
            "symbol": [path.stem] * count,
            "open": [10.0 + index for index in range(count)],
            "high": [10.5 + index for index in range(count)],
            "low": [9.5 + index for index in range(count)],
            "close": [10.2 + index for index in range(count)],
            "volume": [1_000 + index for index in range(count)],
            "amount": [10_200 + index * 1_000 for index in range(count)],
        }
    )
    if adjustments is not None:
        frame["adjust"] = adjustments
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _build(tmp_path: Path, symbols: list[str]) -> tuple[dict, Path, Path]:
    output = tmp_path / "daily_features.csv"
    summary = tmp_path / "daily_features_summary.json"
    payload = features.build_daily_features(
        only_low_price=False,
        daily_dir=tmp_path / "daily",
        output_path=output,
        summary_path=summary,
        symbols=symbols,
    )
    return payload, output, summary


def test_mixed_basis_is_blocked_before_feature_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw", "qfq"])

    def forbidden_feature_row(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("mixed price basis reached _feature_row")

    monkeypatch.setattr(features, "_feature_row", forbidden_feature_row)

    payload, _output, _summary = _build(tmp_path, ["000001"])

    assert payload["feature_count"] == 0
    assert [row["symbol"] for row in payload["skipped"]] == ["000001"]
    assert [row["reason"] for row in payload["skipped"]] == ["mixed_price_basis"]


def test_feature_gate_reports_accepted_and_excluded_price_evidence(tmp_path: Path) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw", "raw", "raw"])
    _write_daily(tmp_path / "daily" / "000002.csv", ["raw", "qfq"])
    _write_daily(tmp_path / "daily" / "000003.csv", None)

    payload, output, _summary = _build(tmp_path, ["000001", "000002", "000003"])

    assert payload["ok"] is True
    assert payload["status"] == "partial"
    assert payload["feature_count"] == 1
    assert payload["price_basis"] == "mixed_or_unknown"
    assert payload["accepted_price_basis"] == "raw"
    assert payload["accepted_basis_counts"] == {"raw": 1}
    assert payload["excluded_reason_counts"] == {
        "mixed_price_basis": 1,
        "unknown_price_basis": 1,
    }
    assert payload["per_symbol"]["000001"] == {
        "price_basis": "raw",
        "reason_code": "",
        "eligible": True,
        "latest_date": "2026-01-06",
    }
    assert payload["per_symbol"]["000002"]["reason_code"] == "mixed_price_basis"
    assert payload["per_symbol"]["000003"]["reason_code"] == "unknown_price_basis"
    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["symbol"] for row in rows] == ["000001"]


@pytest.mark.parametrize(
    ("adjust", "expected_basis"),
    [
        ("raw", "raw"),
        ("qfq", "forward_adjusted"),
        ("hfq", "back_adjusted"),
    ],
)
def test_feature_gate_preserves_each_pure_known_basis(
    tmp_path: Path,
    adjust: str,
    expected_basis: str,
) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", [adjust, adjust, adjust])

    payload, output, _summary = _build(tmp_path, ["000001"])

    assert payload["ok"] is True
    assert payload["status"] == "passed"
    assert payload["feature_count"] == 1
    assert payload["price_basis"] == expected_basis
    assert payload["accepted_price_basis"] == expected_basis
    assert payload["accepted_basis_counts"] == {expected_basis: 1}
    assert payload["excluded_reason_counts"] == {}
    assert payload["per_symbol"]["000001"]["eligible"] is True
    assert output.exists()


def test_cross_symbol_basis_conflict_fails_before_overwriting_outputs(tmp_path: Path) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw", "raw", "raw"])
    _write_daily(tmp_path / "daily" / "000002.csv", ["qfq", "qfq", "qfq"])
    output = tmp_path / "daily_features.csv"
    summary = tmp_path / "daily_features_summary.json"
    output.write_bytes(b"existing-feature-output\n")
    summary.write_bytes(b"existing-summary-output\n")
    output_before = output.read_bytes()
    summary_before = summary.read_bytes()

    payload = features.build_daily_features(
        only_low_price=False,
        daily_dir=tmp_path / "daily",
        output_path=output,
        summary_path=summary,
        symbols=["000001", "000002"],
    )

    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["feature_count"] == 0
    assert payload["candidate_feature_count"] == 2
    assert payload["price_basis"] == "mixed_or_unknown"
    assert payload["accepted_basis_counts"] == {"forward_adjusted": 1, "raw": 1}
    assert payload["excluded_reason_counts"] == {"cross_symbol_price_basis_mismatch": 2}
    assert payload["reason_codes"] == ["cross_symbol_price_basis_mismatch"]
    assert payload["per_symbol"]["000001"]["reason_code"] == "cross_symbol_price_basis_mismatch"
    assert payload["per_symbol"]["000002"]["reason_code"] == "cross_symbol_price_basis_mismatch"
    assert output.read_bytes() == output_before
    assert summary.read_bytes() == summary_before


def test_price_basis_is_classified_before_invalid_dates_are_filtered(tmp_path: Path) -> None:
    path = tmp_path / "daily" / "000001.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "date": "2026-01-02",
                "symbol": "000001",
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.2,
                "volume": 1_000,
                "amount": 10_200,
                "adjust": "raw",
            },
            {
                "date": "not-a-date",
                "symbol": "000001",
                "open": 11.0,
                "high": 11.5,
                "low": 10.5,
                "close": 11.2,
                "volume": 1_100,
                "amount": 12_320,
                "adjust": "qfq",
            },
        ]
    ).to_csv(path, index=False, encoding="utf-8-sig")

    payload, _output, _summary = _build(tmp_path, ["000001"])

    assert payload["feature_count"] == 0
    assert payload["skipped"][0]["reason"] == "mixed_price_basis"


def test_feature_status_preserves_price_policy_provenance(tmp_path: Path) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw", "raw", "raw"])
    payload, output, summary = _build(tmp_path, ["000001"])

    status = features.daily_feature_status(output_path=output, summary_path=summary)

    assert payload["status"] == "passed"
    assert status["price_basis"] == "raw"
    assert status["accepted_price_basis"] == "raw"
    assert status["price_policy_validation"]["status"] == "passed"


def test_feature_input_change_blocks_without_replacing_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_daily(tmp_path / "daily" / "000001.csv", ["raw", "raw", "raw"])
    output = tmp_path / "daily_features.csv"
    summary = tmp_path / "daily_features_summary.json"
    sentinel = b"old-feature-output\n"
    output.write_bytes(sentinel)
    summary.write_text("{}", encoding="utf-8")
    snapshots = iter(
        [
            {"digest": "before", "path_count": 1, "entries": []},
            {"digest": "after", "path_count": 1, "entries": []},
        ]
    )
    monkeypatch.setattr(features, "_feature_input_snapshot", lambda _paths: next(snapshots))

    payload = features.build_daily_features(
        only_low_price=False,
        daily_dir=tmp_path / "daily",
        output_path=output,
        summary_path=summary,
        symbols=["000001"],
    )

    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert output.read_bytes() == sentinel
    assert "feature_input_snapshot_changed_before_publish" in payload["warnings"]

