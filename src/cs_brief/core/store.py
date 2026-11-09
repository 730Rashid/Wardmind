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

import functools
import json
import os
import re
import secrets
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, Field, TypeAdapter

from cs_brief.core.config import write_atomic
from cs_brief.core.feeds.models import FeedSettings
from cs_brief.core.models import Account, Signal
from cs_brief.core.secrets import delete_all_feed_secrets, secrets_dir

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

# File names inside data_dir(), and the folder of spreadsheet snapshots.
_CONNECTIONS_FILE = "connections.json"
_MANUAL_FILE = "manual.json"
_EDITS_FILE = "edits.json"
_NOTICES_FILE = "notices.json"
_LINKS_FILE = "links.json"
_HIDDEN_FILE = "hidden.json"
_SHEETS_DIR = "sheets"
FEED_DRAFTS_DIR = "feed-drafts"  # unfinished feed setups (feeds/setup.py)
# Owned by core/contacts.py and core/client_updates.py (they read and write them with
# config.write_atomic under store.root); listed here so clear_all removes them too.
CONTACTS_FILE = "contacts.json"
CLIENT_UPDATES_FILE = "client_updates.json"
# One lock for every read-modify-write of the data folder in this process. The feed scheduler
# runs on its own thread, so without it a sync that loaded a file before the CSM's change and
# saved after it would undo that change (an opt-out, a rejected update, a removed connection).
# Hold it around load -> change -> save, never around network calls. Re-entrant, so helpers
# that take it can call each other.
WRITE_LOCK = threading.RLock()
_P = ParamSpec("_P")
_R = TypeVar("_R")

_ALL_FILES = (
    _CONNECTIONS_FILE,
    _MANUAL_FILE,
    _EDITS_FILE,
    _NOTICES_FILE,
    _LINKS_FILE,
    _HIDDEN_FILE,
    CONTACTS_FILE,
    CLIENT_UPDATES_FILE,
)

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")  # connection ids, used in file names
_SHEET_ID = re.compile(r"sheet-([1-9][0-9]*)")
_SHEET_PREFIX = re.compile(r"sp([1-9][0-9]*)-")
_FEED_ID = re.compile(r"feed-([1-9][0-9]*)")
_FEED_PREFIX = re.compile(r"fd([1-9][0-9]*)-")
_DATE_FIELDS = frozenset({"start_date", "renewal_date"})
_HUBSPOT_NAME = "HubSpot"
_MANUAL_NAME = "Added by hand"


class ConnectionKind(StrEnum):
    HUBSPOT = "hubspot"
    SPREADSHEET = "spreadsheet"
    MANUAL = "manual"
    FEED = "feed"  # keeps itself up to date: a file, link, API, database or product (feeds/)


class ConnectionConfig(BaseModel):
    id: str  # "hubspot", "manual", "sheet-<n>" for spreadsheets, "feed-<n>" for feeds
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
    # Feed only: the provider, its settings, schedule and last sync. An ACCOUNTS feed also uses
    # mapping and date_order above, exactly like a spreadsheet; a SIGNALS feed uses mapping for
    # its key and signal columns.
    feed: FeedSettings | None = None


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


_CONNECTIONS_ADAPTER: TypeAdapter[Any] = TypeAdapter(list[ConnectionConfig])
_MANUAL_ADAPTER: TypeAdapter[Any] = TypeAdapter(ManualData)
_SNAPSHOT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ImportSnapshot)
_EDITS_ADAPTER: TypeAdapter[Any] = TypeAdapter(dict[str, dict[str, FieldEdit]])
_NOTICES_ADAPTER: TypeAdapter[Any] = TypeAdapter(list[Notice])
_LINKS_ADAPTER: TypeAdapter[Any] = TypeAdapter(LinkData)
_HIDDEN_ADAPTER: TypeAdapter[Any] = TypeAdapter(list[str])


def data_dir() -> Path:
    """CS_BRIEF_DATA_DIR if set, else secrets.secrets_dir() / "data". Not created here."""
    override = os.environ.get(ENV_DATA_DIR, "").strip()
    if override:
        return Path(override)
    return secrets_dir() / "data"


def _now() -> datetime:
    """Now as a naive UTC datetime, the same shape as the demo data."""
    return datetime.now(UTC).replace(tzinfo=None)


def _load(path: Path, adapter: TypeAdapter[Any], empty: Callable[[], Any]) -> Any:
    """The validated content of path, or empty() when it is missing, unreadable or wrong-shaped."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return adapter.validate_python(raw)
    except (OSError, ValueError, TypeError):  # ValidationError and JSONDecodeError are ValueErrors
        return empty()


def _save(path: Path, adapter: TypeAdapter[Any], value: Any) -> None:
    payload = adapter.dump_python(value, mode="json")
    write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _as_date(value: Any) -> Any:
    """An ISO string from the JSON file back to a date, for the two date fields. Anything else
    (including a string that isn't a date) is returned unchanged."""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _locked(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run a mutating LocalStore method holding WRITE_LOCK, so its load -> change -> save is one
    step that no feed sync or other thread can interleave with."""

    @functools.wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with WRITE_LOCK:
            return method(*args, **kwargs)

    return locked


class LocalStore:
    """Typed access to the files above. Every write is atomic; reads never raise."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else data_dir()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, name: str) -> Path:
        return self._root / name

    def _sheet_path(self, connection_id: str) -> Path | None:
        """sheets/<id>.json, or None for an id that isn't safe to use as a file name."""
        if not _SAFE_ID.fullmatch(connection_id):
            return None
        return self._root / _SHEETS_DIR / f"{connection_id}.json"

    # Connections -----------------------------------------------------------------------------
    def connections(self) -> list[ConnectionConfig]:
        return _load(self._path(_CONNECTIONS_FILE), _CONNECTIONS_ADAPTER, list)

    def get_connection(self, connection_id: str) -> ConnectionConfig | None:
        return next((c for c in self.connections() if c.id == connection_id), None)

    @_locked
    def add_connection(self, kind: ConnectionKind, name: str) -> ConnectionConfig:
        """HUBSPOT and MANUAL use their fixed ids and are returned as-is if they already exist.
        SPREADSHEET gets the next free "sheet-<n>" id, and FEED the next free "feed-<n>" id (its
        own numbering). The caller sets a FEED connection's `feed` with update_connection."""
        configs = self.connections()
        if kind in (ConnectionKind.HUBSPOT, ConnectionKind.MANUAL):
            fixed_id = (
                HUBSPOT_CONNECTION_ID if kind is ConnectionKind.HUBSPOT else MANUAL_CONNECTION_ID
            )
            for existing in configs:
                if existing.id == fixed_id:
                    return existing
            fixed_name = _HUBSPOT_NAME if kind is ConnectionKind.HUBSPOT else _MANUAL_NAME
            config = ConnectionConfig(id=fixed_id, kind=kind, name=fixed_name, created_at=_now())
        elif kind is ConnectionKind.FEED:
            number = self._next_number(configs, _FEED_ID, "feed-*.json")
            config = ConnectionConfig(
                id=f"feed-{number}",
                kind=ConnectionKind.FEED,
                name=name.strip() or f"Feed {number}",
                created_at=_now(),
            )
        else:
            # The highest number in use, plus one. Counting connections would reuse a number
            # after a removal from the middle (sheet-1, sheet-3 would give sheet-3 again).
            number = self._next_number(configs, _SHEET_ID, "sheet-*.json")
            config = ConnectionConfig(
                id=f"sheet-{number}",
                kind=ConnectionKind.SPREADSHEET,
                name=name.strip() or f"Spreadsheet {number}",
                created_at=_now(),
            )
        configs.append(config)
        _save(self._path(_CONNECTIONS_FILE), _CONNECTIONS_ADAPTER, configs)
        return config

    def _next_number(
        self, configs: list[ConnectionConfig], pattern: re.Pattern[str], glob: str
    ) -> int:
        """One more than the highest number in use by a connection id or a snapshot file of this
        kind (pattern matches "sheet-<n>" or "feed-<n>"; glob names its snapshot files)."""
        numbers = [int(m.group(1)) for c in configs if (m := pattern.fullmatch(c.id))]
        sheets = self._root / _SHEETS_DIR
        if sheets.is_dir():  # a snapshot left without its connection still holds its number
            for path in sheets.glob(glob):
                if m := pattern.fullmatch(path.stem):
                    numbers.append(int(m.group(1)))
        return max(numbers, default=0) + 1

    @_locked
    def update_connection(self, config: ConnectionConfig) -> None:
        """Replace the connection with the same id. An unknown id is ignored, so a stale copy
        can't bring back a removed connection."""
        configs = self.connections()
        if not any(c.id == config.id for c in configs):
            return
        configs = [config if c.id == config.id else c for c in configs]
        _save(self._path(_CONNECTIONS_FILE), _CONNECTIONS_ADAPTER, configs)

    @_locked
    def remove_connection(self, connection_id: str) -> None:
        """Remove it and its sheet snapshot. Edits, notices, hidden ids and link members that
        point at its accounts are removed too (ids are prefixed per connection, see ids below)."""

        def owned(account_id: str) -> bool:
            return connection_for(account_id) == connection_id

        configs = self.connections()
        remaining = [c for c in configs if c.id != connection_id]
        if len(remaining) != len(configs):
            _save(self._path(_CONNECTIONS_FILE), _CONNECTIONS_ADAPTER, remaining)

        sheet = self._sheet_path(connection_id)
        if sheet is not None:
            sheet.unlink(missing_ok=True)

        edits = self.edits()
        kept_edits = {acc: fields for acc, fields in edits.items() if not owned(acc)}
        if len(kept_edits) != len(edits):
            _save(self._path(_EDITS_FILE), _EDITS_ADAPTER, kept_edits)

        notices = _load(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, list)
        kept_notices = [n for n in notices if not owned(n.account_id)]
        if len(kept_notices) != len(notices):
            _save(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, kept_notices)

        hidden = self.hidden()
        kept_hidden = [acc for acc in hidden if not owned(acc)]
        if len(kept_hidden) != len(hidden):
            _save(self._path(_HIDDEN_FILE), _HIDDEN_ADAPTER, kept_hidden)

        links = self.links()
        groups: list[LinkGroup] = []
        for group in links.groups:
            members = [m for m in group.members if not owned(m)]
            if len(members) < 2:
                continue  # a group with fewer than 2 members is no longer a link
            primary = group.primary if group.primary in members else members[0]
            groups.append(LinkGroup(primary=primary, members=members))
        dismissed = [pair for pair in links.dismissed if not owned(pair[0]) and not owned(pair[1])]
        if groups != links.groups or dismissed != links.dismissed:
            _save(
                self._path(_LINKS_FILE),
                _LINKS_ADAPTER,
                LinkData(groups=groups, dismissed=dismissed),
            )

        manual = self.manual()
        if connection_id == MANUAL_CONNECTION_ID:
            if manual.accounts or manual.signals:
                self.save_manual(ManualData())
        else:
            kept_readings = [s for s in manual.signals if not owned(s.account_id)]
            if len(kept_readings) != len(manual.signals):
                self.save_manual(manual.model_copy(update={"signals": kept_readings}))

    # Manual accounts and readings ------------------------------------------------------------
    def manual(self) -> ManualData:
        return _load(self._path(_MANUAL_FILE), _MANUAL_ADAPTER, ManualData)

    @_locked
    def save_manual(self, data: ManualData) -> None:
        _save(self._path(_MANUAL_FILE), _MANUAL_ADAPTER, data)

    # Spreadsheet snapshots -------------------------------------------------------------------
    def snapshot(self, connection_id: str) -> ImportSnapshot | None:
        path = self._sheet_path(connection_id)
        if path is None:
            return None
        return _load(path, _SNAPSHOT_ADAPTER, lambda: None)

    @_locked
    def save_snapshot(self, snapshot: ImportSnapshot) -> None:
        path = self._sheet_path(snapshot.connection_id)
        if path is None:
            raise ValueError(f"{snapshot.connection_id!r} can't be used as a connection id")
        _save(path, _SNAPSHOT_ADAPTER, snapshot)

    # Edits and notices -----------------------------------------------------------------------
    def edits(self) -> dict[str, dict[str, FieldEdit]]:
        data: dict[str, dict[str, FieldEdit]] = _load(self._path(_EDITS_FILE), _EDITS_ADAPTER, dict)
        # JSON has no date type, so a date edit comes back as its ISO string. Restore the date.
        for fields in data.values():
            for field, edit in list(fields.items()):
                if field in _DATE_FIELDS:
                    fields[field] = edit.model_copy(
                        update={
                            "value": _as_date(edit.value),
                            "base_value": _as_date(edit.base_value),
                        }
                    )
        return data

    @_locked
    def set_edit(self, account_id: str, field: str, edit: FieldEdit) -> None:
        if field not in EDITABLE_FIELDS:
            raise ValueError(f"{field!r} can't be edited")
        data = self.edits()
        data.setdefault(account_id, {})[field] = edit
        _save(self._path(_EDITS_FILE), _EDITS_ADAPTER, data)

    @_locked
    def clear_edit(self, account_id: str, field: str) -> None:
        data = self.edits()
        fields = data.get(account_id)
        if fields is None or field not in fields:
            return
        del fields[field]
        if not fields:
            del data[account_id]
        _save(self._path(_EDITS_FILE), _EDITS_ADAPTER, data)

    def notices(
        self, account_id: str | None = None, *, include_dismissed: bool = False
    ) -> list[Notice]:
        found: list[Notice] = _load(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, list)
        return [
            notice
            for notice in found
            if (account_id is None or notice.account_id == account_id)
            and (include_dismissed or not notice.dismissed)
        ]

    @_locked
    def add_notice(self, account_id: str, field: str, message: str) -> Notice:
        found: list[Notice] = _load(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, list)
        taken = {notice.id for notice in found}
        notice_id = secrets.token_hex(4)  # the stdlib secrets module, not cs_brief.core.secrets
        while notice_id in taken:
            notice_id = secrets.token_hex(4)
        notice = Notice(
            id=notice_id,
            account_id=account_id,
            field=field,
            message=message,
            created_at=_now(),
        )
        found.append(notice)
        _save(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, found)
        return notice

    @_locked
    def dismiss_notice(self, notice_id: str) -> None:
        found: list[Notice] = _load(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, list)
        if not any(notice.id == notice_id for notice in found):
            return
        updated = [
            notice.model_copy(update={"dismissed": True}) if notice.id == notice_id else notice
            for notice in found
        ]
        _save(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, updated)

    @_locked
    def remove_notices(self, account_id: str) -> int:
        """Delete every notice for one account, dismissed ones included. Returns how many went."""
        found: list[Notice] = _load(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, list)
        kept = [notice for notice in found if notice.account_id != account_id]
        removed = len(found) - len(kept)
        if removed:
            _save(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, kept)
        return removed

    @_locked
    def move_notices(self, from_id: str, to_id: str) -> int:
        """Re-key every notice of from_id to to_id. Each keeps its id, its time and its dismissed
        flag. Returns how many moved."""
        found: list[Notice] = _load(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, list)
        moved = 0
        updated: list[Notice] = []
        for notice in found:
            if notice.account_id == from_id:
                updated.append(notice.model_copy(update={"account_id": to_id}))
                moved += 1
            else:
                updated.append(notice)
        if moved:
            _save(self._path(_NOTICES_FILE), _NOTICES_ADAPTER, updated)
        return moved

    # Links and hidden accounts ---------------------------------------------------------------
    def links(self) -> LinkData:
        return _load(self._path(_LINKS_FILE), _LINKS_ADAPTER, LinkData)

    @_locked
    def save_links(self, data: LinkData) -> None:
        _save(self._path(_LINKS_FILE), _LINKS_ADAPTER, data)

    def hidden(self) -> list[str]:
        return _load(self._path(_HIDDEN_FILE), _HIDDEN_ADAPTER, list)

    @_locked
    def set_hidden(self, account_id: str, hidden: bool) -> None:
        ids = self.hidden()
        if hidden and account_id not in ids:
            ids.append(account_id)
        elif not hidden and account_id in ids:
            ids = [acc for acc in ids if acc != account_id]
        else:
            return  # already in the wanted state
        _save(self._path(_HIDDEN_FILE), _HIDDEN_ADAPTER, ids)

    # Everything ------------------------------------------------------------------------------
    @_locked
    def clear_all(self) -> None:
        """Delete every file this store owns, the feed drafts and every feed's saved key (used by
        "Clear local data"). HubSpot's token and the OpenRouter key are not touched."""
        for name in _ALL_FILES:
            self._path(name).unlink(missing_ok=True)
            self._path(f".{name}.tmp").unlink(missing_ok=True)
        shutil.rmtree(self._root / _SHEETS_DIR, ignore_errors=True)
        shutil.rmtree(self._root / FEED_DRAFTS_DIR, ignore_errors=True)
        delete_all_feed_secrets()


# Account ids are prefixed by connection so they never collide across sources:
#   HubSpot "hs-<company id>"; spreadsheets "sp<n>-<external id or name slug>" for "sheet-<n>";
#   feeds "fd<n>-<...>" for "feed-<n>" (an ACCOUNTS feed's accounts; a SIGNALS feed owns none);
#   manual "m-<8 hex>". The demo book ("acc-001") is never combined with these.
def connection_for(account_id: str) -> str | None:
    """The connection id that owns an account id ("hubspot", "sheet-2", "feed-3", "manual"), else
    None."""
    if account_id.startswith("hs-"):
        return HUBSPOT_CONNECTION_ID
    if account_id.startswith("m-"):
        return MANUAL_CONNECTION_ID
    match = _SHEET_PREFIX.match(account_id)
    if match:
        return f"sheet-{match.group(1)}"
    match = _FEED_PREFIX.match(account_id)
    if match:
        return f"feed-{match.group(1)}"
    return None
