"""Research displays must keep sample limits visible without changing results."""
from __future__ import annotations

import json

import pytest

from tests.test_project_status_ui import _run_status_ui


LIMITATION = "尚未建立样本外验证；历史筛选或统计通过不代表策略已验证。"


def _render(renderer: str, panel: str, payload: dict) -> str:
    script = (
        "const sampleTestPayload = " + json.dumps(payload, ensure_ascii=False) + ";"
        "const sampleTestBefore = JSON.stringify(sampleTestPayload);"
        + renderer + "(sampleTestPayload);"
        "console.log(JSON.stringify({html:document.getElementById("
        + json.dumps(panel) + ").innerHTML,requests,"
        "unchanged:sampleTestBefore === JSON.stringify(sampleTestPayload)}));"
    )
    result = _run_status_ui("vm.runInContext(" + json.dumps(script, ensure_ascii=False) + ", context);")
    assert result["requests"] == []
    assert result["unchanged"] is True
    return result["html"]


def _backtest_payload() -> dict:
    return {
        "ok": True,
        "exists": True,
        "strategy": "fixture_strategy",
        "symbol_count": 7,
        "metrics": {
            "annual_return": 0.12, "total_return": 0.25, "max_drawdown": -0.08,
            "sharpe": 1.25, "number_of_trades": 23,
        },
        "equity_curve": [],
    }


@pytest.mark.parametrize("placement", ["top_level", "research_evidence"])
@pytest.mark.parametrize("role,label", [("in_sample", "样本内研究"), ("unspecified", "样本范围未声明")])
def test_backtest_reads_explicit_scope_and_preserves_metrics(placement: str, role: str, label: str) -> None:
    payload = _backtest_payload()
    scope = {"sample_role": role, "out_of_sample_validation": "not_established", "sample_limitation": LIMITATION}
    if placement == "top_level":
        payload.update(scope)
    else:
        payload["research_evidence"] = scope
    html = _render("renderBacktest", "backtestPanel", payload)
    assert label in html
    assert "样本外验证未建立" in html
    assert LIMITATION in html
    assert "历史研究回测" in html
    for metric in ("+12.0%", "+25.0%", "-8.0%", "1.25", "23"):
        assert f"<strong>{metric}</strong>" in html
    assert "默认演示配置" not in html
    assert "三账户回放" not in html


@pytest.mark.parametrize("params", [None, {}, {"start_date": "2020-01-01", "end_date": "2025-01-01"}])
def test_legacy_backtest_never_infers_demo_or_sample_role_from_params(params: dict | None) -> None:
    payload = _backtest_payload()
    if params is not None:
        payload["params"] = params
    html = _render("renderBacktest", "backtestPanel", payload)
    assert "样本范围未声明" in html
    assert "样本外验证状态未知，尚未建立" in html
    assert LIMITATION in html
    assert "样本内研究" not in html
    assert "默认演示配置" not in html
    assert "三账户回放" not in html


@pytest.mark.parametrize("wrapped", [False, True])
def test_funnel_scope_remains_next_to_ranking_with_candidate_status_and_returns(wrapped: bool) -> None:
    summary = {
        "exists": True, "ok": True, "sample_role": "in_sample",
        "out_of_sample_validation": "not_established", "sample_limitation": LIMITATION,
        "candidate_count": 1, "top_strategies": [{
            "strategy": "fixture_strategy", "status": "candidate",
            "annualized_return_net": 0.12, "max_drawdown": -0.08, "sharpe": 1.25,
        }],
    }
    payload = {"status": "passed", "running": False, "latest_result": summary} if wrapped else summary
    html = _render("renderStrategyScreeningStatus", "strategyScreeningPanel", payload)
    ranking = html.split("策略排行预览", 1)[1]
    assert ranking.index("样本内研究") < ranking.index("<table")
    assert "样本外验证未建立" in ranking
    assert LIMITATION in ranking
    for value in ("fixture_strategy", "+12.0%", "-8.0%", "1.25"):
        assert f"<td>{value}</td>" in ranking
    assert "候选" in ranking


def test_legacy_funnel_passed_and_request_params_do_not_prove_sample_scope() -> None:
    payload = {
        "status": "passed", "params": {"sample_role": "in_sample"},
        "latest_result": {"exists": True, "ok": True, "candidate_count": 5},
    }
    html = _render("renderStrategyScreeningStatus", "strategyScreeningPanel", payload)
    assert "passed" in html
    assert "样本范围未声明" in html
    assert "样本外验证状态未知，尚未建立" in html
    assert "样本内研究" not in html


@pytest.mark.parametrize("role,label", [("in_sample", "样本内研究"), ("unspecified", "样本范围未声明")])
def test_statistical_pass_retains_original_status_and_explicit_scope(role: str, label: str) -> None:
    payload = {"ok": True, "guardrails": {
        "statistical_validation_status": "passed",
        "active_diagnostics": {"stats_validation": {
            "status": "passed", "sample_size": 40, "p_value": 0.025,
            "sample_role": role, "out_of_sample_validation": "not_established", "sample_limitation": LIMITATION,
        }},
    }}
    html = _render("renderRiskNotifications", "riskNotificationsPanel", payload)
    assert "统计校验（当前样本）" in html
    assert "通过" in html
    assert label in html
    assert "样本外验证未建立" in html
    assert LIMITATION in html


def test_legacy_statistical_pass_without_scope_remains_unknown() -> None:
    payload = {"ok": True, "guardrails": {
        "statistical_validation_status": "passed",
        "active_diagnostics": {"stats_validation": {"status": "passed", "sample_size": 40, "p_value": 0.025}},
    }}
    html = _render("renderRiskNotifications", "riskNotificationsPanel", payload)
    assert "统计校验（当前样本）" in html
    assert "通过" in html
    assert "样本范围未声明" in html
    assert "样本外验证状态未知，尚未建立" in html
    assert LIMITATION in html


@pytest.mark.parametrize("renderer,panel", [
    ("renderBacktest", "backtestPanel"),
    ("renderStrategyScreeningStatus", "strategyScreeningPanel"),
    ("renderRiskNotifications", "riskNotificationsPanel"),
])
def test_sample_limitation_is_escaped(renderer: str, panel: str) -> None:
    hostile = '<img src=x onerror="unexpected()">'
    payload = _backtest_payload()
    payload["research_evidence"] = {"sample_limitation": hostile}
    if renderer == "renderRiskNotifications":
        payload["guardrails"] = {"active_diagnostics": {"stats_validation": {"sample_limitation": hostile}}}
    html = _render(renderer, panel, payload)
    assert hostile not in html
    assert "&lt;img src=x onerror=&quot;unexpected()&quot;&gt;" in html


def test_unsupported_external_oos_claim_stays_unknown() -> None:
    payload = _backtest_payload()
    payload.update(sample_role="out_of_sample", out_of_sample_validation="established")
    html = _render("renderBacktest", "backtestPanel", payload)
    assert "样本范围未声明" in html
    assert "样本外验证状态未知，尚未建立" in html
    assert LIMITATION in html
