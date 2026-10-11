"""D3/T20：模型返回的 JSON 结构/数组/字段类型错误必须明确失败，不得伪装成"主动空仓"。

全部为合成夹具：不联网、不调用真实模型（`llm._call` 被替换）。
"""

from __future__ import annotations

import json

import pytest

from ai_trader import llm


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("AI_TRADER_LLM_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    return monkeypatch


def _reply(monkeypatch, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(llm, "_call", lambda *a, **k: text)


def test_root_not_an_object_fails_explicitly(configured) -> None:
    diagnostics: dict = {}
    _reply(configured, "[1, 2, 3]")

    assert llm.propose_picks({"as_of": "2026-09-21"}, diagnostics) is None
    assert diagnostics.get("reason") == "unparseable_json", diagnostics


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"buys": {"600000": {"reason": "x"}}}, "malformed_buys_not_array"),
        ({"buys": "600000"}, "malformed_buys_not_array"),
        ({"buys": 1}, "malformed_buys_not_array"),
        ({"buys": [], "sells": True}, "malformed_sells_not_array"),
        ({"buys": [], "sells": {"600000": "x"}}, "malformed_sells_not_array"),
        ({"buys": [], "avoid": 1}, "malformed_avoid_not_array"),
        ({"buys": [], "stance": ["防守"]}, "malformed_stance_not_object"),
    ],
)
def test_wrong_container_types_fail_instead_of_pretending_an_empty_plan(
    configured, payload: dict, expected: str
) -> None:
    diagnostics: dict = {}
    _reply(configured, payload)

    out = llm.propose_picks({"as_of": "2026-09-21"}, diagnostics)

    assert out is None, "类型错误不得被当成'主动空仓'放行"
    assert expected in str(diagnostics.get("reason") or ""), diagnostics


def test_valid_plan_still_parses_and_per_row_junk_is_still_dropped(configured) -> None:
    """正常对照：合法 JSON 照旧解析；单行垃圾保持既有"逐行净化"行为。"""

    _reply(
        configured,
        {
            "buys": [
                {"symbol": "600000", "reason": "低位企稳", "hold_days": 5, "conviction": "probation"},
                {"symbol": "bad", "reason": "x", "hold_days": 3},
                "not-a-row",
            ],
            "sells": [{"symbol": "600519", "reason": "止盈"}],
            "avoid": ["600001"],
            "stance": {"summary": "防守", "preferred_styles": ["波段"], "reasoning": "贵"},
            "market_note": "观望",
        },
    )

    out = llm.propose_picks({"as_of": "2026-09-21"})

    assert out is not None
    assert [row["symbol"] for row in out["buys"]] == ["600000"]
    assert out["buys"][0]["hold_days"] == 5
    assert [row["symbol"] for row in out["sells"]] == ["600519"]
    assert out["avoid"] == ["600001"]
    assert out["stance"]["summary"] == "防守"
    assert out["market_note"] == "观望"


def test_missing_optional_arrays_are_still_allowed(configured) -> None:
    """只给 buys 的回复仍然合法（没有字段 ≠ 字段类型错）。"""

    _reply(configured, {"buys": []})

    out = llm.propose_picks({"as_of": "2026-09-21"})

    assert out is not None and out["buys"] == [] and out["sells"] == [] and out["avoid"] == []