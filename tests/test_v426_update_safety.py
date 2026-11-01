"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


import subprocess


from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v426_canonical_equity_not_modified():
    proc = subprocess.run(["git", "status", "--porcelain=v1", "--", "results/portfolio_daily_equity_rebuild_latest.csv", "results/low_vol_canonical_daily_equity_latest.csv", "results/rev120_bcanonical_daily_equity_latest.csv"], cwd=ROOT, text=True, capture_output=True, check=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
