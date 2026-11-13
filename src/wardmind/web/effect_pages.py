"""Did it work?: what moved since each step the CSM took on a suggestion (core/effects.py).

Three places show it. Today asks about up to effects.MAX_REVIEW_CARDS steps that are due an answer
(today_section, a section under "Suggested for you"). The account page lists the account's recent
steps with what moved and any answer given (account_steps). Settings > Assistant totals the steps
by kind (summary_table). The core does the comparing and saves the answer; this module only turns
its values into the words the templates show. Every sentence says what moved since a step.

One write route is registered here: POST /steps/{id}/review records the CSM's answer. Sent by htmx
(HX-Request), it answers with the step's one-line result, or with the step again when the answer was
refused. Without JavaScript it redirects back with a fixed notice code (?journal=step-reviewed), or
shows a 422 page when refused. A failure reading the steps is logged by its exception type only, and
the section is left out, so Today always renders.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from wardmind.core import effects, proposals, service
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.models import ScoredAccount
from wardmind.core.proposals import Verdict
from wardmind.core.settings import load_settings
from wardmind.web import journal_pages

_log = logging.getLogger(__name__)

Render = Callable[..., HTMLResponse]

MAX_ACCOUNT_STEPS = 10  # the account page lists this many steps, newest first
MAX_QUERY = 100  # characters kept from the find box (as today_pages does)
NOTICE = "step-reviewed"  # the ?journal= code after an answer sent without JavaScript
_UNNAMED = "This account"  # the name shown when the account is no longer in the book
_PICK_VERDICT = "Pick Helped, Didn't help or Too early."
_PICK_CHECK = "Pick a check of 14 or 30 days."  # the same words as core/effects.py
_LATER = "Noted. Wardmind asks again after the next check."
_LAST = "Noted."
_NOTED = {Verdict.HELPED: "Noted: it helped.", Verdict.DIDNT_HELP: "Noted: it didn't help."}


@dataclass(frozen=True)
class ReadingLine:
    label: str  # "Usage trend"
    before_text: str  # "Active users down 38% in 30 days", or "No reading"
    after_text: str


@dataclass(frozen=True)
class ReviewCard:
    """One step asking "Did it work?" on Today, about its latest READY check."""

    proposal_id: str
    account_id: str
    name: str
    title: str  # the suggestion's title
    done: str  # "Done 3 November"
    days: int  # the check the answers are for: 14 or 30
    sentence: (
        str  # the check's one sentence: "Better since: health At risk to Watch, risk 62 to 48."
    )
    direction: str  # the Direction value ("better", "same", "worse", "gone"), or "" for none
    readings: list[ReadingLine]
    error: str = ""  # why the last answer was refused, shown on the card


@dataclass(frozen=True)
class ReviewSection:
    """The "Did it work?" section: the first cards the find text allows, and how many more."""

    cards: list[ReviewCard]
    more: int  # steps beyond effects.MAX_REVIEW_CARDS, counted but not listed
    query: str  # the find text, cleaned; the answers' forms send it back

    @property
    def total(self) -> int:
        return len(self.cards) + self.more


@dataclass(frozen=True)
class StepView:
    """One step on an account page: what it was, what moved at each check, and the answer."""

    proposal_id: str
    title: str
    done: str  # "Done 3 November"
    checks: list[str]  # one sentence per check, in order (WAITING checks say when they fall due)
    said: str  # "You said: Helped", or ""
    needs_review: bool  # the review buttons show
    check_days: int  # the check the buttons answer; 0 when no check is READY yet
    error: str = ""


@dataclass(frozen=True)
class SummaryRow:
    """One row of Settings > Assistant's "What followed your steps" table."""

    label: str  # "Health dropped", ..., or "Suggested by Claude"
    steps: int
    checked: int  # steps with at least one READY check
    better: int  # of `checked`, by the direction of their latest READY check
    same: int
    worse: int
    helped: int  # the CSM's answers
    didnt_help: int


def _review_card(
    effect: effects.Effect, name: str, today: date, *, error: str = ""
) -> ReviewCard | None:
    """The card for a step with a READY check, or None when it has none."""
    latest = effect.latest_ready
    if latest is None:
        return None
    return ReviewCard(
        proposal_id=effect.proposal_id,
        account_id=effect.account_id,
        name=name,
        title=effect.title,
        done=journal_pages.done_label(effect.done_on, today),
        days=latest.days,
        sentence=latest.sentence,
        direction="" if latest.direction is None else latest.direction.value,
        readings=[
            ReadingLine(label=line.label, before_text=line.before_text, after_text=line.after_text)
            for line in latest.readings
        ],
        error=error,
    )


def _step_view(effect: effects.Effect, today: date, *, error: str = "") -> StepView:
    latest = effect.latest_ready
    said = "" if effect.review is None else f"You said: {effect.review.verdict.label}"
    return StepView(
        proposal_id=effect.proposal_id,
        title=effect.title,
        done=journal_pages.done_label(effect.done_on, today),
        checks=[_check_line(check, today) for check in effect.checks],
        said=said,
        needs_review=effect.needs_review(today),
        check_days=0 if latest is None else latest.days,
        error=error,
    )


def _check_line(check: effects.Check, today: date) -> str:
    """A check's sentence, led by which check it is once its day has come: "After 14 days. Better
    since: ..." (a check still to come already says its day)."""
    if check.due_on > today:
        return check.sentence
    return f"After {check.days} days. {check.sentence}"


def _wanted(effect: effects.Effect, names: Mapping[str, str], needle: str) -> bool:
    """Whether the step's account is in the book and its name holds the find text."""
    name = names.get(effect.account_id)
    return name is not None and (not needle or needle in name.casefold())


def today_section(book: list[ScoredAccount], today: date, query: str = "") -> ReviewSection | None:
    """The section for Today, or None when suggestions are off or no step needs an answer (for the
    find text's accounts). Steps whose account is not in `book` are left out."""
    try:
        if not load_settings().suggestions:
            return None
        names = {scored.account.id: scored.account.name for scored in book}
        needle = query.casefold()
        due = [
            effect
            for effect in effects.to_review(service.step_effects(today), today)
            if _wanted(effect, names, needle)
        ]
        cards = [
            card
            for card in (
                _review_card(effect, names[effect.account_id], today)
                for effect in due[: effects.MAX_REVIEW_CARDS]
            )
            if card is not None
        ]
        if not cards:
            return None
        more = max(0, len(due) - effects.MAX_REVIEW_CARDS)
        return ReviewSection(cards=cards, more=more, query=query)
    except Exception as exc:  # a failed read must never break Today
        _log.warning("Steps weren't checked (%s).", type(exc).__name__)
        return None


def account_steps(account_id: str, today: date) -> list[StepView]:
    """The account's steps, newest first, at most MAX_ACCOUNT_STEPS. Empty when there are none,
    or when they can't be read."""
    try:
        found = service.step_effects(today, account_id)
        return [_step_view(effect, today) for effect in found[:MAX_ACCOUNT_STEPS]]
    except Exception as exc:  # a failed read must never break the account page
        _log.warning("Steps weren't read (%s).", type(exc).__name__)
        return []


def summary_table() -> list[SummaryRow]:
    """Settings > Assistant's table: one row per kind of step that has any, or [] when there are
    none or they can't be read."""
    try:
        rows = effects.summary(service.step_effects())
    except Exception as exc:  # a failed read must never break Settings
        _log.warning("Steps weren't summarised (%s).", type(exc).__name__)
        return []
    return [
        SummaryRow(
            label=row.label,
            steps=row.steps,
            checked=row.checked,
            better=row.better,
            same=row.same,
            worse=row.worse,
            helped=row.helped,
            didnt_help=row.didnt_help,
        )
        for row in rows
    ]


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this module's route to the app."""

    @app.post("/steps/{proposal_id}/review")
    async def review_step_route(request: Request, proposal_id: str) -> Response:
        fields = await _posted(request)
        return await run_in_threadpool(_review, request, render, templates, proposal_id, fields)


def _review(
    request: Request,
    render: Render,
    templates: Jinja2Templates,
    proposal_id: str,
    fields: Mapping[str, str],
) -> Response:
    today = date.today()
    query = _find_text(fields)
    back = "account" if fields.get("back") == "account" else "today"
    try:
        verdict = _verdict(fields)
        check_days = _check_days(fields)
        saved = service.review_step(proposal_id, verdict, check_days, now=datetime.now(UTC))
    except LookupError:
        return _unknown(request, render, templates, query)
    except ValueError as error:
        return _refused(request, render, templates, proposal_id, str(error), query, back, today)
    if _is_htmx(request):
        return _line(request, templates, _saved_line(verdict, check_days))
    if back == "account":
        href = f"/accounts/{quote(saved.account_id, safe='')}?{urlencode({'journal': NOTICE})}"
        return RedirectResponse(href, status_code=303)
    return RedirectResponse(_today_url(journal=NOTICE, q=query), status_code=303)


def _saved_line(verdict: Verdict, check_days: int) -> str:
    """What the step says in its place after an answer (the same wording as the notice)."""
    if verdict is Verdict.TOO_EARLY:
        return _LAST if check_days == effects.CHECK_DAYS[-1] else _LATER
    return _NOTED[verdict]


def _refused(
    request: Request,
    render: Render,
    templates: Jinja2Templates,
    proposal_id: str,
    message: str,
    query: str,
    back: str,
    today: date,
) -> Response:
    """The step again, with why the answer was refused. Without JavaScript it is a page of its own
    with status 422; with htmx it is the card (or the step on the account page), sent with 200 so
    the swap happens. A step that can't be found gets the message alone."""
    effect = _step_of(proposal_id, today)
    if _is_htmx(request):
        if effect is None:
            return _line(request, templates, message)
        if back == "account":
            step = _step_view(effect, today, error=message)
            return _partial(request, templates, "accounts/_step.html", {"step": step})
        card = _review_card(effect, _name_in_book(effect.account_id, today), today, error=message)
        if card is None:
            return _line(request, templates, message)
        context = {"card": card, "query": query, "logos": service.all_logos()}
        return _partial(request, templates, "partials/step_review_card.html", context)
    if back == "account" and effect is not None:
        href = f"/accounts/{quote(effect.account_id, safe='')}"
        label = "Back to the account"
    else:
        href, label = _today_url(q=query), "Back to Today"
    return render(
        request,
        "step_review_page.html",
        page="Did it work?",
        active="today" if back == "today" else "book",
        status_code=422,
        message=message,
        back_href=href,
        back_label=label,
    )


def _unknown(request: Request, render: Render, templates: Jinja2Templates, query: str) -> Response:
    """The suggestion is gone, or its id never existed."""
    if _is_htmx(request):
        return _line(request, templates, proposals.NOT_FOUND)
    return render(
        request,
        "not_found.html",
        page="Did it work?",
        status_code=404,
        message=proposals.NOT_FOUND,
        back_href=_today_url(q=query),
        back_label="Back to Today",
    )


def _step_of(proposal_id: str, today: date) -> effects.Effect | None:
    """The step with this suggestion's id, or None: not a step, gone, or the steps can't be read."""
    try:
        item = service.get_proposals().get(proposal_id)
        if item is None:
            return None
        found = service.step_effects(today, item.account_id)
    except Exception as exc:
        _log.warning("Steps weren't read (%s).", type(exc).__name__)
        return None
    return next((effect for effect in found if effect.proposal_id == proposal_id), None)


def _name_in_book(account_id: str, today: date) -> str:
    """The account's name as the book shows it today, or the fallback when it isn't there."""
    try:
        book = service.get_book(as_of=today)
    except ConnectorError:
        return _UNNAMED
    scored = service.find_account(book, account_id)
    return _UNNAMED if scored is None else scored.account.name


def _verdict(fields: Mapping[str, str]) -> Verdict:
    try:
        return Verdict(fields.get("verdict", ""))
    except ValueError:
        raise ValueError(_PICK_VERDICT) from None


def _check_days(fields: Mapping[str, str]) -> int:
    """The check the answer is for. Whether it is 14 or 30 is for the core to say."""
    try:
        return int(fields.get("check_days", "").strip())
    except ValueError:
        raise ValueError(_PICK_CHECK) from None


def _line(request: Request, templates: Jinja2Templates, message: str) -> HTMLResponse:
    """The step's place after an answer: one line of result (htmx swaps it for the card)."""
    return _partial(request, templates, "partials/step_review_done.html", {"message": message})


def _partial(
    request: Request, templates: Jinja2Templates, name: str, context: dict[str, object]
) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, context)
    response.headers["Vary"] = "HX-Request"
    return response


def _find_text(fields: Mapping[str, str]) -> str:
    """The find box's text, cleaned the way Today cleans it."""
    return " ".join(fields.get("q", "").split())[:MAX_QUERY]


def _today_url(**params: str) -> str:
    """/today with the non-empty parameters, e.g. ?journal=step-reviewed&q=acme."""
    kept = {name: value for name, value in params.items() if value}
    return f"/today?{urlencode(kept)}" if kept else "/today"


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _posted(request: Request) -> dict[str, str]:
    """The text fields of a posted form."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}
