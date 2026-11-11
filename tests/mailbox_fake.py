"""Fakes for the mailbox tests: an IMAP connection over in-memory folders, raw email bytes, and a
journal stand-in. Nothing here reaches a mail server; tests pass FakeIMAP as the `connect` seam."""

from __future__ import annotations

import imaplib
from collections.abc import Callable
from datetime import UTC, date, datetime
from email.message import EmailMessage

from wardmind.core import mailbox
from wardmind.core.contacts import Contact
from wardmind.core.journal import AccountJournal, LoggedEmail, TeamMember, TeamRole
from wardmind.core.models import Account, Milestones

NOW = datetime(2026, 11, 11, 10, 0)  # naive UTC, the time the tests pin
PASSWORD = "app-pass 4242"  # a fake app password, with a space like the real ones may have
OWN = {"csm@wardmind.test"}
# 12:00 UTC on 2 Nov 2026 as the naive local time the parser gives.
NOON_2_NOV = datetime(2026, 11, 2, 12, 0, tzinfo=UTC).astimezone().replace(tzinfo=None)
INBOX_LINE = b'(\\HasNoChildren) "/" "INBOX"'
SENT_LINE = b'(\\HasNoChildren \\Sent) "/" "Sent"'


def _folder_name(text: str) -> str:
    """A SELECT argument as the folder's name: quotes and their escapes removed."""
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


class FakeIMAP:
    """The part of imaplib.IMAP4_SSL that mailbox.py calls, over in-memory folders. Every call is
    recorded in `calls` as a tuple, e.g. ("select", '"INBOX"', True) or ("search", None, "SINCE",
    "12-Oct-2026"). `before_fetch`, when set, runs at the start of each FETCH."""

    def __init__(
        self,
        folders: dict[str, list[bytes]] | None = None,
        *,
        lines: list[bytes] | None = None,
        login_error: bool = False,
    ) -> None:
        self.folders: dict[str, list[bytes]] = (
            folders if folders is not None else {"INBOX": [], "Sent": []}
        )
        self.lines = lines if lines is not None else [INBOX_LINE, SENT_LINE]
        self.login_error = login_error
        self.calls: list[tuple] = []
        self.selected = ""
        self.logged_out = False
        self.before_fetch: Callable[[FakeIMAP], None] | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user))
        if self.login_error:
            raise imaplib.IMAP4.error("LOGIN command error: BAD [AUTHENTICATIONFAILED] invalid")
        return "OK", [b"LOGIN completed"]

    def list(self, *args: str) -> tuple[str, list[bytes]]:
        self.calls.append(("list",))
        return "OK", list(self.lines)

    def select(self, mailbox_name: str = "INBOX", readonly: bool = False):
        self.calls.append(("select", mailbox_name, readonly))
        name = _folder_name(mailbox_name)
        if name not in self.folders:
            return "NO", [b"no such mailbox"]
        self.selected = name
        return "OK", [str(len(self.folders[name])).encode()]

    def search(self, charset: str | None, *criteria: str):
        self.calls.append(("search", charset, *criteria))
        count = len(self.folders[self.selected])
        return "OK", [b" ".join(str(number).encode() for number in range(1, count + 1))]

    def fetch(self, message_set: str, message_parts: str):
        self.calls.append(("fetch", message_set, message_parts))
        if self.before_fetch is not None:
            self.before_fetch(self)
        data: list[object] = []
        for text in message_set.split(","):
            number = int(text)
            raw = self.folders[self.selected][number - 1]
            data.append((f"{number} (BODY[]<0> {{{len(raw)}}}".encode(), raw))
            data.append(b")")
        return "OK", data

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        self.logged_out = True
        return "BYE", [b"logging out"]

    def of(self, kind: str) -> list[tuple]:
        """The recorded calls of one kind, in order."""
        return [call for call in self.calls if call[0] == kind]


def raw_email(
    *,
    message_id: str | None = "<m1@acme.com>",
    sender: str | None = "Jane Doe <jane@acme.com>",
    to: str | None = "CSM <csm@wardmind.test>",
    cc: str | None = None,
    subject: str | None = "Renewal plan",
    date_header: str | None = "Mon, 2 Nov 2026 12:00:00 +0000",
    text: str | None = "Hello",
    html: str | None = None,
) -> bytes:
    """One email as bytes. A header given as None is left out."""
    message = EmailMessage()
    headers = (
        ("Message-ID", message_id),
        ("From", sender),
        ("To", to),
        ("Cc", cc),
        ("Subject", subject),
        ("Date", date_header),
    )
    for name, value in headers:
        if value is not None:
            message[name] = value
    if text is not None and html is not None:
        message.set_content(text)
        message.add_alternative(html, subtype="html")
    elif html is not None:
        message.set_content(html, subtype="html")
    else:
        message.set_content(text or "")
    return message.as_bytes()


def mail(sender: str, *recipients: str, name: str = "") -> mailbox.MailMessage:
    """A parsed message for the matching tests (addresses already lower-cased)."""
    return mailbox.MailMessage(
        message_id="m@example.test",
        subject="Renewal",
        sent_at=NOW,
        sender=mailbox.Address(email=sender, name=name),
        recipients=[mailbox.Address(email=address) for address in recipients],
        snippet="",
    )


def account(account_id: str = "acc-1", domain: str | None = "acme.com", name: str = "Acme"):
    return Account(
        id=account_id,
        name=name,
        arr=10_000,
        tier="Pro",
        owner="Sam",
        renewal_date=date(2027, 3, 1),
        milestones=Milestones(done=0, total=0),
        domain=domain,
    )


def contact(email: str, name: str = "Jane Contact") -> Contact:
    return Contact(id="c-00000001", name=name, email=email, updated_at=NOW)


def member(email: str, name: str = "Ann Team") -> TeamMember:
    return TeamMember(id="t-00000001", name=name, email=email, role=TeamRole.CSM, created_at=NOW)


def settings(**overrides) -> mailbox.MailboxSettings:
    base: dict = {
        "provider": mailbox.Provider.GMAIL,
        "host": "imap.gmail.com",
        "username": "csm@wardmind.test",
        "own_addresses": ["csm@wardmind.test"],
        "connected_at": datetime(2026, 11, 1, 9, 0),
    }
    base.update(overrides)
    return mailbox.MailboxSettings(**base)


class FakeJournal:
    """The part of journal.Journal the sync uses: all() and log_email(). `fail` is raised by
    log_email when set."""

    def __init__(
        self, teams: dict[str, list[TeamMember]] | None = None, *, fail: Exception | None = None
    ) -> None:
        self.teams = teams or {}
        self.fail = fail
        self.logged: dict[str, list[LoggedEmail]] = {}

    def all(self) -> dict[str, AccountJournal]:
        return {
            account_id: AccountJournal(team=list(members))
            for account_id, members in self.teams.items()
        }

    def log_email(self, account_id: str, logged: LoggedEmail, *, now: datetime):
        if self.fail is not None:
            raise self.fail
        entries = self.logged.setdefault(account_id, [])
        if any(entry.message_id == logged.message_id for entry in entries):
            return None
        entries.append(logged)
        return logged
