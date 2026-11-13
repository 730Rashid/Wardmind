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

import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from wardmind.core import vault
from wardmind.core.drafts import DraftKind
from wardmind.core.history import WeekChanges
from wardmind.core.journal import AccountJournal, Journal
from wardmind.core.models import ScoredAccount, Signal, SignalKey
from wardmind.core.settings import Settings
from wardmind.core.store import WRITE_LOCK, LocalStore

PROPOSALS_FILE = "proposals.json"
DEMO_PROPOSALS_FILE = "demo-proposals.json"

MAX_OPEN_PER_ACCOUNT = 3  # open suggestions one account may have at once
MAX_OPEN = 60  # open suggestions in the whole book; more wait (RefreshResult.waiting)
MAX_PER_RUN = 30  # suggestions one plan() run may add
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


def plan(
    book: Sequence[ScoredAccount],
    journals: Mapping[str, AccountJournal],
    latest: Mapping[str, Mapping[SignalKey, Signal]],
    changes: WeekChanges | None,
    health_since: Mapping[str, date],
    settings: Settings,
    today: date,
    now: datetime,
) -> list[Proposal]:
    """The suggestions the five Detectors make for this book today, most urgent account first
    (book order), at most one per detector per account. Pure: no I/O, no randomness; ids are
    "" (Proposals.add gives the real one). `latest` is account id -> SignalKey value -> the
    latest Signal, as service.latest_book_signals returns it. `health_since` is account id -> the
    first day of its current health run (history.health_since); HEALTH_DROPPED keys use it, so the
    same drop is suggested once however many days it shows in `changes`. Empty for demo data."""
    raise NotImplementedError


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
) -> RefreshResult:
    """Expire the open DETECTOR suggestions whose reason has gone (expire_cleared), then add
    plan()'s new ones, at most MAX_PER_RUN, through Proposals.add_many, and set last_run. Does
    nothing (and returns an empty result) when settings.suggestions is off."""
    raise NotImplementedError


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
) -> int:
    """Mark EXPIRED (with expired_reason) every open DETECTOR suggestion whose reason no longer
    holds, and any open suggestion whose account has left the book; return how many."""
    raise NotImplementedError


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
) -> Proposal:
    """Write the suggestion's one journal entry and mark it ACCEPTED with result_id. Accepting an
    ACCEPTED suggestion returns it unchanged. LookupError(NOT_FOUND) for an unknown id;
    ValueError with a plain message when it is DISMISSED or EXPIRED, or when the journal refuses
    the entry (its own messages). Records audit PROPOSAL_ACCEPTED (detail: the kind)."""
    raise NotImplementedError


def dismiss(store: Proposals, proposal_id: str, *, now: datetime) -> Proposal:
    """Mark it DISMISSED, so its key is never suggested again. Dismissing a DISMISSED one returns
    it unchanged; ValueError when it is ACCEPTED or EXPIRED. Records PROPOSAL_DISMISSED."""
    raise NotImplementedError
