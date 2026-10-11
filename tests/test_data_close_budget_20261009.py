"""Core close data survives optional fetch deadlines; no real providers or books."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import run_daily_data_refresh as refresh
from scripts import run_data_close_pipeline as pipeline
from quantdesk.process_deadline import ProcessDeadlineResult


TARGET = "2026-10-09"
RUN = "close-budget-fixture"


class FakeLease:
    run_id = RUN
    target = TARGET

    def assert_active(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _main_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "RESULTS_DIR", tmp_path / "results/data_refresh")
    monkeypatch.setattr(refresh, "MarketWriterLease", lambda *a, **k: FakeLease())
    monkeypatch.setattr(refresh, "_complete_live_session", lambda target: True)
    monkeypatch.setattr(refresh, "family_bars_and_valuation", lambda *a, **k: {"ok": True, "bars": {"appended": 2}})
    for name in ("family_benchmarks", "family_news", "family_quarterly"):
        monkeypatch.setattr(refresh, name, lambda *a, **k: {"ok": True})


def _argv(*extra):
    return ["--enable-network", "--target-date", TARGET, "--pipeline-run-id", RUN, *extra]


def test_core_survives_exhausted_optional_budget(tmp_path, monkeypatch, capsys):
    _main_fixture(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(refresh, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def core(*args, **kwargs):
        clock[0] += 2
        return {"ok": True, "bars": {"appended": 2}}

    def forbidden(*args, **kwargs):
        pytest.fail("optional provider started after the shared deadline")

    monkeypatch.setattr(refresh, "family_bars_and_valuation", core)
    for name in ("family_benchmarks", "family_news", "family_quarterly"):
        monkeypatch.setattr(refresh, name, forbidden)
    rc = refresh.main(_argv("--max-refresh-seconds", "1"))
    report = json.loads(capsys.readouterr().out)
    assert rc == 0 and report["status"] == "completed_with_warnings"
    assert report["overall_ok"] is False
    assert report["families"]["bars_and_valuation"]["bars"]["appended"] == 2
    for name in ("benchmarks", "news", "quarterly"):
        family = report["families"][name]
        assert family["status"] == "skipped"
        assert family["started_at"] is None and family["finished_at"] is None
        assert family["attempted"] is False and family["ok"] is False
    stored = json.loads((refresh.RESULTS_DIR / "data_refresh_latest.json").read_text())
    assert stored["status"] == report["status"]
    assert stored["pipeline_run_id"] == RUN


def test_interrupted_optional_family_leaves_core_progress_not_success(tmp_path, monkeypatch):
    _main_fixture(tmp_path, monkeypatch)
    refresh.RESULTS_DIR.mkdir(parents=True)
    latest = refresh.RESULTS_DIR / "data_refresh_latest.json"
    latest.write_bytes(b"previous-success")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("fixture hard interruption")

    monkeypatch.setattr(refresh, "family_news", interrupted)
    with pytest.raises(KeyboardInterrupt):
        refresh.main(_argv())
    progress_files = list(refresh.RESULTS_DIR.rglob("progress.json"))
    assert len(progress_files) == 1
    progress = json.loads(progress_files[0].read_text())
    assert progress["pipeline_run_id"] == RUN and progress["target_date"] == TARGET
    assert progress["status"] == "running" and progress["finished_at"] is None
    assert progress["current_family"] == "news"
    assert progress["families"]["bars_and_valuation"]["ok"] is True
    assert progress["families"]["news"]["status"] == "running"
    assert progress["families"]["news"]["finished_at"] is None
    assert latest.read_bytes() == b"previous-success"


def test_parent_passes_inner_budget_but_progress_cannot_promote_timeout(tmp_path, monkeypatch):
    captured = {}

    def child(root, stage, command, **kwargs):
        captured.update(command=command, timeout=kwargs["timeout"])
        attempt = command[command.index("--refresh-attempt-id") + 1]
        path = pipeline._market_refresh_progress_path(root / "results/data_refresh", RUN)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "schema_version": "daily_refresh_progress_v1", "pipeline_run_id": RUN,
            "refresh_attempt_id": attempt, "target_date": TARGET, "status": "running",
            "current_family": "news", "finished_at": None,
            "families": {"bars_and_valuation": {"ok": True, "status": "completed"},
                         "benchmarks": {"status": "partial", "target_date": TARGET,
                                        "target_covered": False, "freshness_status": "behind_target"}},
        }), encoding="utf-8")
        return "failed", ["stage_timeout"], {"timeout": True}

    monkeypatch.setattr(pipeline, "_run_owned_subprocess", child)
    result = pipeline.stage_market_refresh(tmp_path, network=True, timeout=900, target=TARGET, pipeline_run_id=RUN)
    command = captured["command"]
    assert captured["timeout"] == 900
    assert float(command[command.index("--max-refresh-seconds") + 1]) == 870
    assert result.status == "failed"
    assert result.payload["progress_evidence"]["families"]["bars_and_valuation"]["ok"] is True
    assert result.payload["benchmark_evidence"]["freshness_status"] == "behind_target"
    assert "stdout_json" not in result.payload and "exit_code" not in result.payload


def test_timeout_preserves_safe_output_without_success_payload(tmp_path, monkeypatch):
    class Child:
        pid = 24680
        returncode = -9
        calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fixture", timeout, output=b"core finished\n")
            return "core finished\n", "token=private-fixture-value\nnews stalled"

        def kill(self):
            pass

        def poll(self):
            return self.returncode

    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(pipeline, "_writer_lease_evidence", lambda root: ([], {"orphan_alive": False}))
    if hasattr(pipeline.os, "killpg"):
        monkeypatch.setattr(pipeline.os, "killpg", lambda *a: None)
    status, reasons, payload = pipeline._run_stage_subprocess(tmp_path, "market_refresh", ["fixture"], timeout_seconds=1)
    assert status == "failed" and payload["timeout"] is True
    assert "core finished" in "\n".join(payload["stdout_tail"])
    assert "news stalled" in "\n".join(payload["stderr_tail"])
    assert "private-fixture-value" not in json.dumps(payload)
    assert "stdout_json" not in payload


@pytest.mark.parametrize("bad_date", ["2026-02-30", "2026-9-01", "20260901", "2026-W36-2", "2026-09-01 "])
@pytest.mark.parametrize("location", ["incoming", "history"])
def test_strict_date_rejections_preserve_original_bytes(tmp_path, bad_date, location):
    path = tmp_path / "600000.csv"
    old_date = bad_date if location == "history" else "2026-09-01"
    original = (refresh.DAILY_HEADER + f"\n{old_date},600000,10,10,10,10,100,1000,0,0,raw,fixture,t\n").encode()
    path.write_bytes(original)
    incoming_date = bad_date if location == "incoming" else TARGET
    ok, reason = refresh._append_daily_row(
        path, f"{incoming_date},600000,10,10,10,10,100,1000,0,0,raw,fixture,t",
        header=refresh.DAILY_HEADER, lease=FakeLease(),
    )
    assert not ok
    assert reason.startswith("daily_target_invalid:" if location == "incoming" else "daily_history_date_invalid:")
    assert path.read_bytes() == original


def test_core_failure_stays_failed_even_when_optional_work_is_skipped(tmp_path, monkeypatch, capsys):
    _main_fixture(tmp_path, monkeypatch)
    rc = refresh.main(_argv("--max-refresh-seconds", "0"))
    report = json.loads(capsys.readouterr().out)
    assert rc == 1 and report["status"] == "failed"
    assert report["families"]["bars_and_valuation"]["attempted"] is False
    assert report["overall_ok"] is False


@pytest.fixture
def bounded(monkeypatch, tmp_path):
    token = refresh._OPTIONAL_DEADLINE.set(time.monotonic() + 30)
    monkeypatch.setattr(refresh, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(refresh, "META_DIR", tmp_path / "data/metadata")
    pool = tmp_path / "news_pool.txt"
    pool.write_text("600000\n600001\n600002\n", encoding="utf-8")
    monkeypatch.setattr(refresh, "NEWS_POOL_FILE", pool)
    try:
        yield
    finally:
        refresh._OPTIONAL_DEADLINE.reset(token)


def test_news_partial_batch_publishes_only_completed_rows(tmp_path, monkeypatch, bounded):
    batch = ProcessDeadlineResult(
        {0: [{"title": "fixture", "content": "", "published": TARGET, "source": "fixture", "url": ""}]},
        {1: "TimeoutError:token=private-fixture"}, [2], True, [123],
    )
    monkeypatch.setattr(refresh, "_run_optional_fetches", lambda tasks: batch)
    result = refresh.family_news(True, lease=FakeLease())
    written = list((tmp_path / "data/news").glob("*.csv"))
    assert len(written) == 1 and written[0].stem.endswith("600000")
    assert result["status"] == "partial" and result["ok"] is False
    assert result["refreshed"] == 1 and result["pool_size"] == 3
    assert result["failures"] == ["600001"] and result["unfinished"] == ["600002"]
    assert result["not_attempted"] == [] and result["unfinished_attempt_state"] == "unknown"
    assert "private-fixture" not in json.dumps(result)


def test_quarterly_partial_keeps_old_disclosure_unclaimed(tmp_path, monkeypatch, bounded):
    year = refresh.datetime.now(refresh.SHANGHAI).year
    refresh.META_DIR.mkdir(parents=True)
    old = refresh.META_DIR / f"disclosure_schedule_{year}H1.csv"
    old.write_bytes(b"old-cache")
    content = b"symbol,value\n600000,1\n"
    batch = ProcessDeadlineResult(
        {0: {"rows": 1, "content": content, "filename": f"earnings_report_{year}Q2.csv"}}, {}, [1], True, [123],
    )
    monkeypatch.setattr(refresh, "_run_optional_fetches", lambda tasks: batch)
    result = refresh.family_quarterly(True, True, lease=FakeLease())
    assert result["ok"] is False and result["earnings"]["rows"] == 1
    assert result["unfinished"] == ["disclosure"] and "disclosure" not in result
    assert (refresh.META_DIR / f"earnings_report_{year}Q2.csv").read_bytes() == content
    assert old.read_bytes() == b"old-cache"


@pytest.mark.parametrize("family", ["benchmarks", "news", "quarterly"])
def test_completed_fetch_cannot_publish_after_lease_revocation(tmp_path, monkeypatch, bounded, family):
    class RevocableLease(FakeLease):
        active = True

        def assert_active(self):
            if not self.active:
                raise pipeline.MarketWriterUnauthorized("fixture_revoked")

    lease = RevocableLease()

    def fetched(tasks):
        lease.active = False
        return ProcessDeadlineResult({0: {}}, {}, [], False, [123])

    monkeypatch.setattr(refresh, "_run_optional_fetches", fetched)
    with pytest.raises(pipeline.MarketWriterUnauthorized):
        if family == "benchmarks":
            refresh.family_benchmarks(True, lease=lease, target_date=TARGET)
        elif family == "news":
            refresh.family_news(True, lease=lease)
        else:
            refresh.family_quarterly(True, True, lease=lease)
    assert not list((tmp_path / "data").rglob("*.csv"))


def test_benchmark_timeout_preserves_cache_and_does_not_refetch(tmp_path, monkeypatch, bounded):
    from quantdesk.data_truth import benchmark_incremental_update as benchmark

    target = tmp_path / "data/benchmark" / benchmark.BENCHMARK_SPECS[0].filename
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original benchmark bytes")
    monkeypatch.setattr(refresh, "_run_optional_fetches", lambda tasks: ProcessDeadlineResult({}, {}, [0], True, [123]))
    monkeypatch.setattr(benchmark, "default_index_fetcher", lambda *a, **k: pytest.fail("unbounded fallback fetch"))
    result = refresh.family_benchmarks(True, lease=FakeLease(), target_date=TARGET)
    assert result["ok"] is False and result["target_covered"] is None
    assert result["freshness_status"] == "assessment_incomplete"
    assert target.read_bytes() == b"original benchmark bytes"


@pytest.mark.parametrize("field,value", [
    ("pipeline_run_id", "previous-run"), ("target_date", "2026-10-08"),
    ("refresh_attempt_id", "previous-attempt"), ("schema_version", "old"),
    ("families", []), ("families", {"news": "malformed"}),
])
def test_progress_from_another_attempt_or_invalid_shape_is_rejected(tmp_path, field, value):
    doc = {"schema_version": "daily_refresh_progress_v1", "pipeline_run_id": RUN,
           "target_date": TARGET, "refresh_attempt_id": "current", "families": {}}
    doc[field] = value
    path = pipeline._market_refresh_progress_path(tmp_path / "results/data_refresh", RUN)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert pipeline._read_market_refresh_progress(tmp_path, run_id=RUN, target=TARGET, attempt_id="current") is None


def _isolated_fetch_fixture(task, context):
    """Module-level spawn fixture; all outputs are beneath pytest tmp_path."""
    if task.get("crash"):
        os._exit(7)
    if task.get("error"):
        raise ValueError("fixture failure")
    if task.get("slow"):
        Path(task["started"]).write_text("started", encoding="utf-8")
        time.sleep(30)
        Path(task["late"]).write_text("forbidden late publication", encoding="utf-8")
    return b"x" * int(task.get("bytes", 0))


def test_real_spawn_deadline_retains_large_result_and_kills_running_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "_optional_fetch_worker", _isolated_fetch_fixture)
    started, late = tmp_path / "started.txt", tmp_path / "late.txt"
    token = refresh._OPTIONAL_DEADLINE.set(time.monotonic() + 8)
    try:
        batch = refresh._run_optional_fetches([
            {"bytes": 5 * 1024 * 1024}, {"slow": True, "started": str(started), "late": str(late)},
        ])
    finally:
        refresh._OPTIONAL_DEADLINE.reset(token)
    assert batch.results[0] == b"x" * (5 * 1024 * 1024)
    assert started.is_file(), "the test must interrupt a running fetch, not just spawn startup"
    assert batch.timed_out is True and batch.unfinished_indices == [1]
    assert not late.exists()
    assert not ({process.pid for process in multiprocessing.active_children()} & set(batch.worker_pids))


@pytest.mark.parametrize("task", [{"error": True}, {"crash": True}])
def test_worker_exception_and_exit_never_count_as_success(monkeypatch, task):
    monkeypatch.setattr(refresh, "_optional_fetch_worker", _isolated_fetch_fixture)
    token = refresh._OPTIONAL_DEADLINE.set(time.monotonic() + 8)
    try:
        batch = refresh._run_optional_fetches([task])
    finally:
        refresh._OPTIONAL_DEADLINE.reset(token)
    assert batch.results == {} and batch.timed_out is False
    if task.get("error"):
        assert batch.failures[0].startswith("ValueError:")
    else:
        assert batch.unfinished_indices == [0]


def test_zero_remaining_budget_starts_no_worker(monkeypatch):
    monkeypatch.setattr(refresh, "_optional_fetch_worker", _isolated_fetch_fixture)
    token = refresh._OPTIONAL_DEADLINE.set(time.monotonic() - 1)
    try:
        batch = refresh._run_optional_fetches([{}])
    finally:
        refresh._OPTIONAL_DEADLINE.reset(token)
    assert batch.worker_pids == [] and batch.results == {} and batch.unfinished_indices == [0]
    evidence = refresh._optional_fetch_evidence(batch, ["news"])
    assert evidence["not_attempted"] == ["news"]


@pytest.mark.parametrize("family", ["benchmarks", "news", "quarterly"])
def test_budget_expires_during_preparation_is_skipped(tmp_path, monkeypatch, bounded, family):
    monkeypatch.setattr(refresh, "_run_optional_fetches", lambda tasks: ProcessDeadlineResult({}, {}, list(range(len(tasks))), True, []))
    if family == "benchmarks":
        result = refresh.family_benchmarks(True, lease=FakeLease(), target_date=TARGET)
    elif family == "news":
        result = refresh.family_news(True, lease=FakeLease())
    else:
        result = refresh.family_quarterly(True, True, lease=FakeLease())
    assert result["status"] == "skipped" and result["ok"] is False
    assert result["not_attempted"] and not list((tmp_path / "data").rglob("*.csv"))


def test_benchmark_worker_error_keeps_explicit_type(tmp_path, monkeypatch, bounded):
    monkeypatch.setattr(refresh, "_run_optional_fetches", lambda tasks: ProcessDeadlineResult({}, {0: "TypeError:private-input"}, [], False, [123]))
    result = refresh.family_benchmarks(True, lease=FakeLease(), target_date=TARGET)
    assert result["status"] == "error" and result["error_type"] == "TypeError"
    assert "private-input" not in json.dumps(result)


def test_spawn_from_runpy_main_matches_owned_stage_entry(tmp_path):
    inner = tmp_path / "inner.py"
    inner.write_text(
        "import json, time\n"
        "from scripts import run_daily_data_refresh as refresh\n"
        "def worker(task, context): return {'rows': task['rows']}\n"
        "refresh._optional_fetch_worker = worker\n"
        "if __name__ == '__main__':\n"
        "    token = refresh._OPTIONAL_DEADLINE.set(time.monotonic() + 15)\n"
        "    try:\n"
        "        result = refresh._run_optional_fetches([{'rows': 7}])\n"
        "        assert result.results == {0: {'rows': 7}}\n"
        "        assert not result.unfinished_indices\n"
        "        print(json.dumps({'spawn_from_runpy': True}))\n"
        "    finally: refresh._OPTIONAL_DEADLINE.reset(token)\n",
        encoding="utf-8",
    )
    outer = tmp_path / "outer.py"
    outer.write_text("import runpy, sys\nrunpy.run_path(sys.argv[1], run_name='__main__')\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", str(outer), str(inner)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"spawn_from_runpy": True}
