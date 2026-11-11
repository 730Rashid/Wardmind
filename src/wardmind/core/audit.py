"""The activity log: what happened in Wardmind and when, for Settings > Activity log.

CONTRACT FILE (the API below). The log lives in the per-user root folder,
secrets.root_dir() / LOG_FILE, beside keys.json, as JSON lines. It is plain text even when
encryption is on, so an entry holds only an event kind, the time, a book id and a short detail
chosen by Wardmind's own code: never a customer's name, a note, an email address, a token, a
password, a file path or anything typed by a person. record() never raises: logging must never
stop the action it describes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

LOG_FILE = "activity-log.jsonl"
KEEP = 1000  # entries kept; older ones are dropped
MAX_DETAIL = 120  # characters in one entry's detail


class Group(StrEnum):
    SECURITY = "security"
    DATA = "data"
    SETTINGS = "settings"


class Event(StrEnum):
    # Security
    SESSION_STARTED = "session_started"  # wardmind ui / the desktop app started a session
    SIGN_IN = "sign_in"  # the sign-in link was used
    SIGN_IN_REFUSED = "sign_in_refused"  # a wrong or old sign-in link (counted, not each one)
    UNLOCKED = "unlocked"
    UNLOCK_FAILED = "unlock_failed"
    LOCKED = "locked"  # detail: "by you" or "after N idle minutes"
    ENCRYPTION_ON = "encryption_on"
    ENCRYPTION_OFF = "encryption_off"
    PASSWORD_SET = "password_set"
    PASSWORD_REMOVED = "password_removed"
    RECOVERY_KEY_REPLACED = "recovery_key_replaced"
    # Data
    BOOK_ADDED = "book_added"
    BOOK_RENAMED = "book_renamed"
    BOOK_DELETED = "book_deleted"
    BOOK_OPENED = "book_opened"
    CONNECTION_ADDED = "connection_added"
    CONNECTION_REMOVED = "connection_removed"
    KEY_SAVED = "key_saved"  # a token or API key was saved; detail names the service only
    KEY_DELETED = "key_deleted"
    MAILBOX_CONNECTED = "mailbox_connected"
    MAILBOX_DISCONNECTED = "mailbox_disconnected"
    BACKUP_MADE = "backup_made"
    BACKUP_RESTORED = "backup_restored"
    EXPORTED = "exported"  # detail: which export
    ALL_DATA_DELETED = "all_data_deleted"
    # Settings
    SETTINGS_SAVED = "settings_saved"  # detail: which section
    LOG_CLEARED = "log_cleared"

    @property
    def group(self) -> Group:
        raise NotImplementedError

    @property
    def label(self) -> str:
        """Plain words for the log page, e.g. "Unlock failed"."""
        raise NotImplementedError


class Entry(BaseModel):
    at: datetime  # timezone-aware, UTC
    event: Event
    book: str | None = None  # a book id from core/books.py, never a label
    detail: str = ""  # at most MAX_DETAIL characters, from Wardmind's own code


def record(event: Event, *, book: str | None = None, detail: str = "") -> None:
    """Append one entry. Never raises (a failure to log is swallowed)."""


def entries(limit: int = KEEP) -> list[Entry]:
    """The newest `limit` entries, newest first. A damaged line is skipped."""
    return []


def last(event: Event, *, book: str | None = None) -> Entry | None:
    """The newest entry of this kind (for this book, when given), or None."""
    return None


def clear() -> None:
    """Empty the log, then record LOG_CLEARED as its first entry."""
