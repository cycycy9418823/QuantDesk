"""Independent, offline stdlib tests for the single M3 bootstrap research job.

All products and fault-injection scripts use short external temporary roots.
Child processes preserve the caller's environment/audit guard and use -B.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
from quantdesk.research import resumable_bootstrap_job as job
from quantdesk.research.backtest_statistical_validation import bootstrap_metric_confidence_intervals

ITERATIONS = 24
SEED = 17
VALUES = [((index * 7) % 17 - 8) / 1000 for index in range(40)]
CSV_BYTES = ('return\n' + ''.join(f'{value:.6f}\n' for value in VALUES)).encode('ascii')
CLI = SOURCE_ROOT / 'scripts/run_resumable_bootstrap_job.py'

DRIVER = r'''
import os, sys, time
from pathlib import Path
source, directory, mode, stage = sys.argv[1:5]
sys.path.insert(0, source)
from quantdesk.research import resumable_bootstrap_job as job
root = Path(directory)
original = job._record_stage
def record(job_dir, checkpoint, name, output_path):
    if name == stage:
        (root / 'child_ready').write_text(name, encoding='ascii')
        if mode == 'crash':
            os._exit(73)
        deadline = time.monotonic() + 12
        while not (root / 'child_stop').exists() and time.monotonic() < deadline:
            time.sleep(.025)
    return original(job_dir, checkpoint, name, output_path)
job._record_stage = record
job.run_job(job_root=root / 'jobs', job_id='child', input_path=root / 'returns.csv', iterations=24, seed=17)
'''


class SimulatedCrash(BaseException):
    pass


def canonical(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


class ResumableBootstrapJobTests(unittest.TestCase):
    def setUp(self) -> None:
        preferred = os.environ.get('TMP') or os.environ.get('TEMP')
        temporary = tempfile.TemporaryDirectory(prefix='m3-', dir=preferred or None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.jobs = self.root / 'jobs'
        self.input = self.root / 'returns.csv'
        self.input.write_bytes(CSV_BYTES)
        self.children = []
        self.logs = []
        self.addCleanup(self._stop_children)

    def _stop_children(self) -> None:
        (self.root / 'child_stop').touch()
        for child in self.children:
            try:
                child.wait(timeout=14)
            except subprocess.TimeoutExpired:
                child.terminate()  # only the exact Popen created by this test
                child.wait(timeout=5)
        for log in self.logs:
            log.close()

    def _run(self, name='case', *, input_path=..., **kwargs):
        return job.run_job(job_root=self.jobs, job_id=name,
            input_path=self.input if input_path is ... else input_path,
            iterations=kwargs.pop('iterations', ITERATIONS), seed=kwargs.pop('seed', SEED), **kwargs)

    def _path(self, name='case', filename='result.json') -> Path:
        return self.jobs / name / filename

    def _doc(self, name='case', filename='checkpoint.json'):
        return json.loads(self._path(name, filename).read_bytes())

    def _assert_error(self, code, call):
        with self.assertRaises(job.JobError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def _interrupt(self, stage: str, name='case') -> None:
        original = job._record_stage

        def record(directory, checkpoint, current, output):
            if current == stage:
                self.assertTrue(output.is_file())
                raise SimulatedCrash(stage)
            return original(directory, checkpoint, current, output)

        with mock.patch.object(job, '_record_stage', side_effect=record), self.assertRaises(SimulatedCrash):
            self._run(name)

    def _spawn_driver(self, mode: str, stage: str):
        driver = self.root / 'fault_driver.py'
        driver.write_text(DRIVER, encoding='utf-8')
        log = (self.root / f'{mode}-{stage}.log').open('wb')
        self.logs.append(log)
        child = subprocess.Popen([sys.executable, '-B', str(driver), str(SOURCE_ROOT), str(self.root), mode, stage],
            cwd=self.root, env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.children.append(child)
        return child

    def _wait_ready(self, child) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (self.root / 'child_ready').exists():
                return
            if child.poll() is not None:
                break
            time.sleep(.02)
        logs = {p.name: p.read_text(encoding='utf-8', errors='replace') for p in self.root.glob('*.log')}
        self.fail(f'bounded fixture never reached checkpoint boundary: {logs}')

    def _cli(self, *arguments):
        number = len(list(self.root.glob('cli-*.stdout')))
        out = self.root / f'cli-{number}.stdout'
        err = self.root / f'cli-{number}.stderr'
        with out.open('wb') as stdout, err.open('wb') as stderr:
            result = subprocess.run([sys.executable, '-B', str(CLI), *map(str, arguments)],
                cwd=self.root, env=dict(os.environ), stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, timeout=20, check=False)
        return result.returncode, out.read_text(encoding='utf-8'), err.read_text(encoding='utf-8')

    def test_adaptor_uses_existing_statistics_and_truthful_interpretation(self) -> None:
        result = self._run()
        self.assertEqual(result['bootstrap'], bootstrap_metric_confidence_intervals(VALUES, iterations=ITERATIONS, seed=SEED))
        self.assertEqual(result['row_count'], 40)
        self.assertEqual(result['binding']['input_sha256'], hashlib.sha256(CSV_BYTES).hexdigest())
        self.assertTrue(result['research_only'])
        self.assertEqual(result['validation_status'], '研究输出·未验证')
        self.assertIn('not annualized', result['interpretation']['annual_return_ci'])
        self.assertIn('not annualized', result['interpretation']['sample_compounded_return_ci'])
        self.assertEqual(result['bootstrap']['annual_return_ci'], [None, None])
        self.assertEqual(result['bootstrap']['annualization_status'], 'unavailable_without_observation_frequency')
        self.assertIn('not annualized', result['interpretation']['sharpe_ci'])
        self.assertIn('not evidence', result['interpretation']['passed'])

    def test_fixed_observations_keep_compounded_return_separate_from_unknown_annual_return(self) -> None:
        self.input.write_text('return\n' + '0.01\n' * 8, encoding='ascii')
        result = self._run()
        expected = round(1.01 ** 8 - 1, 8)
        self.assertEqual(result['bootstrap']['sample_compounded_return_ci'], [expected, expected])
        self.assertEqual(result['bootstrap']['annual_return_ci'], [None, None])
        self.assertEqual(result['binding']['implementation_version'], '2')

    def test_short_sample_has_no_invented_return_interval_and_can_resume(self) -> None:
        self.input.write_text('return\n0.01\n0.02\n', encoding='ascii')
        result = self._run()
        self.assertEqual(result['bootstrap']['status'], 'warning')
        self.assertEqual(result['bootstrap']['warnings'], ['insufficient_sample'])
        self.assertEqual(result['bootstrap']['annual_return_ci'], [None, None])
        self.assertEqual(result['bootstrap']['sample_compounded_return_ci'], [None, None])
        with mock.patch.object(job, 'bootstrap_metric_confidence_intervals', side_effect=AssertionError('duplicate compute')):
            self.assertEqual(self._run(input_path=None), result)

    def test_result_bytes_are_independent_of_job_id_path_and_attempt_clock(self) -> None:
        self._run('first')
        with mock.patch.object(job, '_now', return_value='2035-01-02T03:04:05+00:00'):
            other_root = self.root / 'another-root'
            job.run_job(job_root=other_root, job_id='other', input_path=self.input, iterations=ITERATIONS, seed=SEED)
        first = self._path('first').read_bytes()
        self.assertEqual(first, (other_root / 'other/result.json').read_bytes())
        self.assertNotIn(b'\n', first)
        self.assertNotIn(str(self.root).encode(), first)
        self.assertNotIn(b'2035-01-02', first)

    def test_defaults_are_explicit_and_stable(self) -> None:
        result = job.run_job(job_root=self.jobs, job_id='defaults', input_path=self.input)
        self.assertEqual(result['binding']['iterations'], 500)
        self.assertEqual(result['binding']['seed'], 42)

    def test_snapshot_preserves_exact_crlf_and_bom_input_bytes(self) -> None:
        raw = b'\xef\xbb\xbf' + CSV_BYTES.replace(b'\n', b'\r\n')
        self.input.write_bytes(raw)
        self._interrupt('compute')
        snapshot = self._doc(filename='input_snapshot.json')
        self.assertEqual(base64.b64decode(snapshot['base64']), raw)
        self.input.unlink()
        result = self._run(input_path=None)
        self.assertEqual(result['binding']['input_sha256'], hashlib.sha256(raw).hexdigest())

    def test_resume_from_snapshot_after_original_input_is_removed(self) -> None:
        self._run('full')
        self._interrupt('compute')
        self.assertEqual(set(self._doc()['completed']), {'validate'})
        self.input.unlink()
        self._run(input_path=None)
        self.assertEqual(self._path().read_bytes(), self._path('full').read_bytes())
        attempts = self._doc(filename='attempts.json')['attempts']
        self.assertEqual(attempts[0]['status'], 'unfinished')
        self.assertIsNone(attempts[0]['ended_at'])
        self.assertIn('recovered_at', attempts[0])
        self.assertEqual(attempts[-1]['status'], 'completed')

    def test_initial_binding_without_snapshot_requires_exact_original_input(self) -> None:
        self._run('full')
        original_atomic = job._atomic_json

        def interrupt_snapshot(path, payload):
            if path.name == 'input_snapshot.json':
                raise SimulatedCrash('before snapshot publication')
            return original_atomic(path, payload)

        with mock.patch.object(job, '_atomic_json', side_effect=interrupt_snapshot), self.assertRaises(SimulatedCrash):
            self._run()
        checkpoint_before = self._path(filename='checkpoint.json').read_bytes()
        self.assertEqual(self._doc()['completed'], {})
        self.assertEqual(self._doc()['job_id'], 'case')
        self.assertFalse(self._path(filename='input_snapshot.json').exists())
        self._assert_error('snapshot_missing_input_required', lambda: self._run(input_path=None))
        self.input.write_bytes(CSV_BYTES + b'0\n')
        self._assert_error('binding_mismatch', self._run)
        self.assertEqual(self._path(filename='checkpoint.json').read_bytes(), checkpoint_before)
        self.assertFalse(self._path(filename='input_snapshot.json').exists())
        self.input.write_bytes(CSV_BYTES)
        self._run()
        self.assertEqual(self._path().read_bytes(), self._path('full').read_bytes())

    def test_started_job_missing_snapshot_is_not_reinitialized(self) -> None:
        self._run()
        before = self._path().read_bytes()
        self._path(filename='input_snapshot.json').unlink()
        self._assert_error('snapshot_missing_from_started_job', self._run)
        self.assertFalse(self._path(filename='input_snapshot.json').exists())
        self.assertEqual(self._path().read_bytes(), before)

    def test_true_process_crash_after_validation_output_resumes_exactly(self) -> None:
        self._run('full')
        child = self._spawn_driver('crash', 'validate')
        self._wait_ready(child)
        self.assertEqual(child.wait(timeout=5), 73)
        self.assertEqual(self._doc('child')['completed'], {})
        self.assertTrue(self._path('child', 'validate.json').is_file())
        self._run('child', input_path=None)
        self.assertEqual(self._path('child').read_bytes(), self._path('full').read_bytes())

    def test_true_process_crash_after_compute_output_resumes_exactly(self) -> None:
        self._run('full')
        child = self._spawn_driver('crash', 'compute')
        self._wait_ready(child)
        self.assertEqual(child.wait(timeout=5), 73)
        self.assertEqual(set(self._doc('child')['completed']), {'validate'})
        self.assertTrue(self._path('child', 'compute.json').is_file())
        self._run('child', input_path=None)
        self.assertEqual(self._path('child').read_bytes(), self._path('full').read_bytes())
        self.assertEqual(self._doc('child', 'attempts.json')['attempts'][0]['status'], 'unfinished')

    def test_true_process_crash_after_final_output_does_not_republish_or_recompute(self) -> None:
        self._run('full')
        child = self._spawn_driver('crash', 'publish')
        self._wait_ready(child)
        self.assertEqual(child.wait(timeout=5), 73)
        self.assertEqual(set(self._doc('child')['completed']), {'validate', 'compute'})
        before = self._path('child').read_bytes()
        original = job._atomic_json

        def atomic(path, payload):
            self.assertNotEqual(path.name, 'result.json', 'identical unrecorded result must not be republished')
            return original(path, payload)

        with mock.patch.object(job, '_atomic_json', side_effect=atomic), mock.patch.object(
            job, 'bootstrap_metric_confidence_intervals', side_effect=AssertionError('completed compute was rerun')
        ):
            self._run('child', input_path=None)
        self.assertEqual(before, self._path('child').read_bytes())
        self.assertEqual(before, self._path('full').read_bytes())
        self.assertEqual(set(self._doc('child')['completed']), {'validate', 'compute', 'publish'})

    def test_incomplete_partial_output_is_recomputed_instead_of_accepted(self) -> None:
        self._run('full')
        self._interrupt('compute')
        self._path(filename='compute.json').write_bytes(b'{partial')
        with mock.patch.object(job, 'bootstrap_metric_confidence_intervals', wraps=bootstrap_metric_confidence_intervals) as compute:
            self._run(input_path=None)
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(self._path().read_bytes(), self._path('full').read_bytes())

    def test_completed_reentry_preserves_artifacts_and_never_calls_compute(self) -> None:
        expected = self._run()
        names = ['checkpoint.json', 'input_snapshot.json', 'validate.json', 'compute.json', 'result.json']
        before = {name: (self._path(filename=name).read_bytes(), self._path(filename=name).stat().st_mtime_ns) for name in names}
        with mock.patch.object(job, 'bootstrap_metric_confidence_intervals', side_effect=AssertionError('duplicate compute')):
            self.assertEqual(self._run(input_path=None), expected)
        after = {name: (self._path(filename=name).read_bytes(), self._path(filename=name).stat().st_mtime_ns) for name in names}
        self.assertEqual(after, before)
        self.assertEqual(len(list(self.jobs.rglob('result.json'))), 1)

    def test_explicit_different_input_is_rejected_without_changing_result(self) -> None:
        self._run()
        before = self._path().read_bytes()
        self.input.write_bytes(CSV_BYTES + b'0.001\n')
        self._assert_error('input_binding_mismatch', self._run)
        self.assertEqual(self._path().read_bytes(), before)

    def test_identical_input_bytes_at_another_path_can_resume(self) -> None:
        expected = self._run()
        copy = self.root / 'same.csv'
        copy.write_bytes(CSV_BYTES)
        self.assertEqual(self._run(input_path=copy), expected)

    def test_changed_parameters_are_rejected_without_rewriting_completed_result(self) -> None:
        self._run()
        before = self._path().read_bytes()
        for kwargs in ({'iterations': ITERATIONS + 1}, {'seed': SEED + 1}):
            with self.subTest(kwargs=kwargs):
                self._assert_error('binding_mismatch', lambda: self._run(**kwargs))
                self.assertEqual(self._path().read_bytes(), before)

    def test_changed_source_python_or_implementation_binding_is_rejected(self) -> None:
        self._run()
        before = self._path().read_bytes()
        changed_sources = {**job._source_hashes(), 'quantdesk/shared.py': '0' * 64}
        patches = [mock.patch.object(job, '_source_hashes', return_value=changed_sources),
                   mock.patch.object(job, '_python_binding', return_value={'implementation': 'Other', 'version': '0'}),
                   mock.patch.object(job, 'IMPLEMENTATION_VERSION', 'next')]
        for patch in patches:
            with self.subTest(binding=patch.attribute), patch:
                self._assert_error('binding_mismatch', self._run)
                self.assertEqual(self._path().read_bytes(), before)

    def test_malformed_noncanonical_and_duplicate_key_checkpoints_are_rejected(self) -> None:
        self._run()
        path = self._path(filename='checkpoint.json')
        before_result = self._path().read_bytes()
        for raw in (b'{', b'{"x":1,"x":2}', b'{"x":NaN}', b' {}\n'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self._assert_error('malformed_json', self._run)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(self._path().read_bytes(), before_result)

    def test_checkpoint_schema_stage_order_and_arbitrary_output_path_are_rejected(self) -> None:
        self._run()
        original = self._doc()
        mutations = []
        changed = json.loads(json.dumps(original)); changed['schema_version'] = 'wrong'
        mutations.append(('checkpoint_schema_mismatch', changed))
        changed = json.loads(json.dumps(original)); changed['completed'] = {'compute': changed['completed']['compute']}
        mutations.append(('malformed_stage_order', changed))
        changed = json.loads(json.dumps(original)); changed['completed']['validate']['path'] = str(self.root / 'outside.json')
        mutations.append(('malformed_stage_descriptor', changed))
        for code, changed in mutations:
            with self.subTest(code=code):
                self._path(filename='checkpoint.json').write_bytes(canonical(changed))
                self._assert_error(code, self._run)
        self.assertFalse((self.root / 'outside.json').exists())

    def test_missing_checkpoint_does_not_silently_restart_existing_job(self) -> None:
        self._run()
        before = self._path().read_bytes()
        self._path(filename='checkpoint.json').unlink()
        self._assert_error('checkpoint_missing_from_existing_job', self._run)
        self.assertEqual(before, self._path().read_bytes())

    def test_missing_or_tampered_recorded_outputs_are_rejected(self) -> None:
        self._run()
        for name in ('validate.json', 'compute.json', 'result.json'):
            path = self._path(filename=name)
            original = path.read_bytes()
            with self.subTest(file=name, mode='missing'):
                path.unlink()
                self._assert_error('required_file_missing', self._run)
            with self.subTest(file=name, mode='tampered'):
                path.write_bytes(b'{}')
                self._assert_error('stage_output_integrity_mismatch', self._run)
            path.write_bytes(original)

    def test_tampered_snapshot_cannot_be_replaced_by_supplied_original(self) -> None:
        self._run()
        self._path(filename='input_snapshot.json').write_bytes(b'{}')
        self._assert_error('snapshot_integrity_mismatch', self._run)

    def test_invalid_bootstrap_schema_is_rejected_even_with_matching_recorded_hash(self) -> None:
        self._run()
        original_compute = self._doc(filename='compute.json')
        original_checkpoint = self._doc()
        result_before = self._path().read_bytes()
        changes = [('iterations', True), ('sharpe_ci', [2, 1]), ('win_rate_ci', [-1, 2]),
                   ('annual_return_ci', ['not-a-number', 0]), ('annual_return_ci', [0.01, 0.10]),
                   ('sample_compounded_return_ci', [None, None]),
                   ('sample_compounded_return_ci', [0.02, 0.01]),
                   ('sample_compounded_return_ci', [-2, -1]),
                   ('annualization_status', 'annualized')]
        for field, value in changes:
            with self.subTest(field=field):
                computation = json.loads(json.dumps(original_compute))
                computation['bootstrap'][field] = value
                raw = canonical(computation)
                checkpoint = json.loads(json.dumps(original_checkpoint))
                checkpoint['completed']['compute'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                self._path(filename='compute.json').write_bytes(raw)
                self._path(filename='checkpoint.json').write_bytes(canonical(checkpoint))
                self._assert_error('computation_output_schema_mismatch', self._run)
                self.assertEqual(self._path().read_bytes(), result_before)
        invalid_new = json.loads(json.dumps(original_compute['bootstrap']))
        invalid_new['annual_return_ci'] = [0, float('inf')]
        with mock.patch.object(job, 'bootstrap_metric_confidence_intervals', return_value=invalid_new):
            self._assert_error('computation_output_schema_mismatch', lambda: self._run('invalid-new-result'))
        self.assertFalse(self._path('invalid-new-result').exists())

    def test_atomic_replace_failure_keeps_previously_published_bytes(self) -> None:
        self._run()
        path = self._path()
        before = path.read_bytes()
        with mock.patch.object(job.shared.os, 'replace', side_effect=OSError('injected replace failure')):
            with self.assertRaises(OSError):
                job._atomic_json(path, {'replacement': 'must never be visible'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_nonfinite_and_out_of_range_returns_never_reach_computation(self) -> None:
        for index, text in enumerate(('nan', 'inf', '-inf', '1.01', '-1.01')):
            with self.subTest(value=text), mock.patch.object(job, 'bootstrap_metric_confidence_intervals', side_effect=AssertionError('invalid input reached compute')):
                self.input.write_text('return\n' + text + '\n', encoding='ascii')
                self._assert_error('return_out_of_range', lambda: self._run(f'invalid{index}'))

    def test_csv_schema_and_empty_samples_fail_clearly(self) -> None:
        cases = [(b'value\n0.1\n', 'input_csv_schema'), (b'return,return\n0.1,0.1\n', 'input_csv_schema'),
                 (b'return\n0.1,extra\n', 'input_csv_schema'), (b'return\nword\n', 'invalid_return'),
                 (b'return\n', 'empty_return_sample'), (b'\xff\xfe', 'input_csv_schema')]
        for index, (raw, code) in enumerate(cases):
            with self.subTest(code=code, raw=raw):
                self.input.write_bytes(raw)
                self._assert_error(code, lambda: self._run(f'schema{index}'))

    def test_parameter_bounds_and_types_are_checked_before_job_creation(self) -> None:
        cases = [('iterations_out_of_budget', {'iterations': value}) for value in (0, 5001, True, 1.5)]
        cases += [('seed_out_of_range', {'seed': value}) for value in (-1, 2**32, True, 1.5)]
        for code, kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self._assert_error(code, lambda: self._run(**kwargs))
        self.assertFalse(self.jobs.exists())

    def test_input_size_row_work_and_compounding_budgets_precede_computation(self) -> None:
        cases = [(b'x' * (job.MAX_INPUT_BYTES + 1), ITERATIONS, 'file_budget_exceeded'),
                 (b'return\n' + b'0\n' * (job.MAX_ROWS + 1), 1, 'computation_budget_exceeded'),
                 (b'return\n' + b'0\n' * 401, 5000, 'computation_budget_exceeded'),
                 (b'return\n' + b'1\n' * 1000, 1, 'compounding_overflow_budget')]
        for index, (raw, iterations, code) in enumerate(cases):
            with self.subTest(code=code), mock.patch.object(job, 'bootstrap_metric_confidence_intervals', side_effect=AssertionError('budget violation reached compute')):
                self.input.write_bytes(raw)
                self._assert_error(code, lambda: self._run(f'budget{index}', iterations=iterations))

    def test_invalid_job_ids_cannot_create_paths(self) -> None:
        for value in ('', '../escape', 'a/b', 'a\\b', '.', 'CON', 'NUL', 'a' * 65):
            with self.subTest(job_id=value):
                self._assert_error('invalid_job_id', lambda: self._run(value))
        self.assertFalse(self.jobs.exists())

    def test_renamed_job_directory_is_rejected_by_persistent_task_identity(self) -> None:
        self._run('original')
        previous = self.jobs / 'original'
        moved = self.jobs / 'renamed'
        self.assertTrue(previous.resolve().is_relative_to(self.root))
        self.assertTrue(moved.resolve().is_relative_to(self.root))
        previous.rename(moved)
        before = self._path('renamed').read_bytes()
        self._assert_error('job_id_mismatch', lambda: self._run('renamed', input_path=None))
        self.assertEqual(self._path('renamed').read_bytes(), before)
        self.assertEqual(self._doc('renamed')['job_id'], 'original')

    def test_source_tree_job_root_is_rejected_before_mkdir(self) -> None:
        with mock.patch.object(Path, 'mkdir', side_effect=AssertionError('source path mkdir forbidden')):
            self._assert_error('job_root_must_be_external', lambda: job.run_job(
                job_root=SOURCE_ROOT / 'forbidden-m3-job-root', job_id='fixture', input_path=self.input))

    def test_linked_internal_file_is_rejected_and_external_target_unchanged(self) -> None:
        self._run()
        target = self.root / 'external-snapshot.json'
        original = self._path(filename='input_snapshot.json').read_bytes()
        target.write_bytes(original)
        linked = self._path(filename='input_snapshot.json')
        linked.unlink()
        try:
            linked.symlink_to(target)
        except OSError as exc:
            self.skipTest(f'platform lacks symlink creation permission: {type(exc).__name__}')
        self._assert_error('linked_job_path', self._run)
        self.assertEqual(target.read_bytes(), original)

    def test_hardlinked_internal_file_is_rejected(self) -> None:
        self._run()
        path = self._path(filename='compute.json')
        other = self.root / 'other-compute.json'
        try:
            os.link(path, other)
        except OSError as exc:
            self.skipTest(f'filesystem lacks hardlink support: {type(exc).__name__}')
        before = other.read_bytes()
        self._assert_error('linked_or_nonregular_job_file', self._run)
        self.assertEqual(other.read_bytes(), before)

    def test_true_process_contention_rejects_second_writer_and_cli_busy(self) -> None:
        child = self._spawn_driver('hold', 'validate')
        self._wait_ready(child)
        self.assertIsNone(child.poll())
        before = self._path('child', 'checkpoint.json').read_bytes()
        with self.assertRaises(job.JobBusy) as caught:
            self._run('child')
        self.assertEqual(caught.exception.code, 'job_busy')
        rc, stdout, stderr = self._cli('--job-root', self.jobs, '--job-id', 'child', '--input', self.input,
            '--iterations', ITERATIONS, '--seed', SEED)
        self.assertEqual(rc, 3, stderr)
        self.assertEqual(stdout, '')
        self.assertEqual(json.loads(stderr)['code'], 'job_busy')
        self.assertEqual(self._path('child', 'checkpoint.json').read_bytes(), before)
        (self.root / 'child_stop').touch()
        self.assertEqual(child.wait(timeout=5), 0)
        self._run('child', input_path=None)

    def test_cli_help_and_concrete_creation_resume_errors(self) -> None:
        rc, stdout, stderr = self._cli('--help')
        self.assertEqual(rc, 0, stderr)
        self.assertIn('--job-root', stdout)
        self.assertFalse(self.jobs.exists())
        rc, stdout, stderr = self._cli('--job-root', self.jobs, '--job-id', 'missing')
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(stderr)['code'], 'input_required_for_new_job')
        self.assertEqual(stdout, '')
        rc, stdout, stderr = self._cli('--job-root', self.jobs, '--job-id', 'bad-param', '--iterations', 0)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(stderr)['code'], 'iterations_out_of_budget')

    def test_cli_creates_then_resumes_without_original_input(self) -> None:
        args = ['--job-root', self.jobs, '--job-id', 'cli', '--iterations', ITERATIONS, '--seed', SEED]
        rc, stdout, stderr = self._cli(*args, '--input', self.input)
        self.assertEqual(rc, 0, stderr)
        expected = json.loads(stdout)
        before = self._path('cli').read_bytes()
        self.input.unlink()
        rc, stdout, stderr = self._cli(*args)
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(json.loads(stdout), expected)
        self.assertEqual(self._path('cli').read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
