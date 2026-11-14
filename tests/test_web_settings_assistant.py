"""Settings > Assistant: the page and its sidebar entry, saving each field (303 on success; 422
with a message and nothing written on a bad value), the saved state, the activity log entry, and
the "Added by Claude" and "From a suggestion" labels on the account page."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import audit, editing, journal, service
from wardmind.core.settings import DataSource, McpWrites, Settings, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
PAGE = "/settings/assistant"
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)


class _Inputs(HTMLParser):
    """Collects the attributes of every input tag, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            self.found.append(dict(attrs))


def _checked(markup: str) -> set[tuple[str, str | None]]:
    """(name, value) of every checked input."""
    parser = _Inputs()
    parser.feed(markup)
    return {
        (attrs.get("name") or "", attrs.get("value"))
        for attrs in parser.found
        if "checked" in attrs
    }


def _post(client: TestClient, data: dict[str, str]) -> Any:
    return client.post(PAGE, data=data, headers=ORIGIN, follow_redirects=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture(autouse=True)
def _defaults() -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()


# --- The page -----------------------------------------------------------------------------------


def test_the_page_shows_the_defaults(client: TestClient) -> None:
    response = client.get(PAGE)
    assert response.status_code == 200
    assert "<title>Assistant - Wardmind</title>" in response.text
    assert re.search(r'href="/settings/assistant"[^>]*aria-current="page"', response.text)
    assert _checked(response.text) == {
        ("suggestions", "1"),
        ("suggestions_learn", "1"),
        ("mcp_writes", "off"),
    }


def test_the_page_explains_each_level_and_what_claude_never_does(client: TestClient) -> None:
    text = html.unescape(client.get(PAGE).text)
    assert "Claude can read your book but can't change it." in text
    assert "Each one waits on Today until you accept it." in text
    assert "It shows a preview first and makes the change only when it is asked to confirm." in text
    assert "It never deletes or edits anything" in text
    assert "Wardmind never sends an email." in text


def test_the_page_has_no_inline_script_or_style(client: TestClient) -> None:
    response = client.get(PAGE)
    assert not INLINE.search(response.text)


# --- Saving -------------------------------------------------------------------------------------


def test_each_field_saves_and_the_page_shows_the_saved_state(client: TestClient) -> None:
    response = _post(
        client,
        {"mcp_writes": "direct", "ai_drafts": "1", "ai_drafts_cloud": "1"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/assistant?saved=1"
    saved = load_settings()
    assert saved.suggestions is False  # not posted, so off
    assert saved.mcp_writes is McpWrites.DIRECT
    assert saved.ai_drafts is True
    assert saved.ai_drafts_cloud is True

    shown = client.get(response.headers["location"])
    assert "Assistant settings saved." in shown.text
    assert _checked(shown.text) == {
        ("mcp_writes", "direct"),
        ("ai_drafts", "1"),
        ("ai_drafts_cloud", "1"),
    }


def test_suggest_only_and_suggestions_on_save(client: TestClient) -> None:
    response = _post(client, {"suggestions": "1", "mcp_writes": "propose"})
    assert response.status_code == 303
    saved = load_settings()
    assert saved.suggestions is True
    assert saved.mcp_writes is McpWrites.PROPOSE
    assert saved.ai_drafts is False
    assert saved.ai_drafts_cloud is False


def test_cloud_drafts_are_saved_off_without_the_wording(client: TestClient) -> None:
    _post(client, {"mcp_writes": "off", "ai_drafts_cloud": "1"})
    saved = load_settings()
    assert saved.ai_drafts is False
    assert saved.ai_drafts_cloud is False


def test_a_bad_mode_is_refused_and_nothing_is_saved(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.DEMO, suggestions=False))
    response = _post(client, {"suggestions": "1", "mcp_writes": "sometimes"})
    assert response.status_code == 422
    assert "Pick Off, Suggest only or Make changes." in response.text
    assert "Nothing was saved." in response.text
    saved = load_settings()
    assert saved.suggestions is False
    assert saved.mcp_writes is McpWrites.OFF


def test_a_missing_mode_is_refused(client: TestClient) -> None:
    response = _post(client, {"suggestions": "1"})
    assert response.status_code == 422
    assert "Pick Off, Suggest only or Make changes." in response.text
    assert load_settings().mcp_writes is McpWrites.OFF


def test_a_saved_change_goes_into_the_activity_log(client: TestClient) -> None:
    _post(client, {"mcp_writes": "propose"})
    saved = [entry for entry in audit.entries() if entry.event is audit.Event.SETTINGS_SAVED]
    assert [entry.detail for entry in saved] == ["Assistant"]


def test_a_refused_save_is_not_logged(client: TestClient) -> None:
    _post(client, {"mcp_writes": "nope"})
    assert not [entry for entry in audit.entries() if entry.event is audit.Event.SETTINGS_SAVED]


# --- The account page -----------------------------------------------------------------------------


def _account(name: str) -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$184,000", "renewal_date": "2027-06-01"},
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _page(client: TestClient, account_id: str) -> str:
    response = client.get(f"/accounts/{account_id}")
    assert response.status_code == 200
    return html.unescape(response.text)


def test_a_note_claude_added_says_so_on_the_account_page(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()
    account_id = _account("Acme Robotics")
    service.get_journal().add_note(
        account_id,
        journal.TouchKind.NOTE,
        "Asked for a quote on seats",
        TODAY,
        now=NOW,
        today=TODAY,
        source=journal.NoteSource.AGENT,
    )
    page = _page(client, account_id)
    assert "Added by Claude" in page
    assert page.index("Added by Claude") > page.index('id="notes"')


def test_a_note_from_a_suggestion_says_so_and_your_own_notes_do_not(client: TestClient) -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()
    account_id = _account("Beta Freight")
    journal_api = service.get_journal()
    journal_api.add_note(
        account_id,
        journal.TouchKind.NOTE,
        "Accepted from Today",
        TODAY,
        now=NOW,
        today=TODAY,
        source=journal.NoteSource.SUGGESTION,
    )
    journal_api.add_note(
        account_id,
        journal.TouchKind.NOTE,
        "Typed by hand",
        TODAY,
        now=NOW,
        today=TODAY,
    )
    page = _page(client, account_id)
    assert page.count("From a suggestion") == 1
    assert "Added by Claude" not in page
    assert "Typed by hand" in page
