from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk.desk_control import WRITER_TASKS, pause_writers, resume_writers
from quantdesk.session_expectation import build_session_expectation


class TaskRunner:
    def __init__(self, *, state: str = "Running", after_end_error: str = "") -> None:
        self.state = state
        self.after_end_error = after_end_error
        self.ended = False
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> SimpleNamespace:
        self.commands.append(list(argv))
        action, name = argv[1], argv[3]
        if name != WRITER_TASKS[0]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        if action == "/Query":
            if self.ended and self.after_end_error:
                return SimpleNamespace(returncode=1, stdout="", stderr=self.after_end_error)
            return SimpleNamespace(returncode=0, stdout=f"Status: {self.state}\n", stderr="")
        if action == "/End":
            self.ended = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if action == "/Change":
            self.state = "Ready" if "/ENABLE" in argv else "Disabled"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


def test_post_end_access_denied_does_not_prove_stopped(tmp_path: Path) -> None:
    runner = TaskRunner(after_end_error="Access is denied.")
    result = pause_writers(tmp_path, runner=runner)
    assert result["ok"] is False, result
    assert not any(item["stop"] == "stopped" for item in result["writer_stops"])


def test_pending_stop_preserves_disabled_task_for_resume(tmp_path: Path) -> None:
    runner = TaskRunner()
    paused = pause_writers(tmp_path, runner=runner)
    assert paused["ok"] is False
    assert runner.state == "Disabled"
    resumed = resume_writers(tmp_path, runner=runner)
    assert WRITER_TASKS[0] in resumed["resumed_tasks"], resumed


def test_localized_running_task_is_ended(tmp_path: Path) -> None:
    runner = TaskRunner(state="正在运行")
    result = pause_writers(tmp_path, runner=runner)
    assert any(command[1] == "/End" for command in runner.commands), result


def test_scheduler_connection_error_is_not_missing_task(tmp_path: Path) -> None:
    def runner(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="The RPC server is unavailable.")

    result = pause_writers(tmp_path, runner=runner)
    assert result["ok"] is False, result


def test_task_name_words_do_not_override_explicit_status(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(list(argv))
        if argv[1] == "/Query":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"TaskName: QuantDesk Running Archive\n"
                    "Status: Ready\n"
                ),
                stderr="",
            )
        if argv[1] == "/Change":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    result = pause_writers(tmp_path, runner=runner)
    first = next(item for item in result["writers"] if item["name"] == WRITER_TASKS[0])
    assert first["state"] == "Ready"
    assert not any(command[1] == "/End" for command in commands)


@pytest.mark.parametrize(
    "row",
    [
        {"provider": "fixture-provider"},
        {"close": "10"},
        {"signature": "arbitrary_unchecked_label"},
        {"tradestatus": "0", "trade_date": "2026-07-14"},
        {"signature": "zero_volume_with_last_close"},
    ],
    ids=["provider-only", "close-only", "arbitrary-signature", "wrong-date", "placeholder"],
)
def test_incomplete_or_wrong_date_evidence_is_not_verified(
    tmp_path: Path, row: dict[str, str]
) -> None:
    (tmp_path / "data" / "daily").mkdir(parents=True)
    evidence_dir = tmp_path / "data" / "metadata" / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps({"target_date": "2026-07-15", "source": "fixture", "verified_exclusions": {"600929": row}}),
        encoding="utf-8",
    )
    result = build_session_expectation(
        ["600929"], "2026-07-15", project_root=tmp_path, network=False
    )
    observed = result["per_symbol"]["600929"]["classification"]
    assert observed == "unknown", {"evidence": row, "observed": observed}


@pytest.mark.parametrize(
    "row",
    [
        {
            "signature": "zero_volume_with_last_close",
            "open": "10",
            "close": "10",
            "volume": "100",
        },
        {
            "signature": "zero_volume_with_last_close",
            "open": "0",
            "close": "nan",
            "volume": "",
        },
        {
            "signature": "zero_trade_with_estimates",
            "open": "inf",
            "close": "0",
            "volume": "0",
        },
        {
            "signature": "zero_trade_with_estimates",
            "open": "0",
            "close": "-1",
            "volume": "0",
        },
    ],
    ids=["contradictory-volume", "nan-close", "infinite-open", "negative-close"],
)
def test_signature_specific_quote_facts_are_required(tmp_path: Path, row: dict[str, str]) -> None:
    (tmp_path / "data" / "daily").mkdir(parents=True)
    evidence_dir = tmp_path / "data" / "metadata" / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": "2026-07-15",
                "source": "sina.Market_Center.getHQNodeData",
                "verified_exclusions": {"600929": row},
            }
        ),
        encoding="utf-8",
    )
    result = build_session_expectation(["600929"], "2026-07-15", project_root=tmp_path)
    assert result["per_symbol"]["600929"]["classification"] == "unknown"


def test_unrecognized_source_cannot_certify_realistic_quote(tmp_path: Path) -> None:
    (tmp_path / "data" / "daily").mkdir(parents=True)
    evidence_dir = tmp_path / "data" / "metadata" / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": "2026-07-15",
                "source": "not_sina_verified",
                "verified_exclusions": {
                    "600929": {
                        "signature": "zero_volume_with_last_close",
                        "open": "0",
                        "close": "6.04",
                        "volume": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = build_session_expectation(["600929"], "2026-07-15", project_root=tmp_path)
    assert result["per_symbol"]["600929"]["classification"] == "unknown"


def test_real_sina_quote_facts_certify_zero_volume_row(tmp_path: Path) -> None:
    (tmp_path / "data" / "daily").mkdir(parents=True)
    evidence_dir = tmp_path / "data" / "metadata" / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": "2026-07-15",
                "source": "sina.Market_Center.getHQNodeData",
                "verified_exclusions": {
                    "600929": {
                        "signature": "zero_volume_with_last_close",
                        "open": "0.000",
                        "close": "6.040",
                        "volume": "0",
                        "amount": "",
                        "pe": "12.3",
                        "pb": "1.1",
                        "total_mv_raw": "100000000",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = build_session_expectation(["600929"], "2026-07-15", project_root=tmp_path)
    assert result["per_symbol"]["600929"]["classification"] == "verified_full_day_suspension"


def test_conflicting_row_source_does_not_certify_sina_sidecar(tmp_path: Path) -> None:
    (tmp_path / "data" / "daily").mkdir(parents=True)
    evidence_dir = tmp_path / "data" / "metadata" / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps(
            {
                "target_date": "2026-07-15",
                "source": "sina.Market_Center.getHQNodeData",
                "verified_exclusions": {
                    "600929": {
                        "source": "not_sina_verified",
                        "signature": "zero_volume_with_last_close",
                        "open": "0",
                        "close": "6.04",
                        "volume": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = build_session_expectation(["600929"], "2026-07-15", project_root=tmp_path)
    assert result["per_symbol"]["600929"]["classification"] == "unknown"


@pytest.mark.parametrize("volume", ["", None, "   ", "NaN", "Infinity", False])
@pytest.mark.parametrize(
    ("signature", "close"),
    [("zero_volume_with_last_close", "10"), ("zero_trade_with_estimates", "0")],
)
def test_missing_or_invalid_volume_is_not_observed_zero(
    tmp_path: Path, volume: object, signature: str, close: str
) -> None:
    (tmp_path / "data" / "daily").mkdir(parents=True)
    evidence_dir = tmp_path / "data" / "metadata" / "suspension_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "20260715.json").write_text(
        json.dumps({
            "target_date": "2026-07-15",
            "source": "sina.Market_Center.getHQNodeData",
            "verified_exclusions": {"600929": {
                "signature": signature, "open": "0", "close": close, "volume": volume,
            }},
        }),
        encoding="utf-8",
    )
    result = build_session_expectation(["600929"], "2026-07-15", project_root=tmp_path)
    assert result["per_symbol"]["600929"]["classification"] == "unknown"
