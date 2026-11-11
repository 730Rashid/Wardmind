"""The journal's Activity history and Notes split (core/journal.py): the ordering helpers,
thread_key and awaiting_replies, the validation messages for subjects, team names and emails,
add_activity, log_email (the mailbox sync's entries), pinning, the account team, and the rules
that keep the newer fields through an update, a move and a load from an older file.

The store is in a temp folder (see the store fixture). No network.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from wardmind.core import journal as journal_module
from wardmind.core.journal import (
    ANSWER_WAIT_DAYS,
    MAX_NOTE,
    MAX_NOTES_PER_ACCOUNT,
    MAX_PEOPLE,
    MAX_PINNED,
    MAX_SUBJECT,
    MAX_TEAM,
    MAX_TEAM_NAME,
    QUIET_THREAD_DAYS,
    REPLY_WAIT_DAYS,
    AccountJournal,
    Direction,
    Journal,
    JournalData,
    LoggedEmail,
    Note,
    NoteSource,
    TeamMember,
    TeamRole,
    ThreadStatus,
    TouchKind,
    activity_newest_first,
    awaiting_replies,
    clean_subject,
    clean_team_email,
    clean_team_name,
    last_contact,
    last_touch,
    notes_newest_first,
    pinned_entries,
    plain_notes_newest_first,
    thread_key,
    today_view,
)
from wardmind.core.models import Account, Health, Milestones, Motion, ScoredAccount
from wardmind.core.store import JOURNAL_FILE, LocalStore

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 9, 0)
LATER = datetime(2026, 11, 9, 10, 0)
ACC = "acc-1"
TOO_MANY_NOTES = "This account has 1,000 notes, the most it can keep. Remove some old ones first."


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


def refused(message: str):
    """A ValueError whose text is exactly this plain message."""
    return pytest.raises(ValueError, match=re.escape(message))


def missing(message: str):
    """A LookupError whose text is exactly this plain message."""
    return pytest.raises(LookupError, match=re.escape(message))


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


def make_account(account_id: str = ACC, *, arr: float = 100_000.0) -> Account:
    return Account(
        id=account_id,
        name=f"Account {account_id}",
        arr=arr,
        tier="Growth",
        owner="Sam Rivera",
        renewal_date=date(2027, 3, 1),
        milestones=Milestones(done=3, total=3, overdue=0),
    )


def make_scored(account_id: str = ACC, *, health: Health = Health.HEALTHY) -> ScoredAccount:
    return ScoredAccount(
        account=make_account(account_id),
        motion=Motion.RENEWAL,
        health=health,
        risk=20.0,
        priority=20.0,
        reasons=[],
    )


def save_raw(store: LocalStore, data: JournalData) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / JOURNAL_FILE).write_text(data.model_dump_json(), encoding="utf-8")


def saved_file(store: LocalStore) -> dict:
    return json.loads((store.root / JOURNAL_FILE).read_text(encoding="utf-8"))


def fill_notes(store: LocalStore, account: str = ACC) -> None:
    """An account holding MAX_NOTES_PER_ACCOUNT plain calls, written straight to the file."""
    notes = [
        Note(
            id=f"n-{index:08x}",
            kind=TouchKind.CALL,
            text="Call",
            happened_on=TODAY,
            created_at=NOW,
        )
        for index in range(MAX_NOTES_PER_ACCOUNT)
    ]
    save_raw(store, JournalData(by_account={account: AccountJournal(notes=notes)}))


def entry(note_id: str, kind: TouchKind, day: date, *, created=NOW, pinned=False) -> Note:
    return Note(
        id=note_id, kind=kind, text="Entry", happened_on=day, created_at=created, pinned=pinned
    )


def mailbox_note(note_id: str, message_id: str, *, thread: str = "renewal plan") -> Note:
    return Note(
        id=note_id,
        kind=TouchKind.EMAIL,
        text="Hello",
        happened_on=TODAY,
        created_at=NOW,
        source=NoteSource.MAILBOX,
        message_id=message_id,
        direction=Direction.SENT,
        thread=thread,
        subject="Renewal plan",
    )


def mail(
    message_id: str = "m-1",
    *,
    subject: str = "Renewal plan",
    sent_on: date = TODAY,
    direction: Direction = Direction.SENT,
    by: str = "",
    people: list[str] | None = None,
    snippet: str = "Hello there",
) -> LoggedEmail:
    return LoggedEmail(
        message_id=message_id,
        subject=subject,
        sent_on=sent_on,
        direction=direction,
        by=by,
        people=["ann@client.com"] if people is None else people,
        snippet=snippet,
    )


def log_mail(journal: Journal, email: LoggedEmail, account: str = ACC, *, now=NOW) -> Note | None:
    return journal.log_email(account, email, now=now)


def add_member(
    journal: Journal,
    name: str = "Ann Lee",
    email: str = "ann@company.com",
    role: TeamRole = TeamRole.CSM,
    account: str = ACC,
) -> TeamMember:
    return journal.add_team_member(account, name, email, role, now=NOW)


def team_member(member_id: str, name: str, email: str | None = None) -> TeamMember:
    return TeamMember(id=member_id, name=name, email=email, role=TeamRole.CSM, created_at=NOW)


# Ordering helpers (pure) ---------------------------------------------------------------------


def test_activity_is_newest_first_by_day_then_when_added_then_id_largest_first():
    call_now = entry("n-00000001", TouchKind.CALL, ago(2), pinned=True)
    plain_later = entry("n-00000002", TouchKind.NOTE, ago(2), created=LATER, pinned=True)
    email_later = entry("n-00000003", TouchKind.EMAIL, ago(2), created=LATER)
    other = entry("n-00000004", TouchKind.OTHER, ago(5))
    old_plain = entry("n-00000005", TouchKind.NOTE, ago(9))
    meeting_same = entry("n-0000000a", TouchKind.MEETING, ago(2))
    book = AccountJournal(
        notes=[call_now, plain_later, email_later, other, old_plain, meeting_same]
    )

    assert activity_newest_first(book) == [email_later, meeting_same, call_now, other]


def test_plain_notes_are_the_note_kind_only_in_the_same_order():
    book = AccountJournal(
        notes=[
            entry("n-00000001", TouchKind.NOTE, ago(9)),
            entry("n-00000002", TouchKind.CALL, ago(2)),
            entry("n-00000003", TouchKind.NOTE, ago(2), created=LATER),
        ]
    )

    assert [note.id for note in plain_notes_newest_first(book)] == ["n-00000003", "n-00000001"]


def test_pinned_entries_of_any_kind_are_listed_newest_first():
    book = AccountJournal(
        notes=[
            entry("n-00000001", TouchKind.CALL, ago(2), pinned=True),
            entry("n-00000002", TouchKind.NOTE, ago(2), created=LATER, pinned=True),
            entry("n-00000003", TouchKind.OTHER, ago(5)),
            entry("n-00000004", TouchKind.EMAIL, ago(1)),
        ]
    )

    assert [note.id for note in pinned_entries(book)] == ["n-00000002", "n-00000001"]
    assert pinned_entries(AccountJournal()) == []


def test_the_full_note_list_still_holds_every_kind_including_other(journal):
    journal.add_activity(ACC, TouchKind.OTHER, "Replied on LinkedIn", ago(1), now=NOW, today=TODAY)
    call_note = journal.add_note(ACC, TouchKind.CALL, "Called", ago(2), now=NOW, today=TODAY)

    found = notes_newest_first(journal.for_account(ACC))

    assert [note.kind for note in found] == [TouchKind.OTHER, TouchKind.CALL]
    assert found[1] == call_note
    assert TouchKind.OTHER.label == "Other"


def test_an_other_touch_is_contact_for_the_last_contact_and_touch_and_the_week_count(journal):
    journal.add_activity(ACC, TouchKind.OTHER, "Replied on LinkedIn", ago(3), now=NOW, today=TODAY)
    book = journal.for_account(ACC)

    assert last_contact(book) == ago(3)
    assert last_touch(book).kind is TouchKind.OTHER
    view = today_view([make_scored(health=Health.AT_RISK)], {ACC: book}, TODAY)
    assert view.contacted_this_week == 1
    assert view.attention == []


# thread_key ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "key"),
    [
        ("RE: Fwd:  Renewal plan", "renewal plan"),
        ("Renewal plan", "renewal plan"),
        ("Fwd: Renewal plan", "renewal plan"),
        ("FW:Renewal plan", "renewal plan"),
        ("fwd:renewal plan", "renewal plan"),
        ("re[2]: Renewal plan", "renewal plan"),
        ("Re[12]:Re: Renewal plan", "renewal plan"),
        ("AW: Vertrag", "vertrag"),
        ("SV: Avtal", "avtal"),
        ("WG: Angebot", "angebot"),
        ("AW: ÄNDERUNG", "änderung"),
        ("RE: RE: RE: Renewal plan", "renewal plan"),
        ("RENEWAL   PLAN", "renewal plan"),
        ("  Renewal \t plan  ", "renewal plan"),
        ("Reply about renewal", "reply about renewal"),
        ("Re renewal", "re renewal"),
        ("Renewal: Re: plan", "renewal: re: plan"),
        ("", ""),
        ("   ", ""),
        ("Re:", ""),
        ("RE: Fwd:", ""),
    ],
)
def test_thread_key_drops_reply_and_forward_prefixes_and_folds_case_and_space(subject, key):
    assert thread_key(subject) == key


# awaiting_replies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "days", "listed"),
    [
        (Direction.SENT, REPLY_WAIT_DAYS - 1, False),
        (Direction.SENT, REPLY_WAIT_DAYS, True),
        (Direction.RECEIVED, ANSWER_WAIT_DAYS - 1, False),
        (Direction.RECEIVED, ANSWER_WAIT_DAYS, True),
        (Direction.SENT, QUIET_THREAD_DAYS, True),
        (Direction.SENT, QUIET_THREAD_DAYS + 1, False),
        (Direction.RECEIVED, QUIET_THREAD_DAYS + 1, False),
    ],
)
def test_the_wait_thresholds_are_inclusive_and_a_quiet_thread_is_dropped(
    journal, direction, days, listed
):
    log_mail(journal, mail(sent_on=ago(days), direction=direction), now=NOW)

    found = awaiting_replies(journal.for_account(ACC), TODAY)

    expected = [direction is Direction.SENT] if listed else []
    assert [status.waiting_on_them for status in found] == expected


def test_a_waiting_thread_reports_its_subject_day_and_days(journal):
    log_mail(journal, mail(subject="Renewal plan", sent_on=ago(7)), now=NOW)

    [status] = awaiting_replies(journal.for_account(ACC), TODAY)

    assert status == ThreadStatus(
        thread="renewal plan",
        subject="Renewal plan",
        last_on=ago(7),
        waiting_on_them=True,
        days=7,
    )


def test_threads_waiting_on_you_come_first_then_oldest_first_then_by_thread(journal):
    entries = [
        ("Gamma", Direction.SENT, 6),
        ("Echo", Direction.SENT, 2),
        ("Beta", Direction.RECEIVED, 3),
        ("Mu", Direction.RECEIVED, 9),
        ("Delta", Direction.SENT, 30),
        ("Zeta", Direction.RECEIVED, 1),
        ("Alpha", Direction.RECEIVED, 9),
    ]
    for index, (subject, direction, days) in enumerate(entries, start=1):
        log_mail(
            journal,
            mail(f"m-{index}", subject=subject, sent_on=ago(days), direction=direction),
            now=NOW,
        )

    found = awaiting_replies(journal.for_account(ACC), TODAY)

    assert [status.thread for status in found] == ["alpha", "mu", "beta", "delta", "gamma"]


def test_replies_and_forwards_of_one_subject_are_one_thread(journal):
    log_mail(journal, mail("m-1", subject="Renewal plan", sent_on=ago(7)), now=NOW)
    log_mail(
        journal,
        mail(
            "m-2",
            subject="RE: Fwd:  renewal   PLAN",
            sent_on=ago(6),
            direction=Direction.RECEIVED,
        ),
        now=NOW,
    )

    [status] = awaiting_replies(journal.for_account(ACC), TODAY)

    assert (status.thread, status.waiting_on_them, status.last_on) == (
        "renewal plan",
        False,
        ago(6),
    )


def test_the_latest_email_is_the_later_day_even_when_it_was_logged_first(journal):
    log_mail(journal, mail("m-2", sent_on=ago(3), direction=Direction.RECEIVED), now=NOW)
    log_mail(journal, mail("m-1", sent_on=ago(10), direction=Direction.SENT), now=NOW)

    [status] = awaiting_replies(journal.for_account(ACC), TODAY)

    assert (status.waiting_on_them, status.last_on, status.days) == (False, ago(3), 3)


def test_on_the_same_day_the_email_logged_later_is_the_latest(journal):
    log_mail(journal, mail("m-2", sent_on=ago(6), direction=Direction.RECEIVED), now=LATER)
    log_mail(journal, mail("m-1", sent_on=ago(6), direction=Direction.SENT), now=NOW)

    [status] = awaiting_replies(journal.for_account(ACC), TODAY)

    assert status.waiting_on_them is False


def test_only_logged_emails_with_a_thread_and_a_direction_are_judged(store, journal):
    save_raw(
        store,
        JournalData(
            by_account={
                ACC: AccountJournal(
                    notes=[
                        Note(
                            id="n-00000001",
                            kind=TouchKind.EMAIL,
                            text="Typed by hand",
                            happened_on=ago(10),
                            created_at=NOW,
                            source=NoteSource.MANUAL,
                            direction=Direction.SENT,
                            thread="renewal plan",
                        ),
                        Note(
                            id="n-00000002",
                            kind=TouchKind.CALL,
                            text="Rang",
                            happened_on=ago(10),
                            created_at=NOW,
                            source=NoteSource.MAILBOX,
                            thread="renewal plan",
                        ),
                        Note(
                            id="n-00000003",
                            kind=TouchKind.EMAIL,
                            text="Mailed",
                            happened_on=ago(10),
                            created_at=NOW,
                            source=NoteSource.MAILBOX,
                            direction=Direction.SENT,
                        ),
                        Note(
                            id="n-00000004",
                            kind=TouchKind.NOTE,
                            text="A note",
                            happened_on=ago(10),
                            created_at=NOW,
                        ),
                    ]
                )
            }
        ),
    )

    assert awaiting_replies(journal.for_account(ACC), TODAY) == []
    assert awaiting_replies(AccountJournal(), TODAY) == []


# clean_subject, clean_team_name, clean_team_email ----------------------------------------------


@pytest.mark.parametrize("text", ["", "   "])
def test_a_blank_subject_is_allowed_and_stays_blank(text):
    assert clean_subject(text) == ""


def test_a_subject_is_stripped_and_keeps_inner_spaces_and_tabs():
    assert clean_subject("  Renewal  plan\tnow  ") == "Renewal  plan\tnow"


def test_a_subject_may_be_exactly_the_limit_but_not_one_character_over():
    assert clean_subject("s" * MAX_SUBJECT) == "s" * MAX_SUBJECT
    with refused("Keep the subject under 300 characters."):
        clean_subject("s" * (MAX_SUBJECT + 1))


@pytest.mark.parametrize("text", ["a\nb", "a\r\nb", "a\rb", "a b", "a b"])
def test_a_subject_cannot_hold_a_line_break(text):
    with refused("A subject can't contain a line break."):
        clean_subject(text)


def test_a_subject_refuses_control_and_hidden_formatting_characters():
    with refused("A subject can't contain control characters."):
        clean_subject("a\x07b")
    with refused("A subject can't contain hidden formatting characters."):
        clean_subject("a​b")


@pytest.mark.parametrize("text", ["", "   "])
def test_a_team_name_must_be_typed(text):
    with refused("Type their name."):
        clean_team_name(text)


def test_a_team_name_is_stripped_and_limited():
    assert clean_team_name("  Ann Lee  ") == "Ann Lee"
    assert clean_team_name("n" * MAX_TEAM_NAME) == "n" * MAX_TEAM_NAME
    with refused("Keep the name under 100 characters."):
        clean_team_name("n" * (MAX_TEAM_NAME + 1))


def test_a_team_name_refuses_line_breaks_and_odd_characters():
    with refused("A name can't contain a line break."):
        clean_team_name("Ann\nLee")
    with refused("A name can't contain control characters."):
        clean_team_name("Ann\x07Lee")
    with refused("A name can't contain hidden formatting characters."):
        clean_team_name("Ann​Lee")


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_a_blank_team_email_is_none(text):
    assert clean_team_email(text) is None


def test_a_team_email_is_stripped_and_lower_cased():
    assert clean_team_email("  Ann.Lee@Client.COM ") == "ann.lee@client.com"


def test_a_team_email_may_be_exactly_254_characters_but_not_255():
    fits = "a" * 248 + "@x.com"
    assert clean_team_email(fits) == fits
    with refused("Type an email address like name@company.com."):
        clean_team_email("a" * 249 + "@x.com")


@pytest.mark.parametrize(
    "text",
    [
        "ann",
        "ann@client",
        "@client.com",
        "ann@@client.com",
        "ann@client.com@x.com",
        "ann@.client.com",
        "ann@client.com.",
        "ann @client.com",
        "ann@client .com",
        "ann\t@client.com",
        "ann@client\x00.com",
    ],
)
def test_a_team_email_must_look_like_one_address(text):
    with refused("Type an email address like name@company.com."):
        clean_team_email(text)


# add_activity --------------------------------------------------------------------------------


def test_an_activity_keeps_its_subject_who_did_it_and_is_typed_by_hand(journal):
    note = journal.add_activity(
        ACC,
        TouchKind.MEETING,
        "  Went through the plan ",
        ago(1),
        subject=" Renewal  review ",
        by=" Ann Lee ",
        now=NOW,
        today=TODAY,
    )

    assert (note.text, note.subject, note.by) == (
        "Went through the plan",
        "Renewal  review",
        "Ann Lee",
    )
    assert (note.kind, note.source, note.happened_on, note.created_at) == (
        TouchKind.MEETING,
        NoteSource.MANUAL,
        ago(1),
        NOW,
    )
    assert (note.direction, note.message_id, note.people, note.thread) == (None, None, [], "")
    assert note.id.startswith("n-")
    assert journal.for_account(ACC).notes == [note]


def test_a_blank_text_becomes_the_subject(journal):
    note = journal.add_activity(
        ACC, TouchKind.CALL, "   ", ago(1), subject="Pricing call", now=NOW, today=TODAY
    )

    assert (note.text, note.subject) == ("Pricing call", "Pricing call")


def test_an_activity_with_neither_text_nor_subject_is_refused_and_nothing_is_written(
    journal, writes
):
    with refused("Type the note."):
        journal.add_activity(ACC, TouchKind.CALL, "  ", ago(1), now=NOW, today=TODAY)
    with refused("Type the note."):
        journal.add_activity(ACC, TouchKind.CALL, "", ago(1), subject=" ", now=NOW, today=TODAY)

    assert writes == []


def test_the_direction_is_kept_only_for_an_email(journal):
    sent = journal.add_activity(
        ACC,
        TouchKind.EMAIL,
        "Sent the plan",
        ago(1),
        direction=Direction.SENT,
        now=NOW,
        today=TODAY,
    )
    call_note = journal.add_activity(
        ACC,
        TouchKind.CALL,
        "Rang",
        ago(1),
        direction=Direction.RECEIVED,
        now=NOW,
        today=TODAY,
    )

    assert sent.direction is Direction.SENT
    assert call_note.direction is None


def test_a_plain_note_kind_is_allowed_with_a_subject(journal):
    note = journal.add_activity(
        ACC, TouchKind.NOTE, "Context", ago(1), subject="Background", now=NOW, today=TODAY
    )

    assert (note.kind, note.subject) == (TouchKind.NOTE, "Background")


@pytest.mark.parametrize(
    ("options", "text", "message"),
    [
        ({"subject": "s" * (MAX_SUBJECT + 1)}, "Fine", "Keep the subject under 300 characters."),
        ({"subject": "Two\nlines"}, "Fine", "A subject can't contain a line break."),
        ({"by": "Ann\nLee"}, "Fine", "A name can't contain a line break."),
        ({"by": "n" * (MAX_TEAM_NAME + 1)}, "Fine", "Keep the name under 100 characters."),
        ({}, "x" * (MAX_NOTE + 1), "Keep the note under 4,000 characters."),
        ({}, "a\x07b", "A note can't contain control characters."),
    ],
)
def test_an_activity_refuses_a_bad_subject_name_or_text_and_writes_nothing(
    journal, writes, options, text, message
):
    with refused(message):
        journal.add_activity(ACC, TouchKind.CALL, text, ago(1), now=NOW, today=TODAY, **options)

    assert writes == []
    assert journal.for_account(ACC).notes == []


def test_an_activity_day_may_not_be_in_the_future(journal):
    with refused("That date is in the future."):
        journal.add_activity(ACC, TouchKind.CALL, "Fine", ahead(1), now=NOW, today=TODAY)


def test_a_blank_by_means_you(journal):
    note = journal.add_activity(ACC, TouchKind.CALL, "Rang", ago(1), by="   ", now=NOW, today=TODAY)

    assert note.by == ""


def test_an_account_at_its_note_limit_refuses_an_activity(store, journal, writes):
    fill_notes(store)
    writes.clear()

    with refused(TOO_MANY_NOTES):
        journal.add_activity(ACC, TouchKind.EMAIL, "One more", ago(1), now=NOW, today=TODAY)

    assert writes == []
    assert len(journal.for_account(ACC).notes) == MAX_NOTES_PER_ACCOUNT


# log_email -----------------------------------------------------------------------------------


def test_a_logged_email_is_a_mailbox_email_with_its_details(journal):
    logged = log_mail(
        journal,
        mail(
            "m-1",
            sent_on=ago(2),
            direction=Direction.RECEIVED,
            by="Ann Lee",
            people=["ann@client.com", "bo@client.com"],
            snippet="  Thanks for the plan.  ",
        ),
        now=NOW,
    )

    assert logged is not None
    assert (logged.kind, logged.source, logged.text) == (
        TouchKind.EMAIL,
        NoteSource.MAILBOX,
        "Thanks for the plan.",
    )
    assert (logged.subject, logged.happened_on, logged.direction) == (
        "Renewal plan",
        ago(2),
        Direction.RECEIVED,
    )
    assert (logged.by, logged.people, logged.message_id, logged.thread) == (
        "Ann Lee",
        ["ann@client.com", "bo@client.com"],
        "m-1",
        "renewal plan",
    )
    assert (logged.created_at, logged.pinned) == (NOW, False)
    assert journal.for_account(ACC).notes == [logged]


def test_the_same_message_is_logged_once_per_account(journal, writes):
    first = log_mail(journal, mail("m-1"), now=NOW)
    writes.clear()

    again = log_mail(journal, mail("m-1", subject="Changed", snippet="Other"), now=LATER)

    assert first is not None
    assert again is None
    assert writes == []
    assert journal.for_account(ACC).notes == [first]


def test_the_same_message_id_on_another_account_is_logged_there_too(journal):
    assert log_mail(journal, mail("m-1"), account="acc-1", now=NOW) is not None
    assert log_mail(journal, mail("m-1"), account="acc-2", now=NOW) is not None


def test_a_subject_is_one_line_with_breaks_as_spaces_and_hidden_characters_dropped(journal):
    logged = log_mail(journal, mail(subject="Re:\r\nRenewal plan\x07 ​now"), now=NOW)

    assert logged is not None
    assert logged.subject == "Re: Renewal plan now"
    assert logged.thread == "renewal plan now"


def test_a_subject_is_cut_to_the_limit(journal):
    logged = log_mail(journal, mail(subject="s" * (MAX_SUBJECT + 50)), now=NOW)

    assert logged is not None
    assert logged.subject == "s" * MAX_SUBJECT


def test_a_snippet_keeps_its_line_breaks_and_tabs_but_drops_hidden_characters(journal):
    logged = log_mail(journal, mail(snippet="  Line one\r\nLine\ttwo\x07 ​end  "), now=NOW)

    assert logged is not None
    assert logged.text == "Line one\nLine\ttwo end"


def test_a_snippet_is_stripped_and_cut_to_the_note_limit(journal):
    logged = log_mail(journal, mail(snippet="  " + "x" * (MAX_NOTE + 10)), now=NOW)

    assert logged is not None
    assert logged.text == "x" * MAX_NOTE


@pytest.mark.parametrize(
    ("subject", "snippet", "text", "thread"),
    [
        ("Renewal plan", "", "Renewal plan", "renewal plan"),
        ("Renewal plan", "\x07​  ", "Renewal plan", "renewal plan"),
        ("", "", "(No subject)", ""),
        ("\x07", "   ", "(No subject)", ""),
    ],
)
def test_the_text_falls_back_to_the_subject_then_to_no_subject(
    journal, subject, snippet, text, thread
):
    logged = log_mail(journal, mail(subject=subject, snippet=snippet), now=NOW)

    assert logged is not None
    assert (logged.text, logged.thread) == (text, thread)


def test_people_are_cut_to_the_limit_in_the_order_given(journal):
    people = [f"p{index}@client.com" for index in range(MAX_PEOPLE + 5)]

    logged = log_mail(journal, mail(people=people), now=NOW)

    assert logged is not None
    assert logged.people == people[:MAX_PEOPLE]


def test_who_sent_it_drops_hidden_characters_and_blank_means_you(journal):
    sender = log_mail(journal, mail("m-1", by="Ann\x07 Lee​"), now=NOW)
    mine = log_mail(journal, mail("m-2", by=""), now=NOW)

    assert sender is not None and mine is not None
    assert sender.by == "Ann Lee"
    assert mine.by == ""


def test_the_sent_day_is_kept_even_when_it_is_after_today(journal):
    logged = log_mail(journal, mail(sent_on=ahead(2)), now=NOW)

    assert logged is not None
    assert logged.happened_on == ahead(2)


def test_an_account_at_its_note_limit_logs_no_more_email_and_writes_nothing(store, journal, writes):
    fill_notes(store)
    writes.clear()

    assert log_mail(journal, mail("m-new"), now=NOW) is None
    assert writes == []


# Pinning -------------------------------------------------------------------------------------


def test_an_entry_can_be_pinned_and_unpinned(journal):
    note = journal.add_note(ACC, TouchKind.CALL, "Called", TODAY, now=NOW, today=TODAY)

    pinned = journal.set_pinned(ACC, note.id, True)
    assert pinned.pinned is True
    assert journal.for_account(ACC).notes[0].pinned is True
    assert journal.set_pinned(ACC, note.id, False).pinned is False


def test_pinning_an_entry_that_is_not_there_is_a_lookup_error_and_writes_nothing(journal, writes):
    journal.add_note(ACC, TouchKind.CALL, "Called", TODAY, now=NOW, today=TODAY)
    writes.clear()

    with missing("That entry isn't there any more."):
        journal.set_pinned(ACC, "n-ffffffff", True)
    with missing("That entry isn't there any more."):
        journal.set_pinned("acc-none", "n-00000001", True)

    assert writes == []


def test_at_most_five_entries_are_pinned_across_notes_and_activity(journal, writes):
    notes = [
        journal.add_note(ACC, TouchKind.CALL, f"Call {index}", TODAY, now=NOW, today=TODAY)
        for index in range(3)
    ]
    activity = [
        journal.add_activity(ACC, TouchKind.EMAIL, f"Mail {index}", TODAY, now=NOW, today=TODAY)
        for index in range(2)
    ]
    extra = journal.add_note(ACC, TouchKind.NOTE, "Extra", TODAY, now=NOW, today=TODAY)
    for item in [*notes, *activity]:
        journal.set_pinned(ACC, item.id, True)
    assert sum(1 for item in journal.for_account(ACC).notes if item.pinned) == MAX_PINNED
    writes.clear()

    with refused("You can pin up to 5. Unpin one first."):
        journal.set_pinned(ACC, extra.id, True)
    assert writes == []

    journal.set_pinned(ACC, notes[0].id, False)
    assert journal.set_pinned(ACC, extra.id, True).pinned is True


def test_pinning_a_pinned_entry_at_the_cap_or_unpinning_an_unpinned_one_changes_nothing(
    journal, writes
):
    pinned_ones = [
        journal.add_note(ACC, TouchKind.CALL, f"Call {index}", TODAY, now=NOW, today=TODAY)
        for index in range(MAX_PINNED)
    ]
    loose = journal.add_note(ACC, TouchKind.CALL, "Loose", TODAY, now=NOW, today=TODAY)
    for item in pinned_ones:
        journal.set_pinned(ACC, item.id, True)
    writes.clear()

    assert journal.set_pinned(ACC, pinned_ones[0].id, True).pinned is True
    assert journal.set_pinned(ACC, loose.id, False).pinned is False
    assert writes == []


# The account team ----------------------------------------------------------------------------


def test_a_team_member_keeps_a_stripped_name_a_lower_cased_email_and_a_role(journal):
    member = journal.add_team_member(
        ACC, "  Ann Lee ", "  Ann.Lee@Company.com ", TeamRole.ACCOUNT_MANAGER, now=NOW
    )

    assert (member.name, member.email, member.role) == (
        "Ann Lee",
        "ann.lee@company.com",
        TeamRole.ACCOUNT_MANAGER,
    )
    assert member.id.startswith("t-") and len(member.id) == 10
    assert member.created_at == NOW
    assert journal.for_account(ACC).team == [member]


def test_a_team_member_may_have_no_email_and_several_may_have_none(journal):
    first = add_member(journal, "Ann", "")
    second = add_member(journal, "Bo", "   ")

    assert (first.email, second.email) == (None, None)
    assert [member.name for member in journal.for_account(ACC).team] == ["Ann", "Bo"]


def test_a_team_member_must_have_a_name_and_a_valid_email_and_nothing_is_written(journal, writes):
    with refused("Type their name."):
        add_member(journal, "  ", "ann@company.com")
    with refused("Type an email address like name@company.com."):
        add_member(journal, "Ann", "ann at company")

    assert writes == []


def test_two_team_members_cannot_share_an_email_whatever_its_case(journal, writes):
    add_member(journal, "Ann", "ann@company.com")
    writes.clear()

    with refused("Someone on the team already has that email."):
        add_member(journal, "Ann again", "ANN@company.com")

    assert len(journal.for_account(ACC).team) == 1
    assert writes == []


def test_a_team_holds_twelve_people_and_then_refuses(journal):
    assert MAX_TEAM == 12
    for index in range(MAX_TEAM):
        add_member(journal, f"Person {index}", f"p{index}@company.com")

    with refused("This account has 12 people on its team, the most it can keep."):
        add_member(journal, "Thirteenth", "thirteen@company.com")

    assert len(journal.for_account(ACC).team) == MAX_TEAM


def test_removing_a_team_member_returns_true_once_then_false(journal, writes):
    member = add_member(journal)

    assert journal.remove_team_member(ACC, member.id) is True
    assert journal.all() == {}
    writes.clear()
    assert journal.remove_team_member(ACC, member.id) is False
    assert journal.remove_team_member("acc-none", member.id) is False
    assert writes == []


def test_a_team_alone_keeps_an_account_in_the_journal_file(journal, store):
    add_member(journal)

    assert set(saved_file(store)["by_account"]) == {ACC}


def test_moving_an_account_moves_its_team_and_clashing_ids_get_new_ones(store, journal):
    save_raw(
        store,
        JournalData(
            by_account={
                "acc-b": AccountJournal(team=[team_member("t-00000001", "Ann", "ann@x.com")]),
                "acc-a": AccountJournal(team=[team_member("t-00000001", "Bo", "bo@x.com")]),
            }
        ),
    )

    journal.move_account("acc-a", "acc-b")

    team = journal.for_account("acc-b").team
    assert [member.name for member in team] == ["Ann", "Bo"]
    assert team[0].id == "t-00000001"
    assert team[1].id.startswith("t-") and team[1].id != "t-00000001"
    assert "acc-a" not in journal.all()


def test_a_moved_team_member_whose_email_the_target_has_is_dropped_and_so_is_a_repeat(
    store, journal
):
    save_raw(
        store,
        JournalData(
            by_account={
                "acc-b": AccountJournal(team=[team_member("t-00000001", "Ann", "ann@x.com")]),
                "acc-a": AccountJournal(
                    team=[
                        team_member("t-00000002", "Ann again", "ann@x.com"),
                        team_member("t-00000003", "Bo", "bo@x.com"),
                        team_member("t-00000004", "Bo twice", "bo@x.com"),
                        team_member("t-00000005", "No email"),
                    ]
                ),
            }
        ),
    )

    journal.move_account("acc-a", "acc-b")

    team = journal.for_account("acc-b").team
    assert [member.name for member in team] == ["Ann", "Bo", "No email"]
    assert [member.id for member in team] == ["t-00000001", "t-00000003", "t-00000005"]


def test_moving_an_account_drops_an_email_the_target_has_and_a_repeat_of_one_it_moves(
    store, journal
):
    save_raw(
        store,
        JournalData(
            by_account={
                "acc-b": AccountJournal(notes=[mailbox_note("n-00000001", "m-1")]),
                "acc-a": AccountJournal(
                    notes=[
                        mailbox_note("n-00000002", "m-1"),
                        mailbox_note("n-00000003", "m-2"),
                        mailbox_note("n-00000004", "m-2"),
                        entry("n-00000005", TouchKind.CALL, TODAY),
                    ]
                ),
            }
        ),
    )

    journal.move_account("acc-a", "acc-b")

    notes = journal.for_account("acc-b").notes
    assert [note.id for note in notes] == ["n-00000001", "n-00000003", "n-00000005"]
    assert [note.message_id for note in notes] == ["m-1", "m-2", None]
    assert notes[1].source is NoteSource.MAILBOX
    assert notes[1].thread == "renewal plan"


def test_forget_drops_an_accounts_team_and_mailbox_entries_too(journal):
    add_member(journal)
    log_mail(journal, mail("m-1"), now=NOW)

    assert journal.forget(lambda account_id: account_id == ACC) == 1
    assert journal.all() == {}


# Updating an entry --------------------------------------------------------------------------


def test_updating_a_logged_email_keeps_its_pin_sender_and_mailbox_fields(journal):
    logged = log_mail(
        journal,
        mail(direction=Direction.RECEIVED, by="Ann Lee", people=["ann@client.com"]),
        now=NOW,
    )
    assert logged is not None
    journal.set_pinned(ACC, logged.id, True)

    updated = journal.update_note(
        ACC, logged.id, TouchKind.EMAIL, "  Reworded  ", ago(1), now=LATER, today=TODAY
    )

    assert (updated.text, updated.happened_on, updated.edited_at) == ("Reworded", ago(1), LATER)
    assert (updated.id, updated.created_at, updated.pinned) == (logged.id, NOW, True)
    assert (updated.subject, updated.by, updated.direction) == (
        "Renewal plan",
        "Ann Lee",
        Direction.RECEIVED,
    )
    assert (updated.source, updated.message_id, updated.people, updated.thread) == (
        NoteSource.MAILBOX,
        "m-1",
        ["ann@client.com"],
        "renewal plan",
    )


def test_an_email_changed_to_a_call_loses_its_direction_and_stops_awaiting_a_reply(journal):
    logged = log_mail(journal, mail(sent_on=ago(7)), now=NOW)
    assert logged is not None
    assert len(awaiting_replies(journal.for_account(ACC), TODAY)) == 1

    changed = journal.update_note(
        ACC, logged.id, TouchKind.CALL, "Actually a call", ago(7), now=LATER, today=TODAY
    )

    assert (changed.kind, changed.direction) == (TouchKind.CALL, None)
    assert (changed.source, changed.thread) == (NoteSource.MAILBOX, "renewal plan")
    assert awaiting_replies(journal.for_account(ACC), TODAY) == []


def test_updating_an_activity_to_another_kind_keeps_its_subject_and_who_did_it(journal):
    note = journal.add_activity(
        ACC,
        TouchKind.CALL,
        "Pricing talk",
        ago(1),
        subject="Pricing",
        by="Ann Lee",
        now=NOW,
        today=TODAY,
    )

    updated = journal.update_note(
        ACC, note.id, TouchKind.OTHER, "Pricing talk", ago(1), now=LATER, today=TODAY
    )

    assert (updated.kind, updated.subject, updated.by) == (TouchKind.OTHER, "Pricing", "Ann Lee")
    assert updated.source is NoteSource.MANUAL


# Journal files from before the Activity history ----------------------------------------------


def test_a_journal_file_from_before_the_activity_history_loads_with_the_new_fields_empty(
    store, journal
):
    store.root.mkdir(parents=True, exist_ok=True)
    old_file = {
        "by_account": {
            ACC: {
                "notes": [
                    {
                        "id": "n-00000001",
                        "kind": "call",
                        "text": "Old call",
                        "happened_on": "2026-11-08",
                        "created_at": "2026-11-08T09:00:00",
                        "edited_at": None,
                    }
                ],
                "follow_ups": [],
                "snooze": None,
                "outcomes": [],
            }
        }
    }
    (store.root / JOURNAL_FILE).write_text(json.dumps(old_file), encoding="utf-8")

    loaded = journal.for_account(ACC)

    note = loaded.notes[0]
    assert (note.pinned, note.subject, note.by, note.direction) == (False, "", "", None)
    assert (note.source, note.message_id, note.people, note.thread) == (
        NoteSource.MANUAL,
        None,
        [],
        "",
    )
    assert loaded.team == []
    assert [item.text for item in activity_newest_first(loaded)] == ["Old call"]
    assert last_contact(loaded) == date(2026, 11, 8)

    journal.add_team_member(ACC, "Ann Lee", "ann@company.com", TeamRole.CSM, now=NOW)

    assert journal.for_account(ACC).notes == [note]
