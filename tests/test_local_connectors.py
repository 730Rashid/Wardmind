"""ManualConnector and SpreadsheetConnector over the local store."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.local import ManualConnector, SpreadsheetConnector
from cs_brief.core.models import Account, Milestones, Signal, SignalKind
from cs_brief.core.store import ImportRecord, ImportSnapshot, LocalStore, ManualData

NOW = datetime(2026, 10, 21, 9, 30)


def account(acc_id: str, name: str = "Acme") -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=50000.0,
        tier="Growth",
        owner="Dana Whitfield",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
    )


def reading(acc_id: str, kind: SignalKind = SignalKind.NPS_LATEST, value: float = 8.0) -> Signal:
    return Signal(account_id=acc_id, kind=kind, value=value, observed_at=NOW, source="manual")


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "data")


# Manual ---------------------------------------------------------------------------------------


def test_manual_connector_is_empty_before_anything_is_added(store):
    connector = ManualConnector(store)
    assert connector.list_accounts() == []
    assert connector.list_signals() == []
    assert connector.name == "manual"


def test_manual_connector_serves_its_accounts_and_only_their_readings(store):
    store.save_manual(
        ManualData(
            accounts=[account("m-00000001", "First"), account("m-00000002", "Second")],
            signals=[
                reading("m-00000001"),
                reading("m-00000002", SignalKind.CHAMPION_LEFT, 1.0),
                reading("hs-1001"),  # another connection's account: not this connector's
            ],
        )
    )
    connector = ManualConnector(store)
    assert [a.id for a in connector.list_accounts()] == ["m-00000001", "m-00000002"]
    assert [s.account_id for s in connector.list_signals()] == ["m-00000001", "m-00000002"]
    assert [s.account_id for s in connector.list_signals("m-00000002")] == ["m-00000002"]
    assert connector.list_signals("hs-1001") == []


def test_manual_connector_reads_the_store_on_every_call(store):
    connector = ManualConnector(store)
    assert connector.list_accounts() == []
    store.save_manual(ManualData(accounts=[account("m-00000001")]))
    assert [a.id for a in connector.list_accounts()] == ["m-00000001"]


def test_manual_connector_satisfies_the_connector_protocol(store):
    assert isinstance(ManualConnector(store), Connector)


# Spreadsheet ----------------------------------------------------------------------------------


def test_a_spreadsheet_is_empty_before_its_first_import(store):
    connector = SpreadsheetConnector(store, "sheet-1")
    assert connector.list_accounts() == []
    assert connector.list_signals() == []
    assert connector.name == "spreadsheet"


def test_a_spreadsheet_serves_its_latest_accounts_and_accumulated_readings(store):
    store.save_snapshot(
        ImportSnapshot(
            connection_id="sheet-1",
            accounts=[account("sp1-acme", "Acme"), account("sp1-globex", "Globex")],
            signals=[reading("sp1-acme"), reading("sp1-globex", SignalKind.NPS_LATEST, 3.0)],
            history=[ImportRecord(imported_at=NOW, file_name="a.csv", accounts=2, skipped_rows=0)],
        )
    )
    connector = SpreadsheetConnector(store, "sheet-1")
    assert [a.name for a in connector.list_accounts()] == ["Acme", "Globex"]
    assert len(connector.list_signals()) == 2
    assert [s.account_id for s in connector.list_signals("sp1-globex")] == ["sp1-globex"]
    assert connector.list_signals("sp1-missing") == []


def test_a_new_import_replaces_the_accounts_the_connector_serves(store):
    store.save_snapshot(ImportSnapshot(connection_id="sheet-1", accounts=[account("sp1-acme")]))
    connector = SpreadsheetConnector(store, "sheet-1")
    assert [a.id for a in connector.list_accounts()] == ["sp1-acme"]
    store.save_snapshot(ImportSnapshot(connection_id="sheet-1", accounts=[account("sp1-globex")]))
    assert [a.id for a in connector.list_accounts()] == ["sp1-globex"]


def test_each_spreadsheet_only_sees_its_own_snapshot(store):
    store.save_snapshot(ImportSnapshot(connection_id="sheet-1", accounts=[account("sp1-acme")]))
    store.save_snapshot(ImportSnapshot(connection_id="sheet-2", accounts=[account("sp2-globex")]))
    assert [a.id for a in SpreadsheetConnector(store, "sheet-1").list_accounts()] == ["sp1-acme"]
    assert [a.id for a in SpreadsheetConnector(store, "sheet-2").list_accounts()] == ["sp2-globex"]


def test_a_spreadsheet_connector_satisfies_the_connector_protocol(store):
    assert isinstance(SpreadsheetConnector(store, "sheet-1"), Connector)
