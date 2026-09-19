"""Prepare offline daily CSV repair candidates outside the source repository.

This command never applies a candidate, requests quotes, or invokes the pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_repair_candidate import prepare_daily_repair_candidate


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _repository_roots(path: Path) -> set[Path]:
    """Recognize both an ordinary checkout and its linked worktree, read-only."""
    roots: set[Path] = set()
    for parent in (path, *path.parents):
        marker = parent / '.git'
        if marker.is_dir():
            roots.add(parent.resolve())
            break
        if marker.is_file():
            roots.add(parent.resolve())
            text = marker.read_text(encoding='utf-8').strip()
            if not text.startswith('gitdir: '):
                raise ValueError(f'Invalid worktree metadata: {marker}')
            git_dir = (parent / text[len('gitdir: '):]).resolve()
            common = git_dir / 'commondir'
            if common.is_file():
                common_dir = (git_dir / common.read_text(encoding='utf-8').strip()).resolve()
                if common_dir.name == '.git':
                    roots.add(common_dir.parent)
            break
    return roots


def _contained_file(directory: Path, symbol: str) -> Path:
    path = (directory / f'{symbol}.csv').resolve()
    if not path.is_relative_to(directory):
        raise ValueError(f'Symbol input leaves its declared directory: {symbol}')
    return path


def _write_new(path: Path, data: bytes) -> None:
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    _write_new(temporary, data)
    os.replace(temporary, path)


def _input_matches(path: Path, captured_sha256: str | None) -> bool:
    try:
        return _sha256(path.read_bytes()) == captured_sha256
    except FileNotFoundError:
        return captured_sha256 is None
    except OSError:
        return False


def _invalidate_changed_input(item: dict[str, Any]) -> None:
    if item.get('candidate_path'):
        # Retain already written evidence, but remove it from the usable candidate fields.
        item['invalidated_candidate_path'] = item['candidate_path']
        item['invalidated_candidate_sha256'] = item['candidate_sha256']
    item.update(status='blocked', ok=False, candidate_path=None, candidate_sha256=None)
    if 'input_changed_during_preparation' not in item['reasons']:
        item['reasons'].append('input_changed_during_preparation')


def prepare_candidates(
    *, input_dir: Path, output_dir: Path, symbols: Sequence[str], start_date: str,
    target_date: str, calendar: Path | None = None, supplemental_dir: Path | None = None,
    expected_hashes: dict[str, str] | None = None,
    selection_evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write a new external bundle; originals remain read-only and unapplied."""
    input_dir = input_dir.expanduser().resolve(strict=True)
    output_dir = output_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise ValueError('input_dir must be a directory')
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError('Select at least one symbol without duplicates')
    if any(not re.fullmatch(r'[0-9]{6}', symbol) for symbol in symbols):
        raise ValueError('Every symbol must be exactly six ASCII digits')
    start, target = date.fromisoformat(start_date), date.fromisoformat(target_date)
    if start.isoformat() != start_date or target.isoformat() != target_date or start > target:
        raise ValueError('Use an ordered YYYY-MM-DD date range')
    if expected_hashes is not None and any(
        not re.fullmatch(r'[0-9a-fA-F]{64}', str(expected_hashes.get(symbol, '')))
        for symbol in symbols
    ):
        raise ValueError('expected_hashes must contain one SHA256 for every selected symbol')
    supplemental_dir = supplemental_dir.expanduser().resolve(strict=True) if supplemental_dir else None
    if supplemental_dir is not None and not supplemental_dir.is_dir():
        raise ValueError('supplemental_dir must be a directory')
    calendar = calendar.expanduser().resolve(strict=True) if calendar else None
    roots = _repository_roots(PROJECT_ROOT) | _repository_roots(input_dir) | _repository_roots(output_dir)
    if any(output_dir.is_relative_to(root) for root in roots):
        raise ValueError('output_dir must be outside the source checkout, canonical checkout, and every Git repository')
    for protected in (input_dir, supplemental_dir, calendar):
        if protected is not None and (output_dir.is_relative_to(protected) or protected.is_relative_to(output_dir)):
            raise ValueError('Output and input paths must not overlap')
    if output_dir.exists():
        raise ValueError('output_dir must be a new directory; existing bundles are never overwritten')
    calendar_bytes = calendar.read_bytes() if calendar else None
    output_dir.mkdir(parents=True, exist_ok=False)
    for name in ('originals', 'candidates', 'manifests', 'evidence'):
        (output_dir / name).mkdir()
    report: dict[str, Any] = {
        'schema_version': 1, 'status': 'preparing', 'created_at': datetime.now().astimezone().isoformat(),
        'input_dir': str(input_dir), 'output_dir': str(output_dir), 'start_date': start_date,
        'target_date': target_date, 'symbols': list(symbols), 'items': [],
        'calendar': {'path': str(calendar), 'sha256': _sha256(calendar_bytes)} if calendar_bytes is not None else None,
        'selection_evidence': selection_evidence, 'canonical_write': False,
        'network_effective': False, 'applied': False, 'restore_plan_only': True,
        'requires_locked_hash_recheck': True,
    }
    captured_inputs: dict[str, list[tuple[Path, str | None]]] = {}
    manifest_path = output_dir / 'manifest.json'
    _write_manifest(manifest_path, report)
    try:
        if calendar_bytes is not None:
            _write_new(output_dir / 'evidence/calendar.csv', calendar_bytes)
        for symbol in symbols:
            source = _contained_file(input_dir, symbol)
            original = source.read_bytes()
            original_sha = _sha256(original)
            supplemental_path = _contained_file(supplemental_dir, symbol) if supplemental_dir else None
            supplemental = supplemental_path.read_bytes() if supplemental_path and supplemental_path.is_file() else None
            fingerprints: list[tuple[Path, str | None]] = [(source, original_sha)]
            if supplemental_path is not None:
                fingerprints.append((supplemental_path, _sha256(supplemental) if supplemental is not None else None))
            if calendar is not None:
                fingerprints.append((calendar, _sha256(calendar_bytes)))
            captured_inputs[symbol] = fingerprints
            expected = expected_hashes[symbol].lower() if expected_hashes is not None else original_sha
            result = prepare_daily_repair_candidate(
                symbol=symbol, original_bytes=original, expected_sha256=expected,
                start_date=start_date, target_date=target_date,
                calendar_bytes=calendar_bytes, supplemental_bytes=supplemental,
            )
            item = dict(result.manifest)
            item.update({'symbol': symbol, 'input_path': str(source), 'observed_original_sha256': original_sha,
                         'expected_hash_source': 'provided_manifest' if expected_hashes is not None else 'observed_at_read',
                         'original_backup': f'originals/{symbol}.csv', 'candidate_path': None,
                         'supplemental_path': str(supplemental_path) if supplemental_path else None})
            candidate = result.candidate_bytes
            # A writer may have changed an input while this offline candidate was prepared.
            if not all(_input_matches(path, sha) for path, sha in fingerprints):
                _invalidate_changed_input(item)
                candidate = None
            _write_new(output_dir / item['original_backup'], original)
            if supplemental is not None:
                _write_new(output_dir / 'evidence' / f'{symbol}.csv', supplemental)
            if candidate is not None and item.get('status') != 'blocked':
                item['candidate_path'] = f'candidates/{symbol}.csv'
                _write_new(output_dir / item['candidate_path'], candidate)
            _write_manifest(output_dir / 'manifests' / f'{symbol}.json', item)
            report['items'].append(item)
            _write_manifest(manifest_path, report)
        # Earlier symbols may change while later ones are prepared. This is still
        # an offline observation; applying a bundle requires another locked check.
        for item in report['items']:
            stable = all(_input_matches(path, sha) for path, sha in captured_inputs[item['symbol']])
            item['input_hashes_match_at_final_check'] = stable
            if not stable:
                _invalidate_changed_input(item)
            _write_manifest(output_dir / 'manifests' / f"{item['symbol']}.json", item)
        report['input_stability_checked_at'] = datetime.now().astimezone().isoformat()
        counts = Counter(item['status'] for item in report['items'])
        report['counts'] = dict(counts)
        report['status'] = ('candidates_ready' if counts.get('candidate_ready') == len(symbols)
                            else 'blocked' if counts.get('blocked') == len(symbols) else 'incomplete')
        report['completed_at'] = datetime.now().astimezone().isoformat()
        _write_manifest(manifest_path, report)
    except BaseException as exc:
        report['status'] = 'aborted'
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        _write_manifest(manifest_path, report)
        raise
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--symbols', nargs='+')
    selection.add_argument('--review-json', type=Path, help='Select only daily_history_column_mismatch entries')
    parser.add_argument('--start-date', required=True)
    parser.add_argument('--target-date', required=True)
    parser.add_argument('--calendar', type=Path)
    parser.add_argument('--supplemental-dir', type=Path)
    parser.add_argument('--expected-hashes', type=Path, help='JSON mapping of symbol to expected original SHA256')
    args = parser.parse_args(argv)
    try:
        selection_evidence = None
        symbols = args.symbols
        if args.review_json:
            review_bytes = args.review_json.read_bytes()
            review = json.loads(review_bytes.decode('utf-8-sig'))
            if review.get('target_date') != args.target_date:
                raise ValueError('review target_date differs from the requested target_date')
            symbols = sorted(symbol for symbol, item in review['flagged_for_review'].items()
                             if item.get('reason') == f'daily_history_column_mismatch:{symbol}.csv')
            selection_evidence = {'path': str(args.review_json.resolve()), 'sha256': _sha256(review_bytes)}
        expected = json.loads(args.expected_hashes.read_text(encoding='utf-8-sig')) if args.expected_hashes else None
        if expected is not None and not isinstance(expected, dict):
            raise ValueError('expected-hashes must contain a JSON object')
        report = prepare_candidates(
            input_dir=args.input_dir, output_dir=args.output_dir, symbols=symbols,
            start_date=args.start_date, target_date=args.target_date, calendar=args.calendar,
            supplemental_dir=args.supplemental_dir, expected_hashes=expected,
            selection_evidence=selection_evidence,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({key: report[key] for key in ('status', 'counts', 'output_dir', 'canonical_write', 'network_effective', 'applied')},
                     ensure_ascii=False))
    return 0 if report['status'] == 'candidates_ready' else 2 if report['status'] == 'blocked' else 3


if __name__ == '__main__':
    raise SystemExit(main())
