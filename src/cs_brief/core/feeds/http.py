"""The one HTTP client every feed provider uses.

Rules (security code: implementers escalate before changing them):
- GET only (and POST only for providers whose read API is a POST query, e.g. PostHog), never
  anything that writes.
- https only. Plain http is allowed only for 127.0.0.1 and localhost, for a CSM's own test
  server. A URL the CSM typed may point at their company's private network (an internal export
  link): that is their explicit choice, so it is allowed, unlike web research's guard.
- Redirects are followed by hand, at most MAX_REDIRECTS, and each target obeys the same rules.
  A redirect to another origin (scheme, host or port) drops the caller's headers and basic
  auth, so a key is only ever sent to the origin the CSM configured. Pagination links that
  providers follow must stay on the first page's origin (`same_origin`), else FeedError.
- A redirect to another origin is also guarded when the CSM's own URL is public (not
  `is_internal_url`): plain http is refused, and the hop goes through a client whose transport
  refuses every non-public address at connect time (research.GuardedTransport, no environment
  proxies). Same-origin hops and an internal URL's redirects keep the ordinary client.
- TIMEOUT_SECONDS per request, TOTAL_SECONDS per request in total (redirects included, however
  slowly the bytes arrive), MAX_BYTES per response (a longer body is an error, not cut off:
  a truncated export would silently drop accounts).
- The secret goes only in the request headers. It never appears in an exception message, a
  repr or a log: FeedHttp scrubs it from any text it raises.
- No cookies are kept. Proxy settings from the environment are honoured (corporate networks).

Tests: set `transport_factory` (module level) to return an httpx.MockTransport; tests/conftest.py
sets one that fails loudly, so no test reaches the network by accident.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from cs_brief.core.connectors.errors import ConnectorError
from cs_brief.core.research import (
    GuardedTransport,
    ResearchError,
    SiteUnreachable,
    _is_public,
)

TIMEOUT_SECONDS = 30.0
TOTAL_SECONDS = 120.0  # per request in total, redirects included
MAX_BYTES = 25_000_000
MAX_REDIRECTS = 3
USER_AGENT = "cs-brief (local customer success tool)"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# Name endings that are internal whatever DNS says (see is_internal_url).
_INTERNAL_SUFFIXES = (".local", ".internal", ".lan", ".corp", ".intranet", ".home.arpa")
# Headers that describe the encoded bytes, dropped once the body has been decoded.
_CODED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})

# Test seam: None means a real httpx.HTTPTransport.
transport_factory: Callable[[], httpx.BaseTransport] | None = None
# Test seam: the transport for a cross-origin redirect from a public URL. The real one refuses
# every address that isn't public, at connect time.
guarded_transport_factory: Callable[[], httpx.BaseTransport] = GuardedTransport


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


def is_internal_url(url: str) -> bool:
    """True when the URL's host is internal to the CSM's network: localhost, an IP address that
    isn't public, a name with no dot, or a name ending in .local, .internal, .lan, .corp,
    .intranet or .home.arpa. Decided from the text alone, never from DNS: an attacker controls
    DNS for a host they own."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass  # a name, not an address
    else:
        return not _is_public(host)
    return "." not in host or host.endswith(_INTERNAL_SUFFIXES)


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
        # For cross-origin redirects from a public URL; made the first time one is needed.
        self._guarded: httpx.Client | None = None

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
        except RecursionError:  # json nests by recursion, so a very deep document ends here
            raise FeedError("That JSON is nested too deeply.") from None
        except ValueError:
            raise FeedError(self._scrub(f"{_host(url)} didn't send JSON.")) from None

    def close(self) -> None:
        self._client.close()
        if self._guarded is not None:
            self._guarded.close()

    def __repr__(self) -> str:
        return "FeedHttp()"

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        target = check_url(url)
        origin = _origin(target)
        internal = is_internal_url(target)  # from the CSM's own URL only
        deadline = time.monotonic() + TOTAL_SECONDS
        auth = kwargs.pop("auth", None)
        sender = target  # the URL whose redirect led to `target`, named in a refusal
        for _ in range(MAX_REDIRECTS + 1):
            if time.monotonic() >= deadline:
                raise FeedError(self._scrub(f"{_host(target)} took too long to send everything."))
            cross_origin = _origin(target) != origin
            if cross_origin:
                # Like browsers and requests: a redirect to another scheme, host or port never
                # carries the key. Every caller header goes, since any of them may hold it.
                kwargs.pop("headers", None)
                auth = None
            if cross_origin and not internal:
                client = self._public_client(target, sender)
            else:
                client = self._client
            client.cookies.clear()
            try:
                request = client.build_request(method, target, **kwargs)
                response = client.send(
                    request, stream=True, auth=httpx.BasicAuth(*auth) if auth else None
                )
            except httpx.TimeoutException:
                raise FeedError(self._scrub(f"{_host(target)} didn't answer in time.")) from None
            except httpx.HTTPError:
                raise FeedError(self._scrub(f"Couldn't reach {_host(target)}.")) from None
            except ResearchError as error:  # the connect-time guard refused the address
                raise FeedError(self._scrub(_guard_message(sender, target, error))) from None
            try:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "")
                    if not location:
                        raise FeedError(self._scrub(f"{_host(target)} sent a broken redirect."))
                    sender = target
                    target = check_url(urljoin(target, location))
                    if method == "POST":
                        raise FeedError(self._scrub(f"{_host(target)} redirected a query."))
                    kwargs.pop("params", None)  # the redirect target carries its own query
                    continue
                body = self._read(response, target, deadline)
            finally:
                response.close()
            # `body` is already decoded, so the encoding headers must not travel with it.
            headers = [
                (name, value)
                for name, value in response.headers.multi_items()
                if name.lower() not in _CODED_HEADERS
            ]
            built = httpx.Response(
                response.status_code, headers=headers, content=body, request=request
            )
            self._check_status(built, target)
            return built
        raise FeedError(self._scrub(f"{_host(url)} redirected too many times."))

    def _public_client(self, target: str, sender: str) -> httpx.Client:
        """The client for a cross-origin hop from a public URL: https only, every connection
        checked at connect time to be a public address, and environment proxies ignored. The
        refusal names `sender`, the host whose redirect led here."""
        if urlsplit(target).scheme != "https":
            raise FeedError(
                self._scrub(f"{_host(sender)} redirected to an address cs-brief won't follow.")
            )
        if self._guarded is None:
            self._guarded = httpx.Client(
                transport=guarded_transport_factory(),
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT},
            )
        return self._guarded

    def _read(self, response: httpx.Response, url: str, deadline: float) -> bytes:
        body = bytearray()
        try:
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise FeedError(
                        self._scrub(f"{_host(url)} sent more than 25 MB. Export fewer rows.")
                    )
                if time.monotonic() >= deadline:
                    raise FeedError(self._scrub(f"{_host(url)} took too long to send everything."))
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


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the default port filled in, for same-origin checks."""
    parts = urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return (parts.scheme, (parts.hostname or "").lower(), port)


def same_origin(first: str, second: str) -> bool:
    """Whether two URLs share scheme, host and port (a key may be sent to both)."""
    try:
        return _origin(first) == _origin(second)
    except ValueError:
        return False


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "The server"
    except ValueError:
        return "The server"


def _guard_message(sender: str, target: str, error: ResearchError) -> str:
    """Plain English for a hop the connect-time guard refused. Never the guard's own text."""
    if isinstance(error, SiteUnreachable):
        return f"Couldn't reach {_host(target)}."
    return f"{_host(sender)} redirected to an address cs-brief won't follow."
