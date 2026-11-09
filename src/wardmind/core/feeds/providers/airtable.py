"""Airtable: the rows of one table or view in a base, read with a personal access token.

The table name is URL-encoded into the path, and pages are followed by the offset Airtable sends.
The columns are the CSM's own, so they are matched on the import's matching page, like a file.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from wardmind.core import importing
from wardmind.core.feeds import catalog
from wardmind.core.feeds.http import FeedError, FeedHttp
from wardmind.core.feeds.providers.base import MAX_PAGES, records_to_table

API = "https://api.airtable.com/v0"
PAGE_SIZE = 100
_BASE_ID = re.compile(r"app[A-Za-z0-9]{14}")


class AirtableProvider:
    info = catalog.provider_info("airtable")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        base_id = settings.get("base_id", "").strip()
        if _BASE_ID.fullmatch(base_id) is None:
            raise FeedError(
                "The base ID starts with app and has 17 letters and numbers. Copy it from the "
                "base's API page."
            )
        table = settings.get("table", "").strip()
        if not table:
            raise FeedError("Type the table name or ID first.")
        if not secret:
            raise FeedError("Type the personal access token first.")
        params = {"pageSize": str(PAGE_SIZE)}
        view = settings.get("view", "").strip()
        if view:
            params["view"] = view
        encoded_table = quote(table, safe="")
        url = f"{API}/{base_id}/{encoded_table}"
        fields = _all_fields(http, url, {"Authorization": f"Bearer {secret}"}, params)
        return records_to_table(fields, "Airtable")

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _all_fields(
    http: FeedHttp, url: str, headers: dict[str, str], params: dict[str, str]
) -> list[dict[str, Any]]:
    """The "fields" of every record on every page, while Airtable sends an offset."""
    collected: list[dict[str, Any]] = []
    query = dict(params)
    for _ in range(MAX_PAGES):
        data = http.get_json(url, headers=headers, params=query)
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise FeedError("Airtable sent records Wardmind can't read.")
        for record in records:
            fields = record.get("fields") if isinstance(record, dict) else None
            collected.append(fields if isinstance(fields, dict) else {})
        offset = data.get("offset")
        if not isinstance(offset, str) or not offset:
            return collected
        query["offset"] = offset
    raise FeedError(
        f"Airtable has more than {MAX_PAGES} pages of rows, so Wardmind stopped reading."
    )
