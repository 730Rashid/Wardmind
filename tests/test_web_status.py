"""System status and the two shared pieces every page can show (web/status_pages.py): the status
page lists each check with its words and its fix link, Run checks now is a plain POST that works
without JavaScript, the problem banner appears on every page, and the freshness line on My book
says how current the data is (and says so when a feed is overdue). Demo data has no freshness
line.

Runs through the real routes against the temp folders from conftest."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, runlog, service
from wardmind.core.feeds.models import FeedRole, FeedSettings, FeedStatus, Schedule
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import JOURNAL_FILE, ConnectionKind
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
NOW = datetime.now(UTC).replace(tzinfo=None)
TODAY = date.today()


@pytest.fixture(autouse=True)
def scheduler_not_running() -> None:
    runlog.scheduler_stopped()
    yield
    runlog.scheduler_stopped()


@pytest.fixture
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


@pytest.fixture
def demo() -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _account() -> None:
    editing.create_account(
        service.get_store(),
        {
            "name": "Acme Robotics",
            "arr": "$100,000",
            "renewal_date": (TODAY + timedelta(days=200)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()


def _usage_feed(synced: datetime | None, *, error: str | None = None) -> None:
    store = service.get_store()
    config = store.add_connection(ConnectionKind.FEED, "Usage")
    status = FeedStatus(
        last_synced_at=synced,
        last_attempt_at=synced,
        last_error=error,
        failures=1 if error else 0,
        first_failed_at=NOW if error else None,
    )
    feed = FeedSettings(
        provider="file",
        role=FeedRole.SIGNALS,
        settings={},
        schedule=Schedule.HOURLY,
        status=status,
    )
    store.update_connection(config.model_copy(update={"feed": feed}))
    service.reset_connectors()


# --- The status page -----------------------------------------------------------------------------


def test_the_status_page_lists_every_check_with_its_words(client: TestClient, mine: None) -> None:
    service.get_store().add_connection(ConnectionKind.MANUAL, "Added by hand")
    response = client.get("/settings/status")
    assert response.status_code == 200
    _clean(response)
    body = _text(response)
    assert "System status" in body
    for name in ("Your notes", "Score history", "Suggestions", "AI summaries"):
        assert name in body
    assert "Working" in body or "Needs a look" in body
    assert 'action="/settings/status/run"' in response.text
    assert "Run checks now" in body


def test_a_failing_feed_is_counted_on_the_page_and_says_why(client: TestClient, mine: None) -> None:
    _usage_feed(NOW - timedelta(hours=3), error="The web address didn't answer.")
    body = _text(client.get("/settings/status"))
    assert "1 check is not working." in body
    assert "The web address didn't answer. Failing since" in body
    assert "Not working" in body
    assert 'href="/settings/connections"' in body


def test_a_page_that_is_all_working_says_so(client: TestClient, demo: None) -> None:
    body = _text(client.get("/settings/status"))
    assert "Everything is working." in body


def test_run_checks_now_works_without_javascript(client: TestClient, mine: None) -> None:
    _account()
    response = client.post("/settings/status/run", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/status?saved=ran"
    page = client.get("/settings/status?saved=ran")
    assert "Checks ran." in _text(page)
    _clean(page)
    snapshot = runlog.last("snapshot")
    assert snapshot is not None and snapshot.ok is True
    assert "Score history" in _text(page)  # its run shows under Recent runs


def test_run_checks_now_refuses_a_cross_site_post(client: TestClient, mine: None) -> None:
    response = client.post(
        "/settings/status/run",
        headers={"origin": "https://elsewhere.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_the_recent_runs_are_in_a_disclosure_per_job(client: TestClient, mine: None) -> None:
    with runlog.run("rules", day=TODAY):
        pass
    body = _text(client.get("/settings/status"))
    assert "<details" in body
    assert "Automatic follow-ups" in body
    assert "Worked" in body


# --- The banner on every page --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/settings/overview", "/settings/status", "/settings/activity"])
def test_an_unreadable_journal_shows_a_banner_on_every_page(
    client: TestClient, mine: None, path: str
) -> None:
    store = service.get_store()
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_text("{ not a journal", encoding="utf-8")
    response = client.get(path)
    assert response.status_code == 200
    body = _text(response)
    assert 'role="alert"' in response.text
    assert "couldn't be read" in body
    assert 'href="/settings/backup"' in response.text
    assert not INLINE.search(response.text)  # the overview's own copy has "None are on" too


def test_a_stopped_scheduler_shows_a_banner(client: TestClient, mine: None) -> None:
    runlog.scheduler_started(NOW - timedelta(minutes=40))
    runlog.beat(NOW - timedelta(minutes=40))
    body = _text(client.get("/settings/overview"))
    assert "Background checks have stopped" in body
    assert 'href="/settings/status"' in client.get("/settings/overview").text


def test_no_banner_when_everything_is_fine(client: TestClient, mine: None) -> None:
    response = client.get("/settings/overview")
    assert 'role="alert"' not in response.text


# --- The freshness line on My book ---------------------------------------------------------------


def test_my_book_says_when_the_feed_last_synced(client: TestClient, mine: None) -> None:
    _account()
    synced = NOW - timedelta(minutes=10)
    _usage_feed(synced)
    response = client.get("/")
    assert response.status_code == 200
    _clean(response)
    body = _text(response)
    assert "Data as of" in body
    assert f"{synced.replace(tzinfo=UTC).astimezone():%H:%M}" in body
    assert "freshness--warn" not in response.text


def test_an_overdue_feed_is_named_in_the_freshness_line(client: TestClient, mine: None) -> None:
    _account()
    _usage_feed(NOW - timedelta(days=3))
    response = client.get("/")
    body = _text(response)
    assert "Usage hasn't synced since" in body
    assert "scores may be out of date" in body
    assert "freshness--warn" in response.text
    assert 'href="/settings/status"' in response.text


def test_demo_data_has_no_freshness_line(client: TestClient, demo: None) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Data as of" not in _text(response)
    assert 'class="freshness' not in response.text
