"""Files on this computer: a CSV, TSV, text or Excel file (`file`), and a SQLite database opened
read-only (`sqlite`).

Nothing is written to either. The SQLite query runs through an authorizer that allows reads only.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from cs_brief.core import importing
from cs_brief.core.feeds import catalog
from cs_brief.core.feeds.http import MAX_BYTES, FeedError, FeedHttp
from cs_brief.core.feeds.providers.base import MAX_ROWS, cell_text
from cs_brief.core.importing import Table

_FILE_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx")
_NOT_SUFFIX = "cs-brief can't read {kind}. Save it as .xlsx, CSV or TSV and try again."
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


def _existing_file(raw: str, *, missing: str) -> Path:
    """The path as a Path when it names an existing file, else FeedError."""
    text = raw.strip()
    if not text:
        raise FeedError("Choose the file first.")
    path = Path(text)
    if path.is_dir():
        raise FeedError("That path is a folder. Pick the file itself.")
    if not path.is_file():
        raise FeedError(missing)
    return path


def _mtime(raw: str) -> float | None:
    """The file's modification time, or None when there is no such file."""
    text = raw.strip()
    if not text:
        return None
    try:
        return os.stat(text).st_mtime
    except OSError:
        return None


class FileProvider:
    """A CSV, TSV, text or Excel file on this computer. Settings: path, sheet (Excel only)."""

    info = catalog.provider_info("file")

    def fetch(self, settings: dict[str, str], secret: str | None, http: FeedHttp) -> Table:
        path = _existing_file(
            settings.get("path", ""),
            missing="There's no file at that path. Check the path and that the file is there.",
        )
        suffix = path.suffix.lower()
        if suffix not in _FILE_SUFFIXES:
            kind = f"{suffix} files" if suffix else "files with no extension"
            raise FeedError(_NOT_SUFFIX.format(kind=kind))
        if path.stat().st_size > MAX_BYTES:
            raise FeedError("That file is larger than 25 MB. Export fewer rows.")
        try:
            data = path.read_bytes()
        except OSError:
            raise FeedError(
                "cs-brief couldn't read that file. Check it isn't open elsewhere."
            ) from None
        sheet = settings.get("sheet", "").strip() or None
        try:
            return importing.read_upload(data, path.name, sheet)
        except importing.ImportError_ as exc:
            raise FeedError(str(exc)) from None

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return _mtime(settings.get("path", ""))


class SqliteProvider:
    """A SQLite database file, read-only, running one SELECT. Settings: path, query."""

    info = catalog.provider_info("sqlite")

    def fetch(self, settings: dict[str, str], secret: str | None, http: FeedHttp) -> Table:
        path = _existing_file(
            settings.get("path", ""),
            missing="There's no database file at that path. Check the path and that it is there.",
        )
        query = settings.get("query", "").strip()
        if not query:
            raise FeedError("Write the query first.")
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            raise FeedError("cs-brief couldn't open that file as a SQLite database.") from None
        try:
            connection.set_authorizer(_authorize)
            cursor = connection.execute(query)
            if cursor.description is None:
                raise FeedError("The query has to be a SELECT that returns columns.")
            names = [column[0] for column in cursor.description]
            fetched = cursor.fetchmany(MAX_ROWS + 1)
        except FeedError:
            raise
        except sqlite3.ProgrammingError:
            raise FeedError("Write one SELECT query, not several statements.") from None
        except sqlite3.Error as exc:
            raise FeedError(_query_problem(exc)) from None
        finally:
            connection.close()
        if len(fetched) > MAX_ROWS:
            raise FeedError(f"The query returns more than {MAX_ROWS:,} rows. Narrow it down.")
        return _table(names, fetched, source_name=path.name)

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return _mtime(settings.get("path", ""))


def _authorize(action: int, arg1: Any, arg2: Any, database: Any, trigger: Any) -> int:
    """Allow reading a table, a column and a function, and nothing else (no writes, no ATTACH,
    no PRAGMA, no CREATE)."""
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _query_problem(exc: sqlite3.Error) -> str:
    if "not authorized" in str(exc).casefold():
        return "Only read-only SELECT queries can run on this file."
    return f"The query didn't run: {exc}"


def _table(names: list[str], fetched: list[tuple[Any, ...]], *, source_name: str) -> Table:
    """The result as text cells. Column names are made unique, blank ones numbered, and fully
    blank rows dropped, like records_to_table."""
    headers: list[str] = []
    for position, name in enumerate(names, 1):
        base = name.strip() or f"Column {position}"
        header, copy = base, 2
        while header in headers:
            header = f"{base} ({copy})"
            copy += 1
        headers.append(header)
    rows: list[list[str]] = []
    for values in fetched:
        cells = [
            cell_text(v.decode("utf-8", "replace") if isinstance(v, bytes) else v) for v in values
        ]
        if any(cells):
            rows.append(cells)
    return Table(source_name=source_name, headers=headers, rows=rows)
