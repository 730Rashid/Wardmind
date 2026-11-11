"""The mailbox connection: emails to and from clients logged into each account's Activity history.

The CSM connects their own mailbox once (Settings > Mailbox) with an IMAP app password. Every
SYNC_MINUTES the scheduler reads the Inbox and the Sent folder, read-only, and logs each email
whose other side is a client (by the account's website domain, or a contact's address) into that
account's journal (journal.Journal.log_email, which logs a Message-ID only once).

Privacy and safety rules (keep them):
- Read-only: folders are SELECTed with readonly=True and messages fetched with BODY.PEEK, so
  nothing is marked read, moved or deleted.
- Only emails that match an account are kept, and of those only the subject, the day, who sent
  it, the client-side addresses and the first SNIPPET_CHARS characters of the plain-text body.
  Attachments and HTML are never stored. Nothing else from the mailbox is written anywhere.
- TLS only (IMAP over SSL, port 993 by default) with certificate checks. The host must resolve to
  public addresses only (research.check_public_host), and the connection goes to the checked
  address with the certificate checked against the host name, so a DNS change between the check
  and the connection can't redirect it.
- The password is a named secret (secrets.save_secret(SECRET_NAME, ...)), never in mailbox.json,
  never logged, never in an error message, a URL or a page.
- Only Your data: demo data is never matched or written.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
import socket
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from enum import StrEnum
from html.parser import HTMLParser
from typing import NamedTuple

from pydantic import BaseModel, Field

from wardmind.core import research, secrets
from wardmind.core.config import write_atomic
from wardmind.core.contacts import Contact
from wardmind.core.journal import (
    Direction,
    Journal,
    LoggedEmail,
    TeamMember,
    clean_team_email,
)
from wardmind.core.matching import normalize_domain
from wardmind.core.models import Account
from wardmind.core.store import MAILBOX_FILE, WRITE_LOCK, LocalStore

SECRET_NAME = "mailbox-password"
SYNC_MINUTES = 15  # how often the scheduler syncs an enabled mailbox
FIRST_SYNC_DAYS = 30  # the first sync looks back this far
OVERLAP_DAYS = 2  # later syncs start this many days before the last one (late arrivals)
MAX_MESSAGES_PER_SYNC = 2000  # newest first; the rest wait for the next sync
FETCH_BYTES = 65_536  # bytes fetched per message (headers and the start of the body)
SNIPPET_CHARS = 1500  # characters of plain-text body kept per email
TIMEOUT_SECONDS = 30
MAX_MATCHED_ACCOUNTS = 3  # an email matching more accounts than this is skipped (a newsletter)

# Domains anyone can sign up to: an address there matches an account only through its contacts,
# never by domain.
FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.de",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "fastmail.com",
    }
)


class Provider(StrEnum):
    GMAIL = "gmail"
    OUTLOOK = "outlook"
    ICLOUD = "icloud"
    YAHOO = "yahoo"
    FASTMAIL = "fastmail"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "gmail": "Gmail or Google Workspace",
            "outlook": "Outlook.com or Microsoft 365",
            "icloud": "iCloud Mail",
            "yahoo": "Yahoo Mail",
            "fastmail": "Fastmail",
            "other": "Another provider",
        }[self.value]


# Host for each provider (OTHER: the CSM types it). All use port 993.
PROVIDER_HOSTS: dict[Provider, str] = {
    Provider.GMAIL: "imap.gmail.com",
    Provider.OUTLOOK: "outlook.office365.com",
    Provider.ICLOUD: "imap.mail.me.com",
    Provider.YAHOO: "imap.mail.yahoo.com",
    Provider.FASTMAIL: "imap.fastmail.com",
}


class SyncCounts(BaseModel):
    read: int = 0  # messages looked at
    logged: int = 0  # new activity entries written
    already: int = 0  # matched, but that account already had the Message-ID
    unmatched: int = 0  # no account matched (nothing kept)


class MailboxSettings(BaseModel):
    """mailbox.json under store.root (store.MAILBOX_FILE). No password here."""

    provider: Provider
    host: str  # PROVIDER_HOSTS[provider] unless OTHER
    port: int = Field(default=993, ge=1, le=65535)
    username: str  # usually the email address; lower-cased
    own_addresses: list[str]  # the CSM's addresses (username first, then aliases), lower-cased
    enabled: bool = True
    connected_at: datetime  # naive UTC
    last_sync_at: datetime | None = None  # naive UTC, set after every attempt
    last_success_at: datetime | None = None  # naive UTC, after a sync that completed
    last_error: str | None = None  # plain English from the last failed attempt
    last_counts: SyncCounts | None = None  # from the last completed sync


class MailboxError(Exception):
    """A connection or sync that failed. `message` is plain English for the page and never
    contains the password."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Address(BaseModel):
    email: str  # lower-cased
    name: str = ""  # the display name, decoded; may be empty


class MailMessage(BaseModel):
    message_id: str  # without < >; a message without one is skipped by parse_message
    subject: str  # decoded (RFC 2047), may be empty
    sent_at: datetime  # from the Date header, as naive local time; a bad Date gives None (skip)
    sender: Address
    recipients: list[Address]  # To and Cc, in order, without duplicates
    snippet: str  # the first SNIPPET_CHARS of the text/plain part (else of text/html with tags
    # removed); quoted reply lines (starting ">") and everything from a line like "On ... wrote:"
    # onwards are cut, so the snippet is what this email itself said


# --- Settings ---------------------------------------------------------------------------------


def load_settings(store: LocalStore) -> MailboxSettings | None:
    """The saved settings, or None when no mailbox is connected (or the file is unreadable)."""
    try:
        text = (store.root / MAILBOX_FILE).read_text(encoding="utf-8")
        return MailboxSettings.model_validate_json(text)
    except (OSError, ValueError):  # ValidationError and JSON errors are ValueErrors
        return None


def save_settings(store: LocalStore, settings: MailboxSettings) -> None:
    """Write mailbox.json atomically under store.WRITE_LOCK."""
    with WRITE_LOCK:
        write_atomic(store.root / MAILBOX_FILE, settings.model_dump_json(indent=2) + "\n")


def disconnect(store: LocalStore) -> None:
    """Remove mailbox.json and the password secret. The activity already logged stays."""
    with WRITE_LOCK:
        (store.root / MAILBOX_FILE).unlink(missing_ok=True)
        secrets.delete_secret(SECRET_NAME)


def clean_username(text: str) -> str:
    """Stripped and lower-cased. ValueError("Type the email address you sign in with.") when
    blank; same address rules as journal.clean_team_email otherwise."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Type the email address you sign in with.")
    cleaned = clean_team_email(stripped)
    if cleaned is None:  # not reachable: the text isn't blank
        raise ValueError("Type the email address you sign in with.")
    return cleaned


_HOST_SHAPE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_MAX_HOST = 253


def clean_host(text: str) -> str:
    """A host name: stripped, lower-cased, letters, digits, dots and hyphens only, at least one
    dot, at most 253 characters, no scheme, port or path. ValueError("Type the server's name, like
    imap.example.com.") otherwise."""
    host = text.strip().lower()
    if len(host) > _MAX_HOST or _HOST_SHAPE.fullmatch(host) is None:
        raise ValueError("Type the server's name, like imap.example.com.")
    return host


MAX_ALIASES = 10


def clean_aliases(text: str) -> list[str]:
    """Other addresses of yours, one per line or comma-separated: each checked with
    clean_team_email, duplicates dropped, at most 10. ValueError naming the first bad one:
    "“x” isn't an email address." """
    found: list[str] = []
    for part in re.split(r"[\n,]", text):
        raw = part.strip()
        if not raw:
            continue
        try:
            cleaned = clean_team_email(raw)
        except ValueError:
            cleaned = None
        if cleaned is None:
            raise ValueError(f"“{raw}” isn't an email address.")
        if cleaned not in found:
            found.append(cleaned)
    if len(found) > MAX_ALIASES:
        raise ValueError(f"Keep it to {MAX_ALIASES} other addresses.")
    return found


# --- Parsing and matching (pure) --------------------------------------------------------------


def parse_message(raw: bytes) -> MailMessage | None:
    """Parse the fetched bytes (possibly cut at FETCH_BYTES) with the stdlib email package
    (policy=email.policy.default). None when there is no Message-ID, no usable From, or no
    parsable Date. Never raises."""
    try:
        return _parse(raw)
    except Exception:  # garbage bytes, a broken header or a body the parser can't read
        return None


def match_accounts(
    message: MailMessage,
    accounts: list[Account],
    contacts: dict[str, list[Contact]],
    own_addresses: set[str],
    teams: dict[str, list[TeamMember]],
) -> list[str]:
    """Account ids this email belongs to, in `accounts` order.

    The client side is every address in sender + recipients that is not one of own_addresses,
    not a team member's email (any account's team), and not on the same domain as any of
    own_addresses (colleagues), unless that domain is a FREE_MAIL_DOMAINS one. An account matches
    when a client-side address equals one of its contacts' emails, or (outside FREE_MAIL_DOMAINS)
    the address's domain equals account.domain or ends with "." + account.domain. account.domain
    is compared lower-cased, without a leading "www.". More than MAX_MATCHED_ACCOUNTS matches
    gives []."""
    own = {address.lower() for address in own_addresses}
    client = _client_side(message, own, _team_emails(teams))
    if not client:
        return []
    matched: list[str] = []
    for account in accounts:
        domain, contact_emails = _target(account, contacts.get(account.id, []))
        if any(_covers(address, domain, contact_emails) for address in client):
            matched.append(account.id)
    if len(matched) > MAX_MATCHED_ACCOUNTS:
        return []
    return matched


def direction_of(message: MailMessage, own_addresses: set[str], team_emails: set[str]) -> Direction:
    """SENT when the sender is one of own_addresses or a team member's email, else RECEIVED."""
    sender = message.sender.email
    own = {address.lower() for address in own_addresses}
    if sender in own or sender in {address.lower() for address in team_emails}:
        return Direction.SENT
    return Direction.RECEIVED


def sender_label(
    message: MailMessage,
    own_addresses: set[str],
    team: list[TeamMember],
    contacts: list[Contact],
) -> str:
    """The journal's `by`: "" when the sender is one of own_addresses; else the team member's
    name, else the contact's name, else the display name, else the address."""
    sender = message.sender
    if sender.email in {address.lower() for address in own_addresses}:
        return ""
    for member in team:
        if member.email and member.email.lower() == sender.email:
            return member.name
    for contact in contacts:
        if contact.email and contact.email.lower() == sender.email:
            return contact.name
    return sender.name or sender.email


# --- Connecting and syncing -------------------------------------------------------------------


class SyncResult(BaseModel):
    counts: SyncCounts
    finished_at: datetime  # naive UTC


# Test seam: builds the IMAP connection. Production: _connect (a guarded IMAP4_SSL subclass that
# connects to research.check_public_host's address and checks the certificate against the host).
ConnectFn = Callable[[str, int], imaplib.IMAP4]


def check_login(settings: MailboxSettings, password: str, connect: ConnectFn | None = None) -> None:
    """Connect, log in, find the Inbox and Sent folders, log out. Raises MailboxError:
    - "Couldn't reach <host>. Check the server name." (DNS, refused, timeout)
    - "<host> isn't a public mail server, so Wardmind won't connect to it." (private address)
    - "The connection to <host> wasn't secure (its certificate didn't check out)." (TLS)
    - "<host> didn't accept that address and password. Most providers need an app password, not
      your usual one." (LOGIN failed)
    - "Connected, but couldn't find a Sent folder." (no folder with \\Sent and none of the usual
      names)"""
    make = connect if connect is not None else _connect
    with _session(make, settings, password) as conn:
        if _sent_folder(_list_folders(conn, settings.host)) is None:
            raise MailboxError(_NO_SENT)


def sync(
    store: LocalStore,
    settings: MailboxSettings,
    password: str,
    accounts: list[Account],
    contacts: dict[str, list[Contact]],
    journal: Journal,
    *,
    now: datetime,
    connect: ConnectFn | None = None,
) -> SyncResult:
    """One sync. Searches the Inbox and the Sent folder for messages SINCE (last_success_at's
    date minus OVERLAP_DAYS, or now minus FIRST_SYNC_DAYS on the first sync), newest first, at
    most MAX_MESSAGES_PER_SYNC in all, fetching BODY.PEEK[]<0.FETCH_BYTES> in batches of 50.
    Each message: parse_message, match_accounts (teams from journal.all()), then
    journal.log_email for each matched account with direction_of and sender_label. Updates and
    saves settings (last_sync_at always; last_success_at, last_counts and last_error=None on
    success; last_error on failure) and returns the result, or raises MailboxError (after saving
    the error). Never holds store.WRITE_LOCK during network calls."""
    make = connect if connect is not None else _connect
    try:
        fetched = _fetch_all(make, settings, password, _since(settings, now))
        counts = _log_matches(fetched, accounts, contacts, journal, settings, now)
    except MailboxError as error:
        _record(store, settings, now, error=error.message)
        raise
    except Exception:  # whatever it was, the attempt is recorded and the password isn't shown
        _record(store, settings, now, error=_SYNC_FAILED)
        raise MailboxError(_SYNC_FAILED) from None
    _record(store, settings, now, counts=counts)
    return SyncResult(counts=counts, finished_at=now)


# --- Helpers (private) ------------------------------------------------------------------------

_UNREACHABLE = "Couldn't reach {host}. Check the server name."
_NOT_PUBLIC = "{host} isn't a public mail server, so Wardmind won't connect to it."
_NOT_SECURE = "The connection to {host} wasn't secure (its certificate didn't check out)."
_LOGIN_FAILED = (
    "{host} didn't accept that address and password. Most providers need an app password, "
    "not your usual one."
)
_NO_SENT = "Connected, but couldn't find a Sent folder."
_STOPPED = "The connection to {host} stopped part way through. It will try again."
_SYNC_FAILED = "Wardmind couldn't finish the sync. It will try again."
_INBOX = "INBOX"
_SENT_NAMES = ("Sent", "Sent Items", "Sent Messages", "[Gmail]/Sent Mail", "INBOX.Sent")
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FETCH_BATCH = 50
_MAX_NAME = 200
_MAX_ADDRESS = 254
_MAX_MESSAGE_ID = 998
_HTML_BREAKS = frozenset(
    {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
)
_HTML_HIDDEN = frozenset({"head", "script", "style", "title"})
_QUOTE_LINE = re.compile(r"\s*>")
_REPLY_ON = re.compile(r"\s*On\s")
_REPLY_WROTE = re.compile(r"\bwrote:\s*$")
_ORIGINAL_MARK = re.compile(r"\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)
_BLANK_RUNS = re.compile(r"\n{4,}")
_LIST_PREFIX = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+')
_QUOTED_PAIR = re.compile(rb"\\(.)")


class _Folder(NamedTuple):
    name: str
    flags: str


def _parse(raw: bytes) -> MailMessage | None:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = _message_id(message.get("Message-ID"))
    senders = _addresses(message.get_all("From", []))
    sent_at = _sent_at(message.get("Date"))
    if message_id is None or not senders or sent_at is None:
        return None
    recipients = _unique(_addresses([*message.get_all("To", []), *message.get_all("Cc", [])]))
    return MailMessage(
        message_id=message_id,
        subject=_collapse(str(message.get("Subject", ""))),
        sent_at=sent_at,
        sender=senders[0],
        recipients=recipients,
        snippet=_snippet(message),
    )


def _collapse(text: str) -> str:
    """The text with every run of whitespace (line breaks included) made one space."""
    return " ".join(text.split())


def _message_id(value: object) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip().removeprefix("<").removesuffix(">")
    if not candidate or len(candidate) > _MAX_MESSAGE_ID:
        return None
    if any(char.isspace() or char in "<>" for char in candidate):
        return None
    return candidate


def _sent_at(value: object) -> datetime | None:
    """The Date header as naive local time, or None when it isn't a readable date."""
    if value is None:
        return None
    try:
        moment = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:  # "-0000": no zone given, so UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().replace(tzinfo=None)


def _addresses(values: list[object]) -> list[Address]:
    """Every usable address in the header values, in order (lower-cased, names decoded)."""
    found: list[Address] = []
    for name, value in email.utils.getaddresses([str(item) for item in values]):
        address = value.strip().lower()
        if address.count("@") != 1 or len(address) > _MAX_ADDRESS:
            continue
        if any(char.isspace() for char in address):
            continue
        found.append(Address(email=address, name=_collapse(name)[:_MAX_NAME]))
    return found


def _unique(addresses: list[Address]) -> list[Address]:
    seen: set[str] = set()
    found: list[Address] = []
    for address in addresses:
        if address.email not in seen:
            seen.add(address.email)
            found.append(address)
    return found


def _snippet(message: Message) -> str:
    text = _body_text(message, "plain")
    if text is None:
        markup = _body_text(message, "html")
        text = _html_text(markup) if markup is not None else ""
    return _visible(text)[:SNIPPET_CHARS].strip()


def _body_text(message: Message, subtype: str) -> str | None:
    """The first text part of this subtype that isn't an attachment and has something in it."""
    for part in message.walk():
        if part.get_content_maintype() != "text" or part.get_content_subtype() != subtype:
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except Exception:  # a part the parser can't decode (or one cut off by FETCH_BYTES)
            continue
        if isinstance(content, str) and content.strip():
            return content
    return None


class _TextOnly(HTMLParser):
    """Visible text of an HTML body: tags gone, scripts and styles dropped, line breaks kept."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_HIDDEN:
            self._hidden += 1
        elif tag in _HTML_BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _HTML_BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _html_text(markup: str) -> str:
    parser = _TextOnly()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # html.parser gives up on some broken markup; keep what came before
        pass
    return "".join(parser.parts)


def _visible(text: str) -> str:
    """What the email itself says: quoted lines (starting ">") are dropped, and everything from
    a reply line ("On ... wrote:") or an "Original Message" marker onwards is cut. Runs of three
    or more blank lines become two."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        if _QUOTE_LINE.match(line):
            continue
        if _starts_reply(lines, index) or _ORIGINAL_MARK.match(line):
            break
        kept.append(line.rstrip())
    return _BLANK_RUNS.sub("\n\n\n", "\n".join(kept)).strip()


def _starts_reply(lines: list[str], index: int) -> bool:
    """True for a line like "On Mon, ... Jane wrote:", also when "wrote:" wraps to the next line."""
    line = lines[index]
    if _REPLY_ON.match(line) is None:
        return False
    if _REPLY_WROTE.search(line):
        return True
    following = lines[index + 1] if index + 1 < len(lines) else ""
    return len(line) + len(following) < 400 and _REPLY_WROTE.search(following) is not None


def _domain_of(address: str) -> str:
    return address.rpartition("@")[2].lower()


def _team_emails(teams: dict[str, list[TeamMember]]) -> set[str]:
    return {member.email for members in teams.values() for member in members if member.email}


def _client_side(message: MailMessage, own: set[str], team_emails: set[str]) -> list[str]:
    """The addresses in sender + recipients that belong to the other side: not the CSM's own,
    not a team member's, not a colleague's (their domain is the CSM's, unless free mail)."""
    own_domains = {_domain_of(address) for address in own if "@" in address}
    found: list[str] = []
    for address in [message.sender, *message.recipients]:
        candidate = address.email
        if candidate in own or candidate in team_emails or candidate in found:
            continue
        domain = _domain_of(candidate)
        if domain in own_domains and domain not in FREE_MAIL_DOMAINS:
            continue
        found.append(candidate)
    return found


def _target(account: Account, account_contacts: list[Contact]) -> tuple[str | None, set[str]]:
    """The account's website domain (normalised, None when it has none) and its contacts' emails."""
    emails = {contact.email.lower() for contact in account_contacts if contact.email}
    return normalize_domain(account.domain), emails


def _covers(address: str, domain: str | None, contact_emails: set[str]) -> bool:
    """Whether a client-side address belongs to an account: a contact's email, or (outside the
    free-mail domains) the account's domain or a subdomain of it."""
    if address in contact_emails:
        return True
    if domain is None:
        return False
    address_domain = _domain_of(address)
    if address_domain in FREE_MAIL_DOMAINS:
        return False
    return address_domain == domain or address_domain.endswith("." + domain)


@contextmanager
def _session(make: ConnectFn, settings: MailboxSettings, password: str) -> Iterator[imaplib.IMAP4]:
    """A logged-in connection for the block; logged out afterwards. IMAP failures inside the
    block become the plain-English "stopped part way" message."""
    host = settings.host
    conn = _open(make, host, settings.port)
    try:
        _log_in(conn, host, settings.username, password)
        try:
            yield conn
        except (imaplib.IMAP4.error, OSError):
            raise MailboxError(_STOPPED.format(host=host)) from None
    finally:
        _logout(conn)


def _open(make: ConnectFn, host: str, port: int) -> imaplib.IMAP4:
    try:
        return make(host, port)
    except research.BlockedWebsite:
        raise MailboxError(_NOT_PUBLIC.format(host=host)) from None
    except research.SiteUnreachable:  # does not resolve
        raise MailboxError(_UNREACHABLE.format(host=host)) from None
    except ssl.SSLError:  # SSLCertVerificationError is one of these
        raise MailboxError(_NOT_SECURE.format(host=host)) from None
    except (OSError, imaplib.IMAP4.error):  # refused, timed out, or no IMAP greeting
        raise MailboxError(_UNREACHABLE.format(host=host)) from None


def _log_in(conn: imaplib.IMAP4, host: str, username: str, password: str) -> None:
    try:
        conn.login(username, password)
    except (OSError, imaplib.IMAP4.abort):
        raise MailboxError(_UNREACHABLE.format(host=host)) from None
    except (imaplib.IMAP4.error, UnicodeError):  # a refused login; or a password that can't be sent
        raise MailboxError(_LOGIN_FAILED.format(host=host)) from None


def _logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except (OSError, imaplib.IMAP4.error):
        pass


def _list_folders(conn: imaplib.IMAP4, host: str) -> list[_Folder]:
    """Every folder the server lists, with its name as the server sent it and its flags."""
    try:
        typ, data = conn.list()
    except (OSError, imaplib.IMAP4.abort):
        raise MailboxError(_STOPPED.format(host=host)) from None
    except imaplib.IMAP4.error:
        return []
    if typ != "OK":
        return []
    found: list[_Folder] = []
    for item in data or []:
        folder = _parse_folder(item)
        if folder is not None:
            found.append(folder)
    return found


def _parse_folder(item: object) -> _Folder | None:
    """One LIST response: (flags) "delimiter" name. A name sent as a literal arrives as a tuple of
    the line's head and the name."""
    if isinstance(item, tuple):
        head, literal = item[0], item[1]
        if not isinstance(head, bytes) or not isinstance(literal, bytes):
            return None
        match = _LIST_PREFIX.match(head)
        if match is None:
            return None
        return _Folder(literal.decode("utf-8", "replace"), _flags(match))
    if not isinstance(item, bytes):
        return None
    match = _LIST_PREFIX.match(item)
    if match is None:
        return None
    return _Folder(_unquote(item[match.end() :].strip()), _flags(match))


def _flags(match: re.Match[bytes]) -> str:
    return match.group("flags").decode("ascii", "replace").lower()


def _unquote(raw: bytes) -> str:
    if len(raw) >= 2 and raw.startswith(b'"') and raw.endswith(b'"'):
        raw = _QUOTED_PAIR.sub(rb"\1", raw[1:-1])
    return raw.decode("utf-8", "replace")


def _quote_folder(name: str) -> str:
    """A folder name as SELECT needs it: in double quotes, with \\ and " escaped."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sent_folder(folders: list[_Folder]) -> str | None:
    """The folder flagged \\Sent, else the first of the usual Sent names the server has."""
    for folder in folders:
        if r"\sent" in folder.flags.split():
            return folder.name
    by_name = {folder.name.casefold(): folder.name for folder in folders}
    for usual in _SENT_NAMES:
        if usual.casefold() in by_name:
            return by_name[usual.casefold()]
    return None


def _imap_date(day: date) -> str:
    """dd-Mon-yyyy with English month names, whatever the computer's language."""
    return f"{day.day:02d}-{_IMAP_MONTHS[day.month - 1]}-{day.year}"


def _since(settings: MailboxSettings, now: datetime) -> date:
    """The first day to search: the first sync looks back FIRST_SYNC_DAYS; later ones start
    OVERLAP_DAYS before the last successful sync."""
    if settings.last_success_at is None:
        return (now - timedelta(days=FIRST_SYNC_DAYS)).date()
    return settings.last_success_at.date() - timedelta(days=OVERLAP_DAYS)


def _select(conn: imaplib.IMAP4, folder: str, label: str) -> None:
    typ, _ = conn.select(_quote_folder(folder), readonly=True)
    if typ != "OK":
        raise MailboxError(f"Couldn't open the {label} folder.")


def _search(conn: imaplib.IMAP4, folder: str, since: date, label: str) -> list[bytes]:
    """Sequence numbers of the folder's messages since `since`, newest first."""
    _select(conn, folder, label)
    typ, data = conn.search(None, "SINCE", _imap_date(since))
    if typ != "OK":
        raise MailboxError(f"Couldn't search the {label} folder.")
    numbers = (data[0] or b"").split() if data else []
    return list(reversed(numbers))


def _interleave(lists: list[list[bytes]], limit: int) -> list[list[bytes]]:
    """Up to `limit` items in all, taken round robin so that each list's newest come first. Each
    list keeps its order; the result has one list per input list."""
    picked: list[list[bytes]] = [[] for _ in lists]
    positions = [0] * len(lists)
    total = 0
    while total < limit:
        progressed = False
        for index, found in enumerate(lists):
            if total >= limit:
                break
            if positions[index] < len(found):
                picked[index].append(found[positions[index]])
                positions[index] += 1
                total += 1
                progressed = True
        if not progressed:
            break
    return picked


def _fetch(
    conn: imaplib.IMAP4, folder: str, numbers: list[bytes], label: str
) -> list[MailMessage | None]:
    """The parsed messages (None where one can't be parsed), fetched read-only in batches."""
    messages: list[MailMessage | None] = []
    if not numbers:
        return messages
    _select(conn, folder, label)
    part = f"(BODY.PEEK[]<0.{FETCH_BYTES}>)"
    for start in range(0, len(numbers), _FETCH_BATCH):
        batch = b",".join(numbers[start : start + _FETCH_BATCH]).decode("ascii")
        typ, data = conn.fetch(batch, part)
        if typ != "OK":
            raise MailboxError(f"Couldn't read the {label} folder.")
        for item in data or []:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                messages.append(parse_message(item[1]))
    return messages


def _fetch_all(
    make: ConnectFn, settings: MailboxSettings, password: str, since: date
) -> list[MailMessage | None]:
    """Every message since `since` in the Inbox and the Sent folder, at most
    MAX_MESSAGES_PER_SYNC of them (the newest in each folder, interleaved). Logged out before
    anything is written."""
    host = settings.host
    with _session(make, settings, password) as conn:
        sent = _sent_folder(_list_folders(conn, host))
        if sent is None:
            raise MailboxError(_NO_SENT)
        inbox_found = _search(conn, _INBOX, since, "Inbox")
        sent_found = _search(conn, sent, since, "Sent")
        inbox_pick, sent_pick = _interleave([inbox_found, sent_found], MAX_MESSAGES_PER_SYNC)
        fetched = _fetch(conn, _INBOX, inbox_pick, "Inbox")
        fetched += _fetch(conn, sent, sent_pick, "Sent")
    return fetched


def _log_matches(
    messages: list[MailMessage | None],
    accounts: list[Account],
    contacts: dict[str, list[Contact]],
    journal: Journal,
    settings: MailboxSettings,
    now: datetime,
) -> SyncCounts:
    """Match each fetched message to accounts and log it, newest first. No lock is held here
    beyond what journal.log_email takes for each write."""
    own = {address.lower() for address in settings.own_addresses}
    teams = {account_id: list(found.team) for account_id, found in journal.all().items()}
    team_emails = _team_emails(teams)
    counts = SyncCounts(read=len(messages))
    parsed = [message for message in messages if message is not None]
    counts.unmatched = len(messages) - len(parsed)
    parsed.sort(key=lambda message: message.sent_at, reverse=True)
    targets = {account.id: _target(account, contacts.get(account.id, [])) for account in accounts}
    for message in parsed:
        matched = match_accounts(message, accounts, contacts, own, teams)
        if not matched:
            counts.unmatched += 1
            continue
        direction = direction_of(message, own, team_emails)
        client = _client_side(message, own, team_emails)
        for account_id in matched:
            domain, contact_emails = targets[account_id]
            logged = LoggedEmail(
                message_id=message.message_id,
                subject=message.subject,
                sent_on=message.sent_at.date(),
                direction=direction,
                by=sender_label(
                    message,
                    own,
                    teams.get(account_id, []),
                    contacts.get(account_id, []),
                ),
                people=[address for address in client if _covers(address, domain, contact_emails)],
                snippet=message.snippet,
            )
            if journal.log_email(account_id, logged, now=now) is None:
                counts.already += 1
            else:
                counts.logged += 1
    return counts


def _record(
    store: LocalStore,
    started: MailboxSettings,
    now: datetime,
    *,
    counts: SyncCounts | None = None,
    error: str | None = None,
) -> None:
    """Save the outcome of a sync on the settings as they are now. Nothing is saved when the
    mailbox was disconnected (or connected again) while the sync ran."""
    with WRITE_LOCK:
        current = load_settings(store)
        if current is None or current.connected_at != started.connected_at:
            return
        if error is None:
            changes: dict[str, object] = {
                "last_sync_at": now,
                "last_success_at": now,
                "last_error": None,
                "last_counts": counts,
            }
        else:
            changes = {"last_sync_at": now, "last_error": error}
        save_settings(store, current.model_copy(update=changes))


def _connect(host: str, port: int) -> imaplib.IMAP4:
    """The production connection: IMAP over TLS (see _SecureIMAP)."""
    return _SecureIMAP(
        host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_SECONDS
    )


class _SecureIMAP(imaplib.IMAP4_SSL):
    """IMAP4_SSL that connects to the address research.check_public_host picks (only public
    addresses), then wraps the connection with TLS checked against the host name. The name is
    what the certificate must match, so a DNS change after the check can't redirect it."""

    def _create_socket(self, timeout: float | None = None) -> ssl.SSLSocket:
        address = research.check_public_host(self.host, self.port)
        raw = socket.create_connection((address, self.port), timeout=TIMEOUT_SECONDS)
        try:
            return self.ssl_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
