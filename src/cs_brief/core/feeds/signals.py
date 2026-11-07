"""SIGNALS feeds: turn rows into readings on accounts that are already in the book.

CONTRACT FILE: signatures and rules are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from cs_brief.core.feeds.models import MatchBy, UnmatchedRow
from cs_brief.core.importing import DateOrder, ImportIssue, Table
from cs_brief.core.models import Account, Signal

MAX_UNMATCHED = 200  # unmatched rows kept for the status; the count is always exact

# The import fields a SIGNALS feed may map: the keys it matches on, and every "signal:*" field.
KEY_FIELDS = ("external_id", "domain", "name")


class SignalReadings(BaseModel):
    signals: list[Signal]  # account_id is the matched book account's id
    matched_accounts: int  # distinct accounts that got at least one reading
    unmatched: list[UnmatchedRow]  # at most MAX_UNMATCHED, in row order
    unmatched_count: int
    issues: list[ImportIssue]  # unreadable signal values, as importing reports them


def account_keys(account: Account) -> dict[MatchBy, set[str]]:
    """What a row can match this account on, normalised:
    - ID: the account id lowercased, and that id without its connection prefix ("hs-123" and
      "123"; "sp2-acme-inc" and "acme-inc"; "fd1-x" and "x"; "m-ab12" and "ab12").
    - WEBSITE: matching.normalize_domain(account.domain), when there is one.
    - NAME: matching.normalize_name(account.name)."""
    raise NotImplementedError


def build_signal_readings(
    table: Table,
    mapping: dict[str, str],
    *,
    match_by: MatchBy,
    accounts: list[Account],
    date_order: DateOrder,
    observed_at: datetime,
    source: str,
) -> SignalReadings:
    """For each row: find the account (see below), then read every mapped "signal:*" column with
    importing's own parsing (the same values and ranges an import accepts). Each value becomes
    Signal(account_id=<matched id>, kind, value, observed_at as naive UTC, source=source).

    Finding the account: the row's key for each MatchBy comes from the mapped column (ID ->
    "external_id", WEBSITE -> "domain", NAME -> "name"), normalised like account_keys. A key
    matches when exactly one account has it. AUTO tries ID, then WEBSITE, then NAME, and takes the
    first that matches exactly one. A row whose key matches nothing, or several accounts, goes to
    `unmatched` with a plain reason ("No account has the website acme.com.", "2 accounts are
    called Acme."), and a row with no key at all is unmatched with "This row has no ID, website or
    name.". When two rows match the same account, the later row's readings win for that kind.

    Raises ValueError (plain message) when the mapping has no key column, or no signal column."""
    raise NotImplementedError
