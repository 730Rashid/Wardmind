"""Settings > Connections, and what every page does while HubSpot can't be used.

HubSpot's client, connector and mapping are replaced by stubs, so no test reaches HubSpot or the
real HubSpot code. Tokens are saved in the temp secrets folder from conftest. The token used in
these tests is a made-up value with a distinctive shape, so a leak is easy to spot.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Iterator
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_brief.core import secrets, service
from cs_brief.core.config import ScoringConfig
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectionNotReady, ConnectorError
from cs_brief.core.connectors.hubspot import API_BASE, CheckResult, ConnectionCheck, HubSpotMapping
from cs_brief.core.settings import DataSource, Settings, Theme, load_settings, save_settings
from cs_brief.web import app as web
from cs_brief.web import formatting
from cs_brief.web.scoring_form import form_values

TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"
OTHER = "pat-na1-OTHERTOKEN-0000-1111-2222-333344445555"
ORIGIN = {"origin": "http://127.0.0.1"}
HX = {"origin": "http://127.0.0.1", "HX-Request": "true"}
NO_TOKEN = "Save a HubSpot token first."  # the Test connection button still says this
NO_TOKEN_MESSAGE = (
    "HubSpot is connected, but no access token is saved. Add one on the Connections page."
)
ERROR_HEADING = "Couldn't load your book"
SOURCE_MINE = "Showing your data"
SOURCE_DEMO = "Showing demo data"
EMPTY_TOKEN_INPUT = (
    '<input type="password" name="token" autocomplete="off" spellcheck="false" id="token">'
)


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(markup: str, name: str) -> list[dict[str, str | None]]:
    parser = _Tags()
    parser.feed(markup)
    return [attrs for tag, attrs in parser.tags if tag == name]


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes apostrophes and quotes."""
    return html.unescape(response.text)


def _assert_no_token(response: Any) -> None:
    """The token, and its distinctive middle, appear in neither body nor headers."""
    for needle in (TOKEN, "SECRETTEST"):
        assert needle not in response.text, f"{needle} in body of {response.request.url}"
        assert needle.encode() not in response.content
        for name, value in response.headers.items():
            assert needle not in value, f"{needle} in header {name}"


class StubHubSpot:
    """Stands in for the HubSpot client, connector and mapping. Each test sets what it needs."""

    def __init__(self) -> None:
        self.check: ConnectionCheck | Exception = ConnectionCheck(
            ok=True,
            checks=[
                CheckResult(name="Token accepted", ok=True, message="HubSpot accepted the token."),
                CheckResult(name="Companies", ok=True, message="128 customer companies found."),
                CheckResult(name="Owners", ok=True, message="12 owners found."),
                CheckResult(name="Tickets", ok=True, message="Open tickets can be read."),
            ],
            missing_properties=[],
        )
        self.mapping_error: str | None = None
        self.loaded_at: float | None = None
        self.refreshes = 0
        self.checks_run = 0
        self.tokens: list[str] = []


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> StubHubSpot:
    stub = StubHubSpot()

    class FakeClient:
        def __init__(self, token: str, *, base_url: str = API_BASE, **_: Any) -> None:
            if not base_url.startswith(("http://127.0.0.1", "http://localhost", API_BASE)):
                raise ValueError("base_url must be API_BASE or a localhost URL")
            self.token = token
            stub.tokens.append(token)

    class FakeConnector:
        name = "hubspot"

        def __init__(self, client: Any, mapping: Any = None, **_: Any) -> None:
            self.client = client

        @property
        def loaded_at(self) -> float | None:
            return stub.loaded_at

        def list_accounts(self) -> list[Any]:
            return DemoConnector().list_accounts()

        def list_signals(self, account_id: str | None = None) -> list[Any]:
            return DemoConnector().list_signals(account_id)

        def refresh(self) -> None:
            stub.refreshes += 1

        def check_connection(self) -> ConnectionCheck:
            stub.checks_run += 1
            if isinstance(stub.check, Exception):
                raise stub.check
            return stub.check

    def load_mapping(*_: Any) -> HubSpotMapping:
        if stub.mapping_error is not None:
            raise ConnectorError(stub.mapping_error)
        return HubSpotMapping()

    for module in (web, service):
        monkeypatch.setattr(module, "HubSpotClient", FakeClient, raising=False)
        monkeypatch.setattr(module, "HubSpotConnector", FakeConnector, raising=False)
        monkeypatch.setattr(module, "load_mapping", load_mapping, raising=False)
    return stub


@pytest.fixture(autouse=True)
def _every_test_uses_the_stub(hub: StubHubSpot) -> StubHubSpot:
    """The page reads the HubSpot mapping, so every test here runs against the stub."""
    return hub


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def secrets_folder() -> Path:
    return Path(os.environ[secrets.ENV_SECRETS_DIR])


@pytest.fixture
def config_folder() -> Path:
    return Path(os.environ["CS_BRIEF_CONFIG_DIR"])


@pytest.fixture
def resets(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts service.reset_connectors() calls, and still performs them."""
    calls: list[int] = []
    original = service.reset_connectors

    def spy() -> None:
        calls.append(1)
        original()

    monkeypatch.setattr(service, "reset_connectors", spy)
    return calls


def _use(source: DataSource, **changes: Any) -> None:
    save_settings(Settings(source=source, **changes))


def _save_token(token: str = TOKEN) -> None:
    secrets.save_hubspot_token(token)


@pytest.fixture
def hubspot_with_token(hub: StubHubSpot) -> StubHubSpot:
    _use(DataSource.MINE)
    _save_token()
    return hub


# The page and its states


def test_the_page_has_its_title_and_the_sub_navigation(client: TestClient) -> None:
    response = client.get("/settings/connections")
    assert response.status_code == 200
    assert "<title>Connections - cs-brief</title>" in response.text
    links = [a for a in _tags(response.text, "a") if a.get("class") == "settings-link"]
    assert [a["href"] for a in links] == [
        "/settings/appearance",
        "/settings/scoring",
        "/settings/connections",
    ]
    assert [a["href"] for a in links if a.get("aria-current") == "page"] == [
        "/settings/connections"
    ]


def test_with_no_token_the_status_says_so_and_there_is_no_remove_button(
    client: TestClient,
) -> None:
    text = _text(client.get("/settings/connections"))
    assert "No token saved." in text
    assert "Remove token" not in text
    assert EMPTY_TOKEN_INPUT in client.get("/settings/connections").text


def test_with_a_saved_token_the_status_names_the_file_and_its_last_four(
    client: TestClient,
) -> None:
    _save_token()
    response = client.get("/settings/connections")
    today = formatting.day_label(date.today(), date.today())
    assert f"Saved on this computer, ends in …5555. Saved {today}." in _text(response)
    assert "Remove token" in response.text
    _assert_no_token(response)


def test_with_the_environment_token_the_status_says_where_it_is_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secrets.ENV_TOKEN, TOKEN)
    response = client.get("/settings/connections")
    assert "Set by CS_BRIEF_HUBSPOT_TOKEN, ends in …5555. Change it where you set it." in _text(
        response
    )
    assert "Remove token" not in response.text
    _assert_no_token(response)


def test_the_token_input_never_has_a_value_attribute(client: TestClient) -> None:
    _save_token()
    (attrs,) = [
        a
        for a in _tags(client.get("/settings/connections").text, "input")
        if a.get("name") == "token"
    ]
    assert "value" not in attrs
    assert attrs["type"] == "password"


def test_the_sub_navigation_counts_connections_as_a_settings_page(client: TestClient) -> None:
    text = client.get("/settings/connections").text
    assert 'href="/settings/connections" aria-current="page"' in text


def test_the_data_source_form_posts_to_the_source_route(client: TestClient) -> None:
    text = client.get("/settings/connections").text
    forms = [(f["method"], f["action"]) for f in _tags(text, "form")]
    assert ("post", "/settings/connections/source") in forms
    radios = {a["value"]: a for a in _tags(text, "input") if a.get("name") == "source"}
    assert set(radios) == {"demo", "mine"}
    assert "checked" in radios["demo"]


def test_the_saved_source_is_the_one_checked(client: TestClient) -> None:
    _use(DataSource.MINE)
    text = client.get("/settings/connections").text
    radios = {a["value"]: a for a in _tags(text, "input") if a.get("name") == "source"}
    assert "checked" in radios["mine"]
    assert "checked" not in radios["demo"]


def test_the_test_button_posts_without_js_and_swaps_with_htmx(client: TestClient) -> None:
    text = client.get("/settings/connections").text
    buttons = [a for a in _tags(text, "button") if a.get("hx-post")]
    assert len(buttons) == 1
    assert buttons[0]["hx-post"] == "/settings/connections/test"
    assert buttons[0]["hx-target"] == "#connection-test"
    assert ("post", "/settings/connections/test") in [
        (f["method"], f["action"]) for f in _tags(text, "form")
    ]
    assert 'id="connection-test"' in text and 'aria-live="polite"' in text


def test_the_data_source_help_is_shown_under_each_choice(client: TestClient) -> None:
    text = _text(client.get("/settings/connections"))
    assert "40 invented accounts. Nothing leaves your computer." in text
    assert "Your connections: HubSpot, spreadsheets and accounts you add by hand." in text


def test_the_token_help_names_the_scopes(client: TestClient) -> None:
    text = _text(client.get("/settings/connections"))
    assert "crm.objects.companies.read" in text
    assert "crm.objects.owners.read" in text
    assert "not in this folder" in text


# Last fetched and refresh


def test_hubspot_with_a_token_shows_when_data_was_last_fetched(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    hubspot_with_token.loaded_at = time.monotonic() - 60
    response = client.get("/settings/connections")
    assert re.search(r"Data last fetched \d\d:\d\d\.", _text(response))
    assert 'action="/settings/connections/refresh"' in response.text
    assert "Refresh data now" in response.text


def test_before_the_first_load_it_says_not_fetched_yet(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    text = _text(client.get("/settings/connections"))
    assert "Not fetched yet." in text
    assert "Refresh data now" in text


def test_the_fetch_line_is_left_out_when_the_connector_cannot_load(client: TestClient) -> None:
    _use(DataSource.MINE)  # no token saved
    text = _text(client.get("/settings/connections"))
    assert "Data last fetched" not in text and "Not fetched yet." not in text
    assert "Refresh data now" not in text
    assert "No token saved." in text


def test_the_fetch_line_is_left_out_on_demo_data(client: TestClient) -> None:
    text = _text(client.get("/settings/connections"))
    assert "Refresh data now" not in text


def test_refresh_fetches_again_and_says_so(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    response = client.post("/settings/connections/refresh", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?refreshed=1"
    assert hubspot_with_token.refreshes == 1
    assert "Data refreshed." in _text(client.get("/settings/connections?refreshed=1"))


def test_refresh_on_demo_data_still_redirects(client: TestClient, hub: StubHubSpot) -> None:
    response = client.post("/settings/connections/refresh", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert hub.refreshes == 0


def test_refresh_with_nothing_connected_still_redirects(
    client: TestClient, hub: StubHubSpot
) -> None:
    _use(DataSource.MINE)
    response = client.post("/settings/connections/refresh", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert hub.refreshes == 0


# What cs-brief reads


def test_the_mapping_table_lists_the_effective_properties(client: TestClient) -> None:
    text = client.get("/settings/connections").text
    assert "What cs-brief reads" in text
    assert "<details" in text
    assert '<th scope="row">Which companies</th>' in text
    assert "<td>lifecyclestage = customer</td>" in text
    assert "<td>cs_arr</td>" in text
    assert "Signal history" in text


def test_a_mapping_error_is_shown_in_place_of_the_table(
    client: TestClient, hub: StubHubSpot
) -> None:
    hub.mapping_error = "config/hubspot.yaml has a value that isn't a number."
    response = client.get("/settings/connections")
    assert response.status_code == 200
    assert "config/hubspot.yaml has a value that isn&#39;t a number." in response.text
    assert "<table" not in response.text.split("What cs-brief reads", 1)[1]


# Saving the data source


def test_choosing_demo_saves_it_and_keeps_the_other_settings(
    client: TestClient, config_folder: Path, resets: list[int]
) -> None:
    _use(DataSource.MINE, theme=Theme.DARK)
    response = client.post(
        "/settings/connections/source",
        data={"source": "demo"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?saved=source"
    saved = load_settings(config_folder / "settings.yaml")
    assert saved.source is DataSource.DEMO
    assert saved.theme is Theme.DARK
    assert resets == [1]
    assert "Data source saved." in _text(client.get("/settings/connections?saved=source"))


def test_choosing_your_data_saves_it(
    client: TestClient, config_folder: Path, resets: list[int]
) -> None:
    _use(DataSource.DEMO, theme=Theme.LIGHT)
    response = client.post(
        "/settings/connections/source",
        data={"source": "mine"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = load_settings(config_folder / "settings.yaml")
    assert saved.source is DataSource.MINE
    assert saved.theme is Theme.LIGHT
    assert resets == [1]


def test_choosing_your_data_is_allowed_with_nothing_connected_yet(
    client: TestClient, config_folder: Path, resets: list[int]
) -> None:
    response = client.post(
        "/settings/connections/source",
        data={"source": "mine"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_settings(config_folder / "settings.yaml").source is DataSource.MINE
    assert resets == [1]
    assert SOURCE_MINE in _text(client.get("/"))


def test_an_unknown_source_is_refused(client: TestClient, config_folder: Path) -> None:
    response = client.post(
        "/settings/connections/source", data={"source": "salesforce"}, headers=ORIGIN
    )
    assert response.status_code == 422
    assert "Choose your data or demo data." in response.text
    assert not (config_folder / "settings.yaml").exists()


def test_saving_the_appearance_keeps_the_data_source(
    client: TestClient, config_folder: Path
) -> None:
    _use(DataSource.MINE)
    response = client.post(
        "/settings/appearance",
        data={"theme": "dark", "accent": "petrol", "density": "compact", "landing": "brief"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    saved = load_settings(config_folder / "settings.yaml")
    assert saved.theme is Theme.DARK
    assert saved.source is DataSource.MINE


def test_a_settings_file_from_before_says_your_data(
    client: TestClient, config_folder: Path
) -> None:
    (config_folder / "settings.yaml").write_text("source: hubspot\n", encoding="utf-8")
    assert SOURCE_MINE in _text(client.get("/settings/appearance"))


# Saving and removing the token


def test_a_valid_token_is_saved_to_the_secrets_folder(
    client: TestClient, secrets_folder: Path, resets: list[int]
) -> None:
    response = client.post(
        "/settings/connections/token", data={"token": TOKEN}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?saved=token"
    assert (secrets_folder / secrets.TOKEN_FILE).read_text(encoding="utf-8") == TOKEN
    assert resets == [1]
    assert "Token saved." in _text(client.get("/settings/connections?saved=token"))


def test_saving_a_token_adds_one_hubspot_connection(client: TestClient) -> None:
    client.post("/settings/connections/token", data={"token": TOKEN}, headers=ORIGIN)
    client.post("/settings/connections/token", data={"token": OTHER}, headers=ORIGIN)
    assert [c.id for c in service.get_store().connections()] == ["hubspot"]


def test_removing_the_token_leaves_the_hubspot_connection_in_place(
    client: TestClient, hub: StubHubSpot
) -> None:
    _save_token()
    client.post("/settings/connections/token", data={"token": OTHER}, headers=ORIGIN)
    client.post("/settings/connections/token/delete", headers=ORIGIN)
    assert [c.id for c in service.get_store().connections()] == ["hubspot"]
    assert secrets.get_hubspot_token() is None


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("   ", "Paste an access token first."),
        (f"{TOKEN} extra", "An access token can't contain spaces or line breaks."),
        ("pat-short", "That doesn't look like a full access token. Check you copied all of it."),
        (
            "x" * (secrets.MAX_TOKEN_LENGTH + 1),
            "That's longer than any HubSpot access token. Check you copied only the token.",
        ),
    ],
)
def test_an_invalid_token_is_refused_with_the_reason_and_an_empty_field(
    client: TestClient, secrets_folder: Path, typed: str, message: str
) -> None:
    response = client.post(
        "/settings/connections/token", data={"token": typed}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 422
    assert message in _text(response)
    assert EMPTY_TOKEN_INPUT in response.text
    assert not (secrets_folder / secrets.TOKEN_FILE).exists()
    if typed.strip():
        assert typed.strip() not in response.text
    assert "SECRETTEST" not in response.text


def test_an_invalid_token_does_not_replace_the_saved_one(
    client: TestClient, secrets_folder: Path
) -> None:
    _save_token(OTHER)
    client.post("/settings/connections/token", data={"token": "bad token"}, headers=ORIGIN)
    assert (secrets_folder / secrets.TOKEN_FILE).read_text(encoding="utf-8") == OTHER


def test_a_token_that_cannot_be_written_says_so_without_the_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(token: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(secrets, "save_hubspot_token", refuse)
    response = client.post("/settings/connections/token", data={"token": TOKEN}, headers=ORIGIN)
    assert response.status_code == 500
    assert "couldn't save the token" in _text(response)
    _assert_no_token(response)


def test_remove_token_deletes_the_file_and_says_so(
    client: TestClient, secrets_folder: Path, resets: list[int]
) -> None:
    _save_token()
    response = client.post(
        "/settings/connections/token/delete", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/connections?removed=token"
    assert not (secrets_folder / secrets.TOKEN_FILE).exists()
    assert resets == [1]
    page = _text(client.get("/settings/connections?removed=token"))
    assert "Token removed." in page
    assert "No token saved." in page


def test_remove_token_leaves_an_environment_token_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secrets.ENV_TOKEN, TOKEN)
    client.post("/settings/connections/token/delete", headers=ORIGIN)
    assert secrets.get_hubspot_token() == TOKEN


# Test connection


def test_the_test_result_is_only_the_partial_for_htmx(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    response = client.post("/settings/connections/test", headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text and "<title>" not in response.text
    assert "check-list" in response.text
    assert "Companies: 128 customer companies found." in _text(response)
    assert response.headers["vary"] == "HX-Request"


def test_the_test_result_is_the_whole_page_without_htmx(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    response = client.post("/settings/connections/test", headers=ORIGIN)
    assert response.status_code == 200
    assert "<title>Connections - cs-brief</title>" in response.text
    assert "check-list" in response.text
    assert response.headers["vary"] == "HX-Request"


def test_each_check_has_an_icon_and_a_hidden_word(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    hubspot_with_token.check = ConnectionCheck(
        ok=False,
        checks=[
            CheckResult(name="Token accepted", ok=True, message="HubSpot accepted the token."),
            CheckResult(
                name="Tickets",
                ok=False,
                message=(
                    'This key is missing the "tickets" scope. Add it in HubSpot and test again.'
                ),
            ),
        ],
        missing_properties=["cs_nps_latest", "cs_seat_utilisation"],
    )
    response = client.post("/settings/connections/test", headers=HX)
    text = _text(response)
    assert response.text.count('href="/static/icons.svg#check"') == 1
    assert response.text.count('href="/static/icons.svg#cross"') == 1
    assert '<span class="visually-hidden">Passed: </span>' in response.text
    assert '<span class="visually-hidden">Failed: </span>' in response.text
    assert 'class="check-item check-item--failed"' in response.text
    assert 'missing the "tickets" scope' in text
    assert "Missing properties: cs_nps_latest, cs_seat_utilisation" in text


def test_no_missing_properties_line_when_all_are_present(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    response = client.post("/settings/connections/test", headers=HX)
    assert "Missing properties" not in response.text


def test_the_test_uses_a_fresh_connector_whichever_source_is_selected(
    client: TestClient, hub: StubHubSpot
) -> None:
    _use(DataSource.DEMO)
    _save_token()
    response = client.post("/settings/connections/test", headers=HX)
    assert response.status_code == 200
    assert hub.tokens == [TOKEN]
    assert hub.checks_run == 1


def test_without_a_token_the_test_says_to_save_one(client: TestClient, hub: StubHubSpot) -> None:
    for headers in (HX, ORIGIN):
        response = client.post("/settings/connections/test", headers=headers)
        assert response.status_code == 200
        assert 'class="alert" role="alert"' in response.text
        assert NO_TOKEN in response.text
    assert hub.checks_run == 0


def test_a_connector_error_is_shown_as_its_plain_message(
    client: TestClient, hubspot_with_token: StubHubSpot
) -> None:
    hubspot_with_token.check = ConnectorError("HubSpot is taking too long to answer. Try again.")
    response = client.post("/settings/connections/test", headers=HX)
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "HubSpot is taking too long to answer. Try again." in response.text


def test_a_bad_base_url_is_shown_as_a_plain_message(
    client: TestClient, hubspot_with_token: StubHubSpot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CS_BRIEF_HUBSPOT_API_BASE", "https://evil.example")
    response = client.post("/settings/connections/test", headers=HX)
    assert response.status_code == 200
    assert "base_url must be API_BASE or a localhost URL" in response.text
    _assert_no_token(response)


# Use demo data


def test_use_demo_switches_the_source_and_keeps_other_settings(
    client: TestClient, config_folder: Path, resets: list[int]
) -> None:
    _use(DataSource.MINE, theme=Theme.DARK)
    response = client.post("/settings/connections/use-demo", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    saved = load_settings(config_folder / "settings.yaml")
    assert saved.source is DataSource.DEMO
    assert saved.theme is Theme.DARK
    assert resets == [1]
    assert SOURCE_DEMO in _text(client.get("/"))


# Errors on every book page


@pytest.fixture
def no_token_hubspot(monkeypatch: pytest.MonkeyPatch) -> None:
    """HubSpot is selected but no token is saved: every book page raises ConnectionNotReady."""
    _use(DataSource.MINE)

    def not_ready(*_: Any, **__: Any) -> None:
        raise ConnectionNotReady(NO_TOKEN_MESSAGE)

    monkeypatch.setattr(service, "get_connector", not_ready)


@pytest.mark.parametrize("path", ["/", "/brief", "/onboarding", "/accounts/x", "/onboarding/x"])
def test_every_book_page_shows_the_error_with_the_way_out(
    client: TestClient, no_token_hubspot: None, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 503
    text = _text(response)
    assert f"<title>{ERROR_HEADING} - cs-brief</title>" in text
    assert f'<h1 class="page-title">{ERROR_HEADING}</h1>' in response.text
    assert f"Couldn't load your book. {NO_TOKEN_MESSAGE}" in text
    assert '<a class="button" href="/settings/connections">Check Connections</a>' in response.text
    forms = [(f["method"], f["action"]) for f in _tags(response.text, "form")]
    assert forms == [("post", "/settings/connections/use-demo")]
    assert "Switch to demo data" in text
    assert all(a.get("aria-current") is None for a in _tags(response.text, "a"))


def test_the_error_page_keeps_the_top_bar_note(client: TestClient, no_token_hubspot: None) -> None:
    assert SOURCE_MINE in _text(client.get("/"))


def test_the_top_bar_note_follows_the_saved_source(client: TestClient) -> None:
    assert SOURCE_DEMO in _text(client.get("/settings/appearance"))
    _use(DataSource.MINE)
    assert SOURCE_MINE in _text(client.get("/settings/connections"))


def test_the_top_bar_note_does_not_call_the_connector(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(DataSource.MINE)

    def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("the top bar must not ask for the connector")

    monkeypatch.setattr(service, "get_connector", boom)
    response = client.get("/settings/appearance")
    assert response.status_code == 200
    assert SOURCE_MINE in _text(response)


def test_the_scoring_page_renders_with_the_error_in_its_alert(
    client: TestClient, no_token_hubspot: None
) -> None:
    response = client.get("/settings/scoring")
    assert response.status_code == 200
    assert 'id="scoring-form"' in response.text
    alerts = re.findall(
        r'<div class="preview-alert" role="alert">\s*<p>(.*?)</p>', response.text, re.S
    )
    assert [html.unescape(text) for text in alerts] == [NO_TOKEN_MESSAGE]


def test_the_scoring_preview_partial_shows_the_error_too(
    client: TestClient, no_token_hubspot: None
) -> None:
    fields = form_values(ScoringConfig())
    response = client.post("/settings/scoring/preview", data=fields, headers=ORIGIN)
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert NO_TOKEN_MESSAGE in html.unescape(response.text)
    assert "What would change" not in response.text  # the partial only, no page around it


# Every surface: the sweep for leaks

BOOK_PATHS = [
    "/",
    "/brief",
    "/onboarding",
    "/onboarding/acc-001",
    "/accounts/acc-001",
    "/accounts/no-such-account",
    "/onboarding/no-such-account",
    "/settings",
    "/settings/appearance",
    "/settings/scoring",
    "/settings/scoring?defaults=1",
    "/settings/connections",
    "/settings/connections?saved=source",
    "/settings/connections?saved=token",
    "/settings/connections?removed=token",
    "/settings/connections?refreshed=1",
    "/no-such-page",
    "/static/css/app.css",
]


@pytest.mark.parametrize(
    "scenario",
    ["demo-file", "hubspot-file", "hubspot-env", "hubspot-error"],
)
def test_the_token_is_in_no_page_header_or_redirect(
    client: TestClient,
    hub: StubHubSpot,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    if scenario == "demo-file":
        _use(DataSource.DEMO)
        _save_token()
    elif scenario == "hubspot-file":
        _use(DataSource.MINE)
        _save_token()
    elif scenario == "hubspot-env":
        _use(DataSource.MINE)
        monkeypatch.setenv(secrets.ENV_TOKEN, TOKEN)
    else:
        _use(DataSource.MINE)
        _save_token()
        hub.mapping_error = "config/hubspot.yaml could not be read."
        hub.check = ConnectorError("HubSpot is taking too long to answer. Try again.")

    for path in BOOK_PATHS:
        _assert_no_token(client.get(path))

    # Every form and partial that can show the token state or a result.
    _assert_no_token(client.post("/settings/connections/test", headers=HX))
    _assert_no_token(client.post("/settings/connections/test", headers=ORIGIN))
    _assert_no_token(client.post("/settings/connections/refresh", headers=ORIGIN))
    _assert_no_token(
        client.post("/settings/connections/source", data={"source": "mine"}, headers=ORIGIN)
    )
    _assert_no_token(
        client.post("/settings/connections/token", data={"token": f"{TOKEN} extra"}, headers=ORIGIN)
    )
    _assert_no_token(
        client.post("/settings/connections/token", data={"token": OTHER}, headers=ORIGIN)
    )
    _assert_no_token(client.post("/settings/connections/use-demo", headers=ORIGIN))
    _assert_no_token(client.post("/settings/connections/token/delete", headers=ORIGIN))
    _assert_no_token(
        client.post("/settings/scoring/preview", data=form_values(ScoringConfig()), headers=ORIGIN)
    )


def test_the_token_is_absent_from_a_page_rendered_after_it_was_typed_in_error(
    client: TestClient,
) -> None:
    response = client.post(
        "/settings/connections/token", data={"token": f"{TOKEN} oops"}, headers=ORIGIN
    )
    assert response.status_code == 422
    _assert_no_token(response)


# Every new POST refuses cross-site writes and changes nothing

NEW_POSTS = [
    ("/settings/connections/source", {"source": "demo"}),
    ("/settings/connections/token", {"token": OTHER}),
    ("/settings/connections/token/delete", {}),
    ("/settings/connections/test", {}),
    ("/settings/connections/refresh", {}),
    ("/settings/connections/use-demo", {}),
]
CROSS_SITE = [
    {"origin": "https://evil.example"},
    {"origin": "null", "sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "cross-site"},
]


@pytest.fixture
def settled(config_folder: Path, secrets_folder: Path) -> Iterator[dict[str, bytes]]:
    """A HubSpot setup with a saved token, and the bytes of the files a POST could change."""
    _use(DataSource.MINE)
    _save_token()
    yield {
        "settings": (config_folder / "settings.yaml").read_bytes(),
        "token": (secrets_folder / secrets.TOKEN_FILE).read_bytes(),
    }


@pytest.mark.parametrize(("path", "data"), NEW_POSTS)
@pytest.mark.parametrize("headers", CROSS_SITE)
def test_a_cross_site_post_is_refused_and_changes_nothing(
    client: TestClient,
    hub: StubHubSpot,
    resets: list[int],
    config_folder: Path,
    secrets_folder: Path,
    settled: dict[str, bytes],
    path: str,
    data: dict[str, str],
    headers: dict[str, str],
) -> None:
    response = client.post(path, data=data, headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    assert (config_folder / "settings.yaml").read_bytes() == settled["settings"]
    assert (secrets_folder / secrets.TOKEN_FILE).read_bytes() == settled["token"]
    assert hub.refreshes == 0
    assert hub.checks_run == 0
    assert hub.tokens == []
    assert resets == []
