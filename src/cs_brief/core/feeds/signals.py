"""SIGNALS feeds: turn rows into readings on accounts that are already in the book.

CONTRACT FILE: signatures and rules are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel

from cs_brief.core import importing
from cs_brief.core.feeds.models import MatchBy, UnmatchedRow
from cs_brief.core.importing import DateOrder, ImportIssue, Table
from cs_brief.core.matching import normalize_domain, normalize_name
from cs_brief.core.models import Account, Signal, SignalKind

MAX_UNMATCHED = 200  # unmatched rows kept for the status; the count is always exact

# The import fields a SIGNALS feed may map: the keys it matches on, and every "signal:*" field.
KEY_FIELDS = ("external_id", "domain", "name")

# The import field each match method reads, in AUTO's order, and how a reason names it.
_KEY_FIELD: dict[MatchBy, str] = {
    MatchBy.ID: "external_id",
    MatchBy.WEBSITE: "domain",
    MatchBy.NAME: "name",
}
_AUTO_ORDER = (MatchBy.ID, MatchBy.WEBSITE, MatchBy.NAME)
_LABEL: dict[MatchBy, str] = {
    MatchBy.ID: "ID",
    MatchBy.WEBSITE: "website",
    MatchBy.NAME: "account name",
}
# A connection prefix on an account id: "hs-", "m-", "sp<n>-" or "fd<n>-".
_CONNECTION_PREFIX = re.compile(r"(?:hs|m|sp[0-9]+|fd[0-9]+)-")


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
    keys: dict[MatchBy, set[str]] = {MatchBy.ID: set(), MatchBy.WEBSITE: set(), MatchBy.NAME: set()}
    account_id = account.id.casefold()
    keys[MatchBy.ID].add(account_id)
    prefix = _CONNECTION_PREFIX.match(account_id)
    if prefix is not None and prefix.end() < len(account_id):
        keys[MatchBy.ID].add(account_id[prefix.end() :])
    domain = normalize_domain(account.domain)
    if domain is not None:
        keys[MatchBy.WEBSITE].add(domain)
    name = normalize_name(account.name)
    if name:
        keys[MatchBy.NAME].add(name)
    return keys


def _normalise(match_by: MatchBy, text: str) -> str | None:
    """A row's key text as the account keys compare it, or None when it can't match anything."""
    if match_by is MatchBy.ID:
        return text.strip().casefold() or None
    if match_by is MatchBy.WEBSITE:
        return normalize_domain(text)
    return normalize_name(text) or None


def _reason(match_by: MatchBy, text: str, count: int) -> str:
    if match_by is MatchBy.ID:
        if count == 0:
            return f"No account has the ID {text}."
        return f"{count} accounts have the ID {text}."
    if match_by is MatchBy.WEBSITE:
        if count == 0:
            return f"No account has the website {text}."
        return f"{count} accounts have the website {text}."
    if count == 0:
        return f"No account is called {text}."
    return f"{count} accounts are called {text}."


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _match_row(
    row: list[str],
    columns: dict[str, int],
    number: int,
    methods: tuple[MatchBy, ...],
    index: dict[MatchBy, dict[str, list[str]]],
) -> tuple[str | None, UnmatchedRow | None]:
    """The matched account id for one row, or (None, why it is unmatched). For AUTO a row that
    matches nothing reports the first ambiguous method's reason when there is one, else the first
    method's, so a name shared by two accounts is said plainly."""
    problems: list[tuple[bool, UnmatchedRow]] = []  # (ambiguous, reason), in method order
    for method in methods:
        text = importing._cell(row, columns, _KEY_FIELD[method])
        if not text:
            continue
        key = _normalise(method, text)
        found = index[method].get(key, []) if key is not None else []
        if len(found) == 1:
            return found[0], None
        reason = UnmatchedRow(row=number, key=text, reason=_reason(method, text, len(found)))
        problems.append((len(found) > 1, reason))
    if not problems:
        if len(methods) == 1:
            reason_text = f"This row has no {_LABEL[methods[0]]}."
        else:
            reason_text = "This row has no ID, website or name."
        return None, UnmatchedRow(row=number, key="", reason=reason_text)
    ambiguous = [reason for flagged, reason in problems if flagged]
    return None, (ambiguous or [reason for _, reason in problems])[0]


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
    # importing's own helpers (the column lookup, cell reading, value conversion and row
    # numbering build_import uses), so a feed reads a value exactly as an import does.
    columns = importing._mapped_columns(table, mapping)
    methods = _AUTO_ORDER if match_by is MatchBy.AUTO else (match_by,)
    if not any(_KEY_FIELD[method] in columns for method in methods):
        if match_by is MatchBy.AUTO:
            raise ValueError("Match a column to ID, Website or Account name.")
        raise ValueError(f"Match a column to {_LABEL[match_by]}.")
    specs = [
        spec
        for spec in importing.IMPORT_FIELDS
        if spec.key.startswith("signal:") and spec.key in columns
    ]
    if not specs:
        raise ValueError("Match at least one signal column, such as Usage trend.")

    index: dict[MatchBy, dict[str, list[str]]] = {method: {} for method in methods}
    seen: set[str] = set()
    for account in accounts:
        if account.id in seen:
            continue
        seen.add(account.id)
        keys = account_keys(account)
        for method in methods:
            for key in keys[method]:
                index[method].setdefault(key, []).append(account.id)

    observed = _naive_utc(observed_at)
    readings: dict[tuple[str, SignalKind], Signal] = {}  # the later row replaces an earlier one
    issues: list[ImportIssue] = []
    unmatched: list[UnmatchedRow] = []
    unmatched_count = 0
    for number, row in zip(importing._row_numbers(table), table.rows, strict=True):
        account_id, problem = _match_row(row, columns, number, methods, index)
        if account_id is None:
            unmatched_count += 1
            if problem is not None and len(unmatched) < MAX_UNMATCHED:
                unmatched.append(problem)
            continue
        for spec in specs:
            text = importing._cell(row, columns, spec.key)
            if not text:
                continue
            try:
                value = importing._convert(spec, text, date_order)
            except ValueError as exc:
                issues.append(
                    ImportIssue(row=number, field=spec.key, message=f"{spec.label} {exc}")
                )
                continue
            kind = importing.SIGNAL_FIELD_KINDS[spec.key]
            readings[(account_id, kind)] = Signal(
                account_id=account_id,
                kind=kind,
                value=float(value),
                observed_at=observed,
                source=source,
            )

    signals = list(readings.values())
    return SignalReadings(
        signals=signals,
        matched_accounts=len({signal.account_id for signal in signals}),
        unmatched=unmatched,
        unmatched_count=unmatched_count,
        issues=issues,
    )
