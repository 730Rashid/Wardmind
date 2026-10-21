"""HubSpotClient: base URL rules, auth, paths, retries, error messages and paging.

Everything runs on httpx.MockTransport. No test touches the network, and no test sleeps.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cs_brief.core.connectors.hubspot import (
    API_BASE,
    HubSpotAuthError,
    HubSpotClient,
    HubSpotError,
    HubSpotRateLimited,
    HubSpotScopeError,
    HubSpotUnavailable,
)
from hubspot_fake import fake_transport, fixture

TOKEN = "pat-na1-TESTTOKEN-0000-1111-2222-333344445555"
OWNERS_PAGE = {"results": [{"id": "77", "firstName": "Dana", "lastName": "Whitfield"}]}
NOT_FOUND = {"status": "error", "message": "Not found"}
SERVER_ERROR = {"status": "error", "message": "Something broke"}
BAD_URL = "HubSpot requests can only go to api.hubapi.com or a local test server."


def script(*steps):
    """A transport that answers each request with the next step. The last step repeats.

    A step is (status, json_body, headers), or an exception to raise instead.
    """
    queue = list(steps)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        status, body, headers = step
        return httpx.Response(status, json=body, headers=headers)

    return httpx.MockTransport(handler), seen


def make_client(transport, *, max_retries: int = 3, sleeps: list[float] | None = None):
    sleep = sleeps.append if sleeps is not None else (lambda seconds: None)
    return HubSpotClient(TOKEN, transport=transport, max_retries=max_retries, sleep=sleep)


# Base URL rules


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (API_BASE, API_BASE),
        (API_BASE + "/", API_BASE),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://127.0.0.1:8799", "http://127.0.0.1:8799"),
        ("https://localhost:9443/", "https://localhost:9443"),
        ("http://localhost", "http://localhost"),
    ],
)
def test_accepts_hubspot_and_local_test_servers(given, expected):
    client = HubSpotClient(TOKEN, base_url=given, transport=httpx.MockTransport(lambda r: None))
    assert repr(client) == f"HubSpotClient(base_url={expected!r})"


@pytest.mark.parametrize(
    "given",
    [
        "https://api.hubapi.com.evil.example",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "ftp://localhost",
        "http://api.hubapi.com",
        "https://evil.example",
        "https://api.hubapi.com/crm",
        "https://api.hubapi.com?x=1",
        "https://user:pw@api.hubapi.com",
        "https://127.0.0.1@evil.example",
        "not a url",
        "",
    ],
)
def test_rejects_every_other_base_url(given):
    with pytest.raises(ValueError) as info:
        HubSpotClient(TOKEN, base_url=given)
    assert str(info.value) == BAD_URL


# Auth and URLs


def test_token_travels_only_in_the_authorization_header():
    transport, seen = fake_transport()
    client = make_client(transport)
    client.request("POST", "companies_search", json={"filterGroups": [], "limit": 1})
    request = seen[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["User-Agent"] == "cs-brief"
    assert request.headers["Accept"] == "application/json"
    assert TOKEN not in str(request.url)
    assert TOKEN.encode() not in request.content
    assert TOKEN not in repr(client)


def test_dated_path_is_tried_first():
    transport, seen = fake_transport()
    make_client(transport).request("GET", "owners", params={"limit": 1})
    assert seen[0].url.path == "/crm/owners/2026-09/"


# Path fallback


def test_falls_back_to_the_legacy_path_and_remembers_it():
    transport, seen = fake_transport(dated=False)
    client = make_client(transport)
    client.request("GET", "owners", params={"limit": 100})
    client.request("GET", "owners", params={"limit": 100})
    paths = [request.url.path for request in seen]
    assert paths == ["/crm/owners/2026-09/", "/crm/v3/owners/", "/crm/v3/owners/"]


def test_reports_an_unrecognised_request_when_both_paths_return_404():
    transport, seen = script((404, NOT_FOUND, None))
    client = make_client(transport)
    with pytest.raises(HubSpotError) as info:
        client.request("GET", "owners")
    assert type(info.value) is HubSpotError
    assert info.value.message == (
        "HubSpot didn't recognise a request cs-brief made (owners). cs-brief may need an update."
    )
    assert [request.url.path for request in seen] == ["/crm/owners/2026-09/", "/crm/v3/owners/"]


# Retries


def test_429_waits_for_retry_after_then_succeeds():
    sleeps: list[float] = []
    limited = (429, fixture("error_429.json"), {"Retry-After": "2"})
    transport, seen = script(limited, limited, (200, OWNERS_PAGE, None))
    client = make_client(transport, sleeps=sleeps)
    assert client.request("GET", "owners") == OWNERS_PAGE
    assert sleeps == [2, 2]
    assert len(seen) == 3


def test_retry_after_is_capped_at_30_seconds():
    sleeps: list[float] = []
    transport, _ = script(
        (429, fixture("error_429.json"), {"Retry-After": "120"}),
        (200, OWNERS_PAGE, None),
    )
    make_client(transport, sleeps=sleeps).request("GET", "owners")
    assert sleeps == [30]


def test_429_without_retry_after_backs_off_exponentially():
    sleeps: list[float] = []
    limited = (429, fixture("error_429.json"), None)
    transport, _ = script(limited, limited, (200, OWNERS_PAGE, None))
    make_client(transport, sleeps=sleeps).request("GET", "owners")
    assert sleeps == [1, 2]


def test_429_gives_up_after_max_retries():
    sleeps: list[float] = []
    transport, seen = script((429, fixture("error_429.json"), None))
    client = make_client(transport, sleeps=sleeps)
    with pytest.raises(HubSpotRateLimited) as info:
        client.request("GET", "owners")
    assert (
        info.value.message == "HubSpot is limiting requests right now. Wait a minute and try again."
    )
    assert sleeps == [1, 2, 4]
    assert len(seen) == 4


def test_5xx_is_retried_then_succeeds():
    sleeps: list[float] = []
    transport, _ = script(
        (503, SERVER_ERROR, None),
        (500, SERVER_ERROR, None),
        (200, OWNERS_PAGE, None),
    )
    assert make_client(transport, sleeps=sleeps).request("GET", "owners") == OWNERS_PAGE
    assert sleeps == [1, 2]


def test_5xx_gives_up_after_max_retries():
    sleeps: list[float] = []
    transport, seen = script((502, SERVER_ERROR, None))
    client = make_client(transport, sleeps=sleeps)
    with pytest.raises(HubSpotUnavailable) as info:
        client.request("GET", "owners")
    assert (
        info.value.message
        == "Couldn't reach HubSpot. Check your internet connection and try again."
    )
    assert sleeps == [1, 2, 4]
    assert len(seen) == 4


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("took too long")],
    ids=["connect", "timeout"],
)
def test_network_errors_are_retried_then_succeed(error):
    sleeps: list[float] = []
    transport, _ = script(error, (200, OWNERS_PAGE, None))
    assert make_client(transport, sleeps=sleeps).request("GET", "owners") == OWNERS_PAGE
    assert sleeps == [1]


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("took too long")],
    ids=["connect", "timeout"],
)
def test_network_errors_give_up_as_unavailable(error):
    sleeps: list[float] = []
    transport, seen = script(error)
    client = make_client(transport, sleeps=sleeps)
    with pytest.raises(HubSpotUnavailable) as info:
        client.request("GET", "owners")
    assert (
        info.value.message
        == "Couldn't reach HubSpot. Check your internet connection and try again."
    )
    assert sleeps == [1, 2, 4]
    assert len(seen) == 4


# Client errors


def test_401_fails_at_once_with_the_access_token_message():
    sleeps: list[float] = []
    transport, seen = script((401, fixture("error_401.json"), None))
    with pytest.raises(HubSpotAuthError) as info:
        make_client(transport, sleeps=sleeps).request("GET", "owners")
    assert info.value.message == (
        "HubSpot didn't accept the access token. It may be mistyped, revoked or expired. "
        "Save a new one on the Connections page."
    )
    assert len(seen) == 1
    assert sleeps == []


def test_403_names_the_one_missing_scope():
    transport, _ = script((403, fixture("error_403_tickets.json"), None))
    with pytest.raises(HubSpotScopeError) as info:
        make_client(transport).request("POST", "tickets_search", json={})
    assert info.value.message == (
        'This key is missing the "tickets" scope. Add it in HubSpot and test again.'
    )
    assert info.value.missing == ["tickets"]


def test_403_names_every_missing_scope_read_from_requiredScopes():
    transport, _ = script((403, fixture("error_403_two_scopes.json"), None))
    with pytest.raises(HubSpotScopeError) as info:
        make_client(transport).request("GET", "owners")
    assert info.value.message == (
        'This key is missing the "tickets" and "crm.objects.owners.read" scopes. '
        "Add them in HubSpot and test again."
    )
    assert info.value.missing == ["tickets", "crm.objects.owners.read"]


def test_403_without_scope_details_gives_the_generic_message():
    transport, _ = script((403, {"status": "error", "message": "Forbidden"}, None))
    with pytest.raises(HubSpotScopeError) as info:
        make_client(transport).request("GET", "owners")
    assert info.value.message == (
        "This key doesn't have permission for that. Check its scopes in HubSpot."
    )
    assert info.value.missing == []


def test_other_4xx_quotes_hubspot_message_truncated_to_200_characters():
    transport, _ = script((400, {"status": "error", "message": "x" * 500}, None))
    with pytest.raises(HubSpotError) as info:
        make_client(transport).request("GET", "owners")
    prefix = "HubSpot rejected a request: "
    assert info.value.message.startswith(prefix)
    assert len(info.value.message) == len(prefix) + 200


def test_token_is_stripped_from_a_hubspot_message_that_echoes_it():
    transport, _ = script((400, {"status": "error", "message": f"Bad key {TOKEN} sent"}, None))
    with pytest.raises(HubSpotError) as info:
        make_client(transport).request("GET", "owners")
    assert info.value.message == "HubSpot rejected a request: Bad key … sent"


def test_unreadable_success_body_is_a_hubspot_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(HubSpotError):
        make_client(transport).request("GET", "owners")


# Paging


def test_paginate_get_moves_after_into_the_query_string():
    transport, seen = script(
        (200, {"results": [{"id": "77"}], "paging": {"next": {"after": "p2"}}}, None),
        (200, {"results": [{"id": "82"}]}, None),
    )
    items = list(make_client(transport).paginate("GET", "owners", params={"limit": 100}))
    assert [item["id"] for item in items] == ["77", "82"]
    assert "after" not in seen[0].url.params
    assert seen[1].url.params["after"] == "p2"
    assert seen[1].url.params["limit"] == "100"


def test_paginate_post_moves_after_into_the_json_body():
    transport, seen = script(
        (200, {"results": [{"id": "1"}], "paging": {"next": {"after": "100"}}}, None),
        (200, {"results": [{"id": "2"}]}, None),
    )
    items = list(make_client(transport).paginate("POST", "tickets_search", json={"limit": 100}))
    assert [item["id"] for item in items] == ["1", "2"]
    assert json.loads(seen[0].content) == {"limit": 100}
    assert json.loads(seen[1].content) == {"limit": 100, "after": "100"}


def test_paginate_stops_if_hubspot_repeats_a_cursor():
    transport, _ = script(
        (200, {"results": [], "paging": {"next": {"after": "p2"}}}, None),
        (200, {"results": [], "paging": {"next": {"after": "p2"}}}, None),
    )
    with pytest.raises(HubSpotError) as info:
        list(make_client(transport).paginate("GET", "owners"))
    assert info.value.message.startswith("HubSpot kept sending the same page")


# Token never leaks


LEAKY = [
    ("401", lambda: script((401, fixture("error_401.json"), None)), HubSpotAuthError),
    (
        "403 with the token as a scope name",
        lambda: script((403, {"errors": [{"context": {"requiredScopes": [TOKEN]}}]}, None)),
        HubSpotScopeError,
    ),
    (
        "429 that echoes the token",
        lambda: script((429, {"message": f"limit for {TOKEN}"}, {"Retry-After": "0"})),
        HubSpotRateLimited,
    ),
    (
        "503 that echoes the token",
        lambda: script((503, {"message": f"down for {TOKEN}"}, None)),
        HubSpotUnavailable,
    ),
    (
        "network error that echoes the token",
        lambda: script(httpx.ConnectError(f"no route {TOKEN}")),
        HubSpotUnavailable,
    ),
    (
        "400 that echoes the token",
        lambda: script((400, {"message": f"bad token {TOKEN}"}, None)),
        HubSpotError,
    ),
    ("404 on both paths", lambda: script((404, {"message": TOKEN}, None)), HubSpotError),
]


@pytest.mark.parametrize(
    ("factory", "expected"),
    [pytest.param(factory, expected, id=label) for label, factory, expected in LEAKY],
)
def test_token_never_appears_in_an_error(factory, expected):
    transport, _ = factory()
    client = make_client(transport, max_retries=1)
    with pytest.raises(expected) as info:
        client.request("GET", "owners")
    error = info.value
    assert TOKEN not in str(error)
    assert TOKEN not in error.message
    assert TOKEN not in repr(error)
    assert TOKEN not in repr(client)
