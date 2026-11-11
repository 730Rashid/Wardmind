"""The journal in the web: each form's success path with its status line and redirect target, the
refusals (422 with the values as typed, and from Today a redirect with the reason), an unknown
account or item, the escaping of what a CSM typed, the demo journal kept apart, and the account
page's journal parts (last contact, the trend, the outcome box and Undo).

Runs through the real routes against the temp folders from conftest, with hand-added accounts (no
network). Demo data is used only where the demo's own history is the point.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, history, journal, service
from wardmind.core.config import load_scoring_config
from wardmind.core.models import Health, Motion
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import DEMO_JOURNAL_FILE, JOURNAL_FILE, LocalStore
from wardmind.web import app as web
from wardmind.web import journal_pages

ORIGIN = {"origin": "http://127.0.0.1"}
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _path(account_id: str, *parts: str) -> str:
    return "/".join([f"/accounts/{account_id}", *parts])


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


@pytest.fixture(autouse=True)
def mine() -> None:
    """Journal tests use Your data unless a test asks for the demo."""
    _use(DataSource.MINE)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


def _account(name: str, renewal: date) -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$184,000", "renewal_date": renewal.isoformat()},
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


@pytest.fixture
def acme(store: LocalStore) -> str:
    """A hand-added account whose renewal is comfortably ahead. Returns its id."""
    return _account("Acme Robotics", TODAY + timedelta(days=200))


def _journal(account_id: str) -> journal.AccountJournal:
    return service.account_journal(account_id)


# --- Notes ----------------------------------------------------------------------------------


def test_a_call_is_logged_for_today_and_the_page_says_so(client: TestClient, acme: str) -> None:
    response = _post(
        client,
        _path(acme, "notes"),
        {"kind": "call", "text": "Spoke about the renewal", "happened_on": "", "back": "account"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=activity-added#activity"
    (note,) = _journal(acme).notes
    assert note.kind is journal.TouchKind.CALL
    assert note.happened_on == TODAY
    assert note.text == "Spoke about the renewal"
    page = _text(client.get(response.headers["location"]))
    assert "Activity logged." in page
    assert "Spoke about the renewal" in page
    assert "Contacted today" in page


def test_a_note_with_a_bogus_back_value_returns_to_the_account_page(
    client: TestClient, acme: str
) -> None:
    response = _post(
        client,
        _path(acme, "notes"),
        {"kind": "note", "text": "Met at the QBR", "back": "https://evil.example/"},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/accounts/{acme}?")


def test_an_empty_note_is_refused_and_keeps_the_values_typed(client: TestClient, acme: str) -> None:
    happened = (TODAY - timedelta(days=3)).isoformat()
    response = _post(
        client,
        _path(acme, "notes"),
        {"kind": "meeting", "happened_on": happened, "text": "   ", "back": "account"},
    )
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert "Type the note." in page
    assert f'value="{happened}"' in page
    assert 'value="meeting" checked' in page
    assert _journal(acme).notes == []


def test_a_note_over_the_limit_keeps_its_text_in_the_box(client: TestClient, acme: str) -> None:
    long_text = "Customer asked about SSO. " * 200  # 5,200 characters
    response = _post(client, _path(acme, "notes"), {"kind": "note", "text": long_text})
    assert response.status_code == 422
    assert "Keep the note under 4,000 characters." in _text(response)
    assert "Customer asked about SSO." in response.text


def test_a_note_dated_in_the_future_is_refused_on_its_date(client: TestClient, acme: str) -> None:
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    response = _post(
        client, _path(acme, "notes"), {"kind": "call", "happened_on": tomorrow, "text": "Later"}
    )
    assert response.status_code == 422
    page = _text(response)
    assert "That date is in the future." in page
    assert f'value="{tomorrow}"' in page
    assert _journal(acme).notes == []


def test_a_note_is_escaped_on_the_page(client: TestClient, acme: str) -> None:
    payload = "<script>alert('x')</script> and <b>bold</b>"
    _post(client, _path(acme, "notes"), {"kind": "note", "text": payload})
    response = client.get(f"/accounts/{acme}")
    assert "&lt;script&gt;alert(" in response.text
    assert "&lt;b&gt;bold&lt;/b&gt;" in response.text
    assert "<script>alert(" not in response.text
    assert "<b>bold</b>" not in response.text


def test_a_note_can_be_edited_and_the_page_says_so(client: TestClient, acme: str) -> None:
    _post(client, _path(acme, "notes"), {"kind": "note", "text": "First draft"})
    (note,) = _journal(acme).notes
    response = _post(
        client,
        _path(acme, "notes", note.id, "edit"),
        {"kind": "email", "happened_on": TODAY.isoformat(), "text": "Sent the summary"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=activity-saved#activity"
    (changed,) = _journal(acme).notes
    assert changed.kind is journal.TouchKind.EMAIL
    assert changed.text == "Sent the summary"
    assert changed.edited_at is not None
    assert "Activity saved." in _text(client.get(response.headers["location"]))


def test_a_refused_note_edit_opens_its_disclosure_with_the_message(
    client: TestClient, acme: str
) -> None:
    _post(client, _path(acme, "notes"), {"kind": "note", "text": "Keep me"})
    (note,) = _journal(acme).notes
    response = _post(
        client,
        _path(acme, "notes", note.id, "edit"),
        {"kind": "note", "happened_on": TODAY.isoformat(), "text": ""},
    )
    assert response.status_code == 422
    page = _text(response)
    assert '<details class="note-disclosure" open>' in response.text
    assert "Type the note." in page
    assert _journal(acme).notes[0].text == "Keep me"


def test_a_note_can_be_removed_and_an_unknown_one_is_a_friendly_404(
    client: TestClient, acme: str
) -> None:
    _post(client, _path(acme, "notes"), {"kind": "note", "text": "Temporary"})
    (note,) = _journal(acme).notes
    removed = _post(client, _path(acme, "notes", note.id, "remove"))
    assert removed.status_code == 303
    assert removed.headers["location"] == f"/accounts/{acme}?journal=note-removed#notes"
    assert _journal(acme).notes == []

    missing = _post(client, _path(acme, "notes", "n-00000000", "remove"))
    assert missing.status_code == 404
    _clean(missing)
    assert "That note isn't in this account." in _text(missing)


# --- Follow-ups -----------------------------------------------------------------------------


def test_a_follow_up_is_added_for_the_date_given(client: TestClient, acme: str) -> None:
    due = TODAY + timedelta(days=5)
    response = _post(
        client,
        _path(acme, "follow-ups"),
        {"text": "Send the QBR deck", "due": due.isoformat(), "days": "", "back": "account"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=follow-up-added#follow-ups"
    (item,) = journal.open_follow_ups(_journal(acme))
    assert (item.text, item.due) == ("Send the QBR deck", due)
    page = _text(client.get(response.headers["location"]))
    assert "Follow-up added." in page
    assert "Due in 5 days" in page


def test_the_days_shortcut_sets_the_due_date_from_today(client: TestClient, acme: str) -> None:
    response = _post(
        client, _path(acme, "follow-ups"), {"text": "Check usage", "due": "", "days": "7"}
    )
    assert response.status_code == 303
    (item,) = journal.open_follow_ups(_journal(acme))
    assert item.due == TODAY + timedelta(days=7)


def test_a_blank_follow_up_takes_the_next_step_cut_to_200_characters(
    client: TestClient, acme: str
) -> None:
    suggested = service.find_account(service.get_book(), acme).suggested_action
    response = _post(client, _path(acme, "follow-ups"), {"text": "", "due": "", "days": "7"})
    assert response.status_code == 303
    (item,) = journal.open_follow_ups(_journal(acme))
    assert item.text == journal_pages.cut_text(suggested)
    assert len(item.text) <= 200


def test_a_due_date_in_the_past_is_refused_and_the_values_stay(
    client: TestClient, acme: str
) -> None:
    past = (TODAY - timedelta(days=3)).isoformat()
    response = _post(
        client, _path(acme, "follow-ups"), {"text": "Too late", "due": past, "days": ""}
    )
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert "Pick today or a later date." in page
    assert f'value="{past}"' in page
    assert 'value="Too late"' in page
    assert journal.open_follow_ups(_journal(acme)) == []


def test_the_twenty_open_follow_ups_cap_refuses_on_the_account_page(
    client: TestClient, acme: str
) -> None:
    for number in range(journal.MAX_OPEN_FOLLOW_UPS):
        service.get_journal().add_follow_up(acme, f"Item {number}", TODAY, now=NOW, today=TODAY)
    response = _post(
        client,
        _path(acme, "follow-ups"),
        {"text": "One too many", "due": TODAY.isoformat(), "back": "account"},
    )
    assert response.status_code == 422
    assert "This account has 20 open follow-ups. Finish some first." in _text(response)
    assert 'value="One too many"' in response.text


def test_the_same_refusal_from_today_redirects_to_the_account_with_its_reason(
    client: TestClient, acme: str
) -> None:
    for number in range(journal.MAX_OPEN_FOLLOW_UPS):
        service.get_journal().add_follow_up(acme, f"Item {number}", TODAY, now=NOW, today=TODAY)
    response = _post(
        client,
        _path(acme, "follow-ups"),
        {"text": "One too many", "days": "7", "back": "today"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?why=cap#next-step"
    page = _text(client.get(response.headers["location"]))
    assert "This account has 20 open follow-ups. Finish some first." in page


def test_done_offers_undo_and_undo_reopens_it_with_its_due_date(
    client: TestClient, acme: str
) -> None:
    due = TODAY + timedelta(days=4)
    _post(client, _path(acme, "follow-ups"), {"text": "Ring the sponsor", "due": due.isoformat()})
    (item,) = journal.open_follow_ups(_journal(acme))

    done = _post(client, _path(acme, "follow-ups", item.id, "done"), {"back": "account"})
    assert done.status_code == 303
    assert done.headers["location"] == (f"/accounts/{acme}?journal=done&undo={item.id}#follow-ups")
    page = _text(client.get(done.headers["location"]))
    assert "Marked done." in page
    assert f"/accounts/{acme}/follow-ups/{item.id}/undo" in page
    assert "Done today" in page

    undone = _post(client, _path(acme, "follow-ups", item.id, "undo"), {"back": "account"})
    assert undone.status_code == 303
    assert undone.headers["location"] == f"/accounts/{acme}?journal=reopened#follow-ups"
    (back_open,) = journal.open_follow_ups(_journal(acme))
    assert (back_open.id, back_open.due, back_open.done_at) == (item.id, due, None)


def test_move_takes_a_date_or_the_days_shortcut(client: TestClient, acme: str) -> None:
    _post(client, _path(acme, "follow-ups"), {"text": "Renewal call", "days": "2"})
    (item,) = journal.open_follow_ups(_journal(acme))

    moved = _post(
        client,
        _path(acme, "follow-ups", item.id, "move"),
        {"due": (TODAY + timedelta(days=10)).isoformat(), "back": "account"},
    )
    assert moved.status_code == 303
    assert moved.headers["location"] == (
        f"/accounts/{acme}?journal=moved&day={(TODAY + timedelta(days=10)).isoformat()}#follow-ups"
    )
    assert journal.open_follow_ups(_journal(acme))[0].due == TODAY + timedelta(days=10)

    shortcut = _post(
        client,
        _path(acme, "follow-ups", item.id, "move"),
        {"due": "", "days": "1", "back": "today"},
    )
    assert shortcut.status_code == 303
    assert shortcut.headers["location"].startswith("/today?journal=moved&day=")
    assert journal.open_follow_ups(_journal(acme))[0].due == TODAY + timedelta(days=1)


def test_a_move_into_the_past_is_refused_beside_the_date_box(client: TestClient, acme: str) -> None:
    _post(client, _path(acme, "follow-ups"), {"text": "Keep it", "days": "3"})
    (item,) = journal.open_follow_ups(_journal(acme))
    past = (TODAY - timedelta(days=1)).isoformat()
    response = _post(client, _path(acme, "follow-ups", item.id, "move"), {"due": past})
    assert response.status_code == 422
    assert "Pick today or a later date." in _text(response)
    assert f'value="{past}"' in response.text


def test_a_follow_up_can_be_removed_and_an_unknown_one_is_a_friendly_404(
    client: TestClient, acme: str
) -> None:
    _post(client, _path(acme, "follow-ups"), {"text": "Drop me", "days": "3"})
    (item,) = journal.open_follow_ups(_journal(acme))
    removed = _post(client, _path(acme, "follow-ups", item.id, "remove"))
    assert removed.status_code == 303
    assert removed.headers["location"] == f"/accounts/{acme}?journal=follow-up-removed#follow-ups"
    assert journal.open_follow_ups(_journal(acme)) == []

    missing = _post(client, _path(acme, "follow-ups", "f-00000000", "done"), {"back": "today"})
    assert missing.status_code == 404
    _clean(missing)
    page = _text(missing)
    assert "That follow-up isn't in this account." in page
    assert 'href="/today"' in page


# --- Snoozes --------------------------------------------------------------------------------


def test_a_snooze_shows_its_line_and_wake_ends_it(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "snooze"), {"days": "7", "back": "account"})
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/accounts/{acme}?journal=snoozed&day={(TODAY + timedelta(days=7)).isoformat()}#next-step"
    )
    snooze = _journal(acme).snooze
    assert snooze is not None and snooze.until == TODAY + timedelta(days=7)
    line = journal_pages.short_day(TODAY + timedelta(days=7), TODAY)
    page = _text(client.get(response.headers["location"]))
    assert f"Snoozed until {line}." in page
    assert "It comes back sooner if its health gets worse." in page
    assert "Wake now" in page

    woken = _post(client, _path(acme, "wake"), {"back": "account"})
    assert woken.status_code == 303
    assert woken.headers["location"] == f"/accounts/{acme}?journal=woke#next-step"
    assert _journal(acme).snooze is None


def test_a_snooze_on_today_returns_to_today(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "snooze"), {"days": "7", "back": "today"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/today?journal=snoozed&day=")


def test_a_snooze_longer_than_ninety_days_is_refused_with_its_date(
    client: TestClient, acme: str
) -> None:
    far = (TODAY + timedelta(days=100)).isoformat()
    response = _post(client, _path(acme, "snooze"), {"days": "", "until": far})
    assert response.status_code == 422
    assert "Snooze for 90 days at most." in _text(response)
    assert f'value="{far}"' in response.text
    assert _journal(acme).snooze is None


def test_a_snooze_on_a_day_off_the_menu_is_refused(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "snooze"), {"days": "5"})
    assert response.status_code == 422
    assert "Pick 3, 7, 14 or 30 days." in _text(response)


# --- Renewal outcomes -----------------------------------------------------------------------


def test_a_renewal_outcome_keeps_the_arr_before_and_after_and_the_scores(
    client: TestClient,
) -> None:
    renewal = TODAY - timedelta(days=5)
    account_id = _account("Lapsed Co", renewal)
    response = _post(
        client,
        _path(account_id, "outcome"),
        {
            "kind": "expanded",
            "renewal_date": renewal.isoformat(),
            "decided_on": TODAY.isoformat(),
            "arr_after": "$230,000",
            "note": "Added two sites",
            "back": "account",
        },
    )
    assert response.status_code == 303
    assert (
        response.headers["location"] == f"/accounts/{account_id}?journal=outcome-recorded#outcome"
    )
    (outcome,) = _journal(account_id).outcomes
    assert outcome.kind is journal.OutcomeKind.EXPANDED
    assert outcome.arr_before == 184000 and outcome.arr_after == 230000
    assert outcome.note == "Added two sites"
    page = _text(client.get(response.headers["location"]))
    assert "Outcome recorded." in page
    assert "Expanded on " in page and "$184,000 to $230,000" in page


def test_a_plain_number_is_read_as_arr_and_a_churn_needs_none(client: TestClient) -> None:
    renewal = TODAY - timedelta(days=2)
    account_id = _account("Plain Numbers", renewal)
    _post(
        client,
        _path(account_id, "outcome"),
        {"kind": "renewed", "renewal_date": renewal.isoformat(), "arr_after": "120000"},
    )
    assert _journal(account_id).outcomes[-1].arr_after == 120000

    other = _account("Gone Ltd", renewal)
    _post(
        client,
        _path(other, "outcome"),
        {"kind": "churned", "renewal_date": renewal.isoformat(), "arr_after": ""},
    )
    (churned,) = _journal(other).outcomes
    assert churned.kind is journal.OutcomeKind.CHURNED
    assert churned.arr_after == 0.0


def test_an_outcome_decided_in_the_future_is_refused_on_its_date(
    client: TestClient,
) -> None:
    renewal = TODAY - timedelta(days=4)
    account_id = _account("Future Decision", renewal)
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    response = _post(
        client,
        _path(account_id, "outcome"),
        {"kind": "renewed", "renewal_date": renewal.isoformat(), "decided_on": tomorrow},
    )
    assert response.status_code == 422
    page = _text(response)
    assert "That date is in the future." in page
    assert f'value="{tomorrow}"' in response.text
    assert _journal(account_id).outcomes == []


def test_an_outcome_can_be_removed_and_an_unknown_one_is_a_friendly_404(
    client: TestClient,
) -> None:
    renewal = TODAY - timedelta(days=3)
    account_id = _account("Mistake Co", renewal)
    _post(
        client,
        _path(account_id, "outcome"),
        {"kind": "renewed", "renewal_date": renewal.isoformat(), "arr_after": "100000"},
    )
    (outcome,) = _journal(account_id).outcomes
    removed = _post(client, _path(account_id, "outcomes", outcome.id, "remove"))
    assert removed.status_code == 303
    assert removed.headers["location"] == f"/accounts/{account_id}?journal=outcome-removed#outcome"
    assert _journal(account_id).outcomes == []

    missing = _post(client, _path(account_id, "outcomes", "o-00000000", "remove"))
    assert missing.status_code == 404
    assert "That outcome isn't in this account." in _text(missing)


# --- Missing accounts and bad input -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("notes", {"kind": "note", "text": "x"}),
        ("follow-ups", {"text": "x", "days": "3"}),
        ("snooze", {"days": "7"}),
        ("wake", {}),
        ("outcome", {"kind": "renewed"}),
    ],
)
def test_an_unknown_account_is_a_404_for_every_journal_form(
    client: TestClient, path: str, data: dict[str, str]
) -> None:
    response = _post(client, f"/accounts/acc-does-not-exist/{path}", data)
    assert response.status_code == 404
    _clean(response)
    assert "No account with id acc-does-not-exist." in _text(response)


def test_a_bad_kind_of_note_is_refused_with_the_choices(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "notes"), {"kind": "letter", "text": "Hello"})
    assert response.status_code == 422
    assert "Pick Note, Call, Meeting, Email or Other." in _text(response)


# --- The account page -------------------------------------------------------------------------


def test_the_last_contact_and_the_open_follow_ups_facts(client: TestClient, acme: str) -> None:
    page = _text(client.get(f"/accounts/{acme}"))
    assert "Not contacted yet" in page
    assert "Open follow-ups:" not in page

    _post(
        client,
        _path(acme, "notes"),
        {"kind": "note", "happened_on": (TODAY - timedelta(days=12)).isoformat(), "text": "Old"},
    )
    assert "Not contacted yet" in _text(client.get(f"/accounts/{acme}"))

    _post(
        client,
        _path(acme, "notes"),
        {
            "kind": "call",
            "happened_on": (TODAY - timedelta(days=12)).isoformat(),
            "text": "Called",
        },
    )
    assert "Contacted 12 days ago" in _text(client.get(f"/accounts/{acme}"))

    _post(client, _path(acme, "follow-ups"), {"text": "Follow", "days": "2"})
    assert "Open follow-ups: 1" in _text(client.get(f"/accounts/{acme}"))


def test_the_snooze_line_and_the_wake_button_show_on_the_account_page(
    client: TestClient, acme: str
) -> None:
    service.get_journal().snooze(
        acme, TODAY + timedelta(days=14), Health.HEALTHY, now=NOW, today=TODAY
    )
    page = _text(client.get(f"/accounts/{acme}"))
    expected = journal_pages.short_day(TODAY + timedelta(days=14), TODAY)
    assert f"Snoozed until {expected}. It comes back sooner" in page
    assert "Wake now" in page
    assert "Snooze for" not in page


def test_the_outcome_box_opens_when_the_renewal_date_has_passed(
    client: TestClient,
) -> None:
    passed = _account("Passed Renewal", TODAY - timedelta(days=12))
    page = _text(client.get(f"/accounts/{passed}"))
    assert "The renewal date passed 12 days ago. How did it go?" in page
    assert '<details class="outcome-disclosure outcome-add" open>' in page

    ahead = _account("Ahead Renewal", TODAY + timedelta(days=90))
    page_ahead = _text(client.get(f"/accounts/{ahead}"))
    assert "How did it go?" not in page_ahead
    assert '<details class="outcome-disclosure outcome-add">' in page_ahead


def test_the_owner_fact_is_shown_once_in_the_header(client: TestClient, acme: str) -> None:
    page = _text(client.get(f"/accounts/{acme}"))
    assert "<dt>Owner</dt>" not in page
    assert "Renews" in page


def test_a_new_history_says_it_started_today(client: TestClient, acme: str) -> None:
    page = _text(client.get(f"/accounts/{acme}"))
    assert "Risk over the last 90 days" in page
    assert "Wardmind started keeping score history today. The trend fills in as days pass." in page
    assert 'role="img"' in page


def test_the_trend_in_demo_mode_is_drawn_from_the_demo_history(client: TestClient) -> None:
    _use(DataSource.DEMO)
    response = client.get("/accounts/acc-001")
    assert response.status_code == 200
    _clean(response)
    assert 'class="trend-chart" viewBox="0 0 300 60" role="img"' in response.text
    assert 'aria-label="Risk over the last 90 days.' in response.text
    caption = re.search(r'<p class="trend-caption">([^<]*)</p>', _text(response))
    assert caption is not None
    assert caption.group(1).startswith("Risk ")
    assert " now" in caption.group(1)
    assert "a week ago" in caption.group(1)


def test_demo_notes_and_follow_ups_are_kept_apart_from_your_data(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.DEMO)
    response = _post(client, "/accounts/acc-001/notes", {"kind": "note", "text": "Demo only"})
    assert response.status_code == 303
    assert (store.root / DEMO_JOURNAL_FILE).exists()
    assert not (store.root / JOURNAL_FILE).exists()
    page = _text(client.get("/accounts/acc-001"))
    assert (
        "You're on demo data. Notes and follow-ups here are kept apart from your own data." in page
    )
    assert "Demo only" in page


def test_the_account_page_journal_has_no_inline_style(client: TestClient, acme: str) -> None:
    _post(client, _path(acme, "follow-ups"), {"text": "Check", "days": "1"})
    _post(client, _path(acme, "notes"), {"kind": "call", "text": "Talked"})
    response = client.get(f"/accounts/{acme}")
    _clean(response)


# --- The pure helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "label"),
    [
        (0, "Due today"),
        (1, "Due tomorrow"),
        (5, "Due in 5 days"),
        (-1, "1 day late"),
        (-3, "3 days late"),
    ],
)
def test_due_labels_read_in_plain_words(days: int, label: str) -> None:
    assert journal_pages.due_label(TODAY + timedelta(days=days), TODAY) == label


@pytest.mark.parametrize(
    ("days", "label"),
    [(0, "Contacted today"), (1, "Contacted yesterday"), (12, "Contacted 12 days ago")],
)
def test_contact_labels_read_in_plain_words(days: int, label: str) -> None:
    assert journal_pages.contact_label(TODAY - timedelta(days=days), TODAY) == label
    assert journal_pages.contact_label(None, TODAY) == "Not contacted yet"


def test_cut_text_keeps_the_limit_and_ends_on_a_word_with_an_ellipsis() -> None:
    long = "Ask support for a fix date on each open severity-one ticket " * 6
    cut = journal_pages.cut_text(long)
    assert len(cut) <= journal.MAX_FOLLOW_UP
    assert cut.endswith("…")
    assert not cut[:-1].endswith(" ")
    assert journal_pages.cut_text("  Short   one  ") == "Short one"


def test_a_sparkline_stays_inside_its_box_and_marks_the_bands() -> None:
    thresholds = load_scoring_config().thresholds
    points = [
        history.TrendPoint(day=TODAY - timedelta(days=90), risk=0, health=Health.HEALTHY, rank=3),
        history.TrendPoint(day=TODAY - timedelta(days=7), risk=38, health=Health.WATCH, rank=2),
        history.TrendPoint(day=TODAY, risk=100, health=Health.AT_RISK, rank=1),
    ]
    chart = journal_pages.sparkline(points, thresholds, TODAY)
    assert chart.shown
    for pair in chart.points.split():
        x, y = (float(value) for value in pair.split(","))
        assert 0 <= x <= journal_pages.TREND_WIDTH and 0 <= y <= journal_pages.TREND_HEIGHT
    assert chart.dot_y == journal_pages._INSET  # risk 100 sits at the top
    assert chart.dot_health == "at_risk"
    assert [zone.css for zone in chart.zones] == ["trend-zone--at-risk", "trend-zone--watch"]
    assert chart.caption == (
        f"Risk 100 now, 38 a week ago, 0 on {(TODAY - timedelta(days=90)).day} "
        f"{journal_pages._MONTHS[(TODAY - timedelta(days=90)).month - 1]}."
    )


def test_a_single_point_gives_the_first_day_message() -> None:
    point = history.TrendPoint(day=TODAY, risk=12, health=Health.HEALTHY, rank=9)
    chart = journal_pages.sparkline([point], load_scoring_config().thresholds, TODAY)
    assert chart.points == ""
    assert chart.caption == (
        "Wardmind started keeping score history today. The trend fills in as days pass."
    )


def test_outcome_lines_name_the_decision_and_the_arr() -> None:
    kept = journal.Outcome(
        id="o-00000001",
        kind=journal.OutcomeKind.RENEWED,
        renewal_date=TODAY,
        decided_on=date(2026, 11, 3),
        arr_before=120000,
        arr_after=130000,
        risk=20,
        priority=10,
        health=Health.HEALTHY,
        motion=Motion.RENEWAL,
        recorded_at=NOW,
    )
    assert journal_pages.outcome_line(kept) == "Renewed on 3 Nov 2026: $120,000 to $130,000"
    churned = kept.model_copy(update={"kind": journal.OutcomeKind.CHURNED, "arr_after": 0.0})
    assert journal_pages.outcome_line(churned) == "Churned on 3 Nov 2026. ARR was $120,000."
