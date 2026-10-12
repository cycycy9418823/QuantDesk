from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

import quantdesk.paper.auto_paper_portfolio as auto
import quantdesk.paper.position_management_engine as position
from quantdesk.paper.adjustment_policy import AdjustmentFactorTable
from quantdesk.risk.risk_gate_shadow import _ledger_core


FIXED_NOW = "2026-07-17T15:30:00+08:00"


def _shadow_record(**overrides: Any) -> dict[str, Any]:
    row = {
        "evaluated": True,
        "status": "insufficient_evidence",
        "would_pass": False,
        "would_block": False,
        "insufficient_evidence": True,
        "reason_codes": ["returns_evidence_missing"],
        "missing_components": ["returns"],
        "missing_component_count": 1,
        "symbol": "600000",
        "side": "buy",
    }
    row.update(overrides)
    return row


def _patch_auto_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    from tests.test_b5_formal_candidate_intake import dump, make_formal_batch
    from tests.test_daily_candidate_refresh import _strategy_payload

    results = root / "results"
    reports = root / "reports"
    state = root / "state"
    daily = root / "data" / "daily"
    intraday = root / "data" / "intraday"
    for path in (results, reports, state, daily, intraday / "1m"):
        path.mkdir(parents=True, exist_ok=True)
    # 1A: a strategy-book fill needs the session's limit-price evidence (previous
    # close / change), so the candidate's raw daily bar must exist.
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at\n"
        "2026-07-16,600000,9.8,9.9,9.7,9.8,1000000,9800000,0.0,0.0,raw,fixture,2026-07-16T15:30:00+08:00\n"
        "2026-07-17,600000,9.9,10.1,9.8,10.0,1000000,10000000,0.2,2.041,raw,fixture,2026-07-17T15:30:00+08:00\n",
        encoding="utf-8",
    )
    values = {
        "PROJECT_ROOT": root,
        "RESULTS_DIR": results,
        "REPORTS_DIR": reports,
        "STATE_DIR": state,
        "DAILY_DIR": daily,
        "INTRADAY_DIR": intraday,
        "DAILY_FEATURE_LATEST_CSV": root / "data" / "features" / "daily_features_latest.csv",
        "LOW_PRICE_UNIVERSE_CSV": root / "data" / "universe" / "low_price_learning_symbols.csv",
        "AUTO_STRATEGY_LATEST_JSON": results / "auto_strategy_latest.json",
        "AUTO_PAPER_PORTFOLIO_STATE_JSON": state / "auto_paper_portfolio.json",
        "AUTO_PAPER_PORTFOLIO_LATEST_JSON": results / "auto_paper_portfolio_latest.json",
        "AUTO_PAPER_PORTFOLIO_HISTORY_JSON": results / "auto_paper_portfolio_history.json",
        "SWING_TRADE_PLANS_LATEST_JSON": results / "swing_trade_plans_latest.json",
        "INTRADAY_EXECUTION_LATEST_JSON": results / "intraday_execution_latest.json",
        "CONDITIONAL_TRADE_PLANS_LATEST_JSON": results / "conditional_trade_plans_latest.json",
        "CONDITIONAL_EXECUTION_LATEST_JSON": results / "conditional_execution_latest.json",
        "RISK_GATE_SHADOW_LATEST_JSON": results / "risk_gate_shadow_latest.json",
        "RISK_GATE_SHADOW_ERRORS_JSONL": results / "risk_gate_shadow_errors.jsonl",
        "SECTOR_ROTATION_LATEST_JSON": results / "sector_rotation_latest.json",
    }
    for name, value in values.items():
        monkeypatch.setattr(auto, name, value, raising=False)
    monkeypatch.setattr(auto, "now_text", lambda: FIXED_NOW)
    monkeypatch.setattr(auto, "infer_target_date", lambda target_date=None: (target_date or "2026-07-17", "fixture"))
    monkeypatch.setattr(auto, "evaluate_paper_review_freshness", lambda _state, **_kwargs: auto.evaluate_data_freshness())
    monkeypatch.setattr(auto, "_load_auto_adjustment_factors", lambda symbols: AdjustmentFactorTable())
    monkeypatch.setattr(
        auto,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-07-17",
            "local_daily_latest_date": "2026-07-17",
            "is_daily_stale": False,
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
            "can_verify_intraday_triggers": False,
            "portfolio_review_valid": True,
            "intraday_review_valid": False,
            "review_mode": "daily_review_only",
            "reason": "",
        },
    )
    candidate = _strategy_payload("2026-07-17")["standard_candidates"][0]
    candidate.update(name="shadow hook sample", close=10.0, sector="bank", amount_ma20=50_000_000.0)
    batch = make_formal_batch("2026-07-17", candidate=candidate)
    dump(root / "results/candidate_refresh/daily/2026-07-17.json", batch)


def _candidate_payload() -> dict[str, Any]:
    from quantdesk.daily_candidate_refresh import formal_candidate_strategy_payload, load_formal_candidate_intake

    intake = load_formal_candidate_intake(auto.PROJECT_ROOT, target_date="2026-07-17")
    assert intake["status"] == "passed", intake
    return formal_candidate_strategy_payload(intake)


def _run_auto_once(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    enabled: bool,
    enforced: bool = False,
) -> tuple[bytes, str]:
    _patch_auto_root(monkeypatch, root)
    payload = auto.run_auto_paper_portfolio(
        mode="init",
        initial_cash=10_000.0,
        force_reset=True,
        enable_intraday_swing=False,
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=enabled,
        risk_gate_enforced=enforced,
    )
    assert payload["ok"] is True
    state_bytes = auto.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    report_path = Path(payload["report_path"])
    if not report_path.is_absolute():
        report_path = auto.PROJECT_ROOT / report_path
    return state_bytes, report_path.read_text(encoding="utf-8")


def _strip_shadow_section(text: str) -> str:
    return re.sub(
        r"\n## 影子风险门（Shadow Risk Gate）.*?(?=\n## 今日动作)",
        "",
        text,
        flags=re.DOTALL,
    )


def _normalize_run_root(value: bytes | str, root: Path) -> bytes | str:
    """Replace a run's own temporary root with a constant token.

    Provenance fields legitimately embed the project root (e.g.
    ``candidate_envelope.batch_path``), so two isolated runs under different
    temp roots differ by that token alone. Normalising it lets the tests assert
    that the shadow flag changes nothing else; it never touches ledger, price or
    shadow assertions.
    """
    tokens = sorted(
        {str(root), str(root).replace("\\", "\\\\"), root.as_posix()},
        key=len,
        reverse=True,
    )
    for token in tokens:
        if isinstance(value, bytes):
            value = value.replace(token.encode("utf-8"), b"<RUN_ROOT>")
        else:
            value = value.replace(token, "<RUN_ROOT>")
    return value


def _run_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_auto_shadow_off_on_and_enforced_flag_keep_state_bytes_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(auto, "evaluate_shadow_risk_order", lambda **kwargs: _shadow_record())
    off_state, off_report = _run_auto_once(monkeypatch, tmp_path / "off", enabled=False)
    on_state, on_report = _run_auto_once(monkeypatch, tmp_path / "on", enabled=True, enforced=False)
    enforced_state, _ = _run_auto_once(monkeypatch, tmp_path / "enforced", enabled=True, enforced=True)

    assert (
        _normalize_run_root(on_state, tmp_path / "on")
        == _normalize_run_root(off_state, tmp_path / "off")
        == _normalize_run_root(enforced_state, tmp_path / "enforced")
    )
    assert "## 影子风险门（Shadow Risk Gate）" not in off_report
    assert "## 影子风险门（Shadow Risk Gate）" in on_report
    assert _strip_shadow_section(
        _normalize_run_root(on_report, tmp_path / "on")
    ) == _normalize_run_root(off_report, tmp_path / "off")
    summary = json.loads((tmp_path / "on" / "results" / "risk_gate_shadow_latest.json").read_text(encoding="utf-8"))
    assert summary["evaluation_count"] == 1
    assert summary["insufficient_evidence_count"] == 1
    assert summary["risk_gate_enforced"] is False
    assert summary["enforcement_applied"] is False


def test_auto_daily_buy_shadow_binds_exact_content_addressed_pretrade_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_auto_root(monkeypatch, tmp_path)
    captured: list[dict[str, Any]] = []

    def capture_shadow_call(**kwargs: Any) -> dict[str, Any]:
        source_path = Path(kwargs["ledger_source_path"])
        assert source_path.is_file()
        snapshot = json.loads(source_path.read_text(encoding="utf-8"))
        assert _ledger_core(snapshot) == _ledger_core(kwargs["state"])
        captured.append({"kwargs": kwargs, "snapshot": snapshot})
        return _shadow_record(symbol=kwargs["candidate"]["symbol"])

    monkeypatch.setattr(auto, "evaluate_shadow_risk_order", capture_shadow_call)

    payload = auto.run_auto_paper_portfolio(
        mode="init",
        initial_cash=10_000.0,
        force_reset=True,
        enable_intraday_swing=False,
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=True,
        risk_gate_enforced=False,
    )

    assert payload["ok"] is True
    assert len(captured) == 1
    call = captured[0]
    source_path = Path(call["kwargs"]["ledger_source_path"])
    assert source_path != auto.AUTO_PAPER_PORTFOLIO_STATE_JSON
    assert source_path.parent == (
        tmp_path / "results" / "risk_gate_shadow_ledger_inputs" / "2026-07-17" / "auto_paper_daily_buy"
    )
    provenance = call["snapshot"]["risk_gate_shadow_ledger_snapshot"]
    assert provenance["event_ordinal"] == 1
    assert provenance["symbol"] == "600000"
    assert provenance["base_ledger_source"]["path"] == str(
        auto.AUTO_PAPER_PORTFOLIO_STATE_JSON
    )


def test_auto_daily_buy_shadow_exception_and_error_log_exception_do_not_change_legacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    off_state, _ = _run_auto_once(monkeypatch, tmp_path / "off", enabled=False)

    def explode(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected-shadow-evaluation-failure")

    monkeypatch.setattr(auto, "evaluate_shadow_risk_order", explode)
    monkeypatch.setattr(
        auto,
        "record_shadow_error_best_effort",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("injected-shadow-error-log-failure")),
    )
    on_state, _ = _run_auto_once(monkeypatch, tmp_path / "on", enabled=True)

    assert _normalize_run_root(on_state, tmp_path / "on") == _normalize_run_root(
        off_state, tmp_path / "off"
    )
    written = json.loads((tmp_path / "on" / "state" / "auto_paper_portfolio.json").read_text(encoding="utf-8"))
    assert any(action.get("action") == "buy" for action in written["actions"])


def _run_intraday_swing_once(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    enabled: bool,
) -> bytes:
    _patch_auto_root(monkeypatch, root)
    initial = auto._empty_state(10_000.0)
    auto.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_text(
        json.dumps(initial, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        auto,
        "_simulate_swing_for_row",
        lambda row, **kwargs: (
            {
                "ok": True,
                "symbol": row["symbol"],
                "trade_date": "2026-07-17",
            },
            {
                "symbol": row["symbol"],
                "trade_date": "2026-07-17",
                "triggered": True,
                "action": "buy",
                "trigger_price": 10.0,
                "trigger_time": "09:35:00",
                "trigger_type": "pullback_buy",
                "quantity": 100,
                "is_reliable": True,
                "reason": "fixture minute trigger",
            },
        ),
    )
    class _LegacyIntradayBranchProbe:
        def __init__(self) -> None:
            self.calls = 0

        def __bool__(self) -> bool:
            # The legacy function currently routes the first truthy update to
            # conditional execution before reaching its older swing branch.
            # Return false once to exercise that still-maintained branch, then
            # true for its actual branch guard and artifact bookkeeping.
            self.calls += 1
            return self.calls > 1

    payload = auto._run_auto_paper_portfolio_unlocked(
        mode="update",
        initial_cash=10_000.0,
        enable_intraday_swing=_LegacyIntradayBranchProbe(),
        intraday_source="1m",
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=enabled,
        risk_gate_enforced=True,
    )
    assert payload["ok"] is True
    assert any(action.get("action") == "buy" for action in payload["actions"])
    return auto.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()


def test_auto_intraday_swing_shadow_exception_isolated_and_state_byte_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    off_state = _run_intraday_swing_once(
        monkeypatch,
        tmp_path / "off",
        enabled=False,
    )

    calls: list[dict[str, Any]] = []

    def explode(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        raise RuntimeError("injected-intraday-shadow-evaluation-failure")

    monkeypatch.setattr(auto, "evaluate_shadow_risk_order", explode)
    monkeypatch.setattr(
        auto,
        "record_shadow_error_best_effort",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected-intraday-shadow-error-log-failure")
        ),
    )
    on_state = _run_intraday_swing_once(
        monkeypatch,
        tmp_path / "on",
        enabled=True,
    )

    assert _normalize_run_root(on_state, tmp_path / "on") == _normalize_run_root(
        off_state, tmp_path / "off"
    )
    assert len(calls) == 1
    assert calls[0]["surface"] == "auto_paper_intraday_swing_buy"
    assert calls[0]["price_data_cutoff"] == "2026-07-17"
    assert str(calls[0]["price_source_path"]).endswith(
        str(Path("data") / "intraday" / "1m" / "600000.csv")
    )


def test_shadow_summary_write_exception_does_not_interrupt_auto_paper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_auto_root(monkeypatch, tmp_path)
    monkeypatch.setattr(auto, "evaluate_shadow_risk_order", lambda **kwargs: _shadow_record())
    monkeypatch.setattr(auto, "write_shadow_summary", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected-write")))
    monkeypatch.setattr(auto, "record_shadow_error_best_effort", lambda **kwargs: False)

    payload = auto.run_auto_paper_portfolio(
        mode="init",
        force_reset=True,
        enable_intraday_swing=False,
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=True,
    )

    assert payload["ok"] is True
    assert auto.AUTO_PAPER_PORTFOLIO_STATE_JSON.exists()
    assert any(action.get("action") == "buy" for action in payload["actions"])


def test_shadow_report_renderer_exception_leaves_legacy_report_writable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_auto_root(monkeypatch, tmp_path)
    monkeypatch.setattr(auto, "render_shadow_report_section", lambda payload: (_ for _ in ()).throw(RuntimeError("render")))
    monkeypatch.setattr(auto, "record_shadow_error_best_effort", lambda **kwargs: False)
    latest = {
        "summary": {},
        "actions": [],
        "decision_audit": {"strategy_learning_notes": []},
    }

    path = auto._write_report(latest, risk_gate_shadow_summary={"evaluation_count": 1})

    assert path.exists()
    assert "## 今日动作" in path.read_text(encoding="utf-8")
    assert "## 影子风险门（Shadow Risk Gate）" not in path.read_text(encoding="utf-8")


def _conditional_state() -> dict[str, Any]:
    return {
        "schema_version": "3.7.0.6",
        "profile_id": "user_10k",
        "scenario_id": "user_10k",
        "initial_cash": 10_000.0,
        "cash": 10_000.0,
        "positions": [],
        "closed_positions": [],
        "applied_execution_ids": [],
    }


def _conditional_execution() -> dict[str, Any]:
    return {
        "plan_id": "shadow-plan",
        "trade_date": "2026-07-17",
        "events": [
            {
                "symbol": "600000",
                "name": "conditional shadow sample",
                "strategy_type": "long",
                "sector": "bank",
                "trade_date": "2026-07-17",
                "action": "buy",
                "action_label": "模拟买入",
                "is_reliable": True,
                "trigger_price": 10.0,
                "quantity": 100,
                "reason": "fixture conditional trigger",
            }
        ],
        "no_fill": [],
        "blocked": [],
    }


def test_conditional_buy_shadow_off_on_return_bytes_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(position, "evaluate_shadow_risk_order", lambda **kwargs: _shadow_record())
    off = position.apply_conditional_execution_to_state(
        copy.deepcopy(_conditional_state()),
        _conditional_execution(),
        daily_dir=tmp_path / "daily",
        risk_gate_shadow_enabled=False,
    )
    on = position.apply_conditional_execution_to_state(
        copy.deepcopy(_conditional_state()),
        _conditional_execution(),
        daily_dir=tmp_path / "daily",
        risk_gate_shadow_enabled=True,
        risk_gate_enforced=True,
        risk_gate_shadow_output_path=tmp_path / "results" / "risk_gate_shadow_latest.json",
        risk_gate_shadow_error_path=tmp_path / "results" / "risk_gate_shadow_errors.jsonl",
    )

    assert json.dumps(on, ensure_ascii=False, sort_keys=True) == json.dumps(off, ensure_ascii=False, sort_keys=True)
    assert on["actions"][0]["action"] == "buy"


def test_conditional_shadow_and_error_journal_exceptions_never_interrupt_buy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(position, "evaluate_shadow_risk_order", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("evaluate")))
    monkeypatch.setattr(position, "write_shadow_summary", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write")))
    monkeypatch.setattr(position, "record_shadow_error_best_effort", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("journal")))

    result = position.apply_conditional_execution_to_state(
        _conditional_state(),
        _conditional_execution(),
        daily_dir=tmp_path / "daily",
        risk_gate_shadow_enabled=True,
        risk_gate_shadow_output_path=tmp_path / "results" / "risk_gate_shadow_latest.json",
        risk_gate_shadow_error_path=tmp_path / "results" / "risk_gate_shadow_errors.jsonl",
    )

    assert result["ok"] is True
    assert result["actions"][0]["action"] == "buy"
    assert result["state"]["cash"] < 10_000.0


def test_low_level_conditional_default_does_not_write_shadow_to_real_or_temp_sink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The production auto-paper caller opts in explicitly.  Existing direct
    # low-level consumers must stay side-effect free unless they provide that
    # opt-in; otherwise unit tests and alternate project roots leak into the
    # formal results directory.
    monkeypatch.setattr(position, "RESULTS_DIR", tmp_path / "results")

    result = position.apply_conditional_execution_to_state(
        _conditional_state(),
        _conditional_execution(),
        daily_dir=tmp_path / "daily",
    )

    assert result["ok"] is True
    assert not (tmp_path / "results" / "risk_gate_shadow_latest.json").exists()
    assert not (tmp_path / "results" / "risk_gate_shadow_errors.jsonl").exists()


def test_auto_dry_run_shadow_failure_writes_no_runtime_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_auto_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        auto,
        "evaluate_shadow_risk_order",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("dry-run-shadow-failure")),
    )
    # The fixture inputs written by _patch_auto_root are not runtime output; a
    # failing shadow evaluation during dry-run must add no new runtime file.
    before = _run_files(tmp_path)

    payload = auto.run_auto_paper_portfolio(
        mode="init",
        initial_cash=10_000.0,
        enable_intraday_swing=False,
        dry_run=True,
        force_reset=True,
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=True,
    )

    assert payload["ok"] is True
    assert _run_files(tmp_path) == before


def test_conditional_update_dry_run_shadow_failure_preserves_runtime_manifests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_auto_root(monkeypatch, tmp_path)
    initial = auto._empty_state(10_000.0)
    auto.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_text(
        json.dumps(initial, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        auto,
        "build_conditional_trade_plan",
        lambda **kwargs: {
            "plan_id": "shadow-plan",
            "plan_date": "2026-07-16",
            "trade_date": "2026-07-17",
            "summary": {"symbol_count": 1, "new_candidate_plan_count": 1},
        },
    )
    monkeypatch.setattr(
        auto,
        "simulate_conditional_plan_file",
        lambda *args, **kwargs: _conditional_execution(),
    )
    monkeypatch.setattr(
        position,
        "evaluate_shadow_risk_order",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("conditional-dry-run-shadow-failure")
        ),
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for root in (tmp_path / "state", tmp_path / "results", tmp_path / "reports")
        for path in root.rglob("*")
        if path.is_file()
    }

    payload = auto.run_auto_paper_portfolio(
        mode="update",
        initial_cash=10_000.0,
        enable_intraday_swing=True,
        dry_run=True,
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=True,
    )

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for root in (tmp_path / "state", tmp_path / "results", tmp_path / "reports")
        for path in root.rglob("*")
        if path.is_file()
    }
    assert payload["ok"] is True
    assert after == before


def test_stale_conditional_branch_refreshes_zero_traffic_shadow_and_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_auto_root(monkeypatch, tmp_path)
    initial = auto._empty_state(10_000.0)
    auto.AUTO_PAPER_PORTFOLIO_STATE_JSON.write_text(
        json.dumps(initial, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        auto,
        "evaluate_data_freshness",
        lambda: {
            "ok": False,
            "target_complete_trade_date": "2026-07-17",
            "can_review_yesterday_positions": False,
            "reason": "fixture_stale_daily",
        },
    )

    payload = auto.run_auto_paper_portfolio(
        mode="update",
        initial_cash=10_000.0,
        enable_intraday_swing=True,
        dry_run=False,
        auto_strategy_payload=_candidate_payload(),
        risk_gate_shadow_enabled=True,
    )

    summary = json.loads(
        (tmp_path / "results" / "risk_gate_shadow_latest.json").read_text(
            encoding="utf-8"
        )
    )
    report_path = Path(payload["report_path"])
    if not report_path.is_absolute():
        report_path = tmp_path / report_path
    report = report_path.read_text(encoding="utf-8")
    assert summary["as_of"] == "2026-07-17"
    assert summary["evaluation_count"] == 0
    assert summary["observation_reason"] == "stale_data_no_traffic"
    assert "## 影子风险门（Shadow Risk Gate）" in report
