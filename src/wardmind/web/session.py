"""The session gate: only the person who started Wardmind can open its pages.

127.0.0.1 is reachable from every Windows account signed in to the same computer, so the host
allow-list alone lets another account read the book. The gate closes that:

- Each run makes two random tokens: an entry token and a session token.
- The launcher (`wardmind ui` or the desktop window) opens `/enter?t=<entry token>`, which sets the
  session token as an HttpOnly, SameSite=Strict cookie and redirects to the page asked for.
- Every other request needs that cookie; without it the answer is 403. Static files are open, as
  they hold no data.
- The entry link for this run is kept in `session.json` in the per-user Wardmind folder, which
  other Windows accounts can't read, so `wardmind open` can open a new browser later.

Both tokens live only in this process and that file; a restart makes new ones. Comparisons use
`secrets.compare_digest`.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from wardmind.core.config import write_atomic
from wardmind.core.secrets import root_dir

SESSION_COOKIE = "wardmind_session"
ENTER_PATH = "/enter"
SESSION_FILE = "session.json"  # in root_dir(): the running app's entry link, for `wardmind open`
OPEN_PREFIXES = ("/static/",)  # served without the cookie: CSS, fonts, icons and htmx only
TOKEN_BYTES = 32

LOCKED_OUT_MESSAGE = (
    "This page needs Wardmind's sign-in link. Open Wardmind from its own window, or run "
    "`wardmind open` in a terminal on this computer."
)
BAD_ENTRY_MESSAGE = (
    "That sign-in link isn't valid for this run of Wardmind. Run `wardmind open` in a terminal on "
    "this computer to get a new one."
)


@dataclass(frozen=True)
class SessionGate:
    entry_token: str
    session_token: str

    @classmethod
    def new(cls) -> SessionGate:
        return cls(
            entry_token=secrets.token_urlsafe(TOKEN_BYTES),
            session_token=secrets.token_urlsafe(TOKEN_BYTES),
        )

    def entry_url(self, base_url: str, next_path: str = "/") -> str:
        """The sign-in link: base_url (e.g. "http://127.0.0.1:8000") + /enter with the token and
        where to go afterwards."""
        query = urlencode({"t": self.entry_token, "next": safe_next(next_path)})
        return f"{base_url.rstrip('/')}{ENTER_PATH}?{query}"

    def accepts_entry(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(str(token), self.entry_token)

    def accepts_cookie(self, value: str | None) -> bool:
        return bool(value) and secrets.compare_digest(str(value), self.session_token)


def is_open_path(path: str) -> bool:
    """Paths served without the session cookie: the sign-in link itself and static files."""
    return path == ENTER_PATH or path.startswith(OPEN_PREFIXES)


def safe_next(path: str | None) -> str:
    """Where to go after signing in: a path on this app, else "/". Anything with a scheme or a
    host, a protocol-relative "//", or a backslash (which some browsers read as "/") is "/"."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        return "/"
    return path


# --- The entry link on disk, for `wardmind open` ------------------------------------------


def session_path(folder: Path | None = None) -> Path:
    return (folder if folder is not None else root_dir()) / SESSION_FILE


def publish(gate: SessionGate, base_url: str, folder: Path | None = None) -> Path:
    """Write this run's base URL and entry token to session.json in the per-user folder."""
    path = session_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"url": base_url, "entry": gate.entry_token})
    write_atomic(path, body)
    return path


def published(folder: Path | None = None) -> tuple[str, str] | None:
    """(base URL, entry token) of the running app, or None when there is no readable file."""
    try:
        raw = json.loads(session_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    url, entry = raw.get("url"), raw.get("entry")
    if not isinstance(url, str) or not isinstance(entry, str) or not url or not entry:
        return None
    return url, entry


def withdraw(folder: Path | None = None) -> None:
    """Remove session.json (the app has stopped). Missing is fine."""
    with contextlib.suppress(OSError):
        session_path(folder).unlink(missing_ok=True)


def is_listening(base_url: str, timeout: float = 0.5) -> bool:
    """True when something accepts connections at base_url's host and port."""
    parts = urlsplit(base_url)
    if parts.hostname is None or parts.port is None:
        return False
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
            return True
    except OSError:
        return False


def open_link(folder: Path | None = None, next_path: str = "/") -> str | None:
    """The entry link for the running app, or None when none is running (a stale file is
    removed)."""
    found = published(folder)
    if found is None:
        return None
    base_url, entry = found
    if not is_listening(base_url):
        withdraw(folder)
        return None
    query = urlencode({"t": entry, "next": safe_next(next_path)})
    return f"{base_url.rstrip('/')}{ENTER_PATH}?{query}"
