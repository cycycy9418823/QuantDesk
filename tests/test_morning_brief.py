from __future__ import annotations

import inspect
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk.morning_brief import (
    DISCLAIMER,
    _build_freshness_lag,
    build_morning_brief,
    load_morning_brief_snapshot,
    render_morning_brief_markdown,
    run_morning_brief,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _directory_symlink(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")


def _file_symlink(link: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")


def _set_daily_change(paths: dict[str, Path], value: float) -> None:
    workflow = json.loads(paths["workflow"].read_text(encoding="utf-8"))
    workflow["auto_paper_portfolio"]["summary"]["daily_pnl"] = value
    _write_json(paths["workflow"], workflow)


def _seed_root(root: Path, *, target_date: str = "2026-07-31") -> dict[str, Path]:
    run_id = "daily-data-chain-20260731T103005Z-fixture"
    manifest = {
        "run_id": run_id,
        "status": "blocked",
        "target_date": target_date,
        "generated_at": f"{target_date}T19:00:00+08:00",
        "published_latest": True,
        "retry_symbols": ["920685"],
        "warnings": ["daily_symbols_stale"],
        "coverage": {
            "daily": {
                "requested_count": 914,
                "fresh_count": 913,
                "stale_count": 1,
                "status": "blocked",
            },
            "current_analysis_pool": {
                "requested_count": 914,
                "effective_complete_count": 914,
                "missing_count": 0,
                "status": "passed",
            },
        },
    }
    paths = {
        "lock": root / "state/daily_data_chain.lock.json",
        "latest": root / "results/data_chain/daily_data_chain_latest.json",
        "archive": root / f"results/data_chain/runs/{run_id}/manifest.json",
        "workflow": root / "results/daily_workflow_latest.json",
        "health": root / "results/data_health/data_health_dashboard_latest.json",
        "market": root
        / "results/market_intelligence/market_intelligence_latest.json",
        "risk": root / "results/risk_alerts_latest.json",
        "readiness": root
        / "results/data_health/round7_forward_pit_readiness_latest.json",
        "stage4": root
        / "results/data_health/stage4_maintenance_evidence_latest.json",
    }
    _write_json(
        paths["lock"],
        {"run_id": run_id, "pid": 0, "status": "released", "released_at": "x"},
    )
    _write_json(paths["latest"], manifest)
    _write_json(paths["archive"], manifest)
    _write_json(
        paths["workflow"],
        {
            "status": "warning",
            "generated_at": f"{target_date}T19:15:00+08:00",
            "dates": {
                "local_complete_daily_date": target_date,
                "portfolio_valuation_date": target_date,
            },
            "auto_strategy": {
                "generated_at": f"{target_date}T19:12:00+08:00",
                "long_candidates": [
                    {
                        "symbol": "600000",
                        "name": "浦发银行",
                        "strategy_type": "long",
                        "sector": "银行",
                        "reason": "本地研究观察",
                        "risk_notes": "仅供复盘",
                        "planned_action": "POISON_ORDER",
                        "target_price": 99,
                    }
                ],
                "board_candidates": [],
                "pending_orders": [{"symbol": "POISON_PENDING_ORDER"}],
            },
            "auto_paper_portfolio": {
                "generated_at": f"{target_date}T19:13:00+08:00",
                "broker_boundary": "不接券商，不真实下单，不保存账户密码。",
                "summary": {
                    "total_equity": 98629.07,
                    "previous_total_equity": 98633.05,
                    "daily_pnl": -3.98,
                    "daily_return_pct": 0.0,
                    "position_count": 5,
                },
            },
        },
    )
    _write_json(
        paths["health"],
        {
            "status": "warning",
            "target_date": target_date,
            "generated_at": f"{target_date}T19:02:00+08:00",
            "data_health_summary": {"status": "warning"},
        },
    )
    _write_json(
        paths["market"],
        {
            "status": "warning",
            "target_date": target_date,
            "generated_at": f"{target_date}T19:02:00+08:00",
            "market_intelligence_summary": {"market_regime": "SIDEWAYS"},
        },
    )
    _write_json(
        paths["risk"],
        {
            "status": "passed",
            "generated_at": f"{target_date}T19:14:00+08:00",
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
            "alerts": [
                {
                    "severity": "warning",
                    "alert_type": "data_quality",
                    "symbol": "600000",
                    "message": "本地行情存在缺口",
                    "source": "data_health",
                    "planned_action": "POISON_ACTION",
                }
            ],
        },
    )
    _write_json(
        paths["readiness"],
        {
            "status": "t0_launched",
            "generated_at": f"{target_date}T19:03:00+08:00",
            "t0": "2026-07-27",
            "eligible_forward_days": [
                "2026-07-27",
                "2026-07-28",
                "2026-07-29",
                "2026-07-30",
                target_date,
            ],
            "milestones": [
                {"label": "T0+8", "date": "2026-08-06", "status": "pending"}
            ],
        },
    )
    _write_json(
        paths["stage4"],
        {
            "status": "maintenance_window_incomplete",
            "as_of": target_date,
            "reason_codes": [
                "shadow_daily_count_gap",
                "qualification_gate_hash_mismatch",
            ],
            "account_replay": "not_authorized",
            "conclusion_drawn": False,
        },
    )
    return paths


def _seed_freshness_calendar(root: Path, dates: list[str]) -> None:
    calendar = root / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True, exist_ok=True)
    calendar.write_text(
        "date,is_open\n" + "".join(f"{date},1\n" for date in dates),
        encoding="utf-8",
    )


def test_freshness_uses_latest_wide_daily_contract_not_after_as_of(
    tmp_path: Path,
) -> None:
    _seed_freshness_calendar(
        tmp_path, ["2026-08-07", "2026-08-10", "2026-08-11"]
    )
    daily = tmp_path / "data/daily"
    daily.mkdir(parents=True)
    (daily / "600000.csv").write_text(
        "date,symbol,close\n2026-08-10,600000,10\n", encoding="utf-8"
    )
    contracts = tmp_path / "data/universe"
    _write_json(
        contracts / "weekly_daily_wide_pool_contract_2026-08-07.json",
        {"target_date": "2026-08-07", "symbols": ["600000"]},
    )
    _write_json(
        contracts / "weekly_daily_wide_pool_contract_2026-08-12.json",
        {"target_date": "2026-08-12", "symbols": ["999999"]},
    )

    freshness = _build_freshness_lag(tmp_path, "2026-08-11", {})

    assert freshness["wide_daily"] == 1
    assert freshness["wide_daily_contract_as_of"] == "2026-08-07"
    assert freshness["wide_daily_contract_lag"] == 2


def test_freshness_without_eligible_wide_daily_contract_stays_unavailable(
    tmp_path: Path,
) -> None:
    _seed_freshness_calendar(tmp_path, ["2026-08-11"])
    _write_json(
        tmp_path
        / "data/universe/weekly_daily_wide_pool_contract_2026-08-12.json",
        {"target_date": "2026-08-12", "symbols": ["600000"]},
    )

    freshness = _build_freshness_lag(tmp_path, "2026-08-11", {})

    assert freshness["wide_daily"] is None
    assert freshness["wide_daily_contract_as_of"] == ""
    assert freshness["wide_daily_contract_lag"] is None


def test_running_chain_never_reads_mutable_latest(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    _write_json(
        paths["lock"],
        {"run_id": "running-run", "pid": 123, "status": "running"},
    )
    paths["latest"].write_text("POISON_FROM_MUTABLE_LATEST", encoding="utf-8")

    snapshot = load_morning_brief_snapshot(tmp_path)

    assert snapshot["source_mode"] == "archived_terminal_run"
    assert snapshot["status"] == "degraded"
    assert "POISON_FROM_MUTABLE_LATEST" not in repr(snapshot)
    assert snapshot["chain"]["run_id"].endswith("fixture")


def test_running_chain_rejects_archive_directory_symlink_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    paths = _seed_root(root)
    archive_payload = json.loads(paths["archive"].read_text(encoding="utf-8"))
    paths["archive"].unlink()
    paths["archive"].parent.rmdir()
    paths["archive"].parent.parent.rmdir()
    outside = tmp_path / "outside-runs"
    outside_manifest = outside / archive_payload["run_id"] / "manifest.json"
    _write_json(outside_manifest, archive_payload)
    _directory_symlink(root / "results/data_chain/runs", outside)
    _write_json(
        paths["lock"],
        {"run_id": "running-run", "pid": 123, "status": "running"},
    )

    snapshot = load_morning_brief_snapshot(root)

    assert snapshot["status"] == "blocked"
    assert snapshot["reason_codes"] == ["chain_runs_path_escape"]


def test_lock_change_during_read_discards_batch(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    states = [
        {
            "run_id": "daily-data-chain-20260731T103005Z-fixture",
            "pid": 0,
            "status": "released",
        },
        {"run_id": "new-running-run", "pid": 22, "status": "running"},
    ]

    def changing_reader(_path: Path) -> dict:
        return states.pop(0)

    snapshot = load_morning_brief_snapshot(
        tmp_path, lock_reader=changing_reader
    )

    assert snapshot["status"] == "blocked"
    assert "snapshot_changed_during_read" in snapshot["reason_codes"]
    assert snapshot["chain"] == {}


def test_mismatched_dates_hide_candidates_account_and_risk(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    workflow = json.loads(paths["workflow"].read_text(encoding="utf-8"))
    workflow["dates"]["local_complete_daily_date"] = "2026-07-30"
    workflow["dates"]["portfolio_valuation_date"] = "2026-07-30"
    _write_json(paths["workflow"], workflow)

    payload = build_morning_brief(tmp_path)

    assert payload["candidates"] == []
    assert payload["paper_account"] == {}
    assert payload["risk_items"] == []
    assert "artifact_scope_mismatch" in payload["reason_codes"]
    # The umbrella code alone cannot say which artifact slipped, so the specific
    # codes and the observed day per artifact must both be present.
    assert "workflow_scope_mismatch" in payload["reason_codes"]
    assert "paper_account_scope_mismatch" in payload["reason_codes"]
    assert payload["artifact_scope"]["as_of"] == "2026-07-31"
    assert payload["artifact_scope"]["workflow"] == "2026-07-30"
    assert payload["artifact_scope"]["paper_account"] == "2026-07-30"


def test_stage4_milestone_accepts_readiness_object_day_rows(
    tmp_path: Path,
) -> None:
    paths = _seed_root(tmp_path)
    readiness = json.loads(paths["readiness"].read_text(encoding="utf-8"))
    readiness["eligible_forward_days"] = [
        {"date": value, "eligible": True}
        for value in readiness["eligible_forward_days"]
    ]
    _write_json(paths["readiness"], readiness)

    payload = build_morning_brief(tmp_path)

    assert payload["milestone"]["t0"] == "2026-07-27"
    assert "milestone_scope_mismatch" not in payload["reason_codes"]


def test_stage4_milestone_rejects_object_day_row_without_date(
    tmp_path: Path,
) -> None:
    paths = _seed_root(tmp_path)
    readiness = json.loads(paths["readiness"].read_text(encoding="utf-8"))
    readiness["eligible_forward_days"] = [{"eligible": True}]
    _write_json(paths["readiness"], readiness)

    payload = build_morning_brief(tmp_path)

    assert payload["milestone"] == {}
    assert "milestone_scope_mismatch" in payload["reason_codes"]


def test_invalid_paper_boundary_hides_account_values(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    workflow = json.loads(paths["workflow"].read_text(encoding="utf-8"))
    workflow["auto_paper_portfolio"]["broker_boundary"] = "broker connected"
    _write_json(paths["workflow"], workflow)

    payload = build_morning_brief(tmp_path)

    assert payload["paper_account"] == {}
    assert "paper_boundary_invalid" in payload["reason_codes"]


def test_corrupt_optional_artifact_degrades_without_fabricated_zero(
    tmp_path: Path,
) -> None:
    paths = _seed_root(tmp_path)
    paths["risk"].write_text("{broken", encoding="utf-8")

    payload = build_morning_brief(tmp_path)
    rendered = render_morning_brief_markdown(payload)

    assert payload["status"] == "warning"
    assert payload["risk_items"] == []
    assert "risk_alerts_invalid" in payload["reason_codes"]
    assert "风险信息：不可用" in rendered


def test_renderer_is_bounded_truthful_and_drops_poison_fields(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    payload = build_morning_brief(tmp_path)

    rendered = render_morning_brief_markdown(payload)

    assert "2026-07-31" in rendered
    assert "daily-data-chain-20260731T103005Z-fixture" in rendered
    assert "本地研究工件" in rendered
    assert "早报不联网抓取资讯" in rendered
    assert "http://127.0.0.1:8000/#daily-focus-section" in rendered
    assert DISCLAIMER in rendered
    assert "POISON" not in repr(payload)
    for forbidden in ("买入", "卖出", "仓位", "目标价", "止损价"):
        assert forbidden not in rendered
    assert len(rendered) <= 800


def test_candidate_total_is_reported_without_expanding_bounded_details(
    tmp_path: Path,
) -> None:
    paths = _seed_root(tmp_path)
    workflow = json.loads(paths["workflow"].read_text(encoding="utf-8"))
    workflow["auto_strategy"]["long_candidates"] = [
        {
            "symbol": f"60000{index}",
            "name": f"研究样本{index}",
            "strategy_type": "long",
            "reason": "本地研究观察",
        }
        for index in range(5)
    ]
    _write_json(paths["workflow"], workflow)

    payload = build_morning_brief(tmp_path)
    rendered = render_morning_brief_markdown(payload)

    assert payload["candidate_count"] == 5
    assert len(payload["candidates"]) == 3
    # Round 13 ④：行首仍报完整总数（5），明细另以有界摘要展开（限 3 条）。
    assert "候选复盘：5 条；" in rendered
    assert rendered.count("、") <= 2


# --------------------------------------------------------------------------- #
# T3：段落作用域匹配与空字段
#
# 2026-08-11 的真实早报里 `paper_account` 是 `{}`，同时带着一个凭空出现的
# `artifact_scope_mismatch`：宽池覆盖探测成功时，纸面账户根本没被计算。旧夹具
# 从不建 `data/daily`，宽池永远失败，于是这条分支一直没被测到。
# --------------------------------------------------------------------------- #
def _seed_wide_pool(root: Path, *, as_of: str, current: int = 2, stale: int = 1) -> None:
    daily = root / "data" / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    header = "date,symbol,close\n"
    for index in range(current):
        (daily / f"00000{index}.csv").write_text(
            header + f"{as_of},00000{index},10\n", encoding="utf-8"
        )
    for index in range(stale):
        (daily / f"10000{index}.csv").write_text(
            header + f"2026-07-01,10000{index},10\n", encoding="utf-8"
        )


def test_working_wide_pool_still_reports_paper_account(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    _seed_wide_pool(tmp_path, as_of="2026-07-31")

    payload = build_morning_brief(tmp_path)

    assert payload["wide_pool"] == {"daily_current": 2, "daily_total": 3}
    assert payload["paper_account"]["total_equity"] == 98629.07
    assert payload["paper_account"]["daily_change"] == -3.98
    assert "wide_pool_unavailable" not in payload["reason_codes"]
    assert "artifact_scope_mismatch" not in payload["reason_codes"]
    assert "paper_account_scope_mismatch" not in payload["reason_codes"]
    assert payload["artifact_scope"]["paper_account"] == "2026-07-31"


def test_off_scope_risk_and_market_are_named_individually(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    risk = json.loads(paths["risk"].read_text(encoding="utf-8"))
    risk["generated_at"] = "2026-07-29T19:14:00+08:00"
    _write_json(paths["risk"], risk)
    market = json.loads(paths["market"].read_text(encoding="utf-8"))
    market["target_date"] = "2026-07-28"
    _write_json(paths["market"], market)

    payload = build_morning_brief(tmp_path)

    assert payload["risk_items"] == []
    assert payload["market_observations"] == []
    assert "risk_alerts_scope_mismatch" in payload["reason_codes"]
    assert "market_intelligence_scope_mismatch" in payload["reason_codes"]
    assert "artifact_scope_mismatch" in payload["reason_codes"]
    assert payload["artifact_scope"]["risk_alerts"] == "2026-07-29"
    assert payload["artifact_scope"]["market_intelligence"] == "2026-07-28"


def test_in_scope_market_without_usable_regime_still_warns(tmp_path: Path) -> None:
    """空的 `market_observations` 也必须带原因码，不能静默留空。"""

    paths = _seed_root(tmp_path)
    market = json.loads(paths["market"].read_text(encoding="utf-8"))
    market["market_intelligence_summary"] = {"market_temperature": 0.5}
    _write_json(paths["market"], market)

    payload = build_morning_brief(tmp_path)

    assert payload["market_observations"] == []
    assert "market_regime_unavailable" in payload["reason_codes"]
    assert "market_intelligence_scope_mismatch" not in payload["reason_codes"]


def test_stage4_and_readiness_scope_are_reported_separately(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    stage4 = json.loads(paths["stage4"].read_text(encoding="utf-8"))
    stage4["as_of"] = "2026-07-28"
    _write_json(paths["stage4"], stage4)

    payload = build_morning_brief(tmp_path)

    assert payload["milestone"]["t0"] == "2026-07-27"
    assert payload["milestone"]["evidence_as_of"] == "2026-07-28"
    assert payload["milestone"]["current_for_as_of"] is False
    assert "milestone_scope_mismatch" in payload["reason_codes"]
    assert "stage4_maintenance_scope_mismatch" in payload["reason_codes"]
    assert "readiness_scope_mismatch" not in payload["reason_codes"]
    assert payload["artifact_scope"]["stage4_maintenance"] == "2026-07-28"
    assert payload["artifact_scope"]["readiness"] == "2026-07-31"


def test_renderer_labels_stale_milestone_with_evidence_day(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    stage4 = json.loads(paths["stage4"].read_text(encoding="utf-8"))
    stage4["as_of"] = "2026-07-28"
    _write_json(paths["stage4"], stage4)

    rendered = render_morning_brief_markdown(build_morning_brief(tmp_path))

    assert "里程碑：T0=2026-07-27" in rendered
    assert "证据日 2026-07-28" in rendered
    assert "已过期" in rendered


def test_risk_notes_list_is_rendered_as_text_not_python_repr(tmp_path: Path) -> None:
    """真实候选行把 `risk_notes` 存成列表，直接 str() 会输出 Python repr。"""

    paths = _seed_root(tmp_path)
    workflow = json.loads(paths["workflow"].read_text(encoding="utf-8"))
    workflow["auto_strategy"]["long_candidates"][0]["risk_notes"] = [
        "仅用于策略学习与模拟验证。",
        "本地研究观察",
    ]
    _write_json(paths["workflow"], workflow)

    payload = build_morning_brief(tmp_path)

    notes = payload["candidates"][0]["risk_notes"]
    assert notes == "仅用于策略学习与模拟验证。；本地研究观察"
    assert "['" not in notes


def test_zero_candidates_in_scope_is_not_reported_as_unavailable(
    tmp_path: Path,
) -> None:
    paths = _seed_root(tmp_path)
    workflow = json.loads(paths["workflow"].read_text(encoding="utf-8"))
    workflow["auto_strategy"]["long_candidates"] = []
    _write_json(paths["workflow"], workflow)

    payload = build_morning_brief(tmp_path)

    assert payload["candidate_count"] == 0
    assert "候选复盘：0 条（今日无候选）" in render_morning_brief_markdown(payload)

    workflow["dates"]["local_complete_daily_date"] = "2026-07-30"
    _write_json(paths["workflow"], workflow)
    off_scope = build_morning_brief(tmp_path)

    assert "候选复盘：不可用" in render_morning_brief_markdown(off_scope)


def test_dry_run_writes_preview_but_not_delivery_state(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    calls: list[dict] = []

    result = run_morning_brief(
        tmp_path,
        network=False,
        push=False,
        dry_run=True,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        sender=lambda **kwargs: calls.append(kwargs) or {"status": "sent"},
    )

    assert result["status"] == "preview_generated"
    assert result["external_sent_count"] == 0
    assert calls == []
    assert (tmp_path / "results/morning_brief/morning_brief_latest.json").exists()
    assert (tmp_path / "reports/morning_brief/morning_brief_latest.md").exists()
    assert not (tmp_path / "state/morning_brief_delivery.json").exists()
    assert not (tmp_path / "results/notifications").exists()


def test_preview_output_directory_symlink_escape_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_root(root)
    outside = tmp_path / "outside-preview"
    _directory_symlink(root / "results/morning_brief", outside)

    result = run_morning_brief(
        root,
        network=False,
        push=False,
        dry_run=True,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "output_path_escape"
    assert list(outside.iterdir()) == []


def test_success_commits_channel_scoped_dedupe_and_safe_receipt(
    tmp_path: Path,
) -> None:
    _seed_root(tmp_path)
    loader_calls: list[str] = []

    def loader() -> str:
        loader_calls.append("called")
        return "SCT_FAKE_SECRET"

    result = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=loader,
        sender=lambda **kwargs: {
            "channel": "server_chan",
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        },
    )

    state = json.loads(
        (tmp_path / "state/morning_brief_delivery.json").read_text(encoding="utf-8")
    )
    receipts = list((tmp_path / "results/notifications").glob("*.json"))
    receipt_text = receipts[0].read_text(encoding="utf-8")
    assert result["status"] == "sent"
    assert result["external_sent_count"] == 1
    assert loader_calls == ["called"]
    delivery = state["channels"]["server_chan"]["deliveries"]["2026-08-03"][
        result["content_sha256"]
    ]
    assert delivery["status"] == "sent"
    assert len(receipts) == 1
    assert "summary_sha256" in receipt_text
    assert '"delivery_day": "2026-08-03"' in receipt_text
    assert "SCT_FAKE_SECRET" not in receipt_text
    assert "浦发银行" not in receipt_text


def test_same_snapshot_is_eligible_once_on_the_next_delivery_day(
    tmp_path: Path,
) -> None:
    _seed_root(tmp_path)
    sends: list[str] = []

    def sender(**kwargs: object) -> dict:
        sends.append("sent")
        return {
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        }

    first = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
    )
    second = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 4, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
    )

    assert first["status"] == "sent"
    assert second["status"] == "sent"
    assert sends == ["sent", "sent"]


def test_same_day_a_b_a_content_sequence_sends_only_a_and_b(tmp_path: Path) -> None:
    paths = _seed_root(tmp_path)
    sends: list[str] = []

    def sender(**kwargs: object) -> dict:
        sends.append("sent")
        return {
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        }

    first = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
    )
    _set_daily_change(paths, -2.0)
    second = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 36, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
    )
    _set_daily_change(paths, -3.98)
    third = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 37, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
    )

    assert first["status"] == "sent"
    assert second["status"] == "sent"
    assert third["status"] == "skipped_duplicate"
    assert sends == ["sent", "sent"]


def test_confirmed_send_with_state_write_failure_is_receipt_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_root(tmp_path)
    import quantdesk.morning_brief as module

    real_write = module.atomic_write_json

    def fail_delivery_state(path: str | Path, payload: object) -> Path:
        target = Path(path)
        if target.name == "morning_brief_delivery.json":
            raise RuntimeError("simulated state write failure")
        return real_write(target, payload)

    monkeypatch.setattr(module, "atomic_write_json", fail_delivery_state)
    first = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **kwargs: {
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        },
    )
    monkeypatch.setattr(module, "atomic_write_json", real_write)
    calls: list[str] = []
    second = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 36, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert first["status"] == "ambiguous"
    assert first["reason_code"] == "delivery_state_write_failed"
    assert second["status"] == "skipped_duplicate"
    assert calls == []


def test_ambiguous_receipt_blocks_same_day_retry_before_secret_loader(
    tmp_path: Path,
) -> None:
    _seed_root(tmp_path)
    first = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **kwargs: {
            "status": "ambiguous",
            "reason_code": "ambiguous_delivery",
            "http_status": None,
        },
    )
    calls: list[str] = []
    second = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 36, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert first["status"] == "ambiguous"
    assert second["status"] == "skipped_uncertain_prior_attempt"
    assert calls == []


def test_malformed_receipt_fails_closed_before_secret_loader(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    receipt = (
        tmp_path
        / "results/notifications/serverchan_morning_brief_20260803T083500000000_deadbeefdead.json"
    )
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("{broken", encoding="utf-8")
    calls: list[str] = []

    result = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "delivery_receipt_invalid"
    assert calls == []


def test_receipt_directory_symlink_escape_blocks_before_secret_loader(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _seed_root(root)
    outside = tmp_path / "outside-receipts"
    _directory_symlink(root / "results/notifications", outside)
    calls: list[str] = []

    result = run_morning_brief(
        root,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "notification_receipt_path_escape"
    assert calls == []
    assert list(outside.iterdir()) == []


def test_public_run_api_has_no_direct_secret_value_parameter() -> None:
    parameters = inspect.signature(run_morning_brief).parameters

    assert "sendkey" not in parameters
    assert "sendkey_loader" not in parameters
    assert "_sendkey_loader" in parameters


@pytest.mark.parametrize("delivery_status", ["failed", "ambiguous"])
def test_failed_or_ambiguous_send_does_not_commit_dedupe(
    tmp_path: Path, delivery_status: str
) -> None:
    _seed_root(tmp_path)

    result = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **kwargs: {
            "channel": "server_chan",
            "status": delivery_status,
            "reason_code": "provider_failure",
            "http_status": None,
        },
    )

    assert result["status"] == delivery_status
    assert not (tmp_path / "state/morning_brief_delivery.json").exists()


def test_corrupt_dedupe_state_blocks_before_secret_or_transport(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    state_path = tmp_path / "state/morning_brief_delivery.json"
    state_path.write_text("{broken", encoding="utf-8")
    calls: list[str] = []

    result = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "delivery_state_invalid"
    assert calls == []


def test_delivery_state_file_symlink_escape_blocks_before_secret_loader(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _seed_root(root)
    outside_state = tmp_path / "outside-state.json"
    _write_json(outside_state, {"schema_version": "1", "channels": {}})
    _file_symlink(root / "state/morning_brief_delivery.json", outside_state)
    calls: list[str] = []

    result = run_morning_brief(
        root,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "delivery_state_path_escape"
    assert calls == []


def test_duplicate_same_content_skips_without_secret_or_transport(
    tmp_path: Path,
) -> None:
    _seed_root(tmp_path)
    first = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 35, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **kwargs: {
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        },
    )
    calls: list[str] = []

    second = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 8, 36, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert first["status"] == "sent"
    assert second["status"] == "skipped_duplicate"
    assert calls == []


def test_outside_window_blocks_before_secret_loader(tmp_path: Path) -> None:
    _seed_root(tmp_path)
    calls: list[str] = []

    result = run_morning_brief(
        tmp_path,
        network=True,
        push=True,
        dry_run=False,
        now=datetime(2026, 8, 3, 10, 0, tzinfo=SHANGHAI),
        _sendkey_loader=lambda: calls.append("secret") or "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append("sender") or {"status": "sent"},
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "outside_delivery_window"
    assert calls == []
