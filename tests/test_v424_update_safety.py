"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


import subprocess


from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


PROTECTED_PATHS = [
    "data/daily",
    "results/portfolio_daily_equity_rebuild_latest.csv",
    "results/low_vol_canonical_daily_equity_latest.csv",
    "results/rev120_bcanonical_daily_equity_latest.csv",
]


def test_protected_paths_not_modified():
    proc = subprocess.run(["git", "status", "--porcelain=v1", "-uall", "--", *PROTECTED_PATHS], cwd=ROOT, text=True, capture_output=True, check=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
