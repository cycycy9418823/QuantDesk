from __future__ import annotations

import unittest
from pathlib import Path

from quantdesk.data_health_dashboard import build_data_health_dashboard

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BeginnerDataHealthDashboardTests(unittest.TestCase):
    def _payload(self, *, daily_stale: bool = False, intraday_missing: bool = True):
        latest = "2026-05-14" if daily_stale else "2026-05-15"
        target = "2026-05-15"
        return build_data_health_dashboard(
            daily_price_payload={
                "target_date": target,
                "latest_local_date": latest,
                "max_local_date": latest,
                "total_symbols": 80,
                "stale_count": 80 if daily_stale else 0,
                "missing_count": 0,
                "is_stale": daily_stale,
            },
            freshness_payload={
                "target_complete_trade_date": target,
                "local_daily_latest_date": latest,
                "daily_feature_date": latest,
                "is_daily_stale": daily_stale,
                "portfolio_review_valid": not daily_stale,
            },
            intraday_payload={
                "target_trade_date": target,
                "total_symbols": 5,
                "available_count": 0 if intraday_missing else 5,
                "missing_count": 5 if intraday_missing else 0,
            },
            theme_payload={
                "exists": True,
                "total": 80,
                "mapped_count": 50,
                "unknown_count": 30,
                "failed_reason": "",
            },
            current_candidate_theme_payload={
                "candidate_count": 3,
                "mapped_current_candidates": 2,
                "unknown_current_candidates": 1,
            },
            provider_capability_payload={"summary": {"ok_count": 0}},
            portfolio_payload={"exists": True, "positions": [{"symbol": "600744"}]},
            daily_workflow_payload={"summary": {"workflow_status": "partial"}},
            real_sample_payload={"summary": {"missing_intraday_count": 1}},
            self_validation_payload={"summary": {"passed": 9}},
            strategy_payload={
                "long_candidates": [
                    {"symbol": "600744", "name": "华银电力", "strategy_type": "long", "rank": 1, "score": 90, "reason": "样例观察"},
                    {"symbol": "002115", "name": "三维通信", "strategy_type": "board", "rank": 2, "score": 88, "reason": "样例观察"},
                    {"symbol": "300323", "name": "华灿光电", "strategy_type": "long", "rank": 3, "score": 80, "reason": "样例观察"},
                ]
            },
            theme_map={
                "600744": {"symbol": "600744", "sector": "公用事业", "primary_theme": "电力"},
                "002115": {"symbol": "002115", "sector": "通信", "primary_theme": "5G"},
            },
        )

    def test_dashboard_aggregates_existing_status(self):
        payload = self._payload()
        self.assertTrue(payload["ok"])
        self.assertIn("daily_data", payload["cards"])
        self.assertIn("intraday_data", payload["cards"])
        self.assertIn("theme_map", payload["cards"])
        self.assertIn("portfolio_review", payload["cards"])
        self.assertEqual(len(payload["top_observation_candidates"]), 3)

    def test_dashboard_exposes_extended_data_operator_policy(self):
        payload = build_data_health_dashboard(
            daily_price_payload={
                "target_date": "2026-07-03",
                "latest_local_date": "2026-07-03",
                "max_local_date": "2026-07-03",
                "total_symbols": 5,
                "stale_count": 0,
                "missing_count": 0,
                "is_stale": False,
            },
            freshness_payload={
                "target_complete_trade_date": "2026-07-03",
                "local_daily_latest_date": "2026-07-03",
                "daily_feature_date": "2026-07-03",
                "is_daily_stale": False,
                "portfolio_review_valid": True,
            },
            intraday_payload={"target_trade_date": "2026-07-03", "total_symbols": 0, "available_count": 0, "missing_count": 0},
            theme_payload={"exists": True, "total": 0, "mapped_count": 0, "unknown_count": 0},
            current_candidate_theme_payload={"candidate_count": 0},
            provider_capability_payload={"summary": {"ok_count": 0}},
            portfolio_payload={"exists": True, "positions": []},
            daily_workflow_payload={"summary": {"workflow_status": "ok"}},
            real_sample_payload={},
            self_validation_payload={},
            strategy_payload={"long_candidates": []},
            theme_map={},
            extended_gates_payload={
                "task_type": "extended_data_gates",
                "status": "warning",
                "generated_at": "2026-07-05T21:49:18",
                "current_gate": "download_queue",
                "suggested_next_action": "continue_monitoring",
                "operator_wait_reason": "download_queue_active",
                "operator_action_policy": {
                    "safe_next_commands": ["python scripts/qd.py tushare-queue-status --summary true"],
                    "blocked_without_confirmation": ["python scripts/qd.py import-extended-data --apply true"],
                    "requires_user_confirmation": False,
                },
                "gate_chain": {
                    "queue": {
                        "ok_count": 12,
                        "fail_count": 1,
                        "traceback_count": 0,
                        "queue_liveness": "active",
                        "progress_state": "advancing",
                        "stage_position_label": "6/7",
                        "stage_remaining_keys": ["harvest_moneyflow"],
                    },
                    "minute": {
                        "status": "running_with_warnings",
                        "symbol_done_count": 50,
                        "symbol_count": 2656,
                        "target_symbol_count": 2656,
                        "last_symbol": "000967",
                        "traceback_count": 1,
                        "current_available_count": 51,
                        "current_available_ratio": 0.019202,
                        "canonical_current_available_count": 50,
                        "staged_current_not_imported_count": 1,
                        "missing_or_stale_count": 2605,
                        "latest_completed_job_symbol_count": 200,
                        "latest_completed_job_canonical_current_count": 200,
                        "latest_completed_quality_status": "passed",
                        "latest_completed_quality_total_symbols": 200,
                        "latest_completed_quality_usable_count": 200,
                        "suggested_next_action": "continue_monitoring_minute_download",
                    },
                    "import_audit": {
                        "preapply_gate": "wait_for_download_completion",
                        "domain_counts": {"fundamentals": 20},
                        "source_scan_limit_applied": True,
                        "source_scan_limited_domains": ["fundamentals"],
                        "max_files_per_rule": 20,
                        "full_audit_command": "python scripts/qd.py audit-extended-import --summary true --max-files-per-rule 0",
                    },
                    "import_verification": {"post_import_gate": "wait_for_download_completion"},
                },
            },
        )

        self.assertEqual(payload["extended_data_current_gate"], "download_queue")
        self.assertEqual(payload["extended_data_gates_snapshot_generated_at"], "2026-07-05T21:49:18")
        self.assertIsInstance(payload["extended_data_gates_snapshot_age_seconds"], float)
        self.assertGreater(payload["extended_data_gates_snapshot_age_seconds"], 0)
        self.assertEqual(payload["extended_import_max_files_per_rule"], 20)
        self.assertEqual(payload["extended_data_gates_snapshot_path"], "results/data_health/extended_data_gates_latest.json")
        self.assertTrue(payload["extended_data_gates_snapshot_exists"])
        self.assertEqual(payload["extended_data_queue_stage_progress"], "6/7")
        self.assertEqual(payload["tushare_minute_status"], "running_with_warnings")
        self.assertEqual(payload["tushare_minute_symbol_done_count"], 50)
        self.assertEqual(payload["tushare_minute_symbol_count"], 2656)
        self.assertEqual(payload["tushare_minute_target_symbol_count"], 2656)
        self.assertEqual(payload["tushare_minute_current_available_count"], 51)
        self.assertEqual(payload["tushare_minute_current_available_ratio"], 0.019202)
        self.assertEqual(payload["tushare_minute_canonical_current_available_count"], 50)
        self.assertEqual(payload["tushare_minute_staged_current_not_imported_count"], 1)
        self.assertEqual(payload["tushare_minute_traceback_count"], 1)
        self.assertEqual(payload["tushare_minute_suggested_next_action"], "continue_monitoring_minute_download")
        self.assertEqual(payload["tushare_minute_coverage_suggested_next_action"], "continue_monitoring_minute_download")
        self.assertEqual(payload["tushare_minute_latest_completed_job_symbol_count"], 200)
        self.assertEqual(payload["tushare_minute_latest_completed_job_canonical_current_count"], 200)
        self.assertEqual(payload["extended_import_preapply_gate"], "wait_for_download_completion")
        self.assertEqual(payload["extended_import_domain_counts"], {"fundamentals": 20})
        self.assertIn("python scripts/qd.py tushare-queue-status --summary true", payload["extended_data_safe_next_commands"])
        self.assertIn("python scripts/qd.py import-extended-data --apply true", payload["extended_data_blocked_without_confirmation"])
        capability = payload["data_capability_summary"]
        self.assertTrue(capability["research_only"])
        self.assertTrue(capability["paper_only"])
        self.assertFalse(capability["canonical_writes_allowed"])
        self.assertIn("日线收益复盘", [item["label"] for item in capability["available_now"]])
        self.assertIn("只读下载监控", [item["label"] for item in capability["available_now"]])
        self.assertIn("分钟扩展池", [item["label"] for item in capability["waiting_on"]])
        self.assertIn("扩展数据下载队列", [item["label"] for item in capability["waiting_on"]])
        self.assertIn("暂存扩展导入", [item["label"] for item in capability["waiting_on"]])
        self.assertIn("导入 apply", [item["label"] for item in capability["currently_forbidden"]])
        self.assertIn("正式市场数据写入", [item["label"] for item in capability["currently_forbidden"]])
        self.assertIn("现在可用", capability["operator_summary_text"])
        self.assertIn("等待下载", capability["operator_summary_text"])
        self.assertIn("当前禁止", capability["operator_summary_text"])

    def test_daily_complete_intraday_missing_is_yellow(self):
        payload = self._payload(daily_stale=False, intraday_missing=True)
        self.assertEqual(payload["market_health_level"], "yellow")
        self.assertEqual(payload["cards"]["daily_data"]["status"], "green")
        self.assertEqual(payload["cards"]["intraday_data"]["status"], "yellow")

    def test_intraday_stale_next_plan_date_is_not_reported_as_missing_zero(self):
        payload = build_data_health_dashboard(
            daily_price_payload={
                "target_date": "2026-06-23",
                "latest_local_date": "2026-06-23",
                "max_local_date": "2026-06-23",
                "total_symbols": 5,
                "stale_count": 0,
                "missing_count": 0,
                "is_stale": False,
            },
            freshness_payload={
                "target_complete_trade_date": "2026-06-23",
                "local_daily_latest_date": "2026-06-23",
                "daily_feature_date": "2026-06-23",
                "is_daily_stale": False,
                "portfolio_review_valid": True,
            },
            intraday_payload={
                "target_trade_date": "2026-06-24",
                "total_symbols": 5,
                "available_count": 0,
                "missing_count": 0,
                "stale_count": 5,
                "items": [
                    {"symbol": "600000", "latest_available_date": "2026-06-23", "status": "stale_intraday"},
                    {"symbol": "002115", "latest_available_date": "2026-06-23", "status": "stale_intraday"},
                ],
            },
            theme_payload={"exists": True, "total": 5, "mapped_count": 5, "unknown_count": 0},
            current_candidate_theme_payload={
                "candidate_count": 2,
                "mapped_current_candidates": 2,
                "unknown_current_candidates": 0,
            },
            provider_capability_payload={"summary": {"ok_count": 1}},
            portfolio_payload={"exists": True, "positions": []},
            daily_workflow_payload={"summary": {"workflow_status": "ok"}},
            real_sample_payload={},
            self_validation_payload={},
            strategy_payload={"long_candidates": []},
            theme_map={},
        )
        card = payload["cards"]["intraday_data"]
        self.assertEqual(card["status"], "yellow")
        self.assertIn("下一计划日尚未可验证", card["value"])
        self.assertIn("2026-06-23", card["detail"])
        self.assertIn("2026-06-24", card["detail"])
        self.assertNotIn("缺少 0 只", card["detail"])
        self.assertNotIn("缺少历史分钟线", card["value"])
        self.assertIn("下一计划日尚未可验证", payload["summary_text"])

    def test_verified_minute_pool_is_visible_even_while_large_pool_downloads(self):
        payload = build_data_health_dashboard(
            daily_price_payload={
                "target_date": "2026-07-03",
                "latest_local_date": "2026-07-03",
                "max_local_date": "2026-07-03",
                "total_symbols": 5,
                "stale_count": 0,
                "missing_count": 0,
                "is_stale": False,
            },
            freshness_payload={
                "target_complete_trade_date": "2026-07-03",
                "local_daily_latest_date": "2026-07-03",
                "daily_feature_date": "2026-07-03",
                "is_daily_stale": False,
                "portfolio_review_valid": True,
            },
            intraday_payload={
                "target_trade_date": "2026-07-03",
                "total_symbols": 5,
                "available_count": 2,
                "missing_count": 3,
                "verified_minute_pool_available_count": 2,
                "verified_minute_pool_missing_count": 3,
                "verified_minute_pool": {
                    "status": "passed",
                    "usable_count": 200,
                    "total_symbols": 200,
                    "target_date": "2026-07-03",
                    "source_snapshot": "last_passed",
                    "latest_quality_status": "partial",
                    "latest_snapshot_path": "results/data_health/tushare_minute_quality_latest.json",
                    "warnings": ["verified_pool_from_last_passed_snapshot"],
                },
            },
            theme_payload={"exists": True, "total": 5, "mapped_count": 5, "unknown_count": 0},
            current_candidate_theme_payload={
                "candidate_count": 2,
                "mapped_current_candidates": 2,
                "unknown_current_candidates": 0,
            },
            provider_capability_payload={"summary": {"ok_count": 1}},
            portfolio_payload={"exists": True, "positions": []},
            daily_workflow_payload={"summary": {"workflow_status": "ok"}},
            real_sample_payload={},
            self_validation_payload={},
            strategy_payload={"long_candidates": []},
            theme_map={},
        )

        card = payload["cards"]["intraday_data"]
        self.assertEqual(card["status"], "yellow")
        self.assertEqual(card["value"], "已验证分钟池可先用于覆盖内股票")
        self.assertEqual(card["verified_minute_pool"]["status"], "passed")
        self.assertEqual(card["verified_minute_pool"]["usable_count"], 200)
        self.assertEqual(card["verified_minute_pool"]["source_snapshot"], "last_passed")
        self.assertEqual(card["verified_minute_pool"]["latest_quality_status"], "partial")
        self.assertIn("200/200", card["detail"])
        self.assertIn("最近一次通过质检的快照", card["detail"])
        self.assertIn("最新质检状态 部分完成", card["detail"])
        self.assertNotIn("last_passed", card["detail"])
        self.assertNotIn("latest partial", card["detail"])
        self.assertIn("缺分钟线降级", card["detail"])
        self.assertNotIn("missing_intraday", card["detail"])
        self.assertIn("已验证分钟池可先用于覆盖内股票", payload["summary_text"])
        self.assertIn("未覆盖股票仍按缺分钟线降级", payload["summary_text"])
        self.assertNotIn("历史分钟线缺失，当前不能验证盘中触发", payload["summary_text"])

    def test_intraday_card_separates_local_minutes_from_verified_pool_hits(self):
        payload = build_data_health_dashboard(
            daily_price_payload={
                "target_date": "2026-07-03",
                "latest_local_date": "2026-07-03",
                "max_local_date": "2026-07-03",
                "total_symbols": 5,
                "stale_count": 0,
                "missing_count": 0,
                "is_stale": False,
            },
            freshness_payload={
                "target_complete_trade_date": "2026-07-03",
                "local_daily_latest_date": "2026-07-03",
                "daily_feature_date": "2026-07-03",
                "is_daily_stale": False,
                "portfolio_review_valid": True,
            },
            intraday_payload={
                "target_trade_date": "2026-07-03",
                "total_symbols": 5,
                "available_count": 5,
                "missing_count": 0,
                "verified_minute_pool_available_count": 0,
                "verified_minute_pool_missing_count": 5,
                "verified_minute_pool": {
                    "status": "passed",
                    "usable_count": 200,
                    "total_symbols": 200,
                    "target_date": "2026-07-03",
                },
            },
            theme_payload={"exists": True, "total": 5, "mapped_count": 5, "unknown_count": 0},
            current_candidate_theme_payload={"candidate_count": 0},
            provider_capability_payload={"summary": {"ok_count": 1}},
            portfolio_payload={"exists": True, "positions": []},
            daily_workflow_payload={"summary": {"workflow_status": "ok"}},
            real_sample_payload={},
            self_validation_payload={},
            strategy_payload={"long_candidates": []},
            theme_map={},
        )

        card = payload["cards"]["intraday_data"]
        self.assertEqual(card["status"], "green")
        self.assertEqual(card["value"], "本地分钟线可用；已验证池待命中")
        self.assertEqual(card["local_available_count"], 5)
        self.assertEqual(card["local_missing_count"], 0)
        self.assertIn("当前计划命中已验证池 0/5", card["detail"])
        self.assertIn("200/200", card["detail"])

    def test_daily_stale_is_red(self):
        payload = self._payload(daily_stale=True, intraday_missing=True)
        self.assertEqual(payload["health_level"], "red")
        self.assertEqual(payload["cards"]["daily_data"]["status"], "red")
        self.assertIn("先不要急着看策略结果", "\n".join(payload["beginner_next_steps"]))

    def test_beginner_next_steps_and_risk_disclaimer(self):
        payload = self._payload()
        self.assertTrue(payload["beginner_next_steps"])
        self.assertIn("不构成投资建议", payload["risk_disclaimer"])

    def test_get_data_health_dashboard_is_read_only(self):
        server_text = (PROJECT_ROOT / "scripts" / "quant_app_server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/data-health/dashboard"', server_text)
        self.assertIn("build_data_health_dashboard(write=False)", server_text)
        self.assertIn('path == "/api/data-health/rebuild"', server_text)

    def test_ui_contains_beginner_home_sections(self):
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        for token in ["数据健康状态", "今日操作指南", "今日 Top3 观察候选", "不构成投资建议"]:
            self.assertIn(token, html)

    def test_app_js_beginner_expert_mode_and_local_storage(self):
        app_js = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        for token in ["小白模式", "专家模式", "localStorage", "enabled = saved === null ? true"]:
            self.assertIn(token, app_js)

    def test_main_dashboard_home_surfaces_verified_minute_pool(self):
        script = (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")
        self.assertIn("verified_minute_pool", script)
        self.assertIn("分钟池", script)
        self.assertIn("已完成的历史验证子池可用于相应日期的纸面回放", script)
        self.assertIn("缺分钟线时不会用日线假装成交", script)
        self.assertIn("source_snapshot", script)
        self.assertIn("latest_quality_status", script)
        self.assertIn("last_passed", script)
        self.assertIn("最近一次通过质检的分钟池快照", script)
        self.assertNotIn("来源 last_passed 兜底快照", script)
        self.assertIn("missing_intraday", script)
        self.assertNotIn("仍按 missing_intraday 降级", script)
        self.assertNotIn("local-unverified 或 missing_intraday", script)
        self.assertIn("仍按缺分钟线降级", script)
        self.assertIn("paperMinuteGateText", script)
        self.assertIn("分钟池质量", script)
        self.assertIn("分钟线来源", script)
        self.assertIn("verifiedCompletedIntradayCount", script)
        self.assertIn("localUnverifiedIntradayCount", script)
        self.assertIn("missingIntradayCount", script)
        self.assertIn("下载队列", script)
        self.assertIn("分钟扩展池", script)
        self.assertIn("running_with_warnings", script)
        self.assertIn("tushare_minute_traceback_count", script)
        self.assertIn("tushare_minute_current_available_ratio", script)
        self.assertIn("tushare_minute_target_symbol_count", script)
        self.assertIn("tushare_minute_latest_completed_job_canonical_current_count", script)
        self.assertIn("完成约", script)
        self.assertIn("Top200 固化覆盖", script)
        self.assertIn("不自动重启", script)
        self.assertIn("tushare_minute_coverage_suggested_next_action", script)
        self.assertIn("导入门禁", script)
        self.assertIn("extended_data_queue_stage_progress", script)
        self.assertIn("extended_import_preapply_gate", script)
        self.assertIn("未完成前只做只读监控，不导入 staged 数据", script)
        self.assertIn("local_missing_count", script)
        self.assertIn("验证池未命中", script)
        self.assertIn("暂存扩展数据", script)
        self.assertIn("extended_import_domain_counts", script)
        self.assertIn("bounded 暂存计数", script)
        self.assertIn("audit-extended-import", script)
        self.assertIn("安全下一步", script)
        self.assertIn("extended_data_safe_next_commands", script)
        self.assertIn("extended_data_blocked_without_confirmation", script)
        self.assertIn("禁止未确认执行", script)
        self.assertIn("门禁快照", script)
        self.assertIn("extended_data_gates_snapshot_generated_at", script)
        self.assertIn("extended_data_gates_snapshot_path", script)
        self.assertIn("extended_data_gates_snapshot_age_seconds", script)
        self.assertIn("extended_import_max_files_per_rule", script)
        self.assertIn("extended_import_full_audit_command", script)
        self.assertIn("可能已卡住", script)
        self.assertIn("是否重启由你决定", script)
        self.assertIn("extended_data_requires_user_confirmation", script)
        self.assertIn("extended_data_manual_review_required", script)
        self.assertIn("card-tone-warn", script)
        self.assertIn("proxy:", script)
        self.assertIn("degraded:", script)
        self.assertIn("data_capability_summary", script)
        self.assertIn("现在可用", script)
        self.assertIn("等待下载", script)
        self.assertIn("当前禁止", script)

    def test_main_dashboard_home_stale_alert_style_exists(self):
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(encoding="utf-8")
        self.assertIn("card-tone-warn", html)

    def test_ui_localizes_internal_values(self):
        app_js = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        for token in ["缺少分钟线，无法验证盘中触发", "计划未触发", "仅日线复盘"]:
            self.assertIn(token, app_js)

    def test_css_prevents_long_text_overflow(self):
        css = (PROJECT_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("word-break: break-word", css)
        self.assertIn("max-width: 100%", css)


if __name__ == "__main__":
    unittest.main()
