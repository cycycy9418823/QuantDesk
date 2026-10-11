"""QuantDesk 8000 integration surface for the independent AI paper trader.

Locks down the read-only ``/api/ai-trader/*`` routes added to the main 8000
server:

- dashboard payload structure, degradation on missing/corrupt artifacts;
- strict proposed vs committed vs filled vs rejected separation (the exact
  failure mode where the journal headline listed an LLM proposal as if it had
  been submitted);
- explain-only chat: bounded questions, structured failures, and proof that a
  chat turn can never modify any ledger, plan, journal or the formal QuantDesk
  book;
- frontend entry + all seven safety badges, and no order/broker buttons;
- supported 8000/8001 compatibility entry points stay intact.

No test here talks to the network: every LLM touchpoint is monkeypatched.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd
import pytest

import ai_trader.chat as chat_mod
import ai_trader.dashboard as dash_mod
from ai_trader.cycle import CyclePaths, run_cycle
from ai_trader.journal import build_journal
from ai_trader.overnight import llm_journal_headline
from ai_trader.paths import FORBIDDEN_QUANTDESK_LEDGER
from scripts.server.routes.ai_trader import (
    create_ai_trader_blueprint,
)

# --------------------------------------------------------------------------
# Fixtures: a small but realistic copy of the authoritative artifact shapes.
# --------------------------------------------------------------------------


def _ledger_fixture() -> dict:
    return {
        "schema_version": "ai_trader_ledger_v1",
        "ledger_id": "ai_trader_100k",
        "as_of": "2026-08-25",
        "updated_at": "2026-08-25T20:21:37+08:00",
        "initial_cash": 100000.0,
        "cash": 84478.093,
        "equity": 101068.093,
        "market_value": 16590.0,
        "unrealized_pnl": 1068.09,
        "realized_pnl": 0.0,
        "positions": [
            {
                "symbol": "600032",
                "quantity": 1200,
                "avg_cost": 6.777619,
                "market_price": 6.83,
                "market_value": 8196.0,
                "unrealized_pnl": 62.86,
                "style": "中线",
                "planned_hold_days": 60,
                "acquired_on": "2026-08-24",
            }
        ],
        "trades": [
            {
                "date": "2026-08-25",
                "symbol": "600172",
                "side": "buy",
                "quantity": 600,
                "price": 12.3,
            }
        ],
        "boundary": {"no_broker": True},
    }


def _plan_fixture(*, picked: list[str] | None = None, intents: list[dict] | None = None) -> dict:
    picks = ["600018", "600346"] if picked is None else picked
    rows = intents
    if rows is None:
        rows = [
            {
                "symbol": "600018",
                "side": "buy",
                "quantity": 1500,
                "style": "长期",
                "planned_hold_days": 250,
                "pick_source": "llm_self_selected",
                "pick_source_zh": "AI自选",
            }
        ]
    return {
        "schema_version": "ai_trader_plan_v1",
        "plan_id": "plan_20260825_for_20260826",
        "decision_date": "2026-08-25",
        "trade_date": "2026-08-26",
        "status": "pending_open",
        "committed": True,
        "conviction": "probation",
        "intents": rows,
        "llm": {
            "used": True,
            "model": "claude-opus-5",
            "market_note": "低吸现金流公司。",
            "picked": picks,
            "styles": {sym: "中线" for sym in picks},
            "sells_requested": [],
            "unavailable_reason": "",
        },
        "recommendation_view": {
            "symbols": ["600577", "600346"],
            "count": 2,
            "warnings": [],
            "advisory_only": True,
        },
    }


def _journal_fixture() -> dict:
    return {
        "as_of": "2026-08-25",
        "trade_date": "2026-08-26",
        "headline": "明天（2026-08-26）AI自己拿主意：提交买入 600018。",
        "thought": "低吸现金流公司。",
        "lessons": ["理由变了就换。"],
        "filled_count": 0,
        "rejected_count": 0,
        "llm": {"used": True, "model": "claude-opus-5", "unavailable_reason": ""},
    }


def _philosophy_fixture() -> dict:
    return {
        "schema_version": "ai_trader_philosophy_v1",
        "stance": {
            "summary": "少动手、拿得住。",
            "preferred_styles": ["长期", "中线"],
            "reasoning": "成本后边际薄，拿住省换手。",
        },
        "revisions": [{"on": "2026-08-21", "summary": "旧主张"}],
        "style_ledger": {
            "中线": {
                "trades": 2,
                "wins": 1,
                "sum_net": 0.01,
                "mean_net": 0.005,
                "win_rate": 0.5,
                "mean_hold_days": 30.0,
            }
        },
        "notes": [{"on": "2026-08-25", "note": "少交易。"}],
    }


def _cycle_fixture() -> dict:
    return {
        "ok": True,
        "as_of": "2026-08-25",
        "plan": {
            "llm": {
                "used": True,
                "model": "claude-opus-5",
                "unavailable_reason": "",
                "picked": ["600018", "600346"],
            }
        },
    }


def _write_state(tmp_path: Path, *, plan: dict | None = None) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "decision_latest.json").write_text('{"records": []}', encoding="utf-8")
    (state / "ledger.json").write_text(
        json.dumps(_ledger_fixture(), ensure_ascii=False), encoding="utf-8"
    )
    (state / "plan_latest.json").write_text(
        json.dumps(plan if plan is not None else _plan_fixture(), ensure_ascii=False),
        encoding="utf-8",
    )
    (state / "journal_latest.json").write_text(
        json.dumps(_journal_fixture(), ensure_ascii=False), encoding="utf-8"
    )
    (state / "philosophy.json").write_text(
        json.dumps(_philosophy_fixture(), ensure_ascii=False), encoding="utf-8"
    )
    (state / "review_latest.json").write_text(
        json.dumps(
            {
                "score_summary": {"intents": 1, "open_fills": 1, "abandoned": 0},
                "actions": [
                    {"date": "2026-08-25", "symbol": "600172", "side": "buy", "status": "filled"},
                    {
                        "date": "2026-08-25",
                        "symbol": "600999",
                        "side": "buy",
                        "status": "rejected",
                        "reason": "持仓只数已满。",
                    },
                    {
                        "date": "2026-08-25",
                        "symbol": "600998",
                        "side": "buy",
                        "status": "abandoned",
                        "reason": "高开放弃。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "cycle_latest.json").write_text(
        json.dumps(_cycle_fixture(), ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    _write_state(tmp_path)
    monkeypatch.setattr(dash_mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dash_mod, "CYCLE_LATEST_PATH", tmp_path / "cycle_latest.json")
    return tmp_path


@pytest.fixture()
def api_client(isolated_state):
    from scripts import quant_api_server

    quant_api_server.app.config.update(TESTING=True)
    return quant_api_server.app.test_client()


REQUIRED_DASHBOARD_KEYS = {
    "ok",
    "degraded",
    "warnings",
    "boundary",
    "account",
    "positions",
    "latest_journal",
    "latest_plan",
    "stance",
    "style_ledger",
    "recommendation_view",
    "proposed_symbols",
    "committed_intents",
    "uncommitted_intents",
    "llm",
    "llm_runtime",
    "source_paths",
    "updated_at",
}


def _redirect_chat_log(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "chat-test.log.jsonl"
    monkeypatch.setattr(chat_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(chat_mod, "CHAT_LOG_PATH", path)
    return path


# --------------------------------------------------------------------------
# Dashboard API
# --------------------------------------------------------------------------


class TestAiTraderDashboardApi:
    def test_normal_payload_structure(self, api_client) -> None:
        resp = api_client.get("/api/ai-trader/dashboard")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert REQUIRED_DASHBOARD_KEYS <= set(payload.keys())
        assert payload["ok"] is True
        assert payload["degraded"] is False
        # Passthrough, never recomputed:
        assert payload["account"]["equity"] == 101068.093
        assert payload["account"]["cash"] == 84478.093
        assert payload["account"]["positions_count"] == len(payload["positions"]) == 1
        assert payload["positions"][0]["symbol"] == "600032"
        assert str(payload["source_paths"]["ledger"]).replace("\\", "/").endswith(
            "state/ledger.json"
        )
        boundary = payload["boundary"]
        assert boundary["research_only"] is True
        assert boundary["paper_account_only"] is True
        assert boundary["no_broker"] is True
        assert boundary["is_real_order"] is False
        assert boundary["chat_can_not_trade"] is True

    def test_llm_runtime_descriptor_present_and_secret_free(self, api_client) -> None:
        payload = api_client.get("/api/ai-trader/dashboard").get_json()
        runtime = payload["llm_runtime"]
        assert isinstance(runtime, dict)
        # Route-level clean_payload renders JSON null as 数据缺失.
        # This is an explicit privacy contract, not a generic missing-data
        # field: the transport token must serialize as JSON null, never as a
        # placeholder string that could be confused with a credential value.
        assert runtime["token"] is None
        # token_present may be true/false depending on this machine's gitignored
        # config; the SAFETY invariant is that no credential string appears in
        # the serialized dashboard response at all.
        assert isinstance(runtime["token_present"], bool)
        assert isinstance(runtime["endpoint_host"], str)
        assert runtime["provider"] in {"openai", "anthropic"}
        assert isinstance(runtime["context_disclosure"], list) and runtime["context_disclosure"]
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "sk-" not in serialized
        assert "Bearer" not in serialized
        assert "authorization" not in serialized.lower()
        assert "nvapi" not in serialized.lower()

    def test_missing_files_degrade_without_crash(self, monkeypatch, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(dash_mod, "STATE_DIR", empty)
        monkeypatch.setattr(dash_mod, "CYCLE_LATEST_PATH", empty / "cycle_latest.json")
        from scripts import quant_api_server

        resp = quant_api_server.app.test_client().get("/api/ai-trader/dashboard")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is True
        assert payload["degraded"] is True
        joined = ",".join(payload["warnings"])
        assert "ledger_missing" in joined
        assert "plan_missing" in joined
        assert "journal_missing" in joined
        # Route-level clean_payload renders missing numbers as 数据缺失.
        assert payload["account"]["equity"] == "数据缺失"
        assert payload["positions"] == []

    def test_corrupt_json_degrades_without_crash(self, monkeypatch, tmp_path) -> None:
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "ledger.json").write_text("{not-json", encoding="utf-8")
        monkeypatch.setattr(dash_mod, "STATE_DIR", broken)
        monkeypatch.setattr(dash_mod, "CYCLE_LATEST_PATH", broken / "cycle_latest.json")
        from scripts import quant_api_server

        resp = quant_api_server.app.test_client().get("/api/ai-trader/dashboard")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["degraded"] is True
        assert any("ledger_corrupt" in w for w in payload["warnings"])

    def test_proposed_is_never_reported_as_committed(self, api_client) -> None:
        payload = api_client.get("/api/ai-trader/dashboard").get_json()
        stages = {row["symbol"]: row["stage"] for row in payload["proposed_symbols"]}
        # Two proposals; account constraints kept only one.
        assert stages == {"600018": "committed", "600346": "proposed_only"}
        assert payload["proposed_not_committed_symbols"] == ["600346"]
        assert payload["committed_buy_symbols"] == ["600018"]
        symbols_in_intents = {row["symbol"] for row in payload["committed_intents"]}
        assert symbols_in_intents == {"600018"}
        # And the API says so explicitly instead of letting the UI guess:
        assert any("600346" in w for w in payload["warnings"])

    def test_llm_used_model_and_reason_pass_through_verbatim(self, api_client) -> None:
        payload = api_client.get("/api/ai-trader/dashboard").get_json()
        llm = payload["llm"]
        assert llm["used"] is True
        assert llm["model"] == "claude-opus-5"
        assert llm["unavailable_reason"] == ""

    def test_stale_data_flagged(self, isolated_state) -> None:
        journal_path = isolated_state / "journal_latest.json"
        stale = _journal_fixture()
        stale["as_of"] = "2026-08-20"
        journal_path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
        from datetime import datetime

        from ai_trader.dashboard import build_dashboard_payload

        payload = build_dashboard_payload(
            state_dir=isolated_state,
            results_dir=isolated_state,
            now=datetime(2026, 8, 26),
        )
        assert payload["schedule_status"]["stale"] is True
        assert any(w.startswith("ai_trader_data_stale") for w in payload["warnings"])

    def test_filled_and_rejected_actions_are_separated(self, api_client) -> None:
        payload = api_client.get("/api/ai-trader/dashboard").get_json()
        assert [row["symbol"] for row in payload["filled_trades"]] == ["600172"]
        rejected = {row["symbol"]: row["status"] for row in payload["rejected_or_abandoned"]}
        assert rejected == {"600999": "rejected", "600998": "abandoned"}

    def test_assembly_failure_returns_structured_degraded_payload(self, monkeypatch) -> None:
        def boom(**kwargs):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(dash_mod, "build_dashboard_payload", boom)
        blueprint = create_ai_trader_blueprint()
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(blueprint)
        resp = app.test_client().get("/api/ai-trader/dashboard")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is False
        assert payload["degraded"] is True
        assert any("dashboard_assembly_failed" in w for w in payload["warnings"])


# --------------------------------------------------------------------------
# Uncommitted plan: fail-closed classification (P1 regression).
# --------------------------------------------------------------------------


def _uncommitted_plan_fixture() -> dict:
    plan = _plan_fixture(
        picked=["600000"],
        intents=[{"symbol": "600000", "side": "buy", "quantity": 100}],
    )
    plan["committed"] = False
    plan["not_committed_reason"] = "上一份计划还没结算，今晚不叠加新纸面委托。"
    return plan


class TestUncommittedPlanFailClosed:
    def test_uncommitted_plan_never_reports_committed_intents(
        self, isolated_state
    ) -> None:
        """Deferred plans keep their intents visible for audit ONLY."""

        (isolated_state / "state" / "plan_latest.json").write_text(
            json.dumps(_uncommitted_plan_fixture(), ensure_ascii=False),
            encoding="utf-8",
        )
        from ai_trader.dashboard import build_dashboard_payload

        payload = build_dashboard_payload(
            state_dir=isolated_state / "state",
            results_dir=isolated_state,
        )
        assert payload["latest_plan"]["committed"] is False
        assert payload["latest_plan"]["intents_count"] == 0
        assert payload["committed_intents"] == []
        assert payload["committed_buy_symbols"] == []
        assert payload["uncommitted_intents"][0]["symbol"] == "600000"
        assert payload["proposed_symbols"][0]["stage"] == "proposed_only"
        assert payload["proposed_symbols"][0]["stage_zh"] == "仅提议·未提交"
        assert payload["proposed_not_committed_symbols"] == ["600000"]

    def test_missing_committed_field_fails_closed(self, isolated_state) -> None:
        plan = _uncommitted_plan_fixture()
        plan.pop("committed")
        (isolated_state / "state" / "plan_latest.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8"
        )
        from ai_trader.dashboard import build_dashboard_payload

        payload = build_dashboard_payload(
            state_dir=isolated_state / "state",
            results_dir=isolated_state,
        )
        assert payload["latest_plan"]["committed"] is False
        assert payload["committed_intents"] == []
        assert payload["committed_buy_symbols"] == []
        assert len(payload["uncommitted_intents"]) == 1
        assert payload["proposed_symbols"][0]["stage"] == "proposed_only"

    def test_committed_true_path_still_reports_intents(self, api_client) -> None:
        payload = api_client.get("/api/ai-trader/dashboard").get_json()
        assert payload["latest_plan"]["committed"] is True
        assert payload["latest_plan"]["intents_count"] == 1
        assert payload["committed_buy_symbols"] == ["600018"]
        assert payload["committed_intents"][0]["symbol"] == "600018"
        assert payload["uncommitted_intents"] == []
        stages = {row["symbol"]: row["stage"] for row in payload["proposed_symbols"]}
        assert stages["600018"] == "committed"

    def test_api_output_matches_direct_adapter_output(
        self, monkeypatch, tmp_path, isolated_state
    ) -> None:
        """The HTTP layer must not reinterpret what the adapter classified."""

        from scripts import quant_api_server

        for plan in (_plan_fixture(), _uncommitted_plan_fixture()):
            root = tmp_path / ("committed" if plan.get("committed") else "deferred")
            root.mkdir()
            _write_state(root, plan=plan)
            monkeypatch.setattr(dash_mod, "STATE_DIR", root / "state")
            monkeypatch.setattr(dash_mod, "CYCLE_LATEST_PATH", root / "cycle_latest.json")
            api_payload = (
                quant_api_server.app.test_client()
                .get("/api/ai-trader/dashboard")
                .get_json()
            )
            direct = dash_mod.build_dashboard_payload(
                state_dir=root / "state", results_dir=root
            )
            for key in (
                "proposed_not_committed_symbols",
                "committed_buy_symbols",
                "uncommitted_intents",
            ):
                assert api_payload[key] == direct[key], key
            assert [
                (row["symbol"], row["stage"], row["stage_zh"])
                for row in api_payload["proposed_symbols"]
            ] == [
                (row["symbol"], row["stage"], row["stage_zh"])
                for row in direct["proposed_symbols"]
            ]
            assert api_payload["latest_plan"]["committed"] is direct["latest_plan"]["committed"]
            assert api_payload["latest_plan"]["intents_count"] is direct["latest_plan"]["intents_count"]

    def test_frontend_renders_uncommitted_without_submitted_claim(self) -> None:
        js = FRONTEND_JS.read_text(encoding="utf-8")
        start = js.index("function renderAiTraderProposed")
        end = js.index("function renderAiTraderFills")
        block = js[start:end]
        # Committed section renders only backend-certified intents...
        assert "payload.committed_intents" in block
        # ...keeps the committed=false annotation and reason...
        assert "committed=false" in block
        assert "not_committed_reason" in block
        # ...shows withheld intents under an explicit audit-only label...
        assert "uncommitted_intents" in block
        assert "保留但未提交的 intents" in block
        # ...and never hardcodes an 已提交 claim of its own (labels come from
        # backend stage_zh values).
        assert "已提交" not in block

    def test_deferred_journal_copy_never_claims_commit(self) -> None:
        note = build_journal(
            as_of="2026-08-25",
            research={"live": None, "evaluated": []},
            actions=[],
            ledger={"equity": 100000.0, "cash": 100000.0, "initial_cash": 100000.0, "positions": []},
            warnings=[],
            trade_date="2026-08-26",
            llm={
                "used": True,
                "model": "test-model",
                "picked": ["600000"],
                "styles": {"600000": "波段"},
                "sells_requested": [],
                "unavailable_reason": "",
            },
            committed_buy_symbols=[],
        )
        assert "提交买入" not in note["headline"]
        assert "600000" in note["headline"]
        assert "未通过账户约束" in note["headline"]

    def test_llm_provider_does_not_affect_uncommitted_classification(
        self, isolated_state, monkeypatch
    ) -> None:
        """The external-LLM descriptor must never change P1 semantics."""

        (isolated_state / "state" / "plan_latest.json").write_text(
            json.dumps(_uncommitted_plan_fixture(), ensure_ascii=False),
            encoding="utf-8",
        )
        from ai_trader.dashboard import build_dashboard_payload

        payload = build_dashboard_payload(
            state_dir=isolated_state / "state",
            results_dir=isolated_state,
        )
        assert payload["llm_runtime"]["provider"] in {"openai", "anthropic"}
        assert payload["latest_plan"]["committed"] is False
        assert payload["committed_intents"] == []
        assert payload["committed_buy_symbols"] == []
        assert payload["proposed_symbols"][0]["stage"] == "proposed_only"

        # And with a committed=true plan the same descriptor keeps true path.
        (isolated_state / "state" / "plan_latest.json").write_text(
            json.dumps(_plan_fixture(), ensure_ascii=False), encoding="utf-8"
        )
        payload2 = build_dashboard_payload(
            state_dir=isolated_state / "state",
            results_dir=isolated_state,
        )
        assert payload2["latest_plan"]["committed"] is True
        assert payload2["committed_buy_symbols"] == ["600018"]


# --------------------------------------------------------------------------
# Chat API: explain-only, bounded, structured failures, zero ledger impact.
# --------------------------------------------------------------------------


@pytest.fixture()
def chat_client(api_client, monkeypatch, tmp_path):
    """Client with the chat brain and its log redirected away from real state."""

    _redirect_chat_log(monkeypatch, tmp_path)
    monkeypatch.delenv("AI_TRADER_LLM_ENABLED", raising=False)
    return api_client


class TestAiTraderChatApi:
    def test_get_returns_bounded_history(self, chat_client) -> None:
        resp = chat_client.get("/api/ai-trader/chat")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is True
        assert isinstance(payload["messages"], list)
        assert payload["boundary_note"]

    def test_post_empty_question_is_400(self, chat_client) -> None:
        resp = chat_client.post("/api/ai-trader/chat", json={"question": "   "})
        assert resp.status_code == 400
        assert resp.get_json()["reason"] == "empty_question"

    def test_post_overlong_question_is_400(self, chat_client) -> None:
        resp = chat_client.post("/api/ai-trader/chat", json={"question": "问" * 5000})
        assert resp.status_code == 400
        assert resp.get_json()["reason"] == "question_too_long"

    def test_post_success_returns_answer_and_model(
        self, chat_client, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            chat_mod,
            "load_llm_config",
            lambda explicit_optin=False, diagnostics=None: {
                "model": "claude-opus-5",
                "base_url": "http://127.0.0.1:1",
                "token": "unused",
            },
        )
        monkeypatch.setattr(chat_mod, "load_history", lambda limit=8: [])
        monkeypatch.setattr(chat_mod, "_call", lambda *a, **k: "它只会在晚间流程里动仓位。")
        resp = chat_client.post(
            "/api/ai-trader/chat", json={"question": "你现在会卖 600172 吗？"}
        )
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is True
        assert payload["answer"]
        assert payload["model"] == "claude-opus-5"

    def test_post_failure_reason_visible_as_503(self, chat_client, monkeypatch) -> None:
        monkeypatch.setattr(
            chat_mod,
            "load_llm_config",
            lambda explicit_optin=False, diagnostics=None: {
                "model": "claude-opus-5",
                "base_url": "http://127.0.0.1:1",
                "token": "unused",
            },
        )
        monkeypatch.setattr(chat_mod, "load_history", lambda limit=8: [])

        def fail(system, user, cfg, diagnostics=None):
            if diagnostics is not None:
                diagnostics["reason"] = "transport:URLError"
            return None

        monkeypatch.setattr(chat_mod, "_call", fail)
        resp = chat_client.post("/api/ai-trader/chat", json={"question": "在吗"})
        assert resp.status_code == 503
        payload = resp.get_json()
        assert payload["ok"] is False
        assert payload["reason"] == "transport:URLError"

    def test_chat_external_endpoint_without_allow_surfaces_reason(
        self, chat_client, monkeypatch
    ) -> None:
        """A blocked external chat surfaces the REAL reason through the route."""

        def blocked(explicit_optin=False, diagnostics=None):
            if diagnostics is not None:
                diagnostics["reason"] = "external_llm_not_explicitly_allowed"
            return None

        monkeypatch.setattr(chat_mod, "load_llm_config", blocked)
        monkeypatch.setattr(chat_mod, "load_history", lambda limit=8: [])

        resp = chat_client.post("/api/ai-trader/chat", json={"question": "在吗"})
        assert resp.status_code == 503
        assert "external_llm_not_explicitly_allowed" in json.dumps(
            resp.get_json(), ensure_ascii=False
        )

    def test_chat_cannot_modify_any_ledger_or_plan(self, chat_client, monkeypatch) -> None:
        """The big tripwire: one chat turn, then byte-compare every state file."""

        def _hash_all() -> dict[str, str | None]:
            out: dict[str, str | None] = {}
            candidates = [FORBIDDEN_QUANTDESK_LEDGER] + sorted(
                p for p in chat_mod.STATE_DIR.glob("*") if p.is_file()
            )
            for path in candidates:
                out[str(path)] = (
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                )
            return out

        monkeypatch.setattr(
            chat_mod,
            "load_llm_config",
            lambda explicit_optin=False, diagnostics=None: {
                "model": "test-model",
                "base_url": "http://127.0.0.1:1",
                "token": "unused",
            },
        )
        monkeypatch.setattr(chat_mod, "load_history", lambda limit=8: [])
        monkeypatch.setattr(chat_mod, "_call", lambda *a, **k: "解释而已。")

        before = _hash_all()
        resp = chat_client.post(
            "/api/ai-trader/chat", json={"question": "帮我买一点 600000"}
        )
        assert resp.status_code == 200
        after = _hash_all()
        diff = [
            key
            for key in before
            if before[key] != after.get(key)
        ]
        assert diff == [], f"chat modified state files: {diff}"


# --------------------------------------------------------------------------
# Proposed vs committed: journal copy fix + regression.
# --------------------------------------------------------------------------


class TestProposedVsCommittedCopy:
    def test_headline_lists_only_committed_as_submitted(self) -> None:
        headline = llm_journal_headline(
            "2026-08-26",
            proposed=["600346", "600577"],
            committed_buy_symbols=["600346"],
            plan_committed=True,
        )
        assert "提交买入 600346" in headline
        assert "600577" in headline
        assert "未通过账户约束" in headline
        assert "未提交" in headline
        # The old conflation said 看上 A、B as if both were ordered:
        assert "看上 600346、600577" not in headline

    def test_headline_does_not_claim_commit_when_plan_not_committed(self) -> None:
        headline = llm_journal_headline(
            "2026-08-26",
            proposed=["600346"],
            committed_buy_symbols=["600346"],
            plan_committed=False,
        )
        assert "不叠加新纸面委托" in headline
        assert "提交买入" not in headline

    def test_headline_no_proposal_no_false_claim(self) -> None:
        headline = llm_journal_headline(
            "2026-08-26", proposed=[], committed_buy_symbols=[], plan_committed=True
        )
        assert "空仓" in headline

    def test_build_journal_body_marks_unsubmitted_proposal(self) -> None:
        note = build_journal(
            as_of="2026-08-25",
            research={"live": None, "evaluated": []},
            actions=[],
            ledger={"equity": 100000.0, "cash": 100000.0, "initial_cash": 100000.0, "positions": []},
            warnings=[],
            trade_date="2026-08-26",
            llm={
                "used": True,
                "model": "claude-opus-5",
                "picked": ["600346", "600577"],
                "styles": {"600346": "中线", "600577": "中线"},
                "sells_requested": [],
                "unavailable_reason": "",
            },
            committed_buy_symbols=["600346"],
        )
        assert "提交买入 600346(中线)" in note["headline"]
        assert "600577" in note["headline"]
        assert "未通过账户约束" in note["headline"]

    def _runtime(self, tmp_path: Path, daily: Path) -> CyclePaths:
        out = tmp_path / "runtime"
        # Use the authoritative artifact file names so the dashboard adapter
        # can read the produced runtime directly.
        return CyclePaths(
            daily_dir=daily,
            minute_dir=tmp_path / "minutes",
            ledger=out / "ledger.json",
            hypotheses=out / "hypotheses.json",
            journal_latest=out / "journal_latest.json",
            journal_log=out / "journal.jsonl",
            daytime_notes=out / "daytime_notes.jsonl",
            daytime_session=out / "daytime_session.json",
            daytime_intents=out / "daytime_intents.jsonl",
            plan_latest=out / "plan_latest.json",
            review_latest=out / "review_latest.json",
            watchlist=out / "watchlist.json",
            philosophy=out / "philosophy.json",
            cycle_latest=out / "cycle_latest.json",
            snapshot=out / "snapshot_latest.json",
            decision_log=out / "decision_log.jsonl",
            decision_latest=out / "decision_latest.json",
            decision_page=out / "decision_page.html",
            decision_page_md=out / "decision_page.md",
        )

    def _write_daily(self, root: Path, symbol: str, closes: list[float]) -> None:
        start = pd.Timestamp("2024-01-02")
        dates = pd.bdate_range(start, periods=len(closes))
        rows = ["date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"]
        prev = closes[0]
        for day, close in zip(dates, closes, strict=True):
            chg = 0.0 if prev == 0 else (close / prev - 1.0) * 100
            rows.append(
                f"{day.date().isoformat()},{symbol},{close},{close},{close},{close},1000,10000,{chg:.4f},raw"
            )
            prev = close
        (root / f"{symbol}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_nightly_cycle_separates_two_proposals_one_commit(
        self, tmp_path, monkeypatch
    ) -> None:
        """Two LLM proposals, probation allows one commit: headline + API agree."""

        import ai_trader.overnight as ovn

        daily = tmp_path / "daily"
        daily.mkdir()
        for i in range(8):
            closes = [20.0 - n * 0.05 for n in range(80)]
            self._write_daily(daily, f"60000{i}", closes)
        runtime = self._runtime(tmp_path, daily)
        from ai_trader import paths as ai_paths

        # Keep the real directory initializer active inside this test's root.
        monkeypatch.setattr(ai_paths, "STATE_DIR", runtime.ledger.parent)
        monkeypatch.setattr(ai_paths, "RESULTS_DIR", runtime.cycle_latest.parent)

        def fake_propose(context, diagnostics=None):
            return {
                "model": "test-model",
                "buys": [
                    {"symbol": "600000", "reason": "r1", "style": "波段", "hold_days": 10, "conviction": "probation"},
                    {"symbol": "600001", "reason": "r2", "style": "波段", "hold_days": 10, "conviction": "probation"},
                ],
                "sells": [],
                "stance": {"summary": "", "preferred_styles": [], "reasoning": ""},
                "lesson": "",
                "avoid": [],
                "market_note": "观望。",
            }

        # Keep the synthetic universe hermetic: without this, the real
        # results/auto_*_candidates_latest.json would consume focus slots.
        monkeypatch.setattr(
            ovn,
            "load_recommendations",
            lambda require_fresh_for=None: {
                "symbols": [],
                "rows": [],
                "warnings": [],
                "boundary": {"read_only": True, "advisory_only": True, "is_order": False},
            },
        )
        monkeypatch.setattr(ovn, "propose_picks", fake_propose)
        payload = run_cycle(paths=runtime, lookback=80, universe_limit=8, network=False)
        assert payload["ok"] is True
        plan = payload["plan"]
        committed_buys = [
            row["symbol"] for row in plan["intents"] if row.get("side") == "buy"
        ]
        assert plan["llm"]["picked"] == ["600000", "600001"]
        assert committed_buys == ["600000"]

        journal = json.loads(runtime.journal_latest.read_text(encoding="utf-8"))
        assert f"提交买入 {'、'.join(committed_buys)}" in journal["headline"]
        assert "600001" in journal["headline"]
        assert "未提交" in journal["headline"]
        # The proposal itself is preserved honestly under llm.picked:
        assert journal["llm"]["picked"] == ["600000", "600001"]

        # And the dashboard adapter classifies the same artifacts identically.
        from ai_trader.dashboard import build_dashboard_payload

        view = build_dashboard_payload(state_dir=runtime.ledger.parent, results_dir=runtime.ledger.parent)
        stages = {row["symbol"]: row["stage"] for row in view["proposed_symbols"]}
        assert stages == {"600000": "committed", "600001": "proposed_only"}
        assert view["proposed_not_committed_symbols"] == ["600001"]
        assert view["committed_buy_symbols"] == ["600000"]


# --------------------------------------------------------------------------
# Frontend wiring: entry, badges, no order buttons, loader registration.
# --------------------------------------------------------------------------


FRONTEND_HTML = Path(__file__).resolve().parents[1] / "app" / "templates" / "daily_research.html"
FRONTEND_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "daily_research.js"


def _section_html(template: str, section_id: str) -> str:
    match = re.search(
        rf'<section id="{section_id}".*?</section>', template, re.DOTALL
    )
    assert match, f"section {section_id} missing"
    return match.group(0)


class TestFrontendWiring:
    def test_entry_button_and_section_exist(self) -> None:
        template = FRONTEND_HTML.read_text(encoding="utf-8")
        assert 'data-side-module-target="ai-trader-section"' in template
        assert 'id="ai-trader-section"' in template
        js = FRONTEND_JS.read_text(encoding="utf-8")
        assert '"ai-trader-section": () => loadAiTraderPanel()' in js
        # Label drifted to "AI脑" in the 2026-09-11 UI; the guard is that the
        # section still has a module label, not the exact wording.
        assert re.search(r'"ai-trader-section":\s*"[^"]+"', js)

    def test_all_seven_safety_badges_inside_section(self) -> None:
        template = FRONTEND_HTML.read_text(encoding="utf-8")
        section = _section_html(template, "ai-trader-section")
        for badge in (
            "Research Only",
            "Paper Account Only",
            "No Broker Route",
            "Live Execution Locked",
            "QMT Order Route Locked",
            "Not Investment Advice",
            "Not Position Advice",
        ):
            assert badge in section, f"missing badge: {badge}"

    def test_chat_copy_states_it_cannot_trade(self) -> None:
        template = FRONTEND_HTML.read_text(encoding="utf-8")
        section = _section_html(template, "ai-trader-section")
        assert "聊天不能下单" in section
        assert "仓位只在每晚纸面复盘流程中变化" in section

    def test_section_has_no_order_or_scheduler_buttons(self) -> None:
        template = FRONTEND_HTML.read_text(encoding="utf-8")
        section = _section_html(template, "ai-trader-section")
        buttons = re.findall(r"<button[^>]*>(.*?)</button>", section, re.DOTALL)
        texts = [re.sub(r"<[^>]+>", "", b).strip() for b in buttons]
        assert texts, "expected at least the refresh/back/send buttons"
        forbidden = re.compile(r"(买入|卖出|下单|券商|委托|重跑|启动.*任务|停止.*任务)")
        offenders = [t for t in texts if forbidden.search(t)]
        assert offenders == [], f"order/scheduler-like buttons found: {offenders}"
        # Navigation/refresh/chat only. "策略账"/"AI账" are the two-book switch and
        # the profile buttons pick which book's data this panel reads; neither
        # runs a cycle. Keep this list explicit so an order button can never hide.
        allowed = {
            "返回首页", "刷新数据", "发送",
            "策略账", "AI账",
            "默认 · Kimi", "GLM · glm-5.3",
        }
        unexpected = [t for t in texts if t not in allowed]
        assert unexpected == [], f"unexpected buttons in AI trader section: {unexpected}"

    def test_js_never_recomputes_equity_or_positions(self) -> None:
        js = FRONTEND_JS.read_text(encoding="utf-8")
        window_start = js.index("setupAiTraderPanel")
        window_end = js.index("function appendAgentMessage")
        block = js[window_start:window_end]
        # Display-only arithmetic guard: formatting numbers is fine, deriving
        # account math in the browser is not.
        for banned in ("equity =", "+ position", "* quantity", "drawdown"):
            assert banned not in block

    def test_ui_marks_external_llm_and_data_leaves_device(self) -> None:
        js = FRONTEND_JS.read_text(encoding="utf-8")
        start = js.index("function renderAiTraderLlmRuntime")
        end = js.index("function renderAiTraderWarnings")
        block = js[start:end]
        # External and local markers are BOTH wired, selected from backend field.
        assert "External LLM" in block
        assert "Local LLM" in block
        assert "runtime.is_external" in block
        assert "Data Leaves This Device" in block
        assert "Endpoint host" in block
        assert "Provider" in block
        assert "Model" in block

    def test_ui_never_contains_local_llm_fixed_copy(self) -> None:
        template = FRONTEND_HTML.read_text(encoding="utf-8")
        assert "回答由已配置的本地 LLM 生成" not in template
        assert 'id="aiTraderLlmRuntimePanel"' in template
        assert 'id="aiTraderLlmDisclosurePanel"' in template


# --------------------------------------------------------------------------
# Legacy entries and non-regression of existing 8000 APIs.
# --------------------------------------------------------------------------


class TestCompatEntriesAndNonRegression:
    def test_port_8001_classroom_routes_intact(self) -> None:
        from ai_trader.app import create_app

        rules = {str(r) for r in create_app().url_map.iter_rules()}
        assert "/api/snapshot" in rules
        assert "/api/ledger" in rules
        assert "/api/chat" in rules

    def test_existing_8000_routes_still_work(self, monkeypatch) -> None:
        from scripts import quant_api_server

        client = quant_api_server.app.test_client()
        resp = client.get("/api/launcher/build-id")
        assert resp.status_code == 200
        home = client.get("/")
        assert home.status_code == 200

    def test_new_namespace_confined_to_ai_trader_prefix(self) -> None:
        from scripts import quant_api_server

        ai_rules = [
            str(r)
            for r in quant_api_server.app.url_map.iter_rules()
            if "ai-trader" in str(r)
        ]
        assert ai_rules, "ai-trader routes missing"
        assert all(rule.startswith("/api/ai-trader/") for rule in ai_rules)
