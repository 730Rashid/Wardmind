from __future__ import annotations

import re
from datetime import date

import pytest
from typer.testing import CliRunner

from cs_brief import cli
from cs_brief.core import service
from cs_brief.core.config import ScoringConfig
from cs_brief.core.connectors.demo import DemoConnector
from cs_brief.core.connectors.errors import ConnectionNotReady
from cs_brief.core.models import Health, Motion, ScoredAccount

AS_OF = date(2026, 10, 19)
runner = CliRunner()
# Rank, health, priority, account, motion and ARR columns of one table row.
ROW = re.compile(
    r"^\s*(\d+)\s+(At risk|Watch|Healthy)\s+(\d+\.\d)\s+(.+?)\s+(Onboarding|Renewal|Churn)\s+\$"
)


class _PinnedDate(date):
    """A date whose today() is AS_OF, so the motions match the demo data anchored there."""

    @classmethod
    def today(cls) -> date:
        return AS_OF


class _EmptyConnector:
    name = "empty"

    def list_accounts(self):
        return []

    def list_signals(self, account_id=None):
        return []


@pytest.fixture(autouse=True)
def anchored_demo(monkeypatch):
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector(today=AS_OF))
    monkeypatch.setattr(cli, "date", _PinnedDate)


def _book() -> list[ScoredAccount]:
    return service.get_book(
        connector=DemoConnector(today=AS_OF), config=ScoringConfig(), as_of=AS_OF
    )


def _header(book: list[ScoredAccount]) -> str:
    at_risk = [item for item in book if item.health is Health.AT_RISK]
    arr = sum(item.account.arr for item in at_risk)
    return f"{len(book)} accounts, {len(at_risk)} at risk, ${arr:,.0f} ARR at risk"


def _rows(output: str) -> list[re.Match[str]]:
    return [match for match in map(ROW.match, output.splitlines()) if match]


def test_score_prints_the_header_and_ten_rows_by_default():
    result = runner.invoke(cli.app, ["score"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == _header(_book())
    assert len(_rows(result.output)) == 10
    assert result.output.isascii()


def test_rows_follow_book_order_and_show_the_top_reason():
    result = runner.invoke(cli.app, ["score", "--top", "3"])
    assert result.exit_code == 0, result.output
    book = _book()[:3]
    rows = _rows(result.output)
    assert len(rows) == 3
    assert [m.group(1) for m in rows] == ["1", "2", "3"]
    assert [m.group(2) for m in rows] == [item.health.label for item in book]
    assert [m.group(4) for m in rows] == [item.account.name for item in book]
    assert [m.group(5) for m in rows] == [item.motion.label for item in book]
    for item in book:
        assert item.top_reason in result.output


def test_motion_shows_only_that_motion():
    result = runner.invoke(cli.app, ["score", "--motion", "renewal"])
    assert result.exit_code == 0, result.output
    renewals = [item for item in _book() if item.motion is Motion.RENEWAL]
    rows = _rows(result.output)
    assert result.output.splitlines()[0] == _header(renewals)
    assert [m.group(5) for m in rows] == ["Renewal"] * 10
    assert [m.group(4) for m in rows] == [item.account.name for item in renewals[:10]]


def test_an_unknown_motion_is_a_usage_error():
    result = runner.invoke(cli.app, ["score", "--motion", "sales"])
    assert result.exit_code == 2
    assert "sales" in result.output
    assert "renewal" in result.output


def test_top_must_be_at_least_one():
    result = runner.invoke(cli.app, ["score", "--top", "0"])
    assert result.exit_code == 2


def test_an_empty_book_says_so(monkeypatch):
    monkeypatch.setattr(service, "get_connector", lambda: _EmptyConnector())
    result = runner.invoke(cli.app, ["score"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "0 accounts, 0 at risk, $0 ARR at risk",
        "No accounts to show.",
    ]


def test_a_connector_error_goes_to_stderr_and_exits_with_one(monkeypatch):
    message = (
        "HubSpot is selected, but no access token is saved. "
        "Add one on the Connections page, or switch to demo data."
    )

    def not_ready():
        raise ConnectionNotReady(message)

    monkeypatch.setattr(service, "get_connector", not_ready)
    result = runner.invoke(cli.app, ["score"])
    assert result.exit_code == 1
    assert result.stderr.strip() == message
    assert result.stdout == ""


def test_an_empty_motion_says_which_motion_is_empty(monkeypatch):
    monkeypatch.setattr(service, "get_connector", lambda: _EmptyConnector())
    result = runner.invoke(cli.app, ["score", "--motion", "churn"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "No accounts are in churn right now."
