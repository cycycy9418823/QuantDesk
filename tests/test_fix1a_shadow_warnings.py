"""Batch 1A-H: shadow-risk failures are no longer swallowed by ``except Exception: pass``.

Before the fix a failure to append the shadow record or to write the shadow
error log vanished (auto_paper_portfolio._record_shadow_failure and the report
section, position_management_engine.record_shadow_failure). Now the record
carries ``shadow_error_log_failed`` and the latest payload shows
``shadow_warnings``; the ledger state is still untouched (shadow stays shadow).
"""

from __future__ import annotations

import json
from pathlib import Path

import quantdesk.paper.auto_paper_portfolio as auto
import quantdesk.paper.position_management_engine as pme


def _boom(**_kwargs):
    raise OSError("error-log-disk-full")


def test_strategy_book_records_the_log_failure_and_publishes_it(tmp_path: Path, monkeypatch) -> None:
    auto._SHADOW_UNRECORDED.clear()
    monkeypatch.setattr(auto, "record_shadow_error_best_effort", _boom)
    monkeypatch.setattr(auto, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", tmp_path / "latest.json")
    records: list[dict] = []
    auto._record_shadow_failure(records, surface="auto_paper_daily_buy", error=RuntimeError("x"),
                                as_of="2026-10-09", write_error=True)
    assert records[0]["status"] == "shadow_error"
    assert records[0]["shadow_error_log_failed"].startswith("OSError")
    written = auto._write_auto_paper_latest({"ok": True, "cash": 1.0, "positions": []})
    assert any("shadow_error_log_write_failed" in item for item in written["shadow_warnings"])
    on_disk = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert on_disk["shadow_warnings"] == written["shadow_warnings"]
    auto._SHADOW_UNRECORDED.clear()


def test_report_section_failure_is_written_into_the_report(tmp_path: Path, monkeypatch) -> None:
    auto._SHADOW_UNRECORDED.clear()
    monkeypatch.setattr(auto, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(auto, "render_shadow_report_section", lambda summary: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(auto, "record_shadow_error_best_effort", _boom)
    path = auto._write_report({"summary": {}}, risk_gate_shadow_summary={"as_of": "2026-10-09"})
    text = path.read_text(encoding="utf-8")
    assert "## 影子风险门（未能生成）" in text and "影子风险段生成失败" in text
    assert "## 影子风险门（Shadow Risk Gate）" not in text
    assert "错误日志写入也失败" in text
    auto._SHADOW_UNRECORDED.clear()


def test_conditional_engine_marks_the_record_instead_of_passing(monkeypatch) -> None:
    pme.SHADOW_UNRECORDED.clear()
    monkeypatch.setattr(pme, "verify_candidate_envelope", lambda envelope: (_ for _ in ()).throw(RuntimeError("env")))
    monkeypatch.setattr(pme, "record_shadow_error_best_effort", _boom)
    records: list[dict] = []
    state = {"cash": 100000.0, "initial_cash": 100000.0, "positions": [], "closed_positions": []}
    payload = {"plan_id": "p1", "trade_date": "2026-10-09", "events": [
        {"symbol": "600000", "action": "buy", "is_reliable": True, "trigger_price": 10.0, "quantity": 100,
         "strategy_type": "long", "trigger_time": "2026-10-09 09:35:00"}]}
    result = pme.apply_conditional_execution_to_state(
        state, payload, risk_gate_shadow_enabled=True, risk_gate_shadow_records=records,
        risk_gate_shadow_write_errors=True,
    )
    assert result["ok"] is True
    failure = [row for row in records if row.get("status") == "shadow_error"][0]
    assert failure["shadow_error_log_failed"].startswith("OSError")
    assert any("conditional_execution_buy" in item for item in pme.SHADOW_UNRECORDED)
    pme.SHADOW_UNRECORDED.clear()
