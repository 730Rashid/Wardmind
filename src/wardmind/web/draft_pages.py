"""Email drafts: /accounts/{id}/draft, built from templates and optionally polished by AI
(core/drafts.py). Never sends. Workstream B.

CONTRACT STUB: registered by create_app (web/app.py) through register(). `render` is app._render
(the shared layout and context every page needs); `templates` is the app's Jinja2Templates, for
partials and for Jinja globals. Cross-site writes never reach these routes (same_origin_writes in
app.py), and the session check and lock gate run first.

The page shows the template draft at once, with no AI. The AI wording is a POST that answers with
the page again (nothing is saved, so there is no redirect to send it back to). Demo accounts never
reach an AI: their drafts are always the template, the same rule as demo summaries.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from wardmind.core import contacts, drafts, secrets, service, summaries
from wardmind.core.models import ScoredAccount, SignalKind
from wardmind.core.settings import AIProvider, DataSource, Settings, load_settings

Render = Callable[..., HTMLResponse]

BACK_TODAY = "today"
BACK_ACCOUNT = "account"


def _quoted(account_id: str) -> str:
    """An account id as a URL path segment."""
    return quote(account_id, safe="")


def _account(account_id: str) -> ScoredAccount | None:
    """The account as the book shows it right now (hidden accounts are not shown), or None."""
    return service.find_account(service.get_book(), account_id)


def _kind_or_none(value: str) -> drafts.DraftKind | None:
    """The kind named by `value`, or None when it names none."""
    try:
        return drafts.DraftKind(value)
    except ValueError:
        return None


def _back(value: str) -> str:
    """'today' when the page was opened from Today, else 'account'. Nothing else is a target."""
    return BACK_TODAY if value == BACK_TODAY else BACK_ACCOUNT


def _default_kind(account_id: str, item: ScoredAccount) -> drafts.DraftKind:
    """The kind the page offers first for this account (drafts.default_kind)."""
    latest = service.latest_book_signals().get(account_id, {})
    reading = latest.get(SignalKind.CHAMPION_LEFT)
    return drafts.default_kind(
        item.motion.value,
        [reason.kind.value for reason in item.reasons],
        champion_left=reading is not None and reading.value >= 1.0,
    )


def _recipient(account_id: str) -> contacts.Contact | None:
    """The champion when there is an active one, else the first active contact, else None."""
    active = [
        person
        for person in service.account_contacts(account_id)
        if person.status is contacts.ContactStatus.ACTIVE
    ]
    for person in active:
        if person.champion:
            return person
    return active[0] if active else None


def _first_name(person: contacts.Contact | None) -> str:
    """The first word of the contact's name, or "" when there is no contact."""
    words = person.name.split() if person is not None else []
    return words[0] if words else ""


def _template(
    account_id: str, item: ScoredAccount, kind: drafts.DraftKind, settings: Settings
) -> tuple[drafts.Draft, contacts.Contact | None]:
    """The template draft for this account and kind, and the contact it greets."""
    person = _recipient(account_id)
    draft = drafts.template_draft(
        item,
        kind,
        first_name=_first_name(person),
        today=date.today(),
        your_name=settings.your_name,
    )
    return draft, person


def _ai_offer(settings: Settings) -> tuple[bool, str]:
    """Whether the page offers AI wording, and when it doesn't, the plain-English line that says
    where to turn it on. Reads the settings and whether a key is saved; calls no AI."""
    if not settings.ai_drafts:
        return False, (
            "AI wording is off. Turn it on in Settings > Assistant to improve the wording with a "
            "model on this computer."
        )
    if settings.source is DataSource.DEMO:
        return False, (
            "Demo accounts always use the template, so nothing is sent. AI wording works on your "
            "own accounts."
        )
    if settings.ai_provider is AIProvider.LOCAL:
        if not settings.local_ai_model:
            return False, "Choose the model on this computer first, in Settings > Data & privacy."
        return True, ""
    if not settings.ai_drafts_cloud:
        return False, (
            "AI wording with a model online is off. Turn it on in Settings > Assistant, or use a "
            "model on this computer."
        )
    if secrets.get_openrouter_key() is None:
        return False, "Add your OpenRouter API key in Settings > Data & privacy first."
    return True, ""


def _mailto(email: str, subject: str, body: str) -> str:
    """A mailto: link that opens the CSM's email app with the draft filled in. The address is
    included only when it is a valid contact email. Everything but the @ is percent-encoded, so
    the text can't add a bcc or cc to the link. Line breaks become CRLF, as mail apps expect."""
    address = ""
    if email:
        try:
            checked = contacts.validate(contacts.ContactInput(name="Contact", email=email))
        except ValueError:
            checked = None
        if checked is not None:
            address = quote(checked.email, safe="@")
    subject_part = quote(subject, safe="")
    body_part = quote(body.replace("\n", "\r\n"), safe="")
    return "mailto:" + address + "?subject=" + subject_part + "&body=" + body_part


def _to_line(person: contacts.Contact | None) -> str:
    """Who the draft is for, in one line: name, title and email as far as they are known."""
    if person is None:
        return "No contact on this account yet. Add one on the account page to greet them by name."
    parts = [person.name]
    if person.title:
        parts.append(person.title)
    if person.email:
        parts.append(person.email)
    return ", ".join(parts)


def _run_polish(draft: drafts.Draft, model: str, allow_training: bool) -> drafts.Draft:
    """The AI's wording of the draft, or the template with the reason. Blocking: call it in a
    thread. The client is closed here, whatever the answer."""
    try:
        client = service.ai_client()
    except (summaries.SummaryError, ValueError):
        return draft.model_copy(update={"fallback_reason": drafts.UNREACHABLE_REASON})
    try:
        return drafts.polish(draft, client, model=model, allow_training=allow_training)
    finally:
        client.close()


async def _posted(request: Request) -> dict[str, str]:
    """The text fields of a posted form."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this module's routes to the app."""

    def not_found(request: Request, account_id: str) -> HTMLResponse:
        return render(
            request,
            "not_found.html",
            page="Account not found",
            status_code=404,
            message=f"No account with id {account_id}.",
            book=True,
        )

    def show(
        request: Request,
        item: ScoredAccount,
        draft: drafts.Draft,
        person: contacts.Contact | None,
        *,
        back: str,
        offer: bool,
        offer_note: str,
        training_model: str = "",
        training_refused: bool = False,
        training_text: str = "",
    ) -> HTMLResponse:
        quoted = _quoted(item.account.id)
        kinds = [
            {
                "label": kind.label,
                "href": (
                    f"/accounts/{quoted}/draft?" + urlencode({"kind": kind.value, "back": back})
                ),
                "current": kind is draft.kind,
            }
            for kind in drafts.DraftKind
        ]
        if back == BACK_TODAY:
            back_href, back_label = "/today", "Back to Today"
        else:
            back_href, back_label = f"/accounts/{quoted}", "Back to the account"
        made_by = draft.made_by if draft.made_by != drafts.TEMPLATE else ""
        return render(
            request,
            "accounts/draft.html",
            page="Draft an email",
            active="book",
            book=True,
            account_name=item.account.name,
            draft=draft,
            kinds=kinds,
            back=back,
            back_href=back_href,
            back_label=back_label,
            to_line=_to_line(person),
            mailto=_mailto(person.email if person is not None else "", draft.subject, draft.body),
            offer=offer,
            offer_note=offer_note,
            polish_href=f"/accounts/{quoted}/draft/polish",
            training_model=training_model,
            training_refused=training_refused,
            training_text=training_text,
            made_by=made_by,
        )

    @app.get("/accounts/{account_id}/draft")
    def draft_page(
        request: Request, account_id: str, kind: str = "", back: str = ""
    ) -> HTMLResponse:
        """The email draft for one account: the template, in the kind asked for (else the kind
        the account suggests). An unknown kind gets the suggested one. Nothing is sent or saved."""
        item = _account(account_id)
        if item is None:
            return not_found(request, account_id)
        settings = load_settings()
        chosen = _kind_or_none(kind) or _default_kind(account_id, item)
        draft, person = _template(account_id, item, chosen, settings)
        offer, note = _ai_offer(settings)
        training = (service.ai_training_model() or "") if offer else ""
        return show(
            request,
            item,
            draft,
            person,
            back=_back(back),
            offer=offer,
            offer_note=note,
            training_model=training,
        )

    @app.post("/accounts/{account_id}/draft/polish")
    async def polish_draft(request: Request, account_id: str) -> Response:
        """Improve the wording of this draft with the AI, when Settings allow it. The page comes
        back with the result: the AI's wording, or the template and the reason it wasn't used.
        A model online that trains on data asks first, as summaries do (without JavaScript, the
        page offers the proceed button)."""
        fields = await _posted(request)
        item = await run_in_threadpool(_account, account_id)
        if item is None:
            return await run_in_threadpool(not_found, request, account_id)
        settings = await run_in_threadpool(load_settings)
        kind = _kind_or_none(fields.get("kind", ""))
        if kind is None:
            kind = await run_in_threadpool(_default_kind, account_id, item)
        draft, person = await run_in_threadpool(_template, account_id, item, kind, settings)
        back = _back(fields.get("back", ""))
        offer, note = await run_in_threadpool(_ai_offer, settings)
        if not offer:
            return show(request, item, draft, person, back=back, offer=False, offer_note=note)
        allow = fields.get("allow_training") == "1"
        training = (await run_in_threadpool(service.ai_training_model)) or ""
        refusal = await run_in_threadpool(service.training_problem, allow)
        if refusal is not None:
            return show(
                request,
                item,
                draft,
                person,
                back=back,
                offer=True,
                offer_note="",
                training_model=training,
                training_refused=True,
                training_text=refusal.message,
            )
        local = settings.ai_provider is AIProvider.LOCAL
        model = settings.local_ai_model if local else settings.ai_model
        polished = await run_in_threadpool(_run_polish, draft, model, allow)
        return show(
            request,
            item,
            polished,
            person,
            back=back,
            offer=True,
            offer_note="",
            training_model=training,
        )
