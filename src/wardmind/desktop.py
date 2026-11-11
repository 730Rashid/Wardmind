"""Wardmind as a desktop app: the dashboard in its own window instead of a browser tab.

`wardmind desktop` (and Wardmind.exe, which runs it) starts the same web app as `wardmind ui`
on 127.0.0.1, on a free port, in a background thread, and shows it in a native window. On
Windows that window is Microsoft Edge WebView2, which is part of Windows 10 and 11. Closing the
window stops the web app, and with it the background feed syncs.

The window's browser runs in private mode, so it keeps no cookies, cache or site storage. Links
to other websites open in the computer's normal browser (static/js/app.js marks them
target=_blank, and pywebview hands those to the system browser).
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

WINDOW_TITLE = "Wardmind"
START_SECONDS = 30.0  # the web app must be answering within this, or start-up has failed
STOP_SECONDS = 10.0  # how long closing the window waits for the web app to finish
_MUTEX_NAME = "Local\\Wardmind-desktop"  # one desktop window per Windows user session
_ERROR_ALREADY_EXISTS = 183

_mutex_handle: Any = None  # kept for the life of the process; Windows frees it on exit


class DesktopError(Exception):
    """Start-up failed. `message` is shown to the user as is."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def free_port() -> int:
    """A port on 127.0.0.1 that nothing is listening on right now."""
    from wardmind.web.app import HOST

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def claim_single_instance() -> bool:
    """True when this is the only Wardmind window. Two windows would each run the feed
    scheduler, so a second start says Wardmind is already open instead. Always True off
    Windows."""
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return True  # can't tell; better to open than to refuse
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex_handle = handle
    return True


def show_message(text: str) -> None:
    """A message box on Windows (a windowed exe has no console); stderr elsewhere."""
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, WINDOW_TITLE, 0x40)  # MB_ICONINFORMATION
        return
    print(text, file=sys.stderr)


def _give_streams_somewhere() -> None:
    """A windowed exe starts with sys.stdout and sys.stderr set to None. Anything that writes
    to them, uvicorn included, would then fail, so they go to the null device."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


class WebAppThread:
    """The web app running in a background thread on 127.0.0.1:<port>."""

    def __init__(self, port: int) -> None:
        import uvicorn

        from wardmind.web import session
        from wardmind.web.app import HOST, create_app

        self.port = port
        self.url = f"http://{HOST}:{port}"
        # Only this window (and `wardmind open`) can sign in: see web/session.py.
        self.gate = session.SessionGate.new()
        config = uvicorn.Config(
            create_app(self.gate), host=HOST, port=port, log_level="warning", log_config=None
        )
        # uvicorn installs signal handlers only on the main thread, so none are taken here.
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._serve, name="wardmind-web", daemon=True)

    def _serve(self) -> None:
        # uvicorn ends a failed start-up (the port was taken, say) with sys.exit. In this
        # thread that only ends the thread, which start() notices.
        with contextlib.suppress(SystemExit):
            self.server.run()

    def start(self, timeout: float = START_SECONDS) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise DesktopError("Wardmind couldn't start its local web app.")
            if time.monotonic() > deadline:
                self.stop()
                raise DesktopError("Wardmind took too long to start. Try opening it again.")
            time.sleep(0.05)

    def request_stop(self) -> None:
        """Ask the web app to finish; returns at once."""
        self.server.should_exit = True

    def stop(self, timeout: float = STOP_SECONDS) -> None:
        self.request_stop()
        if self.thread.is_alive():
            self.thread.join(timeout)


def open_window(url: str, on_closed: Callable[[], None]) -> None:
    """Show `url` in a native window and block until the window is closed."""
    try:
        import webview
    except ImportError as error:
        raise DesktopError(
            "The desktop window isn't available here. Run `wardmind ui` to use Wardmind in "
            "your browser instead."
        ) from error

    webview.settings["ALLOW_DOWNLOADS"] = True  # the CSV template and rows-with-problems files
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=1280,
        height=860,
        min_size=(900, 600),
        text_select=True,
        zoomable=True,
    )
    window.events.closed += on_closed
    try:
        webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=True)
    except Exception as error:  # the window's browser engine is missing or broken
        raise DesktopError(
            "Wardmind needs Microsoft Edge WebView2 to show its window. Install the "
            '"WebView2 Runtime" from Microsoft, then open Wardmind again.'
        ) from error


def run() -> int:
    """Start the web app, show the window, and stop the web app when the window closes.
    Returns the process exit code."""
    _give_streams_somewhere()
    if not claim_single_instance():
        show_message("Wardmind is already open.")
        return 0

    from wardmind.core.settings import load_settings
    from wardmind.web import session

    web = WebAppThread(free_port())
    try:
        web.start()
        session.publish(web.gate, web.url)
        open_window(
            web.gate.entry_url(web.url, load_settings().landing.path), on_closed=web.request_stop
        )
    except DesktopError as error:
        show_message(error.message)
        return 1
    finally:
        session.withdraw()
        web.stop()
    return 0


def main() -> None:
    """Entry point of Wardmind.exe."""
    raise SystemExit(run())
