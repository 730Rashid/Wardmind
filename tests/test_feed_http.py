"""The one HTTP client feeds use: which addresses it may visit, redirects, the size cap, plain
messages for each status, and the rule that a secret never reaches an error text.

Every request goes through an httpx.MockTransport set on feeds.http.transport_factory, so nothing
here reaches the network.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from wardmind.core.feeds import http as feed_http
from wardmind.core.feeds.http import MAX_REDIRECTS, FeedError, FeedHttp, check_url, same_origin

SECRET = "s3cr3t-token-value-xyz"


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route every request to `handler`; returns the list the requests are appended to."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return seen


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1/accounts",
        "https://exports.example.com/a.csv?token=abc",
        "http://127.0.0.1:8000/accounts.csv",
        "http://localhost/accounts.csv",
        "http://[::1]:8000/accounts.csv",
    ],
)
def test_allowed_addresses_pass(url: str) -> None:
    assert check_url(url) == url


def test_surrounding_spaces_are_stripped() -> None:
    assert (
        check_url("  https://exports.example.com/a.csv \n") == "https://exports.example.com/a.csv"
    )


@pytest.mark.parametrize("url", ["http://example.com/a.csv", "ftp://example.com/a.csv"])
def test_plain_http_and_other_schemes_are_refused_for_other_hosts(url: str) -> None:
    with pytest.raises(FeedError, match="must start with https://"):
        check_url(url)


@pytest.mark.parametrize(
    "url", ["https://user:pw@example.com/a.csv", "https://someone@example.com/a.csv"]
)
def test_credentials_in_the_address_are_refused(url: str) -> None:
    with pytest.raises(FeedError, match="own box, not in the web address"):
        check_url(url)


@pytest.mark.parametrize(
    "url", ["", "   ", "not a url", "https://", "https:///accounts.csv", "http://[bad/x"]
)
def test_garbage_is_refused_as_not_a_web_address(url: str) -> None:
    with pytest.raises(FeedError) as caught:
        check_url(url)
    assert "web address" in caught.value.message


def test_a_redirect_is_followed_and_its_target_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"ok")

    seen = _serve(monkeypatch, handler)
    response = FeedHttp().get("https://exports.example.com/start")
    assert response.content == b"ok"
    assert [request.url.path for request in seen] == ["/start", "/final"]


def test_a_redirect_to_plain_http_on_another_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://elsewhere.example.com/a.csv"})

    _serve(monkeypatch, handler)
    with pytest.raises(FeedError, match="must start with https://"):
        FeedHttp().get("https://exports.example.com/a.csv")


def test_too_many_redirects_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": str(request.url)}),
    )
    with pytest.raises(FeedError, match="redirected too many times"):
        FeedHttp().get("https://exports.example.com/loop")
    assert len(seen) == MAX_REDIRECTS + 1


def test_a_redirect_without_a_location_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(301))
    with pytest.raises(FeedError, match="broken redirect"):
        FeedHttp().get("https://exports.example.com/a.csv")


def test_a_query_post_is_never_followed_through_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(307, headers={"location": "/elsewhere"}))
    with pytest.raises(FeedError, match="redirected a query"):
        FeedHttp().post_query("https://api.example.com/query", json={"q": "SELECT 1"})


def test_get_is_sent_as_get_and_post_query_as_post(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, content=b"{}"))
    http = FeedHttp()
    http.get("https://api.example.com/a")
    http.post_query("https://api.example.com/q", json={"q": "x"})
    assert [request.method for request in seen] == ["GET", "POST"]


def test_a_body_at_the_size_cap_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_http, "MAX_BYTES", 10)
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 10))
    assert FeedHttp().get("https://exports.example.com/a.csv").content == b"x" * 10


def test_a_body_over_the_size_cap_is_an_error_not_a_cut_off_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feed_http, "MAX_BYTES", 10)
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(FeedError, match="more than 25 MB"):
        FeedHttp().get("https://exports.example.com/a.csv")


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (401, "refused the key or password"),
        (403, "refused the key or password"),
        (404, "nothing at that address"),
        (429, "limiting requests"),
        (500, "had a problem (500)"),
        (503, "had a problem (503)"),
        (418, "said no (418)"),
    ],
)
def test_each_status_gets_a_plain_message(
    monkeypatch: pytest.MonkeyPatch, status: int, words: str
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(FeedError) as caught:
        FeedHttp().get("https://exports.example.com/a.csv")
    assert words in caught.value.message
    assert "exports.example.com" in caught.value.message


def test_the_secret_is_scrubbed_from_every_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # The host name carries the secret, so every message built from it must hide it.
    url = f"https://{SECRET}.example.com/a.csv"
    _serve(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(FeedError) as caught:
        FeedHttp(secret=SECRET).get(url)
    assert SECRET not in caught.value.message
    assert "…" in caught.value.message


def test_a_transport_error_never_shows_its_own_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused with {SECRET}")

    _serve(monkeypatch, handler)
    with pytest.raises(FeedError) as caught:
        FeedHttp(secret=SECRET).get("https://exports.example.com/a.csv")
    assert caught.value.message == "Couldn't reach exports.example.com."
    assert SECRET not in caught.value.message


def test_a_timeout_says_so_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _serve(monkeypatch, handler)
    with pytest.raises(FeedError, match="didn't answer in time"):
        FeedHttp().get("https://exports.example.com/a.csv")


def test_a_body_that_is_not_json_is_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"<html>not json"))
    with pytest.raises(FeedError, match="didn't send JSON"):
        FeedHttp().get_json("https://api.example.com/accounts")


def test_the_repr_never_shows_the_secret() -> None:
    assert SECRET not in repr(FeedHttp(secret=SECRET))


def _basic(username: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")


def test_basic_auth_arrives_as_an_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, content=b"ok"))
    FeedHttp(secret=SECRET).get("https://exports.example.com/a.csv", auth=("csm", SECRET))
    assert seen[0].headers["authorization"] == _basic("csm", SECRET)
    assert SECRET not in str(seen[0].url)


def test_a_same_origin_redirect_keeps_the_key_and_the_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"ok")

    seen = _serve(monkeypatch, handler)
    FeedHttp(secret=SECRET).get(
        "https://exports.example.com/start",
        headers={"X-API-Key": SECRET},
        auth=("csm", SECRET),
    )
    assert [request.url.path for request in seen] == ["/start", "/final"]
    assert seen[1].headers["x-api-key"] == SECRET
    assert seen[1].headers["authorization"] == _basic("csm", SECRET)


@pytest.mark.parametrize(
    "location",
    [
        "https://elsewhere.example.com/final",  # another host
        "https://exports.example.com:8443/final",  # the same host on another port
    ],
)
def test_a_redirect_to_another_origin_drops_the_key_and_the_basic_auth(
    monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "exports.example.com" and request.url.path == "/start":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=b"ok")

    seen = _serve(monkeypatch, handler)
    FeedHttp(secret=SECRET).get(
        "https://exports.example.com/start",
        headers={"X-API-Key": SECRET},
        auth=("csm", SECRET),
    )
    assert seen[0].headers["x-api-key"] == SECRET
    assert str(seen[-1].url) == location
    assert "x-api-key" not in seen[-1].headers
    assert "authorization" not in seen[-1].headers


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("https://a.example.com/x", "https://a.example.com/y?z=1", True),
        ("https://a.example.com/x", "https://a.example.com:443/y", True),
        ("https://a.example.com/x", "https://A.example.com/y", True),
        ("https://a.example.com/x", "https://a.example.com:8443/y", False),
        ("https://a.example.com/x", "http://a.example.com/y", False),
        ("https://a.example.com/x", "https://b.example.com/y", False),
        ("https://a.example.com/x", "https://a.example.com:notaport/y", False),
        ("http://127.0.0.1:8000/x", "http://127.0.0.1:8000/y", True),
    ],
)
def test_same_origin_compares_scheme_host_and_port(first: str, second: str, same: bool) -> None:
    assert same_origin(first, second) is same
