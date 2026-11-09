"""Running feeds: one sync, what is due, and the background scheduler.

CONTRACT FILE: signatures and rules are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from wardmind.core import client_updates, import_flow, importing, secrets
from wardmind.core.feeds import signals
from wardmind.core.feeds.http import FeedError, FeedHttp
from wardmind.core.feeds.models import FeedRole, FeedSettings, FeedStatus, Schedule
from wardmind.core.feeds.providers import get_provider
from wardmind.core.importing import DateOrder, Table
from wardmind.core.models import Account
from wardmind.core.store import WRITE_LOCK, ConnectionConfig, LocalStore

TICK_SECONDS = 60  # how often the scheduler looks for due feeds
ON_CHANGE_FALLBACK_MINUTES = 1440  # an ON_CHANGE feed whose mtime can't be read syncs daily
RETRY_MINUTES = 15  # after a failed sync, wait at least this long before trying again

_STOP_WAIT_SECONDS = 5.0  # stop() waits this long for a tick that is already running
NO_SIGNAL_ROWS = "The source sent no rows."
SYNC_FAILED = "Wardmind couldn't finish this sync. Try Sync now."
_UNICODE_FAILED = (
    "The key or password has characters that can't be sent. Check you copied only the key."
)
_DATE_ORDERS = ("dmy", "mdy", "ymd")
_log = logging.getLogger(__name__)


def no_match_message(rows: int) -> str:
    """The failure for a SIGNALS source whose rows match no account in the book."""
    return f"None of the {rows} rows matched an account in your book. Check Match accounts by."


class SyncOutcome(BaseModel):
    status: FeedStatus  # also saved on the connection
    removed_accounts: int = 0  # ACCOUNTS: accounts in the last snapshot that are gone now


def secret_name(connection_id: str) -> str:
    """The secrets name for a feed's key: "feed-<n>" for connection "feed-<n>"."""
    return connection_id


def fetch_with(provider_id: str, settings: dict[str, str], secret: str | None) -> Table:
    """Run one provider with its settings and secret through a fresh FeedHttp, closed afterwards.
    Raises http.FeedError."""
    provider = get_provider(provider_id)
    if provider is None:
        raise FeedError("Wardmind doesn't know this kind of connection.")
    http = FeedHttp(secret=secret)
    try:
        return provider.fetch(dict(settings), secret, http)
    finally:
        http.close()


def fetch_table(config: ConnectionConfig, store: LocalStore) -> Table:
    """Run the connection's provider (catalog id config.feed.provider) with its settings and its
    secret (secrets.get_secret(secret_name(config.id))) through a fresh http.FeedHttp, closed
    afterwards. Raises http.FeedError."""
    feed = _feed_of(config)
    secret = secrets.get_secret(secret_name(config.id))
    return fetch_with(feed.provider, feed.settings, secret)


def sync_feed(
    config: ConnectionConfig,
    store: LocalStore,
    *,
    book_accounts: list[Account],
    now: datetime,
    today: date,
    table: Table | None = None,
) -> SyncOutcome:
    """Sync one FEED connection and save the result. `table` skips the fetch (tests, and the
    first sync right after the setup page already fetched it).

    ACCOUNTS role: importing.build_import with the connection's mapping and date order (the
    connection id is "feed-<n>", so account ids are "fd<n>-..."), then the snapshot is saved
    exactly as import_flow.commit saves a spreadsheet's (accounts replaced, signal history kept,
    an ImportRecord added with file_name = the provider's name). A sync where no row is usable is
    a failure: the old snapshot stays.

    SIGNALS role: signals.build_signal_readings against `book_accounts` (the book without this
    connection's own data), source=f"feed:{config.id}". The snapshot keeps accounts=[] and merges
    the readings into its signal history with importing.merge_into_snapshot_signals, keeping
    every matched account id. A source with no rows, or with rows and none matching an account,
    is a failure like an ACCOUNTS sync with no usable rows: the old snapshot stays.

    CLIENT_UPDATES role: client_updates.ingest with the feed's mapping, date order and the ids of
    `book_accounts`. New rows become pending updates; no snapshot is written. A source with no
    rows is a success (a new form has no responses yet). A mapping without a required question is
    a failure: ingest's message is the last_error.

    Either way the status is updated and saved with the connection (store.update_connection):
    success sets last_synced_at, counts and clears last_error; failure sets last_error and keeps
    everything else (see _last_error for which messages are shown). last_attempt_at is always
    set. Never raises for a source problem; returns the outcome.

    Locking: the source is read with no lock held (it can be a network call). Everything after
    that runs under store.WRITE_LOCK, starting with a fresh load of the connection: if it was
    removed, or is now another provider or role, the fetched table is dropped and nothing is
    written for it."""
    feed = _feed_of(config)
    provider = get_provider(feed.provider)
    provider_name = provider.info.name if provider is not None else feed.provider
    # Read the file's time before its data, so a change made during the sync is seen next time.
    mtime = provider.source_mtime(dict(feed.settings)) if provider is not None else None
    attempt = feed.status.model_copy(update={"last_attempt_at": now})
    if table is None:
        try:
            table = fetch_table(config, store)
        except Exception as exc:
            status = attempt.model_copy(update={"last_error": _last_error(exc, fetching=True)})
            _save_status(store, config, status)
            return SyncOutcome(status=status)
    removed = 0
    with WRITE_LOCK:
        current = _current_feed(store, config)
        if current is None:
            return SyncOutcome(status=attempt)
        try:
            status, removed = _apply(
                current,
                store,
                table,
                book_accounts=book_accounts,
                now=now,
                today=today,
                provider_name=provider_name,
            )
            status = status.model_copy(update={"source_mtime": mtime})
        except Exception as exc:
            status = attempt.model_copy(update={"last_error": _last_error(exc, fetching=False)})
        _save_status(store, current, status)
    return SyncOutcome(status=status, removed_accounts=removed)


def _last_error(exc: Exception, *, fetching: bool) -> str:
    """What last_error says about a failed sync. Only Wardmind's own messages are shown: a
    FeedError's; for a UnicodeError (httpx names the character of a key it can't send) the
    key-characters message; and, from the mapping and validation step only, a plain ValueError's
    message (type exactly ValueError: Wardmind raises those with its own text, while subclasses
    such as pydantic's ValidationError carry the data). Anything else gets the generic message,
    so no text from a library or the source reaches the screen."""
    if isinstance(exc, FeedError):
        return exc.message
    if isinstance(exc, UnicodeError):
        return _UNICODE_FAILED
    if not fetching and type(exc) is ValueError:
        return str(exc)
    return SYNC_FAILED


def _current_feed(store: LocalStore, config: ConnectionConfig) -> ConnectionConfig | None:
    """The connection as it is now, when it is still this feed (same provider and role). None
    when it was removed, or changed to another provider or role, while its source was read."""
    latest = store.get_connection(config.id)
    if latest is None or latest.feed is None or config.feed is None:
        return None
    if latest.feed.provider != config.feed.provider or latest.feed.role is not config.feed.role:
        return None
    return latest


def _apply(
    config: ConnectionConfig,
    store: LocalStore,
    table: Table,
    *,
    book_accounts: list[Account],
    now: datetime,
    today: date,
    provider_name: str,
) -> tuple[FeedStatus, int]:
    """Process a fetched table for one feed by its role. Returns the status and, for an accounts
    feed, how many accounts the last snapshot had that are gone now. Called with WRITE_LOCK held.
    Raises FeedError, or ValueError with a mapping or validation message."""
    feed = _feed_of(config)
    if feed.role is FeedRole.ACCOUNTS:
        return _sync_accounts(
            config, store, table, now=now, today=today, provider_name=provider_name
        )
    if feed.role is FeedRole.CLIENT_UPDATES:
        status = _sync_client_updates(
            config,
            store,
            table,
            book_ids={account.id for account in book_accounts},
            now=now,
        )
        return status, 0
    status = _sync_signals(
        config,
        store,
        table,
        book_accounts=book_accounts,
        now=now,
        provider_name=provider_name,
    )
    return status, 0


def _feed_of(config: ConnectionConfig) -> FeedSettings:
    if config.feed is None:
        raise ValueError(f"Connection {config.id} isn't a feed.")
    return config.feed


def _date_order(config: ConnectionConfig) -> DateOrder:
    """The connection's remembered date order, day first when there is none."""
    if config.date_order in _DATE_ORDERS:
        return DateOrder(config.date_order)
    return DateOrder.DMY


def _sync_accounts(
    config: ConnectionConfig,
    store: LocalStore,
    table: Table,
    *,
    now: datetime,
    today: date,
    provider_name: str,
) -> tuple[FeedStatus, int]:
    """Import the table as this feed's accounts and save the snapshot. Raises FeedError when no
    row can be used (nothing is saved then) and ValueError for a mapping that doesn't fit."""
    result = importing.build_import(
        table,
        config.mapping,
        connection_id=config.id,
        date_order=_date_order(config),
        imported_at=now,
        today=today,
    )
    if result.ready_rows == 0:
        if not table.rows:
            raise FeedError("The source has no rows, so the accounts from the last sync are kept.")
        raise FeedError(
            "None of its rows could be used, so the accounts from the last sync are kept."
        )
    # The readings say where they came from: this feed, not a spreadsheet.
    readings = [
        signal.model_copy(update={"source": f"feed:{config.id}"}) for signal in result.signals
    ]
    removed = import_flow.save_import(
        store,
        config.id,
        result.model_copy(update={"signals": readings}),
        kept_ids={account.id for account in result.accounts},
        file_name=provider_name,
        stamp=now,
    )
    status = FeedStatus(
        last_synced_at=now,
        last_attempt_at=now,
        last_error=None,
        rows=len(table.rows),
        accounts=result.ready_rows,
        skipped_rows=result.skipped_rows,
    )
    return status, removed


def _sync_client_updates(
    config: ConnectionConfig,
    store: LocalStore,
    table: Table,
    *,
    book_ids: set[str],
    now: datetime,
) -> FeedStatus:
    """Turn the form's new rows into pending client updates. Nothing is written to a snapshot.
    Raises ValueError (ingest's plain message) when the mapping lacks a required question."""
    result = client_updates.ingest(
        store,
        table,
        dict(config.mapping),
        connection_id=config.id,
        book_ids=book_ids,
        date_order=_date_order(config),
        now=now,
    )
    return FeedStatus(
        last_synced_at=now,
        last_attempt_at=now,
        last_error=None,
        rows=len(table.rows),
        accounts=result.accounts,
        skipped_rows=result.ignored,
    )


def _sync_signals(
    config: ConnectionConfig,
    store: LocalStore,
    table: Table,
    *,
    book_accounts: list[Account],
    now: datetime,
    provider_name: str,
) -> FeedStatus:
    """Attach the table's readings to the accounts in the book and save them in the snapshot.
    Raises FeedError (nothing saved) when the source has no rows or none of them matches."""
    feed = _feed_of(config)
    if not table.rows:
        raise FeedError(NO_SIGNAL_ROWS)
    readings = signals.build_signal_readings(
        table,
        config.mapping,
        match_by=feed.match_by,
        accounts=book_accounts,
        date_order=_date_order(config),
        observed_at=now,
        source=f"feed:{config.id}",
    )
    if readings.unmatched_count == len(table.rows):
        raise FeedError(no_match_message(len(table.rows)))
    result = importing.ImportResult(
        accounts=[],
        signals=readings.signals,
        issues=readings.issues,
        skipped_rows=readings.unmatched_count,
        ready_rows=readings.matched_accounts,
    )
    import_flow.save_import(
        store,
        config.id,
        result,
        kept_ids={signal.account_id for signal in readings.signals},
        file_name=provider_name,
        stamp=now,
    )
    return FeedStatus(
        last_synced_at=now,
        last_attempt_at=now,
        last_error=None,
        rows=len(table.rows),
        accounts=readings.matched_accounts,
        skipped_rows=readings.unmatched_count,
        unmatched=readings.unmatched,
    )


def _save_status(store: LocalStore, config: ConnectionConfig, status: FeedStatus) -> None:
    """Write the status onto the connection as it is now (a change made meanwhile is kept). A
    connection removed during the sync is left alone."""
    with WRITE_LOCK:
        latest = store.get_connection(config.id)
        if latest is None or latest.feed is None:
            return
        feed = latest.feed.model_copy(update={"status": status})
        store.update_connection(latest.model_copy(update={"feed": feed}))


def is_due(config: ConnectionConfig, now: datetime, mtime: float | None) -> bool:
    """Whether a FEED connection should sync now. Disabled connections and MANUAL schedules are
    never due. A connection that has never synced is due. After a failure, not before
    RETRY_MINUTES since last_attempt_at. ON_CHANGE: due when `mtime` differs from
    status.source_mtime (or, when mtime is None, after ON_CHANGE_FALLBACK_MINUTES). Timed
    schedules: due when last_synced_at is at least the interval ago."""
    feed = config.feed
    if feed is None or not config.enabled or feed.schedule is Schedule.MANUAL:
        return False
    status = feed.status
    if status.last_error is not None and status.last_attempt_at is not None:
        return now - status.last_attempt_at >= timedelta(minutes=RETRY_MINUTES)
    if status.last_synced_at is None:
        return True
    if feed.schedule is Schedule.ON_CHANGE:
        if mtime is not None:
            return mtime != status.source_mtime
        return now - status.last_synced_at >= timedelta(minutes=ON_CHANGE_FALLBACK_MINUTES)
    minutes = feed.schedule.minutes
    return minutes is not None and now - status.last_synced_at >= timedelta(minutes=minutes)


class FeedScheduler:
    """Calls `tick` every TICK_SECONDS on a daemon thread until stop(). A tick that raises is
    logged as one line and the scheduler keeps going. Only one tick runs at a time; start() is
    idempotent. The web app starts it; the MCP server and CLI don't (they read the last sync)."""

    def __init__(self, tick: Callable[[], None], *, interval: float = TICK_SECONDS) -> None:
        self._tick = tick
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if not self._stop.is_set():
                    return  # already running
                self._thread.join(timeout=_STOP_WAIT_SECONDS)  # a stop is still finishing a tick
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop,), name="wardmind-feeds", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_STOP_WAIT_SECONDS)
        with self._lock:
            if thread is not None and not thread.is_alive() and self._thread is thread:
                self._thread = None

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                _log.warning("Feed check stopped with %s; it will try again.", type(exc).__name__)
            if stop.wait(self._interval):
                break
