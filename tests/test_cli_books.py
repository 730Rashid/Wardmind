"""`wardmind books`, and `--book` on `wardmind mcp` and `wardmind score` (cli.py, core/books.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from wardmind import cli
from wardmind.core import books, service
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.mcp import server

runner = CliRunner()
UNKNOWN_BOOK = "b-00000000"
UNKNOWN_MESSAGE = f"There is no book with the id {UNKNOWN_BOOK}. `wardmind books` lists them."


@pytest.fixture(autouse=True)
def no_pin_left_behind() -> Iterator[None]:
    """A pin made by --book never reaches the next test."""
    yield
    books.pin(None)


@pytest.fixture
def run_recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the MCP server's run, so no test starts it. Each call records the open book."""
    seen: list[str] = []
    monkeypatch.setattr(server, "run", lambda: seen.append(books.active_id()))
    return seen


# --- wardmind books ------------------------------------------------------------------------------


def test_one_book_is_listed_as_open_and_your_data_is_said_to_be_showing():
    save_settings(Settings(source=DataSource.MINE))
    result = runner.invoke(cli.app, ["books"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "default  Book 1  (open)",
        "Your data is showing, not demo data.",
    ]


def test_two_books_are_both_listed_and_only_the_open_one_is_marked():
    second = books.add("Book 2")
    books.switch(second.id)
    save_settings(Settings(source=DataSource.MINE))
    result = runner.invoke(cli.app, ["books"])
    assert result.exit_code == 0, result.output
    first_line, second_line, _ = result.output.splitlines()
    assert first_line.split() == ["default", "Book", "1"]
    assert second_line.split() == [second.id, "Book", "2", "(open)"]


def test_demo_data_showing_is_said_on_the_last_line():
    save_settings(Settings(source=DataSource.DEMO))
    result = runner.invoke(cli.app, ["books"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "Demo data is showing instead of these books."


# --- --book on wardmind mcp ----------------------------------------------------------------------


def test_mcp_with_an_unknown_book_exits_two_and_does_not_start_the_server(run_recorded):
    result = runner.invoke(cli.app, ["mcp", "--book", UNKNOWN_BOOK])
    assert result.exit_code == 2
    assert result.stderr.strip() == UNKNOWN_MESSAGE
    assert run_recorded == []


def test_mcp_with_a_book_pins_the_server_to_that_book(run_recorded):
    second = books.add("Book 2")
    result = runner.invoke(cli.app, ["mcp", "--book", second.id])
    assert result.exit_code == 0, result.output
    assert run_recorded == [second.id]  # "default" is still the open book


def test_mcp_without_a_book_follows_the_open_book(run_recorded):
    second = books.add("Book 2")
    books.switch(second.id)
    result = runner.invoke(cli.app, ["mcp"])
    assert result.exit_code == 0, result.output
    assert run_recorded == [second.id]
    assert books.pinned() is None


# --- --book on wardmind score --------------------------------------------------------------------


def test_score_with_an_unknown_book_exits_two_without_scoring(monkeypatch):
    def refuse(**kwargs):
        raise AssertionError("score must not run for a book that doesn't exist")

    monkeypatch.setattr(service, "get_book", refuse)
    result = runner.invoke(cli.app, ["score", "--book", UNKNOWN_BOOK])
    assert result.exit_code == 2
    assert result.stderr.strip() == UNKNOWN_MESSAGE


def test_score_with_a_book_scores_that_book(monkeypatch):
    second = books.add("Book 2")
    scored_while: list[str] = []
    real_get_book = service.get_book

    def recording_get_book(**kwargs):
        scored_while.append(books.active_id())
        return real_get_book(**kwargs)

    monkeypatch.setattr(service, "get_book", recording_get_book)
    result = runner.invoke(cli.app, ["score", "--book", second.id])
    assert result.exit_code == 0, result.output
    assert scored_while == [second.id]
    assert "ARR at risk" in result.output.splitlines()[0]
