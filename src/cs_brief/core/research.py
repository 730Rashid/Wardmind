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

import codecs
import hashlib
import html
import ipaddress
import random
import re
import socket
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from pydantic import BaseModel

from cs_brief.core.config import write_atomic
from cs_brief.core.matching import normalize_domain
from cs_brief.core.models import Account

USER_AGENT = "cs-brief (local customer success tool)"
TIMEOUT_SECONDS = 8.0
MAX_BYTES = 1_000_000  # per response; a longer body is cut off there
MAX_REQUESTS = 8  # per lookup, redirects included
MAX_REDIRECTS = 3  # per fetch
MAX_ARTICLES = 8
MAX_TEXT = 300  # characters kept of a title or description
CACHE_HOURS = 24
# Tried in order, only when neither the home page nor a news page found there has a feed.
FALLBACK_FEEDS = ("/feed", "/rss.xml", "/blog/rss.xml")
# Sections a home page may link to, most useful to a CSM first: company news, then the blog.
NEWS_SECTIONS = ("newsroom", "news", "press", "blog")
# A link on a news page counts as a headline when its text is this long (shorter is navigation).
HEADLINE_CHARS = (25, 200)
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
    # The feed the articles came from, or the news page whose headlines were used when the site
    # has no feed; None if neither was found or readable.
    feed_url: str | None
    articles: list[Article]  # newest first (undated last), at most MAX_ARTICLES
    sample: bool = False  # demo sample news: invented, nothing was sent


class PageInfo(BaseModel):
    title: str | None
    description: str | None
    feeds: list[str]  # absolute https URLs of <link rel="alternate"> RSS/Atom feeds, in order
    links: list[tuple[str, str]] = []  # (text, absolute https URL) of every <a href>, in order


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
    "acme.com" (via matching.normalize_domain). Raises NoWebsite when blank or not a host name
    with a dot in it (so "localhost" is NoWebsite), and BlockedWebsite for an IP address or a
    private-network suffix."""
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

# Whole words or phrases per Topic. Leadership is handled below, because it needs two words.
_TOPIC_WORDS: dict[Topic, tuple[str, ...]] = {
    Topic.ACQUISITION: (
        "acquired",
        "acquires",
        "acquire",
        "acquisition",
        "merger",
        "mergers",
        "merge",
        "merges",
        "merged",
        "takeover",
    ),
    Topic.FUNDING: (
        "raises",
        "raised",
        "funding",
        "investment",
        "investments",
        "IPO",
        *(f"series {letter}" for letter in "abcdef"),
    ),
    Topic.RESTRUCTURING: (
        "layoffs",
        "layoff",
        "lay off",
        "laid off",
        "job cuts",
        "restructuring",
        "reorganisation",
        "reorganization",
    ),
}


def _whole_words(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """One pattern that matches any of the phrases as whole words, ignoring case."""
    alternatives = "|".join(r"\s+".join(map(re.escape, phrase.split())) for phrase in phrases)
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


_TOPIC_PATTERNS: dict[Topic, re.Pattern[str]] = {
    topic: _whole_words(words) for topic, words in _TOPIC_WORDS.items()
}
# Leadership: "new CEO", "appoints" or "steps down" on their own, or a C-level title together
# with a verb such as "names ... as CEO" or "joins as CFO".
_LEADERSHIP_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bnew\s+(?:ceo|cfo|cto|coo|cro)\b", re.IGNORECASE),
    re.compile(r"\bappoints?\b|\bappointed\b", re.IGNORECASE),
    re.compile(r"\bsteps?\s+down\b", re.IGNORECASE),
)
_C_LEVEL_TITLE = re.compile(r"\b(?:ceo|cfo|cto|coo|cro)\b", re.IGNORECASE)
_LEADERSHIP_VERB = re.compile(r"\b(?:names?|named|joins?|hires?|hired)\b", re.IGNORECASE)


def _mentions(topic: Topic, title: str) -> bool:
    if topic is Topic.LEADERSHIP:
        if any(pattern.search(title) for pattern in _LEADERSHIP_PHRASES):
            return True
        return (
            _C_LEVEL_TITLE.search(title) is not None and _LEADERSHIP_VERB.search(title) is not None
        )
    return _TOPIC_PATTERNS[topic].search(title) is not None


def tag_topics(title: str) -> list[Topic]:
    """The topics a headline mentions, in Topic order, each at most once. Whole-word,
    case-insensitive matching, so "Mergers" counts but "emerge" doesn't."""
    return [topic for topic in Topic if _mentions(topic, title)]


_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_SPACE = re.compile(r"\s+")


def _plain(text: str) -> str:
    """Tags removed, entities decoded, whitespace collapsed. Tags go first, so an escaped tag
    in a feed title stays text and is never read as markup."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()


def _clean(text: str | None) -> str | None:
    """_plain(text), cut to MAX_TEXT characters: at a word boundary when there is one, with an
    ellipsis."""
    if text is None:
        return None
    plain = _plain(text)
    if len(plain) <= MAX_TEXT:
        return plain
    cut = plain[: MAX_TEXT - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip() + "…"


def _absolute(href: str, base_url: str, schemes: tuple[str, ...]) -> str | None:
    """`href` resolved against base_url, or None when it is blank, isn't a URL, or its scheme
    isn't one of `schemes` (javascript: and data: are dropped this way)."""
    if not href.strip():
        return None
    try:
        url = urljoin(base_url, href.strip())
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in schemes or not parts.netloc:
        return None
    return url


_FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml"})
# "/newsroom", "/blog/", "/gb/news", "/en-us/press/2026/x": a section, optionally after a locale;
# matched from the start of the path, and the section must be a whole segment.
_SECTION_PATH = re.compile(
    r"/(?:[a-z]{2}(?:[-_][a-z]{2})?/)?(" + "|".join(NEWS_SECTIONS) + r")(?=/|$)", re.IGNORECASE
)
MAX_NEWS_PAGES = 2  # news pages tried per lookup, in NEWS_SECTIONS order


class _PageParser(HTMLParser):
    """Collects what parse_page needs. Nothing is run or fetched."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._title_open = False
        self._title_seen = False
        self._title_text: list[str] = []
        self.title: str | None = None
        self.description: str | None = None
        self.og_description: str | None = None
        self.feeds: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._anchor: str | None = None  # the open <a>'s URL, while its text is collected
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "title" and not self._title_seen:
            self._title_open = True
            self._title_seen = True
        elif tag == "meta":
            self._meta(values)
        elif tag == "link":
            self._link(values)
        elif tag == "a":
            self.end_anchor()  # an unclosed <a> ends where the next one starts
            self._anchor = _absolute(values.get("href", ""), self._base, ("https",))
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.end_title()
        elif tag == "a":
            self.end_anchor()

    def handle_data(self, data: str) -> None:
        if self._title_open:
            self._title_text.append(data)
        if self._anchor is not None:
            self._anchor_text.append(data)

    def end_anchor(self) -> None:
        if self._anchor is not None:
            self.links.append((_clean(" ".join(self._anchor_text)), self._anchor))
            self._anchor = None

    def end_title(self) -> None:
        if self._title_open:
            self._title_open = False
            self.title = _clean("".join(self._title_text)) or None

    def _meta(self, values: dict[str, str]) -> None:
        content = _clean(values.get("content"))
        if not content:
            return
        name = values.get("name", "").strip().lower()
        prop = values.get("property", "").strip().lower()
        if name == "description" and self.description is None:
            self.description = content
        if prop == "og:description" and self.og_description is None:
            self.og_description = content

    def _link(self, values: dict[str, str]) -> None:
        rel = values.get("rel", "").lower().split()
        kind = values.get("type", "").split(";")[0].strip().lower()
        if "alternate" not in rel or kind not in _FEED_TYPES:
            return
        url = _absolute(values.get("href", ""), self._base, ("https",))
        if url is not None and url not in self.feeds:
            self.feeds.append(url)


def parse_page(html: bytes, base_url: str) -> PageInfo:
    """Title, description and advertised feeds from a home page, using html.parser (no scripts
    run, nothing else fetched). Text is unescaped, tag-free, whitespace-collapsed and cut to
    MAX_TEXT. Feed hrefs are resolved against base_url; only https ones are kept, deduplicated.
    Never raises on malformed HTML."""
    parser = _PageParser(base_url)
    try:
        parser.feed(html.decode("utf-8", errors="replace"))
        parser.close()
    except (AssertionError, ValueError):  # html.parser gives up on some broken markup
        pass
    parser.end_title()
    parser.end_anchor()
    return PageInfo(
        title=parser.title,
        description=parser.description or parser.og_description,
        feeds=parser.feeds,
        links=parser.links,
    )


def news_pages(page: PageInfo, hosts: tuple[str, ...]) -> list[str]:
    """The site's own newsroom, news, press and blog index pages that this page links to, in
    NEWS_SECTIONS order, one per section, e.g. ["https://stripe.com/gb/newsroom",
    "https://stripe.com/gb/blog"]. A locale segment such as "/gb/" or "/en-us/" before the
    section is allowed. A deeper link (an article such as "/blog/ai-research") counts as a
    link to its section's index ("/blog")."""
    found: dict[str, str] = {}
    for _, url in page.links:
        if not _same_site(url, hosts):
            continue
        parts = urlsplit(url)
        match = _SECTION_PATH.match(parts.path)
        if match is not None and match.group(1).lower() not in found:
            index = f"{parts.scheme}://{parts.netloc}{match.group(0).rstrip('/')}"
            found[match.group(1).lower()] = index
    return [found[section] for section in NEWS_SECTIONS if section in found]


def page_headlines(page: PageInfo, page_url: str) -> list[Article]:
    """Headlines from a news page that has no feed: links to pages under it (e.g.
    /newsroom/news/<slug> under /newsroom) whose text is HEADLINE_CHARS long, in page order,
    deduplicated by URL, at most MAX_ARTICLES. They have no dates."""
    base = urlsplit(page_url)
    prefix = base.path.rstrip("/") + "/"
    articles: list[Article] = []
    seen: set[str] = set()
    low, high = HEADLINE_CHARS
    for text, url in page.links:
        parts = urlsplit(url)
        url = url.split("#", 1)[0]
        if parts.hostname != base.hostname or not parts.path.startswith(prefix):
            continue
        if not low <= len(text) <= high or url in seen:
            continue
        seen.add(url)
        articles.append(Article(title=text, url=url, published=None, topics=tag_topics(text)))
        if len(articles) == MAX_ARTICLES:
            break
    return articles


_ATOM = "{http://www.w3.org/2005/Atom}"


def _element_text(element: ET.Element | None) -> str:
    return "".join(element.itertext()) if element is not None else ""


def _rfc822_day(text: str) -> date | None:
    """An RSS pubDate such as "Tue, 21 Oct 2026 09:00:00 GMT", as a date."""
    value = text.strip()
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        return None


def _iso_day(text: str) -> date | None:
    """An Atom timestamp such as "2026-10-21T09:00:00Z", as a date."""
    value = text.strip()
    if not value:
        return None
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _atom_href(entry: ET.Element) -> str:
    """The entry's alternate link: a link with rel="alternate" or with no rel at all."""
    for link in entry.findall(f"{_ATOM}link"):
        if link.get("rel", "alternate").strip().lower() == "alternate":
            return link.get("href", "")
    return ""


def _entry(element: ET.Element) -> tuple[str, str, date | None]:
    """(title, link, published) for one RSS item or Atom entry."""
    if element.tag == "item":
        return (
            _element_text(element.find("title")),
            _element_text(element.find("link")),
            _rfc822_day(_element_text(element.find("pubDate"))),
        )
    published = _iso_day(_element_text(element.find(f"{_ATOM}published"))) or _iso_day(
        _element_text(element.find(f"{_ATOM}updated"))
    )
    return (_element_text(element.find(f"{_ATOM}title")), _atom_href(element), published)


def _has_doctype(xml: bytes) -> bool:
    """True when the prolog (before the root element) declares a DOCTYPE. Only the prolog is
    checked: a "<!DOCTYPE" inside an item (escaped or CDATA post HTML) is just text."""
    text = xml.removeprefix(codecs.BOM_UTF8)
    position = 0
    while True:
        while position < len(text) and text[position : position + 1].isspace():
            position += 1
        rest = text[position:]
        if rest.startswith(b"<?"):
            closer = b"?>"
        elif rest.startswith(b"<!--"):
            closer = b"-->"
        else:
            return rest[:9].lower() == b"<!doctype"
        end = rest.find(closer)
        if end < 0:
            return False  # an unclosed prolog: the parser will refuse it
        position += end + len(closer)


_FEED_ROOTS = ("rss", f"{_ATOM}feed")
_ENTRY_TAGS = ("item", f"{_ATOM}entry")


def _entries(xml: bytes) -> list[tuple[str, str, date | None]]:
    """(title, link, published) for each complete RSS item or Atom entry, in document order.
    The body may have been cut off at MAX_BYTES (feeds that carry whole posts are big), so it
    is parsed incrementally and the entries finished before the cut are kept."""
    parser = ET.XMLPullParser(events=("start", "end"))
    root_tag: str | None = None
    entries: list[tuple[str, str, date | None]] = []
    try:
        parser.feed(xml)
        parser.close()
    except ET.ParseError:
        pass  # a cut-off body: the events before the error are still read below
    try:
        # A parse error is queued as an event and raised here, after the good events.
        for event, element in parser.read_events():
            if root_tag is None:
                root_tag = element.tag
                if root_tag not in _FEED_ROOTS:
                    raise ValueError("not an RSS or Atom feed")
            elif event == "end" and element.tag in _ENTRY_TAGS:
                entries.append(_entry(element))
    except ET.ParseError:
        pass
    if root_tag is None:
        raise ValueError("not an XML document")
    return entries


def parse_feed(xml: bytes, base_url: str) -> list[Article]:
    """Articles from an RSS 2.0 or Atom feed, newest first (undated last), at most MAX_ARTICLES,
    deduplicated by URL. Uses xml.etree.ElementTree; a document with a DOCTYPE is refused (no
    entity tricks). Links are resolved against base_url; non-http(s) links drop the item.
    Dates: RSS pubDate (RFC 822) or Atom published/updated (ISO 8601). Raises ValueError when
    the bytes aren't a feed at all; a feed with no usable items gives []. A feed cut off part
    way (at MAX_BYTES) gives the items that were complete."""
    if _has_doctype(xml):
        raise ValueError("feeds with a DOCTYPE are refused")
    entries = _entries(xml)
    articles: list[Article] = []
    seen: set[str] = set()
    for raw_title, raw_link, published in entries:
        title = _clean(raw_title)
        url = _absolute(raw_link, base_url, ("http", "https"))
        if not title or url is None or url in seen:
            continue
        seen.add(url)
        articles.append(
            Article(title=title, url=url, published=published, topics=tag_topics(title))
        )
    dated = sorted(
        (article for article in articles if article.published is not None),
        key=lambda article: article.published or date.min,
        reverse=True,
    )
    undated = [article for article in articles if article.published is None]
    return (dated + undated)[:MAX_ARTICLES]


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def _https_host(url: str) -> str:
    """The host of an https URL on the standard port. SiteUnreachable for anything else."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise SiteUnreachable("That address isn't one cs-brief can read.") from None
    host = parts.hostname or ""
    if parts.scheme != "https" or not host:
        raise SiteUnreachable(f"{host or 'That website'} can only be read over https.")
    if port not in (None, 443):
        raise SiteUnreachable(f"{host} can only be read on the standard https port.")
    return host


def _unreachable(host: str, error: Exception) -> SiteUnreachable:
    if isinstance(error, httpx.TimeoutException):
        return SiteUnreachable(f"{host} didn't answer in time.")
    return SiteUnreachable(f"cs-brief couldn't read {host}. Check the account's website.")


class SiteClient:
    """Fetches pages for one lookup. Fresh cookies per client (none are kept between lookups),
    USER_AGENT, TIMEOUT_SECONDS, MAX_BYTES per body, MAX_REQUESTS per client, redirects
    followed by hand up to MAX_REDIRECTS and only to https URLs. The default transport is
    GuardedTransport(); tests pass an httpx.MockTransport."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            transport=transport if transport is not None else GuardedTransport(),
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
        )
        self._requests = 0

    def fetch(self, url: str) -> Fetched:
        """GET an https URL. Raises SiteUnreachable for a non-https URL, a redirect to one,
        too many redirects, the request budget used up, a non-2xx status, a timeout or any
        transport error. BlockedWebsite from the guard propagates unchanged."""
        target = url
        redirects = 0
        while True:
            host = _https_host(target)
            response = self._send(target, host)
            try:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location or redirects == MAX_REDIRECTS:
                        raise SiteUnreachable(f"{host} redirects in a way cs-brief won't follow.")
                    target = urljoin(target, location)
                    redirects += 1
                    continue  # the next loop checks that the new address is https
                if not 200 <= response.status_code < 300:
                    raise SiteUnreachable(
                        f"{host} answered with an error ({response.status_code})."
                    )
                body = self._read(response, host)
                media_type = response.headers.get("content-type", "").split(";")[0]
                return Fetched(url=target, content_type=media_type.strip().lower(), body=body)
            finally:
                response.close()

    def close(self) -> None:
        self._client.close()

    def _send(self, url: str, host: str) -> httpx.Response:
        """One request, with the body still unread: the caller closes the response."""
        if self._requests >= MAX_REQUESTS:
            raise SiteUnreachable(f"Too many requests to {host} for one look up.")
        self._requests += 1
        self._client.cookies.clear()  # no cookie a site sets is sent back, on any request
        request = self._client.build_request("GET", url)
        try:
            return self._client.send(request, stream=True)
        except (httpx.HTTPError, OSError) as error:
            raise _unreachable(host, error) from error

    def _read(self, response: httpx.Response, host: str) -> bytes:
        body = bytearray()
        try:
            for chunk in response.iter_bytes():
                body.extend(chunk[: MAX_BYTES - len(body)])
                if len(body) >= MAX_BYTES:
                    break
        except (httpx.HTTPError, OSError) as error:
            raise _unreachable(host, error) from error
        return bytes(body)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _same_site(url: str, hosts: tuple[str, ...]) -> bool:
    """True when the URL's host is one of `hosts` or a subdomain of one. A feed on another
    company's host is never fetched: the lookup only talks to the customer's own site."""
    host = urlsplit(url).hostname or ""
    return any(host == known or host.endswith("." + known) for known in hosts)


def lookup(account: Account, client: SiteClient, now: datetime) -> SiteResearch:
    """Visit the account's website and read it.

    1. host = website_host(account.domain); fetch https://{host}/ (SiteUnreachable if it fails
       or isn't HTML).
    2. parse_page; try its advertised feeds (at most 2) until one parses with an article.
    3. Else follow news_pages() (the site's own newsroom, news, press or blog index, at most
       MAX_NEWS_PAGES of them, in order): try each one's
       advertised feeds (at most 2), else take page_headlines() from it.
    4. Else, when no news page was found, try FALLBACK_FEEDS on the final host.
    Feed and news page failures are not errors: the result then has no articles. Returns
    SiteResearch with fetched_at=now, website=host, and feed_url of the feed or news page used.
    Everything fetched must be on the same site (see _same_site). The client is not closed."""
    host = website_host(account.domain)
    home = client.fetch(f"https://{host}/")
    if not _is_html(home):
        raise SiteUnreachable(f"{host} didn't send a web page cs-brief could read.")
    page = parse_page(home.body, home.url)
    final_host = urlsplit(home.url).hostname or host
    hosts = (host, final_host)

    def result(source: str | None, articles: list[Article]) -> SiteResearch:
        return SiteResearch(
            account_id=account.id,
            website=host,
            fetched_at=_aware(now),
            site_title=page.title,
            site_description=page.description,
            feed_url=source,
            articles=articles,
        )

    found = _first_feed(client, _news_feeds(page, hosts), hosts)
    if found is not None:
        return result(*found)
    sections = news_pages(page, hosts)[:MAX_NEWS_PAGES]
    for section in sections:
        try:
            listing = client.fetch(section)
        except ResearchError:
            continue
        if not _is_html(listing) or not _same_site(listing.url, hosts):
            continue
        index = parse_page(listing.body, listing.url)
        found = _first_feed(client, _news_feeds(index, hosts), hosts)
        if found is not None:
            return result(*found)
        headlines = page_headlines(index, listing.url)
        if headlines:
            return result(listing.url, headlines)
    if sections:
        return result(None, [])
    found = _first_feed(client, [urljoin(home.url, path) for path in FALLBACK_FEEDS], hosts)
    return result(*found) if found is not None else result(None, [])


def _news_feeds(page: PageInfo, hosts: tuple[str, ...]) -> list[str]:
    """The page's advertised feeds worth reading: on this site, not a comments feed, at most 2."""
    return [
        url
        for url in page.feeds
        if _same_site(url, hosts) and "comment" not in urlsplit(url).path.lower()
    ][:2]


def _is_html(fetched: Fetched) -> bool:
    return fetched.content_type in ("text/html", "application/xhtml+xml")


def _first_feed(
    client: SiteClient, candidates: list[str], hosts: tuple[str, ...]
) -> tuple[str, list[Article]] | None:
    """(feed URL, articles) for the first candidate on this site that parses with at least one
    article; None if none does. A missing, blocked or unreadable feed just means no feed."""
    for feed_url in candidates:
        try:
            feed = client.fetch(feed_url)
            if not _same_site(feed.url, hosts):
                continue
            articles = parse_feed(feed.body, feed.url)
        except (ResearchError, ValueError):
            continue
        if articles:
            return feed.url, articles
    return None


_SAMPLE_HEADLINES: tuple[str, ...] = (
    "{name} raises a Series B to hire in support",
    "{name} acquires a smaller rival in the same market",
    "{name} appoints a new CFO",
    "{name} names a new COO to lead operations",
    "{name} announces job cuts in its support team",
    "{name} opens a new office in Leeds",
    "{name} launches a customer portal for partners",
    "{name} publishes its annual impact report",
    "{name} wins a regional innovation award",
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _sample_website(account: Account) -> str:
    try:
        return website_host(account.domain)
    except ResearchError:
        return f"{_slug(account.name) or 'example-company'}.example"


def sample_research(account: Account, now: datetime) -> SiteResearch:
    """Demo mode's invented results for a demo account: sample=True, website from the
    account's domain (or "<name>.example"), a site title and description built from the name,
    and 3 or 4 invented articles dated before `now`, deterministic per account id, covering
    some Topics. Nothing is fetched."""
    seed = int.from_bytes(hashlib.sha256(account.id.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    website = _sample_website(account)
    headlines = rng.sample(_SAMPLE_HEADLINES, rng.choice((3, 4)))
    articles = []
    for headline in headlines:
        title = _clean(headline.format(name=account.name)) or ""
        published = (_aware(now) - timedelta(days=rng.randint(5, 80))).date()
        articles.append(
            Article(
                title=title,
                url=f"https://{website}/news/{_slug(title)}",
                published=published,
                topics=tag_topics(title),
            )
        )
    articles.sort(key=lambda article: article.published or date.min, reverse=True)
    name = _clean(account.name) or ""
    return SiteResearch(
        account_id=account.id,
        website=website,
        fetched_at=_aware(now),
        site_title=name or None,
        site_description=(
            f"Sample text for {name}. Demo accounts are invented, so nothing was fetched."
        ),
        feed_url=f"https://{website}/news/feed",
        articles=articles,
        sample=True,
    )


def account_file_name(account_id: str) -> str:
    """The file name for one account's saved data (research, summaries): a hash of the id, so an
    id never becomes part of a path."""
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:32] + ".json"


class ResearchCache:
    """Saved lookups under `root` (service passes store.data_dir() / RESEARCH_DIR), one JSON
    file per account, named by a hash of the account id so ids never become paths."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, account_id: str) -> Path:
        return self._root / account_file_name(account_id)

    def saved(self, account_id: str) -> SiteResearch | None:
        """The saved lookup whatever its age, or None if there isn't one or the file can't be
        read. Used for what the book shows from earlier lookups; `get` is the freshness check."""
        try:
            saved = SiteResearch.model_validate_json(
                self._path(account_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):  # a bad JSON or a bad shape is a ValueError too
            return None
        return saved if saved.account_id == account_id else None

    def get(self, account_id: str, website: str, now: datetime) -> SiteResearch | None:
        """The saved lookup, or None if there isn't one, it is older than CACHE_HOURS, it was
        for a different website, or the file can't be read."""
        saved = self.saved(account_id)
        if saved is None or saved.website != website:
            return None
        age = _aware(now) - _aware(saved.fetched_at)
        if age < timedelta(0) or age >= timedelta(hours=CACHE_HOURS):
            return None
        return saved

    def put(self, result: SiteResearch) -> None:
        """Save atomically (config.write_atomic)."""
        write_atomic(self._path(result.account_id), result.model_dump_json(indent=2) + "\n")

    def clear(self) -> int:
        """Delete every saved lookup; return how many were removed. A missing folder is 0."""
        if not self._root.is_dir():
            return 0
        removed = 0
        for path in self._root.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed
