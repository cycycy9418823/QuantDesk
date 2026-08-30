from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantdesk.candidate_monitoring import (
    PROHIBITED_USAGE,
    refresh_candidate_monitoring,
    render_monitoring_markdown,
    write_json,
    write_text,
)
from quantdesk.csv_io import read_csv_frame
from quantdesk.research_candidate_schema import validate_file


def _copy_inputs(dst: Path, *, source_root: Path = ROOT) -> dict[str, Path]:
    dst.mkdir(parents=True, exist_ok=True)
    mapping = {
        "registry": source_root / "results" / "research_candidate_registry_latest.json",
        "portfolio": source_root / "results" / "portfolio_daily_equity_rebuild_latest.csv",
        "low_vol": source_root / "results" / "low_vol_canonical_daily_equity_latest.csv",
        "rev120": source_root / "results" / "rev120_bcanonical_daily_equity_latest.csv",
        "monitoring": source_root / "results" / "portfolio_candidate_monitoring_refresh_latest.json",
        "dashboard": source_root / "results" / "dashboard_candidate_cards_latest.json",
    }
    copied: dict[str, Path] = {}
    for key, src in mapping.items():
        target = dst / src.name
        if src.exists():
            shutil.copy2(src, target)
        copied[key] = target
    return copied


def _run_refresh(paths: dict[str, Path]) -> dict[str, Any]:
    try:
        payload = refresh_candidate_monitoring(
            registry_path=paths["registry"],
            portfolio_csv=paths["portfolio"],
            low_vol_csv=paths["low_vol"],
            rev120_csv=paths["rev120"],
        )
        text = render_monitoring_markdown(payload)
        traceback = "Traceback" in text or any("Traceback" in str(v) for v in payload.values())
        return {
            "graceful_fail": True,
            "no_traceback": not traceback,
            "monitoring_status": payload.get("monitoring_status"),
            "status": payload.get("status"),
            "human_review_or_data_issue": payload.get("monitoring_status") in {"human_review_required", "data_issue"},
            "not_candidate_healthy": payload.get("monitoring_status") != "candidate_healthy",
            "no_buy_sell_advice": payload.get("no_buy_sell_or_rebalance_instruction", True) is True,
            "failure_reasons": payload.get("failure_reasons", []),
            "warnings": payload.get("warnings", []),
        }
    except Exception as exc:
        return {
            "graceful_fail": False,
            "no_traceback": False,
            "monitoring_status": "exception",
            "status": "failed",
            "human_review_or_data_issue": False,
            "not_candidate_healthy": True,
            "no_buy_sell_advice": True,
            "failure_reasons": [str(exc)],
            "warnings": [],
        }


def _portfolio_frame(path: Path) -> pd.DataFrame:
    return read_csv_frame(path)


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False)


def _break_portfolio(path: Path, scenario: str) -> None:
    frame = _portfolio_frame(path)
    combo = frame["portfolio_name"].eq("combo_50_50")
    idx = frame.index[combo][10]
    if scenario == "nan":
        frame.loc[idx, "daily_return"] = np.nan
    elif scenario == "inf":
        frame.loc[idx, "daily_return"] = np.inf
    elif scenario == "cash_nonzero":
        cash_idx = frame.index[combo & frame["cash_flag"].astype(bool)][0]
        frame.loc[cash_idx, "daily_return"] = 0.01
    elif scenario == "mdd_worse_than_minus36":
        frame.loc[idx, "daily_return"] = -0.36
    elif scenario == "rolling_252_negative":
        tail_idx = frame.index[combo][-252:]
        frame.loc[tail_idx, "daily_return"] = -0.002
    _write_frame(path, frame)


def _json_mutation(path: Path, scenario: str, kind: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if scenario == "illegal_monitoring_status":
        payload["monitoring_status"] = "definitely_not_valid"
    elif scenario == "dashboard_missing_prohibited_usage":
        payload.pop("prohibited_usage", None)
        if payload.get("cards"):
            payload["cards"][0].pop("prohibited_usage", None)
    elif scenario == "registry_missing_candidate_id":
        payload["candidate_pool"][0].pop("candidate_id", None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    validation = validate_file(path, kind)
    return {
        "graceful_fail": True,
        "no_traceback": True,
        "monitoring_status": payload.get("monitoring_status", "schema_invalid"),
        "status": "schema_validation",
        "human_review_or_data_issue": not validation["valid"],
        "not_candidate_healthy": payload.get("monitoring_status") != "candidate_healthy",
        "no_buy_sell_advice": True,
        "failure_reasons": validation["errors"],
        "warnings": [],
        "schema_valid": validation["valid"],
    }


def run_failure_injection(root: Path = ROOT) -> dict[str, Any]:
    base = root / "test_runtime" / "candidate_monitoring_failure_injection"
    if base.exists():
        shutil.rmtree(base)
    scenarios: list[dict[str, Any]] = []
    scenario_names = [
        "missing_portfolio_daily_equity_rebuild_latest_csv",
        "missing_rev120_bcanonical_daily_equity_latest_csv",
        "portfolio_csv_nan",
        "portfolio_csv_inf",
        "cash_day_daily_return_nonzero",
        "illegal_monitoring_status",
        "dashboard_card_missing_prohibited_usage",
        "registry_missing_candidate_id",
        "candidate_daily_mdd_worse_than_minus36",
        "rolling_252d_return_negative",
    ]
    for name in scenario_names:
        scenario_dir = base / name
        paths = _copy_inputs(scenario_dir, source_root=root)
        if name == "missing_portfolio_daily_equity_rebuild_latest_csv":
            paths["portfolio"].unlink(missing_ok=True)
            result = _run_refresh(paths)
        elif name == "missing_rev120_bcanonical_daily_equity_latest_csv":
            paths["rev120"].unlink(missing_ok=True)
            result = _run_refresh(paths)
        elif name == "portfolio_csv_nan":
            _break_portfolio(paths["portfolio"], "nan")
            result = _run_refresh(paths)
        elif name == "portfolio_csv_inf":
            _break_portfolio(paths["portfolio"], "inf")
            result = _run_refresh(paths)
        elif name == "cash_day_daily_return_nonzero":
            _break_portfolio(paths["portfolio"], "cash_nonzero")
            result = _run_refresh(paths)
        elif name == "illegal_monitoring_status":
            result = _json_mutation(paths["monitoring"], "illegal_monitoring_status", "monitoring")
        elif name == "dashboard_card_missing_prohibited_usage":
            result = _json_mutation(paths["dashboard"], "dashboard_missing_prohibited_usage", "dashboard_cards")
        elif name == "registry_missing_candidate_id":
            result = _json_mutation(paths["registry"], "registry_missing_candidate_id", "registry")
        elif name == "candidate_daily_mdd_worse_than_minus36":
            _break_portfolio(paths["portfolio"], "mdd_worse_than_minus36")
            result = _run_refresh(paths)
        else:
            _break_portfolio(paths["portfolio"], "rolling_252_negative")
            result = _run_refresh(paths)
        result["scenario"] = name
        result["passed"] = (
            result["graceful_fail"]
            and result["no_traceback"]
            and result["not_candidate_healthy"]
            and result["no_buy_sell_advice"]
            and result["human_review_or_data_issue"]
        )
        scenarios.append(result)
    failed = [s for s in scenarios if not s["passed"]]
    return {
        "task_id": "candidate_monitoring_failure_injection",
        "status": "passed" if not failed else "failed",
        "research_only": True,
        "prohibited_usage": PROHIBITED_USAGE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scenario_count": len(scenarios),
        "passed_count": len(scenarios) - len(failed),
        "failed_count": len(failed),
        "scenarios": scenarios,
        "failure_reasons": [f"{s['scenario']}: {s['failure_reasons']}" for s in failed],
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        "# Candidate Monitoring Failure Injection",
        "",
        f"Status: `{payload['status']}`",
        f"Passed: `{payload['passed_count']}`",
        f"Failed: `{payload['failed_count']}`",
        "",
        "| scenario | passed | monitoring_status | graceful | no traceback | not healthy |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for item in payload["scenarios"]:
        lines.append(
            f"| {item['scenario']} | {item['passed']} | {item['monitoring_status']} | {item['graceful_fail']} | {item['no_traceback']} | {item['not_candidate_healthy']} |"
        )
    lines += [
        "",
        "All scenarios use copied inputs under `test_runtime/`; canonical results are not mutated.",
        "Boundary: research-only; no live trading, broker connection, auto execution, buy/sell recommendation, or real-money allocation instruction.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run candidate monitoring failure injection checks.")
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    payload = run_failure_injection(root)
    write_json(root / "results" / "candidate_monitoring_failure_injection_latest.json", payload)
    write_text(root / "reports" / "candidate_monitoring_failure_injection_latest.md", render(payload))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
