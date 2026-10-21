"""Everything a CSM can change by hand: add or delete their own accounts, edit any account,
enter a signal reading, hide an account, and link or dismiss duplicates.

The web layer calls these and nothing else for writes, so the rules live in one place.
Every function raises EditError (a ValueError whose str() is plain English) for bad input and
changes nothing in that case.

CONTRACT FILE: signatures and docstrings are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from cs_brief.core import importing
from cs_brief.core.connectors.combined import CombinedConnector, Provenance, _key
from cs_brief.core.matching import normalize_domain
from cs_brief.core.models import Account, Milestones, Signal, SignalKind
from cs_brief.core.store import (
    EDITABLE_FIELDS,
    FIELD_LABELS,
    ConnectionKind,
    EditValue,
    FieldEdit,
    LinkData,
    LinkGroup,
    LocalStore,
    connection_for,
)

_MANUAL_PREFIX = "m-"  # hand-added account ids; see store.connection_for
_MANUAL_NAME = "Added by hand"
_UNKNOWN_TIER = "Unknown"
_UNASSIGNED = "Unassigned"
_DATE_FIELDS = frozenset({"start_date", "renewal_date"})
_MILESTONE_FIELDS = ("milestones_total", "milestones_done", "milestones_overdue")
_REQUIRED_ON_CREATE = ("name", "arr", "renewal_date")
_QUOTE_LIMIT = 40  # quoted values in messages are cut to this many characters
_NOT_IN_BOOK = "That account isn't in your book."
_CANT_EDIT = "That field can't be edited."
_MILESTONES_TOO_MANY = "Milestones done and overdue can't add up to more than the total."
_LINK_TWO = "Choose at least two accounts to link."
_LINK_PRIMARY = "The account to keep has to be one of the accounts you're linking."
_LINK_CONNECTIONS = "Only accounts from different connections can be linked."
_UNLINKED = "That account isn't linked to another."
_DELETE_MANUAL_ONLY = "Only accounts you added by hand can be deleted. Hide it instead."
# The labels the import mapping uses for the same signals.
_READING_LABELS: dict[SignalKind, str] = {
    SignalKind.USAGE_TREND_30D: "Usage trend",
    SignalKind.DAYS_SINCE_LAST_LOGIN: "Days since last login",
    SignalKind.OPEN_TICKETS_SEV1: "Open severity-1 tickets",
    SignalKind.NPS_LATEST: "NPS",
    SignalKind.CHAMPION_LEFT: "Champion left",
    SignalKind.SEAT_UTILISATION: "Seats in use",
    SignalKind.BILLING_OVERDUE_DAYS: "Days invoice overdue",
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: "Milestones overdue",
}
_COUNT_KINDS = frozenset(
    {
        SignalKind.DAYS_SINCE_LAST_LOGIN,
        SignalKind.OPEN_TICKETS_SEV1,
        SignalKind.BILLING_OVERDUE_DAYS,
        SignalKind.ONBOARDING_OVERDUE_MILESTONES,
    }
)


class EditError(ValueError):
    """Bad input for an edit. str() is shown to the CSM next to the field.

    `field` is the EDITABLE_FIELDS entry the message belongs to, or None. `errors` holds every
    message an edit found, by field, so the form can show them all at once.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.errors: dict[str, str] = {} if field is None else {field: message}


def parse_edit_value(field: str, raw: str) -> EditValue:
    """Parse a form value for one of store.EDITABLE_FIELDS. Money accepts the same forms as
    importing ("$120,000", "120k"); dates are ISO from <input type="date">; milestones are
    whole numbers 0 or more; name can't be blank; domain is normalized with
    matching.normalize_domain (blank means none). Raises EditError(field=field)."""
    if field not in EDITABLE_FIELDS:
        raise EditError(_CANT_EDIT)
    label = FIELD_LABELS[field]
    text = raw.strip()
    if field == "name":
        if not text:
            raise EditError(f"{label} can't be empty.", field)
        return text
    if field == "domain":
        if not text:
            return None
        domain = normalize_domain(text)
        if domain is None:
            raise EditError(f"Website {_quoted(text)} doesn't look like a website.", field)
        return domain
    if field == "arr":
        if not text:
            raise EditError(f"{label} can't be empty.", field)
        try:
            return importing.parse_money(text)
        except ValueError:
            reason = "can't be negative." if _has_minus(text) else "isn't an amount."
            raise EditError(f"{label} {_quoted(text)} {reason}", field) from None
    if field in ("tier", "owner"):
        if text:
            return text
        return _UNKNOWN_TIER if field == "tier" else _UNASSIGNED
    if field in _DATE_FIELDS:
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise EditError(f"{label} isn't a date.", field) from None
    # The three milestone counts.
    if re.fullmatch(r"[0-9]+", text) is None:
        raise EditError(f"{label} must be a whole number, 0 or more.", field)
    return int(text)


def create_account(store: LocalStore, values: dict[str, str], *, today: date) -> Account:
    """Add an account by hand (enabling the manual connection if needed). Name, ARR and
    renewal date are required; the rest default as in importing.build_import. Returns it."""
    parsed, errors = _parse_submitted(values, skip_blank=True)
    for field in _REQUIRED_ON_CREATE:
        if not values.get(field, "").strip():
            errors[field] = f"{FIELD_LABELS[field]} is required."
    if not any(field in errors for field in _MILESTONE_FIELDS):
        counts = {field: parsed.get(field, 0) for field in _MILESTONE_FIELDS}
        if not _milestones_fit(counts):
            errors["milestones_done"] = _MILESTONES_TOO_MANY
    _raise_errors(errors)

    manual = store.manual()
    account = Account(
        id=_new_manual_id({existing.id for existing in manual.accounts}),
        name=parsed["name"],
        arr=parsed["arr"],
        tier=parsed.get("tier", _UNKNOWN_TIER),
        owner=parsed.get("owner", _UNASSIGNED),
        start_date=parsed.get("start_date", today),
        renewal_date=parsed["renewal_date"],
        milestones=Milestones(
            done=parsed.get("milestones_done", 0),
            total=parsed.get("milestones_total", 0),
            overdue=parsed.get("milestones_overdue", 0),
        ),
        domain=parsed.get("domain"),
    )
    connection = store.add_connection(ConnectionKind.MANUAL, _MANUAL_NAME)
    if not connection.enabled:
        store.update_connection(connection.model_copy(update={"enabled": True}))
    store.save_manual(manual.model_copy(update={"accounts": [*manual.accounts, account]}))
    return account


def delete_account(store: LocalStore, account_id: str) -> None:
    """Delete a hand-added account and its readings. Only "m-" ids; anything else raises
    EditError("Only accounts you added by hand can be deleted. Hide it instead.")."""
    if not account_id.startswith(_MANUAL_PREFIX):
        raise EditError(_DELETE_MANUAL_ONLY)
    manual = store.manual()
    if not any(account.id == account_id for account in manual.accounts):
        raise EditError(_NOT_IN_BOOK)
    store.save_manual(
        manual.model_copy(
            update={
                "accounts": [account for account in manual.accounts if account.id != account_id],
                "signals": [s for s in manual.signals if s.account_id != account_id],
            }
        )
    )
    for field in list(store.edits().get(account_id, {})):
        store.clear_edit(account_id, field)
    store.remove_notices(account_id)
    store.set_hidden(account_id, False)
    links = store.links()
    groups: list[LinkGroup] = []
    for group in links.groups:
        members = [member for member in group.members if member != account_id]
        if len(members) < 2:
            continue  # a group with fewer than 2 members is no longer a link
        primary = group.primary if group.primary in members else members[0]
        groups.append(LinkGroup(primary=primary, members=members))
    dismissed = [pair for pair in links.dismissed if account_id not in pair]
    store.save_links(LinkData(groups=groups, dismissed=dismissed))


def edit_account(
    store: LocalStore,
    connector: CombinedConnector,
    account_id: str,
    values: dict[str, str],
    *,
    now: datetime,
) -> list[str]:
    """Apply a submitted edit form. Returns the fields that changed.

    Validates every submitted field first (all-or-nothing). For an editable_in_place account,
    changes the manual account directly. Otherwise, for each field whose new value differs
    from what the book shows: if it equals the source value, clear any edit (a revert);
    else store FieldEdit(value, base_value=source value, edited_at=now). Milestones are
    validated together (done + overdue can't exceed total). Then connector.invalidate().
    """
    entry = _book_entry(connector, account_id)
    shown = _shown(entry)
    parsed, errors = _parse_submitted(values)
    if not any(field in errors for field in _MILESTONE_FIELDS):
        counts = {field: parsed.get(field, shown[field]) for field in _MILESTONE_FIELDS}
        if not _milestones_fit(counts):
            errors["milestones_done"] = _MILESTONES_TOO_MANY
    _raise_errors(errors)

    changed = [
        field
        for field in EDITABLE_FIELDS
        if field in parsed and _key(parsed[field]) != _key(shown[field])
    ]
    if entry.editable_in_place:
        _save_manual_fields(store, account_id, parsed, changed)
    else:
        stamp = _naive_utc(now)
        for field in changed:
            source = entry.source_values[field]
            if _key(parsed[field]) == _key(source):
                store.clear_edit(account_id, field)
            else:
                store.set_edit(
                    account_id,
                    field,
                    FieldEdit(
                        value=_stored(parsed[field]),
                        base_value=_stored(source),
                        edited_at=stamp,
                    ),
                )
    connector.invalidate()
    return changed


def revert_field(
    store: LocalStore, connector: CombinedConnector, account_id: str, field: str
) -> None:
    """Drop the edit on one field so the connection's value shows again; invalidate."""
    if field not in EDITABLE_FIELDS:
        raise EditError(_CANT_EDIT, field)
    _book_entry(connector, account_id)
    store.clear_edit(account_id, field)
    connector.invalidate()


def add_reading(
    store: LocalStore,
    connector: CombinedConnector,
    account_id: str,
    kind: SignalKind,
    raw_value: str,
    *,
    now: datetime,
) -> None:
    """Record a hand-entered signal reading (source="manual", observed_at=now as naive UTC).
    It becomes the latest reading for that kind until a connection reports a newer one.
    Values are parsed like the matching import field type; CHAMPION_LEFT takes yes/no.
    Raises EditError for an unknown account or an invalid value."""
    _book_entry(connector, account_id)
    try:
        signal_kind = SignalKind(kind)
    except ValueError:
        raise EditError("That signal isn't one cs-brief knows.") from None
    reading = Signal(
        account_id=account_id,
        kind=signal_kind,
        value=_reading_value(signal_kind, raw_value),
        observed_at=_naive_utc(now),
        source="manual",
    )
    manual = store.manual()
    store.save_manual(manual.model_copy(update={"signals": [*manual.signals, reading]}))
    connector.invalidate()


def set_hidden(
    store: LocalStore, connector: CombinedConnector, account_id: str, hidden: bool
) -> None:
    """Hide or show an account; for a linked account, every member id. Invalidate."""
    if hidden:
        _book_entry(connector, account_id)
    elif account_id not in store.hidden() and connector.provenance(account_id) is None:
        raise EditError(_NOT_IN_BOOK)  # neither in the book nor hidden: there is nothing to show
    for member in _group_members(store, account_id):
        store.set_hidden(member, hidden)
    connector.invalidate()


def link_accounts(
    store: LocalStore, connector: CombinedConnector, account_ids: list[str], primary: str
) -> None:
    """Confirm a duplicate: merge these ids (and any groups they already belong to) into one
    LinkGroup with `primary`. Ids must exist in the book and come from 2+ connections.

    Edits stored under a non-primary member move to the primary (the primary's own edit wins
    when both edited the same field). Each moved edit is rebased onto the merged account's
    current source value, so the link doesn't discard it. Their notices follow. Invalidate."""
    ids = list(dict.fromkeys(account_ids))
    if len(ids) < 2:
        raise EditError(_LINK_TWO)
    entries = [_book_entry(connector, account_id) for account_id in ids]
    if primary not in ids:
        raise EditError(_LINK_PRIMARY)
    connections = {connection_for(member) for entry in entries for member in entry.member_ids}
    if len(connections - {None}) < 2:
        raise EditError(_LINK_CONNECTIONS)

    links = store.links()
    wanted = set(ids)
    kept = [group for group in links.groups if wanted.isdisjoint(group.members)]
    pulled = [
        member
        for group in links.groups
        if not wanted.isdisjoint(group.members)
        for member in group.members
    ]
    members = list(dict.fromkeys([primary, *ids, *pulled]))  # primary first, then group order
    kept.append(LinkGroup(primary=primary, members=members))
    store.save_links(LinkData(groups=kept, dismissed=links.dismissed))
    # Read the merged account's source values from the book with the new link, so each moved
    # edit can be rebased onto what the merged account now reports.
    connector.invalidate()
    merged = _book_entry(connector, primary)
    _move_edits_and_notices(
        store, [m for m in members if m != primary], primary, merged.source_values
    )
    connector.invalidate()


def unlink(store: LocalStore, connector: CombinedConnector, primary: str) -> None:
    """Undo a link; the members show as separate accounts again."""
    links = store.links()
    kept = [group for group in links.groups if primary not in group.members]
    if len(kept) == len(links.groups):
        if connector.provenance(primary) is None:
            raise EditError(_NOT_IN_BOOK)
        raise EditError(_UNLINKED)
    store.save_links(LinkData(groups=kept, dismissed=links.dismissed))
    connector.invalidate()


def dismiss_duplicate(
    store: LocalStore, connector: CombinedConnector, account_ids: tuple[str, str]
) -> None:
    """Mark a suggested pair as "not the same" so it isn't suggested again."""
    first, second = account_ids
    if first == second:
        raise EditError("Choose two different accounts.")
    _book_entry(connector, first)
    _book_entry(connector, second)
    pair = (first, second) if first < second else (second, first)
    links = store.links()
    if pair not in links.dismissed:
        store.save_links(LinkData(groups=links.groups, dismissed=[*links.dismissed, pair]))
    connector.invalidate()


# Helpers -------------------------------------------------------------------------------------


def _book_entry(connector: CombinedConnector, account_id: str) -> Provenance:
    """The book's entry for an account id, or EditError when the book doesn't have it."""
    entry = connector.provenance(account_id)
    if entry is None:
        raise EditError(_NOT_IN_BOOK)
    return entry


def _shown(entry: Provenance) -> dict[str, EditValue]:
    """What the book shows for each editable field now: the edit where there is one, else the
    source value."""
    return {
        field: entry.edits[field].value if field in entry.edits else entry.source_values[field]
        for field in EDITABLE_FIELDS
    }


def _parse_submitted(
    values: Mapping[str, str], *, skip_blank: bool = False
) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse every submitted editable field. Returns the values and the messages by field, in
    EDITABLE_FIELDS order. Nothing is written here. With skip_blank, blank fields are left out."""
    parsed: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in EDITABLE_FIELDS:
        if field not in values:
            continue
        if skip_blank and not values[field].strip():
            continue
        try:
            parsed[field] = parse_edit_value(field, values[field])
        except EditError as error:
            errors[field] = str(error)
    return parsed, errors


def _raise_errors(errors: dict[str, str]) -> None:
    """Raise EditError for the first message in EDITABLE_FIELDS order, carrying all of them."""
    ordered = {field: errors[field] for field in EDITABLE_FIELDS if field in errors}
    if not ordered:
        return
    first = next(iter(ordered))
    error = EditError(ordered[first], first)
    error.errors = ordered
    raise error


def _milestones_fit(counts: Mapping[str, Any]) -> bool:
    """Done and overdue together can't exceed the total (Milestones enforces the same rule)."""
    done = int(counts["milestones_done"])
    overdue = int(counts["milestones_overdue"])
    return done + overdue <= int(counts["milestones_total"])


def _save_manual_fields(
    store: LocalStore, account_id: str, parsed: Mapping[str, Any], changed: list[str]
) -> None:
    """Change a hand-added account in place. Only the changed fields are written."""
    if not changed:
        return
    manual = store.manual()
    current = next((account for account in manual.accounts if account.id == account_id), None)
    if current is None:
        raise EditError(_NOT_IN_BOOK)
    update: dict[str, Any] = {
        field: parsed[field] for field in changed if field not in _MILESTONE_FIELDS
    }
    if any(field in changed for field in _MILESTONE_FIELDS):
        update["milestones"] = Milestones(
            done=parsed.get("milestones_done", current.milestones.done),
            total=parsed.get("milestones_total", current.milestones.total),
            overdue=parsed.get("milestones_overdue", current.milestones.overdue),
        )
    updated = current.model_copy(update=update)
    accounts = [updated if account.id == account_id else account for account in manual.accounts]
    store.save_manual(manual.model_copy(update={"accounts": accounts}))


def _reading_value(kind: SignalKind, raw: str) -> float:
    """A hand-entered reading as the number the signal is stored with. Raises EditError."""
    label = _READING_LABELS[kind]
    text = raw.strip()
    if kind is SignalKind.CHAMPION_LEFT:
        try:
            return 1.0 if importing.parse_bool(text) else 0.0
        except ValueError:
            raise EditError(f"{label} {_quoted(text)} isn't yes or no.") from None
    if not text:
        raise EditError(f"{label} can't be empty.")
    number = _number(text, label)
    if kind is SignalKind.NPS_LATEST and not 0 <= number <= 10:
        raise EditError(f"{label} should be between 0 and 10.")
    if kind in _COUNT_KINDS and (number < 0 or not number.is_integer()):
        raise EditError(f"{label} must be a whole number, 0 or more.")
    return number


def _number(text: str, label: str) -> float:
    try:
        return importing.parse_number(text)
    except ValueError:
        raise EditError(f"{label} {_quoted(text)} isn't a number.") from None


def _group_members(store: LocalStore, account_id: str) -> list[str]:
    """Every id in the link group that holds account_id, or just account_id when it isn't linked."""
    for group in store.links().groups:
        if account_id in group.members:
            return list(group.members)
    return [account_id]


def _move_edits_and_notices(
    store: LocalStore,
    sources: list[str],
    target: str,
    source_values: Mapping[str, EditValue],
) -> None:
    """Move each source's edits and notices onto target.

    A moved edit is rebased: its base_value becomes target's current source value for that field,
    so it still applies after the link. Where target already has an edit on a field, its own edit
    wins and the source's edit is dropped. A moved edit that already equals target's source value
    is a revert, so it is dropped, as edit_account drops one."""
    edits = store.edits()
    target_edits = edits.setdefault(target, {})
    for source in sources:
        for field, edit in edits.get(source, {}).items():
            if field not in target_edits:
                base = source_values[field]
                if _key(edit.value) != _key(base):
                    moved = FieldEdit(
                        value=edit.value, base_value=_stored(base), edited_at=edit.edited_at
                    )
                    store.set_edit(target, field, moved)
                    target_edits[field] = moved
            store.clear_edit(source, field)
        store.move_notices(source, target)


def _new_manual_id(taken: set[str]) -> str:
    """A hand-added account id ("m-" plus 8 random hex characters) that no account uses yet."""
    while True:
        candidate = _MANUAL_PREFIX + secrets.token_hex(4)
        if candidate not in taken:
            return candidate


def _stored(value: EditValue) -> EditValue:
    """A value in the form the JSON store keeps: a date as ISO text, anything else as it is."""
    return value.isoformat() if isinstance(value, date) else value


def _naive_utc(moment: datetime) -> datetime:
    """A datetime as naive UTC, the shape the store keeps. A naive value is taken as UTC."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _quoted(text: str) -> str:
    """The value in double quotes, with spaces collapsed and cut to _QUOTE_LIMIT characters."""
    shown = " ".join(text.split())
    if len(shown) > _QUOTE_LIMIT:
        shown = shown[: _QUOTE_LIMIT - 1] + "…"
    return f'"{shown}"'


def _has_minus(text: str) -> bool:
    return re.search(r"[-−]", text) is not None
