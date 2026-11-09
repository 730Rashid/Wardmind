"""The feed providers, by catalog id. Each module defines one provider class whose `info` is
catalog.provider_info(<its id>).

CONTRACT FILE: get_provider's signature is fixed.
"""

from __future__ import annotations

from collections.abc import Callable

from wardmind.core.feeds.providers import (
    airtable,
    chargebee,
    files,
    freshdesk,
    posthog,
    stripe,
    web,
    zendesk,
)
from wardmind.core.feeds.providers.base import Provider

_PROVIDERS: dict[str, Callable[[], Provider]] = {
    "file": files.FileProvider,
    "link": web.LinkProvider,
    "google_sheets": web.GoogleSheetsProvider,
    "json_api": web.JsonApiProvider,
    "sqlite": files.SqliteProvider,
    "stripe": stripe.StripeProvider,
    "chargebee": chargebee.ChargebeeProvider,
    "zendesk": zendesk.ZendeskProvider,
    "freshdesk": freshdesk.FreshdeskProvider,
    "posthog": posthog.PostHogProvider,
    "airtable": airtable.AirtableProvider,
}


def get_provider(provider_id: str) -> Provider | None:
    """The provider for a catalog id, or None for an unknown id. Every id in catalog.INFOS has
    one."""
    factory = _PROVIDERS.get(provider_id)
    return None if factory is None else factory()
