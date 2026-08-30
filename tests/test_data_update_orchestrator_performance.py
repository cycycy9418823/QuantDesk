from __future__ import annotations

from quantdesk import data_update_orchestrator as orchestrator


def test_offline_preflight_reuses_one_full_freshness_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    daily_dir = tmp_path / "data" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "600000.csv").write_text(
        "date,symbol,close\n2026-05-20,600000,10.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        orchestrator,
        "resolve_symbols",
        lambda *_args, **_kwargs: [
            {"symbol": "600000", "name": "600000", "included": "true"}
        ],
    )

    real_freshness_check = orchestrator.freshness_check
    freshness_calls = 0

    def counting_freshness_check(*args, **kwargs):
        nonlocal freshness_calls
        freshness_calls += 1
        return real_freshness_check(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator,
        "freshness_check",
        counting_freshness_check,
    )

    payload = orchestrator.run_data_update_preflight(
        tmp_path,
        target_date="2026-05-21",
        network=False,
        max_symbols=1,
        write=False,
    )

    assert payload["network"] is False
    assert payload["symbols_checked"] == ["600000"]
    assert freshness_calls == 1
