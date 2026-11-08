"""Data feeds: connections that keep themselves up to date from a file, a link, an API, a local
database or a known product (Stripe, Zendesk, ...).

Every provider turns its source into an importing.Table, so one pipeline handles them all:
- An ACCOUNTS feed is an automatic spreadsheet import: the Table goes through the same column
  matching and importing.build_import, and its snapshot replaces the accounts and keeps the
  signal history (connection id "feed-<n>", account ids "fd<n>-...").
- A CLIENT_UPDATES feed reads the responses of the CSM's client update form; new rows become
  pending updates for the CSM to accept or reject (client_updates.ingest). Its mapping uses the
  client_updates.QUESTIONS keys.
- A SIGNALS feed adds readings to accounts that are already in the book, found by ID, website
  or name (signals.build_signal_readings). Rows that match no account are kept in the status so
  the CSM can see them.

Nothing here is pushed to cs-brief: it fetches, on the feed's schedule or when the CSM presses
Sync now. Every request is read-only.

CONTRACT FILE: models, enums and their values are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class FeedRole(StrEnum):
    ACCOUNTS = "accounts"  # the feed lists accounts (name, ARR, renewal date, ...)
    SIGNALS = "signals"  # the feed adds readings to accounts already in the book
    # The responses of the CSM's client update form: each new row becomes a pending update for
    # the CSM to accept or reject (core/client_updates.py). Never changes anything by itself.
    CLIENT_UPDATES = "client_updates"

    @property
    def label(self) -> str:
        return {
            "accounts": "Adds accounts",
            "signals": "Adds signals to my accounts",
            "client_updates": "Brings in client updates",
        }[self.value]


class MatchBy(StrEnum):
    """How a SIGNALS feed finds the account a row belongs to."""

    AUTO = "auto"  # ID, then website, then name: the first that finds exactly one account
    ID = "id"
    WEBSITE = "website"
    NAME = "name"

    @property
    def label(self) -> str:
        return {
            "auto": "ID, then website, then name",
            "id": "ID",
            "website": "Website",
            "name": "Account name",
        }[self.value]


class Category(StrEnum):
    """What a provider brings in. The catalog's sidebar filters by these."""

    ACCOUNTS = "accounts"
    USAGE = "usage"
    TICKETS = "tickets"
    BILLING = "billing"
    FILES = "files"

    @property
    def label(self) -> str:
        return {
            "accounts": "Accounts",
            "usage": "Product usage",
            "tickets": "Support tickets",
            "billing": "Billing",
            "files": "Files and links",
        }[self.value]


class Schedule(StrEnum):
    """How often a feed syncs by itself. ON_CHANGE is for files: it syncs when the file changes."""

    ON_CHANGE = "on_change"
    EVERY_15_MINUTES = "15"
    HOURLY = "60"
    EVERY_6_HOURS = "360"
    DAILY = "1440"
    MANUAL = "manual"  # only when the CSM presses Sync now

    @property
    def label(self) -> str:
        return {
            "on_change": "When the file changes",
            "15": "Every 15 minutes",
            "60": "Every hour",
            "360": "Every 6 hours",
            "1440": "Once a day",
            "manual": "Only when I press Sync now",
        }[self.value]

    @property
    def minutes(self) -> int | None:
        """The interval, or None for ON_CHANGE and MANUAL."""
        return int(self.value) if self.value.isdigit() else None


class FieldKind(StrEnum):
    TEXT = "text"
    URL = "url"
    PATH = "path"  # a file on this computer
    SECRET = "secret"  # stored with core/secrets.py, never in connections.json, shown masked
    CHOICE = "choice"
    QUERY = "query"  # multi-line text (SQL, HogQL)


class SetupField(BaseModel):
    """One input on a provider's setup page."""

    key: str  # the settings key (or "secret" for the one SECRET field a provider may have)
    label: str
    kind: FieldKind
    required: bool = True
    help: str = ""  # one plain sentence under the input
    placeholder: str = ""
    default: str = ""
    choices: list[tuple[str, str]] = []  # CHOICE only: (value, label)


class ProviderInfo(BaseModel):
    """A card in the Add a connection catalog, and what its setup page asks for."""

    id: str  # e.g. "stripe"; also the key in catalog.PROVIDERS
    name: str  # "Stripe"
    by: str  # "by Stripe", "by cs-brief" for the generic ones
    description: str  # one or two sentences: what it brings in
    categories: list[Category]
    roles: list[FeedRole]  # the roles it can play; the first is the default
    fields: list[SetupField]
    schedules: list[Schedule]  # offered, first is the default
    mark: str  # 1–2 letters for the card's tile, e.g. "St" (no brand logos are bundled)
    docs_hint: str = ""  # where to find the key, e.g. "Developers > API keys > Restricted key"


class UnmatchedRow(BaseModel):
    """A SIGNALS row that matched no account (or more than one)."""

    row: int  # source row number, as importing.Table.row_numbers
    key: str  # what was matched on, as written in the source, e.g. "acme.com"
    reason: str  # "No account has this website." / "2 accounts have this name."


class FeedStatus(BaseModel):
    last_synced_at: datetime | None = None  # naive UTC of the last successful sync
    last_attempt_at: datetime | None = None  # naive UTC, success or not
    last_error: str | None = None  # plain English, never a secret; cleared by a success
    rows: int = 0  # rows read on the last successful sync
    accounts: int = 0  # ACCOUNTS: accounts imported; SIGNALS: accounts that got readings
    skipped_rows: int = 0  # rows the import skipped (ACCOUNTS) or that matched nothing (SIGNALS)
    unmatched: list[UnmatchedRow] = Field(default_factory=list)  # SIGNALS only, at most 200
    source_mtime: float | None = None  # ON_CHANGE: the file's mtime at the last sync


class FeedSettings(BaseModel):
    """Stored on store.ConnectionConfig.feed for a connection of kind FEED."""

    provider: str  # a key of catalog.PROVIDERS
    role: FeedRole
    settings: dict[str, str] = Field(default_factory=dict)  # the non-secret SetupField values
    schedule: Schedule
    match_by: MatchBy = MatchBy.AUTO  # SIGNALS only
    status: FeedStatus = Field(default_factory=FeedStatus)
