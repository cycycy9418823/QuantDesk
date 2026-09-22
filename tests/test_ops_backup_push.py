from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "push_backup_to_student.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("push_backup_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(mod, tmp: Path, files: dict[str, str] | None = None) -> None:
    canon = tmp / "canon"
    plan = tmp / "plan.json"
    tmpdir = tmp / "tmp"
    log = tmp / "backup_push.log"
    canon.mkdir()
    (canon / "docs").mkdir()
    (canon / "docs" / "learning_log.md").write_text("note\n", encoding="utf-8")
    payload = {"files": [{"path": "docs/learning_log.md", "kind": "paper_backup"}]}
    if files:
        payload["files"] = [{"path": name, "kind": "paper_backup"} for name in files]
        for name, text in files.items():
            path = canon / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    plan.write_text(json.dumps(payload), encoding="utf-8")
    mod.CANON = canon
    mod.PLAN = plan
    mod.TMP = tmpdir
    mod.LOG = log
    mod.KEY = tmp / "no-key.pem"
    mod.KNOWN_HOSTS = tmp / "known_hosts"


class FakeProc(SimpleNamespace):
    pass


def test_pack_missing_file_nonzero(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path, {"missing.json": "x"})
    (mod.CANON / "missing.json").unlink()
    code = mod.main([], run=lambda *a, **k: FakeProc(returncode=0, stdout="", stderr=""))
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert code == 2
    assert row["exit_code"] == 2
    assert row["all_hashes_match"] is False
    assert row["stage"] == "pack"
    assert "missing:" in row["error"]


def test_upload_nonzero_does_not_cleanup(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)
    seen: list[str] = []

    def run(cmd, **kwargs):
        seen.append(" ".join(str(x) for x in cmd))
        if kwargs.get("check") and "mkdir" in seen[-1]:
            raise subprocess.CalledProcessError(1, cmd)
        return FakeProc(returncode=0, stdout="MATCH\n", stderr="")

    code = mod.main([], run=run)
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert code == 2
    assert row["stage"] == "upload"
    assert row["all_hashes_match"] is False
    assert not any("rm -rf" in item for item in seen)


def test_timeout_is_not_success(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)

    def run(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "mkdir" in joined:
            raise subprocess.TimeoutExpired(cmd, 120)
        return FakeProc(returncode=0, stdout="MATCH\n", stderr="")

    code = mod.main([], run=run)
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert code == 5
    assert row["stage"] == "upload"
    assert "timeout:upload" in row["error"]
    assert row["all_hashes_match"] is False


def test_no_match_line_does_not_cleanup(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)
    seen: list[str] = []

    def run(cmd, **kwargs):
        seen.append(" ".join(str(x) for x in cmd))
        joined = seen[-1]
        if "python3" in joined:
            return FakeProc(returncode=0, stdout="NO_MATCH\n", stderr="")
        return FakeProc(returncode=0, stdout="", stderr="")

    code = mod.main([], run=run)
    rows = [json.loads(line) for line in mod.LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    final = rows[-1]
    assert code == 4
    assert final["all_hashes_match"] is False
    assert final["stage"] == "verify"
    assert not any("rm -rf" in item for item in seen)


def test_phase_is_persisted_before_remote_calls(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)

    def run(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "mkdir" in joined:
            rows = [json.loads(line) for line in mod.LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert any(row.get("stage") == "upload" and row.get("event") == "stage_enter" for row in rows)
            raise RuntimeError("stop_after_stage_log")
        return FakeProc(returncode=0, stdout="", stderr="")

    raised = False
    try:
        mod.main([], run=run)
    except RuntimeError:
        raised = True
    assert raised
    rows = [json.loads(line) for line in mod.LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    stages = [row.get("stage") for row in rows if row.get("event") == "stage_enter"]
    assert "pack" in stages
    assert "upload" in stages


def test_verify_without_match_token_fails(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)

    def run(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "python3" in joined:
            return FakeProc(returncode=0, stdout="connected ok directory exists\n", stderr="")
        return FakeProc(returncode=0, stdout="", stderr="")

    code = mod.main([], run=run)
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert code == 4
    assert row["stage"] == "verify"
    assert row["all_hashes_match"] is False
    assert "MATCH" not in (row.get("remote_stdout") or "")


def test_hash_fail_not_success(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)

    def run(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "python3" in joined:
            return FakeProc(returncode=0, stdout="MISMATCH docs/learning_log.md\nHASH_FAIL\n", stderr="")
        return FakeProc(returncode=0, stdout="", stderr="")

    code = mod.main([], run=run)
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert code == 4
    assert row["all_hashes_match"] is not True
    assert row["stage"] == "verify"


def test_success_path_keeps_match(tmp_path: Path) -> None:
    mod = load_mod()
    seed(mod, tmp_path)

    def run(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "python3" in joined:
            return FakeProc(returncode=0, stdout="MATCH\n", stderr="")
        return FakeProc(returncode=0, stdout="", stderr="")

    def check_output(cmd, **kwargs):
        return "snapshot_20260918T161712Z\nsnapshot_ok\n"

    code = mod.main([], run=run, check_output=check_output)
    row = json.loads(mod.LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert code == 0
    assert row["exit_code"] == 0
    assert row["all_hashes_match"] is True
    assert row["stage"] == "done"
    assert row["file_count"] == 2
    utc = str(row["stamp"]).replace("snapshot_", "")
    assert (mod.TMP / f"verify_{utc}.py").is_file()
