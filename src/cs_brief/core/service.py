"""The single entry point the MCP server, CLI and web UI call. No surface re-implements this.

CONTRACT: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date

from cs_brief.core.config import ScoringConfig
from cs_brief.core.connectors.base import Connector
from cs_brief.core.models import Motion, ScoredAccount


def get_connector() -> Connector:
    """The active connector. Demo only until the HubSpot phase."""
    raise NotImplementedError


def get_book(
    connector: Connector | None = None,
    config: ScoringConfig | None = None,
    as_of: date | None = None,
) -> list[ScoredAccount]:
    """Load, score and rank. Defaults: get_connector(), load_scoring_config(), date.today()."""
    raise NotImplementedError


def filter_book(
    book: list[ScoredAccount],
    motion: Motion | None = None,
    search: str | None = None,
) -> list[ScoredAccount]:
    """Keep book order. `search` is a case-insensitive substring match on account name."""
    raise NotImplementedError


def find_account(book: list[ScoredAccount], account_id: str) -> ScoredAccount | None:
    raise NotImplementedError
