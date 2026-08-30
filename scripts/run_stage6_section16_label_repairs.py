from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.stage6_conflict_resolution import (  # noqa: E402
    resolve_stage6_intraday_conflicts,
)
from quantdesk.intraday.intraday_quality_audit import audit_intraday_file  # noqa: E402
from quantdesk.shared import normalize_symbol  # noqa: E402
from quantdesk.utils import atomic_write_json  # noqa: E402

CRITICAL_SYMBOLS = ("002115", "300323", "600744", "600958", "601975")
DATE_RUNS = {
    "2026-07-14": "daily-data-chain-20260719T023012Z-d7327694c3",
    "2026-07-15": "daily-data-chain-20260719T031049Z-2c62640865",
    "2026-07-16": "daily-data-chain-20260719T034629Z-8476b5c9d1",
}
AUTHORIZED_SYMBOLS_BY_DATE = {
    "2026-07-14": CRITICAL_SYMBOLS,
    "2026-07-15": CRITICAL_SYMBOLS,
    "2026-07-16": ("002115", "300323", "601975"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight/apply the bounded Stage 6 section 16 unknown->raw "
            "label repair for critical symbol-dates."
        )
    )
    parser.add_argument("--canonical-dir", default=str(PROJECT_ROOT / "data" / "intraday" / "1m"))
    parser.add_argument("--runs-root", default=str(PROJECT_ROOT / "results" / "data_chain" / "runs"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "results" / "data_chain" / "stage6_section16" / "label_repairs"))
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--apply", action="store_true")
    return parser


def _unknown_symbols(canonical_dir: Path, target_date: str) -> list[str]:
    out: list[str] = []
    for symbol in AUTHORIZED_SYMBOLS_BY_DATE[target_date]:
        path = canonical_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(
            path,
            dtype={"symbol": str, "date": str, "price_basis": str},
            usecols=lambda column: column in {"date", "price_basis"},
        )
        date_rows = frame.loc[frame["date"].astype(str).str[:10] == target_date]
        if not date_rows.empty and date_rows["price_basis"].astype(str).str.lower().eq("unknown").any():
            out.append(symbol)
    return out


def run_repairs(
    *,
    canonical_dir: Path,
    runs_root: Path,
    output_dir: Path,
    backup_dir: Path,
    apply: bool,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for target_date, run_name in DATE_RUNS.items():
        symbols = _unknown_symbols(canonical_dir, target_date)
        staging_dir = runs_root / run_name / "intraday_staging" / "critical"
        output_path = output_dir / f"resolution_{target_date}.json"
        if not symbols:
            results.append(
                {
                    "target_date": target_date,
                    "run_name": run_name,
                    "symbols": [],
                    "status": "no_unknown_labels",
                    "output_path": str(output_path),
                }
            )
            continue
        result = resolve_stage6_intraday_conflicts(
            canonical_dir=canonical_dir,
            staging_dir=staging_dir,
            symbols=[normalize_symbol(item) for item in symbols],
            target_date=target_date,
            run_id=f"section16-label-{target_date.replace('-', '')}",
            backup_dir=backup_dir,
            output_path=output_path,
            apply=apply,
        )
        results.append(
            {
                "target_date": target_date,
                "run_name": run_name,
                "symbols": symbols,
                "status": result.get("status"),
                "decision_counts": {
                    decision: sum(
                        1
                        for item in result.get("items") or []
                        if item.get("decision") == decision
                    )
                    for decision in sorted(
                        {
                            str(item.get("decision") or "")
                            for item in result.get("items") or []
                        }
                    )
                },
                "output_path": str(output_path),
            }
        )
        if apply and result.get("status") not in {"completed", "ready"}:
            break
    post_repair_items = [
        audit_intraday_file(symbol, target_date, intraday_dir=canonical_dir)
        for target_date, symbols in AUTHORIZED_SYMBOLS_BY_DATE.items()
        for symbol in symbols
    ]
    post_repair_payload = {
        "schema_version": 1,
        "contract_reference": "2026-07-19 user ruling section 16",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(post_repair_items),
        "formal_execution_eligible_count": sum(
            item.get("formal_execution_eligible") is True
            for item in post_repair_items
        ),
        "items": post_repair_items,
    }
    quality_audit_name = (
        "post_repair_quality_audit.json"
        if apply
        else "preflight_quality_audit.json"
    )
    quality_audit_path = output_dir / quality_audit_name
    atomic_write_json(quality_audit_path, post_repair_payload)
    resolver_ready = all(
        item["status"] in {"completed", "ready", "no_unknown_labels"}
        for item in results
    )
    post_repair_ready = (
        post_repair_payload["formal_execution_eligible_count"]
        == post_repair_payload["item_count"]
    )
    payload = {
        "schema_version": 1,
        "contract_reference": "2026-07-19 user ruling section 16; task-book sections 14.3/15.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": bool(apply),
        "canonical_dir": str(canonical_dir),
        "critical_symbols": list(CRITICAL_SYMBOLS),
        "dates": list(DATE_RUNS),
        "results": results,
        "status": (
            "completed"
            if apply and resolver_ready and post_repair_ready
            else "ready"
            if not apply and resolver_ready
            else "failed"
        ),
        "quality_audit_phase": "post_repair" if apply else "preflight",
        "post_repair_quality_audit_path": str(quality_audit_path),
        "post_repair_formal_execution_eligible_count": post_repair_payload[
            "formal_execution_eligible_count"
        ],
    }
    summary_path = output_dir / (
        "apply_summary.json" if apply else "preflight_summary.json"
    )
    atomic_write_json(summary_path, payload)
    payload["summary_path"] = str(summary_path)
    if apply:
        atomic_write_json(output_dir / "summary.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_repairs(
        canonical_dir=Path(args.canonical_dir),
        runs_root=Path(args.runs_root),
        output_dir=Path(args.output_dir),
        backup_dir=Path(args.backup_dir or (Path(args.output_dir) / "backups")),
        apply=bool(args.apply),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("status") in {"completed", "ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
