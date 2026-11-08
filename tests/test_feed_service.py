"""The service's feed functions and their wiring: feeds in "Your data", the book without a feed's
own data, syncing (now, and the due ones), the scheduler's start and stop, the web app's
lifespan, removing a feed (through the real route), and the numbering of feed connections.

Feeds are fetched from a made-up address on an httpx.MockTransport. Keys are made-up strings.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from cs_brief.core import secrets as vault
from cs_brief.core import service
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds import sync as feed_sync
from cs_brief.core.feeds.models import FeedRole, FeedSettings, FeedStatus, Schedule
from cs_brief.core.models import Account, Milestones, Signal, SignalKind
from cs_brief.core.settings import DataSource, load_settings, save_settings
from cs_brief.core.store import (
    ConnectionKind,
    FieldEdit,
    ImportSnapshot,
    LocalStore,
    connection_for,
)
from cs_brief.web.app import create_app

# Taken before conftest swaps it for a no-op, so the real start and stop can be tested.
REAL_START = service.start_feed_scheduler
REAL_STOP = service.stop_feed_scheduler

LINK_URL = "https://exports.example.com/accounts.csv"
SECRET = "test-secret-0123456789abcdef"
MAPPING = {"name": "Company", "arr": "ARR", "renewal_date": "Renewal date", "owner": "Owner"}
CSV = (
    "Company,ARR,Renewal date,Owner\n"
    '"Acme","$120,000",2027-03-01,Dana\n'
    '"Beta","$50,000",2027-06-01,Sam\n'
)
ORIGIN = {"origin": "http://127.0.0.1"}
NOW = datetime(2026, 11, 8, 9, 0)


class Served:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.body = CSV


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    result = Served()

    def answer(request: httpx.Request) -> httpx.Response:
        result.requests.append(request)
        return httpx.Response(
            200, content=result.body.encode("utf-8"), headers={"content-type": "text/csv"}
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return result


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


@pytest.fixture
def your_data() -> Iterator[None]:
    """Settings say "Your data", so the book is built from the local store."""
    save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))
    service.reset_connectors()
    yield
    service.reset_connectors()


def add_feed(
    store: LocalStore,
    *,
    url: str = LINK_URL,
    schedule: Schedule = Schedule.DAILY,
    role: FeedRole = FeedRole.ACCOUNTS,
    enabled: bool = True,
    status: FeedStatus | None = None,
) -> str:
    config = store.add_connection(ConnectionKind.FEED, "Renewals")
    updated = config.model_copy(
        update={
            "enabled": enabled,
            "mapping": MAPPING,
            "date_order": "dmy",
            "feed": FeedSettings(
                provider="link",
                role=role,
                settings={"url": url, "auth": "none"},
                schedule=schedule,
                status=status or FeedStatus(),
            ),
        }
    )
    store.update_connection(updated)
    return config.id


def add_sheet(store: LocalStore) -> str:
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    account = Account(
        id="sp1-acme",
        name="Acme Sheet",
        arr=1.0,
        tier="Growth",
        owner="Dana",
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
    )
    store.save_snapshot(ImportSnapshot(connection_id=sheet.id, accounts=[account]))
    return sheet.id


# Feeds in the book -----------------------------------------------------------------------------


def test_a_synced_feed_is_one_of_the_members_of_your_data(
    store: LocalStore, served: Served, your_data: None
) -> None:
    add_sheet(store)
    feed_id = add_feed(store)
    service.sync_feed_now(feed_id)

    ids = {account.id for account in service.get_connector().list_accounts()}
    assert {"sp1-acme", "fd1-acme", "fd1-beta"} <= ids


def test_a_disabled_feed_is_left_out_of_the_book(
    store: LocalStore, served: Served, your_data: None
) -> None:
    feed_id = add_feed(store, enabled=False)
    service.sync_feed_now(feed_id)
    ids = {account.id for account in service.get_connector().list_accounts()}
    assert not any(account_id.startswith("fd1-") for account_id in ids)


def test_feed_book_accounts_leaves_out_the_excluded_feed(
    store: LocalStore, served: Served, your_data: None
) -> None:
    add_sheet(store)
    first = add_feed(store)
    service.sync_feed_now(first)

    without_first = {account.id for account in service.feed_book_accounts(first)}
    everything = {account.id for account in service.feed_book_accounts(None)}

    assert "sp1-acme" in without_first
    assert not any(account_id.startswith("fd1-") for account_id in without_first)
    assert "fd1-acme" in everything


def test_demo_mode_has_no_feed_accounts(store: LocalStore, served: Served) -> None:
    feed_id = add_feed(store)
    service.sync_feed_now(feed_id)
    assert load_settings().source is DataSource.DEMO
    assert service.feed_book_accounts() == []


# Syncing ---------------------------------------------------------------------------------------


def test_sync_now_shows_the_new_data_and_resets_the_connectors(
    store: LocalStore, served: Served, your_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed_id = add_feed(store)
    service.sync_feed_now(feed_id)
    assert len(service.get_connector().list_accounts()) == 2

    served.body = CSV + '"Gamma","$10,000",2027-09-01,Lee\n'
    resets: list[bool] = []
    original = service.reset_connectors

    def record() -> None:
        resets.append(True)
        original()

    monkeypatch.setattr(service, "reset_connectors", record)
    outcome = service.sync_feed_now(feed_id)

    assert outcome.status.accounts == 3
    assert resets == [True]
    assert len(service.get_connector().list_accounts()) == 3


@pytest.mark.parametrize("connection_id", ["sheet-1", "feed-9", "hubspot"])
def test_sync_now_refuses_an_id_that_is_not_a_feed(
    store: LocalStore, served: Served, connection_id: str
) -> None:
    add_sheet(store)
    with pytest.raises(LookupError):
        service.sync_feed_now(connection_id)


def test_sync_due_feeds_syncs_only_the_due_ones(store: LocalStore, served: Served) -> None:
    real_now = datetime.now(UTC).replace(tzinfo=None)
    never = add_feed(store, schedule=Schedule.DAILY)
    recent = add_feed(
        store,
        schedule=Schedule.DAILY,
        status=FeedStatus(
            last_synced_at=real_now - timedelta(hours=1),
            last_attempt_at=real_now - timedelta(hours=1),
        ),
    )
    manual = add_feed(store, schedule=Schedule.MANUAL)

    synced = service.sync_due_feeds()

    assert synced == 1
    assert len(served.requests) == 1
    config = store.get_connection(never)
    assert config is not None and config.feed is not None
    assert config.feed.status.last_synced_at is not None
    recent_config = store.get_connection(recent)
    assert recent_config is not None and recent_config.feed is not None
    assert recent_config.feed.status.last_attempt_at == real_now - timedelta(hours=1)
    manual_config = store.get_connection(manual)
    assert manual_config is not None and manual_config.feed is not None
    assert manual_config.feed.status.last_synced_at is None


def test_sync_due_feeds_carries_on_past_one_feed_that_fails(
    store: LocalStore, served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = add_feed(store)
    working = add_feed(store, url="https://exports.example.com/other.csv")
    original = feed_sync.sync_feed

    def fail_for_the_first(config, *args, **kwargs):
        if config.id == broken:
            raise RuntimeError("something unexpected")
        return original(config, *args, **kwargs)

    monkeypatch.setattr(feed_sync, "sync_feed", fail_for_the_first)

    synced = service.sync_due_feeds()

    assert synced == 1
    failed = store.get_connection(broken)
    assert failed is not None and failed.feed is not None
    assert failed.feed.status.last_error == "cs-brief couldn't finish this sync. Try Sync now."
    assert failed.feed.status.last_attempt_at is not None
    done = store.get_connection(working)
    assert done is not None and done.feed is not None
    assert done.feed.status.last_synced_at is not None


# Scheduler and the web app ----------------------------------------------------------------------


def test_the_web_app_starts_the_scheduler_on_startup_and_stops_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(service, "start_feed_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr(service, "stop_feed_scheduler", lambda: calls.append("stop"))
    with TestClient(create_app(), base_url="http://127.0.0.1"):
        assert calls == ["start"]
    assert calls == ["start", "stop"]


def test_one_scheduler_ticks_sync_due_feeds_and_is_started_and_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class FakeScheduler:
        def __init__(self, tick, **_kwargs) -> None:
            self.tick = tick
            events.append(("built", tick))

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(service, "_feed_scheduler", None)
    monkeypatch.setattr(service.feed_sync, "FeedScheduler", FakeScheduler)
    REAL_START()
    REAL_START()
    REAL_STOP()
    assert events == [("built", service.sync_due_feeds), "start", "start", "stop"]


# Removing a feed --------------------------------------------------------------------------------


def test_removing_a_feed_removes_its_snapshot_and_key_but_not_other_accounts_edits(
    store: LocalStore, served: Served, your_data: None
) -> None:
    add_sheet(store)
    feed_id = add_feed(store)
    service.sync_feed_now(feed_id)
    signal_feed = store.add_connection(ConnectionKind.FEED, "Usage")
    store.save_snapshot(
        ImportSnapshot(
            connection_id=signal_feed.id,
            signals=[
                Signal(
                    account_id="sp1-acme",
                    kind=SignalKind.USAGE_TREND_30D,
                    value=-5.0,
                    observed_at=NOW,
                    source=f"feed:{signal_feed.id}",
                )
            ],
        )
    )
    store.set_edit("sp1-acme", "owner", FieldEdit(value="Edited", base_value="Dana", edited_at=NOW))
    store.set_edit("fd1-acme", "owner", FieldEdit(value="Edited", base_value="Dana", edited_at=NOW))
    vault.save_secret(feed_id, SECRET)

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    response = client.post(
        f"/settings/connections/{feed_id}/remove", headers=ORIGIN, follow_redirects=False
    )

    assert response.status_code == 303
    assert store.get_connection(feed_id) is None
    assert store.snapshot(feed_id) is None
    assert vault.get_secret(feed_id) is None
    assert "fd1-acme" not in store.edits()
    assert "sp1-acme" in store.edits()
    sheet_snapshot = store.snapshot("sheet-1")
    assert sheet_snapshot is not None and [a.id for a in sheet_snapshot.accounts] == ["sp1-acme"]
    readings = store.snapshot(signal_feed.id)
    assert readings is not None and [s.account_id for s in readings.signals] == ["sp1-acme"]


# Numbering and ownership ------------------------------------------------------------------------


def test_feed_numbers_are_their_own_and_follow_the_highest_in_use(store: LocalStore) -> None:
    first = store.add_connection(ConnectionKind.FEED, "One")
    second = store.add_connection(ConnectionKind.FEED, "Two")
    assert (first.id, second.id) == ("feed-1", "feed-2")
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    assert sheet.id == "sheet-1"

    store.remove_connection("feed-1")
    assert store.add_connection(ConnectionKind.FEED, "Three").id == "feed-3"

    store.save_snapshot(ImportSnapshot(connection_id="feed-7"))
    assert store.add_connection(ConnectionKind.FEED, "Eight").id == "feed-8"


def test_a_feed_account_belongs_to_its_feed_and_a_spreadsheet_account_to_its_sheet() -> None:
    assert connection_for("fd3-acme") == "feed-3"
    assert connection_for("fd13-acme") == "feed-13"
    assert connection_for("sp2-acme") == "sheet-2"
    assert connection_for("acc-001") is None


def test_feed_settings_round_trip_through_the_store(store: LocalStore) -> None:
    feed_id = add_feed(store, schedule=Schedule.HOURLY)
    config = store.get_connection(feed_id)
    assert config is not None and config.feed is not None
    assert config.feed.schedule is Schedule.HOURLY
    assert config.feed.settings["url"] == LINK_URL


def test_service_remove_connection_removes_a_feeds_key_without_the_web_route(
    store: LocalStore, served: Served, your_data: None
) -> None:
    feed_id = add_feed(store)
    service.sync_feed_now(feed_id)
    vault.save_secret(feed_id, SECRET)

    service.remove_connection(feed_id)

    assert store.get_connection(feed_id) is None
    assert store.snapshot(feed_id) is None
    assert vault.get_secret(feed_id) is None


def test_service_remove_connection_does_nothing_for_an_unknown_id(store: LocalStore) -> None:
    service.remove_connection("feed-9")
    service.remove_connection("sheet-1")


def test_clear_local_cache_leaves_feed_keys_and_drafts_alone(store: LocalStore) -> None:
    vault.save_secret("feed-1", SECRET)
    drafts = store.root / "feed-drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "0123456789abcdef.json").write_text("{}", encoding="utf-8")

    service.clear_local_cache()

    assert vault.get_secret("feed-1") == SECRET
    assert (drafts / "0123456789abcdef.json").exists()
