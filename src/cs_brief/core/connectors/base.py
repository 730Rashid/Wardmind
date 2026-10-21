"""Connector protocol: where accounts and signals come from (demo data now, HubSpot later).

CONTRACT FILE: implementers escalate before editing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cs_brief.core.models import Account, Signal


@runtime_checkable
class Connector(Protocol):
    name: str  # short id shown in the UI, e.g. "demo" or "hubspot"

    def list_accounts(self) -> list[Account]: ...

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        """All signals, or only one account's when account_id is given. Order is not guaranteed."""
        ...
