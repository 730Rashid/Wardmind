"""Fake customer websites for the web research tests.

Each fake site is an httpx MockTransport: a dict of full URLs to replies. Nothing here opens a
connection, and a test can read every URL the lookup asked for in `seen`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx

from wardmind.core import research
from wardmind.core.models import Account, Milestones

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

HOME = b"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Acme Corporation | Home</title>
<meta name="description" content="Acme builds freight software for regional carriers.">
<link rel="alternate" type="application/rss+xml" title="News" href="/news/feed.xml">
</head><body><p>Welcome to Acme.</p></body></html>"""

FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Acme news</title>
<item><title>Acme raises Series B to hire support</title>
  <link>https://acme.example/news/series-b</link>
  <pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Acme names a new CTO</title>
  <link>/news/new-cto</link>
  <pubDate>Thu, 15 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Acme opens an office in Leeds</title>
  <link>https://acme.example/news/leeds</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Acme blog</title>
  <entry>
    <title>Acme acquires Beta Logistics</title>
    <link rel="alternate" href="https://acme.example/blog/beta"/>
    <updated>2026-10-12T08:30:00Z</updated>
  </entry>
  <entry>
    <title>Acme wins an award</title>
    <link href="https://acme.example/blog/award"/>
    <published>2026-10-01T10:00:00+00:00</published>
  </entry>
</feed>"""

NO_FEED_HOME = b"""<!doctype html><html><head><title>Plain page</title></head>
<body><p>Nothing to see.</p></body></html>"""

Route = tuple[int, dict[str, str], bytes] | Callable[[httpx.Request], httpx.Response]


def reply(
    body: bytes = b"",
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> Route:
    """A route that answers with this status, body and headers."""
    return (status, {"content-type": content_type, **(headers or {})}, body)


class FakeSite:
    """Answers each full URL from `routes`; anything else is a 404. Every URL asked for is kept
    in `seen`, in order."""

    def __init__(self, routes: dict[str, Route] | None = None) -> None:
        self.routes: dict[str, Route] = dict(routes or {})
        self.seen: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"not found", headers={"content-type": "text/plain"})
        if callable(route):
            return route(request)
        status, headers, body = route
        return httpx.Response(status, headers=headers, content=body)

    def client(self) -> research.SiteClient:
        return research.SiteClient(transport=httpx.MockTransport(self.handle))


def acme_site(**overrides: Route) -> FakeSite:
    """A site whose home page and both feed locations are readable, unless overridden by URL."""
    routes: dict[str, Route] = {
        "https://acme.example/": reply(HOME),
        "https://acme.example/news/feed.xml": reply(FEED, content_type="application/rss+xml"),
    }
    routes.update(overrides)
    return FakeSite(routes)


def account(
    domain: str | None = "acme.example",
    *,
    account_id: str = "acc-test",
    name: str = "Acme Corporation",
) -> Account:
    return Account(
        id=account_id,
        name=name,
        arr=120_000,
        tier="Growth",
        owner="Sam Lee",
        renewal_date=date(2027, 1, 10),
        milestones=Milestones(done=0, total=0),
        domain=domain,
    )


def raising(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """A route whose request fails with this exception, as a timeout or a blocked host would."""

    def route(request: httpx.Request) -> httpx.Response:
        raise error

    return route


def counted(factory: Any) -> tuple[list[int], Callable[[], Any]]:
    """A call counter around a factory: the list grows by one for every call."""
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        return factory()

    return calls, call
