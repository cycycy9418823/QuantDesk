from quantdesk.update_checkpoint import build_checkpoint


def test_v426_checkpoint_resume_command():
    payload = build_checkpoint(run_id="run1", started_at="now", last_symbol_processed="000006", completed_symbols=["000006"], failed_symbols=[], skipped_symbols=[], rows_written=1, max_symbols=10, priority_only=True)

    assert payload["can_resume"] is True
    assert "--resume true" in payload["resume_command"]
    assert "--start-after 000006" in payload["resume_command"]
    assert payload["research_only"] is True
