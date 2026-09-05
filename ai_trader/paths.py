"""Filesystem contract for the independent AI paper trader.

QuantDesk remains the read-only market-data host. This package never writes
``state/auto_paper_portfolio.json`` or any other QuantDesk ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

FORBIDDEN_QUANTDESK_LEDGER = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"

STATE_DIR = PROJECT_ROOT / "state" / "ai_trader"
RESULTS_DIR = PROJECT_ROOT / "results" / "ai_trader"
DEFAULT_DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DEFAULT_MINUTE_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"

LEDGER_PATH = STATE_DIR / "ledger.json"
HYPOTHESES_PATH = STATE_DIR / "hypotheses.json"
JOURNAL_PATH = STATE_DIR / "journal.jsonl"
JOURNAL_LATEST_PATH = STATE_DIR / "journal_latest.json"
DAYTIME_NOTES_PATH = STATE_DIR / "daytime_notes.jsonl"
DAYTIME_SESSION_PATH = STATE_DIR / "daytime_session.json"
PLAN_LATEST_PATH = STATE_DIR / "plan_latest.json"
REVIEW_LATEST_PATH = STATE_DIR / "review_latest.json"
WATCHLIST_PATH = STATE_DIR / "watchlist.json"
PHILOSOPHY_PATH = STATE_DIR / "philosophy.json"
CYCLE_LATEST_PATH = RESULTS_DIR / "cycle_latest.json"
SNAPSHOT_PATH = RESULTS_DIR / "snapshot_latest.json"
DECISION_LOG_PATH = STATE_DIR / "decision_log.jsonl"
DECISION_LATEST_PATH = STATE_DIR / "decision_latest.json"
DECISION_PAGE_PATH = RESULTS_DIR / "decision_page.html"
DECISION_PAGE_MD_PATH = RESULTS_DIR / "decision_page.md"


@dataclass(frozen=True)
class CyclePaths:
    daily_dir: Path = DEFAULT_DAILY_DIR
    minute_dir: Path = DEFAULT_MINUTE_DIR
    ledger: Path = LEDGER_PATH
    hypotheses: Path = HYPOTHESES_PATH
    journal_latest: Path = JOURNAL_LATEST_PATH
    journal_log: Path = JOURNAL_PATH
    daytime_notes: Path = DAYTIME_NOTES_PATH
    daytime_session: Path = DAYTIME_SESSION_PATH
    plan_latest: Path = PLAN_LATEST_PATH
    review_latest: Path = REVIEW_LATEST_PATH
    watchlist: Path = WATCHLIST_PATH
    philosophy: Path = PHILOSOPHY_PATH
    cycle_latest: Path = CYCLE_LATEST_PATH
    snapshot: Path = SNAPSHOT_PATH
    decision_log: Path = DECISION_LOG_PATH
    decision_latest: Path = DECISION_LATEST_PATH
    decision_page: Path = DECISION_PAGE_PATH
    decision_page_md: Path = DECISION_PAGE_MD_PATH


def ensure_runtime_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def assert_not_quantdesk_ledger(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = FORBIDDEN_QUANTDESK_LEDGER.resolve()
    if resolved == forbidden:
        raise RuntimeError(
            "ai_trader refused to write QuantDesk ledger "
            f"{FORBIDDEN_QUANTDESK_LEDGER}"
        )
    if "auto_paper_portfolio" in resolved.name:
        raise RuntimeError(f"ai_trader refused to write forbidden path {resolved}")
    return resolved
