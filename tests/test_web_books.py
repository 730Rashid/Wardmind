"""Books in the web UI: the header switcher, Settings > Books, and the routes that switch, add,
rename and delete a book. core/books.py does the work; these check the pages, the refusals, the
escaping of typed labels, and the lock. Book names are plain text in books.json, so every label
the pages show is checked for escaping."""

from __future__ import annotations

import html
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import books, vault
from wardmind.core.settings import DataSource, load_settings
from wardmind.web import app as web

BASE = "http://127.0.0.1"
ORIGIN = {"origin": BASE}
BOOK_LIST = re.compile(r'<ul class="book-list">(.*?)</ul>', re.DOTALL)
SWITCHER = re.compile(r'<details class="book-switcher">(.*?)</details>', re.DOTALL)
SUMMARY = re.compile(r'<summary class="book-switcher-summary">([^<]*)</summary>')
CURRENT = re.compile(r'<span class="book-switcher-item" aria-current="true">([^<]*)</span>')
CHOICE = re.compile(
    r'<input type="hidden" name="book" value="([^"]*)">\s*'
    r'<button type="submit" class="book-switcher-button">([^<]*)</button>'
)
ROW_NAME = re.compile(r'<p class="book-name">([^<]*)</p>')
DELETE_DISCLOSURE = '<details class="book-delete"'
OPEN_MARK = '<p class="book-open-mark">Open</p>'
SWITCH_FORM = 'action="/books/switch"'


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url=BASE)


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows protection replaced by a reversible stand-in (as in test_vault_integration.py)."""
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)


def _text(response: Any) -> str:
    """Page text with entities decoded: Jinja escapes apostrophes in variables."""
    return html.unescape(response.text)


def _book_list(markup: str) -> str:
    """The Settings > Books list of rows, without the header's switcher."""
    found = BOOK_LIST.search(markup)
    assert found is not None, "the page has no book list"
    return found.group(1)


def _switcher(markup: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    """The header switcher: its summary, the current item(s), and each button as (value, label)."""
    fragment = SWITCHER.search(markup)
    assert fragment is not None, "the header has no book switcher"
    body = fragment.group(1)
    summary = SUMMARY.search(body)
    assert summary is not None
    return summary.group(1), CURRENT.findall(body), CHOICE.findall(body)


def _switch(client: TestClient, book: str) -> None:
    response = client.post(
        "/books/switch", data={"book": book}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --- The header switcher -------------------------------------------------------------------------


def test_the_switcher_lists_every_book_and_shows_demo_data_open(client: TestClient) -> None:
    added = books.add("Book 2")
    summary, current, choices = _switcher(client.get("/").text)
    assert summary == "Demo data"
    assert current == ["Demo data"]
    assert choices == [("default", "Book 1"), (added.id, "Book 2")]


def test_the_switcher_marks_the_open_book_once_your_data_shows(client: TestClient) -> None:
    added = books.add("Book 2")
    _switch(client, added.id)
    summary, current, choices = _switcher(client.get("/").text)
    assert summary == "Book 2"
    assert current == ["Book 2"]
    assert choices == [("default", "Book 1"), ("demo", "Demo data")]


def test_the_switcher_has_no_script_style_or_event_handler(client: TestClient) -> None:
    books.add("Book 2")
    fragment = SWITCHER.search(client.get("/").text)
    assert fragment is not None
    markup = fragment.group(0)
    assert "style" not in markup
    assert "<script" not in markup
    assert not re.search(r"\son[a-z]+=", markup)


def test_the_switcher_links_to_manage_books(client: TestClient) -> None:
    text = client.get("/").text
    assert '<a class="book-switcher-manage" href="/settings/books">Manage books</a>' in text


def test_the_old_source_note_is_gone_from_the_header(client: TestClient) -> None:
    text = client.get("/").text
    assert "source-note" not in text
    assert "Showing demo data" not in text


def test_switching_to_a_book_shows_your_data_and_opens_that_book(client: TestClient) -> None:
    added = books.add("Book 2")
    response = client.post(
        "/books/switch", data={"book": added.id}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_settings().source is DataSource.MINE
    assert books.active_id() == added.id


def test_switching_to_demo_data_shows_demo_and_keeps_the_open_book(client: TestClient) -> None:
    added = books.add("Book 2")
    _switch(client, added.id)
    _switch(client, "demo")
    assert load_settings().source is DataSource.DEMO
    assert books.active_id() == added.id


def test_an_unknown_book_goes_back_with_a_notice_and_changes_nothing(client: TestClient) -> None:
    response = client.post(
        "/books/switch", data={"book": "b-00000000"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/books?notice=book-missing"
    assert load_settings().source is DataSource.DEMO
    assert books.active_id() == "default"
    page = _text(client.get(response.headers["location"]))
    assert "That book no longer exists. The list below is up to date." in page


def test_a_typed_book_field_is_never_echoed(client: TestClient) -> None:
    response = client.post(
        "/books/switch",
        data={"book": "<script>alert(1)</script>"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "alert(1)" not in response.headers["location"]
    assert "alert(1)" not in client.get(response.headers["location"]).text


# --- Settings > Books: the list -----------------------------------------------------------------


def test_the_books_page_is_in_the_settings_navigation_and_lists_each_book(
    client: TestClient,
) -> None:
    books.add("Book 2")
    response = client.get("/settings/books")
    assert response.status_code == 200
    assert "<title>Books - Wardmind</title>" in response.text
    assert (
        '<a class="settings-link" href="/settings/books" aria-current="page">Books</a>'
        in response.text
    )
    assert ROW_NAME.findall(response.text) == ["Book 1", "Book 2"]
    # Demo data is showing, so no book is open.
    assert OPEN_MARK not in response.text
    assert "Demo data is showing" in _text(response)


def test_the_open_book_is_marked_and_the_first_book_has_no_delete(client: TestClient) -> None:
    added = books.add("Book 2")
    _switch(client, added.id)
    rows = _book_list(client.get("/settings/books").text)
    assert rows.count(OPEN_MARK) == 1
    assert rows.count(SWITCH_FORM) == 1  # Book 1 offers Open; Book 2 is open already
    assert DELETE_DISCLOSURE not in rows  # the first book and the open book stay


def test_a_book_that_is_neither_first_nor_open_can_be_deleted(client: TestClient) -> None:
    added = books.add("Book 2")
    text = client.get("/settings/books").text
    assert text.count(DELETE_DISCLOSURE) == 1
    assert f'action="/settings/books/{added.id}/delete"' in text


def test_the_add_form_suggests_the_next_name_and_explains_names(client: TestClient) -> None:
    text = client.get("/settings/books").text
    assert 'name="label" id="field-book-label" value="Book 2"' in text
    assert (
        "Book names aren't encrypted, so use something like Book 2 rather than a client's or "
        "employer's name."
    ) in text


def test_the_add_form_is_replaced_at_the_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(books, "MAX_BOOKS", 2)
    books.add("Book 2")
    text = _text(client.get("/settings/books"))
    assert 'id="field-book-label"' not in text
    assert "Wardmind keeps up to 2 books. Delete one to add another." in text


# --- Adding ---------------------------------------------------------------------------------------


def test_adding_a_book_does_not_open_it_and_says_so(client: TestClient) -> None:
    response = client.post(
        "/settings/books/add", data={"label": "Book 2"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    added = next(book for book in books.load().books if book.label == "Book 2")
    assert response.headers["location"] == f"/settings/books?notice=added&book={added.id}"
    assert books.active_id() == "default"
    page = client.get(response.headers["location"])
    assert "Book 2 added. Open it to use it." in _text(page)
    assert f'name="book" value="{added.id}"' in _book_list(page.text)  # its row offers Open


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("   ", "Give the book a name, such as Book 2."),
        ("x" * 41, "Keep the name to 40 characters or fewer."),
    ],
)
def test_a_blank_or_long_name_is_refused_on_the_page(
    client: TestClient, typed: str, message: str
) -> None:
    response = client.post(
        "/settings/books/add", data={"label": typed}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 422
    assert message in _text(response)
    assert [book.label for book in books.load().books] == ["Book 1"]


def test_a_taken_name_is_refused_and_a_typed_label_is_escaped(client: TestClient) -> None:
    created = client.post(
        "/settings/books/add", data={"label": "<b>x</b>"}, headers=ORIGIN, follow_redirects=False
    )
    assert created.status_code == 303
    taken = client.post(
        "/settings/books/add", data={"label": "<b>x</b>"}, headers=ORIGIN, follow_redirects=False
    )
    assert taken.status_code == 422
    assert "Another book already has that name." in _text(taken)
    assert "<b>x</b>" not in taken.text
    assert "&lt;b&gt;x&lt;/b&gt;" in taken.text
    page = client.get("/settings/books").text
    assert "<b>x</b>" not in page
    assert page.count("&lt;b&gt;x&lt;/b&gt;") >= 2  # the row's name and its rename field


def test_adding_while_encrypted_gives_the_book_its_own_key(
    client: TestClient, fake_dpapi: None
) -> None:
    vault.enable(use_dpapi=True)
    response = client.post(
        "/settings/books/add", data={"label": "Book 2"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    added = next(book for book in books.load().books if book.label == "Book 2")
    keys = vault.load_keys()
    assert keys is not None and added.id in keys.books


# --- Renaming -------------------------------------------------------------------------------------


def test_rename_changes_the_label_and_keeps_the_id(client: TestClient) -> None:
    book = books.add("Book 2")
    response = client.post(
        f"/settings/books/{book.id}/rename",
        data={"label": "Book 3"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/books?notice=renamed"
    renamed = books.load().get(book.id)
    assert renamed is not None and renamed.label == "Book 3"
    assert "Book renamed." in _text(client.get(response.headers["location"]))


def test_rename_refuses_a_taken_name_and_shows_it_in_that_row(client: TestClient) -> None:
    books.add("Book 2")
    second = books.add("Book 3")
    response = client.post(
        f"/settings/books/{second.id}/rename",
        data={"label": "book 2"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    text = _text(response)
    assert "Another book already has that name." in text
    assert 'value="book 2"' in response.text
    assert 'aria-invalid="true"' in response.text
    unchanged = books.load().get(second.id)
    assert unchanged is not None and unchanged.label == "Book 3"


def test_renaming_an_unknown_book_is_a_not_found_page(client: TestClient) -> None:
    response = client.post(
        "/settings/books/b-00000000/rename",
        data={"label": "Nope"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert "That book doesn't exist. Pick one from the list." in _text(response)


# --- Deleting -------------------------------------------------------------------------------------


def test_delete_refuses_the_first_book_and_the_open_book(client: TestClient) -> None:
    added = books.add("Book 2")
    first = client.post(
        "/settings/books/default/delete",
        data={"confirm": "1"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert first.status_code == 422
    assert "Book 1 is the first book and can't be deleted." in _text(first)

    _switch(client, added.id)
    open_book = client.post(
        f"/settings/books/{added.id}/delete",
        data={"confirm": "1"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert open_book.status_code == 422
    assert "Switch to another book before deleting this one." in _text(open_book)
    assert books.load().get(added.id) is not None


def test_delete_needs_the_box_ticked(client: TestClient) -> None:
    book = books.add("Book 2")
    response = client.post(
        f"/settings/books/{book.id}/delete", data={}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 422
    assert "Tick the box to confirm you want to delete this book." in _text(response)
    assert books.load().get(book.id) is not None
    assert books.book_folder(book.id).is_dir()


def test_delete_removes_the_book_and_its_folder(client: TestClient) -> None:
    book = books.add("Book 2")
    data_dir = books.book_data_dir(book.id)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "kept.txt").write_text("only in Book 2", encoding="utf-8")
    response = client.post(
        f"/settings/books/{book.id}/delete",
        data={"confirm": "1"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/books?notice=deleted"
    assert books.load().get(book.id) is None
    assert not books.book_folder(book.id).exists()
    page = _text(client.get(response.headers["location"]))
    assert "The book and everything in it were deleted." in page
    assert f'action="/settings/books/{book.id}/delete"' not in page


def test_deleting_an_unknown_book_is_a_not_found_page(client: TestClient) -> None:
    response = client.post(
        "/settings/books/b-00000000/delete",
        data={"confirm": "1"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 404


# --- Locked books ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_dpapi")
def test_a_locked_book_sends_the_books_page_to_unlock_and_refuses_changes(
    client: TestClient,
) -> None:
    vault.enable(use_dpapi=True)
    vault.lock()
    page = client.get("/settings/books", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/unlock")

    added = client.post(
        "/settings/books/add", data={"label": "Book 2"}, headers=ORIGIN, follow_redirects=False
    )
    assert added.status_code == 423
    switched = client.post(
        "/books/switch", data={"book": "default"}, headers=ORIGIN, follow_redirects=False
    )
    assert switched.status_code == 423
    assert [book.label for book in books.load().books] == ["Book 1"]


@pytest.mark.usefixtures("fake_dpapi")
def test_the_header_switcher_names_the_open_book_while_it_is_locked(client: TestClient) -> None:
    vault.enable(use_dpapi=True)
    book = books.add("Book 2")
    _switch(client, book.id)
    vault.lock()
    # books.json is plain, so the switcher reads it without the key.
    menu = web._book_menu(load_settings())
    assert menu["current"] == "Book 2"
    assert [item["label"] for item in menu["books"]] == ["Book 1", "Book 2"]
