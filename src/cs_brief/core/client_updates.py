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

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from cs_brief.core.contacts import Contact, ContactInput
from cs_brief.core.importing import DateOrder, Table
from cs_brief.core.models import Account
from cs_brief.core.store import LocalStore

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


def load(store: LocalStore) -> ClientUpdatesData:
    """The saved data (empty when there is none)."""
    raise NotImplementedError


def set_form_link(store: LocalStore, link: str) -> str:
    """Save the pre-filled link, stripped. ValueError (plain) unless it is https (feeds.http
    check_url rules, but http is refused even for localhost), at most MAX_LINK characters, and
    contains CODE_PLACEHOLDER exactly once. "" clears it."""
    raise NotImplementedError


def opt_in(
    store: LocalStore, account_id: str, *, agreed_with: str, agreed_on: date, now: datetime
) -> Consent:
    """Turn updates on for an account with a new unique code. ValueError when agreed_with is
    blank, over 80 characters or has a line break, or agreed_on is in the future (after
    agreed_on's own today: compare with now.date()). Already on: ValueError."""
    raise NotImplementedError


def new_code(store: LocalStore, account_id: str, *, now: datetime) -> Consent:
    """Give an opted-in account a new code (the old link stops working). LookupError if off."""
    raise NotImplementedError


def opt_out(store: LocalStore, account_id: str) -> bool:
    """Turn updates off: delete the consent and the account's PENDING updates. True if it was on."""
    raise NotImplementedError


def update_link(data: ClientUpdatesData, account_id: str) -> str | None:
    """The account's own form link: form_link with CODE_PLACEHOLDER replaced by its code. None
    when the account isn't opted in or no form link is saved."""
    raise NotImplementedError


def normalize_code(text: str) -> str:
    """Upper-case with spaces and dashes removed, so "abcd-2345" matches "ABCD2345"."""
    raise NotImplementedError


# --- Reading the form's responses -------------------------------------------------------------


def suggested_mapping(table: Table) -> dict[str, str]:
    """Question key -> column header, by label or alias (casefolded, punctuation-insensitive)."""
    raise NotImplementedError


def parse_kind(text: str) -> UpdateKind:
    """The form's answer to "What changed?": the UpdateKind labels and words such as new, joined,
    added -> ADD; left, leaving, gone -> LEFT; change, update, moved, new email -> CHANGE;
    anything else -> OTHER."""
    raise NotImplementedError


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
    raise NotImplementedError


# --- Reviewing --------------------------------------------------------------------------------


def pending(store: LocalStore, accounts: dict[str, Account]) -> list[Review]:
    """PENDING updates whose account is in `accounts` (the Your data book by id), oldest first."""
    raise NotImplementedError


def pending_count(store: LocalStore, book_ids: set[str]) -> int:
    raise NotImplementedError


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
    raise NotImplementedError


def reject(store: LocalStore, update_id: str, *, now: datetime) -> None:
    """Mark it REJECTED and clear its text. LookupError for an unknown or decided update."""
    raise NotImplementedError


def forget(store: LocalStore, gone: Callable[[str], bool]) -> None:
    """Drop the consents and updates of every account id for which gone(id) is true
    (service.remove_connection and editing.delete_account call it, like contacts.forget)."""
    raise NotImplementedError


def move_account(store: LocalStore, from_id: str, to_id: str) -> None:
    """Linking: from_id's pending updates move to to_id; its consent moves too unless to_id
    already has one (then from_id's is dropped)."""
    raise NotImplementedError
