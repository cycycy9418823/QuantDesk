"""Round 7 — T0 launch: end-to-end forward-PIT sector capture and milestone artifact.

Part A proves the in-chain T0-birth path end to end and offline: the daily-chain
``_refresh_sector_snapshot`` step (board-heat refresh + same-day membership
capture) feeds ``_archive_forward_sector_snapshot`` and, when an eligible
provider is injected, produces ``written=true`` — the definition of T0.  A
provider returning unaligned/unknown membership degrades without a T0 and never
writes a snapshot, proving the qualification gate is not relaxed.

Part B proves the round7 readiness artifact anchors T0 and computes the
T0+4/T0+8/T0+18 milestones on real trading days.

Every test runs in an isolated ``tmp_path`` root; no network, no ``--apply``, no
canonical/state write.  The archive's ``_local_today`` is frozen so the
same-day guard is deterministic regardless of when the suite runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
FROZEN_DAY = date(2026, 7, 22)
FROZEN_CAPTURE = "2026-07-22T15:28:00"


@pytest.fixture(autouse=True)
def _freeze_archive_day(monkeypatch) -> None:
    import quantdesk.data_truth.sector_history_archive as archive

    monkeypatch.setattr(archive, "_local_today", lambda: FROZEN_DAY)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pin_mtime(path: Path, when: str = "2026-07-22T09:00:00") -> None:
    stamp = datetime.fromisoformat(when).timestamp()
    os.utime(path, (stamp, stamp))


def _board_cache(root: Path) -> Path:
    board_path = _write(
        root / "data" / "cache" / "industry_board_latest.csv",
        "name,pct_chg,amount,leading_stock\n"
        "Finance,1.2,100,Alpha\n"
        "Energy,-0.4,80,Beta\n",
    )
    _pin_mtime(board_path)
    return board_path


def _board_refresher(board_path: Path):
    def refresher() -> dict:
        return {
            "ok": True,
            "source": "eastmoney",
            "cache_path": str(board_path.resolve()),
            "board_count": 2,
        }

    return refresher


def _eligible_fetcher(provider: str, board: str) -> pd.DataFrame:
    rows = {
        "Finance": [{"代码": "000001", "名称": "Alpha"}],
        "Energy": [{"代码": "000002", "名称": "Beta"}],
    }
    return pd.DataFrame(rows[board])


def _run_refresh(chain, root: Path, board_path: Path, fetcher, monkeypatch):
    monkeypatch.setattr(chain, "now_text", lambda: FROZEN_CAPTURE)
    payload = chain._refresh_sector_snapshot(
        root,
        network=True,
        board_refresher=_board_refresher(board_path),
        member_fetcher=fetcher,
    )
    # The captured membership CSV is stamped with real wall-clock mtime; pin it
    # to the frozen day so the archive's same-day source guard is deterministic.
    observation = payload.get("membership_observation") or {}
    captured_path = observation.get("path")
    if captured_path:
        _pin_mtime(Path(captured_path))
    return payload


def test_t0_is_born_when_eligible_provider_feeds_the_chain(tmp_path, monkeypatch) -> None:
    import quantdesk.daily_data_chain as chain

    board_path = _board_cache(tmp_path)
    refresh = _run_refresh(chain, tmp_path, board_path, _eligible_fetcher, monkeypatch)

    assert refresh["status"] == "completed"
    observation = refresh["membership_observation"]
    assert observation["status"] == "completed"
    assert observation["row_count"] == 2
    assert observation["data_universe_written"] is False

    sector_refresh_payload = {
        **{k: v for k, v in refresh.items() if k != "membership_observation"},
        "membership_observation": observation,
    }
    archived = chain._archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        sector_refresh_payload=sector_refresh_payload,
        captured_at=FROZEN_CAPTURE,
    )

    # written=true on the same day the snapshot names == T0 is born.
    assert archived["status"] == "passed"
    assert archived["written"] is True
    assert archived["evidence_eligible"] is True
    assert archived["pit_eligible"] is True
    assert archived["historical_backfill"] is False
    assert archived["reason_codes"] == []
    assert archived["membership_board_alignment_ratio"] == 1.0
    snapshot = tmp_path / "data" / "sector_history" / "2026-07-22" / "snapshot.json"
    assert snapshot.is_file()
    # The membership capture staged into results/, never data/universe.
    assert list((tmp_path / "data" / "universe").glob("*")) == []


def test_no_t0_when_provider_membership_is_unknown(tmp_path, monkeypatch) -> None:
    import quantdesk.daily_data_chain as chain

    board_path = _board_cache(tmp_path)

    def unknown_fetcher(provider: str, board: str) -> pd.DataFrame:
        # Provider returns rows with no usable symbol code → capture degrades.
        return pd.DataFrame([{"名称": "Alpha"}])

    refresh = _run_refresh(chain, tmp_path, board_path, unknown_fetcher, monkeypatch)
    observation = refresh["membership_observation"]
    # No normalizable members → capture stays degraded, no staged CSV path.
    assert observation["status"] == "degraded"
    assert observation["row_count"] == 0

    sector_refresh_payload = {
        **{k: v for k, v in refresh.items() if k != "membership_observation"},
        "membership_observation": observation,
    }
    archived = chain._archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        sector_refresh_payload=sector_refresh_payload,
        captured_at=FROZEN_CAPTURE,
    )
    assert archived["status"] == "degraded"
    assert archived["written"] is False
    assert archived["evidence_eligible"] is False
    assert not (tmp_path / "data" / "sector_history" / "2026-07-22").exists()


def test_degraded_archive_does_not_block_chain_and_writes_nothing(tmp_path) -> None:
    import quantdesk.daily_data_chain as chain

    # A blocked provider refresh must degrade the archive without raising and
    # without creating any sector_history directory.
    archived = chain._archive_forward_sector_snapshot(
        tmp_path,
        target_date="2026-07-22",
        sector_refresh_payload={"ok": False, "status": "blocked"},
        captured_at=FROZEN_CAPTURE,
    )
    assert archived["status"] == "degraded"
    assert archived["written"] is False
    assert archived["reason_codes"] == ["sector_refresh_not_completed"]
    assert not (tmp_path / "data" / "sector_history").exists()


# --------------------------------------------------------------------------- #
# Part B — readiness artifact: T0 anchoring + trading-day milestones
# --------------------------------------------------------------------------- #


def _eligible_snapshot(root: Path, day: str) -> None:
    """Write a minimal eligible (passed) snapshot for ``day`` into history."""
    snap = root / "data" / "sector_history" / day / "snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        __import__("json").dumps(
            {
                "status": "passed",
                "evidence_eligible": True,
                "pit_eligible": True,
                "target_date": day,
                "snapshot_hash": f"sha256:deadbeef{day.replace('-', '')}",
                "membership_count": 2,
                "aligned_membership_count": 2,
                "sector_count": 2,
            }
        ),
        encoding="utf-8",
    )


def _revoked_snapshot(root: Path, day: str) -> None:
    _eligible_snapshot(root, day)
    (root / "data" / "sector_history" / day / "revocation.json").write_text(
        '{"status": "revoked"}', encoding="utf-8"
    )


def test_readiness_stays_unanchored_when_no_eligible_day(tmp_path) -> None:
    from quantdesk.data_truth.forward_pit_readiness import build_forward_pit_readiness

    # A revoked day must never become T0.
    _revoked_snapshot(tmp_path, "2026-07-22")
    payload = build_forward_pit_readiness(tmp_path, generated_at=FROZEN_CAPTURE)

    assert payload["t0"] is None
    assert payload["status"] == "blocked_pit_evidence_pending"
    assert payload["current_eligible_forward_days"] == 0
    assert all(m["calendar_date"] is None for m in payload["milestones"])
    assert all(m["status"] == "unanchored_t0_unknown" for m in payload["milestones"])


def test_readiness_anchors_t0_and_computes_trading_day_milestones(tmp_path) -> None:
    from quantdesk.data_truth.forward_pit_readiness import build_forward_pit_readiness
    from quantdesk.market_clock import (
        is_cn_a_share_trading_day,
        load_cn_trading_calendar,
        next_cn_a_share_trading_day,
    )

    # 2026-07-23 is a Thursday; pick the earliest eligible day as T0.
    _eligible_snapshot(tmp_path, "2026-07-23")
    payload = build_forward_pit_readiness(tmp_path, generated_at=FROZEN_CAPTURE)

    cal = load_cn_trading_calendar()
    t0 = date(2026, 7, 23)
    assert payload["t0"] == "2026-07-23"
    assert payload["status"] == "t0_launched"
    assert payload["current_eligible_forward_days"] == 1
    assert payload["t0_is_trading_day"] is bool(is_cn_a_share_trading_day(t0, cal))

    # Milestone dates must be real trading days computed off the calendar.
    by_days = {m["required_consecutive_trading_days"]: m for m in payload["milestones"]}
    assert by_days[1]["calendar_date"] == "2026-07-23"
    # T0+4 sessions == 4 trading days strictly after T0.
    expected = t0
    for _ in range(4):
        expected = next_cn_a_share_trading_day(expected, cal)
    assert by_days[5]["calendar_date"] == expected.isoformat()
    for date_str in (v["calendar_date"] for v in by_days.values()):
        assert is_cn_a_share_trading_day(date.fromisoformat(date_str), cal)

    # Only T0 (1 day) is achieved with a single eligible archive.
    assert by_days[1]["status"] == "achieved"
    assert by_days[5]["status"] == "pending_forward_accumulation"


def test_readiness_earliest_eligible_day_is_t0_not_latest(tmp_path) -> None:
    from quantdesk.data_truth.forward_pit_readiness import build_forward_pit_readiness

    _eligible_snapshot(tmp_path, "2026-07-24")
    _eligible_snapshot(tmp_path, "2026-07-23")
    payload = build_forward_pit_readiness(tmp_path, generated_at=FROZEN_CAPTURE)

    assert payload["t0"] == "2026-07-23"
    assert payload["current_eligible_forward_days"] == 2
    assert [d["date"] for d in payload["eligible_forward_days"]] == [
        "2026-07-23",
        "2026-07-24",
    ]


def test_readiness_write_only_touches_results_root(tmp_path) -> None:
    from quantdesk.data_truth.forward_pit_readiness import write_forward_pit_readiness

    _eligible_snapshot(tmp_path, "2026-07-23")
    out = tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    payload = write_forward_pit_readiness(
        tmp_path, output_path=out, generated_at=FROZEN_CAPTURE
    )
    assert out.is_file()
    assert payload["readiness_path"] == str(out.resolve())
    # No canonical/state artifacts created by the readiness build.
    assert not (tmp_path / "data" / "universe").exists()
    assert not (tmp_path / "state").exists()


def test_chain_readiness_refresh_writes_artifact_in_isolated_root(tmp_path) -> None:
    import quantdesk.daily_data_chain as chain

    # No sector_history yet -> T0 unknown, artifact still written, chain not broken.
    result = chain._refresh_forward_pit_readiness(tmp_path)
    assert result["status"] == "refreshed"
    assert result["t0"] is None
    assert result["current_eligible_forward_days"] == 0
    artifact = tmp_path / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked_pit_evidence_pending"
    assert payload["t0"] is None
    assert payload["boundaries"]["canonical_write"] is False
    # No canonical/state writes: only results/ touched.
    assert not (tmp_path / "data" / "sector_history").exists()


# --------------------------------------------------------------------------- #
# Part D — forward-window replay runner (T0 .. T0+18) milestone contracts       #
# --------------------------------------------------------------------------- #
class _StubResult:
    """Minimal stand-in for AccountReplayResult for offline contract tests."""

    def __init__(self, account_id, candidate_count, stage_counts, fill_count):
        self.account_id = account_id
        self.candidate_count = candidate_count
        self.stage_counts = stage_counts
        self.fill_count = fill_count

    def to_manifest(self):
        return {
            "account_id": self.account_id,
            "candidate_count": self.candidate_count,
            "fill_count": self.fill_count,
            "stage_counts": dict(self.stage_counts),
        }


def _write_readiness(root: Path, *, t0, eligible_days, milestone_dates) -> Path:
    milestones = []
    labels = [
        (1, "T0"),
        (5, "T0+4 trading sessions"),
        (9, "T0+8 trading sessions"),
        (19, "T0+18 trading sessions"),
    ]
    date_by_label = {
        "T0": milestone_dates.get("T0"),
        "T0+4 trading sessions": milestone_dates.get("T0+4"),
        "T0+8 trading sessions": milestone_dates.get("T0+8"),
        "T0+18 trading sessions": milestone_dates.get("T0+18"),
    }
    for required, label in labels:
        milestones.append(
            {
                "required_consecutive_trading_days": required,
                "relative_to_t0": label,
                "calendar_date": date_by_label.get(label),
                "status": "achieved" if t0 else "unanchored_t0_unknown",
            }
        )
    payload = {
        "t0": t0,
        "current_eligible_forward_days": eligible_days,
        "milestones": milestones,
    }
    target = root / "results" / "data_health" / "round7_forward_pit_readiness_latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_detect_t0_readiness_blocked_when_no_t0(tmp_path) -> None:
    from quantdesk.paper.stage4_forward_replay import detect_t0_readiness

    path = _write_readiness(tmp_path, t0=None, eligible_days=0, milestone_dates={})
    status = detect_t0_readiness(path)
    assert status.t0 is None
    assert status.status == "blocked_pit_evidence_pending"
    assert status.ready_for_interim is False
    assert status.ready_for_terminal is False


def test_detect_t0_readiness_interim_and_terminal_gates(tmp_path) -> None:
    from quantdesk.paper.stage4_forward_replay import detect_t0_readiness

    dates = {"T0": "2026-08-03", "T0+4": "2026-08-07", "T0+8": "2026-08-13", "T0+18": "2026-08-27"}
    # 5 eligible days -> interim ready, not terminal
    p1 = _write_readiness(tmp_path, t0="2026-08-03", eligible_days=5, milestone_dates=dates)
    s1 = detect_t0_readiness(p1)
    assert s1.status == "interim_window_ready"
    assert s1.ready_for_interim is True
    assert s1.ready_for_terminal is False
    assert s1.milestone_dates["T0+18"] == "2026-08-27"

    # 19 eligible days -> terminal ready
    p2 = _write_readiness(tmp_path, t0="2026-08-03", eligible_days=19, milestone_dates=dates)
    s2 = detect_t0_readiness(p2)
    assert s2.status == "terminal_window_ready"
    assert s2.ready_for_terminal is True


def test_interim_evidence_pack_counts_without_conclusion() -> None:
    from quantdesk.paper.stage4_forward_replay import interim_evidence_pack

    results = {
        "A": _StubResult("A", candidate_count=40, stage_counts={"gate": 30, "evidence": 6, "fill": 4}, fill_count=4),
        "B": _StubResult("B", candidate_count=12, stage_counts={"gate": 12}, fill_count=0),
    }
    pack = interim_evidence_pack("T0+4", results, as_of="2026-08-07")
    d = pack.to_dict()
    assert d["milestone"] == "T0+4"
    assert d["account_candidate_counts"] == {"A": 40, "B": 12}
    # gate-pass = evidence + no_fill + fill
    assert d["account_gate_pass_counts"] == {"A": 10, "B": 0}
    assert d["account_would_fill_counts"] == {"A": 4, "B": 0}
    assert d["conclusion_drawn"] is False


def test_interim_pack_rejects_terminal_milestone() -> None:
    from quantdesk.paper.stage4_forward_replay import interim_evidence_pack

    with pytest.raises(ValueError, match=r"T0\+4 or T0\+8"):
        interim_evidence_pack("T0+18", {})


def test_terminal_evaluation_insufficient_sample_below_threshold() -> None:
    from quantdesk.paper.stage4_forward_replay import (
        MIN_EFFECTIVE_SAMPLE,
        terminal_evaluation,
    )

    results = [
        _StubResult("A", candidate_count=100, stage_counts={"fill": 5}, fill_count=5),
        _StubResult("B", candidate_count=50, stage_counts={"fill": 2}, fill_count=2),
    ]
    ev = terminal_evaluation(results, effective_sell_sample=7, as_of="2026-08-27")
    d = ev.to_dict()
    assert d["total_candidates"] == 150
    assert d["total_fills"] == 7
    assert d["effective_sell_sample"] == 7
    assert d["min_effective_sample"] == MIN_EFFECTIVE_SAMPLE
    assert d["sample_sufficient"] is False
    assert d["verdict"] == "insufficient_sample"
    assert len(d["account_manifests"]) == 2


def test_terminal_evaluation_eligible_when_sample_sufficient() -> None:
    from quantdesk.paper.stage4_forward_replay import terminal_evaluation

    results = [_StubResult("A", candidate_count=500, stage_counts={"fill": 40}, fill_count=40)]
    ev = terminal_evaluation(results, effective_sell_sample=35)
    assert ev.sample_sufficient is True
    assert ev.verdict == "return_conclusion_eligible"


def test_run_forward_replay_waits_until_terminal(tmp_path) -> None:
    from quantdesk.paper.stage4_forward_replay import run_forward_replay_if_ready

    dates = {"T0": "2026-08-03", "T0+4": "2026-08-07", "T0+8": "2026-08-13", "T0+18": "2026-08-27"}
    # T0 anchored but only 3 forward days -> still accumulating, runs nothing.
    p1 = _write_readiness(tmp_path, t0="2026-08-03", eligible_days=3, milestone_dates=dates)
    out1 = run_forward_replay_if_ready(readiness_path=p1)
    assert out1["ran"] is False
    assert out1["reason"] == "t0_anchored_accumulating"

    # Terminal ready + injected replay -> runs and evaluates honestly.
    p2 = _write_readiness(tmp_path, t0="2026-08-03", eligible_days=19, milestone_dates=dates)

    def fake_replay(status):
        return [_StubResult("A", candidate_count=300, stage_counts={"fill": 10}, fill_count=10)]

    out2 = run_forward_replay_if_ready(
        readiness_path=p2,
        replay_fn=fake_replay,
        effective_sample_fn=lambda results: 12,
    )
    assert out2["ran"] is True
    assert out2["evaluation"]["verdict"] == "insufficient_sample"
    assert out2["evaluation"]["window_start"] == "2026-08-03"
    assert out2["evaluation"]["window_end"] == "2026-08-27"
