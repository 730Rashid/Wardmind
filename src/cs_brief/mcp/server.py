"""MCP server: a CSM's prioritised book of business, served over stdio.

Thin wrappers only. Each tool calls cs_brief.core.service and cs_brief.core.briefs, and the book
is loaded on every call so edits to config/scoring.yaml apply without a restart. Nothing here may
write to stdout, because the stdio transport uses it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from cs_brief.core import briefs, service
from cs_brief.core.models import Motion, ScoredAccount

_INSTRUCTIONS = (
    "Serves a Customer Success Manager's prioritised book of business: each account's health, "
    "priority, the plain-English reasons it needs attention, and a suggested next action. "
    "It currently serves demo data. "
    "Get account ids from list_book before calling get_account or explain_score."
)

_MIN_LIST_LIMIT = 1
_MAX_LIST_LIMIT = 200
_DEFAULT_LIST_LIMIT = 20
_MIN_TOP_N = 1
_MAX_TOP_N = 50

mcp = MCPServer("cs-brief", instructions=_INSTRUCTIONS)


class _BadRequest(ToolError, ValueError):
    """A caller mistake. MCPServer hides the text of plain exceptions from the client, so this is
    a ToolError, which keeps its message. It is still a ValueError for Python callers."""


@mcp.tool()
def list_book(
    motion: str | None = None,
    search: str | None = None,
    limit: int = _DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """List accounts in priority order, most urgent first.

    Rank is the account's position in the whole book, so it does not change when you filter.
    Args:
        motion: Only this motion: churn, renewal or onboarding. Case-insensitive.
        search: Only accounts whose name contains this text. Case-insensitive.
        limit: How many rows to return, from 1 to 200. Defaults to 20.
    """
    selected = _parse_motion(motion)
    today = date.today()
    book = service.get_book(as_of=today)
    ranks = {item.account.id: rank for rank, item in enumerate(book, start=1)}
    kept = service.filter_book(book, motion=selected, search=search)
    limit = _clamp(limit, _MIN_LIST_LIMIT, _MAX_LIST_LIMIT)
    return [_row(ranks[item.account.id], item) for item in kept[:limit]]


@mcp.tool()
def get_account(account_id: str) -> dict[str, Any]:
    """Everything about one account: its details, health, risk, priority, reasons and action.

    Args:
        account_id: The account id from list_book.
    """
    return _find(account_id).model_dump(mode="json")


@mcp.tool()
def explain_score(account_id: str) -> dict[str, Any]:
    """Why one account is ranked where it is, in plain English, with a one-or-two sentence summary.

    Args:
        account_id: The account id from list_book.
    """
    return briefs.explain(_find(account_id)).model_dump(mode="json")


@mcp.tool()
def weekly_brief(top_n: int = 10) -> dict[str, Any]:
    """This week's brief: totals, counts by health and motion, call-outs, and the top accounts.

    Args:
        top_n: How many accounts to include in "top", from 1 to 50. Defaults to 10.
    """
    today = date.today()
    brief = briefs.weekly_brief(
        service.get_book(as_of=today),
        as_of=today,
        top_n=_clamp(top_n, _MIN_TOP_N, _MAX_TOP_N),
    )
    data = brief.model_dump(mode="json")
    data["top"] = [_row(rank, item) for rank, item in enumerate(brief.top, start=1)]
    return data


def run() -> None:
    """Serve the tools over stdio until the client disconnects."""
    mcp.run("stdio")


def _find(account_id: str) -> ScoredAccount:
    item = service.find_account(service.get_book(), account_id)
    if item is None:
        raise _BadRequest(f"No account with id '{account_id}'. Use list_book to find ids.")
    return item


def _parse_motion(motion: str | None) -> Motion | None:
    """None means every motion. Anything else must be a Motion value, in any case."""
    if motion is None:
        return None
    try:
        return Motion(motion.lower())
    except ValueError:
        valid = ", ".join(item.value for item in Motion)
        raise _BadRequest(f"Unknown motion '{motion}'. Use one of: {valid}.") from None


def _row(rank: int, item: ScoredAccount) -> dict[str, Any]:
    """The compact row shared by list_book and weekly_brief."""
    return {
        "rank": rank,
        "id": item.account.id,
        "name": item.account.name,
        "health": item.health.value,
        "priority": item.priority,
        "risk": item.risk,
        "motion": item.motion.value,
        "arr": item.account.arr,
        "top_reason": item.top_reason,
        "suggested_action": item.suggested_action,
    }


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))
