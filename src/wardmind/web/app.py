"""The local web UI: a FastAPI app that answers only for 127.0.0.1 and localhost.

Routes are thin. Account data comes from `wardmind.core.service`, the same entry point the CLI
and the MCP server use.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, Undefined
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from wardmind.core import (
    briefs,
    bulk,
    client_updates,
    contacts,
    editing,
    history,
    import_flow,
    importing,
    journal,
    research,
    secrets,
    service,
    summaries,
)
from wardmind.core.client_updates import ClientUpdate, UpdateKind, UpdateStatus
from wardmind.core.config import ScoringConfig, load_scoring_config, save_scoring_config
from wardmind.core.connectors.combined import CombinedConnector, ConnectionProblem, Provenance
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.connectors.hubspot import (
    ConnectionCheck,
    HubSpotClient,
    HubSpotConnector,
    HubSpotMapping,
    load_mapping,
)
from wardmind.core.connectors.local import ManualConnector, SpreadsheetConnector
from wardmind.core.contacts import Contact, ContactSource, ContactStatus
from wardmind.core.feeds import catalog as feed_catalog
from wardmind.core.feeds import setup as feed_setup
from wardmind.core.feeds.http import FeedError
from wardmind.core.feeds.models import (
    Category,
    FeedRole,
    FieldKind,
    MatchBy,
    ProviderInfo,
    Schedule,
    UnmatchedRow,
)
from wardmind.core.feeds.setup import FeedDraft
from wardmind.core.import_flow import Draft
from wardmind.core.importing import DateOrder
from wardmind.core.models import Account, Health, Motion, ScoredAccount, SignalKind
from wardmind.core.onboarding import OnboardingBrief
from wardmind.core.settings import (
    Accent,
    DataSource,
    Density,
    Landing,
    Settings,
    Theme,
    load_settings,
    save_settings,
)
from wardmind.core.store import (
    EDITABLE_FIELDS,
    ConnectionConfig,
    ConnectionKind,
    LocalStore,
    Notice,
    connection_for,
)
from wardmind.web import contact_pages, feed_pages, formatting, journal_pages
from wardmind.web.scoring_form import (
    FIELD_NAMES,
    UNIT_HINTS,
    form_values,
    parse_scoring_form,
    scoring_output,
    slider_max,
)

HOST = "127.0.0.1"  # fixed: no CLI flag or environment variable may change it

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: no-referrer makes browsers send `Origin: null` on the app's
    # own form posts, which the cross-site write check then has to treat as unknown.
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    # Other sites can't embed or fetch this app's responses (no-cors reads are refused).
    "Cross-Origin-Resource-Policy": "same-origin",
}
_MOTION_ORDER = (Motion.CHURN, Motion.RENEWAL, Motion.ONBOARDING)  # the order on the filter
# The brief's motion names. Churn reads "Churn watch" here, where the filter says "Churn".
_BRIEF_MOTION_LABELS: dict[Motion, str] = {
    Motion.CHURN: "Churn watch",
    Motion.RENEWAL: "Renewal",
    Motion.ONBOARDING: "Onboarding",
}


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CROSS_SITE_MESSAGE = "Changes can only be made from Wardmind's own pages."
CROSS_SITE_LOAD_MESSAGE = "Wardmind's pages can only be opened from Wardmind itself."

# The Appearance form's fields: the Settings enum each one must belong to, and its error message.
_APPEARANCE_FIELDS: dict[str, tuple[type[StrEnum], str]] = {
    "theme": (Theme, "Theme has to be one of the choices shown."),
    "accent": (Accent, "Accent colour has to be one of the choices shown."),
    "density": (Density, "Density has to be one of the choices shown."),
    "landing": (Landing, "Opens on has to be one of the choices shown."),
}


def is_cross_site_write(method: str, headers: Mapping[str, str]) -> bool:
    """True for a state-changing request that a browser sent from another site.

    The host allow-list stops DNS rebinding, but any web page can still make the browser POST to
    127.0.0.1. Browsers send Origin on POST (and Sec-Fetch-Site on modern ones), so a write is
    refused unless it came from this app's own origin. Requests with neither header come from
    non-browser clients (tests, curl), which a web page can't forge.

    Some form posts carry `Origin: null` (an opaque origin, e.g. under a no-referrer policy or
    from a sandboxed frame). That says nothing either way, so Sec-Fetch-Site decides, and with
    no Sec-Fetch-Site it is refused.
    """
    if method.upper() not in _UNSAFE_METHODS:
        return False
    origin = headers.get("origin")
    if origin is not None and origin != "null":
        return origin != f"http://{headers.get('host', '')}"
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    return origin == "null"


def is_cross_site_load(method: str, headers: Mapping[str, str]) -> bool:
    """True for a request that another site's page started: an image, a script, a fetch or a frame.

    A top-level navigation (a GET or HEAD of a document, such as a link from another site or the
    address typed in) is let through, since that is how the app is opened. A request with no
    Sec-Fetch-Site (old browsers, tests, curl) is not affected.
    """
    if headers.get("sec-fetch-site") != "cross-site":
        return False
    navigation = (
        headers.get("sec-fetch-mode") == "navigate"
        and method.upper() in ("GET", "HEAD")
        and headers.get("sec-fetch-dest") == "document"
    )
    return not navigation


def idpath(value: object) -> str:
    """An id as one URL path segment: every slash and other reserved character is percent-encoded,
    so an id can't add path segments."""
    return quote(str(value), safe="")


def money(value: float) -> str:
    """Whole dollars with separators: $184,000."""
    return f"${value:,.0f}"


def whole(value: float) -> int:
    """The nearest whole number, halves rounded up, so 12.5 shows as 13 (round() gives 12)."""
    return math.floor(value + 0.5)


def money_short(value: float) -> str:
    """Compact dollars: $1.4M, $950K, $800. A K value that rounds to 1,000K shows as millions."""
    thousands = round(value / 1_000)
    if thousands >= 1_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${thousands}K"
    return f"${value:,.0f}"


def format_today(day: date) -> str:
    """The date line, e.g. 'Tuesday 21 October'. Built by hand because strftime has no %-d."""
    return f"{day:%A} {day.day} {day:%B}"


def summary_sentences(at_risk: int, arr_at_risk: float, watching: int) -> tuple[str, str]:
    """The two summary sentences on My book: accounts at risk, then accounts worth watching."""
    arr = money_short(arr_at_risk)
    if at_risk == 0:
        first = "No accounts are at risk this week."
    elif at_risk == 1:
        first = f"1 account is at risk, carrying {arr} of ARR."
    else:
        first = f"{at_risk} accounts are at risk, carrying {arr} of ARR."

    if watching == 0:
        second = "Nothing else needs watching."
    elif at_risk == 0:
        account = _word(watching, "account", "accounts")
        second = f"{watching} {account} {_word(watching, 'is', 'are')} worth watching this week."
    elif watching == 1:
        second = "1 more is worth watching this week."
    else:
        second = f"{watching} more are worth watching this week."
    return first, second


def _bar_label(arr_by_health: Mapping[Health, float]) -> str:
    """'ARR by health: $1.2M at risk, $2.1M watch, $3.0M healthy'. Empty bands are left out."""
    parts = [
        f"{money_short(arr_by_health[health])} {health.label.lower()}"
        for health in formatting.BOOK_BAR_ORDER
        if arr_by_health.get(health, 0.0) > 0
    ]
    if not parts:
        return "ARR by health: no ARR in the book"
    return "ARR by health: " + ", ".join(parts)


def result_count_text(count: int) -> str:
    """The live count under the filters, e.g. 'Showing 1 account' or 'Showing 40 accounts'."""
    return f"Showing {count} {_word(count, 'account', 'accounts')}"


def _word(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


_HEALTH_ORDER = (Health.AT_RISK, Health.WATCH, Health.HEALTHY)  # the order on the filter
_E = TypeVar("_E", bound=StrEnum)


def _chosen(values: list[str], kind: type[_E]) -> set[_E]:
    """The values in the query string that name a member of `kind`; the rest are ignored."""
    chosen: set[_E] = set()
    for value in values:
        try:
            chosen.add(kind(value))
        except ValueError:
            continue
    return chosen


def _filter_groups(
    brief: briefs.WeeklyBrief,
    flags: dict[str, list[research.Topic]],
    motions: set[Motion],
    healths: set[Health],
    topics: set[research.Topic],
) -> list[dict[str, Any]]:
    """The filter sidebar: each group's checkboxes, with the tag style, icon and count to show.
    Counts come from the whole book, so they don't change as filters are ticked. The news group
    is there only when some account has a topic from its website."""
    groups: list[dict[str, Any]] = [
        {
            "name": "motion",
            "legend": "Motion",
            "options": [
                {
                    "value": motion.value,
                    "label": motion.label,
                    "tag": f"pill pill--{motion.value}",
                    "icon": f"motion-{motion.value}",
                    "count": brief.counts_by_motion[motion],
                    "checked": motion in motions,
                }
                for motion in _MOTION_ORDER
            ],
        },
        {
            "name": "health",
            "legend": "Health",
            "options": [
                {
                    "value": health.value,
                    "label": health.label,
                    "tag": f"health health--{health.value}",
                    "icon": f"health-{health.value.replace('_', '-')}",
                    "count": brief.counts_by_health[health],
                    "checked": health in healths,
                }
                for health in _HEALTH_ORDER
            ],
        },
    ]
    counts = {topic: sum(topic in found for found in flags.values()) for topic in research.Topic}
    if any(counts.values()):
        groups.append(
            {
                "name": "topic",
                "legend": "News from their website",
                "options": [
                    {
                        "value": topic.value,
                        "label": topic.label,
                        "tag": "pill",
                        "icon": f"topic-{topic.value}",
                        "count": count,
                        "checked": topic in topics,
                    }
                    for topic, count in counts.items()
                    if count or topic in topics
                ],
            }
        )
    return groups


def _source_note(settings: Settings) -> str:
    """The top bar's note. It reads the saved choice rather than the connector, so it can't fail."""
    if settings.source is DataSource.MINE:
        return "Showing your data"
    return "Showing demo data"


def _connector_label() -> str:
    """The data source as a fact: 'Demo data', 'Your data', or the connector's name."""
    name = service.get_connector().name
    return {"demo": "Demo data", "combined": "Your data"}.get(name, name)


def _brief_source() -> str:
    """The brief's footer source: 'demo data', 'your data', or the connector's name."""
    name = service.get_connector().name
    return {"demo": "demo data", "combined": "your data"}.get(name, name)


class _LoudUndefined(Undefined):
    """A missing template variable may be tested (`{% if notice %}`), but printing one raises.
    Jinja's default prints "", which once turned a form action into "/import//commit"."""

    def __str__(self) -> str:
        self._fail_with_undefined_error()
        return ""  # unreachable; keeps type checkers happy


_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True, undefined=_LoudUndefined
)
_env.filters["money"] = money
_env.filters["idpath"] = idpath
_env.filters["money_short"] = money_short
_env.filters["whole"] = whole
_env.filters["day_label"] = formatting.day_label
_env.filters["ago"] = formatting.ago
_env.filters["motion_fact"] = formatting.motion_fact
_env.filters["number"] = formatting.number
_env.filters["week_of"] = formatting.week_of
_env.filters["long_date"] = formatting.long_date
_env.globals["risk_sentence"] = formatting.risk_sentence
_env.globals["bar_geometry"] = formatting.bar_geometry
_env.globals["book_bar_segments"] = formatting.book_bar_segments
_env.globals["count_bar_width"] = formatting.count_bar_width
_env.globals["plan_dots"] = formatting.plan_dots
_env.globals["plan_summary"] = formatting.plan_summary
_env.globals["scoring_output"] = scoring_output
_env.globals["slider_max"] = slider_max
templates = Jinja2Templates(env=_env)


def _render(
    request: Request,
    template: str,
    *,
    page: str,
    active: str | None = None,
    status_code: int = 200,
    book: bool = False,
    **context: Any,
) -> HTMLResponse:
    """Render one page in the shared layout, with the context every page needs.

    `book=True` marks a page that shows the book (My book, the brief, onboarding, an account).
    On those pages, with Your data showing, each connection that failed to load gets a banner.
    """
    today_day = date.today()
    current = load_settings()
    problems: list[ConnectionProblem] = []
    if book:
        combined = _combined_or_none()
        if combined is not None:
            problems = combined.problems
    values: dict[str, Any] = {
        "page": page,
        "active": active,
        "settings": current,
        "source_note": _source_note(current),
        "today": format_today(today_day),
        "today_day": today_day,
        "problems": problems,
        "pending_updates": _pending_count(),
        "due_follow_ups": _due_count(),
        **context,
    }
    return templates.TemplateResponse(request, template, values, status_code=status_code)


def _missing(
    request: Request,
    page: str,
    message: str,
    *,
    back_href: str = "",
    back_label: str = "",
) -> HTMLResponse:
    """The plain 404 page, in the layout, with the message. The way back is My book unless a
    page names another."""
    return _render(
        request,
        "not_found.html",
        page=page,
        status_code=404,
        message=message,
        back_href=back_href,
        back_label=back_label,
    )


def _text_field(value: object) -> str:
    """A posted field as text. A file where text was expected counts as blank."""
    return value if isinstance(value, str) else ""


def _accounts(count: int) -> str:
    """'1 account' or '42 accounts'."""
    return f"{count} {_word(count, 'account', 'accounts')}"


def _combined_or_none() -> CombinedConnector | None:
    """The combined book while Your data is showing, or None (demo data, or it can't be built)."""
    if load_settings().source is not DataSource.MINE:
        return None
    try:
        return service.get_combined()
    except ConnectorError:
        return None


def _needs_setup() -> bool:
    """True when Your data is showing and nothing is connected, so My book becomes Setup. A book
    whose accounts are all hidden is not empty of setup: My book keeps the hidden notice."""
    combined = _combined_or_none()
    if combined is None or combined.list_accounts() or combined.problems:
        return False
    return not service.get_store().hidden()


def _pending_count() -> int:
    """The nav badge's count of client updates waiting. 0 when the book can't load here: the page
    that needs the book shows that problem itself."""
    try:
        return service.pending_client_update_count()
    except ConnectorError:
        return 0


def _due_count() -> int:
    """The Today badge: open follow-ups due today or earlier, read from the journal file alone."""
    return service.due_follow_up_count()


def _duplicate_count() -> int:
    """How many distinct accounts the duplicate suggestions name, for the line on My book."""
    combined = _combined_or_none()
    if combined is None:
        return 0
    return len({acc for item in combined.duplicate_suggestions() for acc in item.account_ids})


def _import_notice(imported: str, removed: str) -> list[str]:
    """What My book says after a commit: the accounts that came in, rows skipped, accounts that
    left. Only for a spreadsheet connection that has an import on record."""
    if not imported:
        return []
    store = service.get_store()
    config = store.get_connection(imported)
    if config is None or config.kind is not ConnectionKind.SPREADSHEET:
        return []
    snapshot = store.snapshot(config.id)
    if snapshot is None or not snapshot.history:
        return []
    last = snapshot.history[-1]
    sentences = [
        f"Imported {_accounts(last.accounts)} from {config.last_file_name or config.name}."
    ]
    if last.skipped_rows:
        verb = "was" if last.skipped_rows == 1 else "were"
        rows = f"{last.skipped_rows} {_word(last.skipped_rows, 'row', 'rows')}"
        sentences.append(f"{rows} {verb} skipped.")
    left = int(removed) if removed.isdigit() else 0
    if left:
        if left == 1:
            sentences.append("1 account from last time wasn't in the file, so it left your book.")
        else:
            sentences.append(
                f"{left} accounts from last time weren't in the file, so they left your book."
            )
    return sentences


def _render_appearance(
    request: Request,
    current: Settings,
    *,
    saved: bool = False,
    errors: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The Appearance page. `current` is what the form shows: the saved values, or the choices
    the person just made where they are valid."""
    return _render(
        request,
        "settings/appearance.html",
        page="Appearance",
        active="settings",
        section="appearance",
        current=current,
        errors=dict(errors or {}),
        notice="Appearance saved." if saved else "",
        status_code=status_code,
    )


SCORING_PREVIEW_TOP_N = 10  # the preview lists this many accounts
_SCORING_DEFAULTS_NOTICE = "Showing the defaults. Save changes to use them."
_SCORING_SAVED_NOTICE = "Scoring saved. Every screen and Claude now use it."


@dataclass(frozen=True)
class _Mover:
    """One line of the preview's top ten: the account, where it lands, and how far it moved."""

    rank: int
    account_id: str
    name: str
    movement: str  # "Up 1", "Down 2", "No change", or "New in top 10 (was 14)"
    arrow: str  # the sprite symbol drawn beside the words, or "" for none


@dataclass(frozen=True)
class _Preview:
    """What the scoring preview shows. An error (the book can't load) or alerts (the form can't be
    read yet) replace the sentence and list."""

    alerts: list[str]
    sentence: str
    movers: list[_Mover]
    error: str = ""


def _render_scoring(
    request: Request,
    *,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    preview: _Preview,
    notice: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The Scoring page. `values` is what each form field shows; `errors` is keyed by field."""
    return _render(
        request,
        "settings/scoring.html",
        page="Scoring",
        active="settings",
        section="scoring",
        values=dict(values),
        errors=dict(errors),
        preview=preview,
        notice=notice,
        kinds=list(SignalKind),
        signal_labels=briefs.SIGNAL_LABELS,
        signal_hints=UNIT_HINTS,
        status_code=status_code,
    )


# Settings > Connections. The token is read from the store and never given to a template: the page
# shows only the status sentence, which carries the last four characters at most.
_SOURCE_HELP: dict[DataSource, str] = {
    DataSource.DEMO: "40 invented accounts. Nothing leaves your computer.",
    DataSource.MINE: "Your connections: HubSpot, spreadsheets and accounts you add by hand.",
}
_TOKEN_SAVE_FAILED = (
    "Wardmind couldn't save the token. Check that your user profile folder is writable, "
    "then try again."
)


def _token_tail(masked: str | None) -> str:
    """The last characters of a masked token: '9f3e' from 'pat-na1-…9f3e', or '' when none."""
    return (masked or "").rsplit("…", 1)[-1]


def _token_line(status: secrets.TokenStatus, today: date) -> str:
    """The token's status sentence, worded as the Connections design gives it."""
    tail = _token_tail(status.masked)
    ends = f", ends in …{tail}" if tail else ""
    if status.source is secrets.TokenSource.FILE:
        saved = ""
        if status.saved_at is not None:
            saved = f" Saved {formatting.day_label(status.saved_at, today)}."
        return f"Saved on this computer{ends}.{saved}"
    if status.source is secrets.TokenSource.ENV:
        return f"Set by {secrets.ENV_TOKEN}{ends}. Change it where you set it."
    return "No token saved."


def _fetched_line(loaded_at: float | None) -> str:
    """When HubSpot data was last fetched, in wall-clock time. The connector keeps a monotonic
    reading, so the age of that reading is taken off the current time."""
    if loaded_at is None:
        return "Not fetched yet."
    fetched = datetime.now() - timedelta(seconds=time.monotonic() - loaded_at)
    return f"Data last fetched {fetched:%H:%M}."


def _mapping_rows(mapping: HubSpotMapping) -> list[tuple[str, str]]:
    """The effective HubSpot mapping as (what Wardmind shows, where HubSpot keeps it) pairs."""
    milestones = (
        f"{mapping.milestones_total}, {mapping.milestones_done}, {mapping.milestones_overdue}"
    )
    rows = [
        ("Which companies", f"{mapping.customer_property} = {mapping.customer_value}"),
        ("Name", mapping.name),
        ("Owner (CSM)", mapping.owner),
        ("ARR", mapping.arr),
        ("Tier", mapping.tier),
        ("Customer since", mapping.start_date),
        ("Renewal date", mapping.renewal_date),
        ("Onboarding milestones: total, done, overdue", milestones),
    ]
    rows += [(briefs.SIGNAL_LABELS[kind], prop) for kind, prop in mapping.signals.items()]
    priorities = " or ".join(mapping.sev1_priorities) or "none"
    rows.append(
        (
            "Severity-1 tickets",
            f"Tickets with {mapping.ticket_priority_property} of {priorities} "
            f"and no {mapping.ticket_closed_property}",
        )
    )
    rows.append(("Signal history", f"Readings from the last {mapping.history_days} days"))
    return rows


@dataclass(frozen=True)
class _ConnectionLine:
    """One row of "Your connections": the connection, its status sentence and its count."""

    config: ConnectionConfig
    status: str  # the plain sentence the row shows, or the connector's message when it failed
    problem: str  # the connector's message when the connection can't load, else ""
    accounts: int | None  # None when the count isn't known right now
    # A feed's row only: what it brings in, how often it syncs, why its last sync failed (plain
    # text, never a key), its provider for "Change settings", and the rows that matched no account.
    source: str = ""
    schedule: str = ""
    error: str = ""
    provider_id: str = ""
    is_signals: bool = False
    unmatched: list[UnmatchedRow] = dataclass_field(default_factory=list)
    unmatched_total: int = 0
    is_updates: bool = False  # a client update form: its row links to the review page


def _hubspot_line(
    config: ConnectionConfig, combined: CombinedConnector | None, book_ids: list[str]
) -> _ConnectionLine:
    """The HubSpot row: its count in the combined book and when it was fetched, or its problem."""
    if combined is None:
        return _ConnectionLine(config, "Not used while demo data is showing.", "", None)
    problem = {p.connection_id: p.message for p in combined.problems}.get(config.id, "")
    if problem:
        return _ConnectionLine(config, problem, problem, None)
    count = sum(1 for account_id in book_ids if connection_for(account_id) == config.id)
    try:
        loaded_at = getattr(service.hubspot_connector(), "loaded_at", None)
    except ConnectorError:
        loaded_at = None
    clock = _fetched_clock(loaded_at)
    status = f"{_accounts(count)}." + (f" Fetched {clock}." if clock else "")
    return _ConnectionLine(config, status, "", count)


def _fetched_clock(loaded_at: float | None) -> str:
    """The wall-clock time (14:02) a HubSpot fetch happened, or "" when it hasn't."""
    if loaded_at is None:
        return ""
    return f"{datetime.now() - timedelta(seconds=time.monotonic() - loaded_at):%H:%M}"


def _feed_line(config: ConnectionConfig, store: LocalStore, now: datetime) -> _ConnectionLine:
    """A feed's row. An ACCOUNTS feed counts its own accounts, as a spreadsheet does. A SIGNALS
    feed owns none, so its row says how many accounts its readings reach, and its unmatched rows
    are listed under it."""
    feed = config.feed
    if feed is None:
        return _ConnectionLine(config, "Not set up yet.", "", None)
    status = feed.status
    synced = feed_pages.synced_text(status.last_synced_at, now)
    signals_feed = feed.role is FeedRole.SIGNALS
    updates_feed = feed.role is FeedRole.CLIENT_UPDATES
    if signals_feed:
        count = None
        summary = f"Adds readings to {_accounts(status.accounts)}."
        unmatched = list(status.unmatched)
        unmatched_total = status.skipped_rows
    elif updates_feed:
        # A client update form owns no accounts: its row says what its last sync brought in.
        count = None
        brought = contact_pages.brought_in(store, config)
        summary = f"Brought in {contact_pages.updates_word(brought)}."
        unmatched = []
        unmatched_total = 0
    else:
        snapshot = store.snapshot(config.id)
        count = 0 if snapshot is None else len(snapshot.accounts)
        summary = f"{_accounts(count)}."
        unmatched = []
        unmatched_total = 0
    return _ConnectionLine(
        config=config,
        status=f"{summary} {synced}.",
        problem="",
        accounts=count,
        source=feed_pages.feed_source(feed),
        schedule=feed.schedule.label,
        error=status.last_error or "",
        provider_id=feed.provider,
        is_signals=signals_feed,
        unmatched=unmatched,
        unmatched_total=unmatched_total,
        is_updates=updates_feed,
    )


def _connection_lines(
    store: LocalStore, combined: CombinedConnector | None
) -> list[_ConnectionLine]:
    """One line per connection, in the order the store keeps them."""
    today = date.today()
    now = datetime.now(UTC).replace(tzinfo=None)
    book_ids = [account.id for account in combined.list_accounts()] if combined else []
    lines: list[_ConnectionLine] = []
    for config in store.connections():
        if config.kind is ConnectionKind.HUBSPOT:
            line = _hubspot_line(config, combined, book_ids)
        elif config.kind is ConnectionKind.SPREADSHEET:
            snapshot = store.snapshot(config.id)
            count = 0 if snapshot is None else len(snapshot.accounts)
            if config.last_imported_at is None:
                status = "Not imported yet. Import a file to fill it."
            else:
                day = formatting.day_label(config.last_imported_at, today)
                source = config.last_file_name or config.name
                status = f"{_accounts(count)}. Imported {day} from {source}."
            line = _ConnectionLine(config, status, "", count)
        elif config.kind is ConnectionKind.FEED:
            line = _feed_line(config, store, now)
        else:
            count = len(store.manual().accounts)
            line = _ConnectionLine(config, f"{_accounts(count)}.", "", count)
        if not config.enabled and not line.problem:
            line = _ConnectionLine(
                config,
                f"{line.status} Not included in your book.",
                "",
                line.accounts,
            )
        lines.append(line)
    return lines


def _hidden_accounts(store: LocalStore) -> list[tuple[str, str]]:
    """Each hidden account as (id, name). The name comes from the connections' own accounts when
    they can be read, else the id is shown."""
    hidden = store.hidden()
    if not hidden:
        return []
    names: dict[str, str] = {}
    for config in store.connections():
        try:
            if config.kind is ConnectionKind.HUBSPOT:
                member = service.hubspot_connector()
            elif config.kind is ConnectionKind.SPREADSHEET:
                member = SpreadsheetConnector(store, config.id)
            elif config.kind is ConnectionKind.FEED:
                # An ACCOUNTS feed's accounts are its own snapshot. A SIGNALS feed owns none.
                owns_accounts = config.feed is not None and config.feed.role is FeedRole.ACCOUNTS
                member = SpreadsheetConnector(store, config.id) if owns_accounts else None
            else:
                member = ManualConnector(store)
            accounts = [] if member is None else member.list_accounts()
        except ConnectorError:
            continue
        for account in accounts:
            names.setdefault(account.id, account.name)
    return [(account_id, names.get(account_id, account_id)) for account_id in hidden]


# Data feeds: the Add a connection catalog, setup, matching and the feed rows. Feeds belong to Your
# data, so each page explains that in demo mode instead of offering a form.

_FEEDS_DEMO = (
    "Connections belong to Your data, and demo data is showing. "
    "Switch to Your data on Connections, then add one here."
)
_NO_PROVIDER = "That kind of connection isn't in the catalog."
_NO_SETUP = (
    "That connection set-up has expired or was already saved. Start again from Add a connection."
)
_NOTHING_SAVED = "Nothing was saved. Fix the marked field, then test again."


def _feeds_demo() -> bool:
    """True while demo data is showing. Feeds belong to Your data."""
    return load_settings().source is DataSource.DEMO


def _feeds_demo_page(request: Request, page: str, status_code: int = 200) -> HTMLResponse:
    """The explanation a feed page gives in demo mode, with the way to switch."""
    return _render(
        request,
        "not_found.html",
        page=page,
        status_code=status_code,
        message=_FEEDS_DEMO,
        back_href="/settings/connections",
        back_label="Go to Connections",
    )


def _missing_setup(request: Request) -> HTMLResponse:
    """An unknown or expired set-up: the plain page, with the way back to the catalog."""
    return _missing(
        request,
        "Set-up not found",
        _NO_SETUP,
        back_href="/connections/add",
        back_label="Back to Add a connection",
    )


def _choice(kind: type[_E], text: str, default: _E) -> _E | None:
    """The member a posted choice names; the default when it is blank; None when it names none."""
    if not text:
        return default
    try:
        return kind(text)
    except ValueError:
        return None


def _feed_notice(connection_id: str, *, added: bool) -> str:
    """Connections' notice after a feed is saved (added) or synced, from its status."""
    store = service.get_store()
    config = store.get_connection(connection_id)
    if config is None or config.kind is not ConnectionKind.FEED:
        return ""
    brought = contact_pages.brought_in(store, config)
    if added:
        return feed_pages.added_notice(config, brought)
    return feed_pages.synced_notice(config, brought)


def _render_setup(
    request: Request,
    info: ProviderInfo,
    form: feed_pages.SetupForm,
    *,
    errors: Mapping[str, str],
    top_error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The setup card for one provider. Values come from `form`; the key is never in it."""
    heading = f"Change settings for {form.name}" if form.connection else f"Connect {info.name}"
    return _render(
        request,
        "connections/setup.html",
        page=heading,
        active="settings",
        info=info,
        heading=heading,
        name=form.name,
        connection=form.connection,
        role_choices=feed_pages.role_options(info, form.role),
        fields=feed_pages.setup_fields(info, form, errors, editing=bool(form.connection)),
        schedule_choices=feed_pages.schedule_options(info, form.schedule),
        match_choices=feed_pages.match_options(form.match_by),
        errors=dict(errors),
        top_error=top_error,
        status_code=status_code,
    )


def _match_view(draft: FeedDraft, mapping: Mapping[str, str], date_text: str) -> dict[str, Any]:
    """Everything the matching page and its preview show, for one mapping and date order. The
    preview keeps the mapping on the draft in memory only; the page posts it again each time."""
    result = feed_setup.preview(
        draft,
        dict(mapping),
        date_text or None,
        book_accounts=service.feed_book_accounts,
        now=datetime.now(UTC),
        today=date.today(),
    )
    cleaned = dict(draft.mapping or {})
    rows = len(draft.table.rows)
    show_order, date_choice = feed_pages.date_order_view(draft.table, cleaned, date_text)
    info = feed_catalog.provider_info(draft.provider)
    ignored = 0
    if draft.role is FeedRole.CLIENT_UPDATES and result.blocker is None:
        ignored = _updates_ignored(draft, cleaned, date_text)
    return {
        "draft_id": draft.id,
        "name": draft.name,
        "provider_name": info.name if info is not None else draft.provider,
        "row_count": rows,
        "example_row": draft.table.row_numbers[0] if draft.table.row_numbers else 2,
        "headers": draft.table.headers,
        "rows": feed_pages.match_rows(draft.role, cleaned, draft.table),
        "happens": feed_pages.happens(draft.role, rows, result, ignored),
        "blocker": result.blocker or "",
        "show_date_order": show_order,
        "date_choice": date_choice,
        "match_label": draft.match_by.label if draft.role is FeedRole.SIGNALS else "",
    }


def _updates_ignored(draft: FeedDraft, mapping: Mapping[str, str], date_text: str) -> int:
    """How many rows of a client update form the sync would ignore: no code, a code no client
    opted in, an account no longer in the book, or too many waiting. client_updates.ingest with
    save=False applies the sync's own rules and writes nothing."""
    found = client_updates.ingest(
        service.get_store(),
        draft.table,
        dict(mapping),
        connection_id=draft.connection_id or "feed-preview",
        book_ids={account.id for account in service.feed_book_accounts(draft.connection_id)},
        date_order=_as_order(date_text) or DateOrder.DMY,
        now=datetime.now(UTC),
        save=False,
    )
    return found.ignored


def _render_match(
    request: Request,
    draft: FeedDraft,
    mapping: Mapping[str, str],
    date_text: str,
    *,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The matching page for a feed draft, with the mapping and date order as posted."""
    return _render(
        request,
        "connections/match.html",
        page="Match your columns",
        active="settings",
        error=error,
        status_code=status_code,
        **_match_view(draft, mapping, date_text),
    )


def _render_connections(
    request: Request,
    *,
    notice: str = "",
    source_error: str = "",
    token_error: str = "",
    test_check: ConnectionCheck | None = None,
    test_error: str = "",
    rename_errors: Mapping[str, str] | None = None,
    rename_values: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Settings > Connections. The source form shows the saved choice, or the one just refused."""
    today = date.today()
    current = load_settings()
    status = secrets.token_status()
    hubspot = None
    if current.source is DataSource.MINE:
        try:
            hubspot = service.hubspot_connector()
        except ConnectorError:
            hubspot = None  # a bad mapping: the fetch line and refresh are left out
    try:
        mapping_rows = _mapping_rows(load_mapping())
        mapping_error = ""
    except ConnectorError as error:
        mapping_rows, mapping_error = [], error.message
    store = service.get_store()
    combined = _combined_or_none()
    return _render(
        request,
        "settings/connections.html",
        page="Connections",
        active="settings",
        section="connections",
        notice=notice,
        # Your data first, as the design lists them.
        source_options=[
            {
                "value": source.value,
                "label": source.label,
                "help": _SOURCE_HELP[source],
                "checked": source is current.source,
            }
            for source in (DataSource.MINE, DataSource.DEMO)
        ],
        source_error=source_error,
        token_line=_token_line(status, today),
        token_from_file=status.source is secrets.TokenSource.FILE,
        token_error=token_error,
        test_check=test_check,
        test_error=test_error,
        show_refresh=hubspot is not None,
        fetched=_fetched_line(getattr(hubspot, "loaded_at", None)),
        mapping_rows=mapping_rows,
        mapping_error=mapping_error,
        lines=_connection_lines(store, combined),
        hidden_accounts=_hidden_accounts(store),
        rename_errors=dict(rename_errors or {}),
        rename_values=dict(rename_values or {}),
        status_code=status_code,
    )


def _connection_test_response(
    request: Request, *, check: ConnectionCheck | None = None, error: str = ""
) -> HTMLResponse:
    """The test's result: only the results partial for htmx, the whole page otherwise."""
    if request.headers.get("HX-Request") == "true":
        response = templates.TemplateResponse(
            request,
            "settings/partials/connection_results.html",
            {"test_check": check, "test_error": error},
        )
    else:
        response = _render_connections(request, test_check=check, test_error=error)
    response.headers["Vary"] = "HX-Request"
    return response


async def _posted_fields(request: Request) -> dict[str, str]:
    """The text fields of a posted form. The scoring form has no file fields."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _scoring_preview(draft: ScoringConfig) -> _Preview:
    """What `draft` would change against the saved config: the at-risk counts and the top ten."""
    today = date.today()
    try:
        before = service.get_book(as_of=today)
        after = service.get_book(config=draft, as_of=today)
    except ConnectorError as error:
        return _Preview(alerts=[], sentence="", movers=[], error=error.message)
    now = briefs.weekly_brief(before, as_of=today).counts_by_health[Health.AT_RISK]
    with_changes = briefs.weekly_brief(after, as_of=today).counts_by_health[Health.AT_RISK]
    same_accounts = _at_risk_ids(before) == _at_risk_ids(after)
    changes = briefs.compare_rankings(before, after, SCORING_PREVIEW_TOP_N)
    return _Preview(
        alerts=[],
        sentence=_at_risk_sentence(now, with_changes, same_accounts),
        movers=[_mover(change) for change in changes],
    )


def _preview_alerts(errors: Mapping[str, str]) -> _Preview:
    """The preview for a form that can't be turned into a config yet: the messages, no ranking."""
    return _Preview(alerts=list(errors.values()), sentence="", movers=[])


def _at_risk_ids(book: list[ScoredAccount]) -> set[str]:
    return {item.account.id for item in book if item.health is Health.AT_RISK}


def _at_risk_sentence(now: int, with_changes: int, same_accounts: bool) -> str:
    """'10 accounts at risk now; 11 with these changes.', or 'No change to who is at risk.'"""
    if same_accounts:
        return "No change to who is at risk."
    now_text = f"{now} {_word(now, 'account', 'accounts')}"
    changed_text = f"{with_changes} {_word(with_changes, 'account', 'accounts')}"
    return f"{now_text} at risk now; {changed_text} with these changes."


def _mover(change: briefs.RankChange) -> _Mover:
    if change.entered_top:
        movement = f"New in top {SCORING_PREVIEW_TOP_N} (was {change.old_rank})"
        arrow = "arrow-up"
    elif change.movement > 0:
        movement, arrow = f"Up {change.movement}", "arrow-up"
    elif change.movement < 0:
        movement, arrow = f"Down {-change.movement}", "arrow-down"
    else:
        movement, arrow = "No change", ""
    return _Mover(
        rank=change.new_rank,
        account_id=change.account_id,
        name=change.name,
        movement=movement,
        arrow=arrow,
    )


def _other_accounts(book: list[ScoredAccount]) -> list[ScoredAccount]:
    """The accounts outside onboarding, by name, for the picker's second group."""
    others = [item for item in book if item.motion is not Motion.ONBOARDING]
    return sorted(others, key=lambda item: item.account.name.casefold())


# Your data: setup and the import flow ----------------------------------------------------------

_CHOOSE_FILE_OR_PASTE = "Choose a file or paste some cells."
_CHOOSE_ONE_SOURCE = "Choose a file or paste some cells, not both."
_NO_DRAFT = "That import has expired or was removed. Start the import again from Set up your book."
_NO_CONNECTION = "That connection isn't in your list."
_NOT_HIDDEN = "That account isn't hidden."
_FORM_PART_LIMIT = importing.MAX_BYTES + 1  # a pasted block over the limit is refused


def _too_large_paste() -> str:
    """The importing module's own message for a paste over the size limit."""
    try:
        importing.read_pasted("x" * (importing.MAX_BYTES + 1))
    except importing.ImportError_ as error:
        return str(error)
    return "That paste is too large to read. Split it or remove extra columns."


def _back_value(text: str) -> str:
    """Where Cancel goes: the Connections page when asked for, else Setup."""
    if text == import_flow.BACK_CONNECTIONS:
        return import_flow.BACK_CONNECTIONS
    return import_flow.BACK_SETUP


def _chosen_target(store: LocalStore, text: str) -> str:
    """The "Import into" choice when it names a spreadsheet connection, else a new sheet."""
    config = store.get_connection(text)
    if config is not None and config.kind is ConnectionKind.SPREADSHEET:
        return config.id
    return import_flow.NEW_TARGET


def _as_order(text: str) -> DateOrder | None:
    try:
        return DateOrder(text)
    except ValueError:
        return None


def _submitted_mapping(
    fields: Mapping[str, str], role: FeedRole = FeedRole.ACCOUNTS
) -> dict[str, str]:
    """The matching page's selects, keyed by what the role maps: column-<index> is the header
    chosen for the import field at that index (the spreadsheet import and an ACCOUNTS or SIGNALS
    feed), or for the client update question at that index (CLIENT_UPDATES). feed_pages.match_rows
    numbers them the same way."""
    if role is FeedRole.CLIENT_UPDATES:
        return {
            question.key: fields.get(f"column-{index}", "")
            for index, question in enumerate(client_updates.QUESTIONS)
        }
    return {
        spec.key: fields.get(f"column-{index}", "")
        for index, spec in enumerate(importing.IMPORT_FIELDS)
    }


def _current_mapping(store: LocalStore, draft: Draft) -> dict[str, str]:
    """The mapping the page shows: the CSM's last choice, else the suggested one."""
    if draft.mapping is not None:
        return dict(draft.mapping)
    return import_flow.suggested_mapping(draft, store)


def _mapping_view(
    store: LocalStore, draft: Draft, mapping: Mapping[str, str], submitted_order: str
) -> dict[str, Any]:
    """Everything the mapping page and its preview partial show, for one mapping."""
    cleaned = import_flow.clean_mapping(draft.table, mapping)
    order = import_flow.resolve_date_order(draft, cleaned, submitted_order)
    result = import_flow.preview(
        draft, cleaned, order, store, now=datetime.now(UTC), today=date.today()
    )
    implied = import_flow.suggested_date_order(draft, cleaned)
    chosen = _as_order(submitted_order) or import_flow.date_order_choice(draft, store)
    if chosen is DateOrder.YMD:  # the radios only offer day or month first
        chosen = DateOrder.DMY
    rows = [
        {
            "index": index,
            "label": spec.label,
            "help": spec.help,
            "required": spec.required,
            "selected": cleaned.get(spec.key, ""),
            "example": result.examples.get(spec.key, ""),
        }
        for index, spec in enumerate(importing.IMPORT_FIELDS)
    ]
    return {
        "rows": rows,
        "headers": draft.table.headers,
        "preview": result,
        "show_date_order": implied is None and bool(import_flow.date_columns(draft, cleaned)),
        "date_choice": chosen.value,
        "accounts": _accounts(result.ready_rows),
        # The examples come from the first data row; show its real spreadsheet row number.
        "example_row": draft.table.row_numbers[0] if draft.table.row_numbers else 2,
        "draft_id": draft.id,
    }


def _render_import_form(
    request: Request,
    store: LocalStore,
    *,
    target: str,
    new_name: str = "",
    pasted: str = "",
    back: str = import_flow.BACK_SETUP,
    paste_first: bool = False,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The upload-or-paste form. `target` is the "Import into" choice that starts checked."""
    options: list[dict[str, Any]] = [
        {"value": import_flow.NEW_TARGET, "label": "A new sheet", "checked": target == "new"}
    ]
    for config in store.connections():
        if config.kind is ConnectionKind.SPREADSHEET:
            options.append(
                {
                    "value": config.id,
                    "label": f"Update {config.name}",
                    "checked": target == config.id,
                }
            )
    return _render(
        request,
        "import/form.html",
        page="Import a spreadsheet",
        active="book",
        options=options,
        new_name=new_name,
        pasted=pasted,
        back=back,
        paste_first=paste_first or bool(pasted),
        error=error,
        status_code=status_code,
    )


def _render_mapping(
    request: Request,
    store: LocalStore,
    draft: Draft,
    mapping: Mapping[str, str],
    submitted_order: str,
    *,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The mapping and preview page for a draft."""
    names = draft.table.sheet_names if len(draft.table.sheet_names) > 1 else []
    return _render(
        request,
        "import/map.html",
        page="Match your columns",
        active="book",
        draft=draft,
        sheet_names=names,
        row_count=len(draft.table.rows),
        error=error,
        status_code=status_code,
        **_mapping_view(store, draft, mapping, submitted_order),
    )


# Your data: editing an account, adding one, and possible duplicates ---------------------------

_EDIT_DEMO = "Editing is available for your own data."
_DUPLICATES_DEMO = "Possible duplicates are shown for your own data."
_NOT_IN_BOOK_FORM = "That account isn't in your book."
_NOT_SUGGESTED = "Those accounts aren't suggested as duplicates any more."
_NO_NOTICE = "That message isn't on this account any more."
_UNCHANGED = "unchanged"  # ?edited= value when the saved form had no changes
_NEW_READING = {"kind": SignalKind.USAGE_TREND_30D.value, "value": "", "yes_no": "yes"}
_UNKNOWN_SIGNAL = "That signal isn't one Wardmind knows."
_NO_FIELD = "That field can't be edited."
_HAND_ADDED_ONLY = "Only accounts you added by hand can be deleted. Hide it instead."


def _your_book() -> CombinedConnector | None:
    """The combined book while Your data is showing, else None. A book that can't load raises
    ConnectorError, which the app shows on its own error page."""
    if load_settings().source is not DataSource.MINE:
        return None
    return service.get_combined()


def _editable_account(
    request: Request, account_id: str
) -> tuple[CombinedConnector, Account, Provenance] | HTMLResponse:
    """The account's shown values and provenance, or a 404 page. Demo data has no editing, and an
    id that isn't in the book (hidden accounts included) is unknown."""
    combined = _your_book()
    if combined is None:
        return _missing(request, "Page not found", _EDIT_DEMO)
    prov = combined.provenance(account_id)
    account = next((item for item in combined.list_accounts() if item.id == account_id), None)
    if prov is None or account is None:
        return _missing(request, "Account not found", f"No account with id {account_id}.")
    return combined, account, prov


def _quoted_id(account_id: str) -> str:
    """An account id as a URL path segment."""
    return quote(account_id, safe="")


def _arr_text(value: float) -> str:
    """ARR as the edit form shows it: "$135,000", with cents only when the value has them."""
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def _form_values(account: Account) -> dict[str, str]:
    """The edit form's fields as the book shows them now."""
    milestones = account.milestones
    return {
        "name": account.name,
        "domain": account.domain or "",
        "arr": _arr_text(account.arr),
        "tier": account.tier,
        "owner": account.owner,
        # A date input shows nothing for a blank value, so an unknown start date is "".
        "start_date": "" if account.start_date is None else account.start_date.isoformat(),
        "renewal_date": account.renewal_date.isoformat(),
        "milestones_total": str(milestones.total),
        "milestones_done": str(milestones.done),
        "milestones_overdue": str(milestones.overdue),
    }


def _source_text(field: str, value: Any) -> str:
    """A connection's value for one field, as the note says it: "$120,000", "1 December 2026"."""
    if value is None or value == "":
        return "nothing"
    if field in ("start_date", "renewal_date") and isinstance(value, date):
        return formatting.long_date(value)
    if field == "arr":
        return _arr_text(float(value))
    if field.startswith("milestones_"):
        return str(int(value))
    return str(value)


def _edit_notes(prov: Provenance) -> dict[str, dict[str, str]]:
    """For each applied edit: the connection that owns the value and what that connection says.
    Hand-added accounts never have edits, so they get none."""
    if prov.editable_in_place or not prov.connections:
        return {}
    connection = prov.connections[0]
    return {
        field: {"connection": connection, "source": _source_text(field, prov.source_values[field])}
        for field in EDITABLE_FIELDS
        if field in prov.edits
    }


def _joined(names: list[str]) -> str:
    """Joins names as "A", "A and B" or "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _source_line(prov: Provenance) -> str:
    """Where an account comes from: "From HubSpot.", "From HubSpot and Q4 renewals sheet (linked)."
    or "Added by hand."."""
    if prov.editable_in_place:
        return "Added by hand."
    linked = " (linked)" if len(prov.member_ids) > 1 else ""
    return f"From {_joined(prov.connections)}{linked}."


def _account_status(added: str, edited: str, reading: str, unlinked: str) -> str:
    """The status line on the account page, for the query that the last action redirected with."""
    if added == "1":
        return "Account added."
    if edited == "1":
        return "Changes saved."
    if edited == _UNCHANGED:
        return "Nothing changed."
    if reading == "1":
        return "Reading added."
    if unlinked == "1":
        return "Unlinked. The accounts show separately again."
    return ""


def _revert_status(prov: Provenance, reverted: str) -> str:
    """The edit page's status after a revert: which connection's value shows again."""
    if reverted in EDITABLE_FIELDS and reverted not in prov.edits and prov.connections:
        return f"Using {prov.connections[0]}'s value."
    return ""


def _render_edit(
    request: Request,
    account: Account,
    prov: Provenance,
    *,
    values: Mapping[str, str],
    errors: Mapping[str, str] | None = None,
    refused: bool = False,
    notice: str = "",
    reading: Mapping[str, str] | None = None,
    reading_error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The edit page for one account. `values` is what each field shows; `errors` by field."""
    return _render(
        request,
        "accounts/edit.html",
        page=f"Edit {account.name}",
        active="book",
        account=account,
        prov=prov,
        values=dict(values),
        errors=dict(errors or {}),
        notes=_edit_notes(prov),
        refused=refused,
        notice=notice,
        source_line=_source_line(prov),
        kinds=list(SignalKind),
        signal_labels=briefs.SIGNAL_LABELS,
        reading=dict(reading or _NEW_READING),
        reading_error=reading_error,
        status_code=status_code,
    )


def _render_new(
    request: Request,
    *,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    refused: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    """The add-an-account page, with the typed values kept when it is refused."""
    return _render(
        request,
        "accounts/new.html",
        page="Add an account",
        active="book",
        values=dict(values),
        errors=dict(errors),
        refused=refused,
        status_code=status_code,
    )


def _linked_notice(combined: CombinedConnector, account_id: str) -> str:
    """What the duplicates page says after a link: the kept account's name, looked up by its id.
    The generic line when that account is no longer in the book."""
    account = next((item for item in combined.list_accounts() if item.id == account_id), None)
    if account is None:
        return "The accounts were linked."
    return f"Linked {account.name}."


def _hidden_notice(account_id: str) -> str:
    """The name in 'X is hidden.' on My book, looked up by the account's id. 'An account' when the
    name can't be read, and '' when the account isn't hidden (an old link or a made-up id)."""
    if not account_id:
        return ""
    store = service.get_store()
    if account_id not in store.hidden():
        return ""
    name = dict(_hidden_accounts(store)).get(account_id, account_id)
    return "An account" if name == account_id else name


def _refused(request: Request, page: str, message: str) -> HTMLResponse:
    """A plain 422 page for an action the book won't take, with the reason in the layout."""
    return _render(request, "not_found.html", page=page, status_code=422, message=message)


def _duplicate_pairs(combined: CombinedConnector) -> list[dict[str, Any]]:
    """Each suggested pair as two sides (name, connection, ARR, renewal, owner, website) and the
    reason it was suggested."""
    accounts = {account.id: account for account in combined.list_accounts()}
    today = date.today()
    pairs: list[dict[str, Any]] = []
    for suggestion in combined.duplicate_suggestions():
        sides = []
        for account_id in suggestion.account_ids:
            account = accounts[account_id]
            prov = combined.provenance(account_id)
            sides.append(
                {
                    "id": account_id,
                    "name": account.name,
                    "connection": _joined(prov.connections) if prov else "",
                    "arr": money(account.arr),
                    "renewal": formatting.day_label(account.renewal_date, today),
                    "owner": account.owner,
                    "website": account.domain or "Not given",
                }
            )
        pairs.append({"reason": suggestion.reason, "sides": sides})
    return pairs


def _render_duplicates(
    request: Request,
    combined: CombinedConnector,
    *,
    status: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The possible-duplicates page: each pair side by side, with its link and dismiss forms."""
    return _render(
        request,
        "duplicates.html",
        page="Possible duplicates",
        active="book",
        pairs=_duplicate_pairs(combined),
        status=status,
        error=error,
        status_code=status_code,
    )


# --- Contacts and client updates: the account page, the review page and the set-up page ---------
# The view values are built in web/contact_pages.py. Notices travel as codes in the query string;
# names are looked up here, so no contact's name is ever in a URL.

_CONTACT_NOTICES = {
    "added": "Contact added.",
    "saved": "Contact saved.",
    "left": "Marked as left.",
    "active": "Contact is active again.",
    "removed": "Contact removed.",
}
_UPDATES_NOTICES = {
    "on": "Client updates are on.",
    "link": "New link made. The old link stops working.",
    "off": "Client updates are off, and their waiting updates were deleted.",
}
_NO_CONTACT = "That contact isn't in this account."
_UPDATES_OFF = "Client updates are off for this client. Turn them on from its account page."
_UPDATE_GONE = "That update was already decided, or it isn't there."
_UPDATES_DEMO = (
    "Client updates belong to Your data, and demo data is showing. "
    "Switch to Your data on Connections, then come back here."
)
_UPDATE_KINDS = frozenset(kind.value for kind in UpdateKind)


def _local_now() -> datetime:
    """Now as an aware local time. Its date is the day the person is on, which opt-in compares
    with the date they agreed."""
    return datetime.now().astimezone()


def _parse_day(text: str) -> date | None:
    """The date a date box holds (YYYY-MM-DD), or None when it is blank or not a date."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _find_contact(account_id: str, contact_id: str) -> Contact | None:
    """The account's contact with this id, or None."""
    found = contacts.contacts_for(service.get_store(), account_id)
    return next((contact for contact in found if contact.id == contact_id), None)


def _pending_update(store: LocalStore, update_id: str) -> ClientUpdate | None:
    """The update with this id when it is still waiting for review, else None."""
    return next(
        (
            update
            for update in client_updates.load(store).updates
            if update.id == update_id and update.status is UpdateStatus.PENDING
        ),
        None,
    )


def _update_gone(request: Request) -> HTMLResponse:
    return _missing(
        request,
        "Update not found",
        _UPDATE_GONE,
        back_href="/client-updates",
        back_label="Back to Client updates",
    )


def _updates_demo_page(request: Request, status_code: int = 200) -> HTMLResponse:
    """The explanation the review and set-up pages give in demo mode."""
    return _render(
        request,
        "not_found.html",
        page="Client updates",
        active="updates",
        status_code=status_code,
        message=_UPDATES_DEMO,
        back_href="/settings/connections",
        back_label="Go to Connections",
    )


def _done_notice(done: str, account_id: str, contact_id: str) -> str:
    """What the review page says after an accept: the names come from the store, by id."""
    if done not in _UPDATE_KINDS and done not in ("reject", "checked"):
        return ""
    if done in ("reject", "other", "checked"):
        return contact_pages.done_notice(done, account_name="", contact=None, risk=False)
    store = service.get_store()
    account = service.your_book_accounts().get(account_id)
    found = contacts.contacts_for(store, account_id) if account_id else []
    contact = next((item for item in found if item.id == contact_id), None) if contact_id else None
    risk = (
        contact is not None
        and contact.champion
        and not any(item.champion and item.status is ContactStatus.ACTIVE for item in found)
    )
    return contact_pages.done_notice(
        done,
        account_name=account.name if account is not None else "",
        contact=contact,
        risk=risk,
    )


def _render_updates(
    request: Request,
    *,
    chosen: set[UpdateKind],
    notice: str = "",
    errors: Mapping[str, str] | None = None,
    typed: Mapping[str, Mapping[str, str]] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The review page: the pending updates the kind filter chooses, each as a card. A refused
    accept comes back with its message and the values as typed, on its own card (422)."""
    store = service.get_store()
    reviews = service.pending_client_updates()
    today = date.today()
    now = datetime.now(UTC).replace(tzinfo=None)
    shown = [review for review in reviews if not chosen or review.update.kind in chosen]
    errors = dict(errors or {})
    typed = dict(typed or {})
    cards = [
        contact_pages.update_card(
            review,
            today=today,
            typed=typed.get(review.update.id),
            error=errors.get(review.update.id, ""),
        )
        for review in shown
    ]
    feeds = contact_pages.update_feeds(store, now)
    link_saved = bool(client_updates.load(store).settings.form_link)
    gone = len(contact_pages.gone_updates(store, set(service.your_book_accounts())))
    return _render(
        request,
        "client_updates/review.html",
        page="Client updates",
        active="updates",
        notice=notice,
        status_code=status_code,
        kinds=contact_pages.kind_options(reviews, chosen),
        filter_query=contact_pages.filter_query(chosen),
        filtered=bool(chosen),
        cards=cards,
        waiting=len(reviews),
        read_text=contact_pages.read_text(contact_pages.last_read(store), now),
        feeds=feeds,
        setup_done=link_saved and bool(feeds),
        gone=gone,
        gone_text=contact_pages.gone_text(gone) if gone else "",
    )


def _render_updates_setup(
    request: Request,
    *,
    notice: str = "",
    link_value: str = "",
    link_error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The set-up page: the questions, the ways to read the responses, and the pre-filled link."""
    store = service.get_store()
    now = datetime.now(UTC).replace(tzinfo=None)
    return _render(
        request,
        "client_updates/setup.html",
        page="Set up your update form",
        active="updates",
        notice=notice,
        status_code=status_code,
        form_link=client_updates.load(store).settings.form_link,
        link_value=link_value,
        link_error=link_error,
        questions=contact_pages.setup_questions(),
        providers=contact_pages.setup_providers(),
        feeds=contact_pages.update_feeds(store, now),
    )


# --- Web research: the account page's section and the Data & privacy page -----------------

_RESEARCH_CHOICE_ERROR = "Choose On or Off, then save again."
_RESEARCH_NOTICES = {
    "on": "Web research is on.",
    "off": "Web research is off. Saved lookups were removed.",
}


@dataclass(frozen=True)
class _ResearchPanel:
    """What the account page's "From their website" section shows. `state` is one of "off",
    "no_website", "unchecked" (nothing saved yet), "error" (the last look up failed) or "result".
    The AI fields matter in "result": `ai_ready` says whether the summary button works, `ai_hint`
    says why it doesn't ("off" or "key"), `summary` is the saved summary, and `ai_error` is what
    the last summary attempt said."""

    state: str
    account_id: str  # quoted for a URL path
    host: str = ""  # the website a look up visits, e.g. "acme.com"
    demo: bool = False
    message: str = ""  # no_website: why there is no website; error: what went wrong
    add_url: str = ""  # Your data: the edit page's Website field
    add_label: str = ""  # "Add it" when there is no website, else "Change it"
    result: research.SiteResearch | None = None
    looked_up_days: int = 0  # whole days since the saved look up; 0 is today
    ai_ready: bool = False  # AI summaries can be used right now
    ai_hint: str = ""  # "off" or "key" when they can't yet, else ""
    summary: summaries.Summary | None = None  # the saved AI summary of this website, if any
    ai_error: str = ""  # what the last summary attempt said
    ai_error_link: bool = False  # the problem is the key or its credit: link to Data & privacy


def _research_panel(
    account: Account,
    *,
    editable: bool,
    result: research.SiteResearch | None = None,
    error: str = "",
    ai_error: str = "",
    ai_error_link: bool = False,
) -> _ResearchPanel:
    """The section's state for one account. Nothing here reaches the network: a saved summary is
    a file read, and a new summary is only asked for by the summarise button."""
    quoted = _quoted_id(account.id)
    settings = load_settings()
    if not settings.web_research:
        return _ResearchPanel(state="off", account_id=quoted)
    demo = settings.source is DataSource.DEMO
    host = ""
    if not demo:
        try:
            host = research.website_host(account.domain)
        except research.ResearchError as problem:
            blank = not (account.domain or "").strip()
            return _ResearchPanel(
                state="no_website",
                account_id=quoted,
                message="This account has no website." if blank else problem.message,
                add_url=f"/accounts/{quoted}/edit#field-domain" if editable else "",
                add_label="Add it" if blank else "Change it",
            )
    ai_ok = service.ai_ready() is None
    shared: dict[str, Any] = {
        "account_id": quoted,
        "host": host,
        "demo": demo,
        "ai_ready": ai_ok,
        "ai_hint": "" if ai_ok else ("off" if not settings.ai_summaries else "key"),
        "summary": service.saved_summary(account.id) if ai_ok and result is not None else None,
        "ai_error": ai_error,
        "ai_error_link": ai_error_link,
    }
    if error:
        return _ResearchPanel(state="error", message=error, **shared)
    if result is not None:
        days = (date.today() - result.fetched_at.astimezone().date()).days
        return _ResearchPanel(state="result", result=result, looked_up_days=days, **shared)
    return _ResearchPanel(state="unchecked", **shared)


def _shown_account(account_id: str) -> Account | None:
    """The account as the book shows it right now (hidden accounts are not shown), or None."""
    return next(
        (item for item in service.get_connector().list_accounts() if item.id == account_id),
        None,
    )


def _research_editable(account_id: str) -> bool:
    """True when the account is one the CSM can edit (it belongs to Your data)."""
    combined = _combined_or_none()
    return combined is not None and combined.provenance(account_id) is not None


def _cache_notice(lookups: str, drafts: str) -> str:
    """What the Data & privacy page says after Clear local cache. The counts are numbers the
    server wrote into the URL, so they are checked before they are used."""
    saved = int(lookups) if lookups.isascii() and lookups.isdigit() else 0
    unfinished = int(drafts) if drafts.isascii() and drafts.isdigit() else 0
    parts: list[str] = []
    if saved:
        parts.append(f"{saved} saved {_word(saved, 'lookup', 'lookups')}")
    if unfinished:
        parts.append(f"{unfinished} unfinished {_word(unfinished, 'import', 'imports')}")
    if not parts:
        return "There was nothing to clear."
    return "Removed " + " and ".join(parts) + "."


_AI_CHOICE_ERROR = "Choose On or Off, then save again."
_AI_WEB_NOTE = " Turn on web research too to use them."
_KEY_NOTICES = {"saved": "OpenRouter key saved.", "removed": "OpenRouter key removed."}
_KEY_SAVE_FAILED = (
    "Wardmind couldn't save the key. Check that your user profile folder is writable, "
    "then try again."
)
_JOB_ID = re.compile(r"[0-9a-f]{16}")  # what bulk.JobRunner makes: secrets.token_hex(8)


def _ai_status(settings: Settings) -> str:
    """The AI summaries status sentence on Data & privacy, with the web research note."""
    if settings.ai_summaries:
        status = f"AI summaries are on, using {settings.ai_model}."
    else:
        status = "AI summaries are off."
    if not settings.web_research:
        status += " They need web research to be on too."
    return status


def _ai_notice(choice: str) -> str:
    """What Data & privacy says after the AI summaries choice is saved."""
    if choice == "off":
        return "AI summaries are off. Saved summaries were removed."
    return "AI summaries are on." + ("" if load_settings().web_research else _AI_WEB_NOTE)


def _key_line(status: secrets.TokenStatus, today: date) -> str:
    """The OpenRouter key's status sentence. Only the masked form (the last 4 characters) shows."""
    tail = status.masked or ""
    if status.source is secrets.TokenSource.ENV:
        return f"Set by the {secrets.ENV_OPENROUTER_KEY} environment variable ({tail})."
    if status.source is secrets.TokenSource.FILE:
        added = f", added {formatting.day_label(status.saved_at, today)}" if status.saved_at else ""
        return f"Saved key {tail}{added}."
    return "No key saved."


_DELETED_ALL_NOTICE = "All local data was deleted."


def _deleted_all_notice() -> str:
    """The notice after Delete all local data. The app can't unset an environment variable, so a
    set one is named (its name only, never its value) with the words to remove it."""
    names = [
        name for name in (secrets.ENV_TOKEN, secrets.ENV_OPENROUTER_KEY) if os.environ.get(name)
    ]
    if not names:
        return _DELETED_ALL_NOTICE
    if len(names) == 1:
        listed = f"the {names[0]} environment variable, which is still set"
    else:
        listed = f"the {names[0]} and {names[1]} environment variables, which are still set"
    return f"{_DELETED_ALL_NOTICE} Also remove {listed}."


def _render_privacy(
    request: Request,
    *,
    notice: str = "",
    research_error: str = "",
    ai_choice: bool | None = None,
    ai_error: str = "",
    ai_model_value: str | None = None,
    ai_model_error: str = "",
    key_error: str = "",
    key_result: str = "",
    key_result_ok: bool = True,
    status_code: int = 200,
) -> HTMLResponse:
    """The Data & privacy page: web research, AI summaries and their key, and the local cache.
    A refused form passes back what was typed (the model), never the key."""
    current = load_settings()
    key_status = secrets.openrouter_key_status()
    return _render(
        request,
        "settings/privacy.html",
        page="Data & privacy",
        active="settings",
        section="privacy",
        web_on=current.web_research,
        research_error=research_error,
        notice=notice,
        data_folder=str(service.get_store().root),
        ai_on=current.ai_summaries if ai_choice is None else ai_choice,
        ai_status=_ai_status(current),
        ai_model=current.ai_model if ai_model_value is None else ai_model_value,
        ai_error=ai_error,
        ai_model_error=ai_model_error,
        key_line=_key_line(key_status, date.today()),
        key_from_file=key_status.source is secrets.TokenSource.FILE,
        key_error=key_error,
        key_result=key_result,
        key_result_ok=key_result_ok,
        status_code=status_code,
    )


# --- Website lookups: every account at once (the /research page) -------------------------


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _cost_text(value: float) -> str:
    """The job's AI cost: $0.00 for nothing, four decimals under a dollar (small summaries cost
    fractions of a cent), else two."""
    if value <= 0:
        return "$0.00"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _job_facts(job: bulk.Job | None) -> dict[str, Any]:
    """What the progress panel shows of a job, as plain values: the non-zero counts for each
    result, the AI cost, and every account with a message (a problem, a block or a skip)."""
    if job is None:
        return {"lookup_counts": [], "summary_counts": [], "cost_text": "", "problems": []}
    lookup_counts = []
    for result in bulk.LookupResult:
        count = sum(1 for outcome in job.outcomes if outcome.lookup is result)
        if count:
            lookup_counts.append((result.label, count))
    summary_counts = []
    if job.summarise:
        for result in bulk.SummaryResult:
            count = sum(1 for outcome in job.outcomes if outcome.summary is result)
            if count:
                summary_counts.append((result.label, count))
    problems = [
        {"id": outcome.account_id, "name": outcome.account_name, "message": outcome.message}
        for outcome in job.outcomes
        if outcome.message
    ]
    return {
        "lookup_counts": lookup_counts,
        "summary_counts": summary_counts,
        "cost_text": _cost_text(job.cost_usd) if job.summarise else "",
        "problems": problems,
    }


def _lookup_rows(book: list[ScoredAccount], flags: Mapping[str, list[Any]]) -> list[dict[str, Any]]:
    """One row per account in book order, from saved data only (no network): the website, when it
    was last looked up, the saved summary (or the site's description), and the topics."""
    today = date.today()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for item in book:
        account = item.account
        saved = service.saved_lookup(account, now)
        summary = service.saved_summary(account.id)
        if summary is not None:
            about, badge = summary.about, "Sample" if summary.sample else "AI"
        else:
            about, badge = (saved.site_description or "") if saved else "", ""
        if saved is not None:
            website = saved.website
            looked_up = formatting.ago((today - saved.fetched_at.astimezone().date()).days)
        else:
            try:
                website = research.website_host(account.domain)
            except research.ResearchError:
                website = "No website"
            looked_up = "Not yet"
        rows.append(
            {
                "account_id": _quoted_id(account.id),
                "name": account.name,
                "website": website,
                "looked_up": looked_up,
                "about": about,
                "badge": badge,
                "topics": flags.get(account.id, []),
            }
        )
    return rows


def _job_panel(
    request: Request, job: bulk.Job | None, *, error: str = "", status_code: int = 200
) -> HTMLResponse:
    """The progress panel for htmx: while the job runs it polls itself; when it has finished, the
    response asks htmx to reload the page once, so the table below shows the new lookups."""
    response = templates.TemplateResponse(
        request,
        "partials/bulk_job.html",
        {"job": job, "error": error, **_job_facts(job)},
        status_code=status_code,
    )
    response.headers["Vary"] = "HX-Request"
    if job is not None and not job.running:
        response.headers["HX-Refresh"] = "true"
    return response


def _render_research(
    request: Request, *, job_id: str = "", error: str = "", status_code: int = 200
) -> HTMLResponse:
    """The Website lookups page: the form, the progress panel, and what is saved for every account.
    A job named in the URL is shown; otherwise the current or last job. Nothing here reaches the
    network."""
    settings = load_settings()
    if job_id:
        job = service.bulk_job(job_id) if _JOB_ID.fullmatch(job_id) else None
    else:
        job = service.bulk_job()
    book = service.get_book() if settings.web_research else []
    flags = service.account_flags() if settings.web_research else {}
    return _render(
        request,
        "research.html",
        page="Website lookups",
        status_code=status_code,
        web_on=settings.web_research,
        count=len(book),
        ai_reason=service.ai_ready() or "",
        rows=_lookup_rows(book, flags) if settings.web_research else [],
        job=job,
        error=error,
        **_job_facts(job),
    )


def _render_account(
    request: Request,
    account_id: str,
    *,
    status: str = "",
    research_panel: _ResearchPanel | None = None,
    refusal: contact_pages.FormRefusal | None = None,
    journal_refusal: journal_pages.JournalRefusal | None = None,
    why: str = "",
    undo: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """One account's page. Without `research_panel`, the website section shows what is already
    saved, which is a file read and never a network call. `refusal` is a contact or client update
    form that was refused: its page comes back with the form's values and messages (422).
    `journal_refusal` is the same for a journal form. `why` is a refusal sent back from Today, and
    `undo` the id of a follow-up that was just marked done (its Undo button is offered)."""
    connector = service.get_connector()
    detail = service.get_account_detail(account_id, connector)
    if detail is None:
        return _render(
            request,
            "not_found.html",
            page="Account not found",
            status_code=404,
            message=f"No account with id {account_id}.",
            book=True,
        )
    combined = connector if isinstance(connector, CombinedConnector) else None
    prov = combined.provenance(account_id) if combined is not None else None
    notices: list[Notice] = []
    # Where each reading in the history came from, for its label: by hand, or from the contacts.
    origins: dict[tuple[str, datetime, float], str] = {}
    if combined is not None:
        notices = service.get_store().notices(account_id)
        for signal in combined.list_signals(account_id):
            key = (signal.kind.value, signal.observed_at, signal.value)
            if signal.source == "manual":
                origins[key] = "Added by you"
            elif signal.source == contacts.SIGNAL_SOURCE:
                origins[key] = "From contacts"
    edits = prov.edits if prov is not None else {}
    if research_panel is None:
        research_panel = _research_panel(
            detail.scored.account,
            editable=prov is not None,
            result=service.cached_research(account_id),
        )
    today = date.today()
    editable = prov is not None
    section = contact_pages.contacts_section(
        service.account_contacts(account_id), editable=editable, today=today, refusal=refusal
    )
    box = (
        contact_pages.updates_box(service.get_store(), account_id, today=today, refusal=refusal)
        if editable
        else None
    )
    config = load_scoring_config()
    account_journal = service.account_journal(account_id)
    found_undo = journal_pages.find_follow_up({account_id: account_journal}, undo)
    journal_panel = journal_pages.panel(
        scored=detail.scored,
        account_journal=account_journal,
        suggested=detail.explanation.suggested_action,
        today=today,
        now=datetime.now(UTC).replace(tzinfo=None),  # naive UTC, as the journal keeps its times
        demo=load_settings().source is DataSource.DEMO,
        refusal=journal_refusal,
        why=why,
        undo_id=found_undo[1].id if found_undo is not None else "",
    )
    trend = journal_pages.sparkline(
        service.account_trend(detail.scored, detail.rank, today), config.thresholds, today
    )
    return _render(
        request,
        "account.html",
        page=detail.explanation.name,
        active="book",
        book=True,
        status_code=status_code,
        detail=detail,
        thresholds=load_scoring_config().thresholds,
        timeline_days=formatting.group_by_day(detail.timeline),
        connector_label=_connector_label(),
        prov=prov,
        edits=edits,
        milestones_edited=any(field.startswith("milestones_") for field in edits),
        linked=prov is not None and len(prov.member_ids) > 1,
        source_line=_source_line(prov) if prov is not None else "",
        notices=notices,
        origins=origins,
        status=status,
        panel=research_panel,
        contacts=section,
        account_path=contact_pages.account_path(account_id),
        updates_box=box,
        journal=journal_panel,
        trend=trend,
    )


# --- The journal: notes, follow-ups, snoozes and renewal outcomes (see core/journal.py) -------
# Every journal form posts to a route in create_app. A form sent from Today (back=today) returns
# to Today; any other value of `back` returns to the account page. Notices travel as fixed codes in
# the query (?journal=, ?why=, ?day=, ?undo=), never as the text a CSM typed.

_SINCE_LIMIT = 5  # "Since last week" on My book lists this many changes, then a link to the brief
_WEEK_FOLLOW_UPS = 8  # "Follow-ups due this week" on the brief lists this many
_DAY_LIMIT = 4000  # a days shortcut is clamped to this; the core keeps the real date range
_NO_NOTE = "That note isn't in this account."
_NO_FOLLOW_UP = "That follow-up isn't in this account."
_NO_OUTCOME = "That outcome isn't in this account."
_PICK_DAY = "Pick a date, such as 2026-11-16."
_PICK_DAYS = "Pick 3, 7, 14 or 30 days."
_PICK_SNOOZE = "Pick a date after today."
_PICK_KIND = "Pick Note, Call, Meeting or Email."
_PICK_OUTCOME = "Pick Renewed, Expanded, Contracted or Churned."
_PICK_RENEWAL = "Pick the renewal date it answers."
_PICK_DECIDED = "Pick the date they decided."
_ARR_AMOUNT = "Enter the new ARR as an amount, such as $120,000."


def _journal_back(fields: Mapping[str, str]) -> str:
    """'today' when the form was sent from Today, else 'account'. Nothing else is a target."""
    return "today" if fields.get("back") == "today" else "account"


def _journal_redirect(back: str, account_id: str, anchor: str = "", **query: str) -> str:
    """Where a journal action goes: the Today page, or the account page at its section."""
    params = urlencode({name: value for name, value in query.items() if value})
    if back == "today":
        return f"/today?{params}" if params else "/today"
    target = f"/accounts/{_quoted_id(account_id)}"
    if params:
        target += f"?{params}"
    return f"{target}#{anchor}" if anchor else target


def _book_account(account_id: str) -> ScoredAccount | None:
    """The account as the book scores it now, or None when it isn't in the book (hidden too)."""
    return service.find_account(service.get_book(), account_id)


def _day_field(fields: Mapping[str, str], name: str, default: date, message: str) -> date:
    """The date a date box holds, the default when it is blank, or ValueError(message)."""
    text = fields.get(name, "").strip()
    if not text:
        return default
    day = journal_pages.parse_day(text)
    if day is None:
        raise ValueError(message)
    return day


def _days_from(text: str, message: str) -> int:
    """A whole number of days from a shortcut box, clamped to _DAY_LIMIT, or ValueError(message)."""
    try:
        days = int(text.strip())
    except ValueError:
        raise ValueError(message) from None
    return max(-_DAY_LIMIT, min(days, _DAY_LIMIT))


def _due_day(fields: Mapping[str, str], today: date) -> date:
    """A follow-up's due date: the date box, else today plus the days shortcut, else a week."""
    if fields.get("due", "").strip():
        return _day_field(fields, "due", today, _PICK_DAY)
    if fields.get("days", "").strip():
        return today + timedelta(days=_days_from(fields["days"], _PICK_DAY))
    return today + timedelta(days=journal.FOLLOW_UP_DEFAULT_DAYS)


def _snooze_day(fields: Mapping[str, str], today: date) -> date:
    """The day a snooze lasts until. A date box, when filled in, wins over the menu of days (the
    menu always posts a value, so the box must be able to override it); otherwise today plus one
    of the snooze choices."""
    if fields.get("until", "").strip():
        day = journal_pages.parse_day(fields["until"])
        if day is None:
            raise ValueError(_PICK_SNOOZE)
        return day
    if not fields.get("days", "").strip():
        raise ValueError(_PICK_SNOOZE)
    days = _days_from(fields["days"], _PICK_DAYS)
    if days not in journal.SNOOZE_CHOICES:
        raise ValueError(_PICK_DAYS)
    return today + timedelta(days=days)


def _note_kind(text: str) -> journal.TouchKind:
    try:
        return journal.TouchKind(text)
    except ValueError:
        raise ValueError(_PICK_KIND) from None


def _follow_up_text(fields: Mapping[str, str], scored: ScoredAccount) -> str:
    """The follow-up's text: as typed, or the account's next step (cut to 200) when blank."""
    text = fields.get("text", "")
    return text if text.strip() else journal_pages.cut_text(scored.suggested_action)


def _outcome_input(
    scored: ScoredAccount, fields: Mapping[str, str], today: date
) -> journal.OutcomeInput:
    """The outcome form's values, parsed. A blank renewal date is the account's, a blank decision
    date is today, and a blank new ARR is left out (not needed for a churn)."""
    try:
        kind = journal.OutcomeKind(fields.get("kind", ""))
    except ValueError:
        raise ValueError(_PICK_OUTCOME) from None
    renewal = _day_field(fields, "renewal_date", scored.account.renewal_date, _PICK_RENEWAL)
    decided = _day_field(fields, "decided_on", today, _PICK_DECIDED)
    arr_text = fields.get("arr_after", "").strip()
    try:
        arr_after = importing.parse_money(arr_text) if arr_text else None
    except ValueError:
        raise ValueError(_ARR_AMOUNT) from None
    return journal.OutcomeInput(
        kind=kind,
        renewal_date=renewal,
        decided_on=decided,
        arr_after=arr_after,
        note=fields.get("note", ""),
    )


async def _journal_action(
    request: Request,
    account_id: str,
    *,
    form: str,
    anchor: str,
    act: Callable[[ScoredAccount, Mapping[str, str], date], dict[str, str]],
    refused_anchor: str = "",
    item_id: str = "",
    shows_refusal: bool = True,
) -> Response:
    """The one path every journal form takes. The account must be in the book (404 otherwise).
    `act` makes the change and returns the fixed-code query for the redirect. A value it refuses
    (ValueError) re-renders the account page with 422 and the values as typed, next to their
    messages; from Today, or when no form on the page can show the message, it redirects to the
    account page with the refusal's fixed reason instead. An unknown note or follow-up is a 404
    with a message and a way back, never a 500."""
    scored = await run_in_threadpool(_book_account, account_id)
    if scored is None:
        return await run_in_threadpool(
            _missing, request, "Account not found", f"No account with id {account_id}."
        )
    fields = await _posted_fields(request)
    back = _journal_back(fields)
    today = date.today()
    try:
        query = await run_in_threadpool(act, scored, fields, today)
    except ValueError as error:
        message = str(error)
        code = journal_pages.why_code(message)
        if back == "today" or not shows_refusal or code in ("gone", "done"):
            return RedirectResponse(
                _journal_redirect("account", account_id, refused_anchor or anchor, why=code),
                status_code=303,
            )
        refusal = journal_pages.JournalRefusal(
            form=form,
            item_id=item_id,
            values=journal_pages.typed_values(form, fields),
            errors=journal_pages.field_errors(form, message),
        )
        return await run_in_threadpool(
            _render_account, request, account_id, journal_refusal=refusal, status_code=422
        )
    except LookupError as error:
        if back == "today":
            href, label = "/today", "Back to Today"
        else:
            href, label = f"/accounts/{_quoted_id(account_id)}", "Back to the account"
        return await run_in_threadpool(
            _missing, request, "Not found", str(error), back_href=href, back_label=label
        )
    return RedirectResponse(_journal_redirect(back, account_id, anchor, **query), status_code=303)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the feed scheduler with the app and stop it when the app shuts down."""
    service.start_feed_scheduler()
    try:
        yield
    finally:
        service.stop_feed_scheduler()


def create_app() -> FastAPI:
    """Build the web app: no API docs, a host allow-list, and security headers on every response."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_cross_site_write(request.method, request.headers):
            return PlainTextResponse(CROSS_SITE_MESSAGE, status_code=403)
        if is_cross_site_load(request.method, request.headers):
            return PlainTextResponse(CROSS_SITE_LOAD_MESSAGE, status_code=403)
        return await call_next(request)

    # Registered last so it wraps everything above, including the 403.
    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/static/"):
            # Revalidate every time (a cheap 304 locally) so an upgrade never runs stale JS or CSS.
            response.headers["Cache-Control"] = "no-cache"
        else:
            # Pages show the CSM's book. The browser keeps none of them, so Back or a shared
            # computer can't show one from its cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code != 404:
            return await http_exception_handler(request, exc)
        return await run_in_threadpool(
            _render,
            request,
            "not_found.html",
            page="Page not found",
            status_code=404,
            message="This page doesn't exist.",
        )

    @app.exception_handler(ConnectorError)
    def connector_error(request: Request, exc: ConnectorError) -> Response:
        """A book that can't load shows one message in the layout, with the way out (503)."""
        return _render(
            request,
            "connection_error.html",
            page="Couldn't load your book",
            status_code=503,
            message=exc.message,
        )

    @app.get("/")
    def my_book(
        request: Request,
        q: str = "",
        imported: str = "",
        removed: str = "",
        hidden: str = "",
        deleted: str = "",
    ) -> Response:
        """My book. Filters come from the query string, each one repeatable (motion=churn&
        motion=renewal): motion, health and, with web research, topic; view=list shows the
        table instead of cards. An htmx refresh gets only the results partial, not on a history
        restore. With Your data and nothing connected, this is the Setup page instead."""
        if _needs_setup():
            return RedirectResponse("/setup", status_code=303)
        partial = request.headers.get("HX-Request") == "true" and (
            "HX-History-Restore-Request" not in request.headers
        )
        if not partial:
            service.remember_book()  # today's score snapshot, which "Since last week" compares with
        today = date.today()
        book = service.get_book()
        brief = briefs.weekly_brief(book, as_of=today)
        params = request.query_params
        motions = _chosen(params.getlist("motion"), Motion)
        healths = _chosen(params.getlist("health"), Health)
        topics = _chosen(params.getlist("topic"), research.Topic)
        view = "list" if params.get("view") == "list" else "cards"
        flags = service.account_flags()
        ranks = {item.account.id: rank for rank, item in enumerate(book, start=1)}
        rows: list[tuple[int, ScoredAccount]] = [
            (ranks[item.account.id], item)
            for item in service.filter_book(book, None, q)
            if (not motions or item.motion in motions)
            and (not healths or item.health in healths)
            and (not topics or not topics.isdisjoint(flags.get(item.account.id, [])))
        ]
        first, second = summary_sentences(
            brief.counts_by_health[Health.AT_RISK],
            brief.arr_at_risk,
            brief.counts_by_health[Health.WATCH],
        )
        groups = _filter_groups(brief, flags, motions, healths, topics)
        journals = service.get_journal().all()
        chips = {
            item.account.id: journal_pages.book_chips(
                journals.get(item.account.id, journal.AccountJournal()), item.health, today
            )
            for item in book
        }
        since = (
            None
            if partial
            else journal_pages.since_view(
                service.week_changes(book, today),
                service.history_started(),
                today,
                limit=_SINCE_LIMIT,
            )
        )
        response = _render(
            request,
            "partials/book_results.html" if partial else "book.html",
            page="My book",
            active="book",
            book=not partial,
            summary_first=first,
            summary_second=second,
            filter_groups=groups,
            filtered=bool(motions or healths or topics),
            view=view,
            rows=rows,
            q=q,
            count_text=result_count_text(len(rows)),
            intro=not partial,
            oob=partial,
            duplicate_count=0 if partial else _duplicate_count(),
            import_notice=[] if partial else _import_notice(imported, removed),
            hidden_name="" if partial else _hidden_notice(hidden),
            deleted_name="" if partial else ("The account" if deleted == "account" else ""),
            flags=flags,
            journal_chips=chips,
            since=since,
        )
        response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
        return response

    @app.get("/brief")
    def weekly_brief(request: Request) -> HTMLResponse:
        """The weekly brief: where ARR sits, this week's call-outs, what changed since last
        week, the follow-ups due this week, the motions and the top ten."""
        today = date.today()
        book = service.get_book()
        brief = briefs.weekly_brief(book, as_of=today, top_n=10)
        week_follow_ups, more_follow_ups = journal_pages.week_follow_ups(
            journal.today_view(book, service.get_journal().all(), today),
            today,
            limit=_WEEK_FOLLOW_UPS,
        )
        first, second = summary_sentences(
            brief.counts_by_health[Health.AT_RISK],
            brief.arr_at_risk,
            brief.counts_by_health[Health.WATCH],
        )
        largest = max(brief.counts_by_motion.values(), default=0)
        motion_rows = [
            (
                _BRIEF_MOTION_LABELS[motion],
                brief.counts_by_motion[motion],
                formatting.count_bar_width(brief.counts_by_motion[motion], largest),
            )
            for motion in _MOTION_ORDER
        ]
        return _render(
            request,
            "brief.html",
            page="Weekly brief",
            active="brief",
            book=True,
            brief=brief,
            summary_first=first,
            summary_second=second,
            bar_label=_bar_label(brief.arr_by_health),
            health_order=formatting.BOOK_BAR_ORDER,
            motion_rows=motion_rows,
            motion_labels=_BRIEF_MOTION_LABELS,
            source=_brief_source(),
            since=journal_pages.since_view(
                service.week_changes(book, today),
                service.history_started(),
                today,
                limit=history.MAX_CHANGES,
            ),
            week_follow_ups=week_follow_ups,
            more_follow_ups=more_follow_ups,
        )

    @app.get("/onboarding")
    def onboarding(request: Request, missing: str = "") -> HTMLResponse:
        """Onboarding prep: each onboarding account with its day and plan, and a picker for any
        account. Every row's brief uses the same connector, config and date as the book."""
        connector = service.get_connector()
        config = load_scoring_config()
        as_of = date.today()
        book = service.get_book(connector, config, as_of)
        rows: list[tuple[ScoredAccount, OnboardingBrief]] = []
        for item in service.onboarding_accounts(book):
            brief = service.get_onboarding_brief(item.account.id, connector, config, as_of)
            if brief is not None:
                rows.append((item, brief))
        behind = sum(formatting.behind_on_plan(brief.milestones) for _, brief in rows)
        return _render(
            request,
            "onboarding.html",
            page="Onboarding prep",
            active="onboarding",
            book=True,
            summary=formatting.onboarding_summary(len(rows), behind),
            rows=rows,
            other_accounts=_other_accounts(book),
            missing=missing == "1",
        )

    @app.get("/onboarding/open")
    def open_onboarding(account: str = "") -> RedirectResponse:
        """Opens the brief for the account chosen in the picker. A missing or unknown id goes back
        to the picker with a message."""
        found = service.find_account(service.get_book(), account) if account else None
        if found is None:
            return RedirectResponse("/onboarding?missing=1", status_code=303)
        return RedirectResponse(f"/onboarding/{quote(found.account.id, safe='')}", status_code=303)

    @app.get("/onboarding/{account_id}")
    def onboarding_brief(request: Request, account_id: str) -> HTMLResponse:
        """One account's onboarding call brief: the plan, the agenda, the questions and signals."""
        brief = service.get_onboarding_brief(account_id)
        if brief is None:
            return _render(
                request,
                "not_found.html",
                page="Account not found",
                status_code=404,
                message=f"No account with id {account_id}.",
                book=True,
            )
        # The signals table scales its bars to the largest weight in the scoring config, as the
        # account page does, so the same signal is drawn the same width on both pages.
        longest = max(rule.weight for rule in load_scoring_config().signals.values())
        return _render(
            request,
            "onboarding_brief.html",
            page=f"{brief.account.name} onboarding brief",
            active="onboarding",
            book=True,
            brief=brief,
            longest=longest,
            plain_text=formatting.onboarding_plain_text(brief),
        )

    @app.get("/settings")
    def settings_home() -> RedirectResponse:
        """Settings opens on Appearance."""
        return RedirectResponse("/settings/appearance", status_code=303)

    @app.get("/settings/appearance")
    def appearance(request: Request, saved: str = "") -> HTMLResponse:
        return _render_appearance(request, load_settings(), saved=saved == "1")

    @app.post("/settings/appearance")
    async def save_appearance(request: Request) -> Response:
        """Save the four appearance choices. A choice that isn't valid re-renders the form with a
        422 and writes nothing. Cross-site posts never get here (see same_origin_writes)."""
        form = await request.form()
        chosen: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for field, (enum_type, message) in _APPEARANCE_FIELDS.items():
            try:
                chosen[field] = enum_type(form.get(field))
            except ValueError:
                errors[field] = message
        if errors:
            current = (await run_in_threadpool(load_settings)).model_copy(update=chosen)
            return await run_in_threadpool(
                _render_appearance, request, current, errors=errors, status_code=422
            )
        # Only the four appearance choices change; the data source is kept as it was.
        saved = (await run_in_threadpool(load_settings)).model_copy(update=chosen)
        await run_in_threadpool(save_settings, saved)
        return RedirectResponse("/settings/appearance?saved=1", status_code=303)

    @app.get("/settings/scoring")
    def scoring(request: Request, saved: str = "", defaults: str = "") -> HTMLResponse:
        """The scoring form, with its preview. ?defaults=1 shows the built-in defaults, unsaved."""
        if defaults == "1":
            draft, notice = ScoringConfig(), _SCORING_DEFAULTS_NOTICE
        else:
            draft = load_scoring_config()
            notice = _SCORING_SAVED_NOTICE if saved == "1" else ""
        return _render_scoring(
            request,
            values=form_values(draft),
            errors={},
            preview=_scoring_preview(draft),
            notice=notice,
        )

    @app.post("/settings/scoring/preview")
    async def scoring_preview(request: Request) -> HTMLResponse:
        """The preview partial for the posted form. htmx swaps it into the preview column."""
        fields = await _posted_fields(request)
        base = await run_in_threadpool(load_scoring_config)
        draft, errors = parse_scoring_form(fields, base)
        preview = (
            await run_in_threadpool(_scoring_preview, draft)
            if draft is not None
            else _preview_alerts(errors)
        )
        return templates.TemplateResponse(
            request, "settings/partials/scoring_preview.html", {"preview": preview}
        )

    @app.post("/settings/scoring")
    async def save_scoring(request: Request) -> Response:
        """Save the scoring form. A bad value re-renders the page with each message (422) and
        writes nothing. Cross-site posts never get here (see same_origin_writes)."""
        fields = await _posted_fields(request)
        base = await run_in_threadpool(load_scoring_config)
        draft, errors = parse_scoring_form(fields, base)
        if draft is None:
            # Keep what was typed, so the person fixes the one value instead of retyping the form.
            typed = {name: fields[name] for name in FIELD_NAMES if name in fields}
            return await run_in_threadpool(
                _render_scoring,
                request,
                values={**form_values(base), **typed},
                errors=errors,
                preview=_preview_alerts(errors),
                status_code=422,
            )
        await run_in_threadpool(save_scoring_config, draft)
        return RedirectResponse("/settings/scoring?saved=1", status_code=303)

    # Data & privacy: web research on or off, and clearing the local cache. The notices are
    # fixed words chosen by the server, never text from the URL.

    @app.get("/settings/privacy")
    def privacy(
        request: Request,
        research_choice: str = Query("", alias="research"),
        cleared: str = "",
        lookups: str = "",
        drafts: str = "",
        ai: str = "",
        key: str = "",
        deleted_all: str = "",
    ) -> HTMLResponse:
        if deleted_all == "1":
            notice = _deleted_all_notice()
        elif cleared == "1":
            notice = _cache_notice(lookups, drafts)
        elif ai in ("on", "off"):
            notice = _ai_notice(ai)
        elif key in _KEY_NOTICES:
            notice = _KEY_NOTICES[key]
        else:
            notice = _RESEARCH_NOTICES.get(research_choice, "")
        return _render_privacy(request, notice=notice)

    @app.post("/settings/privacy/research")
    async def save_web_research(request: Request) -> Response:
        """Turn web research on or off. Anything but on or off is refused (422) and writes
        nothing. Turning it off also removes every saved lookup, sample news included."""
        choice = (await _posted_fields(request)).get("research", "")
        if choice not in ("on", "off"):
            return await run_in_threadpool(
                _render_privacy,
                request,
                research_error=_RESEARCH_CHOICE_ERROR,
                status_code=422,
            )
        await run_in_threadpool(service.set_web_research, choice == "on")
        query = urlencode({"research": choice})
        return RedirectResponse(f"/settings/privacy?{query}", status_code=303)

    @app.post("/settings/privacy/clear-cache")
    def clear_cache() -> RedirectResponse:
        """Remove saved lookups and unfinished imports. Connections, accounts, edits and settings
        stay as they are."""
        cleared = service.clear_local_cache()
        query = urlencode(
            {"cleared": "1", "lookups": str(cleared.research), "drafts": str(cleared.drafts)}
        )
        return RedirectResponse(f"/settings/privacy?{query}", status_code=303)

    # Delete all local data: the confirmation page explains what goes and what stays. Nothing is
    # deleted by opening it; the POST deletes, and a cross-site POST is refused before it gets here.

    @app.get("/settings/privacy/delete-all")
    def delete_all_page(request: Request) -> HTMLResponse:
        """The confirmation page: what is deleted, what stays, and that it can't be undone."""
        return _render(
            request,
            "settings/delete_all.html",
            page="Delete all local data",
            active="settings",
        )

    @app.post("/settings/privacy/delete-all")
    def delete_all_local() -> RedirectResponse:
        """Delete all local data (service.delete_all_local_data). The notice on the next page
        names any environment variable still set, by name only, so the person can remove it."""
        service.delete_all_local_data()
        return RedirectResponse("/settings/privacy?deleted_all=1", status_code=303)

    # AI summaries and the OpenRouter key. The key is never echoed: not in a page, notice or URL.

    @app.post("/settings/privacy/ai")
    async def save_ai_summaries(request: Request) -> Response:
        """Turn AI summaries on or off, and save the model. Anything invalid re-renders the form
        (422) and nothing is saved. Turning them off also removes every saved summary."""
        fields = await _posted_fields(request)
        choice = fields.get("ai_summaries", "")
        model_text = fields.get("ai_model", "")
        choice_error = "" if choice in ("on", "off") else _AI_CHOICE_ERROR
        model = ""
        model_error = ""
        try:
            model = await run_in_threadpool(service.checked_ai_model, model_text)
        except ValueError as error:
            model_error = str(error)
        if choice_error or model_error:
            return await run_in_threadpool(
                _render_privacy,
                request,
                ai_choice=None if choice_error else choice == "on",
                ai_error=choice_error,
                ai_model_value=model_text,
                ai_model_error=model_error,
                status_code=422,
            )
        await run_in_threadpool(service.set_ai_model, model)
        await run_in_threadpool(service.set_ai_summaries, choice == "on")
        return RedirectResponse(f"/settings/privacy?{urlencode({'ai': choice})}", status_code=303)

    @app.post("/settings/privacy/ai/key")
    async def save_openrouter_key(request: Request) -> Response:
        """Store the OpenRouter key in the user profile. An invalid key re-renders the field with
        the reason (422) and an empty input; the key is never shown back."""
        fields = await _posted_fields(request)
        try:
            await run_in_threadpool(secrets.save_openrouter_key, fields.get("key", ""))
        except ValueError as error:
            return await run_in_threadpool(
                _render_privacy, request, key_error=str(error), status_code=422
            )
        except OSError:
            return await run_in_threadpool(
                _render_privacy, request, key_error=_KEY_SAVE_FAILED, status_code=500
            )
        return RedirectResponse("/settings/privacy?key=saved", status_code=303)

    @app.post("/settings/privacy/ai/key/delete")
    def remove_openrouter_key() -> RedirectResponse:
        """Remove the saved key file. A key set in the environment is not touched."""
        secrets.delete_openrouter_key()
        return RedirectResponse("/settings/privacy?key=removed", status_code=303)

    @app.post("/settings/privacy/ai/key/test")
    def test_openrouter_key(request: Request) -> HTMLResponse:
        """Ask OpenRouter about the saved key (GET /key) and say what it answers. The key itself
        is never shown."""
        try:
            info = service.check_openrouter_key()
        except summaries.SummaryError as error:
            return _render_privacy(request, key_result=error.message, key_result_ok=False)
        except ValueError as error:  # a bad base URL in the environment
            return _render_privacy(request, key_result=str(error), key_result_ok=False)
        if info.limit_remaining_usd is None:
            text = "The key works. It has no credit limit."
        else:
            text = f"The key works. ${info.limit_remaining_usd:.2f} of credit left."
        return _render_privacy(request, key_result=text)

    # Website lookups: every account's website at once, in a background job with progress.

    @app.get("/research")
    def research_page(request: Request, job: str = "") -> HTMLResponse:
        """The Website lookups page. ?job=<id> shows that look up's progress."""
        return _render_research(request, job_id=job)

    @app.post("/research/all")
    async def start_lookups(request: Request) -> Response:
        """Start looking up every account's website, and summarise them with AI if asked. htmx
        gets the progress panel; a plain form post goes to the page with the job in its URL. A
        refusal shows its reason in the panel (role=alert), with 200 so htmx swaps it in."""
        fields = await _posted_fields(request)
        try:
            job = await run_in_threadpool(
                service.start_bulk_research,
                summarise=fields.get("summarise") == "1",
                refresh=fields.get("refresh") == "1",
            )
        except (research.ResearchOff, summaries.SummaryError) as problem:
            if _is_htmx(request):
                return await run_in_threadpool(_job_panel, request, None, error=problem.message)
            return await run_in_threadpool(_render_research, request, error=problem.message)
        if _is_htmx(request):
            return await run_in_threadpool(_job_panel, request, job)
        return RedirectResponse(f"/research?{urlencode({'job': job.id})}", status_code=303)

    @app.get("/research/jobs/{job_id}")
    def research_job(request: Request, job_id: str) -> Response:
        """The progress panel for one look up: polled by htmx while it runs. Any id that isn't a
        look up's own 16-character id is a 404."""
        job = service.bulk_job(job_id) if _JOB_ID.fullmatch(job_id) else None
        if job is None:
            return _missing(request, "Look up not found", "That look up isn't known here.")
        return _job_panel(request, job)

    # One account's AI summary, asked for by its button. Errors show in the section.

    @app.post("/accounts/{account_id}/summarise")
    async def summarise_website(request: Request, account_id: str) -> Response:
        """Summarise this account's website with AI, or again when refresh=1. A failure shows its
        reason in the section; a plain form post goes back to the section on success."""
        account = await run_in_threadpool(_shown_account, account_id)
        if account is None:
            return await run_in_threadpool(
                _missing, request, "Account not found", f"No account with id {account_id}."
            )
        fields = await _posted_fields(request)
        lookup_error = ""
        ai_error = ""
        ai_error_link = False
        try:
            await run_in_threadpool(
                service.summarise_account, account_id, refresh=fields.get("refresh") == "1"
            )
        except LookupError:
            return await run_in_threadpool(
                _missing, request, "Account not found", f"No account with id {account_id}."
            )
        except research.ResearchError as problem:
            lookup_error = problem.message
        except summaries.SummaryError as problem:
            ai_error = problem.message
            ai_error_link = isinstance(
                problem, summaries.NoKey | summaries.KeyRejected | summaries.OutOfCredit
            )
        editable = await run_in_threadpool(_research_editable, account_id)
        result = await run_in_threadpool(service.cached_research, account_id)
        panel = await run_in_threadpool(
            _research_panel,
            account,
            editable=editable,
            result=result,
            error=lookup_error,
            ai_error=ai_error,
            ai_error_link=ai_error_link,
        )
        if _is_htmx(request):
            response = templates.TemplateResponse(
                request, "partials/research.html", {"panel": panel, "today_day": date.today()}
            )
            response.headers["Vary"] = "HX-Request"
            return response
        if lookup_error or ai_error:
            return await run_in_threadpool(
                _render_account, request, account_id, research_panel=panel
            )
        return RedirectResponse(f"/accounts/{_quoted_id(account_id)}#research", status_code=303)

    # Your data: add an account. It works in demo mode too, and switches the book to Your data.
    # /accounts/new is declared before /accounts/{account_id}, which would otherwise match "new".

    @app.get("/accounts/new")
    def new_account_page(request: Request) -> HTMLResponse:
        """Add an account by hand. Name, ARR and renewal date are required."""
        return _render_new(request, values={}, errors={})

    @app.post("/accounts/new")
    async def create_account_route(request: Request) -> Response:
        """Save a hand-added account. A refusal re-renders the form with each message (422)."""
        fields = await _posted_fields(request)
        typed = {name: fields.get(name, "") for name in EDITABLE_FIELDS}
        try:
            account = await run_in_threadpool(
                editing.create_account, service.get_store(), typed, today=date.today()
            )
        except editing.EditError as error:
            return await run_in_threadpool(
                _render_new,
                request,
                values=typed,
                errors=error.errors,
                refused=True,
                status_code=422,
            )
        settings = await run_in_threadpool(load_settings)
        if settings.source is DataSource.DEMO:
            await run_in_threadpool(
                save_settings, settings.model_copy(update={"source": DataSource.MINE})
            )
        await run_in_threadpool(service.reset_connectors)
        return RedirectResponse(f"/accounts/{_quoted_id(account.id)}?added=1", status_code=303)

    @app.get("/accounts/{account_id}")
    def account(
        request: Request,
        account_id: str,
        edited: str = "",
        reading: str = "",
        added: str = "",
        unlinked: str = "",
        contact: str = "",
        updates: str = "",
        journal_code: str = Query("", alias="journal"),
        day: str = "",
        undo: str = "",
        why: str = "",
    ) -> HTMLResponse:
        """One account's call prep: summary, next step, score breakdown and signal history. With
        Your data showing, it also has the edit link, where each figure came from, the notices,
        the readings added by hand, the contacts and the client updates box. The website section
        shows only what is already saved. The journal parts (follow-ups, notes, the trend and the
        renewal outcome) are on it too; `journal`, `day`, `undo` and `why` are its fixed codes."""
        status = (
            _account_status(added, edited, reading, unlinked)
            or _CONTACT_NOTICES.get(contact, "")
            or _UPDATES_NOTICES.get(updates, "")
            or journal_pages.notice_text(journal_code, day, date.today())
        )
        return _render_account(request, account_id, status=status, why=why, undo=undo)

    @app.post("/accounts/{account_id}/research")
    async def look_up_website(request: Request, account_id: str) -> Response:
        """Look up this one account's own website, because the CSM asked. With web research off,
        the section says so. A failed look up shows its reason in the section. htmx gets the
        section alone; a plain form post goes back to the section on the account page, or, when
        the look up failed, shows the account page with the section in its failed state."""
        account = await run_in_threadpool(_shown_account, account_id)
        if account is None:
            return await run_in_threadpool(
                _missing, request, "Account not found", f"No account with id {account_id}."
            )
        fields = await _posted_fields(request)
        result: research.SiteResearch | None = None
        error = ""
        try:
            result = await run_in_threadpool(
                service.research_account, account_id, refresh=fields.get("refresh") == "1"
            )
        except LookupError:
            return await run_in_threadpool(
                _missing, request, "Account not found", f"No account with id {account_id}."
            )
        except research.ResearchOff:
            pass  # the section shows the off state
        except research.ResearchError as problem:
            error = problem.message
        editable = await run_in_threadpool(_research_editable, account_id)
        panel = await run_in_threadpool(
            _research_panel, account, editable=editable, result=result, error=error
        )
        if request.headers.get("HX-Request") == "true":
            response = templates.TemplateResponse(
                request, "partials/research.html", {"panel": panel, "today_day": date.today()}
            )
            response.headers["Vary"] = "HX-Request"
            return response
        if error:
            return await run_in_threadpool(
                _render_account, request, account_id, research_panel=panel
            )
        return RedirectResponse(f"/accounts/{_quoted_id(account_id)}#research", status_code=303)

    # The journal. Each form posts here and returns to the account page or to Today (see
    # _journal_action). Demo data has its own journal, so these work with either data source.

    @app.get("/today")
    def today_page_route(
        request: Request,
        journal_code: str = Query("", alias="journal"),
        day: str = "",
        undo: str = "",
    ) -> HTMLResponse:
        """Today: what needs the CSM now. Follow-ups due, accounts that need attention, renewals
        awaiting their outcome, the next week's follow-ups and the snoozed accounts. Each action
        on the page returns here with a fixed status line; `undo` offers Undo for a follow-up just
        marked done."""
        today_day = date.today()
        page = journal_pages.today_page(service.today_view(today_day), today_day)
        found = journal_pages.find_follow_up(service.get_journal().all(), undo)
        return _render(
            request,
            "today.html",
            page="Today",
            active="today",
            book=True,
            today_page=page,
            notice=journal_pages.notice_text(journal_code, day, today_day),
            undo_account=found[0] if found is not None else "",
            undo_id=found[1].id if found is not None else "",
        )

    @app.post("/accounts/{account_id}/notes")
    async def add_note_route(request: Request, account_id: str) -> Response:
        """Add a note, or a call, meeting or email, dated today unless a date is given. A note
        needs its text; a refused one comes back with 422 and what was typed."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.get_journal().add_note(
                account_id,
                _note_kind(fields.get("kind", "")),
                fields.get("text", ""),
                _day_field(fields, "happened_on", today, _PICK_DAY),
                now=datetime.now(UTC),
                today=today,
            )
            return {"journal": "note-added"}

        return await _journal_action(request, account_id, form="note", anchor="notes", act=act)

    @app.post("/accounts/{account_id}/notes/{note_id}/edit")
    async def edit_note_route(request: Request, account_id: str, note_id: str) -> Response:
        """Change a note's kind, date or text. Its edit form stays open when a save is refused."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.get_journal().update_note(
                account_id,
                note_id,
                _note_kind(fields.get("kind", "")),
                fields.get("text", ""),
                _day_field(fields, "happened_on", today, _PICK_DAY),
                now=datetime.now(UTC),
                today=today,
            )
            return {"journal": "note-saved"}

        return await _journal_action(
            request, account_id, form="note-edit", anchor="notes", act=act, item_id=note_id
        )

    @app.post("/accounts/{account_id}/notes/{note_id}/remove")
    async def remove_note_route(request: Request, account_id: str, note_id: str) -> Response:
        """Delete a note from the account's journal."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            if not service.get_journal().remove_note(account_id, note_id):
                raise LookupError(_NO_NOTE)
            return {"journal": "note-removed"}

        return await _journal_action(
            request, account_id, form="", anchor="notes", act=act, shows_refusal=False
        )

    @app.post("/accounts/{account_id}/follow-ups")
    async def add_follow_up_route(request: Request, account_id: str) -> Response:
        """Add a follow-up: its text, and its due date from the date box, the days shortcut (from
        today) or a week from today. Blank text is the account's next step."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.get_journal().add_follow_up(
                account_id,
                _follow_up_text(fields, scored),
                _due_day(fields, today),
                now=datetime.now(UTC),
                today=today,
            )
            return {"journal": "follow-up-added"}

        return await _journal_action(
            request,
            account_id,
            form="follow-up",
            anchor="follow-ups",
            refused_anchor="next-step",
            act=act,
        )

    @app.post("/accounts/{account_id}/follow-ups/{follow_up_id}/done")
    async def done_follow_up_route(
        request: Request, account_id: str, follow_up_id: str
    ) -> Response:
        """Mark a follow-up done. Its status line offers Undo for it."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            done = service.get_journal().complete_follow_up(
                account_id, follow_up_id, now=datetime.now(UTC)
            )
            return {"journal": "done", "undo": done.id}

        return await _journal_action(
            request, account_id, form="", anchor="follow-ups", act=act, shows_refusal=False
        )

    @app.post("/accounts/{account_id}/follow-ups/{follow_up_id}/undo")
    async def reopen_follow_up_route(
        request: Request, account_id: str, follow_up_id: str
    ) -> Response:
        """Reopen a follow-up that was marked done, keeping its due date."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.get_journal().reopen_follow_up(account_id, follow_up_id)
            return {"journal": "reopened"}

        return await _journal_action(
            request, account_id, form="", anchor="follow-ups", act=act, shows_refusal=False
        )

    @app.post("/accounts/{account_id}/follow-ups/{follow_up_id}/move")
    async def move_follow_up_route(
        request: Request, account_id: str, follow_up_id: str
    ) -> Response:
        """Move an open follow-up to a new due date (the date box, or the days shortcut)."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            due = _due_day(fields, today)
            service.get_journal().move_follow_up(account_id, follow_up_id, due, today=today)
            return {"journal": "moved", "day": due.isoformat()}

        return await _journal_action(
            request,
            account_id,
            form="move",
            anchor="follow-ups",
            act=act,
            item_id=follow_up_id,
        )

    @app.post("/accounts/{account_id}/follow-ups/{follow_up_id}/remove")
    async def remove_follow_up_route(
        request: Request, account_id: str, follow_up_id: str
    ) -> Response:
        """Delete a follow-up from the account's journal."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            if not service.get_journal().remove_follow_up(account_id, follow_up_id):
                raise LookupError(_NO_FOLLOW_UP)
            return {"journal": "follow-up-removed"}

        return await _journal_action(
            request, account_id, form="", anchor="follow-ups", act=act, shows_refusal=False
        )

    @app.post("/accounts/{account_id}/snooze")
    async def snooze_route(request: Request, account_id: str) -> Response:
        """Leave the account off Today for 3, 7, 14 or 30 days, or until a date up to 90 days out.
        It records the account's health now: a worse health ends the snooze early."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            until = _snooze_day(fields, today)
            service.get_journal().snooze(
                account_id, until, scored.health, now=datetime.now(UTC), today=today
            )
            return {"journal": "snoozed", "day": until.isoformat()}

        return await _journal_action(
            request, account_id, form="snooze", anchor="next-step", act=act
        )

    @app.post("/accounts/{account_id}/wake")
    async def wake_route(request: Request, account_id: str) -> Response:
        """End the account's snooze now, so it is back on Today when it needs attention."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.get_journal().wake(account_id)
            return {"journal": "woke"}

        return await _journal_action(
            request, account_id, form="", anchor="next-step", act=act, shows_refusal=False
        )

    @app.post("/accounts/{account_id}/outcome")
    async def record_outcome_route(request: Request, account_id: str) -> Response:
        """Record how the renewal went: renewed, expanded, contracted or churned, with the new ARR
        if it is known. Wardmind keeps what it thought of the account at that moment."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.record_outcome(account_id, _outcome_input(scored, fields, today), today=today)
            return {"journal": "outcome-recorded"}

        return await _journal_action(request, account_id, form="outcome", anchor="outcome", act=act)

    @app.post("/accounts/{account_id}/outcomes/{outcome_id}/remove")
    async def remove_outcome_route(request: Request, account_id: str, outcome_id: str) -> Response:
        """Delete a recorded outcome (a mistake)."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            if not service.get_journal().remove_outcome(account_id, outcome_id):
                raise LookupError(_NO_OUTCOME)
            return {"journal": "outcome-removed"}

        return await _journal_action(
            request, account_id, form="", anchor="outcome", act=act, shows_refusal=False
        )

    # Your data: edit an account. Every route checks the id against the book first.

    @app.get("/accounts/{account_id}/edit")
    def edit_account_page(request: Request, account_id: str, reverted: str = "") -> HTMLResponse:
        """The edit form, with the values the book shows and each edit's connection value."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        _, account, prov = found
        return _render_edit(
            request,
            account,
            prov,
            values=_form_values(account),
            notice=_revert_status(prov, reverted),
        )

    @app.post("/accounts/{account_id}/edit")
    async def save_account(request: Request, account_id: str) -> Response:
        """Save the edit form. Every message comes back with the form (422), and nothing is kept."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        combined, account, prov = found
        fields = await _posted_fields(request)
        submitted = {name: fields[name] for name in EDITABLE_FIELDS if name in fields}
        try:
            changed = await run_in_threadpool(
                editing.edit_account,
                service.get_store(),
                combined,
                account_id,
                submitted,
                now=datetime.now(UTC),
            )
        except editing.EditError as error:
            return await run_in_threadpool(
                _render_edit,
                request,
                account,
                prov,
                values={**_form_values(account), **submitted},
                errors=error.errors,
                refused=True,
                status_code=422,
            )
        status = "1" if changed else _UNCHANGED
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'edited': status})}", status_code=303
        )

    @app.post("/accounts/{account_id}/revert/{field}")
    def revert_field(request: Request, account_id: str, field: str) -> Response:
        """Drop one edit, so the connection's value shows again."""
        if field not in EDITABLE_FIELDS:
            return _missing(request, "Field not found", _NO_FIELD)
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        combined, _, _ = found
        editing.revert_field(service.get_store(), combined, account_id, field)
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}/edit?{urlencode({'reverted': field})}",
            status_code=303,
        )

    @app.post("/accounts/{account_id}/readings")
    async def add_reading_route(request: Request, account_id: str) -> Response:
        """Add a signal reading by hand. Champion left reads its Yes or No choice; every other
        signal reads the value box. A refusal shows the message with the form (422)."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        combined, account, prov = found
        fields = await _posted_fields(request)
        reading = {
            "kind": fields.get("kind", ""),
            "value": fields.get("value", ""),
            "yes_no": fields.get("yes_no", "") or "yes",
        }

        def refuse(message: str) -> HTMLResponse:
            return _render_edit(
                request,
                account,
                prov,
                values=_form_values(account),
                reading=reading,
                reading_error=message,
                status_code=422,
            )

        try:
            kind = SignalKind(reading["kind"])
        except ValueError:
            return await run_in_threadpool(refuse, _UNKNOWN_SIGNAL)
        typed = reading["yes_no"] if kind is SignalKind.CHAMPION_LEFT else reading["value"]
        try:
            await run_in_threadpool(
                editing.add_reading,
                service.get_store(),
                combined,
                account_id,
                kind,
                typed,
                now=datetime.now(UTC),
            )
        except editing.EditError as error:
            return await run_in_threadpool(refuse, str(error))
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'reading': '1'})}", status_code=303
        )

    @app.post("/accounts/{account_id}/notices/{notice_id}/dismiss")
    def dismiss_notice(request: Request, account_id: str, notice_id: str) -> Response:
        """Dismiss one notice on an account. The id must belong to that account."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        store = service.get_store()
        if notice_id not in {notice.id for notice in store.notices(account_id)}:
            return _missing(request, "Message not found", _NO_NOTICE)
        store.dismiss_notice(notice_id)
        return RedirectResponse(f"/accounts/{_quoted_id(account_id)}", status_code=303)

    @app.post("/accounts/{account_id}/hide")
    def hide_account(request: Request, account_id: str) -> Response:
        """Hide an account, and every account linked to it. My book says so, with a way back."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        combined, _, _ = found
        editing.set_hidden(service.get_store(), combined, account_id, True)
        return RedirectResponse(f"/?{urlencode({'hidden': account_id})}", status_code=303)

    @app.get("/accounts/{account_id}/delete")
    def delete_account_page(request: Request, account_id: str) -> HTMLResponse:
        """The confirmation page. Only accounts added by hand can be deleted."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        _, account, prov = found
        if not prov.editable_in_place:
            return _refused(request, f"Can't delete {account.name}", _HAND_ADDED_ONLY)
        return _render(
            request,
            "accounts/delete.html",
            page=f"Delete {account.name}",
            active="book",
            account=account,
        )

    @app.post("/accounts/{account_id}/delete")
    def delete_account(request: Request, account_id: str) -> Response:
        """Delete a hand-added account with its readings. Anything synced is refused (422)."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        _, account, prov = found
        if not prov.editable_in_place:
            return _refused(request, f"Can't delete {account.name}", _HAND_ADDED_ONLY)
        try:
            editing.delete_account(service.get_store(), account_id)
        except editing.EditError as error:
            return _refused(request, f"Can't delete {account.name}", str(error))
        return RedirectResponse("/?deleted=account", status_code=303)

    @app.post("/accounts/{account_id}/unlink")
    def unlink_account(request: Request, account_id: str) -> Response:
        """Undo a link: the accounts it merged show separately again."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        combined, account, _ = found
        try:
            editing.unlink(service.get_store(), combined, account_id)
        except editing.EditError as error:
            return _refused(request, f"Can't unlink {account.name}", str(error))
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'unlinked': '1'})}", status_code=303
        )

    # Your data: contacts and client updates on the account page. Every route checks the account is
    # in Your data first (_editable_account), and every contact is checked to belong to it. A
    # refused form re-renders the account page with its values and messages (422).

    @app.post("/accounts/{account_id}/contacts")
    async def add_contact_route(request: Request, account_id: str) -> Response:
        """Add a contact to the account. Its name, job title and email are checked by the core."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        typed = contact_pages.contact_typed(await _posted_fields(request))
        try:
            await run_in_threadpool(
                contacts.add_contact,
                service.get_store(),
                account_id,
                contact_pages.contact_input(typed),
                source=ContactSource.YOU,
                now=datetime.now(UTC),
            )
        except ValueError as error:
            refusal = contact_pages.FormRefusal(
                form="add", values=typed, errors=contact_pages.errors_for(str(error))
            )
            return await run_in_threadpool(
                _render_account, request, account_id, refusal=refusal, status_code=422
            )
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'contact': 'added'})}", status_code=303
        )

    @app.get("/accounts/{account_id}/contacts/{contact_id}/remove")
    def remove_contact_page(request: Request, account_id: str, contact_id: str) -> Response:
        """The confirmation page: the contact's name, and what stays (readings already recorded)."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        _, account, _ = found
        contact = _find_contact(account_id, contact_id)
        if contact is None:
            return _missing(request, "Contact not found", _NO_CONTACT)
        return _render(
            request,
            "accounts/contact_remove.html",
            page=f"Remove {contact.name}",
            active="book",
            account=account,
            contact=contact,
        )

    @app.post("/accounts/{account_id}/contacts/{contact_id}/remove")
    def remove_contact_route(request: Request, account_id: str, contact_id: str) -> Response:
        """Delete the contact for good. Readings it already recorded stay in the history."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        if not contacts.remove_contact(service.get_store(), account_id, contact_id):
            return _missing(request, "Contact not found", _NO_CONTACT)
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'contact': 'removed'})}",
            status_code=303,
        )

    @app.post("/accounts/{account_id}/contacts/{contact_id}/edit")
    async def edit_contact_route(request: Request, account_id: str, contact_id: str) -> Response:
        """Save a contact's name, job title, email and main-contact flag."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        if await run_in_threadpool(_find_contact, account_id, contact_id) is None:
            return await run_in_threadpool(_missing, request, "Contact not found", _NO_CONTACT)
        typed = contact_pages.contact_typed(await _posted_fields(request))
        try:
            await run_in_threadpool(
                contacts.update_contact,
                service.get_store(),
                account_id,
                contact_id,
                contact_pages.contact_input(typed),
                source=ContactSource.YOU,
                now=datetime.now(UTC),
            )
        except ValueError as error:
            refusal = contact_pages.FormRefusal(
                form="edit",
                contact_id=contact_id,
                values=typed,
                errors=contact_pages.errors_for(str(error)),
            )
            return await run_in_threadpool(
                _render_account, request, account_id, refusal=refusal, status_code=422
            )
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'contact': 'saved'})}", status_code=303
        )

    @app.post("/accounts/{account_id}/contacts/{contact_id}/left")
    async def mark_left_route(request: Request, account_id: str, contact_id: str) -> Response:
        """Mark a contact as having left on the date given (today when the box is blank). The
        last main contact leaving records the Champion left signal (the core does that)."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        if await run_in_threadpool(_find_contact, account_id, contact_id) is None:
            return await run_in_threadpool(_missing, request, "Contact not found", _NO_CONTACT)
        fields = await _posted_fields(request)
        typed = {"left_on": fields.get("left_on", "")}
        today = date.today()
        left_on = today if not typed["left_on"].strip() else _parse_day(typed["left_on"])
        if left_on is None:
            problem = "Pick the date they left."
        elif left_on > today:
            problem = "The date they left can't be in the future."
        else:
            problem = ""
        if problem:
            refusal = contact_pages.FormRefusal(
                form="left",
                contact_id=contact_id,
                values=typed,
                errors={contact_pages.FORM: problem},
            )
            return await run_in_threadpool(
                _render_account, request, account_id, refusal=refusal, status_code=422
            )
        await run_in_threadpool(
            contacts.mark_left,
            service.get_store(),
            account_id,
            contact_id,
            left_on=left_on,
            source=ContactSource.YOU,
            now=datetime.now(UTC),
        )
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'contact': 'left'})}", status_code=303
        )

    @app.post("/accounts/{account_id}/contacts/{contact_id}/active")
    def undo_left_route(request: Request, account_id: str, contact_id: str) -> Response:
        """Undo a contact marked as left, a mistake. Refused (422) when another active contact
        already has the email."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        if _find_contact(account_id, contact_id) is None:
            return _missing(request, "Contact not found", _NO_CONTACT)
        try:
            contacts.mark_active(service.get_store(), account_id, contact_id, now=datetime.now(UTC))
        except ValueError as error:
            refusal = contact_pages.FormRefusal(
                form="active",
                contact_id=contact_id,
                errors={contact_pages.FORM: str(error)},
            )
            return _render_account(request, account_id, refusal=refusal, status_code=422)
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'contact': 'active'})}",
            status_code=303,
        )

    @app.post("/accounts/{account_id}/client-updates/on")
    async def turn_on_updates_route(request: Request, account_id: str) -> Response:
        """Turn client updates on for the account: who agreed and when (today by default)."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        fields = await _posted_fields(request)
        typed = {
            "agreed_with": fields.get("agreed_with", ""),
            "agreed_on": fields.get("agreed_on", ""),
        }
        try:
            agreed_on = _parse_day(typed["agreed_on"])
            if agreed_on is None:
                raise ValueError("Pick the date they agreed.")
            await run_in_threadpool(
                client_updates.opt_in,
                service.get_store(),
                account_id,
                agreed_with=typed["agreed_with"],
                agreed_on=agreed_on,
                now=_local_now(),
            )
        except ValueError as error:
            refusal = contact_pages.FormRefusal(
                form="on", values=typed, errors=contact_pages.opt_in_errors(str(error))
            )
            return await run_in_threadpool(
                _render_account, request, account_id, refusal=refusal, status_code=422
            )
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'updates': 'on'})}", status_code=303
        )

    @app.post("/accounts/{account_id}/client-updates/new-link")
    def new_link_route(request: Request, account_id: str) -> Response:
        """Give the client a new code. Its old link stops working."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        try:
            client_updates.new_code(service.get_store(), account_id, now=datetime.now(UTC))
        except LookupError:
            return _refused(request, "Client updates", _UPDATES_OFF)
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'updates': 'link'})}", status_code=303
        )

    @app.get("/accounts/{account_id}/client-updates/off")
    def turn_off_updates_page(request: Request, account_id: str) -> Response:
        """The confirmation page: turning updates off deletes the waiting ones from this client."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        _, account, _ = found
        return _render(
            request,
            "accounts/updates_off.html",
            page=f"Turn off client updates for {account.name}",
            active="book",
            account=account,
            pending=contact_pages.pending_for_account(service.get_store(), account_id),
        )

    @app.post("/accounts/{account_id}/client-updates/off")
    def turn_off_updates_route(request: Request, account_id: str) -> Response:
        """Turn client updates off: the consent goes, and the waiting updates with it."""
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        client_updates.opt_out(service.get_store(), account_id)
        return RedirectResponse(
            f"/accounts/{_quoted_id(account_id)}?{urlencode({'updates': 'off'})}", status_code=303
        )

    # Your data: possible duplicates. Both ids of a link or a dismissal are checked first.

    @app.get("/duplicates")
    def duplicates_page(request: Request, linked: str = "", dismissed: str = "") -> HTMLResponse:
        """Accounts from different connections that look like the same customer. ?linked= is the id
        of the account the link kept; its name is looked up, never read from the URL."""
        combined = _your_book()
        if combined is None:
            return _missing(request, "Page not found", _DUPLICATES_DEMO)
        status = ""
        if linked:
            status = _linked_notice(combined, linked)
        elif dismissed == "1":
            status = "Got it. They won't be suggested again."
        return _render_duplicates(request, combined, status=status)

    @app.post("/duplicates/link")
    async def link_duplicates(request: Request) -> Response:
        """Link two accounts into one, keeping the name of the one chosen."""
        combined = await run_in_threadpool(_your_book)
        if combined is None:
            return await run_in_threadpool(_missing, request, "Page not found", _DUPLICATES_DEMO)
        fields = await _posted_fields(request)
        first = fields.get("first", "")
        second = fields.get("second", "")
        primary = fields.get("primary", "")
        accounts = {
            account.id: account for account in await run_in_threadpool(combined.list_accounts)
        }
        if first not in accounts or second not in accounts:
            return await run_in_threadpool(
                _render_duplicates, request, combined, error=_NOT_IN_BOOK_FORM, status_code=422
            )
        kept = primary if primary in accounts else first
        try:
            await run_in_threadpool(
                editing.link_accounts, service.get_store(), combined, [first, second], primary
            )
        except editing.EditError as error:
            return await run_in_threadpool(
                _render_duplicates, request, combined, error=str(error), status_code=422
            )
        return RedirectResponse(f"/duplicates?{urlencode({'linked': kept})}", status_code=303)

    @app.post("/duplicates/dismiss")
    async def dismiss_duplicate_route(request: Request) -> Response:
        """Mark a suggested pair as not the same, so it is not suggested again."""
        combined = await run_in_threadpool(_your_book)
        if combined is None:
            return await run_in_threadpool(_missing, request, "Page not found", _DUPLICATES_DEMO)
        fields = await _posted_fields(request)
        first, second = sorted((fields.get("first", ""), fields.get("second", "")))
        suggestions = await run_in_threadpool(combined.duplicate_suggestions)
        suggested = {suggestion.account_ids for suggestion in suggestions}
        if (first, second) not in suggested:
            return await run_in_threadpool(
                _render_duplicates, request, combined, error=_NOT_SUGGESTED, status_code=422
            )
        await run_in_threadpool(
            editing.dismiss_duplicate, service.get_store(), combined, (first, second)
        )
        return RedirectResponse("/duplicates?dismissed=1", status_code=303)

    @app.get("/settings/connections")
    def connections(
        request: Request,
        saved: str = "",
        removed: str = "",
        refreshed: str = "",
        shown: str = "",
        feed_added: str = "",
        synced: str = "",
    ) -> HTMLResponse:
        """Where the data comes from, the connections, whether HubSpot works, and what it reads.
        After a feed is saved or synced, ?feed_added= or ?synced= names it and the notice says how
        its first sync or Sync now went."""
        notice = ""
        if feed_added:
            notice = _feed_notice(feed_added, added=True)
        elif synced:
            notice = _feed_notice(synced, added=False)
        elif saved == "source":
            notice = "Data source saved."
        elif saved == "token":
            notice = "Token saved."
        elif saved == "connection":
            notice = "Connection saved."
        elif shown == "1":
            notice = "Account shown again."
        elif removed == "token":
            notice = "Token removed."
        elif removed == "connection":
            # The connection is gone, so its name is not looked up: a generic line says so.
            notice = "The connection was removed."
        elif refreshed == "1":
            notice = "Data refreshed."
        return _render_connections(request, notice=notice)

    @app.post("/settings/connections/source")
    async def save_source(request: Request) -> Response:
        """Save the data source. Either choice can be saved, even with no connections yet; an
        invalid choice is refused (422) and nothing is saved."""
        fields = await _posted_fields(request)
        try:
            chosen = DataSource(fields.get("source", ""))
        except ValueError:
            return await run_in_threadpool(
                _render_connections,
                request,
                source_error="Choose your data or demo data.",
                status_code=422,
            )
        settings = await run_in_threadpool(load_settings)
        await run_in_threadpool(save_settings, settings.model_copy(update={"source": chosen}))
        await run_in_threadpool(service.reset_connectors)
        return RedirectResponse("/settings/connections?saved=source", status_code=303)

    @app.post("/settings/connections/token")
    async def save_token(request: Request) -> Response:
        """Store the token in the user profile and make sure a HubSpot connection exists (adding
        it is a no-op when it does). A token that isn't valid re-renders the field with the reason
        (422) and an empty input; the token is never shown back."""
        fields = await _posted_fields(request)
        try:
            await run_in_threadpool(secrets.save_hubspot_token, fields.get("token", ""))
        except ValueError as error:
            return await run_in_threadpool(
                _render_connections, request, token_error=str(error), status_code=422
            )
        except OSError:
            return await run_in_threadpool(
                _render_connections, request, token_error=_TOKEN_SAVE_FAILED, status_code=500
            )
        await run_in_threadpool(
            service.get_store().add_connection, ConnectionKind.HUBSPOT, "HubSpot"
        )
        await run_in_threadpool(service.reset_connectors)
        return RedirectResponse("/settings/connections?saved=token", status_code=303)

    @app.post("/settings/connections/token/delete")
    def delete_token() -> RedirectResponse:
        """Remove the saved token file. A token set in the environment is not touched."""
        secrets.delete_hubspot_token()
        service.reset_connectors()
        return RedirectResponse("/settings/connections?removed=token", status_code=303)

    @app.post("/settings/connections/test")
    def test_connection(request: Request) -> HTMLResponse:
        """Test the saved token with a fresh connector, whichever source is selected, so the
        person can check before switching."""
        token = secrets.get_hubspot_token()
        if token is None:
            return _connection_test_response(request, error="Save a HubSpot token first.")
        try:
            connector = HubSpotConnector(
                HubSpotClient(token, base_url=service.hubspot_api_base()), load_mapping()
            )
            check = connector.check_connection()
        except ConnectorError as error:
            return _connection_test_response(request, error=error.message)
        except ValueError as error:
            return _connection_test_response(request, error=str(error))
        return _connection_test_response(request, check=check)

    @app.post("/settings/connections/refresh")
    def refresh_data() -> RedirectResponse:
        """Fetch every connection's data again on the next load. Demo data has nothing to fetch.
        A ConnectorError shows the error page."""
        combined = service.get_combined()
        if combined is not None:
            combined.refresh()
        return RedirectResponse("/settings/connections?refreshed=1", status_code=303)

    @app.post("/settings/connections/use-demo")
    def use_demo() -> RedirectResponse:
        """Switch to demo data. Used by the error page, so it leaves every other setting alone."""
        save_settings(load_settings().model_copy(update={"source": DataSource.DEMO}))
        service.reset_connectors()
        return RedirectResponse("/", status_code=303)

    # Your data: setup and the import flow. Draft ids are checked before any file is touched.

    @app.get("/setup")
    def setup(request: Request) -> HTMLResponse:
        """Set up your book: the ways to bring accounts in. My book shows this while it is empty."""
        return _render(request, "setup.html", page="Set up your book", active="book")

    @app.get("/import/template.csv")
    def import_template() -> Response:
        """The CSV template: every import field as a column, with two example rows."""
        return Response(
            importing.template_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="wardmind-template.csv"'},
        )

    @app.get("/import")
    def import_form(
        request: Request, target: str = "", paste: str = "", back: str = ""
    ) -> HTMLResponse:
        """The upload-or-paste form. ?paste=1 puts the paste section first; ?target= picks the
        connection to update."""
        store = service.get_store()
        return _render_import_form(
            request,
            store,
            target=_chosen_target(store, target),
            back=_back_value(back),
            paste_first=paste == "1",
        )

    @app.post("/import")
    async def import_upload(request: Request) -> Response:
        """Store the upload (or the pasted cells) as a draft, then go to the mapping page.
        Exactly one of the two is needed. Nothing is stored when the file can't be read."""
        store = service.get_store()
        try:
            form = await request.form(max_part_size=_FORM_PART_LIMIT)
        except StarletteHTTPException as error:
            # Starlette reports a part over the limit as a 400 with a size message.
            too_large = error.status_code == 400 and "size" in str(error.detail)
            message = _too_large_paste() if too_large else _CHOOSE_FILE_OR_PASTE
            return await run_in_threadpool(
                _render_import_form,
                request,
                store,
                target="new",
                error=message,
                status_code=422,
            )
        upload = form.get("file")
        has_file = isinstance(upload, UploadFile) and bool(upload.filename)
        pasted = _text_field(form.get("pasted"))
        has_paste = bool(pasted.strip())
        target = await run_in_threadpool(_chosen_target, store, _text_field(form.get("target")))
        back = _back_value(_text_field(form.get("back")))
        new_name = _text_field(form.get("new_name")).strip()

        def refuse(message: str) -> HTMLResponse:
            return _render_import_form(
                request,
                store,
                target=target,
                new_name=new_name,
                pasted=pasted,
                back=back,
                error=message,
                status_code=422,
            )

        if has_file and has_paste:
            return await run_in_threadpool(refuse, _CHOOSE_ONE_SOURCE)
        if not has_file and not has_paste:
            return await run_in_threadpool(refuse, _CHOOSE_FILE_OR_PASTE)
        if target == import_flow.NEW_TARGET and new_name:
            try:
                import_flow.validate_name(new_name)
            except ValueError as error:
                return await run_in_threadpool(refuse, str(error))
        try:
            if has_file and isinstance(upload, UploadFile):
                # Read one byte past the limit: the reader then refuses an over-size file itself.
                data = await upload.read(importing.MAX_BYTES + 1)
                table = await run_in_threadpool(importing.read_upload, data, upload.filename or "")
                draft = await run_in_threadpool(
                    import_flow.save_draft,
                    store,
                    table=table,
                    file_bytes=data,
                    target=target,
                    new_name=new_name,
                    back=back,
                )
            else:
                table = await run_in_threadpool(importing.read_pasted, pasted)
                draft = await run_in_threadpool(
                    import_flow.save_draft,
                    store,
                    table=table,
                    pasted_text=pasted,
                    target=target,
                    new_name=new_name,
                    back=back,
                )
        except (importing.ImportError_, ValueError) as error:
            return await run_in_threadpool(refuse, str(error))
        return RedirectResponse(f"/import/{draft.id}", status_code=303)

    @app.get("/import/{draft_id}")
    def import_map(request: Request, draft_id: str) -> Response:
        """The mapping and preview for a draft: the CSM's last choices, else the suggested ones."""
        store = service.get_store()
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        return _render_mapping(
            request,
            store,
            draft,
            _current_mapping(store, draft),
            draft.date_order or "",
        )

    @app.post("/import/{draft_id}")
    async def import_update(request: Request, draft_id: str) -> Response:
        """The mapping page without JavaScript: save the choices and show the page again."""
        store = service.get_store()
        draft = await run_in_threadpool(import_flow.load_draft, store, draft_id)
        if draft is None:
            return await run_in_threadpool(_missing, request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields)
        order_text = fields.get("date_order", "")
        draft = await run_in_threadpool(
            import_flow.remember_choices,
            store,
            draft,
            mapping=mapping,
            date_order=_as_order(order_text),
        )
        return await run_in_threadpool(_render_mapping, request, store, draft, mapping, order_text)

    @app.post("/import/{draft_id}/preview")
    async def import_preview(request: Request, draft_id: str) -> Response:
        """The htmx partial: the examples and "What will happen", for the mapping as chosen."""
        store = service.get_store()
        draft = await run_in_threadpool(import_flow.load_draft, store, draft_id)
        if draft is None:
            return await run_in_threadpool(_missing, request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields)
        order_text = fields.get("date_order", "")
        draft = await run_in_threadpool(
            import_flow.remember_choices,
            store,
            draft,
            mapping=mapping,
            date_order=_as_order(order_text),
        )
        view = await run_in_threadpool(_mapping_view, store, draft, mapping, order_text)
        return templates.TemplateResponse(
            request,
            "import/partials/preview.html",
            {"draft_id": draft.id, "oob": True, **view},
        )

    @app.post("/import/{draft_id}/sheet")
    async def import_sheet(request: Request, draft_id: str) -> Response:
        """Switch to another sheet of the same Excel file, then back to the mapping page."""
        store = service.get_store()
        draft = await run_in_threadpool(import_flow.load_draft, store, draft_id)
        if draft is None:
            return await run_in_threadpool(_missing, request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        try:
            await run_in_threadpool(import_flow.reread_sheet, store, draft, fields.get("sheet", ""))
        except importing.ImportError_ as error:
            current = await run_in_threadpool(_current_mapping, store, draft)
            return await run_in_threadpool(
                _render_mapping,
                request,
                store,
                draft,
                current,
                draft.date_order or "",
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(f"/import/{draft_id}", status_code=303)

    @app.post("/import/{draft_id}/commit")
    async def import_commit(request: Request, draft_id: str) -> Response:
        """Import the draft. A refusal re-renders the mapping page with the reason (422)."""
        store = service.get_store()
        draft = await run_in_threadpool(import_flow.load_draft, store, draft_id)
        if draft is None:
            return await run_in_threadpool(_missing, request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields)
        order_text = fields.get("date_order", "")
        order = await run_in_threadpool(import_flow.resolve_date_order, draft, mapping, order_text)
        try:
            outcome = await run_in_threadpool(
                import_flow.commit,
                draft,
                mapping,
                order,
                store,
                now=datetime.now(UTC),
                today=date.today(),
            )
        except ValueError as error:
            return await run_in_threadpool(
                _render_mapping,
                request,
                store,
                draft,
                mapping,
                order_text,
                error=str(error),
                status_code=422,
            )
        settings = await run_in_threadpool(load_settings)
        if settings.source is DataSource.DEMO:
            await run_in_threadpool(
                save_settings, settings.model_copy(update={"source": DataSource.MINE})
            )
        await run_in_threadpool(service.reset_connectors)
        query = urlencode({"imported": outcome.connection.id, "removed": outcome.removed})
        return RedirectResponse(f"/?{query}", status_code=303)

    @app.post("/import/{draft_id}/cancel")
    def import_cancel(request: Request, draft_id: str) -> Response:
        """Delete the draft and go back to where the import started."""
        store = service.get_store()
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        import_flow.delete_draft(store, draft_id)
        if draft.back == import_flow.BACK_CONNECTIONS:
            return RedirectResponse("/settings/connections", status_code=303)
        return RedirectResponse("/setup", status_code=303)

    @app.get("/import/{draft_id}/problems.csv")
    def import_problems(request: Request, draft_id: str) -> Response:
        """The rows with problems: their own cells and a Problems column, to fix and re-import."""
        store = service.get_store()
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        mapping = _current_mapping(store, draft)
        order = import_flow.resolve_date_order(draft, mapping, draft.date_order)
        result = import_flow.preview(
            draft, mapping, order, store, now=datetime.now(UTC), today=date.today()
        )
        return Response(
            importing.issues_csv(draft.table, result.issues),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="rows-with-problems.csv"'},
        )

    # Your connections: include, rename, remove and show again. Every id is checked first.

    @app.post("/settings/connections/{connection_id}/enabled")
    async def set_connection_enabled(request: Request, connection_id: str) -> Response:
        """Include a connection in the book, or leave it out. The checkbox submits this form."""
        store = service.get_store()
        config = await run_in_threadpool(store.get_connection, connection_id)
        if config is None:
            return await run_in_threadpool(
                _missing, request, "Connection not found", _NO_CONNECTION
            )
        fields = await _posted_fields(request)
        enabled = fields.get("enabled") == "1"
        await run_in_threadpool(
            store.update_connection, config.model_copy(update={"enabled": enabled})
        )
        return RedirectResponse("/settings/connections?saved=connection", status_code=303)

    @app.post("/settings/connections/{connection_id}/rename")
    async def rename_connection(request: Request, connection_id: str) -> Response:
        """Rename a spreadsheet connection. A blank name, or one over 60 characters, is refused."""
        store = service.get_store()
        config = await run_in_threadpool(store.get_connection, connection_id)
        if config is None:
            return await run_in_threadpool(
                _missing, request, "Connection not found", _NO_CONNECTION
            )
        if config.kind is not ConnectionKind.SPREADSHEET:
            return await run_in_threadpool(
                _render_connections,
                request,
                rename_errors={connection_id: "Only spreadsheets can be renamed."},
                status_code=422,
            )
        fields = await _posted_fields(request)
        typed = fields.get("name", "")
        try:
            name = import_flow.validate_name(typed)
        except ValueError as error:
            return await run_in_threadpool(
                _render_connections,
                request,
                rename_errors={connection_id: str(error)},
                rename_values={connection_id: typed},
                status_code=422,
            )
        await run_in_threadpool(store.update_connection, config.model_copy(update={"name": name}))
        return RedirectResponse("/settings/connections?saved=connection", status_code=303)

    @app.get("/settings/connections/{connection_id}/remove")
    def remove_connection_page(request: Request, connection_id: str) -> HTMLResponse:
        """The confirmation page. It says exactly what leaves the book."""
        store = service.get_store()
        config = store.get_connection(connection_id)
        if config is None:
            return _missing(request, "Connection not found", _NO_CONNECTION)
        line = next(
            (
                item
                for item in _connection_lines(store, _combined_or_none())
                if item.config.id == config.id
            ),
            None,
        )
        count = line.accounts if line is not None else None
        leaving = f"Its {_accounts(count)}" if count is not None else "Its accounts"
        feed = config.feed
        if feed is not None and feed.role is FeedRole.SIGNALS:
            # A SIGNALS feed owns no accounts: its readings leave, the accounts stay.
            leaving = f"The readings it added to {_accounts(feed.status.accounts)}"
        is_updates = feed is not None and feed.role is FeedRole.CLIENT_UPDATES
        return _render(
            request,
            "settings/remove.html",
            page=f"Remove {config.name}",
            active="settings",
            config=config,
            leaving=f"{leaving} and their history",
            is_hubspot=config.kind is ConnectionKind.HUBSPOT,
            is_sheet=config.kind is ConnectionKind.SPREADSHEET,
            is_feed=config.kind is ConnectionKind.FEED,
            is_updates=is_updates,
        )

    @app.post("/settings/connections/{connection_id}/remove")
    def remove_connection(request: Request, connection_id: str) -> Response:
        """Remove a connection and what came with it. HubSpot's saved token goes too."""
        store = service.get_store()
        config = store.get_connection(connection_id)
        if config is None:
            return _missing(request, "Connection not found", _NO_CONNECTION)
        service.remove_connection(config.id)
        if config.kind is ConnectionKind.HUBSPOT:
            secrets.delete_hubspot_token()
        service.reset_connectors()
        return RedirectResponse("/settings/connections?removed=connection", status_code=303)

    @app.post("/settings/connections/hidden/{account_id}/show")
    async def show_hidden_account(request: Request, account_id: str) -> Response:
        """Bring a hidden account back into the book."""
        store = service.get_store()
        if account_id not in await run_in_threadpool(store.hidden):
            return await run_in_threadpool(_missing, request, "Account not found", _NOT_HIDDEN)
        connector = await run_in_threadpool(_combined_or_none) or CombinedConnector([], store)
        await run_in_threadpool(editing.set_hidden, store, connector, account_id, False)
        return RedirectResponse("/settings/connections?shown=1", status_code=303)

    # Data feeds: Sync now, then the Add a connection catalog, its setup card and the matching
    # page. Every id is checked before it is used; every POST goes through the write check above.

    @app.post("/settings/connections/{connection_id}/sync")
    def sync_connection(request: Request, connection_id: str) -> Response:
        """Sync one feed now, then back to Connections with what happened. A source that can't be
        read keeps the last good data and says why."""
        store = service.get_store()
        config = store.get_connection(connection_id)
        if config is None or config.kind is not ConnectionKind.FEED:
            return _missing(
                request,
                "Connection not found",
                _NO_CONNECTION,
                back_href="/settings/connections",
                back_label="Back to Connections",
            )
        if _feeds_demo():
            return _feeds_demo_page(request, "Sync now", status_code=422)
        service.sync_feed_now(config.id)
        return RedirectResponse(
            f"/settings/connections?{urlencode({'synced': config.id})}", status_code=303
        )

    @app.get("/connections/add")
    def add_connection_catalog(request: Request, q: str = "") -> Response:
        """The catalog: the built-in flows, then one card per provider. ?category= (repeatable)
        and ?q= filter it. An htmx refresh gets only the results, not on a history restore."""
        if _feeds_demo():
            return _feeds_demo_page(request, "Add a connection")
        store = service.get_store()
        chosen = _chosen(request.query_params.getlist("category"), Category)
        cards = feed_pages.catalog_cards(store.connections())
        shown = feed_pages.filter_cards(cards, chosen, q)
        partial = _is_htmx(request) and "HX-History-Restore-Request" not in request.headers
        response = _render(
            request,
            "connections/partials/add_results.html" if partial else "connections/add.html",
            page="Add a connection",
            active="settings",
            cards=shown,
            categories=feed_pages.category_options(cards, chosen),
            filtered=bool(chosen),
            q=q,
            count_text=feed_pages.connections_word(len(shown)),
            oob=partial,
        )
        response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
        return response

    @app.get("/connections/add/{provider}")
    def add_connection_setup(
        request: Request, provider: str, connection: str = "", role: str = ""
    ) -> Response:
        """The setup card for one provider. ?connection= changes a feed's settings: the form starts
        from what was saved, and its key stays unless a new one is typed. ?role= preselects a role
        the provider offers (the client update form's links use it)."""
        info = feed_catalog.provider_info(provider)
        if info is None:
            return _missing(
                request,
                "Add a connection",
                _NO_PROVIDER,
                back_href="/connections/add",
                back_label="Back to Add a connection",
            )
        if _feeds_demo():
            return _feeds_demo_page(request, info.name)
        existing = None
        if connection:
            existing = service.get_store().get_connection(connection)
            if (
                existing is None
                or existing.kind is not ConnectionKind.FEED
                or existing.feed is None
                or existing.feed.provider != info.id
            ):
                return _missing(
                    request,
                    "Connection not found",
                    _NO_CONNECTION,
                    back_href="/settings/connections",
                    back_label="Back to Connections",
                )
        form = feed_pages.initial_setup(info, existing)
        if existing is None and role:
            form = feed_pages.preselect_role(form, info, role)
        return _render_setup(request, info, form, errors={})

    @app.post("/connections/add/{provider}")
    async def add_connection_start(request: Request, provider: str) -> Response:
        """Test the source with the values given. When it works, a draft is kept and the matching
        page opens (303). A refusal re-renders the card (422) with the message by its field, or at
        the top; every value comes back except the key. The key is only sent to the address it was
        saved for (setup.start_draft decides)."""
        info = feed_catalog.provider_info(provider)
        if info is None:
            return await run_in_threadpool(
                _missing,
                request,
                "Add a connection",
                _NO_PROVIDER,
                back_href="/connections/add",
                back_label="Back to Add a connection",
            )
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(_feeds_demo_page, request, info.name, status_code=422)
        fields = await _posted_fields(request)
        form = feed_pages.SetupForm(
            name=fields.get("name", ""),
            role=fields.get("role", ""),
            schedule=fields.get("schedule", ""),
            match_by=fields.get("match_by", ""),
            values={
                item.key: fields.get(item.key, "")
                for item in info.fields
                if item.kind is not FieldKind.SECRET
            },
            connection=fields.get("connection", ""),
        )
        errors: dict[str, str] = {}
        role = _choice(FeedRole, form.role, info.roles[0])
        if role is None:
            errors["role"] = "Pick one of the choices shown."
        schedule = _choice(Schedule, form.schedule, info.schedules[0])
        if schedule is None:
            errors["schedule"] = "Pick one of the sync schedules offered."
        match_by = _choice(MatchBy, form.match_by, MatchBy.AUTO)
        if match_by is None:
            errors["match_by"] = "Pick one of the matching choices."
        top_error = _NOTHING_SAVED if errors else ""
        if not errors:
            secret_name = feed_pages.secret_key(info)
            secret = fields.get(secret_name, "") if secret_name else ""
            try:
                draft = await run_in_threadpool(
                    feed_setup.start_draft,
                    service.get_store(),
                    info.id,
                    name=form.name,
                    role=role,
                    values=dict(form.values),
                    secret=secret or None,
                    schedule=schedule,
                    match_by=match_by,
                    connection_id=form.connection or None,
                    now=datetime.now(UTC),
                )
            except ValueError as error:
                message = str(error)
                key = feed_pages.field_for_error(info, message)
                if key:
                    errors[key] = message
                    top_error = _NOTHING_SAVED
                else:
                    top_error = message
            except FeedError as error:
                top_error = error.message
            else:
                return RedirectResponse(f"/connections/feeds/{draft.id}/match", status_code=303)
        return await run_in_threadpool(
            _render_setup, request, info, form, errors=errors, top_error=top_error, status_code=422
        )

    @app.get("/connections/feeds/{draft_id}/match")
    def feed_match(request: Request, draft_id: str) -> Response:
        """The matching page for a feed being set up, with the columns found and pre-matched."""
        if _feeds_demo():
            return _feeds_demo_page(request, "Match your columns")
        store = service.get_store()
        draft = feed_setup.load_draft(store, draft_id, now=datetime.now(UTC))
        if draft is None:
            return _missing_setup(request)
        return _render_match(
            request, draft, feed_setup.suggested_mapping(draft), draft.date_order or ""
        )

    @app.post("/connections/feeds/{draft_id}/match")
    async def feed_match_update(request: Request, draft_id: str) -> Response:
        """The matching page without JavaScript: show it again with the choices as posted."""
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(
                _feeds_demo_page, request, "Match your columns", status_code=422
            )
        draft = await run_in_threadpool(
            feed_setup.load_draft, service.get_store(), draft_id, now=datetime.now(UTC)
        )
        if draft is None:
            return await run_in_threadpool(_missing_setup, request)
        fields = await _posted_fields(request)
        return await run_in_threadpool(
            _render_match,
            request,
            draft,
            _submitted_mapping(fields, draft.role),
            fields.get("date_order", ""),
        )

    @app.post("/connections/feeds/{draft_id}/preview")
    async def feed_match_preview(request: Request, draft_id: str) -> Response:
        """The htmx partial: the examples and "What will happen", for the mapping as posted."""
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(
                _feeds_demo_page, request, "Match your columns", status_code=422
            )
        draft = await run_in_threadpool(
            feed_setup.load_draft, service.get_store(), draft_id, now=datetime.now(UTC)
        )
        if draft is None:
            return await run_in_threadpool(_missing_setup, request)
        fields = await _posted_fields(request)
        view = await run_in_threadpool(
            _match_view, draft, _submitted_mapping(fields, draft.role), fields.get("date_order", "")
        )
        return templates.TemplateResponse(
            request,
            "connections/partials/match_preview.html",
            {"oob": True, **view},
        )

    @app.post("/connections/feeds/{draft_id}/commit")
    async def feed_match_commit(request: Request, draft_id: str) -> Response:
        """Save the feed from its draft, and run its first sync. A refusal re-renders the page
        with the reason (422). Success goes to Connections, which says how the first sync went."""
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(
                _feeds_demo_page, request, "Match your columns", status_code=422
            )
        store = service.get_store()
        draft = await run_in_threadpool(
            feed_setup.load_draft, store, draft_id, now=datetime.now(UTC)
        )
        if draft is None:
            return await run_in_threadpool(_missing_setup, request)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields, draft.role)
        date_text = fields.get("date_order", "")
        try:
            config = await run_in_threadpool(
                feed_setup.commit_draft,
                store,
                draft,
                mapping,
                date_text or None,
                book_accounts=service.feed_book_accounts,
                now=datetime.now(UTC),
                today=date.today(),
            )
        except ValueError as error:
            return await run_in_threadpool(
                _render_match, request, draft, mapping, date_text, error=str(error), status_code=422
            )
        await run_in_threadpool(service.reset_connectors)
        return RedirectResponse(
            f"/settings/connections?{urlencode({'feed_added': config.id})}", status_code=303
        )

    @app.post("/connections/feeds/{draft_id}/cancel")
    def feed_match_cancel(request: Request, draft_id: str) -> Response:
        """Delete the set-up and its saved key, and go back to the catalog."""
        store = service.get_store()
        if feed_setup.load_draft(store, draft_id, now=datetime.now(UTC)) is None:
            return _missing_setup(request)
        feed_setup.cancel_draft(store, draft_id)
        return RedirectResponse("/connections/add", status_code=303)

    # Client updates: the review page, the set-up page, and the feed's check. Demo mode explains
    # that they belong to Your data. The kind filter travels in the query string, so a refused
    # accept comes back to the same filter.

    @app.get("/client-updates")
    def client_updates_page(
        request: Request,
        done: str = "",
        account: str = "",
        contact: str = "",
        checked: str = "",
        discarded: str = "",
    ) -> HTMLResponse:
        """The pending updates, one card each, filtered by kind (?kind=, repeatable). ?done= names
        what the last accept or reject did; the names are looked up, never read from the URL.
        ?discarded= is how many stale updates Discard them removed."""
        if _feeds_demo():
            return _updates_demo_page(request)
        chosen = _chosen(request.query_params.getlist("kind"), UpdateKind)
        notice = "Checked for new responses." if checked == "1" else ""
        if discarded.isdigit():
            notice = contact_pages.discarded_text(int(discarded))
        elif done and not notice:
            notice = _done_notice(done, account, contact)
        return _render_updates(request, chosen=chosen, notice=notice)

    @app.post("/client-updates/discard")
    def discard_gone_route(request: Request) -> Response:
        """Reject the waiting updates of accounts no longer in the book. Their text is deleted, as
        for any reject; nothing is applied to a contact."""
        if _feeds_demo():
            return _updates_demo_page(request, status_code=422)
        store = service.get_store()
        now = datetime.now(UTC)
        discarded = 0
        for update in contact_pages.gone_updates(store, set(service.your_book_accounts())):
            try:
                client_updates.reject(store, update.id, now=now)
            except LookupError:
                continue
            discarded += 1
        return RedirectResponse(f"/client-updates?{urlencode({'discarded': discarded})}", 303)

    @app.post("/client-updates/{update_id}/accept")
    async def accept_update_route(request: Request, update_id: str) -> Response:
        """Apply one update the CSM has confirmed, then back to the page with what it did. A
        refusal shows its message on the card (422) with the values as typed."""
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(_updates_demo_page, request, status_code=422)
        store = service.get_store()
        update = await run_in_threadpool(_pending_update, store, update_id)
        if update is None:
            return await run_in_threadpool(_update_gone, request)
        chosen = _chosen(request.query_params.getlist("kind"), UpdateKind)
        typed = contact_pages.update_typed(await _posted_fields(request))
        before_contacts = await run_in_threadpool(contacts.contacts_for, store, update.account_id)
        before = {contact.id for contact in before_contacts}
        try:
            left_on = None
            if update.kind is UpdateKind.LEFT and typed["left_on"].strip():
                left_on = _parse_day(typed["left_on"])
                if left_on is None:
                    raise ValueError("Pick the date they left.")
                if left_on > date.today():
                    raise ValueError("The date they left can't be in the future.")
            await run_in_threadpool(
                client_updates.accept,
                store,
                update_id,
                values=contact_pages.update_values(update.kind, typed),
                contact_id=typed["contact_id"] or None,
                left_on=left_on,
                now=datetime.now(UTC),
            )
        except LookupError:
            return await run_in_threadpool(_update_gone, request)
        except ValueError as error:
            return await run_in_threadpool(
                _render_updates,
                request,
                chosen=chosen,
                errors={update_id: str(error)},
                typed={update_id: typed},
                status_code=422,
            )
        after = await run_in_threadpool(contacts.contacts_for, store, update.account_id)
        if update.kind is UpdateKind.ADD:
            added = [contact.id for contact in after if contact.id not in before]
            contact_id = added[0] if added else ""
        else:
            contact_id = typed["contact_id"]
        query = [("kind", kind.value) for kind in UpdateKind if kind in chosen]
        query += [
            ("done", update.kind.value),
            ("account", update.account_id),
            ("contact", contact_id),
        ]
        return RedirectResponse(f"/client-updates?{urlencode(query)}", status_code=303)

    @app.post("/client-updates/{update_id}/reject")
    async def reject_update_route(request: Request, update_id: str) -> Response:
        """Reject one update: its text is deleted and nothing is applied."""
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(_updates_demo_page, request, status_code=422)
        store = service.get_store()
        if await run_in_threadpool(_pending_update, store, update_id) is None:
            return await run_in_threadpool(_update_gone, request)
        chosen = _chosen(request.query_params.getlist("kind"), UpdateKind)
        try:
            await run_in_threadpool(client_updates.reject, store, update_id, now=datetime.now(UTC))
        except LookupError:
            return await run_in_threadpool(_update_gone, request)
        query = [("kind", kind.value) for kind in UpdateKind if kind in chosen]
        query.append(("done", "reject"))
        return RedirectResponse(f"/client-updates?{urlencode(query)}", status_code=303)

    @app.post("/client-updates/check")
    def check_responses_route(request: Request) -> Response:
        """Read every client update form's responses now (each feed's Sync now)."""
        if _feeds_demo():
            return _updates_demo_page(request, status_code=422)
        store = service.get_store()
        for config in store.connections():
            feed = config.feed
            if config.kind is ConnectionKind.FEED and feed is not None:
                if feed.role is FeedRole.CLIENT_UPDATES:
                    service.sync_feed_now(config.id)
        return RedirectResponse(f"/client-updates?{urlencode({'checked': '1'})}", status_code=303)

    @app.get("/client-updates/setup")
    def client_updates_setup_page(request: Request, saved: str = "") -> HTMLResponse:
        """Set the update form up once: its questions, the ways to read its responses, and the
        pre-filled link. ?saved= says what the last save did."""
        if _feeds_demo():
            return _updates_demo_page(request)
        notice = {"link": "Pre-filled link saved.", "cleared": "Pre-filled link cleared."}
        return _render_updates_setup(request, notice=notice.get(saved, ""))

    @app.post("/client-updates/setup/link")
    async def save_form_link_route(request: Request) -> Response:
        """Save the form's pre-filled link. A link that isn't one (422) is refused with why."""
        if await run_in_threadpool(_feeds_demo):
            return await run_in_threadpool(_updates_demo_page, request, status_code=422)
        typed = (await _posted_fields(request)).get("link", "")
        try:
            await run_in_threadpool(client_updates.set_form_link, service.get_store(), typed)
        except ValueError as error:
            return await run_in_threadpool(
                _render_updates_setup,
                request,
                link_value=typed,
                link_error=str(error),
                status_code=422,
            )
        return RedirectResponse("/client-updates/setup?saved=link", status_code=303)

    @app.post("/client-updates/setup/link/clear")
    def clear_form_link_route(request: Request) -> Response:
        """Forget the pre-filled link. Clients' own links stop showing until a new one is saved."""
        if _feeds_demo():
            return _updates_demo_page(request, status_code=422)
        client_updates.set_form_link(service.get_store(), "")
        return RedirectResponse("/client-updates/setup?saved=cleared", status_code=303)

    return app
