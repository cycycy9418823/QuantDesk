"""Round 13：日线宽池证据化排除的 fail-closed 契约。

全池门是 all-or-nothing：4 只永久取不到数的票会把 4,599 只合格数据挡在门外。
排除是解开死锁的唯一合法手段，但排除本身必须逐条对当前磁盘证据重新自证——
任何期望值与磁盘不符，整次任务必须 fail-closed，而不是悄悄少排一只。

Research Only / Paper Account Only / No Broker Route / Not Investment Advice.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quantdesk.weekly_daily_wide_pool import load_weekly_daily_exclusion_policy


POLICY = Path("config/weekly_daily_wide_pool_exclusions.json")
TARGET = "2026-08-05"
ROOT = Path(".")


def _policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


@pytest.fixture
def in_repo_tmp(tmp_path_factory):
    """A scratch dir INSIDE the repo.

    The loader's ``_descendant`` guard rejects any policy path outside the
    project root. Writing candidates to pytest's ``tmp_path`` therefore raises
    ``path_outside_project_root`` before a single evidence rule runs, which
    would make every "fails closed" assertion pass for the wrong reason.
    """

    base = Path("results") / "_test_scratch_daily_exclusions"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    for child in base.glob("*"):
        child.unlink()
    base.rmdir()


def _write(payload: dict, path: Path) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _load(path: Path) -> dict:
    return load_weekly_daily_exclusion_policy(
        ROOT, policy_path=path, target_date=TARGET
    )


def test_production_policy_verifies_against_current_disk_evidence() -> None:
    """生产策略必须逐条通过磁盘证据复核，并输出可审计的 audit。

    2026-08-15 起四只票已整体离开上市名单（真实退市走完流程），因此它们的
    universe 证据形态从 `name_matched` 变为 `absent_from_universe`，有效排除
    集合为空 —— 排除项自动失效而不是 fail-closed。canonical 末日证据仍然逐条
    复核，所以"这四只确实存在过且确实停在预期末日"依然被证明。
    """

    audit = _load(POLICY)

    assert audit["verified_against_disk"] is True
    assert audit["declared_symbols"] == ["000004", "002808", "002898", "920305"]
    assert audit["target_date"] == TARGET
    assert len(audit["policy_sha256"]) == 64
    # 四只已离开名单：声明仍在，生效集合为空。
    assert audit["absent_from_universe"] == ["000004", "002808", "002898", "920305"]
    assert audit["symbols"] == []

    by_symbol = {row["symbol"]: row for row in audit["entries"]}
    # 三只退市票的 reason_code 与「退」后缀规则仍由生产代码校验 expected_name。
    for symbol in ("000004", "002808", "002898"):
        row = by_symbol[symbol]
        assert row["reason_code"] == "delisted_by_universe_name"
        assert row["universe_evidence"] == "absent_from_universe"
        assert row["exclusion_effective"] is False
    # ST 票单列，不得混进 delisted。
    st = by_symbol["920305"]
    assert st["reason_code"] == "long_suspended_st"
    assert st["universe_evidence"] == "absent_from_universe"
    # 名单里没有了，但 canonical 末日证据仍必须成立。
    assert st["observed_last_trade_date"] == "2026-04-29"


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda p: p["entries"][0].__setitem__("expected_name", "国华股份"), "name"),
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
def test_evidence_drift_fails_closed(in_repo_tmp, mutate, label) -> None:
    """任一证据不符 → 拒绝整个策略，不得部分放行。"""

    payload = copy.deepcopy(_policy())
    mutate(payload)
    path = _write(payload, in_repo_tmp / f"policy_{label}.json")

    with pytest.raises(ValueError):
        _load(path)


def test_duplicate_symbol_is_rejected(in_repo_tmp) -> None:
    """重复条目会让「排除了几只」变得不可数，直接拒绝。"""

    payload = copy.deepcopy(_policy())
    payload["entries"].append(copy.deepcopy(payload["entries"][0]))
    path = _write(payload, in_repo_tmp / "policy_dup.json")

    with pytest.raises(ValueError, match="duplicate"):
        _load(path)


def test_unknown_symbol_not_in_universe_is_rejected(in_repo_tmp) -> None:
    """凭空写一个代码仍必须被拒绝：不在名单里，也没有 canonical 证据。

    离开上市名单本身不再算证据漂移（真退市走完流程就会离开），但那种票必须
    留下 canonical 日线与预期末日作为"确实存在过"的证据。一个既不在名单、也
    没有 canonical 文件的代码没有任何证据可依，照旧 fail-closed。
    """

    payload = copy.deepcopy(_policy())
    payload["entries"][0]["symbol"] = "999999"
    path = _write(payload, in_repo_tmp / "policy_unknown.json")

    with pytest.raises(ValueError, match="exclusion_canonical_missing:999999"):
        _load(path)


def test_symbol_absent_from_universe_becomes_moot_not_fail_closed(in_repo_tmp) -> None:
    """已离开上市名单的排除项自动失效，而不是把整个任务 fail-closed。

    这是 2026-08-15 的真实场景：基线刷新后三只退市票整体从名单消失，旧行为会
    抛 universe_name_evidence_mismatch 并让每一次宽池运行都返回
    blocked/exclusion_policy_invalid。守卫的本意是"别让恢复交易的票继续被排除"，
    而彻底离开名单恰恰是"不可能恢复交易"的最强形式，且它也不可能再进池子。
    """

    audit = _load(POLICY)

    # 生产策略当前四只全部离开名单：声明保留、生效集合为空、逐条留证。
    assert audit["symbols"] == []
    assert audit["absent_from_universe"] == audit["declared_symbols"]
    assert all(row["exclusion_effective"] is False for row in audit["entries"])
    assert all(
        row["universe_evidence"] == "absent_from_universe" for row in audit["entries"]
    )
    # 失效不等于不复核：canonical 末日证据仍逐条成立。
    assert all(row["observed_last_trade_date"] for row in audit["entries"])


def test_policy_declares_denominator_disclosure_duty() -> None:
    """必须写明 4603 = 4599 + 4，禁止只报 4599 制造「全过」假象。"""

    payload = _policy()
    assert payload["boundaries"]["denominator_disclosure_required"] is True
    assert payload["boundaries"]["quality_gate_relaxed"] is False
    assert payload["boundaries"]["canonical_data_deleted"] is False
    assert "4603" in payload["denominator_disclosure"]
    assert "4599" in payload["denominator_disclosure"]
