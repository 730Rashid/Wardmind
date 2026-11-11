"""Backups (core/backup.py): the sealed file, restoring it as a new book, and the CSV exports.

The round trip runs through the real vault, books and secrets in the temp folders from conftest.
Crafted files are built with the module's own sealing, so each test changes exactly one thing.
Argon2's cost is tiny here (vault.ARGON2 is lowered), except where a test asks for the real header.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import stat
import zipfile
from datetime import UTC, date, datetime
from typing import Any

import pytest

from wardmind.core import audit, backup, books, config, journal, secrets, vault
from wardmind.core.journal import AccountJournal, Direction, FollowUp, Journal, Note, TouchKind
from wardmind.core.models import (
    Account,
    Health,
    Milestones,
    Motion,
    Reason,
    ScoredAccount,
    SignalKind,
)
from wardmind.core.store import LocalStore, data_dir

PASSWORD = "lantern-orbit-7731"
TOKEN = "pat-na1-0123456789abcdef0123"
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 11)


@pytest.fixture(autouse=True)
def cheap_argon2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2 at its smallest cost: the header still carries the values vault.ARGON2 holds."""
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows protection replaced by a reversible stand-in (as in test_vault_integration.py)."""
    monkeypatch.setattr(vault, "dpapi_protect", lambda data: b"DP" + bytes(reversed(data)))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)
    monkeypatch.setattr(vault, "dpapi_available", lambda: True)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[audit.Event, str | None, str]]:
    """Every activity-log call the backup code makes, as (event, book, detail)."""
    calls: list[tuple[audit.Event, str | None, str]] = []

    def record(event: audit.Event, *, book: str | None = None, detail: str = "") -> None:
        calls.append((event, book, detail))

    monkeypatch.setattr(audit, "record", record)
    return calls


# --- Helpers -----------------------------------------------------------------------------------


def _seed() -> None:
    """The open book with a note, a spreadsheet snapshot, a token and a feed key."""
    store = LocalStore(data_dir())
    Journal(store).add_note(
        "acc-1", TouchKind.CALL, "Talked renewal terms", TODAY, now=NOW, today=TODAY
    )
    vault.write_text(data_dir() / "sheets" / "sheet-1.json", '{"accounts": []}')
    secrets.save_hubspot_token(TOKEN)
    secrets.save_secret("feed-1", "sheet password 1234")


def _tree(book_id: str) -> dict[str, bytes]:
    """Every file a book holds, by logical path, decrypted. Written independently of the module."""
    tree: dict[str, bytes] = {}
    data = books.book_data_dir(book_id)
    if data.is_dir():
        for path in data.rglob("*"):
            if path.is_file() and not path.name.startswith(".") and not path.name.endswith(".tmp"):
                tree["data/" + path.relative_to(data).as_posix()] = vault.read_bytes(path)
    root = books.book_folder(book_id)
    for name in ("hubspot-token", "openrouter-key"):
        if (root / name).is_file():
            tree[f"secrets/{name}"] = vault.read_bytes(root / name)
    feeds = root / "feeds"
    if feeds.is_dir():
        for path in feeds.iterdir():
            if path.is_file():
                tree[f"secrets/feeds/{path.name}"] = vault.read_bytes(path)
    return tree


def _data_only(tree: dict[str, bytes]) -> dict[str, bytes]:
    return {name: body for name, body in tree.items() if name.startswith("data/")}


def _unsealed(blob: bytes) -> zipfile.ZipFile:
    payload = backup._open(blob, backup.read_header(blob), PASSWORD)
    return zipfile.ZipFile(io.BytesIO(payload))


def _manifest_of(blob: bytes) -> dict[str, Any]:
    with _unsealed(blob) as archive:
        return json.loads(archive.read(backup.MANIFEST))


def _header(**changes: Any) -> bytes:
    """A header in the Wardmind shape, with some fields changed."""
    fields: dict[str, Any] = {
        "kdf": "argon2id",
        "memory": vault.ARGON2["memory"],
        "iterations": vault.ARGON2["iterations"],
        "lanes": vault.ARGON2["lanes"],
        "salt": base64_of(bytes(16)),
    }
    fields.update(changes)
    return json.dumps(fields).encode("ascii")


def base64_of(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _raw(
    header: bytes, *, magic: bytes = b"WMBK", version: int = 1, rest: bytes = bytes(40)
) -> bytes:
    """A blob from a header and whatever follows it (a nonce and a body of zeros by default)."""
    return magic + bytes([version]) + len(header).to_bytes(2, "big") + header + rest


def _manifest_for(
    files: list[tuple[str, bytes]], *, includes_keys: bool = False, **changes: Any
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": "wardmind-backup",
        "version": 1,
        "created_at": "2026-11-11T09:00:00Z",
        "files": [
            {"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in files
        ],
        "includes_keys": includes_keys,
    }
    manifest.update(changes)
    return manifest


def _named(name: str, body: bytes = b"x") -> zipfile.ZipInfo:
    """A member called exactly `name`. ZipInfo rewrites a backslash to a slash on Windows, so the
    name is set after the object is made."""
    info = zipfile.ZipInfo("placeholder")
    info.filename = name
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _sealed_zip(members: list[tuple[str | zipfile.ZipInfo, bytes]], manifest: Any) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(backup.MANIFEST, json.dumps(manifest))
        for name, body in members:
            archive.writestr(name, body)
    return backup._seal(buffer.getvalue(), PASSWORD)


def _restore(members: list[tuple[str | zipfile.ZipInfo, bytes]], manifest: Any) -> books.Book:
    return backup.restore_backup(_sealed_zip(members, manifest), PASSWORD, "Restored")


def _scored(
    name: str = "Acme Robotics",
    *,
    account_id: str = "acc-1",
    owner: str = "Ilse Marrowby",
    health: Health = Health.WATCH,
    priority: float = 42.0,
    arr: float = 12000.0,
    reasons: tuple[str, ...] = (),
) -> ScoredAccount:
    account = Account(
        id=account_id,
        name=name,
        arr=arr,
        tier="Growth",
        owner=owner,
        renewal_date=date(2027, 1, 23),
        milestones=Milestones(done=0, total=0),
    )
    return ScoredAccount(
        account=account,
        motion=Motion.RENEWAL,
        health=health,
        risk=10.0,
        priority=priority,
        reasons=[
            Reason(kind=SignalKind.NPS_LATEST, contribution=10.0, plain_english=text)
            for text in reasons
        ],
    )


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# --- The sealed file ---------------------------------------------------------------------------


def test_the_file_starts_with_the_magic_and_version_and_its_header_names_the_cost(
    recorded: list[Any],
) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    assert blob.startswith(b"WMBK\x01")
    info = backup.read_header(blob)
    assert (info.memory, info.iterations, info.lanes) == (8, 1, 1)
    assert len(info.salt) == 16


def test_the_real_argon2_cost_is_what_a_backup_header_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "ARGON2", {"memory": 131072, "iterations": 3, "lanes": 4})
    _seed()
    blob = backup.make_backup(PASSWORD)
    info = backup.read_header(blob)
    assert (info.memory, info.iterations, info.lanes) == (131072, 3, 4)


def test_a_backup_of_the_plain_book_restores_identical_files_as_a_new_book(
    recorded: list[Any],
) -> None:
    _seed()
    before = _data_only(_tree(books.DEFAULT_BOOK))
    blob = backup.make_backup(PASSWORD)
    new = backup.restore_backup(blob, PASSWORD, "Book 2")

    assert _data_only(_tree(new.id)) == before
    assert new.label == "Book 2" and new.id != books.DEFAULT_BOOK
    scoring = books.book_folder(new.id) / "scoring.yaml"
    assert config.load_scoring_config(scoring) == config.load_scoring_config()


def test_the_restored_book_is_listed_but_not_opened(recorded: list[Any]) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    current = books.load().current
    new = backup.restore_backup(blob, PASSWORD, "Book 2")
    assert books.load().get(new.id) is not None
    assert books.load().current == current == books.DEFAULT_BOOK
    assert books.active_id() == books.DEFAULT_BOOK


def test_restored_files_are_encrypted_on_disk_when_encryption_is_on(
    fake_dpapi: None, recorded: list[Any]
) -> None:
    _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    before = _data_only(_tree(books.DEFAULT_BOOK))
    blob = backup.make_backup(PASSWORD)
    new = backup.restore_backup(blob, PASSWORD, "Book 2")

    raw = [path.read_bytes() for path in books.book_data_dir(new.id).rglob("*") if path.is_file()]
    assert raw and all(vault.is_encrypted(body) for body in raw)
    assert not any(b"Talked renewal terms" in body for body in raw)
    assert _data_only(_tree(new.id)) == before


def test_keys_are_left_out_unless_asked_for(recorded: list[Any]) -> None:
    _seed()
    plain = backup.make_backup(PASSWORD)
    assert _manifest_of(plain)["includes_keys"] is False
    assert not any(entry["path"].startswith("secrets/") for entry in _manifest_of(plain)["files"])
    new = backup.restore_backup(plain, PASSWORD, "Book 2")
    assert not (books.book_folder(new.id) / "hubspot-token").exists()
    assert not (books.book_folder(new.id) / "feeds").exists()


def test_keys_are_included_when_asked_for(recorded: list[Any]) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD, include_keys=True)
    assert _manifest_of(blob)["includes_keys"] is True
    kept = backup.restore_backup(blob, PASSWORD, "Book 2")
    assert vault.read_text(books.book_folder(kept.id) / "hubspot-token") == TOKEN
    assert vault.read_text(books.book_folder(kept.id) / "feeds" / "feed-1") == (
        "sheet password 1234"
    )


def test_a_backup_names_its_files_by_logical_path_with_a_manifest_first(
    recorded: list[Any],
) -> None:
    _seed()
    with _unsealed(backup.make_backup(PASSWORD)) as archive:
        names = [info.filename for info in archive.infolist()]
    assert names[0] == backup.MANIFEST
    assert "data/journal.json" in names
    assert "data/sheets/sheet-1.json" in names
    assert backup.SCORING_FILE in names
    assert all("\\" not in name for name in names)


def test_the_manifest_records_each_file_size_and_hash(recorded: list[Any]) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    tree = _tree(books.DEFAULT_BOOK)
    entries = {entry["path"]: entry for entry in _manifest_of(blob)["files"]}
    for name, body in _data_only(tree).items():
        assert entries[name]["size"] == len(body)
        assert entries[name]["sha256"] == hashlib.sha256(body).hexdigest()


def test_the_password_rules_apply_when_making(recorded: list[Any]) -> None:
    _seed()
    with pytest.raises(backup.BackupError) as caught:
        backup.make_backup("short")
    assert "10 characters" in caught.value.message


def test_a_book_over_the_size_limit_makes_no_backup(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    _seed()
    monkeypatch.setattr(backup, "MAX_BOOK_BYTES", 10)
    with pytest.raises(backup.BackupError) as caught:
        backup.make_backup(PASSWORD)
    assert caught.value.message == backup.TOO_BIG_BOOK
    assert recorded == []


def test_a_locked_book_makes_no_backup(fake_dpapi: None, recorded: list[Any]) -> None:
    _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    vault.lock()
    with pytest.raises(vault.Locked):
        backup.make_backup(PASSWORD)
    assert recorded == []


def test_making_and_restoring_are_recorded_in_the_activity_log(
    recorded: list[tuple[audit.Event, str | None, str]],
) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    new = backup.restore_backup(blob, PASSWORD, "Book 2")
    kinds = [
        call
        for call in recorded
        if call[0] in (audit.Event.BACKUP_MADE, audit.Event.BACKUP_RESTORED)
    ]
    assert kinds == [
        (audit.Event.BACKUP_MADE, books.DEFAULT_BOOK, ""),
        (audit.Event.BACKUP_RESTORED, new.id, ""),
    ]


def test_last_backup_at_reads_the_newest_backup_of_the_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    when = datetime(2026, 11, 10, 8, 30, tzinfo=UTC)

    class Entry:
        at = when

    seen: list[tuple[audit.Event, str | None]] = []

    def last(event: audit.Event, *, book: str | None = None) -> Entry:
        seen.append((event, book))
        return Entry()

    monkeypatch.setattr(audit, "last", last)
    assert backup.last_backup_at("b-0123abcd") == when
    assert seen == [(audit.Event.BACKUP_MADE, "b-0123abcd")]


# --- Refusing a backup that isn't ours, or was changed -----------------------------------------


def test_the_wrong_password_is_refused_and_nothing_is_made(recorded: list[Any]) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    before = books.load()
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_backup(blob, "not-the-password-1", "Book 2")
    assert caught.value.message == (
        "That password doesn't open this backup, or the file was changed."
    )
    assert books.load() == before


def test_a_changed_byte_anywhere_in_the_file_fails(recorded: list[Any]) -> None:
    """Every byte, one bit at a time: the header, the nonce and the sealed body."""
    _seed()
    blob = backup.make_backup(PASSWORD)
    for index in range(len(blob)):
        changed = bytearray(blob)
        changed[index] ^= 0x01
        with pytest.raises(backup.BackupError):
            backup.restore_backup(bytes(changed), PASSWORD, "Changed")
    assert [book.label for book in books.load().books] == [books.DEFAULT_LABEL]


def test_a_file_that_is_not_a_backup_is_refused() -> None:
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_backup(b"%PDF-1.7 not a backup at all", PASSWORD, "Book 2")
    assert caught.value.message == backup.NOT_A_BACKUP


def test_a_newer_format_version_is_refused() -> None:
    blob = _raw(_header(), version=2)
    with pytest.raises(backup.BackupError) as caught:
        backup.read_header(blob)
    assert caught.value.message == backup.NEWER_BACKUP


def test_a_wrong_magic_is_refused() -> None:
    with pytest.raises(backup.BackupError) as caught:
        backup.read_header(_raw(_header(), magic=b"ZIPX"))
    assert caught.value.message == backup.NOT_A_BACKUP


def test_a_header_longer_than_the_limit_is_refused() -> None:
    blob = _raw(b"{" * (backup.MAX_HEADER_BYTES + 1))
    with pytest.raises(backup.BackupError) as caught:
        backup.read_header(blob)
    assert caught.value.message == backup.NOT_A_BACKUP


@pytest.mark.parametrize(
    "header",
    [
        b"not json at all",
        b"[1, 2, 3]",
        json.dumps({"kdf": "argon2id"}).encode(),
        _header(extra="field"),
    ],
)
def test_a_header_that_isnt_the_format_is_refused(header: bytes) -> None:
    with pytest.raises(backup.BackupError) as caught:
        backup.read_header(_raw(header))
    assert caught.value.message == backup.NOT_A_BACKUP


@pytest.mark.parametrize(
    "header",
    [
        _header(kdf="scrypt"),
        _header(memory=2**30),
        _header(memory=131072.0),
        _header(iterations=1000),
        _header(lanes=True),
    ],
)
def test_a_crafted_argon2_cost_is_refused_before_anything_is_derived(
    header: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("Argon2 must not run for a header that asks for another cost")

    monkeypatch.setattr(backup, "Argon2id", refuse)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_backup(_raw(header), PASSWORD, "Book 2")
    assert caught.value.message == backup.UNKNOWN_SETTINGS


@pytest.mark.parametrize("salt", [base64_of(bytes(8)), base64_of(bytes(17)), "!!not-base64!!", 7])
def test_a_salt_that_isnt_sixteen_bytes_is_refused(
    salt: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("Argon2 must not run for a damaged salt")

    monkeypatch.setattr(backup, "Argon2id", refuse)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_backup(_raw(_header(salt=salt)), PASSWORD, "Book 2")
    assert caught.value.message == backup.DAMAGED


def test_an_oversize_blob_is_refused_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "MAX_BACKUP_BYTES", 100)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_backup(b"x" * 101, PASSWORD, "Book 2")
    assert caught.value.message == backup.TOO_BIG_FILE


def test_a_bad_book_name_is_refused_before_the_file_is_read() -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(blob, PASSWORD, "   ")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(blob, PASSWORD, books.DEFAULT_LABEL)


def test_a_locked_wardmind_restores_nothing(fake_dpapi: None, recorded: list[Any]) -> None:
    _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    blob = backup.make_backup(PASSWORD)
    vault.lock()
    before = books.load()
    with pytest.raises(vault.Locked):
        backup.restore_backup(blob, PASSWORD, "Book 2")
    assert books.load() == before


# --- Zip checks: the file list, names, entry types, sizes and limits ---------------------------


def test_the_existing_book_is_never_overwritten(recorded: list[Any]) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    other = books.add("Book 2")
    vault.write_text(books.book_data_dir(other.id) / "journal.json", '{"by_account": {}}')
    before = _tree(other.id)

    with pytest.raises(backup.BackupError):
        backup.restore_backup(blob, PASSWORD, "book 2")  # the same name, ignoring case
    assert _tree(other.id) == before

    new = backup.restore_backup(blob, PASSWORD, "Book 3")
    assert new.id != other.id
    assert _tree(other.id) == before


@pytest.mark.parametrize(
    "name",
    [
        "../x",
        "/abs",
        "C:/x",
        "C:x",
        "a\\b",
        "data\\..\\..\\x",
        "data/../../x",
        "data//x",
        "data/./x",
        "data/CON",
        "data/nul.json",
        "data/x.",
        "data/x ",
        "data/a:stream",
        "data/" + "a" * 256,
        "data/a\x01b",
    ],
)
def test_an_unsafe_name_is_refused(name: str) -> None:
    body = b"x"
    manifest = _manifest_for([(name, body)])
    with pytest.raises(backup.BackupError) as caught:
        _restore([(_named(name, body), body)], manifest)
    assert caught.value.message == backup.UNSAFE


@pytest.mark.parametrize(
    "name", ["keys.json", "books.json", "session.json", "unlock-ticket.json", "activity-log.jsonl"]
)
def test_wardmind_own_top_level_files_are_refused(name: str) -> None:
    body = b"{}"
    manifest = _manifest_for([(name, body)], includes_keys=True)
    with pytest.raises(backup.BackupError) as caught:
        _restore([(name, body)], manifest)
    assert caught.value.message == backup.UNSAFE


@pytest.mark.parametrize(
    "name", ["secrets/keys.json", "secrets/session.json", "secrets/config/x.yaml", "settings.yaml"]
)
def test_only_the_books_own_secrets_are_restored(name: str) -> None:
    body = b"{}"
    manifest = _manifest_for([(name, body)], includes_keys=True)
    with pytest.raises(backup.BackupError) as caught:
        _restore([(name, body)], manifest)
    assert caught.value.message == backup.UNSAFE


def test_a_secret_is_refused_when_the_backup_leaves_keys_out() -> None:
    body = b"token"
    manifest = _manifest_for([("secrets/hubspot-token", body)], includes_keys=False)
    with pytest.raises(backup.BackupError) as caught:
        _restore([("secrets/hubspot-token", body)], manifest)
    assert caught.value.message == backup.UNSAFE


@pytest.mark.parametrize("name", ["data/.hidden", "data/journal.json.tmp", "data/.x/y.json"])
def test_hidden_and_temporary_files_are_refused(name: str) -> None:
    body = b"{}"
    manifest = _manifest_for([(name, body)])
    with pytest.raises(backup.BackupError) as caught:
        _restore([(name, body)], manifest)
    assert caught.value.message == backup.UNSAFE


def test_a_member_missing_from_the_manifest_is_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}")])
    members: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        ("data/a.json", b"{}"),
        ("data/extra.json", b"{}"),
    ]
    with pytest.raises(backup.BackupError) as caught:
        _restore(members, manifest)
    assert caught.value.message == backup.DAMAGED


def test_a_manifest_entry_missing_from_the_zip_is_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}"), ("data/b.json", b"{}")])
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}")], manifest)
    assert caught.value.message == backup.DAMAGED


def test_a_hash_that_doesnt_match_is_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}")])
    manifest["files"][0]["sha256"] = "0" * 64
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}")], manifest)
    assert caught.value.message == backup.DAMAGED


def test_a_size_that_doesnt_match_is_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}")])
    manifest["files"][0]["size"] = 3
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}")], manifest)
    assert caught.value.message == backup.DAMAGED


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_a_duplicate_name_is_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}")])
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}"), ("data/a.json", b"{}")], manifest)
    assert caught.value.message == backup.DAMAGED


def test_names_that_differ_only_in_case_are_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}"), ("data/A.json", b"{}")])
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}"), ("data/A.json", b"{}")], manifest)
    assert caught.value.message == backup.DAMAGED


def test_a_symlink_member_is_refused() -> None:
    link = _named("data/link.json", b"/etc/passwd")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    manifest = _manifest_for([("data/link.json", b"/etc/passwd")])
    with pytest.raises(backup.BackupError) as caught:
        _restore([(link, b"/etc/passwd")], manifest)
    assert caught.value.message == backup.UNSAFE


def test_a_member_with_an_absurd_ratio_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "MAX_RATIO", 2)
    body = bytes(1024 * 1024)  # a megabyte of zeros packs about a thousand to one
    manifest = _manifest_for([("data/zeros.bin", body)])
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/zeros.bin", body)], manifest)
    assert caught.value.message == backup.UNSAFE


def test_too_many_entries_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "MAX_ENTRIES", 3)
    files = [(f"data/f{n}.json", b"{}") for n in range(5)]
    with pytest.raises(backup.BackupError) as caught:
        _restore(list(files), _manifest_for(files))
    assert caught.value.message == backup.TOO_MANY


def test_a_member_that_unpacks_past_the_limit_is_stopped_while_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest lies about the size. The reader still stops at the limit, not at the claim."""
    monkeypatch.setattr(backup, "MAX_RESTORED_BYTES", 1024 * 1024)
    body = random.Random(7).randbytes(3 * 1024 * 1024)  # does not compress, so no ratio applies
    manifest = _manifest_for([("data/big.bin", b"x")])
    manifest["files"][0]["size"] = 1
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/big.bin", body)], manifest)
    assert caught.value.message == backup.TOO_BIG_RESTORE


def test_a_backup_whose_files_are_honestly_too_big_is_refused_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup, "MAX_RESTORED_BYTES", 1024)
    body = b"y" * 4096
    manifest = _manifest_for([("data/big.bin", body)])
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/big.bin", body)], manifest)
    assert caught.value.message == backup.TOO_BIG_RESTORE


def test_an_unknown_manifest_format_is_refused() -> None:
    manifest = _manifest_for([("data/a.json", b"{}")], format="something-else")
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}")], manifest)
    assert caught.value.message == backup.NOT_A_BACKUP


@pytest.mark.parametrize("version", [True, 2])
def test_a_manifest_with_another_version_is_refused(version: object) -> None:
    manifest = _manifest_for([("data/a.json", b"{}")], version=version)
    with pytest.raises(backup.BackupError) as caught:
        _restore([("data/a.json", b"{}")], manifest)
    assert caught.value.message == backup.NEWER_BACKUP


def test_a_zip_that_is_not_a_zip_is_refused() -> None:
    blob = backup._seal(b"this is not a zip file", PASSWORD)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_backup(blob, PASSWORD, "Book 2")
    assert caught.value.message == backup.DAMAGED


def test_a_scoring_file_that_isnt_valid_is_refused_before_a_book_is_made() -> None:
    body = b"signals: [unclosed"
    manifest = _manifest_for([(backup.SCORING_FILE, body)])
    before = books.load()
    with pytest.raises(backup.BackupError) as caught:
        _restore([(backup.SCORING_FILE, body)], manifest)
    assert caught.value.message == backup.SCORING_INVALID
    assert books.load() == before


def test_a_failed_write_removes_the_half_made_book(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    _seed()
    blob = backup.make_backup(PASSWORD)
    before = [book.id for book in books.load().books]
    real_write = vault.write_bytes
    calls: list[Any] = []

    def failing_write(path: Any, data: bytes) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError("the disk is full")
        real_write(path, data)

    monkeypatch.setattr(vault, "write_bytes", failing_write)
    with pytest.raises(OSError):
        backup.restore_backup(blob, PASSWORD, "Book 2")

    assert [book.id for book in books.load().books] == before
    folder = books.other_books_dir()
    assert not folder.exists() or not any(folder.iterdir())
    assert all(event is not audit.Event.BACKUP_RESTORED for event, _, _ in recorded)


def test_a_refused_book_key_leaves_no_book(
    monkeypatch: pytest.MonkeyPatch, fake_dpapi: None
) -> None:
    _seed()
    vault.enable(password=PASSWORD, use_dpapi=True)
    blob = backup.make_backup(PASSWORD)
    before = books.load()

    def locked(*args: object, **kwargs: object) -> None:
        raise vault.Locked("Wardmind is locked.")

    monkeypatch.setattr(vault, "add_book_key", locked)
    with pytest.raises(vault.Locked):
        backup.restore_backup(blob, PASSWORD, "Book 2")
    assert books.load() == before


# --- CSV exports -------------------------------------------------------------------------------


def test_the_accounts_csv_has_the_book_columns_in_book_order() -> None:
    book = [
        _scored(
            "Acme Robotics",
            account_id="acc-1",
            priority=42.0,
            arr=12000.0,
            reasons=("Active users down 38% in 30 days",),
        ),
        _scored("Bolt Labs", account_id="acc-2", health=Health.HEALTHY, priority=8.64, arr=9000.5),
    ]
    rows = _rows(backup.accounts_csv(book))
    assert rows[0] == [
        "Account",
        "Account id",
        "Health",
        "Priority",
        "Motion",
        "ARR",
        "Renewal date",
        "Owner",
        "Top reason",
    ]
    assert rows[1] == [
        "Acme Robotics",
        "acc-1",
        "Watch",
        "42",
        "Renewal",
        "12000",
        "2027-01-23",
        "Ilse Marrowby",
        "Active users down 38% in 30 days",
    ]
    assert rows[2] == [
        "Bolt Labs",
        "acc-2",
        "Healthy",
        "8.6",
        "Renewal",
        "9000.50",
        "2027-01-23",
        "Ilse Marrowby",
        "No risk signals",
    ]


def test_an_empty_book_exports_just_the_header() -> None:
    assert _rows(backup.accounts_csv([])) == [list(backup.ACCOUNT_COLUMNS)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('=HYPERLINK("http://x.example")', '\'=HYPERLINK("http://x.example")'),
        ("+15551234", "'+15551234"),
        ("-25", "'-25"),
        ("@SUM(A1:A9)", "'@SUM(A1:A9)"),
        ("\tcmd", "'\tcmd"),
        ("\rcmd", "'\rcmd"),
        ("  =1+1", "'  =1+1"),
        ("Acme - 2", "Acme - 2"),
        ("Ilse Marrowby", "Ilse Marrowby"),
    ],
)
def test_a_cell_a_spreadsheet_would_run_as_a_formula_is_made_text(
    value: str, expected: str
) -> None:
    book = [_scored(value, owner=value)]
    row = _rows(backup.accounts_csv(book))[1]
    assert row[0] == expected
    assert row[7] == expected


def test_a_formula_in_a_top_reason_is_made_text() -> None:
    book = [_scored(reasons=("=cmd|' /C calc'!A0",))]
    assert _rows(backup.accounts_csv(book))[1][8] == "'=cmd|' /C calc'!A0"


def test_notes_and_follow_ups_export_one_row_each_for_the_accounts_in_the_book() -> None:
    book = [_scored("Acme Robotics", account_id="acc-1")]
    journals = {
        "acc-1": AccountJournal(
            notes=[
                Note(
                    id="n-00000001",
                    kind=TouchKind.EMAIL,
                    text="Sent the revised quote",
                    happened_on=date(2026, 11, 9),
                    created_at=NOW,
                    direction=Direction.SENT,
                    subject="Revised quote",
                ),
                Note(
                    id="n-00000002",
                    kind=TouchKind.NOTE,
                    text="=cmd()",
                    happened_on=date(2026, 11, 10),
                    created_at=NOW,
                ),
            ],
            follow_ups=[
                FollowUp(
                    id="f-00000001",
                    text="Check the renewal terms",
                    due=date(2026, 11, 20),
                    created_at=NOW,
                ),
                FollowUp(
                    id="f-00000002",
                    text="Send the invoice",
                    due=date(2026, 11, 1),
                    status=journal.FollowUpStatus.DONE,
                    created_at=NOW,
                    done_at=NOW,
                ),
            ],
        ),
        "gone": AccountJournal(
            notes=[
                Note(
                    id="n-00000003",
                    kind=TouchKind.CALL,
                    text="old",
                    happened_on=TODAY,
                    created_at=NOW,
                )
            ]
        ),
    }
    rows = _rows(backup.notes_csv(book, journals))
    assert rows[0] == [
        "Account",
        "Type",
        "Date",
        "Kind",
        "Subject",
        "Text",
        "Due",
        "Status",
    ]
    assert rows[1] == [
        "Acme Robotics",
        "Note",
        "2026-11-09",
        "Email (sent)",
        "Revised quote",
        "Sent the revised quote",
        "",
        "",
    ]
    assert rows[2] == ["Acme Robotics", "Note", "2026-11-10", "Note", "", "'=cmd()", "", ""]
    assert rows[3] == [
        "Acme Robotics",
        "Follow-up",
        "2026-11-11",
        "",
        "",
        "Check the renewal terms",
        "2026-11-20",
        "Open",
    ]
    assert rows[4] == [
        "Acme Robotics",
        "Follow-up",
        "2026-11-11",
        "",
        "",
        "Send the invoice",
        "2026-11-01",
        "Done",
    ]
    assert len(rows) == 5  # "gone" is not in the book, so its note is not exported


def test_a_multi_line_note_stays_one_cell() -> None:
    book = [_scored("Acme Robotics", account_id="acc-1")]
    journals = {
        "acc-1": AccountJournal(
            notes=[
                Note(
                    id="n-00000004",
                    kind=TouchKind.MEETING,
                    text="First line\nSecond line",
                    happened_on=TODAY,
                    created_at=NOW,
                )
            ]
        )
    }
    rows = _rows(backup.notes_csv(book, journals))
    assert rows[1][5] == "First line\nSecond line"
