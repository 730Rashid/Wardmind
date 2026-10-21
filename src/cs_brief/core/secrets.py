"""Where the HubSpot access token lives, and how it is shown (never in full).

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


def _env_token() -> str | None:
    """The environment variable's token, stripped, or None when it is unset or blank."""
    value = os.environ.get(ENV_TOKEN, "").strip()
    return value or None


def _token_path() -> Path:
    return secrets_dir() / TOKEN_FILE


def _file_token() -> str | None:
    """The saved token, stripped, or None when there is no file, it can't be read, or it's blank."""
    try:
        text = _token_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def get_hubspot_token() -> str | None:
    """The token from ENV_TOKEN if set and non-blank, else from the token file, else None."""
    return _env_token() or _file_token()


def token_status() -> TokenStatus:
    env = _env_token()
    if env is not None:
        return TokenStatus(source=TokenSource.ENV, masked=mask_token(env), saved_at=None)
    token = _file_token()
    if token is None:
        return TokenStatus(source=TokenSource.NONE, masked=None, saved_at=None)
    saved_at = datetime.fromtimestamp(_token_path().stat().st_mtime)
    return TokenStatus(source=TokenSource.FILE, masked=mask_token(token), saved_at=saved_at)


def validate_token(token: str) -> str:
    """Return the stripped token, or raise ValueError with a plain-English message (which never
    echoes the token) when it is blank, contains whitespace, or is outside the length limits."""
    stripped = token.strip()
    if not stripped:
        raise ValueError("Paste an access token first.")
    if any(char.isspace() for char in stripped):
        raise ValueError("An access token can't contain spaces or line breaks.")
    if len(stripped) < MIN_TOKEN_LENGTH:
        raise ValueError("That doesn't look like a full access token. Check you copied all of it.")
    if len(stripped) > MAX_TOKEN_LENGTH:
        raise ValueError(
            "That's longer than any HubSpot access token. Check you copied only the token."
        )
    return stripped


def save_hubspot_token(token: str) -> None:
    """validate_token, then write atomically to secrets_dir()/TOKEN_FILE, creating the folder.
    On POSIX the file is chmod 600; on Windows the per-user profile folder's ACL applies."""
    value = validate_token(token)
    folder = secrets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f".{TOKEN_FILE}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        if os.name == "posix":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, folder / TOKEN_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def delete_hubspot_token() -> bool:
    """Remove the token file. True if a file was removed. The environment variable is untouched."""
    try:
        _token_path().unlink()
    except FileNotFoundError:
        return False
    return True
