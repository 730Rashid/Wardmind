"""Settings > Follow-ups: when new follow-ups are due, and the automatic follow-ups (core/rules.py).

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.

A save is all or nothing: every value is checked (Settings.model_validate) before anything is
written, and a bad value re-renders the form with a message for each field (422). A checkbox that
isn't posted is off.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from wardmind.core import rules, service
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.settings import (
    RENEWAL_RULE_CHOICES,
    DataSource,
    Settings,
    load_settings,
    save_settings,
)

Render = Callable[..., HTMLResponse]

# The posted fields. Numbers arrive as text (so a bad one can be shown again as typed); checkboxes
# are on when posted and off when absent.
WHOLE_DAY_FIELDS = (
    "follow_up_days",
    "quiet_days",
    "reply_wait_days",
    "answer_wait_days",
    "rule_at_risk_days",
)
RENEWAL_FIELD = "rule_renewal_days"  # a select of RENEWAL_RULE_CHOICES
CHECKBOX_FIELDS = ("skip_weekends", "rule_at_risk", "rule_renewal", "rule_champion_left")
MAX_TYPED = 8  # the longest text a number box can hold; longer input is refused
MAX_ECHO = 20  # characters of a typed number shown again (the rest is never echoed)
NOT_A_NUMBER = "Type a whole number of days."
NOT_A_RENEWAL_CHOICE = "Pick 30, 60, 90 or 120 days."
SAVED_NOTICE = "Follow-ups saved."
FORM_ERROR = "form"  # the error key for a message about the whole form
NOT_SAVEABLE = "These settings can't be saved. Check them and try again."
_VALIDATOR_PREFIX = "Value error, "  # pydantic puts this in front of a validator's message
_NUMBER_FIELDS = (*WHOLE_DAY_FIELDS, RENEWAL_FIELD)


def values_of(settings: Settings) -> dict[str, Any]:
    """The form's values for saved settings: numbers as text, checkboxes as on or off."""
    values: dict[str, Any] = {name: getattr(settings, name) for name in CHECKBOX_FIELDS}
    values.update({name: str(getattr(settings, name)) for name in _NUMBER_FIELDS})
    return values


def form_values(posted: Mapping[str, str]) -> dict[str, Any]:
    """The values a POST carries, as the form shows them: a number box's text, cut to MAX_ECHO
    characters for showing again (check_form still sees the text's full length, so a long number
    is refused rather than shortened), and each checkbox on or off."""
    values: dict[str, Any] = {name: name in posted for name in CHECKBOX_FIELDS}
    values.update({name: posted.get(name, "").strip()[:MAX_ECHO] for name in _NUMBER_FIELDS})
    return values


def check_form(
    values: Mapping[str, Any], current: Settings
) -> tuple[Settings | None, dict[str, str]]:
    """The settings the form describes, checked by Settings.model_validate, or None with a message
    for each field that is wrong. Fields that are fine are checked too, so every message shows at
    once. The other settings come from `current`."""
    chosen: dict[str, Any] = {name: bool(values[name]) for name in CHECKBOX_FIELDS}
    errors: dict[str, str] = {}
    for name in _NUMBER_FIELDS:
        text = str(values[name])
        if len(text) > MAX_TYPED:
            errors[name] = NOT_A_RENEWAL_CHOICE if name == RENEWAL_FIELD else NOT_A_NUMBER
            continue
        try:
            chosen[name] = int(text)
        except ValueError:
            errors[name] = NOT_A_RENEWAL_CHOICE if name == RENEWAL_FIELD else NOT_A_NUMBER
    checked: Settings | None = None
    try:
        checked = Settings.model_validate({**current.model_dump(), **chosen})
    except ValidationError as error:
        for problem in error.errors():
            field = str(problem["loc"][0]) if problem["loc"] else ""
            message = str(problem["msg"]).removeprefix(_VALIDATOR_PREFIX)
            if field in _NUMBER_FIELDS and field not in errors:
                errors[field] = message
        if not errors:  # a value this form doesn't set is invalid: say so, don't name a field
            errors[FORM_ERROR] = NOT_SAVEABLE
    if errors or checked is None:
        return None, errors
    return checked, {}


def preview_line(settings: Settings, today: date | None = None) -> str:
    """'With these rules on, Wardmind would add 2 follow-ups today.' for Your data with a rule on,
    computed from the book now without saving anything. "" when no rule is on or demo data is
    showing (the rules never add to demo data)."""
    if settings.source is DataSource.DEMO or not rules.any_rule_on(settings):
        return ""
    day = today if today is not None else date.today()
    connector = service.get_connector()
    try:
        book = service.get_book(connector, as_of=day)
        signals = connector.list_signals()
    except ConnectorError:
        return ""
    at_risk = rules.at_risk_since(book, service.history_store(), day)
    count = len(
        rules.planned_follow_ups(
            book,
            service.get_journal().all(),
            settings,
            day,
            at_risk,
            signals=signals,
        )
    )
    noun = "follow-up" if count == 1 else "follow-ups"
    return f"With these rules on, Wardmind would add {count} {noun} today."


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    def page(
        request: Request,
        *,
        values: dict[str, Any],
        errors: dict[str, str],
        preview: str = "",
        notice: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return render(
            request,
            "settings/follow_ups.html",
            page="Follow-ups",
            active="settings",
            section="follow-ups",
            values=values,
            errors=errors,
            preview=preview,
            renewal_choices=RENEWAL_RULE_CHOICES,
            notice=notice,
            status_code=status_code,
        )

    @app.get("/settings/follow-ups")
    def follow_ups(request: Request, saved: str = "") -> HTMLResponse:
        """The saved values, the preview line and, after a save, the notice. ?saved= is only
        read as "1"; the notice's words are fixed here."""
        current = load_settings()
        return page(
            request,
            values=values_of(current),
            errors={},
            preview=preview_line(current),
            notice=SAVED_NOTICE if saved == "1" else "",
        )

    @app.post("/settings/follow-ups")
    async def save_follow_ups(request: Request) -> Response:
        """Save the follow-up settings. A bad value re-renders the form (422) with each message and
        writes nothing; otherwise the settings are saved and the page shows the notice."""
        form = await request.form()
        posted = {key: value for key, value in form.items() if isinstance(value, str)}
        values = form_values(posted)
        current = await run_in_threadpool(load_settings)
        checked, errors = check_form(values, current)
        if checked is None:
            return page(request, values=values, errors=errors, status_code=422)
        await run_in_threadpool(save_settings, checked)
        return RedirectResponse("/settings/follow-ups?saved=1", status_code=303)
