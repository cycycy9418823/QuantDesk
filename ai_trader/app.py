"""Port 8001 classroom for the independent 100k paper trader."""

from __future__ import annotations

import json
from copy import deepcopy
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request

from ai_trader.ledger import LedgerMissingError, assert_finite_json_numbers, load_ledger
from ai_trader.mandate import BOUNDARY, DEFAULT_MANDATE
from ai_trader.paths import active_profile, ledger_id_for_path, profile_paths

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _loopback_host(host: str | None) -> bool:
    value = str(host or "").strip().lower()
    if value == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _local_request_allowed(allow_remote: bool = False) -> bool:
    """Reject untrusted browser origins before reading state or invoking chat."""
    try:
        target = urlsplit(request.host_url)
        if not allow_remote and not _loopback_host(target.hostname):
            return False
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            for header in ("Origin", "Referer"):
                value = request.headers.get(header)
                if value is None:
                    continue
                origin = urlsplit(value)
                if origin.scheme not in {"http", "https"} or origin.username or origin.password:
                    return False
                if allow_remote:
                    if (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
                        return False
                elif not _loopback_host(origin.hostname):
                    return False
    except ValueError:
        return False
    return True


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


NO_LEDGER_REASON = "尚无账本：本页只读，不会新建账本；账本由晚间 20:20 计划任务或 `python -m ai_trader init` 建立。"


def _read_ledger_readonly() -> tuple[dict | None, str, str]:
    """(ledger, status, detail) without ever creating or repairing the book.

    status is "ok", "missing" or "unreadable". A viewer must not call
    init_ledger(): that writes a fresh 100k book and would mask a lost ledger.
    """

    target = profile_paths().ledger
    try:
        return load_ledger(target, missing_ok=False), "ok", ""
    except LedgerMissingError:
        return None, "missing", NO_LEDGER_REASON
    except (OSError, ValueError, RuntimeError) as exc:
        return None, "unreadable", f"账本读取失败（未改动文件）：{type(exc).__name__}: {exc}"


def _empty_snapshot() -> dict:
    book, ledger_status, ledger_detail = _read_ledger_readonly()
    ledger = book or {}
    positions = deepcopy(ledger.get("positions") or [])
    equity, cash = ledger.get("equity"), ledger.get("cash")
    saved_market_value = ledger.get("market_value")
    if saved_market_value is None and isinstance(equity, (int, float)) and isinstance(cash, (int, float)):
        saved_market_value = equity - cash
    initial_cash = ledger.get("initial_cash")
    pnl = equity - initial_cash if isinstance(equity, (int, float)) and isinstance(initial_cash, (int, float)) else None
    if ledger_status == "ok":
        reason = "缺少复盘快照；下方仅显示已保存账本，估值时间见账本，不代表当前行情。本页只读。"
    else:
        reason = ledger_detail
    return {
        "ok": True,
        "as_of": ledger.get("as_of"),
        "degraded": True,
        "snapshot_status": "missing",
        "valuation_status": "saved_book_only" if book is not None else "unavailable",
        "ledger_status": ledger_status,
        "reason": reason,
        "journal": {
            "headline": "复盘快照不可用，显示已保存账本。" if book is not None else "账本不可用。",
            "thought": "没有可用的复盘内容；持仓与估值按已保存账本展示。",
            "methods": [],
            "actions": [],
            "lessons": [],
            "account": {
                "equity": ledger.get("equity"),
                "cash": ledger.get("cash"),
                "market_value": saved_market_value,
                "pnl": pnl,
                "holdings": len(positions) if book is not None else None,
            },
        },
        "ledger": {
            "ledger_id": ledger.get("ledger_id"),
            "cash": ledger.get("cash"),
            "equity": ledger.get("equity"),
            "as_of": ledger.get("as_of"),
            "positions": positions,
        },
        "hypotheses": [],
        "mandate": DEFAULT_MANDATE.to_dict(),
        "boundary": dict(BOUNDARY),
        "disclaimer": "纸面模拟，不是投资建议，不接券商。",
    }


def _snapshot_identity_problem(payload: dict, expected: str) -> str | None:
    ledger = payload.get("ledger")
    journal = payload.get("journal")
    account = journal.get("account") if isinstance(journal, dict) else None
    money_keys = {"cash", "equity", "market_value", "pnl", "positions", "holdings"}
    financial = ((isinstance(ledger, dict) and bool(money_keys & ledger.keys()))
                 or (isinstance(account, dict) and bool(money_keys & account.keys())))
    declared = str(ledger.get("ledger_id") or "") if isinstance(ledger, dict) else ""
    if financial and not declared:
        return "ledger_identity_unverified"
    if declared and declared != expected:
        return "ledger_identity_mismatch"
    for section in (payload, payload.get("plan")):
        if isinstance(section, dict) and section.get("ledger_id") and str(section["ledger_id"]) != expected:
            return "ledger_identity_mismatch"
    return None


def load_snapshot() -> dict:
    snapshot_path = profile_paths().snapshot
    if not snapshot_path.is_file():
        return _empty_snapshot()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert_finite_json_numbers(payload)
    except (OSError, UnicodeError, ValueError):
        snap = _empty_snapshot()
        snap["degraded"] = True
        snap["snapshot_status"] = "unreadable"
        snap["reason"] = "快照读取失败（未改动文件）；" + snap["reason"]
        return snap
    if not isinstance(payload, dict):
        snap = _empty_snapshot()
        snap["snapshot_status"] = "invalid_shape"
        snap["reason"] = "快照结构无效（未改动文件）；" + snap["reason"]
        return snap
    problem = _snapshot_identity_problem(payload, ledger_id_for_path(profile_paths().ledger))
    if problem:
        snap = _empty_snapshot()
        snap["snapshot_status"] = problem
        snap["reason"] = "快照账户身份缺失或不匹配（未改动文件）；" + snap["reason"]
        return snap
    return payload


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

    @app.before_request
    def require_local_request():
        if not _local_request_allowed(bool(app.config.get("ALLOW_REMOTE_ACCESS", False))):
            return jsonify({"ok": False, "reason": "non_local_request"}), 403

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/api/snapshot")
    def snapshot():
        return jsonify(load_snapshot())

    @app.get("/api/minute-focus")
    def minute_focus():
        from ai_trader.minute_focus import read_minute_focus

        payload = read_minute_focus(profile_paths())
        return jsonify(payload), 200 if payload.get("ok") else 503

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
        book, status, detail = _read_ledger_readonly()
        if book is not None:
            return jsonify(book)
        code = 404 if status == "missing" else 500
        body = {
            "ok": False,
            "status": "no_ledger" if status == "missing" else "ledger_unreadable",
            "reason": detail,
            "ledger_path": str(profile_paths().ledger),
        }
        return jsonify(body), code

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


def main(host: str = "127.0.0.1", port: int = 8001, *, allow_remote: bool = False) -> None:
    # Read-only viewer: starting it must not create directories or a ledger.
    if not _loopback_host(host) and not allow_remote:
        raise SystemExit("8001 defaults to loopback only; remote binding requires --allow-remote")
    app = create_app()
    app.config["ALLOW_REMOTE_ACCESS"] = bool(allow_remote)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
