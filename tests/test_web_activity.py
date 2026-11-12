"""The account page's Activity history, pinned strip, Notes box, account team and logo controls.

Covered: logging each kind of activity (with a subject, who did it and, for an email, its
direction), the refusals and what they keep, the kind filter with its counts, the newest
twenty and "Show all",
pinning and unpinning (and the cap of five), the Notes box holding only plain notes, emails logged
from the mailbox (their direction, people, long text and the chips for replies still owed), the
account team (add, the refusals, the cap, remove) and the logo in the header with its controls (only
for Your data). Runs through the real routes against the temp folders from conftest; nothing reaches
the network, and the demo is used only where demo data is the point.
"""

from __future__ import annotations

import html
import re
import struct
import zlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, journal, logos, service
from wardmind.core.journal import Direction, LoggedEmail, TeamRole, TouchKind
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import LocalStore
from wardmind.web import app as web
from wardmind.web import journal_pages

ORIGIN = {"origin": "http://127.0.0.1"}
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
DEMO_ACCOUNT = "acc-001"
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
ACTIVITY_OPEN = '<section class="account-section journal-section" id="activity"'
NOTES_OPEN = '<section class="account-section journal-section" id="notes"'
PINNED_OPEN = '<section class="account-section pinned-section" id="pinned"'
TEAM_OPEN = '<section class="account-team" id="team"'
ITEM = re.compile(r'class="note activity-item')


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _path(account_id: str, *parts: str) -> str:
    return "/".join([f"/accounts/{account_id}", *parts])


def _block(page: str, opening: str) -> str:
    """The HTML of the section whose opening tag starts with `opening`, through its closing tag."""
    start = page.index(opening)
    return page[start : page.index("</section>", start) + len("</section>")]


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


@pytest.fixture(autouse=True)
def mine() -> None:
    """Activity tests use Your data unless a test asks for the demo."""
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


def _add(
    account_id: str, kind: TouchKind, text: str, *, direction: Direction | None = None
) -> journal.Note:
    """An entry made through the core, for the tests that need entries already there."""
    return service.get_journal().add_activity(
        account_id, kind, text, TODAY, direction=direction, now=NOW, today=TODAY
    )


def _email(
    account_id: str,
    message_id: str,
    subject: str,
    days_ago: int,
    direction: Direction,
    *,
    people: list[str] | None = None,
    snippet: str = "",
) -> journal.Note | None:
    """An email as the mailbox sync logs it."""
    return service.get_journal().log_email(
        account_id,
        LoggedEmail(
            message_id=message_id,
            subject=subject,
            sent_on=TODAY - timedelta(days=days_ago),
            direction=direction,
            by="",
            people=people or [],
            snippet=snippet,
        ),
        now=NOW,
    )


def _png(width: int = 32, height: int = 32) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (3 * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _your_account(client: TestClient) -> str:
    response = client.post(
        "/accounts/new",
        data={"name": "Acme Corporation", "arr": "$120,000", "renewal_date": "2027-01-10"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, _text(response)
    return str(response.headers["location"]).split("?", 1)[0].rsplit("/", 1)[-1]


# --- Logging an activity -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "label"),
    [("call", "Call"), ("meeting", "Meeting"), ("email", "Email"), ("other", "Other")],
)
def test_each_kind_of_activity_is_logged_with_its_own_kind(
    client: TestClient, acme: str, kind: str, label: str
) -> None:
    data = {"kind": kind, "text": f"A {label.lower()} about the renewal", "direction": "sent"}
    response = _post(client, _path(acme, "activity"), data)
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=activity-added#activity"
    (entry,) = _journal(acme).notes
    assert entry.kind.value == kind
    page = _text(client.get(response.headers["location"]))
    assert "Activity logged." in page
    activity = _block(page, ACTIVITY_OPEN)
    assert f"A {label.lower()} about the renewal" in activity
    # The kind is a chip: its class, a line icon, then the word.
    assert re.search(
        rf'<span class="note-kind note-kind--{kind} chip[^"]*"><svg[^>]*>.*?</svg>{label}</span>',
        activity,
    )
    _clean(client.get(response.headers["location"]))


def test_an_email_keeps_its_direction_and_a_call_drops_it(client: TestClient, acme: str) -> None:
    _post(client, _path(acme, "activity"), {"kind": "email", "text": "Hi", "direction": "received"})
    _post(client, _path(acme, "activity"), {"kind": "call", "text": "Chat", "direction": "sent"})
    by_text = {entry.text: entry for entry in _journal(acme).notes}
    assert by_text["Hi"].direction is Direction.RECEIVED
    assert by_text["Chat"].direction is None


def test_an_email_without_a_direction_is_refused_and_keeps_its_kind(
    client: TestClient, acme: str
) -> None:
    response = _post(client, _path(acme, "activity"), {"kind": "email", "text": "No way to tell"})
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert "Pick Sent or Received for an email." in page
    assert 'value="email" checked' in page
    assert 'aria-describedby="activity-direction-help activity-direction-error"' in response.text
    assert _journal(acme).notes == []


def test_a_subject_and_who_did_it_are_kept_and_shown(client: TestClient, acme: str) -> None:
    _post(
        client,
        _path(acme, "activity"),
        {"kind": "meeting", "subject": "Renewal plan", "by": "Jo Smith", "text": "Went well."},
    )
    (entry,) = _journal(acme).notes
    assert (entry.subject, entry.by) == ("Renewal plan", "Jo Smith")
    page = _text(client.get(f"/accounts/{acme}"))
    activity = _block(page, ACTIVITY_OPEN)
    assert "Renewal plan" in activity
    assert "By Jo Smith" in activity


def test_a_subject_alone_stands_in_for_the_text(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "activity"), {"kind": "call", "subject": "Quarterly"})
    assert response.status_code == 303
    (entry,) = _journal(acme).notes
    assert entry.text == "Quarterly"


def test_a_blank_activity_is_refused_with_its_message(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "activity"), {"kind": "call", "text": "  "})
    assert response.status_code == 422
    assert "Type the note." in _text(response)
    assert 'id="activity-text-error"' in response.text
    assert _journal(acme).notes == []


@pytest.mark.parametrize(
    ("data", "message", "field"),
    [
        (
            {"kind": "call", "happened_on": (TODAY + timedelta(days=1)).isoformat(), "text": "x"},
            "That date is in the future.",
            "activity-day-error",
        ),
        (
            {"kind": "call", "subject": "s" * 301, "text": "x"},
            "Keep the subject under 300 characters.",
            "activity-subject-error",
        ),
        (
            {"kind": "call", "subject": "Two\nlines", "text": "x"},
            "A subject can't contain a line break.",
            "activity-subject-error",
        ),
        (
            {"kind": "call", "by": "n" * 101, "text": "x"},
            "Keep the name under 100 characters.",
            "activity-by-error",
        ),
        (
            {"kind": "call", "text": "x", "happened_on": "not a date"},
            "Pick a date, such as 2026-11-16.",
            "activity-day-error",
        ),
        (
            {"kind": "note", "text": "x"},
            "Pick Call, Meeting, Email or Other.",
            "activity-kind-error",
        ),
    ],
    ids=["future", "subject-long", "subject-break", "who-long", "bad-date", "kind-note"],
)
def test_each_activity_refusal_names_its_field_and_keeps_nothing(
    client: TestClient, acme: str, data: dict[str, str], message: str, field: str
) -> None:
    response = _post(client, _path(acme, "activity"), data)
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert message in page
    assert f'id="{field}"' in response.text
    assert _journal(acme).notes == []


def test_a_refused_activity_keeps_what_was_typed(client: TestClient, acme: str) -> None:
    day = (TODAY + timedelta(days=2)).isoformat()
    response = _post(
        client,
        _path(acme, "activity"),
        {"kind": "meeting", "happened_on": day, "subject": "Kept", "by": "Sam", "text": ""},
    )
    assert response.status_code == 422
    page = response.text
    assert f'value="{day}"' in page
    assert 'value="Kept"' in page
    assert 'value="Sam"' in page
    assert 'value="meeting" checked' in page


def test_an_activity_is_escaped_on_the_page(client: TestClient, acme: str) -> None:
    payload = "<script>alert('x')</script>"
    _post(client, _path(acme, "activity"), {"kind": "call", "subject": payload, "text": payload})
    response = client.get(f"/accounts/{acme}")
    assert "&lt;script&gt;alert(" in response.text
    assert "<script>alert(" not in response.text


def test_the_old_notes_route_still_logs_a_call_into_the_activity_history(
    client: TestClient, acme: str
) -> None:
    response = _post(client, _path(acme, "notes"), {"kind": "call", "text": "Old route"})
    assert response.headers["location"] == f"/accounts/{acme}?journal=activity-added#activity"
    (entry,) = _journal(acme).notes
    assert entry.kind is TouchKind.CALL


# --- The filter by kind and the newest twenty ---------------------------------------------------


def test_the_filter_lists_one_kind_and_counts_each(client: TestClient, acme: str) -> None:
    _add(acme, TouchKind.CALL, "First call")
    _add(acme, TouchKind.CALL, "Second call")
    _add(acme, TouchKind.EMAIL, "Renewal terms sent", direction=Direction.SENT)
    _add(acme, TouchKind.OTHER, "LinkedIn follow-up")
    _add(acme, TouchKind.NOTE, "Plain note kept apart")

    page = _text(client.get(f"/accounts/{acme}?activity=call"))
    activity = _block(page, ACTIVITY_OPEN)
    assert "First call" in activity and "Second call" in activity
    assert "Renewal terms sent" not in activity and "LinkedIn follow-up" not in activity
    assert re.search(r'href="/accounts/[^"]+\?activity=call#activity" aria-current="true"', page)
    assert 'Calls <span class="filter-count">2</span>' in page
    assert 'All <span class="filter-count">4</span>' in page
    assert 'Meetings <span class="filter-count">0</span>' in page
    assert 'Emails <span class="filter-count">1</span>' in page
    assert 'Other <span class="filter-count">1</span>' in page


def test_an_unknown_filter_shows_everything(client: TestClient, acme: str) -> None:
    _add(acme, TouchKind.CALL, "A call")
    _add(acme, TouchKind.EMAIL, "An email", direction=Direction.RECEIVED)
    activity = _block(_text(client.get(f"/accounts/{acme}?activity=note")), ACTIVITY_OPEN)
    assert "A call" in activity and "An email" in activity


def test_the_activity_list_shows_twenty_then_offers_show_all(client: TestClient, acme: str) -> None:
    for number in range(25):
        _add(acme, TouchKind.CALL, f"Call {number:02d}")

    page = _text(client.get(f"/accounts/{acme}"))
    assert len(ITEM.findall(_block(page, ACTIVITY_OPEN))) == 20
    assert "Show all 25" in page
    assert "activity_all=1" in page

    everything = client.get(f"/accounts/{acme}?activity=call&activity_all=1")
    assert len(ITEM.findall(_block(_text(everything), ACTIVITY_OPEN))) == 25
    assert "Show the newest 20" in _text(everything)


# --- Pinning -------------------------------------------------------------------------------------


def test_an_entry_can_be_pinned_shown_in_the_strip_and_unpinned(
    client: TestClient, acme: str
) -> None:
    entry = _add(acme, TouchKind.CALL, "Call worth keeping")
    response = _post(client, _path(acme, "entries", entry.id, "pin"), {"pinned": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=pinned#activity"
    page = _text(client.get(response.headers["location"]))
    assert "Pinned." in page
    strip = _block(page, PINNED_OPEN)
    assert "Call worth keeping" in strip
    assert "Unpin" in strip
    assert "Pinned" in _block(page, ACTIVITY_OPEN)

    unpinned = _post(client, _path(acme, "entries", entry.id, "pin"), {"pinned": "0"})
    assert unpinned.headers["location"] == f"/accounts/{acme}?journal=unpinned#activity"
    assert PINNED_OPEN not in _text(client.get(f"/accounts/{acme}"))


def test_a_sixth_pin_is_refused_beside_its_entry(client: TestClient, acme: str) -> None:
    entries = [_add(acme, TouchKind.CALL, f"Pinned {number}") for number in range(6)]
    for entry in entries[:5]:
        service.get_journal().set_pinned(acme, entry.id, True)
    response = _post(client, _path(acme, "entries", entries[5].id, "pin"), {"pinned": "1"})
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert "You can pin up to 5. Unpin one first." in page
    (sixth,) = [entry for entry in _journal(acme).notes if entry.id == entries[5].id]
    assert sixth.pinned is False


def test_a_pinned_note_is_in_the_strip_and_marked_in_the_notes_box(
    client: TestClient, acme: str
) -> None:
    entry = _add(acme, TouchKind.NOTE, "Remember the budget")
    _post(client, _path(acme, "entries", entry.id, "pin"), {"pinned": "1"})
    page = _text(client.get(f"/accounts/{acme}"))
    assert "Remember the budget" in _block(page, PINNED_OPEN)
    assert "Pinned" in _block(page, NOTES_OPEN)


# --- The Notes box holds only plain notes --------------------------------------------------------


def test_the_notes_box_holds_only_plain_notes(client: TestClient, acme: str) -> None:
    _add(acme, TouchKind.NOTE, "Plain note text")
    _add(acme, TouchKind.CALL, "Call text")
    page = _text(client.get(f"/accounts/{acme}"))
    notes = _block(page, NOTES_OPEN)
    activity = _block(page, ACTIVITY_OPEN)
    assert "Plain note text" in notes and "Call text" not in notes
    assert "Call text" in activity and "Plain note text" not in activity


def test_an_activity_edited_to_a_note_moves_to_the_notes_box(client: TestClient, acme: str) -> None:
    entry = _add(acme, TouchKind.CALL, "Moves house")
    response = _post(
        client,
        _path(acme, "notes", entry.id, "edit"),
        {"kind": "note", "happened_on": TODAY.isoformat(), "text": "Moves house"},
    )
    assert response.headers["location"] == f"/accounts/{acme}?journal=note-saved#notes"
    page = _text(client.get(response.headers["location"]))
    assert "Note saved." in page
    assert "Moves house" in _block(page, NOTES_OPEN)
    assert "Moves house" not in _block(page, ACTIVITY_OPEN)


def test_removing_an_activity_says_so_and_returns_to_the_activity(
    client: TestClient, acme: str
) -> None:
    entry = _add(acme, TouchKind.MEETING, "Short-lived")
    response = _post(client, _path(acme, "notes", entry.id, "remove"))
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=activity-removed#activity"
    assert "Activity removed." in _text(client.get(response.headers["location"]))
    assert _journal(acme).notes == []


def test_an_unknown_entry_cannot_be_pinned(client: TestClient, acme: str) -> None:
    response = _post(client, _path(acme, "entries", "n-00000000", "pin"), {"pinned": "1"})
    assert response.status_code == 404
    assert "That entry isn't there any more." in _text(response)


# --- Emails logged from the mailbox and the replies still owed -----------------------------------


def test_a_logged_email_shows_its_direction_people_and_source(
    client: TestClient, acme: str
) -> None:
    _email(
        acme,
        "one@acme.example",
        "Renewal plan",
        2,
        Direction.SENT,
        people=["priya@acme.example", "sam@acme.example"],
        snippet="Here is the plan.",
    )
    page = _text(client.get(f"/accounts/{acme}"))
    activity = _block(page, ACTIVITY_OPEN)
    assert "Logged from mailbox" in activity
    assert "Sent to priya@acme.example and sam@acme.example" in activity
    assert "Renewal plan" in activity
    assert "Here is the plan." in activity


def test_a_received_email_names_its_sender_addresses_and_the_rest(
    client: TestClient, acme: str
) -> None:
    people = ["a@acme.example", "b@acme.example", "c@acme.example", "d@acme.example"]
    _email(acme, "two@acme.example", "Question", 1, Direction.RECEIVED, people=people)
    activity = _block(_text(client.get(f"/accounts/{acme}")), ACTIVITY_OPEN)
    assert "Received from a@acme.example, b@acme.example and 2 more" in activity


def test_a_long_email_is_cut_short_with_the_rest_one_click_away(
    client: TestClient, acme: str
) -> None:
    snippet = "Thanks for the call. " * 40  # 880 characters
    _email(acme, "three@acme.example", "Long one", 1, Direction.RECEIVED, snippet=snippet)
    activity = _block(_text(client.get(f"/accounts/{acme}")), ACTIVITY_OPEN)
    assert 'class="entry-text"' in activity
    assert "Show the whole text" in activity
    preview = re.search(r'<span class="entry-preview">([^<]*)</span>', activity)
    assert preview is not None
    assert len(preview.group(1)) <= journal_pages.ENTRY_PREVIEW
    assert snippet.strip() in activity


def test_replies_still_owed_show_as_chips(client: TestClient, acme: str) -> None:
    _email(acme, "four@acme.example", "Renewal plan", 6, Direction.SENT)
    _email(acme, "five@acme.example", "Pricing question", 3, Direction.RECEIVED)
    _email(acme, "six@acme.example", "Too recent", 2, Direction.SENT)
    page = _text(client.get(f"/accounts/{acme}"))
    chips = re.search(r'<ul class="reply-list".*?</ul>', page, re.DOTALL)
    assert chips is not None
    chips = chips.group(0)
    assert "Waiting on them: Renewal plan, 6 days" in chips
    assert "Waiting on you: Pricing question, 3 days" in chips
    assert "Too recent" not in chips


# --- The account team ----------------------------------------------------------------------


def test_a_team_member_is_added_with_an_email_link_and_role(client: TestClient, acme: str) -> None:
    response = _post(
        client,
        _path(acme, "team"),
        {
            "name": "Jo Smith",
            "email": "Jo@Acme.example",
            "role": "account_manager",
            "back": "account",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=team-added#team"
    (member,) = _journal(acme).team
    assert (member.name, member.email, member.role) == (
        "Jo Smith",
        "jo@acme.example",
        TeamRole.ACCOUNT_MANAGER,
    )
    page = _text(client.get(response.headers["location"]))
    assert "Added to the account team." in page
    team = _block(page, TEAM_OPEN)
    assert "Jo Smith" in team
    assert "Account manager" in team
    assert 'href="mailto:jo@acme.example"' in team
    assert "/settings/mailbox" in team


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "", "email": "", "role": "csm"}, "Type their name."),
        ({"name": "Jo", "email": "not-an-email", "role": "csm"}, "Type an email address"),
        ({"name": "Jo", "email": "", "role": "president"}, "Pick a role for them."),
    ],
    ids=["no-name", "bad-email", "bad-role"],
)
def test_a_refused_team_member_keeps_the_values_and_opens_the_form(
    client: TestClient, acme: str, fields: dict[str, str], message: str
) -> None:
    response = _post(client, _path(acme, "team"), fields)
    assert response.status_code == 422
    _clean(response)
    page = _text(response)
    assert message in page
    assert '<details class="team-add" open>' in response.text
    assert _journal(acme).team == []
    assert "aria-invalid" in response.text


def test_a_duplicate_team_email_is_refused(client: TestClient, acme: str) -> None:
    service.get_journal().add_team_member(acme, "Jo", "jo@acme.example", TeamRole.CSM, now=NOW)
    response = _post(
        client,
        _path(acme, "team"),
        {"name": "Another Jo", "email": "JO@acme.example", "role": "csm"},
    )
    assert response.status_code == 422
    assert "Someone on the team already has that email." in _text(response)
    assert len(_journal(acme).team) == 1


def test_the_team_is_capped_at_twelve(client: TestClient, acme: str) -> None:
    for number in range(journal.MAX_TEAM):
        service.get_journal().add_team_member(acme, f"Person {number}", "", TeamRole.OTHER, now=NOW)
    response = _post(client, _path(acme, "team"), {"name": "One more", "role": "other"})
    assert response.status_code == 422
    assert "This account has 12 people on its team, the most it can keep." in _text(response)


def test_a_team_member_is_removed_and_an_unknown_one_is_a_friendly_404(
    client: TestClient, acme: str
) -> None:
    service.get_journal().add_team_member(acme, "Jo Smith", "", TeamRole.CSM, now=NOW)
    (member,) = _journal(acme).team
    response = _post(client, _path(acme, "team", member.id, "remove"))
    assert response.headers["location"] == f"/accounts/{acme}?journal=team-removed#team"
    assert "Removed from the account team." in _text(client.get(response.headers["location"]))
    assert _journal(acme).team == []

    missing = _post(client, _path(acme, "team", "t-00000000", "remove"))
    assert missing.status_code == 404
    _clean(missing)
    assert "That person isn't on this account's team." in _text(missing)


def test_the_team_names_are_offered_for_who_on_an_activity(client: TestClient, acme: str) -> None:
    service.get_journal().add_team_member(acme, "Jo Smith", "", TeamRole.CSM, now=NOW)
    page = client.get(f"/accounts/{acme}").text
    assert '<datalist id="activity-team">' in page
    assert '<option value="Jo Smith"></option>' in page


# --- The logo in the header and its controls ------------------------------------------------------


def test_a_your_data_account_shows_the_monogram_and_its_logo_controls(
    client: TestClient,
) -> None:
    account_id = _your_account(client)
    page = _text(client.get(f"/accounts/{account_id}"))
    title = page[page.index('<div class="account-title">') :]
    assert 'class="logo logo--lg logo--mono' in title
    assert title.index("logo--lg") < title.index('class="page-title account-name"')
    assert 'class="logo-disclosure no-print"' in page
    assert "Upload logo" in page
    assert "Find on website" in page
    assert "Remove logo" not in page
    _clean(client.get(f"/accounts/{account_id}"))


def test_an_uploaded_logo_shows_in_the_header_and_can_be_removed(client: TestClient) -> None:
    account_id = _your_account(client)
    upload = client.post(
        f"/accounts/{account_id}/logo",
        files={"logo": ("logo.png", _png(), "image/png")},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert upload.status_code == 303
    assert logos.get_info(service.get_store(), account_id) is not None
    page = _text(client.get(f"/accounts/{account_id}"))
    assert f'<img class="logo logo--lg" src="/logos/{account_id}"' in page
    assert "Remove logo" in page


def test_demo_data_shows_the_monogram_and_no_logo_controls(client: TestClient) -> None:
    _use(DataSource.DEMO)
    response = client.get(f"/accounts/{DEMO_ACCOUNT}")
    assert response.status_code == 200
    page = _text(response)
    assert "logo logo--lg" in page
    assert "logo-disclosure" not in page
    assert "Upload logo" not in page


def test_the_demo_journal_takes_activity_too(client: TestClient, store: LocalStore) -> None:
    _use(DataSource.DEMO)
    response = _post(
        client, f"/accounts/{DEMO_ACCOUNT}/activity", {"kind": "call", "text": "Demo call text"}
    )
    assert response.status_code == 303
    page = _text(client.get(f"/accounts/{DEMO_ACCOUNT}"))
    assert "Demo call text" in _block(page, ACTIVITY_OPEN)
    assert "You're on demo data." in page


# --- Pure helpers ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", journal_pages.ALL), ("call", "call"), ("email", "email"), ("note", journal_pages.ALL)],
)
def test_the_activity_filter_only_takes_a_kind_or_all(text: str, expected: str) -> None:
    assert journal_pages.activity_choice(text) == expected


def test_an_entry_preview_is_one_line_and_says_when_there_is_more() -> None:
    short = journal_pages.entry_preview("Line one\nline two")
    assert short == ("Line one line two", False)
    long_preview, long = journal_pages.entry_preview("word " * 100)
    assert long is True
    assert len(long_preview) <= journal_pages.ENTRY_PREVIEW
    assert "\n" not in long_preview
