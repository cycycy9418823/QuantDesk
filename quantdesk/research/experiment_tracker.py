from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.config import load_config
from quantdesk.shared import now_text
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "Research experiment log only. Not investment advice. No broker connection. No real orders."
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "output_dir": "results/experiments",
    "index_csv": "results/experiments/index.csv",
    "capture_git_commit": True,
    "failure_mode": "warn",
}
INDEX_FIELDS = [
    "experiment_id",
    "generated_at",
    "app_version",
    "task_type",
    "metric_sharpe",
    "metric_annual_return",
    "metric_total_return",
    "metric_max_drawdown",
    "git_commit",
    "config_hash",
    "output_json",
    "notes",
    "tags",
]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except Exception:
            pass
    for fmt in ("%Y%m%d_%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def load_experiment_tracker_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        loaded = load_config()
        config.update(_as_dict(loaded.get("experiment_tracker")))
    except Exception:
        pass
    if overrides:
        config.update({key: value for key, value in overrides.items() if value not in (None, "")})
    return config


def stable_json_hash(value: Any) -> str:
    safe = json.dumps(_json_safe(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(safe.encode("utf-8")).hexdigest()


def get_git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        commit = completed.stdout.strip()
        return commit or "unknown"
    except Exception:
        return "unknown"


def _short_task(task_type: str) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(task_type or "task").lower()).strip("_")
    return (clean or "task")[:24]


def _next_experiment_id(task_type: str, output_dir: Path, generated_at: datetime | None = None) -> str:
    dt = generated_at or datetime.now()
    prefix = f"exp_{dt.strftime('%Y%m%d_%H%M%S')}_{_short_task(task_type)}"
    counter = 1
    while (output_dir / f"{prefix}_{counter:02d}.json").exists():
        counter += 1
    return f"{prefix}_{counter:02d}"


def _metric(metrics: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in metrics:
            return metrics.get(name)
    return None


def _append_index(index_csv: Path, row: dict[str, Any]) -> None:
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    exists = index_csv.exists()
    with index_csv.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in INDEX_FIELDS})


def log_experiment(
    task_type: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    git_commit: str | None = None,
    output_paths: dict[str, Any] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> str:
    cfg = load_experiment_tracker_config()
    if not bool(cfg.get("enabled", True)):
        return ""
    output_dir = _project_path(cfg.get("output_dir") or DEFAULT_CONFIG["output_dir"])
    index_csv = _project_path(cfg.get("index_csv") or DEFAULT_CONFIG["index_csv"])
    output_dir.mkdir(parents=True, exist_ok=True)
    experiment_id = _next_experiment_id(task_type, output_dir)
    generated_at = now_text()
    safe_params = _json_safe(params or {})
    safe_metrics = _json_safe(metrics or {})
    safe_outputs = _json_safe(output_paths or {})
    commit = git_commit if git_commit is not None else (get_git_commit() if bool(cfg.get("capture_git_commit", True)) else "unknown")
    record = {
        "experiment_id": experiment_id,
        "generated_at": generated_at,
        "app_version": APP_VERSION,
        "task_type": str(task_type or "unknown"),
        "params": safe_params,
        "metrics": safe_metrics,
        "git_commit": commit or "unknown",
        "config_hash": stable_json_hash(safe_params),
        "data_snapshot_hash": "",
        "output_paths": safe_outputs,
        "notes": notes or "",
        "tags": [str(item) for item in (tags or []) if item],
        "disclaimer": DISCLAIMER,
    }
    json_path = output_dir / f"{experiment_id}.json"
    json_path.write_text(json.dumps(_json_safe(record), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    index_row = {
        "experiment_id": experiment_id,
        "generated_at": generated_at,
        "app_version": APP_VERSION,
        "task_type": record["task_type"],
        "metric_sharpe": _metric(safe_metrics, "sharpe", "sharpe_ratio"),
        "metric_annual_return": _metric(safe_metrics, "annual_return", "annualized_return"),
        "metric_total_return": _metric(safe_metrics, "total_return", "net_return"),
        "metric_max_drawdown": _metric(safe_metrics, "max_drawdown"),
        "git_commit": record["git_commit"],
        "config_hash": record["config_hash"],
        "output_json": _display_path(json_path),
        "notes": notes or "",
        "tags": ",".join(record["tags"]),
    }
    _append_index(index_csv, _json_safe(index_row))
    return experiment_id


def log_experiment_non_blocking(
    task_type: str,
    params: dict[str, Any] | None,
    metrics: dict[str, Any] | None,
    *,
    output_paths: dict[str, Any] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    try:
        experiment_id = log_experiment(task_type, params or {}, metrics or {}, output_paths=output_paths, notes=notes, tags=tags)
        if experiment_id:
            return {"ok": True, "experiment_id": experiment_id, "warning": ""}
        return {"ok": True, "experiment_id": "", "warning": "experiment_tracker_disabled"}
    except Exception as exc:
        return {"ok": False, "experiment_id": "", "warning": f"experiment_tracker_failed:{exc}"}


def _read_index(index_csv: str | Path | None = None) -> list[dict[str, Any]]:
    path = _project_path(index_csv or load_experiment_tracker_config().get("index_csv") or DEFAULT_CONFIG["index_csv"])
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_record(experiment_id: str) -> dict[str, Any]:
    cfg = load_experiment_tracker_config()
    output_dir = _project_path(cfg.get("output_dir") or DEFAULT_CONFIG["output_dir"])
    path = output_dir / f"{experiment_id}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def list_experiments(
    *,
    task_type: str | None = None,
    min_sharpe: float | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
    metric: str | None = None,
) -> list[dict[str, Any]]:
    rows = _read_index()
    after_dt = _parse_dt(after)
    before_dt = _parse_dt(before)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if task_type and row.get("task_type") != task_type:
            continue
        dt = _parse_dt(row.get("generated_at"))
        if after_dt and dt and dt < after_dt:
            continue
        if before_dt and dt and dt > before_dt:
            continue
        sharpe = _safe_float(row.get("metric_sharpe"))
        if min_sharpe is not None and (sharpe is None or sharpe < min_sharpe):
            continue
        filtered.append(row)
    sort_key = {
        "annual_return": "metric_annual_return",
        "annualized_return": "metric_annual_return",
        "total_return": "metric_total_return",
        "net_return": "metric_total_return",
        "sharpe": "metric_sharpe",
        "sharpe_ratio": "metric_sharpe",
        "max_drawdown": "metric_max_drawdown",
    }.get(str(metric or ""), "generated_at")
    if sort_key == "generated_at":
        filtered.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    else:
        filtered.sort(key=lambda item: _safe_float(item.get(sort_key)) or float("-inf"), reverse=True)
    return filtered[: max(1, int(limit or 20))]


def _dict_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys = sorted(set(a) | set(b))
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        if a.get(key) != b.get(key):
            out[key] = {"a": a.get(key), "b": b.get(key)}
    return out


def diff_experiments(id_a: str, id_b: str) -> dict[str, Any]:
    a = _load_record(id_a)
    b = _load_record(id_b)
    warnings: list[str] = []
    if not a:
        warnings.append(f"experiment_missing:{id_a}")
    if not b:
        warnings.append(f"experiment_missing:{id_b}")
    return _json_safe({
        "id_a": id_a,
        "id_b": id_b,
        "available": bool(a and b),
        "params_diff": _dict_diff(_as_dict(a.get("params")), _as_dict(b.get("params"))) if a and b else {},
        "metrics_diff": _dict_diff(_as_dict(a.get("metrics")), _as_dict(b.get("metrics"))) if a and b else {},
        "warnings": warnings,
    })


def summarize_experiment_index(
    *,
    task_type: str | None = None,
    min_sharpe: float | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
    metric: str | None = None,
    diff_ids: list[str] | None = None,
) -> dict[str, Any]:
    rows = list_experiments(task_type=task_type, min_sharpe=min_sharpe, after=after, before=before, limit=limit, metric=metric)
    diff = diff_experiments(diff_ids[0], diff_ids[1]) if diff_ids and len(diff_ids) >= 2 else {}
    index_path = _project_path(load_experiment_tracker_config().get("index_csv") or DEFAULT_CONFIG["index_csv"])
    warnings = [] if index_path.exists() else ["experiment_index_missing"]
    return _json_safe({
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "experiments",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "network": False,
        "experiment_count": len(rows),
        "latest_experiments": rows,
        "filters": {
            "task_type": task_type or "",
            "min_sharpe": min_sharpe,
            "after": after or "",
            "before": before or "",
            "limit": limit,
            "metric": metric or "",
        },
        "diff": diff,
        "warnings": warnings + _as_list(diff.get("warnings")),
        "output_paths": {
            "index_csv": _display_path(index_path),
            "experiments_dir": _display_path(load_experiment_tracker_config().get("output_dir") or DEFAULT_CONFIG["output_dir"]),
        },
        "disclaimer": DISCLAIMER,
    })


def load_experiment_tracker_snapshot(
    index_csv: str | Path = "results/experiments/index.csv",
    *,
    stale_hours: int = 48,
    max_preview_items: int = 5,
) -> dict[str, Any]:
    path = _project_path(index_csv)
    if not path.exists():
        return {
            "enabled": True,
            "status": "missing",
            "experiment_count": 0,
            "latest_experiments": [],
            "latest_index": _display_path(path),
            "warnings": [f"experiment_index_missing:{_display_path(path)}"],
            "stale": False,
            "disclaimer": DISCLAIMER,
        }
    rows = _read_index(path)
    rows.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    stale = False
    if rows:
        dt = _parse_dt(rows[0].get("generated_at"))
        stale = bool(dt and datetime.now() - dt > timedelta(hours=max(0, int(stale_hours))))
    warnings = [f"experiment_index_stale:{rows[0].get('generated_at')}"] if stale and rows else []
    return _json_safe({
        "enabled": True,
        "status": "stale" if stale else "passed",
        "experiment_count": len(rows),
        "latest_experiments": rows[: max(1, int(max_preview_items or 5))],
        "latest_index": _display_path(path),
        "warnings": warnings,
        "stale": stale,
        "disclaimer": DISCLAIMER,
    })


def render_daily_experiment_tracker_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    snapshot = _as_dict(snapshot)
    lines = [
        "## Experiment Tracker Snapshot",
        "",
        DISCLAIMER,
        "",
        f"- Status: `{snapshot.get('status') or 'missing'}`",
        f"- Experiment count: {snapshot.get('experiment_count') or 0}",
        f"- Index: {snapshot.get('latest_index') or 'results/experiments/index.csv'}",
        "",
        "### Latest Experiments",
        "",
    ]
    rows = _as_list(snapshot.get("latest_experiments"))
    if rows:
        lines += ["| Experiment | Task | Generated At | Sharpe | Annual Return |", "|---|---|---|---:|---:|"]
        for row in rows[:5]:
            item = _as_dict(row)
            lines.append(
                f"| {item.get('experiment_id')} | {item.get('task_type')} | {item.get('generated_at')} | "
                f"{item.get('metric_sharpe')} | {item.get('metric_annual_return')} |"
            )
    else:
        lines.append("- unavailable")
    warnings = _as_list(snapshot.get("warnings"))
    lines += ["", "### Warnings", ""]
    lines.extend(f"- {item}" for item in warnings[:5]) if warnings else lines.append("- none")
    lines.append("")
    return "\n".join(lines)
