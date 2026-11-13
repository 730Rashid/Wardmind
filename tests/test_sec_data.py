"""Data at rest, races and retention: a feed sync's writes under the store's write lock, a feed
removed or changed while its source is read, unfinished feed setups and their keys expiring,
the hourly housekeeping, Delete all local data, the sync error messages, and the email and
hidden-character rules for client updates and contacts.

The local store is the conftest's temp data folder, and the feed's web address is a made-up one
on an httpx.MockTransport. Keys are made-up strings; nothing reaches the network.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from wardmind.core import client_updates as cu
from wardmind.core import filelock, import_flow, importing, service
from wardmind.core import secrets as vault
from wardmind.core.contacts import ContactInput, validate
from wardmind.core.feeds import http as feed_http
from wardmind.core.feeds import setup, sync
from wardmind.core.feeds.models import FeedRole, FeedSettings, MatchBy, Schedule
from wardmind.core.importing import DateOrder, Table
from wardmind.core.settings import (
    Accent,
    DataSource,
    Density,
    Landing,
    Theme,
    load_settings,
    save_settings,
)
from wardmind.core.store import WRITE_LOCK, ConnectionConfig, ConnectionKind, LocalStore

NOW = datetime(2026, 11, 8, 9, 0)
TODAY = date(2026, 11, 8)
DAY = timedelta(hours=24)
SECRET = "test-secret-0123456789abcdef"
LINK_URL = "https://exports.example.com/accounts.csv"
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
SYNC_FAILED = "Wardmind couldn't finish this sync. Try Sync now."
UNICODE_FAILED = (
    "The key or password has characters that can't be sent. Check you copied only the key."
)
A = "m-aaaa0001"
B = "m-bbbb0002"
QUESTION_HEADERS = ["Client code", "What changed?", "Contact's name", "Anything else?"]
QUESTION_MAPPING = {"code": "Client code", "kind": "What changed?", "name": "Contact's name"}


@pytest.fixture
def store() -> LocalStore:
    """The store the app itself uses, so the service sees the same files the test writes."""
    return service.get_store()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """A feed setup's web address answers the CSV; the requests it got are recorded."""
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=CSV.encode("utf-8"), headers={"content-type": "text/csv"}
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return requests


def table() -> Table:
    return importing.read_csv(CSV.encode("utf-8"), "feed.csv")


def make_feed(store: LocalStore) -> ConnectionConfig:
    config = store.add_connection(ConnectionKind.FEED, "Renewals")
    feed = FeedSettings(
        provider="file", role=FeedRole.ACCOUNTS, settings={}, schedule=Schedule.DAILY
    )
    updated = config.model_copy(update={"feed": feed, "mapping": MAPPING, "date_order": "dmy"})
    store.update_connection(updated)
    return updated


def start_setup(store: LocalStore, *, now: datetime, secret: str | None = None) -> setup.FeedDraft:
    return setup.start_draft(
        store,
        "link",
        name="Renewals",
        role=FeedRole.ACCOUNTS,
        values={"url": LINK_URL, "auth": "bearer" if secret else "none"},
        secret=secret,
        schedule=Schedule.DAILY,
        match_by=MatchBy.AUTO,
        connection_id=None,
        now=now,
    )


def draft_file(store: LocalStore, draft_id: str) -> bool:
    return (store.root / setup.DRAFTS_DIR / f"{draft_id}.json").exists()


def client_code(store: LocalStore, account_id: str, *, agreed: date) -> str:
    return cu.opt_in(store, account_id, agreed_with="Pat", agreed_on=agreed, now=NOW).code


# A3-1: a sync's write can't undo a CSM's opt-out or rejection -----------------------------------


def test_an_opt_out_and_rejection_during_a_sync_hold_and_nothing_deadlocks(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_a = client_code(store, A, agreed=date(2026, 10, 1))
    code_b = client_code(store, B, agreed=date(2026, 10, 1))
    cu.ingest(
        store,
        Table(
            source_name="form",
            headers=QUESTION_HEADERS,
            rows=[[code_a, "New contact", "Jane Roe", "private note A"]],
        ),
        QUESTION_MAPPING | {"note": "Anything else?"},
        connection_id="feed-1",
        book_ids={A, B},
        date_order=DateOrder.DMY,
        now=NOW,
    )
    pending_id = cu.load(store).updates[0].id

    errors: list[BaseException] = []

    def csm_decides() -> None:
        # The CSM's rejection and opt-out, on another thread while the sync is running.
        try:
            cu.reject(store, pending_id, now=NOW)
            cu.opt_out(store, A)
        except BaseException as exc:  # reported by the test thread below
            errors.append(exc)

    csm = threading.Thread(target=csm_decides, name="csm")
    original = cu.parse_kind
    started: list[bool] = []

    def interleave(text: str) -> cu.UpdateKind:
        # Inside the sync, between its read of the file and its write of it.
        if not started:
            started.append(True)
            csm.start()
            csm.join(timeout=1.0)
        return original(text)

    monkeypatch.setattr(cu, "parse_kind", interleave)
    cu.ingest(
        store,
        Table(
            source_name="form",
            headers=QUESTION_HEADERS,
            rows=[
                [code_a, "New contact", "Jane Roe", "private note A"],
                [code_b, "New contact", "Bob", "note B"],
            ],
        ),
        QUESTION_MAPPING | {"note": "Anything else?"},
        connection_id="feed-1",
        book_ids={A, B},
        date_order=DateOrder.DMY,
        now=NOW,
    )
    csm.join(timeout=10)

    assert not csm.is_alive()
    assert errors == []
    data = cu.load(store)
    assert A not in data.consents  # the opt-out holds
    assert not [
        update
        for update in data.updates
        if update.account_id == A and update.status is cu.UpdateStatus.PENDING
    ]  # no pending update of A survives
    rejected = [update for update in data.updates if update.id == pending_id]
    assert rejected and rejected[0].status is cu.UpdateStatus.REJECTED  # the rejection holds
    assert rejected[0].name == "" and rejected[0].note == ""  # and its text is gone
    assert any(
        update.account_id == B and update.status is cu.UpdateStatus.PENDING
        for update in data.updates
    )  # B's row from this sync was kept


def test_the_source_is_read_without_holding_the_write_lock(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_feed(store)
    seen: list[bool] = []

    def probe_fetch(_config: ConnectionConfig, _store: LocalStore) -> Table:
        def probe() -> None:
            acquired = WRITE_LOCK.acquire(timeout=5)
            if acquired:
                WRITE_LOCK.release()
            seen.append(acquired)

        other = threading.Thread(target=probe)
        other.start()
        other.join(timeout=10)
        return table()

    monkeypatch.setattr(sync, "fetch_table", probe_fetch)
    sync.sync_feed(config, store, book_accounts=[], now=NOW, today=TODAY)

    assert seen == [True]


# A3-4: a feed removed or changed while its source is read is not written back -------------------


def test_a_feed_removed_while_its_source_is_read_leaves_no_snapshot_or_status(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_feed(store)

    def fetch_then_remove(current: ConnectionConfig, _store: LocalStore) -> Table:
        service.remove_connection(current.id)  # the CSM presses Remove mid-fetch
        return table()

    monkeypatch.setattr(sync, "fetch_table", fetch_then_remove)
    sync.sync_feed(config, store, book_accounts=[], now=NOW, today=TODAY)

    assert store.get_connection(config.id) is None
    assert store.snapshot(config.id) is None


def test_a_feed_changed_to_another_provider_while_its_source_is_read_is_not_written_back(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_feed(store)

    def fetch_then_change(current: ConnectionConfig, _store: LocalStore) -> Table:
        assert current.feed is not None
        changed = current.feed.model_copy(update={"provider": "link"})
        _store.update_connection(
            _store.get_connection(current.id).model_copy(update={"feed": changed})
        )
        return table()

    monkeypatch.setattr(sync, "fetch_table", fetch_then_change)
    sync.sync_feed(config, store, book_accounts=[], now=NOW, today=TODAY)

    assert store.snapshot(config.id) is None
    saved = store.get_connection(config.id)
    assert saved is not None and saved.feed is not None
    assert saved.feed.provider == "link"
    assert saved.feed.status.last_synced_at is None  # the old feed's result wasn't recorded


# A3-3: unfinished feed setups and their keys expire -----------------------------------------------


def test_an_expired_feed_setup_and_its_key_go_and_a_fresh_one_stays(
    store: LocalStore, served: list[httpx.Request]
) -> None:
    old = start_setup(store, now=NOW, secret=SECRET)
    fresh = start_setup(store, now=NOW + timedelta(hours=20), secret=SECRET)

    removed = setup.purge_old_feed_drafts(store, now=NOW + timedelta(hours=25))

    assert removed == 1
    assert not draft_file(store, old.id)
    assert vault.get_secret(f"draft-{old.id}") is None
    assert draft_file(store, fresh.id)
    assert vault.get_secret(f"draft-{fresh.id}") == SECRET


def test_starting_a_feed_setup_purges_the_expired_ones(
    store: LocalStore, served: list[httpx.Request]
) -> None:
    old = start_setup(store, now=NOW, secret=SECRET)

    start_setup(store, now=NOW + timedelta(hours=25), secret=SECRET)

    assert not draft_file(store, old.id)
    assert vault.get_secret(f"draft-{old.id}") is None


def test_a_draft_key_with_no_draft_file_is_purged_and_a_saved_feed_key_is_kept(
    store: LocalStore,
) -> None:
    vault.save_secret("draft-0123456789abcdef", SECRET)  # a setup that was never saved
    vault.save_secret("feed-1", SECRET)  # a saved feed's key

    assert setup.purge_old_feed_drafts(store, now=NOW) == 1

    assert vault.get_secret("draft-0123456789abcdef") is None
    assert vault.get_secret("feed-1") == SECRET


def test_clear_local_cache_removes_every_feed_setup_whatever_its_age(
    store: LocalStore, served: list[httpx.Request]
) -> None:
    fresh = start_setup(store, now=NOW, secret=SECRET)

    service.clear_local_cache(now=NOW)

    assert not draft_file(store, fresh.id)
    assert vault.get_secret(f"draft-{fresh.id}") is None


def test_housekeeping_runs_both_clean_ups_and_never_raises(
    store: LocalStore,
    served: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old = start_setup(store, now=NOW, secret=SECRET)

    def broken(*_args: object, **_kwargs: object) -> int:
        raise OSError(f"the disk said {SECRET}")

    monkeypatch.setattr(import_flow, "purge_old_drafts", broken)
    with caplog.at_level(logging.WARNING):
        service.housekeeping(NOW + timedelta(hours=25))

    assert not draft_file(store, old.id)  # the feed clean-up still ran
    assert "OSError" in caplog.text
    assert SECRET not in caplog.text


def test_sync_due_feeds_runs_housekeeping_at_most_once_an_hour(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[datetime | None] = []
    monkeypatch.setattr(service, "housekeeping", lambda now=None: calls.append(now))
    monkeypatch.setattr(service, "_last_housekeeping", None)

    service.sync_due_feeds()
    service.sync_due_feeds()
    assert len(calls) == 1

    monkeypatch.setattr(
        service,
        "_last_housekeeping",
        datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=61),
    )
    service.sync_due_feeds()
    assert len(calls) == 2


# A3-7: Delete all local data --------------------------------------------------------------------


def test_delete_all_local_data_removes_every_data_and_key_file_and_switches_two_settings_off(
    store: LocalStore, served: list[httpx.Request]
) -> None:
    make_feed(store)
    start_setup(store, now=NOW, secret=SECRET)
    import_flow.save_draft(store, table=table())
    vault.save_secret("feed-1", SECRET)
    vault.save_hubspot_token("pat-na1-" + "a" * 30)
    vault.save_openrouter_key("sk-or-v1-" + "b" * 30)
    save_settings(
        load_settings().model_copy(
            update={
                "theme": Theme.DARK,
                "accent": Accent.BLUE,
                "density": Density.COMPACT,
                "landing": Landing.BRIEF,
                "source": DataSource.MINE,
                "web_research": True,
                "ai_summaries": True,
                "ai_model": "anthropic/claude-opus-5",
            }
        )
    )

    service.delete_all_local_data()

    assert [path for path in store.root.rglob("*") if path.is_file()] == []
    # Only the empty cross-process lock file (core/filelock.py) remains: it holds no data.
    assert [
        path
        for path in vault.secrets_dir().rglob("*")
        if path.is_file() and path.name != filelock.LOCK_FILE
    ] == []
    settings = load_settings()
    assert settings.web_research is False
    assert settings.ai_summaries is False
    assert settings.theme is Theme.DARK
    assert settings.accent is Accent.BLUE
    assert settings.density is Density.COMPACT
    assert settings.landing is Landing.BRIEF
    assert settings.source is DataSource.MINE
    assert settings.ai_model == "anthropic/claude-opus-5"
    assert store.connections() == []


# Sync errors never echo unknown text ------------------------------------------------------------


def test_a_key_character_named_by_an_encoding_error_never_reaches_last_error(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_feed(store)
    key = "pä-SECRETKEY-0123456789"

    def fetch_fails(_config: ConnectionConfig, _store: LocalStore) -> Table:
        raise UnicodeEncodeError("ascii", key, 1, 2, "ordinal not in range(128)")

    monkeypatch.setattr(sync, "fetch_table", fetch_fails)
    outcome = service.sync_feed_now(config.id)

    assert outcome.status.last_error == UNICODE_FAILED
    saved = store.get_connection(config.id)
    assert saved is not None and saved.feed is not None
    assert saved.feed.status.last_error == UNICODE_FAILED
    assert "SECRETKEY" not in saved.feed.status.last_error
    assert "\\xe4" not in saved.feed.status.last_error


@pytest.mark.parametrize(
    "error",
    [
        RecursionError("maximum recursion depth exceeded"),
        ValueError("pä-SECRETKEY-0123456789 isn't a key"),
    ],
)
def test_any_other_failure_shows_the_generic_message_and_the_sync_returns(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    config = make_feed(store)

    def fetch_fails(_config: ConnectionConfig, _store: LocalStore) -> Table:
        raise error

    monkeypatch.setattr(sync, "fetch_table", fetch_fails)
    outcome = service.sync_feed_now(config.id)

    assert outcome.status.last_error == SYNC_FAILED
    saved = store.get_connection(config.id)
    assert saved is not None and saved.feed is not None
    assert saved.feed.status.last_error == SYNC_FAILED
    assert "SECRETKEY" not in saved.feed.status.last_error


# Hidden characters and email links ---------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "jane@client.com?bcc=attacker%40evil.com&subject=Hi",
        "jane@client.com,boss%40evil.com",
        "jane@client.com;x@evil.com",
        '"jane"@client.com',
        "jane@client",
        "jane@-client.com",
        "jane@client-.com",
        "jane@client..com",
        "jane@client.com>",
        "jane‮@client.com",
    ],
)
def test_an_email_that_could_carry_extra_fields_is_refused(email: str) -> None:
    with pytest.raises(ValueError, match="doesn't look right"):
        validate(ContactInput(name="Jane", email=email))


def test_a_plain_email_address_still_passes() -> None:
    for email in ["jane@client.com", "Jane.Roe+sales@sub.client-co.uk", "o'neil@client.com"]:
        assert validate(ContactInput(name="Jane", email=email)).email == email


@pytest.mark.parametrize(
    "values",
    [
        ContactInput(name="Jane‮Roe"),
        ContactInput(name="Jane", title="VP⁦Ops"),
        ContactInput(name="Jane​Roe"),
    ],
)
def test_hidden_format_characters_are_refused_in_name_and_title(values: ContactInput) -> None:
    with pytest.raises(ValueError, match="hidden formatting characters"):
        validate(values)


@pytest.mark.parametrize(
    "email",
    [
        "jane@client.com?bcc=attacker%40evil.com&subject=Hi",
        "jane@client.com,boss%40evil.com",
    ],
)
def test_a_bad_email_in_a_client_update_becomes_empty_and_the_row_is_kept(
    store: LocalStore, email: str
) -> None:
    code = client_code(store, A, agreed=date(2026, 10, 1))
    headers = ["Client code", "What changed?", "Contact's name", "Email"]
    result = cu.ingest(
        store,
        Table(
            source_name="form",
            headers=headers,
            rows=[[code, "New contact", "Jane Roe", email]],
        ),
        {
            "code": "Client code",
            "kind": "What changed?",
            "name": "Contact's name",
            "email": "Email",
        },
        connection_id="feed-1",
        book_ids={A},
        date_order=DateOrder.DMY,
        now=NOW,
    )

    assert result.new == 1
    update = cu.load(store).updates[0]
    assert update.email == ""
    assert update.name == "Jane Roe"


def test_bidi_overrides_are_stripped_from_a_clients_note(store: LocalStore) -> None:
    code = client_code(store, A, agreed=date(2026, 10, 1))
    cu.ingest(
        store,
        Table(
            source_name="form",
            headers=QUESTION_HEADERS,
            rows=[[code, "Something else", "Jane Roe", "Please review‮ lla ni‬ the terms"]],
        ),
        QUESTION_MAPPING | {"note": "Anything else?"},
        connection_id="feed-1",
        book_ids={A},
        date_order=DateOrder.DMY,
        now=NOW,
    )

    note = cu.load(store).updates[0].note
    assert "‮" not in note and "‬" not in note
    assert note == "Please review lla ni the terms"
