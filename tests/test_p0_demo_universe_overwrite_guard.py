"""P0 guard: a demo data-bootstrap must never clobber a real on-disk universe.

Regression lock for the 2026-07-02 data accident where `data-bootstrap`
overwrote the live low-price universe (and, downstream, the daily feature
table) with a single demo row, and the shrink then flowed silently into
strategy recommendations. See the project P0 rule: stale/shrunk data must warn
loudly, never be treated as real.
"""

from __future__ import annotations

import pandas as pd

from quantdesk import data_bootstrap
from quantdesk.research import strategy_recommender


def _write_universe(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def test_demo_bootstrap_refuses_to_overwrite_real_universe(tmp_path, monkeypatch):
    target = tmp_path / "low_price_learning_symbols.csv"
    # Simulate a real, larger production universe already on disk.
    real_rows = [
        {"symbol": f"{600000 + i}", "name": f"stock{i}", "source": "akshare_daily"}
        for i in range(50)
    ]
    _write_universe(target, real_rows)
    monkeypatch.setattr(data_bootstrap, "low_price_universe_path", lambda: target)

    payload = data_bootstrap.build_data_bootstrap(network=False, write=False)

    # Guard fired: the real file is untouched and a loud warning is present.
    assert payload["created_universe"] is False
    assert any("refused_to_overwrite_real_universe" in str(w) for w in payload["warnings"])
    frame = pd.read_csv(target, encoding="utf-8-sig")
    assert len(frame) == 50  # unchanged


def test_demo_bootstrap_refuses_to_overwrite_real_universe_with_bad_csv_row(tmp_path, monkeypatch):
    target = tmp_path / "low_price_learning_symbols.csv"
    real_rows = [
        {"symbol": f"{600000 + i}", "name": f"stock{i}", "source": "akshare_daily"}
        for i in range(50)
    ]
    _write_universe(target, real_rows)
    with target.open("a", encoding="utf-8-sig", newline="") as f:
        f.write("bad,row,with,too,many,columns,for,this,universe,file\n")
    monkeypatch.setattr(data_bootstrap, "low_price_universe_path", lambda: target)

    payload = data_bootstrap.build_data_bootstrap(symbols=["600000"], network=False, write=False)

    assert payload["created_universe"] is False
    assert any("refused_to_overwrite_real_universe" in str(w) for w in payload["warnings"])
    assert "stock49" in target.read_text(encoding="utf-8-sig")


def test_demo_bootstrap_reseeds_when_only_demo_rows_present(tmp_path, monkeypatch):
    target = tmp_path / "low_price_learning_symbols.csv"
    # A prior demo file (small, demo provenance) is safe to re-seed.
    _write_universe(target, [{"symbol": "600000", "name": "x", "source": "data_bootstrap_demo"}])
    monkeypatch.setattr(data_bootstrap, "low_price_universe_path", lambda: target)

    payload = data_bootstrap.build_data_bootstrap(network=False, write=False)

    assert not any("refused_to_overwrite_real_universe" in str(w) for w in payload["warnings"])


def test_demo_bootstrap_seeds_when_no_file(tmp_path, monkeypatch):
    target = tmp_path / "low_price_learning_symbols.csv"
    monkeypatch.setattr(data_bootstrap, "low_price_universe_path", lambda: target)

    payload = data_bootstrap.build_data_bootstrap(network=False, write=False)

    assert payload["created_universe"] is True
    assert target.exists()


def test_strategy_recommender_warns_on_shrunk_feature_table(tmp_path, monkeypatch):
    # Point the recommender's PROJECT_ROOT-derived feature CSV at a shrunk file
    # by monkeypatching the CSV loader to return a single row.
    monkeypatch.setattr(
        strategy_recommender,
        "_load_csv_candidates",
        lambda path: [{"symbol": "600000", "close": "10.0"}],
    )
    inputs, warnings = strategy_recommender.load_strategy_recommender_inputs({})
    assert any("daily_features_too_few_rows" in str(w) for w in warnings)


def test_strategy_recommender_quiet_on_full_feature_table(tmp_path, monkeypatch):
    monkeypatch.setattr(
        strategy_recommender,
        "_load_csv_candidates",
        lambda path: [{"symbol": f"{600000 + i}", "close": "10.0"} for i in range(200)],
    )
    inputs, warnings = strategy_recommender.load_strategy_recommender_inputs({})
    assert not any("daily_features_too_few_rows" in str(w) for w in warnings)
