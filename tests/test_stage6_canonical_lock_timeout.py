"""Section 16 singleton canonical minute lock: wait is sized for real contention.

The 2026-08-13 15:45 natural chain failed at batch 895/914 with
``interprocess_file_lock_timeout`` on
``data/intraday/1m/.locks/stage6_append_only_batch.lock``. The lock was never
stuck - it is an advisory ``msvcrt.locking`` lock and was free on inspection.
With one singleton channel and ``batch_workers=4``, a batch's legitimate wait is
roughly ``(workers - 1)`` holds, and each hold covers audit + preflight +
commit + verify including three SHA256 passes over multi-megabyte canonical
files. The 60s library default assumed an uncontended lock.

These tests pin the repair shape: every canonical minute writer passes the
sized timeout, and the lock scope stays wide (narrowing it is forbidden by the
section 16 ruling quoted in ``apply_audited_intraday_batch``).
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.stage6_append_only_import import (
    CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS,
    canonical_intraday_write_lock_path,
)


ROOT = Path(__file__).resolve().parents[1]

# Every module that mutates canonical minute data through the section 16
# singleton channel. All of them must wait the same amount.
CANONICAL_WRITER_MODULES = (
    "quantdesk/intraday/stage6_append_only_import.py",
    "quantdesk/intraday/stage6_conflict_resolution.py",
    "quantdesk/intraday/stage6_duplicate_session_repair.py",
    "quantdesk/intraday/stage6_historical_raw_import.py",
)

LIBRARY_DEFAULT_TIMEOUT_SECONDS = 60.0
OBSERVED_BATCH_WORKERS = 4


def _canonical_lock_calls(source: str) -> list[ast.Call]:
    """Return every ``interprocess_file_lock`` call on the canonical minute lock."""

    calls: list[ast.Call] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name != "interprocess_file_lock":
            continue
        first = node.args[0] if node.args else None
        if not isinstance(first, ast.Call):
            continue
        inner = first.func
        inner_name = inner.id if isinstance(inner, ast.Name) else getattr(inner, "attr", "")
        if inner_name == "canonical_intraday_write_lock_path":
            calls.append(node)
    return calls


@pytest.mark.parametrize("relative_path", CANONICAL_WRITER_MODULES)
def test_every_canonical_minute_writer_passes_the_sized_lock_timeout(
    relative_path: str,
) -> None:
    calls = _canonical_lock_calls((ROOT / relative_path).read_text(encoding="utf-8"))
    assert calls, f"{relative_path} no longer takes the canonical minute lock"

    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "timeout_seconds" in keywords, (
            f"{relative_path} relies on the {LIBRARY_DEFAULT_TIMEOUT_SECONDS}s library "
            "default, which assumes an uncontended lock"
        )
        value = keywords["timeout_seconds"]
        assert isinstance(value, ast.Name), (
            f"{relative_path} hard-codes a timeout instead of sharing the "
            "CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS constant"
        )
        assert value.id == "CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS"


def test_sized_timeout_covers_observed_worker_contention() -> None:
    # The failing run showed hold times growing into the seconds while four
    # workers queued. The sized wait must clear the library default by enough
    # margin to absorb that, without becoming an unbounded hang.
    assert CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS > LIBRARY_DEFAULT_TIMEOUT_SECONDS
    assert (
        CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS
        >= LIBRARY_DEFAULT_TIMEOUT_SECONDS * OBSERVED_BATCH_WORKERS
    )
    # Still finite: a genuinely stuck holder must surface, bounded by the
    # scheduled task's own ExecutionTimeLimit rather than waiting forever.
    assert CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS < 3600.0


def test_apply_forwards_the_sized_timeout_to_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as module

    captured: dict[str, object] = {}

    class _Recorder:
        def __init__(self, path: Path, **kwargs: object) -> None:
            captured["path"] = path
            captured.update(kwargs)

        def __enter__(self) -> Path:
            return Path(captured["path"])  # type: ignore[arg-type]

        def __exit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(module, "interprocess_file_lock", _Recorder)

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    staging.mkdir()

    module.apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-08-13",
        run_id="lock-timeout-guard",
        output_path=tmp_path / "out.json",
        apply=True,
    )

    assert captured["path"] == canonical_intraday_write_lock_path(canonical)
    assert captured["timeout_seconds"] == CANONICAL_WRITE_LOCK_TIMEOUT_SECONDS


def test_dry_run_does_not_take_the_singleton_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import stage6_append_only_import as module

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("apply=False must not serialize on the write channel")

    monkeypatch.setattr(module, "interprocess_file_lock", _forbidden)

    canonical = tmp_path / "canonical"
    staging = tmp_path / "staging"
    canonical.mkdir()
    staging.mkdir()

    payload = module.apply_audited_intraday_batch(
        staging_dir=staging,
        canonical_dir=canonical,
        symbols=["600000"],
        target_date="2026-08-13",
        run_id="lock-timeout-guard-dry",
        output_path=tmp_path / "out_dry.json",
        apply=False,
    )
    assert payload.get("applied") is False


def test_waiting_succeeds_when_sized_and_fails_when_undersized(tmp_path: Path) -> None:
    """A held lock is a wait, not an error - provided the wait is sized for it.

    Scaled proof of the real failure: the same contention that times out under
    an undersized wait completes under a sized one. Uses sub-second values so
    the test does not spend a minute reproducing the 60s default.
    """

    lock_path = canonical_intraday_write_lock_path(tmp_path / "canonical")
    hold_seconds = 0.6
    released = threading.Event()

    def _hold() -> None:
        with interprocess_file_lock(lock_path, timeout_seconds=5.0):
            time.sleep(hold_seconds)
        released.set()

    holder = threading.Thread(target=_hold, name="canonical-lock-holder")
    holder.start()
    try:
        # Give the holder time to actually own the lock.
        time.sleep(hold_seconds / 4)

        # Undersized wait: the contention surfaces as the observed failure mode.
        with pytest.raises(TimeoutError) as excinfo:
            with interprocess_file_lock(lock_path, timeout_seconds=0.05):
                pass
        assert "interprocess_file_lock_timeout" in str(excinfo.value)

        # Sized wait: the same contention is absorbed and the append proceeds.
        with interprocess_file_lock(lock_path, timeout_seconds=5.0):
            # Acquiring the lock already proves the holder released it; the
            # holder sets ``released`` immediately after that release, so wait
            # for that final set() deterministically instead of racing a bare
            # ``is_set()`` snapshot (which flakes under heavy full-suite load).
            assert released.wait(timeout=10)
    finally:
        holder.join(timeout=10)
    assert holder.is_alive() is False
