"""Filesystem contract for the independent AI paper trader.

QuantDesk remains the read-only market-data host. This package never writes
``state/auto_paper_portfolio.json`` or any other QuantDesk ledger.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

FORBIDDEN_QUANTDESK_LEDGER = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"

STATE_DIR = PROJECT_ROOT / "state" / "ai_trader"
RESULTS_DIR = PROJECT_ROOT / "results" / "ai_trader"
DEFAULT_DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DEFAULT_MINUTE_DIR = PROJECT_ROOT / "data" / "intraday" / "1m"

PROFILE_ENV = "AI_TRADER_PROFILE"
PROFILE_NAMES = ("glm",)
_DEFAULT_LEDGER_ID = "ai_trader_100k"
_PROFILE_LEDGER_IDS = {"glm": "ai_trader_100k_glm"}

LEDGER_PATH = STATE_DIR / "ledger.json"
HYPOTHESES_PATH = STATE_DIR / "hypotheses.json"
JOURNAL_PATH = STATE_DIR / "journal.jsonl"
JOURNAL_LATEST_PATH = STATE_DIR / "journal_latest.json"
DAYTIME_NOTES_PATH = STATE_DIR / "daytime_notes.jsonl"
DAYTIME_SESSION_PATH = STATE_DIR / "daytime_session.json"
DAYTIME_INTENTS_PATH = STATE_DIR / "daytime_intents.jsonl"
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
    daytime_intents: Path = DAYTIME_INTENTS_PATH
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


def _profile_name(profile: str | None = None) -> str:
    raw = active_profile() if profile is None else str(profile)
    name = raw.strip().lower()
    return "" if name in ("", "default") else name


def active_profile() -> str:
    """The profile this process runs as. Empty string means the default book."""

    return os.environ.get(PROFILE_ENV, "").strip().lower()


def profile_state_dir(profile: str | None = None) -> Path:
    name = _profile_name(profile)
    return STATE_DIR if not name else PROJECT_ROOT / "state" / f"ai_trader_{name}"


def profile_results_dir(profile: str | None = None) -> Path:
    name = _profile_name(profile)
    return RESULTS_DIR if not name else PROJECT_ROOT / "results" / f"ai_trader_{name}"


def ledger_id_for_profile(profile: str | None = None) -> str:
    name = _profile_name(profile)
    return _DEFAULT_LEDGER_ID if not name else _PROFILE_LEDGER_IDS.get(name, f"{_DEFAULT_LEDGER_ID}_{name}")


def ledger_id_for_path(path: str | Path) -> str:
    """Expected ledger id for a ledger file, derived from its directory.

    Env-independent on purpose: one process may observe several profiles (the
    intraday round does), so the identity must follow the path, not the env.
    """

    parent = Path(path).resolve().parent.name
    if parent == STATE_DIR.name:
        return _DEFAULT_LEDGER_ID
    prefix = f"{STATE_DIR.name}_"
    if parent.startswith(prefix):
        suffix = parent[len(prefix):]
        return _PROFILE_LEDGER_IDS.get(suffix, f"{_DEFAULT_LEDGER_ID}_{suffix}")
    return _DEFAULT_LEDGER_ID


def profile_env_file(profile: str | None = None) -> Path:
    return profile_state_dir(profile) / "llm.env"


def configured_profiles() -> list[str]:
    """Default book first, then every profile whose state directory exists."""

    names = [""]
    for name in PROFILE_NAMES:
        if profile_state_dir(name).is_dir():
            names.append(name)
    return names


def profile_paths(profile: str | None = None) -> CyclePaths:
    """Resolve one profile's runtime paths. The default profile is unchanged."""

    name = _profile_name(profile)
    if not name:
        return CyclePaths()
    state = profile_state_dir(name)
    results = profile_results_dir(name)
    return CyclePaths(
        daily_dir=DEFAULT_DAILY_DIR,
        minute_dir=DEFAULT_MINUTE_DIR,
        ledger=state / "ledger.json",
        hypotheses=state / "hypotheses.json",
        journal_latest=state / "journal_latest.json",
        journal_log=state / "journal.jsonl",
        daytime_notes=state / "daytime_notes.jsonl",
        daytime_session=state / "daytime_session.json",
        daytime_intents=state / "daytime_intents.jsonl",
        plan_latest=state / "plan_latest.json",
        review_latest=state / "review_latest.json",
        watchlist=state / "watchlist.json",
        philosophy=state / "philosophy.json",
        cycle_latest=results / "cycle_latest.json",
        snapshot=results / "snapshot_latest.json",
        decision_log=state / "decision_log.jsonl",
        decision_latest=state / "decision_latest.json",
        decision_page=results / "decision_page.html",
        decision_page_md=results / "decision_page.md",
    )


def ensure_profile_dirs(profile: str | None = None) -> None:
    profile_state_dir(profile).mkdir(parents=True, exist_ok=True)
    profile_results_dir(profile).mkdir(parents=True, exist_ok=True)


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
