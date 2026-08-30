"""QuantDesk shared package with v3.5.2 compatibility aliases."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

_MOVED_MODULES = {
    "factor_evaluation": "quantdesk.research.factor_evaluation",
    "factor_combiner": "quantdesk.research.factor_combiner",
    "combined_score_backtest": "quantdesk.research.combined_score_backtest",
    "factor_research_dashboard": "quantdesk.research.factor_research_dashboard",
    "sector_rotation_engine": "quantdesk.research.sector_rotation_engine",
    "point_in_time_universe": "quantdesk.research.point_in_time_universe",
    "experiment_tracker": "quantdesk.research.experiment_tracker",
    "chart_renderer": "quantdesk.research.chart_renderer",
    "risk_attribution": "quantdesk.research.risk_attribution",
    "risk_attribution_lite": "quantdesk.research.risk_attribution_lite",
    "factor_attribution": "quantdesk.research.factor_attribution",
    "strategy_recommender": "quantdesk.research.strategy_recommender",
    "daily_strategy_recommendation_snapshot": "quantdesk.research.daily_strategy_recommendation_snapshot",
    "parameter_sensitivity_audit": "quantdesk.research.parameter_sensitivity_audit",
    "backtest_statistical_validation": "quantdesk.research.backtest_statistical_validation",
    "feature_quality_audit": "quantdesk.research.feature_quality_audit",
    "daily_research_snapshot": "quantdesk.research.daily_research_snapshot",
    "daily_experiment_tracker_snapshot": "quantdesk.research.daily_experiment_tracker_snapshot",
    "daily_risk_attribution_snapshot": "quantdesk.research.daily_risk_attribution_snapshot",
    "backtest_export": "quantdesk.research.backtest_export",
    "backtest_simulation": "quantdesk.research.backtest_simulation",
    "intraday_data_policy": "quantdesk.intraday.intraday_data_policy",
    "intraday_score": "quantdesk.intraday.intraday_score",
    "intraday_live_snapshot": "quantdesk.intraday.intraday_live_snapshot",
    "intraday_live_engine": "quantdesk.intraday.intraday_live_engine",
    "daily_intraday_live_snapshot": "quantdesk.intraday.daily_intraday_live_snapshot",
    "intraday_coverage_dashboard": "quantdesk.intraday.intraday_coverage_dashboard",
    "intraday_data_workbench": "quantdesk.intraday.intraday_data_workbench",
    "intraday_gap_checker": "quantdesk.intraday.intraday_gap_checker",
    "intraday_import": "quantdesk.intraday.intraday_import",
    "intraday_monitor": "quantdesk.intraday.intraday_monitor",
    "intraday_providers": "quantdesk.intraday.intraday_providers",
    "intraday_provider_capability": "quantdesk.intraday.intraday_provider_capability",
    "intraday_quality_audit": "quantdesk.intraday.intraday_quality_audit",
    "intraday_replay_self_validation": "quantdesk.intraday.intraday_replay_self_validation",
    "intraday_schema": "quantdesk.intraday.intraday_schema",
    "intraday_swing_strategy": "quantdesk.intraday.intraday_swing_strategy",
    "intraday_update_engine": "quantdesk.intraday.intraday_update_engine",
    "multi_strategy_account_book": "quantdesk.paper.multi_strategy_account_book",
    "auto_paper_portfolio": "quantdesk.paper.auto_paper_portfolio",
    "paper_trading": "quantdesk.paper.paper_trading",
    "conditional_trade_plan": "quantdesk.paper.conditional_trade_plan",
    "position_management_engine": "quantdesk.paper.position_management_engine",
    "strategy_trade_replay": "quantdesk.paper.strategy_trade_replay",
    "paper_review": "quantdesk.paper.paper_review",
    "paper_vs_backtest_consistency": "quantdesk.paper.paper_vs_backtest_consistency",
    "agent_policy_guard": "quantdesk.agent.agent_policy_guard",
    "agent_evidence_pack": "quantdesk.agent.agent_evidence_pack",
    "agent_hallucination_guard": "quantdesk.agent.agent_hallucination_guard",
    "agent_report_audit": "quantdesk.agent.agent_report_audit",
    "agent_safe_bridge": "quantdesk.agent.agent_safe_bridge",
}

_ROOT_EXPORTS = {
    "build_risk_attribution_report": "quantdesk.research.risk_attribution",
    "render_risk_attribution_markdown": "quantdesk.research.risk_attribution",
}


class _MovedModuleAliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname: str, path: object | None = None, target: object | None = None) -> object | None:
        if fullname.startswith(f"{__name__}.") and fullname.split(".", 1)[1] in _MOVED_MODULES:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec: object) -> object:
        old_name = str(getattr(spec, "name", "")).split(".", 1)[1]
        module = importlib.import_module(_MOVED_MODULES[old_name])
        sys.modules[f"{__name__}.{old_name}"] = module
        return module

    def exec_module(self, module: object) -> None:
        return None


if not any(isinstance(finder, _MovedModuleAliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _MovedModuleAliasFinder())


def __getattr__(name: str) -> object:
    if name in _MOVED_MODULES:
        module = importlib.import_module(_MOVED_MODULES[name])
        sys.modules[f"{__name__}.{name}"] = module
        globals()[name] = module
        return module
    if name in _ROOT_EXPORTS:
        value = getattr(importlib.import_module(_ROOT_EXPORTS[name]), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_MOVED_MODULES) | set(_ROOT_EXPORTS))

__all__ = ["build_risk_attribution_report", "render_risk_attribution_markdown"]
