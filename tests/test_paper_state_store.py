from __future__ import annotations

import json
from pathlib import Path

from quantdesk.paper.state_store import PaperStateStore
from quantdesk.paper import paper_trading


def test_state_store_atomic_write_adds_schema_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "state" / "paper.json"
    store = PaperStateStore(path, schema_version="unit-schema", max_backups=3)

    written = store.write({"cash": 100})

    assert written["schema_version"] == "unit-schema"
    assert json.loads(path.read_text(encoding="utf-8"))["cash"] == 100
    backups = sorted(path.parent.glob("paper.backup_*.json"))
    assert len(backups) == 1
    assert path.with_name("paper.json.tmp").exists() is False


def test_state_store_recovers_corrupt_file_from_latest_backup(tmp_path: Path) -> None:
    path = tmp_path / "state" / "paper.json"
    store = PaperStateStore(path, schema_version="unit-schema", max_backups=3)
    store.write({"cash": 100})
    store.write({"cash": 200})
    path.write_text("{bad json", encoding="utf-8")

    recovered = store.read(default={"cash": 0})

    assert recovered["cash"] == 200
    assert recovered["state_recovered_from_backup"] is True
    assert list(path.parent.glob("paper.bad_*.json"))


def test_state_store_rotates_old_backups(tmp_path: Path) -> None:
    path = tmp_path / "state" / "paper.json"
    store = PaperStateStore(path, schema_version="unit-schema", max_backups=2)

    for cash in (100, 200, 300, 400):
        store.write({"cash": cash})

    backups = sorted(path.parent.glob("paper.backup_*.json"))
    assert len(backups) == 2


def test_manual_paper_portfolio_load_recovers_from_state_store_backup(tmp_path: Path) -> None:
    path = tmp_path / "state" / "paper_portfolio.json"
    portfolio = paper_trading.create_default_paper_portfolio(
        initial_cash=100000,
        scenario_id="acceptance_100k",
    )
    portfolio["account"]["cash"] = 90000
    paper_trading.save_paper_portfolio(portfolio, path=path)
    portfolio["account"]["cash"] = 88000
    paper_trading.save_paper_portfolio(portfolio, path=path)
    path.write_text("{bad json", encoding="utf-8")

    recovered = paper_trading.load_paper_portfolio(path=path)

    assert recovered["account"]["cash"] == 88000
    assert recovered["state_recovered_from_backup"] is True
