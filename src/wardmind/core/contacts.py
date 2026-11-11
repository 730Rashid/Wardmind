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

import hashlib
import random
import re
import secrets
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from wardmind.core import vault
from wardmind.core.models import Signal, SignalKind
from wardmind.core.store import CONTACTS_FILE, WRITE_LOCK, LocalStore

MAX_CONTACTS_PER_ACCOUNT = 50
MAX_NAME = 80
MAX_TITLE = 80
MAX_EMAIL = 254
SIGNAL_SOURCE = "contacts"  # Signal.source of the readings the champion rule records

# One address: a local part of plain ASCII letters, digits and these symbols, one @, then a domain
# of at least two dot-separated labels of letters, digits and hyphens (no label starts or ends
# with a hyphen). This refuses ? & % , ; quotes, angle brackets and spaces, which would let a
# mailto link carry extra fields such as bcc or subject.
_EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_EMAIL_SHAPE = re.compile(
    r"[A-Za-z0-9.!#$'*+/=^_`{|}~-]+@" + _EMAIL_LABEL + r"(?:\." + _EMAIL_LABEL + r")+"
)
# Characters a name or title can't hold: control characters, and format characters such as the
# bidi overrides (U+202A-U+202E, U+2066-U+2069) and zero-width marks (U+200B-U+200F, U+FEFF).
_FORMAT_CATEGORY = "Cf"
_DEMO_PEOPLE = (
    ("Priya", "Shah"),
    ("Tomas", "Berg"),
    ("Maya", "Okonkwo"),
    ("Liam", "Fischer"),
    ("Noor", "Haddad"),
    ("Elena", "Rossi"),
    ("Jonas", "Lind"),
    ("Aiko", "Tanaka"),
)
_DEMO_TITLES = (
    "VP Operations",
    "Head of Finance",
    "IT Director",
    "Operations Manager",
    "Chief Operating Officer",
    "Product Lead",
    "Procurement Manager",
)
_DEMO_STAMP = datetime(2026, 1, 1, 9, 0)  # demo contacts never change, so their stamp is fixed


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
    name = _plain_text(values.name.strip(), "name", MAX_NAME)
    if not name:
        raise ValueError("Type the contact's name.")
    title = _plain_text(values.title.strip(), "job title", MAX_TITLE)
    email = values.email.strip()
    if email:
        if _has_control(email) or _EMAIL_SHAPE.fullmatch(email) is None:
            raise ValueError("That email address doesn't look right.")
        if len(email) > MAX_EMAIL:
            raise ValueError(f"Keep the email address under {MAX_EMAIL} characters.")
    return ContactInput(name=name, title=title, email=email, champion=values.champion)


def _plain_text(text: str, label: str, limit: int) -> str:
    """The text when it is one line of plain text no longer than limit; else ValueError naming
    the field. An empty text is returned as it is (the caller decides whether that is allowed)."""
    if "\n" in text or "\r" in text:
        raise ValueError(f"A {label} can't contain a line break.")
    if _has_control(text):
        raise ValueError(f"A {label} can't contain control characters.")
    if any(unicodedata.category(char) == _FORMAT_CATEGORY for char in text):
        raise ValueError(f"A {label} can't contain hidden formatting characters.")
    if len(text) > limit:
        raise ValueError(f"Keep the {label} under {limit} characters.")
    return text


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def _naive(moment: datetime) -> datetime:
    """A datetime as naive UTC, the shape the store keeps. A naive value is taken as UTC."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _load(store: LocalStore) -> ContactsData:
    """The saved contacts; empty when the file is missing or unreadable."""
    try:
        text = vault.read_text(store.root / CONTACTS_FILE)
        return ContactsData.model_validate_json(text)
    except (OSError, ValueError):  # ValidationError and JSON errors are ValueErrors
        return ContactsData()


def _save(store: LocalStore, data: ContactsData) -> None:
    data.by_account = {account: found for account, found in data.by_account.items() if found}
    vault.write_text(store.root / CONTACTS_FILE, data.model_dump_json(indent=2) + "\n")


def _index_of(contacts: list[Contact], contact_id: str) -> int:
    for index, contact in enumerate(contacts):
        if contact.id == contact_id:
            return index
    raise LookupError("That contact isn't in this account.")


def _new_id(taken: set[str]) -> str:
    while True:
        candidate = "c-" + secrets.token_hex(4)
        if candidate not in taken:
            return candidate


def _email_taken(contacts: list[Contact], email: str, *, except_id: str | None = None) -> bool:
    """Whether an active contact (other than except_id) already has this email, casefolded."""
    if not email:
        return False
    wanted = email.casefold()
    return any(
        contact.status is ContactStatus.ACTIVE
        and contact.id != except_id
        and contact.email.casefold() == wanted
        for contact in contacts
    )


def _has_active_champion(contacts: list[Contact]) -> bool:
    return any(c.champion and c.status is ContactStatus.ACTIVE for c in contacts)


def _latest_champion_reading(store: LocalStore, account_id: str) -> float | None:
    """The value of the newest CHAMPION_LEFT reading the store holds for this account (or for the
    accounts linked with it), from hand-entered readings and every connection's saved readings.
    None when there is none. Readings a live connection reports and the store does not keep
    (HubSpot) are not seen here."""
    ids = {account_id}
    for group in store.links().groups:
        if account_id in group.members:
            ids.update(group.members)
    readings = [
        signal
        for signal in store.manual().signals
        if signal.kind is SignalKind.CHAMPION_LEFT and signal.account_id in ids
    ]
    for config in store.connections():
        snapshot = store.snapshot(config.id)
        if snapshot is not None:
            readings.extend(
                signal
                for signal in snapshot.signals
                if signal.kind is SignalKind.CHAMPION_LEFT and signal.account_id in ids
            )
    if not readings:
        return None
    return max(readings, key=lambda signal: _naive(signal.observed_at)).value


def _champion_rule(
    store: LocalStore, account_id: str, *, before: bool, after: bool, now: datetime
) -> None:
    """Record CHAMPION_LEFT when the account's active-champion state changed, unless the latest
    reading already says the same. Losing the last champion records 1.0; gaining one, 0.0."""
    if before == after:
        return
    value = 0.0 if after else 1.0
    if _latest_champion_reading(store, account_id) == value:
        return
    manual = store.manual()
    reading = Signal(
        account_id=account_id,
        kind=SignalKind.CHAMPION_LEFT,
        value=value,
        observed_at=_naive(now),
        source=SIGNAL_SOURCE,
    )
    store.save_manual(manual.model_copy(update={"signals": [*manual.signals, reading]}))


def contacts_for(store: LocalStore, account_id: str) -> list[Contact]:
    """The account's contacts: active before left; champions first; then by name (casefolded)."""
    found = _load(store).by_account.get(account_id, [])
    return sorted(
        found,
        key=lambda c: (c.status is ContactStatus.LEFT, not c.champion, c.name.casefold()),
    )


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
    clean = validate(values)
    with WRITE_LOCK:
        data = _load(store)
        current = list(data.by_account.get(account_id, []))
        if len(current) >= MAX_CONTACTS_PER_ACCOUNT:
            raise ValueError(
                f"This account already has {MAX_CONTACTS_PER_ACCOUNT} contacts. Remove one first."
            )
        if _email_taken(current, clean.email):
            raise ValueError("Another active contact already has this email address.")
        before = _has_active_champion(current)
        stamp = _naive(now)
        contact = Contact(
            id=_new_id({c.id for c in current}),
            name=clean.name,
            title=clean.title,
            email=clean.email,
            champion=clean.champion,
            status=ContactStatus.ACTIVE,
            left_on=None,
            source=source,
            updated_at=stamp,
        )
        current.append(contact)
        data.by_account[account_id] = current
        _save(store, data)
        _champion_rule(
            store, account_id, before=before, after=_has_active_champion(current), now=stamp
        )
    return contact


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
    clean = validate(values)
    with WRITE_LOCK:
        data = _load(store)
        current = list(data.by_account.get(account_id, []))
        index = _index_of(current, contact_id)
        if _email_taken(current, clean.email, except_id=contact_id):
            raise ValueError("Another active contact already has this email address.")
        stamp = _naive(now)
        before = _has_active_champion(current)
        updated = current[index].model_copy(
            update={
                "name": clean.name,
                "title": clean.title,
                "email": clean.email,
                "champion": clean.champion,
                "source": source,
                "updated_at": stamp,
            }
        )
        current[index] = updated
        data.by_account[account_id] = current
        _save(store, data)
        _champion_rule(
            store, account_id, before=before, after=_has_active_champion(current), now=stamp
        )
    return updated


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
    with WRITE_LOCK:
        data = _load(store)
        current = list(data.by_account.get(account_id, []))
        index = _index_of(current, contact_id)
        stamp = _naive(now)
        before = _has_active_champion(current)
        updated = current[index].model_copy(
            update={
                "status": ContactStatus.LEFT,
                "left_on": left_on,
                "source": source,
                "updated_at": stamp,
            }
        )
        current[index] = updated
        data.by_account[account_id] = current
        _save(store, data)
        _champion_rule(
            store, account_id, before=before, after=_has_active_champion(current), now=stamp
        )
    return updated


def mark_active(store: LocalStore, account_id: str, contact_id: str, *, now: datetime) -> Contact:
    """Undo mark_left (a mistake). Applies the champion rule."""
    with WRITE_LOCK:
        data = _load(store)
        current = list(data.by_account.get(account_id, []))
        index = _index_of(current, contact_id)
        if _email_taken(current, current[index].email, except_id=contact_id):
            raise ValueError("Another active contact already has this email address.")
        stamp = _naive(now)
        before = _has_active_champion(current)
        updated = current[index].model_copy(
            update={"status": ContactStatus.ACTIVE, "left_on": None, "updated_at": stamp}
        )
        current[index] = updated
        data.by_account[account_id] = current
        _save(store, data)
        _champion_rule(
            store, account_id, before=before, after=_has_active_champion(current), now=stamp
        )
    return updated


def remove_contact(store: LocalStore, account_id: str, contact_id: str) -> bool:
    """Delete it for good. True if one was removed. Readings already recorded stay (history)."""
    with WRITE_LOCK:
        data = _load(store)
        current = list(data.by_account.get(account_id, []))
        kept = [contact for contact in current if contact.id != contact_id]
        if len(kept) == len(current):
            return False
        data.by_account[account_id] = kept
        _save(store, data)
        return True


def move_contacts(store: LocalStore, from_id: str, to_id: str) -> int:
    """Move every contact of from_id to to_id (linking accounts); a moved contact whose email
    matches an active contact already on to_id is dropped. Returns how many moved."""
    if from_id == to_id:
        return 0
    with WRITE_LOCK:
        data = _load(store)
        moving = data.by_account.get(from_id, [])
        if not moving:
            return 0
        target = list(data.by_account.get(to_id, []))
        active_emails = {
            contact.email.casefold()
            for contact in target
            if contact.status is ContactStatus.ACTIVE and contact.email
        }
        moved: list[Contact] = []
        for contact in moving:
            if contact.email and contact.email.casefold() in active_emails:
                continue
            moved.append(contact)
        data.by_account.pop(from_id, None)
        data.by_account[to_id] = [*target, *moved]
        _save(store, data)
        return len(moved)


def find_contact(contacts: list[Contact], *, email: str, name: str) -> Contact | None:
    """The one contact a client update means: by email (casefolded) when given, else by name
    (casefolded, spaces collapsed). None when nothing or more than one matches."""
    wanted_email = email.strip().casefold()
    if wanted_email:
        by_email = [c for c in contacts if c.email and c.email.casefold() == wanted_email]
        if by_email:
            return by_email[0] if len(by_email) == 1 else None
    wanted_name = " ".join(name.split()).casefold()
    if not wanted_name:
        return None
    by_name = [c for c in contacts if " ".join(c.name.split()).casefold() == wanted_name]
    return by_name[0] if len(by_name) == 1 else None


def forget(store: LocalStore, gone: Callable[[str], bool]) -> int:
    """Delete the contacts of every account id for which gone(id) is true; how many accounts."""
    with WRITE_LOCK:
        data = _load(store)
        gone_ids = [account for account in data.by_account if gone(account)]
        if not gone_ids:
            return 0
        for account in gone_ids:
            del data.by_account[account]
        _save(store, data)
        return len(gone_ids)


def demo_contacts(account_id: str, account_name: str) -> list[Contact]:
    """2 or 3 invented contacts for a demo account, the same every time for the same id: one
    champion, roles such as "VP Operations", emails at the "example.com" domain only."""
    seed = int.from_bytes(hashlib.sha256(account_id.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    count = rng.choice((2, 3))
    people = rng.sample(_DEMO_PEOPLE, count)
    titles = rng.sample(_DEMO_TITLES, count)
    contacts: list[Contact] = []
    ids: set[str] = set()
    for index, ((first, last), title) in enumerate(zip(people, titles, strict=True)):
        contact_id = f"c-{rng.getrandbits(32):08x}"
        while contact_id in ids:
            contact_id = f"c-{rng.getrandbits(32):08x}"
        ids.add(contact_id)
        contacts.append(
            Contact(
                id=contact_id,
                name=f"{first} {last}",
                title=title,
                email=f"{first}.{last}@example.com".casefold(),
                champion=index == 0,
                status=ContactStatus.ACTIVE,
                source=ContactSource.YOU,
                updated_at=_DEMO_STAMP,
            )
        )
    return contacts
