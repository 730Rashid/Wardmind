"""Locking the book: the lock gate, /unlock and /lock (web/app.py), the unlock page, the idle lock,
the MCP server's answers and the sync while locked. Encryption is turned on for real (vault.enable),
with Windows protection replaced by a reversible stand-in and Argon2 made cheap, so no test waits
for a real key derivation."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client

from wardmind.core import service, vault
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.settings import Settings, load_settings, save_settings
from wardmind.mcp import server
from wardmind.web import app as web
from wardmind.web import lock, session
from wardmind.web.session import SessionGate

BASE = "http://127.0.0.1"
ORIGIN = {"origin": BASE}
PASSWORD = "correct horse battery"
WRONG = "not the password at all"
WEB_DIR = Path(web.__file__).parent
TEMPLATES = WEB_DIR / "templates"
CSS = WEB_DIR / "static" / "css"
LOCKED_WRITE = "Wardmind is locked. Unlock it, then try again."


class FakeClock:
    """A clock the test moves by hand; the app's idle timer and unlock throttle read it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows protection replaced by a reversible stand-in, so the tests run anywhere."""

    def protect(data: bytes) -> bytes:
        return b"DP" + bytes(reversed(data))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_protect", protect)
    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)


@pytest.fixture(autouse=True)
def cheap_argon2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})


def _text(response) -> str:  # type: ignore[no-untyped-def]
    """Page text with entities decoded (Jinja escapes apostrophes in variables)."""
    return html.unescape(response.text)


def _client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app if app is not None else web.create_app(), base_url=BASE)


def _app_with_clock(clock: FakeClock) -> FastAPI:
    """An app whose idle timer and unlock throttle read the test's clock."""
    app = web.create_app()
    app.state.idle = lock.IdleTimer(clock)
    app.state.throttle = lock.UnlockThrottle(clock)
    return app


def _with_password() -> str:
    """Encryption on, with a password (no Windows slot). Returns the recovery key, shown once."""
    return vault.enable(password=PASSWORD)


def _with_windows() -> str:
    """Encryption on, unlocked through Windows protection only. Returns the recovery key."""
    return vault.enable(use_dpapi=True)


def _lock_now(client: TestClient) -> None:
    response = client.post("/lock", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303


def _unlock_post(client: TestClient, **fields: str):  # type: ignore[no-untyped-def]
    return client.post("/unlock", data=fields, headers=ORIGIN, follow_redirects=False)


# --- A plain book: nothing applies ------------------------------------------------------------


def test_a_plain_book_opens_with_no_lock_button() -> None:
    client = _client()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/lock"' not in response.text


def test_the_unlock_page_of_a_plain_book_goes_home() -> None:
    client = _client()
    response = client.get("/unlock", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_lock_on_a_plain_book_just_goes_home() -> None:
    client = _client()
    response = client.post("/lock", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert vault.state() is vault.State.PLAIN


# --- An encrypted book that is unlocked -------------------------------------------------------


def test_an_unlocked_book_opens_and_shows_lock_now_with_a_password() -> None:
    _with_password()
    response = _client().get("/", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/lock"' in response.text
    assert "Lock now" in response.text


def test_an_unlocked_book_opens_and_shows_lock_now_with_windows_protection() -> None:
    _with_windows()
    response = _client().get("/today", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/lock"' in response.text


# --- Locking, and what a locked book answers --------------------------------------------------


def test_lock_now_locks_and_sends_you_to_the_unlock_page() -> None:
    _with_password()
    client = _client()
    response = client.post("/lock", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/unlock"
    assert not vault.is_unlocked()
    again = client.get("/", follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"] == "/unlock?next=%2F"


def test_a_locked_page_remembers_where_you_were_going() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    today = client.get("/today?q=ann", follow_redirects=False)
    assert today.headers["location"] == "/unlock?next=%2Ftoday%3Fq%3Dann"
    account = client.get("/accounts/acc-001", follow_redirects=False)
    assert account.headers["location"] == "/unlock?next=%2Faccounts%2Facc-001"


def test_an_htmx_request_gets_an_hx_redirect_and_no_body() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/unlock?next=%2F"
    assert response.text == ""


def test_a_write_to_a_locked_book_is_refused_with_423_and_saves_nothing() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = client.post(
        "/settings/appearance", data={"theme": "dark"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 423
    assert response.text == LOCKED_WRITE
    assert load_settings().theme.value == "system"


def test_static_files_are_still_served_while_locked() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    assert client.get("/static/css/app.css").status_code == 200


# --- The unlock page --------------------------------------------------------------------------


def test_the_unlock_page_stands_alone_and_says_the_book_is_locked() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = client.get("/unlock", follow_redirects=False)
    assert response.status_code == 200
    assert "<title>Unlock - Wardmind</title>" in response.text
    assert "<h1" in response.text and "Wardmind is locked" in response.text
    assert 'href="/static/css/app.css"' in response.text
    assert 'class="nav-link"' not in response.text  # the layout (and its reads) is not used


def test_a_windows_book_offers_the_windows_button_and_a_closed_recovery_key() -> None:
    _with_windows()
    client = _client()
    _lock_now(client)
    text = client.get("/unlock").text
    assert 'name="windows" value="1"' in text
    assert 'type="password"' not in text
    assert '<details class="lock-disclosure">' in text
    assert 'name="recovery"' in text


def test_a_password_book_offers_a_password_field_and_the_recovery_key() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    text = client.get("/unlock").text
    assert 'name="windows"' not in text
    password_tag = re.search(r'<input[^>]*name="password"[^>]*>', text)
    assert password_tag is not None
    assert 'type="password"' in password_tag.group(0)
    assert 'autocomplete="current-password"' in password_tag.group(0)
    recovery_tag = re.search(r'<input[^>]*name="recovery"[^>]*>', text)
    assert recovery_tag is not None
    assert 'autocomplete="off"' in recovery_tag.group(0)
    assert 'spellcheck="false"' in recovery_tag.group(0)
    assert "Use your recovery key" in text


def test_the_unlock_page_keeps_the_next_address_it_was_given() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    assert 'name="next" value="/today"' in client.get("/unlock?next=/today").text


# --- Unlocking --------------------------------------------------------------------------------


def test_a_wrong_password_is_refused_in_plain_words_and_never_echoed() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, password=WRONG, next="/")
    assert response.status_code == 422
    assert "That password isn't right." in _text(response)
    assert WRONG not in response.text
    assert 'id="unlock-password-error"' in response.text
    assert 'role="alert"' in response.text
    assert not vault.is_unlocked()


def test_the_right_password_goes_to_next_and_opens_the_book() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, password=PASSWORD, next="/today")
    assert response.status_code == 303
    assert response.headers["location"] == "/today"
    assert vault.is_unlocked()
    assert 'action="/lock"' in client.get("/", follow_redirects=False).text


def test_the_right_password_without_a_next_goes_home() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, password=PASSWORD)
    assert response.headers["location"] == "/"


def test_a_blank_password_asks_for_one_and_is_not_counted_as_a_wrong_try() -> None:
    clock = FakeClock()
    app = _app_with_clock(clock)
    _with_password()
    client = _client(app)
    _lock_now(client)
    for _ in range(lock.FREE_FAILURES + 2):
        response = _unlock_post(client, password="", next="/")
        assert response.status_code == 422
    assert "Type your password." in _text(response)
    assert app.state.throttle.seconds_left() == 0


def test_the_recovery_key_unlocks_the_book() -> None:
    code = _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, recovery=code.replace("-", "").lower(), next="/")
    assert response.status_code == 303
    assert vault.is_unlocked()


def test_a_wrong_recovery_key_is_refused_with_its_key_field_open() -> None:
    code = _with_password()
    other = vault.new_recovery_code()
    assert other != code
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, recovery=other, next="/")
    assert response.status_code == 422
    assert "That recovery key doesn't open this book." in _text(response)
    assert other not in response.text
    assert '<details class="lock-disclosure" open>' in response.text
    assert not vault.is_unlocked()


def test_a_recovery_key_of_the_wrong_shape_is_refused_in_plain_words() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, recovery="abc", next="/")
    assert response.status_code == 422
    assert "That isn't a recovery key." in _text(response)


def test_the_windows_button_unlocks_a_windows_book() -> None:
    _with_windows()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, windows="1", next="/brief")
    assert response.status_code == 303
    assert response.headers["location"] == "/brief"
    assert vault.is_unlocked()


def test_the_windows_button_on_a_password_book_says_to_use_the_password() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, windows="1", next="/")
    assert response.status_code == 422
    assert "Unlock it with your password." in _text(response)


@pytest.mark.parametrize(
    "bad_next", ["//evil.example/", "https://evil.example/", "/\\evil.example"]
)
def test_a_next_address_that_leaves_this_app_is_sanitised_after_unlocking(bad_next: str) -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = _unlock_post(client, password=PASSWORD, next=bad_next)
    assert response.headers["location"] == "/"


def test_the_unlock_page_sanitises_next_before_showing_it() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    assert 'name="next" value="/"' in client.get("/unlock?next=//evil.example").text


def test_the_unlock_page_of_an_unlocked_book_goes_on_to_next() -> None:
    _with_password()
    client = _client()
    response = client.get("/unlock?next=/today", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/today"


def test_a_cross_site_unlock_is_refused_and_the_book_stays_locked() -> None:
    _with_password()
    client = _client()
    _lock_now(client)
    response = client.post(
        "/unlock",
        data={"password": PASSWORD, "next": "/"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert not vault.is_unlocked()


# --- The unlock throttle ----------------------------------------------------------------------


def test_five_wrong_tries_then_a_wait_of_two_seconds_even_for_the_right_password() -> None:
    clock = FakeClock()
    _with_password()
    client = _client(_app_with_clock(clock))
    _lock_now(client)
    for _ in range(lock.FREE_FAILURES):
        assert _unlock_post(client, password=WRONG, next="/").status_code == 422
    response = _unlock_post(client, password=WRONG, next="/")
    assert response.status_code == 429
    assert "Too many tries. Wait 2 seconds, then try again." in _text(response)
    assert _unlock_post(client, password=PASSWORD, next="/").status_code == 429
    assert not vault.is_unlocked()


def test_the_wait_counts_down_then_doubles_after_a_wrong_try() -> None:
    clock = FakeClock()
    _with_password()
    client = _client(_app_with_clock(clock))
    _lock_now(client)
    for _ in range(lock.FREE_FAILURES):
        _unlock_post(client, password=WRONG, next="/")
    clock.advance(1)
    response = _unlock_post(client, password=WRONG, next="/")
    assert response.status_code == 429
    assert "Wait 1 second, then try again." in _text(response)
    clock.advance(1)
    assert _unlock_post(client, password=WRONG, next="/").status_code == 422
    response = _unlock_post(client, password=WRONG, next="/")
    assert response.status_code == 429
    assert "Wait 4 seconds, then try again." in _text(response)


def test_a_right_password_after_the_wait_clears_the_count() -> None:
    clock = FakeClock()
    app = _app_with_clock(clock)
    _with_password()
    client = _client(app)
    _lock_now(client)
    for _ in range(lock.FREE_FAILURES):
        _unlock_post(client, password=WRONG, next="/")
    clock.advance(2)
    assert _unlock_post(client, password=PASSWORD, next="/").status_code == 303
    _lock_now(client)
    for _ in range(lock.FREE_FAILURES):
        assert _unlock_post(client, password=WRONG, next="/").status_code == 422


# --- Idle locking -----------------------------------------------------------------------------


def test_idle_past_the_setting_locks_on_the_next_request() -> None:
    clock = FakeClock()
    _with_password()
    client = _client(_app_with_clock(clock))
    assert client.get("/", follow_redirects=False).status_code == 200
    clock.advance(14 * 60)
    assert client.get("/", follow_redirects=False).status_code == 200
    clock.advance(15 * 60)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/unlock?next=%2F"
    assert not vault.is_unlocked()


def test_requests_within_the_setting_keep_the_book_open() -> None:
    clock = FakeClock()
    _with_password()
    client = _client(_app_with_clock(clock))
    for _ in range(3):
        clock.advance(10 * 60)
        assert client.get("/", follow_redirects=False).status_code == 200


def test_zero_minutes_never_locks_the_book() -> None:
    _with_password()
    save_settings(Settings(auto_lock_minutes=0))
    clock = FakeClock()
    client = _client(_app_with_clock(clock))
    clock.advance(3 * 24 * 3600)
    assert client.get("/", follow_redirects=False).status_code == 200


def test_static_files_do_not_count_as_use() -> None:
    clock = FakeClock()
    _with_password()
    client = _client(_app_with_clock(clock))
    client.get("/", follow_redirects=False)
    clock.advance(14 * 60)
    assert client.get("/static/css/app.css").status_code == 200
    clock.advance(2 * 60)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_the_background_check_locks_an_idle_book_without_a_request() -> None:
    clock = FakeClock()
    _with_password()
    app = _app_with_clock(clock)
    app.state.idle_watch.tick()
    assert vault.is_unlocked()
    clock.advance(16 * 60)
    app.state.idle_watch.tick()
    assert not vault.is_unlocked()


def test_the_idle_watch_runs_while_the_app_runs() -> None:
    app = web.create_app()
    with TestClient(app, base_url=BASE):
        assert app.state.idle_watch.running
    assert not app.state.idle_watch.running


# --- A damaged key file or Windows refusal is a 503, never an empty book ----------------------


def test_a_damaged_key_file_answers_503_with_its_message() -> None:
    _with_password()
    vault.keys_path().write_text("this is not a key file", encoding="utf-8")
    response = _client().get("/", follow_redirects=False)
    assert response.status_code == 503
    assert "key file is damaged" in response.text


def test_windows_refusing_the_key_answers_503_with_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_windows()
    vault.forget_key()

    def refuse(blob: bytes) -> bytes:
        raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")

    monkeypatch.setattr(vault, "dpapi_unprotect", refuse)
    response = _client().get("/", follow_redirects=False)
    assert response.status_code == 503
    assert "Windows couldn't unlock" in response.text


# --- The session check still comes first ------------------------------------------------------


@pytest.fixture
def gate() -> SessionGate:
    return SessionGate.new()


def test_a_locked_book_without_the_cookie_is_403_and_learns_nothing(gate: SessionGate) -> None:
    _with_password()
    vault.lock()
    client = _client(web.create_app(gate))
    for path in ("/", "/unlock", "/today", "/settings/privacy"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 403
        assert "location" not in response.headers
        assert response.text == session.LOCKED_OUT_MESSAGE


def test_without_the_cookie_lock_and_unlock_posts_are_refused(gate: SessionGate) -> None:
    _with_password()
    vault.lock()
    client = _client(web.create_app(gate))
    assert client.post("/lock", headers=ORIGIN, follow_redirects=False).status_code == 403
    response = client.post(
        "/unlock", data={"password": PASSWORD, "next": "/"}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 403
    assert not vault.is_unlocked()


def test_with_the_cookie_a_locked_book_goes_to_unlock_and_then_opens(gate: SessionGate) -> None:
    _with_password()
    vault.lock()
    client = _client(web.create_app(gate))
    entered = client.get(gate.entry_url("", "/today"), follow_redirects=False)
    assert entered.status_code == 303
    gated = client.get("/today", follow_redirects=False)
    assert gated.headers["location"] == "/unlock?next=%2Ftoday"
    assert client.get("/unlock", follow_redirects=False).status_code == 200
    response = _unlock_post(client, password=PASSWORD, next="/today")
    assert response.headers["location"] == "/today"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_static_files_are_open_to_a_locked_book_without_the_cookie(gate: SessionGate) -> None:
    _with_password()
    vault.lock()
    client = _client(web.create_app(gate))
    assert client.get("/static/css/app.css").status_code == 200


# --- Copy and markup of the new templates -----------------------------------------------------


def test_the_unlock_page_has_no_inline_script_style_or_event_handler() -> None:
    text = (TEMPLATES / "unlock.html").read_text(encoding="utf-8")
    assert "<script" not in text.lower()
    assert not re.search(r"\sstyle\s*=", text)
    assert not re.search(r"\son[a-z]+\s*=", text, flags=re.IGNORECASE)
    assert "{% extends" not in text
    assert "\r" not in text


def test_the_lock_styles_live_in_app_css() -> None:
    text = (CSS / "app.css").read_text(encoding="utf-8")
    for selector in (".lock-page", ".lock-card", ".lock-input", ".lock-now-button"):
        assert selector in text


def test_the_new_copy_has_no_arrows() -> None:
    for name in ("unlock.html", "base.html"):
        assert "→" not in (TEMPLATES / name).read_text(encoding="utf-8")


# --- Sync does nothing while the book is locked -----------------------------------------------


def test_sync_due_feeds_does_nothing_while_the_book_is_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_password()
    vault.lock()
    calls: list[object] = []
    monkeypatch.setattr(service, "housekeeping", lambda now=None: calls.append(now))
    monkeypatch.setattr(service, "_last_housekeeping", None)
    assert service.sync_due_feeds() == 0
    assert calls == []


def test_sync_due_feeds_runs_while_the_book_is_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_password()
    calls: list[object] = []
    monkeypatch.setattr(service, "housekeeping", lambda now=None: calls.append(now))
    monkeypatch.setattr(service, "remember_book", lambda: None)
    monkeypatch.setattr(service, "_last_housekeeping", None)
    assert service.sync_due_feeds() == 0  # no feeds set up, but the check ran
    assert len(calls) == 1


# --- The MCP server while the book is locked --------------------------------------------------

JOURNAL_JSON = '{"by_account": {}}'


@pytest.fixture
def demo_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())


def _seed_demo_journal() -> None:
    """The demo journal file, written before encryption turns on, so todays_list must read it."""
    from wardmind.core.store import DEMO_JOURNAL_FILE

    vault.write_text(service.get_store().root / DEMO_JOURNAL_FILE, JOURNAL_JSON)


def _error_text(result) -> str:  # type: ignore[no-untyped-def]
    assert result.is_error
    return result.content[0].text


async def test_a_locked_book_gives_the_mcp_client_a_tool_error_that_says_locked(
    demo_mcp: None,
) -> None:
    _seed_demo_journal()
    _with_password()
    vault.lock()
    async with Client(server.mcp) as client:
        result = await client.call_tool("todays_list", {})
    assert "Wardmind is locked" in _error_text(result)


async def test_an_unlocked_book_answers_the_same_tool(demo_mcp: None) -> None:
    _seed_demo_journal()
    _with_password()
    async with Client(server.mcp) as client:
        result = await client.call_tool("todays_list", {})
    assert not result.is_error


async def test_get_account_maps_a_locked_journal_read(
    demo_mcp: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = service.get_book()[0].account.id

    def locked(account: str):  # type: ignore[no-untyped-def]
        raise vault.Locked("Wardmind is locked.")

    monkeypatch.setattr(service, "account_journal", locked)
    async with Client(server.mcp) as client:
        result = await client.call_tool("get_account", {"account_id": account_id})
    assert "Wardmind is locked" in _error_text(result)


async def test_weekly_brief_maps_a_locked_history_read(
    demo_mcp: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise vault.Locked("Wardmind is locked.")

    monkeypatch.setattr(service, "week_changes", locked)
    async with Client(server.mcp) as client:
        result = await client.call_tool("weekly_brief", {})
    assert "Wardmind is locked" in _error_text(result)


async def test_research_account_maps_a_locked_summary_read(
    demo_mcp: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = service.get_book()[0].account.id

    class _Result:
        def model_dump(self, mode: str) -> dict[str, object]:
            return {
                "account_id": account_id,
                "site_title": "Title",
                "site_description": "",
                "feed_url": None,
                "articles": [],
            }

    def locked_summary(account: str):  # type: ignore[no-untyped-def]
        raise vault.Locked("Wardmind is locked.")

    monkeypatch.setattr(service, "research_account", lambda *a, **k: _Result())
    monkeypatch.setattr(service, "saved_summary", locked_summary)
    async with Client(server.mcp) as client:
        result = await client.call_tool("research_account", {"account_id": account_id})
    assert "Wardmind is locked" in _error_text(result)


async def test_a_damaged_book_gives_a_tool_error_that_says_it_cannot_be_read(
    demo_mcp: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def damaged(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise vault.Damaged("Wardmind's key file is damaged.")

    monkeypatch.setattr(service, "get_book", damaged)
    async with Client(server.mcp) as client:
        result = await client.call_tool("list_book", {})
    text = _error_text(result)
    assert "Wardmind can't read its data: Wardmind's key file is damaged." in text
