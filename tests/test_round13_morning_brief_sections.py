"""Round 13 ④：早报新增三段（候选明细／影子风控／宽池覆盖）的契约。

设计要点：新增段落各自读自己的工件，因此陈旧的 workflow 快照不能静默压掉它们；
任何缺失或跨日错配都必须降级成 reason_code，绝不许静默变成 0。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantdesk.morning_brief import (
    _build_shadow_gate,
    _build_wide_pool,
    _candidate_digest,
    _return_suffix,
    render_morning_brief_markdown,
)


AS_OF = "2026-08-05"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 影子风控段
# --------------------------------------------------------------------------- #
def test_shadow_gate_reads_the_day_scoped_archive(tmp_path: Path) -> None:
    _write(
        tmp_path / "results/risk_gate_shadow_daily" / f"{AS_OF}.json",
        {
            "as_of": AS_OF,
            "evaluation_count": 5,
            "insufficient_evidence_count": 5,
            "would_pass_count": 0,
            "would_block_count": 0,
            "risk_gate_enforced": False,
        },
    )

    gate, reason = _build_shadow_gate(tmp_path, AS_OF)

    assert reason == ""
    assert gate["evaluation_count"] == 5
    assert gate["insufficient_evidence_count"] == 5
    assert gate["would_pass_count"] == 0
    assert gate["risk_gate_enforced"] is False


def test_shadow_gate_rejects_other_day_archive(tmp_path: Path) -> None:
    """跨日错配必须降级，不能把昨天的观察报成今天。"""

    _write(
        tmp_path / "results/risk_gate_shadow_daily" / f"{AS_OF}.json",
        {"as_of": "2026-08-04", "evaluation_count": 9},
    )

    gate, reason = _build_shadow_gate(tmp_path, AS_OF)

    assert gate == {}
    assert reason == "shadow_gate_scope_mismatch"


def test_shadow_gate_missing_archive_degrades(tmp_path: Path) -> None:
    gate, reason = _build_shadow_gate(tmp_path, AS_OF)
    assert gate == {}
    assert reason == "shadow_gate_unavailable"


@pytest.mark.parametrize("bad", [None, "5", True, 1.5])
def test_shadow_gate_rejects_non_integer_evaluation_count(tmp_path: Path, bad) -> None:
    """`evaluation_count` 不是整数就拒收——包括 bool（True 会被误当 1）。"""

    _write(
        tmp_path / "results/risk_gate_shadow_daily" / f"{AS_OF}.json",
        {"as_of": AS_OF, "evaluation_count": bad},
    )

    gate, reason = _build_shadow_gate(tmp_path, AS_OF)

    assert gate == {}
    assert reason == "shadow_gate_invalid"


# --------------------------------------------------------------------------- #
# 宽池覆盖段
# --------------------------------------------------------------------------- #
def test_wide_pool_counts_only_files_reaching_as_of(tmp_path: Path) -> None:
    daily = tmp_path / "data" / "daily"
    daily.mkdir(parents=True)
    header = "date,symbol,close\n"
    (daily / "000001.csv").write_text(header + f"{AS_OF},000001,10\n", encoding="utf-8")
    (daily / "000002.csv").write_text(header + f"{AS_OF},000002,11\n", encoding="utf-8")
    (daily / "000003.csv").write_text(header + "2026-07-10,000003,12\n", encoding="utf-8")

    pool, reason = _build_wide_pool(tmp_path, AS_OF)

    assert reason == ""
    assert pool == {"daily_current": 2, "daily_total": 3}


def test_wide_pool_missing_dir_degrades(tmp_path: Path) -> None:
    pool, reason = _build_wide_pool(tmp_path, AS_OF)
    assert pool == {}
    assert reason == "wide_pool_unavailable"


# --------------------------------------------------------------------------- #
# 候选明细与收益率
# --------------------------------------------------------------------------- #
def test_candidate_digest_uses_chinese_label_and_bounds_to_three() -> None:
    rows = [
        {"symbol": f"60000{i}", "name": f"标的{i}", "strategy_type": "long"}
        for i in range(5)
    ]

    digest = _candidate_digest(rows)

    assert digest.count("、") == 2, "明细必须限制为 3 条"
    assert "长线" in digest and "long" not in digest
    assert "600000 标的0(长线)" in digest


def test_candidate_digest_without_usable_rows_is_explicit() -> None:
    assert _candidate_digest([]) == "明细不可用"
    assert _candidate_digest([{"name": "无代码"}]) == "明细不可用"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.15, " (+0.15%)"), (-1.5, " (-1.50%)"), (0.0, " (+0.00%)"), (None, ""), ("x", "")],
)
def test_return_suffix_only_renders_finite_numbers(value, expected) -> None:
    assert _return_suffix(value) == expected


# --------------------------------------------------------------------------- #
# 渲染整体
# --------------------------------------------------------------------------- #
def test_render_includes_new_sections_and_stays_within_push_budget() -> None:
    payload = {
        "as_of": AS_OF,
        "run_id": "run-x",
        "data_health": {
            "chain_status": "completed",
            "daily_fresh_count": 914,
            "daily_requested_count": 914,
            "minute_effective_complete_count": 914,
            "minute_requested_count": 914,
        },
        "freshness_lag": {
            "core_daily": 0,
            "core_minute": 5,
            "sector_snapshot": 0,
            "wide_daily": 0,
            "wide_minute": None,
        },
        "candidates": [
            {"symbol": "600227", "name": "赤天化", "strategy_type": "long"},
        ],
        "candidate_count": 6,
        "paper_account": {
            "total_equity": 98775.11,
            "daily_change": 146.04,
            "daily_return_pct": 0.15,
        },
        "shadow_gate": {
            "evaluation_count": 5,
            "insufficient_evidence_count": 5,
            "would_pass_count": 0,
            "risk_gate_enforced": False,
        },
        "wide_pool": {"daily_current": 5508, "daily_total": 5864},
        "candidate_review_url": "http://127.0.0.1:8000/#daily-focus-section",
    }

    rendered = render_morning_brief_markdown(payload)

    assert "候选复盘：6 条；600227 赤天化(长线)" in rendered
    assert "(+0.15%)" in rendered
    assert "风控影子：评估 5 · 证据不足 5 · 通过 0（观察中，未启用拦截）" in rendered
    assert "宽池日线：已至今日 5508/5864" in rendered
    assert "数据新鲜度（最大落后交易日）：核心日线 0 天；核心分钟 5 天（警示）；板块快照 0 天；宽池日线 0 天；宽池分钟 不可用（警示）" in rendered
    assert len(rendered) <= 800


def test_render_marks_enforcement_when_gate_is_live() -> None:
    """一旦真的启用拦截，措辞必须改变——不能继续说"观察中"。"""

    rendered = render_morning_brief_markdown(
        {
            "as_of": AS_OF,
            "shadow_gate": {
                "evaluation_count": 3,
                "insufficient_evidence_count": 0,
                "would_pass_count": 3,
                "risk_gate_enforced": True,
            },
        }
    )

    assert "（已启用拦截）" in rendered
    assert "（观察中，未启用拦截）" not in rendered


def test_render_degrades_new_sections_without_data() -> None:
    rendered = render_morning_brief_markdown({"as_of": AS_OF})
    assert "风控影子：不可用" in rendered
    assert "宽池日线：不可用" in rendered
