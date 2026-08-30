"""Tests for the unified daily-bar fetch facade (Phase 0.5).

The facade ``fetch_daily_unified`` returns a ``UnifiedDailyResult`` (a dict
subclass) that preserves the adapter payload and adds ``frame`` / ``adjust`` /
``adjust_consistent`` / ``warnings``.  It performs no network IO when
``network=False``; offline it falls back to local cache only.
"""

from __future__ import annotations

import pandas as pd

from quantdesk.data_providers import unified_fetch as uf


def test_facade_offline_returns_unified_dict() -> None:
    result = uf.fetch_daily_unified("000001", network=False)
    # dict subclass with the unified keys always present.
    assert isinstance(result, dict)
    assert "frame" in result
    assert "adjust" in result
    assert "adjust_consistent" in result
    assert "warnings" in result
    assert isinstance(result["frame"], pd.DataFrame)
    assert result.get("symbol") == "000001"


def test_facade_adjust_token_is_canonical() -> None:
    result = uf.fetch_daily_unified("000001", network=False)
    assert result["adjust"] in {"raw", "qfq", "hfq", "unknown"}


def test_infer_adjust_prefers_explicit_frame_column() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2026-01-02", "2026-01-05"],
            "close": [10.0, 10.5],
            "adjust": ["qfq", "qfq"],
        }
    )
    token = uf._infer_adjust({}, frame)
    assert token == "qfq"


def test_infer_adjust_falls_back_to_source_tag() -> None:
    empty = pd.DataFrame()
    assert uf._infer_adjust({"source": "tencent_daily"}, empty) == "qfq"
    assert uf._infer_adjust({"source": "akshare_daily"}, empty) == "raw"


def test_infer_adjust_unknown_when_no_signal() -> None:
    assert uf._infer_adjust({}, pd.DataFrame()) == "unknown"


def test_rows_to_frame_handles_list_and_df() -> None:
    df = uf._rows_to_frame({"data": [{"date": "2026-01-02", "close": 10.0}]})
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    df2 = uf._rows_to_frame({"data": pd.DataFrame({"close": [1.0]})})
    assert len(df2) == 1
    assert uf._rows_to_frame({}).empty


def test_facade_mixed_adjust_surfaces_warning(monkeypatch) -> None:
    def fake_adapter(symbol: str, start_date: str, end_date: str, *, network: bool) -> dict:
        return {
            "status": "success",
            "data": [
                {"date": "2026-01-02", "close": 10.0, "adjust": "qfq"},
                {"date": "2026-01-05", "close": 10.5, "adjust": "raw"},
            ],
        }

    monkeypatch.setattr(uf, "_adapter_get_daily_bars", fake_adapter)

    result = uf.fetch_daily_unified("000001", network=False)

    assert result["adjust"] == "unknown"
    assert result["adjust_consistent"] is False
    assert any("adjust_consistency_failed" in warning for warning in result["warnings"])
