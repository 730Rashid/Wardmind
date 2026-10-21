"""LocalStore: file round trips, tolerant reads, sheet numbering, removal cascades and clear_all."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from cs_brief.core.models import Account, Milestones, Signal, SignalKind
from cs_brief.core.secrets import ENV_SECRETS_DIR
from cs_brief.core.store import (
    ENV_DATA_DIR,
    ConnectionKind,
    FieldEdit,
    ImportRecord,
    ImportSnapshot,
    LinkData,
    LinkGroup,
    LocalStore,
    ManualData,
    connection_for,
    data_dir,
)

NOW = datetime(2026, 10, 21, 9, 30)


def account(acc_id: str, name: str = "Acme", domain: str | None = "acme.com") -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=120000.0,
        tier="Enterprise",
        owner="Dana Whitfield",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=4, total=6, overdue=1),
        domain=domain,
    )


def reading(acc_id: str, kind: SignalKind = SignalKind.NPS_LATEST, value: float = 8.0) -> Signal:
    return Signal(account_id=acc_id, kind=kind, value=value, observed_at=NOW, source="manual")


def edit(value, base=None) -> FieldEdit:
    return FieldEdit(value=value, base_value=base, edited_at=NOW)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


def _names(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir())


# Folder choice --------------------------------------------------------------------------------


def test_the_env_override_sets_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "custom"))
    assert data_dir() == tmp_path / "custom"


def test_without_the_override_the_data_dir_sits_under_the_secrets_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_DATA_DIR, raising=False)
    monkeypatch.setenv(ENV_SECRETS_DIR, str(tmp_path / "secrets"))
    assert data_dir() == tmp_path / "secrets" / "data"


def test_the_store_root_is_the_temp_dir_from_conftest():
    root = LocalStore().root
    assert root == Path(os.environ[ENV_DATA_DIR])
    assert root == data_dir()


def test_constructing_a_store_creates_nothing(tmp_path):
    root = tmp_path / "never-made"
    LocalStore(root).connections()
    assert not root.exists()


# Empty and round trips ------------------------------------------------------------------------


def test_an_empty_store_reads_as_empty(store):
    assert store.connections() == []
    assert store.manual() == ManualData()
    assert store.snapshot("sheet-1") is None
    assert store.edits() == {}
    assert store.notices() == []
    assert store.links() == LinkData()
    assert store.hidden() == []


def test_connections_round_trip_and_are_written_with_indent_two(store):
    config = store.add_connection(ConnectionKind.HUBSPOT, "ignored")
    assert store.connections() == [config]
    assert store.get_connection("hubspot") == config
    text = (store.root / "connections.json").read_text(encoding="utf-8")
    assert text.startswith('[\n  {\n    "id": "hubspot"')
    assert text.endswith("\n")


def test_manual_data_round_trips(store):
    data = ManualData(
        accounts=[account("m-1a2b3c4d", "Hand Co", None)],
        signals=[reading("m-1a2b3c4d"), reading("hs-1001", SignalKind.USAGE_TREND_30D, -12.5)],
    )
    store.save_manual(data)
    assert store.manual() == data


def test_a_snapshot_round_trips_under_sheets(store):
    snapshot = ImportSnapshot(
        connection_id="sheet-1",
        accounts=[account("sp1-acme")],
        signals=[reading("sp1-acme")],
        history=[
            ImportRecord(imported_at=NOW, file_name="renewals.xlsx", accounts=1, skipped_rows=0)
        ],
    )
    store.save_snapshot(snapshot)
    assert store.snapshot("sheet-1") == snapshot
    assert (store.root / "sheets" / "sheet-1.json").is_file()


def test_edits_round_trip_with_their_types(store):
    store.set_edit("hs-1", "arr", edit(135000.0, 120000.0))
    store.set_edit("hs-1", "milestones_total", edit(7, 6))
    store.set_edit("hs-1", "renewal_date", edit(date(2028, 1, 1), date(2027, 1, 1)))
    store.set_edit("hs-1", "start_date", edit(date(2023, 6, 1), None))
    store.set_edit("hs-1", "domain", edit(None, "acme.com"))
    store.set_edit("hs-1", "name", edit("Acme Ltd", "Acme"))

    loaded = store.edits()["hs-1"]
    assert loaded["arr"] == edit(135000.0, 120000.0)
    assert isinstance(loaded["arr"].value, float)
    assert isinstance(loaded["milestones_total"].value, int)
    assert loaded["milestones_total"].value == 7
    assert loaded["renewal_date"].value == date(2028, 1, 1)
    assert loaded["renewal_date"].base_value == date(2027, 1, 1)
    assert loaded["start_date"].base_value is None
    assert loaded["domain"].value is None
    assert loaded["name"].value == "Acme Ltd"


def test_notices_round_trip_and_dismiss_hides_them_by_default(store):
    first = store.add_notice("hs-1", "arr", "HubSpot changed ARR to $140,000.")
    second = store.add_notice("hs-2", "tier", "HubSpot changed Tier to Growth.")
    assert store.notices("hs-1") == [first]
    assert store.notices() == [first, second]
    store.dismiss_notice(first.id)
    assert store.notices("hs-1") == []
    assert store.notices("hs-1", include_dismissed=True) == [
        first.model_copy(update={"dismissed": True})
    ]
    store.dismiss_notice("nope0000")  # unknown ids change nothing
    assert len(store.notices(include_dismissed=True)) == 2


def test_remove_notices_deletes_one_accounts_notices_dismissed_ones_included(store):
    keep = store.add_notice("hs-2", "tier", "a notice for another account")
    store.add_notice("hs-1", "arr", "first")
    dismissed = store.add_notice("hs-1", "tier", "second")
    store.dismiss_notice(dismissed.id)

    assert store.remove_notices("hs-1") == 2
    assert store.notices(include_dismissed=True) == [keep]
    assert store.remove_notices("hs-1") == 0  # nothing left to remove, nothing written


def test_remove_notices_on_an_empty_store_creates_no_file(store):
    assert store.remove_notices("hs-1") == 0
    assert not (store.root / "notices.json").exists()


def test_move_notices_rekeys_them_and_keeps_id_time_and_dismissed(store):
    first = store.add_notice("hs-1", "arr", "HubSpot changed ARR.")
    dismissed = store.add_notice("hs-1", "tier", "HubSpot changed Tier.")
    store.dismiss_notice(dismissed.id)
    other = store.add_notice("hs-2", "arr", "a notice for another account")

    assert store.move_notices("hs-1", "sp1-acme") == 2
    assert store.notices("hs-1", include_dismissed=True) == []
    assert store.notices("sp1-acme", include_dismissed=True) == [
        first.model_copy(update={"account_id": "sp1-acme"}),
        dismissed.model_copy(update={"account_id": "sp1-acme", "dismissed": True}),
    ]
    assert store.notices("hs-2") == [other]
    assert store.move_notices("hs-1", "sp1-acme") == 0  # already moved


def test_move_notices_on_an_empty_store_creates_no_file(store):
    assert store.move_notices("hs-1", "sp1-acme") == 0
    assert not (store.root / "notices.json").exists()


def test_links_round_trip_including_dismissed_pairs(store):
    data = LinkData(
        groups=[LinkGroup(primary="sp1-acme", members=["sp1-acme", "hs-1001"])],
        dismissed=[("hs-1002", "sp1-other")],
    )
    store.save_links(data)
    assert store.links() == data


def test_hidden_round_trips(store):
    store.set_hidden("hs-1", True)
    store.set_hidden("acc-001", True)
    assert store.hidden() == ["hs-1", "acc-001"]


def test_set_hidden_is_idempotent_and_can_show_again(store):
    store.set_hidden("hs-1", True)
    store.set_hidden("hs-1", True)
    assert store.hidden() == ["hs-1"]
    store.set_hidden("hs-1", False)
    store.set_hidden("hs-1", False)
    assert store.hidden() == []
    assert not (store.root / "hidden.json").read_text(encoding="utf-8").count("hs-1")


def test_set_hidden_false_on_a_never_hidden_account_writes_nothing(store):
    store.set_hidden("hs-9", False)
    assert not (store.root / "hidden.json").exists()


def test_set_edit_rejects_a_field_that_cannot_be_edited(store):
    with pytest.raises(ValueError):
        store.set_edit("hs-1", "cs_arr", edit(1.0, 0.0))


def test_clear_edit_removes_the_field_and_then_the_account(store):
    store.set_edit("hs-1", "arr", edit(135000.0, 120000.0))
    store.set_edit("hs-1", "tier", edit("Growth", "Enterprise"))
    store.clear_edit("hs-1", "arr")
    assert list(store.edits()["hs-1"]) == ["tier"]
    store.clear_edit("hs-1", "tier")
    assert store.edits() == {}
    store.clear_edit("hs-1", "tier")  # clearing twice is fine
    store.clear_edit("hs-404", "arr")


# Unreadable files -----------------------------------------------------------------------------

_BAD_CONTENTS = [
    "{not json",
    '[{"id": "hubspot"',  # truncated
    '"just a string"',
    "[1, 2, 3]",
    '{"unexpected": true}',
]


@pytest.mark.parametrize("content", _BAD_CONTENTS)
@pytest.mark.parametrize(
    ("file_name", "read", "empty"),
    [
        ("connections.json", lambda s: s.connections(), []),
        ("manual.json", lambda s: s.manual(), ManualData()),
        ("edits.json", lambda s: s.edits(), {}),
        ("notices.json", lambda s: s.notices(include_dismissed=True), []),
        ("links.json", lambda s: s.links(), LinkData()),
        ("hidden.json", lambda s: s.hidden(), []),
    ],
)
def test_a_corrupt_or_wrong_shaped_file_reads_as_empty(store, file_name, read, empty, content):
    store.root.mkdir(parents=True)
    (store.root / file_name).write_text(content, encoding="utf-8")
    assert read(store) == empty


def test_a_corrupt_snapshot_reads_as_none(store):
    (store.root / "sheets").mkdir(parents=True)
    (store.root / "sheets" / "sheet-1.json").write_text("{nope", encoding="utf-8")
    assert store.snapshot("sheet-1") is None


def test_a_connection_entry_of_the_wrong_shape_empties_the_whole_list(store):
    store.root.mkdir(parents=True)
    (store.root / "connections.json").write_text('[{"id": 3}]', encoding="utf-8")
    assert store.connections() == []


def test_the_next_write_replaces_a_corrupt_file(store):
    store.root.mkdir(parents=True)
    (store.root / "hidden.json").write_text("garbage", encoding="utf-8")
    store.set_hidden("hs-1", True)
    assert json.loads((store.root / "hidden.json").read_text(encoding="utf-8")) == ["hs-1"]


def test_unreadable_bytes_read_as_empty(store):
    store.root.mkdir(parents=True)
    (store.root / "links.json").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
    assert store.links() == LinkData()


# Connections ----------------------------------------------------------------------------------


def test_hubspot_and_manual_have_fixed_ids_and_names(store):
    hubspot = store.add_connection(ConnectionKind.HUBSPOT, "Whatever the caller said")
    assert (hubspot.id, hubspot.name, hubspot.kind) == (
        "hubspot",
        "HubSpot",
        ConnectionKind.HUBSPOT,
    )
    manual = store.add_connection(ConnectionKind.MANUAL, "Other")
    assert (manual.id, manual.name, manual.kind) == (
        "manual",
        "Added by hand",
        ConnectionKind.MANUAL,
    )
    assert store.add_connection(ConnectionKind.HUBSPOT, "Again") == hubspot
    assert store.add_connection(ConnectionKind.MANUAL, "Again") == manual
    assert [c.id for c in store.connections()] == ["hubspot", "manual"]


def test_a_new_spreadsheet_keeps_its_name_and_starts_at_sheet_one(store):
    first = store.add_connection(ConnectionKind.SPREADSHEET, "  Q4 renewals sheet ")
    assert first.id == "sheet-1"
    assert first.name == "Q4 renewals sheet"
    assert first.kind is ConnectionKind.SPREADSHEET
    assert first.enabled is True
    assert first.created_at.tzinfo is None


def test_spreadsheet_numbers_count_up(store):
    ids = [store.add_connection(ConnectionKind.SPREADSHEET, f"Sheet {n}").id for n in range(3)]
    assert ids == ["sheet-1", "sheet-2", "sheet-3"]


def test_a_removed_middle_sheet_number_is_not_handed_out_again(store):
    for n in range(3):
        store.add_connection(ConnectionKind.SPREADSHEET, f"Sheet {n}")
    store.remove_connection("sheet-2")
    # Counting connections would give sheet-3 again, which is still in use.
    assert store.add_connection(ConnectionKind.SPREADSHEET, "Next").id == "sheet-4"


def test_a_snapshot_left_without_its_connection_keeps_its_number(store):
    store.save_snapshot(ImportSnapshot(connection_id="sheet-5"))
    assert store.add_connection(ConnectionKind.SPREADSHEET, "Next").id == "sheet-6"


def test_update_connection_replaces_by_id(store):
    config = store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    changed = config.model_copy(update={"last_file_name": "renewals.xlsx", "enabled": False})
    store.update_connection(changed)
    assert store.get_connection("sheet-1") == changed
    assert len(store.connections()) == 1


def test_update_connection_does_not_bring_back_a_removed_one(store):
    config = store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    store.remove_connection(config.id)
    store.update_connection(config)
    assert store.connections() == []


def test_remove_connection_cascades_for_a_spreadsheet(store):
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Renewals")
    hubspot = store.add_connection(ConnectionKind.HUBSPOT, "")
    store.add_connection(ConnectionKind.MANUAL, "")
    store.save_snapshot(ImportSnapshot(connection_id=sheet.id, accounts=[account("sp1-acme")]))
    store.set_edit("sp1-acme", "arr", edit(135000.0, 120000.0))
    store.set_edit("hs-1001", "arr", edit(99000.0, 120000.0))
    store.add_notice("sp1-acme", "arr", "a notice for the sheet account")
    keep_notice = store.add_notice("hs-1001", "arr", "a notice for HubSpot")
    store.set_hidden("sp1-acme", True)
    store.set_hidden("hs-1001", True)
    store.save_links(
        LinkData(
            groups=[
                # The primary is removed, so the next member takes over and the group stays.
                LinkGroup(primary="sp1-acme", members=["sp1-acme", "hs-1001", "hs-1002"]),
                # Only one member is left, so the group goes.
                LinkGroup(primary="sp1-x", members=["sp1-x", "m-0000aaaa"]),
            ],
            dismissed=[("sp1-acme", "hs-9999"), ("hs-1001", "hs-9999")],
        )
    )
    store.save_manual(
        ManualData(signals=[reading("sp1-acme"), reading("m-0000aaaa"), reading("hs-1001")])
    )

    store.remove_connection(sheet.id)

    assert store.get_connection(sheet.id) is None
    assert store.get_connection(hubspot.id) is not None
    assert store.snapshot(sheet.id) is None
    assert not (store.root / "sheets" / "sheet-1.json").exists()
    assert store.edits() == {"hs-1001": {"arr": edit(99000.0, 120000.0)}}
    assert store.notices(include_dismissed=True) == [keep_notice]
    assert store.hidden() == ["hs-1001"]
    assert store.links() == LinkData(
        groups=[LinkGroup(primary="hs-1001", members=["hs-1001", "hs-1002"])],
        dismissed=[("hs-1001", "hs-9999")],
    )
    assert [s.account_id for s in store.manual().signals] == ["m-0000aaaa", "hs-1001"]


def test_removing_manual_clears_its_accounts_and_readings(store):
    store.add_connection(ConnectionKind.MANUAL, "")
    store.save_manual(ManualData(accounts=[account("m-1a2b3c4d")], signals=[reading("m-1a2b3c4d")]))
    store.remove_connection("manual")
    assert store.manual() == ManualData()
    assert store.get_connection("manual") is None


def test_removing_an_unknown_connection_changes_nothing(store):
    store.set_hidden("hs-1", True)
    store.remove_connection("sheet-99")
    assert store.hidden() == ["hs-1"]


def test_clear_all_deletes_every_file_and_the_sheets_folder(store):
    store.add_connection(ConnectionKind.HUBSPOT, "")
    store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    store.save_manual(ManualData(accounts=[account("m-1a2b3c4d")]))
    store.set_edit("hs-1", "arr", edit(1.0, 0.0))
    store.add_notice("hs-1", "arr", "note")
    store.save_links(LinkData(dismissed=[("hs-1", "hs-2")]))
    store.set_hidden("hs-1", True)
    store.save_snapshot(ImportSnapshot(connection_id="sheet-1"))
    store.clear_all()

    assert not store.root.joinpath("sheets").exists()
    for name in (
        "connections.json",
        "manual.json",
        "edits.json",
        "notices.json",
        "links.json",
        "hidden.json",
    ):
        assert not (store.root / name).exists()
    assert store.connections() == [] and store.hidden() == [] and store.snapshot("sheet-1") is None


def test_writes_leave_no_temporary_files(store):
    store.add_connection(ConnectionKind.HUBSPOT, "")
    store.add_connection(ConnectionKind.SPREADSHEET, "Sheet")
    store.save_manual(ManualData(accounts=[account("m-1a2b3c4d")]))
    store.set_edit("hs-1", "arr", edit(1.0, 0.0))
    store.add_notice("hs-1", "arr", "note")
    store.save_links(LinkData())
    store.set_hidden("hs-1", True)
    store.save_snapshot(ImportSnapshot(connection_id="sheet-1"))
    assert list(store.root.rglob("*.tmp")) == []
    assert _names(store.root) == [
        "connections.json",
        "edits.json",
        "hidden.json",
        "links.json",
        "manual.json",
        "notices.json",
        "sheets",
    ]


def test_notice_ids_are_eight_hex_characters_and_unique(store):
    ids = [store.add_notice("hs-1", "arr", f"note {n}").id for n in range(50)]
    assert all(re.fullmatch(r"[0-9a-f]{8}", notice_id) for notice_id in ids)
    assert len(set(ids)) == 50


@pytest.mark.parametrize(
    ("account_id", "expected"),
    [
        ("hs-1001", "hubspot"),
        ("m-1a2b3c4d", "manual"),
        ("sp3-acme", "sheet-3"),
        ("sp12-0042", "sheet-12"),
        ("acc-001", None),
        ("sheet-2", None),
        ("sp-acme", None),
        ("sp0-acme", None),
        ("", None),
    ],
)
def test_connection_for_reads_the_id_prefix(account_id, expected):
    assert connection_for(account_id) == expected
