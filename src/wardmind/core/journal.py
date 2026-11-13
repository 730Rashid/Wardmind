"""The CSM's own record of each account: notes and touches, follow-ups, snoozes and outcomes.

Wardmind ranks accounts and says what to do; the journal is where the CSM records what they did:
- **Notes and touches.** A dated note, optionally a touch (a call, a meeting or an email with
  the customer). The latest touch gives the account's "last contacted" date.
- **Follow-ups.** "Do this by <date>": open until marked done. The Today page lists the ones due.
- **Snooze.** "I've dealt with this, leave it off Today until <date>." A snooze ends by itself
  on that day, or earlier when the account's health gets worse than it was when snoozed.
- **Outcomes.** How a renewal went (renewed, expanded, contracted, churned), with what Wardmind
  thought of the account at that moment (risk, health, the latest signal values). These are the
  labels the planned "scoring that adapts itself" will learn from, so they are kept as recorded.

Storage: one JSON file under store.root, written with config.write_atomic under store.WRITE_LOCK:
store.JOURNAL_FILE for Your data and store.DEMO_JOURNAL_FILE for demo data, so notes typed while
trying the demo never mix with real accounts. Both are in store._ALL_FILES (Delete all local data
removes them). The journal is the CSM's own words about customers, so it stays on this computer;
the MCP server shares dates, counts, follow-ups and outcomes but never note text.

Account ids: entries belong to the id the account page shows. Linking accounts moves a member's
entries to the primary (move_account); removing a connection or deleting a hand-added account
forgets them (forget). An entry for an account that is no longer in the book is not shown.

Times: datetimes are naive UTC (the store's shape). Dates (happened_on, due, until, renewal_date,
decided_on) are the CSM's local calendar days; callers pass `today` (date.today()) explicitly so
tests can pin it.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them. Bodies marked NotImplementedError are to be written.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, Field

from wardmind.core import vault
from wardmind.core.models import Account, Health, Motion, ScoredAccount, SignalKey
from wardmind.core.store import DEMO_JOURNAL_FILE, JOURNAL_FILE, WRITE_LOCK, LocalStore

MAX_NOTE = 4000  # characters in one note
MAX_FOLLOW_UP = 200  # characters in a follow-up's text (one line)
MAX_OUTCOME_NOTE = 1000
MAX_NOTES_PER_ACCOUNT = 1000  # your own notes and activities (mailbox entries not counted)
MAX_MAILBOX_ENTRIES = 500  # emails logged by the mailbox sync; the oldest unpinned go first
MAILBOX_DAYS_AHEAD = 1  # a logged email's date can be at most this far after today
MAX_SUBJECT = 300  # characters in an activity's subject (one line)
MAX_PEOPLE = 20  # client-side email addresses kept on one logged email
MAX_TEAM = 12  # team members per account
MAX_TEAM_NAME = 100
MAX_PINNED = 5  # pinned entries per account (activity and notes together)
REPLY_WAIT_DAYS = 5  # a sent email with no reply after this many days is "awaiting their reply"
ANSWER_WAIT_DAYS = 2  # a received email not answered after this many days is "awaiting yours"
QUIET_THREAD_DAYS = 60  # a thread whose latest email is older than this has gone quiet
MAX_OPEN_FOLLOW_UPS = 20  # open follow-ups per account
MAX_DISMISSED_RULES = 200  # removed automatic follow-ups remembered per account
MAX_SNOOZE_DAYS = 90
SNOOZE_CHOICES = (3, 7, 14, 30)  # the snooze buttons, in days
FOLLOW_UP_DEFAULT_DAYS = 7  # a new follow-up is due this many days from today unless chosen
MAX_FOLLOW_UP_DAYS = 366  # a follow-up can't be due further out than this
QUIET_DAYS = 14  # Today's "Needs attention" skips accounts contacted within this many days
ATTENTION_LIMIT = 10  # at most this many accounts in "Needs attention"
UPCOMING_DAYS = 7  # Today's "Coming up" covers follow-ups due in the next 7 days
OUTCOME_WINDOW_DAYS = 60  # a renewal that passed up to 60 days ago asks for its outcome
OUTCOME_MATCH_DAYS = 45  # an outcome whose renewal_date is within 45 days of it answers it

# Health from worst to best, for "health got worse than when snoozed".
HEALTH_ORDER: dict[Health, int] = {Health.AT_RISK: 0, Health.WATCH: 1, Health.HEALTHY: 2}


class TouchKind(StrEnum):
    """NOTE is a plain note (the Notes box). Every other kind is an activity with the customer
    (the Activity history box) and counts as contact."""

    NOTE = "note"  # a plain note: not a contact with the customer
    CALL = "call"
    MEETING = "meeting"
    EMAIL = "email"
    OTHER = "other"  # any other touch: a message on LinkedIn or Slack, a site visit, a demo

    @property
    def label(self) -> str:
        return {
            "note": "Note",
            "call": "Call",
            "meeting": "Meeting",
            "email": "Email",
            "other": "Other",
        }[self.value]

    @property
    def is_contact(self) -> bool:
        """A call, meeting or email counts as contact with the customer ("last contacted")."""
        return self is not TouchKind.NOTE


class NoteSource(StrEnum):
    MANUAL = "manual"  # typed on the account page (or Today)
    MAILBOX = "mailbox"  # logged by the mailbox sync (core/mailbox.py)
    AGENT = "agent"  # added by Claude through the MCP server (mcp/writes.py): "Added by Claude"
    SUGGESTION = "suggestion"  # an accepted suggestion (core/proposals.py)


class Direction(StrEnum):
    """For an email: SENT by us (you or a team member) or RECEIVED from the client."""

    SENT = "sent"
    RECEIVED = "received"

    @property
    def label(self) -> str:
        return {"sent": "Sent", "received": "Received"}[self.value]


class Note(BaseModel):
    """One entry: a plain note (kind NOTE) or an activity (any other kind). Every field after
    `edited_at` was added with the Activity history; older files load with the defaults."""

    id: str  # "n-" + 8 hex, unique within the account
    kind: TouchKind = TouchKind.NOTE
    text: str  # 1..MAX_NOTE characters; line breaks allowed, no other control/format characters
    happened_on: date  # the day of the call, meeting or email (or of the note); never after today
    created_at: datetime
    edited_at: datetime | None = None
    pinned: bool = False  # pinned entries are listed first, in their own group
    subject: str = ""  # 0..MAX_SUBJECT characters, one line (an email's subject, a call's topic)
    by: str = ""  # who did it: a team member's or contact's name; "" means you
    direction: Direction | None = None  # emails only; None for every other kind
    source: NoteSource = NoteSource.MANUAL
    message_id: str | None = None  # MAILBOX only: the email's Message-ID, so it's logged once
    people: list[str] = Field(default_factory=list)  # MAILBOX only: client-side addresses
    thread: str = ""  # MAILBOX only: thread_key(subject), to group replies into one thread


class FollowUpStatus(StrEnum):
    OPEN = "open"
    DONE = "done"


class FollowUp(BaseModel):
    id: str  # "f-" + 8 hex, unique within the account
    text: str  # 1..MAX_FOLLOW_UP characters, one line
    due: date
    status: FollowUpStatus = FollowUpStatus.OPEN
    created_at: datetime
    done_at: datetime | None = None  # set while DONE
    # The occasion key of the automatic rule (core/rules.py) that added this follow-up, e.g.
    # "at_risk:2026-11-02", "renewal:2027-01-14" or "champion_left:2026-10-30". None for one the
    # CSM added. A rule adds at most one follow-up per key on an account, open or done.
    rule: str | None = None


class Snooze(BaseModel):
    until: date  # the account comes back on this day
    health: Health  # its health when snoozed: a worse health ends the snooze early
    created_at: datetime


class OutcomeKind(StrEnum):
    RENEWED = "renewed"  # renewed at about the same ARR
    EXPANDED = "expanded"  # renewed for more
    CONTRACTED = "contracted"  # renewed for less
    CHURNED = "churned"  # did not renew

    @property
    def label(self) -> str:
        return {
            "renewed": "Renewed",
            "expanded": "Expanded",
            "contracted": "Contracted",
            "churned": "Churned",
        }[self.value]


class OutcomeInput(BaseModel):
    """What the outcome form supplies (strings already parsed by the web layer)."""

    kind: OutcomeKind
    renewal_date: date  # the renewal it answers; the form defaults it to the account's
    decided_on: date  # when the customer decided; never after today
    arr_after: float | None = Field(default=None, ge=0)  # new ARR; ignored (0) for CHURNED
    note: str = ""  # 0..MAX_OUTCOME_NOTE characters, line breaks allowed


class Outcome(BaseModel):
    id: str  # "o-" + 8 hex
    kind: OutcomeKind
    renewal_date: date
    decided_on: date
    arr_before: float = Field(ge=0)  # the account's ARR when the outcome was recorded
    arr_after: float | None = Field(default=None, ge=0)  # 0.0 for CHURNED; None if not given
    note: str = ""
    # What Wardmind thought when the outcome was recorded (for learning weights later).
    risk: float
    priority: float
    health: Health
    motion: Motion
    # Latest value of each signal: the built in, and the book's own (custom:<id> keys).
    signals: dict[SignalKey, float] = Field(default_factory=dict)
    recorded_at: datetime


class CallKind(StrEnum):
    """Your own forecast for a renewal (the honest forecast)."""

    COMMIT = "commit"  # you would bet on it renewing
    LIKELY = "likely"  # probably renews, not certain
    AT_RISK = "at_risk"  # you think it may not renew

    @property
    def label(self) -> str:
        return {"commit": "Commit", "likely": "Likely", "at_risk": "At risk"}[self.value]


class ForecastCall(BaseModel):
    """Your call on one renewal. One call per renewal_date; a new call replaces the old one."""

    id: str  # "c-" + 8 hex
    kind: CallKind
    renewal_date: date  # the renewal the call is about (the account's renewal date then)
    made_on: date  # the day the call was made (today when it was recorded)
    # What Wardmind thought when the call was made, so the two can be compared later.
    health: Health
    risk: float
    arr: float = Field(ge=0)
    recorded_at: datetime


class TeamRole(StrEnum):
    """Who on your side works the account."""

    CSM = "csm"
    ACCOUNT_MANAGER = "account_manager"
    RENEWAL_MANAGER = "renewal_manager"
    IMPLEMENTATION_MANAGER = "implementation_manager"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "csm": "Customer success manager",
            "account_manager": "Account manager",
            "renewal_manager": "Renewal manager",
            "implementation_manager": "Implementation manager",
            "other": "Other",
        }[self.value]

    @property
    def short(self) -> str:
        """For tags: "CSM", "AM", "Renewals", "Implementation", "Team"."""
        return {
            "csm": "CSM",
            "account_manager": "AM",
            "renewal_manager": "Renewals",
            "implementation_manager": "Implementation",
            "other": "Team",
        }[self.value]


class TeamMember(BaseModel):
    id: str  # "t-" + 8 hex, unique within the account
    name: str  # 1..MAX_TEAM_NAME characters, one line
    email: str | None = None  # lower-cased; how the mailbox sync knows an email came from them
    role: TeamRole
    created_at: datetime


class LoggedEmail(BaseModel):
    """What the mailbox sync hands to Journal.log_email for one matched email."""

    message_id: str  # the Message-ID header, stripped of < >; never empty
    subject: str  # as sent (decoded); may be empty
    sent_on: date  # the Date header as a local calendar day
    direction: Direction
    by: str  # the sender's name (a team member, a contact or the display name), "" for you
    people: list[str]  # the client-side addresses, lower-cased
    snippet: str  # the start of the plain-text body; may be empty


class ThreadStatus(BaseModel):
    """An email thread that is waiting on someone (see awaiting_replies)."""

    thread: str  # thread_key of the subject
    subject: str  # the latest email's subject as written
    last_on: date  # the latest email's day
    waiting_on_them: bool  # True: we sent last; False: they sent last and we haven't answered
    days: int  # today - last_on


class AccountJournal(BaseModel):
    notes: list[Note] = Field(default_factory=list)  # in the order added
    follow_ups: list[FollowUp] = Field(default_factory=list)  # in the order added
    snooze: Snooze | None = None
    outcomes: list[Outcome] = Field(default_factory=list)  # in the order recorded
    team: list[TeamMember] = Field(default_factory=list)  # in the order added
    calls: list[ForecastCall] = Field(default_factory=list)  # in the order made
    # Occasion keys (FollowUp.rule) of automatic follow-ups the CSM removed, so a rule never adds
    # them back. The newest MAX_DISMISSED_RULES are kept.
    dismissed_rules: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.notes
            or self.follow_ups
            or self.snooze
            or self.outcomes
            or self.team
            or self.calls
            or self.dismissed_rules
        )


class JournalData(BaseModel):
    """The journal file: account id -> its journal. Empty journals are not written."""

    by_account: dict[str, AccountJournal] = Field(default_factory=dict)


# --- Validation (plain messages, shown next to the field) ---------------------------------


def clean_note_text(text: str) -> str:
    """The note stripped of leading/trailing whitespace, with \\r\\n and \\r turned into \\n;
    ValueError("Type the note.") when empty, "Keep the note under 4,000 characters." when long,
    "A note can't contain control characters." for any Cc character other than \\n and \\t,
    "A note can't contain hidden formatting characters." for any Cf character."""
    cleaned = _unify_breaks(text).strip()
    if not cleaned:
        raise ValueError("Type the note.")
    if len(cleaned) > MAX_NOTE:
        raise ValueError(f"Keep the note under {MAX_NOTE:,} characters.")
    _check_characters(cleaned, "note", allowed="\n\t")
    return cleaned


def clean_follow_up_text(text: str) -> str:
    """One line of plain text, stripped. ValueError("Say what to follow up on.") when empty,
    "Keep it under 200 characters.", "A follow-up can't contain a line break.", and the same
    control/format messages as notes ("A follow-up can't contain ...")."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Say what to follow up on.")
    if len(cleaned) > MAX_FOLLOW_UP:
        raise ValueError(f"Keep it under {MAX_FOLLOW_UP} characters.")
    if any(char in cleaned for char in _LINE_BREAKS):
        raise ValueError("A follow-up can't contain a line break.")
    _check_characters(cleaned, "follow-up", allowed="\t")
    return cleaned


def clean_outcome_note(text: str) -> str:
    """Like clean_note_text, but "" is allowed and the limit is MAX_OUTCOME_NOTE ("Keep the note
    under 1,000 characters.")."""
    cleaned = _unify_breaks(text).strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_OUTCOME_NOTE:
        raise ValueError(f"Keep the note under {MAX_OUTCOME_NOTE:,} characters.")
    _check_characters(cleaned, "note", allowed="\n\t")
    return cleaned


def check_happened_on(day: date, today: date) -> date:
    """day, or ValueError("That date is in the future.") when day > today."""
    if day > today:
        raise ValueError("That date is in the future.")
    return day


def check_due(day: date, today: date) -> date:
    """day, or ValueError("Pick today or a later date.") when day < today, or
    ValueError("Pick a date within a year.") when day > today + MAX_FOLLOW_UP_DAYS."""
    if day < today:
        raise ValueError("Pick today or a later date.")
    if day > today + timedelta(days=MAX_FOLLOW_UP_DAYS):
        raise ValueError("Pick a date within a year.")
    return day


def check_snooze_until(day: date, today: date) -> date:
    """day, or ValueError("Pick a date after today.") when day <= today, or
    ValueError("Snooze for 90 days at most.") when day > today + MAX_SNOOZE_DAYS."""
    if day <= today:
        raise ValueError("Pick a date after today.")
    if day > today + timedelta(days=MAX_SNOOZE_DAYS):
        raise ValueError(f"Snooze for {MAX_SNOOZE_DAYS} days at most.")
    return day


# --- Reading one account's journal (pure) -------------------------------------------------


def last_contact(journal: AccountJournal) -> date | None:
    """The latest happened_on of a note whose kind is_contact, or None."""
    return max((note.happened_on for note in journal.notes if note.kind.is_contact), default=None)


def last_touch(journal: AccountJournal) -> Note | None:
    """The contact note with the latest happened_on (ties: the latest created_at), or None."""
    contacts = [note for note in journal.notes if note.kind.is_contact]
    if not contacts:
        return None
    return max(contacts, key=lambda note: (note.happened_on, note.created_at))


def notes_newest_first(journal: AccountJournal) -> list[Note]:
    """Notes by happened_on descending, then created_at descending."""
    return sorted(journal.notes, key=lambda note: (note.happened_on, note.created_at), reverse=True)


def activity_newest_first(journal: AccountJournal) -> list[Note]:
    """Every entry whose kind is not NOTE, newest first: happened_on desc, then created_at desc,
    then id. Pinned ones are included here too (the page shows them in their own group)."""
    return _by_recency([note for note in journal.notes if note.kind is not TouchKind.NOTE])


def plain_notes_newest_first(journal: AccountJournal) -> list[Note]:
    """Every NOTE entry, newest first, ordered as activity_newest_first."""
    return _by_recency([note for note in journal.notes if note.kind is TouchKind.NOTE])


def pinned_entries(journal: AccountJournal) -> list[Note]:
    """Pinned entries of any kind, newest first, ordered as activity_newest_first."""
    return _by_recency([note for note in journal.notes if note.pinned])


def thread_key(subject: str) -> str:
    """The subject without reply and forward prefixes, case-folded and with runs of whitespace
    made one space: "RE: Fwd:  Renewal plan" -> "renewal plan". Prefixes are removed repeatedly,
    in any case, with or without a space after the colon: re, fw, fwd, aw, sv, wg (common in
    other languages), and "re[2]:" style counters. An empty or prefix-only subject gives ""."""
    text = " ".join(subject.split())
    prefix = _THREAD_PREFIX.match(text)
    while prefix is not None:
        text = text[prefix.end() :]
        prefix = _THREAD_PREFIX.match(text)
    return text.casefold()


def awaiting_replies(
    journal: AccountJournal,
    today: date,
    *,
    reply_wait_days: int = REPLY_WAIT_DAYS,
    answer_wait_days: int = ANSWER_WAIT_DAYS,
) -> list[ThreadStatus]:
    """Email threads waiting on someone, from MAILBOX emails with a non-empty thread. For each
    thread take its latest email (happened_on, then created_at). Waiting on them when it was SENT
    and today - happened_on >= reply_wait_days; waiting on you when it was RECEIVED and
    today - happened_on >= answer_wait_days (the Settings values; the defaults are
    REPLY_WAIT_DAYS and ANSWER_WAIT_DAYS). Threads whose latest email is older than
    QUIET_THREAD_DAYS are left out (they've gone quiet). Waiting on you first, then oldest first,
    then by thread."""
    latest: dict[str, Note] = {}
    for note in journal.notes:
        if note.source is not NoteSource.MAILBOX or not note.thread or note.direction is None:
            continue
        current = latest.get(note.thread)
        newer = (note.happened_on, note.created_at)
        if current is None or newer > (current.happened_on, current.created_at):
            latest[note.thread] = note
    found: list[ThreadStatus] = []
    for thread, note in latest.items():
        days = (today - note.happened_on).days
        if days > QUIET_THREAD_DAYS:
            continue
        waiting_on_them = note.direction is Direction.SENT
        if days < (reply_wait_days if waiting_on_them else answer_wait_days):
            continue
        found.append(
            ThreadStatus(
                thread=thread,
                subject=note.subject,
                last_on=note.happened_on,
                waiting_on_them=waiting_on_them,
                days=days,
            )
        )
    found.sort(key=lambda status: (status.waiting_on_them, status.last_on, status.thread))
    return found


def clean_subject(text: str) -> str:
    """One line, stripped. "" is allowed. ValueError("Keep the subject under 300 characters."),
    "A subject can't contain a line break." and the same control/format messages as notes
    ("A subject can't contain ...")."""
    cleaned = text.strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_SUBJECT:
        raise ValueError(f"Keep the subject under {MAX_SUBJECT} characters.")
    if any(char in cleaned for char in _LINE_BREAKS):
        raise ValueError("A subject can't contain a line break.")
    _check_characters(cleaned, "subject", allowed="\t")
    return cleaned


def clean_team_name(text: str) -> str:
    """One line, stripped. ValueError("Type their name."), "Keep the name under 100
    characters.", "A name can't contain a line break." and the control/format messages
    ("A name can't contain ...")."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Type their name.")
    if len(cleaned) > MAX_TEAM_NAME:
        raise ValueError(f"Keep the name under {MAX_TEAM_NAME} characters.")
    if any(char in cleaned for char in _LINE_BREAKS):
        raise ValueError("A name can't contain a line break.")
    _check_characters(cleaned, "name", allowed="\t")
    return cleaned


def clean_team_email(text: str) -> str | None:
    """None for blank. Otherwise stripped and lower-cased; ValueError("Type an email address like
    name@company.com.") unless it has exactly one @, a non-empty local part, a dot in the domain
    (not first or last), no whitespace or control characters, and at most 254 characters."""
    cleaned = text.strip().lower()
    if not cleaned:
        return None
    if cleaned.count("@") != 1:
        raise ValueError(_BAD_EMAIL)
    local, domain = cleaned.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError(_BAD_EMAIL)
    if len(cleaned) > _MAX_EMAIL or any(
        char.isspace() or unicodedata.category(char) == "Cc" for char in cleaned
    ):
        raise ValueError(_BAD_EMAIL)
    return cleaned


def open_follow_ups(journal: AccountJournal) -> list[FollowUp]:
    """OPEN follow-ups by due ascending, then created_at ascending."""
    found = [item for item in journal.follow_ups if item.status is FollowUpStatus.OPEN]
    return sorted(found, key=lambda item: (item.due, item.created_at))


def recently_done(journal: AccountJournal, now: datetime, days: int = 14) -> list[FollowUp]:
    """DONE follow-ups whose done_at is within `days` days before now, newest first."""
    cutoff = _naive(now) - timedelta(days=days)
    found = [
        item
        for item in journal.follow_ups
        if item.status is FollowUpStatus.DONE
        and item.done_at is not None
        and item.done_at >= cutoff
    ]
    return sorted(found, key=lambda item: item.done_at or cutoff, reverse=True)


def active_snooze(journal: AccountJournal, health: Health, today: date) -> Snooze | None:
    """The snooze while it applies: None when there is none, when until <= today, or when
    `health` (the account's health now) is worse than snooze.health (HEALTH_ORDER)."""
    snooze = journal.snooze
    if snooze is None or snooze.until <= today:
        return None
    if HEALTH_ORDER[health] < HEALTH_ORDER[snooze.health]:
        return None
    return snooze


def awaiting_outcome(account: Account, journal: AccountJournal, today: date) -> bool:
    """True when the account's renewal_date has passed (renewal_date < today) no more than
    OUTCOME_WINDOW_DAYS ago and no outcome's renewal_date is within OUTCOME_MATCH_DAYS of it."""
    renewal = account.renewal_date
    passed_recently = renewal < today and (today - renewal).days <= OUTCOME_WINDOW_DAYS
    if not passed_recently:
        return False
    answered = any(
        abs((outcome.renewal_date - renewal).days) <= OUTCOME_MATCH_DAYS
        for outcome in journal.outcomes
    )
    return not answered


# --- The Today page (pure) ----------------------------------------------------------------


class DueItem(BaseModel):
    """A follow-up on the Today page."""

    scored: ScoredAccount
    follow_up: FollowUp
    days_late: int  # today - due in days; 0 when due today, negative for "Coming up"


class AttentionItem(BaseModel):
    """An account in "Needs attention": at risk or watch, not snoozed, no open follow-up, and
    no contact within QUIET_DAYS."""

    scored: ScoredAccount
    rank: int  # its place in the whole book, from 1
    last_contact: date | None


class SnoozedItem(BaseModel):
    scored: ScoredAccount
    snooze: Snooze


class TodayView(BaseModel):
    due: list[DueItem]  # open follow-ups due today or earlier: most days late first, then rank
    attention: list[AttentionItem]  # book order, at most ATTENTION_LIMIT
    outcomes: list[ScoredAccount]  # awaiting_outcome, renewal_date ascending
    upcoming: list[DueItem]  # open follow-ups due in the next UPCOMING_DAYS days, due ascending
    snoozed: list[SnoozedItem]  # active snoozes, until ascending
    contacted_this_week: int  # accounts with a contact note whose happened_on is in the last 7
    #                           days (today and the 6 days before)


def today_view(
    book: list[ScoredAccount],
    journals: dict[str, AccountJournal],
    today: date,
    *,
    quiet_days: int = QUIET_DAYS,
) -> TodayView:
    """Build the Today page from the scored book and every account's journal. Journal entries
    for ids that aren't in the book are ignored. An account is in "Needs attention" when its
    health is AT_RISK or WATCH, active_snooze is None, it has no open follow-up, and its
    last_contact is None or more than quiet_days (the Settings value; default QUIET_DAYS) days
    before today. A snoozed account is listed in `snoozed` and left out of `attention`; its open
    follow-ups still appear in `due` and `upcoming` (a follow-up is a promise the CSM made, so a
    snooze never hides it), and so does a renewal awaiting its outcome in `outcomes`."""
    upcoming_end = today + timedelta(days=UPCOMING_DAYS)
    quiet_before = today - timedelta(days=quiet_days)
    week_start = today - timedelta(days=6)
    due_rows: list[tuple[int, DueItem]] = []
    upcoming_rows: list[tuple[int, DueItem]] = []
    attention: list[AttentionItem] = []
    outcome_rows: list[tuple[int, ScoredAccount]] = []
    snoozed_rows: list[tuple[int, SnoozedItem]] = []
    contacted = 0
    for rank, scored in enumerate(book, start=1):
        journal = journals.get(scored.account.id, AccountJournal())
        open_items = open_follow_ups(journal)
        for follow_up in open_items:
            days_late = (today - follow_up.due).days
            if follow_up.due <= today:
                item = DueItem(scored=scored, follow_up=follow_up, days_late=days_late)
                due_rows.append((rank, item))
            elif follow_up.due <= upcoming_end:
                item = DueItem(scored=scored, follow_up=follow_up, days_late=days_late)
                upcoming_rows.append((rank, item))
        snooze = active_snooze(journal, scored.health, today)
        if snooze is not None:
            snoozed_rows.append((rank, SnoozedItem(scored=scored, snooze=snooze)))
        else:
            latest = last_contact(journal)
            quiet = latest is None or latest < quiet_before
            if scored.health in (Health.AT_RISK, Health.WATCH) and not open_items and quiet:
                attention.append(AttentionItem(scored=scored, rank=rank, last_contact=latest))
        if awaiting_outcome(scored.account, journal, today):
            outcome_rows.append((rank, scored))
        if any(
            note.kind.is_contact and week_start <= note.happened_on <= today
            for note in journal.notes
        ):
            contacted += 1

    due_rows.sort(key=lambda row: (-row[1].days_late, row[0]))
    upcoming_rows.sort(key=lambda row: (row[1].follow_up.due, row[0]))
    outcome_rows.sort(key=lambda row: (row[1].account.renewal_date, row[0]))
    snoozed_rows.sort(key=lambda row: (row[1].snooze.until, row[0]))
    return TodayView(
        due=[item for _, item in due_rows],
        attention=attention[:ATTENTION_LIMIT],
        outcomes=[scored for _, scored in outcome_rows],
        upcoming=[item for _, item in upcoming_rows],
        snoozed=[item for _, item in snoozed_rows],
        contacted_this_week=contacted,
    )


# --- The journal file ---------------------------------------------------------------------


class Journal:
    """Read and change the journal file. Reads never raise (a missing or unreadable file reads
    as empty). Every change holds store.WRITE_LOCK around load -> change -> save. Methods that
    take ids raise LookupError("That note isn't in this account.") / ("That follow-up isn't in
    this account.") / ("That outcome isn't in this account.") for an unknown id, and ValueError
    with the clean_*/check_* messages for bad values."""

    def __init__(self, store: LocalStore, *, demo: bool = False) -> None:
        self._store = store
        self._path = store.root / (DEMO_JOURNAL_FILE if demo else JOURNAL_FILE)

    def load(self) -> JournalData:
        return _load(self._path)

    def for_account(self, account_id: str) -> AccountJournal:
        """That account's journal (an empty one when it has none)."""
        return _load(self._path).by_account.get(account_id, AccountJournal())

    def all(self) -> dict[str, AccountJournal]:
        return _load(self._path).by_account

    # Notes
    def add_note(
        self,
        account_id: str,
        kind: TouchKind,
        text: str,
        happened_on: date,
        *,
        now: datetime,
        today: date,
        source: NoteSource = NoteSource.MANUAL,
    ) -> Note:
        """ValueError("This account has 1,000 notes, the most it can keep. Remove some old
        ones first.") at MAX_NOTES_PER_ACCOUNT. `source` is MANUAL unless Claude (AGENT) or an
        accepted suggestion (SUGGESTION) adds it; MAILBOX notes come from log_email."""
        cleaned = clean_note_text(text)
        day = check_happened_on(happened_on, today)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            if _own_entries(journal) >= MAX_NOTES_PER_ACCOUNT:
                raise ValueError(_TOO_MANY_NOTES)
            note = Note(
                id=_new_id("n-", {entry.id for entry in journal.notes}),
                kind=kind,
                text=cleaned,
                happened_on=day,
                created_at=_naive(now),
                source=source,
            )
            journal.notes.append(note)
            data.by_account[account_id] = journal
            _save(self._path, data)
        return note

    def update_note(
        self,
        account_id: str,
        note_id: str,
        kind: TouchKind,
        text: str,
        happened_on: date,
        *,
        now: datetime,
        today: date,
    ) -> Note:
        """Sets kind, text, happened_on and edited_at=now."""
        cleaned = clean_note_text(text)
        day = check_happened_on(happened_on, today)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            index = _position(journal.notes, note_id, _NO_NOTE)
            current = journal.notes[index]
            # Only an email has a direction, so it is cleared when the kind changes away from
            # EMAIL. Everything else about the entry (pinned, subject, by, source, message_id,
            # people, thread, created_at) stays as it was.
            updated = current.model_copy(
                update={
                    "kind": kind,
                    "text": cleaned,
                    "happened_on": day,
                    "edited_at": _naive(now),
                    "direction": current.direction if kind is TouchKind.EMAIL else None,
                }
            )
            journal.notes[index] = updated
            data.by_account[account_id] = journal
            _save(self._path, data)
        return updated

    def remove_note(self, account_id: str, note_id: str) -> bool:
        """False for an unknown id (nothing to do)."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id)
            if journal is None or not any(entry.id == note_id for entry in journal.notes):
                return False
            journal.notes = [entry for entry in journal.notes if entry.id != note_id]
            _save(self._path, data)
            return True

    # Follow-ups
    def add_activity(
        self,
        account_id: str,
        kind: TouchKind,
        text: str,
        happened_on: date,
        *,
        subject: str = "",
        by: str = "",
        direction: Direction | None = None,
        now: datetime,
        today: date,
    ) -> Note:
        """A hand-logged entry with a subject and who did it (source MANUAL). `kind` NOTE is
        allowed. `direction` is kept only for EMAIL (None otherwise). The text may be blank when a
        subject is given, in which case the text becomes the subject. Same limits and messages as
        add_note; the subject is checked with clean_subject; `by` is "" or checked like
        clean_team_name (its messages say "name")."""
        cleaned_subject = clean_subject(subject)
        cleaned_by = clean_team_name(by) if by.strip() else ""
        cleaned = clean_note_text(text if text.strip() else cleaned_subject)
        day = check_happened_on(happened_on, today)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            if _own_entries(journal) >= MAX_NOTES_PER_ACCOUNT:
                raise ValueError(_TOO_MANY_NOTES)
            note = Note(
                id=_new_id("n-", {entry.id for entry in journal.notes}),
                kind=kind,
                text=cleaned,
                happened_on=day,
                created_at=_naive(now),
                subject=cleaned_subject,
                by=cleaned_by,
                direction=direction if kind is TouchKind.EMAIL else None,
            )
            journal.notes.append(note)
            data.by_account[account_id] = journal
            _save(self._path, data)
        return note

    def log_email(self, account_id: str, email: LoggedEmail, *, now: datetime) -> Note | None:
        """Log one email from the mailbox sync. None (nothing written) when this account already
        has an entry with the same message_id. Mailbox entries have their own room
        (MAX_MAILBOX_ENTRIES), so mail can never fill the space for the CSM's own notes: when it
        is full, the oldest unpinned mailbox entry makes way (None when all are pinned). An
        email dated more than MAILBOX_DAYS_AHEAD after today is dated today+1 instead, so a
        forged Date header can't make an account look contacted for weeks. Never raises
        for odd content: the subject is made one line (breaks and tabs become spaces) and cut
        to MAX_SUBJECT; control and format characters are dropped from subject, snippet and by
        (the snippet keeps \n and \t); the text is the snippet stripped and cut to MAX_NOTE, else
        the subject, else "(No subject)". happened_on is email.sent_on, capped as above. people
        is cut to MAX_PEOPLE. thread is thread_key(subject). kind EMAIL, source MAILBOX,
        created_at now."""
        subject = _subject_line(email.subject)
        snippet = _snippet_text(email.snippet)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            if any(entry.message_id == email.message_id for entry in journal.notes):
                return None
            mailbox = [entry for entry in journal.notes if entry.source is NoteSource.MAILBOX]
            if len(mailbox) >= MAX_MAILBOX_ENTRIES:
                movable = [entry for entry in mailbox if not entry.pinned]
                if not movable:
                    return None
                oldest = min(movable, key=lambda entry: (entry.happened_on, entry.created_at))
                journal.notes = [entry for entry in journal.notes if entry.id != oldest.id]
            latest = _naive(now).date() + timedelta(days=MAILBOX_DAYS_AHEAD)
            note = Note(
                id=_new_id("n-", {entry.id for entry in journal.notes}),
                kind=TouchKind.EMAIL,
                text=snippet or subject or _NO_SUBJECT,
                happened_on=min(email.sent_on, latest),
                created_at=_naive(now),
                subject=subject,
                by=_drop_hidden(email.by).strip(),
                direction=email.direction,
                source=NoteSource.MAILBOX,
                message_id=email.message_id,
                people=email.people[:MAX_PEOPLE],
                thread=thread_key(subject),
            )
            journal.notes.append(note)
            data.by_account[account_id] = journal
            _save(self._path, data)
        return note

    def set_pinned(self, account_id: str, note_id: str, pinned: bool) -> Note:
        """Pin or unpin an entry. LookupError("That entry isn't there any more.") when missing.
        Pinning when MAX_PINNED are already pinned raises ValueError("You can pin up to 5. Unpin
        one first."). Pinning a pinned entry, or unpinning an unpinned one, changes nothing."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            index = _position(journal.notes, note_id, _NO_ENTRY)
            current = journal.notes[index]
            if current.pinned == pinned:
                return current
            if pinned and sum(1 for entry in journal.notes if entry.pinned) >= MAX_PINNED:
                raise ValueError(_TOO_MANY_PINNED)
            updated = current.model_copy(update={"pinned": pinned})
            journal.notes[index] = updated
            data.by_account[account_id] = journal
            _save(self._path, data)
        return updated

    def add_team_member(
        self,
        account_id: str,
        name: str,
        email: str,
        role: TeamRole,
        *,
        now: datetime,
    ) -> TeamMember:
        """ValueError from clean_team_name / clean_team_email; ValueError("This account has 12
        people on its team, the most it can keep.") at MAX_TEAM; ValueError("Someone on the team
        already has that email.") for a duplicate email."""
        cleaned_name = clean_team_name(name)
        cleaned_email = clean_team_email(email)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            if len(journal.team) >= MAX_TEAM:
                raise ValueError(_TOO_MANY_TEAM)
            if cleaned_email is not None and any(
                member.email == cleaned_email for member in journal.team
            ):
                raise ValueError(_TEAM_EMAIL_TAKEN)
            member = TeamMember(
                id=_new_id("t-", {entry.id for entry in journal.team}),
                name=cleaned_name,
                email=cleaned_email,
                role=role,
                created_at=_naive(now),
            )
            journal.team.append(member)
            data.by_account[account_id] = journal
            _save(self._path, data)
        return member

    def remove_team_member(self, account_id: str, member_id: str) -> bool:
        """True if one was removed."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id)
            if journal is None or not any(entry.id == member_id for entry in journal.team):
                return False
            journal.team = [entry for entry in journal.team if entry.id != member_id]
            _save(self._path, data)
            return True

    def add_follow_up(
        self,
        account_id: str,
        text: str,
        due: date,
        *,
        now: datetime,
        today: date,
        rule: str | None = None,
    ) -> FollowUp:
        """ValueError("This account has 20 open follow-ups. Finish some first.") at
        MAX_OPEN_FOLLOW_UPS open ones. `rule` is the occasion key of an automatic follow-up
        (core/rules.py): ValueError("That follow-up was already added.") when this account already
        has a follow-up with that key, open or done."""
        cleaned = clean_follow_up_text(text)
        day = check_due(due, today)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            if rule is not None and (
                rule in journal.dismissed_rules
                or any(entry.rule == rule for entry in journal.follow_ups)
            ):
                raise ValueError(_RULE_TAKEN)
            if len(open_follow_ups(journal)) >= MAX_OPEN_FOLLOW_UPS:
                raise ValueError(_TOO_MANY_OPEN)
            item = FollowUp(
                id=_new_id("f-", {entry.id for entry in journal.follow_ups}),
                text=cleaned,
                due=day,
                status=FollowUpStatus.OPEN,
                created_at=_naive(now),
                rule=rule,
            )
            journal.follow_ups.append(item)
            data.by_account[account_id] = journal
            _save(self._path, data)
        return item

    def complete_follow_up(self, account_id: str, follow_up_id: str, *, now: datetime) -> FollowUp:
        """Mark DONE with done_at=now. Completing a DONE one changes nothing and returns it."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            index = _position(journal.follow_ups, follow_up_id, _NO_FOLLOW_UP)
            current = journal.follow_ups[index]
            if current.status is FollowUpStatus.DONE:
                return current
            updated = current.model_copy(
                update={"status": FollowUpStatus.DONE, "done_at": _naive(now)}
            )
            journal.follow_ups[index] = updated
            data.by_account[account_id] = journal
            _save(self._path, data)
        return updated

    def reopen_follow_up(self, account_id: str, follow_up_id: str) -> FollowUp:
        """Back to OPEN, done_at=None (the Undo after Done). Its due date is kept even if past.
        ValueError like add_follow_up when the account already has MAX_OPEN_FOLLOW_UPS open."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            index = _position(journal.follow_ups, follow_up_id, _NO_FOLLOW_UP)
            current = journal.follow_ups[index]
            if current.status is FollowUpStatus.OPEN:
                return current  # already open: nothing to reopen, so the cap doesn't apply
            if len(open_follow_ups(journal)) >= MAX_OPEN_FOLLOW_UPS:
                raise ValueError(_TOO_MANY_OPEN)
            updated = current.model_copy(update={"status": FollowUpStatus.OPEN, "done_at": None})
            journal.follow_ups[index] = updated
            data.by_account[account_id] = journal
            _save(self._path, data)
        return updated

    def move_follow_up(
        self, account_id: str, follow_up_id: str, due: date, *, today: date
    ) -> FollowUp:
        """A new due date (check_due). Only an OPEN follow-up can move:
        ValueError("That follow-up is already done.") otherwise."""
        day = check_due(due, today)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            index = _position(journal.follow_ups, follow_up_id, _NO_FOLLOW_UP)
            current = journal.follow_ups[index]
            if current.status is FollowUpStatus.DONE:
                raise ValueError("That follow-up is already done.")
            updated = current.model_copy(update={"due": day})
            journal.follow_ups[index] = updated
            data.by_account[account_id] = journal
            _save(self._path, data)
        return updated

    def remove_follow_up(self, account_id: str, follow_up_id: str) -> bool:
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id)
            if journal is None or not any(entry.id == follow_up_id for entry in journal.follow_ups):
                return False
            removed = next(entry for entry in journal.follow_ups if entry.id == follow_up_id)
            journal.follow_ups = [entry for entry in journal.follow_ups if entry.id != follow_up_id]
            if removed.rule is not None and removed.rule not in journal.dismissed_rules:
                # Removed on purpose: the rule that added it mustn't add it again.
                journal.dismissed_rules = [*journal.dismissed_rules, removed.rule][
                    -MAX_DISMISSED_RULES:
                ]
            _save(self._path, data)
            return True

    # Snooze
    def snooze(
        self, account_id: str, until: date, health: Health, *, now: datetime, today: date
    ) -> Snooze:
        """Replace any snooze with this one (check_snooze_until)."""
        day = check_snooze_until(until, today)
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            record = Snooze(until=day, health=health, created_at=_naive(now))
            journal.snooze = record
            data.by_account[account_id] = journal
            _save(self._path, data)
        return record

    def wake(self, account_id: str) -> bool:
        """Remove the snooze; False when there was none."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id)
            if journal is None or journal.snooze is None:
                return False
            journal.snooze = None
            _save(self._path, data)
            return True

    # Outcomes
    def record_outcome(
        self,
        account_id: str,
        values: OutcomeInput,
        scored: ScoredAccount,
        latest: dict[SignalKey, float],
        *,
        now: datetime,
        today: date,
    ) -> Outcome:
        """Record an outcome for scored.account (whose id must equal account_id). arr_before is
        scored.account.arr; arr_after is 0.0 for CHURNED, else values.arr_after. decided_on goes
        through check_happened_on; note through clean_outcome_note. risk, priority, health and
        motion come from scored; signals is `latest`. An outcome with the same renewal_date
        replaces the earlier one (one answer per renewal)."""
        if scored.account.id != account_id:
            raise ValueError("That outcome is for a different account.")
        decided = check_happened_on(values.decided_on, today)
        note = clean_outcome_note(values.note)
        churned = values.kind is OutcomeKind.CHURNED
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            kept = [
                entry for entry in journal.outcomes if entry.renewal_date != values.renewal_date
            ]
            outcome = Outcome(
                id=_new_id("o-", {entry.id for entry in journal.outcomes}),
                kind=values.kind,
                renewal_date=values.renewal_date,
                decided_on=decided,
                arr_before=scored.account.arr,
                arr_after=0.0 if churned else values.arr_after,
                note=note,
                risk=scored.risk,
                priority=scored.priority,
                health=scored.health,
                motion=scored.motion,
                signals=dict(latest),
                recorded_at=_naive(now),
            )
            journal.outcomes = [*kept, outcome]
            data.by_account[account_id] = journal
            _save(self._path, data)
        return outcome

    def remove_outcome(self, account_id: str, outcome_id: str) -> bool:
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id)
            if journal is None or not any(entry.id == outcome_id for entry in journal.outcomes):
                return False
            journal.outcomes = [entry for entry in journal.outcomes if entry.id != outcome_id]
            _save(self._path, data)
            return True

    # Forecast calls (the honest forecast)
    def make_call(
        self,
        account_id: str,
        kind: CallKind,
        scored: ScoredAccount,
        *,
        now: datetime,
        today: date,
    ) -> ForecastCall:
        """Record your call on scored.account's next renewal (scored.account.id must equal
        account_id, else ValueError("That call is for a different account.")). renewal_date is
        scored.account.renewal_date, made_on is today, and health, risk and arr come from scored.
        A call with the same renewal_date replaces the earlier one (one call per renewal);
        calls on other renewal dates are kept, so past calls can be checked against outcomes."""
        if scored.account.id != account_id:
            raise ValueError("That call is for a different account.")
        renewal = scored.account.renewal_date
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id, AccountJournal())
            kept = [entry for entry in journal.calls if entry.renewal_date != renewal]
            made = ForecastCall(
                id=_new_id("c-", {entry.id for entry in journal.calls}),
                kind=kind,
                renewal_date=renewal,
                made_on=today,
                health=scored.health,
                risk=scored.risk,
                arr=scored.account.arr,
                recorded_at=_naive(now),
            )
            journal.calls = [*kept, made]
            data.by_account[account_id] = journal
            _save(self._path, data)
        return made

    def clear_call(self, account_id: str, renewal_date: date) -> bool:
        """Remove the call on that renewal_date. False when there wasn't one."""
        with WRITE_LOCK:
            data = _load(self._path)
            journal = data.by_account.get(account_id)
            if journal is None or not any(
                entry.renewal_date == renewal_date for entry in journal.calls
            ):
                return False
            journal.calls = [entry for entry in journal.calls if entry.renewal_date != renewal_date]
            _save(self._path, data)
            return True

    # Accounts that are linked or removed
    def move_account(self, from_id: str, to_id: str) -> None:
        """Linking: from_id's notes, follow-ups, outcomes, team and calls are appended to to_id's
        (ids that clash get new ones); the later of the two snoozes is kept. A moved MAILBOX email
        whose message_id to_id already has is dropped, and a moved team member whose email to_id's
        team already has is dropped. A moved call whose renewal_date to_id already has a call for
        is dropped too: to_id's call wins. from_id is then removed. Nothing happens when from_id
        has no journal or from_id == to_id."""
        if from_id == to_id:
            return
        with WRITE_LOCK:
            data = _load(self._path)
            if from_id not in data.by_account:
                return
            moving = data.by_account.pop(from_id)
            target = data.by_account.get(to_id, AccountJournal())
            notes = _without_repeats(
                moving.notes,
                {entry.message_id for entry in target.notes if entry.message_id is not None},
                lambda entry: entry.message_id,
            )
            team = _without_repeats(
                moving.team,
                {entry.email for entry in target.team if entry.email is not None},
                lambda entry: entry.email,
            )
            target.notes = [
                *target.notes,
                *_adopt(notes, {entry.id for entry in target.notes}, "n-"),
            ]
            target.follow_ups = [
                *target.follow_ups,
                *_adopt(moving.follow_ups, {entry.id for entry in target.follow_ups}, "f-"),
            ]
            target.outcomes = [
                *target.outcomes,
                *_adopt(moving.outcomes, {entry.id for entry in target.outcomes}, "o-"),
            ]
            target.team = [
                *target.team,
                *_adopt(team, {entry.id for entry in target.team}, "t-"),
            ]
            target_renewals = {entry.renewal_date for entry in target.calls}
            target.calls = [
                *target.calls,
                *_adopt(
                    [entry for entry in moving.calls if entry.renewal_date not in target_renewals],
                    {entry.id for entry in target.calls},
                    "c-",
                ),
            ]
            target.snooze = _later_snooze(moving.snooze, target.snooze)
            data.by_account[to_id] = target
            _save(self._path, data)

    def forget(self, gone: Callable[[str], bool]) -> int:
        """Delete the journal of every account id for which gone(id) is True; return how many."""
        with WRITE_LOCK:
            data = _load(self._path)
            gone_ids = [account_id for account_id in data.by_account if gone(account_id)]
            if not gone_ids:
                return 0
            for account_id in gone_ids:
                del data.by_account[account_id]
            _save(self._path, data)
            return len(gone_ids)


def _own_entries(journal: AccountJournal) -> int:
    """The entries that count against MAX_NOTES_PER_ACCOUNT: all but the mailbox's."""
    return sum(1 for entry in journal.notes if entry.source is not NoteSource.MAILBOX)


def _new_id(prefix: str, taken: set[str]) -> str:
    """prefix + 8 random hex characters, not in taken."""
    while True:
        candidate = prefix + secrets.token_hex(4)
        if candidate not in taken:
            return candidate


# --- Helpers (private) --------------------------------------------------------------------

_Entry = TypeVar("_Entry", bound=BaseModel)

# Characters that end a line. A follow-up is one line, so none of these may appear in it.
_LINE_BREAKS = ("\n", "\r", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}")
_TOO_MANY_NOTES = (
    f"This account has {MAX_NOTES_PER_ACCOUNT:,} notes, the most it can keep. "
    "Remove some old ones first."
)
_TOO_MANY_OPEN = f"This account has {MAX_OPEN_FOLLOW_UPS} open follow-ups. Finish some first."
_RULE_TAKEN = "That follow-up was already added."
_NO_NOTE = "That note isn't in this account."
_NO_FOLLOW_UP = "That follow-up isn't in this account."
_NO_ENTRY = "That entry isn't there any more."
_TOO_MANY_PINNED = f"You can pin up to {MAX_PINNED}. Unpin one first."
_TOO_MANY_TEAM = f"This account has {MAX_TEAM} people on its team, the most it can keep."
_TEAM_EMAIL_TAKEN = "Someone on the team already has that email."
_BAD_EMAIL = "Type an email address like name@company.com."
_MAX_EMAIL = 254  # the longest address the team email accepts
_NO_SUBJECT = "(No subject)"
# A reply or forward prefix at the start of a subject, in several languages: "Re:", "Fwd:",
# "re[2]:", "AW:", "SV:", "WG:". The colon is required; a space after it is optional.
_THREAD_PREFIX = re.compile(r"(?:re|fwd?|aw|sv|wg)(?:\[\d+\])?:\s*", re.IGNORECASE)


def _unify_breaks(text: str) -> str:
    """The text with \\r\\n and \\r turned into \\n."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _check_characters(text: str, what: str, *, allowed: str) -> None:
    """ValueError for a control character not in `allowed`, then for any format character."""
    if any(unicodedata.category(char) == "Cc" and char not in allowed for char in text):
        raise ValueError(f"A {what} can't contain control characters.")
    if any(unicodedata.category(char) == "Cf" for char in text):
        raise ValueError(f"A {what} can't contain hidden formatting characters.")


def _naive(moment: datetime) -> datetime:
    """A datetime as naive UTC, the shape the store keeps. A naive value is taken as UTC."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _load(path: Path) -> JournalData:
    """The saved journal; empty when the file is missing or unreadable."""
    try:
        return JournalData.model_validate_json(vault.read_text(path))
    except (OSError, ValueError):  # ValidationError and JSON errors are ValueErrors
        return JournalData()


def _set_aside_if_unreadable(path: Path) -> None:
    """A journal file that exists but can't be read (a bad hand edit, a damaged disk) reads as
    empty. Before the next change overwrites it, it is renamed to
    "<name>.unreadable-<UTC timestamp>.json" beside it, so the CSM's notes can still be recovered.
    store.clear_all deletes these copies too."""
    if not path.exists():
        return
    try:
        JournalData.model_validate_json(vault.read_text(path))
        return
    except (OSError, ValueError):
        pass
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    with contextlib.suppress(OSError):
        path.replace(path.with_name(f"{path.stem}.unreadable-{stamp}{path.suffix}"))


def _save(path: Path, data: JournalData) -> None:
    """Write the journal atomically, leaving out accounts with nothing in them. An unreadable
    file already there is set aside first (_set_aside_if_unreadable)."""
    _set_aside_if_unreadable(path)
    data.by_account = {
        account_id: found for account_id, found in data.by_account.items() if not found.empty
    }
    vault.write_text(path, data.model_dump_json(indent=2) + "\n")


def _position(entries: Sequence[_Entry], entry_id: str, message: str) -> int:
    """The index of the entry with this id, or LookupError(message)."""
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return index
    raise LookupError(message)


def _adopt(moved: list[_Entry], taken: set[str], prefix: str) -> list[_Entry]:
    """The moved entries, each given a new id when its id is already in `taken`. Every id kept
    is added to `taken`, so the moved entries can't clash with each other either."""
    kept: list[_Entry] = []
    for entry in moved:
        if entry.id in taken:
            entry = entry.model_copy(update={"id": _new_id(prefix, taken)})
        taken.add(entry.id)
        kept.append(entry)
    return kept


def _later_snooze(moved: Snooze | None, kept: Snooze | None) -> Snooze | None:
    """The snooze that lasts longer. A tie keeps the target's (`kept`)."""
    if moved is None:
        return kept
    if kept is None:
        return moved
    return moved if moved.until > kept.until else kept


def _without_repeats(
    moved: list[_Entry], taken: set[str], key: Callable[[_Entry], str | None]
) -> list[_Entry]:
    """The moved entries whose key is None or not yet in `taken`. Each key kept is added to
    `taken`, so a repeat among the moved entries is dropped as well."""
    kept: list[_Entry] = []
    for entry in moved:
        value = key(entry)
        if value is not None:
            if value in taken:
                continue
            taken.add(value)
        kept.append(entry)
    return kept


def _by_recency(entries: list[Note]) -> list[Note]:
    """Entries newest first: happened_on, then created_at, then id, all descending."""
    return sorted(
        entries, key=lambda note: (note.happened_on, note.created_at, note.id), reverse=True
    )


def _drop_hidden(text: str, *, keep: str = "") -> str:
    """The text without control and format characters, except those in `keep`."""
    return "".join(
        char for char in text if char in keep or unicodedata.category(char) not in ("Cc", "Cf")
    )


def _subject_line(text: str) -> str:
    """An email's subject made one line: each line break and tab becomes a space, control and format
    characters are dropped, then it is stripped and cut to MAX_SUBJECT."""
    spaced = _unify_breaks(text)
    for char in (*_LINE_BREAKS, "\t"):
        spaced = spaced.replace(char, " ")
    return _drop_hidden(spaced).strip()[:MAX_SUBJECT].rstrip()


def _snippet_text(text: str) -> str:
    """An email's snippet with its line breaks and tabs kept, control and format characters
    dropped, stripped and cut to MAX_NOTE."""
    return _drop_hidden(_unify_breaks(text), keep="\n\t").strip()[:MAX_NOTE].rstrip()
