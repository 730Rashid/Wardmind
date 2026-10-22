"""Web research, core only: topics, page and feed parsing, the site client, the address guard,
lookups, the invented sample and the saved-lookup cache. Every test is offline: sites are
httpx MockTransports (tests/research_fake.py) and the guard is given a fake resolver."""

from __future__ import annotations

import socket
from datetime import datetime, timedelta

import httpx
import pytest

from cs_brief.core import research
from cs_brief.core.research import (
    CACHE_HOURS,
    FALLBACK_FEEDS,
    MAX_ARTICLES,
    MAX_BYTES,
    MAX_REDIRECTS,
    MAX_REQUESTS,
    MAX_TEXT,
    BlockedWebsite,
    GuardedTransport,
    NoWebsite,
    ResearchCache,
    ResearchError,
    SiteClient,
    SiteResearch,
    SiteUnreachable,
    Topic,
    lookup,
    parse_feed,
    parse_page,
    sample_research,
    tag_topics,
    website_host,
)
from research_fake import (
    ATOM,
    FEED,
    HOME,
    NO_FEED_HOME,
    NOW,
    FakeSite,
    account,
    acme_site,
    raising,
    reply,
)

BASE = "https://acme.example/"


def _rss(*items: str) -> bytes:
    body = "".join(items)
    head = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
    return f"{head}{body}</channel></rss>".encode()


def _item(title: str, link: str = "https://acme.example/news/x", pub: str | None = None) -> str:
    date_part = f"<pubDate>{pub}</pubDate>" if pub else ""
    return f"<item><title>{title}</title><link>{link}</link>{date_part}</item>"


# --- Topics ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "topics"),
    [
        ("Acme acquired Beta Logistics", [Topic.ACQUISITION]),
        ("Acme to acquire a rival", [Topic.ACQUISITION]),
        ("Acme in merger talks", [Topic.ACQUISITION]),
        ("Mergers reshape the market", [Topic.ACQUISITION]),
        ("Acme takeover completes", [Topic.ACQUISITION]),
        ("Acme raises $20M", [Topic.FUNDING]),
        ("Acme closes its Series A", [Topic.FUNDING]),
        ("Acme leads a series f round", [Topic.FUNDING]),
        ("Acme plans an IPO next year", [Topic.FUNDING]),
        ("Acme appoints a new CFO", [Topic.LEADERSHIP]),
        ("Jane Doe steps down as CEO", [Topic.LEADERSHIP]),
        ("Acme names Jane Doe as CTO", [Topic.LEADERSHIP]),
        ("Acme hires a COO", [Topic.LEADERSHIP]),
        ("Acme job cuts in support", [Topic.RESTRUCTURING]),
        ("Layoffs hit Acme's sales team", [Topic.RESTRUCTURING]),
        ("Acme announces a reorganization", [Topic.RESTRUCTURING]),
        ("Acme opens a new office in Leeds", []),
        ("Startups emerge from stealth", []),
        ("Series Apartments open downtown", []),
        ("Acme names its best place to work", []),
        ("Our CEO speaks at a conference", []),
    ],
)
def test_tag_topics_matches_whole_words_only(title: str, topics: list[Topic]) -> None:
    assert tag_topics(title) == topics


def test_tag_topics_is_case_insensitive() -> None:
    assert tag_topics("ACME ACQUIRED BETA") == [Topic.ACQUISITION]


def test_tag_topics_lists_each_topic_once_in_topic_order() -> None:
    title = "Acme raises funding after layoffs, and names a new CTO"
    assert tag_topics(title) == [Topic.FUNDING, Topic.LEADERSHIP, Topic.RESTRUCTURING]


def test_every_topic_has_a_label() -> None:
    assert {topic.label for topic in Topic} == {
        "Acquisition or merger",
        "Funding",
        "Leadership change",
        "Restructuring",
    }


# --- Home pages -----------------------------------------------------------------------------


def test_parse_page_reads_title_and_meta_description() -> None:
    page = parse_page(HOME, BASE)
    assert page.title == "Acme Corporation | Home"
    assert page.description == "Acme builds freight software for regional carriers."
    assert page.feeds == ["https://acme.example/news/feed.xml"]


def test_parse_page_falls_back_to_og_description() -> None:
    html = b'<title>T</title><meta property="og:description" content="From Open Graph">'
    assert parse_page(html, BASE).description == "From Open Graph"


def test_parse_page_prefers_meta_description_over_og() -> None:
    html = b'<meta property="og:description" content="OG"><meta name="description" content="Meta">'
    assert parse_page(html, BASE).description == "Meta"


def test_parse_page_skips_an_empty_meta_description_for_the_og_one() -> None:
    html = b'<meta name="description" content="  "><meta property="og:description" content="OG">'
    assert parse_page(html, BASE).description == "OG"


def test_parse_page_finds_feed_links_with_several_rel_tokens_and_any_case() -> None:
    html = b"""
      <link rel="alternate nofollow" type="Application/RSS+XML" href="/a.xml">
      <link rel="ALTERNATE" type="application/atom+xml; charset=utf-8" href="https://acme.example/b.xml">
      <link rel="stylesheet" type="text/css" href="/style.css">
      <link rel="alternate" type="text/html" href="/page">
    """
    assert parse_page(html, BASE).feeds == [
        "https://acme.example/a.xml",
        "https://acme.example/b.xml",
    ]


def test_parse_page_keeps_only_https_feeds_and_dedupes_them_in_order() -> None:
    html = b"""
      <link rel="alternate" type="application/rss+xml" href="http://acme.example/plain.xml">
      <link rel="alternate" type="application/rss+xml" href="javascript:alert(1)">
      <link rel="alternate" type="application/rss+xml" href="data:text/xml,boo">
      <link rel="alternate" type="application/rss+xml" href="/news/a.xml">
      <link rel="alternate" type="application/rss+xml" href="/news/a.xml">
      <link rel="alternate" type="application/rss+xml" href="//feeds.example.net/c.xml">
    """
    assert parse_page(html, BASE).feeds == [
        "https://acme.example/news/a.xml",
        "https://feeds.example.net/c.xml",
    ]


def test_parse_page_resolves_relative_feed_links_against_the_base() -> None:
    html = b'<link rel="alternate" type="application/rss+xml" href="blog/feed">'
    assert parse_page(html, "https://acme.example/en/").feeds == [
        "https://acme.example/en/blog/feed"
    ]


def test_parse_page_unescapes_entities_and_collapses_whitespace() -> None:
    html = b"<title>  Tom &amp; Jerry&#39;s\n\n  shop </title>"
    assert parse_page(html, BASE).title == "Tom & Jerry's shop"


def test_parse_page_strips_tags_inside_the_title() -> None:
    assert parse_page(b"<title>Acme <b>Home</b></title>", BASE).title == "Acme Home"


def test_parse_page_cuts_long_text_at_a_word_with_an_ellipsis() -> None:
    description = "word " * 200
    html = f'<meta name="description" content="{description}">'.encode()
    text = parse_page(html.strip(), BASE).description
    assert text is not None
    assert len(text) <= MAX_TEXT
    assert text.endswith("…")
    assert not text[:-1].endswith(" ")


def test_parse_page_survives_malformed_markup() -> None:
    page = parse_page(b"<html><head><title>Broken <<<>>> <meta name=", BASE)
    assert page.title is not None
    assert page.title.startswith("Broken")
    assert parse_page(b"<<<</>>>&&&;;;", BASE).title is None


def test_parse_page_never_raises_on_bad_bytes() -> None:
    page = parse_page(b"<title>\xff\xfe bad \xc3</title>", BASE)
    assert page.title is not None


def test_parse_page_without_a_title_or_description_gives_none() -> None:
    page = parse_page(NO_FEED_HOME, BASE)
    assert page.title == "Plain page"
    assert page.description is None
    assert parse_page(b"<p>no head</p>", BASE).title is None


# --- Feeds ----------------------------------------------------------------------------------


def test_rss_items_are_newest_first_with_undated_last() -> None:
    feed = _rss(
        _item("Old", "https://acme.example/news/old", "Mon, 05 Oct 2026 09:00:00 GMT"),
        _item("No date", "https://acme.example/news/nodate"),
        _item("New", "https://acme.example/news/new", "Thu, 15 Oct 2026 09:00:00 GMT"),
    )
    articles = parse_feed(feed, BASE)
    assert [a.title for a in articles] == ["New", "Old", "No date"]
    assert articles[0].published is not None and articles[0].published.isoformat() == "2026-10-15"
    assert articles[2].published is None


def test_rss_relative_links_resolve_against_the_base() -> None:
    feed = _rss(_item("Relative", "/news/relative"))
    assert parse_feed(feed, "https://acme.example/news/feed.xml")[0].url == (
        "https://acme.example/news/relative"
    )


@pytest.mark.parametrize(
    "link",
    ["javascript:alert(1)", "data:text/html,hello", "ftp://acme.example/x", "mailto:a@b.example"],
)
def test_items_with_a_non_web_link_are_dropped(link: str) -> None:
    feed = _rss(_item("Bad link", link), _item("Good", "https://acme.example/news/good"))
    assert [a.title for a in parse_feed(feed, BASE)] == ["Good"]


def test_items_without_a_title_are_dropped() -> None:
    feed = _rss(_item("", "https://acme.example/news/a"), _item("  ", "https://acme.example/b"))
    assert parse_feed(feed, BASE) == []


def test_duplicate_urls_are_kept_once() -> None:
    feed = _rss(
        _item("First", "https://acme.example/news/same"),
        _item("Second", "https://acme.example/news/same"),
    )
    assert [a.title for a in parse_feed(feed, BASE)] == ["First"]


def test_only_the_newest_articles_are_kept() -> None:
    items = [
        _item(
            f"Story {day}", f"https://acme.example/news/{day}", f"{day:02d} Oct 2026 09:00:00 GMT"
        )
        for day in range(1, 13)
    ]
    articles = parse_feed(_rss(*items), BASE)
    assert len(articles) == MAX_ARTICLES
    assert articles[0].title == "Story 12"
    assert articles[-1].title == f"Story {13 - MAX_ARTICLES}"


def test_an_unreadable_pubdate_leaves_the_article_undated() -> None:
    feed = _rss(_item("Odd date", "https://acme.example/news/odd", "next Tuesday-ish"))
    assert parse_feed(feed, BASE)[0].published is None


def test_rss_items_get_their_topics() -> None:
    feed = _rss(_item("Acme raises Series B", "https://acme.example/news/b"))
    assert parse_feed(feed, BASE)[0].topics == [Topic.FUNDING]


def test_an_empty_channel_gives_no_articles() -> None:
    assert parse_feed(_rss(), BASE) == []


def test_atom_entries_use_published_then_updated_and_the_alternate_link() -> None:
    articles = parse_feed(ATOM, "https://acme.example/blog/")
    assert [a.title for a in articles] == ["Acme acquires Beta Logistics", "Acme wins an award"]
    assert articles[0].url == "https://acme.example/blog/beta"
    assert articles[0].published is not None and articles[0].published.isoformat() == "2026-10-12"
    assert articles[0].topics == [Topic.ACQUISITION]
    assert articles[1].url == "https://acme.example/blog/award"


def test_atom_entries_with_only_a_self_link_are_dropped() -> None:
    feed = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
      <entry><title>Only self</title><link rel="self" href="https://acme.example/self"/></entry>
    </feed>"""
    assert parse_feed(feed, BASE) == []


def test_a_document_with_a_doctype_is_refused_before_parsing() -> None:
    feed = b'<!DOCTYPE rss [<!ENTITY boom "x">]><rss version="2.0"><channel/></rss>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_feed(feed, BASE)
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_feed(b"<!doctype html><rss/>", BASE)


@pytest.mark.parametrize(
    "prolog",
    [
        b'<?xml version="1.0"?>\n<!-- generated --><!DOCTYPE rss [<!ENTITY boom "x">]>',
        b"\xef\xbb\xbf<!DOCTYPE rss>",
        b'<?xml version="1.0"?><?xml-stylesheet href="s.xsl"?>\n  <!DOCTYPE rss>',
    ],
)
def test_a_doctype_after_a_declaration_comment_or_bom_is_still_refused(prolog: bytes) -> None:
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_feed(prolog + b'<rss version="2.0"><channel/></rss>', BASE)


def test_a_doctype_inside_an_items_html_is_just_text() -> None:
    # Feeds that carry whole posts embed their HTML, doctype and all (github.blog does).
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Post</title><link>https://acme.example/post</link>
        <description><![CDATA[<!DOCTYPE html><p>Body</p>]]></description></item>
      <item><title>Other</title><link>https://acme.example/other</link>
        <description>&lt;!DOCTYPE html&gt;</description></item>
    </channel></rss>"""
    assert [article.title for article in parse_feed(feed, BASE)] == ["Post", "Other"]


def test_a_feed_cut_off_at_the_size_limit_keeps_its_complete_items() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>First</title><link>https://acme.example/1</link></item>
      <item><title>Second</title><link>https://acme.example/2</link></item>
      <item><title>Third, cut</title><link>https://acme.example/3</li"""
    assert [article.title for article in parse_feed(feed, BASE)] == ["First", "Second"]


def test_a_feed_cut_off_before_any_item_ends_has_no_articles() -> None:
    assert parse_feed(b'<rss version="2.0"><channel><item><title>Half', BASE) == []


@pytest.mark.parametrize(
    "document",
    [
        b"<html><body>Not a feed</body></html>",
        b"<rdf:RDF xmlns:rdf='x'/>",
        b"not xml at all <<",
        b"",
    ],
)
def test_something_that_is_not_a_feed_is_a_value_error(document: bytes) -> None:
    with pytest.raises(ValueError):
        parse_feed(document, BASE)


# --- The site client ------------------------------------------------------------------------


def test_requests_send_the_user_agent_and_no_cookie_from_an_earlier_reply() -> None:
    seen_cookies: list[str | None] = []
    seen_agents: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        seen_agents.append(request.headers.get("user-agent"))
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "set-cookie": "session=abc; Path=/"},
            content=b"<title>x</title>",
        )

    client = SiteClient(transport=httpx.MockTransport(handler))
    client.fetch("https://acme.example/")
    client.fetch("https://acme.example/news/feed.xml")
    client.close()
    assert seen_cookies == [None, None]
    assert seen_agents == [research.USER_AGENT, research.USER_AGENT]


def test_a_new_client_does_not_send_cookies_from_an_old_one() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "a=1", "content-type": "text/html"})

    first = SiteClient(transport=httpx.MockTransport(handler))
    first.fetch("https://acme.example/")
    first.close()
    second = SiteClient(transport=httpx.MockTransport(handler))
    second.fetch("https://acme.example/")
    second.close()
    assert seen == [None, None]


def test_a_plain_http_url_is_refused_without_a_request() -> None:
    site = FakeSite()
    client = site.client()
    with pytest.raises(SiteUnreachable, match="acme.example"):
        client.fetch("http://acme.example/")
    assert site.seen == []


def test_redirects_are_followed_one_hop_at_a_time() -> None:
    site = FakeSite(
        {
            "https://acme.example/": reply(status=301, headers={"location": "/a"}),
            "https://acme.example/a": reply(status=302, headers={"location": "/b"}),
            "https://acme.example/b": reply(status=307, headers={"location": "/c"}),
            "https://acme.example/c": reply(b"<title>end</title>"),
        }
    )
    fetched = site.client().fetch("https://acme.example/")
    assert fetched.url == "https://acme.example/c"
    assert site.seen == [
        "https://acme.example/",
        "https://acme.example/a",
        "https://acme.example/b",
        "https://acme.example/c",
    ]


def test_a_redirect_to_plain_http_is_refused() -> None:
    site = FakeSite(
        {"https://acme.example/": reply(status=301, headers={"location": "http://acme.example/"})}
    )
    with pytest.raises(SiteUnreachable, match="https"):
        site.client().fetch("https://acme.example/")
    assert site.seen == ["https://acme.example/"]


def test_a_redirect_without_a_location_is_refused() -> None:
    site = FakeSite({"https://acme.example/": reply(status=302)})
    with pytest.raises(SiteUnreachable, match="redirects"):
        site.client().fetch("https://acme.example/")


def test_too_many_redirects_stop_the_chain() -> None:
    chain = {
        f"https://acme.example/{'' if i == 0 else i}": reply(
            status=301, headers={"location": f"/{i + 1}"}
        )
        for i in range(MAX_REDIRECTS + 2)
    }
    site = FakeSite(chain)
    with pytest.raises(SiteUnreachable, match="redirects"):
        site.client().fetch("https://acme.example/")
    assert len(site.seen) == MAX_REDIRECTS + 1


def test_exactly_the_redirect_limit_is_allowed() -> None:
    routes = {
        "https://acme.example/": reply(status=301, headers={"location": "/1"}),
        "https://acme.example/1": reply(status=301, headers={"location": "/2"}),
        "https://acme.example/2": reply(status=301, headers={"location": "/3"}),
        "https://acme.example/3": reply(b"<title>ok</title>"),
    }
    assert FakeSite(routes).client().fetch("https://acme.example/").url.endswith("/3")


def test_the_request_budget_runs_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x")

    client = SiteClient(transport=httpx.MockTransport(handler))
    for index in range(MAX_REQUESTS):
        client.fetch(f"https://acme.example/{index}")
    with pytest.raises(SiteUnreachable, match="Too many requests to acme.example"):
        client.fetch("https://acme.example/one-more")


def test_a_body_is_cut_off_at_the_size_limit() -> None:
    site = FakeSite({"https://acme.example/": reply(b"x" * (2 * MAX_BYTES))})
    assert len(site.client().fetch("https://acme.example/").body) == MAX_BYTES


def test_a_server_error_names_the_host_and_status() -> None:
    site = FakeSite({"https://acme.example/": reply(b"oops", status=500)})
    with pytest.raises(SiteUnreachable, match=r"acme\.example answered with an error \(500\)"):
        site.client().fetch("https://acme.example/")


def test_a_timeout_names_the_host() -> None:
    site = FakeSite({"https://acme.example/": raising(httpx.ReadTimeout("slow"))})
    with pytest.raises(SiteUnreachable) as caught:
        site.client().fetch("https://acme.example/")
    assert caught.value.message == "acme.example didn't answer in time."


def test_a_transport_error_is_a_plain_unreachable_message() -> None:
    site = FakeSite({"https://acme.example/": raising(httpx.ConnectError("refused"))})
    with pytest.raises(SiteUnreachable) as caught:
        site.client().fetch("https://acme.example/")
    assert "acme.example" in caught.value.message


def test_the_content_type_is_lowercased_without_parameters() -> None:
    site = FakeSite({"https://acme.example/": reply(b"x", content_type="Text/HTML; charset=UTF-8")})
    assert site.client().fetch("https://acme.example/").content_type == "text/html"


def test_blocked_websites_from_the_guard_pass_through_unchanged() -> None:
    def resolve(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = SiteClient(transport=GuardedTransport(resolve=resolve))
    with pytest.raises(BlockedWebsite) as caught:
        client.fetch("https://acme.example/")
    client.close()
    assert type(caught.value) is BlockedWebsite
    assert caught.value.message == research._BLOCKED_MESSAGE


# --- The website and the guard --------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "host"),
    [
        ("acme.example", "acme.example"),
        ("https://www.Acme.example/about", "acme.example"),
        ("  acme.example  ", "acme.example"),
        ("acme.example.", "acme.example"),
    ],
)
def test_website_host_normalises_the_domain(domain: str, host: str) -> None:
    assert website_host(domain) == host


@pytest.mark.parametrize("domain", [None, "", "   "])
def test_a_blank_website_has_nowhere_to_look(domain: str | None) -> None:
    with pytest.raises(NoWebsite, match="no website"):
        website_host(domain)


def test_a_website_that_is_not_a_host_name_is_no_website() -> None:
    with pytest.raises(NoWebsite, match="isn't a website address"):
        website_host("acme")


@pytest.mark.parametrize(
    "domain",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "http://192.168.1.10/admin",
        "git.internal",
        "printer.local",
        "nas.lan",
        "intranet.corp",
    ],
)
def test_addresses_on_this_computer_or_a_private_network_are_blocked(domain: str) -> None:
    with pytest.raises(BlockedWebsite):
        website_host(domain)


@pytest.mark.parametrize("domain", ["localhost", "[::1]"])
def test_dotless_local_names_are_refused_before_any_lookup(domain: str) -> None:
    # normalize_domain() has no host without a dot, so these are refused as "not a website
    # address" rather than as BlockedWebsite. Either way no connection is made.
    with pytest.raises(ResearchError):
        website_host(domain)


def _resolver(*addresses: str):
    def resolve(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, port, 0, 0)) for a in addresses]

    return resolve


def test_a_public_address_is_allowed() -> None:
    assert research._check_address("acme.example", 443, _resolver("93.184.216.34")) == (
        "93.184.216.34"
    )


@pytest.mark.parametrize(
    "addresses",
    [
        ("10.0.0.5",),
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("::1",),
        ("fe80::1%eth0",),
        ("::ffff:127.0.0.1",),
        ("::ffff:10.1.2.3",),
        ("93.184.216.34", "192.168.0.9"),
        ("93.184.216.34", "127.0.0.1"),
        (),
    ],
)
def test_a_name_that_reaches_a_private_address_is_blocked(addresses: tuple[str, ...]) -> None:
    with pytest.raises(BlockedWebsite):
        research._check_address("acme.example", 443, _resolver(*addresses))


def test_an_ipv4_mapped_public_address_is_allowed() -> None:
    assert research._check_address("acme.example", 443, _resolver("::ffff:93.184.216.34")) == (
        "::ffff:93.184.216.34"
    )


def test_a_name_that_does_not_resolve_is_unreachable() -> None:
    def resolve(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        raise socket.gaierror(-2, "Name or service not known")

    with pytest.raises(SiteUnreachable, match="acme.example"):
        research._check_address("acme.example", 443, resolve)


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.0.0.5"])
def test_the_guarded_transport_refuses_a_private_address_before_connecting(address: str) -> None:
    client = SiteClient(transport=GuardedTransport(resolve=_resolver(address)))
    with pytest.raises(BlockedWebsite):
        client.fetch("https://acme.example/")
    client.close()


# --- Lookups --------------------------------------------------------------------------------


def test_a_lookup_reads_the_home_page_and_the_advertised_feed() -> None:
    site = acme_site()
    result = lookup(account(), site.client(), NOW)
    assert result.account_id == "acc-test"
    assert result.website == "acme.example"
    assert result.fetched_at == NOW
    assert result.site_title == "Acme Corporation | Home"
    assert result.site_description == "Acme builds freight software for regional carriers."
    assert result.feed_url == "https://acme.example/news/feed.xml"
    assert [a.title for a in result.articles] == [
        "Acme names a new CTO",
        "Acme raises Series B to hire support",
        "Acme opens an office in Leeds",
    ]
    assert result.sample is False
    assert site.seen == ["https://acme.example/", "https://acme.example/news/feed.xml"]


def test_without_an_advertised_feed_the_fallbacks_are_tried_in_order() -> None:
    site = FakeSite(
        {
            "https://acme.example/": reply(NO_FEED_HOME),
            "https://acme.example/rss.xml": reply(FEED, content_type="application/rss+xml"),
        }
    )
    result = lookup(account(), site.client(), NOW)
    assert result.feed_url == "https://acme.example/rss.xml"
    assert site.seen == [
        "https://acme.example/",
        "https://acme.example" + FALLBACK_FEEDS[0],
        "https://acme.example" + FALLBACK_FEEDS[1],
    ]


def test_no_feed_at_all_still_gives_the_title_and_an_empty_article_list() -> None:
    site = FakeSite({"https://acme.example/": reply(NO_FEED_HOME)})
    result = lookup(account(), site.client(), NOW)
    assert result.site_title == "Plain page"
    assert result.site_description is None
    assert result.feed_url is None
    assert result.articles == []


def test_a_feed_on_another_host_is_never_fetched() -> None:
    home = b'<title>Acme</title><link rel="alternate" type="application/rss+xml" href="https://feeds.example.net/acme.xml">'
    site = FakeSite(
        {
            "https://acme.example/": reply(home),
            "https://feeds.example.net/acme.xml": reply(FEED, content_type="application/rss+xml"),
        }
    )
    result = lookup(account(), site.client(), NOW)
    assert result.articles == []
    assert "https://feeds.example.net/acme.xml" not in site.seen
    assert site.seen[0] == "https://acme.example/"


def test_a_subdomain_feed_of_the_customer_is_allowed() -> None:
    home = b'<title>Acme</title><link rel="alternate" type="application/rss+xml" href="https://blog.acme.example/feed.xml">'
    site = FakeSite(
        {
            "https://acme.example/": reply(home),
            "https://blog.acme.example/feed.xml": reply(FEED, content_type="application/rss+xml"),
        }
    )
    assert len(lookup(account(), site.client(), NOW).articles) == 3


def test_a_blocked_feed_just_counts_as_no_feed() -> None:
    site = acme_site(**{"https://acme.example/news/feed.xml": raising(BlockedWebsite("blocked"))})
    result = lookup(account(), site.client(), NOW)
    assert result.articles == []
    assert result.site_title == "Acme Corporation | Home"


def test_a_feed_that_is_not_a_feed_just_counts_as_no_feed() -> None:
    site = acme_site(**{"https://acme.example/news/feed.xml": reply(b"<html>sorry</html>")})
    assert lookup(account(), site.client(), NOW).articles == []


def test_a_feed_with_only_unusable_links_counts_as_no_feed() -> None:
    feed = _rss(_item("Bad", "javascript:alert(1)"))
    site = acme_site(**{"https://acme.example/news/feed.xml": reply(feed)})
    result = lookup(account(), site.client(), NOW)
    assert result.articles == []
    assert result.feed_url is None


def test_a_home_page_that_is_not_html_is_unreachable() -> None:
    site = FakeSite({"https://acme.example/": reply(b"{}", content_type="application/json")})
    with pytest.raises(SiteUnreachable, match="acme.example didn't send a web page"):
        lookup(account(), site.client(), NOW)


def test_a_failed_home_page_is_unreachable() -> None:
    site = FakeSite({"https://acme.example/": reply(b"", status=503)})
    with pytest.raises(SiteUnreachable, match="acme.example answered with an error"):
        lookup(account(), site.client(), NOW)


def test_a_home_page_redirect_to_another_https_host_is_followed() -> None:
    site = FakeSite(
        {
            "https://acme.example/": reply(
                status=301, headers={"location": "https://www.acme.example/"}
            ),
            "https://www.acme.example/": reply(HOME),
            "https://www.acme.example/news/feed.xml": reply(
                FEED, content_type="application/rss+xml"
            ),
        }
    )
    result = lookup(account(), site.client(), NOW)
    assert result.website == "acme.example"
    assert len(result.articles) == 3


def test_a_lookup_for_a_blank_website_makes_no_request() -> None:
    site = FakeSite()
    with pytest.raises(NoWebsite):
        lookup(account(domain=None), site.client(), NOW)
    assert site.seen == []


def test_the_lookup_uses_the_guard_for_a_private_name() -> None:
    with pytest.raises(BlockedWebsite):
        lookup(account(domain="10.0.0.5"), SiteClient(), NOW)


# --- The invented sample --------------------------------------------------------------------


def test_the_sample_is_deterministic_per_account() -> None:
    first = sample_research(account(), NOW)
    second = sample_research(account(), NOW)
    assert first == second


def test_different_accounts_get_different_samples() -> None:
    a = sample_research(account(account_id="acc-one"), NOW)
    b = sample_research(account(account_id="acc-two"), NOW)
    assert [x.title for x in a.articles] != [x.title for x in b.articles]


def test_the_sample_is_marked_as_a_sample_and_uses_the_account_name() -> None:
    result = sample_research(account(name="Brindle Studio"), NOW)
    assert result.sample is True
    assert result.website == "acme.example"
    assert result.site_title == "Brindle Studio"
    assert result.feed_url == "https://acme.example/news/feed"
    assert 3 <= len(result.articles) <= 4
    assert all("Brindle Studio" in article.title for article in result.articles)


def test_sample_articles_are_dated_five_to_eighty_days_back_and_newest_first() -> None:
    result = sample_research(account(), NOW)
    days = [
        (NOW.date() - article.published).days for article in result.articles if article.published
    ]
    assert all(5 <= day <= 80 for day in days)
    assert days == sorted(days)


def test_sample_links_stay_on_the_account_website() -> None:
    result = sample_research(account(), NOW)
    assert all(a.url.startswith("https://acme.example/news/") for a in result.articles)


def test_samples_cover_every_topic_across_the_demo_accounts() -> None:
    covered: set[Topic] = set()
    untagged = 0
    for index in range(60):
        result = sample_research(account(account_id=f"acc-{index:03d}"), NOW)
        for article in result.articles:
            covered.update(article.topics)
            untagged += not article.topics
    assert covered == set(Topic)
    assert untagged > 0


def test_a_sample_without_a_domain_uses_a_name_based_example_host() -> None:
    result = sample_research(account(domain=None, name="Brindle Studio"), NOW)
    assert result.website == "brindle-studio.example"
    assert all(a.url.startswith("https://brindle-studio.example/") for a in result.articles)


def test_a_sample_with_a_blocked_domain_uses_the_name_too() -> None:
    result = sample_research(account(domain="10.0.0.5", name="Brindle Studio"), NOW)
    assert result.website == "brindle-studio.example"


# --- The saved-lookup cache -----------------------------------------------------------------


def _saved(account_id: str = "acc-test", *, fetched_at: datetime = NOW) -> SiteResearch:
    return SiteResearch(
        account_id=account_id,
        website="acme.example",
        fetched_at=fetched_at,
        site_title="Acme",
        site_description=None,
        feed_url=None,
        articles=[],
    )


def test_a_saved_lookup_comes_back_unchanged(tmp_path) -> None:
    cache = ResearchCache(tmp_path / "research")
    original = sample_research(account(), NOW)
    cache.put(original)
    assert cache.get("acc-test", "acme.example", NOW) == original


def test_a_saved_lookup_for_another_website_is_not_returned(tmp_path) -> None:
    cache = ResearchCache(tmp_path)
    cache.put(_saved())
    assert cache.get("acc-test", "other.example", NOW) is None


def test_a_saved_lookup_is_fresh_for_cache_hours_and_then_stale(tmp_path) -> None:
    cache = ResearchCache(tmp_path)
    cache.put(_saved())
    almost = NOW + timedelta(hours=CACHE_HOURS) - timedelta(minutes=1)
    assert cache.get("acc-test", "acme.example", almost) is not None
    assert cache.get("acc-test", "acme.example", NOW + timedelta(hours=CACHE_HOURS)) is None
    assert cache.get("acc-test", "acme.example", NOW + timedelta(hours=CACHE_HOURS + 1)) is None


def test_a_lookup_saved_in_the_future_is_not_trusted(tmp_path) -> None:
    cache = ResearchCache(tmp_path)
    cache.put(_saved(fetched_at=NOW + timedelta(hours=1)))
    assert cache.get("acc-test", "acme.example", NOW) is None


def test_a_naive_timestamp_is_read_as_utc(tmp_path) -> None:
    cache = ResearchCache(tmp_path)
    cache.put(_saved())
    later = (NOW + timedelta(hours=2)).replace(tzinfo=None)
    assert cache.get("acc-test", "acme.example", later) is not None


def test_a_corrupt_file_reads_as_no_lookup(tmp_path) -> None:
    cache = ResearchCache(tmp_path)
    cache.put(_saved())
    path = next(tmp_path.glob("*.json"))
    path.write_text("{not json", encoding="utf-8")
    assert cache.get("acc-test", "acme.example", NOW) is None
    path.write_text('{"account_id": "acc-test"}', encoding="utf-8")
    assert cache.get("acc-test", "acme.example", NOW) is None


def test_a_file_saved_for_another_account_is_not_returned(tmp_path) -> None:
    cache = ResearchCache(tmp_path)
    cache.put(_saved("acc-other"))
    source = next(tmp_path.glob("*.json"))
    target = cache._path("acc-test")
    target.write_bytes(source.read_bytes())
    assert cache.get("acc-test", "acme.example", NOW) is None


def test_ids_with_slashes_and_dots_never_become_paths(tmp_path) -> None:
    cache = ResearchCache(tmp_path / "research")
    tricky = "../../escape/..\\me"
    cache.put(_saved(tricky))
    files = list((tmp_path / "research").iterdir())
    assert len(files) == 1
    assert len(files[0].stem) == 32
    assert files[0].parent == tmp_path / "research"
    assert cache.get(tricky, "acme.example", NOW) is not None
    assert not (tmp_path / "escape").exists()


def test_clear_counts_and_removes_every_saved_lookup(tmp_path) -> None:
    cache = ResearchCache(tmp_path / "research")
    cache.put(_saved("acc-a"))
    cache.put(_saved("acc-b"))
    assert cache.clear() == 2
    assert list((tmp_path / "research").glob("*.json")) == []
    assert cache.clear() == 0


def test_clear_on_a_missing_folder_is_zero(tmp_path) -> None:
    assert ResearchCache(tmp_path / "never-made").clear() == 0


def test_put_creates_the_folder(tmp_path) -> None:
    root = tmp_path / "a" / "b"
    ResearchCache(root).put(_saved())
    assert len(list(root.glob("*.json"))) == 1
