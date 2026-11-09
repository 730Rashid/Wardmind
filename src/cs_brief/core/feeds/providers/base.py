"""What every feed provider implements, and helpers they share.

CONTRACT FILE: the Provider protocol and the helper signatures are fixed; implementers escalate
before changing them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from cs_brief.core.feeds.http import FeedError, FeedHttp
from cs_brief.core.feeds.models import ProviderInfo
from cs_brief.core.importing import (
    CELL_TOO_LONG_MESSAGE,
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    TOO_MANY_COLUMNS_MESSAGE,
    Table,
)

MAX_ROWS = 50_000  # a source with more rows is refused with a FeedError, not cut off
MAX_PAGES = 100  # pagination stops here with a FeedError, so a loop can't run forever
MAX_TOTAL_BYTES = 100_000_000  # a paginated source whose pages add up to more is refused

# Mail providers whose domain says nothing about the customer's company.
_FREE_MAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "gmx.de",
        "mail.com",
        "yandex.com",
        "yandex.ru",
        "zoho.com",
        "hey.com",
        "fastmail.com",
        "tutanota.com",
        "qq.com",
        "163.com",
    }
)
_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")


class Provider(Protocol):
    info: ProviderInfo

    def fetch(self, settings: dict[str, str], secret: str | None, http: FeedHttp) -> Table:
        """Read the source and return it as a Table (headers + rows of text). Raise
        http.FeedError with a plain message for anything that goes wrong (bad settings, nothing
        found, wrong format). Never write to the source."""
        ...

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        """For ON_CHANGE schedules: the source's modification time, or None if it can't be
        known (then the feed syncs on its fallback interval). Providers that aren't files
        return None."""
        ...


def records_to_table(
    records: list[dict[str, Any]], source_name: str, *, columns: list[str] | None = None
) -> Table:
    """A Table from JSON-like records. Nested objects flatten to "parent.child" headers; lists
    become their items joined with ", " (objects in lists are skipped); None becomes "";
    numbers keep their JSON text (no "1.0" for 1); booleans become "true"/"false". Headers
    follow `columns` when given (missing keys give ""), else first-seen order across all
    records. Raises FeedError when there are more than MAX_ROWS records, more than MAX_COLUMNS
    columns, or a cell longer than MAX_CELL_CHARS."""
    if len(records) > MAX_ROWS:
        raise FeedError(f"This source has more than {MAX_ROWS:,} rows. Export fewer rows.")
    flat = [_flatten(record) for record in records if isinstance(record, dict)]
    if columns is None:
        found: dict[str, None] = {}  # first-seen order, no repeats
        for row in flat:
            found.update(dict.fromkeys(row))
            if len(found) > MAX_COLUMNS:
                raise FeedError(TOO_MANY_COLUMNS_MESSAGE)
        names = list(found)
    else:
        names = list(columns)
        if len(names) > MAX_COLUMNS:
            raise FeedError(TOO_MANY_COLUMNS_MESSAGE)
    headers = [name.strip() or f"Column {position}" for position, name in enumerate(names, 1)]
    rows: list[list[str]] = []
    for row in flat:
        cells = [row.get(name, "") for name in names]
        if not any(cells):  # a fully blank record is no row
            continue
        if any(len(cell) > MAX_CELL_CHARS for cell in cells):
            raise FeedError(CELL_TOO_LONG_MESSAGE)
        rows.append(cells)
    return Table(source_name=source_name, headers=headers, rows=rows)


def _flatten(record: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """One record as text cells: nested objects become "parent.child" keys. The nesting is walked
    with an explicit stack, not recursion, so a deeply nested record can't hit Python's recursion
    limit. Keys come out in the same order a recursive walk would give."""
    cells: dict[str, str] = {}
    stack = [(iter(record.items()), prefix)]
    while stack:
        items, path = stack[-1]
        for key, value in items:
            name = f"{path}{key}"
            if isinstance(value, dict):
                stack.append((iter(value.items()), f"{name}."))
                break
            cells[name] = cell_text(value)
        else:
            stack.pop()
    return cells


def cell_text(value: Any) -> str:
    """One JSON-like value as cell text (see records_to_table)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    if isinstance(value, list):
        items = [cell_text(item) for item in value if not isinstance(item, dict | list)]
        return ", ".join(item for item in items if item)
    return str(value)


def json_path(data: Any, path: str) -> Any:
    """The value at a dotted path such as "data.items" or "results.0.rows" ("" is the whole
    document). Raises FeedError("Nothing at <path> in the response.") when a step is missing."""
    path = path.strip()
    current = data
    if not path:
        return current
    for step in path.split("."):
        if isinstance(current, dict) and step in current:
            current = current[step]
        elif isinstance(current, list) and step.isdigit() and int(step) < len(current):
            current = current[int(step)]
        else:
            raise FeedError(f"Nothing at {path} in the response.")
    return current


def email_domain(email: str | None) -> str:
    """ "jo@acme.com" gives "acme.com"; free mail domains (gmail.com, outlook.com, hotmail.com,
    yahoo.com, icloud.com, proton.me, ...) give "" because they don't identify a company."""
    if not email or "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].strip().strip(".").casefold()
    if _DOMAIN.fullmatch(domain) is None or domain in _FREE_MAIL:
        return ""
    return domain
