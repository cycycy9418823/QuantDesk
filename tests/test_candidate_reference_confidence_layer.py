from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_daily_fixture(root: Path, symbol: str = "600000") -> None:
    daily_dir = root / "data" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    rows = ["date,open,high,low,close,volume,amount"]
    start = date(2026, 1, 1)
    for offset in range(80):
        close = 10.0 + offset * 0.01
        volume = 100_000 + offset * 100
        rows.append(
            f"{start + timedelta(days=offset)},{close - 0.02:.2f},{close + 0.05:.2f},"
            f"{close - 0.05:.2f},{close:.2f},{volume},{close * volume:.2f}"
        )
    (daily_dir / f"{symbol}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")


@contextmanager
def _isolated_candidate_api():
    from scripts import quant_api_server as server

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_daily_fixture(root)
        try:
            with mock.patch.multiple(
                server,
                project_root=root,
                DAILY_DIR=root / "data" / "daily",
                TRACKING_DIR=root / "data" / "tracking",
                UNIVERSE_DIR=root / "data" / "universe",
                INDUSTRY_BOARD_CACHE=root / "data" / "cache" / "industry_board_latest.csv",
                PAPER_WATCHLIST_JSON=root / "state" / "paper_watchlist.json",
                DAILY_CANDIDATE_GENERATION_JSON=(
                    root / "results" / "daily_research" / "daily_candidate_generation_latest.json"
                ),
                DAILY_CANDIDATE_LATEST_CSV=(
                    root / "results" / "daily_research" / "v420_daily_stock_candidates_latest.csv"
                ),
                _LATEST_GENERATION_SYMBOL_CACHE={
                    "csv_mtime": None,
                    "ledger_mtime": None,
                    "signal_date": "",
                    "symbols": set(),
                },
            ):
                yield server.app
        finally:
            server._sync_market_helper_contract()
            server.market_helpers._load_stock_data_cached.cache_clear()


class CandidateReferenceConfidenceLayerTests(unittest.TestCase):
    def test_isolated_candidate_api_restores_market_helper_state(self) -> None:
        from scripts import quant_api_server as server

        market_helpers = server.market_helpers
        expected_daily_dir = server.DAILY_DIR
        market_helpers._load_stock_data_cached.cache_clear()

        with _isolated_candidate_api() as app:
            rows = app.test_client().get("/api/daily/candidates").get_json().get("all_candidates") or []
            self.assertTrue(rows)
            self.assertEqual(market_helpers.DAILY_DIR, server.DAILY_DIR)
            self.assertEqual(market_helpers._load_stock_data_cached.cache_info().currsize, 1)

        self.assertEqual(market_helpers.DAILY_DIR, expected_daily_dir)
        self.assertEqual(market_helpers.DAILY_DIR, server.DAILY_DIR)
        self.assertEqual(market_helpers._load_stock_data_cached.cache_info().currsize, 0)

    def test_intraday_file_exists_reads_flat_1m_cache_and_checks_target_date(self) -> None:
        from quantdesk.market_intelligence_utils import intraday_file_exists

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "data" / "intraday" / "1m" / "600000.csv"
            path.parent.mkdir(parents=True)
            path.write_text(
                "symbol,datetime,open,high,low,close,volume,amount,source\n"
                "600000,2026-06-26 09:30:00,10,10.1,9.9,10.0,100,1000,test\n",
                encoding="utf-8-sig",
            )

            self.assertTrue(intraday_file_exists("600000", "2026-06-26", project_root=root))
            self.assertFalse(intraday_file_exists("600000", "2026-06-25", project_root=root))

    def test_reference_confidence_marks_missing_intraday_as_unverified(self) -> None:
        from quantdesk.data_confidence import build_reference_confidence

        payload = build_reference_confidence(
            {
                "symbol": "600000",
                "latest_close": 10.0,
                "daily_amount": 100_000_000,
                "sector": "银行",
                "style": "board_watch",
                "intraday_required": True,
                "minute_data_available": False,
                "data_quality": {
                    "tone": "ok",
                    "status": "数据完整",
                    "row_count": 120,
                    "missing_reasons": ["未发现关键缺口。"],
                },
            },
            target_date="2026-06-26",
            project_root=PROJECT_ROOT,
        )

        self.assertEqual(payload["verification_status"], "intraday_unverified")
        self.assertIn(payload["data_confidence"]["status"], {"partial", "degraded"})
        self.assertIn("missing_1m_data", json.dumps(payload["missing_evidence"], ensure_ascii=False))
        self.assertIn(payload["data_confidence_status"], {"warning", "blocked"})

    def test_daily_candidates_api_exposes_confidence_fields(self) -> None:
        with _isolated_candidate_api() as app:
            payload = app.test_client().get("/api/daily/candidates").get_json()
        rows = payload.get("all_candidates") or payload.get("candidates") or []
        self.assertTrue(rows, payload.get("message"))
        first = rows[0]

        self.assertIn("data_confidence", first)
        self.assertIn(first["data_confidence"].get("status"), {"research_ready", "partial", "degraded", "missing"})
        self.assertIn("score", first["data_confidence"])
        self.assertIn("verification_status", first)
        self.assertIn("missing_evidence", first)
        self.assertIn("data_confidence_status", first)
        self.assertIn("data_confidence_score", first)
        summary = payload.get("candidate_confidence_summary") or {}
        self.assertIn("status_groups", summary)
        self.assertIn("top_data_gaps", summary)
        self.assertEqual(summary.get("candidate_count"), len(rows))
        labels = {row.get("label") for row in summary.get("status_groups") or []}
        self.assertGreaterEqual(labels, {"高可信参考", "部分可信参考", "降级参考", "证据缺失"})

    def test_candidate_confidence_summary_prioritizes_fixable_gaps(self) -> None:
        from scripts.quant_api_server import candidate_confidence_summary

        summary = candidate_confidence_summary(
            [
                {
                    "symbol": "600001",
                    "name": "缺日线",
                    "is_today_recommendation": True,
                    "data_confidence": {"status": "missing", "score": 0, "label": "证据缺失"},
                    "verification_status_label": "待补数据后复核",
                    "missing_evidence": [
                        {"kind": "missing_daily_file", "severity": "error", "message": "缺少本地日线文件"}
                    ],
                },
                {
                    "symbol": "600002",
                    "name": "缺分钟线",
                    "is_today_recommendation": True,
                    "data_confidence": {"status": "partial", "score": 72, "label": "部分可信参考"},
                    "verification_status_label": "分钟线待核验",
                    "missing_evidence": [
                        {"kind": "missing_1m_data", "severity": "warning", "message": "缺少目标日 1m 分钟线"}
                    ],
                },
                {
                    "symbol": "600003",
                    "name": "完整",
                    "data_confidence": {"status": "research_ready", "score": 96, "label": "高可信参考"},
                    "verification_status_label": "日线研究候选",
                    "missing_evidence": [],
                },
            ]
        )

        self.assertEqual(summary["candidate_count"], 3)
        self.assertEqual(summary["usable_count"], 2)
        self.assertEqual(summary["degraded_count"], 1)
        self.assertEqual(summary["top_data_gaps"][0]["kind"], "missing_daily_file")
        self.assertIn("data/daily", summary["top_data_gaps"][0]["suggested_action"])

    def test_stock_detail_strategy_review_exposes_confidence_fields(self) -> None:
        with _isolated_candidate_api() as app:
            client = app.test_client()
            rows = (client.get("/api/daily/candidates").get_json().get("all_candidates") or [])
            self.assertTrue(rows)
            first = rows[0]
            response = client.get(f"/api/stock/{first['symbol']}/detail")
        self.assertEqual(response.status_code, 200)
        review = response.get_json()["strategy_review"]

        self.assertIn("data_confidence", review)
        self.assertIn("verification_status", review)
        self.assertIn("missing_evidence", review)

    def test_daily_research_js_displays_confidence_contract(self) -> None:
        script = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")
        template = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8")

        for token in (
            "data_confidence",
            "verification_status",
            "missing_evidence",
            "data-candidate-confidence-panel",
            "renderCandidateConfidencePanel",
            "参考可信度",
            "验证状态",
            "缺失证据",
            "可信依据",
            "最值得补的数据缺口",
        ):
            self.assertIn(token, script)
        self.assertIn("candidateConfidenceHomePanel", template)
        self.assertIn("candidateConfidencePanel", template)
        self.assertIn("data-candidate-confidence-panel", template)

    def test_daily_workflow_report_includes_reference_confidence_section(self) -> None:
        from quantdesk.daily_workflow_orchestrator import (
            _attach_reference_confidence_to_candidates,
            build_daily_workflow_report,
        )

        candidates = _attach_reference_confidence_to_candidates(
            [
                {
                    "symbol": "999998",
                    "name": "样例",
                    "strategy_type": "board",
                    "strategy_label": "打板",
                    "latest_close": 8.76,
                    "amount_ma20": 100_000_000,
                    "sector": "银行",
                }
            ],
            target_date="2026-06-26",
        )
        self.assertIn("data_confidence", candidates[0])
        self.assertIn("verification_status", candidates[0])
        self.assertIn("missing_evidence", candidates[0])

        report = build_daily_workflow_report(
            {
                "workflow_status": "completed",
                "summary": {
                    "long_candidate_count": 0,
                    "board_candidate_count": 1,
                    "target_complete_trade_date": "2026-06-26",
                },
                "auto_strategy": {"standard_candidates": candidates},
                "steps": [],
                "disclaimer": "仅用于策略学习与本地纸面交易模拟验证，不构成投资建议。",
            }
        )

        for token in (
            "## 参考可信度分层",
            "参考可信度",
            "验证状态",
            "缺失证据",
            "为什么可信 / 哪里降级",
            "missing_1m_data",
        ):
            self.assertIn(token, report)


if __name__ == "__main__":
    unittest.main()
