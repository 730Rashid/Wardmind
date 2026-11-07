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

from collections.abc import Callable
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from cs_brief.core.feeds.models import FeedRole, MatchBy, Schedule, UnmatchedRow
from cs_brief.core.importing import ImportIssue, Table
from cs_brief.core.models import Account
from cs_brief.core.store import ConnectionConfig, LocalStore

DRAFT_TTL = timedelta(hours=24)
DRAFTS_DIR = "feed-drafts"


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


def validate_values(provider_id: str, values: dict[str, str]) -> dict[str, str]:
    """The setup form's values for this provider: every non-secret SetupField key, stripped,
    defaults filled in, required ones present, CHOICE values among the choices, URL fields
    passing feeds.http.check_url, PATH fields absolute. Raises ValueError with a message naming
    the field ("Fill in Subdomain.", "File must be a full path such as C:\\Exports\\a.csv.").
    Unknown keys are dropped. The secret is not part of the result."""
    raise NotImplementedError


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
    raise NotImplementedError


def load_draft(store: LocalStore, draft_id: str, *, now: datetime) -> FeedDraft | None:
    """The draft, or None when the id isn't 16 hex characters, there is no such draft, or it
    has expired (an expired one is deleted with its secret)."""
    raise NotImplementedError


def suggested_mapping(draft: FeedDraft) -> dict[str, str]:
    """The draft's own mapping if the CSM chose one, else importing.auto_map(draft.table
    .headers). For a SIGNALS draft only key fields (signals.KEY_FIELDS) and "signal:*" fields are
    kept."""
    raise NotImplementedError


def preview(
    draft: FeedDraft,
    mapping: dict[str, str],
    date_order: str | None,
    *,
    book_accounts: BookAccounts,
    now: datetime,
    today: date,
) -> FeedPreview:
    """What saving would do, without saving. Also stores mapping and date_order on the draft."""
    raise NotImplementedError


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
    raise NotImplementedError


def cancel_draft(store: LocalStore, draft_id: str) -> None:
    """Delete the draft and its secret, if any."""
    raise NotImplementedError


def remove_feed_secret(connection_id: str) -> None:
    """Delete a feed's saved secret; called when its connection is removed."""
    raise NotImplementedError
