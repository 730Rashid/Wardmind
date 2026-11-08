"""Setting up (or changing) a feed: the setup form, Test connection, column matching, Save.

Flow on the web: the provider's setup page posts its values -> start_draft fetches the source
once (that is the test) and saves a FeedDraft -> the matching page shows the columns found,
pre-matched, with a preview -> commit_draft creates or updates the connection and runs its first
sync from the table already fetched. A draft lives under store.data_dir()/feed-drafts/<id>.json
and expires after DRAFT_TTL like an import draft; its secret, if one was typed, waits under
secrets name "draft-<id>" and moves to "feed-<n>" on commit (deleted on cancel or expiry).

CONTRACT FILE: models and signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PureWindowsPath
from secrets import token_hex

from pydantic import BaseModel

from cs_brief.core import import_flow, importing
from cs_brief.core import secrets as vault
from cs_brief.core.config import write_atomic
from cs_brief.core.feeds import catalog, signals, sync
from cs_brief.core.feeds.http import FeedError, check_url
from cs_brief.core.feeds.models import (
    FeedRole,
    FeedSettings,
    FeedStatus,
    FieldKind,
    MatchBy,
    ProviderInfo,
    Schedule,
    SetupField,
    UnmatchedRow,
)
from cs_brief.core.importing import DateOrder, FieldType, ImportIssue, Table
from cs_brief.core.models import Account
from cs_brief.core.store import FEED_DRAFTS_DIR, ConnectionConfig, ConnectionKind, LocalStore

DRAFT_TTL = timedelta(hours=24)
DRAFTS_DIR = FEED_DRAFTS_DIR

_DRAFT_ID = re.compile(r"[0-9a-f]{16}")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")  # an HTTP header name
_AUTH_NEEDS_SECRET = frozenset({"bearer", "header", "basic"})
_PREVIEW_ISSUES = 10  # the first problems the preview shows
_PREVIEW_UNMATCHED = 5  # the first unmatched rows the preview shows
_NO_READY = "No accounts are ready to save. Fix the rows with problems, or match other columns."


class FeedDraft(BaseModel):
    id: str  # 16 hex characters
    created_at: datetime  # naive UTC
    provider: str  # catalog id
    name: str  # what the CSM called the connection
    role: FeedRole
    settings: dict[str, str]  # non-secret values only
    schedule: Schedule
    match_by: MatchBy
    connection_id: str | None = None  # set when changing an existing feed
    has_secret: bool = False  # a secret was typed for this draft (under "draft-<id>")
    table: Table  # what the test fetch read
    mapping: dict[str, str] | None = None  # the CSM's last choices, as import drafts keep them
    date_order: str | None = None


class FeedPreview(BaseModel):
    """The matching page's "What will happen" for either role."""

    rows: int
    ready: int  # ACCOUNTS: accounts ready to import; SIGNALS: rows that matched an account
    accounts: int  # SIGNALS: distinct accounts that will get readings; ACCOUNTS: same as ready
    blocker: str | None  # why it can't be saved yet, e.g. "Match a column to Account name."
    issues: list[ImportIssue]  # the first problems, as the import shows them
    unmatched: list[UnmatchedRow]  # SIGNALS only, the first few


BookAccounts = Callable[[str | None], list[Account]]
"""The book's accounts without the given connection's own data (None: the whole book)."""


def _info(provider_id: str) -> ProviderInfo:
    info = catalog.provider_info(provider_id)
    if info is None:
        raise ValueError("cs-brief doesn't know that kind of connection.")
    return info


def _field(info: ProviderInfo, key: str) -> SetupField:
    return next(field for field in info.fields if field.key == key)


def _is_absolute(path: str) -> bool:
    return PureWindowsPath(path).is_absolute() or path.startswith("/")


def _needs_secret(info: ProviderInfo, values: dict[str, str]) -> bool:
    """Whether this setup can't read its source without a key, token or password."""
    if any(field.kind is FieldKind.SECRET and field.required for field in info.fields):
        return True
    return values.get("auth", "none") in _AUTH_NEEDS_SECRET


# The settings that decide where a key is sent. Changing any of them means the saved key is not
# reused (start_draft), so a key typed for one site never goes to another without being retyped.
_DESTINATION_KEYS = ("url", "host", "site", "subdomain", "domain")


def _destination(settings: Mapping[str, str]) -> tuple[str, ...]:
    return tuple((settings.get(key) or "").strip().casefold() for key in _DESTINATION_KEYS)


def _secret_field(info: ProviderInfo) -> SetupField | None:
    return next((field for field in info.fields if field.kind is FieldKind.SECRET), None)


def validate_values(provider_id: str, values: dict[str, str]) -> dict[str, str]:
    """The setup form's values for this provider: every non-secret SetupField key, stripped,
    defaults filled in, required ones present, CHOICE values among the choices, URL fields
    passing feeds.http.check_url, PATH fields absolute. Raises ValueError with a message naming
    the field ("Fill in Subdomain.", "File must be a full path such as C:\\Exports\\a.csv.").
    Unknown keys are dropped. The secret is not part of the result."""
    info = _info(provider_id)
    clean: dict[str, str] = {}
    for field in info.fields:
        if field.kind is FieldKind.SECRET:
            continue
        clean[field.key] = (values.get(field.key) or "").strip() or field.default
    for field in info.fields:
        if field.kind is FieldKind.SECRET:
            continue
        raw = clean[field.key]
        if not raw:
            if field.required:
                raise ValueError(f"Fill in {field.label}.")
            continue
        if field.kind is FieldKind.CHOICE and raw not in {value for value, _ in field.choices}:
            raise ValueError(f"Pick one of the choices for {field.label}.")
        if field.kind is FieldKind.URL:
            try:
                check_url(raw)
            except FeedError as exc:
                raise ValueError(exc.message) from None
        if field.kind is FieldKind.PATH and not _is_absolute(raw):
            raise ValueError(f"{field.label} must be a full path such as C:\\Exports\\a.csv.")
    if clean.get("auth") == "header":
        name = clean.get("header_name", "")
        if not name:
            raise ValueError(f"Fill in {_field(info, 'header_name').label}.")
        if _HEADER_NAME.fullmatch(name) is None:
            raise ValueError(
                f"{_field(info, 'header_name').label} must be a header name, such as X-API-Key."
            )
    if clean.get("auth") == "basic" and not clean.get("username"):
        raise ValueError(f"Fill in {_field(info, 'username').label}.")
    return clean


def _naive(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _draft_path(store: LocalStore, draft_id: str) -> Path:
    return store.root / DRAFTS_DIR / f"{draft_id}.json"


def _secret_for(draft_id: str) -> str:
    return f"draft-{draft_id}"


def _write(store: LocalStore, draft: FeedDraft) -> None:
    write_atomic(_draft_path(store, draft.id), draft.model_dump_json())


def _read(path: Path) -> FeedDraft | None:
    try:
        return FeedDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start_draft(
    store: LocalStore,
    provider_id: str,
    *,
    name: str,
    role: FeedRole,
    values: dict[str, str],
    secret: str | None,
    schedule: Schedule,
    match_by: MatchBy,
    connection_id: str | None,
    now: datetime,
) -> FeedDraft:
    """validate_values, check role and schedule are among the provider's, then fetch once:
    with `secret` when one was typed, else (when changing a feed) its saved secret. The typed
    secret is saved under "draft-<id>" only once the fetch worked. Expired drafts are purged
    first. Raises ValueError for the form, FeedError for the source; nothing is saved then."""
    info = _info(provider_id)
    clean = validate_values(provider_id, values)
    if role not in info.roles:
        raise ValueError(f"{info.name} can't be used for that.")
    if schedule not in info.schedules:
        raise ValueError("Pick one of the sync schedules offered.")
    feed_name = import_flow.validate_name(name or info.name)
    moment = _naive(now)
    import_flow.purge_old_drafts(store, now=moment)
    existing: ConnectionConfig | None = None
    if connection_id is not None:
        existing = store.get_connection(connection_id)
        if existing is None or existing.kind is not ConnectionKind.FEED or existing.feed is None:
            raise ValueError("That connection was removed. Start again from Add a connection.")
    if existing is not None and existing.feed is not None and existing.feed.provider != provider_id:
        raise ValueError("That connection uses another source. Start again from Add a connection.")
    typed = vault.validate_secret(secret) if secret and secret.strip() else None
    fetch_secret = typed
    if fetch_secret is None and existing is not None and existing.feed is not None:
        if _destination(existing.feed.settings) != _destination(clean):
            # The saved key was given for one address; it is never sent to a new one unasked.
            if _needs_secret(info, clean):
                raise ValueError(
                    "The address changed, so type the key again. "
                    "cs-brief only sends a saved key to the address it was saved for."
                )
        else:
            fetch_secret = vault.get_secret(sync.secret_name(existing.id))
    if fetch_secret is None and _needs_secret(info, clean):
        field = _secret_field(info)
        raise ValueError(f"Fill in {field.label if field else 'the key'}.")
    table = sync.fetch_with(provider_id, clean, fetch_secret)
    draft_id = token_hex(8)
    if typed is not None:
        vault.save_secret(_secret_for(draft_id), typed)
    mapping: dict[str, str] | None = None
    date_order: str | None = None
    if existing is not None and existing.mapping:
        mapping = import_flow.clean_mapping(table, existing.mapping) or None
        date_order = existing.date_order
    draft = FeedDraft(
        id=draft_id,
        created_at=moment,
        provider=provider_id,
        name=feed_name,
        role=role,
        settings=clean,
        schedule=schedule,
        match_by=match_by,
        connection_id=existing.id if existing is not None else None,
        has_secret=typed is not None,
        table=table,
        mapping=mapping,
        date_order=date_order,
    )
    _write(store, draft)
    return draft


def load_draft(store: LocalStore, draft_id: str, *, now: datetime) -> FeedDraft | None:
    """The draft, or None when the id isn't 16 hex characters, there is no such draft, or it
    has expired (an expired one is deleted with its secret)."""
    if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
        return None
    path = _draft_path(store, draft_id)
    if not path.is_file():
        return None
    draft = _read(path)
    if draft is None or draft.id != draft_id:
        return None
    if _naive(now) - draft.created_at > DRAFT_TTL:
        path.unlink(missing_ok=True)
        vault.delete_secret(_secret_for(draft_id))
        return None
    return draft


def _keep_for_role(role: FeedRole, mapping: dict[str, str]) -> dict[str, str]:
    """A SIGNALS feed maps only its key columns and signal columns."""
    if role is not FeedRole.SIGNALS:
        return mapping
    return {
        key: header
        for key, header in mapping.items()
        if key in signals.KEY_FIELDS or key.startswith("signal:")
    }


def suggested_mapping(draft: FeedDraft) -> dict[str, str]:
    """The draft's own mapping if the CSM chose one, else importing.auto_map(draft.table
    .headers). For a SIGNALS draft only key fields (signals.KEY_FIELDS) and "signal:*" fields are
    kept."""
    if draft.mapping is not None:
        chosen = import_flow.clean_mapping(draft.table, draft.mapping)
    else:
        chosen = importing.auto_map(draft.table.headers)
    kept = _keep_for_role(draft.role, chosen)
    return {spec.key: kept[spec.key] for spec in importing.IMPORT_FIELDS if spec.key in kept}


def _resolved_order(table: Table, mapping: dict[str, str], chosen: str | None) -> DateOrder:
    """The date order an import uses: the one the mapped date columns imply, else the CSM's
    choice, else day first (the same rule as import_flow.resolve_date_order)."""
    values: list[str] = []
    for spec in importing.IMPORT_FIELDS:
        if spec.type is FieldType.DATE and spec.key in mapping:
            index = table.headers.index(mapping[spec.key])
            values.extend(row[index] for row in table.rows)
    implied = importing.detect_date_order(values)
    if implied is not None:
        return implied
    try:
        return DateOrder(chosen) if chosen else DateOrder.DMY
    except ValueError:
        return DateOrder.DMY


def _match_message(labels: list[str]) -> str:
    if len(labels) == 1:
        return f"Match {labels[0]} to import."
    return f"Match {', '.join(labels[:-1])} and {labels[-1]} to import."


def preview(
    draft: FeedDraft,
    mapping: dict[str, str],
    date_order: str | None,
    *,
    book_accounts: BookAccounts,
    now: datetime,
    today: date,
) -> FeedPreview:
    """What saving would do, without saving. Also stores mapping and date_order on the draft.

    The draft is updated in memory only: the caller saves it (commit_draft does, with the
    choices it is given)."""
    cleaned = _keep_for_role(draft.role, import_flow.clean_mapping(draft.table, mapping))
    draft.mapping = cleaned
    draft.date_order = date_order
    order = _resolved_order(draft.table, cleaned, date_order)
    rows = len(draft.table.rows)

    if draft.role is FeedRole.ACCOUNTS:
        missing = [
            spec.label
            for spec in importing.IMPORT_FIELDS
            if spec.required and spec.key not in cleaned
        ]
        if missing:
            return FeedPreview(
                rows=rows,
                ready=0,
                accounts=0,
                blocker=_match_message(missing),
                issues=[],
                unmatched=[],
            )
        result = importing.build_import(
            draft.table,
            cleaned,
            connection_id=draft.connection_id or "feed-1",
            date_order=order,
            imported_at=_naive(now),
            today=today,
        )
        return FeedPreview(
            rows=rows,
            ready=result.ready_rows,
            accounts=result.ready_rows,
            blocker=_NO_READY if result.ready_rows == 0 else None,
            issues=result.issues[:_PREVIEW_ISSUES],
            unmatched=[],
        )

    if not rows:
        return FeedPreview(
            rows=0, ready=0, accounts=0, blocker=sync.NO_SIGNAL_ROWS, issues=[], unmatched=[]
        )
    try:
        readings = signals.build_signal_readings(
            draft.table,
            cleaned,
            match_by=draft.match_by,
            accounts=book_accounts(draft.connection_id),
            date_order=order,
            observed_at=_naive(now),
            source="feed:preview",
        )
    except ValueError as exc:
        return FeedPreview(
            rows=rows, ready=0, accounts=0, blocker=str(exc), issues=[], unmatched=[]
        )
    ready = rows - readings.unmatched_count
    # Saving a feed that would fail on its first sync is refused here, as an ACCOUNTS import is.
    blocker = sync.no_match_message(rows) if ready == 0 else None
    return FeedPreview(
        rows=rows,
        ready=ready,
        accounts=readings.matched_accounts,
        blocker=blocker,
        issues=readings.issues[:_PREVIEW_ISSUES],
        unmatched=readings.unmatched[:_PREVIEW_UNMATCHED],
    )


def commit_draft(
    store: LocalStore,
    draft: FeedDraft,
    mapping: dict[str, str],
    date_order: str | None,
    *,
    book_accounts: BookAccounts,
    now: datetime,
    today: date,
) -> ConnectionConfig:
    """Save the feed: a new FEED connection (store.add_connection, then its feed settings) or
    the existing one updated. The draft's secret becomes the feed's (secrets.rename_secret).
    Then the first sync runs from draft.table (sync.sync_feed with table=draft.table), and the
    draft is deleted. Raises ValueError (and saves nothing) when preview() has a blocker."""
    info = _info(draft.provider)
    checked = preview(draft, mapping, date_order, book_accounts=book_accounts, now=now, today=today)
    if checked.blocker:
        raise ValueError(checked.blocker)
    cleaned = _keep_for_role(draft.role, import_flow.clean_mapping(draft.table, mapping))
    order = _resolved_order(draft.table, cleaned, date_order)
    feed_name = import_flow.validate_name(draft.name or info.name)

    if draft.connection_id is None:
        config = store.add_connection(ConnectionKind.FEED, feed_name)
    else:
        existing = store.get_connection(draft.connection_id)
        if existing is None or existing.kind is not ConnectionKind.FEED:
            raise ValueError("That feed was removed. Start again from Add a connection.")
        config = existing
    if draft.has_secret:
        vault.rename_secret(_secret_for(draft.id), sync.secret_name(config.id))

    feed = FeedSettings(
        provider=draft.provider,
        role=draft.role,
        settings=dict(draft.settings),
        schedule=draft.schedule,
        match_by=draft.match_by,
        status=FeedStatus(),
    )
    updated = config.model_copy(
        update={
            "name": feed_name,
            "enabled": True,
            "mapping": cleaned,
            "date_order": order.value,
            "feed": feed,
        }
    )
    store.update_connection(updated)
    book = book_accounts(updated.id) if draft.role is FeedRole.SIGNALS else []
    sync.sync_feed(
        updated,
        store,
        book_accounts=book,
        now=_naive(now),
        today=today,
        table=draft.table,
    )
    _draft_path(store, draft.id).unlink(missing_ok=True)
    return store.get_connection(updated.id) or updated


def cancel_draft(store: LocalStore, draft_id: str) -> None:
    """Delete the draft and its secret, if any."""
    if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
        return
    _draft_path(store, draft_id).unlink(missing_ok=True)
    vault.delete_secret(_secret_for(draft_id))


def remove_feed_secret(connection_id: str) -> None:
    """Delete a feed's saved secret; called when its connection is removed."""
    vault.delete_secret(sync.secret_name(connection_id))
