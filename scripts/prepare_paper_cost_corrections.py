"""Preview approved paper cost corrections; writes only a new external preview."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.paper.ledger_read_contract import IDENTITY_FIELDS  # noqa: E402
from quantdesk.paper.realized_corrections import prepare_cost_correction_bytes  # noqa: E402


def _external_output(path: Path, source: Path) -> Path:
    output = path.resolve()
    protected = {PROJECT_ROOT.resolve()}
    for parent in source.resolve().parents:
        if any((parent / marker).exists() for marker in ("CHARTER.md", "AGENTS.md", ".git")):
            protected.add(parent)
    if any(output == root or root in output.parents for root in protected):
        raise ValueError("cost_correction_output_must_be_outside_project")
    if output.exists():
        raise ValueError("cost_correction_output_already_exists")
    return output


def _read_stable(path: Path) -> bytes:
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("cost_correction_source_changed_during_read")
    return raw


def preview(source: Path, *, observed_at: str, output_dir: Path | None = None) -> dict[str, Any]:
    """Read an immutable input image and optionally save a reviewable proposal."""
    source = source.resolve(strict=True)
    output = _external_output(output_dir, source) if output_dir is not None else None
    raw = _read_stable(source)
    original = json.loads(raw.decode("utf-8-sig"))
    prepared = prepare_cost_correction_bytes(raw, observed_at=observed_at)
    proposed = prepared["state"]
    if _read_stable(source) != raw:
        raise ValueError("cost_correction_source_changed_during_preview")
    invariants = ("cash", "total_equity", "initial_cash", "legacy_realized_adjustment",
        "valuation_date", *IDENTITY_FIELDS, "actions", "daily_actions", "closed_positions", "realized_reductions")
    changed_invariants = [key for key in invariants if original.get(key) != proposed.get(key)]
    if changed_invariants:
        raise ValueError("cost_correction_protected_fields_changed:" + ",".join(changed_invariants))
    fields = ("realized_pnl", "unrealized_pnl", "legacy_realized_adjustment",
        "realized_corrections_total", "effective_realized_adjustment", "cash", "market_value", "total_equity")
    report = {"schema": "paper_cost_correction_preview.v1", "status": "candidate_only",
        "source_path": str(source), "source_sha256": prepared["source_sha256"],
        "proposed_sha256": prepared["proposed_sha256"], "observed_at": observed_at,
        "ledger_identity": {key: original.get(key) for key in IDENTITY_FIELDS},
        "valuation_date": original.get("valuation_date"),
        "before": {key: original.get(key) for key in fields},
        "after": {key: proposed.get(key) for key in fields},
        "added_corrections": prepared["added_corrections"],
        "skipped_records": prepared["skipped_records"],
        "accounting_reconciliation": proposed["accounting_reconciliation"],
        "original_arrays_preserved": prepared["original_arrays_preserved"],
        "unchanged_fields_checked": list(invariants), "source_unchanged": True,
        "historical_cashflow_verified": False, "formal_state_written": False,
        "network_used": False, "model_used": False, "external_notification_sent": False,
        "installed": False, "label": "纸面 · 未验证"}
    if output is not None:
        output.mkdir(parents=True, exist_ok=False)
        # Exclusive new files keep earlier proposals and original evidence intact.
        for name, content in (("original_ledger.json", raw),
                              ("proposed_ledger.json", prepared["proposed_bytes"])):
            with (output / name).open("xb") as handle:
                handle.write(content)
        report["preview_directory"] = str(output)
        report["proposed_ledger_path"] = str(output / "proposed_ledger.json")
        with (output / "preview.json").open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=PROJECT_ROOT / "state/auto_paper_portfolio.json")
    parser.add_argument("--output-dir", type=Path, help="new preview folder outside source/runtime projects")
    parser.add_argument("--observed-at", default=None, help="timezone-aware observation time; default is current UTC")
    args = parser.parse_args(argv)
    try:
        report = preview(args.state, observed_at=args.observed_at or datetime.now(timezone.utc).isoformat(),
                         output_dir=args.output_dir)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        print(json.dumps({"schema": "paper_cost_correction_preview.v1", "status": "not_prepared",
            "reason": str(exc), "installed": False, "formal_state_written": False,
            "network_used": False}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
