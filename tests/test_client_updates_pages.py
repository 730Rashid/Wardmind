"""Client updates in the web: the account page's Client updates box (turn on, the client's link, a
new link, turn off, what is waiting), the nav badge, the review page (each kind, accept, reject,
the refusals, the filters, the check for new responses, the stale updates), the set-up page, and
the feed pages for the CLIENT_UPDATES role.

Everything runs through the real routes against the temp folders from conftest. Responses come
from a made-up address answered by an httpx.MockTransport, and the form link is made up too, so
nothing leaves the test.
"""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from wardmind.core import client_updates, contacts, editing, service
from wardmind.core.client_updates import (
    CODE_PLACEHOLDER,
    QUESTIONS,
    ClientUpdate,
    UpdateKind,
    UpdateStatus,
)
from wardmind.core.contacts import ContactInput, ContactSource, ContactStatus
from wardmind.core.feeds import http as feed_http
from wardmind.core.feeds import setup
from wardmind.core.feeds.models import FeedRole, MatchBy, Schedule
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import CLIENT_UPDATES_FILE, LocalStore
from wardmind.web import app as web
from wardmind.web import formatting

ORIGIN = {"origin": "http://127.0.0.1"}
CROSS_SITE = [
    {"origin": "https://evil.example"},
    {"origin": "null", "sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "cross-site"},
]
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
LINK_URL = "https://exports.example.com/responses.csv"
FORM_LINK = f"https://forms.example.com/r?entry.1={CODE_PLACEHOLDER}&entry.2=x"
ACCOUNT_NAME = "Acme Robotics"
NOW = datetime(2026, 11, 8, 9, 0)
ON = date(2026, 11, 1)
HEADER = [
    "Timestamp",
    "Client code",
    "What changed?",
    "Contact's name",
    "Job title",
    "Email",
    "Is this our main contact?",
    "Anything else?",
    "Your name",
]


class Served:
    """The responses the mock address answers with (a CSV text)."""

    def __init__(self) -> None:
        self.body = ""


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    result = Served()

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=result.body.encode("utf-8"), headers={"content-type": "text/csv"}
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return result


@pytest.fixture(autouse=True)
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


@pytest.fixture
def acme(store: LocalStore) -> str:
    """A hand-added account, Acme Robotics. Returns its id."""
    account = editing.create_account(
        store,
        {"name": ACCOUNT_NAME, "arr": "$184,000", "renewal_date": "2027-03-01"},
        today=date.today(),
    )
    return account.id


@pytest.fixture
def code(store: LocalStore, acme: str) -> str:
    """Acme has turned client updates on. Returns its code."""
    return client_updates.opt_in(store, acme, agreed_with="Jo Smith", agreed_on=ON, now=NOW).code


@pytest.fixture
def jo(store: LocalStore, acme: str) -> str:
    """Jo Smith, Acme's main contact. Returns the contact's id."""
    contact = contacts.add_contact(
        store,
        acme,
        ContactInput(name="Jo Smith", title="Head of Finance", email="jo@acme.com", champion=True),
        source=ContactSource.YOU,
        now=NOW,
    )
    return contact.id


@pytest.fixture
def form_link(store: LocalStore) -> None:
    client_updates.set_form_link(store, FORM_LINK)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _assert_clean(response: Any) -> None:
    assert response.status_code in (200, 303, 404, 422, 403), response.url
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _csv(*rows: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def _response(
    code: str,
    *,
    kind: str = "New contact",
    name: str = "Priya Shah",
    title: str = "VP Operations",
    email: str = "priya@acme.com",
    champion: str = "Yes",
    note: str = "Welcome",
    by: str = "Dana Whitfield",
    when: str = "11/8/2026 9:00:00",
) -> list[str]:
    return [when, code, kind, name, title, email, champion, note, by]


def _feed(store: LocalStore, served: Served, body: str) -> Any:
    """A CLIENT_UPDATES feed made through the same steps as its matching page's Save, with its
    first sync run from the table the setup fetched."""
    served.body = body
    draft = setup.start_draft(
        store,
        "link",
        name="Client updates",
        role=FeedRole.CLIENT_UPDATES,
        values={"url": LINK_URL, "auth": "none"},
        secret=None,
        schedule=Schedule.MANUAL,
        match_by=MatchBy.AUTO,
        connection_id=None,
        now=NOW,
    )
    return setup.commit_draft(
        store,
        draft,
        setup.suggested_mapping(draft),
        None,
        book_accounts=service.feed_book_accounts,
        now=NOW,
        today=date(2026, 11, 8),
    )


def _pending(store: LocalStore) -> list[ClientUpdate]:
    return [u for u in client_updates.load(store).updates if u.status is UpdateStatus.PENDING]


def _card(page: str, update_id: str) -> str:
    match = re.search(
        rf'<article class="card update-card" aria-labelledby="update-{update_id}">'
        r"(.*?)</article>",
        page,
        re.S,
    )
    assert match is not None, update_id
    return match.group(1)


def _client_updates_card(page: str) -> re.Match[str] | None:
    """The Client update form card on the catalog, its markup inside the article."""
    return re.search(
        r'<article class="account-card provider-card" aria-labelledby="card-client_updates">'
        r"(.*?)</article>",
        page,
        re.S,
    )


def _badge(page: str) -> str | None:
    """The Client updates badge, from its own nav link. (The Today badge counts follow-ups and
    suggestions, so the first badge on the page is not necessarily this one.)"""
    link = re.search(r'<a class="nav-link" href="/client-updates"[^>]*>(.*?)</a>', page, re.S)
    if link is None:
        return None
    match = re.search(r'<span class="nav-badge" role="img" aria-label="([^"]+)">', link.group(1))
    return match.group(1) if match else None


def _waiting(page: str) -> str:
    match = re.search(r'<p class="page-lede result-count" aria-live="polite">([^<]+)</p>', page)
    assert match is not None
    return html.unescape(match.group(1))


def _snapshot(store: LocalStore) -> str:
    path = store.root / CLIENT_UPDATES_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _mapping_fields(mapping: dict[str, str]) -> dict[str, str]:
    """The matching page's selects for a mapping keyed by question, as a browser posts them."""
    return {f"column-{index}": mapping.get(q.key, "") for index, q in enumerate(QUESTIONS)}


# The account page: the box ---------------------------------------------------------------------


def test_turning_updates_on_stores_who_and_when_and_shows_the_clients_link(
    client: TestClient, store: LocalStore, acme: str, form_link: None
) -> None:
    response = _post(
        client,
        f"/accounts/{acme}/client-updates/on",
        {"agreed_with": "Jo Smith", "agreed_on": ON.isoformat()},
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?updates=on")
    consent = client_updates.load(store).consents[acme]
    assert consent.agreed_with == "Jo Smith"
    assert consent.agreed_on == ON
    page = client.get(f"/accounts/{acme}")
    _assert_clean(page)
    text = _text(page)
    assert f"Agreed with Jo Smith on {formatting.day_label(ON, date.today())}." in text
    expected = FORM_LINK.replace(CODE_PLACEHOLDER, consent.code)
    assert f'value="{html.escape(expected)}"' in page.text
    assert 'data-copy-link="client-link"' in page.text
    assert 'class="button button--small js-only"' in page.text


def test_a_refused_turn_on_names_the_field_and_saves_nothing(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    blank = _post(
        client,
        f"/accounts/{acme}/client-updates/on",
        {"agreed_with": " ", "agreed_on": "2026-11-01"},
    )
    assert blank.status_code == 422
    _assert_clean(blank)
    assert 'id="agreed-with-error"' in blank.text
    assert "Type the name of the person who agreed." in _text(blank)

    future = _post(
        client,
        f"/accounts/{acme}/client-updates/on",
        {"agreed_with": "Jo Smith", "agreed_on": "2999-01-01"},
    )
    assert future.status_code == 422
    assert 'id="agreed-on-error"' in future.text
    assert "The date they agreed can't be in the future." in _text(future)

    missing = _post(
        client, f"/accounts/{acme}/client-updates/on", {"agreed_with": "Jo", "agreed_on": ""}
    )
    assert missing.status_code == 422
    assert "Pick the date they agreed." in _text(missing)
    assert client_updates.load(store).consents == {}


def test_without_a_form_link_the_box_asks_for_the_set_up_instead_of_a_link(
    client: TestClient, store: LocalStore, acme: str, code: str
) -> None:
    page = client.get(f"/accounts/{acme}")
    _assert_clean(page)
    text = _text(page)
    assert "Set up your update form first" in text
    assert 'href="/client-updates/setup"' in page.text
    assert 'id="client-link"' not in page.text
    assert code not in text


def test_a_new_link_gives_a_new_code_and_the_old_link_stops_working(
    client: TestClient, store: LocalStore, acme: str, code: str, form_link: None
) -> None:
    response = _post(client, f"/accounts/{acme}/client-updates/new-link")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?updates=link")
    new = client_updates.load(store).consents[acme].code
    assert new != code
    text = _text(client.get(f"/accounts/{acme}"))
    assert new in text
    assert code not in text
    assert "New link made. The old link stops working." in _text(
        client.get(response.headers["location"])
    )


def test_a_new_link_when_updates_are_off_is_refused(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    response = _post(client, f"/accounts/{acme}/client-updates/new-link")
    assert response.status_code == 422
    assert "Client updates are off for this client." in _text(response)


def test_turn_off_asks_first_then_deletes_the_waiting_updates_of_that_client(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code), _response(code, name="Tomas Berg")))
    assert len(_pending(store)) == 2

    confirm = client.get(f"/accounts/{acme}/client-updates/off")
    _assert_clean(confirm)
    assert "Turn off client updates for Acme Robotics?" in _text(confirm)
    assert "The 2 updates waiting for your review from this client are deleted with it." in _text(
        confirm
    )
    assert len(_pending(store)) == 2  # the confirmation changes nothing

    response = _post(client, f"/accounts/{acme}/client-updates/off")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?updates=off")
    assert acme not in client_updates.load(store).consents
    assert _pending(store) == []
    assert "Client updates are off, and their waiting updates were deleted." in _text(
        client.get(response.headers["location"])
    )


def test_the_box_counts_this_clients_waiting_updates_and_links_to_review(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code), _response(code, name="Tomas Berg")))
    text = _text(client.get(f"/accounts/{acme}"))
    assert "2 updates waiting for review" in text
    assert (
        'href="/client-updates">2 updates waiting for review</a>'
        in client.get(f"/accounts/{acme}").text
    )


def test_turning_updates_on_twice_is_refused_on_the_form(
    client: TestClient, store: LocalStore, acme: str, code: str
) -> None:
    response = _post(
        client,
        f"/accounts/{acme}/client-updates/on",
        {"agreed_with": "Jo Smith", "agreed_on": "2026-11-01"},
    )
    assert response.status_code == 422
    assert "Client updates are already on for this client." in _text(response)


# The nav badge -----------------------------------------------------------------------------------


def test_the_nav_badge_is_absent_at_zero_and_shows_the_count_with_its_label(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    page = client.get("/")
    _assert_clean(page)
    assert _badge(page.text) is None
    assert '<a class="nav-link" href="/client-updates"' in page.text
    _feed(
        store,
        served,
        _csv(_response(code), _response(code, name="Tomas"), _response(code, name="Ada")),
    )
    assert _badge(client.get("/").text) == "3 waiting"


# The review page: the kinds ----------------------------------------------------------------------


def test_each_kind_renders_as_its_card_says(
    client: TestClient,
    store: LocalStore,
    acme: str,
    code: str,
    jo: str,
    served: Served,
    form_link: None,
) -> None:
    _feed(
        store,
        served,
        _csv(
            _response(code, kind="New contact", name="Priya Shah", email="priya@acme.com"),
            _response(code, kind="Someone left", name="Jo Smith", email="", note="Moved on"),
            _response(code, kind="Someone left", name="Nobody Known", email="", note=""),
            _response(
                code,
                kind="Change a contact's details",
                name="Jo Smith",
                title="",
                email="jo.new@acme.com",
                champion="",
            ),
            _response(code, kind="Something else", name="", title="", email="", note="Call them"),
        ),
    )
    by_kind = {u.kind: u for u in _pending(store)}
    page = client.get("/client-updates")
    _assert_clean(page)
    text = _text(page)
    assert "5 updates waiting." in text

    add = _card(page.text, by_kind[UpdateKind.ADD].id)
    assert 'name="name" value="Priya Shah"' in add
    assert 'name="title" value="VP Operations"' in add
    assert 'name="email" value="priya@acme.com"' in add
    assert 'name="champion" value="1" checked' in add
    assert "Sent by Dana Whitfield on" in add
    assert "Accept</button>" in add and "Reject</button>" in add

    left_ids = [u.id for u in _pending(store) if u.kind is UpdateKind.LEFT]
    matched = _card(page.text, left_ids[0])
    assert "Contact: Jo Smith, Head of Finance" in html.unescape(matched)
    assert f'name="contact_id" value="{jo}"' in matched
    assert 'name="left_on"' in matched
    unmatched = _card(page.text, left_ids[1])
    assert 'name="contact_id"' in unmatched and "<select" in unmatched
    assert "Choose a contact" in unmatched
    assert "Jo Smith, Head of Finance" in html.unescape(unmatched)
    assert 'name="left_on"' in unmatched

    change = _card(page.text, by_kind[UpdateKind.CHANGE].id)
    assert "Contact: Jo Smith, Head of Finance" in html.unescape(change)
    assert 'name="email" value="jo.new@acme.com"' in change
    assert 'name="name" value="Jo Smith"' in change
    assert 'name="champion" value="1" checked' in change  # blank in the answer: keeps Jo's flag

    other = _card(page.text, by_kind[UpdateKind.OTHER].id)
    assert "Call them" in other
    assert "Mark as read</button>" in other
    assert 'name="name"' not in other
    assert "Reject</button>" in other


def test_the_kind_filter_shows_only_the_kinds_chosen_with_their_counts(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(
        store,
        served,
        _csv(
            _response(code, kind="New contact"),
            _response(code, kind="Someone left", name="Jo Smith", email=""),
            _response(code, kind="Something else", name="", email="", note="Hi"),
        ),
    )
    everything = _text(client.get("/client-updates"))
    assert "New contact" in everything and "Someone left" in everything
    only_left = client.get("/client-updates?kind=left")
    _assert_clean(only_left)
    assert only_left.text.count('class="card update-card"') == 1
    assert "Someone left" in _text(only_left)
    assert 'value="left" data-autosubmit checked' in only_left.text
    assert "Clear filters" in _text(only_left)
    assert '<span class="filter-count">1</span>' in only_left.text
    assert "No updates match these kinds." not in _text(only_left)
    none_chosen = client.get("/client-updates?kind=change")
    assert "No updates match these kinds." in _text(none_chosen)


def test_the_empty_states_say_so_and_offer_the_set_up_when_it_is_not_done(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    empty = _text(client.get("/client-updates"))
    assert "No updates waiting." in empty
    assert "Set up your update form first" in empty
    assert "No update form is connected yet." in empty


def test_the_empty_state_without_a_setup_prompt_once_the_form_is_set_up(
    client: TestClient, store: LocalStore, acme: str, code: str, form_link: None, served: Served
) -> None:
    _feed(store, served, _csv())
    text = _text(client.get("/client-updates"))
    assert "No updates waiting." in text
    assert "Set up your update form first" not in text


# The review page: accept, reject, refusals --------------------------------------------------------


def _one(store: LocalStore, kind: UpdateKind) -> ClientUpdate:
    return next(u for u in _pending(store) if u.kind is kind)


def test_accepting_an_add_with_corrected_values_adds_the_contact_and_says_so(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, name="Priya Shah", email="priya@acme.co")))
    update = _one(store, UpdateKind.ADD)
    response = _post(
        client,
        f"/client-updates/{update.id}/accept",
        {
            "name": "Priya S.",
            "title": "COO",
            "email": "priya.s@acme.com",
            "champion": "1",
        },
    )
    assert response.status_code == 303
    added = next(c for c in contacts.contacts_for(store, acme) if c.name == "Priya S.")
    assert (added.title, added.email, added.champion) == ("COO", "priya.s@acme.com", True)
    assert added.source is ContactSource.CLIENT
    stored = next(u for u in client_updates.load(store).updates if u.id == update.id)
    assert stored.status is UpdateStatus.ACCEPTED
    assert stored.name == ""
    assert "Added Priya S. to Acme Robotics." in _text(client.get(response.headers["location"]))


def test_accepting_a_left_for_the_matched_contact_says_it_is_a_risk_signal(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, kind="Someone left", name="Jo Smith", email="")))
    update = _one(store, UpdateKind.LEFT)
    response = _post(
        client,
        f"/client-updates/{update.id}/accept",
        {"contact_id": jo, "left_on": "2026-01-05"},
    )
    assert response.status_code == 303
    jo_now = next(c for c in contacts.contacts_for(store, acme) if c.id == jo)
    assert jo_now.status is ContactStatus.LEFT
    assert jo_now.left_on == date(2026, 1, 5)
    assert jo_now.source is ContactSource.CLIENT
    readings = [
        s
        for s in store.manual().signals
        if s.kind.value == "champion_left" and s.account_id == acme
    ]
    assert readings[-1].value == 1.0
    assert (
        "Marked Jo Smith as left at Acme Robotics. Main contact left is now a risk signal."
        in _text(client.get(response.headers["location"]))
    )


def test_accepting_a_left_for_a_contact_chosen_from_the_list(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    other = contacts.add_contact(
        store,
        acme,
        ContactInput(name="Tomas Berg", title="IT Director", email="", champion=False),
        source=ContactSource.YOU,
        now=NOW,
    )
    _feed(store, served, _csv(_response(code, kind="Someone left", name="Nobody Known", email="")))
    update = _one(store, UpdateKind.LEFT)
    page = client.get("/client-updates")
    card = _card(page.text, update.id)
    assert f'<option value="{other.id}"' in card
    response = _post(
        client,
        f"/client-updates/{update.id}/accept",
        {"contact_id": other.id, "left_on": "2026-01-06"},
    )
    assert response.status_code == 303
    gone = next(c for c in contacts.contacts_for(store, acme) if c.id == other.id)
    assert gone.status is ContactStatus.LEFT
    assert "Marked Tomas Berg as left at Acme Robotics." in _text(
        client.get(response.headers["location"])
    )
    assert "risk signal" not in _text(client.get(response.headers["location"]))


def test_accepting_a_change_applies_the_confirmed_values_to_the_matched_contact(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    _feed(
        store,
        served,
        _csv(
            _response(
                code,
                kind="Change a contact's details",
                name="Jo Smith",
                title="",
                email="jo.new@acme.com",
                champion="",
            )
        ),
    )
    update = _one(store, UpdateKind.CHANGE)
    response = _post(
        client,
        f"/client-updates/{update.id}/accept",
        {
            "name": "Jo Smith",
            "title": "CFO",
            "email": "jo.smith@acme.com",
            "champion": "1",
            "contact_id": jo,
        },
    )
    assert response.status_code == 303
    changed = next(c for c in contacts.contacts_for(store, acme) if c.id == jo)
    assert (changed.title, changed.email, changed.champion) == ("CFO", "jo.smith@acme.com", True)
    assert "Updated Jo Smith at Acme Robotics." in _text(client.get(response.headers["location"]))


def test_accepting_something_else_marks_it_read_and_applies_nothing(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    before = contacts.contacts_for(store, acme)
    _feed(store, served, _csv(_response(code, kind="Something else", name="", email="", note="Hi")))
    update = _one(store, UpdateKind.OTHER)
    response = _post(client, f"/client-updates/{update.id}/accept")
    assert response.status_code == 303
    assert contacts.contacts_for(store, acme) == before
    assert "Marked as read." in _text(client.get(response.headers["location"]))
    stored = next(u for u in client_updates.load(store).updates if u.id == update.id)
    assert stored.status is UpdateStatus.ACCEPTED


def test_rejecting_drops_the_update_and_says_so(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    before = contacts.contacts_for(store, acme)
    _feed(store, served, _csv(_response(code, name="Priya Shah")))
    update = _one(store, UpdateKind.ADD)
    response = _post(client, f"/client-updates/{update.id}/reject")
    assert response.status_code == 303
    assert "Rejected." in _text(client.get(response.headers["location"]))
    stored = next(u for u in client_updates.load(store).updates if u.id == update.id)
    assert stored.status is UpdateStatus.REJECTED
    assert stored.name == ""
    assert contacts.contacts_for(store, acme) == before


def test_a_refused_accept_shows_its_message_on_its_card_with_the_values_as_typed(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, name="Priya Shah", email="jo@acme.com")))
    update = _one(store, UpdateKind.ADD)
    response = _post(
        client,
        f"/client-updates/{update.id}/accept",
        {"name": "Priya Lee", "title": "", "email": "jo@acme.com", "champion": ""},
    )
    assert response.status_code == 422
    _assert_clean(response)
    card = _card(response.text, update.id)
    assert 'role="alert">Another active contact already has this email address.' in card
    assert 'name="name" value="Priya Lee"' in card
    assert _one(store, UpdateKind.ADD).status is UpdateStatus.PENDING
    assert "Priya Lee" not in [c.name for c in contacts.contacts_for(store, acme)]


def test_a_refused_left_date_shows_on_its_card(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, kind="Someone left", name="Jo Smith", email="")))
    update = _one(store, UpdateKind.LEFT)
    response = _post(
        client,
        f"/client-updates/{update.id}/accept",
        {"contact_id": jo, "left_on": "2999-01-01"},
    )
    assert response.status_code == 422
    assert "The date they left can't be in the future." in _text(response)
    assert _one(store, UpdateKind.LEFT).status is UpdateStatus.PENDING


def test_a_left_with_no_contact_chosen_is_refused_on_its_card(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, kind="Someone left", name="Nobody Known", email="")))
    update = _one(store, UpdateKind.LEFT)
    response = _post(
        client, f"/client-updates/{update.id}/accept", {"contact_id": "", "left_on": ""}
    )
    assert response.status_code == 422
    assert "Choose the contact this update is about." in _card(response.text, update.id)


def test_a_decided_or_unknown_update_is_a_not_found_page_with_a_way_back(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, name="Priya Shah")))
    update = _one(store, UpdateKind.ADD)
    assert _post(client, f"/client-updates/{update.id}/reject").status_code == 303
    again = _post(client, f"/client-updates/{update.id}/accept", {"name": "Priya"})
    assert again.status_code == 404
    text = _text(again)
    assert "That update was already decided, or it isn't there." in text
    assert 'href="/client-updates"' in again.text
    unknown = _post(client, "/client-updates/0123456789abcdef/reject")
    assert unknown.status_code == 404


# Check for new responses, and the stale updates -------------------------------------------------


def test_check_for_new_responses_reads_the_form_again(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, name="Priya Shah")))
    assert len(_pending(store)) == 1
    served.body = _csv(_response(code, name="Priya Shah"), _response(code, name="Tomas Berg"))
    response = _post(client, "/client-updates/check")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?checked=1")
    assert len(_pending(store)) == 2
    page = _text(client.get(response.headers["location"]))
    assert "Checked for new responses." in page
    assert "2 updates waiting." in page
    assert "Responses last read just now." in page


def test_the_review_page_header_says_how_many_wait_and_when_responses_were_read(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code)))
    page = client.get("/client-updates")
    _assert_clean(page)
    assert _waiting(page.text) == "1 update waiting."
    assert "Check for new responses" in page.text
    assert 'form="check-responses"' in page.text
    assert 'id="check-responses"' in page.text


def test_stale_updates_are_named_and_discarded_and_the_badge_then_matches_the_review_page(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    # Two updates for Acme, which stays in the book, and one for an account about to leave it.
    second = editing.create_account(
        store,
        {"name": "Beacon Labs", "arr": "$50,000", "renewal_date": "2027-05-01"},
        today=date.today(),
    )
    beacon_code = client_updates.opt_in(
        store, second.id, agreed_with="Ada Lovelace", agreed_on=ON, now=NOW
    ).code
    _feed(
        store,
        served,
        _csv(
            _response(code, name="Priya Shah"),
            _response(code, name="Tomas Berg"),
            _response(beacon_code, name="Ada Two"),
        ),
    )
    assert _badge(client.get("/").text) == "3 waiting"

    assert _post(client, f"/accounts/{second.id}/hide").status_code == 303
    page = client.get("/client-updates")
    _assert_clean(page)
    text = _text(page)
    assert _waiting(page.text) == "2 updates waiting."
    assert "1 update is for an account no longer in your book." in text
    assert "Discard it" in page.text
    assert _badge(client.get("/").text) == "3 waiting"

    response = _post(client, "/client-updates/discard")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?discarded=1")
    after = client.get(response.headers["location"])
    assert "Discarded 1 update." in _text(after)
    assert "no longer in your book" not in _text(after)
    assert _badge(client.get("/").text) == "2 waiting"
    assert _waiting(client.get("/client-updates").text) == "2 updates waiting."
    assert [u.account_id for u in _pending(store)] == [acme, acme]


def test_two_stale_updates_are_named_in_the_plural_with_one_discard_button(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    second = editing.create_account(
        store,
        {"name": "Beacon Labs", "arr": "$50,000", "renewal_date": "2027-05-01"},
        today=date.today(),
    )
    beacon_code = client_updates.opt_in(
        store, second.id, agreed_with="Ada", agreed_on=ON, now=NOW
    ).code
    _feed(store, served, _csv(_response(beacon_code, name="A"), _response(beacon_code, name="B")))
    _post(client, f"/accounts/{second.id}/hide")
    page = _text(client.get("/client-updates"))
    assert "2 updates are for accounts no longer in your book." in page
    assert "Discard them" in page
    response = _post(client, "/client-updates/discard")
    assert "Discarded 2 updates." in _text(client.get(response.headers["location"]))
    assert _pending(store) == []


# The set-up page ----------------------------------------------------------------------------------


def test_the_set_up_page_lists_the_questions_without_the_timestamp(
    client: TestClient, store: LocalStore
) -> None:
    page = client.get("/client-updates/setup")
    _assert_clean(page)
    text = _text(page)
    for label in (
        "Client code",
        "What changed?",
        "Contact's name",
        "Job title",
        "Email",
        "Is this our main contact?",
        "Anything else?",
        "Your name",
    ):
        assert f'<span class="setup-question">{label}</span>' in text
    assert '<span class="setup-question">Timestamp</span>' not in page.text
    assert "Microsoft Forms" in text
    assert (
        "its responses sheet must be shared as Anyone with the link, so keep that link private."
        in text
    )
    assert "WARDMINDCODE" in text


def test_the_set_up_provider_buttons_pass_the_client_updates_role(
    client: TestClient, store: LocalStore
) -> None:
    page = client.get("/client-updates/setup").text
    for provider in ("file", "airtable", "google_sheets", "link"):
        assert f'href="/connections/add/{provider}?role=client_updates"' in page
    preselected = client.get("/connections/add/link?role=client_updates")
    _assert_clean(preselected)
    assert 'value="client_updates" checked' in preselected.text
    assert "Each new response becomes an update for you to review." in _text(preselected)
    ignored = client.get("/connections/add/link?role=nonsense")
    assert 'value="accounts" checked' in ignored.text


def test_the_set_up_page_lists_connected_update_feeds_with_their_status(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code)))
    text = _text(client.get("/client-updates/setup"))
    assert "Already connected" in text
    assert "Client updates" in text
    assert "Synced" in text


def test_a_refused_form_link_is_named_on_its_field_and_a_saved_one_is_shown_and_cleared(
    client: TestClient, store: LocalStore
) -> None:
    missing_code = _post(
        client, "/client-updates/setup/link", {"link": "https://forms.example.com/r"}
    )
    assert missing_code.status_code == 422
    _assert_clean(missing_code)
    assert 'id="form-link-error"' in missing_code.text
    assert "The form link needs WARDMINDCODE exactly once, where the client code goes." in _text(
        missing_code
    )
    assert client_updates.load(store).settings.form_link == ""

    not_https = _post(
        client, "/client-updates/setup/link", {"link": "http://forms.example.com/r?x=WARDMINDCODE"}
    )
    assert not_https.status_code == 422
    assert 'id="form-link-error"' in not_https.text

    saved = _post(client, "/client-updates/setup/link", {"link": FORM_LINK})
    assert saved.status_code == 303
    assert saved.headers["location"].endswith("?saved=link")
    shown = _text(client.get(saved.headers["location"]))
    assert "Pre-filled link saved." in shown
    assert FORM_LINK in shown
    assert 'id="saved-link"' in client.get(saved.headers["location"]).text

    cleared = _post(client, "/client-updates/setup/link/clear")
    assert cleared.status_code == 303
    assert client_updates.load(store).settings.form_link == ""
    gone = client.get(cleared.headers["location"])
    assert "Pre-filled link cleared." in _text(gone)
    assert 'id="saved-link"' not in gone.text


def test_the_connections_row_for_the_role_says_what_its_last_sync_brought_in(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code), _response(code, name="Tomas Berg")))
    page = client.get("/settings/connections")
    _assert_clean(page)
    text = _text(page)
    assert "Brought in 2 new updates." in text
    assert 'href="/client-updates">Review updates</a>' in page.text


# The feed pages for the role ----------------------------------------------------------------------


def _start(client: TestClient, body: dict[str, str] | None = None) -> Any:
    data = {
        "name": "Client updates",
        "role": "client_updates",
        "schedule": "manual",
        "match_by": "auto",
        "url": LINK_URL,
        "format": "auto",
        "auth": "none",
        "records_path": "",
        "header_name": "",
        "username": "",
        "secret": "",
    }
    data.update(body or {})
    return client.post("/connections/add/link", data=data, headers=ORIGIN, follow_redirects=False)


def test_the_matching_page_for_the_role_has_a_row_per_question_and_previews_the_counts(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    served.body = _csv(
        _response(code, name="Priya Shah"),
        _response(code, name="Tomas Berg"),
        _response("ZZZZZZZZ", name="Mallory Ignored"),
    )
    started = _start(client)
    assert started.status_code == 303
    draft = str(started.headers["location"]).split("/")[3]
    page = client.get(f"/connections/feeds/{draft}/match")
    _assert_clean(page)
    text = _text(page)
    for index, question in enumerate(QUESTIONS):
        assert f'<label for="column-{index}">{question.label}</label>' in text
    assert "Timestamp" in text
    assert "3 rows" in text
    assert "2 new updates from 1 client." in text
    assert "1 row ignored: its code matches no client who opted in." in text
    assert "Mallory Ignored" not in text


def test_a_matching_page_with_a_missing_required_question_blocks_saving(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    served.body = _csv(_response(code))
    draft = str(_start(client).headers["location"]).split("/")[3]
    response = _post(
        client,
        f"/connections/feeds/{draft}/commit",
        {f"column-{index}": "" for index in range(len(QUESTIONS))},
    )
    assert response.status_code == 422
    assert (
        "Match Client code, What changed? and Contact's name to bring in client updates."
        in _text(response)
    )
    assert not [c for c in store.connections() if c.feed is not None]


def test_saving_the_matching_page_creates_the_feed_and_its_first_sync_makes_the_updates(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    served.body = _csv(_response(code, name="Priya Shah"), _response(code, name="Tomas Berg"))
    draft = str(_start(client).headers["location"]).split("/")[3]
    found = setup.load_draft(store, draft, now=datetime.now(UTC))
    assert found is not None
    mapping = client_updates.suggested_mapping(found.table)
    commit = _post(client, f"/connections/feeds/{draft}/commit", _mapping_fields(mapping))
    assert commit.status_code == 303
    assert commit.headers["location"].startswith("/settings/connections?feed_added=feed-")
    assert len(_pending(store)) == 2
    feeds = [c for c in store.connections() if c.feed and c.feed.role is FeedRole.CLIENT_UPDATES]
    assert len(feeds) == 1
    notice = _text(client.get(commit.headers["location"]))
    assert "is connected. Brought in 2 new updates." in notice


def test_the_client_update_form_card_is_connected_once_a_feed_exists(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    before = client.get("/connections/add")
    _assert_clean(before)
    assert "Client update form" in _text(before)
    card = _client_updates_card(before.text)
    assert card is not None
    assert "Connected" not in card.group(1)
    assert 'href="/client-updates/setup"' in card.group(1)

    _feed(store, served, _csv(_response(code)))
    after = client.get("/connections/add")
    card = _client_updates_card(after.text)
    assert card is not None and "Connected" in card.group(1)


def test_a_responses_row_with_script_and_quotes_is_escaped_everywhere_it_appears(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    hostile = "<script>alert(\"x\")</script> 'quoted' & <img src=x>"
    _feed(
        store,
        served,
        _csv(
            _response(
                code,
                kind="Something else",
                name=hostile,
                title=hostile,
                email="",
                note=hostile,
                by=hostile,
            )
        ),
    )
    page = client.get("/client-updates")
    _assert_clean(page)
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert" in page.text
    assert "<img src=x>" not in page.text
    update = _one(store, UpdateKind.OTHER)
    accepted = _post(client, f"/client-updates/{update.id}/accept")
    assert "<script>alert" not in client.get(accepted.headers["location"]).text


def test_the_pending_text_of_a_new_update_is_never_in_a_url(
    client: TestClient, store: LocalStore, acme: str, code: str, jo: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code, name="Priya Shah")))
    update = _one(store, UpdateKind.ADD)
    response = _post(
        client, f"/client-updates/{update.id}/accept", {"name": "Priya Shah", "email": ""}
    )
    assert response.status_code == 303
    assert "Priya" not in response.headers["location"]


# Demo mode -------------------------------------------------------------------------------------


def test_demo_mode_explains_that_client_updates_belong_to_your_data(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code)))
    _use(DataSource.DEMO)
    page = client.get("/client-updates")
    _assert_clean(page)
    assert "Client updates belong to Your data, and demo data is showing." in _text(page)
    assert client.get("/client-updates/setup").status_code == 200
    assert "Client updates belong to Your data" in _text(client.get("/client-updates/setup"))
    assert _badge(client.get("/").text) is None


def test_demo_mode_refuses_every_client_update_write_and_changes_nothing(
    client: TestClient, store: LocalStore, acme: str, code: str, served: Served
) -> None:
    _feed(store, served, _csv(_response(code)))
    update = _one(store, UpdateKind.ADD)
    before = _snapshot(store)
    _use(DataSource.DEMO)
    for path in (
        f"/client-updates/{update.id}/accept",
        f"/client-updates/{update.id}/reject",
        "/client-updates/discard",
        "/client-updates/check",
        "/client-updates/setup/link",
        "/client-updates/setup/link/clear",
    ):
        response = _post(client, path, {"link": FORM_LINK})
        assert response.status_code == 422, path
        assert "Client updates belong to Your data" in _text(response)
    assert _snapshot(store) == before


# The refusals that apply to every new route -------------------------------------------------------


@pytest.mark.parametrize("headers", CROSS_SITE)
def test_a_cross_site_post_to_every_client_update_route_is_refused_and_changes_nothing(
    client: TestClient,
    store: LocalStore,
    acme: str,
    code: str,
    jo: str,
    served: Served,
    headers: dict[str, str],
) -> None:
    _feed(store, served, _csv(_response(code, name="Priya Shah")))
    update = _one(store, UpdateKind.ADD)
    before = _snapshot(store)
    contact_before = contacts.contacts_for(store, acme)
    routes = [
        (f"/accounts/{acme}/client-updates/on", {"agreed_with": "Eve", "agreed_on": "2026-11-01"}),
        (f"/accounts/{acme}/client-updates/new-link", {}),
        (f"/accounts/{acme}/client-updates/off", {}),
        (f"/client-updates/{update.id}/accept", {"name": "Eve"}),
        (f"/client-updates/{update.id}/reject", {}),
        ("/client-updates/discard", {}),
        ("/client-updates/check", {}),
        ("/client-updates/setup/link", {"link": FORM_LINK}),
        ("/client-updates/setup/link/clear", {}),
    ]
    for path, data in routes:
        response = client.post(path, data=data, headers=headers, follow_redirects=False)
        assert response.status_code == 403, path
    assert _snapshot(store) == before
    assert contacts.contacts_for(store, acme) == contact_before
