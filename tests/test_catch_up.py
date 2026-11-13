"""The background catch-up (core/service.py): the first tick takes the day's score snapshot, a book
switch takes one for the new book, a failed follow-up run is retried on a later tick, the
suggestions refresh runs once a day after the rules, a run says how many follow-ups it held back, a
feed that keeps failing counts its failures and is logged once a day, and a failure's backoff.

Runs through the real service against the temp folders from conftest. Times are real (the tick reads
the clock itself), so the day is date.today()."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from wardmind.core import audit, books, editing, importing, proposals, rules, runlog, service
from wardmind.core.feeds import sync as feed_sync
from wardmind.core.feeds.http import FeedError
from wardmind.core.feeds.models import FeedRole, FeedSettings, FeedStatus, Schedule
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import ConnectionConfig, ConnectionKind, LocalStore

NOW = datetime.now(UTC).replace(tzinfo=None)
TODAY = date.today()
CSV = (
    "Company,ARR,Renewal date,Owner,Usage trend\n"
    '"Acme","$120,000",2027-03-01,Dana,-38\n'
    '"Beta","$50,000",2027-06-01,Sam,5\n'
)
MAPPING = {
    "name": "Company",
    "arr": "ARR",
    "renewal_date": "Renewal date",
    "owner": "Owner",
    "signal:usage_trend_30d": "Usage trend",
}


@pytest.fixture(autouse=True)
def fresh_scheduler_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts as a freshly started scheduler: no housekeeping yet, no snapshot taken."""
    monkeypatch.setattr(service, "_last_housekeeping", None)
    monkeypatch.setattr(service, "_snapshot_days", {})
    runlog.scheduler_stopped()


@pytest.fixture
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


def _account(name: str, renewal: date | None = None) -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$100,000",
            "renewal_date": (renewal or TODAY + timedelta(days=300)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _feed(store: LocalStore, *, schedule: Schedule = Schedule.DAILY) -> ConnectionConfig:
    config = store.add_connection(ConnectionKind.FEED, "Test feed")
    feed = FeedSettings(
        provider="file",
        role=FeedRole.ACCOUNTS,
        settings={},
        schedule=schedule,
    )
    updated = config.model_copy(
        update={"feed": feed, "mapping": dict(MAPPING), "date_order": "dmy"}
    )
    store.update_connection(updated)
    return updated


def _failures() -> list[audit.Entry]:
    return [entry for entry in audit.entries() if entry.event is audit.Event.BACKGROUND_FAILED]


# --- The first tick and the day's snapshot -------------------------------------------------------


def test_the_first_tick_takes_todays_snapshot_when_only_yesterdays_exists(mine: None) -> None:
    _account("Acme Robotics")
    yesterday = TODAY - timedelta(days=1)
    service.remember_book(now=NOW - timedelta(days=1), today=yesterday)
    assert service.history_store().load(TODAY) is None

    service.sync_due_feeds()

    assert service.history_store().load(TODAY) is not None
    snapshot = runlog.last_success("snapshot")
    assert snapshot is not None and snapshot.day == TODAY


def test_a_tick_within_the_hour_does_not_ask_for_the_snapshot_again(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(service, "remember_book", lambda: calls.append("snapshot") or True)
    service.sync_due_feeds()
    service.sync_due_feeds()
    assert calls == ["snapshot"]


def test_switching_books_takes_a_snapshot_for_the_new_book_at_once(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account("Acme Robotics")
    monkeypatch.setattr(service, "_last_housekeeping", NOW)  # the hourly work is not due
    service.sync_due_feeds()
    assert service.history_store().load(TODAY) is not None

    other = books.add("Book 2")
    books.switch(other.id)
    service.reset_connectors()
    assert service.history_store().load(TODAY) is None  # the new book has no snapshot yet

    service.sync_due_feeds()

    assert service.history_store().load(TODAY) is not None
    assert runlog.last_success("snapshot") is not None


def test_a_tick_records_its_heartbeat_and_housekeeping(mine: None) -> None:
    service.sync_due_feeds()
    assert runlog.heartbeat().last_tick is not None
    housekeeping = runlog.last_success("housekeeping")
    assert housekeeping is not None and housekeeping.ok is True


def test_a_failed_snapshot_is_recorded_and_the_tick_carries_on(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise RuntimeError("the disk said C:/Dana/Acme.csv")

    monkeypatch.setattr(service, "get_connector", broken)
    assert service.sync_due_feeds() == 0  # no exception reaches the scheduler
    last = runlog.last("snapshot")
    assert last is not None and last.ok is False
    assert last.detail == "Unexpected error: RuntimeError."
    assert "Dana" not in last.detail


# --- Follow-ups and suggestions after the snapshot -----------------------------------------------


def test_a_failed_follow_up_run_is_retried_on_a_later_tick(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_renewal=True))
    renewal = TODAY + timedelta(days=10)
    account_id = _account("Acme Robotics", renewal=renewal)
    real_apply = rules.apply_rules
    attempts: list[int] = []

    def flaky(*args: object, **kwargs: object) -> int:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the rules broke")
        return real_apply(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rules, "apply_rules", flaky)
    service.remember_book(now=NOW, today=TODAY)
    assert service.account_journal(account_id).follow_ups == []
    failed = runlog.last("rules")
    assert failed is not None and failed.ok is False

    service.sync_due_feeds()  # within the quarter hour after a failure: not tried again yet
    assert service.account_journal(account_id).follow_ups == []
    assert len(attempts) == 1

    monkeypatch.setattr(service, "_RETRY_AFTER", timedelta(0))  # the quarter hour has passed
    service.sync_due_feeds()

    [added] = service.account_journal(account_id).follow_ups
    assert added.rule == f"renewal:{renewal.isoformat()}"
    done = runlog.last_success("rules")
    assert done is not None and done.day == TODAY
    assert len(attempts) == 2


def test_the_suggestions_refresh_runs_once_a_day_after_the_snapshot(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[date | None, datetime | None]] = []

    def fake_refresh(
        today: date | None = None, now: datetime | None = None, *, force: bool = False
    ) -> proposals.RefreshResult:
        calls.append((today, now))
        return proposals.RefreshResult(added=2, waiting=1)

    monkeypatch.setattr(service, "refresh_suggestions", fake_refresh)
    service.remember_book(now=NOW, today=TODAY)
    service.sync_due_feeds()
    service.sync_due_feeds()

    assert [day for day, _ in calls] == [TODAY]
    last = runlog.last_success("suggestions")
    assert last is not None and last.counts == {"added": 2, "waiting": 1}


def test_a_run_says_how_many_follow_ups_it_held_back(mine: None) -> None:
    save_settings(Settings(source=DataSource.MINE, rule_renewal=True))
    soon = TODAY + timedelta(days=5)
    for index in range(rules.MAX_PLANNED + 2):
        _account(f"Account {index:02d}", renewal=soon)
    service.remember_book(now=NOW, today=TODAY)
    last = runlog.last_success("rules")
    assert last is not None
    assert last.counts == {"added": rules.MAX_PLANNED, "held_back": 2}


# --- Feeds that keep failing ---------------------------------------------------------------------


def test_a_feed_failing_twice_counts_its_failures_and_is_logged_once_a_day(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = service.get_store()
    config = _feed(store)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the export is at C:/Dana/export.csv")

    monkeypatch.setattr(service, "_sync_one", broken)
    service.sync_due_feeds()
    first = store.get_connection(config.id)
    assert first is not None and first.feed is not None
    status = first.feed.status
    assert status.last_error == feed_sync.SYNC_FAILED
    assert status.failures == 1
    assert status.first_failed_at is not None
    began = status.first_failed_at

    # Due again: the retry waits fifteen minutes after the last attempt.
    assert status.last_attempt_at is not None
    store.update_connection(
        first.model_copy(
            update={
                "feed": first.feed.model_copy(
                    update={
                        "status": status.model_copy(
                            update={
                                "last_attempt_at": status.last_attempt_at - timedelta(minutes=16)
                            }
                        )
                    }
                )
            }
        )
    )
    service.sync_due_feeds()

    second = store.get_connection(config.id)
    assert second is not None and second.feed is not None
    assert second.feed.status.failures == 2
    assert second.feed.status.first_failed_at == began
    assert [entry.detail for entry in _failures()] == ["feed sync"]

    records = runlog.recent(f"feed:{config.id}")
    assert [record.ok for record in records] == [False, False]
    assert {record.detail for record in records} == {"Unexpected error: RuntimeError."}
    assert "Dana" not in str(records)


def test_a_failed_sync_counts_its_failures_and_a_success_clears_them(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = service.get_store()
    config = _feed(store)

    reachable = {"yes": False}
    real_fetch = feed_sync.fetch_table

    def maybe_unreachable(*args: object, **kwargs: object) -> object:
        if not reachable["yes"]:
            raise FeedError("The web address didn't answer.")
        return real_fetch(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(feed_sync, "fetch_table", maybe_unreachable)
    feed_sync.sync_feed(config, store, book_accounts=[], now=NOW, today=TODAY)
    once = store.get_connection(config.id)
    assert once is not None and once.feed is not None
    assert once.feed.status.failures == 1
    assert once.feed.status.first_failed_at == NOW
    assert once.feed.status.last_error == "The web address didn't answer."

    later = NOW + timedelta(minutes=30)
    feed_sync.sync_feed(once, store, book_accounts=[], now=later, today=TODAY)
    twice = store.get_connection(config.id)
    assert twice is not None and twice.feed is not None
    assert twice.feed.status.failures == 2
    assert twice.feed.status.first_failed_at == NOW  # the run of failures began at the first

    table = importing.read_csv(CSV.encode("utf-8"), "feed.csv")
    feed_sync.sync_feed(twice, store, book_accounts=[], now=later, today=TODAY, table=table)
    done = store.get_connection(config.id)
    assert done is not None and done.feed is not None
    assert done.feed.status.last_error is None
    assert done.feed.status.failures == 0
    assert done.feed.status.first_failed_at is None


def test_a_failing_feed_is_retried_every_fifteen_minutes_then_hourly_after_three_failures() -> None:
    def config_with(failures: int, minutes_ago: int) -> ConnectionConfig:
        status = FeedStatus(
            last_synced_at=NOW - timedelta(days=2),
            last_attempt_at=NOW - timedelta(minutes=minutes_ago),
            last_error="The web address didn't answer.",
            failures=failures,
            first_failed_at=NOW - timedelta(days=1),
        )
        feed = FeedSettings(
            provider="file", role=FeedRole.ACCOUNTS, schedule=Schedule.HOURLY, status=status
        )
        return ConnectionConfig(
            id="feed-1", kind=ConnectionKind.FEED, name="Usage", created_at=NOW, feed=feed
        )

    assert feed_sync.is_due(config_with(failures=2, minutes_ago=20), NOW, None) is True
    assert feed_sync.is_due(config_with(failures=3, minutes_ago=20), NOW, None) is False
    assert feed_sync.is_due(config_with(failures=3, minutes_ago=61), NOW, None) is True


def test_a_feed_status_saved_before_the_failure_counts_still_loads() -> None:
    old = {
        "last_synced_at": "2026-11-10T09:00:00",
        "last_attempt_at": "2026-11-10T09:00:00",
        "last_error": None,
        "rows": 4,
        "accounts": 4,
        "skipped_rows": 0,
        "unmatched": [],
        "source_mtime": None,
    }
    status = FeedStatus.model_validate(old)
    assert status.failures == 0
    assert status.first_failed_at is None
    assert status.rows == 4


def test_a_missing_snapshot_file_is_taken_again_after_the_retry_wait(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account("Acme Robotics")
    monkeypatch.setattr(service, "_last_housekeeping", NOW)  # the hourly work is not due
    monkeypatch.setattr(service, "_snapshot_tried", {})
    service.sync_due_feeds()
    assert service.history_store().load(TODAY) is not None
    service.history_store().clear()  # e.g. Delete all local data removed it

    service.sync_due_feeds()  # asked moments ago: not asked again yet
    assert service.history_store().load(TODAY) is None

    book = books.active_id()
    service._snapshot_tried[book] = NOW - timedelta(minutes=20)
    service.sync_due_feeds()
    assert service.history_store().load(TODAY) is not None


def test_run_checks_now_asks_for_the_snapshot_even_after_one_today(
    mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(service, "remember_book", lambda: calls.append("snapshot") or True)
    service.sync_due_feeds()
    service.run_checks_now()
    assert calls == ["snapshot", "snapshot"]
