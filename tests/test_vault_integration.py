"""Encryption at rest end to end: the real data modules, through core/vault.py."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from test_logos import png
from wardmind.core import logos, secrets, vault
from wardmind.core.journal import Journal, TouchKind
from wardmind.core.store import LocalStore, data_dir
from wardmind.core.vault import Damaged, Locked
from wardmind.web import app as web

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 11)


@pytest.fixture(autouse=True)
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)


@pytest.fixture
def store() -> LocalStore:
    return LocalStore(data_dir())


def _seed(store: LocalStore) -> None:
    Journal(store).add_note(
        "acc-1", TouchKind.CALL, "Talked renewal terms", TODAY, now=NOW, today=TODAY
    )
    secrets.save_secret("feed-1", "sheet password")
    logos.save_logo(store, "acc-1", png(), logos.LogoSource.UPLOAD, now=NOW)


def _raw_files() -> list[bytes]:
    return [path.read_bytes() for path in vault.managed_files()]


def test_turning_on_encrypts_what_is_on_disk_and_everything_still_reads(store: LocalStore) -> None:
    _seed(store)
    before = secrets.secret_status("feed-1").saved_at
    vault.enable(use_dpapi=True)

    raw = _raw_files()
    assert raw and all(body.startswith(vault.MAGIC) for body in raw)
    assert not any(b"Talked renewal terms" in body or b"sheet password" in body for body in raw)

    (note,) = Journal(store).for_account("acc-1").notes
    assert note.text == "Talked renewal terms"
    assert secrets.get_secret("feed-1") == "sheet password"
    assert secrets.secret_status("feed-1").saved_at == before
    found = logos.read_logo(store, "acc-1")
    assert found is not None and found[0] == png()


def test_new_saves_are_encrypted_and_a_renamed_secret_still_opens(store: LocalStore) -> None:
    vault.enable(use_dpapi=True)
    _seed(store)
    assert all(body.startswith(vault.MAGIC) for body in _raw_files())
    assert secrets.rename_secret("feed-1", "feed-2")
    assert secrets.get_secret("feed-2") == "sheet password"
    assert secrets.get_secret("feed-1") is None


def test_a_locked_book_raises_and_never_reads_as_empty(store: LocalStore) -> None:
    _seed(store)
    vault.enable(use_dpapi=True)
    vault.lock()
    with pytest.raises(Locked):
        Journal(store).for_account("acc-1")
    with pytest.raises(Locked):
        secrets.get_secret("feed-1")
    with pytest.raises(Locked):
        Journal(store).add_note("acc-1", TouchKind.NOTE, "x", TODAY, now=NOW, today=TODAY)
    vault.unlock()
    assert len(Journal(store).for_account("acc-1").notes) == 1


def test_a_damaged_journal_is_never_set_aside_or_overwritten(store: LocalStore) -> None:
    _seed(store)
    vault.enable(use_dpapi=True)
    path = data_dir() / "journal.json"
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(Damaged):
        Journal(store).add_note("acc-1", TouchKind.NOTE, "x", TODAY, now=NOW, today=TODAY)
    assert path.read_bytes() == bytes(raw)
    assert [p.name for p in data_dir().iterdir() if "unreadable" in p.name] == []


def test_the_web_app_sends_a_locked_book_to_the_unlock_page(store: LocalStore) -> None:
    secrets.save_openrouter_key("sk-or-v1-" + "a" * 64)
    vault.enable(use_dpapi=True)
    vault.lock()
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    response = client.get("/settings/privacy", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/unlock?next=%2Fsettings%2Fprivacy"


def test_a_damaged_key_file_answers_503_and_never_an_empty_page(store: LocalStore) -> None:
    vault.enable(use_dpapi=True)
    vault.forget_key()
    vault.keys_path().write_text("{not json", encoding="utf-8")
    backup = vault.keys_path().with_name(vault.keys_path().name + ".bak")
    backup.unlink(missing_ok=True)
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    response = client.get("/settings/privacy")
    assert response.status_code == 503
    assert "Wardmind can't open your data" in response.text


def test_turning_off_leaves_plain_files_that_read_the_same(store: LocalStore) -> None:
    _seed(store)
    vault.enable(use_dpapi=True)
    vault.disable()
    assert not any(body.startswith(vault.MAGIC) for body in _raw_files())
    assert Journal(store).for_account("acc-1").notes[0].text == "Talked renewal terms"
    assert secrets.get_secret("feed-1") == "sheet password"
