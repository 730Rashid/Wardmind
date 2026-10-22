"""Look up every account's website (and, if asked, summarise it with AI) in one go.

A Job runs in a background thread inside the web app's process, so the page can show progress
by polling. One job at a time: starting another while one runs returns the running one.
Accounts are processed concurrently (MAX_SITE_WORKERS website lookups, at most MAX_AI_WORKERS
AI calls at once). One account failing never stops the others. A key problem (no key, key
rejected, out of credit) stops the AI part for the rest of the job, with the reason in
`ai_stopped`, while the website lookups carry on.

The runner knows nothing about settings, the store or the network: service.py passes the two
per-account functions in, so this module is easy to test with plain functions.

CONTRACT FILE: models and signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from cs_brief.core.models import Account
from cs_brief.core.research import SiteResearch
from cs_brief.core.summaries import Summary

MAX_SITE_WORKERS = 8
MAX_AI_WORKERS = 4


class LookupResult(StrEnum):
    LOOKED_UP = "looked_up"  # fetched now
    REUSED = "reused"  # a saved lookup from the last 24 hours was used
    NO_WEBSITE = "no_website"
    BLOCKED = "blocked"  # the website points at this computer or a private network
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            "looked_up": "Looked up",
            "reused": "Already looked up",
            "no_website": "No website",
            "blocked": "Not visited",
            "failed": "Couldn't read",
        }[self.value]


class SummaryResult(StrEnum):
    WRITTEN = "written"
    REUSED = "reused"  # the saved summary still matches the website text and model
    SKIPPED = "skipped"  # no lookup to summarise, or the AI part was stopped
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            "written": "Summarised",
            "reused": "Summary up to date",
            "skipped": "Not summarised",
            "failed": "Summary failed",
        }[self.value]


class Outcome(BaseModel):
    account_id: str
    account_name: str
    lookup: LookupResult
    summary: SummaryResult | None  # None when the job doesn't summarise
    message: str | None  # the plain-English reason for a failure, block or skip


class Job(BaseModel):
    id: str  # 16 hex characters
    started_at: datetime
    finished_at: datetime | None  # None while running
    summarise: bool
    refresh: bool  # look up again even when a saved lookup is fresh
    total: int
    outcomes: list[Outcome]  # in book order; only finished accounts are listed
    cost_usd: float  # total AI cost reported by OpenRouter for this job (0.0 if none)
    ai_stopped: str | None  # why the AI part stopped early, if it did

    @property
    def done(self) -> int:
        return len(self.outcomes)

    @property
    def running(self) -> bool:
        return self.finished_at is None


# Per-account work, supplied by service.py.
# research_one(account, refresh) → the lookup and whether it was reused; raises
#   research.ResearchError (NoWebsite, BlockedWebsite, SiteUnreachable, ...) on failure.
ResearchOne = Callable[[Account, bool], tuple[SiteResearch, bool]]
# summarise_one(account, research) → the summary and whether it was reused; raises
#   summaries.SummaryError on failure (NoKey, KeyRejected and OutOfCredit stop the AI part).
SummariseOne = Callable[[Account, SiteResearch], tuple[Summary, bool]]


class JobRunner:
    """Holds the current (or last) job. Thread-safe: the page polls job() while workers write.
    `clock` returns timezone-aware UTC now (tests pass a fixed one)."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        accounts: list[Account],
        research_one: ResearchOne,
        summarise_one: SummariseOne | None,
        *,
        refresh: bool,
    ) -> Job:
        """Start a job over `accounts` in a daemon thread and return its first snapshot. If a
        job is running, return that job's snapshot instead and start nothing. summarise_one is
        None when the job doesn't summarise."""
        raise NotImplementedError

    def job(self, job_id: str | None = None) -> Job | None:
        """A snapshot (a copy) of the current or last job; with job_id, only if it matches."""
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> Job | None:
        """Block until the current job finishes (tests and the CLI); return its snapshot."""
        raise NotImplementedError
