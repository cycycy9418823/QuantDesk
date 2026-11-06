"""Batch 1A-D (minimal): a factor table that ends before the valuation date is
reported as stale, never as "adjusted".

The real table stops at 2026-07-02. factor_at_or_before() then returns the
last row for every later date, the ratio reads 1.0, and 39 strategy-book
positions were labelled price_adjustment_status="adjusted" with ratio 1.0.
"""

from __future__ import annotations

from pathlib import Path

from quantdesk.paper.adjustment_policy import apply_paper_adjustment_policy, load_adjustment_factor_table


def _table(tmp_path: Path):
    meta = tmp_path / "data" / "metadata"
    meta.mkdir(parents=True)
    (meta / "adjustment_factors.csv").write_text(
        "trade_date,symbol,adj_factor,source\n"
        "2026-06-30,600000,16.5935,unit\n"
        "2026-07-02,600000,16.5935,unit\n",
        encoding="utf-8",
    )
    return load_adjustment_factor_table(meta)


def test_valuation_after_the_table_end_is_stale_not_adjusted(tmp_path: Path) -> None:
    position = {"symbol": "600000", "entry_date": "2026-08-05", "entry_price": 9.4047, "quantity": 500}
    row = apply_paper_adjustment_policy(position, latest_price=9.54, latest_date="2026-10-09",
                                        factors=_table(tmp_path))
    assert row["price_adjustment_status"] == "stale_adj_factor"
    assert row["price_adjustment_warning_level"] == "warning"
    assert row["adj_factor_table_last_date"] == "2026-07-02"
    assert "adjustment_factor_ratio" not in row
    assert row["market_value"] == round(9.54 * 500, 2)  # raw valuation, numbers unchanged


def test_valuation_inside_the_table_keeps_factor_as_research_reference(tmp_path: Path) -> None:
    position = {"symbol": "600000", "entry_date": "2026-06-30", "entry_price": 9.0, "quantity": 100}
    row = apply_paper_adjustment_policy(position, latest_price=9.5, latest_date="2026-07-02",
                                        factors=_table(tmp_path))
    assert row["price_adjustment_status"] == "factor_reference_only"
    assert row["factor_research_proxy"]["included_in_equity"] is False
