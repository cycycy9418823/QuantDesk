"""C 段（T02）：固定 8000 入口，以及"同一份文件 → API → 页面"同值同日期。

本文件只服务 PRODUCT-DAILY-ACCESS-20260928 的 C 段证据，复用既有装置、不另造夹具：

* ``tests.test_owner_clarity.status_response``：真 Flask 蓝图 + 注入临时项目根
  （``QUANTDESK_PROJECT_ROOT``），因此读的是临时文件，不碰正式 ``data/``。
* ``tests.test_project_status_ui._run_status_ui``：在 node 里加载**真页面 JS**
  （``app/static/js/daily_research.js``），带 DOM/fetch 桩。

要证明的三件事：
1. 半份（24/48）在 API 与页面上都是 24，不许显示成 48；跑满（48/48）才显示 48。
2. 日期不符或缺文件的 manifest 不得被当作当日数据（显示"未知"）。
3. 服务没起来时，页面给出一句能照做的启动指令，并且入口写死 8000、默认不漂端口。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_owner_clarity import status_response
from tests.test_project_status_ui import _run_status_ui

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-09-28"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def seed_day(root: Path, *, collected: int, expected: int, quoted: int = 4800,
             valid: int = 4700, trade_date: str = DAY, with_manifest: bool = True) -> None:
    """写一份"当天"的最小现场：AI 记录钉住目标日，快照 manifest 给出档数。"""

    write_json(root / "results/ai_trader/cycle_latest.json", {
        "ok": True, "as_of": DAY,
        "plan": {"decision_date": DAY, "committed": True, "llm": {"used": True, "model": "fixture-model"}},
    })
    if with_manifest:
        write_json(root / f"data/snapshots/{DAY}/manifest.json", {
            "trade_date": trade_date, "collected_slot_count": collected, "expected_slot_count": expected,
            "quoted_row_count": quoted, "valid_quote_count": valid,
        })


def snapshot_facts(root: Path) -> dict:
    return status_response(root)["runtime_observations"]["snapshot"]


def test_partial_day_reports_24_of_48_and_never_claims_full(tmp_path) -> None:
    seed_day(tmp_path, collected=24, expected=48)

    facts = snapshot_facts(tmp_path)

    assert facts["as_of"] == DAY
    assert facts["expected_slots"] == 48
    assert facts["collected_slots"] == 24, "半份必须原样报 24，不能凑成 48"
    assert facts["collected_slots"] < facts["expected_slots"]
    assert "已收到 24/48 份" in facts["message"]
    assert "已收到 48/48 份" not in facts["message"]
    assert facts["received_slots"] == 24
    assert facts["strict_qualified_slots"] == 24
    assert facts["blocks_workflow"] is False


def test_full_day_reports_48_of_48(tmp_path) -> None:
    seed_day(tmp_path, collected=48, expected=48, valid=4800)

    facts = snapshot_facts(tmp_path)

    assert (facts["collected_slots"], facts["expected_slots"]) == (48, 48)
    assert "已收到 48/48 份" in facts["message"]
    assert facts["received_slots"] == facts["strict_qualified_slots"] == 48


def test_manifest_of_another_day_is_not_used_as_today(tmp_path) -> None:
    seed_day(tmp_path, collected=48, expected=48, trade_date="2026-09-25")

    facts = snapshot_facts(tmp_path)

    assert facts["as_of"] == DAY
    assert facts["collected_slots"] is None and facts["expected_slots"] is None
    assert "数量未知" in facts["message"], "日期不符的 manifest 不能充当日数据"


def test_missing_manifest_stays_unknown_instead_of_full(tmp_path) -> None:
    seed_day(tmp_path, collected=0, expected=0, with_manifest=False)

    facts = snapshot_facts(tmp_path)

    assert facts["collected_slots"] is None and facts["expected_slots"] is None
    assert "数量未知" in facts["message"]



def test_page_card_shows_the_same_numbers_as_the_api(tmp_path) -> None:
    """文件 → API → 页面：同一份 manifest，页面必须显示与 API 完全相同的档数。"""

    seed_day(tmp_path, collected=24, expected=48)
    observations = status_response(tmp_path)["runtime_observations"]

    partial = _run_status_ui(f"""
vm.runInContext(`
    renderOwnerRuntimeSummary({json.dumps(observations, ensure_ascii=False)});
    console.log(JSON.stringify({{ html: document.getElementById('projectRuntimeSummary').innerHTML }}));
`, context);
""")
    html = partial["html"]
    assert "24 / 48 份" in html
    assert "48 / 48 份" not in html, "半份不能显示成跑满"
    assert "文件未齐" in html
    assert "严格时段合格 24" in html
    assert DAY in html

    seed_day(tmp_path, collected=48, expected=48)
    full_observations = status_response(tmp_path)["runtime_observations"]
    full = _run_status_ui(f"""
vm.runInContext(`
    renderOwnerRuntimeSummary({json.dumps(full_observations, ensure_ascii=False)});
    console.log(JSON.stringify({{ html: document.getElementById('projectRuntimeSummary').innerHTML }}));
`, context);
""")
    assert "48 / 48 份" in full["html"]
    assert "文件未齐" not in full["html"]
    assert "严格时段合格 48" in full["html"]


def test_page_unknown_counts_do_not_look_like_a_full_day(tmp_path) -> None:
    seed_day(tmp_path, collected=48, expected=48, trade_date="2026-09-25")
    observations = status_response(tmp_path)["runtime_observations"]

    result = _run_status_ui(f"""
vm.runInContext(`
    renderOwnerRuntimeSummary({json.dumps(observations, ensure_ascii=False)});
    console.log(JSON.stringify({{ html: document.getElementById('projectRuntimeSummary').innerHTML }}));
`, context);
""")

    assert "收到文件数量未知" in result["html"]
    assert "48 / 48 份" not in result["html"]


def test_page_tells_the_owner_how_to_open_the_fixed_door_when_server_is_down() -> None:
    """服务没起来：页面要能说清"去启动哪个入口、门是哪个地址"。"""

    result = _run_status_ui(r"""
vm.runInContext(`
    // 让 requestJson 在真实路径上抛出：fetch 桩返回的 json() 解出的是一个会拒绝的 thenable，
    // 等价于"连不上服务器"（浏览器里是 Failed to fetch / 连接被拒绝）。
    responses.push({ then: (resolve, reject) => reject("Failed to fetch") });
    (async () => {
        await loadProjectStatus();
        console.log(JSON.stringify({
            banner: nodeText("projectStatusBanner"),
            readTime: nodeText("projectStatusReadTime"),
            requests,
        }));
    })();
`, context);
""")

    banner = result["banner"]
    assert "start_ui.cmd" in banner and "scripts/qd.py app" in banner
    assert "http://127.0.0.1:8000/" in banner
    assert "不漂到别的端口" in banner
    assert "这是状态文件问题" in banner
    assert result["readTime"] == "本次读取失败（服务器时间未提供）"
    assert result["requests"] == [{"url": "/api/project-status", "method": "GET"}]


def test_server_answered_but_unreadable_gets_no_network_hint() -> None:
    """服务器有回应（只是文件读不到）时，不能给"去启动服务"的误导提示。"""

    result = _run_status_ui(r"""
vm.runInContext(`
    responses.push({readable: false, error: "missing_file"});
    (async () => {
        await loadProjectStatus();
        console.log(JSON.stringify({ banner: nodeText("projectStatusBanner") }));
    })();
`, context);
""")

    assert "start_ui.cmd" not in result["banner"]


def test_fixed_door_contract_is_pinned_in_code() -> None:
    """固定入口的契约钉在源码里：默认 8000、默认不漂端口、页面只读这一个地址。"""

    start_ui = (ROOT / "start_ui.cmd").read_text(encoding="utf-8")
    assert "scripts\\qd.py" in start_ui and " app " in start_ui

    launcher = (ROOT / "scripts" / "launcher.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--port", type=int, default=8000' in launcher
    assert '"--allow-fallback-port", action="store_true"' in launcher
    assert "def find_free_port(preferred: int = 8000" in launcher

    system_route = (ROOT / "scripts" / "server" / "routes" / "system.py").read_text(encoding="utf-8")
    assert 'app.config.get("QUANTDESK_PROJECT_ROOT")' in system_route

    js = (ROOT / "app" / "static" / "js" / "daily_research.js").read_text(encoding="utf-8")
    assert 'requestJson("/api/project-status")' in js
    assert "http://127.0.0.1:8000/" in js
