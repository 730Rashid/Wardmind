"""One book from several connections, with the CSM's links, edits and hidden accounts applied.

To everything downstream (scoring, briefs, web, MCP) this is just a Connector.

Order of work on each load:
1. Ask every member connector for accounts and signals. A member that raises ConnectorError is
   skipped and recorded in `problems`; the book is built from the rest.
2. Drop hidden accounts (store.hidden()).
3. Apply confirmed links: each LinkGroup becomes one account with the primary's id. Fields come
   from the primary; a field the primary lacks (None domain, "Unknown" tier, "Unassigned" owner,
   0 ARR, 0-total milestones) is filled from the other members in group order. Every member's
   signals are re-keyed to the primary id. Members not present this load are ignored.
4. Add hand-entered readings from store.manual().signals for any account id, re-keyed through
   links.
5. Apply edits with the "until the source changes" rule (see store.FieldEdit): an edit whose
   base_value still equals the merged source value is applied; otherwise the edit is removed
   from the store and a Notice is added: "{connection name} changed {field label} to {new},
   replacing your edit of {old}." Values compare as their display strings (dates ISO, numbers
   without trailing ".0"). Accounts from the manual connection are edited in place by
   editing.py, so they never carry FieldEdits.

The result is cached until refresh(), which also refreshes members that have refresh().

CONTRACT FILE: models, signatures and docstrings are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from cs_brief.core.connectors.base import Connector
from cs_brief.core.matching import DuplicateSuggestion
from cs_brief.core.models import Account, Signal
from cs_brief.core.store import ConnectionConfig, EditValue, FieldEdit, LocalStore


class ConnectionProblem(BaseModel):
    connection_id: str
    name: str  # the connection's display name
    message: str  # ConnectorError.message


class Provenance(BaseModel):
    """Where an account in the combined book came from, for the account page and edit form."""

    account_id: str
    member_ids: list[str]  # the primary first; one id when not linked
    connections: list[str]  # display names of the connections the members came from, in order
    editable_in_place: bool  # True for a manual-only account (edits change it directly)
    edits: dict[str, FieldEdit]  # applied edits, by field
    source_values: dict[str, EditValue]  # every EDITABLE_FIELDS value before edits


class CombinedConnector:
    name = "combined"

    def __init__(
        self,
        members: list[tuple[ConnectionConfig, Connector]],
        store: LocalStore,
        *,
        today: date | None = None,
    ) -> None:
        raise NotImplementedError

    def list_accounts(self) -> list[Account]:
        raise NotImplementedError

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        raise NotImplementedError

    @property
    def problems(self) -> list[ConnectionProblem]:
        """Members that failed on the last load, in member order."""
        raise NotImplementedError

    def provenance(self, account_id: str) -> Provenance | None:
        raise NotImplementedError

    def duplicate_suggestions(self) -> list[DuplicateSuggestion]:
        """matching.suggest_duplicates over the loaded accounts (after links, before edits)."""
        raise NotImplementedError

    def refresh(self) -> None:
        raise NotImplementedError
