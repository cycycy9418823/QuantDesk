"""D2/T21：到期缺证的判断，补证后可重评；但原预测/原判定/原编号一字不改。

全部合成夹具：无网络、无模型调用、不写正式 data/state/results。
"""

from __future__ import annotations

from pathlib import Path

from ai_trader import decision_log as dl

SYMBOL = "600000"
DECISION_DAY = "2026-09-04"
TRADE_DAY = "2026-09-07"
DUE_DAY = "2026-09-08"
HEADER = "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"


def _write_bars(root: Path, days_and_closes: list[tuple[str, float]]) -> Path:
    daily = root / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    rows = [HEADER] + [
        f"{day},{SYMBOL},{close},{close},{close},{close},1000,10000,0,raw"
        for day, close in days_and_closes
    ]
    (daily / f"{SYMBOL}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return daily


def _record() -> dict:
    plan = {
        "decision_date": DECISION_DAY,
        "trade_date": TRADE_DAY,
        "committed": True,
        "recorded_at": f"{DECISION_DAY}T20:20:00+08:00",
        "llm": {"used": True, "model": "synthetic-fixture-no-model-call", "market_note": "观望"},
    }
    intent = {
        "symbol": SYMBOL,
        "side": "buy",
        "decision_origin": "model",
        "ref_close": 10.0,
        "reason": "站上10.4就拿两天",
        "style": "波段",
        "planned_hold_days": 2,
        "verification": {"kind": "stand_above", "n_sessions": 2, "level": 10.4, "price_basis": "raw", "due_date": DUE_DAY},
    }
    return dl.build_decision_record(intent, plan=plan, daily_dir=Path("no-such-dir"), backfill=False)


def test_missing_evidence_is_a_gap_not_a_score(tmp_path: Path) -> None:
    """① 缺证：标成"到期·证据不足"，未知既不算成功也不算失败。"""

    daily = _write_bars(tmp_path, [(DECISION_DAY, 10.0)])  # 窗口内没有K线
    row = dl.score_decision(_record(), daily_dir=daily, as_of=DUE_DAY)

    assert row["result"] == dl.RESULT_EXPIRED_NO_EVIDENCE, row["result"]
    assert row["result_evidence"], "缺证要留下证据说明"

    summary = dl.decision_review_summary([row], as_of=DUE_DAY)
    group = summary["groups"]["model"]
    assert group["due_evidence_gap"] == 1, group
    assert group["mature_evaluable"] == 0, "未知不得进入可评样本"
    assert group["accuracy"] is None, "未知不得算成绩"


def test_backfilled_evidence_is_re_evaluated_without_rewriting_the_original(tmp_path: Path) -> None:
    """② 补证后重评：新增关联评价，原预测/原判定/原编号一字不改，且重复重评不叠加。"""

    daily = _write_bars(tmp_path, [(DECISION_DAY, 10.0)])
    original = dl.score_decision(_record(), daily_dir=daily, as_of=DUE_DAY)
    frozen = {key: original.get(key) for key in
              ("decision_id", "judgment", "verification", "recorded_at", "result", "seen", "result_evidence")}

    # 资料补齐：窗口内两根K线都站上 10.4
    _write_bars(tmp_path, [(DECISION_DAY, 10.0), (TRADE_DAY, 10.8), (DUE_DAY, 10.9)])
    rechecked = dl.score_decision(original, daily_dir=daily, as_of=DUE_DAY)

    for key, value in frozen.items():
        assert rechecked.get(key) == value, f"{key} 不得被重评改写"

    recheck = rechecked.get("recheck")
    assert isinstance(recheck, dict), "补证后必须留下新的关联评价"
    assert recheck.get("result") == dl.RESULT_RIGHT, recheck
    assert recheck.get("as_of") == DUE_DAY
    assert recheck.get("observed"), recheck

    again = dl.score_decision(rechecked, daily_dir=daily, as_of=DUE_DAY)
    assert again.get("recheck") == recheck, "重复重评必须幂等"

    summary = dl.decision_review_summary([again], as_of=DUE_DAY)
    group = summary["groups"]["model"]
    assert group["rechecked"] == 1, group
    assert group["rechecked_correct"] == 1, group
    assert group["mature_evaluable"] == 0, "重评不得冒充原观察期样本"


def test_still_missing_after_recheck_is_not_scored(tmp_path: Path) -> None:
    """③ 补证后仍缺：重评状态是"未核验"，不写成功也不写失败。"""

    daily = _write_bars(tmp_path, [(DECISION_DAY, 10.0)])
    original = dl.score_decision(_record(), daily_dir=daily, as_of=DUE_DAY)

    # 只补了窗口第一根、且没达标：仍不足以判定
    _write_bars(tmp_path, [(DECISION_DAY, 10.0), (TRADE_DAY, 9.5)])
    rechecked = dl.score_decision(original, daily_dir=daily, as_of=DUE_DAY)

    recheck = rechecked.get("recheck") or {}
    assert recheck.get("result") is None, recheck
    assert recheck.get("evidence_status") == "unverified", recheck

    summary = dl.decision_review_summary([rechecked], as_of=DUE_DAY)
    group = summary["groups"]["model"]
    assert group["rechecked"] == 0 and group["due_evidence_gap"] == 1, group
