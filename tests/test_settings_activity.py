"""Settings > Activity log: the page (the filter, the 200-row limit, the local times, the book
labels), the CSV download and its formula guard, the clear with its confirm box, and the routes
that record an event (spot checks). No entry may hold a book label, a token or anything typed."""

from __future__ import annotations

import csv
import html
import io
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import audit, books, vault
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.web import app as web
from wardmind.web import settings_activity

ORIGIN = {"origin": "http://127.0.0.1"}
BASE = "http://127.0.0.1"
PAGE = "/settings/activity"
ROW_WHEN = re.compile(r'<td class="activity-log-when">([^<]*)</td>')
# One table row: the when, what, book and detail cells (the sidebar has words that repeat).
ROW = re.compile(
    r'<tr>\s*<td class="activity-log-when">[^<]*</td>\s*<td>([^<]*)</td>\s*'
    r"<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*</tr>"
)
PASSWORD = "lantern-orbit-7731"
TOKEN_TEXT = "sk-or-v1-" + "9" * 30  # an OpenRouter key shape; must never reach the log
TOKEN = re.compile(r'name="pending" value="([0-9a-f]+)"')


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _table(markup: str) -> list[tuple[str, str, str]]:
    """The (what, book, detail) of every row of the activity table, in page order."""
    return [
        (html.unescape(what), html.unescape(book), html.unescape(detail))
        for what, book, detail in ROW.findall(markup)
    ]


def _whats(markup: str) -> list[str]:
    return [what for what, _, _ in _table(markup)]


def _log_text() -> str:
    path = audit._log_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def client() -> TestClient:
    save_settings(Settings(source=DataSource.MINE))
    return TestClient(web.create_app(), base_url=BASE)


@pytest.fixture
def cheap_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows protection is a reversible stand-in and the password's Argon2 cost is tiny."""
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)
    monkeypatch.setattr(vault, "dpapi_available", lambda: True)
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})


def _turn_on_with_password(client: TestClient) -> None:
    shown = client.post(
        "/settings/privacy/encryption/start",
        data={"method": "password", "password": PASSWORD, "confirm": PASSWORD},
        headers=ORIGIN,
    )
    assert shown.status_code == 200
    token = TOKEN.search(shown.text)
    assert token is not None
    done = _post(
        client, "/settings/privacy/encryption/on", {"pending": token.group(1), "saved": "1"}
    )
    assert done.status_code == 303


# --- The page --------------------------------------------------------------------------------


def test_an_empty_log_says_so_and_states_what_it_keeps(client: TestClient) -> None:
    response = client.get(PAGE)
    assert response.status_code == 200
    text = _text(response)
    assert "Activity log" in text
    assert "holds no customer data" in text
    assert "Nothing here yet." in text
    assert 'href="/settings/activity.csv"' in response.text
    assert "Download as CSV" in text
    assert 'action="/settings/activity/clear"' in response.text
    assert "None" not in text


def test_the_page_lists_newest_first_with_plain_words_and_local_times(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    audit.record(audit.Event.SETTINGS_SAVED, detail="Appearance")
    markup = client.get(PAGE).text
    assert '<th scope="col">When</th>' in markup
    assert _table(markup) == [("Settings saved", "", "Appearance"), ("Unlocked", "", "")]
    whens = ROW_WHEN.findall(markup)
    assert len(whens) == 2
    assert all(re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2}", when) for when in whens)


def test_a_book_shows_its_current_label_or_deleted_book(client: TestClient) -> None:
    added = books.add("Renewals 2027")
    audit.record(audit.Event.BOOK_OPENED, book=added.id, detail="Book opened")
    audit.record(audit.Event.BOOK_RENAMED, book="b-0000dead", detail="Book renamed")
    markup = client.get(PAGE).text
    assert _table(markup) == [
        ("Book renamed", "Deleted book", "Book renamed"),  # newest first
        ("Book opened", "Renewals 2027", "Book opened"),
    ]
    text = _text(client.get(PAGE))
    assert "Renewals 2027" in text
    assert "Deleted book" in text
    # The table shows the label, never the id (the header's switcher holds ids as form values).
    assert all(added.id not in cell for row in _table(markup) for cell in row)


def test_a_book_label_is_escaped_on_the_page(client: TestClient) -> None:
    added = books.add("<i>Tom</i> & co")
    audit.record(audit.Event.BOOK_ADDED, book=added.id, detail="Book added")
    markup = client.get(PAGE).text
    assert "<i>Tom</i>" not in markup
    assert "&lt;i&gt;Tom&lt;/i&gt; &amp; co" in markup


def test_the_filter_keeps_one_group(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)  # security
    audit.record(audit.Event.BOOK_ADDED, book=None, detail="Book added")  # data
    audit.record(audit.Event.SETTINGS_SAVED, detail="Scoring")  # settings
    assert _whats(client.get(PAGE).text) == ["Settings saved", "Book added", "Unlocked"]
    assert _whats(client.get(f"{PAGE}?group=security").text) == ["Unlocked"]
    assert _whats(client.get(f"{PAGE}?group=data").text) == ["Book added"]
    assert _whats(client.get(f"{PAGE}?group=settings").text) == ["Settings saved"]
    assert _whats(client.get(f"{PAGE}?group=nonsense").text) == [
        "Settings saved",
        "Book added",
        "Unlocked",
    ]


def test_the_filter_marks_the_chosen_kind(client: TestClient) -> None:
    markup = client.get(f"{PAGE}?group=security").text
    assert re.search(r'href="/settings/activity\?group=security"[^>]*aria-current="true"', markup)


def test_at_most_200_rows_are_shown_with_a_note_about_the_rest(client: TestClient) -> None:
    for number in range(settings_activity.SHOWN + 5):
        audit.record(audit.Event.SETTINGS_SAVED, detail=f"Save {number}")
    markup = client.get(PAGE).text
    assert len(ROW_WHEN.findall(markup)) == settings_activity.SHOWN
    assert "Showing the newest 200 of 205 entries." in _text(client.get(PAGE))
    assert "Download the CSV for the rest." in markup


# --- The CSV download ------------------------------------------------------------------------


def _rows(response: Any) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


def test_the_csv_is_an_attachment_with_the_same_four_columns(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    response = client.get("/settings/activity.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="wardmind-activity.csv"'
    )
    rows = _rows(response)
    assert rows[0] == ["When", "What", "Book", "Detail"]
    assert len(rows) == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", rows[1][0])
    assert rows[1][1] == "Unlocked"


def test_the_csv_shows_book_labels_and_deleted_books(client: TestClient) -> None:
    added = books.add("Quarter review")
    audit.record(audit.Event.BOOK_OPENED, book=added.id, detail="Book opened")
    audit.record(audit.Event.BOOK_DELETED, book="b-0000dead", detail="Book deleted")
    rows = _rows(client.get("/settings/activity.csv"))
    books_column = [row[2] for row in rows[1:]]
    assert books_column == ["Deleted book", "Quarter review"]


def test_the_csv_guards_cells_a_spreadsheet_would_run_as_formulas(client: TestClient) -> None:
    for text in ("=1+1", "+cmd", "-2", "@SUM(A1)"):
        audit.record(audit.Event.SETTINGS_SAVED, detail=text)
    rows = _rows(client.get("/settings/activity.csv"))
    details = [row[3] for row in rows[1:]]
    assert details == ["'@SUM(A1)", "'-2", "'+cmd", "'=1+1"]


def test_the_guard_covers_every_risky_start_and_leaves_plain_text_alone() -> None:
    for start in ("=", "+", "-", "@", "\t", "\r"):
        assert settings_activity._safe_cell(f"{start}x") == f"'{start}x"
    assert settings_activity._safe_cell("Unlocked") == "Unlocked"
    assert settings_activity._safe_cell("") == ""
    assert settings_activity._safe_cell("a=b") == "a=b"


def test_the_csv_takes_the_same_group_filter_as_the_page(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    audit.record(audit.Event.SETTINGS_SAVED, detail="Scoring")
    rows = _rows(client.get("/settings/activity.csv?group=settings"))
    assert [row[1] for row in rows[1:]] == ["Settings saved"]


# --- Clearing the log ------------------------------------------------------------------------


def test_clearing_needs_the_confirm_box_and_changes_nothing_without_it(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    audit.record(audit.Event.LOCKED, detail="by you")
    for fields in ({}, {"confirm": ""}, {"confirm": "yes"}):
        response = _post(client, "/settings/activity/clear", fields)
        assert response.status_code == 422
        assert "Tick the box to confirm you want to clear the log." in _text(response)
    assert [entry.event for entry in audit.entries()] == [audit.Event.LOCKED, audit.Event.UNLOCKED]


def test_clearing_with_the_box_ticked_empties_the_log_and_says_so(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    audit.record(audit.Event.LOCKED, detail="by you")
    response = _post(client, "/settings/activity/clear", {"confirm": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?saved=cleared"
    (only,) = audit.entries()
    assert only.event is audit.Event.LOG_CLEARED
    shown = _text(client.get(f"{PAGE}?saved=cleared"))
    assert "Activity log cleared." in shown
    assert "Log cleared" in shown


def test_an_unknown_saved_code_shows_no_notice(client: TestClient) -> None:
    assert "Activity log cleared." not in _text(client.get(f"{PAGE}?saved=whatever"))


# --- Routes that record an event (spot checks) -----------------------------------------------


def test_a_failed_unlock_and_a_good_one_are_both_recorded(
    client: TestClient, cheap_encryption: None
) -> None:
    _turn_on_with_password(client)
    book_id = books.active_id()
    vault.lock()
    wrong = _post(client, "/unlock", {"password": "not-the-password-at-all"})
    assert wrong.status_code == 422
    failed = audit.last(audit.Event.UNLOCK_FAILED, book=book_id)
    assert failed is not None
    right = _post(client, "/unlock", {"password": PASSWORD})
    assert right.status_code == 303
    assert audit.last(audit.Event.UNLOCKED, book=book_id) is not None
    assert PASSWORD not in _log_text()
    assert "not-the-password-at-all" not in _log_text()


def test_turning_encryption_on_is_recorded_for_the_open_book(
    client: TestClient, cheap_encryption: None
) -> None:
    _turn_on_with_password(client)
    entry = audit.last(audit.Event.ENCRYPTION_ON)
    assert entry is not None
    assert entry.book == books.active_id()
    assert PASSWORD not in _log_text()


def test_locking_now_is_recorded_as_by_you(client: TestClient, cheap_encryption: None) -> None:
    _turn_on_with_password(client)
    response = _post(client, "/lock")
    assert response.status_code == 303
    entry = audit.last(audit.Event.LOCKED)
    assert entry is not None and entry.detail == "by you"


def test_adding_a_book_is_recorded_with_its_id_and_never_its_label(client: TestClient) -> None:
    before = {book.id for book in books.load().books}
    response = _post(client, "/settings/books/add", {"label": "Client Pilot Book"})
    assert response.status_code == 303
    (new_id,) = {book.id for book in books.load().books} - before
    entry = audit.last(audit.Event.BOOK_ADDED)
    assert entry is not None and entry.book == new_id
    assert "Client Pilot" not in _log_text()


def test_switching_to_a_book_is_recorded(client: TestClient) -> None:
    added = books.add("Second book")
    response = _post(client, "/books/switch", {"book": added.id})
    assert response.status_code == 303
    entry = audit.last(audit.Event.BOOK_OPENED)
    assert entry is not None and entry.book == added.id


def test_renaming_and_deleting_a_book_are_recorded(client: TestClient) -> None:
    added = books.add("Old name")
    _post(client, f"/settings/books/{added.id}/rename", {"label": "New name"})
    renamed = audit.last(audit.Event.BOOK_RENAMED)
    assert renamed is not None and renamed.book == added.id
    _post(client, f"/settings/books/{added.id}/delete", {"confirm": "1"})
    deleted = audit.last(audit.Event.BOOK_DELETED)
    assert deleted is not None and deleted.book == added.id
    assert "New name" not in _log_text() and "Old name" not in _log_text()


def test_saving_a_key_is_recorded_by_service_name_and_the_key_never_appears(
    client: TestClient,
) -> None:
    response = _post(client, "/settings/privacy/ai/key", {"key": TOKEN_TEXT})
    assert response.status_code == 303
    entry = audit.last(audit.Event.KEY_SAVED)
    assert entry is not None and entry.detail == "OpenRouter"
    assert TOKEN_TEXT not in _log_text()
    _post(client, "/settings/privacy/ai/key/delete")
    deleted = audit.last(audit.Event.KEY_DELETED)
    assert deleted is not None and deleted.detail == "OpenRouter"


def test_saving_settings_is_recorded_by_section(client: TestClient) -> None:
    response = _post(
        client,
        "/settings/appearance",
        {"theme": "dark", "accent": "blue", "density": "compact", "landing": "book"},
    )
    assert response.status_code == 303
    entry = audit.last(audit.Event.SETTINGS_SAVED)
    assert entry is not None and entry.detail == "Appearance"
    assert load_settings().theme.value == "dark"


def test_a_refused_settings_save_records_nothing(client: TestClient) -> None:
    response = _post(client, "/settings/appearance", {"theme": "neon"})
    assert response.status_code == 422
    assert audit.last(audit.Event.SETTINGS_SAVED) is None


def test_deleting_all_local_data_is_recorded_after_it_happens(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    response = _post(client, "/settings/privacy/delete-all")
    assert response.status_code == 303
    newest = audit.entries(1)[0]
    assert newest.event is audit.Event.ALL_DATA_DELETED
    assert newest.detail == ""


def test_the_log_page_and_csv_never_show_a_token(client: TestClient) -> None:
    _post(client, "/settings/privacy/ai/key", {"key": TOKEN_TEXT})
    assert TOKEN_TEXT not in client.get(PAGE).text
    assert TOKEN_TEXT not in client.get("/settings/activity.csv").text


def test_the_log_file_is_plain_json_lines_in_the_root_folder(client: TestClient) -> None:
    audit.record(audit.Event.UNLOCKED)
    path = audit._log_path()
    assert path.name == audit.LOG_FILE
    assert path.parent == audit.root_dir()
    lines = [line for line in Path(path).read_text(encoding="utf-8").split("\n") if line]
    assert len(lines) == 1 and lines[0].startswith("{") and lines[0].endswith("}")
