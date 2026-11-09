"""wardmind.core.editing: every write a CSM makes by hand, checked against a real CombinedConnector.

The members are small fakes, as in test_combined.py. The store is a temp folder.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from wardmind.core import client_updates, contacts, editing
from wardmind.core.connectors.combined import CombinedConnector
from wardmind.core.connectors.local import ManualConnector
from wardmind.core.contacts import ContactInput, ContactSource
from wardmind.core.editing import (
    EditError,
    add_reading,
    create_account,
    delete_account,
    dismiss_duplicate,
    edit_account,
    link_accounts,
    parse_edit_value,
    revert_field,
    set_hidden,
    unlink,
)
from wardmind.core.models import Account, Milestones, Signal, SignalKind
from wardmind.core.scoring import latest_signals
from wardmind.core.store import (
    ConnectionConfig,
    ConnectionKind,
    FieldEdit,
    LinkData,
    LinkGroup,
    LocalStore,
    ManualData,
)

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 9, 30)
HUBSPOT = ConnectionConfig(
    id="hubspot", kind=ConnectionKind.HUBSPOT, name="HubSpot", created_at=NOW
)
SHEET = ConnectionConfig(
    id="sheet-1", kind=ConnectionKind.SPREADSHEET, name="Q4 renewals sheet", created_at=NOW
)
MANUAL = ConnectionConfig(
    id="manual", kind=ConnectionKind.MANUAL, name="Added by hand", created_at=NOW
)
NEW_ACCOUNT = {"name": "Larkspur Freight", "arr": "$184,000", "renewal_date": "2026-12-01"}


def acc(
    acc_id: str,
    name: str = "Acme",
    *,
    arr: float = 120000.0,
    tier: str = "Enterprise",
    owner: str = "Dana Whitfield",
    domain: str | None = "acme.com",
    start: date = date(2024, 1, 1),
    renewal: date = date(2027, 1, 1),
    milestones: tuple[int, int, int] = (4, 6, 1),  # done, total, overdue
) -> Account:
    done, total, overdue = milestones
    return Account(
        id=acc_id,
        name=name,
        arr=arr,
        tier=tier,
        owner=owner,
        start_date=start,
        renewal_date=renewal,
        milestones=Milestones(done=done, total=total, overdue=overdue),
        domain=domain,
    )


def edit(value, base) -> FieldEdit:
    return FieldEdit(value=value, base_value=base, edited_at=NOW)


def reading(acc_id: str, kind: SignalKind, value: float, source: str = "manual") -> Signal:
    return Signal(account_id=acc_id, kind=kind, value=value, observed_at=NOW, source=source)


class FakeConnector:
    """A member connection whose accounts and signals the test changes as it likes."""

    def __init__(self, name: str, accounts=None, signals=None) -> None:
        self.name = name
        self.accounts = list(accounts or [])
        self.signals = list(signals or [])

    def list_accounts(self) -> list[Account]:
        return list(self.accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        if account_id is None:
            return list(self.signals)
        return [s for s in self.signals if s.account_id == account_id]

    def refresh(self) -> None:
        pass


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def book(store: LocalStore, *members) -> CombinedConnector:
    return CombinedConnector(list(members), store, today=TODAY)


def link(store: LocalStore, primary: str, *members: str) -> None:
    store.save_links(LinkData(groups=[LinkGroup(primary=primary, members=list(members))]))


def synced_book(store: LocalStore, *, arr: float = 120000.0):
    """One HubSpot account, hs-1, and the book built on it."""
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", arr=arr)])
    return hubspot, book(store, (HUBSPOT, hubspot))


def linked_book(store: LocalStore):
    """hs-1 (HubSpot) and sp1-acme (sheet) are the same customer; sp1-globex is separate."""
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", arr=120000.0, tier="Enterprise")])
    sheet = FakeConnector(
        "spreadsheet",
        [
            acc("sp1-acme", "Acme sheet", arr=130000.0, tier="Growth"),
            acc("sp1-globex", "Globex", arr=40000.0, domain="globex.example"),
        ],
    )
    return hubspot, sheet, book(store, (HUBSPOT, hubspot), (SHEET, sheet))


# Parsing -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("name", "  Acme  ", "Acme"),
        ("domain", "", None),
        ("domain", "   ", None),
        ("domain", "https://www.Acme.com/about", "acme.com"),
        ("arr", "$120,000", 120000.0),
        ("arr", "120k", 120000.0),
        ("tier", "  Growth ", "Growth"),
        ("tier", " ", "Unknown"),
        ("owner", "", "Unassigned"),
        ("start_date", "2024-03-01", date(2024, 3, 1)),
        ("renewal_date", "2027-01-01", date(2027, 1, 1)),
        ("milestones_total", " 6 ", 6),
        ("milestones_done", "0", 0),
    ],
)
def test_valid_values_are_parsed(field, raw, expected):
    assert parse_edit_value(field, raw) == expected


@pytest.mark.parametrize(
    ("field", "raw", "message"),
    [
        ("name", "   ", "Name can't be empty."),
        ("domain", "not a site", 'Website "not a site" doesn\'t look like a website.'),
        ("arr", "", "ARR can't be empty."),
        ("arr", "lots", 'ARR "lots" isn\'t an amount.'),
        ("arr", "-5", 'ARR "-5" can\'t be negative.'),
        ("start_date", "1 December 2026", "Customer since isn't a date."),
        ("renewal_date", "2026-13-01", "Renewal date isn't a date."),
        ("milestones_total", "2.5", "Onboarding milestones must be a whole number, 0 or more."),
        ("milestones_overdue", "-1", "Milestones overdue must be a whole number, 0 or more."),
        ("milestones_done", "", "Milestones done must be a whole number, 0 or more."),
    ],
)
def test_bad_values_give_a_plain_message_for_their_field(field, raw, message):
    with pytest.raises(EditError) as caught:
        parse_edit_value(field, raw)
    assert str(caught.value) == message
    assert caught.value.field == field


def test_a_long_value_is_quoted_and_cut_to_forty_characters():
    with pytest.raises(EditError) as caught:
        parse_edit_value("domain", "x" * 60)
    assert str(caught.value) == f'Website "{"x" * 39}…" doesn\'t look like a website.'


def test_a_field_that_cannot_be_edited_is_refused():
    with pytest.raises(EditError) as caught:
        parse_edit_value("secret", "x")
    assert str(caught.value) == "That field can't be edited."
    assert caught.value.field is None


# Creating and deleting hand-added accounts ----------------------------------------------


def test_create_adds_a_hand_account_with_the_defaults(store):
    account = create_account(store, dict(NEW_ACCOUNT), today=TODAY)

    assert account.id.startswith("m-") and len(account.id) == 10
    assert account.name == "Larkspur Freight"
    assert account.arr == 184000.0
    assert account.renewal_date == date(2026, 12, 1)
    assert account.start_date is None  # not known, so never today
    assert account.tier == "Unknown"
    assert account.owner == "Unassigned"
    assert account.domain is None
    assert account.milestones == Milestones(done=0, total=0, overdue=0)
    assert store.manual().accounts == [account]
    [connection] = store.connections()
    assert (connection.id, connection.kind, connection.name, connection.enabled) == (
        "manual",
        ConnectionKind.MANUAL,
        "Added by hand",
        True,
    )


def test_create_says_which_required_fields_are_missing_and_writes_nothing(store):
    with pytest.raises(EditError) as caught:
        create_account(store, {"name": " ", "arr": "", "renewal_date": ""}, today=TODAY)
    assert caught.value.errors == {
        "name": "Name is required.",
        "arr": "ARR is required.",
        "renewal_date": "Renewal date is required.",
    }
    assert caught.value.field == "name"
    assert store.manual().accounts == []
    assert store.connections() == []


def test_create_reports_every_bad_field_at_once(store):
    with pytest.raises(EditError) as caught:
        create_account(
            store,
            {**NEW_ACCOUNT, "arr": "lots", "domain": "nope", "start_date": "soon"},
            today=TODAY,
        )
    assert set(caught.value.errors) == {"arr", "domain", "start_date"}
    assert store.manual().accounts == []


def test_blank_optional_fields_on_create_take_their_defaults(store):
    account = create_account(
        store,
        {
            **NEW_ACCOUNT,
            "start_date": "",
            "tier": " ",
            "owner": "",
            "domain": " ",
            "milestones_done": "",
        },
        today=TODAY,
    )
    assert account.start_date is None
    assert (account.tier, account.owner, account.domain) == ("Unknown", "Unassigned", None)
    assert account.milestones.done == 0


def test_create_checks_the_milestones_together(store):
    with pytest.raises(EditError) as caught:
        create_account(
            store,
            {
                **NEW_ACCOUNT,
                "milestones_total": "2",
                "milestones_done": "2",
                "milestones_overdue": "1",
            },
            today=TODAY,
        )
    assert caught.value.errors == {
        "milestones_done": "Milestones done and overdue can't add up to more than the total."
    }


def test_a_switched_off_hand_connection_is_turned_back_on_by_create(store):
    config = store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    store.update_connection(config.model_copy(update={"enabled": False}))
    create_account(store, dict(NEW_ACCOUNT), today=TODAY)
    assert store.get_connection("manual").enabled is True


def test_hand_account_ids_are_unique(store, monkeypatch):
    store.save_manual(ManualData(accounts=[acc("m-00000001", "Taken")]))
    tokens = iter(["00000001", "00000002"])  # the first candidate is already taken
    monkeypatch.setattr(editing.secrets, "token_hex", lambda nbytes: next(tokens))
    assert create_account(store, dict(NEW_ACCOUNT), today=TODAY).id == "m-00000002"


def test_a_created_account_shows_in_the_book(store):
    combined = book(store, (MANUAL, ManualConnector(store)))
    created = create_account(store, dict(NEW_ACCOUNT), today=TODAY)
    assert [a.id for a in combined.list_accounts()] == [created.id]


def test_delete_removes_the_account_and_its_readings_only(store):
    created = create_account(store, dict(NEW_ACCOUNT), today=TODAY)
    other = create_account(store, {**NEW_ACCOUNT, "name": "Other"}, today=TODAY)
    store.save_manual(
        store.manual().model_copy(
            update={
                "signals": [
                    reading(created.id, SignalKind.NPS_LATEST, 3.0),
                    reading(other.id, SignalKind.NPS_LATEST, 9.0),
                ]
            }
        )
    )

    delete_account(store, created.id)

    assert store.manual().accounts == [other]
    assert [s.account_id for s in store.manual().signals] == [other.id]


def test_delete_also_drops_edits_notices_hidden_ids_and_link_memberships(store):
    store.save_manual(ManualData(accounts=[acc("m-00000001", "Hand added")]))
    store.set_edit("m-00000001", "arr", edit(150000.0, 140000.0))
    store.add_notice("m-00000001", "arr", "Something changed.")
    store.set_hidden("m-00000001", True)
    store.save_links(
        LinkData(
            groups=[
                LinkGroup(primary="hs-1", members=["hs-1", "m-00000001"]),  # left with one: goes
                LinkGroup(  # keeps two members; its primary was the deleted account
                    primary="m-00000001", members=["m-00000001", "hs-2", "sp1-x"]
                ),
            ],
            dismissed=[("hs-1", "m-00000001"), ("hs-1", "hs-2")],
        )
    )

    delete_account(store, "m-00000001")

    assert store.manual().accounts == []
    assert store.edits() == {}
    assert store.notices("m-00000001", include_dismissed=True) == []
    assert store.hidden() == []
    assert store.links() == LinkData(
        groups=[LinkGroup(primary="hs-2", members=["hs-2", "sp1-x"])],
        dismissed=[("hs-1", "hs-2")],
    )


def test_only_hand_added_accounts_can_be_deleted(store):
    with pytest.raises(EditError) as caught:
        delete_account(store, "hs-1")
    assert str(caught.value) == "Only accounts you added by hand can be deleted. Hide it instead."


# Editing ---------------------------------------------------------------------------------------


def test_a_synced_account_is_edited_as_an_edit_over_its_source(store):
    _, combined = synced_book(store)

    changed = edit_account(store, combined, "hs-1", {"arr": "$135,000"}, now=NOW)

    assert changed == ["arr"]
    assert store.edits() == {
        "hs-1": {"arr": FieldEdit(value=135000.0, base_value=120000.0, edited_at=NOW)}
    }
    assert combined.list_accounts()[0].arr == 135000.0


def test_a_date_edit_is_kept_as_iso_text_and_read_back_as_a_date(store):
    _, combined = synced_book(store)

    edit_account(store, combined, "hs-1", {"renewal_date": "2027-03-01"}, now=NOW)

    assert store.edits()["hs-1"]["renewal_date"] == FieldEdit(
        value=date(2027, 3, 1), base_value=date(2027, 1, 1), edited_at=NOW
    )
    assert combined.list_accounts()[0].renewal_date == date(2027, 3, 1)


def test_typing_the_source_value_back_clears_the_edit(store):
    _, combined = synced_book(store)
    edit_account(store, combined, "hs-1", {"arr": "$135,000"}, now=NOW)

    changed = edit_account(store, combined, "hs-1", {"arr": "$120,000"}, now=NOW)

    assert changed == ["arr"]
    assert store.edits() == {}
    assert combined.list_accounts()[0].arr == 120000.0


def test_submitting_what_the_book_already_shows_changes_nothing(store):
    _, combined = synced_book(store)
    edit_account(store, combined, "hs-1", {"arr": "$135,000"}, now=NOW)
    before = store.edits()

    assert edit_account(store, combined, "hs-1", {"arr": "$135,000", "name": "Acme"}, now=NOW) == []
    assert store.edits() == before


def test_a_hand_added_account_is_changed_in_place(store):
    created = create_account(store, dict(NEW_ACCOUNT), today=TODAY)
    combined = book(store, (MANUAL, ManualConnector(store)))

    changed = edit_account(
        store,
        combined,
        created.id,
        {"name": "Larkspur", "domain": "larkspur.example", "arr": "$150,000"},
        now=NOW,
    )

    assert changed == ["name", "domain", "arr"]
    saved = store.manual().accounts[0]
    assert (saved.name, saved.domain, saved.arr) == ("Larkspur", "larkspur.example", 150000.0)
    assert store.edits() == {}
    assert combined.list_accounts()[0].arr == 150000.0


def test_hand_added_milestones_are_saved_together(store):
    created = create_account(store, dict(NEW_ACCOUNT), today=TODAY)
    combined = book(store, (MANUAL, ManualConnector(store)))

    changed = edit_account(
        store,
        combined,
        created.id,
        {"milestones_total": "6", "milestones_done": "4", "milestones_overdue": "1"},
        now=NOW,
    )

    assert changed == ["milestones_total", "milestones_done", "milestones_overdue"]
    assert store.manual().accounts[0].milestones == Milestones(done=4, total=6, overdue=1)


def test_editing_an_account_outside_the_book_says_so(store):
    combined = book(store)
    with pytest.raises(EditError) as caught:
        edit_account(store, combined, "nope", {"arr": "1"}, now=NOW)
    assert str(caught.value) == "That account isn't in your book."


def test_every_bad_field_is_reported_and_nothing_is_saved(store):
    _, combined = synced_book(store)

    with pytest.raises(EditError) as caught:
        edit_account(
            store,
            combined,
            "hs-1",
            {
                "name": " ",
                "arr": "abc",
                "renewal_date": "soon",
                "milestones_total": "x",
                "domain": "nope",
                "tier": "Growth",  # valid, but it must not be saved either
            },
            now=NOW,
        )

    assert caught.value.errors == {
        "name": "Name can't be empty.",
        "domain": 'Website "nope" doesn\'t look like a website.',
        "arr": 'ARR "abc" isn\'t an amount.',
        "renewal_date": "Renewal date isn't a date.",
        "milestones_total": "Onboarding milestones must be a whole number, 0 or more.",
    }
    assert caught.value.field == "name"
    assert store.edits() == {}


def test_milestones_are_checked_together_against_what_the_book_shows(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", milestones=(4, 6, 1))])
    combined = book(store, (HUBSPOT, hubspot))

    with pytest.raises(EditError) as caught:
        edit_account(store, combined, "hs-1", {"milestones_total": "3"}, now=NOW)  # 4 done, 1 late

    assert caught.value.errors == {
        "milestones_done": "Milestones done and overdue can't add up to more than the total."
    }
    assert store.edits() == {}


def test_milestone_edits_that_fit_together_are_saved_together(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", milestones=(4, 6, 1))])
    combined = book(store, (HUBSPOT, hubspot))

    changed = edit_account(
        store, combined, "hs-1", {"milestones_done": "5", "milestones_total": "8"}, now=NOW
    )

    assert changed == ["milestones_total", "milestones_done"]
    assert combined.list_accounts()[0].milestones == Milestones(done=5, total=8, overdue=1)


def test_an_edit_is_dropped_with_a_notice_once_the_source_changes(store):
    hubspot, combined = synced_book(store)
    edit_account(store, combined, "hs-1", {"arr": "$135,000"}, now=NOW)
    assert combined.list_accounts()[0].arr == 135000.0

    hubspot.accounts = [acc("hs-1", "Acme", arr=140000.0)]
    combined.refresh()

    assert combined.list_accounts()[0].arr == 140000.0
    assert store.edits() == {}
    [notice] = store.notices("hs-1")
    assert notice.message == "HubSpot changed ARR to $140,000, replacing your edit of $135,000."


def test_revert_field_shows_the_connections_value_again(store):
    _, combined = synced_book(store)
    edit_account(store, combined, "hs-1", {"arr": "$135,000"}, now=NOW)

    revert_field(store, combined, "hs-1", "arr")

    assert store.edits() == {}
    assert combined.list_accounts()[0].arr == 120000.0


def test_revert_field_refuses_a_field_that_cannot_be_edited(store):
    _, combined = synced_book(store)
    with pytest.raises(EditError) as caught:
        revert_field(store, combined, "hs-1", "secret")
    assert str(caught.value) == "That field can't be edited."


# Readings ----------------------------------------------------------------------------------------


def test_a_reading_is_kept_as_manual_at_now_in_utc(store):
    _, combined = synced_book(store)
    moment = datetime(2026, 10, 21, 11, 0, tzinfo=timezone(timedelta(hours=1)))  # 10:00 UTC

    add_reading(store, combined, "hs-1", SignalKind.CHAMPION_LEFT, "yes", now=moment)

    [saved] = store.manual().signals
    assert (saved.account_id, saved.kind, saved.value, saved.source) == (
        "hs-1",
        SignalKind.CHAMPION_LEFT,
        1.0,
        "manual",
    )
    assert saved.observed_at == datetime(2026, 10, 21, 10, 0)


@pytest.mark.parametrize(("raw", "value"), [("yes", 1.0), ("Y", 1.0), ("no", 0.0), ("0", 0.0)])
def test_champion_left_takes_yes_or_no(store, raw, value):
    _, combined = synced_book(store)
    add_reading(store, combined, "hs-1", SignalKind.CHAMPION_LEFT, raw, now=NOW)
    assert store.manual().signals[-1].value == value


def test_champion_left_refuses_a_word_that_is_not_yes_or_no(store):
    _, combined = synced_book(store)
    with pytest.raises(EditError) as caught:
        add_reading(store, combined, "hs-1", SignalKind.CHAMPION_LEFT, "maybe", now=NOW)
    assert str(caught.value) == 'Champion left "maybe" isn\'t yes or no.'
    assert store.manual().signals == []


def test_a_hand_reading_is_the_latest_until_a_connection_reports_a_newer_one(store):
    hubspot = FakeConnector(
        "hubspot",
        [acc("hs-1")],
        [reading("hs-1", SignalKind.NPS_LATEST, 8.0, source="hubspot")],  # observed at 9:30
    )
    combined = book(store, (HUBSPOT, hubspot))

    add_reading(store, combined, "hs-1", SignalKind.NPS_LATEST, "4", now=datetime(2026, 10, 21, 12))
    latest = latest_signals(combined.list_signals("hs-1"))["hs-1"][SignalKind.NPS_LATEST]
    assert latest.value == 4.0

    hubspot.signals.append(
        Signal(
            account_id="hs-1",
            kind=SignalKind.NPS_LATEST,
            value=9.0,
            observed_at=datetime(2026, 10, 22, 8),
            source="hubspot",
        )
    )
    combined.refresh()
    latest = latest_signals(combined.list_signals("hs-1"))["hs-1"][SignalKind.NPS_LATEST]
    assert latest.value == 9.0


@pytest.mark.parametrize(
    ("kind", "raw", "value"),
    [
        (SignalKind.OPEN_TICKETS_SEV1, "3", 3.0),
        (SignalKind.DAYS_SINCE_LAST_LOGIN, "0", 0.0),
        (SignalKind.NPS_LATEST, "7.5", 7.5),
        (SignalKind.USAGE_TREND_30D, "-25.5", -25.5),
        (SignalKind.SEAT_UTILISATION, "64%", 64.0),
    ],
)
def test_readings_are_parsed_like_their_import_field(store, kind, raw, value):
    _, combined = synced_book(store)
    add_reading(store, combined, "hs-1", kind, raw, now=NOW)
    assert store.manual().signals[-1].value == value


@pytest.mark.parametrize(
    ("kind", "raw", "message"),
    [
        (
            SignalKind.OPEN_TICKETS_SEV1,
            "-1",
            "Open severity-1 tickets must be a whole number, 0 or more.",
        ),
        (
            SignalKind.OPEN_TICKETS_SEV1,
            "1.5",
            "Open severity-1 tickets must be a whole number, 0 or more.",
        ),
        (SignalKind.NPS_LATEST, "11", "NPS should be between 0 and 10."),
        (SignalKind.SEAT_UTILISATION, "lots", 'Seats in use "lots" isn\'t a number.'),
        (SignalKind.USAGE_TREND_30D, " ", "Usage trend can't be empty."),
    ],
)
def test_a_bad_reading_gives_a_plain_message(store, kind, raw, message):
    _, combined = synced_book(store)
    with pytest.raises(EditError) as caught:
        add_reading(store, combined, "hs-1", kind, raw, now=NOW)
    assert str(caught.value) == message
    assert store.manual().signals == []


def test_a_reading_for_an_account_outside_the_book_is_refused(store):
    _, combined = synced_book(store)
    with pytest.raises(EditError) as caught:
        add_reading(store, combined, "hs-404", SignalKind.NPS_LATEST, "4", now=NOW)
    assert str(caught.value) == "That account isn't in your book."


# Hiding ------------------------------------------------------------------------------------------


def test_hiding_a_linked_account_hides_every_member_and_showing_it_brings_the_link_back(store):
    _, _, combined = linked_book(store)
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    combined.invalidate()

    set_hidden(store, combined, "sp1-acme", True)
    assert sorted(store.hidden()) == ["hs-1", "sp1-acme"]
    assert [a.id for a in combined.list_accounts()] == ["sp1-globex"]

    set_hidden(store, combined, "hs-1", False)  # a member id shows the whole link again
    assert store.hidden() == []
    assert [a.id for a in combined.list_accounts()] == ["sp1-acme", "sp1-globex"]


def test_hide_and_show_refuse_an_unknown_account(store):
    _, combined = synced_book(store)
    with pytest.raises(EditError) as hiding:
        set_hidden(store, combined, "nope", True)
    with pytest.raises(EditError) as showing:
        set_hidden(store, combined, "nope", False)
    assert str(hiding.value) == str(showing.value) == "That account isn't in your book."


# Linking and unlinking ------------------------------------------------------------------------


def test_linking_needs_at_least_two_different_accounts(store):
    _, _, combined = linked_book(store)
    with pytest.raises(EditError) as caught:
        link_accounts(store, combined, ["hs-1", "hs-1"], "hs-1")
    assert str(caught.value) == "Choose at least two accounts to link."


def test_linking_needs_accounts_from_two_connections(store):
    _, _, combined = linked_book(store)
    with pytest.raises(EditError) as caught:
        link_accounts(store, combined, ["sp1-acme", "sp1-globex"], "sp1-acme")
    assert str(caught.value) == "Only accounts from different connections can be linked."
    assert store.links().groups == []


def test_linking_refuses_an_account_outside_the_book(store):
    _, _, combined = linked_book(store)
    with pytest.raises(EditError) as caught:
        link_accounts(store, combined, ["hs-1", "nope"], "hs-1")
    assert str(caught.value) == "That account isn't in your book."


def test_the_account_to_keep_has_to_be_one_of_the_linked_accounts(store):
    _, _, combined = linked_book(store)
    with pytest.raises(EditError) as caught:
        link_accounts(store, combined, ["hs-1", "sp1-acme"], "sp1-globex")
    assert str(caught.value) == "The account to keep has to be one of the accounts you're linking."


def test_linking_merges_the_accounts_under_the_primary(store):
    _, _, combined = linked_book(store)

    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    assert store.links().groups == [LinkGroup(primary="sp1-acme", members=["sp1-acme", "hs-1"])]
    assert [a.id for a in combined.list_accounts()] == ["sp1-acme", "sp1-globex"]
    assert combined.provenance("sp1-acme").member_ids == ["sp1-acme", "hs-1"]


def test_linking_an_account_already_in_a_link_merges_the_two_groups(store):
    _, _, combined = linked_book(store)
    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="hs-1")

    # The book now shows the merged account as hs-1, so that is the id to link.
    link_accounts(store, combined, ["hs-1", "sp1-globex"], primary="sp1-globex")

    assert store.links().groups == [
        LinkGroup(primary="sp1-globex", members=["sp1-globex", "hs-1", "sp1-acme"])
    ]
    assert [a.id for a in combined.list_accounts()] == ["sp1-globex"]


def test_linking_moves_edits_to_the_primary_and_the_primary_wins_a_clash(store):
    _, _, combined = linked_book(store)
    store.set_edit("hs-1", "arr", edit(135000.0, 120000.0))
    store.set_edit("hs-1", "tier", edit("Pro", "Enterprise"))
    store.set_edit("sp1-acme", "arr", edit(140000.0, 130000.0))

    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    # The primary's own arr edit wins. The moved tier edit is rebased onto the primary's tier.
    assert store.edits() == {
        "sp1-acme": {"arr": edit(140000.0, 130000.0), "tier": edit("Pro", "Growth")}
    }
    assert combined.list_accounts()[0].arr == 140000.0


def test_an_edit_on_the_other_account_survives_the_link(store):
    """Linking doesn't discard the CSM's edit: it is rebased onto the merged account's source
    value, so it still applies and shows on the merged account, with no notice."""
    _, _, combined = linked_book(store)
    store.set_edit("hs-1", "tier", edit("Pro", "Enterprise"))

    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    assert store.edits() == {"sp1-acme": {"tier": edit("Pro", "Growth")}}
    assert store.notices(include_dismissed=True) == []
    merged = combined.provenance("sp1-acme")
    assert merged.edits == {"tier": edit("Pro", "Growth")}
    assert [a.tier for a in combined.list_accounts()] == ["Pro", "Enterprise"]


def test_a_moved_edit_that_matches_the_merged_value_is_dropped(store):
    _, _, combined = linked_book(store)
    store.set_edit("hs-1", "tier", edit("Growth", "Enterprise"))

    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    assert store.edits() == {}  # the primary already shows that value, so it's a revert


def test_notices_follow_the_linked_accounts_onto_the_primary(store):
    _, _, combined = linked_book(store)
    original = store.add_notice(
        "hs-1", "arr", "HubSpot changed ARR to $1, replacing your edit of $2."
    )
    dismissed = store.add_notice("hs-1", "tier", "HubSpot changed Tier to Pro.")
    store.dismiss_notice(dismissed.id)

    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    assert store.notices("hs-1", include_dismissed=True) == []
    # Moved notices keep their id, time and dismissed flag; only the account changes.
    assert store.notices("sp1-acme", include_dismissed=True) == [
        original.model_copy(update={"account_id": "sp1-acme"}),
        dismissed.model_copy(update={"account_id": "sp1-acme", "dismissed": True}),
    ]


def test_unlink_shows_the_members_as_separate_accounts_again(store):
    _, _, combined = linked_book(store)
    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    unlink(store, combined, "sp1-acme")

    assert store.links() == LinkData()
    assert [a.id for a in combined.list_accounts()] == ["hs-1", "sp1-acme", "sp1-globex"]


def test_unlink_needs_a_linked_account(store):
    _, _, combined = linked_book(store)
    with pytest.raises(EditError) as linked:
        unlink(store, combined, "hs-1")
    with pytest.raises(EditError) as unknown:
        unlink(store, combined, "nope")
    assert str(linked.value) == "That account isn't linked to another."
    assert str(unknown.value) == "That account isn't in your book."


def test_dismissing_a_suggestion_stops_it_being_suggested(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", domain="acme.com")])
    sheet = FakeConnector("spreadsheet", [acc("sp1-acme", "ACME, Inc.", domain="acme.com")])
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))
    assert len(combined.duplicate_suggestions()) == 1

    dismiss_duplicate(store, combined, ("sp1-acme", "hs-1"))
    dismiss_duplicate(store, combined, ("hs-1", "sp1-acme"))  # the same pair, once

    assert store.links().dismissed == [("hs-1", "sp1-acme")]
    assert combined.duplicate_suggestions() == []


def test_dismiss_refuses_the_same_account_twice(store):
    _, _, combined = linked_book(store)
    with pytest.raises(EditError) as caught:
        dismiss_duplicate(store, combined, ("hs-1", "hs-1"))
    assert str(caught.value) == "Choose two different accounts."


def test_every_action_refuses_an_account_outside_the_book(store):
    _, combined = synced_book(store)
    calls = [
        lambda: edit_account(store, combined, "nope", {"arr": "1"}, now=NOW),
        lambda: revert_field(store, combined, "nope", "arr"),
        lambda: add_reading(store, combined, "nope", SignalKind.NPS_LATEST, "4", now=NOW),
        lambda: set_hidden(store, combined, "nope", True),
        lambda: link_accounts(store, combined, ["nope", "hs-1"], "hs-1"),
        lambda: unlink(store, combined, "nope"),
        lambda: dismiss_duplicate(store, combined, ("nope", "hs-1")),
        lambda: delete_account(store, "m-nope"),
    ]
    for call in calls:
        with pytest.raises(EditError):
            call()


def test_naive_and_aware_times_are_both_accepted(store):
    _, combined = synced_book(store)
    aware = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)
    edit_account(store, combined, "hs-1", {"arr": "$135,000"}, now=aware)
    assert store.edits()["hs-1"]["arr"].edited_at == NOW


# Contacts and client updates follow the account -----------------------------------------------


def test_deleting_a_hand_added_account_forgets_its_contacts_and_consent(store):
    created = create_account(store, dict(NEW_ACCOUNT), today=TODAY)
    keep = create_account(store, {**NEW_ACCOUNT, "name": "Other"}, today=TODAY)
    for account_id in (created.id, keep.id):
        contacts.add_contact(
            store,
            account_id,
            ContactInput(name="Dana"),
            source=ContactSource.YOU,
            now=NOW,
        )
        client_updates.opt_in(store, account_id, agreed_with="Dana", agreed_on=TODAY, now=NOW)

    delete_account(store, created.id)

    assert contacts.contacts_for(store, created.id) == []
    assert set(client_updates.load(store).consents) == {keep.id}
    assert [c.name for c in contacts.contacts_for(store, keep.id)] == ["Dana"]


def test_linking_moves_the_members_contacts_and_consent_to_the_primary(store):
    _, _, combined = linked_book(store)
    contacts.add_contact(
        store, "hs-1", ContactInput(name="Dana", champion=True), source=ContactSource.YOU, now=NOW
    )
    contacts.add_contact(
        store, "sp1-acme", ContactInput(name="Sam"), source=ContactSource.YOU, now=NOW
    )
    client_updates.opt_in(store, "hs-1", agreed_with="Dana", agreed_on=TODAY, now=NOW)

    link_accounts(store, combined, ["hs-1", "sp1-acme"], primary="sp1-acme")

    assert contacts.contacts_for(store, "hs-1") == []
    assert sorted(c.name for c in contacts.contacts_for(store, "sp1-acme")) == ["Dana", "Sam"]
    assert set(client_updates.load(store).consents) == {"sp1-acme"}
