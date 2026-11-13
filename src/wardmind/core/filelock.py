"""One writer at a time across processes: the web app, the desktop app, the MCP server and the CLI.

CONTRACT FILE. store.WRITE_LOCK is a ProcessWriteLock. It is a thread lock (re-entrant, as
before) plus an OS file lock on LOCK_FILE in the per-user root folder (secrets.root_dir()), taken
when a thread first enters and released when it last leaves. So the roughly 100 existing
`with WRITE_LOCK:` sites keep working unchanged, and a load -> change -> save in the MCP server
can no longer undo one made in the web app at the same moment (or the other way round).

Standard library only: msvcrt.locking on Windows, fcntl.flock elsewhere. The OS drops the lock
when a process dies, so a crashed process never leaves it stuck. The folder is read each time
the lock is first taken, so tests that point WARDMIND_* at a temporary folder get their own lock
file. A root folder that doesn't exist yet has nothing in it to protect, so then only the thread
lock is taken.

A wait longer than WAIT_SECONDS raises StoreBusy, an OSError, so callers that already catch
OSError show a plain message instead of hanging.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO

from wardmind.core.secrets import root_dir

LOCK_FILE = ".write.lock"  # a dotfile, so the vault and backups never pick it up
WAIT_SECONDS = 15.0
_POLL_SECONDS = 0.02

BUSY_MESSAGE = "Wardmind is busy saving in another window. Try again in a moment."


class StoreBusy(OSError):
    """Another process held the write lock for longer than WAIT_SECONDS."""

    def __init__(self) -> None:
        super().__init__(BUSY_MESSAGE)


def _lock_handle(handle: IO[bytes]) -> bool:
    """Try to take the OS lock on byte 0 without waiting; True when taken."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_handle(handle: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProcessWriteLock:
    """A re-entrant lock shared by every thread in this process and, through a lock file, by
    every Wardmind process run by this person. Use it as `with WRITE_LOCK:`; acquire() and
    release() exist for the rare caller that needs them. `folder` fixes the lock file's folder
    (tests); by default it is secrets.root_dir(), read on each first entry."""

    def __init__(self, folder: Path | None = None, *, wait_seconds: float = WAIT_SECONDS) -> None:
        self._thread_lock = threading.RLock()
        self._folder = folder
        self._wait = wait_seconds
        self._depth = 0
        self._handle: IO[bytes] | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Like threading.RLock.acquire: False when the thread lock isn't free in time. The file
        lock then waits up to WAIT_SECONDS (StoreBusy after that)."""
        if not self._thread_lock.acquire(blocking, timeout):
            return False
        if self._depth == 0:
            try:
                self._handle = self._take_file_lock()
            except BaseException:
                self._thread_lock.release()
                raise
        self._depth += 1
        return True

    def release(self) -> None:
        if self._depth <= 0:
            raise RuntimeError("release() called on a lock that isn't held")
        self._depth -= 1
        if self._depth == 0:
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    _unlock_handle(handle)
                except OSError:
                    pass  # closing the handle drops the lock anyway
                finally:
                    handle.close()
        self._thread_lock.release()

    def __enter__(self) -> ProcessWriteLock:
        self.acquire()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.release()

    @property
    def held(self) -> bool:
        """Whether some thread in this process holds it (for tests and assertions)."""
        return self._depth > 0

    def _take_file_lock(self) -> IO[bytes] | None:
        folder = self._folder if self._folder is not None else root_dir()
        if not folder.is_dir():
            return None
        path = folder / LOCK_FILE
        try:
            handle = open(path, "a+b")  # noqa: SIM115 - held open until release()
        except OSError:
            return None  # a read-only folder can't be written to anyway; the thread lock remains
        deadline = time.monotonic() + self._wait
        while True:
            if _lock_handle(handle):
                return handle
            if time.monotonic() >= deadline:
                handle.close()
                raise StoreBusy()
            time.sleep(_POLL_SECONDS)
