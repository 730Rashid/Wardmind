"""Suggestions: next steps Wardmind (or Claude) proposes, which the CSM accepts, edits or dismisses.

CONTRACT FILE. The models, the constants and the Proposals store below are the contract; the
lead wrote them. plan(), refresh(), accept(), dismiss() and expire_cleared() at the bottom are
built against it.

Nothing a suggestion says reaches the journal until the CSM accepts it. Accepting writes exactly
one journal entry (a follow-up, a note or a snooze) and stores its id in `result_id`; accepting
again changes nothing. A suggestion is unique per (account, key) for ever: a dismissed key is
never suggested again, and an accepted follow-up carries the key as FollowUp.rule, so the
automatic rules (core/rules.py) and a suggestion can never both add the same follow-up.

Where suggestions come from (ProposalSource):
- DETECTOR: plan(), run once a day after the score snapshot (service.refresh_suggestions) and
  when Today is opened with no run yet that day. Pure: the same inputs give the same suggestions.
- MCP: Claude, through the MCP server's propose_* tools (mcp/writes.py), when Settings >
  Assistant allows it.

Every suggestion says why (`why`, at least one Evidence), so the CSM can check it before
accepting. A suggestion whose reason has gone (the account recovered, the reply came) is marked
EXPIRED with `expired_reason`, never deleted, so the history stays honest.

Stored per book as PROPOSALS_FILE in the book's data folder (demo data: DEMO_PROPOSALS_FILE in the
first book's folder, like the demo journal), through core/vault.py so it is encrypted when the
book is. Reads never raise (a missing or unreadable file reads as empty). Every change holds
store.WRITE_LOCK around load -> change -> save.
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from wardmind.core import audit, books, insights, vault
from wardmind.core.config import ScoringConfig, load_scoring_config
from wardmind.core.drafts import DraftKind
from wardmind.core.history import Change, ChangeKind, WeekChanges
from wardmind.core.journal import (
    AccountJournal,
    Journal,
    NoteSource,
    TouchKind,
    active_snooze,
    awaiting_replies,
    last_contact,
)
from wardmind.core.models import Health, ScoredAccount, Signal, SignalKey
from wardmind.core.settings import Settings
from wardmind.core.store import WRITE_LOCK, LocalStore

PROPOSALS_FILE = "proposals.json"
DEMO_PROPOSALS_FILE = "demo-proposals.json"

MAX_OPEN_PER_ACCOUNT = 3  # open suggestions one account may have at once
MAX_OPEN = 30  # open suggestions in the whole book; more wait (RefreshResult.waiting)
MAX_PER_RUN = 12  # suggestions one plan() run may add, most urgent accounts first
MAX_BLIND_SPOTS = 3  # "Get fresh data" suggestions one plan() run makes, largest ARR first
MAX_KEPT = 2000  # decided (accepted, dismissed, expired) suggestions kept; the oldest go first
MAX_WHY = 6  # evidence lines on one suggestion
MAX_TITLE = 120
MAX_TEXT = 500
MAX_EVIDENCE = 200
MAX_KEY = 80


class ProposalKind(StrEnum):
    FOLLOW_UP = "follow_up"  # accept -> Journal.add_follow_up(text, due, rule=key)
    NOTE = "note"  # accept -> Journal.add_note(NOTE, text, source=SUGGESTION)
    SNOOZE = "snooze"  # accept -> Journal.snooze(until)


class ProposalSource(StrEnum):
    DETECTOR = "detector"  # Wardmind's own daily check (plan())
    MCP = "mcp"  # Claude, through the MCP server

    @property
    def label(self) -> str:
        return {"detector": "Wardmind", "mcp": "Claude"}[self.value]


class ProposalStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


class Detector(StrEnum):
    """The five daily checks plan() runs. Each key starts with the detector's value."""

    HEALTH_DROPPED = "health_dropped"  # health got worse since the snapshot a week ago
    RENEWAL_NO_CALL = "renewal_no_call"  # renewal within its window and no forecast call made
    REPLY_OVERDUE = "reply_overdue"  # an email of ours has waited longer than reply_wait_days
    GONE_QUIET = "gone_quiet"  # At risk or Watch, and no contact for quiet_days or more
    BLIND_SPOT = "blind_spot"  # looks Healthy on thin data: get a fresh reading


class Verdict(StrEnum):
    """The CSM's answer to "Did it work?" about a step they took (core/effects.py)."""

    HELPED = "helped"
    DIDNT_HELP = "didnt_help"
    TOO_EARLY = "too_early"  # asked again at the next check

    @property
    def label(self) -> str:
        return {"helped": "Helped", "didnt_help": "Didn't help", "too_early": "Too early"}[
            self.value
        ]


class DismissReason(StrEnum):
    """Why the CSM dismissed a suggestion (core/taste.py learns from it). NOT_NOW and
    ALREADY_ON_IT are about timing, so they don't count against the kind of suggestion; NOT_USEFUL,
    WRONG and a dismissal with no reason do."""

    NOT_NOW = "not_now"
    ALREADY_ON_IT = "already_on_it"
    NOT_USEFUL = "not_useful"
    WRONG = "wrong"

    @property
    def label(self) -> str:
        return {
            "not_now": "Not now",
            "already_on_it": "Already on it",
            "not_useful": "Not useful",
            "wrong": "Wrong",
        }[self.value]

    @property
    def counts_against(self) -> bool:
        return self in (DismissReason.NOT_USEFUL, DismissReason.WRONG)


class Review(BaseModel):
    verdict: Verdict
    check_days: int  # the check (effects.CHECK_DAYS) the answer was given on
    at: datetime  # naive UTC


class Evidence(BaseModel):
    """One reason a suggestion was made, in plain English: "Health went from Watch to At risk"
    (observed 2026-11-10). `signal` is the SignalKey value it rests on, when there is one."""

    text: str = Field(min_length=1, max_length=MAX_EVIDENCE)
    signal: str | None = None
    observed: date | None = None


class Proposal(BaseModel):
    id: str  # "p-" + 8 hex, unique in the file
    account_id: str
    kind: ProposalKind
    source: ProposalSource
    detector: Detector | None = None  # DETECTOR only
    # The occasion key, unique per account for ever, e.g. "health_dropped:2026-11-10" or
    # "renewal_no_call:2027-01-14"; for MCP, "mcp:" + the tool call's request_id.
    key: str = Field(min_length=1, max_length=MAX_KEY)
    title: str = Field(min_length=1, max_length=MAX_TITLE)  # what to do, one line
    why: list[Evidence] = Field(min_length=1, max_length=MAX_WHY)
    # What accepting writes: the follow-up's text, or the note's text, or the snooze's reason
    # (shown on the card; Journal.snooze keeps no reason).
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    due: date | None = None  # FOLLOW_UP: required
    until: date | None = None  # SNOOZE: required
    # When set, the card offers "Draft the email" (core/drafts.py, /accounts/{id}/draft).
    draft_kind: DraftKind | None = None
    status: ProposalStatus = ProposalStatus.OPEN
    created_at: datetime  # naive UTC
    decided_at: datetime | None = None  # naive UTC; set with ACCEPTED, DISMISSED or EXPIRED
    result_id: str | None = None  # ACCEPTED: the journal entry's id ("f-...", "n-...", "snooze")
    expired_reason: str | None = None  # EXPIRED: plain English, e.g. "Health recovered."
    basis_day: date  # the day the evidence was read
    review: Review | None = None  # ACCEPTED follow-ups: "Did it work?" (core/effects.py)
    dismiss_reason: DismissReason | None = None  # DISMISSED: why, when the CSM said

    @field_validator("title")
    @classmethod
    def _one_line(cls, value: str) -> str:
        """One line of plain text: no control characters; spaces trimmed."""
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Use one line of plain text.")
        return value.strip()

    @field_validator("text")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        """Plain text; line breaks allowed (a note may have several lines)."""
        if any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in value):
            raise ValueError("Use plain text.")
        return value.strip()

    @model_validator(mode="after")
    def _dates_match_kind(self) -> Proposal:
        if self.kind is ProposalKind.FOLLOW_UP and self.due is None:
            raise ValueError("A follow-up suggestion needs a due date.")
        if self.kind is ProposalKind.SNOOZE and self.until is None:
            raise ValueError("A snooze suggestion needs an end date.")
        return self


class ProposalsData(BaseModel):
    items: list[Proposal] = Field(default_factory=list)
    last_run: date | None = None  # the last day plan() ran for this book (refresh())
    # Settings > Assistant's Reset per detector (core/taste.py): decisions made before this
    # moment (naive UTC) are no longer learnt from.
    taste_resets: dict[Detector, datetime] = Field(default_factory=dict)


class RefreshResult(BaseModel):
    added: int = 0
    expired: int = 0
    waiting: int = 0  # suggestions the caps held back this run; shown as "N more waiting"


class Proposals:
    """Read and change the suggestions file of one book (or of the demo)."""

    def __init__(self, store: LocalStore, *, demo: bool = False) -> None:
        self._store = store
        self._path = store.root / (DEMO_PROPOSALS_FILE if demo else PROPOSALS_FILE)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ProposalsData:
        return _load(self._path)

    def all(self) -> list[Proposal]:
        """Every suggestion, oldest first."""
        return list(_load(self._path).items)

    def open(self, account_id: str | None = None) -> list[Proposal]:
        """Open suggestions, newest first; only that account's when account_id is given."""
        items = [
            item
            for item in _load(self._path).items
            if item.status is ProposalStatus.OPEN
            and (account_id is None or item.account_id == account_id)
        ]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def open_count(self) -> int:
        return sum(1 for item in _load(self._path).items if item.status is ProposalStatus.OPEN)

    def get(self, proposal_id: str) -> Proposal | None:
        return next((item for item in _load(self._path).items if item.id == proposal_id), None)

    def has_key(self, account_id: str, key: str) -> bool:
        """Whether this account has ever had a suggestion with this key, in any status."""
        return any(
            item.account_id == account_id and item.key == key for item in _load(self._path).items
        )

    def add(self, proposal: Proposal) -> Proposal | None:
        """Store a new OPEN suggestion with a fresh id, or return None when the account already
        has one with its key (in any status) or already has MAX_OPEN_PER_ACCOUNT open ones, or
        the book has MAX_OPEN open. The caller's id is replaced."""
        with WRITE_LOCK:
            data = _load(self._path)
            added = _add_to(data, proposal)
            if added is not None:
                _save(self._path, data)
        return added

    def add_many(self, proposals: Sequence[Proposal]) -> tuple[list[Proposal], int]:
        """add() for each, in order, in one save; returns (added, held back by the caps). A
        repeated key is skipped silently: it is not counted as held back."""
        added: list[Proposal] = []
        held = 0
        with WRITE_LOCK:
            data = _load(self._path)
            for proposal in proposals:
                if _key_taken(data, proposal.account_id, proposal.key):
                    continue
                stored = _add_to(data, proposal)
                if stored is None:
                    held += 1
                else:
                    added.append(stored)
            if added:
                _save(self._path, data)
        return added, held

    def replace(self, proposal: Proposal) -> Proposal:
        """Save a changed suggestion over the stored one with its id; LookupError(NOT_FOUND)
        when it is gone. Used by accept(), dismiss() and expire_cleared()."""
        with WRITE_LOCK:
            data = _load(self._path)
            for index, item in enumerate(data.items):
                if item.id == proposal.id:
                    data.items[index] = proposal
                    _save(self._path, _pruned(data))
                    return proposal
        raise LookupError(NOT_FOUND)

    def set_last_run(self, day: date) -> None:
        with WRITE_LOCK:
            data = _load(self._path)
            data.last_run = day
            _save(self._path, data)

    def reset_taste(self, detector: Detector, now: datetime) -> None:
        """Forget what was learnt about this detector: decisions before `now` no longer count
        (core/taste.py). The decisions themselves are kept."""
        with WRITE_LOCK:
            data = _load(self._path)
            data.taste_resets[detector] = naive_utc(now)
            _save(self._path, data)

    def clear(self) -> None:
        """Forget every suggestion (Delete all local data)."""
        with WRITE_LOCK:
            self._path.unlink(missing_ok=True)


NOT_FOUND = "That suggestion isn't there any more."


def new_id(taken: set[str]) -> str:
    while True:
        candidate = "p-" + secrets.token_hex(4)
        if candidate not in taken:
            return candidate


def naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _key_taken(data: ProposalsData, account_id: str, key: str) -> bool:
    return any(item.account_id == account_id and item.key == key for item in data.items)


def _add_to(data: ProposalsData, proposal: Proposal) -> Proposal | None:
    if _key_taken(data, proposal.account_id, proposal.key):
        return None
    open_items = [item for item in data.items if item.status is ProposalStatus.OPEN]
    if len(open_items) >= MAX_OPEN:
        return None
    if sum(1 for item in open_items if item.account_id == proposal.account_id) >= (
        MAX_OPEN_PER_ACCOUNT
    ):
        return None
    stored = proposal.model_copy(
        update={
            "id": new_id({item.id for item in data.items}),
            "status": ProposalStatus.OPEN,
            "created_at": naive_utc(proposal.created_at),
            "decided_at": None,
            "result_id": None,
            "expired_reason": None,
            "review": None,
            "dismiss_reason": None,
        }
    )
    data.items.append(stored)
    return stored


def _pruned(data: ProposalsData) -> ProposalsData:
    """Keep every open suggestion and the newest MAX_KEPT decided ones."""
    decided = [item for item in data.items if item.status is not ProposalStatus.OPEN]
    if len(decided) <= MAX_KEPT:
        return data
    newest = sorted(decided, key=lambda item: item.decided_at or item.created_at)[-MAX_KEPT:]
    keep = {item.id for item in newest}
    data.items = [
        item for item in data.items if item.status is ProposalStatus.OPEN or item.id in keep
    ]
    return data


def _load(path: Path) -> ProposalsData:
    try:
        return ProposalsData.model_validate_json(vault.read_text(path))
    except (OSError, ValueError):
        return ProposalsData()


def _save(path: Path, data: ProposalsData) -> None:
    vault.write_text(path, data.model_dump_json(indent=2) + "\n")


# --- Built against the contract (workstream A) ---------------------------------------------

MAX_REASONS = 3  # the account's top reasons, listed after a suggestion's own evidence
MAX_NAME = 60  # an account's name in a title is cut to this many characters
MAX_SUBJECT_TEXT = 120  # an email's subject in the evidence is cut to this many characters
MAX_THREAD_TEXT = 40  # a thread's text in its key; a longer one is replaced by a short hash
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_NOT_IN_BOOK = "No longer in your book."
_REPLACED = "A newer check replaced this one."
_DISMISSED_REFUSAL = "You dismissed this suggestion, so it can't be accepted now."
_ACCEPTED_REFUSAL = "You accepted this suggestion, so it can't be dismissed now."
_EXPIRED_DISMISS = "This suggestion has expired, so there is nothing to dismiss."
_EXPIRED_ACCEPT = "This suggestion has expired, so it can't be accepted."
_NEEDS_HEALTH = "This account isn't in your book, so it can't be snoozed here."
_REMOVED_RULE = "You removed this follow-up before, so it isn't added again."


@dataclass(frozen=True)
class _Facts:
    """What the detectors read. plan() and expire_cleared() build it the same way."""

    settings: Settings
    today: date
    config: ScoringConfig | None
    latest: Mapping[str, Mapping[SignalKey, Signal]]
    worse: Mapping[str, Change]  # account id -> its WORSE change in this week's changes
    since: Mapping[str, date]  # account id -> the first day of its current health run


@dataclass(frozen=True)
class _Found:
    """One reason a detector found for an account today, before it becomes a suggestion."""

    detector: Detector
    key: str
    title: str  # also the follow-up's text, so accepting writes what the card says
    due: date
    why: tuple[Evidence, ...]  # the detector's own evidence, most important first
    draft_kind: DraftKind | None


_Detect = Callable[[_Facts, ScoredAccount, AccountJournal], list[_Found]]


def _facts(
    settings: Settings,
    today: date,
    config: ScoringConfig | None,
    latest: Mapping[str, Mapping[SignalKey, Signal]],
    changes: WeekChanges | None,
    health_since: Mapping[str, date],
) -> _Facts:
    worse: dict[str, Change] = {}
    if changes is not None:
        worse = {item.account_id: item for item in changes.changes if item.kind is ChangeKind.WORSE}
    return _Facts(
        settings=settings,
        today=today,
        config=ScoringConfig() if config is None else config,
        latest=latest,
        worse=worse,
        since=health_since,
    )


def _cut_text(text: str, limit: int) -> str:
    """The text on one line, at most `limit` characters; a longer one ends with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _name(scored: ScoredAccount) -> str:
    """The account's name for a title: no control or hidden characters, cut to MAX_NAME."""
    visible = "".join(
        char for char in scored.account.name if unicodedata.category(char) not in ("Cc", "Cf")
    )
    return _cut_text(visible, MAX_NAME) or "this account"


def _day(day: date) -> str:
    """'3 Nov 2026'. Built here, so core doesn't depend on the web's date wording."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _days_text(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _reply_key(thread: str, sent_on: date) -> str:
    """reply_overdue:<thread>:<day>, with a long thread replaced by a short hash (MAX_KEY)."""
    if len(thread) <= MAX_THREAD_TEXT:
        text = thread
    else:
        text = hashlib.sha1(thread.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"reply_overdue:{text}:{sent_on.isoformat()}"


def _health_dropped(facts: _Facts, scored: ScoredAccount, entry: AccountJournal) -> list[_Found]:
    change = facts.worse.get(scored.account.id)
    if change is None or change.health_after is None:
        return []
    since = facts.since.get(scored.account.id, facts.today)
    return [
        _Found(
            detector=Detector.HEALTH_DROPPED,
            key=f"health_dropped:{change.health_after.value}:{since.isoformat()}",
            title=f"Check in with {_name(scored)}: health dropped to {change.health_after.label}",
            due=facts.today,
            why=(Evidence(text=change.sentence, observed=facts.today),),
            draft_kind=DraftKind.CHECK_IN,
        )
    ]


def _renewal_no_call(facts: _Facts, scored: ScoredAccount, entry: AccountJournal) -> list[_Found]:
    renewal = scored.account.renewal_date
    ahead = (renewal - facts.today).days
    if not 0 <= ahead <= facts.settings.rule_renewal_days:
        return []
    if any(call.renewal_date == renewal for call in entry.calls):
        return []
    return [
        _Found(
            detector=Detector.RENEWAL_NO_CALL,
            key=f"renewal_no_call:{renewal.isoformat()}",
            title=f"Make your renewal call for {_name(scored)}",
            due=facts.today,
            why=(
                Evidence(text=f"Renews {_day(renewal)}"),
                Evidence(text="No forecast call yet", observed=facts.today),
            ),
            draft_kind=DraftKind.RENEWAL,
        )
    ]


def _reply_overdue(facts: _Facts, scored: ScoredAccount, entry: AccountJournal) -> list[_Found]:
    found: list[_Found] = []
    threads = awaiting_replies(
        entry,
        facts.today,
        reply_wait_days=facts.settings.reply_wait_days,
        answer_wait_days=facts.settings.answer_wait_days,
    )
    for thread in threads:
        if not thread.waiting_on_them:  # they wrote to us and we owe the answer: not ours to chase
            continue
        why = [
            Evidence(
                text=(
                    f"Your email on {_day(thread.last_on)} has had no reply for "
                    f"{_days_text(thread.days)}"
                ),
                observed=thread.last_on,
            )
        ]
        if thread.subject.strip():
            subject = _cut_text(thread.subject, MAX_SUBJECT_TEXT)
            why.append(Evidence(text=f"Subject: {subject}", observed=thread.last_on))
        found.append(
            _Found(
                detector=Detector.REPLY_OVERDUE,
                key=_reply_key(thread.thread, thread.last_on),
                title=f"Chase {_name(scored)} for a reply",
                due=facts.today,
                why=tuple(why),
                draft_kind=DraftKind.CHASE_REPLY,
            )
        )
    return found


def _gone_quiet(facts: _Facts, scored: ScoredAccount, entry: AccountJournal) -> list[_Found]:
    if scored.health not in (Health.AT_RISK, Health.WATCH):
        return []
    last = last_contact(entry)
    quiet_before = facts.today - timedelta(days=facts.settings.quiet_days)
    if last is not None and last >= quiet_before:
        return []
    if last is None:
        why = Evidence(text="No contact logged yet")
        key = "gone_quiet:never"
    else:
        why = Evidence(text=f"Last contact {_day(last)}", observed=last)
        key = f"gone_quiet:{last.isoformat()}"
    return [
        _Found(
            detector=Detector.GONE_QUIET,
            key=key,
            title=f"Get back in touch with {_name(scored)}",
            due=facts.today,
            why=(why,),
            draft_kind=DraftKind.RE_ENGAGE,
        )
    ]


def _blind_spot(facts: _Facts, scored: ScoredAccount, entry: AccountJournal) -> list[_Found]:
    readings = dict(facts.latest.get(scored.account.id, {}))
    confidence = insights.score_confidence(scored, readings, facts.today, facts.config)
    if not confidence.blind_spot:
        return []
    labels = "\n".join(sorted([*confidence.missing, *confidence.stale]))
    digest = hashlib.sha1(labels.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return [
        _Found(
            detector=Detector.BLIND_SPOT,
            key=f"blind_spot:{digest}",
            title=f"Get fresh data for {_name(scored)}",
            due=facts.today,
            why=(Evidence(text=_cut_text(confidence.sentence, MAX_EVIDENCE)),),
            draft_kind=None,
        )
    ]


_DETECTORS: dict[Detector, _Detect] = {
    Detector.HEALTH_DROPPED: _health_dropped,
    Detector.RENEWAL_NO_CALL: _renewal_no_call,
    Detector.REPLY_OVERDUE: _reply_overdue,
    Detector.GONE_QUIET: _gone_quiet,
    Detector.BLIND_SPOT: _blind_spot,
}


def _reasons(facts: _Facts, scored: ScoredAccount) -> list[Evidence]:
    """The account's top reasons as evidence, each dated by the day of its reading."""
    readings = facts.latest.get(scored.account.id, {})
    found: list[Evidence] = []
    for reason in scored.reasons[:MAX_REASONS]:
        text = _cut_text(reason.plain_english, MAX_EVIDENCE)
        if not text:
            continue
        reading = readings.get(reason.kind)
        observed = None if reading is None else reading.observed_at.date()
        found.append(Evidence(text=text, signal=reason.kind.value, observed=observed))
    return found


def _eligible(entry: AccountJournal, found: _Found) -> bool:
    """Not when the CSM removed a follow-up with this key, nor when the account already has one
    with it, open or done: one occasion is never two follow-ups (core/rules.py checks the same)."""
    if found.key in entry.dismissed_rules:
        return False
    return not any(item.rule == found.key for item in entry.follow_ups)


def _proposal(facts: _Facts, scored: ScoredAccount, found: _Found, created: datetime) -> Proposal:
    why = [*found.why, *_reasons(facts, scored)][:MAX_WHY]
    return Proposal(
        id="",
        account_id=scored.account.id,
        kind=ProposalKind.FOLLOW_UP,
        source=ProposalSource.DETECTOR,
        detector=found.detector,
        key=found.key,
        title=found.title,
        why=why,
        text=found.title,
        due=found.due,
        draft_kind=found.draft_kind,
        created_at=created,
        basis_day=facts.today,
    )


def plan(
    book: Sequence[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    latest: Mapping[str, Mapping[SignalKey, Signal]],
    changes: WeekChanges | None,
    health_since: Mapping[str, date],
    settings: Settings,
    today: date,
    now: datetime,
    *,
    config: ScoringConfig | None = None,
    fewer: frozenset[Detector] = frozenset(),
    known: AbstractSet[tuple[str, str]] = frozenset(),
) -> list[Proposal]:
    """`known` holds the (account id, key) pairs the store already has, in any status: those are
    left out before `fewer` is applied, so an old (say, dismissed) suggestion never takes a held
    detector's one place.

    `fewer` (core/taste.py: the detectors the CSM keeps dismissing) are held back: each of
    them makes at most taste.FEWER_PER_RUN suggestions (the most urgent accounts'), and they come
    after every other suggestion, so MAX_PER_RUN cuts them first.

    The suggestions the five Detectors make for this book today, most urgent account first
    (book order), at most one per detector per account, and no GONE_QUIET where HEALTH_DROPPED
    fired (both ask for the same contact). Pure: no I/O, no randomness; ids are "" (Proposals.add
    gives the real one). `latest` is account id -> SignalKey value -> the
    latest Signal, as service.latest_book_signals returns it. `health_since` is account id -> the
    first day of its current health run (history.health_since); HEALTH_DROPPED keys use it, so the
    same drop is suggested once however many days it shows in `changes`. Empty for demo data.
    `config` is the scoring config the confidence check reads (None: the built-in signals only).
    An account with an active snooze gets nothing. For each detector the first candidate the
    journal doesn't already have is the one suggested."""
    facts = _facts(settings, today, config, latest, changes, health_since)
    created = naive_utc(now)
    planned: list[Proposal] = []
    for scored in book:
        entry = journals.get(scored.account.id, AccountJournal())
        if active_snooze(entry, scored.health, today) is not None:
            continue
        chosen: set[Detector] = set()
        for detector in Detector:
            if detector is Detector.GONE_QUIET and Detector.HEALTH_DROPPED in chosen:
                continue  # "Check in: health dropped" already asks for the same contact
            options = _DETECTORS[detector](facts, scored, entry)
            choice = next((found for found in options if _eligible(entry, found)), None)
            if choice is not None:
                chosen.add(detector)
                planned.append(_proposal(facts, scored, choice, created))
    spread = _fewer_blind_spots(planned, {item.account.id: item.account.arr for item in book})
    fresh = [item for item in spread if (item.account_id, item.key) not in known]
    return _held_back(fresh, fewer)


def _fewer_blind_spots(planned: list[Proposal], arr: Mapping[str, float]) -> list[Proposal]:
    """At most MAX_BLIND_SPOTS BLIND_SPOT suggestions, the largest accounts' (a new book with few
    readings would otherwise fill Today with them); the rest keep their order."""
    spots = [item for item in planned if item.detector is Detector.BLIND_SPOT]
    if len(spots) <= MAX_BLIND_SPOTS:
        return planned
    ranked = sorted(spots, key=lambda item: (-arr.get(item.account_id, 0.0), item.account_id))
    kept = {id(item) for item in ranked[:MAX_BLIND_SPOTS]}
    return [
        item for item in planned if item.detector is not Detector.BLIND_SPOT or id(item) in kept
    ]


def _held_back(planned: list[Proposal], fewer: frozenset[Detector]) -> list[Proposal]:
    """Each `fewer` detector keeps its first FEWER_PER_RUN suggestions (book order: the most urgent
    accounts'), and those kept go after every other suggestion, in their order. core/taste.py
    imports this module, so its FEWER_PER_RUN is read here, when plan() runs."""
    if not fewer:
        return planned
    from wardmind.core.taste import FEWER_PER_RUN  # local: taste imports this module

    ordinary: list[Proposal] = []
    held: list[Proposal] = []
    kept: dict[Detector | None, int] = {}
    for item in planned:
        if item.detector not in fewer:
            ordinary.append(item)
        elif kept.get(item.detector, 0) < FEWER_PER_RUN:
            kept[item.detector] = kept.get(item.detector, 0) + 1
            held.append(item)
    return ordinary + held


def refresh(
    store: Proposals,
    book: Sequence[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    latest: Mapping[str, Mapping[SignalKey, Signal]],
    changes: WeekChanges | None,
    health_since: Mapping[str, date],
    settings: Settings,
    today: date,
    now: datetime,
    *,
    config: ScoringConfig | None = None,
    fewer: frozenset[Detector] = frozenset(),
) -> RefreshResult:
    """Expire the open DETECTOR suggestions whose reason has gone (expire_cleared), then add
    plan()'s new ones, at most MAX_PER_RUN, through Proposals.add_many, and set last_run. Does
    nothing (and returns an empty result) when settings.suggestions is off. A key the store
    already has, in any status, is not new. `waiting` counts what the caps held back plus what
    was beyond MAX_PER_RUN. `config` defaults to load_scoring_config(). `fewer` goes to plan()."""
    if not settings.suggestions:
        return RefreshResult()
    scoring = config if config is not None else load_scoring_config()
    expired = expire_cleared(
        store, book, journals, latest, changes, health_since, settings, today, now, config=scoring
    )
    known = {(item.account_id, item.key) for item in store.all()}
    planned = plan(
        book,
        journals,
        latest,
        changes,
        health_since,
        settings,
        today,
        now,
        config=scoring,
        fewer=fewer,
        known=known,
    )
    fresh = [item for item in planned if (item.account_id, item.key) not in known]
    beyond = max(0, len(fresh) - MAX_PER_RUN)
    added, held = store.add_many(fresh[:MAX_PER_RUN])
    store.set_last_run(today)
    return RefreshResult(added=len(added), expired=expired, waiting=held + beyond)


def expire_cleared(
    store: Proposals,
    book: Sequence[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    latest: Mapping[str, Mapping[SignalKey, Signal]],
    changes: WeekChanges | None,
    health_since: Mapping[str, date],
    settings: Settings,
    today: date,
    now: datetime,
    *,
    config: ScoringConfig | None = None,
) -> int:
    """Mark EXPIRED (with expired_reason) every open DETECTOR suggestion whose reason no longer
    holds, and any open suggestion whose account has left the book; return how many. A detector
    suggestion still holds while its detector finds its key for the account. Snoozes don't count:
    a snooze hides a suggestion, it doesn't make its reason untrue."""
    facts = _facts(settings, today, config, latest, changes, health_since)
    when = naive_utc(now)
    in_book = {scored.account.id: scored for scored in book}
    expired = 0
    for item in store.open():
        scored = in_book.get(item.account_id)
        if scored is None:
            reason = _NOT_IN_BOOK
        elif item.source is ProposalSource.DETECTOR and item.detector is not None:
            entry = journals.get(item.account_id, AccountJournal())
            found = _DETECTORS[item.detector](facts, scored, entry)
            if any(option.key == item.key for option in found):
                continue
            # The detector still finds the account, under a newer key (a new week's drop, a new
            # set of missing readings): that one replaces this, which says so.
            reason = _REPLACED if found else _gone_reason(facts, scored, entry, item)
        else:
            continue
        store.replace(
            item.model_copy(
                update={
                    "status": ProposalStatus.EXPIRED,
                    "decided_at": when,
                    "expired_reason": reason,
                }
            )
        )
        expired += 1
    return expired


def _gone_reason(
    facts: _Facts, scored: ScoredAccount, entry: AccountJournal, item: Proposal
) -> str:
    """Why a detector suggestion no longer holds, in plain English."""
    detector = item.detector
    if detector is Detector.HEALTH_DROPPED:
        return "Health is no longer worse."
    if detector is Detector.RENEWAL_NO_CALL:
        if any(call.renewal_date == scored.account.renewal_date for call in entry.calls):
            return "The forecast call was made."
        return f"The renewal is no longer within {facts.settings.rule_renewal_days} days."
    if detector is Detector.REPLY_OVERDUE:
        return "The reply came, or the thread has gone quiet."
    if detector is Detector.GONE_QUIET:
        last = last_contact(entry)
        if last is not None and last >= item.basis_day:
            return f"You logged a contact on {_day(last)}."
        if scored.health is Health.HEALTHY:
            return "Health recovered."
        return "The account is no longer quiet."
    if scored.health is not Health.HEALTHY:
        return "It no longer looks healthy on thin data."
    return "Its readings are no longer thin."


class Edits(BaseModel):
    """What the CSM changed on the card before accepting: the text, the due day, the end of the
    snooze. None keeps the suggestion's own value."""

    text: str | None = None
    due: date | None = None
    until: date | None = None


def accept(
    store: Proposals,
    journal: Journal,
    proposal_id: str,
    *,
    now: datetime,
    today: date,
    edits: Edits | None = None,
    health: Health | None = None,
) -> Proposal:
    """Write the suggestion's one journal entry and mark it ACCEPTED with result_id. Accepting an
    ACCEPTED suggestion returns it unchanged. LookupError(NOT_FOUND) for an unknown id;
    ValueError with a plain message when it is DISMISSED or EXPIRED, or when the journal refuses
    the entry (its own messages). A follow-up whose key the journal already has is not written
    again: the suggestion is ACCEPTED with that follow-up's id. A snooze needs `health`, the
    account's health now (journal.snooze keeps it); without it, ValueError. Records audit
    PROPOSAL_ACCEPTED (detail: the kind). The edits replace the text, the due day or the end of
    a snooze; a snooze keeps no text, so an edited text is ignored for one."""
    with WRITE_LOCK:
        current = store.get(proposal_id)
        if current is None:
            raise LookupError(NOT_FOUND)
        if current.status is ProposalStatus.ACCEPTED:
            return current
        if current.status is ProposalStatus.DISMISSED:
            raise ValueError(_DISMISSED_REFUSAL)
        if current.status is ProposalStatus.EXPIRED:
            reason = f" {current.expired_reason}" if current.expired_reason else ""
            raise ValueError(_EXPIRED_ACCEPT + reason)
        result_id = _write_entry(journal, current, now=now, today=today, edits=edits, health=health)
        accepted = current.model_copy(
            update={
                "status": ProposalStatus.ACCEPTED,
                "decided_at": naive_utc(now),
                "result_id": result_id,
            }
        )
        store.replace(accepted)
    _audit(audit.Event.PROPOSAL_ACCEPTED, current)
    return accepted


def _write_entry(
    journal: Journal,
    current: Proposal,
    *,
    now: datetime,
    today: date,
    edits: Edits | None,
    health: Health | None,
) -> str:
    """The one journal entry the suggestion stands for: its id, or "snooze". An edit replaces the
    suggestion's own value; no edit keeps it."""
    text = current.text
    due = current.due
    until = current.until
    if edits is not None:
        text = text if edits.text is None else edits.text
        due = due if edits.due is None else edits.due
        until = until if edits.until is None else edits.until
    if current.kind is ProposalKind.FOLLOW_UP:
        return _follow_up(journal, current, text, max(due or today, today), now=now, today=today)
    if current.kind is ProposalKind.NOTE:
        note = journal.add_note(
            current.account_id,
            TouchKind.NOTE,
            text,
            today,
            now=now,
            today=today,
            source=NoteSource.SUGGESTION,
        )
        return note.id
    if health is None:
        raise ValueError(_NEEDS_HEALTH)
    journal.snooze(current.account_id, until or today, health, now=now, today=today)
    return "snooze"


def _follow_up(
    journal: Journal, current: Proposal, text: str, due: date, *, now: datetime, today: date
) -> str:
    """Add the follow-up under the suggestion's key. A key the journal already has is not an
    error: that follow-up's id stands for the suggestion."""
    try:
        return journal.add_follow_up(
            current.account_id, text, due, now=now, today=today, rule=current.key
        ).id
    except ValueError:
        account = journal.for_account(current.account_id)
        existing = next((item for item in account.follow_ups if item.rule == current.key), None)
        if existing is not None:
            return existing.id
        if current.key in account.dismissed_rules:
            raise ValueError(_REMOVED_RULE) from None
        raise


def dismiss(
    store: Proposals,
    proposal_id: str,
    *,
    now: datetime,
    reason: DismissReason | None = None,
) -> Proposal:
    """Mark it DISMISSED with the CSM's reason (if given), so its key is never suggested again.
    Dismissing a DISMISSED one returns it unchanged; ValueError when it is ACCEPTED or EXPIRED.
    Records PROPOSAL_DISMISSED."""
    with WRITE_LOCK:
        current = store.get(proposal_id)
        if current is None:
            raise LookupError(NOT_FOUND)
        if current.status is ProposalStatus.DISMISSED:
            return current
        if current.status is ProposalStatus.ACCEPTED:
            raise ValueError(_ACCEPTED_REFUSAL)
        if current.status is ProposalStatus.EXPIRED:
            raise ValueError(_EXPIRED_DISMISS)
        dismissed = current.model_copy(
            update={
                "status": ProposalStatus.DISMISSED,
                "decided_at": naive_utc(now),
                "dismiss_reason": reason,
            }
        )
        store.replace(dismissed)
    _audit(audit.Event.PROPOSAL_DISMISSED, current)
    return dismissed


def _audit(event: audit.Event, item: Proposal) -> None:
    """The log line names the event and the kind of suggestion only: never a name or any text."""
    audit.record(event, book=_book_name(), detail=item.kind.value)


def _book_name() -> str | None:
    try:
        return books.active_id() or None
    except books.BookError:
        return None
