"""Editing in the web: the account page additions, the edit page, add an account, readings, hide,
delete, and possible duplicates.

Everything runs through the real routes against the temp data folder from conftest. The Your-data
books are built the way the import flow builds them (spreadsheet connections and accounts added by
hand), so no test reaches HubSpot.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_brief.core import importing, secrets, service
from cs_brief.core.settings import DataSource, Settings, load_settings, save_settings
from cs_brief.core.store import ConnectionKind, LocalStore
from cs_brief.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
CROSS_SITE = [
    {"origin": "https://evil.example"},
    {"origin": "null", "sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "cross-site"},
]
TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"
# An inline script (one without src), a style block or attribute, or an event handler attribute.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)

LARKSPUR = "sp1-larkspur-freight"
QUILLON = "sp1-quillon-health"
ACME = "sp2-acme-corporation"

RENEWALS = (
    "Company,ARR,Renewal date,Website,Usage trend\n"
    '"Larkspur Freight","$184,000",2026-12-01,larkspur.example,-38\n'
    '"Quillon Health","$96,500",2026-11-15,quillon.example,6\n'
)
RENEWALS_CHANGED = (
    "Company,ARR,Renewal date,Website,Usage trend\n"
    '"Larkspur Freight","$190,000",2026-12-01,larkspur.example,-38\n'
    '"Brindle Studio","$543,000",2027-01-10,brindle.example,-5\n'
)
OTHER = (
    'Company,ARR,Renewal date,Website\n"Acme Corporation","$2,000",2026-12-02,larkspur.example\n'
)
ONE_ROW = 'Company,ARR,Renewal date\n"Larkspur Freight","$184,000",2026-12-01\n'
ANOTHER_ROW = 'Company,ARR,Renewal date\n"Quillon Health","$96,500",2026-11-15\n'


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _assert_clean(response: Any) -> None:
    """No inline script, style or event handler on a page this work adds."""
    assert response.status_code in (200, 303, 422), response.url
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))


def _tail(location: str) -> str:
    """The last path segment of a redirect location, without its query."""
    return location.split("?", 1)[0].rsplit("/", 1)[-1]


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def store() -> LocalStore:
    return service.get_store()


def _upload(client: TestClient, text: str, *, target: str = "new", new_name: str = "") -> Any:
    data = {"target": target, "new_name": new_name, "back": ""}
    files = {"file": ("renewals.csv", text.encode("utf-8"), "text/csv")}
    return client.post("/import", data=data, files=files, headers=ORIGIN, follow_redirects=False)


def _import(client: TestClient, text: str, *, target: str = "new", new_name: str = "") -> str:
    """Upload a CSV, commit it with the automatic mapping, and return the book's redirect."""
    upload = _upload(client, text, target=target, new_name=new_name)
    assert upload.status_code == 303, _text(upload)
    draft = str(upload.headers["location"]).rsplit("/", 1)[-1]
    mapping = importing.auto_map(importing.read_csv(text.encode("utf-8"), "x.csv").headers)
    fields = {
        f"column-{index}": mapping.get(spec.key, "")
        for index, spec in enumerate(importing.IMPORT_FIELDS)
    }
    commit = client.post(
        f"/import/{draft}/commit", data=fields, headers=ORIGIN, follow_redirects=False
    )
    assert commit.status_code == 303, _text(commit)
    return str(commit.headers["location"])


@pytest.fixture
def book(client: TestClient) -> None:
    """Your data: Larkspur Freight and Quillon Health, from the Renewals sheet."""
    _use(DataSource.MINE)
    _import(client, RENEWALS, new_name="Renewals")


def _add_manual(
    client: TestClient,
    name: str = "Brindle Studio",
    arr: str = "$543,000",
    renewal: str = "2027-01-10",
) -> str:
    """Add an account by hand and return its id."""
    response = client.post(
        "/accounts/new",
        data={"name": name, "arr": arr, "renewal_date": renewal},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, _text(response)
    return _tail(str(response.headers["location"]))


def _edit_fields(client: TestClient, account_id: str) -> dict[str, str]:
    """The values in the edit form's own inputs, as a browser would post them."""
    page = _text(client.get(f"/accounts/{account_id}/edit"))
    form = re.search(r'<form class="account-form".*?</form>', page, re.S)
    assert form is not None
    return {
        name: value
        for name, value in re.findall(
            r'<input[^>]*\bname="(\w+)"[^>]*\bvalue="([^"]*)"', form.group(0)
        )
    }


def _save(client: TestClient, account_id: str, **changes: str) -> Any:
    """Post the edit form with some fields changed."""
    fields = _edit_fields(client, account_id)
    fields.update(changes)
    return client.post(
        f"/accounts/{account_id}/edit", data=fields, headers=ORIGIN, follow_redirects=False
    )


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


# The account page -------------------------------------------------------------------------------


def test_the_account_page_links_to_edit_and_says_where_the_account_came_from(
    client: TestClient, book: None
) -> None:
    response = client.get(f"/accounts/{LARKSPUR}")
    text = _text(response)
    assert response.status_code == 200
    assert f'href="/accounts/{LARKSPUR}/edit"' in response.text
    assert "Edit account" in text
    assert "From Renewals." in text
    assert "Unlink" not in text
    assert "Editing is available for your own data." not in text
    _assert_clean(response)


def test_a_hand_added_account_says_it_was_added_by_hand(client: TestClient, book: None) -> None:
    account_id = _add_manual(client)
    text = _text(client.get(f"/accounts/{account_id}"))
    assert "Added by hand." in text


def test_an_edit_is_marked_in_the_facts_row_and_the_aside(client: TestClient, book: None) -> None:
    assert _save(client, LARKSPUR, arr="$135,000").status_code == 303
    page = client.get(f"/accounts/{LARKSPUR}").text
    assert re.search(r'\$135,000 ARR <span class="edited">', page)
    assert "Edited by you" in _text(client.get(f"/accounts/{LARKSPUR}"))

    assert _save(client, LARKSPUR, tier="Gold").status_code == 303
    page = client.get(f"/accounts/{LARKSPUR}").text
    assert re.search(r'<dd>Gold <span class="edited">', page)


def test_the_demo_account_page_has_the_note_and_no_edit_controls(
    client: TestClient,
) -> None:
    _use(DataSource.DEMO)
    response = client.get("/accounts/acc-001")
    assert response.status_code == 200
    assert "Editing is available for your own data." in _text(response)
    assert "Edit account" not in response.text
    assert "Edited by you" not in response.text
    assert "notice-list" not in response.text
    _assert_clean(response)


def test_readings_added_by_hand_are_marked_on_the_account_page(
    client: TestClient, book: None
) -> None:
    _post(client, f"/accounts/{LARKSPUR}/readings", {"kind": "champion_left", "yes_no": "yes"})
    page = _text(client.get(f"/accounts/{LARKSPUR}"))
    assert "Added by you" in page
    assert "Current. Added by you." in page
    _assert_clean(client.get(f"/accounts/{LARKSPUR}"))


def test_a_linked_account_shows_both_sources_and_an_unlink_button(
    client: TestClient, book: None
) -> None:
    _import(client, OTHER, new_name="Other sheet")
    assert (
        _post(
            client,
            "/duplicates/link",
            {"first": LARKSPUR, "second": ACME, "primary": LARKSPUR},
        ).status_code
        == 303
    )
    response = client.get(f"/accounts/{LARKSPUR}")
    assert "From Renewals and Other sheet (linked)." in _text(response)
    assert f'action="/accounts/{LARKSPUR}/unlink"' in response.text
    _assert_clean(response)


def test_a_notice_lists_with_a_dismiss_button_and_a_wrong_id_is_404(
    client: TestClient, store: LocalStore, book: None
) -> None:
    _save(client, LARKSPUR, arr="$135,000")
    _import(client, RENEWALS_CHANGED, target="sheet-1")

    page = client.get(f"/accounts/{LARKSPUR}")
    text = _text(page)
    assert "Renewals changed ARR to $190,000, replacing your edit of $135,000." in text
    notice_list = re.search(r'<ul class="notice-list" role="status">', page.text)
    assert notice_list is not None
    notice_id = re.search(r"/notices/([0-9a-f]{8})/dismiss", page.text)
    assert notice_id is not None
    _assert_clean(page)

    manual = _add_manual(client)
    wrong_account = _post(client, f"/accounts/{manual}/notices/{notice_id.group(1)}/dismiss")
    assert wrong_account.status_code == 404
    unknown = _post(client, f"/accounts/{LARKSPUR}/notices/00000000/dismiss")
    assert unknown.status_code == 404
    still_there = _text(client.get(f"/accounts/{LARKSPUR}"))
    assert "replacing your edit" in still_there


# The edit page ----------------------------------------------------------------------------------


def test_the_edit_page_shows_the_values_the_book_shows(client: TestClient, book: None) -> None:
    response = client.get(f"/accounts/{LARKSPUR}/edit")
    text = _text(response)
    assert response.status_code == 200
    assert '<h1 class="page-title">Edit Larkspur Freight</h1>' in response.text
    assert "From Renewals." in text
    assert 'name="name" value="Larkspur Freight"' in response.text
    assert 'name="domain" value="larkspur.example"' in response.text
    assert 'name="arr" value="$184,000"' in response.text
    assert 'type="date"' in response.text
    assert 'name="renewal_date" value="2026-12-01"' in response.text
    assert "Onboarding milestones" in text
    assert 'name="milestones_total"' in response.text
    assert "Edited by you" not in text
    assert "Add a signal reading" in text
    assert "Hide this account from my book." in text
    _assert_clean(response)


def test_saving_an_edit_stores_it_and_marks_it(
    client: TestClient, store: LocalStore, book: None
) -> None:
    response = _save(client, LARKSPUR, arr="$135,000")
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{LARKSPUR}?edited=1"
    assert LARKSPUR in store.edits()
    assert "arr" in store.edits()[LARKSPUR]

    page = _text(client.get(response.headers["location"]))
    assert "Changes saved." in page
    assert "$135,000 ARR" in page


def test_saving_without_changes_says_nothing_changed(client: TestClient, book: None) -> None:
    response = _save(client, LARKSPUR)
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{LARKSPUR}?edited=unchanged"
    assert "Nothing changed." in _text(client.get(response.headers["location"]))


def test_the_revert_button_gives_the_connection_value_back(
    client: TestClient, store: LocalStore, book: None
) -> None:
    _save(client, LARKSPUR, arr="$135,000")
    page = _text(client.get(f"/accounts/{LARKSPUR}/edit"))
    assert "Edited by you" in page
    assert "Renewals says $184,000." in page
    assert "Use Renewals's value" in page
    assert 'form="revert-arr"' in page
    assert (
        f'action="/accounts/{LARKSPUR}/revert/arr"' in client.get(f"/accounts/{LARKSPUR}/edit").text
    )
    _assert_clean(client.get(f"/accounts/{LARKSPUR}/edit"))

    reverted = _post(client, f"/accounts/{LARKSPUR}/revert/arr")
    assert reverted.status_code == 303
    assert reverted.headers["location"] == f"/accounts/{LARKSPUR}/edit?reverted=arr"
    after = client.get(reverted.headers["location"])
    assert "Using Renewals's value." in _text(after)
    assert 'name="arr" value="$184,000"' in after.text
    assert "Edited by you" not in _text(after)
    assert LARKSPUR not in store.edits()


def test_typing_the_source_value_back_clears_the_edit(
    client: TestClient, store: LocalStore, book: None
) -> None:
    _save(client, LARKSPUR, arr="$135,000")
    assert _save(client, LARKSPUR, arr="$184,000").status_code == 303
    assert LARKSPUR not in store.edits()
    assert "Edited by you" not in _text(client.get(f"/accounts/{LARKSPUR}"))


def test_all_the_errors_come_back_with_the_form_and_the_typed_input(
    client: TestClient, book: None
) -> None:
    response = _save(
        client,
        LARKSPUR,
        name="",
        arr="TBC",
        renewal_date="2026-13-40",
        milestones_total="x",
        tier="Gold",
    )
    assert response.status_code == 422
    text = _text(response)
    assert "Nothing was saved. Fix the marked fields, then save again." in text
    assert "Name can't be empty." in text
    assert 'ARR "TBC" isn\'t an amount.' in text
    assert "Renewal date isn't a date." in text
    assert "Onboarding milestones must be a whole number, 0 or more." in text
    assert response.text.count('aria-invalid="true"') >= 4
    assert 'aria-describedby="field-name-error"' in response.text
    assert 'name="tier" value="Gold"' in response.text
    _assert_clean(response)


def test_milestones_that_do_not_fit_the_total_are_refused(client: TestClient, book: None) -> None:
    response = _save(
        client,
        LARKSPUR,
        milestones_total="2",
        milestones_done="2",
        milestones_overdue="1",
    )
    assert response.status_code == 422
    assert "Milestones done and overdue can't add up to more than the total." in _text(response)


def test_a_hand_added_account_is_edited_in_place_with_no_revert(
    client: TestClient, store: LocalStore, book: None
) -> None:
    account_id = _add_manual(client)
    page = client.get(f"/accounts/{account_id}/edit")
    assert page.status_code == 200
    assert "Added by hand." in _text(page)
    assert 'class="edit-note"' not in page.text
    assert "revert-" not in page.text
    assert "Use " not in _text(page)
    assert "Edited by you" not in _text(page)
    assert "Hide this account from my book." not in page.text
    assert f'href="/accounts/{account_id}/delete"' in page.text
    _assert_clean(page)

    assert _save(client, account_id, arr="$600,000").status_code == 303
    assert account_id not in store.edits()
    saved = next(account for account in store.manual().accounts if account.id == account_id)
    assert saved.arr == 600000.0


def test_the_until_the_source_changes_notice_end_to_end(
    client: TestClient, store: LocalStore, book: None
) -> None:
    _save(client, LARKSPUR, arr="$135,000")
    assert "Edited by you" in _text(client.get(f"/accounts/{LARKSPUR}"))

    _import(client, RENEWALS_CHANGED, target="sheet-1")
    page = client.get(f"/accounts/{LARKSPUR}")
    assert "Renewals changed ARR to $190,000, replacing your edit of $135,000." in _text(page)
    assert "$190,000 ARR" in page.text
    assert "Edited by you" not in page.text
    assert LARKSPUR not in store.edits()

    notice = re.search(r"/notices/([0-9a-f]{8})/dismiss", page.text)
    assert notice is not None
    dismissed = _post(client, f"/accounts/{LARKSPUR}/notices/{notice.group(1)}/dismiss")
    assert dismissed.status_code == 303
    assert dismissed.headers["location"] == f"/accounts/{LARKSPUR}"
    after = _text(client.get(f"/accounts/{LARKSPUR}"))
    assert "replacing your edit" not in after
    assert 'class="notice-list"' not in after


# Readings ---------------------------------------------------------------------------------------


def test_readings_are_added_by_hand_and_marked(
    client: TestClient, store: LocalStore, book: None
) -> None:
    usage = _post(
        client,
        f"/accounts/{LARKSPUR}/readings",
        {"kind": "usage_trend_30d", "value": "-20", "yes_no": "yes"},
    )
    assert usage.status_code == 303
    assert usage.headers["location"] == f"/accounts/{LARKSPUR}?reading=1"
    champion = _post(
        client,
        f"/accounts/{LARKSPUR}/readings",
        {"kind": "champion_left", "value": "", "yes_no": "yes"},
    )
    assert champion.status_code == 303

    values = {signal.kind.value: signal.value for signal in store.manual().signals}
    assert values == {"usage_trend_30d": -20.0, "champion_left": 1.0}
    page = _text(client.get(f"/accounts/{LARKSPUR}?reading=1"))
    assert "Reading added." in page
    assert "Added by you" in page


def test_the_champion_reading_reads_the_yes_or_no_choice(
    client: TestClient, store: LocalStore, book: None
) -> None:
    response = _post(
        client,
        f"/accounts/{LARKSPUR}/readings",
        {"kind": "champion_left", "value": "abc", "yes_no": "no"},
    )
    assert response.status_code == 303
    values = {signal.kind.value: signal.value for signal in store.manual().signals}
    assert values == {"champion_left": 0.0}


def test_an_invalid_reading_is_refused_with_its_message(
    client: TestClient, store: LocalStore, book: None
) -> None:
    refused = _post(
        client,
        f"/accounts/{LARKSPUR}/readings",
        {"kind": "usage_trend_30d", "value": "abc", "yes_no": "yes"},
    )
    assert refused.status_code == 422
    assert 'Usage trend "abc" isn\'t a number.' in _text(refused)
    unknown = _post(
        client, f"/accounts/{LARKSPUR}/readings", {"kind": "not_a_signal", "value": "1"}
    )
    assert unknown.status_code == 422
    assert "That signal isn't one cs-brief knows." in _text(unknown)
    assert store.manual().signals == []


# Hide and show again ----------------------------------------------------------------------------


def test_hide_then_show_again_from_connections(client: TestClient, book: None) -> None:
    hidden = _post(client, f"/accounts/{LARKSPUR}/hide")
    assert hidden.status_code == 303
    assert hidden.headers["location"] == "/?hidden=Larkspur+Freight"

    book_page = client.get(hidden.headers["location"])
    assert book_page.status_code == 200
    text = _text(book_page)
    assert "Larkspur Freight is hidden." in text
    assert "Show it again from Connections." in text
    assert 'href="/settings/connections"' in book_page.text

    assert client.get(f"/accounts/{LARKSPUR}").status_code == 404
    assert client.get(f"/accounts/{LARKSPUR}/edit").status_code == 404

    shown = _post(client, f"/settings/connections/hidden/{LARKSPUR}/show")
    assert shown.status_code == 303
    assert "Account shown again." in _text(client.get(shown.headers["location"]))
    assert client.get(f"/accounts/{LARKSPUR}").status_code == 200


def test_hiding_the_only_account_keeps_my_book_with_the_notice(
    client: TestClient, book: None, store: LocalStore
) -> None:
    store.set_hidden(LARKSPUR, True)
    store.set_hidden(QUILLON, True)
    page = client.get("/?hidden=Quillon+Health", follow_redirects=False)
    assert page.status_code == 200
    assert "Quillon Health is hidden." in _text(page)


def test_a_book_with_every_account_hidden_does_not_go_back_to_setup(
    client: TestClient, store: LocalStore
) -> None:
    _use(DataSource.MINE)
    _import(client, ONE_ROW, new_name="Solo")
    assert _post(client, f"/accounts/{LARKSPUR}/hide").status_code == 303
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Set up your book" not in response.text


# Delete -----------------------------------------------------------------------------------------


def test_delete_asks_first_then_removes_a_hand_added_account(
    client: TestClient, store: LocalStore, book: None
) -> None:
    account_id = _add_manual(client)
    confirm = client.get(f"/accounts/{account_id}/delete")
    assert confirm.status_code == 200
    text = _text(confirm)
    assert "Delete Brindle Studio?" in text
    assert "It leaves your book, along with readings you added. This can't be undone." in text
    assert "Delete account" in text
    assert "Keep it" in text
    _assert_clean(confirm)

    deleted = _post(client, f"/accounts/{account_id}/delete")
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/?deleted=Brindle+Studio"
    assert "Brindle Studio was deleted." in _text(client.get(deleted.headers["location"]))
    assert client.get(f"/accounts/{account_id}").status_code == 404
    assert all(account.id != account_id for account in store.manual().accounts)


def test_a_synced_account_cannot_be_deleted(
    client: TestClient, store: LocalStore, book: None
) -> None:
    confirm = client.get(f"/accounts/{LARKSPUR}/delete")
    assert confirm.status_code == 422
    assert "Only accounts you added by hand can be deleted. Hide it instead." in _text(confirm)

    refused = _post(client, f"/accounts/{LARKSPUR}/delete")
    assert refused.status_code == 422
    assert client.get(f"/accounts/{LARKSPUR}").status_code == 200
    assert store.get_connection("sheet-1") is not None


# Add an account ---------------------------------------------------------------------------------


def test_add_an_account_in_demo_mode_switches_to_your_data(client: TestClient) -> None:
    _use(DataSource.DEMO)
    page = client.get("/accounts/new")
    assert page.status_code == 200
    assert "Add an account" in _text(page)
    _assert_clean(page)

    response = _post(
        client,
        "/accounts/new",
        {"name": "Brindle Studio", "arr": "$543,000", "renewal_date": "2027-01-10"},
    )
    assert response.status_code == 303
    assert load_settings().source is DataSource.MINE
    account_id = _tail(str(response.headers["location"]))
    assert response.headers["location"] == f"/accounts/{account_id}?added=1"
    shown = _text(client.get(response.headers["location"]))
    assert "Account added." in shown
    assert "Brindle Studio" in shown
    assert "Added by hand." in shown


def test_add_an_account_validates_and_keeps_the_input(client: TestClient, book: None) -> None:
    response = _post(
        client, "/accounts/new", {"name": "", "arr": "", "renewal_date": "", "tier": "Enterprise"}
    )
    assert response.status_code == 422
    text = _text(response)
    assert "Nothing was saved. Fix the marked fields, then save again." in text
    assert "Name is required." in text
    assert "ARR is required." in text
    assert "Renewal date is required." in text
    assert 'name="tier" value="Enterprise"' in response.text
    assert 'aria-invalid="true"' in response.text
    _assert_clean(response)


def test_the_setup_and_connections_links_to_add_an_account_resolve(
    client: TestClient, book: None
) -> None:
    setup = client.get("/setup")
    assert 'href="/accounts/new"' in setup.text
    connections = client.get("/settings/connections")
    assert 'href="/accounts/new"' in connections.text
    assert client.get("/accounts/new").status_code == 200


# Possible duplicates ----------------------------------------------------------------------------


def test_the_duplicates_page_lists_each_pair_with_its_reason(
    client: TestClient, book: None
) -> None:
    _import(client, OTHER, new_name="Other sheet")
    response = client.get("/duplicates")
    text = _text(response)
    assert response.status_code == 200
    assert "Possible duplicates" in text
    assert "Same website: larkspur.example" in text
    assert "Larkspur Freight" in text and "Acme Corporation" in text
    assert "Link these" in text and "Not the same" in text
    assert "They differ on ARR and renewal date." in text
    assert "Account to keep" in text
    assert "The one you keep supplies the name, ARR, dates and owner." in text
    assert "Other sheet" in text
    _assert_clean(response)
    # "Not the same" sits beside "Link these" but submits its own dismiss form.
    assert re.search(r'<button[^>]*form="dismiss-1"[^>]*>Not the same</button>', response.text)
    assert 'id="dismiss-1"' in response.text and 'action="/duplicates/dismiss"' in response.text


def test_linking_merges_the_accounts_and_unlink_restores_them(
    client: TestClient, book: None
) -> None:
    _import(client, OTHER, new_name="Other sheet")
    linked = _post(
        client,
        "/duplicates/link",
        {"first": LARKSPUR, "second": ACME, "primary": LARKSPUR},
    )
    assert linked.status_code == 303
    assert linked.headers["location"] == "/duplicates?linked=Larkspur+Freight"
    assert "Linked Larkspur Freight." in _text(client.get(linked.headers["location"]))
    assert "Same website" not in _text(client.get("/duplicates"))

    merged = _text(client.get(f"/accounts/{LARKSPUR}"))
    assert "From Renewals and Other sheet (linked)." in merged
    assert client.get(f"/accounts/{ACME}").status_code == 404

    unlinked = _post(client, f"/accounts/{LARKSPUR}/unlink")
    assert unlinked.status_code == 303
    assert unlinked.headers["location"] == f"/accounts/{LARKSPUR}?unlinked=1"
    after = _text(client.get(unlinked.headers["location"]))
    assert "Unlinked. The accounts show separately again." in after
    assert "From Renewals." in after
    assert client.get(f"/accounts/{ACME}").status_code == 200


def test_dismissing_a_pair_stops_it_being_suggested(client: TestClient, book: None) -> None:
    _import(client, OTHER, new_name="Other sheet")
    dismissed = _post(
        client,
        "/duplicates/dismiss",
        {"first": ACME, "second": LARKSPUR},
    )
    assert dismissed.status_code == 303
    assert dismissed.headers["location"] == "/duplicates?dismissed=1"
    page = _text(client.get(dismissed.headers["location"]))
    assert "Got it. They won't be suggested again." in page
    assert "No possible duplicates right now." in page
    assert "might be duplicates" not in _text(client.get("/"))


def test_the_duplicates_empty_state_links_back_to_my_book(client: TestClient, book: None) -> None:
    response = client.get("/duplicates")
    assert "No possible duplicates right now." in _text(response)
    assert 'href="/"' in response.text
    assert "Back to My book" in _text(response)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {"first": LARKSPUR, "second": "nope", "primary": LARKSPUR},
            "That account isn't in your book.",
        ),
        (
            {"first": LARKSPUR, "second": LARKSPUR, "primary": LARKSPUR},
            "Choose at least two accounts to link.",
        ),
        (
            {"first": LARKSPUR, "second": QUILLON, "primary": LARKSPUR},
            "Only accounts from different connections can be linked.",
        ),
    ],
)
def test_a_bad_link_is_refused_with_a_message(
    client: TestClient, book: None, data: dict[str, str], message: str
) -> None:
    _import(client, OTHER, new_name="Other sheet")
    response = _post(client, "/duplicates/link", data)
    assert response.status_code == 422
    assert message in _text(response)


def test_a_link_with_a_primary_outside_the_pair_is_refused(client: TestClient, book: None) -> None:
    _import(client, OTHER, new_name="Other sheet")
    response = _post(
        client,
        "/duplicates/link",
        {"first": LARKSPUR, "second": ACME, "primary": QUILLON},
    )
    assert response.status_code == 422
    assert "The account to keep has to be one of the accounts you're linking." in _text(response)


def test_a_dismissal_of_a_pair_that_is_not_suggested_is_refused(
    client: TestClient, book: None
) -> None:
    response = _post(client, "/duplicates/dismiss", {"first": LARKSPUR, "second": QUILLON})
    assert response.status_code == 422
    assert "Those accounts aren't suggested as duplicates any more." in _text(response)
    unknown = _post(client, "/duplicates/dismiss", {"first": LARKSPUR, "second": "nope"})
    assert unknown.status_code == 422


# Demo data: every new route is a 404, except adding an account --------------------------------

DEMO_ROUTES = [
    ("get", "/accounts/acc-001/edit"),
    ("post", "/accounts/acc-001/edit"),
    ("post", "/accounts/acc-001/revert/arr"),
    ("post", "/accounts/acc-001/readings"),
    ("post", "/accounts/acc-001/notices/0123abcd/dismiss"),
    ("post", "/accounts/acc-001/hide"),
    ("get", "/accounts/acc-001/delete"),
    ("post", "/accounts/acc-001/delete"),
    ("post", "/accounts/acc-001/unlink"),
    ("get", "/duplicates"),
    ("post", "/duplicates/link"),
    ("post", "/duplicates/dismiss"),
]


@pytest.mark.parametrize(("method", "path"), DEMO_ROUTES)
def test_every_new_route_is_a_404_in_demo_mode(client: TestClient, method: str, path: str) -> None:
    _use(DataSource.DEMO)
    if method == "get":
        response = client.get(path)
    else:
        response = _post(
            client, path, {"first": "acc-001", "second": "acc-002", "primary": "acc-001"}
        )
    assert response.status_code == 404, path


def test_the_demo_add_page_is_the_one_new_page_that_works(client: TestClient) -> None:
    _use(DataSource.DEMO)
    assert client.get("/accounts/new").status_code == 200


# Security: the cross-site sweep, inline markup and the token ------------------------------------

NEW_POSTS = [
    ("/accounts/new", {"name": "Acme", "arr": "1", "renewal_date": "2027-01-01"}),
    (f"/accounts/{LARKSPUR}/edit", {"name": "Larkspur"}),
    (f"/accounts/{LARKSPUR}/revert/arr", {}),
    (f"/accounts/{LARKSPUR}/readings", {"kind": "usage_trend_30d", "value": "1"}),
    (f"/accounts/{LARKSPUR}/notices/0123abcd/dismiss", {}),
    (f"/accounts/{LARKSPUR}/hide", {}),
    (f"/accounts/{LARKSPUR}/delete", {}),
    (f"/accounts/{LARKSPUR}/unlink", {}),
    ("/duplicates/link", {"first": LARKSPUR, "second": ACME, "primary": LARKSPUR}),
    ("/duplicates/dismiss", {"first": ACME, "second": LARKSPUR}),
]


@pytest.mark.parametrize(("path", "data"), NEW_POSTS)
@pytest.mark.parametrize("headers", CROSS_SITE)
def test_a_cross_site_post_is_refused_and_changes_nothing(
    client: TestClient,
    store: LocalStore,
    book: None,
    path: str,
    data: dict[str, str],
    headers: dict[str, str],
) -> None:
    _import(client, OTHER, new_name="Other sheet")

    def snapshot() -> dict[Path, bytes]:
        return {
            p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()
        }

    before = snapshot()
    response = client.post(path, data=data, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    assert snapshot() == before


def test_the_new_pages_have_no_inline_markup(
    client: TestClient, store: LocalStore, book: None
) -> None:
    manual = _add_manual(client)
    _import(client, OTHER, new_name="Other sheet")
    for path in (
        "/accounts/new",
        f"/accounts/{LARKSPUR}",
        f"/accounts/{LARKSPUR}/edit",
        f"/accounts/{LARKSPUR}/delete",
        f"/accounts/{manual}/edit",
        f"/accounts/{manual}/delete",
        "/duplicates",
        "/",
    ):
        _assert_clean(client.get(path))


def test_the_token_is_in_no_new_page(client: TestClient, store: LocalStore, book: None) -> None:
    # HubSpot is saved but switched off, so no page reaches HubSpot. The token must still stay out.
    secrets.save_hubspot_token(TOKEN)
    store.add_connection(ConnectionKind.HUBSPOT, "HubSpot")
    hubspot = store.get_connection("hubspot")
    assert hubspot is not None
    store.update_connection(hubspot.model_copy(update={"enabled": False}))
    _import(client, OTHER, new_name="Other sheet")
    for path in (
        "/accounts/new",
        f"/accounts/{LARKSPUR}",
        f"/accounts/{LARKSPUR}/edit",
        f"/accounts/{LARKSPUR}/delete",
        "/duplicates",
        "/",
    ):
        response = client.get(path)
        assert "SECRETTEST" not in response.text, path
        assert TOKEN not in response.text, path
        assert TOKEN.encode() not in response.content, path
