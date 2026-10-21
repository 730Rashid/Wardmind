"""The single entry point the MCP server, CLI and web UI call. No surface re-implements this.

CONTRACT: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date

from cs_brief.core.briefs import AccountDetail, account_detail, suggest_action
from cs_brief.core.config import ScoringConfig, load_scoring_config
from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.models import Motion, ScoredAccount
from cs_brief.core.onboarding import OnboardingBrief, onboarding_brief
from cs_brief.core.scoring import score_book


def get_connector() -> Connector:
    """The connector for load_settings().source.

    Demo: DemoConnector(). HubSpot: one shared HubSpotConnector per (token, mapping, base_url)
    for the life of the process, so its cache survives across requests; raises
    ConnectionNotReady when no token is available. CS_BRIEF_HUBSPOT_API_BASE (test-only) is
    passed through as base_url; HubSpotClient rejects anything that isn't localhost.
    """
    return DemoConnector()  # T25 replaces this body


def reset_connectors() -> None:
    """Forget shared connectors, e.g. after the token, mapping or source changes."""
    raise NotImplementedError


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


def get_account_detail(
    account_id: str,
    connector: Connector | None = None,
    config: ScoringConfig | None = None,
    as_of: date | None = None,
) -> AccountDetail | None:
    """The account view's data, or None for an unknown id. Same defaults as get_book().

    Uses one connector, config and as_of for both the book and the account's signal history.
    """
    connector = connector if connector is not None else get_connector()
    config = config if config is not None else load_scoring_config()
    as_of = as_of if as_of is not None else date.today()
    book = get_book(connector, config, as_of)
    signals = connector.list_signals(account_id)
    return account_detail(book, account_id, signals, config, as_of)


def onboarding_accounts(book: list[ScoredAccount]) -> list[ScoredAccount]:
    """Accounts in the onboarding motion, in book (priority) order."""
    return [item for item in book if item.motion is Motion.ONBOARDING]


def get_onboarding_brief(
    account_id: str,
    connector: Connector | None = None,
    config: ScoringConfig | None = None,
    as_of: date | None = None,
) -> OnboardingBrief | None:
    """The onboarding call brief for any account, or None for an unknown id.

    Same defaults as get_book(); builds on get_account_detail() with the same config.
    """
    connector = connector if connector is not None else get_connector()
    config = config if config is not None else load_scoring_config()
    as_of = as_of if as_of is not None else date.today()
    detail = get_account_detail(account_id, connector, config, as_of)
    if detail is None:
        return None
    return onboarding_brief(detail, config.motion)


def find_account(book: list[ScoredAccount], account_id: str) -> ScoredAccount | None:
    """The first account with this id, or None."""
    return next((item for item in book if item.account.id == account_id), None)
