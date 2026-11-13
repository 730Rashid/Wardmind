"""Settings > System status, the freshness line and the problem banner (core/health.py,
core/runlog.py). Workstream D.

GET /settings/status lists every check with its state (Working, Needs a look, Not working), when it
last worked, and the page that fixes it. POST /settings/status/run runs the background catch-up once
for the open book, then goes back to the page (it works without JavaScript). The recent runs of each
job sit in a <details> per job.

Two Jinja globals serve the shared partials: freshness_line() for partials/freshness.html, and
status_banner() for partials/status_banner.html. Both read the book themselves and return None or []
on any error, so a page never breaks on them.

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared layout
and context every page needs); `templates` is the app's Jinja2Templates, whose env is the render
environment, so the globals reach every page.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from wardmind.core import health, runlog, service
from wardmind.core.store import LocalStore

Render = Callable[..., HTMLResponse]
_log = logging.getLogger(__name__)

NOTICES = {"ran": "Checks ran. The results below are up to date."}
RECENT_RUNS = runlog.RECENT  # per job, on the page
STATUS_PATH = "/settings/status"
_STATE_WORDS = {
    health.State.OK: "Working",
    health.State.WARN: "Needs a look",
    health.State.FAIL: "Not working",
}
_STATE_ICONS = {
    health.State.OK: "check",
    health.State.WARN: "status-overdue",
    health.State.FAIL: "cross",
}
_PAGE_LABELS = {
    "/unlock": "Unlock Wardmind",
    "/settings/connections": "Connections",
    "/settings/backup": "Backup & export",
    "/settings/privacy": "Data & privacy",
    "/settings/follow-ups": "Follow-ups",
    "/settings/mailbox": "Mailbox",
    "/settings/status": "System status",
    "/today": "Today",
}
_JOB_LABELS = {
    "housekeeping": "Housekeeping",
    "snapshot": "Score history",
    "rules": "Automatic follow-ups",
    "suggestions": "Suggestions",
    "mailbox": "Mailbox check",
}
_JOB_ORDER = tuple(_JOB_LABELS)  # the core jobs, in this order; feed jobs follow them
_COUNT_LABELS = {
    "added": "Added",
    "held_back": "Held back",
    "waiting": "Waiting",
    "rows": "Rows",
    "accounts": "Accounts",
    "read": "Read",
    "logged": "Logged",
}


@dataclass(frozen=True)
class FreshnessLine:
    """The freshness line's sentence. `warn` is True when a timed feed is overdue, so the line
    links to System status and its words say the scores may be out of date."""

    text: str
    warn: bool


@dataclass(frozen=True)
class BannerItem:
    """One problem on every page: its sentence, and the page that helps, with its link words."""

    message: str
    href: str
    link: str


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def freshness_line() -> FreshnessLine | None:
    """The line under a page's title, or None when there is nothing to say (demo data, a book with
    no timed feeds, or a feed that is still waiting for its first sync). Never raises."""
    try:
        today = date.today()
        data = health.freshness(_now(), today)
        if not data.sources:
            return None
        overdue = [source for source in data.sources if source.overdue]
        if overdue:
            oldest = min(
                overdue,
                key=lambda source: source.last_success or datetime.min,
            )
            if oldest.last_success is None:
                text = f"{oldest.label} hasn't synced yet: scores may be out of date."
            else:
                since = health.day_words(oldest.last_success, today)
                text = f"{oldest.label} hasn't synced since {since}: scores may be out of date."
            return FreshnessLine(text=text, warn=True)
        if data.as_of is None:
            return None
        return FreshnessLine(text=f"Data as of {health.stamp(data.as_of, today)}.", warn=False)
    except Exception as exc:
        _log.warning("The freshness line couldn't be made (%s).", type(exc).__name__)
        return None


def status_banner() -> list[BannerItem]:
    """The problems every page shows, with the link to the page that helps. [] on any error."""
    try:
        problems = health.banner(_now(), date.today())
        return [
            BannerItem(message=problem.message, href=problem.fix_url, link=_link(problem.fix_url))
            for problem in problems
        ]
    except Exception as exc:
        _log.warning("The status banner couldn't be made (%s).", type(exc).__name__)
        return []


def _link(href: str) -> str:
    """The link words for a page: "Unlock Wardmind" or "Go to Connections"."""
    label = _PAGE_LABELS.get(href)
    if label is None:
        return "See System status"
    return label if href == "/unlock" else f"Go to {label}"


def _stamp_or_none(moment: datetime | None, today: date) -> str:
    return "" if moment is None else health.stamp(moment, today)


def _row(check: health.Check, today: date) -> dict[str, object]:
    fix = check.fix_url
    return {
        "name": check.name,
        "state": check.state.value,
        "word": _STATE_WORDS[check.state],
        "icon": _STATE_ICONS[check.state],
        "detail": check.detail,
        "last": _stamp_or_none(check.last_success, today),
        "fix_url": fix or "",
        "fix_label": _link(fix) if fix else "",
    }


def _summary(found: list[health.Check]) -> str:
    """How many checks are not working and how many need a look, in plain words."""
    failing = sum(1 for check in found if check.state is health.State.FAIL)
    looking = sum(1 for check in found if check.state is health.State.WARN)
    if not failing and not looking:
        return "Everything is working."
    sentences: list[str] = []
    if failing:
        sentences.append(f"{failing} {'check is' if failing == 1 else 'checks are'} not working.")
    if looking:
        sentences.append(f"{looking} {'check needs' if looking == 1 else 'checks need'} a look.")
    return " ".join(sentences)


def _counts(counts: dict[str, int]) -> str:
    return ". ".join(
        f"{_COUNT_LABELS[name]} {count}" for name, count in counts.items() if name in _COUNT_LABELS
    )


def _job_label(job: str, store: LocalStore) -> str:
    """The words for a job: a core job's name, or "Feed: <name>" for a feed that still exists."""
    if job in _JOB_LABELS:
        return _JOB_LABELS[job]
    if job.startswith(runlog.FEED_JOB_PREFIX):
        config = store.get_connection(job[len(runlog.FEED_JOB_PREFIX) :])
        return f"Feed: {config.name}" if config is not None else "Feed"
    return "Background job"


def _recent(today: date) -> list[dict[str, object]]:
    """The recent runs of every job with records, one group per job, the core jobs first."""
    store = service.get_store()
    known = runlog.jobs(store=store)
    ordered = [job for job in _JOB_ORDER if job in known]
    ordered += [job for job in known if job not in _JOB_ORDER]
    groups: list[dict[str, object]] = []
    for job in ordered:
        rows = [
            {
                "when": health.stamp(record.finished_at, today),
                "result": "Worked" if record.ok else "Failed",
                "detail": record.detail,
                "counts": _counts(record.counts),
            }
            for record in runlog.recent(job, limit=RECENT_RUNS, store=store)
        ]
        groups.append({"label": _job_label(job, store), "rows": rows})
    return groups


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`, and the two globals the shared partials use."""
    templates.env.globals["freshness_line"] = freshness_line
    templates.env.globals["status_banner"] = status_banner

    def page(request: Request, *, notice: str = "", status_code: int = 200) -> HTMLResponse:
        today = date.today()
        found = health.checks(_now(), today)
        return render(
            request,
            "settings/status.html",
            page="System status",
            active="settings",
            section="status",
            notice=notice,
            status_code=status_code,
            summary=_summary(found),
            rows=[_row(check, today) for check in found],
            groups=_recent(today),
            shown=RECENT_RUNS,
        )

    @app.get("/settings/status")
    def status_page(request: Request, saved: str = "") -> HTMLResponse:
        """Every check, Run checks now, and the recent runs. ?saved= is one code from NOTICES."""
        return page(request, notice=NOTICES.get(saved, ""))

    @app.post("/settings/status/run")
    async def run_checks(request: Request) -> Response:
        """Run the catch-up once for the open book (feeds that are due, the score history, the day's
        follow-ups and suggestions), then back to the page. A failure is in the run log, so the
        page only says that the checks ran."""
        try:
            await run_in_threadpool(service.run_checks_now)
        except Exception as exc:  # the page still shows what the run log holds
            _log.warning("Run checks now didn't finish (%s).", type(exc).__name__)
        return RedirectResponse(f"{STATUS_PATH}?saved=ran", status_code=303)
