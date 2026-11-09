"""Network, research parsing and SSRF hardening: slow or huge replies, UTF-16 feeds, IPv6 forms that
embed an IPv4 address, feed redirects to this computer or a private network, and HubSpot paging.

Offline: every request is an httpx.MockTransport, the guard is given a fake resolver, and the
slow streams sleep for a few milliseconds at most. Each hostile input must finish quickly.
"""

from __future__ import annotations

import gzip
import json
import socket
import time
from collections.abc import Iterator

import httpx
import pytest

from cs_brief.core import research
from cs_brief.core.connectors.hubspot import MAX_PAGES, HubSpotClient, HubSpotError
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds.http import FeedError, FeedHttp, is_internal_url
from cs_brief.core.research import (
    MAX_PARSE_BYTES,
    MAX_TEXT,
    BlockedWebsite,
    GuardedTransport,
    SiteClient,
    SiteUnreachable,
    lookup,
    parse_feed,
    parse_page,
)
from research_fake import FEED, HOME, NOW, FakeSite, account, reply

BASE = "https://acme.example/"
TOKEN = "pat-na1-TESTTOKEN-0000-1111-2222-333344445555"
SLOW_SECONDS = 5.0  # a generous bound: the hostile inputs finish well inside it


class _Slow(httpx.SyncByteStream):
    """A body that trickles `count` one-byte chunks, `pause` seconds apart."""

    def __init__(self, count: int, pause: float) -> None:
        self.count = count
        self.pause = pause
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.count):
            time.sleep(self.pause)
            self.sent += 1
            yield b"x"


class _ThenWait(httpx.SyncByteStream):
    """A complete body that is only finished after `wait` seconds."""

    def __init__(self, body: bytes, wait: float) -> None:
        self.body = body
        self.wait = wait

    def __iter__(self) -> Iterator[bytes]:
        yield self.body
        time.sleep(self.wait)


class _Chunks(httpx.SyncByteStream):
    """A body of `count` one-megabyte chunks, made as they are asked for."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.count):
            self.sent += 1
            yield b"x" * 1_000_000


class _Ticking:
    """Stands in for the time module: each monotonic() call moves the clock on by one tick."""

    def __init__(self) -> None:
        self.ticks = 0

    def monotonic(self) -> float:
        self.ticks += 1
        return float(self.ticks)


def _resolves_to(*addresses: str):
    def resolve(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]

    return resolve


def _serve_feeds(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Answer every ordinary feed request with `handler`; returns the requests seen."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"ok")


# --- N1 + P4: hostile HTML and feeds parse quickly and read a bounded amount ----------------


@pytest.mark.parametrize(
    "page",
    [
        b"<a " * 200_000,
        b"<!--" * 250_000,
        b"</" * 250_000,
        b'<html><head><meta name="description" content="' + b"<a" * 400_000 + b'"></head></html>',
    ],
    ids=["a-tag-run", "comment-run", "close-tag-run", "meta-description-run"],
)
def test_a_hostile_page_finishes_quickly(page: bytes) -> None:
    started = time.perf_counter()
    parsed = parse_page(page, BASE)
    assert time.perf_counter() - started < SLOW_SECONDS
    assert parsed.description is None or len(parsed.description) <= MAX_TEXT


def test_a_hostile_feed_title_finishes_quickly_and_is_cut() -> None:
    feed = b"<rss><channel><item><title>" + b"&lt;a" * 200_000
    feed += b"</title><link>https://acme.example/a</link></item></channel></rss>"
    started = time.perf_counter()
    articles = parse_feed(feed, BASE)
    assert time.perf_counter() - started < SLOW_SECONDS
    assert all(len(article.title) <= MAX_TEXT for article in articles)


def test_only_the_first_512_kilobytes_of_a_page_are_read() -> None:
    late_title = b"<title>Too late</title>"
    page = b"<html>" + b" " * MAX_PARSE_BYTES + late_title
    assert parse_page(page, BASE).title is None
    early = b"<title>Early</title>" + b" " * MAX_PARSE_BYTES
    assert parse_page(early, BASE).title == "Early"


def test_a_page_parse_stops_at_its_deadline_and_keeps_what_it_gathered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research, "PARSE_CHUNK", 64)
    monkeypatch.setattr(research, "PARSE_SECONDS", 3.0)
    monkeypatch.setattr(research, "time", _Ticking())
    late_feed = b'<link rel="alternate" type="application/rss+xml" href="/late.xml">'
    page = b"<html><head><title>Acme</title></head><body>" + b"<p>filler</p>" * 40 + late_feed
    parsed = parse_page(page, BASE)
    assert parsed.title == "Acme"
    assert parsed.feeds == []


def test_a_page_parse_past_its_deadline_from_the_start_is_an_unreadable_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research, "PARSE_SECONDS", 0.0)
    parsed = parse_page(HOME, BASE)
    assert (parsed.title, parsed.description, parsed.feeds, parsed.links) == (None, None, [], [])


def test_a_feed_parse_past_its_deadline_from_the_start_is_not_a_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research, "PARSE_SECONDS", 0.0)
    with pytest.raises(ValueError):
        parse_feed(FEED, BASE)


def test_only_the_first_4000_characters_of_text_are_looked_at() -> None:
    assert research._plain(" " * 4_000 + "<b>tail</b> words") == ""


def test_the_tag_pattern_is_not_fooled_by_openers_without_a_close() -> None:
    # No ">" anywhere, so nothing is a tag: the text is kept, cut to the input limit, and quickly.
    started = time.perf_counter()
    assert research._plain("<a" * 100_000) == "<a" * (research.MAX_INPUT_TEXT // 2)
    assert time.perf_counter() - started < SLOW_SECONDS


# --- N3: a total deadline per request and per lookup ----------------------------------------


def test_a_request_that_trickles_its_body_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "REQUEST_SECONDS", 0.05)
    stream = _Slow(count=10_000, pause=0.01)
    client = SiteClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/html"}, stream=stream
            )
        )
    )
    started = time.perf_counter()
    with pytest.raises(SiteUnreachable, match="took too long to send everything"):
        client.fetch(BASE)
    assert time.perf_counter() - started < SLOW_SECONDS
    assert stream.sent < 100  # stopped early, not after reading the whole stream


def test_no_request_starts_once_the_lookup_has_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "LOOKUP_SECONDS", 0.2)
    site = FakeSite(
        {
            "https://acme.example/": lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=_ThenWait(HOME, wait=0.4),
            ),
        }
    )
    result = lookup(account(), site.client(), NOW)
    assert result.articles == []
    assert site.seen == ["https://acme.example/"]


def test_a_lookup_with_no_time_left_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "LOOKUP_SECONDS", 0.0)
    site = FakeSite({"https://acme.example/": reply(HOME)})
    with pytest.raises(SiteUnreachable, match="took too long"):
        site.client().fetch(BASE)
    assert site.seen == []


def test_the_research_time_limits_have_their_documented_values() -> None:
    assert research.REQUEST_SECONDS == 20
    assert research.LOOKUP_SECONDS == 60
    assert research.PARSE_SECONDS == 2.0


def test_a_feed_request_that_trickles_its_body_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_http, "TOTAL_SECONDS", 0.05)
    stream = _Slow(count=10_000, pause=0.01)
    _serve_feeds(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    started = time.perf_counter()
    with pytest.raises(FeedError) as caught:
        FeedHttp().get("https://exports.example.com/a.csv")
    assert time.perf_counter() - started < SLOW_SECONDS
    assert caught.value.message == "exports.example.com took too long to send everything."
    assert stream.sent < 100


def test_the_feed_total_deadline_covers_the_redirects_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_http, "TOTAL_SECONDS", 0.05)

    def slow_redirect(request: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return httpx.Response(302, headers={"location": "/final"})

    seen = _serve_feeds(monkeypatch, slow_redirect)
    with pytest.raises(FeedError, match="took too long to send everything"):
        FeedHttp().get("https://exports.example.com/start")
    assert len(seen) == 1  # the redirect's target is never requested


# --- N4: a UTF-16 or UTF-32 feed is refused before any XML parsing --------------------------

_EXPANDING_FEED = (
    '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY boom "BOOMBOOMBOOM">]>'
    '<rss version="2.0"><channel><item><title>&boom;</title>'
    "<link>https://acme.example/a</link></item></channel></rss>"
)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_a_utf16_or_utf32_feed_with_a_doctype_is_refused_and_nothing_expands(
    encoding: str,
) -> None:
    with pytest.raises(ValueError, match="only UTF-8"):
        parse_feed(_EXPANDING_FEED.encode(encoding), BASE)


def test_a_utf8_feed_with_a_byte_order_mark_is_still_read() -> None:
    assert [article.title for article in parse_feed(b"\xef\xbb\xbf" + FEED, BASE)] == [
        "Acme names a new CTO",  # newest first
        "Acme raises Series B to hire support",
        "Acme opens an office in Leeds",  # undated, last
    ]


# --- N5: IPv6 forms that embed an IPv4 address are refused ----------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "::7f00:1",  # ::127.0.0.1, IPv4-compatible
        "::a9fe:a9fe",  # ::169.254.169.254, IPv4-compatible
        "::808:808",  # ::8.8.8.8, IPv4-compatible (the whole ::/96 is refused)
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "64:ff9b::808:808",  # NAT64 of a public address: refused whole
        "64:ff9b:1::c0a8:101",  # local-use NAT64
        "2002:c0a8:101::1",  # 6to4 of 192.168.1.1
        "2002:808:808::1",  # 6to4 of a public address: refused whole
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "fec0::1",  # site-local
    ],
)
def test_an_ipv6_form_that_embeds_an_ipv4_address_is_blocked(address: str) -> None:
    with pytest.raises(BlockedWebsite):
        research._check_address("acme.example", 443, _resolves_to(address))


def test_a_public_ipv6_address_is_still_allowed() -> None:
    assert research._check_address("acme.example", 443, _resolves_to("2001:4860:4860::8888")) == (
        "2001:4860:4860::8888"
    )


# --- N2: a feed's redirect cannot steer a request to this computer or a private network -----


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/feed",
        "https://127.0.0.1/feed",
        "https://10.0.0.5/feed",
        "https://[::1]/feed",
        "https://[fd00::1]/feed",
        "https://intranet/feed",  # a single label
        "https://git.internal/feed",
        "https://nas.lan/feed",
        "https://printer.local/feed",
        "https://files.corp/feed",
        "https://wiki.intranet/feed",
        "https://ops.home.arpa/feed",
        "https://FILES.LAN/feed",
    ],
)
def test_a_configured_url_on_an_internal_host_is_internal(url: str) -> None:
    assert is_internal_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://exports.example.com/a.csv",
        "https://93.184.216.34/a.csv",
        "https://[2606:4700:4700::1111]/a.csv",
        "https://lan.example.com/a.csv",
        "https://localhost.example.com/a.csv",
        "https://example.com.lan.evil.test/a.csv",
    ],
)
def test_a_configured_url_on_a_public_host_is_not_internal(url: str) -> None:
    assert is_internal_url(url) is False


def test_a_cross_origin_hop_from_a_public_url_goes_through_the_guarded_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ordinary(request: httpx.Request) -> httpx.Response:
        if request.url.host == "exports.example.com" and request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/second"})
        if request.url.host == "exports.example.com":
            return httpx.Response(302, headers={"location": "https://elsewhere.example.com/x"})
        pytest.fail("the cross-origin hop was sent through the ordinary client")

    guarded: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        guarded.append(request)
        return httpx.Response(200, content=b"ok")

    plain = _serve_feeds(monkeypatch, ordinary)
    monkeypatch.setattr(feed_http, "guarded_transport_factory", lambda: httpx.MockTransport(record))
    assert FeedHttp().get("https://exports.example.com/start").content == b"ok"
    assert [str(request.url) for request in plain] == [
        "https://exports.example.com/start",
        "https://exports.example.com/second",
    ]
    assert [str(request.url) for request in guarded] == ["https://elsewhere.example.com/x"]


def test_a_redirect_to_plain_http_from_a_public_url_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _serve_feeds(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1:2375/x"}),
    )
    guarded: list[httpx.Request] = []
    monkeypatch.setattr(
        feed_http,
        "guarded_transport_factory",
        lambda: httpx.MockTransport(lambda request: guarded.append(request) or _ok(request)),
    )
    with pytest.raises(FeedError) as caught:
        FeedHttp().get("https://exports.example.com/start")
    assert (
        caught.value.message
        == "exports.example.com redirected to an address cs-brief won't follow."
    )
    assert len(plain) == 1
    assert guarded == []


def test_a_cross_origin_redirect_to_a_private_address_is_blocked_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_feeds(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "https://10.0.0.5/admin.csv"}),
    )
    monkeypatch.setattr(
        feed_http,
        "guarded_transport_factory",
        lambda: GuardedTransport(resolve=_resolves_to("10.0.0.5")),
    )
    with pytest.raises(FeedError) as caught:
        FeedHttp(secret="s3cr3t").get("https://exports.example.com/start")
    assert (
        caught.value.message
        == "exports.example.com redirected to an address cs-brief won't follow."
    )


def test_the_real_guarded_backend_refuses_a_redirect_that_resolves_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the real guarded backend end to end: the name resolves to 127.0.0.1 and the hop
    is refused before any connection is made."""
    _serve_feeds(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://elsewhere.example.com/x"}
        ),
    )
    monkeypatch.setattr(
        feed_http,
        "guarded_transport_factory",
        lambda: GuardedTransport(resolve=_resolves_to("127.0.0.1")),
    )
    with pytest.raises(FeedError, match="won't follow"):
        FeedHttp().get("https://exports.example.com/start")


def test_a_redirect_that_does_not_resolve_is_reported_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_feeds(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "https://gone.example.com/x"}),
    )

    def no_such_name(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(
        feed_http, "guarded_transport_factory", lambda: GuardedTransport(resolve=no_such_name)
    )
    with pytest.raises(FeedError, match="Couldn't reach gone.example.com"):
        FeedHttp().get("https://exports.example.com/start")


@pytest.mark.parametrize(
    ("first", "location"),
    [
        ("https://10.0.0.5/export.csv", "https://10.0.0.6/other.csv"),  # internal, another host
        ("http://127.0.0.1:8000/a.csv", "http://127.0.0.1:9000/b.csv"),  # internal, plain http
    ],
)
def test_an_internal_url_keeps_its_redirects_as_before(
    monkeypatch: pytest.MonkeyPatch, first: str, location: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == first:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=b"ok")

    plain = _serve_feeds(monkeypatch, handler)
    guarded: list[httpx.Request] = []
    monkeypatch.setattr(
        feed_http,
        "guarded_transport_factory",
        lambda: httpx.MockTransport(lambda request: guarded.append(request) or _ok(request)),
    )
    assert FeedHttp().get(first).content == b"ok"
    assert [str(request.url) for request in plain] == [first, location]
    assert guarded == []


def test_a_same_origin_hop_keeps_the_ordinary_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"ok")

    plain = _serve_feeds(monkeypatch, handler)
    guarded: list[httpx.Request] = []
    monkeypatch.setattr(
        feed_http,
        "guarded_transport_factory",
        lambda: httpx.MockTransport(lambda request: guarded.append(request) or _ok(request)),
    )
    FeedHttp().get("https://exports.example.com/start")
    assert [request.url.path for request in plain] == ["/start", "/final"]
    assert guarded == []


def test_a_gzip_reply_from_a_feed_host_is_read_as_its_json(monkeypatch: pytest.MonkeyPatch) -> None:
    body = gzip.compress(json.dumps({"ok": True}).encode("utf-8"))
    _serve_feeds(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=body,
        ),
    )
    assert FeedHttp().get_json("https://api.example.com/accounts") == {"ok": True}


# --- HubSpot: a page cap and a body cap, both on MockTransport -------------------------------


def _hubspot(handler) -> HubSpotClient:
    return HubSpotClient(TOKEN, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_a_list_of_exactly_the_page_cap_is_read_in_full() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        more = count < MAX_PAGES
        paging = {"next": {"after": str(count)}} if more else None
        body = {"results": [{"id": str(count)}]} | ({"paging": paging} if paging else {})
        return httpx.Response(200, json=body)

    items = list(_hubspot(handler).paginate("GET", "owners"))
    assert len(items) == MAX_PAGES
    assert count == MAX_PAGES


def test_a_list_longer_than_the_page_cap_stops_with_a_plain_error() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        after = str(len(seen))
        return httpx.Response(
            200, json={"results": [{"id": after}], "paging": {"next": {"after": after}}}
        )

    with pytest.raises(HubSpotError, match="more pages than cs-brief reads"):
        list(_hubspot(handler).paginate("POST", "tickets_search", json={"limit": 100}))
    assert len(seen) == MAX_PAGES  # the page after the cap is never asked for


def test_a_reply_over_the_body_cap_stops_early_with_a_plain_error() -> None:
    stream = _Chunks(count=100)
    client = _hubspot(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=stream
        )
    )
    with pytest.raises(HubSpotError, match="more data than cs-brief reads"):
        client.request("GET", "owners")
    assert stream.sent < 100  # stopped after the cap, not after all 100 megabytes


def test_a_gzip_reply_from_hubspot_is_read_as_its_json() -> None:
    body = gzip.compress(json.dumps({"results": [{"id": "77"}]}).encode("utf-8"))
    client = _hubspot(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=body,
        )
    )
    assert client.request("GET", "owners") == {"results": [{"id": "77"}]}
