"""Client updates (core/client_updates.py): opt-in and codes, the form link, reading the form's
responses (suggested mapping, parse_kind, ingest), accepting and rejecting, and forgetting or moving
an account's consent and updates. The store is in a temp folder. No network.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest

from wardmind.core import client_updates, contacts, importing
from wardmind.core.client_updates import (
    CODE_ALPHABET,
    CODE_PLACEHOLDER,
    MAX_NOTE,
    MAX_PENDING_PER_ACCOUNT,
    ClientUpdate,
    IngestResult,
    UpdateKind,
    UpdateStatus,
)
from wardmind.core.contacts import ContactInput, ContactSource
from wardmind.core.importing import DateOrder
from wardmind.core.models import Account, Milestones
from wardmind.core.store import LocalStore

NOW = datetime(2026, 11, 8, 9, 0)
LATER = datetime(2026, 11, 9, 9, 0)
ON = date(2026, 11, 1)
BOOK = {"acc-1", "acc-2"}
LINK = f"https://forms.example.com/r?entry.1={CODE_PLACEHOLDER}&entry.2=x"
HEADER = (
    "Timestamp,Client code,What changed?,Contact's name,Job title,Email,"
    "Is this our main contact?,Anything else?,Your name"
)
MAPPING = {
    "code": "Client code",
    "kind": "What changed?",
    "name": "Contact's name",
    "title": "Job title",
    "email": "Email",
    "champion": "Is this our main contact?",
    "note": "Anything else?",
    "submitted_by": "Your name",
    "submitted_at": "Timestamp",
}


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def table(text: str) -> importing.Table:
    return importing.read_csv(text.encode("utf-8"), "responses.csv")


def form(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def response(
    code: str,
    *,
    when: str = "11/8/2026 9:00:00",
    kind: str = "New contact",
    name: str = "Priya Shah",
    title: str = "VP Operations",
    email: str = "priya@acme.com",
    champion: str = "Yes",
    note: str = "Welcome",
    by: str = "Dana Whitfield",
) -> str:
    return ",".join([when, code, kind, name, title, email, champion, note, by])


def consent(store: LocalStore, account: str = "acc-1", name: str = "Dana Whitfield") -> str:
    return client_updates.opt_in(store, account, agreed_with=name, agreed_on=ON, now=NOW).code


def ingest(
    store: LocalStore,
    text: str,
    *,
    book: set[str] = BOOK,
    mapping: dict[str, str] = MAPPING,
    save: bool = True,
    order: DateOrder = DateOrder.DMY,
    now: datetime = NOW,
) -> IngestResult:
    return client_updates.ingest(
        store,
        table(text),
        mapping,
        connection_id="feed-1",
        book_ids=book,
        date_order=order,
        now=now,
        save=save,
    )


def updates(store: LocalStore) -> list[ClientUpdate]:
    return client_updates.load(store).updates


def only_update(store: LocalStore) -> ClientUpdate:
    found = updates(store)
    assert len(found) == 1
    return found[0]


def acct(account_id: str, name: str = "Acme") -> Account:
    return Account(
        id=account_id,
        name=name,
        arr=1.0,
        tier="Growth",
        owner="Dana",
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
    )


def contacts_of(store: LocalStore, account: str = "acc-1") -> list[contacts.Contact]:
    return contacts.contacts_for(store, account)


def contact_readings(store: LocalStore) -> list[float]:
    return [s.value for s in store.manual().signals if s.source == contacts.SIGNAL_SOURCE]


# Opt-in and codes ------------------------------------------------------------------------------


def test_opt_in_gives_a_code_of_eight_characters_from_the_alphabet(store):
    code = consent(store)

    assert len(code) == client_updates.CODE_LENGTH
    assert set(code) <= set(CODE_ALPHABET)
    saved = client_updates.load(store).consents["acc-1"]
    assert (saved.agreed_with, saved.agreed_on, saved.created_at) == (
        "Dana Whitfield",
        ON,
        NOW,
    )


def test_codes_are_unique_across_consents(store, monkeypatch):
    picks = iter("A" * 8 + "A" * 8 + "B" * 8)  # the second draw repeats the first code
    monkeypatch.setattr(client_updates.secrets, "choice", lambda alphabet: next(picks))

    first = client_updates.opt_in(store, "acc-1", agreed_with="Dana", agreed_on=ON, now=NOW)
    second = client_updates.opt_in(store, "acc-2", agreed_with="Sam", agreed_on=ON, now=NOW)

    assert (first.code, second.code) == ("AAAAAAAA", "BBBBBBBB")


@pytest.mark.parametrize(
    ("name", "on", "message"),
    [
        ("   ", ON, "Type the name"),
        ("x" * 81, ON, "80 characters"),
        ("Dana\nWhitfield", ON, "line breaks"),
        ("Dana", date(2026, 11, 9), "can't be in the future"),
    ],
)
def test_opt_in_refuses_a_blank_long_multi_line_or_future_agreement(store, name, on, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        client_updates.opt_in(store, "acc-1", agreed_with=name, agreed_on=on, now=NOW)
    assert client_updates.load(store).consents == {}


def test_opt_in_twice_is_refused(store):
    consent(store)
    with pytest.raises(ValueError, match="already on"):
        consent(store)


def test_a_date_agreed_today_is_fine(store):
    client_updates.opt_in(store, "acc-1", agreed_with="Dana", agreed_on=NOW.date(), now=NOW)
    assert "acc-1" in client_updates.load(store).consents


def test_new_code_makes_the_old_link_stop_working(store):
    old = consent(store)

    new = client_updates.new_code(store, "acc-1", now=LATER).code

    assert new != old
    result = ingest(store, form(response(old)))
    assert (result.new, result.ignored) == (0, 1)
    assert ingest(store, form(response(new))).new == 1


def test_new_code_for_an_account_that_is_off_is_a_lookup_error(store):
    with pytest.raises(LookupError):
        client_updates.new_code(store, "acc-1", now=NOW)


def test_opt_out_deletes_the_consent_and_the_pending_updates(store):
    code = consent(store)
    ingest(store, form(response(code), response(code, name="Sam Okafor")))
    assert len(updates(store)) == 2

    assert client_updates.opt_out(store, "acc-1") is True

    data = client_updates.load(store)
    assert data.consents == {}
    assert data.updates == []
    assert client_updates.opt_out(store, "acc-1") is False


# The form link --------------------------------------------------------------------------------


def test_set_form_link_saves_the_stripped_link(store):
    assert client_updates.set_form_link(store, f"  {LINK}  ") == LINK
    assert client_updates.load(store).settings.form_link == LINK


@pytest.mark.parametrize(
    ("link", "message"),
    [
        (LINK.replace("https://", "http://"), "https://"),
        (f"http://localhost:8000/r?code={CODE_PLACEHOLDER}", "https://"),
        (f"ftp://forms.example.com/{CODE_PLACEHOLDER}", "https://"),
        ("https://forms.example.com/r", "exactly once"),
        (f"https://forms.example.com/r?a={CODE_PLACEHOLDER}&b={CODE_PLACEHOLDER}", "exactly once"),
        (f"https://forms.example.com/{'a' * 2000}?c={CODE_PLACEHOLDER}", "2000 characters"),
        (f"https://user:pw@forms.example.com/r?code={CODE_PLACEHOLDER}", "password"),
    ],
)
def test_set_form_link_refuses_what_it_should(store, link, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        client_updates.set_form_link(store, link)
    assert client_updates.load(store).settings.form_link == ""


def test_an_empty_form_link_clears_it(store):
    client_updates.set_form_link(store, LINK)

    assert client_updates.set_form_link(store, "   ") == ""
    assert client_updates.load(store).settings.form_link == ""


def test_update_link_puts_the_clients_own_code_in_the_form_link(store):
    client_updates.set_form_link(store, LINK)
    code = consent(store)

    data = client_updates.load(store)
    assert client_updates.update_link(data, "acc-1") == LINK.replace(CODE_PLACEHOLDER, code)
    assert client_updates.update_link(data, "acc-2") is None

    client_updates.set_form_link(store, "")
    assert client_updates.update_link(client_updates.load(store), "acc-1") is None


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("abcd-2345", "ABCD2345"),
        (" ab cd 23 45 ", "ABCD2345"),
        ("ABCD2345", "ABCD2345"),
        ("ab–cd", "ABCD"),
    ],
)
def test_normalize_code_ignores_case_spaces_and_dashes(raw, clean):
    assert client_updates.normalize_code(raw) == clean


# Reading the form's responses ------------------------------------------------------------------


GOOGLE_HEADERS = [
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
MICROSOFT_HEADERS = [
    "Completion time",
    "Email",
    "Name",
    "Client code",
    "What changed?",
    "Job title",
    "Anything else?",
]


def headers_only(headers: list[str]) -> importing.Table:
    return importing.read_csv((",".join(headers) + "\n").encode("utf-8"), "form.csv")


def test_suggested_mapping_matches_a_google_forms_export():
    assert client_updates.suggested_mapping(headers_only(GOOGLE_HEADERS)) == MAPPING


def test_suggested_mapping_matches_a_microsoft_forms_export():
    assert client_updates.suggested_mapping(headers_only(MICROSOFT_HEADERS)) == {
        "code": "Client code",
        "kind": "What changed?",
        "name": "Name",
        "title": "Job title",
        "email": "Email",
        "note": "Anything else?",
        "submitted_at": "Completion time",
    }


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("New contact", UpdateKind.ADD),
        ("new", UpdateKind.ADD),
        ("A colleague joined", UpdateKind.ADD),
        ("Someone left", UpdateKind.LEFT),
        ("left", UpdateKind.LEFT),
        ("They are leaving", UpdateKind.LEFT),
        ("gone", UpdateKind.LEFT),
        ("Change a contact's details", UpdateKind.CHANGE),
        ("new email", UpdateKind.CHANGE),
        ("update", UpdateKind.CHANGE),
        ("Moved to another team", UpdateKind.CHANGE),
        ("Something else", UpdateKind.OTHER),
        ("Nothing much", UpdateKind.OTHER),
        ("", UpdateKind.OTHER),
    ],
)
def test_parse_kind_reads_the_form_answer(text, kind):
    assert client_updates.parse_kind(text) is kind


def test_new_rows_become_pending_updates_for_the_clients_account(store):
    code = consent(store)

    result = ingest(
        store,
        form(response(code), response(code, kind="Someone left", name="Sam Okafor")),
    )

    assert result == IngestResult(new=2, already_seen=0, ignored=0, accounts=1)
    found = updates(store)
    assert [u.kind for u in found] == [UpdateKind.ADD, UpdateKind.LEFT]
    assert all(u.status is UpdateStatus.PENDING and u.account_id == "acc-1" for u in found)
    assert all(u.source_connection == "feed-1" and u.received_at == NOW for u in found)
    first = found[0]
    assert (first.name, first.title, first.email) == (
        "Priya Shah",
        "VP Operations",
        "priya@acme.com",
    )
    assert (first.champion, first.note, first.submitted_by) == (True, "Welcome", "Dana Whitfield")
    assert re.fullmatch(r"[0-9a-f]{16}", first.id)


def test_a_resync_recognises_the_rows_it_has_read(store):
    code = consent(store)
    text = form(response(code))
    ingest(store, text)

    again = ingest(store, text)

    assert again == IngestResult(new=0, already_seen=1, ignored=0, accounts=0)
    assert len(updates(store)) == 1


def test_a_decided_update_is_not_brought_back_by_a_resync(store):
    code = consent(store)
    text = form(response(code))
    ingest(store, text)
    client_updates.reject(store, only_update(store).id, now=LATER)

    again = ingest(store, text)

    assert again.already_seen == 1 and again.new == 0
    assert only_update(store).status is UpdateStatus.REJECTED


def test_a_row_with_no_code_is_ignored_and_nothing_is_kept(store):
    consent(store)

    result = ingest(store, form(response("")))

    assert result == IngestResult(new=0, already_seen=0, ignored=1, accounts=0)
    assert updates(store) == []


def test_a_row_with_an_unknown_code_is_ignored_and_nothing_is_kept(store):
    consent(store)

    result = ingest(store, form(response("ZZZZZZZZ")))

    assert (result.new, result.ignored) == (0, 1)
    assert updates(store) == []


def test_a_row_for_an_account_no_longer_in_the_book_is_ignored(store):
    code = consent(store)

    result = ingest(store, form(response(code)), book={"acc-2"})

    assert (result.new, result.ignored) == (0, 1)
    assert updates(store) == []


def test_an_account_holds_at_most_the_pending_cap(store):
    code = consent(store)
    rows = [
        response(code, name=f"Person {number}") for number in range(MAX_PENDING_PER_ACCOUNT + 1)
    ]

    result = ingest(store, form(*rows))

    assert (result.new, result.ignored) == (MAX_PENDING_PER_ACCOUNT, 1)
    assert len(updates(store)) == MAX_PENDING_PER_ACCOUNT


def test_control_characters_are_removed_and_text_is_cut_to_the_limit(store):
    code = consent(store)

    ingest(
        store,
        form(
            response(code, name="Priya\x07 Shah", title="  VP\tOps  ", note="x" * (MAX_NOTE + 50))
        ),
    )

    found = only_update(store)
    assert (found.name, found.title) == ("Priya Shah", "VP Ops")
    assert found.note == "x" * MAX_NOTE


def test_a_bad_email_becomes_blank_and_the_row_is_still_kept(store):
    code = consent(store)

    result = ingest(store, form(response(code, email="priya at acme")))

    assert result.new == 1
    assert only_update(store).email == ""


@pytest.mark.parametrize(
    ("answer", "champion"),
    [("Yes", True), ("No", False), ("maybe", None), ("", None)],
)
def test_the_champion_answer_is_yes_no_or_unanswered(store, answer, champion):
    code = consent(store)

    ingest(store, form(response(code, champion=answer)))

    assert only_update(store).champion is champion


@pytest.mark.parametrize(
    ("when", "order", "expected"),
    [
        ("11/8/2026 9:00:00", DateOrder.DMY, datetime(2026, 8, 11)),
        ("11/8/2026 9:00:00", DateOrder.MDY, datetime(2026, 11, 8)),
        ("2026-11-08 09:15:00", DateOrder.DMY, datetime(2026, 11, 8)),
        ("", DateOrder.DMY, None),
        ("sometime", DateOrder.DMY, None),
    ],
)
def test_submitted_at_is_read_with_the_date_order(store, when, order, expected):
    code = consent(store)

    ingest(store, form(response(code, when=when)), order=order)

    assert only_update(store).submitted_at == expected


def test_save_false_counts_the_rows_and_writes_nothing(store):
    code = consent(store)

    result = ingest(store, form(response(code)), save=False)

    assert result.new == 1
    assert updates(store) == []
    assert client_updates.load(store).updates == []


def test_a_mapping_without_a_required_question_raises_and_names_it(store):
    code = consent(store)
    without_kind = {key: header for key, header in MAPPING.items() if key != "kind"}
    without_code = {key: header for key, header in MAPPING.items() if key != "code"}

    with pytest.raises(ValueError, match="What changed"):
        ingest(store, form(response(code)), mapping=without_kind)
    with pytest.raises(ValueError, match="Client code"):
        ingest(store, form(response(code)), mapping=without_code)
    with pytest.raises(ValueError, match="What changed"):
        ingest(store, HEADER + "\n", mapping=without_kind)


def test_a_form_with_no_responses_yet_is_a_success(store):
    result = ingest(store, HEADER + "\n")

    assert result == IngestResult()
    assert updates(store) == []


def test_a_response_is_read_with_the_mapping_not_the_column_order(store):
    code = consent(store)
    shuffled = "Your name,Contact's name,Client code,What changed?,Timestamp,Email\n"
    row = f"Dana Whitfield,Priya Shah,{code},New contact,11/8/2026 9:00:00,priya@acme.com\n"
    mapping = {
        "code": "Client code",
        "kind": "What changed?",
        "name": "Contact's name",
        "email": "Email",
        "submitted_by": "Your name",
        "submitted_at": "Timestamp",
    }

    result = client_updates.ingest(
        store,
        table(shuffled + row),
        mapping,
        connection_id="feed-1",
        book_ids=BOOK,
        date_order=DateOrder.DMY,
        now=NOW,
    )

    assert result.new == 1
    assert only_update(store).submitted_by == "Dana Whitfield"


# Accepting, rejecting ------------------------------------------------------------------------


def test_accept_an_add_applies_the_csms_corrected_values(store):
    code = consent(store)
    ingest(store, form(response(code)))
    update = only_update(store)
    corrected = ContactInput(
        name="Priya Shah", title="Vice President", email="priya@acme.com", champion=True
    )

    kind = client_updates.accept(
        store, update.id, values=corrected, contact_id=None, left_on=None, now=LATER
    )

    assert kind is UpdateKind.ADD
    added = contacts_of(store)
    assert len(added) == 1
    assert (added[0].name, added[0].title, added[0].email) == (
        "Priya Shah",
        "Vice President",
        "priya@acme.com",
    )
    assert added[0].source is ContactSource.CLIENT
    assert contact_readings(store) == [0.0]


def test_accept_an_add_without_values_uses_the_clients_own_values(store):
    code = consent(store)
    ingest(store, form(response(code)))

    client_updates.accept(
        store, only_update(store).id, values=None, contact_id=None, left_on=None, now=LATER
    )

    added = contacts_of(store)[0]
    assert (added.name, added.title, added.email, added.champion) == (
        "Priya Shah",
        "VP Operations",
        "priya@acme.com",
        True,
    )


def test_accepting_clears_the_text_and_keeps_only_the_decision(store):
    code = consent(store)
    ingest(store, form(response(code)))

    client_updates.accept(
        store, only_update(store).id, values=None, contact_id=None, left_on=None, now=LATER
    )

    decided = only_update(store)
    assert decided.status is UpdateStatus.ACCEPTED
    assert decided.decided_at == LATER
    assert decided.received_at == NOW
    assert (decided.name, decided.title, decided.email, decided.note) == ("", "", "", "")
    assert (decided.champion, decided.submitted_by, decided.submitted_at) == (None, "", None)


def test_an_add_with_a_blank_name_is_refused_and_changes_nothing(store):
    code = consent(store)
    ingest(store, form(response(code, name="")))
    update = only_update(store)

    with pytest.raises(ValueError, match="contact's name"):
        client_updates.accept(
            store, update.id, values=None, contact_id=None, left_on=None, now=LATER
        )
    assert contacts_of(store) == []
    assert only_update(store).status is UpdateStatus.PENDING


def test_invalid_corrected_values_change_nothing(store):
    code = consent(store)
    ingest(store, form(response(code)))

    with pytest.raises(ValueError, match="email address"):
        client_updates.accept(
            store,
            only_update(store).id,
            values=ContactInput(name="Priya", email="not an email"),
            contact_id=None,
            left_on=None,
            now=LATER,
        )
    assert contacts_of(store) == []
    assert only_update(store).status is UpdateStatus.PENDING


def test_accept_someone_left_uses_the_contact_the_update_means(store):
    code = consent(store)
    priya = contacts.add_contact(
        store, "acc-1", ContactInput(name="Priya Shah"), source=ContactSource.YOU, now=NOW
    )
    ingest(store, form(response(code, kind="Someone left", when="11/8/2026 9:00:00")))

    kind = client_updates.accept(
        store, only_update(store).id, values=None, contact_id=None, left_on=None, now=LATER
    )

    assert kind is UpdateKind.LEFT
    left = contacts_of(store)[0]
    assert (left.id, left.status) == (priya.id, contacts.ContactStatus.LEFT)
    assert left.left_on == date(2026, 8, 11)  # the form's timestamp, read day first
    assert left.source is ContactSource.CLIENT


def test_accept_someone_left_with_a_date_the_csm_chose(store):
    code = consent(store)
    contacts.add_contact(
        store, "acc-1", ContactInput(name="Priya Shah"), source=ContactSource.YOU, now=NOW
    )
    ingest(store, form(response(code, kind="Someone left")))

    client_updates.accept(
        store,
        only_update(store).id,
        values=None,
        contact_id=None,
        left_on=date(2026, 11, 2),
        now=LATER,
    )

    assert contacts_of(store)[0].left_on == date(2026, 11, 2)


def test_accept_someone_left_with_a_chosen_contact(store):
    code = consent(store)
    sam = contacts.add_contact(
        store, "acc-1", ContactInput(name="Sam Okafor"), source=ContactSource.YOU, now=NOW
    )
    contacts.add_contact(
        store, "acc-1", ContactInput(name="Priya Shah"), source=ContactSource.YOU, now=NOW
    )
    ingest(store, form(response(code, kind="Someone left", name="Samuel")))

    client_updates.accept(
        store,
        only_update(store).id,
        values=None,
        contact_id=sam.id,
        left_on=date(2026, 11, 2),
        now=LATER,
    )

    by_name = {c.name: c for c in contacts_of(store)}
    assert by_name["Sam Okafor"].status is contacts.ContactStatus.LEFT
    assert by_name["Priya Shah"].status is contacts.ContactStatus.ACTIVE


def test_accept_someone_left_with_no_contact_is_refused_and_changes_nothing(store):
    code = consent(store)
    contacts.add_contact(
        store, "acc-1", ContactInput(name="Priya Shah"), source=ContactSource.YOU, now=NOW
    )
    ingest(store, form(response(code, kind="Someone left", name="Nobody Known")))
    update = only_update(store)

    with pytest.raises(ValueError, match="Choose the contact"):
        client_updates.accept(
            store, update.id, values=None, contact_id=None, left_on=None, now=LATER
        )
    assert contacts_of(store)[0].status is contacts.ContactStatus.ACTIVE
    assert only_update(store).status is UpdateStatus.PENDING


def test_a_champion_who_left_through_an_accepted_update_records_one(store):
    code = consent(store)
    contacts.add_contact(
        store,
        "acc-1",
        ContactInput(name="Priya Shah", champion=True),
        source=ContactSource.YOU,
        now=NOW,
    )
    ingest(store, form(response(code, kind="Someone left")))

    client_updates.accept(
        store, only_update(store).id, values=None, contact_id=None, left_on=None, now=LATER
    )

    assert contact_readings(store) == [0.0, 1.0]


def test_accept_a_change_applies_the_new_details_and_keeps_blank_ones(store):
    code = consent(store)
    contacts.add_contact(
        store,
        "acc-1",
        ContactInput(name="Priya Shah", title="VP", email="priya@acme.com", champion=True),
        source=ContactSource.YOU,
        now=NOW,
    )
    ingest(
        store,
        form(
            response(
                code,
                kind="Change a contact's details",
                title="",
                email="priya.shah@acme.com",
                champion="",
            )
        ),
    )

    kind = client_updates.accept(
        store, only_update(store).id, values=None, contact_id=None, left_on=None, now=LATER
    )

    assert kind is UpdateKind.CHANGE
    changed = contacts_of(store)[0]
    assert (changed.title, changed.email, changed.champion) == (
        "VP",
        "priya.shah@acme.com",
        True,
    )
    assert changed.source is ContactSource.CLIENT


def test_accept_other_applies_nothing(store):
    code = consent(store)
    ingest(store, form(response(code, kind="Something else", name="Anything")))

    kind = client_updates.accept(
        store, only_update(store).id, values=None, contact_id=None, left_on=None, now=LATER
    )

    assert kind is UpdateKind.OTHER
    assert contacts_of(store) == []
    assert only_update(store).status is UpdateStatus.ACCEPTED


def test_reject_clears_the_text_and_a_decided_update_cannot_be_decided_again(store):
    code = consent(store)
    ingest(store, form(response(code), response(code, name="Sam Okafor")))
    first, second = updates(store)

    client_updates.reject(store, first.id, now=LATER)
    client_updates.accept(store, second.id, values=None, contact_id=None, left_on=None, now=LATER)

    rejected, accepted = updates(store)
    assert rejected.status is UpdateStatus.REJECTED
    assert (rejected.name, rejected.decided_at) == ("", LATER)
    assert accepted.status is UpdateStatus.ACCEPTED
    with pytest.raises(LookupError):
        client_updates.accept(
            store, first.id, values=None, contact_id=None, left_on=None, now=LATER
        )
    with pytest.raises(LookupError):
        client_updates.reject(store, second.id, now=LATER)
    assert contacts_of(store)[0].name == "Sam Okafor"


def test_an_unknown_update_is_a_lookup_error(store):
    with pytest.raises(LookupError):
        client_updates.accept(
            store, "0" * 16, values=None, contact_id=None, left_on=None, now=LATER
        )
    with pytest.raises(LookupError):
        client_updates.reject(store, "0" * 16, now=LATER)


# Reviewing -----------------------------------------------------------------------------------


def test_pending_lists_only_accounts_in_the_book_oldest_first(store):
    code_one = consent(store, "acc-1")
    code_two = consent(store, "acc-2", name="Sam")
    ingest(store, form(response(code_two, name="Later row")), now=LATER)
    ingest(store, form(response(code_one, name="Earlier row")), now=NOW)

    both = client_updates.pending(store, {"acc-1": acct("acc-1"), "acc-2": acct("acc-2")})
    only_one = client_updates.pending(store, {"acc-1": acct("acc-1")})

    assert [review.update.name for review in both] == ["Earlier row", "Later row"]
    assert [review.update.name for review in only_one] == ["Earlier row"]
    assert client_updates.pending_count(store, {"acc-1"}) == 1
    assert client_updates.pending_count(store, {"acc-1", "acc-2"}) == 2


def test_a_review_shows_the_contact_it_means_and_the_active_candidates(store):
    code = consent(store)
    sam = contacts.add_contact(
        store, "acc-1", ContactInput(name="Sam Okafor"), source=ContactSource.YOU, now=NOW
    )
    dana = contacts.add_contact(
        store, "acc-1", ContactInput(name="Dana Whitfield"), source=ContactSource.YOU, now=NOW
    )
    ingest(store, form(response(code, kind="Someone left", name="Sam Okafor")))

    review = client_updates.pending(store, {"acc-1": acct("acc-1")})[0]

    assert review.target == sam
    assert {c.id for c in review.candidates} == {sam.id, dana.id}


# Forgetting and moving -------------------------------------------------------------------------


def test_forget_drops_the_consent_and_updates_of_the_accounts_given(store):
    code_one = consent(store, "acc-1")
    code_two = consent(store, "acc-2", name="Sam")
    ingest(store, form(response(code_one), response(code_two, name="Sam Okafor")))

    client_updates.forget(store, lambda account: account == "acc-1")

    data = client_updates.load(store)
    assert set(data.consents) == {"acc-2"}
    assert [u.account_id for u in data.updates] == ["acc-2"]


def test_move_account_moves_the_pending_updates_and_the_consent(store):
    code = consent(store, "acc-1")
    ingest(store, form(response(code)))

    client_updates.move_account(store, "acc-1", "acc-9")

    data = client_updates.load(store)
    assert set(data.consents) == {"acc-9"}
    assert data.consents["acc-9"].code == code
    assert data.consents["acc-9"].account_id == "acc-9"
    assert [(u.account_id, u.status) for u in data.updates] == [("acc-9", UpdateStatus.PENDING)]


def test_move_account_drops_the_consent_when_the_target_has_one(store):
    keep = consent(store, "acc-9")
    consent(store, "acc-3", name="Sam")

    client_updates.move_account(store, "acc-3", "acc-9")

    data = client_updates.load(store)
    assert set(data.consents) == {"acc-9"}
    assert data.consents["acc-9"].code == keep


def test_move_account_does_nothing_for_an_account_with_no_updates_or_consent(store):
    consent(store, "acc-1")

    client_updates.move_account(store, "acc-none", "acc-1")

    assert set(client_updates.load(store).consents) == {"acc-1"}
