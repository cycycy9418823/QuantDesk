from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from v360_cli_common import run_builder  # noqa: E402


def _builder_map():
    from quantdesk.data.dragon_tiger import build_dragon_tiger_report
    from quantdesk.data.fundamental_metrics import build_fundamental_metrics_report
    from quantdesk.data.money_flow import build_money_flow_report
    from quantdesk.notifications.notification_center import build_notification_center_report
    from quantdesk.research.barra_style_attribution import build_style_attribution_report
    from quantdesk.research.daily_candidate_review import build_daily_candidate_review_report
    from quantdesk.research.decision_journal import build_decision_journal_report
    from quantdesk.research.dynamic_factor_model import build_dynamic_factor_model_report
    from quantdesk.research.execution_realism import build_execution_realism_report
    from quantdesk.research.paper_watchlist import build_paper_watchlist_report
    from quantdesk.research.return_explain import build_return_explain_report
    from quantdesk.research.similar_day import build_similar_day_report
    from quantdesk.research.stock_picking_flow import build_stock_picking_flow_report
    from quantdesk.research.stress_scenarios import build_stress_scenarios_report
    from quantdesk.risk.risk_alerts import build_risk_alerts_report
    from quantdesk.risk.smart_stop import build_smart_stop_report
    from quantdesk.storage.local_store import build_local_store_report

    return {
        "smart_stop": (build_smart_stop_report, "Run paper smart stop warnings."),
        "similar_day": (build_similar_day_report, "Find similar days."),
        "stress_test": (build_stress_scenarios_report, "Run stress scenarios."),
        "style_attribution": (build_style_attribution_report, "Run style attribution lite."),
        "decision_journal": (build_decision_journal_report, "Manage decision journal."),
        "dragon_tiger": (build_dragon_tiger_report, "Read local dragon tiger CSV."),
        "dynamic_factor_model": (build_dynamic_factor_model_report, "Run dynamic factor model lite."),
        "execution_realism": (build_execution_realism_report, "Run optional execution realism model."),
        "fundamental_metrics": (build_fundamental_metrics_report, "Read local fundamental metrics."),
        "local_store": (build_local_store_report, "Run optional local store."),
        "money_flow": (build_money_flow_report, "Read local money flow CSV."),
        "notification_center": (build_notification_center_report, "Run dry-run notification center."),
        "paper_watchlist": (build_paper_watchlist_report, "Manage research-only paper watchlist."),
        "return_explain": (build_return_explain_report, "Explain return contributors."),
        "risk_alerts": (build_risk_alerts_report, "Run research risk alerts."),
        "stock_picking_flow": (build_stock_picking_flow_report, "Run real-use stock picking flow."),
        "daily_candidate_review": (build_daily_candidate_review_report, "Review paper candidates."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run consolidated v3.6-style research builders.")
    parser.add_argument("builder", choices=sorted(_builder_map()))
    args, rest = parser.parse_known_args(argv)
    builder, description = _builder_map()[args.builder]
    return run_builder(builder, args.builder, description=description, argv=rest)


if __name__ == "__main__":
    raise SystemExit(main())
