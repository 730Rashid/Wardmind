from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

from cs_brief.core import briefs, service
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectionNotReady
from cs_brief.core.models import Motion
from cs_brief.core.settings import DataSource, Settings, save_settings
from cs_brief.mcp import server

REPO = Path(__file__).resolve().parents[1]
TOOLS = {
    "list_book",
    "get_account",
    "explain_score",
    "weekly_brief",
    "onboarding_brief",
    "research_account",
}
ROW_KEYS = {
    "rank",
    "id",
    "name",
    "health",
    "priority",
    "risk",
    "motion",
    "arr",
    "top_reason",
    "suggested_action",
}
SUBPROCESS_TIMEOUT_S = 30


@pytest.fixture(autouse=True)
def demo_connector(monkeypatch):
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())


def _data(result: CallToolResult) -> Any:
    assert not result.is_error, result.content
    return result.structured_content


def _rows(result: CallToolResult) -> list[dict[str, Any]]:
    return _data(result)["result"]


def _error_text(result: CallToolResult) -> str:
    assert result.is_error
    return result.content[0].text


async def test_lists_the_six_tools_with_descriptions():
    async with Client(server.mcp) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == TOOLS
    assert len(tools) == 6
    assert all(tool.description for tool in tools)


async def test_list_book_defaults_to_twenty_ranked_rows():
    async with Client(server.mcp) as client:
        rows = _rows(await client.call_tool("list_book", {}))
    assert [row["rank"] for row in rows] == list(range(1, 21))
    assert all(set(row) == ROW_KEYS for row in rows)


async def test_motion_filter_keeps_ranks_from_the_whole_book():
    async with Client(server.mcp) as client:
        rows = _rows(await client.call_tool("list_book", {"motion": "RENEWAL"}))
    expected = [
        rank
        for rank, item in enumerate(service.get_book(), start=1)
        if item.motion is Motion.RENEWAL
    ]
    assert len(rows) == 12
    assert all(row["motion"] == "renewal" for row in rows)
    assert [row["rank"] for row in rows] == expected
    assert expected != list(range(expected[0], expected[0] + len(expected)))


async def test_search_matches_names_case_insensitively():
    async with Client(server.mcp) as client:
        rows = _rows(await client.call_tool("list_book", {"search": "brindle"}))
    expected = [
        item.account.name
        for item in service.get_book()
        if "brindle" in item.account.name.casefold()
    ]
    assert [row["name"] for row in rows] == expected
    assert expected


async def test_limit_is_clamped_to_at_least_one():
    async with Client(server.mcp) as client:
        rows = _rows(await client.call_tool("list_book", {"limit": 0}))
    assert [row["rank"] for row in rows] == [1]


async def test_unknown_motion_is_an_error_result_that_lists_the_valid_values():
    async with Client(server.mcp) as client:
        text = _error_text(await client.call_tool("list_book", {"motion": "sales"}))
    assert "sales" in text
    for motion in ("churn", "renewal", "onboarding"):
        assert motion in text


async def test_get_account_returns_the_top_ranked_account():
    top = service.get_book()[0]
    async with Client(server.mcp) as client:
        account = _data(await client.call_tool("get_account", {"account_id": top.account.id}))
    assert account == top.model_dump(mode="json")


async def test_explain_score_returns_the_explanation_for_the_top_ranked_account():
    top = service.get_book()[0]
    async with Client(server.mcp) as client:
        explanation = _data(await client.call_tool("explain_score", {"account_id": top.account.id}))
    assert explanation == briefs.explain(top).model_dump(mode="json")
    assert explanation["summary"]


async def test_unknown_account_id_is_an_error_result_for_both_lookups():
    async with Client(server.mcp) as client:
        for tool in ("get_account", "explain_score"):
            text = _error_text(await client.call_tool(tool, {"account_id": "acc-missing"}))
            assert "Use list_book" in text


def test_unknown_account_raises_value_error_for_python_callers():
    with pytest.raises(ValueError, match="Use list_book"):
        server.get_account("acc-missing")


async def test_onboarding_brief_round_trips_for_an_onboarding_account():
    account = service.onboarding_accounts(service.get_book())[0].account
    async with Client(server.mcp) as client:
        brief = _data(await client.call_tool("onboarding_brief", {"account_id": account.id}))
    assert brief == service.get_onboarding_brief(account.id).model_dump(mode="json")
    assert brief["account"]["id"] == account.id
    assert brief["in_onboarding"] is True
    assert brief["agenda"]
    assert brief["questions"]


async def test_unknown_account_id_is_an_error_result_for_onboarding_brief():
    async with Client(server.mcp) as client:
        text = _error_text(
            await client.call_tool("onboarding_brief", {"account_id": "acc-missing"})
        )
    assert "Use list_book" in text


def test_unknown_account_raises_value_error_for_onboarding_brief_callers():
    with pytest.raises(ValueError, match="Use list_book"):
        server.onboarding_brief("acc-missing")


async def test_weekly_brief_counts_the_whole_book_and_lists_ten_rows():
    async with Client(server.mcp) as client:
        brief = _data(await client.call_tool("weekly_brief", {}))
    assert brief["total_accounts"] == 40
    assert sum(brief["counts_by_health"].values()) == 40
    assert [row["rank"] for row in brief["top"]] == list(range(1, 11))
    assert all(set(row) == ROW_KEYS for row in brief["top"])


@pytest.mark.skipif(
    os.environ.get("CS_BRIEF_SKIP_SUBPROCESS") == "1",
    reason="CS_BRIEF_SKIP_SUBPROCESS=1 skips the slow stdio smoke test",
)
async def test_stdio_subprocess_serves_the_tools():
    params = StdioServerParameters(command=sys.executable, args=["-m", "cs_brief", "mcp"], cwd=REPO)
    async with asyncio.timeout(SUBPROCESS_TIMEOUT_S):
        async with Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool("list_book", {"limit": 1})
    assert {tool.name for tool in tools.tools} == TOOLS
    assert [row["rank"] for row in _rows(result)] == [1]


NOT_READY = (
    "HubSpot is selected, but no access token is saved. "
    "Add one on the Connections page, or switch to demo data."
)


@pytest.fixture
def no_token(monkeypatch):
    def not_ready(*args, **kwargs):
        raise ConnectionNotReady(NOT_READY)

    monkeypatch.setattr(service, "get_connector", not_ready)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_book", {}),
        ("get_account", {"account_id": "acc-001"}),
        ("explain_score", {"account_id": "acc-001"}),
        ("weekly_brief", {}),
        ("onboarding_brief", {"account_id": "acc-001"}),
    ],
)
async def test_every_book_tool_reports_a_connector_error_as_its_message(no_token, tool, arguments):
    async with Client(server.mcp) as client:
        text = _error_text(await client.call_tool(tool, arguments))
    assert NOT_READY in text
    assert "Traceback" not in text


def test_a_python_caller_gets_the_connector_message_as_a_value_error(no_token):
    with pytest.raises(ValueError, match="no access token is saved"):
        server.list_book()


def _research_on(source: DataSource = DataSource.DEMO) -> None:
    save_settings(Settings(web_research=True, source=source))


async def test_research_account_is_off_until_the_csm_turns_it_on():
    async with Client(server.mcp) as client:
        text = _error_text(await client.call_tool("research_account", {"account_id": "acc-001"}))
    assert "Web research is off" in text
    assert "Data & privacy" in text


async def test_research_account_on_demo_data_returns_a_sample_and_sends_nothing(monkeypatch):
    def refuse():
        raise AssertionError("the MCP tool must not open a website connection in demo mode")

    monkeypatch.setattr(service, "_site_client_factory", refuse)
    _research_on()
    async with Client(server.mcp) as client:
        data = _data(await client.call_tool("research_account", {"account_id": "acc-001"}))
    assert data["sample"] is True
    assert data["account_id"] == "acc-001"
    assert data["articles"]
    assert set(data) >= {"website", "fetched_at", "site_title", "articles", "feed_url"}


def test_research_account_python_caller_gets_a_value_error_when_off():
    with pytest.raises(ValueError, match="Web research is off"):
        server.research_account("acc-001")


async def test_research_account_unknown_id_is_an_error_result():
    _research_on()
    async with Client(server.mcp) as client:
        text = _error_text(await client.call_tool("research_account", {"account_id": "acc-nope"}))
    assert "No account with id 'acc-nope'" in text
    assert "Use list_book" in text
