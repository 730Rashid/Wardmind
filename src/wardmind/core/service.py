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

from wardmind.core import (
    books,
    bulk,
    client_updates,
    contacts,
    effects,
    growth,
    history,
    import_flow,
    journal,
    logos,
    mailbox,
    proposals,
    research,
    rules,
    runlog,
    secrets,
    summaries,
    vault,
)
from wardmind.core.briefs import AccountDetail, account_detail, suggest_action
from wardmind.core.config import ScoringConfig, load_scoring_config
from wardmind.core.connectors.base import Connector
from wardmind.core.connectors.combined import CombinedConnector
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.connectors.errors import ConnectionNotReady
from wardmind.core.connectors.hubspot import (
    API_BASE,
    HubSpotClient,
    HubSpotConnector,
    load_mapping,
)
from wardmind.core.connectors.local import ManualConnector, SpreadsheetConnector
from wardmind.core.feeds import setup as feed_setup
from wardmind.core.feeds import sync as feed_sync
from wardmind.core.feeds.models import FeedRole
from wardmind.core.feeds.providers import get_provider
from wardmind.core.models import Account, Motion, ScoredAccount, Signal, SignalKind
from wardmind.core.onboarding import OnboardingBrief, onboarding_brief
from wardmind.core.scoring import latest_signals, score_book
from wardmind.core.settings import (
    AIProvider,
    DataSource,
    Settings,
    check_local_model,
    check_local_url,
    load_settings,
    save_settings,
)
from wardmind.core.store import (
    WRITE_LOCK,
    ConnectionConfig,
    ConnectionKind,
    LocalStore,
    connection_for,
)

_log = logging.getLogger(__name__)

# Test-only: points the HubSpot client at a local fake server. HubSpotClient refuses other hosts.
_API_BASE_ENV = "WARDMIND_HUBSPOT_API_BASE"
_NO_TOKEN_MESSAGE = (
    "HubSpot is connected, but no access token is saved. Add one on the Connections page."
)
_BAD_BASE_MESSAGE = "WARDMIND_HUBSPOT_API_BASE must point at 127.0.0.1 or localhost."
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
    """The base URL for HubSpot calls: WARDMIND_HUBSPOT_API_BASE (test-only), else API_BASE."""
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
    WARDMIND_HUBSPOT_API_BASE (test-only) is passed through as base_url; HubSpotClient rejects
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
    update consents and updates, journal entries and score history of the accounts it owned
    (each forget with store.connection_for(id) == connection_id). A feed's saved key goes too,
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
        journal.Journal(store).forget(owned)
        history.HistoryStore(store).forget(owned)
        logos.forget(store, owned)
        if config is not None and config.kind is ConnectionKind.FEED:
            feed_setup.remove_feed_secret(connection_id)
    reset_connectors()


def reset_connectors() -> None:
    """Forget shared connectors, e.g. after the token, mapping or source changes, and the scored
    demo book and its rescored days."""
    _connectors.clear()
    _DEMO_BOOKS.clear()
    _DEMO_SNAPSHOTS.clear()


def get_store() -> LocalStore:
    """The local store at store.data_dir(). Cheap to create; callers needn't cache it."""
    return LocalStore()


# The packaged demo book, scored: the same for every request on a day under the same scoring, so
# it is worked out once. Keyed by (the demo's day, as_of, the scoring config as JSON); a handful
# of entries at most. Your data is never cached here: its readings change under it.
_DEMO_BOOKS: dict[tuple[date, date, str], list[ScoredAccount]] = {}
_DEMO_BOOKS_MAX = 8


def get_book(
    connector: Connector | None = None,
    config: ScoringConfig | None = None,
    as_of: date | None = None,
) -> list[ScoredAccount]:
    """Load, score and rank. Defaults: get_connector(), load_scoring_config(), date.today()."""
    connector = connector if connector is not None else get_connector()
    config = config if config is not None else load_scoring_config()
    as_of = as_of if as_of is not None else date.today()
    key = None
    if isinstance(connector, DemoConnector) and connector.packaged:
        key = (connector.today, as_of, config.model_dump_json())
        cached = _DEMO_BOOKS.get(key)
        if cached is not None:
            return list(cached)
    scored = score_book(connector.list_accounts(), connector.list_signals(), config, as_of)
    book = [
        item.model_copy(
            update={"suggested_action": suggest_action(item.motion, item.reasons, config)}
        )
        for item in scored
    ]
    if key is not None:
        if len(_DEMO_BOOKS) >= _DEMO_BOOKS_MAX:
            _DEMO_BOOKS.clear()
        _DEMO_BOOKS[key] = book
        return list(book)
    return book


def latest_book_signals(connector: Connector | None = None) -> dict[str, dict[SignalKind, Signal]]:
    """Every account's latest reading per kind, from the same connector get_book() uses."""
    connector = connector if connector is not None else get_connector()
    return latest_signals(connector.list_signals())


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
    return onboarding_brief(detail, config.motion, config)


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
    """Delete what Wardmind keeps only to save time: saved web research (sample news included),
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
# The same for a model on this computer: tests build a LocalModelClient on a MockTransport.
_local_factory: Callable[[str], summaries.LocalModelClient] | None = None

# The one job runner for this process (the web app's "Look up all websites").
_runner = bulk.JobRunner(clock=lambda: datetime.now(UTC))

_RESEARCH_FIRST_MESSAGE = "Turn on web research first, in Settings > Data & privacy."
_AI_OFF_MESSAGE = "AI summaries are off. Turn them on in Settings > Data & privacy."
_NO_KEY_MESSAGE = "Add your OpenRouter API key in Settings > Data & privacy."
_NO_LOCAL_MODEL_MESSAGE = (
    "Choose which model on this computer writes summaries, in Settings > Data & privacy."
)
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
    if settings.ai_provider is AIProvider.LOCAL:
        if not settings.local_ai_model:
            return summaries.NoLocalModel(_NO_LOCAL_MODEL_MESSAGE)
        return None
    if secrets.get_openrouter_key() is None:
        return summaries.NoKey(_NO_KEY_MESSAGE)
    return None


def ai_ready() -> str | None:
    """None when AI summaries can be used; else the plain-English reason they can't (web
    research off, AI summaries off, or no OpenRouter key). Demo mode needs no key."""
    problem = _ai_problem()
    return None if problem is None else problem.message


def ai_training_model() -> str | None:
    """The chosen model when its providers may train on what they're sent
    (summaries.trains_on_data) and using it would send something; None otherwise. Demo mode
    sends nothing, so it is None there, and so does a model on this computer."""
    settings = load_settings()
    if (
        settings.source is DataSource.DEMO
        or settings.ai_provider is AIProvider.LOCAL
        or not summaries.trains_on_data(settings.ai_model)
    ):
        return None
    return settings.ai_model


def _training_problem(allow_training: bool) -> summaries.TrainingNotConfirmed | None:
    model = ai_training_model()
    if model is None or allow_training:
        return None
    return summaries.TrainingNotConfirmed(summaries.training_message(model))


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


def _local_client(base_url: str) -> summaries.LocalModelClient:
    """A client for the model server on this computer at `base_url`."""
    if _local_factory is not None:
        return _local_factory(base_url)
    return summaries.LocalModelClient(base_url)


def ai_problem() -> summaries.SummaryError | None:
    """Why the configured AI can't be used now (no key, no local model chosen), or None. For
    callers outside this module, e.g. email drafts (web/draft_pages.py)."""
    return _ai_problem()


def training_problem(allow_training: bool) -> summaries.TrainingNotConfirmed | None:
    """The free-model guard (see _training_problem), for callers outside this module."""
    return _training_problem(allow_training)


def ai_client() -> summaries.OpenRouterClient | summaries.LocalModelClient:
    """The client for the configured AI, for callers outside this module (see _client)."""
    return _client()


def _client() -> summaries.OpenRouterClient | summaries.LocalModelClient:
    """A client for one call: the model on this computer when that is chosen, else OpenRouter.
    Callers check _ai_problem() first, so a missing key is only reported here as a guard for
    the real factory."""
    settings = load_settings()
    if settings.ai_provider is AIProvider.LOCAL:
        return _local_client(settings.local_ai_url)
    if _openrouter_factory is not None:
        return _openrouter_factory()
    key = secrets.get_openrouter_key()
    if key is None:
        raise summaries.NoKey(_NO_KEY_MESSAGE)
    base = os.environ.get(summaries.ENV_API_BASE) or summaries.API_BASE
    return summaries.OpenRouterClient(key, base_url=base)


def _summary_one(
    account: Account, found: research.SiteResearch, refresh: bool, allow_training: bool = False
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
    local = settings.ai_provider is AIProvider.LOCAL
    model = settings.local_ai_model if local else settings.ai_model
    # The name a saved summary carries: a local model's is "local/<name>" (summaries.summarise).
    if demo:
        saved_as = summaries.SAMPLE_MODEL
    else:
        saved_as = summaries.local_model_id(model) if local else model
    if not refresh:
        saved = store.fresh(account.id, found, saved_as)
        if saved is not None:
            return saved, True
    now = datetime.now(UTC)
    if demo:
        summary = summaries.sample_summary(account, found, now)
    else:
        client = _client()
        try:
            summary = summaries.summarise(
                account.id, found, client, model, now, allow_training=allow_training
            )
        finally:
            client.close()
    store.put(summary)
    return summary, False


def summarise_account(
    account_id: str, *, refresh: bool = False, allow_training: bool = False
) -> summaries.Summary:
    """Summarise one account (the CSM asked). Raises summaries.SummariesOff / NoKey (see
    ai_ready), summaries.TrainingNotConfirmed for a model that trains on what it's sent unless
    `allow_training` (the CSM confirmed it), LookupError for an unknown id,
    research.ResearchError when the website can't be looked up, and the other
    summaries.SummaryError types. Uses research_account() (so its cache), then the saved summary
    if it is fresh for this text and model, unless `refresh`; else summaries.summarise() (demo:
    summaries.sample_summary(), nothing sent), saved."""
    problem = _ai_problem() or _training_problem(allow_training)
    if problem is not None:
        raise problem
    account = _book_account(account_id)
    if account is None:
        raise LookupError(f"No account with id {account_id}.")
    found, _ = _research_one(account, False, datetime.now(UTC))
    summary, _ = _summary_one(account, found, refresh, allow_training)
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
    found = _research_one(account, refresh, datetime.now(UTC))
    _find_logo_quietly(account)  # after the lookup, so a failed lookup never reaches the logo
    return found


def _bulk_summary(
    account: Account, found: research.SiteResearch, allow_training: bool = False
) -> tuple[summaries.Summary, bool]:
    return _summary_one(account, found, False, allow_training)


def start_bulk_research(
    *, summarise: bool, refresh: bool, allow_training: bool = False
) -> bulk.Job:
    """Start (or return the running) job over every account in the current book, in book
    order. Raises research.ResearchOff when web research is off, and summaries.SummariesOff /
    NoKey (from ai_ready) when `summarise` is asked for but can't be done, or
    TrainingNotConfirmed for a model that trains on what it's sent unless `allow_training`.
    Each account reuses the same caches as research_account() and summarise_account()."""
    if not load_settings().web_research:
        raise research.ResearchOff(_RESEARCH_OFF)
    if summarise:
        problem = _ai_problem() or _training_problem(allow_training)
        if problem is not None:
            raise problem
    accounts = [item.account for item in get_book()]
    book_id = books.active_id()

    # The job runs in its own thread: each step is bound to the book it started in.
    def research_in_book(account: Account, again: bool) -> tuple[research.SiteResearch, bool]:
        with books.bound(book_id):
            return _bulk_research(account, again)

    def summary_in_book(
        account: Account, found: research.SiteResearch
    ) -> tuple[summaries.Summary, bool]:
        with books.bound(book_id):
            return _bulk_summary(account, found, allow_training)

    return _runner.start(
        accounts,
        research_in_book,
        summary_in_book if summarise else None,
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


def checked_local_ai(url: str, model: str) -> tuple[str, str]:
    """The local model server's address and model name, cleaned, or ValueError with the reason
    (settings.check_local_url / check_local_model). Saves nothing."""
    return check_local_url(url), check_local_model(model)


def set_ai_provider(provider: AIProvider, url: str, model: str) -> None:
    """Save who writes AI summaries, and the local server's address and model name (kept even
    while OpenRouter is chosen). Raises ValueError like checked_local_ai. Saved summaries are
    kept; each is only reused for the model that wrote it."""
    clean_url, clean_model = checked_local_ai(url, model)
    save_settings(
        load_settings().model_copy(
            update={
                "ai_provider": provider,
                "local_ai_url": clean_url,
                "local_ai_model": clean_model,
            }
        )
    )


def local_models(url: str) -> list[str]:
    """The models the server at `url` (on this computer) has, for "Test connection". Raises
    ValueError for an address off this computer, and summaries.AIUnavailable when it can't be
    reached or read. Only that server is contacted."""
    client = _local_client(check_local_url(url))
    try:
        return client.models()
    finally:
        client.close()


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
# book id -> the day the scheduler last asked remember_book for that book's snapshot. A new
# day, or another open book, asks again at once (see _sync_due_feeds).
_snapshot_days: dict[str, date] = {}
# When this process last asked for each book's snapshot (naive UTC): a day with no snapshot file is
# asked again only after _RETRY_AFTER, so a snapshot that keeps failing isn't retried every tick.
_snapshot_tried: dict[str, datetime] = {}


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
    """_sync_due_feeds bound to the book open when the tick starts (see books.bound): a book
    opened during the sync never gets this book's data, and this book's feeds never get its
    keys."""
    with books.bound(books.active_id()):
        return _sync_due_feeds()


def run_checks_now() -> int:
    """Settings > System status > Run checks now: the scheduler's tick, at once, for the open book,
    with the day's snapshot asked for again (remember_book still skips one taken within the hour).
    Returns how many feeds were synced."""
    book = books.active_id()
    _snapshot_days.pop(book, None)
    _snapshot_tried.pop(book, None)
    return sync_due_feeds()


def _no_snapshot_for(day: date) -> bool:
    """Your data has no score snapshot file for `day` (it was never written, or Delete all local
    data removed it), so the tick takes one even though this process already tried today."""
    if _demo_mode():
        return False
    try:
        return day not in history_store().days()
    except Exception:  # a folder that can't be read: remember_book reports its own failure
        return False


def _sync_due_feeds() -> int:
    """Sync every enabled feed that feed_sync.is_due says is due, one after another; return how
    many were synced. Never raises for one feed's problem: it lands in that feed's status and the
    run log (core/runlog.py). Each tick catches up on what has fallen due. Housekeeping and the
    day's score snapshot (remember_book) run at most once an hour, and the snapshot runs again
    whenever the date or the open book has changed since the last one, so a book switch takes its
    own snapshot. Then the feeds, the mailbox, and the day's follow-up rules and suggestions, which
    run if they haven't succeeded for the day (_daily_steps). While the book is locked it does
    nothing and returns 0."""
    global _last_housekeeping
    now = _utc_now()
    runlog.beat(now)
    if not vault.ensure_unlocked():
        return 0
    store = get_store()
    today = date.today()
    book = books.active_id()
    hourly = _last_housekeeping is None or now - _last_housekeeping >= _HOUSEKEEPING_EVERY
    if hourly:
        _last_housekeeping = now
        _job("housekeeping", lambda: housekeeping(now))
    missing = _no_snapshot_for(today) and now - _snapshot_tried.get(book, now) >= _RETRY_AFTER
    if hourly or _snapshot_days.get(book) != today or missing:
        _snapshot_days[book] = today
        _snapshot_tried[book] = now
        remember_book()
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
            with runlog.run(f"feed:{config.id}", store=store) as job:
                outcome = _sync_one(config, store, now)
                job.counts.update(
                    {"rows": outcome.status.rows, "accounts": outcome.status.accounts}
                )
                if outcome.status.last_error is not None:
                    job.fail(outcome.status.last_error)
        except Exception as exc:  # one feed's failure must not stop the others
            _log.warning("Feed %s wasn't synced (%s).", config.id, type(exc).__name__)
            _record_failure(store, config.id, now)
            continue
        synced += 1
    _sync_mailbox_if_due(now)
    _daily_steps(now, today)
    if synced:
        reset_connectors()
    return synced


def _job(name: str, body: Callable[[], None]) -> None:
    """One step of the background work, inside runlog.run(name). A failure is recorded there and
    logged here by its exception type; it never stops the steps after it."""
    try:
        with runlog.run(name):
            body()
    except Exception as exc:
        _log.warning("The %s step didn't finish (%s).", name, type(exc).__name__)


def _record_failure(store: LocalStore, connection_id: str, now: datetime) -> None:
    """Show an unexpected failure on the feed's status, so the CSM sees it and it is retried."""
    with WRITE_LOCK:
        latest = store.get_connection(connection_id)
        if latest is None or latest.feed is None:
            return
        status = feed_sync.with_failure(
            latest.feed.status.model_copy(update={"last_attempt_at": now}), _FEED_FAILED, now
        )
        feed = latest.feed.model_copy(update={"status": status})
        store.update_connection(latest.model_copy(update={"feed": feed}))


def start_feed_scheduler() -> None:
    """Start the background scheduler (idempotent). The web app calls it on startup. Its first tick
    runs at once, so the day's score snapshot and housekeeping don't wait an hour; later ticks run
    every feed_sync.TICK_SECONDS."""
    global _feed_scheduler
    runlog.scheduler_started(_utc_now())
    if _feed_scheduler is None:
        _feed_scheduler = feed_sync.FeedScheduler(tick=sync_due_feeds)
    _feed_scheduler.start()


def stop_feed_scheduler() -> None:
    """Stop it (idempotent). The web app calls it on shutdown; tests call it too."""
    runlog.scheduler_stopped()
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
    """Settings > Data & privacy > Delete all local data: everything Wardmind keeps on this
    computer except settings.yaml and scoring.yaml (preferences) and the program itself.
    Deletes every connection, snapshot, hand-added account, edit, reading, link, hidden entry,
    notice, contact and client update (store.clear_all); unfinished imports and feed setups;
    saved web research and AI summaries; the HubSpot token, the OpenRouter key and every feed key;
    and the encryption keys first (core/vault.py), so anything left behind can't be read. Every
    book but the first is deleted with its folder, and the books list goes too (core/books.py),
    so the first book is the open one when the rest is cleared.
    Environment variables are not touched (the page tells the CSM to remove those). Turns web
    research and AI summaries off and forgets shared connectors. Holds store.WRITE_LOCK so a
    running sync can't write anything back. Never raises for a file that is already gone."""
    with WRITE_LOCK:
        # The keys go first: if anything below fails, what is left can't be read.
        vault.destroy_keys()
        books.delete_everything()
        store = get_store()
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


# --- Logos (see core/logos.py for the three sources and the file rules) -----------------------

LOGO_NO_WEBSITE_MESSAGE = "Add the account's website first."
LOGO_UPLOADED_MESSAGE = "You uploaded this logo. Remove it first to look for one on their website."
_LOGO_LOOKUP_DEMO = "Logos can only be looked up for your own accounts."


def logo_info(account_id: str) -> logos.LogoInfo | None:
    """The account's saved logo (its info), or None: the monogram shows instead."""
    return logos.get_info(get_store(), account_id)


def all_logos() -> dict[str, logos.LogoInfo]:
    """Every saved logo by account id, for the list pages (one read)."""
    return logos.all_info(get_store())


def upload_logo(account_id: str, data: bytes) -> logos.LogoInfo:
    """Save the CSM's own image for this account, replacing any logo it had. LogoError (the
    checker's message) for an image that can't be used. The web page checks the account is
    in Your data first."""
    return logos.save_logo(get_store(), account_id, data, logos.LogoSource.UPLOAD, now=_utc_now())


def remove_logo(account_id: str) -> bool:
    """Remove the account's logo, uploaded or found. True if one was removed."""
    return logos.remove_logo(get_store(), account_id)


def find_logo(account_id: str) -> logos.LogoInfo:
    """Look for the account's logo on its own website and save it (the CSM asked). Raises
    research.ResearchOff when web research is off, LookupError for an unknown id,
    LogoError(LOGO_NO_WEBSITE_MESSAGE) without a website, LogoError(LOGO_UPLOADED_MESSAGE) when
    the CSM uploaded the logo (an uploaded logo is never replaced here), LogoError for demo
    accounts (nothing is looked up for them), and LogoError("Couldn't find a logo ...") when none
    works. Research errors (blocked or unreachable website) propagate. Uses one client from
    _site_client_factory(), closed afterwards, like research_account()."""
    settings = load_settings()
    if not settings.web_research:
        raise research.ResearchOff(_RESEARCH_OFF)
    account = _book_account(account_id)
    if account is None:
        raise LookupError(f"No account with id {account_id}.")
    if settings.source is DataSource.DEMO:
        raise logos.LogoError(_LOGO_LOOKUP_DEMO)
    store = get_store()
    current = logos.get_info(store, account_id)
    if current is not None and current.source is logos.LogoSource.UPLOAD:
        raise logos.LogoError(LOGO_UPLOADED_MESSAGE)
    try:
        website = research.website_host(account.domain)
    except research.NoWebsite:
        raise logos.LogoError(LOGO_NO_WEBSITE_MESSAGE) from None
    client = _site_client_factory()
    try:
        data, _, url = logos.find_on_website(client, website)
    finally:
        client.close()
    with WRITE_LOCK:  # an upload made while the site was being read is kept, not replaced
        latest = logos.get_info(store, account_id)
        if latest is not None and latest.source is logos.LogoSource.UPLOAD:
            raise logos.LogoError(LOGO_UPLOADED_MESSAGE)
        return logos.save_logo(
            store, account_id, data, logos.LogoSource.WEBSITE, now=_utc_now(), origin_url=url
        )


def _find_logo_quietly(account: Account) -> None:
    """The bulk job's logo step: an account with no logo yet gets one looked for on its website,
    with its own client (so its own request budget). Never raises: a failure, a demo account or a
    site with no logo just leaves the account with its monogram, and its lookup stands."""
    if load_settings().source is DataSource.DEMO:
        return
    store = get_store()
    if logos.get_info(store, account.id) is not None:
        return
    try:
        website = research.website_host(account.domain)
        client = _site_client_factory()
        try:
            data, _, url = logos.find_on_website(client, website)
        finally:
            client.close()
        with WRITE_LOCK:  # saved only if no logo arrived meanwhile (an upload is never replaced)
            if logos.get_info(store, account.id) is None:
                logos.save_logo(
                    store,
                    account.id,
                    data,
                    logos.LogoSource.WEBSITE,
                    now=_utc_now(),
                    origin_url=url,
                )
    except Exception as exc:
        _log.info("No logo was found for an account (%s).", type(exc).__name__)


# --- The journal and score history (see core/journal.py, core/history.py) -------------------


def _demo_mode() -> bool:
    return load_settings().source is DataSource.DEMO


def get_journal() -> journal.Journal:
    """The journal for the data showing: demo data has its own file, so notes typed while trying
    the demo never mix with real accounts. The demo's notes are kept with the first book, so
    they are the same whichever book was open last (core/books.py)."""
    if _demo_mode():
        return journal.Journal(LocalStore(books.book_data_dir(books.DEFAULT_BOOK)), demo=True)
    return journal.Journal(get_store())


def account_journal(account_id: str) -> journal.AccountJournal:
    return get_journal().for_account(account_id)


def get_proposals() -> proposals.Proposals:
    """The suggestions for the data showing (core/proposals.py). Demo data keeps its own file
    beside the demo journal, so trying the demo never mixes with real suggestions."""
    if _demo_mode():
        return proposals.Proposals(LocalStore(books.book_data_dir(books.DEFAULT_BOOK)), demo=True)
    return proposals.Proposals(get_store())


def open_suggestion_count() -> int:
    """For the Today badge: open suggestions, read from the file alone. Never raises."""
    try:
        return get_proposals().open_count()
    except Exception as exc:
        _log.warning("Suggestions weren't counted (%s).", type(exc).__name__)
        return 0


_SUGGESTIONS_WAIT = "A connection couldn't load, so suggestions wait until it does."


def refresh_suggestions(
    today: date | None = None, now: datetime | None = None, *, force: bool = False
) -> proposals.RefreshResult | None:
    """Run the daily suggestion check (proposals.refresh) for the data showing, unless it already
    ran today (force runs it anyway). Returns None when it didn't run: already ran, suggestions
    are off, or the book is locked. Raises on failure, so the caller (remember_book's job runner,
    or the Today page) can record it; the Today page shows the suggestions it already has."""
    current = load_settings()
    if not current.suggestions or not vault.ensure_unlocked():
        return None
    day = today if today is not None else date.today()
    moment = now if now is not None else _utc_now()
    store = get_proposals()
    if not force and store.load().last_run == day:
        return None
    connector = get_connector()
    if isinstance(connector, CombinedConnector) and connector.problems:
        # A connection that can't load leaves its accounts out of the book. Suggesting from the
        # rest would expire theirs as "no longer in your book", so wait until it loads again.
        raise ConnectionNotReady(_SUGGESTIONS_WAIT)
    book = get_book(connector, as_of=day)
    latest = latest_book_signals(connector)
    since = {} if _demo_mode() else history.health_since(book, history_store(), day)
    return proposals.refresh(
        store,
        book,
        get_journal().all(),
        latest,
        week_changes(book, day),
        since,
        current,
        day,
        moment,
    )


def due_follow_up_count(today: date | None = None) -> int:
    """For the Today badge on every page: open follow-ups due today or earlier, read from the
    journal file alone (like pending_client_update_count, it never asks a connector, so it may
    count a follow-up on an account that has left the book)."""
    day = today if today is not None else date.today()
    return sum(
        1
        for entry in get_journal().all().values()
        for follow_up in entry.follow_ups
        if follow_up.status is journal.FollowUpStatus.OPEN and follow_up.due <= day
    )


def history_store() -> history.HistoryStore:
    return history.HistoryStore(get_store())


def remember_book(now: datetime | None = None, today: date | None = None) -> bool:
    """Your data: record today's score snapshot, unless one was taken in the last hour; return
    whether one was written. Then the day's follow-up rules and suggestions run if they haven't
    succeeded for the day yet (_daily_steps). Demo data: nothing is recorded or run (its history is
    worked out from the sample readings). Never raises: a failure (a connection that can't load, a
    full disk) is recorded in the run log and logged by its exception type only, because a snapshot
    must never break a page."""
    if _demo_mode():
        return False
    moment = now if now is not None else _utc_now()
    day = today if today is not None else date.today()
    scored: tuple[list[ScoredAccount], list[Signal]] | None = None
    try:
        store = history_store()
        saved = store.load(day)
        if saved is not None and moment - saved.taken_at < history.REWRITE_AFTER:
            written = False
        else:
            with runlog.run("snapshot", day=day):
                connector = get_connector()
                signals = connector.list_signals()
                book = get_book(connector, as_of=day)
                written = store.save(history.snapshot_of(book, signals, day, moment))
                scored = (book, signals)
    except Exception as exc:
        _log.warning("Today's score history wasn't recorded (%s).", type(exc).__name__)
        return False
    _daily_steps(moment, day, scored)
    return written


def _daily_steps(
    moment: datetime,
    day: date,
    scored: tuple[list[ScoredAccount], list[Signal]] | None = None,
) -> None:
    """The follow-up rules, then the suggestions, for `day`, once that day's score history is saved.
    Each runs only while it hasn't succeeded for the day (the run log's last success), so a failed
    run is tried again on the next call; the scheduler calls this on every tick. `scored` is the
    book and its readings when the caller already has them. Nothing runs for demo data, while the
    book is locked, or before the day's score history exists. Never raises."""
    if _demo_mode() or not vault.ensure_unlocked():
        return
    try:
        store = get_store()
        if history_store().load(day) is None:
            return
        settings = load_settings()
        if rules.any_rule_on(settings) and _pending("rules", day, store):
            _run_rules(moment, day, scored)
        if settings.suggestions and _pending("suggestions", day, store):
            _run_suggestions(moment, day)
    except Exception as exc:
        _log.warning(
            "The day's follow-ups and suggestions weren't checked (%s).", type(exc).__name__
        )


_RETRY_AFTER = timedelta(minutes=15)  # a failed daily step waits this long before trying again


def _pending(job: str, day: date, store: LocalStore) -> bool:
    """Whether `job` has not succeeded for `day` yet and didn't fail in the last _RETRY_AFTER (so
    a step that keeps failing is retried every quarter hour, not on every tick)."""
    done = runlog.last_success(job, store=store)
    if done is not None and done.day == day:
        return False
    latest = runlog.last(job, store=store)
    return latest is None or latest.ok or _utc_now() - latest.finished_at >= _RETRY_AFTER


def _run_rules(
    moment: datetime, day: date, scored: tuple[list[ScoredAccount], list[Signal]] | None
) -> None:
    """The automatic follow-up rules for `day`, recorded as the "rules" run. Logged by type on a
    failure, which the next call retries."""
    try:
        with runlog.run("rules", day=day) as job:
            if scored is None:
                connector = get_connector()
                scored = (get_book(connector, as_of=day), connector.list_signals())
            book, signals = scored
            job.counts.update(
                _add_automatic_follow_ups(book, signals, history_store(), day, moment)
            )
    except Exception as exc:
        _log.warning("Automatic follow-ups weren't added (%s).", type(exc).__name__)


def _run_suggestions(moment: datetime, day: date) -> None:
    """The day's suggestions (refresh_suggestions), recorded as the "suggestions" run. Logged by
    type on a failure, which the next call retries."""
    try:
        with runlog.run("suggestions", day=day) as job:
            result = refresh_suggestions(today=day, now=moment)
            if result is not None:
                job.counts.update({"added": result.added, "waiting": result.waiting})
    except Exception as exc:
        _log.warning("Suggestions weren't refreshed (%s).", type(exc).__name__)


def _add_automatic_follow_ups(
    book: list[ScoredAccount],
    signals: list[Signal],
    store: history.HistoryStore,
    day: date,
    moment: datetime,
) -> dict[str, int]:
    """Run the automatic follow-up rules (core/rules.py) for Your data, for `day`. Returns the run
    log's counts: "added", and "held_back" (the follow-ups past rules.MAX_PLANNED, which this run
    leaves for a later one). Raises on failure, so _run_rules can record it."""
    settings = load_settings()
    journal_file = get_journal()
    at_risk = rules.at_risk_since(book, store, day)
    held = rules.held_back(book, journal_file.all(), settings, day, at_risk, signals=signals)
    added = rules.apply_rules(
        journal_file, book, settings, day, at_risk, signals=signals, now=moment
    )
    return {"added": added, "held_back": held}


# The demo book rescored as of a past day: the same for every account and every request on a day
# under the same scoring, so each is worked out once (the account trend needs about 20 of them).
# Keyed by (the demo's day, the day rescored, the scoring config as JSON).
_DEMO_SNAPSHOTS: dict[tuple[date, date, str], history.BookSnapshot] = {}
_DEMO_SNAPSHOTS_MAX = 128


def _demo_rescored(
    demo: DemoConnector, config: ScoringConfig, day: date, now: datetime
) -> history.BookSnapshot:
    key = (demo.today, day, config.model_dump_json())
    snapshot = _DEMO_SNAPSHOTS.get(key)
    if snapshot is None:
        snapshot = history.rescored_snapshot(
            demo.list_accounts(), demo.list_signals(), config, day, now
        )
        if len(_DEMO_SNAPSHOTS) >= _DEMO_SNAPSHOTS_MAX:
            _DEMO_SNAPSHOTS.clear()
        _DEMO_SNAPSHOTS[key] = snapshot
    return snapshot


def _demo_snapshot(day: date, now: datetime) -> history.BookSnapshot:
    return _demo_rescored(DemoConnector(), load_scoring_config(), day, now)


def week_changes(
    book: list[ScoredAccount], today: date | None = None
) -> history.WeekChanges | None:
    """What changed since a week ago: the book compared with the snapshot on or before
    today - WEEK_DAYS. Your data: None until a snapshot that old exists. Demo data: compared with
    the demo book scored as of that day."""
    day = today if today is not None else date.today()
    week_ago = day - timedelta(days=history.WEEK_DAYS)
    if _demo_mode():
        earlier: history.BookSnapshot | None = _demo_snapshot(week_ago, _utc_now())
    else:
        earlier = history_store().on_or_before(week_ago)
    return None if earlier is None else history.changes_since(book, earlier)


class _DemoSnapshots:
    """effects.Snapshots for demo data: the demo book scored as of the day asked for (there is
    a reading for every day: the latest in a range is its last day, the earliest its first)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def latest_between(self, since: date, until: date) -> history.BookSnapshot | None:
        return _demo_snapshot(until, self._now) if since <= until else None

    def earliest_between(self, since: date, until: date) -> history.BookSnapshot | None:
        return _demo_snapshot(since, self._now) if since <= until else None


def step_effects(today: date | None = None, account_id: str | None = None) -> list[effects.Effect]:
    """Did it work? Every step taken on a suggestion for the data showing, with what moved
    since (core/effects.py), newest first; only that account's when account_id is given. Reads
    files only (the suggestions, the journal and the score history), never a connector."""
    day = today if today is not None else date.today()
    snapshots: effects.Snapshots
    if _demo_mode():
        snapshots = _DemoSnapshots(_utc_now())
    else:
        snapshots = history_store()
    return effects.effects_for(
        get_proposals().all(),
        get_journal().all(),
        snapshots,
        day,
        load_scoring_config(),
        account_id,
    )


def review_step(
    proposal_id: str, verdict: proposals.Verdict, check_days: int, now: datetime | None = None
) -> proposals.Proposal:
    """Record the CSM's answer to "Did it work?" (effects.review)."""
    moment = now if now is not None else _utc_now()
    return effects.review(get_proposals(), proposal_id, verdict, check_days, now=moment)


def history_started() -> date | None:
    """The first day with a recorded snapshot (Your data), or None (none yet, or demo data)."""
    return None if _demo_mode() else history_store().first_day()


_DEMO_TREND_STEP = 3  # the demo trend is worked out every 3 days


def account_trend(
    scored: ScoredAccount, rank: int, today: date | None = None
) -> list[history.TrendPoint]:
    """The account's risk over the last TREND_DAYS days, ending with today's point from the book
    now (`scored`, at `rank`). Your data: the recorded snapshots before today. Demo data: the
    demo book scored every few days, starting from its first reading."""
    day = today if today is not None else date.today()
    since = day - timedelta(days=history.TREND_DAYS)
    account_id = scored.account.id
    if _demo_mode():
        demo = DemoConnector()
        signals = demo.list_signals()
        config = load_scoring_config()
        first = min((signal.observed_at.date() for signal in signals), default=day)
        moment = _utc_now()
        snapshots = []
        point = max(since, first)
        while point < day:
            snapshots.append(_demo_rescored(demo, config, point, moment))
            point += timedelta(days=_DEMO_TREND_STEP)
    else:
        snapshots = [snap for snap in history_store().between(since, day) if snap.day < day]
    points = history.trend_points(snapshots, account_id)
    points.append(history.TrendPoint(day=day, risk=scored.risk, health=scored.health, rank=rank))
    return points


def today_view(today: date | None = None) -> journal.TodayView:
    """The Today page: follow-ups due, accounts needing attention, renewals awaiting an outcome,
    follow-ups coming up and snoozed accounts (journal.today_view over the book now)."""
    day = today if today is not None else date.today()
    quiet_days = load_settings().quiet_days
    return journal.today_view(get_book(as_of=day), get_journal().all(), day, quiet_days=quiet_days)


def record_outcome(
    account_id: str, values: journal.OutcomeInput, today: date | None = None
) -> journal.Outcome:
    """Record how the account's renewal went, with its scores and latest signal values now.
    LookupError("That account isn't in your book.") for an unknown id; ValueError for bad
    values (journal's messages)."""
    day = today if today is not None else date.today()
    connector = get_connector()
    scored = find_account(get_book(connector, as_of=day), account_id)
    if scored is None:
        raise LookupError("That account isn't in your book.")
    readings = latest_signals(connector.list_signals(account_id)).get(account_id, {})
    latest = {kind: signal.value for kind, signal in readings.items()}
    return get_journal().record_outcome(
        account_id, values, scored, latest, now=_utc_now(), today=day
    )


# --- The mailbox (see core/mailbox.py: the CSM's own IMAP mailbox, logged to Your data) --------

_MAILBOX_NOT_CONNECTED = "Connect a mailbox first."
_MAILBOX_NO_PASSWORD = "The mailbox password is missing. Connect it again."


def _your_data_accounts() -> list[Account]:
    """The Your data book's accounts, whatever the page shows: the mailbox always matches Your
    data (demo data is never matched or written), so this ignores the demo setting. Built the
    same way as feed_book_accounts."""
    store = get_store()
    _add_legacy_hubspot(store)
    members = [(config, _member(config, store)) for config in store.connections() if config.enabled]
    return CombinedConnector(members, store, today=date.today()).list_accounts()


def mailbox_settings() -> mailbox.MailboxSettings | None:
    """The connected mailbox's saved settings (no password), or None when none is connected."""
    return mailbox.load_settings(get_store())


def connect_mailbox(
    provider: mailbox.Provider, host: str, username: str, aliases: str, password: str
) -> mailbox.MailboxSettings:
    """Connect the CSM's mailbox. The host comes from mailbox.PROVIDER_HOSTS unless the provider
    is OTHER (then host is cleaned). Raises ValueError with the clean_* / validate_secret message
    for a bad field, and mailbox.MailboxError when the login check fails: nothing is saved then.
    On success the password is saved as the mailbox secret, the settings are saved (enabled),
    and one sync runs; its failure is only recorded in the settings. Returns the saved settings."""
    store = get_store()
    chosen = mailbox.Provider(provider)
    server = (
        mailbox.clean_host(host)
        if chosen is mailbox.Provider.OTHER
        else mailbox.PROVIDER_HOSTS[chosen]
    )
    address = mailbox.clean_username(username)
    others = mailbox.clean_aliases(aliases)
    secret = secrets.validate_secret(password)
    candidate = mailbox.MailboxSettings(
        provider=chosen,
        host=server,
        username=address,
        own_addresses=[address, *(other for other in others if other != address)],
        enabled=True,
        connected_at=_utc_now(),
    )
    mailbox.check_login(candidate, secret)
    secrets.save_secret(mailbox.SECRET_NAME, secret)
    mailbox.save_settings(store, candidate)
    try:
        sync_mailbox_now()
    except mailbox.MailboxError:
        pass  # the failure is recorded in the settings (last_error); the page shows it
    return mailbox.load_settings(store) or candidate


def disconnect_mailbox() -> None:
    """Remove the mailbox settings and its password. The activity already logged stays."""
    mailbox.disconnect(get_store())


def set_mailbox_enabled(on: bool) -> None:
    """Pause or resume the scheduled sync. Raises mailbox.MailboxError when none is connected."""
    store = get_store()
    with WRITE_LOCK:
        current = mailbox.load_settings(store)
        if current is None:
            raise mailbox.MailboxError(_MAILBOX_NOT_CONNECTED)
        mailbox.save_settings(store, current.model_copy(update={"enabled": on}))


def sync_mailbox_now() -> mailbox.SyncResult:
    """Sync the mailbox now (Sync now, or a connect, or the scheduler). Matches Your data's
    accounts and their contacts and logs into the Your data journal. Raises
    mailbox.MailboxError when none is connected, the password is missing, or the sync fails (the
    failure is saved in the settings too)."""
    with books.bound(books.active_id()):
        return _sync_mailbox_now()


def _sync_mailbox_now() -> mailbox.SyncResult:
    store = get_store()
    settings = mailbox.load_settings(store)
    if settings is None:
        raise mailbox.MailboxError(_MAILBOX_NOT_CONNECTED)
    password = secrets.get_secret(mailbox.SECRET_NAME)
    if password is None:
        raise mailbox.MailboxError(_MAILBOX_NO_PASSWORD)
    accounts = _your_data_accounts()
    account_contacts = {
        account.id: contacts.contacts_for(store, account.id) for account in accounts
    }
    return mailbox.sync(
        store,
        settings,
        password,
        accounts,
        account_contacts,
        journal.Journal(store, demo=False),
        now=_utc_now(),
    )


def mailbox_due(now: datetime) -> bool:
    """Whether the scheduler should sync the mailbox now: it is connected with its password, it
    is enabled, and it has never synced or its last attempt is SYNC_MINUTES old or more."""
    store = get_store()
    settings = mailbox.load_settings(store)
    if settings is None or not settings.enabled:
        return False
    if secrets.get_secret(mailbox.SECRET_NAME) is None:
        return False
    if settings.last_sync_at is None:
        return True
    return now - settings.last_sync_at >= timedelta(minutes=mailbox.SYNC_MINUTES)


def _sync_mailbox_if_due(now: datetime) -> None:
    """The scheduler's mailbox step (called from sync_due_feeds). Never raises: a failed sync is
    recorded in the settings, and anything else is logged by its exception type only."""
    try:
        due = mailbox_due(now)
    except Exception as exc:
        _log.warning("The mailbox wasn't checked (%s).", type(exc).__name__)
        return
    if not due:
        return
    try:
        with runlog.run("mailbox") as job:
            result = sync_mailbox_now()
            job.counts.update({"read": result.counts.read, "logged": result.counts.logged})
    except Exception as exc:
        _log.warning("The mailbox wasn't synced (%s).", type(exc).__name__)


# --- Growth radar (see core/growth.py) -----------------------------------------------------------


def account_funding(account: Account, now: datetime | None = None) -> growth.FundingNews | None:
    """The newest funding headline in the account's saved website lookup, when one is within
    growth.FUNDING_DAYS of today (as of `now`). None when web research is off or there is no
    saved lookup. Nothing is fetched."""
    moment = now if now is not None else datetime.now(UTC)
    saved = saved_lookup(account, moment)
    if saved is None:
        return None
    return growth.funding_news(saved.articles, moment.date())


def book_funding(now: datetime | None = None) -> dict[str, growth.FundingNews]:
    """account_funding() for every account in the book, keyed by account id. Accounts with no
    funding news are left out, and the result is {} at once when web research is off. Nothing is
    fetched."""
    if not load_settings().web_research:
        return {}
    moment = now if now is not None else datetime.now(UTC)
    found: dict[str, growth.FundingNews] = {}
    for account in get_connector().list_accounts():
        saved = _saved_lookup_for(account, moment)
        if saved is None:
            continue
        news = growth.funding_news(saved.articles, moment.date())
        if news is not None:
            found[account.id] = news
    return found
