"""Settings > Backup & export in the web UI: the page, making a backup (a download), restoring one
as a new book, and the CSV exports. Runs through the real routes against the temp folders from
conftest. core/backup.py does the work; these check the pages, the refusals (422, nothing written,
the password never sent back), the attachment headers, the lock, and the escaping.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import audit, backup, books, editing, secrets, service, vault
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.journal import Journal, TouchKind
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import LocalStore, data_dir
from wardmind.web import app as web
from wardmind.web import settings_backup

BASE = "http://127.0.0.1"
ORIGIN = {"origin": BASE}
PASSWORD = "lantern-orbit-7731"  # distinctive, so a copy into any response is easy to spot
TOKEN = "pat-na1-0123456789abcdef0123"
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 11)
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
MAKE_ACTION = 'action="/settings/backup/make"'
RESTORE_ACTION = 'action="/settings/backup/restore"'
BLOB_TYPE = "application/octet-stream"
WRONG_PASSWORD = "That password doesn't open this backup, or the file was changed."


@pytest.fixture(autouse=True)
def cheap_argon2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url=BASE)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[audit.Event, str | None, str]]:
    calls: list[tuple[audit.Event, str | None, str]] = []

    def record(event: audit.Event, *, book: str | None = None, detail: str = "") -> None:
        calls.append((event, book, detail))

    monkeypatch.setattr(audit, "record", record)
    return calls


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)
    monkeypatch.setattr(vault, "dpapi_available", lambda: True)


@pytest.fixture
def mine() -> None:
    """Your data is showing (the default is demo data, which has no book to back up)."""
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _post(client: TestClient, path: str, data: dict[str, str]) -> Any:
    return client.post(path, data=data, headers=ORIGIN, follow_redirects=False)


def _upload(client: TestClient, blob: bytes, *, password: str, name: str) -> Any:
    return client.post(
        "/settings/backup/restore",
        data={"password": password, "name": name},
        files={"file": ("backup.wmbackup", blob, BLOB_TYPE)},
        headers=ORIGIN,
        follow_redirects=False,
    )


def _make(client: TestClient, password: str = PASSWORD, **extra: str) -> Any:
    return _post(
        client, "/settings/backup/make", {"password": password, "confirm": password, **extra}
    )


def _make_blob(client: TestClient, **extra: str) -> bytes:
    response = _make(client, **extra)
    assert response.status_code == 200, response.text
    return response.content


def _manifest(blob: bytes) -> dict[str, Any]:
    payload = backup._open(blob, backup.read_header(blob), PASSWORD)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return json.loads(archive.read(backup.MANIFEST))


def _assert_clean(response: Any) -> None:
    """No inline script or style, and the password is not in what came back."""
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert PASSWORD not in response.text
    assert PASSWORD.encode() not in response.content


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.removeprefix("﻿"))))


def _seed_book() -> None:
    """The open book's data, a token and a feed key, so a backup has something to carry."""
    Journal(LocalStore(data_dir())).add_note(
        "acc-1", TouchKind.CALL, "Talked renewal", TODAY, now=NOW, today=TODAY
    )
    secrets.save_hubspot_token(TOKEN)
    secrets.save_secret("feed-1", "sheet password 1234")


# --- The page ----------------------------------------------------------------------------------


def test_the_page_has_the_three_sections_and_typed_password_fields(
    client: TestClient, mine: None
) -> None:
    response = client.get("/settings/backup")
    assert response.status_code == 200
    _assert_clean(response)
    markup = response.text
    assert "Back up this book" in markup
    assert "Restore a backup" in markup
    assert "Download accounts (CSV)" in markup and "Download notes and follow-ups (CSV)" in markup
    assert MAKE_ACTION in markup and RESTORE_ACTION in markup
    assert markup.count('type="password"') == 3  # the new password, its confirmation, the backup's
    assert 'autocomplete="new-password"' in markup
    assert 'name="include_keys"' in markup and "Include connection keys and tokens" in markup
    assert "Leave this off unless you are moving to a new computer." in markup
    assert 'accept=".wmbackup"' in markup
    assert 'enctype="multipart/form-data"' in markup
    assert "Download backup" in markup and "Restore as a new book" in markup
    assert "can't be recovered" in markup


def test_the_book_name_is_escaped_on_the_page(client: TestClient, mine: None) -> None:
    books.rename(books.DEFAULT_BOOK, "Tom & <b>Jerry</b>")
    response = client.get("/settings/backup")
    assert "Tom &amp; &lt;b&gt;Jerry&lt;/b&gt;" in response.text
    assert "<b>Jerry</b>" not in response.text


def test_demo_data_has_no_book_to_back_up(client: TestClient) -> None:
    response = client.get("/settings/backup")
    assert "there's no book to back up" in _text(response)
    assert MAKE_ACTION not in response.text
    assert RESTORE_ACTION in response.text  # restoring a backup doesn't need the book open


def test_a_backup_made_from_demo_data_is_refused_with_the_reason(
    client: TestClient, recorded: list[Any]
) -> None:
    response = _make(client)
    assert response.status_code == 422
    assert "Demo data is showing" in _text(response)
    _assert_clean(response)
    assert recorded == []


def test_the_last_backup_date_is_shown(
    client: TestClient, mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        backup, "last_backup_at", lambda book_id: datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    )
    assert "Last backed up 10 November 2026." in _text(client.get("/settings/backup"))


def test_a_book_never_backed_up_says_so(client: TestClient, mine: None) -> None:
    assert "hasn't been backed up yet" in _text(client.get("/settings/backup"))


def test_an_unknown_saved_code_shows_nothing(client: TestClient, mine: None) -> None:
    response = client.get("/settings/backup?saved=anything-else")
    assert "Restored as a new book" not in response.text
    assert "Wardmind couldn't make that export" not in response.text


def test_the_restored_notice_is_shown_from_its_code(client: TestClient, mine: None) -> None:
    text = client.get("/settings/backup?saved=restored").text
    assert "Restored as a new book. Open it from the book switcher." in text


# --- Making a backup ---------------------------------------------------------------------------


def test_a_backup_downloads_as_an_attachment_named_by_date(
    client: TestClient, mine: None, recorded: list[Any]
) -> None:
    _seed_book()
    response = _make(client)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="wardmind-backup-{date.today().isoformat()}.wmbackup"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith(BLOB_TYPE)
    assert response.content.startswith(b"WMBK\x01")
    assert PASSWORD.encode() not in response.content
    assert "Book 1" not in response.headers["content-disposition"]  # no book label in the name
    assert recorded == [(audit.Event.BACKUP_MADE, books.DEFAULT_BOOK, "")]


def test_the_keys_box_decides_whether_the_tokens_are_in_the_backup(
    client: TestClient, mine: None
) -> None:
    _seed_book()
    plain = _make_blob(client)
    assert _manifest(plain)["includes_keys"] is False
    assert not any(entry["path"].startswith("secrets/") for entry in _manifest(plain)["files"])

    keyed = _make_blob(client, include_keys="1")
    assert _manifest(keyed)["includes_keys"] is True
    paths = {entry["path"] for entry in _manifest(keyed)["files"]}
    assert "secrets/hubspot-token" in paths and "secrets/feeds/feed-1" in paths


@pytest.mark.parametrize(
    ("password", "confirm", "message"),
    [
        ("zqx-9", "zqx-9", "Use at least 10 characters."),
        (PASSWORD, "lantern-orbit-7732", "The two passwords don't match."),
        ("", "", "Use at least 10 characters."),
        ("line\nbreak-password", "line\nbreak-password", "can't contain line breaks"),
        ("x" * 1025, "x" * 1025, "under 1,024 characters"),
    ],
)
def test_a_refused_backup_password_returns_422_and_never_echoes_it(
    client: TestClient,
    mine: None,
    recorded: list[Any],
    password: str,
    confirm: str,
    message: str,
) -> None:
    response = _post(client, "/settings/backup/make", {"password": password, "confirm": confirm})
    assert response.status_code == 422
    assert message in _text(response)
    if password:
        assert password not in response.text
    assert response.headers.get("content-disposition") is None
    assert recorded == []


def test_a_backup_over_the_size_limit_is_refused_with_the_reason(
    client: TestClient, mine: None, recorded: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_book()
    monkeypatch.setattr(backup, "MAX_BOOK_BYTES", 10)
    response = _make(client)
    assert response.status_code == 422
    assert "more than 200 MB" in _text(response)
    assert recorded == []


def test_a_locked_book_refuses_a_backup_with_the_lock_gate(
    client: TestClient, fake_dpapi: None, mine: None
) -> None:
    _seed_book()
    vault.enable(password=PASSWORD, use_dpapi=True)
    vault.lock()
    response = _make(client)
    assert response.status_code == 423
    assert PASSWORD not in response.text


# --- Restoring a backup ------------------------------------------------------------------------


def test_a_backup_restores_as_a_new_book_through_the_routes(client: TestClient, mine: None) -> None:
    _seed_book()
    before = books.load().current
    blob = _make_blob(client)
    response = _upload(client, blob, password=PASSWORD, name="Book 2")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/backup?saved=restored"
    _assert_clean(response)

    labels = [book.label for book in books.load().books]
    assert "Book 2" in labels
    assert books.load().current == before  # the new book isn't opened
    notice = client.get(response.headers["location"]).text
    assert "Restored as a new book. Open it from the book switcher." in notice


def test_the_restored_book_carries_keys_only_when_the_backup_did(
    client: TestClient, mine: None
) -> None:
    _seed_book()
    plain = _make_blob(client)
    _upload(client, plain, password=PASSWORD, name="Plain copy")
    keyed = _make_blob(client, include_keys="1")
    _upload(client, keyed, password=PASSWORD, name="Keyed copy")

    plain_book = next(book for book in books.load().books if book.label == "Plain copy")
    keyed_book = next(book for book in books.load().books if book.label == "Keyed copy")
    assert not (books.book_folder(plain_book.id) / "hubspot-token").exists()
    assert vault.read_text(books.book_folder(keyed_book.id) / "hubspot-token") == TOKEN


@pytest.mark.parametrize(
    ("blob_kind", "password", "name", "message"),
    [
        ("none", PASSWORD, "Book 2", "Choose a backup file first."),
        ("junk", PASSWORD, "Book 2", "That file isn't a Wardmind backup."),
        ("real", "wrong-password-1234", "Book 2", WRONG_PASSWORD),
        ("real", "", "Book 2", "Type the password this backup was made with."),
        ("real", PASSWORD, "   ", "Give the book a name"),
        ("real", PASSWORD, "Book 1", "Another book already has that name."),
        ("real", PASSWORD, "x" * 41, "Keep the name to 40 characters or fewer."),
        ("tampered", PASSWORD, "Book 2", WRONG_PASSWORD),
    ],
)
def test_a_refused_restore_returns_422_with_its_message_and_adds_nothing(
    client: TestClient,
    mine: None,
    blob_kind: str,
    password: str,
    name: str,
    message: str,
) -> None:
    _seed_book()
    real = _make_blob(client)
    before = [book.id for book in books.load().books]
    if blob_kind == "none":
        response = client.post(
            "/settings/backup/restore",
            data={"password": password, "name": name},
            files={"file": ("", b"", BLOB_TYPE)},
            headers=ORIGIN,
            follow_redirects=False,
        )
    else:
        blob = {
            "junk": b"%PDF-1.7 this is not a backup",
            "real": real,
            "tampered": real[:-1] + bytes([real[-1] ^ 1]),
        }[blob_kind]
        response = _upload(client, blob, password=password, name=name)

    assert response.status_code == 422
    assert message in _text(response)
    assert PASSWORD not in response.text
    assert "wrong-password-1234" not in response.text
    assert [book.id for book in books.load().books] == before


def test_a_typed_name_comes_back_in_the_box(client: TestClient, mine: None) -> None:
    _seed_book()
    real = _make_blob(client)
    response = _upload(client, real, password=PASSWORD, name="Book 1")
    assert 'value="Book 1"' in response.text


def test_a_file_over_the_cap_is_refused_as_read(
    client: TestClient, mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 200)
    response = _upload(client, b"x" * 300, password=PASSWORD, name="Book 2")
    assert response.status_code == 422
    assert "That file is too big to be a Wardmind backup." in response.text
    assert PASSWORD not in response.text


def test_a_request_declared_over_the_cap_is_refused_before_it_is_read(
    client: TestClient, mine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 200)
    monkeypatch.setattr(settings_backup, "_FORM_ALLOWANCE", 0)
    response = _upload(client, b"x" * 300, password=PASSWORD, name="Book 2")
    assert response.status_code == 422
    assert "That file is too big to be a Wardmind backup." in response.text


def test_a_locked_book_refuses_a_restore_with_the_lock_gate(
    client: TestClient, fake_dpapi: None, mine: None
) -> None:
    _seed_book()
    vault.enable(password=PASSWORD, use_dpapi=True)
    blob = _make_blob(client)
    vault.lock()
    before = [book.id for book in books.load().books]
    response = _upload(client, blob, password=PASSWORD, name="Book 2")
    assert response.status_code == 423
    assert PASSWORD not in response.text
    assert [book.id for book in books.load().books] == before


def test_a_restore_into_an_encrypted_book_writes_encrypted_files(
    client: TestClient, fake_dpapi: None, mine: None
) -> None:
    _seed_book()
    vault.enable(password=PASSWORD, use_dpapi=True)
    blob = _make_blob(client)
    response = _upload(client, blob, password=PASSWORD, name="Book 2")
    assert response.status_code == 303
    new = next(book for book in books.load().books if book.label == "Book 2")
    files = [path for path in books.book_data_dir(new.id).rglob("*") if path.is_file()]
    assert files and all(vault.is_encrypted(path.read_bytes()) for path in files)


# --- CSV exports -------------------------------------------------------------------------------


def test_the_accounts_export_is_a_bom_prefixed_download_of_the_demo_book(
    client: TestClient, recorded: list[Any]
) -> None:
    response = client.get("/settings/export/accounts.csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="wardmind-accounts-{date.today().isoformat()}.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    rows = _rows(response.text)
    assert rows[0][:3] == ["Account", "Account id", "Health"]
    assert any(row[0] == "Hessler Fitness" for row in rows[1:])
    assert recorded == [(audit.Event.EXPORTED, None, "Accounts CSV")]


def test_the_accounts_export_lists_your_own_accounts_and_records_the_book(
    client: TestClient, mine: None, recorded: list[Any]
) -> None:
    account = editing.create_account(
        service.get_store(),
        {"name": "Acme Robotics", "arr": "$184,000", "renewal_date": TODAY.isoformat()},
        today=TODAY,
    )
    service.reset_connectors()
    rows = _rows(client.get("/settings/export/accounts.csv").text)
    assert any(row[0] == "Acme Robotics" and row[1] == account.id for row in rows[1:])
    assert recorded == [(audit.Event.EXPORTED, books.DEFAULT_BOOK, "Accounts CSV")]


def test_the_notes_export_has_each_note_and_guards_formulas(
    client: TestClient, recorded: list[Any]
) -> None:
    Journal(LocalStore(books.book_data_dir(books.DEFAULT_BOOK)), demo=True).add_note(
        "acc-001", TouchKind.CALL, '=HYPERLINK("http://x.example")', TODAY, now=NOW, today=TODAY
    )
    service.reset_connectors()
    response = client.get("/settings/export/notes.csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="wardmind-notes-{date.today().isoformat()}.csv"'
    )
    rows = _rows(response.text)
    assert rows[0] == ["Account", "Type", "Date", "Kind", "Subject", "Text", "Due", "Status"]
    assert any(
        row[0] == "Hessler Fitness" and row[5] == '\'=HYPERLINK("http://x.example")'
        for row in rows[1:]
    )
    assert recorded == [(audit.Event.EXPORTED, None, "Notes CSV")]


def test_an_export_that_cannot_load_the_book_goes_back_with_a_notice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    def broken() -> None:
        raise ConnectorError("HubSpot couldn't be reached.")

    monkeypatch.setattr(service, "get_book", broken)
    response = client.get("/settings/export/accounts.csv", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/backup?saved=export-failed"
    assert recorded == []
    assert "Wardmind couldn't make that export" in _text(client.get(response.headers["location"]))


def test_a_cross_site_post_is_refused(client: TestClient, mine: None) -> None:
    response = client.post(
        "/settings/backup/make",
        data={"password": PASSWORD, "confirm": PASSWORD},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
