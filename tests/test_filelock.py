"""core/filelock.py: store.WRITE_LOCK keeps one writer at a time across processes, not only
threads, so the MCP server and the web app can't undo each other's saves."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from wardmind.core import filelock, store
from wardmind.core.filelock import LOCK_FILE, ProcessWriteLock, StoreBusy

_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from wardmind.core.filelock import ProcessWriteLock
    lock = ProcessWriteLock(Path(sys.argv[1]))
    with lock:
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
    print("released", flush=True)
    """
)

_COUNTER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from wardmind.core.filelock import ProcessWriteLock
    folder = Path(sys.argv[1])
    lock = ProcessWriteLock(folder)
    counter = folder / "counter.txt"
    for _ in range(int(sys.argv[2])):
        with lock:
            value = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(value + 1))
    """
)


def _python(script: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_write_lock_is_a_process_lock() -> None:
    assert isinstance(store.WRITE_LOCK, ProcessWriteLock)


def test_reentrant_in_one_thread(tmp_path: Path) -> None:
    lock = ProcessWriteLock(tmp_path)
    with lock, lock, lock:
        assert lock.held
    assert not lock.held


def test_lock_file_is_a_dotfile_in_the_folder(tmp_path: Path) -> None:
    with ProcessWriteLock(tmp_path):
        assert (tmp_path / LOCK_FILE).exists()
    assert LOCK_FILE.startswith(".")


def test_missing_folder_takes_only_the_thread_lock(tmp_path: Path) -> None:
    folder = tmp_path / "not-there"
    with ProcessWriteLock(folder) as lock:
        assert lock.held
    assert not folder.exists()


def test_default_folder_is_the_root_folder_of_the_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filelock, "root_dir", lambda: tmp_path)
    with ProcessWriteLock():
        assert (tmp_path / LOCK_FILE).exists()


def test_release_without_acquire_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        ProcessWriteLock(tmp_path).release()


def test_threads_take_turns(tmp_path: Path) -> None:
    lock = ProcessWriteLock(tmp_path)
    counter = {"value": 0}

    def bump() -> None:
        for _ in range(200):
            with lock:
                current = counter["value"]
                counter["value"] = current + 1

    workers = [threading.Thread(target=bump) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert counter["value"] == 800


def test_another_process_holding_it_makes_us_wait_then_busy(tmp_path: Path) -> None:
    holder = _python(_HOLDER, str(tmp_path), "3")
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(StoreBusy) as caught, ProcessWriteLock(tmp_path, wait_seconds=0.3):
            pass
        assert isinstance(caught.value, OSError)
        assert str(caught.value) == filelock.BUSY_MESSAGE
        # The thread lock was released with the failure, so a later wait succeeds.
        with ProcessWriteLock(tmp_path, wait_seconds=10) as lock:
            assert lock.held
    finally:
        holder.wait(timeout=30)


def test_a_dead_process_never_leaves_it_stuck(tmp_path: Path) -> None:
    holder = _python(_HOLDER, str(tmp_path), "60")
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    holder.kill()
    holder.wait(timeout=30)
    with ProcessWriteLock(tmp_path, wait_seconds=10) as lock:
        assert lock.held


def test_two_processes_lose_no_updates(tmp_path: Path) -> None:
    runs = [_python(_COUNTER, str(tmp_path), "150") for _ in range(2)]
    for run in runs:
        _, errors = run.communicate(timeout=120)
        assert run.returncode == 0, errors
    assert (tmp_path / "counter.txt").read_text() == "300"
