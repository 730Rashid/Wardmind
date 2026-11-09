"""Zendesk: each organisation's open urgent tickets, as the severity-1 count.

Reads every organisation and every urgent ticket that is not solved, with an API token: the sign-in
is your email with "/token" as the user name and the token as the password. A next-page link is
followed only while it stays on the site the first request went to, so the key never goes elsewhere.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from wardmind.core import importing
from wardmind.core.feeds import catalog
from wardmind.core.feeds.http import FeedError, FeedHttp, check_url, same_origin
from wardmind.core.feeds.providers.base import MAX_PAGES, cell_text

PAGE_SIZE = 100
URGENT_OPEN = "type:ticket status<solved priority:urgent"
HEADERS = ["ID", "Account name", "Website", "Open severity-1 tickets"]
_SUBDOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_NOT_SAME_SITE = "The next page is on a different site, so Wardmind won't send your key there."


class ZendeskProvider:
    info = catalog.provider_info("zendesk")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        subdomain = settings.get("subdomain", "").strip()
        if _SUBDOMAIN.fullmatch(subdomain) is None:
            raise FeedError(
                "Zendesk's subdomain is the part before .zendesk.com, such as acme. Check it."
            )
        email = settings.get("email", "").strip()
        if "@" not in email:
            raise FeedError("Type the email you sign in to Zendesk with.")
        if not secret:
            raise FeedError("Type the API token first.")
        base = f"https://{subdomain}.zendesk.com/api/v2"
        auth = (f"{email}/token", secret)
        organizations = _all_pages(
            http, f"{base}/organizations.json", {"per_page": str(PAGE_SIZE)}, auth, "organizations"
        )
        tickets = _all_pages(
            http,
            f"{base}/search.json",
            {"query": URGENT_OPEN, "per_page": str(PAGE_SIZE)},
            auth,
            "results",
        )
        counts: dict[str, int] = {}
        for ticket in tickets:
            organization = cell_text(ticket.get("organization_id")).strip()
            if organization:
                counts[organization] = counts.get(organization, 0) + 1
        rows: list[list[str]] = []
        for organization in organizations:
            organization_id = cell_text(organization.get("id")).strip()
            external_id = cell_text(organization.get("external_id")).strip()
            domains = organization.get("domain_names")
            website = cell_text(domains[0]).strip() if isinstance(domains, list) and domains else ""
            rows.append(
                [
                    external_id or organization_id,
                    cell_text(organization.get("name")).strip(),
                    website,
                    str(counts.get(organization_id, 0)),
                ]
            )
        return importing.Table(source_name="Zendesk", headers=HEADERS, rows=rows)

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _all_pages(
    http: FeedHttp,
    url: str,
    params: dict[str, str],
    auth: tuple[str, str],
    key: str,
) -> list[dict[str, Any]]:
    """The objects under `key` on every page. next_page is followed only when it is on the same
    site as the first request, and the check happens before the request is sent."""
    first = url
    collected: list[dict[str, Any]] = []
    current = url
    query: dict[str, str] | None = params
    for _ in range(MAX_PAGES):
        data = http.get_json(current, auth=auth, params=query)
        items = data.get(key) if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise FeedError("Zendesk sent a list Wardmind can't read.")
        collected.extend(item for item in items if isinstance(item, dict))
        following = data.get("next_page")
        if not isinstance(following, str) or not following.strip():
            return collected
        current = check_url(urljoin(current, following.strip()))
        if not same_origin(first, current):
            raise FeedError(_NOT_SAME_SITE)
        query = None  # the next-page link carries its own query
    raise FeedError(
        f"Zendesk has more than {MAX_PAGES} pages of {key}, so Wardmind stopped reading."
    )
