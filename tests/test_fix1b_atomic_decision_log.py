"""1B-E: decision_log.jsonl is replaced whole (temp + fsync + replace), never half-written.

Before the fix the file was rewritten in place every night with ``write_text``;
an interruption mid-write left a truncated file, and unreadable lines were
silently skipped on read and then erased by the next rewrite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_trader import decision_log as dl


def _rows(n: int) -> list[dict]:
    return [{"decision_id": f"dec_{i}", "judgment": f"第{i}条", "result": dl.RESULT_PENDING} for i in range(n)]


def test_write_round_trips_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "decision_log.jsonl"
    dl.write_decision_records(path, _rows(3))
    assert [row["decision_id"] for row in dl.load_decision_records(path)] == ["dec_0", "dec_1", "dec_2"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["decision_log.jsonl"]
    # Byte-stable: LF only, one JSON object per line.
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")


def test_interrupted_replace_keeps_the_previous_file_intact(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "decision_log.jsonl"
    dl.write_decision_records(path, _rows(2))
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("simulated power loss before replace")

    monkeypatch.setattr(dl.os, "replace", boom)
    with pytest.raises(OSError):
        dl.write_decision_records(path, _rows(5))
    assert path.read_bytes() == before, "old file must survive an interrupted rewrite"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["decision_log.jsonl"], "temp file must be cleaned up"


def test_unreadable_lines_are_quarantined_not_erased(tmp_path: Path) -> None:
    path = tmp_path / "decision_log.jsonl"
    good = json.dumps({"decision_id": "dec_ok"}, ensure_ascii=False)
    path.write_text(good + "\n{\"decision_id\": \"dec_half\n[1,2]\n", encoding="utf-8")
    assert [row["decision_id"] for row in dl.load_decision_records(path)] == ["dec_ok"]

    dl.upsert_decision_records(path, [{"decision_id": "dec_new", "judgment": "x"}])

    quarantine = tmp_path / "decision_log.corrupt.jsonl"
    assert quarantine.is_file(), "bad lines must be kept somewhere before the rewrite"
    kept = [json.loads(line)["raw_line"] for line in quarantine.read_text(encoding="utf-8").splitlines()]
    assert kept == ['{"decision_id": "dec_half', "[1,2]"]
    ids = [row["decision_id"] for row in dl.load_decision_records(path)]
    assert ids == ["dec_new", "dec_ok"] or sorted(ids) == ["dec_new", "dec_ok"]
