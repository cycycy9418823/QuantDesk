"""PRODUCT-S2-1 真实链路用例：当晚被拒的判断，重建成空记录库后仍看得见、也不被后来的行情改写。

五个场景（全部走真函数，不替换成固定返回值）：

1. 计划 + 日记都在 → 空库重建：原拒绝（代码/方向/理由/原证据/当时所见）在
   ``decision_latest.json`` 与页面里都能找到，且不得退化成无代码的“观望/不适用”。
2. 只有日记（删掉 ``plan_latest.json``）→ 同上。
3. 同一输入重复重建：条数不增、``decision_id`` 不变、``seen`` 逐字段一致。
4. 行情事后由 10 修正为 10.01 → 原 ``seen.close`` 不被改写。
5. 旧证据缺失（当晚所见没保存、日线也读不到）→ 标 ``source="missing"``、价格留 None，
   不用当前行情或当前时钟补造。

链路：``apply_gate_to_plan`` → ``overnight.save_plan`` → ``journal.build_journal`` /
``journal.append_journal`` → ``decision_log.ingest_state``（空库重建）→ 页面。

合成夹具：假行情（表头 ``date,symbol,open,high,low,close,volume,amount,pct_chg,adjust``）、
假模型回复（``model="synthetic-fixture-no-model-call"``）、临时路径；不联网、不调用真实模型、
不写正式 ``data/``、``state/``、``results/``。

已知缺口（本单按指令只修 ``seen``，登记在 ``OWNER_SUMMARY.md`` 的“还差哪些块”）：
原计划的 ``recorded_at`` 没有随被拒项保存，日记里也不写时刻，所以空库重建后
``recorded_at`` 只能是空串；用例只保证它不会被重建时的当前时钟顶替。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import decision_log as dl
from ai_trader import journal as journal_io
from ai_trader import overnight as evening
from ai_trader.ledger import empty_ledger
from ai_trader.trader import execute_intents

DAY = "2026-09-22"
NEXT_TRADE_DAY = "2026-09-23"
SYMBOL = "600000"
BAD_REASON = "今天收盘价99元"
GOOD_REASON = "今天收盘价10元"
NIGHT_STAMP = f"{DAY}T20:20:00+08:00"
DAILY_HEADER = "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"
RISING_DAY = "2026-09-21"

_BRIEF_KEYS = (
    "decision_id", "decision_date", "trade_date", "symbol", "side", "judgment",
    "result", "committed", "recorded_at", "seen", "result_evidence",
)


def _brief(rows: list[dict]) -> list[dict]:
    return [{key: row.get(key) for key in _BRIEF_KEYS} for row in rows]


def _detail(value: object, note: str) -> str:
    return f"{note}\n实际：{json.dumps(value, ensure_ascii=False, indent=1)}"


def _write_daily_bars(daily_dir: Path, *, close: float) -> None:
    """假行情：前一交易日收盘 10；决策日收盘由参数决定（用于模拟事后修正）。"""
    daily_dir.mkdir(parents=True, exist_ok=True)
    pct = (close / 10.0 - 1.0) * 100
    rows = [
        DAILY_HEADER,
        f"{RISING_DAY},{SYMBOL},10,10.1,9.9,10,1000,10000,0,raw",
        f"{DAY},{SYMBOL},10,{max(10.1, close)},{min(9.9, close)},{close},1000,10000,{pct:.4f},raw",
    ]
    (daily_dir / f"{SYMBOL}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _plan(reason: str) -> dict:
    return {
        "schema": evening.PLAN_SCHEMA,
        "decision_date": DAY,
        "trade_date": NEXT_TRADE_DAY,
        "committed": True,
        "recorded_at": NIGHT_STAMP,
        "llm": {"used": True, "model": "synthetic-fixture-no-model-call", "market_note": "观望"},
        "intents": [
            {
                "symbol": SYMBOL,
                "side": "buy",
                "ref_close": 10.0,
                "reason": reason,
                "style": "波段",
                "planned_hold_days": 1,
            }
        ],
    }


def _night(
    tmp_path: Path,
    *,
    reason: str = BAD_REASON,
    close: float = 10.0,
    write_bars: bool = True,
) -> tuple[Path, Path, dict, object]:
    """真函数走一遍当晚：gate → save_plan → 组装/追加日记。

    ``write_bars=False`` 模拟“当晚日线本来就缺失”：目录在，但当天没有任何行情。
    """
    daily_dir = tmp_path / "daily"
    state_dir = tmp_path / "state"
    if write_bars:
        _write_daily_bars(daily_dir, close=close)
    else:
        daily_dir.mkdir(parents=True, exist_ok=True)
    plan = _plan(reason)
    gated = dl.apply_gate_to_plan(plan, daily_dir=daily_dir)
    evening.save_plan(plan, state_dir / "plan_latest.json")
    note = journal_io.build_journal(
        as_of=DAY,
        trade_date=NEXT_TRADE_DAY,
        research={},
        actions=[],
        ledger={"cash": 100000.0, "equity": 100000.0, "initial_cash": 100000.0},
        warnings=["合成用例：没有真实行情、模型或交易"],
        llm=plan["llm"],
        committed_buy_symbols=[row["symbol"] for row in plan["intents"] if row.get("side") == "buy"],
    )
    # 与 overnight.run_evening 的持久化形状一致（那里需要整晚循环与模型，本用例不跑）：
    # 被拒的原提议随计划一起写进日记，最新计划隔天被覆盖后仍要能还原。
    note["tomorrow_plan"] = {
        "trade_date": NEXT_TRADE_DAY,
        "intents": plan.get("intents"),
        "statement_rejections": plan.get("statement_rejections"),
        "recorded_at": plan.get("recorded_at"),
        "conviction": "probation",
        "prediction_note": evening.PREDICTION_NOTE,
    }
    note["prompt_version"] = plan.get("prompt_version")
    note["menu_version"] = plan.get("menu_version")
    journal_io.append_journal(
        note,
        latest_path=state_dir / "journal_latest.json",
        log_path=state_dir / "journal.jsonl",
    )
    return daily_dir, state_dir, plan, gated


def _execute(intents: list[dict]) -> tuple[dict, list[dict]]:
    """真执行器：同一账户、同一开盘价，只换传入的意图。"""
    return execute_intents(
        empty_ledger(created_at=NIGHT_STAMP, ledger_id="fixture-ledger"),
        as_of=NEXT_TRADE_DAY,
        intents=[dict(row) for row in intents],
        open_prices={SYMBOL: 10.0},
        prev_closes={SYMBOL: 10.0},
        live_id=None,
    )



def _rebuild(tmp_path: Path, daily_dir: Path, state_dir: Path, name: str = "rebuilt") -> tuple[Path, list[dict]]:
    out = tmp_path / name
    dl.ingest_state(daily_dir=daily_dir, state_dir=state_dir, out_dir=out, as_of=DAY)
    latest = json.loads((out / "decision_latest.json").read_text(encoding="utf-8"))["records"]
    return out, latest


def _only_rejection(rows: list[dict]) -> dict:
    found = [row for row in rows if row.get("symbol") == SYMBOL and row.get("side") == "buy"]
    assert len(found) == 1, _detail(_brief(rows), "原拒绝必须且只能出现一次")
    return found[0]


def _only_intent(rows: list[dict]) -> dict:
    found = [row for row in rows if row.get("symbol") == SYMBOL and row.get("side") == "buy"]
    assert len(found) == 1, _detail(_brief(rows), "通过的记录必须且只能出现一次")
    return found[0]



def test_s2_plan_and_journal_rebuild_keeps_rejected_judgment(tmp_path: Path) -> None:
    """场景 1：计划 + 日记都在，空记录库重建后原拒绝仍完整可见。"""
    daily_dir, state_dir, plan, gated = _night(tmp_path)
    assert len(gated.rejections) == 1, "夹具必须产生一条真实拒绝"
    assert plan["intents"] == [], "被拒提议不得留在可执行意图里"
    assert plan["statement_rejections"], "当晚被拒项必须落进计划（否则日记也带不出去）"
    assert (state_dir / "plan_latest.json").is_file()
    assert (state_dir / "journal.jsonl").is_file()

    out, rows = _rebuild(tmp_path, daily_dir, state_dir)
    row = _only_rejection(rows)
    assert row["judgment"] == BAD_REASON, _detail(_brief(rows), "原拒绝理由必须保留")
    assert row["result"] == dl.RESULT_MISMATCH
    assert row["committed"] is False, "被拒的记录不得当成已生效"
    assert [hit.get("kind") for hit in row["result_evidence"]] == ["claimed_close"]
    assert row["seen"]["close"] == 10.0, _detail(row["seen"], "当晚所见收盘价必须保留")
    assert row["seen"]["as_of"] == DAY
    assert not [
        item for item in rows if (item.get("verification") or {}).get("source") == "no_symbol_call"
    ], _detail(_brief(rows), "不得退化成无代码的“观望/不适用”")
    # 原落盘时刻：日记里没写就只能留空，但绝不能用重建时的当前时钟顶替
    assert row["recorded_at"] == "" or row["recorded_at"].startswith(DAY), row["recorded_at"]

    html = (out / "decision_page.html").read_text(encoding="utf-8")
    md = (out / "decision_page.md").read_text(encoding="utf-8")
    assert BAD_REASON in html and BAD_REASON in md, "页面必须显示原拒绝理由"
    assert SYMBOL in html and SYMBOL in md, "页面必须显示被拒的代码"
    assert dl.RESULT_MISMATCH in html and dl.RESULT_MISMATCH in md


def test_s2_journal_only_rebuild_keeps_rejected_judgment(tmp_path: Path) -> None:
    """场景 2：最新计划被删（只剩日记）时，重建仍要还原原拒绝。"""
    daily_dir, state_dir, plan, gated = _night(tmp_path)
    assert len(gated.rejections) == 1
    (state_dir / "plan_latest.json").unlink()
    assert not (state_dir / "plan_latest.json").exists()

    out, rows = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt_from_journal")
    row = _only_rejection(rows)
    assert row["judgment"] == BAD_REASON, _detail(_brief(rows), "只剩日记也必须还原原理由")
    assert row["result"] == dl.RESULT_MISMATCH
    assert [hit.get("kind") for hit in row["result_evidence"]] == ["claimed_close"]
    assert row["seen"]["close"] == 10.0
    assert not [
        item for item in rows if (item.get("verification") or {}).get("source") == "no_symbol_call"
    ], _detail(_brief(rows), "不得退回兜底的“观望/不适用”")
    assert BAD_REASON in (out / "decision_page.md").read_text(encoding="utf-8")


def test_s2_same_input_rebuild_does_not_grow_or_renumber(tmp_path: Path) -> None:
    """场景 3：同一输入重复重建——条数不增、``decision_id`` 与 ``seen`` 不变。"""
    daily_dir, state_dir, plan, gated = _night(tmp_path)
    assert len(gated.rejections) == 1

    out, first = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt")
    second_payload = dl.ingest_state(
        daily_dir=daily_dir, state_dir=state_dir, out_dir=out, as_of=DAY
    )
    second = second_payload["records"]
    assert len(first) == len(second) == 1, _detail(_brief(second), "重复重建不得新增记录")
    assert first[0]["decision_id"] == second[0]["decision_id"]
    assert first[0]["seen"] == second[0]["seen"], _detail(
        [first[0]["seen"], second[0]["seen"]], "所见行情必须逐字段一致"
    )
    assert first[0]["judgment"] == second[0]["judgment"] == BAD_REASON


def test_s2_corrected_daily_never_rewrites_original_seen(tmp_path: Path) -> None:
    """场景 4：行情事后由 10 修正为 10.01，原 ``seen`` 不得被改写。"""
    daily_dir, state_dir, plan, gated = _night(tmp_path)
    assert len(gated.rejections) == 1

    _write_daily_bars(daily_dir, close=10.01)  # 事后修正行情
    assert dl.load_daily_bar(SYMBOL, DAY, daily_dir)["close"] == 10.01, "夹具必须先真的改掉行情"

    out, rows = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt_after_correction")
    row = _only_rejection(rows)
    assert row["seen"]["close"] == 10.0, _detail(row["seen"], "原 seen.close 不得被修正后的行情覆盖")
    assert row["seen"]["source"] == "recorded_at_decision", row["seen"]
    assert row["seen"]["as_of"] == DAY
    assert row["seen"]["open"] == 10.0 and row["seen"]["high"] == 10.1 and row["seen"]["low"] == 9.9
    assert row["seen"]["pct_chg"] == 0.0, "pct_chg 也必须是当晚所见，不是修正后的 0.1"
    assert row["result"] == dl.RESULT_MISMATCH and row["judgment"] == BAD_REASON
    assert BAD_REASON in (out / "decision_page.md").read_text(encoding="utf-8")
    # 修好它的前提：当晚所见确实随被拒项落了盘
    seen_at_night = dict(plan["statement_rejections"][0].get("seen_at_decision") or {})
    assert seen_at_night.get("close") == 10.0, _detail(seen_at_night, "当晚所见收盘价必须随计划落盘")


def test_s2_missing_evidence_is_marked_missing_not_filled(tmp_path: Path) -> None:
    """场景 5：旧记录没保存当晚所见、日线也读不到——标缺失，不拿新行情/当前时钟补造。"""
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)  # 空行情目录（当天没有任何日线）
    assert dl.load_daily_bar(SYMBOL, DAY, daily_dir) is None

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    old_shape_rejection = {
        "symbol": SYMBOL,
        "side": "buy",
        "label": dl.RESULT_MISMATCH,
        "hits": [
            {
                "kind": "claimed_close",
                "detail": f"陈述{DAY}收盘99.0，日线收盘10.0",
                "label": dl.RESULT_MISMATCH,
                "actual": 10.0,
                "claimed": 99.0,
                "symbol": SYMBOL,
                "evidence_date": DAY,
            }
        ],
        "original_reason": BAD_REASON,
        "original_intent": {
            "symbol": SYMBOL,
            "side": "buy",
            "ref_close": 10.0,
            "reason": BAD_REASON,
            "style": "波段",
            "planned_hold_days": 1,
        },
        # 故意不带 seen_at_decision：模拟“当时所见没有保存”的旧记录
    }
    note = {
        "as_of": DAY,
        "trade_date": NEXT_TRADE_DAY,
        "llm": {"used": True, "model": "synthetic-fixture-no-model-call", "market_note": "观望"},
        "tomorrow_plan": {
            "trade_date": NEXT_TRADE_DAY,
            "intents": [],
            "statement_rejections": [old_shape_rejection],
        },
    }
    journal_io.append_journal(
        note,
        latest_path=state_dir / "journal_latest.json",
        log_path=state_dir / "journal.jsonl",
    )

    out, rows = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt_missing_evidence")
    row = _only_rejection(rows)
    assert row["seen"]["source"] == "missing", _detail(row["seen"], "没有证据就必须标缺失")
    for key in ("open", "high", "low", "close", "pct_chg"):
        assert row["seen"][key] is None, _detail(row["seen"], f"缺失的 {key} 不得补造")
    assert row["seen"]["as_of"] == DAY, "缺失时的标注日期只能用决策日，不能用重建当天"
    assert row["seen"]["ref_close"] == 10.0, "原提议自带的 ref_close 仍应保留"
    assert row["recorded_at"] == "", _detail(row["recorded_at"], "不得用重建时的当前时钟补造落盘时间")
    assert row["judgment"] == BAD_REASON and row["result"] == dl.RESULT_MISMATCH
    assert BAD_REASON in (out / "decision_page.md").read_text(encoding="utf-8")


def test_s2_accepted_intent_keeps_night_seen_and_executor_is_unchanged(tmp_path: Path) -> None:
    """场景 6（r9 验收发现 #2）：通过的记录也在当晚存所见；重建不被修正行情改写。

    同时证明新键不进 identity、不改执行器与账本：同一意图加键前后逐字段相同。
    """
    daily_dir, state_dir, plan, gated = _night(tmp_path, reason=GOOD_REASON)
    assert gated.rejections == [] and len(plan["intents"]) == 1, "夹具必须是“通过”的那条路"
    intent = plan["intents"][0]
    assert intent["seen_at_decision"]["close"] == 10.0, _detail(intent.get("seen_at_decision"), "通过的意图也要在当晚存所见")
    assert intent["recorded_at"] == NIGHT_STAMP

    # 新键不得改变执行器与账本：加键前 / 加键后逐字段相同
    with_keys = dict(intent, quantity=1000)
    without_keys = {k: v for k, v in with_keys.items() if k not in ("seen_at_decision", "recorded_at")}
    ledger_with, actions_with = _execute([with_keys])
    ledger_without, actions_without = _execute([without_keys])
    assert [row.get("status") for row in actions_with] == ["filled"], _detail(actions_with, "夹具必须真的成交")
    assert actions_with == actions_without, _detail([actions_with, actions_without], "执行器产出必须逐字段相同")
    assert ledger_with == ledger_without, "账本必须逐字段相同"
    # identity / decision_id 也不受新键影响
    record_with = dl.build_decision_record(with_keys, plan=plan, daily_dir=daily_dir)
    record_without = dl.build_decision_record(without_keys, plan=plan, daily_dir=daily_dir)
    assert record_with["decision_id"] == record_without["decision_id"]

    _write_daily_bars(daily_dir, close=10.01)  # 事后修正行情
    out, rows = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt_after_correction")
    row = _only_intent(rows)
    assert row["seen"]["close"] == 10.0, _detail(row["seen"], "通过记录的 seen.close 不得被修正行情覆盖")
    assert row["seen"]["source"] == "recorded_at_decision", row["seen"]
    assert row["recorded_at"] == NIGHT_STAMP
    assert row["decision_id"] == record_with["decision_id"]
    assert GOOD_REASON in (out / "decision_page.md").read_text(encoding="utf-8")


def test_s2_night_without_daily_bar_is_marked_missing(tmp_path: Path) -> None:
    """场景 7（Claude 反例 B）：当晚本来就没有日线 → 所见全为 null，记录必须标 missing。"""
    daily_dir, state_dir, plan, gated = _night(tmp_path, write_bars=False)
    assert len(gated.rejections) == 1, "没有日线时提议应被拒"
    assert gated.rejections[0]["statement_rejections"][0]["kind"] == "daily_bar_missing"
    snapshot = dict(plan["statement_rejections"][0]["seen_at_decision"])
    assert snapshot["as_of"] == DAY
    for key in ("open", "high", "low", "close", "pct_chg"):
        assert snapshot[key] is None, _detail(snapshot, f"当晚没有日线，{key} 必须是 null")

    immediate = dl.records_from_plan(plan, daily_dir=daily_dir, rejections=gated.rejections)
    assert len(immediate) == 1
    assert immediate[0]["seen"]["source"] == "missing", _detail(immediate[0]["seen"], "当晚没看到任何价格就必须标 missing")
    assert immediate[0]["seen"]["close"] is None

    out, rows = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt_no_bars")
    row = _only_rejection(rows)
    assert row["seen"]["source"] == "missing", _detail(row["seen"], "重建后也必须标 missing，不能标 recorded_at_decision")
    for key in ("open", "high", "low", "close", "pct_chg"):
        assert row["seen"][key] is None, _detail(row["seen"], f"缺失的 {key} 不得补造")
    assert row["judgment"] == BAD_REASON and row["result"] == dl.RESULT_MISMATCH
    assert BAD_REASON in (out / "decision_page.md").read_text(encoding="utf-8")


def test_s2_plan_recorded_time_survives_empty_store_rebuild(tmp_path: Path) -> None:
    """场景 8（r9 验收发现 #3）：计划的落盘时刻随被拒项与日记保存，重建用它。"""
    daily_dir, state_dir, plan, gated = _night(tmp_path)
    assert len(gated.rejections) == 1
    assert plan["recorded_at"] == NIGHT_STAMP
    assert plan["statement_rejections"][0]["recorded_at"] == NIGHT_STAMP, "落盘时刻必须随被拒项保存"
    note = json.loads((state_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert note["tomorrow_plan"]["recorded_at"] == NIGHT_STAMP, "也要随日记保存"

    _, rows = _rebuild(tmp_path, daily_dir, state_dir)
    assert _only_rejection(rows)["recorded_at"] == NIGHT_STAMP, _detail(rows[0].get("recorded_at"), "空库重建必须用当晚的落盘时刻")

    (state_dir / "plan_latest.json").unlink()  # 只剩日记
    _, journal_rows = _rebuild(tmp_path, daily_dir, state_dir, name="rebuilt_recorded_journal_only")
    assert _only_rejection(journal_rows)["recorded_at"] == NIGHT_STAMP, "只剩日记也要还原落盘时刻"

    # 注意：旧记录没有这个键时留空（场景 5 已断言），重建时不得用当前时钟补造


