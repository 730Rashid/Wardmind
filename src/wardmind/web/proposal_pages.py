"""Suggestions on Today: the cards, Accept, Edit and Dismiss (core/proposals.py). Workstream A.

CONTRACT STUB: registered by create_app (web/app.py) through register(). `render` is app._render
(the shared layout and context every page needs); `templates` is the app's Jinja2Templates, for
partials and for Jinja globals. Cross-site writes never reach these routes (same_origin_writes in
app.py), and the session check and lock gate run first.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

Render = Callable[..., HTMLResponse]


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this module's routes to the app."""
