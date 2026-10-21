"""The local web UI: a FastAPI app that answers only for 127.0.0.1 and localhost.

Routes are thin. Account data comes from `cs_brief.core.service`, the same entry point the CLI
and the MCP server use.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cs_brief.core import briefs, secrets, service
from cs_brief.core.config import ScoringConfig, load_scoring_config, save_scoring_config
from cs_brief.core.connectors.errors import ConnectorError
from cs_brief.core.connectors.hubspot import (
    ConnectionCheck,
    HubSpotClient,
    HubSpotConnector,
    HubSpotMapping,
    load_mapping,
)
from cs_brief.core.models import Health, Motion, ScoredAccount, SignalKind
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
from cs_brief.core.store import ConnectionKind
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
    **context: Any,
) -> HTMLResponse:
    """Render one page in the shared layout, with the context every page needs."""
    today_day = date.today()
    current = load_settings()
    values: dict[str, Any] = {
        "page": page,
        "active": active,
        "settings": current,
        "source_note": _source_note(current),
        "today": format_today(today_day),
        "today_day": today_day,
        **context,
    }
    return templates.TemplateResponse(request, template, values, status_code=status_code)


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


def _render_connections(
    request: Request,
    *,
    notice: str = "",
    source_error: str = "",
    token_error: str = "",
    test_check: ConnectionCheck | None = None,
    test_error: str = "",
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
    def my_book(request: Request, motion: str = "", q: str = "") -> HTMLResponse:
        """My book. An htmx refresh gets only the results partial, not on a history restore."""
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
            summary_first=first,
            summary_second=second,
            motion_options=options,
            selected_value="" if selected is None else selected.value,
            rows=rows,
            q=q,
            count_text=result_count_text(len(rows)),
            intro=not partial,
            oob=partial,
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
            )
        # The signals table scales its bars to the largest weight in the scoring config, as the
        # account page does, so the same signal is drawn the same width on both pages.
        longest = max(rule.weight for rule in load_scoring_config().signals.values())
        return _render(
            request,
            "onboarding_brief.html",
            page=f"{brief.account.name} onboarding brief",
            active="onboarding",
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

    @app.get("/accounts/{account_id}")
    def account(request: Request, account_id: str) -> HTMLResponse:
        """One account's call prep: summary, next step, score breakdown and signal history."""
        detail = service.get_account_detail(account_id)
        if detail is None:
            return _render(
                request,
                "not_found.html",
                page="Account not found",
                status_code=404,
                message=f"No account with id {account_id}.",
            )
        return _render(
            request,
            "account.html",
            page=detail.explanation.name,
            active="book",
            detail=detail,
            thresholds=load_scoring_config().thresholds,
            timeline_days=formatting.group_by_day(detail.timeline),
            connector_label=_connector_label(),
        )

    @app.get("/settings/connections")
    def connections(
        request: Request, saved: str = "", removed: str = "", refreshed: str = ""
    ) -> HTMLResponse:
        """Where the data comes from, whether HubSpot is set up and working, and what it reads."""
        notice = ""
        if saved == "source":
            notice = "Data source saved."
        elif saved == "token":
            notice = "Token saved."
        elif removed == "token":
            notice = "Token removed."
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

    return app
