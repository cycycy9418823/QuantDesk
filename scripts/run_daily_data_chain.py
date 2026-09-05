from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_data_chain import run_daily_data_chain  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402


def stamp_pipeline_run_id(
    root: Path,
    payload: dict,
    pipeline_run_id: str,
) -> None:
    """Write pipeline_run_id onto latest AND the run archive so they stay equal."""

    stamp = str(pipeline_run_id or "").strip()
    if not stamp:
        return
    payload["pipeline_run_id"] = stamp
    run_id = str(payload.get("run_id") or "").strip()
    targets = [root / "results" / "data_chain" / "daily_data_chain_latest.json"]
    if run_id:
        targets.append(root / "results" / "data_chain" / "runs" / run_id / "manifest.json")
    manifest_rel = str(payload.get("manifest_path") or "").strip()
    if manifest_rel:
        extra = Path(manifest_rel)
        if not extra.is_absolute():
            extra = root / extra
        if extra not in targets:
            targets.append(extra)
    seen: set[Path] = set()
    for path in targets:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(disk, dict):
            continue
        disk["pipeline_run_id"] = stamp
        path.write_text(
            json.dumps(disk, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the authoritative QuantDesk daily data chain."
    )
    parser.add_argument("--target-date", default="")
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--dry-run", default="false")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--batch-workers", type=int, default=4)
    parser.add_argument("--retry-from-latest", default="true")
    parser.add_argument("--skip-analysis-pool-minute", default="false")
    parser.add_argument("--rebuild-features", default="true")
    parser.add_argument("--refresh-universe", default="true")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--reuse-upstream-manifest", default="")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", default="false")
    parser.add_argument("--pipeline-run-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    upstream_reuse = None
    reuse_arg = str(args.reuse_upstream_manifest or "").strip()
    if reuse_arg:
        candidate = Path(reuse_arg)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {"schema_version": "unreadable"}
        upstream_reuse = loaded if isinstance(loaded, dict) else {"schema_version": "not_object"}
    payload = run_daily_data_chain(
        project_root=PROJECT_ROOT,
        target_date=args.target_date or None,
        symbols=symbols,
        network=bool(args.enable_network or truthy(args.network)),
        dry_run=truthy(args.dry_run),
        batch_size=max(1, int(args.batch_size)),
        batch_workers=max(1, min(int(args.batch_workers), 8)),
        rebuild_features=truthy(args.rebuild_features),
        retry_from_latest=truthy(args.retry_from_latest),
        skip_analysis_pool_minute=truthy(args.skip_analysis_pool_minute),
        refresh_universe=truthy(args.refresh_universe),
        trigger=args.trigger,
        upstream_reuse=upstream_reuse,
    )
    pipeline_run_id = str(args.pipeline_run_id or "").strip()
    if pipeline_run_id:
        stamp_pipeline_run_id(PROJECT_ROOT, payload, pipeline_run_id)
    if truthy(args.json):
        latest = PROJECT_ROOT / "results" / "data_chain" / "daily_data_chain_latest.json"
        printed = dict(payload)
        if latest.is_file():
            printed["artifact_path"] = "results/data_chain/daily_data_chain_latest.json"
            printed["artifact_sha256"] = "sha256:" + hashlib.sha256(latest.read_bytes()).hexdigest()
        print(json.dumps(printed, ensure_ascii=False, indent=None if truthy(args.summary) else 2))
    else:
        coverage = payload.get("coverage") or {}
        print(
            f"daily-data-chain status={payload.get('status')} "
            f"target={payload.get('target_date')} run_id={payload.get('run_id')}"
        )
        print(
            "daily={daily} critical={critical} analysis={analysis}".format(
                daily=(coverage.get("daily") or {}).get("coverage_ratio"),
                critical=(coverage.get("critical") or {}).get("coverage_ratio"),
                analysis=(coverage.get("current_analysis_pool") or {}).get("coverage_ratio"),
            )
        )
        print(payload.get("next_action") or "")
    status = str(payload.get("status") or "")
    if status in {"completed", "dry_run", "skipped_already_completed"}:
        return 0
    if status == "partial":
        return 2
    if status == "busy":
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
