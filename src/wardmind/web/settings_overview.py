"""Settings > Overview (a setup check and a way to find any setting) and Settings > Profile & region
(your name, currency and date style).

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
    from fastapi.responses import RedirectResponse

    @app.get("/settings")
    def settings_home() -> RedirectResponse:
        """Settings opens on Overview."""
        return RedirectResponse("/settings/overview", status_code=303)

    for path, section, page in (
        ("/settings/overview", "overview", "Settings"),
        ("/settings/profile", "profile", "Profile & region"),
    ):

        def placeholder(request: Request, _s: str = section, _p: str = page) -> HTMLResponse:
            return render(
                request, "settings/placeholder.html", page=_p, active="settings", section=_s
            )

        app.add_api_route(path, placeholder, methods=["GET"])
