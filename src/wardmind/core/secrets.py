"""Where the HubSpot access token and the OpenRouter API key live, and how they are shown
(never in full).

Lookup order: the WARDMIND_HUBSPOT_TOKEN environment variable, then a file in a per-user,
local-only folder. That folder is never the project's config/ (which sits in a synced OneDrive
folder and in git): on Windows it is %LOCALAPPDATA%\\wardmind, elsewhere
$XDG_STATE_HOME/wardmind or ~/.local/state/wardmind. WARDMIND_SECRETS_DIR overrides it (tests).

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them. Nothing here may log, print or raise with the token in the message.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from wardmind.core import vault

ENV_TOKEN = "WARDMIND_HUBSPOT_TOKEN"
ENV_SECRETS_DIR = "WARDMIND_SECRETS_DIR"
TOKEN_FILE = "hubspot-token"
MIN_TOKEN_LENGTH = 20
MAX_TOKEN_LENGTH = 1024
# The leading segment shown by mask_token ("pat-na1") is short. A longer one, such as the first
# part of a UUID-shaped legacy token, is treated as secret and left out.
_MAX_PREFIX_LENGTH = 12


class TokenSource(StrEnum):
    ENV = "env"  # set by the environment variable; the UI can't remove it
    FILE = "file"  # saved from the Connections page
    NONE = "none"


class TokenStatus(BaseModel):
    source: TokenSource
    masked: str | None  # mask_token(token), or None when there is no token
    saved_at: datetime | None  # file modification time for FILE, else None


def secrets_dir() -> Path:
    """The open book's secrets folder (core/books.py): root_dir() for the first book, else
    root_dir()/books/<id>. Not created here."""
    from wardmind.core import books

    return books.book_folder(books.active_id())


def root_dir() -> Path:
    """The per-user, local-only folder described in the module docstring (not created here).
    It holds books.json, keys.json and session.json, and is the first book's folder."""
    override = os.environ.get(ENV_SECRETS_DIR, "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "wardmind"
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "wardmind"


def mask_token(token: str) -> str:
    """Show only what helps someone recognise their token: the last 4 characters, plus the
    leading segment up to the second hyphen when the token has that shape.

    "pat-na1-0123abcd-…-9f3e" gives "pat-na1-…9f3e"; "abcdef0123456789xyz1" gives "…xyz1".
    Tokens shorter than MIN_TOKEN_LENGTH give "…" only.
    """
    if len(token) < MIN_TOKEN_LENGTH:
        return "…"
    tail = token[-4:]
    first = token.find("-")
    second = token.find("-", first + 1) if first != -1 else -1
    if 0 < second <= _MAX_PREFIX_LENGTH:
        return f"{token[:second]}-…{tail}"
    return f"…{tail}"


def _mask_tail(secret: str) -> str:
    """Only the last 4 characters of a secret, for keys whose leading characters are a public
    prefix (the OpenRouter key shows nothing before the dots)."""
    if len(secret) < MIN_TOKEN_LENGTH:
        return "…"
    return f"…{secret[-4:]}"


# --- Shared file helpers: the HubSpot token and the OpenRouter key both use these ----------


def _env_value(name: str) -> str | None:
    """The environment variable's value, stripped, or None when it is unset or blank."""
    value = os.environ.get(name, "").strip()
    return value or None


def _secret_path(file_name: str) -> Path:
    return secrets_dir() / file_name


def _read_secret_file(file_name: str) -> str | None:
    """The saved secret, stripped, or None when there is no file, it can't be read or it's blank."""
    try:
        text = vault.read_text(_secret_path(file_name)).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _write_secret_file(file_name: str, value: str) -> None:
    """Write `value` atomically to secrets_dir()/file_name, creating the folder. On POSIX the file
    is chmod 600; on Windows the per-user profile folder's ACL applies."""
    _write_in(secrets_dir(), file_name, value)


def _write_in(folder: Path, file_name: str, value: str) -> None:
    """_write_secret_file for any folder (the named feed secrets live in a subfolder): atomic,
    and encrypted when encryption is on (core/vault.py)."""
    folder.mkdir(parents=True, exist_ok=True)
    vault.write_text(folder / file_name, value)


def _delete_secret_file(file_name: str) -> bool:
    """Remove the file. True if a file was removed."""
    try:
        _secret_path(file_name).unlink()
    except FileNotFoundError:
        return False
    return True


def _status(env_name: str, file_name: str, mask: Callable[[str], str]) -> TokenStatus:
    """Where a secret comes from (the environment wins), and how it is shown."""
    env = _env_value(env_name)
    if env is not None:
        return TokenStatus(source=TokenSource.ENV, masked=mask(env), saved_at=None)
    value = _read_secret_file(file_name)
    if value is None:
        return TokenStatus(source=TokenSource.NONE, masked=None, saved_at=None)
    saved_at = datetime.fromtimestamp(_secret_path(file_name).stat().st_mtime)
    return TokenStatus(
        source=TokenSource.FILE,
        masked=mask(value),
        saved_at=saved_at,
    )


def _checked(value: str, *, blank: str, short: str, long: str, spaces: str | None) -> str:
    """The stripped value, or ValueError with the given plain-English message. No message ever
    contains the value."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(blank)
    if spaces is not None and any(char.isspace() for char in stripped):
        raise ValueError(spaces)
    if len(stripped) < MIN_TOKEN_LENGTH:
        raise ValueError(short)
    if len(stripped) > MAX_TOKEN_LENGTH:
        raise ValueError(long)
    return stripped


# --- HubSpot access token ------------------------------------------------------------------


def _env_token() -> str | None:
    """The environment variable's token, stripped, or None when it is unset or blank."""
    return _env_value(ENV_TOKEN)


def _file_token() -> str | None:
    """The saved token, stripped, or None when there is no file, it can't be read, or it's blank."""
    return _read_secret_file(TOKEN_FILE)


def get_hubspot_token() -> str | None:
    """The token from ENV_TOKEN if set and non-blank, else from the token file, else None."""
    return _env_token() or _file_token()


def token_status() -> TokenStatus:
    return _status(ENV_TOKEN, TOKEN_FILE, mask_token)


def validate_token(token: str) -> str:
    """Return the stripped token, or raise ValueError with a plain-English message (which never
    echoes the token) when it is blank, contains whitespace, or is outside the length limits."""
    return _checked(
        token,
        blank="Paste an access token first.",
        short="That doesn't look like a full access token. Check you copied all of it.",
        long="That's longer than any HubSpot access token. Check you copied only the token.",
        spaces="An access token can't contain spaces or line breaks.",
    )


def save_hubspot_token(token: str) -> None:
    """validate_token, then write atomically to secrets_dir()/TOKEN_FILE, creating the folder.
    On POSIX the file is chmod 600; on Windows the per-user profile folder's ACL applies."""
    _write_secret_file(TOKEN_FILE, validate_token(token))


def delete_hubspot_token() -> bool:
    """Remove the token file. True if a file was removed. The environment variable is untouched."""
    return _delete_secret_file(TOKEN_FILE)


# --- OpenRouter API key (AI summaries; see core/summaries.py) ------------------------------
# Same rules as the HubSpot token: the environment variable wins, else a file in secrets_dir();
# shown only through a mask; never logged, printed or put in an error message. Only the last 4
# characters are shown, because an OpenRouter key has no useful leading segment.

ENV_OPENROUTER_KEY = "WARDMIND_OPENROUTER_KEY"
OPENROUTER_KEY_FILE = "openrouter-key"


def get_openrouter_key() -> str | None:
    """The key from ENV_OPENROUTER_KEY if set and non-blank, else from the key file, else None."""
    return _env_value(ENV_OPENROUTER_KEY) or _read_secret_file(OPENROUTER_KEY_FILE)


def openrouter_key_status() -> TokenStatus:
    """Like token_status(), for the OpenRouter key."""
    return _status(ENV_OPENROUTER_KEY, OPENROUTER_KEY_FILE, _mask_tail)


def validate_openrouter_key(key: str) -> str:
    """Like validate_token (same length limits and messages' style, never echoing the key), with
    OpenRouter wording: e.g. "Paste an OpenRouter API key first."."""
    return _checked(
        key,
        blank="Paste an OpenRouter API key first.",
        short="That doesn't look like a full OpenRouter API key. Check you copied all of it.",
        long="That's longer than any OpenRouter API key. Check you copied only the key.",
        spaces="An OpenRouter API key can't contain spaces or line breaks.",
    )


def save_openrouter_key(key: str) -> None:
    """validate_openrouter_key, then write atomically to secrets_dir()/OPENROUTER_KEY_FILE, the
    same way save_hubspot_token does (share the helper rather than copying it)."""
    _write_secret_file(OPENROUTER_KEY_FILE, validate_openrouter_key(key))


def delete_openrouter_key() -> bool:
    """Remove the key file. True if a file was removed. The environment variable is untouched."""
    return _delete_secret_file(OPENROUTER_KEY_FILE)


# --- Named secrets (each data feed's key, token or password; see core/feeds) ---------------
# Stored one per file in secrets_dir() / FEED_SECRETS_DIR / <name>, written like the HubSpot
# token (atomic, chmod 600 on POSIX), never logged, printed or put in an error message, and
# shown only through mask_token. A name is lowercase letters, digits and hyphens, e.g. "feed-3"
# or "draft-<id>" while a feed is being set up.

FEED_SECRETS_DIR = "feeds"
MAX_SECRET_LENGTH = 4096
_SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# Characters that would break a one-line value. NUL is refused too.
_LINE_BREAKS = frozenset("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\x00")
_NAME_MESSAGE = "That secret name isn't valid."


def _feed_folder() -> Path:
    return secrets_dir() / FEED_SECRETS_DIR


def delete_all_feed_secrets() -> None:
    """Remove every named feed secret (each feed's key, and any draft's). Only Clear local data
    calls this: HubSpot's token and the OpenRouter key are separate files and are kept."""
    shutil.rmtree(_feed_folder(), ignore_errors=True)


def _named_path(name: str) -> Path | None:
    """The file for a secret name, or None when the name isn't one (so no path is built from it)."""
    if not isinstance(name, str) or _SECRET_NAME.fullmatch(name) is None:
        return None
    return _feed_folder() / name


def validate_secret(value: str) -> str:
    """The value stripped at both ends, or ValueError with a plain message (never the value):
    blank, longer than MAX_SECRET_LENGTH, or with a line break. Spaces inside are allowed."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("Type the key, token or password first.")
    if len(stripped) > MAX_SECRET_LENGTH:
        raise ValueError("That's longer than a key or password can be. Check you copied only it.")
    if any(char in _LINE_BREAKS for char in stripped):
        raise ValueError("A key or password can't contain a line break.")
    return stripped


def get_secret(name: str) -> str | None:
    """The saved secret, or None when there is none or the name isn't valid."""
    path = _named_path(name)
    if path is None:
        return None
    try:
        text = vault.read_text(path).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def secret_status(name: str) -> TokenStatus:
    """FILE with the masked value and saved time, or NONE."""
    value = get_secret(name)
    path = _named_path(name)
    if value is None or path is None:
        return TokenStatus(source=TokenSource.NONE, masked=None, saved_at=None)
    return TokenStatus(
        source=TokenSource.FILE,
        masked=mask_token(value),
        saved_at=datetime.fromtimestamp(path.stat().st_mtime),
    )


def save_secret(name: str, value: str) -> None:
    """Save after stripping the ends. Raises ValueError (never echoing the value) when the name
    isn't valid, the value is blank, has a line break, or is longer than MAX_SECRET_LENGTH.
    Spaces inside are allowed: a password may have them."""
    if _named_path(name) is None:
        raise ValueError(_NAME_MESSAGE)
    _write_in(_feed_folder(), name, validate_secret(value))


def delete_secret(name: str) -> bool:
    """Remove it. True if a file was removed."""
    path = _named_path(name)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def rename_secret(old: str, new: str) -> bool:
    """Move a secret to a new name (a draft's key becoming the feed's). True if one was moved;
    an existing secret under `new` is replaced. Raises ValueError when either name isn't valid."""
    old_path = _named_path(old)
    new_path = _named_path(new)
    if old_path is None or new_path is None:
        raise ValueError(_NAME_MESSAGE)
    if not old_path.is_file():
        return False
    vault.move(old_path, new_path)
    return True
