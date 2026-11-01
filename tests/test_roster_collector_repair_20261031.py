"""Offline refresh-to-collector proofs; every write belongs to tmp_path."""
from __future__ import annotations

import hashlib
import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import requests

from quantdesk import snapshot_history as history
from quantdesk.full_a_daily_coverage import load_roster_evidence, prepare_roster_version
from quantdesk.process_deadline import ProcessDeadlineResult
from scripts import refresh_universe_baseline as publisher
from scripts import update_a_share_universe as updater


SOURCE = "sina.spot_direct"


def _reader_case(root: Path) -> SimpleNamespace:
    def publish(code: str, day: str, run: str, pointer: str) -> dict:
        prepared = prepare_roster_version([{"symbol": code, "name": "fixture"}],
            source=SOURCE, as_of=day, run_id=run, accepted_at=day + "T08:00:00+08:00")
        version = root / prepared["manifest"]["path"]
        version.parent.mkdir(parents=True, exist_ok=True)
        version.write_bytes(prepared["csv_bytes"])
        raw = json.dumps(prepared["manifest"]).encode()
        (root / "data/universe" / pointer).write_bytes(raw)
        return {**prepared, "pointer_bytes": raw, "version": version}

    old = publish("600000", "2026-10-12", "reader_old", "a_share_roster_last_accepted.json")
    latest = publish("600001", "2026-10-30", "reader_latest", "a_share_roster_latest.json")
    replacement = publish("600002", "2026-10-30", "reader_replacement", "unused_fixture_pointer.json")
    return SimpleNamespace(root=root, old=old, latest=latest, replacement=replacement,
        pointer=root / "data/universe/a_share_roster_latest.json")


def _replace_after_first_read(monkeypatch, target: Path, replacement: bytes) -> list[bytes]:
    original_open = Path.open
    reads = []

    def open_path(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path != target or "r" not in mode:
            return original_open(path, *args, **kwargs)
        with original_open(path, "rb") as source:
            raw = source.read()
        reads.append(raw)
        if len(reads) == 1:
            with original_open(path, "wb") as output:
                output.write(replacement)
        return io.BytesIO(raw) if "b" in mode else io.StringIO(raw.decode(kwargs.get("encoding") or "utf-8"))

    monkeypatch.setattr(Path, "open", open_path)
    return reads


def test_roster_pointer_metadata_and_hash_describe_one_read(tmp_path, monkeypatch):
    case = _reader_case(tmp_path)
    reads = _replace_after_first_read(monkeypatch, case.pointer, case.replacement["pointer_bytes"])
    result = load_roster_evidence(tmp_path, target_date="2026-10-30")
    assert result["symbols"] == ["600001"]
    assert result["pointer_sha256"] == _hash(case.latest["pointer_bytes"])
    assert len(reads) == 1


def test_roster_version_rows_and_hash_describe_one_read(tmp_path, monkeypatch):
    case = _reader_case(tmp_path)
    reads = _replace_after_first_read(monkeypatch, case.latest["version"], case.replacement["csv_bytes"])
    result = load_roster_evidence(tmp_path, target_date="2026-10-30")
    assert result["symbols"] == ["600001"] and not result["fallback"]
    assert result["sha256"] == case.latest["manifest"]["sha256"]
    assert len(reads) == 1


def test_legacy_roster_rows_date_and_hash_describe_one_read(tmp_path, monkeypatch):
    legacy = tmp_path / "data/universe/a_share_symbols.csv"
    legacy.parent.mkdir(parents=True)
    original = b"symbol,source,updated_at\n600000,fixture,2026-10-12\n"
    legacy.write_bytes(original)
    reads = _replace_after_first_read(monkeypatch, legacy,
        b"symbol,source,updated_at\n600001,fixture,2026-10-30\n")
    result = load_roster_evidence(tmp_path, target_date="2026-10-30")
    assert result["symbols"] == ["600000"] and result["as_of"] == "2026-10-12"
    assert result["sha256"] == _hash(original) and result["accepted"] is None
    assert len(reads) == 1


@pytest.mark.parametrize("defect", ["source", "as_of", "updated_at",
    "duplicate_symbol", "invalid_extra_symbol", "missing_run_id"])
def test_versioned_roster_requires_matching_row_identity(tmp_path, defect):
    case = _reader_case(tmp_path)
    meta = dict(case.latest["manifest"])
    rows = list(csv.DictReader(io.StringIO(case.latest["csv_bytes"].decode())))
    if defect in {"source", "as_of", "updated_at"}:
        rows[0][defect] = {"source": "wrong_provider", "as_of": "2026-10-31",
            "updated_at": "2026-10-31T08:00:00+08:00"}[defect]
    elif defect == "duplicate_symbol":
        rows.append(dict(rows[0]))
    elif defect == "invalid_extra_symbol":
        rows.append({**rows[0], "symbol": "bad"})
    else:
        meta["run_id"] = ""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    case.latest["version"].write_bytes(raw)
    meta["sha256"] = _hash(raw)
    case.pointer.write_text(json.dumps(meta), encoding="utf-8")
    result = load_roster_evidence(tmp_path, target_date="2026-10-30")
    assert result["symbols"] == ["600000"]
    assert result["fallback"] and result["age_days"] == 18 and result["reason_codes"]
    assert result["accepted_at"] == case.old["manifest"]["accepted_at"]


@pytest.mark.parametrize("terminal_fails", [False, True])
def test_one_connection_finishes_pagination_inside_the_original_window(monkeypatch, terminal_fails):
    # A deterministic transport charges 0.35s per new connection and 0.03s
    # per page. The real Requests session/response path is used, with no socket.
    clock = [100.0]
    sessions = []
    pages = []
    closed = []
    original_close = requests.Session.close

    def send(session, request, **kwargs):
        assert request.url.startswith("https://vip.stock.finance.sina.com.cn/")
        if session not in sessions:
            sessions.append(session)
            clock[0] += 0.35
        clock[0] += 0.03
        assert 0 < kwargs["timeout"] <= 5
        params = parse_qs(urlparse(request.url).query)
        page, width = int(params["page"][0]), int(params["num"][0])
        assert width == 100
        pages.append(page)
        if terminal_fails and page == 56:
            raise requests.exceptions.ReadTimeout("fixture terminal page unavailable")
        first = (page - 1) * width
        payload = [{"code": str(600000 + i), "name": "fixture"}
                   for i in range(first, min(first + width, 5500))]
        response = requests.Response()
        response.status_code, response.url = 200, request.url
        response._content = json.dumps(payload).encode()
        return response

    def close(session):
        closed.append(session)
        original_close(session)

    monkeypatch.setattr(requests.sessions.Session, "send", send)
    monkeypatch.setattr(requests.sessions.Session, "close", close)
    monkeypatch.setattr(updater.time, "monotonic", lambda: clock[0])
    result = updater._universe_source_worker(SOURCE, {"fetch_deadline_monotonic": 118.0})
    assert pages == list(range(1, 57))
    assert len(sessions) == 1 and closed == sessions
    assert clock[0] < 118.0 and len(result["rows"]) == 5500
    assert result["response_complete"] is (not terminal_fails)
    assert result["pagination_complete"] is (not terminal_fails)
    if terminal_fails:
        assert "terminal page unavailable" in result["provider_warning"]


def test_normalization_preserves_numeric_codes_when_other_columns_are_floats():
    frame = pd.DataFrame({"code": [1, 600000], "price": [1.1, 2.2]})
    assert updater.normalize_universe_frame(frame, SOURCE) == [
        {"symbol": "000001", "name": "", "source": SOURCE},
        {"symbol": "600000", "name": "", "source": SOURCE},
    ]


def test_normalization_retains_first_name_and_leading_zero_aliases():
    frame = pd.DataFrame({"证券代码": ["sz.000001", "000001", "600000", "bad"],
                          "证券简称": ["first", "second", "third", "ignored"]})
    assert updater.normalize_universe_frame(frame, SOURCE) == [
        {"symbol": "000001", "name": "first", "source": SOURCE},
        {"symbol": "600000", "name": "third", "source": SOURCE},
    ]


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def refresh_case(tmp_path, monkeypatch):
    root = tmp_path / "refresh"
    universe = root / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts/update_a_share_universe.py").write_text("# injected local entry\n")
    (root / "config").mkdir()
    (root / "config/weekly_daily_wide_pool_contract.json").write_text(json.dumps({
        "rules": {"approved_market_universe_sources": [SOURCE]},
    }))
    today = datetime.now(updater.SHANGHAI).date()
    old_day = (today - timedelta(days=7)).isoformat()
    old_rows = [{"symbol": str(600000 + i), "name": "old"} for i in range(5500)]
    universe.write_text("symbol,name,source,updated_at\n" + "".join(
        f"{row['symbol']},old,{SOURCE},{old_day}T08:00:00+08:00\n" for row in old_rows))
    old = prepare_roster_version(old_rows, source=SOURCE, as_of=old_day,
        run_id="previous_fixture", accepted_at=old_day + "T08:00:00+08:00")
    version = root / old["manifest"]["path"]
    version.parent.mkdir()
    version.write_bytes(old["csv_bytes"])
    for name in ("a_share_roster_latest.json", "a_share_roster_last_accepted.json"):
        (universe.parent / name).write_text(json.dumps(old["manifest"]))
    case = SimpleNamespace(root=root, universe=universe, today=today.isoformat(), old=old,
                           tamper="", exit_code=None, commands=[])
    case.before = {p: p.read_bytes() for p in universe.parent.rglob("*") if p.is_file()}

    def runner(worker, tasks, **kwargs):
        provider = tasks[0]
        if provider != SOURCE:
            return ProcessDeadlineResult({}, {0: "offline other source unavailable"}, [], False, [])
        rows = [{"symbol": str(600000 + i), "name": "new", "source": SOURCE} for i in range(5509)]
        payload = {"rows": rows, "response_complete": True, "pagination_complete": True,
                   "completion_evidence": "terminal_page", "observed_pages": 56, "expected_pages": 56}
        return ProcessDeadlineResult({0: payload}, {}, [], False, [])

    monkeypatch.setattr(updater, "_run_roster_source_process", runner)
    monkeypatch.setattr(updater, "PROJECT_ROOT", root)
    monkeypatch.setattr(updater, "DEFAULT_UNIVERSE_PATH", universe)
    monkeypatch.setattr(updater, "DEFAULT_STATUS_PATH", root / "data/cache/a_share_universe_status.json")
    monkeypatch.setattr(updater, "load_config", lambda *_: {})
    monkeypatch.setattr(publisher, "market_writer_subprocess_kwargs", lambda _root: {})
    original_run = publisher.subprocess.run

    def command(argv, **kwargs):
        if len(argv) < 3 or Path(argv[2]) != root / "scripts/update_a_share_universe.py":
            return original_run(argv, **kwargs)
        case.commands.append(argv)
        status = updater.run_update_universe(updater.parse_args(argv[3:]))
        status_path = Path(status["status_path"])
        if case.tamper == "missing_receipt":
            status_path.unlink()
        elif case.tamper:
            if case.tamper == "old_run":
                status["run_id"] = "previous_run"
            elif case.tamper == "wrong_hash":
                status["roster_sha256"] = "0" * 64
            elif case.tamper == "unfinished_pages":
                status["source_snapshots"][0]["pagination_complete"] = False
            elif case.tamper == "wrong_membership":
                status["source_snapshots"][0]["symbols_sha256"] = "0" * 64
            elif case.tamper == "preview":
                status["full_roster_published"] = False
                status["preview"] = True
            elif case.tamper == "unapproved_source":
                universe.write_bytes(universe.read_bytes().replace(SOURCE.encode(), b"not_approved"))
            else:
                raise AssertionError(case.tamper)
            status_path.write_text(json.dumps(status))
        return SimpleNamespace(returncode=case.exit_code if case.exit_code is not None else 0,
                               stdout=json.dumps(status), stderr="")

    monkeypatch.setattr(publisher.subprocess, "run", command)
    return case


def test_guarded_success_publishes_the_exact_roster_that_the_collector_requests(refresh_case, monkeypatch):
    case = refresh_case
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert result["accepted"] and result["roster_publication"]["status"] == "published"
    roster = load_roster_evidence(case.root, target_date=case.today)
    assert roster["identity_status"] == "verified_version" and not roster["fallback"]
    assert roster["as_of"] == case.today and roster["row_count"] == 5509
    assert roster["run_id"] == result["run_id"]
    assert roster["sha256"] == _hash((case.root / roster["path"]).read_bytes())
    budget = float(case.commands[0][case.commands[0].index("--budget-seconds") + 1])
    assert 0 < budget < 35 and result["duration_seconds"] <= 35
    assert "--deadline-monotonic" in case.commands[0]
    assert result["roster_publication"]["updater_receipt_sha256"]
    now = datetime.fromisoformat(case.today + "T10:00:03+08:00")
    monkeypatch.setattr(history, "_read_trading_day", lambda *_: (True, "fixture", []))
    requested = []

    def fetch(symbols):
        requested.extend(symbols)
        return [{"symbol": s, "price": 10, "open": 10, "high": 10, "low": 10,
                 "volume": 100, "amount": 1000, "price_basis": "raw", "quote_date": case.today,
                 "quote_time": "10:00:01"} for s in symbols], "offline_fixture"

    snapshot = history.collect_market_snapshot(case.root, network=True, now=now,
        clock=lambda: now, fetcher=fetch)
    assert snapshot["written"] and requested == roster["symbols"]
    stored = json.loads((case.root / snapshot["metadata_path"]).read_text())
    assert stored["producer_provenance"]["roster_evidence"]["sha256"] == roster["sha256"]
    assert len(stored["code_version"]["source_sha256"]) == 64
    assert (case.root / case.old["manifest"]["path"]).read_bytes() == case.old["csv_bytes"]


@pytest.mark.parametrize("transport_fails", [False, True])
def test_published_roster_sync_keeps_local_and_remote_outcomes_separate(refresh_case, monkeypatch, transport_fails):
    from scripts.ops import sync_snapshot_roster as sync

    case = refresh_case
    remote = case.root.parent / "cloud_fixture"
    remote.mkdir()
    key, hosts = case.root / "key_fixture", case.root / "hosts_fixture"
    key.write_bytes(b"no-real-key")
    hosts.write_bytes(b"no-real-host")
    config = case.root / "config/snapshot_roster_sync.json"
    config.with_name("transport.json").write_text(json.dumps({"network": True, "project_root": str(case.root),
        "remote": "fixture@192.0.2.20", "remote_snapshot_root": "/srv/collector/data/snapshots",
        "ssh_key_path": str(key), "known_hosts_path": str(hosts)}))
    config.write_text(json.dumps({"network": True, "transport_config": "transport.json",
        "receiver_script": "/srv/releases/source-v1/scripts/ops/sync_snapshot_roster.py"}))
    calls = []

    def transport(command, **kwargs):
        calls.append(command)
        if transport_fails:
            raise sync.subprocess.TimeoutExpired(command, kwargs["timeout"])
        receipt = sync.receive_roster_packet(remote, kwargs["input"], apply=True)
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode(), stderr=b"")

    monkeypatch.setattr(publisher, "send_roster",
        lambda root, **kwargs: sync.send_roster(root, run=transport, **kwargs))
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert result["accepted"] and not result["restored_after_failure"]
    assert result["roster_publication"]["status"] == "published" and len(calls) == 1
    assert result["before"]["sha256"] != result["after"]["sha256"]
    assert result["duration_seconds"] <= 35 and result["budget_seconds"] == 35
    assert result["roster_sync"]["budget_seconds"] == 45
    stored = json.loads((case.root / "evidence/universe_baseline_refresh.json").read_text())
    assert stored["roster_sync"] == result["roster_sync"]
    if transport_fails:
        assert result["roster_sync"]["status"] == "unknown"
        assert not (remote / "data/universe").exists()
    else:
        assert result["roster_sync"]["status"] == "synced"
        actual = load_roster_evidence(remote, target_date=case.today)
        assert actual["sha256"] == result["roster_publication"]["manifest"]["sha256"]
        assert actual["run_id"] == result["run_id"] and actual["row_count"] == 5509


@pytest.mark.parametrize("tamper", ["old_run", "wrong_hash", "unfinished_pages", "wrong_membership",
                                     "preview", "missing_receipt", "unapproved_source"])
def test_rejected_refresh_preserves_old_csv_date_and_both_pointers(refresh_case, tamper):
    case = refresh_case
    case.tamper = tamper
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert not result["accepted"] and result["restored_after_failure"]
    for path, raw in case.before.items():
        assert path.read_bytes() == raw, str(path)
    roster = load_roster_evidence(case.root, target_date=case.today)
    assert roster["as_of"] == case.old["manifest"]["as_of"] and roster["age_days"] == 7
    assert len(list((case.universe.parent / "versions").glob("*.csv"))) == 1


def test_updater_nonzero_never_publishes_a_version(refresh_case):
    case = refresh_case
    case.exit_code = 1
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert not result["accepted"] and result["restored_after_failure"]
    assert all(path.read_bytes() == raw for path, raw in case.before.items())


def test_dry_run_never_publishes_or_refreshes_old_dates(refresh_case):
    case = refresh_case
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence", dry_run=True)
    assert result["accepted"] and result["roster_publication"]["status"] == "dry_run"
    assert all(path.read_bytes() == raw for path, raw in case.before.items())


def test_pointer_write_failure_rolls_back_csv_and_previous_pointers(refresh_case, monkeypatch):
    case = refresh_case
    original = publisher.os.replace
    failed = []

    def replace(src, dst):
        if Path(dst).name == "a_share_roster_latest.json" and not failed:
            failed.append(str(dst))
            raise OSError("fixture pointer storage failure")
        return original(src, dst)

    monkeypatch.setattr(publisher.os, "replace", replace)
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert failed and not result["accepted"] and result["restored_after_failure"]
    assert all(path.read_bytes() == raw for path, raw in case.before.items())


def test_corrupt_latest_falls_back_to_the_actual_previous_accepted_version(refresh_case):
    case = refresh_case
    assert publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")["accepted"]
    latest = case.universe.parent / "a_share_roster_latest.json"
    latest.write_text('{"accepted": false}')
    roster = load_roster_evidence(case.root, target_date=case.today)
    assert roster["identity_status"] == "verified_version" and roster["fallback"]
    assert roster["sha256"] == case.old["manifest"]["sha256"] and roster["age_days"] == 7


def test_first_publication_provides_a_verified_fallback_without_relabelling_legacy(refresh_case):
    case = refresh_case
    for name in ("a_share_roster_latest.json", "a_share_roster_last_accepted.json"):
        (case.universe.parent / name).unlink()
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert result["accepted"]
    (case.universe.parent / "a_share_roster_latest.json").write_text("broken")
    roster = load_roster_evidence(case.root, target_date=case.today)
    assert roster["fallback"] and roster["run_id"] == result["run_id"]
    assert roster["as_of"] == case.today and roster["row_count"] == 5509


@pytest.mark.parametrize("late_stage", ["latest_pointer", "readback"])
def test_late_publication_preserves_all_previous_reader_visible_bytes(refresh_case, monkeypatch, late_stage):
    case = refresh_case
    clock = [0.0]
    monkeypatch.setattr(publisher.time, "monotonic", lambda: clock[0])
    original_replace = publisher._replace_bytes
    original_load = publisher.load_roster_evidence
    calls = []

    def replace(path, raw, **kwargs):
        if path.name == "a_share_roster_latest.json" and kwargs.get("deadline") is not None:
            if late_stage == "latest_pointer":
                clock[0] = 36.0
        return original_replace(path, raw, **kwargs)

    def load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        calls.append(result)
        if late_stage == "readback" and len(calls) == 2:
            clock[0] = 36.0
        return result

    monkeypatch.setattr(publisher, "_replace_bytes", replace)
    monkeypatch.setattr(publisher, "load_roster_evidence", load)
    result = publisher.run_refresh(case.root, evidence_dir=case.root / "evidence")
    assert not result["accepted"] and result["restored_after_failure"]
    assert "deadline_exhausted" in " ".join(result["problems"])
    assert all(path.read_bytes() == raw for path, raw in case.before.items())


def test_parent_launch_time_is_charged_without_a_second_publication_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.time, "monotonic", lambda: 102.0)
    monkeypatch.setattr(updater, "load_config", lambda *_: {})
    seen = []

    def fetch(**kwargs):
        seen.append(kwargs["budget_seconds"])
        return updater.UniverseSourceRows(), "", {}

    monkeypatch.setattr(updater, "fetch_universe_from_akshare", fetch)
    args = updater.parse_args(["--dry-run", "--budget-seconds", "35", "--deadline-monotonic", "135",
        "--output", str(tmp_path / "roster.csv"), "--status-path", str(tmp_path / "status.json")])
    result = updater.run_update_universe(args, started_monotonic=102.0)
    assert seen == [31.0]  # 2s of parent launch + one 2s publication reserve.
    assert not result["published"]
