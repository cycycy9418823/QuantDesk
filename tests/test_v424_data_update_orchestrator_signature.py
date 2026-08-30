import inspect
import subprocess
import sys
from pathlib import Path

from quantdesk.data_update_orchestrator import run_data_update_preflight


ROOT = Path(__file__).resolve().parents[1]


def test_data_update_preflight_signature_accepts_v424_parameters():
    sig = inspect.signature(run_data_update_preflight)

    for name in ["target_date", "auto_update", "scope", "network", "max_symbols", "dry_run", "provider", "timeout_seconds", "max_retries", "write_mode"]:
        assert name in sig.parameters


def test_data_update_dry_run_cli_works_without_type_error():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_data_update.py"), "--dry-run", "--network", "false", "--scope", "daily", "--max-symbols", "5", "--json", "true"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "TypeError" not in proc.stderr
    assert '"dry_run": true' in proc.stdout


def test_data_update_help_works():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/run_data_update.py"), "--help"], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)

    assert proc.returncode == 0
    assert "--dry-run" in proc.stdout
