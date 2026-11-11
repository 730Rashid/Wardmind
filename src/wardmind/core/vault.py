"""The vault: every data and secret file goes through here, encrypted when the user turns it on.

Plain is the default. Then the vault reads and writes files unchanged (writes are still atomic).
When the user turns encryption on (Settings > Data & privacy), every managed file is encrypted in
place and every later write is encrypted:

- `keys.json` (in the per-user Wardmind folder, plain JSON) holds a random 32-byte master key in
  "slots", each a different way to unwrap it: Windows (DPAPI, nothing to type), a recovery code
  shown once, and an optional password (Argon2id). It also holds one random key per book
  (core/books.py), each wrapped by the master key, so one password opens every book.
- Each file is AES-256-GCM under its own key: HKDF-SHA256(book key, b"wardmind file v1\\0" + its
  logical path, e.g. "data/journal.json"). The header and the logical path are the additional
  authenticated data, so a file swapped for another, or a changed byte, fails as `Damaged`.
- File format: b"WMD1", version (1 byte), algorithm (1 byte), key epoch (4 bytes, big-endian), a
  12-byte random nonce, then the ciphertext and its 16-byte tag.

Reads sniff the header: a file that starts with b"WMD1" is decrypted, anything else is plain. Writes
read the current state from keys.json (cached by its mtime and size), so a second process (the MCP
server) never writes plain text over an encrypted book.

Errors are `VaultError` subclasses, deliberately not OSError or ValueError, so the loaders that
treat an unreadable file as empty let them through: a locked or damaged book must never look
empty, or the next save would overwrite it.

The algorithms are public standards from the `cryptography` package; the protection lives in the
keys, never in this code (see docs/security-and-sharing.md).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"WMD1"
FORMAT_VERSION = 1
ALG_AES256GCM = 1
NONCE_BYTES = 12
TAG_BYTES = 16
HEADER_BYTES = len(MAGIC) + 1 + 1 + 4 + NONCE_BYTES  # 22
KEY_BYTES = 32

KEYS_FILE = "keys.json"
KEYS_BACKUP = "keys.json.bak"
KEYS_VERSION = 1
DEFAULT_BOOK = "default"

# The recovery code: 25 characters of Crockford base32 (125 bits), shown as five groups of five.
RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RECOVERY_LENGTH = 25
RECOVERY_GROUP = 5

REPLACE_TRIES = 20  # os.replace retries while another process has the file open (Windows)
REPLACE_WAIT = 0.05  # seconds between tries


class VaultError(Exception):
    """Base of the vault's errors. Never an OSError or ValueError (see the module docstring)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Locked(VaultError):
    """The book is encrypted and no key is unlocked in this process."""


class Damaged(VaultError):
    """An encrypted file (or keys.json) can't be read: changed, truncated or for another key."""


class State(StrEnum):
    PLAIN = "plain"  # nothing is encrypted; the vault passes files through
    ENCRYPTING = "encrypting"  # turning on: some files may still be plain
    ON = "on"  # every managed file is encrypted
    DECRYPTING = "decrypting"  # turning off: some files may still be encrypted


class SlotKind(StrEnum):
    DPAPI = "dpapi"
    RECOVERY = "recovery"
    PASSWORD = "password"


# --- Where the files are --------------------------------------------------------------------


def _root_dir() -> Path:
    """The per-user Wardmind folder: keys.json, its backup and the unlock ticket live here."""
    from wardmind.core.secrets import root_dir

    return root_dir()


def _book_ids() -> list[str]:
    from wardmind.core import books

    return books.all_ids()


def _book_roots(book_id: str) -> tuple[Path, Path]:
    """(data folder, secrets folder) of a book."""
    from wardmind.core import books

    return books.book_data_dir(book_id), books.book_folder(book_id)


def keys_path() -> Path:
    return _root_dir() / KEYS_FILE


def locate(path: Path) -> tuple[str, str]:
    """(book id, logical path) of a managed file. The logical path is the file's name inside its
    book, used in its key and its authenticated data: "data/<path under the data folder>" or
    "secrets/<path under the book's folder>" (POSIX slashes). Books in their own folders are
    checked before the first book, whose folder holds theirs. VaultError for a file outside every
    book (it can't be encrypted)."""
    from wardmind.core import books

    resolved = Path(os.path.abspath(path))
    ordered = sorted(_book_ids(), key=lambda book_id: book_id == books.DEFAULT_BOOK)
    for book_id in ordered:
        data, secrets_root = _book_roots(book_id)
        for tag, root in (("data", data), ("secrets", secrets_root)):
            base = Path(os.path.abspath(root))
            with contextlib.suppress(ValueError):
                relative = resolved.relative_to(base)
                if not relative.parts:
                    continue
                if book_id == books.DEFAULT_BOOK and tag == "secrets":
                    if relative.parts[0] == books.BOOKS_DIR:
                        break  # a folder of a book that isn't listed: not the first book's
                return book_id, f"{tag}/{relative.as_posix()}"
    raise VaultError(f"{path} isn't in a Wardmind book's data or secrets folder.")


def logical_path(path: Path) -> str:
    """The logical path of locate(path)."""
    return locate(path)[1]


SECRET_FILES = ("hubspot-token", "openrouter-key")  # in the secrets folder
SECRET_FOLDERS = ("feeds",)  # every file directly in these folders of the secrets folder


def managed_files() -> list[Path]:
    """Every file the vault encrypts, in every book: the data folder (recursively), the two key
    files and the named feed secrets. Dotfiles and *.tmp are skipped. Never a whole secrets
    folder: the first book's also holds the virtualenv, config, keys.json and session.json."""
    found: list[Path] = []
    for book_id in _book_ids():
        found.extend(_book_files(book_id))
    return found


def _book_files(book_id: str) -> list[Path]:
    found: list[Path] = []
    data, root = _book_roots(book_id)
    if data.is_dir():
        for path in sorted(data.rglob("*")):
            if path.is_file() and not _skipped(path):
                found.append(path)
    for name in SECRET_FILES:
        path = root / name
        if path.is_file():
            found.append(path)
    for folder in SECRET_FOLDERS:
        directory = root / folder
        if directory.is_dir():
            found.extend(
                path
                for path in sorted(directory.iterdir())
                if path.is_file() and not _skipped(path)
            )
    return found


def _skipped(path: Path) -> bool:
    return path.name.startswith(".") or path.suffix == ".tmp"


# --- DPAPI (Windows): wraps the master key for this Windows account --------------------------
# Two module functions so tests can replace them; on other systems they raise.

_DPAPI_ENTROPY = b"wardmind master key v1"


def dpapi_available() -> bool:
    return os.name == "nt"


def dpapi_protect(data: bytes) -> bytes:  # pragma: no cover - exercised on Windows only
    return _dpapi_call(data, protect=True)


def dpapi_unprotect(blob: bytes) -> bytes:  # pragma: no cover - exercised on Windows only
    return _dpapi_call(blob, protect=False)


def _dpapi_call(data: bytes, *, protect: bool) -> bytes:  # pragma: no cover - Windows only
    if os.name != "nt":
        raise VaultError("Windows protection isn't available on this system.")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob_of(raw: bytes) -> tuple[Blob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    source, _keep_source = blob_of(data)
    entropy, _keep_entropy = blob_of(_DPAPI_ENTROPY)
    result = Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = call(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(result)
    )
    if not ok:
        raise Damaged("Windows couldn't unlock Wardmind's key for this account.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


# --- keys.json --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    kind: SlotKind
    fields: dict[str, str | int]  # base64 strings and KDF parameters, as stored


@dataclass(frozen=True)
class KeyFile:
    state: State
    epoch: int
    slots: tuple[Slot, ...]
    books: dict[str, tuple[bytes, bytes]]  # book id -> (nonce, wrapped book key)

    def slot(self, kind: SlotKind) -> Slot | None:
        return next((slot for slot in self.slots if slot.kind is kind), None)

    def replace(
        self,
        *,
        state: State | None = None,
        slots: tuple[Slot, ...] | None = None,
        books: dict[str, tuple[bytes, bytes]] | None = None,
    ) -> KeyFile:
        return KeyFile(
            state if state is not None else self.state,
            self.epoch,
            slots if slots is not None else self.slots,
            books if books is not None else self.books,
        )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: object) -> bytes:
    if not isinstance(text, str):
        raise Damaged("Wardmind's key file is damaged.")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except ValueError as error:
        raise Damaged("Wardmind's key file is damaged.") from error


def _parse_keys(text: str) -> KeyFile:
    try:
        raw = json.loads(text)
        state = State(raw["state"])
        epoch = int(raw["epoch"])
        slots = tuple(
            Slot(SlotKind(item["kind"]), {k: v for k, v in item.items() if k != "kind"})
            for item in raw["slots"]
        )
        books = {
            str(book_id): (_unb64(entry["nonce"]), _unb64(entry["wrapped"]))
            for book_id, entry in raw["books"].items()
        }
        if DEFAULT_BOOK not in books:
            raise KeyError(DEFAULT_BOOK)
        return KeyFile(state, epoch, slots, books)
    except (KeyError, TypeError, ValueError) as error:
        raise Damaged("Wardmind's key file is damaged.") from error


def _dump_keys(keys: KeyFile) -> str:
    return json.dumps(
        {
            "version": KEYS_VERSION,
            "state": keys.state.value,
            "epoch": keys.epoch,
            "slots": [{"kind": slot.kind.value, **slot.fields} for slot in keys.slots],
            "books": {
                book_id: {"nonce": _b64(nonce), "wrapped": _b64(wrapped)}
                for book_id, (nonce, wrapped) in keys.books.items()
            },
        },
        indent=2,
    )


_keys_cache: tuple[tuple[int, int], KeyFile] | None = None
_keys_lock = threading.Lock()


def load_keys() -> KeyFile | None:
    """keys.json, or None when there is none (plain). Cached by the file's mtime and size, so a
    change made by another process is seen on the next call. Damaged when it can't be read."""
    global _keys_cache
    path = keys_path()
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    stamp = (info.st_mtime_ns, info.st_size)
    with _keys_lock:
        if _keys_cache is not None and _keys_cache[0] == stamp:
            return _keys_cache[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise Damaged("Wardmind's key file can't be read.") from error
    keys = _parse_keys(text)
    with _keys_lock:
        _keys_cache = (stamp, keys)
    return keys


def _save_keys(keys: KeyFile) -> None:
    global _keys_cache
    text = _dump_keys(keys)
    _write_raw(keys_path(), text.encode("utf-8"))
    _write_raw(_root_dir() / KEYS_BACKUP, text.encode("utf-8"))
    with _keys_lock:
        _keys_cache = None


def _delete_keys() -> None:
    global _keys_cache
    for name in (KEYS_FILE, KEYS_BACKUP):
        with contextlib.suppress(FileNotFoundError):
            (_root_dir() / name).unlink()
    with _keys_lock:
        _keys_cache = None


def destroy_keys() -> None:
    """Delete keys.json, its backup and the ticket, and forget the key: every encrypted file is
    unreadable from then on. Delete all local data calls this first, then deletes the files."""
    _delete_keys()
    delete_ticket()
    forget_key()


def state() -> State:
    keys = load_keys()
    return State.PLAIN if keys is None else keys.state


# --- Wrapping the master key -----------------------------------------------------------------


def _wrap(key: bytes, secret: bytes, label: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, secret, label)


def _unwrap(key: bytes, nonce: bytes, wrapped: bytes, label: bytes, wrong: str) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, wrapped, label)
    except InvalidTag as error:
        raise Locked(wrong) from error


def _hkdf(key: bytes, info: bytes, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=info).derive(key)


def new_recovery_code() -> str:
    """25 random characters from RECOVERY_ALPHABET (125 bits), as "XXXXX-XXXXX-XXXXX-...."."""
    return _formatted("".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH)))


def normalise_recovery_code(text: str) -> str:
    """The code's 25 characters, upper case, without spaces or dashes; Crockford's look-alikes
    read as their digits (O -> 0, I and L -> 1). VaultError when it isn't 25 valid characters."""
    cleaned = "".join(text.split()).replace("-", "").upper()
    cleaned = cleaned.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    if len(cleaned) != RECOVERY_LENGTH or any(c not in RECOVERY_ALPHABET for c in cleaned):
        raise Locked("That isn't a recovery key. It has 25 letters and digits in five groups.")
    return cleaned


def _recovery_key(code: str, salt: bytes) -> bytes:
    return _hkdf(normalise_recovery_code(code).encode("ascii"), b"wardmind recovery v1", salt)


def _dpapi_slot(master: bytes) -> Slot:
    return Slot(SlotKind.DPAPI, {"blob": _b64(dpapi_protect(master))})


def _recovery_slot(master: bytes, code: str) -> Slot:
    salt = os.urandom(16)
    nonce, wrapped = _wrap(_recovery_key(code, salt), master, b"wardmind slot recovery")
    return Slot(
        SlotKind.RECOVERY, {"salt": _b64(salt), "nonce": _b64(nonce), "wrapped": _b64(wrapped)}
    )


def _master_from(keys: KeyFile, *, password: str | None, recovery: str | None) -> bytes:
    if recovery is not None:
        slot = keys.slot(SlotKind.RECOVERY)
        if slot is None:
            raise Locked("This book has no recovery key.")
        key = _recovery_key(recovery, _unb64(slot.fields.get("salt")))
        return _unwrap(
            key,
            _unb64(slot.fields.get("nonce")),
            _unb64(slot.fields.get("wrapped")),
            b"wardmind slot recovery",
            "That recovery key doesn't open this book.",
        )
    if password is not None:
        return _master_from_password(keys, password)
    slot = keys.slot(SlotKind.DPAPI)
    if slot is None:
        raise Locked("Wardmind is locked. Unlock it with your password.")
    return dpapi_unprotect(_unb64(slot.fields.get("blob")))


def _master_from_password(keys: KeyFile, password: str) -> bytes:
    slot = keys.slot(SlotKind.PASSWORD)
    if slot is None:
        raise Locked("This book has no password. Unlock it with Windows or the recovery key.")
    try:
        memory = int(slot.fields["memory"])
        iterations = int(slot.fields["iterations"])
        lanes = int(slot.fields["lanes"])
    except (KeyError, TypeError, ValueError) as error:
        raise Damaged("Wardmind's key file is damaged.") from error
    key = _password_key(password, _unb64(slot.fields.get("salt")), memory, iterations, lanes)
    return _unwrap(
        key,
        _unb64(slot.fields.get("nonce")),
        _unb64(slot.fields.get("wrapped")),
        b"wardmind slot password",
        "That password isn't right.",
    )


# --- The password slot (Argon2id) ----------------------------------------------------------------

MIN_PASSWORD = 10
MAX_PASSWORD = 1024
# Argon2id cost. About 0.2 s here at 128 MiB; tests lower it through ARGON2.
ARGON2 = {"memory": 131072, "iterations": 3, "lanes": 4}


def check_password(password: str) -> str:
    """The password unchanged when it can be used. ValueError with a plain message otherwise:
    at least MIN_PASSWORD characters, at most MAX_PASSWORD, no line breaks or control characters.
    Spaces are allowed (a passphrase is a good password)."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters. A short phrase works well.")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Keep the password under {MAX_PASSWORD:,} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise ValueError("A password can't contain line breaks or control characters.")
    return password


def _password_key(password: str, salt: bytes, memory: int, iterations: int, lanes: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    return Argon2id(
        salt=salt, length=KEY_BYTES, iterations=iterations, lanes=lanes, memory_cost=memory
    ).derive(password.encode("utf-8"))


def _password_slot(master: bytes, password: str) -> Slot:
    salt = os.urandom(16)
    cost = dict(ARGON2)
    key = _password_key(password, salt, cost["memory"], cost["iterations"], cost["lanes"])
    nonce, wrapped = _wrap(key, master, b"wardmind slot password")
    return Slot(
        SlotKind.PASSWORD,
        {
            "kdf": "argon2id",
            **cost,
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "wrapped": _b64(wrapped),
        },
    )


def has_password() -> bool:
    keys = load_keys()
    return keys is not None and keys.slot(SlotKind.PASSWORD) is not None


def has_windows_unlock() -> bool:
    keys = load_keys()
    return keys is not None and keys.slot(SlotKind.DPAPI) is not None


# --- The unlocked key (this process only) ------------------------------------------------------


@dataclass
class _Session:
    master: bytes
    epoch: int
    book_keys: dict[str, bytes]  # unwrapped as each book is first used
    file_keys: dict[tuple[str, str], bytes]  # (book id, logical path) -> file key
    from_ticket: bool = False  # the MCP server or CLI, unlocked by the app's ticket


_session: _Session | None = None
_session_lock = threading.RLock()
_locked_by_user = False  # "Lock now": Windows protection must not unlock it again by itself


def is_unlocked() -> bool:
    return _session is not None


def _book_label(book_id: str) -> bytes:
    return b"wardmind book " + book_id.encode("ascii")


def _unwrap_book(keys: KeyFile, master: bytes, book_id: str) -> bytes:
    entry = keys.books.get(book_id)
    if entry is None:
        raise Damaged("Wardmind's key file has no key for this book.")
    return _unwrap(
        master, entry[0], entry[1], _book_label(book_id), "Wardmind's key file doesn't match."
    )


def _open_book(keys: KeyFile, master: bytes, *, from_ticket: bool = False) -> _Session:
    """A session for this master key. The first book's key is unwrapped at once, which checks
    the master key; the others as they are used."""
    first = _unwrap_book(keys, master, DEFAULT_BOOK)
    return _Session(
        master=master,
        epoch=keys.epoch,
        book_keys={DEFAULT_BOOK: first},
        file_keys={},
        from_ticket=from_ticket,
    )


def _book_key(session: _Session, book_id: str) -> bytes:
    key = session.book_keys.get(book_id)
    if key is None:
        keys = load_keys()
        if keys is None:
            raise Locked("Wardmind is locked.")
        key = _unwrap_book(keys, session.master, book_id)
        session.book_keys[book_id] = key
    return key


def unlock(*, password: str | None = None, recovery: str | None = None) -> None:
    """Unwrap the book key into memory. With no arguments, through Windows protection (DPAPI).
    Locked with a plain message when the password or recovery code is wrong; Damaged when
    keys.json can't be read. Nothing to do when the book is plain. With a password slot and
    Windows protection available, a ticket is written so the MCP server and the command line can
    read the book while the app is unlocked (deleted on lock)."""
    global _session, _locked_by_user
    keys = load_keys()
    if keys is None:
        return
    master = _master_from(keys, password=password, recovery=recovery)
    opened = _open_book(keys, master)
    with _session_lock:
        _session = opened
        _locked_by_user = False
    if keys.slot(SlotKind.PASSWORD) is not None:
        _write_ticket(master)


def lock() -> None:
    """Forget the key and delete the ticket. Windows protection won't unlock it again by itself
    in this process until unlock()."""
    global _session, _locked_by_user
    with _session_lock:
        _session = None
        _locked_by_user = True
    delete_ticket()


def forget_key() -> None:
    """Forget the key without marking the book locked by the user (tests, deleting all data)."""
    global _session, _locked_by_user
    with _session_lock:
        _session = None
        _locked_by_user = False


def ensure_unlocked() -> bool:
    """True when the book is plain or unlocked (unlocking through Windows protection or the
    app's ticket when that's allowed); False when it needs the password or recovery key.
    Damaged passes through."""
    if load_keys() is None:
        return True
    try:
        _active_session()
    except Locked:
        return False
    return True


def _active_session() -> _Session:
    """The unlocked key. Without one, and unless the user locked this process: Windows
    protection when the book has that slot, else the app's ticket. Locked otherwise. A session
    that came from a ticket ends when the ticket is gone (the app locked)."""
    global _session
    with _session_lock:
        if _session is not None:
            if _session.from_ticket and not _ticket_path().exists():
                _session = None
                raise Locked("Wardmind is locked. Unlock it in the app.")
            return _session
        if _locked_by_user:
            raise Locked("Wardmind is locked.")
        keys = load_keys()
        if keys is None:
            raise Locked("Wardmind is locked.")
        if keys.slot(SlotKind.DPAPI) is not None:
            unlock()
            assert _session is not None
            return _session
        master = _read_ticket()
        if master is None:
            raise Locked("Wardmind is locked. Unlock it in the app.")
        _session = _open_book(keys, master, from_ticket=True)
        return _session


# --- The unlock ticket: lets the MCP server read a password-protected book while the app is open

TICKET_FILE = "unlock-ticket.json"
TICKET_HOURS = 12  # a ticket left behind by a crash stops working after this long


def _ticket_path() -> Path:
    return _root_dir() / TICKET_FILE


def _write_ticket(master: bytes) -> None:
    if not dpapi_available():
        return
    try:
        blob = dpapi_protect(master)
    except VaultError:
        return
    body = json.dumps({"blob": _b64(blob), "until": time.time() + TICKET_HOURS * 3600})
    _write_raw(_ticket_path(), body.encode("utf-8"))


def _read_ticket() -> bytes | None:
    try:
        raw = json.loads(_ticket_path().read_text(encoding="utf-8"))
        if float(raw["until"]) < time.time():
            return None
        return dpapi_unprotect(_unb64(raw["blob"]))
    except (OSError, KeyError, TypeError, ValueError, VaultError):
        return None


def delete_ticket() -> None:
    with contextlib.suppress(OSError):
        _ticket_path().unlink(missing_ok=True)


# --- Changing the slots (needs the book unlocked) ---------------------------------------------


def _replace_slots(keep: Callable[[Slot], bool], add: list[Slot]) -> None:
    keys = load_keys()
    if keys is None:
        raise VaultError("Encryption is off.")
    slots = tuple(slot for slot in keys.slots if keep(slot)) + tuple(add)
    _save_keys(keys.replace(slots=slots))


def set_password(password: str) -> None:
    """Add or change the password. Windows protection is removed, so the password (or the
    recovery key) is needed after every lock. ValueError for a password check_password refuses;
    Locked when the book isn't unlocked."""
    check_password(password)
    with _migrate_lock:
        session = _active_session()
        slot = _password_slot(session.master, password)
        _replace_slots(lambda s: s.kind is SlotKind.RECOVERY, [slot])
    _write_ticket(session.master)


def remove_password() -> None:
    """Go back to Windows protection: unlocking needs nothing typed. VaultError where Windows
    protection isn't available; Locked when the book isn't unlocked."""
    if not dpapi_available():
        raise VaultError("Without a password, Wardmind needs Windows protection, which isn't here.")
    with _migrate_lock:
        session = _active_session()
        _replace_slots(lambda s: s.kind is SlotKind.RECOVERY, [_dpapi_slot(session.master)])
    delete_ticket()


def replace_recovery_code() -> str:
    """Make a new recovery key (the old one stops working) and return it to show once. Locked
    when the book isn't unlocked."""
    code = new_recovery_code()
    with _migrate_lock:
        session = _active_session()
        _replace_slots(
            lambda s: s.kind is not SlotKind.RECOVERY, [_recovery_slot(session.master, code)]
        )
    return code


def _file_key(session: _Session, book_id: str, logical: str) -> bytes:
    key = session.file_keys.get((book_id, logical))
    if key is None:
        book = _book_key(session, book_id)
        key = _hkdf(book, b"wardmind file v1\x00" + logical.encode("utf-8"))
        session.file_keys[(book_id, logical)] = key
    return key


# --- Encrypting one file ------------------------------------------------------------------------


def is_encrypted(raw: bytes) -> bool:
    return raw.startswith(MAGIC)


def encrypt(
    plain: bytes, logical: str, session: _Session | None = None, *, book: str = DEFAULT_BOOK
) -> bytes:
    use = session if session is not None else _active_session()
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + bytes([FORMAT_VERSION, ALG_AES256GCM]) + use.epoch.to_bytes(4, "big") + nonce
    sealed = AESGCM(_file_key(use, book, logical)).encrypt(nonce, plain, header + logical.encode())
    return header + sealed


def decrypt(
    raw: bytes, logical: str, session: _Session | None = None, *, book: str = DEFAULT_BOOK
) -> bytes:
    if len(raw) < HEADER_BYTES + TAG_BYTES:
        raise Damaged(f"{logical} is damaged: it's too short.")
    header = raw[:HEADER_BYTES]
    if header[4] != FORMAT_VERSION or header[5] != ALG_AES256GCM:
        raise Damaged(f"{logical} was written by a newer Wardmind.")
    use = session if session is not None else _active_session()
    nonce = header[10:HEADER_BYTES]
    try:
        return AESGCM(_file_key(use, book, logical)).decrypt(
            nonce, raw[HEADER_BYTES:], header + logical.encode()
        )
    except InvalidTag as error:
        raise Damaged(f"{logical} is damaged or belongs to another book.") from error


# --- The file API every data module uses --------------------------------------------------------


def read_bytes(path: Path) -> bytes:
    """The file's contents, decrypted when it is encrypted. FileNotFoundError and other OSErrors
    pass through unchanged; Locked or Damaged for an encrypted file that can't be read."""
    raw = path.read_bytes()
    if not is_encrypted(raw):
        return raw
    book_id, logical = locate(path)
    return decrypt(raw, logical, book=book_id)


def read_text(path: Path) -> str:
    """read_bytes decoded as UTF-8 (a UnicodeDecodeError passes through, as before)."""
    return read_bytes(path).decode("utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    """Replace the file atomically: encrypted while encryption is on (or turning on), else plain.
    On POSIX the file is 0600 and new folders 0700; on Windows the profile folder's ACL applies."""
    current = state()
    if current in (State.ON, State.ENCRYPTING):
        book_id, logical = locate(path)
        data = encrypt(data, logical, book=book_id)
    _write_raw(path, data)


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def move(old: Path, new: Path) -> None:
    """Rename a managed file. An encrypted file is decrypted and written again under its new
    name (its key and authenticated data depend on the name), then the old one is removed."""
    raw = old.read_bytes()
    if is_encrypted(raw):
        book_id, logical = locate(old)
        write_bytes(new, decrypt(raw, logical, book=book_id))
        old.unlink()
    else:
        _replace(old, new)


def _write_raw(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        _replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _replace(source: Path, target: Path) -> None:
    """os.replace, retried briefly while Windows says another process has the target open."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_WAIT)


# --- Turning encryption on and off ------------------------------------------------------------


_migrate_lock = threading.Lock()


def enable(
    *, code: str | None = None, password: str | None = None, use_dpapi: bool | None = None
) -> str:
    """Turn encryption on: make the keys, then encrypt every managed file in place. Returns the
    recovery code (`code` when the caller already showed one, else a new one). With a password,
    unlocking needs it (or the recovery key); without one, Windows protection unlocks it.
    VaultError when encryption is already on or turning on/off, or with no password where
    Windows protection isn't available; ValueError for a password check_password refuses."""
    global _session, _locked_by_user
    if password is not None:
        check_password(password)
    shown = _formatted(normalise_recovery_code(code)) if code is not None else new_recovery_code()
    with _migrate_lock:
        if load_keys() is not None:
            raise VaultError("Encryption is already on.")
        dpapi = dpapi_available() if use_dpapi is None else use_dpapi
        if password is None and not dpapi:
            raise VaultError("Choose a password: Windows protection isn't available here.")
        master = os.urandom(KEY_BYTES)
        book_keys = {book_id: os.urandom(KEY_BYTES) for book_id in [DEFAULT_BOOK, *_book_ids()]}
        wrapped_books = {
            book_id: _wrap(master, key, _book_label(book_id)) for book_id, key in book_keys.items()
        }
        unlock_slot = (
            _password_slot(master, password) if password is not None else _dpapi_slot(master)
        )
        keys = KeyFile(
            state=State.ENCRYPTING,
            epoch=1,
            slots=(unlock_slot, _recovery_slot(master, shown)),
            books=wrapped_books,
        )
        _save_keys(keys)
        with _session_lock:
            _session = _Session(master=master, epoch=1, book_keys=book_keys, file_keys={})
            _locked_by_user = False
        _encrypt_all()
        _save_keys(_with_state(keys, State.ON))
    if password is not None:
        _write_ticket(master)
    return shown


def _formatted(cleaned: str) -> str:
    """The 25 characters in five dash-separated groups."""
    groups = range(0, RECOVERY_LENGTH, RECOVERY_GROUP)
    return "-".join(cleaned[index : index + RECOVERY_GROUP] for index in groups)


def disable() -> None:
    """Turn encryption off: decrypt every managed file back to plain, check that none is still
    encrypted, then delete the keys. Locked when the book isn't unlocked."""
    with _migrate_lock:
        keys = load_keys()
        if keys is None:
            return
        _active_session()  # Locked unless unlocked (or unlockable through Windows protection)
        _save_keys(_with_state(keys, State.DECRYPTING))
        _decrypt_all()
        left = [path for path in managed_files() if is_encrypted(path.read_bytes())]
        if left:
            raise Damaged(
                f"{len(left)} files are still encrypted. Try turning encryption off again."
            )
        _delete_keys()
        delete_ticket()
        forget_key()


def resume() -> None:
    """Finish a turn-on or turn-off that was interrupted (a crash, a closed window). Safe to call
    at any time: it does nothing when the state is PLAIN or ON."""
    with _migrate_lock:
        keys = load_keys()
        if keys is None:
            return
        if keys.state is State.ENCRYPTING:
            _encrypt_all()
            _save_keys(_with_state(keys, State.ON))
        elif keys.state is State.DECRYPTING:
            _decrypt_all()
            _delete_keys()
            forget_key()


def _with_state(keys: KeyFile, new: State) -> KeyFile:
    return keys.replace(state=new)


def _encrypt_all() -> None:
    session = _active_session()
    for path in managed_files():
        _rewrite(
            path,
            lambda raw, book_id, logical: (
                raw if is_encrypted(raw) else encrypt(raw, logical, session, book=book_id)
            ),
        )


def _decrypt_all() -> None:
    session = _active_session()
    for path in managed_files():
        _rewrite(
            path,
            lambda raw, book_id, logical: (
                decrypt(raw, logical, session, book=book_id) if is_encrypted(raw) else raw
            ),
        )


def _rewrite(path: Path, change: Callable[[bytes, str, str], bytes]) -> None:
    """Replace the file with change(its bytes, its book, its logical path), atomically, keeping
    its modified time (the "saved on" dates of keys and tokens come from it). Unchanged files
    are left alone."""
    raw = path.read_bytes()
    updated = change(raw, *locate(path))
    if updated == raw:
        return
    info = path.stat()
    _write_raw(path, updated)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))


# --- Books (core/books.py): a key for each -----------------------------------------------------


def add_book_key(book_id: str) -> None:
    """Give a new book its own key, wrapped by the master key. Nothing to do while encryption is
    off. Locked when Wardmind isn't unlocked; VaultError while encryption is being turned on or
    off."""
    with _migrate_lock:
        keys = load_keys()
        if keys is None or book_id in keys.books:
            return
        if keys.state is not State.ON:
            raise VaultError("Wait until encryption has finished turning on or off.")
        session = _active_session()
        key = os.urandom(KEY_BYTES)
        books = {**keys.books, book_id: _wrap(session.master, key, _book_label(book_id))}
        _save_keys(keys.replace(books=books))
        with _session_lock:
            session.book_keys[book_id] = key


def remove_book_key(book_id: str) -> None:
    """Forget a deleted book's key, so whatever is left of its files can't be read. The first
    book's key is never removed (VaultError)."""
    if book_id == DEFAULT_BOOK:
        raise VaultError("The first book's key can't be removed.")
    with _migrate_lock:
        keys = load_keys()
        if keys is not None and book_id in keys.books:
            books = {key: value for key, value in keys.books.items() if key != book_id}
            _save_keys(keys.replace(books=books))
        with _session_lock:
            if _session is not None:
                _session.book_keys.pop(book_id, None)
                for cached in [item for item in _session.file_keys if item[0] == book_id]:
                    del _session.file_keys[cached]
