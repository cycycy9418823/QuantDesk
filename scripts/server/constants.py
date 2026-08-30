"""Module-level constants and shared global state for the 8000 server.

Split out of quant_api_server.py; re-exported from that namespace so the test
attribute contract and existing import sites keep working unchanged.
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

project_root = Path(__file__).resolve().parents[2]

DATA_DIR = project_root / "data"
DAILY_DIR = DATA_DIR / "daily"
TRACKING_DIR = DATA_DIR / "tracking"
BENCHMARK_DIR = DATA_DIR / "benchmark"
CACHE_DIR = DATA_DIR / "cache"
UNIVERSE_DIR = DATA_DIR / "universe"
STARTUP_UPDATE_JSON = project_root / "results" / "startup_update_latest.json"
STRATEGY_SCREENING_TASK_JSON = project_root / "results" / "strategy_screening_funnel_task_latest.json"
DAILY_CANDIDATE_GENERATION_JSON = project_root / "results" / "daily_research" / "daily_candidate_generation_latest.json"
DAILY_CANDIDATE_LATEST_CSV = project_root / "results" / "daily_research" / "v420_daily_stock_candidates_latest.csv"
_LATEST_GENERATION_SYMBOL_CACHE: dict[str, Any] = {
    "csv_mtime": None,
    "ledger_mtime": None,
    "signal_date": "",
    "symbols": set(),
}
# Local cache for the akshare industry-board snapshot (refreshed on demand).
INDUSTRY_BOARD_CACHE = CACHE_DIR / "industry_board_latest.csv"
SPOT_SNAPSHOT_CACHE = CACHE_DIR / "spot_snapshot_latest.csv"
SPOT_SNAPSHOT_META = CACHE_DIR / "spot_snapshot_meta.json"
LIMIT_UP_REVIEW_CACHE = CACHE_DIR / "limit_up_review_latest.json"
MANUAL_OBSERVATION_WATCHLIST = TRACKING_DIR / "manual_observation_watchlist.csv"
PAPER_WATCHLIST_JSON = project_root / "state" / "paper_watchlist.json"
STOCK_THEME_MAP_CSV = UNIVERSE_DIR / "stock_theme_map.csv"
STOCK_THEME_MAP_IMPORT_CSV = UNIVERSE_DIR / "stock_theme_map_import.csv"

UPDATE_HINT = (
    "本地数据缺失。请先运行 python scripts/qd.py daily --network false --summary true；"
    "如果本地没有该股票日线，请导入 CSV，或在你确认联网时运行数据更新。"
)
MARKET_OVERVIEW_HINT = (
    "市场全景读取本地指数、实时快照缓存和行业板块缓存；盘中/实时层可联网刷新，"
    "正式研究层仍以完整日线复核。"
)

STRATEGY_LABELS = {
    "long_term": "长期观察",
    "da_ban": "打板观察",
}
DETAIL_LANE_TO_STRATEGY = {
    "long": "long_term",
    "board": "da_ban",
}
PRIMARY_STRATEGY_PRIORITY = {
    "da_ban": 20,
    "long_term": 10,
}

REALTIME_CACHE_MAX_AGE_SECONDS = 300
REALTIME_PROVIDER_RETRY_SECONDS = 300
SECTOR_CACHE_MAX_AGE_SECONDS = 300
REALTIME_BACKGROUND_LOCK = Lock()
REALTIME_BACKGROUND_STATUS: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "message_for_ui": "实时观察层后台刷新空闲。",
}


STARTUP_REFRESH_LOCK = Lock()
STARTUP_REFRESH_STATE: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "reason": "",
    "started_at": "",
    "request_id": "",
    "message_for_ui": "",
}

STARTUP_TERMINAL_STATUSES = {
    "completed",
    "completed_with_warnings",
    "partial",
    "failed",
    "timeout",
    "busy",
    "skipped",
    "skipped_network_disabled",
}

STRATEGY_SCREENING_LOCK = Lock()
STRATEGY_SCREENING_STATE: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "request_id": "",
    "started_at": "",
    "message_for_ui": "",
}

# Mutual exclusion for destructive data-write endpoints (update-all,
# repair-daily, backtest run, etc.).  Only one write operation at a time
# prevents CSV corruption from concurrent writes to the same file.
DATA_WRITE_LOCK = Lock()
