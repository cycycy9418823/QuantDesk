from datetime import date
from pathlib import Path

from quantdesk.bounded_data_updater import bounded_update_attempt


ROOT = Path(__file__).resolve().parents[1]


def test_network_false_does_not_attempt_provider_update():
    payload = bounded_update_attempt(ROOT, network=False)

    assert payload["attempted"] is False
    assert payload["provider_used"] == "none"
    assert payload["rows_written"] == 0
    assert payload["protected_paths_unchanged"] is True


def test_skip_update_does_not_crash_or_write_rows():
    payload = bounded_update_attempt(ROOT, network=True, skip_update=True)

    assert payload["attempted"] is False
    assert payload["rows_written"] == 0
    assert payload["status"] == "skipped_fresh_or_requested"

