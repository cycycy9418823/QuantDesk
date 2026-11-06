"""A03: owner's 46/48 and 99% rule must be backed by archived bytes."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

from quantdesk import owner_view, snapshot_history

DAY = "2026-11-04"
SLOTS = snapshot_history.expected_snapshot_slots()
SYMBOLS = [f"{600000 + i:06d}" for i in range(100)]


def write_archive(
    root: Path, day: str, slot: str, *, bad: int = 0,
    rows: list[dict] | None = None, legacy: bool = False,
) -> tuple[Path, dict, list[dict]]:
    """A real, tiny deterministic archive; unneeded quote fields stay missing."""
    target = root / "data" / "snapshots" / day / f"{slot}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    fetched = f"{day}T{slot[:2]}:{slot[2:]}:03+08:00"
    if rows is None:
        rows = [dict(snapshot_date=day, snapshot_time=slot, symbol=symbol,
                     price="" if i < bad else "10", quote_date=day,
                     quote_time="", source="fixture", fetched_at=fetched)
                for i, symbol in enumerate(SYMBOLS)]
    columns = snapshot_history.LEGACY_SNAPSHOT_COLUMNS if legacy else snapshot_history.SNAPSHOT_COLUMNS
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    content = text.getvalue().encode("utf-8")
    target.write_bytes(content)
    observed = {row["symbol"] for row in rows}
    covered = len(set(SYMBOLS) & observed)
    meta = dict(schema_version="a0_snapshot_history_v1", trade_date=day, slot_time=slot,
                snapshot_path=target.relative_to(root).as_posix(), row_count=len(rows),
                expected_symbols=SYMBOLS, expected_symbol_count=len(SYMBOLS),
                universe_sha256=hashlib.sha256(json.dumps(SYMBOLS, separators=(",", ":")).encode()).hexdigest(),
                csv_sha256=hashlib.sha256(content).hexdigest(), fetched_at=fetched,
                source="fixture", status="partial", quality_status="partial",
                missing_symbols=sorted(set(SYMBOLS) - observed), coverage_count=covered,
                coverage_ratio=covered / len(SYMBOLS), unexpected_symbols=sorted(observed - set(SYMBOLS)),
                price_missing_count=bad, quote_date_missing_count=0,
                quote_date_mismatch_count=0, quote_date_invalid_count=0)
    write_meta(target, meta)
    return target, meta, rows


def write_meta(target: Path, meta: object) -> None:
    target.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")


def test_forged_empty_archives_cannot_make_a_green_day(tmp_path: Path) -> None:
    for slot in SLOTS[:46]:
        target, meta, _ = write_archive(tmp_path, DAY, slot)
        target.write_bytes(b"")
        meta["csv_sha256"] = "fake"
        write_meta(target, meta)
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert result["good"] == 0
    assert result["ok"] is False


@pytest.mark.parametrize("damage", [
    "empty", "header_only", "truncated_row", "extra_column", "broken_csv", "missing_csv", "missing_meta",
    "hash_missing", "hash_wrong", "date", "slot", "path", "row_count", "expected_count",
    "universe_hash", "duplicate_roster", "duplicate_symbol", "row_date", "row_slot", "row_source",
    "row_fetch", "timezone", "status", "schema", "invalid_json", "not_mapping", "missing_column",
])
def test_damaged_archive_cannot_count(tmp_path: Path, damage: str) -> None:
    target, meta, rows = write_archive(tmp_path, DAY, "0930")
    replacements = {"hash_wrong": ("csv_sha256", "0" * 64), "date": ("trade_date", "2026-11-03"),
                    "slot": ("slot_time", "0935"), "path": ("snapshot_path", "other.csv"),
                    "row_count": ("row_count", 999), "expected_count": ("expected_symbol_count", 1),
                    "universe_hash": ("universe_sha256", "0" * 64), "timezone": ("fetched_at", DAY + "T09:30:03"),
                    "status": ("status", "failed"), "schema": ("schema_version", "unknown")}
    if damage in replacements:
        key, value = replacements[damage]
        meta[key] = value
    elif damage == "hash_missing":
        del meta["csv_sha256"]
    elif damage == "duplicate_roster":
        meta["expected_symbols"] = SYMBOLS + [SYMBOLS[0]]
    elif damage in {"duplicate_symbol", "row_date", "row_slot", "row_source", "row_fetch"}:
        key, value = {"duplicate_symbol": ("symbol", SYMBOLS[1]), "row_date": ("snapshot_date", "2026-11-03"),
                      "row_slot": ("snapshot_time", "0935"), "row_source": ("source", "other"),
                      "row_fetch": ("fetched_at", DAY + "T09:35:03+08:00")}[damage]
        rows[0][key] = value
        target, meta, _ = write_archive(tmp_path, DAY, "0930", rows=rows)
    elif damage in {"empty", "header_only", "truncated_row", "extra_column", "broken_csv", "missing_column"}:
        lines = target.read_text(encoding="utf-8").splitlines()
        content = {"empty": "", "header_only": lines[0] + "\n", "truncated_row": lines[0] + "\n" + "600000\n",
                   "extra_column": "\n".join([lines[0]] + [line + ",extra" for line in lines[1:]]) + "\n",
                   "broken_csv": lines[0] + '\n"unfinished', "missing_column": "symbol,price\n600000,10\n"}[damage]
        target.write_text(content, encoding="utf-8")
        # Even matching hashes cannot turn malformed CSV into valid evidence.
        meta["csv_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        if damage == "truncated_row":
            meta["row_count"] = 1
    elif damage == "missing_csv":
        target.unlink()
    elif damage == "not_mapping":
        meta = []
    write_meta(target, meta)
    if damage == "missing_meta":
        target.with_suffix(".json").unlink()
    elif damage == "invalid_json":
        target.with_suffix(".json").write_text("{broken", encoding="utf-8")
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert result["good"] == 0
    assert result["ok"] is False


@pytest.mark.parametrize("price,quote_date", [("", DAY), ("0", DAY), ("-1", DAY), ("NaN", DAY),
                                             ("inf", DAY), ("10", ""), ("10", "2026-11-03"),
                                             ("10", "2026-11-04junk"), ("10", "invalid")])
def test_actual_rows_override_sidecar_quality_claims(tmp_path: Path, price: str, quote_date: str) -> None:
    _, _, rows = write_archive(tmp_path, DAY, "0930")
    for row in rows[:2]:
        row.update(price=price, quote_date=quote_date)
    write_archive(tmp_path, DAY, "0930", rows=rows)  # sidecar still says zero bad
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert result["good"] == 0
    assert result["min_ratio"] == 0.98


def test_99_percent_and_overlapping_errors_count_each_symbol_once(tmp_path: Path) -> None:
    target, meta, rows = write_archive(tmp_path, DAY, "0930")
    rows[0].update(price="", quote_date="invalid")
    target, meta, _ = write_archive(tmp_path, DAY, "0930", rows=rows)
    meta.update(price_missing_count=1, quote_date_mismatch_count=1, quote_date_invalid_count=1)
    write_meta(target, meta)
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert (result["good"], result["min_ratio"]) == (1, 0.99)


def test_missing_and_unexpected_symbols_use_frozen_roster_denominator(tmp_path: Path) -> None:
    _, _, rows = write_archive(tmp_path, DAY, "0930")
    rows[0]["symbol"], rows[1]["symbol"] = "900001", "900002"
    write_archive(tmp_path, DAY, "0930", rows=rows)
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert (result["good"], result["min_ratio"]) == (0, 0.98)
    write_archive(tmp_path, DAY, "0930", rows=rows[2:])
    assert owner_view.snapshot_sheet_quality(tmp_path, DAY)["min_ratio"] == 0.98


def test_only_regular_48_slots_count_and_partial_fields_are_allowed(tmp_path: Path) -> None:
    for slot in SLOTS[:45] + ["0931", "0960", "0000", "1500"]:
        write_archive(tmp_path, DAY, slot)
    folder = tmp_path / "data" / "snapshots" / DAY
    (folder / "manifest.json").write_text(json.dumps({"field_verified_complete_slot_count": 0,
                                                    "collected_slot_count": 99}), encoding="utf-8")
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert (result["sheets"], result["good"], result["ok"]) == (45, 45, False)
    write_archive(tmp_path, DAY, SLOTS[45], legacy=True, bad=1)
    result = owner_view.snapshot_sheet_quality(tmp_path, DAY)
    assert (result["good"], result["ok"]) == (46, True)
    assert snapshot_history._slot_evidence(tmp_path, DAY, SLOTS[45])["status"] == "partial"


@pytest.mark.parametrize("day", [DAY + "/../" + DAY, DAY + "garbage", "2026-13-04", ""])
def test_noncanonical_dates_are_rejected(tmp_path: Path, day: str) -> None:
    write_archive(tmp_path, DAY, "0930")
    assert owner_view.snapshot_sheet_quality(tmp_path, day)["sheets"] == 0


def test_health_dry_run_is_read_only_and_uses_verified_owner_count(tmp_path: Path, capsys) -> None:
    source = Path(__file__).resolve().parents[1] / "scripts/ops/write_health_line.py"
    spec = importlib.util.spec_from_file_location("health_a03", source)
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    status = tmp_path / "STATUS.md"
    status.write_text("# STATUS\nKEEP\n", encoding="utf-8")
    for slot in SLOTS[:46]:
        write_archive(tmp_path, DAY, slot, bad=1)
    def files():
        return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in tmp_path.rglob("*") if p.is_file()}
    before = files()
    assert health.main(["--root", str(tmp_path), "--target-date", DAY, "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "快照 ✓46/48" in payload["owner_line"]
    assert files() == before
    for path in (tmp_path / "data/snapshots" / DAY).glob("*.csv"):
        path.write_bytes(b"")
    before = files()
    assert health.main(["--root", str(tmp_path), "--target-date", DAY, "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "快照 ✗0/48" in payload["owner_line"]
    assert files() == before
