"""Safe transport diagnostics for the AI trader LLM call. No real network."""

from __future__ import annotations

import json
import socket
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

import ai_trader.ledger as ledger_module
import ai_trader.llm as llm
import ai_trader.overnight as overnight
import ai_trader.paths as paths_module
from ai_trader.paths import CyclePaths

SECRET = "sk-TRANSPORT-SECRET"
LEAK_MARKERS = (SECRET, "token=", "Authorization", "sk-")


def _assert_secret_free(blob: str) -> None:
    if any(marker in blob for marker in LEAK_MARKERS):
        raise AssertionError("secret or query url leaked")


def _cfg() -> dict[str, str]:
    return {
        "base_url": "http://127.0.0.1:9",
        "token": SECRET,
        "model": "glm-5.2",
        "provider": "openai",
    }


def _enable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.delenv("AI_TRADER_PROFILE", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", SECRET)
    monkeypatch.setenv("AI_TRADER_LLM_MODEL", "glm-5.2")
    monkeypatch.setenv("AI_TRADER_LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm, "ENV_FILE", tmp_path / "missing.env")


def _install(
    monkeypatch, error: BaseException | None = None, payload: dict | None = None,
    read_error: BaseException | None = None,
) -> list[float | None]:
    calls: list[float | None] = []

    def fake_urlopen(request, timeout=None):
        calls.append(timeout)
        if error is not None:
            raise error
        class Response:
            status = 200

            def read(self) -> bytes:
                if read_error is not None:
                    raise read_error
                return json.dumps(payload or {}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return calls


def _saved(diagnostics: dict) -> str:
    plan = {
        "llm": {
            "used": False,
            "unavailable_reason": diagnostics.get("reason") or "",
            "diagnostics": llm.persistable_transport_diagnostics(diagnostics),
        }
    }
    return json.dumps(plan, ensure_ascii=False)


def test_normal_response_keeps_parse_and_records_elapsed(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    calls = _install(
        monkeypatch,
        payload={
            "choices": [
                {"message": {"content": '{"buys":[{"symbol":"600000","reason":"ok","hold_days":5}]}'}, "finish_reason": "stop"}
            ],
            "usage": {"completion_tokens": 8},
        },
    )
    diagnostics: dict = {}
    out = llm.propose_picks({"as_of": "2026-09-21", "candidates": ["600000"]}, diagnostics)
    saved = llm.persistable_transport_diagnostics(diagnostics)
    assert out is not None
    assert out["buys"][0]["symbol"] == "600000"
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    assert saved["stage"] == "response"
    assert saved["http_status"] == 200
    assert isinstance(saved["elapsed_ms"], int) and saved["elapsed_ms"] >= 0
    assert "error_category" not in saved
    assert "reason" not in diagnostics
    _assert_secret_free(json.dumps(saved))


def test_http_error_keeps_status_and_class_name(monkeypatch) -> None:
    error = urllib.error.HTTPError(
        "https://relay.example/v1/chat/completions?token=" + SECRET,
        429,
        SECRET,
        hdrs=None,
        fp=None,
    )
    calls = _install(monkeypatch, error=error)
    diagnostics: dict = {}
    assert llm._post_json("http://127.0.0.1:9/v1/chat/completions", {"model": "glm-5.2"}, {}, diagnostics) is None
    saved = _saved(diagnostics)
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    assert diagnostics["reason"] == "transport:HTTPError"
    body = json.loads(saved)["llm"]
    assert body["unavailable_reason"] == "transport:HTTPError"
    assert body["diagnostics"] == {
        "stage": "response",
        "elapsed_ms": body["diagnostics"]["elapsed_ms"],
        "error_category": "http_error",
        "http_status": 429,
    }
    _assert_secret_free(saved)


def test_direct_timeout_is_timeout_without_exception_text(monkeypatch) -> None:
    calls = _install(monkeypatch, error=TimeoutError("timed out " + SECRET))
    diagnostics: dict = {}
    assert llm._call("s", "u", _cfg(), diagnostics) is None
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    assert diagnostics["reason"] == "transport:TimeoutError"
    saved = _saved(diagnostics)
    body = json.loads(saved)["llm"]["diagnostics"]
    assert body["stage"] == "request"
    assert body["error_category"] == "timeout"
    assert body["urlerror_cause"] == "timeout"
    assert "http_status" not in body
    _assert_secret_free(saved)


@pytest.mark.parametrize(
    ("failure", "reason", "cause"),
    [
        (TimeoutError(SECRET), "transport:TimeoutError", "timeout"),
        (ConnectionResetError(SECRET), "transport:ConnectionResetError", "connection_reset"),
        (urllib.error.URLError(socket.timeout(SECRET)), "transport:URLError", "timeout"),
    ],
    ids=["body_timeout", "body_reset", "body_wrapped_timeout"],
)
def test_body_failure_keeps_received_status_and_phase(monkeypatch, failure, reason, cause) -> None:
    calls = _install(monkeypatch, read_error=failure)
    diagnostics: dict = {}
    assert llm._post_json("http://127.0.0.1:9/v1/chat/completions", {}, {}, diagnostics) is None
    body = llm.persistable_transport_diagnostics(diagnostics)
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    assert diagnostics["reason"] == reason
    assert body["stage"] == "response"
    assert body["http_status"] == 200
    assert body["urlerror_cause"] == cause
    assert body["error_category"] == ("timeout" if cause == "timeout" else "connection_error")
    _assert_secret_free(_saved(diagnostics))


def test_nested_urlerror_timeout_classifies_inner_cause(monkeypatch) -> None:
    inner = socket.timeout("timed out " + SECRET + " https://relay.example/?token=" + SECRET)
    calls = _install(monkeypatch, error=urllib.error.URLError(urllib.error.URLError(inner)))
    diagnostics: dict = {}
    assert llm._call("s", "u", _cfg(), diagnostics) is None
    assert len(calls) == 1
    assert diagnostics["reason"] == "transport:URLError"
    body = json.loads(_saved(diagnostics))["llm"]["diagnostics"]
    assert body["error_category"] == "timeout"
    assert body["urlerror_cause"] == "timeout"
    assert body["stage"] == "request"
    _assert_secret_free(json.dumps(body))


def test_connection_refused_is_connection_error(monkeypatch) -> None:
    _install(monkeypatch, error=urllib.error.URLError(ConnectionRefusedError(111, SECRET)))
    diagnostics: dict = {}
    assert llm._post_json("http://127.0.0.1:9/v1/chat/completions", {}, {}, diagnostics) is None
    body = json.loads(_saved(diagnostics))["llm"]["diagnostics"]
    assert diagnostics["reason"] == "transport:URLError"
    assert body["error_category"] == "connection_error"
    assert body["urlerror_cause"] == "connection_refused"
    _assert_secret_free(json.dumps(body))


def test_unknown_reason_stays_unknown_and_drops_secret_text(monkeypatch) -> None:
    _install(
        monkeypatch,
        error=urllib.error.URLError(RuntimeError("https://relay.example/v1?token=" + SECRET)),
    )
    diagnostics: dict = {}
    diagnostics["transport"] = {"detail": SECRET, "url": "https://x?token=" + SECRET}
    assert llm._post_json("http://127.0.0.1:9/v1/chat/completions", {}, {}, diagnostics) is None
    body = llm.persistable_transport_diagnostics(diagnostics)
    assert body["error_category"] == "connection_error"
    assert body["urlerror_cause"] == "unknown"
    assert set(body) <= {"stage", "elapsed_ms", "error_category", "http_status", "urlerror_cause"}
    _assert_secret_free(json.dumps(body))


def test_rule_fallback_reason_string_is_unchanged(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    _install(monkeypatch, error=urllib.error.URLError(OSError(SECRET)))
    diagnostics: dict = {}
    assert llm.propose_picks({"as_of": "2026-09-21"}, diagnostics) is None
    assert diagnostics["reason"] == "transport:URLError"
    assert llm.persistable_transport_diagnostics(diagnostics)["urlerror_cause"] == "unknown"


def _seed(tmp_path: Path) -> CyclePaths:
    root = tmp_path / "evening"
    state = root / "state" / "ai_trader"
    results = root / "results" / "ai_trader"
    daily = root / "data" / "daily"
    minute = root / "data" / "intraday"
    for directory in (state, results, daily, minute):
        directory.mkdir(parents=True)
    (daily / "600500.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,fetched_at\n"
        "2026-09-18,600500,5.00,5.10,4.90,5.00,1000,5000,0.0,raw,synthetic,2026-09-18T16:00:00+08:00\n"
        "2026-09-21,600500,5.02,5.20,4.99,5.08,1000,5080,1.6,raw,synthetic,2026-09-21T16:00:00+08:00\n",
        encoding="utf-8",
    )
    ledger = ledger_module.empty_ledger(created_at="2026-09-18T20:00:00+08:00")
    (state / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (state / "plan_latest.json").write_text(
        json.dumps({
            "plan_id": "plan_settled",
            "decision_date": "2026-09-18",
            "trade_date": "2026-09-21",
            "status": "settled",
            "settled_on": "2026-09-21",
            "intents": [],
            "committed": True,
        }),
        encoding="utf-8",
    )
    return CyclePaths(
        daily_dir=daily,
        minute_dir=minute,
        ledger=state / "ledger.json",
        hypotheses=state / "hypotheses.json",
        journal_latest=state / "journal_latest.json",
        journal_log=state / "journal.jsonl",
        daytime_notes=state / "daytime_notes.jsonl",
        daytime_session=state / "daytime_session.json",
        daytime_intents=state / "daytime_intents.jsonl",
        plan_latest=state / "plan_latest.json",
        review_latest=state / "review_latest.json",
        watchlist=state / "watchlist.json",
        philosophy=state / "philosophy.json",
        cycle_latest=results / "cycle_latest.json",
        snapshot=results / "snapshot_latest.json",
        decision_log=state / "decision_log.jsonl",
        decision_latest=state / "decision_latest.json",
        decision_page=results / "decision_page.html",
        decision_page_md=results / "decision_page.md",
    )


def test_evening_plan_persists_safe_diagnostics(tmp_path, monkeypatch) -> None:
    _enable(monkeypatch, tmp_path)
    inner = socket.timeout("timed out " + SECRET)
    calls = _install(monkeypatch, error=urllib.error.URLError(inner))
    panel = pd.DataFrame({"600500": [5.0, 5.08]}, index=["2026-09-18", "2026-09-21"])
    monkeypatch.setattr(overnight, "latest_as_of", lambda *_: {"as_of": "2026-09-21"})
    monkeypatch.setattr(overnight, "focus_universe", lambda *a, **k: ["600500"])
    monkeypatch.setattr(overnight, "load_close_panel", lambda *a, **k: (panel.copy(), pd.DataFrame(), []))
    monkeypatch.setattr(overnight, "load_recommendations", lambda **k: {"symbols": [], "rows": [], "warnings": []})
    monkeypatch.setattr(overnight, "research_ideas", lambda p, b, **k: {"book": b, "evaluated": [], "live": None})
    monkeypatch.setattr(overnight, "pick_operating_method", lambda *_: (None, "idle"))
    monkeypatch.setattr(overnight, "next_trade_day", lambda *_: "2026-09-22")
    monkeypatch.setattr(overnight, "_build_llm_context", lambda **k: {"as_of": "2026-09-21", "candidates": []})
    runtime = _seed(tmp_path)
    monkeypatch.setattr(paths_module, "STATE_DIR", runtime.ledger.parent)
    monkeypatch.setattr(paths_module, "RESULTS_DIR", runtime.cycle_latest.parent)
    snapshot = overnight.run_evening(paths=runtime, network_news=False)
    plan = json.loads(runtime.plan_latest.read_text(encoding="utf-8"))
    cycle = json.loads(runtime.cycle_latest.read_text(encoding="utf-8"))
    assert calls == [llm.DEFAULT_TIMEOUT_S]
    for row in (plan["llm"], cycle["plan"]["llm"], snapshot["plan"]["llm"]):
        assert row["used"] is False
        assert row["unavailable_reason"] == "transport:URLError"
        assert row["diagnostics"]["stage"] == "request"
        assert row["diagnostics"]["error_category"] == "timeout"
        assert row["diagnostics"]["urlerror_cause"] == "timeout"
        assert "http_status" not in row["diagnostics"]
    _assert_secret_free(runtime.plan_latest.read_text(encoding="utf-8"))
    _assert_secret_free(runtime.cycle_latest.read_text(encoding="utf-8"))
    _assert_secret_free(runtime.journal_latest.read_text(encoding="utf-8"))
