"""The mailbox connection (core/mailbox.py): parsing and matching, the settings file, the IMAP
session and one sync. The IMAP connection is mailbox_fake.FakeIMAP, passed as the `connect` seam,
so no test reaches a mail server."""

from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from mailbox_fake import (
    INBOX_LINE,
    NOON_2_NOV,
    NOW,
    OWN,
    PASSWORD,
    SENT_LINE,
    FakeIMAP,
    FakeJournal,
    account,
    contact,
    mail,
    member,
    raw_email,
    settings,
)
from wardmind.core import journal, mailbox, research, secrets
from wardmind.core.journal import Direction
from wardmind.core.store import LocalStore

NOT_CONNECTED = "Connect a mailbox first."
TIMEOUT = mailbox.TIMEOUT_SECONDS


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def run_sync(
    store: LocalStore,
    fake: FakeIMAP,
    *,
    accounts=None,
    contacts=None,
    journal_=None,
    now: datetime = NOW,
    **overrides,
) -> mailbox.SyncResult:
    """Save the settings (a connected mailbox) and run one sync against the fake."""
    saved = settings(**overrides)
    mailbox.save_settings(store, saved)
    return mailbox.sync(
        store,
        saved,
        PASSWORD,
        [account()] if accounts is None else accounts,
        contacts or {},
        journal_ if journal_ is not None else FakeJournal(),
        now=now,
        connect=lambda host, port: fake,
    )


# --- parse_message ----------------------------------------------------------------------------


def test_a_plain_message_gives_its_fields():
    raw = raw_email(
        message_id="<abc@acme.com>",
        sender="Jane Doe <Jane@Acme.com>",
        to="CSM <csm@wardmind.test>, Bob <bob@acme.com>",
        cc="Ann <ann@acme.com>, bob@acme.com",
        subject="Renewal plan",
        text="Hello there\n\nSee you Monday",
    )
    parsed = mailbox.parse_message(raw)
    assert parsed is not None
    assert parsed.message_id == "abc@acme.com"
    assert parsed.subject == "Renewal plan"
    assert parsed.sent_at == NOON_2_NOV
    assert parsed.sender == mailbox.Address(email="jane@acme.com", name="Jane Doe")
    assert [address.email for address in parsed.recipients] == [
        "csm@wardmind.test",
        "bob@acme.com",
        "ann@acme.com",
    ]
    assert parsed.snippet == "Hello there\n\nSee you Monday"


def test_an_html_only_message_keeps_its_text_and_drops_tags_and_scripts():
    html = (
        "<html><head><title>Hidden title</title><style>p {}</style></head><body>"
        "<p>Dear Jane,</p><script>alert(1)</script>"
        "<p>Our <b>new</b> plan &amp; &lt;terms&gt;</p></body></html>"
    )
    parsed = mailbox.parse_message(raw_email(text=None, html=html))
    assert parsed is not None
    assert parsed.snippet == "Dear Jane,\n\nOur new plan & <terms>"


def test_attachments_are_never_read_as_the_body():
    built = EmailMessage()
    built["Message-ID"] = "<att@acme.com>"
    built["From"] = "Jane Doe <jane@acme.com>"
    built["To"] = "csm@wardmind.test"
    built["Date"] = "Mon, 2 Nov 2026 12:00:00 +0000"
    built["Subject"] = "Files"
    built.set_content("The real body")
    built.add_attachment("ATTACHED NOTES", filename="notes.txt")
    built.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="x.pdf")
    parsed = mailbox.parse_message(built.as_bytes())
    assert parsed is not None
    assert parsed.snippet == "The real body"


def test_an_encoded_subject_is_decoded():
    raw = raw_email(subject="=?utf-8?q?Renewal_=E2=80=93_plan?=")
    parsed = mailbox.parse_message(raw)
    assert parsed is not None
    assert parsed.subject == "Renewal \u2013 plan"


@pytest.mark.parametrize(
    "raw",
    [
        raw_email(message_id=None),
        raw_email(date_header=None),
        raw_email(date_header="sometime last week"),
        raw_email(sender=None),
    ],
    ids=["no-message-id", "no-date", "unreadable-date", "no-from"],
)
def test_a_message_without_an_id_a_sender_or_a_date_is_skipped(raw):
    assert mailbox.parse_message(raw) is None


def test_quoted_reply_lines_are_cut():
    text = "Thanks, that works.\n\n> Can we move the call?\n> Tuesday?\nSee you then"
    parsed = mailbox.parse_message(raw_email(text=text))
    assert parsed is not None
    assert parsed.snippet == "Thanks, that works.\n\nSee you then"


def test_an_on_wrote_line_cuts_the_rest_of_the_message():
    text = (
        "Yes, Monday works.\n\n"
        "On Mon, 2 Nov 2026 at 09:00, Jane Doe <jane@acme.com> wrote:\n"
        "> the old stuff\nolder still"
    )
    parsed = mailbox.parse_message(raw_email(text=text))
    assert parsed is not None
    assert parsed.snippet == "Yes, Monday works."


def test_an_on_wrote_line_that_wraps_is_cut_too():
    text = "Yes.\n\nOn Mon, 2 Nov 2026 at 09:00, Jane Doe\n<jane@acme.com> wrote:\n> old"
    parsed = mailbox.parse_message(raw_email(text=text))
    assert parsed is not None
    assert parsed.snippet == "Yes."


def test_a_sentence_that_starts_with_on_is_kept():
    parsed = mailbox.parse_message(raw_email(text="On Monday we'll talk.\nBye"))
    assert parsed is not None
    assert parsed.snippet == "On Monday we'll talk.\nBye"


def test_the_snippet_is_cut_to_snippet_chars():
    parsed = mailbox.parse_message(raw_email(text="x" * 5000))
    assert parsed is not None
    assert len(parsed.snippet) == mailbox.SNIPPET_CHARS


def test_three_or_more_blank_lines_become_two():
    parsed = mailbox.parse_message(raw_email(text="a" + "\n" * 6 + "b"))
    assert parsed is not None
    assert parsed.snippet == "a\n\n\nb"


def test_a_message_cut_at_fetch_bytes_still_parses():
    raw = raw_email(text="word " * 40_000)
    assert len(raw) > mailbox.FETCH_BYTES
    parsed = mailbox.parse_message(raw[: mailbox.FETCH_BYTES])
    assert parsed is not None
    assert parsed.snippet.startswith("word word")
    assert len(parsed.snippet) <= mailbox.SNIPPET_CHARS


def test_a_message_cut_inside_its_headers_is_skipped():
    raw = raw_email()
    assert mailbox.parse_message(raw[:40]) is None


@pytest.mark.parametrize(
    "raw", [b"", b"\x00\xff\xfe garbage", b"Message-ID: <x@y.z>\r\n\r\nbody", b"\x80" * 500]
)
def test_garbage_bytes_give_none_and_never_raise(raw):
    assert mailbox.parse_message(raw) is None


# --- match_accounts ---------------------------------------------------------------------------


def test_a_contact_email_matches_its_account_even_on_free_mail():
    accounts = [account("acc-x", "acme.com")]
    contacts = {"acc-x": [contact("pat@gmail.com")]}
    found = mailbox.match_accounts(
        mail("pat@gmail.com", "csm@wardmind.test"), accounts, contacts, OWN, {}
    )
    assert found == ["acc-x"]


def test_a_free_mail_address_never_matches_by_domain():
    accounts = [account("acc-x", "gmail.com")]
    assert mailbox.match_accounts(mail("someone@gmail.com"), accounts, {}, OWN, {}) == []


def test_the_domain_and_its_subdomains_match():
    accounts = [account("acc-acme", "acme.com")]
    assert mailbox.match_accounts(mail("jane@acme.com"), accounts, {}, OWN, {}) == ["acc-acme"]
    assert mailbox.match_accounts(mail("jane@eu.acme.com"), accounts, {}, OWN, {}) == ["acc-acme"]


def test_a_lookalike_domain_does_not_match():
    accounts = [account("acc-acme", "acme.com")]
    assert mailbox.match_accounts(mail("jane@notacme.com"), accounts, {}, OWN, {}) == []
    assert mailbox.match_accounts(mail("jane@acme.com.evil.test"), accounts, {}, OWN, {}) == []


def test_the_account_domain_ignores_scheme_www_and_case():
    accounts = [account("acc-w", "https://www.Acme.com/about")]
    assert mailbox.match_accounts(mail("jane@acme.com"), accounts, {}, OWN, {}) == ["acc-w"]


def test_colleagues_on_the_own_domain_are_not_clients():
    own = {"csm@ourco.com"}
    accounts = [account("acc-ours", "ourco.com")]
    colleague = mail("ann@ourco.com", "csm@ourco.com")
    assert mailbox.match_accounts(colleague, accounts, {}, own, {}) == []


def test_an_own_free_mail_domain_is_not_treated_as_colleagues():
    own = {"csm@gmail.com"}
    accounts = [account("acc-x", "acme.com")]
    contacts = {"acc-x": [contact("pat@gmail.com")]}
    found = mailbox.match_accounts(
        mail("pat@gmail.com", "csm@gmail.com"), accounts, contacts, own, {}
    )
    assert found == ["acc-x"]


def test_team_emails_of_any_account_are_not_clients():
    teams = {"acc-other": [member("pat@acme.com")]}
    accounts = [account("acc-acme", "acme.com")]
    assert mailbox.match_accounts(mail("pat@acme.com"), accounts, {}, OWN, teams) == []


def test_the_own_address_is_never_a_client():
    accounts = [account("acc-acme", "acme.com")]
    assert mailbox.match_accounts(mail("csm@wardmind.test"), accounts, {}, OWN, {}) == []
    found = mailbox.match_accounts(
        mail("csm@wardmind.test", "jane@acme.com"), accounts, {}, OWN, {}
    )
    assert found == ["acc-acme"]


def test_more_than_three_matching_accounts_gives_nothing():
    four = [account(f"acc-{letter}", "acme.com") for letter in "abcd"]
    assert mailbox.match_accounts(mail("jane@acme.com"), four, {}, OWN, {}) == []
    three = four[:3]
    assert mailbox.match_accounts(mail("jane@acme.com"), three, {}, OWN, {}) == [
        "acc-a",
        "acc-b",
        "acc-c",
    ]


def test_matches_come_back_in_account_order():
    accounts = [account("acc-b", "acme.com"), account("acc-a", "acme.com")]
    assert mailbox.match_accounts(mail("jane@acme.com"), accounts, {}, OWN, {}) == [
        "acc-b",
        "acc-a",
    ]


# --- direction_of and sender_label ------------------------------------------------------------


def test_direction_is_sent_for_us_or_a_team_member_otherwise_received():
    assert mailbox.direction_of(mail("csm@wardmind.test", "jane@acme.com"), OWN, set()) is (
        Direction.SENT
    )
    assert mailbox.direction_of(mail("pat@acme.com"), OWN, {"pat@acme.com"}) is Direction.SENT
    assert mailbox.direction_of(mail("jane@acme.com", "csm@wardmind.test"), OWN, set()) is (
        Direction.RECEIVED
    )


def test_sender_label_follows_team_contact_display_name_then_address():
    team = [member("pat@acme.com", name="Pat Team")]
    contacts = [contact("jane@acme.com", name="Jane Contact")]
    assert mailbox.sender_label(mail("csm@wardmind.test"), OWN, team, contacts) == ""
    assert mailbox.sender_label(mail("pat@acme.com"), OWN, team, contacts) == "Pat Team"
    named = mail("jane@acme.com", name="Jane Display")
    assert mailbox.sender_label(named, OWN, team, contacts) == "Jane Contact"
    assert mailbox.sender_label(mail("bob@acme.com", name="Bob Display"), OWN, team, contacts) == (
        "Bob Display"
    )
    assert mailbox.sender_label(mail("bob@acme.com"), OWN, team, contacts) == "bob@acme.com"


# --- The clean_* validators -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        (" IMAP.Example.com ", "imap.example.com"),
        ("mail-1.corp.example.org", "mail-1.corp.example.org"),
    ],
)
def test_clean_host_accepts_a_plain_host_name(text, expected):
    assert mailbox.clean_host(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "imap",
        "https://imap.example.com",
        "imap.example.com:993",
        "imap.example.com/inbox",
        "imap..example.com",
        "imap.exa mple.com",
        "x." + "a" * 260 + ".com",
    ],
)
def test_clean_host_refuses_anything_else(text):
    with pytest.raises(ValueError, match="Type the server's name, like imap.example.com."):
        mailbox.clean_host(text)


def test_clean_username_lower_cases_and_refuses_blank():
    assert mailbox.clean_username("  Jane@Acme.com ") == "jane@acme.com"
    with pytest.raises(ValueError, match="Type the email address you sign in with."):
        mailbox.clean_username("   ")


def test_clean_username_refuses_a_bad_address():
    with pytest.raises(ValueError):
        mailbox.clean_username("not an address")


def test_clean_aliases_splits_lines_and_commas_and_drops_duplicates():
    text = "Jane@Acme.com\nalt@wardmind.test, jane@acme.com"
    assert mailbox.clean_aliases(text) == ["jane@acme.com", "alt@wardmind.test"]
    assert mailbox.clean_aliases("  \n ") == []


def test_clean_aliases_names_the_first_bad_one():
    with pytest.raises(ValueError, match="“nope” isn't an email address."):
        mailbox.clean_aliases("ok@wardmind.test, nope, also-bad")


def test_clean_aliases_keeps_at_most_ten():
    eleven = ", ".join(f"a{number}@wardmind.test" for number in range(11))
    with pytest.raises(ValueError, match="10 other addresses"):
        mailbox.clean_aliases(eleven)


# --- Settings ---------------------------------------------------------------------------------


def test_settings_round_trip_and_none_when_missing(store):
    assert mailbox.load_settings(store) is None
    saved = settings(last_sync_at=NOW, last_counts=mailbox.SyncCounts(read=3, logged=2))
    mailbox.save_settings(store, saved)
    assert mailbox.load_settings(store) == saved


def test_an_unreadable_settings_file_reads_as_not_connected(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "mailbox.json").write_text("{ not json", encoding="utf-8")
    assert mailbox.load_settings(store) is None


def test_disconnect_removes_the_settings_and_password_and_keeps_the_activity(store):
    mailbox.save_settings(store, settings())
    secrets.save_secret(mailbox.SECRET_NAME, PASSWORD)
    (store.root / "journal.json").write_text('{"by_account": {}}', encoding="utf-8")
    mailbox.disconnect(store)
    assert mailbox.load_settings(store) is None
    assert not (store.root / "mailbox.json").exists()
    assert secrets.get_secret(mailbox.SECRET_NAME) is None
    assert (store.root / "journal.json").exists()


# --- check_login ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        [INBOX_LINE, SENT_LINE],
        [INBOX_LINE, b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"'],
        [b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren) "." "INBOX.Sent"'],
        [b'(\\HasNoChildren \\Sent) "/" "Outbox items"', INBOX_LINE],
    ],
    ids=["flagged", "gmail-name", "dotted-name", "flag-wins-over-name"],
)
def test_login_finds_the_sent_folder(lines):
    fake = FakeIMAP(lines=lines)
    mailbox.check_login(settings(), PASSWORD, connect=lambda host, port: fake)
    assert ("login", "csm@wardmind.test") in fake.calls
    assert fake.logged_out


def test_folder_names_are_quoted_for_select_and_unquoted_from_list():
    assert mailbox._quote_folder('Work "Q3"\\draft') == '"Work \\"Q3\\"\\\\draft"'
    escaped = mailbox._parse_folder(b'(\\HasNoChildren) "/" "Work \\"Q3\\""')
    assert escaped is not None and escaped.name == 'Work "Q3"'
    literal = mailbox._parse_folder((b'(\\HasNoChildren \\Sent) "/" {4}', b"Sent"))
    assert literal is not None
    assert (literal.name, literal.flags) == ("Sent", "\\hasnochildren \\sent")
    assert mailbox._parse_folder(b"not a list response") is None


def test_no_sent_folder_is_reported_and_the_session_is_closed():
    fake = FakeIMAP(lines=[INBOX_LINE, b'(\\HasNoChildren) "/" "Drafts"'])
    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.check_login(settings(), PASSWORD, connect=lambda host, port: fake)
    assert caught.value.message == "Connected, but couldn't find a Sent folder."
    assert fake.logged_out


def test_a_refused_login_says_so_and_never_shows_the_password():
    fake = FakeIMAP(login_error=True)
    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.check_login(settings(), PASSWORD, connect=lambda host, port: fake)
    assert caught.value.message == (
        "imap.gmail.com didn't accept that address and password. Most providers need an app "
        "password, not your usual one."
    )
    assert PASSWORD not in caught.value.message
    assert fake.logged_out


def test_a_private_address_is_refused_before_any_connection(monkeypatch):
    def refuse(host, port, resolve=socket.getaddrinfo):
        raise research.BlockedWebsite("refused")

    def no_socket(*args, **kwargs):
        pytest.fail("A connection was attempted to a private address.")

    monkeypatch.setattr(research, "check_public_host", refuse)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.check_login(settings(host="mail.example.com"), PASSWORD)
    assert caught.value.message == (
        "mail.example.com isn't a public mail server, so Wardmind won't connect to it."
    )


def test_a_host_that_does_not_resolve_is_unreachable(monkeypatch):
    def unresolved(host, port, resolve=socket.getaddrinfo):
        raise research.SiteUnreachable("Couldn't find it.")

    monkeypatch.setattr(research, "check_public_host", unresolved)
    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.check_login(settings(host="mail.example.com"), PASSWORD)
    assert caught.value.message == "Couldn't reach mail.example.com. Check the server name."


def test_a_refused_connection_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        research,
        "check_public_host",
        lambda host, port, resolve=socket.getaddrinfo: "93.184.216.34",
    )

    def refused(address, timeout=None):
        raise ConnectionRefusedError(111, "refused")

    monkeypatch.setattr(socket, "create_connection", refused)
    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.check_login(settings(host="mail.example.com"), PASSWORD)
    assert caught.value.message == "Couldn't reach mail.example.com. Check the server name."


class _RawSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _CertificateContext:
    """A TLS context whose handshake fails the way a bad certificate does, recording the host
    name the connection asked to be checked against."""

    def __init__(self, record: dict) -> None:
        self.record = record

    def wrap_socket(self, sock, server_hostname=None):
        self.record["server_hostname"] = server_hostname
        raise ssl.SSLCertVerificationError("certificate verify failed")


def test_a_certificate_that_does_not_check_out_is_reported(monkeypatch):
    seen: dict = {}
    raw = _RawSocket()

    def connect_to(address, timeout=None):
        seen["address"] = address
        seen["timeout"] = timeout
        return raw

    monkeypatch.setattr(
        research,
        "check_public_host",
        lambda host, port, resolve=socket.getaddrinfo: "93.184.216.34",
    )
    monkeypatch.setattr(socket, "create_connection", connect_to)
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: _CertificateContext(seen))
    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.check_login(settings(), PASSWORD)
    assert caught.value.message == (
        "The connection to imap.gmail.com wasn't secure (its certificate didn't check out)."
    )
    # The checked address is the one connected to; the certificate is checked against the name.
    assert seen["address"] == ("93.184.216.34", 993)
    assert seen["timeout"] == TIMEOUT
    assert seen["server_hostname"] == "imap.gmail.com"
    assert raw.closed


# --- sync -------------------------------------------------------------------------------------


def test_the_first_sync_looks_back_thirty_days(store):
    fake = FakeIMAP()
    run_sync(store, fake, now=datetime(2026, 11, 11, 10, 0))
    assert {call[1:] for call in fake.of("search")} == {(None, "SINCE", "12-Oct-2026")}


def test_a_later_sync_starts_two_days_before_the_last_success(store):
    fake = FakeIMAP()
    run_sync(store, fake, last_success_at=datetime(2026, 11, 9, 8, 0))
    assert {call[1:] for call in fake.of("search")} == {(None, "SINCE", "07-Nov-2026")}


def test_folders_are_read_only_and_messages_are_only_peeked(store):
    fake = FakeIMAP(
        {
            "INBOX": [raw_email(message_id="<in@acme.com>")],
            "Sent": [raw_email(message_id="<out@acme.com>")],
        }
    )
    run_sync(store, fake)
    selects = fake.of("select")
    assert [call[1] for call in selects] == ['"INBOX"', '"Sent"', '"INBOX"', '"Sent"']
    assert all(call[2] is True for call in selects)
    fetches = fake.of("fetch")
    assert fetches
    assert all(call[2] == "(BODY.PEEK[]<0.65536>)" for call in fetches)


def test_messages_are_fetched_in_batches_of_fifty(store):
    inbox = [raw_email(message_id=f"<m{number}@other.example>") for number in range(120)]
    fake = FakeIMAP({"INBOX": inbox, "Sent": []})
    result = run_sync(store, fake, accounts=[])
    batch_sizes = [len(call[1].split(",")) for call in fake.of("fetch")]
    assert batch_sizes == [50, 50, 20]
    assert result.counts.read == 120
    assert result.counts.unmatched == 120


def test_matched_emails_are_logged_with_direction_sender_people_and_snippet(store):
    inbox = [
        raw_email(
            message_id="<in1@acme.com>",
            sender="Jane Doe <jane@acme.com>",
            to="CSM <csm@wardmind.test>",
            subject="Renewal",
            date_header="Mon, 2 Nov 2026 12:00:00 +0000",
            text="Can we talk Tuesday?\n> old quoted line\n",
        ),
        raw_email(
            message_id="<news@shop.example>",
            sender="News <news@shop.example>",
            to="csm@wardmind.test",
        ),
    ]
    sent = [
        raw_email(
            message_id="<out1@wardmind.test>",
            sender="CSM <csm@wardmind.test>",
            to="Jane Doe <jane@acme.com>, Bob <bob@acme.com>",
            subject="Re: Renewal",
            date_header="Tue, 3 Nov 2026 09:00:00 +0000",
            text="Tuesday works.",
        )
    ]
    fake = FakeIMAP({"INBOX": inbox, "Sent": sent})
    fake_journal = FakeJournal()
    result = run_sync(store, fake, journal_=fake_journal)

    assert result.counts == mailbox.SyncCounts(read=3, logged=2, already=0, unmatched=1)
    outgoing, incoming = fake_journal.logged["acc-1"]  # newest first
    assert outgoing.direction is Direction.SENT
    assert outgoing.by == ""
    assert outgoing.people == ["jane@acme.com", "bob@acme.com"]
    assert outgoing.subject == "Re: Renewal"
    assert outgoing.snippet == "Tuesday works."
    assert outgoing.sent_on == datetime(2026, 11, 3, 9, tzinfo=UTC).astimezone().date()
    assert incoming.direction is Direction.RECEIVED
    assert incoming.by == "Jane Doe"
    assert incoming.people == ["jane@acme.com"]
    assert incoming.snippet == "Can we talk Tuesday?"
    assert set(fake_journal.logged) == {"acc-1"}


def test_a_second_run_logs_nothing_twice(store):
    inbox = [raw_email(message_id="<in1@acme.com>")]
    sent = [
        raw_email(
            message_id="<out1@wardmind.test>", sender="CSM <csm@wardmind.test>", to="jane@acme.com"
        )
    ]
    fake = FakeIMAP({"INBOX": inbox, "Sent": sent})
    fake_journal = FakeJournal()
    first = run_sync(store, fake, journal_=fake_journal)
    assert first.counts.logged == 2
    second = mailbox.sync(
        store,
        mailbox.load_settings(store),
        PASSWORD,
        [account()],
        {},
        fake_journal,
        now=NOW + timedelta(minutes=15),
        connect=lambda host, port: fake,
    )
    assert second.counts == mailbox.SyncCounts(read=2, logged=0, already=2, unmatched=0)
    assert len(fake_journal.logged["acc-1"]) == 2


def test_the_newest_come_first_across_both_folders(store):
    old_inbox = raw_email(
        message_id="<old@acme.com>", date_header="Sat, 31 Oct 2026 12:00:00 +0000", subject="old"
    )
    mid_inbox = raw_email(
        message_id="<mid@acme.com>", date_header="Mon, 2 Nov 2026 12:00:00 +0000", subject="mid"
    )
    new_sent = raw_email(
        message_id="<new@acme.com>",
        sender="CSM <csm@wardmind.test>",
        to="jane@acme.com",
        date_header="Thu, 5 Nov 2026 12:00:00 +0000",
        subject="new",
    )
    fake_journal = FakeJournal()
    run_sync(
        store,
        FakeIMAP({"INBOX": [old_inbox, mid_inbox], "Sent": [new_sent]}),
        journal_=fake_journal,
    )
    assert [entry.subject for entry in fake_journal.logged["acc-1"]] == ["new", "mid", "old"]


def test_the_cap_limits_how_many_messages_are_read(store, monkeypatch):
    monkeypatch.setattr(mailbox, "MAX_MESSAGES_PER_SYNC", 3)
    inbox = [raw_email(message_id=f"<i{number}@acme.com>") for number in range(4)]
    sent = [
        raw_email(
            message_id=f"<s{number}@acme.com>", sender="CSM <csm@wardmind.test>", to="jane@acme.com"
        )
        for number in range(2)
    ]
    fake = FakeIMAP({"INBOX": inbox, "Sent": sent})
    result = run_sync(store, fake)
    assert result.counts.read == 3
    fetched = [ident for call in fake.of("fetch") for ident in call[1].split(",")]
    assert len(fetched) == 3


def test_a_failed_sync_is_recorded_and_the_password_never_is(store):
    fake = FakeIMAP(login_error=True)
    with pytest.raises(mailbox.MailboxError) as caught:
        run_sync(store, fake)
    saved = mailbox.load_settings(store)
    assert saved is not None
    assert saved.last_error == caught.value.message
    assert saved.last_sync_at == NOW
    assert saved.last_success_at is None
    assert PASSWORD not in caught.value.message
    assert PASSWORD not in (store.root / "mailbox.json").read_text(encoding="utf-8")


def test_a_successful_sync_clears_the_last_error(store):
    with pytest.raises(mailbox.MailboxError):
        run_sync(store, FakeIMAP(login_error=True))
    later = NOW + timedelta(hours=1)
    result = mailbox.sync(
        store,
        mailbox.load_settings(store),
        PASSWORD,
        [account()],
        {},
        FakeJournal(),
        now=later,
        connect=lambda host, port: FakeIMAP(),
    )
    saved = mailbox.load_settings(store)
    assert saved is not None
    assert saved.last_error is None
    assert saved.last_success_at == later
    assert saved.last_counts == result.counts


def test_a_missing_sent_folder_is_recorded(store):
    fake = FakeIMAP(lines=[INBOX_LINE])
    with pytest.raises(mailbox.MailboxError) as caught:
        run_sync(store, fake)
    assert caught.value.message == "Connected, but couldn't find a Sent folder."
    saved = mailbox.load_settings(store)
    assert saved is not None and saved.last_error == caught.value.message


def test_a_disconnect_during_the_sync_is_not_undone(store):
    fake = FakeIMAP({"INBOX": [raw_email(message_id="<in@acme.com>")], "Sent": []})
    fake.before_fetch = lambda conn: mailbox.disconnect(store)
    run_sync(store, fake)
    assert mailbox.load_settings(store) is None


def test_an_unexpected_failure_is_a_mailbox_error_and_is_recorded(store):
    fake = FakeIMAP({"INBOX": [raw_email(message_id="<in@acme.com>")], "Sent": []})
    failing = FakeJournal(fail=RuntimeError("disk full at C:\\secret\\path"))
    with pytest.raises(mailbox.MailboxError) as caught:
        run_sync(store, fake, journal_=failing)
    assert caught.value.message == "Wardmind couldn't finish the sync. It will try again."
    saved = mailbox.load_settings(store)
    assert saved is not None and saved.last_error == caught.value.message


def test_a_sync_logs_into_the_real_journal_once(store):
    real = journal.Journal(store, demo=False)
    fake = FakeIMAP({"INBOX": [raw_email(message_id="<m1@acme.com>")], "Sent": []})
    first = run_sync(store, fake, journal_=real)
    assert first.counts.logged == 1
    notes = real.for_account("acc-1").notes
    assert len(notes) == 1
    assert notes[0].source is journal.NoteSource.MAILBOX
    assert notes[0].message_id == "m1@acme.com"
    second = mailbox.sync(
        store,
        mailbox.load_settings(store),
        PASSWORD,
        [account()],
        {},
        real,
        now=NOW + timedelta(minutes=15),
        connect=lambda host, port: fake,
    )
    assert second.counts.already == 1
    assert len(real.for_account("acc-1").notes) == 1
