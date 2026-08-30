from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.data_truth import candidate_lifecycle_evidence as lifecycle


def _write_fixture(root: Path) -> tuple[Path, Path, pd.DataFrame, list[str]]:
    daily_dir = root / "data" / "daily"
    history_dir = root / "data" / "sector_history"
    daily_dir.mkdir(parents=True)
    days = pd.bdate_range(end="2026-07-17", periods=30)
    frame = pd.DataFrame(
        {
            "date": [day.date().isoformat() for day in days],
            "symbol": ["600000"] * len(days),
            "open": [10.0 + index / 100 for index in range(len(days))],
            "high": [10.2 + index / 100 for index in range(len(days))],
            "low": [9.8 + index / 100 for index in range(len(days))],
            "close": [10.1 + index / 100 for index in range(len(days))],
            "volume": [1_000 + index for index in range(len(days))],
            "amount": [1_000_000 + index for index in range(len(days))],
            "adjust": ["raw"] * len(days),
        }
    )
    frame.to_csv(daily_dir / "600000.csv", index=False, encoding="utf-8-sig")

    as_of_days = [str(days[-2].date()), str(days[-1].date())]
    for day in as_of_days:
        snapshot = history_dir / day / "snapshot.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "target_date": day,
                    "evidence_eligible": True,
                    "pit_eligible": True,
                    "reason_codes": [],
                    "degraded_reasons": [],
                    "sector_membership": [
                        {
                            "symbol": "600000",
                            "sector": "Bank",
                            "effective_from": day,
                            "observed_at": f"{day}T15:30:00+08:00",
                            "source": "fixture",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    return daily_dir, history_dir, frame, as_of_days


def _candidate(frame: pd.DataFrame, as_of: str) -> dict[str, Any]:
    rows = frame[frame["date"] <= as_of]
    return {
        "symbol": "600000",
        "close": float(rows.iloc[-1]["close"]),
        "amount_ma20": float(rows.tail(20)["amount"].mean()),
        "data_latest_date": as_of,
        "daily_feature_latest_date": as_of,
        "is_target_date_covered": True,
    }


def test_candidate_lifecycle_cache_reuses_sources_without_mixing_as_of(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daily_dir, history_dir, frame, as_of_days = _write_fixture(tmp_path)
    source_cache: dict[str, Any] = {}
    reads = {"daily": 0, "membership": 0}
    original_read_csv_frame = lifecycle.read_csv_frame
    original_read_text = Path.read_text

    def counted_read_csv_frame(path: Path, *args: Any, **kwargs: Any) -> pd.DataFrame:
        if Path(path).resolve() == (daily_dir / "600000.csv").resolve():
            reads["daily"] += 1
        return original_read_csv_frame(path, *args, **kwargs)

    def counted_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if history_dir.resolve() in path.resolve().parents:
            reads["membership"] += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "read_csv_frame", counted_read_csv_frame)
    monkeypatch.setattr(Path, "read_text", counted_read_text)

    first_day, second_day = as_of_days
    first = lifecycle.derive_candidate_evidence(
        _candidate(frame, first_day),
        as_of=first_day,
        daily_dir=daily_dir,
        sector_history_dir=history_dir,
        source_cache=source_cache,
    )
    repeated = lifecycle.derive_candidate_evidence(
        _candidate(frame, first_day),
        as_of=first_day,
        daily_dir=daily_dir,
        sector_history_dir=history_dir,
        source_cache=source_cache,
    )
    second = lifecycle.derive_candidate_evidence(
        _candidate(frame, second_day),
        as_of=second_day,
        daily_dir=daily_dir,
        sector_history_dir=history_dir,
        source_cache=source_cache,
    )
    first_after_second = lifecycle.derive_candidate_evidence(
        _candidate(frame, first_day),
        as_of=first_day,
        daily_dir=daily_dir,
        sector_history_dir=history_dir,
        source_cache=source_cache,
    )

    assert reads == {"daily": 1, "membership": 2}
    assert all(payload["eligible"] is True for payload in (first, repeated, second, first_after_second))
    assert first == repeated == first_after_second
    assert (
        first["derivation"]["criteria"]["close_matches_canonical_raw"]
        ["canonical_last_date_at_or_before_as_of"]
        == first_day
    )
    assert (
        second["derivation"]["criteria"]["close_matches_canonical_raw"]
        ["canonical_last_date_at_or_before_as_of"]
        == second_day
    )
