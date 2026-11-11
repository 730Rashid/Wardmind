"""Books: separate sets of accounts, connections, notes and keys on one computer.

A book is a folder. The first book ("default", shown as "Book 1" until renamed) is the per-user
Wardmind folder itself, laid out exactly as before books existed, so nothing is moved. Every
other book lives in its own folder under it:

    %LOCALAPPDATA%\\wardmind\\            the default book: data\\, hubspot-token, feeds\\ ...
      books.json                          plain: each book's id and label, and which is open
      keys.json                           encryption keys for every book (core/vault.py)
      books\\<id>\\                        another book: data\\, its secrets, scoring.yaml

Per book: the data folder, every secret (HubSpot token, OpenRouter key, feed keys, mailbox
password) and the scoring. Shared by all books: the appearance settings (settings.yaml), the
HubSpot field mapping, the session link and the encryption keys file. Demo data is not a folder:
it is the Demo data choice (settings.source), shown beside the books in the switcher.

One book is open at a time per process. The web app and the MCP server follow books.json's
"current", unless a process is pinned to a book (`wardmind mcp --book <id>`, see pin()). Labels
are stored in plain text, so the Books page asks for a label that isn't a client's or employer's
name.

books.json is plain JSON written atomically through config.write_atomic. It holds no customer
data, and it has to be read before the book is unlocked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from wardmind.core.config import write_atomic

BOOKS_FILE = "books.json"
BOOKS_DIR = "books"
DEFAULT_BOOK = "default"
DEFAULT_LABEL = "Book 1"
MAX_BOOKS = 20
MAX_LABEL = 40
ENV_BOOK = "WARDMIND_BOOK"  # pins a process to a book (set by `wardmind mcp --book`)
_BOOK_ID = re.compile(r"b-[0-9a-f]{8}")
_LABEL_CHARS = re.compile(r"[^\x00-\x1f\x7f]+")


class BookError(ValueError):
    """A book change that can't be made, with a plain message for the page."""


@dataclass(frozen=True)
class Book:
    id: str
    label: str

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_BOOK


@dataclass(frozen=True)
class BookList:
    books: tuple[Book, ...]
    current: str

    def get(self, book_id: str) -> Book | None:
        return next((book for book in self.books if book.id == book_id), None)


# --- Where things are ----------------------------------------------------------------------------


def root_dir() -> Path:
    """The per-user Wardmind folder (secrets.root_dir): books.json, keys.json and the default
    book live here."""
    from wardmind.core.secrets import root_dir as secrets_root

    return secrets_root()


def books_path() -> Path:
    return root_dir() / BOOKS_FILE


def book_folder(book_id: str) -> Path:
    """The folder holding this book's secrets (and, below it, its data folder)."""
    if book_id == DEFAULT_BOOK:
        return root_dir()
    if not _BOOK_ID.fullmatch(book_id):
        raise BookError("That isn't a book.")
    return root_dir() / BOOKS_DIR / book_id


def book_data_dir(book_id: str) -> Path:
    """The book's data folder. The default book keeps WARDMIND_DATA_DIR (tests, the sandbox)."""
    if book_id == DEFAULT_BOOK:
        override = os.environ.get("WARDMIND_DATA_DIR", "").strip()
        if override:
            return Path(override)
    return book_folder(book_id) / "data"


def other_books_dir() -> Path:
    """The folder that holds every book but the default one."""
    return root_dir() / BOOKS_DIR


# --- books.json ----------------------------------------------------------------------------------

_cache: tuple[tuple[str, int, int], BookList] | None = None
_cache_lock = threading.Lock()
_pinned: str | None = None
# The book a background job (a feed sync, website lookups, a mailbox sync) started in. Set with
# bound(); while set, active_id() returns it, so a book opened in the meantime never receives
# that job's data or lends it its keys.
_bound: ContextVar[str | None] = ContextVar("wardmind_bound_book", default=None)


def _default_list() -> BookList:
    return BookList((Book(DEFAULT_BOOK, DEFAULT_LABEL),), DEFAULT_BOOK)


def load() -> BookList:
    """The books, from books.json (cached by its path, mtime and size, so a change made by
    another process is seen on the next call). No file, or one that can't be read, gives the
    default book alone: the default book's files are always where they were."""
    global _cache
    path = books_path()
    try:
        info = path.stat()
    except OSError:
        return _default_list()
    stamp = (str(path), info.st_mtime_ns, info.st_size)
    with _cache_lock:
        if _cache is not None and _cache[0] == stamp:
            return _cache[1]
    parsed = _parse(path)
    with _cache_lock:
        _cache = (stamp, parsed)
    return parsed


def _parse(path: Path) -> BookList:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["books"]
        books: list[Book] = []
        for item in items:
            book_id, label = str(item["id"]), str(item["label"])
            if book_id != DEFAULT_BOOK and not _BOOK_ID.fullmatch(book_id):
                continue
            if any(book.id == book_id for book in books):
                continue
            books.append(Book(book_id, _clean_label(label) or DEFAULT_LABEL))
        current = str(raw.get("current", DEFAULT_BOOK))
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return _default_list()
    if not any(book.is_default for book in books):
        books.insert(0, Book(DEFAULT_BOOK, DEFAULT_LABEL))
    if not any(book.id == current for book in books):
        current = DEFAULT_BOOK
    return BookList(tuple(books), current)


def _save(books: BookList) -> None:
    global _cache
    body = {
        "version": 1,
        "current": books.current,
        "books": [{"id": book.id, "label": book.label} for book in books.books],
    }
    write_atomic(books_path(), json.dumps(body, indent=2) + "\n")
    with _cache_lock:
        _cache = None


# --- The open book -------------------------------------------------------------------------------


def pin(book_id: str | None) -> None:
    """Pin this process to a book (the MCP server's --book), or unpin it with None. BookError for
    a book that doesn't exist."""
    global _pinned
    if book_id is not None and load().get(book_id) is None:
        raise BookError(f"There is no book with the id {book_id}. `wardmind books` lists them.")
    _pinned = book_id


def pinned() -> str | None:
    return _pinned or (os.environ.get(ENV_BOOK, "").strip() or None)


@contextmanager
def bound(book_id: str) -> Iterator[None]:
    """Run the code inside against this book, whichever book is opened meanwhile (in this thread
    or task; a new thread starts unbound, so a job binds inside its own thread)."""
    token = _bound.set(book_id)
    try:
        yield
    finally:
        _bound.reset(token)


def active_id() -> str:
    """The book this code works on: the one a running job is bound to (see bound), else the
    pinned one (the MCP server's --book), else books.json's current. BookError when the bound or
    pinned book has since been deleted: carrying on in another book would mix the two."""
    listed = load()
    held = _bound.get()
    if held is not None:
        if listed.get(held) is None:
            raise BookError("The book this job started in has been deleted.")
        return held
    chosen = pinned()
    if chosen is not None:
        if listed.get(chosen) is None:
            raise BookError(f"The book {chosen} has been deleted. `wardmind books` lists them.")
        return chosen
    return listed.current


def active() -> Book:
    listed = load()
    return listed.get(active_id()) or listed.books[0]


def all_ids() -> list[str]:
    """Every book's id, the default first."""
    return [book.id for book in load().books]


# --- Changing the list ---------------------------------------------------------------------------


def _clean_label(label: str) -> str:
    """The label with outer spaces removed and inner runs of spaces collapsed; control
    characters are dropped."""
    return " ".join("".join(_LABEL_CHARS.findall(label)).split())


def check_label(label: str, books: BookList | None = None, *, renaming: str | None = None) -> str:
    """The cleaned label, or BookError: blank, longer than MAX_LABEL, or the same as another
    book's label (ignoring case)."""
    cleaned = _clean_label(label)
    if not cleaned:
        raise BookError("Give the book a name, such as Book 2.")
    if len(cleaned) > MAX_LABEL:
        raise BookError(f"Keep the name to {MAX_LABEL} characters or fewer.")
    listed = books if books is not None else load()
    for book in listed.books:
        if book.id != renaming and book.label.casefold() == cleaned.casefold():
            raise BookError("Another book already has that name.")
    return cleaned


def suggested_label() -> str:
    """ "Book <n>" for the first n not already used."""
    taken = {book.label.casefold() for book in load().books}
    number = 1
    while f"book {number}" in taken:
        number += 1
    return f"Book {number}"


def add(label: str) -> Book:
    """Create an empty book and return it (it is not opened). BookError for a refused label or
    when there are MAX_BOOKS already. When encryption is on, the book gets its own key (vault
    .add_book_key), which needs Wardmind unlocked."""
    from wardmind.core import vault

    listed = load()
    if len(listed.books) >= MAX_BOOKS:
        raise BookError(f"Wardmind keeps up to {MAX_BOOKS} books.")
    cleaned = check_label(label, listed)
    book = Book(_new_id(listed), cleaned)
    vault.add_book_key(book.id)
    book_folder(book.id).mkdir(parents=True, exist_ok=True)
    _save(BookList((*listed.books, book), listed.current))
    return book


def _new_id(listed: BookList) -> str:
    while True:
        candidate = "b-" + os.urandom(4).hex()
        if listed.get(candidate) is None:
            return candidate


def rename(book_id: str, label: str) -> Book:
    listed = load()
    if listed.get(book_id) is None:
        raise BookError("That book no longer exists.")
    cleaned = check_label(label, listed, renaming=book_id)
    renamed = Book(book_id, cleaned)
    _save(
        BookList(
            tuple(renamed if book.id == book_id else book for book in listed.books), listed.current
        )
    )
    return renamed


def switch(book_id: str) -> Book:
    """Make this the book that opens (books.json's current). BookError for an unknown book."""
    listed = load()
    book = listed.get(book_id)
    if book is None:
        raise BookError("That book no longer exists.")
    if listed.current != book_id:
        _save(BookList(listed.books, book_id))
    return book


def delete(book_id: str) -> None:
    """Delete a book: its key first (so anything left behind can't be read), then its folder,
    then its entry. The default book and the open book can't be deleted (BookError)."""
    from wardmind.core import vault

    listed = load()
    book = listed.get(book_id)
    if book is None:
        raise BookError("That book no longer exists.")
    if book.is_default:
        raise BookError(f"{book.label} is the first book and can't be deleted.")
    if book_id == active_id() or book_id == listed.current:
        raise BookError("Switch to another book before deleting this one.")
    vault.remove_book_key(book_id)
    shutil.rmtree(book_folder(book_id), ignore_errors=True)
    _save(BookList(tuple(book for book in listed.books if book.id != book_id), listed.current))


def delete_everything() -> None:
    """Delete all local data: every book but the default one, and books.json. The default
    book's files are cleared by the caller (service.delete_all_local_data)."""
    global _cache
    shutil.rmtree(other_books_dir(), ignore_errors=True)
    books_path().unlink(missing_ok=True)
    with _cache_lock:
        _cache = None


def forget_cache() -> None:
    """Tests: drop the cached books.json and any pin."""
    global _cache, _pinned
    with _cache_lock:
        _cache = None
    _pinned = None
