"""The web shell: routes, security headers, templates, filters and the ui command."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import socket
import threading
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wardmind import cli
from wardmind.core import service
from wardmind.core.connectors.demo import DemoConnector
from wardmind.web import app as web

WEB_DIR = Path(web.__file__).parent
TEMPLATES = WEB_DIR / "templates"
STATIC = WEB_DIR / "static"
NAV_LINKS = ["/today", "/", "/brief", "/onboarding", "/renewals", "/client-updates", "/settings"]
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
EXPECTED_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
}


class _Anchors(HTMLParser):
    """Collects the attributes of every <a> tag."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.anchors.append(dict(attrs))


def _anchors(markup: str) -> list[dict[str, str | None]]:
    parser = _Anchors()
    parser.feed(markup)
    return parser.anchors


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes apostrophes in variables."""
    return html.unescape(response.text)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.mark.parametrize("path", NAV_LINKS)
def test_nav_route_is_html_and_marks_its_own_link_current(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    nav = [a for a in _anchors(response.text) if a.get("class") == "nav-link"]
    assert [a["href"] for a in nav] == NAV_LINKS
    assert [a["href"] for a in nav if a.get("aria-current") == "page"] == [path]
    assert '<summary class="book-switcher-summary">Demo data</summary>' in response.text


@pytest.mark.parametrize("path", ["/", "/accounts/acc-001"])
def test_security_headers_on_html(client: TestClient, path: str) -> None:
    response = client.get(path)
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


def test_security_headers_on_a_static_file(client: TestClient) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


def test_foreign_host_header_is_rejected(client: TestClient) -> None:
    response = client.get("/", headers={"host": "evil.example"})
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_unknown_path_renders_the_layout_with_the_copy(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "<title>Page not found - Wardmind</title>" in response.text
    assert "This page doesn't exist." in _text(response)
    assert "Back to My book" in _text(response)
    assert 'class="top-bar"' in response.text


def test_account_page_shows_the_account_name(client: TestClient) -> None:
    account = next(a for a in DemoConnector().list_accounts() if a.id == "acc-001")
    response = client.get("/accounts/acc-001")
    assert response.status_code == 200
    assert f"<title>{html.escape(account.name)} - Wardmind</title>" in response.text
    assert account.name in _text(response)


def test_unknown_account_is_a_404_in_the_layout(client: TestClient) -> None:
    response = client.get("/accounts/nope")
    assert response.status_code == 404
    assert "No account with id nope." in _text(response)
    assert "Back to My book" in _text(response)


def test_account_id_is_escaped_in_the_404_message(client: TestClient) -> None:
    response = client.get("/accounts/%3Cb%3Ex")
    assert response.status_code == 404
    assert "No account with id &lt;b&gt;x." in response.text
    assert "<b>x" not in response.text


def test_htmx_integrity_matches_the_vendored_file(client: TestClient) -> None:
    digest = hashlib.sha384((STATIC / "js" / "htmx.min.js").read_bytes()).digest()
    expected = "sha384-" + base64.b64encode(digest).decode("ascii")
    assert f'integrity="{expected}"' in client.get("/").text
    assert expected in (STATIC / "VENDORED.md").read_text(encoding="utf-8")


URL_PATTERN = re.compile(r"https?://[^\s\"'<>)]+")
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
# Pages may link out (navigation only, never loaded) to these addresses, and nowhere else.
ALLOWED_LINK_TARGETS = {"https://openrouter.ai/models"}
LINK_TARGET_PATTERN = re.compile(r'<a\b[^>]*\bhref="(https?://[^"]+)"')


def test_no_external_origins_in_templates_css_or_icons() -> None:
    files = [
        *(f for f in TEMPLATES.rglob("*") if f.is_file()),
        *(f for f in (STATIC / "css").rglob("*") if f.is_file()),
        STATIC / "icons.svg",
    ]
    for file in files:
        text = file.read_text(encoding="utf-8")
        links = set(LINK_TARGET_PATTERN.findall(text))
        assert links <= ALLOWED_LINK_TARGETS, f"{file.name} links to {links}"
        found = set(URL_PATTERN.findall(text)) - ALLOWED_URLS - links
        assert not found, f"{file.name} refers to {found}"


def test_templates_have_no_inline_script_or_style_attributes() -> None:
    for file in TEMPLATES.glob("*.html"):
        text = file.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", text, flags=re.IGNORECASE):
            assert re.search(r"\ssrc=", tag), f"{file.name} has an inline script: {tag}"
        assert not re.search(r"\sstyle\s*=", text), f"{file.name} has a style attribute"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(950, "$950"), (950_000, "$950,000"), (1_400_000, "$1,400,000"), (184_000, "$184,000")],
)
def test_money_filter(value: float, expected: str) -> None:
    assert web.money(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (800, "$800"),
        (950, "$950"),
        (950_000, "$950K"),
        (184_000, "$184K"),
        (1_400_000, "$1.4M"),
        (999_600, "$1.0M"),
    ],
)
def test_money_short_filter(value: float, expected: str) -> None:
    assert web.money_short(value) == expected


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace uvicorn.run and record what it was called with."""
    seen: dict[str, Any] = {}

    def fake_run(app: FastAPI, **kwargs: Any) -> None:
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return seen


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


def test_ui_binds_loopback_on_the_requested_port(server: dict[str, Any], timers: list) -> None:
    port = _free_port()
    result = CliRunner().invoke(cli.app, ["ui", "--port", str(port), "--no-browser"])
    assert result.exit_code == 0, result.output
    assert server["host"] == "127.0.0.1"
    assert server["port"] == port
    assert server["log_level"] == "warning"
    assert server["app"].docs_url is None
    assert timers == []
    assert f"Wardmind is running at http://127.0.0.1:{port}  (Ctrl+C to stop)" in result.output
    assert "wardmind open" in result.output
    # The app it serves is gated: without the sign-in cookie every page is refused.
    gated = TestClient(server["app"], base_url="http://127.0.0.1")
    assert gated.get("/").status_code == 403
    assert gated.get("/static/css/app.css").status_code == 200


def test_ui_opens_the_browser_one_second_later(
    server: dict[str, Any], timers: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    result = CliRunner().invoke(cli.app, ["ui", "--port", str(port)])
    assert result.exit_code == 0, result.output
    (timer,) = timers
    assert timer.interval == 1.0
    assert timer.started
    timer.function(*timer.args)
    (link,) = opened
    assert link.startswith(f"http://127.0.0.1:{port}/enter?t=")
    assert link.endswith("&next=%2F")
    # The link signs the browser in and lands on the page it names.
    gated = TestClient(server["app"], base_url="http://127.0.0.1")
    landed = gated.get(link.split(str(port), 1)[1])
    assert landed.status_code == 200
    assert str(landed.url).endswith("/")
    assert server["host"] == "127.0.0.1"


def test_ui_refuses_a_port_that_is_in_use(server: dict[str, Any], timers: list) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = int(holder.getsockname()[1])
        result = CliRunner().invoke(cli.app, ["ui", "--port", str(port), "--no-browser"])
    assert result.exit_code != 0
    assert f"Port {port} is in use. Try: wardmind ui --port {port + 1}" in result.output
    assert server == {}
    assert timers == []


def test_ui_has_no_host_option(server: dict[str, Any]) -> None:
    result = CliRunner().invoke(cli.app, ["ui", "--host", "0.0.0.0", "--no-browser"])
    assert result.exit_code != 0
    assert server == {}
