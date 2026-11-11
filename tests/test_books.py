"""Books (core/books.py): separate folders, keys and scoring, one open at a time."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from wardmind.core import books, config, secrets, service, vault
from wardmind.core.books import BookError
from wardmind.core.journal import Journal, TouchKind
from wardmind.core.settings import DataSource, load_settings, save_settings
from wardmind.core.store import LocalStore, data_dir

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 11)


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))
    monkeypatch.setattr(vault, "dpapi_unprotect", lambda blob: bytes(reversed(blob[2:])))


def _note(text: str) -> None:
    Journal(LocalStore(data_dir())).add_note(
        "acc-1", TouchKind.NOTE, text, TODAY, now=NOW, today=TODAY
    )


def _notes() -> list[str]:
    return [note.text for note in Journal(LocalStore(data_dir())).for_account("acc-1").notes]


# --- The list ------------------------------------------------------------------------------------


def test_without_books_json_there_is_one_book_in_the_old_place() -> None:
    listed = books.load()
    assert [(book.id, book.label) for book in listed.books] == [("default", "Book 1")]
    assert books.active_id() == "default"
    assert secrets.secrets_dir() == secrets.root_dir()
    assert not books.books_path().exists()


def test_adding_a_book_gives_it_its_own_folder_but_does_not_open_it() -> None:
    book = books.add("Book 2")
    assert book.id.startswith("b-") and book.label == "Book 2"
    assert books.active_id() == "default"
    assert books.book_folder(book.id) == secrets.root_dir() / "books" / book.id
    assert books.book_folder(book.id).is_dir()
    saved = json.loads(books.books_path().read_text(encoding="utf-8"))
    assert saved["current"] == "default"
    assert [item["label"] for item in saved["books"]] == ["Book 1", "Book 2"]


def test_switching_moves_the_data_secrets_and_scoring_folders() -> None:
    book = books.add("Book 2")
    books.switch(book.id)
    assert books.active_id() == book.id
    assert data_dir() == secrets.root_dir() / "books" / book.id / "data"
    assert secrets.secrets_dir() == secrets.root_dir() / "books" / book.id
    assert config.scoring_path() == config.config_dir() / "scoring.yaml"  # until saved
    assert config.scoring_path(saving=True) == books.book_folder(book.id) / "scoring.yaml"


def test_notes_and_tokens_stay_in_their_own_book() -> None:
    _note("first book note")
    secrets.save_openrouter_key("sk-or-v1-" + "a" * 64)
    second = books.add("Book 2")
    books.switch(second.id)
    assert _notes() == []
    assert secrets.get_openrouter_key() is None
    _note("second book note")
    books.switch("default")
    assert _notes() == ["first book note"]
    assert secrets.get_openrouter_key() == "sk-or-v1-" + "a" * 64


def test_a_book_has_its_own_scoring_once_saved() -> None:
    first = config.load_scoring_config()
    book = books.add("Book 2")
    books.switch(book.id)
    weight = 0.9 if first.arr.weight != 0.9 else 0.1
    changed = first.model_copy(update={"arr": first.arr.model_copy(update={"weight": weight})})
    config.save_scoring_config(changed)
    assert config.load_scoring_config().arr.weight == weight
    books.switch("default")
    assert config.load_scoring_config().arr.weight == first.arr.weight


def test_labels_are_cleaned_and_checked() -> None:
    assert books.add("  Renewals \t Q4  ").label == "Renewals Q4"
    with pytest.raises(BookError, match="Give the book a name"):
        books.add("   ")
    with pytest.raises(BookError, match="already has that name"):
        books.add("renewals q4")
    with pytest.raises(BookError, match="40 characters"):
        books.add("x" * 41)
    assert books.suggested_label() == "Book 2"


def test_rename_keeps_the_id_and_refuses_a_taken_name() -> None:
    book = books.add("Book 2")
    assert books.rename(book.id, "Spare").id == book.id
    with pytest.raises(BookError):
        books.rename(book.id, "Book 1")
    assert books.rename("default", "Main").label == "Main"
    assert [b.label for b in books.load().books] == ["Main", "Spare"]


def test_the_first_book_and_the_open_book_cannot_be_deleted() -> None:
    book = books.add("Book 2")
    with pytest.raises(BookError, match="first book"):
        books.delete("default")
    books.switch(book.id)
    with pytest.raises(BookError, match="Switch to another book"):
        books.delete(book.id)


def test_deleting_a_book_removes_its_folder_and_entry() -> None:
    book = books.add("Book 2")
    books.switch(book.id)
    _note("goes away")
    books.switch("default")
    books.delete(book.id)
    assert not books.book_folder(book.id).exists()
    assert [b.id for b in books.load().books] == ["default"]
    with pytest.raises(BookError):
        books.switch(book.id)


def test_a_damaged_books_json_falls_back_to_the_first_book() -> None:
    books.books_path().parent.mkdir(parents=True, exist_ok=True)
    books.books_path().write_text("{nope", encoding="utf-8")
    assert books.active_id() == "default"
    books.books_path().write_text(
        json.dumps({"current": "b-00000000", "books": [{"id": "../x", "label": "bad"}]}),
        encoding="utf-8",
    )
    assert [b.id for b in books.load().books] == ["default"]
    assert books.active_id() == "default"


def test_a_pinned_process_reads_its_own_book_whatever_is_current() -> None:
    book = books.add("Book 2")
    books.pin(book.id)
    assert books.active_id() == book.id
    books.pin(None)
    assert books.active_id() == "default"
    with pytest.raises(BookError, match="no book with the id"):
        books.pin("b-12345678")


def test_the_environment_variable_pins_too(monkeypatch: pytest.MonkeyPatch) -> None:
    book = books.add("Book 2")
    monkeypatch.setenv(books.ENV_BOOK, book.id)
    assert books.active_id() == book.id


def test_there_is_a_limit_on_books() -> None:
    for number in range(2, books.MAX_BOOKS + 1):
        books.add(f"Book {number}")
    with pytest.raises(BookError, match="up to"):
        books.add("One more")


# --- Demo data ------------------------------------------------------------------------------------


def test_demo_notes_are_kept_with_the_first_book_whichever_book_was_open() -> None:
    save_settings(load_settings().model_copy(update={"source": DataSource.DEMO}))
    book = books.add("Book 2")
    service.account_journal("acc-001")  # reads without creating
    books.switch(book.id)
    save_settings(load_settings().model_copy(update={"source": DataSource.DEMO}))
    service.get_journal().add_note(
        "acc-001", TouchKind.NOTE, "demo note", TODAY, now=NOW, today=TODAY
    )
    books.switch("default")
    assert [n.text for n in service.account_journal("acc-001").notes] == ["demo note"]
    assert not (books.book_data_dir(book.id) / "demo-journal.json").exists()


# --- Encryption -------------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_dpapi")
def test_turning_encryption_on_encrypts_every_book_with_its_own_key() -> None:
    _note("first secret note")
    book = books.add("Book 2")
    books.switch(book.id)
    _note("second secret note")
    vault.enable(use_dpapi=True)
    raw = [path.read_bytes() for path in vault.managed_files()]
    assert len(raw) == 2 and all(body.startswith(vault.MAGIC) for body in raw)
    keys = json.loads(vault.keys_path().read_text(encoding="utf-8"))
    assert set(keys["books"]) == {"default", book.id}
    assert _notes() == ["second secret note"]
    books.switch("default")
    assert _notes() == ["first secret note"]


@pytest.mark.usefixtures("fake_dpapi")
def test_a_book_added_while_encrypted_gets_a_key_and_its_files_are_encrypted() -> None:
    vault.enable(use_dpapi=True)
    book = books.add("Book 2")
    assert book.id in json.loads(vault.keys_path().read_text(encoding="utf-8"))["books"]
    books.switch(book.id)
    _note("encrypted from the start")
    path = books.book_data_dir(book.id) / "journal.json"
    assert path.read_bytes().startswith(vault.MAGIC)
    assert _notes() == ["encrypted from the start"]


@pytest.mark.usefixtures("fake_dpapi")
def test_a_file_copied_into_another_book_is_refused() -> None:
    vault.enable(use_dpapi=True)
    _note("first book only")
    book = books.add("Book 2")
    target = books.book_data_dir(book.id) / "journal.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((data_dir() / "journal.json").read_bytes())
    books.switch(book.id)
    with pytest.raises(vault.Damaged):
        _notes()


@pytest.mark.usefixtures("fake_dpapi")
def test_deleting_a_book_removes_its_key() -> None:
    vault.enable(use_dpapi=True)
    book = books.add("Book 2")
    books.delete(book.id)
    assert book.id not in json.loads(vault.keys_path().read_text(encoding="utf-8"))["books"]


@pytest.mark.usefixtures("fake_dpapi")
def test_adding_a_book_while_locked_is_refused_and_lists_nothing() -> None:
    vault.enable(use_dpapi=True)
    vault.lock()
    with pytest.raises(vault.Locked):
        books.add("Book 2")
    assert [b.id for b in books.load().books] == ["default"]


@pytest.mark.usefixtures("fake_dpapi")
def test_turning_encryption_off_decrypts_every_book() -> None:
    book = books.add("Book 2")
    books.switch(book.id)
    _note("second")
    vault.enable(use_dpapi=True)
    vault.disable()
    assert not any(path.read_bytes().startswith(vault.MAGIC) for path in vault.managed_files())
    assert _notes() == ["second"]


def test_a_file_in_an_unlisted_book_folder_is_not_the_first_books() -> None:
    stray = secrets.root_dir() / "books" / "b-deadbeef" / "data" / "journal.json"
    with pytest.raises(vault.VaultError):
        vault.locate(stray)


# --- Delete all local data ------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_dpapi")
def test_delete_all_local_data_removes_every_book_and_opens_the_first() -> None:
    _note("first")
    book = books.add("Book 2")
    books.switch(book.id)
    _note("second")
    vault.enable(use_dpapi=True)
    service.delete_all_local_data()
    assert not books.other_books_dir().exists()
    assert not books.books_path().exists()
    assert not vault.keys_path().exists()
    assert books.active_id() == "default"
    assert _notes() == []
