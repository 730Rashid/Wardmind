"""The single entry point the MCP server, CLI and web UI call. No surface re-implements this.

CONTRACT: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date

from cs_brief.core import secrets
from cs_brief.core.briefs import AccountDetail, account_detail, suggest_action
from cs_brief.core.config import ScoringConfig, load_scoring_config
from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectionNotReady
from cs_brief.core.connectors.hubspot import (
    API_BASE,
    HubSpotClient,
    HubSpotConnector,
    load_mapping,
)
from cs_brief.core.models import Motion, ScoredAccount
from cs_brief.core.onboarding import OnboardingBrief, onboarding_brief
from cs_brief.core.scoring import score_book
from cs_brief.core.settings import DataSource, load_settings
from cs_brief.core.store import LocalStore

# Test-only: points the HubSpot client at a local fake server. HubSpotClient refuses other hosts.
_API_BASE_ENV = "CS_BRIEF_HUBSPOT_API_BASE"
_NO_TOKEN_MESSAGE = (
    "HubSpot is selected, but no access token is saved. "
    "Add one on the Connections page, or switch to demo data."
)
_BAD_BASE_MESSAGE = "CS_BRIEF_HUBSPOT_API_BASE must point at 127.0.0.1 or localhost."

# Shared HubSpot connectors. Keys hold a hash of the token, never the token itself.
_connectors: dict[tuple[str, str, str], Connector] = {}


def hubspot_api_base() -> str:
    """The base URL for HubSpot calls: CS_BRIEF_HUBSPOT_API_BASE (test-only), else API_BASE."""
    return os.environ.get(_API_BASE_ENV) or API_BASE


def get_connector() -> Connector:
    """The connector for load_settings().source.

    Demo: DemoConnector(). HubSpot: one shared HubSpotConnector per (token, mapping, base_url)
    for the life of the process, so its cache survives across requests; raises
    ConnectionNotReady when no token is available. CS_BRIEF_HUBSPOT_API_BASE (test-only) is
    passed through as base_url; HubSpotClient rejects anything that isn't localhost.
    """
    if load_settings().source is DataSource.DEMO:
        return DemoConnector()
    token = secrets.get_hubspot_token()
    if token is None:
        raise ConnectionNotReady(_NO_TOKEN_MESSAGE)
    base = hubspot_api_base()
    mapping = load_mapping()
    key = (hashlib.sha256(token.encode("utf-8")).hexdigest(), mapping.model_dump_json(), base)
    connector = _connectors.get(key)
    if connector is None:
        try:
            client = HubSpotClient(token, base_url=base)
        except ValueError:
            raise ConnectionNotReady(_BAD_BASE_MESSAGE) from None
        connector = HubSpotConnector(client, mapping)
        _connectors[key] = connector
    return connector


def reset_connectors() -> None:
    """Forget shared connectors, e.g. after the token, mapping or source changes."""
    _connectors.clear()


def get_store() -> LocalStore:
    """The local store at store.data_dir(). Cheap to create; callers needn't cache it."""
    return LocalStore()


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
