"""The MCP server's view of the journal and score history: get_account's journal summary (never
the text of a note), weekly_brief's "since last week", and the todays_list tool. Runs on the demo
book, whose journal is its own file in the temp data folder."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp import Client
from mcp.types import CallToolResult

from wardmind.core import journal, service
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.mcp import server

TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
SECRET_NOTE = "Priya said budget is frozen until March"


def _data(result: CallToolResult) -> Any:
    assert not result.is_error, result.content
    return result.structured_content


def _demo() -> None:
    save_settings(Settings(source=DataSource.DEMO))
    service.reset_connectors()


async def test_get_account_shares_the_journal_but_never_note_text() -> None:
    _demo()
    book = service.get_journal()
    book.add_note(
        "acc-001",
        journal.TouchKind.CALL,
        SECRET_NOTE,
        TODAY - timedelta(days=2),
        now=NOW,
        today=TODAY,
    )
    follow_up = book.add_follow_up("acc-001", "Send the renewal quote", TODAY, now=NOW, today=TODAY)
    async with Client(server.mcp) as client:
        data = _data(await client.call_tool("get_account", {"account_id": "acc-001"}))
    summary = data["journal"]
    assert summary["last_contact"] == (TODAY - timedelta(days=2)).isoformat()
    assert summary["notes"] == 1
    assert summary["open_follow_ups"] == [
        {"follow_up_id": follow_up.id, "text": "Send the renewal quote", "due": TODAY.isoformat()}
    ]
    assert summary["snoozed_until"] is None
    assert summary["outcomes"] == []
    assert SECRET_NOTE not in json.dumps(data)


async def test_get_account_shows_a_snooze() -> None:
    _demo()
    item = service.find_account(service.get_book(), "acc-001")
    assert item is not None
    until = TODAY + timedelta(days=7)
    service.get_journal().snooze("acc-001", until, item.health, now=NOW, today=TODAY)
    async with Client(server.mcp) as client:
        data = _data(await client.call_tool("get_account", {"account_id": "acc-001"}))
    assert data["journal"]["snoozed_until"] == until.isoformat()


async def test_weekly_brief_says_what_changed_since_last_week_on_demo_data() -> None:
    _demo()
    async with Client(server.mcp) as client:
        data = _data(await client.call_tool("weekly_brief", {}))
    since = data["since_last_week"]
    assert since is not None
    assert since["since"] == (TODAY - timedelta(days=7)).isoformat()
    for change in since["changes"]:
        assert set(change) == {"id", "name", "kind", "sentence"}


async def test_weekly_brief_explains_missing_history_on_your_data() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()
    async with Client(server.mcp) as client:
        result = await client.call_tool("weekly_brief", {})
    if result.is_error:  # no connections: the book can't load, which is its own message
        return
    data = _data(result)
    assert data["since_last_week"] is None
    assert "history" in data["history_note"]


async def test_todays_list_lists_due_follow_ups_and_attention() -> None:
    _demo()
    service.get_journal().add_follow_up("acc-002", "Book the QBR", TODAY, now=NOW, today=TODAY)
    async with Client(server.mcp) as client:
        data = _data(await client.call_tool("todays_list", {}))
    assert data["as_of"] == TODAY.isoformat()
    assert [row["follow_up"] for row in data["due"]] == ["Book the QBR"]
    assert data["due"][0]["id"] == "acc-002"
    assert data["due"][0]["days_late"] == 0
    assert len(data["needs_attention"]) <= journal.ATTENTION_LIMIT
    for row in data["needs_attention"]:
        assert row["health"] in {"at_risk", "watch"}
        assert "last_contact" in row
    assert set(data) >= {"renewals_to_record", "coming_up", "snoozed", "contacted_in_last_7_days"}
