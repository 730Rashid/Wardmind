"""The trust checks (core/health.py): each check's state and words, what makes a feed overdue, the
freshness data behind the page's line, the banner, the scheduler's heartbeat, the daily runs, and
what a locked or demo book shows.

Runs against the temp folders from conftest with fixed times, so the same checks come out whatever
the clock says. The times the page shows are this computer's, so tests build expected text with the
module's own stamp()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wardmind.core import health, history, mailbox, runlog, service, vault
from wardmind.core.feeds.models import FeedRole, FeedSettings, FeedStatus, Schedule
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import JOURNAL_FILE, ConnectionConfig, ConnectionKind, LocalStore

NOW = datetime(2026, 11, 12, 14, 5)
TODAY = NOW.replace(tzinfo=UTC).astimezone().date()
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def scheduler_not_running() -> None:
    runlog.scheduler_stopped()
    yield
    runlog.scheduler_stopped()


@pytest.fixture
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


@pytest.fixture
def demo() -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()


def _store() -> LocalStore:
    return service.get_store()


def _feed(
    store: LocalStore,
    *,
    name: str = "Usage",
    schedule: Schedule = Schedule.HOURLY,
    status: FeedStatus | None = None,
    enabled: bool = True,
) -> ConnectionConfig:
    config = store.add_connection(ConnectionKind.FEED, name)
    feed = FeedSettings(
        provider="file",
        role=FeedRole.SIGNALS,
        settings={},
        schedule=schedule,
        status=status or FeedStatus(),
    )
    updated = config.model_copy(update={"feed": feed, "enabled": enabled})
    store.update_connection(updated)
    return updated


def _check(found: list[health.Check], name: str) -> health.Check:
    matches = [check for check in found if check.name == name]
    assert len(matches) == 1, [check.name for check in found]
    return matches[0]


def _names(found: list[health.Check]) -> list[str]:
    return [check.name for check in found]


def _synced(at: datetime) -> FeedStatus:
    return FeedStatus(last_synced_at=at, last_attempt_at=at, rows=3, accounts=3)


# --- Demo data -----------------------------------------------------------------------------------


def test_demo_data_is_all_ok_and_says_it_is_demo(demo: None) -> None:
    found = health.checks(NOW, TODAY)
    assert found, "the demo book still has checks"
    assert all(check.state is health.State.OK for check in found)
    assert _check(found, "Your notes").detail.startswith("Demo data")
    assert _check(found, "Score history").detail.startswith("Demo data")
    assert _check(found, "AI summaries").detail.startswith("Off")
    assert health.freshness(NOW, TODAY).sources == []
    assert health.banner(NOW, TODAY) == []


# --- Feeds ---------------------------------------------------------------------------------------


def test_a_failing_feed_is_not_working_and_says_since_when(mine: None) -> None:
    first = NOW - timedelta(hours=2)
    status = FeedStatus(
        last_synced_at=NOW - timedelta(hours=3),
        last_attempt_at=NOW - timedelta(minutes=5),
        last_error="The web address didn't answer.",
        failures=4,
        first_failed_at=first,
    )
    _feed(_store(), name="Usage", status=status)
    check = _check(health.checks(NOW, TODAY), "Usage")
    assert check.state is health.State.FAIL
    assert check.detail == (
        f"The web address didn't answer. Failing since {health.stamp(first, TODAY)}."
    )
    assert check.fix_url == "/settings/connections"
    assert check.last_success == NOW - timedelta(hours=3)


def test_a_timed_feed_with_no_success_for_two_intervals_is_overdue(mine: None) -> None:
    store = _store()
    config = _feed(store, name="Usage", schedule=Schedule.HOURLY)
    store.update_connection(
        config.model_copy(
            update={
                "feed": config.feed.model_copy(update={"status": _synced(NOW - timedelta(hours=3))})
            }
        )
    )
    check = _check(health.checks(NOW, TODAY), "Usage")
    assert check.state is health.State.WARN
    assert check.detail.startswith("No sync since ")
    [source] = health.freshness(NOW, TODAY).sources
    assert source.overdue is True


def test_a_timed_feed_within_two_intervals_is_working(mine: None) -> None:
    _feed(
        _store(),
        name="Usage",
        schedule=Schedule.HOURLY,
        status=_synced(NOW - timedelta(minutes=90)),
    )
    check = _check(health.checks(NOW, TODAY), "Usage")
    assert check.state is health.State.OK
    assert check.detail.startswith("Synced ")
    assert "Schedule: Every hour." in check.detail


def test_manual_and_on_change_feeds_are_never_overdue(mine: None) -> None:
    store = _store()
    old = _synced(NOW - timedelta(days=10))
    manual = _feed(store, name="Hand made", schedule=Schedule.MANUAL, status=old)
    on_change = _feed(store, name="Shared file", schedule=Schedule.ON_CHANGE, status=old)
    assert health.feed_overdue(manual, NOW) is False
    assert health.feed_overdue(on_change, NOW) is False
    found = health.checks(NOW, TODAY)
    assert _check(found, "Hand made").state is health.State.OK
    assert _check(found, "Shared file").state is health.State.OK
    assert health.freshness(NOW, TODAY).sources == []


def test_a_feed_that_never_synced_is_overdue_only_after_two_intervals_from_when_it_was_made(
    mine: None,
) -> None:
    config = _feed(_store(), name="Usage", schedule=Schedule.DAILY)
    created = config.created_at
    assert health.feed_overdue(config, created + timedelta(days=1)) is False
    assert health.feed_overdue(config, created + timedelta(days=2, minutes=1)) is True


def test_a_disabled_feed_is_left_out_of_the_checks_and_the_freshness(mine: None) -> None:
    _feed(_store(), name="Usage", status=_synced(NOW - timedelta(days=5)), enabled=False)
    assert "Usage" not in _names(health.checks(NOW, TODAY))
    assert health.freshness(NOW, TODAY).sources == []


def test_the_freshness_is_the_oldest_timed_feed_sync(mine: None) -> None:
    store = _store()
    _feed(
        store, name="Usage", schedule=Schedule.HOURLY, status=_synced(NOW - timedelta(minutes=30))
    )
    _feed(store, name="Tickets", schedule=Schedule.DAILY, status=_synced(NOW - timedelta(hours=3)))
    fresh = health.freshness(NOW, TODAY)
    assert fresh.as_of == NOW - timedelta(hours=3)
    assert [source.label for source in fresh.sources] == ["Usage", "Tickets"]
    assert not any(source.overdue for source in fresh.sources)


def test_a_timed_feed_still_waiting_for_its_first_sync_has_no_as_of(mine: None) -> None:
    store = _store()
    _feed(store, name="New", schedule=Schedule.DAILY)
    fresh = health.freshness(NOW, TODAY)
    assert fresh.as_of is None
    [source] = fresh.sources
    assert source.last_success is None and source.overdue is False


def test_a_feed_failing_for_more_than_a_day_is_on_every_page(mine: None) -> None:
    store = _store()
    long_gone = FeedStatus(
        last_attempt_at=NOW - timedelta(days=2),
        last_error="The file is no longer there.",
        failures=9,
        first_failed_at=NOW - timedelta(days=2),
    )
    just_now = FeedStatus(
        last_attempt_at=NOW - timedelta(hours=1),
        last_error="The web address didn't answer.",
        failures=1,
        first_failed_at=NOW - timedelta(hours=1),
    )
    _feed(store, name="Old feed", status=long_gone)
    _feed(store, name="New feed", status=just_now)
    problems = health.banner(NOW, TODAY)
    assert [problem.name for problem in problems] == ["Old feed"]
    assert "The file is no longer there." in problems[0].message
    assert problems[0].fix_url == "/settings/connections"


# --- The journal ---------------------------------------------------------------------------------


def test_an_unreadable_journal_is_a_failing_check_and_a_banner(mine: None) -> None:
    store = _store()
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_text("{ not a journal", encoding="utf-8")
    check = _check(health.checks(NOW, TODAY), "Your notes")
    assert check.state is health.State.FAIL
    assert check.fix_url == "/settings/backup"
    [problem] = health.banner(NOW, TODAY)
    assert problem.name == "Your notes"
    assert problem.fix_url == "/settings/backup"


def test_a_set_aside_journal_copy_is_a_warning_that_names_the_file(mine: None) -> None:
    store = _store()
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_text('{"by_account": {}}', encoding="utf-8")
    copy = "journal.unreadable-20261111T090000.json"
    (store.root / copy).write_text("{ damaged", encoding="utf-8")
    check = _check(health.checks(NOW, TODAY), "Your notes")
    assert check.state is health.State.WARN
    assert copy in check.detail
    assert health.banner(NOW, TODAY) == []


def test_a_journal_that_was_never_written_is_fine(mine: None) -> None:
    check = _check(health.checks(NOW, TODAY), "Your notes")
    assert check.state is health.State.OK
    assert check.detail == "No notes yet."


# --- The scheduler -------------------------------------------------------------------------------


def test_no_scheduler_check_when_this_process_does_not_run_the_scheduler(mine: None) -> None:
    assert "Background checks" not in _names(health.checks(NOW, TODAY))


def test_the_scheduler_warns_after_three_quiet_minutes_and_fails_after_fifteen(
    mine: None,
) -> None:
    runlog.scheduler_started(NOW)
    runlog.beat(NOW)
    running = _check(health.checks(NOW + timedelta(minutes=1), TODAY), "Background checks")
    assert running.state is health.State.OK
    assert running.last_success == NOW

    quiet = health.checks(NOW + timedelta(minutes=5), TODAY)
    assert _check(quiet, "Background checks").state is health.State.WARN
    assert health.banner(NOW + timedelta(minutes=5), TODAY) == []

    stopped = NOW + timedelta(minutes=20)
    assert _check(health.checks(stopped, TODAY), "Background checks").state is health.State.FAIL
    [problem] = health.banner(stopped, TODAY)
    assert problem.name == "Background checks"
    assert "Restart Wardmind" in problem.message


def test_a_scheduler_that_has_not_ticked_yet_is_starting(mine: None) -> None:
    runlog.scheduler_started(NOW)
    check = _check(health.checks(NOW + timedelta(seconds=30), TODAY), "Background checks")
    assert check.state is health.State.OK
    assert check.detail.startswith("Starting")


# --- Score history and the daily runs ------------------------------------------------------------


def test_the_score_history_check_says_what_is_missing(mine: None) -> None:
    store = _store()
    store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    found = health.checks(NOW, TODAY)
    assert _check(found, "Score history").state is health.State.WARN

    with pytest.raises(RuntimeError):
        with runlog.run("snapshot", day=TODAY):
            raise RuntimeError("disk full")
    failing = _check(health.checks(NOW, TODAY), "Score history")
    assert failing.state is health.State.FAIL
    assert failing.detail == "Today's scores couldn't be recorded. Unexpected error: RuntimeError."

    service.history_store().save(history.snapshot_of([], [], TODAY, NOW))
    recorded = _check(health.checks(NOW, TODAY), "Score history")
    assert recorded.state is health.State.OK
    assert recorded.detail.startswith("Recorded ")


def test_the_score_history_check_is_left_out_with_no_connection(mine: None) -> None:
    assert "Score history" not in _names(health.checks(NOW, TODAY))


def test_the_daily_checks_follow_their_settings(mine: None) -> None:
    store = _store()
    store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    save_settings(Settings(source=DataSource.MINE, rule_renewal=False, suggestions=False))
    assert "Automatic follow-ups" not in _names(health.checks(NOW, TODAY))
    assert "Suggestions" not in _names(health.checks(NOW, TODAY))

    save_settings(Settings(source=DataSource.MINE, rule_renewal=True, suggestions=True))
    waiting = health.checks(NOW, TODAY)
    assert _check(waiting, "Automatic follow-ups").detail == (
        "Runs once today's scores are recorded."
    )

    service.history_store().save(history.snapshot_of([], [], TODAY, NOW))
    assert _check(health.checks(NOW, TODAY), "Automatic follow-ups").state is health.State.WARN

    with runlog.run("rules", day=TODAY):
        pass
    done = _check(health.checks(NOW, TODAY), "Automatic follow-ups")
    assert done.state is health.State.OK
    assert done.detail.startswith("Ran ")


def test_a_failed_daily_run_is_worth_a_look_with_its_reason(mine: None) -> None:
    store = _store()
    store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    save_settings(Settings(source=DataSource.MINE, rule_renewal=True))
    service.history_store().save(history.snapshot_of([], [], TODAY, NOW))
    with pytest.raises(RuntimeError):
        with runlog.run("rules", day=TODAY):
            raise RuntimeError("the rules broke")
    check = _check(health.checks(NOW, TODAY), "Automatic follow-ups")
    assert check.state is health.State.WARN
    assert check.detail == "The last run failed. Unexpected error: RuntimeError."
    assert check.fix_url == "/settings/follow-ups"


# --- Mailbox and AI ------------------------------------------------------------------------------


def test_the_mailbox_check_reads_the_saved_settings(mine: None) -> None:
    store = _store()
    settings = mailbox.MailboxSettings(
        provider=mailbox.Provider.GMAIL,
        host="imap.gmail.com",
        username="dana@example.com",
        own_addresses=["dana@example.com"],
        enabled=True,
        connected_at=NOW - timedelta(days=3),
        last_error="The mailbox didn't accept that address and password.",
    )
    mailbox.save_settings(store, settings)
    failing = _check(health.checks(NOW, TODAY), "Mailbox")
    assert failing.state is health.State.FAIL
    assert failing.detail == (
        "The last check failed. The mailbox didn't accept that address and password."
    )
    mailbox.save_settings(store, settings.model_copy(update={"enabled": False, "last_error": None}))
    paused = _check(health.checks(NOW, TODAY), "Mailbox")
    assert paused.state is health.State.OK
    assert paused.detail.startswith("Paused.")


def test_no_mailbox_means_no_mailbox_check(mine: None) -> None:
    assert "Mailbox" not in _names(health.checks(NOW, TODAY))


def test_ai_summaries_turned_on_without_a_key_are_worth_a_look(mine: None) -> None:
    save_settings(Settings(source=DataSource.MINE, web_research=True, ai_summaries=True))
    check = _check(health.checks(NOW, TODAY), "AI summaries")
    assert check.state is health.State.WARN
    assert check.detail == "Add your OpenRouter API key in Settings > Data & privacy."
    assert check.fix_url == "/settings/privacy"


# --- Locked books and the wording helpers --------------------------------------------------------


def test_a_locked_book_has_only_the_lock_check_and_no_banner(mine: None) -> None:
    vault.enable(password=PASSWORD)
    vault.lock()
    found = health.checks(NOW, TODAY)
    assert _names(found) == ["Your data"]
    assert found[0].state is health.State.WARN
    assert found[0].fix_url == "/unlock"
    assert health.banner(NOW, TODAY) == []
    assert health.freshness(NOW, TODAY).sources == []


def test_the_words_for_a_time_say_today_yesterday_or_the_day(mine: None) -> None:
    assert health.stamp(NOW, TODAY) == f"{NOW.replace(tzinfo=UTC).astimezone():%H:%M} today"
    assert health.day_words(NOW - timedelta(days=1), TODAY) == "yesterday"
    earlier = NOW - timedelta(days=4)
    local = earlier.replace(tzinfo=UTC).astimezone()
    assert health.day_words(earlier, TODAY) == f"{local:%a} {local.day} {local:%b}"
    assert health.stamp(earlier, TODAY).endswith(f"{local:%H:%M}")
