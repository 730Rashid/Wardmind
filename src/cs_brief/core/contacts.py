"""Contacts: the people at each customer (Your data only).

A contact belongs to one account id, the one the account page shows. When accounts are linked,
the members' contacts move to the primary (editing.link_accounts), so a page never has to merge
lists. Removing a connection (service.remove_connection) or deleting a hand-added account
(editing.delete_account) calls forget() for the accounts that went. The file is
store.root / store.CONTACTS_FILE, written with config.write_atomic.

Champion rule: an account's champion is its main contact. When the last active champion is
marked as left (by the CSM, or by a client update the CSM accepts), a manual Signal
CHAMPION_LEFT = 1.0 is recorded (source SIGNAL_SOURCE), which raises the account's risk. When an
active champion is added or set again after that, CHAMPION_LEFT = 0.0 is recorded, which clears
it. Nothing is recorded when the latest CHAMPION_LEFT reading already says the same.

Contacts are personal data: they stay on this computer (data/contacts.json), the MCP server
returns names, titles, champion and status but never emails, and removing a contact deletes it.

Demo mode: demo accounts get a few invented contacts (demo_contacts), shown read-only.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from cs_brief.core.store import LocalStore

MAX_CONTACTS_PER_ACCOUNT = 50
MAX_NAME = 80
MAX_TITLE = 80
MAX_EMAIL = 254
SIGNAL_SOURCE = "contacts"  # Signal.source of the readings the champion rule records


class ContactStatus(StrEnum):
    ACTIVE = "active"
    LEFT = "left"

    @property
    def label(self) -> str:
        return {"active": "Active", "left": "Left"}[self.value]


class ContactSource(StrEnum):
    YOU = "you"  # added or changed by the CSM
    CLIENT = "client"  # from a client update the CSM accepted


class Contact(BaseModel):
    id: str  # "c-" + 8 hex, unique within the account
    name: str  # 1..MAX_NAME characters, no line breaks or control characters
    title: str = ""  # job title, 0..MAX_TITLE
    email: str = ""  # "" or a plausible address (one "@", a dot in the domain), 0..MAX_EMAIL
    champion: bool = False
    status: ContactStatus = ContactStatus.ACTIVE
    left_on: date | None = None  # set when status is LEFT
    source: ContactSource = ContactSource.YOU
    updated_at: datetime  # naive UTC


class ContactsData(BaseModel):
    """data/contacts.json: account id -> its contacts."""

    by_account: dict[str, list[Contact]] = Field(default_factory=dict)


class ContactInput(BaseModel):
    """What a form (or an accepted client update) supplies. Strings are stripped by validate()."""

    name: str
    title: str = ""
    email: str = ""
    champion: bool = False


def validate(values: ContactInput) -> ContactInput:
    """The values stripped, or ValueError with one plain message naming the field ("Type the
    contact's name.", "That email address doesn't look right.", "Keep the job title under 80
    characters.", "A name can't contain a line break.")."""
    raise NotImplementedError


def contacts_for(store: LocalStore, account_id: str) -> list[Contact]:
    """The account's contacts: active before left; champions first; then by name (casefolded)."""
    raise NotImplementedError


def add_contact(
    store: LocalStore,
    account_id: str,
    values: ContactInput,
    *,
    source: ContactSource,
    now: datetime,
) -> Contact:
    """Validate and add. ValueError when the account already has MAX_CONTACTS_PER_ACCOUNT, or an
    active contact with the same email (casefolded) exists. Applies the champion rule."""
    raise NotImplementedError


def update_contact(
    store: LocalStore,
    account_id: str,
    contact_id: str,
    values: ContactInput,
    *,
    source: ContactSource,
    now: datetime,
) -> Contact:
    """Replace name, title, email and champion. LookupError for an unknown id. Applies the
    champion rule."""
    raise NotImplementedError


def mark_left(
    store: LocalStore,
    account_id: str,
    contact_id: str,
    *,
    left_on: date,
    source: ContactSource,
    now: datetime,
) -> Contact:
    """Status LEFT on that date (champion flag kept, so the history says who it was). LookupError
    for an unknown id. Applies the champion rule."""
    raise NotImplementedError


def mark_active(store: LocalStore, account_id: str, contact_id: str, *, now: datetime) -> Contact:
    """Undo mark_left (a mistake). Applies the champion rule."""
    raise NotImplementedError


def remove_contact(store: LocalStore, account_id: str, contact_id: str) -> bool:
    """Delete it for good. True if one was removed. Readings already recorded stay (history)."""
    raise NotImplementedError


def move_contacts(store: LocalStore, from_id: str, to_id: str) -> int:
    """Move every contact of from_id to to_id (linking accounts); a moved contact whose email
    matches an active contact already on to_id is dropped. Returns how many moved."""
    raise NotImplementedError


def find_contact(contacts: list[Contact], *, email: str, name: str) -> Contact | None:
    """The one contact a client update means: by email (casefolded) when given, else by name
    (casefolded, spaces collapsed). None when nothing or more than one matches."""
    raise NotImplementedError


def forget(store: LocalStore, gone: Callable[[str], bool]) -> int:
    """Delete the contacts of every account id for which gone(id) is true; how many accounts."""
    raise NotImplementedError


def demo_contacts(account_id: str, account_name: str) -> list[Contact]:
    """2 or 3 invented contacts for a demo account, the same every time for the same id: one
    champion, roles such as "VP Operations", emails at the "example.com" domain only."""
    raise NotImplementedError
