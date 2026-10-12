from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantdesk import (
    daily_price_update_engine,
    technical_indicators,
    theme_map_import_draft,
    theme_map_providers,
    theme_map_updater,
)
from quantdesk.daily_price_providers import merge_daily_frames
from quantdesk.explainers import stock_explainer
from scripts import quant_api_server as api_server
from scripts.quant_api_server import app
from scripts.server.routes import backtest as backtest_routes

BAD_UI_TOKENS = ("NaN", "Infinity", "undefined", "TODO", "功能待实现", "da_ban", "long_term", "波动很小")


def _json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _assert_clean_payload(payload: object) -> None:
    text = _json_text(payload)
    for token in BAD_UI_TOKENS:
        assert token not in text
    assert "null" not in text.lower()


def _install_self_contained_candidate_fixture(monkeypatch) -> list[dict[str, object]]:
    dates = pd.bdate_range("2026-01-05", periods=90)
    close = pd.Series([10.0 + index * 0.025 + (index % 7) * 0.01 for index in range(len(dates))])
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.03,
            "high": close + 0.08,
            "low": close - 0.09,
            "close": close,
            "volume": [1_000_000 + index * 1_000 for index in range(len(dates))],
            "amount": [10_000_000 + index * 10_000 for index in range(len(dates))],
        }
    )
    symbols = ("990001", "990002", "990003", "990004", "990005", "990006")
    names = {symbol: f"fixture-{index + 1}" for index, symbol in enumerate(symbols)}
    monkeypatch.setattr(api_server, "load_stock_data", lambda _symbol: frame.copy())
    monkeypatch.setattr(
        api_server,
        "candidate_name",
        lambda symbol, row=None: str(names.get(symbol) or (row.get("stock_name") if row is not None else "") or symbol),
    )
    monkeypatch.setattr(
        api_server,
        "load_candidate_ledger",
        lambda: pd.DataFrame(
            {
                "ts_code": list(symbols),
                "stock_name": [names[symbol] for symbol in symbols],
                "entry_date": [pd.Timestamp("2026-03-02")] * len(symbols),
                "strategy_type": ["board", "long", "long", "long", "long", "long"],
            }
        ),
    )
    candidates: list[dict[str, object]] = []
    for index, symbol in enumerate(symbols):
        row = pd.Series(
            {
                "ts_code": symbol,
                "stock_name": names[symbol],
                "entry_date": "2026-03-02",
                "strategy_type": "board" if index == 0 else "long",
            }
        )
        candidate = api_server.build_candidate(symbol, row)
        candidate.update(
            {
                "is_today_recommendation": index < 3,
                "candidate_origin_label": "今日新候选" if index < 3 else "历史观察",
                "strategy_lane": "打板复核" if index == 0 else "长期-波段复核",
                "detail_lane": "board" if index == 0 else "long",
                "reason_cn": "本地完整日线用于测试。趋势证据可读取。量价字段可读取。后续只做纸面验证。",
                "risk_level": "中",
                "tracking": {
                    **(candidate.get("tracking") or {}),
                    "status_label": "观察中",
                    "expected_path": "后续交易日继续验证研究假设。",
                    "elapsed_trading_days": 3,
                },
                "pool_action": {"label": "继续验证", "source": "auto", "status": "active"},
                "data_quality": {"status": "数据完整", "latest_date": "2026-05-08", "tone": "ok"},
            }
        )
        candidates.append(candidate)
    monkeypatch.setattr(api_server, "candidate_rows", lambda limit=18: [dict(item) for item in candidates[:limit]])
    return candidates


def test_api_json_provider_never_emits_nan_or_infinity_tokens() -> None:
    with app.app_context():
        response = app.json.response(
            {
                "python_nan": float("nan"),
                "python_inf": float("inf"),
                "numpy_nan": np.float64(np.nan),
                "nested": [{"value": -float("inf")}, pd.NA],
            }
        )

    text = response.get_data(as_text=True)
    assert "NaN" not in text
    assert "Infinity" not in text
    parsed = json.loads(text)
    assert parsed["python_nan"] == "数据缺失"
    assert parsed["python_inf"] == "数据缺失"
    assert parsed["nested"][0]["value"] == "数据缺失"


def test_load_stock_data_filters_invalid_ohlc_rows(monkeypatch, tmp_path, caplog) -> None:
    rows = [
        {"date": "2026-06-01", "symbol": "000100", "open": 4.10, "high": 4.20, "low": 4.05, "close": 4.15, "volume": 1000, "amount": 4150},
        {"date": "2026-06-02", "symbol": "000100", "open": 4.20, "high": 4.30, "low": 4.10, "close": 9135259.0, "volume": 3955173622, "amount": 3.32},
        {"date": "2026-06-03", "symbol": "000100", "open": 4.18, "high": 4.35, "low": 4.12, "close": 4.28, "volume": 1100, "amount": 4708},
    ]
    pd.DataFrame(rows).to_csv(tmp_path / "000100.csv", index=False)
    monkeypatch.setattr(api_server, "DAILY_DIR", tmp_path)

    with caplog.at_level(logging.WARNING, logger="quant_api"):
        frame = api_server.load_stock_data("000100")

    assert len(frame) == 2
    assert frame["close"].max() < 10
    assert frame.attrs["bad_ohlc_rows_filtered"] == 1
    assert "filtered 1 invalid OHLC rows" in caplog.text

    quality = api_server.stock_data_quality(frame, "000100")
    assert "异常 OHLC" in "；".join(quality["missing_reasons"])


def test_load_stock_data_records_csv_bad_line_warning(monkeypatch, tmp_path, caplog) -> None:
    (tmp_path / "000101.csv").write_text(
        "\ufeffdate,symbol,open,high,low,close,volume,amount\n"
        "2026-06-01,000101,4.10,4.20,4.05,4.15,1000,4150\n"
        "2026-06-02,000101,4.20,4.30,4.10,4.18,1000,4180,extra-field\n"
        "2026-06-03,000101,4.18,4.35,4.12,4.28,1100,4708\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "DAILY_DIR", tmp_path)

    with caplog.at_level(logging.WARNING, logger="quant_api"):
        frame = api_server.load_stock_data("000101")

    assert frame["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-06-01", "2026-06-03"]
    assert "csv_bad_lines_skipped" in caplog.text


def test_api_cors_is_restricted_to_local_ui_origins() -> None:
    client = app.test_client()

    allowed = client.get("/api/daily/candidates", headers={"Origin": "http://127.0.0.1:8000"})
    blocked = client.get("/api/daily/candidates", headers={"Origin": "https://example.com"})

    assert allowed.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8000"
    assert blocked.headers.get("Access-Control-Allow-Origin") is None


def test_spot_snapshot_read_failure_is_logged(monkeypatch, tmp_path, caplog) -> None:
    cache_path = tmp_path / "spot_snapshot_latest.csv"
    cache_path.write_text("symbol,name\n000001,平安银行\n", encoding="utf-8")
    monkeypatch.setitem(api_server.load_spot_snapshot_frame.__globals__, "SPOT_SNAPSHOT_CACHE", cache_path)

    def fail_read_csv(*_args, **_kwargs):
        raise ValueError("broken csv")

    monkeypatch.setitem(api_server.load_spot_snapshot_frame.__globals__, "read_csv_frame", fail_read_csv)

    with caplog.at_level(logging.WARNING, logger="quant_api"):
        frame = api_server.load_spot_snapshot_frame()

    assert frame.empty
    assert "spot_snapshot_cache_read_failed" in caplog.text


def test_spot_snapshot_skips_bad_rows_with_frame_warning(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "spot_snapshot_latest.csv"
    cache_path.write_text(
        "symbol,name,price,pct_chg,amount\n"
        "000001,平安银行,10.1,1.2,100000000\n"
        "bad-row,坏行,10,0.1,100,extra-field\n"
        "000002,万科A,8.5,-0.5,200000000\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(api_server.load_spot_snapshot_frame.__globals__, "SPOT_SNAPSHOT_CACHE", cache_path)

    frame = api_server.load_spot_snapshot_frame()

    assert frame["symbol"].tolist() == ["000001", "000002"]
    assert any("csv_bad_lines_skipped" in item for item in frame.attrs["warnings"])


def test_data_update_resolve_symbol_failure_is_logged(monkeypatch, caplog) -> None:
    from quantdesk import data_update_orchestrator as duo

    def fail_resolve_symbols(**_kwargs):
        raise RuntimeError("resolver broke")

    monkeypatch.setattr(duo, "_engine_resolve_symbols", fail_resolve_symbols)

    with caplog.at_level(logging.WARNING, logger=duo.__name__):
        rows = duo.resolve_symbols("000001", max_symbols=1)

    assert rows == [{"symbol": "000001", "name": "000001", "included": "explicit"}]
    assert "engine_resolve_symbols_failed" in caplog.text


def _sample_indicators() -> dict[str, float]:
    return {
        "close": 10.5,
        "ma5": 10.1,
        "ma20": 9.8,
        "return_5d": 0.081,
        "return_20d": 0.126,
        "volatility_20d": 3.2,
        "dif": 0.529,
        "dea": 0.411,
        "macd": 0.236,
        "k": 94.9,
        "d": 82.1,
        "j": 120.5,
        "rsi6": 74.2,
        "rsi12": 66.8,
        "vol_ma5": 2000000,
        "vol_ma20": 1300000,
    }


def test_latest_indicator_values_reads_list_outputs() -> None:
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=40),
            "open": [10 + i * 0.1 for i in range(40)],
            "high": [10.2 + i * 0.1 for i in range(40)],
            "low": [9.8 + i * 0.1 for i in range(40)],
            "close": [10 + i * 0.1 for i in range(40)],
            "volume": [1000000 + i * 1000 for i in range(40)],
        }
    )
    latest = technical_indicators.get_latest_indicator_values(df)
    for key in ("dif", "dea", "macd", "k", "d", "j", "rsi6", "rsi12", "volatility_20d"):
        assert key in latest
        assert math.isfinite(float(latest[key]))


def test_explainer_uses_real_values_and_educational_judgment() -> None:
    indicators = _sample_indicators()
    reason = stock_explainer.generate_selection_reason("000001", indicators, 60)
    detail = stock_explainer.generate_detailed_explanation("000001", indicators, 60)
    indicator_text = stock_explainer.generate_indicator_explanation(indicators)
    risk = stock_explainer.generate_risk_warning(indicators, 60)

    combined = reason + detail + indicator_text + risk
    _assert_clean_payload(combined)
    assert "8.1%" in reason
    assert "20日波动率为3.20%" in reason
    assert "DIF 为0.529" in reason
    assert "K 值为94.9" in reason
    assert reason.count("。") >= 4
    assert "超过80属于超买区" in indicator_text
    assert "5%观察线" in risk
    assert "约为" not in risk
    assert "止损价" not in risk
    assert "不接券商、不下真实订单" in risk


def test_daily_research_api_payloads_are_complete_and_clean(monkeypatch) -> None:
    _install_self_contained_candidate_fixture(monkeypatch)
    client = app.test_client()
    overview = client.get("/api/daily/overview").get_json()
    candidates = client.get("/api/daily/candidates").get_json()

    _assert_clean_payload(overview)
    _assert_clean_payload(candidates)
    assert overview["hs300"]["label"] == "沪深300"
    assert overview["sh_index"]["label"] == "上证指数"
    assert len(overview["sector_top_gainers"]) <= 5
    assert len(overview["sector_top_losers"]) <= 5
    assert "市场" in overview["market_sentiment"]
    assert "市场全景读取本地指数" in overview["update_hint"]
    assert "本地数据缺失" not in overview["update_hint"]
    assert 5 <= len(candidates["candidates"]) <= 10
    assert 1 <= len(candidates["daily_focus"]) <= 4
    assert "candidate_generation" in candidates
    assert candidates["candidate_generation"]["freshness_status"] in {"current", "stale", "missing"}
    assert candidates["candidate_generation"]["freshness_label"]
    assert candidates["candidate_generation"]["freshness_message"]
    assert "today_recommendations" in candidates
    assert "observation_candidates" in candidates
    assert "今日新候选" in candidates["message"]
    assert "今日新推荐" not in candidates["message"]
    assert any(group["title"] == "今日新候选" for group in candidates["candidate_groups"])
    assert {group["title"] for group in candidates["candidate_groups"]} >= {
        "今日新候选",
        "长期 - 波段复核",
        "长期 - 只观察",
        "打板观察",
        "待移出 / 降级复盘",
        "手动只观察",
    }
    pool_policy = candidates["candidate_pool_policy"]
    assert pool_policy["title"] == "降级复盘机制"
    assert pool_policy["today_count"] == len(candidates["today_recommendations"])
    assert pool_policy["history_count"] == len(candidates["observation_candidates"])
    assert pool_policy["exit_review_count"] >= 0
    assert "不会自动删除" in pool_policy["summary"]
    assert "专项回测" in pool_policy["backtest_note"]
    assert any("自动分层" in step for step in pool_policy["steps"])
    assert candidates["data_gap_summary"]["message"]
    summary = candidates["quant_daily_summary"]
    assert summary["title"] == "Quant 日总结"
    assert "candidate_generation" in summary
    assert len(summary["items"]) == len(candidates["all_candidates"])
    assert "不是价格预测" in summary["report_note"]
    assert "Not Position Advice" in summary["boundary"]
    assert summary["backtest_ready_count"] >= 0
    assert {"符合入选假设", "等待更多交易日", "偏离入选假设", "数据不足"}.issubset(summary["fit_counts"])
    limit_review = summary["limit_up_review"]
    assert "report_line" in limit_review
    assert "涨停复盘" in limit_review["boundary"]
    assert "Quant" in limit_review["report_line"] or limit_review["status"] == "missing"
    assert "真实可成交" in limit_review["boundary"]
    first_summary = summary["items"][0]
    assert first_summary["fit_label"] in {"符合入选假设", "等待更多交易日", "偏离入选假设", "数据不足"}
    assert first_summary["backtest_readiness"]["label"] in {"可进入回测队列", "可做轻量复盘", "暂不适合回测"}
    assert "策略预期" not in first_summary["reason_analysis"]
    assert "目标价" not in _json_text(summary)
    assert "买入" not in _json_text(summary)
    assert "卖出" not in _json_text(summary)
    assert "仓位" not in _json_text(summary)

    first = candidates["candidates"][0]
    for key in ("symbol", "name", "current_price", "reason_cn", "macd_val", "kdj_val", "rsi_val", "risk_level"):
        assert first[key] != "数据缺失"
    assert first["risk_level"] in {"低", "中", "高"}
    assert first["reason_cn"].count("。") >= 4
    assert first["strategy_lane"] in {"长期-波段复核", "长期-只观察", "打板复核", "普通观察", "待移出复盘", "手动只观察", "已移出活跃池"}
    assert first["tracking"]["status_label"] in {"阶段符合观察假设", "观察中", "尚未确认", "偏离观察假设", "数据不足"}
    assert first["pool_action"]["label"]
    assert first["pool_action"]["source"] in {"auto", "manual"}
    assert first["data_quality"]["status"] in {"数据完整", "成交额缺失", "字段缺失", "日线滞后", "缺少日线"}
    symbols = [row["symbol"] for row in candidates["all_candidates"]]
    assert len(symbols) == len(set(symbols))

    detail = client.get(f"/api/stock/{first['symbol']}/detail").get_json()
    _assert_clean_payload(detail)
    assert detail["basic_info"]["name"] == first["name"]
    assert detail["strategy_review"]["strategy_lane"]
    assert detail["strategy_review"]["pool_action"]["label"]
    assert detail["strategy_review"]["detail_lane"] in {"long", "board", "pool"}
    assert detail["strategy_review"]["tracking"]["expected_path"]
    assert "技术指标" not in detail["explanation"]
    assert "MACD" in detail["indicators_exp"]
    assert "KDJ" in detail["indicators_exp"]
    assert "RSI" in detail["indicators_exp"]
    assert len(detail["chart_data"]["kline_data"]) >= 60
    assert detail["chart_data"]["select_reason"]
    assert "news_summary" in detail
    assert "industry_comparison" in detail
    assert "tracking_vs_benchmark" in detail
    assert "paper_simulation" in detail
    assert "in_paper_pool" in detail["paper_simulation"]
    markers = detail["chart_data"]["markers"]
    marker_panels = {marker["panel"] for marker in markers}
    assert {"price", "volume", "macd", "kdj", "rsi"}.issubset(marker_panels)
    assert any(marker["kind"] == "next_review" for marker in markers)
    assert any(marker["kind"] == "expected_review" for marker in markers)
    assert "目标价" not in _json_text(markers)
    assert "买入" not in _json_text(markers)
    assert "卖出" not in _json_text(markers)
    ref = detail["research_reference"]
    assert "reference_1d_pct" in ref["price_band"]
    assert "reference_1d" in ref["price_band"]
    assert "reference_5d" in ref["price_band"]
    assert "scenario_projection" in ref
    assert "情景推演" in _json_text(ref)
    assert "买入" not in _json_text(ref)
    assert "卖出" not in _json_text(ref)


def test_candidate_pool_review_state_auto_and_manual(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "TRACKING_DIR", tmp_path)
    candidate = {
        "symbol": "000711",
        "select_date": "2026-06-04",
        "detail_lane": "board",
        "group_key": "board",
        "strategy_lane": "打板复核",
        "tracking": {"status_label": "偏离观察假设", "status_tone": "warn"},
        "data_quality": {"tone": "ok"},
    }

    auto_state = api_server.apply_candidate_review_state(dict(candidate), {})

    assert auto_state["group_key"] == "exit_review"
    assert auto_state["strategy_lane"] == "待移出复盘"
    assert auto_state["pool_action"]["label"] == "自动转待移出"
    assert "打板" in auto_state["pool_action"]["reason"]

    saved = api_server.save_candidate_review_action("000711", "2026-06-04", "board", "observe", "测试转只观察")
    assert saved["ok"] is True

    manual_state = api_server.apply_candidate_review_state(dict(candidate))
    assert manual_state["group_key"] == "manual_observe"
    assert manual_state["strategy_lane"] == "手动只观察"
    assert manual_state["pool_action"]["source"] == "manual"

    reviewed = api_server.save_candidate_review_action("000711", "2026-06-04", "board", "review", "测试进入降级复盘")
    assert reviewed["ok"] is True
    review_state = api_server.apply_candidate_review_state(dict(candidate))
    assert review_state["group_key"] == "exit_review"
    assert review_state["strategy_lane"] == "手动降级复盘"
    assert review_state["pool_action"]["status"] == "manual_exit_review"
    assert review_state["pool_action"]["label"] == "手动降级复盘"

    archived = api_server.save_candidate_review_action("000711", "2026-06-04", "board", "hide", "测试归档忽略")
    assert archived["ok"] is True
    archived_state = api_server.apply_candidate_review_state(dict(candidate))
    assert archived_state["group_key"] == "manual_archived"
    assert archived_state["strategy_lane"] == "归档/忽略"
    assert archived_state["pool_action"]["label"] == "归档/忽略"

    cleared = api_server.save_candidate_review_action("000711", "2026-06-04", "board", "clear")
    assert cleared["ok"] is True
    assert api_server.load_candidate_review_overrides() == {}


def test_candidate_can_be_added_to_paper_simulation_pool(monkeypatch, tmp_path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    candidate = {
        "symbol": "000657",
        "name": "中钨高新",
        "select_date": "2026-06-22",
        "detail_lane": "board",
        "strategy_lane": "打板复核",
        "strategy_bucket": "board_review",
        "candidate_origin_label": "今日新候选",
        "current_price": 12.34,
        "reason_cn": "量价弹性较强，适合进入纸面跟踪复盘。",
        "tracking": {"status_label": "观察中", "elapsed_trading_days": 0},
        "data_quality": {"status": "数据完整", "latest_date": "2026-06-22"},
        "priority_rank": 1,
        "priority_label": "优先复盘",
        "priority_reason": "今日候选优先级靠前",
    }
    monkeypatch.setattr(
        api_server,
        "find_visible_candidate",
        lambda symbol, entry_date="", lane="": candidate if api_server.normalize_symbol(symbol) == "000657" else None,
    )

    client = app.test_client()
    response = client.post(
        "/api/paper-simulation/add-candidate",
        json={
            "symbol": "000657",
            "entry_date": "2026-06-22",
            "lane": "board",
            "note": "手动确认进入纸面模拟池",
            "llm_decision_status": "user_confirmed_after_llm",
            "llm_research_opinion": "LLM 研究意见：建议作为纸面样本验证打板弹性，但需要警惕量能衰减。",
            "llm_primary_action": "打板复核",
            "llm_confidence": "高",
            "llm_validation_days": 3,
            "llm_success_condition": "3 个完整交易日内量价弹性继续兑现。",
            "llm_failure_condition": "连续回落且量能衰减，进入降级复盘。",
            "llm_missing_data": "分钟线缺失，新闻线索待补。",
            "llm_review_sentence": "最值得验证的是短线弹性是否真实延续。",
        },
    )
    payload = response.get_json()

    assert response.status_code == 200
    _assert_clean_payload(payload)
    assert payload["ok"] is True
    assert payload["status"] == "added"
    assert payload["watchlist_count"] == 1
    assert payload["research_only"] is True
    assert payload["paper_only"] is True
    assert payload["no_broker"] is True
    assert "agent_question_suggestion" in payload
    assert watchlist_path.exists()

    written = json.loads(watchlist_path.read_text(encoding="utf-8"))
    assert written["watchlist_count"] == 1
    item = written["items"][0]
    assert item["symbol"] == "000657"
    assert item["source_candidate_key"] == "000657|2026-06-22|board"
    assert item["research_only"] is True
    assert item["paper_only"] is True
    assert item["no_broker"] is True
    assert item["no_real_orders"] is True
    assert item["not_position_advice"] is True
    assert item["review_status"] == "watching"
    assert item["initial_reference_price"] == 12.34
    assert item["primary_action"] == "打板复核"
    assert "今日新候选" in item["strategy_tags"]
    assert item["paper_research_contract"]["primary_action"] == "打板复核"
    assert item["validation_cycle"]["code"] == "board_1_3d"
    assert item["success_conditions"]
    assert item["failure_conditions"]
    assert item["daily_tracking_plan"]
    assert item["expected_path"]
    assert item["llm_decision_status"] == "user_confirmed_after_llm"
    assert "建议作为纸面样本验证" in item["llm_research_opinion"]
    assert item["llm_confidence"] == "高"
    assert item["llm_missing_data"] == "分钟线缺失，新闻线索待补。"
    assert item["llm_structured_decision"]["validation_days"] == 3
    assert "建议作为纸面样本验证" in item["paper_research_contract"]["llm_research_opinion"]
    assert item["paper_research_contract"]["llm_confidence"] == "高"
    assert item["paper_research_contract"]["validation_cycle"]["llm_validation_days"] == 3
    assert item["paper_research_contract"]["validation_cycle"]["review_trading_days"] == 3
    assert item["success_conditions"][0] == "3 个完整交易日内量价弹性继续兑现。"
    assert item["failure_conditions"][0] == "连续回落且量能衰减，进入降级复盘。"
    assert item["scenario_projection"]["llm_review_sentence"] == "最值得验证的是短线弹性是否真实延续。"
    assert item["expectation_fit_label"] == "等待更多样本"

    watchlist = client.get("/api/paper-simulation/watchlist").get_json()
    assert watchlist["watchlist_count"] == 1
    assert watchlist["items"][0]["symbol"] == "000657"

    performance = client.get("/api/performance/summary").get_json()
    assert performance["paper_watchlist"]["watchlist_count"] == 1
    assert performance["paper_watchlist"]["items"][0]["source_candidate_key"] == "000657|2026-06-22|board"
    assert "get_performance_summary" not in api_server.__dict__


def test_performance_summary_reads_local_metrics_after_blueprint_split(monkeypatch, tmp_path) -> None:
    import quantdesk.paper.auto_paper_portfolio as auto_paper_portfolio

    monkeypatch.setattr(api_server, "project_root", tmp_path)
    watchlist_path = tmp_path / "state" / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "items": [{"symbol": "000657", "source_candidate_key": "000657|2026-06-22|board"}],
                "paper_tracking_summary": {"updated_count": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    perf_dir = tmp_path / "data" / "tracking"
    perf_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"date": "2026-06-01", "strategy_type": "alpha", "hold_5d_ret": 0.10, "hold_20d_ret": 0.20, "hit_flag": 1},
            {"date": "2026-06-02", "strategy_type": "beta", "hold_5d_ret": -0.05, "hold_20d_ret": 0.10, "hit_flag": 0},
            {"date": "2026-06-03", "strategy_type": "alpha", "hold_5d_ret": 8.00, "hold_20d_ret": 0.30, "hit_flag": 1},
        ]
    ).to_csv(perf_dir / "candidate_performance.csv", index=False)
    monkeypatch.setattr(auto_paper_portfolio, "auto_paper_portfolio_latest", lambda: {"ok": True, "paper_only": True})

    payload = app.test_client().get("/api/performance/summary").get_json()

    assert payload["ok"] is True
    assert payload["performance_csv_exists"] is True
    assert payload["total_rows"] == 3
    assert payload["sample_count"] == 2
    assert payload["excluded_outliers"] == 1
    assert payload["overall"]["sample_count"] == 2
    assert payload["overall"]["hit_rate"] == 0.5
    assert payload["overall"]["hold_means"]["hold_5d_ret"] == 0.025
    assert {row["strategy_type"] for row in payload["by_strategy"]} == {"alpha", "beta"}
    assert payload["paper_watchlist"]["watchlist_count"] == 1
    assert payload["paper_watchlist"]["path"] == "state\\paper_watchlist.json"
    assert payload["paper_portfolio"]["paper_only"] is True
    assert "get_performance_summary" not in api_server.__dict__


def test_performance_summary_excludes_pending_hit_flags_and_reports_maturity(monkeypatch, tmp_path) -> None:
    import quantdesk.paper.auto_paper_portfolio as auto_paper_portfolio

    monkeypatch.setattr(api_server, "project_root", tmp_path)
    watchlist_path = tmp_path / "state" / "paper_watchlist.json"
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    watchlist_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    perf_dir = tmp_path / "data" / "tracking"
    perf_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"date": "2026-06-01", "hold_5d_ret": 0.10, "hold_20d_ret": 0.20, "hit_flag": 1},
            {"date": "2026-06-02", "hold_5d_ret": -0.05, "hold_20d_ret": 0.10, "hit_flag": 0},
            {"date": "2026-06-03", "hold_5d_ret": 0.02, "hold_20d_ret": None, "hit_flag": None},
            {"date": "2026-06-04", "hold_5d_ret": 0.03, "hold_20d_ret": 0.05, "hit_flag": None},
        ]
    ).to_csv(perf_dir / "candidate_performance.csv", index=False)
    monkeypatch.setattr(auto_paper_portfolio, "auto_paper_portfolio_latest", lambda: {"ok": True})

    payload = app.test_client().get("/api/performance/summary").get_json()
    metrics = payload["overall"]

    assert metrics["hit_rate"] == 0.5
    assert metrics["hit_rate_sample_count"] == 2
    assert metrics["hit_rate_pending_count"] == 2
    assert metrics["sample_count_5d"] == 4
    assert metrics["sample_count_20d"] == 3
    assert metrics["forward_return_20d_status"] == "insufficient_sample"
    assert isinstance(metrics["forward_return_20d_mean"], (float, int))
    assert payload["evidence_freshness"]["metrics_trustworthy"] is False
    assert "不可信" in payload["message_for_ui"]


def test_performance_summary_keeps_audit_warning_when_no_samples(monkeypatch, tmp_path) -> None:
    import quantdesk.paper.auto_paper_portfolio as auto_paper_portfolio

    monkeypatch.setattr(api_server, "project_root", tmp_path)
    watchlist_path = tmp_path / "state" / "paper_watchlist.json"
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    watchlist_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    monkeypatch.setattr(auto_paper_portfolio, "auto_paper_portfolio_latest", lambda: {"ok": True})

    payload = app.test_client().get("/api/performance/summary").get_json()

    assert payload["sample_count"] == 0
    assert payload["evidence_freshness"]["metrics_trustworthy"] is False
    assert "当前绩效指标不可信" in payload["message_for_ui"]
    assert "暂无绩效样本" in payload["message_for_ui"]


def test_paper_simulation_refresh_updates_local_tracking(monkeypatch, tmp_path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "watchlist_id": "paper_watchlist",
                "items": [
                    {
                        "symbol": "000657",
                        "name": "中钨高新",
                        "strategy": "打板复核",
                        "entry_date": "2026-06-20",
                        "initial_reference_price": 10.0,
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-20", "2026-06-21", "2026-06-22"]),
            "open": [10.0, 10.4, 10.8],
            "high": [10.2, 11.2, 11.1],
            "low": [9.9, 10.2, 10.4],
            "close": [10.0, 11.0, 10.5],
            "volume": [1000, 1200, 1300],
        }
    )
    monkeypatch.setattr(api_server, "load_stock_data", lambda symbol: frame if symbol == "000657" else pd.DataFrame())

    client = app.test_client()
    response = client.post("/api/paper-simulation/refresh", json={})
    payload = response.get_json()

    assert response.status_code == 200
    _assert_clean_payload(payload)
    assert payload["status"] == "refreshed"
    assert payload["watchlist_count"] == 1
    assert payload["paper_tracking_summary"]["updated_count"] == 1
    item = payload["items"][0]
    assert item["latest_reference_price"] == 10.5
    assert item["latest_reference_date"] == "2026-06-22"
    assert item["return_since_added_pct"] == 5.0
    assert item["max_gain_since_added"] == 10.0
    assert item["max_drawdown_since_added"] == 0.0
    assert item["elapsed_trading_days"] == 2
    assert item["review_status"] in {"board_following_expected", "paper_tracking"}
    assert item["expectation_fit_status"] == "fit"
    assert item["expectation_fit_label"] == "符合原始预期"
    assert item["no_broker"] is True
    assert item["no_real_orders"] is True

    written = json.loads(watchlist_path.read_text(encoding="utf-8"))
    assert written["items"][0]["return_since_added_pct"] == 5.0
    assert written["paper_tracking_summary"]["updated_count"] == 1
    assert written["paper_tracking_summary"]["expectation_fit_counts"]["符合原始预期"] == 1


def test_paper_simulation_refresh_marks_smart_stop_review_without_orders(monkeypatch, tmp_path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "watchlist_id": "paper_watchlist",
                "items": [
                    {
                        "symbol": "000657",
                        "name": "中钨高新",
                        "strategy": "打板复核",
                        "entry_date": "2026-06-01",
                        "initial_reference_price": 10.0,
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dates = pd.date_range("2026-06-01", periods=20, freq="B")
    closes = [10.0 - idx * 0.08 for idx in range(19)] + [8.7]
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.99 for value in closes],
            "close": closes,
            "volume": [1000 + idx for idx in range(20)],
        }
    )
    monkeypatch.setattr(api_server, "load_stock_data", lambda symbol: frame if symbol == "000657" else pd.DataFrame())

    client = app.test_client()
    response = client.post("/api/paper-simulation/refresh", json={})
    payload = response.get_json()

    assert response.status_code == 200
    _assert_clean_payload(payload)
    item = payload["items"][0]
    assert item["smart_stop"]["trigger_status"] == "triggered"
    assert item["paper_exit_plan"]["plan_status"] == "triggered"
    assert item["paper_exit_plan"]["paper_exit_candidate"] is True
    assert item["paper_exit_plan"]["auto_execute"] is False
    assert item["paper_exit_plan"]["execution_allowed"] is False
    assert item["paper_exit_plan"]["no_real_orders"] is True
    assert item["paper_exit_plan"]["condition_plan"]["exit_conditions"][0]["field"] == "close"
    assert payload["paper_tracking_summary"]["paper_exit_plan_count"] == 1
    assert item["review_status"] == "smart_stop_triggered_review"
    assert item["review_label"] == "风险观察线触发，待复盘"
    assert item["no_broker"] is True
    assert item["no_real_orders"] is True
    assert "真实订单" in item["smart_stop"]["message"]


def test_paper_simulation_lifecycle_action_marks_review_and_archive(monkeypatch, tmp_path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "watchlist_id": "paper_watchlist",
                "items": [
                    {
                        "watchlist_id": "paper-000657",
                        "symbol": "000657",
                        "name": "中钨高新",
                        "strategy": "打板复核",
                        "entry_date": "2026-06-20",
                        "initial_reference_price": 10.0,
                        "expectation_fit_status": "pending",
                        "expectation_fit_label": "等待更多样本",
                        "research_only": True,
                        "paper_only": True,
                        "no_broker": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client = app.test_client()
    reviewed = client.post(
        "/api/paper-simulation/action",
        json={
            "symbol": "000657",
            "watchlist_id": "paper-000657",
            "action": "review",
            "note": "用户确认原打板假设偏离，进入降级复盘。",
        },
    ).get_json()

    assert reviewed["ok"] is True
    assert reviewed["item"]["review_status"] == "manual_downgrade_review"
    assert reviewed["item"]["review_label"] == "已确认降级复盘"
    assert reviewed["item"]["paper_lifecycle_state"] == "downgrade_review"
    assert reviewed["item"]["expectation_fit_status"] == "deviated"
    assert reviewed["item"]["no_real_orders"] is True
    assert reviewed["paper_tracking_summary"]["needs_review_count"] == 1

    archived = client.post(
        "/api/paper-simulation/action",
        json={"symbol": "000657", "watchlist_id": "paper-000657", "action": "archive"},
    ).get_json()

    assert archived["ok"] is True
    assert archived["item"]["review_status"] == "manual_archived"
    assert archived["item"]["paper_lifecycle_state"] == "archived"
    assert archived["active_watchlist_count"] == 0
    assert archived["archived_watchlist_count"] == 1
    assert archived["item"]["paper_exit_plan"]["auto_execute"] is False
    assert archived["item"]["paper_exit_plan"]["no_real_orders"] is True

    watchlist = client.get("/api/paper-simulation/watchlist").get_json()
    assert watchlist["watchlist_count"] == 1
    assert watchlist["active_watchlist_count"] == 0
    assert watchlist["archived_watchlist_count"] == 1


def test_strategy_lab_summary_endpoint_is_read_only_evidence(monkeypatch) -> None:
    def fake_read_json_file(path: Path) -> dict[str, object]:
        text = str(path).replace("\\", "/")
        if text.endswith("results/strategy_lab/v420_strategy_leaderboard_latest.json"):
            return {
                "status": "completed_with_warnings",
                "items": [
                    {
                        "strategy_id": "limit_up_family",
                        "research_rank": 1,
                        "strategy_health": "research_healthy",
                        "research_readiness": "research_ready_with_warnings",
                        "robustness_score": 0.77,
                        "mapping_coverage": 0.9,
                        "annualized_return": 0.24,
                        "Sharpe": 1.2,
                        "max_drawdown": -0.18,
                        "win_rate": 0.53,
                        "data_risk_level": "MODERATE_HIGH",
                        "limitation_note": "research-only leaderboard; not a trade instruction",
                    }
                ],
                "research_only": True,
                "no_broker": True,
                "no_buy_sell_advice": True,
            }
        if text.endswith("results/strategy_lab/v420_real_use_strategy_lab_handoff_latest.json"):
            return {
                "status": "completed_with_warnings",
                "created_at": "2026-06-23T11:00:00",
                "research_only_boundary_kept": True,
                "remaining_gap_for_one_stop_research": "provider freshness still needs explicit network update",
            }
        if text.endswith("results/strategy_lab/v421_strategy_statistics_latest.json"):
            return {
                "status": "completed",
                "items": [
                    {
                        "strategy_family": "limit_up_family",
                        "candidate_count": 8,
                        "signal_count": 6,
                    }
                ],
            }
        return {}

    monkeypatch.setattr(api_server, "read_json_file", fake_read_json_file)

    payload = app.test_client().get("/api/strategy-lab/summary").get_json()

    assert payload["ok"] is True
    assert payload["available"] is True
    assert payload["evidence_only"] is True
    assert payload["paper_only"] is True
    assert payload["no_broker"] is True
    assert payload["no_real_orders"] is True
    assert payload["not_position_advice"] is True
    assert payload["top_strategies"][0]["strategy_id"] == "limit_up_family"
    assert payload["top_strategies"][0]["candidate_count"] == 8
    assert payload["top_strategies"][0]["mapping_coverage"] == 0.9
    assert any("provider freshness" in item for item in payload["warnings_top"])
    assert "get_strategy_lab_summary" not in api_server.__dict__


def test_risk_notifications_summary_reads_paper_risk_and_guardrails(monkeypatch, tmp_path) -> None:
    import quantdesk.local_alerts as local_alerts

    monkeypatch.setattr(api_server, "project_root", tmp_path)
    watchlist_path = tmp_path / "state" / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "watchlist_id": "paper_watchlist",
                "paper_tracking_summary": {"updated_count": 1},
                "items": [
                    {
                        "symbol": "000657",
                        "name": "sample",
                        "review_status": "smart_stop_triggered_review",
                        "review_label": "paper review",
                        "smart_stop": {
                            "trigger_status": "triggered",
                            "trigger_reason": "daily close touched research risk line",
                        },
                        "paper_exit_plan": {
                            "paper_exit_candidate": True,
                            "condition_plan": {"forced_exit_rules": [{"source": "smart_stop"}]},
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "daily_workflow_latest.json").write_text(
        json.dumps(
            {
                "guardrail_summary": {
                    "enabled": True,
                    "status": "warning",
                    "warning_count": 2,
                    "failed_count": 0,
                    "feature_quality_status": "passed",
                    "statistical_validation_status": "warning",
                    "active_diagnostics": {"symbols": ["000657"]},
                    "warnings": ["stats warning"],
                },
                "risk_alert_snapshot": {
                    "status": "warning",
                    "alert_count": 1,
                    "critical_count": 1,
                    "top_alerts": [{"severity": "critical", "message": "review required"}],
                    "warnings": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (results_dir / "notification_center_latest.json").write_text(
        json.dumps(
            {
                "status": "dry_run",
                "dry_run": True,
                "message_count": 1,
                "warnings": ["dry run only"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        local_alerts,
        "local_alerts_payload",
        lambda limit=20: {"ok": True, "alerts": [{"title": "local review"}], "summary": {"total": 1}},
    )

    payload = app.test_client().get("/api/risk-notifications/summary").get_json()

    assert payload["ok"] is True
    assert payload["status"] == "warning"
    assert payload["paper_watchlist"]["watchlist_count"] == 1
    assert payload["smart_stop"]["triggered_count"] == 1
    assert payload["smart_stop"]["paper_exit_review_count"] == 1
    assert payload["smart_stop"]["triggered_items"][0]["forced_exit_rule_count"] == 1
    assert payload["risk_alerts"]["critical_count"] == 1
    assert payload["notification_center"]["dry_run"] is True
    assert payload["local_alerts"]["summary"]["total"] == 1
    assert payload["guardrails"]["feature_quality_status"] == "passed"
    assert payload["guardrails"]["active_diagnostics"]["symbols"] == ["000657"]
    assert payload["research_only"] is True
    assert payload["paper_only"] is True
    assert payload["no_broker"] is True
    assert payload["no_live_trading"] is True
    assert payload["no_auto_execution"] is True
    assert payload["no_position_advice"] is True
    assert "get_risk_notifications_summary" not in api_server.__dict__


def test_sync_today_candidates_to_paper_simulation_pool(monkeypatch, tmp_path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    candidates = [
        {
            "symbol": "000657",
            "name": "中钨高新",
            "select_date": "2026-06-22",
            "detail_lane": "board",
            "strategy_lane": "打板复核",
            "strategy_bucket": "board_review",
            "is_today_recommendation": True,
            "candidate_origin_label": "今日新候选",
            "current_price": 10.0,
            "reason_cn": "今日策略候选，进入纸面池做结果跟踪。",
            "tracking": {"status_label": "观察中"},
            "data_quality": {"status": "数据完整", "latest_date": "2026-06-22"},
            "priority_rank": 1,
            "priority_score": 10,
            "pool_action": {"status": "active"},
        },
        {
            "symbol": "000725",
            "name": "京东方A",
            "select_date": "2026-06-18",
            "detail_lane": "long",
            "strategy_lane": "长期-只观察",
            "is_today_recommendation": False,
            "current_price": 5.0,
            "reason_cn": "历史观察样本，默认不自动进入纸面池。",
            "tracking": {"status_label": "观察中"},
            "data_quality": {"status": "数据完整", "latest_date": "2026-06-22"},
            "priority_rank": "",
            "priority_score": 1,
            "pool_action": {"status": "active"},
        },
    ]
    monkeypatch.setattr(api_server, "candidate_rows", lambda limit=18: candidates)
    monkeypatch.setattr(api_server, "annotate_recommendation_priorities", lambda rows: rows)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-22", "2026-06-23"]),
            "open": [10.0, 10.5],
            "high": [10.5, 11.0],
            "low": [9.9, 10.3],
            "close": [10.0, 10.8],
            "volume": [1000, 1200],
        }
    )
    monkeypatch.setattr(api_server, "load_stock_data", lambda symbol: frame if symbol == "000657" else pd.DataFrame())

    client = app.test_client()
    response = client.post("/api/paper-simulation/sync-candidates", json={"max_candidates": 10})
    payload = response.get_json()

    assert response.status_code == 200
    _assert_clean_payload(payload)
    assert payload["status"] == "synced"
    assert payload["selected_count"] == 1
    assert payload["added_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["selected_symbols"] == ["000657"]
    assert payload["strategy_counts"]["打板复核"] == 1
    assert payload["watchlist_count"] == 1
    assert payload["paper_tracking_summary"]["updated_count"] == 1

    written = json.loads(watchlist_path.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in written["items"]] == ["000657"]
    assert written["items"][0]["return_since_added_pct"] == 8.0
    assert written["items"][0]["expectation_fit_status"] == "fit"
    assert written["items"][0]["paper_research_contract"]["primary_action"] == "打板复核"
    assert written["items"][0]["paper_only"] is True
    assert written["items"][0]["no_broker"] is True


def test_strategy_scorecard_summarizes_today_paper_and_review_loop(monkeypatch, tmp_path) -> None:
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    weights_path = tmp_path / "strategy_effectiveness_weights.json"
    sentinel_weights = b'{"sentinel":true}\n'
    weights_path.write_bytes(sentinel_weights)
    before_weights_mtime = weights_path.stat().st_mtime_ns
    monkeypatch.setattr(api_server, "STRATEGY_EFFECTIVENESS_JSON", weights_path)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "watchlist_id": "paper_watchlist",
                "paper_tracking_summary": {
                    "updated_count": 2,
                    "needs_review_count": 1,
                    "expectation_fit_counts": {"符合原始预期": 1, "偏离原始预期": 1},
                },
                "items": [
                    {
                        "symbol": "000657",
                        "name": "中钨高新",
                        "primary_action": "打板复核",
                        "strategy": "打板复核",
                        "expectation_fit_status": "fit",
                        "expectation_fit_label": "符合原始预期",
                        "return_since_added_pct": 5.2,
                        "max_drawdown_since_added": -1.1,
                        "review_tone": "ok",
                        "llm_structured_decision": {
                            "decision_status": "建议进入纸面模拟池",
                            "primary_action": "打板复核",
                            "confidence": "高",
                            "validation_days": 3,
                            "success_condition": "1-3 个完整交易日仍能保持量价弹性。",
                            "failure_condition": "短线弹性消失或回撤扩大，进入降级复盘。",
                            "review_sentence": "优先进入纸面池验证原始策略假设。",
                        },
                    },
                    {
                        "symbol": "000032",
                        "name": "深桑达A",
                        "primary_action": "打板复核",
                        "strategy": "打板复核",
                        "expectation_fit_status": "deviated",
                        "expectation_fit_label": "偏离原始预期",
                        "expectation_fit_reason": "打板样本到复核窗口仍未体现短线弹性。",
                        "return_since_added_pct": -4.1,
                        "max_drawdown_since_added": -6.3,
                        "review_tone": "warn",
                        "review_label": "打板假设偏离，待降级复盘",
                        "llm_structured_decision": {
                            "decision_status": "建议进入纸面模拟池",
                            "primary_action": "打板复核",
                            "confidence": "中",
                            "validation_days": 3,
                            "success_condition": "放量修复且回撤收敛。",
                            "failure_condition": "继续低于关键均线。",
                            "missing_data": "新闻线索缺失",
                            "review_sentence": "短线弹性没有兑现，应先复盘原因。",
                        },
                    },
                    {
                        "symbol": "600110",
                        "name": "诺德股份",
                        "primary_action": "打板复核",
                        "strategy": "打板复核",
                        "paper_lifecycle_state": "archived",
                        "expectation_fit_status": "deviated",
                        "expectation_fit_label": "偏离原始预期",
                        "review_tone": "warn",
                        "review_label": "已归档保留",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        api_server,
        "candidate_rows",
        lambda limit=18: [
            {
                "symbol": "000657",
                "name": "中钨高新",
                "strategy_lane": "打板复核",
                "priority_rank": 1,
                "priority_score": 10,
                "is_today_recommendation": True,
                "data_status": "数据完整",
                "data_quality": {"tone": "ok", "status": "数据完整", "row_count": 120},
                "tracking": {"status_label": "阶段符合观察假设", "status_tone": "ok", "elapsed_trading_days": 2},
                "reason_cn": "量价弹性较强。",
            }
        ],
    )
    monkeypatch.setattr(api_server, "annotate_recommendation_priorities", lambda rows: rows)
    monkeypatch.setattr(api_server, "generation_status_for_ui", lambda: {"status": "completed", "candidate_count": 1})
    monkeypatch.setattr(
        api_server,
        "previous_recommendation_performance",
        lambda current_signal_date="", limit=10: {
            "available": True,
            "batch_date": "2026-06-21",
            "sample_count": 2,
            "fit_count": 1,
            "deviated_or_gap_count": 1,
            "items": [],
        },
    )

    payload = app.test_client().get("/api/strategy-scorecard").get_json()

    assert payload["ok"] is True
    assert payload["today_focus_count"] == 1
    assert payload["paper_pool_count"] == 2
    assert payload["paper_pool_total_count"] == 3
    assert payload["paper_archived_count"] == 1
    assert payload["review_needed_count"] == 1
    assert payload["paper_expectation_fit_counts"]["偏离原始预期"] == 1
    assert payload["by_strategy"][0]["strategy"] == "打板复核"
    assert payload["by_strategy"][0]["sample_count"] == 2
    assert payload["by_strategy"][0]["scorecard_label"] == "需要降权/复盘"
    assert payload["by_strategy"][0]["decision_status_counts"]["建议进入纸面模拟池"] == 2
    assert payload["by_strategy"][0]["examples"][0]["decision_status"] == "建议进入纸面模拟池"
    assert payload["paper_research_decision_count"] == 2
    assert payload["paper_research_decisions"][0]["symbol"] == "000032"
    assert payload["paper_research_decisions"][0]["alignment_label"] == "偏离研究判断"
    assert payload["paper_research_decisions"][0]["failure_condition"] == "继续低于关键均线。"
    assert payload["paper_research_decisions"][1]["alignment_label"] == "已兑现研究判断"
    assert payload["review_needed"][0]["symbol"] == "000032"
    assert payload["review_needed"][0]["research_decision"]["decision_status"] == "建议进入纸面模拟池"
    assert payload["previous_recommendation_performance"]["batch_date"] == "2026-06-21"
    assert payload["previous_recommendation_performance"]["fit_count"] == 1
    assert payload["research_only"] is True
    assert payload["paper_only"] is True
    assert payload["no_broker"] is True
    assert weights_path.read_bytes() == sentinel_weights
    assert weights_path.stat().st_mtime_ns == before_weights_mtime


def _install_scorecard_purity_fixture(monkeypatch, tmp_path) -> Path:
    """Minimal read-only scorecard fixture returning the patched weights path.

    The on-disk weights deliberately carry a non-neutral tilt so that a hidden
    re-persist during a read-only GET would visibly reorder candidates.
    """

    watchlist_path = tmp_path / "paper_watchlist.json"
    watchlist_path.write_text(
        json.dumps({"ok": True, "watchlist_id": "paper_watchlist", "items": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)

    weights_path = tmp_path / "strategy_effectiveness_weights.json"
    weights_path.write_text(
        json.dumps(
            {
                "ok": True,
                "weight_by_bucket": {"打板复核": 1.4, "长期/波段复核": 0.6},
                "strategies": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "STRATEGY_EFFECTIVENESS_JSON", weights_path)
    monkeypatch.setattr(api_server, "generation_status_for_ui", lambda: {"status": "completed", "candidate_count": 2})
    monkeypatch.setattr(
        api_server,
        "previous_recommendation_performance",
        lambda current_signal_date="", limit=10: {"available": False},
    )
    monkeypatch.setattr(
        api_server,
        "candidate_rows",
        lambda limit=18: [
            {
                "symbol": "000657",
                "name": "板块样本",
                "strategy_lane": "打板复核",
                "group_key": "board",
                "is_today_recommendation": True,
                "data_quality": {"tone": "ok", "status": "数据完整", "row_count": 120},
                "tracking": {"status_label": "阶段符合观察假设", "status_tone": "ok", "elapsed_trading_days": 2},
                "reason_cn": "量价弹性较强。",
            },
            {
                "symbol": "000032",
                "name": "波段样本",
                "strategy_lane": "长期/波段复核",
                "group_key": "long_swing",
                "is_today_recommendation": True,
                "data_quality": {"tone": "ok", "status": "数据完整", "row_count": 120},
                "tracking": {"status_label": "阶段符合观察假设", "status_tone": "ok", "elapsed_trading_days": 4},
                "reason_cn": "均线结构良好。",
            },
        ],
    )
    return weights_path


def _ranked_symbols(rows: list[dict[str, object]]) -> list[tuple[str, object]]:
    annotated = api_server.annotate_recommendation_priorities([dict(row) for row in rows])
    return [(str(row.get("symbol")), row.get("priority_rank")) for row in annotated]


def test_strategy_scorecard_get_does_not_persist_weights_or_reorder_candidates(monkeypatch, tmp_path) -> None:
    """GET /api/strategy-scorecard must be a pure read.

    Regression guard for the P0 read-only violation where the GET recomputed and
    re-persisted results/strategy_effectiveness_weights.json, letting a page
    refresh silently change later research candidate ordering.
    """

    weights_path = _install_scorecard_purity_fixture(monkeypatch, tmp_path)
    rows = api_server.candidate_rows()

    before_bytes = weights_path.read_bytes()
    before_mtime = weights_path.stat().st_mtime_ns
    before_order = _ranked_symbols(rows)

    payload = app.test_client().get("/api/strategy-scorecard").get_json()

    assert payload["ok"] is True
    # The computed weights are still reported to the UI, just never written.
    assert isinstance(payload["strategy_effectiveness"], dict)
    assert weights_path.read_bytes() == before_bytes
    assert weights_path.stat().st_mtime_ns == before_mtime
    assert _ranked_symbols(rows) == before_order
    # The on-disk tilt (not a freshly computed neutral one) still drives ordering.
    assert [symbol for symbol, _rank in before_order] == ["000657", "000032"]

    # Repeated reads stay pure.
    app.test_client().get("/api/strategy-scorecard")
    assert weights_path.read_bytes() == before_bytes
    assert weights_path.stat().st_mtime_ns == before_mtime
    assert _ranked_symbols(rows) == before_order


def test_compute_strategy_effectiveness_is_pure_unless_persist_requested(monkeypatch, tmp_path) -> None:
    """The explicit writer must still work; the default path must not write."""

    weights_path = _install_scorecard_purity_fixture(monkeypatch, tmp_path)
    before_bytes = weights_path.read_bytes()
    scorecard = {"by_strategy": [{"strategy": "打板复核", "sample_count": 6, "fit_rate": 80.0}]}

    pure = api_server.compute_strategy_effectiveness(scorecard)
    assert pure["ok"] is True
    assert weights_path.read_bytes() == before_bytes

    assert api_server.persist_strategy_effectiveness(pure) is True
    written = json.loads(weights_path.read_text(encoding="utf-8"))
    assert written["weight_by_bucket"] == pure["weight_by_bucket"]

    weights_path.write_bytes(before_bytes)
    persisted = api_server.compute_strategy_effectiveness(scorecard, persist=True)
    reloaded = json.loads(weights_path.read_text(encoding="utf-8"))
    assert reloaded["weight_by_bucket"] == persisted["weight_by_bucket"]


def test_previous_recommendation_performance_summarizes_prior_batch(monkeypatch) -> None:
    ledger = pd.DataFrame(
        [
            {
                "entry_date": "2026-06-22",
                "signal_date": "2026-06-22",
                "ts_code": "000001.SZ",
                "stock_name": "今日样本",
                "strategy_type": "da_ban",
                "candidate_role": "daily_recommendation",
            },
            {
                "entry_date": "2026-06-21",
                "signal_date": "2026-06-21",
                "ts_code": "000002.SZ",
                "stock_name": "上一批A",
                "strategy_type": "da_ban",
                "candidate_role": "daily_recommendation",
            },
            {
                "entry_date": "2026-06-21",
                "signal_date": "2026-06-21",
                "ts_code": "000003.SZ",
                "stock_name": "上一批B",
                "strategy_type": "long_term",
                "candidate_role": "daily_recommendation",
            },
        ]
    )
    monkeypatch.setattr(api_server, "load_candidate_ledger", lambda: ledger)

    def fake_build_candidate(symbol: str, row: pd.Series | None = None) -> dict[str, object]:
        is_fit = api_server.normalize_symbol(symbol) == "000002"
        return {
            "symbol": api_server.normalize_symbol(symbol),
            "name": "上一批A" if is_fit else "上一批B",
            "strategy_lane": "打板复核" if is_fit else "长期-波段复核",
            "data_status": "数据完整",
            "tracking": {
                "status_label": "阶段符合观察假设" if is_fit else "偏离观察假设",
                "status_tone": "ok" if is_fit else "warn",
                "return_since_select": 3.0 if is_fit else -2.0,
                "max_drawdown_since_select": -1.0 if is_fit else -5.0,
                "elapsed_trading_days": 2,
                "review_note": "上一批候选复盘样本。",
                "expected_path": "验证原始假设。",
            },
        }

    monkeypatch.setattr(api_server, "build_candidate", fake_build_candidate)

    payload = api_server.previous_recommendation_performance("2026-06-22")

    assert payload["available"] is True
    assert payload["batch_date"] == "2026-06-21"
    assert payload["sample_count"] == 2
    assert payload["fit_count"] == 1
    assert payload["deviated_or_gap_count"] == 1
    assert payload["avg_return_since_select_pct"] == 0.5
    assert payload["items"][0]["status_label"] == "阶段符合观察假设"
    assert payload["paper_only"] is True


def test_generate_recommendations_syncs_today_candidates_to_paper_pool(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_generate_daily_recommendations(*args, **kwargs):
        calls.append({"target": "generate", "args": args, "kwargs": kwargs})
        return {
            "ok": True,
            "status": "completed",
            "signal_date": "2026-06-23",
            "candidate_count": 2,
            "message_for_ui": "generated",
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        }

    def fake_sync_candidates_to_paper_simulation_pool(*, include_observation=False, max_candidates=10):
        calls.append(
            {
                "target": "sync",
                "include_observation": include_observation,
                "max_candidates": max_candidates,
            }
        )
        return {
            "ok": True,
            "status": "synced",
            "selected_count": 2,
            "watchlist_count": 2,
            "paper_tracking_summary": {"updated_count": 2},
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
        }

    monkeypatch.setattr(api_server, "generate_daily_recommendations", fake_generate_daily_recommendations)
    monkeypatch.setattr(api_server, "sync_candidates_to_paper_simulation_pool", fake_sync_candidates_to_paper_simulation_pool)
    monkeypatch.setattr(
        api_server,
        "evaluate_data_chain_consumer_gate",
        lambda **kwargs: {"status": "passed", "allowed": True, "reasons": []},
    )

    response = app.test_client().post("/api/daily/generate-recommendations", json={"max_candidates": 18})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "completed"
    assert payload["paper_simulation_sync"]["status"] == "synced"
    assert payload["paper_simulation_sync"]["selected_count"] == 2
    assert payload["paper_simulation_sync_policy"]
    assert "paper portfolio trade" in payload["paper_simulation_sync_policy"]
    assert any(call.get("target") == "generate" for call in calls)
    sync_call = next(call for call in calls if call.get("target") == "sync")
    assert sync_call["include_observation"] is False
    assert sync_call["max_candidates"] == 18


def test_generate_recommendations_can_skip_paper_sync(monkeypatch) -> None:
    sync_called = False

    def fake_generate_daily_recommendations(*args, **kwargs):
        return {"ok": True, "status": "completed", "candidate_count": 1, "research_only": True}

    def fake_sync_candidates_to_paper_simulation_pool(**kwargs):  # pragma: no cover - should not be called
        nonlocal sync_called
        sync_called = True
        return {}

    monkeypatch.setattr(api_server, "generate_daily_recommendations", fake_generate_daily_recommendations)
    monkeypatch.setattr(api_server, "sync_candidates_to_paper_simulation_pool", fake_sync_candidates_to_paper_simulation_pool)
    monkeypatch.setattr(
        api_server,
        "evaluate_data_chain_consumer_gate",
        lambda **kwargs: {"status": "passed", "allowed": True, "reasons": []},
    )

    response = app.test_client().post("/api/daily/generate-recommendations", json={"sync_paper": False})
    payload = response.get_json()

    assert response.status_code == 200
    assert sync_called is False
    assert payload["paper_simulation_sync"]["status"] == "skipped"
    assert payload["paper_simulation_sync"]["paper_only"] is True


def test_backtest_run_can_use_strategy_screening_handoff(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(backtest_routes, "project_root", tmp_path)
    dates = pd.date_range("2024-01-01", periods=190, freq="D")
    close = np.concatenate(
        [
            np.linspace(8.0, 10.0, 125),
            np.linspace(10.2, 15.0, 35),
            np.linspace(14.6, 7.6, 30),
        ]
    )
    volume = np.full(190, 1_000_000.0)
    volume[126:160] = 3_000_000.0
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "adjust": ["raw"] * len(dates),
        }
    )
    handoff = {
        "status": "ready",
        "strategy": "ma_breakout",
        "symbols": ["000001"],
        "source": "candidate_pool",
    }
    monkeypatch.setattr(backtest_routes, "_strategy_screening_backtest_handoff", lambda max_symbols: (["000001"], handoff))
    monkeypatch.setattr(
        backtest_routes,
        "_load_backtest_histories",
        lambda max_symbols, symbols=None, candidate_rows_func=None: {"000001": frame},
    )

    client = app.test_client()
    response = client.post("/api/backtest/run", json={"source": "strategy_screening", "strategy": "equal_weight_random", "max_symbols": 5})
    payload = response.get_json()

    assert response.status_code == 200
    _assert_clean_payload(payload)
    assert payload["ok"] is True
    assert payload["source"] == "strategy_screening"
    assert payload["strategy"] == "ma_breakout"
    assert payload["symbols"] == ["000001"]
    assert payload["backtest_handoff"]["status"] == "ready"
    assert payload["symbol_count"] == 1


def test_daily_research_frontend_has_local_echarts_and_no_cdn() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/daily_research.html").read_text(encoding="utf-8")
    script = (root / "app/static/js/daily_research.js").read_text(encoding="utf-8")
    assert "/static/echarts.min.js" in template
    assert "/static/js/daily_research.js?v=" in template
    assert "__QD_DAILY_RESEARCH_ASSET_VERSION__" in template
    assert "cdn" not in (template + script).lower()
    assert (root / "app/static/echarts.min.js").exists()
    assert "priceChart" in script
    assert "volumeChart" in script
    assert "macdChart" in script


def test_frontend_disclaimer_text_does_not_render_boolean_research_only() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/daily_research.html").read_text(encoding="utf-8")
    script = (root / "app/static/js/daily_research.js").read_text(encoding="utf-8")

    assert "function researchOnlyDisclaimer" in script
    assert 'typeof data.research_only === "string"' in script
    assert "researchOnlyDisclaimer(data, \"历史回测不代表未来收益，不构成投资建议。\")" in script
    assert "kdjChart" in script
    assert "rsiChart" in script
    assert "activeCharts" in script
    assert "function _glossaryStockStatus" in script
    assert "dailyFocusGrid" in template
    assert "candidateGapPanel" in template
    assert "quantSummaryPanel" in template
    assert "Quant 日总结" in template
    side_targets = [
        'data-side-module-target="daily-focus-section"',
        'data-side-module-target="paper-watchlist-section"',
        'data-side-module-target="agent-section"',
        'data-side-module-target="backtest-section"',
        'data-side-module-target="strategy-lab-section"',
        'data-side-module-target="data-ops-section"',
    ]
    for target in side_targets:
        assert target in template
    assert "moduleLauncher" not in template
    assert "position: sticky" in template
    assert "module-dock-fixed" not in template
    assert "candidate-brief" in template
    assert "key-review-panel" in template
    assert "candidate-action-panel" in template
    assert "scenario-chart" in template
    assert "side-rail" in template
    assert "sideCollapseButton" in template
    assert "side-collapse-button" in template
    assert "nav-collapsed" in template
    assert "data-module-home" in template
    assert "data-home-section" in template
    assert "data-module-view" in template
    assert 'data-side-module-target="daily-focus-section"' in template
    assert 'data-side-module-target="paper-watchlist-section"' in template
    assert 'data-side-module-target="agent-section"' in template
    assert 'data-side-module-target="backtest-section"' in template
    assert 'data-side-module-target="data-ops-section"' in template
    for legacy_section in (
        'id="limit-review-section"',
        'id="theme-map-section"',
        'id="quant-summary-section"',
        'id="candidate-pool-section"',
        'id="strategy-lab-section"',
        'id="smart-stop-section"',
        'id="notification-center-section"',
        'id="risk-notifications-section"',
        'id="backtest-section"',
    ):
        assert legacy_section in template
    assert "返回首页" in template
    assert "setupModuleNavigation" in script
    assert "showModuleView" in script
    assert "showHomeView" in script
    assert "highlightText" in script
    assert "highlightHtmlText" in script
    assert "keyReviewRow" in script
    assert "renderScenarioProjectionChart" in script
    assert "renderCandidateActionPanel" in script
    assert "renderCandidatePoolPolicy" in script
    assert "candidate-zone-today" in template
    assert "candidate-zone-history" in template
    assert "candidate-zone-exit" in template
    assert "今日新候选" in script
    assert "历史候选 / 观察池" in script
    assert "今日推荐" not in script
    assert "历史推荐 / 观察池" not in script
    assert "历史推荐" not in template
    assert "待移出 / 降级复盘" in script
    assert "/api/daily/candidate-action" in script
    assert "/api/paper-simulation/add-candidate" in script
    assert 'data-candidate-action="paper"' in script
    assert 'data-candidate-action="review"' in script
    assert "归档/忽略" in script
    assert "addDiscussedCandidateToPaperButton" in template
    assert "addDiscussedCandidateToPaperPool" in script
    assert "llm_research_opinion" in script
    assert "user_confirmed_after_llm" in script
    assert "renderPaperWatchlistSummary" in script
    assert "setupPaperWatchlist" in script
    assert "renderPaperWatchlist" in script
    assert "原始研究假设" in script
    assert "LLM 研究意见" in script
    assert "每日跟踪计划" in script
    assert "成功条件" in script
    assert "失败/降级条件" in script
    assert "paper-contract-grid" in template
    assert "paper-llm-opinion" in template
    assert "loadStrategyScorecard" in script
    assert "renderStrategyScorecard" in script
    assert "上一批候选表现" in script
    assert "纸面研究决策复盘" in script
    assert "paper_research_decisions" in script
    assert "当初判断" in script
    assert "当前兑现" in script
    assert "previous_recommendation_performance" in script
    assert "/api/strategy-scorecard" in script
    assert "paperWatchlistPanel" in template
    assert "paperWatchlistNote" in template
    assert "/api/decision-board" in script
    assert "setupSmartStop" in script
    assert "renderSmartStop" in script
    assert "smartStopPanel" in template
    assert "setupNotificationCenter" in script
    assert "renderNotificationCenter" in script
    assert "notificationCenterPanel" in template
    assert "paper_watchlist" in script
    assert "strategy_lab" in script
    assert "lab.top_strategies" in script
    assert "mapping_coverage" in script
    assert "setupStrategyLab" in script
    assert "renderStrategyLab" in script
    assert "/api/strategy-lab/summary" in script
    assert "strategyLabPanel" in template
    assert "研究快照状态" in script
    assert "证据快照文件" in script
    assert "交接状态" not in script
    assert "交接文件" not in script
    assert "纸面模拟池（确认/同步 / 本地跟踪）" in script
    assert "纸面池研究证据" in script
    assert "仅作证据" in script
    assert "风险线" in script
    assert "refreshPaperSimulationButton" in template
    assert "refreshPaperSimulation" in script
    assert "/api/paper-simulation/refresh" in script
    assert "/api/paper-simulation/action" in script
    assert "handlePaperLifecycleAction" in script
    assert "data-paper-action=\"review\"" in script
    assert "确认降级复盘" in script
    assert "归档保留" in script
    assert "恢复跟踪" in script
    assert "paper-action-row" in template
    assert "syncPaperCandidatesButton" in template
    assert "syncCandidatesToPaperSimulation" in script
    assert "/api/paper-simulation/sync-candidates" in script
    assert 'select.value ? select.value : "ma_breakout"' in script
    assert 'select.value ? select.value : "ma_trend"' not in script
    assert "homeButtons" in script
    assert "setupSidebarCollapse" in script
    assert "SIDEBAR_COLLAPSE_KEY" in script
    assert "localStorage.setItem" in script
    assert "position: fixed" in template
    assert 'window.dispatchEvent(new Event("resize"))' in script
    assert "history.replaceState" in script
    assert 'event.key === "Escape"' in script
    assert 'event.altKey && event.key === "ArrowLeft"' in script
    assert "limitReviewPanel" in template
    assert "涨停复盘报告" in template
    assert "refreshLimitReviewButton" in template
    assert "themeMapPanel" in template
    assert "板块/题材映射" in template
    assert "refreshThemeMapNetworkButton" in template
    assert "generateThemeMapDraftButton" in template
    assert "realtimeTitle" in template
    assert "stockSearchInput" in template
    assert "refreshRealtimeButton" in template
    assert "loadRealtimeMarket" in script
    assert "showRealtimeStockDetail" in script
    assert "realtimeFreshnessCard" in script
    assert "数据新鲜度" in script
    assert "收盘后复盘快照" in script or "data_freshness_label" in script
    assert "不是最新" not in script
    assert "provider_note" in script
    assert 'safeText(data.provider_note || "")' not in script
    assert "indexMetricHint" in script
    assert "current_view_override" in script
    assert "当前候选数据" in script
    assert "startup_update_explanation" in script
    assert "startup-update-explain" in template
    assert "下一步：" in script
    assert "研究候选批次" in script
    assert "freshness_message" in script
    assert "freshness_label" in script
    assert "refreshResearchLoopButton" in template
    assert "刷新研究闭环" in template
    assert "refreshResearchLoop" in script
    assert "data-research-loop-refresh" in script
    assert "/api/startup/refresh" in script
    assert "daily --with-guardrails true" not in script
    assert "每日流程开启防护诊断时" in script
    assert "loadPaperWatchlist(true)" in script
    assert "loadStrategyScorecard()" in script
    assert "loadDataHealth()" in script
    assert "查看全 A 收盘更新和当前研究结果" in script
    assert "renderQuantDailySummary" in script
    assert "renderQuantLimitReviewDigest" in script
    assert "涨停复盘摘要" in script
    assert "追溯证据" in script
    assert "renderAttributionEvidence" in script
    assert "evidence-pill" in template
    assert "补齐 stock_theme_map.csv" in template
    assert "行业线索" in script
    assert "provider行业" not in template
    assert "Provider 诊断" not in template
    assert "setupThemeMapTools" in script
    assert "loadThemeMapStatus" in script
    assert "/api/theme-map/status" in script
    assert "/api/theme-map/refresh" in script
    assert "/api/theme-map/import-draft" in script
    assert "network: isNetwork" in script
    assert "loadLimitUpReview(false, true)" in script
    assert "stock_theme_map_import.csv" in script
    assert "回测可用性" in script
    assert "loadLimitUpReview" in script
    assert "/api/limit-up/refresh" in script
    assert "network: true" in script
    assert "加入手动观察池" in script
    assert "策略跟踪验证" in script
    assert "axisPointer: { link" not in script
    assert "chart-card" in template
    assert "命令中心" not in template
    assert "agent-section" in template
    assert "研究助手" in template
    assert "agentNetworkToggle" in template
    assert "Not Investment Advice" in template
    assert "setupAgentAssistant" in script
    assert "loadAgentAssistant" in script
    assert "agentLlmStatus" in script
    assert "agentSkillLabel" in script
    assert "纸面模拟池决策" in script
    assert "外部 API 未就绪，本地证据分析可用" in script
    assert "Agent 已返回本地证据分析；未调用外部 API。" in script
    assert "/api/agent/context" in script
    assert "/api/agent/chat" in script
    assert "network: Boolean(networkToggle?.checked)" in script
    assert "paper_decision" in script
    assert "normalizeAgentPaperDecision" in script
    assert "renderAgentPaperDecisionCard" in script
    assert "agent-decision-card" in template
    assert "recommended_lifecycle_action" in script
    assert "llm_primary_action" in script
    assert "llm_validation_days" in script
    assert "llm_success_condition" in script
    assert "llm_failure_condition" in script
    assert "llm_missing_data" in script
    assert "llm_review_sentence" in script
    assert "/api/paper-simulation/action" in script
    assert 'data-candidate-action="discuss"' in script
    assert "openCandidateAgentDiscussion" in script
    assert "paper_simulation_decision" in script
    assert "window.quantdeskShowModuleView" in script
    assert "chanlun" not in template.lower()


def test_daily_research_cache_busting_and_legacy_detail_alias(monkeypatch) -> None:
    _install_self_contained_candidate_fixture(monkeypatch)
    client = app.test_client()
    page = client.get("/")
    html = page.get_data(as_text=True)
    assert page.headers["Cache-Control"].startswith("no-store")
    assert "/static/js/daily_research.js?v=" in html

    script = client.get("/static/js/daily_research.js")
    assert script.headers["Cache-Control"].startswith("no-store")

    candidates = client.get("/api/daily/candidates").get_json()
    first = candidates["candidates"][0]
    canonical = client.get(f"/api/stock/{first['symbol']}/detail")
    legacy = client.get(f"/api/stock-detail?symbol={first['symbol']}")
    assert canonical.status_code == 200
    assert legacy.status_code == 200
    assert legacy.get_json()["basic_info"]["symbol"] == canonical.get_json()["basic_info"]["symbol"]


def test_integrated_agent_endpoints_are_available_and_network_gated(monkeypatch, tmp_path) -> None:
    from quantdesk import llm_config
    from quantdesk.llm_providers import openai_compatible_provider
    from quantdesk.llm_providers.base import build_llm_result

    client = app.test_client()
    watchlist_path = tmp_path / "paper_watchlist.json"
    monkeypatch.setattr(api_server, "PAPER_WATCHLIST_JSON", watchlist_path)
    watchlist_path.write_text(
        json.dumps(
            {
                "ok": True,
                "watchlist_id": "paper_watchlist",
                "paper_tracking_summary": {"updated_count": 1, "needs_review_count": 1},
                "items": [
                    {
                        "symbol": "000657",
                        "name": "中钨高新",
                        "strategy": "打板复核",
                        "primary_action": "打板复核",
                        "strategy_tags": ["打板", "今日重点"],
                        "entry_date": "2026-06-22",
                        "review_status": "board_downgrade_review",
                        "review_label": "打板假设偏离，待降级复盘",
                        "expectation_fit_status": "deviated",
                        "expectation_fit_label": "偏离原始预期",
                        "expectation_fit_reason": "短线弹性未兑现。",
                        "return_since_added_pct": -4.2,
                        "max_drawdown_since_added": -6.1,
                        "elapsed_trading_days": 3,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    skills = client.get("/api/agent/skills")
    assert skills.status_code == 200
    skills_payload = skills.get_json()
    assert skills_payload["ok"] is True
    assert "daily_review" in skills_payload["skills"]

    monkeypatch.setattr(
        api_server,
        "candidate_rows",
        lambda limit=18: [
            {
                "symbol": "000657",
                "name": "中钨高新",
                "strategy_lane": "打板复核",
                "priority_rank": 1,
                "risk_level": "高",
                "data_status": "数据完整",
                "is_today_recommendation": True,
                "reason_cn": "量价突破观察，短线动能转强。",
                "tracking": {"status_label": "观察中", "elapsed_trading_days": 0},
            },
            {
                "symbol": "000725",
                "name": "京东方A",
                "strategy_lane": "长期-波段复核",
                "priority_rank": 2,
                "risk_level": "中",
                "data_status": "数据完整",
                "is_today_recommendation": False,
                "reason_cn": "趋势修复观察，等待后续交易日验证。",
            },
        ],
    )
    monkeypatch.setattr(api_server, "annotate_recommendation_priorities", lambda rows: rows)

    context = client.get("/api/agent/context")
    assert context.status_code == 200
    context_payload = context.get_json()
    assert context_payload["ok"] is True
    assert context_payload["no_broker"] is True
    assert context_payload["no_buy_sell_advice"] is True
    assert context_payload["context"]["candidate_count"] == 2
    assert context_payload["context"]["today_recommendation_count"] == 1
    assert context_payload["context"]["observation_candidate_count"] == 1
    assert context_payload["context"]["candidates_preview"][0]["symbol"] == "000657"
    assert context_payload["context"]["paper_simulation_watchlist_count"] == 1
    assert context_payload["context"]["paper_simulation_preview"][0]["symbol"] == "000657"
    assert "smart_stop" in context_payload["context"]["paper_simulation_preview"][0]
    assert "primary_action" in context_payload["context"]["paper_simulation_preview"][0]
    assert "expectation_fit_label" in context_payload["context"]["paper_simulation_preview"][0]
    assert "daily_tracking_plan" in context_payload["context"]["paper_simulation_preview"][0]
    assert context_payload["context"]["paper_simulation_summary"]["needs_review_count"] == 1
    assert "strategy_scorecard_summary" in context_payload["context"]
    assert context_payload["context"]["strategy_scorecard_summary"]["paper_pool_count"] == 1

    stock_df = pd.DataFrame(
        {
            "date": pd.date_range("2026-03-01", periods=90, freq="D"),
            "open": [10 + index * 0.05 for index in range(90)],
            "high": [10.2 + index * 0.05 for index in range(90)],
            "low": [9.8 + index * 0.05 for index in range(90)],
            "close": [10.1 + index * 0.05 for index in range(90)],
            "volume": [1000000 + index * 1000 for index in range(90)],
            "amount": [10000000 + index * 10000 for index in range(90)],
        }
    )
    monkeypatch.setattr(api_server, "load_stock_data", lambda symbol: stock_df.copy())
    monkeypatch.setattr(
        api_server,
        "build_news_summary",
        lambda symbol: {
            "available": True,
            "count": 1,
            "items": [{"title": "公司公告显示订单线索增强", "published": "2026-06-23", "source": "本地新闻缓存"}],
        },
    )
    monkeypatch.setattr(
        api_server,
        "build_industry_comparison",
        lambda symbol, stock_pct_chg: {
            "available": True,
            "sector_name": "有色金属",
            "sector_change": 1.8,
            "sector_rank": 3,
            "stock_vs_sector": 2.1,
            "hint": "本地行业缓存对比。",
        },
    )
    monkeypatch.setattr(
        api_server,
        "build_tracking_vs_benchmark",
        lambda stock_df, entry_date: {
            "available": True,
            "entry_date": entry_date,
            "trading_days": 3,
            "stock_return_pct": 4.2,
            "benchmark_return_pct": 0.8,
            "excess_return_pct": 3.4,
            "verdict": "入选以来跑赢上证指数。",
        },
    )
    monkeypatch.setattr(
        api_server,
        "build_research_reference",
        lambda stock_df: {
            "available": True,
            "disclaimer": "研究情景不是确定预测。",
            "price_band": {
                "latest": 14.55,
                "volatility_label": "中波动",
                "daily_volatility_pct": 2.0,
                "reference_1d_pct": 2.0,
                "reference_5d_pct": 4.5,
                "reference_1d": {"low": 14.26, "high": 14.84},
                "reference_5d": {"low": 13.89, "high": 15.21},
                "note": "基于本地历史波动。",
            },
            "support_resistance": {
                "high_60d": 15.0,
                "low_60d": 11.0,
                "pct_from_high": -3.0,
                "pct_from_low": 32.0,
                "position": "接近60日高点",
            },
            "simple_backtest": {
                "total_trades": 6,
                "win_rate_pct": 66.7,
                "avg_return_pct": 2.3,
                "label": "MA5/MA20 交叉样本可参考。",
                "note": "仅用于纸面研究。",
            },
            "recent_trend": {
                "return_5d_pct": 5.2,
                "return_20d_pct": 18.4,
                "trend_5d": "上行",
                "trend_20d": "上行",
            },
            "scenario_projection": {
                "bias": "偏强进攻观察",
                "thesis": "量价路径偏强，重点验证是否延续。",
                "structure_note": "MA5 在 MA20 上方；近5日量能强于20日均量。",
                "confidence_note": "基于本地历史波动生成。",
                "scenarios": [
                    {
                        "name": "进攻情景",
                        "tone": "strong",
                        "weight": "主观察",
                        "range": "14.55 - 15.0",
                        "trigger": "量价继续配合。",
                        "watch": "观察强势是否延续。",
                    }
                ],
            },
        },
    )

    cfg = {
        "llm_enabled": True,
        "default_provider": "openai_compatible",
        "providers": {
            "openai_compatible": {
                "enabled": True,
                "type": "openai_compatible",
                "base_url": "http://example.invalid/v1",
                "model": "test-model",
                "api_key": "test-key",
            }
        },
    }
    called: list[bool] = []
    captured_messages: list[list[dict[str, str]]] = []

    def fake_chat_completion(messages, config=None, *, network=False):
        called.append(bool(network))
        captured_messages.append(messages)
        return build_llm_result(
            ok=True,
            status="ok",
            provider="openai_compatible",
            report="基于本地证据：候选池需要先检查数据新鲜度和策略匹配。",
            llm_used=True,
        )

    monkeypatch.setattr(llm_config, "load_llm_config", lambda: cfg)
    monkeypatch.setattr(openai_compatible_provider, "chat_completion", fake_chat_completion)

    blocked = client.post("/api/agent/chat", json={"question": "分析今天候选池"})
    blocked_payload = blocked.get_json()
    assert blocked.status_code == 200
    assert blocked_payload["llm_used"] is False
    assert blocked_payload["status"] == "local_fallback_offline"
    assert blocked_payload["local_analysis"] is True
    assert called == []

    allowed = client.post(
        "/api/agent/chat",
        json={
            "question": "分析中钨高新是否适合进入纸面模拟池",
            "skill": "paper_simulation_decision",
            "network": True,
            "context": {
                "focused_candidate": {
                    "symbol": "000657",
                    "name": "中钨高新",
                    "entry_date": "2026-06-22",
                    "lane": "",
                },
                "ui_candidate_count": 2,
            },
        },
    )
    allowed_payload = allowed.get_json()
    assert allowed.status_code == 200
    assert allowed_payload["ok"] is True
    assert allowed_payload["llm_used"] is True
    assert called == [True]
    joined_messages = "\n".join(message["content"] for message in captured_messages[-1])
    assert "focused_candidate" in joined_messages
    assert "中钨高新" in joined_messages
    assert "MACD" in joined_messages or "macd" in joined_messages
    assert "公司公告显示订单线索增强" in joined_messages
    assert "有色金属" in joined_messages
    assert "偏强进攻观察" in joined_messages
    assert "paper_research_contract" in joined_messages
    assert "Research Only" in joined_messages


def test_strategy_screening_background_endpoint_defaults_to_bounded_cached_run(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    task_path = tmp_path / "strategy_screening_task.json"
    monkeypatch.setattr(api_server, "STRATEGY_SCREENING_TASK_JSON", task_path)
    api_server.STRATEGY_SCREENING_STATE.update({"running": False, "status": "idle", "request_id": "", "started_at": "", "message_for_ui": ""})

    class FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=False):
            captured["target"] = target
            captured["kwargs"] = kwargs or {}
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(api_server, "Thread", FakeThread)

    response = app.test_client().post("/api/strategy-screening/run", json={})
    payload = response.get_json()

    assert response.status_code == 202
    assert payload["status"] == "running"
    assert payload["params"]["max_symbols"] == 600
    assert payload["params"]["max_strategies"] == 8
    assert payload["params"]["feature_cache"] == "true"
    assert payload["params"]["full_scan_requested"] is False
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["kwargs"]["params"]["max_symbols"] == 600
    api_server.STRATEGY_SCREENING_STATE.update({"running": False, "status": "idle", "request_id": "", "started_at": "", "message_for_ui": ""})


def test_strategy_screening_worker_writes_status_without_blocking_page(monkeypatch, tmp_path) -> None:
    from scripts import run_strategy_screening_funnel as funnel
    from scripts.server.routes.strategy_screening import run_strategy_screening_worker

    task_path = tmp_path / "strategy_screening_task.json"
    monkeypatch.setattr(api_server, "STRATEGY_SCREENING_TASK_JSON", task_path)
    api_server.STRATEGY_SCREENING_STATE.update({"running": False, "status": "idle", "request_id": "", "started_at": "", "message_for_ui": ""})

    def fake_run_screening(args):
        assert args.max_symbols == 40
        assert args.max_strategies == 3
        assert args.feature_cache == "true"
        return {
            "ok": True,
            "status": "passed",
            "task_type": "strategy_screening_funnel",
            "data_quality_summary": {
                "daily_csv_available": 5000,
                "scanned_csv": 40,
                "usable_csv": 38,
                "max_symbols": 40,
            },
            "performance": {
                "elapsed_seconds": 1.25,
                "max_symbols": 40,
                "strategies_tested": 3,
                "feature_cache": {"status": "hit"},
            },
            "leaderboard": [{"strategy": "ma_breakout", "status": "warning", "annualized_return_net": 0.1}],
            "candidate_pool": [{}],
            "watchlist": [],
            "warnings": [],
        }

    def fake_write_outputs(payload, output_dir, report_dir):
        payload = dict(payload)
        payload["output_paths"] = {"json": str(output_dir / "strategy_screening_funnel_latest.json"), "markdown": str(report_dir / "strategy_screening_funnel_latest.md")}
        return payload

    monkeypatch.setattr(funnel, "run_screening", fake_run_screening)
    monkeypatch.setattr(funnel, "write_outputs", fake_write_outputs)

    run_strategy_screening_worker(
        api_server._STRATEGY_SCREENING_ROUTE_DEPS,
        request_id="test-run",
        params={"max_symbols": 40, "max_strategies": 3, "feature_cache": "true", "cache_dir": str(tmp_path / "cache")},
    )
    status = json.loads(task_path.read_text(encoding="utf-8"))

    assert status["status"] == "passed"
    assert status["running"] is False
    assert status["latest_result"]["scanned_csv"] == 40
    assert status["latest_result"]["daily_csv_available"] == 5000
    assert status["latest_result"]["feature_cache_status"] == "hit"
    assert status["latest_result"]["strategies_tested"] == 3
    assert api_server.STRATEGY_SCREENING_STATE["running"] is False


def test_strategy_screening_ui_is_wired_to_background_endpoint() -> None:
    template = (Path("app") / "templates" / "daily_research.html").read_text(encoding="utf-8")
    script = (Path("app") / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")

    assert "strategyScreeningPanel" in template
    assert "runStrategyScreeningButton" in template
    assert "strategyScreeningMaxSymbols" in template
    assert "Research Only" in template
    assert "No Broker Route" in template

    assert "/api/strategy-screening/run" in script
    assert "/api/strategy-screening/status" in script
    assert "renderStrategyScreeningStatus" in script
    assert "scheduleStrategyScreeningPoll" in script
    assert "showModuleView(initialTargetId, false, false, true)" in script


def test_daily_focus_copy_uses_research_candidate_boundary() -> None:
    template = (Path("app") / "templates" / "daily_research.html").read_text(encoding="utf-8")
    script = (Path("app") / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")

    active_frontend = template + "\n" + script
    forbidden_copy = "每日" + "\u8350\u80a1"
    assert forbidden_copy not in active_frontend
    assert "每日研究候选" in template
    assert '"daily-focus-section": "每日研究候选"' in script
    assert "Research Only" in template
    assert "No Broker Route" in template
    assert "Not Investment Advice" in template
    assert "Not Position Advice" in template


def test_realtime_observation_layer_is_separate_from_daily_research(monkeypatch) -> None:
    _install_self_contained_candidate_fixture(monkeypatch)
    client = app.test_client()
    market = client.get("/api/realtime/market").get_json()
    _assert_clean_payload(market)
    assert market["temporary_quote_only"] is True
    assert "不覆盖正式完整日线" in market["boundary"]
    assert market["network_authorized"] is False
    assert "sector_top_gainers" in market
    assert "data_freshness_label" in market
    assert "cache_age_text" in market
    assert "last_success_at" in market
    assert "is_cache_stale" in market
    assert "is_live_lagging" in market
    assert "freshness_hint" in market

    candidates = client.get("/api/daily/candidates").get_json()
    symbol = candidates["all_candidates"][0]["symbol"]

    search = client.get(f"/api/realtime/stock-search?q={symbol}").get_json()
    _assert_clean_payload(search)
    assert search["count"] >= 1
    assert any(row["symbol"] == symbol for row in search["rows"])
    assert "正式候选池" in search["message"]

    detail = client.get(f"/api/realtime/stock/{symbol}?refresh_quote=0&refresh_daily=0").get_json()
    _assert_clean_payload(detail)
    assert detail["temporary_quote_only"] is True
    assert detail["daily_layer"]["message"].startswith("正式研究层")
    assert "不是价格预测" in detail["observation_analysis"]["disclaimer"]
    assert len(detail["chart_data"]["kline_data"]) >= 60
    assert "不覆盖正式日线" in detail["research_only"]
    ref = detail["research_reference"]
    assert "reference_1d_pct" in ref["price_band"]
    assert "reference_1d" in ref["price_band"]
    assert "scenario_projection" in ref
    assert "news_summary" in detail
    assert "industry_comparison" in detail
    assert "tracking_vs_benchmark" in detail
    assert detail["tracking_vs_benchmark"]["available"] is False
    assert "尚未加入正式候选池" in detail["tracking_vs_benchmark"]["hint"]


def test_realtime_provider_fallback_is_not_reported_as_current_failure() -> None:
    background = {
        "spot": {
            "ok": True,
            "status": "refreshed",
            "errors": ["eastmoney.spot_direct: 502 Server Error"],
        },
        "sector": {
            "ok": False,
            "message_for_ui": "板块实时刷新失败：provider timeout after 30s",
        },
    }

    assert api_server.realtime_provider_error_summary(background, include_sector=False) == ""
    note = api_server.realtime_provider_note(background, sector_cache_usable=True)
    assert "备用源" in note
    assert "板块缓存" in note


def test_after_close_snapshot_is_reviewable_not_live_lagging() -> None:
    frame = pd.DataFrame({"quote_time": ["2026-06-15 15:00:00"], "pct_chg": [1.2]})
    state = api_server.realtime_freshness_state(
        session="after_close",
        frame=frame,
        age_seconds=7200,
        updated_at="2026-06-15T21:15:33",
        now=datetime(2026, 6, 15, 21, 30, 0),
    )

    assert state["data_freshness_label"] == "收盘后复盘快照"
    assert state["is_cache_stale"] is False
    assert state["is_live_lagging"] is False
    assert "不会继续跳动" in state["freshness_hint"]


def test_read_theme_map_prefers_formal_map_over_import_draft(monkeypatch, tmp_path) -> None:
    universe_dir = tmp_path / "universe"
    universe_dir.mkdir()
    (universe_dir / "stock_theme_map.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
        "000711,ST京蓝,环境治理,环境治理,节能环保,节能环保,环境治理,eastmoney,2026-06-15\n",
        encoding="utf-8-sig",
    )
    (universe_dir / "stock_theme_map_import.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at,notes\n"
        "000711,ST京蓝,未知板块,未知行业,未知概念,未知题材,未知题材,pending_manual,2026-06-15,old draft\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(api_server, "UNIVERSE_DIR", universe_dir)

    frame = api_server.read_theme_map()
    row = frame[frame["symbol"].astype(str).str.zfill(6) == "000711"].iloc[0]

    assert row["primary_theme"] == "环境治理"
    assert row["industry"] == "环境治理"


def test_read_theme_map_skips_bad_formal_rows_with_visible_warning(monkeypatch, tmp_path) -> None:
    universe_dir = tmp_path / "universe"
    universe_dir.mkdir()
    (universe_dir / "stock_theme_map.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
        "000711,ST京蓝,环境治理,环境治理,节能环保,节能环保,环境治理,eastmoney,2026-06-15\n"
        "bad-row,坏行,未知,未知,未知,未知,未知,manual,2026-06-15,extra-field\n",
        encoding="utf-8-sig",
    )
    (universe_dir / "stock_theme_map_import.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at,notes\n"
        "000711,ST京蓝,未知板块,未知行业,未知概念,未知题材,未知题材,pending_manual,2026-06-15,old draft\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setitem(api_server.read_theme_map.__globals__, "UNIVERSE_DIR", universe_dir)

    frame = api_server.read_theme_map()
    row = frame[frame["symbol"].astype(str).str.zfill(6) == "000711"].iloc[0]

    assert row["primary_theme"] == "环境治理"
    assert any("csv_bad_lines_skipped" in item for item in frame.attrs["warnings"])


def test_load_limit_up_theme_map_skips_bad_rows_with_visible_warning(monkeypatch, tmp_path, caplog) -> None:
    universe_dir = tmp_path / "universe"
    universe_dir.mkdir()
    (universe_dir / "stock_theme_map.csv").write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at\n"
        "000711,ST浜摑,鐜娌荤悊,鐜娌荤悊,鑺傝兘鐜繚,鑺傝兘鐜繚,鐜娌荤悊,eastmoney,2026-06-15\n"
        "bad-row,鍧忚,鏈煡,鏈煡,鏈煡,鏈煡,鏈煡,manual,2026-06-15,extra-field\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(api_server, "UNIVERSE_DIR", universe_dir)

    with caplog.at_level("WARNING", logger="quant_api"):
        mapping = api_server.load_limit_up_theme_map()

    assert mapping["000711"]["primary_theme"] == "鐜娌荤悊"
    assert any("csv_bad_lines_skipped" in item for item in caplog.messages)


def test_tracking_vs_benchmark_degrades_on_bad_stock_or_benchmark_data(monkeypatch, tmp_path) -> None:
    stock = pd.DataFrame(
        {
            "date": pd.date_range("2026-06-10", periods=3),
            "close": [0, 10.5, 10.8],
        }
    )
    bad_stock = api_server.build_tracking_vs_benchmark(stock, "2026-06-10")
    assert bad_stock["available"] is False
    assert "收盘价无效" in bad_stock["hint"]

    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    monkeypatch.setattr(api_server, "BENCHMARK_DIR", benchmark_dir)
    ok_stock = pd.DataFrame(
        {
            "date": pd.date_range("2026-06-10", periods=3),
            "close": [10.0, 10.5, 10.8],
        }
    )
    missing_benchmark = api_server.build_tracking_vs_benchmark(ok_stock, "2026-06-10")
    assert missing_benchmark["available"] is True
    assert missing_benchmark["benchmark_return_pct"] is None
    assert "缺少 data/benchmark/sh000001.csv" in missing_benchmark["benchmark_hint"]


def test_limit_up_review_payload_groups_themes_and_quant_intersections(monkeypatch) -> None:
    rows = [
        {
            "symbol": "000802",
            "name": "北京文化",
            "primary_theme": "影视传媒",
            "industry": "文化传媒",
            "consecutive_boards": 2,
            "first_seal_time": "09:45:12",
            "seal_amount": 250000000,
            "reason_reference": "进入当日涨停池；题材线索为“影视传媒”。",
        },
        {
            "symbol": "600000",
            "name": "样本银行",
            "primary_theme": "银行",
            "industry": "银行",
            "consecutive_boards": 1,
            "first_seal_time": "10:12:00",
            "seal_amount": 120000000,
            "reason_reference": "进入当日涨停池；题材线索为“银行”。",
        },
    ]
    monkeypatch.setattr(
        api_server,
        "candidate_rows",
        lambda limit=18: [
            {
                "symbol": "000802",
                "name": "北京文化",
                "strategy_lane": "打板复核",
                "tracking": {"status_label": "阶段符合观察假设", "status_tone": "ok"},
            }
        ],
    )
    monkeypatch.setattr(
        api_server,
        "cached_news_items_for_theme",
        lambda symbol, limit=5: [
            {
                "title": "北京文化: 短剧与影视版权业务进展公告",
                "content": "影视版权",
                "published": "2026-06-15",
                "source": "东方财富公告",
                "url": "",
            }
        ]
        if symbol == "000802"
        else [],
    )
    monkeypatch.setattr(api_server, "load_limit_up_theme_map", lambda: {})

    payload = api_server.build_limit_up_review_payload(
        date_text="2026-06-15",
        rows=rows,
        broken_count=1,
        source="test_provider",
    )

    _assert_clean_payload(payload)
    assert payload["limit_up_count"] == 2
    assert payload["broken_count"] == 1
    assert payload["seal_rate"] == 66.67
    assert payload["connected_count"] == 1
    assert payload["theme_summary"][0]["primary_theme"] in {"文化传媒", "影视传媒", "银行"}
    assert payload["attribution_summary"]["traceable_count"] == 2
    assert payload["attribution_summary"]["news_evidence_count"] == 1
    first_row = next(row for row in payload["rows"] if row["symbol"] == "000802")
    assert first_row["has_news_evidence"] is True
    assert any(source["source_type"] == "announcement_keyword" for source in first_row["attribution_sources"])
    assert "公告/新闻" in first_row["attribution_summary"]
    assert payload["quant_intersections"][0]["symbol"] == "000802"
    assert "真实可成交" in payload["boundary"]
    digest = api_server.limit_up_review_daily_digest(payload)
    assert digest["limit_up_count"] == 2
    assert digest["connected_count"] == 1
    assert digest["top_themes"]
    assert digest["quant_intersections"][0]["symbol"] == "000802"
    assert digest["attribution_summary"]["news_evidence_count"] == 1
    assert "涨停池 2 只" in digest["report_line"]
    assert "公告/新闻 1 只" in digest["report_line"]
    assert "买入" not in _json_text(payload)
    assert "卖出" not in _json_text(payload)
    assert "仓位" not in _json_text(payload)
    assert "买入" not in _json_text(digest)
    assert "卖出" not in _json_text(digest)
    assert "仓位" not in _json_text(digest)


def test_limit_up_cached_provider_label_is_displayed_as_industry_clue() -> None:
    rows = [
        {
            "symbol": "000001",
            "name": "平安银行",
            "industry": "银行",
            "primary_theme": "银行",
            "attribution_sources": [
                {
                    "source_type": "provider_industry",
                    "source_label": "provider行业",
                    "source_name": "涨停池 provider 所属行业",
                    "theme": "银行",
                    "evidence_text": "涨停池 provider 返回所属行业：银行。",
                }
            ],
            "attribution_summary": "provider行业:银行",
            "reason_reference": "归因证据：provider行业:银行。",
        }
    ]

    cleaned = api_server.ensure_limit_up_rows_attribution(rows)
    text = _json_text(cleaned)

    assert cleaned[0]["attribution_sources"][0]["source_label"] == "行业线索"
    assert "行业线索:银行" in cleaned[0]["attribution_summary"]
    assert "provider行业" not in text
    assert "provider 所属行业" not in text


def test_limit_up_refresh_requires_explicit_network_confirmation() -> None:
    response = app.test_client().post("/api/limit-up/refresh", json={})
    payload = response.get_json()

    assert response.status_code == 400
    assert payload["status"] == "network_not_confirmed"
    assert "显式 network=true" in payload["message_for_ui"]


def test_theme_map_refresh_requires_explicit_network_confirmation() -> None:
    response = app.test_client().post("/api/theme-map/refresh", json={"source": "auto"})
    payload = response.get_json()

    assert response.status_code == 400
    assert payload["status"] == "network_not_confirmed"
    assert "network=true" in payload["message_for_ui"]


def test_theme_map_local_refresh_rebuilds_limit_up_attribution(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_update_theme_map(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "ok": True,
            "source": kwargs.get("source"),
            "mapped_count": 2,
            "pending_count": 1,
            "message": "已补全 2 只股票板块/题材，仍有 1 只待补。",
            "provider_results": [{"provider": "local", "status": "ok", "mapped": 2}],
        }

    monkeypatch.setattr(api_server, "update_theme_map", fake_update_theme_map)
    monkeypatch.setattr(
        api_server,
        "theme_map_status",
        lambda path: {
            "ok": True,
            "exists": True,
            "path": str(path),
            "total_rows": 3,
            "mapped_count": 2,
            "pending_count": 1,
            "unknown_count": 1,
            "manual_count": 0,
            "manual_import_count": 0,
            "local_valid_count": 2,
            "akshare_count": 0,
            "last_update_source": "local",
            "updated_at": "2026-06-15 12:00:00",
            "import_path": "data/universe/stock_theme_map_import.csv",
        },
    )
    monkeypatch.setattr(
        api_server,
        "validate_theme_map_for_current_candidates",
        lambda path: {
            "ok": True,
            "candidate_count": 2,
            "mapped_current_candidates": 1,
            "unknown_current_candidates": 1,
            "reason": "当前候选仍有 1 只待补。",
        },
    )
    monkeypatch.setattr(
        api_server,
        "rebuild_limit_up_review_attribution_cache",
        lambda: {"ok": True, "cache_enriched_after_theme_map_update": True},
    )

    response = app.test_client().post("/api/theme-map/refresh", json={"source": "local"})
    payload = response.get_json()

    assert response.status_code == 200
    assert captured["source"] == "local"
    assert captured["only_low_price"] is True
    assert payload["theme_map_status"]["mapped_count"] == 2
    assert payload["limit_up_review"]["cache_enriched_after_theme_map_update"] is True
    assert "Research Only" in payload["boundary"]


def test_theme_map_import_uses_import_symbols_and_keeps_pending_unknown(monkeypatch, tmp_path) -> None:
    import_csv = tmp_path / "stock_theme_map_import.csv"
    output_csv = tmp_path / "stock_theme_map.csv"
    pending_csv = tmp_path / "stock_theme_map_pending.csv"
    import_csv.write_text(
        "symbol,name,sector,industry,concept_tags,theme_tags,primary_theme,source,updated_at,notes\n"
        "600744,华银电力,公用事业,电力,电力;绿色电力,电力;绿色电力,绿色电力,seed,2026-06-15,本地种子映射\n"
        "300000,待补样本,,,,待分类,待分类,akshare_sina_spot,2026-06-15,旧草稿待分类\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(theme_map_updater, "LOW_PRICE_SYMBOLS_CSV", tmp_path / "missing_low_price.csv")

    result = theme_map_updater.update_theme_map(
        source="import",
        input_path=import_csv,
        output_path=output_csv,
        pending_path=pending_csv,
    )

    assert result["ok"] is True
    assert result["total"] == 2
    assert result["mapped_count"] == 1
    assert result["pending_count"] == 1
    imported = pd.read_csv(output_csv, dtype=str, encoding="utf-8-sig")
    assert set(imported["symbol"].astype(str).str.zfill(6)) == {"600744", "300000"}
    mapped = imported[imported["symbol"].astype(str).str.zfill(6) == "600744"].iloc[0]
    assert mapped["primary_theme"] == "绿色电力"
    pending = imported[imported["symbol"].astype(str).str.zfill(6) == "300000"].iloc[0]
    assert pending["source"] == "pending"


def test_theme_map_draft_collects_visible_candidate_ledger_and_cache(monkeypatch, tmp_path) -> None:
    ledger_csv = tmp_path / "candidate_ledger.csv"
    cache_json = tmp_path / "limit_up_review_latest.json"
    output_csv = tmp_path / "results" / "theme_map_drafts" / "stock_theme_map_import.csv"
    empty_csv = tmp_path / "missing.csv"
    missing_json = tmp_path / "missing.json"
    ledger_csv.write_text(
        "entry_date,ts_code,stock_name,strategy_type,entry_price,entry_signal,data_source\n"
        "2026-06-15,000802.SZ,北京文化,long_term,4.82,趋势修复研究信号,proxy\n"
        "2026-06-15,000510.SZ,新金路,da_ban,21.31,量价突破研究信号,proxy\n",
        encoding="utf-8-sig",
    )
    cache_json.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "symbol": "000510",
                        "name": "新金路",
                        "sector": "化学原料",
                        "industry": "化学原料",
                        "primary_theme": "化学原料",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(theme_map_import_draft, "CANDIDATE_LEDGER_CSV", ledger_csv)
    monkeypatch.setattr(theme_map_import_draft, "LIMIT_UP_REVIEW_CACHE", cache_json)
    monkeypatch.setattr(theme_map_import_draft, "STOCK_THEME_MAP_CSV", empty_csv)
    monkeypatch.setattr(theme_map_import_draft, "STOCK_THEME_MAP_PENDING_CSV", empty_csv)
    monkeypatch.setattr(theme_map_import_draft, "LOW_PRICE_SYMBOLS_CSV", empty_csv)
    monkeypatch.setattr(theme_map_import_draft, "SPOT_SNAPSHOT_LATEST_CSV", empty_csv)
    monkeypatch.setattr(theme_map_import_draft, "AUTO_STRATEGY_LATEST_JSON", missing_json)
    monkeypatch.setattr(theme_map_import_draft, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", missing_json)
    monkeypatch.setattr(theme_map_import_draft, "AUTO_PAPER_PORTFOLIO_STATE_JSON", missing_json)
    monkeypatch.setattr(theme_map_import_draft, "PROJECT_ROOT", tmp_path)

    result = theme_map_import_draft.generate_import_draft(source="current", overwrite=True, output_path=output_csv)

    assert result["total"] == 2
    assert result["filled_count"] == 1
    assert result["pending_manual_count"] == 1
    rows = pd.read_csv(output_csv, dtype=str, encoding="utf-8-sig")
    mapped = rows[rows["symbol"].astype(str).str.zfill(6) == "000510"].iloc[0]
    pending = rows[rows["symbol"].astype(str).str.zfill(6) == "000802"].iloc[0]
    assert mapped["source"] == "limit_up_review_cache"
    assert mapped["primary_theme"] == "化学原料"
    assert pending["source"] == "pending_manual"


def test_theme_map_draft_skips_bad_candidate_ledger_rows_with_warning(monkeypatch, tmp_path, caplog) -> None:
    ledger_csv = tmp_path / "candidate_ledger.csv"
    ledger_csv.write_text(
        "entry_date,ts_code,stock_name,strategy_type,entry_price,entry_signal,data_source\n"
        "2026-06-15,000802.SZ,鍖椾含鏂囧寲,long_term,4.82,signal,proxy\n"
        "bad,row,with,too,many,columns,for,this,ledger,file\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(theme_map_import_draft, "CANDIDATE_LEDGER_CSV", ledger_csv)

    with caplog.at_level("WARNING", logger="quantdesk.theme_map_import_draft"):
        rows = theme_map_import_draft.collect_dashboard_candidate_symbols(limit=18)

    assert set(rows) == {"000802"}
    assert any("csv_bad_lines_skipped" in item for item in caplog.messages)


def test_startup_status_preserves_run_warning_despite_current_candidate_health(monkeypatch) -> None:
    monkeypatch.setitem(api_server.STARTUP_REFRESH_STATE, "running", False)
    monkeypatch.setattr(
        api_server,
        "read_json_file",
        lambda path: {
            "status": "completed_with_warnings",
            "target_date": "2026-06-15",
            "latest_local_date_after": "2026-06-12",
            "stale_count_after": 1,
            "failed_symbols": 1,
            "startup_update_attempted": True,
            "provider_network_authorized": True,
        },
    )
    monkeypatch.setattr(api_server, "candidate_rows", lambda limit=18: [{"symbol": "000802"}])
    monkeypatch.setattr(
        api_server,
        "data_gap_summary",
        lambda rows: {
            "candidate_count": 1,
            "fresh_count": 1,
            "gap_count": 0,
            "field_gap_count": 0,
            "stale_count": 0,
            "missing_count": 0,
            "message": "候选价格日线和关键字段可读取。",
        },
    )

    payload = app.test_client().get("/api/startup/update-status").get_json()

    assert payload["running"] is False
    assert payload["current_view_override"] is False
    assert payload["status"] == "completed_with_warnings"
    assert payload["failed_symbols"] == 1
    assert payload["latest_local_date_after"] == "2026-06-12"
    assert payload["startup_update_explanation"]["status"] == "completed_with_warnings"


def test_startup_update_explanation_marks_stale_generation(monkeypatch) -> None:
    monkeypatch.setattr(
        api_server,
        "expected_settlement_date",
        lambda now: pd.Timestamp("2026-06-23").date(),
    )
    payload = {
        "status": "completed_with_warnings",
        "target_date": "2026-06-22",
        "startup_update_attempted": True,
        "provider_network_authorized": True,
        "candidate_generation": api_server.generation_status_for_ui(
            {"status": "completed", "signal_date": "2026-06-22", "candidate_count": 8}
        ),
        "current_candidate_gap_summary": {
            "candidate_count": 8,
            "gap_count": 0,
            "field_gap_count": 0,
            "target_date": "2026-06-23",
        },
    }

    explanation = api_server.startup_update_explanation(payload)

    assert explanation["reason_code"] == "recommendation_stale"
    assert explanation["reason_label"] == "研究候选批次滞后"
    # The attempt keeps its target even when today's candidate view is newer.
    assert explanation["target_date"] == "2026-06-22"
    assert explanation["recommendation_signal_date"] == "2026-06-22"
    assert "重新生成每日研究候选" in explanation["next_action"]


def test_startup_update_explanation_separates_quality_warning_from_daily_gap() -> None:
    payload = {
        "status": "completed_with_warnings",
        "startup_update_attempted": True,
        "provider_network_authorized": True,
        "candidate_generation": {
            "signal_date": "2026-06-23",
            "target_date": "2026-06-23",
            "freshness_status": "current",
        },
        "current_candidate_gap_summary": {
            "candidate_count": 7,
            "gap_count": 1,
            "stale_count": 0,
            "missing_count": 0,
            "field_gap_count": 0,
            "quality_warning_count": 1,
            "price_layer_ready": True,
            "message": "价格/K线已更新；1 只存在异常历史行过滤等质量提示，指标已基于剩余有效价格计算。",
            "target_date": "2026-06-23",
        },
        "current_candidate_latest_date_min": "2026-06-23",
    }

    explanation = api_server.startup_update_explanation(payload)

    assert explanation["reason_code"] == "quality_warning"
    assert explanation["reason_label"] == "价格日线已更新，存在质量提示"
    assert "继续研究今日重点" in explanation["next_action"]
    assert "候选日线仍有缺口" not in explanation["reason_label"]


def test_startup_status_recovers_stale_running_file_when_worker_is_not_running(monkeypatch) -> None:
    monkeypatch.setitem(api_server.STARTUP_REFRESH_STATE, "running", False)
    monkeypatch.setitem(api_server.STARTUP_REFRESH_STATE, "request_id", "")
    monkeypatch.setattr(
        api_server,
        "read_json_file",
        lambda path: {
            "task_id": "startup_data_update",
            "status": "running",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "update_result": {"status": "failed"},
            "message": "旧的运行中状态文件。",
        },
    )
    monkeypatch.setattr(api_server, "candidate_rows", lambda limit=18: [])

    payload = app.test_client().get("/api/startup/update-status").get_json()

    assert payload["running"] is False
    assert payload["status"] != "running"


def test_startup_status_recovers_stuck_running_state_from_terminal_payload(monkeypatch) -> None:
    monkeypatch.setitem(api_server.STARTUP_REFRESH_STATE, "running", True)
    monkeypatch.setitem(api_server.STARTUP_REFRESH_STATE, "started_at", "2026-06-24T16:51:11")
    monkeypatch.setitem(api_server.STARTUP_REFRESH_STATE, "request_id", "2026-06-24T16:51:11")
    monkeypatch.setattr(
        api_server,
        "read_json_file",
        lambda path: {
            "task_id": "startup_data_update",
            "status": "completed_with_warnings",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "updated_at": "2026-06-24T16:58:05",
            "target_date": "2026-06-24",
            "latest_local_date_after": "2026-06-23",
            "updated_symbols": 78,
            "failed_symbols": 2,
            "stale_count_after": 2,
            "candidate_generation": {"status": "completed", "signal_date": "2026-06-24", "candidate_count": 7},
        },
    )
    monkeypatch.setattr(api_server, "candidate_rows", lambda limit=18: [])

    payload = app.test_client().get("/api/startup/update-status").get_json()

    assert payload["running"] is False
    assert payload["status"] == "completed_with_warnings"
    assert payload["startup_update_explanation"]["status"] == "completed_with_warnings"


def test_startup_update_explanation_summarizes_partial_provider_refresh() -> None:
    payload = {
        "status": "completed_with_warnings",
        "startup_update_attempted": True,
        "provider_network_authorized": True,
        "target_date": "2026-06-24",
        "latest_local_date_after": "2026-06-23",
        "updated_symbols": 78,
        "failed_symbols": 2,
        "stale_count_after": 2,
        "candidate_generation": {
            "status": "completed",
            "signal_date": "2026-06-24",
            "target_date": "2026-06-24",
            "freshness_status": "current",
        },
        "current_candidate_gap_summary": {
            "candidate_count": 18,
            "gap_count": 2,
            "stale_count": 2,
            "missing_count": 0,
            "field_gap_count": 0,
            "target_date": "2026-06-24",
        },
        "update_result": {
            "target_date": "2026-06-24",
            "latest_local_date_after": "2026-06-23",
            "updated_symbols": 78,
            "failed_symbols": 2,
            "stale_count_after": 2,
            "provider_results": [
                {
                    "provider": "tencent_daily",
                    "status": "warning",
                    "updated": 78,
                    "failed": 2,
                    "latest_date": "2026-06-24",
                    "failed_symbol_list": ["000032", "000582"],
                    "failed_reason": "ssl timeout",
                }
            ],
        },
    }

    explanation = api_server.startup_update_explanation(payload)

    assert explanation["reason_code"] == "provider_partial_daily_gap"
    assert explanation["reason_label"] == "自动刷新已部分完成"
    assert "tencent_daily 成功更新 78 只" in explanation["summary"]
    assert explanation["provider_diagnosis"]["failed_symbols_sample"] == ["000032", "000582"]


def test_data_gap_summary_separates_today_focus_from_observation_gaps() -> None:
    rows = [
        {
            "is_today_recommendation": True,
            "data_quality": {"tone": "ok", "status": "数据完整", "target_date": "2026-06-24", "missing_reasons": []},
        },
        {
            "is_today_recommendation": True,
            "data_quality": {"tone": "field_gap", "status": "成交额缺失", "target_date": "2026-06-24", "missing_reasons": ["成交额缺失"]},
        },
        {
            "is_today_recommendation": False,
            "data_quality": {"tone": "warn", "status": "日线滞后", "target_date": "2026-06-24", "missing_reasons": ["最新日线停在 2026-06-23"]},
        },
        {
            "is_today_recommendation": False,
            "data_quality": {"tone": "missing", "status": "缺少日线", "target_date": "2026-06-24", "missing_reasons": ["缺少 CSV"]},
        },
    ]

    summary = api_server.data_gap_summary(rows)

    assert summary["today_price_layer_ready"] is True
    assert summary["today_count"] == 2
    assert summary["observation_count"] == 2
    assert summary["today_stale_count"] == 0
    assert summary["observation_stale_count"] == 1
    assert summary["observation_missing_count"] == 1
    assert "今日重点价格/K线已覆盖目标日" in summary["message"]


def test_explicit_startup_refresh_delegates_without_paper_sync(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    sync_calls: list[dict[str, object]] = []
    written: list[dict[str, object]] = []

    def fake_run_startup_data_update(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {
            "task_id": "startup_data_update",
            "status": "completed",
            "startup_update_attempted": True,
            "provider_network_authorized": True,
            "network": True,
            "dry_run": False,
            "updated_symbols": 18,
            "failed_symbols": 0,
            "stale_count_after": 0,
            "message": "finished",
            "candidate_generation": {"candidate_count": 2, "signal_date": "2026-06-23"},
        }

    def fail_subprocess_run(*args, **kwargs):  # pragma: no cover - should never be reached
        raise AssertionError("startup refresh should not use a timeout-limited subprocess")

    def fake_sync_candidates_to_paper_simulation_pool(*, include_observation=False, max_candidates=10):
        sync_calls.append({"include_observation": include_observation, "max_candidates": max_candidates})
        return {
            "ok": True,
            "status": "synced",
            "selected_count": 2,
            "watchlist_count": 2,
            "paper_tracking_summary": {"updated_count": 2},
        }

    monkeypatch.setattr(api_server, "run_startup_data_update", fake_run_startup_data_update)
    monkeypatch.setattr(api_server, "sync_candidates_to_paper_simulation_pool", fake_sync_candidates_to_paper_simulation_pool)
    monkeypatch.setattr(api_server.subprocess, "run", fail_subprocess_run)
    monkeypatch.setattr(api_server, "_write_startup_status_payload", lambda payload: written.append(payload))

    api_server._run_startup_refresh_worker("test", request_id="refresh-test")

    assert calls
    kwargs = calls[0]["kwargs"]
    assert kwargs["enable_network"] is True
    assert kwargs.get("generate_candidates", False) is False
    assert sync_calls == []
    assert written
    assert "paper_simulation_sync" not in written[-1]
    state = api_server.startup_refresh_state_payload()
    assert state["running"] is False
    assert state["status"] == "completed"
    assert state["request_id"] == "refresh-test"


def test_candidate_rows_prioritizes_latest_generation_before_history(monkeypatch) -> None:
    ledger = pd.DataFrame(
        [
            {
                "entry_date": "2026-06-18",
                "signal_date": "2026-06-18",
                "ts_code": "000001.SZ",
                "stock_name": "Old",
                "strategy_type": "da_ban",
                "candidate_role": "daily_recommendation",
            },
            {
                "entry_date": "2026-06-22",
                "signal_date": "2026-06-22",
                "ts_code": "000002.SZ",
                "stock_name": "New",
                "strategy_type": "long_term",
                "candidate_role": "daily_recommendation",
            },
        ]
    )

    monkeypatch.setattr(api_server, "load_candidate_ledger", lambda: ledger)
    monkeypatch.setattr(api_server, "load_candidate_review_overrides", lambda: {})
    monkeypatch.setattr(
        api_server,
        "latest_generation_status",
        lambda: {"signal_date": "2026-06-22", "candidate_count": 1},
    )
    monkeypatch.setattr(api_server, "latest_generation_symbols", lambda payload=None: {"000002"})
    monkeypatch.setattr(
        api_server,
        "build_candidate",
        lambda symbol, row=None: {
            "symbol": symbol,
            "is_today_recommendation": symbol == "000002",
            "select_date": api_server.row_entry_date(row),
        },
    )
    monkeypatch.setattr(api_server, "attach_strategy_conflict", lambda candidate, related, primary: candidate)
    monkeypatch.setattr(api_server, "apply_candidate_review_state", lambda candidate, overrides: candidate)

    rows = api_server.candidate_rows(limit=2)

    assert [row["symbol"] for row in rows] == ["000002", "000001"]
    assert rows[0]["is_today_recommendation"] is True


def test_latest_generation_symbols_prefers_ledger_when_latest_csv_is_stale(monkeypatch, tmp_path) -> None:
    ledger_path = tmp_path / "candidate_ledger.csv"
    latest_csv = tmp_path / "v420_daily_stock_candidates_latest.csv"
    ledger_path.write_text(
        "\n".join(
            [
                "entry_date,signal_date,ts_code,stock_name,strategy_type,candidate_role",
                "2026-06-18,2026-06-18,000001.SZ,Old,da_ban,daily_recommendation",
                "2026-06-22,2026-06-22,000002.SZ,New,long_term,daily_recommendation",
                "2026-06-22,2026-06-22,000003.SZ,New2,da_ban,daily_recommendation",
            ]
        ),
        encoding="utf-8",
    )
    latest_csv.write_text(
        "\n".join(
            [
                "symbol,signal_date,candidate_role",
                "000608,2026-06-22,daily_recommendation",
                "000711,2026-06-22,daily_recommendation",
            ]
        ),
        encoding="utf-8",
    )
    api_server._LATEST_GENERATION_SYMBOL_CACHE.clear()
    api_server._LATEST_GENERATION_SYMBOL_CACHE.update(
        {"csv_mtime": None, "ledger_mtime": None, "signal_date": "", "symbols": set()}
    )
    monkeypatch.setattr(api_server, "TRACKING_DIR", tmp_path)
    monkeypatch.setattr(api_server, "DAILY_CANDIDATE_LATEST_CSV", latest_csv)

    symbols = api_server.latest_generation_symbols({"signal_date": "2026-06-22"})

    assert symbols == {"000002", "000003"}


def test_generation_status_marks_recommendation_batch_stale(monkeypatch) -> None:
    monkeypatch.setattr(api_server, "expected_settlement_date", lambda now: pd.Timestamp("2026-06-23").date())

    payload = api_server.generation_status_for_ui(
        {
            "status": "completed",
            "signal_date": "2026-06-22",
            "candidate_count": 8,
            "strategy_counts": {"long_term": 4, "da_ban": 4},
        }
    )

    assert payload["target_date"] == "2026-06-23"
    assert payload["freshness_status"] == "stale"
    assert payload["freshness_label"] == "研究候选批次滞后"
    assert payload["needs_refresh"] is True
    assert "最新研究候选批次停在 2026-06-22" in payload["freshness_message"]


def test_candidate_rows_uses_ledger_today_symbols_when_latest_csv_is_stale(monkeypatch, tmp_path) -> None:
    latest_csv = tmp_path / "v420_daily_stock_candidates_latest.csv"
    latest_csv.write_text(
        "\n".join(
            [
                "symbol,signal_date,candidate_role",
                "000608,2026-06-22,daily_recommendation",
                "000711,2026-06-22,daily_recommendation",
            ]
        ),
        encoding="utf-8",
    )
    ledger = pd.DataFrame(
        [
            {
                "entry_date": "2026-06-18",
                "signal_date": "2026-06-18",
                "ts_code": "000001.SZ",
                "stock_name": "Old",
                "strategy_type": "da_ban",
                "candidate_role": "daily_recommendation",
            },
            {
                "entry_date": "2026-06-22",
                "signal_date": "2026-06-22",
                "ts_code": "000002.SZ",
                "stock_name": "New",
                "strategy_type": "long_term",
                "candidate_role": "daily_recommendation",
            },
        ]
    )
    api_server._LATEST_GENERATION_SYMBOL_CACHE.clear()
    api_server._LATEST_GENERATION_SYMBOL_CACHE.update(
        {"csv_mtime": None, "ledger_mtime": None, "signal_date": "", "symbols": set()}
    )
    monkeypatch.setattr(api_server, "DAILY_CANDIDATE_LATEST_CSV", latest_csv)
    monkeypatch.setattr(api_server, "load_candidate_ledger", lambda: ledger)
    monkeypatch.setattr(api_server, "load_candidate_review_overrides", lambda: {})
    monkeypatch.setattr(
        api_server,
        "latest_generation_status",
        lambda: {"signal_date": "2026-06-22", "candidate_count": 1},
    )
    monkeypatch.setattr(
        api_server,
        "build_candidate",
        lambda symbol, row=None: {
            "symbol": symbol,
            "is_today_recommendation": row is not None and api_server.row_signal_date(row) == "2026-06-22",
            "select_date": api_server.row_entry_date(row),
        },
    )
    monkeypatch.setattr(api_server, "attach_strategy_conflict", lambda candidate, related, primary: candidate)
    monkeypatch.setattr(api_server, "apply_candidate_review_state", lambda candidate, overrides: candidate)

    rows = api_server.candidate_rows(limit=2)

    assert [row["symbol"] for row in rows] == ["000002", "000001"]
    assert rows[0]["is_today_recommendation"] is True


def test_benchmark_index_without_amount_is_labeled_source_not_connected(monkeypatch, tmp_path) -> None:
    benchmark_dir = tmp_path / "data" / "benchmark"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "hs300.csv").write_text(
        "date,close,pct_chg\n2026-06-15,6123.45,1.23\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "BENCHMARK_DIR", benchmark_dir)

    index = api_server.load_benchmark_index("hs300")

    assert index["amount"] == "源未接入"
    assert index["amount_status"] == "source_not_connected"
    assert "指数成交额源暂未接入" in index["hint"]
    assert "市场成交额请看本地样本成交额" in index["hint"]


def test_benchmark_index_skips_bad_rows_with_visible_warning(monkeypatch, tmp_path) -> None:
    benchmark_dir = tmp_path / "data" / "benchmark"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "hs300.csv").write_text(
        "date,close,pct_chg,amount\n"
        "2026-06-14,6000,0.1,100000000\n"
        "bad-row,6100,0.2,100000000,extra-field\n"
        "2026-06-15,6123.45,1.23,200000000\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(api_server.load_benchmark_index.__globals__, "BENCHMARK_DIR", benchmark_dir)

    index = api_server.load_benchmark_index("hs300")

    assert index["value"] == 6123.45
    assert index["change"] == 1.23
    assert any("csv_bad_lines_skipped" in item for item in index["warnings"])


def test_sector_stats_skip_bad_cache_rows_with_visible_warning(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "industry_board_latest.csv"
    cache_path.write_text(
        "name,pct_chg,amount\n"
        "半导体,3.2,10000000000\n"
        "坏行,1.0,100,extra-field\n"
        "银行,-1.5,8000000000\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(api_server.calculate_sector_stats.__globals__, "INDUSTRY_BOARD_CACHE", cache_path)

    stats = api_server.calculate_sector_stats()

    assert stats["stale"] is False
    assert stats["gainers"][0]["name"] == "半导体"
    assert stats["losers"][0]["name"] == "银行"
    assert any("csv_bad_lines_skipped" in item for item in stats["warnings"])


def test_spot_market_breadth_skips_bad_snapshot_rows_with_visible_warning(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "spot_snapshot_latest.csv"
    cache_path.write_text(
        "symbol,name,pct_chg,amount\n"
        "000001,平安银行,1.2,100000000\n"
        "000002,万科A,-0.5,200000000\n"
        "bad-row,坏行,0.1,100,extra-field\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(api_server.spot_market_breadth.__globals__, "SPOT_SNAPSHOT_CACHE", cache_path)

    breadth = api_server.spot_market_breadth()

    assert breadth["advancing_count"] == 1
    assert breadth["declining_count"] == 1
    assert breadth["sample_count"] == 2
    assert any("csv_bad_lines_skipped" in item for item in breadth["warnings"])


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def test_eastmoney_stock_profile_provider_maps_industry_and_concepts(monkeypatch) -> None:
    def fake_get(url: str, *, params: dict[str, object], headers: dict[str, str], timeout: float) -> _FakeResponse:
        assert "push2.eastmoney.com" in url
        assert params["secid"] == "0.000802"
        assert "f127" in str(params["fields"])
        return _FakeResponse(
            {
                "data": {
                    "f57": "000802",
                    "f58": "北京文化",
                    "f127": "影视院线",
                    "f129": "影视概念,文娱消费",
                }
            }
        )

    monkeypatch.setattr(theme_map_providers.requests, "get", fake_get)

    mapping, result = theme_map_providers.eastmoney_stock_profile_provider(
        [{"symbol": "000802", "name": "北京文化"}]
    )

    assert result.provider == "eastmoney_stock_profile"
    assert result.status == "ok"
    assert result.mapped == 1
    row = mapping["000802"]
    assert row["industry"] == "影视院线"
    assert row["sector"] == "影视院线"
    assert row["concept_tags"] == "影视概念;文娱消费"
    assert row["primary_theme"] == "影视院线"


def test_eastmoney_direct_spot_provider_normalizes_pages(monkeypatch) -> None:
    import requests

    calls: list[int] = []

    def fake_get(url: str, *, params: dict[str, object], headers: dict[str, str], timeout: float) -> _FakeResponse:
        calls.append(int(params["pn"]))
        diff = [
            {
                "f12": "000001",
                "f14": "平安银行",
                "f2": 11.05,
                "f3": -1.69,
                "f4": -0.19,
                "f5": 1502969,
                "f6": 1669173613.51,
                "f17": 11.21,
                "f18": 11.24,
                "f15": 11.21,
                "f16": 10.98,
                "f124": 1781506221,
            }
        ]
        if int(params["pn"]) == 2:
            diff = [
                {
                    "f12": "600000",
                    "f14": "浦发银行",
                    "f2": 9.52,
                    "f3": -1.55,
                    "f4": -0.15,
                    "f5": 848729,
                    "f6": 813128438,
                    "f17": 9.68,
                    "f18": 9.67,
                    "f15": 9.72,
                    "f16": 9.47,
                    "f124": 1781506221,
                }
            ]
        return _FakeResponse({"data": {"total": 1001, "diff": diff}})

    monkeypatch.setattr(requests, "get", fake_get)

    frame = api_server.fetch_eastmoney_spot_direct(page_size=1000, max_pages=3)

    assert calls == [1, 2]
    assert list(frame["symbol"]) == ["000001", "600000"]
    assert frame.iloc[0]["source"] == "eastmoney.spot_direct"
    assert frame.iloc[0]["pct_chg"] == -1.69


def test_spot_refresh_uses_direct_provider_without_akshare_wrapper(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(api_server, "SPOT_SNAPSHOT_CACHE", tmp_path / "spot_snapshot_latest.csv")
    monkeypatch.setattr(api_server, "SPOT_SNAPSHOT_META", tmp_path / "spot_snapshot_meta.json")
    monkeypatch.setattr(
        api_server,
        "fetch_eastmoney_spot_direct",
        lambda: pd.DataFrame(
            [
                {
                    "symbol": "000001",
                    "name": "平安银行",
                    "price": 11.05,
                    "pct_chg": -1.69,
                    "change": -0.19,
                    "volume": 1502969,
                    "amount": 1669173613.51,
                    "quote_time": "2026-06-15 14:50:21",
                    "source": "eastmoney.spot_direct",
                    "updated_at": "2026-06-15T14:50:21",
                }
            ]
        ),
    )
    monkeypatch.setattr(api_server, "fetch_sina_spot_direct", lambda: pd.DataFrame())

    result = api_server.refresh_spot_snapshot_cache(network=True, force=True)

    assert result["ok"] is True
    assert result["source"] == "eastmoney.spot_direct"
    assert result["row_count"] == 1
    assert any(item["provider"] == "akshare.full_market_wrappers" and item["status"] == "skipped_timeout_risk" for item in result["provider_chain"])
    assert any(item["provider"] == "eastmoney.spot_direct" and item["status"] == "ok" for item in result["provider_chain"])
    cached = pd.read_csv(api_server.SPOT_SNAPSHOT_CACHE)
    assert cached.iloc[0]["symbol"] == 1 or cached.iloc[0]["symbol"] == "000001"


def test_merge_daily_frames_preserves_existing_amount_when_fresh_is_blank() -> None:
    existing = pd.DataFrame(
        [
            {
                "date": "2026-06-12",
                "symbol": "000802",
                "open": 4.35,
                "high": 4.65,
                "low": 4.22,
                "close": 4.45,
                "volume": 60594609,
                "amount": 268027339,
                "adjust": "raw",
                "source": "sina_daily",
                "updated_at": "old",
            }
        ]
    )
    fresh = pd.DataFrame(
        [
            {
                "date": "2026-06-12",
                "symbol": "000802",
                "open": 4.35,
                "high": 4.65,
                "low": 4.22,
                "close": 4.45,
                "volume": 60594600,
                "amount": None,
                "adjust": "raw",
                "source": "tencent_daily",
                "updated_at": "new",
            }
        ]
    )

    merged = merge_daily_frames(existing, fresh)

    assert float(merged.iloc[-1]["amount"]) == 268027339
    assert float(merged.iloc[-1]["volume"]) == 60594600


def test_target_amount_backfill_uses_same_day_spot_snapshot(monkeypatch, tmp_path) -> None:
    daily_dir = tmp_path / "data" / "daily"
    cache_dir = tmp_path / "data" / "cache"
    daily_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (daily_dir / "000802.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,source,updated_at,adjust\n"
        "2026-06-15,000802,4.31,4.63,4.31,4.42,418214,,tencent_daily,test,raw\n",
        encoding="utf-8",
    )
    (cache_dir / "spot_snapshot_latest.csv").write_text(
        "symbol,name,price,pct_chg,volume,amount,quote_time,source,fetched_at\n"
        "000802,北京文化,4.42,-0.67,41821352,186791640,2026-06-15 15:00:00,sina.spot_direct,2026-06-15T15:05:00+08:00\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_price_update_engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(daily_price_update_engine, "DAILY_DIR", daily_dir)

    result = daily_price_update_engine._backfill_target_amount_from_spot_snapshot(["000802"], "2026-06-15")

    assert result["updated"] == 1
    repaired = pd.read_csv(daily_dir / "000802.csv")
    assert float(repaired.iloc[-1]["amount"]) == 186791640
    assert float(repaired.iloc[-1]["volume"]) == 41821352
    assert repaired.iloc[-1]["source"] == "daily_price+spot_snapshot_amount"


def test_target_amount_backfill_skips_bad_spot_snapshot_rows(monkeypatch, tmp_path) -> None:
    daily_dir = tmp_path / "data" / "daily"
    cache_dir = tmp_path / "data" / "cache"
    daily_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (daily_dir / "000802.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,source,updated_at,adjust\n"
        "2026-06-15,000802,4.31,4.63,4.31,4.42,418214,,tencent_daily,test,raw\n",
        encoding="utf-8",
    )
    (cache_dir / "spot_snapshot_latest.csv").write_text(
        "symbol,name,price,pct_chg,volume,amount,quote_time,source,fetched_at\n"
        "000802,北京文化,4.42,-0.67,41821352,186791640,2026-06-15 15:00:00,sina.spot_direct,2026-06-15T15:05:00+08:00\n"
        "bad-row,坏行,4.42,-0.67,41821352,186791640,2026-06-15 15:00:00,sina.spot_direct,2026-06-15T15:05:00+08:00,extra-field\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_price_update_engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(daily_price_update_engine, "DAILY_DIR", daily_dir)

    result = daily_price_update_engine._backfill_target_amount_from_spot_snapshot(["000802"], "2026-06-15")

    assert result["updated"] == 1
    assert any("csv_bad_lines_skipped" in item for item in result["warnings"])


def test_update_candidates_endpoint_delegates_full_a(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_candidate_rows(limit: int = 10) -> list[dict[str, object]]:
        return [{"symbol": "000802"}, {"symbol": "601778"}]

    def fake_update_daily_prices(root, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "updated_symbols": 2,
            "failed_symbols": 0,
            "target_date": "2026-06-15",
            "scope": "full_a",
        }

    monkeypatch.setattr(api_server, "candidate_rows", fake_candidate_rows)
    monkeypatch.setattr(api_server, "run_startup_data_update", fake_update_daily_prices)
    monkeypatch.setitem(app.config, "REALTIME_NETWORK_ENABLED", True)

    response = app.test_client().post("/api/daily/update-candidates", json={"max_symbols": 40})
    payload = response.get_json()

    assert response.status_code == 200
    assert captured == {"enable_network": True}
    assert payload["scope"] == "full_a"


def test_update_candidates_endpoint_requires_network_authorization(monkeypatch) -> None:
    called = False

    def fake_update_daily_prices(root, **kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setitem(app.config, "REALTIME_NETWORK_ENABLED", False)
    monkeypatch.setattr(api_server, "run_startup_data_update", fake_update_daily_prices)

    response = app.test_client().post("/api/daily/update-candidates", json={"max_symbols": 40})
    payload = response.get_json()

    assert response.status_code == 403
    assert payload["status"] == "network_blocked"
    assert called is False


def test_quick_guardrail_feature_audit_uses_local_features_when_no_symbols(monkeypatch) -> None:
    from quantdesk import daily_workflow_orchestrator as dwo

    captured: dict[str, object] = {}

    def fake_audit_feature_quality(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "status": "passed",
            "row_count": 3,
            "feature_count": 2,
            "warnings": [],
        }

    monkeypatch.setattr("quantdesk.research.feature_quality_audit.audit_feature_quality", fake_audit_feature_quality)
    monkeypatch.setattr(
        "quantdesk.research.backtest_statistical_validation.run_backtest_statistical_validation",
        lambda **kwargs: {"status": "warning", "sample_size": 0, "warnings": ["insufficient_sample"]},
    )

    guardrails = dwo.build_daily_guardrail_summary(
        target_date="2026-07-01",
        strategy_payload={"standard_candidates": []},
        portfolio_payload={},
    )

    assert "rows" not in captured
    assert str(captured["input_dir"]).endswith("data\\daily") or str(captured["input_dir"]).endswith("data/daily")
    assert captured["max_files"] == 5
    assert guardrails["active_diagnostics"]["feature_quality"]["row_count"] == 3
    assert "未找到本地日线/特征数据" not in " ".join(guardrails["warnings"])


def test_daily_research_status_ok_and_amount_are_not_market_colored() -> None:
    template = Path("app/templates/daily_research.html").read_text(encoding="utf-8")
    script = Path("app/static/js/daily_research.js").read_text(encoding="utf-8")
    ok_block = template.split(".llm-analysis-content .llm-ok", 1)[1].split("}", 1)[0]

    assert "var(--color-down)" not in ok_block
    for selector in (
        ".tag-ok",
        ".tag-warn, .tag-missing",
        ".startup-update-ok",
        ".news-status-ok",
        ".info-value.ok",
        ".info-value.warn, .info-value.missing",
        ".candidate-action-status.ok",
        ".candidate-action-status.warn, .candidate-action-status.missing",
        ".action-button.is-danger",
        ".risk-low",
        ".risk-high",
        ".key-review-label.risk",
        ".key-review-label.data",
        ".health-ok",
        ".health-bad",
        ".badge-ok",
        ".scenario-row.strong",
    ):
        block = template.split(selector, 1)[1].split("}", 1)[0]
        assert "var(--color-up)" not in block
        assert "var(--color-down)" not in block
    assert 'showAmount ? "neutral" : changeClass(item.pct_chg)' in script


def test_daily_research_provider_debug_labels_are_user_facing() -> None:
    script = Path("app/static/js/daily_research.js").read_text(encoding="utf-8")

    assert "provider=${provider}" not in script
    assert '"provider 鍒锋柊璇婃柇"' not in script


def test_daily_research_template_governance_copy_is_user_facing() -> None:
    template = Path("app/templates/daily_research.html").read_text(encoding="utf-8")

    assert "dry-run 状态" not in template
    assert "daily guardrails" not in template
    assert "通知中心 dry-run" not in template


def test_daily_research_summary_card_pct_change_uses_market_color() -> None:
    script = Path("app/static/js/daily_research.js").read_text(encoding="utf-8")
    card_block = script.split("function renderQuantSummaryCard(item)", 1)[1].split("function backtestTone", 1)[0]

    assert 'class="${changeClass(item.pct_chg)}"' in card_block


def test_daily_research_governance_labels_are_chinese_for_users() -> None:
    script = Path("app/static/js/daily_research.js").read_text(encoding="utf-8")

    assert "<span>handoff</span>" not in script
    assert "handoff: ${" not in script
    assert "<span>guardrails</span>" not in script
    assert "<span>feature-audit</span>" not in script
    assert "<span>stats-validate</span>" not in script
    assert "<span>risk-alerts</span>" not in script
    assert "<span>dry-run</span>" not in script
    assert "暂无 guardrail 警告" not in script


def test_performance_panel_surfaces_audit_evidence_warning() -> None:
    script = Path("app/static/js/daily_research.js").read_text(encoding="utf-8")
    block = script.split("function renderPerformance(data)", 1)[1].split(
        "async function rebuildPerformance", 1
    )[0]

    assert "data.evidence_freshness" in block
    assert "metrics_trustworthy === false" in block
    assert "data.message_for_ui" in block
    no_sample_block = block.split("if (!data.sample_count)", 1)[1].split("const o = data.overall", 1)[0]
    assert "${auditWarningHtml}" in no_sample_block
