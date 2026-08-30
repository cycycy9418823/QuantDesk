from __future__ import annotations

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown, synthetic_candidates, truthy


def build_return_explain_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    synthetic = truthy(cfg.get("synthetic_test_data"))
    if synthetic:
        items = [
            {
                "symbol": row["symbol"],
                "sector": row["sector"],
                "contribution": round(0.01 / (idx + 1), 4),
                "summary": "synthetic contributor",
            }
            for idx, row in enumerate(synthetic_candidates())
        ]
        sector_contributors = [{"sector": "电力", "contribution": 0.01}]
        warnings = ["synthetic_test_data: return contributors are test/demo evidence only"]
    else:
        items = []
        sector_contributors = []
        warnings = ["return_contribution_evidence_missing: no contributors were generated"]
    payload = base_payload("return_explain", status="warning", warnings=warnings)
    payload.update(
        {
            "date": cfg.get("date") or "",
            "top_contributors": items,
            "sector_contributors": sector_contributors,
            "items": items,
            "synthetic_test_data": synthetic,
        }
    )
    return finalize_outputs(payload, "return_explain", simple_markdown("Return Explain", payload, "items"))
