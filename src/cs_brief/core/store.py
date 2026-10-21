"""The local store: everything a CSM adds or changes, kept on this computer only.

Customer data never goes in the project's config/ folder (it sits in a synced OneDrive folder and
in git). The store lives in data_dir(): CS_BRIEF_DATA_DIR if set (tests), else
secrets.secrets_dir() / "data" (on Windows %LOCALAPPDATA%\\cs-brief\\data).

One JSON file per concern, each written atomically (config.write_atomic):
- connections.json  list[ConnectionConfig]          which sources make up the book, in order
- manual.json       ManualData                      accounts added by hand, and hand-entered
                                                    signal readings (for any account id)
- sheets/<id>.json  ImportSnapshot                  the latest import of one spreadsheet
                                                    connection, plus its accumulated readings
- edits.json        dict[account_id, dict[field, FieldEdit]]   edits over synced accounts
- notices.json      list[Notice]                    "HubSpot updated this" messages
- links.json        LinkData                        confirmed duplicates and dismissed pairs
- hidden.json       list[str]                       account ids hidden from the book

A missing or unreadable file reads as empty; the store never raises for bad content, it logs
nothing and returns the empty value, so a hand-edited file can't break the UI.

CONTRACT FILE: models, constants, signatures and docstrings are fixed; implementers escalate
before changing them.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from cs_brief.core.models import Account, Signal

ENV_DATA_DIR = "CS_BRIEF_DATA_DIR"
MANUAL_CONNECTION_ID = "manual"
HUBSPOT_CONNECTION_ID = "hubspot"

# Account fields a CSM can edit. Milestones are edited as three numbers.
EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "domain",
    "arr",
    "tier",
    "owner",
    "start_date",
    "renewal_date",
    "milestones_total",
    "milestones_done",
    "milestones_overdue",
)

FIELD_LABELS: dict[str, str] = {
    "name": "Name",
    "domain": "Website",
    "arr": "ARR",
    "tier": "Tier",
    "owner": "Owner",
    "start_date": "Customer since",
    "renewal_date": "Renewal date",
    "milestones_total": "Onboarding milestones",
    "milestones_done": "Milestones done",
    "milestones_overdue": "Milestones overdue",
}

EditValue = str | float | int | date | None


class ConnectionKind(StrEnum):
    HUBSPOT = "hubspot"
    SPREADSHEET = "spreadsheet"
    MANUAL = "manual"


class ConnectionConfig(BaseModel):
    id: str  # "hubspot", "manual", or "sheet-<n>" for spreadsheets
    kind: ConnectionKind
    name: str  # what the CSM sees: "HubSpot", "Added by hand", or a name they chose
    enabled: bool = True
    created_at: datetime
    # Spreadsheet only: the remembered column mapping (import field key -> column header),
    # the date order the CSM confirmed, and when/what was last imported.
    mapping: dict[str, str] = Field(default_factory=dict)
    date_order: str | None = None  # "dmy", "mdy" or "ymd"
    last_imported_at: datetime | None = None
    last_file_name: str | None = None


class ManualData(BaseModel):
    accounts: list[Account] = Field(default_factory=list)  # ids start with "m-"
    signals: list[Signal] = Field(default_factory=list)  # source="manual"; any account id


class ImportRecord(BaseModel):
    imported_at: datetime
    file_name: str
    accounts: int
    skipped_rows: int


class ImportSnapshot(BaseModel):
    connection_id: str
    accounts: list[Account] = Field(default_factory=list)  # the latest import replaces these
    signals: list[Signal] = Field(default_factory=list)  # accumulated across imports
    history: list[ImportRecord] = Field(default_factory=list)  # newest last


class FieldEdit(BaseModel):
    """A CSM's edit over a value that came from a connection.

    It applies while the connection still reports `base_value` (the value it had when the edit
    was made). Once the connection reports something else, the connection wins: the edit is
    dropped and a Notice tells the CSM. That is the "until the source changes" rule.
    """

    value: EditValue
    base_value: EditValue
    edited_at: datetime


class Notice(BaseModel):
    id: str  # short random hex
    account_id: str
    field: str  # one of EDITABLE_FIELDS
    message: str  # plain English, e.g. "HubSpot changed ARR to $140,000, replacing your edit
    #               of $135,000."
    created_at: datetime
    dismissed: bool = False


class LinkGroup(BaseModel):
    primary: str  # the account id the merged account keeps (its URL and name)
    members: list[str]  # every linked account id, primary included, at least 2


class LinkData(BaseModel):
    groups: list[LinkGroup] = Field(default_factory=list)
    dismissed: list[tuple[str, str]] = Field(default_factory=list)  # pairs marked "not the same",
    #                                                                each sorted


def data_dir() -> Path:
    """CS_BRIEF_DATA_DIR if set, else secrets.secrets_dir() / "data". Not created here."""
    raise NotImplementedError


class LocalStore:
    """Typed access to the files above. Every write is atomic; reads never raise."""

    def __init__(self, root: Path | None = None) -> None:
        raise NotImplementedError

    @property
    def root(self) -> Path:
        raise NotImplementedError

    # Connections -----------------------------------------------------------------------------
    def connections(self) -> list[ConnectionConfig]:
        raise NotImplementedError

    def get_connection(self, connection_id: str) -> ConnectionConfig | None:
        raise NotImplementedError

    def add_connection(self, kind: ConnectionKind, name: str) -> ConnectionConfig:
        """HUBSPOT and MANUAL use their fixed ids and are returned as-is if they already exist.
        SPREADSHEET gets the next free "sheet-<n>" id."""
        raise NotImplementedError

    def update_connection(self, config: ConnectionConfig) -> None:
        raise NotImplementedError

    def remove_connection(self, connection_id: str) -> None:
        """Remove it and its sheet snapshot. Edits, notices, hidden ids and link members that
        point at its accounts are removed too (ids are prefixed per connection, see ids below)."""
        raise NotImplementedError

    # Manual accounts and readings ------------------------------------------------------------
    def manual(self) -> ManualData:
        raise NotImplementedError

    def save_manual(self, data: ManualData) -> None:
        raise NotImplementedError

    # Spreadsheet snapshots -------------------------------------------------------------------
    def snapshot(self, connection_id: str) -> ImportSnapshot | None:
        raise NotImplementedError

    def save_snapshot(self, snapshot: ImportSnapshot) -> None:
        raise NotImplementedError

    # Edits and notices -----------------------------------------------------------------------
    def edits(self) -> dict[str, dict[str, FieldEdit]]:
        raise NotImplementedError

    def set_edit(self, account_id: str, field: str, edit: FieldEdit) -> None:
        raise NotImplementedError

    def clear_edit(self, account_id: str, field: str) -> None:
        raise NotImplementedError

    def notices(
        self, account_id: str | None = None, *, include_dismissed: bool = False
    ) -> list[Notice]:
        raise NotImplementedError

    def add_notice(self, account_id: str, field: str, message: str) -> Notice:
        raise NotImplementedError

    def dismiss_notice(self, notice_id: str) -> None:
        raise NotImplementedError

    # Links and hidden accounts ---------------------------------------------------------------
    def links(self) -> LinkData:
        raise NotImplementedError

    def save_links(self, data: LinkData) -> None:
        raise NotImplementedError

    def hidden(self) -> list[str]:
        raise NotImplementedError

    def set_hidden(self, account_id: str, hidden: bool) -> None:
        raise NotImplementedError

    # Everything ------------------------------------------------------------------------------
    def clear_all(self) -> None:
        """Delete every file this store owns (used by "Clear local data")."""
        raise NotImplementedError


# Account ids are prefixed by connection so they never collide across sources:
#   HubSpot "hs-<company id>"; spreadsheets "sp<n>-<external id or name slug>" for "sheet-<n>";
#   manual "m-<8 hex>". The demo book ("acc-001") is never combined with these.
def connection_for(account_id: str) -> str | None:
    """The connection id that owns an account id ("hubspot", "sheet-2", "manual"), else None."""
    raise NotImplementedError
