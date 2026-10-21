"""The local web UI: a FastAPI app that answers only for 127.0.0.1 and localhost.

Routes are thin. Account data comes from `cs_brief.core.service`, the same entry point the CLI
and the MCP server use.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cs_brief.core import briefs, service
from cs_brief.core.config import load_scoring_config
from cs_brief.core.models import Health, Motion, ScoredAccount
from cs_brief.web import formatting

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
    "Referrer-Policy": "no-referrer",
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


def is_cross_site_write(method: str, headers: Mapping[str, str]) -> bool:
    """True for a state-changing request that a browser sent from another site.

    The host allow-list stops DNS rebinding, but any web page can still make the browser POST to
    127.0.0.1. Browsers send Origin on POST (and Sec-Fetch-Site on modern ones), so a write is
    refused unless it came from this app's own origin. Requests with neither header come from
    non-browser clients (tests, curl), which a web page can't forge.
    """
    if method.upper() not in _UNSAFE_METHODS:
        return False
    origin = headers.get("origin")
    if origin is not None:
        return origin != f"http://{headers.get('host', '')}"
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    return False


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


def _source_note() -> str:
    name = service.get_connector().name
    return "Showing demo data" if name == "demo" else f"Connected to {name}"


def _connector_label() -> str:
    """The data source as a fact: 'Demo data', or the connector's name."""
    name = service.get_connector().name
    return "Demo data" if name == "demo" else name


def _brief_source() -> str:
    """The brief's footer source: 'demo data', or the connector's name."""
    name = service.get_connector().name
    return "demo data" if name == "demo" else name


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
    values: dict[str, Any] = {
        "page": page,
        "active": active,
        "source_note": _source_note(),
        "today": format_today(today_day),
        "today_day": today_day,
        **context,
    }
    return templates.TemplateResponse(request, template, values, status_code=status_code)


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
    def onboarding(request: Request) -> HTMLResponse:
        return _render(request, "coming.html", page="Onboarding prep", active="onboarding")

    @app.get("/settings")
    def settings(request: Request) -> HTMLResponse:
        return _render(request, "coming.html", page="Settings", active="settings")

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

    return app
