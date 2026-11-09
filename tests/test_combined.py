"""CombinedConnector: merging members, links, hidden accounts, edits, provenance and caching.

The members are small fakes with a name, list_accounts and list_signals, and optionally refresh
or a ConnectorError to raise. The store is a temp folder.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from wardmind.core import service
from wardmind.core.config import ScoringConfig
from wardmind.core.connectors.combined import CombinedConnector, ConnectionProblem
from wardmind.core.connectors.errors import ConnectorError
from wardmind.core.connectors.local import ManualConnector, SpreadsheetConnector
from wardmind.core.models import Account, Milestones, Signal, SignalKind
from wardmind.core.store import (
    EDITABLE_FIELDS,
    ConnectionConfig,
    ConnectionKind,
    FieldEdit,
    ImportSnapshot,
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
    milestones: tuple[int, int, int] = (4, 6, 1),
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


def sig(
    acc_id: str,
    kind: SignalKind = SignalKind.NPS_LATEST,
    value: float = 8.0,
    source: str = "hubspot",
) -> Signal:
    return Signal(account_id=acc_id, kind=kind, value=value, observed_at=NOW, source=source)


def edit(value, base) -> FieldEdit:
    return FieldEdit(value=value, base_value=base, edited_at=NOW)


class FakeConnector:
    """A member connection whose answers the test changes as it likes."""

    def __init__(
        self,
        name: str,
        accounts: list[Account] | None = None,
        signals: list[Signal] | None = None,
        *,
        error: ConnectorError | None = None,
        signal_error: ConnectorError | None = None,
    ) -> None:
        self.name = name
        self.accounts = list(accounts or [])
        self.signals = list(signals or [])
        self.error = error
        self.signal_error = signal_error
        self.account_calls = 0
        self.refreshes = 0

    def list_accounts(self) -> list[Account]:
        if self.error is not None:
            raise self.error
        self.account_calls += 1
        return list(self.accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        if self.error is not None or self.signal_error is not None:
            raise self.error or self.signal_error
        if account_id is None:
            return list(self.signals)
        return [s for s in self.signals if s.account_id == account_id]

    def refresh(self) -> None:
        self.refreshes += 1


class NoRefreshConnector(FakeConnector):
    refresh = None  # a member with no refresh method at all


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def book(store: LocalStore, *members) -> CombinedConnector:
    return CombinedConnector(list(members), store, today=TODAY)


def link(store: LocalStore, primary: str, *members: str) -> None:
    store.save_links(LinkData(groups=[LinkGroup(primary=primary, members=list(members))]))


# Members and hidden accounts -----------------------------------------------------------------


def test_the_book_is_the_union_of_the_members_in_member_order(store):
    hubspot = FakeConnector(
        "hubspot", [acc("hs-1"), acc("hs-2", "Globex")], [sig("hs-1"), sig("hs-2")]
    )
    sheet = FakeConnector("spreadsheet", [acc("sp1-acme", "Acme sheet")], [sig("sp1-acme")])
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    assert [a.id for a in combined.list_accounts()] == ["hs-1", "hs-2", "sp1-acme"]
    assert [s.account_id for s in combined.list_signals()] == ["hs-1", "hs-2", "sp1-acme"]
    assert [s.account_id for s in combined.list_signals("hs-2")] == ["hs-2"]
    assert combined.problems == []
    assert combined.name == "combined"


def test_a_failing_member_becomes_a_problem_and_the_rest_still_load(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1")])
    sheet = FakeConnector(
        "spreadsheet",
        [acc("sp1-acme")],
        error=ConnectorError("Couldn't read the sheet. Import it again."),
    )
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    assert [a.id for a in combined.list_accounts()] == ["hs-1"]
    assert combined.problems == [
        ConnectionProblem(
            connection_id="sheet-1",
            name="Q4 renewals sheet",
            message="Couldn't read the sheet. Import it again.",
        )
    ]


def test_a_member_whose_signals_fail_is_left_out_as_a_whole(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1")], [sig("hs-1")])
    sheet = FakeConnector(
        "spreadsheet",
        [acc("sp1-acme")],
        [sig("sp1-acme")],
        signal_error=ConnectorError("The readings can't be read."),
    )
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    assert [a.id for a in combined.list_accounts()] == ["hs-1"]
    assert [s.account_id for s in combined.list_signals()] == ["hs-1"]
    assert [p.message for p in combined.problems] == ["The readings can't be read."]


def test_problems_are_listed_in_member_order(store):
    first = FakeConnector("hubspot", error=ConnectorError("HubSpot is down."))
    second = FakeConnector("spreadsheet", error=ConnectorError("The sheet is gone."))
    combined = book(store, (HUBSPOT, first), (SHEET, second))

    assert [p.connection_id for p in combined.problems] == ["hubspot", "sheet-1"]


def test_errors_other_than_connector_errors_propagate(store):
    class Broken(FakeConnector):
        def list_accounts(self):
            raise RuntimeError("a bug")

    combined = book(store, (HUBSPOT, Broken("hubspot")))
    with pytest.raises(RuntimeError, match="a bug"):
        combined.list_accounts()


def test_hidden_accounts_and_their_readings_are_dropped(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1"), acc("hs-2", "Globex")], [sig("hs-2")])
    store.set_hidden("hs-2", True)
    combined = book(store, (HUBSPOT, hubspot))

    assert [a.id for a in combined.list_accounts()] == ["hs-1"]
    assert combined.list_signals() == []
    assert combined.provenance("hs-2") is None


def test_the_real_local_connectors_work_as_members(store):
    store.add_connection(ConnectionKind.SPREADSHEET, "Q4 renewals sheet")
    store.save_snapshot(
        ImportSnapshot(
            connection_id="sheet-1",
            accounts=[acc("sp1-acme", "Acme sheet", domain=None)],
            signals=[sig("sp1-acme", source="spreadsheet")],
        )
    )
    store.save_manual(ManualData(accounts=[acc("m-00000001", "Hand added")]))
    combined = book(
        store,
        (SHEET, SpreadsheetConnector(store, "sheet-1")),
        (MANUAL, ManualConnector(store)),
    )
    assert [a.id for a in combined.list_accounts()] == ["sp1-acme", "m-00000001"]
    assert [s.account_id for s in combined.list_signals()] == ["sp1-acme"]


# Links ----------------------------------------------------------------------------------------


def test_a_linked_account_keeps_the_primary_id_name_and_fields(store):
    hubspot = FakeConnector(
        "hubspot", [acc("hs-1", "Acme Inc (HubSpot)", arr=120000.0, tier="Enterprise")]
    )
    sheet = FakeConnector(
        "spreadsheet",
        [acc("sp1-acme", "Acme", arr=130000.0, tier="Growth", owner="Corin Vashti")],
    )
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    [merged] = combined.list_accounts()
    assert merged.id == "sp1-acme"
    assert merged.name == "Acme"
    assert merged.arr == 130000.0
    assert merged.tier == "Growth"
    assert merged.owner == "Corin Vashti"


def test_fields_the_primary_lacks_are_filled_from_the_other_members(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", milestones=(4, 6, 1))])
    sheet = FakeConnector(
        "spreadsheet",
        [
            acc(
                "sp1-acme",
                "Acme sheet",
                arr=0.0,
                tier="Unknown",
                owner="Unassigned",
                domain=None,
                milestones=(0, 0, 0),
            )
        ],
    )
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    [merged] = combined.list_accounts()
    assert merged.name == "Acme sheet"  # never filled: the primary always has a name
    assert merged.start_date == date(2024, 1, 1)
    assert merged.domain == "acme.com"
    assert merged.tier == "Enterprise"
    assert merged.owner == "Dana Whitfield"
    assert merged.arr == 120000.0
    assert merged.milestones == Milestones(done=4, total=6, overdue=1)


def test_a_fill_takes_the_first_other_member_in_group_order(store):
    members = FakeConnector(
        "hubspot",
        [
            acc("hs-1", tier="Growth", domain="first.example"),
            acc("hs-2", tier="Starter", domain="second.example"),
        ],
    )
    primary = FakeConnector(
        "spreadsheet",
        [
            acc(
                "sp1-acme",
                tier="Unknown",
                owner="Unassigned",
                domain=None,
                arr=0.0,
                milestones=(0, 0, 0),
            )
        ],
    )
    store.save_links(
        LinkData(groups=[LinkGroup(primary="sp1-acme", members=["sp1-acme", "hs-2", "hs-1"])])
    )
    combined = book(store, (HUBSPOT, members), (SHEET, primary))

    [merged] = combined.list_accounts()
    assert merged.tier == "Starter"
    assert merged.domain == "second.example"


def test_a_member_absent_this_load_is_ignored(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1")])
    sheet = FakeConnector("spreadsheet", [])  # the sheet has no rows this load
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    [merged] = combined.list_accounts()
    assert merged.id == "hs-1"
    assert combined.provenance("hs-1").member_ids == ["hs-1"]
    assert combined.problems == []


def test_when_the_primary_is_absent_the_first_present_member_keeps_its_id(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme from HubSpot")])
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    combined = book(store, (HUBSPOT, hubspot))

    [merged] = combined.list_accounts()
    assert merged.id == "hs-1"
    assert merged.name == "Acme from HubSpot"


def test_signals_are_rekeyed_to_the_primary_and_the_members_are_untouched(store):
    hubspot_signals = [sig("hs-1", SignalKind.USAGE_TREND_30D, -20.0)]
    hubspot = FakeConnector("hubspot", [acc("hs-1")], hubspot_signals)
    sheet = FakeConnector(
        "spreadsheet",
        [acc("sp1-acme")],
        [sig("sp1-acme", SignalKind.NPS_LATEST, 3.0, "spreadsheet")],
    )
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    kinds = {s.kind for s in combined.list_signals("sp1-acme")}
    assert kinds == {SignalKind.USAGE_TREND_30D, SignalKind.NPS_LATEST}
    assert all(s.account_id == "sp1-acme" for s in combined.list_signals())
    assert combined.list_signals("hs-1") == []
    assert hubspot_signals[0].account_id == "hs-1"  # the member's own object is not changed


def test_hand_entered_readings_are_rekeyed_through_links(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1"), acc("hs-2", "Globex")])
    sheet = FakeConnector("spreadsheet", [acc("sp1-acme")])
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    store.save_manual(
        ManualData(
            signals=[
                sig("hs-1", SignalKind.CHAMPION_LEFT, 1.0, "manual"),
                sig("hs-2", SignalKind.NPS_LATEST, 4.0, "manual"),
                sig("hs-404", SignalKind.NPS_LATEST, 2.0, "manual"),  # not in the book
            ]
        )
    )
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    merged = combined.list_signals("sp1-acme")
    assert [(s.kind, s.value, s.source) for s in merged] == [
        (SignalKind.CHAMPION_LEFT, 1.0, "manual")
    ]
    assert [s.value for s in combined.list_signals("hs-2")] == [4.0]
    assert combined.list_signals("hs-404") == []


def test_a_manual_member_does_not_double_its_own_readings(store):
    store.save_manual(
        ManualData(
            accounts=[acc("m-00000001", "Hand added", domain=None)],
            signals=[sig("m-00000001", SignalKind.NPS_LATEST, 9.0, "manual")],
        )
    )
    combined = book(store, (MANUAL, ManualConnector(store)))

    assert [s.value for s in combined.list_signals("m-00000001")] == [9.0]


# Edits ----------------------------------------------------------------------------------------


def test_an_edit_applies_while_the_source_still_reports_the_base_value(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", arr=120000.0)])
    store.set_edit("hs-1", "arr", edit(135000.0, 120000.0))
    combined = book(store, (HUBSPOT, hubspot))

    [merged] = combined.list_accounts()
    assert merged.arr == 135000.0
    provenance = combined.provenance("hs-1")
    assert provenance.edits == {"arr": edit(135000.0, 120000.0)}
    assert provenance.source_values["arr"] == 120000.0
    assert store.edits()["hs-1"]["arr"] == edit(135000.0, 120000.0)
    assert store.notices() == []


def test_an_edit_is_dropped_with_the_exact_notice_when_the_source_changes(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", arr=120000.0)])
    store.set_edit("hs-1", "arr", edit(135000.0, 120000.0))
    combined = book(store, (HUBSPOT, hubspot))
    assert combined.list_accounts()[0].arr == 135000.0

    hubspot.accounts = [acc("hs-1", arr=140000.0)]
    combined.refresh()

    assert combined.list_accounts()[0].arr == 140000.0
    assert store.edits() == {}
    [notice] = store.notices("hs-1")
    assert notice.field == "arr"
    assert notice.message == "HubSpot changed ARR to $140,000, replacing your edit of $135,000."

    combined.refresh()
    combined.list_accounts()
    assert len(store.notices("hs-1", include_dismissed=True)) == 1  # not added again


def test_a_changed_date_reads_in_words_in_the_notice(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", renewal=date(2026, 12, 1))])
    store.set_edit("hs-1", "renewal_date", edit(date(2027, 3, 1), date(2027, 1, 1)))
    combined = book(store, (HUBSPOT, hubspot))

    assert combined.list_accounts()[0].renewal_date == date(2026, 12, 1)
    [notice] = store.notices("hs-1")
    assert notice.message == (
        "HubSpot changed Renewal date to 1 December 2026, replacing your edit of 1 March 2027."
    )


def test_money_that_differs_only_in_its_last_digits_counts_as_changed(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", arr=1234568.0)])
    store.set_edit("hs-1", "arr", edit(2000000.0, 1234567.0))
    combined = book(store, (HUBSPOT, hubspot))

    assert combined.list_accounts()[0].arr == 1234568.0
    [notice] = store.notices("hs-1")
    assert notice.message == (
        "HubSpot changed ARR to $1,234,568, replacing your edit of $2,000,000."
    )


def test_money_dates_and_blanks_compare_by_their_display_text(store):
    hubspot = FakeConnector(
        "hubspot", [acc("hs-1", arr=120000.0, domain=None, renewal=date(2027, 1, 1))]
    )
    store.set_edit("hs-1", "arr", edit(130000.0, 120000))  # whole number base, float source
    store.set_edit("hs-1", "domain", edit("acme.example", None))  # blank base, blank source
    store.set_edit("hs-1", "renewal_date", edit(date(2028, 1, 1), "2027-01-01"))  # ISO text base
    combined = book(store, (HUBSPOT, hubspot))

    merged = combined.list_accounts()[0]
    assert (merged.arr, merged.domain, merged.renewal_date) == (
        130000.0,
        "acme.example",
        date(2028, 1, 1),
    )
    assert store.notices() == []


def test_a_blank_source_matches_a_blank_base_text(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", domain=None)])
    store.set_edit("hs-1", "domain", edit("acme.example", ""))
    combined = book(store, (HUBSPOT, hubspot))

    assert combined.list_accounts()[0].domain == "acme.example"


def test_a_changed_source_to_blank_is_reported_as_nothing(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", domain=None)])
    store.set_edit("hs-1", "domain", edit("acme.example", "acme.com"))
    combined = book(store, (HUBSPOT, hubspot))

    assert combined.list_accounts()[0].domain is None
    [notice] = store.notices("hs-1")
    assert notice.message == (
        "HubSpot changed Website to nothing, replacing your edit of acme.example."
    )


def test_milestone_edits_that_no_longer_add_up_are_removed_with_a_notice(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", milestones=(4, 6, 1))])
    store.set_edit("hs-1", "milestones_total", edit(3, 6))  # 4 done is more than 3
    combined = book(store, (HUBSPOT, hubspot))

    assert combined.list_accounts()[0].milestones == Milestones(done=4, total=6, overdue=1)
    assert store.edits() == {}
    [notice] = store.notices("hs-1")
    assert notice.message == (
        "Your onboarding milestone edits no longer fit HubSpot's numbers, so they were removed."
    )


def test_milestone_edits_that_fit_together_are_applied(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", milestones=(4, 6, 1))])
    store.set_edit("hs-1", "milestones_done", edit(5, 4))
    store.set_edit("hs-1", "milestones_total", edit(8, 6))
    combined = book(store, (HUBSPOT, hubspot))

    assert combined.list_accounts()[0].milestones == Milestones(done=5, total=8, overdue=1)
    assert store.notices() == []


def test_an_edit_is_left_alone_when_its_account_is_absent_this_load(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1")])
    sheet = FakeConnector("spreadsheet", error=ConnectorError("The sheet can't be read."))
    # The sheet account is not in the book this load, so its base doesn't match and that's fine.
    store.set_edit("sp1-acme", "arr", edit(135000.0, 999.0))
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    assert [a.id for a in combined.list_accounts()] == ["hs-1"]
    assert store.edits()["sp1-acme"]["arr"] == edit(135000.0, 999.0)
    assert store.notices() == []


# Provenance -----------------------------------------------------------------------------------


def test_provenance_describes_where_a_linked_account_came_from(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", arr=120000.0, tier="Enterprise")])
    sheet = FakeConnector(
        "spreadsheet",
        [
            acc(
                "sp1-acme",
                "Acme sheet",
                arr=0.0,
                tier="Unknown",
                owner="Unassigned",
                domain=None,
                milestones=(0, 0, 0),
            )
        ],
    )
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    store.set_edit("sp1-acme", "tier", edit("Growth", "Enterprise"))
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    provenance = combined.provenance("sp1-acme")
    assert provenance.account_id == "sp1-acme"
    assert provenance.member_ids == ["sp1-acme", "hs-1"]
    assert provenance.connections == ["Q4 renewals sheet", "HubSpot"]
    assert provenance.editable_in_place is False
    assert provenance.edits == {"tier": edit("Growth", "Enterprise")}
    assert set(provenance.source_values) == set(EDITABLE_FIELDS)
    assert provenance.source_values["name"] == "Acme sheet"
    assert provenance.source_values["tier"] == "Enterprise"  # before the edit
    assert provenance.source_values["arr"] == 120000.0  # filled from HubSpot
    assert provenance.source_values["milestones_total"] == 6
    assert combined.list_accounts()[0].tier == "Growth"
    assert combined.provenance("nope") is None


def test_a_manual_account_is_editable_in_place(store):
    store.save_manual(ManualData(accounts=[acc("m-00000001", "Hand added", domain=None)]))
    combined = book(store, (MANUAL, ManualConnector(store)))

    provenance = combined.provenance("m-00000001")
    assert provenance.member_ids == ["m-00000001"]
    assert provenance.connections == ["Added by hand"]
    assert provenance.editable_in_place is True
    assert provenance.edits == {}
    assert provenance.source_values["name"] == "Hand added"


def test_a_plain_synced_account_has_one_member_and_is_not_editable_in_place(store):
    combined = book(store, (HUBSPOT, FakeConnector("hubspot", [acc("hs-1")])))
    provenance = combined.provenance("hs-1")
    assert provenance.member_ids == ["hs-1"]
    assert provenance.connections == ["HubSpot"]
    assert provenance.editable_in_place is False


# Caching and refresh --------------------------------------------------------------------------


def test_the_book_is_cached_until_refresh_and_refresh_reaches_the_members(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1")])
    sheet = NoRefreshConnector("spreadsheet", [acc("sp1-acme", "Acme sheet")])
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    combined.list_accounts()
    combined.list_accounts()
    combined.list_signals()
    assert combined.problems == []
    combined.provenance("hs-1")
    assert hubspot.account_calls == 1

    hubspot.accounts = [acc("hs-1", "Changed")]
    store.set_hidden("sp1-acme", True)
    assert [a.name for a in combined.list_accounts()] == ["Acme", "Acme sheet"]

    combined.refresh()
    assert hubspot.refreshes == 1
    assert [a.name for a in combined.list_accounts()] == ["Changed"]
    assert hubspot.account_calls == 2


def test_refresh_is_fine_with_a_member_that_has_no_refresh(store):
    combined = book(store, (SHEET, NoRefreshConnector("spreadsheet", [acc("sp1-acme")])))
    combined.list_accounts()
    combined.refresh()
    assert [a.id for a in combined.list_accounts()] == ["sp1-acme"]


def test_a_failing_member_is_retried_after_refresh(store):
    sheet = FakeConnector("spreadsheet", error=ConnectorError("Not yet."))
    combined = book(store, (SHEET, sheet))
    assert [p.message for p in combined.problems] == ["Not yet."]

    sheet.error = None
    sheet.accounts = [acc("sp1-acme")]
    combined.refresh()
    assert combined.problems == []
    assert [a.id for a in combined.list_accounts()] == ["sp1-acme"]


# Duplicates -----------------------------------------------------------------------------------


def test_duplicate_suggestions_come_from_the_loaded_accounts(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", domain="acme.com")])
    sheet = FakeConnector(
        "spreadsheet",
        [acc("sp1-acme", "ACME, Inc.", domain=None), acc("sp1-globex", "Globex", domain=None)],
    )
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    [suggestion] = combined.duplicate_suggestions()
    assert suggestion.account_ids == ("hs-1", "sp1-acme")
    assert suggestion.names == ("Acme", "ACME, Inc.")
    assert suggestion.reason == "Same name: Acme"


def test_a_linked_pair_is_no_longer_suggested(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", domain="acme.com")])
    sheet = FakeConnector("spreadsheet", [acc("sp1-acme", "ACME, Inc.", domain="acme.com")])
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))
    assert len(combined.duplicate_suggestions()) == 1

    link(store, "hs-1", "hs-1", "sp1-acme")
    combined.refresh()
    assert combined.duplicate_suggestions() == []


def test_edits_do_not_change_the_duplicate_suggestions(store):
    hubspot = FakeConnector("hubspot", [acc("hs-1", "Acme", domain="acme.com")])
    sheet = FakeConnector("spreadsheet", [acc("sp1-acme", "ACME, Inc.", domain=None)])
    store.set_edit("sp1-acme", "name", edit("Zeta Labs", "ACME, Inc."))
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    assert combined.list_accounts()[1].name == "Zeta Labs"
    [suggestion] = combined.duplicate_suggestions()
    assert suggestion.names == ("Acme", "ACME, Inc.")


# Scoring --------------------------------------------------------------------------------------


def test_get_book_scores_a_combined_book_with_a_link_and_an_edit(store):
    hubspot = FakeConnector(
        "hubspot",
        [acc("hs-1", "Acme", arr=120000.0, renewal=date(2026, 12, 1), milestones=(0, 0, 0))],
        [sig("hs-1", SignalKind.USAGE_TREND_30D, -40.0, "hubspot")],
    )
    sheet = FakeConnector(
        "spreadsheet",
        [
            acc(
                "sp1-acme",
                "Acme",
                arr=0.0,
                tier="Unknown",
                owner="Unassigned",
                domain=None,
                renewal=date(2026, 12, 1),
                milestones=(0, 0, 0),
            ),
            acc(
                "sp1-globex", "Globex", arr=40000.0, renewal=date(2027, 6, 1), milestones=(0, 0, 0)
            ),
        ],
        [sig("sp1-acme", SignalKind.NPS_LATEST, 2.0, "spreadsheet")],
    )
    link(store, "sp1-acme", "sp1-acme", "hs-1")
    store.set_edit("sp1-acme", "arr", edit(150000.0, 120000.0))
    combined = book(store, (HUBSPOT, hubspot), (SHEET, sheet))

    scored = service.get_book(connector=combined, config=ScoringConfig(), as_of=TODAY)

    by_id = {item.account.id: item for item in scored}
    assert set(by_id) == {"sp1-acme", "sp1-globex"}
    merged = by_id["sp1-acme"]
    assert merged.account.arr == 150000.0
    assert merged.account.domain == "acme.com"
    kinds = {reason.kind for reason in merged.reasons}
    assert kinds == {SignalKind.USAGE_TREND_30D, SignalKind.NPS_LATEST}
