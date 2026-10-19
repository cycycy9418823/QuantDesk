"""C-line decision log: versions, statement gate vs data/daily, owner page."""

from __future__ import annotations

from pathlib import Path

from ai_trader.cycle import CyclePaths, run_cycle
from ai_trader.decision_log import (
    RESULT_EXPIRED_NO_EVIDENCE,
    RESULT_MISMATCH,
    RESULT_PENDING,
    RESULT_RIGHT,
    RESULT_WRONG,
    apply_gate_to_plan,
    ingest_state,
    load_daily_bar,
    records_from_plan,
    render_owner_page,
    score_decision,
    upsert_decision_records,
    verify_intent_against_daily,
    write_decision_records,
)
from ai_trader.ledger import empty_ledger, save_ledger
from ai_trader.llm import _build_prompt
from ai_trader.prompt_pack import load_prompt_pack


FROZEN_SYSTEM = (
    "你是一名 A 股纸面交易员，人格：TEST_PERSONA。"
    "你在收盘后复盘、为次日开盘布置纸面计划。这是纯纸面模拟、不接券商、不是投资建议。\n\n"
    "**做什么风格由你自己决定，没人规定你必须做短线。** 你可以选："
    "打板(追涨停,持1-3天,最激进)、短线(几天)、波段(一到几周)、"
    "中线(一到几个月)、长期(半年以上,看公司不看盘)。"
    "你可以对不同的票用不同的做法；也可以今天想通了就改主意，改了就说为什么。"
    "如果你想买一只票放一年，那就把 hold_days 写成 250，这是允许的。"
    "如果今天什么都看不上，buys 就留空——空仓也是一种决定。\n\n"
    "唯一的硬约束是账户安全(不是风格)：持仓数上限、单票占比上限、现金下限、"
    "每天最多开几只。成交按次日开盘价、一字涨停买不进、一字跌停卖不出，"
    "这些由系统铁面执行，你不用管。\n\n"
    "只在给你的候选清单里选，不许编造代码。"
    "只输出一个 JSON 对象，不要多余文字，格式："
    '{"stance":{"summary":"你现在想怎么投资,一两句","preferred_styles":["长期","波段"],'
    '"reasoning":"为什么这么想,尤其是如果你改主意了"},'
    '"buys":[{"symbol":"6位代码","reason":"一句中文理由","style":"长期",'
    '"hold_days":整数,"conviction":"live或probation"}],'
    '"sells":[{"symbol":"6位代码","reason":"为什么现在卖"}],'
    '"avoid":["代码"],"lesson":"今天复盘学到的一句话(可留空)",'
    '"market_note":"一句中文对今天盘面的看法"}'
)


def _write_daily(root: Path, symbol: str, rows: list[tuple[str, float, float]]) -> None:
    lines = ["date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"]
    prev = rows[0][2]
    for day, open_px, close in rows:
        high = max(open_px, close)
        low = min(open_px, close)
        chg = 0.0 if prev == 0 else (close / prev - 1.0) * 100.0
        lines.append(
            f"{day},{symbol},{open_px},{high},{low},{close},1000,10000,{chg:.4f},raw"
        )
        prev = close
    (root / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _runtime(tmp_path: Path, daily: Path) -> CyclePaths:
    out = tmp_path / "runtime"
    return CyclePaths(
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        ledger=out / "ledger.json",
        hypotheses=out / "hypotheses.json",
        journal_latest=out / "journal_latest.json",
        journal_log=out / "journal.jsonl",
        plan_latest=out / "plan.json",
        review_latest=out / "review.json",
        watchlist=out / "watchlist.json",
        philosophy=out / "philosophy.json",
        cycle_latest=out / "cycle.json",
        snapshot=out / "snapshot.json",
        daytime_notes=out / "daytime_notes.jsonl",
        daytime_session=out / "daytime_session.json",
        decision_log=out / "decision_log.jsonl",
        decision_latest=out / "decision_latest.json",
        decision_page=out / "decision_page.html",
        decision_page_md=out / "decision_page.md",
    )


def test_prompt_file_matches_frozen_53fa41c_text() -> None:
    pack = load_prompt_pack()
    assert pack.prompt_version == "v1.0.0"
    assert pack.menu_version == "v1.0.0"
    assert pack.render_system("TEST_PERSONA") == FROZEN_SYSTEM
    system, user = _build_prompt(
        {
            "persona_zh": "TEST_PERSONA",
            "as_of": "2026-09-04",
            "trade_date": "2026-09-07",
            "equity": 1,
            "cash": 1,
            "holdings_count": 0,
            "mandate_note": "MANDATE",
        }
    )
    assert system == FROZEN_SYSTEM
    assert "今天(决策日)：2026-09-04，为次日 2026-09-07 开盘布置。" in user
    assert "账户硬约束：MANDATE" in user
    assert "可选候选" in user


def test_ref_close_must_match_daily(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-04", 10.0, 10.0), ("2026-09-07", 10.2, 10.4)])
    ok = verify_intent_against_daily(
        {"symbol": "600000", "side": "buy", "ref_close": 10.0},
        decision_date="2026-09-04",
        daily_dir=daily,
    )
    assert ok == []
    bad = verify_intent_against_daily(
        {"symbol": "600000", "side": "buy", "ref_close": 99.0},
        decision_date="2026-09-04",
        daily_dir=daily,
    )
    assert bad
    assert bad[0]["label"] == RESULT_MISMATCH


def test_drop_pct_matches_computed_daily_change(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(
        daily,
        "600172",
        [
            ("2026-08-17", 13.64, 13.64),
            ("2026-08-18", 14.32, 14.32),
            ("2026-08-19", 14.42, 14.42),
            ("2026-08-20", 13.44, 13.44),
            ("2026-08-21", 13.54, 13.54),
            ("2026-08-24", 12.72, 12.72),
        ],
    )
    ok = verify_intent_against_daily(
        {
            "symbol": "600172",
            "side": "buy",
            "ref_close": 12.72,
            "llm_reason": "研究台推荐且近5个交易日跌6.7%，正合我超跌反转口味",
        },
        decision_date="2026-08-24",
        daily_dir=daily,
    )
    assert ok == []
    fake = verify_intent_against_daily(
        {
            "symbol": "600172",
            "side": "buy",
            "ref_close": 12.72,
            "llm_reason": "近期跌30%，已经腰斩",
        },
        decision_date="2026-08-24",
        daily_dir=daily,
    )
    assert fake
    assert fake[0]["kind"] == "claimed_pct_ambiguous_period"


def test_llm_reason_fake_price_is_rejected(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-04", 10.0, 10.0)])
    hits = verify_intent_against_daily(
        {
            "symbol": "600000",
            "side": "buy",
            "ref_close": 10.0,
            "llm_reason": "今天收盘18.88，要上20",
        },
        decision_date="2026-09-04",
        daily_dir=daily,
    )
    assert any(item["kind"] == "claimed_close" for item in hits)


def test_market_note_fake_pct_rejects_llm_plan(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-04", 10.0, 10.0)])
    _write_daily(daily, "600613", [("2026-09-04", 5.0, 5.1)])
    plan = {
        "decision_date": "2026-09-04",
        "trade_date": "2026-09-07",
        "prompt_version": "v1.0.0",
        "menu_version": "v1.0.0",
        "llm": {"used": True, "market_note": "个别票暴涨(600613+40%)", "model": "x"},
        "intents": [
            {
                "symbol": "600000",
                "side": "buy",
                "ref_close": 10.0,
                "style": "中线",
                "planned_hold_days": 60,
                "llm_reason": "蓝筹底仓",
            }
        ],
    }
    gated = apply_gate_to_plan(plan, daily_dir=daily)
    # 2AI-1（所有者规则 2）：点评里的假涨幅只标注在点评上，不拦理由本身没问题的买入。
    assert len(plan["intents"]) == 1
    assert gated.rejections == []
    assert gated.market_note_rejections[0]["label"] == RESULT_MISMATCH
    assert plan["statement_review"]["market_note_ok"] is False


def test_structured_record_has_versions_and_condition(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-04", 10.0, 10.0)])
    plan = {
        "decision_date": "2026-09-04",
        "trade_date": "2026-09-07",
        "prompt_version": "v1.0.0",
        "menu_version": "v1.0.0",
        "committed": True,
        "llm": {"used": True, "market_note": "防守", "model": "x"},
        "intents": [
            {
                "symbol": "600000",
                "side": "buy",
                "ref_close": 10.0,
                "style": "中线",
                "planned_hold_days": 5,
                "llm_reason": "当底仓",
            }
        ],
    }
    records = records_from_plan(plan, daily_dir=daily)
    assert len(records) == 1
    rec = records[0]
    assert rec["prompt_version"] == "v1.0.0"
    assert rec["menu_version"] == "v1.0.0"
    assert rec["result"] == RESULT_PENDING
    assert rec["seen"]["close"] == 10.0
    assert rec["seen"]["source"] == "data/daily"
    assert "5个交易日内收盘不站上10.0就是错了" == rec["verification"]["text"]


def test_score_stand_above_right_wrong_pending(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(
        daily,
        "600000",
        [
            ("2026-09-01", 10.0, 10.0),
            ("2026-09-02", 10.0, 10.5),
            ("2026-09-03", 10.5, 9.5),
            ("2026-09-04", 9.5, 9.4),
        ],
    )
    base = {
        "symbol": "600000",
        "side": "buy",
        "result": RESULT_PENDING,
        "trade_date": "2026-09-02",
        "verification": {"kind": "stand_above", "n_sessions": 2, "level": 10.4},
    }
    pending = score_decision(dict(base), daily_dir=daily, as_of="2026-09-01")
    assert pending["result"] == RESULT_PENDING
    early = score_decision(dict(base), daily_dir=daily, as_of="2026-09-02")
    assert early["result"] == RESULT_PENDING
    assert early["result_evidence"][0]["kind"] == "early_hit"
    right = score_decision(early, daily_dir=daily, as_of="2026-09-03")
    assert right["result"] == RESULT_RIGHT
    miss_plan = dict(base)
    miss_plan["verification"] = {"kind": "stand_above", "n_sessions": 2, "level": 20.0}
    wrong = score_decision(miss_plan, daily_dir=daily, as_of="2026-09-03")
    assert wrong["result"] == RESULT_WRONG


def test_owner_page_lists_wrong_first(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    md = tmp_path / "page.md"
    render_owner_page(
        [
            {
                "result": RESULT_RIGHT,
                "decision_date": "2026-09-01",
                "symbol": "600001",
                "side": "buy",
                "style": "短线",
                "planned_hold_days": 3,
                "judgment": "对的那笔",
                "verification": {"text": "条件A"},
                "prompt_version": "v1.0.0",
                "menu_version": "v1.0.0",
            },
            {
                "result": RESULT_WRONG,
                "decision_date": "2026-09-02",
                "symbol": "600002",
                "side": "buy",
                "style": "短线",
                "planned_hold_days": 3,
                "judgment": "错的那笔",
                "verification": {"text": "条件B"},
                "prompt_version": "v1.0.0",
                "menu_version": "v1.0.0",
            },
        ],
        html_path=html,
        md_path=md,
    )
    text = md.read_text(encoding="utf-8")
    html_text = html.read_text(encoding="utf-8")
    assert text.index("600002") < text.index("600001")
    assert "纸面 · 未验证" in html_text
    assert "验证条件为程序推导，非 AI 声明" in text
    assert "验证条件为程序推导，非 AI 声明" in html_text


def test_ingest_state_does_not_call_llm(tmp_path: Path, monkeypatch) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(
        daily,
        "600000",
        [("2026-09-04", 10.0, 10.0), ("2026-09-05", 10.0, 10.2), ("2026-09-08", 10.2, 10.3)],
    )
    state = tmp_path / "state"
    state.mkdir()
    journal = {
        "as_of": "2026-09-04",
        "trade_date": "2026-09-05",
        "llm": {"used": True, "market_note": "防守", "model": "x"},
        "tomorrow_plan": {
            "trade_date": "2026-09-05",
            "intents": [
                {
                    "symbol": "600000",
                    "side": "buy",
                    "ref_close": 10.0,
                    "style": "短线",
                    "planned_hold_days": 2,
                    "llm_reason": "当底仓",
                }
            ],
        },
    }
    (state / "journal.jsonl").write_text(
        __import__("json").dumps(journal, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("LLM must not run")

    monkeypatch.setattr("ai_trader.llm.propose_picks", boom)
    out = tmp_path / "out"
    payload = ingest_state(daily_dir=daily, state_dir=state, out_dir=out, as_of="2026-09-08")
    assert called["n"] == 0
    assert payload["ok"] is True
    assert (out / "decision_page.html").is_file()
    assert (out / "decision_page.md").is_file()
    log = (out / "decision_log.jsonl").read_text(encoding="utf-8")
    assert "600000" in log
    assert "historical_review" in log
    assert "T20:20:00+08:00" not in log
    assert payload["manifest"]["git_head"]


def test_offline_cycle_writes_versioned_decision_log(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    for i in range(8):
        closes = [20.0 - n * 0.05 for n in range(80)]
        start = __import__("pandas").Timestamp("2024-01-02")
        dates = __import__("pandas").bdate_range(start, periods=len(closes))
        rows = ["date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"]
        prev = closes[0]
        symbol = f"60000{i}"
        for day, close in zip(dates, closes, strict=True):
            chg = 0.0 if prev == 0 else (close / prev - 1.0) * 100
            rows.append(
                f"{day.date().isoformat()},{symbol},{close},{close},{close},{close},1000,10000,{chg:.4f},raw"
            )
            prev = close
        (daily / f"{symbol}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    runtime = _runtime(tmp_path, daily)
    save_ledger(empty_ledger(), runtime.ledger)
    payload = run_cycle(paths=runtime, lookback=80, universe_limit=8, network=False)
    assert payload["ok"] is True
    assert payload["plan"]["prompt_version"] == "v1.0.0"
    assert payload["plan"]["menu_version"] == "v1.0.0"
    log_path = runtime.journal_log.parent / "decision_log.jsonl"
    assert log_path.is_file()
    text = log_path.read_text(encoding="utf-8")
    assert "ai_trader_decision_v1" in text
    assert "v1.0.0" in text
    page = runtime.cycle_latest.parent / "decision_page.html"
    assert page.is_file()
    assert "纸面 · 未验证" in page.read_text(encoding="utf-8")


def test_load_daily_bar_reads_only_csv(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600795", [("2026-09-03", 5.37, 5.40)])
    bar = load_daily_bar("600795", "2026-09-03", daily)
    assert bar is not None
    assert bar["close"] == 5.4
    assert load_daily_bar("600795", "1999-01-01", daily) is None


def test_gate_rejects_false_facts_in_reason_and_integer_price(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-03", 10.0, 10.0), ("2026-09-04", 10.0, 10.04)])
    sell_plan = {
        "decision_date": "2026-09-04",
        "intents": [{"symbol": "600000", "side": "sell", "ref_close": 10.04, "reason": "今天收盘18.88"}],
    }
    gated = apply_gate_to_plan(sell_plan, daily_dir=daily)
    # 2AI-1（所有者规则 2）：卖出照常放行，带"含与数据不符说法"标注。
    assert len(sell_plan["intents"]) == 1
    assert sell_plan["intents"][0]["statement_label"] == "含与数据不符说法"
    assert gated.rejections == []
    integer_plan = {
        "decision_date": "2026-09-04",
        "intents": [{"symbol": "600000", "side": "buy", "ref_close": 10.04, "llm_reason": "今天收盘18元"}],
    }
    gated = apply_gate_to_plan(integer_plan, daily_dir=daily)
    assert integer_plan["intents"] == []
    assert gated.rejections


def test_gate_rejects_40_percent_when_bar_moved_point_four(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-03", 10.0, 10.0), ("2026-09-04", 10.0, 10.04)])
    plan = {
        "decision_date": "2026-09-04",
        "intents": [{"symbol": "600000", "side": "buy", "ref_close": 10.04, "llm_reason": "今天涨40%"}],
    }
    gated = apply_gate_to_plan(plan, daily_dir=daily)
    assert plan["intents"] == []
    assert gated.rejections


def test_gate_does_not_treat_tomorrow_prediction_as_today_fact(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-03", 10.0, 10.0), ("2026-09-04", 10.0, 10.04)])
    plan = {
        "decision_date": "2026-09-04",
        "intents": [{"symbol": "600000", "side": "buy", "ref_close": 10.04, "llm_reason": "预计明天涨10%"}],
    }
    gated = apply_gate_to_plan(plan, daily_dir=daily)
    assert len(plan["intents"]) == 1
    assert gated.rejections == []


def test_backfill_does_not_invent_2020_or_current_prompt_version(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-04", 10.0, 10.0)])
    plan = {
        "decision_date": "2026-09-04",
        "trade_date": "2026-09-07",
        "committed": True,
        "intents": [{"symbol": "600000", "side": "buy", "ref_close": 10.0, "style": "短线", "planned_hold_days": 2}],
    }
    records = records_from_plan(plan, daily_dir=daily, backfill=True)
    assert records[0]["recorded_at"] == ""
    assert records[0]["backfill"] is True
    assert records[0]["provenance"] == "historical_review"
    assert records[0]["prompt_version"] == ""
    assert "T20:20:00" not in str(records[0]["recorded_at"])


def test_upsert_keeps_old_wrong_when_horizon_is_revised(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    old = {
        "decision_id": "dec_20260904_600000_buy",
        "judgment": "当底仓",
        "planned_hold_days": 2,
        "verification": {"kind": "stand_above", "n_sessions": 2, "level": 10.4},
        "result": RESULT_WRONG,
        "result_evidence": [{"kind": "window_miss"}],
    }
    write_decision_records(path, [old])
    revised = {
        "decision_id": "dec_20260904_600000_buy",
        "judgment": "当底仓",
        "planned_hold_days": 60,
        "verification": {"kind": "stand_above", "n_sessions": 60, "level": 10.4},
        "result": RESULT_PENDING,
        "structured_at": "2026-09-20T12:00:00+08:00",
    }
    merged = upsert_decision_records(path, [revised])
    kept = [row for row in merged if row["decision_id"] == "dec_20260904_600000_buy"]
    assert len(kept) == 1
    assert kept[0]["result"] == RESULT_WRONG
    assert kept[0]["planned_hold_days"] == 2
    kids = [row for row in merged if row.get("revises") == "dec_20260904_600000_buy"]
    assert len(kids) == 1
    assert kids[0]["planned_hold_days"] == 60
    assert kids[0]["result"] == RESULT_PENDING


def test_owner_page_escapes_html_and_marks_backfill(tmp_path: Path) -> None:
    html_path = tmp_path / "page.html"
    md_path = tmp_path / "page.md"
    render_owner_page(
        [
            {
                "result": RESULT_WRONG,
                "decision_date": "2026-09-02",
                "symbol": "600002",
                "side": "buy",
                "style": "短线",
                "planned_hold_days": 3,
                "judgment": "<script>alert(1)</script>",
                "verification": {"text": "<img src=x>"},
                "prompt_version": "v1.0.0",
                "menu_version": "v1.0.0",
                "backfill": True,
                "recorded_at": "",
            }
        ],
        html_path=html_path,
        md_path=md_path,
    )
    text = html_path.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "历史复盘" in text
    assert "事前记录" in md_path.read_text(encoding="utf-8")


def test_expired_window_with_missing_bars_is_not_pending(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_daily(daily, "600000", [("2026-09-01", 10.0, 10.0)])
    scored = score_decision(
        {
            "symbol": "600000",
            "side": "buy",
            "result": RESULT_PENDING,
            "trade_date": "2026-09-02",
            "verification": {
                "kind": "stand_above",
                "n_sessions": 2,
                "level": 10.4,
                "due_date": "2026-09-03",
            },
        },
        daily_dir=daily,
        as_of="2026-09-20",
    )
    assert scored["result"] == RESULT_EXPIRED_NO_EVIDENCE
