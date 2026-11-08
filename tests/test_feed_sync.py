"""Running a feed: an ACCOUNTS sync (the snapshot, its history and readings), a SIGNALS sync (the
readings attached to accounts already in the book), failures that keep the data, what is due, and
the background scheduler.

The local store is in a temp folder (conftest). The one web address a test fetches from is a
made-up one on an httpx.MockTransport.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

import httpx
import pytest

from cs_brief.core import client_updates, importing
from cs_brief.core.connectors.combined import CombinedConnector
from cs_brief.core.connectors.local import SpreadsheetConnector
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds import signals, sync
from cs_brief.core.feeds.models import FeedRole, FeedSettings, FeedStatus, MatchBy, Schedule
from cs_brief.core.models import Account, Milestones, SignalKind
from cs_brief.core.secrets import save_secret
from cs_brief.core.store import ConnectionConfig, ConnectionKind, ImportSnapshot, LocalStore

NOW = datetime(2026, 11, 8, 9, 0)
TODAY = date(2026, 11, 8)
SECRET = "test-secret-0123456789abcdef"

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


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def table_from(text: str) -> importing.Table:
    return importing.read_csv(text.encode("utf-8"), "feed.csv")


def make_feed(
    store: LocalStore,
    *,
    role: FeedRole = FeedRole.ACCOUNTS,
    provider: str = "file",
    settings: dict[str, str] | None = None,
    schedule: Schedule = Schedule.DAILY,
    mapping: dict[str, str] | None = None,
    match_by: MatchBy = MatchBy.AUTO,
) -> ConnectionConfig:
    config = store.add_connection(ConnectionKind.FEED, "Test feed")
    feed = FeedSettings(
        provider=provider,
        role=role,
        settings=settings or {},
        schedule=schedule,
        match_by=match_by,
    )
    updated = config.model_copy(
        update={
            "feed": feed,
            "mapping": MAPPING if mapping is None else mapping,
            "date_order": "dmy",
        }
    )
    store.update_connection(updated)
    return updated


def run(
    store: LocalStore,
    config: ConnectionConfig,
    table: importing.Table | None = None,
    *,
    now: datetime = NOW,
    book: list[Account] | None = None,
) -> sync.SyncOutcome:
    current = store.get_connection(config.id)
    assert current is not None
    return sync.sync_feed(
        current,
        store,
        book_accounts=book or [],
        now=now,
        today=TODAY,
        table=table,
    )


def acct(acc_id: str, name: str, domain: str | None = None) -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=50000.0,
        tier="Growth",
        owner="Dana",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
        domain=domain,
    )


# ACCOUNTS -------------------------------------------------------------------------------------


def test_an_accounts_sync_creates_fd_accounts_and_their_history(store: LocalStore) -> None:
    config = make_feed(store)
    outcome = run(store, config, table_from(CSV))

    assert outcome.status.last_error is None
    assert outcome.status.rows == 2
    assert outcome.status.accounts == 2
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    assert [account.id for account in snapshot.accounts] == ["fd1-acme", "fd1-beta"]
    assert snapshot.history[-1].file_name == "File on this computer"
    assert snapshot.history[-1].accounts == 2
    assert {signal.source for signal in snapshot.signals} == {"feed:feed-1"}
    assert {signal.kind for signal in snapshot.signals} == {SignalKind.USAGE_TREND_30D}
    saved = store.get_connection("feed-1")
    assert saved is not None and saved.feed is not None
    assert saved.feed.status.last_synced_at == NOW
    assert saved.feed.status.last_attempt_at == NOW
    assert len(SpreadsheetConnector(store, "feed-1").list_accounts()) == 2


def test_resyncing_keeps_the_history_and_adds_only_changed_readings(store: LocalStore) -> None:
    config = make_feed(store)
    run(store, config, table_from(CSV))
    run(store, config, table_from(CSV), now=NOW + timedelta(days=1))
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    assert len(snapshot.history) == 2
    assert len(snapshot.signals) == 2  # unchanged values add nothing

    changed = CSV.replace("-38", "-40")
    run(store, config, table_from(changed), now=NOW + timedelta(days=2))
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    acme = [signal for signal in snapshot.signals if signal.account_id == "fd1-acme"]
    assert [signal.value for signal in acme] == [-38.0, -40.0]


def test_a_sync_with_no_usable_rows_keeps_the_old_snapshot_and_records_why(
    store: LocalStore,
) -> None:
    config = make_feed(store)
    run(store, config, table_from(CSV))
    unusable = "Company,ARR,Renewal date,Owner,Usage trend\nAcme,,,Dana,-38\n"

    outcome = run(store, config, table_from(unusable), now=NOW + timedelta(hours=1))

    assert outcome.status.last_error is not None
    assert "accounts from the last sync are kept" in outcome.status.last_error
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    assert len(snapshot.accounts) == 2
    saved = store.get_connection("feed-1")
    assert saved is not None and saved.feed is not None
    assert saved.feed.status.last_synced_at == NOW
    assert saved.feed.status.last_attempt_at == NOW + timedelta(hours=1)
    assert saved.feed.status.accounts == 2


def test_a_mapping_that_no_longer_fits_the_file_is_recorded_as_the_error(store: LocalStore) -> None:
    config = make_feed(store)
    run(store, config, table_from(CSV))
    renamed = CSV.replace("Renewal date", "Renews")

    outcome = run(store, config, table_from(renamed), now=NOW + timedelta(hours=1))

    assert outcome.status.last_error is not None
    assert "isn't a column in this file" in outcome.status.last_error
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None and len(snapshot.accounts) == 2


def test_a_fetch_that_fails_records_the_error_and_creates_nothing(
    store: LocalStore, tmp_path
) -> None:
    config = make_feed(store, provider="file", settings={"path": str(tmp_path / "missing.csv")})
    outcome = run(store, config)
    assert outcome.status.last_error is not None
    assert "no file at that path" in outcome.status.last_error
    assert store.snapshot("feed-1") is None


def test_a_link_sync_uses_the_saved_secret_and_sends_it_in_a_header(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=CSV.encode("utf-8"), headers={"content-type": "text/csv"}
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    config = make_feed(
        store,
        provider="link",
        settings={"url": "https://exports.example.com/accounts.csv", "auth": "bearer"},
    )
    save_secret("feed-1", SECRET)

    outcome = run(store, config)

    assert outcome.status.last_error is None
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in str(seen[0].url)
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None and len(snapshot.accounts) == 2


def test_removed_accounts_are_counted_when_they_leave_the_file(store: LocalStore) -> None:
    config = make_feed(store)
    run(store, config, table_from(CSV))
    only_acme = (
        'Company,ARR,Renewal date,Owner,Usage trend\n"Acme","$120,000",2027-03-01,Dana,-38\n'
    )
    outcome = run(store, config, table_from(only_acme), now=NOW + timedelta(days=1))
    assert outcome.removed_accounts == 1


# SIGNALS --------------------------------------------------------------------------------------


def _sheet_with(store: LocalStore, accounts: list[Account]) -> ConnectionConfig:
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Renewals")
    store.save_snapshot(ImportSnapshot(connection_id=sheet.id, accounts=accounts))
    return sheet


def test_a_signals_sync_attaches_readings_to_accounts_already_in_the_book(
    store: LocalStore,
) -> None:
    sheet = _sheet_with(store, [acct("sp1-acme", "Acme", domain="acme.com")])
    config = make_feed(
        store,
        role=FeedRole.SIGNALS,
        mapping={"name": "Company", "signal:usage_trend_30d": "Usage trend"},
        match_by=MatchBy.NAME,
    )
    book = CombinedConnector(
        [(sheet, SpreadsheetConnector(store, sheet.id))], store
    ).list_accounts()
    feed_table = table_from("Company,Usage trend\nAcme,-12\nUnknown Co,4\n")

    outcome = run(store, config, feed_table, book=book)

    assert outcome.status.last_error is None
    assert outcome.status.accounts == 1
    assert outcome.status.skipped_rows == 1
    assert [item.key for item in outcome.status.unmatched] == ["Unknown Co"]
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    assert snapshot.accounts == []
    assert snapshot.history[-1].accounts == 1

    fresh = store.get_connection("feed-1")
    assert fresh is not None
    combined = CombinedConnector(
        [
            (sheet, SpreadsheetConnector(store, sheet.id)),
            (fresh, SpreadsheetConnector(store, fresh.id)),
        ],
        store,
    )
    readings = combined.list_signals("sp1-acme")
    assert [(signal.kind, signal.value, signal.source) for signal in readings] == [
        (SignalKind.USAGE_TREND_30D, -12.0, "feed:feed-1")
    ]


def test_unmatched_rows_are_recorded_capped_with_an_exact_count(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signals, "MAX_UNMATCHED", 3)
    config = make_feed(
        store,
        role=FeedRole.SIGNALS,
        mapping={"name": "Company", "signal:usage_trend_30d": "Usage trend"},
        match_by=MatchBy.NAME,
    )
    ghosts = "".join(f"Ghost {n},1\n" for n in range(1, 6))
    rows = "Company,Usage trend\nAcme,4\n" + ghosts

    outcome = run(store, config, table_from(rows), book=[acct("hs-1", "Acme")])

    assert outcome.status.last_error is None
    assert outcome.status.accounts == 1
    assert outcome.status.skipped_rows == 5
    assert len(outcome.status.unmatched) == 3
    assert outcome.status.unmatched[0].row == 3


def test_a_signals_sync_with_no_rows_is_a_failure_that_keeps_the_old_readings(
    store: LocalStore,
) -> None:
    config = make_feed(
        store,
        role=FeedRole.SIGNALS,
        mapping={"name": "Company", "signal:usage_trend_30d": "Usage trend"},
        match_by=MatchBy.NAME,
    )
    book = [acct("hs-1", "Acme")]
    run(store, config, table_from("Company,Usage trend\nAcme,-12\n"), book=book)
    before = store.snapshot("feed-1")
    assert before is not None and len(before.signals) == 1

    outcome = run(
        store,
        config,
        table_from("Company,Usage trend\n"),
        now=NOW + timedelta(hours=1),
        book=book,
    )

    assert outcome.status.last_error == "The source sent no rows."
    after = store.snapshot("feed-1")
    assert after is not None
    assert after.signals == before.signals
    assert len(after.history) == len(before.history)
    saved = store.get_connection("feed-1")
    assert saved is not None and saved.feed is not None
    assert saved.feed.status.last_synced_at == NOW
    assert saved.feed.status.last_attempt_at == NOW + timedelta(hours=1)
    assert saved.feed.status.accounts == 1


def test_a_signals_sync_where_no_row_matches_is_a_failure_that_keeps_the_old_readings(
    store: LocalStore,
) -> None:
    config = make_feed(
        store,
        role=FeedRole.SIGNALS,
        mapping={"name": "Company", "signal:usage_trend_30d": "Usage trend"},
        match_by=MatchBy.NAME,
    )
    book = [acct("hs-1", "Acme")]
    run(store, config, table_from("Company,Usage trend\nAcme,-12\n"), book=book)
    before = store.snapshot("feed-1")
    assert before is not None

    outcome = run(
        store,
        config,
        table_from("Company,Usage trend\nUnknown A,4\nUnknown B,5\n"),
        now=NOW + timedelta(hours=1),
        book=book,
    )

    assert outcome.status.last_error == (
        "None of the 2 rows matched an account in your book. Check Match accounts by."
    )
    after = store.snapshot("feed-1")
    assert after is not None and after.signals == before.signals


# Due ------------------------------------------------------------------------------------------


def _config(
    schedule: Schedule = Schedule.DAILY,
    *,
    enabled: bool = True,
    status: FeedStatus | None = None,
    role: FeedRole = FeedRole.ACCOUNTS,
    with_feed: bool = True,
) -> ConnectionConfig:
    feed = (
        FeedSettings(
            provider="file",
            role=role,
            schedule=schedule,
            status=status or FeedStatus(),
        )
        if with_feed
        else None
    )
    return ConnectionConfig(
        id="feed-1",
        kind=ConnectionKind.FEED,
        name="Test feed",
        enabled=enabled,
        created_at=NOW,
        feed=feed,
    )


def _synced(hours_ago: float, **extra) -> FeedStatus:
    moment = NOW - timedelta(hours=hours_ago)
    return FeedStatus(last_synced_at=moment, last_attempt_at=moment, **extra)


@pytest.mark.parametrize("schedule", [Schedule.DAILY, Schedule.HOURLY, Schedule.ON_CHANGE])
def test_a_feed_that_never_synced_is_due(schedule: Schedule) -> None:
    assert sync.is_due(_config(schedule), NOW, None) is True


def test_manual_feeds_and_disabled_feeds_are_never_due() -> None:
    assert sync.is_due(_config(Schedule.MANUAL), NOW, None) is False
    assert sync.is_due(_config(Schedule.DAILY, enabled=False), NOW, None) is False
    assert sync.is_due(_config(with_feed=False), NOW, None) is False


@pytest.mark.parametrize(
    ("schedule", "hours_ago", "due"),
    [
        (Schedule.DAILY, 23, False),
        (Schedule.DAILY, 25, True),
        (Schedule.HOURLY, 0.5, False),
        (Schedule.HOURLY, 1.5, True),
        (Schedule.EVERY_15_MINUTES, 14 / 60, False),
        (Schedule.EVERY_15_MINUTES, 16 / 60, True),
        (Schedule.EVERY_6_HOURS, 5, False),
        (Schedule.EVERY_6_HOURS, 7, True),
    ],
)
def test_timed_schedules_are_due_once_their_interval_has_passed(
    schedule: Schedule, hours_ago: float, due: bool
) -> None:
    config = _config(schedule, status=_synced(hours_ago))
    assert sync.is_due(config, NOW, None) is due


def test_after_a_failure_a_retry_waits_fifteen_minutes_whatever_the_schedule() -> None:
    failed = FeedStatus(
        last_synced_at=NOW - timedelta(days=2),
        last_attempt_at=NOW - timedelta(minutes=10),
        last_error="The web address didn't answer.",
    )
    assert sync.is_due(_config(Schedule.DAILY, status=failed), NOW, None) is False
    later = failed.model_copy(update={"last_attempt_at": NOW - timedelta(minutes=16)})
    assert sync.is_due(_config(Schedule.DAILY, status=later), NOW, None) is True


def test_on_change_is_due_when_the_file_time_differs_from_the_last_sync() -> None:
    status = _synced(1, source_mtime=100.0)
    config = _config(Schedule.ON_CHANGE, status=status)
    assert sync.is_due(config, NOW, 100.0) is False
    assert sync.is_due(config, NOW, 200.0) is True


def test_on_change_without_a_file_time_falls_back_to_a_daily_check() -> None:
    config = _config(Schedule.ON_CHANGE, status=_synced(23))
    assert sync.is_due(config, NOW, None) is False
    config = _config(Schedule.ON_CHANGE, status=_synced(25))
    assert sync.is_due(config, NOW, None) is True


# Scheduler ------------------------------------------------------------------------------------


def test_start_and_stop_are_idempotent() -> None:
    scheduler = sync.FeedScheduler(lambda: None, interval=0.01)
    scheduler.start()
    first = scheduler._thread
    scheduler.start()
    assert scheduler._thread is first
    scheduler.stop()
    scheduler.stop()
    assert first is not None and not first.is_alive()


def test_stopping_a_scheduler_that_never_started_is_fine() -> None:
    sync.FeedScheduler(lambda: None).stop()


def test_a_tick_that_raises_does_not_stop_the_scheduler() -> None:
    calls: list[int] = []
    enough = threading.Event()

    def tick() -> None:
        calls.append(1)
        if len(calls) >= 3:
            enough.set()
        raise RuntimeError("boom")

    scheduler = sync.FeedScheduler(tick, interval=0.01)
    scheduler.start()
    try:
        assert enough.wait(timeout=10)
    finally:
        scheduler.stop()
    assert len(calls) >= 3


# CLIENT_UPDATES -------------------------------------------------------------------------------

FORM_URL = "https://forms.example.com/responses.csv"
FORM_HEADER = (
    "Timestamp,Client code,What changed?,Contact's name,Job title,Email,"
    "Is this our main contact?,Anything else?,Your name"
)
FORM_MAPPING = {
    "code": "Client code",
    "kind": "What changed?",
    "name": "Contact's name",
    "title": "Job title",
    "email": "Email",
    "champion": "Is this our main contact?",
    "note": "Anything else?",
    "submitted_by": "Your name",
    "submitted_at": "Timestamp",
}


@pytest.fixture
def form_body(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The CSV the form's responses link answers with."""
    body = {"text": FORM_HEADER + "\n"}

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body["text"].encode("utf-8"), headers={"content-type": "text/csv"}
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return body


def form_row(code: str, name: str = "Priya Shah") -> str:
    return f"11/8/2026 9:00:00,{code},New contact,{name},VP Ops,priya@acme.com,Yes,Hi,Dana"


def form_feed(store: LocalStore) -> ConnectionConfig:
    return make_feed(
        store,
        role=FeedRole.CLIENT_UPDATES,
        provider="link",
        settings={"url": FORM_URL, "auth": "none"},
        mapping=FORM_MAPPING,
    )


def test_a_client_updates_feed_syncs_new_rows_into_pending_updates(
    store: LocalStore, form_body: dict[str, str]
) -> None:
    code = client_updates.opt_in(store, "acc-1", agreed_with="Dana", agreed_on=TODAY, now=NOW).code
    form_body["text"] = (
        FORM_HEADER
        + "\n"
        + form_row(code)
        + "\n"
        + form_row(code, name="Sam Okafor")
        + "\n"
        + form_row("ZZZZZZZZ")
        + "\n"
    )
    config = form_feed(store)

    outcome = run(store, config, book=[acct("acc-1", "Acme")])

    assert outcome.status.last_error is None
    assert outcome.status.last_synced_at == NOW
    assert outcome.status.rows == 3
    assert outcome.status.accounts == 1
    assert outcome.status.skipped_rows == 1
    assert [u.name for u in client_updates.load(store).updates] == ["Priya Shah", "Sam Okafor"]
    assert store.snapshot("feed-1") is None  # no snapshot is written


def test_a_resync_of_the_same_responses_adds_nothing_new(
    store: LocalStore, form_body: dict[str, str]
) -> None:
    code = client_updates.opt_in(store, "acc-1", agreed_with="Dana", agreed_on=TODAY, now=NOW).code
    form_body["text"] = FORM_HEADER + "\n" + form_row(code) + "\n"
    config = form_feed(store)
    run(store, config, book=[acct("acc-1", "Acme")])

    outcome = run(store, config, book=[acct("acc-1", "Acme")])

    assert outcome.status.last_error is None
    assert outcome.status.accounts == 0
    assert len(client_updates.load(store).updates) == 1


def test_a_form_with_no_responses_yet_is_a_success(
    store: LocalStore, form_body: dict[str, str]
) -> None:
    config = form_feed(store)

    outcome = run(store, config, book=[acct("acc-1", "Acme")])

    assert outcome.status.last_error is None
    assert outcome.status.rows == 0
    assert outcome.status.last_synced_at == NOW


def test_a_mapping_missing_a_required_question_fails_with_its_message(
    store: LocalStore, form_body: dict[str, str]
) -> None:
    form_body["text"] = FORM_HEADER + "\n" + form_row("ABCDEFGH") + "\n"
    mapping = {key: header for key, header in FORM_MAPPING.items() if key != "kind"}
    config = make_feed(
        store,
        role=FeedRole.CLIENT_UPDATES,
        provider="link",
        settings={"url": FORM_URL, "auth": "none"},
        mapping=mapping,
    )

    outcome = run(store, config, book=[acct("acc-1", "Acme")])

    assert outcome.status.last_error == "Match What changed? to bring in client updates."
    saved = store.get_connection(config.id)
    assert saved is not None and saved.feed is not None
    assert saved.feed.status.last_error == outcome.status.last_error
    assert saved.feed.status.last_synced_at is None
    assert store.snapshot("feed-1") is None
