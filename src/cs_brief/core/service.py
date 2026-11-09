"""The single entry point the MCP server, CLI and web UI call. No surface re-implements this.

CONTRACT: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ValidationError

from cs_brief.core import bulk, client_updates, contacts, import_flow, research, secrets, summaries
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
from cs_brief.core.feeds import setup as feed_setup
from cs_brief.core.feeds import sync as feed_sync
from cs_brief.core.feeds.models import FeedRole
from cs_brief.core.feeds.providers import get_provider
from cs_brief.core.models import Account, Motion, ScoredAccount, Signal
from cs_brief.core.onboarding import OnboardingBrief, onboarding_brief
from cs_brief.core.scoring import score_book
from cs_brief.core.settings import DataSource, Settings, load_settings, save_settings
from cs_brief.core.store import (
    WRITE_LOCK,
    ConnectionConfig,
    ConnectionKind,
    LocalStore,
    connection_for,
)

_log = logging.getLogger(__name__)

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
    if config.kind in (ConnectionKind.SPREADSHEET, ConnectionKind.FEED):
        # A feed's snapshot has the same shape as a spreadsheet's, so the same connector serves it.
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


def remove_connection(connection_id: str) -> None:
    """Remove a connection and what came with it (its accounts, history, edits, notices, links,
    hidden entries and readings; see LocalStore.remove_connection), plus the contacts, client
    update consents and updates of the accounts it owned (contacts.forget, client_updates.forget
    with store.connection_for(id) == connection_id). A feed's saved key goes too,
    so every caller removes a feed the same way. HubSpot's saved token is not touched here (the
    Connections page removes it). An unknown id does nothing. Shared connectors are forgotten."""
    store = get_store()
    with WRITE_LOCK:  # a sync that is reading this feed can't write its data back afterwards
        config = store.get_connection(connection_id)
        store.remove_connection(connection_id)

        def owned(account_id: str) -> bool:
            return connection_for(account_id) == connection_id

        contacts.forget(store, owned)
        client_updates.forget(store, owned)
        if config is not None and config.kind is ConnectionKind.FEED:
            feed_setup.remove_feed_secret(connection_id)
    reset_connectors()


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
    summaries: int = 0  # saved AI summaries removed


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


def _research_one(
    account: Account, refresh: bool, moment: datetime
) -> tuple[research.SiteResearch, bool]:
    """The lookup for one account the book already has: (the lookup, whether a saved one was
    reused). The same rules as research_account(), which calls this."""
    settings = load_settings()
    if not settings.web_research:
        raise research.ResearchOff(_RESEARCH_OFF)
    cache = research_cache()
    if settings.source is DataSource.DEMO:
        sample = research.sample_research(account, moment)
        if not refresh:
            saved = cache.get(account.id, sample.website, moment)
            if saved is not None:
                return saved, True
        cache.put(sample)
        return sample, False
    website = research.website_host(account.domain)
    if not refresh:
        saved = cache.get(account.id, website, moment)
        if saved is not None:
            return saved, True
    client = _site_client_factory()
    try:
        result = research.lookup(account, client, moment)
    finally:
        client.close()
    cache.put(result)
    return result, False


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
    result, _ = _research_one(account, refresh, moment)
    return result


def set_web_research(on: bool) -> None:
    """Save Settings.web_research. Turning it off also deletes every saved lookup, sample news
    included."""
    save_settings(load_settings().model_copy(update={"web_research": on}))
    if not on:
        research_cache().clear()


def clear_local_cache(now: datetime | None = None) -> CacheCleared:
    """Delete what cs-brief keeps only to save time: saved web research (sample news included),
    unfinished imports (import_flow drafts) and unfinished feed setups (their drafts and keys,
    whatever their age). Also forgets in-memory connector caches
    (reset_connectors), so the next page load re-reads every connection. Never touches
    connections, imported accounts, hand-added accounts, edits, readings, links, hidden accounts,
    settings or the token."""
    store = get_store()
    cleared = CacheCleared(
        research=research_cache().clear(),
        drafts=import_flow.clear_drafts(store),
        summaries=summary_store().clear(),
    )
    feed_setup.clear_feed_drafts(store)
    reset_connectors()
    return cleared


# --- AI summaries and looking up every account (see core/summaries.py and core/bulk.py) -----

# Test-only seam: tests replace this with a factory that builds a client on a MockTransport.
# The real one reads secrets.get_openrouter_key() and summaries.ENV_API_BASE (localhost only).
_openrouter_factory: Callable[[], summaries.OpenRouterClient] | None = None

# The one job runner for this process (the web app's "Look up all websites").
_runner = bulk.JobRunner(clock=lambda: datetime.now(UTC))

_RESEARCH_FIRST_MESSAGE = "Turn on web research first, in Settings > Data & privacy."
_AI_OFF_MESSAGE = "AI summaries are off. Turn them on in Settings > Data & privacy."
_NO_KEY_MESSAGE = "Add your OpenRouter API key in Settings > Data & privacy."
_MODEL_MESSAGE = "That isn't an OpenRouter model id, like anthropic/claude-haiku-5.5."
# Topics next to My book come from articles no older than this many days.
_FLAG_DAYS = 90


def _ai_problem() -> summaries.SummaryError | None:
    """Why AI summaries can't be used right now, as the error to raise; None when they can.
    Demo mode needs no key, because it invents its summaries and sends nothing."""
    settings = load_settings()
    if not settings.web_research:
        return summaries.SummariesOff(_RESEARCH_FIRST_MESSAGE)
    if not settings.ai_summaries:
        return summaries.SummariesOff(_AI_OFF_MESSAGE)
    if settings.source is DataSource.DEMO:
        return None
    if secrets.get_openrouter_key() is None:
        return summaries.NoKey(_NO_KEY_MESSAGE)
    return None


def ai_ready() -> str | None:
    """None when AI summaries can be used; else the plain-English reason they can't (web
    research off, AI summaries off, or no OpenRouter key). Demo mode needs no key."""
    problem = _ai_problem()
    return None if problem is None else problem.message


def summary_store() -> summaries.SummaryStore:
    """Saved summaries under store.data_dir() / summaries.SUMMARIES_DIR."""
    return summaries.SummaryStore(get_store().root / summaries.SUMMARIES_DIR)


def saved_summary(account_id: str) -> summaries.Summary | None:
    """The saved summary for this account, without any network call. None when AI summaries or
    web research are off, or there isn't one."""
    settings = load_settings()
    if not (settings.web_research and settings.ai_summaries):
        return None
    return summary_store().get(account_id)


def _client() -> summaries.OpenRouterClient:
    """A client for one call. Callers check _ai_problem() first, so a missing key is only
    reported here as a guard for the real factory."""
    if _openrouter_factory is not None:
        return _openrouter_factory()
    key = secrets.get_openrouter_key()
    if key is None:
        raise summaries.NoKey(_NO_KEY_MESSAGE)
    base = os.environ.get(summaries.ENV_API_BASE) or summaries.API_BASE
    return summaries.OpenRouterClient(key, base_url=base)


def _summary_one(
    account: Account, found: research.SiteResearch, refresh: bool
) -> tuple[summaries.Summary, bool]:
    """The summary for one account's lookup: (the summary, whether a saved one was reused). The
    saved summary is used while it matches this website text and model, unless `refresh`.
    Demo mode uses summaries.sample_summary(); nothing is sent. Every new summary is saved."""
    problem = _ai_problem()
    if problem is not None:
        raise problem
    settings = load_settings()
    store = summary_store()
    demo = settings.source is DataSource.DEMO
    model = summaries.SAMPLE_MODEL if demo else settings.ai_model
    if not refresh:
        saved = store.fresh(account.id, found, model)
        if saved is not None:
            return saved, True
    now = datetime.now(UTC)
    if demo:
        summary = summaries.sample_summary(account, found, now)
    else:
        client = _client()
        try:
            summary = summaries.summarise(account.id, found, client, model, now)
        finally:
            client.close()
    store.put(summary)
    return summary, False


def summarise_account(account_id: str, *, refresh: bool = False) -> summaries.Summary:
    """Summarise one account (the CSM asked). Raises summaries.SummariesOff / NoKey (see
    ai_ready), LookupError for an unknown id, research.ResearchError when the website can't be
    looked up, and the other summaries.SummaryError types. Uses research_account() (so its
    cache), then the saved summary if it is fresh for this text and model, unless `refresh`;
    else summaries.summarise() (demo: summaries.sample_summary(), nothing sent), saved."""
    problem = _ai_problem()
    if problem is not None:
        raise problem
    account = _book_account(account_id)
    if account is None:
        raise LookupError(f"No account with id {account_id}.")
    found, _ = _research_one(account, False, datetime.now(UTC))
    summary, _ = _summary_one(account, found, refresh)
    return summary


def saved_lookup(account: Account, now: datetime | None = None) -> research.SiteResearch | None:
    """The saved lookup for this account whatever its age, when it is for the website the account
    has now. Nothing is fetched. None when web research is off, or there isn't one."""
    if not load_settings().web_research:
        return None
    return _saved_lookup_for(account, now if now is not None else datetime.now(UTC))


def _saved_lookup_for(account: Account, moment: datetime) -> research.SiteResearch | None:
    """saved_lookup() without the setting check, for callers that have checked it once."""
    website = _saved_website(account, moment)
    if website is None:
        return None
    saved = research_cache().saved(account.id)
    return saved if saved is not None and saved.website == website else None


def account_flags() -> dict[str, list[research.Topic]]:
    """Topics to show next to each account in My book, without any network call: the saved
    summary's highlight topics, else the topics of saved lookup articles from the last 90 days.
    Each list is in Topic order without repeats; accounts with none are left out. Empty when
    web research is off."""
    settings = load_settings()
    if not settings.web_research:
        return {}
    store = summary_store()
    now = datetime.now(UTC)
    cutoff = now.date() - timedelta(days=_FLAG_DAYS)
    flags: dict[str, list[research.Topic]] = {}
    for account in get_connector().list_accounts():
        topics: set[research.Topic] = set()
        summary = store.get(account.id) if settings.ai_summaries else None
        if summary is not None and summary.highlights:
            topics.update(item.topic for item in summary.highlights)
        else:
            saved = _saved_lookup_for(account, now)
            if saved is not None:
                for article in saved.articles:
                    if article.published is not None and article.published >= cutoff:
                        topics.update(article.topics)
        if topics:
            flags[account.id] = [topic for topic in research.Topic if topic in topics]
    return flags


def _bulk_research(account: Account, refresh: bool) -> tuple[research.SiteResearch, bool]:
    return _research_one(account, refresh, datetime.now(UTC))


def _bulk_summary(account: Account, found: research.SiteResearch) -> tuple[summaries.Summary, bool]:
    return _summary_one(account, found, refresh=False)


def start_bulk_research(*, summarise: bool, refresh: bool) -> bulk.Job:
    """Start (or return the running) job over every account in the current book, in book
    order. Raises research.ResearchOff when web research is off, and summaries.SummariesOff /
    NoKey (from ai_ready) when `summarise` is asked for but can't be done. Each account reuses
    the same caches as research_account() and summarise_account()."""
    if not load_settings().web_research:
        raise research.ResearchOff(_RESEARCH_OFF)
    if summarise:
        problem = _ai_problem()
        if problem is not None:
            raise problem
    accounts = [item.account for item in get_book()]
    return _runner.start(
        accounts,
        _bulk_research,
        _bulk_summary if summarise else None,
        refresh=refresh,
    )


def bulk_job(job_id: str | None = None) -> bulk.Job | None:
    """The current or last job (see JobRunner.job)."""
    return _runner.job(job_id)


def set_ai_summaries(on: bool) -> None:
    """Save Settings.ai_summaries. Turning it off also deletes every saved summary."""
    save_settings(load_settings().model_copy(update={"ai_summaries": on}))
    if not on:
        summary_store().clear()


def checked_ai_model(model: str) -> str:
    """The model id stripped, or ValueError with the Settings validator's message. Saves nothing,
    so a form can check its model before it saves anything else."""
    try:
        return Settings(ai_model=model).ai_model
    except ValidationError as error:
        cause = error.errors()[0].get("ctx", {}).get("error")
        raise ValueError(str(cause) if cause else _MODEL_MESSAGE) from None


def set_ai_model(model: str) -> None:
    """Save Settings.ai_model. Raises ValueError (the Settings validator's message) for an id
    that isn't an OpenRouter model id. Saved summaries are kept; they are rewritten the next
    time each account is summarised, because a summary is only fresh for its own model."""
    value = checked_ai_model(model)
    save_settings(load_settings().model_copy(update={"ai_model": value}))


def check_openrouter_key() -> summaries.KeyInfo:
    """GET /key with the saved key, for "Test key". Raises summaries.NoKey without one, and the
    client's errors otherwise."""
    if secrets.get_openrouter_key() is None:
        raise summaries.NoKey(_NO_KEY_MESSAGE)
    client = _client()
    try:
        return client.key_info()
    finally:
        client.close()


# --- Data feeds (see core/feeds) -------------------------------------------------------------

_feed_scheduler: feed_sync.FeedScheduler | None = None
_FEED_FAILED = feed_sync.SYNC_FAILED
_HOUSEKEEPING_EVERY = timedelta(hours=1)
_last_housekeeping: datetime | None = None  # when sync_due_feeds last ran housekeeping


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def feed_book_accounts(exclude_connection_id: str | None = None) -> list[Account]:
    """The Your data book's accounts (after links, hidden accounts and edits), for matching a
    SIGNALS feed's rows. A connection's own data is left out, so a feed can't match its own rows.
    Empty in demo mode (feeds belong to Your data)."""
    if load_settings().source is DataSource.DEMO:
        return []
    store = get_store()
    _add_legacy_hubspot(store)
    members = [
        (config, _member(config, store))
        for config in store.connections()
        if config.enabled and config.id != exclude_connection_id
    ]
    return CombinedConnector(members, store, today=date.today()).list_accounts()


def _sync_one(config: ConnectionConfig, store: LocalStore, now: datetime) -> feed_sync.SyncOutcome:
    """One feed's sync. A SIGNALS feed matches against the book without its own data; a
    CLIENT_UPDATES feed matches its rows against the whole book (it owns no accounts)."""
    feed = config.feed
    book: list[Account] = []
    if feed is not None and feed.role is FeedRole.SIGNALS:
        book = feed_book_accounts(config.id)
    elif feed is not None and feed.role is FeedRole.CLIENT_UPDATES:
        book = feed_book_accounts(None)
    return feed_sync.sync_feed(config, store, book_accounts=book, now=now, today=date.today())


def sync_feed_now(connection_id: str) -> feed_sync.SyncOutcome:
    """Sync one feed now (Sync now). Raises LookupError for an id that isn't a FEED connection.
    Afterwards the shared connectors are invalidated so the next page shows the new data."""
    store = get_store()
    config = store.get_connection(connection_id)
    if config is None or config.kind is not ConnectionKind.FEED or config.feed is None:
        raise LookupError(f"No feed with id {connection_id}.")
    outcome = _sync_one(config, store, _utc_now())
    reset_connectors()
    return outcome


def housekeeping(now: datetime | None = None) -> None:
    """Delete what has run out of time: unfinished imports and feed setups older than their 24
    hours, with their feed keys, and feed keys left with no setup. Never raises: a failure is
    logged by its exception type only, and the other clean-up still runs."""
    moment = now if now is not None else _utc_now()
    try:
        import_flow.purge_old_drafts(get_store(), now=moment)
    except Exception as exc:
        _log.warning("Old imports weren't cleared (%s).", type(exc).__name__)
    try:
        feed_setup.purge_old_feed_drafts(get_store(), now=moment)
    except Exception as exc:
        _log.warning("Old feed setups weren't cleared (%s).", type(exc).__name__)


def sync_due_feeds() -> int:
    """Sync every enabled feed that feed_sync.is_due says is due, one after another; return how
    many were synced. Never raises for one feed's problem (it lands in that feed's status).
    Runs housekeeping at most once an hour."""
    global _last_housekeeping
    store = get_store()
    now = _utc_now()
    if _last_housekeeping is None or now - _last_housekeeping >= _HOUSEKEEPING_EVERY:
        _last_housekeeping = now
        housekeeping(now)
    synced = 0
    for config in store.connections():
        feed = config.feed
        if config.kind is not ConnectionKind.FEED or feed is None or not config.enabled:
            continue
        provider = get_provider(feed.provider)
        mtime = provider.source_mtime(dict(feed.settings)) if provider is not None else None
        if not feed_sync.is_due(config, now, mtime):
            continue
        try:
            _sync_one(config, store, now)
        except Exception as exc:  # one feed's failure must not stop the others
            _log.warning("Feed %s wasn't synced (%s).", config.id, type(exc).__name__)
            _record_failure(store, config.id, now)
            continue
        synced += 1
    if synced:
        reset_connectors()
    return synced


def _record_failure(store: LocalStore, connection_id: str, now: datetime) -> None:
    """Show an unexpected failure on the feed's status, so the CSM sees it and it is retried."""
    with WRITE_LOCK:
        latest = store.get_connection(connection_id)
        if latest is None or latest.feed is None:
            return
        status = latest.feed.status.model_copy(
            update={"last_attempt_at": now, "last_error": _FEED_FAILED}
        )
        feed = latest.feed.model_copy(update={"status": status})
        store.update_connection(latest.model_copy(update={"feed": feed}))


def start_feed_scheduler() -> None:
    """Start the background scheduler (idempotent). The web app calls it on startup. Runs
    housekeeping once before the first tick, which then waits its hour."""
    global _feed_scheduler, _last_housekeeping
    _last_housekeeping = _utc_now()
    housekeeping(_last_housekeeping)
    if _feed_scheduler is None:
        _feed_scheduler = feed_sync.FeedScheduler(tick=sync_due_feeds)
    _feed_scheduler.start()


def stop_feed_scheduler() -> None:
    """Stop it (idempotent). The web app calls it on shutdown; tests call it too."""
    if _feed_scheduler is not None:
        _feed_scheduler.stop()


# --- Contacts and client updates (see core/contacts.py, core/client_updates.py) ---------------


def account_contacts(account_id: str) -> list[contacts.Contact]:
    """The contacts shown on an account page. Demo mode: contacts.demo_contacts for a demo
    account (read-only), [] for an unknown id. Your data: contacts.contacts_for."""
    if load_settings().source is DataSource.DEMO:
        account = _book_account(account_id)
        return [] if account is None else contacts.demo_contacts(account.id, account.name)
    return contacts.contacts_for(get_store(), account_id)


def your_book_accounts() -> dict[str, Account]:
    """The Your data book (after links, hidden accounts and edits) by id; {} in demo mode. The
    client updates page and the feed sync use it to drop updates for accounts that are gone."""
    if load_settings().source is DataSource.DEMO:
        return {}
    return {account.id: account for account in get_connector().list_accounts()}


def pending_client_updates() -> list[client_updates.Review]:
    """client_updates.pending over your_book_accounts(); [] in demo mode."""
    if load_settings().source is DataSource.DEMO:
        return []
    return client_updates.pending(get_store(), your_book_accounts())


def pending_client_update_count() -> int:
    """For the nav badge on every page: the PENDING updates of accounts that still have a consent,
    read from the store alone. It never asks a connector (pages such as Settings must work while
    a connection is broken), so it may include updates for accounts that left the book; the
    review page lists those separately so they can be discarded. 0 in demo mode."""
    if load_settings().source is DataSource.DEMO:
        return 0
    data = client_updates.load(get_store())
    return sum(
        1
        for update in data.updates
        if update.status is client_updates.UpdateStatus.PENDING
        and update.account_id in data.consents
    )


def delete_all_local_data() -> None:
    """Settings > Data & privacy > Delete all local data: everything cs-brief keeps on this
    computer except settings.yaml and scoring.yaml (preferences) and the program itself.
    Deletes every connection, snapshot, hand-added account, edit, reading, link, hidden entry,
    notice, contact and client update (store.clear_all); unfinished imports and feed setups;
    saved web research and AI summaries; the HubSpot token, the OpenRouter key and every feed key.
    Environment variables are not touched (the page tells the CSM to remove those). Turns web
    research and AI summaries off and forgets shared connectors. Holds store.WRITE_LOCK so a
    running sync can't write anything back. Never raises for a file that is already gone."""
    store = get_store()
    with WRITE_LOCK:
        store.clear_all()
        import_flow.clear_drafts(store)
        secrets.delete_hubspot_token()
        secrets.delete_openrouter_key()
        secrets.delete_all_feed_secrets()
        research_cache().clear()
        summary_store().clear()
        save_settings(
            load_settings().model_copy(update={"web_research": False, "ai_summaries": False})
        )
        reset_connectors()
