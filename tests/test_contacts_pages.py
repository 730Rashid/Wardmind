"""Contacts in the web: the Contacts section on the account page and its routes (add, edit, mark as
left, undo, remove), validation on the right field, the email link, demo mode, the "From contacts"
label in the signal history, and the refusals that apply to every new route.

Everything runs through the real routes against the temp folders from conftest. The accounts are
added by hand (the Your data book), so no test reaches HubSpot or the network.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import contacts, editing, service
from wardmind.core.config import write_atomic
from wardmind.core.contacts import Contact, ContactStatus
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import CONTACTS_FILE, LocalStore
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
CROSS_SITE = [
    {"origin": "https://evil.example"},
    {"origin": "null", "sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "cross-site"},
]
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
NOW = datetime(2026, 11, 8, 9, 0)
PAST = date(2026, 10, 1)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _assert_clean(response: Any) -> None:
    assert response.status_code in (200, 303, 404, 422, 403), response.url
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))


def _post(client: TestClient, path: str, data: dict[str, str] | None = None, **kwargs: Any) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False, **kwargs)


@pytest.fixture(autouse=True)
def mine() -> None:
    """The Contacts section is Your data's, so every test here runs with it showing."""
    _use(DataSource.MINE)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


@pytest.fixture
def acme(store: LocalStore) -> str:
    """A hand-added account: Acme Robotics. Returns its id."""
    account = editing.create_account(
        store,
        {"name": "Acme Robotics", "arr": "$184,000", "renewal_date": "2027-03-01"},
        today=date.today(),
    )
    return account.id


def _path(account_id: str, *parts: str) -> str:
    return "/".join([f"/accounts/{account_id}", *parts])


def _add(client: TestClient, account_id: str, **fields: str) -> Any:
    data = {"name": "Priya Shah", "title": "VP Operations", "email": "", "champion": ""}
    data.update(fields)
    return _post(client, _path(account_id, "contacts"), data)


def _only(store: LocalStore, account_id: str) -> Contact:
    found = contacts.contacts_for(store, account_id)
    assert len(found) == 1, found
    return found[0]


def _snapshot(store: LocalStore) -> str:
    """The contacts file as text, so a refused request can be shown to change nothing."""
    path = store.root / CONTACTS_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


# Adding, editing, marking as left, undoing, removing -------------------------------------------


def test_adding_a_contact_stores_it_and_says_so_on_the_account_page(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    response = _add(client, acme, email="priya@acme.com", champion="1")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?contact=added")
    contact = _only(store, acme)
    assert (contact.name, contact.title, contact.email) == (
        "Priya Shah",
        "VP Operations",
        "priya@acme.com",
    )
    assert contact.champion is True
    assert contact.status is ContactStatus.ACTIVE
    page = _text(client.get(response.headers["location"]))
    assert "Contact added." in page
    assert "Priya Shah" in page


def test_edit_mark_left_undo_and_remove_go_through_the_routes(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, email="priya@acme.com")
    contact = _only(store, acme)

    edited = _post(
        client,
        _path(acme, "contacts", contact.id, "edit"),
        {"name": "Priya S.", "title": "COO", "email": "", "champion": "1"},
    )
    assert edited.status_code == 303
    assert edited.headers["location"].endswith("?contact=saved")
    changed = _only(store, acme)
    assert (changed.name, changed.title, changed.email, changed.champion) == (
        "Priya S.",
        "COO",
        "",
        True,
    )

    left = _post(
        client,
        _path(acme, "contacts", contact.id, "left"),
        {"left_on": PAST.isoformat()},
    )
    assert left.status_code == 303
    gone = _only(store, acme)
    assert gone.status is ContactStatus.LEFT
    assert gone.left_on == PAST
    assert gone.champion is True  # the history still says who it was

    undone = _post(client, _path(acme, "contacts", contact.id, "active"))
    assert undone.status_code == 303
    assert undone.headers["location"].endswith("?contact=active")
    assert _only(store, acme).status is ContactStatus.ACTIVE

    removed = _post(client, _path(acme, "contacts", contact.id, "remove"))
    assert removed.status_code == 303
    assert removed.headers["location"].endswith("?contact=removed")
    assert contacts.contacts_for(store, acme) == []


def test_mark_left_defaults_to_today_when_the_date_is_blank(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    contact = _only(store, acme)
    response = _post(client, _path(acme, "contacts", contact.id, "left"), {"left_on": ""})
    assert response.status_code == 303
    assert _only(store, acme).left_on == date.today()


def test_the_mark_as_left_box_starts_with_today(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    page = _text(client.get(f"/accounts/{acme}"))
    assert f'name="left_on" value="{date.today().isoformat()}"' in page


def test_a_future_left_date_is_refused_on_the_date_field(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    contact = _only(store, acme)
    response = _post(
        client,
        _path(acme, "contacts", contact.id, "left"),
        {"left_on": (date.today().replace(year=date.today().year + 1)).isoformat()},
    )
    assert response.status_code == 422
    _assert_clean(response)
    text = _text(response)
    assert "The date they left can't be in the future." in text
    assert _only(store, acme).status is ContactStatus.ACTIVE


def test_an_unreadable_left_date_is_refused(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    contact = _only(store, acme)
    response = _post(client, _path(acme, "contacts", contact.id, "left"), {"left_on": "soon"})
    assert response.status_code == 422
    assert "Pick the date they left." in _text(response)


def test_the_last_main_contact_leaving_records_champion_left(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, champion="1")
    contact = _only(store, acme)
    _post(client, _path(acme, "contacts", contact.id, "left"), {"left_on": PAST.isoformat()})
    readings = [
        signal
        for signal in store.manual().signals
        if signal.kind.value == "champion_left" and signal.account_id == acme
    ]
    assert readings and readings[-1].value == 1.0
    assert readings[-1].source == contacts.SIGNAL_SOURCE


# Validation lands on the field it is about -----------------------------------------------------


def test_a_bad_email_is_refused_on_the_email_field_and_keeps_what_was_typed(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    response = _add(client, acme, email="not-an-address", title="Head of Finance")
    assert response.status_code == 422
    _assert_clean(response)
    page = _text(response)
    assert 'aria-describedby="contact-add-email-error"' in response.text
    assert '<p class="field-error" id="contact-add-email-error">' in response.text
    assert "That email address doesn't look right." in page
    assert 'value="Priya Shah"' in response.text
    assert 'value="Head of Finance"' in response.text
    assert contacts.contacts_for(store, acme) == []


def test_a_blank_name_is_refused_on_the_name_field(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    response = _add(client, acme, name="   ")
    assert response.status_code == 422
    text = _text(response)
    assert "Type the contact's name." in text
    assert 'id="contact-add-name-error"' in response.text
    assert 'id="contact-add-email-error"' not in response.text


def test_a_long_job_title_is_refused_on_the_title_field(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    response = _add(client, acme, title="x" * 81)
    assert response.status_code == 422
    assert 'id="contact-add-title-error"' in response.text
    assert "Keep the job title under 80 characters." in _text(response)


def test_a_refused_edit_opens_its_form_with_the_message_on_the_field(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    contact = _only(store, acme)
    response = _post(
        client,
        _path(acme, "contacts", contact.id, "edit"),
        {"name": "Priya Shah", "title": "", "email": "nope", "champion": ""},
    )
    assert response.status_code == 422
    _assert_clean(response)
    assert '<details class="contact-disclosure" open>' in response.text
    assert f'id="contact-{contact.id}-edit-email-error"' in response.text
    assert _only(store, acme).email == ""


def test_a_second_active_contact_with_the_same_email_is_refused(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, email="priya@acme.com")
    response = _add(client, acme, name="Tomas Berg", email="PRIYA@acme.com")
    assert response.status_code == 422
    text = _text(response)
    assert "Another active contact already has this email address." in text
    assert 'id="contact-add-email-error"' in response.text
    assert len(contacts.contacts_for(store, acme)) == 1


def test_undo_left_is_refused_when_another_active_contact_has_the_email(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, email="priya@acme.com")
    first = _only(store, acme)
    _post(client, _path(acme, "contacts", first.id, "left"), {"left_on": PAST.isoformat()})
    _add(client, acme, name="Priya Lee", email="priya@acme.com")
    response = _post(client, _path(acme, "contacts", first.id, "active"))
    assert response.status_code == 422
    assert "Another active contact already has this email address." in _text(response)
    assert _only_named(store, acme, "Priya Shah").status is ContactStatus.LEFT


def _only_named(store: LocalStore, account_id: str, name: str) -> Contact:
    return next(c for c in contacts.contacts_for(store, account_id) if c.name == name)


def test_the_fifty_contact_limit_is_refused_on_the_form(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    for index in range(contacts.MAX_CONTACTS_PER_ACCOUNT):
        _add(client, acme, name=f"Contact {index:02d}")
    response = _add(client, acme, name="One more")
    assert response.status_code == 422
    assert "This account already has 50 contacts. Remove one first." in _text(response)


# Presentation: the tags, the order, the mailto link ----------------------------------------------


def test_the_email_is_a_mailto_link_only_for_an_address_contacts_validate_accepts(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, email="priya@acme.com")
    page = client.get(f"/accounts/{acme}").text
    assert 'href="mailto:priya@acme.com"' in page

    # A stored address that validation would refuse is shown as plain text, never as a link.
    odd = Contact(
        id="c-0badf00d",
        name="Odd Address",
        email="not an address",
        updated_at=NOW,
    )
    write_atomic(
        store.root / CONTACTS_FILE,
        contacts.ContactsData(by_account={acme: [odd]}).model_dump_json(),
    )
    page = _text(client.get(f"/accounts/{acme}"))
    assert "not an address" in page
    assert "mailto:not an address" not in page
    assert 'href="mailto:not' not in client.get(f"/accounts/{acme}").text


def test_left_contacts_sit_below_the_active_ones_muted_with_the_left_tag_and_date(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, name="Aiko Tanaka")
    _add(client, acme, name="Zed Zimmer")
    leaver = _only_named(store, acme, "Zed Zimmer")
    _post(client, _path(acme, "contacts", leaver.id, "left"), {"left_on": PAST.isoformat()})
    page = client.get(f"/accounts/{acme}").text
    names = re.findall(r'<li class="contact[^"]*" id="contact-[^"]+">', page)
    assert len(names) == 2
    assert "contact--left" not in names[0]
    assert "contact--left" in names[1]
    assert "Left 1 October" in _text(client.get(f"/accounts/{acme}"))
    assert 'class="pill pill--left"' in page


def test_the_main_contact_tag_carries_its_icon_and_word(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, champion="1")
    page = client.get(f"/accounts/{acme}").text
    assert 'class="pill pill--main"' in page
    assert "/static/icons.svg#star" in page
    assert "Main contact</li>" in page


def test_the_remove_page_names_the_contact_and_keeps_the_history_note(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    contact = _only(store, acme)
    response = client.get(_path(acme, "contacts", contact.id, "remove"))
    assert response.status_code == 200
    _assert_clean(response)
    text = _text(response)
    assert "Remove Priya Shah?" in text
    assert "Signals already recorded from contacts stay in the history." in text
    assert f'action="/accounts/{acme}/contacts/{contact.id}/remove"' in response.text


def test_an_unknown_contact_is_a_not_found_page_on_every_contact_route(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme)
    for path, method in (
        ("edit", "post"),
        ("left", "post"),
        ("active", "post"),
        ("remove", "post"),
        ("remove", "get"),
    ):
        request = getattr(client, method)
        response = request(
            _path(acme, "contacts", "c-deadbeef", path),
            **({"data": {"name": "x"}} if method == "post" else {}),
            headers=ORIGIN,
        )
        assert response.status_code == 404, path
        assert "That contact isn't in this account." in _text(response)


def test_an_account_with_no_contacts_says_so_and_offers_to_add_one(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    page = _text(client.get(f"/accounts/{acme}"))
    assert "No contacts yet." in page
    assert "Add a contact" in page


# Demo mode ----------------------------------------------------------------------------------


def test_demo_mode_shows_the_contacts_read_only_and_says_where_they_are_kept(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.DEMO)
    account_id = service.get_book()[0].account.id
    response = client.get(f"/accounts/{account_id}")
    assert response.status_code == 200
    _assert_clean(response)
    text = _text(response)
    assert "These are sample contacts. Contacts are kept for Your data" in text
    assert f'action="/accounts/{account_id}/contacts"' not in response.text
    assert "Add a contact" not in text


def test_demo_mode_refuses_every_contact_write_and_saves_nothing(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.DEMO)
    account_id = service.get_book()[0].account.id
    response = _post(client, _path(account_id, "contacts"), {"name": "Nobody"})
    assert response.status_code == 404
    assert "Editing is available for your own data." in _text(response)
    assert _snapshot(store) == ""


# The signal history and the rest of the account page -------------------------------------------


def test_a_reading_from_the_contacts_is_labelled_from_contacts_in_the_history(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    _add(client, acme, champion="1")
    contact = _only(store, acme)
    _post(client, _path(acme, "contacts", contact.id, "left"), {"left_on": PAST.isoformat()})
    page = _text(client.get(f"/accounts/{acme}"))
    assert "From contacts" in page
    assert "Added by you" not in page


def test_contact_text_is_escaped_everywhere_it_appears(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    hostile = "<script>alert(\"x\")</script> & 'quotes'"
    response = _add(client, acme, name=hostile, title=hostile, email="")
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    _assert_clean(page)
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert(" in page.text
    edit_page = client.get(f"/accounts/{acme}")
    assert "<script>alert" not in edit_page.text


def test_a_refused_contact_form_keeps_hostile_text_escaped(
    client: TestClient, store: LocalStore, acme: str
) -> None:
    response = _add(client, acme, name="<b>Bold</b>", email="<img src=x onerror=alert(1)>")
    assert response.status_code == 422
    assert "None" not in _text(response)
    assert "<img src=x" not in response.text
    assert "&lt;img src=x" in response.text


# The refusals that apply to every new route ----------------------------------------------------


@pytest.mark.parametrize("headers", CROSS_SITE)
def test_a_cross_site_post_to_every_contact_route_is_refused_and_changes_nothing(
    client: TestClient, store: LocalStore, acme: str, headers: dict[str, str]
) -> None:
    _add(client, acme, email="priya@acme.com")
    contact = _only(store, acme)
    before = _snapshot(store)
    routes = [
        (_path(acme, "contacts"), {"name": "Eve", "title": "", "email": "", "champion": ""}),
        (
            _path(acme, "contacts", contact.id, "edit"),
            {"name": "Eve", "title": "", "email": "", "champion": ""},
        ),
        (_path(acme, "contacts", contact.id, "left"), {"left_on": PAST.isoformat()}),
        (_path(acme, "contacts", contact.id, "active"), {}),
        (_path(acme, "contacts", contact.id, "remove"), {}),
    ]
    for path, data in routes:
        response = client.post(path, data=data, headers=headers, follow_redirects=False)
        assert response.status_code == 403, path
        assert _snapshot(store) == before, path
