"""The local web UI: a FastAPI app that answers only for 127.0.0.1 and localhost.

Routes are thin. Account data comes from `cs_brief.core.service`, the same entry point the CLI
and the MCP server use.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cs_brief.core import briefs, editing, import_flow, importing, secrets, service
from cs_brief.core.config import ScoringConfig, load_scoring_config, save_scoring_config
from cs_brief.core.connectors.combined import CombinedConnector, ConnectionProblem, Provenance
from cs_brief.core.connectors.errors import ConnectorError
from cs_brief.core.connectors.hubspot import (
    ConnectionCheck,
    HubSpotClient,
    HubSpotConnector,
    HubSpotMapping,
    load_mapping,
)
from cs_brief.core.connectors.local import ManualConnector, SpreadsheetConnector
from cs_brief.core.import_flow import Draft
from cs_brief.core.importing import DateOrder
from cs_brief.core.models import Account, Health, Motion, ScoredAccount, SignalKind
from cs_brief.core.onboarding import OnboardingBrief
from cs_brief.core.settings import (
    Accent,
    DataSource,
    Density,
    Landing,
    Settings,
    Theme,
    load_settings,
    save_settings,
)
from cs_brief.core.store import (
    EDITABLE_FIELDS,
    ConnectionConfig,
    ConnectionKind,
    LocalStore,
    Notice,
    connection_for,
)
from cs_brief.web import formatting
from cs_brief.web.scoring_form import (
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
}
_MOTION_ORDER = (Motion.CHURN, Motion.RENEWAL, Motion.ONBOARDING)  # the order on the filter
# The brief's motion names. Churn reads "Churn watch" here, where the filter says "Churn".
_BRIEF_MOTION_LABELS: dict[Motion, str] = {
    Motion.CHURN: "Churn watch",
    Motion.RENEWAL: "Renewal",
    Motion.ONBOARDING: "Onboarding",
}


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CROSS_SITE_MESSAGE = "Changes can only be made from cs-brief's own pages."

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


def _selected_motion(value: str) -> Motion | None:
    """The motion named in the query string, or None (all) when it is missing or not a motion."""
    try:
        return Motion(value)
    except ValueError:
        return None


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


_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
_env.filters["money"] = money
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
        **context,
    }
    return templates.TemplateResponse(request, template, values, status_code=status_code)


def _missing(request: Request, page: str, message: str) -> HTMLResponse:
    """The plain 404 page, in the layout, with the message."""
    return _render(request, "not_found.html", page=page, status_code=404, message=message)


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
    "cs-brief couldn't save the token. Check that your user profile folder is writable, "
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
    """The effective HubSpot mapping as (what cs-brief shows, where HubSpot keeps it) pairs."""
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


def _connection_lines(
    store: LocalStore, combined: CombinedConnector | None
) -> list[_ConnectionLine]:
    """One line per connection, in the order the store keeps them."""
    today = date.today()
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
            else:
                member = ManualConnector(store)
            accounts = [] if member is None else member.list_accounts()
        except ConnectorError:
            continue
        for account in accounts:
            names.setdefault(account.id, account.name)
    return [(account_id, names.get(account_id, account_id)) for account_id in hidden]


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


def _submitted_mapping(fields: Mapping[str, str]) -> dict[str, str]:
    """The mapping selects, keyed by import field: column-<index> is the header chosen."""
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
_UNKNOWN_SIGNAL = "That signal isn't one cs-brief knows."
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
        "start_date": account.start_date.isoformat(),
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
    """"A", "A and B", "A, B and C"."""
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


def create_app() -> FastAPI:
    """Build the web app: no API docs, a host allow-list, and security headers on every response."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_cross_site_write(request.method, request.headers):
            return PlainTextResponse(CROSS_SITE_MESSAGE, status_code=403)
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
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code != 404:
            return await http_exception_handler(request, exc)
        return _render(
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
        motion: str = "",
        q: str = "",
        imported: str = "",
        removed: str = "",
        hidden: str = "",
        deleted: str = "",
    ) -> Response:
        """My book. An htmx refresh gets only the results partial, not on a history restore.
        With Your data and nothing connected, this is the Setup page instead."""
        if _needs_setup():
            return RedirectResponse("/setup", status_code=303)
        book = service.get_book()
        brief = briefs.weekly_brief(book, as_of=date.today())
        selected = _selected_motion(motion)
        ranks = {item.account.id: rank for rank, item in enumerate(book, start=1)}
        rows: list[tuple[int, ScoredAccount]] = [
            (ranks[item.account.id], item) for item in service.filter_book(book, selected, q)
        ]
        first, second = summary_sentences(
            brief.counts_by_health[Health.AT_RISK],
            brief.arr_at_risk,
            brief.counts_by_health[Health.WATCH],
        )
        options: list[dict[str, Any]] = [
            {
                "value": "",
                "label": "All",
                "count": brief.total_accounts,
                "checked": selected is None,
            },
            *(
                {
                    "value": motion_kind.value,
                    "label": motion_kind.label,
                    "count": brief.counts_by_motion[motion_kind],
                    "checked": selected == motion_kind,
                }
                for motion_kind in _MOTION_ORDER
            ),
        ]
        partial = request.headers.get("HX-Request") == "true" and (
            "HX-History-Restore-Request" not in request.headers
        )
        response = _render(
            request,
            "partials/book_results.html" if partial else "book.html",
            page="My book",
            active="book",
            book=not partial,
            summary_first=first,
            summary_second=second,
            motion_options=options,
            selected_value="" if selected is None else selected.value,
            rows=rows,
            q=q,
            count_text=result_count_text(len(rows)),
            intro=not partial,
            oob=partial,
            duplicate_count=0 if partial else _duplicate_count(),
            import_notice=[] if partial else _import_notice(imported, removed),
            hidden_name="" if partial else hidden,
            deleted_name="" if partial else deleted,
        )
        response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
        return response

    @app.get("/brief")
    def weekly_brief(request: Request) -> HTMLResponse:
        """The weekly brief: where ARR sits, this week's call-outs, the motions and the top ten."""
        brief = briefs.weekly_brief(service.get_book(), as_of=date.today(), top_n=10)
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
            current = load_settings().model_copy(update=chosen)
            return _render_appearance(request, current, errors=errors, status_code=422)
        # Only the four appearance choices change; the data source is kept as it was.
        save_settings(load_settings().model_copy(update=chosen))
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
        draft, errors = parse_scoring_form(await _posted_fields(request), load_scoring_config())
        preview = _scoring_preview(draft) if draft is not None else _preview_alerts(errors)
        return templates.TemplateResponse(
            request, "settings/partials/scoring_preview.html", {"preview": preview}
        )

    @app.post("/settings/scoring")
    async def save_scoring(request: Request) -> Response:
        """Save the scoring form. A bad value re-renders the page with each message (422) and
        writes nothing. Cross-site posts never get here (see same_origin_writes)."""
        fields = await _posted_fields(request)
        base = load_scoring_config()
        draft, errors = parse_scoring_form(fields, base)
        if draft is None:
            # Keep what was typed, so the person fixes the one value instead of retyping the form.
            typed = {name: fields[name] for name in FIELD_NAMES if name in fields}
            return _render_scoring(
                request,
                values={**form_values(base), **typed},
                errors=errors,
                preview=_preview_alerts(errors),
                status_code=422,
            )
        save_scoring_config(draft)
        return RedirectResponse("/settings/scoring?saved=1", status_code=303)

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
            account = editing.create_account(service.get_store(), typed, today=date.today())
        except editing.EditError as error:
            return _render_new(
                request, values=typed, errors=error.errors, refused=True, status_code=422
            )
        if load_settings().source is DataSource.DEMO:
            save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))
        service.reset_connectors()
        return RedirectResponse(f"/accounts/{_quoted_id(account.id)}?added=1", status_code=303)

    @app.get("/accounts/{account_id}")
    def account(
        request: Request,
        account_id: str,
        edited: str = "",
        reading: str = "",
        added: str = "",
        unlinked: str = "",
    ) -> HTMLResponse:
        """One account's call prep: summary, next step, score breakdown and signal history. With
        Your data showing, it also has the edit link, where each figure came from, the notices
        and the readings added by hand."""
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
        manual: set[tuple[str, datetime, float]] = set()
        if combined is not None:
            notices = service.get_store().notices(account_id)
            manual = {
                (signal.kind.value, signal.observed_at, signal.value)
                for signal in combined.list_signals(account_id)
                if signal.source == "manual"
            }
        edits = prov.edits if prov is not None else {}
        return _render(
            request,
            "account.html",
            page=detail.explanation.name,
            active="book",
            book=True,
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
            manual_readings=manual,
            status=_account_status(added, edited, reading, unlinked),
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
        found = _editable_account(request, account_id)
        if isinstance(found, HTMLResponse):
            return found
        combined, account, prov = found
        fields = await _posted_fields(request)
        submitted = {name: fields[name] for name in EDITABLE_FIELDS if name in fields}
        try:
            changed = editing.edit_account(
                service.get_store(), combined, account_id, submitted, now=datetime.now(UTC)
            )
        except editing.EditError as error:
            return _render_edit(
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
        found = _editable_account(request, account_id)
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
            return refuse(_UNKNOWN_SIGNAL)
        typed = reading["yes_no"] if kind is SignalKind.CHAMPION_LEFT else reading["value"]
        try:
            editing.add_reading(
                service.get_store(),
                combined,
                account_id,
                kind,
                typed,
                now=datetime.now(UTC),
            )
        except editing.EditError as error:
            return refuse(str(error))
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
        combined, account, _ = found
        editing.set_hidden(service.get_store(), combined, account_id, True)
        return RedirectResponse(f"/?{urlencode({'hidden': account.name})}", status_code=303)

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
        return RedirectResponse(f"/?{urlencode({'deleted': account.name})}", status_code=303)

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

    # Your data: possible duplicates. Both ids of a link or a dismissal are checked first.

    @app.get("/duplicates")
    def duplicates_page(request: Request, linked: str = "", dismissed: str = "") -> HTMLResponse:
        """Accounts from different connections that look like the same customer."""
        combined = _your_book()
        if combined is None:
            return _missing(request, "Page not found", _DUPLICATES_DEMO)
        status = ""
        if linked:
            status = f"Linked {linked}."
        elif dismissed == "1":
            status = "Got it. They won't be suggested again."
        return _render_duplicates(request, combined, status=status)

    @app.post("/duplicates/link")
    async def link_duplicates(request: Request) -> Response:
        """Link two accounts into one, keeping the name of the one chosen."""
        combined = _your_book()
        if combined is None:
            return _missing(request, "Page not found", _DUPLICATES_DEMO)
        fields = await _posted_fields(request)
        first = fields.get("first", "")
        second = fields.get("second", "")
        primary = fields.get("primary", "")
        accounts = {account.id: account for account in combined.list_accounts()}
        if first not in accounts or second not in accounts:
            return _render_duplicates(request, combined, error=_NOT_IN_BOOK_FORM, status_code=422)
        name = accounts[primary].name if primary in accounts else accounts[first].name
        try:
            editing.link_accounts(service.get_store(), combined, [first, second], primary)
        except editing.EditError as error:
            return _render_duplicates(request, combined, error=str(error), status_code=422)
        return RedirectResponse(f"/duplicates?{urlencode({'linked': name})}", status_code=303)

    @app.post("/duplicates/dismiss")
    async def dismiss_duplicate_route(request: Request) -> Response:
        """Mark a suggested pair as not the same, so it is not suggested again."""
        combined = _your_book()
        if combined is None:
            return _missing(request, "Page not found", _DUPLICATES_DEMO)
        fields = await _posted_fields(request)
        first, second = sorted((fields.get("first", ""), fields.get("second", "")))
        suggested = {suggestion.account_ids for suggestion in combined.duplicate_suggestions()}
        if (first, second) not in suggested:
            return _render_duplicates(request, combined, error=_NOT_SUGGESTED, status_code=422)
        editing.dismiss_duplicate(service.get_store(), combined, (first, second))
        return RedirectResponse("/duplicates?dismissed=1", status_code=303)

    @app.get("/settings/connections")
    def connections(
        request: Request,
        saved: str = "",
        removed: str = "",
        refreshed: str = "",
        shown: str = "",
    ) -> HTMLResponse:
        """Where the data comes from, the connections, whether HubSpot works, and what it reads."""
        notice = ""
        if saved == "source":
            notice = "Data source saved."
        elif saved == "token":
            notice = "Token saved."
        elif saved == "connection":
            notice = "Connection saved."
        elif shown == "1":
            notice = "Account shown again."
        elif removed == "token":
            notice = "Token removed."
        elif removed:
            notice = f"Removed {removed}."
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
            return _render_connections(
                request, source_error="Choose your data or demo data.", status_code=422
            )
        save_settings(load_settings().model_copy(update={"source": chosen}))
        service.reset_connectors()
        return RedirectResponse("/settings/connections?saved=source", status_code=303)

    @app.post("/settings/connections/token")
    async def save_token(request: Request) -> Response:
        """Store the token in the user profile and make sure a HubSpot connection exists (adding
        it is a no-op when it does). A token that isn't valid re-renders the field with the reason
        (422) and an empty input; the token is never shown back."""
        fields = await _posted_fields(request)
        try:
            secrets.save_hubspot_token(fields.get("token", ""))
        except ValueError as error:
            return _render_connections(request, token_error=str(error), status_code=422)
        except OSError:
            return _render_connections(request, token_error=_TOKEN_SAVE_FAILED, status_code=500)
        service.get_store().add_connection(ConnectionKind.HUBSPOT, "HubSpot")
        service.reset_connectors()
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
            headers={"Content-Disposition": 'attachment; filename="cs-brief-template.csv"'},
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
            return _render_import_form(request, store, target="new", error=message, status_code=422)
        upload = form.get("file")
        has_file = isinstance(upload, UploadFile) and bool(upload.filename)
        pasted = _text_field(form.get("pasted"))
        has_paste = bool(pasted.strip())
        target = _chosen_target(store, _text_field(form.get("target")))
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
            return refuse(_CHOOSE_ONE_SOURCE)
        if not has_file and not has_paste:
            return refuse(_CHOOSE_FILE_OR_PASTE)
        if target == import_flow.NEW_TARGET and new_name:
            try:
                import_flow.validate_name(new_name)
            except ValueError as error:
                return refuse(str(error))
        try:
            if has_file and isinstance(upload, UploadFile):
                # Read one byte past the limit: the reader then refuses an over-size file itself.
                data = await upload.read(importing.MAX_BYTES + 1)
                table = importing.read_upload(data, upload.filename or "")
                draft = import_flow.save_draft(
                    store,
                    table=table,
                    file_bytes=data,
                    target=target,
                    new_name=new_name,
                    back=back,
                )
            else:
                table = importing.read_pasted(pasted)
                draft = import_flow.save_draft(
                    store,
                    table=table,
                    pasted_text=pasted,
                    target=target,
                    new_name=new_name,
                    back=back,
                )
        except (importing.ImportError_, ValueError) as error:
            return refuse(str(error))
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
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields)
        order_text = fields.get("date_order", "")
        draft = import_flow.remember_choices(
            store, draft, mapping=mapping, date_order=_as_order(order_text)
        )
        return _render_mapping(request, store, draft, mapping, order_text)

    @app.post("/import/{draft_id}/preview")
    async def import_preview(request: Request, draft_id: str) -> Response:
        """The htmx partial: the examples and "What will happen", for the mapping as chosen."""
        store = service.get_store()
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields)
        order_text = fields.get("date_order", "")
        draft = import_flow.remember_choices(
            store, draft, mapping=mapping, date_order=_as_order(order_text)
        )
        view = _mapping_view(store, draft, mapping, order_text)
        return templates.TemplateResponse(
            request,
            "import/partials/preview.html",
            {"draft_id": draft.id, "oob": True, **view},
        )

    @app.post("/import/{draft_id}/sheet")
    async def import_sheet(request: Request, draft_id: str) -> Response:
        """Switch to another sheet of the same Excel file, then back to the mapping page."""
        store = service.get_store()
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        try:
            import_flow.reread_sheet(store, draft, fields.get("sheet", ""))
        except importing.ImportError_ as error:
            return _render_mapping(
                request,
                store,
                draft,
                _current_mapping(store, draft),
                draft.date_order or "",
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(f"/import/{draft_id}", status_code=303)

    @app.post("/import/{draft_id}/commit")
    async def import_commit(request: Request, draft_id: str) -> Response:
        """Import the draft. A refusal re-renders the mapping page with the reason (422)."""
        store = service.get_store()
        draft = import_flow.load_draft(store, draft_id)
        if draft is None:
            return _missing(request, "Import not found", _NO_DRAFT)
        fields = await _posted_fields(request)
        mapping = _submitted_mapping(fields)
        order_text = fields.get("date_order", "")
        order = import_flow.resolve_date_order(draft, mapping, order_text)
        try:
            outcome = import_flow.commit(
                draft, mapping, order, store, now=datetime.now(UTC), today=date.today()
            )
        except ValueError as error:
            return _render_mapping(
                request, store, draft, mapping, order_text, error=str(error), status_code=422
            )
        if load_settings().source is DataSource.DEMO:
            save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))
        service.reset_connectors()
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
        config = store.get_connection(connection_id)
        if config is None:
            return _missing(request, "Connection not found", _NO_CONNECTION)
        fields = await _posted_fields(request)
        enabled = fields.get("enabled") == "1"
        store.update_connection(config.model_copy(update={"enabled": enabled}))
        return RedirectResponse("/settings/connections?saved=connection", status_code=303)

    @app.post("/settings/connections/{connection_id}/rename")
    async def rename_connection(request: Request, connection_id: str) -> Response:
        """Rename a spreadsheet connection. A blank name, or one over 60 characters, is refused."""
        store = service.get_store()
        config = store.get_connection(connection_id)
        if config is None:
            return _missing(request, "Connection not found", _NO_CONNECTION)
        if config.kind is not ConnectionKind.SPREADSHEET:
            return _render_connections(
                request,
                rename_errors={connection_id: "Only spreadsheets can be renamed."},
                status_code=422,
            )
        fields = await _posted_fields(request)
        typed = fields.get("name", "")
        try:
            name = import_flow.validate_name(typed)
        except ValueError as error:
            return _render_connections(
                request,
                rename_errors={connection_id: str(error)},
                rename_values={connection_id: typed},
                status_code=422,
            )
        store.update_connection(config.model_copy(update={"name": name}))
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
        return _render(
            request,
            "settings/remove.html",
            page=f"Remove {config.name}",
            active="settings",
            config=config,
            leaving=f"{leaving} and their history",
            is_hubspot=config.kind is ConnectionKind.HUBSPOT,
            is_sheet=config.kind is ConnectionKind.SPREADSHEET,
        )

    @app.post("/settings/connections/{connection_id}/remove")
    def remove_connection(request: Request, connection_id: str) -> Response:
        """Remove a connection and what came with it. HubSpot's saved token goes too."""
        store = service.get_store()
        config = store.get_connection(connection_id)
        if config is None:
            return _missing(request, "Connection not found", _NO_CONNECTION)
        store.remove_connection(config.id)
        if config.kind is ConnectionKind.HUBSPOT:
            secrets.delete_hubspot_token()
        service.reset_connectors()
        return RedirectResponse(
            f"/settings/connections?{urlencode({'removed': config.name})}", status_code=303
        )

    @app.post("/settings/connections/hidden/{account_id}/show")
    async def show_hidden_account(request: Request, account_id: str) -> Response:
        """Bring a hidden account back into the book."""
        store = service.get_store()
        if account_id not in store.hidden():
            return _missing(request, "Account not found", _NOT_HIDDEN)
        connector = _combined_or_none() or CombinedConnector([], store)
        editing.set_hidden(store, connector, account_id, False)
        return RedirectResponse("/settings/connections?shown=1", status_code=303)

    return app
