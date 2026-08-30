from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from pathlib import Path


def _serialized_probe(callable_) -> int:
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: callable_(), range(2)))
    return 0


def test_daily_same_symbol_read_merge_write_is_serialized(monkeypatch, tmp_path: Path) -> None:
    import quantdesk.daily_price_update_engine as engine

    active = 0
    max_active = 0
    guard = threading.Lock()

    def fake_unlocked(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return {"symbol": "600000", "updated": True}

    monkeypatch.setattr(engine, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(engine, "_fetch_merge_write_symbol_unlocked", fake_unlocked)

    def call() -> None:
        engine._fetch_merge_write_symbol(
            "provider",
            "600000",
            target="2026-07-16",
            network=True,
            calendar={},
            daily_dir=tmp_path,
            metadata_dir=tmp_path / "metadata",
        )

    _serialized_probe(call)

    assert max_active == 1


def test_intraday_same_symbol_read_merge_write_is_serialized(monkeypatch, tmp_path: Path) -> None:
    import pandas as pd

    from quantdesk.intraday import intraday_update_engine as engine

    active = 0
    max_active = 0
    guard = threading.Lock()

    def fake_unlocked(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return tmp_path / "600000.csv"

    monkeypatch.setattr(engine, "_merge_and_write_unlocked", fake_unlocked)
    frame = pd.DataFrame()

    def call() -> None:
        engine._merge_and_write(
            "600000",
            frame,
            append=True,
            overwrite=False,
            base_dir=tmp_path,
        )

    _serialized_probe(call)

    assert max_active == 1
