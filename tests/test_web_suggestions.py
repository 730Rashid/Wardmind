"""Suggested for you on Today (web/proposal_pages.py): the section and its cards, Accept (with and
without JavaScript, and with changes), Dismiss, the refused and gone cases, the find box, the hidden
section, and a Today page that still renders when the day's check fails.

Runs through the real routes against the temp folders from conftest, on hand-added accounts, so no
network and no demo data. The core rules are in tests/test_proposals.py.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, service
from wardmind.core.proposals import (
    Detector,
    Evidence,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalStatus,
    RefreshResult,
)
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
HTMX = {"HX-Request": "true"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(
    client: TestClient, path: str, data: dict[str, str] | None = None, *, htmx: bool = False
) -> Any:
    headers = {**ORIGIN, **(HTMX if htmx else {})}
    return client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _section(page: str) -> str:
    match = re.search(r'<section[^>]* id="today-suggested"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None, "the Suggested for you section"
    return match.group(1)


def _cards(page: str) -> list[str]:
    # A card ends at the list item's own closing tag, which the template puts at the line start.
    return re.findall(r'<li class="today-row">(.*?)\n</li>', _section(page), re.S)


def _open(kind: Detector | None = None) -> list[Proposal]:
    found = service.get_proposals().open()
    return [item for item in found if kind is None or item.detector is kind]


def _account(name: str, renewal: date) -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$150,000", "renewal_date": renewal.isoformat()},
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _suggestions_off() -> None:
    save_settings(load_settings().model_copy(update={"suggestions": False}))


@pytest.fixture(autouse=True)
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def acme(client: TestClient) -> str:
    """An account whose renewal is 30 days out and has no forecast call: Today suggests the call,
    and a data check (it has no readings yet)."""
    account_id = _account("Acme", TODAY + timedelta(days=30))
    client.get("/today")  # the day's check runs when Today is opened
    return account_id


# --- The section ----------------------------------------------------------------------------


def test_today_shows_suggested_for_you_first_with_its_cards(client: TestClient, acme: str) -> None:
    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    assert "Suggested for you" in page
    assert "Nothing reaches your journal until you accept it." in page
    index = re.findall(r'<a href="#(today-[a-z]+)">([^<]+) <span class="today-index-count">', page)
    assert index[0][0] == "today-suggested"

    cards = _cards(page)
    titles = [re.search(r'suggestion-title">([^<]+)<', card).group(1) for card in cards]
    assert "Make your renewal call for Acme" in titles
    renewal = next(card for card in cards if "renewal call" in card)
    assert "Wardmind" in renewal
    assert "Why" in renewal and "No forecast call yet" in renewal
    assert "Renews" in renewal
    assert f"/accounts/{acme}/draft?kind=renewal&back=today" in renewal
    assert "Accept" in renewal and "Dismiss" in renewal and "Accept with changes" in renewal


def test_the_card_forms_post_to_the_routes_and_say_where_to_send_htmx(
    client: TestClient, acme: str
) -> None:
    page = _text(client.get("/today"))
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    assert f'action="/suggestions/{item.id}/accept"' in page
    assert f'action="/suggestions/{item.id}/dismiss"' in page
    assert f'hx-post="/suggestions/{item.id}/accept"' in page
    assert 'hx-target="closest li"' in page
    assert '<details class="dismiss-menu">' in page  # Dismiss opens its reasons (test_web_taste.py)


def test_the_section_is_hidden_when_suggestions_are_off(client: TestClient, acme: str) -> None:
    _suggestions_off()
    page = _text(client.get("/today"))
    assert "Suggested for you" not in page
    assert 'id="today-suggested"' not in page


def test_the_page_still_renders_when_the_day_check_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the boom with a name in it")

    monkeypatch.setattr(service, "refresh_suggestions", broken)
    _account("Beta", TODAY + timedelta(days=30))
    with caplog.at_level(logging.WARNING):
        response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    assert "Today" in _text(response)
    assert "Suggested for you" not in _text(response)
    assert "(RuntimeError)" in caplog.text
    assert "the boom" not in caplog.text


def test_a_waiting_line_says_how_many_the_caps_held_back(
    client: TestClient, acme: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service, "refresh_suggestions", lambda *args, **kwargs: RefreshResult(waiting=3)
    )
    page = _text(client.get("/today"))
    assert "3 more suggestions are waiting. Accept or dismiss some to make room." in page
    monkeypatch.setattr(
        service, "refresh_suggestions", lambda *args, **kwargs: RefreshResult(waiting=1)
    )
    assert "1 more suggestion is waiting." in _text(client.get("/today"))


def test_the_find_box_narrows_the_suggestions(client: TestClient, acme: str) -> None:
    _account("Beacon", TODAY + timedelta(days=30))
    service.refresh_suggestions(force=True)  # Beacon joins today's check
    page = _text(client.get("/today", params={"q": "acme"}))
    assert "Make your renewal call for Beacon" in _text(client.get("/today"))
    assert "Make your renewal call for Acme" in page
    assert "Beacon" not in _section(page)
    empty = _text(client.get("/today", params={"q": "zzzz"}))
    assert 'id="today-suggested"' not in empty


def test_the_find_text_comes_back_on_each_action(client: TestClient, acme: str) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/accept", {"q": "acme"})
    assert response.status_code == 303
    assert response.headers["location"] == "/today?journal=follow-up-added&q=acme"


# --- Accept -----------------------------------------------------------------------------------


def test_accept_without_javascript_adds_one_follow_up_and_says_so(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/accept")
    assert response.status_code == 303
    assert response.headers["location"] == "/today?journal=follow-up-added"

    follow_ups = service.get_journal().for_account(acme).follow_ups
    assert len(follow_ups) == 1
    assert follow_ups[0].text == item.title and follow_ups[0].rule == item.key
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.ACCEPTED
    assert stored.result_id == follow_ups[0].id

    again = _post(client, f"/suggestions/{item.id}/accept")
    assert again.status_code == 303
    assert len(service.get_journal().for_account(acme).follow_ups) == 1  # accepted once


def test_accept_with_htmx_answers_one_line_in_place_of_the_card(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/accept", htmx=True)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert 'class="today-row suggestion-done"' in response.text
    assert "Added a follow-up." in response.text
    assert len(service.get_journal().for_account(acme).follow_ups) == 1


def test_accept_with_changes_writes_the_edited_text_and_day(client: TestClient, acme: str) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    day = TODAY + timedelta(days=4)
    response = _post(
        client,
        f"/suggestions/{item.id}/accept",
        {"text": "Call Sam about the renewal", "due": day.isoformat()},
    )
    assert response.status_code == 303
    follow_up = service.get_journal().for_account(acme).follow_ups[0]
    assert follow_up.text == "Call Sam about the renewal"
    assert follow_up.due == day


def test_a_date_the_edit_cannot_read_is_refused_on_the_card(client: TestClient, acme: str) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(
        client,
        f"/suggestions/{item.id}/accept",
        {"text": "Call Sam about the renewal", "due": "next tuesday"},
    )
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert "Pick a date, such as 2026-11-16." in page
    assert "Call Sam about the renewal" in page  # what was typed comes back
    assert "Suggested for you" in page and item.title in page
    assert service.get_journal().for_account(acme).follow_ups == []


def test_a_refused_edit_with_htmx_is_the_card_with_its_message(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/accept", {"text": "   "}, htmx=True)
    assert response.status_code == 200
    assert 'class="today-row"' in response.text
    assert "Say what to follow up on." in response.text
    assert service.get_journal().for_account(acme).follow_ups == []


def test_a_claude_snooze_is_accepted_with_the_accounts_health(
    client: TestClient, acme: str
) -> None:
    until = TODAY + timedelta(days=7)
    stored = service.get_proposals().add(
        Proposal(
            id="",
            account_id=acme,
            kind=ProposalKind.SNOOZE,
            source=ProposalSource.MCP,
            key="mcp:req-snooze",
            title="Snooze Acme for a week",
            why=[Evidence(text="Claude: the renewal is handled")],
            text="The renewal is handled.",
            until=until,
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert stored is not None
    response = _post(client, f"/suggestions/{stored.id}/accept")
    assert response.status_code == 303
    assert response.headers["location"] == f"/today?journal=snoozed&day={until.isoformat()}"
    snooze = service.get_journal().for_account(acme).snooze
    assert snooze is not None and snooze.until == until


def test_a_claude_note_is_accepted_as_a_suggestion_note(client: TestClient, acme: str) -> None:
    stored = service.get_proposals().add(
        Proposal(
            id="",
            account_id=acme,
            kind=ProposalKind.NOTE,
            source=ProposalSource.MCP,
            key="mcp:req-note",
            title="Record the call",
            why=[Evidence(text="Claude read the call summary")],
            text="Spoke with Sam about seats.",
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert stored is not None
    response = _post(client, f"/suggestions/{stored.id}/accept")
    assert response.status_code == 303
    assert response.headers["location"] == "/today?journal=note-added"
    note = service.get_journal().for_account(acme).notes[0]
    assert note.text == "Spoke with Sam about seats."


# --- Dismiss ----------------------------------------------------------------------------------


def test_dismiss_without_javascript_removes_the_card_for_good(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/dismiss")
    assert response.status_code == 303
    assert response.headers["location"] == "/today"
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.DISMISSED
    page = _text(client.get("/today"))
    assert "Make your renewal call for Acme" not in page  # the key is never suggested again


def test_dismiss_with_htmx_answers_one_line(client: TestClient, acme: str) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/dismiss", htmx=True)
    assert response.status_code == 200
    assert "Dismissed." in response.text
    assert 'class="today-row suggestion-done"' in response.text


# --- Gone and refused ---------------------------------------------------------------------------


def test_an_unknown_suggestion_is_a_clear_not_found(client: TestClient) -> None:
    response = _post(client, "/suggestions/p-deadbeef/accept")
    assert response.status_code == 404
    _clean(response)
    assert "That suggestion isn't there any more." in _text(response)
    assert 'href="/today"' in response.text and "Back to Today" in response.text

    dismiss = _post(client, "/suggestions/p-deadbeef/dismiss", htmx=True)
    assert dismiss.status_code == 200
    assert "That suggestion isn't there any more." in _text(dismiss)


def test_an_expired_suggestion_says_why_it_cannot_be_accepted(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    store = service.get_proposals()
    store.replace(
        item.model_copy(
            update={
                "status": ProposalStatus.EXPIRED,
                "decided_at": NOW,
                "expired_reason": "The forecast call was made.",
            }
        )
    )
    response = _post(client, f"/suggestions/{item.id}/accept")
    assert response.status_code == 422
    assert "This suggestion has expired, so it can't be accepted. The forecast call was made." in (
        _text(response)
    )
    assert service.get_journal().for_account(acme).follow_ups == []


def test_a_dismissed_suggestion_cannot_be_accepted_from_a_stale_page(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    _post(client, f"/suggestions/{item.id}/dismiss")
    response = _post(client, f"/suggestions/{item.id}/accept", htmx=True)
    assert response.status_code == 200
    assert "You dismissed this suggestion, so it can't be accepted now." in _text(response)
    assert service.get_journal().for_account(acme).follow_ups == []


def test_suggestions_for_an_account_that_left_the_book_are_not_shown(client: TestClient) -> None:
    store = service.get_proposals()
    stored = store.add(
        Proposal(
            id="",
            account_id="gone-account",
            kind=ProposalKind.FOLLOW_UP,
            source=ProposalSource.DETECTOR,
            detector=Detector.GONE_QUIET,
            key="gone_quiet:never",
            title="Get back in touch with Gone Co",
            why=[Evidence(text="No contact logged yet")],
            text="Get back in touch with Gone Co",
            due=TODAY,
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert stored is not None
    _account("Keeper", TODAY + timedelta(days=400))
    page = _text(client.get("/today"))
    assert "Get back in touch with Gone Co" not in page
