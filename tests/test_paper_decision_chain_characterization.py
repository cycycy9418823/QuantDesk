"""Task A0 — characterization of the current (legacy) daily paper decision path.

These tests freeze the *current* baseline before any new brain/decision-chain
work is wired in. They prove three things about the authority-as-of-baseline:

1. The active daily workflow still drives paper state through the legacy
   ``run_auto_strategy_cycle`` -> ``run_auto_paper_portfolio`` flow.
2. That legacy flow does NOT call the new Market Brain, Strategy Brain,
   ``build_portfolio_snapshot``, ``evaluate_paper_risk_gate``,
   ``simulate_paper_intent``, or ``apply_paper_event``.
3. The legacy candidate contract still emits the frozen symbols, lanes, scores,
   ranks, default 10,000 CNY profile, and legacy paper payload fields.

If a later change wires the new chain into the legacy path, or renames/reshapes
the frozen legacy payload, these tests must fail so the change is reviewed
against the promotion gate (Gate C) rather than landing silently.

The tests are deterministic: they use throwaway temporary universes/daily CSVs
and never read canonical ``data/daily`` or write any paper state.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import quantdesk.auto_strategy_sandbox as sandbox
from quantdesk.candidate_contract import candidate_rows_from_strategy_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The new-brain / decision-chain production entrypoints that the legacy daily
# path must NOT call directly. Names are matched as called attributes or bare
# calls in the legacy source files.
#
# 2026-07-20 constraint revision (task book section 5.5, pre-approved by the
# user): the original A0 rule was "legacy chain must not touch the new brain
# stack at all". Stage 1 opens exactly ONE sanctioned entry — the thin
# assembly layer quantdesk/market_brain_daily.py. The legacy chain may import
# and call run_daily_brain_snapshots (read-only, results/-artifacts-only,
# shadow-only), but still must never import or call the raw brain/kernel
# builders below directly. market_brain_daily.py itself is the only module
# allowed to compose those builders for the daily path.
FORBIDDEN_NEW_CHAIN_CALLS = (
    "build_market_brain_snapshot",
    "build_market_brain_from_frame",
    "build_strategy_brain_snapshot",
    "build_portfolio_snapshot",
    "build_paper_order_intent",
    "evaluate_paper_risk_gate",
    "simulate_paper_intent",
    "apply_paper_event",
)

# Source files that form the current legacy daily paper path.
LEGACY_CHAIN_FILES = (
    "quantdesk/daily_workflow_orchestrator.py",
    "quantdesk/auto_strategy_sandbox.py",
    "quantdesk/paper/auto_paper_portfolio.py",
    "quantdesk/paper/execution/__init__.py",
    "quantdesk/paper/execution/ports.py",
    "quantdesk/paper/execution/daily.py",
    "quantdesk/paper/execution/conditional.py",
    "quantdesk/paper/execution/snapshot.py",
)


def _called_names(tree: ast.AST) -> set[str]:
    """Collect every function/attribute name that appears in a call position."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _imported_names(tree: ast.AST) -> set[str]:
    """Collect names introduced by import / from-import statements."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


class LegacyDailyPathCharacterizationTests(unittest.TestCase):
    """Prove the active daily workflow still uses the legacy writer only."""

    def test_orchestrator_imports_legacy_writer_entrypoints(self) -> None:
        source = (PROJECT_ROOT / "quantdesk/daily_workflow_orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = _imported_names(tree)
        # The two legacy authorities that own candidate generation and paper state.
        self.assertIn("run_auto_strategy_cycle", imported)
        self.assertIn("run_auto_paper_portfolio", imported)

    def test_orchestrator_calls_legacy_writer(self) -> None:
        source = (PROJECT_ROOT / "quantdesk/daily_workflow_orchestrator.py").read_text(encoding="utf-8")
        called = _called_names(ast.parse(source))
        self.assertIn("run_auto_strategy_cycle", called)
        self.assertIn("run_auto_paper_portfolio", called)

    def test_orchestrator_uses_only_the_sanctioned_assembly_entry(self) -> None:
        # Stage 1 (task book section 5.5): the orchestrator's ONLY doorway to
        # the new brain stack is the market_brain_daily assembly layer. Assert
        # the sanctioned entry is present AND the raw builders stay out.
        source = (PROJECT_ROOT / "quantdesk/daily_workflow_orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = _imported_names(tree)
        called = _called_names(tree)
        self.assertIn(
            "run_daily_brain_snapshots",
            imported,
            msg="Stage 1 wiring lost: orchestrator no longer imports the assembly entry",
        )
        self.assertIn("run_daily_brain_snapshots", called)
        # The assembly entry must come from market_brain_daily, not be re-implemented.
        self.assertIn("quantdesk.market_brain_daily", source)

    def test_legacy_chain_does_not_call_new_brain_or_kernel(self) -> None:
        offenders: dict[str, list[str]] = {}
        for rel in LEGACY_CHAIN_FILES:
            path = PROJECT_ROOT / rel
            called = _called_names(ast.parse(path.read_text(encoding="utf-8")))
            hits = sorted(called.intersection(FORBIDDEN_NEW_CHAIN_CALLS))
            if hits:
                offenders[rel] = hits
        self.assertEqual(
            offenders,
            {},
            msg=(
                "Legacy daily path unexpectedly calls new brain/kernel entrypoints; "
                "promotion must go through Gate C, not silently: "
                f"{offenders}"
            ),
        )

    def test_legacy_chain_does_not_wire_new_brain_snapshot_builders(self) -> None:
        # The legacy path may legitimately reuse capital-profile constants/helpers
        # from portfolio_contracts (ACCEPTANCE_CAPITAL, resolve_paper_capital_profile,
        # PaperStateStore, trade_costs). What it must NOT do at the A0 baseline is
        # import the new snapshot/decision builders that would indicate the new
        # chain is being wired into the legacy writer. Those builders are only
        # exported by the new-brain modules; importing their names would be the
        # signal of premature promotion.
        forbidden_names = set(FORBIDDEN_NEW_CHAIN_CALLS) | {
            "compute_decision_chain_digest",
            "build_paper_decision_chain",
        }
        offenders: dict[str, list[str]] = {}
        for rel in LEGACY_CHAIN_FILES:
            path = PROJECT_ROOT / rel
            imported = _imported_names(ast.parse(path.read_text(encoding="utf-8")))
            hits = sorted(imported.intersection(forbidden_names))
            if hits:
                offenders[rel] = hits
        self.assertEqual(
            offenders,
            {},
            msg=f"Legacy path imports new-chain snapshot/decision builders: {offenders}",
        )


class LegacyCandidateContractCharacterizationTests(unittest.TestCase):
    """Freeze the legacy candidate symbols, lanes, scores, ranks, and payload."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily = self.root / "daily"
        self.daily.mkdir()
        self.universe = self.root / "low_price_learning_symbols.csv"
        self.report = self.root / "low_price_learning_report_latest.json"
        self._write_universe(self.universe)
        # Two clean long-lane symbols and one board-lane symbol.
        self._write_daily(self.daily / "600001.csv", board=False)
        self._write_daily(self.daily / "600002.csv", board=False)
        self._write_daily(self.daily / "600003.csv", board=True)
        self._write_report(self.report)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _write_universe(path: Path) -> None:
        rows = [
            {"symbol": "600001", "name": "低价一号", "included": "true"},
            {"symbol": "600002", "name": "低价二号", "included": "true"},
            {"symbol": "600003", "name": "打板样本", "included": "true"},
        ]
        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")

    @staticmethod
    def _write_daily(path: Path, *, board: bool, rows: int = 70) -> None:
        dates = pd.date_range("2026-02-01", periods=rows, freq="B")
        close = [8 + i * 0.02 for i in range(rows)]
        if board and rows >= 2:
            close[-2] = 9.00
            close[-1] = 9.83  # near limit-up jump for the board lane
        data = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "open": [c * 0.99 for c in close],
                "high": [c * 1.02 for c in close],
                "low": [c * 0.98 for c in close],
                "close": close,
                "volume": [2_000_000] * rows,
                "amount": [45_000_000] * rows,
            }
        )
        data.to_csv(path, index=False, encoding="utf-8-sig")

    @staticmethod
    def _write_report(path: Path) -> None:
        items = []
        for symbol in ("600001", "600002", "600003"):
            items.append(
                {
                    "symbol": symbol,
                    "name": symbol,
                    "liquidity_status": "ok",
                    "data_confidence_status": "ok",
                    "amount_ma20": 45_000_000,
                    "data_confidence_score": 90,
                    "expected_trade_date": "2026-05-08",
                    "latest_date": "2026-05-08",
                }
            )
        path.write_text(
            json.dumps(
                {
                    "summary": {
                        "expected_trade_date": "2026-05-08",
                        "latest_complete_daily_date": "2026-05-08",
                    },
                    "items": items,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _run_cycle(self) -> dict:
        # Deliberately do NOT pass initial_cash: this exercises the true default
        # paper profile so the characterization freezes the real default, not a
        # value the test itself injected.
        return sandbox.run_auto_strategy_cycle(
            mode="dry_run",
            strategy="all",
            universe_path=self.universe,
            daily_dir=self.daily,
            report_json=self.report,
            dry_run=True,
        )

    def test_legacy_cycle_default_profile_is_10000(self) -> None:
        payload = self._run_cycle()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        # The frozen default paper profile for the legacy flow lives in the
        # legacy paper_trading sub-payload (not a top-level field). No explicit
        # initial_cash was passed, so this is the genuine default.
        paper = payload["paper_trading"]
        self.assertEqual(int(paper["initial_cash"]), 10_000)
        self.assertEqual(int(paper["cash"]), 10_000)
        self.assertEqual(int(paper["total_equity"]), 10_000)
        # Frozen legacy paper payload marker fields.
        self.assertEqual(paper["order_source"], "auto_strategy_sandbox")
        self.assertEqual(paper["positions"], [])
        self.assertEqual(paper["trades"], [])

    def test_legacy_payload_has_frozen_lane_fields(self) -> None:
        payload = self._run_cycle()
        # Legacy lanes are exposed as long_candidates / board_candidates.
        self.assertIn("long_candidates", payload)
        self.assertIn("board_candidates", payload)
        long_items = payload["long_candidates"]
        board_items = payload["board_candidates"]
        self.assertTrue(long_items, "expected at least one long-lane candidate")
        # Frozen legacy scoring field names per lane.
        self.assertIn("long_score", long_items[0])
        self.assertEqual(long_items[0]["strategy_type"], "long")
        if board_items:
            self.assertIn("board_score", board_items[0])
            self.assertEqual(board_items[0]["strategy_type"], "board")

    def test_legacy_lane_sequence_is_frozen_exactly(self) -> None:
        payload = self._run_cycle()
        # Freeze the exact per-lane (symbol, score, rank) sequence the legacy
        # selectors emit for this deterministic fixture. Any change to the
        # legacy scoring/ranking/ordering must fail here and be reviewed.
        long_seq = [
            (row["symbol"], round(float(row["long_score"]), 2), int(row["rank"]))
            for row in payload["long_candidates"]
        ]
        board_seq = [
            (row["symbol"], round(float(row["board_score"]), 2), int(row["rank"]))
            for row in payload["board_candidates"]
        ]
        self.assertEqual(
            long_seq,
            [
                ("600003", 84.99, 1),
                ("600001", 81.97, 2),
                ("600002", 81.97, 3),
            ],
        )
        self.assertEqual(board_seq, [("600003", 64.11, 1)])

    def test_legacy_candidate_rows_expose_frozen_full_sequence(self) -> None:
        payload = self._run_cycle()
        rows = candidate_rows_from_strategy_payload(payload, strategy="all")
        self.assertTrue(rows, "candidate contract produced no rows")
        # Frozen legacy candidate-row identity fields consumed by the daily UI.
        for field in ("symbol", "name", "rank", "strategy_type", "score"):
            self.assertIn(field, rows[0], msg=f"legacy candidate row missing '{field}'")
        # Freeze the complete symbol / lane / score / rank sequence exactly.
        actual = [
            (row["symbol"], row["strategy_type"], round(float(row["score"]), 2), int(row["rank"]))
            for row in rows
        ]
        self.assertEqual(
            actual,
            [
                ("600003", "long", 84.99, 1),
                ("600001", "long", 81.97, 2),
                ("600002", "long", 81.97, 3),
                ("600003", "board", 64.11, 1),
            ],
        )

    def test_legacy_cycle_does_not_emit_new_chain_fields(self) -> None:
        payload = self._run_cycle()
        # The legacy payload must not yet carry new decision-chain identity.
        for forbidden in ("chain_digest", "decision_chain_digest", "market_brain_digest"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
