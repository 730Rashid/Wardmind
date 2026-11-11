"""The activity log: what happened in Wardmind and when, for Settings > Activity log.

CONTRACT FILE (the API below). The log lives in the per-user root folder,
secrets.root_dir() / LOG_FILE, beside keys.json, as JSON lines. It is plain text even when
encryption is on, so an entry holds only an event kind, the time, a book id and a short detail
chosen by Wardmind's own code: never a customer's name, a note, an email address, a token, a
password, a file path or anything typed by a person. record() never raises: logging must never
stop the action it describes.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from wardmind.core import config
from wardmind.core.secrets import root_dir

LOG_FILE = "activity-log.jsonl"
KEEP = 1000  # entries kept; older ones are dropped
MAX_DETAIL = 120  # characters in one entry's detail

_TRIM_AT = KEEP + 200  # the file is rewritten to its newest KEEP lines once it passes this
_BOOK_ID = re.compile(r"default|b-[0-9a-f]{8}")  # core/books.py ids; a label never matches
_LOCK = threading.RLock()  # one writer at a time in this process; re-entrant for clear()
# The log file this process last counted, and its line count since (so a trim check needs no
# re-read). A trim re-reads the file, so a stale count only costs one extra read.
_counted_path: Path | None = None
_counted_lines = 0


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
    KEYS_REPLACED = "keys_replaced"  # Replace all keys: new master and book keys
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
        return _GROUPS[self]

    @property
    def label(self) -> str:
        """Plain words for the log page, e.g. "Unlock failed"."""
        return _LABELS[self]


_GROUPS: dict[Event, Group] = {
    Event.SESSION_STARTED: Group.SECURITY,
    Event.SIGN_IN: Group.SECURITY,
    Event.SIGN_IN_REFUSED: Group.SECURITY,
    Event.UNLOCKED: Group.SECURITY,
    Event.UNLOCK_FAILED: Group.SECURITY,
    Event.LOCKED: Group.SECURITY,
    Event.ENCRYPTION_ON: Group.SECURITY,
    Event.ENCRYPTION_OFF: Group.SECURITY,
    Event.PASSWORD_SET: Group.SECURITY,
    Event.PASSWORD_REMOVED: Group.SECURITY,
    Event.RECOVERY_KEY_REPLACED: Group.SECURITY,
    Event.KEYS_REPLACED: Group.SECURITY,
    Event.BOOK_ADDED: Group.DATA,
    Event.BOOK_RENAMED: Group.DATA,
    Event.BOOK_DELETED: Group.DATA,
    Event.BOOK_OPENED: Group.DATA,
    Event.CONNECTION_ADDED: Group.DATA,
    Event.CONNECTION_REMOVED: Group.DATA,
    Event.KEY_SAVED: Group.DATA,
    Event.KEY_DELETED: Group.DATA,
    Event.MAILBOX_CONNECTED: Group.DATA,
    Event.MAILBOX_DISCONNECTED: Group.DATA,
    Event.BACKUP_MADE: Group.DATA,
    Event.BACKUP_RESTORED: Group.DATA,
    Event.EXPORTED: Group.DATA,
    Event.ALL_DATA_DELETED: Group.DATA,
    Event.SETTINGS_SAVED: Group.SETTINGS,
    Event.LOG_CLEARED: Group.SETTINGS,
}

_LABELS: dict[Event, str] = {
    Event.SESSION_STARTED: "Session started",
    Event.SIGN_IN: "Signed in",
    Event.SIGN_IN_REFUSED: "Sign-in link refused",
    Event.UNLOCKED: "Unlocked",
    Event.UNLOCK_FAILED: "Unlock failed",
    Event.LOCKED: "Locked",
    Event.ENCRYPTION_ON: "Encryption turned on",
    Event.ENCRYPTION_OFF: "Encryption turned off",
    Event.PASSWORD_SET: "Password set",
    Event.PASSWORD_REMOVED: "Password removed",
    Event.RECOVERY_KEY_REPLACED: "Recovery key replaced",
    Event.KEYS_REPLACED: "All keys replaced",
    Event.BOOK_ADDED: "Book added",
    Event.BOOK_RENAMED: "Book renamed",
    Event.BOOK_DELETED: "Book deleted",
    Event.BOOK_OPENED: "Book opened",
    Event.CONNECTION_ADDED: "Connection added",
    Event.CONNECTION_REMOVED: "Connection removed",
    Event.KEY_SAVED: "Key saved",
    Event.KEY_DELETED: "Key deleted",
    Event.MAILBOX_CONNECTED: "Mailbox connected",
    Event.MAILBOX_DISCONNECTED: "Mailbox disconnected",
    Event.BACKUP_MADE: "Backup made",
    Event.BACKUP_RESTORED: "Backup restored",
    Event.EXPORTED: "Export made",
    Event.ALL_DATA_DELETED: "All local data deleted",
    Event.SETTINGS_SAVED: "Settings saved",
    Event.LOG_CLEARED: "Log cleared",
}


class Entry(BaseModel):
    at: datetime  # timezone-aware, UTC
    event: Event
    book: str | None = None  # a book id from core/books.py, never a label
    detail: str = ""  # at most MAX_DETAIL characters, from Wardmind's own code


def _log_path() -> Path:
    return root_dir() / LOG_FILE


def _clean_detail(text: str) -> str:
    """The detail with control, format and line-separator characters turned into spaces (or
    dropped), runs of spaces collapsed, and cut to MAX_DETAIL."""
    kept: list[str] = []
    for char in str(text):
        category = unicodedata.category(char)
        if category in ("Cc", "Zl", "Zp"):
            kept.append(" ")
        elif category[0] != "C":
            kept.append(char)
    return " ".join("".join(kept).split())[:MAX_DETAIL].rstrip()


def _book_or_none(book: str | None) -> str | None:
    """The book id when it is one, else None. A label is never stored, even if passed by mistake."""
    if book is not None and _BOOK_ID.fullmatch(book):
        return book
    return None


def _count_lines(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.split("\n") if line)


def _append(path: Path, line: str) -> None:
    """Append one line. A file whose last line was cut short (a crash mid-write) gets a line
    break first, so the new entry is never glued to the damaged one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(prefix + line)


def _trim(path: Path) -> None:
    """Rewrite the file with its newest KEEP lines, atomically."""
    global _counted_path, _counted_lines
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in text.split("\n") if line]
    if len(lines) > KEEP:
        lines = lines[-KEEP:]
        config.write_atomic(path, "\n".join(lines) + "\n")
    _counted_path, _counted_lines = path, len(lines)


def record(event: Event, *, book: str | None = None, detail: str = "") -> None:
    """Append one entry. Never raises (a failure to log is swallowed)."""
    global _counted_path, _counted_lines
    try:
        entry = Entry(
            at=datetime.now(UTC),
            event=Event(event),
            book=_book_or_none(book),
            detail=_clean_detail(detail),
        )
        line = entry.model_dump_json() + "\n"
        with _LOCK:
            path = _log_path()
            if _counted_path != path:  # the first entry this process writes to this file
                _counted_path, _counted_lines = path, _count_lines(path)
            _append(path, line)
            _counted_lines += 1
            if _counted_lines > _TRIM_AT:
                _trim(path)
    except Exception:  # noqa: BLE001 - logging must never stop the action it describes
        return


def entries(limit: int = KEEP) -> list[Entry]:
    """The newest `limit` entries, newest first. A damaged line is skipped."""
    found: list[Entry] = []
    try:
        with _LOCK:
            path = _log_path()
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except Exception:  # noqa: BLE001 - an unreadable log reads as empty
        return []
    for line in reversed(lines):
        if len(found) >= limit:
            break
        if not line.strip():
            continue
        try:
            found.append(Entry.model_validate_json(line))
        except ValueError:  # a damaged line, or an event this version doesn't know
            continue
    return found


def last(event: Event, *, book: str | None = None) -> Entry | None:
    """The newest entry of this kind (for this book, when given), or None."""
    for entry in entries(KEEP):
        if entry.event == event and (book is None or entry.book == book):
            return entry
    return None


def clear() -> None:
    """Empty the log, then record LOG_CLEARED as its first entry."""
    global _counted_path
    with _LOCK:
        try:
            config.write_atomic(_log_path(), "")
        except OSError:
            return  # nothing was cleared, so nothing is recorded
        _counted_path = None  # recount on the next entry
        record(Event.LOG_CLEARED)
