"""The locking rules in web/lock.py, with no app and no real waiting: the open paths, idle time, the
unlock throttle, the background watch and the wording of the refusal."""

from __future__ import annotations

import logging
import threading

import pytest

from wardmind.web import lock


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Open paths -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/unlock", "/enter", "/static/css/app.css", "/static/"])
def test_the_unlock_page_the_sign_in_link_and_static_files_stay_open(path: str) -> None:
    assert lock.is_lock_open_path(path)


@pytest.mark.parametrize(
    "path",
    ["/", "/today", "/lock", "/unlock/", "/unlocked", "/enter/x", "/static", "//unlock"],
)
def test_every_other_path_is_gated(path: str) -> None:
    assert not lock.is_lock_open_path(path)


# --- Idle time --------------------------------------------------------------------------------


def test_the_idle_timer_expires_at_the_setting_and_not_before() -> None:
    clock = FakeClock()
    timer = lock.IdleTimer(clock)
    clock.advance(14 * 60)
    assert not timer.expired(minutes=15)
    clock.advance(60)
    assert timer.expired(minutes=15)


def test_touching_the_timer_restarts_the_idle_time() -> None:
    clock = FakeClock()
    timer = lock.IdleTimer(clock)
    clock.advance(14 * 60)
    timer.touch()
    clock.advance(14 * 60)
    assert not timer.expired(minutes=15)


def test_zero_minutes_never_expires() -> None:
    clock = FakeClock()
    timer = lock.IdleTimer(clock)
    clock.advance(10 * 24 * 3600)
    assert not timer.expired(minutes=0)


def test_an_explicit_now_is_used_instead_of_the_clock() -> None:
    timer = lock.IdleTimer(FakeClock(1000.0))
    assert timer.expired(now=1000.0 + 15 * 60, minutes=15)
    assert not timer.expired(now=1000.0 + 15 * 60 - 1, minutes=15)


@pytest.mark.parametrize(
    ("idle", "minutes", "unlocked", "encrypted", "expected"),
    [
        (899, 15, True, True, False),  # just under the setting
        (900, 15, True, True, True),  # at the setting
        (10_000, 15, False, True, False),  # already locked
        (10_000, 15, True, False, False),  # a plain book is never locked
        (10_000, 0, True, True, False),  # never
        (299, 5, True, True, False),
        (300, 5, True, True, True),
    ],
)
def test_should_lock_only_an_unlocked_encrypted_book_idle_past_the_setting(
    idle: float, minutes: int, unlocked: bool, encrypted: bool, expected: bool
) -> None:
    assert (
        lock.should_lock(
            now=idle,
            last_activity=0.0,
            minutes=minutes,
            unlocked=unlocked,
            encrypted=encrypted,
        )
        is expected
    )


# --- The unlock throttle ----------------------------------------------------------------------


def test_the_first_five_wrong_tries_have_no_wait_and_the_fifth_starts_one() -> None:
    throttle = lock.UnlockThrottle(FakeClock())
    for _ in range(lock.FREE_FAILURES - 1):
        throttle.failed()
        assert throttle.seconds_left() == 0
    throttle.failed()
    assert throttle.seconds_left() == 2


def test_each_further_wrong_try_doubles_the_wait_up_to_a_minute() -> None:
    clock = FakeClock()
    throttle = lock.UnlockThrottle(clock)
    for _ in range(lock.FREE_FAILURES):
        throttle.failed()
    waits = []
    for _ in range(8):
        wait = throttle.seconds_left()
        waits.append(wait)
        clock.advance(wait)  # the next try, once it is allowed
        throttle.failed()
    assert waits == [2, 4, 8, 16, 32, 60, 60, 60]


def test_a_refused_try_reports_the_whole_seconds_left() -> None:
    clock = FakeClock()
    throttle = lock.UnlockThrottle(clock)
    for _ in range(lock.FREE_FAILURES):
        throttle.failed()
    clock.advance(0.5)
    assert throttle.seconds_left() == 2  # 1.5 s left, rounded up
    clock.advance(0.5)
    assert throttle.seconds_left() == 1
    clock.advance(1)
    assert throttle.seconds_left() == 0


def test_refused_tries_do_not_lengthen_the_wait() -> None:
    clock = FakeClock()
    throttle = lock.UnlockThrottle(clock)
    for _ in range(lock.FREE_FAILURES):
        throttle.failed()
    for _ in range(10):
        throttle.seconds_left()
    clock.advance(2)
    assert throttle.seconds_left() == 0


def test_a_success_clears_the_count_and_the_wait() -> None:
    clock = FakeClock()
    throttle = lock.UnlockThrottle(clock)
    for _ in range(lock.FREE_FAILURES + 1):
        throttle.failed()
    assert throttle.seconds_left() == 4
    throttle.succeeded()
    assert throttle.seconds_left() == 0
    for _ in range(lock.FREE_FAILURES - 1):
        throttle.failed()
        assert throttle.seconds_left() == 0


@pytest.mark.parametrize(
    ("seconds", "message"),
    [
        (1, "Too many tries. Wait 1 second, then try again."),
        (2, "Too many tries. Wait 2 seconds, then try again."),
        (60, "Too many tries. Wait 60 seconds, then try again."),
    ],
)
def test_the_refusal_says_how_many_seconds_to_wait(seconds: int, message: str) -> None:
    assert lock.too_many_tries(seconds) == message


# --- The background watch ---------------------------------------------------------------------


def test_the_watch_runs_its_check_until_it_is_stopped() -> None:
    ran = threading.Event()
    watch = lock.IdleWatch(ran.set, interval=0.01)
    watch.start()
    try:
        assert ran.wait(5)
        assert watch.running
    finally:
        watch.stop()
    assert not watch.running


def test_starting_a_running_watch_starts_no_second_thread() -> None:
    watch = lock.IdleWatch(lambda: None, interval=0.01)
    watch.start()
    try:
        first = watch._thread
        watch.start()
        assert watch._thread is first
    finally:
        watch.stop()


def test_a_failing_check_is_logged_by_its_type_only(caplog: pytest.LogCaptureFixture) -> None:
    def check() -> None:
        raise ValueError("a secret detail")

    with caplog.at_level(logging.WARNING, logger="wardmind.web.lock"):
        lock.IdleWatch(check).tick()  # does not raise
    assert "Idle check failed (ValueError)" in caplog.text
    assert "a secret detail" not in caplog.text
