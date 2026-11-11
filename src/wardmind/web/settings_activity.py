"""Settings > Activity log: the entries of core/audit.py, newest first, with a filter, a CSV
download and a way to clear it.

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.

The log holds no customer data (core/audit.py), so the page and the CSV show what it recorded:
the event's words, the book's current label (or "Deleted book" when it is gone) and the detail.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from wardmind.core import audit, books
from wardmind.web.journal_pages import short_date

Render = Callable[..., HTMLResponse]

SHOWN = 200  # the page lists this many rows, newest first
CSV_FILE_NAME = "wardmind-activity.csv"
CSV_HEADINGS = ("When", "What", "Book", "Detail")
DELETED_BOOK = "Deleted book"
_CONFIRM_CLEAR = "Tick the box to confirm you want to clear the log."
_CSV_RISKY_START = ("=", "+", "-", "@", "\t", "\r")  # a spreadsheet would read these as a formula
_CHOICES: tuple[tuple[str, str, audit.Group | None], ...] = (
    ("all", "All", None),
    ("security", "Security", audit.Group.SECURITY),
    ("data", "Your data", audit.Group.DATA),
    ("settings", "Settings", audit.Group.SETTINGS),
)
# The notice codes the page accepts in ?saved=. Each maps to fixed words.
NOTICES = {"cleared": "Activity log cleared."}


def _chosen(text: str) -> tuple[str, audit.Group | None]:
    """The filter key and its group. Anything unknown means All."""
    for key, _, group in _CHOICES:
        if key == text:
            return key, group
    return "all", None


def _when(moment: datetime) -> str:
    """The entry's time in this computer's local time: '11 Nov 2026, 14:05'."""
    local = moment.astimezone()
    return f"{short_date(local.date())}, {local:%H:%M}"


def _csv_when(moment: datetime) -> str:
    """The entry's time in local time, to the minute, as ISO 8601 with its offset."""
    return moment.astimezone().isoformat(timespec="minutes")


def _safe_cell(text: str) -> str:
    """A CSV cell that a spreadsheet won't run as a formula: text that starts with = + - @, a tab
    or a carriage return gets a ' in front."""
    return f"'{text}" if text.startswith(_CSV_RISKY_START) else text


def _book_labels() -> dict[str, str]:
    return {book.id: book.label for book in books.load().books}


def _book_text(book_id: str | None, labels: dict[str, str]) -> str:
    """The book's current label when it still exists, "Deleted book" when it doesn't, and "" for
    an entry that isn't about one book."""
    if book_id is None:
        return ""
    return labels.get(book_id, DELETED_BOOK)


def _entries(group: audit.Group | None) -> list[audit.Entry]:
    """Every entry the log holds (newest first), limited to one group when given."""
    found = audit.entries(audit.KEEP)
    if group is None:
        return found
    return [entry for entry in found if entry.event.group is group]


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    def page(
        request: Request,
        *,
        choice: str = "all",
        notice: str = "",
        clear_error: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        key, group = _chosen(choice)
        found = _entries(group)
        labels = _book_labels()
        rows = [
            {
                "when": _when(entry.at),
                "what": entry.event.label,
                "book": _book_text(entry.book, labels),
                "detail": entry.detail,
            }
            for entry in found[:SHOWN]
        ]
        return render(
            request,
            "settings/activity.html",
            page="Activity log",
            active="settings",
            section="activity",
            notice=notice,
            status_code=status_code,
            rows=rows,
            total=len(found),
            more=max(len(found) - SHOWN, 0),
            shown=SHOWN,
            choices=[
                {"key": option, "label": label, "current": option == key}
                for option, label, _ in _CHOICES
            ],
            choice=key,
            csv_link=(
                f"/settings/activity.csv?group={key}" if key != "all" else "/settings/activity.csv"
            ),
            clear_error=clear_error,
            log_size=f"{audit.KEEP:,}",
        )

    @app.get("/settings/activity")
    def activity_page(request: Request, group: str = "", saved: str = "") -> HTMLResponse:
        """The log, newest first. ?group= picks a filter (all, security, data, settings); ?saved=
        is one code from NOTICES."""
        return page(request, choice=group, notice=NOTICES.get(saved, ""))

    @app.get("/settings/activity.csv")
    def activity_csv(group: str = "") -> Response:
        """Every entry of the chosen filter as a CSV file: the same four columns as the page, with
        the times in local time. Cells a spreadsheet could read as formulas are made plain text."""
        _, chosen_group = _chosen(group)
        labels = _book_labels()
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(CSV_HEADINGS)
        for entry in _entries(chosen_group):
            writer.writerow(
                [
                    _safe_cell(_csv_when(entry.at)),
                    _safe_cell(entry.event.label),
                    _safe_cell(_book_text(entry.book, labels)),
                    _safe_cell(entry.detail),
                ]
            )
        return Response(
            content=buffer.getvalue().encode("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{CSV_FILE_NAME}"'},
        )

    @app.post("/settings/activity/clear")
    async def clear_log(request: Request) -> Response:
        """Empty the log, then record that it was cleared (the first entry of the new log). The
        confirm box must be ticked; without it the page is shown again (422) and nothing changes."""
        form = await request.form()
        if form.get("confirm") != "1":
            return await run_in_threadpool(
                page, request, clear_error=_CONFIRM_CLEAR, status_code=422
            )
        await run_in_threadpool(audit.clear)
        return RedirectResponse("/settings/activity?saved=cleared", status_code=303)
