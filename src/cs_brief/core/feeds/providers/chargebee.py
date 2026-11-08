"""Chargebee: each customer's most overdue unpaid invoice, as days overdue.

Reads every customer and every invoice still due or not paid. The API key is the basic-auth user
name with an empty password, and the pages are followed by the offset Chargebee sends.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from cs_brief.core import importing
from cs_brief.core.feeds import catalog
from cs_brief.core.feeds.http import FeedError, FeedHttp
from cs_brief.core.feeds.providers.base import MAX_PAGES, cell_text, email_domain

PAGE_SIZE = 100
UNPAID = '["payment_due","not_paid"]'
HEADERS = ["ID", "Account name", "Website", "Days invoice overdue"]
_SITE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def _today() -> date:
    return datetime.now(UTC).date()


class ChargebeeProvider:
    info = catalog.provider_info("chargebee")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        site = settings.get("site", "").strip()
        if _SITE.fullmatch(site) is None:
            raise FeedError(
                "Chargebee's site is the part before .chargebee.com, such as acme. Check it."
            )
        if not secret:
            raise FeedError("Type the API key first.")
        base = f"https://{site}.chargebee.com/api/v2"
        auth = (secret, "")
        customers = _all_pages(http, f"{base}/customers", auth, "customer", {})
        invoices = _all_pages(http, f"{base}/invoices", auth, "invoice", {"status[in]": UNPAID})
        today = _today()
        overdue: dict[str, int] = {}
        for invoice in invoices:
            customer_id = cell_text(invoice.get("customer_id")).strip()
            if customer_id:
                days = _days_overdue(invoice.get("due_date"), today)
                overdue[customer_id] = max(overdue.get(customer_id, 0), days)
        rows: list[list[str]] = []
        for customer in customers:
            customer_id = cell_text(customer.get("id")).strip()
            company = cell_text(customer.get("company")).strip()
            person = " ".join(
                part
                for part in (
                    cell_text(customer.get("first_name")).strip(),
                    cell_text(customer.get("last_name")).strip(),
                )
                if part
            )
            rows.append(
                [
                    customer_id,
                    company or person,
                    email_domain(cell_text(customer.get("email"))),
                    str(overdue.get(customer_id, 0)),
                ]
            )
        return importing.Table(source_name="Chargebee", headers=HEADERS, rows=rows)

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _all_pages(
    http: FeedHttp,
    url: str,
    auth: tuple[str, str],
    item_key: str,
    extra: dict[str, str],
) -> list[dict[str, Any]]:
    """The objects under item_key in every page, following next_offset while Chargebee sends one."""
    collected: list[dict[str, Any]] = []
    params = {**extra, "limit": str(PAGE_SIZE)}
    for _ in range(MAX_PAGES):
        data = http.get_json(url, auth=auth, params=params)
        entries = data.get("list") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise FeedError("Chargebee sent a list cs-brief can't read.")
        for entry in entries:
            item = entry.get(item_key) if isinstance(entry, dict) else None
            if isinstance(item, dict):
                collected.append(item)
        offset = data.get("next_offset")
        if not isinstance(offset, str) or not offset:
            return collected
        params["offset"] = offset
    raise FeedError(
        f"Chargebee has more than {MAX_PAGES} pages of {item_key}s, so cs-brief stopped reading."
    )


def _days_overdue(due_date: object, today: date) -> int:
    """Days past a unix due date. 0 when there is no due date or it is still ahead."""
    if due_date is None:
        return 0
    if isinstance(due_date, bool) or not isinstance(due_date, int | float):
        raise FeedError("Chargebee sent a due date cs-brief can't read.")
    try:
        due = datetime.fromtimestamp(due_date, UTC).date()
    except (OverflowError, OSError, ValueError):
        raise FeedError("Chargebee sent a due date cs-brief can't read.") from None
    return max(0, (today - due).days)
