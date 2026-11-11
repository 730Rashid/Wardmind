"""Settings > Mailbox in the web UI: the page in both states, connecting and the refusals, Sync now,
pause or resume, disconnect, the nav entry, unknown notices and cross-site writes.

The IMAP connection is FakeIMAP (mailbox_fake.py), which replaces mailbox._connect, so no test
reaches a mail server. PASSWORD is a fake app password: no response may ever contain it."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mailbox_fake import PASSWORD, FakeIMAP, raw_email
from wardmind.core import mailbox, secrets, service
from wardmind.core.settings import DataSource, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
CROSS_SITE = {"origin": "https://evil.example"}
USERNAME = "csm@wardmind.test"
SETTINGS_LINK = re.compile(r'<a class="settings-link" href="([^"]+)"([^>]*)>')
SETTINGS_PAGES = (
    "/settings/appearance",
    "/settings/scoring",
    "/settings/connections",
    "/settings/privacy",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeIMAP:
    """The one IMAP connection every request uses (set its fields to change the server)."""
    imap = FakeIMAP()
    monkeypatch.setattr(mailbox, "_connect", lambda host, port: imap)
    return imap


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _saved(*, password: bool = True, **overrides: Any) -> mailbox.MailboxSettings:
    """A connected mailbox written straight to the store, with no login (as the service tests)."""
    base: dict[str, Any] = {
        "provider": mailbox.Provider.GMAIL,
        "host": "imap.gmail.com",
        "username": USERNAME,
        "own_addresses": [USERNAME],
        "connected_at": _now() - timedelta(days=3),
    }
    base.update(overrides)
    saved = mailbox.MailboxSettings(**base)
    mailbox.save_settings(service.get_store(), saved)
    if password:
        secrets.save_secret(mailbox.SECRET_NAME, PASSWORD)
    return saved


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "provider": "gmail",
        "host": "",
        "username": USERNAME,
        "aliases": "",
        "password": PASSWORD,
    }
    data.update(overrides)
    return data


def _connect(client: TestClient, **overrides: str) -> Any:
    return client.post(
        "/settings/mailbox/connect", data=_form(**overrides), headers=ORIGIN, follow_redirects=False
    )


def _input(text: str, field: str) -> str:
    """The start tag of the input, select or textarea with this id."""
    match = re.search(rf'<(?:input|select|textarea)[^>]*\sid="{field}"[^>]*>', text)
    assert match is not None, f"no field {field} on the page"
    return match.group(0)


def _your_data() -> None:
    save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))


# --- The page: not connected ----------------------------------------------------------------


def test_the_page_has_its_title_and_is_in_the_settings_navigation(client: TestClient) -> None:
    response = client.get("/settings/mailbox")
    assert response.status_code == 200
    assert "<title>Mailbox - Wardmind</title>" in response.text
    links = SETTINGS_LINK.findall(response.text)
    assert [href for href, _ in links] == [
        "/settings/appearance",
        "/settings/scoring",
        "/settings/books",
        "/settings/connections",
        "/settings/mailbox",
        "/settings/privacy",
    ]
    assert [href for href, rest in links if 'aria-current="page"' in rest] == ["/settings/mailbox"]


@pytest.mark.parametrize("path", SETTINGS_PAGES)
def test_the_mailbox_link_is_in_the_navigation_of_every_settings_page(
    client: TestClient, path: str
) -> None:
    hrefs = [href for href, _ in SETTINGS_LINK.findall(client.get(path).text)]
    assert "/settings/mailbox" in hrefs
    assert hrefs.index("/settings/mailbox") == hrefs.index("/settings/connections") + 1


def test_not_connected_explains_what_is_logged_and_stored(client: TestClient) -> None:
    text = _text(client.get("/settings/mailbox"))
    assert (
        "Only emails to or from a client's contacts or website domain are saved, as a short "
        "excerpt. Wardmind never sends, moves, deletes or marks mail as read."
    ) in text
    assert (
        "Gmail, Outlook, iCloud, Yahoo or Fastmail: create one in your account's security settings."
        in text
    )
    assert "Connect mailbox" in text
    assert "Pause automatic logging" not in text


def test_the_connect_form_offers_each_provider_and_the_host_is_for_other_only(
    client: TestClient,
) -> None:
    text = _text(client.get("/settings/mailbox"))
    options = re.findall(r'<option value="([^"]+)"', text)
    assert options == ["gmail", "outlook", "icloud", "yahoo", "fastmail", "other"]
    assert "Gmail or Google Workspace" in text
    assert "Another provider" in text
    assert "Only for Another provider" in text


def test_the_password_field_is_empty_and_asks_for_a_new_password(client: TestClient) -> None:
    tag = _input(client.get("/settings/mailbox").text, "field-mailbox-password")
    assert 'type="password"' in tag
    assert 'name="password"' in tag
    assert 'autocomplete="new-password"' in tag
    assert "value=" not in tag


def test_every_field_has_a_label(client: TestClient) -> None:
    text = client.get("/settings/mailbox").text
    form = re.search(r'<form class="settings-form mailbox-form".*?</form>', text, re.S)
    assert form is not None, "no connect form on the page"
    fields = re.findall(r'<(?:input|select|textarea)[^>]*?\sid="([^"]+)"', form.group(0))
    assert fields == [
        "field-mailbox-provider",
        "field-mailbox-host",
        "field-mailbox-username",
        "field-mailbox-aliases",
        "field-mailbox-password",
    ]
    for field in fields:
        assert f'<label for="{field}">' in text


def test_the_page_has_no_inline_style_attributes_or_inline_scripts(client: TestClient) -> None:
    text = client.get("/settings/mailbox").text
    assert not re.search(r"\sstyle=", text)
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", text)


def test_demo_data_says_emails_are_logged_to_your_data_only(client: TestClient) -> None:
    assert "Emails are only logged to accounts in your own data." in _text(
        client.get("/settings/mailbox")
    )


def test_your_data_does_not_show_the_demo_line(client: TestClient) -> None:
    _your_data()
    assert "Emails are only logged to accounts in your own data." not in _text(
        client.get("/settings/mailbox")
    )


# --- The page: connected --------------------------------------------------------------------


def test_connected_shows_the_status_the_counts_and_the_actions(client: TestClient) -> None:
    _saved(
        last_sync_at=_now() - timedelta(minutes=4),
        last_success_at=_now() - timedelta(minutes=4),
        last_counts=mailbox.SyncCounts(read=340, logged=12, already=3, unmatched=325),
    )
    response = client.get("/settings/mailbox")
    text = _text(response)
    assert "Gmail or Google Workspace" in text
    assert USERNAME in text
    assert "Automatic logging is on." in text
    assert "Last checked 4 minutes ago." in text
    assert "12 emails logged from 340 read. 3 were already logged." in text
    assert 'action="/settings/mailbox/sync"' in text
    assert "Sync now" in text
    assert "Pause automatic logging" in text
    assert 'action="/settings/mailbox/disconnect"' in text
    assert "Emails already logged stay in each account's history." in text
    assert 'name="password"' not in text, "the connect form is only for a new connection"


def test_paused_shows_resume_and_the_hidden_value_that_resumes(client: TestClient) -> None:
    _saved(enabled=False)
    text = _text(client.get("/settings/mailbox"))
    assert "Automatic logging is paused." in text
    assert "Resume automatic logging" in text
    assert 'name="on" value="1"' in text


def test_the_last_check_is_not_shown_before_the_first_check(client: TestClient) -> None:
    _saved()
    assert "Not checked yet." in _text(client.get("/settings/mailbox"))


def test_a_failed_last_check_shows_its_reason_in_an_alert(client: TestClient) -> None:
    reason = "Couldn't reach imap.gmail.com. Check the server name."
    _saved(last_sync_at=_now(), last_error=reason)
    text = _text(client.get("/settings/mailbox"))
    assert f'role="alert">{reason}</p>' in text


def test_a_missing_password_says_to_disconnect_and_connect_again(client: TestClient) -> None:
    _saved(password=False)
    assert "The mailbox password is missing. Disconnect, then connect it again." in _text(
        client.get("/settings/mailbox")
    )


# --- Connecting -----------------------------------------------------------------------------


def test_connecting_saves_the_mailbox_and_redirects_with_the_connected_notice(
    client: TestClient, fake: FakeIMAP
) -> None:
    response = _connect(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/mailbox?notice=connected"
    saved = service.mailbox_settings()
    assert saved is not None
    assert saved.provider is mailbox.Provider.GMAIL
    assert saved.host == "imap.gmail.com"
    assert secrets.get_secret(mailbox.SECRET_NAME) == PASSWORD
    assert fake.of("search"), "the first sync ran"


def test_the_password_is_in_no_response_after_a_connect(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client)
    assert PASSWORD not in response.headers["location"]
    page = client.get(response.headers["location"])
    assert PASSWORD not in page.text
    assert "Mailbox connected." in _text(page)
    assert "Automatic logging is on." in _text(page)


def test_a_refused_login_re_renders_the_message_and_not_the_password(
    client: TestClient, fake: FakeIMAP
) -> None:
    fake.login_error = True
    response = _connect(client, aliases="other@wardmind.test")
    assert response.status_code == 422
    text = _text(response)
    assert "didn't accept that address and password" in text
    assert 'id="mailbox-connect-error"' in text
    assert f'value="{USERNAME}"' in response.text
    assert "other@wardmind.test" in response.text
    assert PASSWORD not in response.text
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None


def test_a_blank_username_is_refused_next_to_its_field(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, username="   ")
    assert response.status_code == 422
    tag = _input(response.text, "field-mailbox-username")
    assert 'aria-invalid="true"' in tag
    assert 'aria-describedby="field-mailbox-username-error field-mailbox-username-help"' in tag
    assert (
        '<p class="field-error" role="alert" id="field-mailbox-username-error">'
        "Type the email address you sign in with.</p>"
    ) in response.text
    assert fake.calls == []
    assert service.mailbox_settings() is None


def test_a_blank_password_is_refused_with_the_message_and_nothing_is_saved(
    client: TestClient, fake: FakeIMAP
) -> None:
    response = _connect(client, password="   ")
    assert response.status_code == 422
    assert "Type the key, token or password first." in _text(response)
    assert 'id="field-mailbox-password-error"' in response.text
    assert 'aria-invalid="true"' in _input(response.text, "field-mailbox-password")
    assert fake.calls == []
    assert service.mailbox_settings() is None


def test_other_requires_a_server_name(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, provider="other", host="")
    assert response.status_code == 422
    assert "Type the server's name, like imap.example.com." in _text(response)
    assert 'aria-invalid="true"' in _input(response.text, "field-mailbox-host")
    assert fake.calls == []


def test_other_refuses_a_server_name_with_a_scheme(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, provider="other", host="https://imap.example.com")
    assert response.status_code == 422
    assert "Type the server's name, like imap.example.com." in _text(response)
    assert fake.calls == []


def test_other_connects_to_the_server_that_was_typed(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, provider="other", host="IMAP.Example.com")
    assert response.status_code == 303
    saved = service.mailbox_settings()
    assert saved is not None
    assert saved.provider is mailbox.Provider.OTHER
    assert saved.host == "imap.example.com"


def test_a_named_provider_ignores_the_host_field(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, provider="fastmail", host="not a host at all")
    assert response.status_code == 303
    saved = service.mailbox_settings()
    assert saved is not None
    assert saved.host == "imap.fastmail.com"


def test_an_unknown_provider_is_refused(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, provider="hotmail")
    assert response.status_code == 422
    assert "Choose your mail provider." in _text(response)
    assert fake.calls == []


def test_bad_aliases_are_refused_next_to_the_box(client: TestClient, fake: FakeIMAP) -> None:
    response = _connect(client, aliases="not-an-address")
    assert response.status_code == 422
    assert "“not-an-address” isn't an email address." in _text(response)
    assert 'aria-invalid="true"' in _input(response.text, "field-mailbox-aliases")
    assert fake.calls == []


def test_the_typed_values_come_back_but_never_the_password(
    client: TestClient, fake: FakeIMAP
) -> None:
    response = _connect(client, provider="other", host="imap.example.com", username="   ")
    assert response.status_code == 422
    assert '<option value="other" selected>' in response.text
    assert 'value="imap.example.com"' in response.text
    assert PASSWORD not in response.text


# --- Sync now -------------------------------------------------------------------------------


def test_sync_now_checks_and_redirects_with_the_synced_notice(
    client: TestClient, fake: FakeIMAP
) -> None:
    fake.folders["INBOX"] = [raw_email(message_id="<unmatched@elsewhere.example>")]
    _saved()
    response = client.post("/settings/mailbox/sync", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/mailbox?notice=synced"
    page = _text(client.get(response.headers["location"]))
    assert "Mailbox checked. The counts are below." in page
    assert "0 emails logged from 1 read." in page
    assert fake.of("search")


def test_a_failed_check_redirects_and_the_page_shows_the_reason(
    client: TestClient, fake: FakeIMAP
) -> None:
    fake.login_error = True
    _saved()
    response = client.post("/settings/mailbox/sync", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/settings/mailbox?notice=sync-failed"
    page = client.get(response.headers["location"])
    assert "The check didn't finish. The reason is below." in _text(page)
    assert "didn't accept that address and password" in _text(page)
    assert PASSWORD not in page.text


def test_sync_now_with_no_mailbox_says_to_connect_first(client: TestClient) -> None:
    response = client.post("/settings/mailbox/sync", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/settings/mailbox?notice=not-connected"
    assert "Connect a mailbox first." in _text(client.get(response.headers["location"]))


def test_sync_now_with_the_password_missing_shows_why(client: TestClient, fake: FakeIMAP) -> None:
    _saved(password=False)
    response = client.post("/settings/mailbox/sync", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/settings/mailbox?notice=sync-failed"
    page = _text(client.get(response.headers["location"]))
    assert "The mailbox password is missing. Disconnect, then connect it again." in page
    assert fake.calls == []


# --- Pause and resume ---------------------------------------------------------------------


def test_pausing_saves_and_says_so(client: TestClient) -> None:
    _saved()
    response = client.post(
        "/settings/mailbox/enabled", data={"on": "0"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.headers["location"] == "/settings/mailbox?notice=paused"
    saved = service.mailbox_settings()
    assert saved is not None and saved.enabled is False
    assert "Automatic logging is paused." in _text(client.get(response.headers["location"]))


def test_resuming_saves_and_says_so(client: TestClient) -> None:
    _saved(enabled=False)
    response = client.post(
        "/settings/mailbox/enabled", data={"on": "1"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.headers["location"] == "/settings/mailbox?notice=resumed"
    saved = service.mailbox_settings()
    assert saved is not None and saved.enabled is True
    assert "Automatic logging is on again." in _text(client.get(response.headers["location"]))


def test_pausing_with_no_mailbox_says_to_connect_first(client: TestClient) -> None:
    response = client.post(
        "/settings/mailbox/enabled", data={"on": "0"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.headers["location"] == "/settings/mailbox?notice=not-connected"


@pytest.mark.parametrize("bad", ["", "maybe", "2", "true"])
def test_an_invalid_toggle_is_refused_and_nothing_changes(client: TestClient, bad: str) -> None:
    _saved()
    response = client.post(
        "/settings/mailbox/enabled", data={"on": bad}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 422
    assert "Choose pause or resume, then try again." in _text(response)
    saved = service.mailbox_settings()
    assert saved is not None and saved.enabled is True


# --- Disconnect -----------------------------------------------------------------------------


def test_disconnect_removes_the_settings_and_password_and_says_so(client: TestClient) -> None:
    _saved()
    response = client.post("/settings/mailbox/disconnect", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/mailbox?notice=disconnected"
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None
    page = _text(client.get(response.headers["location"]))
    assert "Mailbox disconnected. Emails already logged stay in each account's history." in page
    assert 'name="password"' in page, "the connect form is back"


# --- Notices --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notice, message",
    [
        ("connected", "Mailbox connected."),
        ("paused", "Automatic logging is paused."),
        ("resumed", "Automatic logging is on again."),
    ],
)
def test_a_known_notice_is_shown(client: TestClient, notice: str, message: str) -> None:
    response = client.get(f"/settings/mailbox?notice={notice}")
    assert f'role="status">{message}</p>' in response.text


@pytest.mark.parametrize("notice", ["bogus", "<b>x</b>", "connected%20now", "synced2"])
def test_an_unknown_notice_shows_nothing(client: TestClient, notice: str) -> None:
    response = client.get(f"/settings/mailbox?notice={notice}")
    assert response.status_code == 200
    assert 'class="settings-status"' not in response.text
    assert "bogus" not in response.text


# --- Writes from another site are refused -----------------------------------------------------


@pytest.mark.parametrize(
    "path, data",
    [
        ("/settings/mailbox/sync", {}),
        ("/settings/mailbox/enabled", {"on": "0"}),
        ("/settings/mailbox/disconnect", {}),
    ],
)
def test_a_cross_site_post_changes_nothing(
    client: TestClient, fake: FakeIMAP, path: str, data: dict[str, str]
) -> None:
    before = _saved()
    response = client.post(path, data=data, headers=CROSS_SITE, follow_redirects=False)
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    assert service.mailbox_settings() == before
    assert secrets.get_secret(mailbox.SECRET_NAME) == PASSWORD
    assert fake.calls == []


def test_a_cross_site_connect_saves_nothing(client: TestClient, fake: FakeIMAP) -> None:
    response = client.post(
        "/settings/mailbox/connect", data=_form(), headers=CROSS_SITE, follow_redirects=False
    )
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    assert service.mailbox_settings() is None
    assert secrets.get_secret(mailbox.SECRET_NAME) is None
    assert fake.calls == []
