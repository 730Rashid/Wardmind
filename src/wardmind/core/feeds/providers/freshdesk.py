"""Freshdesk: each company's open urgent tickets, as the severity-1 count.

Reads every company, then the urgent tickets that are open or pending. The API key is the basic-auth
user name, with "X" as the password, as Freshdesk expects.
"""

from __future__ import annotations

import re
from typing import Any

from wardmind.core import importing
from wardmind.core.feeds import catalog
from wardmind.core.feeds.http import FeedError, FeedHttp
from wardmind.core.feeds.providers.base import MAX_PAGES, cell_text

PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 30
SEARCH_PAGES = 10  # Freshdesk's search stops after 10 pages, so 300 tickets is the most it gives
URGENT_OPEN = '"priority:4 AND (status:2 OR status:3)"'
HEADERS = ["ID", "Account name", "Website", "Open severity-1 tickets"]
_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class FreshdeskProvider:
    info = catalog.provider_info("freshdesk")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        domain = settings.get("domain", "").strip()
        if _DOMAIN.fullmatch(domain) is None:
            raise FeedError(
                "Freshdesk's helpdesk name is the part before .freshdesk.com, such as acme. "
                "Check it."
            )
        if not secret:
            raise FeedError("Type the API key first.")
        base = f"https://{domain}.freshdesk.com/api/v2"
        auth = (secret, "X")
        companies = _companies(http, base, auth)
        counts = _urgent_counts(http, base, auth)
        rows: list[list[str]] = []
        for company in companies:
            company_id = cell_text(company.get("id")).strip()
            domains = company.get("domains")
            website = cell_text(domains[0]).strip() if isinstance(domains, list) and domains else ""
            rows.append(
                [
                    company_id,
                    cell_text(company.get("name")).strip(),
                    website,
                    str(counts.get(company_id, 0)),
                ]
            )
        return importing.Table(source_name="Freshdesk", headers=HEADERS, rows=rows)

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _companies(http: FeedHttp, base: str, auth: tuple[str, str]) -> list[dict[str, Any]]:
    """Every company, page by page, until a page comes back with fewer than PAGE_SIZE."""
    collected: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        data = http.get_json(
            f"{base}/companies",
            auth=auth,
            params={"per_page": str(PAGE_SIZE), "page": str(page)},
        )
        if not isinstance(data, list):
            raise FeedError("Freshdesk sent a list Wardmind can't read.")
        collected.extend(item for item in data if isinstance(item, dict))
        if len(data) < PAGE_SIZE:
            return collected
    raise FeedError(
        f"Freshdesk has more than {MAX_PAGES * PAGE_SIZE:,} companies, so Wardmind stopped reading."
    )


def _urgent_counts(http: FeedHttp, base: str, auth: tuple[str, str]) -> dict[str, int]:
    """Open urgent tickets per company id, from the search's pages (at most SEARCH_PAGES)."""
    counts: dict[str, int] = {}
    for page in range(1, SEARCH_PAGES + 1):
        data = http.get_json(
            f"{base}/search/tickets",
            auth=auth,
            params={"query": URGENT_OPEN, "page": str(page)},
        )
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise FeedError("Freshdesk sent tickets Wardmind can't read.")
        for ticket in results:
            if not isinstance(ticket, dict):
                continue
            company = cell_text(ticket.get("company_id")).strip()
            if company:
                counts[company] = counts.get(company, 0) + 1
        if len(results) < SEARCH_PAGE_SIZE:
            break
    return counts
