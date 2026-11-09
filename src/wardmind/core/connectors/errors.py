"""Errors a connector raises when it can't produce the book.

CONTRACT FILE: every surface (web, MCP, CLI) catches ConnectorError and shows `message` as is,
so messages must be plain English, say what to do next, and never contain a token.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """The book couldn't be loaded. `message` is safe to show to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConnectionNotReady(ConnectorError):
    """A live source is selected but can't be used yet (for example, no token is saved)."""
