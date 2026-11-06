from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from quantdesk import full_a_daily_coverage as coverage
from quantdesk import full_a_daily_reconciliation as reconciliation


DAY = "2026-11-03"
HEADER = "date,symbol,open,high,low,close,volume,amount,adjust\n"
BAR = f"{DAY},000001,10,11,9,10,100,1000,raw\n"


def project(tmp_path: Path, content: bytes | str, *, second_symbol: bool = False) -> Path:
    root = tmp_path / "project"
    daily = root / "data/daily"
    daily.mkdir(parents=True)
    raw = content.encode("utf-8") if isinstance(content, str) else content
    (daily / "000001.csv").write_bytes(raw)
    if second_symbol:
        (daily / "000002.csv").write_text(HEADER + BAR.replace("000001", "000002"), encoding="utf-8")
    universe = root / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol,name\n000001,first\n" + ("000002,second\n" if second_symbol else ""), encoding="utf-8")
    return root


def test_audit_validates_each_unchanged_daily_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quantdesk import price_basis

    root = project(tmp_path, HEADER + BAR, second_symbol=True)
    validated: list[str] = []
    opened: list[str] = []
    original_validate = price_basis.validate_price_frame_set
    original_open = Path.open

    def validate(frames, **kwargs):
        validated.extend(frames)
        return original_validate(frames, **kwargs)

    def open_path(path, *args, **kwargs):
        if path.parent == root / "data/daily" and path.suffix == ".csv":
            opened.append(path.stem)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(price_basis, "validate_price_frame_set", validate)
    monkeypatch.setattr(Path, "open", open_path)
    result = reconciliation.run_full_a_daily_reconciliation(
        project_root=root, target_date=DAY, audit_only=True,
        update_runner=lambda **kwargs: pytest.fail("read-only audit called a provider"),
    )

    assert result["audit_only"] is True
    assert result["raw_coverage"]["valid_raw_target_trading_bar_count"] == 2
    assert sorted(validated) == ["000001", "000002"]
    assert len(opened) <= 6  # content hash, suspension tail, content hash per file


@pytest.mark.parametrize("content", [
    HEADER + BAR,
    HEADER + "2026-11-02,000001,10,11,9,10,100,1000,qfq\n" + BAR,
    HEADER + "2026-11-02,000001,10,11,9,10,100,1000,raw\n" * 2 + BAR,
    HEADER + BAR * 2,
    HEADER + BAR.replace(DAY, "2026-11-04"),
    HEADER + BAR.replace(",10,11,9,10,", ",0,11,9,10,"),
    HEADER + BAR.replace(",100,1000,", ",nan,1000,"),
    (HEADER + BAR).replace("date,", "trade_date,"),
    (HEADER + BAR).replace("date,", " DATE ,"),
    HEADER + BAR.replace(DAY, "abcdefghijk"),
    (HEADER + BAR).replace("\n", "\r\n"),
    (HEADER + BAR).replace("\n", "\r"),
    HEADER + BAR.replace("000001,", '"000001\r\n",'),
    HEADER,
    "",
    b"date,symbol,adjust\n\xff,000001,raw\n",
])
def test_cached_audit_preserves_complete_results(tmp_path: Path, content: bytes | str) -> None:
    root = project(tmp_path, content)
    expected_partition = reconciliation.build_reconciliation_partition(root, target_date=DAY)
    kwargs = {"target_date": DAY, "universe_path": root / "data/universe/a_share_symbols.csv", "daily_dir": root / "data/daily"}
    expected_coverage = coverage.build_full_a_daily_coverage(**kwargs)
    cache = coverage.DailyAuditCache()

    assert reconciliation.build_reconciliation_partition(root, target_date=DAY, inspection_cache=cache) == expected_partition
    assert coverage.build_full_a_daily_coverage(**kwargs, inspection_cache=cache) == expected_coverage


def test_changed_bytes_invalidate_even_with_same_size_and_mtime(tmp_path: Path) -> None:
    root = project(tmp_path, HEADER + BAR)
    path = root / "data/daily/000001.csv"
    cache = coverage.DailyAuditCache()
    before = cache.inspect(path, target_date=DAY)
    stamp = path.stat()
    raw = path.read_bytes().replace(b",raw\n", b",qfq\n")
    path.write_bytes(raw)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size

    after = cache.inspect(path, target_date=DAY)
    assert before.target_row["valid_trading_bar"] is True
    assert after.target_row["valid_trading_bar"] is False
    assert after.target_row["reason_code"] == "price_basis_untrusted"
    assert before.source_sha256 != after.source_sha256
    assert after.target_row == coverage.inspect_target_daily_row(path, target_date=DAY)


def test_cache_is_scoped_by_path_and_target_and_cannot_be_poisoned(tmp_path: Path) -> None:
    root = project(tmp_path, HEADER + BAR, second_symbol=True)
    cache = coverage.DailyAuditCache()
    path = root / "data/daily/000001.csv"
    first = cache.inspect(path, target_date=DAY)
    first.target_row["valid_trading_bar"] = False
    first.target_row["price_policy_validation"].clear()
    assert cache.inspect(path, target_date=DAY).target_row["valid_trading_bar"] is True
    assert cache.inspect(path, target_date="2026-11-02").target_row["target_row_present"] is False
    other = root / "data/daily/000002.csv"
    assert cache.inspect(other, target_date=DAY).target_row["source_path"] == str(other)


def test_removal_and_replacement_cannot_reuse_old_verdict(tmp_path: Path) -> None:
    root = project(tmp_path, HEADER + BAR)
    path = root / "data/daily/000001.csv"
    cache = coverage.DailyAuditCache()
    assert cache.inspect(path, target_date=DAY).target_row["valid_trading_bar"] is True
    path.unlink()
    missing = cache.inspect(path, target_date=DAY)
    assert missing.target_row["target_row_present"] is False
    assert missing.target_row["reason_code"] == "daily_read_failed:FileNotFoundError"
    path.write_text(HEADER + BAR.replace(DAY, "2026-11-02"), encoding="utf-8")
    assert cache.inspect(path, target_date=DAY).target_row["target_row_present"] is False


def test_audits_do_not_keep_process_wide_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quantdesk import price_basis

    root = project(tmp_path, HEADER + BAR)
    original = price_basis.validate_price_frame_set
    calls: list[str] = []

    def validate(frames, **kwargs):
        calls.extend(frames)
        return original(frames, **kwargs)

    monkeypatch.setattr(price_basis, "validate_price_frame_set", validate)
    for _ in range(2):
        reconciliation.run_full_a_daily_reconciliation(project_root=root, target_date=DAY, audit_only=True)
    assert calls == ["000001", "000001"]


def test_provider_update_path_does_not_use_audit_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project(tmp_path, HEADER + BAR.replace(DAY, "2026-11-02"))
    path = root / "data/daily/000001.csv"

    def unexpected_cache(*args, **kwargs):
        pytest.fail("mutable update path must retain its independent inspections")

    def update(**kwargs):
        assert kwargs["symbols"] == ["000001"]
        path.write_text(HEADER + BAR, encoding="utf-8")
        return {"updated_symbol_list": ["000001"]}

    monkeypatch.setattr(reconciliation, "DailyAuditCache", unexpected_cache)
    result = reconciliation.run_full_a_daily_reconciliation(
        project_root=root, target_date=DAY, network=True, apply=True,
        update_runner=update,
        health_refresher=lambda **kwargs: {"data_alerts": {"current": [], "resolved": []}},
        alert_dispatcher=lambda *args, **kwargs: {"status": "skipped"},
    )
    assert result["status"] == "completed"
    assert result["retry_symbols"] == []


def test_csv_parser_error_preserves_coverage_failure(tmp_path: Path) -> None:
    root = project(tmp_path, HEADER + BAR.replace("000001", "x" * (csv.field_size_limit() + 1)))
    kwargs = {"target_date": DAY, "universe_path": root / "data/universe/a_share_symbols.csv", "daily_dir": root / "data/daily"}
    old = coverage.build_full_a_daily_coverage(**kwargs)
    cache = coverage.DailyAuditCache()
    assert reconciliation.build_reconciliation_partition(root, target_date=DAY, inspection_cache=cache) == reconciliation.build_reconciliation_partition(root, target_date=DAY)
    assert coverage.build_full_a_daily_coverage(**kwargs, inspection_cache=cache) == old
    assert old["invalid_symbols_sample"] == ["000001:Error"]
