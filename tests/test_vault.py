"""The vault (core/vault.py): plain by default, encrypted at rest once turned on."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from wardmind.core import vault
from wardmind.core.secrets import secrets_dir
from wardmind.core.store import data_dir
from wardmind.core.vault import Damaged, Locked, State, VaultError

# The real Windows functions, kept before the autouse fixture replaces them.
REAL_PROTECT = vault.dpapi_protect
REAL_UNPROTECT = vault.dpapi_unprotect
CODE_SHAPE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}(-[0-9A-HJKMNP-TV-Z]{5}){4}$")


@pytest.fixture(autouse=True)
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Windows protection replaced by a reversible stand-in, so the tests run anywhere."""
    calls: list[str] = []

    def protect(data: bytes) -> bytes:
        calls.append("protect")
        return b"DP" + bytes(reversed(data))

    def unprotect(blob: bytes) -> bytes:
        calls.append("unprotect")
        if not blob.startswith(b"DP"):
            raise Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_protect", protect)
    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)
    return calls


def _data(name: str = "journal.json") -> Path:
    return data_dir() / name


def _seed() -> dict[Path, bytes]:
    files = {
        _data("journal.json"): b'{"by_account": {}}',
        _data("history/2026-11-10.json"): b'{"day": "2026-11-10"}',
        _data("logos/acc-1.png"): bytes(range(256)),
        secrets_dir() / "hubspot-token": b"pat-na1-test",
        secrets_dir() / "feeds" / "feed-1": b"feed-secret",
    }
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return files


def _unmanaged() -> dict[Path, bytes]:
    files = {
        secrets_dir() / "session.json": b'{"url": "x"}',
        secrets_dir() / "venv" / "pyvenv.cfg": b"home = x",
        _data(".journal.json-abc.tmp"): b"half written",
    }
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return files


# --- Plain (the default) -----------------------------------------------------------------


def test_plain_is_the_default_and_files_pass_through() -> None:
    assert vault.state() is State.PLAIN
    path = _data()
    vault.write_text(path, "héllo")
    assert path.read_bytes() == "héllo".encode()
    assert vault.read_text(path) == "héllo"
    assert not vault.keys_path().exists()


def test_a_missing_file_raises_file_not_found_as_before() -> None:
    with pytest.raises(FileNotFoundError):
        vault.read_text(_data("missing.json"))


def test_vault_errors_are_not_os_or_value_errors() -> None:
    for error in (VaultError, Locked, Damaged):
        assert not issubclass(error, OSError)
        assert not issubclass(error, ValueError)


def test_writes_are_atomic_and_leave_no_temp_file() -> None:
    path = _data("contacts.json")
    vault.write_text(path, "one")
    vault.write_text(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert [p.name for p in path.parent.iterdir() if p.suffix == ".tmp"] == []


# --- Logical paths and managed files --------------------------------------------------------


def test_logical_paths_are_root_tagged_and_posix() -> None:
    assert vault.logical_path(_data("history/2026-11-10.json")) == "data/history/2026-11-10.json"
    assert vault.logical_path(secrets_dir() / "feeds" / "feed-1") == "secrets/feeds/feed-1"


def test_a_file_outside_both_folders_has_no_logical_path(tmp_path: Path) -> None:
    with pytest.raises(VaultError):
        vault.logical_path(tmp_path / "elsewhere.json")


def test_managed_files_are_the_data_folder_and_named_secrets_only() -> None:
    files = _seed()
    unmanaged = _unmanaged()
    managed = set(vault.managed_files())
    assert managed == set(files)
    assert not managed & set(unmanaged)


# --- Turning encryption on -----------------------------------------------------------------


def test_enable_encrypts_every_managed_file_and_returns_a_recovery_code() -> None:
    files = _seed()
    unmanaged = _unmanaged()
    code = vault.enable(use_dpapi=True)
    assert CODE_SHAPE.match(code)
    assert vault.state() is State.ON
    for path, body in files.items():
        raw = path.read_bytes()
        assert raw.startswith(vault.MAGIC)
        assert body not in raw
        assert vault.read_bytes(path) == body
    for path, body in unmanaged.items():
        assert path.read_bytes() == body
    keys = json.loads(vault.keys_path().read_text(encoding="utf-8"))
    assert keys["state"] == "on"
    assert {slot["kind"] for slot in keys["slots"]} == {"dpapi", "recovery"}
    assert code.replace("-", "") not in vault.keys_path().read_text(encoding="utf-8")


def test_enable_keeps_each_files_modified_time() -> None:
    files = _seed()
    stamp = 1_700_000_000
    for path in files:
        os.utime(path, (stamp, stamp))
    vault.enable(use_dpapi=True)
    for path in files:
        assert int(path.stat().st_mtime) == stamp


def test_enable_twice_is_refused() -> None:
    vault.enable(use_dpapi=True)
    with pytest.raises(VaultError):
        vault.enable(use_dpapi=True)


def test_enable_without_windows_protection_needs_a_password_for_now() -> None:
    with pytest.raises(VaultError):
        vault.enable(use_dpapi=False)
    assert vault.state() is State.PLAIN
    assert not vault.keys_path().exists()


def test_new_writes_are_encrypted_once_on() -> None:
    vault.enable(use_dpapi=True)
    path = _data("client_updates.json")
    vault.write_text(path, '{"x": 1}')
    assert path.read_bytes().startswith(vault.MAGIC)
    assert vault.read_text(path) == '{"x": 1}'


def test_each_write_uses_a_fresh_nonce() -> None:
    vault.enable(use_dpapi=True)
    path = _data()
    vault.write_text(path, "same")
    first = path.read_bytes()
    vault.write_text(path, "same")
    assert path.read_bytes() != first


def test_a_plain_file_left_over_still_reads_while_on() -> None:
    vault.enable(use_dpapi=True)
    path = _data("late.json")
    path.write_bytes(b"plain")
    assert vault.read_bytes(path) == b"plain"


# --- Damage ------------------------------------------------------------------------------------


def test_a_changed_byte_is_damaged_never_empty() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    path = next(iter(files))
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(Damaged):
        vault.read_bytes(path)


def test_a_file_swapped_for_another_is_damaged() -> None:
    _seed()
    vault.enable(use_dpapi=True)
    journal = _data("journal.json")
    history = _data("history/2026-11-10.json")
    journal.write_bytes(history.read_bytes())
    with pytest.raises(Damaged):
        vault.read_bytes(journal)


def test_a_truncated_file_is_damaged() -> None:
    _seed()
    vault.enable(use_dpapi=True)
    path = _data()
    path.write_bytes(path.read_bytes()[: vault.HEADER_BYTES + 3])
    with pytest.raises(Damaged):
        vault.read_bytes(path)


def test_a_damaged_key_file_is_damaged() -> None:
    vault.enable(use_dpapi=True)
    vault.keys_path().write_text("{not json", encoding="utf-8")
    vault.forget_key()
    with pytest.raises(Damaged):
        vault.state()


# --- Locking and unlocking -----------------------------------------------------------------------


def test_after_lock_reads_and_writes_are_locked() -> None:
    _seed()
    vault.enable(use_dpapi=True)
    vault.lock()
    assert not vault.is_unlocked()
    with pytest.raises(Locked):
        vault.read_bytes(_data())
    with pytest.raises(Locked):
        vault.write_text(_data(), "x")


def test_windows_protection_unlocks_again_on_request(fake_dpapi: list[str]) -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    vault.lock()
    vault.unlock()
    assert vault.is_unlocked()
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]


def test_a_new_process_unlocks_through_windows_protection_by_itself() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    vault.forget_key()  # as if the MCP server started after the web app turned it on
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]
    assert vault.is_unlocked()


def test_the_recovery_code_unlocks_and_is_forgiving_about_its_shape() -> None:
    files = _seed()
    code = vault.enable(use_dpapi=True)
    vault.lock()
    messy = " " + code.lower().replace("-", " ").replace("0", "o") + " "
    vault.unlock(recovery=messy)
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]


def test_a_wrong_recovery_code_is_refused() -> None:
    vault.enable(use_dpapi=True)
    vault.lock()
    with pytest.raises(Locked, match="doesn't open this book"):
        vault.unlock(recovery=vault.new_recovery_code())
    with pytest.raises(Locked, match="25 letters and digits"):
        vault.unlock(recovery="too short")
    assert not vault.is_unlocked()


def test_another_accounts_windows_protection_cant_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed()
    vault.enable(use_dpapi=True)
    vault.forget_key()

    def refuse(blob: bytes) -> bytes:
        raise Damaged("Windows couldn't unlock Wardmind's key for this account.")

    monkeypatch.setattr(vault, "dpapi_unprotect", refuse)
    with pytest.raises(Damaged):
        vault.read_bytes(_data())


def test_the_key_file_holds_no_key_in_the_clear() -> None:
    vault.enable(use_dpapi=True)
    text = vault.keys_path().read_text(encoding="utf-8")
    session = vault._session
    assert session is not None
    assert session.book_keys["default"].hex() not in text
    assert vault.KEYS_BACKUP in {p.name for p in vault.keys_path().parent.iterdir()}


# --- Moving a file ----------------------------------------------------------------------------


def test_move_reencrypts_under_the_new_name() -> None:
    vault.enable(use_dpapi=True)
    old = secrets_dir() / "feeds" / "draft-1"
    new = secrets_dir() / "feeds" / "feed-7"
    vault.write_text(old, "key")
    vault.move(old, new)
    assert not old.exists()
    assert new.read_bytes().startswith(vault.MAGIC)
    assert vault.read_text(new) == "key"


def test_move_of_a_plain_file_is_a_rename() -> None:
    old = secrets_dir() / "feeds" / "draft-1"
    new = secrets_dir() / "feeds" / "feed-7"
    vault.write_text(old, "key")
    vault.move(old, new)
    assert new.read_text(encoding="utf-8") == "key"
    assert not old.exists()


# --- Turning encryption off, and interrupted changes ---------------------------------------


def test_disable_decrypts_everything_and_removes_the_keys() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    vault.disable()
    assert vault.state() is State.PLAIN
    assert not vault.keys_path().exists()
    for path, body in files.items():
        assert path.read_bytes() == body


def test_disable_needs_the_book_unlocked() -> None:
    _seed()
    vault.enable(use_dpapi=True)
    vault.lock()
    with pytest.raises(Locked):
        vault.disable()
    assert vault.state() is State.ON


def test_an_interrupted_turn_on_finishes_on_resume() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    # As if the process stopped part-way: one file back to plain, state still "encrypting".
    path = next(iter(files))
    path.write_bytes(files[path])
    keys = json.loads(vault.keys_path().read_text(encoding="utf-8"))
    keys["state"] = "encrypting"
    vault.keys_path().write_text(json.dumps(keys), encoding="utf-8")
    vault.resume()
    assert vault.state() is State.ON
    assert path.read_bytes().startswith(vault.MAGIC)
    assert vault.read_bytes(path) == files[path]


def test_an_interrupted_turn_off_finishes_on_resume() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    keys = json.loads(vault.keys_path().read_text(encoding="utf-8"))
    keys["state"] = "decrypting"
    vault.keys_path().write_text(json.dumps(keys), encoding="utf-8")
    vault.resume()
    assert vault.state() is State.PLAIN
    for path, body in files.items():
        assert path.read_bytes() == body


def test_resume_does_nothing_when_plain_or_on() -> None:
    vault.resume()
    assert vault.state() is State.PLAIN
    vault.enable(use_dpapi=True)
    vault.resume()
    assert vault.state() is State.ON


# --- Real Windows protection ---------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows protection (DPAPI) is Windows only")
def test_real_windows_protection_round_trips() -> None:
    secret = os.urandom(32)
    blob = REAL_PROTECT(secret)
    assert secret not in blob
    assert REAL_UNPROTECT(blob) == secret
    with pytest.raises(Damaged):
        REAL_UNPROTECT(blob[:-4] + b"\x00\x00\x00\x00")


# --- Passwords, the unlock ticket and changing slots (phase 1b) ----------------------------

PASSWORD = "correct horse battery"


@pytest.fixture
def cheap_argon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})


def _slots() -> set[str]:
    return {slot["kind"] for slot in json.loads(vault.keys_path().read_text("utf-8"))["slots"]}


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "at least 10 characters"),
        ("x" * 1025, "under 1,024 characters"),
        ("line one\nline two", "line breaks"),
    ],
)
def test_a_password_that_cant_be_used_is_refused(password: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        vault.check_password(password)


def test_a_passphrase_with_spaces_is_fine() -> None:
    assert vault.check_password(PASSWORD) == PASSWORD


@pytest.mark.usefixtures("cheap_argon")
def test_enable_with_a_password_has_no_windows_slot_and_unlocks_with_it() -> None:
    files = _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    assert _slots() == {"password", "recovery"}
    assert vault.has_password() and not vault.has_windows_unlock()
    vault.lock()
    with pytest.raises(Locked, match="isn't right"):
        vault.unlock(password="wrong password here")
    vault.unlock(password=PASSWORD)
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]


@pytest.mark.usefixtures("cheap_argon")
def test_with_a_password_a_locked_book_stays_locked() -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    vault.lock()
    assert not vault.ensure_unlocked()
    with pytest.raises(Locked):
        vault.unlock()  # no Windows slot to fall back on


@pytest.fixture
def ticket_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """This test process plays the MCP server, which may read the app's ticket."""
    monkeypatch.setattr(vault, "_ticket_reader", True)
    monkeypatch.setattr(vault, "_ticket_checked", 0.0)


def _need_ticket() -> Path:
    ticket = secrets_dir() / vault.TICKET_FILE
    if not ticket.exists():
        pytest.skip("the ticket needs Windows protection")
    return ticket


@pytest.mark.usefixtures("cheap_argon", "ticket_reader")
def test_the_ticket_lets_another_process_in_until_the_app_locks() -> None:
    files = _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    assert (secrets_dir() / vault.TICKET_FILE).exists() == vault.dpapi_available()
    _need_ticket()
    vault.forget_key()  # another process: the MCP server
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]
    vault.delete_ticket()  # the app locked
    with pytest.raises(Locked):
        vault.read_bytes(path)


@pytest.mark.usefixtures("cheap_argon")
def test_the_web_app_never_opens_the_book_from_a_ticket() -> None:
    """A restarted app (or a second one) must ask for the password, even with a ticket there."""
    files = _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    _need_ticket()
    vault.forget_key()
    assert not vault.ensure_unlocked()
    with pytest.raises(Locked):
        vault.read_bytes(next(iter(files)))


@pytest.mark.usefixtures("cheap_argon", "ticket_reader")
def test_an_expired_ticket_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    _need_ticket()
    vault.forget_key()
    later = vault.time.time() + vault.TICKET_MINUTES * 60 + 1
    monkeypatch.setattr(vault.time, "time", lambda: later)
    assert not vault.ensure_unlocked()


@pytest.mark.usefixtures("cheap_argon", "ticket_reader")
def test_editing_the_ticket_file_cant_extend_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The expiry is inside the protected blob; a field added outside it changes nothing, and an
    old-format ticket with an outside expiry is refused."""
    vault.enable(password=PASSWORD, use_dpapi=True)
    ticket = _need_ticket()
    body = json.loads(ticket.read_text("utf-8"))
    body["until"] = 10**12
    ticket.write_text(json.dumps(body), encoding="utf-8")
    vault.forget_key()
    real_time = vault.time.time
    later = real_time() + vault.TICKET_MINUTES * 60 + 1
    monkeypatch.setattr(vault.time, "time", lambda: later)
    assert not vault.ensure_unlocked()
    monkeypatch.setattr(vault.time, "time", real_time)
    old_format = {"blob": body["blob"], "until": 10**12}
    ticket.write_text(json.dumps(old_format), encoding="utf-8")
    assert not vault.ensure_unlocked()


@pytest.mark.usefixtures("cheap_argon", "ticket_reader")
def test_a_ticket_from_another_key_file_epoch_is_refused() -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    _need_ticket()
    keys = vault.load_keys()
    assert keys is not None
    vault._save_keys(keys.replace(epoch=keys.epoch + 1))
    vault.forget_key()
    assert not vault.ensure_unlocked()


@pytest.mark.usefixtures("cheap_argon", "ticket_reader")
def test_a_ticket_session_ends_when_the_ticket_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    _need_ticket()
    vault.forget_key()
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]
    later = vault.time.time() + vault.TICKET_MINUTES * 60 + 1
    monkeypatch.setattr(vault.time, "time", lambda: later)
    with pytest.raises(Locked):
        vault.read_bytes(path)


@pytest.mark.usefixtures("cheap_argon")
def test_the_app_refreshes_its_ticket_while_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    ticket = _need_ticket()
    first = ticket.read_bytes()
    vault.refresh_ticket()  # plenty of time left: not rewritten
    assert ticket.read_bytes() == first
    later = vault.time.time() + vault.TICKET_MINUTES * 60 * 0.75
    monkeypatch.setattr(vault.time, "time", lambda: later)
    vault.refresh_ticket()
    assert ticket.read_bytes() != first
    vault.lock()
    assert not ticket.exists()
    vault.refresh_ticket()  # locked: nothing written
    assert not ticket.exists()


@pytest.mark.usefixtures("cheap_argon")
def test_setting_a_password_removes_windows_unlock() -> None:
    vault.enable(use_dpapi=True)
    vault.set_password(PASSWORD)
    assert _slots() == {"password", "recovery"}
    vault.lock()
    with pytest.raises(Locked):
        vault.unlock()
    vault.unlock(password=PASSWORD)
    assert vault.is_unlocked()


@pytest.mark.usefixtures("cheap_argon")
def test_removing_the_password_brings_windows_unlock_back() -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    if not vault.dpapi_available():
        pytest.skip("needs Windows protection")
    vault.remove_password()
    assert _slots() == {"dpapi", "recovery"}
    assert not (secrets_dir() / vault.TICKET_FILE).exists()
    vault.lock()
    vault.unlock()
    assert vault.is_unlocked()


def test_slot_changes_need_the_book_unlocked() -> None:
    vault.enable(use_dpapi=True)
    vault.lock()
    with pytest.raises(Locked):
        vault.set_password(PASSWORD)
    with pytest.raises(Locked):
        vault.replace_recovery_code()


def test_a_new_recovery_key_replaces_the_old_one() -> None:
    old = vault.enable(use_dpapi=True)
    new = vault.replace_recovery_code()
    assert new != old
    vault.lock()
    with pytest.raises(Locked):
        vault.unlock(recovery=old)
    vault.unlock(recovery=new)
    assert vault.is_unlocked()


def test_enable_uses_the_recovery_code_already_shown() -> None:
    shown = vault.new_recovery_code()
    assert vault.enable(code=shown.lower(), use_dpapi=True) == shown
    vault.lock()
    vault.unlock(recovery=shown)
    assert vault.is_unlocked()


def test_ensure_unlocked_is_true_for_plain_and_windows_books() -> None:
    assert vault.ensure_unlocked()
    vault.enable(use_dpapi=True)
    vault.forget_key()
    assert vault.ensure_unlocked()


@pytest.mark.usefixtures("cheap_argon")
def test_turning_off_deletes_the_ticket() -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    vault.disable()
    assert not (secrets_dir() / vault.TICKET_FILE).exists()
    assert vault.state() is State.PLAIN


# --- Replacing every key, and a key file that was tampered with ------------------------------


def _epochs(paths: list[Path]) -> set[int]:
    return {int.from_bytes(path.read_bytes()[6:10], "big") for path in paths}


def _keys_json() -> dict:
    return json.loads(vault.keys_path().read_text("utf-8"))


def _write_keys_json(body: dict) -> None:
    text = json.dumps(body)
    vault.keys_path().write_text(text, encoding="utf-8")
    (secrets_dir() / vault.KEYS_BACKUP).write_text(text, encoding="utf-8")


@pytest.mark.usefixtures("cheap_argon")
def test_replacing_all_keys_reencrypts_and_old_secrets_open_nothing() -> None:
    files = _seed()
    old_code = vault.enable(password=PASSWORD, use_dpapi=True)
    old_copy = vault.keys_path().read_text("utf-8")
    new_code = vault.replace_all_keys(password="a brand new passphrase")
    assert new_code != old_code and CODE_SHAPE.match(new_code)
    assert _epochs(list(files)) == {2}
    assert vault.state() is State.ON and "previous" not in _keys_json()
    for path, body in files.items():
        assert vault.read_bytes(path) == body
    vault.lock()
    with pytest.raises(Locked):
        vault.unlock(password=PASSWORD)
    with pytest.raises(Locked):
        vault.unlock(recovery=old_code)
    # An old copy of keys.json with the old password opens the old master key, which no file
    # uses any more.
    vault.keys_path().write_text(old_copy, encoding="utf-8")
    vault.unlock(password=PASSWORD)
    with pytest.raises((Locked, Damaged)):
        vault.read_bytes(next(iter(files)))
    vault.forget_key()


@pytest.mark.usefixtures("cheap_argon")
def test_replacing_keys_needs_the_password_when_there_is_one() -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    with pytest.raises(VaultError, match="Type a password"):
        vault.replace_all_keys()


def test_replacing_keys_keeps_windows_protection_without_a_password() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    vault.replace_all_keys()
    vault.forget_key()
    assert vault.ensure_unlocked()
    for path, body in files.items():
        assert vault.read_bytes(path) == body


def test_an_interrupted_key_replacement_finishes_on_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    real_rewrite = vault._rewrite
    done: list[Path] = []

    def crash_after_two(path: Path, change) -> None:  # type: ignore[no-untyped-def]
        if len(done) == 2:
            raise KeyboardInterrupt
        real_rewrite(path, change)
        done.append(path)

    monkeypatch.setattr(vault, "_rewrite", crash_after_two)
    with pytest.raises(KeyboardInterrupt):
        vault.replace_all_keys()
    monkeypatch.setattr(vault, "_rewrite", real_rewrite)
    assert vault.state() is State.REKEYING
    assert _epochs(list(files)) == {1, 2}
    vault.forget_key()  # a new run of the app
    for path, body in files.items():  # readable in between, with old and new keys
        assert vault.read_bytes(path) == body
    assert vault.try_resume()
    assert vault.state() is State.ON and _epochs(list(files)) == {2}
    for path, body in files.items():
        assert vault.read_bytes(path) == body


def test_a_key_file_saying_plain_is_damaged() -> None:
    vault.enable(use_dpapi=True)
    body = _keys_json()
    body["state"] = "plain"
    _write_keys_json(body)
    with pytest.raises(Damaged):
        vault.state()


def test_a_forged_turn_off_is_refused_before_anything_is_decrypted() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    body = _keys_json()
    body["state"] = "decrypting"
    _write_keys_json(body)
    vault.forget_key()
    with pytest.raises(Damaged, match="changed"):
        vault.resume()
    assert all(vault.is_encrypted(path.read_bytes()) for path in files)


def test_a_forged_turn_off_without_a_mac_is_refused_too() -> None:
    _seed()
    vault.enable(use_dpapi=True)
    body = _keys_json()
    body["state"] = "decrypting"
    del body["mac"]
    _write_keys_json(body)
    vault.forget_key()
    with pytest.raises(Damaged):
        vault.resume()


def test_a_genuine_interrupted_turn_off_still_finishes() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    keys = vault.load_keys()
    assert keys is not None
    vault._save_keys(keys.replace(state=State.DECRYPTING))  # what disable() writes first
    vault.forget_key()
    vault.resume()
    assert vault.state() is State.PLAIN
    for path, body in files.items():
        assert path.read_bytes() == body


def test_a_deleted_key_file_comes_back_from_its_copy() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    vault.keys_path().unlink()
    assert vault.state() is State.ON
    vault.forget_key()
    path = next(iter(files))
    assert vault.read_bytes(path) == files[path]


def test_plain_text_never_overwrites_an_encrypted_file_after_the_keys_are_deleted() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    vault.keys_path().unlink()
    (secrets_dir() / vault.KEYS_BACKUP).unlink()
    vault.forget_key()
    path = next(iter(files))
    with pytest.raises(Damaged, match="missing"):
        vault.write_bytes(path, b"plain over it")
    assert vault.is_encrypted(path.read_bytes())


@pytest.mark.parametrize(
    ("field", "value"), [("memory", 10**9), ("iterations", 10**6), ("lanes", 0)]
)
@pytest.mark.usefixtures("cheap_argon")
def test_a_key_file_asking_for_absurd_password_work_is_refused(field: str, value: int) -> None:
    vault.enable(password=PASSWORD, use_dpapi=True)
    body = _keys_json()
    for slot in body["slots"]:
        if slot["kind"] == "password":
            slot[field] = value
    _write_keys_json(body)
    vault.forget_key()
    with pytest.raises(Damaged):
        vault.unlock(password=PASSWORD)


def test_a_tampered_slot_list_is_noticed_at_unlock() -> None:
    vault.enable(use_dpapi=True)
    body = _keys_json()
    body["slots"] = [slot for slot in body["slots"] if slot["kind"] != "recovery"]
    _write_keys_json(body)
    vault.forget_key()
    with pytest.raises(Damaged, match="changed"):
        vault.unlock()


def test_try_resume_finishes_an_interrupted_turn_on() -> None:
    files = _seed()
    vault.enable(use_dpapi=True)
    keys = vault.load_keys()
    assert keys is not None
    vault.disable()
    for path in files:  # plain again; now pretend a turn-on stopped half way
        assert not vault.is_encrypted(path.read_bytes())
    vault.enable(use_dpapi=True)
    assert vault.try_resume() and vault.state() is State.ON


def test_old_temp_files_left_by_a_crash_are_swept() -> None:
    _seed()
    old = _data(".journal.json-abc123.tmp")
    fresh = _data(".journal.json-def456.tmp")
    feed = secrets_dir() / "feeds" / ".feed-1-xyz.tmp"
    for path in (old, fresh, feed):
        path.write_bytes(b"half written plain text")
    stale = vault.time.time() - vault.STALE_TEMP_SECONDS - 5
    os.utime(old, (stale, stale))
    os.utime(feed, (stale, stale))
    assert vault.sweep_temp_files() == 2
    assert not old.exists() and not feed.exists()
    assert fresh.exists()  # may be a write still in progress
    assert _data("journal.json").exists()
