"""Batch 1A-E: the AI book is written durably, keeps rotating backups, and a lost
ledger.json is never silently replaced by a fresh 100k book.

Before the fix: save_ledger wrote without fsync and kept no backup, and
init_ledger() created a brand-new book whenever ledger.json was missing -- even
when the journal / evidence of a months-old book sat right next to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_trader import ledger as ledger_mod
from ai_trader.ledger import (
    LEDGER_BACKUP_KEEP,
    LedgerMissingError,
    empty_ledger,
    init_ledger,
    ledger_backup_dir,
    load_ledger,
    save_ledger,
)
from quantdesk.paper.state_store import PaperStateStore


def _book_path(tmp_path: Path) -> Path:
    state = tmp_path / "state" / "ai_trader"
    state.mkdir(parents=True)
    return state / "ledger.json"


def test_save_is_fsynced_and_keeps_seven_rotating_backups(tmp_path: Path, monkeypatch) -> None:
    target = _book_path(tmp_path)
    synced: list[int] = []
    real_fsync = ledger_mod.os.fsync
    monkeypatch.setattr(ledger_mod.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    book = empty_ledger(created_at="2026-10-09T20:20:00+08:00")
    for index in range(LEDGER_BACKUP_KEEP + 3):
        book["cash"] = 100000.0 - index
        save_ledger(book, target)
    assert synced, "ledger writes must be fsynced"
    backups = sorted(ledger_backup_dir(target).glob("ledger.*.json"))
    assert len(backups) == LEDGER_BACKUP_KEEP
    assert json.loads(backups[-1].read_text(encoding="utf-8"))["cash"] == json.loads(
        target.read_text(encoding="utf-8")
    )["cash"]
    assert not list(target.parent.glob("*.tmp"))


def test_missing_ledger_with_history_refuses_to_start_a_new_book(tmp_path: Path) -> None:
    target = _book_path(tmp_path)
    (target.parent / "journal.jsonl").write_text('{"as_of": "2026-10-09"}\n', encoding="utf-8")
    with pytest.raises(LedgerMissingError):
        init_ledger(path=target)
    assert not target.exists()


def test_missing_ledger_with_backups_refuses_too(tmp_path: Path) -> None:
    target = _book_path(tmp_path)
    save_ledger(empty_ledger(created_at="2026-10-09T20:20:00+08:00"), target)
    target.unlink()
    with pytest.raises(LedgerMissingError):
        init_ledger(path=target)


def test_first_run_and_explicit_reset_still_create_a_book(tmp_path: Path) -> None:
    target = _book_path(tmp_path)
    created = init_ledger(path=target)
    assert target.is_file() and created["cash"] == created["initial_cash"]
    (target.parent / "journal.jsonl").write_text('{"x": 1}\n', encoding="utf-8")
    target.unlink()
    reset = init_ledger(reset=True, path=target)
    assert target.is_file() and reset["trades"] == []


def test_strict_load_raises_and_default_load_is_unchanged(tmp_path: Path) -> None:
    target = _book_path(tmp_path)
    with pytest.raises(LedgerMissingError):
        load_ledger(target, missing_ok=False)
    view = load_ledger(target)  # read-only pages keep the empty, unsaved view
    assert view["trades"] == [] and not target.exists()


def test_strategy_state_store_write_is_fsynced(tmp_path: Path, monkeypatch) -> None:
    import quantdesk.paper.state_store as store_mod

    synced: list[int] = []
    real_fsync = store_mod.os.fsync
    monkeypatch.setattr(store_mod.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    store = PaperStateStore(tmp_path / "auto_paper_portfolio.json", schema_version="x", max_backups=7)
    store.write({"cash": 1.0})
    assert len(synced) >= 2  # the new file and its backup
    assert json.loads((tmp_path / "auto_paper_portfolio.json").read_text(encoding="utf-8"))["cash"] == 1.0
