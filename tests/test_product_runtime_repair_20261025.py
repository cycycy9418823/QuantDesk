"""Offline regression evidence for cache, restart, scheduler and backup repairs."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
from threading import Event
import time
from types import SimpleNamespace

import pytest

from quantdesk.daily_scheduler_status import CURRENT_TASK_NAME, _action_contract_kind, daily_data_scheduler_status
from quantdesk.paper_state_backup import build_backup_evidence_closure, pack_backup_evidence, verify_backup_archive
from quantdesk.ui_read_cache import ReadCache, ReadCacheWaitTimeout
from scripts import launcher
from scripts.ops import push_backup_to_student as push


def wrapper_action(root: Path, *, policy: str = "trading_day") -> dict:
    inner = {"executable": str(root / ".venv/Scripts/python.exe"),
             "arguments": f'-B "{root / "scripts/run_data_close_pipeline.py"}" --enable-network --trigger scheduled --enable-alert-network --alert-push',
             "working_directory": str(root)}
    encoded = base64.b64encode(json.dumps(inner).encode()).decode()
    return {"task_name": CURRENT_TASK_NAME, "exists": True, "managed": True, "enabled": True,
            "state": "Ready", "last_task_result": 0,
            "execute": str(root / ".venv/Scripts/pythonw.exe"),
            "arguments": f'-B "{root / "scripts/run_scheduled_task.py"}" --task-name "{CURRENT_TASK_NAME}" --policy {policy} --action-base64 {encoded}',
            "working_directory": str(root), "trigger_count": 2, "enabled_trigger_count": 2}


def replace_inner(raw: dict, change) -> None:
    prefix, encoded = raw["arguments"].rsplit(" ", 1)
    value = json.loads(base64.b64decode(encoded))
    change(value)
    raw["arguments"] = prefix + " " + base64.b64encode(json.dumps(value).encode()).decode()


def test_scheduler_accepts_exact_wrapper_and_keeps_business_result_separate(tmp_path: Path) -> None:
    raw = wrapper_action(tmp_path)
    assert _action_contract_kind(raw, tmp_path) == "data_close_pipeline_wrapper"
    receipt = tmp_path / "results/data_close_pipeline/data_close_pipeline_latest.json"
    receipt.parent.mkdir(parents=True)
    for business_status in ("completed_with_skips", "failed"):
        receipt.write_text(json.dumps({"schema_version": "data_close_pipeline_v1", "target_date": "2026-10-23",
            "run_id": "fixture", "status": business_status, "skipped_stages": ["health_alert"]}), encoding="utf-8")
        result = daily_data_scheduler_status(project_root=tmp_path, platform_name="win32", target_date="2026-10-23",
            runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))
        assert result["automatic_update_configured"] is True
        assert result["last_task_result"] == 0
        assert result["completion_status"] == business_status
        assert result["last_run_succeeded"] is (business_status == "completed_with_skips")


@pytest.mark.parametrize("damage", ["policy", "name", "outer_python", "outer_root", "wrapper_script",
    "inner_python", "inner_root", "inner_flags", "flag_case", "nested_wrapper", "bad_base64", "duplicate_flag", "project_root"])
def test_scheduler_does_not_accept_arbitrary_wrappers(tmp_path: Path, damage: str) -> None:
    raw = wrapper_action(tmp_path)
    if damage == "policy": raw["arguments"] = raw["arguments"].replace("--policy trading_day", "--policy daily")
    elif damage == "name": raw["arguments"] = raw["arguments"].replace(CURRENT_TASK_NAME, "Other Task")
    elif damage == "outer_python": raw["execute"] = str(tmp_path / "elsewhere/pythonw.exe")
    elif damage == "outer_root": raw["working_directory"] = str(tmp_path.parent)
    elif damage == "wrapper_script": raw["arguments"] = raw["arguments"].replace("run_scheduled_task.py", "unknown_wrapper.py")
    elif damage == "inner_python": replace_inner(raw, lambda item: item.update(executable="cmd.exe"))
    elif damage == "inner_root": replace_inner(raw, lambda item: item.update(working_directory=str(tmp_path.parent)))
    elif damage == "inner_flags": replace_inner(raw, lambda item: item.update(arguments=item["arguments"] + " --extra"))
    elif damage == "flag_case": replace_inner(raw, lambda item: item.update(arguments=item["arguments"].replace("--enable-network", "--ENABLE-NETWORK")))
    elif damage == "nested_wrapper": replace_inner(raw, lambda item: item.update(arguments=raw["arguments"]))
    elif damage == "bad_base64": raw["arguments"] = raw["arguments"].rsplit(" ", 1)[0] + " !"
    elif damage == "duplicate_flag": raw["arguments"] += " --policy trading_day"
    elif damage == "project_root": raw["arguments"] += f' --project-root "{tmp_path.parent}"'
    assert _action_contract_kind(raw, tmp_path) == ""


def test_cache_follower_timeout_does_not_cancel_owner_or_cache_failure() -> None:
    cache = ReadCache(wait_timeout_seconds=0.025)
    entered, release = Event(), Event()
    builds = []
    def build():
        builds.append(1)
        entered.set()
        assert release.wait(2)
        return {"source": "original"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(cache.get, "same", build)
        assert entered.wait(1)
        started = time.monotonic()
        try:
            with pytest.raises(ReadCacheWaitTimeout, match="ui_read_cache_wait_timeout"):
                cache.get("same", build)
            assert time.monotonic() - started < 0.5
        finally:
            release.set()
        assert owner.result(timeout=1) == {"source": "original"}
    assert cache.get("same", build) == {"source": "original"}
    assert builds == [1]


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_cache_timeout_is_always_finite(seconds: float) -> None:
    with pytest.raises(ValueError): ReadCache(wait_timeout_seconds=seconds)


def test_expired_response_reuses_parsed_file_but_same_stat_rewrite_rebuilds(tmp_path: Path, monkeypatch) -> None:
    from quantdesk import ui_read_cache as cache_module

    clock = [0.0]
    monkeypatch.setattr(cache_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    responses = ReadCache(ttl_seconds=cache_module.UI_READ_CACHE.ttl_seconds)
    files = ReadCache(ttl_seconds=cache_module.UI_FILE_CACHE.ttl_seconds)
    monkeypatch.setattr(cache_module, "UI_FILE_CACHE", files)
    path = tmp_path / "generation.json"
    path.write_text('{"generation":"one"}', encoding="utf-8")
    counts = {"responses": 0, "file_parses": 0}
    def parse_file():
        counts["file_parses"] += 1
        return json.loads(path.read_text(encoding="utf-8"))
    def build_response():
        counts["responses"] += 1
        return {"response_number": counts["responses"],
            "file": cache_module.cached_file_value([path], parse_file, scope="slow-generation-fixture")}
    def read():
        return responses.get("same-response", build_response)
    assert read() == {"response_number": 1, "file": {"generation": "one"}}
    clock[0] += responses.ttl_seconds + 1
    assert read() == {"response_number": 2, "file": {"generation": "one"}}
    assert counts == {"responses": 2, "file_parses": 1}
    # Source hashing must invalidate the longer-lived parse even when size and
    # modification time are restored and its 60-second TTL has not elapsed.
    original_stat = path.stat()
    path.write_text('{"generation":"two"}', encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (original_stat.st_size, original_stat.st_mtime_ns)
    clock[0] += responses.ttl_seconds + 1
    assert read() == {"response_number": 3, "file": {"generation": "two"}}
    assert counts == {"responses": 3, "file_parses": 2}
    clock[0] += files.ttl_seconds + 1
    assert read() == {"response_number": 4, "file": {"generation": "two"}}
    assert counts == {"responses": 4, "file_parses": 3}


def project_process(root: Path) -> dict:
    executable = str(root / ".venv/Scripts/python.exe")
    return {"pid": 8123, "created": "2026-10-25T00:00:00", "executable": executable,
            "argv": [executable, "-B", "-X", "utf8", str(root / "scripts/quant_api_server.py"), "--port", "8000"]}


@pytest.mark.parametrize("damage", ["unknown", "other_script", "other_root", "python_c", "pid_reuse"])
def test_restart_never_kills_unknown_or_unrelated_python(tmp_path: Path, monkeypatch, damage: str) -> None:
    identity = project_process(tmp_path)
    second = identity
    if damage == "unknown": identity = None
    elif damage == "other_script": identity["argv"][4] = str(tmp_path / "scripts/unrelated.py")
    elif damage == "other_root": identity["argv"][4] = str(tmp_path.parent / "scripts/quant_api_server.py")
    elif damage == "python_c": identity["argv"] = [identity["executable"], "-c", "print('fixture')"]
    elif damage == "pid_reuse": second = {**identity, "created": "changed"}
    values = iter([identity, second])
    monkeypatch.setattr(launcher, "_listener_pids", lambda _port: [8123])
    monkeypatch.setattr(launcher, "_process_identity", lambda _pid: next(values))
    monkeypatch.setattr(launcher, "port_is_alive", lambda _port: True)
    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_a, **_k: pytest.fail("unsafe process termination"))
    assert launcher.kill_server_on_port(8000, project_root=tmp_path) is False


def test_explicit_verified_restart_checks_same_process_twice(tmp_path: Path, monkeypatch) -> None:
    identity = project_process(tmp_path)
    calls = []
    monkeypatch.setattr(launcher, "_listener_pids", lambda _port: [8123])
    monkeypatch.setattr(launcher, "_process_identity", lambda pid: calls.append(("identity", pid)) or identity)
    monkeypatch.setattr(launcher, "port_is_alive", lambda _port: False)
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **_k: calls.append(command))
    assert launcher.kill_server_on_port(8000, project_root=tmp_path)
    assert calls == [("identity", 8123), ("identity", 8123), ["taskkill", "/F", "/PID", "8123"]]


@pytest.mark.parametrize("update_on_start", [False, True])
def test_default_launcher_does_not_restart_a_different_build(tmp_path: Path, monkeypatch, update_on_start: bool) -> None:
    monkeypatch.setattr(sys, "argv", ["launcher", "--no-browser", *(["--update-on-start"] if update_on_start else [])])
    monkeypatch.setattr(launcher, "resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "ensure_runtime_dirs", lambda _root: None)
    monkeypatch.setattr(launcher, "skipped_startup_status", lambda *_a: None)
    monkeypatch.setattr(launcher, "port_is_alive", lambda _port: True)
    observed_modes = []
    monkeypatch.setattr(launcher, "server_is_current", lambda *_a, **kwargs: observed_modes.append(kwargs["expected_read_only"]) or False)
    monkeypatch.setattr(launcher, "kill_server_on_port", lambda *_a, **_k: pytest.fail("automatic termination"))
    assert launcher.main() == 2
    assert observed_modes == [not update_on_start]


@pytest.mark.parametrize("expected_read_only,actual_read_only,damage,expected", [
    (True, True, "none", True), (False, False, "none", True),
    (True, False, "none", False), (False, True, "none", False),
    (True, True, "missing", False), (True, True, "mode", False),
    (True, True, "flags", False), (True, True, "data_root", False),
    (True, True, "build", False), (True, True, "entry_hash", False),
    (True, True, "quote_unknown", False), (True, True, "string_boolean", False),
])
def test_current_build_is_reused_only_with_matching_advertised_mode(tmp_path: Path, monkeypatch,
        expected_read_only: bool, actual_read_only: bool, damage: str, expected: bool) -> None:
    import urllib.request

    capabilities = {"ok": True, "read_only": actual_read_only, "mutations_enabled": not actual_read_only,
        "selected_quote_refresh": False, "mode": "read_only_research" if actual_read_only else "local_research_edit",
        "source_identity": {"server_build_id": "fixture-build", "entry_sha256": "a" * 64, "data_root": str(tmp_path)}}
    if damage == "mode": capabilities.pop("mode")
    elif damage == "flags": capabilities["mutations_enabled"] = True
    elif damage == "data_root": capabilities["source_identity"]["data_root"] = str(tmp_path.parent)
    elif damage == "build": capabilities["source_identity"]["server_build_id"] = "different"
    elif damage == "entry_hash": capabilities["source_identity"]["entry_sha256"] = ""
    elif damage == "quote_unknown": capabilities.pop("selected_quote_refresh")
    elif damage == "string_boolean": capabilities["read_only"] = "true"
    requests = []
    class Response:
        def __init__(self, status: int, value: dict): self.status, self.value = status, value
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return json.dumps(self.value).encode("utf-8")
    def fake_open(url, *, timeout):
        requests.append(url)
        assert timeout == 1.5
        if url.endswith("/api/launcher/build-id"):
            return Response(200, {"build_id": "fixture-build"})
        assert url.endswith("/api/runtime-capabilities")
        return Response(404 if damage == "missing" else 200, capabilities)
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(launcher, "server_source_build_id", lambda _root: "fixture-build")
    assert launcher.server_is_current(8000, tmp_path, expected_read_only=expected_read_only) is expected
    assert len(requests) == 2


def test_source_only_helper_keeps_legacy_contract_without_mode_probe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "running_server_build_id", lambda _port: "fixture-build")
    monkeypatch.setattr(launcher, "server_source_build_id", lambda _root: "fixture-build")
    monkeypatch.setattr(launcher, "running_server_capabilities", lambda _port: pytest.fail("legacy caller acquired mode dependency"))
    assert launcher.server_is_current(8000, tmp_path)


@pytest.mark.parametrize("realtime_network,script,flags", [
    (False, "run_research_viewer.py", []),
    (True, "quant_api_server.py", ["--enable-realtime-network"]),
])
def test_launcher_selects_explicit_mode_without_starting_processes(tmp_path: Path, monkeypatch,
        realtime_network: bool, script: str, flags: list[str]) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("run_research_viewer.py", "quant_api_server.py"):
        (scripts / name).write_text("# fixture entry; never execute\n", encoding="utf-8")
    calls = []
    process = SimpleNamespace(stdout=None)
    monkeypatch.setattr(launcher, "resolve_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)) or process)
    monkeypatch.setattr(launcher.threading, "Thread", lambda **_kwargs: SimpleNamespace(start=lambda: None))
    assert launcher.start_server(tmp_path, 8027, realtime_network=realtime_network) is process
    assert calls[0][0] == [sys.executable, str(scripts / script), "--port", "8027", *flags]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert "--enable-selected-quotes" not in calls[0][0]


def test_missing_readonly_entry_cannot_fall_back_to_writable_api(tmp_path: Path, monkeypatch, capsys) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "quant_api_server.py").write_text("# legacy fixture\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "resolve_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_a, **_k: pytest.fail("implicit writable fallback"))
    assert launcher.start_server(tmp_path, 8027) is None
    assert "Required read-only research entry not found" in capsys.readouterr().out


def test_restart_identity_accepts_only_the_two_maintained_project_entries(tmp_path: Path) -> None:
    identity = project_process(tmp_path)
    assert launcher._identity_is_project_server(identity, tmp_path)
    identity["argv"][4] = str(tmp_path / "scripts/run_research_viewer.py")
    assert launcher._identity_is_project_server(identity, tmp_path)
    identity["argv"][4] = str(tmp_path / "scripts/other_viewer.py")
    assert not launcher._identity_is_project_server(identity, tmp_path)


def seed_execution_book(root: Path) -> Path:
    from ai_trader.execution_evidence import capture_execution_evidence
    for folder, account in (("ai_trader", "ai_trader_100k"), ("ai_trader_glm", "ai_trader_100k_glm")):
        path = root / "state" / folder / "ledger.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ledger_id": account, "cash": 100000, "positions": []}), encoding="utf-8")
    strategy = root / "state/auto_paper_portfolio.json"
    strategy.write_text('{"ledger_id":"strategy","cash":100000}', encoding="utf-8")
    evidence = capture_execution_evidence({"symbol": "600000", "side": "buy", "trade_date": "2026-10-23",
        "price": 9.53, "price_basis": "raw"}, {"source_evidence": {"status": "stable", "sha256": "a" * 64},
        "original_row": {"date": "2026-10-23", "open": 9.53, "close": 9.53, "price_basis": "raw"}},
        plan_id="fixture-plan", evidence_dir=root / "state/ai_trader/execution_evidence")
    ledger = root / "state/ai_trader/ledger.json"
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value["trades"] = [{"price_evidence": evidence}]
    ledger.write_text(json.dumps(value), encoding="utf-8")
    return Path(evidence["archive_path"])


def test_execution_archive_is_in_closed_backup_and_restores_byte_exactly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    evidence = seed_execution_book(source)
    originals = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    closure = build_backup_evidence_closure(source)
    assert closure["ok"] and len(closure["references"]) == 1
    assert closure["references"][0]["binding_status"] == "embedded_execution_receipt_match"
    archive = tmp_path / "backup.tar.gz"
    assert pack_backup_evidence(source, archive)["ok"]
    restored = tmp_path / "restore"
    result = verify_backup_archive(archive, isolated_restore_root=restored)
    assert result["isolated_restore_verified"]
    assert result["execution_archive_reference_count"] == 1
    assert result["historical_trade_evidence_complete"] is None
    assert (restored / evidence.relative_to(source)).read_bytes() == evidence.read_bytes()
    assert {path.relative_to(restored).as_posix(): path.read_bytes() for path in restored.rglob("*") if path.is_file()} == originals


@pytest.mark.parametrize("damage", ["missing", "changed_receipt", "cross_account", "escape", "invalid_embedded"])
def test_execution_archive_omission_or_rebinding_blocks_pack(tmp_path: Path, damage: str) -> None:
    source = tmp_path / "source"
    evidence = seed_execution_book(source)
    ledger = source / "state/ai_trader/ledger.json"
    value = json.loads(ledger.read_text(encoding="utf-8"))
    price_evidence = value["trades"][0]["price_evidence"]
    if damage == "missing": evidence.unlink()
    elif damage == "changed_receipt":
        receipt = json.loads(evidence.read_text(encoding="utf-8"))
        receipt["captured_at"] = "changed"
        evidence.write_text(json.dumps(receipt), encoding="utf-8")
    elif damage == "cross_account":
        destination = source / "state/ai_trader_glm/execution_evidence" / evidence.name
        destination.parent.mkdir()
        shutil.copyfile(evidence, destination)
        price_evidence["archive_path"] = str(destination)
    elif damage == "escape": price_evidence["archive_path"] = "../private.json"
    elif damage == "invalid_embedded": price_evidence["receipt"]["evidence_id"] = "0" * 64
    ledger.write_text(json.dumps(value), encoding="utf-8")
    archive = tmp_path / "bad.tar.gz"
    result = pack_backup_evidence(source, archive)
    assert not result["ok"] and not archive.exists()


def test_duplicate_seed_is_archived_once_and_malformed_manifest_never_restores(tmp_path: Path) -> None:
    source = tmp_path / "source"
    path = source / "docs/learning_log.md"
    path.parent.mkdir(parents=True)
    path.write_text("fixture\n", encoding="utf-8")
    archive = tmp_path / "one.tar.gz"
    result = pack_backup_evidence(source, archive, seed_paths=["docs/learning_log.md"] * 2)
    assert result["ok"] and result["file_count"] == 1
    with tarfile.open(archive) as incoming:
        assert incoming.getnames().count("docs/learning_log.md") == 1
    broken = tmp_path / "bad-manifest.tar.gz"
    with tarfile.open(broken, "w:gz") as outgoing:
        item = tarfile.TarInfo("manifest.json"); item.size = 2
        outgoing.addfile(item, io.BytesIO(b"[]"))
    result = verify_backup_archive(broken, isolated_restore_root=tmp_path / "not-created")
    assert not result["ok"] and not (tmp_path / "not-created").exists()


def test_natural_backup_uses_same_closure_and_restorable_archive(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    evidence = seed_execution_book(source)
    learning = source / "docs/learning_log.md"
    learning.parent.mkdir()
    learning.write_text("owner fixture\n", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"files": [{"kind": "paper_backup", "path": path} for path in (
        "state/auto_paper_portfolio.json", "state/ai_trader/ledger.json", "state/ai_trader_glm/ledger.json",
        "docs/learning_log.md")]}), encoding="utf-8")
    monkeypatch.setattr(push, "CANON", source)
    monkeypatch.setattr(push, "PLAN", plan)
    monkeypatch.setattr(push, "TMP", tmp_path / "pack")
    monkeypatch.setattr(push, "LOG", tmp_path / "push.log")
    calls = []
    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="MATCH\n", stderr="")
    assert push.main([], run=fake_run, check_output=lambda *_a, **_k: "") == 0
    result = json.loads(push.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert result["file_count"] == 5
    assert result["execution_archive_reference_count"] == 1
    assert result["historical_trade_evidence_complete"] is None
    archive = next(push.TMP.glob("quantdesk_backup_*.tar.gz"))
    restored = tmp_path / "restore"
    assert verify_backup_archive(archive, isolated_restore_root=restored)["isolated_restore_verified"]
    assert (restored / evidence.relative_to(source)).read_bytes() == evidence.read_bytes()
    # Exercise the exact uploaded verifier locally against a fixture directory;
    # every transport call above was fake and no real SSH process was started.
    remote = tmp_path / "remote"
    remote.mkdir()
    shutil.copyfile(archive, remote / "quantdesk_backup.tar.gz")
    monkeypatch.setattr(sys, "argv", ["verify.py", str(remote), hashlib.sha256(archive.read_bytes()).hexdigest()])
    exec(compile(push.REMOTE_VERIFY_SOURCE, "fixture_remote_verifier", "exec"), {})
    assert (remote / evidence.relative_to(source)).read_bytes() == evidence.read_bytes()
