"""Actionable local remediation guidance for QuantDesk warnings.

This module only translates local status/result payloads into copyable
follow-up commands. It never fetches data, never invokes LLMs, never changes
strategy parameters, and never emits trading advice.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from quantdesk.shared import read_json, write_json
from quantdesk.time_utils import now_text
from quantdesk.trading_calendar import resolve_target_complete_trade_date
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIX_PLAN_PATH = PROJECT_ROOT / "results" / "fix_plan_latest.json"
DISCLAIMER = "仅用于流程修复建议，不构成投资建议。"


WARNING_KIND_META: dict[str, dict[str, str]] = {
    "data_stale": {
        "category": "data",
        "severity": "high",
        "user_message": "本地日线数据过期。",
        "impact": "daily、复盘和验证只能基于滞后数据，可信度下降。",
    },
    "data_missing": {
        "category": "data",
        "severity": "high",
        "user_message": "本地日线数据缺失。",
        "impact": "相关股票无法参与本地日线研究流程。",
    },
    "intraday_missing": {
        "category": "validation",
        "severity": "warning",
        "user_message": "缺少分钟线。",
        "impact": "不能可靠验证盘中触发；系统不会用日线 high/low 假装成交。",
    },
    "provider_unavailable": {
        "category": "system",
        "severity": "warning",
        "user_message": "部分 provider 不可用。",
        "impact": "联网补数时可能需要切换 provider 或安装可选依赖。",
    },
    "provider_skipped_offline": {
        "category": "system",
        "severity": "info",
        "user_message": "离线模式跳过联网 provider。",
        "impact": "这是默认安全行为，不代表系统故障。",
    },
    "guardrail_warning": {
        "category": "validation",
        "severity": "warning",
        "user_message": "daily quick guardrails 有提示。",
        "impact": "这是流程和数据可信度提示，不是策略失败，也不是收益保证。",
    },
    "statistical_insufficient_sample": {
        "category": "validation",
        "severity": "warning",
        "user_message": "统计验证样本不足。",
        "impact": "stats-validate 结论有限，只能作为学习观察。",
    },
    "parameter_sensitivity_warning": {
        "category": "validation",
        "severity": "warning",
        "user_message": "参数敏感性有提示。",
        "impact": "说明结果稳定性需要谨慎解读，不代表应自动调参。",
    },
    "schema_secret_like_reference": {
        "category": "system",
        "severity": "info",
        "user_message": "schema-check 发现低风险敏感词引用。",
        "impact": "通常是路径名、示例字段或 masked value；高风险为 0 时一般不阻断。",
    },
    "schema_confirmed_secret_leak": {
        "category": "system",
        "severity": "critical",
        "user_message": "schema-check 发现高风险密钥泄露。",
        "impact": "需要立即移除真实密钥并重新生成结果文件。",
    },
    "artifact_missing": {
        "category": "artifact",
        "severity": "info",
        "user_message": "结果产物尚未生成。",
        "impact": "clean zip 初次打开没有 results/reports 是正常状态。",
    },
    "no_universe": {
        "category": "data",
        "severity": "high",
        "user_message": "当前没有可分析股票。",
        "impact": "clean zip 初次打开没有本地股票池和行情数据，无法更新 current_analysis_pool。",
    },
    "no_symbols_selected": {
        "category": "data",
        "severity": "high",
        "user_message": "本次没有选中任何股票。",
        "impact": "无法对空股票集合执行 data-update、data-health、daily 或 market-intel。",
    },
    "stale_report": {
        "category": "review",
        "severity": "warning",
        "user_message": "报告可能落后于最新数据。",
        "impact": "需要重新生成 daily 或对应报告。",
    },
    "scope_mismatch": {
        "category": "data",
        "severity": "warning",
        "user_message": "data-update 和 data-health 统计范围不一致。",
        "impact": "首页状态可能口径不同，需要用同一 scope 刷新 data-health。",
    },
    "target_date_mismatch": {
        "category": "data",
        "severity": "warning",
        "user_message": "目标日期不一致。",
        "impact": "不同结果文件不适合直接比较，需要重新运行相关命令。",
    },
    "download_queue_active": {
        "category": "data-gates",
        "severity": "warning",
        "user_message": "扩展数据下载队列仍在运行。",
        "impact": "队列 active 时只能只读监控，不能引导 data-update 或 import/apply。",
    },
    "download_queue_stale": {
        "category": "data-gates",
        "severity": "warning",
        "user_message": "The extended-data download queue is stale.",
        "impact": "Staged imports remain blocked until the stale queue is reviewed.",
    },
}


def _root(project_root: str | Path | None = None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _load(path: str, project_root: str | Path | None = None) -> dict[str, Any]:
    payload = read_json(_root(project_root) / path, {})
    return payload if isinstance(payload, dict) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _iso_date_text(value: Any) -> str:
    text = str(value or "").strip()[:10]
    if not text:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _current_target_date() -> str:
    try:
        return _iso_date_text(resolve_target_complete_trade_date().get("target_complete_trade_date"))
    except Exception:
        return ""


def _command_with_optional_target(command: str, target_date: str = "") -> str:
    text = " ".join(str(command or "").strip().split())
    if not text:
        return ""
    target = _iso_date_text(target_date) or _current_target_date()
    if target:
        return text.replace("YYYY-MM-DD", target)
    return (
        text.replace(" --target-date YYYY-MM-DD", "")
        .replace("--target-date YYYY-MM-DD ", "")
        .replace("--target-date YYYY-MM-DD", "")
        .strip()
    )


def _target_date(*payloads: dict[str, Any]) -> str:
    iso_dates: list[str] = []
    fallback = ""
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        daily_price = payload.get("daily_price_status") if isinstance(payload.get("daily_price_status"), dict) else {}
        freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
        for value in (
            payload.get("target_complete_trade_date"),
            payload.get("effective_target_date"),
            payload.get("target_date"),
            summary.get("target_complete_trade_date"),
            daily_price.get("target_date"),
            freshness.get("target_date"),
        ):
            text = str(value or "").strip()
            if not text:
                continue
            text = text[:10]
            fallback = fallback or text
            iso_text = _iso_date_text(text)
            if iso_text:
                iso_dates.append(iso_text)
    if iso_dates:
        return max(iso_dates)
    if fallback:
        text = _iso_date_text(fallback)
        if text:
            return text
    return _current_target_date()


def _command_target_dates(*commands: Any) -> set[str]:
    dates: set[str] = set()
    for command in commands:
        parts = str(command or "").replace("\\", "/").split()
        for index, part in enumerate(parts):
            if part == "--target-date" and index + 1 < len(parts):
                date_text = _iso_date_text(parts[index + 1])
                if date_text:
                    dates.add(date_text)
            elif part.startswith("--target-date="):
                date_text = _iso_date_text(part.split("=", 1)[1])
                if date_text:
                    dates.add(date_text)
    return dates


def _action_matches_target(action: dict[str, Any], target_date: str) -> bool:
    if not target_date:
        return True
    text = f"{action.get('command') or ''} {action.get('follow_up_command') or ''}"
    if "YYYY-MM-DD" in text:
        return False
    dates = _command_target_dates(action.get("command"), action.get("follow_up_command"))
    return not dates or target_date in dates


def _list_len(value: Any) -> int:
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, str):
        return len([item for item in value.split(",") if item.strip()])
    return 0


def _has_no_selected_symbols(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    for key in ("analysis_pool_count", "selected_count", "symbol_count", "total_symbols"):
        if key in payload and _int(payload.get(key), default=-1) == 0:
            return True
    for key in ("symbols_checked", "selected_symbols", "processed_symbols", "symbols"):
        if key in payload and _list_len(payload.get(key)) == 0:
            return True
    for key in (
        "freshness",
        "freshness_summary",
        "data_freshness_summary",
        "daily_price_status",
        "data_health_summary",
        "scope",
    ):
        nested = payload.get(key)
        if isinstance(nested, dict) and _has_no_selected_symbols(nested):
            return True
    return False


def _data_gate_payloads(
    data_health: dict[str, Any],
    extended_data_gates: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(extended_data_gates, dict) and extended_data_gates:
        policy = extended_data_gates.get("operator_action_policy")
        queue = (
            (extended_data_gates.get("gate_chain") or {}).get("queue")
            if isinstance(extended_data_gates.get("gate_chain"), dict)
            else {}
        )
        if isinstance(policy, dict):
            payloads.append(policy)
        if isinstance(queue, dict):
            payloads.append(queue)
        payloads.append(extended_data_gates)
    if not isinstance(data_health, dict):
        return payloads
    for key in ("data_health_summary", "key_counts", "extended_data_gates", "extended_data_gate_summary"):
        nested = data_health.get(key)
        if isinstance(nested, dict):
            payloads.append(nested)
    payloads.append(data_health)
    return payloads


def _first_from_payloads(payloads: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", []):
                return value
    return None


def _list_from_payloads(payloads: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
    return []


def _download_queue_gate_action(
    data_health: dict[str, Any],
    extended_data_gates: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    payloads = _data_gate_payloads(data_health, extended_data_gates)
    if not payloads:
        return None
    gate = str(
        _first_from_payloads(
            payloads,
            (
                "extended_data_current_gate",
                "current_gate",
                "extended_import_gate",
                "extended_import_preapply_gate",
                "post_import_gate",
            ),
        )
        or ""
    ).strip()
    gate_next = str(
        _first_from_payloads(
            payloads,
            (
                "extended_data_gate_suggested_next_action",
                "suggested_next_action",
                "extended_data_suggested_next_action",
            ),
        )
        or ""
    ).strip()
    wait_reason = str(_first_from_payloads(payloads, ("extended_data_operator_wait_reason", "operator_wait_reason")) or "").strip()
    queue_status = str(
        _first_from_payloads(payloads, ("queue_status", "extended_data_queue_status", "status")) or ""
    ).strip().lower()
    queue_liveness = str(
        _first_from_payloads(payloads, ("queue_liveness", "extended_data_queue_liveness")) or ""
    ).strip().lower()
    queue_progress = str(
        _first_from_payloads(
            payloads,
            ("queue_progress_state", "extended_data_queue_progress_state", "progress_state"),
        )
        or ""
    ).strip()
    stale_gate = (
        wait_reason in {"download_queue_stale", "download_queue_unknown"}
        or queue_liveness == "stale"
    )
    active_gate = (
        wait_reason == "download_queue_active"
        or queue_liveness == "active"
        or (
            gate == "download_queue"
            and queue_status == "running"
            and queue_liveness in {"unknown", ""}
        )
    )
    if not (active_gate or stale_gate):
        return None
    safe_commands = _list_from_payloads(payloads, ("extended_data_safe_next_commands", "safe_next_commands"))
    blocked_commands = _list_from_payloads(
        payloads,
        ("extended_data_blocked_without_confirmation", "blocked_without_confirmation"),
    )
    command = safe_commands[0] if safe_commands else "python scripts/qd.py tushare-queue-status --summary true"
    reason = (
        "Tushare download queue is stale"
        if stale_gate
        else "Tushare download queue is active"
    )
    if queue_progress:
        reason += f" ({queue_progress})"
    if gate_next:
        reason += f"; gate suggests {gate_next}"
    action = _action(
        warning_kind="download_queue_stale" if stale_gate else "download_queue_active",
        priority="high",
        title="Stale Tushare download queue" if stale_gate else "Active Tushare download queue",
        why=(
            "Queue metadata still says running, but no recent progress was observed. "
            "Treat it as stale until an operator restarts or retires it."
            if stale_gate
            else "The extended-data queue is still writing staging output, so fix-plan must remain read-only."
        ),
        impact=(
            "Staged imports remain blocked until the stale queue is reviewed."
            if stale_gate
            else "Prevents data updates or staged imports from racing an active download."
        ),
        command=command,
        follow_up_command="python scripts/qd.py data-gates --summary true",
        network_required=False,
        safety_note="Read-only monitoring; no token access, network fetch, canonical write, or broker route.",
    )
    action["reason"] = reason
    action["safe_next_commands"] = safe_commands
    action["blocked_without_confirmation"] = blocked_commands
    return action


def _safe_actions_while_download_queue_active(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for item in actions:
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        if item.get("network_required"):
            continue
        if "data-update" in command:
            continue
        if "import-extended-data" in command and "--apply" in command:
            continue
        safe.append(item)
    return safe


def _is_clean_zip_initial_state(project_root: str | Path | None = None) -> bool:
    root = _root(project_root)
    results_dir = root / "results"
    daily_dir = root / "data" / "daily"
    universe_path = root / "data" / "universe" / "low_price_learning_symbols.csv"
    has_daily_files = daily_dir.exists() and any(daily_dir.glob("*.csv"))
    return not results_dir.exists() or (not universe_path.exists() and not has_daily_files)


def build_no_universe_remediation(*, target_date: str = "") -> list[dict[str, Any]]:
    target = _iso_date_text(target_date) or _current_target_date()
    health_command = (
        _command_with_optional_target(
            "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
            target,
        )
        if target
        else "python scripts/qd.py data-health --network false --scope current_analysis_pool"
    )
    action = _action(
        warning_kind="no_universe",
        priority="high",
        title="初始化 demo 股票池",
        why="当前没有可分析股票，无法更新 current_analysis_pool；clean zip 初次打开需要先 bootstrap。",
        impact="没有股票池时直接运行 daily 或 data-update --scope current_analysis_pool 只会空转。",
        command="python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
        follow_up_command=health_command,
        network_required=False,
        safety_note="默认离线初始化本地 demo 股票池，不联网、不接券商、不真实下单、不构成投资建议。",
    )
    action["warning_kind"] = "no_universe"
    action["alternative_commands"] = [
        "python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
        "python scripts/qd.py data-update --symbols 600000,000001 --enable-network",
        "python scripts/build_low_price_universe.py --enable-network",
    ]
    action["offline_note"] = "离线模式只创建或检查本地结构，不会补数。"
    return [action]


def normalize_remediation_command(command: str) -> str:
    text = " ".join(str(command or "").strip().split())
    if text.startswith("python scripts\\qd.py"):
        text = text.replace("python scripts\\qd.py", "python scripts/qd.py", 1)
    return text


def classify_warning_kind(value: Any, *, context: str = "") -> dict[str, str]:
    text = f"{context} {value}".lower()
    if "confirmed_secret" in text or ("api_key" in text and "sk-" in text):
        kind = "schema_confirmed_secret_leak"
    elif "secret_like" in text or "provider_secrets.json" in text or "llm_config.json" in text:
        kind = "schema_secret_like_reference"
    elif "minute" in text or "intraday" in text or "分钟线" in text or "missing_intraday" in text:
        kind = "intraday_missing"
    elif "sample" in text or "样本" in text or "baseline" in text or "not_clearly_above" in text:
        kind = "statistical_insufficient_sample"
    elif "parameter" in text or "robustness" in text or "参数" in text:
        kind = "parameter_sensitivity_warning"
    elif "provider" in text and ("offline" in text or "skipped" in text or "离线" in text):
        kind = "provider_skipped_offline"
    elif "provider" in text or "mootdx" in text or "akshare" in text:
        kind = "provider_unavailable"
    elif "no_universe" in text or "no_symbols_selected" in text or "没有可分析股票" in text:
        kind = "no_universe"
    elif "stale" in text or "过期" in text or "滞后" in text:
        kind = "data_stale"
    elif "missing" in text or "缺失" in text:
        kind = "data_missing"
    else:
        kind = "guardrail_warning"
    return {"warning_kind": kind, **WARNING_KIND_META[kind]}


def _action(
    *,
    warning_kind: str,
    priority: str,
    title: str,
    why: str,
    impact: str,
    command: str,
    follow_up_command: str = "",
    network_required: bool = False,
    safety_note: str = "",
) -> dict[str, Any]:
    meta = WARNING_KIND_META.get(warning_kind, WARNING_KIND_META["guardrail_warning"])
    return {
        "priority": priority,
        "category": meta["category"],
        "severity": meta["severity"],
        "warning_kind": warning_kind,
        "title": title,
        "why": why,
        "impact": impact,
        "command": normalize_remediation_command(command),
        "follow_up_command": normalize_remediation_command(follow_up_command),
        "network_required": bool(network_required),
        "safety_note": safety_note or DISCLAIMER,
    }


def build_data_stale_remediation(payload: dict[str, Any], *, target_date: str = "") -> list[dict[str, Any]]:
    target = target_date or _target_date(payload)
    if _has_no_selected_symbols(payload):
        return build_no_universe_remediation(target_date=target)
    coverage = payload.get("coverage_date") or payload.get("latest_local_daily_date") or payload.get("latest_date_min") or "-"
    latest = payload.get("latest_date_max") or payload.get("latest_local_daily_max_date") or "-"
    stale = _int(payload.get("stale_count") or payload.get("daily_price_stale_count"))
    missing = _int(payload.get("missing_count") or payload.get("daily_price_missing_count"))
    if not (stale or missing or str(payload.get("status") or "").lower() in {"warning", "failed"} or payload.get("is_stale")):
        return []
    return [
        _action(
            warning_kind="data_stale" if stale else "data_missing",
            priority="high",
            title="联网补齐日线" if stale else "联网补齐缺失日线",
            why=f"当前全池覆盖到 {coverage}，最晚已有日线 {latest}，目标交易日 {target}，过期 {stale} 只，缺失 {missing} 只。",
            impact="后续 daily、复盘和验证结果只能作为滞后数据观察。",
            command=_command_with_optional_target(
                "python scripts/qd.py data-update --target-date YYYY-MM-DD --enable-network --scope current_analysis_pool",
                target,
            ),
            follow_up_command=_command_with_optional_target(
                "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
                target,
            ),
            network_required=True,
            safety_note="需要显式联网授权，仅用于更新本地行情数据；离线检查只检查、不会补数；不接券商、不真实下单、不构成投资建议。",
        )
    ]


def build_missing_intraday_remediation(payload: dict[str, Any], *, target_date: str = "") -> list[dict[str, Any]]:
    intraday = payload if isinstance(payload, dict) else {}
    missing = _int(intraday.get("missing_count") or intraday.get("intraday_missing_symbols") or intraday.get("missing_intraday_count"))
    if not missing and not intraday.get("missing_symbols"):
        return []
    target = target_date or _target_date(intraday)
    return [
        _action(
            warning_kind="intraday_missing",
            priority="medium",
            title="缺少分钟线",
            why=f"目标日期 {target} 的部分股票缺少 1m 分钟线。",
            impact="不能可靠验证盘中触发；系统不会用日线 high/low 冒充分钟成交。",
            command="python scripts/qd.py data-health --network false --scope current_analysis_pool",
            follow_up_command=(
                f"导入 data/intraday/1m/{target}/{{symbol}}.csv 后重新运行 data-health"
                if target
                else "导入目标交易日的 data/intraday/1m/{symbol}.csv 后重新运行 data-health"
            ),
            network_required=False,
            safety_note="这是验证提示；缺分钟线不代表日线更新失败，也不构成投资建议。",
        )
    ]


def build_guardrail_remediation(guardrails: dict[str, Any], *, target_date: str = "") -> list[dict[str, Any]]:
    if not isinstance(guardrails, dict):
        return []
    warnings = [str(item) for item in (guardrails.get("warnings") or guardrails.get("top_warnings") or []) if item]
    actions: list[dict[str, Any]] = []
    for warning in warnings[:8]:
        kind = classify_warning_kind(warning, context="guardrail")["warning_kind"]
        if kind == "intraday_missing":
            actions.extend(build_missing_intraday_remediation({"missing_symbols": ["unknown"], "target_date": target_date}))
        elif kind == "statistical_insufficient_sample":
            actions.append(
                _action(
                    warning_kind=kind,
                    priority="medium",
                    title="统计验证样本不足",
                    why=warning,
                    impact="stats-validate 结论有限，只能作为学习观察。",
                    command=_command_with_optional_target(
                        "python scripts/qd.py stats-validate --target-date YYYY-MM-DD --network false",
                        target_date,
                    ),
                    network_required=False,
                    safety_note="统计提示不是收益保证，也不是自动调参建议。",
                )
            )
        elif kind == "parameter_sensitivity_warning":
            actions.append(
                _action(
                    warning_kind=kind,
                    priority="low",
                    title="参数敏感性提示",
                    why=warning,
                    impact="结果稳定性需要谨慎解读；本命令不做参数优化。",
                    command=_command_with_optional_target(
                        "python scripts/qd.py param-sensitivity --target-date YYYY-MM-DD --network false --max-combinations 50 --seed 42",
                        target_date,
                    ),
                    network_required=False,
                    safety_note="仅做稳健性审计，不寻找最佳参数。",
                )
            )
        else:
            actions.append(
                _action(
                    warning_kind="guardrail_warning",
                    priority="low",
                    title="Guardrails 提示",
                    why=warning,
                    impact="这是流程和数据可信度检查，不是策略失败。",
                    command="python scripts/qd.py daily --network false --with-guardrails true",
                    network_required=False,
                )
            )
    return actions


def build_schema_warning_remediation(schema_payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(schema_payload, dict) or not schema_payload:
        return []
    confirmed = _int(schema_payload.get("confirmed_secret_leak_count") or schema_payload.get("secret_leak_count"))
    references = _int(schema_payload.get("secret_like_reference_count"))
    actions: list[dict[str, Any]] = []
    if confirmed:
        actions.append(
            _action(
                warning_kind="schema_confirmed_secret_leak",
                priority="critical",
                title="高风险密钥泄露",
                why=f"schema-check 发现 {confirmed} 个 confirmed_secret_leak。",
                impact="必须移除真实密钥并重新生成相关结果；不要把密钥提交或打包。",
                command="python scripts/qd.py schema-check --results-dir results --network false",
                network_required=False,
                safety_note="不会打印完整密钥；请人工检查报告中的 masked_preview。",
            )
        )
    elif references or str(schema_payload.get("status") or "").lower() == "warning":
        actions.append(
            _action(
                warning_kind="schema_secret_like_reference",
                priority="low",
                title="低风险敏感词引用",
                why=f"schema-check 有 {references} 个 secret_like_reference，高风险密钥泄露为 0。",
                impact="通常是 provider_secrets.json 路径名、示例字段或 masked value，不阻断日常流程。",
                command="python scripts/qd.py schema-check --results-dir results --network false",
                network_required=False,
                safety_note="若高风险为 0，通常无需处理；需要时查看 schema-check 报告。",
            )
        )
    return actions


def build_provider_remediation(provider_payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(provider_payload, dict) or not provider_payload:
        return []
    failed = _int(provider_payload.get("failed_count"))
    offline = _int(provider_payload.get("offline_skipped_count"))
    actions: list[dict[str, Any]] = []
    if failed:
        actions.append(
            _action(
                warning_kind="provider_unavailable",
                priority="medium",
                title="Provider 不可用",
                why=f"provider 失败数量为 {failed}。",
                impact="联网更新可能降级到其他 provider；不会造数据。",
                command="python scripts/qd.py providers --network false",
                network_required=False,
            )
        )
    elif offline:
        actions.append(
            _action(
                warning_kind="provider_skipped_offline",
                priority="low",
                title="离线跳过 provider",
                why=f"当前默认离线，跳过 {offline} 个联网 provider。",
                impact="这是默认安全行为，不是失败。",
                command="python scripts/qd.py providers --network false",
                network_required=False,
            )
        )
    return actions


def build_daily_remediation(daily_payload: dict[str, Any], *, target_date: str = "") -> list[dict[str, Any]]:
    if not isinstance(daily_payload, dict) or not daily_payload:
        return [
            _action(
                warning_kind="artifact_missing",
                priority="medium",
                title="每日流程尚未生成",
                why="未找到 results/daily_workflow_latest.json。",
                impact="status 和产物中心无法展示 latest daily 报告。",
                command="python scripts/qd.py daily --network false --with-guardrails true",
                network_required=False,
            )
        ]
    target = target_date or _target_date(daily_payload)
    actions: list[dict[str, Any]] = []
    daily_price = daily_payload.get("daily_price_status") if isinstance(daily_payload.get("daily_price_status"), dict) else {}
    summary = daily_payload.get("summary") if isinstance(daily_payload.get("summary"), dict) else {}
    stale_payload = {
        **daily_price,
        "daily_price_stale_count": summary.get("daily_price_stale_count"),
        "daily_price_missing_count": summary.get("daily_price_missing_count"),
    }
    actions.extend(build_data_stale_remediation(stale_payload, target_date=target))
    intraday = daily_payload.get("intraday_status") if isinstance(daily_payload.get("intraday_status"), dict) else {}
    actions.extend(build_missing_intraday_remediation(intraday, target_date=target))
    guardrails = daily_payload.get("guardrail_summary") if isinstance(daily_payload.get("guardrail_summary"), dict) else {}
    actions.extend(build_guardrail_remediation(guardrails, target_date=target))
    return actions


def rank_remediation_actions(actions: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    severity_order = {"critical": 0, "high": 1, "warning": 2, "info": 3}
    semantic_singletons = {
        "data_stale",
        "data_missing",
        "intraday_missing",
        "provider_skipped_offline",
        "provider_unavailable",
        "schema_secret_like_reference",
        "schema_confirmed_secret_leak",
        "statistical_insufficient_sample",
        "parameter_sensitivity_warning",
        "sector_mapping_missing",
        "no_universe",
        "no_symbols_selected",
    }
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in actions:
        warning_kind = str(item.get("warning_kind") or "")
        command_key = "" if warning_kind in semantic_singletons else str(item.get("command") or "")
        key = (warning_kind, command_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped.sort(key=lambda row: (priority_order.get(str(row.get("priority")), 9), severity_order.get(str(row.get("severity")), 9)))
    return deduped[:limit]


def load_remediation_inputs(*, project_root: str | Path | None = None) -> dict[str, Any]:
    return {
        "data_update": _load("results/data_update/data_update_latest.json", project_root),
        "data_health": _load("results/data_health/data_health_dashboard_latest.json", project_root),
        "extended_data_gates": _load("results/data_health/extended_data_gates_latest.json", project_root),
        "daily": _load("results/daily_workflow_latest.json", project_root),
        "market_intel": _load("results/market_intelligence/market_intelligence_latest.json", project_root),
        "schema": _load("results/schema_validation_latest.json", project_root),
        "command_center": _load("results/command_center_status_latest.json", project_root),
    }


def build_remediation_plan(
    *,
    data_update: dict[str, Any] | None = None,
    data_health: dict[str, Any] | None = None,
    extended_data_gates: dict[str, Any] | None = None,
    daily_payload: dict[str, Any] | None = None,
    schema_payload: dict[str, Any] | None = None,
    provider_payload: dict[str, Any] | None = None,
    market_intel_payload: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    if all(value is None for value in (data_update, data_health, extended_data_gates, daily_payload, schema_payload, provider_payload, market_intel_payload)):
        loaded = load_remediation_inputs(project_root=project_root)
        data_update = loaded.get("data_update")
        data_health = loaded.get("data_health")
        extended_data_gates = loaded.get("extended_data_gates")
        daily_payload = loaded.get("daily")
        market_intel_payload = loaded.get("market_intel")
        schema_payload = loaded.get("schema")
    actions: list[dict[str, Any]] = []
    primary_payloads = [
        value
        for value in (data_update, data_health, daily_payload)
        if isinstance(value, dict) and value
    ]
    current_target = _target_date(*primary_payloads) if primary_payloads else ""
    if _is_clean_zip_initial_state(project_root) and not any(
        isinstance(value, dict) and value
        for value in (data_update, data_health, daily_payload, schema_payload, provider_payload, market_intel_payload)
    ):
        actions.extend(build_no_universe_remediation())
    if isinstance(data_update, dict) and data_update:
        actions.extend(build_data_stale_remediation(data_update, target_date=current_target or _target_date(data_update)))
    if isinstance(data_health, dict) and data_health:
        freshness = data_health.get("freshness") if isinstance(data_health.get("freshness"), dict) else {}
        intraday = data_health.get("intraday") if isinstance(data_health.get("intraday"), dict) else {}
        provider = data_health.get("provider_failure_stats") if isinstance(data_health.get("provider_failure_stats"), dict) else {}
        target = current_target or _target_date(data_health, freshness)
        actions.extend(build_data_stale_remediation(freshness, target_date=target))
        actions.extend(build_missing_intraday_remediation(intraday, target_date=target))
        actions.extend(build_provider_remediation(provider))
    if isinstance(daily_payload, dict):
        actions.extend(build_daily_remediation(daily_payload, target_date=current_target))
    if isinstance(schema_payload, dict):
        actions.extend(build_schema_warning_remediation(schema_payload))
    if isinstance(provider_payload, dict):
        actions.extend(build_provider_remediation(provider_payload))
    if isinstance(market_intel_payload, dict) and market_intel_payload:
        market_actions = market_intel_payload.get("remediation_actions") if isinstance(market_intel_payload.get("remediation_actions"), list) else []
        market_target = _target_date(market_intel_payload)
        effective_target = current_target or market_target
        if current_target and market_target and current_target != market_target:
            actions.append(
                _action(
                    warning_kind="target_date_mismatch",
                    priority="medium",
                    title="刷新 market-intel 到当前目标日",
                    why=f"market-intel 最新产物目标日为 {market_target}，当前数据目标日为 {current_target}。",
                    impact="旧目标日的分钟线、题材映射或候选解释修复建议不应混入当前修复计划。",
                    command=f"python scripts/qd.py market-intel --target-date {current_target} --network false --scope current_analysis_pool",
                    network_required=False,
                    safety_note="仅刷新本地研究产物，不联网、不接券商、不构成投资建议。",
                )
            )
        actions.extend(
            [
                item
                for item in market_actions
                if isinstance(item, dict) and _action_matches_target(item, effective_target)
            ]
        )
    plan_target = current_target
    if not plan_target and isinstance(market_intel_payload, dict) and market_intel_payload:
        plan_target = _target_date(market_intel_payload)
    if not _iso_date_text(plan_target):
        plan_target = ""
    gate_action = _download_queue_gate_action(
        data_health if isinstance(data_health, dict) else {},
        extended_data_gates if isinstance(extended_data_gates, dict) else {},
    )
    if gate_action:
        safe_actions = _safe_actions_while_download_queue_active(actions)
        ranked = [gate_action, *rank_remediation_actions(safe_actions, limit=max(0, limit - 1))][:limit]
    else:
        ranked = rank_remediation_actions(actions, limit=limit)
    status = "passed"
    if any(row.get("severity") == "critical" for row in ranked):
        status = "failed"
    elif ranked:
        status = "warning"
    return {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "status": status,
        "ok": True,
        "generated_at": now_text(),
        "target_date": plan_target,
        "problem_count": len(ranked),
        "actions": ranked,
        "suggested_next_action": ranked[0].get("command") if ranked else "",
        "network": False,
        "llm_enabled": False,
        "disclaimer": DISCLAIMER,
    }


def format_remediation_console(plan: dict[str, Any], *, max_actions: int = 5, title: str = "QuantDesk 修复计划") -> str:
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    lines = [title, ""]
    if not actions:
        lines.extend(["当前没有必须立即处理的问题。", "可以运行：python scripts/qd.py app", "", DISCLAIMER])
        return "\n".join(lines)

    visible = actions[:max_actions]
    offline = [action for action in visible if not action.get("network_required")]
    online = [action for action in visible if action.get("network_required")]

    def append_group(group_title: str, rows: list[dict[str, Any]], note: str, *, start_index: int) -> int:
        if not rows:
            return start_index
        lines.append(f"{group_title}：")
        index = start_index
        for action in rows:
            lines.append(f"{index}. {action.get('title') or '修复建议'}")
            lines.append(f"   {action.get('command') or '-'}")
            if action.get("follow_up_command"):
                lines.append(f"   然后：{action.get('follow_up_command')}")
            impact = str(action.get("impact") or "").strip()
            if impact:
                lines.append(f"   影响：{impact}")
            index += 1
        lines.extend([f"说明：{note}", ""])
        return index

    next_index = append_group("离线可运行", offline, "离线命令不会联网。", start_index=1)
    append_group("需要联网授权", online, "联网命令只在你显式运行时执行。", start_index=next_index)
    lines.extend(["以上是流程修复建议，不构成投资建议。", DISCLAIMER])
    return "\n".join(lines)


def write_fix_plan_latest(plan: dict[str, Any], path: str | Path = FIX_PLAN_PATH) -> Path:
    return write_json(path, plan)
