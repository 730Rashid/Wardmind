"""Settings > Appearance: the page, saving, the choices on every page, refusals, and the CLI."""

from __future__ import annotations

import re
import socket
import threading
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wardmind import cli
from wardmind.core import service
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.settings import (
    Accent,
    Density,
    Landing,
    Settings,
    Theme,
    load_settings,
    save_settings,
)
from wardmind.web import app as web

WEB_DIR = Path(web.__file__).parent
STATIC = WEB_DIR / "static"
TEMPLATES = WEB_DIR / "templates"
ORIGIN = {"origin": "http://127.0.0.1"}
CHOICES = {"theme": "dark", "accent": "petrol", "density": "compact", "landing": "brief"}
DEFAULT_CHECKED = {"theme": "system", "accent": "plum", "density": "comfortable", "landing": "book"}


class _Tags(HTMLParser):
    """Collects the attributes of every start tag."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(markup: str, name: str) -> list[dict[str, str | None]]:
    parser = _Tags()
    parser.feed(markup)
    return [attrs for tag, attrs in parser.tags if tag == name]


def _checked(markup: str) -> dict[str, str | None]:
    """The checked radio in each group, by name."""
    return {
        attrs["name"]: attrs["value"]
        for attrs in _tags(markup, "input")
        if attrs.get("type") == "radio" and "checked" in attrs
    }


def _html_attributes(markup: str) -> dict[str, str | None]:
    (attrs,) = _tags(markup, "html")
    return attrs


def _settings_links(markup: str) -> list[dict[str, str | None]]:
    return [a for a in _tags(markup, "a") if a.get("class") == "settings-link"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Saves go to a temp folder, never to the real config/."""
    monkeypatch.setenv("WARDMIND_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record threading.Timer instances instead of starting threads."""
    created: list[Any] = []

    class FakeTimer:
        def __init__(self, interval: float, function: Any, args: tuple[Any, ...] = ()) -> None:
            self.interval = interval
            self.function = function
            self.args = args
            self.daemon = False
            self.started = False
            created.append(self)

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(threading, "Timer", FakeTimer)
    return created


def _save(client: TestClient, **changes: str) -> None:
    response = client.post(
        "/settings/appearance",
        data={**CHOICES, **changes},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def test_settings_opens_on_appearance(client: TestClient) -> None:
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/appearance"


def test_appearance_shows_the_defaults_checked(client: TestClient) -> None:
    response = client.get("/settings/appearance")
    assert response.status_code == 200
    assert "<title>Appearance - Wardmind</title>" in response.text
    assert _checked(response.text) == DEFAULT_CHECKED


def test_appearance_marks_settings_current_and_lists_the_sub_navigation(client: TestClient) -> None:
    response = client.get("/settings/appearance")
    top = [a for a in _tags(response.text, "a") if a.get("class") == "nav-link"]
    assert [a["href"] for a in top if a.get("aria-current") == "page"] == ["/settings"]
    links = _settings_links(response.text)
    assert [a["href"] for a in links] == [
        "/settings/appearance",
        "/settings/scoring",
        "/settings/connections",
        "/settings/mailbox",
        "/settings/privacy",
    ]
    assert [a["href"] for a in links if a.get("aria-current") == "page"] == ["/settings/appearance"]


def test_appearance_form_posts_to_itself_with_the_four_fieldsets(client: TestClient) -> None:
    response = client.get("/settings/appearance")
    forms = _tags(response.text, "form")
    assert [(f["method"], f["action"]) for f in forms] == [("post", "/settings/appearance")]
    legends = re.findall(r"<legend>(.*?)</legend>", response.text)
    assert legends == ["Theme", "Accent colour", "Density", "Opens on"]
    assert "Compact fits more rows on screen." in response.text
    assert "The screen wardmind ui opens on." in response.text


def test_every_accent_has_a_swatch_and_a_radio(client: TestClient) -> None:
    text = client.get("/settings/appearance").text
    for accent in Accent:
        assert f'name="accent" value="{accent.value}"' in text
        assert f"swatch swatch--{accent.value}" in text


def test_scoring_is_a_form_inside_settings(client: TestClient) -> None:
    response = client.get("/settings/scoring")
    assert response.status_code == 200
    assert 'id="scoring-form"' in response.text
    links = _settings_links(response.text)
    assert [a["href"] for a in links if a.get("aria-current") == "page"] == ["/settings/scoring"]


def test_saving_writes_the_settings_and_redirects(client: TestClient, config_dir: Path) -> None:
    response = client.post(
        "/settings/appearance", data=CHOICES, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/appearance?saved=1"
    assert load_settings(config_dir / "settings.yaml") == Settings(
        theme=Theme.DARK,
        accent=Accent.PETROL,
        density=Density.COMPACT,
        landing=Landing.BRIEF,
    )


def test_the_saved_page_says_so_once(client: TestClient) -> None:
    saved = client.get("/settings/appearance?saved=1").text
    assert '<p class="settings-status" role="status">Appearance saved.</p>' in saved
    assert 'role="status"' not in client.get("/settings/appearance").text


@pytest.mark.parametrize(
    "path", ["/", "/brief", "/settings/appearance", "/accounts/acc-001", "/no-such-page"]
)
def test_saved_choices_reach_every_page(client: TestClient, path: str) -> None:
    _save(client)
    attrs = _html_attributes(client.get(path).text)
    assert attrs["data-theme"] == "dark"
    assert attrs["data-accent"] == "petrol"
    assert attrs["data-density"] == "compact"


def test_system_theme_leaves_out_data_theme(client: TestClient) -> None:
    _save(client, theme="system")
    attrs = _html_attributes(client.get("/").text)
    assert "data-theme" not in attrs
    assert attrs["data-accent"] == "petrol"


def test_light_theme_is_written_to_the_page(client: TestClient) -> None:
    _save(client, theme="light", accent="blue", density="comfortable")
    attrs = _html_attributes(client.get("/").text)
    assert attrs["data-theme"] == "light"
    assert attrs["data-accent"] == "blue"
    assert attrs["data-density"] == "comfortable"


@pytest.mark.parametrize(
    ("field", "bad"),
    [("theme", "purple"), ("accent", "magenta"), ("density", "cosy"), ("landing", "settings")],
)
def test_an_invalid_choice_is_refused_and_writes_nothing(
    client: TestClient, config_dir: Path, field: str, bad: str
) -> None:
    response = client.post("/settings/appearance", data={**CHOICES, field: bad}, headers=ORIGIN)
    assert response.status_code == 422
    assert "Nothing was saved." in response.text
    assert f'id="{field}-error"' in response.text
    assert not (config_dir / "settings.yaml").exists()


def test_an_invalid_post_leaves_the_saved_settings_alone(
    client: TestClient, config_dir: Path
) -> None:
    _save(client)
    response = client.post(
        "/settings/appearance", data={**CHOICES, "theme": "purple"}, headers=ORIGIN
    )
    assert response.status_code == 422
    assert load_settings(config_dir / "settings.yaml").theme == Theme.DARK
    checked = _checked(response.text)
    assert checked["theme"] == "dark"  # the saved value, since the submitted one was invalid
    assert checked["accent"] == "petrol"  # a valid submitted choice stays selected


def test_a_missing_choice_is_refused(client: TestClient, config_dir: Path) -> None:
    data = {key: value for key, value in CHOICES.items() if key != "landing"}
    response = client.post("/settings/appearance", data=data, headers=ORIGIN)
    assert response.status_code == 422
    assert not (config_dir / "settings.yaml").exists()


@pytest.mark.parametrize(
    "headers", [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}]
)
def test_a_cross_site_post_is_refused_and_writes_nothing(
    client: TestClient, config_dir: Path, headers: dict[str, str]
) -> None:
    response = client.post("/settings/appearance", data=CHOICES, headers=headers)
    assert response.status_code == 403
    assert not (config_dir / "settings.yaml").exists()


@pytest.mark.parametrize(("landing", "href"), [("book", "/"), ("brief", "/brief")])
def test_product_name_links_to_the_landing_screen(
    client: TestClient, landing: str, href: str
) -> None:
    _save(client, landing=landing)
    brand = [a for a in _tags(client.get("/").text, "a") if a.get("class") == "brand"]
    assert [a["href"] for a in brand] == [href]


def test_ui_opens_the_landing_screen(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, timers: list[Any]
) -> None:
    save_settings(Settings(landing=Landing.BRIEF))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    port = _free_port()
    result = CliRunner().invoke(cli.app, ["ui", "--port", str(port)])
    assert result.exit_code == 0, result.output
    assert f"Wardmind is running at http://127.0.0.1:{port}" in result.output
    (timer,) = timers
    timer.function(*timer.args)
    # The browser opens the sign-in link, which lands on the chosen screen.
    (link,) = opened
    assert link.startswith(f"http://127.0.0.1:{port}/enter?t=")
    assert link.endswith("&next=%2Fbrief")


def test_css_has_a_selector_for_every_accent_and_compact_density() -> None:
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    for accent in Accent:
        pattern = (
            r':root\[data-accent="'
            + accent.value
            + r'"\]\s*\{\s*--accent:\s*var\(--accent-'
            + accent.value
            + r"\);"
        )
        assert re.search(pattern, css), f"no selector for accent {accent.value}"
    assert re.search(r':root\[data-density="compact"\]\s*\{[^}]*--cell-pad:', css)
    assert re.search(r"\.book th,\s*\.book td \{\s*padding: var\(--cell-pad\)", css)


def test_app_js_wires_the_live_preview() -> None:
    js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    assert 'addEventListener("change"' in js
    for attribute in ("data-theme", "data-accent", "data-density", "data-appearance"):
        assert attribute in js


def test_templates_have_no_inline_event_handlers() -> None:
    for file in TEMPLATES.rglob("*.html"):
        text = file.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", text), f"{file.name} has an inline handler"


def test_settings_pages_keep_the_security_headers(client: TestClient) -> None:
    response = client.get("/settings/appearance")
    assert (
        response.headers["content-security-policy"]
        == (web.SECURITY_HEADERS["Content-Security-Policy"])
    )
