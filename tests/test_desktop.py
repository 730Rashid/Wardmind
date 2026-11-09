"""The desktop app: a frozen build keeps its settings in the per-user folder, the web app runs
in a background thread on 127.0.0.1 and stops when the window closes, and start-up problems are
shown as a message rather than a crash. No real window is opened: a fake `webview` module
stands in for pywebview."""

from __future__ import annotations

import socket
import sys
import types
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from wardmind import desktop
from wardmind.cli import app
from wardmind.core.config import config_dir
from wardmind.core.secrets import ENV_SECRETS_DIR
from wardmind.web.app import HOST

# --- where a frozen build keeps its settings ---------------------------------------------


def test_from_source_settings_live_in_the_folder_wardmind_starts_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WARDMIND_CONFIG_DIR")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config_dir() == tmp_path / "config"


def test_the_desktop_app_keeps_settings_in_the_per_user_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WARDMIND_CONFIG_DIR")
    monkeypatch.setenv(ENV_SECRETS_DIR, str(tmp_path / "profile"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.chdir(tmp_path)
    assert config_dir() == tmp_path / "profile" / "config"


def test_the_config_dir_variable_still_wins_in_the_desktop_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WARDMIND_CONFIG_DIR", str(tmp_path / "chosen"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config_dir() == tmp_path / "chosen"


def test_a_blank_config_dir_variable_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WARDMIND_CONFIG_DIR", "  ")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config_dir() == tmp_path / "config"


# --- the window and the web app behind it ------------------------------------------------


class FakeWindow:
    def __init__(self) -> None:
        self.events = types.SimpleNamespace(closed=FakeEvent())


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def fake_webview(on_start) -> types.ModuleType:
    """A stand-in for pywebview: create_window records its arguments, and start() runs
    `on_start(url)` (as if the user looked at the page), then "closes" the window."""
    module = types.ModuleType("webview")
    module.settings = {"ALLOW_DOWNLOADS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
    module.calls = {}

    def create_window(title: str, url: str, **options: object) -> FakeWindow:
        module.calls["window"] = {"title": title, "url": url, **options}
        module.window = FakeWindow()
        return module.window

    def start(**options: object) -> None:
        module.calls["start"] = options
        on_start(module.calls["window"]["url"])
        for handler in module.window.events.closed.handlers:
            handler()

    module.create_window = create_window
    module.start = start
    return module


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []
    monkeypatch.setattr(desktop, "show_message", shown.append)
    monkeypatch.setattr(desktop, "claim_single_instance", lambda: True)
    return shown


def test_the_window_shows_the_dashboard_and_closing_it_stops_the_web_app(
    monkeypatch: pytest.MonkeyPatch, messages: list[str]
) -> None:
    seen: dict[str, object] = {}

    def look(url: str) -> None:
        response = httpx.get(url, timeout=10)
        seen["status"] = response.status_code
        seen["body"] = response.text

    module = fake_webview(look)
    monkeypatch.setitem(sys.modules, "webview", module)
    threads: list[desktop.WebAppThread] = []
    real = desktop.WebAppThread

    def tracked(port: int) -> desktop.WebAppThread:
        web = real(port)
        threads.append(web)
        return web

    monkeypatch.setattr(desktop, "WebAppThread", tracked)

    assert desktop.run() == 0

    window = module.calls["window"]
    assert window["title"] == "Wardmind"
    assert str(window["url"]).startswith(f"http://{HOST}:")
    assert seen["status"] == 200
    assert "Wardmind" in str(seen["body"])
    assert module.settings["ALLOW_DOWNLOADS"] is True
    assert module.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True
    assert module.calls["start"]["private_mode"] is True
    assert messages == []
    (web,) = threads
    assert not web.thread.is_alive()
    with pytest.raises(httpx.HTTPError):
        httpx.get(web.url, timeout=2)


def test_the_web_app_listens_on_this_computer_only(
    monkeypatch: pytest.MonkeyPatch, messages: list[str]
) -> None:
    hosts: list[str] = []

    def look(url: str) -> None:
        hosts.append(httpx.URL(url).host)

    monkeypatch.setitem(sys.modules, "webview", fake_webview(look))
    assert desktop.run() == 0
    assert hosts == ["127.0.0.1"]


def test_a_second_window_is_refused_with_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(desktop, "show_message", shown.append)
    monkeypatch.setattr(desktop, "claim_single_instance", lambda: False)
    monkeypatch.setattr(desktop, "WebAppThread", lambda port: pytest.fail("no web app may start"))
    assert desktop.run() == 0
    assert shown == ["Wardmind is already open."]


def test_a_missing_window_engine_is_explained_and_the_web_app_stops(
    monkeypatch: pytest.MonkeyPatch, messages: list[str]
) -> None:
    def broken(url: str) -> None:
        raise RuntimeError("WebView2 not found")

    monkeypatch.setitem(sys.modules, "webview", fake_webview(broken))
    assert desktop.run() == 1
    assert len(messages) == 1
    assert "WebView2" in messages[0]


def test_without_pywebview_it_points_to_the_browser_version(
    monkeypatch: pytest.MonkeyPatch, messages: list[str]
) -> None:
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview raises ImportError
    assert desktop.run() == 1
    assert "wardmind ui" in messages[0]


def test_a_web_app_that_never_starts_is_reported(
    monkeypatch: pytest.MonkeyPatch, messages: list[str]
) -> None:
    monkeypatch.setitem(
        sys.modules, "webview", fake_webview(lambda url: pytest.fail("no window may open"))
    )
    monkeypatch.setattr(desktop.WebAppThread, "start", _fail_to_start)
    assert desktop.run() == 1
    assert messages == ["Wardmind couldn't start its local web app."]


def _fail_to_start(self: desktop.WebAppThread, timeout: float = 0) -> None:
    raise desktop.DesktopError("Wardmind couldn't start its local web app.")


def test_the_web_app_thread_reports_a_port_already_taken() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind((HOST, 0))
        taken.listen()
        web = desktop.WebAppThread(taken.getsockname()[1])
        with pytest.raises(desktop.DesktopError):
            web.start(timeout=10)
        assert not web.thread.is_alive()


def test_free_port_is_on_this_computer_and_free() -> None:
    port = desktop.free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, port))


def test_a_windowed_exe_without_console_streams_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop._give_streams_somewhere()
    try:
        print("goes nowhere")
        print("also nowhere", file=sys.stderr)
    finally:
        sys.stdout.close()
        sys.stderr.close()


def test_only_one_window_per_user_session_on_windows() -> None:
    if sys.platform != "win32":
        pytest.skip("the single-window check uses a Windows mutex")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, desktop._MUTEX_NAME)
    try:
        assert desktop.claim_single_instance() is False
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_the_desktop_command_runs_the_desktop_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "run", lambda: 0)
    result = CliRunner().invoke(app, ["desktop"])
    assert result.exit_code == 0
