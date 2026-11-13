"""Claude's changes through the MCP server (mcp/writes.py): each McpWrites level (OFF refuses every
write, PROPOSE stores suggestions and never touches the journal, DIRECT previews and then writes
once), the request ids, the checks every write makes, the hourly cap, the activity log, the locked
book, and the read-only suggestions list. Runs on the demo book, whose journal and ledger are files
in the temp data folder from conftest, and on a hand-added account for Your data."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

from wardmind.core import audit, books, editing, journal, service, vault
from wardmind.core.proposals import ProposalKind, ProposalSource, ProposalStatus
from wardmind.core.settings import DataSource, McpWrites, Settings, save_settings
from wardmind.core.store import DEMO_JOURNAL_FILE
from wardmind.mcp import server, writes

REPO = Path(__file__).resolve().parents[1]

TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
ACCOUNT = "acc-001"
OTHER_ACCOUNT = "acc-002"
DEMO_BOOK = {"id": "demo", "label": "Demo data"}
SECRET = "Priya said the budget is frozen until March"
PASSWORD = "correct horse battery"
SUGGEST_ONLY = (
    "Claude may only suggest changes: use propose_* (the CSM accepts them on Today), "
    "or change Settings > Assistant."
)

PROPOSE_TOOLS = {"propose_follow_up", "propose_note", "propose_snooze"}
DIRECT_TOOLS = {"add_note", "add_follow_up", "complete_follow_up", "snooze_account"}

# One valid call per write tool. The direct ones are confirmed, so each would write if allowed.
WRITE_CALLS: list[tuple[str, dict[str, Any]]] = [
    (
        "propose_follow_up",
        {
            "account_id": ACCOUNT,
            "text": "Check the renewal plan",
            "due": (TODAY + timedelta(days=3)).isoformat(),
            "why": "No contact for 21 days",
            "request_id": "r-pf-1",
        },
    ),
    (
        "propose_note",
        {
            "account_id": ACCOUNT,
            "text": "Quarterly review booked for March",
            "why": "The review was agreed on the call",
            "request_id": "r-pn-1",
        },
    ),
    (
        "propose_snooze",
        {
            "account_id": ACCOUNT,
            "until": (TODAY + timedelta(days=7)).isoformat(),
            "why": "Waiting on their legal team",
            "request_id": "r-ps-1",
        },
    ),
    (
        "add_note",
        {
            "account_id": ACCOUNT,
            "text": "Called about the renewal",
            "request_id": "r-an-1",
            "confirm": True,
        },
    ),
    (
        "add_follow_up",
        {
            "account_id": ACCOUNT,
            "text": "Send the renewal quote",
            "due": (TODAY + timedelta(days=3)).isoformat(),
            "request_id": "r-af-1",
            "confirm": True,
        },
    ),
    (
        "complete_follow_up",
        {
            "account_id": ACCOUNT,
            "follow_up_id": "f-00000000",
            "request_id": "r-cf-1",
            "confirm": True,
        },
    ),
    (
        "snooze_account",
        {
            "account_id": ACCOUNT,
            "until": (TODAY + timedelta(days=7)).isoformat(),
            "request_id": "r-sa-1",
            "confirm": True,
        },
    ),
]


@pytest.fixture
def cheap_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encryption with a password, quick to set up: no Windows protection or slow key work."""
    monkeypatch.setattr(vault, "ARGON2", {"memory": 8, "iterations": 1, "lanes": 1})

    def protect(data: bytes) -> bytes:
        return b"DP" + bytes(reversed(data))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"DP"):
            raise vault.Damaged("Windows couldn't unlock Wardmind's key for this account.")
        return bytes(reversed(blob[2:]))

    monkeypatch.setattr(vault, "dpapi_protect", protect)
    monkeypatch.setattr(vault, "dpapi_unprotect", unprotect)


def _mode(mode: McpWrites, **changes: Any) -> None:
    save_settings(Settings(source=DataSource.DEMO, mcp_writes=mode, **changes))
    service.reset_connectors()


def _data(result: CallToolResult) -> Any:
    assert not result.is_error, result.content
    return result.structured_content


def _error(result: CallToolResult) -> str:
    """The tool's error message, without the "Error executing tool <name>:" the server adds."""
    assert result.is_error
    return re.sub(r"^Error executing tool \w+: ", "", result.content[0].text)


async def _call(tool: str, arguments: dict[str, Any]) -> CallToolResult:
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments)


def _demo_journal_file() -> Path:
    return service.get_store().root / DEMO_JOURNAL_FILE


def _demo_ledger_file() -> Path:
    return writes._ledger_path("demo")


def _notes(account_id: str = ACCOUNT) -> list[journal.Note]:
    return service.get_journal().for_account(account_id).notes


def _follow_ups(account_id: str = ACCOUNT) -> list[journal.FollowUp]:
    return service.get_journal().for_account(account_id).follow_ups


def _mode_for(tool: str) -> McpWrites:
    return McpWrites.PROPOSE if tool in PROPOSE_TOOLS else McpWrites.DIRECT


# --- Each level -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("tool", "arguments"), WRITE_CALLS)
async def test_off_refuses_every_write_tool_and_writes_nothing(
    tool: str, arguments: dict[str, Any]
) -> None:
    _mode(McpWrites.OFF)
    text = _error(await _call(tool, arguments))
    assert text == writes.OFF_MESSAGE
    assert "Settings > Assistant" in text
    assert not _demo_journal_file().exists()
    assert service.get_proposals().all() == []


async def test_list_suggestions_still_works_when_writes_are_off() -> None:
    _mode(McpWrites.OFF)
    data = _data(await _call("list_suggestions", {}))
    assert data["book"] == DEMO_BOOK
    assert data["suggestions"] == []


async def test_suggest_only_stores_suggestions_and_never_touches_the_journal() -> None:
    _mode(McpWrites.PROPOSE)
    for tool, arguments in WRITE_CALLS:
        if tool in PROPOSE_TOOLS:
            data = _data(await _call(tool, arguments))
            assert data["suggested"] is True
            assert data["book"] == DEMO_BOOK
    stored = service.get_proposals().all()
    assert [item.kind for item in stored] == [
        ProposalKind.FOLLOW_UP,
        ProposalKind.NOTE,
        ProposalKind.SNOOZE,
    ]
    assert all(item.source is ProposalSource.MCP for item in stored)
    assert all(item.status is ProposalStatus.OPEN for item in stored)
    assert [item.key for item in stored] == ["mcp:r-pf-1", "mcp:r-pn-1", "mcp:r-ps-1"]
    assert not _demo_journal_file().exists()
    assert service.get_journal().all() == {}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [(tool, arguments) for tool, arguments in WRITE_CALLS if tool in DIRECT_TOOLS],
)
async def test_suggest_only_refuses_the_direct_tools_even_as_a_preview(
    tool: str, arguments: dict[str, Any]
) -> None:
    _mode(McpWrites.PROPOSE)
    assert _error(await _call(tool, arguments)) == SUGGEST_ONLY
    preview = {**arguments, "confirm": False}
    assert _error(await _call(tool, preview)) == SUGGEST_ONLY
    assert not _demo_journal_file().exists()


async def test_a_direct_change_without_confirm_only_previews() -> None:
    _mode(McpWrites.DIRECT)
    data = _data(
        await _call(
            "add_note",
            {"account_id": ACCOUNT, "text": SECRET, "request_id": "r-preview-1"},
        )
    )
    assert SECRET in data["preview"]
    assert "confirm" in data["confirm"]
    assert data["book"] == DEMO_BOOK
    assert _notes() == []
    assert not _demo_journal_file().exists()
    assert not _demo_ledger_file().exists()


async def test_a_confirmed_note_is_written_once_with_the_agent_source() -> None:
    _mode(McpWrites.DIRECT)
    data = _data(
        await _call(
            "add_note",
            {
                "account_id": ACCOUNT,
                "text": "Called about the renewal",
                "request_id": "r-note-1",
                "confirm": True,
            },
        )
    )
    assert data["changed"] == "note"
    (note,) = _notes()
    assert note.id == data["id"]
    assert note.source is journal.NoteSource.AGENT
    assert note.kind is journal.TouchKind.NOTE
    assert note.happened_on == TODAY
    assert note.text == "Called about the renewal"


async def test_a_confirmed_follow_up_carries_its_request_as_the_rule() -> None:
    _mode(McpWrites.DIRECT)
    due = TODAY + timedelta(days=3)
    data = _data(
        await _call(
            "add_follow_up",
            {
                "account_id": ACCOUNT,
                "text": "Send the renewal quote",
                "due": due.isoformat(),
                "request_id": "r-fu-1",
                "confirm": True,
            },
        )
    )
    assert data["changed"] == "follow_up"
    (item,) = _follow_ups()
    assert item.id == data["id"]
    assert item.due == due
    assert item.rule == "agent:r-fu-1"


async def test_complete_follow_up_marks_the_one_follow_up_done_and_refuses_it_again() -> None:
    _mode(McpWrites.DIRECT)
    follow_up = service.get_journal().add_follow_up(
        ACCOUNT, "Send the quote", TODAY, now=NOW, today=TODAY
    )
    arguments = {"account_id": ACCOUNT, "follow_up_id": follow_up.id, "request_id": "r-done-1"}
    preview = _data(await _call("complete_follow_up", arguments))
    assert "Send the quote" in preview["preview"]
    assert _follow_ups()[0].status is journal.FollowUpStatus.OPEN
    done = _data(await _call("complete_follow_up", {**arguments, "confirm": True}))
    assert done["changed"] == "follow_up_done"
    assert _follow_ups()[0].status is journal.FollowUpStatus.DONE
    again = _error(
        await _call("complete_follow_up", {**arguments, "request_id": "r-done-2", "confirm": True})
    )
    assert again == writes.ALREADY_DONE_MESSAGE


async def test_complete_follow_up_refuses_an_id_that_is_not_in_the_account() -> None:
    _mode(McpWrites.DIRECT)
    text = _error(
        await _call(
            "complete_follow_up",
            {
                "account_id": ACCOUNT,
                "follow_up_id": "f-deadbeef",
                "request_id": "r-missing-1",
                "confirm": True,
            },
        )
    )
    assert text == writes.NOT_IN_ACCOUNT_MESSAGE


async def test_snooze_account_leaves_it_off_today_with_its_health() -> None:
    _mode(McpWrites.DIRECT)
    until = TODAY + timedelta(days=7)
    item = service.find_account(service.get_book(), ACCOUNT)
    assert item is not None
    preview = _data(
        await _call(
            "snooze_account",
            {"account_id": ACCOUNT, "until": until.isoformat(), "request_id": "r-snz-1"},
        )
    )
    assert f"until {until.isoformat()}" in preview["preview"]
    assert service.account_journal(ACCOUNT).snooze is None
    _data(
        await _call(
            "snooze_account",
            {
                "account_id": ACCOUNT,
                "until": until.isoformat(),
                "request_id": "r-snz-1",
                "confirm": True,
            },
        )
    )
    snooze = service.account_journal(ACCOUNT).snooze
    assert snooze is not None
    assert snooze.until == until
    assert snooze.health is item.health


# --- Request ids: the same request is the same answer ---------------------------------------------


async def test_the_same_request_id_twice_writes_one_entry_and_gives_the_same_answer() -> None:
    _mode(McpWrites.DIRECT)
    arguments = {
        "account_id": ACCOUNT,
        "text": "Once only",
        "request_id": "r-same",
        "confirm": True,
    }
    first = _data(await _call("add_note", arguments))
    second = _data(await _call("add_note", arguments))
    assert second == first
    assert len(_notes()) == 1


async def test_the_same_request_id_twice_in_suggest_only_stores_one_suggestion() -> None:
    _mode(McpWrites.PROPOSE)
    arguments = {
        "account_id": ACCOUNT,
        "text": "Book the review",
        "why": "Agreed",
        "request_id": "r-twice",
    }
    first = _data(await _call("propose_note", arguments))
    second = _data(await _call("propose_note", arguments))
    assert second == first
    assert len(service.get_proposals().all()) == 1


async def test_a_reused_request_id_for_a_different_change_is_refused() -> None:
    _mode(McpWrites.DIRECT)
    await _call(
        "add_note",
        {"account_id": ACCOUNT, "text": "First", "request_id": "r-reused", "confirm": True},
    )
    text = _error(
        await _call(
            "add_note",
            {
                "account_id": ACCOUNT,
                "text": "Something else",
                "request_id": "r-reused",
                "confirm": True,
            },
        )
    )
    assert text == writes.REUSED_ID_MESSAGE
    assert [note.text for note in _notes()] == ["First"]


async def test_an_earlier_answer_comes_back_even_after_the_account_has_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(McpWrites.DIRECT)
    arguments = {"account_id": ACCOUNT, "text": "Kept", "request_id": "r-gone", "confirm": True}
    first = _data(await _call("add_note", arguments))
    monkeypatch.setattr(service, "get_book", lambda *args, **kwargs: [])  # the account has left
    assert _data(await _call("add_note", arguments)) == first
    assert len(_notes()) == 1


@pytest.mark.parametrize(("tool", "arguments"), WRITE_CALLS)
async def test_a_request_id_must_be_letters_digits_dashes_or_underscores(
    tool: str, arguments: dict[str, Any]
) -> None:
    _mode(_mode_for(tool))
    text = _error(await _call(tool, {**arguments, "request_id": "bad id!"}))
    assert text == writes.REQUEST_ID_MESSAGE
    text = _error(await _call(tool, {**arguments, "request_id": "x" * 65}))
    assert text == writes.REQUEST_ID_MESSAGE
    assert not _demo_journal_file().exists()


# --- The checks every write makes ----------------------------------------------------------------


async def test_an_unknown_account_is_refused_with_how_to_find_ids() -> None:
    _mode(McpWrites.DIRECT)
    text = _error(
        await _call(
            "add_note",
            {
                "account_id": "acc-missing",
                "text": "Hello",
                "request_id": "r-unknown",
                "confirm": True,
            },
        )
    )
    assert "No account with id 'acc-missing'" in text
    assert "Use list_book" in text


@pytest.mark.parametrize(
    ("tool", "changes", "message"),
    [
        ("add_follow_up", {"due": "2020-01-01"}, "Pick today or a later date."),
        ("add_follow_up", {"due": "2100-01-01"}, "Pick a date within a year."),
        ("add_follow_up", {"due": "soon"}, writes.DATE_MESSAGE),
        ("add_follow_up", {"text": "x" * 201}, "Keep it under 200 characters."),
        ("add_follow_up", {"text": "Line\nbreak"}, "A follow-up can't contain a line break."),
        ("add_follow_up", {"text": "   "}, "Say what to follow up on."),
        ("add_note", {"text": "   "}, "Type the note."),
        ("add_note", {"text": "Bell\x07here"}, "A note can't contain control characters."),
        ("add_note", {"text": "Hidden​space"}, "A note can't contain hidden formatting characters."),
        ("snooze_account", {"until": TODAY.isoformat()}, "Pick a date after today."),
        (
            "snooze_account",
            {"until": (TODAY + timedelta(days=91)).isoformat()},
            "Snooze for 90 days at most.",
        ),
        ("propose_note", {"text": "x" * 501}, f"Keep a suggested note under {500} characters."),
        ("propose_follow_up", {"why": ""}, "Say why in up to 200 characters."),
        ("propose_snooze", {"why": "y" * 201}, "Say why in up to 200 characters."),
    ],
)
async def test_bad_dates_and_text_are_refused_and_nothing_is_written(
    tool: str, changes: dict[str, str], message: str
) -> None:
    _mode(_mode_for(tool))
    base = next(arguments for name, arguments in WRITE_CALLS if name == tool)
    arguments = {**base, **changes, "request_id": "r-bad-1"}
    assert _error(await _call(tool, arguments)) == message
    assert not _demo_journal_file().exists()
    assert service.get_proposals().all() == []


async def test_a_suggestion_is_refused_plainly_when_the_account_is_at_its_cap() -> None:
    _mode(McpWrites.PROPOSE)
    for index in range(3):
        _data(
            await _call(
                "propose_note",
                {
                    "account_id": ACCOUNT,
                    "text": f"Note {index}",
                    "why": "Agreed",
                    "request_id": f"r-cap-{index}",
                },
            )
        )
    text = _error(
        await _call(
            "propose_note",
            {
                "account_id": ACCOUNT,
                "text": "One too many",
                "why": "Agreed",
                "request_id": "r-cap-4",
            },
        )
    )
    assert text == "This account already has 3 open suggestions. Accept or dismiss one first."


async def test_suggestions_switched_off_refuse_the_propose_tools() -> None:
    _mode(McpWrites.PROPOSE, suggestions=False)
    text = _error(
        await _call(
            "propose_note",
            {"account_id": ACCOUNT, "text": "Hello", "why": "Agreed", "request_id": "r-off-1"},
        )
    )
    assert text == writes.SUGGESTIONS_OFF_MESSAGE
    assert service.get_proposals().all() == []


async def test_a_locked_book_refuses_writes_and_says_so(cheap_vault: None) -> None:
    _mode(McpWrites.DIRECT)
    vault.enable(password=PASSWORD)
    vault.lock()
    text = _error(
        await _call(
            "add_note",
            {"account_id": ACCOUNT, "text": "Hello", "request_id": "r-locked", "confirm": True},
        )
    )
    assert text == writes.LOCKED_MESSAGE
    assert "Wardmind is locked" in text


async def test_your_data_gets_the_same_checks_and_its_own_ledger() -> None:
    save_settings(Settings(source=DataSource.MINE, mcp_writes=McpWrites.DIRECT))
    service.reset_connectors()
    account = editing.create_account(
        service.get_store(),
        {
            "name": "Acme Robotics",
            "arr": "$184,000",
            "renewal_date": (TODAY + timedelta(days=200)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    arguments = {
        "account_id": account.id,
        "text": "Met the team",
        "request_id": "r-mine-1",
        "confirm": True,
    }
    data = _data(await _call("add_note", arguments))
    assert data["book"]["id"] == books.DEFAULT_BOOK
    assert _data(await _call("add_note", arguments)) == data
    notes = service.get_journal().for_account(account.id).notes
    assert [note.source for note in notes] == [journal.NoteSource.AGENT]
    # The first book's folder also holds demo data's ledger; answers are kept under their scope.
    assert writes._ledger_path("mine").exists()
    assert [
        record.scope for record in writes._load_ledger(writes._ledger_path("mine")).records
    ] == ["mine"]


# --- The hourly cap -------------------------------------------------------------------------------


async def test_thirty_changes_an_hour_then_a_clear_refusal_until_one_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(McpWrites.DIRECT)
    clock = [NOW]
    monkeypatch.setattr(writes, "_now", lambda: clock[0])
    for index in range(writes.MAX_WRITES):
        await _call(
            "add_note",
            {
                "account_id": ACCOUNT,
                "text": f"Note {index}",
                "request_id": f"r-rate-{index}",
                "confirm": True,
            },
        )
    refused = {
        "account_id": ACCOUNT,
        "text": "One more",
        "request_id": "r-rate-over",
        "confirm": True,
    }
    text = _error(await _call("add_note", refused))
    assert text.startswith("Claude has made 30 changes to this book in the last hour.")
    assert "The next one can be made after" in text
    # A preview uses no room, but it is refused too while the hour is full.
    assert _error(await _call("add_note", {**refused, "confirm": False})) == text
    assert len(_notes()) == writes.MAX_WRITES
    clock[0] = NOW + timedelta(hours=1, minutes=1)
    later = _data(await _call("add_note", {**refused, "request_id": "r-rate-later"}))
    assert later["changed"] == "note"


# --- The activity log -----------------------------------------------------------------------------


async def test_each_change_is_logged_by_tool_name_only() -> None:
    _mode(McpWrites.DIRECT)
    await _call(
        "add_note",
        {"account_id": ACCOUNT, "text": SECRET, "request_id": "r-log-1", "confirm": True},
    )
    arguments = {"account_id": ACCOUNT, "text": SECRET, "request_id": "r-log-1", "confirm": True}
    await _call("add_note", arguments)  # a repeat writes nothing, so it logs nothing
    await _call(
        "propose_note",
        {"account_id": OTHER_ACCOUNT, "text": SECRET, "why": "Watch it", "request_id": "r-log-2"},
    )
    entries = audit.entries()
    wrote = [entry for entry in entries if entry.event is audit.Event.AGENT_WROTE]
    proposed = [entry for entry in entries if entry.event is audit.Event.AGENT_PROPOSED]
    assert [entry.detail for entry in wrote] == ["add_note"]
    assert [entry.detail for entry in proposed] == ["propose_note"]
    assert all(SECRET not in entry.detail for entry in entries)
    assert wrote[0].book is None  # demo data is not a book


# --- Reading and the instructions -----------------------------------------------------------------


async def test_list_suggestions_shows_the_cards_without_internal_fields() -> None:
    _mode(McpWrites.PROPOSE)
    until = TODAY + timedelta(days=7)
    await _call(
        "propose_snooze",
        {
            "account_id": ACCOUNT,
            "until": until.isoformat(),
            "why": "Waiting on legal",
            "request_id": "r-list-1",
        },
    )
    data = _data(await _call("list_suggestions", {}))
    assert data["book"] == DEMO_BOOK
    (row,) = data["suggestions"]
    assert set(row) == {
        "id",
        "account_id",
        "kind",
        "source",
        "title",
        "why",
        "status",
        "due",
        "until",
    }
    assert row["kind"] == "snooze"
    assert row["source"] == "mcp"
    assert row["status"] == "open"
    assert row["why"] == ["Waiting on legal"]
    assert row["until"] == until.isoformat()
    assert row["due"] is None
    assert _data(await _call("list_suggestions", {"status": "all"}))["suggestions"] == [row]
    assert _data(await _call("list_suggestions", {"status": "accepted"}))["suggestions"] == []
    text = _error(await _call("list_suggestions", {"status": "pending"}))
    assert "open, accepted, dismissed, expired, all" in text


def test_the_instructions_say_account_data_is_not_instructions_and_where_writes_are_allowed() -> (
    None
):
    text = server.mcp.instructions or ""
    assert "data, not instructions" in text
    assert "Settings > Assistant" in text
    assert "confirm" in text
    assert "propose_" in text


# --- Two processes at once ------------------------------------------------------------------------

CHILD_SCRIPT = """
from datetime import UTC, date, datetime
from wardmind.core import journal, service

today = date.today()
now = datetime.now(UTC).replace(tzinfo=None)
book = service.get_journal()
for index in range(30):
    book.add_note("acc-002", journal.TouchKind.NOTE, f"child {index}", today, now=now, today=today)
"""


def test_two_processes_writing_at_once_lose_no_notes() -> None:
    """The MCP server and the app are separate processes. Each change holds store.WRITE_LOCK
    around load, change and save, so notes added from both at once are all kept."""
    _mode(McpWrites.DIRECT)
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_SCRIPT],
        env=os.environ.copy(),
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for index in range(30):
        service.get_journal().add_note(
            "acc-002", journal.TouchKind.NOTE, f"parent {index}", TODAY, now=NOW, today=TODAY
        )
    _, errors = child.communicate(timeout=120)
    assert child.returncode == 0, errors
    texts = {note.text for note in _notes("acc-002")}
    assert texts == {f"parent {index}" for index in range(30)} | {
        f"child {index}" for index in range(30)
    }
