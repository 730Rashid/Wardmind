"""Settings > Backup & export (core/backup.py: a password-protected backup of the open book,
restored as a new book; CSV exports).

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

Render = Callable[..., HTMLResponse]


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    @app.get("/settings/backup")
    def placeholder(request: Request) -> HTMLResponse:
        return render(
            request,
            "settings/placeholder.html",
            page="Backup & export",
            active="settings",
            section="backup",
        )
