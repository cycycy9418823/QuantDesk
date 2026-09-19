"""Round 13：日线宽池证据化排除的 fail-closed 契约（离线完整夹具）。

全池门是 all-or-nothing：4 只永久取不到数的票会把 4,599 只合格数据挡在门外。
排除是解开死锁的唯一合法手段，但排除本身必须逐条对当前磁盘证据重新自证——
任何期望值与磁盘不符，整次任务必须 fail-closed，而不是悄悄少排一只。

本测试自建一个**完整的临时项目根**：universe、ST 历史、每只票的 canonical
日线、策略文件全部就位，因此先证明「完整夹具能正常通过」，再对策略逐项注入
漂移以验证真正的规则失败，而不是因为某个证据文件缺失而提前抛错假通过。

真实现场（生产 config + 真实磁盘证据）的复核仍列为待验：本工作区不含生产
`data/daily` 与 `data/universe`，不复制生产数据、不伪造现场结论。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quantdesk.weekly_daily_wide_pool import load_weekly_daily_exclusion_policy

TARGET = "2026-08-05"
UNIVERSE_ROWS = ["000004,国华退", "920305,ST赛隆"]
ST_ROWS = ["920305,ST赛隆"]
CANONICAL_LAST = {
    "000004": "2026-04-28",
    "002808": "2026-04-30",
    "002898": "2026-04-29",
    "920305": "2026-04-29",
}
ENTRIES = [
    {
        "symbol": "000004",
        "expected_name": "国华退",
        "reason_code": "delisted_by_universe_name",
        "expected_last_trade_date": "2026-04-28",
    },
    {
        "symbol": "002808",
        "expected_name": "恒久退",
        "reason_code": "delisted_by_universe_name",
        "expected_last_trade_date": "2026-04-30",
    },
    {
        "symbol": "002898",
        "expected_name": "赛隆退",
        "reason_code": "delisted_by_universe_name",
        "expected_last_trade_date": "2026-04-29",
    },
    {
        "symbol": "920305",
        "expected_name": "ST赛隆",
        "reason_code": "long_suspended_st",
        "expected_last_trade_date": "2026-04-29",
    },
]


def _fixture_policy() -> dict:
    return {
        "schema_version": "weekly_daily_wide_pool_exclusions_v1",
        "task_type": "weekly_daily_wide_pool_exclusions",
        "effective_from": "2026-08-01",
        "evidence_sources": {
            "current_a_share_universe": "data/universe/current_a_share_universe.csv",
            "st_history": "data/universe/st_history.csv",
        },
        "entries": copy.deepcopy(ENTRIES),
        "boundaries": {
            "denominator_disclosure_required": True,
            "quality_gate_relaxed": False,
            "canonical_data_deleted": False,
        },
        "denominator_disclosure": "4603 = 4599 + 4",
    }


def _build_fixture_root(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (root / "data" / "daily").mkdir(parents=True, exist_ok=True)
    (root / "data" / "universe" / "current_a_share_universe.csv").write_text(
        "symbol,name\n" + "\n".join(UNIVERSE_ROWS) + "\n", encoding="utf-8"
    )
    (root / "data" / "universe" / "st_history.csv").write_text(
        "symbol,name\n" + "\n".join(ST_ROWS) + "\n", encoding="utf-8"
    )
    for symbol, last in CANONICAL_LAST.items():
        (root / "data" / "daily" / f"{symbol}.csv").write_text(
            "date,open,high,low,close,volume\n"
            "2026-04-01,1,1,1,1,100\n"
            f"{last},1,1,1,1,100\n",
            encoding="utf-8",
        )


def _write_policy(root: Path, payload: dict, name: str = "fixture_exclusions.json") -> Path:
    path = root / "config" / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _install_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _build_fixture_root(root)
    _write_policy(root, _fixture_policy())
    return root


def _load(root: Path, path: Path | None = None) -> dict:
    return load_weekly_daily_exclusion_policy(
        root,
        policy_path=path or (root / "config" / "fixture_exclusions.json"),
        target_date=TARGET,
    )


def test_complete_exclusion_fixture_verifies_against_disk(tmp_path: Path) -> None:
    """完整夹具必须真正逐条通过磁盘证据复核，并输出可审计 audit。"""

    root = _install_fixture(tmp_path)

    audit = _load(root)

    assert audit["verified_against_disk"] is True
    assert audit["declared_symbols"] == ["000004", "002808", "002898", "920305"]
    assert audit["target_date"] == TARGET
    assert len(audit["policy_sha256"]) == 64
    # 002808 / 002898 已离开名单：声明仍在，生效集合只含仍在名单内的两只。
    assert audit["absent_from_universe"] == ["002808", "002898"]
    assert audit["symbols"] == ["000004", "920305"]

    by_symbol = {row["symbol"]: row for row in audit["entries"]}
    for symbol in ("000004", "002808", "002898"):
        row = by_symbol[symbol]
        assert row["reason_code"] == "delisted_by_universe_name"
        assert row["observed_last_trade_date"] == CANONICAL_LAST[symbol]
    assert by_symbol["000004"]["universe_evidence"] == "name_matched"
    assert by_symbol["000004"]["exclusion_effective"] is True
    assert by_symbol["002808"]["universe_evidence"] == "absent_from_universe"
    assert by_symbol["002808"]["exclusion_effective"] is False
    # ST 票单列，不得混进 delisted，且必须有 st_history 证据。
    st = by_symbol["920305"]
    assert st["reason_code"] == "long_suspended_st"
    assert st["universe_evidence"] == "name_matched"
    assert st["exclusion_effective"] is True
    assert st["observed_last_trade_date"] == "2026-04-29"


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (
            lambda p: p["entries"][0].__setitem__("expected_name", "国华股份"),
            "name",
        ),
        (
            lambda p: p["entries"][0].__setitem__(
                "expected_last_trade_date", "2026-08-04"
            ),
            "last_trade_date",
        ),
        (
            lambda p: p["entries"][0].__setitem__("reason_code", "long_suspended_st"),
            "delisted_faked_as_st",
        ),
        (
            lambda p: p["entries"][3].__setitem__(
                "reason_code", "delisted_by_universe_name"
            ),
            "st_faked_as_delisted",
        ),
        (lambda p: p["entries"][0].__setitem__("reason_code", "whatever"), "bad_reason"),
        (lambda p: p.__setitem__("effective_from", "2099-01-01"), "not_yet_effective"),
        (lambda p: p.__setitem__("schema_version", "v99"), "schema"),
        (lambda p: p.__setitem__("entries", []), "empty_entries"),
    ],
)
def test_evidence_drift_fails_closed(tmp_path: Path, mutate, label: str) -> None:
    """任一证据不符 → 拒绝整个策略，不得部分放行。"""

    root = _install_fixture(tmp_path)
    payload = _fixture_policy()
    mutate(payload)
    path = _write_policy(root, payload, name=f"policy_{label}.json")

    with pytest.raises(ValueError):
        _load(root, path)


def test_duplicate_symbol_is_rejected(tmp_path: Path) -> None:
    """重复条目会让「排除了几只」变得不可数，直接拒绝。"""

    root = _install_fixture(tmp_path)
    payload = _fixture_policy()
    payload["entries"].append(copy.deepcopy(payload["entries"][0]))
    path = _write_policy(root, payload, name="policy_dup.json")

    with pytest.raises(ValueError, match="duplicate"):
        _load(root, path)


def test_unknown_symbol_not_in_universe_is_rejected(tmp_path: Path) -> None:
    """凭空写一个代码仍必须被拒绝：不在名单里，也没有 canonical 证据。

    离开上市名单本身不再算证据漂移（真退市走完流程就会离开），但那种票必须
    留下 canonical 日线与预期末日作为"确实存在过"的证据。一个既不在名单、也
    没有 canonical 文件的代码没有任何证据可依，照旧 fail-closed。
    """

    root = _install_fixture(tmp_path)
    payload = _fixture_policy()
    payload["entries"][0]["symbol"] = "999999"
    path = _write_policy(root, payload, name="policy_unknown.json")

    with pytest.raises(ValueError, match="exclusion_canonical_missing:999999"):
        _load(root, path)


def test_symbol_absent_from_universe_becomes_moot_not_fail_closed(tmp_path: Path) -> None:
    """已离开上市名单的排除项自动失效，而不是把整个任务 fail-closed。"""

    root = _install_fixture(tmp_path)

    audit = _load(root)

    assert audit["absent_from_universe"] == ["002808", "002898"]
    assert all(
        row["exclusion_effective"] is False
        for row in audit["entries"]
        if row["symbol"] in audit["absent_from_universe"]
    )
    assert all(
        row["universe_evidence"] == "absent_from_universe"
        for row in audit["entries"]
        if row["symbol"] in audit["absent_from_universe"]
    )
    # 失效不等于不复核：canonical 末日证据仍逐条成立。
    assert all(row["observed_last_trade_date"] for row in audit["entries"])


def test_policy_declares_denominator_disclosure_duty(tmp_path: Path) -> None:
    """必须写明 4603 = 4599 + 4，禁止只报 4599 制造「全过」假象。"""

    _install_fixture(tmp_path)
    payload = _fixture_policy()
    assert payload["boundaries"]["denominator_disclosure_required"] is True
    assert payload["boundaries"]["quality_gate_relaxed"] is False
    assert payload["boundaries"]["canonical_data_deleted"] is False
    assert "4603" in payload["denominator_disclosure"]
    assert "4599" in payload["denominator_disclosure"]
