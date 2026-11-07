"""What every feed provider implements, and helpers they share.

CONTRACT FILE: the Provider protocol and the helper signatures are fixed; implementers escalate
before changing them.
"""

from __future__ import annotations

from typing import Any, Protocol

from cs_brief.core.feeds.http import FeedHttp
from cs_brief.core.feeds.models import ProviderInfo
from cs_brief.core.importing import Table

MAX_ROWS = 50_000  # a source with more rows is refused with a FeedError, not cut off
MAX_PAGES = 100  # pagination stops here with a FeedError, so a loop can't run forever


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
    records. Raises FeedError when there are more than MAX_ROWS records."""
    raise NotImplementedError


def json_path(data: Any, path: str) -> Any:
    """The value at a dotted path such as "data.items" or "results.0.rows" ("" is the whole
    document). Raises FeedError("Nothing at <path> in the response.") when a step is missing."""
    raise NotImplementedError


def email_domain(email: str | None) -> str:
    """ "jo@acme.com" gives "acme.com"; free mail domains (gmail.com, outlook.com, hotmail.com,
    yahoo.com, icloud.com, proton.me, ...) give "" because they don't identify a company."""
    raise NotImplementedError
