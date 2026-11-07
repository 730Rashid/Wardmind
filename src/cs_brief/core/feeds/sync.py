"""Running feeds: one sync, what is due, and the background scheduler.

CONTRACT FILE: signatures and rules are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime

from pydantic import BaseModel

from cs_brief.core.feeds.models import FeedStatus
from cs_brief.core.importing import Table
from cs_brief.core.models import Account
from cs_brief.core.store import ConnectionConfig, LocalStore

TICK_SECONDS = 60  # how often the scheduler looks for due feeds
ON_CHANGE_FALLBACK_MINUTES = 1440  # an ON_CHANGE feed whose mtime can't be read syncs daily
RETRY_MINUTES = 15  # after a failed sync, wait at least this long before trying again


class SyncOutcome(BaseModel):
    status: FeedStatus  # also saved on the connection
    removed_accounts: int = 0  # ACCOUNTS: accounts in the last snapshot that are gone now


def fetch_table(config: ConnectionConfig, store: LocalStore) -> Table:
    """Run the connection's provider (catalog id config.feed.provider) with its settings and its
    secret (secrets.get_secret(secret_name(config.id))) through a fresh http.FeedHttp, closed
    afterwards. Raises http.FeedError."""
    raise NotImplementedError


def secret_name(connection_id: str) -> str:
    """The secrets name for a feed's key: "feed-<n>" for connection "feed-<n>"."""
    raise NotImplementedError


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
    every matched account id.

    Either way the status is updated and saved with the connection (store.update_connection):
    success sets last_synced_at, counts and clears last_error; failure (FeedError, ValueError)
    sets last_error to the plain message and keeps everything else. last_attempt_at is always
    set. Never raises for a source problem; returns the outcome."""
    raise NotImplementedError


def is_due(config: ConnectionConfig, now: datetime, mtime: float | None) -> bool:
    """Whether a FEED connection should sync now. Disabled connections and MANUAL schedules are
    never due. A connection that has never synced is due. After a failure, not before
    RETRY_MINUTES since last_attempt_at. ON_CHANGE: due when `mtime` differs from
    status.source_mtime (or, when mtime is None, after ON_CHANGE_FALLBACK_MINUTES). Timed
    schedules: due when last_synced_at is at least the interval ago."""
    raise NotImplementedError


class FeedScheduler:
    """Calls `tick` every TICK_SECONDS on a daemon thread until stop(). A tick that raises is
    logged as one line and the scheduler keeps going. Only one tick runs at a time; start() is
    idempotent. The web app starts it; the MCP server and CLI don't (they read the last sync)."""

    def __init__(self, tick: Callable[[], None], *, interval: float = TICK_SECONDS) -> None:
        self._tick = tick
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError
