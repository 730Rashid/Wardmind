"""Zendesk: a placeholder until its provider is built. A sync says it isn't available yet."""

from __future__ import annotations

from cs_brief.core import importing
from cs_brief.core.feeds import catalog
from cs_brief.core.feeds.http import FeedError, FeedHttp


class ZendeskProvider:
    info = catalog.provider_info("zendesk")

    def fetch(
        self, settings: dict[str, str], secret: str | None, http: FeedHttp
    ) -> importing.Table:
        raise FeedError("This connection isn't available yet.")

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None
