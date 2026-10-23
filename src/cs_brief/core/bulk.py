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

import bisect
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from cs_brief.core.models import Account
from cs_brief.core.research import BlockedWebsite, NoWebsite, ResearchError, SiteResearch
from cs_brief.core.summaries import KeyRejected, NoKey, OutOfCredit, Summary, SummaryError

MAX_SITE_WORKERS = 8
MAX_AI_WORKERS = 4

# Plain-English messages for the accounts whose work failed in a way nobody planned for.
_UNEXPECTED_LOOKUP = "Something went wrong looking this website up. The other accounts carried on."
_UNEXPECTED_SUMMARY = "Something went wrong summarising this one. The other accounts carried on."
_UNEXPECTED_ACCOUNT = "Something went wrong with this account. The other accounts carried on."
_NO_LOOKUP = "No website lookup to summarise."


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


def _look_up(
    account: Account, research_one: ResearchOne, refresh: bool
) -> tuple[LookupResult, SiteResearch | None, str | None]:
    """(result, the lookup or None, the message for a failure or block)."""
    try:
        found, reused = research_one(account, refresh)
    except NoWebsite as error:
        return LookupResult.NO_WEBSITE, None, error.message
    except BlockedWebsite as error:
        return LookupResult.BLOCKED, None, error.message
    except ResearchError as error:
        return LookupResult.FAILED, None, error.message
    except Exception:  # a bug in one account must not stop the job
        return LookupResult.FAILED, None, _UNEXPECTED_LOOKUP
    return (LookupResult.REUSED if reused else LookupResult.LOOKED_UP), found, None


class JobRunner:
    """Holds the current (or last) job. Thread-safe: the page polls job() while workers write.
    `clock` returns timezone-aware UTC now (tests pass a fixed one)."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._thread: threading.Thread | None = None
        self._positions: list[int] = []  # book positions of the outcomes so far, sorted

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
        with self._lock:
            if self._job is not None and self._job.running:
                return self._job.model_copy(deep=True)
            job = Job(
                id=secrets.token_hex(8),
                started_at=self._clock(),
                finished_at=None,
                summarise=summarise_one is not None,
                refresh=refresh,
                total=len(accounts),
                outcomes=[],
                cost_usd=0.0,
                ai_stopped=None,
            )
            self._job = job
            self._positions = []
            self._thread = threading.Thread(
                target=self._run,
                args=(job, list(accounts), research_one, summarise_one, refresh),
                name=f"cs-brief-lookups-{job.id}",
                daemon=True,
            )
            self._thread.start()
            return job.model_copy(deep=True)

    def job(self, job_id: str | None = None) -> Job | None:
        """A snapshot (a copy) of the current or last job; with job_id, only if it matches."""
        with self._lock:
            if self._job is None:
                return None
            if job_id is not None and self._job.id != job_id:
                return None
            return self._job.model_copy(deep=True)

    def wait(self, timeout: float | None = None) -> Job | None:
        """Block until the current job finishes (tests and the CLI); return its snapshot."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.job()

    # --- the job's thread -----------------------------------------------------------------

    def _run(
        self,
        job: Job,
        accounts: list[Account],
        research_one: ResearchOne,
        summarise_one: SummariseOne | None,
        refresh: bool,
    ) -> None:
        gate = threading.BoundedSemaphore(MAX_AI_WORKERS)
        try:
            with ThreadPoolExecutor(
                max_workers=MAX_SITE_WORKERS, thread_name_prefix="cs-brief-lookup"
            ) as pool:
                for index, account in enumerate(accounts):
                    pool.submit(
                        self._work, job, index, account, research_one, summarise_one, refresh, gate
                    )
        finally:
            with self._lock:
                job.finished_at = self._clock()

    def _work(
        self,
        job: Job,
        index: int,
        account: Account,
        research_one: ResearchOne,
        summarise_one: SummariseOne | None,
        refresh: bool,
        gate: threading.BoundedSemaphore,
    ) -> None:
        """One account, recorded at its place in book order whatever happens inside it."""
        try:
            outcome, cost = self._outcome(job, account, research_one, summarise_one, refresh, gate)
        except Exception:  # never let one account kill the job
            outcome = Outcome(
                account_id=account.id,
                account_name=account.name,
                lookup=LookupResult.FAILED,
                summary=SummaryResult.FAILED if summarise_one is not None else None,
                message=_UNEXPECTED_ACCOUNT,
            )
            cost = 0.0
        with self._lock:
            position = bisect.bisect_left(self._positions, index)
            self._positions.insert(position, index)
            job.outcomes.insert(position, outcome)
            job.cost_usd += cost

    def _outcome(
        self,
        job: Job,
        account: Account,
        research_one: ResearchOne,
        summarise_one: SummariseOne | None,
        refresh: bool,
        gate: threading.BoundedSemaphore,
    ) -> tuple[Outcome, float]:
        lookup, found, message = _look_up(account, research_one, refresh)
        base = {"account_id": account.id, "account_name": account.name, "lookup": lookup}
        if summarise_one is None:
            return Outcome(**base, summary=None, message=message), 0.0
        if found is None:
            return (
                Outcome(
                    **base,
                    summary=SummaryResult.SKIPPED,
                    message=message or _NO_LOOKUP,
                ),
                0.0,
            )
        summary, summary_message, cost = self._summarise(job, account, found, summarise_one, gate)
        return Outcome(**base, summary=summary, message=message or summary_message), cost

    def _summarise(
        self,
        job: Job,
        account: Account,
        found: SiteResearch,
        summarise_one: SummariseOne,
        gate: threading.BoundedSemaphore,
    ) -> tuple[SummaryResult, str | None, float]:
        """(result, message, cost). Only a summary that was written adds its cost."""
        with self._lock:
            stopped = job.ai_stopped
        if stopped is not None:
            return SummaryResult.SKIPPED, stopped, 0.0
        try:
            with gate:
                summary, reused = summarise_one(account, found)
        except (NoKey, KeyRejected, OutOfCredit) as error:
            with self._lock:
                if job.ai_stopped is None:
                    job.ai_stopped = error.message
            return SummaryResult.FAILED, error.message, 0.0
        except SummaryError as error:
            return SummaryResult.FAILED, error.message, 0.0
        except Exception:  # a bug in one account must not stop the job
            return SummaryResult.FAILED, _UNEXPECTED_SUMMARY, 0.0
        if reused:
            return SummaryResult.REUSED, None, 0.0
        return SummaryResult.WRITTEN, None, summary.cost_usd or 0.0
