"""cs-brief command line: `cs-brief score`, `cs-brief mcp`, `cs-brief ui`."""

from __future__ import annotations

from datetime import date
from typing import Annotated

import typer

from cs_brief.core import service
from cs_brief.core.briefs import WeeklyBrief, weekly_brief
from cs_brief.core.models import Health, Motion, ScoredAccount

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

_NAME_WIDTH = 28  # longer account names are cut to this width


@app.command()
def score(
    top: Annotated[int, typer.Option(min=1, help="How many accounts to show.")] = 10,
    motion: Annotated[
        Motion | None,
        typer.Option(help="Show one motion only: churn, renewal or onboarding."),
    ] = None,
) -> None:
    """Print the top of the ranked book with each account's top reason."""
    as_of = date.today()
    book = service.filter_book(service.get_book(as_of=as_of), motion=motion)
    brief = weekly_brief(book, as_of=as_of, top_n=top)
    typer.echo(_header(brief))
    if not brief.top:
        typer.echo(
            "No accounts to show."
            if motion is None
            else f"No accounts are in {motion.value} right now."
        )
        return
    columns = _line("#", "Health", "Priority", "Account", "Motion", "ARR", "Why")
    typer.echo(columns)
    typer.echo("-" * len(columns))
    for rank, item in enumerate(brief.top, start=1):
        typer.echo(_row(rank, item))


def _header(brief: WeeklyBrief) -> str:
    at_risk = brief.counts_by_health[Health.AT_RISK]
    account_word = "account" if brief.total_accounts == 1 else "accounts"
    return (
        f"{brief.total_accounts} {account_word}, {at_risk} at risk, "
        f"${brief.arr_at_risk:,.0f} ARR at risk"
    )


def _row(rank: int, item: ScoredAccount) -> str:
    return _line(
        str(rank),
        item.health.label,
        f"{item.priority:.1f}",
        _cut(item.account.name),
        item.motion.label,
        f"${item.account.arr:,.0f}",
        item.top_reason,
    )


def _line(
    rank: str, health: str, priority: str, account: str, motion: str, arr: str, why: str
) -> str:
    """One table line with fixed column widths."""
    fixed = f"{rank:>3}  {health:<7}  {priority:>8}  {account:<{_NAME_WIDTH}}  {motion:<10}"
    return f"{fixed}  {arr:>10}  {why}".rstrip()


def _cut(name: str) -> str:
    return name if len(name) <= _NAME_WIDTH else name[: _NAME_WIDTH - 3] + "..."


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio."""
    raise typer.Exit("Not built yet (task T4).")


@app.command()
def ui(
    port: int = typer.Option(8000, help="Port on 127.0.0.1."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser tab."),
) -> None:
    """Start the local web dashboard on 127.0.0.1."""
    raise typer.Exit("Not built yet (task T5).")
