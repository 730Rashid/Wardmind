"""PostHog: each company's active-user trend and days since anyone logged in, from a HogQL query.

The query runs through PostHog's read-only query API with a personal API key. With no query of
its own, the standard usage query is built around the company property, which is checked first:
it may hold only letters, numbers, underscores, dots and a leading $, so nothing else can get into
the query text. A custom query is sent exactly as written.
"""

from __future__ import annotations

import re

from cs_brief.core import importing
from cs_brief.core.feeds import catalog
from cs_brief.core.feeds.http import FeedError, FeedHttp
from cs_brief.core.feeds.providers.base import MAX_ROWS, cell_text

DEFAULT_HOST = "https://us.posthog.com"
_PROJECT_ID = re.compile(r"[0-9]+")
_GROUP_KEY = re.compile(r"\$?[A-Za-z0-9_.]{1,64}")
_NOT_A_RESULT = "PostHog didn't send a result cs-brief can read."


def _key_expression(group_key: str) -> str:
    """A $group_<n> column is named as it is; any other property is read from properties."""
    return group_key if group_key.startswith("$group_") else f"properties.{group_key}"


def _default_query(key: str) -> str:
    """The standard usage query. `key` has already passed _GROUP_KEY."""
    active = "uniqIf(distinct_id, timestamp > now() - INTERVAL 30 DAY)"
    before = "uniqIf(distinct_id, timestamp <= now() - INTERVAL 30 DAY)"
    return (
        f"SELECT {key} AS ID,\n"
        f"       round(100 * ({active} - {before}) / greatest({before}, 1)) AS `Usage trend`,\n"
        "       dateDiff('day', max(timestamp), now()) AS `Days since last login`\n"
        "FROM events\n"
        f"WHERE timestamp > now() - INTERVAL 60 DAY AND {key} IS NOT NULL AND {key} != ''\n"
        "GROUP BY ID\n"
        "LIMIT 50000"
    )


class PostHogProvider:
    info = catalog.provider_info("posthog")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        host = settings.get("host", "").strip().rstrip("/") or DEFAULT_HOST
        project_id = settings.get("project_id", "").strip()
        if _PROJECT_ID.fullmatch(project_id) is None:
            raise FeedError("The project ID is the number in your project's address. Check it.")
        group_key = settings.get("group_key", "").strip()
        if _GROUP_KEY.fullmatch(group_key) is None:
            raise FeedError(
                "The company property can use only letters, numbers, underscores and dots, "
                "with an optional $ at the start, such as $group_0 or company_id."
            )
        if not secret:
            raise FeedError("Type the personal API key first.")
        custom = settings.get("query", "")
        query = custom if custom.strip() else _default_query(_key_expression(group_key))
        response = http.post_query(
            f"{host}/api/projects/{project_id}/query/",
            json={"query": {"kind": "HogQLQuery", "query": query}},
            headers={"Authorization": f"Bearer {secret}"},
        )
        try:
            data = response.json()
        except ValueError:
            raise FeedError(_NOT_A_RESULT) from None
        return _table(data)

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _table(data: object) -> importing.Table:
    """The query's columns as headers and its rows as text, like records_to_table."""
    columns = data.get("columns") if isinstance(data, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(columns, list) or not isinstance(results, list):
        raise FeedError(_NOT_A_RESULT)
    if len(results) > MAX_ROWS:
        raise FeedError(f"This query returns more than {MAX_ROWS:,} rows. Narrow it down.")
    headers = [
        cell_text(name).strip() or f"Column {position}" for position, name in enumerate(columns, 1)
    ]
    rows: list[list[str]] = []
    for result in results:
        if not isinstance(result, list):
            raise FeedError(_NOT_A_RESULT)
        cells = [cell_text(value) for value in result[: len(headers)]]
        cells += [""] * (len(headers) - len(cells))
        if any(cells):
            rows.append(cells)
    return importing.Table(source_name="PostHog", headers=headers, rows=rows)
