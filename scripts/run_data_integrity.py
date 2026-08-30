from v360_cli_common import run_builder

from quantdesk.data.corporate_actions import detect_price_jump_candidates, validate_corporate_actions
from quantdesk.data.price_adjustment import build_adjustment_policy_payload
from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown


def build_data_integrity_report(config):
    policy = build_adjustment_policy_payload(config)
    actions = validate_corporate_actions([], [])
    jumps = detect_price_jump_candidates([{"date": "2026-05-25", "close": 10}, {"date": "2026-05-26", "close": 10.2}])
    warnings = list(policy.get("warnings") or [])
    payload = base_payload("data_integrity", status="warning" if warnings else "passed", warnings=warnings)
    payload.update({"price_adjustment": config.get("adjustment") or "forward", "adjustment_layout_status": policy.get("layout"), "corporate_actions_status": actions, "price_jump_candidates": jumps.get("price_jump_candidates", []), "items": jumps.get("price_jump_candidates", [])})
    return finalize_outputs(payload, "data_integrity", simple_markdown("Data Integrity", payload, "items"))


if __name__ == "__main__":
    raise SystemExit(run_builder(build_data_integrity_report, "data_integrity", description="Check adjusted price data integrity."))
