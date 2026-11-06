"""The default isolated copy includes only the two explicitly maintained ignored assets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_tests_isolated as runner


def fake_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if 'rev-parse' in command:
            return SimpleNamespace(stdout='f' * 40 + '\n')
        if 'ls-files' in command:
            return SimpleNamespace(stdout=b'README.md\0')
        pytest.fail(f'Unexpected git mutation: {command}')
    monkeypatch.setattr(runner.subprocess, 'run', run)


def write(root: Path, name: str, raw: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def test_default_copy_preserves_required_ignored_source_without_private_or_runtime_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_git(monkeypatch)
    source = tmp_path / 'source'
    fixtures = {'README.md': b'repo\n', 'docs/screenshots/README.md': b'maintained document\n',
                'build/QuantDesk.spec': b'build_recipe = []\n'}
    for name, raw in fixtures.items():
        write(source, name, raw)
    for name in ('build/private.key', 'docs/screenshots/private.png', 'data/daily/600000.csv',
                 'state/auto_paper_portfolio.json', 'config/llm_config.json'):
        write(source, name, b'SHOULD_NOT_BE_COPIED')
    sandbox, manifest = runner.build_test_copy(source, tmp_path / 'out', include_git_metadata=False)
    assert set(manifest['files']) == set(fixtures)
    for name, raw in fixtures.items():
        assert (sandbox / name).read_bytes() == raw
        assert manifest['files'][name] == hashlib.sha256(raw).hexdigest()
    assert manifest['real_runtime_copied'] is False
    assert json.loads((sandbox / '.quantdesk-test-sandbox.json').read_text(encoding='utf-8'))['files'] == manifest['files']


def test_missing_assets_are_not_fabricated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git(monkeypatch)
    source = tmp_path / 'source'
    write(source, 'README.md', b'repo\n')
    sandbox, manifest = runner.build_test_copy(source, tmp_path / 'out', include_git_metadata=False)
    assert set(manifest['files']) == {'README.md'}
    assert not (sandbox / 'build/QuantDesk.spec').exists()


def test_ignored_asset_symlink_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git(monkeypatch)
    source = tmp_path / 'source'
    write(source, 'README.md', b'repo\n')
    write(source, 'build/QuantDesk.spec', b'not the intended source\n')
    real = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path.name == 'QuantDesk.spec' or real(path))
    sandbox, manifest = runner.build_test_copy(source, tmp_path / 'out', include_git_metadata=False)
    assert 'build/QuantDesk.spec' not in manifest['files']
    assert not (sandbox / 'build/QuantDesk.spec').exists()


def test_ignored_asset_size_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git(monkeypatch)
    source = tmp_path / 'source'
    write(source, 'README.md', b'repo\n')
    write(source, 'build/QuantDesk.spec', b'x' * 2_000_001)
    with pytest.raises(ValueError, match='maintained_source_asset_too_large'):
        runner.build_test_copy(source, tmp_path / 'out', include_git_metadata=False)


def test_runner_preserves_the_inherited_child_fence_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = tmp_path / 'prepared'
    sandbox.mkdir()
    manifest = {'source_root': str(tmp_path / 'source'), 'base_commit': 'f' * 40, 'files': {}}
    monkeypatch.setattr(runner, 'build_test_copy', lambda *args: (sandbox, manifest))
    captured = {}
    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs['env'])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, 'run', run)
    inherited = os.environ['QUANTDESK_TEST_FENCE_BOOTSTRAP']
    state = os.environ['QUANTDESK_TEST_FENCE_STATE']
    assert runner.main(['--source-root', str(tmp_path / 'source'), '--output-root', str(tmp_path / 'out'), '--', '-q']) == 0
    assert captured['PYTHONPATH'].split(os.pathsep)[0] == inherited
    assert captured['QUANTDESK_TEST_FENCE_STATE'] == state
