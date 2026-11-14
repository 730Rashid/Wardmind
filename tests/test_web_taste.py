"""Suggestions that learn your taste, on the web.

The Dismiss menu's four reasons (web/proposal_pages.py), with and without JavaScript: what each
stores, the result lines and the refused choice. Settings > Assistant (web/settings_assistant.py):
the "Learn from what I accept and dismiss" box, "How suggestions adapt to you" with a row per kind
and its Reset button, and the Reset route.

Runs through the real routes and the real store, against the temp folders from conftest. Decisions
are added through the store the way Today's checks add suggestions (add, then the decision). The
learning rules themselves are in tests/test_taste.py.
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
    DismissReason,
    Evidence,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalStatus,
)
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
HTMX = {"HX-Request": "true"}
CROSS_SITE = {"origin": "http://elsewhere.example"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
PAGE = "/settings/assistant"
NOT_A_KIND = "That kind of suggestion isn't one Wardmind makes."
MENU_HELP = "Not useful and Wrong teach Wardmind to suggest fewer of these."

REASON_LINES = [
    (DismissReason.NOT_NOW, "Dismissed: not now."),
    (DismissReason.ALREADY_ON_IT, "Dismissed: already on it."),
    (DismissReason.NOT_USEFUL, "Dismissed: not useful."),
    (DismissReason.WRONG, "Dismissed: wrong."),
]


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(
    client: TestClient,
    path: str,
    data: dict[str, str] | None = None,
    *,
    htmx: bool = False,
    headers: dict[str, str] | None = None,
) -> Any:
    sent = {**(ORIGIN if headers is None else headers), **(HTMX if htmx else {})}
    return client.post(path, data=data or {}, headers=sent, follow_redirects=False)


def _account(name: str, renewal: date) -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$150,000", "renewal_date": renewal.isoformat()},
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _open(kind: Detector | None = None) -> list[Proposal]:
    found = service.get_proposals().open()
    return [item for item in found if kind is None or item.detector is kind]


def _decide(
    account_id: str,
    detector: Detector,
    status: ProposalStatus,
    n: int,
    reason: DismissReason | None = None,
) -> None:
    """One decided suggestion of this kind, added through the store and then decided, so it never
    sits open (the store caps open suggestions, not decided ones)."""
    store = service.get_proposals()
    stored = store.add(
        Proposal(
            id="",
            account_id=account_id,
            kind=ProposalKind.FOLLOW_UP,
            source=ProposalSource.DETECTOR,
            detector=detector,
            key=f"{detector.value}:decided-{n}",
            title=f"Decided suggestion {n}",
            why=[Evidence(text="A decision for the tests")],
            text=f"Decided suggestion {n}",
            due=TODAY,
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert stored is not None
    store.replace(
        stored.model_copy(update={"status": status, "decided_at": NOW, "dismiss_reason": reason})
    )


def _decisions(account_id: str, detector: Detector, *, accepted: int, dismissed: int) -> None:
    """`accepted` accepted and `dismissed` dismissed as Not useful, for one kind."""
    for n in range(accepted):
        _decide(account_id, detector, ProposalStatus.ACCEPTED, n)
    for n in range(dismissed):
        _decide(
            account_id,
            detector,
            ProposalStatus.DISMISSED,
            accepted + n,
            DismissReason.NOT_USEFUL,
        )


def _row(detector: Detector) -> Any:
    return next(row for row in service.get_tastes() if row.detector is detector)


def _taste_section(page: str) -> str:
    match = re.search(r"<section[^>]*assistant-taste[^>]*>(.*?)</section>", page, re.S)
    assert match is not None, "the How suggestions adapt section"
    return match.group(1)


def _box(page: str, name: str) -> str:
    """The whole <input> tag of the form field `name`."""
    tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    assert tag is not None, name
    return tag.group(0)


@pytest.fixture(autouse=True)
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def acme(client: TestClient) -> str:
    """An account whose renewal is 30 days out with no forecast call: Today suggests the call."""
    account_id = _account("Acme", TODAY + timedelta(days=30))
    client.get("/today")  # the day's check runs when Today is opened
    return account_id


# --- The Dismiss menu -----------------------------------------------------------------------------


def test_the_card_offers_the_four_reasons_in_a_dismiss_menu(client: TestClient, acme: str) -> None:
    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    item = _open(Detector.RENEWAL_NO_CALL)[0]

    assert '<details class="dismiss-menu">' in page
    assert '<summary class="button button--small">Dismiss' in page
    positions = []
    for reason in DismissReason:
        assert f'<input type="hidden" name="reason" value="{reason.value}">' in page
        positions.append(page.index(f">{reason.label}<"))
    assert positions == sorted(positions)  # in DismissReason's order
    assert MENU_HELP in page
    menu_posts = f'hx-post="/suggestions/{item.id}/dismiss"'
    assert page.count(menu_posts) == len(DismissReason)
    assert page.count(f'action="/suggestions/{item.id}/dismiss"') == len(DismissReason)


@pytest.mark.parametrize(("reason", "line"), REASON_LINES)
def test_each_reason_with_htmx_is_stored_and_answered_in_one_line(
    client: TestClient, acme: str, reason: DismissReason, line: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/dismiss", {"reason": reason.value}, htmx=True)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert 'class="today-row suggestion-done"' in response.text
    assert line in response.text
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.DISMISSED
    assert stored.dismiss_reason is reason


@pytest.mark.parametrize("reason", list(DismissReason))
def test_each_reason_without_javascript_is_stored_and_goes_back_to_today(
    client: TestClient, acme: str, reason: DismissReason
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    data = {"reason": reason.value, "q": "acme"}
    response = _post(client, f"/suggestions/{item.id}/dismiss", data)
    assert response.status_code == 303
    assert response.headers["location"] == "/today?q=acme"
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.DISMISSED
    assert stored.dismiss_reason is reason
    assert "Make your renewal call for Acme" not in _text(client.get("/today"))


@pytest.mark.parametrize("reason", [None, ""])
def test_no_reason_is_the_plain_dismiss_and_stores_none(
    client: TestClient, acme: str, reason: str | None
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    data = {} if reason is None else {"reason": reason}
    response = _post(client, f"/suggestions/{item.id}/dismiss", data, htmx=True)
    assert response.status_code == 200
    assert "Dismissed." in response.text and "Dismissed:" not in response.text
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.DISMISSED
    assert stored.dismiss_reason is None


def test_a_reason_outside_the_list_is_refused_with_htmx_and_nothing_is_stored(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/dismiss", {"reason": "bored"}, htmx=True)
    assert response.status_code == 200
    _clean(response)
    assert 'class="today-row"' in response.text
    assert "Pick a reason from the list." in response.text
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.OPEN
    assert stored.dismiss_reason is None


def test_a_reason_outside_the_list_is_a_422_page_without_javascript(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    response = _post(client, f"/suggestions/{item.id}/dismiss", {"reason": "bored"})
    assert response.status_code == 422
    _clean(response)
    assert "Pick a reason from the list." in _text(response)
    assert "Make your renewal call for Acme" in _text(response)
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.status is ProposalStatus.OPEN


def test_a_second_dismiss_keeps_the_reason_already_stored(client: TestClient, acme: str) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    _post(client, f"/suggestions/{item.id}/dismiss", {"reason": "not_useful"})
    again = _post(client, f"/suggestions/{item.id}/dismiss", {"reason": "wrong"}, htmx=True)
    assert again.status_code == 200
    assert "Dismissed: not useful." in again.text  # the line says what was stored
    stored = service.get_proposals().get(item.id)
    assert stored is not None and stored.dismiss_reason is DismissReason.NOT_USEFUL


def test_a_not_useful_dismissal_counts_against_its_kind(client: TestClient, acme: str) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    _post(client, f"/suggestions/{item.id}/dismiss", {"reason": "not_useful"})
    row = _row(Detector.RENEWAL_NO_CALL)
    assert (row.accepted, row.dismissed, row.set_aside) == (0, 1, 0)


def test_a_not_now_dismissal_is_set_aside_and_does_not_count_against_its_kind(
    client: TestClient, acme: str
) -> None:
    item = _open(Detector.RENEWAL_NO_CALL)[0]
    _post(client, f"/suggestions/{item.id}/dismiss", {"reason": "not_now"})
    row = _row(Detector.RENEWAL_NO_CALL)
    assert (row.accepted, row.dismissed, row.set_aside) == (0, 0, 1)


# --- Settings > Assistant: the learn box ---------------------------------------------------------


def test_the_learn_box_is_on_by_default_with_its_words(client: TestClient) -> None:
    page = _text(client.get(PAGE))
    assert "Learn from what I accept and dismiss" in page
    assert (
        "A kind of suggestion you keep dismissing as Not useful or Wrong is shown less. "
        "Health drops and renewals are always shown."
    ) in page
    assert "checked" in _box(page, "suggestions_learn")


def test_the_learn_box_saves_both_ways(client: TestClient) -> None:
    on = _post(client, PAGE, {"suggestions": "1", "suggestions_learn": "1", "mcp_writes": "off"})
    assert on.status_code == 303
    assert load_settings().suggestions_learn is True
    assert "checked" in _box(client.get(PAGE).text, "suggestions_learn")

    off = _post(client, PAGE, {"suggestions": "1", "mcp_writes": "off"})
    assert off.status_code == 303
    assert load_settings().suggestions_learn is False
    assert "checked" not in _box(client.get(PAGE).text, "suggestions_learn")


def test_a_refused_save_keeps_the_learn_box_as_typed_and_saves_nothing(client: TestClient) -> None:
    save_settings(load_settings().model_copy(update={"suggestions_learn": False}))
    posted = {"suggestions": "1", "suggestions_learn": "1", "mcp_writes": "x"}
    response = _post(client, PAGE, posted)
    assert response.status_code == 422
    assert "checked" in _box(response.text, "suggestions_learn")
    assert load_settings().suggestions_learn is False


# --- Settings > Assistant: how suggestions adapt ----------------------------------------------


def test_the_adapt_table_has_a_row_per_kind_with_its_sentence(client: TestClient) -> None:
    response = client.get(PAGE)
    assert response.status_code == 200
    _clean(response)
    section = _taste_section(_text(response))
    assert "How suggestions adapt to you" in _text(response)
    rows = re.findall(r'<th scope="row">([^<]+)</th>', section)
    tastes = service.get_tastes()
    assert rows == [row.label for row in tastes]
    assert len(tastes) == len(Detector)
    for row in tastes:
        assert row.sentence in section
        assert row.state.label in section
    assert "No decisions yet." in section
    assert "Always shown: Wardmind never holds these back." in section  # PROTECTED, no decisions


def test_counts_show_the_state_and_a_reset_for_a_kind_with_decisions(
    client: TestClient,
) -> None:
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    _decisions(account_id, Detector.REPLY_OVERDUE, accepted=2, dismissed=8)
    row = _row(Detector.REPLY_OVERDUE)
    assert row.state.label == "Showing fewer"
    assert row.sentence == "You accepted 2 of 10. Showing fewer: at most 1 a day."

    section = _taste_section(_text(client.get(PAGE)))
    assert "Showing fewer" in section
    assert "You accepted 2 of 10. Showing fewer: at most 1 a day." in section
    assert f'action="{PAGE}/taste/reply_overdue/reset"' in section
    assert "Wardmind learnt about Reply overdue" in section


def test_no_reset_for_a_kind_with_only_set_aside_decisions(client: TestClient) -> None:
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    for n in range(3):
        _decide(account_id, Detector.GONE_QUIET, ProposalStatus.DISMISSED, n, DismissReason.NOT_NOW)
    section = _taste_section(_text(client.get(PAGE)))
    assert "No decisions yet." in section
    assert f"{PAGE}/taste/gone_quiet/reset" not in section


def test_a_protected_kind_has_no_reset_as_nothing_learnt_changes_it(client: TestClient) -> None:
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    _decisions(account_id, Detector.HEALTH_DROPPED, accepted=3, dismissed=1)
    section = _taste_section(_text(client.get(PAGE)))
    assert "You accepted 3 of 4. Always shown: Wardmind never holds these back." in section
    assert f"{PAGE}/taste/health_dropped/reset" not in section


def test_with_learning_off_every_kind_is_not_learning_and_has_no_reset(client: TestClient) -> None:
    save_settings(load_settings().model_copy(update={"suggestions_learn": False}))
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    _decisions(account_id, Detector.REPLY_OVERDUE, accepted=2, dismissed=8)
    section = _taste_section(_text(client.get(PAGE)))
    assert section.count("Not learning") == len(Detector)
    assert section.count("Learning is off.") == len(Detector)
    assert f"{PAGE}/taste/reply_overdue/reset" not in section


def test_the_section_is_hidden_when_suggestions_are_off(client: TestClient) -> None:
    save_settings(load_settings().model_copy(update={"suggestions": False}))
    page = _text(client.get(PAGE))
    assert "How suggestions adapt to you" not in page
    assert "assistant-taste" not in page


def test_the_section_is_hidden_when_the_taste_cannot_be_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> None:
        raise RuntimeError("the boom with a name in it")

    monkeypatch.setattr(service, "get_tastes", broken)
    with caplog.at_level(logging.WARNING):
        response = client.get(PAGE)
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    assert "How suggestions adapt to you" not in page
    assert "Suggest next steps on Today" in page  # the rest of the page still renders
    assert "(RuntimeError)" in caplog.text
    assert "the boom" not in caplog.text


# --- The Reset route ----------------------------------------------------------------------------


def test_reset_stores_the_reset_and_goes_back_with_the_notice(client: TestClient) -> None:
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    _decisions(account_id, Detector.REPLY_OVERDUE, accepted=2, dismissed=8)
    decided_before = len(service.get_proposals().all())

    response = _post(client, f"{PAGE}/taste/reply_overdue/reset")
    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?saved=forgot-reply_overdue"

    taken = service.get_proposals().load().taste_resets
    assert set(taken) == {Detector.REPLY_OVERDUE}
    assert taken[Detector.REPLY_OVERDUE] is not None
    assert len(service.get_proposals().all()) == decided_before  # the decisions are kept

    shown = _text(client.get(response.headers["location"]))
    assert "Wardmind forgot what it learnt about Reply overdue." in shown
    section = _taste_section(shown)
    assert "No decisions yet." in section
    assert f"{PAGE}/taste/reply_overdue/reset" not in section


def test_reset_leaves_the_other_kinds_alone(client: TestClient) -> None:
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    _decisions(account_id, Detector.REPLY_OVERDUE, accepted=2, dismissed=8)
    _decisions(account_id, Detector.GONE_QUIET, accepted=1, dismissed=9)
    _post(client, f"{PAGE}/taste/reply_overdue/reset")
    section = _taste_section(_text(client.get(PAGE)))
    assert f"{PAGE}/taste/gone_quiet/reset" in section
    assert "You accepted 1 of 10." in section


def test_an_unknown_kind_is_a_404_page_and_stores_nothing(client: TestClient) -> None:
    response = _post(client, f"{PAGE}/taste/nope/reset")
    assert response.status_code == 404
    _clean(response)
    assert NOT_A_KIND in _text(response)
    assert 'href="/settings/assistant"' in response.text
    assert service.get_proposals().load().taste_resets == {}


def test_a_reset_from_another_site_is_refused_and_stores_nothing(client: TestClient) -> None:
    account_id = _account("Beacon", TODAY + timedelta(days=30))
    _decisions(account_id, Detector.REPLY_OVERDUE, accepted=2, dismissed=8)
    response = _post(client, f"{PAGE}/taste/reply_overdue/reset", headers=CROSS_SITE)
    assert response.status_code == 403
    assert service.get_proposals().load().taste_resets == {}


def test_an_unknown_notice_code_shows_nothing(client: TestClient) -> None:
    page = _text(client.get(PAGE, params={"saved": "forgot-nope"}))
    assert "Wardmind forgot" not in page
    assert "Assistant settings saved." not in page
