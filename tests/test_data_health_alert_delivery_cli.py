from __future__ import annotations

import json
from pathlib import Path


def _health() -> dict:
    return {
        "scope_health": {"target_date": "2026-08-28"},
        "data_alerts": {
            "current": [
                {
                    "alert_key": "research_pool:coverage_incomplete_missing",
                    "dedup_key": "research_pool:coverage_incomplete_missing",
                    "scope": "research_pool",
                    "reason_code": "coverage_incomplete_missing",
                    "severity": "critical",
                }
            ],
            "resolved": [],
        },
    }


def test_cli_defaults_to_dry_run_without_loading_sendkey(tmp_path: Path, monkeypatch) -> None:
    import scripts.run_data_health_alert_delivery as cli

    health_path = tmp_path / "results/data_health/data_health_dashboard_latest.json"
    health_path.parent.mkdir(parents=True)
    health_path.write_text(json.dumps(_health()), encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "HEALTH_PATH", health_path)
    monkeypatch.setattr(
        cli,
        "_load_sendkey",
        lambda: (_ for _ in ()).throw(AssertionError("dry-run must not load SendKey")),
    )

    assert cli.main(["--json", "false"]) == 0


def test_cli_requires_all_real_delivery_gates_before_loading_sendkey(
    tmp_path: Path, monkeypatch
) -> None:
    import scripts.run_data_health_alert_delivery as cli

    health_path = tmp_path / "results/data_health/data_health_dashboard_latest.json"
    health_path.parent.mkdir(parents=True)
    health_path.write_text(json.dumps(_health()), encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "HEALTH_PATH", health_path)
    monkeypatch.delenv("QUANTDESK_DATA_HEALTH_ALERTS_PUSH", raising=False)
    monkeypatch.setattr(
        cli,
        "_load_sendkey",
        lambda: (_ for _ in ()).throw(AssertionError("blocked send must not load SendKey")),
    )

    assert cli.main(["--enable-network", "--push", "--dry-run", "false", "--json", "false"]) == 4
