"""The local web UI: a FastAPI app that answers only for 127.0.0.1 and localhost.

Routes are thin. Account data comes from `wardmind.core.service`, the same entry point the CLI
and the MCP server use.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import sys
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
    audit,
    books,
    brand,
    briefs,
    bulk,
    client_updates,
    contacts,
    editing,
    growth,
    history,
    import_flow,
    importing,
    insights,
    journal,
    logos,
    mailbox,
    negotiation,
    note_templates,
    research,
    rules,
    secrets,
    service,
    signal_info,
    summaries,
    vault,
)
from wardmind.core import forecast as renewal_forecast
from wardmind.core.client_updates import ClientUpdate, UpdateKind, UpdateStatus
from wardmind.core.config import (
    MAX_CUSTOM_SIGNALS,
    CustomSignal,
    ScoringConfig,
    load_scoring_config,
    save_scoring_config,
)
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
from wardmind.core.models import Account, Health, Motion, ScoredAccount, SignalKind, signal_key
from wardmind.core.onboarding import OnboardingBrief
from wardmind.core.onboarding import onboarding_brief as build_onboarding_brief
from wardmind.core.scoring import latest_signals
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
from wardmind.web import (
    contact_pages,
    custom_signals_form,
    feed_pages,
    formatting,
    journal_pages,
    lock,
    renewal_pages,
    session,
    settings_activity,
    settings_backup,
    settings_followups,
    settings_note_templates,
    settings_overview,
    today_pages,
)
from wardmind.web import runway as runway_geometry
from wardmind.web.scoring_form import (
    FIELD_NAMES,
    UNIT_HINTS,
    form_values,
    parse_scoring_form,
    scoring_output,
    slider_max,
)

_log = logging.getLogger(__name__)

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
    # A page opened from Wardmind (a customer's website) gets no handle on Wardmind's window.
    "Cross-Origin-Opener-Policy": "same-origin",
    # Wardmind never needs these; a page can't ask for them even if script got in.
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), hid=(), "
        "bluetooth=(), clipboard-read=(), display-capture=()"
    ),
}
MAX_BODY_BYTES = 16 * 1024 * 1024  # the largest request body most routes accept
MAX_RESTORE_BODY_BYTES = 260 * 1024 * 1024  # a backup file (core/backup.py caps it at 250 MB)
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
    """Whole units with separators, after the chosen currency's symbol: $184,000 or £184,000."""
    return f"{formatting.currency_symbol()}{value:,.0f}"


def whole(value: float) -> int:
    """The nearest whole number, halves rounded up, so 12.5 shows as 13 (round() gives 12)."""
    return math.floor(value + 0.5)


def money_short(value: float) -> str:
    """Compact money: $1.4M, $950K, $800 (or the chosen currency's symbol). A K value that rounds
    to 1,000K shows as millions."""
    symbol = formatting.currency_symbol()
    thousands = round(value / 1_000)
    if thousands >= 1_000:
        return f"{symbol}{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{symbol}{thousands}K"
    return f"{symbol}{value:,.0f}"


def format_today(day: date) -> str:
    """The date line in the chosen style, e.g. 'Tuesday 21 October' (formatting.weekday_date)."""
    return formatting.weekday_date(day)


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


class _DataFilter(StrEnum):
    """Values of the Data group on My book. BLIND: an account that is Healthy only because its
    readings are missing or out of date (insights.ScoreConfidence.blind_spot)."""

    BLIND = "blind"


def _filter_groups(
    brief: briefs.WeeklyBrief,
    flags: dict[str, list[research.Topic]],
    motions: set[Motion],
    healths: set[Health],
    topics: set[research.Topic],
    data: set[_DataFilter],
    blind_count: int,
) -> list[dict[str, Any]]:
    """The filter sidebar: each group's checkboxes, with the tag style, icon and count to show.
    Counts come from the whole book, so they don't change as filters are ticked. The data group
    is there only when some account is a blind spot, and the news group only when some account has
    a topic from its website."""
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
    if blind_count:
        groups.append(
            {
                "name": "data",
                "legend": "Data",
                "options": [
                    {
                        "value": _DataFilter.BLIND.value,
                        "label": "Blind spots",
                        "tag": "pill",
                        "icon": None,  # icons.svg has no fitting symbol; the word carries it
                        "count": blind_count,
                        "checked": _DataFilter.BLIND in data,
                    }
                ],
            }
        )
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


# The desktop app (a frozen build) never has its templates edited, so it doesn't check each
# template file for changes on every page; run from source it does, so edits show at once.
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True,
    undefined=_LoudUndefined,
    auto_reload=not getattr(sys, "frozen", False),
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
_env.globals["trend_width"] = journal_pages.TREND_WIDTH
_env.globals["trend_height"] = journal_pages.TREND_HEIGHT
_env.globals["plot"] = {
    "top": journal_pages.PLOT_TOP,
    "bottom": journal_pages.PLOT_BOTTOM,
    "left": journal_pages.PLOT_LEFT,
    "right": journal_pages.PLOT_RIGHT,
}
_env.globals["marker_y"] = journal_pages.MARKER_Y
_env.globals["tick_y"] = journal_pages.TICK_Y
_env.globals["plan_dots"] = formatting.plan_dots
_env.globals["plan_summary"] = formatting.plan_summary
_env.globals["scoring_output"] = scoring_output
_env.globals["slider_max"] = slider_max
_env.globals["monogram"] = logos.monogram
_env.globals["runway"] = runway_geometry.runway
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
    formatting.use_display(current.currency, current.date_style)
    problems: list[ConnectionProblem] = []
    if book:
        combined = _combined_or_none()
        if combined is not None:
            problems = combined.problems
    values: dict[str, Any] = {
        "page": page,
        "active": active,
        "settings": current,
        # The header's book switcher (see _book_menu, in the Books block below create_app).
        "book_menu": _book_menu(current),
        "today": format_today(today_day),
        "today_day": today_day,
        "problems": problems,
        "pending_updates": _pending_count(),
        "due_follow_ups": _due_count(),
        # Locked books show "Lock now" in the header (see web/lock.py and base.html).
        "can_lock": vault.state() is not vault.State.PLAIN,
        # Account id -> LogoInfo, for partials/logo.html (a monogram stands in for the rest).
        "logos": service.all_logos(),
        # Your own logo in the top bar in place of Wardmind's (core/brand.py), or None.
        "brand_logo": brand.current(),
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
    logo: str = "",
) -> HTMLResponse:
    """The Appearance page. `current` is what the form shows: the saved values, or the choices
    the person just made where they are valid. `logo` is a code from _BRAND_NOTICES (?logo=)."""
    notice, logo_error = _BRAND_NOTICES.get(logo, ("", False))
    return _render(
        request,
        "settings/appearance.html",
        page="Appearance",
        active="settings",
        section="appearance",
        current=current,
        errors=dict(errors or {}),
        notice="Appearance saved." if saved else "",
        logo_notice=notice,
        logo_error=logo_error,
        status_code=status_code,
    )


# Your logo on Appearance: the line after an upload or removal, by the code its redirect carries,
# and whether it is a refusal (shown as an error by the file box) or a confirmation.
_BRAND_NOTICES = {
    "saved": ("Your logo is in the top bar now.", False),
    "removed": ("Wardmind's own mark is back in the top bar.", False),
    "no-file": ("Choose an image first.", True),
    "too-big": ("That image is larger than 200 KB. Try a smaller one.", True),
    "refused": ("Use a PNG, JPEG, GIF, WebP or ICO image at least 16 pixels wide.", True),
}


SCORING_PREVIEW_TOP_N = 10  # the preview lists this many accounts
_SCORING_DEFAULTS_NOTICE = (
    "Showing the defaults. Your own signals are kept. Save changes to use them."
)
_SCORING_SAVED_NOTICE = "Scoring saved. Every screen and Claude now use it."
# Your own signals (Settings > Scoring): the notice for each code the redirects carry in ?custom=.
_CUSTOM_NOTICES = {"added": "Signal added.", "saved": "Signal saved.", "removed": "Signal removed."}
_CUSTOM_DETAIL = "Your own signals"  # the Activity log's detail for every change to them
_NO_CUSTOM_SIGNAL = "That signal isn't in your list."


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
    custom: list[CustomSignal],
    notice: str = "",
    custom_notice: str = "",
    custom_add: Mapping[str, str] | None = None,
    custom_add_errors: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The Scoring page. `values` is what each built-in field shows; `errors` is keyed by field.
    `custom` is the book's own signals, listed under the form; `custom_add` is what the add form
    shows (None for a blank form) and `custom_add_errors` its messages, keyed by field."""
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
        custom_rows=custom_signals_form.listing(custom),
        custom_can_add=len(custom) < MAX_CUSTOM_SIGNALS,
        custom_max=MAX_CUSTOM_SIGNALS,
        custom_notice=custom_notice,
        custom_values=dict(custom_add)
        if custom_add is not None
        else custom_signals_form.form_values(None),
        custom_errors=dict(custom_add_errors or {}),
        custom_limits=custom_signals_form.LIMITS,
        status_code=status_code,
    )


def _render_custom_signal(
    request: Request,
    signal: CustomSignal,
    *,
    values: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The edit page for one of the book's own signals: the form, with `values` (what was typed,
    after a refusal) or the signal's own values, and the messages by field."""
    return _render(
        request,
        "settings/custom_signal_edit.html",
        page="Edit signal",
        active="settings",
        section="scoring",
        signal_id=signal.id,
        name=signal.name,
        values=dict(values) if values is not None else custom_signals_form.form_values(signal),
        errors=dict(errors or {}),
        limits=custom_signals_form.LIMITS,
        status_code=status_code,
    )


def _missing_custom_signal(request: Request) -> HTMLResponse:
    """The plain 404 page for a signal id this book doesn't have, with the way back to Scoring."""
    return _missing(
        request,
        "Signal not found",
        _NO_CUSTOM_SIGNAL,
        back_href="/settings/scoring#custom-signals",
        back_label="Back to Scoring",
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
        for index, spec in enumerate(importing.import_fields())
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
    rows = import_flow.match_rows(cleaned, result.examples)
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
    """ARR as the edit form shows it: "$135,000" (in the chosen currency's symbol, which
    importing.parse_money reads back), with cents only when the value has them."""
    symbol = formatting.currency_symbol()
    return f"{symbol}{value:,.0f}" if float(value).is_integer() else f"{symbol}{value:,.2f}"


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
    scoring = load_scoring_config()
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
        kinds=scoring.kinds(),
        signal_labels={kind: signal_info.label(kind, scoring) for kind in scoring.kinds()},
        signal_units={signal.kind: signal.unit for signal in scoring.custom},
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
    # The chosen model when its providers may train on what they're sent: the summarise button
    # then asks first (app.js), and without JavaScript the refusal offers ai_confirm's button.
    ai_training_model: str = ""
    ai_confirm: bool = False  # the last attempt was refused for want of that confirmation


def _research_panel(
    account: Account,
    *,
    editable: bool,
    result: research.SiteResearch | None = None,
    error: str = "",
    ai_error: str = "",
    ai_error_link: bool = False,
    ai_confirm: bool = False,
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
        "ai_training_model": (service.ai_training_model() or "") if ai_ok else "",
        "ai_confirm": ai_confirm,
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
    encryption_errors: Mapping[str, str] | None = None,
    encryption_method: str = "",
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
        ai_model_trains=summaries.trains_on_data(
            current.ai_model if ai_model_value is None else ai_model_value
        ),
        ai_error=ai_error,
        ai_model_error=ai_model_error,
        key_line=_key_line(key_status, date.today()),
        key_from_file=key_status.source is secrets.TokenSource.FILE,
        key_error=key_error,
        key_result=key_result,
        key_result_ok=key_result_ok,
        encryption=_encryption_view(encryption_errors, encryption_method),
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
    request: Request,
    *,
    job_id: str = "",
    error: str = "",
    confirm_training: bool = False,
    refresh: bool = False,
    status_code: int = 200,
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
        # The chosen model when its providers may train on what they're sent: ticking "Also
        # summarise" then asks first (app.js); confirm_training is the no-JavaScript fallback.
        ai_training_model=service.ai_training_model() or "",
        confirm_training=confirm_training,
        confirm_refresh=refresh,
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
    activity: str = "",
    show_all: bool = False,
    template: str = "",
) -> HTMLResponse:
    """One account's page. Without `research_panel`, the website section shows what is already
    saved, which is a file read and never a network call. `refusal` is a contact or client update
    form that was refused: its page comes back with the form's values and messages (422).
    `journal_refusal` is the same for a journal form. `why` is a refusal sent back from Today, and
    `undo` the id of a follow-up that was just marked done (its Undo button is offered).
    `activity` is the Activity filter and `show_all` the "Show all" choice from the query."""
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
    # The account's readings, read once: for the history's origins and the score's confidence.
    signals = connector.list_signals(account_id)
    if combined is not None:
        notices = service.get_store().notices(account_id)
        for signal in signals:
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

    latest = latest_signals(signals).get(account_id, {})
    confidence = insights.score_confidence(detail.scored, latest, today, config)
    path = insights.path_down(detail.scored, latest, config, today)
    forecast = insights.golive_forecast(detail.scored.account, today, config.motion)
    # Unused seats are expected while onboarding is still rolling out, so they aren't flagged then.
    seats = (
        None
        if detail.scored.motion is Motion.ONBOARDING
        else insights.shelfware(detail.scored.account, latest, today, config.motion)
    )
    reading_ages = {age.kind: age for age in confidence.ages}
    # Room to grow (core/growth.py): the funding headline comes from the saved website lookup only.
    radar = growth.growth_radar(
        detail.scored, latest, service.account_funding(detail.scored.account)
    )
    account_journal = service.account_journal(account_id)
    # Your call on this renewal (core/forecast.py), and where Wardmind's score disagrees with it.
    call = renewal_forecast.current_call(account_journal, detail.scored.account.renewal_date)
    found_undo = journal_pages.find_follow_up({account_id: account_journal}, undo)
    shown = load_settings()  # the Settings > Follow-ups values this page's journal uses
    # "Start from a template" (core/note_templates.py): the chosen template's text, with the
    # account's details filled in, goes in the Notes box. It is saved only when the note is added.
    saved_templates = note_templates.all_templates()
    picked = next((item for item in saved_templates if item.id == template), None)
    note_text = ""
    if picked is not None:
        note_text = note_templates.fill(
            picked.body,
            account=detail.scored.account.name,
            today=format_today(today),
            renewal_date=renewal_pages.renewal_day(detail.scored.account.renewal_date),
            owner=detail.scored.account.owner,
            arr=money(detail.scored.account.arr),
        )
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
        activity=activity,
        show_all=show_all,
        follow_up_days=shown.follow_up_days,
        skip_weekends=shown.skip_weekends,
        reply_wait_days=shown.reply_wait_days,
        answer_wait_days=shown.answer_wait_days,
        note_text=note_text,
    )
    trend = journal_pages.sparkline(
        service.account_trend(detail.scored, detail.rank, today),
        config.thresholds,
        today,
        account_journal,
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
        confidence=confidence,
        path=path,
        forecast=forecast,
        seats=seats,
        reading_ages=reading_ages,
        growth=radar,
        call=call,
        call_disagreement=renewal_forecast.disagreement(
            call.kind if call is not None else None, detail.scored.health
        ),
        call_box=renewal_pages.account_call(detail.scored.account, call, today),
        # The "Start from a template" picker in the Notes box (accounts/_notes.html).
        note_templates=saved_templates,
        note_template_id=picked.id if picked is not None else "",
        note_template_name=picked.name if picked is not None else "",
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
_PICK_KIND = "Pick Note, Call, Meeting, Email or Other."
_PICK_ACTIVITY = "Pick Call, Meeting, Email or Other."
_PICK_DIRECTION = "Pick Sent or Received for an email."
_PICK_ROLE = "Pick a role for them."
_NO_MEMBER = "That person isn't on this account's team."
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
    settings = load_settings()
    return rules.due_day(today, settings.follow_up_days, settings.skip_weekends)


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


def _activity_kind(text: str) -> journal.TouchKind:
    """An activity's kind: a call, meeting, email or other touch (a plain note is not one)."""
    try:
        kind = journal.TouchKind(text)
    except ValueError:
        raise ValueError(_PICK_ACTIVITY) from None
    if kind is journal.TouchKind.NOTE:
        raise ValueError(_PICK_ACTIVITY)
    return kind


def _direction(fields: Mapping[str, str], kind: journal.TouchKind) -> journal.Direction | None:
    """Sent or received for an email, which must say; None for every other kind (ignored)."""
    if kind is not journal.TouchKind.EMAIL:
        return None
    try:
        return journal.Direction(fields.get("direction", ""))
    except ValueError:
        raise ValueError(_PICK_DIRECTION) from None


def _team_role(text: str) -> journal.TeamRole:
    try:
        return journal.TeamRole(text)
    except ValueError:
        raise ValueError(_PICK_ROLE) from None


def _entry_section(kind: journal.TouchKind | None) -> str:
    """Where an entry is listed on the account page: 'activity' for a call, meeting, email or
    other touch, else 'notes' (a plain note, or an entry that isn't there any more)."""
    return "activity" if kind not in (None, journal.TouchKind.NOTE) else "notes"


def _entry_kind(account_id: str, entry_id: str) -> journal.TouchKind | None:
    """The kind of one entry in the account's journal, or None when there is no such entry."""
    for entry in service.account_journal(account_id).notes:
        if entry.id == entry_id:
            return entry.kind
    return None


def _kind_of(text: str) -> journal.TouchKind | None:
    """The kind a form posted, or None when it isn't one (the form's own refusal says why)."""
    try:
        return journal.TouchKind(text)
    except ValueError:
        return None


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
    if back == "today":
        # Today keeps the find box's text, so the list stays narrowed after Done or Snooze.
        query = {**query, "q": today_pages.clean_query(fields.get("q", ""))}
    return RedirectResponse(_journal_redirect(back, account_id, anchor, **query), status_code=303)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the feed scheduler and the idle-lock watch with the app; stop both at shutdown.
    An unlock ticket left by an earlier run that crashed is deleted at start, and this run's is
    deleted when it stops, so the MCP server can't read the book once the app is gone."""
    vault.delete_ticket()
    with contextlib.suppress(vault.VaultError, OSError):
        vault.sweep_temp_files()  # plain text a crashed write may have left behind
    service.start_feed_scheduler()
    _app.state.idle_watch.start()
    try:
        yield
    finally:
        _app.state.idle_watch.stop()
        service.stop_feed_scheduler()
        vault.delete_ticket()


# --- Unused seats in the weekly brief (see core/insights.py: renewal_shelfware) -----------------

_SHELFWARE_ROWS = 5  # accounts the weekly brief lists, largest unused value first; the rest counted


def _shelfware_sentence(count: int, total_unused_arr: float) -> str:
    """The lead of the unused-seats section, e.g. '2 renewing accounts pay for about $337,500 of
    unused seats.' The total is a whole-dollar figure, as elsewhere in the sentences."""
    if count == 1:
        return f"1 renewing account pays for about {money(total_unused_arr)} of unused seats."
    return f"{count} renewing accounts pay for about {money(total_unused_arr)} of unused seats."


# --- Logos (see core/logos.py; the routes are in create_app, after the account research route) --

# What the account page says after a logo action. The redirect carries only the code.
_LOGO_NOTICES = {
    "logo-saved": "Logo saved.",
    "logo-removed": "Logo removed. The account shows its monogram again.",
    "logo-no-file": "Choose an image file first.",
    "logo-too-big": "That image is larger than 200 KB. Try a smaller one.",
    "logo-refused": (
        "That isn't a PNG, JPEG, GIF, WebP or ICO image of at least 16 pixels. "
        "SVG files can't be used."
    ),
    "logo-found": "Found a logo on their website and saved it.",
    "logo-not-found": "No logo was found on their website.",
    "logo-research-off": (
        "Web research is off. Turn it on in Settings > Data & privacy to look for a logo."
    ),
    "logo-no-website": "Add the account's website first, then look for a logo.",
    "logo-uploaded-kept": (
        "You uploaded this logo, so it was kept. Remove it first to look for one on their website."
    ),
}
# The lookup's refusals that have their own codes; any other failure is "logo-not-found".
_LOGO_LOOKUP_CODES = {
    service.LOGO_NO_WEBSITE_MESSAGE: "logo-no-website",
    service.LOGO_UPLOADED_MESSAGE: "logo-uploaded-kept",
}


def _logo_redirect(account_id: str, code: str) -> RedirectResponse:
    """Back to the account page, with one logo code in the query (never a message)."""
    return RedirectResponse(
        f"/accounts/{_quoted_id(account_id)}?{urlencode({'logo': code})}", status_code=303
    )


# --- Encryption settings (Settings > Data & privacy; the routes sit in create_app) -------------
# A recovery key is shown once and held in memory under a random token until it is turned on or
# ten minutes pass. The password typed for a new password slot is held the same way. Nothing here
# is written to disk, and no key or password is ever sent back in a page, a notice or a URL.

_PENDING_KEY_MINUTES = 10
_PENDING_KEY_LIMIT = 4  # pending recovery keys at once; the oldest is dropped first
_ENCRYPTION_METHODS = ("windows", "password")
_ENCRYPTION_ERRORS = (
    "method",
    "password",
    "confirm",
    "remove",
    "recovery",
    "off",
    "auto_lock",
    "rekey",
    "rekey_password",
    "rekey_confirm",
)
_METHOD_ERROR = "Choose how Wardmind unlocks: your Windows sign-in or a password."
_WINDOWS_UNAVAILABLE = "Windows can't unlock Wardmind on this computer. Choose a password."
_MISMATCH = "The two passwords don't match. Type the same password in both boxes."
_SAVED_FIRST = "Tick the box to say you've saved your recovery key."
_CONFIRM_REMOVE = "Tick the box to confirm you want to remove the password."
_CONFIRM_RECOVERY = "Tick the box to confirm the old recovery key will stop working."
_CONFIRM_OFF = "Tick the box to confirm you want to turn encryption off."
_CONFIRM_REKEY = "Tick the box to confirm your old recovery key will stop working."
_AUTO_LOCK_ERROR = "Pick 5, 15, 30 or 60 minutes, or never."
_AUTO_LOCK_CHOICE_LABELS = (
    (5, "5 minutes"),
    (15, "15 minutes"),
    (30, "30 minutes"),
    (60, "60 minutes"),
    (0, "Never"),
)
# The notice codes the Data & privacy page accepts in ?notice=. Each maps to fixed words.
_ENCRYPTION_NOTICES = {
    "encryption-on": (
        "Encryption is on. Your recovery key is the way back in if you forget your password."
    ),
    "encryption-off": "Encryption is off. Your files are stored without it again.",
    "encryption-locked": "Wardmind is locked. Unlock it first, then try again.",
    "encryption-not-on": "Encryption is off, so there's nothing to change. Turn it on first.",
    "encryption-expired": (
        "That recovery key page has expired, so nothing was turned on. Start again."
    ),
    "encryption-failed": (
        "Wardmind couldn't change encryption. Try again. If it keeps failing, check that "
        "Wardmind can write to its data folder."
    ),
    "password-set": "Password saved. Wardmind asks for it again after it locks.",
    "password-removed": "Password removed. Windows unlocks Wardmind when you sign in.",
    "auto-lock-saved": "Lock time saved.",
}
_R = TypeVar("_R")


@dataclass(frozen=True)
class _PendingKey:
    """A recovery key that is shown but not yet turned on, with the password it goes with."""

    code: str
    password: str | None
    created: float  # time.monotonic()


def _pending_add(pending: dict[str, _PendingKey], code: str, password: str | None) -> str:
    """Keep a recovery key under a new random token and return the token. Expired entries are
    dropped first, then the oldest while the limit is reached."""
    now = time.monotonic()
    for token, entry in list(pending.items()):
        if now - entry.created > _PENDING_KEY_MINUTES * 60:
            del pending[token]
    while len(pending) >= _PENDING_KEY_LIMIT:
        oldest = min(pending, key=lambda token: pending[token].created)
        del pending[oldest]
    token = os.urandom(24).hex()
    pending[token] = _PendingKey(code=code, password=password, created=now)
    return token


def _pending_peek(pending: dict[str, _PendingKey], token: str) -> _PendingKey | None:
    """The recovery key for this token, unless there is none or it has expired (then it's
    removed). The entry stays in place until the caller takes it."""
    entry = pending.get(token)
    if entry is not None and time.monotonic() - entry.created > _PENDING_KEY_MINUTES * 60:
        pending.pop(token, None)
        return None
    return entry


def _encryption_redirect(notice: str) -> RedirectResponse:
    """Back to Data & privacy, with one fixed notice code in the query."""
    return RedirectResponse(f"/settings/privacy?{urlencode({'notice': notice})}", status_code=303)


# A change to encryption that a crash or a closed window interrupted. The lock gate finishes it on
# the next request once Wardmind is unlocked, so no file stays plain (or keeps a replaced key).
_UNFINISHED = (vault.State.ENCRYPTING, vault.State.DECRYPTING, vault.State.REKEYING)


def _finish_encryption_change() -> None:
    """vault.try_resume(), logged by exception type only when it can't finish yet (it is tried
    again on the next request)."""
    try:
        vault.try_resume()
    except vault.VaultError as error:
        _log.warning("Couldn't finish the change to encryption yet (%s).", type(error).__name__)


def _encryption_view(errors: Mapping[str, str] | None = None, method: str = "") -> dict[str, Any]:
    """What the Encryption section on Data & privacy shows. A password typed into a form is never
    passed in, so it can't be shown back."""
    state = vault.state()
    dpapi = vault.dpapi_available()
    if method not in _ENCRYPTION_METHODS or (method == "windows" and not dpapi):
        method = "windows" if dpapi else "password"
    shown = dict.fromkeys(_ENCRYPTION_ERRORS, "")
    shown.update(errors or {})
    return {
        "off": state is vault.State.PLAIN,
        "on": state is vault.State.ON,
        "busy": state in _UNFINISHED,
        "has_password": vault.has_password(),
        "windows": vault.has_windows_unlock(),
        "dpapi": dpapi,
        "method": method,
        "auto_lock": load_settings().auto_lock_minutes,
        "auto_lock_choices": _AUTO_LOCK_CHOICE_LABELS,
        "errors": shown,
    }


def _new_password_errors(fields: Mapping[str, str]) -> dict[str, str]:
    """The new password checked by its own rules (vault.check_password), then against the
    confirmation. The messages never contain the password."""
    password = fields.get("password", "")
    try:
        vault.check_password(password)
    except ValueError as error:
        return {"password": str(error)}
    if fields.get("confirm", "") != password:
        return {"confirm": _MISMATCH}
    return {}


def _start_errors(method: str, fields: Mapping[str, str]) -> dict[str, str]:
    """Why the unlock choice can't be used, by field. Only the password fields are read."""
    if method not in _ENCRYPTION_METHODS:
        return {"method": _METHOD_ERROR}
    if method == "windows":
        return {} if vault.dpapi_available() else {"method": _WINDOWS_UNAVAILABLE}
    return _new_password_errors(fields)


def _recovery_key_page(
    request: Request,
    code: str,
    *,
    mode: str,
    token: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The page that shows a recovery key, once. "start" ticks a box and then turns encryption
    on; "new" says the old key no longer works and has no turn-on form."""
    return _render(
        request,
        "settings/recovery_key.html",
        page="Your recovery key",
        active="settings",
        status_code=status_code,
        mode=mode,
        groups=code.split("-"),
        token=token,
        error=error,
    )


def _change_encryption(
    change: Callable[[], _R], done: str, *, need_on: bool = True
) -> tuple[str, _R | None]:
    """Run one change to the keys under the store's write lock. The first value is the notice
    code: `done` when it worked (empty when the caller shows its own page), else a fixed code for
    why not. Exception text is never shown or logged."""
    from wardmind.core.store import WRITE_LOCK

    try:
        if need_on and vault.state() is not vault.State.ON:
            return "encryption-not-on", None
        with WRITE_LOCK:
            result = change()
    except vault.Locked:
        return "encryption-locked", None
    except (vault.VaultError, ValueError, OSError):
        return "encryption-failed", None
    return done, result


def _turn_on_encryption(entry: _PendingKey) -> str:
    """Encrypt every managed file with the recovery key the person was shown (and its password,
    if one was chosen). Returns the notice code. The connectors are reset either way."""
    from wardmind.core.store import WRITE_LOCK

    try:
        with WRITE_LOCK:
            vault.enable(code=entry.code, password=entry.password)
    except (vault.VaultError, ValueError, OSError):
        return "encryption-failed"
    finally:
        service.reset_connectors()
    return "encryption-on"


# --- Books (Settings > Books and the header switcher) ---------------------------------------------
# The switcher in the header and the Books page (see core/books.py). books.json is plain, so
# nothing here needs the vault: a locked book still shows its label. A change to the books from a
# locked book is refused by the lock gate (see create_app) before it reaches these helpers.

_DEMO_CHOICE = "demo"  # the switcher's value for Demo data; no book id is "demo"
_BOOK_NOTICES = {
    "book-missing": "That book no longer exists. The list below is up to date.",
    "renamed": "Book renamed.",
    "deleted": "The book and everything in it were deleted.",
    "locked": "Unlock Wardmind first.",
    "books-vault": (
        "Wardmind can't change books right now. If encryption is turning on or off, wait for it "
        "to finish, then try again."
    ),
    "books-failed": (
        "Wardmind couldn't save the list of books. Check that your user profile folder is "
        "writable, then try again."
    ),
}
_DELETE_CONFIRM = "Tick the box to confirm you want to delete this book."
_NO_BOOK = "That book doesn't exist. Pick one from the list."


def _book_menu(current: Settings) -> dict[str, Any]:
    """The header switcher: the open book's label, or Demo data when Demo data is showing; then
    every book, with the open one marked, and Demo data after them."""
    listed = books.load()
    if current.source is DataSource.DEMO:
        open_id, label = None, "Demo data"
    else:
        chosen = listed.get(books.active_id()) or listed.books[0]
        open_id, label = chosen.id, chosen.label
    return {
        "current": label,
        "demo_current": open_id is None,
        "books": [
            {"id": book.id, "label": book.label, "current": book.id == open_id}
            for book in listed.books
        ],
    }


def _book_exists(book_id: str) -> bool:
    return books.load().get(book_id) is not None


def _book_notice(code: str, book_id: str) -> str:
    """The fixed sentence for a notice code. The one code that names a book ("added") takes its
    id from the URL and looks the label up in books.json, so no label comes from a query string."""
    if code == "added":
        added = books.load().get(book_id)
        if added is None:
            return "Book added. Open it to use it."
        return f"{added.label} added. Open it to use it."
    return _BOOK_NOTICES.get(code, "")


def _books_redirect(notice: str, book_id: str = "") -> RedirectResponse:
    """Back to Settings > Books with one notice code, and the id of the book it is about."""
    query = {"notice": notice, "book": book_id} if book_id else {"notice": notice}
    return RedirectResponse(f"/settings/books?{urlencode(query)}", status_code=303)


def _books_failure(error: Exception) -> str:
    """The notice code for a books change that failed for a reason other than its name (a
    BookError has its own message on the page)."""
    if isinstance(error, vault.Locked):
        return "locked"
    if isinstance(error, vault.VaultError):
        return "books-vault"
    return "books-failed"


def _book_write(change: Callable[[], _R]) -> _R:
    """Run one change to the books under the store's write lock, so a running sync can't write
    in the middle of it."""
    from wardmind.core.store import WRITE_LOCK

    with WRITE_LOCK:
        return change()


def _open_demo() -> None:
    """Show Demo data. The open book stays as it was, so switching back opens the same one."""

    def change() -> None:
        save_settings(load_settings().model_copy(update={"source": DataSource.DEMO}))

    _book_write(change)
    service.reset_connectors()


def _open_book(book_id: str) -> None:
    """Make the book the one that opens, and show your data. BookError for an unknown id."""

    def change() -> None:
        books.switch(book_id)
        save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))

    _book_write(change)
    service.reset_connectors()


def _missing_book(request: Request) -> HTMLResponse:
    return _missing(
        request, "Book not found", _NO_BOOK, back_href="/settings/books", back_label="Back to Books"
    )


def _render_books(
    request: Request,
    *,
    notice: str = "",
    add_value: str | None = None,
    add_error: str = "",
    rename_values: Mapping[str, str] | None = None,
    rename_errors: Mapping[str, str] | None = None,
    delete_errors: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Settings > Books: each book with Open, Rename and Delete, then the form to add one. A
    refused change passes back what was typed and its message, by the book it was for."""
    listed = books.load()
    demo = load_settings().source is DataSource.DEMO
    open_id = books.active_id()
    typed = rename_values or {}
    renames = rename_errors or {}
    deletes = delete_errors or {}
    rows = [
        {
            "id": book.id,
            "label": book.label,
            "is_open": not demo and book.id == open_id,
            "is_first": book.is_default,
            # The same rule core/books.py applies: the first book and the open book stay.
            "can_delete": not book.is_default and book.id != open_id,
            "rename_value": typed.get(book.id, book.label),
            "rename_error": renames.get(book.id, ""),
            "delete_error": deletes.get(book.id, ""),
        }
        for book in listed.books
    ]
    return _render(
        request,
        "settings/books.html",
        page="Books",
        active="settings",
        section="books",
        status_code=status_code,
        notice=notice,
        demo=demo,
        books=rows,
        can_add=len(listed.books) < books.MAX_BOOKS,
        max_books=books.MAX_BOOKS,
        add_value=books.suggested_label() if add_value is None else add_value,
        add_error=add_error,
    )


# What the activity log says a connection was (core/audit.py: the detail names the kind).
_CONNECTION_WORDS: dict[ConnectionKind, str] = {
    ConnectionKind.HUBSPOT: "HubSpot",
    ConnectionKind.SPREADSHEET: "Spreadsheet",
    ConnectionKind.MANUAL: "Added by hand",
    ConnectionKind.FEED: "Data feed",
}


def create_app(gate: session.SessionGate | None = None) -> FastAPI:
    """Build the web app: no API docs, a host allow-list, and security headers on every response.

    `gate` is the session gate (web/session.py). The launchers (`wardmind ui` and the desktop
    window) always pass one, so only the person who started Wardmind can open its pages; without
    it (tests) every page is open to whoever reaches 127.0.0.1."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    # --- Locking: the lock gate (see core/vault.py and web/lock.py) ----------------------------
    # Registered before the session check below, which therefore runs first: a request without
    # the cookie still gets 403 and learns nothing about the lock. A locked book answers GET pages
    # with a redirect to /unlock (htmx gets HX-Redirect) and any other method with 423. The paths
    # in lock.is_lock_open_path skip the gate.
    app.state.idle = lock.IdleTimer()
    app.state.throttle = lock.UnlockThrottle()

    def lock_if_idle() -> None:
        """The background check (see _lifespan): locks an unlocked, encrypted book that has been
        idle past the setting, even when no request comes in."""
        idle = app.state.idle
        if lock.should_lock(
            idle.now(),
            idle.last,
            load_settings().auto_lock_minutes,
            unlocked=vault.is_unlocked(),
            encrypted=vault.state() is not vault.State.PLAIN,
        ):
            vault.lock()
            service.reset_connectors()
            audit.record(audit.Event.LOCKED, detail="after being idle")
        else:
            vault.refresh_ticket()  # keep the MCP server's ticket current while unlocked

    app.state.idle_watch = lock.IdleWatch(lock_if_idle)

    def locked_answer(request: Request) -> Response | None:
        """None when the request may go on; otherwise the answer for a locked book."""
        if vault.state() is vault.State.PLAIN:
            return None
        if vault.is_unlocked() and app.state.idle.expired(
            minutes=load_settings().auto_lock_minutes
        ):
            vault.lock()
            service.reset_connectors()
            audit.record(audit.Event.LOCKED, detail="after being idle")
        if vault.ensure_unlocked():
            return None
        if request.method.upper() not in ("GET", "HEAD"):
            return PlainTextResponse(lock.LOCKED_WRITE_MESSAGE, status_code=423)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        location = f"{lock.UNLOCK_PATH}?{urlencode({'next': session.safe_next(target)})}"
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=200, headers={"HX-Redirect": location})
        return RedirectResponse(location, status_code=303)

    @app.middleware("http")
    async def lock_gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if lock.is_lock_open_path(request.url.path):
            return await call_next(request)
        try:
            refused = locked_answer(request)
        except vault.Damaged as error:
            return PlainTextResponse(
                f"Wardmind can't open your data: {error.message}", status_code=503
            )
        if refused is not None:
            return refused
        if vault.state() in _UNFINISHED:
            await run_in_threadpool(_finish_encryption_change)
        # The idle clock is touched when a request starts and when it ends, so a page still
        # loading (or a migration still running) is never locked under its own feet. Static
        # files don't count as use.
        touching = not request.url.path.startswith("/static/")
        if touching:
            app.state.idle.touch()
        response = await call_next(request)
        if touching:
            app.state.idle.touch()
        return response

    if gate is not None:

        @app.middleware("http")
        async def session_check(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if session.is_open_path(request.url.path):
                return await call_next(request)
            name = session.cookie_name(request.url.port)
            if not gate.accepts_cookie(request.cookies.get(name)):
                return PlainTextResponse(session.LOCKED_OUT_MESSAGE, status_code=403)
            return await call_next(request)

        audit.record(audit.Event.SESSION_STARTED)
        refused_logged = [-60.0]  # when SIGN_IN_REFUSED was last recorded (monotonic seconds)

        @app.get(session.ENTER_PATH)
        async def enter(request: Request, t: str = "", next: str = "/") -> Response:  # noqa: A002
            if not gate.accepts_entry(t):
                # Logged at most once a minute, so a flood of bad links can't flood the log.
                now = time.monotonic()
                if now - refused_logged[0] >= 60:
                    refused_logged[0] = now
                    audit.record(audit.Event.SIGN_IN_REFUSED)
                return PlainTextResponse(session.BAD_ENTRY_MESSAGE, status_code=403)
            audit.record(audit.Event.SIGN_IN)
            response = RedirectResponse(session.safe_next(next), status_code=303)
            response.set_cookie(
                session.cookie_name(request.url.port),
                gate.session_token,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_cross_site_write(request.method, request.headers):
            return PlainTextResponse(CROSS_SITE_MESSAGE, status_code=403)
        if is_cross_site_load(request.method, request.headers):
            return PlainTextResponse(CROSS_SITE_LOAD_MESSAGE, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def body_size_cap(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Refuse a request body larger than any form here takes before it is read (and spooled
        to a temp file in plain text): MAX_BODY_BYTES, or MAX_RESTORE_BODY_BYTES for restoring a
        backup. Each route still checks its own, smaller limit on what it reads."""
        length = request.headers.get("content-length", "")
        limit = (
            MAX_RESTORE_BODY_BYTES
            if request.url.path == "/settings/backup/restore"
            else MAX_BODY_BYTES
        )
        if length.isdigit() and int(length) > limit:
            return PlainTextResponse("That upload is too big.", status_code=413)
        return await call_next(request)

    # Registered last so it wraps everything above, including the 403.
    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            # A route may send a stricter policy of its own (a logo image sends
            # default-src 'none'); every other response gets the app's.
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/logos/") and "cache-control" in response.headers:
            return response  # a logo is cached privately for an hour (see the /logos route)
        if request.url.path.startswith("/static/"):
            # Revalidate every time (a cheap 304 locally) so an upgrade never runs stale JS or CSS.
            response.headers["Cache-Control"] = "no-cache"
        else:
            # Pages show the CSM's book. The browser keeps none of them, so Back or a shared
            # computer can't show one from its cache.
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(vault.VaultError)
    async def vault_error(request: Request, exc: vault.VaultError) -> Response:
        # A locked or damaged book is never shown as an empty one (see core/vault.py). A page
        # that met the lock partway through (the idle lock ran meanwhile) goes to the unlock page.
        if isinstance(exc, vault.Locked) and request.method.upper() in ("GET", "HEAD"):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            location = f"{lock.UNLOCK_PATH}?{urlencode({'next': session.safe_next(target)})}"
            if request.headers.get("HX-Request") == "true":
                return Response(status_code=200, headers={"HX-Redirect": location})
            return RedirectResponse(location, status_code=303)
        return PlainTextResponse(f"Wardmind can't open your data: {exc.message}", status_code=503)

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
        motion=renewal): motion, health, data=blind (Healthy only because the data is missing or
        out of date) and, with web research, topic; view=list shows the table instead of cards;
        sort=growth ranks by room to grow (core/growth.py), and any other sort is priority.
        An htmx refresh gets only the results partial, not on a history restore. With Your data
        and nothing connected, this is the Setup page instead."""
        if _needs_setup():
            return RedirectResponse("/setup", status_code=303)
        partial = request.headers.get("HX-Request") == "true" and (
            "HX-History-Restore-Request" not in request.headers
        )
        if not partial:
            service.remember_book()  # today's score snapshot, which "Since last week" compares with
        today = date.today()
        connector = service.get_connector()
        book = service.get_book(connector)
        brief = briefs.weekly_brief(book, as_of=today)

        latest = service.latest_book_signals(connector)
        confidence = insights.book_confidence(book, latest, today, load_scoring_config())
        blind = {account_id for account_id, found in confidence.items() if found.blind_spot}
        params = request.query_params
        motions = _chosen(params.getlist("motion"), Motion)
        healths = _chosen(params.getlist("health"), Health)
        topics = _chosen(params.getlist("topic"), research.Topic)
        data = _chosen(params.getlist("data"), _DataFilter)
        view = "list" if params.get("view") == "list" else "cards"
        sort = "growth" if params.get("sort") == "growth" else "priority"
        flags = service.account_flags()
        # Room to grow for every account, computed once per request: the tags and the sort.
        radars = growth.book_growth(book, latest, service.book_funding())
        ranks = {item.account.id: rank for rank, item in enumerate(book, start=1)}
        rows: list[tuple[int, ScoredAccount]] = [
            (ranks[item.account.id], item)
            for item in service.filter_book(book, None, q)
            if (not motions or item.motion in motions)
            and (not healths or item.health in healths)
            and (not topics or not topics.isdisjoint(flags.get(item.account.id, [])))
            and (_DataFilter.BLIND not in data or item.account.id in blind)
        ]
        if sort == "growth":
            # Stable: accounts with equal growth keep the priority order they came in.
            rows.sort(key=lambda row: -radars[row[1].account.id].score)
        first, second = summary_sentences(
            brief.counts_by_health[Health.AT_RISK],
            brief.arr_at_risk,
            brief.counts_by_health[Health.WATCH],
        )
        groups = _filter_groups(brief, flags, motions, healths, topics, data, len(blind))
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
            filtered=bool(motions or healths or topics or data),
            view=view,
            sort=sort,
            radar_by_id=radars,
            rows=rows,
            blind_spots=blind,
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
        week, the follow-ups due this week, unused seats on the renewals coming up, the motions
        and the top ten."""

        today = date.today()
        config = load_scoring_config()
        connector = service.get_connector()
        book = service.get_book(connector, config, today)
        brief = briefs.weekly_brief(book, as_of=today, top_n=10)
        week_follow_ups, more_follow_ups = journal_pages.week_follow_ups(
            journal.today_view(
                book,
                service.get_journal().all(),
                today,
                quiet_days=load_settings().quiet_days,
            ),
            today,
            limit=_WEEK_FOLLOW_UPS,
        )
        shelfware = insights.renewal_shelfware(
            book,
            service.latest_book_signals(connector),
            today,
            config.motion,
            limit=_SHELFWARE_ROWS,
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
            shelfware=shelfware,
            shelfware_sentence=_shelfware_sentence(shelfware.count, shelfware.total_unused_arr),
            shelfware_more=shelfware.count - len(shelfware.rows),
            renewals={item.account.id: item.account.renewal_date for item in book},
        )

    @app.get("/onboarding")
    def onboarding(request: Request, missing: str = "") -> HTMLResponse:
        """Onboarding prep: each onboarding account with its day, plan and go-live forecast, and a
        picker for any account. Every row's brief uses the same connector, config and date as the
        book."""

        connector = service.get_connector()
        config = load_scoring_config()
        as_of = date.today()
        book = service.get_book(connector, config, as_of)
        rows: list[tuple[ScoredAccount, OnboardingBrief, insights.GoLiveForecast | None]] = []
        for item in service.onboarding_accounts(book):
            brief = service.get_onboarding_brief(item.account.id, connector, config, as_of)
            if brief is not None:
                forecast = insights.golive_forecast(item.account, as_of, config.motion)
                rows.append((item, brief, forecast))
        behind = sum(formatting.behind_on_plan(brief.milestones) for _, brief, _ in rows)
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

        connector = service.get_connector()
        config = load_scoring_config()
        as_of = date.today()
        detail = service.get_account_detail(account_id, connector, config, as_of)
        if detail is None:
            return _render(
                request,
                "not_found.html",
                page="Account not found",
                status_code=404,
                message=f"No account with id {account_id}.",
                book=True,
            )
        # The same brief service.get_onboarding_brief builds, from the detail already loaded.
        brief = build_onboarding_brief(detail, config.motion, config)
        # How old each reading is, read the way the account page reads it, so the signals table
        # is the same table on both pages.
        latest = latest_signals(connector.list_signals(account_id)).get(account_id, {})
        reading_ages = {
            age.kind: age
            for age in insights.score_confidence(detail.scored, latest, as_of, config).ages
        }
        # The signals table scales its bars to the largest weight in the scoring config, as the
        # account page does, so the same signal is drawn the same width on both pages.
        longest = max(rule.weight for rule in config.signals.values())
        forecast = insights.golive_forecast(brief.account, as_of, config.motion)
        return _render(
            request,
            "onboarding_brief.html",
            page=f"{brief.account.name} onboarding brief",
            active="onboarding",
            book=True,
            brief=brief,
            longest=longest,
            reading_ages=reading_ages,
            forecast=forecast,
            plain_text=formatting.onboarding_plain_text(brief),
        )

    # --- Encryption: Settings > Data & privacy (see core/vault.py) ----------------------------
    # Routes under /settings/privacy/encryption and /settings/privacy/auto-lock go here.
    # The pending recovery keys live on app.state (memory only, see _pending_add). They are read
    # and taken on the event loop, so no two requests can take the same key.
    app.state.encryption_pending = {}

    @app.post("/settings/privacy/encryption/start")
    async def encryption_start(request: Request) -> Response:
        """Check the unlock choice (and a new password), then show a recovery key once. Nothing is
        encrypted yet: that happens when the recovery key page's box is ticked and its button
        pressed. When encryption is already on, this goes back to the page."""
        fields = await _posted_fields(request)
        method = fields.get("method", "")
        if await run_in_threadpool(vault.state) is not vault.State.PLAIN:
            return RedirectResponse("/settings/privacy", status_code=303)
        errors = _start_errors(method, fields)
        if errors:
            return await run_in_threadpool(
                _render_privacy,
                request,
                encryption_errors=errors,
                encryption_method=method,
                status_code=422,
            )
        code = vault.new_recovery_code()
        password = fields.get("password", "") if method == "password" else None
        token = _pending_add(app.state.encryption_pending, code, password)
        return await run_in_threadpool(_recovery_key_page, request, code, mode="start", token=token)

    @app.post("/settings/privacy/encryption/on")
    async def encryption_on(request: Request) -> Response:
        """Turn encryption on with the recovery key the person was shown: every managed file is
        encrypted in place. The pending key is taken once, whatever the outcome."""
        fields = await _posted_fields(request)
        token = fields.get("pending", "")
        pending = app.state.encryption_pending
        entry = _pending_peek(pending, token)
        if entry is None:
            return _encryption_redirect("encryption-expired")
        if not fields.get("saved"):
            return await run_in_threadpool(
                _recovery_key_page,
                request,
                entry.code,
                mode="start",
                token=token,
                error=_SAVED_FIRST,
                status_code=422,
            )
        pending.pop(token, None)
        notice = await run_in_threadpool(_turn_on_encryption, entry)
        if notice == "encryption-on":
            audit.record(audit.Event.ENCRYPTION_ON, book=books.active_id())
        return _encryption_redirect(notice)

    @app.post("/settings/privacy/encryption/off")
    async def encryption_off(request: Request) -> Response:
        """Turn encryption off: every file goes back to plain, then the keys are deleted. Needs
        the tick box; a locked book gets a notice and nothing changes."""
        fields = await _posted_fields(request)
        if not fields.get("confirm_off"):
            return await run_in_threadpool(
                _render_privacy,
                request,
                encryption_errors={"off": _CONFIRM_OFF},
                status_code=422,
            )
        notice, _ = await run_in_threadpool(
            _change_encryption, vault.disable, "encryption-off", need_on=False
        )
        if notice == "encryption-off":
            audit.record(audit.Event.ENCRYPTION_OFF, book=books.active_id())
        return _encryption_redirect(notice)

    @app.post("/settings/privacy/encryption/password")
    async def encryption_password(request: Request) -> Response:
        """Set or change the password. Windows protection is replaced, so the password (or the
        recovery key) is needed after every lock. Refused fields re-render the page (422)."""
        fields = await _posted_fields(request)
        errors = _new_password_errors(fields)
        if errors:
            return await run_in_threadpool(
                _render_privacy, request, encryption_errors=errors, status_code=422
            )
        password = fields.get("password", "")
        notice, _ = await run_in_threadpool(
            _change_encryption, lambda: vault.set_password(password), "password-set"
        )
        if notice == "password-set":
            audit.record(audit.Event.PASSWORD_SET)
        return _encryption_redirect(notice)

    @app.post("/settings/privacy/encryption/password/remove")
    async def encryption_remove_password(request: Request) -> Response:
        """Remove the password: Windows unlocks Wardmind again, with nothing typed."""
        fields = await _posted_fields(request)
        if not fields.get("confirm_remove"):
            return await run_in_threadpool(
                _render_privacy,
                request,
                encryption_errors={"remove": _CONFIRM_REMOVE},
                status_code=422,
            )
        notice, _ = await run_in_threadpool(
            _change_encryption, vault.remove_password, "password-removed"
        )
        if notice == "password-removed":
            audit.record(audit.Event.PASSWORD_REMOVED)
        return _encryption_redirect(notice)

    @app.post("/settings/privacy/encryption/recovery")
    async def encryption_new_recovery(request: Request) -> Response:
        """Make a new recovery key. The old one stops working with the current key file at once
        (an older copy of keys.json still takes it: Replace all keys covers that). The new key is
        shown on its own page, once; nothing is stored for it."""
        fields = await _posted_fields(request)
        if not fields.get("confirm_recovery"):
            return await run_in_threadpool(
                _render_privacy,
                request,
                encryption_errors={"recovery": _CONFIRM_RECOVERY},
                status_code=422,
            )
        notice, code = await run_in_threadpool(_change_encryption, vault.replace_recovery_code, "")
        if notice:
            return _encryption_redirect(notice)
        audit.record(audit.Event.RECOVERY_KEY_REPLACED)
        return await run_in_threadpool(_recovery_key_page, request, code or "", mode="new")

    @app.post("/settings/privacy/encryption/rekey")
    async def encryption_rekey(request: Request) -> Response:
        """Replace the master key and every book's key, and encrypt every file again (see
        vault.replace_all_keys). With a password, it is typed again (or a new one) to protect
        the new keys. The new recovery key is shown once, on its own page."""
        fields = await _posted_fields(request)
        errors: dict[str, str] = {}
        has_password = await run_in_threadpool(vault.has_password)
        if has_password:
            errors = {f"rekey_{name}": text for name, text in _new_password_errors(fields).items()}
        if not fields.get("confirm_rekey"):
            errors["rekey"] = _CONFIRM_REKEY
        if errors:
            return await run_in_threadpool(
                _render_privacy, request, encryption_errors=errors, status_code=422
            )
        password = fields.get("password", "") if has_password else None
        notice, code = await run_in_threadpool(
            _change_encryption, lambda: vault.replace_all_keys(password=password), ""
        )
        service.reset_connectors()
        if notice:
            return _encryption_redirect(notice)
        audit.record(audit.Event.KEYS_REPLACED)
        return await run_in_threadpool(_recovery_key_page, request, code or "", mode="rekey")

    @app.post("/settings/privacy/auto-lock")
    async def encryption_auto_lock(request: Request) -> Response:
        """Save the idle time before Wardmind locks. Only the choices in AUTO_LOCK_CHOICES are
        kept; anything else re-renders the section (422) and nothing is saved."""
        from wardmind.core.settings import AUTO_LOCK_CHOICES

        fields = await _posted_fields(request)
        raw = fields.get("minutes", "")
        if not (raw.isascii() and raw.isdigit()) or int(raw) not in AUTO_LOCK_CHOICES:
            return await run_in_threadpool(
                _render_privacy,
                request,
                encryption_errors={"auto_lock": _AUTO_LOCK_ERROR},
                status_code=422,
            )
        minutes = int(raw)
        await run_in_threadpool(
            save_settings,
            load_settings().model_copy(update={"auto_lock_minutes": minutes}),
        )
        audit.record(audit.Event.SETTINGS_SAVED, detail="Data & privacy")
        return _encryption_redirect("auto-lock-saved")

    # --- Locking: /unlock and /lock (see core/vault.py and web/lock.py) -----------------------
    # The unlock page does not extend the layout: the layout reads the book, which is locked here.
    # A wrong try re-shows it with the message by the field used; the typed value never comes back.
    def render_unlock(
        request: Request,
        *,
        next_path: str,
        errors: Mapping[str, str] | None = None,
        alert: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "unlock.html",
            {
                "settings": load_settings(),
                "next": next_path,
                "has_windows": vault.has_windows_unlock(),
                "has_password": vault.has_password(),
                "errors": dict(errors or {}),
                "alert": alert,
            },
            status_code=status_code,
        )

    @app.get(lock.UNLOCK_PATH)
    def unlock_form(request: Request, next: str = "") -> Response:
        """The unlock page. A plain book, or one that unlocks itself through Windows, goes on to
        `next` (sanitised by session.safe_next) instead."""
        target = session.safe_next(next)
        if vault.state() is vault.State.PLAIN or vault.ensure_unlocked():
            return RedirectResponse(target, status_code=303)
        return render_unlock(request, next_path=target)

    @app.post(lock.UNLOCK_PATH)
    async def unlock_submit(request: Request) -> Response:
        """Unlock with Windows (windows=1), the recovery key (recovery) or the password (password).
        Too many wrong tries wait (lock.UnlockThrottle); a wrong try re-shows the page with the
        message. Same-origin and session checks apply, as to every write."""
        fields = await _posted_fields(request)
        target = session.safe_next(fields.get("next"))
        throttle = app.state.throttle
        if not throttle.begin():
            return render_unlock(
                request, next_path=target, alert=lock.too_many_tries(1), status_code=429
            )
        try:
            return await unlock_attempt(request, fields, target, throttle)
        finally:
            throttle.end()

    async def unlock_attempt(
        request: Request, fields: dict[str, str], target: str, throttle: lock.UnlockThrottle
    ) -> Response:
        """One unlock try, while it holds the throttle's single slot (see unlock_submit)."""
        wait = throttle.seconds_left()
        if wait:
            return render_unlock(
                request, next_path=target, alert=lock.too_many_tries(wait), status_code=429
            )
        if fields.get("windows") == "1":
            slot = "windows"
        elif "recovery" in fields:
            slot = "recovery"
        else:
            slot = "password"
        if slot != "windows" and not fields.get(slot, "").strip():
            blank = "Type your recovery key." if slot == "recovery" else "Type your password."
            return render_unlock(request, next_path=target, errors={slot: blank}, status_code=422)

        def attempt() -> None:
            if slot == "password":
                vault.unlock(password=fields["password"])
            elif slot == "recovery":
                vault.unlock(recovery=fields["recovery"])
            else:
                vault.unlock()

        try:
            await run_in_threadpool(attempt)
        except vault.Locked as error:
            throttle.failed()
            audit.record(audit.Event.UNLOCK_FAILED, book=books.active_id())
            return render_unlock(
                request, next_path=target, errors={slot: error.message}, status_code=422
            )
        throttle.succeeded()
        audit.record(audit.Event.UNLOCKED, book=books.active_id())
        service.reset_connectors()
        app.state.idle.touch()
        return RedirectResponse(target, status_code=303)

    @app.post("/lock")
    def lock_now() -> RedirectResponse:
        """Lock the book now: the key is forgotten here, and nothing reads the book until it is
        unlocked again. With encryption off there is nothing to lock: back to My book."""
        if vault.state() is vault.State.PLAIN:
            return RedirectResponse("/", status_code=303)
        vault.lock()
        service.reset_connectors()
        audit.record(audit.Event.LOCKED, book=books.active_id(), detail="by you")
        return RedirectResponse(lock.UNLOCK_PATH, status_code=303)

    # --- Books: the switcher and Settings > Books (see core/books.py) -------------------------
    # The switch form in the header, and the Books page. Every change holds the store's write
    # lock (see _book_write). A bad name or a refused delete is answered on the page (422); a
    # locked book never gets here, because the lock gate answers first.

    @app.post("/books/switch")
    async def switch_book(request: Request) -> Response:
        """Open a book, or show Demo data. The field is a book id, or "demo". An unknown id goes
        back to Settings > Books with a notice and changes nothing."""
        choice = (await _posted_fields(request)).get("book", "")
        try:
            if choice == _DEMO_CHOICE:
                await run_in_threadpool(_open_demo)
            else:
                await run_in_threadpool(_open_book, choice)
                audit.record(audit.Event.BOOK_OPENED, book=choice)
        except books.BookError:
            return _books_redirect("book-missing")
        except (vault.VaultError, OSError) as error:
            return _books_redirect(_books_failure(error))
        return RedirectResponse("/", status_code=303)

    @app.get("/settings/books")
    def books_page(request: Request, notice: str = "", book: str = "") -> HTMLResponse:
        """The Books page. ?notice= is one code from _BOOK_NOTICES, or "added" with ?book= the new
        book's id; any other value shows nothing."""
        return _render_books(request, notice=_book_notice(notice, book))

    @app.post("/settings/books/add")
    async def add_book(request: Request) -> Response:
        """Add a book. It is not opened: its row offers Open. A blank or taken name, or too many
        books, re-shows the form (422) with the reason and saves nothing."""
        typed = (await _posted_fields(request)).get("label", "")
        try:
            added = await run_in_threadpool(_book_write, lambda: books.add(typed))
        except books.BookError as error:
            return await run_in_threadpool(
                _render_books, request, add_value=typed, add_error=str(error), status_code=422
            )
        except (vault.VaultError, OSError) as error:
            return _books_redirect(_books_failure(error))
        audit.record(audit.Event.BOOK_ADDED, book=added.id)
        return _books_redirect("added", added.id)

    @app.post("/settings/books/{book_id}/rename")
    async def rename_book(request: Request, book_id: str) -> Response:
        """Rename a book. A blank or taken name re-shows that book's row (422) with the reason."""
        if not await run_in_threadpool(_book_exists, book_id):
            return await run_in_threadpool(_missing_book, request)
        typed = (await _posted_fields(request)).get("label", "")
        try:
            await run_in_threadpool(_book_write, lambda: books.rename(book_id, typed))
        except books.BookError as error:
            return await run_in_threadpool(
                _render_books,
                request,
                rename_values={book_id: typed},
                rename_errors={book_id: str(error)},
                status_code=422,
            )
        except (vault.VaultError, OSError) as error:
            return _books_redirect(_books_failure(error))
        audit.record(audit.Event.BOOK_RENAMED, book=book_id)
        return _books_redirect("renamed")

    @app.post("/settings/books/{book_id}/delete")
    async def delete_book(request: Request, book_id: str) -> Response:
        """Delete a book with its folder and its key. The confirm box must be ticked. The first
        book and the open book can't be deleted: that row says so (422)."""
        if not await run_in_threadpool(_book_exists, book_id):
            return await run_in_threadpool(_missing_book, request)
        if not (await _posted_fields(request)).get("confirm"):
            return await run_in_threadpool(
                _render_books, request, delete_errors={book_id: _DELETE_CONFIRM}, status_code=422
            )
        try:
            await run_in_threadpool(_book_write, lambda: books.delete(book_id))
        except books.BookError as error:
            return await run_in_threadpool(
                _render_books, request, delete_errors={book_id: str(error)}, status_code=422
            )
        except (vault.VaultError, OSError) as error:
            return _books_redirect(_books_failure(error))
        await run_in_threadpool(service.reset_connectors)
        audit.record(audit.Event.BOOK_DELETED, book=book_id)
        return _books_redirect("deleted")

    # --- Renewals: the honest forecast (see core/forecast.py) ---------------------------------

    @app.get("/renewals")
    def renewals_route(request: Request, show: str = "", call: str = "") -> HTMLResponse:
        """Renewals in the scoring window, soonest first: your call on each (Commit, Likely or At
        risk), the ARR behind each call, where your call and Wardmind's score disagree (show=
        disagree lists only those) and how the calls held up. `call` is a notice code from a
        save."""
        today = date.today()
        config = load_scoring_config()
        book = service.get_book(config=config, as_of=today)
        journals = service.get_journal().all()
        view = renewal_forecast.forecast_view(
            book, journals, today, config.motion.renewal_window_days
        )
        page = renewal_pages.renewals_page(
            view,
            renewal_forecast.calibration(journals),
            window_days=config.motion.renewal_window_days,
            book_size=len(book),
            disagree_only=show == "disagree",
            notice=renewal_pages.call_notice(call),
            money_short=money_short,
        )
        return _render(
            request, "renewals.html", page="Renewals", active="renewals", book=True, screen=page
        )

    def record_call(
        account_id: str, scored: ScoredAccount, fields: Mapping[str, str], today: date
    ) -> str:
        """Save, clear or refuse one posted call. A call can't be saved on a renewal date that has
        passed (the outcome takes over then), but a clear always goes through. Returns the notice
        code."""
        text = fields.get("kind", "")
        if text == renewal_pages.CLEAR:
            service.get_journal().clear_call(account_id, scored.account.renewal_date)
            return renewal_pages.CALL_CLEARED
        try:
            kind = journal.CallKind(text)
        except ValueError:
            return renewal_pages.CALL_PICK
        if scored.account.renewal_date < today:
            return renewal_pages.CALL_PASSED
        service.get_journal().make_call(
            account_id, kind, scored, now=datetime.now(UTC), today=today
        )
        return renewal_pages.CALL_SAVED

    @app.post("/accounts/{account_id}/call")
    async def record_call_route(request: Request, account_id: str) -> Response:
        """Your call on the account's next renewal, or Clear. Back to the renewals page when the
        form was sent from there (back=renewals, keeping show=disagree), else to the account page's
        call box. Unknown account: 404."""
        scored = await run_in_threadpool(_book_account, account_id)
        if scored is None:
            return await run_in_threadpool(
                _missing, request, "Account not found", f"No account with id {account_id}."
            )
        fields = await _posted_fields(request)
        code = await run_in_threadpool(record_call, account_id, scored, fields, date.today())
        return RedirectResponse(
            renewal_pages.call_redirect(
                fields.get("back", ""), account_id, code, fields.get("show", "")
            ),
            status_code=303,
        )

    # --- Negotiation sheet (see core/negotiation.py) ------------------------------------------
    @app.get("/accounts/{account_id}/renewal-sheet")
    def renewal_sheet(request: Request, account_id: str) -> HTMLResponse:
        """One account's negotiation sheet: the one-page prep for its renewal call, built from the
        same data as the account page (negotiation.negotiation_sheet)."""

        connector = service.get_connector()
        config = load_scoring_config()
        as_of = date.today()
        detail = service.get_account_detail(account_id, connector, config, as_of)
        if detail is None:
            return _render(
                request,
                "not_found.html",
                page="Account not found",
                status_code=404,
                message=f"No account with id {account_id}.",
                book=True,
            )
        scored = detail.scored
        latest = latest_signals(connector.list_signals(account_id)).get(account_id, {})
        radar = growth.growth_radar(scored, latest, service.account_funding(scored.account))
        # Unused seats are expected while onboarding is still rolling out, as on the account page.
        seats = (
            None
            if scored.motion is Motion.ONBOARDING
            else insights.shelfware(scored.account, latest, as_of, config.motion)
        )
        sheet = negotiation.negotiation_sheet(
            scored, latest, service.account_journal(account_id), radar, seats, as_of
        )
        return _render(
            request,
            "renewal_sheet.html",
            page=f"{sheet.name} renewal",
            active="book",
            book=True,
            sheet=sheet,
        )

    @app.get("/settings/appearance")
    def appearance(request: Request, saved: str = "", logo: str = "") -> HTMLResponse:
        return _render_appearance(request, load_settings(), saved=saved == "1", logo=logo)

    # Your logo in place of Wardmind's (core/brand.py). The image is served only to this app, with
    # its checked type and a policy that lets it run nothing.

    @app.get("/brand-logo")
    def brand_logo_image() -> Response:
        saved = brand.read()
        if saved is None:
            return PlainTextResponse("No logo.", status_code=404)
        data, kind = saved
        return Response(
            content=data,
            media_type=kind.value,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
                "Cache-Control": "private, max-age=86400",
            },
        )

    @app.post("/settings/appearance/logo")
    async def upload_brand_logo(request: Request) -> Response:
        """Save the image posted as "logo" as the top bar's logo. At most one byte past the
        limit is read, so a larger file is refused without being kept."""
        form = await request.form()
        upload = form.get("logo")
        if not (isinstance(upload, UploadFile) and upload.filename):
            return RedirectResponse("/settings/appearance?logo=no-file#logo", status_code=303)
        data = await upload.read(logos.MAX_LOGO_BYTES + 1)
        if len(data) > logos.MAX_LOGO_BYTES:
            return RedirectResponse("/settings/appearance?logo=too-big#logo", status_code=303)
        try:
            await run_in_threadpool(brand.save, data)
        except logos.LogoError:
            return RedirectResponse("/settings/appearance?logo=refused#logo", status_code=303)
        audit.record(audit.Event.SETTINGS_SAVED, detail="Your logo")
        return RedirectResponse("/settings/appearance?logo=saved#logo", status_code=303)

    @app.post("/settings/appearance/logo/remove")
    async def remove_brand_logo() -> Response:
        await run_in_threadpool(brand.remove)
        audit.record(audit.Event.SETTINGS_SAVED, detail="Your logo")
        return RedirectResponse("/settings/appearance?logo=removed#logo", status_code=303)

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
        audit.record(audit.Event.SETTINGS_SAVED, detail="Appearance")
        return RedirectResponse("/settings/appearance?saved=1", status_code=303)

    @app.get("/settings/scoring")
    def scoring(
        request: Request, saved: str = "", defaults: str = "", custom: str = ""
    ) -> HTMLResponse:
        """The scoring form, with its preview and the book's own signals. ?defaults=1 shows the
        built-in defaults, unsaved; the book's own signals are kept either way (they aren't part of
        the defaults). ?custom= is one code from _CUSTOM_NOTICES; any other value shows nothing."""
        saved_config = load_scoring_config()
        if defaults == "1":
            draft = ScoringConfig(custom=saved_config.custom)
            notice = _SCORING_DEFAULTS_NOTICE
        else:
            draft = saved_config
            notice = _SCORING_SAVED_NOTICE if saved == "1" else ""
        return _render_scoring(
            request,
            values=form_values(draft),
            errors={},
            preview=_scoring_preview(draft),
            notice=notice,
            custom=draft.custom,
            custom_notice=_CUSTOM_NOTICES.get(custom, ""),
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
                custom=base.custom,
                status_code=422,
            )
        await run_in_threadpool(save_scoring_config, draft)
        audit.record(audit.Event.SETTINGS_SAVED, detail="Scoring")
        return RedirectResponse("/settings/scoring?saved=1", status_code=303)

    # Your own signals (core/config.py CustomSignal), listed under the built-in sliders. They live
    # in the same scoring file, so saving the sliders keeps them (scoring_form), and saving them
    # never touches the built-in rules. The id is made from the name when a signal is added and then
    # never changes. A refused form writes nothing; an id this book doesn't have is a 404.

    async def _refuse_custom_add(
        request: Request,
        current: ScoringConfig,
        fields: Mapping[str, str],
        errors: Mapping[str, str],
    ) -> HTMLResponse:
        """The Scoring page again (422) for a refused add: the add form keeps what was typed."""
        preview = await run_in_threadpool(_scoring_preview, current)
        return await run_in_threadpool(
            _render_scoring,
            request,
            values=form_values(current),
            errors={},
            preview=preview,
            custom=current.custom,
            custom_add=custom_signals_form.typed(fields),
            custom_add_errors=errors,
            status_code=422,
        )

    @app.post("/settings/scoring/custom")
    async def add_custom_signal(request: Request) -> Response:
        """Add one of the book's own signals to the end of the list. A full list (12) or a refused
        form re-shows the page with a 422 and writes nothing."""
        fields = await _posted_fields(request)
        current = await run_in_threadpool(load_scoring_config)
        if len(current.custom) >= MAX_CUSTOM_SIGNALS:
            full = {custom_signals_form.FORM_ERROR: custom_signals_form.FULL_MESSAGE}
            return await _refuse_custom_add(request, current, fields, full)
        signal, errors = custom_signals_form.parse_signal(fields, current.custom)
        if signal is None:
            return await _refuse_custom_add(request, current, fields, errors)
        updated = current.model_copy(update={"custom": [*current.custom, signal]})
        await run_in_threadpool(save_scoring_config, updated)
        audit.record(audit.Event.SETTINGS_SAVED, detail=_CUSTOM_DETAIL)
        return RedirectResponse("/settings/scoring?custom=added#custom-signals", status_code=303)

    @app.get("/settings/scoring/custom/{signal_id}")
    def edit_custom_signal(request: Request, signal_id: str) -> HTMLResponse:
        """The edit form for one of the book's own signals."""
        config = load_scoring_config()
        index = custom_signals_form.index_of(config.custom, signal_id)
        if index is None:
            return _missing_custom_signal(request)
        return _render_custom_signal(request, config.custom[index])

    @app.post("/settings/scoring/custom/{signal_id}")
    async def save_custom_signal(request: Request, signal_id: str) -> Response:
        """Save a signal's name, unit, range, weight and text. The id stays as it was, so its
        readings stay with it. A refused form re-shows the edit page (422) and writes nothing."""
        config = await run_in_threadpool(load_scoring_config)
        index = custom_signals_form.index_of(config.custom, signal_id)
        if index is None:
            return await run_in_threadpool(_missing_custom_signal, request)
        fields = await _posted_fields(request)
        current = config.custom[index]
        others = [signal for signal in config.custom if signal.id != signal_id]
        signal, errors = custom_signals_form.parse_signal(fields, others, current=current)
        if signal is None:
            return await run_in_threadpool(
                _render_custom_signal,
                request,
                current,
                values=custom_signals_form.typed(fields),
                errors=errors,
                status_code=422,
            )
        listed = [signal if item.id == signal_id else item for item in config.custom]
        await run_in_threadpool(save_scoring_config, config.model_copy(update={"custom": listed}))
        audit.record(audit.Event.SETTINGS_SAVED, detail=_CUSTOM_DETAIL)
        return RedirectResponse("/settings/scoring?custom=saved#custom-signals", status_code=303)

    @app.get("/settings/scoring/custom/{signal_id}/remove")
    def remove_custom_signal_page(request: Request, signal_id: str) -> HTMLResponse:
        """The confirmation page for removing one of the book's own signals. It says what happens
        to its readings, so nothing is removed until the person presses the button there."""
        config = load_scoring_config()
        index = custom_signals_form.index_of(config.custom, signal_id)
        if index is None:
            return _missing_custom_signal(request)
        return _render(
            request,
            "settings/custom_signal_remove.html",
            page="Remove signal",
            active="settings",
            section="scoring",
            signal_id=signal_id,
            name=config.custom[index].name,
        )

    @app.post("/settings/scoring/custom/{signal_id}/remove")
    async def remove_custom_signal(request: Request, signal_id: str) -> Response:
        """Remove one of the book's own signals. Its readings stay on accounts but stop counting;
        a signal with the same name added later gets the same id, so they count again."""
        config = await run_in_threadpool(load_scoring_config)
        if custom_signals_form.index_of(config.custom, signal_id) is None:
            return await run_in_threadpool(_missing_custom_signal, request)
        remaining = [signal for signal in config.custom if signal.id != signal_id]
        updated = config.model_copy(update={"custom": remaining})
        await run_in_threadpool(save_scoring_config, updated)
        audit.record(audit.Event.SETTINGS_SAVED, detail=_CUSTOM_DETAIL)
        return RedirectResponse("/settings/scoring?custom=removed#custom-signals", status_code=303)

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
        encryption_notice: str = Query("", alias="notice"),
    ) -> HTMLResponse:
        if deleted_all == "1":
            notice = _deleted_all_notice()
        elif encryption_notice in _ENCRYPTION_NOTICES:
            notice = _ENCRYPTION_NOTICES[encryption_notice]
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
        audit.record(audit.Event.SETTINGS_SAVED, detail="Data & privacy")
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
        # The log goes too (it is local data); the deletion is its first entry afterwards.
        audit.clear()
        audit.record(audit.Event.ALL_DATA_DELETED)
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
        audit.record(audit.Event.SETTINGS_SAVED, detail="Data & privacy")
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
        audit.record(audit.Event.KEY_SAVED, detail="OpenRouter")
        return RedirectResponse("/settings/privacy?key=saved", status_code=303)

    @app.post("/settings/privacy/ai/key/delete")
    def remove_openrouter_key() -> RedirectResponse:
        """Remove the saved key file. A key set in the environment is not touched."""
        if secrets.delete_openrouter_key():
            audit.record(audit.Event.KEY_DELETED, detail="OpenRouter")
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
                allow_training=fields.get("allow_training") == "1",
            )
        except (research.ResearchOff, summaries.SummaryError) as problem:
            if _is_htmx(request):
                return await run_in_threadpool(_job_panel, request, None, error=problem.message)
            return await run_in_threadpool(
                _render_research,
                request,
                error=problem.message,
                confirm_training=isinstance(problem, summaries.TrainingNotConfirmed),
                refresh=fields.get("refresh") == "1",
            )
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
        ai_confirm = False
        try:
            await run_in_threadpool(
                service.summarise_account,
                account_id,
                refresh=fields.get("refresh") == "1",
                allow_training=fields.get("allow_training") == "1",
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
            ai_confirm = isinstance(problem, summaries.TrainingNotConfirmed)
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
            ai_confirm=ai_confirm,
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
        logo: str = "",
        activity: str = "",
        activity_all: str = "",
        call_code: str = Query("", alias="call"),
        template: str = "",
    ) -> HTMLResponse:
        """One account's call prep: summary, next step, score breakdown and signal history. With
        Your data showing, it also has the edit link, where each figure came from, the notices,
        the readings added by hand, the contacts and the client updates box. The website section
        shows only what is already saved. The journal parts (follow-ups, the activity history, the
        notes, the team, the trend and the renewal outcome) are on it too; `journal`, `day`, `undo`
        and `why` are its fixed codes. `activity` picks the Activity filter (a kind, else all) and
        `activity_all=1` lists every entry of it. `template` is the id of a note template the
        Notes box starts from (an unknown id leaves the box empty)."""
        status = (
            _account_status(added, edited, reading, unlinked)
            or _CONTACT_NOTICES.get(contact, "")
            or _UPDATES_NOTICES.get(updates, "")
            or journal_pages.notice_text(journal_code, day, date.today())
            or _LOGO_NOTICES.get(logo, "")
            or renewal_pages.call_notice(call_code)
        )
        return _render_account(
            request,
            account_id,
            status=status,
            why=why,
            undo=undo,
            activity=activity,
            show_all=activity_all == "1",
            template=template,
        )

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

    # Logos. The image is public to this app only (GET below); the three actions are Your data
    # only, like the other edits (_editable_account: a demo account gets its refusal page), and
    # each ends with a redirect to the account page carrying one code from _LOGO_NOTICES.

    @app.get("/logos/{account_id}")
    def logo_image(account_id: str) -> Response:
        """The account's saved logo, with the type its bytes were checked as. No logo is a plain
        404 (the page then draws the monogram)."""
        saved = logos.read_logo(service.get_store(), account_id)
        if saved is None:
            return PlainTextResponse("No logo.", status_code=404)
        data, kind = saved
        return Response(
            content=data,
            media_type=kind.value,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.post("/accounts/{account_id}/logo")
    async def upload_logo_route(request: Request, account_id: str) -> Response:
        """Save the image posted as "logo" as this account's logo, replacing any logo it had.
        Read at most one byte past the limit, so a larger file is refused without being kept."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        form = await request.form()
        upload = form.get("logo")
        if not (isinstance(upload, UploadFile) and upload.filename):
            return _logo_redirect(account_id, "logo-no-file")
        data = await upload.read(logos.MAX_LOGO_BYTES + 1)
        if len(data) > logos.MAX_LOGO_BYTES:
            return _logo_redirect(account_id, "logo-too-big")
        try:
            await run_in_threadpool(service.upload_logo, account_id, data)
        except logos.LogoError:
            return _logo_redirect(account_id, "logo-refused")
        return _logo_redirect(account_id, "logo-saved")

    @app.post("/accounts/{account_id}/logo/remove")
    async def remove_logo_route(request: Request, account_id: str) -> Response:
        """Remove the logo, uploaded or found: the account shows its monogram again."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        await run_in_threadpool(service.remove_logo, account_id)
        return _logo_redirect(account_id, "logo-removed")

    @app.post("/accounts/{account_id}/logo/find")
    async def find_logo_route(request: Request, account_id: str) -> Response:
        """Look for the logo on the account's own website, when web research is on. An uploaded
        logo is kept; the lookup's outcome comes back as a code."""
        found = await run_in_threadpool(_editable_account, request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        try:
            await run_in_threadpool(service.find_logo, account_id)
        except LookupError:
            return await run_in_threadpool(
                _missing, request, "Account not found", f"No account with id {account_id}."
            )
        except research.ResearchOff:
            code = "logo-research-off"
        except logos.LogoError as problem:
            code = _LOGO_LOOKUP_CODES.get(problem.message, "logo-not-found")
        except research.ResearchError:  # a blocked or unreachable website
            code = "logo-not-found"
        else:
            code = "logo-found"
        return _logo_redirect(account_id, code)

    # The journal. Each form posts here and returns to the account page or to Today (see
    # _journal_action). Demo data has its own journal, so these work with either data source.

    @app.get("/today")
    def today_page_route(
        request: Request,
        journal_code: str = Query("", alias="journal"),
        day: str = "",
        undo: str = "",
        q: str = "",
    ) -> HTMLResponse:
        """Today: what needs the CSM now. Follow-ups due, email threads waiting on replies, accounts
        that need attention, renewals awaiting their outcome, the next week's follow-ups and the
        snoozed accounts. `q` narrows every section to accounts whose name contains it. Each action
        on the page returns here with a fixed status line; `undo` offers Undo for a follow-up just
        marked done."""
        today_day = date.today()
        book = service.get_book(as_of=today_day)
        journals = service.get_journal().all()
        found = journal_pages.find_follow_up(journals, undo)
        shown = load_settings()  # the Settings > Follow-ups values Today uses
        return _render(
            request,
            "today.html",
            page="Today",
            active="today",
            book=True,
            screen=today_pages.screen(
                book,
                journals,
                today_day,
                q,
                quiet_days=shown.quiet_days,
                reply_wait_days=shown.reply_wait_days,
                answer_wait_days=shown.answer_wait_days,
            ),
            notice=journal_pages.notice_text(journal_code, day, today_day),
            undo_account=found[0] if found is not None else "",
            undo_id=found[1].id if found is not None else "",
        )

    @app.post("/accounts/{account_id}/notes")
    async def add_note_route(request: Request, account_id: str) -> Response:
        """Add a plain note (kind note, the Notes box), or, for backwards compatibility, a call,
        meeting, email or other touch (the Activity history). Dated today unless a date is given.
        A note needs its text; a refused one comes back with 422 and what was typed. The redirect
        goes to the box the entry landed in."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            kind = _note_kind(fields.get("kind", ""))
            service.get_journal().add_note(
                account_id,
                kind,
                fields.get("text", ""),
                _day_field(fields, "happened_on", today, _PICK_DAY),
                now=datetime.now(UTC),
                today=today,
            )
            return {"journal": "note-added" if kind is journal.TouchKind.NOTE else "activity-added"}

        # A refused note comes back on the Notes form; a refused call, meeting, email or other
        # comes back on the Activity form, with its kind checked and its message beside the kind.
        fields = await _posted_fields(request)
        is_note = fields.get("kind", "") == journal.TouchKind.NOTE.value
        return await _journal_action(
            request,
            account_id,
            form="note" if is_note else "activity",
            anchor="notes" if is_note else "activity",
            act=act,
        )

    @app.post("/accounts/{account_id}/notes/{note_id}/edit")
    async def edit_note_route(request: Request, account_id: str, note_id: str) -> Response:
        """Change an entry's kind, date or text; a plain note can become an activity, and back.
        Its edit form stays open when a save is refused. The redirect goes to the box the entry is
        in after the save."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            kind = _note_kind(fields.get("kind", ""))
            service.get_journal().update_note(
                account_id,
                note_id,
                kind,
                fields.get("text", ""),
                _day_field(fields, "happened_on", today, _PICK_DAY),
                now=datetime.now(UTC),
                today=today,
            )
            return {"journal": "note-saved" if kind is journal.TouchKind.NOTE else "activity-saved"}

        fields = await _posted_fields(request)
        section = _entry_section(_kind_of(fields.get("kind", "")))
        return await _journal_action(
            request, account_id, form="note-edit", anchor=section, act=act, item_id=note_id
        )

    @app.post("/accounts/{account_id}/notes/{note_id}/remove")
    async def remove_note_route(request: Request, account_id: str, note_id: str) -> Response:
        """Delete a plain note or an activity from the account's journal."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            kind = _entry_kind(account_id, note_id)
            if not service.get_journal().remove_note(account_id, note_id):
                raise LookupError(_NO_NOTE)
            removed = "activity-removed" if _entry_section(kind) == "activity" else "note-removed"
            return {"journal": removed}

        section = _entry_section(_entry_kind(account_id, note_id))
        return await _journal_action(
            request, account_id, form="", anchor=section, act=act, shows_refusal=False
        )

    @app.post("/accounts/{account_id}/activity")
    async def add_activity_route(request: Request, account_id: str) -> Response:
        """Log a call, meeting, email or other touch with the customer, dated today unless a date is
        given. A subject and who did it are optional; an email also says whether it was sent or
        received. Text may be left blank when there is a subject."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            kind = _activity_kind(fields.get("kind", ""))
            service.get_journal().add_activity(
                account_id,
                kind,
                fields.get("text", ""),
                _day_field(fields, "happened_on", today, _PICK_DAY),
                subject=fields.get("subject", ""),
                by=fields.get("by", ""),
                direction=_direction(fields, kind),
                now=datetime.now(UTC),
                today=today,
            )
            return {"journal": "activity-added"}

        return await _journal_action(
            request, account_id, form="activity", anchor="activity", act=act
        )

    @app.post("/accounts/{account_id}/entries/{note_id}/pin")
    async def pin_entry_route(request: Request, account_id: str, note_id: str) -> Response:
        """Pin an entry (any kind) to the pinned strip, or unpin it (pinned=0). Up to five pinned
        entries per account; a sixth is refused beside its entry."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            pinned = fields.get("pinned") == "1"
            service.get_journal().set_pinned(account_id, note_id, pinned)
            return {"journal": "pinned" if pinned else "unpinned"}

        section = _entry_section(_entry_kind(account_id, note_id))
        return await _journal_action(
            request, account_id, form="pin", anchor=section, act=act, item_id=note_id
        )

    @app.post("/accounts/{account_id}/team")
    async def add_team_route(request: Request, account_id: str) -> Response:
        """Add someone to the account team: their name, an optional email (how the mailbox knows
        an email came from them) and their role."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            service.get_journal().add_team_member(
                account_id,
                fields.get("name", ""),
                fields.get("email", ""),
                _team_role(fields.get("role", "")),
                now=datetime.now(UTC),
            )
            return {"journal": "team-added"}

        return await _journal_action(request, account_id, form="team", anchor="team", act=act)

    @app.post("/accounts/{account_id}/team/{member_id}/remove")
    async def remove_team_route(request: Request, account_id: str, member_id: str) -> Response:
        """Take someone off the account team. The entries they logged stay as they are."""

        def act(scored: ScoredAccount, fields: Mapping[str, str], today: date) -> dict[str, str]:
            if not service.get_journal().remove_team_member(account_id, member_id):
                raise LookupError(_NO_MEMBER)
            return {"journal": "team-removed"}

        return await _journal_action(
            request, account_id, form="", anchor="team", act=act, shows_refusal=False
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
            kind = signal_key(reading["kind"])
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
        audit.record(audit.Event.KEY_SAVED, detail="HubSpot")
        return RedirectResponse("/settings/connections?saved=token", status_code=303)

    @app.post("/settings/connections/token/delete")
    def delete_token() -> RedirectResponse:
        """Remove the saved token file. A token set in the environment is not touched."""
        if secrets.delete_hubspot_token():
            audit.record(audit.Event.KEY_DELETED, detail="HubSpot")
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
        if draft.target == import_flow.NEW_TARGET:
            audit.record(audit.Event.CONNECTION_ADDED, detail="Spreadsheet")
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
        audit.record(audit.Event.CONNECTION_REMOVED, detail=_CONNECTION_WORDS[config.kind])
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

    # --- Settings > Mailbox
    # The CSM's own mailbox, read-only, logged to Your data (core/mailbox.py). The password is
    # never echoed: not in a page, a redirect, a notice or a log. The helpers and notice codes
    # stay in this block, so the change sits in one place.

    _MAILBOX_NOTICES = {
        "connected": "Mailbox connected.",
        "synced": "Mailbox checked. The counts are below.",
        "sync-failed": "The check didn't finish. The reason is below.",
        "paused": "Automatic logging is paused.",
        "resumed": "Automatic logging is on again.",
        "disconnected": (
            "Mailbox disconnected. Emails already logged stay in each account's history."
        ),
        "not-connected": "Connect a mailbox first.",
    }
    _MAILBOX_PROVIDER_ERROR = "Choose your mail provider."
    _MAILBOX_TOGGLE_ERROR = "Choose pause or resume, then try again."
    _MAILBOX_SAVE_FAILED = (
        "Wardmind couldn't save the mailbox. Check that your user profile folder is writable, "
        "then try again."
    )

    def _mailbox_checked(last_sync_at: datetime | None) -> str:
        """'Last checked 4 minutes ago.', from the same relative time a feed's Synced line uses."""
        if last_sync_at is None:
            return "Not checked yet."
        now = datetime.now(UTC).replace(tzinfo=None)
        return feed_pages.synced_text(last_sync_at, now).replace("Synced", "Last checked", 1) + "."

    def _mailbox_counts(counts: mailbox.SyncCounts) -> str:
        """'12 emails logged from 340 read.', then how many were already logged, if any."""
        emails = "email" if counts.logged == 1 else "emails"
        text = f"{counts.logged} {emails} logged from {counts.read} read."
        if counts.already:
            text += f" {counts.already} {'was' if counts.already == 1 else 'were'} already logged."
        return text

    def _mailbox_page(
        request: Request,
        *,
        notice: str = "",
        form: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        toggle_error: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        """The Settings > Mailbox page. Connected: the status, Sync now, pause or resume, and
        disconnect. Not connected: the explanation and the connect form. `form` is what the form
        shows again (never the password); `errors` maps a field name to its message, and "form"
        is for the whole form."""
        if form is None:
            form = {
                "provider": mailbox.Provider.GMAIL.value,
                "host": "",
                "username": "",
                "aliases": "",
            }
        saved = service.mailbox_settings()
        checked = _mailbox_checked(saved.last_sync_at) if saved is not None else ""
        counts = ""
        if saved is not None and saved.last_counts is not None:
            counts = _mailbox_counts(saved.last_counts)
        return _render(
            request,
            "settings/mailbox.html",
            page="Mailbox",
            active="settings",
            section="mailbox",
            status_code=status_code,
            notice=notice,
            demo=load_settings().source is DataSource.DEMO,
            saved=saved,
            password_missing=saved is not None and secrets.get_secret(mailbox.SECRET_NAME) is None,
            sync_minutes=mailbox.SYNC_MINUTES,
            checked_text=checked,
            counts_text=counts,
            providers=[(provider.value, provider.label) for provider in mailbox.Provider],
            form=form,
            errors=errors or {},
            toggle_error=toggle_error,
        )

    @app.get("/settings/mailbox")
    def mailbox_page(request: Request, notice: str = "") -> HTMLResponse:
        """The page. ?notice= is one code from _MAILBOX_NOTICES; any other value shows nothing."""
        return _mailbox_page(request, notice=_MAILBOX_NOTICES.get(notice, ""))

    @app.post("/settings/mailbox/connect")
    async def mailbox_connect(request: Request) -> Response:
        """Connect the mailbox: check the login, save the password as a secret and the settings,
        then sync once. A bad field or a refused login re-renders the form (422) with the message,
        what was typed except the password, and nothing saved."""
        fields = await _posted_fields(request)
        form = {name: fields.get(name, "") for name in ("provider", "host", "username", "aliases")}
        password = fields.get("password", "")
        errors: dict[str, str] = {}
        chosen: mailbox.Provider | None
        try:
            chosen = mailbox.Provider(form["provider"])
        except ValueError:
            chosen = None
            errors["provider"] = _MAILBOX_PROVIDER_ERROR
        checks: dict[str, tuple[Callable[[str], object], str]] = {
            "username": (mailbox.clean_username, form["username"]),
            "aliases": (mailbox.clean_aliases, form["aliases"]),
            "password": (secrets.validate_secret, password),
        }
        if chosen is mailbox.Provider.OTHER:  # the host is only typed for Another provider
            checks["host"] = (mailbox.clean_host, form["host"])
        for field, (clean, value) in checks.items():
            try:
                clean(value)
            except ValueError as error:
                errors[field] = str(error)
        if chosen is None or errors:
            return await run_in_threadpool(
                _mailbox_page, request, form=form, errors=errors, status_code=422
            )
        try:
            await run_in_threadpool(
                service.connect_mailbox,
                chosen,
                form["host"],
                form["username"],
                form["aliases"],
                password,
            )
        except ValueError as error:  # checked above; this is a safety net
            errors["form"] = str(error)
        except mailbox.MailboxError as error:  # the login or the server; never the password
            errors["form"] = error.message
        except OSError:
            return await run_in_threadpool(
                _mailbox_page,
                request,
                form=form,
                errors={"form": _MAILBOX_SAVE_FAILED},
                status_code=500,
            )
        else:
            audit.record(audit.Event.MAILBOX_CONNECTED)
            return RedirectResponse("/settings/mailbox?notice=connected", status_code=303)
        return await run_in_threadpool(
            _mailbox_page, request, form=form, errors=errors, status_code=422
        )

    @app.post("/settings/mailbox/sync")
    def mailbox_sync() -> RedirectResponse:
        """Check the mailbox now. A failed check is saved on the settings, and the page shows why;
        with no mailbox connected, the page says so."""
        try:
            service.sync_mailbox_now()
        except mailbox.MailboxError:
            if service.mailbox_settings() is None:
                return RedirectResponse("/settings/mailbox?notice=not-connected", status_code=303)
            return RedirectResponse("/settings/mailbox?notice=sync-failed", status_code=303)
        return RedirectResponse("/settings/mailbox?notice=synced", status_code=303)

    @app.post("/settings/mailbox/enabled")
    async def mailbox_enabled(request: Request) -> Response:
        """Pause (on=0) or resume (on=1) the automatic check. Any other value is refused (422)."""
        choice = (await _posted_fields(request)).get("on", "")
        if choice not in ("0", "1"):
            return await run_in_threadpool(
                _mailbox_page, request, toggle_error=_MAILBOX_TOGGLE_ERROR, status_code=422
            )
        try:
            await run_in_threadpool(service.set_mailbox_enabled, choice == "1")
        except mailbox.MailboxError:
            return RedirectResponse("/settings/mailbox?notice=not-connected", status_code=303)
        notice = "resumed" if choice == "1" else "paused"
        return RedirectResponse(f"/settings/mailbox?notice={notice}", status_code=303)

    @app.post("/settings/mailbox/disconnect")
    def mailbox_disconnect() -> RedirectResponse:
        """Remove the saved settings and the password. The activity already logged stays."""
        service.disconnect_mailbox()
        audit.record(audit.Event.MAILBOX_DISCONNECTED)
        return RedirectResponse("/settings/mailbox?notice=disconnected", status_code=303)

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
                if secret:
                    audit.record(audit.Event.KEY_SAVED, detail="Data feed")
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
        if draft.connection_id is None:
            audit.record(audit.Event.CONNECTION_ADDED, detail="Data feed")
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

    # --- Settings sections that live in their own modules (each has register()) -------------
    settings_overview.register(app, _render, templates)
    settings_followups.register(app, _render, templates)
    settings_note_templates.register(app, _render, templates)
    settings_backup.register(app, _render, templates)
    settings_activity.register(app, _render, templates)

    return app
