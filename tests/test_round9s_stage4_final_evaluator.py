"""Round 9S Part B — Stage 4 terminal evaluator tests (fixture-driven).

Proves the evaluator's honesty contract on three synthetic shapes plus a
real-shaped probe:

* Any synthetic / fixture product is NEVER formal strategy evidence — the
  acceptor invariant (``acceptance.py:149`` forces ``evidence_eligible=False``
  on every synthetic schedule).  A synthetic product must therefore surface as
  ``synthetic_fixture_not_conclusion`` regardless of sample size, while still
  reporting its metrics honestly (numbers shown, no conclusion drawn).
* A thin sample (< ``MIN_EFFECTIVE_SAMPLE``) is ``insufficient_sample``.
* Benchmark days with no local data are ``benchmark_unavailable`` and NEVER
  extrapolated (W1 honesty ruling).
* The gate is not a dead ``False``: a real-shaped product that clears every
  gate earns ``return_conclusion_eligible`` with ``fixture_or_synthetic=False``.

No network, no canonical/state writes, no real conclusions (real data waits for
T0+18).  Boundary: Research Only / Paper Account Only / No Broker Route / not
advice.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import pytest

from quantdesk.paper.stage4_final_evaluator import (
    EVALUATOR_SCHEMA_VERSION,
    FINAL_ARTIFACT_SCHEMA_VERSION,
    MIN_EFFECTIVE_SAMPLE,
    build_stage4_final_evaluation_artifact,
    build_synthetic_replay_fixture,
    evaluate_stage4_final,
    write_stage4_final_evaluation_artifact,
)


def _real_shaped_product(
    *,
    trade_count: int,
    dates: list[str] | None = None,
    all_eligible: bool = True,
) -> dict:
    """A product that carries NO synthetic marker — models a real replay output.

    Deliberately omits ``fixture_kind`` / ``synthetic://`` source / fixture
    reason codes so the synthetic gate does not trip; used to prove the gate is
    live rather than a constant ``False``.
    """

    window = dates or [f"2026-09-{i:02d}" for i in range(1, 21)]
    trades = []
    nav = [1.0]
    for i in range(trade_count):
        pnl = round(0.004 * ((i % 4) - 1), 6)
        trades.append(
            {
                "trade_id": f"real_{i + 1}",
                "symbol": f"60010{i % 9}",
                "entry_date": window[i % (len(window) - 2)],
                "exit_date": window[(i % (len(window) - 2)) + 2],
                "hold_days": 2,
                "pnl_pct": pnl,
                "fees": 4.0,
                "evidence_eligible": all_eligible,
                "evidence_ref": f"sha256:real_evidence_{i + 1}",
                "reason_codes": [],
            }
        )
        nav.append(round(nav[-1] * (1.0 + pnl), 8))
    return {
        "window_start": window[0],
        "window_end": window[-1],
        "window_dates": window,
        "as_of": window[-1],
        "trades": trades,
        "nav_series": [
            {"date": window[min(i, len(window) - 1)], "nav": v} for i, v in enumerate(nav)
        ],
        "effective_sell_sample": trade_count,
        "evidence_eligible": True,
        # No fixture_kind, no synthetic source, no fixture reason codes.
        "reason_codes": [],
    }


class SyntheticShapesTests(unittest.TestCase):
    def test_normal_sample_reports_metrics_but_is_never_a_conclusion(self):
        product = build_synthetic_replay_fixture(kind="normal_sample")
        result = evaluate_stage4_final(product).to_dict()

        # Sample is sufficient, yet the synthetic gate forbids a conclusion.
        self.assertTrue(result["sample_sufficient"])
        self.assertFalse(result["insufficient_sample"])
        self.assertFalse(result["evidence_eligible"])
        self.assertEqual(result["verdict"], "synthetic_fixture_not_conclusion")
        self.assertTrue(result["fixture_or_synthetic"])
        self.assertIn("synthetic_source_not_formal_evidence", result["reason_codes"])
        # Metrics are still computed honestly (numbers shown, no beautification).
        self.assertIsNotNone(result["metrics"]["total_return"])
        self.assertIsNotNone(result["metrics"]["max_drawdown"])
        self.assertIsNotNone(result["metrics"]["win_rate"])
        self.assertGreater(result["metrics"]["total_fees"], 0.0)
        self.assertTrue(result["metrics"]["hold_days_distribution"])
        # Boundary flags intact.
        self.assertFalse(result["risk_gate_enforced"])
        self.assertFalse(result["is_real_order"])
        self.assertTrue(result["research_only"])
        self.assertTrue(result["paper_only"])
        self.assertTrue(result["no_broker"])
        self.assertEqual(result["schema_version"], EVALUATOR_SCHEMA_VERSION)

    def test_insufficient_sample_shape_flags_both_thin_and_synthetic(self):
        product = build_synthetic_replay_fixture(kind="insufficient_sample")
        result = evaluate_stage4_final(product).to_dict()

        self.assertFalse(result["sample_sufficient"])
        self.assertTrue(result["insufficient_sample"])
        self.assertFalse(result["evidence_eligible"])
        self.assertLess(result["metrics"]["effective_sample"], MIN_EFFECTIVE_SAMPLE)
        self.assertIn("insufficient_sample", result["reason_codes"])
        self.assertIn("synthetic_source_not_formal_evidence", result["reason_codes"])
        # The upstream fixture reason code is preserved, not swallowed.
        self.assertIn("acceptance_fixture_not_strategy_evidence", result["reason_codes"])

    def test_benchmark_missing_shape_marks_unavailable_without_extrapolation(self):
        product = build_synthetic_replay_fixture(kind="benchmark_missing")
        # benchmark_by_date is None -> no local benchmark data for the window.
        result = evaluate_stage4_final(product, benchmark_by_date=None).to_dict()

        bench = result["benchmark"]
        self.assertEqual(bench["status"], "benchmark_unavailable")
        self.assertFalse(bench["extrapolated"])
        self.assertIsNone(bench["benchmark_return"])
        self.assertGreater(bench["missing_day_count"], 0)
        self.assertIn("benchmark_source_missing", bench["reason_codes"])
        # Every series point is explicitly unavailable, none fabricated.
        self.assertTrue(all(p["value"] is None for p in bench["series"]))
        self.assertTrue(
            all(p["status"] == "benchmark_unavailable" for p in bench["series"])
        )


class BenchmarkAlignmentTests(unittest.TestCase):
    def test_partial_local_benchmark_marks_only_missing_days(self):
        dates = [f"2026-09-{i:02d}" for i in range(1, 6)]
        product = build_synthetic_replay_fixture(
            kind="normal_sample", trade_count=32, window_dates=dates
        )
        # Local benchmark covers only 3 of 5 days; the other 2 must be unavailable.
        benchmark_by_date = {
            dates[0]: 100.0,
            dates[1]: 101.0,
            dates[3]: 102.0,
        }
        bench = evaluate_stage4_final(
            product, benchmark_by_date=benchmark_by_date, window_dates=dates
        ).to_dict()["benchmark"]

        self.assertEqual(bench["status"], "partial_benchmark_unavailable")
        self.assertEqual(bench["available_days"], 3)
        self.assertEqual(bench["missing_day_count"], 2)
        self.assertFalse(bench["extrapolated"])
        self.assertIn(dates[2], bench["missing_days"])
        self.assertIn(dates[4], bench["missing_days"])
        # Benchmark return derives only from available anchors, not filled gaps.
        self.assertIsNotNone(bench["benchmark_return"])

    def test_full_local_benchmark_aligns(self):
        dates = [f"2026-09-{i:02d}" for i in range(1, 5)]
        product = build_synthetic_replay_fixture(
            kind="normal_sample", trade_count=32, window_dates=dates
        )
        benchmark_by_date = {d: 100.0 + idx for idx, d in enumerate(dates)}
        bench = evaluate_stage4_final(
            product, benchmark_by_date=benchmark_by_date, window_dates=dates
        ).to_dict()["benchmark"]

        self.assertEqual(bench["status"], "aligned")
        self.assertEqual(bench["missing_day_count"], 0)
        self.assertFalse(bench["extrapolated"])


class SyntheticGateLivenessTests(unittest.TestCase):
    """Prove the synthetic gate is a live discriminator, not a constant False."""

    def test_real_shaped_product_can_earn_conclusion(self):
        product = _real_shaped_product(trade_count=31)
        result = evaluate_stage4_final(product).to_dict()

        self.assertFalse(result["fixture_or_synthetic"])
        self.assertTrue(result["sample_sufficient"])
        self.assertTrue(result["evidence_eligible"])
        self.assertEqual(result["verdict"], "return_conclusion_eligible")
        self.assertNotIn("synthetic_source_not_formal_evidence", result["reason_codes"])
        # Metrics are real numbers.
        self.assertIsNotNone(result["metrics"]["total_return"])
        self.assertIsNotNone(result["metrics"]["win_rate"])

    def test_real_shaped_thin_sample_is_insufficient_not_eligible(self):
        product = _real_shaped_product(trade_count=12)
        result = evaluate_stage4_final(product).to_dict()

        self.assertFalse(result["fixture_or_synthetic"])
        self.assertFalse(result["evidence_eligible"])
        self.assertEqual(result["verdict"], "insufficient_sample")
        self.assertIn("insufficient_sample", result["reason_codes"])

    def test_synthetic_source_path_trips_gate_even_without_fixture_kind(self):
        product = _real_shaped_product(trade_count=31)
        product.pop("reason_codes", None)
        product["source_path"] = "synthetic://forced_probe"
        result = evaluate_stage4_final(product).to_dict()

        self.assertTrue(result["fixture_or_synthetic"])
        self.assertFalse(result["evidence_eligible"])
        self.assertEqual(result["verdict"], "synthetic_fixture_not_conclusion")

    def test_real_product_with_ineligible_trade_is_not_eligible(self):
        product = _real_shaped_product(trade_count=31, all_eligible=False)
        result = evaluate_stage4_final(product).to_dict()

        self.assertFalse(result["fixture_or_synthetic"])
        self.assertFalse(result["evidence_eligible"])
        self.assertEqual(result["verdict"], "not_evidence_eligible")


class TradeEvidenceTests(unittest.TestCase):
    def test_trade_evidence_refs_are_carried_through(self):
        product = build_synthetic_replay_fixture(kind="normal_sample", trade_count=32)
        result = evaluate_stage4_final(product).to_dict()

        self.assertEqual(len(result["trade_evidence"]), 32)
        first = result["trade_evidence"][0]
        for key in (
            "trade_id",
            "symbol",
            "entry_date",
            "exit_date",
            "hold_days",
            "pnl_pct",
            "fees",
            "evidence_eligible",
            "evidence_ref",
            "reason_codes",
        ):
            self.assertIn(key, first)
        self.assertTrue(first["evidence_ref"])


def test_terminal_artifact_hashes_exact_adapter_and_publishes_thin_sample(
    tmp_path: Path,
) -> None:
    as_of = "2026-08-20"
    adapter_path = (
        tmp_path
        / "results"
        / "stage4_replay_adapter"
        / as_of
        / "adapter_result.json"
    )
    adapter_path.parent.mkdir(parents=True)
    adapter = {
        "schema_version": "stage4_tri_account_replay_adapter_v1",
        "task_type": "stage4_tri_account_replay_adapter",
        "status": "real_replay_ready",
        "quality_gate": {"passed": True, "reason_codes": []},
        "fixture_or_synthetic": False,
        "maintenance_eligible": True,
        "replay_product": _real_shaped_product(
            trade_count=6,
            dates=[f"2026-08-{day:02d}" for day in range(1, 21)],
        ),
        "benchmark_by_date": {
            f"2026-08-{day:02d}": 100.0 + day for day in range(1, 21)
        },
        "source_manifest": {
            "data_mode": "real_canonical",
            "seal": "sha256:" + "a" * 64,
        },
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "is_real_order": False,
    }
    adapter_path.write_text(json.dumps(adapter, sort_keys=True), encoding="utf-8")
    expected_hash = "sha256:" + hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    maintenance = {
        "as_of": as_of,
        "status": "closed_inconclusive",
        "terminal_reached": True,
        "maintenance_disposition": "inconclusive",
        "sample_disposition": "not_evaluable",
        "account_replay": "enabled_real",
        "reason_codes": ["shadow_daily_count_gap"],
    }

    artifact = build_stage4_final_evaluation_artifact(
        adapter_path=adapter_path,
        project_root=tmp_path,
        maintenance_evidence=maintenance,
        as_of=as_of,
    )

    assert artifact["schema_version"] == FINAL_ARTIFACT_SCHEMA_VERSION
    assert artifact["source_adapter"]["sha256"] == expected_hash
    assert artifact["evaluator_verdict"] == "insufficient_sample"
    assert artifact["sample_disposition"] == "insufficient_sample"
    assert artifact["evaluation"]["metrics"]["effective_sample"] == 6
    assert artifact["evaluation"]["metrics"]["min_effective_sample"] == 30

    paths = write_stage4_final_evaluation_artifact(
        artifact,
        output_dir=tmp_path / "results" / "data_health",
        results_root=tmp_path / "results",
        as_of=as_of,
    )
    first_bytes = paths["dated"].read_bytes()
    second = write_stage4_final_evaluation_artifact(
        artifact,
        output_dir=tmp_path / "results" / "data_health",
        results_root=tmp_path / "results",
        as_of=as_of,
    )
    assert second == paths
    assert paths["dated"].read_bytes() == first_bytes
    conflicting = dict(artifact)
    conflicting["reason_codes"] = ["different"]
    with pytest.raises(RuntimeError, match="stage4_final_evaluation_conflict"):
        write_stage4_final_evaluation_artifact(
            conflicting,
            output_dir=tmp_path / "results" / "data_health",
            results_root=tmp_path / "results",
            as_of=as_of,
        )


def test_terminal_builder_rejects_adapter_date_mismatch(tmp_path: Path) -> None:
    as_of = "2026-08-20"
    adapter_path = (
        tmp_path / "results" / "stage4_replay_adapter" / as_of / "adapter_result.json"
    )
    adapter_path.parent.mkdir(parents=True)
    adapter = {
        "schema_version": "stage4_tri_account_replay_adapter_v1",
        "status": "real_replay_ready",
        "quality_gate": {"passed": True, "reason_codes": []},
        "fixture_or_synthetic": False,
        "maintenance_eligible": True,
        "source_manifest": {"data_mode": "real_canonical", "seal": "sha256:" + "a" * 64},
        "replay_product": {
            **_real_shaped_product(
                trade_count=1,
                dates=["2026-08-01", "2026-08-02", "2026-08-03"],
            ),
            "as_of": "2026-08-19",
            "window_end": "2026-08-19",
        },
        "benchmark_by_date": {},
    }
    adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
    with pytest.raises(RuntimeError, match="adapter.*date|identity"):
        build_stage4_final_evaluation_artifact(
            adapter_path=adapter_path,
            project_root=tmp_path,
            maintenance_evidence={"status": "closed_inconclusive"},
            as_of=as_of,
        )


def test_terminal_builder_rejects_synthetic_adapter_boundary(tmp_path: Path) -> None:
    as_of = "2026-08-20"
    adapter_path = (
        tmp_path / "results" / "stage4_replay_adapter" / as_of / "adapter_result.json"
    )
    adapter_path.parent.mkdir(parents=True)
    adapter = {
        "schema_version": "stage4_tri_account_replay_adapter_v1",
        "status": "real_replay_ready",
        "quality_gate": {"passed": True, "reason_codes": []},
        "fixture_or_synthetic": True,
        "maintenance_eligible": False,
        "source_manifest": {"data_mode": "fixture", "seal": "sha256:" + "a" * 64},
        "replay_product": _real_shaped_product(
            trade_count=1,
            dates=["2026-08-01", "2026-08-02", "2026-08-20"],
        ),
        "benchmark_by_date": {},
    }
    adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
    with pytest.raises(RuntimeError, match="synthetic|fixture|boundary"):
        build_stage4_final_evaluation_artifact(
            adapter_path=adapter_path,
            project_root=tmp_path,
            maintenance_evidence={"status": "closed_inconclusive"},
            as_of=as_of,
        )


if __name__ == "__main__":
    unittest.main()
