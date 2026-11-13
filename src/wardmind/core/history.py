"""Score history: a daily snapshot of the book, "what changed since last week", and each
account's risk trend.

**Your data.** Wardmind records one BookSnapshot per calendar day in store.root / HISTORY_DIR /
"<YYYY-MM-DD>.json" (config.write_atomic). The web app records it when My book is shown and the
feed scheduler about once an hour while Wardmind is open, so a day's file holds the latest state
of that day (rewritten at most every REWRITE_AFTER). Days Wardmind isn't open have no file.
Files older than KEEP_DAYS are deleted. Each entry keeps the account's name, rank, risk,
priority, health, motion, ARR and the latest value of every signal: the "feature vector" that the
planned learned scoring needs at the time of each renewal outcome. History starts the day this
feature is first used; nothing is invented for earlier days.

**Demo data.** Nothing is recorded. The demo's history is worked out on the fly by scoring the
demo book as of an earlier day, using only the readings observed by the end of that day
(rescored_snapshot). The demo's readings go back about 9 weeks, so its trend and "since last
week" are real computations on the sample data.

Privacy: snapshots are the same facts the book shows (names, ARR, scores and signal values),
kept on this computer. Delete all local data removes the folder (store.clear_all); removing a
connection or deleting an account removes that account's entries from every file (forget).

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them. Bodies marked NotImplementedError are to be written.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from wardmind.core import vault
from wardmind.core.config import ScoringConfig
from wardmind.core.models import (
    Account,
    Health,
    Motion,
    ScoredAccount,
    Signal,
    SignalKey,
    SignalKind,
)
from wardmind.core.scoring import _whole, latest_signals, score_book
from wardmind.core.store import HISTORY_DIR, WRITE_LOCK, LocalStore

KEEP_DAYS = 730  # two years: long enough to see a renewal cycle
REWRITE_AFTER = timedelta(hours=1)  # a day's file is rewritten at most this often
WEEK_DAYS = 7  # "since last week" compares with the snapshot on or before today - 7 days
TREND_DAYS = 90  # the account page's trend covers this many days
RANK_STEP = 5  # a rank move of at least this many places (same health) is worth a mention
RISK_STEP = 10.0  # a risk change of at least this many points (same health) is worth a mention
MAX_CHANGES = 12  # "since last week" lists at most this many


class SnapshotEntry(BaseModel):
    name: str
    rank: int = Field(ge=1)  # place in that day's book, from 1
    risk: float
    priority: float
    health: Health
    motion: Motion
    arr: float
    # Latest value of each signal: the built in, and the book's own ("custom:<id>" keys).
    signals: dict[SignalKey, float] = Field(default_factory=dict)


class BookSnapshot(BaseModel):
    day: date  # the calendar day it describes
    taken_at: datetime  # naive UTC
    accounts: dict[str, SnapshotEntry] = Field(default_factory=dict)  # account id -> entry


class TrendPoint(BaseModel):
    day: date
    risk: float
    health: Health
    rank: int


class ChangeKind(StrEnum):
    WORSE = "worse"  # health got worse (Healthy -> Watch, Watch -> At risk, Healthy -> At risk)
    NEW = "new"  # in the book now, not in the earlier snapshot
    RISING = "rising"  # same health, risk up by RISK_STEP or more, or rank up by RANK_STEP or more
    BETTER = "better"  # health got better
    GONE = "gone"  # in the earlier snapshot, not in the book now

    @property
    def label(self) -> str:
        return {
            "worse": "Got worse",
            "new": "New",
            "rising": "Rising risk",
            "better": "Improved",
            "gone": "Left the book",
        }[self.value]


class Change(BaseModel):
    account_id: str
    name: str
    kind: ChangeKind
    sentence: str  # plain English, e.g. "Moved from Watch to At risk (risk 38 to 52)."
    health_before: Health | None = None
    health_after: Health | None = None
    rank_before: int | None = None
    rank_after: int | None = None
    risk_before: float | None = None
    risk_after: float | None = None
    priority_after: float = 0.0  # for ordering within a kind (0 for GONE)


class WeekChanges(BaseModel):
    since: date  # the day of the snapshot compared with
    changes: list[Change]  # ordered by kind (ChangeKind order above), then priority_after desc,
    #                        then name; at most MAX_CHANGES
    more: int = 0  # how many more changes there were beyond MAX_CHANGES
    unchanged: bool = False  # True when there were no changes at all


# --- Building snapshots (pure) ------------------------------------------------------------


def snapshot_of(
    book: list[ScoredAccount], signals: Iterable[Signal], day: date, now: datetime
) -> BookSnapshot:
    """A snapshot of a scored book in its order (rank 1 first). `signals` are the readings the
    book was scored from; each entry's `signals` holds the latest value per kind for that account
    (scoring.latest_signals), so the entry matches the scores."""
    latest = latest_signals(signals)
    accounts: dict[str, SnapshotEntry] = {}
    for rank, scored in enumerate(book, start=1):
        account = scored.account
        readings = latest.get(account.id, {})
        accounts[account.id] = SnapshotEntry(
            name=account.name,
            rank=rank,
            risk=scored.risk,
            priority=scored.priority,
            health=scored.health,
            motion=scored.motion,
            arr=account.arr,
            signals={kind: readings[kind].value for kind in _in_book_order(readings)},
        )
    return BookSnapshot(day=day, taken_at=now, accounts=accounts)


_BUILTIN_POSITION: dict[SignalKind, int] = {kind: index for index, kind in enumerate(SignalKind)}


def _in_book_order(readings: Iterable[SignalKey]) -> list[SignalKey]:
    """The kinds with readings: the built in in SignalKind order, then the book's own by id. A
    snapshot keeps every kind that has a reading, including one the book has since removed."""

    def position(kind: SignalKey) -> tuple[int, int, str]:
        if isinstance(kind, SignalKind):
            return (0, _BUILTIN_POSITION[kind], "")
        return (1, 0, kind.value)

    return sorted(readings, key=position)


def rescored_snapshot(
    accounts: Iterable[Account],
    signals: Iterable[Signal],
    config: ScoringConfig,
    day: date,
    now: datetime,
) -> BookSnapshot:
    """The book as it would have been scored at the end of `day`: scoring.score_book with
    as_of=day over the readings whose observed_at falls on or before `day` (observed_at.date()
    <= day). Used for demo data, where every reading is known in advance."""
    known = [signal for signal in signals if signal.observed_at.date() <= day]
    book = score_book(accounts, known, config, as_of=day)
    return snapshot_of(book, known, day, now)


# --- Comparing (pure) ---------------------------------------------------------------------


# Health severity, to tell "worse" from "better".
_HEALTH_SEVERITY: dict[Health, int] = {Health.HEALTHY: 0, Health.WATCH: 1, Health.AT_RISK: 2}
_KIND_ORDER: dict[ChangeKind, int] = {kind: index for index, kind in enumerate(ChangeKind)}


def _moved(
    account_id: str, scored: ScoredAccount, rank: int, before: SnapshotEntry
) -> Change | None:
    """The change for an account in both snapshots, or None when nothing worth a mention moved."""
    name = scored.account.name
    if scored.health != before.health:
        worse = _HEALTH_SEVERITY[scored.health] > _HEALTH_SEVERITY[before.health]
        return Change(
            account_id=account_id,
            name=name,
            kind=ChangeKind.WORSE if worse else ChangeKind.BETTER,
            sentence=(
                f"Moved from {before.health.label} to {scored.health.label} "
                f"(risk {_whole(before.risk)} to {_whole(scored.risk)})."
            ),
            health_before=before.health,
            health_after=scored.health,
            rank_before=before.rank,
            rank_after=rank,
            risk_before=before.risk,
            risk_after=scored.risk,
            priority_after=scored.priority,
        )
    # Risk is kept to one decimal, so round the difference before comparing with the step.
    risk_up = round(scored.risk - before.risk, 1) >= RISK_STEP
    rank_up = before.rank - rank >= RANK_STEP
    if not (risk_up or rank_up):
        return None
    if risk_up:
        sentence = f"Risk up from {_whole(before.risk)} to {_whole(scored.risk)}."
    else:
        sentence = f"Up from #{before.rank} to #{rank}."
    return Change(
        account_id=account_id,
        name=name,
        kind=ChangeKind.RISING,
        sentence=sentence,
        health_before=before.health,
        health_after=scored.health,
        rank_before=before.rank,
        rank_after=rank,
        risk_before=before.risk,
        risk_after=scored.risk,
        priority_after=scored.priority,
    )


def changes_since(book: list[ScoredAccount], earlier: BookSnapshot) -> WeekChanges:
    """What changed between `earlier` and the book now. Each account gives at most one Change,
    the first that applies in this order: WORSE, BETTER (health moved), RISING (same health and
    risk up >= RISK_STEP or rank number down >= RANK_STEP), then NEW / GONE for accounts in only
    one of the two. Sentences (risk shown as whole numbers, ranks as "#3"):
      WORSE / BETTER: "Moved from Watch to At risk (risk 38 to 52)."
      RISING: "Risk up from 30 to 44." or, when only the rank moved, "Up from #14 to #6."
      NEW: "New in your book, ranked #7."
      GONE: "No longer in your book."
    A risk drop within the same health is not listed."""
    found: list[Change] = []
    book_ids: set[str] = set()
    for rank, scored in enumerate(book, start=1):
        account_id = scored.account.id
        book_ids.add(account_id)
        before = earlier.accounts.get(account_id)
        if before is None:
            found.append(
                Change(
                    account_id=account_id,
                    name=scored.account.name,
                    kind=ChangeKind.NEW,
                    sentence=f"New in your book, ranked #{rank}.",
                    health_after=scored.health,
                    rank_after=rank,
                    risk_after=scored.risk,
                    priority_after=scored.priority,
                )
            )
            continue
        change = _moved(account_id, scored, rank, before)
        if change is not None:
            found.append(change)
    for account_id, before in earlier.accounts.items():
        if account_id not in book_ids:
            found.append(
                Change(
                    account_id=account_id,
                    name=before.name,
                    kind=ChangeKind.GONE,
                    sentence="No longer in your book.",
                    health_before=before.health,
                    rank_before=before.rank,
                    risk_before=before.risk,
                )
            )
    # Kind order, then the biggest priority first, then name. The id is a last resort for equal
    # names, so the order never depends on how the input was arranged.
    found.sort(key=lambda c: (_KIND_ORDER[c.kind], -c.priority_after, c.name, c.account_id))
    return WeekChanges(
        since=earlier.day,
        changes=found[:MAX_CHANGES],
        more=max(0, len(found) - MAX_CHANGES),
        unchanged=not found,
    )


def trend_points(snapshots: Iterable[BookSnapshot], account_id: str) -> list[TrendPoint]:
    """The account's points, one per snapshot that has it, by day ascending."""
    points: list[TrendPoint] = []
    for snapshot in sorted(snapshots, key=lambda s: s.day):
        entry = snapshot.accounts.get(account_id)
        if entry is not None:
            points.append(
                TrendPoint(day=snapshot.day, risk=entry.risk, health=entry.health, rank=entry.rank)
            )
    return points


def health_since(
    book: Sequence[ScoredAccount], store: HistoryStore, today: date
) -> dict[str, date]:
    """For each account in `book`, the first day of its CURRENT health run: the earliest recorded
    day before today on which it had the same health as it has now. The run goes back through the
    snapshot files for as long as the account has that health in each one. A day with no snapshot
    file (Wardmind wasn't open) is skipped and does not end the run; the first snapshot where the
    account has another health, or is not in it, ends the run. An account with no earlier snapshot
    to look at starts its run today. This is rules.at_risk_since for every health."""
    start: dict[str, date] = {}
    running: dict[str, Health] = {}
    for scored in book:
        start[scored.account.id] = today
        running[scored.account.id] = scored.health
    for day in reversed([known for known in store.days() if known < today]):
        if not running:
            break
        snapshot = store.load(day)
        if snapshot is None:  # an unreadable file counts as absent, so it doesn't end a run
            continue
        for account_id, health in list(running.items()):
            entry = snapshot.accounts.get(account_id)
            if entry is not None and entry.health is health:
                start[account_id] = day
            else:
                del running[account_id]
    return start


# --- The history folder (Your data) -------------------------------------------------------


class HistoryStore:
    """store.root / HISTORY_DIR / "<YYYY-MM-DD>.json", one BookSnapshot per file. Reads never
    raise: a missing, unreadable or wrong-shaped file is treated as absent. Writes use
    config.write_atomic and hold store.WRITE_LOCK."""

    def __init__(self, store: LocalStore) -> None:
        self._folder = store.root / HISTORY_DIR

    def _file(self, day: date) -> Path:
        return self._folder / f"{day.isoformat()}.json"

    def days(self) -> list[date]:
        """The days that have a file, ascending (file names that aren't a date are ignored)."""
        try:
            paths = list(self._folder.glob("*.json"))
        except OSError:
            return []
        found: list[date] = []
        for path in paths:
            stem = path.stem
            if stem.startswith(".") or not path.is_file():  # temp and hidden files, folders
                continue
            try:
                day = date.fromisoformat(stem)
            except ValueError:
                continue
            if day.isoformat() == stem:  # only the YYYY-MM-DD names that _file() writes
                found.append(day)
        return sorted(found)

    def load(self, day: date) -> BookSnapshot | None:
        try:
            text = vault.read_text(self._file(day))
            snapshot = BookSnapshot.model_validate_json(text)
        except (OSError, ValueError):  # a pydantic ValidationError is a ValueError
            return None
        # A file that says it is another day is wrong-shaped, so it reads as absent.
        return snapshot if snapshot.day == day else None

    def on_or_before(self, day: date) -> BookSnapshot | None:
        """The snapshot of the latest day <= `day`, or None."""
        # An unreadable day counts as absent, so the next earlier readable day is used.
        for candidate in reversed(self.days()):
            if candidate <= day:
                snapshot = self.load(candidate)
                if snapshot is not None:
                    return snapshot
        return None

    def first_day(self) -> date | None:
        found = self.days()
        return found[0] if found else None

    def latest_between(self, since: date, until: date) -> BookSnapshot | None:
        """The readable snapshot of the latest day in [since, until], or None."""
        for candidate in reversed(self.days()):
            if since <= candidate <= until:
                snapshot = self.load(candidate)
                if snapshot is not None:
                    return snapshot
        return None

    def earliest_between(self, since: date, until: date) -> BookSnapshot | None:
        """The readable snapshot of the earliest day in [since, until], or None."""
        for candidate in self.days():
            if since <= candidate <= until:
                snapshot = self.load(candidate)
                if snapshot is not None:
                    return snapshot
        return None

    def between(self, since: date, until: date) -> list[BookSnapshot]:
        """Snapshots with since <= day <= until, by day ascending."""
        found: list[BookSnapshot] = []
        for candidate in self.days():
            if since <= candidate <= until:
                snapshot = self.load(candidate)
                if snapshot is not None:
                    found.append(snapshot)
        return found

    def save(self, snapshot: BookSnapshot) -> bool:
        """Write the day's file unless one already exists whose taken_at is less than
        REWRITE_AFTER before snapshot.taken_at; return whether it was written. After a write,
        delete files whose day is more than KEEP_DAYS before snapshot.day."""
        with WRITE_LOCK:
            existing = self.load(snapshot.day)
            if existing is not None and snapshot.taken_at - existing.taken_at < REWRITE_AFTER:
                return False
            vault.write_text(self._file(snapshot.day), snapshot.model_dump_json() + "\n")
            cutoff = snapshot.day - timedelta(days=KEEP_DAYS)
            for day in self.days():
                if day < cutoff:
                    with contextlib.suppress(OSError):  # a file that can't go is kept, not fatal
                        self._file(day).unlink(missing_ok=True)
            return True

    def forget(self, gone: Callable[[str], bool]) -> int:
        """Remove the entries of every account id for which gone(id) is True from every file
        (rewriting only files that change); return how many entries were removed."""
        removed = 0
        with WRITE_LOCK:
            for day in self.days():
                snapshot = self.load(day)
                if snapshot is None:
                    # Unreadable, so its entries can't be matched. It is useless anyway (it reads
                    # as absent), and it may still name a removed account, so it goes.
                    with contextlib.suppress(OSError):
                        self._file(day).unlink(missing_ok=True)
                    continue
                kept = {
                    account_id: entry
                    for account_id, entry in snapshot.accounts.items()
                    if not gone(account_id)
                }
                dropped = len(snapshot.accounts) - len(kept)
                if dropped:
                    removed += dropped
                    trimmed = snapshot.model_copy(update={"accounts": kept})
                    vault.write_text(self._file(day), trimmed.model_dump_json() + "\n")
        return removed

    def clear(self) -> None:
        """Delete the folder (missing is fine)."""
        with WRITE_LOCK:
            shutil.rmtree(self._folder, ignore_errors=True)
