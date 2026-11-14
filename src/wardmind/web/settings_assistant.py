"""Settings > Assistant: suggestions on Today and how they adapt to what the CSM does with them
(core/taste.py, with a Reset for each kind), what Claude may change (McpWrites), and whether an AI
may improve the wording of email drafts (ai_drafts, and ai_drafts_cloud for a model online).

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.

A save is all or nothing: a bad value re-renders the form (422) with a message and writes nothing;
otherwise the settings are saved, the change goes into the activity log and the page shows the
notice. A checkbox that isn't posted is off. A Reset (POST /settings/assistant/taste/{detector}/
reset) forgets what was learnt about one kind of suggestion, then goes back with its notice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from wardmind.core import audit, service, taste
from wardmind.core.proposals import Detector
from wardmind.core.settings import McpWrites, Settings, load_settings, save_settings
from wardmind.web import effect_pages

_log = logging.getLogger(__name__)

Render = Callable[..., HTMLResponse]

PAGE = "/settings/assistant"
CHECKBOX_FIELDS = ("suggestions", "suggestions_learn", "ai_drafts", "ai_drafts_cloud")
MODE_FIELD = "mcp_writes"  # a radio: McpWrites values off, propose or direct
SAVED_NOTICE = "Assistant settings saved."
MODE_MESSAGE = "Pick Off, Suggest only or Make changes."
NOT_SAVEABLE = "These settings can't be saved. Check them and try again."
FORM_ERROR = "form"  # the error key for a message about the whole form
AUDIT_DETAIL = "Assistant"  # the section named in the activity log
NOT_A_KIND = "That kind of suggestion isn't one Wardmind makes."


def _reset_code(kind: Detector) -> str:
    """The ?saved= code a Reset of this kind redirects back with."""
    return f"forgot-{kind.value}"


# The notice for each Reset code, from the kind's label (never from the query text).
RESET_NOTICES = {
    _reset_code(kind): f"Wardmind forgot what it learnt about {taste.LABELS[kind]}."
    for kind in Detector
}


def values_of(settings: Settings) -> dict[str, Any]:
    """The form's values for saved settings: checkboxes as on or off, the mode as its value."""
    values: dict[str, Any] = {name: getattr(settings, name) for name in CHECKBOX_FIELDS}
    values[MODE_FIELD] = settings.mcp_writes.value
    return values


def form_values(posted: Mapping[str, str]) -> dict[str, Any]:
    """The values a POST carries, as the form shows them again: each checkbox on or off, and the
    mode's text as typed (checked by check_form)."""
    values: dict[str, Any] = {name: name in posted for name in CHECKBOX_FIELDS}
    values[MODE_FIELD] = posted.get(MODE_FIELD, "").strip()
    return values


def check_form(
    values: Mapping[str, Any], current: Settings
) -> tuple[Settings | None, dict[str, str]]:
    """The settings the form describes, or None with a message for each field that is wrong. The
    other settings come from `current`. Cloud drafts need the wording on as well: a posted
    ai_drafts_cloud without ai_drafts is saved off."""
    chosen: dict[str, Any] = {name: bool(values[name]) for name in CHECKBOX_FIELDS}
    chosen["ai_drafts_cloud"] = chosen["ai_drafts"] and chosen["ai_drafts_cloud"]
    errors: dict[str, str] = {}
    try:
        chosen[MODE_FIELD] = McpWrites(values[MODE_FIELD])
    except ValueError:
        errors[MODE_FIELD] = MODE_MESSAGE
    if errors:
        return None, errors
    try:
        return Settings.model_validate({**current.model_dump(), **chosen}), {}
    except ValidationError:
        return None, {FORM_ERROR: NOT_SAVEABLE}


def taste_rows(on: bool) -> list[taste.DetectorTaste] | None:
    """How suggestions adapt (service.get_tastes), one row per kind, for the section shown while
    suggestions are on. None hides the section: when they are off, or when the suggestions file
    can't be read (only the exception's type is logged)."""
    if not on:
        return None
    try:
        return service.get_tastes()
    except Exception as exc:
        _log.warning("How suggestions adapt wasn't read (%s).", type(exc).__name__)
        return None


def _notice_for(code: str) -> str:
    """The notice for a ?saved= code: "1" after a save, a Reset's code after a Reset, else none."""
    if code == "1":
        return SAVED_NOTICE
    return RESET_NOTICES.get(code, "")


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    def page(
        request: Request,
        *,
        values: dict[str, Any],
        errors: dict[str, str],
        notice: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return render(
            request,
            "settings/assistant.html",
            page="Assistant",
            active="settings",
            section="assistant",
            values=values,
            errors=errors,
            notice=notice,
            status_code=status_code,
            # "What followed your steps" (effect_pages.summary_table): shown only when it has rows.
            step_rows=effect_pages.summary_table(),
            # "How suggestions adapt to you": shown while the Suggestions box on the form is ticked.
            taste_rows=taste_rows(bool(values["suggestions"])),
        )

    def not_found(request: Request) -> HTMLResponse:
        return render(
            request,
            "not_found.html",
            page="Assistant",
            active="settings",
            status_code=404,
            message=NOT_A_KIND,
            back_href=PAGE,
            back_label="Back to Assistant",
        )

    @app.get("/settings/assistant")
    def assistant(request: Request, saved: str = "") -> HTMLResponse:
        """The saved values and, after a save or a Reset, its notice. ?saved= is one fixed code
        (see _notice_for); any other value shows nothing."""
        current = load_settings()
        return page(
            request,
            values=values_of(current),
            errors={},
            notice=_notice_for(saved),
        )

    @app.post("/settings/assistant")
    async def save_assistant(request: Request) -> Response:
        """Save the assistant settings. A bad value re-renders the form (422) with each message and
        writes nothing; otherwise the settings are saved and logged, and the notice shows."""
        form = await request.form()
        posted = {key: value for key, value in form.items() if isinstance(value, str)}
        values = form_values(posted)
        current = await run_in_threadpool(load_settings)
        checked, errors = check_form(values, current)
        if checked is None:
            return await run_in_threadpool(
                page, request, values=values, errors=errors, status_code=422
            )
        await run_in_threadpool(save_settings, checked)
        await run_in_threadpool(audit.record, audit.Event.SETTINGS_SAVED, detail=AUDIT_DETAIL)
        return RedirectResponse(f"{PAGE}?saved=1", status_code=303)

    @app.post("/settings/assistant/taste/{detector}/reset")
    async def reset_taste(request: Request, detector: str) -> Response:
        """Forget what was learnt about one kind of suggestion (core/taste.py), then back to the
        page with its notice. A detector that isn't one of the five is a 404 page."""
        try:
            kind = Detector(detector)
        except ValueError:
            return await run_in_threadpool(not_found, request)
        await run_in_threadpool(service.reset_taste, kind)
        return RedirectResponse(f"{PAGE}?saved={_reset_code(kind)}", status_code=303)
