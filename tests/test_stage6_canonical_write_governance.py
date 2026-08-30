from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _dangerous_writer_tokens(tree: ast.AST) -> set[str]:
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            if function.attr in {"to_csv", "write_bytes", "write_text"}:
                tokens.add(function.attr)
            if function.attr == "open":
                mode_node = node.args[0] if node.args else None
                mode = mode_node.value if isinstance(mode_node, ast.Constant) else "r"
                if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                    tokens.add("open")
            if (
                function.attr in {"rename", "replace"}
                and isinstance(function.value, ast.Name)
                and function.value.id == "os"
            ):
                tokens.add(f"os.{function.attr}")
            if (
                function.attr in {"copy2", "copyfile", "move"}
                and isinstance(function.value, ast.Name)
                and function.value.id == "shutil"
            ):
                tokens.add(f"shutil.{function.attr}")
        elif isinstance(function, ast.Name) and function.id == "open":
            mode_node = node.args[1] if len(node.args) > 1 else None
            mode = mode_node.value if isinstance(mode_node, ast.Constant) else "r"
            if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                tokens.add("open")
    return tokens


def test_writer_token_scan_detects_path_and_move_variants() -> None:
    tree = ast.parse(
        """
path.open('w')
path.write_text('x')
path.write_bytes(b'x')
os.rename(source, target)
shutil.move(source, target)
"""
    )
    assert _dangerous_writer_tokens(tree) == {
        "open",
        "write_text",
        "write_bytes",
        "os.rename",
        "shutil.move",
    }


def test_canonical_minute_writer_surface_has_explicit_allowlist_and_guards() -> None:
    """Prevent a new direct writer from bypassing Stage 6 audit/import gates."""

    reviewed_writer_functions = {
        "quantdesk/intraday/intraday_import.py": {
            "create_intraday_template",
            "import_intraday_1m_file",
        },
        "quantdesk/intraday/intraday_update_engine.py": {
            "_merge_and_write_unlocked",
        },
        "quantdesk/extended_staged_data_importer.py": {
            "_copy_file",
            "build_extended_staged_import_audit",
        },
        "quantdesk/intraday/stage6_append_only_import.py": {
            "_append_candidate",
            "_apply_audited_intraday_batch_locked",
        },
        "quantdesk/intraday/stage6_conflict_resolution.py": {
            "_resolve_stage6_intraday_conflicts_locked",
        },
        "quantdesk/intraday/stage6_duplicate_session_repair.py": {
            "_write_inventory_csv",
            "scan_intraday_duplicate_sessions",
            "_write_candidate",
            "_repair_intraday_duplicate_sessions_locked",
        },
        "quantdesk/intraday/intraday_replay_self_validation.py": {
            "_write_bars",
            "_write_report",
        },
        "scripts/generate_intraday_1m_import_template.py": {
            "generate_template",
        },
    }
    canonical_markers = (
        "INTRADAY_1M_DIR",
        "CANONICAL_INTRADAY_1M_DIR",
        "data/intraday/1m",
        "data\\intraday\\1m",
    )
    marker_writer_functions: set[tuple[str, str]] = set()
    for base in (ROOT / "quantdesk", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            relative = path.relative_to(ROOT).as_posix()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                segment = ast.get_source_segment(source, node) or ""
                if any(marker in segment for marker in canonical_markers) and (
                    _dangerous_writer_tokens(node)
                ):
                    marker_writer_functions.add((relative, node.name))

    reviewed_pairs = {
        (relative, function)
        for relative, functions in reviewed_writer_functions.items()
        for function in functions
    }
    assert marker_writer_functions <= reviewed_pairs

    for relative, approved_functions in reviewed_writer_functions.items():
        source = (ROOT / relative).read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=relative)
        actual_functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _dangerous_writer_tokens(node)
        }
        assert actual_functions <= approved_functions

    updater = (ROOT / "quantdesk/intraday/intraday_update_engine.py").read_text(
        encoding="utf-8"
    )
    assert "canonical_intraday_direct_write_forbidden" in updater
    assert "apply_audited_intraday_batch" in updater

    manual = (ROOT / "quantdesk/intraday/intraday_import.py").read_text(
        encoding="utf-8"
    )
    assert "canonical_intraday_direct_write_forbidden" in manual
    assert "apply_audited_intraday_batch" in manual

    extended = (ROOT / "quantdesk/extended_staged_data_importer.py").read_text(
        encoding="utf-8"
    )
    assert "blocked_requires_dedicated_intraday_importer" in extended

    replay = (
        ROOT / "quantdesk/intraday/intraday_replay_self_validation.py"
    ).read_text(encoding="utf-8")
    assert "ensure_directory_disjoint_from_canonical" in replay

    generator = (
        ROOT / "scripts/generate_intraday_1m_import_template.py"
    ).read_text(encoding="utf-8")
    assert "ensure_intraday_template_target_is_noncanonical" in generator


def test_no_production_caller_invokes_unlocked_intraday_writer() -> None:
    offenders: list[str] = []
    for base in (ROOT / "quantdesk", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path.name == "intraday_update_engine.py":
                continue
            source = path.read_text(encoding="utf-8")
            if "_merge_and_write_unlocked(" in source:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_section16_repair_clis_are_read_only_by_default() -> None:
    from scripts.audit_stage6_intraday_duplicates import build_parser as duplicate_parser
    from scripts.run_stage6_section16_label_repairs import build_parser as label_parser

    assert duplicate_parser().parse_args([]).apply is False
    assert label_parser().parse_args([]).apply is False


def test_all_authorized_canonical_minute_writers_share_one_lock() -> None:
    """Append and exceptional repair transactions must exclude one another."""

    sources = {
        relative: (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "quantdesk/intraday/stage6_append_only_import.py",
            "quantdesk/intraday/stage6_conflict_resolution.py",
            "quantdesk/intraday/stage6_duplicate_session_repair.py",
        )
    }
    combined = "\n".join(sources.values())
    assert "stage6_section15_resolution.lock" not in combined
    assert "stage6_section16_duplicate_repair.lock" not in combined
    assert all(
        "canonical_intraday_write_lock_path(" in source
        for source in sources.values()
    )

    from quantdesk.intraday.stage6_append_only_import import (
        canonical_intraday_write_lock_path,
    )

    assert canonical_intraday_write_lock_path(Path("canonical")) == (
        Path("canonical") / ".locks" / "stage6_append_only_batch.lock"
    )


def test_template_helpers_reject_any_target_inside_canonical_intraday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import intraday_import
    from scripts import generate_intraday_1m_import_template as generator

    canonical = tmp_path / "data" / "intraday" / "1m"
    canonical.mkdir(parents=True)
    target = canonical / "600000.csv"
    original = b"protected-canonical-bytes\n"
    target.write_bytes(original)
    monkeypatch.setattr(intraday_import, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(
        generator,
        "collect_intraday_required_symbols",
        lambda **_kwargs: {
            "items": [{"symbol": "600000", "name": "test"}],
            "target_trade_date": "2026-07-17",
        },
    )

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        intraday_import.create_intraday_template(target)
    assert target.read_bytes() == original

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        generator.generate_template(
            source="symbols",
            target_date="2026-07-17",
            output=target,
            symbols=["600000"],
            overwrite=True,
        )
    assert target.read_bytes() == original


def test_import_and_provider_status_outputs_cannot_overwrite_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import intraday_import, intraday_update_engine

    canonical = tmp_path / "data" / "intraday" / "1m"
    canonical.mkdir(parents=True)
    target = canonical / "600000.csv"
    original = b"protected-canonical-bytes\n"
    target.write_bytes(original)
    monkeypatch.setattr(intraday_import, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(intraday_update_engine, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(intraday_update_engine, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(
        intraday_update_engine,
        "_resolve_symbols",
        lambda *_args, **_kwargs: ([], "2026-07-17"),
    )

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        intraday_import.import_intraday_1m_file(
            tmp_path / "missing.csv",
            validate_only=True,
            output_path=target,
        )
    assert target.read_bytes() == original

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        intraday_update_engine.update_intraday_1m_for_symbols(
            symbols=[],
            target_date="2026-07-17",
            provider_order=[],
            source="symbols",
            intraday_dir=canonical,
            output_path=target,
            run_id="path-guard-test",
        )
    assert target.read_bytes() == original


def test_import_and_provider_update_reject_canonical_subdirectory_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import intraday_import, intraday_update_engine

    canonical = tmp_path / "data" / "intraday" / "1m"
    canonical.mkdir(parents=True)
    nested = canonical / "bypass"
    monkeypatch.setattr(intraday_import, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(intraday_update_engine, "INTRADAY_1M_DIR", canonical)
    monkeypatch.setattr(intraday_update_engine, "RESULTS_DIR", tmp_path / "results")

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        intraday_import.import_intraday_1m_file(
            tmp_path / "missing.csv",
            base_dir=nested,
            output_path=tmp_path / "import-result.json",
        )

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        intraday_update_engine.update_intraday_1m_for_symbols(
            symbols=[],
            target_date="2026-07-17",
            provider_order=[],
            source="symbols",
            intraday_dir=nested,
            output_path=tmp_path / "update-result.json",
        )

    assert not nested.exists()

def test_stage6_support_artifacts_cannot_be_written_inside_canonical(
    tmp_path: Path,
) -> None:
    from quantdesk.intraday.stage6_append_only_import import (
        apply_audited_intraday_batch,
    )
    from quantdesk.intraday.stage6_conflict_resolution import (
        resolve_stage6_intraday_conflicts,
    )
    from quantdesk.intraday.stage6_duplicate_session_repair import (
        repair_intraday_duplicate_sessions,
        scan_intraday_duplicate_sessions,
    )

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    protected = canonical / "600000.csv"
    original = b"protected-canonical-bytes\n"

    calls = [
        lambda: apply_audited_intraday_batch(
            staging_dir=tmp_path / "staging",
            canonical_dir=canonical,
            symbols=[],
            target_date="2026-07-17",
            run_id="artifact-guard-append",
            quality_audit={},
            output_path=protected,
            apply=False,
        ),
        lambda: resolve_stage6_intraday_conflicts(
            canonical_dir=canonical,
            staging_dir=tmp_path / "staging",
            symbols=[],
            target_date="2026-07-17",
            run_id="artifact-guard-conflict",
            backup_dir=tmp_path / "backups",
            output_path=protected,
            apply=False,
        ),
        lambda: repair_intraday_duplicate_sessions(
            canonical_dir=canonical,
            anomalies=[],
            backup_dir=tmp_path / "backups",
            output_path=protected,
            run_id="artifact-guard-duplicate",
            apply=False,
        ),
        lambda: scan_intraday_duplicate_sessions(
            canonical_dir=canonical,
            output_dir=canonical,
            run_id="artifact-guard-scan",
        ),
    ]
    for call in calls:
        protected.write_bytes(original)
        with pytest.raises(
            ValueError,
            match="canonical_intraday_direct_write_forbidden",
        ):
            call()
        assert protected.read_bytes() == original


def test_destructive_replay_runtime_rejects_canonical_intraday_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from quantdesk.intraday import intraday_replay_self_validation as replay

    protected_root = tmp_path / "data" / "intraday"
    target = protected_root / "1m" / "600000.csv"
    target.parent.mkdir(parents=True)
    original = b"protected-canonical-bytes\n"
    target.write_bytes(original)
    monkeypatch.setattr(replay, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(replay, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(replay, "REPORTS_DIR", tmp_path / "reports")

    with pytest.raises(ValueError, match="canonical_intraday_direct_write_forbidden"):
        replay.run_intraday_replay_self_validation(
            runtime_dir=protected_root,
            output_path=tmp_path / "result.json",
            execution_output_path=tmp_path / "execution.json",
            keep_artifacts=False,
        )
    assert target.read_bytes() == original
