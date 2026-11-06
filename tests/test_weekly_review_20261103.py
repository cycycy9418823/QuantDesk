"""Weekly review inside the first brief of a week (2026-11-03)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from quantdesk import owner_view, weekly_review


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload, encoding="utf-8")


def _seed(root: Path) -> None:
    _write(root / "results/auto_paper_portfolio_history.json", {"items": [
        {"valuation_date": "2026-10-23", "total_equity": 82515.64},
        {"valuation_date": "2026-10-27", "total_equity": 79331.78},
        {"valuation_date": "2026-10-30", "total_equity": 77329.71},
    ]})
    _write(root / "results/auto_paper_portfolio_latest.json", {
        "valuation_date": "2026-10-30", "summary": {"total_equity": 77329.71},
        "closed_positions": [
            {"symbol": "301513", "name": "尚水智能", "entry_date": "2026-09-29", "entry_price": 51.4957,
             "execution_price": 51.42, "exit_date": "2026-10-30", "exit_price": 44.38, "realized_pnl": -721.01,
             "exit_reason": "最新价较模拟买入价回撤超过 8%"},
            {"symbol": "002979", "name": "雷赛智能", "entry_date": "2026-10-27", "execution_price": 55.54,
             "exit_date": "2026-10-30", "exit_price": 50.0, "realized_pnl": -571.78, "exit_reason": "回撤超过 8%"},
            {"symbol": "600000", "name": "旧的", "entry_date": "2026-09-01", "execution_price": 10.0,
             "exit_date": "2026-10-20", "exit_price": 9.0, "realized_pnl": -100.0},
        ],
        "positions": [{"symbol": "002421", "entry_date": "2026-10-26"}],
    })
    days = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
    closes = [4110.0, 4105.329, 4103.543, 4049.552, 4083.87, 4041.838, 3985.501]
    _write(root / "data/benchmark/hs300.csv",
           "date,open,high,low,close\n" + "".join(f"{d},1,1,1,{c}\n" for d, c in zip(days, closes)))
    rows = "".join(f"2026-09-{day:02d},301513,1,1,1,{40 + day * 0.5},1,raw\n" for day in range(1, 30))
    _write(root / "data/daily/301513.csv", "date,symbol,open,high,low,close,volume,adjust\n" + rows)


def test_first_session_of_week_detection() -> None:
    assert weekly_review.is_first_session_of_week("2026-10-30", date(2026, 11, 2)) is True
    assert weekly_review.is_first_session_of_week("2026-11-02", date(2026, 11, 3)) is False
    assert weekly_review.is_first_session_of_week("", date(2026, 11, 2)) is False


def test_weekly_review_uses_strict_records_and_lists_gaps(tmp_path: Path) -> None:
    _seed(tmp_path)
    review = weekly_review.build_weekly_review(tmp_path, "2026-10-30")
    assert (review["start"], review["end"]) == ("2026-10-23", "2026-10-30")
    assert round(review["strategy_pct"], 2) == -6.28
    assert round(review["index_pct"], 2) == -2.92
    assert review["missing_valuation"] == ["2026-10-26", "2026-10-28", "2026-10-29"]
    assert review["closed_count"] == 2 and review["closed_pnl"] == -1292.79
    assert [row["name"] for row in review["worst"]] == ["尚水智能", "雷赛智能"]
    assert review["worst"][0]["entry_price"] == 51.42  # the fill, not the fee-loaded cost
    assert "比 20 日均线高" in review["worst"][0]["entry_note"]
    assert review["opened_count"] == 1


def test_weekly_text_is_plain_and_asks_three_questions(tmp_path: Path) -> None:
    _seed(tmp_path)
    text = weekly_review.render_weekly_review(weekly_review.build_weekly_review(tmp_path, "2026-10-30"))
    assert "上周回顾（10/26–10/30）" in text
    assert "缺估值记录的日子：10/26、10/28、10/29（不补）" in text
    assert "尚水智能的止损线是 -8%，实际亏了 13.7%" in text
    assert text.count("\n1. ") == 1 and "\n3. " in text
    assert not any(term in text for term in owner_view.PROHIBITED_TERMS)


def test_weekly_review_with_no_files_says_so_without_numbers(tmp_path: Path) -> None:
    text = weekly_review.render_weekly_review(weekly_review.build_weekly_review(tmp_path, "2026-10-30"))
    assert "策略账 读不到，沪深300 读不到" in text
    assert "新开和平掉的记录：读不到" in text and "+0 元" not in text
