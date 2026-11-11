"""The mailbox functions in core/service.py: connecting and disconnecting, pausing, Sync now, the
scheduler's due check and tick, and Delete all local data. The IMAP connection is a FakeIMAP that
replaces mailbox._connect, so no test reaches a mail server."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mailbox_fake import NOW, PASSWORD, FakeIMAP, raw_email
from wardmind.core import journal, mailbox, secrets, service
from wardmind.core.models import Account, Milestones
from wardmind.core.store import ConnectionKind, ManualData


@pytest.fixture
def fake(monkeypatch) -> FakeIMAP:
    """The one IMAP connection every service call uses (set its fields to change the server)."""
    imap = FakeIMAP()
    monkeypatch.setattr(mailbox, "_connect", lambda host, port: imap)
    return imap


def root():
    return service.get_store().root


def connect(username: str = "csm@wardmind.test", aliases: str = "", password: str = PASSWORD):
    return service.connect_mailbox(mailbox.Provider.GMAIL, "", username, aliases, password)


def save_connected(*, password: bool = True, **overrides) -> mailbox.MailboxSettings:
    """A connected mailbox written straight to the store (no login)."""
    base: dict = {
        "provider": mailbox.Provider.GMAIL,
        "host": "imap.gmail.com",
        "username": "csm@wardmind.test",
        "own_addresses": ["csm@wardmind.test"],
        "connected_at": NOW - timedelta(days=3),
    }
    base.update(overrides)
    saved = mailbox.MailboxSettings(**base)
    mailbox.save_settings(service.get_store(), saved)
    if password:
        secrets.save_secret(mailbox.SECRET_NAME, PASSWORD)
    return saved


# --- Connecting -------------------------------------------------------------------------------


def test_connect_saves_the_settings_and_the_password_and_runs_a_sync(fake):
    fake.folders["INBOX"] = [raw_email(message_id="<in@other.example>")]
    saved = service.connect_mailbox(
        mailbox.Provider.GMAIL,
        "",
        " CSM@Wardmind.test ",
        "alias@wardmind.test\ncsm@wardmind.test",
        "app pass word",
    )
    assert saved.host == "imap.gmail.com"
    assert saved.username == "csm@wardmind.test"
    assert saved.own_addresses == ["csm@wardmind.test", "alias@wardmind.test"]
    assert saved.enabled is True
    assert saved.last_success_at is not None  # the sync ran after the save
    assert secrets.get_secret(mailbox.SECRET_NAME) == "app pass word"
    assert "app pass word" not in (root() / "mailbox.json").read_text(encoding="utf-8")
    assert fake.of("search")  # the sync searched both folders


def test_another_provider_uses_the_host_that_was_typed(fake):
    saved = service.connect_mailbox(
        mailbox.Provider.OTHER, "IMAP.Example.com", "csm@wardmind.test", "", PASSWORD
    )
    assert saved.host == "imap.example.com"


@pytest.mark.parametrize(
    "provider, host, username, aliases, password, message",
    [
        (
            mailbox.Provider.OTHER,
            "https://imap.example.com",
            "csm@wardmind.test",
            "",
            PASSWORD,
            "Type the server's name, like imap.example.com.",
        ),
        (
            mailbox.Provider.GMAIL,
            "",
            "   ",
            "",
            PASSWORD,
            "Type the email address you sign in with.",
        ),
        (
            mailbox.Provider.GMAIL,
            "",
            "csm@wardmind.test",
            "",
            "   ",
            "Type the key, token or password first.",
        ),
    ],
    ids=["bad-host", "blank-username", "blank-password"],
)
def test_a_bad_field_is_refused_and_nothing_is_saved(
    fake, provider, host, username, aliases, password, message
):
    with pytest.raises(ValueError) as caught:
        service.connect_mailbox(provider, host, username, aliases, password)
    assert str(caught.value) == message
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None


def test_a_failed_login_saves_nothing(fake):
    fake.login_error = True
    with pytest.raises(mailbox.MailboxError):
        connect()
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None


def test_a_failed_first_sync_does_not_fail_the_connect(fake, monkeypatch):
    def broken_sync():
        raise mailbox.MailboxError("Wardmind couldn't finish the sync. It will try again.")

    monkeypatch.setattr(service, "sync_mailbox_now", broken_sync)
    saved = connect()
    assert saved.username == "csm@wardmind.test"
    assert service.mailbox_settings() is not None


def test_disconnect_removes_the_connection_and_password_and_keeps_the_activity(fake):
    connect()
    (root() / "journal.json").write_text('{"by_account": {}}', encoding="utf-8")
    service.disconnect_mailbox()
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None
    assert (root() / "journal.json").exists()


# --- Pausing ----------------------------------------------------------------------------------


def test_pausing_and_resuming_keeps_the_rest_of_the_settings(fake):
    connect()
    service.set_mailbox_enabled(False)
    paused = service.mailbox_settings()
    assert paused is not None and paused.enabled is False
    assert paused.username == "csm@wardmind.test"
    service.set_mailbox_enabled(True)
    assert service.mailbox_settings().enabled is True


def test_pausing_without_a_connection_says_to_connect_first():
    with pytest.raises(mailbox.MailboxError, match="Connect a mailbox first."):
        service.set_mailbox_enabled(False)


# --- Sync now ---------------------------------------------------------------------------------


def test_sync_now_needs_a_connection():
    with pytest.raises(mailbox.MailboxError, match="Connect a mailbox first."):
        service.sync_mailbox_now()


def test_sync_now_needs_the_password(fake):
    save_connected(password=False)
    with pytest.raises(mailbox.MailboxError, match="The mailbox password is missing"):
        service.sync_mailbox_now()


def test_sync_now_logs_into_your_data_even_in_demo_mode(fake):
    """The sync matches Your data's accounts whatever the page shows (demo is the default here)."""
    store = service.get_store()
    store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    store.save_manual(
        ManualData(
            accounts=[
                Account(
                    id="m-00000001",
                    name="Acme",
                    arr=10_000,
                    tier="Pro",
                    owner="Sam",
                    renewal_date=NOW.date(),
                    milestones=Milestones(done=0, total=0),
                    domain="acme.com",
                )
            ]
        )
    )
    fake.folders["INBOX"] = [raw_email(message_id="<jane@acme.com>")]
    save_connected()
    result = service.sync_mailbox_now()
    assert result.counts.logged == 1
    # Read the Your data journal (not the demo one, which the page would show in demo mode).
    notes = journal.Journal(store, demo=False).for_account("m-00000001").notes
    assert [note.message_id for note in notes] == ["jane@acme.com"]


# --- The scheduler: due check and tick --------------------------------------------------------


def test_due_needs_a_connection_a_password_to_be_enabled_and_the_interval(fake):
    assert service.mailbox_due(NOW) is False  # not connected
    save_connected(password=False)
    assert service.mailbox_due(NOW) is False  # no password
    save_connected(last_sync_at=None)
    assert service.mailbox_due(NOW) is True  # never synced
    save_connected(last_sync_at=NOW - timedelta(minutes=14))
    assert service.mailbox_due(NOW) is False
    save_connected(last_sync_at=NOW - timedelta(minutes=mailbox.SYNC_MINUTES))
    assert service.mailbox_due(NOW) is True
    save_connected(enabled=False, last_sync_at=None)
    assert service.mailbox_due(NOW) is False  # paused


def test_the_tick_syncs_a_due_mailbox(fake, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(service, "sync_mailbox_now", lambda: calls.append(1))
    save_connected(last_sync_at=None)
    service.sync_due_feeds()
    assert calls == [1]


def test_the_tick_leaves_a_mailbox_that_is_not_due_alone(fake, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(service, "sync_mailbox_now", lambda: calls.append(1))
    save_connected(last_sync_at=service._utc_now())
    service.sync_due_feeds()
    assert calls == []


def test_the_tick_swallows_every_error(fake, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("something went very wrong")

    monkeypatch.setattr(mailbox, "sync", boom)
    save_connected(last_sync_at=None)
    assert service.sync_due_feeds() == 0  # no feed, and no exception from the mailbox


def test_a_failed_scheduled_sync_is_recorded_in_the_settings(fake):
    fake.login_error = True
    save_connected(last_sync_at=None)
    service.sync_due_feeds()  # must not raise
    saved = service.mailbox_settings()
    assert saved is not None
    assert saved.last_error is not None
    assert PASSWORD not in saved.last_error


# --- Delete all local data --------------------------------------------------------------------


def test_delete_all_local_data_removes_the_mailbox_and_its_password(fake):
    connect()
    assert (root() / "mailbox.json").exists()
    service.delete_all_local_data()
    assert not (root() / "mailbox.json").exists()
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None
    assert not (secrets.secrets_dir() / secrets.FEED_SECRETS_DIR / mailbox.SECRET_NAME).exists()
