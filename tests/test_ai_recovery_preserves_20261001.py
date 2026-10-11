"""D2/T15：重启/同周期重入时保留已提交意图、原落盘时间与原模型异常。

沿用 DS12 harness（`test_ai_recovery_closeout_20260912` 的合成夹具），全部离线：
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
ORIGINAL_RECORDED_AT = f"{AS_OF}T20:20:00+08:00"
COMMITTED_INTENT = {
    "symbol": "600500",
    "side": "buy",
    "ref_close": 5.0,
    "reason": "底仓观察，等回调",
    "style": "波段",
    "planned_hold_days": 1,
}
ORIGINAL_DIAG = {"reason": "model_timeout", "elapsed_seconds": 297.7, "attempts": 1}


def _prior_with_committed_intent() -> dict:
    plan = _prior_cycle_plan(
        {
            "used": False,
            "model": "",
            "unavailable_reason": "model_timeout",
            "diagnostics": dict(ORIGINAL_DIAG),
        }
    )
    plan["intents"] = [dict(COMMITTED_INTENT)]
    plan["recorded_at"] = ORIGINAL_RECORDED_AT
    plan["statement_rejections"] = []
    return plan


def _reenter(tmp_path: Path, monkeypatch, prior: dict) -> tuple[dict, dict, dict]:
    root = _seed_root(tmp_path, plan=prior, cash=100000.0, position=False)
    runtime = _cycle_paths(root)
    ledger_before = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    _stub_evening(monkeypatch, overnight.settle_plan)

    def must_not_call(*args, **kwargs):
        raise AssertionError("恢复已提交周期时不得再调模型")

    monkeypatch.setattr(overnight, "propose_picks", must_not_call)
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    return snapshot, ledger_before, json.loads(runtime.ledger.read_text(encoding="utf-8"))


def test_reentry_keeps_the_committed_intent(tmp_path: Path, monkeypatch) -> None:
    """重启/重入后，当晚已提交的意图不能被规则重算顶掉。"""

    snapshot, ledger_before, ledger_after = _reenter(tmp_path, monkeypatch, _prior_with_committed_intent())

    intents = list(snapshot["plan"].get("intents") or [])
    assert intents, "已提交的意图不得消失"
    assert any(
        str(row.get("symbol")) == COMMITTED_INTENT["symbol"]
        and str(row.get("side")) == COMMITTED_INTENT["side"]
        and str(row.get("reason")) == COMMITTED_INTENT["reason"]
        for row in intents
    ), intents
    assert ledger_after["cash"] == ledger_before["cash"], "重入不得动资金"
    assert ledger_after["trades"] == ledger_before["trades"], "重入不得记成交"


def test_reentry_keeps_the_original_recorded_time(tmp_path: Path, monkeypatch) -> None:
    """原落盘时间不得被重入那一刻的时钟覆盖。"""

    snapshot, _, _ = _reenter(tmp_path, monkeypatch, _prior_with_committed_intent())

    assert snapshot["plan"]["recorded_at"] == ORIGINAL_RECORDED_AT, snapshot["plan"]["recorded_at"]


def test_preserved_cycle_with_a_used_brain_still_keeps_its_facts(tmp_path: Path, monkeypatch) -> None:
    """保留态 + 原计划记着"大脑用过"时，日记与记录仍走同一份原事实（不崩、不重写）。"""

    prior = _prior_with_committed_intent()
    prior["llm"] = {
        "used": True,
        "model": "fixture-model",
        "market_note": "盘面偏弱",
        "picked": [COMMITTED_INTENT["symbol"]],
        "unavailable_reason": "",
        "diagnostics": {},
    }
    prior["statement_rejections"] = [
        {
            "symbol": "600613",
            "side": "buy",
            "label": "陈述与数据不符",
            "hits": [{"kind": "claimed_pct_untagged", "detail": "个别票暴涨(+40%)", "label": "陈述与数据不符"}],
            "original_reason": "个别票暴涨(+40%)",
            "recorded_at": ORIGINAL_RECORDED_AT,
            "seen_at_decision": {"as_of": AS_OF, "close": 5.1},
        }
    ]

    snapshot, ledger_before, ledger_after = _reenter(tmp_path, monkeypatch, prior)
    plan = snapshot["plan"]

    assert plan["llm"]["used"] is True, "原计划的模型参与事实保持"
    assert plan["llm"]["recovery_status"] == "missing_original_model_output"
    assert plan["recorded_at"] == ORIGINAL_RECORDED_AT
    assert [str(row.get("symbol")) for row in plan["intents"]] == [COMMITTED_INTENT["symbol"]]
    assert [str(row.get("symbol")) for row in plan["statement_rejections"]] == ["600613"]
    assert ledger_after["cash"] == ledger_before["cash"], "恢复不得改现金"
    assert ledger_after["trades"] == ledger_before["trades"], "恢复不得记成交"
    assert ledger_after["positions"] == ledger_before["positions"], "恢复不得改持仓"


def test_reusable_reply_keeps_the_original_recorded_times(tmp_path: Path, monkeypatch) -> None:
    """复用原回复的恢复，同样不得把原时刻换成这次重入的时钟（T14/T15）。"""

    raw_text = json.dumps({"buys": [], "sells": [], "stance": {}, "lesson": "", "market_note": ""})
    prior = _prior_with_committed_intent()
    prior["llm"] = {
        "used": True,
        "model": "fixture-model",
        "market_note": "",
        "unavailable_reason": "",
        "diagnostics": {},
        "raw_response": {
            "text": raw_text,
            "model": "fixture-model",
            "source": "llm",
            "created_at": ORIGINAL_RECORDED_AT,
        },
    }
    prior["intents"][0]["recorded_at"] = ORIGINAL_RECORDED_AT

    root = _seed_root(tmp_path, plan=prior, cash=100000.0, position=False)
    runtime = _cycle_paths(root)
    _stub_evening(monkeypatch, overnight.settle_plan, stub_model=False)

    import ai_trader.llm as llm_module

    def no_network(*args, **kwargs):
        raise AssertionError("复用已存回复时不得联网")

    monkeypatch.setattr(llm_module, "_call", no_network)

    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    plan = snapshot["plan"]

    assert plan["llm"]["recovery_status"] == "reusable_original_output"
    assert plan["recorded_at"] == ORIGINAL_RECORDED_AT, "计划原时刻不得被重入时钟覆盖"
    assert [row["recorded_at"] for row in plan["intents"]] == [ORIGINAL_RECORDED_AT], (
        "已提交意图的原时刻同样不得被覆盖"
    )


def test_reentry_keeps_the_original_model_error(tmp_path: Path, monkeypatch) -> None:
    """原模型超时/异常原因必须保留，不能重启后变成空白或换一个说法。"""

    snapshot, _, _ = _reenter(tmp_path, monkeypatch, _prior_with_committed_intent())
    llm = snapshot["plan"]["llm"]

    assert llm["recovery_status"] == "missing_original_model_output"
    assert llm["used"] is False
    assert llm.get("unavailable_reason") == "model_timeout", llm
    assert (llm.get("diagnostics") or {}).get("reason") == "model_timeout", llm
    assert (llm.get("diagnostics") or {}).get("elapsed_seconds") == 297.7, llm
