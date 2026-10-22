"""The steps behind the web import flow: drafts, mapping, preview and commit.

A draft is an upload, or pasted cells, held in the local data folder until the CSM commits it or
it expires after 24 hours. The routes stay thin: they call these functions and render the results.
Every rule that can refuse an import lives here, so the page and the tests see the same answer.

Draft ids are 16 random hex characters from the stdlib secrets module. Each id is checked against
DRAFT_ID before any path is built from it. Nothing here touches the network.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel

from cs_brief.core import importing
from cs_brief.core.config import write_atomic
from cs_brief.core.importing import IMPORT_FIELDS, DateOrder, FieldType, ImportIssue, Table
from cs_brief.core.store import (
    ConnectionConfig,
    ConnectionKind,
    ImportRecord,
    ImportSnapshot,
    LocalStore,
)

DRAFT_TTL = timedelta(hours=24)
DRAFT_ID = re.compile(r"[0-9a-f]{16}")
NAME_LIMIT = 60  # characters in a connection name
NEW_TARGET = "new"  # the import target that makes a new spreadsheet connection
BACK_SETUP = "setup"  # where Cancel goes: the Setup page
BACK_CONNECTIONS = "connections"  # or the Connections page
PASTED_SOURCE = "Pasted cells"

_DRAFTS_DIR = "drafts"
_SHEETS_DIR = "sheets"  # the store's folder of spreadsheet snapshots
_SHEET_NUMBER = re.compile(r"sheet-([1-9][0-9]*)")
_NOTHING_READY = (
    "No accounts are ready to import. Fix the rows with problems, or match other columns."
)


class Draft(BaseModel):
    """An upload waiting for the CSM to match its columns and commit it.

    `mapping`, `date_order` and `back` are the CSM's last choices on the draft. They are saved on
    each preview, so a refresh keeps them and the problems download matches the page.
    """

    id: str
    created_at: datetime  # naive UTC
    source_name: str  # file name or "Pasted cells"
    file_bytes_b64: str | None = None  # original upload, for re-reading another Excel sheet
    pasted_text: str | None = None
    sheet: str | None = None
    table: Table
    target: str  # "new" or an existing "sheet-<n>" connection id
    new_name: str = ""  # name for a new connection
    back: str = BACK_SETUP
    mapping: dict[str, str] | None = None  # import field key -> column header
    date_order: str | None = None  # "dmy", "mdy" or "ymd"


class Preview(BaseModel):
    """What a commit would do right now, for the "What will happen" section."""

    ready_rows: int
    skipped_rows: int
    issues: list[ImportIssue]
    examples: dict[str, str]  # import field key -> the first data row's cell, when mapped
    example_row: int  # the spreadsheet row number of that first data row
    missing_required: list[str]  # labels of required fields with no column
    removed_ids: list[str]  # re-import only: accounts from last time that aren't in this file

    @property
    def blocker(self) -> str:
        """Why the import can't run yet, or "" when it can."""
        if self.missing_required:
            return _match_message(self.missing_required)
        if self.ready_rows == 0:
            return _NOTHING_READY
        return ""


class CommitResult(BaseModel):
    connection: ConnectionConfig
    ready_rows: int
    skipped_rows: int
    issue_count: int
    removed: int  # accounts from the last import that aren't in this one


# Drafts ----------------------------------------------------------------------------------------


def new_draft_id() -> str:
    """A fresh draft id: 16 random hex characters."""
    return secrets.token_hex(8)


def is_draft_id(value: str) -> bool:
    """True for a well-formed draft id. Anything else is refused before it reaches a path."""
    return isinstance(value, str) and DRAFT_ID.fullmatch(value) is not None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _checked_path(store: LocalStore, draft_id: str) -> Path | None:
    """The file for a draft id, or None when the id isn't well formed."""
    if not is_draft_id(draft_id):
        return None
    return store.root / _DRAFTS_DIR / f"{draft_id}.json"


def _read(path: Path) -> Draft | None:
    try:
        return Draft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(store: LocalStore, draft: Draft) -> None:
    path = _checked_path(store, draft.id)
    if path is None:  # a draft is only ever built with a valid id
        raise ValueError("That draft id isn't valid.")
    write_atomic(path, draft.model_dump_json())


def _fresh_id(store: LocalStore) -> str:
    while True:
        candidate = new_draft_id()
        path = _checked_path(store, candidate)
        if path is not None and not path.exists():
            return candidate


def _expired(draft: Draft, moment: datetime) -> bool:
    return moment - draft.created_at > DRAFT_TTL


def save_draft(
    store: LocalStore,
    *,
    table: Table,
    file_bytes: bytes | None = None,
    pasted_text: str | None = None,
    target: str = NEW_TARGET,
    new_name: str = "",
    back: str = BACK_SETUP,
    now: datetime | None = None,
) -> Draft:
    """Store an upload or pasted cells as a draft and return it. Expired drafts are purged first.

    `target` must be "new" or an existing spreadsheet connection. Raises ValueError otherwise.
    """
    moment = _as_naive_utc(now) if now is not None else _now()
    purge_old_drafts(store, now=moment)
    _check_target(store, target)
    draft = Draft(
        id=_fresh_id(store),
        created_at=moment,
        source_name=table.source_name,
        file_bytes_b64=None if file_bytes is None else base64.b64encode(file_bytes).decode("ascii"),
        pasted_text=pasted_text,
        sheet=table.sheet,
        table=table,
        target=target,
        new_name=new_name.strip(),
        back=back if back in (BACK_SETUP, BACK_CONNECTIONS) else BACK_SETUP,
    )
    _write(store, draft)
    return draft


def load_draft(store: LocalStore, draft_id: str, *, now: datetime | None = None) -> Draft | None:
    """The draft with this id, or None. A malformed id, a missing or unreadable file, or a draft
    older than DRAFT_TTL all read as None; an expired draft is deleted as well."""
    path = _checked_path(store, draft_id)
    if path is None or not path.is_file():
        return None
    draft = _read(path)
    if draft is None or draft.id != draft_id:
        return None
    moment = _as_naive_utc(now) if now is not None else _now()
    if _expired(draft, moment):
        path.unlink(missing_ok=True)
        return None
    return draft


def delete_draft(store: LocalStore, draft_id: str) -> None:
    """Remove a draft's file. A malformed or unknown id does nothing."""
    path = _checked_path(store, draft_id)
    if path is not None:
        path.unlink(missing_ok=True)


def purge_old_drafts(store: LocalStore, *, now: datetime | None = None) -> int:
    """Delete every draft older than DRAFT_TTL, and any unreadable draft file. Returns how many."""
    folder = store.root / _DRAFTS_DIR
    if not folder.is_dir():
        return 0
    moment = _as_naive_utc(now) if now is not None else _now()
    removed = 0
    for path in folder.glob("*.json"):
        if not is_draft_id(path.stem):
            continue
        draft = _read(path)
        if draft is None or _expired(draft, moment):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def remember_choices(
    store: LocalStore,
    draft: Draft,
    *,
    mapping: Mapping[str, str],
    date_order: DateOrder | None,
    back: str | None = None,
) -> Draft:
    """Save the CSM's mapping and date order on the draft, so a refresh keeps them."""
    update: dict[str, object] = {
        "mapping": clean_mapping(draft.table, mapping),
        "date_order": date_order.value if date_order is not None else draft.date_order,
    }
    if back in (BACK_SETUP, BACK_CONNECTIONS):
        update["back"] = back
    updated = draft.model_copy(update=update)
    _write(store, updated)
    return updated


def reread_sheet(store: LocalStore, draft: Draft, sheet: str) -> Draft:
    """Read another sheet of the same Excel file from the stored bytes, and keep the draft.

    The mapping is forgotten, because another sheet has other headers. Raises ImportError_ and
    changes nothing when the file has no such sheet, or isn't a spreadsheet at all.
    """
    if draft.file_bytes_b64 is None:
        raise importing.ImportError_("Only an Excel file has sheets to choose from.")
    data = base64.b64decode(draft.file_bytes_b64)
    table = importing.read_upload(data, draft.source_name, sheet)
    updated = draft.model_copy(
        update={"table": table, "sheet": table.sheet, "mapping": None, "date_order": None}
    )
    _write(store, updated)
    return updated


def original_bytes(draft: Draft) -> bytes | None:
    """The original upload's bytes, or None for pasted cells."""
    if draft.file_bytes_b64 is None:
        return None
    return base64.b64decode(draft.file_bytes_b64)


# Connections the import can target -----------------------------------------------------------


def _sheet_config(store: LocalStore, target: str) -> ConnectionConfig | None:
    """The spreadsheet connection an import targets, or None for a new one or a missing one."""
    if target == NEW_TARGET:
        return None
    config = store.get_connection(target)
    if config is None or config.kind is not ConnectionKind.SPREADSHEET:
        return None
    return config


def _check_target(store: LocalStore, target: str) -> None:
    if target == NEW_TARGET:
        return
    if _sheet_config(store, target) is None:
        raise ValueError("That connection can't take a spreadsheet import. Start the import again.")


def next_sheet_id(store: LocalStore) -> str:
    """The id the store gives the next spreadsheet connection, worked out without creating it.

    Mirrors LocalStore.add_connection: one more than the highest number in use by a connection or
    by a snapshot file, so a removed number is never reused.
    """
    numbers = [int(m.group(1)) for c in store.connections() if (m := _SHEET_NUMBER.fullmatch(c.id))]
    sheets = store.root / _SHEETS_DIR
    if sheets.is_dir():  # a snapshot left without its connection still holds its number
        for path in sheets.glob("sheet-*.json"):
            if m := _SHEET_NUMBER.fullmatch(path.stem):
                numbers.append(int(m.group(1)))
    return f"sheet-{max(numbers, default=0) + 1}"


def connection_id_for(draft: Draft, store: LocalStore) -> str:
    """The connection id this draft's accounts get: the target, or the id a new one would get."""
    if draft.target == NEW_TARGET:
        return next_sheet_id(store)
    return draft.target


def validate_name(text: str) -> str:
    """A connection name, stripped. Raises ValueError with a plain message when it's blank or too
    long. Used for new connections and for renames."""
    name = text.strip()
    if not name:
        raise ValueError("Give it a name.")
    if len(name) > NAME_LIMIT:
        raise ValueError(f"A name can be at most {NAME_LIMIT} characters.")
    return name


def default_name(source_name: str) -> str:
    """The name a new connection gets when the CSM leaves it blank: the file name without its
    extension, or "Pasted cells"."""
    if source_name == PASTED_SOURCE:
        return PASTED_SOURCE
    return PureWindowsPath(source_name).stem or source_name


# Mapping and dates ---------------------------------------------------------------------------


def clean_mapping(table: Table, submitted: Mapping[str, str]) -> dict[str, str]:
    """Keep the choices that are real: a known field, mapped to a header this table has. Anything
    else (a stale header, a blank choice) is "Not in my file"."""
    mapping: dict[str, str] = {}
    for spec in IMPORT_FIELDS:
        header = submitted.get(spec.key, "")
        if header and header in table.headers:
            mapping[spec.key] = header
    return mapping


def suggested_mapping(draft: Draft, store: LocalStore) -> dict[str, str]:
    """The mapping the page starts with.

    Re-importing into a spreadsheet connection starts from its remembered mapping, keeping only
    headers that are still in the file, and tops up unmapped fields from auto_map. Otherwise it is
    auto_map alone. A header is used by one field at a time, as auto_map does.
    """
    automatic = importing.auto_map(draft.table.headers)
    config = _sheet_config(store, draft.target)
    if config is None or not config.mapping:
        return automatic
    chosen = clean_mapping(draft.table, config.mapping)
    taken = set(chosen.values())
    for key, header in automatic.items():
        if key not in chosen and header not in taken:
            chosen[key] = header
            taken.add(header)
    return {spec.key: chosen[spec.key] for spec in IMPORT_FIELDS if spec.key in chosen}


def date_columns(draft: Draft, mapping: Mapping[str, str]) -> list[str]:
    """The headers mapped to a date field, in field order, each once."""
    keys = [spec.key for spec in IMPORT_FIELDS if spec.type is FieldType.DATE]
    return list(dict.fromkeys(mapping[key] for key in keys if key in mapping))


def suggested_date_order(draft: Draft, mapping: Mapping[str, str]) -> DateOrder | None:
    """The date order the mapped date columns imply, or None when nothing decides it. None means
    the page asks the CSM to choose."""
    values: list[str] = []
    for header in date_columns(draft, mapping):
        index = draft.table.headers.index(header)
        values.extend(row[index] for row in draft.table.rows)
    return importing.detect_date_order(values)


def date_order_choice(draft: Draft, store: LocalStore) -> DateOrder:
    """The radio that starts checked: the last choice on this draft, else the connection's
    remembered order, else day first."""
    config = _sheet_config(store, draft.target)
    remembered = config.date_order if config is not None else None
    for value in (draft.date_order, remembered):
        if value in ("dmy", "mdy", "ymd"):
            return DateOrder(value)
    return DateOrder.DMY


def resolve_date_order(draft: Draft, mapping: Mapping[str, str], chosen: str | None) -> DateOrder:
    """The date order an import uses. A date order the file implies wins; otherwise the CSM's
    choice, and day first when there is none or it isn't a date order."""
    implied = suggested_date_order(draft, clean_mapping(draft.table, mapping))
    if implied is not None:
        return implied
    try:
        return DateOrder(chosen) if chosen else DateOrder.DMY
    except ValueError:
        return DateOrder.DMY


# Preview and commit --------------------------------------------------------------------------


def _examples(table: Table, mapping: Mapping[str, str]) -> dict[str, str]:
    if not table.rows:
        return {}
    first = table.rows[0]
    return {key: first[table.headers.index(header)] for key, header in mapping.items()}


def _first_row_number(table: Table) -> int:
    return table.row_numbers[0] if table.row_numbers else 2


def _match_message(labels: list[str]) -> str:
    """'Match Account name to import.', or 'Match ARR and Renewal date to import.'"""
    if len(labels) == 1:
        return f"Match {labels[0]} to import."
    return f"Match {', '.join(labels[:-1])} and {labels[-1]} to import."


def _build(
    draft: Draft,
    mapping: Mapping[str, str],
    date_order: DateOrder,
    connection_id: str,
    *,
    now: datetime,
    today: date,
) -> importing.ImportResult:
    return importing.build_import(
        draft.table,
        clean_mapping(draft.table, mapping),
        connection_id=connection_id,
        date_order=date_order,
        imported_at=now,
        today=today,
    )


def preview(
    draft: Draft,
    mapping: Mapping[str, str],
    date_order: DateOrder,
    store: LocalStore,
    *,
    now: datetime,
    today: date,
) -> Preview:
    """What a commit would do with this mapping. Nothing is created or saved.

    A new target is previewed under the id a new connection would get, without creating it.
    On a re-import, removed_ids are the accounts from the last import that this file doesn't have.
    """
    cleaned = clean_mapping(draft.table, mapping)
    result = _build(
        draft, cleaned, date_order, connection_id_for(draft, store), now=now, today=today
    )
    snapshot = None if draft.target == NEW_TARGET else store.snapshot(draft.target)
    ready = {account.id for account in result.accounts}
    removed = [] if snapshot is None else [a.id for a in snapshot.accounts if a.id not in ready]
    missing = [spec.label for spec in IMPORT_FIELDS if spec.required and spec.key not in cleaned]
    return Preview(
        ready_rows=result.ready_rows,
        skipped_rows=result.skipped_rows,
        issues=result.issues,
        examples=_examples(draft.table, cleaned),
        example_row=_first_row_number(draft.table),
        missing_required=missing,
        removed_ids=removed,
    )


def commit(
    draft: Draft,
    mapping: Mapping[str, str],
    date_order: DateOrder,
    store: LocalStore,
    *,
    now: datetime,
    today: date,
) -> CommitResult:
    """Import the draft into its connection, and delete the draft.

    Refuses (ValueError, with a plain message, and nothing changed) when a required field has no
    column or no account is ready. A new target creates its connection first. The snapshot's
    accounts are replaced; its readings keep their history and only changed values are added; the
    connection remembers the mapping and date order, and is enabled.
    """
    _check_target(store, draft.target)
    cleaned = clean_mapping(draft.table, mapping)
    checked = preview(draft, cleaned, date_order, store, now=now, today=today)
    if checked.blocker:
        raise ValueError(checked.blocker)

    if draft.target == NEW_TARGET:
        name = validate_name(draft.new_name or default_name(draft.source_name))
        config = store.add_connection(ConnectionKind.SPREADSHEET, name)
    else:
        config = store.get_connection(draft.target)
        if config is None:
            raise ValueError("That connection was removed. Start the import again.")

    result = _build(draft, cleaned, date_order, config.id, now=now, today=today)
    stamp = _as_naive_utc(now)
    previous = store.snapshot(config.id)
    earlier = [] if previous is None else previous.accounts
    kept = {account.id for account in result.accounts}
    removed_ids = [account.id for account in earlier if account.id not in kept]
    signals = importing.merge_into_snapshot_signals(
        [] if previous is None else previous.signals, result.signals, kept
    )
    record = ImportRecord(
        imported_at=stamp,
        file_name=draft.source_name,
        accounts=result.ready_rows,
        skipped_rows=result.skipped_rows,
    )
    history = [*([] if previous is None else previous.history), record]
    store.save_snapshot(
        ImportSnapshot(
            connection_id=config.id,
            accounts=result.accounts,
            signals=signals,
            history=history,
        )
    )
    updated = config.model_copy(
        update={
            "mapping": cleaned,
            "date_order": date_order.value,
            "last_imported_at": stamp,
            "last_file_name": draft.source_name,
            "enabled": True,
        }
    )
    store.update_connection(updated)
    delete_draft(store, draft.id)
    return CommitResult(
        connection=updated,
        ready_rows=result.ready_rows,
        skipped_rows=result.skipped_rows,
        issue_count=len(result.issues),
        removed=len(removed_ids),
    )
