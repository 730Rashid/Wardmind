"""Backups and exports for Settings > Backup & export.

A backup is one file holding the open book, sealed with a password the person chooses. Restoring
it always makes a new book (core/books.py) and never overwrites one or opens the new one. Exports
are CSV files of the scored book and of its notes and follow-ups, made safe to open in a
spreadsheet (no cell can start a formula).

The backup file (integers are big-endian):

    b"WMBK"        magic
    1 byte         format version (FORMAT_VERSION)
    2 bytes        header length (at most MAX_HEADER_BYTES)
    header         JSON: {"kdf": "argon2id", "memory", "iterations", "lanes", "salt"}; the salt
                   is 16 random bytes, base64. Only the cost in vault.ARGON2 is accepted.
    12 bytes       nonce
    the rest       AES-256-GCM of a zip file, under the key Argon2id(password, salt) gives. The
                   bytes before the nonce are the additional authenticated data, so the header
                   can't be changed without failing.

The zip holds manifest.json first, then each file under its logical path (core/vault.py's
"data/..." or "secrets/...", and "scoring.yaml"). The manifest gives each file's size and SHA-256
and says whether the keys (the secrets) are included. Restoring checks all of it before a book is
made. A header that asks for any other Argon2 cost is refused before anything is derived, so a
crafted file can't make Argon2 use gigabytes of memory.

Restored files go through core/vault.py, so they are encrypted when encryption is on. scoring.yaml
is the exception: config.py reads it as plain text, so it is written the way config.py writes it.

CONTRACT FILE: last_backup_at (below) and the names the Settings page uses are fixed.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import stat
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from wardmind.core import audit, books, config, journal, vault
from wardmind.core.models import ScoredAccount

MAGIC = b"WMBK"
FORMAT_VERSION = 1
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
MAX_HEADER_BYTES = 1024
MAX_BACKUP_BYTES = 250 * 1024 * 1024  # the sealed file, as read
MAX_BOOK_BYTES = 200 * 1024 * 1024  # a book's files before sealing: more than this, no backup
MAX_RESTORED_BYTES = 500 * 1024 * 1024  # a backup's files once unpacked
MAX_ENTRIES = 20_000  # zip members, and manifest entries
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SCORING_BYTES = 1024 * 1024
MAX_NAME = 255  # characters in one file's logical path
# Deflate packs at most about 1032 bytes into one, so a member claiming more is not real.
MAX_RATIO = 1100
MANIFEST = "manifest.json"
MANIFEST_FORMAT = "wardmind-backup"
SCORING_FILE = "scoring.yaml"
_CHUNK = 1024 * 1024
_HEADER_KEYS = frozenset({"kdf", "memory", "iterations", "lanes", "salt"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{n}" for n in range(1, 10)),
        *(f"lpt{n}" for n in range(1, 10)),
    }
)
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")

WRONG_PASSWORD = "That password doesn't open this backup, or the file was changed."
NOT_A_BACKUP = "That file isn't a Wardmind backup."
NEWER_BACKUP = "This backup was made in a format this version of Wardmind doesn't read."
UNKNOWN_SETTINGS = "This backup uses settings this version of Wardmind doesn't read."
TOO_BIG_FILE = "That file is too big to be a Wardmind backup."
TOO_BIG_BOOK = (
    "This book's files come to more than 200 MB, which is too much for one backup. Export its "
    "accounts and notes as CSV instead."
)
TOO_BIG_RESTORE = (
    "This backup holds more than 500 MB of files, which is too much to restore at once."
)
TOO_MANY = "This backup holds more files than Wardmind restores, so nothing was restored."
DAMAGED = "This backup is damaged, so nothing was restored."
UNSAFE = "This backup has a file Wardmind won't restore, so nothing was restored."
SCORING_INVALID = "The scoring in this backup isn't valid, so nothing was restored."


class BackupError(Exception):
    """A backup that can't be made or restored. `message` is plain English, safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _Entry:
    """One file as the manifest lists it."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupInfo:
    """What the start of a backup says, before the password is used."""

    memory: int
    iterations: int
    lanes: int
    salt: bytes
    nonce_at: int  # offset of the nonce; everything before it is authenticated


def last_backup_at(book_id: str) -> datetime | None:
    """When a backup of this book was last made (from the activity log), or None."""
    entry = audit.last(audit.Event.BACKUP_MADE, book=book_id)
    return entry.at if entry is not None else None


# --- The sealed file ----------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _key(password: str, salt: bytes, memory: int, iterations: int, lanes: int) -> bytes:
    """The file key: Argon2id, with the same algorithm and cost as the vault's password slot."""
    return Argon2id(
        salt=salt, length=KEY_BYTES, iterations=iterations, lanes=lanes, memory_cost=memory
    ).derive(password.encode("utf-8"))


def read_header(blob: bytes) -> BackupInfo:
    """What the start of a backup says, checked against this format. BackupError for anything
    that isn't exactly it. Nothing is derived from a password here."""
    start = len(MAGIC) + 1 + 2
    if len(blob) < start or blob[: len(MAGIC)] != MAGIC:
        raise BackupError(NOT_A_BACKUP)
    if blob[len(MAGIC)] != FORMAT_VERSION:
        raise BackupError(NEWER_BACKUP)
    length = int.from_bytes(blob[len(MAGIC) + 1 : start], "big")
    if not 0 < length <= MAX_HEADER_BYTES:
        raise BackupError(NOT_A_BACKUP)
    nonce_at = start + length
    if len(blob) < nonce_at + NONCE_BYTES + TAG_BYTES:
        raise BackupError(DAMAGED)
    try:
        header = json.loads(blob[start:nonce_at].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BackupError(NOT_A_BACKUP) from error
    if not isinstance(header, dict) or set(header) != _HEADER_KEYS:
        raise BackupError(NOT_A_BACKUP)
    if header["kdf"] != "argon2id":
        raise BackupError(UNKNOWN_SETTINGS)
    cost = vault.ARGON2
    if any(type(header[name]) is not int or header[name] != cost[name] for name in cost):
        raise BackupError(UNKNOWN_SETTINGS)
    return BackupInfo(
        memory=cost["memory"],
        iterations=cost["iterations"],
        lanes=cost["lanes"],
        salt=_salt(header["salt"]),
        nonce_at=nonce_at,
    )


def _salt(value: object) -> bytes:
    if not isinstance(value, str):
        raise BackupError(DAMAGED)
    try:
        salt = base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError as error:
        raise BackupError(DAMAGED) from error
    if len(salt) != SALT_BYTES:
        raise BackupError(DAMAGED)
    return salt


def _seal(payload: bytes, password: str) -> bytes:
    salt = os.urandom(SALT_BYTES)
    cost = vault.ARGON2
    header = json.dumps(
        {
            "kdf": "argon2id",
            "memory": cost["memory"],
            "iterations": cost["iterations"],
            "lanes": cost["lanes"],
            "salt": _b64(salt),
        },
        separators=(",", ":"),
    ).encode("ascii")
    prefix = MAGIC + bytes([FORMAT_VERSION]) + len(header).to_bytes(2, "big") + header
    nonce = os.urandom(NONCE_BYTES)
    key = _key(password, salt, cost["memory"], cost["iterations"], cost["lanes"])
    return prefix + nonce + AESGCM(key).encrypt(nonce, payload, prefix)


def _open(blob: bytes, info: BackupInfo, password: str) -> bytes:
    if not password or len(password) > vault.MAX_PASSWORD:
        raise BackupError(WRONG_PASSWORD)
    prefix = blob[: info.nonce_at]
    nonce = blob[info.nonce_at : info.nonce_at + NONCE_BYTES]
    sealed = blob[info.nonce_at + NONCE_BYTES :]
    key = _key(password, info.salt, info.memory, info.iterations, info.lanes)
    try:
        return AESGCM(key).decrypt(nonce, sealed, prefix)
    except InvalidTag as error:
        raise BackupError(WRONG_PASSWORD) from error


# --- Making a backup ----------------------------------------------------------------------------


def _book_files(book_id: str, include_keys: bool) -> list[tuple[str, Path]]:
    """(logical path, file) for every file of the book the vault manages, then its scoring. The
    secrets (tokens and feed keys) only when include_keys."""
    found: list[tuple[str, Path]] = []
    for path in vault.managed_files():
        owner, logical = vault.locate(path)
        if owner != book_id or (not include_keys and logical.startswith("secrets/")):
            continue
        found.append((logical, path))
    scoring = config.scoring_path(saving=True)
    if scoring.is_file():
        found.append((SCORING_FILE, scoring))
    return found


def make_backup(password: str, *, include_keys: bool = False) -> bytes:
    """The open book, sealed with `password`. The secrets (HubSpot token, OpenRouter key, feed
    keys, mailbox password) are left out unless include_keys. BackupError for a password that
    can't be used, or a book whose files come to more than MAX_BOOK_BYTES; vault.Locked when
    Wardmind is locked. Records BACKUP_MADE for the book."""
    try:
        vault.check_password(password)
    except ValueError as error:
        raise BackupError(str(error)) from error
    if not vault.ensure_unlocked():
        raise vault.Locked("Wardmind is locked. Unlock it, then try again.")
    book_id = books.active_id()
    files = _book_files(book_id, include_keys)
    if sum(path.stat().st_size for _, path in files) > MAX_BOOK_BYTES:
        raise BackupError(TOO_BIG_BOOK)
    if len(files) + 1 > MAX_ENTRIES:
        raise BackupError(TOO_MANY)
    entries = [(logical, vault.read_bytes(path)) for logical, path in files]
    if sum(len(data) for _, data in entries) > MAX_BOOK_BYTES:
        raise BackupError(TOO_BIG_BOOK)
    manifest = {
        "format": MANIFEST_FORMAT,
        "version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "files": [
            {"path": logical, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for logical, data in entries
        ],
        "includes_keys": include_keys,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
        for logical, data in entries:
            archive.writestr(logical, data)
    sealed = _seal(buffer.getvalue(), password)
    audit.record(audit.Event.BACKUP_MADE, book=book_id)
    return sealed


# --- Restoring a backup -------------------------------------------------------------------------


def restore_backup(blob: bytes, password: str, label: str) -> books.Book:
    """Make a new book named `label` from a backup and fill it. Everything in the backup is
    checked before anything is written. The book is never overwritten and is not opened. Records
    BACKUP_RESTORED for the new book.

    BackupError for a refusal, with the message to show. vault.Locked when Wardmind is locked
    (nothing is made). If a file can't be written, the half-made book is removed and the error
    is raised again."""
    if len(blob) > MAX_BACKUP_BYTES:
        raise BackupError(TOO_BIG_FILE)
    try:
        books.check_label(label)
    except books.BookError as error:
        raise BackupError(str(error)) from error
    if not vault.ensure_unlocked():
        raise vault.Locked("Wardmind is locked. Unlock it, then try again.")
    info = read_header(blob)
    payload = _open(blob, info, password)
    members = _members(payload)
    for parts, data in members:
        if parts == (SCORING_FILE,):
            _checked_scoring(data)
    book = _add_book(label)
    try:
        for parts, data in members:
            if parts == (SCORING_FILE,):
                config.save_scoring_config(
                    _checked_scoring(data), books.book_folder(book.id) / SCORING_FILE
                )
            else:
                vault.write_bytes(_target(book.id, parts), data)
    except BaseException:
        _discard(book.id)
        raise
    audit.record(audit.Event.BACKUP_RESTORED, book=book.id)
    return book


def _add_book(label: str) -> books.Book:
    try:
        return books.add(label)
    except books.BookError as error:
        raise BackupError(str(error)) from error


def _discard(book_id: str) -> None:
    """Remove a book that couldn't be filled. A failure here is not raised: the error that stopped
    the restore is the one the person needs to see."""
    with contextlib.suppress(books.BookError, vault.VaultError, OSError):
        books.delete(book_id)


def _target(book_id: str, parts: tuple[str, ...]) -> Path:
    """Where a backup file goes inside a book: its data folder, or its own folder for secrets."""
    if parts[0] == "data":
        return books.book_data_dir(book_id).joinpath(*parts[1:])
    return books.book_folder(book_id).joinpath(*parts[1:])


def _checked_scoring(data: bytes) -> config.ScoringConfig:
    if len(data) > MAX_SCORING_BYTES:
        raise BackupError(SCORING_INVALID)
    try:
        raw = yaml.safe_load(data.decode("utf-8")) or {}
        return config.ScoringConfig.model_validate(raw)
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError) as error:
        raise BackupError(SCORING_INVALID) from error


def _members(payload: bytes) -> list[tuple[tuple[str, ...], bytes]]:
    """The zip's files, checked against its manifest: names, entry types, sizes and hashes. Each
    member is read with a running limit, so a zip bomb stops at the limit."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
        raise BackupError(DAMAGED) from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) > MAX_ENTRIES:
            raise BackupError(TOO_MANY)
        for info in infos:
            # On Windows zipfile turns a backslash into a slash when it reads a name; the name as
            # written keeps it, and that is the one to refuse.
            if "\\" in getattr(info, "orig_filename", info.filename):
                raise BackupError(UNSAFE)
        if len({name.casefold() for name in names}) != len(names):
            raise BackupError(DAMAGED)
        by_name = {info.filename: info for info in infos}
        if MANIFEST not in by_name:
            raise BackupError(DAMAGED)
        includes_keys, listed = _manifest(
            _read_limited(archive, by_name[MANIFEST], MAX_MANIFEST_BYTES, too_big=DAMAGED)
        )
        if {entry.path for entry in listed} != set(names) - {MANIFEST}:
            raise BackupError(DAMAGED)
        planned = []
        for entry in listed:
            parts = _parts_of(entry.path)
            if not _is_book_file(parts, includes_keys):
                raise BackupError(UNSAFE)
            planned.append((parts, entry))
        if sum(entry.size for entry in listed) > MAX_RESTORED_BYTES:
            raise BackupError(TOO_BIG_RESTORE)
        members: list[tuple[tuple[str, ...], bytes]] = []
        used = 0
        for parts, entry in planned:
            info = by_name[entry.path]
            _check_member(info)
            data = _read_limited(archive, info, MAX_RESTORED_BYTES - used)
            used += len(data)
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise BackupError(DAMAGED)
            members.append((parts, data))
    return members


def _manifest(raw: bytes) -> tuple[bool, list[_Entry]]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BackupError(DAMAGED) from error
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise BackupError(NOT_A_BACKUP)
    if type(manifest.get("version")) is not int or manifest["version"] != FORMAT_VERSION:
        raise BackupError(NEWER_BACKUP)
    includes_keys = manifest.get("includes_keys")
    files = manifest.get("files")
    if type(includes_keys) is not bool or not isinstance(files, list):
        raise BackupError(DAMAGED)
    if len(files) > MAX_ENTRIES:
        raise BackupError(TOO_MANY)
    seen: set[str] = set()
    listed: list[_Entry] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise BackupError(DAMAGED)
        path, size, digest = item["path"], item["size"], item["sha256"]
        if (
            not isinstance(path, str)
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or path in seen
        ):
            raise BackupError(DAMAGED)
        seen.add(path)
        listed.append(_Entry(path, size, digest))
    return includes_keys, listed


def _parts_of(name: str) -> tuple[str, ...]:
    """The name's segments, or BackupError unless it is a plain relative path: no leading slash,
    backslash, colon (drive letters and streams), control character, empty or dot segment, Windows
    device name (CON, NUL, COM1 ...), or segment ending in a dot or a space."""
    if (
        not name
        or len(name) > MAX_NAME
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise BackupError(UNSAFE)
    parts = tuple(name.split("/"))
    for part in parts:
        if (
            part in ("", ".", "..")
            or part != part.rstrip(". ")
            or part.split(".", 1)[0].casefold() in _RESERVED_NAMES
        ):
            raise BackupError(UNSAFE)
    return parts


def _is_book_file(parts: tuple[str, ...], includes_keys: bool) -> bool:
    """Only the paths a book has: its data folder, its scoring, and (when the backup includes
    keys) its named secrets. Never a top-level file of Wardmind's own (keys.json, books.json,
    session.json, the unlock ticket, the activity log)."""
    if any(part.startswith(".") or part.endswith(".tmp") for part in parts):
        return False
    if parts == (SCORING_FILE,):
        return True
    if parts[0] == "data":
        return len(parts) >= 2
    if parts[0] == "secrets" and includes_keys:
        if len(parts) == 2:
            return parts[1] in vault.SECRET_FILES
        if len(parts) == 3:
            return parts[1] in vault.SECRET_FOLDERS
    return False


def _check_member(info: zipfile.ZipInfo) -> None:
    """Refuse an encrypted member, an unknown compression, a link or device, and a member whose
    own header claims a ratio deflate can't reach."""
    if info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise BackupError(DAMAGED)
    if stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
        raise BackupError(UNSAFE)
    if (
        info.compress_type == zipfile.ZIP_DEFLATED
        and info.compress_size
        and info.file_size > info.compress_size * MAX_RATIO + 1024
    ):
        raise BackupError(UNSAFE)


def _read_limited(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, *, too_big: str = TOO_BIG_RESTORE
) -> bytes:
    """The member's bytes, read in chunks. BackupError (`too_big`) once they pass `limit`, so the
    size the zip header claims is never trusted."""
    parts: list[bytes] = []
    total = 0
    try:
        with archive.open(info) as member:
            while True:
                chunk = member.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise BackupError(too_big)
                parts.append(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError):
        raise BackupError(DAMAGED) from None
    return b"".join(parts)


# --- CSV exports --------------------------------------------------------------------------------

ACCOUNT_COLUMNS = (
    "Account",
    "Account id",
    "Health",
    "Priority",
    "Motion",
    "ARR",
    "Renewal date",
    "Owner",
    "Top reason",
)
NOTE_COLUMNS = ("Account", "Type", "Date", "Kind", "Subject", "Text", "Due", "Status")


def accounts_csv(book: Sequence[ScoredAccount]) -> str:
    """The scored book as CSV, in the order the book shows it. ARR is a plain number and dates are
    ISO (2026-11-11), so every spreadsheet reads them the same way."""
    rows: list[Sequence[str]] = [ACCOUNT_COLUMNS]
    for item in book:
        account = item.account
        rows.append(
            (
                account.name,
                account.id,
                item.health.label,
                _plain(item.priority, 1),
                item.motion.label,
                _plain(account.arr, 2),
                account.renewal_date.isoformat(),
                account.owner,
                item.top_reason,
            )
        )
    return _csv(rows)


def notes_csv(book: Sequence[ScoredAccount], journals: Mapping[str, journal.AccountJournal]) -> str:
    """Every note and follow-up of the accounts in the book, one row each, grouped by account in
    book order. A note's date is the day it happened; a follow-up's is the day it was added.
    Entries of accounts that have left the book are left out, as the screens leave them out."""
    rows: list[Sequence[str]] = [NOTE_COLUMNS]
    for item in book:
        entry = journals.get(item.account.id)
        if entry is None:
            continue
        name = item.account.name
        for note in entry.notes:
            kind = note.kind.label
            if note.direction is not None:
                kind = f"{kind} ({note.direction.value})"
            rows.append(
                (name, "Note", note.happened_on.isoformat(), kind, note.subject, note.text, "", "")
            )
        for follow_up in entry.follow_ups:
            status = "Done" if follow_up.status is journal.FollowUpStatus.DONE else "Open"
            rows.append(
                (
                    name,
                    "Follow-up",
                    follow_up.created_at.date().isoformat(),
                    "",
                    "",
                    follow_up.text,
                    follow_up.due.isoformat(),
                    status,
                )
            )
    return _csv(rows)


def _plain(value: float, places: int) -> str:
    """A whole number without a decimal point, otherwise the given number of places."""
    rounded = round(value, places)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{places}f}"


def _guard(cell: str) -> str:
    """A cell a spreadsheet would run as a formula ("=HYPERLINK(...)", "+1", "-2", "@SUM(...)")
    gets a leading apostrophe, so it stays text. Leading spaces don't hide a formula."""
    if cell[:1] in _FORMULA_STARTS or cell.lstrip(" ")[:1] in _FORMULA_STARTS:
        return "'" + cell
    return cell


def _csv(rows: Sequence[Sequence[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow([_guard(cell) for cell in row])
    return buffer.getvalue()
