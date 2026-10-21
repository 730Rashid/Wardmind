"""cs-brief command line: `cs-brief score`, `cs-brief mcp`, `cs-brief ui`."""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


@app.command()
def score(top: int = typer.Option(10, help="How many accounts to show.")) -> None:
    """Print the top of the ranked book with each account's top reason."""
    raise typer.Exit("Not built yet (task T3).")


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
