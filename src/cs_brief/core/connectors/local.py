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
        self._store = store

    def list_accounts(self) -> list[Account]:
        return list(self._store.manual().accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        """Readings whose account_id is one of this connector's accounts."""
        data = self._store.manual()
        ids = {account.id for account in data.accounts}
        return [
            signal
            for signal in data.signals
            if signal.account_id in ids and (account_id is None or signal.account_id == account_id)
        ]


class SpreadsheetConnector:
    """One imported spreadsheet: its latest accounts and accumulated readings."""

    name = "spreadsheet"

    def __init__(self, store: LocalStore, connection_id: str) -> None:
        self._store = store
        self._connection_id = connection_id

    def list_accounts(self) -> list[Account]:
        """Empty before the first import."""
        snapshot = self._store.snapshot(self._connection_id)
        return [] if snapshot is None else list(snapshot.accounts)

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        snapshot = self._store.snapshot(self._connection_id)
        signals = [] if snapshot is None else snapshot.signals
        if account_id is None:
            return list(signals)
        return [signal for signal in signals if signal.account_id == account_id]
