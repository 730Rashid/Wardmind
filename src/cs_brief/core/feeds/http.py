"""The one HTTP client every feed provider uses.

Rules (security code: implementers escalate before changing them):
- GET only (and POST only for providers whose read API is a POST query, e.g. PostHog), never
  anything that writes.
- https only. Plain http is allowed only for 127.0.0.1 and localhost, for a CSM's own test
  server. A URL the CSM typed may point at their company's private network (an internal export
  link): that is their explicit choice, so it is allowed, unlike web research's guard.
- Redirects are followed by hand, at most MAX_REDIRECTS, and each target obeys the same rules.
- TIMEOUT_SECONDS per request, MAX_BYTES per response (a longer body is an error, not cut off:
  a truncated export would silently drop accounts).
- The secret goes only in the request headers. It never appears in an exception message, a
  repr or a log: FeedHttp scrubs it from any text it raises.
- No cookies are kept. Proxy settings from the environment are honoured (corporate networks).

Tests: set `transport_factory` (module level) to return an httpx.MockTransport; tests/conftest.py
sets one that fails loudly, so no test reaches the network by accident.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from cs_brief.core.connectors.errors import ConnectorError

TIMEOUT_SECONDS = 30.0
MAX_BYTES = 25_000_000
MAX_REDIRECTS = 3
USER_AGENT = "cs-brief (local customer success tool)"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# Test seam: None means a real httpx.HTTPTransport.
transport_factory: Callable[[], httpx.BaseTransport] | None = None


class FeedError(ConnectorError):
    """A feed that can't be read. `message` is plain English and never holds a secret."""


def check_url(url: str) -> str:
    """The URL, stripped, if feeds may fetch it; else FeedError with the reason. Allowed: https
    to any host, or http to 127.0.0.1/localhost. No credentials in the URL."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        raise FeedError("That isn't a web address cs-brief can read.") from None
    if not host:
        raise FeedError("That isn't a web address cs-brief can read.")
    if parts.username or parts.password:
        raise FeedError("Put the password or key in its own box, not in the web address.")
    if parts.scheme == "https":
        return text
    if parts.scheme == "http" and host in _LOCAL_HOSTS:
        return text
    raise FeedError("The web address must start with https://.")


class FeedHttp:
    """One client per sync. `secret` is scrubbed from every error text."""

    def __init__(self, *, secret: str | None = None) -> None:
        self._secret = secret or ""
        transport = transport_factory() if transport_factory is not None else None
        self._client = httpx.Client(
            transport=transport,
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        return self._request("GET", url, headers=headers, params=params, auth=auth)

    def post_query(
        self,
        url: str,
        *,
        json: Any,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """A POST that only reads (a query API such as PostHog's). Never use it to write."""
        return self._request("POST", url, headers=headers, json=json)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """get(), then the body as JSON (FeedError if it isn't)."""
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise FeedError(self._scrub(f"{_host(url)} didn't send JSON.")) from None

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:
        return "FeedHttp()"

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        target = check_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            self._client.cookies.clear()
            try:
                request = self._client.build_request(method, target, **kwargs)
                response = self._client.send(request, stream=True)
            except httpx.TimeoutException:
                raise FeedError(self._scrub(f"{_host(target)} didn't answer in time.")) from None
            except httpx.HTTPError:
                raise FeedError(self._scrub(f"Couldn't reach {_host(target)}.")) from None
            try:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "")
                    if not location:
                        raise FeedError(self._scrub(f"{_host(target)} sent a broken redirect."))
                    target = check_url(urljoin(target, location))
                    if method == "POST":
                        raise FeedError(self._scrub(f"{_host(target)} redirected a query."))
                    kwargs.pop("params", None)  # the redirect target carries its own query
                    continue
                body = self._read(response, target)
            finally:
                response.close()
            built = httpx.Response(
                response.status_code, headers=response.headers, content=body, request=request
            )
            self._check_status(built, target)
            return built
        raise FeedError(self._scrub(f"{_host(url)} redirected too many times."))

    def _read(self, response: httpx.Response, url: str) -> bytes:
        body = bytearray()
        try:
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise FeedError(
                        self._scrub(f"{_host(url)} sent more than 25 MB. Export fewer rows.")
                    )
        except httpx.HTTPError:
            raise FeedError(self._scrub(f"The download from {_host(url)} broke off.")) from None
        return bytes(body)

    def _check_status(self, response: httpx.Response, url: str) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        host = _host(url)
        if status in (401, 403):
            message = f"{host} refused the key or password. Check it, and that it can read."
        elif status == 404:
            message = f"{host} has nothing at that address. Check the address or IDs."
        elif status == 429:
            message = f"{host} is limiting requests. Try again in a few minutes."
        elif status >= 500:
            message = f"{host} had a problem ({status}). Try again later."
        else:
            message = f"{host} said no ({status}). Check the settings."
        raise FeedError(self._scrub(message))

    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "…") if self._secret else text


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "The server"
    except ValueError:
        return "The server"
