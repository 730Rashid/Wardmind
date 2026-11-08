"""The generic feed providers: the helpers every provider shares, files (CSV, TSV, text, Excel,
read-only SQLite), web links, Google Sheets and a JSON API with pagination.

Requests go through an httpx.MockTransport. The databases and workbooks are built in tmp_path.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from cs_brief.core.feeds import catalog, sync
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds.http import FeedError
from cs_brief.core.feeds.providers import base, files, get_provider, web
from cs_brief.core.feeds.providers.base import email_domain, json_path, records_to_table

SECRET = "test-secret-0123456789abcdef"
SHEET_EXPORT = "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=0"


def fetch(provider_id: str, settings: dict[str, str], secret: str | None = None):
    """The provider's Table, through the same path a sync takes."""
    return sync.fetch_with(provider_id, settings, secret)


def serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return seen


def _json_response(body: object) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json"}
    )


def _csv(text: str, content_type: str = "text/csv") -> httpx.Response:
    return httpx.Response(200, content=text.encode("utf-8"), headers={"content-type": content_type})


def _workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Company", "ARR"])
    sheet.append(["Acme", 120000])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# Shared helpers -------------------------------------------------------------------------------


def test_every_catalog_id_has_a_provider_whose_info_matches() -> None:
    for info in catalog.INFOS:
        provider = get_provider(info.id)
        assert provider is not None, info.id
        assert provider.info.id == info.id


def test_an_unknown_id_has_no_provider() -> None:
    assert get_provider("nonsense") is None


def test_records_to_table_flattens_nested_objects_to_dotted_headers() -> None:
    table = records_to_table(
        [{"name": "Acme", "address": {"city": "Leeds", "zip": {"code": "LS1"}}}], "x.json"
    )
    assert table.headers == ["name", "address.city", "address.zip.code"]
    assert table.rows == [["Acme", "Leeds", "LS1"]]


def test_records_to_table_joins_list_items_and_skips_objects_in_lists() -> None:
    table = records_to_table([{"tags": ["a", None, {"x": 1}, "b"], "empty": []}], "x")
    assert table.rows == [["a, b", ""]]


def test_records_to_table_keeps_numbers_as_json_text_and_booleans_as_words() -> None:
    table = records_to_table(
        [{"none": None, "int": 1, "float": 1.5, "whole": 2.0, "yes": True, "no": False}], "x"
    )
    assert table.rows == [["", "1", "1.5", "2.0", "true", "false"]]


def test_records_to_table_takes_headers_in_first_seen_order_and_blanks_missing_keys() -> None:
    table = records_to_table([{"a": "1"}, {"b": "2", "a": "3"}], "x")
    assert table.headers == ["a", "b"]
    assert table.rows == [["1", ""], ["3", "2"]]


def test_records_to_table_with_columns_fixes_headers_and_drops_extra_keys() -> None:
    table = records_to_table([{"b": "2", "extra": "9"}], "x", columns=["a", "b"])
    assert table.headers == ["a", "b"]
    assert table.rows == [["", "2"]]


def test_records_to_table_drops_fully_blank_records_and_non_objects() -> None:
    table = records_to_table([{"a": None}, {"a": "x"}, {}, "text"], "x")
    assert table.rows == [["x"]]


def test_records_to_table_refuses_more_than_the_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "MAX_ROWS", 2)
    with pytest.raises(FeedError, match="more than 2 rows"):
        records_to_table([{"a": "1"}, {"a": "2"}, {"a": "3"}], "x")


def test_json_path_empty_is_the_whole_document() -> None:
    assert json_path({"a": 1}, "") == {"a": 1}


def test_json_path_follows_keys_and_list_indexes() -> None:
    data = {"data": {"items": [1, 2]}, "results": [{"rows": ["x"]}]}
    assert json_path(data, "data.items") == [1, 2]
    assert json_path(data, "results.0.rows") == ["x"]


@pytest.mark.parametrize("path", ["data.missing", "results.5", "results.x", "data..items"])
def test_json_path_missing_step_is_a_plain_error(path: str) -> None:
    data = {"data": {"items": []}, "results": [{"rows": []}]}
    with pytest.raises(FeedError, match=f"Nothing at {path} in the response."):
        json_path(data, path)


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("jo@acme.com", "acme.com"),
        (" JO@Acme.COM. ", "acme.com"),
        ("sam@sub.acme.co.uk", "sub.acme.co.uk"),
        ("jo@gmail.com", ""),
        ("jo@outlook.com", ""),
        ("jo@localhost", ""),
        ("no-at-sign", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_email_domain_gives_the_company_domain_or_nothing(email: str | None, domain: str) -> None:
    assert email_domain(email) == domain


# Files ----------------------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def test_a_csv_file_reads_as_a_table(tmp_path: Path) -> None:
    path = _write(tmp_path / "accounts.csv", 'Company,ARR\nAcme,"$120,000"\n')
    table = fetch("file", {"path": str(path)})
    assert table.source_name == "accounts.csv"
    assert table.headers == ["Company", "ARR"]
    assert table.rows == [["Acme", "$120,000"]]


def test_a_tsv_file_reads_its_tabs(tmp_path: Path) -> None:
    path = _write(tmp_path / "accounts.tsv", "Company\tARR\nAcme\t5\n")
    assert fetch("file", {"path": str(path)}).headers == ["Company", "ARR"]


def test_a_text_file_with_semicolons_is_sniffed(tmp_path: Path) -> None:
    path = _write(tmp_path / "accounts.txt", "Company;ARR\nAcme;5\n")
    assert fetch("file", {"path": str(path)}).rows == [["Acme", "5"]]


def test_an_excel_file_reads_the_named_sheet_or_the_first_with_data(tmp_path: Path) -> None:
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    summary.append(["Note"])
    summary.append(["hello"])
    accounts = workbook.create_sheet("Accounts")
    accounts.append(["Company", "ARR"])
    accounts.append(["Acme", 120000])
    path = tmp_path / "book.xlsx"
    workbook.save(path)

    named = fetch("file", {"path": str(path), "sheet": "Accounts"})
    assert named.headers == ["Company", "ARR"]
    assert named.rows == [["Acme", "120000"]]
    first = fetch("file", {"path": str(path), "sheet": ""})
    assert first.headers == ["Note"]


def test_an_excel_sheet_that_is_not_there_is_a_plain_error(tmp_path: Path) -> None:
    workbook = Workbook()
    path = tmp_path / "book.xlsx"
    workbook.save(path)
    with pytest.raises(FeedError, match="no sheet called"):
        fetch("file", {"path": str(path), "sheet": "Nope"})


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FeedError, match="no file at that path"):
        fetch("file", {"path": str(tmp_path / "gone.csv")})


def test_a_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FeedError, match="is a folder"):
        fetch("file", {"path": str(tmp_path)})


def test_a_blank_path_is_refused() -> None:
    with pytest.raises(FeedError, match="Choose the file first."):
        fetch("file", {"path": "  "})


def test_an_unsupported_extension_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "report.pdf", "%PDF")
    with pytest.raises(FeedError, match="can't read .pdf"):
        fetch("file", {"path": str(path)})


def test_a_file_over_the_size_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "MAX_BYTES", 5)
    path = _write(tmp_path / "accounts.csv", "Company,ARR\nAcme,5\n")
    with pytest.raises(FeedError, match="larger than 25 MB"):
        fetch("file", {"path": str(path)})


def test_an_empty_file_is_refused_with_the_import_message(tmp_path: Path) -> None:
    path = _write(tmp_path / "accounts.csv", "  \n")
    with pytest.raises(FeedError, match="That file is empty."):
        fetch("file", {"path": str(path)})


def test_the_file_modification_time_is_the_source_mtime(tmp_path: Path) -> None:
    path = _write(tmp_path / "accounts.csv", "Company,ARR\n")
    provider = get_provider("file")
    assert provider is not None
    assert provider.source_mtime({"path": str(path)}) == os.stat(path).st_mtime
    assert provider.source_mtime({"path": str(tmp_path / "gone.csv")}) is None
    assert provider.source_mtime({"path": ""}) is None


# SQLite ---------------------------------------------------------------------------------------


def _make_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE accounts (name TEXT, arr INTEGER, notes TEXT)")
    connection.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?)",
        [("Acme", 120000, None), ("Beta", 5, "renewal soon")],
    )
    connection.commit()
    connection.close()
    return path


def test_a_select_returns_the_rows_as_text(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "product.db")
    table = fetch(
        "sqlite",
        {"path": str(path), "query": "SELECT name AS Company, arr AS ARR, notes FROM accounts"},
    )
    assert table.headers == ["Company", "ARR", "notes"]
    assert table.rows == [["Acme", "120000", ""], ["Beta", "5", "renewal soon"]]


def test_duplicate_result_columns_get_numbers(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "product.db")
    table = fetch("sqlite", {"path": str(path), "query": "SELECT name, name FROM accounts"})
    assert table.headers == ["name", "name (2)"]


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO accounts (name) VALUES ('Gamma')",
        "UPDATE accounts SET arr = 0",
        "DELETE FROM accounts",
        "DROP TABLE accounts",
        "CREATE TABLE evil (x)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA writable_schema = ON",
        "PRAGMA table_info(accounts)",
        "SELECT 1; DELETE FROM accounts",
    ],
)
def test_writes_and_extra_statements_are_refused_and_the_file_is_unchanged(
    tmp_path: Path, query: str
) -> None:
    path = _make_db(tmp_path / "product.db")
    before = path.read_bytes()
    with pytest.raises(FeedError):
        fetch("sqlite", {"path": str(path), "query": query})
    assert path.read_bytes() == before


def test_two_statements_get_the_one_query_message(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "product.db")
    with pytest.raises(FeedError, match="Write one SELECT query"):
        fetch("sqlite", {"path": str(path), "query": "SELECT 1; SELECT 2"})


def test_a_refused_write_says_only_select_is_allowed(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "product.db")
    with pytest.raises(FeedError, match="Only read-only SELECT queries"):
        fetch("sqlite", {"path": str(path), "query": "DELETE FROM accounts"})


def test_the_row_cap_refuses_a_bigger_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "MAX_ROWS", 2)
    path = _make_db(tmp_path / "product.db")
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO accounts VALUES ('Gamma', 1, NULL)")
    connection.commit()
    connection.close()
    with pytest.raises(FeedError, match="more than 2 rows"):
        fetch("sqlite", {"path": str(path), "query": "SELECT * FROM accounts"})
    assert (
        len(fetch("sqlite", {"path": str(path), "query": "SELECT * FROM accounts LIMIT 2"}).rows)
        == 2
    )


def test_an_empty_query_and_a_bad_query_are_plain_errors(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "product.db")
    with pytest.raises(FeedError, match="Write the query first."):
        fetch("sqlite", {"path": str(path), "query": "  "})
    with pytest.raises(FeedError, match="didn't run"):
        fetch("sqlite", {"path": str(path), "query": "SELEC name FROM accounts"})


def test_a_missing_or_non_database_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FeedError, match="no database file"):
        fetch("sqlite", {"path": str(tmp_path / "gone.db"), "query": "SELECT 1"})
    path = _write(tmp_path / "notes.db", "this is not a database")
    with pytest.raises(FeedError, match="didn't run"):
        fetch("sqlite", {"path": str(path), "query": "SELECT * FROM accounts"})


def test_the_sqlite_source_mtime_is_the_file_time(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "product.db")
    provider = get_provider("sqlite")
    assert provider is not None
    assert provider.source_mtime({"path": str(path)}) == os.stat(path).st_mtime


# Links ----------------------------------------------------------------------------------------


def test_a_csv_link_reads_as_a_table_with_the_file_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    table = fetch("link", {"url": "https://exports.example.com/accounts.csv", "auth": "none"})
    assert table.source_name == "accounts.csv"
    assert table.rows == [["Acme", "5"]]
    assert seen[0].headers.get("authorization") is None


def test_a_link_with_no_file_name_is_named_after_its_host(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    table = fetch("link", {"url": "https://exports.example.com/"})
    assert table.source_name == "exports.example.com"


def test_json_with_a_records_path_reads_those_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(
        monkeypatch,
        lambda request: _json_response({"data": {"items": [{"name": "Acme", "arr": 5}]}}),
    )
    table = fetch("link", {"url": "https://api.example.com/x", "records_path": "data.items"})
    assert table.headers == ["name", "arr"]
    assert table.rows == [["Acme", "5"]]


@pytest.mark.parametrize(
    ("content_type", "url", "body"),
    [
        ("application/json", "https://api.example.com/export", b'[{"name": "Acme"}]'),
        ("text/plain", "https://api.example.com/accounts.json", b'[{"name": "Acme"}]'),
        ("text/plain", "https://api.example.com/export", b'[{"name": "Acme"}]'),
    ],
)
def test_format_is_worked_out_from_type_then_extension_then_body(
    monkeypatch: pytest.MonkeyPatch, content_type: str, url: str, body: bytes
) -> None:
    serve(
        monkeypatch,
        lambda request: httpx.Response(200, content=body, headers={"content-type": content_type}),
    )
    table = fetch("link", {"url": url, "format": "auto"})
    assert table.headers == ["name"]
    assert table.rows == [["Acme"]]


def test_a_chosen_format_wins_over_the_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: _csv("name,arr\nAcme,5\n", content_type="application/json"))
    table = fetch("link", {"url": "https://api.example.com/x", "format": "csv"})
    assert table.headers == ["name", "arr"]


def test_a_tsv_link_reads_its_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: httpx.Response(200, content=b"name\tarr\nAcme\t5\n"))
    table = fetch("link", {"url": "https://exports.example.com/accounts.tsv", "format": "auto"})
    assert table.headers == ["name", "arr"]
    assert table.source_name == "accounts.tsv"


def test_an_excel_link_reads_its_workbook(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _workbook_bytes()
    serve(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            },
        ),
    )
    table = fetch("link", {"url": "https://exports.example.com/export", "format": "auto"})
    assert table.headers == ["Company", "ARR"]
    assert table.rows == [["Acme", "120000"]]


def test_a_web_page_is_not_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=b"<!DOCTYPE html><html>Log in</html>",
            headers={"content-type": "text/html"},
        ),
    )
    with pytest.raises(FeedError, match="opens a web page, not a file"):
        fetch("link", {"url": "https://exports.example.com/accounts"})


def test_invalid_json_is_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"{nope", headers={"content-type": "application/json"}
        ),
    )
    with pytest.raises(FeedError, match="didn't send JSON"):
        fetch("link", {"url": "https://api.example.com/x", "format": "json"})


def test_rows_that_are_not_a_list_are_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda request: _json_response({"data": {}}))
    with pytest.raises(FeedError, match="aren't a list"):
        fetch("link", {"url": "https://api.example.com/x", "records_path": "data"})


def test_bearer_auth_is_in_the_authorization_header_and_never_in_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    fetch("link", {"url": "https://api.example.com/a.csv", "auth": "bearer"}, SECRET)
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in str(seen[0].url)


def test_header_auth_uses_the_header_the_csm_named(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    fetch(
        "link",
        {"url": "https://api.example.com/a.csv", "auth": "header", "header_name": "X-API-Key"},
        SECRET,
    )
    assert seen[0].headers["x-api-key"] == SECRET
    assert SECRET not in str(seen[0].url)


def test_basic_auth_sends_the_username_and_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    fetch(
        "link",
        {"url": "https://api.example.com/a.csv", "auth": "basic", "username": "csm"},
        SECRET,
    )
    expected = "Basic " + base64.b64encode(f"csm:{SECRET}".encode()).decode("ascii")
    assert seen[0].headers["authorization"] == expected
    assert SECRET not in str(seen[0].url)


def test_no_sign_in_sends_no_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    fetch("link", {"url": "https://api.example.com/a.csv", "auth": "none"}, SECRET)
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    ("settings", "secret", "message"),
    [
        (
            {"url": "https://a.example.com/x", "auth": "bearer"},
            None,
            "Type the key, token or password first.",
        ),
        (
            {"url": "https://a.example.com/x", "auth": "header"},
            SECRET,
            "Give the header name, such as X-API-Key.",
        ),
        (
            {"url": "https://a.example.com/x", "auth": "header", "header_name": "X API"},
            SECRET,
            "Give the header name, such as X-API-Key.",
        ),
        ({"url": "https://a.example.com/x", "auth": "basic"}, SECRET, "Type the username first."),
        ({"url": "https://a.example.com/x", "auth": "magic"}, SECRET, "Choose how to sign in."),
        ({"url": "  "}, None, "Paste the web address first."),
        ({"url": "http://example.com/a.csv"}, None, "must start with https://"),
    ],
)
def test_bad_link_settings_are_plain_errors(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], secret: str | None, message: str
) -> None:
    serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    with pytest.raises(FeedError, match=message):
        fetch("link", settings, secret)


# Google Sheets --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "export"),
    [
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit#gid=456",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=456",
        ),
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing&gid=7",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=7",
        ),
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=0",
        ),
        (
            "https://docs.google.com/spreadsheets/u/2/d/abc123/edit#gid=0",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=0",
        ),
        (
            "  https://docs.google.com/spreadsheets/d/abc123/  ",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=0",
        ),
        (
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vT/pub?gid=0&single=true&output=csv",
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vT/pub?output=csv&gid=0",
        ),
    ],
)
def test_a_sheet_link_becomes_its_csv_export(address: str, export: str) -> None:
    assert web.sheet_export_url(address) == export


@pytest.mark.parametrize(
    "address",
    [
        "https://example.com/spreadsheets/d/abc123/edit",
        "https://docs.google.com/document/d/abc123/edit",
        "https://docs.google.com/spreadsheets/",
        "https://docs.google.com/spreadsheets/d/abc123/edit#gid=abc",
        "https://docs.google.com/spreadsheets/d/abc123/edit?gid=-1",
    ],
)
def test_a_link_that_is_not_a_sheet_is_refused(address: str) -> None:
    with pytest.raises(FeedError, match="That isn't a Google Sheets link."):
        web.sheet_export_url(address)


def test_a_blank_sheet_link_asks_for_one() -> None:
    with pytest.raises(FeedError, match="Paste the sheet's link first."):
        web.sheet_export_url("  ")


def test_a_public_sheet_reads_its_csv_export(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, lambda request: _csv("Company,ARR\nAcme,5\n"))
    table = fetch(
        "google_sheets",
        {"url": "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0"},
    )
    assert str(seen[0].url) == SHEET_EXPORT
    assert table.source_name == "Google Sheets"
    assert table.rows == [["Acme", "5"]]


def test_a_sign_in_page_is_explained_in_plain_words(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "docs.google.com":
            return httpx.Response(
                302, headers={"location": "https://accounts.example.com/ServiceLogin?continue=x"}
            )
        return httpx.Response(
            200,
            content=b"<!DOCTYPE html><html>Sign in</html>",
            headers={"content-type": "text/html"},
        )

    serve(monkeypatch, handler)
    with pytest.raises(FeedError) as caught:
        fetch("google_sheets", {"url": "https://docs.google.com/spreadsheets/d/abc123/edit"})
    assert caught.value.message == (
        "Google wants a sign-in. Share the sheet as Anyone with the link can view."
    )


# JSON API -------------------------------------------------------------------------------------


def _pages(pages: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(pages[str(request.url)])

    return handler


def test_pages_are_followed_through_an_absolute_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://api.example.com/v1/accounts": {
            "data": [{"name": "Acme"}],
            "links": {"next": "https://api.example.com/v1/accounts?page=2"},
        },
        "https://api.example.com/v1/accounts?page=2": {
            "data": [{"name": "Beta"}],
            "links": {"next": None},
        },
    }
    seen = serve(monkeypatch, _pages(pages))
    table = fetch(
        "json_api",
        {
            "url": "https://api.example.com/v1/accounts",
            "records_path": "data",
            "next_path": "links.next",
        },
    )
    assert table.rows == [["Acme"], ["Beta"]]
    assert len(seen) == 2


def test_a_relative_next_link_is_resolved_against_the_current_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "https://api.example.com/v1/accounts": {
            "data": [{"name": "Acme"}],
            "next": "?page=2",
        },
        "https://api.example.com/v1/accounts?page=2": {"data": [{"name": "Beta"}]},
    }
    serve(monkeypatch, _pages(pages))
    table = fetch(
        "json_api",
        {"url": "https://api.example.com/v1/accounts", "records_path": "data", "next_path": "next"},
    )
    assert table.rows == [["Acme"], ["Beta"]]


@pytest.mark.parametrize("next_value", [None, "", "   "])
def test_pagination_stops_on_a_null_or_empty_next(
    monkeypatch: pytest.MonkeyPatch, next_value: object
) -> None:
    pages = {
        "https://api.example.com/v1/accounts": {"data": [{"name": "Acme"}], "next": next_value}
    }
    seen = serve(monkeypatch, _pages(pages))
    table = fetch(
        "json_api",
        {"url": "https://api.example.com/v1/accounts", "records_path": "data", "next_path": "next"},
    )
    assert table.rows == [["Acme"]]
    assert len(seen) == 1


def test_pagination_stops_when_the_next_path_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {"https://api.example.com/v1/accounts": {"data": [{"name": "Acme"}]}}
    seen = serve(monkeypatch, _pages(pages))
    table = fetch(
        "json_api",
        {
            "url": "https://api.example.com/v1/accounts",
            "records_path": "data",
            "next_path": "links.next",
        },
    )
    assert table.rows == [["Acme"]]
    assert len(seen) == 1


def test_pagination_stops_when_a_page_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://api.example.com/v1/accounts": {"data": [{"name": "Acme"}], "next": "?page=2"},
        "https://api.example.com/v1/accounts?page=2": {
            "data": [{"name": "Beta"}],
            "next": "https://api.example.com/v1/accounts",
        },
    }
    seen = serve(monkeypatch, _pages(pages))
    table = fetch(
        "json_api",
        {"url": "https://api.example.com/v1/accounts", "records_path": "data", "next_path": "next"},
    )
    assert table.rows == [["Acme"], ["Beta"]]
    assert len(seen) == 2


def test_more_pages_than_the_cap_is_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "MAX_PAGES", 3)

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(str(request.url).rsplit("=", 1)[1]) if "page=" in str(request.url) else 1
        return _json_response({"data": [{"page": page}], "next": f"?page={page + 1}"})

    seen = serve(monkeypatch, handler)
    with pytest.raises(FeedError, match="more than 3 pages"):
        fetch(
            "json_api",
            {
                "url": "https://api.example.com/v1/accounts",
                "records_path": "data",
                "next_path": "next",
            },
        )
    assert len(seen) == 3


def test_the_sign_in_is_sent_on_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://api.example.com/v1/accounts": {"data": [{"name": "Acme"}], "next": "?page=2"},
        "https://api.example.com/v1/accounts?page=2": {"data": [{"name": "Beta"}]},
    }
    seen = serve(monkeypatch, _pages(pages))
    fetch(
        "json_api",
        {
            "url": "https://api.example.com/v1/accounts",
            "records_path": "data",
            "next_path": "next",
            "auth": "bearer",
        },
        SECRET,
    )
    assert [request.headers["authorization"] for request in seen] == [f"Bearer {SECRET}"] * 2


def test_a_next_link_to_plain_http_elsewhere_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://api.example.com/v1/accounts": {
            "data": [{"name": "Acme"}],
            "next": "http://elsewhere.example.com/accounts?page=2",
        }
    }
    serve(monkeypatch, _pages(pages))
    with pytest.raises(FeedError, match="must start with https://"):
        fetch(
            "json_api",
            {
                "url": "https://api.example.com/v1/accounts",
                "records_path": "data",
                "next_path": "next",
            },
        )


def test_a_json_api_is_named_after_its_last_path_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {"https://api.example.com/v1/accounts": {"data": [{"name": "Acme"}]}}
    serve(monkeypatch, _pages(pages))
    table = fetch(
        "json_api", {"url": "https://api.example.com/v1/accounts", "records_path": "data"}
    )
    assert table.source_name == "accounts"


@pytest.mark.parametrize(
    "next_link",
    [
        "https://other.example.com/v1/accounts?page=2",
        "https://api.example.com:8443/v1/accounts?page=2",
    ],
)
def test_a_next_link_on_another_site_is_refused_before_it_is_requested(
    monkeypatch: pytest.MonkeyPatch, next_link: str
) -> None:
    pages = {"https://api.example.com/v1/accounts": {"data": [{"name": "Acme"}], "next": next_link}}
    seen = serve(monkeypatch, _pages(pages))
    with pytest.raises(FeedError) as caught:
        fetch(
            "json_api",
            {
                "url": "https://api.example.com/v1/accounts",
                "records_path": "data",
                "next_path": "next",
                "auth": "bearer",
            },
            SECRET,
        )
    assert caught.value.message == (
        "The next page is on a different site, so cs-brief won't send your key there."
    )
    assert len(seen) == 1
