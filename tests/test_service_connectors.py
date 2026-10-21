"""service.get_connector(): demo data, and "Your data" built from the local store's connections.

The HubSpot client and connector are replaced by stubs, so no test talks to HubSpot or reads the
real HubSpot code. The token store and the local data are the temp folders from conftest.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import pytest

from cs_brief.core import secrets, service
from cs_brief.core.connectors.combined import CombinedConnector, ConnectionProblem
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectorError
from cs_brief.core.connectors.hubspot import API_BASE, HubSpotMapping
from cs_brief.core.models import Account, Milestones
from cs_brief.core.settings import DataSource, Settings, save_settings
from cs_brief.core.store import (
    ConnectionKind,
    ImportSnapshot,
    ManualData,
)

TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"
OTHER = "pat-na1-OTHERTOKEN-0000-1111-2222-333344445555"
NO_TOKEN_MESSAGE = (
    "HubSpot is connected, but no access token is saved. Add one on the Connections page."
)
BAD_BASE_MESSAGE = "CS_BRIEF_HUBSPOT_API_BASE must point at 127.0.0.1 or localhost."
LOCAL_OK = ("http://127.0.0.1", "http://localhost", API_BASE)


def _account(acc_id: str, name: str) -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=50000.0,
        tier="Growth",
        owner="Dana Whitfield",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
        domain=None,
    )


class _Built:
    """Every stub client and connector the service made, in order."""

    def __init__(self) -> None:
        self.clients: list[Any] = []
        self.connectors: list[Any] = []


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> _Built:
    record = _Built()

    class FakeClient:
        def __init__(self, token: str, *, base_url: str = API_BASE, **_: Any) -> None:
            if not base_url.startswith(LOCAL_OK):
                # The real client refuses anything but API_BASE or localhost, with ValueError.
                raise ValueError("base_url must be API_BASE or a localhost URL")
            self.token = token
            self.base_url = base_url
            record.clients.append(self)

    class FakeConnector:
        name = "hubspot"

        def __init__(self, client: Any, mapping: Any = None, **_: Any) -> None:
            self.client = client
            self.mapping = mapping
            record.connectors.append(self)

        def list_accounts(self) -> list[Account]:
            return [_account("hs-1", "Acme from HubSpot")]

        def list_signals(self, account_id: str | None = None) -> list[Any]:
            return []

    monkeypatch.setattr(service, "HubSpotClient", FakeClient)
    monkeypatch.setattr(service, "HubSpotConnector", FakeConnector)
    monkeypatch.setattr(service, "load_mapping", lambda: HubSpotMapping())
    return record


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))


def _connect_hubspot() -> None:
    """A saved token and the HubSpot connection, as the Connections page leaves them."""
    secrets.save_hubspot_token(TOKEN)
    service.get_store().add_connection(ConnectionKind.HUBSPOT, "HubSpot")


def test_demo_gives_the_demo_connector(built: _Built) -> None:
    _use(DataSource.DEMO)
    assert isinstance(service.get_connector(), DemoConnector)
    assert built.connectors == []
    assert service.get_store().connections() == []  # demo never touches the local store


def test_your_data_with_nothing_connected_is_an_empty_book(built: _Built) -> None:
    _use(DataSource.MINE)
    connector = service.get_connector()
    assert isinstance(connector, CombinedConnector)
    assert connector.list_accounts() == []
    assert connector.problems == []
    assert built.connectors == []


def test_each_kind_of_connection_contributes_its_accounts_in_order(built: _Built) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    store = service.get_store()
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Q4 renewals")
    store.save_snapshot(
        ImportSnapshot(connection_id=sheet.id, accounts=[_account("sp1-globex", "Globex")])
    )
    store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    store.save_manual(ManualData(accounts=[_account("m-00000001", "Hand added")]))

    connector = service.get_connector()

    assert [a.id for a in connector.list_accounts()] == ["hs-1", "sp1-globex", "m-00000001"]
    assert connector.problems == []
    assert len(built.connectors) == 1


def test_a_disabled_connection_is_left_out(built: _Built) -> None:
    _use(DataSource.MINE)
    store = service.get_store()
    config = store.add_connection(ConnectionKind.MANUAL, "Added by hand")
    store.save_manual(ManualData(accounts=[_account("m-00000001", "Hand added")]))
    store.update_connection(config.model_copy(update={"enabled": False}))

    assert service.get_connector().list_accounts() == []


def test_hubspot_without_a_token_is_a_problem_and_the_rest_still_loads(built: _Built) -> None:
    _use(DataSource.MINE)
    store = service.get_store()
    store.add_connection(ConnectionKind.HUBSPOT, "HubSpot")  # connected, but no token saved
    sheet = store.add_connection(ConnectionKind.SPREADSHEET, "Q4 renewals")
    store.save_snapshot(
        ImportSnapshot(connection_id=sheet.id, accounts=[_account("sp1-globex", "Globex")])
    )

    connector = service.get_connector()

    assert [a.id for a in connector.list_accounts()] == ["sp1-globex"]
    assert connector.problems == [
        ConnectionProblem(connection_id="hubspot", name="HubSpot", message=NO_TOKEN_MESSAGE)
    ]
    assert built.connectors == []


def test_hubspot_builds_the_client_and_connector_with_the_saved_token(built: _Built) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    connector = service.get_connector()
    assert [a.id for a in connector.list_accounts()] == ["hs-1"]
    assert built.clients[0].token == TOKEN
    assert built.clients[0].base_url == API_BASE
    assert built.connectors[0].mapping == HubSpotMapping()


def test_the_environment_token_is_used_too(built: _Built, monkeypatch) -> None:
    _use(DataSource.MINE)
    store = service.get_store()
    store.add_connection(ConnectionKind.HUBSPOT, "HubSpot")
    monkeypatch.setenv(secrets.ENV_TOKEN, TOKEN)
    service.get_connector().list_accounts()
    assert built.clients[0].token == TOKEN


def test_one_hubspot_connector_is_shared_across_calls(built: _Built) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    first = service.get_connector()
    second = service.get_connector()
    assert first is not second  # each call makes a fresh combined book...
    assert len(built.connectors) == 1  # ...over one shared HubSpot connector


def test_the_cache_key_holds_a_hash_and_never_the_token(built: _Built) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    service.get_connector().list_accounts()
    (key,) = service._connectors
    assert hashlib.sha256(TOKEN.encode("utf-8")).hexdigest() == key[0]
    assert all(TOKEN not in part for part in key)
    assert TOKEN not in repr(service._connectors)


def test_a_token_change_creates_a_new_connector(built: _Built) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    service.get_connector().list_accounts()
    secrets.save_hubspot_token(OTHER)
    service.get_connector().list_accounts()
    assert len(built.connectors) == 2
    assert built.clients[-1].token == OTHER


def test_a_mapping_change_creates_a_new_connector(built: _Built, monkeypatch) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    service.get_connector().list_accounts()
    monkeypatch.setattr(service, "load_mapping", lambda: HubSpotMapping(arr="other_arr"))
    service.get_connector().list_accounts()
    assert len(built.connectors) == 2
    assert built.connectors[-1].mapping.arr == "other_arr"


def test_reset_forgets_the_shared_connectors(built: _Built) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    service.get_connector().list_accounts()
    service.reset_connectors()
    assert service._connectors == {}
    service.get_connector().list_accounts()
    assert len(built.connectors) == 2


def test_a_bad_base_url_is_a_problem_and_is_not_cached(built: _Built, monkeypatch) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    monkeypatch.setenv("CS_BRIEF_HUBSPOT_API_BASE", "https://evil.example")
    connector = service.get_connector()
    assert connector.list_accounts() == []
    assert [problem.message for problem in connector.problems] == [BAD_BASE_MESSAGE]
    assert TOKEN not in BAD_BASE_MESSAGE
    assert service._connectors == {}


def test_the_test_base_url_is_passed_through(built: _Built, monkeypatch) -> None:
    _use(DataSource.MINE)
    _connect_hubspot()
    monkeypatch.setenv("CS_BRIEF_HUBSPOT_API_BASE", "http://127.0.0.1:9999")
    service.get_connector().list_accounts()
    assert built.clients[0].base_url == "http://127.0.0.1:9999"
    assert service.hubspot_api_base() == "http://127.0.0.1:9999"


def test_a_mapping_error_reaches_the_caller_as_it_is(built: _Built, monkeypatch) -> None:
    """A problem with config/hubspot.yaml is about the file, not one connection, so it is raised."""
    _use(DataSource.MINE)
    _connect_hubspot()

    def broken():
        raise ConnectorError("config/hubspot.yaml has a value that isn't a number.")

    monkeypatch.setattr(service, "load_mapping", broken)
    with pytest.raises(ConnectorError) as caught:
        service.get_connector()
    assert caught.value.message == "config/hubspot.yaml has a value that isn't a number."
    assert built.connectors == []


def test_a_saved_token_with_no_connections_adds_the_hubspot_connection_once(
    built: _Built,
) -> None:
    _use(DataSource.MINE)
    secrets.save_hubspot_token(TOKEN)

    connector = service.get_connector()

    assert [c.id for c in service.get_store().connections()] == ["hubspot"]
    assert [a.id for a in connector.list_accounts()] == ["hs-1"]
    service.get_connector()
    assert len(service.get_store().connections()) == 1


def test_no_migration_when_the_store_already_has_connections(built: _Built) -> None:
    _use(DataSource.MINE)
    secrets.save_hubspot_token(TOKEN)
    service.get_store().add_connection(ConnectionKind.SPREADSHEET, "Q4 renewals")

    connector = service.get_connector()

    assert [c.id for c in service.get_store().connections()] == ["sheet-1"]
    assert connector.problems == []


def test_demo_never_migrates_or_adds_connections(built: _Built) -> None:
    _use(DataSource.DEMO)
    secrets.save_hubspot_token(TOKEN)
    service.get_connector()
    assert service.get_store().connections() == []


def test_get_combined_is_the_your_data_connector_or_none(built: _Built) -> None:
    _use(DataSource.DEMO)
    assert service.get_combined() is None
    _use(DataSource.MINE)
    assert isinstance(service.get_combined(), CombinedConnector)


def test_hubspot_connector_is_the_one_the_book_reads(built: _Built) -> None:
    _use(DataSource.MINE)
    assert service.hubspot_connector() is None  # no token and no connections
    secrets.save_hubspot_token(TOKEN)
    # A token with no connections yet is the older HubSpot-only setting, so it is migrated.
    assert service.hubspot_connector() is built.connectors[0]
    assert [c.id for c in service.get_store().connections()] == ["hubspot"]


def test_hubspot_connector_is_none_when_only_spreadsheets_are_connected(built: _Built) -> None:
    _use(DataSource.MINE)
    secrets.save_hubspot_token(TOKEN)
    service.get_store().add_connection(ConnectionKind.SPREADSHEET, "Q4 renewals")
    assert service.hubspot_connector() is None
    assert built.connectors == []


def test_hubspot_connector_is_none_without_a_token(built: _Built) -> None:
    _use(DataSource.MINE)
    service.get_store().add_connection(ConnectionKind.HUBSPOT, "HubSpot")
    assert service.hubspot_connector() is None
    assert built.connectors == []


def test_the_default_base_is_hubspots_own_host(monkeypatch) -> None:
    monkeypatch.delenv("CS_BRIEF_HUBSPOT_API_BASE", raising=False)
    assert service.hubspot_api_base() == API_BASE
