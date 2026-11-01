from scripts.run_exit_rule_comparison import main


def test_unsafe_exit_replay_is_refused_without_writing_output(tmp_path, capsys):
    destination = tmp_path / "report.md"
    status = main(["--out-md", str(destination)])
    assert status == 2
    assert not destination.exists()
    message = capsys.readouterr().err
    assert "同日" in message
    assert "收益" in message
    assert "未完成" in message
