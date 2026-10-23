"""Where the HubSpot access token and the OpenRouter API key live, and how they are shown
(never in full).

Lookup order: the CS_BRIEF_HUBSPOT_TOKEN environment variable, then a file in a per-user,
local-only folder. That folder is never the project's config/ (which sits in a synced OneDrive
folder and in git): on Windows it is %LOCALAPPDATA%\\cs-brief, elsewhere
$XDG_STATE_HOME/cs-brief or ~/.local/state/cs-brief. CS_BRIEF_SECRETS_DIR overrides it (tests).

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them. Nothing here may log, print or raise with the token in the message.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

ENV_TOKEN = "CS_BRIEF_HUBSPOT_TOKEN"
ENV_SECRETS_DIR = "CS_BRIEF_SECRETS_DIR"
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
    """The per-user, local-only folder described in the module docstring (not created here)."""
    override = os.environ.get(ENV_SECRETS_DIR, "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "cs-brief"
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "cs-brief"


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
        text = _secret_path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _write_secret_file(file_name: str, value: str) -> None:
    """Write `value` atomically to secrets_dir()/file_name, creating the folder. On POSIX the file
    is chmod 600; on Windows the per-user profile folder's ACL applies."""
    folder = secrets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f".{file_name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        if os.name == "posix":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, folder / file_name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


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

ENV_OPENROUTER_KEY = "CS_BRIEF_OPENROUTER_KEY"
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
