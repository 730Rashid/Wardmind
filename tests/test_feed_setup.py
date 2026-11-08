"""Setting up a feed: the form's values, the Test fetch and its draft (fetched once, the typed key
saved only after it works, expiry), the column matching and the preview, and Save (a new or a
changed feed, its key moved over, the first sync run).

The source is a made-up web address answered by an httpx.MockTransport. Keys are made-up strings.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cs_brief.core import secrets as vault
from cs_brief.core.feeds import http as feed_http
from cs_brief.core.feeds import setup
from cs_brief.core.feeds.http import FeedError
from cs_brief.core.feeds.models import FeedRole, MatchBy, Schedule
from cs_brief.core.models import Account, Milestones, SignalKind
from cs_brief.core.secrets import FEED_SECRETS_DIR, secrets_dir
from cs_brief.core.store import ConnectionKind, LocalStore

NOW = datetime(2026, 11, 8, 9, 0)
TODAY = date(2026, 11, 8)
SECRET = "test-secret-0123456789abcdef"
LINK_URL = "https://exports.example.com/accounts.csv"
CSV = (
    "Company,ARR,Renewal date,Owner,Usage trend\n"
    '"Acme","$120,000",2027-03-01,Dana,-38\n'
    '"Beta","$50,000",2027-06-01,Sam,5\n'
)


class Served:
    """What the mock web address answers, and the requests it got."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body = CSV


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    result = Served()

    def answer(request: httpx.Request) -> httpx.Response:
        result.requests.append(request)
        return httpx.Response(
            result.status,
            content=result.body.encode("utf-8"),
            headers={"content-type": "text/csv"},
        )

    monkeypatch.setattr(feed_http, "transport_factory", lambda: httpx.MockTransport(answer))
    return result


def no_book(connection_id: str | None) -> list[Account]:
    return []


def acct(acc_id: str, name: str) -> Account:
    return Account(
        id=acc_id,
        name=name,
        arr=50000.0,
        tier="Growth",
        owner="Dana",
        start_date=date(2024, 1, 1),
        renewal_date=date(2027, 1, 1),
        milestones=Milestones(done=0, total=0, overdue=0),
    )


def start(
    store: LocalStore,
    *,
    provider: str = "link",
    values: dict[str, str] | None = None,
    secret: str | None = None,
    role: FeedRole = FeedRole.ACCOUNTS,
    schedule: Schedule = Schedule.DAILY,
    match_by: MatchBy = MatchBy.AUTO,
    connection_id: str | None = None,
    name: str = "Renewals",
    now: datetime = NOW,
) -> setup.FeedDraft:
    return setup.start_draft(
        store,
        provider,
        name=name,
        role=role,
        values=values if values is not None else {"url": LINK_URL, "auth": "none"},
        secret=secret,
        schedule=schedule,
        match_by=match_by,
        connection_id=connection_id,
        now=now,
    )


def feeds_folder() -> Path:
    return secrets_dir() / FEED_SECRETS_DIR


# validate_values -------------------------------------------------------------------------------


def test_a_missing_required_field_is_named() -> None:
    with pytest.raises(ValueError, match="Fill in Subdomain."):
        setup.validate_values("zendesk", {"email": "dana@acme.com"})


def test_defaults_are_filled_in_for_blank_fields() -> None:
    values = setup.validate_values("posthog", {"project_id": "123"})
    assert values["host"] == "https://us.posthog.com"
    assert values["group_key"] == "$group_0"
    assert values["project_id"] == "123"
    assert "secret" not in values


def test_text_is_stripped_and_unknown_or_secret_keys_are_dropped() -> None:
    values = setup.validate_values(
        "zendesk",
        {"subdomain": "  acme  ", "email": "dana@acme.com", "secret": "x", "extra": "y"},
    )
    assert values == {"subdomain": "acme", "email": "dana@acme.com"}


def test_a_choice_must_be_one_of_its_choices() -> None:
    with pytest.raises(ValueError, match="Pick one of the choices for Format."):
        setup.validate_values("link", {"url": LINK_URL, "format": "pdf"})


def test_a_web_address_must_pass_the_feed_address_rules() -> None:
    with pytest.raises(ValueError, match="must start with https://"):
        setup.validate_values("link", {"url": "http://example.com/a.csv"})
    with pytest.raises(ValueError, match="own box"):
        setup.validate_values("link", {"url": "https://user:pw@example.com/a.csv"})


def test_a_file_path_must_be_a_full_path() -> None:
    with pytest.raises(ValueError) as caught:
        setup.validate_values("file", {"path": "exports/a.csv"})
    assert caught.value.args[0] == "File must be a full path such as C:\\Exports\\a.csv."


def test_a_full_file_path_passes(tmp_path: Path) -> None:
    values = setup.validate_values("file", {"path": f"  {tmp_path / 'a.csv'}  "})
    assert values["path"] == str(tmp_path / "a.csv")


def test_a_header_sign_in_needs_a_valid_header_name() -> None:
    with pytest.raises(ValueError, match="Fill in Header name."):
        setup.validate_values("link", {"url": LINK_URL, "auth": "header"})
    with pytest.raises(ValueError, match="must be a header name"):
        setup.validate_values("link", {"url": LINK_URL, "auth": "header", "header_name": "X API"})


def test_a_basic_sign_in_needs_a_username() -> None:
    with pytest.raises(ValueError, match="Fill in Username."):
        setup.validate_values("link", {"url": LINK_URL, "auth": "basic"})


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError):
        setup.validate_values("nonsense", {})


# start_draft and the draft --------------------------------------------------------------------


def test_the_test_fetch_reads_the_source_once_and_keeps_a_draft(
    store: LocalStore, served: Served
) -> None:
    draft = start(store)
    assert len(served.requests) == 1
    assert "Company" in draft.table.headers
    assert draft.provider == "link"
    assert draft.role is FeedRole.ACCOUNTS
    assert draft.has_secret is False
    assert setup.load_draft(store, draft.id, now=NOW) is not None


def test_the_typed_key_is_saved_only_once_the_fetch_has_worked(
    store: LocalStore, served: Served
) -> None:
    served.status = 500
    with pytest.raises(FeedError):
        start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    assert not feeds_folder().exists() or list(feeds_folder().iterdir()) == []

    served.status = 200
    draft = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    assert draft.has_secret is True
    assert vault.get_secret(f"draft-{draft.id}") == SECRET
    assert served.requests[-1].headers["authorization"] == f"Bearer {SECRET}"


def test_a_key_the_provider_needs_must_be_given(store: LocalStore, served: Served) -> None:
    with pytest.raises(ValueError, match="Fill in Key, token or password."):
        start(store, values={"url": LINK_URL, "auth": "bearer"})
    assert served.requests == []


def test_a_schedule_or_role_the_provider_lacks_is_refused_before_any_fetch(
    store: LocalStore, served: Served
) -> None:
    with pytest.raises(ValueError, match="Pick one of the sync schedules offered."):
        start(
            store,
            provider="google_sheets",
            values={"url": "https://docs.google.com/spreadsheets/d/abc/edit"},
            schedule=Schedule.ON_CHANGE,
        )
    with pytest.raises(ValueError, match="can't be used for that"):
        start(store, provider="stripe", values={}, secret=SECRET, role=FeedRole.ACCOUNTS)
    assert served.requests == []


def test_changing_a_feed_uses_its_saved_key_when_none_is_typed(
    store: LocalStore, served: Served
) -> None:
    existing = store.add_connection(ConnectionKind.FEED, "Renewals")
    store.update_connection(
        existing.model_copy(
            update={
                "feed": setup.FeedSettings(
                    provider="link",
                    role=FeedRole.ACCOUNTS,
                    settings={"url": LINK_URL, "auth": "bearer"},
                    schedule=Schedule.DAILY,
                )
            }
        )
    )
    vault.save_secret(existing.id, SECRET)

    draft = start(
        store, values={"url": LINK_URL, "auth": "bearer"}, connection_id=existing.id, secret=None
    )

    assert draft.connection_id == existing.id
    assert draft.has_secret is False
    assert served.requests[-1].headers["authorization"] == f"Bearer {SECRET}"


def test_a_feed_that_was_removed_cannot_be_changed(store: LocalStore, served: Served) -> None:
    with pytest.raises(ValueError, match="was removed"):
        start(store, connection_id="feed-9")


# load_draft, expiry ----------------------------------------------------------------------------


@pytest.mark.parametrize("draft_id", ["../secret", "ABC", "0" * 15, "f" * 16 + "x", ""])
def test_a_malformed_draft_id_reads_as_none(store: LocalStore, draft_id: str) -> None:
    assert setup.load_draft(store, draft_id, now=NOW) is None


def test_a_draft_that_is_not_there_reads_as_none(store: LocalStore) -> None:
    assert setup.load_draft(store, "f" * 16, now=NOW) is None


def test_an_expired_draft_is_deleted_with_its_key(store: LocalStore, served: Served) -> None:
    draft = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    later = NOW + timedelta(hours=25)
    assert setup.load_draft(store, draft.id, now=later) is None
    assert not (store.root / setup.DRAFTS_DIR / f"{draft.id}.json").exists()
    assert vault.get_secret(f"draft-{draft.id}") is None


# Matching ------------------------------------------------------------------------------------


def test_with_no_choice_saved_the_columns_are_matched_automatically(
    store: LocalStore, served: Served
) -> None:
    draft = start(store)
    mapping = setup.suggested_mapping(draft)
    assert mapping["name"] == "Company"
    assert mapping["arr"] == "ARR"
    assert mapping["renewal_date"] == "Renewal date"


def test_the_csm_choice_wins_and_headers_that_are_gone_are_dropped(
    store: LocalStore, served: Served
) -> None:
    draft = start(store)
    draft.mapping = {"name": "Company", "arr": "Gone"}
    assert setup.suggested_mapping(draft) == {"name": "Company"}


def test_a_signals_draft_keeps_only_its_key_and_signal_columns(
    store: LocalStore, served: Served
) -> None:
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.NAME)
    mapping = setup.suggested_mapping(draft)
    assert mapping == {"name": "Company", "signal:usage_trend_30d": "Usage trend"}


# Preview ----------------------------------------------------------------------------------------


def test_an_accounts_preview_names_the_columns_still_to_match(
    store: LocalStore, served: Served
) -> None:
    draft = start(store)
    result = setup.preview(
        draft, {"name": "Company"}, None, book_accounts=no_book, now=NOW, today=TODAY
    )
    assert result.blocker == "Match ARR and Renewal date to import."
    assert result.ready == 0


def test_an_accounts_preview_counts_the_rows_ready_to_import(
    store: LocalStore, served: Served
) -> None:
    draft = start(store)
    result = setup.preview(
        draft, setup.suggested_mapping(draft), "dmy", book_accounts=no_book, now=NOW, today=TODAY
    )
    assert result.blocker is None
    assert result.rows == 2
    assert result.ready == 2
    assert result.accounts == 2
    assert result.issues == []


def test_rows_that_are_all_unusable_are_a_blocker(store: LocalStore, served: Served) -> None:
    served.body = "Company,ARR,Renewal date\nAcme,,\nBeta,,\n"
    draft = start(store)
    result = setup.preview(
        draft, setup.suggested_mapping(draft), "dmy", book_accounts=no_book, now=NOW, today=TODAY
    )
    assert result.blocker is not None
    assert result.blocker.startswith("No accounts are ready to save.")


def test_the_choices_are_kept_on_the_draft(store: LocalStore, served: Served) -> None:
    draft = start(store)
    mapping = setup.suggested_mapping(draft)
    setup.preview(draft, mapping, "mdy", book_accounts=no_book, now=NOW, today=TODAY)
    assert draft.mapping == mapping
    assert draft.date_order == "mdy"


def test_a_signals_preview_needs_a_signal_column(store: LocalStore, served: Served) -> None:
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.NAME)
    result = setup.preview(
        draft, {"name": "Company"}, None, book_accounts=no_book, now=NOW, today=TODAY
    )
    assert result.blocker == "Match at least one signal column, such as Usage trend."


def test_a_signals_preview_needs_the_column_for_its_match_method(
    store: LocalStore, served: Served
) -> None:
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.ID)
    result = setup.preview(
        draft,
        {"name": "Company", "signal:usage_trend_30d": "Usage trend"},
        None,
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )
    assert result.blocker == "Match a column to ID."


def test_a_signals_preview_counts_matches_and_lists_the_rest(
    store: LocalStore, served: Served
) -> None:
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.NAME)
    mapping = {"name": "Company", "signal:usage_trend_30d": "Usage trend"}
    result = setup.preview(
        draft,
        mapping,
        None,
        book_accounts=lambda connection_id: [acct("hs-1", "Acme")],
        now=NOW,
        today=TODAY,
    )
    assert result.blocker is None
    assert result.ready == 1
    assert result.accounts == 1
    assert [item.key for item in result.unmatched] == ["Beta"]


# Save ------------------------------------------------------------------------------------------


def test_saving_a_new_feed_moves_its_key_and_runs_the_first_sync(
    store: LocalStore, served: Served
) -> None:
    draft = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    config = setup.commit_draft(
        store,
        draft,
        setup.suggested_mapping(draft),
        "dmy",
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )

    assert config.id == "feed-1"
    assert config.kind is ConnectionKind.FEED
    assert config.enabled is True
    assert config.feed is not None
    assert config.feed.provider == "link"
    assert config.feed.role is FeedRole.ACCOUNTS
    assert config.feed.settings["url"] == LINK_URL
    assert config.feed.status.last_synced_at == NOW
    assert config.feed.status.accounts == 2
    assert vault.get_secret("feed-1") == SECRET
    assert vault.get_secret(f"draft-{draft.id}") is None
    assert setup.load_draft(store, draft.id, now=NOW) is None
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    assert [account.id for account in snapshot.accounts] == ["fd1-acme", "fd1-beta"]
    assert len(served.requests) == 1  # the first sync used the table the test fetch read


def test_a_blocked_save_saves_nothing(store: LocalStore, served: Served) -> None:
    draft = start(store)
    with pytest.raises(ValueError, match="Match ARR and Renewal date to import."):
        setup.commit_draft(
            store,
            draft,
            {"name": "Company"},
            "dmy",
            book_accounts=no_book,
            now=NOW,
            today=TODAY,
        )
    assert store.connections() == []
    assert setup.load_draft(store, draft.id, now=NOW) is not None


def test_saving_a_changed_feed_keeps_its_saved_key_when_none_is_typed(
    store: LocalStore, served: Served
) -> None:
    first = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    setup.commit_draft(
        store,
        first,
        setup.suggested_mapping(first),
        "dmy",
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )

    again = start(
        store,
        values={"url": LINK_URL, "auth": "bearer"},
        connection_id="feed-1",
        name="Renewals v2",
        now=NOW + timedelta(days=1),
    )
    config = setup.commit_draft(
        store,
        again,
        setup.suggested_mapping(again),
        "dmy",
        book_accounts=no_book,
        now=NOW + timedelta(days=1),
        today=TODAY,
    )

    assert config.id == "feed-1"
    assert config.name == "Renewals v2"
    assert vault.get_secret("feed-1") == SECRET
    assert served.requests[-1].headers["authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    "new_url",
    [
        "https://elsewhere.example.net/accounts.csv",  # another host
        "https://exports.example.com:8443/accounts.csv",  # same host, another port
    ],
)
def test_a_saved_key_is_never_sent_to_a_changed_address(
    store: LocalStore, served: Served, new_url: str
) -> None:
    first = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    setup.commit_draft(
        store,
        first,
        setup.suggested_mapping(first),
        "dmy",
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )
    before = len(served.requests)

    with pytest.raises(ValueError, match="type the key again"):
        start(
            store,
            values={"url": new_url, "auth": "bearer"},
            connection_id="feed-1",
            now=NOW + timedelta(days=1),
        )

    assert len(served.requests) == before  # nothing was sent anywhere
    assert vault.get_secret("feed-1") == SECRET


def test_a_changed_address_works_when_the_key_is_typed_again(
    store: LocalStore, served: Served
) -> None:
    first = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    setup.commit_draft(
        store,
        first,
        setup.suggested_mapping(first),
        "dmy",
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )
    other = "test-secret-other-9876543210"
    start(
        store,
        values={"url": "https://elsewhere.example.net/accounts.csv", "auth": "bearer"},
        secret=other,
        connection_id="feed-1",
        now=NOW + timedelta(days=1),
    )
    assert served.requests[-1].headers["authorization"] == f"Bearer {other}"


def test_changing_a_feed_to_another_provider_is_refused(store: LocalStore, served: Served) -> None:
    first = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    setup.commit_draft(
        store,
        first,
        setup.suggested_mapping(first),
        "dmy",
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )
    before = len(served.requests)
    with pytest.raises(ValueError, match="another source"):
        start(
            store,
            provider="json_api",
            values={"url": LINK_URL, "auth": "bearer"},
            connection_id="feed-1",
        )
    assert len(served.requests) == before


def test_cancelling_a_draft_deletes_it_and_its_key(store: LocalStore, served: Served) -> None:
    draft = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    setup.cancel_draft(store, draft.id)
    assert setup.load_draft(store, draft.id, now=NOW) is None
    assert vault.get_secret(f"draft-{draft.id}") is None


def test_removing_a_feed_deletes_its_saved_key(store: LocalStore, served: Served) -> None:
    draft = start(store, values={"url": LINK_URL, "auth": "bearer"}, secret=SECRET)
    setup.commit_draft(
        store,
        draft,
        setup.suggested_mapping(draft),
        "dmy",
        book_accounts=no_book,
        now=NOW,
        today=TODAY,
    )
    setup.remove_feed_secret("feed-1")
    assert vault.get_secret("feed-1") is None


def test_saving_a_signals_feed_attaches_its_readings_to_the_book(
    store: LocalStore, served: Served
) -> None:
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.NAME)
    mapping = {"name": "Company", "signal:usage_trend_30d": "Usage trend"}
    config = setup.commit_draft(
        store,
        draft,
        mapping,
        "dmy",
        book_accounts=lambda connection_id: [acct("hs-1", "Acme")],
        now=NOW,
        today=TODAY,
    )
    assert config.feed is not None
    assert config.feed.status.accounts == 1
    assert config.feed.status.skipped_rows == 1
    snapshot = store.snapshot("feed-1")
    assert snapshot is not None
    assert [(signal.account_id, signal.kind) for signal in snapshot.signals] == [
        ("hs-1", SignalKind.USAGE_TREND_30D)
    ]


def test_a_signals_preview_refuses_a_source_where_no_row_matches(
    store: LocalStore, served: Served
) -> None:
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.NAME)
    mapping = {"name": "Company", "signal:usage_trend_30d": "Usage trend"}
    result = setup.preview(draft, mapping, None, book_accounts=no_book, now=NOW, today=TODAY)
    assert result.blocker == (
        "None of the 2 rows matched an account in your book. Check Match accounts by."
    )
    with pytest.raises(ValueError, match="None of the 2 rows matched"):
        setup.commit_draft(store, draft, mapping, None, book_accounts=no_book, now=NOW, today=TODAY)
    assert store.connections() == []


def test_a_signals_preview_refuses_a_source_with_no_rows(store: LocalStore, served: Served) -> None:
    served.body = "Company,ARR,Renewal date,Owner,Usage trend\n"
    draft = start(store, role=FeedRole.SIGNALS, match_by=MatchBy.NAME)
    mapping = {"name": "Company", "signal:usage_trend_30d": "Usage trend"}
    result = setup.preview(draft, mapping, None, book_accounts=no_book, now=NOW, today=TODAY)
    assert result.blocker == "The source sent no rows."
