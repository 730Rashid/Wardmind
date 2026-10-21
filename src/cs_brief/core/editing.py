"""Everything a CSM can change by hand: add or delete their own accounts, edit any account,
enter a signal reading, hide an account, and link or dismiss duplicates.

The web layer calls these and nothing else for writes, so the rules live in one place.
Every function raises EditError (a ValueError whose str() is plain English) for bad input and
changes nothing in that case.

CONTRACT FILE: signatures and docstrings are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import date, datetime

from cs_brief.core.connectors.combined import CombinedConnector
from cs_brief.core.models import Account, SignalKind
from cs_brief.core.store import EditValue, LocalStore


class EditError(ValueError):
    """Bad input for an edit. str() is shown to the CSM next to the field."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def parse_edit_value(field: str, raw: str) -> EditValue:
    """Parse a form value for one of store.EDITABLE_FIELDS. Money accepts the same forms as
    importing ("$120,000", "120k"); dates are ISO from <input type="date">; milestones are
    whole numbers 0 or more; name can't be blank; domain is normalized with
    matching.normalize_domain (blank means none). Raises EditError(field=field)."""
    raise NotImplementedError


def create_account(store: LocalStore, values: dict[str, str], *, today: date) -> Account:
    """Add an account by hand (enabling the manual connection if needed). Name, ARR and
    renewal date are required; the rest default as in importing.build_import. Returns it."""
    raise NotImplementedError


def delete_account(store: LocalStore, account_id: str) -> None:
    """Delete a hand-added account and its readings. Only "m-" ids; anything else raises
    EditError("Only accounts you added by hand can be deleted. Hide it instead.")."""
    raise NotImplementedError


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
    raise NotImplementedError


def revert_field(
    store: LocalStore, connector: CombinedConnector, account_id: str, field: str
) -> None:
    """Drop the edit on one field so the connection's value shows again; invalidate."""
    raise NotImplementedError


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
    raise NotImplementedError


def set_hidden(
    store: LocalStore, connector: CombinedConnector, account_id: str, hidden: bool
) -> None:
    """Hide or show an account; for a linked account, every member id. Invalidate."""
    raise NotImplementedError


def link_accounts(
    store: LocalStore, connector: CombinedConnector, account_ids: list[str], primary: str
) -> None:
    """Confirm a duplicate: merge these ids (and any groups they already belong to) into one
    LinkGroup with `primary`. Ids must exist in the book and come from 2+ connections.

    Edits stored under a non-primary member move to the primary (the primary's own edit wins
    when both edited the same field), and their notices follow. Invalidate."""
    raise NotImplementedError


def unlink(store: LocalStore, connector: CombinedConnector, primary: str) -> None:
    """Undo a link; the members show as separate accounts again."""
    raise NotImplementedError


def dismiss_duplicate(
    store: LocalStore, connector: CombinedConnector, account_ids: tuple[str, str]
) -> None:
    """Mark a suggested pair as "not the same" so it isn't suggested again."""
    raise NotImplementedError
