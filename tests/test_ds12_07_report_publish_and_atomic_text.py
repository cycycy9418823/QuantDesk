"""DS12-07 Section-C4/C6: report publish / multi-artifact and atomic text save.

Offline only: a throwaway ``tmp_path`` project root; no real providers, no real
project writes.

- C6: the report/note text writer must be atomic, so an interrupted save cannot
  truncate the previous file.
- C4: the data-close pipeline publish must keep ``last_successful`` in its own
  pointer (a later failure must not erase the last success), keep the archive
  manifest immutable, and reject an invalid attempt/report schema before writing.
"""

from __future__ import annotations

import json

import pytest

import quantdesk.shared as shared


# --- C6: atomic text save -------------------------------------------------


def test_atomic_text_replace_failure_preserves_old_bytes(tmp_path, monkeypatch) -> None:
    target = tmp_path / "report.md"
    target.write_text("OLD-CONTENT\n", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shared.os, "replace", _boom)

    with pytest.raises(OSError):
        shared.write_text(target, "NEW-CONTENT")

    assert target.read_text(encoding="utf-8") == "OLD-CONTENT\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_text_success_writes_content_without_leftover(tmp_path) -> None:
    target = tmp_path / "report.md"

    returned = shared.write_text(target, "hello  world  ")

    assert returned == target
    assert target.read_text(encoding="utf-8") == "hello  world\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_text_partial_write_failure_preserves_old_bytes(tmp_path, monkeypatch) -> None:
    target = tmp_path / "report.md"
    target.write_text("OLD\n", encoding="utf-8")

    real_fdopen = shared.os.fdopen

    class _PartialHandle:
        def __init__(self, handle):
            self._handle = handle

        def write(self, _content):
            raise OSError("interrupted mid-write")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

    monkeypatch.setattr(
        shared.os, "fdopen", lambda fd, *a, **k: _PartialHandle(real_fdopen(fd, *a, **k))
    )

    with pytest.raises(OSError):
        shared.write_text(target, "NEW")

    assert target.read_text(encoding="utf-8") == "OLD\n"


# --- C4: multi-artifact publish / success pointer -------------------------


def _report(run_id: str, attempt_id: str, status: str, *, reuse_only: bool = False) -> dict:
    return {
        "run_id": run_id,
        "invocation_run_id": run_id,
        "attempt_id": attempt_id,
        "status": status,
        "stages": {},
        "reused_stages": ["market_refresh"] if reuse_only else [],
        "executed_stages": [] if reuse_only else ["universe"],
    }


def _manifest(root, run_id: str) -> dict:
    path = root / "results" / "data_close_pipeline" / "runs" / run_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_last_successful_survives_a_later_failure(tmp_path) -> None:
    from scripts.run_data_close_pipeline import _publish

    _publish(tmp_path, _report("run_ok", "a" * 24, "completed_all_executed"))
    _publish(tmp_path, _report("run_bad", "b" * 24, "failed"))

    latest = json.loads(
        (tmp_path / "results/data_close_pipeline/data_close_pipeline_latest.json").read_text(encoding="utf-8")
    )
    assert latest["run_id"] == "run_bad"
    assert latest["latest_attempt_is_successful"] is False
    assert latest["last_successful_run_id"] == "run_ok"
    # The archive manifest is an immutable receipt, not a latest pointer.
    assert _manifest(tmp_path, "run_ok")["run_id"] == "run_ok"
    assert _manifest(tmp_path, "run_bad")["run_id"] == "run_bad"


def test_success_pointer_write_failure_does_not_advance_latest(tmp_path, monkeypatch) -> None:
    import scripts.run_data_close_pipeline as pipeline

    _publish = pipeline._publish
    _publish(tmp_path, _report("run_ok", "a" * 24, "completed_all_executed"))
    latest_path = tmp_path / "results/data_close_pipeline/data_close_pipeline_latest.json"
    before = latest_path.read_bytes()

    real_replace = pipeline.os.replace
    success_pointer = tmp_path / "results/data_close_pipeline/data_close_pipeline_last_successful.json"

    def _failing_replace(src, dst):
        if str(dst) == str(success_pointer):
            raise OSError("success pointer write failed")
        return real_replace(src, dst)

    monkeypatch.setattr(pipeline.os, "replace", _failing_replace)

    with pytest.raises(OSError):
        _publish(tmp_path, _report("run_second", "c" * 24, "completed_all_executed"))

    # A later failure must not leave `latest` advanced to a success-looking run.
    assert latest_path.read_bytes() == before


def test_invalid_attempt_schema_is_rejected_before_write(tmp_path) -> None:
    from scripts.run_data_close_pipeline import _publish

    with pytest.raises(ValueError):
        _publish(tmp_path, _report("run_x", "not-24-hex", "completed_all_executed"))

    latest_path = tmp_path / "results/data_close_pipeline/data_close_pipeline_latest.json"
    assert not latest_path.exists()


# --- C4: research-report publish receipt + old-success preservation -------


def _seed_refresh_inputs(tmp_path) -> Path:
    for rel in (
        "results/daily_research/v420_daily_stock_candidates_latest.csv",
        "results/daily_research/v420_daily_sector_candidates_latest.csv",
        "results/strategy_lab/v420_strategy_leaderboard_latest.csv",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("symbol\n", encoding="utf-8")
    page = tmp_path / "reports/daily_research/v421_daily_result_page_latest.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("OLD SUCCESSFUL PAGE\n", encoding="utf-8")
    return page


def test_research_report_publish_receipt_and_old_success_preserved(tmp_path, monkeypatch) -> None:
    import quantdesk.research_outputs_refresh as refresh

    page = _seed_refresh_inputs(tmp_path)

    def ok_workflow(root, **kwargs):
        page.write_text("CURRENT SUCCESSFUL PAGE\n", encoding="utf-8")
        return {"handoff": {"status": "completed"}}

    monkeypatch.setattr(refresh, "run_daily_open_workflow", ok_workflow)
    monkeypatch.setattr(refresh, "write_dashboard_cards", lambda root: {"status": "ok"})

    first = refresh.refresh_research_outputs(tmp_path)
    assert first["result_page_updated"] is True
    success_receipt = tmp_path / "results/data_update/v424_research_outputs_publish_receipt_latest.json"
    assert success_receipt.is_file()
    receipt_payload = json.loads(success_receipt.read_text(encoding="utf-8"))
    assert receipt_payload["schema_version"] == refresh.PUBLISH_SCHEMA_VERSION
    assert receipt_payload["status"] == "published"
    before = success_receipt.read_bytes()

    def failing_workflow(root, **kwargs):
        raise OSError("deterministic workflow failure before publication")

    monkeypatch.setattr(refresh, "run_daily_open_workflow", failing_workflow)
    second = refresh.refresh_research_outputs(tmp_path)

    assert second["refresh_success"] is False
    assert second["result_page_updated"] is False
    assert page.read_text(encoding="utf-8").strip() == "CURRENT SUCCESSFUL PAGE"
    # The last successful receipt is preserved; the failure gets its own receipt.
    assert success_receipt.read_bytes() == before
    assert second["publish_failure_receipt_path"]
    failure_path = tmp_path / second["publish_failure_receipt_path"]
    assert failure_path.is_file()
    assert json.loads(failure_path.read_text(encoding="utf-8"))["status"] == "failed"

