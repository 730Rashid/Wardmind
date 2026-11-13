"""Suggested for you: the section at the top of Today, with Accept, Edit and Dismiss.

today_section() builds the section. today_pages.screen calls it for every Today page: it runs the
day's check (service.refresh_suggestions, once a day) and reads the open suggestions. A failure
there is logged by its exception type only and the section is left out, so Today always renders.

Two write routes are registered here: POST /suggestions/{id}/accept and POST
/suggestions/{id}/dismiss. Without JavaScript each returns to Today with a fixed notice code
(?journal=, as the journal forms do). Sent by htmx (HX-Request), each answers with the card's
one-line result, or with the card again when something refused it. Nothing a suggestion says
reaches the journal until Accept.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from wardmind.core import proposals, service
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.models import ScoredAccount
from wardmind.core.proposals import Proposal, ProposalKind
from wardmind.core.settings import load_settings
from wardmind.web import journal_pages

_log = logging.getLogger(__name__)

Render = Callable[..., HTMLResponse]

MAX_QUERY = 100  # characters kept from the find box (as today_pages does)
_PICK_DAY = "Pick a date, such as 2026-11-16."
_UNNAMED = "This account"  # the name shown when the account is no longer in the book
_DISMISSED_LINE = "Dismissed."
# The ?journal= notice codes journal_pages.NOTICES already has, for what Accept writes.
_NOTICE_CODES = {ProposalKind.FOLLOW_UP: "follow-up-added", ProposalKind.NOTE: "note-added"}


@dataclass(frozen=True)
class WhyLine:
    text: str
    observed: str  # the evidence's day, "3 Nov 2026"; "" when it has none


@dataclass(frozen=True)
class Card:
    """One suggestion as the card shows it. `text` and the date are what the Edit box holds."""

    id: str
    account_id: str
    name: str
    title: str
    source: str  # "Wardmind" or "Claude"
    when: str  # "Due today", "Until Fri 20 Nov", or "" for a note
    why: list[WhyLine]
    edit_text: bool  # the Edit box has a text field (a snooze keeps no text)
    multiline: bool  # the text is a note, so a text area
    text: str
    date_name: str  # the form field for the date: "due", "until", or "" when there is none
    date_label: str
    date_value: str  # ISO day for the date box, or ""
    draft_kind: str  # the DraftKind value for "Draft the email", or ""
    error: str = ""  # why the last action was refused, shown on the card


@dataclass(frozen=True)
class Section:
    """The "Suggested for you" section: the open cards the find box allows, and how many the
    day's check held back (0 when it didn't run today)."""

    cards: list[Card]
    query: str  # the find text, cleaned; the card's actions send it back so the list stays narrowed
    waiting: int


def card_for(
    item: Proposal,
    scored: ScoredAccount | None,
    today: date,
    *,
    error: str = "",
    typed: Mapping[str, str] | None = None,
) -> Card:
    """The card for one suggestion. `scored` is its account in the book, or None when it has
    left the book. `typed` holds what the CSM typed in a refused Edit box, shown as typed."""
    values = typed or {}
    name = scored.account.name if scored is not None else _UNNAMED
    why = [
        WhyLine(
            text=line.text,
            observed=journal_pages.short_date(line.observed) if line.observed else "",
        )
        for line in item.why
    ]
    due_value = item.due.isoformat() if item.due else ""
    until_value = item.until.isoformat() if item.until else ""
    if item.kind is ProposalKind.FOLLOW_UP:
        when = journal_pages.due_label(item.due or today, today)
        date_name, date_label = "due", "Due"
        date_value = values.get("due", due_value)
    elif item.kind is ProposalKind.SNOOZE:
        when = f"Until {journal_pages.short_day(item.until or today, today)}"
        date_name, date_label = "until", "Snooze until"
        date_value = values.get("until", until_value)
    else:
        when, date_name, date_label, date_value = "", "", "", ""
    return Card(
        id=item.id,
        account_id=item.account_id,
        name=name,
        title=item.title,
        source=item.source.label,
        when=when,
        why=why,
        edit_text=item.kind is not ProposalKind.SNOOZE,
        multiline=item.kind is ProposalKind.NOTE,
        text=values.get("text", item.text),
        date_name=date_name,
        date_label=date_label,
        date_value=date_value,
        draft_kind="" if item.draft_kind is None else item.draft_kind.value,
        error=error,
    )


def today_section(book: list[ScoredAccount], today: date, query: str = "") -> Section | None:
    """The section for Today, or None when suggestions are off or none are open (for the find
    text's accounts). Runs the day's check first (service.refresh_suggestions returns None when it
    already ran today, the book is locked or suggestions are off). Suggestions whose account is not
    in `book` are left out."""
    if not load_settings().suggestions:
        return None
    waiting = 0
    try:
        result = service.refresh_suggestions(today)
        if result is not None:
            waiting = result.waiting
    except Exception as exc:  # a failed check must never break Today
        _log.warning("Suggestions weren't checked (%s).", type(exc).__name__)
    try:
        open_items = service.get_proposals().open()
    except Exception as exc:
        _log.warning("Suggestions weren't read (%s).", type(exc).__name__)
        return None
    needle = query.casefold()
    in_book = {scored.account.id: scored for scored in book}
    cards: list[Card] = []
    for item in open_items:
        scored = in_book.get(item.account_id)
        if scored is None:
            continue
        if needle and needle not in scored.account.name.casefold():
            continue
        cards.append(card_for(item, scored, today))
    if not cards:
        return None
    return Section(cards=cards, query=query, waiting=waiting)


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this module's routes to the app."""

    @app.post("/suggestions/{proposal_id}/accept")
    async def accept_suggestion(request: Request, proposal_id: str) -> Response:
        fields = await _posted(request)
        return await run_in_threadpool(_accept, request, render, templates, proposal_id, fields)

    @app.post("/suggestions/{proposal_id}/dismiss")
    async def dismiss_suggestion(request: Request, proposal_id: str) -> Response:
        fields = await _posted(request)
        return await run_in_threadpool(_dismiss, request, render, templates, proposal_id, fields)


def _accept(
    request: Request,
    render: Render,
    templates: Jinja2Templates,
    proposal_id: str,
    fields: Mapping[str, str],
) -> Response:
    today = date.today()
    query = _find_text(fields)
    store = service.get_proposals()
    item = store.get(proposal_id)
    if item is None:
        return _unknown(request, render, templates, proposal_id, query)
    scored = _in_book(item.account_id, today)
    try:
        edits = _edits(fields)
        accepted = proposals.accept(
            store,
            service.get_journal(),
            proposal_id,
            now=datetime.now(UTC),
            today=today,
            edits=edits,
            health=None if scored is None else scored.health,
        )
    except LookupError:
        return _unknown(request, render, templates, proposal_id, query)
    except ValueError as error:
        card = card_for(item, scored, today, error=str(error), typed=fields)
        return _refused(request, render, templates, card, query)
    until = accepted.until
    if edits is not None and edits.until is not None:
        until = edits.until
    if _is_htmx(request):
        return _line(request, templates, _accepted_line(accepted.kind, until, today))
    if accepted.kind is ProposalKind.SNOOZE:
        params = {"journal": "snoozed", "day": until.isoformat() if until else ""}
    else:
        params = {"journal": _NOTICE_CODES[accepted.kind]}
    params["q"] = query
    return RedirectResponse(_today_url(**params), status_code=303)


def _dismiss(
    request: Request,
    render: Render,
    templates: Jinja2Templates,
    proposal_id: str,
    fields: Mapping[str, str],
) -> Response:
    today = date.today()
    query = _find_text(fields)
    store = service.get_proposals()
    item = store.get(proposal_id)
    if item is None:
        return _unknown(request, render, templates, proposal_id, query)
    try:
        proposals.dismiss(store, proposal_id, now=datetime.now(UTC))
    except LookupError:
        return _unknown(request, render, templates, proposal_id, query)
    except ValueError as error:
        card = card_for(item, _in_book(item.account_id, today), today, error=str(error))
        return _refused(request, render, templates, card, query)
    if _is_htmx(request):
        return _line(request, templates, _DISMISSED_LINE)
    return RedirectResponse(_today_url(q=query), status_code=303)


def _accepted_line(kind: ProposalKind, until: date | None, today: date) -> str:
    """What the card says in its place after Accept (the same as the Today notice)."""
    if kind is ProposalKind.SNOOZE:
        return f"Snoozed until {journal_pages.short_day(until, today)}." if until else "Snoozed."
    if kind is ProposalKind.NOTE:
        return "Added a note."
    return "Added a follow-up."


def _refused(
    request: Request, render: Render, templates: Jinja2Templates, card: Card, query: str
) -> Response:
    """The card again, with why the action was refused. Without JavaScript it is a page of its
    own with status 422 (and the typed values); with htmx it is the card, sent with 200 so the
    swap happens, since htmx doesn't swap an error response."""
    if _is_htmx(request):
        response = templates.TemplateResponse(
            request,
            "partials/suggestion_card.html",
            {"card": card, "query": query, "logos": service.all_logos()},
        )
        response.headers["Vary"] = "HX-Request"
        return response
    return render(
        request,
        "suggestion_page.html",
        page="Today",
        active="today",
        status_code=422,
        card=card,
        query=query,
    )


def _unknown(
    request: Request, render: Render, templates: Jinja2Templates, proposal_id: str, query: str
) -> Response:
    """The suggestion is gone (a stale page, or an id that never existed)."""
    if _is_htmx(request):
        return _line(request, templates, proposals.NOT_FOUND)
    return render(
        request,
        "not_found.html",
        page="Today",
        status_code=404,
        message=proposals.NOT_FOUND,
        back_href=_today_url(q=query),
        back_label="Back to Today",
    )


def _line(request: Request, templates: Jinja2Templates, message: str) -> HTMLResponse:
    """The card's place after an action: one line of result (htmx swaps it for the card)."""
    response = templates.TemplateResponse(
        request,
        "partials/suggestion_done.html",
        {"message": message},
    )
    response.headers["Vary"] = "HX-Request"
    return response


def _in_book(account_id: str, today: date) -> ScoredAccount | None:
    """The account as the book shows it today, or None (not in the book, or the book can't load)."""
    try:
        book = service.get_book(as_of=today)
    except ConnectorError:
        return None
    return service.find_account(book, account_id)


def _edits(fields: Mapping[str, str]) -> proposals.Edits | None:
    """What the Edit box changed, or None when the form sent no edit fields (the plain Accept)."""
    if not {"text", "due", "until"} & fields.keys():
        return None
    return proposals.Edits(
        text=fields.get("text"),
        due=_day_field(fields, "due"),
        until=_day_field(fields, "until"),
    )


def _day_field(fields: Mapping[str, str], name: str) -> date | None:
    """The date a date box holds, None when it is blank, or ValueError for a date that isn't one."""
    text = fields.get(name, "").strip()
    if not text:
        return None
    day = journal_pages.parse_day(text)
    if day is None:
        raise ValueError(_PICK_DAY)
    return day


def _find_text(fields: Mapping[str, str]) -> str:
    """The find box's text, cleaned the way Today cleans it."""
    return " ".join(fields.get("q", "").split())[:MAX_QUERY]


def _today_url(**params: str) -> str:
    """/today with the non-empty parameters, e.g. ?journal=follow-up-added&q=acme."""
    kept = {name: value for name, value in params.items() if value}
    return f"/today?{urlencode(kept)}" if kept else "/today"


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _posted(request: Request) -> dict[str, str]:
    """The text fields of a posted form."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}
