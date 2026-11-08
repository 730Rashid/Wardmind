"""Contacts and client updates in the web: the values the account page, the review page and the
client update set-up page print.

Each function turns core objects (contacts.Contact, client_updates.Review, ...) into plain values,
so no template reads a core object or sees a None. Client text (a name, a note, an email) is shown
as text only. The one link made from contact text is a mailto: for an email that
contacts.validate accepts, and the template escapes it like any other attribute.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import quote, urlencode

from cs_brief.core import client_updates, contacts
from cs_brief.core.client_updates import ClientUpdate, Review, UpdateKind, UpdateStatus
from cs_brief.core.contacts import Contact, ContactInput, ContactStatus
from cs_brief.core.feeds import catalog
from cs_brief.core.feeds.models import FeedRole
from cs_brief.core.store import ConnectionConfig, ConnectionKind, LocalStore
from cs_brief.web import feed_pages, formatting

FORM = "form"  # the error key for a message about the whole form, not one field

# The icon of each kind of update (an icon id in static/icons.svg).
KIND_ICON: dict[UpdateKind, str] = {
    UpdateKind.ADD: "person-plus",
    UpdateKind.LEFT: "person-minus",
    UpdateKind.CHANGE: "edited",
    UpdateKind.OTHER: "note",
}
# The provider cards the set-up page offers, in its order, each with the role it needs.
_SETUP_PROVIDERS = ("file", "airtable", "google_sheets", "link")


@dataclass(frozen=True)
class FormRefusal:
    """A form on the account page that was refused: which form it was, the contact it was for
    (contact forms only), the values as typed (never a secret) and the messages by field, with
    FORM for a message about the whole form."""

    form: str  # "add", "edit", "left", "active" (undo left), "on" (turn client updates on)
    contact_id: str = ""
    values: Mapping[str, str] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContactRow:
    """One contact on the account page, with the values of its edit and mark-as-left forms."""

    id: str
    name: str
    title: str
    email: str  # as shown; "" when none
    mailto: str  # the email when contacts.validate accepts it, else ""
    main: bool  # the main contact (champion) and still active
    left: bool
    left_label: str  # "Left 3 November", or "" for an active contact
    edit_values: dict[str, str]  # name, title, email as the edit form shows them
    edit_main: bool
    edit_errors: dict[str, str]
    edit_open: bool  # the edit form was refused, so it opens with its messages
    left_on: str  # ISO date the mark-as-left box starts with: today
    left_errors: dict[str, str]
    left_open: bool
    row_error: str  # undo left refused, e.g. the email is now taken


@dataclass(frozen=True)
class ContactsSection:
    """The Contacts section: its rows, and the add form's values and messages."""

    editable: bool  # False in demo mode: the section is read-only
    rows: list[ContactRow]
    add_open: bool
    add_values: dict[str, str]
    add_errors: dict[str, str]


@dataclass(frozen=True)
class UpdatesBox:
    """The Client updates box in the account page's aside, for one account in Your data."""

    on: bool
    agreed: str  # "Agreed with Jo Smith on 3 November.", or "" when off
    link: str  # the client's own link, or "" when none can be shown
    form_saved: bool  # False: the form isn't set up, so the box says so instead of a link
    pending: int  # this client's updates waiting for review
    agreed_with: str  # the "Who agreed" box as it shows
    agreed_on: str  # ISO date the "Agreed on" box shows
    errors: dict[str, str]


@dataclass(frozen=True)
class UpdateCard:
    """One pending update on the review page."""

    id: str
    account_id: str  # quoted for a URL path
    account_name: str
    kind: str  # UpdateKind value, for the tag's colour class
    kind_label: str
    icon: str
    sent: str  # "Sent by Dana Whitfield on 3 November." or "Sent on 3 November."
    note: str
    adds: bool  # ADD and CHANGE: the editable contact fields
    is_left: bool
    is_change: bool
    is_other: bool
    name: str
    title: str
    email: str
    main: bool
    about: str  # LEFT and CHANGE: the matched contact, "Priya Shah, VP Operations", or ""
    about_id: str  # the matched contact's id, sent as the hidden contact_id, or ""
    candidates: list[tuple[str, str, bool]]  # (id, label, selected) when nothing matched
    left_on: str  # ISO date, LEFT only
    accept_label: str  # "Accept", or "Mark as read" for OTHER
    error: str


@dataclass(frozen=True)
class UpdateFeed:
    """A CLIENT_UPDATES connection: where its responses come from and how its last read went."""

    name: str
    source: str  # "Link to a file. Brings in client updates."
    status: str  # "Synced 5 minutes ago."
    error: str  # the feed's last error, or ""


def errors_for(message: str) -> dict[str, str]:
    """The contact form's message keyed by the field it is about. Email, job title and name are
    the only fields a contact's validation names."""
    lowered = message.casefold()
    if "email" in lowered:
        key = "email"
    elif "job title" in lowered:
        key = "title"
    elif "name" in lowered:
        key = "name"
    else:
        key = FORM
    return {key: message}


def opt_in_errors(message: str) -> dict[str, str]:
    """The "turn on" form's message keyed by its field."""
    if "date they agreed" in message:
        return {"agreed_on": message}
    if "person who agreed" in message:
        return {"agreed_with": message}
    return {FORM: message}


def contact_typed(fields: Mapping[str, str]) -> dict[str, str]:
    """The contact form's posted values, as they are echoed back on a refusal."""
    return {
        "name": fields.get("name", ""),
        "title": fields.get("title", ""),
        "email": fields.get("email", ""),
        "champion": "1" if fields.get("champion") == "1" else "",
    }


def contact_input(typed: Mapping[str, str]) -> ContactInput:
    """The core's input for a contact form's posted values (validated by the core)."""
    return ContactInput(
        name=typed.get("name", ""),
        title=typed.get("title", ""),
        email=typed.get("email", ""),
        champion=typed.get("champion") == "1",
    )


def refused_values(
    refusal: FormRefusal | None, form: str, contact_id: str = ""
) -> tuple[dict[str, str], dict[str, str]]:
    """The values and messages of one form, when it is the one refused; empty otherwise."""
    if refusal is None or refusal.form != form or refusal.contact_id != contact_id:
        return {}, {}
    return dict(refusal.values), dict(refusal.errors)


def _mailto(email: str) -> str:
    """The email when contacts.validate accepts it (it is linked), else ""."""
    if not email:
        return ""
    try:
        return contacts.validate(ContactInput(name="Contact", email=email)).email
    except ValueError:
        return ""


def _label(contact: Contact) -> str:
    """ "Priya Shah, VP Operations", or the name alone when there is no job title."""
    return f"{contact.name}, {contact.title}" if contact.title else contact.name


def contacts_section(
    found: list[Contact],
    *,
    editable: bool,
    today: date,
    refusal: FormRefusal | None,
) -> ContactsSection:
    """The section for an account's contacts (active first, as the core sorts them). A refused
    form comes back with its values and messages, and its disclosure open."""
    rows: list[ContactRow] = []
    for contact in found:
        edit_values, edit_errors = refused_values(refusal, "edit", contact.id)
        left_values, left_errors = refused_values(refusal, "left", contact.id)
        _, active_errors = refused_values(refusal, "active", contact.id)
        left = contact.status is ContactStatus.LEFT
        if edit_values:
            shown = {
                "name": edit_values.get("name", ""),
                "title": edit_values.get("title", ""),
                "email": edit_values.get("email", ""),
            }
            edit_main = edit_values.get("champion") == "1"
        else:
            shown = {"name": contact.name, "title": contact.title, "email": contact.email}
            edit_main = contact.champion
        left_on = left_values.get("left_on", today.isoformat())
        rows.append(
            ContactRow(
                id=contact.id,
                name=contact.name,
                title=contact.title,
                email=contact.email,
                mailto=_mailto(contact.email),
                main=contact.champion and not left,
                left=left,
                left_label=(
                    f"Left {formatting.day_label(contact.left_on, today)}"
                    if left and contact.left_on is not None
                    else ("Left" if left else "")
                ),
                edit_values=shown,
                edit_main=edit_main,
                edit_errors=edit_errors,
                edit_open=bool(edit_values or edit_errors),
                left_on=left_on,
                left_errors=left_errors,
                left_open=bool(left_values or left_errors),
                row_error=active_errors.get(FORM, ""),
            )
        )
    add_open = refusal is not None and refusal.form == "add"
    add_values, add_errors = refused_values(refusal, "add")
    return ContactsSection(
        editable=editable,
        rows=rows,
        add_open=add_open,
        add_values=add_values,
        add_errors=add_errors,
    )


def updates_box(
    store: LocalStore,
    account_id: str,
    *,
    today: date,
    refusal: FormRefusal | None,
) -> UpdatesBox:
    """The Client updates box for one account: whether it is on, the client's link, and what is
    waiting. The turn-on form's values and messages come back when that form was refused."""
    data = client_updates.load(store)
    consent = data.consents.get(account_id)
    values, errors = refused_values(refusal, "on")
    agreed = ""
    if consent is not None:
        day = formatting.day_label(consent.agreed_on, today)
        agreed = f"Agreed with {consent.agreed_with} on {day}."
    return UpdatesBox(
        on=consent is not None,
        agreed=agreed,
        link=client_updates.update_link(data, account_id) or "",
        form_saved=bool(data.settings.form_link),
        pending=pending_for_account(store, account_id),
        agreed_with=values.get("agreed_with", ""),
        agreed_on=values.get("agreed_on", today.isoformat()),
        errors=errors,
    )


def gone_updates(store: LocalStore, book_ids: set[str]) -> list[ClientUpdate]:
    """The PENDING updates of accounts that still have a consent but are no longer in the book
    (hidden, or removed with their connection). The badge counts them; the review page offers to
    discard them. `book_ids` is the Your data book's account ids."""
    data = client_updates.load(store)
    return [
        update
        for update in data.updates
        if update.status is UpdateStatus.PENDING
        and update.account_id in data.consents
        and update.account_id not in book_ids
    ]


def gone_text(count: int) -> str:
    """The review page's notice about updates for accounts that left the book."""
    if count == 1:
        return "1 update is for an account no longer in your book."
    return f"{count} updates are for accounts no longer in your book."


def discarded_text(count: int) -> str:
    """The notice after Discard them: how many were discarded."""
    if count == 0:
        return "Nothing was discarded."
    return "Discarded 1 update." if count == 1 else f"Discarded {count} updates."


def pending_for_account(store: LocalStore, account_id: str) -> int:
    """How many updates from this account are waiting for review."""
    return sum(
        1
        for update in client_updates.load(store).updates
        if update.status is UpdateStatus.PENDING and update.account_id == account_id
    )


def update_typed(fields: Mapping[str, str]) -> dict[str, str]:
    """The review card's posted values, as they are echoed back on a refusal."""
    typed = contact_typed(fields)
    typed["contact_id"] = fields.get("contact_id", "")
    typed["left_on"] = fields.get("left_on", "")
    return typed


def update_values(kind: UpdateKind, typed: Mapping[str, str]) -> ContactInput | None:
    """What accept applies for ADD and CHANGE: the confirmed values. None for the other kinds."""
    if kind not in (UpdateKind.ADD, UpdateKind.CHANGE):
        return None
    return contact_input(typed)


def update_card(
    review: Review,
    *,
    today: date,
    typed: Mapping[str, str] | None = None,
    error: str = "",
) -> UpdateCard:
    """One pending update as its card. The fields start with the client's values (a CHANGE's blank
    ones with the contact's current values); typed values, from a refused accept, replace them."""
    update = review.update
    target = review.target
    kind = update.kind
    when = update.submitted_at or update.received_at
    day = formatting.day_label(when.date(), today)
    sent = f"Sent by {update.submitted_by} on {day}." if update.submitted_by else f"Sent on {day}."

    is_change = kind is UpdateKind.CHANGE
    fallback = target if is_change else None
    name = update.name or (fallback.name if fallback else "")
    title = update.title or (fallback.title if fallback else "")
    email = update.email or (fallback.email if fallback else "")
    if update.champion is not None:
        main = update.champion
    else:
        main = fallback.champion if fallback else False
    left_default = (update.submitted_at.date() if update.submitted_at else today).isoformat()
    values = dict(typed or {})
    if typed is not None:
        name = values.get("name", "")
        title = values.get("title", "")
        email = values.get("email", "")
        main = values.get("champion") == "1"

    matched = kind in (UpdateKind.LEFT, UpdateKind.CHANGE) and target is not None
    candidates: list[tuple[str, str, bool]] = []
    if kind in (UpdateKind.LEFT, UpdateKind.CHANGE) and not matched:
        chosen = values.get("contact_id", "")
        candidates = [
            (contact.id, _label(contact), contact.id == chosen) for contact in review.candidates
        ]
    return UpdateCard(
        id=update.id,
        account_id=quote(update.account_id, safe=""),
        account_name=review.account.name,
        kind=kind.value,
        kind_label=kind.label,
        icon=KIND_ICON[kind],
        sent=sent,
        note=update.note,
        adds=kind in (UpdateKind.ADD, UpdateKind.CHANGE),
        is_left=kind is UpdateKind.LEFT,
        is_change=is_change,
        is_other=kind is UpdateKind.OTHER,
        name=name,
        title=title,
        email=email,
        main=main,
        about=_label(target) if matched and target is not None else "",
        about_id=target.id if matched and target is not None else "",
        candidates=candidates,
        left_on=values.get("left_on", left_default),
        accept_label="Mark as read" if kind is UpdateKind.OTHER else "Accept",
        error=error,
    )


def done_notice(done: str, *, account_name: str, contact: Contact | None, risk: bool) -> str:
    """What the review page says after an accept or reject, from the code the redirect carries.
    The names come from the store, never from the URL. `risk`: the contact that left was the last
    main contact, so the Champion left signal now applies."""
    if done == "reject":
        return "Rejected."
    if done == "other":
        return "Marked as read."
    if done == "checked":
        return "Checked for new responses."
    if contact is None or not account_name:
        return "Accepted." if done in ("add", "left", "change") else ""
    if done == "add":
        return f"Added {contact.name} to {account_name}."
    if done == "left":
        sentence = f"Marked {contact.name} as left at {account_name}."
        if risk:
            sentence += " Main contact left is now a risk signal."
        return sentence
    if done == "change":
        return f"Updated {contact.name} at {account_name}."
    return ""


def brought_in(store: LocalStore, config: ConnectionConfig) -> int:
    """How many new updates a CLIENT_UPDATES feed brought in with its last sync: the updates read
    at the moment of that sync. 0 for any other connection, or before the first sync."""
    feed = config.feed
    if feed is None or feed.role is not FeedRole.CLIENT_UPDATES:
        return 0
    stamp = feed.status.last_synced_at
    if stamp is None:
        return 0
    return sum(1 for update in client_updates.load(store).updates if update.received_at == stamp)


def updates_word(count: int) -> str:
    """'1 new update' or '3 new updates'."""
    return f"{count} new {'update' if count == 1 else 'updates'}"


def update_feeds(store: LocalStore, now: datetime) -> list[UpdateFeed]:
    """The CLIENT_UPDATES connections, in the order the store keeps them."""
    lines: list[UpdateFeed] = []
    for config in store.connections():
        feed = config.feed
        if config.kind is not ConnectionKind.FEED or feed is None:
            continue
        if feed.role is not FeedRole.CLIENT_UPDATES:
            continue
        lines.append(
            UpdateFeed(
                name=config.name,
                source=feed_pages.feed_source(feed),
                status=f"{feed_pages.synced_text(feed.status.last_synced_at, now)}.",
                error=feed.status.last_error or "",
            )
        )
    return lines


def last_read(store: LocalStore) -> datetime | None:
    """The newest sync of any CLIENT_UPDATES connection, or None."""
    stamps = [
        config.feed.status.last_synced_at
        for config in store.connections()
        if config.feed is not None
        and config.feed.role is FeedRole.CLIENT_UPDATES
        and config.feed.status.last_synced_at is not None
    ]
    return max(stamps) if stamps else None


def read_text(stamp: datetime | None, now: datetime) -> str:
    """ "Responses last read 5 minutes ago." for the review page's header."""
    if stamp is None:
        return "Responses haven't been read yet."
    relative = feed_pages.synced_text(stamp, now).removeprefix("Synced ")
    return f"Responses last read {relative}."


def filter_query(chosen: set[UpdateKind]) -> str:
    """The review page's kind filter as a query string, so a form keeps it on the way back."""
    return urlencode([("kind", kind.value) for kind in UpdateKind if kind in chosen])


def kind_options(reviews: list[Review], chosen: set[UpdateKind]) -> list[dict[str, object]]:
    """The filter's checkboxes: each kind, how many are waiting, its icon, and whether ticked."""
    totals = Counter(review.update.kind for review in reviews)
    return [
        {
            "value": kind.value,
            "label": kind.label,
            "icon": KIND_ICON[kind],
            "count": totals.get(kind, 0),
            "checked": kind in chosen,
        }
        for kind in UpdateKind
    ]


def setup_providers() -> list[tuple[str, str]]:
    """(link, name) for each provider the set-up page offers, with the role it needs."""
    pairs: list[tuple[str, str]] = []
    for provider_id in _SETUP_PROVIDERS:
        info = catalog.provider_info(provider_id)
        name = info.name if info is not None else provider_id
        pairs.append((f"/connections/add/{provider_id}?role=client_updates", name))
    return pairs


def setup_questions() -> list[client_updates.Question]:
    """The questions the form must ask, in order. The timestamp is added by the form tool."""
    return [question for question in client_updates.QUESTIONS if question.key != "submitted_at"]


def account_path(account_id: str) -> str:
    """An account id as a URL path segment."""
    return quote(account_id, safe="")
