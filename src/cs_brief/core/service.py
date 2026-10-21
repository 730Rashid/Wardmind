"""The single entry point the MCP server, CLI and web UI call. No surface re-implements this.

CONTRACT: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date

from cs_brief.core.briefs import suggest_action
from cs_brief.core.config import ScoringConfig, load_scoring_config
from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.models import Motion, ScoredAccount
from cs_brief.core.scoring import score_book


def get_connector() -> Connector:
    """The active connector. Demo only until the HubSpot phase."""
    return DemoConnector()


def get_book(
    connector: Connector | None = None,
    config: ScoringConfig | None = None,
    as_of: date | None = None,
) -> list[ScoredAccount]:
    """Load, score and rank. Defaults: get_connector(), load_scoring_config(), date.today()."""
    connector = connector if connector is not None else get_connector()
    config = config if config is not None else load_scoring_config()
    as_of = as_of if as_of is not None else date.today()
    scored = score_book(connector.list_accounts(), connector.list_signals(), config, as_of)
    return [
        item.model_copy(update={"suggested_action": suggest_action(item.motion, item.reasons)})
        for item in scored
    ]


def filter_book(
    book: list[ScoredAccount],
    motion: Motion | None = None,
    search: str | None = None,
) -> list[ScoredAccount]:
    """Keep book order. `search` is a case-insensitive substring match on account name."""
    needle = (search or "").strip().casefold()
    return [
        item
        for item in book
        if (motion is None or item.motion == motion)
        and (not needle or needle in item.account.name.casefold())
    ]


def find_account(book: list[ScoredAccount], account_id: str) -> ScoredAccount | None:
    """The first account with this id, or None."""
    return next((item for item in book if item.account.id == account_id), None)
