"""D2/T19：同日重入不重复记账；latest、历史、成交计数引用同一事实。

沿用 DS12 harness（真实 `run_evening` + 合成 K 线/账本），全程离线：
无网络、无模型调用、不写正式 data/state/results。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import overnight

from test_ai_recovery_closeout_20260912 import (  # noqa: E402
    _cycle_paths,
    _prior_cycle_plan,
    _seed_root,
    _stub_evening,
)

AS_OF = "2026-09-11"
SYMBOL = "600500"


def _fillable_prior() -> dict:
    """一份 trade_date 就在 as_of 当天的已提交计划（会在当晚结算出 1 笔成交）。"""

    plan = _prior_cycle_plan({"used": False, "unavailable_reason": "not_enabled_or_unconfigured"})
    plan.update(
        {
            "decision_date": "2026-09-10",
            "trade_date": AS_OF,
            "status": "pending_open",
            "settled_on": "",
            "recorded_at": "2026-09-10T20:20:00+08:00",
        }
    )
    plan["intents"] = [
        {
            "symbol": SYMBOL,
            "side": "buy",
            "quantity": 2500,
            "ref_close": 5.0,
            "predicted_direction": "up",
            "preset_touch_price": 5.2,
            "reason": "底仓观察",
            "style": "波段",
            "planned_hold_days": 1,
        }
    ]
    return plan


def _decision_rows(runtime) -> list[dict]:
    if not runtime.decision_log.is_file():
        return []
    return [
        json.loads(line)
        for line in runtime.decision_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _journal_rows(runtime) -> list[dict]:
    if not runtime.journal_log.is_file():
        return []
    return [
        json.loads(line)
        for line in runtime.journal_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _ledger(runtime) -> dict:
    return json.loads(runtime.ledger.read_text(encoding="utf-8"))


def test_same_day_rerun_keeps_one_fact_across_latest_history_and_fills(tmp_path: Path, monkeypatch) -> None:
    """同日跑两次：成交只记 1 笔，latest 与历史的条数、成交计数互相一致。"""

    root = _seed_root(tmp_path, plan=_fillable_prior(), cash=100000.0, position=False)
    runtime = _cycle_paths(root)
    _stub_evening(monkeypatch, overnight.settle_plan)

    overnight.run_evening(paths=runtime, network_news=False)
    first_ledger, first_rows = _ledger(runtime), _decision_rows(runtime)
    first_latest = json.loads(runtime.decision_latest.read_text(encoding="utf-8"))

    overnight.run_evening(paths=runtime, network_news=False)
    second_ledger, second_rows = _ledger(runtime), _decision_rows(runtime)
    second_latest = json.loads(runtime.decision_latest.read_text(encoding="utf-8"))

    assert len(first_ledger["trades"]) == 1, first_ledger["trades"]
    assert len(second_ledger["trades"]) == 1, "同日重跑不得再记一笔成交"
    assert second_ledger["cash"] == first_ledger["cash"], "同日重跑不得再扣一次钱"
    assert len(second_rows) == len(first_rows), "同日重跑不得多记历史记录"

    ids = [str(row.get("decision_id")) for row in second_rows]
    assert len(ids) == len(set(ids)), "decision_id 不得重复"

    for latest in (first_latest, second_latest):
        assert len(latest.get("records") or []) == len(second_rows), (
            "latest 与历史必须引用同一批记录"
        )

    same_day = [row for row in _journal_rows(runtime) if str(row.get("as_of")) == AS_OF]
    assert len(same_day) == 1, "同日重跑不得追加第二条日记"

    # 最新视图必须与历史、账本引用同一事实：同一天的成交数不能被重入重算成 0。
    latest_note = json.loads(runtime.journal_latest.read_text(encoding="utf-8"))
    snapshot_payload = json.loads(runtime.snapshot.read_text(encoding="utf-8"))
    assert int(latest_note.get("filled_count") or 0) == 1, latest_note.get("filled_count")
    assert int(snapshot_payload["journal"].get("filled_count") or 0) == 1
    assert same_day[0].get("filled_count") == latest_note.get("filled_count") == 1
    assert len(latest_note.get("actions") or []) == len(same_day[0].get("actions") or [])

    fills = [t for t in second_ledger["trades"] if str(t.get("symbol")) == SYMBOL]
    assert len(fills) == 1 and float(fills[0].get("quantity") or 0) == 2500.0, fills


def test_save_failure_then_retry_does_not_duplicate_fill_or_journal(tmp_path: Path, monkeypatch) -> None:
    """保存失败后重试：成交/资金/日记都只算一次。"""

    root = _seed_root(tmp_path, plan=_fillable_prior(), cash=100000.0, position=False)
    runtime = _cycle_paths(root)
    _stub_evening(monkeypatch, overnight.settle_plan)

    real_save_ledger = overnight.save_ledger
    state = {"failed": False}

    def flaky_save_ledger(ledger, path):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("injected_save_ledger_failure")
        return real_save_ledger(ledger, path)

    monkeypatch.setattr(overnight, "save_ledger", flaky_save_ledger)
    try:
        overnight.run_evening(paths=runtime, network_news=False)
    except RuntimeError as exc:
        assert "injected_save_ledger_failure" in str(exc)
    else:
        raise AssertionError("注入的保存失败必须显式抛出，不得被吞掉")

    monkeypatch.setattr(overnight, "save_ledger", real_save_ledger)
    overnight.run_evening(paths=runtime, network_news=False)

    ledger = _ledger(runtime)
    assert len(ledger["trades"]) == 1, "重试不得补记第二笔成交"
    same_day = [row for row in _journal_rows(runtime) if str(row.get("as_of")) == AS_OF]
    assert len(same_day) == 1, "重试不得追加第二条日记"

    rows = _decision_rows(runtime)
    ids = [str(row.get("decision_id")) for row in rows]
    assert len(ids) == len(set(ids)), "重试不得多记历史记录"
    latest = json.loads(runtime.decision_latest.read_text(encoding="utf-8"))
    assert len(latest.get("records") or []) == len(rows)
