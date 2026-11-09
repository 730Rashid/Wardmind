"""Web providers: a file at a web address (`link`), a Google Sheet (`google_sheets`) and a JSON API
with pagination (`json_api`). Every request goes through feeds.http.FeedHttp, which is GET only
(apart from the query POST of a product provider) and checks each address it visits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urljoin, urlsplit

from wardmind.core import importing
from wardmind.core.feeds import catalog
from wardmind.core.feeds.http import FeedError, FeedHttp, check_url, same_origin
from wardmind.core.feeds.providers.base import (
    MAX_PAGES,
    MAX_ROWS,
    MAX_TOTAL_BYTES,
    json_path,
    records_to_table,
)
from wardmind.core.importing import Table

# An HTTP header name: a token (RFC 7230), so no spaces, colons or line breaks.
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_SHEET_PATH = re.compile(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:/.*)?")
_PUBLISHED_PATH = re.compile(r"/spreadsheets/(?:u/\d+/)?d/(e/[A-Za-z0-9_-]+)/pub")
_GID = re.compile(r"[0-9]+")
_NOT_SHEET = "That isn't a Google Sheets link."
_SIGN_IN = "Google wants a sign-in. Share the sheet as Anyone with the link can view."
_NOT_PAGE = (
    "That link opens a web page, not a file. Use the address of the file itself, such as the "
    "export link."
)
_ZIP_MAGIC = b"PK\x03\x04"
_KINDS = ("csv", "tsv", "xlsx", "json")


def _text_of(value: str | None) -> str:
    return (value or "").strip()


def auth_for(
    settings: Mapping[str, str], secret: str | None
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """The request headers and basic-auth pair for the chosen sign-in. The secret only ever goes
    into these, never into the address."""
    mode = _text_of(settings.get("auth")) or "none"
    if mode == "none":
        return {}, None
    if not secret:
        raise FeedError("Type the key, token or password first.")
    if mode == "bearer":
        return {"Authorization": f"Bearer {secret}"}, None
    if mode == "header":
        name = _text_of(settings.get("header_name"))
        if not name or _HEADER_NAME.fullmatch(name) is None:
            raise FeedError("Give the header name, such as X-API-Key.")
        return {name: secret}, None
    if mode == "basic":
        username = _text_of(settings.get("username"))
        if not username:
            raise FeedError("Type the username first.")
        return {}, (username, secret)
    raise FeedError("Choose how to sign in.")


def source_name(url: str) -> str:
    """The last segment of the address's path (accounts.csv), else its host."""
    parts = urlsplit(url)
    return PurePosixPath(parts.path).name or parts.hostname or "Web link"


def _is_html(body: bytes, content_type: str) -> bool:
    if content_type.casefold().startswith("text/html"):
        return True
    head = body[:512].lstrip().lower()  # bytes have lower(), not casefold()
    return head.startswith((b"<!doctype", b"<html", b"<head"))


def _kind_by_type(content_type: str) -> str | None:
    media = content_type.split(";", 1)[0].strip().casefold()
    if media in ("application/json", "text/json") or media.endswith("+json"):
        return "json"
    if media in ("text/csv", "application/csv"):
        return "csv"
    if media == "text/tab-separated-values":
        return "tsv"
    if media == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return "xlsx"
    return None


def _kind_by_extension(url: str) -> str | None:
    return {".json": "json", ".csv": "csv", ".tsv": "tsv", ".xlsx": "xlsx"}.get(
        PurePosixPath(urlsplit(url).path).suffix.casefold()
    )


def _kind_by_content(body: bytes) -> str:
    """JSON if the body opens like JSON, Excel if it is a ZIP file, else text (CSV)."""
    if body.startswith(_ZIP_MAGIC):
        return "xlsx"
    if body.lstrip().startswith((b"{", b"[")):
        return "json"
    return "csv"


def _format_of(choice: str, content_type: str, url: str, body: bytes) -> str:
    """The format to read: the CSM's choice, else Content-Type, the address's extension, then
    the body itself (see _kind_by_content)."""
    if choice in _KINDS:
        return choice
    return _kind_by_type(content_type) or _kind_by_extension(url) or _kind_by_content(body)


def _json_records(data: object, records_path: str) -> list[object]:
    """The list of rows at records_path (the whole document when blank)."""
    value = json_path(data, records_path)
    if not isinstance(value, list):
        where = f"at {records_path}" if records_path else "in the response"
        raise FeedError(f"The rows {where} aren't a list. Check Where the rows are.")
    return value


def _decode_json(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8-sig"))
    except RecursionError:  # json nests by recursion, so a very deep document ends here
        raise FeedError("That JSON is nested too deeply.") from None
    except (UnicodeDecodeError, ValueError):
        raise FeedError("The web address didn't send JSON.") from None


def _table_from_body(
    body: bytes,
    kind: str,
    name: str,
    *,
    records_path: str = "",
    sheet: str | None = None,
) -> Table:
    """The table a body reads as, in the given format."""
    if kind == "json":
        records = _json_records(_decode_json(body), records_path)
        return records_to_table([record for record in records if isinstance(record, dict)], name)
    try:
        if kind == "xlsx":
            return importing.read_xlsx(body, name, sheet)
        if kind == "tsv":
            table = importing.read_upload(body, f"{name}.tsv")
            return table.model_copy(update={"source_name": name})
        return importing.read_csv(body, name)
    except importing.ImportError_ as exc:
        raise FeedError(str(exc)) from None


class LinkProvider:
    """A CSV, TSV, Excel or JSON file at a web address. Settings: url, format, records_path,
    auth (with header_name or username), and the secret."""

    info = catalog.provider_info("link")

    def fetch(self, settings: dict[str, str], secret: str | None, http: FeedHttp) -> Table:
        url = _text_of(settings.get("url"))
        if not url:
            raise FeedError("Paste the web address first.")
        url = check_url(url)
        headers, auth = auth_for(settings, secret)
        response = http.get(url, headers=headers, auth=auth)
        body = response.content
        content_type = response.headers.get("content-type", "")
        if _is_html(body, content_type):
            raise FeedError(_NOT_PAGE)
        kind = _format_of(_text_of(settings.get("format")) or "auto", content_type, url, body)
        return _table_from_body(
            body,
            kind,
            source_name(url),
            records_path=_text_of(settings.get("records_path")),
        )

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def sheet_export_url(address: str) -> str:
    """The CSV export address of a Google Sheets link, or FeedError when it isn't one.

    Accepts docs.google.com/spreadsheets/d/<id>/... and published-to-the-web links
    (/spreadsheets/d/e/<id>/pub). The tab is the gid in the query (?gid=) or the fragment
    (#gid=), and 0 (the first tab) when there is none."""
    text = _text_of(address)
    if not text:
        raise FeedError("Paste the sheet's link first.")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise FeedError(_NOT_SHEET) from None
    if parts.hostname != "docs.google.com":
        raise FeedError(_NOT_SHEET)
    gid = "0"
    for source in (parts.fragment, parts.query):
        found = parse_qs(source).get("gid")
        if found:
            gid = found[0]
            break
    if _GID.fullmatch(gid) is None:
        raise FeedError(_NOT_SHEET)
    published = _PUBLISHED_PATH.match(parts.path)
    if published is not None:
        return (
            f"https://docs.google.com/spreadsheets/d/{published.group(1)}/pub?output=csv&gid={gid}"
        )
    match = _SHEET_PATH.fullmatch(parts.path)
    if match is None:
        raise FeedError(_NOT_SHEET)
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv&gid={gid}"


class GoogleSheetsProvider:
    """A Google Sheet shared as "Anyone with the link can view", or published to the web. No
    sign-in. Settings: url."""

    info = catalog.provider_info("google_sheets")

    def fetch(self, settings: dict[str, str], secret: str | None, http: FeedHttp) -> Table:
        export = sheet_export_url(settings.get("url", ""))
        response = http.get(export)
        body = response.content
        if _is_html(body, response.headers.get("content-type", "")):
            raise FeedError(_SIGN_IN)
        try:
            return importing.read_csv(body, "Google Sheets")
        except importing.ImportError_ as exc:
            raise FeedError(str(exc)) from None

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None


def _next_url(data: object, next_path: str, current: str, first: str) -> str | None:
    """The next page's address (absolute, or relative to the current page), checked like any
    address. None when there is none: the path is missing, null, empty or not text. A next page
    on another site than the first page is refused before it is requested, so the key never
    goes there."""
    try:
        value = json_path(data, next_path)
    except FeedError:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    following = check_url(urljoin(current, value.strip()))
    if not same_origin(following, first):
        raise FeedError(
            "The next page is on a different site, so Wardmind won't send your key there."
        )
    return following


class JsonApiProvider:
    """Rows from a JSON API, following pages while the response names a next page. Settings:
    url, records_path, next_path, auth (with header_name or username), and the secret."""

    info = catalog.provider_info("json_api")

    def fetch(self, settings: dict[str, str], secret: str | None, http: FeedHttp) -> Table:
        url = _text_of(settings.get("url"))
        if not url:
            raise FeedError("Paste the web address first.")
        current = check_url(url)
        first = current  # next links must stay on this site
        headers, auth = auth_for(settings, secret)
        records_path = _text_of(settings.get("records_path"))
        next_path = _text_of(settings.get("next_path"))
        records: list[object] = []
        visited: set[str] = set()
        total_bytes = 0
        for page in range(1, MAX_PAGES + 1):
            visited.add(current)
            body = http.get(current, headers=headers, auth=auth).content
            total_bytes += len(body)
            if total_bytes > MAX_TOTAL_BYTES:
                raise FeedError(
                    f"That source sent more than {MAX_TOTAL_BYTES // 1_000_000} MB in total."
                )
            data = _decode_json(body)
            records.extend(
                record for record in _json_records(data, records_path) if isinstance(record, dict)
            )
            if len(records) > MAX_ROWS:
                raise FeedError(f"That source has more than {MAX_ROWS:,} rows.")
            following = _next_url(data, next_path, current, first) if next_path else None
            if following is None or following in visited:
                break
            if page == MAX_PAGES:
                raise FeedError(f"This API has more than {MAX_PAGES} pages. Narrow the request.")
            current = following
        return records_to_table(
            [record for record in records if isinstance(record, dict)], source_name(url)
        )

    def source_mtime(self, settings: dict[str, str]) -> float | None:
        return None
