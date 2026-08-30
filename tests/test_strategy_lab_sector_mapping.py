from __future__ import annotations

from quantdesk.strategy_lab.daily_research_signal_engine import combine_daily_signals
from quantdesk.strategy_lab.rolling_strategy_backtest import backtest_summary
from quantdesk.strategy_lab.sector_industry_mapping import sector_mapping_quality
from quantdesk.strategy_lab.sector_strategy import generate_sector_rotation
from quantdesk.strategy_lab.strategy_family_registry import strategy_family_registry


def test_strategy_lab_tail_window_outputs_are_explicit_proxy_not_backtest_ready() -> None:
    summary = backtest_summary(
        "long_term_family",
        [
            {
                "annualized_return": 0.12,
                "sharpe": 0.8,
                "max_drawdown": -0.1,
                "win_rate": 0.55,
                "turnover": 1.0,
                "amount_proxy": 1000.0,
            }
        ],
    )
    families = strategy_family_registry()["families"]

    assert summary["evidence_kind"] == "tail_window_proxy"
    assert summary["backtest_ready"] is False
    assert summary["formal_performance_evidence"] is False
    assert all(row["evidence_kind"] == "tail_window_proxy" for row in families)
    assert all(row["backtest_ready"] is False for row in families)


def test_sector_rotation_prefers_theme_map_over_symbol_prefix() -> None:
    metrics = [
        {"symbol": "600001", "name": "Alpha", "return_20d": 0.10, "volatility": 0.20},
        {"symbol": "000002", "name": "Beta", "return_20d": 0.05, "volatility": 0.10},
    ]
    theme_map = {
        "600001": {"sector_name": "Bank", "primary_theme": "Banking", "source": "test"},
        "000002": {"sector_name": "Semiconductor", "primary_theme": "Chip", "source": "test"},
    }

    summary, rows = generate_sector_rotation(metrics, theme_map=theme_map)

    assert summary["status"] == "completed_with_theme_mapping"
    assert summary["mapped_stock_count"] == 2
    assert summary["mapping_coverage"] == 1.0
    assert {row["sector_name"] for row in rows} == {"Bank", "Semiconductor"}
    assert {row["sector_mapping_source"] for row in rows} == {"stock_theme_map"}
    assert all(row["risk_flags"] == "mapped_theme_or_industry" for row in rows)
    assert all("target_price" not in row and "recommended_position" not in row for row in rows)


def test_sector_rotation_consumes_classic_sector_rotation_for_ordering() -> None:
    metrics = [
        {"symbol": "600001", "name": "Alpha", "return_20d": 0.01, "volatility": 0.10},
        {"symbol": "000002", "name": "Beta", "return_20d": 0.50, "volatility": 0.10},
    ]
    theme_map = {
        "600001": {"sector_name": "Bank", "primary_theme": "Banking", "source": "test"},
        "000002": {"sector_name": "Semiconductor", "primary_theme": "Chip", "source": "test"},
    }
    classic = {
        "Bank": {"sector_name": "Bank", "current_rank": 1, "heat_score": 0.90, "sector_status": "leading"},
        "Semiconductor": {"sector_name": "Semiconductor", "current_rank": 2, "heat_score": 0.10, "sector_status": "neutral"},
    }

    summary, rows = generate_sector_rotation(metrics, theme_map=theme_map, classic_sector_rotation=classic)

    assert summary["classic_sector_rotation"]["used"] is True
    assert summary["top_sectors"][0]["sector_name"] == "Bank"
    assert summary["top_sectors"][0]["sector_score_source"] == "classic_sector_rotation_engine"
    assert rows[0]["sector_name"] == "Bank"
    assert rows[0]["sector_score_source"] == "classic_sector_rotation_engine"
    assert rows[0]["classic_sector_rank"] == 1
    assert rows[0]["research_reason"] == "classic_sector_rotation_heat"


def test_sector_rotation_ignores_synthetic_classic_sector_rotation_file(tmp_path) -> None:
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    (result_dir / "sector_rotation_latest.json").write_text(
        '{"data_source":"synthetic_test_data","sector_rankings":[{"sector_name":"Bank","heat_score":99}]}',
        encoding="utf-8",
    )
    metrics = [
        {"symbol": "600001", "name": "Alpha", "return_20d": 0.01, "volatility": 0.10},
        {"symbol": "000002", "name": "Beta", "return_20d": 0.50, "volatility": 0.10},
    ]
    theme_map = {
        "600001": {"sector_name": "Bank", "primary_theme": "Banking", "source": "test"},
        "000002": {"sector_name": "Semiconductor", "primary_theme": "Chip", "source": "test"},
    }

    summary, _ = generate_sector_rotation(metrics, root=tmp_path, theme_map=theme_map)

    assert summary["classic_sector_rotation"]["status"] == "synthetic_ignored"
    assert summary["classic_sector_rotation"]["used"] is False
    assert summary["top_sectors"][0]["sector_name"] == "Semiconductor"
    assert summary["top_sectors"][0]["sector_score_source"] == "strategy_lab_metric_proxy"


def test_sector_rotation_degrades_visibly_when_theme_map_missing() -> None:
    metrics = [
        {"symbol": "600001", "return_20d": 0.10, "volatility": 0.20},
        {"symbol": "000002", "return_20d": 0.05, "volatility": 0.10},
    ]

    summary, rows = generate_sector_rotation(metrics, theme_map={})

    assert summary["status"] == "completed_with_proxy_mapping"
    assert summary["mapping_coverage"] == 0.0
    assert {row["sector_mapping_source"] for row in rows} == {"symbol_prefix_proxy"}
    assert all(row["risk_flags"] == "sector_proxy_mapping_warning" for row in rows)


def test_sector_mapping_quality_counts_theme_mapped_rows() -> None:
    payload, unmapped = sector_mapping_quality(
        [
            {"stock_symbol": "600001", "sector_name": "Bank", "sector_mapping_source": "stock_theme_map"},
            {"stock_symbol": "000002", "sector_name": "sz_main_board_proxy", "risk_flags": "sector_proxy_mapping_warning"},
        ]
    )

    assert payload["mapped_rows"] == 1
    assert payload["total_stock_rows"] == 2
    assert payload["mapping_coverage"] == 0.5
    assert payload["status"] == "completed_with_mapping_gaps"
    assert unmapped == [{"symbol": "000002", "reason": "formal sector map missing; proxy sector may be Unknown"}]


def test_sector_mapping_quality_does_not_count_unknown_concept_as_formal_mapping() -> None:
    payload, unmapped = sector_mapping_quality(
        [
            {"stock_symbol": "000725", "sector_name": "未知概念", "sector_mapping_source": "stock_theme_map"},
        ]
    )

    assert payload["mapped_rows"] == 0
    assert payload["mapping_coverage"] == 0.0
    assert payload["status"] == "completed_with_mapping_gaps"
    assert unmapped == [{"symbol": "000725", "reason": "formal sector map missing; proxy sector may be Unknown"}]


def test_daily_signal_stock_candidates_carry_formal_theme_mapping(monkeypatch, tmp_path) -> None:
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True)
    (universe / "stock_theme_map.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
        "600001,Alpha,Finance,Bank,Banking;Dividend,Large Cap,Banking,test,2026-06-23\n",
        encoding="utf-8-sig",
    )
    monkeypatch.chdir(tmp_path)

    payload, stock_rows, _, _ = combine_daily_signals(
        freshness={"status": "data_fresh"},
        leaderboard_rows=[],
        low_rows=[
            {
                "symbol": "600001",
                "name": "Alpha",
                "signal_date": "2026-06-23",
                "strategy_id": "low_price_breakout",
                "recent_return": 0.12,
                "risk_flags": "research_only",
                "trigger_reason": "sample",
                "research_signal_label": "observe",
            }
        ],
        long_rows=[],
        limit_rows=[],
        sector_rows=[],
    )

    assert payload["stock_sector_mapping"]["status"] == "complete"
    assert payload["stock_sector_mapping"]["mapping_coverage"] == 1.0
    assert stock_rows[0]["sector_name"] == "Bank"
    assert stock_rows[0]["industry"] == "Bank"
    assert stock_rows[0]["primary_theme"] == "Banking"
    assert stock_rows[0]["concept_tags"] == "Banking;Dividend"
    assert stock_rows[0]["sector_mapping_source"] == "stock_theme_map"


def test_daily_signal_stock_candidates_show_proxy_when_theme_mapping_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    payload, stock_rows, _, _ = combine_daily_signals(
        freshness={"status": "data_fresh"},
        leaderboard_rows=[],
        low_rows=[
            {
                "symbol": "300001",
                "name": "Beta",
                "signal_date": "2026-06-23",
                "strategy_id": "low_price_breakout",
                "recent_return": 0.08,
                "risk_flags": "research_only",
                "trigger_reason": "sample",
                "research_signal_label": "observe",
            }
        ],
        long_rows=[],
        limit_rows=[],
        sector_rows=[],
    )

    assert payload["stock_sector_mapping"]["status"] == "missing"
    assert payload["stock_sector_mapping"]["mapped_stock_rows"] == 0
    assert stock_rows[0]["sector_name"] == "growth_board_proxy"
    assert stock_rows[0]["sector_mapping_source"] == "symbol_prefix_proxy"
    assert stock_rows[0]["sector_mapping_note"] == "formal theme map missing; using visible exchange-board proxy"


def test_daily_signal_stock_candidates_do_not_treat_unknown_concept_as_mapping(monkeypatch, tmp_path) -> None:
    universe = tmp_path / "data" / "universe"
    universe.mkdir(parents=True)
    (universe / "stock_theme_map.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
        "000725,Beta,,,,未知概念,,test,2026-06-23\n",
        encoding="utf-8-sig",
    )
    monkeypatch.chdir(tmp_path)

    payload, stock_rows, _, _ = combine_daily_signals(
        freshness={"status": "data_fresh"},
        leaderboard_rows=[],
        low_rows=[
            {
                "symbol": "000725",
                "name": "Beta",
                "signal_date": "2026-06-23",
                "strategy_id": "low_price_breakout",
                "recent_return": 0.08,
                "risk_flags": "research_only",
                "trigger_reason": "sample",
                "research_signal_label": "observe",
            }
        ],
        long_rows=[],
        limit_rows=[],
        sector_rows=[],
    )

    assert payload["stock_sector_mapping"]["status"] == "missing"
    assert stock_rows[0]["sector_name"] == "sz_main_board_proxy"
    assert stock_rows[0]["sector_mapping_source"] == "symbol_prefix_proxy"
