from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE / "write_health_line.py"
if not DEFAULT_SOURCE.is_file():
    DEFAULT_SOURCE = HERE.parent / "scripts/ops/write_health_line.py"
SOURCE = Path(os.environ.get("QD_HEALTH_SOURCE", DEFAULT_SOURCE))
DAY = "2026-09-30"
RUN = "daily-data-chain-20260930T104141Z-fixture"
PROVIDER = "akshare.stock_tfp_em"


def load():
    spec = importlib.util.spec_from_file_location("health_display", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def bar(root, code, day=DAY):
    path = root / "data/daily" / f"{code}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,symbol,open,high,low,close,volume,adjust\n"
                    f"{day},{code},10,11,9,10,100,raw\n", encoding="utf-8")


def scenario(root, *, codes=None, present=None, listed=None):
    codes = codes or ["000001", "000002", "000003"]
    present = ["000001"] if present is None else present
    listed = ["000002", "000003"] if listed is None else listed
    roster = root / "data/universe/a_share_symbols.csv"
    roster.parent.mkdir(parents=True)
    roster.write_text("symbol\n" + "\n".join(codes) + "\n", encoding="utf-8")
    for code in present:
        bar(root, code)
    run_dir = root / "results/data_chain/runs" / RUN
    provider = {"source": PROVIDER, "as_of": DAY, "status": "success", "read_ok": True,
                "network_enabled": True, "sha256": "a" * 64, "count": len(listed),
                "matched_count": len(listed), "normalizable_count": len(listed), "error": ""}
    rows = {}
    for code in codes:
        row = {"symbol": code, "classification": "expected_trade" if code in present else
               "verified_full_day_suspension" if code in listed else "unknown", "evidence": []}
        if code in listed:
            row["evidence"].append({"source": PROVIDER, "symbol": code,
                                    "target_date": DAY, "status": "returned_code"})
        rows[code] = row
    session = {"schema_version": "session_expectation.v1", "target_date": DAY,
               "provider_date": DAY.replace("-", ""), "network_enabled": True,
               "project_root": str(root), "output_path": str(run_dir / "session_expectation.json"),
               "symbols": codes, "per_symbol": rows, "source_evidence": {"provider": provider}}
    manifest = {"task_type": "daily_data_chain", "authoritative": True, "target_date": DAY,
                "run_id": RUN, "pipeline_run_id": "data-close-fixture", "status": "completed",
                "scope": {"research_universe_source_path": "data/universe/a_share_symbols.csv",
                          "research_universe_source_sha256": hashlib.sha256(roster.read_bytes()).hexdigest()},
                "components": {"session_expectation": session}}
    latest = {key: manifest[key] for key in ("task_type", "authoritative", "target_date", "run_id", "pipeline_run_id", "status")}
    latest["manifest_path"] = f"results/data_chain/runs/{RUN}/manifest.json"
    put(root / "results/data_chain/daily_data_chain_latest.json", latest)
    put(run_dir / "manifest.json", manifest)
    put(run_dir / "session_expectation.json", session)
    return latest, manifest, session, run_dir


def save(root, objects):
    latest, manifest, session, run_dir = objects
    put(root / "results/data_chain/daily_data_chain_latest.json", latest)
    put(run_dir / "manifest.json", manifest)
    put(run_dir / "session_expectation.json", session)


def test_roster_intersection_reproduces_ten_vs_eleven(tmp_path):
    codes = [f"{i:06d}" for i in range(12)]
    scenario(tmp_path, codes=codes, present=[codes[0]], listed=codes[1:])
    bar(tmp_path, "999999")
    cell, extra = load().daily_cell(tmp_path, DAY)
    assert "1/12(缺11)" in cell
    assert "供应商停牌名单命中11" in cell + extra
    assert "全天状态待核" in cell + extra
    assert "12/12" not in cell


@pytest.mark.parametrize("fault", ["stale", "wrong_run", "failed", "empty", "wrong_provider_date", "roster_changed", "bad_json", "row_wrong_day", "sidecar_mismatch"])
def test_untrusted_receipt_stays_unknown(tmp_path, fault):
    objects = scenario(tmp_path)
    latest, manifest, session, run_dir = objects
    provider = session["source_evidence"]["provider"]
    if fault == "stale":
        latest["target_date"] = "2026-09-29"
    elif fault == "wrong_run":
        manifest["run_id"] = "older-run"
    elif fault == "failed":
        provider["status"] = "failed"
    elif fault == "empty":
        provider.update(status="empty", count=0)
    elif fault == "wrong_provider_date":
        provider["as_of"] = "2026-09-29"
    elif fault == "row_wrong_day":
        session["per_symbol"]["000002"]["evidence"][0]["target_date"] = "2026-09-29"
        session["per_symbol"]["000003"]["evidence"][0]["target_date"] = "2026-09-29"
    save(tmp_path, objects)
    if fault == "roster_changed":
        (tmp_path / "data/universe/a_share_symbols.csv").write_text("symbol\n000001\n000002\n000004\n", encoding="utf-8")
    elif fault == "bad_json":
        (run_dir / "manifest.json").write_text("{", encoding="utf-8")
    elif fault == "sidecar_mismatch":
        other = dict(session, target_date="2026-09-29")
        put(run_dir / "session_expectation.json", other)
    cell, extra = load().daily_cell(tmp_path, DAY)
    assert "缺2" in cell
    assert "原因未核验" in extra
    assert "名单命中2" not in cell + extra


def test_raw_trade_overrides_list_and_reports_conflict(tmp_path):
    scenario(tmp_path, present=["000001", "000002"], listed=["000002", "000003"])
    cell, extra = load().daily_cell(tmp_path, DAY)
    assert "2/3(缺1)" in cell
    assert "供应商停牌名单命中1" in cell + extra
    assert "冲突1" in extra and "000002" in extra


def test_resumption_does_not_reuse_yesterday_suspension(tmp_path):
    scenario(tmp_path, present=["000001", "000002"], listed=["000002", "000003"])
    bar(tmp_path, "000003", "2026-10-08")
    cell, extra = load().daily_cell(tmp_path, "2026-10-08")
    assert "1/3(缺2)" in cell
    assert "原因未核验" in extra
    assert "名单命中" not in cell + extra


def test_missing_evidence_never_becomes_suspension(tmp_path):
    scenario(tmp_path)
    (tmp_path / "results/data_chain/daily_data_chain_latest.json").unlink()
    cell, extra = load().daily_cell(tmp_path, DAY)
    assert "1/3(缺2)" in cell
    assert "原因未核验" in extra


def test_no_writes_to_observed_inputs(tmp_path):
    scenario(tmp_path)
    before = {p.relative_to(tmp_path): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in tmp_path.rglob("*") if p.is_file()}
    load().daily_cell(tmp_path, DAY)
    after = {p.relative_to(tmp_path): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--adjacent", action="store_true")
    parser.add_argument("--red", action="store_true")
    args = parser.parse_args()
    os.environ["QD_HEALTH_SOURCE"] = str(args.source)

    class SourcePlugin:
        def pytest_collection_modifyitems(self, items):
            for item in items:
                if item.module.__name__ == "test_ops_write_health_line":
                    item.module.SCRIPT = args.source

    targets = [str(__file__)]
    if args.adjacent:
        adjacent = HERE / "test_ops_write_health_line.py"
        if not adjacent.is_file():
            adjacent = HERE.parent / "candidate/tests/test_ops_write_health_line.py"
        targets.append(str(adjacent))
    if args.red:
        targets += ["-k", "roster_intersection"]
    raise SystemExit(pytest.main([*targets, "-q", "--tb=short", "-p", "no:cacheprovider",
                                 "--basetemp", str(Path.cwd() / "tmp")], plugins=[SourcePlugin()]))
