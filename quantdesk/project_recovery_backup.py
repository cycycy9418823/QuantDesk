"""Offline, streaming recovery supplements; no task installation or network I/O."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Mapping
import xml.etree.ElementTree as ET

SCHEMA = "project_recovery.v1"
CHUNK_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024**3
MAX_BUNDLE_BYTES = 64 * 1024**2
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_FILES = 5000
SETTINGS = Path("config/project_recovery_backup.json")
_RUNTIME = {"state", "results", "reports", "logs", "dist", "test_runtime", ".venv", "node_modules"}
_PRIVATE_NAMES = {"llm.env", "llm_config.json", ".env", "credentials.json", "secrets.json"}
_SECRET_KEYS = {"api_key", "apikey", "token", "access_token", "password", "secret", "authorization", "private_key"}
SOURCE_SCOPES = {"full_history", "current_tree", "current_working_tree"}
_WORKING_LOCAL_CONFIGS = {SETTINGS.as_posix(), 'config/local_data_store_override.yaml', '.codex/config.toml'}
_WORKING_SOURCE_ROOTS = {"quantdesk", "scripts", "tests", "docs", "schemas", "templates", "examples",
                         "app", "ai_trader", "design-system", ".github"}
_WORKING_SOURCE_SUFFIXES = {".py", ".md", ".rst", ".txt", ".toml", ".json", ".yaml", ".yml",
                            ".ps1", ".cmd", ".bat", ".html", ".css", ".js", ".cjs", ".mjs",
                            ".sql", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    # Git object payloads require literal LF, including when Python runs on Windows.
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            input=None if input_text is None else input_text.encode("utf-8"), timeout=120)
    if result.returncode:
        raise ValueError("recovery_git_command_failed:" + args[0])
    output = result.stdout.decode("utf-8", errors="replace")
    return output.strip() if "-z" not in args else output


def _private_path(path: PurePosixPath) -> bool:
    return (path.as_posix() in {SETTINGS.as_posix(), "config/provider_secrets.json", "config/local_data_store_override.yaml",
                               ".codex/config.toml", "docs/learning_journal.md", "docs/learning_log.md", "docs/owner_learning_log.md"}
            or any(part.lower() in _PRIVATE_NAMES or part.lower().startswith(".env.") for part in path.parts)
            or path.suffix.lower() in {".pem", ".key", ".pfx", ".p12"})


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or path.as_posix() != name or ":" in name or "\\" in name
            or ".." in path.parts or _private_path(path)):
        return False
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) or p.endswith((".", " ")) for p in path.parts):
        return False
    return (name in {"source/project.bundle", "environment.json", "manifest.json"}
            or (len(path.parts) == 2 and path.parts[0] == "tasks" and path.suffix == ".xml")
            or (path.parts[:2] == ("data", "metadata") and len(path.parts) > 2))


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _assert_plain_path(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root) or any(_linked(p) for p in (path, *path.parents) if p != root.parent):
        raise ValueError("recovery_path_escape_or_link")


def recovery_settings(source_root: Path | str) -> dict[str, Any]:
    """Read the explicit transfer opt-in and the reviewed source scope."""
    root = Path(source_root).resolve()
    path = root / SETTINGS
    if not path.exists():
        return {"enabled": False, "source_scope": "full_history"}
    _assert_plain_path(root, path)
    if path.stat().st_size > 65536:
        raise ValueError("recovery_settings_too_large")
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(settings, dict) or not {"enabled"} <= settings.keys()
            or not settings.keys() <= {"enabled", "source_scope"} or not isinstance(settings["enabled"], bool)
            or not isinstance(settings.get("source_scope", "full_history"), str)
            or settings.get("source_scope", "full_history") not in SOURCE_SCOPES):
        raise ValueError("recovery_settings_invalid")
    return {"enabled": settings["enabled"], "source_scope": settings.get("source_scope", "full_history")}


def recovery_enabled(source_root: Path | str) -> bool:
    """A separate, reviewed local setting enables supplemental remote transfer."""
    return recovery_settings(source_root)["enabled"]


def metadata_inventory(source_root: Path | str) -> dict[str, Any]:
    root = Path(source_root).resolve()
    folder = root / "data/metadata"
    _assert_plain_path(root, folder)
    if not folder.is_dir():
        raise ValueError("recovery_metadata_directory_missing")
    files = []
    for directory, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs + names:
            _assert_plain_path(root, Path(directory) / name)
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if not _safe_name(relative) or not path.is_file():
                raise ValueError("recovery_metadata_path_invalid")
            stat = path.stat()
            files.append({"path": relative, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    if len(files) > MAX_FILES - 100 or sum(row["bytes"] for row in files) > MAX_TOTAL_BYTES - MAX_BUNDLE_BYTES:
        raise ValueError("recovery_metadata_size_limit")
    return {"files": sorted(files, key=lambda row: row["path"]), "file_count": len(files),
            "total_bytes": sum(row["bytes"] for row in files)}


def _assert_no_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS and item not in (None, "", "[REDACTED]"):
                raise ValueError("recovery_secret_field_refused")
            _assert_no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_secrets(item)


def _validated_task_xml(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > 1024**2 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("recovery_task_xml_invalid")
    tree = ET.fromstring(text)
    if tree.tag.rsplit("}", 1)[-1] != "Task":
        raise ValueError("recovery_task_xml_invalid")
    for element in tree.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        value = (element.text or "").strip()
        if tag in _SECRET_KEYS and value:
            raise ValueError("recovery_task_secret_refused")
        if re.search(r"(?i)(?:--?|/|\b)(?:api[_-]?key|access[_-]?token|password|secret|token)(?:\s+|\s*[:=])|\b(?:Bearer|Basic)\s+\S+|PRIVATE KEY", value):
            raise ValueError("recovery_task_secret_refused")
    # Export-ScheduledTask declares UTF-16 even when transported as JSON text.
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)


def collect_task_definitions() -> dict[str, str]:
    """Read only QuantDesk task definitions; never install, enable or run them."""
    if sys.platform != "win32":
        raise ValueError("recovery_task_export_requires_windows_or_explicit_xml")
    command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; @(Get-ScheduledTask -TaskName 'QuantDesk*' | ForEach-Object { [pscustomobject]@{name=($_.TaskPath+$_.TaskName); xml=(Export-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath)} }) | ConvertTo-Json -Depth 4 -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, encoding="utf-8", errors="strict", timeout=60)
    if result.returncode:
        raise ValueError("recovery_task_export_failed")
    rows = json.loads(result.stdout.lstrip("\ufeff"))
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("recovery_task_export_empty")
    tasks = {str(row["name"]): str(row["xml"]) for row in rows}
    if len(tasks) != len(rows):
        raise ValueError("recovery_duplicate_task_identity")
    return tasks


def collect_environment() -> dict[str, Any]:
    """Use explicit public fields, never environment variables or package URLs."""
    packages = sorted({(str(d.metadata.get("Name") or ""), str(d.version)) for d in importlib.metadata.distributions()})
    return {"python_version": platform.python_version(), "python_implementation": platform.python_implementation(),
            "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "packages": [{"name": name, "version": version} for name, version in packages if name],
            "secrets_included": False, "scheduled_tasks_installed": False}


def _protected_source_path(path: PurePosixPath) -> bool:
    return (_private_path(path) or path.parts[0] in _RUNTIME
            or (path.parts[0] == "data" and path.suffix.lower() not in {"", ".md"}))


def _create_current_tree_bundle(root: Path, head: str, tree: str, target: Path) -> str:
    """Build an independent backup object graph; never rewrite the source repo."""
    with tempfile.TemporaryDirectory(prefix="source-snapshot-", dir=target.parent) as tmp:
        repository = Path(tmp)
        _git(repository, "init", "--quiet", "--bare", "--object-format=" + _git(root, "rev-parse", "--show-object-format"))
        objects = _git(root, "rev-list", "--objects", "--no-object-names", tree)
        pack = repository / "current-tree.pack"
        with pack.open("xb") as output:
            exported = subprocess.run(["git", "-C", str(root), "pack-objects", "--stdout"],
                                      input=(objects + "\n").encode("ascii"), stdout=output,
                                      stderr=subprocess.PIPE, timeout=120)
        if exported.returncode or pack.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("recovery_source_snapshot_pack_failed")
        with pack.open("rb") as incoming:
            imported = subprocess.run(["git", "-C", str(repository), "index-pack", "--stdin"],
                                      stdin=incoming, capture_output=True, timeout=120)
        if imported.returncode:
            raise ValueError("recovery_source_snapshot_import_failed")
        timestamp = _git(root, "show", "-s", "--format=%ct", head)
        identity = f"QuantDesk Recovery <recovery@example.invalid> {timestamp} +0000"
        commit = f"tree {tree}\nauthor {identity}\ncommitter {identity}\n\nCurrent source snapshot of {head}; ancestor history omitted.\n"
        bundle_head = _git(repository, "hash-object", "-t", "commit", "-w", "--stdin", input_text=commit)
        _git(repository, "update-ref", "refs/heads/recovery-snapshot", bundle_head)
        _git(repository, "symbolic-ref", "HEAD", "refs/heads/recovery-snapshot")
        _git(repository, "bundle", "create", str(target), "HEAD")
        _git(repository, "bundle", "verify", str(target))
        return bundle_head


def _valid_source_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and path.as_posix() == name and ':' not in name
                and '\\' not in name and '..' not in path.parts and not any(ord(char) < 32 for char in name)
                and not any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
                            or part.endswith(('.', ' ')) for part in path.parts))


def _working_source_paths(root: Path) -> list[dict[str, str]]:
    """Select maintained bytes without opening private, ignored or runtime files."""
    selected = {}
    for entry in _git(root, 'ls-files', '--stage', '-z').split('\0'):
        if not entry:
            continue
        header, separator, name = entry.partition('\t')
        mode, _, stage = header.split()
        if not separator or stage != '0':
            raise ValueError('recovery_working_source_unmerged_index')
        if not _valid_source_name(name):
            raise ValueError('recovery_working_source_path_invalid')
        if name in _WORKING_LOCAL_CONFIGS:
            continue
        if mode not in {'100644', '100755'}:
            raise ValueError('recovery_source_link_or_submodule')
        if _protected_source_path(PurePosixPath(name)):
            raise ValueError('recovery_current_source_contains_protected_path')
        path = root / name
        _assert_plain_path(root, path)
        if path.is_file():
            selected[name] = mode if os.name == 'nt' else ('100755' if path.stat().st_mode & 0o111 else '100644')
        elif path.exists():
            raise ValueError('recovery_working_source_not_regular_file')
    for name in _git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0'):
        if not name:
            continue
        path = PurePosixPath(name)
        if (_protected_source_path(path) or any(part in {'__pycache__', '.pytest_cache', 'node_modules'} for part in path.parts)):
            continue
        if (path.parts[0] not in _WORKING_SOURCE_ROOTS and path.parts[:2] != ('config', 'examples')):
            continue
        if path.suffix.lower() not in _WORKING_SOURCE_SUFFIXES:
            continue
        if not _valid_source_name(name):
            raise ValueError('recovery_working_source_path_invalid')
        current = root / name
        _assert_plain_path(root, current)
        if not current.is_file():
            raise ValueError('recovery_working_source_not_regular_file')
        selected[name] = '100755' if os.name != 'nt' and current.stat().st_mode & 0o111 else '100644'
    names = sorted(selected)
    if not names or len(names) > MAX_FILES or len({name.casefold() for name in names}) != len(names):
        raise ValueError('recovery_working_source_file_set_invalid')
    return [{'path': name, 'mode': selected[name]} for name in names]


def _working_status(root: Path) -> str:
    # Status may normally refresh the real index. Disable that optional write.
    return _git(root, '--no-optional-locks', 'status', '--porcelain=v1', '--untracked-files=all', '-z')


def _working_fingerprint(files: list[dict[str, Any]], status_sha256: str) -> str:
    return hashlib.sha256(_json_bytes({'files': files, 'git_status_sha256': status_sha256})).hexdigest()


def _validate_working_source(root: Path, source: Mapping[str, Any]) -> None:
    selected = _working_source_paths(root)
    expected = [{'path': row['path'], 'mode': row['mode']} for row in source['working_files']]
    if (selected != expected or _git(root, 'rev-parse', 'HEAD') != source['head']
            or hashlib.sha256(_working_status(root).encode('utf-8')).hexdigest() != source['git_status_sha256']):
        raise ValueError('recovery_working_source_changed')
    for row in source['working_files']:
        path = root / row['path']
        _assert_plain_path(root, path)
        if path.stat().st_size != row['bytes'] or sha256_file(path) != row['sha256']:
            raise ValueError('recovery_working_source_changed')


def _create_working_tree_bundle(root: Path, head: str, base_tree: str, target: Path) -> dict[str, Any]:
    """Create an external parentless object graph from actual maintained files."""
    paths, status = _working_source_paths(root), _working_status(root)
    status_sha256 = hashlib.sha256(status.encode('utf-8')).hexdigest()
    files, total = [], 0
    with tempfile.TemporaryDirectory(prefix='working-source-snapshot-', dir=target.parent) as tmp:
        staging = Path(tmp)
        repository = staging / 'repository'
        repository.mkdir()
        _git(repository, 'init', '--quiet', '--bare', '--object-format=' + _git(root, 'rev-parse', '--show-object-format'))
        copied = []
        for row in paths:
            current, copy = root / row['path'], staging / 'files' / row['path']
            _assert_plain_path(root, current)
            before = current.stat()
            total += before.st_size
            if total > MAX_BUNDLE_BYTES:
                raise ValueError('recovery_working_source_size_limit')
            copy.parent.mkdir(parents=True, exist_ok=True)
            digest, count = hashlib.sha256(), 0
            with current.open('rb') as incoming, copy.open('xb') as output:
                for block in iter(lambda: incoming.read(CHUNK_BYTES), b''):
                    digest.update(block)
                    count += len(block)
                    output.write(block)
            _assert_plain_path(root, current)
            after = current.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or count != before.st_size:
                raise ValueError('recovery_working_source_changed')
            files.append({**row, 'bytes': count, 'sha256': digest.hexdigest()})
            copied.append(copy)
        # Hash raw copies in one Git process. No attributes, clean filters or hooks.
        inputs = ''.join(json.dumps(path.as_posix(), ensure_ascii=False) + '\n' for path in copied)
        objects = _git(repository, 'hash-object', '--no-filters', '-w', '--stdin-paths', input_text=inputs).splitlines()
        if len(objects) != len(files) or not all(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) for value in objects):
            raise ValueError('recovery_working_source_objects_invalid')
        entries = ''.join(f"{row['mode']} {oid}\t{row['path']}\0" for row, oid in zip(files, objects))
        _git(repository, 'update-index', '-z', '--index-info', input_text=entries)
        tree = _git(repository, 'write-tree')
        fingerprint = _working_fingerprint(files, status_sha256)
        timestamp = _git(root, 'show', '-s', '--format=%ct', head)
        identity = f'QuantDesk Recovery <recovery@example.invalid> {timestamp} +0000'
        commit = (f'tree {tree}\nauthor {identity}\ncommitter {identity}\n\n'
                  f'Working source snapshot of {head}; fingerprint {fingerprint}; ancestor history omitted.\n')
        bundle_head = _git(repository, 'hash-object', '-t', 'commit', '-w', '--stdin', input_text=commit)
        _git(repository, 'update-ref', 'refs/heads/recovery-snapshot', bundle_head)
        _git(repository, 'symbolic-ref', 'HEAD', 'refs/heads/recovery-snapshot')
        _git(repository, 'bundle', 'create', str(target), 'HEAD')
        _git(repository, 'bundle', 'verify', str(target))
        if target.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError('recovery_bundle_size_limit')
        if _git(repository, 'bundle', 'list-heads', str(target)) != bundle_head + ' HEAD':
            raise ValueError('recovery_bundle_head_mismatch')
        source = {'head': head, 'base_tree': base_tree, 'tree': tree, 'bundle_head': bundle_head,
                  'scope': 'current_working_tree_without_ancestor_history', 'history_included': False,
                  'worktree_byte_snapshot': True, 'git_dirty': bool(status), 'git_status_sha256': status_sha256,
                  'working_files': files, 'working_file_count': len(files), 'working_fingerprint': fingerprint,
                  'source_filter': 'maintained_source_without_declared_local_configuration_v1',
                  'excluded_local_configuration_paths': sorted(name for name in _git(root, 'ls-files', '-z').split('\0')
                                                               if name in _WORKING_LOCAL_CONFIGS),
                  'git_version': _git(root, '--version')}
        _validate_working_source(root, source)
        return source


def _create_bundle(root: Path, target: Path, *, source_scope: str = "full_history") -> dict[str, Any]:
    if source_scope not in SOURCE_SCOPES:
        raise ValueError("recovery_source_scope_invalid")
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", head + "^{tree}")
    if _git(root, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("recovery_source_history_shallow")
    if source_scope == 'current_working_tree':
        return _create_working_tree_bundle(root, head, tree, target)
    _git(root, "diff", "--quiet", "HEAD", "--")
    for name in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if name and PurePosixPath(name).parts[0] not in _RUNTIME | {"data", "config"} and name != "docs/learning_journal.md":
            raise ValueError("recovery_uncommitted_source_present")
    for line in _git(root, "ls-tree", "-r", "-z", head).split("\0"):
        if not line:
            continue
        if line.startswith(("120000", "160000")):
            raise ValueError("recovery_source_link_or_submodule")
        if _protected_source_path(PurePosixPath(line.partition("\t")[2])):
            raise ValueError("recovery_current_source_contains_protected_path")
    if source_scope == "full_history":
        for line in _git(root, "rev-list", "--objects", head).splitlines():
            _, _, name = line.partition(" ")
            if name and _protected_source_path(PurePosixPath(name)):
                raise ValueError("recovery_source_history_contains_protected_path")
        _git(root, "bundle", "create", str(target), "HEAD")
        bundle_head = head
    else:
        bundle_head = _create_current_tree_bundle(root, head, tree, target)
    if target.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("recovery_bundle_size_limit")
    _git(root, "bundle", "verify", str(target))
    if _git(root, "bundle", "list-heads", str(target)) != bundle_head + " HEAD":
        raise ValueError("recovery_bundle_head_mismatch")
    if _git(root, "rev-parse", "HEAD") != head:
        raise ValueError("recovery_source_changed")
    return {"head": head, "tree": tree, "bundle_head": bundle_head,
            "scope": "current_HEAD_and_reachable_history" if source_scope == "full_history" else "current_tree_without_ancestor_history",
            "history_included": source_scope == "full_history", "git_version": _git(root, "--version")}


class _HashingReader:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        raw = self.stream.read(CHUNK_BYTES if size < 0 else min(size, CHUNK_BYTES))
        self.digest.update(raw)
        return raw


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def pack_project_recovery(source_root: Path | str, archive_path: Path | str, *,
                          task_definitions: Mapping[str, str] | None = None,
                          source_scope: str = "full_history") -> dict[str, Any]:
    """Freeze the explicitly selected source scope and metadata externally."""
    root, target = Path(source_root).resolve(), Path(archive_path).resolve()
    try:
        if target.exists() or target.is_relative_to(root):
            raise ValueError("recovery_destination_not_new_external_path")
        inventory = metadata_inventory(root)
        tasks = dict(collect_task_definitions() if task_definitions is None else task_definitions)
        if not tasks or len(tasks) > 100:
            raise ValueError("recovery_task_export_empty_or_too_large")
        task_rows, small = [], {"environment.json": _json_bytes(collect_environment())}
        for name, text in sorted(tasks.items()):
            if not name.replace("\\", "/").rsplit("/", 1)[-1].startswith("QuantDesk"):
                raise ValueError("recovery_unrelated_task_refused")
            path = "tasks/" + hashlib.sha256(name.encode()).hexdigest() + ".xml"
            small[path] = _validated_task_xml(text)
            task_rows.append({"task_identity": name, "path": path})
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="recovery-stage-", dir=target.parent) as tmp:
            staging = Path(tmp)
            bundle = staging / "project.bundle"
            source = _create_bundle(root, bundle, source_scope=source_scope)
            files = []
            staged_archive = staging / "recovery.tar.gz"
            with tarfile.open(staged_archive, "w:gz", compresslevel=1) as archive:
                rows = [("source/project.bundle", bundle), *[(row["path"], root / row["path"]) for row in inventory["files"]]]
                for name, path in rows:
                    before = path.stat()
                    if path.suffix.lower() == ".json":
                        if before.st_size > MAX_MANIFEST_BYTES:
                            raise ValueError("recovery_metadata_json_audit_size_limit")
                        _assert_no_secrets(json.loads(path.read_text(encoding="utf-8-sig")))
                    item = tarfile.TarInfo(name)
                    item.size = before.st_size
                    with path.open("rb") as stream:
                        reader = _HashingReader(stream)
                        archive.addfile(item, reader)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("recovery_input_changed")
                    files.append({"path": name, "bytes": before.st_size, "sha256": reader.digest.hexdigest()})
                for name, raw in sorted(small.items()):
                    item = tarfile.TarInfo(name)
                    item.size = len(raw)
                    archive.addfile(item, io.BytesIO(raw))
                    files.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
                manifest = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
                            "source_root": str(root), "source": source, "files": files, "tasks": task_rows,
                            "metadata_file_count": inventory["file_count"], "metadata_bytes": inventory["total_bytes"],
                            "paper_ledgers_included": False, "restores_tasks_automatically": False}
                raw = _json_bytes(manifest)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise ValueError("recovery_manifest_size_limit")
                item = tarfile.TarInfo("manifest.json")
                item.size = len(raw)
                archive.addfile(item, io.BytesIO(raw))
            if metadata_inventory(root) != inventory or _git(root, "rev-parse", "HEAD") != source["head"]:
                raise ValueError("recovery_input_set_changed")
            if source_scope == 'current_working_tree':
                _validate_working_source(root, source)
            else:
                _git(root, "diff", "--quiet", "HEAD", "--")
            verified = verify_project_recovery(staged_archive)
            if not verified["ok"]:
                raise ValueError("recovery_local_verification_failed")
            # A hard link publishes atomically without overwriting another run.
            os.link(staged_archive, target)
        return {"ok": True, "status": "packed_verified", "archive": str(target),
                "archive_sha256": verified["archive_sha256"], "archive_bytes": target.stat().st_size,
                "source_head": source["head"], "source_bundle_head": source["bundle_head"],
                "source_tree": source["tree"], "source_scope": source["scope"],
                "history_included": source["history_included"], "file_count": len(files),
                "metadata_file_count": inventory["file_count"], "metadata_bytes": inventory["total_bytes"],
                "task_count": len(task_rows), "network_used": False}
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError, subprocess.SubprocessError, tarfile.TarError) as exc:
        return {"ok": False, "status": "blocked", "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                "network_used": False}


def _scan_archive(path: Path, destination: Path | None = None) -> dict[str, Any]:
    actual, names, total, manifest = {}, set(), 0, None
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            key = item.name.casefold()
            total += item.size
            if (not item.isfile() or not _safe_name(item.name) or key in names or item.size < 0
                    or total > MAX_TOTAL_BYTES or len(names) >= MAX_FILES
                    or (not item.name.startswith("data/metadata/") and item.size > MAX_BUNDLE_BYTES)):
                raise ValueError("recovery_archive_member_invalid")
            names.add(key)
            stream = archive.extractfile(item)
            assert stream is not None
            if item.name == "manifest.json":
                if item.size > MAX_MANIFEST_BYTES:
                    raise ValueError("recovery_manifest_size_limit")
                manifest = json.loads(stream.read(MAX_MANIFEST_BYTES + 1))
                continue
            output = None
            if destination is not None:
                out = destination / item.name
                _assert_plain_path(destination, out)
                out.parent.mkdir(parents=True, exist_ok=True)
                output = out.open("xb")
            digest, count = hashlib.sha256(), 0
            try:
                for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
                    digest.update(block)
                    count += len(block)
                    if output is not None:
                        output.write(block)
            finally:
                if output is not None:
                    output.close()
            if count != item.size:
                raise ValueError("recovery_archive_truncated_member")
            if destination is not None and sha256_file(destination / item.name) != digest.hexdigest():
                raise ValueError("recovery_restored_file_hash_mismatch")
            actual[item.name] = {"path": item.name, "bytes": count, "sha256": digest.hexdigest()}
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("recovery_manifest_missing_or_invalid")
    expected = manifest.get("files")
    if (not isinstance(expected, list) or len(expected) != len(actual)
            or {row["path"] for row in expected} != set(actual)
            or any(actual.get(row["path"]) != row for row in expected)):
        raise ValueError("recovery_manifest_hash_or_file_set_mismatch")
    if not {"source/project.bundle", "environment.json"} <= actual.keys():
        raise ValueError("recovery_required_member_missing")
    tasks = manifest.get("tasks") or []
    task_names = [row["path"] for row in tasks]
    if (not task_names or len(set(task_names)) != len(task_names)
            or set(task_names) != {name for name in actual if name.startswith("tasks/")}):
        raise ValueError("recovery_task_manifest_incomplete")
    metadata = [row for name, row in actual.items() if name.startswith("data/metadata/")]
    if len(metadata) != manifest.get("metadata_file_count") or sum(row["bytes"] for row in metadata) != manifest.get("metadata_bytes"):
        raise ValueError("recovery_metadata_manifest_incomplete")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(manifest.get("source", {}).get("head", ""))):
        raise ValueError("recovery_source_identity_missing")
    source = manifest['source']
    if source.get('scope') == 'current_working_tree_without_ancestor_history':
        files = source.get('working_files')
        valid = (isinstance(files, list) and bool(files) and len(files) <= MAX_FILES
                 and source.get('working_file_count') == len(files)
                 and source.get('history_included') is False and source.get('worktree_byte_snapshot') is True
                 and isinstance(source.get('git_dirty'), bool)
                 and re.fullmatch(r'[0-9a-f]{64}', str(source.get('git_status_sha256', ''))))
        if not valid:
            raise ValueError('recovery_working_source_manifest_invalid')
        names = set()
        source_bytes = 0
        for row in files:
            if (not isinstance(row, dict) or set(row) != {'path', 'mode', 'bytes', 'sha256'}
                    or not isinstance(row['path'], str) or not _valid_source_name(row['path'])
                    or _protected_source_path(PurePosixPath(row['path'])) or row['mode'] not in {'100644', '100755'}
                    or not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool) or row['bytes'] < 0
                    or not re.fullmatch(r'[0-9a-f]{64}', str(row['sha256'])) or row['path'].casefold() in names):
                raise ValueError('recovery_working_source_manifest_invalid')
            names.add(row['path'].casefold())
            source_bytes += row['bytes']
        if (source_bytes > MAX_BUNDLE_BYTES or source.get('working_fingerprint') != _working_fingerprint(files, source['git_status_sha256'])
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(source.get('tree', '')))
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(source.get('base_tree', '')))
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(source.get('bundle_head', '')))):
            raise ValueError('recovery_working_source_manifest_invalid')
    return manifest


def verify_project_recovery(archive_path: Path | str, *, isolated_restore_root: Path | str | None = None,
                            expected_sha256: str | None = None) -> dict[str, Any]:
    """Verify all bytes before restoring to a new external directory; no execution."""
    path = Path(archive_path).resolve()
    try:
        if path.stat().st_size > MAX_TOTAL_BYTES:
            raise ValueError("recovery_archive_size_limit")
        digest = sha256_file(path)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("recovery_archive_hash_mismatch")
        manifest = _scan_archive(path)
        restored = None
        if isolated_restore_root is not None:
            target = Path(isolated_restore_root).resolve()
            source = Path(manifest["source_root"]).resolve()
            if target.exists() or target.is_relative_to(source) or source.is_relative_to(target) or path.is_relative_to(target):
                raise ValueError("recovery_restore_requires_new_external_directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="recovery-restore-", dir=target.parent) as tmp:
                stage = Path(tmp)
                if _scan_archive(path, stage) != manifest or sha256_file(path) != digest:
                    raise ValueError("recovery_archive_changed_during_restore")
                (stage / "manifest.json").write_bytes(_json_bytes(manifest))
                # Both resolved paths are inside this explicitly chosen parent.
                assert stage.resolve().parent == target.parent and not target.exists()
                stage.rename(target)
            restored = str(target)
        return {"ok": True, "status": "restored_verified" if restored else "verified",
                "archive_sha256": digest, "source_head": manifest["source"]["head"],
                "source_bundle_head": manifest["source"].get("bundle_head", manifest["source"]["head"]),
                "source_tree": manifest["source"].get("tree"), "source_scope": manifest["source"]["scope"],
                "history_included": manifest["source"].get("history_included", True),
                "source_worktree_byte_snapshot": manifest['source'].get('worktree_byte_snapshot', False),
                "source_git_dirty": manifest['source'].get('git_dirty'),
                "source_working_fingerprint": manifest['source'].get('working_fingerprint'),
                "source_working_file_count": manifest['source'].get('working_file_count'),
                "file_count": len(manifest["files"]), "metadata_file_count": manifest["metadata_file_count"],
                "metadata_bytes": manifest["metadata_bytes"], "task_count": len(manifest["tasks"]),
                "restored_root": restored, "network_used": False, "tasks_installed": False}
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError) as exc:
        return {"ok": False, "status": "blocked", "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                "network_used": False, "tasks_installed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--isolated-restore-root")
    parser.add_argument("--match-only", action="store_true")
    args = parser.parse_args(argv)
    result = verify_project_recovery(args.archive, isolated_restore_root=args.isolated_restore_root,
                                     expected_sha256=args.expected_sha256)
    print(("MATCH" if result["ok"] else "NO_MATCH") if args.match_only else json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
