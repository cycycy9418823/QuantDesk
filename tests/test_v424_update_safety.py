import json
import subprocess
import sys
from pathlib import Path
from tests._isolated_project import IsolatedProject

from quantdesk.intraday_snapshot_updater import run_intraday_snapshot_update
from quantdesk.v424_workflow import run_v424_workflow


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PATHS = [
    "data/daily",
    "results/portfolio_daily_equity_rebuild_latest.csv",
    "results/low_vol_canonical_daily_equity_latest.csv",
    "results/rev120_bcanonical_daily_equity_latest.csv",
]


def test_intraday_snapshot_network_false_does_not_write_daily():
    payload = run_intraday_snapshot_update(ROOT, network=False, max_symbols=5)

    assert payload["writes_data_daily"] is False
    assert payload["rows_written"] == 0


def test_v424_full_outputs_and_boundaries(tmp_path):
    with IsolatedProject(ROOT, temp_parent=tmp_path) as project:
        root = project.root
        manifest_path = root / "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_manifest.json"
        result = run_v424_workflow(root, network=False, max_symbols=5, dry_run=True)
        handoff = json.loads((root / "results/data_update/v424_work_start_auto_update_gate_handoff_latest.json").read_text(encoding="utf-8-sig"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

        assert handoff["candidate_upgraded"] is False
        assert handoff["generated_trade_advice"] is False
        assert handoff["modified_data_daily"] is False
        assert manifest["missing_files"] == []
        assert manifest["research_only"] is True


        assert result["cli_fix"]["cli_help_works"] is True
        assert result["cli_fix"]["dry_run_works"] is True

def test_protected_paths_not_modified():
    proc = subprocess.run(["git", "status", "--porcelain=v1", "-uall", "--", *PROTECTED_PATHS], cwd=ROOT, text=True, capture_output=True, check=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
