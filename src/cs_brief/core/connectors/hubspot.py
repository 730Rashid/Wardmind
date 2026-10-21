"""Read-only HubSpot connector: customer companies, their owners and open high-priority tickets.

cs-brief never writes to HubSpot. Every request is a GET or a read-only POST (search, batch read).

Data model mapping (all property names are configurable in config/hubspot.yaml):
- Accounts are companies whose `customer_property` equals `customer_value` (lifecycle stage
  "customer" by default), found with the CRM search API.
- Signal values come from company properties, read with `propertiesWithHistory`, so each change
  in HubSpot becomes one Signal with its own timestamp (that's the account page's history).
- OPEN_TICKETS_SEV1 is counted from tickets: priority in `sev1_priorities` and no value in
  `ticket_closed_property`, associated with the company.
- ONBOARDING_OVERDUE_MILESTONES comes from the `milestones_overdue` property's history.

Safety rules (enforced by HubSpotClient):
- The token goes only in the Authorization header, to API_BASE or a localhost test server.
  It never appears in logs, exceptions, reprs or user-facing messages.
- base_url other than API_BASE is accepted only for http(s)://127.0.0.1 or localhost (tests and
  the local fake server), so a token can't be sent anywhere else by configuration.

CONTRACT FILE: models, constants, signatures and docstrings are fixed; implementers escalate
before changing them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from cs_brief.core.connectors.errors import ConnectorError
from cs_brief.core.models import Account, Signal, SignalKind

API_BASE = "https://api.hubapi.com"

# Dated API paths first; the legacy v3 path is tried once if the dated one returns 404, and the
# working one is remembered per key for the client's lifetime. Keep every path here, in one place.
PATHS: dict[str, tuple[str, str]] = {
    "companies_search": (
        "/crm/objects/2026-09/companies/search",
        "/crm/v3/objects/companies/search",
    ),
    "companies_batch_read": (
        "/crm/objects/2026-09/companies/batch/read",
        "/crm/v3/objects/companies/batch/read",
    ),
    "owners": ("/crm/owners/2026-09/", "/crm/v3/owners/"),
    "tickets_search": ("/crm/objects/2026-09/tickets/search", "/crm/v3/objects/tickets/search"),
    "ticket_company_associations": (
        "/crm/associations/2026-09/tickets/companies/batch/read",
        "/crm/v4/associations/tickets/companies/batch/read",
    ),
}

REQUIRED_SCOPES = ("crm.objects.companies.read", "crm.objects.owners.read", "tickets")


class HubSpotError(ConnectorError):
    """Base for HubSpot failures. `message` is plain English and never contains the token."""


class HubSpotAuthError(HubSpotError):
    """401: the token was rejected (wrong, revoked or expired)."""


class HubSpotScopeError(HubSpotError):
    """403: the token works but lacks a scope. `missing` lists scope names when HubSpot says."""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


class HubSpotRateLimited(HubSpotError):
    """429 still returned after the retries."""


class HubSpotUnavailable(HubSpotError):
    """Network failure, timeout, or 5xx after the retries."""


class HubSpotMapping(BaseModel):
    """Which HubSpot properties feed which cs-brief fields. Defaults follow docs/hubspot.md."""

    customer_property: str = "lifecyclestage"
    customer_value: str = "customer"
    name: str = "name"
    owner: str = "hubspot_owner_id"
    arr: str = "cs_arr"
    tier: str = "cs_tier"
    start_date: str = "hs_lifecyclestage_customer_date"  # HubSpot's "Became a Customer Date"
    renewal_date: str = "cs_renewal_date"
    milestones_total: str = "cs_onboarding_milestones_total"
    milestones_done: str = "cs_onboarding_milestones_done"
    milestones_overdue: str = "cs_onboarding_milestones_overdue"
    signals: dict[SignalKind, str] = Field(
        default_factory=lambda: {
            SignalKind.USAGE_TREND_30D: "cs_usage_trend_30d",
            SignalKind.DAYS_SINCE_LAST_LOGIN: "cs_days_since_last_login",
            SignalKind.NPS_LATEST: "cs_nps_latest",
            SignalKind.CHAMPION_LEFT: "cs_champion_left",
            SignalKind.SEAT_UTILISATION: "cs_seat_utilisation",
            SignalKind.BILLING_OVERDUE_DAYS: "cs_billing_overdue_days",
        }
    )
    sev1_priorities: list[str] = Field(default_factory=lambda: ["HIGH", "URGENT"])
    ticket_priority_property: str = "hs_ticket_priority"
    ticket_closed_property: str = "closed_date"
    history_days: int = Field(default=90, ge=0)  # signal readings older than this are dropped


class CheckResult(BaseModel):
    name: str  # what was checked, e.g. "Token accepted", "Companies", "Owners", "Tickets"
    ok: bool
    message: str  # plain English, e.g. "128 customer companies found." or what to fix


class ConnectionCheck(BaseModel):
    ok: bool  # every check passed
    checks: list[CheckResult]
    missing_properties: list[str]  # mapped company properties HubSpot doesn't have


def load_mapping(path: Path | None = None) -> HubSpotMapping:
    """Read config/hubspot.yaml (via config_dir()); a missing file gives the defaults."""
    raise NotImplementedError


class HubSpotClient:
    """Thin HTTP client: auth header, JSON, paging, retries and the dated/legacy path fallback."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Raise ValueError if base_url is neither API_BASE nor a 127.0.0.1/localhost URL."""
        raise NotImplementedError

    def request(
        self,
        method: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call PATHS[key] (dated, then legacy on 404). Retries 429 (Retry-After, else
        exponential backoff from 1s) and 5xx/network errors up to max_retries, then raises the
        matching HubSpotError. 401 and 403 raise at once."""
        raise NotImplementedError

    def paginate(
        self,
        method: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of `results` across pages, following paging.next.after."""
        raise NotImplementedError

    def __repr__(self) -> str:
        """Never includes the token."""
        raise NotImplementedError


class HubSpotConnector:
    """The Connector for HubSpot. Fetches everything once, then serves it from memory until the
    cache expires or refresh() is called."""

    name = "hubspot"

    def __init__(
        self,
        client: HubSpotClient,
        mapping: HubSpotMapping | None = None,
        *,
        today: date | None = None,
        cache_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        raise NotImplementedError

    def list_accounts(self) -> list[Account]:
        raise NotImplementedError

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        raise NotImplementedError

    @property
    def warnings(self) -> list[str]:
        """Plain-English notes from the last load, e.g. "3 companies have no renewal date, so
        they were left out." Empty when everything mapped cleanly."""
        raise NotImplementedError

    @property
    def loaded_at(self) -> float | None:
        """clock() value of the last successful load, or None before the first."""
        raise NotImplementedError

    def refresh(self) -> None:
        """Drop the cache so the next call fetches from HubSpot again."""
        raise NotImplementedError

    def check_connection(self) -> ConnectionCheck:
        """Run small live calls (one page each) to test the token, each scope and the mapping.
        Never raises for HubSpot errors: each failure becomes a CheckResult."""
        raise NotImplementedError
