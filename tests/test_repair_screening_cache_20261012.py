from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pandas as pd
import pytest

from scripts import run_strategy_screening_funnel as funnel


def _daily(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 200
    close = [10 + math.sin(i / 7) + i / 200 for i in range(count)]
    pd.DataFrame({"date": pd.date_range("2025-01-01", periods=count),
                  "symbol": path.stem, "open": close, "high": [v + .1 for v in close],
                  "low": [v - .1 for v in close], "close": close,
                  "volume": [1000 + i for i in range(count)],
                  "amount": [30000000 + i for i in range(count)]}).to_csv(path, index=False)


def _args(root: Path, *extra: str):
    return funnel.parse_args(["--data-root", str(root / "daily"), "--output-dir", str(root / "output"),
                              "--feature-cache", "true", *extra])


def test_default_cache_follows_output_root_and_preserves_exact_values(tmp_path: Path) -> None:
    _daily(tmp_path / "daily" / "000001.csv")
    args = _args(tmp_path)
    first, quality, write = funnel.load_or_build_features(args, tmp_path / "daily")
    second, cached_quality, read = funnel.load_or_build_features(args, tmp_path / "daily")
    assert write["status"] == "miss_written"
    assert read["status"] == "hit"
    assert Path(read["path"]).parent == tmp_path / "output" / "cache"
    assert quality == cached_quality
    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert list(second["symbol"].unique()) == ["000001"]
    assert not list(tmp_path.rglob("*.pkl"))


def test_relative_cache_is_relative_to_output_and_absolute_is_explicit(tmp_path: Path) -> None:
    assert funnel._feature_cache_directory(_args(tmp_path, "--cache-dir", "research-cache")) == tmp_path / "output" / "research-cache"
    assert funnel._feature_cache_directory(_args(tmp_path, "--cache-dir", str(tmp_path / "explicit"))) == tmp_path / "explicit"


def test_legacy_pickle_is_never_deserialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _daily(tmp_path / "daily" / "000001.csv")
    args = _args(tmp_path)
    key, _ = funnel.feature_cache_fingerprint(tmp_path / "daily", 180)
    cache = tmp_path / "output" / "cache"
    cache.mkdir(parents=True)
    legacy = cache / f"strategy_screening_features_{key[:16]}.pkl"
    legacy.write_bytes(b"legacy pickle is not executable input")
    def forbidden(*args, **kwargs):
        raise AssertionError("pickle must not be read or written")
    monkeypatch.setattr(pd, "read_pickle", forbidden)
    monkeypatch.setattr(pd, "to_pickle", forbidden)
    _, _, receipt = funnel.load_or_build_features(args, tmp_path / "daily")
    assert receipt["status"] == "miss_written"
    assert legacy.read_bytes() == b"legacy pickle is not executable input"


def test_same_size_and_mtime_data_changes_invalidate_cache(tmp_path: Path) -> None:
    daily = tmp_path / "daily" / "000001.csv"
    _daily(daily)
    args = _args(tmp_path)
    _, _, before = funnel.load_or_build_features(args, daily.parent)
    stat = daily.stat()
    content = daily.read_bytes()
    daily.write_bytes(content.replace(b"30000000", b"40000000", 1))
    os.utime(daily, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert daily.stat().st_size == stat.st_size
    _, _, after = funnel.load_or_build_features(args, daily.parent)
    assert before["cache_key"] != after["cache_key"]
    assert after["status"] == "miss_written"


def test_truncated_or_tampered_cache_is_rebuilt_with_visible_reason(tmp_path: Path) -> None:
    _daily(tmp_path / "daily" / "000001.csv")
    args = _args(tmp_path)
    first, _, info = funnel.load_or_build_features(args, tmp_path / "daily")
    receipt = Path(info["path"])
    meta = json.loads(receipt.read_text(encoding="utf-8"))
    content = receipt.with_name(f"{receipt.stem}_{meta['csv_sha256']}.csv")
    content.write_text("date,symbol\n", encoding="utf-8")
    rebuilt, _, info = funnel.load_or_build_features(args, tmp_path / "daily")
    assert info["status"] == "miss_written"
    assert info["read_status"] == "read_failed_rebuilt_from_daily"
    assert "feature_cache_content_mismatch" in info["error"]
    pd.testing.assert_frame_equal(first, rebuilt, check_exact=True)


def test_disabled_cache_does_not_create_output(tmp_path: Path) -> None:
    _daily(tmp_path / "daily" / "000001.csv")
    _, _, receipt = funnel.load_or_build_features(_args(tmp_path, "--feature-cache", "false"), tmp_path / "daily")
    assert receipt["status"] == "disabled"
    assert not (tmp_path / "output").exists()
