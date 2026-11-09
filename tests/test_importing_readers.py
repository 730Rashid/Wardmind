"""Reading files and pasted cells into a Table: CSV, TSV, pipe and semicolon files, Excel
workbooks (generated in code with openpyxl), limits and upload dispatch."""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

from wardmind.core.importing import (
    MAX_BYTES,
    MAX_ROWS,
    DateOrder,
    ImportError_,
    Table,
    auto_map,
    build_import,
    read_csv,
    read_pasted,
    read_upload,
    read_xlsx,
)

FIXTURES = Path(__file__).parent / "fixtures" / "import"
TOO_BIG = "That file is larger than 5 MB. Split it or remove extra columns."
TOO_MANY = "That file has more than 5,000 rows."
EMPTY = "That file is empty."
NOT_EXCEL = "That file isn't a readable Excel file. Save it as .xlsx or CSV and try again."


def _workbook(sheets: list[tuple[str, list[list]]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets:
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ----------------------------------------------------------------------------- read_csv


@pytest.mark.parametrize(
    ("delimiter", "name"),
    [(",", "comma"), (";", "semicolon"), ("\t", "tab"), ("|", "pipe")],
)
def test_read_csv_sniffs_each_delimiter(delimiter, name):
    data = delimiter.join(["Account name", "ARR", "Renewal date"]) + "\n"
    data += delimiter.join(["Acme", "120000", "2026-12-01"]) + "\n"
    table = read_csv(data.encode("utf-8"), f"{name}.csv")
    assert table.headers == ["Account name", "ARR", "Renewal date"]
    assert table.rows == [["Acme", "120000", "2026-12-01"]]
    assert table.source_name == f"{name}.csv"
    assert table.sheet is None
    assert table.sheet_names == []


def test_semicolon_file_keeps_decimal_commas_inside_cells():
    table = read_csv(b"Name;ARR\nAcme;120,5\nBeta;1.000\n", "eu.csv")
    assert table.rows == [["Acme", "120,5"], ["Beta", "1.000"]]


def test_one_line_header_is_sniffed_too():
    table = read_csv(b"Account name;ARR;Renewal date", "one.csv")
    assert table.headers == ["Account name", "ARR", "Renewal date"]
    assert table.rows == []


def test_single_column_file_falls_back_to_comma():
    table = read_csv(b'Names\nAcme Ltd\n"Beta, Inc"\n', "one-col.csv")
    assert table.headers == ["Names"]
    assert table.rows == [["Acme Ltd"], ["Beta, Inc"]]


def test_utf8_bom_is_removed_from_the_first_header():
    data = b"\xef\xbb\xbfAccount name,ARR\nAcme,1\n"
    table = read_csv(data, "bom.csv")
    assert table.headers == ["Account name", "ARR"]


def test_utf16_with_bom_is_read():
    table = read_csv("Name,ARR\nAcme,1\n".encode("utf-16"), "unicode.csv")
    assert table.headers == ["Name", "ARR"]
    assert table.rows == [["Acme", "1"]]


def test_utf8_accents_read_as_utf8():
    table = read_csv("Name\nCafé Müller\n".encode(), "utf8.csv")
    assert table.rows == [["Café Müller"]]


def test_cp1252_accents_fall_back_when_not_utf8():
    data = "Name,Note\nCafé,Ça va\n".encode("cp1252")
    table = read_csv(data, "cp1252.csv")
    assert table.rows == [["Café", "Ça va"]]


def test_bytes_undefined_in_cp1252_still_read_as_latin1():
    table = read_csv(b"Name\nAcme\x81\n", "odd.csv")
    assert table.rows == [["Acme\x81"]]


def test_quoted_commas_and_newlines_stay_in_their_cell():
    data = b'Name,Notes\n"Acme, Inc","line one\nline two"\n'
    table = read_csv(data, "quoted.csv")
    assert table.rows == [["Acme, Inc", "line one\nline two"]]


def test_crlf_line_endings():
    table = read_csv(b"Name,ARR\r\nAcme,1\r\nBeta,2\r\n", "crlf.csv")
    assert table.rows == [["Acme", "1"], ["Beta", "2"]]


def test_blank_headers_become_column_numbers():
    table = read_csv(b"Name,,ARR\nAcme,x,1\n", "blank.csv")
    assert table.headers == ["Name", "Column 2", "ARR"]


def test_duplicate_headers_get_a_number_in_brackets():
    table = read_csv(b"Owner,Owner,Owner\na,b,c\n", "dupes.csv")
    assert table.headers == ["Owner", "Owner (2)", "Owner (3)"]
    assert table.rows == [["a", "b", "c"]]


def test_a_real_header_called_like_a_blank_one_does_not_clash():
    table = read_csv(b"Column 2,,Column 2\n1,2,3\n", "clash.csv")
    assert table.headers == ["Column 2", "Column 2 (2)", "Column 2 (3)"]


def test_blank_rows_are_dropped_wherever_they_are():
    data = b"Name,ARR\n\nAcme,1\n,\n   ,  \nBeta,2\n\n"
    table = read_csv(data, "blank-rows.csv")
    assert table.rows == [["Acme", "1"], ["Beta", "2"]]


def test_cells_are_stripped():
    table = read_csv(b" Name , ARR \n  Acme  , 1 \n", "spaces.csv")
    assert table.headers == ["Name", "ARR"]
    assert table.rows == [["Acme", "1"]]


def test_short_rows_are_padded_and_long_rows_are_cut_to_the_header():
    table = read_csv(b"A,B,C\n1\n1,2,3,4\n", "ragged.csv")
    assert table.headers == ["A", "B", "C"]
    assert table.rows == [["1", "", ""], ["1", "2", "3"]]


def test_trailing_blank_header_columns_are_not_columns():
    table = read_csv(b"A,B,,\n1,2,,\n3,4\n", "trailing.csv")
    assert table.headers == ["A", "B"]
    assert table.rows == [["1", "2"], ["3", "4"]]


def test_a_blank_header_with_data_below_is_kept_as_a_column():
    table = read_csv(b"A,B,\n1,2,3\n", "data.csv")
    assert table.headers == ["A", "B", "Column 3"]
    assert table.rows == [["1", "2", "3"]]


def test_header_only_file_has_no_rows():
    table = read_csv(b"Account name,ARR\n", "header.csv")
    assert table.headers == ["Account name", "ARR"]
    assert table.rows == []


@pytest.mark.parametrize("data", [b"", b"   ", b"\n\n  \n", b"\xef\xbb\xbf", b",,,\n,,\n"])
def test_empty_input_is_refused(data):
    with pytest.raises(ImportError_) as caught:
        read_csv(data, "empty.csv")
    assert str(caught.value) == EMPTY


def test_file_over_the_byte_limit_is_refused_before_reading():
    with pytest.raises(ImportError_) as caught:
        read_csv(b"x" * (MAX_BYTES + 1), "big.csv")
    assert str(caught.value) == TOO_BIG


def test_file_at_the_byte_limit_is_read():
    # MAX_ROWS rows whose lengths add up to exactly MAX_BYTES (each cell stays under the csv
    # module's field size limit).
    body_total = MAX_BYTES - len(b"Name\n") - MAX_ROWS
    base, extra = divmod(body_total, MAX_ROWS)
    cells = ["a" * (base + 1)] * extra + ["a" * base] * (MAX_ROWS - extra)
    data = b"Name\n" + "".join(cell + "\n" for cell in cells).encode("ascii")
    assert len(data) == MAX_BYTES
    assert len(read_csv(data, "edge.csv").rows) == MAX_ROWS


def test_more_than_the_row_limit_is_refused():
    data = b"Name,ARR\n" + b"Acme,1\n" * (MAX_ROWS + 1)
    with pytest.raises(ImportError_) as caught:
        read_csv(data, "rows.csv")
    assert str(caught.value) == TOO_MANY


def test_exactly_the_row_limit_is_read():
    data = b"Name,ARR\n" + b"Acme,1\n" * MAX_ROWS
    table = read_csv(data, "rows.csv")
    assert len(table.rows) == MAX_ROWS


def test_blank_rows_do_not_count_towards_the_row_limit():
    data = b"Name,ARR\n" + b"Acme,1\n\n" * MAX_ROWS
    assert len(read_csv(data, "rows.csv").rows) == MAX_ROWS


def test_a_cell_longer_than_the_csv_module_allows_is_refused_plainly():
    data = b"Name\n" + b"x" * 200_000 + b"\n"
    with pytest.raises(ImportError_) as caught:
        read_csv(data, "long.csv")
    assert str(caught.value).startswith("That file couldn't be read as a table.")


def test_the_committed_gainsight_fixture_reads_with_its_headers():
    table = read_csv((FIXTURES / "gainsight_style_export.csv").read_bytes(), "g.csv")
    assert table.headers[:3] == ["Company", "Company ID", "CSM"]
    assert len(table.rows) == 2
    assert auto_map(table.headers)["name"] == "Company"


# --------------------------------------------------------------------------- read_pasted


def test_pasted_tab_cells_with_crlf_and_a_trailing_newline():
    table = read_pasted("Account name\tARR\r\nAcme\t$1\r\nBeta\t$2\r\n")
    assert table.source_name == "Pasted cells"
    assert table.headers == ["Account name", "ARR"]
    assert table.rows == [["Acme", "$1"], ["Beta", "$2"]]


def test_pasted_cells_with_a_quoted_multiline_note_keep_the_note_together():
    text = 'Account name\tNotes\nAcme\t"line one\nline two"\n'
    table = read_pasted(text)
    assert table.rows == [["Acme", "line one\nline two"]]


def test_pasted_cells_with_embedded_quotes_are_read():
    table = read_pasted('Account name\tNote\nAcme\t"say ""hi"""\n')
    assert table.rows == [["Acme", 'say "hi"']]


def test_pasted_commas_fall_back_to_the_sniffer():
    table = read_pasted("Account name,ARR\nAcme,1\n")
    assert table.headers == ["Account name", "ARR"]
    assert table.rows == [["Acme", "1"]]


def test_pasted_single_column_of_names_with_spaces():
    table = read_pasted("Name\nAcme Ltd\nBeta Co\n")
    assert table.rows == [["Acme Ltd"], ["Beta Co"]]


def test_pasted_bom_is_ignored():
    table = read_pasted("\ufeffAccount name\tARR\nAcme\t1\n")
    assert table.headers == ["Account name", "ARR"]


@pytest.mark.parametrize("text", ["", "   ", "\n\r\n", "\t\t\n"])
def test_empty_paste_is_refused_with_a_paste_message(text):
    with pytest.raises(ImportError_) as caught:
        read_pasted(text)
    assert str(caught.value) == "The pasted text is empty."


def test_paste_over_the_byte_limit_is_refused():
    with pytest.raises(ImportError_) as caught:
        read_pasted("Name\n" + "x" * MAX_BYTES)
    assert str(caught.value) == (
        "That paste is larger than 5 MB. Split it or remove extra columns."
    )


def test_paste_over_the_row_limit_is_refused():
    with pytest.raises(ImportError_) as caught:
        read_pasted("Name\n" + "a\n" * (MAX_ROWS + 1))
    assert str(caught.value) == "That paste has more than 5,000 rows."


def test_paste_limits_are_measured_in_utf8_bytes():
    # Each "é" is two bytes, so this paste is over the limit although it has fewer characters.
    text = "Name\n" + "é" * (MAX_BYTES // 2 + 10)
    assert len(text) < MAX_BYTES
    with pytest.raises(ImportError_):
        read_pasted(text)


# ------------------------------------------------------------------------------ read_xlsx


def test_default_sheet_is_the_first_one_with_any_data():
    data = _workbook(
        [
            ("Empty", []),
            ("Accounts", [["Account name", "ARR"], ["Acme", 120000]]),
            ("Notes", [["Something else"]]),
        ]
    )
    table = read_xlsx(data, "renewals.xlsx")
    assert table.source_name == "renewals.xlsx"
    assert table.sheet == "Accounts"
    assert table.sheet_names == ["Empty", "Accounts", "Notes"]
    assert table.headers == ["Account name", "ARR"]
    assert table.rows == [["Acme", "120000"]]


def test_named_sheet_is_chosen_even_when_it_is_not_first():
    data = _workbook(
        [("Accounts", [["Account name"], ["Acme"]]), ("Q4", [["Account name"], ["Beta"]])]
    )
    table = read_xlsx(data, "x.xlsx", sheet="Q4")
    assert table.sheet == "Q4"
    assert table.rows == [["Beta"]]


def test_sheet_name_matches_ignoring_case():
    data = _workbook([("Accounts", [["Account name"], ["Acme"]])])
    assert read_xlsx(data, "x.xlsx", sheet="accounts").sheet == "Accounts"


def test_missing_sheet_is_refused_and_lists_the_options():
    data = _workbook([("Accounts", [["Account name"], ["Acme"]])])
    with pytest.raises(ImportError_) as caught:
        read_xlsx(data, "x.xlsx", sheet="Nope")
    assert str(caught.value) == (
        'There\'s no sheet called "Nope". Pick one of the sheets in this file.'
    )


def test_named_empty_sheet_is_refused_with_its_name():
    data = _workbook([("Accounts", [["Account name"], ["Acme"]]), ("Blank", [])])
    with pytest.raises(ImportError_) as caught:
        read_xlsx(data, "x.xlsx", sheet="Blank")
    assert str(caught.value) == 'The sheet "Blank" is empty.'


def test_workbook_with_no_data_at_all_is_empty():
    data = _workbook([("One", []), ("Two", [[None, None]])])
    with pytest.raises(ImportError_) as caught:
        read_xlsx(data, "x.xlsx")
    assert str(caught.value) == EMPTY


def test_dates_become_iso_dates_and_times_drop_out():
    data = _workbook(
        [
            (
                "Accounts",
                [
                    ["Account name", "Renewal date", "Start"],
                    ["Acme", date(2026, 12, 1), datetime(2023, 3, 1, 9, 30)],
                ],
            )
        ]
    )
    table = read_xlsx(data, "dates.xlsx")
    assert table.rows == [["Acme", "2026-12-01", "2023-03-01"]]


def test_whole_floats_lose_the_point_zero_and_fractions_stay():
    data = _workbook([("Accounts", [["Name", "ARR", "NPS", "Count"], ["Acme", 120000.0, 8.5, 3]])])
    assert read_xlsx(data, "x.xlsx").rows == [["Acme", "120000", "8.5", "3"]]


def test_empty_cells_and_booleans():
    data = _workbook([("Accounts", [["Name", "Left", "Note"], ["Acme", True, None]])])
    assert read_xlsx(data, "x.xlsx").rows == [["Acme", "TRUE", ""]]


def test_formula_without_a_cached_value_reads_as_blank():
    data = _workbook([("Accounts", [["Name", "Double"], ["Acme", "=1+1"]])])
    table = read_xlsx(data, "x.xlsx")
    assert table.rows == [["Acme", ""]]


def test_text_cells_are_stripped():
    data = _workbook([("Accounts", [["  Name  "], ["  Acme  "]])])
    table = read_xlsx(data, "x.xlsx")
    assert table.headers == ["Name"]
    assert table.rows == [["Acme"]]


def test_blank_rows_inside_the_sheet_are_dropped():
    data = _workbook([("Accounts", [["Name"], [None], ["Acme"], [""], ["Beta"]])])
    assert read_xlsx(data, "x.xlsx").rows == [["Acme"], ["Beta"]]


def test_not_an_excel_file_is_refused():
    with pytest.raises(ImportError_) as caught:
        read_xlsx(b"just some text, not a workbook", "fake.xlsx")
    assert str(caught.value) == NOT_EXCEL


def test_zip_that_is_not_a_workbook_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hello")
    with pytest.raises(ImportError_) as caught:
        read_xlsx(buffer.getvalue(), "zip.xlsx")
    assert str(caught.value) == NOT_EXCEL


def test_workbook_over_the_byte_limit_is_refused_before_opening():
    with pytest.raises(ImportError_) as caught:
        read_xlsx(b"0" * (MAX_BYTES + 1), "big.xlsx")
    assert str(caught.value) == TOO_BIG


def test_workbook_over_the_row_limit_is_refused():
    rows = [["Name"]] + [["Acme"]] * (MAX_ROWS + 1)
    data = _workbook([("Accounts", rows)])
    with pytest.raises(ImportError_) as caught:
        read_xlsx(data, "many.xlsx")
    assert str(caught.value) == TOO_MANY


def test_chosen_sheet_name_and_sheet_list_are_on_the_table():
    data = _workbook(
        [("Accounts", [["Account name", "ARR", "Renewal date"], ["Acme", 1, "2026-12-01"]])]
    )
    table = read_xlsx(data, "x.xlsx")
    assert auto_map(table.headers) == {
        "name": "Account name",
        "arr": "ARR",
        "renewal_date": "Renewal date",
    }
    assert table.sheet_names == ["Accounts"]


# ------------------------------------------------------------------------------- upload


def test_upload_reads_csv_by_extension_in_any_case():
    table = read_upload(b"Name,ARR\nAcme,1\n", "Renewals.CSV")
    assert table.headers == ["Name", "ARR"]
    assert table.source_name == "Renewals.CSV"


def test_upload_reads_txt_with_the_sniffer():
    table = read_upload(b"Name;ARR\nAcme;1\n", "paste.txt")
    assert table.headers == ["Name", "ARR"]


def test_upload_forces_tab_for_tsv_even_when_cells_hold_commas():
    table = read_upload(b"Name\tNote\nAcme\tone, two\n", "export.tsv")
    assert table.headers == ["Name", "Note"]
    assert table.rows == [["Acme", "one, two"]]


def test_upload_reads_xlsx_with_the_sheet_argument():
    data = _workbook([("One", [["Name"], ["A"]]), ("Two", [["Name"], ["B"]])])
    assert read_upload(data, "book.xlsx", sheet="Two").rows == [["B"]]
    assert read_upload(data, "book.xlsx").sheet == "One"


def test_zip_content_is_read_as_excel_whatever_the_extension():
    data = _workbook([("Accounts", [["Name"], ["Acme"]])])
    assert read_upload(data, "actually-excel.csv").rows == [["Acme"]]


def test_full_windows_path_is_reduced_to_the_file_name():
    data = _workbook([("Accounts", [["Name"], ["Acme"]])])
    table = read_upload(data, "C:\\Users\\me\\Downloads\\renewals.xlsx")
    assert table.source_name == "renewals.xlsx"


def test_old_excel_files_are_refused_with_a_clear_message():
    for name in ("old.xls", "OLD.XLS"):
        with pytest.raises(ImportError_) as caught:
            read_upload(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32, name)
        assert str(caught.value) == (
            "Old Excel files (.xls) aren't supported. Save it as .xlsx or CSV and try again."
        )


def test_unknown_file_type_is_refused_by_name():
    with pytest.raises(ImportError_) as caught:
        read_upload(b"%PDF-1.4", "notes.pdf")
    assert str(caught.value) == (
        '"notes.pdf" isn\'t a file type Wardmind can read. Save it as .xlsx or CSV and try again.'
    )


def test_a_name_with_no_extension_is_refused():
    with pytest.raises(ImportError_):
        read_upload(b"Name\nAcme\n", "export")


def test_xlsx_name_with_csv_content_says_it_is_not_a_readable_excel_file():
    with pytest.raises(ImportError_) as caught:
        read_upload(b"Name,ARR\nAcme,1\n", "renamed.xlsx")
    assert str(caught.value) == NOT_EXCEL


def test_upload_passes_the_byte_limit_through():
    with pytest.raises(ImportError_) as caught:
        read_upload(b"x" * (MAX_BYTES + 1), "big.csv")
    assert str(caught.value) == TOO_BIG


# ----------------------------------------------------------------------------- row numbers

HEADER = "Account name,ARR,Renewal date"
HEADER_CELLS = ["Account name", "ARR", "Renewal date"]
GOOD_ROW = ["Good", "$1", "2026-12-01"]
BAD_ROW = ["Bad", "TBC", "2026-12-01"]
BAD_MESSAGE = 'ARR "TBC" isn\'t an amount.'


def _issue_rows(table: Table) -> list[tuple[int, str]]:
    result = build_import(
        table,
        auto_map(table.headers),
        connection_id="sheet-1",
        date_order=DateOrder.DMY,
        imported_at=datetime(2026, 10, 22, 9, 0),
        today=date(2026, 10, 22),
    )
    return [(issue.row, issue.message) for issue in result.issues]


def _cells_at(row: int, values: list[object]) -> dict[tuple[int, int], object]:
    return {(row, column): value for column, value in enumerate(values, start=1)}


def _sparse_workbook(cells: dict[tuple[int, int], object]) -> bytes:
    """A workbook with values at exact (row, column) positions, so rows can be left empty."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Accounts"
    for (row, column), value in cells.items():
        sheet.cell(row=row, column=column, value=value)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_csv_blank_line_on_line_two_puts_the_bad_row_on_row_four():
    data = f"{HEADER}\n\nGood,$1,2026-12-01\nBad,TBC,2026-12-01\n".encode()
    table = read_csv(data, "x.csv")
    assert table.row_numbers == [3, 4]
    assert _issue_rows(table) == [(4, BAD_MESSAGE)]


def test_csv_blank_lines_before_the_header_are_counted():
    table = read_csv(f"\n\n{HEADER}\nBad,TBC,2026-12-01\n".encode(), "x.csv")
    assert table.row_numbers == [4]
    assert _issue_rows(table) == [(4, BAD_MESSAGE)]


def test_csv_quoted_multiline_cell_counts_as_one_row():
    data = (
        b"Account name,Notes,ARR,Renewal date\n"
        b'Acme,"line one\nline two",$1,2026-12-01\n'
        b"Bad,x,TBC,2026-12-01\n"
    )
    table = read_csv(data, "x.csv")
    assert table.row_numbers == [2, 3]
    assert _issue_rows(table) == [(3, BAD_MESSAGE)]


def test_csv_row_numbers_count_the_blank_lines_between_records():
    data = b"Account name,ARR\n" + b"Acme,1\n\n" * MAX_ROWS
    table = read_csv(data, "rows.csv")
    assert table.row_numbers[:3] == [2, 4, 6]
    assert table.row_numbers[-1] == 2 * MAX_ROWS
    assert len(table.rows) == MAX_ROWS


def test_paste_blank_line_on_line_two_puts_the_bad_row_on_row_four():
    text = "Account name\tARR\tRenewal date\n\nGood\t$1\t2026-12-01\nBad\tTBC\t2026-12-01\n"
    table = read_pasted(text)
    assert table.row_numbers == [3, 4]
    assert _issue_rows(table) == [(4, BAD_MESSAGE)]


def test_paste_blank_lines_before_the_header_are_counted():
    table = read_pasted("\n\nAccount name\tARR\tRenewal date\nBad\tTBC\t2026-12-01\n")
    assert table.row_numbers == [4]
    assert _issue_rows(table) == [(4, BAD_MESSAGE)]


def test_xlsx_blank_row_two_puts_the_bad_row_on_row_four():
    cells = {
        **_cells_at(1, HEADER_CELLS),
        **_cells_at(3, GOOD_ROW),
        **_cells_at(4, BAD_ROW),
    }
    table = read_xlsx(_sparse_workbook(cells), "x.xlsx")
    assert table.row_numbers == [3, 4]
    assert _issue_rows(table) == [(4, BAD_MESSAGE)]


def test_xlsx_header_below_blank_rows_keeps_the_sheet_row_numbers():
    cells = {**_cells_at(3, HEADER_CELLS), **_cells_at(5, BAD_ROW)}
    table = read_xlsx(_sparse_workbook(cells), "x.xlsx")
    assert table.headers == HEADER_CELLS
    assert table.row_numbers == [5]
    assert _issue_rows(table) == [(5, BAD_MESSAGE)]


def test_xlsx_blank_row_between_data_rows_is_counted():
    cells = {
        **_cells_at(1, HEADER_CELLS),
        **_cells_at(2, GOOD_ROW),
        **_cells_at(4, BAD_ROW),
    }
    table = read_xlsx(_sparse_workbook(cells), "x.xlsx")
    assert table.row_numbers == [2, 4]
    assert _issue_rows(table) == [(4, BAD_MESSAGE)]
