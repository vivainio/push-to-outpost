import os
import signal
from unittest.mock import MagicMock

from outpost import run_lock


def test_exclusive_run_records_and_removes_current_pid(tmp_path, monkeypatch):
    path = tmp_path / "run.pid"
    monkeypatch.setattr(run_lock, "RUN_PID_FILE", path)

    with run_lock.exclusive_run() as previous_pid:
        assert previous_pid is None
        assert path.read_text().strip() == str(os.getpid())

    assert not path.exists()


def test_exclusive_run_terminates_previous_pid(tmp_path, monkeypatch):
    path = tmp_path / "run.pid"
    path.write_text("4321\n")
    kill = MagicMock()
    monkeypatch.setattr(run_lock, "RUN_PID_FILE", path)
    monkeypatch.setattr(run_lock.os, "kill", kill)
    monkeypatch.setattr(run_lock.time, "sleep", lambda seconds: None)

    with run_lock.exclusive_run() as previous_pid:
        assert previous_pid == 4321

    kill.assert_called_once_with(4321, signal.SIGTERM)
