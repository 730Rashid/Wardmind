"""Settings > Data & privacy > Encryption: the section in each state, turning encryption on (the
recovery key is shown once), off, the password, a new recovery key and the lock time.

Runs through the real routes against the temp folders from conftest. Windows protection is a
reversible stand-in that is always available here, and the password's Argon2 cost is tiny (the
real Windows calls are covered by tests/test_vault.py, where they run on Windows only).
"""

from __future__ import annotations

import dataclasses
import html
import re
import time
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import vault
from wardmind.core.journal import Journal, TouchKind
from wardmind.core.settings import load_settings
from wardmind.core.store import LocalStore, data_dir
from wardmind.core.vault import Damaged, Locked, State, VaultError
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
PASSWORD = "lantern-orbit-7731"  # distinctive, so a copy into any page or header is easy to spot
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
GROUP = re.compile(r'<li class="recovery-group">([0-9A-Z]{5})</li>')
TOKEN = re.compile(r'name="pending" value="([0-9a-f]+)"')
CODE_SHAPE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}(-[0-9A-HJKMNP-TV-Z]{5}){4}$")
START_ACTION = 'action="/settings/privacy/encryption/start"'
ON_ACTION = 'action="/settings/privacy/encryption/on"'
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 11)
MAGIC = vault.MAGIC


@pytest.fixture(autouse=True)
def fake_windows_and_cheap_argon2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows protection is reversible here and always available; Argon2 costs are tiny."""
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)
    monkeypatch.setattr(vault, "dpapi_available", lambda: True)
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return LocalStore(data_dir())


# --- Helpers ---------------------------------------------------------------------------------


def _text(response: Any) -> str:
    """The page text with entities decoded, so "don't" matches the escaped apostrophe."""
    return html.unescape(response.text)


def _get(client: TestClient, path: str) -> Any:
    return client.get(path, follow_redirects=False)


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _start(client: TestClient, **fields: str) -> Any:
    return _post(client, "/settings/privacy/encryption/start", fields)


def _token(response: Any) -> str:
    match = TOKEN.search(response.text)
    assert match, "the recovery key page has no pending token"
    return match.group(1)


def _shown_code(response: Any) -> str:
    """The recovery key on a recovery key page: five groups, joined as the person sees them."""
    groups = GROUP.findall(response.text)
    assert len(groups) == 5, f"expected five groups, found {len(groups)}"
    return "-".join(groups)


def _add_note(store: LocalStore) -> None:
    Journal(store).add_note(
        "acc-1", TouchKind.CALL, "Talked renewal terms", TODAY, now=NOW, today=TODAY
    )


def _note_texts(store: LocalStore) -> list[str]:
    return [note.text for note in Journal(store).for_account("acc-1").notes]


def _turn_on_with_windows(client: TestClient) -> str:
    """Start with Windows, then turn on with the box ticked. Returns the recovery key shown."""
    shown = _start(client, method="windows")
    assert shown.status_code == 200
    code = _shown_code(shown)
    done = _post(
        client, "/settings/privacy/encryption/on", {"pending": _token(shown), "saved": "1"}
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/settings/privacy?notice=encryption-on"
    return code


def _turn_on_with_password(client: TestClient) -> str:
    """Start with a password, then turn on with the box ticked. Returns the recovery key shown."""
    shown = _start(client, method="password", password=PASSWORD, confirm=PASSWORD)
    assert shown.status_code == 200
    code = _shown_code(shown)
    done = _post(
        client, "/settings/privacy/encryption/on", {"pending": _token(shown), "saved": "1"}
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/settings/privacy?notice=encryption-on"
    return code


def _managed_raw() -> list[bytes]:
    return [path.read_bytes() for path in vault.managed_files()]


# --- The section in each state ---------------------------------------------------------------


def test_the_section_is_off_with_both_unlock_choices(client: TestClient) -> None:
    response = _get(client, "/settings/privacy")
    assert response.status_code == 200
    text = _text(response)
    assert "Encryption" in text
    assert "Encrypt your Wardmind data on this computer" in text
    assert '<p class="privacy-status">Off.</p>' in text
    assert "Unlock with my Windows sign-in" in text
    assert "Use a password" in text
    assert START_ACTION in text
    assert "Lock after" not in text


def test_without_windows_protection_only_the_password_choice_is_offered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "dpapi_available", lambda: False)
    text = _text(_get(client, "/settings/privacy"))
    assert "Unlock with my Windows sign-in" not in text
    assert 'value="password" aria-describedby="method-password-help" checked' in text


def test_on_with_windows_offers_a_new_password_but_not_its_removal(client: TestClient) -> None:
    _turn_on_with_windows(client)
    text = _text(_get(client, "/settings/privacy"))
    assert '<p class="privacy-status">On. Windows unlocks it when you sign in.</p>' in text
    assert ">Set a password</button>" in text
    assert ">Remove password</button>" not in text
    assert ">Make a new recovery key</button>" in text
    assert ">Turn off encryption</button>" in text
    assert "Lock after" in text


def test_on_with_a_password_offers_its_change_and_removal(client: TestClient) -> None:
    _turn_on_with_password(client)
    text = _text(_get(client, "/settings/privacy"))
    assert '<p class="privacy-status">On. It needs your password after it locks.</p>' in text
    assert ">Change password</button>" in text
    assert ">Remove password</button>" in text
    assert "Windows unlocks it when you sign in" not in text


def test_while_changing_only_a_status_line_shows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "state", lambda: State.ENCRYPTING)
    text = _text(_get(client, "/settings/privacy"))
    assert "Finishing a change to encryption." in text
    assert START_ACTION not in text
    assert "Lock after" not in text
    assert "Turn off encryption" not in text


def test_lock_after_offers_five_choices_and_never_only_when_on(client: TestClient) -> None:
    assert 'name="minutes"' not in _text(_get(client, "/settings/privacy"))
    _turn_on_with_windows(client)
    text = _get(client, "/settings/privacy").text
    for minutes, label in ((5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes")):
        assert f'<option value="{minutes}"' in text
        assert label in text
    assert '<option value="60"' in text
    assert '<option value="0"' in text
    assert ">Never</option>" in text
    assert '<option value="15" selected>' in text  # the default, 15 minutes


def test_the_section_has_no_inline_markup_in_any_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not INLINE.search(_get(client, "/settings/privacy").text)
    _turn_on_with_password(client)
    assert not INLINE.search(_get(client, "/settings/privacy").text)
    monkeypatch.setattr(vault, "state", lambda: State.DECRYPTING)
    assert not INLINE.search(_get(client, "/settings/privacy").text)


def test_privacy_notices_come_from_fixed_codes(client: TestClient) -> None:
    shown = _text(_get(client, "/settings/privacy?notice=encryption-off"))
    assert "Encryption is off. Your files are stored without it again." in shown
    unknown = _text(_get(client, "/settings/privacy?notice=bogus-code"))
    assert "bogus-code" not in unknown
    assert "Encryption is off." not in unknown


# --- Starting: the choice, the password and the recovery key ----------------------------------


def test_start_with_windows_shows_the_key_in_five_groups_and_writes_nothing(
    client: TestClient,
) -> None:
    response = _start(client, method="windows")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = _text(response)
    code = _shown_code(response)
    assert CODE_SHAPE.match(code)
    assert "This is your recovery key." in text
    assert "it's the only way to open your data" in text
    assert "Wardmind won't show it again." in text
    assert "data-print" in text and 'class="button js-only"' in text
    assert re.search(r'name="saved" value="1" required', response.text)
    assert "I've saved my recovery key somewhere safe" in text
    assert ON_ACTION in response.text
    assert "Cancel" in text
    assert not INLINE.search(response.text)
    # Nothing is encrypted and no key is kept on disk until the box is ticked.
    assert vault.state() is State.PLAIN
    assert not vault.keys_path().exists()


def test_start_with_a_password_never_shows_it_back(client: TestClient) -> None:
    response = _start(client, method="password", password=PASSWORD, confirm=PASSWORD)
    assert response.status_code == 200
    assert PASSWORD not in response.text
    assert PASSWORD not in _text(response)
    assert "Turn on encryption" in response.text
    assert not vault.keys_path().exists()


def test_start_refuses_a_short_password_next_to_its_field(client: TestClient) -> None:
    response = _start(client, method="password", password="abc1", confirm="abc1")
    assert response.status_code == 422
    text = _text(response)
    assert "Use at least 10 characters" in text
    assert 'id="field-enc-password-error"' in response.text
    assert "abc1" not in response.text
    assert vault.state() is State.PLAIN


def test_start_refuses_a_mismatched_confirmation(client: TestClient) -> None:
    response = _start(client, method="password", password=PASSWORD, confirm=PASSWORD + "x")
    assert response.status_code == 422
    assert "The two passwords don't match" in _text(response)
    assert 'id="field-enc-confirm-error"' in response.text
    assert PASSWORD not in response.text
    assert not vault.keys_path().exists()


def test_start_refuses_a_missing_choice(client: TestClient) -> None:
    response = _start(client)
    assert response.status_code == 422
    assert "Choose how Wardmind unlocks" in _text(response)


def test_start_refuses_windows_when_windows_protection_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "dpapi_available", lambda: False)
    response = _start(client, method="windows")
    assert response.status_code == 422
    assert "Windows can't unlock Wardmind on this computer" in _text(response)


def test_start_when_already_on_goes_back_to_the_page(client: TestClient) -> None:
    _turn_on_with_windows(client)
    response = _start(client, method="windows")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy"


# --- Turning on -------------------------------------------------------------------------------


def test_on_without_the_box_refuses_and_keeps_the_same_key(client: TestClient) -> None:
    shown = _start(client, method="windows")
    code = _shown_code(shown)
    token = _token(shown)
    response = _post(client, "/settings/privacy/encryption/on", {"pending": token})
    assert response.status_code == 422
    text = _text(response)
    assert "Tick the box to say you've saved your recovery key." in text
    assert _shown_code(response) == code
    assert _token(response) == token
    assert vault.state() is State.PLAIN


def test_on_with_an_unknown_token_goes_back_with_a_notice(client: TestClient) -> None:
    response = _post(client, "/settings/privacy/encryption/on", {"pending": "0" * 48, "saved": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?notice=encryption-expired"
    assert vault.state() is State.PLAIN


def test_on_with_an_expired_token_goes_back_with_a_notice(client: TestClient) -> None:
    shown = _start(client, method="windows")
    token = _token(shown)
    pending = client.app.state.encryption_pending
    pending[token] = dataclasses.replace(pending[token], created=time.monotonic() - 11 * 60)
    response = _post(client, "/settings/privacy/encryption/on", {"pending": token, "saved": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?notice=encryption-expired"
    assert token not in pending
    assert vault.state() is State.PLAIN


def test_a_pending_key_works_once(client: TestClient) -> None:
    shown = _start(client, method="windows")
    token = _token(shown)
    first = _post(client, "/settings/privacy/encryption/on", {"pending": token, "saved": "1"})
    assert first.headers["location"] == "/settings/privacy?notice=encryption-on"
    again = _post(client, "/settings/privacy/encryption/on", {"pending": token, "saved": "1"})
    assert again.headers["location"] == "/settings/privacy?notice=encryption-expired"


def test_turning_on_encrypts_every_file_and_keeps_the_data(
    client: TestClient, store: LocalStore
) -> None:
    _add_note(store)
    _turn_on_with_windows(client)
    assert vault.state() is State.ON
    raw = _managed_raw()
    assert raw and all(body.startswith(MAGIC) for body in raw)
    assert not any(b"Talked renewal terms" in body for body in raw)
    assert _note_texts(store) == ["Talked renewal terms"]
    notice = _text(_get(client, "/settings/privacy?notice=encryption-on"))
    assert "Encryption is on. Your recovery key is the way back in" in notice


def test_turning_on_with_a_password_needs_it_after_a_lock(client: TestClient) -> None:
    _turn_on_with_password(client)
    assert vault.has_password()
    assert not vault.has_windows_unlock()
    vault.lock()
    with pytest.raises(Locked):
        vault.unlock(password="a-wrong-password")
    vault.unlock(password=PASSWORD)
    assert vault.is_unlocked()


def test_the_recovery_key_shown_opens_the_book_after_a_lock(
    client: TestClient, store: LocalStore
) -> None:
    _add_note(store)
    code = _turn_on_with_windows(client)
    vault.lock()
    vault.unlock(recovery=code)
    assert _note_texts(store) == ["Talked renewal terms"]


def test_a_failed_turn_on_shows_a_fixed_notice_and_no_exception_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**kwargs: Any) -> str:
        raise VaultError("/secret/folder/keys.json is in the way")

    monkeypatch.setattr(vault, "enable", fail)
    shown = _start(client, method="windows")
    token = _token(shown)
    done = _post(client, "/settings/privacy/encryption/on", {"pending": token, "saved": "1"})
    assert done.headers["location"] == "/settings/privacy?notice=encryption-failed"
    page = _text(_get(client, "/settings/privacy?notice=encryption-failed"))
    assert "Wardmind couldn't change encryption." in page
    assert "/secret/folder" not in page
    # The pending key was taken even though the change failed.
    again = _post(client, "/settings/privacy/encryption/on", {"pending": token, "saved": "1"})
    assert again.headers["location"] == "/settings/privacy?notice=encryption-expired"


# --- Turning off ------------------------------------------------------------------------------


def test_turning_off_needs_the_box(client: TestClient) -> None:
    _turn_on_with_windows(client)
    response = _post(client, "/settings/privacy/encryption/off")
    assert response.status_code == 422
    assert "Tick the box to confirm you want to turn encryption off." in _text(response)
    assert vault.state() is State.ON


def test_turning_off_restores_plain_files_that_read_the_same(
    client: TestClient, store: LocalStore
) -> None:
    _add_note(store)
    _turn_on_with_windows(client)
    response = _post(client, "/settings/privacy/encryption/off", {"confirm_off": "1"})
    assert response.headers["location"] == "/settings/privacy?notice=encryption-off"
    assert vault.state() is State.PLAIN
    assert not any(body.startswith(MAGIC) for body in _managed_raw())
    assert _note_texts(store) == ["Talked renewal terms"]


def test_turning_off_while_locked_changes_nothing(client: TestClient, store: LocalStore) -> None:
    _add_note(store)
    _turn_on_with_windows(client)
    vault.lock()
    response = _post(client, "/settings/privacy/encryption/off", {"confirm_off": "1"})
    # The lock gate answers writes to a locked book before any route runs.
    assert response.status_code == 423
    assert vault.state() is State.ON
    assert all(body.startswith(MAGIC) for body in _managed_raw())


def test_a_locked_answer_from_the_vault_becomes_the_locked_notice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on_with_windows(client)

    def locked(**kwargs: Any) -> None:
        raise Locked("Wardmind is locked.")

    monkeypatch.setattr(vault, "disable", locked)
    response = _post(client, "/settings/privacy/encryption/off", {"confirm_off": "1"})
    assert response.headers["location"] == "/settings/privacy?notice=encryption-locked"
    assert vault.state() is State.ON


# --- Password ---------------------------------------------------------------------------------


def test_setting_a_password_on_a_windows_book(client: TestClient) -> None:
    _turn_on_with_windows(client)
    response = _post(
        client,
        "/settings/privacy/encryption/password",
        {"password": PASSWORD, "confirm": PASSWORD},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?notice=password-set"
    assert PASSWORD not in response.headers["location"]
    assert vault.has_password()
    assert not vault.has_windows_unlock()
    vault.lock()
    vault.unlock(password=PASSWORD)
    assert vault.is_unlocked()


def test_password_errors_are_shown_next_to_their_fields(client: TestClient) -> None:
    _turn_on_with_windows(client)
    short = _post(
        client, "/settings/privacy/encryption/password", {"password": "abc1", "confirm": "abc1"}
    )
    assert short.status_code == 422
    assert "Use at least 10 characters" in _text(short)
    assert 'id="field-enc-password-error"' in short.text
    assert "abc1" not in short.text
    mismatch = _post(
        client,
        "/settings/privacy/encryption/password",
        {"password": PASSWORD, "confirm": PASSWORD + "x"},
    )
    assert mismatch.status_code == 422
    assert "The two passwords don't match" in _text(mismatch)
    assert 'id="field-enc-confirm-error"' in mismatch.text
    assert PASSWORD not in mismatch.text
    assert not vault.has_password()


def test_removing_the_password_goes_back_to_windows(client: TestClient) -> None:
    _turn_on_with_password(client)
    refused = _post(client, "/settings/privacy/encryption/password/remove")
    assert refused.status_code == 422
    assert "Tick the box to confirm you want to remove the password." in _text(refused)
    assert vault.has_password()
    done = _post(client, "/settings/privacy/encryption/password/remove", {"confirm_remove": "1"})
    assert done.headers["location"] == "/settings/privacy?notice=password-removed"
    assert not vault.has_password()
    assert vault.has_windows_unlock()


def test_the_remove_button_needs_a_password_and_windows_protection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _turn_on_with_password(client)
    monkeypatch.setattr(vault, "dpapi_available", lambda: False)
    text = _text(_get(client, "/settings/privacy"))
    assert ">Remove password</button>" not in text
    assert ">Change password</button>" in text


def test_password_changes_need_encryption_on(client: TestClient) -> None:
    response = _post(
        client,
        "/settings/privacy/encryption/password",
        {"password": PASSWORD, "confirm": PASSWORD},
    )
    assert response.headers["location"] == "/settings/privacy?notice=encryption-not-on"
    assert vault.state() is State.PLAIN
    assert not vault.keys_path().exists()


# --- A new recovery key -----------------------------------------------------------------------


def test_a_new_recovery_key_needs_the_box(client: TestClient) -> None:
    _turn_on_with_windows(client)
    response = _post(client, "/settings/privacy/encryption/recovery")
    assert response.status_code == 422
    assert "Tick the box to confirm the old recovery key will stop working." in _text(response)


def test_a_new_recovery_key_replaces_the_old_one(client: TestClient) -> None:
    old = _turn_on_with_windows(client)
    response = _post(client, "/settings/privacy/encryption/recovery", {"confirm_recovery": "1"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = _text(response)
    new = _shown_code(response)
    assert new != old
    assert "Your old recovery key no longer works." in text
    assert "Back to Data & privacy" in text
    assert ON_ACTION not in response.text
    assert 'name="saved"' not in response.text
    assert "Print or save as PDF" in text
    vault.lock()
    with pytest.raises(Locked):
        vault.unlock(recovery=old)
    vault.unlock(recovery=new)
    assert vault.is_unlocked()


# --- Replacing all keys -----------------------------------------------------------------------


def test_replacing_keys_needs_the_box(client: TestClient) -> None:
    _turn_on_with_windows(client)
    response = _post(client, "/settings/privacy/encryption/rekey")
    assert response.status_code == 422
    assert "Tick the box to confirm your old recovery key will stop working." in _text(response)


def test_replacing_keys_with_windows_shows_a_new_recovery_key(client: TestClient) -> None:
    old = _turn_on_with_windows(client)
    store = LocalStore()
    _add_note(store)
    before = _managed_raw()
    response = _post(client, "/settings/privacy/encryption/rekey", {"confirm_rekey": "1"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Every key has been replaced" in _text(response)
    new = _shown_code(response)
    assert new != old
    assert _managed_raw() != before
    assert _note_texts(store) == ["Talked renewal terms"]
    vault.lock()
    with pytest.raises(Locked):
        vault.unlock(recovery=old)
    vault.unlock(recovery=new)


def test_replacing_keys_with_a_password_asks_for_it(client: TestClient) -> None:
    _turn_on_with_password(client)
    page = _text(_get(client, "/settings/privacy"))
    assert "Replace all keys" in page
    missing = _post(client, "/settings/privacy/encryption/rekey", {"confirm_rekey": "1"})
    assert missing.status_code == 422
    mismatch = _post(
        client,
        "/settings/privacy/encryption/rekey",
        {"confirm_rekey": "1", "password": PASSWORD, "confirm": PASSWORD + "x"},
    )
    assert mismatch.status_code == 422
    assert PASSWORD not in mismatch.text
    done = _post(
        client,
        "/settings/privacy/encryption/rekey",
        {"confirm_rekey": "1", "password": PASSWORD, "confirm": PASSWORD},
    )
    assert done.status_code == 200
    assert PASSWORD not in done.text
    vault.lock()
    vault.unlock(password=PASSWORD)
    assert vault.is_unlocked()


def test_replacing_keys_needs_encryption_on(client: TestClient) -> None:
    response = _post(client, "/settings/privacy/encryption/rekey", {"confirm_rekey": "1"})
    assert response.headers["location"] == "/settings/privacy?notice=encryption-not-on"


def test_an_interrupted_turn_on_finishes_on_the_next_page(client: TestClient) -> None:
    _turn_on_with_windows(client)
    keys = vault.load_keys()
    assert keys is not None
    vault.disable()
    store = LocalStore()
    _add_note(store)  # a plain file, written while "turning on" never finished
    vault.enable(use_dpapi=True)
    keys = vault.load_keys()
    assert keys is not None
    vault._save_keys(keys.replace(state=State.ENCRYPTING))
    assert _get(client, "/settings/privacy").status_code == 200
    assert vault.state() is State.ON
    assert all(vault.is_encrypted(raw) for raw in _managed_raw())


# --- Lock time --------------------------------------------------------------------------------


def test_auto_lock_saves_a_choice(client: TestClient) -> None:
    response = _post(client, "/settings/privacy/auto-lock", {"minutes": "30"})
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?notice=auto-lock-saved"
    assert load_settings().auto_lock_minutes == 30
    _post(client, "/settings/privacy/auto-lock", {"minutes": "0"})
    assert load_settings().auto_lock_minutes == 0


def test_auto_lock_refuses_anything_else_and_saves_nothing(client: TestClient) -> None:
    # The Lock after form only shows while encryption is on, so the refusal is checked there.
    _turn_on_with_windows(client)
    _post(client, "/settings/privacy/auto-lock", {"minutes": "30"})
    for value in ("7", "abc", ""):
        response = _post(client, "/settings/privacy/auto-lock", {"minutes": value})
        assert response.status_code == 422
        assert "Pick 5, 15, 30 or 60 minutes, or never." in _text(response)
    assert load_settings().auto_lock_minutes == 30


# --- Writes from another site -----------------------------------------------------------------


def test_a_post_from_another_site_is_refused(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/encryption/start",
        data={"method": "windows"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert vault.state() is State.PLAIN
