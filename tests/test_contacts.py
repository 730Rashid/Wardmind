"""Contacts (core/contacts.py): validation, add, update, mark left and active, remove, the limits,
the order they are listed in, the champion rule that records CHAMPION_LEFT readings, moving and
forgetting them, finding the contact a client update means, and the demo contacts.

The store is in a temp folder (see LocalStore's root). No network.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest

from wardmind.core import contacts
from wardmind.core.contacts import (
    MAX_CONTACTS_PER_ACCOUNT,
    MAX_EMAIL,
    MAX_NAME,
    MAX_TITLE,
    Contact,
    ContactInput,
    ContactSource,
    ContactStatus,
)
from wardmind.core.models import Signal, SignalKind
from wardmind.core.store import CONTACTS_FILE, ConnectionKind, ImportSnapshot, LocalStore

NOW = datetime(2026, 11, 8, 9, 0)
LATER = datetime(2026, 11, 9, 9, 0)
ACC = "acc-1"
LEFT_ON = date(2026, 11, 1)


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def values(name="Dana Whitfield", title="", email="", champion=False) -> ContactInput:
    return ContactInput(name=name, title=title, email=email, champion=champion)


def add(
    store: LocalStore,
    *,
    name="Dana Whitfield",
    email="",
    champion=False,
    account=ACC,
    source=ContactSource.YOU,
    now=NOW,
) -> Contact:
    return contacts.add_contact(
        store, account, values(name=name, email=email, champion=champion), source=source, now=now
    )


def champion_readings(store: LocalStore, account=ACC) -> list[float]:
    """The CHAMPION_LEFT readings the champion rule recorded, oldest first."""
    return [
        signal.value
        for signal in store.manual().signals
        if signal.kind is SignalKind.CHAMPION_LEFT
        and signal.account_id == account
        and signal.source == contacts.SIGNAL_SOURCE
    ]


def contact(name: str, email: str = "", *, champion: bool = False, cid: str = "c-00000001"):
    return Contact(
        id=cid,
        name=name,
        email=email,
        champion=champion,
        updated_at=NOW,
    )


# validate --------------------------------------------------------------------------------------


def test_validate_strips_the_values_and_keeps_good_ones():
    clean = contacts.validate(
        ContactInput(name="  Dana  ", title=" VP ", email=" dana@acme.com ", champion=True)
    )
    assert clean == ContactInput(name="Dana", title="VP", email="dana@acme.com", champion=True)


def test_a_blank_title_and_email_are_fine():
    clean = contacts.validate(values(name="Dana", title="  ", email=""))
    assert (clean.title, clean.email) == ("", "")


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (values(name="   "), "Type the contact's name."),
        (values(name="x" * (MAX_NAME + 1)), "80 characters"),
        (values(name="Dana\nWhitfield"), "line break"),
        (values(name="Dana\x07"), "control characters"),
        (values(title="x" * (MAX_TITLE + 1)), "80 characters"),
        (values(title="VP\rOps"), "line break"),
        (values(title="VP\x1b"), "control characters"),
        (values(email="dana-at-acme.com"), "doesn't look right"),
        (values(email="dana@acmecom"), "doesn't look right"),
        (values(email="a@b@acme.com"), "doesn't look right"),
        (values(email="dana @acme.com"), "doesn't look right"),
        (values(email="dana@acme.com\x01x"), "doesn't look right"),
        (values(email="d" * (MAX_EMAIL - 4) + "@a.co"), "254 characters"),
    ],
)
def test_validate_names_the_field_that_is_wrong(given, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        contacts.validate(given)


def test_a_name_at_the_limit_is_accepted():
    assert contacts.validate(values(name="x" * MAX_NAME)).name == "x" * MAX_NAME


# add, update, mark, remove ----------------------------------------------------------------------


def test_add_stores_a_contact_with_an_id_and_lists_it(store):
    contact_added = add(store, email="dana@acme.com")

    assert re.fullmatch(r"c-[0-9a-f]{8}", contact_added.id)
    assert contact_added.status is ContactStatus.ACTIVE
    assert contact_added.source is ContactSource.YOU
    assert contact_added.updated_at == NOW
    assert contacts.contacts_for(store, ACC) == [contact_added]
    assert (store.root / CONTACTS_FILE).exists()


def test_contact_ids_are_unique_within_the_account(store, monkeypatch):
    tokens = iter(["00000001", "00000001", "00000002"])
    monkeypatch.setattr(contacts.secrets, "token_hex", lambda size: next(tokens))

    first = add(store, name="First")
    second = add(store, name="Second")

    assert (first.id, second.id) == ("c-00000001", "c-00000002")


def test_update_replaces_name_title_email_and_champion_and_keeps_the_status(store):
    added = add(store, name="Dana", email="dana@acme.com")
    contacts.mark_left(store, ACC, added.id, left_on=LEFT_ON, source=ContactSource.YOU, now=NOW)

    updated = contacts.update_contact(
        store,
        ACC,
        added.id,
        values(name="Dana W.", title="CFO", email="dana.w@acme.com", champion=True),
        source=ContactSource.CLIENT,
        now=LATER,
    )

    assert (updated.name, updated.title, updated.email) == ("Dana W.", "CFO", "dana.w@acme.com")
    assert updated.champion is True
    assert updated.status is ContactStatus.LEFT
    assert updated.left_on == LEFT_ON
    assert updated.source is ContactSource.CLIENT
    assert updated.updated_at == LATER


def test_update_of_an_unknown_contact_is_a_lookup_error(store):
    add(store)
    with pytest.raises(LookupError):
        contacts.update_contact(
            store, ACC, "c-ffffffff", values(), source=ContactSource.YOU, now=NOW
        )


def test_mark_left_then_mark_active_undoes_it(store):
    added = add(store, name="Dana")

    left = contacts.mark_left(
        store, ACC, added.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER
    )
    assert left.status is ContactStatus.LEFT
    assert left.left_on == LEFT_ON

    back = contacts.mark_active(store, ACC, added.id, now=LATER)
    assert back.status is ContactStatus.ACTIVE
    assert back.left_on is None


def test_mark_left_and_active_of_an_unknown_contact_are_lookup_errors(store):
    with pytest.raises(LookupError):
        contacts.mark_left(
            store, ACC, "c-ffffffff", left_on=LEFT_ON, source=ContactSource.YOU, now=NOW
        )
    with pytest.raises(LookupError):
        contacts.mark_active(store, ACC, "c-ffffffff", now=NOW)


def test_remove_deletes_the_contact_and_keeps_the_readings(store):
    champion = add(store, name="Dana", champion=True)
    other = add(store, name="Sam")

    assert contacts.remove_contact(store, ACC, champion.id) is True
    assert contacts.remove_contact(store, ACC, champion.id) is False
    assert contacts.contacts_for(store, ACC) == [other]
    assert champion_readings(store) == [0.0]
    assert contacts.remove_contact(store, "acc-other", champion.id) is False


def test_an_account_holds_at_most_fifty_contacts(store):
    for index in range(MAX_CONTACTS_PER_ACCOUNT):
        add(store, name=f"Person {index}", email=f"person{index}@acme.com")

    with pytest.raises(ValueError, match="50 contacts"):
        add(store, name="One more", email="more@acme.com")
    assert len(contacts.contacts_for(store, ACC)) == MAX_CONTACTS_PER_ACCOUNT


def test_an_active_email_belongs_to_one_contact_only_casefolded(store):
    add(store, name="Dana", email="Dana@Acme.com")
    sam = add(store, name="Sam", email="sam@acme.com")

    with pytest.raises(ValueError, match="already has this email"):
        add(store, name="Dana Two", email="dana@acme.COM")
    with pytest.raises(ValueError, match="already has this email"):
        contacts.update_contact(
            store,
            ACC,
            sam.id,
            values(name="Sam", email="DANA@acme.com"),
            source=ContactSource.YOU,
            now=NOW,
        )
    assert len(contacts.contacts_for(store, ACC)) == 2


def test_a_left_contact_does_not_block_its_email_and_blank_emails_never_clash(store):
    gone = add(store, name="Dana", email="dana@acme.com")
    contacts.mark_left(store, ACC, gone.id, left_on=LEFT_ON, source=ContactSource.YOU, now=NOW)

    add(store, name="Dana New", email="dana@acme.com")
    add(store, name="No email one")
    add(store, name="No email two")

    assert len(contacts.contacts_for(store, ACC)) == 4


def test_mark_active_refuses_an_email_an_active_contact_now_has(store):
    gone = add(store, name="Dana", email="dana@acme.com")
    contacts.mark_left(store, ACC, gone.id, left_on=LEFT_ON, source=ContactSource.YOU, now=NOW)
    add(store, name="Dana New", email="dana@acme.com")

    with pytest.raises(ValueError, match="already has this email"):
        contacts.mark_active(store, ACC, gone.id, now=LATER)
    assert contacts.contacts_for(store, ACC)[-1].status is ContactStatus.LEFT


def test_contacts_are_listed_active_first_then_champions_then_by_name(store):
    add(store, name="zed")
    add(store, name="Amy")
    add(store, name="Chief", champion=True)
    gone = add(store, name="Aaron")
    contacts.mark_left(store, ACC, gone.id, left_on=LEFT_ON, source=ContactSource.YOU, now=NOW)

    assert [c.name for c in contacts.contacts_for(store, ACC)] == [
        "Chief",
        "Amy",
        "zed",
        "Aaron",
    ]


def test_a_missing_or_unreadable_file_reads_as_no_contacts(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / CONTACTS_FILE).write_text("{ not json", encoding="utf-8")

    assert contacts.contacts_for(store, ACC) == []
    assert add(store, name="Dana").name == "Dana"  # and a write replaces the bad file
    assert [c.name for c in contacts.contacts_for(store, ACC)] == ["Dana"]


# The champion rule -------------------------------------------------------------------------------


def test_the_last_champion_leaving_records_one(store):
    dana = add(store, name="Dana", champion=True)

    contacts.mark_left(store, ACC, dana.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER)

    assert champion_readings(store) == [0.0, 1.0]
    reading = store.manual().signals[-1]
    assert reading.source == "contacts"
    assert reading.observed_at == LATER
    assert reading.value == 1.0


def test_with_another_active_champion_still_there_nothing_is_recorded(store):
    first = add(store, name="Dana", champion=True)
    add(store, name="Sam", champion=True)

    contacts.mark_left(store, ACC, first.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER)

    assert champion_readings(store) == [0.0]


def test_a_new_champion_records_zero(store):
    add(store, name="Dana")  # not a champion: nothing recorded
    assert champion_readings(store) == []

    add(store, name="Sam", champion=True)

    assert champion_readings(store) == [0.0]


def test_a_new_champion_after_the_last_one_left_records_zero_again(store):
    dana = add(store, name="Dana", champion=True)
    contacts.mark_left(store, ACC, dana.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER)

    add(store, name="Sam", champion=True, now=LATER)

    assert champion_readings(store) == [0.0, 1.0, 0.0]


def test_nothing_is_recorded_when_the_latest_reading_already_says_so(store):
    dana = add(store, name="Dana", champion=True)  # records 0.0 at NOW
    manual = store.manual()
    later_reading = Signal(
        account_id=ACC,
        kind=SignalKind.CHAMPION_LEFT,
        value=1.0,
        observed_at=LATER,
        source="manual",
    )
    store.save_manual(manual.model_copy(update={"signals": [*manual.signals, later_reading]}))

    contacts.mark_left(store, ACC, dana.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER)

    assert champion_readings(store) == [0.0]


def test_a_connection_snapshot_reading_counts_as_the_latest(store):
    dana = add(store, name="Dana", champion=True)  # records 0.0 at NOW
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    store.save_snapshot(
        ImportSnapshot(
            connection_id=sheet.id,
            signals=[
                Signal(
                    account_id=ACC,
                    kind=SignalKind.CHAMPION_LEFT,
                    value=1.0,
                    observed_at=LATER,
                    source="spreadsheet",
                )
            ],
        )
    )

    # The newest reading, the spreadsheet's 1.0, already says the champion left.
    contacts.mark_left(store, ACC, dana.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER)

    assert champion_readings(store) == [0.0]


def test_mark_active_undoing_the_last_loss_records_zero(store):
    dana = add(store, name="Dana", champion=True)
    contacts.mark_left(store, ACC, dana.id, left_on=LEFT_ON, source=ContactSource.YOU, now=LATER)

    contacts.mark_active(store, ACC, dana.id, now=LATER)

    assert champion_readings(store) == [0.0, 1.0, 0.0]


def test_clearing_the_champion_flag_on_the_last_champion_records_one(store):
    dana = add(store, name="Dana", champion=True)

    contacts.update_contact(
        store,
        ACC,
        dana.id,
        values(name="Dana", champion=False),
        source=ContactSource.YOU,
        now=LATER,
    )

    assert champion_readings(store) == [0.0, 1.0]


def test_removing_a_champion_records_nothing(store):
    dana = add(store, name="Dana", champion=True)

    contacts.remove_contact(store, ACC, dana.id)

    assert champion_readings(store) == [0.0]


# Moving, finding, forgetting ---------------------------------------------------------------------


def test_move_contacts_moves_them_and_drops_one_whose_email_is_already_there(store):
    add(store, name="Dana", email="dana@acme.com", account="acc-a")
    add(store, name="Sam", email="sam@acme.com", account="acc-a")
    add(store, name="Dana Again", email="DANA@acme.com", account="acc-b")

    moved = contacts.move_contacts(store, "acc-a", "acc-b")

    assert moved == 1
    assert [c.name for c in contacts.contacts_for(store, "acc-b")] == ["Dana Again", "Sam"]
    assert contacts.contacts_for(store, "acc-a") == []


def test_move_contacts_does_nothing_for_no_contacts_or_the_same_account(store):
    add(store, name="Dana", account="acc-a")

    assert contacts.move_contacts(store, "acc-none", "acc-a") == 0
    assert contacts.move_contacts(store, "acc-a", "acc-a") == 0
    assert [c.name for c in contacts.contacts_for(store, "acc-a")] == ["Dana"]


def test_find_contact_uses_the_email_first(store):
    dana = contact("Dana Whitfield", "dana@acme.com", cid="c-00000001")
    sam = contact("Sam Okafor", "sam@acme.com", cid="c-00000002")

    found = contacts.find_contact([dana, sam], email="SAM@acme.com", name="Dana Whitfield")

    assert found == sam


def test_find_contact_falls_back_to_the_name_with_spaces_collapsed(store):
    dana = contact("Dana Whitfield", "dana@acme.com")

    assert contacts.find_contact([dana], email="", name="dana   whitfield") == dana
    assert contacts.find_contact([dana], email="other@acme.com", name="DANA WHITFIELD") == dana


def test_find_contact_gives_none_when_nothing_or_more_than_one_matches():
    first = contact("Sam Okafor", cid="c-00000001")
    second = contact("sam okafor", cid="c-00000002")

    assert contacts.find_contact([first, second], email="", name="Sam Okafor") is None
    assert contacts.find_contact([first], email="", name="Dana") is None
    assert contacts.find_contact([first], email="", name="  ") is None


def test_forget_drops_the_accounts_given_and_no_others(store):
    add(store, name="Dana", account="acc-a")
    add(store, name="Sam", account="acc-b")

    assert contacts.forget(store, lambda account: account == "acc-a") == 1
    assert contacts.contacts_for(store, "acc-a") == []
    assert [c.name for c in contacts.contacts_for(store, "acc-b")] == ["Sam"]
    assert contacts.forget(store, lambda account: False) == 0


# Demo contacts -----------------------------------------------------------------------------------


def test_demo_contacts_are_the_same_every_time_with_one_champion():
    first = contacts.demo_contacts("acc-001", "Larkspur Freight")

    assert first == contacts.demo_contacts("acc-001", "Larkspur Freight")
    assert 2 <= len(first) <= 3
    assert sum(c.champion for c in first) == 1
    assert all(c.status is ContactStatus.ACTIVE for c in first)
    assert all(c.title and c.name for c in first)
    assert all(re.fullmatch(r"c-[0-9a-f]{8}", c.id) for c in first)
    assert len({c.id for c in first}) == len(first)


def test_demo_contacts_use_only_example_dot_com_emails():
    for index in range(30):
        found = contacts.demo_contacts(f"acc-{index:03d}", "Demo Co")
        assert found
        assert all(c.email.endswith("@example.com") for c in found)
        assert all(c.email == c.email.casefold() for c in found)


def test_demo_contacts_differ_between_accounts():
    sizes = {len(contacts.demo_contacts(f"acc-{index:03d}", "Demo")) for index in range(30)}
    assert sizes <= {2, 3}
    assert contacts.demo_contacts("acc-001", "A") != contacts.demo_contacts("acc-002", "A")
