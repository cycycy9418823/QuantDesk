from __future__ import annotations

import json
from pathlib import Path
import threading

from flask import Flask
import pytest

from quantdesk.research.backtest_statistical_validation import run_backtest_statistical_validation, summarize_statistical_validation
from quantdesk.research.sample_provenance import research_sample_context
from quantdesk.strategy_observation_contract import normalize_statistical_validation_for_observation
from scripts import run_formal_alpha_adjudication, run_strategy_screening_funnel as funnel
from scripts.server.routes.strategy_screening import StrategyScreeningRouteDeps, compact_strategy_screening_result, create_strategy_screening_blueprint, run_strategy_screening_worker
from tests.test_repair_screening_cache_20261012 import _daily, _args


def test_screening_date_arguments_apply_after_historical_feature_warmup(tmp_path: Path) -> None:
    _daily(tmp_path / "daily" / "000001.csv")
    args = _args(tmp_path, "--feature-cache", "false", "--start-date", "2025-03-01", "--end-date", "2025-06-01", "--min-history-days", "30", "--max-strategies", "1")
    result = funnel.run_screening(args)
    assert result["sample_window"]["observed_start"] == "2025-03-01"
    assert result["sample_window"]["observed_end"] == "2025-06-01"
    assert result["sample_role"] == "in_sample"
    assert result["out_of_sample_validation"] == "not_established"
    for row in result["strategy_results"]:
        assert row["sample_window"] == result["sample_window"]
        for selection in row.get("selected_by_rebalance", []):
            assert "2025-03-01" <= selection["signal_date"] <= "2025-06-01"
    compact = compact_strategy_screening_result(result)
    assert compact["sample_role"] == "in_sample"
    assert compact["sample_window"] == result["sample_window"]
    assert compact["top_strategies"][0]["out_of_sample_validation"] == "not_established"


def test_significant_statistics_never_erase_sample_limitations() -> None:
    result = run_backtest_statistical_validation({"trade_returns": [.02, -.001] * 30,
        "closed_trade_count": 60, "return_evidence": {"complete_fill_history": True, "cost_coverage_complete": True},
        "sample_role": "in_sample", "start_date": "2025-01-01", "end_date": "2025-12-31"}, iterations=40, write=False)
    assert result["status"] == "passed"
    for item in (result, summarize_statistical_validation(result), normalize_statistical_validation_for_observation(result)):
        assert item["sample_role"] == "in_sample"
        assert item["out_of_sample_validation"] == "not_established"
        assert item["validation_status"] == "research_unvalidated"
        assert "样本外" in item["sample_limitation"]
    assert normalize_statistical_validation_for_observation(result)["risk_notes"]


def test_claimed_holdout_is_not_a_verified_holdout() -> None:
    context = research_sample_context({"sample_role": "out_of_sample", "out_of_sample_validation": "passed"})
    assert context["declared_sample_role"] == "out_of_sample"
    assert context["sample_role"] == "unspecified"
    assert context["out_of_sample_validation"] == "not_established"


def _deps(root: Path, started: list) -> StrategyScreeningRouteDeps:
    state = {}
    lock, data_lock = threading.Lock(), threading.Lock()
    return StrategyScreeningRouteDeps(project_root=lambda: root, task_json=lambda: root / "task.json",
        state=lambda: state, state_lock=lambda: lock, data_write_lock=lambda: data_lock,
        read_json_file=lambda _: {}, request_truthy=lambda x: x is True or x == "true",
        make_request_id=lambda: "test-id", start_worker=lambda *args: started.append(args))


def test_screening_api_rejects_client_selected_filesystem_destinations(tmp_path: Path) -> None:
    started = []
    app = Flask(__name__)
    app.register_blueprint(create_strategy_screening_blueprint(_deps(tmp_path, started)))
    client = app.test_client()
    for destination in ("../outside", str(tmp_path / "absolute")):
        response = client.post("/api/strategy-screening/run", json={"cache_dir": destination})
        assert response.status_code == 400
    assert not started
    response = client.post("/api/strategy-screening/run", json={"full_scan": True, "full_strategies": True})
    assert response.status_code == 202
    assert started[0][1]["cache_dir"] is None
    assert started[0][1]["max_symbols"] == started[0][1]["max_strategies"] == 0


def test_screening_worker_keeps_explicit_full_scope_and_server_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = []
    def run(args):
        captured.append(args)
        return {"ok": True, "status": "warning"}
    monkeypatch.setattr(funnel, "run_screening", run)
    monkeypatch.setattr(funnel, "write_outputs", lambda payload, *paths: payload)
    run_strategy_screening_worker(_deps(tmp_path, []), "test", {"max_symbols": 0, "max_strategies": 0, "cache_dir": "../outside"})
    assert len(captured) == 1
    assert captured[0].max_symbols == captured[0].max_strategies == 0
    assert captured[0].cache_dir is None
    assert Path(captured[0].output_dir) == tmp_path / "results"


def test_retired_formal_alpha_has_no_publish_or_write_path(capsys: pytest.CaptureFixture) -> None:
    assert run_formal_alpha_adjudication.main(["--evaluate", "--publish", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "retired"
    assert payload["publication_status"] == "disabled" and payload["paths"] == {}
    assert payload["runtime_write"] is False
