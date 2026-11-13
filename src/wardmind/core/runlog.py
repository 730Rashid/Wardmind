"""Run records for the background work: what each job did, when, and whether it worked.

Every background step runs inside run(job): the housekeeping, the day's score snapshot, the
automatic follow-ups, the suggestions, each feed's sync and the mailbox check. The outcome goes
into runlog.json in the book's data folder (store.root): the newest RUNS_KEPT records per job, with
a plain-English detail and counts, never customer data. A failed run is also put in the activity
log (core/audit.py) once per job per day, so a job that fails every minute can't flood it.

run() never lets a failure to record break the job. The job's own exception still propagates, so
its caller decides what happens next, and a failure to write the record is logged by type and
dropped.

The scheduler's heartbeat (when this process started it, and the last tick) is kept in memory
here as well. A CLI or MCP process never starts the scheduler, so it reads as "not running here",
which is not a failure.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field

from wardmind.core import audit, books, mailbox, vault
from wardmind.core.feeds.http import FeedError
from wardmind.core.store import WRITE_LOCK, LocalStore

RUNLOG_FILE = "runlog.json"  # in the book's data folder (store.root); listed in store._ALL_FILES
RUNS_KEPT = 30  # records kept per job
RECENT = 10  # records the System status page lists per job
MAX_DETAIL = 160  # characters in one record's detail
FEED_JOB_PREFIX = "feed:"  # the feed jobs are "feed:<connection id>"
_UNEXPECTED = "Unexpected error: {name}."
_GENERIC = "The run didn't finish."
_KINDS = {
    "housekeeping": "housekeeping",
    "snapshot": "score snapshot",
    "rules": "follow-up rules",
    "suggestions": "suggestions",
    "mailbox": "mailbox check",
}
# Errors whose .message is Wardmind's own plain English (no path, no customer text).
_OWN_ERRORS = (FeedError, mailbox.MailboxError, vault.VaultError)
_log = logging.getLogger(__name__)


class RunRecord(BaseModel):
    """One finished run of a job. Times are naive UTC, as everywhere in the store. `day` is the
    calendar day a run was for (the score snapshot, the rules and the suggestions), so a run is
    done for a day once it has a successful record with that day."""

    job: str
    started_at: datetime
    finished_at: datetime
    ok: bool
    detail: str = ""  # plain English, "" when ok
    counts: dict[str, int] = Field(default_factory=dict)  # e.g. {"added": 2, "held_back": 0}
    day: date | None = None

    @property
    def seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class RunFile(BaseModel):
    """runlog.json: the records per job (oldest first), and for each job the day its failure was
    last put in the activity log (only today's entries are kept)."""

    runs: dict[str, list[RunRecord]] = Field(default_factory=dict)
    reported: dict[str, str] = Field(default_factory=dict)


class Run:
    """What a running job reports: counts for its record, and a failure it finds without raising
    (a feed that answered with an error, say). Set `counts` and call fail() before the block ends.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.failure: str | None = None

    def fail(self, detail: str) -> None:
        """Record this run as failed, with `detail` as its plain-English reason."""
        self.failure = _clean(detail) or _GENERIC


@dataclass(frozen=True)
class Heartbeat:
    """The scheduler in this process: when it was started (None when it isn't running here) and
    the time of its last tick (None before the first one)."""

    started_at: datetime | None
    last_tick: datetime | None


_state_lock = threading.Lock()
_started_at: datetime | None = None
_last_tick: datetime | None = None


# --- Reading -------------------------------------------------------------------------------------


def last(job: str, *, store: LocalStore | None = None) -> RunRecord | None:
    """The newest record of `job`, or None. Raises vault.Locked while the book is locked."""
    runs = _read(_resolve(store)).runs.get(job, [])
    return runs[-1] if runs else None


def last_success(job: str, *, store: LocalStore | None = None) -> RunRecord | None:
    """The newest successful record of `job`, or None. Raises vault.Locked while locked."""
    for record in reversed(_read(_resolve(store)).runs.get(job, [])):
        if record.ok:
            return record
    return None


def recent(job: str, *, limit: int = RECENT, store: LocalStore | None = None) -> list[RunRecord]:
    """The newest `limit` records of `job`, newest first."""
    runs = _read(_resolve(store)).runs.get(job, [])
    return list(reversed(runs[-limit:])) if limit > 0 else []


def jobs(*, store: LocalStore | None = None) -> list[str]:
    """Every job that has at least one record, sorted by name."""
    return sorted(name for name, runs in _read(_resolve(store)).runs.items() if runs)


# --- Recording -----------------------------------------------------------------------------------


@contextmanager
def run(job: str, *, day: date | None = None, store: LocalStore | None = None) -> Iterator[Run]:
    """Run a job's block and record how it went. A block that raises is recorded as failed with
    the reason detail_of gives, and the exception propagates. A block that calls Run.fail() is
    recorded as failed without raising. Recording never raises."""
    started = _now()
    outcome = Run()
    try:
        yield outcome
    except Exception as exc:
        _finish(
            job,
            started,
            ok=False,
            detail=detail_of(exc),
            counts=outcome.counts,
            day=day,
            store=store,
        )
        raise
    if outcome.failure is None:
        _finish(job, started, ok=True, detail="", counts=outcome.counts, day=day, store=store)
    else:
        _finish(
            job,
            started,
            ok=False,
            detail=outcome.failure,
            counts=outcome.counts,
            day=day,
            store=store,
        )


def detail_of(exc: BaseException) -> str:
    """The record's reason for an exception: Wardmind's own plain-English message when the error
    is one of its own, else only the exception's type. Another library's text is never kept (it
    can hold a path or a customer's name)."""
    if isinstance(exc, _OWN_ERRORS):
        message = getattr(exc, "message", "")
        if isinstance(message, str) and message.strip():
            return _clean(message)
    return _clean(_UNEXPECTED.format(name=type(exc).__name__))


def _finish(
    job: str,
    started: datetime,
    *,
    ok: bool,
    detail: str,
    counts: dict[str, int],
    day: date | None,
    store: LocalStore | None,
) -> None:
    """Append the record, keeping the newest RUNS_KEPT for the job, and put a failure in the
    activity log unless one for this job was already put there today. Never raises."""
    try:
        target = _resolve(store)
        finished = _now()
        today = date.today().isoformat()
        with WRITE_LOCK:
            data = _read(target)
            runs = data.runs.setdefault(job, [])
            runs.append(
                RunRecord(
                    job=job,
                    started_at=started,
                    finished_at=finished,
                    ok=ok,
                    detail=_clean(detail),
                    counts={name: int(count) for name, count in counts.items()},
                    day=day,
                )
            )
            del runs[:-RUNS_KEPT]
            report = not ok and data.reported.get(job) != today
            data.reported = {name: when for name, when in data.reported.items() if when == today}
            if report:
                data.reported[job] = today
            _write(target, data)
    except Exception as exc:  # recording must never stop the job it describes
        _log.warning("A background run wasn't recorded (%s).", type(exc).__name__)
        return
    if report:
        _report(job)


def _report(job: str) -> None:
    """The activity-log entry for a failed job: the kind of job, never its name or its text."""
    kind = "feed sync" if job.startswith(FEED_JOB_PREFIX) else _KINDS.get(job, "background job")
    try:
        audit.record(audit.Event.BACKGROUND_FAILED, book=books.active_id(), detail=kind)
    except Exception as exc:  # the activity log never raises, but keep the job's path clean
        _log.warning("A background failure wasn't logged (%s).", type(exc).__name__)


# --- The scheduler's heartbeat -------------------------------------------------------------------


def scheduler_started(now: datetime) -> None:
    """The web app started the feed scheduler in this process."""
    global _started_at, _last_tick
    with _state_lock:
        _started_at = now
        _last_tick = None


def scheduler_stopped() -> None:
    """The web app stopped the scheduler."""
    global _started_at
    with _state_lock:
        _started_at = None


def beat(now: datetime) -> None:
    """A scheduler tick happened (whether or not the book was unlocked to do any work)."""
    global _last_tick
    with _state_lock:
        _last_tick = now


def heartbeat() -> Heartbeat:
    with _state_lock:
        return Heartbeat(started_at=_started_at, last_tick=_last_tick)


# --- Files ---------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _resolve(store: LocalStore | None) -> LocalStore:
    """The given store, else the open book's (store.data_dir())."""
    return store if store is not None else LocalStore()


def _read(store: LocalStore) -> RunFile:
    """The saved records; empty when the file is missing or can't be parsed. A locked book raises
    vault.Locked, which the callers that read while locked catch."""
    try:
        return RunFile.model_validate_json(vault.read_text(store.root / RUNLOG_FILE))
    except (OSError, ValueError):  # a pydantic ValidationError is a ValueError
        return RunFile()


def _write(store: LocalStore, data: RunFile) -> None:
    vault.write_text(store.root / RUNLOG_FILE, data.model_dump_json() + "\n")


def _clean(text: str) -> str:
    """One line of plain text, at most MAX_DETAIL characters."""
    return " ".join(str(text).split())[:MAX_DETAIL].rstrip()
