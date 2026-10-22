"""The import steps: drafts, mapping memory, date order, preview and commit.

Each test works in its own temp data folder (conftest), so no real store is written.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook

from cs_brief.core import import_flow, importing
from cs_brief.core.importing import DateOrder
from cs_brief.core.models import SignalKind
from cs_brief.core.store import ConnectionKind, LocalStore

TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 9, 0)

RENEWALS = (
    "Company,ARR,Renewal date,Owner,Usage trend\n"
    '"Larkspur Freight","$184,000",2026-12-01,Dana Whitfield,-38\n'
    '"Quillon Health","$96,500",2026-11-15,Sam Okafor,6\n'
)
RENEWALS_EDITED = (
    "Company,ARR,Renewal date,Owner,Usage trend\n"
    '"Larkspur Freight","$190,000",2026-12-01,Dana Whitfield,-38\n'
    '"Brindle Studio","$543,000",2027-01-10,Corin Vashti,-5\n'
)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def _table(text: str, name: str = "renewals.csv") -> importing.Table:
    return importing.read_csv(text.encode("utf-8"), name)


def _draft(
    store: LocalStore,
    text: str = RENEWALS,
    *,
    now: datetime = NOW,
    target: str = "new",
    new_name: str = "",
    back: str = "setup",
) -> import_flow.Draft:
    return import_flow.save_draft(
        store,
        table=_table(text),
        file_bytes=text.encode("utf-8"),
        target=target,
        new_name=new_name,
        back=back,
        now=now,
    )


def _auto(draft: import_flow.Draft) -> dict[str, str]:
    """The automatic mapping for a draft's headers."""
    return importing.auto_map(draft.table.headers)


def _commit(store: LocalStore, draft: import_flow.Draft, mapping: dict[str, str], at: datetime):
    return import_flow.commit(draft, mapping, DateOrder.YMD, store, now=at, today=TODAY)


# Drafts -----------------------------------------------------------------------------------------


def test_a_draft_round_trips_through_the_store(store: LocalStore) -> None:
    draft = _draft(store, new_name="Renewals", back="connections")
    loaded = import_flow.load_draft(store, draft.id, now=NOW)
    assert loaded is not None
    assert loaded.id == draft.id
    assert loaded.source_name == "renewals.csv"
    assert loaded.table == draft.table
    assert loaded.new_name == "Renewals"
    assert loaded.back == "connections"
    assert import_flow.original_bytes(loaded) == RENEWALS.encode("utf-8")
    assert loaded.pasted_text is None


def test_a_pasted_draft_has_no_file_bytes(store: LocalStore) -> None:
    table = importing.read_pasted("Company\tARR\tRenewal date\nAcme\t1000\t2027-01-01")
    draft = import_flow.save_draft(store, table=table, pasted_text="x", now=NOW)
    assert import_flow.original_bytes(draft) is None
    assert draft.source_name == "Pasted cells"


def test_a_new_draft_id_is_16_lowercase_hex_characters(store: LocalStore) -> None:
    draft = _draft(store)
    assert len(draft.id) == 16
    assert import_flow.is_draft_id(draft.id)
    assert import_flow.new_draft_id() != import_flow.new_draft_id()


@pytest.mark.parametrize(
    "bad_id",
    [
        "../x",
        "..\\x",
        "..",
        "0123456789ABCDEF",
        "0123456789abcde",
        "0123456789abcdef0",
        "",
        "x" * 16,
    ],
)
def test_a_malformed_draft_id_is_never_used(store: LocalStore, bad_id: str) -> None:
    _draft(store)
    outside = store.root / "x.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("keep me", encoding="utf-8")
    assert import_flow.is_draft_id(bad_id) is False
    assert import_flow.load_draft(store, bad_id, now=NOW) is None
    import_flow.delete_draft(store, bad_id)
    assert outside.read_text(encoding="utf-8") == "keep me"


def test_a_draft_older_than_a_day_reads_as_none_and_is_deleted(store: LocalStore) -> None:
    draft = _draft(store)
    path = store.root / "drafts" / f"{draft.id}.json"
    assert path.exists()
    later = NOW + timedelta(hours=24, minutes=1)
    assert import_flow.load_draft(store, draft.id, now=later) is None
    assert not path.exists()


def test_a_draft_just_under_a_day_old_still_loads(store: LocalStore) -> None:
    draft = _draft(store)
    assert import_flow.load_draft(store, draft.id, now=NOW + timedelta(hours=23)) is not None


def test_saving_purges_drafts_that_have_expired(store: LocalStore) -> None:
    old = _draft(store, now=NOW - timedelta(days=2))
    fresh = _draft(store, now=NOW)
    assert import_flow.load_draft(store, old.id, now=NOW) is None
    assert import_flow.load_draft(store, fresh.id, now=NOW) is not None


def test_purge_removes_old_and_unreadable_drafts_only(store: LocalStore) -> None:
    old = _draft(store)
    fresh = _draft(store, now=NOW)
    folder = store.root / "drafts"
    old_path = folder / f"{old.id}.json"
    data = json.loads(old_path.read_text(encoding="utf-8"))
    data["created_at"] = (NOW - timedelta(days=3)).isoformat()
    old_path.write_text(json.dumps(data), encoding="utf-8")
    corrupt = folder / "0123456789abcdef.json"
    corrupt.write_text("not json", encoding="utf-8")
    other = folder / "notes.txt"
    other.write_text("keep", encoding="utf-8")

    removed = import_flow.purge_old_drafts(store, now=NOW)

    assert removed == 2
    assert not old_path.exists()
    assert not corrupt.exists()
    assert (folder / f"{fresh.id}.json").exists()
    assert other.exists()


def test_delete_draft_removes_the_file(store: LocalStore) -> None:
    draft = _draft(store)
    import_flow.delete_draft(store, draft.id)
    assert import_flow.load_draft(store, draft.id, now=NOW) is None


def test_saving_refuses_a_target_that_is_not_a_spreadsheet(store: LocalStore) -> None:
    store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    with pytest.raises(ValueError):
        _draft(store, target="manual")
    with pytest.raises(ValueError):
        _draft(store, target="sheet-9")


def test_a_bad_back_value_falls_back_to_setup(store: LocalStore) -> None:
    draft = import_flow.save_draft(store, table=_table(RENEWALS), back="somewhere", now=NOW)
    assert draft.back == "setup"


# Names, ids and the next connection ------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   "])
def test_a_blank_name_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="Give it a name"):
        import_flow.validate_name(text)


def test_a_name_is_stripped_and_limited_to_sixty_characters() -> None:
    assert import_flow.validate_name(" " + "a" * 60 + " ") == "a" * 60
    with pytest.raises(ValueError, match="at most 60 characters"):
        import_flow.validate_name("a" * 61)


def test_default_names_come_from_the_file_or_the_paste() -> None:
    assert import_flow.default_name("Q4 renewals.xlsx") == "Q4 renewals"
    assert import_flow.default_name("Pasted cells") == "Pasted cells"


def test_the_placeholder_id_is_the_id_a_new_connection_gets(store: LocalStore) -> None:
    assert import_flow.next_sheet_id(store) == "sheet-1"
    draft = _draft(store)
    outcome = _commit(store, draft, _auto(draft), NOW)
    assert outcome.connection.id == "sheet-1"
    assert import_flow.next_sheet_id(store) == "sheet-2"


def test_the_next_sheet_number_never_reuses_a_removed_one(store: LocalStore) -> None:
    store.add_connection(ConnectionKind.SPREADSHEET, "One")
    store.add_connection(ConnectionKind.SPREADSHEET, "Two")
    store.add_connection(ConnectionKind.SPREADSHEET, "Three")
    store.remove_connection("sheet-2")
    assert import_flow.next_sheet_id(store) == "sheet-4"


# Mapping memory ----------------------------------------------------------------------------------


def test_a_new_import_starts_from_auto_map(store: LocalStore) -> None:
    draft = _draft(store)
    assert import_flow.suggested_mapping(draft, store) == _auto(draft)
    assert import_flow.suggested_mapping(draft, store)["name"] == "Company"


def test_a_reimport_starts_from_the_remembered_mapping(store: LocalStore) -> None:
    first = _draft(store)
    remembered = {"name": "Company", "arr": "ARR", "renewal_date": "Renewal date"}
    _commit(store, first, remembered, NOW)
    # The second file calls the ARR column something auto_map does not know.
    edited = (
        "Account,Annual money,Renewal date,Owner\n"
        '"Larkspur Freight","$190,000",2026-12-01,Dana Whitfield\n'
    )
    second = import_flow.save_draft(store, table=_table(edited), target="sheet-1", now=NOW)
    mapping = import_flow.suggested_mapping(second, store)
    assert mapping["renewal_date"] == "Renewal date"  # remembered
    assert mapping["name"] == "Account"  # "Company" is gone, so auto_map fills it
    assert "arr" not in mapping  # "Annual money" isn't an ARR synonym, and nothing remembered it


def test_a_remembered_header_that_is_gone_is_dropped(store: LocalStore) -> None:
    first = _draft(store)
    _commit(store, first, {"name": "Company", "arr": "ARR", "renewal_date": "Renewal date"}, NOW)
    no_arr = 'Company,Renewal date\n"Larkspur",2026-12-01\n'
    second = import_flow.save_draft(store, table=_table(no_arr), target="sheet-1", now=NOW)
    mapping = import_flow.suggested_mapping(second, store)
    assert "arr" not in mapping
    assert mapping["name"] == "Company"


def test_clean_mapping_keeps_only_real_fields_and_headers() -> None:
    cleaned = import_flow.clean_mapping(
        _table(RENEWALS), {"name": "Company", "arr": "Nope", "bogus": "ARR", "owner": ""}
    )
    assert cleaned == {"name": "Company"}


# Date order -------------------------------------------------------------------------------------


def test_iso_dates_suggest_year_first(store: LocalStore) -> None:
    draft = _draft(store)
    mapping = {"renewal_date": "Renewal date"}
    assert import_flow.date_columns(draft, mapping) == ["Renewal date"]
    assert import_flow.suggested_date_order(draft, mapping) is DateOrder.YMD


def test_a_day_above_twelve_suggests_day_first(store: LocalStore) -> None:
    text = 'Company,ARR,Renewal date\nAcme,"$1",31/12/2026\nBeta,"$2",01/02/2027\n'
    draft = _draft(store, text)
    implied = import_flow.suggested_date_order(draft, {"renewal_date": "Renewal date"})
    assert implied is DateOrder.DMY


def test_an_ambiguous_column_suggests_nothing(store: LocalStore) -> None:
    text = 'Company,ARR,Renewal date\nAcme,"$1",03/04/2026\nBeta,"$2",05/06/2026\n'
    draft = _draft(store, text)
    assert import_flow.suggested_date_order(draft, {"renewal_date": "Renewal date"}) is None


def test_no_date_columns_suggest_nothing(store: LocalStore) -> None:
    draft = _draft(store)
    assert import_flow.suggested_date_order(draft, {"name": "Company"}) is None


def test_the_date_order_choice_prefers_the_draft_then_the_connection(store: LocalStore) -> None:
    draft = _draft(store)
    assert import_flow.date_order_choice(draft, store) is DateOrder.DMY
    chosen = import_flow.remember_choices(store, draft, mapping={}, date_order=DateOrder.MDY)
    assert import_flow.date_order_choice(chosen, store) is DateOrder.MDY


def test_resolve_uses_the_file_before_the_choice(store: LocalStore) -> None:
    draft = _draft(store)  # ISO dates: year first is implied
    mapping = {"renewal_date": "Renewal date"}
    assert import_flow.resolve_date_order(draft, mapping, "dmy") is DateOrder.YMD
    ambiguous = _draft(store, 'Company,ARR,Renewal date\nAcme,"$1",03/04/2026\n')
    assert import_flow.resolve_date_order(ambiguous, mapping, "mdy") is DateOrder.MDY
    assert import_flow.resolve_date_order(ambiguous, mapping, "nonsense") is DateOrder.DMY


# Preview -----------------------------------------------------------------------------------------


def test_preview_counts_issues_and_examples(store: LocalStore) -> None:
    text = (
        "Company,ARR,Renewal date,Usage trend\n"
        '"Larkspur Freight","$184,000",2026-12-01,-38\n'
        '"Quillon Health","TBC",2026-11-15,6\n'
        '"Brindle Studio","$543,000",,-5\n'
    )
    draft = _draft(store, text)
    result = import_flow.preview(draft, _auto(draft), DateOrder.YMD, store, now=NOW, today=TODAY)
    assert result.ready_rows == 1
    assert result.skipped_rows == 2
    assert {issue.row for issue in result.issues} == {3, 4}
    assert result.examples["name"] == "Larkspur Freight"
    assert result.examples["arr"] == "$184,000"
    assert result.example_row == 2
    assert result.missing_required == []
    assert result.removed_ids == []
    assert result.blocker == ""


def test_preview_names_unmapped_required_fields(store: LocalStore) -> None:
    draft = _draft(store)
    result = import_flow.preview(
        draft, {"name": "Company"}, DateOrder.YMD, store, now=NOW, today=TODAY
    )
    assert result.missing_required == ["ARR", "Renewal date"]
    assert result.blocker == "Match ARR and Renewal date to import."
    assert result.ready_rows == 0


def test_preview_blocker_for_one_missing_field_and_for_zero_ready(store: LocalStore) -> None:
    draft = _draft(store)
    one_missing = import_flow.preview(
        draft, {"name": "Company", "arr": "ARR"}, DateOrder.YMD, store, now=NOW, today=TODAY
    )
    assert one_missing.blocker == "Match Renewal date to import."
    all_skipped = _draft(store, "Company,ARR,Renewal date\nAcme,TBC,2026-01-01\n")
    result = import_flow.preview(
        all_skipped, _auto(all_skipped), DateOrder.YMD, store, now=NOW, today=TODAY
    )
    assert result.blocker.startswith("No accounts are ready to import.")


def test_preview_lists_accounts_that_would_leave_on_reimport(store: LocalStore) -> None:
    first = _draft(store)
    _commit(store, first, _auto(first), NOW)
    second = import_flow.save_draft(store, table=_table(RENEWALS_EDITED), target="sheet-1", now=NOW)
    result = import_flow.preview(second, _auto(second), DateOrder.YMD, store, now=NOW, today=TODAY)
    assert result.ready_rows == 2
    assert result.removed_ids == ["sp1-quillon-health"]


def test_preview_creates_nothing(store: LocalStore) -> None:
    draft = _draft(store)
    import_flow.preview(draft, _auto(draft), DateOrder.YMD, store, now=NOW, today=TODAY)
    assert store.connections() == []
    assert not (store.root / "sheets").exists()


# Commit ------------------------------------------------------------------------------------------


def test_commit_creates_a_connection_and_deletes_the_draft(store: LocalStore) -> None:
    draft = _draft(store, target="new", new_name="Q4 renewals")
    mapping = _auto(draft)
    outcome = _commit(store, draft, mapping, NOW)

    assert outcome.connection.name == "Q4 renewals"
    assert outcome.connection.enabled is True
    assert outcome.connection.mapping == mapping
    assert outcome.connection.date_order == "ymd"
    assert outcome.connection.last_file_name == "renewals.csv"
    assert outcome.connection.last_imported_at == NOW
    assert outcome.ready_rows == 2
    assert outcome.removed == 0
    assert [c.id for c in store.connections()] == ["sheet-1"]
    snapshot = store.snapshot("sheet-1")
    assert snapshot is not None
    assert len(snapshot.accounts) == 2
    assert snapshot.history[-1].accounts == 2
    assert import_flow.load_draft(store, draft.id, now=NOW) is None


def test_commit_defaults_the_name_to_the_file(store: LocalStore) -> None:
    draft = _draft(store, new_name="")
    outcome = _commit(store, draft, _auto(draft), NOW)
    assert outcome.connection.name == "renewals"


def test_a_reimport_appends_history_only_for_changed_values_and_drops_removed(
    store: LocalStore,
) -> None:
    first = _draft(store)
    _commit(store, first, _auto(first), NOW)
    before = store.snapshot("sheet-1")
    assert before is not None
    assert len([s for s in before.signals if s.kind is SignalKind.USAGE_TREND_30D]) == 2

    later = NOW + timedelta(days=7)
    second = import_flow.save_draft(
        store, table=_table(RENEWALS_EDITED), target="sheet-1", now=later
    )
    outcome = _commit(store, second, _auto(second), later)

    assert outcome.removed == 1
    after = store.snapshot("sheet-1")
    assert after is not None
    assert [a.id for a in after.accounts] == ["sp1-larkspur-freight", "sp1-brindle-studio"]
    assert len(after.history) == 2
    assert after.history[-1].accounts == 2
    usage = [s for s in after.signals if s.kind is SignalKind.USAGE_TREND_30D]
    # Larkspur's usage is unchanged, so nothing is added. Quillon left, so its readings go.
    # Brindle is new, so its reading is kept.
    assert sorted(s.account_id for s in usage) == ["sp1-brindle-studio", "sp1-larkspur-freight"]


def test_a_changed_reading_is_appended_to_its_history(store: LocalStore) -> None:
    first = _draft(store)
    _commit(store, first, _auto(first), NOW)
    later = NOW + timedelta(days=7)
    changed = import_flow.save_draft(
        store, table=_table(RENEWALS.replace("-38", "-45")), target="sheet-1", now=later
    )
    _commit(store, changed, _auto(changed), later)
    snapshot = store.snapshot("sheet-1")
    assert snapshot is not None
    larkspur = [
        s.value
        for s in snapshot.signals
        if s.account_id == "sp1-larkspur-freight" and s.kind is SignalKind.USAGE_TREND_30D
    ]
    assert larkspur == [-38.0, -45.0]


def test_commit_refuses_a_missing_required_field_and_changes_nothing(store: LocalStore) -> None:
    draft = _draft(store)
    with pytest.raises(ValueError, match="Match ARR to import."):
        _commit(store, draft, {"name": "Company", "renewal_date": "Renewal date"}, NOW)
    assert store.connections() == []
    assert import_flow.load_draft(store, draft.id, now=NOW) is not None


def test_commit_refuses_when_no_account_is_ready(store: LocalStore) -> None:
    draft = _draft(store, "Company,ARR,Renewal date\nAcme,TBC,2026-01-01\n")
    with pytest.raises(ValueError, match="No accounts are ready to import."):
        _commit(store, draft, _auto(draft), NOW)
    assert store.connections() == []


def test_commit_into_a_connection_that_was_removed_is_refused(store: LocalStore) -> None:
    store.add_connection(ConnectionKind.SPREADSHEET, "Old")
    draft = _draft(store, target="sheet-1")
    store.remove_connection("sheet-1")
    with pytest.raises(ValueError):
        _commit(store, draft, _auto(draft), NOW)


def test_remember_choices_keeps_the_mapping_and_the_date_order(store: LocalStore) -> None:
    draft = _draft(store)
    mapping = _auto(draft)
    saved = import_flow.remember_choices(
        store, draft, mapping=mapping, date_order=DateOrder.MDY, back="connections"
    )
    loaded = import_flow.load_draft(store, saved.id, now=NOW)
    assert loaded is not None
    assert loaded.mapping == mapping
    assert loaded.date_order == "mdy"
    assert loaded.back == "connections"
    assert loaded.created_at == draft.created_at  # saving a choice doesn't extend the draft


def test_remember_choices_drops_headers_the_file_does_not_have(store: LocalStore) -> None:
    draft = _draft(store)
    saved = import_flow.remember_choices(
        store, draft, mapping={"name": "Company", "arr": "Gone"}, date_order=None
    )
    assert saved.mapping == {"name": "Company"}


# Excel sheets ------------------------------------------------------------------------------------


def _workbook() -> bytes:
    book = Workbook()
    accounts = book.active
    assert accounts is not None
    accounts.title = "Accounts"
    accounts.append(["Company", "ARR", "Renewal date"])
    accounts.append(["Larkspur Freight", 184000, "2026-12-01"])
    notes = book.create_sheet("Notes")
    notes.append(["Topic", "Detail"])
    notes.append(["Renewal", "Check the contract"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_reread_sheet_switches_the_sheet_and_forgets_the_mapping(store: LocalStore) -> None:
    data = _workbook()
    draft = import_flow.save_draft(
        store, table=importing.read_upload(data, "renewals.xlsx"), file_bytes=data, now=NOW
    )
    draft = import_flow.remember_choices(store, draft, mapping={"name": "Company"}, date_order=None)
    switched = import_flow.reread_sheet(store, draft, "Notes")
    assert switched.table.sheet == "Notes"
    assert switched.table.headers == ["Topic", "Detail"]
    assert switched.mapping is None
    assert switched.table.sheet_names == ["Accounts", "Notes"]


def test_reread_sheet_refuses_a_missing_sheet_and_keeps_the_draft(store: LocalStore) -> None:
    data = _workbook()
    draft = import_flow.save_draft(
        store, table=importing.read_upload(data, "renewals.xlsx"), file_bytes=data, now=NOW
    )
    with pytest.raises(importing.ImportError_, match="no sheet called"):
        import_flow.reread_sheet(store, draft, "Missing")
    loaded = import_flow.load_draft(store, draft.id, now=NOW)
    assert loaded is not None
    assert loaded.table.sheet == "Accounts"


def test_a_pasted_draft_cannot_switch_sheets(store: LocalStore) -> None:
    table = importing.read_pasted("Company\tARR\tRenewal date\nAcme\t1\t2027-01-01")
    draft = import_flow.save_draft(store, table=table, pasted_text="x", now=NOW)
    with pytest.raises(importing.ImportError_):
        import_flow.reread_sheet(store, draft, "Accounts")
