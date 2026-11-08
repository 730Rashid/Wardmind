"""View helpers for the data feed pages: the Add a connection catalog, the setup form, the matching
page and the feed rows on Settings > Connections.

These turn feed objects into plain values the templates print. Nothing here fetches, writes or
reads a key; every value a template shows is text this module has already made, so no template
ever sees a secret or a None.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from cs_brief.core import client_updates, importing
from cs_brief.core.feeds import catalog, signals
from cs_brief.core.feeds.models import (
    Category,
    FeedRole,
    FeedSettings,
    FieldKind,
    MatchBy,
    ProviderInfo,
    SetupField,
)
from cs_brief.core.feeds.setup import FeedPreview
from cs_brief.core.importing import FieldType
from cs_brief.core.store import ConnectionConfig, ConnectionKind
from cs_brief.web import formatting

HUBSPOT_ANCHOR = "/settings/connections#hubspot-access-title"
_SAVED_KEY_HELP = "Leave blank to keep the saved key."
_ROW_PREVIEW = 5  # unmatched rows the matching page lists; the rest are on Connections
_ISSUE_PREVIEW = 10  # the issues the matching page lists


# The catalog ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """One card of the Add a connection catalog: a built-in flow or a provider."""

    key: str  # "spreadsheet", "paste", "manual", "hubspot", or a provider id
    name: str
    by: str
    description: str
    mark: str  # 1-2 letters for the tile
    categories: tuple[Category, ...]
    href: str
    connected: bool = False


_BUILT_IN: tuple[Card, ...] = (
    Card(
        key="spreadsheet",
        name="Spreadsheet import",
        by="by cs-brief",
        description="A CSV, TSV or Excel file from this computer. You match its columns once, "
        "and importing it again updates the book.",
        mark="Sh",
        categories=(Category.FILES, Category.ACCOUNTS),
        href="/import?back=connections",
    ),
    Card(
        key="paste",
        name="Paste cells",
        by="by cs-brief",
        description="Copy cells from Excel or Google Sheets and paste them here. You match the "
        "columns on the next page.",
        mark="Pa",
        categories=(Category.ACCOUNTS,),
        href="/import?paste=1&back=connections",
    ),
    Card(
        key="manual",
        name="Add an account by hand",
        by="by cs-brief",
        description="Type in one account at a time. Good for a small book or a few extra accounts.",
        mark="Ac",
        categories=(Category.ACCOUNTS,),
        href="/accounts/new",
    ),
    Card(
        key="hubspot",
        name="HubSpot",
        by="by HubSpot",
        description="Companies, owners and support tickets from your HubSpot account, read-only. "
        "Needs a Service Key.",
        mark="HS",
        categories=(Category.ACCOUNTS, Category.TICKETS),
        href=HUBSPOT_ANCHOR,
    ),
)


_CLIENT_UPDATES_CARD = Card(
    key="client_updates",
    name="Client update form",
    by="by cs-brief",
    description="Clients who agreed tell you about new or departing contacts through one form. "
    "You review each change before it is applied.",
    mark="Cu",
    categories=(Category.ACCOUNTS, Category.FILES),
    href="/client-updates/setup",
)


def catalog_cards(configs: list[ConnectionConfig]) -> list[Card]:
    """The built-in cards, then one card per provider in catalog order. A card is "connected"
    when a connection of its kind exists: a HubSpot connection, a client update form (a feed with
    the CLIENT_UPDATES role), or a feed of its provider."""
    feed_providers = {config.feed.provider for config in configs if config.feed is not None}
    hubspot = any(config.kind is ConnectionKind.HUBSPOT for config in configs)
    updates = any(
        config.feed is not None and config.feed.role is FeedRole.CLIENT_UPDATES
        for config in configs
    )
    cards = [
        replace(card, connected=hubspot) if card.key == "hubspot" else card for card in _BUILT_IN
    ]
    cards.append(replace(_CLIENT_UPDATES_CARD, connected=updates))
    for info in catalog.INFOS:
        cards.append(
            Card(
                key=info.id,
                name=info.name,
                by=info.by,
                description=info.description,
                mark=info.mark,
                categories=tuple(info.categories),
                href=f"/connections/add/{info.id}",
                connected=info.id in feed_providers,
            )
        )
    return cards


def filter_cards(cards: list[Card], chosen: set[Category], q: str) -> list[Card]:
    """The cards in any chosen category (all, when none is chosen) whose name, "by" line or
    description holds the search text, ignoring case. Catalog order is kept."""
    needle = q.strip().casefold()
    shown = []
    for card in cards:
        if chosen and chosen.isdisjoint(card.categories):
            continue
        haystack = f"{card.name} {card.by} {card.description}".casefold()
        if needle and needle not in haystack:
            continue
        shown.append(card)
    return shown


def category_options(cards: list[Card], chosen: set[Category]) -> list[dict[str, object]]:
    """The sidebar's checkboxes: each category, how many cards carry it, and whether it's ticked."""
    return [
        {
            "value": category.value,
            "label": category.label,
            "count": sum(category in card.categories for card in cards),
            "checked": category in chosen,
        }
        for category in Category
    ]


def connections_word(count: int) -> str:
    """'Showing 1 way to connect' or 'Showing 6 ways to connect'."""
    return f"Showing {count} {'way' if count == 1 else 'ways'} to connect"


# Setup ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetupForm:
    """What the setup form shows: the values as typed, or as saved when changing a feed."""

    name: str
    role: str  # FeedRole value, or "" for the provider's first role
    schedule: str  # Schedule value, or "" for the first offered
    match_by: str  # MatchBy value, or "" for automatic
    values: dict[str, str]  # the non-secret SetupField values, keyed by field
    connection: str  # the feed being changed, or ""


@dataclass(frozen=True)
class FieldView:
    """One setup input, ready for the template. A SECRET field has no value to show."""

    key: str
    dom: str  # the input's id, e.g. "field-header-name"
    label: str
    kind: str  # FieldKind value
    required: bool
    help: str
    placeholder: str
    value: str
    choices: tuple[tuple[str, str, bool], ...]  # CHOICE only: (value, label, selected)
    error: str


def initial_setup(info: ProviderInfo, existing: ConnectionConfig | None) -> SetupForm:
    """The form a person starts with: the provider's defaults, or a feed's saved settings when
    its settings are being changed. A saved secret is never part of the form."""
    if existing is None or existing.feed is None:
        return SetupForm(
            name=info.name,
            role="",
            schedule="",
            match_by="",
            values={
                item.key: item.default for item in info.fields if item.kind is not FieldKind.SECRET
            },
            connection="",
        )
    feed = existing.feed
    return SetupForm(
        name=existing.name,
        role=feed.role.value,
        schedule=feed.schedule.value,
        match_by=feed.match_by.value,
        values={
            item.key: feed.settings.get(item.key, item.default)
            for item in info.fields
            if item.kind is not FieldKind.SECRET
        },
        connection=existing.id,
    )


def secret_key(info: ProviderInfo) -> str:
    """The key of the provider's SECRET field, or "" when it has none."""
    return next((item.key for item in info.fields if item.kind is FieldKind.SECRET), "")


def field_for_error(info: ProviderInfo, message: str) -> str:
    """Which part of the setup form a refusal from start_draft is about: a setup field's key,
    "name", "role" or "schedule", or "" when it belongs to the whole form."""
    if message.startswith("The address changed"):
        return secret_key(info)
    if message.startswith(("Give it a name", "A name can be")):
        return "name"
    if "can't be used for that" in message:
        return "role"
    if "sync schedules" in message:
        return "schedule"
    for item in info.fields:
        if item.label and item.label in message:
            return item.key
    if "web address" in message or "https://" in message:
        return next((item.key for item in info.fields if item.kind is FieldKind.URL), "")
    if any(word in message for word in ("key", "password", "token")):
        return secret_key(info)
    return ""


def _field_view(
    item: SetupField,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    *,
    editing: bool,
) -> FieldView:
    kind = item.kind
    value = "" if kind is FieldKind.SECRET else values.get(item.key, "")
    choices: tuple[tuple[str, str, bool], ...] = ()
    if kind is FieldKind.CHOICE:
        current = value or item.default
        choices = tuple((val, label, val == current) for val, label in item.choices)
    help_text = item.help
    if kind is FieldKind.SECRET and editing:
        help_text = f"{help_text} {_SAVED_KEY_HELP}".strip()
    return FieldView(
        key=item.key,
        dom=f"field-{item.key.replace('_', '-')}",
        label=item.label,
        kind=kind.value,
        required=item.required,
        help=help_text,
        placeholder=item.placeholder,
        value=value,
        choices=choices,
        error=errors.get(item.key, ""),
    )


def setup_fields(
    info: ProviderInfo,
    form: SetupForm,
    errors: Mapping[str, str],
    *,
    editing: bool,
) -> list[FieldView]:
    """The provider's SetupFields in order. When `editing`, a secret's help says that a blank box
    keeps the saved key."""
    return [_field_view(item, form.values, errors, editing=editing) for item in info.fields]


_ROLE_HELP: dict[FeedRole, str] = {
    FeedRole.CLIENT_UPDATES: "Each new response becomes an update for you to review.",
}


def role_options(info: ProviderInfo, chosen: str) -> list[dict[str, object]]:
    """The role radios, with the first role checked when nothing is chosen yet. Each carries the
    sentence the setup card shows under the radios for that role, or ""."""
    current = chosen or info.roles[0].value
    return [
        {
            "value": role.value,
            "label": role.label,
            "checked": role.value == current,
            "help": _ROLE_HELP.get(role, ""),
        }
        for role in info.roles
    ]


def preselect_role(form: SetupForm, info: ProviderInfo, role: str) -> SetupForm:
    """The form with the role a link asked for (?role=), when the provider offers it. Any other
    text leaves the form as it was."""
    offered = {item.value for item in info.roles}
    if role not in offered:
        return form
    return replace(form, role=role)


def schedule_options(info: ProviderInfo, chosen: str) -> list[dict[str, object]]:
    """The schedule choices the provider offers, with the first checked when nothing is chosen."""
    current = chosen or info.schedules[0].value
    return [
        {"value": schedule.value, "label": schedule.label, "checked": schedule.value == current}
        for schedule in info.schedules
    ]


def match_options(chosen: str) -> list[dict[str, object]]:
    """The "Match accounts by" choices, automatic first."""
    current = chosen or MatchBy.AUTO.value
    return [
        {"value": option.value, "label": option.label, "checked": option.value == current}
        for option in MatchBy
    ]


# Matching ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchRow:
    """One cs-brief field on the matching page."""

    index: int  # its position in importing.IMPORT_FIELDS: the select is column-<index>
    key: str
    label: str
    help: str
    required: bool
    selected: str  # the chosen column header, or ""
    example: str  # the first data row's cell in that column, or ""


def _update_rows(mapping: Mapping[str, str], table: importing.Table) -> list[MatchRow]:
    """One row per client update question, in the form's order. The index is the question's
    position, so the select is column-<index> (see app._submitted_mapping)."""
    first = table.rows[0] if table.rows else []
    rows: list[MatchRow] = []
    for index, question in enumerate(client_updates.QUESTIONS):
        header = mapping.get(question.key, "")
        example = ""
        if header and header in table.headers and first:
            example = first[table.headers.index(header)]
        rows.append(
            MatchRow(
                index=index,
                key=question.key,
                label=question.label,
                help=question.help,
                required=question.required,
                selected=header,
                example=example,
            )
        )
    return rows


def match_rows(
    role: FeedRole, mapping: Mapping[str, str], table: importing.Table
) -> list[MatchRow]:
    """The fields the matching page shows. An ACCOUNTS feed maps every import field. A SIGNALS
    feed maps its keys (ID, Website, Account name) first, then its signal fields. A CLIENT_UPDATES
    feed maps the client update questions."""
    if role is FeedRole.CLIENT_UPDATES:
        return _update_rows(mapping, table)
    indexed = list(enumerate(importing.IMPORT_FIELDS))
    if role is FeedRole.SIGNALS:
        key_order = {key: position for position, key in enumerate(signals.KEY_FIELDS)}
        indexed = [
            pair
            for pair in indexed
            if pair[1].key in key_order or pair[1].key.startswith("signal:")
        ]
        indexed.sort(key=lambda pair: (key_order.get(pair[1].key, len(key_order)), pair[0]))
    first = table.rows[0] if table.rows else []
    rows: list[MatchRow] = []
    for index, spec in indexed:
        header = mapping.get(spec.key, "")
        example = ""
        if header and header in table.headers and first:
            example = first[table.headers.index(header)]
        rows.append(
            MatchRow(
                index=index,
                key=spec.key,
                label=spec.label,
                help=spec.help,
                required=role is FeedRole.ACCOUNTS and spec.required,
                selected=header,
                example=example,
            )
        )
    return rows


def _date_columns(mapping: Mapping[str, str]) -> list[str]:
    """The headers mapped to a date field, each once. A client update form's timestamp counts too:
    it is the only date its matching page maps."""
    keys = [spec.key for spec in importing.IMPORT_FIELDS if spec.type is FieldType.DATE]
    keys.append("submitted_at")
    return list(dict.fromkeys(mapping[key] for key in keys if key in mapping))


def date_order_view(
    table: importing.Table, mapping: Mapping[str, str], chosen: str
) -> tuple[bool, str]:
    """(whether to ask day or month first, the choice that starts checked). The question is asked
    only when the mapped dates don't settle it, as on the import page."""
    columns = _date_columns(mapping)
    values = [row[table.headers.index(header)] for header in columns for row in table.rows]
    implied = importing.detect_date_order(values) if columns else None
    current = chosen if chosen in ("dmy", "mdy") else "dmy"
    return implied is None and bool(columns), current


@dataclass(frozen=True)
class Happens:
    """The "What will happen" panel as sentences. A blocker is shown by the page, not here."""

    headline: str  # "34 accounts ready." or "2 of 3 rows match an account, ..."
    skipped: str  # ACCOUNTS: "1 row will be skipped." or ""
    issues: tuple[tuple[int, str], ...]  # (row, message)
    unmatched_heading: str  # SIGNALS: "1 row didn't match an account:" or ""
    unmatched: tuple[tuple[int, str, str], ...]  # (row, key, reason)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _accounts(count: int) -> str:
    return f"{count} {_plural(count, 'account', 'accounts')}"


def _updates_happens(rows: int, preview: FeedPreview, ignored: int) -> Happens:
    """ "3 new updates from 2 clients. 1 row ignored: its code matches no client who opted in."
    `ignored` is what client_updates.ingest (save=False) reports as ignored, so rows already read
    before are not counted as ignored. A blocked preview says nothing of rows: the page's message
    covers it."""
    headline = ""
    if rows == 0:
        headline = "The form has no responses yet."
    elif preview.blocker is None:
        if preview.ready == 0:
            headline = "No new updates in these responses."
        else:
            headline = (
                f"{preview.ready} new {_plural(preview.ready, 'update', 'updates')} from "
                f"{preview.accounts} {_plural(preview.accounts, 'client', 'clients')}."
            )
    skipped = ""
    if rows and preview.blocker is None and ignored > 0:
        skipped = (
            f"{ignored} {_plural(ignored, 'row', 'rows')} ignored: its code matches no client who "
            "opted in."
        )
    return Happens(
        headline=headline,
        skipped=skipped,
        issues=(),
        unmatched_heading="",
        unmatched=(),
    )


def happens(role: FeedRole, rows: int, preview: FeedPreview, ignored: int = 0) -> Happens:
    """The panel's sentences for a preview. `ignored` is for a client update form only (the rows
    that will not become updates); the other roles read their counts from the preview."""
    if role is FeedRole.CLIENT_UPDATES:
        return _updates_happens(rows, preview, ignored)
    issues = tuple((issue.row, issue.message) for issue in preview.issues[:_ISSUE_PREVIEW])
    if role is FeedRole.ACCOUNTS:
        skipped = rows - preview.ready
        skipped_text = ""
        if skipped > 0:
            skipped_text = f"{skipped} {_plural(skipped, 'row', 'rows')} will be skipped."
        return Happens(
            headline=f"{_accounts(preview.ready)} ready.",
            skipped=skipped_text,
            issues=issues,
            unmatched_heading="",
            unmatched=(),
        )
    if rows == 0:
        headline = ""
    elif preview.ready == 0:
        headline = f"None of the {rows} rows matched an account."
    else:
        headline = (
            f"{preview.ready} of {rows} {_plural(rows, 'row', 'rows')} match an account, "
            f"adding readings to {_accounts(preview.accounts)}."
        )
    missed = rows - preview.ready
    heading = (
        f"{missed} {_plural(missed, 'row', 'rows')} didn't match an account:"
        if missed > 0 and rows
        else ""
    )
    unmatched = tuple(
        (item.row, item.key, item.reason) for item in preview.unmatched[:_ROW_PREVIEW]
    )
    return Happens(
        headline=headline,
        skipped="",
        issues=issues,
        unmatched_heading=heading,
        unmatched=unmatched,
    )


# Connections: the feed rows and their notices ---------------------------------------------------


def _count(amount: int, unit: str) -> str:
    return f"{amount} {unit if amount == 1 else unit + 's'}"


def synced_text(last_synced_at: datetime | None, now: datetime) -> str:
    """'Synced 5 minutes ago', 'Synced 3 days ago', or 'Not synced yet'. Both moments are naive
    UTC, as the feed status keeps them."""
    if last_synced_at is None:
        return "Not synced yet"
    minutes = int(max(0.0, (now - last_synced_at).total_seconds()) // 60)
    if minutes < 1:
        return "Synced just now"
    if minutes < 60:
        return f"Synced {_count(minutes, 'minute')} ago"
    hours = minutes // 60
    if hours < 24:
        return f"Synced {_count(hours, 'hour')} ago"
    return f"Synced {formatting.ago(hours // 24)}"


def provider_name(provider_id: str) -> str:
    """The catalog name of a provider, or its id when the catalog doesn't know it."""
    info = catalog.provider_info(provider_id)
    return info.name if info is not None else provider_id


def feed_source(feed: FeedSettings) -> str:
    """The line under a feed's name: 'Stripe. Adds signals to my accounts.'"""
    return f"{provider_name(feed.provider)}. {feed.role.label}."


def _tail(config: ConnectionConfig, brought: int) -> str:
    """'34 accounts got readings.' for a SIGNALS feed, '2 accounts were imported.' otherwise, and
    'Brought in 3 new updates.' for a client update form (`brought`: its last sync's count)."""
    feed = config.feed
    if feed is not None and feed.role is FeedRole.CLIENT_UPDATES:
        return f"Brought in {brought} {_plural(brought, 'new update', 'new updates')}."
    count = feed.status.accounts if feed is not None else 0
    if feed is not None and feed.role is FeedRole.SIGNALS:
        return f"{_accounts(count)} got readings."
    verb = "was" if count == 1 else "were"
    return f"{_accounts(count)} {verb} imported."


def added_notice(config: ConnectionConfig, brought: int = 0) -> str:
    """What Connections says after a feed is saved: the result of its first sync."""
    feed = config.feed
    if feed is None:
        return ""
    if feed.status.last_error:
        return f"{config.name} is saved, but its first sync didn't work. {feed.status.last_error}"
    return f"{config.name} is connected. {_tail(config, brought)}"


def synced_notice(config: ConnectionConfig, brought: int = 0) -> str:
    """What Connections says after Sync now."""
    feed = config.feed
    if feed is None:
        return ""
    if feed.status.last_error:
        return f"{config.name} didn't sync. {feed.status.last_error}"
    return f"{config.name} synced. {_tail(config, brought)}"
