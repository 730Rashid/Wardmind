"""The six product providers: Stripe, Chargebee, Zendesk, Freshdesk, PostHog and Airtable.

Every request goes through an httpx.MockTransport, so nothing reaches the network. The responses
are hand-written to match each API's documented shape. Each provider is checked for its rows and
headers, for the key going only into its header or basic auth, for pagination, and for the
refusals that happen before any request is sent.
"""

from __future__ import annotations

import base64
import itertools
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from wardmind.core import importing
from wardmind.core.feeds import http as feed_http
from wardmind.core.feeds import sync
from wardmind.core.feeds.http import FeedError
from wardmind.core.feeds.providers import chargebee, get_provider, stripe
from wardmind.core.feeds.providers.base import MAX_PAGES, MAX_ROWS

SECRET = "fake-key-7f3a9c1e5b2d8046"
DAY = date(2026, 11, 8)
STRIPE_HEADERS = ["ID", "Account name", "Website", "Days invoice overdue"]
CHARGEBEE_HEADERS = ["ID", "Account name", "Website", "Days invoice overdue"]
ZENDESK_HEADERS = ["ID", "Account name", "Website", "Open severity-1 tickets"]
FRESHDESK_HEADERS = ["ID", "Account name", "Website", "Open severity-1 tickets"]
SIGNAL_MAP = {
    "external_id": "ID",
    "name": "Account name",
    "domain": "Website",
    "signal:billing_overdue_days": "Days invoice overdue",
}


# Helpers ----------------------------------------------------------------------------------------


def serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    """Answer every feed request with `handler`, and return the requests in the order sent."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return seen


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


def fail(status: int) -> httpx.Response:
    # The body repeats the key on purpose: no error text may ever show it.
    return httpx.Response(status, json={"error": f"bad key {SECRET}"})


def fetch(provider_id: str, settings: dict[str, str], secret: str | None = SECRET) -> Any:
    return sync.fetch_with(provider_id, settings, secret)


def refusal(provider_id: str, settings: dict[str, str], secret: str | None = SECRET) -> FeedError:
    """The FeedError a sync raises. Asserts it never shows the key."""
    with pytest.raises(FeedError) as caught:
        fetch(provider_id, settings, secret)
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.message
    return caught.value


def unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def days_ago(days: int) -> int:
    return unix(DAY - timedelta(days=days))


def days_ahead(days: int) -> int:
    return unix(DAY + timedelta(days=days))


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


def assert_key_never_in_an_address(seen: list[httpx.Request]) -> None:
    assert seen
    for request in seen:
        assert SECRET not in str(request.url)


# Stripe -----------------------------------------------------------------------------------------


def stripe_customer(
    customer_id: str, name: str = "", email: str = "", metadata: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "id": customer_id,
        "object": "customer",
        "name": name,
        "email": email,
        "metadata": metadata or {},
    }


def stripe_invoice(invoice_id: str, customer: str, due_date: int | None) -> dict[str, Any]:
    return {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer,
        "due_date": due_date,
        "created": days_ago(60),
    }


def stripe_api(customers: list[dict[str, Any]], invoices: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/customers":
            return ok({"object": "list", "data": customers, "has_more": False})
        if request.url.path == "/v1/invoices":
            return ok({"object": "list", "data": invoices, "has_more": False})
        return httpx.Response(404)

    return handler


@pytest.fixture
def stripe_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stripe, "_today", lambda: DAY)


def test_stripe_rows_show_each_customers_most_overdue_invoice(
    monkeypatch: pytest.MonkeyPatch, stripe_today: None
) -> None:
    customers = [
        stripe_customer(
            "cus_1",
            "Acme Ltd",
            "billing@acme.example",
            {"account_id": "acct-1", "website": "acme.example"},
        ),
        stripe_customer("cus_2", "Globex", "ops@globex.example", {"wardmind_id": "gbx-9"}),
        stripe_customer("cus_3", "", "jo@gmail.com"),
        stripe_customer("cus_4", "Initech"),
    ]
    invoices = [
        stripe_invoice("in_1", "cus_1", days_ago(7)),
        stripe_invoice("in_2", "cus_1", days_ago(30)),  # the older one wins
        stripe_invoice("in_3", "cus_2", None),  # no due date
        stripe_invoice("in_4", "cus_3", days_ahead(5)),  # still to fall due
        stripe_invoice("in_5", "cus_gone", days_ago(90)),  # no customer in the list
    ]
    seen = serve(monkeypatch, stripe_api(customers, invoices))

    table = fetch("stripe", {})

    assert table.headers == STRIPE_HEADERS
    assert table.rows == [
        ["acct-1", "Acme Ltd", "acme.example", "30"],
        ["gbx-9", "Globex", "globex.example", "0"],
        ["cus_3", "", "", "0"],
        ["cus_4", "Initech", "", "0"],
    ]
    assert importing.auto_map(table.headers) == SIGNAL_MAP
    assert_key_never_in_an_address(seen)
    assert {request.headers["authorization"] for request in seen} == {f"Bearer {SECRET}"}
    assert {request.url.params["limit"] for request in seen} == {"100"}
    invoice_request = next(r for r in seen if r.url.path == "/v1/invoices")
    assert invoice_request.url.params["status"] == "open"


def test_stripe_follows_starting_after_while_has_more(
    monkeypatch: pytest.MonkeyPatch, stripe_today: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("starting_after")
        if request.url.path == "/v1/customers":
            if after is None:
                page = [stripe_customer("cus_1", "A"), stripe_customer("cus_2", "B")]
                return ok({"data": page, "has_more": True})
            return ok({"data": [stripe_customer("cus_3", "C")], "has_more": False})
        if after is None:
            return ok({"data": [stripe_invoice("in_1", "cus_1", days_ago(5))], "has_more": True})
        return ok({"data": [stripe_invoice("in_2", "cus_2", days_ago(9))], "has_more": False})

    seen = serve(monkeypatch, handler)

    table = fetch("stripe", {})

    assert table.rows == [
        ["cus_1", "A", "", "5"],
        ["cus_2", "B", "", "9"],
        ["cus_3", "C", "", "0"],
    ]
    customer_pages = [r.url.params.get("starting_after") for r in seen if "customers" in r.url.path]
    invoice_pages = [r.url.params.get("starting_after") for r in seen if "invoices" in r.url.path]
    assert customer_pages == [None, "cus_2"]
    assert invoice_pages == [None, "in_1"]


def test_stripe_a_page_claiming_more_without_a_cursor_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(monkeypatch, lambda request: ok({"data": [], "has_more": True}))

    error = refusal("stripe", {})

    assert error.message == ("Stripe said there are more to read but didn't say where they start.")


def test_stripe_a_refused_key_gets_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda request: fail(401))

    error = refusal("stripe", {})

    assert error.message == (
        "api.stripe.com refused the key or password. Check it, and that it can read."
    )
    assert_key_never_in_an_address(seen)


@pytest.mark.parametrize("secret", [None, ""])
def test_stripe_without_a_key_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    seen = serve(monkeypatch, stripe_api([], []))

    error = refusal("stripe", {}, secret)

    assert error.message == "Type the restricted key first."
    assert seen == []


def test_stripe_stops_at_max_pages(monkeypatch: pytest.MonkeyPatch, stripe_today: None) -> None:
    counter = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/customers":
            return ok({"data": [stripe_customer(f"cus_{next(counter)}")], "has_more": True})
        return ok({"data": [], "has_more": False})

    seen = serve(monkeypatch, handler)

    error = refusal("stripe", {})

    assert error.message == (
        f"Stripe has more than {MAX_PAGES} pages of customers, so Wardmind stopped reading."
    )
    assert sum(1 for r in seen if r.url.path == "/v1/customers") == MAX_PAGES


@pytest.mark.parametrize(
    ("invoices", "expected"),
    [
        ([], "0"),  # no open invoices: paid up
        ([stripe_invoice("in_1", "cus_1", None)], "0"),  # no due date
        ([stripe_invoice("in_1", "cus_1", days_ahead(3))], "0"),  # due in the future
        (
            [
                stripe_invoice("in_1", "cus_1", days_ago(2)),
                stripe_invoice("in_2", "cus_1", days_ago(45)),
                stripe_invoice("in_3", "cus_1", days_ahead(1)),
            ],
            "45",
        ),
    ],
)
def test_stripe_overdue_days_follow_the_stated_rule(
    monkeypatch: pytest.MonkeyPatch,
    stripe_today: None,
    invoices: list[dict[str, Any]],
    expected: str,
) -> None:
    serve(monkeypatch, stripe_api([stripe_customer("cus_1", "Acme")], invoices))

    table = fetch("stripe", {})

    assert table.rows == [["cus_1", "Acme", "", expected]]


# Chargebee --------------------------------------------------------------------------------------


def chargebee_api(customers: list[dict[str, Any]], invoices: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/customers":
            return ok({"list": [{"customer": c} for c in customers]})
        if request.url.path == "/api/v2/invoices":
            return ok({"list": [{"invoice": i} for i in invoices]})
        return httpx.Response(404)

    return handler


@pytest.fixture
def chargebee_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chargebee, "_today", lambda: DAY)


CHARGEBEE_SETTINGS = {"site": "acme"}


def test_chargebee_rows_show_each_customers_most_overdue_invoice(
    monkeypatch: pytest.MonkeyPatch, chargebee_today: None
) -> None:
    customers = [
        {"id": "cb-1", "company": "Acme Ltd", "email": "ap@acme.example", "first_name": "Ann"},
        {
            "id": "cb-2",
            "company": "",
            "email": "pat@outlook.com",
            "first_name": "Pat",
            "last_name": "Kim",
        },
        {"id": "cb-3", "company": "", "email": "", "first_name": "", "last_name": ""},
    ]
    invoices = [
        {"id": "inv-1", "customer_id": "cb-1", "due_date": days_ago(14), "date": days_ago(44)},
        {"id": "inv-2", "customer_id": "cb-1", "due_date": days_ago(3), "date": days_ago(33)},
        {"id": "inv-3", "customer_id": "cb-2", "date": days_ago(10)},  # no due date
        {"id": "inv-4", "customer_id": "cb-3", "due_date": days_ahead(10), "date": days_ago(1)},
        {"id": "inv-5", "customer_id": "cb-404", "due_date": days_ago(100), "date": days_ago(120)},
    ]
    seen = serve(monkeypatch, chargebee_api(customers, invoices))

    table = fetch("chargebee", CHARGEBEE_SETTINGS, SECRET)

    assert table.headers == CHARGEBEE_HEADERS
    assert table.rows == [
        ["cb-1", "Acme Ltd", "acme.example", "14"],
        ["cb-2", "Pat Kim", "", "0"],
        ["cb-3", "", "", "0"],
    ]
    assert importing.auto_map(table.headers) == SIGNAL_MAP
    assert_key_never_in_an_address(seen)
    assert {r.headers["authorization"] for r in seen} == {basic(SECRET, "")}
    invoice_request = next(r for r in seen if r.url.path == "/api/v2/invoices")
    assert invoice_request.url.params["status[in]"] == '["payment_due","not_paid"]'
    assert invoice_request.url.params["limit"] == "100"


def test_chargebee_follows_next_offset_for_customers_and_invoices(
    monkeypatch: pytest.MonkeyPatch, chargebee_today: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = request.url.params.get("offset")
        if request.url.path == "/api/v2/customers":
            if offset is None:
                first = {"customer": {"id": "cb-1", "company": "Acme"}}
                return ok({"list": [first], "next_offset": "cust-off-2"})
            return ok({"list": [{"customer": {"id": "cb-2", "company": "Globex"}}]})
        if offset is None:
            first = {"invoice": {"customer_id": "cb-1", "due_date": days_ago(4)}}
            return ok({"list": [first], "next_offset": "inv-off-2"})
        return ok({"list": [{"invoice": {"customer_id": "cb-2", "due_date": days_ago(8)}}]})

    seen = serve(monkeypatch, handler)

    table = fetch("chargebee", CHARGEBEE_SETTINGS, SECRET)

    assert table.rows == [["cb-1", "Acme", "", "4"], ["cb-2", "Globex", "", "8"]]
    customer_offsets = [r.url.params.get("offset") for r in seen if "customers" in r.url.path]
    invoice_offsets = [r.url.params.get("offset") for r in seen if "invoices" in r.url.path]
    assert customer_offsets == [None, "cust-off-2"]
    assert invoice_offsets == [None, "inv-off-2"]


@pytest.mark.parametrize(
    "site", ["Acme", "acme.example.com", "-acme", "acme_co", "a" * 64, "https://acme", " "]
)
def test_chargebee_a_bad_site_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    seen = serve(monkeypatch, chargebee_api([], []))

    error = refusal("chargebee", {"site": site}, SECRET)

    assert error.message == (
        "Chargebee's site is the part before .chargebee.com, such as acme. Check it."
    )
    assert seen == []


@pytest.mark.parametrize("secret", [None, ""])
def test_chargebee_without_a_key_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    seen = serve(monkeypatch, chargebee_api([], []))

    error = refusal("chargebee", CHARGEBEE_SETTINGS, secret)

    assert error.message == "Type the API key first."
    assert seen == []


def test_chargebee_a_refused_key_gets_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: fail(401))

    error = refusal("chargebee", CHARGEBEE_SETTINGS)

    assert error.message == (
        "acme.chargebee.com refused the key or password. Check it, and that it can read."
    )


def test_chargebee_stops_at_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok({"list": [{"customer": {"id": "cb-x"}}], "next_offset": "more"})

    seen = serve(monkeypatch, handler)

    error = refusal("chargebee", CHARGEBEE_SETTINGS)

    assert error.message == (
        f"Chargebee has more than {MAX_PAGES} pages of customers, so Wardmind stopped reading."
    )
    assert sum(1 for r in seen if r.url.path == "/api/v2/customers") == MAX_PAGES


# Zendesk ----------------------------------------------------------------------------------------

ZENDESK_SETTINGS = {"subdomain": "acme", "email": "ap@acme.example"}
ZENDESK_SIGN_IN = "ap@acme.example/token"


def zendesk_api(orgs_pages: list[dict[str, Any]], search_pages: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/organizations.json":
            pages = orgs_pages
        elif request.url.path == "/api/v2/search.json":
            pages = search_pages
        else:
            return httpx.Response(404)
        page = int(request.url.params.get("page", "1"))
        return ok(pages[page - 1])

    return handler


def test_zendesk_rows_count_urgent_open_tickets_per_organisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orgs = [
        {
            "id": 101,
            "name": "Acme Ltd",
            "domain_names": ["acme.example", "acme.test"],
            "external_id": "acct-101",
        },
        {"id": 102, "name": "Globex", "domain_names": [], "external_id": None},
        {"id": 103, "name": "Initech", "domain_names": ["initech.example"], "external_id": ""},
    ]
    tickets = [
        {"id": 1, "organization_id": 101},
        {"id": 2, "organization_id": 101},
        {"id": 3, "organization_id": 102},
        {"id": 4, "organization_id": None},
        {"id": 5, "organization_id": 999},  # an organisation not in the list
    ]
    orgs_pages = [{"organizations": orgs, "next_page": None}]
    search_pages = [{"results": tickets, "next_page": None}]
    seen = serve(monkeypatch, zendesk_api(orgs_pages, search_pages))

    table = fetch("zendesk", ZENDESK_SETTINGS, SECRET)

    assert table.headers == ZENDESK_HEADERS
    assert table.rows == [
        ["acct-101", "Acme Ltd", "acme.example", "2"],
        ["102", "Globex", "", "1"],
        ["103", "Initech", "initech.example", "0"],
    ]
    assert importing.auto_map(table.headers) == {
        "external_id": "ID",
        "name": "Account name",
        "domain": "Website",
        "signal:open_tickets_sev1": "Open severity-1 tickets",
    }
    assert_key_never_in_an_address(seen)
    assert {r.headers["authorization"] for r in seen} == {basic(ZENDESK_SIGN_IN, SECRET)}
    search = next(r for r in seen if r.url.path == "/api/v2/search.json")
    assert search.url.params["query"] == "type:ticket status<solved priority:urgent"
    assert search.url.params["per_page"] == "100"


def test_zendesk_follows_next_page_links_on_the_same_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orgs_pages = [
        {
            "organizations": [{"id": 101, "name": "Acme", "domain_names": []}],
            "next_page": "https://acme.zendesk.com/api/v2/organizations.json?page=2&per_page=100",
        },
        {"organizations": [{"id": 102, "name": "Globex"}], "next_page": None},
    ]
    search_pages = [
        {
            "results": [{"id": 1, "organization_id": 101}],
            "next_page": "https://acme.zendesk.com/api/v2/search.json?page=2&per_page=100",
        },
        {"results": [{"id": 2, "organization_id": 101}], "next_page": None},
    ]
    seen = serve(monkeypatch, zendesk_api(orgs_pages, search_pages))

    table = fetch("zendesk", ZENDESK_SETTINGS, SECRET)

    assert table.rows == [["101", "Acme", "", "2"], ["102", "Globex", "", "0"]]
    orgs = [r for r in seen if r.url.path == "/api/v2/organizations.json"]
    searches = [r for r in seen if r.url.path == "/api/v2/search.json"]
    assert len(orgs) == 2
    assert len(searches) == 2
    assert {r.headers["authorization"] for r in seen} == {basic(ZENDESK_SIGN_IN, SECRET)}


def test_zendesk_a_next_page_on_another_site_is_refused_before_it_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orgs_pages = [
        {
            "organizations": [{"id": 101, "name": "Acme"}],
            "next_page": "https://evil.example.net/api/v2/organizations.json?page=2",
        }
    ]
    seen = serve(monkeypatch, zendesk_api(orgs_pages, []))

    error = refusal("zendesk", ZENDESK_SETTINGS, SECRET)

    assert error.message == (
        "The next page is on a different site, so Wardmind won't send your key there."
    )
    assert len(seen) == 1
    assert {r.url.host for r in seen} == {"acme.zendesk.com"}


def test_zendesk_a_redirect_to_another_site_never_gets_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "elsewhere.example.net":
            return ok({"organizations": [], "results": []})
        if request.url.path == "/api/v2/organizations.json":
            elsewhere = "https://elsewhere.example.net/api/v2/organizations.json"
            return httpx.Response(302, headers={"location": elsewhere})
        return ok({"results": [], "next_page": None})

    seen = serve(monkeypatch, handler)

    fetch("zendesk", ZENDESK_SETTINGS, SECRET)

    elsewhere = [r for r in seen if r.url.host == "elsewhere.example.net"]
    assert elsewhere
    assert all("authorization" not in r.headers for r in elsewhere)


def test_zendesk_stops_at_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        following = f"https://acme.zendesk.com/api/v2/organizations.json?page={page + 1}"
        return ok({"organizations": [], "next_page": following})

    seen = serve(monkeypatch, handler)

    error = refusal("zendesk", ZENDESK_SETTINGS, SECRET)

    assert error.message == (
        f"Zendesk has more than {MAX_PAGES} pages of organizations, so Wardmind stopped reading."
    )
    assert sum(1 for r in seen if r.url.path == "/api/v2/organizations.json") == MAX_PAGES


@pytest.mark.parametrize("subdomain", ["Acme", "acme.zendesk.com", "-acme", "a" * 64, ""])
def test_zendesk_a_bad_subdomain_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, subdomain: str
) -> None:
    seen = serve(monkeypatch, zendesk_api([], []))

    error = refusal("zendesk", {"subdomain": subdomain, "email": "ap@acme.example"}, SECRET)

    assert error.message == (
        "Zendesk's subdomain is the part before .zendesk.com, such as acme. Check it."
    )
    assert seen == []


@pytest.mark.parametrize("email", ["acme.example", "", "   "])
def test_zendesk_an_email_without_an_at_sign_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    seen = serve(monkeypatch, zendesk_api([], []))

    error = refusal("zendesk", {"subdomain": "acme", "email": email}, SECRET)

    assert error.message == "Type the email you sign in to Zendesk with."
    assert seen == []


@pytest.mark.parametrize("secret", [None, ""])
def test_zendesk_without_a_token_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    seen = serve(monkeypatch, zendesk_api([], []))

    error = refusal("zendesk", ZENDESK_SETTINGS, secret)

    assert error.message == "Type the API token first."
    assert seen == []


def test_zendesk_a_refused_token_gets_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: fail(401))

    error = refusal("zendesk", ZENDESK_SETTINGS, SECRET)

    assert error.message == (
        "acme.zendesk.com refused the key or password. Check it, and that it can read."
    )


# Freshdesk --------------------------------------------------------------------------------------

FRESHDESK_SETTINGS = {"domain": "acme"}
FRESHDESK_URGENT = '"priority:4 AND (status:2 OR status:3)"'


def freshdesk_api(companies_pages: list[list[dict[str, Any]]], tickets_pages: list[list[dict]]):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if request.url.path == "/api/v2/companies":
            return ok(companies_pages[page - 1])
        if request.url.path == "/api/v2/search/tickets":
            return ok({"results": tickets_pages[page - 1], "total": 0})
        return httpx.Response(404)

    return handler


def test_freshdesk_rows_count_urgent_open_tickets_per_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    companies = [
        {"id": 11, "name": "Acme Ltd", "domains": ["acme.example"]},
        {"id": 12, "name": "Globex", "domains": []},
    ]
    tickets = [
        {"id": 1, "company_id": 11},
        {"id": 2, "company_id": 11},
        {"id": 3, "company_id": None},
        {"id": 4, "company_id": 12},
    ]
    seen = serve(monkeypatch, freshdesk_api([companies], [tickets]))

    table = fetch("freshdesk", FRESHDESK_SETTINGS, SECRET)

    assert table.headers == FRESHDESK_HEADERS
    assert table.rows == [["11", "Acme Ltd", "acme.example", "2"], ["12", "Globex", "", "1"]]
    assert importing.auto_map(table.headers) == {
        "external_id": "ID",
        "name": "Account name",
        "domain": "Website",
        "signal:open_tickets_sev1": "Open severity-1 tickets",
    }
    assert_key_never_in_an_address(seen)
    assert {r.headers["authorization"] for r in seen} == {basic(SECRET, "X")}
    search = next(r for r in seen if r.url.path == "/api/v2/search/tickets")
    assert search.url.params["query"] == FRESHDESK_URGENT


def test_freshdesk_reads_companies_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    full = [{"id": i, "name": f"Co {i}", "domains": []} for i in range(1, 101)]
    rest = [{"id": i, "name": f"Co {i}", "domains": []} for i in range(101, 104)]
    seen = serve(monkeypatch, freshdesk_api([full, rest], [[]]))

    table = fetch("freshdesk", FRESHDESK_SETTINGS, SECRET)

    assert len(table.rows) == 103
    assert table.rows[0] == ["1", "Co 1", "", "0"]
    assert table.rows[-1] == ["103", "Co 103", "", "0"]
    company_pages = [r.url.params["page"] for r in seen if r.url.path == "/api/v2/companies"]
    assert company_pages == ["1", "2"]
    assert {r.url.params["per_page"] for r in seen if "companies" in r.url.path} == {"100"}


def test_freshdesk_reads_search_pages_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    first = [{"id": n, "company_id": 11} for n in range(30)]
    second = [{"id": n, "company_id": 11} for n in range(30, 35)]
    seen = serve(
        monkeypatch, freshdesk_api([[{"id": 11, "name": "Acme", "domains": []}]], [first, second])
    )

    table = fetch("freshdesk", FRESHDESK_SETTINGS, SECRET)

    assert table.rows == [["11", "Acme", "", "35"]]
    search_pages = [r.url.params["page"] for r in seen if "search" in r.url.path]
    assert search_pages == ["1", "2"]


def test_freshdesk_search_stops_after_ten_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    full_page = [{"id": n, "company_id": 11} for n in range(30)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/companies":
            return ok([{"id": 11, "name": "Acme", "domains": []}])
        return ok({"results": full_page, "total": 999})

    seen = serve(monkeypatch, handler)

    table = fetch("freshdesk", FRESHDESK_SETTINGS, SECRET)

    assert table.rows == [["11", "Acme", "", "300"]]
    assert sum(1 for r in seen if "search" in r.url.path) == 10


def test_freshdesk_a_refused_key_gets_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: fail(403))

    error = refusal("freshdesk", FRESHDESK_SETTINGS, SECRET)

    assert error.message == (
        "acme.freshdesk.com refused the key or password. Check it, and that it can read."
    )


def test_freshdesk_stops_at_max_companies_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    full = [{"id": i, "name": "x", "domains": []} for i in range(100)]
    seen = serve(monkeypatch, freshdesk_api([full] * MAX_PAGES, [[]]))

    error = refusal("freshdesk", FRESHDESK_SETTINGS, SECRET)

    assert error.message == (
        f"Freshdesk has more than {MAX_PAGES * 100:,} companies, so Wardmind stopped reading."
    )
    assert sum(1 for r in seen if r.url.path == "/api/v2/companies") == MAX_PAGES


@pytest.mark.parametrize("domain", ["Acme", "acme.freshdesk.com", "-acme", "a" * 64, ""])
def test_freshdesk_a_bad_helpdesk_name_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    seen = serve(monkeypatch, freshdesk_api([[]], [[]]))

    error = refusal("freshdesk", {"domain": domain}, SECRET)

    assert error.message == (
        "Freshdesk's helpdesk name is the part before .freshdesk.com, such as acme. Check it."
    )
    assert seen == []


@pytest.mark.parametrize("secret", [None, ""])
def test_freshdesk_without_a_key_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    seen = serve(monkeypatch, freshdesk_api([[]], [[]]))

    error = refusal("freshdesk", FRESHDESK_SETTINGS, secret)

    assert error.message == "Type the API key first."
    assert seen == []


# PostHog ----------------------------------------------------------------------------------------

POSTHOG_SETTINGS = {"project_id": "12345", "group_key": "company_id"}
POSTHOG_URL = "https://us.posthog.com/api/projects/12345/query/"


def posthog_api(columns: list[str], results: list[list[Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok({"columns": columns, "results": results})

    return handler


def sent_query(request: httpx.Request) -> str:
    body = json.loads(request.content)
    assert body["query"]["kind"] == "HogQLQuery"
    return body["query"]["query"]


def test_posthog_default_query_returns_usage_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    columns = ["ID", "Usage trend", "Days since last login"]
    results = [["acme.example", 25, 2], ["globex.example", -40.5, None]]
    seen = serve(monkeypatch, posthog_api(columns, results))

    table = fetch("posthog", POSTHOG_SETTINGS, SECRET)

    assert table.headers == columns
    assert table.rows == [["acme.example", "25", "2"], ["globex.example", "-40.5", ""]]
    assert importing.auto_map(table.headers) == {
        "external_id": "ID",
        "signal:usage_trend_30d": "Usage trend",
        "signal:days_since_last_login": "Days since last login",
    }
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == POSTHOG_URL
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in request.content.decode()
    query = sent_query(request)
    assert "properties.company_id AS ID" in query
    assert "AND properties.company_id IS NOT NULL" in query
    assert "LIMIT 50000" in query


def test_posthog_a_dollar_group_key_is_named_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, posthog_api(["ID"], []))

    fetch("posthog", {"project_id": "12345", "group_key": "$group_0"}, SECRET)

    query = sent_query(seen[0])
    assert "$group_0 AS ID" in query
    assert "properties.$group_0" not in query


def test_posthog_a_custom_query_is_sent_exactly_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = "SELECT person_id AS ID, 5 AS `Seats in use`\nFROM events\nLIMIT 10"
    seen = serve(monkeypatch, posthog_api(["ID", "Seats in use"], [["p-1", 5]]))

    table = fetch("posthog", {**POSTHOG_SETTINGS, "query": custom}, SECRET)

    assert sent_query(seen[0]) == custom
    assert table.rows == [["p-1", "5"]]


def test_posthog_a_blank_custom_query_uses_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, posthog_api(["ID"], []))

    fetch("posthog", {**POSTHOG_SETTINGS, "query": "   "}, SECRET)

    assert "properties.company_id AS ID" in sent_query(seen[0])


def test_posthog_the_host_setting_is_used_without_a_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = serve(monkeypatch, posthog_api(["ID"], []))

    fetch("posthog", {**POSTHOG_SETTINGS, "host": "https://eu.posthog.com/"}, SECRET)

    assert str(seen[0].url) == "https://eu.posthog.com/api/projects/12345/query/"


@pytest.mark.parametrize(
    "group_key", ["company'id", "company id", "company-id", "a;b", "", "x" * 65, "$"]
)
def test_posthog_a_group_key_outside_the_safe_pattern_is_refused(
    monkeypatch: pytest.MonkeyPatch, group_key: str
) -> None:
    seen = serve(monkeypatch, posthog_api(["ID"], []))

    error = refusal("posthog", {"project_id": "12345", "group_key": group_key}, SECRET)

    assert error.message == (
        "The company property can use only letters, numbers, underscores and dots, "
        "with an optional $ at the start, such as $group_0 or company_id."
    )
    assert seen == []


@pytest.mark.parametrize("project_id", ["abc", "12 34", "", "12a"])
def test_posthog_a_project_id_that_is_not_digits_is_refused(
    monkeypatch: pytest.MonkeyPatch, project_id: str
) -> None:
    seen = serve(monkeypatch, posthog_api(["ID"], []))

    error = refusal("posthog", {"project_id": project_id, "group_key": "company_id"}, SECRET)

    assert error.message == "The project ID is the number in your project's address. Check it."
    assert seen == []


@pytest.mark.parametrize("secret", [None, ""])
def test_posthog_without_a_key_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    seen = serve(monkeypatch, posthog_api(["ID"], []))

    error = refusal("posthog", POSTHOG_SETTINGS, secret)

    assert error.message == "Type the personal API key first."
    assert seen == []


def test_posthog_a_refused_key_gets_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: fail(401))

    error = refusal("posthog", POSTHOG_SETTINGS, SECRET)

    assert error.message == (
        "us.posthog.com refused the key or password. Check it, and that it can read."
    )


def test_posthog_a_reply_that_is_not_json_is_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: httpx.Response(200, content=b"<html>oops</html>"))

    error = refusal("posthog", POSTHOG_SETTINGS, SECRET)

    assert error.message == "PostHog didn't send a result Wardmind can read."


def test_posthog_a_query_with_too_many_rows_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [[f"c{n}", 1, 1] for n in range(MAX_ROWS + 1)]
    serve(monkeypatch, posthog_api(["ID", "Usage trend", "Days since last login"], results))

    error = refusal("posthog", POSTHOG_SETTINGS, SECRET)

    assert error.message == (f"This query returns more than {MAX_ROWS:,} rows. Narrow it down.")


def test_posthog_an_empty_result_keeps_its_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, posthog_api(["ID", "Usage trend"], []))

    table = fetch("posthog", POSTHOG_SETTINGS, SECRET)

    assert table.headers == ["ID", "Usage trend"]
    assert table.rows == []


# Airtable ---------------------------------------------------------------------------------------

AIRTABLE_BASE = "appAbCdEfGhIjKlMn"
AIRTABLE_SETTINGS = {"base_id": AIRTABLE_BASE, "table": "Customers"}


def airtable_api(pages: dict[str | None, dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(pages[request.url.params.get("offset")])

    return handler


def test_airtable_rows_come_from_every_page_of_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: dict[str | None, dict[str, Any]] = {
        None: {
            "records": [
                {
                    "id": "rec1",
                    "fields": {
                        "Account name": "Acme Ltd",
                        "ID": "acct-1",
                        "Website": "acme.example",
                        "Usage trend": -12,
                        "Days since last login": 3,
                    },
                },
                {"id": "rec2", "fields": {}},  # a blank record is no row
            ],
            "offset": "itr-page2",
        },
        "itr-page2": {
            "records": [
                {
                    "id": "rec3",
                    "fields": {"Account name": "Globex", "ID": "gbx-9", "Usage trend": 5},
                }
            ]
        },
    }
    seen = serve(monkeypatch, airtable_api(pages))

    table = fetch("airtable", AIRTABLE_SETTINGS, SECRET)

    assert table.headers == [
        "Account name",
        "ID",
        "Website",
        "Usage trend",
        "Days since last login",
    ]
    assert table.rows == [
        ["Acme Ltd", "acct-1", "acme.example", "-12", "3"],
        ["Globex", "gbx-9", "", "5", ""],
    ]
    assert importing.auto_map(table.headers) == {
        "name": "Account name",
        "external_id": "ID",
        "domain": "Website",
        "signal:usage_trend_30d": "Usage trend",
        "signal:days_since_last_login": "Days since last login",
    }
    assert [r.url.params.get("offset") for r in seen] == [None, "itr-page2"]
    assert {r.url.params["pageSize"] for r in seen} == {"100"}
    assert_key_never_in_an_address(seen)
    assert {r.headers["authorization"] for r in seen} == {f"Bearer {SECRET}"}


def test_airtable_the_table_name_is_encoded_and_the_view_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = serve(monkeypatch, airtable_api({None: {"records": []}}))

    fetch(
        "airtable",
        {
            "base_id": AIRTABLE_BASE,
            "table": "Customer Health & Notes",
            "view": "All accounts",
        },
        SECRET,
    )

    url = str(seen[0].url)
    assert f"/v0/{AIRTABLE_BASE}/Customer%20Health%20%26%20Notes?" in url
    assert seen[0].url.params["view"] == "All accounts"


def test_airtable_a_refused_token_gets_a_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: fail(401))

    error = refusal("airtable", AIRTABLE_SETTINGS, SECRET)

    assert error.message == (
        "api.airtable.com refused the key or password. Check it, and that it can read."
    )


@pytest.mark.parametrize(
    "base_id", ["app123", "AppAbCdEfGhIjKlMn", "appAbCdEfGhIjKlM", "appAbCdEfGhIjKlMn!", ""]
)
def test_airtable_a_bad_base_id_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, base_id: str
) -> None:
    seen = serve(monkeypatch, airtable_api({None: {"records": []}}))

    error = refusal("airtable", {"base_id": base_id, "table": "Customers"}, SECRET)

    assert error.message == (
        "The base ID starts with app and has 17 letters and numbers. Copy it from the base's "
        "API page."
    )
    assert seen == []


def test_airtable_a_blank_table_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = serve(monkeypatch, airtable_api({None: {"records": []}}))

    error = refusal("airtable", {"base_id": AIRTABLE_BASE, "table": "  "}, SECRET)

    assert error.message == "Type the table name or ID first."
    assert seen == []


@pytest.mark.parametrize("secret", [None, ""])
def test_airtable_without_a_token_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    seen = serve(monkeypatch, airtable_api({None: {"records": []}}))

    error = refusal("airtable", AIRTABLE_SETTINGS, secret)

    assert error.message == "Type the personal access token first."
    assert seen == []


def test_airtable_stops_at_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok({"records": [{"id": "rec", "fields": {"ID": "x"}}], "offset": "more"})

    seen = serve(monkeypatch, handler)

    error = refusal("airtable", AIRTABLE_SETTINGS, SECRET)

    assert error.message == (
        f"Airtable has more than {MAX_PAGES} pages of rows, so Wardmind stopped reading."
    )
    assert len(seen) == MAX_PAGES


# Every provider ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_id", ["stripe", "chargebee", "zendesk", "freshdesk", "posthog", "airtable"]
)
def test_the_product_providers_have_no_source_file_time(provider_id: str) -> None:
    provider = get_provider(provider_id)

    assert provider is not None
    assert provider.source_mtime({}) is None
