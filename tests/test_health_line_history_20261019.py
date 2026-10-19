"""Charter §7: the daily health line must have a durable per-date history."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "write_health_line.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("write_health_line_history_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_same_day_rerun_replaces_only_that_day(tmp_path: Path) -> None:
    mod = load_mod()
    first = mod.append_health_history(
        tmp_path,
        target="2026-10-16",
        header="# STATUS · 更新于 2026-10-16 22:30",
        line="日线 ✓1/1",
        explains=["健康业务日期 2026-10-16", "备份 ✓"],
        trading=True,
    )
    assert first == tmp_path / "results" / "health_line" / "history" / "2026-10-16.md"
    text = first.read_text(encoding="utf-8")
    assert "2026-10-16" in text
    assert "日线 ✓1/1" in text
    assert "备份 ✓" in text
    assert "交易日：是" in text

    second = mod.append_health_history(
        tmp_path,
        target="2026-10-16",
        header="# STATUS · 更新于 2026-10-16 23:00",
        line="日线 ✗0/1",
        explains=["重跑"],
        trading=True,
    )
    assert second == first
    assert len(list((tmp_path / "results" / "health_line" / "history").glob("*.md"))) == 1
    assert "日线 ✗0/1" in first.read_text(encoding="utf-8")


def test_different_days_are_additive(tmp_path: Path) -> None:
    mod = load_mod()
    for day in ("2026-10-15", "2026-10-16"):
        mod.append_health_history(
            tmp_path,
            target=day,
            header=f"# STATUS · 更新于 {day} 22:30",
            line="日线 ✓",
            explains=[],
            trading=True,
        )
    files = sorted((tmp_path / "results" / "health_line" / "history").glob("*.md"))
    assert [path.stem for path in files] == ["2026-10-15", "2026-10-16"]
