"""Connectors over the local store: accounts added by hand, and imported spreadsheets.

CONTRACT FILE: signatures and docstrings are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from cs_brief.core.models import Account, Signal
from cs_brief.core.store import LocalStore


class ManualConnector:
    """Accounts the CSM added by hand. Hand-entered readings for *other* connections' accounts
    are not served here; CombinedConnector adds those to whichever account they belong to."""

    name = "manual"

    def __init__(self, store: LocalStore) -> None:
        raise NotImplementedError

    def list_accounts(self) -> list[Account]:
        raise NotImplementedError

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        """Readings whose account_id is one of this connector's accounts."""
        raise NotImplementedError


class SpreadsheetConnector:
    """One imported spreadsheet: its latest accounts and accumulated readings."""

    name = "spreadsheet"

    def __init__(self, store: LocalStore, connection_id: str) -> None:
        raise NotImplementedError

    def list_accounts(self) -> list[Account]:
        """Empty before the first import."""
        raise NotImplementedError

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        raise NotImplementedError
