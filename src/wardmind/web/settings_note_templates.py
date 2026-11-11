"""Settings > Note templates: saved starting text for notes (core/note_templates.py), offered on
every account's note form.

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.

Every write is a POST that answers 303 back to the list with a fixed notice code (?saved=), or 422
with the typed values and the message by field, and writes nothing. A template id in a URL must
look like t-<8 hex>; anything else, or an id that isn't in this book, is a 404.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from wardmind.core import note_templates

Render = Callable[..., HTMLResponse]

# The notice codes the list accepts in ?saved=. Each maps to fixed words, never to query text.
NOTICES = {
    "added": "Template added.",
    "updated": "Template saved.",
    "deleted": "Template deleted.",
    "starters": "Starter templates added.",
}
_PAGE = "/settings/note-templates"


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    def not_found(request: Request) -> HTMLResponse:
        return render(
            request,
            "not_found.html",
            page="Template not found",
            active="settings",
            status_code=404,
            message="That template isn't in this book.",
            back_href=_PAGE,
            back_label="Back to Note templates",
        )

    def list_page(
        request: Request,
        *,
        notice: str = "",
        status_code: int = 200,
        add_values: dict[str, str] | None = None,
        add_errors: dict[str, str] | None = None,
        edit_id: str = "",
        edit_values: dict[str, str] | None = None,
        edit_errors: dict[str, str] | None = None,
        starters_error: str = "",
    ) -> HTMLResponse:
        """The list, with the add form, or the edit form for edit_id when it is set."""
        found = note_templates.all_templates()
        last = len(found) - 1
        rows = [
            {
                "id": item.id,
                "name": item.name,
                "body": item.body,
                "first": index == 0,
                "last": index == last,
            }
            for index, item in enumerate(found)
        ]
        edit = edit_values or {"name": "", "body": ""}
        add = add_values or {"name": "", "body": ""}
        context: dict[str, Any] = {
            "rows": rows,
            "can_add": len(found) < note_templates.MAX_TEMPLATES,
            "max_templates": note_templates.MAX_TEMPLATES,
            "missing": note_templates.missing_starters(),
            "starters_error": starters_error,
            "placeholders": note_templates.PLACEHOLDERS,
            "name_max": note_templates.MAX_NAME,
            "body_max": note_templates.MAX_BODY,
            "add_name": add["name"],
            "add_body": add["body"],
            "add_errors": dict(add_errors or {}),
            "editing": bool(edit_id),
            "edit_id": edit_id,
            "edit_name": edit["name"],
            "edit_body": edit["body"],
            "edit_errors": dict(edit_errors or {}),
        }
        return render(
            request,
            "settings/note_templates.html",
            page="Note templates",
            active="settings",
            section="note-templates",
            notice=notice,
            status_code=status_code,
            **context,
        )

    def typed_errors(name: str, body: str, error: ValueError | None = None) -> dict[str, str]:
        """The message by field: the name's and the text's own checks first, then a refusal from
        the core that names a field (a taken name), else the whole form."""
        errors: dict[str, str] = {}
        try:
            note_templates.clean_name(name)
        except ValueError as problem:
            errors["name"] = str(problem)
        try:
            note_templates.clean_body(body)
        except ValueError as problem:
            errors["body"] = str(problem)
        if error is not None and not errors:
            if str(error) == note_templates.NAME_TAKEN_MESSAGE:
                errors["name"] = str(error)
            else:
                errors["form"] = str(error)
        return errors

    async def posted_text(request: Request) -> dict[str, str]:
        form = await request.form()
        return {key: value for key, value in form.items() if isinstance(value, str)}

    @app.get("/settings/note-templates")
    def page(request: Request, saved: str = "") -> HTMLResponse:
        """The list and the add form. ?saved= is one code from NOTICES; any other value shows
        nothing."""
        return list_page(request, notice=NOTICES.get(saved, ""))

    @app.get("/settings/note-templates/{template_id}")
    def edit_page(request: Request, template_id: str) -> HTMLResponse:
        """The same list, with the edit form filled in for this template."""
        found = note_templates.find(template_id)
        if found is None:
            return not_found(request)
        return list_page(
            request,
            edit_id=found.id,
            edit_values={"name": found.name, "body": found.body},
        )

    @app.post("/settings/note-templates/add")
    async def add_template(request: Request) -> Response:
        """Add a template at the end of the list. A refused name or text re-shows the form (422)
        with what was typed and the message by field."""
        fields = await posted_text(request)
        name, body = fields.get("name", ""), fields.get("body", "")
        try:
            await run_in_threadpool(note_templates.add, name, body)
        except ValueError as error:
            return await run_in_threadpool(
                list_page,
                request,
                add_values={"name": name, "body": body},
                add_errors=typed_errors(name, body, error),
                status_code=422,
            )
        return RedirectResponse(f"{_PAGE}?saved=added", status_code=303)

    @app.post("/settings/note-templates/starters")
    def add_starters(request: Request) -> Response:
        """Add the starter templates the book is missing, as far as the 30 cap allows. When the
        book is full and none fit, the list says so (422)."""
        if not note_templates.missing_starters():
            return RedirectResponse(_PAGE, status_code=303)
        if note_templates.add_starters() == 0:
            return list_page(request, starters_error=note_templates.FULL_MESSAGE, status_code=422)
        return RedirectResponse(f"{_PAGE}?saved=starters", status_code=303)

    @app.post("/settings/note-templates/{template_id}/edit")
    async def edit_template(request: Request, template_id: str) -> Response:
        """Save a template's name and text. A refused one re-shows the edit form (422) with what
        was typed; an id that isn't in this book is a 404."""
        if await run_in_threadpool(note_templates.find, template_id) is None:
            return await run_in_threadpool(not_found, request)
        fields = await posted_text(request)
        name, body = fields.get("name", ""), fields.get("body", "")
        try:
            await run_in_threadpool(note_templates.update, template_id, name, body)
        except LookupError:
            return await run_in_threadpool(not_found, request)
        except ValueError as error:
            return await run_in_threadpool(
                list_page,
                request,
                edit_id=template_id,
                edit_values={"name": name, "body": body},
                edit_errors=typed_errors(name, body, error),
                status_code=422,
            )
        return RedirectResponse(f"{_PAGE}?saved=updated", status_code=303)

    @app.post("/settings/note-templates/{template_id}/up")
    def move_up(request: Request, template_id: str) -> Response:
        """Move a template one place up the list. At the top, nothing changes."""
        if note_templates.find(template_id) is None:
            return not_found(request)
        note_templates.move(template_id, up=True)
        return RedirectResponse(f"{_PAGE}#{template_id}", status_code=303)

    @app.post("/settings/note-templates/{template_id}/down")
    def move_down(request: Request, template_id: str) -> Response:
        """Move a template one place down the list. At the bottom, nothing changes."""
        if note_templates.find(template_id) is None:
            return not_found(request)
        note_templates.move(template_id, up=False)
        return RedirectResponse(f"{_PAGE}#{template_id}", status_code=303)

    @app.post("/settings/note-templates/{template_id}/delete")
    def delete_template(request: Request, template_id: str) -> Response:
        """Delete a template. Accounts' notes already written from it are not changed."""
        if not note_templates.is_template_id(template_id):
            return not_found(request)
        if not note_templates.delete(template_id):
            return not_found(request)
        return RedirectResponse(f"{_PAGE}?saved=deleted", status_code=303)
