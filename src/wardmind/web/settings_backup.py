"""Settings > Backup & export (core/backup.py: a password-protected backup of the open book,
restored as a new book; CSV exports of the accounts, and of the notes and follow-ups).

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.

Passwords are read from the form and never sent back: a refused form re-renders with its messages
only. A backup file is read into memory once, with a hard cap, and never written to disk here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from wardmind.core import audit, backup, books, service, vault
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.settings import DataSource, load_settings
from wardmind.web import formatting

Render = Callable[..., HTMLResponse]

_FORM_ALLOWANCE = 1024 * 1024  # what the multipart wrapper and the form's own fields add
_LOCKED_CODE = "locked"  # notice codes an export redirects back with
_EXPORT_FAILED_CODE = "export-failed"
_NOTICES = {
    "restored": "Restored as a new book. Open it from the book switcher.",
    "export-failed": (
        "Wardmind couldn't make that export. Check that your book loads on the Connections "
        "page, then try again."
    ),
    "locked": "Unlock Wardmind first, then try again.",
}
_DEMO_NO_BOOK = "Demo data is showing, so there's no book to back up. Open a book, then try again."
_MISMATCH = "The two passwords don't match. Type the same password in both boxes."
_NO_FILE = "Choose a backup file first."
_NO_PASSWORD = "Type the password this backup was made with."
_UNREADABLE_FORM = "That upload couldn't be read. Choose the backup file again."
_LOCKED = "Wardmind is locked. Unlock it first, then try again."
_MAKE_FAILED = (
    "Wardmind couldn't read this book's files, so no backup was made. Check that Wardmind can "
    "read its data folder, then try again."
)
_RESTORE_FAILED = (
    "Wardmind couldn't add the restored book, so nothing was kept. Check that Wardmind can write "
    "to its data folder, then try again."
)


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    def page(
        request: Request,
        *,
        notice: str = "",
        status_code: int = 200,
        make_errors: dict[str, str] | None = None,
        make_keys: bool = False,
        restore_errors: dict[str, str] | None = None,
        restore_name: str | None = None,
    ) -> HTMLResponse:
        demo = _demo()
        book = None if demo else books.active()
        return render(
            request,
            "settings/backup.html",
            page="Backup & export",
            active="settings",
            section="backup",
            notice=notice,
            status_code=status_code,
            demo=demo,
            book_label=book.label if book is not None else "",
            last_backup=_last_backup_text(book.id) if book is not None else "",
            make_errors=make_errors or {},
            make_keys=make_keys,
            restore_errors=restore_errors or {},
            restore_name=restore_name if restore_name is not None else books.suggested_label(),
        )

    @app.get("/settings/backup")
    def backup_page(request: Request, saved: str = "") -> HTMLResponse:
        """Backup & export. ?saved= is one fixed notice code from _NOTICES; anything else shows
        nothing."""
        return page(request, notice=_NOTICES.get(saved, ""))

    @app.post("/settings/backup/make")
    async def make_backup_file(request: Request) -> Response:
        """Seal the open book with the password and send the file as a download. A refused form
        re-renders the page (422) with its messages and nothing is made."""
        fields = await _fields(request)
        keys = fields.get("include_keys", "") == "1"
        errors = {"form": _DEMO_NO_BOOK} if _demo() else _make_password_errors(fields)
        if errors:
            return await run_in_threadpool(
                page, request, make_errors=errors, make_keys=keys, status_code=422
            )
        try:
            blob = await run_in_threadpool(
                backup.make_backup, fields.get("password", ""), include_keys=keys
            )
        except backup.BackupError as error:
            message = error.message
        except vault.Locked:
            message = _LOCKED
        except (vault.VaultError, OSError):
            message = _MAKE_FAILED
        else:
            return Response(
                blob,
                media_type="application/octet-stream",
                headers=_attachment(f"wardmind-backup-{date.today().isoformat()}.wmbackup"),
            )
        return await run_in_threadpool(
            page, request, make_errors={"form": message}, make_keys=keys, status_code=422
        )

    @app.post("/settings/backup/restore")
    async def restore_backup_file(request: Request) -> Response:
        """Restore a backup as a new book, which is listed but not opened. Nothing is kept when
        the backup is refused, and the typed name and every message come back on the form."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > backup.MAX_BACKUP_BYTES + _FORM_ALLOWANCE:
            return await run_in_threadpool(
                page, request, restore_errors={"file": backup.TOO_BIG_FILE}, status_code=422
            )
        try:
            form = await request.form()
        except StarletteHTTPException:
            return await run_in_threadpool(
                page, request, restore_errors={"form": _UNREADABLE_FORM}, status_code=422
            )
        try:
            upload = form.get("file")
            password = _text(form.get("password"))
            typed_name = _text(form.get("name"))
            blob = b""
            if isinstance(upload, UploadFile) and upload.filename:
                blob = await upload.read(backup.MAX_BACKUP_BYTES + 1)
        finally:
            await form.close()

        errors: dict[str, str] = {}
        if not blob:
            errors["file"] = _NO_FILE
        elif len(blob) > backup.MAX_BACKUP_BYTES:
            errors["file"] = backup.TOO_BIG_FILE
        if not password:
            errors["password"] = _NO_PASSWORD
        name_error = await run_in_threadpool(_name_error, typed_name)
        if name_error:
            errors["name"] = name_error
        if errors:
            return await run_in_threadpool(
                page, request, restore_errors=errors, restore_name=typed_name, status_code=422
            )
        try:
            await run_in_threadpool(backup.restore_backup, blob, password, typed_name)
        except backup.BackupError as error:
            message = error.message
        except vault.Locked:
            message = _LOCKED
        except (vault.VaultError, OSError):
            message = _RESTORE_FAILED
        else:
            return RedirectResponse("/settings/backup?saved=restored", status_code=303)
        return await run_in_threadpool(
            page,
            request,
            restore_errors={"form": message},
            restore_name=typed_name,
            status_code=422,
        )

    @app.get("/settings/export/accounts.csv")
    async def export_accounts() -> Response:
        """The accounts the book shows, one row each, as a CSV download."""
        try:
            text = await run_in_threadpool(_accounts_export)
        except vault.Locked:
            return _back(_LOCKED_CODE)
        except (ConnectorError, vault.VaultError, OSError):
            return _back(_EXPORT_FAILED_CODE)
        return _csv_download(text, f"wardmind-accounts-{date.today().isoformat()}.csv")

    @app.get("/settings/export/notes.csv")
    async def export_notes() -> Response:
        """Every note and follow-up of the accounts the book shows, one row each, as a CSV
        download."""
        try:
            text = await run_in_threadpool(_notes_export)
        except vault.Locked:
            return _back(_LOCKED_CODE)
        except (ConnectorError, vault.VaultError, OSError):
            return _back(_EXPORT_FAILED_CODE)
        return _csv_download(text, f"wardmind-notes-{date.today().isoformat()}.csv")


# --- Helpers ------------------------------------------------------------------------------------


async def _fields(request: Request) -> dict[str, str]:
    """The text fields of a posted form (a file field is left out)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _demo() -> bool:
    return load_settings().source is DataSource.DEMO


def _make_password_errors(fields: dict[str, str]) -> dict[str, str]:
    """The new backup password, checked by the vault's rules, then against its confirmation.
    The messages never contain the password."""
    password = fields.get("password", "")
    try:
        vault.check_password(password)
    except ValueError as error:
        return {"password": str(error)}
    if fields.get("confirm", "") != password:
        return {"confirm": _MISMATCH}
    return {}


def _name_error(name: str) -> str:
    """Why a book name can't be used, or "" when it can."""
    try:
        books.check_label(name)
    except books.BookError as error:
        return str(error)
    return ""


def _last_backup_text(book_id: str) -> str:
    last: datetime | None = backup.last_backup_at(book_id)
    if last is None:
        return "This book hasn't been backed up yet."
    return f"Last backed up {formatting.long_date(last.astimezone().date())}."


def _accounts_export() -> str:
    text = backup.accounts_csv(service.get_book())
    audit.record(audit.Event.EXPORTED, book=_export_book(), detail="Accounts CSV")
    return text


def _notes_export() -> str:
    text = backup.notes_csv(service.get_book(), service.get_journal().all())
    audit.record(audit.Event.EXPORTED, book=_export_book(), detail="Notes CSV")
    return text


def _export_book() -> str | None:
    """The book an export came from, for the activity log: none for demo data."""
    return None if _demo() else books.active_id()


def _attachment(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }


def _csv_download(text: str, filename: str) -> Response:
    """The CSV as a download. The byte order mark first, so Excel reads the text as UTF-8."""
    return Response(
        text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers=_attachment(filename),
    )


def _back(code: str) -> RedirectResponse:
    """Back to the Backup & export page with one fixed notice code."""
    return RedirectResponse(f"/settings/backup?{urlencode({'saved': code})}", status_code=303)
