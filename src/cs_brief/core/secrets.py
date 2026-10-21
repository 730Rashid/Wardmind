"""Where the HubSpot access token lives, and how it is shown (never in full).

Lookup order: the CS_BRIEF_HUBSPOT_TOKEN environment variable, then a file in a per-user,
local-only folder. That folder is never the project's config/ (which sits in a synced OneDrive
folder and in git): on Windows it is %LOCALAPPDATA%\\cs-brief, elsewhere
$XDG_STATE_HOME/cs-brief or ~/.local/state/cs-brief. CS_BRIEF_SECRETS_DIR overrides it (tests).

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them. Nothing here may log, print or raise with the token in the message.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

ENV_TOKEN = "CS_BRIEF_HUBSPOT_TOKEN"
ENV_SECRETS_DIR = "CS_BRIEF_SECRETS_DIR"
TOKEN_FILE = "hubspot-token"
MIN_TOKEN_LENGTH = 20
MAX_TOKEN_LENGTH = 1024


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
    raise NotImplementedError


def mask_token(token: str) -> str:
    """Show only what helps someone recognise their token: the last 4 characters, plus the
    leading segment up to the second hyphen when the token has that shape.

    "pat-na1-0123abcd-…-9f3e" gives "pat-na1-…9f3e"; "abcdef0123456789xyz1" gives "…xyz1".
    Tokens shorter than MIN_TOKEN_LENGTH give "…" only.
    """
    raise NotImplementedError


def get_hubspot_token() -> str | None:
    """The token from ENV_TOKEN if set and non-blank, else from the token file, else None."""
    raise NotImplementedError


def token_status() -> TokenStatus:
    raise NotImplementedError


def validate_token(token: str) -> str:
    """Return the stripped token, or raise ValueError with a plain-English message (which never
    echoes the token) when it is blank, contains whitespace, or is outside the length limits."""
    raise NotImplementedError


def save_hubspot_token(token: str) -> None:
    """validate_token, then write atomically to secrets_dir()/TOKEN_FILE, creating the folder.
    On POSIX the file is chmod 600; on Windows the per-user profile folder's ACL applies."""
    raise NotImplementedError


def delete_hubspot_token() -> bool:
    """Remove the token file. True if a file was removed. The environment variable is untouched."""
    raise NotImplementedError
