"""Pytest subprocess compatibility helpers and real-state write tripwire.

The state tripwire is a pytest plugin hook.  ``python -m unittest`` does not
load ``conftest.py``; callers using unittest discovery must keep an independent
before/after state manifest around that command.
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


REVIEW_PACK_GENERATORS: dict[str, tuple[str, list[str]]] = {}


_GENERATED_REVIEW_PACKS: set[str] = set()
_REVIEW_PACK_GENERATION_ERRORS: dict[str, str] = {}


_MISSING_SHA256 = "<missing>"


@dataclass(frozen=True)
class _StateFileFingerprint:
    sha256: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _StateTripwireViolation:
    owner: str
    phase: str
    path: str
    kind: str
    before_sha256: str
    after_sha256: str


_STATE_TRIPWIRE_STARTED = False
_STATE_TRIPWIRE_SESSION_BASELINE: dict[str, _StateFileFingerprint] = {}
_STATE_TRIPWIRE_LAST_OBSERVED: dict[str, _StateFileFingerprint] = {}
_STATE_TRIPWIRE_TEST_BASELINES: dict[str, dict[str, _StateFileFingerprint]] = {}
_STATE_TRIPWIRE_VIOLATIONS: list[_StateTripwireViolation] = []
_STATE_TRIPWIRE_VIOLATION_KEYS: set[tuple[str, str, str, str, str]] = set()
_OPERATION_LOG_SESSION_DIR: Path | None = None


def _state_tripwire_roots() -> list[tuple[str, Path]]:
    """Return read-only watched roots; demo roots can only be additive."""

    real_state = (ROOT / "state").resolve()
    real_features = (ROOT / "data" / "features").resolve()
    real_universe = (ROOT / "data" / "universe").resolve()
    real_shadow_archive = (ROOT / "results" / "risk_gate_shadow_daily").resolve()
    roots = [
        ("state", real_state),
        ("data/features", real_features),
        ("data/universe", real_universe),
        ("results/risk_gate_shadow_daily", real_shadow_archive),
    ]
    demo_raw = os.environ.get("QUANTDESK_STATE_TRIPWIRE_DEMO_DIR", "").strip()
    if demo_raw:
        demo_state = Path(demo_raw).expanduser().resolve()
        if demo_state != real_state:
            roots.append(("demo_state", demo_state))
    feature_demo_raw = os.environ.get(
        "QUANTDESK_FEATURE_TRIPWIRE_DEMO_DIR", ""
    ).strip()
    if feature_demo_raw:
        demo_features = Path(feature_demo_raw).expanduser().resolve()
        if demo_features not in {real_state, real_features, real_universe}:
            roots.append(("demo_features", demo_features))
    universe_demo_raw = os.environ.get(
        "QUANTDESK_UNIVERSE_TRIPWIRE_DEMO_DIR", ""
    ).strip()
    if universe_demo_raw:
        demo_universe = Path(universe_demo_raw).expanduser().resolve()
        if demo_universe not in {real_state, real_features, real_universe}:
            roots.append(("demo_universe", demo_universe))
    shadow_demo_raw = os.environ.get(
        "QUANTDESK_SHADOW_SINK_TRIPWIRE_DEMO_DIR", ""
    ).strip()
    if shadow_demo_raw:
        demo_archive = (
            Path(shadow_demo_raw).expanduser().resolve()
            / "risk_gate_shadow_daily"
        )
        if demo_archive != real_shadow_archive:
            roots.append(("demo_shadow_archive", demo_archive))
    return roots


def _state_tripwire_watched_files() -> list[tuple[str, Path]]:
    """Return specific real-sink files the suite must never create or rewrite.

    ``_state_tripwire_roots`` covers ``state/``, ``data/features/``, and
    ``data/universe/``.
    The formal risk-gate shadow sinks, the operation-log timeline sink, and
    the production industry-board cache live under ``results/`` (which
    legitimately receives many other artifacts); they are watched as narrow,
    named files rather than by adding either whole tree.  A stray write or
    byte-identical rewrite to any protected sink therefore fails the suite
    closed (裁定�?/ Round 5 §0.1, Round 10 Part C, and Round 12 §34.3b).
    """

    results_dir = (ROOT / "results").resolve()
    cache_dir = (ROOT / "data" / "cache").resolve()
    watched = [
        (
            "results/risk_gate_shadow_latest.json",
            results_dir / "risk_gate_shadow_latest.json",
        ),
        (
            "results/risk_gate_shadow_errors.jsonl",
            results_dir / "risk_gate_shadow_errors.jsonl",
        ),
        (
            "data/cache/industry_board_latest.csv",
            cache_dir / "industry_board_latest.csv",
        ),
        (
            "results/operation_log/operation_timeline.jsonl",
            results_dir / "operation_log" / "operation_timeline.jsonl",
        ),
        (
            "results/operation_log/latest_operation_status.json",
            results_dir / "operation_log" / "latest_operation_status.json",
        ),
    ]
    # Additive demo sink used only by the tripwire self-test so it can prove the
    # named-file watcher trips without ever writing the real ``results/`` sinks.
    demo_raw = os.environ.get("QUANTDESK_SHADOW_SINK_TRIPWIRE_DEMO_DIR", "").strip()
    if demo_raw:
        demo_dir = Path(demo_raw).expanduser().resolve()
        if demo_dir != results_dir:
            watched.append(
                (
                    "demo_shadow_sink/risk_gate_shadow_latest.json",
                    demo_dir / "risk_gate_shadow_latest.json",
                )
            )
            watched.append(
                (
                    "demo_shadow_sink/risk_gate_shadow_errors.jsonl",
                    demo_dir / "risk_gate_shadow_errors.jsonl",
                )
            )
    cache_demo_raw = os.environ.get(
        "QUANTDESK_CACHE_SINK_TRIPWIRE_DEMO_DIR", ""
    ).strip()
    if cache_demo_raw:
        demo_cache_dir = Path(cache_demo_raw).expanduser().resolve()
        if demo_cache_dir not in {results_dir, cache_dir}:
            watched.append(
                (
                    "demo_cache_sink/industry_board_latest.csv",
                    demo_cache_dir / "industry_board_latest.csv",
                )
            )
    # Additive demo operation-log sink used by the tripwire self-test so it can
    # prove the named-file watcher trips for the timeline sink without ever
    # writing the real ``results/operation_log`` files.
    operation_log_demo_raw = os.environ.get(
        "QUANTDESK_OPERATION_LOG_TRIPWIRE_DEMO_DIR", ""
    ).strip()
    if operation_log_demo_raw:
        demo_operation_log_dir = (
            Path(operation_log_demo_raw).expanduser().resolve()
        )
        if demo_operation_log_dir != results_dir:
            watched.append(
                (
                    "demo_operation_log/operation_timeline.jsonl",
                    demo_operation_log_dir / "operation_timeline.jsonl",
                )
            )
            watched.append(
                (
                    "demo_operation_log/latest_operation_status.json",
                    demo_operation_log_dir / "latest_operation_status.json",
                )
            )
    return watched


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint_or_race_marker(
    manifest: dict[str, _StateFileFingerprint], display_path: str, path: Path
) -> None:
    """Record a file's fingerprint, treating a mid-snapshot vanish as an event."""

    try:
        sha256 = _sha256(path)
        stat = path.stat()
    except FileNotFoundError:
        # A file disappearing during a snapshot is itself a fail-closed
        # observation.  Retaining the path makes the next comparison
        # report it instead of silently skipping the race.
        manifest[display_path] = _StateFileFingerprint(
            sha256="<vanished-during-snapshot>", size=-1, mtime_ns=-1
        )
        return
    except (PermissionError, OSError) as exc:
        # A watched file may be legitimately byte-locked by a concurrent
        # production run (``state/daily_data_chain.lock`` while a chain holds
        # it). That is an observation, not a reason to abort the whole test
        # session: recording a stable marker keeps the tripwire meaningful
        # (an added/removed lock still shows up as a diff) while letting the
        # suite run. Size/mtime are still read when available so a same-marker
        # file that changes size is not silently equal.
        marker = f"<unreadable-during-snapshot:{type(exc).__name__}>"
        try:
            stat = path.stat()
        except OSError:
            manifest[display_path] = _StateFileFingerprint(
                sha256=marker, size=-1, mtime_ns=-1
            )
            return
        manifest[display_path] = _StateFileFingerprint(
            sha256=marker, size=stat.st_size, mtime_ns=stat.st_mtime_ns
        )
        return
    manifest[display_path] = _StateFileFingerprint(
        sha256=sha256,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _snapshot_watched_state() -> dict[str, _StateFileFingerprint]:
    """Read watched fingerprints without creating, touching, or restoring files."""

    manifest: dict[str, _StateFileFingerprint] = {}
    for label, root in _state_tripwire_roots():
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            display_path = f"{label}/{relative}"
            _fingerprint_or_race_marker(manifest, display_path, path)
    # Named real-sink files (watched even when absent so an added file trips).
    for display_path, path in _state_tripwire_watched_files():
        if not path.is_file():
            continue
        _fingerprint_or_race_marker(manifest, display_path, path)
    return manifest


def _state_manifest_diff(
    before: dict[str, _StateFileFingerprint],
    after: dict[str, _StateFileFingerprint],
) -> list[tuple[str, str, str, str]]:
    diffs: list[tuple[str, str, str, str]] = []
    for path in sorted(set(before) | set(after)):
        before_value = before.get(path)
        after_value = after.get(path)
        if before_value == after_value:
            continue
        if before_value is None:
            kind = "added"
        elif after_value is None:
            kind = "removed"
        else:
            # Metadata is deliberately included so a write followed by a
            # byte-identical restore in the same pytest phase still trips.
            kind = "modified"
        diffs.append(
            (
                path,
                kind,
                before_value.sha256 if before_value else _MISSING_SHA256,
                after_value.sha256 if after_value else _MISSING_SHA256,
            )
        )
    return diffs


def _record_state_diffs(
    before: dict[str, _StateFileFingerprint],
    after: dict[str, _StateFileFingerprint],
    *,
    owner: str,
    phase: str,
) -> list[_StateTripwireViolation]:
    recorded: list[_StateTripwireViolation] = []
    for path, kind, before_sha256, after_sha256 in _state_manifest_diff(before, after):
        key = (owner, phase, path, before_sha256, after_sha256)
        if key in _STATE_TRIPWIRE_VIOLATION_KEYS:
            continue
        violation = _StateTripwireViolation(
            owner=owner,
            phase=phase,
            path=path,
            kind=kind,
            before_sha256=before_sha256,
            after_sha256=after_sha256,
        )
        _STATE_TRIPWIRE_VIOLATION_KEYS.add(key)
        _STATE_TRIPWIRE_VIOLATIONS.append(violation)
        recorded.append(violation)
    return recorded


def _format_state_violations(violations: list[_StateTripwireViolation]) -> str:
    lines = [
        "QuantDesk protected write tripwire detected unauthorized mutation "
        "(tests must never write real project state, data/features, "
        "data/universe, risk-shadow files/archive, operation-log timeline "
        "sinks, or market-cache sinks)."
    ]
    for violation in violations:
        lines.extend(
            [
                f"owner={violation.owner}",
                f"phase={violation.phase}",
                f"path={violation.path}",
                f"kind={violation.kind}",
                f"before_sha256={violation.before_sha256}",
                f"after_sha256={violation.after_sha256}",
            ]
        )
    return "\n".join(lines)


def pytest_configure() -> None:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    os.environ["PYTHONIOENCODING"] = f"{encoding}:backslashreplace"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Freeze protected per-file SHA baselines before collection begins.

    Redirect the operation-log sink to a per-session temp dir so qd.py
    subprocesses spawned by tests never append to the real
    ``results/operation_log`` timeline (Round 12 §34.3b).  Test modules that
    self-isolate at import time may overwrite this env var with their own
    per-process temp dir; either way the real sink stays untouched.
    """

    del session  # hook signature; the snapshot is process-global for this session
    global _STATE_TRIPWIRE_STARTED
    global _STATE_TRIPWIRE_SESSION_BASELINE
    global _STATE_TRIPWIRE_LAST_OBSERVED
    global _OPERATION_LOG_SESSION_DIR

    _STATE_TRIPWIRE_TEST_BASELINES.clear()
    _STATE_TRIPWIRE_VIOLATIONS.clear()
    _STATE_TRIPWIRE_VIOLATION_KEYS.clear()
    _STATE_TRIPWIRE_SESSION_BASELINE = _snapshot_watched_state()
    _STATE_TRIPWIRE_LAST_OBSERVED = dict(_STATE_TRIPWIRE_SESSION_BASELINE)
    _STATE_TRIPWIRE_STARTED = True

    if not os.environ.get("QUANTDESK_OPERATION_LOG_DIR", "").strip():
        _OPERATION_LOG_SESSION_DIR = Path(
            tempfile.mkdtemp(prefix="qd_pytest_operation_log_")
        ).resolve()
        os.environ["QUANTDESK_OPERATION_LOG_DIR"] = str(_OPERATION_LOG_SESSION_DIR)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_finish(session: pytest.Session):
    """Attribute writes during collection to the session, not the first test."""

    yield
    global _STATE_TRIPWIRE_LAST_OBSERVED
    if not _STATE_TRIPWIRE_STARTED:
        return
    current = _snapshot_watched_state()
    _record_state_diffs(
        _STATE_TRIPWIRE_SESSION_BASELINE,
        current,
        owner="session/collection",
        phase="collection",
    )
    _STATE_TRIPWIRE_LAST_OBSERVED = current


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    release_packaging_files = {
        "test_v350_battle_ready_portable_release.py",
        "test_v351_packaging_honesty.py",
        "test_v3511_release_staging_cleanup_and_attribution_stress.py",
    }
    skip_release_packaging = pytest.mark.skip(
        reason="Skipped by research-only boundary: release/portable packaging is intentionally disabled."
    )
    for item in items:
        path = Path(str(item.fspath))
        if path.name in release_packaging_files:
            item.add_marker(skip_release_packaging)
        if path.name == "test_v360_ui_and_release_hardening.py" and item.name == "test_build_scripts_use_no_bom_json_writer":
            item.add_marker(skip_release_packaging)

def _manifest_complete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    return not bool(payload.get("missing_files"))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    global _STATE_TRIPWIRE_LAST_OBSERVED
    if _STATE_TRIPWIRE_STARTED:
        current = _snapshot_watched_state()
        _record_state_diffs(
            _STATE_TRIPWIRE_LAST_OBSERVED,
            current,
            owner=f"session/between-tests-before::{item.nodeid}",
            phase="between-tests",
        )
        _STATE_TRIPWIRE_TEST_BASELINES[item.nodeid] = current
        _STATE_TRIPWIRE_LAST_OBSERVED = current

    test_name = Path(str(item.fspath)).name
    if test_name in REVIEW_PACK_GENERATORS and test_name not in _REVIEW_PACK_GENERATION_ERRORS:
        manifest_rel, command = REVIEW_PACK_GENERATORS[test_name]
        error = _ensure_review_pack_manifest(test_name, manifest_rel, command)
        if error:
            _REVIEW_PACK_GENERATION_ERRORS[test_name] = error
    if test_name in _REVIEW_PACK_GENERATION_ERRORS:
        pytest.fail(_REVIEW_PACK_GENERATION_ERRORS[test_name])


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    """Check setup/call/fixture-teardown and retain every observed event."""

    global _STATE_TRIPWIRE_LAST_OBSERVED
    outcome = yield
    report = outcome.get_result()
    if not _STATE_TRIPWIRE_STARTED:
        return

    current = _snapshot_watched_state()
    baseline = _STATE_TRIPWIRE_TEST_BASELINES.get(
        item.nodeid, _STATE_TRIPWIRE_SESSION_BASELINE
    )
    recorded = _record_state_diffs(
        baseline,
        current,
        owner=item.nodeid,
        phase=call.when,
    )
    _STATE_TRIPWIRE_LAST_OBSERVED = current
    if call.when == "teardown":
        _STATE_TRIPWIRE_TEST_BASELINES.pop(item.nodeid, None)

    if recorded:
        detail = _format_state_violations(recorded)
        if report.passed:
            report.outcome = "failed"
            report.longrepr = detail
        else:
            report.sections.append(("QuantDesk state write tripwire", detail))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int):
    """Perform the mandatory final comparison after all other finish hooks."""

    del exitstatus
    yield
    global _OPERATION_LOG_SESSION_DIR
    if not _STATE_TRIPWIRE_STARTED:
        return
    current = _snapshot_watched_state()
    recorded_at_finish = _record_state_diffs(
        _STATE_TRIPWIRE_SESSION_BASELINE,
        current,
        owner="session/session-finish",
        phase="session-finish",
    )
    if recorded_at_finish:
        # The terminal plugin may already have invoked pytest_terminal_summary
        # inside its own sessionfinish hook.  Emit finish-only evidence here as
        # well so an unattributed late write can never fail without details.
        terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminalreporter is not None:
            terminalreporter.section(
                "QuantDesk state write tripwire (session finish)", sep="="
            )
            terminalreporter.write_line(_format_state_violations(recorded_at_finish))
    if _STATE_TRIPWIRE_VIOLATIONS:
        session.testsfailed = max(session.testsfailed, 1)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if _OPERATION_LOG_SESSION_DIR is not None:
        shutil.rmtree(_OPERATION_LOG_SESSION_DIR, ignore_errors=True)
        _OPERATION_LOG_SESSION_DIR = None


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _STATE_TRIPWIRE_VIOLATIONS:
        return
    terminalreporter.section("QuantDesk state write tripwire", sep="=")
    terminalreporter.write_line(_format_state_violations(_STATE_TRIPWIRE_VIOLATIONS))


def _ensure_review_pack_manifest(test_name: str, manifest_rel: str, command: list[str]) -> str:
    if manifest_rel in _GENERATED_REVIEW_PACKS:
        return ""
    proc = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=360,
        check=False,
    )
    manifest = ROOT / manifest_rel
    if not _manifest_complete(manifest):
        tail = (proc.stderr + "\n" + proc.stdout)[-2000:]
        return f"{test_name} could not generate complete review manifest {manifest_rel}: returncode={proc.returncode}\n{tail}"
    _GENERATED_REVIEW_PACKS.add(manifest_rel)
    return ""
