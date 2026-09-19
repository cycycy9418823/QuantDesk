from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import prepare_daily_repair_candidates as cli


HEADER = b'date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at\n'


def _row(day: str, symbol: str = '002633') -> bytes:
    return f'{day},{symbol},10,11,9,10.5,100,1050,0.5,5,raw,fixture,{day}T16:00:00\n'.encode()


def _inputs(tmp_path: Path) -> tuple[Path, Path, bytes]:
    daily = tmp_path / 'input'
    daily.mkdir()
    original = HEADER + _row('2026-09-07') + b'\x00' * 113 + b'\n' + _row('2026-09-08')
    (daily / '002633.csv').write_bytes(original)
    calendar = tmp_path / 'calendar.csv'
    calendar.write_text('date,is_open,source,note\n' + ''.join(
        f'2026-09-{day},true,baostock.query_trade_dates,trading_day\n' for day in ('07', '08', '09', '10')
    ), encoding='utf-8')
    return daily, calendar, original


def _prepare(tmp_path: Path, **kwargs) -> dict:
    return cli.prepare_candidates(
        input_dir=tmp_path / 'input', output_dir=tmp_path / 'bundle', symbols=['002633'],
        start_date='2026-09-07', target_date='2026-09-10', calendar=tmp_path / 'calendar.csv', **kwargs,
    )


def test_missing_bars_produce_incomplete_external_bundle_without_applying(tmp_path: Path) -> None:
    daily, _, original = _inputs(tmp_path)
    report = _prepare(tmp_path)
    assert report['status'] == 'incomplete'
    assert report['counts'] == {'structural_candidate_only': 1}
    assert report['canonical_write'] is False
    assert report['network_effective'] is False
    assert report['applied'] is False
    assert (daily / '002633.csv').read_bytes() == original
    assert (tmp_path / 'bundle/originals/002633.csv').read_bytes() == original
    assert (tmp_path / 'bundle/candidates/002633.csv').read_bytes() == HEADER + _row('2026-09-07') + _row('2026-09-08')
    assert json.loads((tmp_path / 'bundle/manifest.json').read_text(encoding='utf-8')) == report


def test_actual_supplemental_bars_can_prepare_a_complete_candidate(tmp_path: Path) -> None:
    daily, calendar, original = _inputs(tmp_path)
    supplements = tmp_path / 'supplements'
    supplements.mkdir()
    evidence = HEADER + _row('2026-09-09') + _row('2026-09-10')
    (supplements / '002633.csv').write_bytes(evidence)
    code = cli.main([
        '--input-dir', str(daily), '--output-dir', str(tmp_path / 'bundle'), '--symbols', '002633',
        '--start-date', '2026-09-07', '--target-date', '2026-09-10',
        '--calendar', str(calendar), '--supplemental-dir', str(supplements),
    ])
    assert code == 0
    report = json.loads((tmp_path / 'bundle/manifest.json').read_text(encoding='utf-8'))
    assert report['counts'] == {'candidate_ready': 1}
    assert report['applied'] is False
    assert (daily / '002633.csv').read_bytes() == original
    assert (tmp_path / 'bundle/evidence/002633.csv').read_bytes() == evidence
    assert (tmp_path / 'bundle/evidence/calendar.csv').read_bytes() == calendar.read_bytes()
    assert (tmp_path / 'bundle/candidates/002633.csv').read_bytes().endswith(_row('2026-09-09') + _row('2026-09-10'))


def test_expected_hash_mismatch_does_not_publish_a_candidate(tmp_path: Path) -> None:
    _, _, original = _inputs(tmp_path)
    report = _prepare(tmp_path, expected_hashes={'002633': '0' * 64})
    assert report['status'] == 'blocked'
    assert not (tmp_path / 'bundle/candidates/002633.csv').exists()
    assert (tmp_path / 'bundle/originals/002633.csv').read_bytes() == original


def test_expected_hash_manifest_must_cover_all_selected_symbols(tmp_path: Path) -> None:
    _inputs(tmp_path)
    with pytest.raises(ValueError, match='every selected symbol'):
        _prepare(tmp_path, expected_hashes={})
    assert not (tmp_path / 'bundle').exists()


@pytest.mark.parametrize('suffix', ['', 'repair'])
def test_output_under_input_is_rejected_before_any_write(tmp_path: Path, suffix: str) -> None:
    daily, calendar, original = _inputs(tmp_path)
    with pytest.raises(ValueError, match='overlap'):
        cli.prepare_candidates(input_dir=daily, output_dir=daily / suffix, symbols=['002633'],
                               start_date='2026-09-07', target_date='2026-09-10', calendar=calendar)
    assert (daily / '002633.csv').read_bytes() == original
    assert list(daily.iterdir()) == [daily / '002633.csv']


def test_output_inside_checkout_is_rejected_before_any_write(tmp_path: Path) -> None:
    daily, calendar, _ = _inputs(tmp_path)
    output = cli.PROJECT_ROOT / 'data/daily/forbidden_repair_candidate_test'
    assert not output.exists()
    with pytest.raises(ValueError, match='outside the source checkout'):
        cli.prepare_candidates(input_dir=daily, output_dir=output, symbols=['002633'],
                               start_date='2026-09-07', target_date='2026-09-10', calendar=calendar)
    assert not output.exists()


@pytest.mark.parametrize('git_metadata', ['directory', 'worktree_file'])
def test_output_inside_another_source_repository_is_rejected(tmp_path: Path, git_metadata: str) -> None:
    daily, calendar, original = _inputs(tmp_path)
    other_repository = tmp_path / 'clean_source_repository'
    other_repository.mkdir()
    if git_metadata == 'directory':
        (other_repository / '.git').mkdir()
    else:
        git_dir = tmp_path / 'git_metadata'
        git_dir.mkdir()
        (other_repository / '.git').write_text(f'gitdir: {git_dir.as_posix()}\n', encoding='utf-8')
    output = other_repository / 'candidate_bundle'
    with pytest.raises(ValueError, match='outside the source checkout'):
        cli.prepare_candidates(input_dir=daily, output_dir=output, symbols=['002633'],
                               start_date='2026-09-07', target_date='2026-09-10', calendar=calendar)
    assert not output.exists()
    assert (daily / '002633.csv').read_bytes() == original


def test_existing_bundle_is_not_overwritten(tmp_path: Path) -> None:
    _inputs(tmp_path)
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle / 'keep.txt').write_text('existing evidence', encoding='utf-8')
    with pytest.raises(ValueError, match='new directory'):
        _prepare(tmp_path)
    assert (bundle / 'keep.txt').read_text(encoding='utf-8') == 'existing evidence'
    assert len(list(bundle.iterdir())) == 1


@pytest.mark.parametrize('symbol', ['../002633', '002633.csv', '\u0660\u0660\u0662\u0666\u0663\u0663'])
def test_noncanonical_symbols_cannot_select_an_arbitrary_file(tmp_path: Path, symbol: str) -> None:
    daily, calendar, _ = _inputs(tmp_path)
    with pytest.raises(ValueError, match='six ASCII digits'):
        cli.prepare_candidates(input_dir=daily, output_dir=tmp_path / 'bundle', symbols=[symbol],
                               start_date='2026-09-07', target_date='2026-09-10', calendar=calendar)
    assert not (tmp_path / 'bundle').exists()


def test_source_change_during_preparation_blocks_the_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daily, _, original = _inputs(tmp_path)
    core = cli.prepare_daily_repair_candidate
    changed = original + _row('2026-09-09')

    def racing_core(**kwargs):
        result = core(**kwargs)
        (daily / '002633.csv').write_bytes(changed)
        return result

    monkeypatch.setattr(cli, 'prepare_daily_repair_candidate', racing_core)
    report = _prepare(tmp_path)
    assert report['status'] == 'blocked'
    assert 'input_changed_during_preparation' in report['items'][0]['reasons']
    assert report['items'][0]['ok'] is False
    assert report['items'][0]['candidate_sha256'] is None
    assert report['items'][0]['candidate_path'] is None
    assert not (tmp_path / 'bundle/candidates/002633.csv').exists()
    assert (daily / '002633.csv').read_bytes() == changed


@pytest.mark.parametrize('changed_input', ['original', 'supplemental', 'calendar'])
def test_final_bundle_recheck_invalidates_a_candidate_whose_input_changed_after_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_input: str,
) -> None:
    daily, calendar, original = _inputs(tmp_path)
    (daily / '002634.csv').write_bytes(original.replace(b'002633', b'002634'))
    supplements = tmp_path / 'supplements'
    supplements.mkdir()
    for symbol in ('002633', '002634'):
        (supplements / f'{symbol}.csv').write_bytes(HEADER + _row('2026-09-09', symbol) + _row('2026-09-10', symbol))
    changed_path = {'original': daily / '002633.csv', 'supplemental': supplements / '002633.csv',
                    'calendar': calendar}[changed_input]
    changed_bytes = changed_path.read_bytes() + b'\n'
    write_new = cli._write_new

    def racing_write(path: Path, data: bytes) -> None:
        write_new(path, data)
        if path == tmp_path / 'bundle/originals/002633.csv':
            changed_path.write_bytes(changed_bytes)

    monkeypatch.setattr(cli, '_write_new', racing_write)
    report = cli.prepare_candidates(
        input_dir=daily, output_dir=tmp_path / 'bundle', symbols=['002633', '002634'],
        start_date='2026-09-07', target_date='2026-09-10', calendar=calendar, supplemental_dir=supplements,
    )
    first = report['items'][0]
    assert report['status'] != 'candidates_ready'
    assert first['status'] == 'blocked'
    assert first['ok'] is False
    assert 'input_changed_during_preparation' in first['reasons']
    assert first['candidate_path'] is None
    assert first['candidate_sha256'] is None
    assert first['input_hashes_match_at_final_check'] is False
    assert first['invalidated_candidate_path'] == 'candidates/002633.csv'
    assert (tmp_path / 'bundle' / first['invalidated_candidate_path']).exists()
    assert (tmp_path / 'bundle/originals/002633.csv').read_bytes() == original
    assert json.loads((tmp_path / 'bundle/manifests/002633.json').read_text(encoding='utf-8')) == first
    assert report['requires_locked_hash_recheck'] is True


def test_unexpected_error_leaves_aborted_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daily, _, original = _inputs(tmp_path)

    def fail_core(**kwargs):
        raise RuntimeError('injected preparation error')

    monkeypatch.setattr(cli, 'prepare_daily_repair_candidate', fail_core)
    with pytest.raises(RuntimeError, match='injected preparation error'):
        _prepare(tmp_path)
    report = json.loads((tmp_path / 'bundle/manifest.json').read_text(encoding='utf-8'))
    assert report['status'] == 'aborted'
    assert report['error']['type'] == 'RuntimeError'
    assert report['applied'] is False
    assert (daily / '002633.csv').read_bytes() == original


def test_review_selection_is_limited_to_the_requested_mismatch_and_date(tmp_path: Path) -> None:
    daily, calendar, original = _inputs(tmp_path)
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'target_date': '2026-09-10', 'flagged_for_review': {
        '002633': {'reason': 'daily_history_column_mismatch:002633.csv'},
        '600000': {'reason': 'invalid_daily_bar:open_not_positive'},
    }}), encoding='utf-8')
    expected = tmp_path / 'hashes.json'
    expected.write_text(json.dumps({'002633': hashlib.sha256(original).hexdigest()}), encoding='utf-8')
    code = cli.main([
        '--input-dir', str(daily), '--output-dir', str(tmp_path / 'bundle'), '--review-json', str(review),
        '--start-date', '2026-09-07', '--target-date', '2026-09-10', '--calendar', str(calendar),
        '--expected-hashes', str(expected),
    ])
    assert code == 3
    report = json.loads((tmp_path / 'bundle/manifest.json').read_text(encoding='utf-8'))
    assert report['symbols'] == ['002633']
    assert report['items'][0]['expected_hash_source'] == 'provided_manifest'
    assert report['selection_evidence']['sha256'] == hashlib.sha256(review.read_bytes()).hexdigest()


def test_review_from_another_round_is_rejected_before_creating_output(tmp_path: Path) -> None:
    daily, _, _ = _inputs(tmp_path)
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'target_date': '2026-09-09', 'flagged_for_review': {}}), encoding='utf-8')
    assert cli.main(['--input-dir', str(daily), '--output-dir', str(tmp_path / 'bundle'),
                     '--review-json', str(review), '--start-date', '2026-09-07', '--target-date', '2026-09-10']) == 2
    assert not (tmp_path / 'bundle').exists()
