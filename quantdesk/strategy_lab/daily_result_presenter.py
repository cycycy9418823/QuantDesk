from __future__ import annotations

import csv
import json
from pathlib import Path


def build_result_page(root: Path, top_n: int = 8) -> tuple[dict, str]:
    fresh = json.loads((root / "results/daily_research/v421_trading_day_freshness_latest.json").read_text(encoding="utf-8-sig"))
    update = json.loads((root / "results/daily_research/v421_data_update_attempt_latest.json").read_text(encoding="utf-8-sig"))
    leaderboard = []
    stocks = []
    sectors = []
    with (root / "results/strategy_lab/v420_strategy_leaderboard_latest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        leaderboard = list(csv.DictReader(handle))[:top_n]
    with (root / "results/daily_research/v420_daily_stock_candidates_latest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        stocks = list(csv.DictReader(handle))[:top_n]
    with (root / "results/daily_research/v420_daily_sector_candidates_latest.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        sectors = list(csv.DictReader(handle))[:top_n]
    payload = {
        "task_id": "v421_daily_result_page",
        "status": "completed",
        "data_status": fresh,
        "update_status": update,
        "top_strategy_rows": leaderboard,
        "top_stock_rows": stocks,
        "top_sector_rows": sectors,
        "research_only": True,
    }
    lines = [
        "# v421 Daily Result Page",
        "",
        f"Data status: `{fresh['status']}`",
        f"Latest local trade date: `{fresh['latest_local_trade_date']}`",
        f"Latest expected trade date: `{fresh['latest_expected_trade_date']}`",
        f"Update status: `{update['status']}`",
        "",
        "## Top Strategy Families",
    ]
    for row in leaderboard:
        lines.append(f"- #{row.get('research_rank')} {row.get('strategy_id')} | {row.get('strategy_health')} | robustness={row.get('robustness_score')}")
    lines.append("\n## Top Stock Research Candidates")
    for row in stocks:
        lines.append(f"- {row.get('symbol')} | {row.get('strategy_family')} | {row.get('trigger_reason')} | risks={row.get('risk_flags')}")
    lines.append("\n## Top Sector Research Candidates")
    for row in sectors:
        lines.append(f"- {row.get('sector_name')} | {row.get('stock_symbol')} | {row.get('research_reason')} | risks={row.get('risk_flags')}")
    lines.append("\nResearch-only. No live trading, broker connection, auto execution, buy/sell recommendation, target price, position advice, or real-money allocation.")
    lines.append("\nDetailed files are available under `results/daily_research/` and `results/strategy_lab/`.")
    return payload, "\n".join(lines) + "\n"

