"""Universe degrade must trust the on-disk approved list, not the latest attempt.

Tonight's failure mode: a successful refresh wrote the CSV, then a later
same-day attempt failed, restored the CSV, and overwrote the daily report
with accepted=false. The pipeline then treated the list as missing.

This is not "any CSV younger than 14 days". The list must:
- use only approved sources
- have updated_at within UNIVERSE_MAX_STALENESS_DAYS
- hash-bind to evidence that is either accepted or restored_after_failure
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_data_close_pipeline import (  # noqa: E402
    STAGE_ORDER,
    UNIVERSE_MAX_STALENESS_DAYS,
    StageResult,
    _universe_staleness_days,
    run_pipeline,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
APPROVED = "stock_info_a_code_name"
TODAY = datetime.now(SHANGHAI).strftime("%Y-%m-%d")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(root: Path, *, source: str, updated_at: str, rows: int = 20) -> Path:
    path = root / "data" / "universe" / "a_share_symbols.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["symbol,name,source,updated_at"]
    for index in range(rows):
        lines.append(f"{index:06d},Name{index},{source},{updated_at}T17:37:57")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_spec(root: Path) -> None:
    path = root / "config" / "weekly_daily_wide_pool_contract.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "rules": {
                    "approved_market_universe_sources": [
                        "baostock.query_stock_basic",
                        "stock_info_a_code_name",
                        "stock_zh_a_spot",
                        "stock_zh_a_spot_em",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def _write_report(
    root: Path,
    day: str,
    *,
    accepted: bool,
    csv_path: Path,
    source: str,
    updated_at: str,
    restored: bool = False,
) -> None:
    stamp = day.replace("-", "")
    dest = root / "results" / "universe_baseline_refresh" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    info = {
        "rows": 20,
        "sources": [source],
        "updated_at_dates": [updated_at[:10]],
        "sha256": _sha(csv_path),
    }
    (dest / "universe_baseline_refresh.json").write_text(
        json.dumps(
            {
                "accepted": accepted,
                "restored_after_failure": restored,
                "after": info,
                "before": info,
            }
        ),
        encoding="utf-8",
    )


def _skipped() -> dict:
    return {
        name: (
            lambda *a, n=name, **k: StageResult(
                stage=n, status="skipped", started_at="t"
            )
        )
        for name in STAGE_ORDER
    }


def test_restored_same_day_failure_still_sees_approved_csv(tmp_path: Path) -> None:
    _write_spec(tmp_path)
    csv_path = _write_csv(tmp_path, source=APPROVED, updated_at=TODAY)
    _write_report(
        tmp_path,
        TODAY,
        accepted=False,
        csv_path=csv_path,
        source=APPROVED,
        updated_at=TODAY,
        restored=True,
    )

    age = _universe_staleness_days(tmp_path)
    assert age == 0

    def universe_failed(*_a, **_k):
        return StageResult(
            stage="universe",
            status="failed",
            started_at="t",
            reason_codes=["universe_refresh_not_accepted"],
        )

    calls: list[str] = []

    def market(*_a, **_k):
        calls.append("market_refresh")
        return StageResult(stage="market_refresh", status="completed", started_at="t")

    report = run_pipeline(
        tmp_path,
        network=True,
        run_id="uni-restore",
        target_date=TODAY,
        stage_runner={
            **_skipped(),
            "universe": universe_failed,
            "market_refresh": market,
        },
        only_stages=["universe", "market_refresh"],
    )
    assert report["stages"]["universe"]["status"] == "completed_with_warnings"
    assert any(
        "universe_degraded" in item
        for item in report["stages"]["universe"]["reason_codes"]
    )
    assert report["stages"]["market_refresh"]["status"] == "completed"
    assert calls == ["market_refresh"]


def test_local_cache_source_is_not_usable_even_if_fresh(tmp_path: Path) -> None:
    _write_spec(tmp_path)
    csv_path = _write_csv(tmp_path, source="local_daily_cache", updated_at=TODAY)
    _write_report(
        tmp_path,
        TODAY,
        accepted=False,
        csv_path=csv_path,
        source="local_daily_cache",
        updated_at=TODAY,
        restored=True,
    )
    assert _universe_staleness_days(tmp_path) is None

    report = run_pipeline(
        tmp_path,
        network=True,
        run_id="uni-cache",
        target_date=TODAY,
        stage_runner={
            **_skipped(),
            "universe": lambda *_a, **_k: StageResult(
                stage="universe", status="failed", started_at="t"
            ),
            "market_refresh": lambda *_a, **_k: StageResult(
                stage="market_refresh", status="completed", started_at="t"
            ),
        },
        only_stages=["universe", "market_refresh"],
    )
    assert report["stages"]["universe"]["status"] == "blocked"
    assert "universe_unusable" in report["stages"]["market_refresh"]["reason_codes"]
    assert report["stages"]["market_refresh"]["status"] == "blocked"


def test_restored_flag_with_sha_mismatch_is_not_usable(tmp_path: Path) -> None:
    _write_spec(tmp_path)
    csv_path = _write_csv(tmp_path, source=APPROVED, updated_at=TODAY)
    _write_report(
        tmp_path,
        TODAY,
        accepted=False,
        csv_path=csv_path,
        source=APPROVED,
        updated_at=TODAY,
        restored=True,
    )
    report_path = (
        tmp_path
        / "results"
        / "universe_baseline_refresh"
        / TODAY.replace("-", "")
        / "universe_baseline_refresh.json"
    )
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    doc["after"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(doc), encoding="utf-8")
    assert _universe_staleness_days(tmp_path) is None


def test_stale_approved_csv_retains_its_age_for_degraded_use(tmp_path: Path) -> None:
    _write_spec(tmp_path)
    old = (datetime.now(SHANGHAI) - timedelta(days=UNIVERSE_MAX_STALENESS_DAYS + 1)).strftime(
        "%Y-%m-%d"
    )
    csv_path = _write_csv(tmp_path, source=APPROVED, updated_at=old)
    _write_report(
        tmp_path,
        old,
        accepted=True,
        csv_path=csv_path,
        source=APPROVED,
        updated_at=old,
    )
    assert _universe_staleness_days(tmp_path) == UNIVERSE_MAX_STALENESS_DAYS + 1


def test_older_accepted_report_matching_csv_hash_is_usable(tmp_path: Path) -> None:
    _write_spec(tmp_path)
    yesterday = (datetime.now(SHANGHAI) - timedelta(days=1)).strftime("%Y-%m-%d")
    csv_path = _write_csv(tmp_path, source=APPROVED, updated_at=yesterday)
    _write_report(
        tmp_path,
        yesterday,
        accepted=True,
        csv_path=csv_path,
        source=APPROVED,
        updated_at=yesterday,
    )
    _write_report(
        tmp_path,
        TODAY,
        accepted=False,
        csv_path=csv_path,
        source=APPROVED,
        updated_at=yesterday,
        restored=False,
    )
    # today's rejected report still carries the same after.sha256 (failed before write)
    assert _universe_staleness_days(tmp_path) == 1
