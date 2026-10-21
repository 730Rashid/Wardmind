"""service.get_connector(): which connector each source gives, and when a HubSpot one is shared.

The HubSpot client and connector are replaced by stubs, so no test talks to HubSpot or reads the
real HubSpot code. The token store is the temp folder from conftest.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from cs_brief.core import secrets, service
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectionNotReady, ConnectorError
from cs_brief.core.connectors.hubspot import API_BASE, HubSpotMapping
from cs_brief.core.settings import DataSource, Settings, save_settings

TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"
OTHER = "pat-na1-OTHERTOKEN-0000-1111-2222-333344445555"
NO_TOKEN_MESSAGE = (
    "HubSpot is selected, but no access token is saved. "
    "Add one on the Connections page, or switch to demo data."
)
BAD_BASE_MESSAGE = "CS_BRIEF_HUBSPOT_API_BASE must point at 127.0.0.1 or localhost."
LOCAL_OK = ("http://127.0.0.1", "http://localhost", API_BASE)


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

    monkeypatch.setattr(service, "HubSpotClient", FakeClient)
    monkeypatch.setattr(service, "HubSpotConnector", FakeConnector)
    monkeypatch.setattr(service, "load_mapping", lambda: HubSpotMapping())
    return record


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))


def test_demo_gives_the_demo_connector(built: _Built) -> None:
    _use(DataSource.DEMO)
    assert isinstance(service.get_connector(), DemoConnector)
    assert built.connectors == []


def test_hubspot_without_a_token_is_not_ready(built: _Built) -> None:
    _use(DataSource.HUBSPOT)
    with pytest.raises(ConnectionNotReady) as caught:
        service.get_connector()
    assert caught.value.message == NO_TOKEN_MESSAGE
    assert built.clients == [] and built.connectors == []


def test_hubspot_builds_the_client_and_connector_with_the_saved_token(built: _Built) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    connector = service.get_connector()
    assert connector is built.connectors[0]
    assert built.clients[0].token == TOKEN
    assert built.clients[0].base_url == API_BASE
    assert connector.mapping == HubSpotMapping()


def test_the_environment_token_is_used_too(built: _Built, monkeypatch) -> None:
    _use(DataSource.HUBSPOT)
    monkeypatch.setenv(secrets.ENV_TOKEN, TOKEN)
    service.get_connector()
    assert built.clients[0].token == TOKEN


def test_one_connector_is_shared_for_the_same_key(built: _Built) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    first = service.get_connector()
    second = service.get_connector()
    assert first is second
    assert len(built.connectors) == 1


def test_the_cache_key_holds_a_hash_and_never_the_token(built: _Built) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    service.get_connector()
    (key,) = service._connectors
    assert hashlib.sha256(TOKEN.encode("utf-8")).hexdigest() == key[0]
    assert all(TOKEN not in part for part in key)
    assert TOKEN not in repr(service._connectors)


def test_a_token_change_creates_a_new_connector(built: _Built) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    first = service.get_connector()
    secrets.save_hubspot_token(OTHER)
    second = service.get_connector()
    assert second is not first
    assert built.clients[-1].token == OTHER


def test_a_mapping_change_creates_a_new_connector(built: _Built, monkeypatch) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    first = service.get_connector()
    monkeypatch.setattr(service, "load_mapping", lambda: HubSpotMapping(arr="other_arr"))
    second = service.get_connector()
    assert second is not first
    assert second.mapping.arr == "other_arr"


def test_reset_forgets_the_shared_connectors(built: _Built) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    first = service.get_connector()
    service.reset_connectors()
    assert service._connectors == {}
    assert service.get_connector() is not first


def test_a_bad_base_url_is_not_ready_and_is_not_cached(built: _Built, monkeypatch) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    monkeypatch.setenv("CS_BRIEF_HUBSPOT_API_BASE", "https://evil.example")
    with pytest.raises(ConnectionNotReady) as caught:
        service.get_connector()
    assert caught.value.message == BAD_BASE_MESSAGE
    assert TOKEN not in caught.value.message
    assert service._connectors == {}


def test_the_test_base_url_is_passed_through(built: _Built, monkeypatch) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)
    monkeypatch.setenv("CS_BRIEF_HUBSPOT_API_BASE", "http://127.0.0.1:9999")
    service.get_connector()
    assert built.clients[0].base_url == "http://127.0.0.1:9999"
    assert service.hubspot_api_base() == "http://127.0.0.1:9999"


def test_a_mapping_error_reaches_the_caller_as_it_is(built: _Built, monkeypatch) -> None:
    _use(DataSource.HUBSPOT)
    secrets.save_hubspot_token(TOKEN)

    def broken():
        raise ConnectorError("config/hubspot.yaml has a value that isn't a number.")

    monkeypatch.setattr(service, "load_mapping", broken)
    with pytest.raises(ConnectorError) as caught:
        service.get_connector()
    assert caught.value.message == "config/hubspot.yaml has a value that isn't a number."
    assert built.connectors == []


def test_the_default_base_is_hubspots_own_host(monkeypatch) -> None:
    monkeypatch.delenv("CS_BRIEF_HUBSPOT_API_BASE", raising=False)
    assert service.hubspot_api_base() == API_BASE
