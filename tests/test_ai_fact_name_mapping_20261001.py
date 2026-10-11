"""D2/T11：名称映射接真实链；未核验保持未核验标注；真实矛盾仍拒绝。

全部为合成临时夹具：无网络、无模型调用、不写正式账本与正式清单。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from ai_trader import decision_log as dl

DAY = "2026-09-28"
PREV = "2026-09-25"
SYMBOL = "600000"
OTHER = "600001"
SPARE_SYMBOL = "600002"
NAME = "浦发银行"
SPARE_NAME = "招商银行"
DAILY_HEADER = "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"


def write_listing(root: Path, rows: list[tuple[str, str]]) -> None:
    path = root / "data" / "metadata" / "security_master.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["symbol,name,list_date"] + [f"{symbol},{name},2020-01-01" for symbol, name in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_daily(root: Path, symbol: str = SYMBOL) -> None:
    daily = root / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    rows = [
        DAILY_HEADER,
        f"{PREV},{symbol},10,10.1,9.9,10,1000,10000,0,raw",
        f"{DAY},{symbol},10,10.0,10.0,10,1000,10000,0,raw",
    ]
    (daily / f"{symbol}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def plan_with(reason: str, *, symbol: str = SYMBOL, market_note: str = "观望") -> dict:
    return {
        "decision_date": DAY,
        "trade_date": DAY,
        "committed": True,
        "recorded_at": f"{DAY}T20:20:00+08:00",
        "llm": {"used": True, "model": "synthetic-fixture-no-model-call", "market_note": market_note},
        "intents": [
            {"symbol": symbol, "side": "buy", "ref_close": 10.0, "reason": reason,
             "style": "波段", "planned_hold_days": 1}
        ],
    }


def gate_with(root: Path, plan: dict, name_map: dict[str, str] | None = None):
    """每次用**新计划副本**跑门：apply_gate_to_plan 会把 plan["intents"] 换成通过项。"""

    mapping = dl.load_name_to_symbol(root=root) if name_map is None else name_map
    return dl.apply_gate_to_plan(
        deepcopy(plan), daily_dir=root / "data" / "daily", name_to_symbol=mapping
    )


def test_name_map_reads_runtime_listing_and_refuses_ambiguous_names(tmp_path: Path) -> None:
    """映射只从运行时清单读；同名多代码不入表（宁可缺证，也不替它挑票）。"""

    write_listing(tmp_path, [(SYMBOL, NAME), (OTHER, NAME), (SPARE_SYMBOL, SPARE_NAME)])

    mapping = dl.load_name_to_symbol(root=tmp_path)

    assert mapping.get(SPARE_NAME) == SPARE_SYMBOL
    assert NAME not in mapping, "重名必须保持缺证，不能猜一只"
    assert dl.load_name_to_symbol(root=tmp_path / "no-such-root") == {}


def test_name_map_binds_a_chinese_subject_through_the_real_chain(tmp_path: Path) -> None:
    """名称映射接真实链的证据：同一句话，有清单时能绑到 600000，没有清单时绑不到。"""

    write_daily(tmp_path)  # 600000：收盘 10
    write_daily(tmp_path, symbol=OTHER)  # 提议标的自己的日线（收盘 10）
    write_listing(tmp_path, [(SYMBOL, NAME)])  # 运行时清单：浦发银行 → 600000
    plan = plan_with(f"{NAME}今天收盘价99元", symbol=OTHER)

    with_map = gate_with(tmp_path, plan)
    assert with_map.rejections, "有清单时中文名能绑到 600000，收盘 99 与实际 10 是真实矛盾"
    hits = with_map.rejections[0]["statement_rejections"]
    assert any(hit.get("symbol") == SYMBOL and hit.get("evidence_status") != "unverified"
               for hit in hits), hits

    without_map = gate_with(tmp_path, plan, name_map={})
    hits_without = without_map.rejections[0]["statement_rejections"]
    assert not any(hit.get("symbol") == SYMBOL for hit in hits_without), (
        "没有清单时链上无从知道这句话指 600000——这正是名称映射要补的缺口"
    )


def test_unverified_stays_labelled_unverified_and_the_record_says_so(tmp_path: Path) -> None:
    """未知保持未核验：命中标 unverified、状态写"未核验"，记录里也分层留下。"""

    write_daily(tmp_path)

    plan = plan_with("近期涨20%")
    plan_copy = deepcopy(plan)
    gate = dl.apply_gate_to_plan(
        plan_copy,
        daily_dir=tmp_path / "data" / "daily",
        name_to_symbol=dl.load_name_to_symbol(root=tmp_path),
    )

    assert gate.rejections, "核对不上的话按既有语义仍不放行"
    hit = gate.rejections[0]["statement_rejections"][0]
    assert hit["evidence_status"] == "unverified"
    assert hit["kind"] == "claimed_pct_ambiguous_period"
    assert plan_copy["statement_review"]["status"] == "unverified", plan_copy["statement_review"]

    records = dl.records_from_plan(
        {**plan_copy, "intents": []},
        daily_dir=tmp_path / "data" / "daily",
        rejections=gate.rejections,
    )
    row = next(item for item in records if item.get("symbol") == SYMBOL)
    assert row["statement_unverified"], "记录必须能说清这是未核验"
    assert row["statement_unverified"][0]["evidence_status"] == "unverified"


def test_real_mismatch_is_still_rejected_even_beside_an_unknown(tmp_path: Path) -> None:
    """同一条记录里既有真实矛盾又有未知：仍须拒绝，未知不能把矛盾盖掉。"""

    write_daily(tmp_path)

    gate = gate_with(tmp_path, plan_with("今天收盘价99元", market_note=f"{NAME}涨99%"))

    assert gate.rejections, "真实矛盾在场就必须拒绝"
    hits = gate.rejections[0]["statement_rejections"]
    statuses = {hit.get("evidence_status") for hit in hits}
    assert "unverified" in statuses and len(statuses) > 1, statuses
    assert any(hit.get("evidence_status") != "unverified" for hit in hits), hits


def test_unresolved_name_cannot_be_verified_against_the_proposed_stock(tmp_path: Path) -> None:
    """P1：清单缺这条/重名时，"浦发银行今天收盘价10元"不能说成已核对。

    10 元正好等于提议标的自己的收盘价——不修就会退回提议股票核成 accepted。
    """

    write_daily(tmp_path, symbol=OTHER)  # 提议标的 600001：收盘 10
    plan = plan_with(f"{NAME}今天收盘价10元", symbol=OTHER)

    for mapping in ({}, dl.load_name_to_symbol(root=tmp_path)):
        gated_plan = deepcopy(plan)
        gate = dl.apply_gate_to_plan(
            gated_plan, daily_dir=tmp_path / "data" / "daily", name_to_symbol=mapping
        )
        assert gated_plan["statement_review"]["status"] == "unverified", gate.rejections
        assert not gate.accepted_intents, "主体缺证不得放行"
        hit = gate.rejections[0]["statement_rejections"][0]
        assert hit["kind"] == "claimed_subject_unresolved" and hit["evidence_status"] == "unverified"
        assert hit.get("subject_text") == NAME
        assert "symbol" not in hit or not hit.get("symbol"), "没绑定就不能替它挑一只票"


def test_pure_unverified_record_is_not_called_a_mismatch(tmp_path: Path) -> None:
    """P2：纯未核验的记录，主结论是"未核验"——安全拒收，但不写成已证实矛盾。"""

    write_daily(tmp_path)

    plan = plan_with("近期涨20%")
    plan_copy = deepcopy(plan)
    gate = dl.apply_gate_to_plan(
        plan_copy, daily_dir=tmp_path / "data" / "daily", name_to_symbol={}
    )
    assert plan_copy["statement_review"]["status"] == "unverified"

    records = dl.records_from_plan(
        {**plan_copy, "intents": []}, daily_dir=tmp_path / "data" / "daily", rejections=gate.rejections
    )
    row = next(item for item in records if item.get("symbol") == SYMBOL)

    assert row["statement_unverified"], "未核验命中必须留在记录里"
    assert row["result"] == dl.RESULT_UNVERIFIED, row["result"]
    assert row["result"] != dl.RESULT_MISMATCH
    assert row["verification"]["text"] == dl.RESULT_UNVERIFIED
    assert row["verification"]["unverified_only"] is True
    assert row["committed"] is False, "标成未核验不等于放行"


def test_unlisted_name_without_any_suffix_cannot_borrow_the_proposed_stock(tmp_path: Path) -> None:
    """通用主体识别：不在任何后缀表里的中文名（"贵州茅台"）同样不能借提议股票核验。"""

    write_daily(tmp_path, symbol=OTHER)  # 提议标的 600001：收盘 10（与句中所说同价）
    plan = plan_with("贵州茅台今天收盘价10元", symbol=OTHER)

    gated_plan = deepcopy(plan)
    gate = dl.apply_gate_to_plan(
        gated_plan,
        daily_dir=tmp_path / "data" / "daily",
        name_to_symbol=dl.load_name_to_symbol(root=tmp_path),
    )
    assert gated_plan["statement_review"]["status"] == "unverified", gate.rejections
    assert not gate.accepted_intents, "主体缺证不得放行"
    hit = gate.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "claimed_subject_unresolved"
    assert hit["subject_text"] == "贵州茅台"


def test_name_with_letter_suffix_is_also_treated_as_a_subject(tmp_path: Path) -> None:
    """「万科A」这类带字母的简称同样算明确主体，不能借提议股票核验。"""

    write_daily(tmp_path, symbol=OTHER)  # 提议标的 600001：收盘 10（与句中所说同价）
    plan = plan_with("万科A今天收盘价10元", symbol=OTHER)

    gated_plan = deepcopy(plan)
    gate = dl.apply_gate_to_plan(
        gated_plan,
        daily_dir=tmp_path / "data" / "daily",
        name_to_symbol=dl.load_name_to_symbol(root=tmp_path),
    )
    assert gated_plan["statement_review"]["status"] == "unverified", gate.rejections
    assert not gate.accepted_intents, "主体缺证不得放行"
    hit = gate.rejections[0]["statement_rejections"][0]
    assert hit["kind"] == "claimed_subject_unresolved"
    assert hit["subject_text"] == "万科A"


def test_time_words_are_not_mistaken_for_a_subject(tmp_path: Path) -> None:
    """时间语义正常对照：只有时段词、没有票名时照旧退回提议标的自己核对。"""

    write_daily(tmp_path)
    intent = {"symbol": SYMBOL, "side": "buy", "ref_close": 10.0,
              "llm_reason": "近1个交易日收盘价10元", "style": "波段", "planned_hold_days": 1}

    hits = dl.verify_intent_against_daily(
        intent, decision_date=DAY, daily_dir=tmp_path / "data" / "daily", name_to_symbol={}
    )

    assert hits == [], hits


def test_correct_claim_is_accepted_without_any_unverified_flag(tmp_path: Path) -> None:
    """正常对照：收盘价对得上 → 通过，且没有未核验标记。"""

    write_daily(tmp_path)

    plan = plan_with("今天收盘价10元")
    plan_copy = deepcopy(plan)
    gate = dl.apply_gate_to_plan(
        plan_copy,
        daily_dir=tmp_path / "data" / "daily",
        name_to_symbol=dl.load_name_to_symbol(root=tmp_path),
    )

    assert gate.rejections == []
    assert plan_copy["statement_review"]["status"] == "accepted", plan_copy["statement_review"]
    assert not gate.accepted_intents[0].get("statement_unverified")
