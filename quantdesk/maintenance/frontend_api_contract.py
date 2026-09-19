from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import write_json, write_md
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "maintenance" / "frontend_api_contract_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "maintenance" / "frontend_api_contract_latest.md"
FRONTEND_SOURCE_PATTERNS = (
    "app/static/js/**/*.js",
    "app/templates/**/*.html",
)
SERVER_SOURCE_PATTERNS = (
    "scripts/quant_api_server.py",
    "scripts/server/routes/*.py",
)
IGNORED_FRONTEND_FILENAMES = {"echarts.min.js"}
DISCLAIMER = (
    "Read-only frontend API contract inventory. It statically compares local app API references "
    "with the local QuantDesk server routes; it does not start the server, enable network providers, "
    "connect brokers, place real orders, or change strategy/backtest behavior."
)
AUDITED_UNREFERENCED_SERVER_ROUTES: dict[tuple[str, str], tuple[str, str, str]] = {
    ("POST", "/api/daily/update-candidates"): (
        "compatibility_alias", "keep_as_explicit_pipeline_alias",
        "The legacy candidate update address invokes the shared full-A Pipeline only after explicit network authorization.",
    ),
    ("POST", "/api/data/repair-daily"): (
        "retired_endpoint", "keep_gone_response_for_legacy_clients",
        "The removed repair writer returns HTTP 410 without data writes; clients use the shared Pipeline entrypoint.",
    ),
    (
        "GET",
        "/api/ai-trader/boundary",
    ): (
        "ai_trader_boundary_read_endpoint",
        "keep_as_test_and_diagnostic_consumed",
        "The AI trader boundary endpoint is consumed by the server test suite (tests/test_ai_trader_server_8000.py) and direct API diagnostics; the active UI reads boundary fields from the consolidated /api/ai-trader/dashboard payload instead.",
    ),
    (
        "GET",
        "/api/launcher/build-id",
    ): (
        "launcher_build_probe",
        "keep_as_launcher_consumed_probe",
        "The desktop launcher (scripts/launcher.py) polls this GET route over HTTP to detect a stale running server before restart; it is intentionally not called from the browser UI.",
    ),
    (
        "GET",
        "/api/daily/quant-summary",
    ): (
        "daily_research_summary_read_endpoint",
        "keep_as_direct_daily_research_summary",
        "The daily research UI uses the full overview payload; this compact summary route is retained for direct local diagnostics and scripted review.",
    ),
    (
        "GET",
        "/api/benchmark/series",
    ): (
        "decision_board_component_read_endpoint",
        "keep_as_direct_benchmark_diagnostic",
        "The active UI consumes backend-aligned benchmark returns from /api/decision-board; this raw local benchmark series remains a read-only diagnostic component and compatibility endpoint.",
    ),
    (
        "GET",
        "/api/paper-simulation/equity-history",
    ): (
        "decision_board_component_read_endpoint",
        "keep_as_direct_paper_history_diagnostic",
        "The active UI consumes the authoritative aligned series from /api/decision-board; this local paper-equity history remains a read-only component and compatibility endpoint.",
    ),
    (
        "GET",
        "/api/paper-simulation/watchlist",
    ): (
        "decision_board_component_read_endpoint",
        "keep_as_direct_watchlist_diagnostic",
        "The active UI consumes the consolidated /api/decision-board payload; this local paper watchlist remains a read-only component and compatibility endpoint.",
    ),
    (
        "GET",
        "/api/stock-detail",
    ): (
        "compatibility_alias",
        "keep_until_alias_users_are_migrated",
        "The current UI opens symbol details through /api/stock/<symbol>/detail; this query-string form remains a local compatibility alias.",
    ),
    (
        "POST",
        "/api/theme-map/generate-import-draft",
    ): (
        "compatibility_alias",
        "keep_as_theme_map_import_draft_alias",
        "The current UI posts to /api/theme-map/import-draft; this older explicit action name is retained for scripted local review.",
    ),
    (
        "POST",
        "/api/theme-map/update",
    ): (
        "compatibility_alias",
        "keep_as_theme_map_refresh_alias",
        "The current UI posts to /api/theme-map/refresh; this older action name is retained for scripted local review.",
    ),
    (
        "POST",
        "/api/news/refresh",
    ): (
        "operator_gated_refresh",
        "keep_as_explicit_local_news_refresh",
        "This route is an explicit local operator action for refreshing cached news status; it is intentionally not invoked by the current browser UI and remains non-default/network-gated.",
    ),
    (
        "GET",
        "/api/a-stock-data-providers/latest",
    ): (
        "compatibility_alias",
        "keep_as_provider_status_alias",
        "Current UI calls /api/a-stock-data-providers/status; /latest is retained for historical direct links and schema readers.",
    ),
    (
        "GET",
        "/api/workbench/next-actions",
    ): (
        "legacy_workbench_read_endpoint",
        "keep_for_legacy_workbench_and_direct_status_review",
        "The current daily research app no longer renders the old command center, but the legacy workbench keeps this direct read endpoint.",
    ),
    (
        "GET",
        "/api/conditional-trade-plans/preview",
    ): (
        "paper_research_read_endpoint",
        "keep_as_direct_paper_research_preview",
        "Conditional plan previews are paper/research-only diagnostics and may be opened from docs or direct API checks without a current home-page link.",
    ),
    (
        "POST",
        "/api/auto-paper-portfolio/update",
    ): (
        "paper_account_operator_api",
        "keep_as_legacy_update_alias",
        "The current UI updates paper portfolios through /run; this historical alias is kept for scripted paper-account review and does not route broker orders.",
    ),
    (
        "POST",
        "/api/auto-paper-portfolio/update-from-plan",
    ): (
        "paper_account_operator_api",
        "keep_as_explicit_plan_update_api",
        "This paper-account endpoint replays local plans into the virtual portfolio; it is API-only and never connects to a broker or places orders.",
    ),
    (
        "POST",
        "/api/workbench/data-update/run",
    ): (
        "operator_gated_api",
        "keep_as_explicit_operator_action",
        "The scanned UI intentionally avoids a one-click data-update runner; this direct endpoint remains guarded and requires an explicit operator request body.",
    ),
    (
        "POST",
        "/api/fundamental-health/check",
    ): (
        "research_diagnostic_api",
        "keep_as_direct_research_check",
        "Fundamental health checks are local research diagnostics covered by API schema/tests, not a primary daily-home interaction.",
    ),
    (
        "POST",
        "/api/research/candidate-replay",
    ): (
        "research_diagnostic_api",
        "keep_as_direct_replay_check",
        "Candidate replay is an explicit research validation endpoint and must stay outside automatic UI execution.",
    ),
    (
        "POST",
        "/api/research/strategy-replay",
    ): (
        "research_diagnostic_api",
        "keep_as_direct_replay_check",
        "Strategy replay is an explicit research validation endpoint and must stay outside automatic UI execution.",
    ),
    (
        "POST",
        "/api/watchlist/update",
    ): (
        "paper_watchlist_api",
        "keep_as_direct_watchlist_edit_api",
        "The current UI exposes add/remove; this update endpoint supports explicit local watchlist field edits for scripts and future forms.",
    ),
}

API_LITERAL_RE = re.compile(r"""(?P<quote>["'`])(?P<url>/api/[^"'`<>\s]+)(?P=quote)""")
DIRECT_CALL_RE = re.compile(
    r"""(?P<kind>requestJson|fetch|window\.open)\s*\(\s*(?P<quote>["'`])(?P<url>/api/[^"'`<>\s]+)(?P=quote)(?P<tail>[\s\S]{0,420})""",
    re.MULTILINE,
)
METHOD_RE = re.compile(r"""method\s*:\s*["'](?P<method>[A-Za-z]+)["']""")


@dataclass(frozen=True)
class FrontendApiUse:
    path: str
    method: str
    source: str
    line: int
    call_kind: str
    raw_url: str


@dataclass(frozen=True)
class ServerApiRoute:
    path: str
    method: str
    route_type: str
    source: str
    line: int
    pattern: str


@dataclass(frozen=True)
class ServerRouteReview:
    path: str
    method: str
    source: str
    line: int
    usage_class: str
    recommendation: str
    keep_reason: str
    action_required: bool


def _rel(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def discover_frontend_sources(project_root: Path = PROJECT_ROOT) -> list[Path]:
    root = Path(project_root)
    sources: dict[str, Path] = {}
    for pattern in FRONTEND_SOURCE_PATTERNS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            if path.name in IGNORED_FRONTEND_FILENAMES or path.name.endswith(".min.js"):
                continue
            sources[_rel(path, root=root)] = path
    return [sources[key] for key in sorted(sources)]


def discover_server_sources(project_root: Path = PROJECT_ROOT) -> list[Path]:
    root = Path(project_root)
    sources: dict[str, Path] = {}
    for pattern in SERVER_SOURCE_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file():
                sources[_rel(path, root=root)] = path
    return [sources[key] for key in sorted(sources)]


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def normalize_api_path(url: str) -> str:
    text = str(url or "").strip()
    if not text.startswith("/api/"):
        return ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    text = text.rstrip("/")
    return text or "/api"


def _has_dynamic_path_segment(url: str) -> bool:
    path_part = str(url or "").split("#", 1)[0].split("?", 1)[0]
    return "${" in path_part


def _method_from_direct_call(kind: str, tail: str) -> str:
    if kind == "window.open":
        return "GET"
    truncated = tail.split(");", 1)[0]
    match = METHOD_RE.search(truncated)
    if match:
        return match.group("method").upper()
    return "GET"


def extract_frontend_api_uses(frontend_path: str | Path) -> list[FrontendApiUse]:
    path = Path(frontend_path)
    text = _read_text(path)
    if not text:
        return []
    uses: list[FrontendApiUse] = []
    seen_call_spans: set[tuple[int, int]] = set()
    for match in DIRECT_CALL_RE.finditer(text):
        raw_url = match.group("url")
        api_path = normalize_api_path(raw_url)
        if not api_path:
            continue
        seen_call_spans.add((match.start("url"), match.end("url")))
        uses.append(
            FrontendApiUse(
                path=api_path,
                method=_method_from_direct_call(match.group("kind"), match.group("tail")),
                source=_rel(path),
                line=_line_number(text, match.start("url")),
                call_kind=match.group("kind"),
                raw_url=raw_url,
            )
        )
    for match in API_LITERAL_RE.finditer(text):
        span = (match.start("url"), match.end("url"))
        if span in seen_call_spans:
            continue
        raw_url = match.group("url")
        api_path = normalize_api_path(raw_url)
        if not api_path:
            continue
        uses.append(
            FrontendApiUse(
                path=api_path,
                method="UNKNOWN",
                source=_rel(path),
                line=_line_number(text, match.start("url")),
                call_kind="literal",
                raw_url=raw_url,
            )
        )
    deduped: dict[tuple[str, str, int, str], FrontendApiUse] = {}
    for use in uses:
        deduped[(use.path, use.method, use.line, use.call_kind)] = use
    return sorted(deduped.values(), key=lambda item: (item.path, item.method, item.line, item.call_kind))


def _api_strings_from_node(node: ast.AST) -> list[str]:
    values: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/api/"):
        values.append(normalize_api_path(node.value))
    elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        for child in node.elts:
            values.extend(_api_strings_from_node(child))
    return [value for value in values if value]


def _is_path_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "path"


def _extract_compare_routes(node: ast.Compare) -> list[str]:
    routes: list[str] = []
    if _is_path_name(node.left):
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq):
                routes.extend(_api_strings_from_node(comparator))
            elif isinstance(op, ast.In):
                routes.extend(_api_strings_from_node(comparator))
    for op, comparator in zip(node.ops, node.comparators):
        if isinstance(op, ast.Eq) and _is_path_name(comparator):
            routes.extend(_api_strings_from_node(node.left))
    return sorted(set(routes))


def _extract_prefix_route(node: ast.Call) -> str:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "startswith":
        return ""
    if not _is_path_name(node.func.value) or not node.args:
        return ""
    routes = _api_strings_from_node(node.args[0])
    return routes[0] if routes else ""


def _method_from_function_name(name: str) -> str:
    if name == "do_GET":
        return "GET"
    if name == "do_POST":
        return "POST"
    return "UNKNOWN"


def _methods_from_flask_route(node: ast.Call) -> list[str]:
    methods = ["GET"]
    for keyword in node.keywords:
        if keyword.arg != "methods":
            continue
        values = _api_strings_from_node(keyword.value)
        if values:
            return values
        if isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
            methods = [
                str(item.value).upper()
                for item in keyword.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ] or methods
        elif isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            methods = [keyword.value.value.upper()]
    return sorted(set(methods))


def _route_type_and_path(route: str) -> tuple[str, str, str]:
    normalized = normalize_api_path(route)
    if not normalized:
        return "", "", ""
    if "<" not in normalized and "{" not in normalized:
        return "exact", normalized, normalized
    prefix = re.split(r"[<{]", normalized, maxsplit=1)[0].rstrip("/")
    if prefix.startswith("/api/"):
        return "prefix", prefix, f"{normalized}*"
    return "pattern", normalized, normalized


def _extract_flask_route_decorator(node: ast.expr) -> list[tuple[str, str, str, list[str]]]:
    if not isinstance(node, ast.Call):
        return []
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "route":
        return []
    if not node.args:
        return []
    if not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return []
    route_type, path, pattern = _route_type_and_path(node.args[0].value)
    if not path:
        return []
    return [(path, route_type, pattern, _methods_from_flask_route(node))]


def extract_server_api_routes(server_path: str | Path) -> list[ServerApiRoute]:
    path = Path(server_path)
    text = _read_text(path)
    if not text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    routes: list[ServerApiRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            for route, route_type, pattern, methods in _extract_flask_route_decorator(decorator):
                for method in methods:
                    routes.append(
                        ServerApiRoute(
                            path=route,
                            method=method,
                            route_type=route_type,
                            source=_rel(path),
                            line=getattr(decorator, "lineno", getattr(node, "lineno", 0)),
                            pattern=pattern,
                        )
                    )
        method = _method_from_function_name(node.name)
        if method == "UNKNOWN":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Compare):
                for route in _extract_compare_routes(child):
                    routes.append(
                        ServerApiRoute(
                            path=route,
                            method=method,
                            route_type="exact",
                            source=_rel(path),
                            line=getattr(child, "lineno", 0),
                            pattern=route,
                        )
                    )
            elif isinstance(child, ast.Call):
                prefix = _extract_prefix_route(child)
                if prefix:
                    routes.append(
                        ServerApiRoute(
                            path=prefix,
                            method=method,
                            route_type="prefix",
                            source=_rel(path),
                            line=getattr(child, "lineno", 0),
                            pattern=f"{prefix}*",
                        )
                    )
    deduped: dict[tuple[str, str, str], ServerApiRoute] = {}
    for route in routes:
        deduped[(route.path, route.method, route.route_type)] = route
    return sorted(deduped.values(), key=lambda item: (item.path, item.method, item.route_type))


def _covered_by_route(
    frontend_path: str,
    method: str,
    server_exact: dict[str, set[str]],
    server_prefix: dict[str, set[str]],
    *,
    check_method: bool,
) -> bool:
    if frontend_path in server_exact:
        return (not check_method) or method in server_exact[frontend_path]
    for prefix, methods in server_prefix.items():
        if frontend_path.startswith(prefix):
            return (not check_method) or method in methods
    return False


def _endpoint_method_map(uses: list[FrontendApiUse]) -> dict[str, list[str]]:
    method_map: dict[str, set[str]] = defaultdict(set)
    for use in uses:
        method_map[use.path].add(use.method)
    return {path: sorted(methods) for path, methods in sorted(method_map.items())}


def _route_review_class(route: ServerApiRoute) -> tuple[str, str, str, bool]:
    path = route.path
    method = route.method
    audited = AUDITED_UNREFERENCED_SERVER_ROUTES.get((method, path))
    if audited:
        usage_class, recommendation, keep_reason = audited
        return (usage_class, recommendation, keep_reason, False)
    if path == "/api/health":
        return (
            "operational_probe",
            "keep",
            "Health/readiness probes are used by launchers, smoke checks, and external diagnostics, not necessarily by the UI.",
            False,
        )
    if path in {"/api/stock_detail", "/api/stock-detail"}:
        return (
            "legacy_alias",
            "keep_until_alias_users_are_migrated",
            "Legacy spelling is retained as a compatibility alias while the canonical frontend path is /api/stock-detail.",
            False,
        )
    if path.endswith("/status") or path in {"/api/market-clock", "/api/market-regime", "/api/command-center/status"}:
        return (
            "diagnostic_status_endpoint",
            "review_for_ui_link_or_keep_as_diagnostic",
            "Status routes may be consumed by launchers, operator diagnostics, or direct API checks even when not linked from a page.",
            False,
        )
    if path.endswith("/artifacts") or "/report/" in path or path.endswith("/weekly") or path.endswith("/summary"):
        return (
            "artifact_or_report_endpoint",
            "review_for_artifact_center_or_keep_as_direct_link",
            "Artifact/report routes are often opened from generated HTML, docs, or direct links rather than active JavaScript.",
            False,
        )
    if method == "POST":
        return (
            "server_action_without_current_ui_call",
            "add_ui_control_or_archive_after_reference_audit",
            "POST routes can mutate local research/paper state and should either have an intentional UI/operator entry or be archived.",
            True,
        )
    return (
        "read_endpoint_without_current_ui_call",
        "add_ui_link_or_archive_after_reference_audit",
        "Read routes not referenced by the scanned UI should be reviewed before keeping or deleting.",
        True,
    )


def review_unreferenced_server_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for item in routes:
        route = ServerApiRoute(
            path=str(item.get("path") or ""),
            method=str(item.get("method") or "UNKNOWN"),
            route_type=str(item.get("route_type") or "exact"),
            source=str(item.get("source") or ""),
            line=int(item.get("line") or 0),
            pattern=str(item.get("pattern") or item.get("path") or ""),
        )
        usage_class, recommendation, keep_reason, action_required = _route_review_class(route)
        reviews.append(
            asdict(
                ServerRouteReview(
                    path=route.path,
                    method=route.method,
                    source=route.source,
                    line=route.line,
                    usage_class=usage_class,
                    recommendation=recommendation,
                    keep_reason=keep_reason,
                    action_required=action_required,
                )
            )
        )
    return reviews


def review_dynamic_frontend_apis(
    frontend_uses: list[FrontendApiUse],
    server_exact: dict[str, set[str]],
    server_prefix: dict[str, set[str]],
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for use in frontend_uses:
        if not _has_dynamic_path_segment(use.raw_url):
            continue
        normalized = normalize_api_path(use.raw_url)
        covered = _covered_by_route(normalized, use.method, server_exact, server_prefix, check_method=use.method != "UNKNOWN")
        reviews.append(
            {
                "raw_url": use.raw_url,
                "normalized_path": normalized,
                "method": use.method,
                "source": use.source,
                "line": use.line,
                "coverage_status": "covered_by_server_prefix" if covered else "manual_review_required",
                "action_required": not covered,
            }
        )
    return sorted(reviews, key=lambda item: (item["action_required"], item["source"], item["line"], item["raw_url"]))


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(item.get(key) or "unknown") for item in items).items()))


def build_frontend_api_contract(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    project_root = Path(project_root)
    frontend_sources = discover_frontend_sources(project_root)
    server_sources = discover_server_sources(project_root)
    frontend_uses = [use for source in frontend_sources for use in extract_frontend_api_uses(source)]
    server_routes = [route for source in server_sources for route in extract_server_api_routes(source)]
    frontend_paths = sorted({use.path for use in frontend_uses if not _has_dynamic_path_segment(use.raw_url)})
    frontend_dynamic = sorted({use.raw_url for use in frontend_uses if _has_dynamic_path_segment(use.raw_url)})
    server_exact: dict[str, set[str]] = defaultdict(set)
    server_prefix: dict[str, set[str]] = defaultdict(set)
    for route in server_routes:
        target = server_prefix if route.route_type == "prefix" else server_exact
        target[route.path].add(route.method)
    missing_on_server = [
        path
        for path in frontend_paths
        if not _covered_by_route(path, "UNKNOWN", server_exact, server_prefix, check_method=False)
    ]
    known_method_uses = [
        use
        for use in frontend_uses
        if use.method != "UNKNOWN" and not _has_dynamic_path_segment(use.raw_url)
    ]
    method_gaps = [
        asdict(use)
        for use in known_method_uses
        if not _covered_by_route(use.path, use.method, server_exact, server_prefix, check_method=True)
    ]
    frontend_method_map = _endpoint_method_map(frontend_uses)
    server_method_map = {
        path: sorted(methods)
        for path, methods in sorted(server_exact.items())
    }
    server_prefix_routes = {
        path: sorted(methods)
        for path, methods in sorted(server_prefix.items())
    }
    unreferenced_server_routes = [
        asdict(route)
        for route in server_routes
        if route.route_type == "exact" and route.path not in frontend_paths
    ]
    dynamic_reviews = review_dynamic_frontend_apis(frontend_uses, server_exact, server_prefix)
    dynamic_action_required = [item for item in dynamic_reviews if item.get("action_required")]
    unreferenced_reviews = review_unreferenced_server_routes(unreferenced_server_routes)
    unreferenced_action_required = [item for item in unreferenced_reviews if item.get("action_required")]
    warnings: list[str] = []
    notes: list[str] = []
    if missing_on_server:
        warnings.append("frontend_endpoint_missing_on_server")
    if method_gaps:
        warnings.append("frontend_method_missing_on_server")
    if dynamic_action_required:
        warnings.append("frontend_dynamic_api_literals_need_manual_review")
    if unreferenced_action_required:
        warnings.append("server_unreferenced_routes_need_ui_or_archive_review")
    elif unreferenced_server_routes:
        notes.append("server_routes_not_referenced_by_frontend_classified")
    if not frontend_uses:
        warnings.append("no_frontend_api_uses_detected")
    if not server_routes:
        warnings.append("no_server_api_routes_detected")
    payload: dict[str, Any] = {
        "ok": bool(frontend_uses and server_routes),
        "status": "warning" if warnings else "passed",
        "task_type": "frontend_api_contract",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "frontend_source": _rel(frontend_sources[0], root=project_root) if frontend_sources else "",
        "frontend_sources": [_rel(path, root=project_root) for path in frontend_sources],
        "frontend_source_count": len(frontend_sources),
        "server_source": _rel(server_sources[0], root=project_root) if server_sources else "",
        "server_sources": [_rel(path, root=project_root) for path in server_sources],
        "server_source_count": len(server_sources),
        "frontend_use_count": len(frontend_uses),
        "frontend_endpoint_count": len(frontend_method_map),
        "server_route_count": len(server_routes),
        "server_exact_route_count": len(server_exact),
        "server_prefix_route_count": len(server_prefix),
        "frontend_methods": dict(sorted(Counter(use.method for use in frontend_uses).items())),
        "server_methods": dict(sorted(Counter(route.method for route in server_routes).items())),
        "frontend_endpoint_methods": frontend_method_map,
        "server_endpoint_methods": server_method_map,
        "server_prefix_routes": server_prefix_routes,
        "frontend_missing_on_server": missing_on_server,
        "frontend_missing_on_server_count": len(missing_on_server),
        "frontend_method_gaps": method_gaps,
        "frontend_method_gap_count": len(method_gaps),
        "frontend_dynamic_api_literals": frontend_dynamic,
        "frontend_dynamic_api_literal_count": len(frontend_dynamic),
        "frontend_dynamic_api_reviews": dynamic_reviews,
        "frontend_dynamic_api_action_required_count": len(dynamic_action_required),
        "server_unreferenced_by_frontend": unreferenced_server_routes[:120],
        "server_unreferenced_by_frontend_count": len(unreferenced_server_routes),
        "server_unreferenced_route_reviews": unreferenced_reviews[:120],
        "server_unreferenced_review_count": len(unreferenced_reviews),
        "server_unreferenced_action_required_count": len(unreferenced_action_required),
        "server_unreferenced_usage_class_counts": _count_by(unreferenced_reviews, "usage_class"),
        "server_unreferenced_recommendation_counts": _count_by(unreferenced_reviews, "recommendation"),
        "frontend_uses": [asdict(use) for use in frontend_uses],
        "server_routes": [asdict(route) for route in server_routes],
        "warnings": warnings,
        "warning_count": len(warnings),
        "notes": notes,
        "note_count": len(notes),
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return payload


def render_frontend_api_contract_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Frontend API Contract",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- frontend_source_count: `{payload.get('frontend_source_count')}`",
        f"- frontend_endpoint_count: `{payload.get('frontend_endpoint_count')}`",
        f"- frontend_use_count: `{payload.get('frontend_use_count')}`",
        f"- server_source_count: `{payload.get('server_source_count')}`",
        f"- server_exact_route_count: `{payload.get('server_exact_route_count')}`",
        f"- server_prefix_route_count: `{payload.get('server_prefix_route_count')}`",
        f"- frontend_missing_on_server_count: `{payload.get('frontend_missing_on_server_count')}`",
        f"- frontend_method_gap_count: `{payload.get('frontend_method_gap_count')}`",
        f"- frontend_dynamic_api_action_required_count: `{payload.get('frontend_dynamic_api_action_required_count')}`",
        f"- server_unreferenced_by_frontend_count: `{payload.get('server_unreferenced_by_frontend_count')}`",
        f"- server_unreferenced_action_required_count: `{payload.get('server_unreferenced_action_required_count')}`",
        f"- note_count: `{payload.get('note_count')}`",
        "",
        "## Frontend Sources",
        "",
    ]
    lines.extend([f"- `{item}`" for item in payload.get("frontend_sources") or []] or ["- none"])
    lines.extend(
        [
            "",
            "## Server Sources",
            "",
        ]
    )
    lines.extend([f"- `{item}`" for item in payload.get("server_sources") or []] or ["- none"])
    lines.extend([
        "",
        "## Frontend Missing On Server",
        "",
    ])
    missing = payload.get("frontend_missing_on_server") or []
    lines.extend([f"- `{item}`" for item in missing] if missing else ["- none"])
    lines.extend(["", "## Method Gaps", ""])
    gaps = payload.get("frontend_method_gaps") or []
    if gaps:
        for item in gaps[:80]:
            lines.append(
                f"- `{item.get('method')}` `{item.get('path')}` from `{item.get('source')}:{item.get('line')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Dynamic Frontend API Reviews", ""])
    dynamic_reviews = payload.get("frontend_dynamic_api_reviews") or []
    if dynamic_reviews:
        for item in dynamic_reviews[:80]:
            lines.append(
                f"- `{item.get('method')}` `{item.get('raw_url')}` from `{item.get('source')}:{item.get('line')}` "
                f"status=`{item.get('coverage_status')}` action_required=`{item.get('action_required')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Server Prefix Routes", ""])
    prefixes = payload.get("server_prefix_routes") or {}
    lines.extend([f"- `{path}` methods={methods}" for path, methods in prefixes.items()] if prefixes else ["- none"])
    lines.extend(["", "## Server Routes Not Referenced By Frontend", ""])
    unreferenced = payload.get("server_unreferenced_by_frontend") or []
    if unreferenced:
        for item in unreferenced[:80]:
            lines.append(f"- `{item.get('method')}` `{item.get('path')}` line={item.get('line')}")
    else:
        lines.append("- none")
    lines.extend(["", "## Server Route Review Queue", ""])
    route_reviews = payload.get("server_unreferenced_route_reviews") or []
    if route_reviews:
        for item in route_reviews[:80]:
            lines.append(
                f"- `{item.get('method')}` `{item.get('path')}` class=`{item.get('usage_class')}` "
                f"recommendation=`{item.get('recommendation')}` action_required=`{item.get('action_required')}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Server Route Review Counts", ""])
    usage_counts = payload.get("server_unreferenced_usage_class_counts") or {}
    lines.extend([f"- `{key}`: `{value}`" for key, value in usage_counts.items()] if usage_counts else ["- none"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in payload.get("warnings") or []] if payload.get("warnings") else ["- none"])
    lines.extend(["", "## Notes", ""])
    lines.extend([f"- {item}" for item in payload.get("notes") or []] if payload.get("notes") else ["- none"])
    return "\n".join(lines) + "\n"


def write_frontend_api_contract_outputs(
    payload: dict[str, Any] | None = None,
    *,
    result_path: str | Path = RESULT_JSON,
    report_path: str | Path = REPORT_MD,
) -> dict[str, Any]:
    payload = dict(payload or build_frontend_api_contract())
    result = Path(result_path)
    report = Path(report_path)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    write_json(result, payload)
    write_md(report, render_frontend_api_contract_markdown(payload))
    return payload


def compact_frontend_api_contract_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "task_type": payload.get("task_type"),
        "frontend_source_count": payload.get("frontend_source_count"),
        "frontend_sources": payload.get("frontend_sources"),
        "server_source_count": payload.get("server_source_count"),
        "server_sources": payload.get("server_sources"),
        "frontend_endpoint_count": payload.get("frontend_endpoint_count"),
        "frontend_use_count": payload.get("frontend_use_count"),
        "server_exact_route_count": payload.get("server_exact_route_count"),
        "server_prefix_route_count": payload.get("server_prefix_route_count"),
        "frontend_missing_on_server_count": payload.get("frontend_missing_on_server_count"),
        "frontend_missing_on_server": payload.get("frontend_missing_on_server"),
        "frontend_method_gap_count": payload.get("frontend_method_gap_count"),
        "frontend_dynamic_api_literal_count": payload.get("frontend_dynamic_api_literal_count"),
        "frontend_dynamic_api_action_required_count": payload.get("frontend_dynamic_api_action_required_count"),
        "server_unreferenced_by_frontend_count": payload.get("server_unreferenced_by_frontend_count"),
        "server_unreferenced_action_required_count": payload.get("server_unreferenced_action_required_count"),
        "server_unreferenced_usage_class_counts": payload.get("server_unreferenced_usage_class_counts"),
        "warnings": payload.get("warnings"),
        "notes": payload.get("notes"),
        "output_paths": payload.get("output_paths"),
        "disclaimer": payload.get("disclaimer"),
    }


def dumps_payload(payload: dict[str, Any], *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
