"""Owner-visible facts stay separate from model success and mature outcomes."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask
import pytest


def record(decision_id: str = "early-rule", **changes):
    value = {
        "decision_id": decision_id, "symbol": "603468", "decision_date": "2026-09-15",
        "recorded_at": "2026-09-15T20:26:14+08:00", "provenance": "prior_record",
        "backfill": False, "committed": True, "llm_used": False,
        "result": "对", "result_as_of": "2026-09-16",
        "verification": {"kind": "close_above_within", "due_date": "2026-09-22", "level": 21.73},
        "judgment": "临时测试记录", "result_evidence": [{"kind": "window_hit", "close": 21.74}],
    }
    value.update(changes)
    return value


def decisions_response(tmp_path, monkeypatch, records, as_of="2026-09-21"):
    from scripts.server.routes import ai_trader as routes
    path = tmp_path / "decision_latest.json"
    original = json.dumps({"schema": "ai_trader_decision_v1", "count": len(records),
                           "as_of": as_of, "records": records}, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)
    monkeypatch.setattr(routes, "DECISION_LATEST_PATH", path)
    app = Flask(__name__)
    app.register_blueprint(routes.create_ai_trader_blueprint())
    response = app.test_client().get("/api/ai-trader/decisions")
    assert response.status_code == 200
    assert path.read_bytes() == original, "Viewing must not rescore or rewrite history"
    return response.get_json()


def test_early_hit_is_preserved_but_not_a_mature_model_success(tmp_path, monkeypatch):
    row = record()
    payload = decisions_response(tmp_path, monkeypatch, [row])
    assert payload["records"] == [row]
    summary = payload["review_summary"]
    assert summary["groups"]["rule"]["early_hits"] == 1
    assert summary["groups"]["rule"]["mature_evaluable"] == 0
    assert summary["groups"]["model"]["mature_evaluable"] == 0
    assert summary["groups"]["rule"]["accuracy"] is None
    assert summary["record_context"]["early-rule"]["observation_status"] == "early_hit"


def test_maturity_uses_published_as_of_not_the_computer_clock(tmp_path, monkeypatch):
    before = decisions_response(tmp_path, monkeypatch, [record()], "2026-09-21")
    after = decisions_response(tmp_path, monkeypatch, [record()], "2026-09-22")
    assert before["review_summary"]["groups"]["rule"]["mature_evaluable"] == 0
    group = after["review_summary"]["groups"]["rule"]
    assert group["mature_evaluable"] == group["mature_correct"] == 0
    assert group["diagnostic_mature"] == 1
    assert "verdict_before_maturity" in after["review_summary"]["record_context"]["early-rule"]["exclusion_reasons"]
    assert group["early_hits"] == 0
    assert before["records"] == after["records"], "A mature observation must not change the early hit"


def test_sources_history_and_missing_evidence_never_mix_in_the_model_denominator(tmp_path, monkeypatch):
    model = record("model", llm_used=True, decision_origin="model", llm_model="fixture-model",
                   ledger_id="fixture-book", plan_id="fixture-plan", side="buy",
                   trade_date="2026-09-21", recorded_at_source="original", result_as_of="2026-09-22",
                   verification={"kind": "stand_above", "n_sessions": 2, "due_date": "2026-09-22",
                                 "source": "model_declared", "level": 21.73, "price_basis": "raw"},
                   result_evidence=[{"kind": "window_hit", "price_basis": "raw",
                                     "due_date": "2026-09-22", "level": 21.73,
                                     "observed": [["2026-09-21", 21.74], ["2026-09-22", 21.80]]}],
                   fill_evidence={"status": "confirmed", "ledger_id": "fixture-book", "trades": [
                       {"plan_id": "fixture-plan", "symbol": "603468", "side": "buy", "date": "2026-09-21",
                        "quantity": 100, "price": 21.73, "price_basis": "raw", "evidence_id": "fixture-raw"}]})
    rows = [
        model, record("rule"),
        record("history", llm_used=True, backfill=True),
        record("unknown", llm_used=None),
        record("missing", llm_used=True, decision_origin="model", result="已到期 · 证据不足", result_as_of="2026-09-22"),
        record("rejected", llm_used=True, decision_origin="model", committed=False),
        record("hold", llm_used=True, decision_origin="model", symbol="", result="不适用"),
    ]
    summary = decisions_response(tmp_path, monkeypatch, rows, "2026-09-22")["review_summary"]
    assert summary["historical_records"] == 1
    assert summary["groups"]["model"]["mature_evaluable"] == 1
    for origin in ("rule", "unknown"):
        assert summary["groups"][origin]["mature_evaluable"] == 0
    assert summary["credible_model"]["denominator"] == 1
    assert "实际纸面成交" in summary["denominator_note"]
    assert summary["groups"]["model"]["due_evidence_gap"] == 1
    assert summary["groups"]["model"]["unaccepted"] == 1
    assert summary["groups"]["model"]["not_applicable"] == 1


@pytest.mark.parametrize("as_of", [None, "", "2026-02-30", "2026-09-22T00:01:00+08:00"])
def test_missing_or_non_date_cutoff_is_unknown_not_zero_percent(tmp_path, monkeypatch, as_of):
    summary = decisions_response(tmp_path, monkeypatch, [record()], as_of)["review_summary"]
    assert summary["maturity_known"] is False
    assert summary["groups"]["rule"]["mature_evaluable"] is None
    assert summary["groups"]["rule"]["accuracy"] is None


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def status_response(root: Path):
    from scripts.server.routes.system import create_system_blueprint
    app = Flask(__name__)
    app.config["QUANTDESK_PROJECT_ROOT"] = str(root)
    app.register_blueprint(create_system_blueprint(build_id="fixture", asset_versions_func=lambda: {}))
    return app.test_client().get("/api/project-status").get_json()


def test_home_separates_model_reply_from_task_success_and_small_quote_gaps(tmp_path):
    (tmp_path / "STATUS.md").write_text(
        "# STATUS · 更新于 2026-09-21 22:30 · 更新者：health_line\n\n## 今日健康行\n快照 ✓48/48 · AI账 ✓0笔\n",
        encoding="utf-8",
    )
    for profile, used, reason in (("ai_trader", True, ""), ("ai_trader_glm", False, "transport:URLError")):
        write_json(tmp_path / f"results/{profile}/cycle_latest.json", {
            "ok": True, "as_of": "2026-09-21", "plan": {"decision_date": "2026-09-21", "committed": True,
                "llm": {"used": used, "model": "fixture-model", "unavailable_reason": reason,
                        "raw_response": "DO_NOT_EXPOSE_THIS_RESPONSE"}},
        })
    write_json(tmp_path / "data/snapshots/2026-09-21/manifest.json", {
        "trade_date": "2026-09-21", "collected_slot_count": 48, "expected_slot_count": 48,
        "partial_slot_count": 48, "completed_slot_count": 0,
        "quoted_row_count": 267072, "valid_quote_count": 266780,
    })
    original = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    payload = status_response(tmp_path)
    facts = payload["runtime_observations"]
    assert facts["profiles"]["default"]["model_replied"] is True
    assert facts["profiles"]["glm"]["model_replied"] is False
    assert "固定规则" in facts["profiles"]["glm"]["message"]
    snapshot = facts["snapshot"]
    assert snapshot["collected_slots"] == snapshot["expected_slots"] == 48
    assert snapshot["invalid_quotes"] == 292
    assert snapshot["valid_quote_ratio"] == pytest.approx(266780 / 267072)
    assert snapshot["blocks_workflow"] is False
    assert "整日不合格" not in snapshot["message"]
    assert "DO_NOT_EXPOSE_THIS_RESPONSE" not in json.dumps(payload)
    assert {p: p.read_bytes() for p in original} == original


def test_missing_runtime_results_do_not_look_like_successful_zero_trade_days(tmp_path):
    facts = status_response(tmp_path)["runtime_observations"]
    assert facts["profiles"]["glm"]["model_replied"] is None
    assert facts["snapshot"]["valid_quote_ratio"] is None
    assert facts["snapshot"]["collected_slots"] is None


def test_observations_do_not_echo_secret_error_text_or_invent_valid_ratios(tmp_path):
    write_json(tmp_path / "results/ai_trader_glm/cycle_latest.json", {
        "as_of": "2026-09-21", "plan": {"llm": {"used": False,
            "unavailable_reason": "https://private.invalid/?key=SHOULD_NOT_LEAK"}},
    })
    write_json(tmp_path / "data/snapshots/2026-09-21/manifest.json", {
        "collected_slot_count": 48, "expected_slot_count": 48,
        "quoted_row_count": 10, "valid_quote_count": 11,
    })
    facts = status_response(tmp_path)["runtime_observations"]
    assert "SHOULD_NOT_LEAK" not in json.dumps(facts)
    assert facts["snapshot"]["valid_quote_ratio"] is None
    assert facts["snapshot"]["invalid_quotes"] is None


def test_ui_explains_early_hit_and_rule_origin_without_changing_the_record():
    from tests.test_ai_trader_review_profiles import run_ui
    run_ui(r"""
        const payload = decision('default');
        payload.records[0].llm_used = false;
        payload.records[0].result = '对';
        payload.records[0].verification = {due_date: '2026-09-22'};
        payload.review_summary = {
            as_of: '2026-09-21', maturity_known: true, historical_records: 0,
            groups: {rule: {label: '固定规则', mature_evaluable: 0, mature_correct: 0,
                mature_wrong: 0, early_hits: 1, pending: 0, maturity_unknown: 0}},
            record_context: {default: {origin: 'rule', origin_label: '固定规则',
                observation_status: 'early_hit', label: '提前命中，观察期尚未结束'}},
        };
        const original = JSON.stringify(payload.records);
        reply = (url) => url.startsWith('/api/ai-trader/decisions') ? payload :
            url === '/api/decision-board' ? {watchlist: [], events: []} : {filled_trades: []};
        await loadDecisionHistory();
        assert.match(node('decisionHistoryPanel').innerHTML, /固定规则/);
        assert.match(node('decisionHistoryPanel').innerHTML, /提前命中/);
        assert.match(node('decisionHistoryPanel').innerHTML, /不计算正确率/);
        assert.match(node('decisionHistoryPanel').innerHTML, /fixture-default/,
            'A nonempty review must open on existing records, not an empty mismatch filter');
        assert.equal(JSON.stringify(payload.records), original);
    """)


def test_newer_cycle_date_is_not_hidden_by_an_older_status_document(tmp_path):
    (tmp_path / "STATUS.md").write_text(
        "# STATUS · 更新于 2026-09-19 22:30 · 更新者：health_line\n\n## 今日健康行\n旧记录\n", encoding="utf-8")
    write_json(tmp_path / "results/ai_trader/cycle_latest.json", {
        "as_of": "2026-09-21", "plan": {"llm": {"used": True, "model": "moonshotai/kimi-k3"}}})
    write_json(tmp_path / "data/snapshots/2026-09-21/manifest.json", {
        "trade_date": "2026-09-21", "collected_slot_count": 48, "expected_slot_count": 48,
        "quoted_row_count": 100, "valid_quote_count": 99})
    facts = status_response(tmp_path)["runtime_observations"]
    assert facts["snapshot"]["as_of"] == "2026-09-21"
    assert facts["snapshot"]["collected_slots"] == 48
    assert facts["profiles"]["default"]["model_label"] == "Kimi"


def test_model_response_does_not_imply_its_statement_was_accepted(tmp_path):
    write_json(tmp_path / "results/ai_trader/cycle_latest.json", {
        "as_of": "2026-09-21", "plan": {"committed": False, "llm": {"used": True}}})
    model = status_response(tmp_path)["runtime_observations"]["profiles"]["default"]
    assert model["model_replied"] is True
    assert model["decision_accepted"] is False
    assert "未被接收" in model["message"]


def test_home_cards_keep_small_gaps_and_fallback_visible_in_chinese():
    from tests.test_ai_trader_review_profiles import run_ui
    run_ui(r"""
        renderOwnerRuntimeSummary({
            snapshot: {as_of:'2026-09-21', collected_slots:48, expected_slots:48,
                valid_quote_ratio:0.9989, message:'少量缺价只作提示'},
            profiles: {default:{as_of:'2026-09-21',model_replied:true, model_label:'Kimi',message:'模型已回应'},
                glm:{as_of:'2026-09-21',model_replied:false,message:'连接失败；固定规则接管'}},
        });
        const rendered = node('projectRuntimeSummary').innerHTML;
        assert.match(rendered, /已收到 48 \/ 48 份/);
        assert.match(rendered, /严格时段合格 未核实/);
        assert.match(rendered, /99\.89%/);
        assert.match(rendered, /Kimi/);
        assert.match(rendered, /固定规则接管/);
        assert.match(rendered, /本轮不是模型判断/);
        assert.doesNotMatch(rendered, /整日不合格|采集失败/);
        renderOwnerRuntimeSummary(null);
        assert.doesNotMatch(node('projectRuntimeSummary').innerHTML, /99\.89%/);
        assert.match(node('projectRuntimeSummary').innerHTML, /尚未读取/);
        assert.equal(requests.length, 0, 'Rendering must not run any task or model');
    """)


def test_full_day_manifest_over_four_mib_still_reports_its_small_summary(tmp_path):
    write_json(tmp_path / "results/ai_trader/cycle_latest.json", {
        "as_of": "2026-09-21", "plan": {"llm": {"used": True}}})
    write_json(tmp_path / "data/snapshots/2026-09-21/manifest.json", {
        "trade_date": "2026-09-21", "collected_slot_count": 48, "expected_slot_count": 48,
        "quoted_row_count": 267072, "valid_quote_count": 266780,
        "retained_attempt_detail": "x" * (5 * 1024 * 1024)})
    facts = status_response(tmp_path)["runtime_observations"]
    assert facts["snapshot"]["collected_slots"] == 48
    assert facts["snapshot"]["valid_quote_ratio"] == pytest.approx(266780 / 267072)
    assert len(json.dumps(facts)) < 10000


def test_glm_book_does_not_relabel_copied_kimi_history_as_glm_judgment():
    from tests.test_ai_trader_review_profiles import run_ui
    run_ui(r"""
        aiTraderProfile = 'glm';
        const payload = decision('glm');
        payload.records[0].llm_used = true;
        payload.records[0].llm_model = 'moonshotai/kimi-k3';
        reply = (url) => url.startsWith('/api/ai-trader/decisions') ? payload :
            url === '/api/decision-board' ? {watchlist: [], events: []} : {filled_trades: []};
        await loadDecisionHistory();
        const rendered = node('decisionHistoryPanel').innerHTML;
        assert.match(rendered, /Kimi 调用/);
        assert.match(rendered, /意图来源未知/);
        assert.doesNotMatch(rendered, /GLM 模型/);
        assert.match(node('decisionHistoryNote').textContent, /GLM/);
        payload.records[0].decision_origin = 'model';
        await loadDecisionHistory();
        assert.match(node('decisionHistoryPanel').innerHTML, /Kimi 模型/);
    """)
