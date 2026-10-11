"""Offline M2 lock/process acceptance using only stdlib unittest and temp files.

Every fake writer is created by this file, has a twelve-second self-exit limit,
and uses a per-test stop file. No production processes or paths are touched.
Run directly or with ``python -m unittest tests.test_muse_pipeline_linux_20260929``.
"""
from __future__ import annotations

import errno
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
from scripts import run_data_close_pipeline as pipeline
from scripts import refresh_universe_baseline as universe
from quantdesk import pipeline_process_guard as guard


DRIVER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
mode, root_text, source_text = sys.argv[1:4]
root, source = Path(root_text), Path(source_text)
sys.path.insert(0, str(source))
from scripts import run_data_close_pipeline as p
from scripts import refresh_universe_baseline as u
def mark(name, value=True):
    (root / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
def wait_stop():
    deadline = time.monotonic() + 12
    while not (root / 'STOP').exists() and time.monotonic() < deadline:
        time.sleep(.025)
def spawn(kind, env):
    return subprocess.Popen([sys.executable, '-B', __file__, kind, str(root), str(source)],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if mode == 'lock_byte':
    h = p._OSLockHandle(root / p.LOCK_RELATIVE, offset=int(sys.argv[4]), existing=True)
    h.acquire()
    mark('byte_ready')
    wait_stop()
    h.release()
elif mode == 'leased_child':
    with p.MarketWriterLease(root, run_id='m2-fixture', target='2026-09-29', scope='universe'):
        mark('writer_ready', {'pid': os.getpid()})
        wait_stop()
elif mode == 'owner_exit':
    h, path = p.acquire_pipeline_lock(root, run_id='m2-fixture')
    env = dict(os.environ)
    env.update(p._grant_market_writer(root, run_id='m2-fixture', target='2026-09-29', owner=h, scope='universe'))
    child = spawn('leased_child', env)
    until = time.monotonic() + 6
    while not (root / 'writer_ready.json').exists() and child.poll() is None and time.monotonic() < until:
        time.sleep(.02)
    if not (root / 'writer_ready.json').exists():
        raise RuntimeError('fixture child failed to acquire its real lease')
    mark('owner_done', {'child_pid': child.pid})
    os._exit(0)
elif mode == 'grandchild':
    fd = int(os.environ['M2_INHERITED_FD'])
    expected = (root / p.LOCK_RELATIVE).stat()
    actual = os.fstat(fd)
    assert (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
    mark('grandchild_ready', {'pid': os.getpid(), 'fd': fd})
    wait_stop()
    os.close(fd)
    mark('grandchild_done')
elif mode in {'universe_exit', 'universe_release', 'universe_hang'}:
    with p.MarketWriterLease(root, run_id='m2-fixture', target='2026-09-29', scope='universe'):
        result = u.run_refresh(root, evidence_dir=root / 'evidence')
        mark('universe_result', result)
        if not result.get('accepted'):
            raise RuntimeError('fixture universe updater was rejected')
        if mode == 'universe_exit':
            os._exit(0)
        if mode == 'universe_hang':
            wait_stop()
    mark('lease_released')
    wait_stop()
else:
    raise RuntimeError('unknown bounded fixture mode')
'''


UPDATER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path.cwd()
lock = (root / 'data/.locks/data_close_pipeline.lock.json').stat()
fds = []
for name in os.listdir('/proc/self/fd'):
    try:
        st = os.fstat(int(name))
    except OSError:
        continue
    if (st.st_dev, st.st_ino) == (lock.st_dev, lock.st_ino):
        fds.append(int(name))
(root / 'updater_fds.json').write_text(json.dumps(fds), encoding='utf-8')
assert len(fds) == 1, ('must inherit exactly byte1 lease fd, no byte0 owner fd', fds)
env = dict(os.environ, M2_INHERITED_FD=str(fds[0]))
child = subprocess.Popen([sys.executable, '-B', env['M2_DRIVER'], 'grandchild', str(root), env['M2_SOURCE']],
    pass_fds=tuple(fds), env=env, start_new_session=True,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 6
while not (root / 'grandchild_ready.json').exists() and child.poll() is None and time.monotonic() < deadline:
    time.sleep(.02)
assert (root / 'grandchild_ready.json').exists(), 'bounded inherited writer did not start'
print(json.dumps({'fixture_updater': True, 'grandchild_pid': child.pid}))
'''


class FixedStageArguments(unittest.TestCase):
    def test_standalone_subprocess_kwargs_remain_empty(self) -> None:
        with tempfile.TemporaryDirectory(prefix='m2-standalone-') as directory:
            self.assertEqual(guard.market_writer_subprocess_kwargs(Path(directory)), {})

    def test_research_chain_always_skips_both_minute_paths_and_has_no_symbol_filter(self) -> None:
        with tempfile.TemporaryDirectory(prefix='m2-args-') as directory:
            root = Path(directory)
            for reuse in (None, root / 'reuse.json'):
                with self.subTest(reuse=bool(reuse)), mock.patch.object(
                    pipeline, '_run_owned_subprocess', return_value=('completed', [], {})
                ) as run:
                    pipeline.stage_research_chain(root, network=True, timeout=1, target='2026-09-29',
                        reuse_upstream_manifest=reuse, pipeline_run_id='m2-fixture')
                    command = run.call_args.args[2]
                    self.assertEqual(command[command.index('--skip-analysis-pool-minute') + 1], 'true')
                    self.assertEqual(command[command.index('--include-critical-minute') + 1], 'false')
                    self.assertFalse(any('symbol' in arg or 'filter' in arg for arg in command[3:] if arg.startswith('--')))
                    self.assertEqual('--reuse-upstream-manifest' in command, bool(reuse))

    def test_reconciliation_stays_audit_only_even_when_network_and_push_requested(self) -> None:
        with tempfile.TemporaryDirectory(prefix='m2-args-') as directory, mock.patch.object(
            pipeline, '_run_owned_subprocess', return_value=('completed', [], {})
        ) as run:
            result = pipeline.stage_reconcile(Path(directory), network=True, timeout=1,
                target='2026-09-29', alert_network=True, alert_push=True, pipeline_run_id='m2-fixture')
            command = run.call_args.args[2]
            self.assertIn('--audit-only', command)
            self.assertNotIn('--enable-network', command)
            self.assertFalse(result.network)
            self.assertFalse(result.canonical_write)

    def test_pipeline_residual_writer_blocks_every_later_stage_including_health_alert(self) -> None:
        called = []

        def fixture_stage(root, **kwargs):
            called.append('universe')
            return pipeline.StageResult('universe', 'failed', pipeline._now_iso(),
                reason_codes=['stage_writer_lease_still_alive'], payload={'orphan_alive': True})

        def forbidden_stage(root, **kwargs):
            self.fail('main loop executed a later stage despite the residual writer')

        runners = {name: forbidden_stage for name in pipeline.STAGE_ORDER}
        runners['universe'] = fixture_stage
        with tempfile.TemporaryDirectory(prefix='m2-loop-') as directory, mock.patch.object(
            pipeline.subprocess, 'Popen', side_effect=AssertionError('fixture must never spawn')
        ):
            # network=True selects the execution branch, but every stage is an
            # injected local function and subprocess creation is prohibited.
            report = pipeline.run_pipeline(Path(directory), network=True, run_id='m2-fixture',
                target_date='2026-09-29', stage_runner=runners)
        self.assertEqual(called, ['universe'])
        for name in pipeline.STAGE_ORDER[1:]:
            self.assertEqual(report['stages'][name]['status'], 'blocked')
            self.assertIn('previous_stage_writer_still_alive', report['stages'][name]['reason_codes'])


@unittest.skipUnless(os.name == 'nt', 'Windows msvcrt compatibility only')
class WindowsByteLockCompatibility(unittest.TestCase):
    def test_existing_windows_byte_lock_semantics_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory(prefix='m2-windows-') as directory:
            path = Path(directory) / 'lock'
            owner = pipeline._OSLockHandle(path)
            writer = pipeline._OSLockHandle(path, offset=1)
            contender = pipeline._OSLockHandle(path)
            try:
                owner.acquire()
                writer.acquire()
                with self.assertRaises(pipeline._LockContended):
                    contender.acquire()
                self.assertIsNone(contender._fh)
                writer.release()
                with self.assertRaises(pipeline._LockContended):
                    contender.acquire()
                self.assertEqual(guard.market_writer_subprocess_kwargs(Path(directory)), {})
            finally:
                contender.release()
                writer.release()
                owner.release()


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux OFD and process-group contract')
class LinuxWriterLease(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='m2-linux-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.driver = self.root / 'fixture_driver.py'
        self.driver.write_text(DRIVER, encoding='utf-8')
        (self.root / pipeline.LOCK_RELATIVE).parent.mkdir(parents=True)
        (self.root / pipeline.LOCK_RELATIVE).touch()
        self.processes: list[subprocess.Popen] = []
        self.log_files = []
        self.addCleanup(self._stop_created_processes)

    def _stop_created_processes(self) -> None:
        (self.root / 'STOP').touch()
        for process in self.processes:
            try:
                process.wait(timeout=14)
            except subprocess.TimeoutExpired:
                process.terminate()  # exact Popen object created by this fixture only
                process.wait(timeout=5)
        if (self.root / 'grandchild_ready.json').exists():
            deadline = time.monotonic() + 14
            while not (self.root / 'grandchild_done.json').exists() and time.monotonic() < deadline:
                time.sleep(.025)
        for log in self.log_files:
            log.close()

    def _spawn(self, mode: str, *, env: dict[str, str] | None = None, extra: tuple[str, ...] = ()):
        log = (self.root / (mode + '.log')).open('wb')
        self.log_files.append(log)
        child_env = dict(os.environ)
        child_env.pop(pipeline.MARKET_WRITER_TOKEN_ENV, None)
        child_env.update(env or {})
        child_env.update(M2_DRIVER=str(self.driver), M2_SOURCE=str(SOURCE_ROOT))
        process = subprocess.Popen([sys.executable, '-B', str(self.driver), mode, str(self.root),
            str(SOURCE_ROOT), *extra], cwd=self.root, env=child_env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.processes.append(process)
        return process

    def _wait_json(self, name: str, timeout: float = 6) -> object:
        path = self.root / (name + '.json')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding='utf-8'))
                except json.JSONDecodeError:
                    pass
            time.sleep(.02)
        logs = {p.name: p.read_text(encoding='utf-8', errors='replace') for p in self.root.glob('*.log')}
        self.fail(f'bounded fixture did not publish {name}: {logs}')

    def _assert_contended(self, offset: int) -> None:
        handle = pipeline._OSLockHandle(self.root / pipeline.LOCK_RELATIVE, offset=offset, existing=True)
        try:
            with self.assertRaises(pipeline._LockContended):
                handle.acquire()
        finally:
            handle.release()

    def _assert_free(self, offset: int) -> None:
        handle = pipeline._OSLockHandle(self.root / pipeline.LOCK_RELATIVE, offset=offset, existing=True)
        try:
            handle.acquire()
        finally:
            handle.release()

    def _assert_new_pipeline_busy(self) -> None:
        try:
            handle, path = pipeline.acquire_pipeline_lock(self.root, run_id='another-writer')
        except pipeline.PipelineBusy:
            return
        pipeline.release_pipeline_lock(handle, path, run_id='another-writer')
        self.fail('a new pipeline acquired while the old actual writer was alive')

    def _wait_writer_gone_and_reacquire(self) -> None:
        (self.root / 'STOP').touch()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                handle, path = pipeline.acquire_pipeline_lock(self.root, run_id='after-writer')
            except pipeline.PipelineBusy:
                time.sleep(.025)
                continue
            pipeline.release_pipeline_lock(handle, path, run_id='after-writer')
            return
        self.fail('final bounded writer exited but pipeline lease did not become available')

    def _owner(self):
        handle, path = pipeline.acquire_pipeline_lock(self.root, run_id='m2-fixture')
        self.addCleanup(handle.release)
        return handle, path

    def _grant(self, owner):
        return pipeline._grant_market_writer(self.root, run_id='m2-fixture', target='2026-09-29',
            owner=owner, scope='universe')

    def _universe_inputs(self) -> None:
        (self.root / 'scripts').mkdir()
        (self.root / 'config').mkdir()
        (self.root / 'data/universe').mkdir()
        (self.root / 'scripts/update_a_share_universe.py').write_text(UPDATER, encoding='utf-8')
        (self.root / 'config/weekly_daily_wide_pool_contract.json').write_text(json.dumps(
            {'rules': {'approved_market_universe_sources': ['fixture']}}), encoding='utf-8')
        from datetime import date
        (self.root / 'data/universe/a_share_symbols.csv').write_text(
            f'symbol,source,updated_at\n000001,fixture,{date.today().isoformat()}\n', encoding='utf-8')

    def test_same_byte_contends_and_distinct_byte_is_independent_across_processes(self) -> None:
        child = self._spawn('lock_byte', extra=('0',))
        self._wait_json('byte_ready')
        self.assertIsNone(child.poll())
        self._assert_contended(0)
        self._assert_free(1)

    def test_closing_same_process_probes_does_not_release_owner_or_other_byte(self) -> None:
        first = pipeline._OSLockHandle(self.root / pipeline.LOCK_RELATIVE, offset=0, existing=True)
        second = pipeline._OSLockHandle(self.root / pipeline.LOCK_RELATIVE, offset=1, existing=True)
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        first.acquire()
        second.acquire()
        for _ in range(3):
            self._assert_contended(0)
            self._assert_contended(1)
        second.release()
        self._assert_contended(0)
        self._assert_free(1)

    def test_real_market_lease_owner_probe_preserves_both_byte_locks(self) -> None:
        owner, _ = self._owner()
        with mock.patch.dict(os.environ, self._grant(owner)):
            with pipeline.MarketWriterLease(self.root, run_id='m2-fixture', target='2026-09-29', scope='universe') as lease:
                for _ in range(3):
                    lease.assert_active()
                    self.assertEqual(guard.market_writer_subprocess_kwargs(self.root),
                        {'pass_fds': (lease._handle._fh.fileno(),)})
                    self._assert_contended(0)
                    self._assert_contended(1)
        self._assert_contended(0)
        self._assert_free(1)

    def test_parent_exit_keeps_live_child_lease_and_blocks_new_pipeline(self) -> None:
        owner_process = self._spawn('owner_exit')
        self._wait_json('owner_done')
        self.assertEqual(owner_process.wait(timeout=3), 0)
        self._assert_free(0)
        self._assert_contended(1)
        self._assert_new_pipeline_busy()
        self._wait_writer_gone_and_reacquire()

    def _universe_inheritance(self, mode: str) -> None:
        self._universe_inputs()
        owner, path = self._owner()
        wrapper = self._spawn(mode, env=self._grant(owner))
        self.assertTrue(self._wait_json('universe_result')['accepted'])
        self.assertEqual(len(self._wait_json('updater_fds')), 1)
        self._wait_json('grandchild_ready')
        if mode == 'universe_exit':
            self.assertEqual(wrapper.wait(timeout=3), 0)
        else:
            self._wait_json('lease_released')
            self.assertIsNone(wrapper.poll())
        self._assert_contended(1)
        pipeline.release_pipeline_lock(owner, path, run_id='m2-fixture')
        self._assert_free(0)  # the updater/grandchild did not inherit byte0
        self._assert_new_pipeline_busy()
        self._wait_writer_gone_and_reacquire()

    def test_real_universe_pass_fds_keeps_grandchild_lock_after_wrapper_exit(self) -> None:
        self._universe_inheritance('universe_exit')

    def test_explicit_lease_release_does_not_unlock_live_universe_grandchild(self) -> None:
        self._universe_inheritance('universe_release')

    def test_missing_ofd_support_fails_closed_and_closes_half_open_handle(self) -> None:
        handle = pipeline._OSLockHandle(self.root / pipeline.LOCK_RELATIVE, existing=True)
        with mock.patch.dict(sys.modules, {'fcntl': object()}):
            with self.assertRaisesRegex(OSError, 'ofd_unavailable'):
                handle.acquire()
        self.assertIsNone(handle._fh)

    def test_kernel_ofd_rejection_fails_closed_and_closes_half_open_handle(self) -> None:
        import fcntl
        handle = pipeline._OSLockHandle(self.root / pipeline.LOCK_RELATIVE, existing=True)
        with mock.patch.object(fcntl, 'fcntl', side_effect=OSError(errno.EINVAL, 'kernel rejects OFD')):
            with self.assertRaises(OSError):
                handle.acquire()
        self.assertIsNone(handle._fh)

    def test_invalid_closed_wrong_inode_and_unlocked_descriptors_are_rejected(self) -> None:
        lock_path = self.root / pipeline.LOCK_RELATIVE
        with (self.root / 'other_inode').open('w+b') as other, lock_path.open('r+b') as unlocked:
            closed = os.open(lock_path, os.O_RDWR)
            os.close(closed)
            for label, fd in [('negative', -1), ('closed', closed), ('wrong_inode', other.fileno()),
                              ('unlocked_same_inode', unlocked.fileno())]:
                token = None
                try:
                    with self.subTest(case=label), self.assertRaises(OSError):
                        token = guard.activate_market_writer_lease(fd, lock_path, lambda: None)
                finally:
                    if token is not None:
                        guard.reset_market_writer_lease(token)

    def test_byte0_descriptor_is_never_propagated_as_writer(self) -> None:
        owner, _ = self._owner()
        with mock.patch.dict(os.environ, self._grant(owner)):
            with pipeline.MarketWriterLease(self.root, run_id='m2-fixture', target='2026-09-29', scope='universe'):
                token = None
                try:
                    with self.assertRaises(OSError):
                        token = guard.activate_market_writer_lease(owner._fh.fileno(), owner._path, lambda: None)
                finally:
                    if token is not None:
                        guard.reset_market_writer_lease(token)

    def test_wrong_root_and_revoked_capability_block_subprocess_fd_propagation(self) -> None:
        owner, _ = self._owner()
        with mock.patch.dict(os.environ, self._grant(owner)):
            with pipeline.MarketWriterLease(self.root, run_id='m2-fixture', target='2026-09-29', scope='universe'):
                with self.assertRaisesRegex(OSError, 'root_mismatch'):
                    guard.market_writer_subprocess_kwargs(self.root / 'wrong_root')
                pipeline._revoke_market_writer(self.root, 'm2-fixture')
                with self.assertRaises(pipeline.MarketWriterUnauthorized):
                    guard.market_writer_subprocess_kwargs(self.root)

    def test_lease_check_failure_is_not_reported_as_writer_gone(self) -> None:
        missing = self.root / 'missing-root'
        reasons, evidence = pipeline._writer_lease_evidence(missing)
        self.assertIn('stage_writer_lease_unverifiable', reasons)
        self.assertIsNone(evidence['writer_lease_alive'])
        self.assertTrue(evidence['writer_lease_check_failed'])
        self.assertTrue(evidence['orphan_alive'])

    def _run_universe_stage(self, mode: str, env: dict[str, str], *, timeout: int, on_timeout=None):
        real_popen = subprocess.Popen

        def track_created(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        env = {**env, 'M2_DRIVER': str(self.driver), 'M2_SOURCE': str(SOURCE_ROOT)}
        command = [sys.executable, '-B', str(self.driver), mode, str(self.root), str(SOURCE_ROOT)]
        with mock.patch.object(pipeline.subprocess, 'Popen', side_effect=track_created):
            return pipeline._run_stage_subprocess(self.root, 'universe', command,
                timeout_seconds=timeout, env_overrides=env, on_timeout=on_timeout)

    def test_successful_direct_child_exit_with_live_grandchild_is_failed(self) -> None:
        self._universe_inputs()
        owner, path = self._owner()
        status, reasons, evidence = self._run_universe_stage('universe_exit', self._grant(owner), timeout=4)
        self.assertTrue(self._wait_json('universe_result')['accepted'])
        self.assertEqual(evidence['exit_code'], 0)
        self.assertEqual(status, 'failed')
        self.assertIn('stage_writer_lease_still_alive', reasons)
        self.assertTrue(evidence['writer_lease_alive'])
        self.assertTrue(evidence['orphan_alive'])
        pipeline.release_pipeline_lock(owner, path, run_id='m2-fixture')
        self._assert_new_pipeline_busy()
        self._wait_writer_gone_and_reacquire()

    def test_timeout_revokes_before_group_kill_and_reports_escaped_writer_lease(self) -> None:
        self._universe_inputs()
        owner, path = self._owner()
        revoked = []
        killed_groups = []
        real_killpg = os.killpg

        def revoke():
            pipeline._revoke_market_writer(self.root, 'm2-fixture')
            revoked.append(True)

        def kill_created_group(group, sig):
            self.assertEqual(revoked, [True], 'revoke must precede group termination')
            self.assertIn(group, [process.pid for process in self.processes])
            killed_groups.append(group)
            return real_killpg(group, sig)

        with mock.patch.object(pipeline.os, 'killpg', side_effect=kill_created_group):
            status, reasons, evidence = self._run_universe_stage('universe_hang', self._grant(owner),
                timeout=2, on_timeout=revoke)
        self.assertTrue(self._wait_json('universe_result')['accepted'])
        self.assertEqual(status, 'failed')
        self.assertTrue(evidence['timeout'])
        self.assertEqual(killed_groups, [evidence['child_pid']])
        self.assertIn('stage_writer_lease_still_alive', reasons)
        self.assertTrue(evidence['writer_lease_alive'])
        self.assertFalse(evidence['writer_lease_check_failed'])
        self.assertTrue(evidence['orphan_alive'])
        self._assert_contended(1)
        pipeline.release_pipeline_lock(owner, path, run_id='m2-fixture')
        self._assert_new_pipeline_busy()
        self._wait_writer_gone_and_reacquire()


if __name__ == '__main__':
    unittest.main(verbosity=2)
