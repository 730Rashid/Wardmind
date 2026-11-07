"""The feed providers, by catalog id. Each module defines one provider class whose `info` is
catalog.provider_info(<its id>).

CONTRACT FILE: get_provider's signature is fixed.
"""

from __future__ import annotations

from cs_brief.core.feeds.providers.base import Provider


def get_provider(provider_id: str) -> Provider | None:
    """The provider for a catalog id, or None for an unknown id. Every id in catalog.INFOS has
    one."""
    raise NotImplementedError
