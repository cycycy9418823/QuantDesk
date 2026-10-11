"""Theme-map routes for the QuantDesk 8000 server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Flask, jsonify, request

from scripts.server.helpers.util import clean_payload, safe_int
from quantdesk.theme_map_import_draft import resolve_import_draft_output_path


@dataclass(frozen=True)
class ThemeMapRouteDeps:
    """Callbacks that keep this blueprint decoupled from quant_api_server."""

    stock_theme_map_csv: Callable[[], Path]
    stock_theme_map_import_csv: Callable[[], Path]
    theme_map_status_payload: Callable[[], dict[str, Any]]
    theme_map_preview: Callable[[int], list[dict[str, Any]]]
    preview_theme_map_import_draft: Callable[[int], dict[str, Any]]
    generate_theme_map_import_draft: Callable[..., dict[str, Any]]
    import_theme_map: Callable[..., dict[str, Any]]
    update_theme_map: Callable[..., dict[str, Any]]
    rebuild_limit_up_review_attribution_cache: Callable[[], dict[str, Any]]
    resolve_project_csv_path: Callable[[Any, Path], Path]
    request_truthy: Callable[[Any], bool]


def create_theme_map_blueprint(deps: ThemeMapRouteDeps) -> Blueprint:
    """Create theme-map routes used by the active 8000 UI."""

    bp = Blueprint("theme_map_api", __name__)

    def request_object() -> tuple[dict[str, Any] | None, Any]:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, (jsonify({
                "ok": False, "status": "bad_request_body",
                "message_for_ui": "请求正文必须是 JSON 对象；空操作参数请使用 {}。",
            }), 400)
        return body, None

    @bp.route("/api/theme-map/status", methods=["GET"])
    def get_theme_map_status() -> Any:
        """Read-only theme-map status; never connects to the network."""
        return jsonify(clean_payload(deps.theme_map_status_payload()))

    @bp.route("/api/theme-map/preview", methods=["GET"])
    def get_theme_map_preview() -> Any:
        try:
            limit = max(1, min(80, int(request.args.get("limit") or 20)))
        except (TypeError, ValueError):
            limit = 20
        rows = deps.theme_map_preview(limit)
        return jsonify(
            clean_payload(
                {
                    "ok": True,
                    "rows": rows,
                    "count": len(rows),
                    "path": str(deps.stock_theme_map_csv()),
                    "message_for_ui": "映射预览只读，不会联网。",
                }
            )
        )

    @bp.route("/api/theme-map/import-draft/preview", methods=["GET"])
    def get_theme_map_import_draft_preview() -> Any:
        try:
            limit = max(1, min(80, int(request.args.get("limit") or 20)))
        except (TypeError, ValueError):
            limit = 20
        return jsonify(clean_payload(deps.preview_theme_map_import_draft(limit)))

    @bp.route("/api/theme-map/import-draft", methods=["POST"])
    @bp.route("/api/theme-map/generate-import-draft", methods=["POST"])
    def generate_theme_map_import_draft_endpoint() -> Any:
        body, error = request_object()
        if error is not None:
            return error
        source = str(body.get("source") or "current").strip().lower()
        if source not in {"current", "pending", "low-price", "all"}:
            source = "current"
        try:
            output_path = resolve_import_draft_output_path(body.get("output"))
            result = deps.generate_theme_map_import_draft(
                source=source,
                overwrite=deps.request_truthy(body.get("overwrite")),
                network=False,
                limit=max(0, safe_int(body.get("limit"), 0)),
                output_path=output_path,
            )
        except ValueError as exc:
            return jsonify(clean_payload({"ok": False, "status": "bad_output_path", "message_for_ui": str(exc)})), 400
        result["message_for_ui"] = (
            f"已生成 stock_theme_map_import.csv 草稿：写入 {result.get('written_rows', 0)} 行，"
            f"已自动填充 {result.get('filled_count', 0)} 行，仍待人工确认 {result.get('pending_manual_count', 0)} 行。"
        )
        result["boundary"] = "本地导入草稿默认不联网；仅用于补齐题材映射，不接券商、不下单。"
        return jsonify(clean_payload(result))

    @bp.route("/api/theme-map/import", methods=["POST"])
    def import_theme_map_endpoint() -> Any:
        body, error = request_object()
        if error is not None:
            return error
        try:
            input_path = deps.resolve_project_csv_path(
                body.get("input") or body.get("input_path"),
                deps.stock_theme_map_import_csv(),
            )
        except ValueError as exc:
            return jsonify(clean_payload({"ok": False, "status": "bad_import_path", "message_for_ui": str(exc)})), 400
        result = deps.import_theme_map(
            input_path,
            overwrite_manual=deps.request_truthy(body.get("overwrite_manual")),
            limit=max(0, safe_int(body.get("limit"), 0)),
        )
        result["theme_map_status"] = deps.theme_map_status_payload()
        result["limit_up_review"] = deps.rebuild_limit_up_review_attribution_cache()
        result["message_for_ui"] = result.get("message") or "已导入本地题材映射草稿。"
        result["boundary"] = "导入只读取本地 CSV，不联网、不接券商、不触发真实交易。"
        return jsonify(clean_payload(result))

    @bp.route("/api/theme-map/refresh", methods=["POST"])
    @bp.route("/api/theme-map/update", methods=["POST"])
    def refresh_theme_map_endpoint() -> Any:
        body, error = request_object()
        if error is not None:
            return error
        source = str(body.get("source") or "local").strip().lower()
        if source not in {"local", "auto", "akshare"}:
            source = "local"
        network_confirmed = deps.request_truthy(body.get("network"))
        if source in {"auto", "akshare"} and not network_confirmed:
            return (
                jsonify(
                    clean_payload(
                        {
                            "ok": False,
                            "status": "network_not_confirmed",
                            "message_for_ui": "联网补全题材映射需要显式 network=true；状态读取和本地导入不会联网。",
                            "boundary": "Research Only；不接券商、不下单、不构成投资建议。",
                        }
                    )
                ),
                400,
            )
        result = deps.update_theme_map(
            source=source,
            overwrite_manual=deps.request_truthy(body.get("overwrite_manual")),
            only_low_price=not (str(body.get("only_low_price") or "true").strip().lower() in {"0", "false", "no", "off"}),
            limit=max(0, safe_int(body.get("limit"), 0)),
        )
        result["theme_map_status"] = deps.theme_map_status_payload()
        result["limit_up_review"] = deps.rebuild_limit_up_review_attribution_cache()
        result["message_for_ui"] = result.get("message") or (
            "板块/题材映射已更新。"
            if result.get("ok")
            else "板块/题材映射更新未获得可用外部数据。"
        )
        result["boundary"] = (
            "联网刷新由用户显式触发，仅写入本地题材映射；Research Only，No Broker Route，Not Investment Advice。"
            if source in {"auto", "akshare"}
            else "本地刷新只读取已有 CSV/快照；Research Only，No Broker Route。"
        )
        return jsonify(clean_payload(result))

    return bp


def register_theme_map_routes(app: Flask, deps: ThemeMapRouteDeps) -> None:
    app.register_blueprint(create_theme_map_blueprint(deps))
