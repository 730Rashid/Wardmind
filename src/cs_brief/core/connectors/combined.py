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

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from cs_brief.core.connectors.base import Connector
from cs_brief.core.connectors.errors import ConnectorError
from cs_brief.core.matching import DuplicateSuggestion, suggest_duplicates
from cs_brief.core.models import Account, Milestones, Signal
from cs_brief.core.store import (
    EDITABLE_FIELDS,
    FIELD_LABELS,
    ConnectionConfig,
    ConnectionKind,
    EditValue,
    FieldEdit,
    LinkData,
    LocalStore,
)


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


# Account fields that a linked account fills in from its other members when the primary lacks them.
_MERGE_ATTRS: tuple[str, ...] = ("domain", "tier", "owner", "arr", "milestones")
_DATE_FIELDS = frozenset({"start_date", "renewal_date"})
_MILESTONE_ATTRS: dict[str, str] = {
    "milestones_total": "total",
    "milestones_done": "done",
    "milestones_overdue": "overdue",
}
_UNKNOWN_TIER = "Unknown"
_UNASSIGNED = "Unassigned"
_NO_VALUE = "nothing"  # how a blank value reads in a notice
_MILESTONES_DROPPED = (
    "Your onboarding milestone edits no longer fit {name}'s numbers, so they were removed."
)
_CHANGED = "{connection} changed {label} to {new}, replacing your edit of {old}."


def _read_field(account: Account, field_name: str) -> EditValue:
    """The value of one EDITABLE_FIELDS entry on an account."""
    if field_name in _MILESTONE_ATTRS:
        return getattr(account.milestones, _MILESTONE_ATTRS[field_name])
    return getattr(account, field_name)


def _as_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _key(value: Any) -> str:
    """The text two values are compared by: ISO dates, whole numbers without ".0" (so 120000.0
    and "120000" match), and None the same as blank text. Floats keep every digit, unlike :g,
    which would make 1234567 and 1234568 look the same."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    return str(value)


def _display(field_name: str, value: Any) -> str:
    """How a value reads in a notice: money as $140,000, dates as 1 December 2026."""
    if value is None or value == "":
        return _NO_VALUE
    if field_name in _DATE_FIELDS:
        day = _as_day(value)
        if day is not None:
            return f"{day.day} {day:%B} {day.year}"
    elif field_name == "arr":
        amount = _number(value)
        if amount is not None:
            return f"${amount:,.0f}"
    return str(value) if isinstance(value, str) else _key(value)


def _coerce(field_name: str, value: Any) -> Any:
    """The edited value in the type its Account attribute needs. Raises ValueError if it can't."""
    if field_name in _DATE_FIELDS:
        day = _as_day(value)
        if day is None:
            raise ValueError(field_name)
        return day
    if field_name == "arr":
        amount = _number(value)
        if amount is None:
            raise ValueError(field_name)
        return amount
    if field_name in _MILESTONE_ATTRS:
        number = _number(value)
        if number is None or not number.is_integer():
            raise ValueError(field_name)
        return int(number)
    if field_name == "domain":
        if value is None:
            return None
        return str(value).strip() or None
    if value is None:
        raise ValueError(field_name)
    return str(value)


def _is_missing(account: Account, attr: str) -> bool:
    """The "missing" test for filling a linked account's field from another member."""
    if attr == "domain":
        return not account.domain
    if attr == "tier":
        return account.tier == _UNKNOWN_TIER
    if attr == "owner":
        return account.owner == _UNASSIGNED
    if attr == "arr":
        return account.arr == 0
    return account.milestones.total == 0


def _merge(primary: Account, others: list[Account]) -> Account:
    """The primary, with each missing field taken from the first other member that has it."""
    update: dict[str, Any] = {}
    for attr in _MERGE_ATTRS:
        if not _is_missing(primary, attr):
            continue
        for other in others:
            if not _is_missing(other, attr):
                update[attr] = getattr(other, attr)
                break
    return primary.model_copy(update=update) if update else primary


def _milestones(source: Milestones, values: dict[str, Any]) -> Milestones:
    """The source milestones with the edited numbers put in. Raises ValidationError if they
    don't add up."""
    return Milestones(
        done=values.get("milestones_done", source.done),
        total=values.get("milestones_total", source.total),
        overdue=values.get("milestones_overdue", source.overdue),
    )


def _changed_message(field_name: str, edit: FieldEdit, current: Any, connection: str) -> str:
    return _CHANGED.format(
        connection=connection,
        label=FIELD_LABELS[field_name],
        new=_display(field_name, current),
        old=_display(field_name, edit.value),
    )


@dataclass
class _Entry:
    """One account in the book: its merged source values, its edited result and its origin."""

    source: Account  # merged across linked members, before edits
    account: Account  # after edits; what the book serves
    member_ids: list[str]  # present members, primary first
    connection_id: str  # the connection the primary member came from
    connection_name: str
    connections: list[str]  # display names, in member order, no repeats
    editable_in_place: bool
    source_values: dict[str, EditValue]
    applied: dict[str, FieldEdit]


@dataclass
class _Book:
    entries: dict[str, _Entry]  # by account id, in book order
    signals: list[Signal]
    problems: list[ConnectionProblem]
    links: LinkData  # as read at load time


class CombinedConnector:
    name = "combined"

    def __init__(
        self,
        members: list[tuple[ConnectionConfig, Connector]],
        store: LocalStore,
        *,
        today: date | None = None,
    ) -> None:
        self._members = list(members)
        self._store = store
        self._today = today if today is not None else date.today()
        self._book: _Book | None = None

    def list_accounts(self) -> list[Account]:
        return [entry.account for entry in self._loaded().entries.values()]

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        signals = self._loaded().signals
        if account_id is None:
            return list(signals)
        return [signal for signal in signals if signal.account_id == account_id]

    @property
    def problems(self) -> list[ConnectionProblem]:
        """Members that failed on the last load, in member order."""
        return list(self._loaded().problems)

    def provenance(self, account_id: str) -> Provenance | None:
        entry = self._loaded().entries.get(account_id)
        if entry is None:
            return None
        return Provenance(
            account_id=account_id,
            member_ids=list(entry.member_ids),
            connections=list(entry.connections),
            editable_in_place=entry.editable_in_place,
            edits=dict(entry.applied),
            source_values=dict(entry.source_values),
        )

    def duplicate_suggestions(self) -> list[DuplicateSuggestion]:
        """matching.suggest_duplicates over the loaded accounts (after links, before edits)."""
        book = self._loaded()
        return suggest_duplicates(
            [entry.source for entry in book.entries.values()],
            {acc_id: entry.connection_id for acc_id, entry in book.entries.items()},
            book.links,
        )

    def refresh(self) -> None:
        self._book = None
        for _config, connector in self._members:
            refresh = getattr(connector, "refresh", None)
            if callable(refresh):
                refresh()

    # Loading -------------------------------------------------------------------------------
    def _loaded(self) -> _Book:
        if self._book is None:
            self._book = self._build()
        return self._book

    def _build(self) -> _Book:
        problems: list[ConnectionProblem] = []
        accounts: dict[str, Account] = {}
        origin: dict[str, ConnectionConfig] = {}
        signals: list[Signal] = []

        # 1. Members. A failing member is recorded and skipped; anything else propagates.
        for config, connector in self._members:
            try:
                member_accounts = connector.list_accounts()
                member_signals = connector.list_signals()
            except ConnectorError as exc:
                problems.append(
                    ConnectionProblem(
                        connection_id=config.id, name=config.name, message=exc.message
                    )
                )
                continue
            for account in member_accounts:
                if account.id not in accounts:
                    accounts[account.id] = account
                    origin[account.id] = config
            signals.extend(member_signals)

        # 2. Hidden accounts.
        hidden = set(self._store.hidden())
        accounts = {acc_id: acc for acc_id, acc in accounts.items() if acc_id not in hidden}
        origin = {acc_id: cfg for acc_id, cfg in origin.items() if acc_id in accounts}

        # 3. Links. Each group's present members become one account with the first of them
        # (the primary if it is present) as its id.
        links = self._store.links()
        merged_into: dict[str, str] = {}  # member id -> the id of the account it became
        members_of: dict[str, list[str]] = {}  # merged id -> present member ids, primary first
        for group in links.groups:
            ordered = [group.primary, *(m for m in group.members if m != group.primary)]
            present = [m for m in dict.fromkeys(ordered) if m in accounts and m not in merged_into]
            if not present:
                continue
            keep = present[0]
            members_of[keep] = present
            for member_id in present:
                merged_into[member_id] = keep

        entries: dict[str, _Entry] = {}
        for acc_id, account in accounts.items():
            if merged_into.get(acc_id, acc_id) != acc_id:
                continue  # folded into the account that keeps its id
            member_ids = members_of.get(acc_id, [acc_id])
            source = _merge(account, [accounts[m] for m in member_ids[1:]])
            configs = [origin[m] for m in member_ids]
            connection_names: dict[str, str] = {}
            for config in configs:
                connection_names.setdefault(config.id, config.name)
            primary_config = origin[acc_id]
            entries[acc_id] = _Entry(
                source=source,
                account=source,
                member_ids=member_ids,
                connection_id=primary_config.id,
                connection_name=primary_config.name,
                connections=list(connection_names.values()),
                editable_in_place=all(c.kind is ConnectionKind.MANUAL for c in configs),
                source_values={name: _read_field(source, name) for name in EDITABLE_FIELDS},
                applied={},
            )

        # Member readings, re-keyed to the account they now belong to. Readings for accounts
        # that aren't in the book (hidden or absent) are dropped.
        readings: list[Signal] = []
        for signal in signals:
            target = merged_into.get(signal.account_id, signal.account_id)
            if target not in entries:
                continue
            if target != signal.account_id:
                signal = signal.model_copy(update={"account_id": target})
            readings.append(signal)

        # 4. Hand-entered readings. A reading the manual connector already supplied is skipped.
        seen = {_signal_key(signal) for signal in readings}
        for signal in self._store.manual().signals:
            target = merged_into.get(signal.account_id, signal.account_id)
            if target not in entries:
                continue
            if target != signal.account_id:
                signal = signal.model_copy(update={"account_id": target})
            if _signal_key(signal) in seen:
                continue
            readings.append(signal)

        # 5. Edits, with the "until the source changes" rule.
        all_edits = self._store.edits()
        for acc_id, entry in entries.items():
            fields = all_edits.get(acc_id)
            if fields:
                self._apply_edits(acc_id, entry, fields)

        return _Book(entries=entries, signals=readings, problems=problems, links=links)

    def _apply_edits(self, acc_id: str, entry: _Entry, fields: dict[str, FieldEdit]) -> None:
        store = self._store
        applied: dict[str, FieldEdit] = {}
        values: dict[str, Any] = {}
        for field_name in EDITABLE_FIELDS:
            edit = fields.get(field_name)
            if edit is None:
                continue
            current = entry.source_values[field_name]
            if _key(edit.base_value) != _key(current):
                store.clear_edit(acc_id, field_name)
                store.add_notice(
                    acc_id,
                    field_name,
                    _changed_message(field_name, edit, current, entry.connection_name),
                )
                continue
            try:
                values[field_name] = _coerce(field_name, edit.value)
            except ValueError:
                continue  # not a usable value; leave the source showing
            applied[field_name] = edit

        updates: dict[str, Any] = {
            name: value for name, value in values.items() if name not in _MILESTONE_ATTRS
        }
        milestone_edits = [name for name in _MILESTONE_ATTRS if name in applied]
        if milestone_edits:
            try:
                updates["milestones"] = _milestones(entry.source.milestones, values)
            except ValidationError:
                for name in milestone_edits:
                    store.clear_edit(acc_id, name)
                    applied.pop(name)
                store.add_notice(
                    acc_id,
                    milestone_edits[0],
                    _MILESTONES_DROPPED.format(name=entry.connection_name),
                )

        entry.account = entry.source.model_copy(update=updates) if updates else entry.source
        entry.applied = applied


def _signal_key(signal: Signal) -> tuple[str, Any, datetime, float, str]:
    return (signal.account_id, signal.kind, signal.observed_at, signal.value, signal.source)
