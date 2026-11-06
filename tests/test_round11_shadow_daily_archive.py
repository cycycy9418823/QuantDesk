from __future__ import annotations

import importlib
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from quantdesk.risk import risk_gate_shadow as shadow
from quantdesk.risk import risk_gate_shadow_weekly as weekly


def _process_write_shadow(
    payload: dict[str, object],
    latest: str,
    barrier: Any,
    queue: Any,
) -> None:
    barrier.wait(timeout=20)
    try:
        shadow.write_shadow_summary(payload, output_path=Path(latest))
    except Exception as exc:  # pragma: no cover - asserted in the parent process
        queue.put(("error", type(exc).__name__, str(exc)))
    else:
        queue.put(("ok", "", ""))


def _summary(
    as_of: str,
    *,
    generated_at: str,
    evaluation_count: int = 0,
    insufficient_count: int = 0,
) -> dict[str, object]:
    payload = shadow.summarize_shadow_records([], as_of=as_of)
    payload.update(
        {
            "generated_at": generated_at,
            "observed_count": evaluation_count,
            "evaluation_count": evaluation_count,
            "would_pass_count": max(evaluation_count - insufficient_count, 0),
            "would_block_count": 0,
            "insufficient_evidence_count": insufficient_count,
            "insufficient_evidence_ratio": (
                insufficient_count / evaluation_count if evaluation_count else 0.0
            ),
        }
    )
    return payload


def _write_archive(archive_dir: Path, payload: dict[str, object]) -> Path:
    latest = archive_dir.parent / "risk_gate_shadow_latest.json"
    shadow.write_shadow_summary(
        payload,
        output_path=latest,
        archive_dir=archive_dir,
    )
    return archive_dir / f"{payload['as_of']}.json"


def test_daily_archive_same_day_keeps_newest_and_prior_day_is_immutable(
    tmp_path: Path,
) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    archive_dir = latest.parent / "risk_gate_shadow_daily"
    day_one_early = _summary(
        "2026-07-27", generated_at="2026-07-27T09:00:00+00:00"
    )
    day_one_late = _summary(
        "2026-07-27", generated_at="2026-07-27T10:00:00+00:00"
    )

    shadow.write_shadow_summary(day_one_early, output_path=latest)
    archive_one = archive_dir / "2026-07-27.json"
    assert archive_one.read_bytes() == latest.read_bytes()
    assert shadow.verify_shadow_archive_seal(archive_one) is True

    shadow.write_shadow_summary(day_one_late, output_path=latest)
    assert json.loads(archive_one.read_text(encoding="utf-8"))["generated_at"] == (
        "2026-07-27T10:00:00+00:00"
    )

    shadow.write_shadow_summary(
        _summary("2026-07-27", generated_at="2026-07-27T09:30:00+00:00"),
        output_path=latest,
    )
    assert json.loads(latest.read_text(encoding="utf-8"))["generated_at"] == (
        "2026-07-27T10:00:00+00:00"
    )

    day_two = _summary("2026-07-28", generated_at="2026-07-28T10:00:00+00:00")
    shadow.write_shadow_summary(day_two, output_path=latest)
    archive_one_before = archive_one.read_bytes()
    latest_before = latest.read_bytes()
    with pytest.raises(ValueError, match="historical shadow archive is immutable"):
        shadow.write_shadow_summary(
            _summary("2026-07-27", generated_at="2026-07-28T11:00:00+00:00"),
            output_path=latest,
        )
    assert archive_one.read_bytes() == archive_one_before
    assert latest.read_bytes() == latest_before


def test_stale_zero_traffic_summary_is_archived(tmp_path: Path) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    payload = _summary("2026-07-28", generated_at="2026-07-28T10:00:00+00:00")
    payload["observation_reason"] = "stale_data_no_traffic"

    shadow.write_shadow_summary(payload, output_path=latest)

    archived = json.loads(
        (latest.parent / "risk_gate_shadow_daily" / "2026-07-28.json").read_text(
            encoding="utf-8"
        )
    )
    assert archived["observation_reason"] == "stale_data_no_traffic"
    assert archived["evaluation_count"] == 0


def test_latest_backfill_is_explicit_idempotent_and_does_not_rewrite_latest(
    tmp_path: Path,
) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    latest.parent.mkdir(parents=True)
    payload = _summary("2026-07-27", generated_at="2026-07-26T17:36:40+00:00")
    latest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    latest_before = latest.read_bytes()

    archive = shadow.backfill_latest_shadow_archive(
        latest_path=latest,
        archive_dir=latest.parent / "risk_gate_shadow_daily",
    )
    archive_before = archive.read_bytes()
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert archived["as_of"] == "2026-07-27"
    assert archived["archive_source"] == "latest_backfill"
    assert shadow.shadow_archive_seal_path(archive).is_file()
    assert shadow.verify_shadow_archive_seal(archive) is True
    assert latest.read_bytes() == latest_before

    assert shadow.backfill_latest_shadow_archive(
        latest_path=latest,
        archive_dir=latest.parent / "risk_gate_shadow_daily",
    ) == archive
    assert archive.read_bytes() == archive_before
    assert shadow.verify_shadow_archive_seal(archive) is True


def test_default_backfill_path_follows_runtime_latest_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            _summary("2026-07-27", generated_at="2026-07-26T17:36:40+00:00")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(shadow, "RISK_GATE_SHADOW_LATEST_JSON", latest)

    archive = shadow.backfill_latest_shadow_archive()

    assert archive == latest.parent / "risk_gate_shadow_daily" / "2026-07-27.json"
    assert archive.is_file()


def test_shadow_window_deduplicates_latest_and_rejects_strays(tmp_path: Path) -> None:
    archive_dir = tmp_path / "risk_gate_shadow_daily"
    archive_dir.mkdir()
    for day in (
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
    ):
        _write_archive(
            archive_dir,
            _summary(
                day,
                generated_at=f"{day}T10:00:00+00:00",
                evaluation_count=1,
            ),
        )
    (archive_dir / "stray.json").write_text(
        json.dumps({"task_type": "strategy_brain_shadow", "as_of": "2026-07-27"}),
        encoding="utf-8",
    )
    latest = tmp_path / "risk_gate_shadow_latest.json"
    latest.write_text(
        json.dumps(
            _summary(
                "2026-07-27",
                generated_at="2026-07-27T11:00:00+00:00",
                evaluation_count=2,
            )
        ),
        encoding="utf-8",
    )

    window = weekly.load_shadow_daily_window(
        archive_dir=archive_dir,
        latest_path=latest,
        as_of="2026-07-27",
        limit=5,
    )

    assert [row["as_of"] for row in window.summaries] == [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
    ]
    assert window.summaries[-1]["evaluation_count"] == 1
    assert any(
        row["reason"] == "latest_conflicts_with_dated_archive"
        for row in window.rejected
    )
    assert any(row["reason"] == "archive_filename_invalid" for row in window.rejected)


@pytest.mark.parametrize(
    ("days", "expected_mode"),
    [(0, "no_data"), (2, "insufficient"), (5, "observed")],
)
def test_shadow_weekly_cli_reports_three_honest_modes(
    tmp_path: Path,
    days: int,
    expected_mode: str,
) -> None:
    archive_dir = tmp_path / "results" / "risk_gate_shadow_daily"
    archive_dir.mkdir(parents=True)
    trading_days = [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
    ]
    for day in trading_days[:days]:
        _write_archive(
            archive_dir,
            _summary(
                day,
                generated_at=f"{day}T10:00:00+00:00",
                evaluation_count=1,
            ),
        )
    reports_dir = tmp_path / "reports" / "risk_gate_shadow"

    runner = importlib.import_module("scripts.run_risk_gate_shadow_weekly")
    exit_code = runner.main(
        [
            "--project-root",
            str(tmp_path),
            "--archive-dir",
            str(archive_dir),
            "--latest-path",
            str(tmp_path / "results" / "missing_latest.json"),
            "--reports-dir",
            str(reports_dir),
            "--as-of",
            "2026-07-27",
            "--network",
            "false",
            "--json",
            "true",
            "--summary",
            "true",
        ]
    )

    assert exit_code == 0
    payload = json.loads(
        (reports_dir / "risk_gate_shadow_weekly_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["mode"] == expected_mode
    assert payload["observed_days"] == days
    assert payload["risk_gate_enforced"] is False
    assert payload["enforcement_applied"] is False
    assert "停摆" not in json.dumps(payload, ensure_ascii=False)


def test_qd_registers_shadow_weekly_in_all_command_surfaces() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / "qd.py").read_text(
        encoding="utf-8"
    )
    assert '"shadow-weekly": ("run_risk_gate_shadow_weekly.py"' in text
    assert '"shadow-weekly": [' in text
    assert '"shadow-weekly": PROJECT_ROOT' in text
    assert '"shadow-weekly --network false --json true --summary true"' in text


def test_all_four_production_shadow_writes_share_the_archive_aware_sink() -> None:
    from quantdesk.paper import auto_paper_portfolio as paper
    from quantdesk.paper import position_management_engine as positions

    root = Path(__file__).resolve().parents[1]
    auto_source = "\n".join(
        (root / "quantdesk" / "paper" / path).read_text(encoding="utf-8")
        for path in ("auto_paper_portfolio.py", "execution/daily.py", "execution/conditional.py", "execution/snapshot.py")
    )
    position_source = (
        root / "quantdesk" / "paper" / "position_management_engine.py"
    ).read_text(encoding="utf-8")

    assert auto_source.count("write_shadow_summary(") == 3
    assert position_source.count("write_shadow_summary(") == 1
    assert paper._daily_execution_ports().risk.write_shadow_summary is shadow.write_shadow_summary
    assert paper._conditional_execution_ports().risk.write_shadow_summary is shadow.write_shadow_summary
    assert positions.write_shadow_summary is shadow.write_shadow_summary


def test_retry_repairs_latest_after_archive_succeeds_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    payload = _summary("2026-07-27", generated_at="2026-07-27T10:00:00+00:00")
    real_atomic_write = shadow.atomic_write_json
    failed = False

    def fail_latest_once(path: Path, value: object) -> Path:
        nonlocal failed
        if Path(path) == latest and not failed:
            failed = True
            raise OSError("simulated latest replace failure")
        return real_atomic_write(path, value)

    monkeypatch.setattr(shadow, "atomic_write_json", fail_latest_once)
    with pytest.raises(OSError, match="simulated latest replace failure"):
        shadow.write_shadow_summary(payload, output_path=latest)

    archive = latest.parent / "risk_gate_shadow_daily" / "2026-07-27.json"
    assert archive.is_file()
    assert not latest.exists()

    monkeypatch.setattr(shadow, "atomic_write_json", real_atomic_write)
    shadow.write_shadow_summary(payload, output_path=latest)

    assert latest.read_bytes() == archive.read_bytes()


def test_weekly_does_not_count_weekends_or_sparse_stale_days_as_five_sessions() -> None:
    weekend_window = [
        _summary(day, generated_at=f"{day}T10:00:00+00:00", evaluation_count=1)
        for day in (
            "2026-07-23",
            "2026-07-24",
            "2026-07-25",
            "2026-07-26",
            "2026-07-27",
        )
    ]
    review = weekly.summarize_shadow_weekly(weekend_window, as_of="2026-07-27")
    assert review.mode == "insufficient"
    assert review.observed_days == 3
    assert any(row["reason"] == "as_of_not_trading_day" for row in review.input_rejections)

    sparse_window = [
        _summary(day, generated_at=f"{day}T10:00:00+00:00", evaluation_count=1)
        for day in (
            "2026-07-01",
            "2026-07-07",
            "2026-07-14",
            "2026-07-21",
            "2026-07-27",
        )
    ]
    sparse = weekly.summarize_shadow_weekly(sparse_window, as_of="2026-07-27")
    assert sparse.mode == "insufficient"
    assert sparse.observed_days == 2
    assert any(
        row["reason"] == "outside_trailing_trading_window"
        for row in sparse.input_rejections
    )


def test_weekly_rejects_enforced_input_instead_of_relabelling_false() -> None:
    payload = _summary(
        "2026-07-27",
        generated_at="2026-07-27T10:00:00+00:00",
        evaluation_count=1,
    )
    payload["risk_gate_enforced"] = True
    payload["enforcement_applied"] = True

    review = weekly.summarize_shadow_weekly([payload], as_of="2026-07-27")

    assert review.observed_days == 0
    assert any(
        row["reason"] == "shadow_enforcement_boundary_breach"
        for row in review.input_rejections
    )


def test_latest_cannot_override_conflicting_dated_archive(tmp_path: Path) -> None:
    archive_dir = tmp_path / "risk_gate_shadow_daily"
    archive_dir.mkdir()
    day = "2026-07-27"
    dated = _summary(day, generated_at=f"{day}T10:00:00+00:00", evaluation_count=1)
    latest_payload = _summary(
        day,
        generated_at=f"{day}T11:00:00+00:00",
        evaluation_count=2,
    )
    _write_archive(archive_dir, dated)
    latest = tmp_path / "risk_gate_shadow_latest.json"
    latest.write_text(json.dumps(latest_payload), encoding="utf-8")

    window = weekly.load_shadow_daily_window(
        archive_dir=archive_dir,
        latest_path=latest,
        as_of=day,
        limit=5,
    )

    assert window.summaries[0]["evaluation_count"] == 1
    assert any(
        row["reason"] == "latest_conflicts_with_dated_archive"
        for row in window.rejected
    )


@pytest.mark.parametrize("dated_mode", ["missing", "malformed"])
def test_latest_only_never_counts_as_a_dated_observation(
    tmp_path: Path,
    dated_mode: str,
) -> None:
    archive_dir = tmp_path / "risk_gate_shadow_daily"
    archive_dir.mkdir()
    days = [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
    ]
    for day in days[:4]:
        _write_archive(
            archive_dir,
            _summary(
                day,
                generated_at=f"{day}T10:00:00+00:00",
                evaluation_count=1,
            ),
        )
    latest = tmp_path / "risk_gate_shadow_latest.json"
    latest.write_text(
        json.dumps(
            _summary(
                days[-1],
                generated_at=f"{days[-1]}T10:00:00+00:00",
                evaluation_count=1,
            )
        ),
        encoding="utf-8",
    )
    if dated_mode == "malformed":
        (archive_dir / f"{days[-1]}.json").write_text("{broken", encoding="utf-8")

    window = weekly.load_shadow_daily_window(
        archive_dir=archive_dir,
        latest_path=latest,
        as_of=days[-1],
        limit=5,
    )
    review = weekly.summarize_shadow_weekly(window.summaries, as_of=days[-1])

    assert len(window.summaries) == 4
    assert review.mode == "insufficient"
    assert any(
        row["reason"] == "latest_without_valid_dated_archive"
        for row in window.rejected
    )


def test_dated_archive_seal_detects_payload_tampering(tmp_path: Path) -> None:
    archive_dir = tmp_path / "risk_gate_shadow_daily"
    day = "2026-07-27"
    archive = _write_archive(
        archive_dir,
        _summary(
            day,
            generated_at=f"{day}T10:00:00+00:00",
            evaluation_count=1,
        ),
    )
    tampered = json.loads(archive.read_text(encoding="utf-8"))
    tampered["observation_reason"] = "edited_after_archive"
    archive.write_text(json.dumps(tampered), encoding="utf-8")

    assert shadow.verify_shadow_archive_seal(archive) is False
    window = weekly.load_shadow_daily_window(
        archive_dir=archive_dir,
        latest_path=tmp_path / "missing_latest.json",
        as_of=day,
        limit=5,
    )

    assert window.summaries == []
    assert any(
        row["reason"] == "archive_content_sha256_mismatch"
        for row in window.rejected
    )


@pytest.mark.parametrize("operation", ["write", "backfill"])
def test_explicit_archive_directory_must_be_latest_sibling(
    tmp_path: Path,
    operation: str,
) -> None:
    latest = tmp_path / "isolated" / "risk_gate_shadow_latest.json"
    archive_dir = tmp_path / "other" / "risk_gate_shadow_daily"
    payload = _summary("2026-07-27", generated_at="2026-07-27T10:00:00+00:00")
    if operation == "backfill":
        latest.parent.mkdir(parents=True)
        latest.write_text(json.dumps(payload), encoding="utf-8")
        call = lambda: shadow.backfill_latest_shadow_archive(
            latest_path=latest,
            archive_dir=archive_dir,
        )
    else:
        call = lambda: shadow.write_shadow_summary(
            payload,
            output_path=latest,
            archive_dir=archive_dir,
        )

    with pytest.raises(ValueError, match="archive_dir.*sibling"):
        call()

    assert not archive_dir.exists()


def test_shadow_weekly_cli_rejects_reports_escape(tmp_path: Path) -> None:
    runner = importlib.import_module("scripts.run_risk_gate_shadow_weekly")
    root = tmp_path / "project"

    exit_code = runner.main(
        [
            "--project-root",
            str(root),
            "--reports-dir",
            str(root / "state"),
            "--as-of",
            "2026-07-27",
            "--network",
            "false",
        ]
    )

    assert exit_code == 2
    assert not (root / "state").exists()


@pytest.mark.parametrize(
    ("module_name", "output_flag", "allowed_name"),
    [
        ("scripts.run_risk_gate_shadow_weekly", "--reports-dir", "reports"),
        ("scripts.run_stage4_maintenance_evidence", "--output-dir", "results"),
    ],
)
def test_cli_output_guard_rejects_symlinked_allowed_root(
    tmp_path: Path,
    module_name: str,
    output_flag: str,
    allowed_name: str,
) -> None:
    runner = importlib.import_module(module_name)
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    allowed = root / allowed_name
    try:
        allowed.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    exit_code = runner.main(
        [
            "--project-root",
            str(root),
            output_flag,
            str(allowed / "round11_probe"),
            "--as-of",
            "2026-07-27",
            "--network",
            "false",
        ]
    )

    assert exit_code == 2
    assert list(outside.iterdir()) == []


def test_concurrent_same_day_writers_converge_on_newest_payload(tmp_path: Path) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    early = _summary("2026-07-27", generated_at="2026-07-27T09:00:00+00:00")
    late = _summary("2026-07-27", generated_at="2026-07-27T10:00:00+00:00")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(shadow.write_shadow_summary, early, output_path=latest),
            pool.submit(shadow.write_shadow_summary, late, output_path=latest),
        ]
        for future in futures:
            future.result()

    archive = latest.parent / "risk_gate_shadow_daily" / "2026-07-27.json"
    assert json.loads(latest.read_text(encoding="utf-8"))["generated_at"] == (
        "2026-07-27T10:00:00+00:00"
    )
    assert latest.read_bytes() == archive.read_bytes()


def test_two_process_writers_converge_on_newest_payload(tmp_path: Path) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    early = _summary("2026-07-27", generated_at="2026-07-27T09:00:00+00:00")
    late = _summary("2026-07-27", generated_at="2026-07-27T10:00:00+00:00")
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_process_write_shadow,
            args=(payload, str(latest), barrier, queue),
        )
        for payload in (early, late)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=5) for _ in processes]

    archive = latest.parent / "risk_gate_shadow_daily" / "2026-07-27.json"
    assert [row[0] for row in outcomes] == ["ok", "ok"]
    assert json.loads(archive.read_text(encoding="utf-8"))["generated_at"] == (
        "2026-07-27T10:00:00+00:00"
    )
    assert latest.read_bytes() == archive.read_bytes()
    assert shadow.verify_shadow_archive_seal(archive) is True


def test_two_process_equal_timestamp_conflict_fails_closed(tmp_path: Path) -> None:
    latest = tmp_path / "results" / "risk_gate_shadow_latest.json"
    left = _summary(
        "2026-07-27",
        generated_at="2026-07-27T10:00:00+00:00",
        evaluation_count=1,
    )
    right = _summary(
        "2026-07-27",
        generated_at="2026-07-27T10:00:00+00:00",
        evaluation_count=2,
    )
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_process_write_shadow,
            args=(payload, str(latest), barrier, queue),
        )
        for payload in (left, right)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=5) for _ in processes]

    archive = latest.parent / "risk_gate_shadow_daily" / "2026-07-27.json"
    assert sorted(row[0] for row in outcomes) == ["error", "ok"]
    assert any(
        row[1] == "ValueError" and "generated_at conflict" in row[2]
        for row in outcomes
    )
    assert latest.read_bytes() == archive.read_bytes()
    assert shadow.verify_shadow_archive_seal(archive) is True


def test_direct_weekly_summary_deduplicates_as_of_by_generated_at() -> None:
    days = [
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
    ]
    summaries = [
        _summary(day, generated_at=f"{day}T09:00:00+00:00", evaluation_count=1)
        for day in days
    ]
    summaries.append(
        _summary(
            "2026-07-27",
            generated_at="2026-07-27T10:00:00+00:00",
            evaluation_count=2,
        )
    )

    review = weekly.summarize_shadow_weekly(summaries, as_of="2026-07-27")

    assert review.observed_days == 5
    assert review.totals["evaluation_count"] == 6
    assert any(
        row["reason"] == "duplicate_as_of_older_copy"
        for row in review.input_rejections
    )
