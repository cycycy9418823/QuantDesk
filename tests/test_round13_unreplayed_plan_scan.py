"""Round 13 收尾：漏放归档计划的只读扫描契约。

问题背景（磁盘实测，2026-08-06）：条件计划的退出规则只在晚间例行**实际运行**的
那天被评估，而止损带每次都用滚动 support 重算。2026-05-14 至 08-05 的 59 个交易日
里例行只跑了 17 天；漏跑日触发的退出信号全部被静默丢弃——等下次运行时，带子已随
价格下移、不再匹配。结果是 26 次执行只产生 1 笔平仓。

**没有任何东西检测这些漏放。** 本扫描把它变成可见、可计数的事实。

红线：扫描只读。它不补放、不写盘、不伪造重放证据——漏放日的退出信号已不可追溯，
补放必须是单独授权的动作。一个会自动补放历史的扫描器，正是本项目禁止的"发明证据"。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk.daily_candidate_refresh import scan_unreplayed_archived_plans


def _archive(root: Path, trade_date: str, plan_id: str, *, symbols: int = 2) -> None:
    path = root / "results" / "conditional_trade_plans_daily" / f"{trade_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "plan_id": plan_id,
                "plan_date": "2026-08-04",
                "trade_date": trade_date,
                "symbols": [{"symbol": f"60000{i}"} for i in range(symbols)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_passed_trade_date_without_replay_is_reported(tmp_path: Path) -> None:
    """交易日已过、账本无对应 execution_id → 必须报出来。"""

    _archive(tmp_path, "2026-08-03", "plan_20260731_for_20260803")

    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[]
    )

    assert result["status"] == "attention"
    assert result["unreplayed_count"] == 1
    row = result["unreplayed"][0]
    assert row["plan_id"] == "plan_20260731_for_20260803"
    assert row["trade_date"] == "2026-08-03"
    assert row["execution_id"] == "plan_20260731_for_20260803:2026-08-03"
    assert row["reason_code"] == "trade_date_passed_without_recorded_replay"
    assert "unreplayed_archived_plans_present" in result["reason_codes"]


def test_recorded_replay_is_not_flagged(tmp_path: Path) -> None:
    _archive(tmp_path, "2026-08-03", "plan_20260731_for_20260803")

    result = scan_unreplayed_archived_plans(
        tmp_path,
        as_of="2026-08-06",
        applied_execution_ids=["plan_20260731_for_20260803:2026-08-03"],
    )

    assert result["status"] == "passed"
    assert result["unreplayed_count"] == 0
    assert result["reason_codes"] == []


@pytest.mark.parametrize("trade_date", ["2026-08-06", "2026-08-07"])
def test_today_and_future_plans_are_not_yet_due(tmp_path: Path, trade_date: str) -> None:
    """当日与未来计划不算漏放——它们还没到期。"""

    _archive(tmp_path, trade_date, f"plan_x_for_{trade_date.replace('-', '')}")

    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[]
    )

    assert result["unreplayed_count"] == 0
    assert result["status"] == "passed"


def test_multiple_skipped_days_are_all_counted(tmp_path: Path) -> None:
    """连续漏放必须逐条报出，而不是只报最近一条。"""

    _archive(tmp_path, "2026-07-28", "plan_a")
    _archive(tmp_path, "2026-07-30", "plan_b")
    _archive(tmp_path, "2026-08-03", "plan_c")

    result = scan_unreplayed_archived_plans(
        tmp_path,
        as_of="2026-08-06",
        applied_execution_ids=["plan_b:2026-07-30"],
    )

    assert result["unreplayed_count"] == 2
    assert {row["plan_id"] for row in result["unreplayed"]} == {"plan_a", "plan_c"}


def test_unreadable_archive_entry_is_surfaced_not_skipped(tmp_path: Path) -> None:
    """损坏的归档不能被静默跳过——那会把漏放藏起来。"""

    bad = tmp_path / "results" / "conditional_trade_plans_daily" / "2026-08-03.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not-json", encoding="utf-8")

    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[]
    )

    assert result["status"] == "attention"
    assert result["archive_unreadable"] == ["2026-08-03.json"]
    assert "archive_entries_unreadable" in result["reason_codes"]


def test_scan_writes_nothing_and_never_claims_recovery(tmp_path: Path) -> None:
    """只读证明：扫描前后目录快照一致，且不自称已授权补放。"""

    _archive(tmp_path, "2026-08-03", "plan_20260731_for_20260803")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[]
    )

    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after, "扫描必须零写入"
    assert result["recovery_authorized"] is False
    assert result["boundaries"]["state_write"] is False
    assert result["boundaries"]["broker_route"] is False


def test_missing_archive_directory_is_passed_not_crash(tmp_path: Path) -> None:
    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[]
    )

    assert result["status"] == "passed"
    assert result["archive_scanned"] == 0
    assert result["unreplayed_count"] == 0


def test_invalid_as_of_fails_closed(tmp_path: Path) -> None:
    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="not-a-date", applied_execution_ids=[]
    )

    assert result["status"] == "blocked"
    assert "scan_as_of_invalid" in result["reason_codes"]


def test_plan_without_id_is_reported_with_its_own_reason(tmp_path: Path) -> None:
    """缺 plan_id 无法构成 execution_id，必须单独归因而非当作已放。"""

    path = tmp_path / "results" / "conditional_trade_plans_daily" / "2026-08-03.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ok": True, "trade_date": "2026-08-03", "symbols": []}),
        encoding="utf-8",
    )

    result = scan_unreplayed_archived_plans(
        tmp_path, as_of="2026-08-06", applied_execution_ids=[]
    )

    assert result["unreplayed_count"] == 1
    assert result["unreplayed"][0]["reason_code"] == "plan_id_or_trade_date_missing"
