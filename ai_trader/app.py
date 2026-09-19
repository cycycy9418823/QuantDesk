"""Port 8001 classroom for the independent 100k paper trader."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from ai_trader.ledger import init_ledger, load_ledger
from ai_trader.mandate import BOUNDARY, DEFAULT_MANDATE
from ai_trader.paths import active_profile, ensure_profile_dirs, profile_paths

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _chat_scope_error() -> tuple[dict, int] | None:
    """The chat panel reads the default book only; refuse rather than mislead."""

    profile = active_profile()
    if profile and profile != "default":
        return (
            {
                "ok": False,
                "reason": "chat_not_available_for_profile",
                "detail": f"chat 面板目前只读默认账；本页是 {profile} 档，请用对比报告看它。",
                "profile": profile,
            },
            501,
        )
    return None


def _empty_snapshot() -> dict:
    ledger = init_ledger(path=profile_paths().ledger)
    return {
        "ok": True,
        "as_of": None,
        "degraded": True,
        "reason": "还没有跑过每日复盘。点「今天复盘一次」开始。",
        "journal": {
            "headline": "新开的10万纸面账户，还没开始想办法。",
            "thought": "没有策略池。我会自己试想法、自己证伪、自己决定能不能下单。",
            "methods": [],
            "actions": [],
            "lessons": ["先跑一次复盘，才有可学习的记录。"],
            "account": {
                "equity": ledger.get("equity"),
                "cash": ledger.get("cash"),
                "market_value": 0,
                "pnl": 0,
                "holdings": 0,
            },
        },
        "ledger": {
            "ledger_id": ledger.get("ledger_id"),
            "cash": ledger.get("cash"),
            "equity": ledger.get("equity"),
            "positions": [],
        },
        "hypotheses": [],
        "mandate": DEFAULT_MANDATE.to_dict(),
        "boundary": dict(BOUNDARY),
        "disclaimer": "纸面模拟，不是投资建议，不接券商。",
    }


def load_snapshot() -> dict:
    snapshot_path = profile_paths().snapshot
    if not snapshot_path.is_file():
        return _empty_snapshot()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snap = _empty_snapshot()
        snap["degraded"] = True
        snap["reason"] = "快照读失败，显示空账。"
        return snap
    if not isinstance(payload, dict):
        return _empty_snapshot()
    return payload


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/api/snapshot")
    def snapshot():
        return jsonify(load_snapshot())

    @app.post("/api/cycle")
    def cycle():
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "8001 兼容教室不再运行 cycle；晚间 20:20 计划任务是唯一自动写入入口。",
                    "boundary": "8001 compatibility classroom is read-only; the 20:20 evening scheduled task is the only automatic writer.",
                }
            ),
            410,
        )

    @app.get("/api/ledger")
    def ledger():
        return jsonify(load_ledger(profile_paths().ledger))

    @app.get("/api/chat")
    def chat_history():
        blocked = _chat_scope_error()
        if blocked is not None:
            return jsonify(blocked[0]), blocked[1]
        from ai_trader.chat import load_history

        return jsonify(
            {
                "ok": True,
                "messages": load_history(),
                "disclaimer": "纸面模拟，不接券商、不下单、不构成投资建议。",
            }
        )

    @app.post("/api/chat")
    def chat_send():
        blocked = _chat_scope_error()
        if blocked is not None:
            return jsonify(blocked[0]), blocked[1]
        from ai_trader.chat import answer_question

        payload = request.get_json(silent=True) or {}
        question = str(payload.get("question") or "").strip()
        if not question:
            return jsonify({"ok": False, "reason": "empty_question"}), 400

        # Carry back WHY it failed: "endpoint unreachable" must never be
        # indistinguishable from "the trader had nothing to say".
        diagnostics: dict = {}
        result = answer_question(question, diagnostics)
        if result is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "reason": diagnostics.get("reason") or "no_answer",
                        "detail": diagnostics,
                    }
                ),
                503,
            )
        return jsonify({"ok": True, **result})

    return app


def main(host: str = "127.0.0.1", port: int = 8001) -> None:
    ensure_profile_dirs()
    init_ledger()
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
