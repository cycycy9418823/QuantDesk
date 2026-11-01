"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


from __future__ import annotations


from datetime import datetime


import hashlib


import io


import json


import os


from pathlib import Path


import sys


import zipfile


import pandas as pd


import pytest


@pytest.fixture
def tmp_path(tmp_path_factory):
    # The review root is long; a receipt plus its atomic temporary suffix must
    # not fail merely because pytest copied the full test name into the path.
    return tmp_path_factory.mktemp("r")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def save_observation(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class FixedTime(datetime):
    value = datetime(2026, 9, 14, 18, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.value.replace(tzinfo=tz) if tz is not None else cls.value


def report_environment(tmp_path, monkeypatch):
    from tests.test_core_logic import dqr, sample_history, sample_spot
    from tests.test_calendar_source_unification import _write_offline_calendar
    from quantdesk import market_clock, trading_calendar, word_report

    root = tmp_path / "scenario"
    for name, relative in {
        "RAW_DIR": "data/raw", "DAILY_DIR": "data/daily", "NEWS_DIR": "data/news",
        "CACHE_DIR": "data/cache", "STATE_DIR": "state", "REPORT_DIR": "reports", "LOG_DIR": "logs",
    }.items():
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dqr, name, path)
    monkeypatch.setattr(dqr, "LONG_POOL_PATH", root / "state/long_pool.csv")
    monkeypatch.setattr(dqr, "datetime", FixedTime)
    monkeypatch.setattr(word_report, "datetime", FixedTime)
    calendar = root / "data/calendar/a_share_trading_calendar.csv"
    _write_offline_calendar(calendar)
    monkeypatch.setattr(trading_calendar, "DEFAULT_TRADING_CALENDAR_CSV", calendar)
    monkeypatch.setattr(market_clock, "DEFAULT_CN_TRADING_CALENDAR_PATH", calendar)
    trading_calendar.clear_calendar_cache()
    dqr.DATA_HEALTH_ROWS.clear()
    dqr.NEWS_HEALTH_ROWS.clear()
    spot_row = sample_spot()
    feature = dqr.compute_base_features("000001", "independent_fixture", sample_history(), spot_row)
    features = dqr.finalize_scores(pd.DataFrame([feature]))
    spot = pd.DataFrame([spot_row])
    monkeypatch.setattr(sys, "argv", ["daily_quant_report.py", "--top-n", "1", "--no-learning-appendix"])
    args = dqr.parse_args()
    metrics = dqr.BacktestMetrics(annual_return=.1, total_return=.1, max_drawdown=-.05,
        sharpe=1, win_rate=.5, days=180, final_equity=110000)
    return dqr, root, features, spot, metrics, args


@pytest.mark.parametrize("stage", ["normal", "markdown_open", "dated_meta_replace", "latest_meta_replace", "word_open"])
def test_actual_report_save_chain_preserves_a_coherent_last_success(tmp_path, monkeypatch, stage):
    dqr, root, features, spot, metrics, args = report_environment(tmp_path, monkeypatch)
    FixedTime.value = datetime(2026, 9, 14, 18, 0, 0)
    dqr.write_report(features, spot, metrics, args)
    reports = root / "reports"
    paths = {"md": reports / "2026-09-14_quant_report.md",
        "word": reports / "2026-09-14_quant_report.docx",
        "dated_meta": reports / "2026-09-14_quant_report_meta.json",
        "latest_meta": reports / "latest_quant_report_meta.json"}
    before = {name: path.read_bytes() for name, path in paths.items()}
    assert zipfile.is_zipfile(paths["word"]), "positive producer must have saved a real Word archive"
    FixedTime.value = datetime(2026, 9, 14, 18, 5, 0)
    actual_open, actual_replace = io.open, os.replace
    fired = []
    def open_with_fault(file, mode="r", *args, **kwargs):
        target = paths["md"] if stage == "markdown_open" else paths["word"] if stage == "word_open" else None
        if target is not None and isinstance(file, (str, Path)) and Path(file).resolve() == target.resolve() and any(c in mode for c in "wax+"):
            fired.append({"stage": stage, "event": "io.open", "path": str(file), "mode": mode})
            raise OSError("injected_actual_report_open_failure:" + stage)
        return actual_open(file, mode, *args, **kwargs)
    def replace_with_fault(src, dst, *args, **kwargs):
        target = paths["dated_meta"] if stage == "dated_meta_replace" else paths["latest_meta"] if stage == "latest_meta_replace" else None
        if target is not None and Path(dst).resolve() == target.resolve():
            fired.append({"stage": stage, "event": "os.replace", "source": str(src), "destination": str(dst)})
            raise OSError("injected_actual_report_replace_failure:" + stage)
        return actual_replace(src, dst, *args, **kwargs)
    error = None
    with pytest.MonkeyPatch.context() as faults:
        faults.setattr(io, "open", open_with_fault)
        faults.setattr(os, "replace", replace_with_fault)
        try:
            dqr.write_report(features, spot, metrics, args)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
    after = {name: path.read_bytes() if path.is_file() else None for name, path in paths.items()}
    changed = [name for name in paths if before[name] != after[name]]
    metas = {name: json.loads(paths[name].read_text(encoding="utf-8-sig")) for name in ("dated_meta", "latest_meta")}
    save_observation(tmp_path / "report_save_observation.json", {"stage": stage, "error": error,
        "actual_io_faults": fired, "changed_artifacts": changed, "metadata": metas,
        "before_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in before.items()},
        "after_sha256": {n: digest(p) for n, p in paths.items()},
        "report_files": sorted(str(p.relative_to(root)) for p in reports.rglob("*") if p.is_file()),
        "entry": "scripts/daily_quant_report.py::write_report -> real build_word_report -> real python-docx save"})
    if stage == "normal":
        assert error is None, error
        assert {"md", "word", "dated_meta", "latest_meta"} <= set(changed)
        assert metas["dated_meta"] == metas["latest_meta"]
    else:
        assert fired and error and error["type"] in {"OSError", "RuntimeError"} and "injected_actual_report_" in error["message"], {"error": error, "fired": fired}
        # A failed save may safely leave all old artifacts intact. Publishing a
        # new MD/meta beside the old Word without a per-attempt failure state
        # does not satisfy the existing same-run/old-success contract.
        if changed:
            failed_metadata = any(m.get("refresh_success") is False or m.get("status") in {"failed", "publish_failed"} for m in metas.values())
            failure_receipts = []
            for path in root.rglob("*.json"):
                try:
                    receipt = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                if isinstance(receipt, dict) and receipt.get("failed_steps") and (receipt.get("status") in {"failed", "publish_failed"} or receipt.get("refresh_success") is False):
                    failure_receipts.append(str(path))
            assert failed_metadata or failure_receipts, {"stage": stage, "changed": changed, "metadata": metas}
