"""Option C (dual-track price-basis labelling) acceptance tests.

Covers the shared ``quantdesk.price_basis`` helper and its wiring into the
core research / backtest / report / route payloads:

- Option C fields (``price_basis`` / ``adjustment_basis`` / ``adjustment_warnings``)
  are present on the payloads.
- A ``forward`` request that falls back to flat raw ``data/daily`` emits a
  visible fallback warning.
- A divergent ``adjustment_factor.csv`` alias emits a visible warning.
- The alias is never used as a fallback data source.
- The helper never writes to ``data/daily``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantdesk import price_basis
from quantdesk.price_basis import (
    DIVERGENT_ALIAS_WARNING,
    FORWARD_FALLBACK_WARNING,
    PRICE_BASIS_FORWARD,
    PRICE_BASIS_RAW,
    build_adjustment_basis,
    normalize_price_basis,
    price_basis_labels,
)


def _write_daily_csv(path: Path, symbol: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["date,symbol,open,high,low,close,volume,amount"]
    for i in range(130):
        day = 2 + i
        rows.append(f"2026-01-{day:02d},{symbol},10,10.5,9.5,10.2,1000,10200")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _canonical_and_divergent_alias(metadata_dir: Path) -> None:
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "adjustment_factors.csv").write_text(
        "trade_date,symbol,adj_factor,source\n2026-01-02,000001,1.0,proxy\n",
        encoding="utf-8",
    )
    # A divergent alias: different rows / sample from the canonical file.
    (metadata_dir / "adjustment_factor.csv").write_text(
        "trade_date,symbol,adj_factor,source\n2026-01-02,000001,2.5,alias\n2026-01-03,000002,3.0,alias\n",
        encoding="utf-8",
    )


def test_normalize_price_basis_maps_common_aliases() -> None:
    assert normalize_price_basis("") == PRICE_BASIS_RAW
    assert normalize_price_basis("raw") == PRICE_BASIS_RAW
    assert normalize_price_basis("qfq") == PRICE_BASIS_FORWARD
    assert normalize_price_basis("forward") == PRICE_BASIS_FORWARD
    assert normalize_price_basis("hfq") == "back_adjusted"
    assert normalize_price_basis("something_weird") == "mixed_or_unknown"


def test_price_basis_labels_expose_option_c_fields() -> None:
    labels = price_basis_labels("raw", data_root=None)
    assert set(labels) >= {"price_basis", "adjustment_basis", "adjustment_warnings"}
    assert labels["price_basis"] == PRICE_BASIS_RAW
    assert labels["price_basis_label"] == "原始日线价格"
    assert isinstance(labels["adjustment_basis"], dict)
    assert isinstance(labels["adjustment_warnings"], list)


def test_forward_request_without_forward_dir_warns_fallback_to_raw(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_daily_csv(data_root / "daily" / "000001.csv", "000001")
    # No data/daily/forward_adjusted -> forward request must fall back to raw.
    labels = price_basis_labels("forward", data_root=data_root)
    assert labels["price_basis"] == PRICE_BASIS_RAW
    assert FORWARD_FALLBACK_WARNING in labels["adjustment_warnings"]
    basis = labels["adjustment_basis"]
    assert basis["requested"] == PRICE_BASIS_FORWARD
    assert basis["effective"] == PRICE_BASIS_RAW
    assert basis["forward_fallback_to_raw"] is True


def test_forward_request_with_forward_dir_is_not_fallback(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_daily_csv(data_root / "daily" / "forward_adjusted" / "000001.csv", "000001")
    labels = price_basis_labels("forward", data_root=data_root)
    assert labels["price_basis"] == PRICE_BASIS_FORWARD
    assert FORWARD_FALLBACK_WARNING not in labels["adjustment_warnings"]


def test_divergent_alias_emits_visible_warning(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_daily_csv(data_root / "daily" / "000001.csv", "000001")
    _canonical_and_divergent_alias(data_root / "metadata")
    labels = price_basis_labels("raw", data_root=data_root)
    assert DIVERGENT_ALIAS_WARNING in labels["adjustment_warnings"]
    basis = labels["adjustment_basis"]
    assert basis["alias_factor_present"] is True
    assert basis["alias_factor_divergent"] is True
    assert basis["adjustment_factor_file_set_status"] == "canonical_with_divergent_alias"


def test_alias_is_never_used_as_fallback_data_source(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _canonical_and_divergent_alias(data_root / "metadata")
    basis = build_adjustment_basis("forward", data_root=data_root)
    # The alias may be present + divergent, but is only ever audit material.
    assert basis["alias_factor_used_as_fallback"] is False
    assert basis["canonical_factor_file"].endswith("adjustment_factors.csv")
    assert basis["alias_factor_file"].endswith("adjustment_factor.csv")


def test_helper_does_not_write_to_data_daily(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    daily_csv = data_root / "daily" / "000001.csv"
    _write_daily_csv(daily_csv, "000001")
    _canonical_and_divergent_alias(data_root / "metadata")
    before = daily_csv.read_bytes()
    daily_listing_before = sorted(p.name for p in (data_root / "daily").glob("*"))
    # Exercise every public entry point.
    price_basis_labels("forward", data_root=data_root)
    price_basis.basis_warnings("raw", data_root=data_root)
    price_basis.detect_divergent_adjustment_factor_alias(data_root=data_root)
    build_adjustment_basis("back", data_root=data_root)
    assert daily_csv.read_bytes() == before
    assert sorted(p.name for p in (data_root / "daily").glob("*")) == daily_listing_before


def test_combined_score_backtest_payload_carries_option_c_fields() -> None:
    from quantdesk.research.combined_score_backtest import run_combined_score_backtest

    payload = run_combined_score_backtest(pd.DataFrame(), pd.DataFrame())
    for key in ("price_basis", "adjustment_basis", "adjustment_warnings"):
        assert key in payload, key
    assert payload["price_basis"] == PRICE_BASIS_RAW


def test_sector_rotation_payload_carries_option_c_fields() -> None:
    from quantdesk.research.sector_rotation_engine import run_sector_rotation

    payload = run_sector_rotation(pd.DataFrame())
    for key in ("price_basis", "adjustment_basis", "adjustment_warnings"):
        assert key in payload, key


def test_factor_panel_payload_carries_option_c_fields(tmp_path: Path) -> None:
    from quantdesk.research.factor_panel_builder import build_factor_panel

    # Point at an empty dir to hit the failure payload path; fields must still exist.
    empty_dir = tmp_path / "daily"
    empty_dir.mkdir(parents=True)
    payload, _panel = build_factor_panel(empty_dir)
    for key in ("price_basis", "adjustment_basis", "adjustment_warnings"):
        assert key in payload, key


def test_backtest_assumptions_carry_option_c_fields() -> None:
    from quantdesk.backtest_assumptions import default_backtest_assumptions

    assumptions = default_backtest_assumptions()
    for key in ("price_basis", "adjustment_basis", "adjustment_warnings"):
        assert key in assumptions, key
    assert assumptions["price_basis"] == PRICE_BASIS_RAW
