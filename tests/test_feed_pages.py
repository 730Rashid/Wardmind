"""The data feed pages: the Add a connection catalog, the setup card, the matching page, and the
feed rows on Settings > Connections.

Everything runs through the real routes against the temp folders from conftest. Sources are
made-up web addresses answered by an httpx.MockTransport, so nothing leaves the test. The keys are
made-up strings with a distinctive middle, so a leak into any page is easy to spot. The
product providers are not used here: only the generic link and file sources fetch.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cs_brief.core import editing, importing, service
from cs_brief.core import secrets as vault
from cs_brief.core.feeds import catalog, setup
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds.models import Category, FeedRole, MatchBy, Schedule
from cs_brief.core.settings import DataSource, Settings, save_settings
from cs_brief.core.store import FEED_DRAFTS_DIR, ConnectionKind, LocalStore
from cs_brief.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
HX = {"origin": "http://127.0.0.1", "HX-Request": "true"}
LINK_URL = "https://exports.example.com/accounts.csv"
SAVED_KEY = "rk-test-SAVEDKEY-0123456789abcdef"  # made up
TYPED_KEY = "rk-test-TYPEDKEY-fedcba9876543210"  # made up
NOW = datetime(2026, 11, 8, 9, 0)
TODAY = date(2026, 11, 8)
ACCOUNTS_CSV = (
    "Company,ARR,Renewal date,Owner,Usage trend\n"
    '"Acme","$120,000",2027-03-01,Dana,-38\n'
    '"Beta","$50,000",2027-06-01,Sam,5\n'
)
SIGNALS_CSV = "Website,Usage trend\nacme.com,-38\nnowhere.example,6\n"
AMBIGUOUS_DATES_CSV = (
    'Company,ARR,Renewal date\n"Acme","$120,000",03/04/2027\n"Beta","$50,000",12/05/2027\n'
)
BUILT_IN = ["Spreadsheet import", "Paste cells", "Add an account by hand", "HubSpot"]
PROVIDER_NAMES = [info.name for info in catalog.INFOS]
CROSS_SITE = [
    {"origin": "https://evil.example"},
    {"origin": "null", "sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "cross-site"},
]
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
_CARD = re.compile(
    r'<article class="account-card provider-card" aria-labelledby="card-([^"]+)">(.*?)</article>',
    re.S,
)
_NAME = re.compile(
    r'<h3 class="card-title" id="card-([^"]+)">'
    r'<a class="account-link" href="[^"]*">([^<]+)</a></h3>'
)


class Served:
    """What the mock web address answers, and the requests it got."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body = ACCOUNTS_CSV
        self.key: str | None = None  # the key this source accepts: a request must send it


@pytest.fixture(autouse=True)
def mine() -> None:
    """Feeds belong to Your data, so every test here runs with it showing."""
    save_settings(Settings(source=DataSource.MINE))


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    """Every test here has a mock source, so a feed can be fetched without the network."""
    result = Served()

    def answer(request: httpx.Request) -> httpx.Response:
        result.requests.append(request)
        if result.status != 200:
            return httpx.Response(result.status, content=b"")
        if (
            result.key is not None
            and request.headers.get("authorization") != f"Bearer {result.key}"
        ):
            return httpx.Response(401, content=b"")
        return httpx.Response(
            200,
            content=result.body.encode("utf-8"),
            headers={"content-type": "text/csv"},
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return result


@pytest.fixture
def acme(store: LocalStore) -> None:
    """One account in the book, found by its website: what a SIGNALS feed matches."""
    editing.create_account(
        store,
        {"name": "Acme", "domain": "acme.com", "arr": "$1,000", "renewal_date": "2027-01-01"},
        today=TODAY,
    )


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _cards(markup: str) -> dict[str, str]:
    """Each catalog card's markup by its id (a provider id or a built-in key)."""
    return {key: body for key, body in _CARD.findall(markup)}


def _card_names(markup: str) -> list[str]:
    return [name for _, name in _NAME.findall(markup)]


def _draft_id(response: Any) -> str:
    assert response.status_code == 303, _text(response)
    return str(response.headers["location"]).split("/")[3]


def _start(client: TestClient, provider: str = "link", **fields: str) -> Any:
    """Post the setup card for a provider: the link source by default, with the form's usual
    values, overridden by any given."""
    data = {
        "name": "Renewals",
        "role": "accounts",
        "schedule": "60",
        "match_by": "auto",
        "url": LINK_URL,
        "format": "auto",
        "auth": "none",
        "records_path": "",
        "header_name": "",
        "username": "",
        "secret": "",
    }
    data.update(fields)
    return client.post(
        f"/connections/add/{provider}", data=data, headers=ORIGIN, follow_redirects=False
    )


def _mapping_form(mapping: dict[str, str], date_order: str = "") -> dict[str, str]:
    """The matching page's fields for a mapping keyed by import field, as a browser posts them."""
    fields = {
        f"column-{index}": mapping.get(spec.key, "")
        for index, spec in enumerate(importing.IMPORT_FIELDS)
    }
    if date_order:
        fields["date_order"] = date_order
    return fields


def _auto(text: str) -> dict[str, str]:
    return importing.auto_map(importing.read_csv(text.encode("utf-8"), "x.csv").headers)


def _feed(
    store: LocalStore,
    *,
    role: FeedRole = FeedRole.ACCOUNTS,
    values: dict[str, str] | None = None,
    secret: str | None = None,
    schedule: Schedule = Schedule.HOURLY,
    match_by: MatchBy = MatchBy.AUTO,
) -> Any:
    """A saved feed, made through the core steps (the same ones the matching page's Save runs).
    Its first sync uses the table the setup fetched, at the real time, so it reads as recent."""
    moment = datetime.now(UTC).replace(tzinfo=None)
    draft = setup.start_draft(
        store,
        "link",
        name="Renewals",
        role=role,
        values=values or {"url": LINK_URL, "auth": "none"},
        secret=secret,
        schedule=schedule,
        match_by=match_by,
        connection_id=None,
        now=moment,
    )
    return setup.commit_draft(
        store,
        draft,
        setup.suggested_mapping(draft),
        None,
        book_accounts=service.feed_book_accounts,
        now=moment,
        today=TODAY,
    )


def _snapshot(store: LocalStore) -> dict[str, bytes]:
    """Every file the feeds keep: the data folder and the secrets folder."""
    found: dict[str, bytes] = {}
    for root in (store.root, vault.secrets_dir()):
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    found[str(path)] = path.read_bytes()
    return found


def _assert_no_python_none(text: str) -> None:
    """A None from Python never reaches a page. ("None of the rows" is a sentence, so it is set
    aside first.)"""
    assert "None" not in text.replace("None of the", "")


# The catalog ------------------------------------------------------------------------------------


def test_the_catalog_lists_the_built_in_cards_then_every_provider_in_order(
    client: TestClient,
) -> None:
    response = client.get("/connections/add")
    assert response.status_code == 200
    text = _text(response)
    assert '<h1 class="page-title">Add a connection</h1>' in text
    assert "What do you want to bring in?" in text
    assert _card_names(text) == [*BUILT_IN, *PROVIDER_NAMES]


def test_each_provider_card_has_its_mark_by_line_description_and_tags(client: TestClient) -> None:
    cards = _cards(client.get("/connections/add").text)
    for info in catalog.INFOS:
        body = html.unescape(cards[info.id])
        assert f'<span class="rank-tile mark-tile" aria-hidden="true">{info.mark}</span>' in body
        assert info.by in body
        assert info.description in body
        for category in info.categories:
            assert f"pill--{category.value}" in body
            assert category.label in body


def test_the_built_in_cards_link_to_the_existing_flows(client: TestClient) -> None:
    cards = _cards(client.get("/connections/add").text)
    assert 'href="/import?back=connections"' in cards["spreadsheet"]
    assert 'href="/import?paste=1&amp;back=connections"' in cards["paste"]
    assert 'href="/accounts/new"' in cards["manual"]
    assert 'href="/settings/connections#hubspot-access-title"' in cards["hubspot"]


def test_the_category_filter_keeps_any_card_with_a_chosen_category(client: TestClient) -> None:
    text = client.get("/connections/add?category=billing").text
    billing = [info.name for info in catalog.INFOS if Category.BILLING in info.categories]
    assert _card_names(text) == billing
    assert 'value="billing" checked>' in text
    both = client.get("/connections/add?category=billing&category=tickets").text
    expected = [
        info.name
        for info in catalog.INFOS
        if {Category.BILLING, Category.TICKETS} & set(info.categories)
    ]
    assert _card_names(both) == ["HubSpot", *expected]
    assert "Clear filters" in both


def test_the_search_matches_the_name_the_by_line_and_the_description(client: TestClient) -> None:
    assert _card_names(client.get("/connections/add?q=zendesk").text) == ["Zendesk"]
    assert _card_names(client.get("/connections/add?q=invoice").text) == [
        info.name for info in catalog.INFOS if "invoice" in info.description.lower()
    ]
    assert "No connections match" in client.get("/connections/add?q=nothing-like-this").text


def test_an_htmx_refresh_gets_only_the_results_and_the_count(client: TestClient) -> None:
    response = client.get("/connections/add?q=stripe", headers=HX)
    text = _text(response)
    assert "<html" not in text
    assert text.startswith('<div id="results">')
    assert _card_names(text) == ["Stripe"]
    assert 'id="result-count" hx-swap-oob="innerHTML"' in text


def test_an_htmx_category_change_gets_only_the_matching_cards(client: TestClient) -> None:
    text = _text(client.get("/connections/add?category=usage", headers=HX))
    assert "<html" not in text
    assert _card_names(text) == [
        info.name for info in catalog.INFOS if Category.USAGE in info.categories
    ]
    assert 'value="usage" checked>' in client.get("/connections/add?category=usage").text


def test_a_provider_with_a_feed_shows_connected_and_hubspot_with_its_connection(
    client: TestClient, store: LocalStore
) -> None:
    cards = _cards(client.get("/connections/add").text)
    assert "Connected" not in cards["link"] and "Connected" not in cards["hubspot"]
    _feed(store)
    store.add_connection(ConnectionKind.HUBSPOT, "HubSpot")
    cards = _cards(client.get("/connections/add").text)
    assert "Connected" in cards["link"]
    assert "Connected" not in cards["stripe"]
    assert "Connected" in cards["hubspot"]


def test_the_catalog_explains_demo_data_and_offers_the_way_to_switch(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.DEMO))
    response = client.get("/connections/add")
    assert response.status_code == 200
    assert "Connections belong to Your data, and demo data is showing." in response.text
    assert 'href="/settings/connections"' in response.text
    assert 'id="add-filter"' not in response.text


# The setup card ---------------------------------------------------------------------------------


def test_every_kind_of_field_has_its_input(client: TestClient) -> None:
    link = client.get("/connections/add/link").text
    assert 'id="field-url" name="url"' in link and 'inputmode="url"' in link
    assert '<select id="field-format" name="format"' in link
    assert 'type="password" id="field-secret" name="secret"' in link
    assert 'autocomplete="off"' in link
    sqlite = client.get("/connections/add/sqlite").text
    assert '<textarea id="field-query" name="query"' in sqlite
    assert 'id="field-path" name="path"' in sqlite
    files = client.get("/connections/add/file").text
    assert 'id="field-path" name="path"' in files and 'id="field-sheet" name="sheet"' in files


def test_the_setup_card_offers_roles_schedules_and_matching(client: TestClient) -> None:
    link = client.get("/connections/add/link").text
    assert "What does it add?" in link
    assert "Adds accounts" in link and "Adds signals to my accounts" in link
    assert "Every hour" in link
    assert "Match accounts by" in link and "Only used when it adds signals." in link
    assert "Test and continue" in link
    stripe = html.unescape(client.get("/connections/add/stripe").text)
    assert "What does it add?" not in stripe  # one role: no choice to make
    assert "In Stripe: Developers > API keys > Create restricted key." in stripe


def test_a_secret_is_a_password_box_and_is_never_echoed_back(client: TestClient) -> None:
    response = client.post(
        "/connections/add/link",
        data={
            "name": "Renewals",
            "url": "http://exports.example.com/a.csv",
            "secret": TYPED_KEY,
            "role": "accounts",
            "schedule": "60",
            "match_by": "auto",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 422
    assert TYPED_KEY not in response.text and "TYPEDKEY" not in response.text
    secret_input = re.search(r'<input type="password" id="field-secret"[^>]*>', response.text)
    assert secret_input is not None and "value=" not in secret_input.group(0)


def test_a_bad_address_is_named_on_its_own_field(client: TestClient) -> None:
    response = _start(client, url="http://exports.example.com/a.csv")
    assert response.status_code == 422
    text = _text(response)
    assert re.search(
        r'id="field-url"[^>]*aria-invalid="true"[^>]*>.*?id="field-url-error">'
        r"The web address must start with https://\.<",
        text,
        re.S,
    )
    assert "Nothing was saved. Fix the marked field, then test again." in text


def test_a_missing_required_field_is_named_on_its_field(client: TestClient) -> None:
    text = _text(_start(client, url=""))
    assert 'id="field-url-error">Fill in Web address.<' in text


def test_a_refusal_keeps_what_was_typed_except_the_key(client: TestClient) -> None:
    response = _start(
        client,
        url="http://exports.example.com/a.csv",
        name="My feed",
        schedule="360",
        match_by="id",
        secret=TYPED_KEY,
        auth="bearer",
    )
    assert response.status_code == 422
    text = response.text
    assert 'value="My feed"' in text
    assert 'value="http://exports.example.com/a.csv"' in text
    assert '<option value="360" selected>' in text
    assert 'value="id" aria-describedby="match-by-help" checked>' in text
    assert TYPED_KEY not in text


def test_a_source_that_cannot_be_read_shows_a_top_alert_and_saves_nothing(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    served.status = 404
    response = _start(client, auth="bearer", secret=TYPED_KEY)
    assert response.status_code == 422
    text = _text(response)
    assert 'role="alert">exports.example.com has nothing at that address.' in text
    assert TYPED_KEY not in response.text
    assert store.connections() == []
    drafts = store.root / FEED_DRAFTS_DIR
    assert not drafts.exists() or list(drafts.glob("*.json")) == []
    feeds = vault.secrets_dir() / vault.FEED_SECRETS_DIR
    assert not feeds.exists() or list(feeds.iterdir()) == []


def test_a_working_source_opens_the_matching_page(client: TestClient, served: Served) -> None:
    response = _start(client)
    draft_id = _draft_id(response)
    assert response.headers["location"] == f"/connections/feeds/{draft_id}/match"
    page = client.get(f"/connections/feeds/{draft_id}/match")
    assert page.status_code == 200
    assert "Match your columns" in page.text


def test_an_unknown_provider_is_a_plain_not_found_page(client: TestClient) -> None:
    response = client.get("/connections/add/not-a-provider")
    assert response.status_code == 404
    assert "isn't in the catalog" in _text(response)
    assert (
        client.post("/connections/add/not-a-provider", data={}, headers=ORIGIN).status_code == 404
    )


def test_setup_in_demo_mode_explains_and_saves_nothing(
    client: TestClient, store: LocalStore
) -> None:
    save_settings(Settings(source=DataSource.DEMO))
    assert "Connections belong to Your data" in client.get("/connections/add/link").text
    response = _start(client)
    assert response.status_code == 422
    assert "Connections belong to Your data" in response.text
    assert store.connections() == []


# The matching page ------------------------------------------------------------------------------


def test_the_matching_page_preselects_the_columns_and_shows_an_example(
    client: TestClient, served: Served
) -> None:
    draft_id = _draft_id(_start(client))
    text = client.get(f"/connections/feeds/{draft_id}/match").text
    assert '<option value="Company" selected>' in text
    assert '<option value="ARR" selected>' in text
    assert '<option value="Usage trend" selected>' in text
    assert re.search(r'id="example-0">Acme</td>', text)
    assert "Example from row 2" in text


def test_the_preview_counts_the_ready_accounts_and_saving_is_on(
    client: TestClient, served: Served
) -> None:
    draft_id = _draft_id(_start(client))
    text = client.get(f"/connections/feeds/{draft_id}/match").text
    assert "2 accounts ready." in text
    assert 'class="button-primary">Save connection</button>' in text


def test_a_missing_required_column_blocks_saving(client: TestClient, served: Served) -> None:
    draft_id = _draft_id(_start(client))
    mapping = _auto(ACCOUNTS_CSV)
    mapping.pop("arr")
    response = client.post(
        f"/connections/feeds/{draft_id}/preview", data=_mapping_form(mapping), headers=HX
    )
    assert "Match ARR to import." in _text(response)
    assert 'class="button-primary" disabled' in response.text


def test_the_preview_partial_refreshes_the_examples_and_the_outcome(
    client: TestClient, served: Served
) -> None:
    draft_id = _draft_id(_start(client))
    response = client.post(
        f"/connections/feeds/{draft_id}/preview",
        data=_mapping_form(_auto(ACCOUNTS_CSV)),
        headers=HX,
    )
    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="what-happens"' in response.text and 'hx-swap-oob="true"' in response.text
    assert 'id="example-0" hx-swap-oob="true">Acme</td>' in response.text
    assert "2 accounts ready." in _text(response)
    assert "<html" not in response.text


def test_signals_show_the_key_and_signal_fields_and_the_rows_that_matched_nothing(
    client: TestClient, store: LocalStore, served: Served, acme: None
) -> None:
    served.body = SIGNALS_CSV
    draft_id = _draft_id(_start(client, role="signals"))
    text = _text(client.get(f"/connections/feeds/{draft_id}/match"))
    assert "Matches accounts by ID, then website, then name." in text
    assert text.count('class="cell-field"') == 10  # 3 keys, then the 7 signal fields
    assert "Renewal date" not in text and "ARR" not in text
    assert "1 of 2 rows match an account, adding readings to 1 account." in text
    assert "1 row didn't match an account:" in text
    assert "Row 3 (nowhere.example): No account has the website nowhere.example." in text
    assert "Save connection" in text


def test_a_signals_source_with_no_rows_is_blocked(client: TestClient, served: Served) -> None:
    served.body = "Website,Usage trend\n"
    draft_id = _draft_id(_start(client, role="signals"))
    text = _text(client.get(f"/connections/feeds/{draft_id}/match"))
    assert "The source sent no rows." in text
    assert 'class="button-primary" disabled' in text


def test_a_signals_source_that_matches_no_account_is_blocked_and_says_why(
    client: TestClient, served: Served
) -> None:
    served.body = SIGNALS_CSV
    draft_id = _draft_id(_start(client, role="signals"))
    text = _text(client.get(f"/connections/feeds/{draft_id}/match"))
    assert "None of the 2 rows matched an account." in text
    assert "None of the 2 rows matched an account in your book. Check Match accounts by." in text
    assert 'class="button-primary" disabled' in text


def test_day_or_month_is_asked_only_when_the_dates_do_not_settle_it(
    client: TestClient, served: Served
) -> None:
    served.body = AMBIGUOUS_DATES_CSV
    ambiguous = _draft_id(_start(client))
    assert "Day first (31/12/2026)" in client.get(f"/connections/feeds/{ambiguous}/match").text
    served.body = ACCOUNTS_CSV
    settled = _draft_id(_start(client))
    assert "Day first (31/12/2026)" not in client.get(f"/connections/feeds/{settled}/match").text


def test_save_creates_the_feed_with_its_key_and_says_so_on_connections(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    served.key = TYPED_KEY
    draft_id = _draft_id(_start(client, auth="bearer", secret=TYPED_KEY))
    response = client.post(
        f"/connections/feeds/{draft_id}/commit",
        data=_mapping_form(_auto(ACCOUNTS_CSV)),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?feed_added=feed-1"
    config = store.get_connection("feed-1")
    assert config is not None and config.kind is ConnectionKind.FEED and config.feed is not None
    assert config.feed.status.accounts == 2 and config.feed.schedule is Schedule.HOURLY
    assert vault.get_secret("feed-1") == TYPED_KEY
    assert setup.load_draft(store, draft_id, now=NOW) is None
    notice = _text(client.get(response.headers["location"]))
    assert "Renewals is connected. 2 accounts were imported." in notice


def test_save_is_refused_on_the_page_when_nothing_is_ready(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    draft_id = _draft_id(_start(client))
    mapping = _auto(ACCOUNTS_CSV)
    mapping.pop("arr")
    response = client.post(
        f"/connections/feeds/{draft_id}/commit",
        data=_mapping_form(mapping),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert 'role="alert">Match ARR to import.' in response.text
    assert store.connections() == []


def test_cancel_deletes_the_set_up_and_its_key(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    draft_id = _draft_id(_start(client, auth="bearer", secret=TYPED_KEY))
    assert vault.get_secret(f"draft-{draft_id}") == TYPED_KEY
    response = client.post(
        f"/connections/feeds/{draft_id}/cancel", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/connections/add"
    assert setup.load_draft(store, draft_id, now=NOW) is None
    assert vault.get_secret(f"draft-{draft_id}") is None
    gone = client.get(f"/connections/feeds/{draft_id}/match")
    assert gone.status_code == 404
    assert 'href="/connections/add"' in gone.text


# Settings > Connections: the feed rows ----------------------------------------------------------


def test_a_feed_row_shows_its_source_schedule_and_status(
    client: TestClient, store: LocalStore
) -> None:
    _feed(store)
    text = _text(client.get("/settings/connections"))
    assert "Link to a file. Adds accounts." in text
    assert "2 accounts. Synced just now." in text
    assert "Every hour" in text
    assert 'href="/connections/add/link?connection=feed-1"' in text
    assert 'action="/settings/connections/feed-1/sync"' in text


def test_a_failed_sync_shows_its_error_as_an_alert_and_keeps_the_data(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    _feed(store)
    served.status = 500
    response = client.post(
        "/settings/connections/feed-1/sync", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    text = _text(client.get(response.headers["location"]))
    assert "Renewals didn't sync. exports.example.com had a problem (500)." in text
    assert re.search(r'role="alert">exports\.example\.com had a problem \(500\)\.', text)
    assert "2 accounts." in text


def test_unmatched_rows_are_listed_under_a_signals_feed(
    client: TestClient, store: LocalStore, served: Served, acme: None
) -> None:
    served.body = SIGNALS_CSV
    _feed(store, role=FeedRole.SIGNALS, values={"url": LINK_URL, "auth": "none"})
    text = _text(client.get("/settings/connections"))
    assert "Adds readings to 1 account." in text
    assert '<details class="unmatched">' in text
    assert "1 row didn't match an account" in text
    assert "Row 3 (nowhere.example): No account has the website nowhere.example." in text
    assert "Adds signals to my accounts" in text


def test_sync_now_runs_the_feed_and_says_what_came_in(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    _feed(store)
    served.body = ACCOUNTS_CSV + '"Gamma","$10,000",2027-09-01,Lee,1\n'
    response = client.post(
        "/settings/connections/feed-1/sync", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?synced=feed-1"
    text = _text(client.get(response.headers["location"]))
    assert "Renewals synced. 3 accounts were imported." in text
    assert "Sync now" in text


def test_sync_now_for_an_unknown_connection_is_a_not_found_page(client: TestClient) -> None:
    response = client.post("/settings/connections/feed-9/sync", headers=ORIGIN)
    assert response.status_code == 404
    assert "That connection isn't in your list." in _text(response)


def test_change_settings_prefills_the_form_and_keeps_the_saved_key(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    served.key = SAVED_KEY
    _feed(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SAVED_KEY)
    page = client.get("/connections/add/link?connection=feed-1")
    assert page.status_code == 200
    text = page.text
    assert f'id="field-url" name="url" value="{LINK_URL}"' in text
    assert 'name="name" value="Renewals"' in text
    assert '<option value="60" selected>' in text
    assert 'name="connection" value="feed-1"' in text
    assert "Leave blank to keep the saved key." in text
    assert SAVED_KEY not in text and "SAVEDKEY" not in text
    response = _start(client, auth="bearer", connection="feed-1")
    assert response.status_code == 303
    draft = setup.load_draft(store, _draft_id(response), now=NOW)
    assert draft is not None and draft.connection_id == "feed-1"
    assert vault.get_secret("feed-1") == SAVED_KEY


def test_changing_the_address_asks_for_the_key_again_next_to_the_key_field(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    served.key = SAVED_KEY
    _feed(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SAVED_KEY)
    response = _start(
        client,
        url="https://elsewhere.example.com/a.csv",
        auth="bearer",
        connection="feed-1",
    )
    assert response.status_code == 422
    text = _text(response)
    assert re.search(
        r'id="field-secret"[^>]*aria-invalid="true"[^>]*>.*?id="field-secret-error">'
        r"The address changed, so type the key again\.",
        text,
        re.S,
    )
    assert SAVED_KEY not in response.text
    assert not any("elsewhere" in str(req.url) for req in served.requests)


def test_removing_a_feed_deletes_its_key_and_the_remove_page_says_so(
    client: TestClient, store: LocalStore
) -> None:
    _feed(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SAVED_KEY)
    assert vault.get_secret("feed-1") == SAVED_KEY
    page = client.get("/settings/connections/feed-1/remove")
    assert "Its saved key is deleted too." in page.text
    response = client.post(
        "/settings/connections/feed-1/remove", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert store.get_connection("feed-1") is None
    assert vault.get_secret("feed-1") is None


def test_the_remove_page_of_a_signals_feed_speaks_of_readings_not_accounts(
    client: TestClient, store: LocalStore, served: Served, acme: None
) -> None:
    served.body = SIGNALS_CSV
    _feed(store, role=FeedRole.SIGNALS, values={"url": LINK_URL, "auth": "none"})
    text = _text(client.get("/settings/connections/feed-1/remove"))
    assert "The readings it added to 1 account and their history leave your book." in text


# Cross-site writes, markup and the words that must not appear -----------------------------------


@pytest.fixture
def settled(store: LocalStore, served: Served, acme: None) -> Iterator[str]:
    """A saved feed and a set-up in progress, so a write that got through would show."""
    _feed(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SAVED_KEY)
    setup.start_draft(
        store,
        "link",
        name="Second",
        role=FeedRole.ACCOUNTS,
        values={"url": LINK_URL, "auth": "none"},
        secret=None,
        schedule=Schedule.HOURLY,
        match_by=MatchBy.AUTO,
        connection_id=None,
        now=NOW,
    )
    yield "ok"


@pytest.mark.parametrize("headers", CROSS_SITE)
@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/connections/add/link", {"name": "X", "url": LINK_URL, "auth": "none"}),
        ("/connections/feeds/0123456789abcdef/match", {"column-0": "Company"}),
        ("/connections/feeds/0123456789abcdef/preview", {"column-0": "Company"}),
        ("/connections/feeds/0123456789abcdef/commit", {"column-0": "Company"}),
        ("/connections/feeds/0123456789abcdef/cancel", {}),
        ("/settings/connections/feed-1/sync", {}),
    ],
)
def test_a_cross_site_post_to_a_feed_page_is_refused_and_changes_nothing(
    client: TestClient,
    store: LocalStore,
    settled: str,
    path: str,
    data: dict[str, str],
    headers: dict[str, str],
) -> None:
    before = _snapshot(store)
    response = client.post(path, data=data, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    assert _snapshot(store) == before


def test_the_feed_pages_have_no_inline_markup_and_print_no_python_none(
    client: TestClient, store: LocalStore, served: Served, acme: None
) -> None:
    served.body = SIGNALS_CSV
    feed = _feed(store, role=FeedRole.SIGNALS, values={"url": LINK_URL, "auth": "none"})
    draft_id = _draft_id(_start(client, role="signals"))
    pages = [
        "/connections/add",
        "/connections/add?category=usage",
        "/connections/add/link",
        "/connections/add/link?connection=" + feed.id,
        f"/connections/feeds/{draft_id}/match",
        "/settings/connections",
        "/settings/connections/" + feed.id + "/remove",
    ]
    for path in pages:
        response = client.get(path)
        assert response.status_code in (200, 404), path
        assert not INLINE.search(response.text), path
        _assert_no_python_none(_text(response))


def test_no_saved_key_reaches_any_feed_page(
    client: TestClient, store: LocalStore, served: Served
) -> None:
    served.key = SAVED_KEY
    _feed(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SAVED_KEY)
    for path in (
        "/connections/add",
        "/connections/add/link?connection=feed-1",
        "/connections/add/file",
        "/settings/connections",
        "/settings/connections/feed-1/remove",
    ):
        response = client.get(path)
        assert SAVED_KEY not in response.text and "SAVEDKEY" not in response.text, path
        assert SAVED_KEY.encode() not in response.content, path


def test_the_built_in_links_on_connections_still_go_where_they_did(client: TestClient) -> None:
    text = client.get("/settings/connections").text
    assert 'href="/import?back=connections"' in text
    assert 'href="/connections/add"' in text
    assert "Add a connection" in text
