"""The single entry point the MCP server, CLI and web UI call. No surface re-implements this.

CONTRACT: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, date, datetime

from pydantic import BaseModel

from cs_brief.core import import_flow, research, secrets
from cs_brief.core.briefs import AccountDetail, account_detail, suggest_action
from cs_brief.core.config import ScoringConfig, load_scoring_config
from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.combined import CombinedConnector
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectionNotReady
from cs_brief.core.connectors.hubspot import (
    API_BASE,
    HubSpotClient,
    HubSpotConnector,
    load_mapping,
)
from cs_brief.core.connectors.local import ManualConnector, SpreadsheetConnector
from cs_brief.core.models import Account, Motion, ScoredAccount, Signal
from cs_brief.core.onboarding import OnboardingBrief, onboarding_brief
from cs_brief.core.scoring import score_book
from cs_brief.core.settings import DataSource, load_settings, save_settings
from cs_brief.core.store import ConnectionConfig, ConnectionKind, LocalStore

# Test-only: points the HubSpot client at a local fake server. HubSpotClient refuses other hosts.
_API_BASE_ENV = "CS_BRIEF_HUBSPOT_API_BASE"
_NO_TOKEN_MESSAGE = (
    "HubSpot is connected, but no access token is saved. Add one on the Connections page."
)
_BAD_BASE_MESSAGE = "CS_BRIEF_HUBSPOT_API_BASE must point at 127.0.0.1 or localhost."
_HUBSPOT_NAME = "HubSpot"

# Shared HubSpot connectors. Keys hold a hash of the token, never the token itself.
_connectors: dict[tuple[str, str, str], Connector] = {}


class _UnavailableConnector:
    """A HubSpot connection that can't be used right now. Its reads raise ConnectionNotReady, so
    CombinedConnector lists it as a problem and the rest of the book still loads."""

    name = "hubspot"

    def __init__(self, message: str) -> None:
        self._message = message

    def list_accounts(self) -> list[Account]:
        raise ConnectionNotReady(self._message)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        raise ConnectionNotReady(self._message)


def hubspot_api_base() -> str:
    """The base URL for HubSpot calls: CS_BRIEF_HUBSPOT_API_BASE (test-only), else API_BASE."""
    return os.environ.get(_API_BASE_ENV) or API_BASE


def _hubspot_member() -> Connector:
    """The shared HubSpot connector for the saved token and mapping. Without a token, or with a
    base URL that isn't allowed, an _UnavailableConnector. A mapping error propagates as
    ConnectorError: it is about the file, not about one connection."""
    token = secrets.get_hubspot_token()
    if token is None:
        return _UnavailableConnector(_NO_TOKEN_MESSAGE)
    base = hubspot_api_base()
    mapping = load_mapping()
    key = (hashlib.sha256(token.encode("utf-8")).hexdigest(), mapping.model_dump_json(), base)
    connector = _connectors.get(key)
    if connector is None:
        try:
            client = HubSpotClient(token, base_url=base)
        except ValueError:
            return _UnavailableConnector(_BAD_BASE_MESSAGE)
        connector = HubSpotConnector(client, mapping)
        _connectors[key] = connector
    return connector


def _member(config: ConnectionConfig, store: LocalStore) -> Connector:
    if config.kind is ConnectionKind.HUBSPOT:
        return _hubspot_member()
    if config.kind is ConnectionKind.SPREADSHEET:
        return SpreadsheetConnector(store, config.id)
    return ManualConnector(store)


def _add_legacy_hubspot(store: LocalStore) -> None:
    """The HubSpot-only setting of earlier versions: a token saved from the page, with no
    connections at all, becomes a HubSpot connection. It happens once, because the connection
    then exists. A token from the environment variable doesn't count: otherwise removing the
    HubSpot connection would undo itself on the next load while the variable is still set."""
    if store.connections():
        return
    if secrets.token_status().source is secrets.TokenSource.FILE:
        store.add_connection(ConnectionKind.HUBSPOT, _HUBSPOT_NAME)


def hubspot_connector() -> Connector | None:
    """The shared HubSpot connector the book reads from, for the Connections page's fetch time.
    None when no HubSpot connection is enabled, or it can't be used (no token, bad base URL)."""
    store = get_store()
    _add_legacy_hubspot(store)
    if not any(c.kind is ConnectionKind.HUBSPOT and c.enabled for c in store.connections()):
        return None
    member = _hubspot_member()
    return None if isinstance(member, _UnavailableConnector) else member


def get_connector() -> Connector:
    """The connector for load_settings().source.

    Demo: DemoConnector(). Your data: a CombinedConnector over the enabled connections in the
    local store, in order. A connection that can't be used (HubSpot without a token) becomes a
    problem on the combined connector, so the rest of the book still loads. A saved HubSpot token
    with no connections yet gets a HubSpot connection first (see _add_legacy_hubspot). HubSpot
    connectors are shared per (token, mapping, base_url) for the life of the process.
    CS_BRIEF_HUBSPOT_API_BASE (test-only) is passed through as base_url; HubSpotClient rejects
    anything that isn't localhost.
    """
    if load_settings().source is DataSource.DEMO:
        return DemoConnector()
    store = get_store()
    _add_legacy_hubspot(store)
    members = [(config, _member(config, store)) for config in store.connections() if config.enabled]
    return CombinedConnector(members, store, today=date.today())


def get_combined() -> CombinedConnector | None:
    """get_connector() when it is the combined "Your data" connector, else None (demo data).
    Callers that refresh or inspect the connections use this."""
    connector = get_connector()
    return connector if isinstance(connector, CombinedConnector) else None


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


# --- Web research and the local cache (see core/research.py for the privacy rules) ---------

# Test-only seam: tests replace this with a factory that builds a SiteClient on a MockTransport.
_site_client_factory: Callable[[], research.SiteClient] = research.SiteClient
_RESEARCH_OFF = "Web research is off. Turn it on in Settings > Data & privacy to use it."


class CacheCleared(BaseModel):
    research: int  # saved web research lookups removed
    drafts: int  # unfinished imports removed (their pasted cells or uploaded file)


def research_cache() -> research.ResearchCache:
    """Saved lookups under store.data_dir() / research.RESEARCH_DIR."""
    return research.ResearchCache(get_store().root / research.RESEARCH_DIR)


def _book_account(account_id: str) -> Account | None:
    """The account as the current book shows it (hidden accounts are not in the book), or None."""
    return next((item for item in get_connector().list_accounts() if item.id == account_id), None)


def _saved_website(account: Account, moment: datetime) -> str | None:
    """The website a saved lookup of this account is filed under. Demo accounts file their
    sample under the sample's website, the same one research_account() uses. None when a real
    account has no website to look up."""
    if load_settings().source is DataSource.DEMO:
        return research.sample_research(account, moment).website
    try:
        return research.website_host(account.domain)
    except research.ResearchError:
        return None


def cached_research(account_id: str, now: datetime | None = None) -> research.SiteResearch | None:
    """The saved lookup for this account, without any network call. None when web research is
    off, the account is unknown, it has no saved lookup, or the saved one is stale or was for a
    different website. Demo samples are saved like real lookups, so they show here too once
    looked up (the no-JavaScript redirect after a look up reads them from here)."""
    if not load_settings().web_research:
        return None
    account = _book_account(account_id)
    if account is None:
        return None
    moment = now if now is not None else datetime.now(UTC)
    website = _saved_website(account, moment)
    if website is None:
        return None
    return research_cache().get(account_id, website, moment)


def research_account(
    account_id: str, *, refresh: bool = False, now: datetime | None = None
) -> research.SiteResearch:
    """Look the account up (the CSM asked). Raises research.ResearchOff when the setting is
    off, LookupError for an unknown id, and the other research.ResearchError types from the
    lookup. Demo mode returns research.sample_research(), saved like a real lookup: a saved
    sample is reused unless `refresh`, and nothing is sent. Otherwise uses the cache unless
    `refresh`, else research.lookup() with a client from _site_client_factory() (always
    closed), and saves the result. `now` defaults to datetime.now(UTC)."""
    settings = load_settings()
    if not settings.web_research:
        raise research.ResearchOff(_RESEARCH_OFF)
    moment = now if now is not None else datetime.now(UTC)
    account = _book_account(account_id)
    if account is None:
        raise LookupError(f"No account with id {account_id}.")
    cache = research_cache()
    if settings.source is DataSource.DEMO:
        sample = research.sample_research(account, moment)
        if not refresh:
            saved = cache.get(account_id, sample.website, moment)
            if saved is not None:
                return saved
        cache.put(sample)
        return sample
    website = research.website_host(account.domain)
    if not refresh:
        saved = cache.get(account_id, website, moment)
        if saved is not None:
            return saved
    client = _site_client_factory()
    try:
        result = research.lookup(account, client, moment)
    finally:
        client.close()
    cache.put(result)
    return result


def set_web_research(on: bool) -> None:
    """Save Settings.web_research. Turning it off also deletes every saved lookup, sample news
    included."""
    save_settings(load_settings().model_copy(update={"web_research": on}))
    if not on:
        research_cache().clear()


def clear_local_cache(now: datetime | None = None) -> CacheCleared:
    """Delete what cs-brief keeps only to save time: saved web research (sample news included)
    and unfinished imports (import_flow drafts). Also forgets in-memory connector caches
    (reset_connectors), so the next page load re-reads every connection. Never touches
    connections, imported accounts, hand-added accounts, edits, readings, links, hidden accounts,
    settings or the token."""
    store = get_store()
    cleared = CacheCleared(
        research=research_cache().clear(),
        drafts=import_flow.clear_drafts(store),
    )
    reset_connectors()
    return cleared
