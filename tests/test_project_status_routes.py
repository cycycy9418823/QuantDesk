"""Flask GET /api/project-status is fixed-file and read-only."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from scripts.server.routes.system import create_system_blueprint
from tests.test_project_status import SAMPLE_A, SAMPLE_B


class _Tripwire:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError(f"{self.name} must not run during project-status")


def _client(root: Path):
    app = Flask(__name__)
    app.config["QUANTDESK_PROJECT_ROOT"] = str(root)
    app.register_blueprint(
        create_system_blueprint(build_id="test", asset_versions_func=lambda: {})
    )
    return app.test_client()


def test_get_reads_only_status_and_ignores_path_query(tmp_path: Path) -> None:
    (tmp_path / "STATUS.md").write_text(SAMPLE_A, encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-read", encoding="utf-8")
    client = _client(tmp_path)
    response = client.get("/api/project-status?path=secret.txt")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["readable"] is True
    assert "do-not-read" not in payload["raw_text"]
    assert "日线 ✓5547/5555" in payload["health_line"]

    traversal = client.get("/api/project-status?path=../STATUS.md")
    assert traversal.get_json()["source_kind"] == "STATUS.md"
    assert secret.read_text(encoding="utf-8") == "do-not-read"


def test_post_put_delete_do_not_modify_file(tmp_path: Path) -> None:
    path = tmp_path / "STATUS.md"
    path.write_text(SAMPLE_A, encoding="utf-8")
    before = path.read_bytes()
    client = _client(tmp_path)
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/project-status")
        assert response.status_code == 405
    assert path.read_bytes() == before


def test_a_to_b_refresh_via_http(tmp_path: Path) -> None:
    path = tmp_path / "STATUS.md"
    path.write_text(SAMPLE_A, encoding="utf-8")
    client = _client(tmp_path)
    first = client.get("/api/project-status").get_json()
    path.write_text(SAMPLE_B, encoding="utf-8")
    second = client.get("/api/project-status").get_json()
    assert "✓5547/5555" in first["health_line"]
    assert "✓1/1" in second["health_line"]
    assert first["sha256"] != second["sha256"]


def test_missing_file_is_not_success_and_does_not_create_placeholder(tmp_path: Path) -> None:
    client = _client(tmp_path)
    payload = client.get("/api/project-status").get_json()
    assert payload["ok"] is False
    assert payload["readable"] is False
    assert payload["error"] == "missing_file"
    assert not (tmp_path / "STATUS.md").exists()


def test_route_does_not_call_data_health_llm_or_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "STATUS.md").write_text(SAMPLE_A, encoding="utf-8")
    traps = {
        "quantdesk.data_health_alert_delivery.dispatch_data_health_alerts": _Tripwire("alerts"),
        "quantdesk.notifications.server_chan.deliver_server_chan": _Tripwire("serverchan"),
    }
    for target, trap in traps.items():
        monkeypatch.setattr(target, trap, raising=False)
    payload = _client(tmp_path).get("/api/project-status").get_json()
    assert payload["readable"] is True
    for trap in traps.values():
        assert trap.calls == 0
