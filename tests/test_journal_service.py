"""The service layer of the journal and score history: which journal file the data showing uses,
recording the day's snapshot, "since last week", the trend, outcomes, the Today badge, and the
clean-up when accounts are removed, deleted, linked or everything is deleted.

Accounts are added by hand (Your data) or come from the demo book, so nothing reaches the
network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from wardmind.core import editing, history, journal, service
from wardmind.core.connectors.combined import CombinedConnector
from wardmind.core.models import Health, SignalKind
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import (
    DEMO_JOURNAL_FILE,
    HISTORY_DIR,
    JOURNAL_FILE,
    MANUAL_CONNECTION_ID,
    LocalStore,
)

TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


@pytest.fixture
def mine() -> None:
    _use(DataSource.MINE)


@pytest.fixture
def demo() -> None:
    _use(DataSource.DEMO)


def _add_account(store: LocalStore, name: str, renewal: date | None = None) -> str:
    renewal = renewal or TODAY + timedelta(days=300)
    account = editing.create_account(
        store,
        {"name": name, "arr": "$100,000", "renewal_date": renewal.isoformat()},
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _note(account_id: str, kind: journal.TouchKind = journal.TouchKind.CALL) -> journal.Note:
    return service.get_journal().add_note(
        account_id, kind, "Spoke to Priya.", TODAY, now=NOW, today=TODAY
    )


# --- Which journal file ---------------------------------------------------------------------


@pytest.mark.usefixtures("mine")
def test_your_data_uses_the_journal_file(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    _note(account_id)
    assert (store.root / JOURNAL_FILE).exists()
    assert not (store.root / DEMO_JOURNAL_FILE).exists()
    assert len(service.account_journal(account_id).notes) == 1


@pytest.mark.usefixtures("demo")
def test_demo_data_uses_its_own_journal_file(store: LocalStore) -> None:
    _note("acc-001")
    assert (store.root / DEMO_JOURNAL_FILE).exists()
    assert not (store.root / JOURNAL_FILE).exists()
    _use(DataSource.MINE)
    assert service.account_journal("acc-001").notes == []


# --- The Today badge --------------------------------------------------------------------------


@pytest.mark.usefixtures("mine")
def test_due_follow_up_count_counts_open_follow_ups_due_today_or_earlier(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    book = service.get_journal()
    book.add_follow_up(account_id, "Send the QBR deck", TODAY, now=NOW, today=TODAY)
    late = book.add_follow_up(account_id, "Chase invoice", TODAY, now=NOW, today=TODAY)
    book.add_follow_up(account_id, "Plan renewal", TODAY + timedelta(days=3), now=NOW, today=TODAY)
    done = book.add_follow_up(account_id, "Intro call", TODAY, now=NOW, today=TODAY)
    book.complete_follow_up(account_id, done.id, now=NOW)
    assert service.due_follow_up_count(TODAY) == 2
    assert service.due_follow_up_count(TODAY + timedelta(days=3)) == 3
    book.complete_follow_up(account_id, late.id, now=NOW)
    assert service.due_follow_up_count(TODAY) == 1


@pytest.mark.usefixtures("mine")
def test_due_follow_up_count_is_zero_with_no_journal() -> None:
    assert service.due_follow_up_count() == 0


# --- Recording the day's snapshot -------------------------------------------------------------


@pytest.mark.usefixtures("demo")
def test_demo_data_records_no_history(store: LocalStore) -> None:
    assert service.remember_book() is False
    assert not (store.root / HISTORY_DIR).exists()


@pytest.mark.usefixtures("mine")
def test_your_data_records_the_day_once_an_hour(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    assert service.remember_book(now=NOW, today=TODAY) is True
    saved = service.history_store().load(TODAY)
    assert saved is not None
    assert saved.accounts[account_id].name == "Acme Robotics"
    assert saved.accounts[account_id].rank == 1
    assert service.remember_book(now=NOW + timedelta(minutes=30), today=TODAY) is False
    assert service.remember_book(now=NOW + timedelta(hours=2), today=TODAY) is True


@pytest.mark.usefixtures("mine")
def test_a_snapshot_keeps_the_latest_signal_values(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    _add_reading(store, account_id, SignalKind.OPEN_TICKETS_SEV1, 2)
    service.remember_book(now=NOW, today=TODAY)
    saved = service.history_store().load(TODAY)
    assert saved is not None
    assert saved.accounts[account_id].signals[SignalKind.OPEN_TICKETS_SEV1] == 2


@pytest.mark.usefixtures("mine")
def test_remember_book_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "get_connector", broken)
    assert service.remember_book() is False


@pytest.mark.usefixtures("mine")
def test_the_feed_scheduler_records_the_snapshot_with_housekeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(service, "remember_book", lambda: calls.append("snapshot") or True)
    monkeypatch.setattr(service, "_last_housekeeping", None)
    service.sync_due_feeds()
    service.sync_due_feeds()  # within the hour: not again
    assert calls == ["snapshot"]


def _add_reading(store: LocalStore, account_id: str, kind: SignalKind, value: float) -> None:
    connector = service.get_connector()
    assert isinstance(connector, CombinedConnector)
    editing.add_reading(store, connector, account_id, kind, str(value), now=NOW)
    service.reset_connectors()


# --- Since last week ------------------------------------------------------------------------


@pytest.mark.usefixtures("demo")
def test_demo_week_changes_compare_with_the_demo_book_a_week_ago() -> None:
    book = service.get_book()
    changes = service.week_changes(book)
    assert changes is not None
    assert changes.since == TODAY - timedelta(days=history.WEEK_DAYS)
    assert changes.changes or changes.unchanged


@pytest.mark.usefixtures("mine")
def test_your_data_has_no_week_changes_until_a_week_old_snapshot_exists(
    store: LocalStore,
) -> None:
    account_id = _add_account(store, "Acme Robotics")
    service.remember_book(now=NOW, today=TODAY)
    assert service.week_changes(service.get_book(), TODAY) is None
    assert service.history_started() == TODAY

    old = history.BookSnapshot(
        day=TODAY - timedelta(days=8),
        taken_at=NOW - timedelta(days=8),
        accounts={},
    )
    service.history_store().save(old)
    changes = service.week_changes(service.get_book(), TODAY)
    assert changes is not None
    assert [change.account_id for change in changes.changes] == [account_id]
    assert changes.changes[0].kind is history.ChangeKind.NEW


@pytest.mark.usefixtures("demo")
def test_history_started_is_none_for_demo_data() -> None:
    assert service.history_started() is None


# --- The trend ------------------------------------------------------------------------------


@pytest.mark.usefixtures("demo")
def test_demo_trend_is_worked_out_from_the_sample_readings() -> None:
    book = service.get_book()
    points = service.account_trend(book[0], 1)
    assert len(points) > 5
    assert points[-1].day == TODAY
    assert points[-1].risk == book[0].risk
    assert [point.day for point in points] == sorted(point.day for point in points)
    assert points[0].day >= TODAY - timedelta(days=history.TREND_DAYS)


@pytest.mark.usefixtures("mine")
def test_your_data_trend_starts_with_today_only(store: LocalStore) -> None:
    _add_account(store, "Acme Robotics")
    book = service.get_book()
    points = service.account_trend(book[0], 1)
    assert [point.day for point in points] == [TODAY]


@pytest.mark.usefixtures("mine")
def test_your_data_trend_uses_recorded_days_before_today(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    service.remember_book(now=NOW - timedelta(days=3), today=TODAY - timedelta(days=3))
    service.remember_book(now=NOW, today=TODAY)  # today's file is replaced by the live point
    book = service.get_book()
    points = service.account_trend(book[0], 1)
    assert [point.day for point in points] == [TODAY - timedelta(days=3), TODAY]
    assert book[0].account.id == account_id


# --- Outcomes and the Today view ------------------------------------------------------------


@pytest.mark.usefixtures("mine")
def test_record_outcome_keeps_scores_and_latest_signals(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics", renewal=TODAY - timedelta(days=5))
    _add_reading(store, account_id, SignalKind.NPS_LATEST, 4)
    outcome = service.record_outcome(
        account_id,
        journal.OutcomeInput(
            kind=journal.OutcomeKind.RENEWED,
            renewal_date=TODAY - timedelta(days=5),
            decided_on=TODAY,
            arr_after=110_000,
        ),
    )
    assert outcome.arr_before == 100_000
    assert outcome.signals == {SignalKind.NPS_LATEST: 4}
    assert outcome.health in set(Health)
    assert service.account_journal(account_id).outcomes == [outcome]


@pytest.mark.usefixtures("mine")
def test_record_outcome_refuses_an_unknown_account() -> None:
    with pytest.raises(LookupError, match="isn't in your book"):
        service.record_outcome(
            "m-00000000",
            journal.OutcomeInput(
                kind=journal.OutcomeKind.CHURNED, renewal_date=TODAY, decided_on=TODAY
            ),
        )


@pytest.mark.usefixtures("mine")
def test_today_view_uses_the_book_and_the_journal(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    service.get_journal().add_follow_up(account_id, "Call back", TODAY, now=NOW, today=TODAY)
    view = service.today_view(TODAY)
    assert [item.follow_up.text for item in view.due] == ["Call back"]
    assert view.due[0].scored.account.id == account_id


# --- Clean-up ------------------------------------------------------------------------------


@pytest.mark.usefixtures("mine")
def test_removing_a_connection_forgets_its_journal_and_history(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    _note(account_id)
    service.remember_book(now=NOW, today=TODAY)
    service.remove_connection(MANUAL_CONNECTION_ID)
    assert service.account_journal(account_id).empty
    saved = service.history_store().load(TODAY)
    assert saved is None or account_id not in saved.accounts


@pytest.mark.usefixtures("mine")
def test_deleting_an_account_forgets_its_journal_and_history(store: LocalStore) -> None:
    keep = _add_account(store, "Keep Ltd")
    gone = _add_account(store, "Gone Ltd")
    _note(keep)
    _note(gone)
    service.remember_book(now=NOW, today=TODAY)
    editing.delete_account(store, gone)
    assert service.account_journal(gone).empty
    assert len(service.account_journal(keep).notes) == 1
    saved = service.history_store().load(TODAY)
    assert saved is not None
    assert gone not in saved.accounts
    assert keep in saved.accounts


@pytest.mark.usefixtures("mine")
def test_delete_all_local_data_removes_both_journals_and_the_history(store: LocalStore) -> None:
    account_id = _add_account(store, "Acme Robotics")
    _note(account_id)
    service.remember_book(now=NOW, today=TODAY)
    journal.Journal(store, demo=True).add_note(
        "acc-001", journal.TouchKind.NOTE, "Demo note", TODAY, now=NOW, today=TODAY
    )
    service.delete_all_local_data()
    assert not (store.root / JOURNAL_FILE).exists()
    assert not (store.root / DEMO_JOURNAL_FILE).exists()
    assert not (store.root / HISTORY_DIR).exists()
