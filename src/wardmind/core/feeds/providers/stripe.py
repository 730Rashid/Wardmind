"""Stripe: each customer's most overdue open invoice, as days overdue.

Reads every customer and every open invoice with a restricted key, sent as a Bearer token, one
page of 100 at a time. One row per customer, so a customer who has paid up reads 0 and clears an
old reading.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from wardmind.core import importing
from wardmind.core.feeds import catalog
from wardmind.core.feeds.http import FeedError, FeedHttp
from wardmind.core.feeds.providers.base import MAX_PAGES, cell_text, email_domain

API = "https://api.stripe.com/v1"
PAGE_SIZE = 100
HEADERS = ["ID", "Account name", "Website", "Days invoice overdue"]


def _today() -> date:
    return datetime.now(UTC).date()


class StripeProvider:
    info = catalog.provider_info("stripe")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        if not secret:
            raise FeedError("Type the restricted key first.")
        headers = {"Authorization": f"Bearer {secret}"}
        customers = _all_pages(http, "customers", headers, {})
        invoices = _all_pages(http, "invoices", headers, {"status": "open"})
        today = _today()
        overdue: dict[str, int] = {}
        for invoice in invoices:
            customer = cell_text(invoice.get("customer")).strip()
            if customer:
                days = _days_overdue(invoice.get("due_date"), today)
                overdue[customer] = max(overdue.get(customer, 0), days)
        rows: list[list[str]] = []
        for customer in customers:
            customer_id = cell_text(customer.get("id")).strip()
            metadata = customer.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            website = _first_text(metadata.get("website")) or email_domain(
                cell_text(customer.get("email"))
            )
            rows.append(
                [
                    _first_text(
                        metadata.get("account_id"), metadata.get("wardmind_id"), customer_id
                    ),
                    cell_text(customer.get("name")).strip(),
                    website,
                    str(overdue.get(customer_id, 0)),
                ]
            )
        return importing.Table(source_name="Stripe", headers=HEADERS, rows=rows)

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _first_text(*values: object) -> str:
    """The first value that is text once trimmed, or "" when none is."""
    for value in values:
        text = cell_text(value).strip()
        if text:
            return text
    return ""


def _all_pages(
    http: FeedHttp, path: str, headers: dict[str, str], extra: dict[str, str]
) -> list[dict[str, Any]]:
    """Every object of a Stripe list, asking for the next page while Stripe says there is one."""
    collected: list[dict[str, Any]] = []
    params = {**extra, "limit": str(PAGE_SIZE)}
    for _ in range(MAX_PAGES):
        data = http.get_json(f"{API}/{path}", headers=headers, params=params)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise FeedError("Stripe sent a list Wardmind can't read.")
        page = [item for item in items if isinstance(item, dict)]
        collected.extend(page)
        if data.get("has_more") is not True:
            return collected
        last = page[-1].get("id") if page else None
        if not isinstance(last, str) or not last:
            raise FeedError("Stripe said there are more to read but didn't say where they start.")
        params["starting_after"] = last
    raise FeedError(
        f"Stripe has more than {MAX_PAGES} pages of {path}, so Wardmind stopped reading."
    )


def _days_overdue(due_date: object, today: date) -> int:
    """Days past a unix due date. 0 when there is no due date or it is still ahead."""
    if due_date is None:
        return 0
    if isinstance(due_date, bool) or not isinstance(due_date, int | float):
        raise FeedError("Stripe sent a due date Wardmind can't read.")
    try:
        due = datetime.fromtimestamp(due_date, UTC).date()
    except (OverflowError, OSError, ValueError):
        raise FeedError("Stripe sent a due date Wardmind can't read.") from None
    return max(0, (today - due).days)
