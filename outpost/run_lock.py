import contextlib
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path

RUN_PID_FILE = Path.home() / ".outpost-run.pid"


@contextlib.contextmanager
def exclusive_run() -> Iterator[int | None]:
    previous_pid = None
    try:
        previous_pid = int(RUN_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pass

    if previous_pid and previous_pid != os.getpid():
        try:
            os.kill(previous_pid, signal.SIGTERM)
            time.sleep(0.2)
        except OSError:
            previous_pid = None

    RUN_PID_FILE.write_text(f"{os.getpid()}\n")
    try:
        yield previous_pid
    finally:
        # Don't remove a PID file that a newer run has already replaced.
        try:
            if int(RUN_PID_FILE.read_text().strip()) == os.getpid():
                RUN_PID_FILE.unlink()
        except (OSError, ValueError):
            pass
