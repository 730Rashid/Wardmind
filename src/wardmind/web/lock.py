"""Locking the web app: the rules, with no I/O. web/app.py wires them into the lock gate.

- Paths that stay open while the book is locked: the unlock page, the sign-in link, static files.
- Idle time: when the person last used Wardmind, and whether that is long enough to lock.
- Unlock tries: after a few wrong passwords or recovery keys, each further try waits longer.
- A daemon thread that runs a check every so often, for the idle lock when nobody is clicking.

Every clock is injectable (time.monotonic by default), so the tests never wait.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from wardmind.web.session import ENTER_PATH

_log = logging.getLogger(__name__)

Clock = Callable[[], float]

UNLOCK_PATH = "/unlock"
OPEN_PATHS = frozenset({UNLOCK_PATH, ENTER_PATH})
OPEN_PREFIXES = ("/static/",)
MINUTE_SECONDS = 60
CHECK_SECONDS = 30  # how often the idle check runs when nobody is sending requests
STOP_WAIT_SECONDS = 5
FREE_FAILURES = 5  # unlock tries that may fail before any wait applies
FIRST_WAIT_SECONDS = 2
MAX_WAIT_SECONDS = 60
LOCKED_WRITE_MESSAGE = "Wardmind is locked. Unlock it, then try again."


def is_lock_open_path(path: str) -> bool:
    """True for the paths served while the book is locked: the unlock page, the sign-in link and
    static files. Exact matches only, so "/unlock/x" or "/unlocked" are not open."""
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def idle_past_limit(now: float, last_activity: float, minutes: int) -> bool:
    """True when the person has been idle for `minutes` or more. Zero (or less) means never."""
    return minutes > 0 and now - last_activity >= minutes * MINUTE_SECONDS


def should_lock(
    now: float, last_activity: float, minutes: int, unlocked: bool, encrypted: bool
) -> bool:
    """Whether the background check should lock the book: it is encrypted and unlocked, and idle
    past the setting. A plain book is never locked; neither is one that is already locked."""
    return encrypted and unlocked and idle_past_limit(now, last_activity, minutes)


class IdleTimer:
    """When the person last used Wardmind, read from `clock`. The app touches it on every request
    that passes the lock gate (see web/app.py), so a page that is still loading counts as use."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._last = clock()

    @property
    def last(self) -> float:
        return self._last

    def now(self) -> float:
        return self._clock()

    def touch(self, now: float | None = None) -> None:
        self._last = self.now() if now is None else now

    def expired(self, now: float | None = None, *, minutes: int) -> bool:
        """True when the last use is `minutes` or more before `now` (the clock's reading if
        omitted). Zero minutes never expires."""
        return idle_past_limit(self.now() if now is None else now, self._last, minutes)


class UnlockThrottle:
    """Slows guessing at the password and the recovery key.

    The first FREE_FAILURES wrong tries have no wait. After that, each wrong try blocks the next
    try for a wait that doubles: 2 s, 4 s, 8 s ... up to 60 s. A refused try is not a failure, so
    it doesn't lengthen the wait. A successful unlock clears everything.

    One try is checked at a time (begin/end): tries sent together would otherwise each pass the
    wait check before any failure is counted, and each run the password's costly key derivation.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._failures = 0
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._busy = threading.Lock()

    def begin(self) -> bool:
        """Claim the one try that may run now. False while another try is being checked; the
        caller answers "wait" without trying. A True must be followed by end()."""
        return self._busy.acquire(blocking=False)

    def end(self) -> None:
        self._busy.release()

    def seconds_left(self) -> int:
        """Whole seconds (rounded up) until a try is allowed; 0 when one may be made now."""
        with self._lock:
            remaining = self._blocked_until - self._clock()
        return math.ceil(remaining) if remaining > 0 else 0

    def failed(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= FREE_FAILURES:
                steps = min(self._failures - FREE_FAILURES, 10)  # 2 * 2**10 is past the cap
                wait = min(MAX_WAIT_SECONDS, FIRST_WAIT_SECONDS * 2**steps)
                self._blocked_until = self._clock() + wait

    def succeeded(self) -> None:
        with self._lock:
            self._failures = 0
            self._blocked_until = 0.0


def too_many_tries(seconds: int) -> str:
    """The message for a refused try, with the seconds left."""
    unit = "second" if seconds == 1 else "seconds"
    return f"Too many tries. Wait {seconds} {unit}, then try again."


class IdleWatch:
    """Runs `check` every `interval` seconds on a daemon thread until stop(). A check that raises
    is logged by its exception type only, and the watch goes on. start() does nothing while the
    thread is running."""

    def __init__(self, check: Callable[[], None], *, interval: float = CHECK_SECONDS) -> None:
        self._check = check
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._stop,), name="wardmind-idle-lock", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STOP_WAIT_SECONDS)

    def tick(self) -> None:
        """One check, as the thread runs it. A failure is logged, never raised."""
        try:
            self._check()
        except Exception as error:
            _log.warning("Idle check failed (%s); it will try again.", type(error).__name__)

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval):
            self.tick()
