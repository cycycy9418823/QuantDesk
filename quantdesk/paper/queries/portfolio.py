"""Read existing strategy-account artifacts without importing the execution engine.

Paths are explicit. Missing data, evidence warnings and the legacy result schema
are preserved. This layer neither initializes nor repairs an account.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from quantdesk.paper.ledger_read_contract import inspect_strategy_ledger
from quantdesk.paper.performance_report import finite_number, iso_date, observed_history
from quantdesk.paper.state_store import PaperStateStore
from quantdesk.plan_review_receipts import STORE_PATH, load_plan_review_snapshot
from quantdesk.utils import read_json_safe, safe_int

DISCLAIMER = "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。"
BROKER_BOUNDARY = "不接券商，不真实下单，不保存账户密码。"


@dataclass(frozen=True)
class PaperQueryPaths:
    """All locations needed by paper queries, with no implicit working directory."""

    project_root: Path
    review_root: Path
    reports: Path
    state: Path
    latest: Path
    history: Path
    shared_plan: Path
    conditional_plan: Path
    conditional_execution: Path
    swing_plan: Path
    intraday_execution: Path

    @classmethod
    def from_root(cls, root: Path) -> PaperQueryPaths:
        """Use the established filenames beneath an explicitly chosen project."""
        root = Path(root)
        results = root / "results"
        return cls(
            project_root=root, review_root=root, reports=root / "reports",
            state=root / "state" / "auto_paper_portfolio.json",
            latest=results / "auto_paper_portfolio_latest.json",
            history=results / "auto_paper_portfolio_history.json",
            shared_plan=results / "shared_paper_plan_latest.json",
            conditional_plan=results / "conditional_trade_plans_latest.json",
            conditional_execution=results / "conditional_execution_latest.json",
            swing_plan=results / "swing_trade_plans_latest.json",
            intraday_execution=results / "intraday_execution_latest.json",
        )


@dataclass(frozen=True)
class PaperLatestReadInputs:
    """File contents and recovery selection facts consumed by the latest query."""

    files: tuple[Path, ...]
    backup_order: tuple[str, ...]
    bad_marker_exists: bool


def auto_paper_portfolio_latest_read_inputs(paths: PaperQueryPaths) -> PaperLatestReadInputs:
    """Describe reads without running validation, recovery or any account writes."""
    store = PaperStateStore(paths.state, schema_version=None)
    backups = store._backups()
    return PaperLatestReadInputs(
        files=(paths.latest, paths.review_root / STORE_PATH / "latest.json",
               paths.state, store._identity_path(), *backups),
        backup_order=tuple(str(path) for path in backups),
        bad_marker_exists=any(paths.state.parent.glob(f"{paths.state.stem}.bad_*.json")),
    )


def newest_auto_paper_report(reports_dir: Path) -> Path | None:
    if not reports_dir.exists():
        return None
    files = sorted(reports_dir.glob("auto_paper_portfolio_report_*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None



def newest_swing_execution_report(reports_dir: Path) -> Path | None:
    if not reports_dir.exists():
        return None
    files = sorted(reports_dir.glob("swing_execution_report_*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None



def shared_paper_plan_latest(
    paths: PaperQueryPaths, *, disclaimer: str, broker_boundary: str,
    json_reader: Callable[..., Any] = read_json_safe,
) -> dict[str, Any]:
    """Read-only view of the latest shared paper-capital plan.

    Stage-1 boundary: this only *reads* the plan produced by the shared
    allocation planner so the auto paper portfolio can display it. It never
    executes the plan, never mutates paper state, and never places orders.
    """

    data = json_reader(paths.shared_plan, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "plan_executed": False,
            "message": "尚未生成共享纸面资金计划。请先运行 shared-paper-plan。",
            "disclaimer": disclaimer,
            "broker_boundary": broker_boundary,
        }
    data["exists"] = True
    # Defensive: this accessor must never imply execution in stage 1.
    data["plan_executed"] = False
    data.setdefault("disclaimer", disclaimer)
    data.setdefault("broker_boundary", broker_boundary)
    return data



def auto_paper_portfolio_latest(
    paths: PaperQueryPaths, *, disclaimer: str, broker_boundary: str,
    json_reader: Callable[..., Any] = read_json_safe,
    review_loader: Callable[..., dict[str, Any]] = load_plan_review_snapshot,
    ledger_inspector: Callable[..., dict[str, Any]] = inspect_strategy_ledger,
) -> dict[str, Any]:
    data = json_reader(paths.latest, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "message": "尚未初始化自动虚拟组合。请先确认目标日期的正式候选批次，再初始化组合。",
            **review_loader(paths.review_root),
            "disclaimer": disclaimer,
            "broker_boundary": broker_boundary,
        }
    data["exists"] = True
    data.setdefault("disclaimer", disclaimer)
    data.setdefault("broker_boundary", broker_boundary)
    data.update(review_loader(paths.review_root))
    ledger_health = ledger_inspector(paths.state,
        target_date=str(data.get("valuation_date") or "")[:10], latest=data)
    data["ledger_health"] = ledger_health
    if not ledger_health["ok"]:
        data["portfolio_review_valid"] = False
        data["warnings"] = list(dict.fromkeys([*(data.get("warnings") or []), *ledger_health["reason_codes"]]))
    return data



def auto_paper_portfolio_equity_history(paths: PaperQueryPaths, limit: int = 250) -> dict[str, Any]:
    """Return the local paper-equity history without mutating runtime state."""

    data = read_json_safe(paths.history, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "status": "missing",
            "items": [],
            "latest": {},
            "source_path": str(paths.history),
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "message_for_ui": "尚未生成纸面净值历史。",
        }

    raw_items = data.get("items")
    structure_invalid = not isinstance(raw_items, list)
    rows = raw_items if isinstance(raw_items, list) else []
    evidence = observed_history({"items": rows})
    # Metadata is attached only after the shared reader has rejected missing
    # valuation dates, invalid amounts and contradictory same-day observations.
    by_date = {iso_date(raw.get("valuation_date")): raw for raw in rows if isinstance(raw, dict)}
    bounded_limit = max(2, min(2_000, safe_int(limit, 250)))
    items = []
    for point in evidence.points[-bounded_limit:]:
        raw = by_date[point.date]
        items.append({
            "date": point.date,
            "valuation_date": point.date,
            "total_equity": point.equity,
            "total_return_pct": finite_number(raw.get("total_return_pct")),
            "cash": point.cash,
            "market_value": point.market_value,
            "position_count": safe_int(raw.get("position_count"), 0),
            "mode": raw.get("mode"),
            "strategy": raw.get("strategy"),
            "source_generated_at": raw.get("generated_at"),
            "portfolio_review_valid": point.review_valid,
        })
    unconfirmed = [point.date for point in evidence.points if point.review_valid is not True]
    warnings = []
    for condition, reason in (
        (structure_invalid, "paper_history_items_invalid"),
        (evidence.undated_count, "paper_history_valuation_dates_missing"),
        (evidence.invalid_count, "paper_history_invalid_records"),
        (evidence.conflicting_dates, "paper_history_conflicting_dates"),
        (evidence.duplicate_count, "paper_history_duplicate_dates"),
        (unconfirmed, "paper_history_review_unconfirmed"),
        (len(items) < 2, "paper_history_insufficient_observations"),
    ):
        if condition:
            warnings.append(reason)
    note = f"保留 {len(items)} 条明确估值日期的已记录权益；纸面 · 未验证。"
    if evidence.undated_count:
        note += f" {evidence.undated_count} 条缺少估值日期，已排除。"
    if structure_invalid or evidence.invalid_count or evidence.conflicting_dates:
        note += " 历史结构、金额或同日记录存在异常，暂不计算对照。"
    if unconfirmed:
        note += f" {len(unconfirmed)} 个日期的复盘尚未确认。"
    return {
        "ok": True,
        "exists": True,
        "status": "degraded" if warnings else "passed",
        "items": items,
        "latest": items[-1] if items else {},
        "as_of": str(data.get("updated_at") or (items[-1]["date"] if items else "")),
        "valuation_date": items[-1]["date"] if items else None,
        "source_path": str(paths.history),
        "history_evidence": {
            "date_source": "valuation_date",
            "raw_row_count": len(rows),
            "observation_count": len(evidence.points),
            "undated_count": evidence.undated_count,
            "invalid_count": evidence.invalid_count,
            "invalid_dates": evidence.invalid_dates,
            "conflicting_dates": evidence.conflicting_dates,
            "duplicate_count": evidence.duplicate_count,
            "structure_invalid": structure_invalid,
            "unconfirmed_review_dates": unconfirmed,
        },
        "warnings": warnings,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "message_for_ui": note,
    }



def auto_paper_portfolio_artifacts(paths: PaperQueryPaths, *, auto_report: Path | None, swing_report: Path | None) -> list[dict[str, Any]]:
    rows = [
        ("自动虚拟组合状态", "JSON", paths.state, "持续虚拟组合状态"),
        ("自动虚拟组合快照", "JSON", paths.latest, "最新组合快照"),
        ("自动虚拟组合历史", "JSON", paths.history, "组合权益历史曲线"),
        ("自动虚拟组合报告", "Markdown", auto_report, "每日虚拟组合复盘报告"),
        ("条件交易计划", "JSON", paths.conditional_plan, "T 日收盘后冻结的下一交易日条件交易计划"),
        ("盘中执行结果", "JSON", paths.conditional_execution, "按本地 1 分钟线顺序触发的条件计划执行结果"),
        ("长线波段计划", "JSON", paths.swing_plan, "长线持仓和候选的次日波段计划"),
        ("盘中触发回放", "JSON", paths.intraday_execution, "分钟线触发结果，不用日线假装成交"),
        ("波段执行报告", "Markdown", swing_report, "长线波段计划与盘中触发复盘报告"),
    ]
    result = []
    for name, kind, path, purpose in rows:
        exists = bool(path and path.exists())
        stat = path.stat() if exists else None
        result.append({
            "name": name,
            "type": kind,
            "path": str(path.relative_to(paths.project_root)) if path else "",
            "exists": exists,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds") if stat else "",
            "file_size": stat.st_size if stat else 0,
            "purpose": purpose,
        })
    return result



def swing_plans_latest(paths: PaperQueryPaths) -> dict[str, Any]:
    data = read_json_safe(paths.swing_plan, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "message": "尚未生成长线波段计划。请先运行自动虚拟组合 update 流程。",
        }
    data["exists"] = True
    return data



def intraday_execution_latest(paths: PaperQueryPaths) -> dict[str, Any]:
    data = read_json_safe(paths.intraday_execution, {})
    if not isinstance(data, dict) or not data:
        return {
            "ok": True,
            "exists": False,
            "message": "尚未生成盘中触发回放结果。请先运行自动虚拟组合 update 流程。",
        }
    data["exists"] = True
    return data

