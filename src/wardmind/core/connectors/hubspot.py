"""Read-only HubSpot connector: customer companies, their owners and open high-priority tickets.

Wardmind never writes to HubSpot. Every request is a GET or a read-only POST (search, batch read).

Data model mapping (all property names are configurable in config/hubspot.yaml):
- Accounts are companies whose `customer_property` equals `customer_value` (lifecycle stage
  "customer" by default), found with the CRM search API.
- Signal values come from company properties, read with `propertiesWithHistory`, so each change
  in HubSpot becomes one Signal with its own timestamp (that's the account page's history).
- OPEN_TICKETS_SEV1 is counted from tickets: priority in `sev1_priorities` and no value in
  `ticket_closed_property`, associated with the company.
- ONBOARDING_OVERDUE_MILESTONES comes from the `milestones_overdue` property's history.

Safety rules (enforced by HubSpotClient):
- The token goes only in the Authorization header, to API_BASE or a localhost test server.
  It never appears in logs, exceptions, reprs or user-facing messages.
- base_url other than API_BASE is accepted only for http(s)://127.0.0.1 or localhost (tests and
  the local fake server), so a token can't be sent anywhere else by configuration.

CONTRACT FILE: models, constants, signatures and docstrings are fixed; implementers escalate
before changing them.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, Field, ValidationError

from wardmind.core.config import config_dir
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.matching import normalize_domain
from wardmind.core.models import Account, Milestones, Signal, SignalKind

API_BASE = "https://api.hubapi.com"

# Dated API paths first; the legacy v3 path is tried once if the dated one returns 404, and the
# working one is remembered per key for the client's lifetime. Keep every path here, in one place.
PATHS: dict[str, tuple[str, str]] = {
    "companies_search": (
        "/crm/objects/2026-09/companies/search",
        "/crm/v3/objects/companies/search",
    ),
    "companies_batch_read": (
        "/crm/objects/2026-09/companies/batch/read",
        "/crm/v3/objects/companies/batch/read",
    ),
    "owners": ("/crm/owners/2026-09/", "/crm/v3/owners/"),
    "tickets_search": ("/crm/objects/2026-09/tickets/search", "/crm/v3/objects/tickets/search"),
    "ticket_company_associations": (
        "/crm/associations/2026-09/tickets/companies/batch/read",
        "/crm/v4/associations/tickets/companies/batch/read",
    ),
}

REQUIRED_SCOPES = ("crm.objects.companies.read", "crm.objects.owners.read", "tickets")

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_BAD_BASE_URL = "HubSpot requests can only go to api.hubapi.com or a local test server."
_MAX_RETRY_AFTER = 30  # seconds
_MAX_MESSAGE = 200  # characters of HubSpot's own message that reach the user
_COMPANY_BATCH = 50  # history reads have the lower limit
_CHECK_SAMPLE = 50  # companies sampled by check_connection for the property check
_TICKET_BATCH = 100
_SEARCH_PAGE = 100  # the search API's largest page
_SEARCH_CAP = 10_000  # the search API returns at most this many results
MAX_PAGES = 200  # pages of one list or search; a longer one stops with a HubSpotError
MAX_BODY_BYTES = 25_000_000  # per response body; a larger one stops with a HubSpotError
_CODED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})
_EPOCH_MS = re.compile(r"\d{10,}")  # HubSpot date properties come back as epoch milliseconds
_EPOCH = datetime(1970, 1, 1)
_UNASSIGNED = "Unassigned"
_UNAUTHORIZED = (
    "HubSpot didn't accept the access token. It may be mistyped, revoked or expired. "
    "Save a new one on the Connections page."
)
_RATE_LIMITED = "HubSpot is limiting requests right now. Wait a minute and try again."
_UNAVAILABLE = "Couldn't reach HubSpot. Check your internet connection and try again."
_LOOPING = "HubSpot kept sending the same page, so Wardmind stopped. Try again in a minute."
_TOO_MANY_PAGES = (
    "HubSpot sent more pages than Wardmind reads in one go, so Wardmind stopped. "
    "Narrow what is shared with Wardmind in HubSpot and try again."
)
_TOO_LARGE = "HubSpot sent more data than Wardmind reads in one reply, so Wardmind stopped."
_UNREADABLE = "HubSpot sent a reply Wardmind couldn't read. Try again in a minute."
_ACCEPTED = "HubSpot accepted the access token."
_SKIPPED_NO_TOKEN = "Skipped until the token works."
_SKIPPED_OTHER = "Skipped because the first check failed."
_CAP_WARNING = (
    "HubSpot's search returns at most 10,000 companies, so only the first 10,000 are included."
)


class HubSpotError(ConnectorError):
    """Base for HubSpot failures. `message` is plain English and never contains the token."""


class HubSpotAuthError(HubSpotError):
    """401: the token was rejected (wrong, revoked or expired)."""


class HubSpotScopeError(HubSpotError):
    """403: the token works but lacks a scope. `missing` lists scope names when HubSpot says."""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


class HubSpotRateLimited(HubSpotError):
    """429 still returned after the retries."""


class HubSpotUnavailable(HubSpotError):
    """Network failure, timeout, or 5xx after the retries."""


class HubSpotMapping(BaseModel):
    """Which HubSpot properties feed which Wardmind fields. Defaults follow docs/hubspot.md."""

    customer_property: str = "lifecyclestage"
    customer_value: str = "customer"
    name: str = "name"
    domain: str = "domain"  # HubSpot's standard company domain name, used to spot duplicates
    owner: str = "hubspot_owner_id"
    arr: str = "cs_arr"
    tier: str = "cs_tier"
    start_date: str = "hs_lifecyclestage_customer_date"  # HubSpot's "Became a Customer Date"
    renewal_date: str = "cs_renewal_date"
    milestones_total: str = "cs_onboarding_milestones_total"
    milestones_done: str = "cs_onboarding_milestones_done"
    milestones_overdue: str = "cs_onboarding_milestones_overdue"
    signals: dict[SignalKind, str] = Field(
        default_factory=lambda: {
            SignalKind.USAGE_TREND_30D: "cs_usage_trend_30d",
            SignalKind.DAYS_SINCE_LAST_LOGIN: "cs_days_since_last_login",
            SignalKind.NPS_LATEST: "cs_nps_latest",
            SignalKind.CHAMPION_LEFT: "cs_champion_left",
            SignalKind.SEAT_UTILISATION: "cs_seat_utilisation",
            SignalKind.BILLING_OVERDUE_DAYS: "cs_billing_overdue_days",
        }
    )
    sev1_priorities: list[str] = Field(default_factory=lambda: ["HIGH", "URGENT"])
    ticket_priority_property: str = "hs_ticket_priority"
    ticket_closed_property: str = "closed_date"
    history_days: int = Field(default=90, ge=0)  # signal readings older than this are dropped


class CheckResult(BaseModel):
    name: str  # what was checked, e.g. "Token accepted", "Companies", "Owners", "Tickets"
    ok: bool
    message: str  # plain English, e.g. "128 customer companies found." or what to fix


class ConnectionCheck(BaseModel):
    ok: bool  # every check passed
    checks: list[CheckResult]
    missing_properties: list[str]  # mapped company properties HubSpot doesn't have


def _text(raw: Any) -> str:
    """A HubSpot value as stripped text. None is the empty string."""
    return "" if raw is None else str(raw).strip()


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _count(raw: Any) -> int:
    value = _parse_float(raw)
    return 0 if value is None else int(value)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_float(raw: Any) -> float | None:
    """A number from a HubSpot value, or None when it's blank or not a finite number."""
    text = _text(raw)
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _from_epoch_ms(text: str) -> datetime | None:
    try:
        return _EPOCH + timedelta(milliseconds=int(text))
    except OverflowError:
        return None


def _parse_datetime(raw: Any) -> datetime | None:
    """A timestamp as a naive UTC datetime: ISO with Z or an offset, a bare date, or epoch ms."""
    text = _text(raw)
    if not text:
        return None
    if _EPOCH_MS.fullmatch(text):
        return _from_epoch_ms(text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def _parse_date(raw: Any) -> date | None:
    """A date from YYYY-MM-DD, an ISO datetime (taken in UTC) or epoch milliseconds."""
    text = _text(raw)
    if not text:
        return None
    if _EPOCH_MS.fullmatch(text):
        moment = _from_epoch_ms(text)
        return None if moment is None else moment.date()
    try:
        return date.fromisoformat(text)
    except ValueError:
        moment = _parse_datetime(text)
        return None if moment is None else moment.date()


def _parse_value(kind: SignalKind, raw: Any) -> float | None:
    """A signal reading. CHAMPION_LEFT also takes HubSpot's true and false words."""
    if kind is SignalKind.CHAMPION_LEFT:
        word = _text(raw).lower()
        if word in _TRUE_WORDS:
            return 1.0
        if word in _FALSE_WORDS:
            return 0.0
    return _parse_float(raw)


def _clamp_milestones(total: int, done: int, overdue: int) -> tuple[Milestones, bool]:
    """Keep done within total and overdue within what's left. Also reports whether it changed."""
    fixed_total = max(total, 0)
    fixed_done = min(max(done, 0), fixed_total)
    fixed_overdue = min(max(overdue, 0), fixed_total - fixed_done)
    changed = (fixed_total, fixed_done, fixed_overdue) != (total, done, overdue)
    return Milestones(done=fixed_done, total=fixed_total, overdue=fixed_overdue), changed


def _count_warning(count: int, one: str, many: str) -> str:
    return f"1 {one}" if count == 1 else f"{count} {many}"


def _signal_map(mapping: HubSpotMapping) -> dict[SignalKind, str]:
    """Signal kind to HubSpot property, read with history. Open tickets come from tickets."""
    signals = {
        kind: prop
        for kind, prop in mapping.signals.items()
        if kind is not SignalKind.OPEN_TICKETS_SEV1
    }
    signals[SignalKind.ONBOARDING_OVERDUE_MILESTONES] = mapping.milestones_overdue
    return signals


def _company_props(mapping: HubSpotMapping) -> list[str]:
    """Every company property Wardmind maps, in a fixed order, without duplicates."""
    names = [
        mapping.customer_property,
        mapping.name,
        mapping.domain,
        mapping.owner,
        mapping.arr,
        mapping.tier,
        mapping.start_date,
        mapping.renewal_date,
        mapping.milestones_total,
        mapping.milestones_done,
        mapping.milestones_overdue,
        *_signal_map(mapping).values(),
    ]
    return list(dict.fromkeys(names))


def _read_props(mapping: HubSpotMapping) -> list[str]:
    return list(dict.fromkeys([*_company_props(mapping), "createdate"]))


def _customer_filter(mapping: HubSpotMapping) -> list[dict[str, Any]]:
    return [
        {
            "filters": [
                {
                    "propertyName": mapping.customer_property,
                    "operator": "EQ",
                    "value": mapping.customer_value,
                }
            ]
        }
    ]


def _sev1_ticket_filter(mapping: HubSpotMapping) -> list[dict[str, Any]]:
    return [
        {
            "filters": [
                {
                    "propertyName": mapping.ticket_priority_property,
                    "operator": "IN",
                    "values": list(mapping.sev1_priorities),
                },
                {"propertyName": mapping.ticket_closed_property, "operator": "NOT_HAS_PROPERTY"},
            ]
        }
    ]


def _companies_message(total: int, mapping: HubSpotMapping) -> str:
    if total == 0:
        return (
            f"No companies have {mapping.customer_property} set to {mapping.customer_value}. "
            "Check customer_property in config/hubspot.yaml."
        )
    if total == 1:
        return "1 customer company found."
    return f"{total} customer companies found."


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _next_after(page: dict[str, Any]) -> str | None:
    paging = page.get("paging")
    following = paging.get("next") if isinstance(paging, dict) else None
    after = following.get("after") if isinstance(following, dict) else None
    return None if after in (None, "") else str(after)


def _now_utc() -> datetime:
    """Now as a naive UTC datetime, the same shape as the demo data."""
    return datetime.now(UTC).replace(tzinfo=None)


def _normalise_base_url(base_url: str) -> str:
    """API_BASE, or a local test server as scheme://host[:port]. Anything else is refused."""
    try:
        parts = urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError(_BAD_BASE_URL) from None
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ValueError(_BAD_BASE_URL)
    if parts.scheme == "https" and host == "api.hubapi.com" and port in (None, 443):
        return API_BASE
    if parts.scheme in ("http", "https") and host in _LOCAL_HOSTS:
        return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    raise ValueError(_BAD_BASE_URL)


def _backoff(attempt: int) -> float:
    return float(2**attempt)


def _retry_after(response: httpx.Response) -> float | None:
    """Retry-After in seconds, capped; None when the header is missing or isn't a whole number."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(raw.strip())
    except ValueError:
        return None
    return float(min(max(seconds, 0), _MAX_RETRY_AFTER))


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _join_quoted(names: list[str]) -> str:
    quoted = [f'"{name}"' for name in names]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def _mapping_error(reason: str) -> ConnectorError:
    return ConnectorError(
        f"config/hubspot.yaml has a problem: {reason}. "
        "Fix it or delete the file to use the defaults."
    )


def load_mapping(path: Path | None = None) -> HubSpotMapping:
    """Read config/hubspot.yaml (via config_dir()); a missing file gives the defaults."""
    path = path or config_dir() / "hubspot.yaml"
    if not path.exists():
        return HubSpotMapping()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        raise _mapping_error("it isn't valid YAML") from None
    except (OSError, UnicodeDecodeError):
        raise _mapping_error("it couldn't be read") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _mapping_error("it should be a set of name: value settings")

    values = dict(data)
    signals = dict(HubSpotMapping().signals)
    overrides = values.pop("signals", None)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise _mapping_error("signals should be a list of signal: property pairs")
        for key, prop in overrides.items():
            try:
                kind = SignalKind(key)
            except (TypeError, ValueError):
                raise _mapping_error(f"signals.{key} isn't a signal Wardmind knows") from None
            if kind is SignalKind.OPEN_TICKETS_SEV1:
                raise _mapping_error("signals.open_tickets_sev1 comes from tickets, not a property")
            if not isinstance(prop, str) or not prop.strip():
                raise _mapping_error(f"signals.{key} should be a property name")
            signals[kind] = prop.strip()
    for key in values:
        if key not in HubSpotMapping.model_fields:
            raise _mapping_error(f"{key} isn't a setting Wardmind knows")
    values["signals"] = signals

    try:
        mapping = HubSpotMapping.model_validate(values)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "the file"
        raise _mapping_error(f"{field}: {first['msg']}") from None
    for name, value in mapping.model_dump().items():
        if isinstance(value, str) and not value.strip():
            raise _mapping_error(f"{name} is blank")
    if not mapping.sev1_priorities:
        raise _mapping_error("sev1_priorities is empty")
    return mapping


class _PathNotFound(Exception):
    """Internal: HubSpot answered 404 for one path, so the other path may work."""


def _iter_pages(
    client: HubSpotClient,
    method: str,
    key: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Each page of a list or search, following paging.next.after. GET sends `after` in the
    query string; POST sends it in the JSON body."""
    after: str | None = None
    seen: set[str] = set()
    pages = 0
    while True:
        query = dict(params or {})
        body = dict(json) if json is not None else None
        if after is not None:
            if method.upper() == "GET":
                query["after"] = after
            else:
                body = {**(body or {}), "after": after}
        page = client.request(method, key, params=query or None, json=body)
        pages += 1
        yield page
        after = _next_after(page)
        if after is None:
            return
        if after in seen:
            raise HubSpotError(_LOOPING)
        if pages >= MAX_PAGES:
            raise HubSpotError(_TOO_MANY_PAGES)
        seen.add(after)


class HubSpotClient:
    """Thin HTTP client: auth header, JSON, paging, retries and the dated/legacy path fallback."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Raise ValueError if base_url is neither API_BASE nor a 127.0.0.1/localhost URL."""
        self._base_url = _normalise_base_url(base_url)
        self._secret = token
        self._max_retries = max_retries
        self._sleep = sleep
        self._legacy_keys: set[str] = set()  # keys whose legacy path has answered
        self._http = httpx.Client(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Wardmind",
            },
        )

    def close(self) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call PATHS[key] (dated, then legacy on 404). Retries 429 (Retry-After, else
        exponential backoff from 1s) and 5xx/network errors up to max_retries, then raises the
        matching HubSpotError. 401 and 403 raise at once."""
        paths = PATHS[key]
        if key not in self._legacy_keys:
            try:
                return self._send(method, paths[0], params, json)
            except _PathNotFound:
                pass
        try:
            data = self._send(method, paths[1], params, json)
        except _PathNotFound:
            raise HubSpotError(
                f"HubSpot didn't recognise a request Wardmind made ({key}). "
                "Wardmind may need an update."
            ) from None
        self._legacy_keys.add(key)
        return data

    def paginate(
        self,
        method: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of `results` across pages, following paging.next.after."""
        for page in _iter_pages(self, method, key, params=params, json=json):
            yield from _dict_items(page.get("results"))

    def __repr__(self) -> str:
        """Never includes the token."""
        return f"HubSpotClient(base_url={self._base_url!r})"

    def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = self._send_with_retries(method, path, params, json)
        status = response.status_code
        if 200 <= status < 300:
            return self._read(response)
        if status == 401:
            raise HubSpotAuthError(_UNAUTHORIZED)
        if status == 403:
            raise self._scope_error(response)
        if status == 404:
            raise _PathNotFound()
        raise HubSpotError(f"HubSpot rejected a request: {self._hubspot_message(response)}")

    def _send_with_retries(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._fetch(method, path, params, json)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise HubSpotUnavailable(_UNAVAILABLE) from exc
                self._sleep(_backoff(attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                if attempt >= self._max_retries:
                    raise HubSpotRateLimited(_RATE_LIMITED)
                wait = _retry_after(response)
                self._sleep(_backoff(attempt) if wait is None else wait)
                attempt += 1
                continue
            if 500 <= status <= 599:
                if attempt >= self._max_retries:
                    raise HubSpotUnavailable(_UNAVAILABLE)
                self._sleep(_backoff(attempt))
                attempt += 1
                continue
            return response

    def _fetch(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        """One request whose body is read in pieces, so a reply over MAX_BODY_BYTES stops early
        with a HubSpotError instead of filling memory. The returned response holds the decoded
        body, with the encoding headers removed so it isn't decoded twice."""
        request = self._http.build_request(method, path, params=params, json=json)
        response = self._http.send(request, stream=True)
        try:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise HubSpotError(_TOO_LARGE)
        finally:
            response.close()
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _CODED_HEADERS
        ]
        return httpx.Response(
            response.status_code, headers=headers, content=bytes(body), request=request
        )

    def _read(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise HubSpotError(_UNREADABLE) from None
        if not isinstance(data, dict):
            raise HubSpotError(_UNREADABLE)
        return data

    def _scrub(self, text: str) -> str:
        """Remove the token from any text that might reach the user."""
        return text.replace(self._secret, "…") if self._secret else text

    def _scope_error(self, response: httpx.Response) -> HubSpotScopeError:
        scopes: list[str] = []
        for error in _dict_items(_body(response).get("errors")):
            context = _dict_or_empty(error.get("context"))
            for field in ("requiredGranularScopes", "requiredScopes"):
                values = context.get(field)
                if not isinstance(values, list):
                    continue
                for scope in values:
                    name = self._scrub(str(scope))
                    if name and name not in scopes:
                        scopes.append(name)
        if not scopes:
            return HubSpotScopeError(
                "This key doesn't have permission for that. Check its scopes in HubSpot."
            )
        if len(scopes) == 1:
            message = (
                f'This key is missing the "{scopes[0]}" scope. Add it in HubSpot and test again.'
            )
        else:
            message = (
                f"This key is missing the {_join_quoted(scopes)} scopes. "
                "Add them in HubSpot and test again."
            )
        return HubSpotScopeError(message, missing=scopes)

    def _hubspot_message(self, response: httpx.Response) -> str:
        message = _body(response).get("message")
        text = message.strip() if isinstance(message, str) else ""
        if not text:
            text = f"HubSpot returned status {response.status_code}."
        text = self._scrub(text)
        if len(text) <= _MAX_MESSAGE:
            return text
        return text[: _MAX_MESSAGE - 1] + "…"


@dataclass(frozen=True)
class _Snapshot:
    """One successful load: what the account, signal and warning reads are served from."""

    accounts: list[Account]
    signals: list[Signal]
    warnings: list[str]


class HubSpotConnector:
    """The Connector for HubSpot. Fetches everything once, then serves it from memory until the
    cache expires or refresh() is called."""

    name = "hubspot"

    def __init__(
        self,
        client: HubSpotClient,
        mapping: HubSpotMapping | None = None,
        *,
        today: date | None = None,
        cache_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._mapping = mapping if mapping is not None else HubSpotMapping()
        self._today = today if today is not None else date.today()
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._snapshot: _Snapshot | None = None
        self._loaded_at: float | None = None
        self._stale = False  # set by refresh(); the next read loads again

    def list_accounts(self) -> list[Account]:
        return list(self._current().accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        signals = self._current().signals
        if account_id is None:
            return list(signals)
        return [signal for signal in signals if signal.account_id == account_id]

    @property
    def warnings(self) -> list[str]:
        """Plain-English notes from the last load, e.g. "3 companies have no renewal date, so
        they were left out." Empty when everything mapped cleanly."""
        return list(self._snapshot.warnings) if self._snapshot is not None else []

    @property
    def loaded_at(self) -> float | None:
        """clock() value of the last successful load, or None before the first."""
        return self._loaded_at

    def refresh(self) -> None:
        """Drop the cache so the next call fetches from HubSpot again."""
        self._stale = True

    def check_connection(self) -> ConnectionCheck:
        """Run small live calls (one page each) to test the token, each scope and the mapping.
        Never raises for HubSpot errors: each failure becomes a CheckResult."""
        mapping = self._mapping
        checks: list[CheckResult] = []
        missing: list[str] = []
        try:
            page = self._client.request(
                "POST",
                "companies_search",
                json={
                    "filterGroups": _customer_filter(mapping),
                    "properties": [mapping.name],
                    "limit": _CHECK_SAMPLE,
                },
            )
        except HubSpotScopeError as exc:
            # HubSpot authenticated the token and refused one scope, so the token itself works.
            checks.append(CheckResult(name="Token accepted", ok=True, message=_ACCEPTED))
            checks.append(CheckResult(name="Companies", ok=False, message=exc.message))
        except HubSpotError as exc:
            skipped = _SKIPPED_NO_TOKEN if isinstance(exc, HubSpotAuthError) else _SKIPPED_OTHER
            checks.append(CheckResult(name="Token accepted", ok=False, message=exc.message))
            checks.extend(
                CheckResult(name=name, ok=False, message=skipped)
                for name in ("Companies", "Owners", "Tickets")
            )
            return ConnectionCheck(ok=False, checks=checks, missing_properties=[])
        else:
            rows = _dict_items(page.get("results"))
            checks.append(CheckResult(name="Token accepted", ok=True, message=_ACCEPTED))
            checks.append(
                CheckResult(
                    name="Companies",
                    ok=True,
                    message=_companies_message(_as_int(page.get("total")), mapping),
                )
            )
            sample = [_text(row.get("id")) for row in rows]
            sample = [company_id for company_id in sample if company_id]
            if sample:
                missing = self._missing_company_properties(sample)
        checks.append(self._owners_check())
        checks.append(self._tickets_check())
        return ConnectionCheck(
            ok=all(check.ok for check in checks),
            checks=checks,
            missing_properties=missing,
        )

    def _current(self) -> _Snapshot:
        """The loaded book. Loads again when there is none, after refresh(), or when expired."""
        if (
            self._snapshot is None
            or self._stale
            or self._loaded_at is None
            or self._clock() - self._loaded_at >= self._cache_seconds
        ):
            snapshot = self._load()  # raises on failure and leaves the previous snapshot alone
            self._snapshot = snapshot
            self._loaded_at = self._clock()
            self._stale = False
        return self._snapshot

    def _load(self) -> _Snapshot:
        warnings: list[str] = []
        now = _now_utc()
        rows = self._search_companies(warnings)
        companies = self._read_companies([_text(row.get("id")) for row in rows])
        owners = self._load_owners()
        sev1 = self._count_sev1_tickets()
        accounts, signals = self._build(rows, companies, owners, sev1, now, warnings)
        return _Snapshot(accounts=accounts, signals=signals, warnings=warnings)

    def _search_companies(self, warnings: list[str]) -> list[dict[str, Any]]:
        body = {
            "filterGroups": _customer_filter(self._mapping),
            "properties": _read_props(self._mapping),
            "limit": _SEARCH_PAGE,
        }
        rows: list[dict[str, Any]] = []
        total = 0
        for index, page in enumerate(
            _iter_pages(self._client, "POST", "companies_search", json=body)
        ):
            if index == 0:
                total = _as_int(page.get("total"))
            rows.extend(row for row in _dict_items(page.get("results")) if _text(row.get("id")))
            if len(rows) >= _SEARCH_CAP:
                break
        if total > _SEARCH_CAP:
            warnings.append(_CAP_WARNING)
        return rows[:_SEARCH_CAP]

    def _read_companies(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        mapping = self._mapping
        properties = _read_props(mapping)
        history = list(_signal_map(mapping).values())
        found: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(ids, _COMPANY_BATCH):
            page = self._client.request(
                "POST",
                "companies_batch_read",
                json={
                    "inputs": [{"id": company_id} for company_id in chunk],
                    "properties": properties,
                    "propertiesWithHistory": history,
                },
            )
            for result in _dict_items(page.get("results")):
                found[_text(result.get("id"))] = result
        return found

    def _load_owners(self) -> dict[str, str]:
        owners: dict[str, str] = {}
        for row in self._client.paginate("GET", "owners", params={"limit": 100}):
            owner_id = _text(row.get("id"))
            if not owner_id:
                continue
            full_name = f"{_text(row.get('firstName'))} {_text(row.get('lastName'))}".strip()
            owners[owner_id] = full_name or _text(row.get("email")) or _UNASSIGNED
        return owners

    def _count_sev1_tickets(self) -> dict[str, int]:
        """Distinct open sev-1 tickets per company id."""
        body = {
            "filterGroups": _sev1_ticket_filter(self._mapping),
            "properties": [self._mapping.ticket_priority_property],
            "limit": _SEARCH_PAGE,
        }
        ticket_ids: dict[str, None] = {}  # a dict keeps first-seen order and drops repeats
        for row in self._client.paginate("POST", "tickets_search", json=body):
            ticket_id = _text(row.get("id"))
            if ticket_id:
                ticket_ids[ticket_id] = None
        linked: dict[str, set[str]] = {}
        for chunk in _chunks(list(ticket_ids), _TICKET_BATCH):
            page = self._client.request(
                "POST",
                "ticket_company_associations",
                json={"inputs": [{"id": ticket_id} for ticket_id in chunk]},
            )
            for entry in _dict_items(page.get("results")):
                source = _dict_or_empty(entry.get("from"))
                ticket_id = _text(source.get("id"))
                for target in _dict_items(entry.get("to")):
                    raw = target.get("toObjectId")
                    if raw is None:
                        raw = target.get("id")
                    company_id = _text(raw)
                    if ticket_id and company_id:
                        linked.setdefault(company_id, set()).add(ticket_id)
        return {company_id: len(tickets) for company_id, tickets in linked.items()}

    def _build(
        self,
        rows: list[dict[str, Any]],
        companies: dict[str, dict[str, Any]],
        owners: dict[str, str],
        sev1: dict[str, int],
        now: datetime,
        warnings: list[str],
    ) -> tuple[list[Account], list[Signal]]:
        mapping = self._mapping
        kinds = _signal_map(mapping)
        cutoff = datetime.combine(
            self._today - timedelta(days=mapping.history_days), datetime.min.time()
        )
        accounts: list[Account] = []
        signals: list[Signal] = []
        no_arr = no_renewal = adjusted = 0
        for row in rows:
            company_id = _text(row.get("id"))
            result = companies.get(company_id)
            if result is None:
                continue
            props = _dict_or_empty(result.get("properties"))
            renewal = _parse_date(props.get(mapping.renewal_date))
            if renewal is None:
                no_renewal += 1
                continue
            arr = _parse_float(props.get(mapping.arr))
            if arr is None or arr < 0:
                arr = 0.0
                no_arr += 1
            milestones, changed = _clamp_milestones(
                _count(props.get(mapping.milestones_total)),
                _count(props.get(mapping.milestones_done)),
                _count(props.get(mapping.milestones_overdue)),
            )
            if changed:
                adjusted += 1
            owner_id = _text(props.get(mapping.owner))
            account = Account(
                id=f"hs-{company_id}",
                name=_text(props.get(mapping.name)) or f"Company {company_id}",
                arr=arr,
                tier=_text(props.get(mapping.tier)) or "Unknown",
                owner=owners.get(owner_id, _UNASSIGNED) if owner_id else _UNASSIGNED,
                start_date=(
                    _parse_date(props.get(mapping.start_date))
                    or _parse_date(props.get("createdate"))
                ),
                renewal_date=renewal,
                milestones=milestones,
                domain=normalize_domain(_text(props.get(mapping.domain))),
            )
            accounts.append(account)
            signals.extend(self._account_signals(account.id, result, props, kinds, cutoff, now))
            signals.append(
                Signal(
                    account_id=account.id,
                    kind=SignalKind.OPEN_TICKETS_SEV1,
                    value=float(sev1.get(company_id, 0)),
                    observed_at=now,
                    source="hubspot",
                )
            )
        if no_arr:
            warnings.append(
                _count_warning(
                    no_arr,
                    "company has no ARR, so it counts as $0.",
                    "companies have no ARR, so they count as $0.",
                )
            )
        if no_renewal:
            warnings.append(
                _count_warning(
                    no_renewal,
                    "company has no renewal date, so it was left out.",
                    "companies have no renewal date, so they were left out.",
                )
            )
        if adjusted:
            warnings.append(
                _count_warning(
                    adjusted,
                    "company has onboarding milestone numbers that don't add up, "
                    "so it was adjusted.",
                    "companies have onboarding milestone numbers that don't add up, "
                    "so they were adjusted.",
                )
            )
        return accounts, signals

    def _account_signals(
        self,
        account_id: str,
        result: dict[str, Any],
        props: dict[str, Any],
        kinds: dict[SignalKind, str],
        cutoff: datetime,
        now: datetime,
    ) -> list[Signal]:
        """History readings inside the window; or, with no history at all, the current value
        stamped at the company's last update."""
        history = _dict_or_empty(result.get("propertiesWithHistory"))
        updated = _parse_datetime(result.get("updatedAt")) or now
        out: list[Signal] = []
        for kind, prop in kinds.items():
            readings = history.get(prop)
            if isinstance(readings, list) and readings:
                for reading in _dict_items(readings):
                    value = _parse_value(kind, reading.get("value"))
                    observed = _parse_datetime(reading.get("timestamp"))
                    if value is None or observed is None or observed < cutoff:
                        continue
                    out.append(
                        Signal(
                            account_id=account_id,
                            kind=kind,
                            value=value,
                            observed_at=observed,
                            source="hubspot",
                        )
                    )
            else:
                value = _parse_value(kind, props.get(prop))
                if value is not None:
                    out.append(
                        Signal(
                            account_id=account_id,
                            kind=kind,
                            value=value,
                            observed_at=updated,
                            source="hubspot",
                        )
                    )
        return out

    def _missing_company_properties(self, company_ids: list[str]) -> list[str]:
        """Mapped properties that no sampled company returns at all, from one batch read.

        HubSpot omits undefined properties, and a defined property with no value comes back as
        null. So a key counts as present when any sampled company returns it, even as null.
        Only a key missing from every sampled company is reported, so one company with an unset
        value doesn't produce a false "HubSpot doesn't have this property".
        """
        expected = _company_props(self._mapping)
        try:
            page = self._client.request(
                "POST",
                "companies_batch_read",
                json={
                    "inputs": [{"id": company_id} for company_id in company_ids[:_CHECK_SAMPLE]],
                    "properties": expected,
                },
            )
        except HubSpotError:
            return []
        rows = _dict_items(page.get("results"))
        returned: set[str] = set()
        for row in rows:
            # A key with a null value is still returned by HubSpot, so it counts as present.
            returned.update(_dict_or_empty(row.get("properties")).keys())
        if not rows:
            return []
        return sorted(prop for prop in expected if prop not in returned)

    def _owners_check(self) -> CheckResult:
        try:
            self._client.request("GET", "owners", params={"limit": 1})
        except HubSpotError as exc:
            return CheckResult(name="Owners", ok=False, message=exc.message)
        return CheckResult(name="Owners", ok=True, message="Owners can be read.")

    def _tickets_check(self) -> CheckResult:
        try:
            self._client.request(
                "POST",
                "tickets_search",
                json={
                    "filterGroups": _sev1_ticket_filter(self._mapping),
                    "properties": [self._mapping.ticket_priority_property],
                    "limit": 1,
                },
            )
        except HubSpotError as exc:
            return CheckResult(name="Tickets", ok=False, message=exc.message)
        return CheckResult(name="Tickets", ok=True, message="High-priority tickets can be read.")
