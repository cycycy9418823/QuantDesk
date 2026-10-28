"""Explicit synthetic final-minute evidence for positive contract tests only."""
from __future__ import annotations

from pathlib import Path
from datetime import timedelta

import pandas as pd

from quantdesk.intraday.minute_bar_lifecycle import normalize_bar_observation, observation_hash


def final_minute_fixture(frame: pd.DataFrame, *, convention: str = "minute_start") -> pd.DataFrame:
    """Seal caller-declared synthetic raw/share bars; never use on real inputs.

    Call this only for a test that intends to supply complete final evidence.
    Legacy, unknown-unit, provisional and invalid-data fixtures stay unsealed.
    """
    records = []
    for source in frame.to_dict("records"):
        row = dict(source)
        label = pd.Timestamp(row["datetime"]).to_pydatetime()
        end = label + timedelta(minutes=1) if convention == "minute_start" else label
        row["datetime"] = label.strftime("%Y-%m-%d %H:%M:%S")
        row.setdefault("price_basis", "raw")
        row.update(provider="deterministic_test_fixture", finality="final", is_final=True,
                   timestamp_convention=convention,
                   volume_unit="shares", amount_unit="CNY", volume_semantics="per_minute",
                   aggregation_method="synthetic_test_bar", source_quote_time=end.isoformat() + "+08:00")
        row = normalize_bar_observation(row, fetched_at=label.strftime("%Y-%m-%d") + "T16:00:00+08:00")
        row.update(revision=1, supersedes_hash="", revision_reason="")
        row["evidence_hash"] = observation_hash(row)
        records.append(row)
    return pd.DataFrame(records)


def seal_minute_csv_fixture(path: Path, *, convention: str = "minute_end") -> None:
    """Add explicit synthetic evidence to an existing temporary test CSV."""
    frame = pd.read_csv(path, dtype={"symbol": str})
    final_minute_fixture(frame, convention=convention).to_csv(path, index=False, encoding="utf-8-sig")
