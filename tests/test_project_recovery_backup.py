from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from quantdesk import project_recovery_backup as recovery

TASK_XML = '<?xml version="1.0" encoding="UTF-16"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><URI>\\QuantDesk Fixture</URI></RegistrationInfo><Settings><Enabled>false</Enabled></Settings><Actions><Exec><Command>python</Command><Arguments>scripts/check.py</Arguments></Exec></Actions></Task>'
TASKS = {"\\QuantDesk Fixture": TASK_XML}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / ".gitignore").write_text("data/\nconfig/project_recovery_backup.json\n", encoding="utf-8")
    (root / "main.py").write_text('print("fixture program")\n', encoding="utf-8")
    git(root, "init", "--quiet")
    git(root, "add", ".gitignore", "main.py")
    git(root, "-c", "user.name=Recovery Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "deterministic fixture")
    metadata = root / "data/metadata"
    metadata.mkdir(parents=True)
    (metadata / "security_master.csv").write_text("symbol,name\n600000,fixture\n", encoding="utf-8")
    (metadata / "evidence.json").write_text('{"source":"deterministic_fixture"}', encoding="utf-8")
    return root


def make_archive(source: Path, tmp_path: Path) -> tuple[Path, dict]:
    archive = tmp_path / "recovery.tar.gz"
    result = recovery.pack_project_recovery(source, archive, task_definitions=TASKS)
    assert result["ok"], result
    return archive, result


def test_recovers_exact_source_head_metadata_and_disabled_task_without_running_it(source: Path, tmp_path: Path) -> None:
    original = {p.name: p.read_bytes() for p in (source / "data/metadata").iterdir()}
    archive, packed = make_archive(source, tmp_path)
    restored = tmp_path / "restored"
    checked = recovery.verify_project_recovery(archive, isolated_restore_root=restored, expected_sha256=packed["archive_sha256"])
    assert checked["ok"] and checked["tasks_installed"] is False
    assert {p.name: p.read_bytes() for p in (restored / "data/metadata").iterdir()} == original
    assert {p.name: p.read_bytes() for p in (source / "data/metadata").iterdir()} == original
    xml = next((restored / "tasks").glob("*.xml")).read_text(encoding="utf-8")
    assert "false" in xml and "QuantDesk Fixture" in xml
    repo = tmp_path / "recovered-source"
    subprocess.run(["git", "clone", "--quiet", str(restored / "source/project.bundle"), str(repo)], check=True, capture_output=True)
    assert git(repo, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD") == packed["source_head"]
    assert (repo / "main.py").read_bytes() == (source / "main.py").read_bytes()
    assert not (repo / "data/metadata").exists()


def test_metadata_larger_than_old_single_file_limit_is_streamed(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    large = source / "data/metadata/history.csv"
    block = b"1234567890,fixture\n" * 65536
    with large.open("wb") as stream:
        for _ in range(17):
            stream.write(block)
    assert large.stat().st_size > 16 * 1024**2
    original_read = Path.read_bytes
    def bounded_read(path: Path):
        if path == large:
            pytest.fail("large metadata must not be read as one byte string")
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    archive, packed = make_archive(source, tmp_path)
    assert packed["metadata_bytes"] >= large.stat().st_size
    restored = tmp_path / "restored"
    assert recovery.verify_project_recovery(archive, isolated_restore_root=restored)["ok"]
    assert recovery.sha256_file(restored / "data/metadata/history.csv") == recovery.sha256_file(large)


@pytest.mark.parametrize("damage", ["tracked_dirty", "untracked_source", "private_metadata", "json_secret", "invalid_task", "task_secret", "empty_tasks", "size_limit"])
def test_bad_inputs_do_not_publish_an_archive(source: Path, tmp_path: Path, damage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = TASKS
    if damage == "tracked_dirty": (source / "main.py").write_text("changed", encoding="utf-8")
    elif damage == "untracked_source": (source / "new.py").write_text("pass", encoding="utf-8")
    elif damage == "private_metadata": (source / "data/metadata/private.pem").write_text("fixture-only", encoding="utf-8")
    elif damage == "json_secret": (source / "data/metadata/evidence.json").write_text('{"token":"fixture-sensitive"}', encoding="utf-8")
    elif damage == "invalid_task": tasks = {"QuantDesk Fixture": "<not-task/>"}
    elif damage == "task_secret": tasks = {"QuantDesk Fixture": TASK_XML.replace("scripts/check.py", "--token fixture-sensitive")}
    elif damage == "empty_tasks": tasks = {}
    elif damage == "size_limit": monkeypatch.setattr(recovery, "MAX_TOTAL_BYTES", 2)
    archive = tmp_path / "bad.tar.gz"
    result = recovery.pack_project_recovery(source, archive, task_definitions=tasks)
    assert not result["ok"] and not archive.exists()
    assert "fixture-sensitive" not in json.dumps(result)


def test_modified_metadata_during_pack_refuses_publication(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = recovery.metadata_inventory
    calls = 0
    def inventory(root):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "data/metadata/security_master.csv").write_text("changed-during-pack", encoding="utf-8")
        return original(root)
    monkeypatch.setattr(recovery, "metadata_inventory", inventory)
    archive = tmp_path / "unstable.tar.gz"
    result = recovery.pack_project_recovery(source, archive, task_definitions=TASKS)
    assert not result["ok"] and result["error"] == "recovery_input_set_changed"
    assert not archive.exists()


@pytest.mark.parametrize("damage", ["member_bytes", "missing_file", "duplicate_manifest_entry", "path_escape", "symlink", "case_collision", "task_manifest"])
def test_damaged_archives_are_rejected_before_any_restore(source: Path, tmp_path: Path, damage: str) -> None:
    archive, _ = make_archive(source, tmp_path)
    broken = tmp_path / "broken.tar.gz"
    with tarfile.open(archive) as incoming, tarfile.open(broken, "w:gz") as outgoing:
        for member in incoming:
            raw = incoming.extractfile(member).read()
            if damage == "missing_file" and member.name.endswith("security_master.csv"):
                continue
            if damage == "member_bytes" and member.name.endswith("security_master.csv"):
                raw += b"altered"
            if member.name == "manifest.json":
                manifest = json.loads(raw)
                if damage == "duplicate_manifest_entry": manifest["files"][1] = manifest["files"][0]
                if damage == "task_manifest": manifest["tasks"] = []
                raw = json.dumps(manifest).encode()
            item = tarfile.TarInfo(member.name)
            item.size = len(raw)
            outgoing.addfile(item, io.BytesIO(raw))
        if damage in {"path_escape", "symlink", "case_collision"}:
            item = tarfile.TarInfo({"path_escape": "../escape.json", "symlink": "data/metadata/link.csv", "case_collision": "ENVIRONMENT.JSON"}[damage])
            if damage == "symlink":
                item.type = tarfile.SYMTYPE
                item.linkname = "../outside"
                outgoing.addfile(item)
            else:
                item.size = 2
                outgoing.addfile(item, io.BytesIO(b"{}"))
    destination = tmp_path / "restored"
    assert not recovery.verify_project_recovery(broken, isolated_restore_root=destination)["ok"]
    assert not destination.exists() and not (tmp_path / "escape.json").exists()


def test_restore_requires_a_new_directory_outside_source(source: Path, tmp_path: Path) -> None:
    archive, packed = make_archive(source, tmp_path)
    for destination in (source, source / "new", tmp_path):
        assert not recovery.verify_project_recovery(archive, isolated_restore_root=destination)["ok"]
    assert not recovery.verify_project_recovery(archive, expected_sha256="0" * 64)["ok"]
    assert recovery.sha256_file(archive) == packed["archive_sha256"]


def test_existing_destination_is_not_overwritten(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "existing.tar.gz"
    target.write_bytes(b"keep-existing")
    result = recovery.pack_project_recovery(source, target, task_definitions=TASKS)
    assert not result["ok"] and target.read_bytes() == b"keep-existing"


def test_environment_contains_versions_without_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "fixture-never-export-value")
    result = recovery.collect_environment()
    assert result["python_version"] and result["packages"]
    assert "fixture-never-export-value" not in json.dumps(result)
    assert result["secrets_included"] is False


def test_settings_are_explicit_and_type_checked(source: Path) -> None:
    assert recovery.recovery_enabled(source) is False
    path = source / recovery.SETTINGS
    path.parent.mkdir()
    path.write_text('{"enabled":true}', encoding="utf-8")
    assert recovery.recovery_enabled(source) is True
    assert recovery.recovery_settings(source)["source_scope"] == "full_history"
    path.write_text('{"enabled":false,"source_scope":"current_tree"}', encoding="utf-8")
    assert recovery.recovery_settings(source) == {"enabled": False, "source_scope": "current_tree"}
    for value in ({"enabled": "true"}, {"enabled": True, "token": "fixture"},
                  {"enabled": True, "source_scope": []}, {"enabled": True, "source_scope": "automatic"}):
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError): recovery.recovery_enabled(source)


def test_standalone_verifier_has_no_project_import_dependency(source: Path, tmp_path: Path) -> None:
    archive, packed = make_archive(source, tmp_path)
    script = tmp_path / "verify.py"
    script.write_bytes(Path(recovery.__file__).read_bytes())
    completed = subprocess.run([sys.executable, "-B", str(script), "--archive", str(archive),
                                "--expected-sha256", packed["archive_sha256"], "--match-only"],
                               cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 0 and completed.stdout.strip() == "MATCH", completed.stderr


def test_removed_private_file_in_git_history_still_blocks_source_export(source: Path, tmp_path: Path) -> None:
    private = source / "fixture.pem"
    private.write_text("fixture-only-not-a-key", encoding="utf-8")
    git(source, "add", "fixture.pem")
    git(source, "-c", "user.name=Recovery Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "private fixture")
    git(source, "rm", "fixture.pem")
    git(source, "-c", "user.name=Recovery Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "remove private fixture")
    target = tmp_path / "history.tar.gz"
    result = recovery.pack_project_recovery(source, target, task_definitions=TASKS)
    assert result["error"] == "recovery_source_history_contains_protected_path"
    assert not target.exists()


def test_explicit_current_tree_bundle_restores_source_without_private_history(source: Path, tmp_path: Path) -> None:
    private = source / "config/llm_config.json"
    private.parent.mkdir()
    private.write_text('{"api_key":"fixture-never-export"}', encoding="utf-8")
    git(source, "add", "config/llm_config.json")
    git(source, "-c", "user.name=Recovery Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "private historical fixture")
    private_blob = git(source, "rev-parse", "HEAD:config/llm_config.json")
    git(source, "rm", "config/llm_config.json")
    git(source, "-c", "user.name=Recovery Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "remove private fixture")
    original_head = git(source, "rev-parse", "HEAD")
    archive = tmp_path / "snapshot.tar.gz"
    packed = recovery.pack_project_recovery(source, archive, task_definitions=TASKS, source_scope="current_tree")
    assert packed["ok"], packed
    restored = tmp_path / "restored"
    assert recovery.verify_project_recovery(archive, isolated_restore_root=restored)["ok"]
    manifest = json.loads((restored / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["head"] == original_head
    assert manifest["source"]["scope"] == "current_tree_without_ancestor_history"
    assert manifest["source"]["history_included"] is False
    repo = tmp_path / "recovered-source"
    subprocess.run(["git", "clone", "--quiet", str(restored / "source/project.bundle"), str(repo)], check=True, capture_output=True)
    assert git(repo, "rev-parse", "HEAD") == manifest["source"]["bundle_head"] != original_head
    assert git(repo, "rev-parse", "HEAD^{tree}") == git(source, "rev-parse", "HEAD^{tree}") == manifest["source"]["tree"]
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    absent = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", private_blob], capture_output=True)
    assert absent.returncode != 0
    assert not (repo / "config/llm_config.json").exists()
    assert git(source, "rev-parse", "HEAD") == original_head
    assert git(source, "status", "--porcelain") == ""
    blocked = recovery.pack_project_recovery(source, tmp_path / "history.tar.gz", task_definitions=TASKS)
    assert blocked["error"] == "recovery_source_history_contains_protected_path"


def test_current_tree_scope_still_refuses_a_private_file_in_current_source(source: Path, tmp_path: Path) -> None:
    (source / "fixture.pem").write_text("fixture-not-a-real-key", encoding="utf-8")
    git(source, "add", "fixture.pem")
    git(source, "-c", "user.name=Recovery Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "private current fixture")
    archive = tmp_path / "blocked.tar.gz"
    result = recovery.pack_project_recovery(source, archive, task_definitions=TASKS, source_scope="current_tree")
    assert not result["ok"] and not archive.exists()
    assert result["error"] == "recovery_current_source_contains_protected_path"


def test_untracked_personal_journal_is_not_read_as_source(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = source / "docs/learning_journal.md"
    journal.parent.mkdir()
    journal.write_text("private-fixture", encoding="utf-8")
    original = Path.open
    def guarded(path, *args, **kwargs):
        if path == journal:
            pytest.fail("personal journal was opened")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    archive, _ = make_archive(source, tmp_path)
    assert recovery.verify_project_recovery(archive)["ok"]


def test_restore_checks_bytes_read_back_from_disk(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, _ = make_archive(source, tmp_path)
    original = recovery.sha256_file
    def damaged_hash(path):
        if "recovery-restore-" in str(path) and str(path).endswith("security_master.csv"):
            return "0" * 64
        return original(path)
    monkeypatch.setattr(recovery, "sha256_file", damaged_hash)
    destination = tmp_path / "restored"
    result = recovery.verify_project_recovery(archive, isolated_restore_root=destination)
    assert not result["ok"] and result["error"] == "recovery_restored_file_hash_mismatch"
    assert not destination.exists()


def test_current_working_tree_restores_installed_uncommitted_bytes_and_deletions(source: Path, tmp_path: Path) -> None:
    obsolete = source / 'obsolete.py'
    obsolete.write_text('old source\n', encoding='utf-8')
    git(source, 'add', 'obsolete.py')
    git(source, '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'source deletion fixture')
    head, base_tree = git(source, 'rev-parse', 'HEAD'), git(source, 'rev-parse', 'HEAD^{tree}')
    original_bytes = b'print("installed uncommitted program")\r\n'
    (source / 'main.py').write_bytes(original_bytes)
    installed = source / 'quantdesk/newly_installed.py'
    installed.parent.mkdir()
    installed.write_bytes(b'CURRENT_FIX = True\r\n')
    obsolete.unlink()
    before_status = git(source, 'status', '--porcelain')
    before_index = (source / '.git/index').read_bytes()
    archive = tmp_path / 'working-source.tar.gz'
    packed = recovery.pack_project_recovery(source, archive, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert packed['ok'], packed
    restored = tmp_path / 'restored-working'
    checked = recovery.verify_project_recovery(archive, isolated_restore_root=restored,
                                               expected_sha256=packed['archive_sha256'])
    assert checked['ok'], checked
    manifest = json.loads((restored / 'manifest.json').read_text(encoding='utf-8'))
    snapshot = manifest['source']
    assert snapshot['scope'] == 'current_working_tree_without_ancestor_history'
    assert snapshot['head'] == head and snapshot['base_tree'] == base_tree
    assert snapshot['git_dirty'] is True and snapshot['worktree_byte_snapshot'] is True
    assert snapshot['history_included'] is False and len(snapshot['working_fingerprint']) == 64
    assert snapshot['tree'] != base_tree
    recovered = tmp_path / 'recovered-working-source'
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'clone', '--quiet',
                    str(restored / 'source/project.bundle'), str(recovered)], check=True, capture_output=True)
    assert (recovered / 'main.py').read_bytes() == original_bytes
    assert (recovered / 'quantdesk/newly_installed.py').read_bytes() == installed.read_bytes()
    assert not (recovered / 'obsolete.py').exists()
    assert git(recovered, 'rev-parse', 'HEAD') == snapshot['bundle_head']
    assert git(recovered, 'rev-parse', 'HEAD^{tree}') == snapshot['tree']
    assert git(recovered, 'rev-list', '--count', 'HEAD') == '1'
    for row in snapshot['working_files']:
        assert recovery.sha256_file(recovered / row['path']) == row['sha256']
        assert (recovered / row['path']).stat().st_size == row['bytes']
    assert git(source, 'rev-parse', 'HEAD') == head
    assert git(source, 'status', '--porcelain') == before_status
    assert (source / '.git/index').read_bytes() == before_index
    for old_scope in ('full_history', 'current_tree'):
        refused = recovery.pack_project_recovery(source, tmp_path / (old_scope + '.tar.gz'),
                                                 task_definitions=TASKS, source_scope=old_scope)
        assert not refused['ok'] and not (tmp_path / (old_scope + '.tar.gz')).exists()


def test_current_working_tree_does_not_open_untracked_private_or_runtime_files(source: Path, tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    private_paths = [source / '.env', source / 'config/llm_config.json',
                     source / 'config/provider_secrets.json', source / 'config/local_data_store_override.yaml',
                     source / 'docs/learning_journal.md', source / 'state/ledger.json']
    for path in private_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture-private-never-export', encoding='utf-8')
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path in private_paths:
            pytest.fail('Private/runtime file was read into working source')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    archive = tmp_path / 'private-excluded.tar.gz'
    packed = recovery.pack_project_recovery(source, archive, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert packed['ok'], packed
    assert recovery.verify_project_recovery(archive)['ok']
    with tarfile.open(archive, 'r:gz') as stream:
        manifest = json.load(stream.extractfile('manifest.json'))
    names = {row['path'] for row in manifest['source']['working_files']}
    assert not names.intersection(path.relative_to(source).as_posix() for path in private_paths)


def test_current_working_tree_still_refuses_tracked_protected_source(source: Path, tmp_path: Path) -> None:
    (source / 'fixture.pem').write_text('fixture-not-a-key', encoding='utf-8')
    git(source, 'add', 'fixture.pem')
    git(source, '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'protected current source fixture')
    archive = tmp_path / 'blocked-working.tar.gz'
    result = recovery.pack_project_recovery(source, archive, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert not result['ok'] and not archive.exists()
    assert result['error'] == 'recovery_current_source_contains_protected_path'


def test_current_working_tree_omits_private_ancestor_objects(source: Path, tmp_path: Path) -> None:
    private = source / 'fixture.pem'
    private.write_text('historical-private-fixture', encoding='utf-8')
    git(source, 'add', 'fixture.pem')
    git(source, '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'private history fixture')
    private_blob = git(source, 'rev-parse', 'HEAD:fixture.pem')
    git(source, 'rm', 'fixture.pem')
    git(source, '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'removed private history fixture')
    archive = tmp_path / 'working-without-history.tar.gz'
    packed = recovery.pack_project_recovery(source, archive, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert packed['ok'], packed
    restored = tmp_path / 'restored-no-private-history'
    assert recovery.verify_project_recovery(archive, isolated_restore_root=restored)['ok']
    repo = tmp_path / 'source-no-private-history'
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'clone', '--quiet',
                    str(restored / 'source/project.bundle'), str(repo)], check=True, capture_output=True)
    assert subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', private_blob], capture_output=True).returncode != 0
    assert git(repo, 'rev-list', '--count', 'HEAD') == '1'
    old = recovery.pack_project_recovery(source, tmp_path / 'old-full-history.tar.gz', task_definitions=TASKS)
    assert old['error'] == 'recovery_source_history_contains_protected_path'


def test_current_working_tree_source_change_during_pack_refuses_publication(source: Path, tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    original_inventory = recovery.metadata_inventory
    calls = 0
    def changed_source(root):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / 'main.py').write_text('changed after source snapshot', encoding='utf-8')
        return original_inventory(root)
    monkeypatch.setattr(recovery, 'metadata_inventory', changed_source)
    target = tmp_path / 'unstable-working-source.tar.gz'
    result = recovery.pack_project_recovery(source, target, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert not result['ok'] and not target.exists()
    assert result['error'] == 'recovery_working_source_changed'


def test_current_working_tree_rejects_links_before_reading_them(source: Path, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    installed = source / 'quantdesk/new.py'
    installed.parent.mkdir()
    installed.write_text('fixture program', encoding='utf-8')
    original_linked = recovery._linked
    monkeypatch.setattr(recovery, '_linked', lambda path: path == installed or original_linked(path))
    target = tmp_path / 'linked-working-source.tar.gz'
    result = recovery.pack_project_recovery(source, target, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert not result['ok'] and not target.exists()
    assert result['error'] == 'recovery_path_escape_or_link'


def test_current_working_tree_manifest_tampering_is_refused(source: Path, tmp_path: Path) -> None:
    original = tmp_path / 'working-before-tampering.tar.gz'
    packed = recovery.pack_project_recovery(source, original, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert packed['ok'], packed
    damaged = tmp_path / 'damaged-working-manifest.tar.gz'
    with tarfile.open(original, 'r:gz') as incoming, tarfile.open(damaged, 'w:gz') as outgoing:
        for item in incoming:
            raw = incoming.extractfile(item).read()
            if item.name == 'manifest.json':
                manifest = json.loads(raw)
                manifest['source']['working_files'][0]['sha256'] = '0' * 64
                raw = json.dumps(manifest).encode('utf-8')
                item.size = len(raw)
            outgoing.addfile(item, io.BytesIO(raw))
    result = recovery.verify_project_recovery(damaged)
    assert not result['ok'] and result['error'] == 'recovery_working_source_manifest_invalid'


def test_current_working_tree_setting_requires_explicit_scope(source: Path) -> None:
    settings = source / 'config/project_recovery_backup.json'
    settings.parent.mkdir()
    settings.write_text('{"enabled":false,"source_scope":"current_working_tree"}', encoding='utf-8')
    assert recovery.recovery_settings(source) == {'enabled': False, 'source_scope': 'current_working_tree'}


def test_current_working_tree_excludes_tracked_local_configuration_without_reading(source: Path, tmp_path: Path,
                                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    local = source / 'config/local_data_store_override.yaml'
    local.parent.mkdir()
    local.write_text('data_store:\n  enabled: true\n', encoding='utf-8')
    git(source, 'add', 'config/local_data_store_override.yaml')
    git(source, '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--quiet', '-m', 'tracked local preference fixture')
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path == local:
            pytest.fail('Tracked local configuration was read into source snapshot')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    archive = tmp_path / 'local-configuration-excluded.tar.gz'
    packed = recovery.pack_project_recovery(source, archive, task_definitions=TASKS,
                                            source_scope='current_working_tree')
    assert packed['ok'], packed
    with tarfile.open(archive, 'r:gz') as stream:
        manifest = json.load(stream.extractfile('manifest.json'))
    assert manifest['source']['excluded_local_configuration_paths'] == ['config/local_data_store_override.yaml']
    assert 'config/local_data_store_override.yaml' not in {row['path'] for row in manifest['source']['working_files']}
    assert recovery.verify_project_recovery(archive)['ok']
    old = recovery.pack_project_recovery(source, tmp_path / 'committed-local-config.tar.gz',
                                         task_definitions=TASKS, source_scope='current_tree')
    assert old['error'] == 'recovery_current_source_contains_protected_path'
