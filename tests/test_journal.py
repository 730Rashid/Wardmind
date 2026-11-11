"""The journal (core/journal.py): the validation messages and limits; notes, follow-ups, snoozes and
outcomes; the reading helpers; the Today page; the journal file (demo and Your data, corrupt files,
atomic writes, changes that write nothing); moving and forgetting accounts; and a two-thread smoke
test of the write lock.

The store is in a temp folder (see the store fixture). No network.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from wardmind.core import journal as journal_module
from wardmind.core.journal import (
    ATTENTION_LIMIT,
    MAX_FOLLOW_UP,
    MAX_FOLLOW_UP_DAYS,
    MAX_NOTE,
    MAX_NOTES_PER_ACCOUNT,
    MAX_OPEN_FOLLOW_UPS,
    MAX_OUTCOME_NOTE,
    MAX_SNOOZE_DAYS,
    OUTCOME_MATCH_DAYS,
    OUTCOME_WINDOW_DAYS,
    QUIET_DAYS,
    UPCOMING_DAYS,
    AccountJournal,
    FollowUp,
    FollowUpStatus,
    Journal,
    JournalData,
    Note,
    Outcome,
    OutcomeInput,
    OutcomeKind,
    Snooze,
    TodayView,
    TouchKind,
    active_snooze,
    awaiting_outcome,
    check_due,
    check_happened_on,
    check_snooze_until,
    clean_follow_up_text,
    clean_note_text,
    clean_outcome_note,
    last_contact,
    last_touch,
    notes_newest_first,
    open_follow_ups,
    recently_done,
    today_view,
)
from wardmind.core.models import Account, Health, Milestones, Motion, ScoredAccount, SignalKind
from wardmind.core.store import DEMO_JOURNAL_FILE, JOURNAL_FILE, LocalStore

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 9, 0)
LATER = datetime(2026, 11, 9, 10, 0)
ACC = "acc-1"
RENEWAL = date(2026, 11, 1)  # the renewal the outcome tests answer


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


def refused(message: str):
    """A ValueError whose text is exactly this plain message."""
    return pytest.raises(ValueError, match=re.escape(message))


# Fixtures and builders -----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "data")


@pytest.fixture
def journal(store: LocalStore) -> Journal:
    return Journal(store)


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every path the journal writes to, in order (the write still happens)."""
    seen: list[Path] = []
    real = journal_module.vault.write_text

    def spy(path: Path, text: str) -> None:
        seen.append(path)
        real(path, text)

    monkeypatch.setattr(journal_module.vault, "write_text", spy)
    return seen


def make_account(account_id: str = ACC, *, arr: float = 100_000.0, renewal=date(2027, 3, 1)):
    return Account(
        id=account_id,
        name=f"Account {account_id}",
        arr=arr,
        tier="Growth",
        owner="Sam Rivera",
        renewal_date=renewal,
        milestones=Milestones(done=3, total=3, overdue=0),
    )


def make_scored(
    account_id: str = ACC,
    *,
    health: Health = Health.HEALTHY,
    arr: float = 100_000.0,
    renewal: date = date(2027, 3, 1),
    risk: float = 20.0,
) -> ScoredAccount:
    return ScoredAccount(
        account=make_account(account_id, arr=arr, renewal=renewal),
        motion=Motion.RENEWAL,
        health=health,
        risk=risk,
        priority=risk,
        reasons=[],
    )


def note_at(
    day: date,
    *,
    note_id: str = "n-00000001",
    kind: TouchKind = TouchKind.CALL,
    created: datetime = NOW,
    text: str = "A note",
) -> Note:
    return Note(id=note_id, kind=kind, text=text, happened_on=day, created_at=created)


def follow_up_at(
    due: date,
    *,
    item_id: str = "f-00000001",
    status: FollowUpStatus = FollowUpStatus.OPEN,
    done_at: datetime | None = None,
    text: str = "Follow up",
) -> FollowUp:
    return FollowUp(id=item_id, text=text, due=due, status=status, created_at=NOW, done_at=done_at)


def outcome_at(renewal: date, *, outcome_id: str = "o-00000001") -> Outcome:
    return Outcome(
        id=outcome_id,
        kind=OutcomeKind.RENEWED,
        renewal_date=renewal,
        decided_on=renewal,
        arr_before=100_000.0,
        risk=20.0,
        priority=20.0,
        health=Health.HEALTHY,
        motion=Motion.RENEWAL,
        recorded_at=NOW,
    )


def snooze_at(until: date, health: Health = Health.WATCH) -> Snooze:
    return Snooze(until=until, health=health, created_at=NOW)


def journal_of(
    *,
    notes: list[Note] | tuple = (),
    follow_ups: list[FollowUp] | tuple = (),
    snooze: Snooze | None = None,
    outcomes: list[Outcome] | tuple = (),
) -> AccountJournal:
    return AccountJournal(
        notes=list(notes), follow_ups=list(follow_ups), snooze=snooze, outcomes=list(outcomes)
    )


def view_of(book: list[ScoredAccount], journals: dict[str, AccountJournal] | None = None):
    return today_view(book, journals or {}, TODAY)


def save_raw(store: LocalStore, data: JournalData, name: str = JOURNAL_FILE) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / name).write_text(data.model_dump_json(), encoding="utf-8")


def saved_file(store: LocalStore, name: str = JOURNAL_FILE) -> dict:
    return json.loads((store.root / name).read_text(encoding="utf-8"))


def call(
    journal: Journal,
    account: str = ACC,
    *,
    text: str = "Spoke about renewal",
    day: date = TODAY,
    kind: TouchKind = TouchKind.CALL,
    now: datetime = NOW,
) -> Note:
    return journal.add_note(account, kind, text, day, now=now, today=TODAY)


def follow_up(
    journal: Journal, account: str = ACC, *, text: str = "Send the deck", due: date | None = None
) -> FollowUp:
    return journal.add_follow_up(account, text, due or ahead(2), now=NOW, today=TODAY)


def record(
    journal: Journal,
    scored: ScoredAccount | None = None,
    *,
    kind: OutcomeKind = OutcomeKind.RENEWED,
    renewal: date = RENEWAL,
    arr_after: float | None = None,
    note: str = "",
    latest: dict[SignalKind, float] | None = None,
) -> Outcome:
    scored = make_scored() if scored is None else scored
    values = OutcomeInput(
        kind=kind,
        renewal_date=renewal,
        decided_on=TODAY,
        arr_after=arr_after,
        note=note,
    )
    return journal.record_outcome(
        scored.account.id, values, scored, latest or {}, now=NOW, today=TODAY
    )


# Notes: validation -----------------------------------------------------------------------------


def test_a_note_is_stripped_and_line_endings_become_line_feeds():
    assert clean_note_text("  Hello\r\nthere\r  \n") == "Hello\nthere"
    assert clean_note_text("a\rb") == "a\nb"


@pytest.mark.parametrize("text", ["", "   ", "\n\t \r\n"])
def test_an_empty_note_is_refused(text):
    with refused("Type the note."):
        clean_note_text(text)


def test_a_note_may_be_exactly_the_limit_but_not_one_character_over():
    assert clean_note_text("x" * MAX_NOTE) == "x" * MAX_NOTE
    with refused("Keep the note under 4,000 characters."):
        clean_note_text("x" * (MAX_NOTE + 1))


def test_the_note_limit_is_counted_after_stripping():
    assert clean_note_text("  " + "x" * MAX_NOTE + "\n\n") == "x" * MAX_NOTE


def test_tabs_and_line_breaks_are_kept_in_a_note():
    assert clean_note_text("Name\tRole\nLine two") == "Name\tRole\nLine two"


@pytest.mark.parametrize("text", ["a\x07b", "a\x00b", "a\x1bb", "a\x0bb", "a\x85b"])
def test_control_characters_are_refused_in_a_note(text):
    with refused("A note can't contain control characters."):
        clean_note_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "a\N{ZERO WIDTH SPACE}b",
        "\N{ZERO WIDTH SPACE}start",
        "a\N{RIGHT-TO-LEFT OVERRIDE}b",
        "a\N{LEFT-TO-RIGHT ISOLATE}b",
        "a\N{ZERO WIDTH NO-BREAK SPACE}b",
    ],
)
def test_hidden_formatting_characters_are_refused_in_a_note(text):
    with refused("A note can't contain hidden formatting characters."):
        clean_note_text(text)


# Follow-ups and outcome notes: validation ------------------------------------------------------


def test_a_follow_up_is_one_stripped_line_and_keeps_tabs():
    assert clean_follow_up_text("  Send the deck  ") == "Send the deck"
    assert clean_follow_up_text("Name\tRole") == "Name\tRole"


@pytest.mark.parametrize("text", ["", "   "])
def test_an_empty_follow_up_is_refused(text):
    with refused("Say what to follow up on."):
        clean_follow_up_text(text)


def test_a_follow_up_may_be_exactly_200_characters_but_not_201():
    assert clean_follow_up_text("x" * MAX_FOLLOW_UP) == "x" * MAX_FOLLOW_UP
    with refused("Keep it under 200 characters."):
        clean_follow_up_text("x" * (MAX_FOLLOW_UP + 1))


@pytest.mark.parametrize("text", ["a\nb", "a\rb", "a\r\nb", "a\N{LINE SEPARATOR}b"])
def test_a_follow_up_cannot_hold_a_line_break(text):
    with refused("A follow-up can't contain a line break."):
        clean_follow_up_text(text)


def test_a_follow_up_refuses_control_and_format_characters_in_its_own_wording():
    with refused("A follow-up can't contain control characters."):
        clean_follow_up_text("a\x07b")
    with refused("A follow-up can't contain hidden formatting characters."):
        clean_follow_up_text("a\N{ZERO WIDTH SPACE}b")


def test_an_outcome_note_may_be_blank_and_is_cleaned():
    assert clean_outcome_note("   ") == ""
    assert clean_outcome_note("  Happy\r\nwith it  ") == "Happy\nwith it"


def test_an_outcome_note_is_limited_to_1000_characters():
    assert clean_outcome_note("x" * MAX_OUTCOME_NOTE) == "x" * MAX_OUTCOME_NOTE
    with refused("Keep the note under 1,000 characters."):
        clean_outcome_note("x" * (MAX_OUTCOME_NOTE + 1))


@pytest.mark.parametrize(
    ("text", "message"),
    [("a\x07b", "control characters"), ("a\N{ZERO WIDTH SPACE}b", "hidden formatting characters")],
)
def test_an_outcome_note_refuses_control_and_format_characters(text, message):
    with pytest.raises(ValueError, match=message):
        clean_outcome_note(text)


# Dates -----------------------------------------------------------------------------------------


def test_a_happened_on_day_may_be_today_or_earlier_but_not_later():
    assert check_happened_on(TODAY, TODAY) == TODAY
    assert check_happened_on(ago(400), TODAY) == ago(400)
    with refused("That date is in the future."):
        check_happened_on(ahead(1), TODAY)


def test_a_follow_up_is_due_today_or_later_and_within_a_year():
    assert check_due(TODAY, TODAY) == TODAY
    assert check_due(ahead(MAX_FOLLOW_UP_DAYS), TODAY) == ahead(MAX_FOLLOW_UP_DAYS)
    with refused("Pick today or a later date."):
        check_due(ago(1), TODAY)
    with refused("Pick a date within a year."):
        check_due(ahead(MAX_FOLLOW_UP_DAYS + 1), TODAY)


def test_a_snooze_ends_after_today_and_within_ninety_days():
    assert check_snooze_until(ahead(1), TODAY) == ahead(1)
    assert check_snooze_until(ahead(MAX_SNOOZE_DAYS), TODAY) == ahead(MAX_SNOOZE_DAYS)
    with refused("Pick a date after today."):
        check_snooze_until(TODAY, TODAY)
    with refused("Snooze for 90 days at most."):
        check_snooze_until(ahead(MAX_SNOOZE_DAYS + 1), TODAY)


# Reading one account's journal -----------------------------------------------------------------


def test_last_contact_ignores_plain_notes():
    assert last_contact(journal_of(notes=[note_at(ago(1), kind=TouchKind.NOTE)])) is None
    assert last_contact(AccountJournal()) is None


def test_last_contact_is_the_latest_call_meeting_or_email_day():
    journal_obj = journal_of(
        notes=[
            note_at(ago(9), note_id="n-00000001", kind=TouchKind.CALL),
            note_at(ago(3), note_id="n-00000002", kind=TouchKind.EMAIL),
            note_at(ago(1), note_id="n-00000003", kind=TouchKind.NOTE),
        ]
    )
    assert last_contact(journal_obj) == ago(3)


def test_last_touch_is_the_latest_contact_and_a_tie_goes_to_the_latest_added():
    earlier = note_at(ago(2), note_id="n-00000001", kind=TouchKind.CALL, created=NOW)
    added_later = note_at(ago(2), note_id="n-00000002", kind=TouchKind.MEETING, created=LATER)
    plain = note_at(TODAY, note_id="n-00000003", kind=TouchKind.NOTE)

    assert last_touch(journal_of(notes=[earlier, added_later, plain])) == added_later
    assert last_touch(journal_of(notes=[plain])) is None


def test_notes_newest_first_orders_by_day_then_by_when_added():
    first = note_at(ago(1), note_id="n-00000001", created=NOW)
    old = note_at(ago(5), note_id="n-00000002", created=LATER)
    same_day_later = note_at(ago(1), note_id="n-00000003", created=LATER)

    found = notes_newest_first(journal_of(notes=[first, old, same_day_later]))

    assert [note.id for note in found] == ["n-00000003", "n-00000001", "n-00000002"]


def test_open_follow_ups_are_ordered_by_due_then_when_added_and_done_ones_are_left_out():
    late = FollowUp(id="f-00000001", text="Late", due=ahead(5), created_at=NOW)
    added_second = FollowUp(id="f-00000002", text="Second", due=ahead(1), created_at=LATER)
    added_first = FollowUp(id="f-00000003", text="First", due=ahead(1), created_at=NOW)
    done = follow_up_at(ago(30), item_id="f-00000004", status=FollowUpStatus.DONE, done_at=NOW)

    found = open_follow_ups(journal_of(follow_ups=[late, added_second, added_first, done]))

    assert [item.id for item in found] == ["f-00000003", "f-00000002", "f-00000001"]


def done_follow_up(days_ago: int, item_id: str) -> FollowUp:
    return follow_up_at(
        TODAY,
        item_id=item_id,
        status=FollowUpStatus.DONE,
        done_at=NOW - timedelta(days=days_ago),
    )


def test_recently_done_covers_fourteen_days_and_lists_the_newest_first():
    journal_obj = journal_of(
        follow_ups=[
            done_follow_up(14, "f-00000001"),
            done_follow_up(1, "f-00000002"),
            done_follow_up(15, "f-00000003"),
            done_follow_up(3, "f-00000004"),
            follow_up_at(TODAY, item_id="f-00000005"),
        ]
    )

    found = recently_done(journal_obj, NOW)

    assert [item.id for item in found] == ["f-00000002", "f-00000004", "f-00000001"]


def test_recently_done_takes_its_own_window_and_an_aware_now():
    journal_obj = journal_of(
        follow_ups=[done_follow_up(1, "f-00000001"), done_follow_up(4, "f-00000002")]
    )
    found = recently_done(journal_obj, datetime(2026, 11, 9, 9, 0, tzinfo=UTC), days=3)
    assert [item.id for item in found] == ["f-00000001"]


def test_a_snooze_applies_until_its_day_and_ends_on_that_day():
    journal_obj = journal_of(snooze=snooze_at(ahead(2)))

    assert active_snooze(journal_obj, Health.WATCH, ahead(1)) is not None
    assert active_snooze(journal_obj, Health.WATCH, ahead(2)) is None
    assert active_snooze(AccountJournal(), Health.WATCH, TODAY) is None


@pytest.mark.parametrize(
    ("health_now", "applies"),
    [(Health.AT_RISK, False), (Health.WATCH, True), (Health.HEALTHY, True)],
)
def test_a_worse_health_than_when_snoozed_ends_the_snooze_early(health_now, applies):
    journal_obj = journal_of(snooze=snooze_at(ahead(5), Health.WATCH))
    assert (active_snooze(journal_obj, health_now, TODAY) is not None) is applies


def test_a_better_health_than_when_snoozed_keeps_the_snooze():
    journal_obj = journal_of(snooze=snooze_at(ahead(5), Health.AT_RISK))
    assert active_snooze(journal_obj, Health.HEALTHY, TODAY) is not None
    assert active_snooze(journal_obj, Health.WATCH, TODAY) is not None
    assert active_snooze(journal_obj, Health.AT_RISK, TODAY) is not None


@pytest.mark.parametrize(
    ("offset", "awaiting"),
    [
        (1, False),
        (0, False),
        (-1, True),
        (-OUTCOME_WINDOW_DAYS, True),
        (-(OUTCOME_WINDOW_DAYS + 1), False),
    ],
)
def test_a_renewal_that_passed_within_sixty_days_awaits_its_outcome(offset, awaiting):
    renewal = ahead(offset)
    assert awaiting_outcome(make_account(renewal=renewal), AccountJournal(), TODAY) is awaiting


@pytest.mark.parametrize(
    ("offset", "answered"),
    [
        (0, True),
        (-OUTCOME_MATCH_DAYS, True),
        (OUTCOME_MATCH_DAYS, True),
        (OUTCOME_MATCH_DAYS + 1, False),
        (-(OUTCOME_MATCH_DAYS + 1), False),
    ],
)
def test_an_outcome_within_forty_five_days_of_the_renewal_answers_it(offset, answered):
    renewal = ago(10)
    journal_obj = journal_of(outcomes=[outcome_at(renewal + timedelta(days=offset))])
    assert awaiting_outcome(make_account(renewal=renewal), journal_obj, TODAY) is not answered


# The Today page -------------------------------------------------------------------------------


def test_an_empty_book_gives_an_empty_today_page():
    view = today_view([], {}, TODAY)

    assert view.due == view.attention == view.outcomes == view.upcoming == view.snoozed == []
    assert view.contacted_this_week == 0


def test_due_lists_follow_ups_due_today_or_earlier_most_days_late_first_then_rank():
    book = [make_scored("acc-1"), make_scored("acc-2"), make_scored("acc-3")]
    journals = {
        "acc-1": journal_of(follow_ups=[follow_up_at(TODAY)]),
        "acc-2": journal_of(follow_ups=[follow_up_at(ago(5))]),
        "acc-3": journal_of(follow_ups=[follow_up_at(ago(5))]),
    }

    view = view_of(book, journals)

    assert [(item.scored.account.id, item.days_late) for item in view.due] == [
        ("acc-2", 5),
        ("acc-3", 5),
        ("acc-1", 0),
    ]


def test_upcoming_is_the_next_seven_days_by_due_date_then_rank():
    book = [make_scored(f"acc-{number}") for number in range(1, 6)]
    journals = {
        "acc-1": journal_of(follow_ups=[follow_up_at(ahead(1))]),
        "acc-2": journal_of(follow_ups=[follow_up_at(ahead(7))]),
        "acc-3": journal_of(follow_ups=[follow_up_at(ahead(UPCOMING_DAYS + 1))]),
        "acc-4": journal_of(follow_ups=[follow_up_at(ahead(1))]),
        "acc-5": journal_of(follow_ups=[follow_up_at(TODAY)]),
    }

    view = view_of(book, journals)

    assert [item.scored.account.id for item in view.upcoming] == ["acc-1", "acc-4", "acc-2"]
    assert [item.days_late for item in view.upcoming] == [-1, -1, -7]
    assert [item.scored.account.id for item in view.due] == ["acc-5"]


def test_a_done_follow_up_is_not_listed_on_today():
    journals = {
        ACC: journal_of(follow_ups=[follow_up_at(TODAY, status=FollowUpStatus.DONE, done_at=NOW)])
    }
    view = view_of([make_scored()], journals)
    assert view.due == []
    assert view.upcoming == []


def test_attention_lists_at_risk_and_watch_accounts_in_book_order_with_their_rank():
    book = [
        make_scored("acc-1", health=Health.AT_RISK),
        make_scored("acc-2", health=Health.HEALTHY),
        make_scored("acc-3", health=Health.WATCH),
        make_scored("acc-4", health=Health.AT_RISK),
    ]
    journals = {"acc-4": journal_of(notes=[note_at(ago(2))])}

    view = view_of(book, journals)

    assert [(item.scored.account.id, item.rank) for item in view.attention] == [
        ("acc-1", 1),
        ("acc-3", 3),
    ]


def test_an_attention_item_carries_its_last_contact_or_none():
    book = [make_scored("acc-1", health=Health.AT_RISK), make_scored("acc-2", health=Health.WATCH)]
    journals = {"acc-1": journal_of(notes=[note_at(ago(30))])}

    view = view_of(book, journals)

    assert [(item.scored.account.id, item.last_contact) for item in view.attention] == [
        ("acc-1", ago(30)),
        ("acc-2", None),
    ]


@pytest.mark.parametrize(
    ("days_ago", "listed"),
    [(0, False), (QUIET_DAYS - 1, False), (QUIET_DAYS, False), (QUIET_DAYS + 1, True)],
)
def test_an_account_contacted_within_the_quiet_days_is_not_in_attention(days_ago, listed):
    book = [make_scored(health=Health.AT_RISK)]
    journals = {ACC: journal_of(notes=[note_at(ago(days_ago))])}

    assert bool(view_of(book, journals).attention) is listed


def test_a_plain_note_is_not_a_contact_so_it_does_not_quiet_attention():
    book = [make_scored(health=Health.AT_RISK)]
    journals = {ACC: journal_of(notes=[note_at(ago(1), kind=TouchKind.NOTE)])}

    view = view_of(book, journals)

    assert [item.last_contact for item in view.attention] == [None]


def test_an_open_follow_up_keeps_an_account_out_of_attention_but_a_done_one_does_not():
    book = [
        make_scored("acc-1", health=Health.AT_RISK),
        make_scored("acc-2", health=Health.AT_RISK),
    ]
    journals = {
        "acc-1": journal_of(follow_ups=[follow_up_at(ahead(3))]),
        "acc-2": journal_of(follow_ups=[follow_up_at(ahead(3), status=FollowUpStatus.DONE)]),
    }

    view = view_of(book, journals)

    assert [item.scored.account.id for item in view.attention] == ["acc-2"]


def test_attention_holds_at_most_ten_accounts_in_book_order():
    book = [make_scored(f"acc-{number:02d}", health=Health.AT_RISK) for number in range(1, 13)]

    view = view_of(book)

    assert ATTENTION_LIMIT == 10
    assert [item.rank for item in view.attention] == list(range(1, 11))


def test_a_snoozed_account_leaves_attention_but_its_follow_ups_and_outcome_still_show():
    book = [make_scored(health=Health.AT_RISK, renewal=ago(3))]
    journals = {
        ACC: journal_of(
            follow_ups=[
                follow_up_at(TODAY, item_id="f-00000001"),
                follow_up_at(ahead(2), item_id="f-00000002"),
            ],
            snooze=snooze_at(ahead(4), Health.AT_RISK),
        )
    }

    view = view_of(book, journals)

    assert [item.scored.account.id for item in view.snoozed] == [ACC]
    assert [item.follow_up.id for item in view.due] == ["f-00000001"]
    assert [item.follow_up.id for item in view.upcoming] == ["f-00000002"]
    assert view.attention == []
    assert [scored.account.id for scored in view.outcomes] == [ACC]


def test_snoozed_accounts_are_listed_by_the_day_they_return():
    book = [make_scored("acc-1", health=Health.WATCH), make_scored("acc-2", health=Health.WATCH)]
    journals = {
        "acc-1": journal_of(snooze=snooze_at(ahead(10))),
        "acc-2": journal_of(snooze=snooze_at(ahead(3))),
    }

    view = view_of(book, journals)

    assert [item.scored.account.id for item in view.snoozed] == ["acc-2", "acc-1"]
    assert [item.snooze.until for item in view.snoozed] == [ahead(3), ahead(10)]


def test_a_snooze_ended_by_worse_health_brings_the_account_back_to_attention():
    book = [make_scored(health=Health.AT_RISK)]
    journals = {ACC: journal_of(snooze=snooze_at(ahead(5), Health.WATCH))}

    view = view_of(book, journals)

    assert view.snoozed == []
    assert [item.scored.account.id for item in view.attention] == [ACC]


def test_outcomes_lists_the_renewals_awaiting_an_answer_by_renewal_date():
    book = [
        make_scored("acc-1", renewal=ago(10)),
        make_scored("acc-2", renewal=ago(40)),
        make_scored("acc-3", renewal=ago(2)),
        make_scored("acc-4", renewal=ahead(5)),
        make_scored("acc-5", renewal=ago(70)),
    ]

    view = view_of(book)

    assert [scored.account.id for scored in view.outcomes] == ["acc-2", "acc-1", "acc-3"]


def test_a_renewal_already_answered_is_not_awaiting_an_outcome():
    journals = {ACC: journal_of(outcomes=[outcome_at(ago(10))])}
    view = view_of([make_scored(renewal=ago(10))], journals)
    assert view.outcomes == []


def test_contacted_this_week_counts_each_account_once_for_a_contact_in_the_last_seven_days():
    book = [make_scored(f"acc-{letter}") for letter in "abcdef"]
    journals = {
        "acc-a": journal_of(notes=[note_at(TODAY)]),
        "acc-b": journal_of(notes=[note_at(ago(6), kind=TouchKind.MEETING)]),
        "acc-c": journal_of(notes=[note_at(ago(7))]),
        "acc-d": journal_of(notes=[note_at(ahead(1), kind=TouchKind.EMAIL)]),
        "acc-e": journal_of(notes=[note_at(TODAY, kind=TouchKind.NOTE)]),
        "acc-f": journal_of(
            notes=[
                note_at(TODAY, note_id="n-00000001"),
                note_at(ago(3), note_id="n-00000002"),
            ]
        ),
    }

    assert view_of(book, journals).contacted_this_week == 3


def test_journal_entries_for_accounts_not_in_the_book_are_ignored():
    journals = {
        "acc-gone": journal_of(
            notes=[note_at(TODAY)],
            follow_ups=[follow_up_at(ago(3))],
            snooze=snooze_at(ahead(3)),
        )
    }

    view = view_of([make_scored(ACC)], journals)

    assert (
        view.due,
        view.upcoming,
        view.attention,
        view.outcomes,
        view.snoozed,
        view.contacted_this_week,
    ) == ([], [], [], [], [], 0)
    assert isinstance(view, TodayView)


# The journal file -----------------------------------------------------------------------------


def test_a_missing_file_reads_as_empty_and_is_not_created(store, journal):
    assert journal.load() == JournalData()
    assert journal.all() == {}
    assert journal.for_account(ACC) == AccountJournal()
    assert not (store.root / JOURNAL_FILE).exists()


@pytest.mark.parametrize("raw", [b"{ not json", b'{"by_account": [1, 2]}', b"\xff\xfe\x00bad", b""])
def test_a_corrupt_file_reads_as_empty_and_is_set_aside_before_the_next_change(store, journal, raw):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_bytes(raw)

    assert journal.all() == {}
    call(journal, text="Fresh start")

    assert [note.text for note in journal.for_account(ACC).notes] == ["Fresh start"]
    (kept,) = store.root.glob("journal.unreadable-*.json")
    assert kept.read_bytes() == raw


def test_delete_all_removes_set_aside_journal_copies(store, journal):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_bytes(b"{ broken")
    call(journal, text="Fresh start")
    assert list(store.root.glob("journal.unreadable-*.json"))
    store.clear_all()
    assert not list(store.root.glob("journal*"))


def test_demo_and_your_data_keep_separate_files(store):
    demo = Journal(store, demo=True)
    real = Journal(store)

    call(demo, text="Demo note")

    assert real.all() == {}
    assert [note.text for note in demo.for_account(ACC).notes] == ["Demo note"]
    assert (store.root / DEMO_JOURNAL_FILE).exists()
    assert not (store.root / JOURNAL_FILE).exists()


def test_an_account_with_nothing_left_in_it_is_not_written(store, journal):
    note = call(journal)

    assert journal.remove_note(ACC, note.id) is True

    assert saved_file(store) == {"by_account": {}}


def test_changes_that_change_nothing_write_nothing(journal, writes):
    assert journal.remove_note(ACC, "n-00000000") is False
    assert journal.remove_follow_up(ACC, "f-00000000") is False
    assert journal.remove_outcome(ACC, "o-00000000") is False
    assert journal.wake(ACC) is False
    assert journal.forget(lambda account_id: True) == 0
    journal.move_account("acc-none", ACC)
    journal.move_account(ACC, ACC)

    assert writes == []


def test_writes_leave_only_the_journal_file_behind(store, journal):
    call(journal)
    follow_up(journal)
    journal.snooze(ACC, ahead(3), Health.WATCH, now=NOW, today=TODAY)

    assert sorted(path.name for path in store.root.iterdir()) == [JOURNAL_FILE]


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp_file(store, journal, monkeypatch):
    call(journal, text="Saved first")
    before = (store.root / JOURNAL_FILE).read_text(encoding="utf-8")

    def disk_full(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", disk_full)
    with pytest.raises(OSError, match="disk full"):
        call(journal, text="Never saved")
    monkeypatch.undo()

    assert (store.root / JOURNAL_FILE).read_text(encoding="utf-8") == before
    assert sorted(path.name for path in store.root.iterdir()) == [JOURNAL_FILE]


def test_everything_a_journal_holds_survives_a_reload(store, journal):
    call(journal, kind=TouchKind.MEETING)
    follow_up(journal)
    journal.snooze(ACC, ahead(3), Health.WATCH, now=NOW, today=TODAY)
    record(journal, latest={SignalKind.NPS_LATEST: 6.0})

    reloaded = Journal(store).for_account(ACC)

    assert reloaded == journal.for_account(ACC)
    assert reloaded.outcomes[0].signals == {SignalKind.NPS_LATEST: 6.0}


# Notes -----------------------------------------------------------------------------------------


def test_add_note_stores_the_text_stripped_with_an_id_and_the_day(journal):
    note = journal.add_note(
        ACC, TouchKind.EMAIL, "  Sent the pricing \r\n", ago(2), now=NOW, today=TODAY
    )

    assert re.fullmatch(r"n-[0-9a-f]{8}", note.id)
    assert (note.kind, note.text, note.happened_on, note.created_at, note.edited_at) == (
        TouchKind.EMAIL,
        "Sent the pricing",
        ago(2),
        NOW,
        None,
    )
    assert journal.for_account(ACC).notes == [note]


def test_an_aware_time_is_stored_as_naive_utc(journal):
    plus_one = timezone(timedelta(hours=1))
    note = journal.add_note(
        ACC,
        TouchKind.CALL,
        "Call",
        TODAY,
        now=datetime(2026, 11, 9, 10, 0, tzinfo=plus_one),
        today=TODAY,
    )
    assert note.created_at == datetime(2026, 11, 9, 9, 0)


@pytest.mark.parametrize(
    ("text", "day", "message"),
    [
        ("   ", TODAY, "Type the note."),
        ("Fine", ahead(1), "That date is in the future."),
        (
            "bad\N{ZERO WIDTH SPACE}chars",
            TODAY,
            "A note can't contain hidden formatting characters.",
        ),
    ],
)
def test_add_note_refuses_bad_text_or_a_future_day_and_writes_nothing(
    journal, writes, text, day, message
):
    with refused(message):
        journal.add_note(ACC, TouchKind.CALL, text, day, now=NOW, today=TODAY)
    assert writes == []


def test_an_account_keeps_at_most_1000_notes(store, journal):
    full = AccountJournal(
        notes=[note_at(TODAY, note_id=f"n-{number:08x}") for number in range(999)]
    )
    save_raw(store, JournalData(by_account={ACC: full}))

    call(journal, text="The thousandth")

    with refused("This account has 1,000 notes, the most it can keep. Remove some old ones first."):
        call(journal, text="One too many")
    assert len(journal.for_account(ACC).notes) == MAX_NOTES_PER_ACCOUNT


def test_note_ids_are_unique_within_an_account(journal, monkeypatch):
    tokens = iter(["00000001", "00000001", "00000002"])
    monkeypatch.setattr(journal_module.secrets, "token_hex", lambda size: next(tokens))

    first = call(journal, text="First")
    second = call(journal, text="Second")

    assert (first.id, second.id) == ("n-00000001", "n-00000002")


def test_update_note_changes_what_it_says_and_marks_it_edited(journal):
    note = call(journal, text="First", kind=TouchKind.CALL)

    updated = journal.update_note(
        ACC, note.id, TouchKind.MEETING, "  Second  ", ago(1), now=LATER, today=TODAY
    )

    assert (updated.id, updated.kind, updated.text, updated.happened_on) == (
        note.id,
        TouchKind.MEETING,
        "Second",
        ago(1),
    )
    assert (updated.created_at, updated.edited_at) == (NOW, LATER)
    assert journal.for_account(ACC).notes == [updated]


def test_a_call_changed_to_a_plain_note_no_longer_counts_as_contact(journal):
    note = call(journal, day=ago(1))
    assert last_contact(journal.for_account(ACC)) == ago(1)

    journal.update_note(ACC, note.id, TouchKind.NOTE, "Just a note", ago(1), now=LATER, today=TODAY)

    assert last_contact(journal.for_account(ACC)) is None


def test_update_note_names_an_unknown_note_and_checks_the_text(journal):
    note = call(journal)

    with pytest.raises(LookupError, match=re.escape("That note isn't in this account.")):
        journal.update_note(
            ACC, "n-ffffffff", TouchKind.CALL, "Fine", TODAY, now=LATER, today=TODAY
        )
    with refused("Type the note."):
        journal.update_note(ACC, note.id, TouchKind.CALL, " ", TODAY, now=LATER, today=TODAY)

    assert journal.for_account(ACC).notes == [note]


def test_remove_note_returns_true_once_then_false(journal):
    note = call(journal)

    assert journal.remove_note(ACC, note.id) is True
    assert journal.remove_note(ACC, note.id) is False
    assert journal.remove_note("acc-none", note.id) is False


# Follow-ups -----------------------------------------------------------------------------------


def test_add_follow_up_is_open_and_due_on_the_day_asked(journal):
    item = journal.add_follow_up(ACC, "  Send the ROI deck  ", ahead(3), now=NOW, today=TODAY)

    assert re.fullmatch(r"f-[0-9a-f]{8}", item.id)
    assert (item.text, item.due, item.status, item.created_at, item.done_at) == (
        "Send the ROI deck",
        ahead(3),
        FollowUpStatus.OPEN,
        NOW,
        None,
    )
    assert journal.for_account(ACC).follow_ups == [item]


@pytest.mark.parametrize(
    ("text", "due", "message"),
    [
        ("", TODAY, "Say what to follow up on."),
        ("Chase", ago(1), "Pick today or a later date."),
        ("Chase\nHarder", TODAY, "A follow-up can't contain a line break."),
        ("x" * (MAX_FOLLOW_UP + 1), TODAY, "Keep it under 200 characters."),
    ],
)
def test_add_follow_up_refuses_bad_text_or_due_date_and_writes_nothing(
    journal, writes, text, due, message
):
    with refused(message):
        journal.add_follow_up(ACC, text, due, now=NOW, today=TODAY)
    assert writes == []


def test_an_account_holds_twenty_open_follow_ups_and_then_refuses(journal):
    for number in range(MAX_OPEN_FOLLOW_UPS):
        follow_up(journal, text=f"Item {number}")

    with refused("This account has 20 open follow-ups. Finish some first."):
        follow_up(journal, text="One too many")

    assert len(open_follow_ups(journal.for_account(ACC))) == MAX_OPEN_FOLLOW_UPS


def test_done_follow_ups_do_not_count_toward_the_cap(journal):
    first = follow_up(journal, text="First")
    for number in range(MAX_OPEN_FOLLOW_UPS - 1):
        follow_up(journal, text=f"Item {number}")
    journal.complete_follow_up(ACC, first.id, now=NOW)

    follow_up(journal, text="Room again")

    assert len(open_follow_ups(journal.for_account(ACC))) == MAX_OPEN_FOLLOW_UPS


def test_complete_marks_a_follow_up_done_and_completing_it_again_changes_nothing(journal):
    item = follow_up(journal)

    done = journal.complete_follow_up(ACC, item.id, now=LATER)
    assert (done.status, done.done_at) == (FollowUpStatus.DONE, LATER)

    again = journal.complete_follow_up(ACC, item.id, now=datetime(2026, 11, 20, 8, 0))
    assert again == done


def test_reopen_undoes_done_and_keeps_its_due_date_even_when_past(journal):
    old = journal.add_follow_up(ACC, "Old promise", ago(3), now=NOW, today=ago(3))
    journal.complete_follow_up(ACC, old.id, now=LATER)

    reopened = journal.reopen_follow_up(ACC, old.id)

    assert (reopened.status, reopened.done_at, reopened.due) == (
        FollowUpStatus.OPEN,
        None,
        ago(3),
    )


def test_reopening_is_refused_at_the_cap_but_an_open_one_is_returned_as_it_is(journal):
    items = [follow_up(journal, text=f"Item {number}") for number in range(MAX_OPEN_FOLLOW_UPS)]
    journal.complete_follow_up(ACC, items[0].id, now=NOW)
    follow_up(journal, text="Replacement")  # back to 20 open

    with refused("This account has 20 open follow-ups. Finish some first."):
        journal.reopen_follow_up(ACC, items[0].id)
    assert journal.reopen_follow_up(ACC, items[1].id).status is FollowUpStatus.OPEN
    assert journal.for_account(ACC).follow_ups[0].status is FollowUpStatus.DONE


def test_move_changes_the_due_date_of_an_open_follow_up_only(journal):
    item = follow_up(journal)

    moved = journal.move_follow_up(ACC, item.id, ahead(5), today=TODAY)
    assert (moved.id, moved.due) == (item.id, ahead(5))

    journal.complete_follow_up(ACC, item.id, now=LATER)
    with refused("That follow-up is already done."):
        journal.move_follow_up(ACC, item.id, ahead(9), today=TODAY)


def test_move_checks_the_new_date(journal):
    item = follow_up(journal)

    with refused("Pick today or a later date."):
        journal.move_follow_up(ACC, item.id, ago(1), today=TODAY)
    with refused("Pick a date within a year."):
        journal.move_follow_up(ACC, item.id, ahead(MAX_FOLLOW_UP_DAYS + 1), today=TODAY)


def test_unknown_follow_up_ids_are_lookup_errors_and_removing_one_says_false(journal):
    follow_up(journal)
    message = re.escape("That follow-up isn't in this account.")

    with pytest.raises(LookupError, match=message):
        journal.complete_follow_up(ACC, "f-ffffffff", now=NOW)
    with pytest.raises(LookupError, match=message):
        journal.reopen_follow_up(ACC, "f-ffffffff")
    with pytest.raises(LookupError, match=message):
        journal.move_follow_up(ACC, "f-ffffffff", ahead(2), today=TODAY)
    assert journal.remove_follow_up(ACC, "f-ffffffff") is False


def test_remove_follow_up_returns_true_once(journal):
    item = follow_up(journal)

    assert journal.remove_follow_up(ACC, item.id) is True
    assert journal.remove_follow_up(ACC, item.id) is False


# Snoozes --------------------------------------------------------------------------------------


def test_a_new_snooze_replaces_the_old_one_and_wake_removes_it(journal):
    journal.snooze(ACC, ahead(3), Health.WATCH, now=NOW, today=TODAY)

    second = journal.snooze(ACC, ahead(10), Health.AT_RISK, now=LATER, today=TODAY)

    assert journal.for_account(ACC).snooze == second
    assert journal.wake(ACC) is True
    assert journal.for_account(ACC).snooze is None
    assert journal.wake(ACC) is False


@pytest.mark.parametrize(
    ("until", "message"),
    [
        (TODAY, "Pick a date after today."),
        (ahead(MAX_SNOOZE_DAYS + 1), "Snooze for 90 days at most."),
    ],
)
def test_a_snooze_is_checked_and_nothing_is_written(journal, writes, until, message):
    with refused(message):
        journal.snooze(ACC, until, Health.WATCH, now=NOW, today=TODAY)
    assert writes == []


# Outcomes -------------------------------------------------------------------------------------


def test_a_churned_outcome_records_zero_arr_after_whatever_was_typed(journal):
    scored = make_scored(arr=60_000.0)

    churned = record(journal, scored, kind=OutcomeKind.CHURNED, arr_after=50_000.0)
    assert (churned.arr_before, churned.arr_after) == (60_000.0, 0.0)

    blank = record(journal, scored, kind=OutcomeKind.CHURNED, renewal=ago(400))
    assert blank.arr_after == 0.0


def test_an_outcome_keeps_what_wardmind_thought_at_the_time(journal):
    scored = make_scored(health=Health.WATCH, arr=80_000.0, risk=42.5)
    values = OutcomeInput(
        kind=OutcomeKind.EXPANDED,
        renewal_date=RENEWAL,
        decided_on=ago(5),
        arr_after=95_000.0,
        note="  Added two seats.  ",
    )

    outcome = journal.record_outcome(
        ACC,
        values,
        scored,
        {SignalKind.NPS_LATEST: 6.0, SignalKind.USAGE_TREND_30D: -38.0},
        now=NOW,
        today=TODAY,
    )

    assert re.fullmatch(r"o-[0-9a-f]{8}", outcome.id)
    assert (outcome.kind, outcome.renewal_date, outcome.decided_on) == (
        OutcomeKind.EXPANDED,
        RENEWAL,
        ago(5),
    )
    assert (outcome.arr_before, outcome.arr_after, outcome.note) == (
        80_000.0,
        95_000.0,
        "Added two seats.",
    )
    assert (outcome.risk, outcome.priority, outcome.health, outcome.motion) == (
        42.5,
        42.5,
        Health.WATCH,
        Motion.RENEWAL,
    )
    assert outcome.signals == {SignalKind.NPS_LATEST: 6.0, SignalKind.USAGE_TREND_30D: -38.0}
    assert outcome.recorded_at == NOW


def test_the_same_renewal_date_replaces_the_earlier_outcome(journal):
    record(journal, kind=OutcomeKind.RENEWED)
    later = record(journal, kind=OutcomeKind.EXPANDED, arr_after=120_000.0)
    other = record(journal, kind=OutcomeKind.CONTRACTED, renewal=date(2025, 11, 1))

    assert journal.for_account(ACC).outcomes == [later, other]


def test_a_decided_on_date_in_the_future_is_refused(journal, writes):
    values = OutcomeInput(kind=OutcomeKind.RENEWED, renewal_date=RENEWAL, decided_on=ahead(1))

    with refused("That date is in the future."):
        journal.record_outcome(ACC, values, make_scored(), {}, now=NOW, today=TODAY)
    assert writes == []


def test_an_outcome_note_is_cleaned_and_limited(journal):
    assert record(journal, note="  Happy with it.  ").note == "Happy with it."
    with refused("Keep the note under 1,000 characters."):
        record(journal, note="x" * (MAX_OUTCOME_NOTE + 1))


def test_an_outcome_must_be_recorded_for_the_account_it_describes(journal):
    values = OutcomeInput(kind=OutcomeKind.RENEWED, renewal_date=RENEWAL, decided_on=TODAY)

    with pytest.raises(ValueError):
        journal.record_outcome("acc-2", values, make_scored("acc-1"), {}, now=NOW, today=TODAY)


def test_arr_after_may_be_left_blank_but_not_negative(journal):
    assert record(journal, kind=OutcomeKind.RENEWED).arr_after is None
    with pytest.raises(ValueError):
        OutcomeInput(kind=OutcomeKind.RENEWED, renewal_date=RENEWAL, decided_on=TODAY, arr_after=-1)


def test_remove_outcome_returns_true_once(journal):
    outcome = record(journal)

    assert journal.remove_outcome(ACC, outcome.id) is True
    assert journal.remove_outcome(ACC, outcome.id) is False


# Moving and forgetting accounts ----------------------------------------------------------------


def test_moving_an_account_appends_its_entries_to_the_target_and_drops_it(journal):
    call(journal, "acc-b", text="Target call")
    call(journal, "acc-a", text="Source call")
    follow_up(journal, "acc-a", text="Source follow-up")
    record(journal, make_scored("acc-a"))

    journal.move_account("acc-a", "acc-b")

    target = journal.for_account("acc-b")
    assert [note.text for note in target.notes] == ["Target call", "Source call"]
    assert [item.text for item in target.follow_ups] == ["Source follow-up"]
    assert len(target.outcomes) == 1
    assert "acc-a" not in journal.all()


def test_moved_entries_whose_ids_clash_with_the_target_get_new_ids(store, journal):
    save_raw(
        store,
        JournalData(
            by_account={
                "acc-a": AccountJournal(
                    notes=[note_at(ago(1), text="moved")],
                    follow_ups=[follow_up_at(ahead(2), text="moved")],
                    outcomes=[outcome_at(RENEWAL)],
                ),
                "acc-b": AccountJournal(
                    notes=[note_at(ago(4), text="kept")],
                    follow_ups=[follow_up_at(ahead(9), text="kept")],
                    outcomes=[outcome_at(date(2025, 11, 1))],
                ),
            }
        ),
    )

    journal.move_account("acc-a", "acc-b")

    target = journal.for_account("acc-b")
    assert [note.text for note in target.notes] == ["kept", "moved"]
    assert target.notes[0].id == "n-00000001"
    assert re.fullmatch(r"n-[0-9a-f]{8}", target.notes[1].id)
    assert target.notes[1].id != "n-00000001"
    assert [item.text for item in target.follow_ups] == ["kept", "moved"]
    assert target.follow_ups[0].id == "f-00000001"
    assert re.fullmatch(r"f-[0-9a-f]{8}", target.follow_ups[1].id)
    assert target.follow_ups[1].id != "f-00000001"
    assert [outcome.renewal_date for outcome in target.outcomes] == [date(2025, 11, 1), RENEWAL]
    assert target.outcomes[0].id == "o-00000001"
    assert re.fullmatch(r"o-[0-9a-f]{8}", target.outcomes[1].id)
    assert target.outcomes[1].id != "o-00000001"


def test_the_later_snooze_is_kept_when_accounts_merge(journal):
    journal.snooze("acc-a", ahead(10), Health.AT_RISK, now=NOW, today=TODAY)
    journal.snooze("acc-b", ahead(3), Health.WATCH, now=NOW, today=TODAY)

    journal.move_account("acc-a", "acc-b")

    kept = journal.for_account("acc-b").snooze
    assert (kept.until, kept.health) == (ahead(10), Health.AT_RISK)


def test_the_target_keeps_its_snooze_when_it_is_the_later_one(journal):
    journal.snooze("acc-a", ahead(3), Health.AT_RISK, now=NOW, today=TODAY)
    journal.snooze("acc-b", ahead(10), Health.WATCH, now=NOW, today=TODAY)

    journal.move_account("acc-a", "acc-b")

    kept = journal.for_account("acc-b").snooze
    assert (kept.until, kept.health) == (ahead(10), Health.WATCH)


def test_a_snooze_on_only_one_side_is_kept(journal):
    journal.snooze("acc-a", ahead(4), Health.WATCH, now=NOW, today=TODAY)
    journal.move_account("acc-a", "acc-b")
    assert journal.for_account("acc-b").snooze.until == ahead(4)

    journal.snooze("acc-c", ahead(6), Health.WATCH, now=NOW, today=TODAY)
    call(journal, "acc-d")
    journal.move_account("acc-d", "acc-c")
    assert journal.for_account("acc-c").snooze.until == ahead(6)


def test_moving_an_account_onto_itself_or_from_nothing_changes_nothing(journal, writes):
    call(journal, "acc-a")
    writes.clear()

    journal.move_account("acc-a", "acc-a")
    journal.move_account("acc-none", "acc-a")

    assert writes == []
    assert len(journal.for_account("acc-a").notes) == 1


def test_forget_drops_the_journals_it_is_given_and_counts_them(journal):
    call(journal, "acc-a")
    call(journal, "acc-b")

    assert journal.forget(lambda account_id: account_id == "acc-a") == 1
    assert set(journal.all()) == {"acc-b"}
    assert journal.forget(lambda account_id: True) == 1
    assert journal.all() == {}


def test_forget_with_nothing_to_drop_returns_zero_and_writes_nothing(journal, writes):
    call(journal)
    writes.clear()

    assert journal.forget(lambda account_id: False) == 0
    assert writes == []


# Concurrency ----------------------------------------------------------------------------------


def test_two_threads_adding_notes_to_one_account_lose_none(store):
    errors: list[Exception] = []

    def add_many(label: str) -> None:
        try:
            for number in range(15):
                Journal(store).add_note(
                    ACC, TouchKind.CALL, f"{label} {number}", TODAY, now=NOW, today=TODAY
                )
        except Exception as error:  # reported to the test instead of lost in the thread
            errors.append(error)

    threads = [threading.Thread(target=add_many, args=(label,)) for label in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    texts = [note.text for note in Journal(store).for_account(ACC).notes]
    expected = [f"{label} {number}" for label in ("first", "second") for number in range(15)]
    assert sorted(texts) == sorted(expected)
