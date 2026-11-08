"""Client updates: customers who opted in tell you about changes (a new contact, someone who left)
through a form you made, and you accept or reject each change before anything is applied.

How it fits together (the setup that needs the least from the CSM and nothing from the client):
1. The CSM makes ONE form for all clients, in a tool they already have (Microsoft Forms with
   responses in an Excel file in OneDrive, an Airtable form, or Google Forms). It has the
   questions in QUESTIONS, one of which is the client code.
2. The CSM pastes the form's pre-filled link with CODE_PLACEHOLDER typed as the client code
   answer (settings.form_link). Each opted-in client then gets their own link with their code
   filled in (update_link): nothing to log in to, nothing to install.
3. The form's responses are read by an ordinary data feed with role FeedRole.CLIENT_UPDATES
   (a file, a link, Google Sheets or Airtable). Its sync calls ingest(), which turns new rows into
   PENDING updates. cs-brief never connects to the client and the client never reaches cs-brief.
4. The CSM accepts (accept) or rejects (reject) each one on the Client updates page. Only accept
   changes contacts (core/contacts.py), and the champion rule there adjusts the risk signal.

Opt-in is per account and off by default (Consent). A row is ignored, and none of its content is
kept, when: it has no code; the code matches no consent; the account is no longer in the book;
or the account already has MAX_PENDING_PER_ACCOUNT pending updates. Codes are compared in
constant time and are not secrets of the CSM's: a code only lets someone suggest a change that
the CSM still has to accept.

The file is store.root / store.CLIENT_UPDATES_FILE, written with config.write_atomic.

Privacy: a decided update keeps only its id, account, kind, status and decided_at (so a re-sync
doesn't bring it back); the client's text is deleted. Turning a client's updates off deletes its
consent and its pending updates. Clear local data deletes everything.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from cs_brief.core import contacts, importing
from cs_brief.core.config import write_atomic
from cs_brief.core.contacts import (
    MAX_EMAIL,
    MAX_NAME,
    MAX_TITLE,
    Contact,
    ContactInput,
    ContactSource,
    ContactStatus,
)
from cs_brief.core.feeds.http import FeedError, check_url
from cs_brief.core.importing import DateOrder, Table
from cs_brief.core.models import Account
from cs_brief.core.store import CLIENT_UPDATES_FILE, LocalStore

CODE_PLACEHOLDER = "CSBRIEFCODE"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LENGTH = 8
MAX_PENDING_PER_ACCOUNT = 20
MAX_NOTE = 1000
MAX_LINK = 2000


class UpdateKind(StrEnum):
    ADD = "add"
    LEFT = "left"
    CHANGE = "change"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "add": "New contact",
            "left": "Someone left",
            "change": "Change a contact's details",
            "other": "Something else",
        }[self.value]


class UpdateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Question(BaseModel):
    """One question the CSM's form must ask; also a field on the feed's matching page."""

    key: str  # mapping key, e.g. "code"
    label: str  # the question text to put in the form, and the matching page's field label
    help: str  # one sentence: what to put in the form for it
    required: bool  # the matching page requires a column for it
    aliases: list[str] = []  # other header spellings auto-matching accepts (casefolded)


# In this order on the setup page and the matching page.
QUESTIONS: list[Question] = [
    Question(
        key="code",
        label="Client code",
        help="Short answer. Your link fills it in for each client, so they don't type it.",
        required=True,
        aliases=["code", "client code", "update code"],
    ),
    Question(
        key="kind",
        label="What changed?",
        help="Choice: New contact, Someone left, Change a contact's details, Something else.",
        required=True,
        aliases=["what changed", "change", "type"],
    ),
    Question(
        key="name",
        label="Contact's name",
        help="Short answer.",
        required=True,
        aliases=["name", "contact name", "contact"],
    ),
    Question(
        key="title",
        label="Job title",
        help="Short answer, optional.",
        required=False,
        aliases=["title", "role", "job title"],
    ),
    Question(
        key="email",
        label="Email",
        help="Short answer, optional.",
        required=False,
        aliases=["email", "email address", "e-mail"],
    ),
    Question(
        key="champion",
        label="Is this our main contact?",
        help="Choice: Yes, No.",
        required=False,
        aliases=["main contact", "champion", "is this our main contact"],
    ),
    Question(
        key="note",
        label="Anything else?",
        help="Long answer, optional.",
        required=False,
        aliases=["note", "notes", "details", "anything else", "comments"],
    ),
    Question(
        key="submitted_by",
        label="Your name",
        help="Short answer: who at the client sent this.",
        required=False,
        aliases=["your name", "submitted by", "from", "respondent", "name of person"],
    ),
    Question(
        key="submitted_at",
        label="Timestamp",
        help="Added by the form tool itself; don't add it as a question.",
        required=False,
        aliases=["timestamp", "submitted at", "completion time", "start time", "created"],
    ),
]


class Consent(BaseModel):
    """A client that agreed to send updates."""

    account_id: str
    code: str  # CODE_LENGTH characters from CODE_ALPHABET, unique across consents
    agreed_with: str  # who at the client agreed, 1..80 characters
    agreed_on: date
    created_at: datetime  # naive UTC


class ClientUpdate(BaseModel):
    id: str  # 16 hex: sha256 of the feed's connection id and the row's mapped values, so a re-sync
    #          of the same response is recognised
    account_id: str
    kind: UpdateKind
    status: UpdateStatus = UpdateStatus.PENDING
    received_at: datetime  # naive UTC, when cs-brief first read it
    decided_at: datetime | None = None
    # The client's text: kept only while PENDING (cleared to defaults by accept/reject).
    name: str = ""
    title: str = ""
    email: str = ""
    champion: bool | None = None  # None when not answered
    note: str = ""
    submitted_by: str = ""
    submitted_at: datetime | None = None
    source_connection: str = ""  # the feed it came from


class UpdateSettings(BaseModel):
    form_link: str = ""  # the form's pre-filled link with CODE_PLACEHOLDER as the code answer


class ClientUpdatesData(BaseModel):
    """data/client_updates.json."""

    settings: UpdateSettings = Field(default_factory=UpdateSettings)
    consents: dict[str, Consent] = Field(default_factory=dict)  # account id -> consent
    updates: list[ClientUpdate] = Field(default_factory=list)  # newest last


class IngestResult(BaseModel):
    new: int = 0  # PENDING updates added
    already_seen: int = 0  # rows read before (pending or decided)
    ignored: int = 0  # rows dropped (no/unknown code, account gone, too many pending, unreadable)
    accounts: int = 0  # accounts that got a new update


class Review(BaseModel):
    """One pending update as the review page shows it."""

    update: ClientUpdate
    account: Account
    target: Contact | None  # LEFT/CHANGE: the contact it means (contacts.find_contact), if found
    candidates: list[Contact]  # the account's active contacts, for choosing when target is None


# --- Settings and consent ---------------------------------------------------------------------


def _naive(moment: datetime) -> datetime:
    """A datetime as naive UTC, the shape the store keeps. A naive value is taken as UTC."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _path(store: LocalStore) -> Path:
    return store.root / CLIENT_UPDATES_FILE


def load(store: LocalStore) -> ClientUpdatesData:
    """The saved data (empty when there is none)."""
    try:
        return ClientUpdatesData.model_validate_json(_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValidationError and JSON errors are ValueErrors
        return ClientUpdatesData()


def _save(store: LocalStore, data: ClientUpdatesData) -> None:
    write_atomic(_path(store), data.model_dump_json(indent=2) + "\n")


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def _new_code(taken: set[str]) -> str:
    """A code of CODE_LENGTH characters that no code in `taken` equals (constant-time compare)."""
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not any(hmac.compare_digest(code.encode(), other.encode()) for other in taken):
            return code


def set_form_link(store: LocalStore, link: str) -> str:
    """Save the pre-filled link, stripped. ValueError (plain) unless it is https (feeds.http
    check_url rules, but http is refused even for localhost), at most MAX_LINK characters, and
    contains CODE_PLACEHOLDER exactly once. "" clears it."""
    text = link.strip()
    if text:
        try:
            check_url(text)
        except FeedError as error:
            raise ValueError(error.message) from None
        if not text.casefold().startswith("https://"):
            raise ValueError("The form link must start with https://.")
        if text.count(CODE_PLACEHOLDER) != 1:
            raise ValueError(
                f"The form link needs {CODE_PLACEHOLDER} exactly once, where the client code goes."
            )
        if len(text) > MAX_LINK:
            raise ValueError(f"Keep the form link under {MAX_LINK} characters.")
    data = load(store)
    data.settings = data.settings.model_copy(update={"form_link": text})
    _save(store, data)
    return text


def opt_in(
    store: LocalStore, account_id: str, *, agreed_with: str, agreed_on: date, now: datetime
) -> Consent:
    """Turn updates on for an account with a new unique code. ValueError when agreed_with is
    blank, over 80 characters or has a line break, or agreed_on is in the future (after
    agreed_on's own today: compare with now.date()). Already on: ValueError."""
    name = agreed_with.strip()
    if not name:
        raise ValueError("Type the name of the person who agreed.")
    if len(name) > 80:
        raise ValueError("Keep the name of the person who agreed under 80 characters.")
    if _has_control(name):
        raise ValueError("The name of the person who agreed can't contain line breaks.")
    if agreed_on > now.date():
        raise ValueError("The date they agreed can't be in the future.")
    data = load(store)
    if account_id in data.consents:
        raise ValueError("Client updates are already on for this client.")
    code = _new_code({consent.code for consent in data.consents.values()})
    consent = Consent(
        account_id=account_id,
        code=code,
        agreed_with=name,
        agreed_on=agreed_on,
        created_at=_naive(now),
    )
    data.consents[account_id] = consent
    _save(store, data)
    return consent


def new_code(store: LocalStore, account_id: str, *, now: datetime) -> Consent:
    """Give an opted-in account a new code (the old link stops working). LookupError if off."""
    data = load(store)
    consent = data.consents.get(account_id)
    if consent is None:
        raise LookupError("Client updates are off for this client.")
    # Every code in use, the account's own old one included, so the new one always differs.
    code = _new_code({other.code for other in data.consents.values()})
    updated = consent.model_copy(update={"code": code})
    data.consents[account_id] = updated
    _save(store, data)
    return updated


def opt_out(store: LocalStore, account_id: str) -> bool:
    """Turn updates off: delete the consent and the account's PENDING updates. True if it was on."""
    data = load(store)
    was_on = data.consents.pop(account_id, None) is not None
    kept = [
        update
        for update in data.updates
        if not (update.account_id == account_id and update.status is UpdateStatus.PENDING)
    ]
    if was_on or len(kept) != len(data.updates):
        data.updates = kept
        _save(store, data)
    return was_on


def update_link(data: ClientUpdatesData, account_id: str) -> str | None:
    """The account's own form link: form_link with CODE_PLACEHOLDER replaced by its code. None
    when the account isn't opted in or no form link is saved."""
    consent = data.consents.get(account_id)
    if consent is None or not data.settings.form_link:
        return None
    return data.settings.form_link.replace(CODE_PLACEHOLDER, consent.code)


def normalize_code(text: str) -> str:
    """Upper-case with spaces and dashes removed, so "abcd-2345" matches "ABCD2345"."""
    return re.sub(r"[\s\-–—]", "", text).upper()


# --- Reading the form's responses -------------------------------------------------------------


_LINE_BREAKS = re.compile(r"[\r\n\t\v\f\u0085  ]+")
_CHANGE_WORDS = frozenset(
    {"change", "changed", "changes", "changing", "update", "updated", "updates", "updating"}
    | {"moved", "move"}
)
_CHANGE_PHRASES = ("new email", "new phone", "new title", "new role", "new address")
_LEFT_WORDS = frozenset(
    {"left", "leaving", "leave", "gone", "departed", "departure", "quit", "resigned", "retired"}
)
_ADD_WORDS = frozenset({"new", "joined", "join", "joining", "added", "add", "onboarded"})


def _columns(table: Table, mapping: dict[str, str]) -> dict[str, int]:
    """Question key -> column index for the mapping's headers this table has. Raises ValueError
    naming the required questions that have no column."""
    missing = [
        question.label
        for question in QUESTIONS
        if question.required and mapping.get(question.key) not in table.headers
    ]
    if missing:
        joined = missing[0] if len(missing) == 1 else f"{', '.join(missing[:-1])} and {missing[-1]}"
        raise ValueError(f"Match {joined} to bring in client updates.")
    columns: dict[str, int] = {}
    for question in QUESTIONS:
        header = mapping.get(question.key)
        if header and header in table.headers:
            columns[question.key] = table.headers.index(header)
    return columns


def suggested_mapping(table: Table) -> dict[str, str]:
    """Question key -> column header, by label or alias (casefolded, punctuation-insensitive)."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for question in QUESTIONS:
        for wanted in (question.label, *question.aliases):
            text = importing._normalise_header(wanted)
            header = next(
                (
                    candidate
                    for candidate in table.headers
                    if candidate not in taken and importing._normalise_header(candidate) == text
                ),
                None,
            )
            if header is not None:
                mapping[question.key] = header
                taken.add(header)
                break
    return mapping


def parse_kind(text: str) -> UpdateKind:
    """The form's answer to "What changed?": the UpdateKind labels and words such as new, joined,
    added -> ADD; left, leaving, gone -> LEFT; change, update, moved, new email -> CHANGE;
    anything else -> OTHER."""
    words = importing._normalise_header(text)
    for kind in UpdateKind:
        if words == importing._normalise_header(kind.label):
            return kind
    tokens = set(words.split())
    if tokens & _CHANGE_WORDS or any(phrase in words for phrase in _CHANGE_PHRASES):
        return UpdateKind.CHANGE  # before ADD, because "new email" is a change
    if tokens & _LEFT_WORDS:
        return UpdateKind.LEFT
    if tokens & _ADD_WORDS:
        return UpdateKind.ADD
    return UpdateKind.OTHER


def _clean_text(text: str, limit: int) -> str:
    """Client text as kept: line breaks and tabs become spaces, other control characters are
    removed, runs of spaces collapse, and it is cut to limit characters."""
    spaced = _LINE_BREAKS.sub(" ", text)
    kept = "".join(char for char in spaced if unicodedata.category(char) != "Cc")
    return " ".join(kept.split())[:limit].rstrip()


def _clean_email(text: str) -> str:
    """The cleaned email when contacts.validate accepts it, else "" (the row is still kept)."""
    email = _clean_text(text, MAX_EMAIL)
    try:
        return contacts.validate(ContactInput(name="Contact", email=email)).email
    except ValueError:
        return ""


def _champion(text: str) -> bool | None:
    """The yes/no answer, or None when it is blank or not yes or no."""
    if not text:
        return None
    try:
        return importing.parse_bool(text)
    except ValueError:
        return None


def _submitted(text: str, date_order: DateOrder) -> datetime | None:
    if not text:
        return None
    try:
        day = importing.parse_date(text, date_order)
    except ValueError:
        return None
    return datetime.combine(day, time())


def _cell(row: list[str], columns: dict[str, int], key: str) -> str:
    index = columns.get(key)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _update_id(connection_id: str, row: list[str], columns: dict[str, int]) -> str:
    """16 hex characters: the feed's connection id and the row's mapped values, in QUESTIONS
    order."""
    parts = [connection_id, *(_cell(row, columns, q.key) for q in QUESTIONS if q.key in columns)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _consent_for(data: ClientUpdatesData, code: str) -> Consent | None:
    """The consent whose code equals this normalised code, compared in constant time."""
    if not code:
        return None
    wanted = code.encode("utf-8")
    found: Consent | None = None
    for consent in data.consents.values():
        if hmac.compare_digest(wanted, consent.code.encode("utf-8")):
            found = consent
    return found


def ingest(
    store: LocalStore,
    table: Table,
    mapping: dict[str, str],
    *,
    connection_id: str,
    book_ids: set[str],
    date_order: DateOrder,
    now: datetime,
    save: bool = True,
) -> IngestResult:
    """Turn the form's rows into PENDING updates (see the module docstring for what is ignored).
    Text is stripped, control characters removed, and cut to the contact limits (MAX_NOTE for
    note). A yes/no champion answer uses importing's yes/no words. submitted_at is parsed with
    date_order (None when it can't be). Raises ValueError when mapping lacks a required question.
    save=False counts what would happen without writing anything (the matching page's preview)."""
    columns = _columns(table, mapping)
    data = load(store)
    stamp = _naive(now)
    known = {update.id for update in data.updates}
    pending_per_account: dict[str, int] = {}
    for update in data.updates:
        if update.status is UpdateStatus.PENDING:
            pending_per_account[update.account_id] = (
                pending_per_account.get(update.account_id, 0) + 1
            )
    result = IngestResult()
    added: list[ClientUpdate] = []
    touched: set[str] = set()
    for row in table.rows:
        consent = _consent_for(data, normalize_code(_cell(row, columns, "code")))
        if consent is None or consent.account_id not in book_ids:
            result.ignored += 1
            continue
        update_id = _update_id(connection_id, row, columns)
        if update_id in known:
            result.already_seen += 1
            continue
        account_id = consent.account_id
        if pending_per_account.get(account_id, 0) >= MAX_PENDING_PER_ACCOUNT:
            result.ignored += 1
            continue
        kind_text = _cell(row, columns, "kind")
        update = ClientUpdate(
            id=update_id,
            account_id=account_id,
            kind=parse_kind(kind_text) if kind_text else UpdateKind.OTHER,
            status=UpdateStatus.PENDING,
            received_at=stamp,
            name=_clean_text(_cell(row, columns, "name"), MAX_NAME),
            title=_clean_text(_cell(row, columns, "title"), MAX_TITLE),
            email=_clean_email(_cell(row, columns, "email")),
            champion=_champion(_cell(row, columns, "champion")),
            note=_clean_text(_cell(row, columns, "note"), MAX_NOTE),
            submitted_by=_clean_text(_cell(row, columns, "submitted_by"), MAX_NAME),
            submitted_at=_submitted(_cell(row, columns, "submitted_at"), date_order),
            source_connection=connection_id,
        )
        added.append(update)
        known.add(update_id)
        pending_per_account[account_id] = pending_per_account.get(account_id, 0) + 1
        touched.add(account_id)
        result.new += 1
    result.accounts = len(touched)
    if save and added:
        data.updates.extend(added)
        _save(store, data)
    return result


# --- Reviewing --------------------------------------------------------------------------------


def _find_pending(data: ClientUpdatesData, update_id: str) -> ClientUpdate:
    for update in data.updates:
        if update.id == update_id:
            if update.status is UpdateStatus.PENDING:
                return update
            break
    raise LookupError("That update was already decided, or it isn't there.")


def _decided(update: ClientUpdate, status: UpdateStatus, stamp: datetime) -> ClientUpdate:
    """The update as it is kept once decided: id, account, kind, status and times only. Every
    text field goes back to its default."""
    return ClientUpdate(
        id=update.id,
        account_id=update.account_id,
        kind=update.kind,
        status=status,
        received_at=update.received_at,
        decided_at=stamp,
    )


def _replace(data: ClientUpdatesData, decided: ClientUpdate) -> None:
    data.updates = [decided if update.id == decided.id else update for update in data.updates]


def _active(current: list[Contact]) -> list[Contact]:
    return [contact for contact in current if contact.status is ContactStatus.ACTIVE]


def _chosen(current: list[Contact], update: ClientUpdate, contact_id: str | None) -> Contact:
    """The contact a LEFT or CHANGE applies to: the one the CSM chose, else the one the update
    means. ValueError (plain) when there is neither."""
    if contact_id is not None:
        chosen = next((contact for contact in current if contact.id == contact_id), None)
        if chosen is None:
            raise ValueError("That contact isn't in this account.")
        return chosen
    target = contacts.find_contact(_active(current), email=update.email, name=update.name)
    if target is None:
        raise ValueError("Choose the contact this update is about.")
    return target


def _own_values(update: ClientUpdate) -> ContactInput:
    return ContactInput(
        name=update.name,
        title=update.title,
        email=update.email,
        champion=bool(update.champion),
    )


def _changed_values(update: ClientUpdate, target: Contact) -> ContactInput:
    """A CHANGE's own values: a blank field keeps the contact's current value, and an unanswered
    champion question keeps the current flag."""
    return ContactInput(
        name=update.name or target.name,
        title=update.title or target.title,
        email=update.email or target.email,
        champion=target.champion if update.champion is None else update.champion,
    )


def pending(store: LocalStore, accounts: dict[str, Account]) -> list[Review]:
    """PENDING updates whose account is in `accounts` (the Your data book by id), oldest first."""
    data = load(store)
    waiting = sorted(
        (
            update
            for update in data.updates
            if update.status is UpdateStatus.PENDING and update.account_id in accounts
        ),
        key=lambda update: update.received_at,
    )
    cache: dict[str, list[Contact]] = {}
    reviews: list[Review] = []
    for update in waiting:
        if update.account_id not in cache:
            cache[update.account_id] = _active(contacts.contacts_for(store, update.account_id))
        active = cache[update.account_id]
        target = None
        if update.kind in (UpdateKind.LEFT, UpdateKind.CHANGE):
            target = contacts.find_contact(active, email=update.email, name=update.name)
        reviews.append(
            Review(
                update=update,
                account=accounts[update.account_id],
                target=target,
                candidates=active,
            )
        )
    return reviews


def pending_count(store: LocalStore, book_ids: set[str]) -> int:
    return sum(
        1
        for update in load(store).updates
        if update.status is UpdateStatus.PENDING and update.account_id in book_ids
    )


def accept(
    store: LocalStore,
    update_id: str,
    *,
    values: ContactInput | None,
    contact_id: str | None,
    left_on: date | None,
    now: datetime,
) -> UpdateKind:
    """Apply a PENDING update, then mark it ACCEPTED and clear its text. `values` are the fields as
    the CSM confirmed them on the review page (they may have corrected the client's text); None
    means the update's own values. ADD: contacts.add_contact. LEFT: contacts.mark_left on the
    contact chosen (contact_id, else the found target) dated left_on, else submitted_at's date,
    else now's date. CHANGE: contacts.update_contact on the chosen contact. OTHER: nothing is
    applied (the CSM has read it). Source is ContactSource.CLIENT. LookupError for an unknown or
    decided update; ValueError (plain) when LEFT/CHANGE has no contact to apply to, or values
    don't validate. Nothing is changed when it raises."""
    data = load(store)
    update = _find_pending(data, update_id)
    stamp = _naive(now)
    kind = update.kind
    if kind is UpdateKind.ADD:
        typed = values if values is not None else _own_values(update)
        contacts.add_contact(
            store, update.account_id, typed, source=ContactSource.CLIENT, now=stamp
        )
    elif kind is UpdateKind.LEFT:
        target = _chosen(contacts.contacts_for(store, update.account_id), update, contact_id)
        if left_on is not None:
            when = left_on
        elif update.submitted_at is not None:
            when = update.submitted_at.date()
        else:
            when = stamp.date()
        contacts.mark_left(
            store,
            update.account_id,
            target.id,
            left_on=when,
            source=ContactSource.CLIENT,
            now=stamp,
        )
    elif kind is UpdateKind.CHANGE:
        target = _chosen(contacts.contacts_for(store, update.account_id), update, contact_id)
        typed = values if values is not None else _changed_values(update, target)
        contacts.update_contact(
            store,
            update.account_id,
            target.id,
            typed,
            source=ContactSource.CLIENT,
            now=stamp,
        )
    # OTHER applies nothing: the CSM has read it.
    data = load(store)
    _replace(data, _decided(update, UpdateStatus.ACCEPTED, stamp))
    _save(store, data)
    return kind


def reject(store: LocalStore, update_id: str, *, now: datetime) -> None:
    """Mark it REJECTED and clear its text. LookupError for an unknown or decided update."""
    data = load(store)
    update = _find_pending(data, update_id)
    _replace(data, _decided(update, UpdateStatus.REJECTED, _naive(now)))
    _save(store, data)


def forget(store: LocalStore, gone: Callable[[str], bool]) -> None:
    """Drop the consents and updates of every account id for which gone(id) is true
    (service.remove_connection and editing.delete_account call it, like contacts.forget)."""
    data = load(store)
    consents = {account: consent for account, consent in data.consents.items() if not gone(account)}
    updates = [update for update in data.updates if not gone(update.account_id)]
    if len(consents) != len(data.consents) or len(updates) != len(data.updates):
        data.consents = consents
        data.updates = updates
        _save(store, data)


def move_account(store: LocalStore, from_id: str, to_id: str) -> None:
    """Linking: from_id's pending updates move to to_id; its consent moves too unless to_id
    already has one (then from_id's is dropped)."""
    if from_id == to_id:
        return
    data = load(store)
    changed = False
    updates: list[ClientUpdate] = []
    for update in data.updates:
        if update.status is UpdateStatus.PENDING and update.account_id == from_id:
            update = update.model_copy(update={"account_id": to_id})
            changed = True
        updates.append(update)
    consent = data.consents.pop(from_id, None)
    if consent is not None:
        changed = True
        if to_id not in data.consents:
            data.consents[to_id] = consent.model_copy(update={"account_id": to_id})
    if changed:
        data.updates = updates
        _save(store, data)
