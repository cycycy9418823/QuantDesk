import json
import subprocess
import sys
from pathlib import Path

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


def test_v424_full_outputs_and_boundaries():
    manifest_path = ROOT / "dist/quantdesk_v4.2.4_work_start_auto_update_gate_review_manifest.json"
    if not manifest_path.exists():
        run_v424_workflow(ROOT, network=False, max_symbols=5, dry_run=True)
    handoff = json.loads((ROOT / "results/data_update/v424_work_start_auto_update_gate_handoff_latest.json").read_text(encoding="utf-8-sig"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    assert handoff["candidate_upgraded"] is False
    assert handoff["generated_trade_advice"] is False
    assert handoff["modified_data_daily"] is False
    assert manifest["missing_files"] == []
    assert manifest["research_only"] is True


def test_v421_v422_gate_scripts_help():
    for script in ["run_v421_daily_open_workflow.py", "run_v422_daily_research_long_loop.py", "run_v424_work_start_update_gate.py"]:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"], cwd=ROOT, text=True, capture_output=True, check=False)
        assert proc.returncode == 0


def test_protected_paths_not_modified():
    proc = subprocess.run(["git", "status", "--porcelain=v1", "-uall", "--", *PROTECTED_PATHS], cwd=ROOT, text=True, capture_output=True, check=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
