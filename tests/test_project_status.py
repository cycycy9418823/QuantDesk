"""Isolated tests for read-only STATUS.md parsing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk.project_status import MAX_STATUS_BYTES, read_project_status

SAMPLE_A = """# STATUS · 更新于 2026-09-05 14:20 · 更新者：本会话

## 今日健康行
日线 ✓5547/5555 · 快照 ✗0/48 · 策略账 ✓40仓 · AI账 ✓8仓 · 核对 工作树错1 · 备份 ✗
（红格：A0 未装。验证条件为程序推导，非 AI 声明。）

## 当前阶段
B + C + A0 并行 · 检查点 2026-10-09 · 剩 — 个交易日（今天周六）

## B 线
最近三件事：停牌对不上已修（工作树）。
下一步：解冻 914。
卡住：未授权 commit / 合入。

## C 线
最近三件事：对照页顶部已加「验证条件为程序推导，非 AI 声明」。
下一步：所有者授权后才 commit。
卡住：等授权 commit。

## A0 线
最近三件事：未拉网。
下一步：下个交易日干跑一次。
卡住：今天周六。

## 跨线待办（写给对方，由所有者转达）
- [ ]

## 疑似废弃（§6）
- 历史全图

## 本周花费
模型调用 — 次
"""

SAMPLE_B = SAMPLE_A.replace("更新于 2026-09-05 14:20", "更新于 2026-09-05 18:00").replace(
    "日线 ✓5547/5555", "日线 ✓1/1"
)


def _write(root: Path, text: str, encoding: str = "utf-8") -> Path:
    path = root / "STATUS.md"
    path.write_text(text, encoding=encoding, newline="\n")
    return path


def test_normal_status_keeps_wording_and_does_not_invent_days(tmp_path: Path) -> None:
    _write(tmp_path, SAMPLE_A)
    payload = read_project_status(tmp_path)
    assert payload["ok"] is True
    assert payload["readable"] is True
    assert payload["readable_means"] == "file_opened_not_system_health"
    assert payload["source_label"] == "项目记录 · 非实时核验"
    assert payload["document_updated_at"] == "2026-09-05 14:20"
    assert payload["document_updater"] == "本会话"
    assert "快照 ✗0/48" in payload["health_line"]
    assert "验证条件为程序推导，非 AI 声明" in payload["health_explanation"]
    assert payload["phase"]["checkpoint"] == "2026-10-09"
    assert payload["phase"]["remaining_trading_days"] == "未提供"
    assert payload["line_progress"]["B"]["stuck"] == "未授权 commit / 合入。"
    assert "未授权 commit / 合入" in payload["raw_text"]
    assert "工作树" in payload["raw_text"]
    assert payload["error"] is None
    assert payload["document_updated_at"] != payload["read_at"]


def test_refresh_replaces_a_with_b_and_does_not_write_elsewhere(tmp_path: Path) -> None:
    extra = tmp_path / "untouched.txt"
    extra.write_text("keep", encoding="utf-8")
    _write(tmp_path, SAMPLE_A)
    first = read_project_status(tmp_path)
    _write(tmp_path, SAMPLE_B)
    second = read_project_status(tmp_path)
    assert "✓5547/5555" in first["health_line"]
    assert "✓1/1" in second["health_line"]
    assert first["sha256"] != second["sha256"]
    assert extra.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.iterdir())  # still only our files


def test_missing_empty_bad_encoding_too_large_do_not_create_file(tmp_path: Path) -> None:
    missing = read_project_status(tmp_path)
    assert missing["readable"] is False
    assert missing["error"] == "missing_file"
    assert not (tmp_path / "STATUS.md").exists()

    empty = tmp_path / "STATUS.md"
    empty.write_bytes(b"")
    empty_payload = read_project_status(tmp_path)
    assert empty_payload["error"] == "empty_file"
    assert empty_payload["ok"] is False

    empty.write_bytes(b"\xff\xfe\x00not-utf8")
    bad = read_project_status(tmp_path)
    assert bad["error"] == "bad_encoding"
    assert bad["readable"] is False

    huge = tmp_path / "STATUS.md"
    huge.write_bytes(b"A" * (MAX_STATUS_BYTES + 1))
    large = read_project_status(tmp_path)
    assert large["error"] == "too_large"
    assert large["ok"] is False


def test_filesystem_error_is_structured_when_status_disappears_during_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, SAMPLE_A)
    original_exists = Path.exists

    def fail_exists(self: Path) -> bool:
        if self == path:
            raise PermissionError("status probe denied")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", fail_exists)
    payload = read_project_status(tmp_path)

    assert payload["ok"] is False
    assert payload["readable"] is False
    assert payload["error"] == "read_failed:PermissionError"
    assert payload["read_at"]
    assert payload["source_kind"] == "STATUS.md"


def test_status_resolve_error_returns_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, SAMPLE_A)
    original_resolve = Path.resolve

    def fail_resolve(self: Path, *args, **kwargs) -> Path:
        if self == path:
            raise PermissionError("status resolve denied")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    payload = read_project_status(tmp_path)

    assert payload["readable"] is False
    assert payload["error"] == "read_failed:PermissionError"
    assert payload["raw_text"] == ""
    assert payload["read_at"]


def test_status_changed_during_read_does_not_mix_text_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, SAMPLE_A)
    before = path.stat()
    changed = SimpleNamespace(
        st_size=before.st_size,
        st_mtime=before.st_mtime + 1,
        st_mtime_ns=before.st_mtime_ns + 1_000_000_000,
        st_ctime_ns=before.st_ctime_ns + 1_000_000_000,
        st_ino=before.st_ino,
        st_dev=before.st_dev,
    )
    calls: list[int] = []

    def changing_fstat(fd: int):
        calls.append(fd)
        return before if len(calls) == 1 else changed

    monkeypatch.setattr("quantdesk.project_status.os.fstat", changing_fstat)
    payload = read_project_status(tmp_path)

    assert payload["readable"] is False
    assert payload["error"] == "changed_during_read"
    assert payload["raw_text"] == ""
    assert payload["sha256"] == ""


def test_oversized_status_uses_bounded_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "STATUS.md"
    path.write_bytes(b"A" * (MAX_STATUS_BYTES + 1))
    original_open = Path.open
    requested: list[int] = []

    class _Reader:
        def __enter__(self):
            self.handle = original_open(path, "rb")
            return self

        def __exit__(self, *args):
            self.handle.close()
            return False

        def read(self, size: int = -1) -> bytes:
            requested.append(size)
            return self.handle.read(size)

        def fileno(self) -> int:
            return self.handle.fileno()

    def bounded_open(self, *args, **kwargs):
        if self == path:
            return _Reader()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", bounded_open)
    payload = read_project_status(tmp_path)

    assert payload["error"] == "too_large"
    assert requested == [MAX_STATUS_BYTES + 1]


def test_missing_health_heading_keeps_raw_and_does_not_turn_green(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "# 杂记\n\n没有健康行标题\n\n## 其他\n随便写\n",
    )
    payload = read_project_status(tmp_path)
    assert payload["readable"] is True
    assert "missing_or_unrecognized_status_header" in payload["parse_warnings"]
    assert "missing_health_section" in payload["parse_warnings"]
    assert payload["health_line"] == "未提供"
    assert "没有健康行标题" in payload["raw_text"]
    assert payload["health_trade_date_label"] == "记录未明确标注"


def test_phase_summary_does_not_repeat_checkpoint_metadata(tmp_path: Path) -> None:
    _write(tmp_path, SAMPLE_A)
    payload = read_project_status(tmp_path)
    assert payload["phase"]["summary"] == "B + C + A0 并行"


def test_line_progress_stops_at_nested_heading(tmp_path: Path) -> None:
    _write(
        tmp_path,
        SAMPLE_A.replace(
            "卡住：未授权 commit / 合入。",
            "卡住：未授权 commit / 合入。\n\n### 证据细节\n不应出现在卡住摘要。",
        ),
    )
    payload = read_project_status(tmp_path)
    assert payload["line_progress"]["B"]["stuck"] == "未授权 commit / 合入。"


def test_script_and_dangerous_links_stay_in_text(tmp_path: Path) -> None:
    _write(
        tmp_path,
        SAMPLE_A.replace(
            "模型调用 — 次",
            '<script>alert(1)</script>\njavascript:alert(1)\nE:/secret/ledger.json\nhttp://evil.example/x.png',
        ),
    )
    payload = read_project_status(tmp_path)
    assert "<script>alert(1)</script>" in payload["raw_text"]
    assert "javascript:alert(1)" in payload["raw_text"]
    assert "E:/secret/ledger.json" in payload["raw_text"]


def test_bom_and_crlf_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "STATUS.md"
    path.write_bytes(("\ufeff" + SAMPLE_A.replace("\n", "\r\n")).encode("utf-8"))
    payload = read_project_status(tmp_path)
    assert payload["ok"] is True
    assert payload["encoding"] == "utf-8-sig"
    assert "\r" not in payload["raw_text"]
    assert payload["document_updated_at"] == "2026-09-05 14:20"


def test_sha256_matches_on_disk_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path, SAMPLE_A)
    payload = read_project_status(tmp_path)
    assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_committed_worktree_status_is_readable(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "STATUS.md"
    copy = tmp_path / "STATUS.md"
    copy.write_bytes(src.read_bytes())
    payload = read_project_status(tmp_path)
    assert payload["readable"] is True
    assert payload["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert payload["phase"]["remaining_trading_days"] == "未提供"
    assert "missing_health_section" not in payload["parse_warnings"] or payload["health_line"] == "未提供"
