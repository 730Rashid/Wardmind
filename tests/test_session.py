"""The session gate (web/session.py): only the person who started Wardmind can open its pages."""

from __future__ import annotations

import json
import socket
import webbrowser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wardmind import cli
from wardmind.web import app as web
from wardmind.web import session
from wardmind.web.session import SessionGate

BASE = "http://127.0.0.1"
ORIGIN = {"origin": BASE}


@pytest.fixture
def gate() -> SessionGate:
    return SessionGate.new()


@pytest.fixture
def client(gate: SessionGate) -> TestClient:
    return TestClient(web.create_app(gate), base_url=BASE)


def _enter(client: TestClient, gate: SessionGate, next_path: str = "/") -> None:
    response = client.get(gate.entry_url("", next_path), follow_redirects=False)
    assert response.status_code == 303


# --- Tokens -------------------------------------------------------------------------------


def test_each_gate_has_its_own_long_random_tokens() -> None:
    first, second = SessionGate.new(), SessionGate.new()
    assert first.entry_token != second.entry_token
    assert first.session_token != second.session_token
    assert first.entry_token != first.session_token
    assert len(first.entry_token) >= 40 and len(first.session_token) >= 40


@pytest.mark.parametrize("value", ["", None, "wrong", " "])
def test_a_wrong_or_missing_token_is_refused(gate: SessionGate, value: str | None) -> None:
    assert not gate.accepts_entry(value)
    assert not gate.accepts_cookie(value)


def test_the_right_tokens_are_accepted_and_not_swapped(gate: SessionGate) -> None:
    assert gate.accepts_entry(gate.entry_token)
    assert gate.accepts_cookie(gate.session_token)
    assert not gate.accepts_entry(gate.session_token)
    assert not gate.accepts_cookie(gate.entry_token)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "/"),
        ("/today", "/today"),
        ("/accounts/acc-1?activity=call#activity", "/accounts/acc-1?activity=call#activity"),
        ("", "/"),
        (None, "/"),
        ("today", "/"),
        ("//evil.example/", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_next_only_ever_goes_to_a_path_on_this_app(path: str | None, expected: str) -> None:
    assert session.safe_next(path) == expected


def test_the_entry_url_carries_the_token_and_a_safe_next(gate: SessionGate) -> None:
    url = gate.entry_url("http://127.0.0.1:8000/", "//evil.example")
    assert url == f"http://127.0.0.1:8000/enter?t={gate.entry_token}&next=%2F"


# --- The gated app ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/today", "/brief", "/renewals", "/settings/privacy"])
def test_without_the_cookie_every_page_is_refused(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 403
    assert "wardmind open" in response.text
    assert "Wardmind" in response.text
    # The refusal still carries the app's security headers.
    assert response.headers["content-security-policy"] == web.CSP


def test_without_the_cookie_a_write_is_refused_before_it_runs(client: TestClient) -> None:
    response = client.post("/settings/privacy/research", data={"on": "1"}, headers=ORIGIN)
    assert response.status_code == 403


def test_static_files_are_open_without_the_cookie(client: TestClient) -> None:
    assert client.get("/static/css/app.css").status_code == 200


def test_the_entry_link_sets_a_strict_http_only_cookie_and_redirects(
    client: TestClient, gate: SessionGate
) -> None:
    response = client.get(gate.entry_url("", "/today"), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/today"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{session.SESSION_COOKIE}={gate.session_token};")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert gate.entry_token not in cookie


def test_after_entering_pages_open(client: TestClient, gate: SessionGate) -> None:
    _enter(client, gate)
    assert client.get("/").status_code == 200
    assert client.get("/today").status_code == 200


def test_the_entry_link_can_be_used_again_in_the_same_run(
    gate: SessionGate,
) -> None:
    app = web.create_app(gate)
    for _ in range(2):
        browser = TestClient(app, base_url=BASE)
        _enter(browser, gate)
        assert browser.get("/").status_code == 200


def test_a_wrong_entry_token_is_refused_and_sets_no_cookie(client: TestClient) -> None:
    response = client.get("/enter?t=nope&next=%2F", follow_redirects=False)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert client.get("/").status_code == 403


def test_a_cookie_from_another_run_is_refused(gate: SessionGate) -> None:
    other = SessionGate.new()
    browser = TestClient(web.create_app(gate), base_url=BASE)
    browser.cookies.set(session.SESSION_COOKIE, other.session_token)
    assert browser.get("/").status_code == 403


def test_the_entry_token_itself_is_not_a_session_cookie(gate: SessionGate) -> None:
    browser = TestClient(web.create_app(gate), base_url=BASE)
    browser.cookies.set(session.SESSION_COOKIE, gate.entry_token)
    assert browser.get("/").status_code == 403


def test_an_open_redirect_through_the_entry_link_goes_home_instead(
    client: TestClient, gate: SessionGate
) -> None:
    changed = f"/enter?t={gate.entry_token}x&next=%2F"
    assert client.get(changed, follow_redirects=False).status_code == 403  # a changed token
    response = client.get(
        f"/enter?t={gate.entry_token}&next=%2F%2Fevil.example%2F", follow_redirects=False
    )
    assert response.headers["location"] == "/"


def test_without_a_gate_there_is_no_entry_route_and_pages_are_open() -> None:
    open_client = TestClient(web.create_app(), base_url=BASE)
    assert open_client.get("/").status_code == 200
    assert open_client.get("/enter?t=x").status_code == 404


# --- The entry link on disk ---------------------------------------------------------------


def test_publish_and_read_back_then_withdraw(tmp_path: Path, gate: SessionGate) -> None:
    path = session.publish(gate, "http://127.0.0.1:8123", tmp_path)
    assert path == tmp_path / session.SESSION_FILE
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:8123",
        "entry": gate.entry_token,
    }
    assert session.published(tmp_path) == ("http://127.0.0.1:8123", gate.entry_token)
    session.withdraw(tmp_path)
    assert not path.exists()
    assert session.published(tmp_path) is None
    session.withdraw(tmp_path)  # already gone is fine


@pytest.mark.parametrize("body", ["", "not json", "[]", '{"url": "", "entry": "x"}', '{"url": 1}'])
def test_an_unreadable_session_file_reads_as_none(tmp_path: Path, body: str) -> None:
    (tmp_path / session.SESSION_FILE).write_text(body, encoding="utf-8")
    assert session.published(tmp_path) is None


def test_open_link_needs_a_running_app_and_removes_a_stale_file(
    tmp_path: Path, gate: SessionGate
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = int(holder.getsockname()[1])
    session.publish(gate, f"http://127.0.0.1:{port}", tmp_path)
    assert session.open_link(tmp_path) is None
    assert not (tmp_path / session.SESSION_FILE).exists()


def test_open_link_gives_the_entry_link_of_a_running_app(tmp_path: Path, gate: SessionGate) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        session.publish(gate, f"http://127.0.0.1:{port}", tmp_path)
        link = session.open_link(tmp_path, "/today")
    assert link == f"http://127.0.0.1:{port}/enter?t={gate.entry_token}&next=%2Ftoday"


# --- wardmind open --------------------------------------------------------------------------


def test_wardmind_open_says_so_when_nothing_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    result = CliRunner().invoke(cli.app, ["open"])
    assert result.exit_code == 1
    assert "Wardmind isn't running. Start it with: wardmind ui" in result.output
    assert opened == []


def test_wardmind_open_opens_the_signed_in_link(
    monkeypatch: pytest.MonkeyPatch, gate: SessionGate
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        session.publish(gate, f"http://127.0.0.1:{port}")  # the test's secrets folder
        result = CliRunner().invoke(cli.app, ["open"])
    assert result.exit_code == 0, result.output
    assert opened == [f"http://127.0.0.1:{port}/enter?t={gate.entry_token}&next=%2F"]


def test_the_cookie_name_carries_the_port() -> None:
    assert session.cookie_name(8000) == "wardmind_session_8000"
    assert session.cookie_name(8001) != session.cookie_name(8000)
    assert session.cookie_name(None) == session.SESSION_COOKIE


def test_the_activity_log_records_the_session_and_sign_ins(
    client: TestClient, gate: SessionGate
) -> None:
    from wardmind.core import audit

    for _ in range(3):  # a burst of bad links is one entry, not three
        assert client.get("/enter?t=wrong").status_code == 403
    _enter(client, gate)
    kinds = [entry.event for entry in audit.entries()]
    assert kinds.count(audit.Event.SIGN_IN_REFUSED) == 1
    assert audit.Event.SIGN_IN in kinds and audit.Event.SESSION_STARTED in kinds
    assert all(gate.entry_token not in entry.detail for entry in audit.entries())
