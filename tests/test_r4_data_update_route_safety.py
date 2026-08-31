"""POST /api/data/update-all must never treat a falsy value as network consent.

Why this exists: the route once read

    network_allowed = bool(deps.realtime_network_enabled() or body.get("explicit_network"))

and Python considers the non-empty string "false" truthy, so a client sending
{"explicit_network": "false"} was granted network access and a full-market
catch-up. The fix routes the value through the project's strict truthy helper.
This test pins the *semantics*, not the implementation, so a future refactor
that reintroduces bool() fails here.

No network is used: the updater is a tripwire fake that records invocation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.server.helpers.util import request_truthy  # noqa: E402


# Values a client might send that must NEVER authorize network access.
FALSY_INPUTS = [False, "false", "False", "FALSE", 0, "0", "", "no", "No",
                "off", "Off", None, [], {}, "none", "null"]
# The only forms that may authorize it.
TRUTHY_INPUTS = [True, "true", "True", "TRUE", 1, "1", "yes", "Yes", "on", "On"]


@pytest.mark.parametrize("value", FALSY_INPUTS)
def test_strict_truthy_rejects_every_falsy_form(value) -> None:
    assert request_truthy(value) is False, (
        f"request_truthy({value!r}) granted consent; a bare bool() would too"
    )


@pytest.mark.parametrize("value", TRUTHY_INPUTS)
def test_strict_truthy_accepts_explicit_affirmatives(value) -> None:
    assert request_truthy(value) is True, f"request_truthy({value!r}) refused consent"


def test_bare_bool_would_have_been_wrong_for_the_string_false() -> None:
    """Documents the exact defect this route once had.

    Without this, a future 'simplification' back to bool() looks harmless.
    """
    assert bool("false") is True, "sanity: Python treats a non-empty string as truthy"
    assert request_truthy("false") is False, "strict helper must disagree with bool()"


def test_route_uses_the_strict_helper_not_bare_bool() -> None:
    """The route source must not gate network access on bool() of the body value."""
    source = (ROOT / "scripts" / "server" / "routes" / "data_update.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "request_truthy" in source, "route no longer uses the strict truthy helper"
    # the specific defective shape must not reappear
    assert 'bool(deps.realtime_network_enabled() or body.get("explicit_network"))' not in source
    assert 'or body.get("explicit_network")' not in source, (
        "network consent must not be OR-ed straight out of the request body"
    )


class _TripwireUpdater:
    """Records any invocation. Never performs work."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "the full-market catch-up ran without explicit network consent"
        )


class _LockTripwire:
    """Mirrors the real data-write lock: acquire(blocking=False) / release().

    Counts both so a refused request cannot silently leak the lock.
    """

    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self, blocking: bool = True) -> bool:  # noqa: ARG002
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


def _client_and_tripwire(tmp_path: Path):
    """Mount only the data-update blueprint, with every dependency injected.

    Uses the route's real DataUpdateRouteDeps contract rather than patching the
    module, so the test exercises the same wiring quant_api_server uses.
    """
    from flask import Flask

    from scripts.server.routes.data_update import (
        DataUpdateRouteDeps,
        create_data_update_blueprint,
    )

    updater = _TripwireUpdater()
    lock = _LockTripwire()

    def _repair() -> tuple[int, str, dict]:
        raise AssertionError("repair-daily must not run in this test")

    deps = DataUpdateRouteDeps(
        project_root=lambda: tmp_path,
        data_write_lock=lambda: lock,
        realtime_network_enabled=lambda: False,
        run_full_market_catchup=updater,
        run_repair_daily=_repair,
    )
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(create_data_update_blueprint(deps))
    return app.test_client(), updater, lock


@pytest.mark.parametrize("payload", [
    {"explicit_network": "false"},
    {"explicit_network": False},
    {"explicit_network": "0"},
    {"explicit_network": 0},
    {"explicit_network": ""},
    {"explicit_network": "no"},
    {"explicit_network": "off"},
    {},
])
def test_update_all_refuses_without_explicit_consent(tmp_path, payload) -> None:
    client, updater, lock = _client_and_tripwire(tmp_path)
    response = client.post(
        "/api/data/update-all",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 403, (
        f"payload {payload!r} was not refused; status={response.status_code} "
        f"body={response.get_data(as_text=True)[:300]}"
    )
    body = response.get_json() or {}
    assert body.get("status") == "network_blocked", body
    assert updater.calls == [], "the updater ran despite a refused request"
    assert lock.acquired == lock.released, "the data write lock leaked on refusal"


def test_update_all_reports_a_ui_message_when_blocked(tmp_path) -> None:
    client, _, _ = _client_and_tripwire(tmp_path)
    response = client.post(
        "/api/data/update-all",
        data=json.dumps({"explicit_network": "false"}),
        content_type="application/json",
    )
    body = response.get_json() or {}
    assert body.get("message_for_ui"), "a blocked update must explain itself to the UI"


@pytest.mark.parametrize("value", ["true", True, "1", 1, "yes", "on"])
def test_update_all_reaches_the_updater_only_with_explicit_consent(tmp_path, value) -> None:
    """The affirmative path must actually be reachable, or the refusal proves nothing."""
    client, updater, _ = _client_and_tripwire(tmp_path)
    response = client.post(
        "/api/data/update-all",
        data=json.dumps({"explicit_network": value}),
        content_type="application/json",
    )
    # The tripwire raises once reached, so the route surfaces a 500-class error
    # rather than a 403. What matters is that it was NOT refused as blocked.
    body = response.get_json() or {}
    assert body.get("status") != "network_blocked", (
        f"explicit consent {value!r} was still treated as blocked"
    )
    assert updater.calls or response.status_code >= 500, (
        f"explicit consent {value!r} neither reached the updater nor errored; "
        f"status={response.status_code} body={response.get_data(as_text=True)[:200]}"
    )
