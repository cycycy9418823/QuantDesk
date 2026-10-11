"""D3/T22：页面显示的模型必须来自真实记录，并与配置/历史分别呈现。

全部合成夹具：只写临时 state/results，不碰正式目录、无网络。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import dashboard


def _payload(tmp_path: Path, *, plan=None, journal=None, cycle=None) -> dict:
    state = tmp_path / "state" / "ai_trader"
    results = tmp_path / "results" / "ai_trader"
    state.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    if plan is not None:
        (state / "plan_latest.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    if journal is not None:
        (state / "journal_latest.json").write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
    if cycle is not None:
        (results / "cycle_latest.json").write_text(json.dumps(cycle, ensure_ascii=False), encoding="utf-8")
    return dashboard.build_dashboard_payload(state_dir=state, results_dir=results)


def test_actual_model_comes_from_the_plan_record(tmp_path: Path) -> None:
    payload = _payload(tmp_path, plan={"llm": {"used": True, "model": "model-A", "market_note": "观望"}})

    llm = payload["llm"]
    assert llm["model"] == "model-A"
    assert llm["model_source"] == "plan_latest"
    assert llm["mode"] == "model"


def test_same_run_history_is_used_and_older_run_is_not(tmp_path: Path) -> None:
    """回看历史只在**同一次判断**（plan_id/日期对得上）时发生；旧记录不得混进来。"""

    # 同一次：日记与计划同 plan_id/同日期 → 可以回看
    same = _payload(
        tmp_path / "same",
        plan={"plan_id": "current-plan", "decision_date": "2026-10-02", "llm": {}},
        journal={"plan_id": "current-plan", "as_of": "2026-10-02", "llm": {"used": True, "model": "model-B"}},
    )
    assert same["llm"]["model"] == "model-B" and same["llm"]["model_source"] == "journal_latest"
    assert same["llm"]["used"] is True and same["llm"]["mode"] == "model"

    # 旧记录：日期/plan_id 都对不上 → 不得借它的模型
    older = _payload(
        tmp_path / "older",
        plan={"plan_id": "current-plan", "decision_date": "2026-10-02", "llm": {}},
        journal={"plan_id": "old-plan", "as_of": "2026-10-01", "llm": {"used": True, "model": "old-model-A"}},
    )
    assert older["llm"]["model"] == "" and older["llm"]["used"] is False, older["llm"]


def test_rule_fallback_is_labelled_and_not_dressed_up_as_the_model(tmp_path: Path) -> None:
    """备用规则（AI 未参与）必须显式标注，且不得把配置里的模型当成"当次实际模型"。"""

    payload = _payload(
        tmp_path,
        plan={"llm": {"used": False, "model": "", "unavailable_reason": "model_timeout"}},
    )

    llm = payload["llm"]
    assert llm["mode"] == "rule_fallback", llm
    assert llm["model"] == "", "当次没有模型参与就不能显示任何模型名"
    assert llm["unavailable_reason"] == "model_timeout"
    # 配置档位单独一个键，不与"当次实际模型"混在一起
    assert "model" in payload["llm_runtime"]
    assert payload["llm_runtime"]["model"] != llm["model"] or payload["llm_runtime"]["model"] == ""


def test_no_records_means_unknown_not_a_guessed_model(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    llm = payload["llm"]
    assert llm["model"] == "" and llm["model_source"] == ""
    assert llm["mode"] == "unknown", llm