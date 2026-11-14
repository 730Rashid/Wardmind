"""The journal in the web: the words and values that the account page, My book, the weekly brief and
the Today page print for notes, follow-ups, snoozes, renewal outcomes and the risk trend.

Each function turns journal and history objects into plain values with the wording already chosen,
so no template works out a date or sees a None. What the CSM typed (a note, a follow-up, an
outcome's note) is shown as text only. Nothing a CSM typed reaches a URL, a class name or a style;
the redirects after an action carry fixed codes from this module (NOTICES and REFUSALS).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from wardmind.core import history, journal, proposals, rules
from wardmind.core.config import Thresholds
from wardmind.core.journal import (
    AccountJournal,
    DueItem,
    FollowUp,
    FollowUpStatus,
    Note,
    Outcome,
    OutcomeKind,
    Snooze,
    TodayView,
    TouchKind,
)
from wardmind.core.models import Health, ScoredAccount
from wardmind.core.settings import DateStyle
from wardmind.web import formatting

FORM = "form"  # the error key for a message about the whole form, not one field
FOLLOW_UP_ID = re.compile(r"f-[0-9a-f]{8}")  # what journal.py makes for a follow-up id
DEMO_NOTE = "You're on demo data. Notes and follow-ups here are kept apart from your own data."
ALL = "all"  # the Activity filter that lists every kind
ACTIVITY_SHOWN = 20  # the Activity box lists the newest this many, then offers "Show all"
ENTRY_PREVIEW = 200  # an entry's text is cut to this in a list; the rest sits in a disclosure
_SHOWN_PEOPLE = 2  # client addresses named on an email before "and N more"
_FILTER_LABELS = {"call": "Calls", "meeting": "Meetings", "email": "Emails", "other": "Other"}
TREND_WIDTH = 640  # the trend chart's viewBox, in user units
TREND_HEIGHT = 200
_INSET = 4  # keeps the line and its dot inside the box
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# The fixed status lines after an action, by the code the redirect carries (?journal=...).
NOTICES = {
    "note-added": "Note added.",
    "note-saved": "Note saved.",
    "note-removed": "Note removed.",
    "follow-up-added": "Follow-up added.",
    "done": "Marked done.",
    "reopened": "Follow-up reopened.",
    "follow-up-removed": "Follow-up removed.",
    "woke": "Snooze ended.",
    "outcome-recorded": "Outcome recorded.",
    "outcome-removed": "Outcome removed.",
    "activity-added": "Activity logged.",
    "activity-saved": "Activity saved.",
    "activity-removed": "Activity removed.",
    "pinned": "Pinned.",
    "unpinned": "Unpinned.",
    "team-added": "Added to the account team.",
    "team-removed": "Removed from the account team.",
    "step-reviewed": "Saved your answer.",
}
# The message for a refusal that came from a Today form, which has no room for the values typed.
REFUSALS = {
    "cap": f"This account has {journal.MAX_OPEN_FOLLOW_UPS} open follow-ups. Finish some first.",
    "gone": "That isn't in this account any more.",
    "done": "That follow-up is already done.",
    "date": "Pick a date from today on, up to a year ahead.",
    "other": "That didn't save. Check it and try again.",
}


# --- Plain words ---------------------------------------------------------------------------


def money(value: float) -> str:
    """Whole units with separators, after the chosen currency's symbol: $120,000 or £120,000."""
    return f"{formatting.currency_symbol()}{value:,.0f}"


def whole(value: float) -> int:
    """The nearest whole number, halves up, so 37.5 reads as 38."""
    return math.floor(value + 0.5)


def short_date(day: date) -> str:
    """'3 Nov 2026'. A recorded decision always carries its year."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def short_day(day: date, today: date) -> str:
    """'Mon 16 Nov', with the year added only when it isn't this year: 'Mon 4 Jan 2027'.
    Month-day style gives 'Mon Nov 16' and 'Mon Jan 4, 2027'; ISO gives 'Mon 2027-01-04'."""
    style = formatting.date_style()
    weekday = _WEEKDAYS[day.weekday()]
    if style is DateStyle.ISO:
        return f"{weekday} {day.isoformat()}"
    month = _MONTHS[day.month - 1]
    if style is DateStyle.MONTH_DAY:
        label = f"{weekday} {month} {day.day}"
        separator = ", "
    else:
        label = f"{weekday} {day.day} {month}"
        separator = " "
    return label if day.year == today.year else f"{label}{separator}{day.year}"


def parse_day(text: str) -> date | None:
    """The date a date box holds (YYYY-MM-DD), or None when it is blank or not a date."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def cut_text(text: str, limit: int = journal.MAX_FOLLOW_UP) -> str:
    """The text on one line, at most `limit` characters, cut at a word boundary with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    head = flat[: limit - 1]  # one character is kept for the ellipsis
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:.") + "…"


def is_follow_up_id(text: str) -> bool:
    return FOLLOW_UP_ID.fullmatch(text) is not None


def _local_day(moment: datetime) -> date:
    """The local calendar day of a naive UTC time, as the journal keeps them."""
    return moment.replace(tzinfo=UTC).astimezone().date()


def _late(days: int) -> str:
    """'1 day late' or '3 days late' for a count of days past due."""
    return "1 day late" if days == 1 else f"{days} days late"


def due_label(due: date, today: date) -> str:
    """'Due today', 'Due tomorrow', 'Due in 5 days', '1 day late' or '3 days late'."""
    days = (due - today).days
    if days < 0:
        return _late(-days)
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    return f"Due in {days} days"


def follow_up_chip(due: date, today: date) -> str:
    """'Follow-up due tomorrow' or 'Follow-up 3 days late', for a card or a row of My book."""
    days = (due - today).days
    if days < 0:
        return f"Follow-up {_late(-days)}"
    return f"Follow-up {due_label(due, today).lower()}"


def contact_label(last: date | None, today: date) -> str:
    """'Contacted today', 'Contacted yesterday', 'Contacted 12 days ago' or 'Not contacted yet'."""
    if last is None:
        return "Not contacted yet"
    days = (today - last).days
    if days <= 0:
        return "Contacted today"
    if days == 1:
        return "Contacted yesterday"
    return f"Contacted {formatting.ago(days)}"


def snooze_line(snooze: Snooze, today: date) -> str:
    """'Snoozed until Mon 16 Nov'."""
    return f"Snoozed until {short_day(snooze.until, today)}"


def done_label(done_on: date, today: date) -> str:
    """'Done today', 'Done yesterday' or 'Done 3 November'."""
    days = (today - done_on).days
    if days <= 0:
        return "Done today"
    if days == 1:
        return "Done yesterday"
    return f"Done {formatting.day_label(done_on, today)}"


def _join(parts: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


# --- Forms: typed values and messages ----------------------------------------------------


@dataclass(frozen=True)
class JournalRefusal:
    """A journal form on the account page that was refused: which form (note, note-edit,
    activity, pin, team, follow-up, move, snooze, outcome), the entry, follow-up or team member it
    was for (note-edit, pin and move only), the values as typed and the messages by field, with FORM
    for a message about the whole form."""

    form: str
    item_id: str = ""
    values: Mapping[str, str] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FormState:
    """A form's values and messages as its template shows them."""

    values: dict[str, str]
    errors: dict[str, str]


def refused(refusal: JournalRefusal | None, form: str, item_id: str = "") -> FormState:
    """The state of one form: the typed values and messages when it was the one refused, and
    nothing otherwise (its template then shows its defaults)."""
    if refusal is None or refusal.form != form or refusal.item_id != item_id:
        return FormState(values={}, errors={})
    return FormState(values=dict(refusal.values), errors=dict(refusal.errors))


def _or_defaults(state: FormState, defaults: dict[str, str]) -> FormState:
    """The refused state when there is one, else the form's defaults."""
    return state if state.values else FormState(values=defaults, errors={})


def typed_values(form: str, fields: Mapping[str, str]) -> dict[str, str]:
    """The values a journal form posted, as they are echoed back on a refusal. Only the fields
    that form has are kept."""
    return {name: fields.get(name, "") for name in _FORM_FIELDS[form]}


_FORM_FIELDS: dict[str, tuple[str, ...]] = {
    "note": ("kind", "happened_on", "text"),
    "note-edit": ("kind", "happened_on", "text"),
    "activity": ("kind", "happened_on", "subject", "by", "direction", "text"),
    "pin": ("pinned",),
    "team": ("name", "email", "role"),
    "follow-up": ("text", "due", "days"),
    "move": ("due", "days"),
    "snooze": ("days", "until"),
    "outcome": ("kind", "renewal_date", "decided_on", "arr_after", "note"),
}
_NOTE_FIELDS = (
    (("in the future", "Pick a date"), "happened_on"),
    (("Pick Note",), "kind"),
    (("Type the note", "under 4,000", "control characters", "formatting characters"), "text"),
)
# Which field a message is about: the first entry whose words appear in it wins.
_FIELD_WORDS: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "note": _NOTE_FIELDS,
    "note-edit": _NOTE_FIELDS,
    "activity": (
        (("in the future", "Pick a date"), "happened_on"),
        (("Pick Call",), "kind"),
        (("Pick Sent",), "direction"),
        (("subject",), "subject"),
        (("name",), "by"),
        (("Type the note", "under 4,000", "control characters", "formatting characters"), "text"),
    ),
    # Email is checked first: "Type an email address like name@company.com." names both.
    "team": (
        (("email", "that email"), "email"),
        (("Pick a role",), "role"),
        (("name",), "name"),
    ),
    "follow-up": (
        (("Pick",), "due"),
        (("Say what", "Keep it under", "line break", "characters"), "text"),
    ),
    "move": ((("Pick",), "due"),),
    "snooze": (
        (("Pick 3",), "days"),
        (("Pick a date", "90 days"), "until"),
    ),
    "outcome": (
        (("in the future", "they decided"), "decided_on"),
        (("renewal date",), "renewal_date"),
        (("ARR",), "arr_after"),
        (("Pick Renewed",), "kind"),
        (("under 1,000", "control characters", "formatting characters"), "note"),
    ),
}


def field_errors(form: str, message: str) -> dict[str, str]:
    """The message keyed by the field it is about. A message no field claims is the whole form's."""
    for words, name in _FIELD_WORDS.get(form, ()):
        if any(word in message for word in words):
            return {name: message}
    return {FORM: message}


def why_code(message: str) -> str:
    """The fixed code (REFUSALS) for a refusal made by a Today form, from the core's message."""
    if "open follow-ups" in message:
        return "cap"
    if "isn't in this account" in message:
        return "gone"
    if "already done" in message:
        return "done"
    if message.startswith("Pick") or "date" in message:
        return "date"
    return "other"


def notice_text(code: str, day: str, today: date) -> str:
    """The status line for a redirect's ?journal= code. `day` is an ISO date for the two codes
    that name one (snoozed, moved); a day that doesn't parse is left out of the sentence."""
    if code == "snoozed":
        until = parse_day(day)
        return f"Snoozed until {short_day(until, today)}." if until else "Snoozed."
    if code == "moved":
        due = parse_day(day)
        return f"Follow-up moved to {short_day(due, today)}." if due else "Follow-up moved."
    return NOTICES.get(code, "")


def refusal_text(code: str) -> str:
    """The message for a refusal code from a redirect (?why=...), or "" for an unknown code."""
    return REFUSALS.get(code, "")


def find_follow_up(
    journals: Mapping[str, AccountJournal], follow_up_id: str
) -> tuple[str, FollowUp] | None:
    """The done follow-up with this id and its account's id, so Undo can be offered for it."""
    if not is_follow_up_id(follow_up_id):
        return None
    for account_id, entry in journals.items():
        for item in entry.follow_ups:
            if item.id == follow_up_id and item.status is FollowUpStatus.DONE:
                return account_id, item
    return None


# --- The account page's journal ---------------------------------------------------------


@dataclass(frozen=True)
class SnoozeView:
    line: str  # "Snoozed until Mon 16 Nov"
    until: str  # ISO


# Where a follow-up with an occasion key (FollowUp.rule) came from, by the key's prefix.
_CLAUDE_RULES = ("agent:", "mcp:")  # Claude, through the MCP server (directly or a suggestion)
_SUGGESTION_RULES = tuple(f"{detector.value}:" for detector in proposals.Detector)


def follow_up_origin(rule: str | None) -> str:
    """The tag a follow-up shows: "" for one you added, "Added by Claude", "From a suggestion"
    (one of Wardmind's suggestions you accepted) or "Added automatically" (a follow-up rule)."""
    if rule is None:
        return ""
    if rule.startswith(_CLAUDE_RULES):
        return "Added by Claude"
    if rule.startswith(_SUGGESTION_RULES):
        return "From a suggestion"
    return "Added automatically"


@dataclass(frozen=True)
class FollowUpRow:
    id: str
    text: str
    due_label: str
    late: bool
    move: FormState  # the move box: the due date to show, and a refused move's message
    automatic: bool  # added by a rule, a suggestion or Claude: shows the `origin` tag
    origin: str = ""  # follow_up_origin(rule): "Added automatically", "From a suggestion", ...
    mirror: str = ""  # journal.slip_line: "Moved 4 times. Do it, hand it off or remove it.", or ""


@dataclass(frozen=True)
class DoneRow:
    id: str
    text: str
    done_label: str


@dataclass(frozen=True)
class NoteRow:
    """A plain note in the Notes box."""

    id: str
    kind: str  # TouchKind value, for the kind's class
    kind_label: str
    day: str  # "8 November", with the year when it isn't this year
    text: str
    preview: str  # the text cut to ENTRY_PREVIEW characters, one line
    long: bool  # the text is longer than the preview, so the rest sits in a disclosure
    edited: bool
    edit: FormState  # the edit form's values; it opens when a save was refused
    edit_open: bool
    pinned: bool
    pin_error: str  # a refused pin's message, or ""
    source_label: str  # "Added by Claude" or "From a suggestion"; "" for your own notes


@dataclass(frozen=True)
class ActivityRow:
    """An activity (a call, meeting, email or other touch) in the Activity box, or in the pinned
    strip, which shows only the pin and the summary and never an edit form."""

    id: str
    kind: str  # TouchKind value, for the kind's class
    kind_label: str  # "Call", "Email"...
    day: str  # "8 November", with the year when it isn't this year
    subject: str  # "" when there is none
    by: str  # who did it; "" means you
    direction: str  # "sent", "received", or "" (emails only)
    direction_line: str  # "Sent to priya@acme.com", "Received from ...", "Sent"; "" for non-emails
    from_mailbox: bool  # logged by the mailbox sync
    source_label: str  # "Added by Claude" or "From a suggestion"; "" for your own entries
    text: str  # the whole text, as typed or as the email's start
    preview: str  # the text cut to ENTRY_PREVIEW characters, one line
    long: bool  # the text is longer than the preview, so the rest sits in a disclosure
    edited: bool
    pinned: bool
    edit: FormState  # the edit form's values; it opens when a save was refused
    edit_open: bool
    pin_error: str  # a refused pin's message, or ""


@dataclass(frozen=True)
class ActivityFilter:
    value: str  # ALL, or a TouchKind value: call, meeting, email, other
    label: str  # "All", "Calls", "Meetings", "Emails", "Other"
    count: int  # the entries of this kind (all of them, not only the ones listed)
    current: bool


@dataclass(frozen=True)
class ReplyChip:
    """An email thread waiting on someone: "Waiting on them: Renewal plan, 6 days"."""

    text: str
    waiting_on_them: bool  # True: we sent last; False: they did and we haven't answered


@dataclass(frozen=True)
class TeamRow:
    id: str
    name: str
    role: str  # the role's label, e.g. "Account manager"
    email: str  # "" when none


@dataclass(frozen=True)
class OutcomeRow:
    id: str
    line: str  # "Renewed on 3 Nov 2026: $120,000 to $130,000"
    note: str


@dataclass(frozen=True)
class JournalPanel:
    """Everything the account page's journal parts print. Built by `panel`."""

    last_contact: str  # "Contacted 12 days ago", or "Not contacted yet"
    open_count: int  # open follow-ups; the header shows the fact only above zero
    suggested: str  # the next step, cut to 200 characters for the follow-up box
    snooze: SnoozeView | None
    snooze_choices: tuple[int, ...]
    follow_ups: list[FollowUpRow]  # open, by due date
    done: list[DoneRow]  # recently done, newest first; each offers Undo
    notes: list[NoteRow]  # the plain notes (kind NOTE), newest first
    activity: list[ActivityRow]  # the Activity box's rows: the filter's, newest first, cut to 20
    activity_filter: str  # the chosen filter: ALL, or a kind's value
    activity_filters: list[ActivityFilter]  # All, then each kind with its count
    activity_limit: int  # how many entries the list shows before "Show all" (ACTIVITY_SHOWN)
    activity_total: int  # every activity entry, whatever the filter
    activity_more: int  # entries of the filter that aren't listed (0 when all are)
    activity_all: bool  # every entry of the filter is listed
    activity_collapse: bool  # every entry is listed and there are more than ACTIVITY_SHOWN
    activity_form: FormState
    replies: list[ReplyChip]  # threads waiting on someone, at the top of the Activity box
    pinned: list[ActivityRow]  # pinned entries of any kind, for the pinned strip
    team: list[TeamRow]  # in the order added
    team_form: FormState
    team_open: bool  # the "Add someone" disclosure opens when a team form was refused
    team_roles: list[tuple[str, str]]  # (TeamRole value, label) for the role menu
    team_names: list[str]  # the team's names, offered as "Who" on an activity
    outcomes: list[OutcomeRow]  # in the order recorded
    add_follow_up: FormState
    snooze_form: FormState
    note_form: FormState
    outcome_form: FormState
    outcome_open: bool  # the renewal date has passed and no outcome answers it
    awaiting: str  # "The renewal date passed 12 days ago. How did it go?", or ""
    refused: str  # a refusal sent back from Today, or ""
    undo_id: str  # the follow-up an Undo button is offered for, or ""
    demo: bool  # demo data is showing: a line says the journal is kept apart
    snooze_mirror: str = ""  # journal.snooze_line: "Snoozed 3 times since 2 Oct 2026...", or ""


def panel(
    *,
    scored: ScoredAccount,
    account_journal: AccountJournal,
    suggested: str,
    today: date,
    now: datetime,
    demo: bool,
    refusal: JournalRefusal | None,
    why: str,
    undo_id: str,
    activity: str = ALL,
    show_all: bool = False,
    follow_up_days: int = journal.FOLLOW_UP_DEFAULT_DAYS,
    skip_weekends: bool = False,
    reply_wait_days: int = journal.REPLY_WAIT_DAYS,
    answer_wait_days: int = journal.ANSWER_WAIT_DAYS,
    note_text: str = "",
) -> JournalPanel:
    """The account page's journal. A refusal's values and messages come back on their own form.
    `activity` is the Activity filter a link asked for (anything unknown is ALL); `show_all` lists
    every entry of that filter rather than the newest ACTIVITY_SHOWN. `note_text` fills the Notes
    box when a note template was chosen (it is not saved until the note is added)."""
    account = scored.account
    open_items = journal.open_follow_ups(account_journal)
    chosen = activity_choice(activity)
    entries = journal.activity_newest_first(account_journal)
    matching = [entry for entry in entries if chosen == ALL or entry.kind.value == chosen]
    listed = matching if show_all else matching[:ACTIVITY_SHOWN]
    activity_rows = [_activity_row(entry, refusal, today) for entry in listed]
    team_state = refused(refusal, "team")
    team_form = _or_defaults(
        team_state, {"name": "", "email": "", "role": journal.TeamRole.CSM.value}
    )
    snooze = journal.active_snooze(account_journal, scored.health, today)
    awaiting = journal.awaiting_outcome(account, account_journal, today)
    passed = (today - account.renewal_date).days
    default_due = rules.due_day(today, follow_up_days, skip_weekends).isoformat()
    outcome_refused = _or_defaults(
        refused(refusal, "outcome"),
        {
            "kind": "",
            "renewal_date": account.renewal_date.isoformat(),
            "decided_on": today.isoformat(),
            "arr_after": "",
            "note": "",
        },
    )
    return JournalPanel(
        last_contact=contact_label(journal.last_contact(account_journal), today),
        open_count=len(open_items),
        suggested=cut_text(suggested),
        snooze=(
            SnoozeView(line=snooze_line(snooze, today), until=snooze.until.isoformat())
            if snooze is not None
            else None
        ),
        snooze_choices=journal.SNOOZE_CHOICES,
        follow_ups=[
            FollowUpRow(
                id=item.id,
                text=item.text,
                due_label=due_label(item.due, today),
                late=item.due < today,
                move=_or_defaults(refused(refusal, "move", item.id), {"due": item.due.isoformat()}),
                automatic=item.rule is not None,
                origin=follow_up_origin(item.rule),
                mirror=journal.slip_line(item) or "",
            )
            for item in open_items
        ],
        done=[
            DoneRow(
                id=item.id,
                text=item.text,
                done_label=done_label(_local_day(item.done_at or now), today),
            )
            for item in journal.recently_done(account_journal, now)
        ],
        notes=[
            _note_row(note, refusal, today)
            for note in journal.plain_notes_newest_first(account_journal)
        ],
        activity=activity_rows,
        activity_filter=chosen,
        activity_filters=_activity_filters(entries, chosen),
        activity_limit=ACTIVITY_SHOWN,
        activity_total=len(entries),
        activity_more=len(matching) - len(listed),
        activity_all=show_all,
        activity_collapse=show_all and len(matching) > ACTIVITY_SHOWN,
        activity_form=_or_defaults(
            refused(refusal, "activity"),
            {
                "kind": journal.TouchKind.CALL.value,
                "happened_on": today.isoformat(),
                "subject": "",
                "by": "",
                "direction": "",
                "text": "",
            },
        ),
        replies=[
            _reply_chip(status)
            for status in journal.awaiting_replies(
                account_journal,
                today,
                reply_wait_days=reply_wait_days,
                answer_wait_days=answer_wait_days,
            )
        ],
        pinned=[
            _activity_row(entry, refusal, today)
            for entry in journal.pinned_entries(account_journal)
        ],
        team=[
            TeamRow(
                id=member.id,
                name=member.name,
                role=member.role.label,
                email=member.email or "",
            )
            for member in account_journal.team
        ],
        team_form=team_form,
        team_open=bool(team_state.values),
        team_roles=[(role.value, role.label) for role in journal.TeamRole],
        team_names=[member.name for member in account_journal.team],
        outcomes=[
            OutcomeRow(id=outcome.id, line=outcome_line(outcome), note=outcome.note)
            for outcome in account_journal.outcomes
        ],
        add_follow_up=_or_defaults(
            refused(refusal, "follow-up"),
            {"text": cut_text(suggested), "due": default_due},
        ),
        snooze_form=refused(refusal, "snooze"),
        note_form=_or_defaults(
            refused(refusal, "note"),
            {
                "kind": journal.TouchKind.NOTE.value,
                "happened_on": today.isoformat(),
                "text": note_text,
            },
        ),
        outcome_form=outcome_refused,
        outcome_open=awaiting or bool(refused(refusal, "outcome").values),
        awaiting=(
            f"The renewal date passed {formatting.ago(passed)}. How did it go?" if awaiting else ""
        ),
        refused=refusal_text(why),
        undo_id=undo_id,
        demo=demo,
        snooze_mirror=journal.snooze_line(account_journal, today) or "",
    )


def _source_label(source: journal.NoteSource) -> str:
    """Who made an entry, when it wasn't you: "Added by Claude" or "From a suggestion". "" for your
    own entries, and for a mailbox email, whose label ("Logged from mailbox") the page shows."""
    return {
        journal.NoteSource.AGENT: "Added by Claude",
        journal.NoteSource.SUGGESTION: "From a suggestion",
    }.get(source, "")


def _note_row(note: Note, refusal: JournalRefusal | None, today: date) -> NoteRow:
    edit_refused = refused(refusal, "note-edit", note.id)
    edit = _or_defaults(
        edit_refused,
        {
            "kind": note.kind.value,
            "happened_on": note.happened_on.isoformat(),
            "text": note.text,
        },
    )
    preview, long = entry_preview(note.text)
    return NoteRow(
        id=note.id,
        kind=note.kind.value,
        kind_label=note.kind.label,
        day=formatting.day_label(note.happened_on, today),
        text=note.text,
        preview=preview,
        long=long,
        edited=note.edited_at is not None,
        edit=edit,
        edit_open=bool(edit_refused.values),
        pinned=note.pinned,
        pin_error=refused(refusal, "pin", note.id).errors.get(FORM, ""),
        source_label=_source_label(note.source),
    )


def _activity_row(note: Note, refusal: JournalRefusal | None, today: date) -> ActivityRow:
    edit_refused = refused(refusal, "note-edit", note.id)
    edit = _or_defaults(
        edit_refused,
        {
            "kind": note.kind.value,
            "happened_on": note.happened_on.isoformat(),
            "text": note.text,
        },
    )
    preview, long = entry_preview(note.text)
    return ActivityRow(
        id=note.id,
        kind=note.kind.value,
        kind_label=note.kind.label,
        day=formatting.day_label(note.happened_on, today),
        subject=note.subject,
        by=note.by,
        direction=note.direction.value if note.direction is not None else "",
        direction_line=_direction_line(note),
        from_mailbox=note.source is journal.NoteSource.MAILBOX,
        source_label=_source_label(note.source),
        text=note.text,
        preview=preview,
        long=long,
        edited=note.edited_at is not None,
        pinned=note.pinned,
        edit=edit,
        edit_open=bool(edit_refused.values),
        pin_error=refused(refusal, "pin", note.id).errors.get(FORM, ""),
    )


def activity_choice(text: str) -> str:
    """The Activity filter a link asked for: a kind's value, or ALL for anything else (a plain
    note's value included, since notes have their own box)."""
    return text if text == ALL or text in _FILTER_LABELS else ALL


def entry_preview(text: str) -> tuple[str, bool]:
    """The start of an entry's text for a list, on one line, and whether there is more to show."""
    flat = " ".join(text.split())
    return cut_text(text, ENTRY_PREVIEW), len(flat) > ENTRY_PREVIEW


def _activity_filters(entries: Sequence[Note], chosen: str) -> list[ActivityFilter]:
    """All, then each activity kind with how many entries it has."""
    filters = [ActivityFilter(value=ALL, label="All", count=len(entries), current=chosen == ALL)]
    for value, label in _FILTER_LABELS.items():
        count = sum(1 for entry in entries if entry.kind.value == value)
        filters.append(
            ActivityFilter(value=value, label=label, count=count, current=chosen == value)
        )
    return filters


def _direction_line(note: Note) -> str:
    """'Sent to priya@acme.com and sam@acme.com', 'Received from priya@acme.com and 2 more', or
    'Sent' when no client address was logged. "" for anything but an email."""
    if note.direction is None:
        return ""
    sent = note.direction is journal.Direction.SENT
    verb = "Sent" if sent else "Received"
    if not note.people:
        return verb
    shown = note.people[:_SHOWN_PEOPLE]
    rest = len(note.people) - len(shown)
    names = [*shown, f"{rest} more"] if rest else list(shown)
    return f"{verb} {'to' if sent else 'from'} {_join(names)}"


def _reply_chip(status: journal.ThreadStatus) -> ReplyChip:
    subject = cut_text(status.subject, 60) or "No subject"
    who = "Waiting on them" if status.waiting_on_them else "Waiting on you"
    return ReplyChip(
        text=f"{who}: {subject}, {status.days} days",
        waiting_on_them=status.waiting_on_them,
    )


def outcome_line(outcome: Outcome) -> str:
    """'Renewed on 3 Nov 2026: $120,000 to $130,000', or 'Churned on 3 Nov 2026. ARR was ...'."""
    day = short_date(outcome.decided_on)
    before = money(outcome.arr_before)
    if outcome.kind is OutcomeKind.CHURNED:
        return f"Churned on {day}. ARR was {before}."
    if outcome.arr_after is None:
        return f"{outcome.kind.label} on {day}. ARR was {before}."
    return f"{outcome.kind.label} on {day}: {before} to {money(outcome.arr_after)}"


# --- The trend (risk over the last 90 days) ------------------------------------------------


@dataclass(frozen=True)
class Band:
    y: float
    height: float
    css: str  # the zone's class in app.css


@dataclass(frozen=True)
class Stop:
    offset: float  # 0 to 1 down the plot
    tone: str  # "at_risk", "watch" or "healthy": the stop's colour class


@dataclass(frozen=True)
class Gridline:
    y: float
    label: str  # "At risk", "Watch" or "Healthy", set just under the line
    label_y: float


@dataclass(frozen=True)
class Tick:
    x: float
    text: str  # "10 Aug", or "Today"
    anchor: str  # the text-anchor: "start", "middle" or "end"


@dataclass(frozen=True)
class Marker:
    x: float
    kind: str  # "outcome", "talk" (a call or meeting), "email" or "note": its class and shape


@dataclass(frozen=True)
class Sparkline:
    shown: bool
    points: str  # the polyline's "x,y" pairs, oldest first; "" with a single point
    dot_x: float
    dot_y: float
    dot_health: str  # the latest point's health value, for the dot's class
    zones: list[Band]  # the watch and at-risk zones, faint behind the line
    caption: str  # the sentence under the chart, or the first-day message
    label: str  # the chart's accessible name
    area: str = ""  # the polygon under the line: the line's points closed along the bottom
    stops: list[Stop] = field(default_factory=list)  # the line's colour, by zone, top to bottom
    gridlines: list[Gridline] = field(default_factory=list)
    ticks: list[Tick] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)  # one per day with activity
    kinds: list[tuple[str, str]] = field(default_factory=list)  # (kind, name) for the key
    data: str = "[]"  # JSON for app.js: one entry per day with a reading or activity


_MARKER_ORDER = ("outcome", "talk", "email", "note")  # the strongest kind names a day's marker
MARKER_KEYS = {
    "outcome": "Renewal outcome",
    "talk": "Call or meeting",
    "email": "Email",
    "note": "Note",
}
_DAY_EVENTS = 3  # a day's tooltip names this many entries, then "and N more"
_EVENT_TEXT = 60  # an entry's subject or text is cut to this in the tooltip


def sparkline(
    points: Sequence[history.TrendPoint],
    thresholds: Thresholds,
    today: date,
    account_journal: AccountJournal | None = None,
) -> Sparkline:
    """The account's risk over the last TREND_DAYS days as SVG geometry in a TREND_WIDTH by
    TREND_HEIGHT box, with risk 100 at the top of the plot. Days sit on the 90-day window, so a
    young history is at the right and fills in to the left. Under the plot, a row of marks shows
    the days with calls, meetings, emails, notes or a renewal outcome from `account_journal`.

    `data` carries, per day, what app.js shows when the pointer or the arrow keys land on it: the
    day, the risk and health that day, the change from the reading before, and what happened."""
    if not points:
        return Sparkline(
            shown=False,
            points="",
            dot_x=0,
            dot_y=0,
            dot_health="healthy",
            zones=[],
            caption="",
            label="",
        )
    latest = points[-1]
    at_risk_y = _y(thresholds.at_risk)
    watch_y = _y(thresholds.watch)
    zones = [
        Band(y=PLOT_TOP, height=round(at_risk_y - PLOT_TOP, 1), css="trend-zone--at-risk"),
        Band(y=at_risk_y, height=round(watch_y - at_risk_y, 1), css="trend-zone--watch"),
    ]
    span = PLOT_BOTTOM - PLOT_TOP
    at_risk_at = round((at_risk_y - PLOT_TOP) / span, 4)
    watch_at = round((watch_y - PLOT_TOP) / span, 4)
    # A point exactly on a threshold has that threshold's health, so the colour changes at the line.
    stops = [
        Stop(0, "at_risk"),
        Stop(at_risk_at, "at_risk"),
        Stop(at_risk_at, "watch"),
        Stop(watch_at, "watch"),
        Stop(watch_at, "healthy"),
        Stop(1, "healthy"),
    ]
    gridlines = [
        Gridline(
            y=PLOT_TOP,
            label=f"At risk {whole(thresholds.at_risk)}+",
            label_y=PLOT_TOP + _LABEL_DROP,
        ),
        Gridline(
            y=at_risk_y,
            label=f"Watch {whole(thresholds.watch)}+",
            label_y=at_risk_y + _LABEL_DROP,
        ),
        Gridline(y=watch_y, label="Healthy", label_y=watch_y + _LABEL_DROP),
    ]
    pairs = [(_x(point.day, today), _y(point.risk)) for point in points]
    line = " ".join(f"{x},{y}" for x, y in pairs) if len(points) > 1 else ""
    area = (
        f"{pairs[0][0]},{PLOT_BOTTOM} {line} {pairs[-1][0]},{PLOT_BOTTOM}"
        if len(points) > 1
        else ""
    )
    events = _events_by_day(account_journal, today) if account_journal is not None else {}
    markers = [
        Marker(x=_x(day, today), kind=kinds[0]) for day, (kinds, _) in sorted(events.items())
    ]
    shown_kinds = [
        (kind, MARKER_KEYS[kind]) for kind in _MARKER_ORDER if any(m.kind == kind for m in markers)
    ]
    caption = _caption(points, today)
    return Sparkline(
        shown=True,
        points=line,
        dot_x=pairs[-1][0],
        dot_y=pairs[-1][1],
        dot_health=latest.health.value,
        zones=zones,
        caption=caption,
        label=f"Risk over the last {history.TREND_DAYS} days. {caption}",
        area=area,
        stops=stops,
        gridlines=gridlines,
        ticks=_ticks(today),
        markers=markers,
        kinds=shown_kinds,
        data=_trend_data(points, events, today),
    )


PLOT_TOP = _INSET
PLOT_BOTTOM = 150  # the plot's floor; the activity marks and the dates sit under it
PLOT_LEFT = _INSET
PLOT_RIGHT = TREND_WIDTH - _INSET
MARKER_Y = 168
TICK_Y = 194
_LABEL_DROP = 15  # a zone's name sits this far under the top of its zone
_TICK_DAYS = (90, 60, 30, 0)


def _y(risk: float) -> float:
    span = PLOT_BOTTOM - PLOT_TOP
    return round(PLOT_TOP + (100 - min(max(risk, 0.0), 100.0)) / 100 * span, 1)


def _x(day: date, today: date) -> float:
    span = PLOT_RIGHT - PLOT_LEFT
    days_ago = min(max((today - day).days, 0), history.TREND_DAYS)
    return round(PLOT_LEFT + (1 - days_ago / history.TREND_DAYS) * span, 1)


def _tick_text(day: date) -> str:
    """'10 Aug', 'Aug 10' or '08-10', by the date style."""
    style = formatting.date_style()
    if style is DateStyle.ISO:
        return day.isoformat()[5:]
    month = _MONTHS[day.month - 1]
    return f"{month} {day.day}" if style is DateStyle.MONTH_DAY else f"{day.day} {month}"


def _ticks(today: date) -> list[Tick]:
    ticks: list[Tick] = []
    for days in _TICK_DAYS:
        if days > history.TREND_DAYS:
            continue
        day = today - timedelta(days=days)
        anchor = "start" if days == history.TREND_DAYS else "end" if days == 0 else "middle"
        ticks.append(
            Tick(x=_x(day, today), text="Today" if days == 0 else _tick_text(day), anchor=anchor)
        )
    return ticks


def _note_kind(note: Note) -> str:
    if note.kind in (TouchKind.CALL, TouchKind.MEETING):
        return "talk"
    if note.kind is TouchKind.NOTE:
        return "note"
    return "email"  # an email, or any other touch with the customer


def _note_event(note: Note) -> str:
    """'Call: Quarterly review', 'Email received: Renewal terms', 'Note: Champion is leaving'."""
    if note.kind is TouchKind.EMAIL and note.direction is not None:
        what = f"Email {note.direction.label.lower()}"
    else:
        what = note.kind.label
    detail = note.subject or note.text
    detail = cut_text(detail, _EVENT_TEXT) if detail else ""
    return f"{what}: {detail}" if detail else what


def _events_by_day(
    account_journal: AccountJournal, today: date
) -> dict[date, tuple[list[str], list[str]]]:
    """Each day in the window with activity: its marker kinds (strongest first) and what happened
    that day, outcomes first and then entries in the order they were added."""
    since = today - timedelta(days=history.TREND_DAYS)
    kinds: dict[date, set[str]] = {}
    texts: dict[date, list[str]] = {}
    for outcome in account_journal.outcomes:
        if since <= outcome.decided_on <= today:
            kinds.setdefault(outcome.decided_on, set()).add("outcome")
            texts.setdefault(outcome.decided_on, []).append(f"Outcome: {outcome.kind.label}")
    for note in account_journal.notes:
        if since <= note.happened_on <= today:
            kinds.setdefault(note.happened_on, set()).add(_note_kind(note))
            texts.setdefault(note.happened_on, []).append(_note_event(note))
    days: dict[date, tuple[list[str], list[str]]] = {}
    for day, found in kinds.items():
        ordered = [kind for kind in _MARKER_ORDER if kind in found]
        lines = texts[day]
        if len(lines) > _DAY_EVENTS:
            extra = len(lines) - _DAY_EVENTS
            lines = [*lines[:_DAY_EVENTS], f"and {extra} more"]
        days[day] = (ordered, lines)
    return days


def _trend_data(
    points: Sequence[history.TrendPoint],
    events: Mapping[date, tuple[list[str], list[str]]],
    today: date,
) -> str:
    by_day = {point.day: point for point in points}
    rows: list[dict[str, object]] = []
    previous: history.TrendPoint | None = None
    for day in sorted(set(by_day) | set(events)):
        point = by_day.get(day)
        row: dict[str, object] = {
            "x": _x(day, today),
            "day": "Today" if day == today else short_day(day, today),
            "events": events[day][1] if day in events else [],
        }
        if point is not None:
            row.update(
                y=_y(point.risk),
                risk=whole(point.risk),
                health=point.health.label,
                tone=point.health.value,
                change=whole(point.risk) - whole(previous.risk) if previous is not None else None,
                rank=point.rank,
            )
            previous = point
        rows.append(row)
    return json.dumps(rows, separators=(",", ":"), ensure_ascii=False)


def _caption(points: Sequence[history.TrendPoint], today: date) -> str:
    """'Risk 52 now, 38 a week ago, 30 on 10 Aug.' The earlier points used are the nearest ones: a
    week ago means within three days of it, and the oldest point is named by its day."""
    earlier = [point for point in points if point.day < today]
    if not earlier:
        return "Wardmind started keeping score history today. The trend fills in as days pass."
    parts = [f"Risk {whole(points[-1].risk)} now"]
    target = today - timedelta(days=7)
    week = min(earlier, key=lambda point: abs((point.day - target).days))
    week_found = abs((week.day - target).days) <= 3
    if week_found:
        parts.append(f"{whole(week.risk)} a week ago")
    oldest = earlier[0]
    if not (week_found and oldest.day == week.day):
        parts.append(f"{whole(oldest.risk)} on {oldest.day.day} {_MONTHS[oldest.day.month - 1]}")
    return f"{', '.join(parts)}."


# --- My book: chips on each card and row, and the "Since last week" panel ---------------------


@dataclass(frozen=True)
class BookChips:
    snooze: str  # "Snoozed until Mon 16 Nov", or ""
    follow_up: str  # "Follow-up due tomorrow", or ""
    late: bool  # the earliest open follow-up is past due
    contact: str  # "Contacted 12 days ago", or "" when there is no contact yet


def book_chips(account_journal: AccountJournal, health: Health, today: date) -> BookChips:
    """The small labels on an account's card or row: its snooze, its earliest open follow-up and
    its last contact. An empty journal gives nothing to show."""
    snooze = journal.active_snooze(account_journal, health, today)
    open_items = journal.open_follow_ups(account_journal)
    latest = journal.last_contact(account_journal)
    first = open_items[0] if open_items else None
    return BookChips(
        snooze=snooze_line(snooze, today) if snooze is not None else "",
        follow_up=follow_up_chip(first.due, today) if first is not None else "",
        late=first is not None and first.due < today,
        contact=contact_label(latest, today) if latest is not None else "",
    )


@dataclass(frozen=True)
class SinceRow:
    account_id: str  # "" for an account no longer in the book: its name shows, unlinked
    name: str
    label: str  # "Got worse", "New", ...
    kind: str  # the change's kind, for the tag's class
    sentence: str


@dataclass(frozen=True)
class SinceView:
    message: str  # shown instead of the rows: "" when there are rows
    rows: list[SinceRow]
    more: int  # changes not listed here


def since_view(
    week: history.WeekChanges | None,
    started: date | None,
    today: date,
    limit: int,
) -> SinceView:
    """The "Since last week" panel: up to `limit` changes, or the message that says why there are
    none. `started` is the first day with a snapshot (today when there is none yet)."""
    if week is None:
        begun = started or today
        return SinceView(
            message=(
                f"Wardmind started keeping score history on {short_day(begun, today)}. "
                f"From {short_day(begun + timedelta(days=history.WEEK_DAYS), today)} this shows "
                "what changed each week."
            ),
            rows=[],
            more=0,
        )
    if week.unchanged:
        return SinceView(message="No changes since last week.", rows=[], more=0)
    rows = [
        SinceRow(
            account_id=("" if change.kind is history.ChangeKind.GONE else change.account_id),
            name=change.name,
            label=change.kind.label,
            kind=change.kind.value,
            sentence=change.sentence,
        )
        for change in week.changes[:limit]
    ]
    hidden = max(0, len(week.changes) - limit) + week.more
    return SinceView(message="", rows=rows, more=hidden)


# --- The weekly brief: follow-ups due this week -----------------------------------------------


@dataclass(frozen=True)
class WeekFollowUp:
    account_id: str
    name: str
    text: str
    due_label: str
    late: bool


def week_follow_ups(view: TodayView, today: date, limit: int) -> tuple[list[WeekFollowUp], int]:
    """Open follow-ups due within seven days, late ones included, most late first; and how many
    more there are beyond `limit`. The Today view already scopes and sorts them this way."""
    items = [*view.due, *view.upcoming]
    rows = [
        WeekFollowUp(
            account_id=item.scored.account.id,
            name=item.scored.account.name,
            text=item.follow_up.text,
            due_label=due_label(item.follow_up.due, today),
            late=item.follow_up.due < today,
        )
        for item in items
    ]
    return rows[:limit], max(0, len(rows) - limit)


# --- The Today page ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TodayDue:
    account_id: str
    name: str
    follow_up_id: str
    text: str
    due_label: str
    late: bool
    automatic: bool  # added by a rule, a suggestion or Claude: shows the `origin` tag
    origin: str = ""  # follow_up_origin(rule): "Added automatically", "From a suggestion", ...
    mirror: str = ""  # journal.slip_line: "Moved 4 times. Do it, hand it off or remove it.", or ""


@dataclass(frozen=True)
class TodayAttention:
    account_id: str
    name: str
    rank: int
    health: str  # Health value, for the badge's class
    health_label: str
    reason: str  # the top reason, in full
    contact: str  # "Contacted 12 days ago" or "Not contacted yet"
    suggested: str  # the next step, cut for the quick follow-up form
    mirror: str = ""  # journal.snooze_line for the account, or ""


@dataclass(frozen=True)
class TodayRenewal:
    account_id: str
    name: str
    passed: str  # "Renewal date passed 12 days ago"


@dataclass(frozen=True)
class TodaySnoozed:
    account_id: str
    name: str
    line: str  # "Snoozed until Mon 16 Nov"
    mirror: str = ""  # journal.snooze_line for the account, or ""


@dataclass(frozen=True)
class TodayPage:
    summary: str  # "3 follow-ups due, 5 accounts need attention and 1 renewal to record.", or ""
    contacted: str  # "You contacted 4 accounts in the last 7 days."
    due: list[TodayDue]
    attention: list[TodayAttention]
    renewals: list[TodayRenewal]
    upcoming: list[TodayDue]
    snoozed: list[TodaySnoozed]

    @property
    def empty(self) -> bool:
        return not (self.due or self.attention or self.renewals or self.upcoming or self.snoozed)


def today_page(
    view: TodayView, today: date, journals: Mapping[str, AccountJournal] | None = None
) -> TodayPage:
    """The Today page's rows and sentences, from the core's view. `journals` (every account's
    journal, by id) gives the snooze mirror for the Needs attention and Snoozed rows; without it
    those rows carry no mirror. The follow-up mirror needs no journal: it is on the follow-up."""
    found = journals or {}

    def snooze_mirror(account_id: str) -> str:
        return journal.snooze_line(found.get(account_id, AccountJournal()), today) or ""

    due = [_today_due(item, today) for item in view.due]
    attention = [
        TodayAttention(
            account_id=item.scored.account.id,
            name=item.scored.account.name,
            rank=item.rank,
            health=item.scored.health.value,
            health_label=item.scored.health.label,
            reason=item.scored.top_reason,
            contact=contact_label(item.last_contact, today),
            suggested=cut_text(item.scored.suggested_action),
            mirror=snooze_mirror(item.scored.account.id),
        )
        for item in view.attention
    ]
    renewals = [
        TodayRenewal(
            account_id=scored.account.id,
            name=scored.account.name,
            passed=_passed(scored.account.renewal_date, today),
        )
        for scored in view.outcomes
    ]
    return TodayPage(
        summary=_summary(len(due), len(attention), len(renewals)),
        contacted=_contacted(view.contacted_this_week),
        due=due,
        attention=attention,
        renewals=renewals,
        upcoming=[_today_due(item, today) for item in view.upcoming],
        snoozed=[
            TodaySnoozed(
                account_id=item.scored.account.id,
                name=item.scored.account.name,
                line=snooze_line(item.snooze, today),
                mirror=snooze_mirror(item.scored.account.id),
            )
            for item in view.snoozed
        ],
    )


def _passed(renewal: date, today: date) -> str:
    """'Renewal date passed 12 days ago', for a renewal whose date is behind today."""
    return f"Renewal date passed {formatting.ago((today - renewal).days)}"


def _today_due(item: DueItem, today: date) -> TodayDue:
    return TodayDue(
        account_id=item.scored.account.id,
        name=item.scored.account.name,
        follow_up_id=item.follow_up.id,
        text=item.follow_up.text,
        due_label=due_label(item.follow_up.due, today),
        late=item.follow_up.due < today,
        automatic=item.follow_up.rule is not None,
        origin=follow_up_origin(item.follow_up.rule),
        mirror=journal.slip_line(item.follow_up) or "",
    )


def _summary(due: int, attention: int, renewals: int) -> str:
    parts: list[str] = []
    if due:
        parts.append(f"{due} {'follow-up' if due == 1 else 'follow-ups'} due")
    if attention:
        verb = "account needs" if attention == 1 else "accounts need"
        parts.append(f"{attention} {verb} attention")
    if renewals:
        parts.append(f"{renewals} {'renewal' if renewals == 1 else 'renewals'} to record")
    if not parts:
        return ""
    return f"{_join(parts)}."


def _contacted(count: int) -> str:
    if count == 0:
        return "You haven't recorded a contact in the last 7 days."
    noun = "account" if count == 1 else "accounts"
    return f"You contacted {count} {noun} in the last 7 days."
