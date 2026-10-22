"""Optional web research: what one customer says about itself on its own website.

Privacy rules (PRIVACY.md and the Data & privacy page repeat them in plain English):
- Off unless Settings.web_research is on. Nothing here is called while it is off.
- Only when the CSM asks, for one account: never in the background, never for the whole book.
- The only place it goes is that account's own website (Account.domain). No search engine or
  other company is involved, and nothing about the account (ARR, owner, health, signals, notes,
  other accounts) is sent: the request is an ordinary visit, with no cookies kept.
- It reads the home page's title and description and the news or blog feed the site publishes.
- Results are saved on this computer for CACHE_HOURS; Clear local cache removes them.
- Demo accounts are invented: in demo mode, sample_research() returns built-in sample news and
  nothing is sent.

Safety: the website comes from imported data, so it is untrusted. website_host() refuses
anything that isn't a public host name, and the guarded transport refuses to connect to an
address on this computer or a private network, checked at connect time for every request
(redirects included), so DNS can't be used to point a lookup inward. Proxies from the
environment are ignored, because a proxy would make that check meaningless.

CONTRACT FILE: models, exceptions and signatures are fixed; implementers escalate before
changing them. The guard section (website_host, _check_address, _GuardedBackend,
GuardedTransport) is security code: do not change it without escalating.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpcore
import httpx
from pydantic import BaseModel

from cs_brief.core.matching import normalize_domain
from cs_brief.core.models import Account

USER_AGENT = "cs-brief (local customer success tool)"
TIMEOUT_SECONDS = 8.0
MAX_BYTES = 1_000_000  # per response; a longer body is cut off there
MAX_REQUESTS = 6  # per lookup, redirects included
MAX_REDIRECTS = 3  # per fetch
MAX_ARTICLES = 8
MAX_TEXT = 300  # characters kept of a title or description
CACHE_HOURS = 24
# Tried in order, only when the home page advertises no feed.
FALLBACK_FEEDS = ("/feed", "/rss.xml", "/blog/rss.xml")
RESEARCH_DIR = "research"  # under store.data_dir()

# Hosts that are never public websites, whatever DNS says.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".corp", ".intranet")


class Topic(StrEnum):
    """What a headline is about, when it matters to a CSM. Matched on words in the title."""

    ACQUISITION = "acquisition"  # acquired, acquires, acquisition, merger, merge, takeover
    FUNDING = "funding"  # raises, funding, Series A-F, investment, IPO
    LEADERSHIP = "leadership"  # new CEO/CFO/CTO/COO/CRO, appoints, names ... as, steps down
    RESTRUCTURING = "restructuring"  # layoffs, job cuts, restructuring, reorganisation

    @property
    def label(self) -> str:
        return {
            "acquisition": "Acquisition or merger",
            "funding": "Funding",
            "leadership": "Leadership change",
            "restructuring": "Restructuring",
        }[self.value]


class Article(BaseModel):
    title: str  # plain text, whitespace collapsed, at most MAX_TEXT characters
    url: str  # absolute http(s) URL; items whose link isn't http(s) are dropped
    published: date | None  # None when the feed doesn't say, or says something unreadable
    topics: list[Topic] = []  # tag_topics(title), in Topic order


class SiteResearch(BaseModel):
    account_id: str
    website: str  # the host visited, e.g. "acme.com": the only thing the lookup "sends"
    fetched_at: datetime  # timezone-aware UTC
    site_title: str | None  # the home page's <title>, plain text, None if missing
    site_description: str | None  # meta description, else og:description, else None
    feed_url: str | None  # the feed the articles came from; None if none was found or readable
    articles: list[Article]  # newest first (undated last), at most MAX_ARTICLES
    sample: bool = False  # demo sample news: invented, nothing was sent


class PageInfo(BaseModel):
    title: str | None
    description: str | None
    feeds: list[str]  # absolute https URLs of <link rel="alternate"> RSS/Atom feeds, in order


class Fetched(BaseModel):
    url: str  # the final URL, after redirects
    content_type: str  # lowercased media type without parameters, e.g. "text/html"
    body: bytes  # at most MAX_BYTES


class ResearchError(Exception):
    """A lookup that can't be done. `message` is plain English for the page."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResearchOff(ResearchError):
    """Web research is turned off in Settings > Data & privacy."""


class NoWebsite(ResearchError):
    """The account has no website, so there is nowhere to look."""


class BlockedWebsite(ResearchError):
    """The website is not a public host (an IP address, localhost, a private network name, or
    a name that resolves to this computer or a private network). cs-brief won't visit it."""


class SiteUnreachable(ResearchError):
    """The home page couldn't be read: timeout, TLS error, HTTP error, not https, too many
    redirects, not HTML. A missing feed is not this: the lookup then has no articles."""


_BLOCKED_MESSAGE = (
    "That website points at this computer or a private network, so cs-brief won't visit it."
)


# --- Guard: security code, see the module docstring ---------------------------------------


def website_host(domain: str | None) -> str:
    """The host to visit for an account's website field, e.g. "https://www.Acme.com/x" gives
    "acme.com" (via matching.normalize_domain). Raises NoWebsite when blank or not a host name,
    and BlockedWebsite for an IP address, "localhost", or a private-network suffix."""
    raw = (domain or "").strip()
    if not raw:
        raise NoWebsite("This account has no website. Add one to look it up.")
    host = normalize_domain(raw)
    if host is None:
        raise NoWebsite(f"“{raw}” isn't a website address. Edit the account to fix it.")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise BlockedWebsite(_BLOCKED_MESSAGE)
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise BlockedWebsite(_BLOCKED_MESSAGE)
    return host


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


Resolver = Callable[..., list[tuple[Any, ...]]]


def _check_address(host: str, port: int, resolve: Resolver) -> str:
    """The first address `host` resolves to, if every address it resolves to is public.
    Raises BlockedWebsite otherwise, or SiteUnreachable if it doesn't resolve."""
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise SiteUnreachable(f"Couldn't find {host}. Check the account's website.") from error
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or not all(_is_public(address) for address in addresses):
        raise BlockedWebsite(_BLOCKED_MESSAGE)
    return addresses[0]


class _GuardedBackend(httpcore.SyncBackend):
    """Resolves the host itself and connects to the checked address. TLS still verifies the
    certificate against the host name, because httpcore passes it to start_tls separately."""

    def __init__(self, resolve: Resolver = socket.getaddrinfo) -> None:
        self._resolve = resolve

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        address = _check_address(host, port, self._resolve)
        return super().connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class GuardedTransport(httpx.HTTPTransport):
    """An httpx transport that never connects to this computer or a private network, and
    ignores proxy settings from the environment."""

    def __init__(self, resolve: Resolver = socket.getaddrinfo) -> None:
        super().__init__(trust_env=False)
        ssl_context = httpx.create_ssl_context(trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=4,
            network_backend=_GuardedBackend(resolve),
        )


# --- Reading pages and feeds -------------------------------------------------------------


def tag_topics(title: str) -> list[Topic]:
    """The topics a headline mentions, in Topic order, each at most once. Whole-word,
    case-insensitive matching, so "Mergers" counts but "emerge" doesn't."""
    raise NotImplementedError


def parse_page(html: bytes, base_url: str) -> PageInfo:
    """Title, description and advertised feeds from a home page, using html.parser (no scripts
    run, nothing else fetched). Text is unescaped, tag-free, whitespace-collapsed and cut to
    MAX_TEXT. Feed hrefs are resolved against base_url; only https ones are kept, deduplicated.
    Never raises on malformed HTML."""
    raise NotImplementedError


def parse_feed(xml: bytes, base_url: str) -> list[Article]:
    """Articles from an RSS 2.0 or Atom feed, newest first (undated last), at most MAX_ARTICLES,
    deduplicated by URL. Uses xml.etree.ElementTree; a document with a DOCTYPE is refused (no
    entity tricks). Links are resolved against base_url; non-http(s) links drop the item.
    Dates: RSS pubDate (RFC 822) or Atom published/updated (ISO 8601). Raises ValueError when
    the bytes aren't a feed at all; a feed with no usable items gives []."""
    raise NotImplementedError


class SiteClient:
    """Fetches pages for one lookup. Fresh cookies per client (none are kept between lookups),
    USER_AGENT, TIMEOUT_SECONDS, MAX_BYTES per body, MAX_REQUESTS per client, redirects
    followed by hand up to MAX_REDIRECTS and only to https URLs. The default transport is
    GuardedTransport(); tests pass an httpx.MockTransport."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        raise NotImplementedError

    def fetch(self, url: str) -> Fetched:
        """GET an https URL. Raises SiteUnreachable for a non-https URL, a redirect to one,
        too many redirects, the request budget used up, a non-2xx status, a timeout or any
        transport error. BlockedWebsite from the guard propagates unchanged."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def lookup(account: Account, client: SiteClient, now: datetime) -> SiteResearch:
    """Visit the account's website and read it.

    1. host = website_host(account.domain); fetch https://{host}/ (SiteUnreachable if it fails
       or isn't HTML).
    2. parse_page; then try its advertised feeds (at most 2), or else FALLBACK_FEEDS on the
       final host, until one parses with at least one article. Feed failures are not errors.
    3. Return SiteResearch with fetched_at=now, website=host, feed_url of the feed used.
    The client is not closed here."""
    raise NotImplementedError


def sample_research(account: Account, now: datetime) -> SiteResearch:
    """Demo mode's invented results for a demo account: sample=True, website from the
    account's domain (or "<name>.example"), a site title and description built from the name,
    and 3 or 4 invented articles dated before `now`, deterministic per account id, covering
    some Topics. Nothing is fetched."""
    raise NotImplementedError


class ResearchCache:
    """Saved lookups under `root` (service passes store.data_dir() / RESEARCH_DIR), one JSON
    file per account, named by a hash of the account id so ids never become paths."""

    def __init__(self, root: Path) -> None:
        raise NotImplementedError

    def get(self, account_id: str, website: str, now: datetime) -> SiteResearch | None:
        """The saved lookup, or None if there isn't one, it is older than CACHE_HOURS, it was
        for a different website, or the file can't be read."""
        raise NotImplementedError

    def put(self, result: SiteResearch) -> None:
        """Save atomically (config.write_atomic)."""
        raise NotImplementedError

    def clear(self) -> int:
        """Delete every saved lookup; return how many were removed. A missing folder is 0."""
        raise NotImplementedError
