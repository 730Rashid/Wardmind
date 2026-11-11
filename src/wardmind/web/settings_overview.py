"""Settings > Overview (a setup check, and a way to find any setting) and Settings > Profile and
region (your name, currency and date style).

Registered by create_app (web/app.py) through register(). `render` is app._render (the shared
layout and context every page needs); `templates` is the app's Jinja2Templates, for partials.
Cross-site writes never reach these routes (see same_origin_writes in app.py), and the session
check and lock gate run first.

The setup check reads what Wardmind already knows, and never changes anything. Each check is a
small function that returns a Reading (or None when it doesn't apply); one check failing shows
"Couldn't check this" and never breaks the page.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from wardmind.core import backup, books, service, vault
from wardmind.core.settings import (
    Currency,
    DataSource,
    DateStyle,
    Settings,
    load_settings,
    save_settings,
)
from wardmind.web import formatting

Render = Callable[..., HTMLResponse]
_log = logging.getLogger(__name__)

STALE_BACKUP_DAYS = 30  # a backup older than this is worth making again
MAX_FIND_CHARS = 100  # the search box's limit; a longer query is cut to this
MAX_FIND_WORDS = 8  # words beyond this are ignored
COULDNT_CHECK = "Couldn't check this."
PROFILE_SAVED = "Profile saved."
_PROFILE_FIELDS = ("your_name", "currency", "date_style")
_PROFILE_MESSAGES = {
    "currency": "Pick a currency from the list.",
    "date_style": "Pick a date style from the list.",
}


# --- The setup check ---------------------------------------------------------------------------


class CheckState(StrEnum):
    TODO = "todo"  # worth doing: listed first
    UNKNOWN = "unknown"  # the check itself failed
    BUSY = "busy"  # encryption is being turned on or off
    DONE = "done"  # in place: listed last and quieter


_MARKS = {
    CheckState.TODO: "Worth a look",
    CheckState.UNKNOWN: "Not checked",
    CheckState.BUSY: "In progress",
    CheckState.DONE: "Done",
}


@dataclass(frozen=True)
class Reading:
    """What one check found: its state, and the one sentence that says so."""

    state: CheckState
    sentence: str


@dataclass(frozen=True)
class SetupCheck:
    """One line of the setup check, ready for the page."""

    key: str
    title: str
    state: CheckState
    mark: str  # the words beside the title; the state is never shown by colour alone
    sentence: str
    href: str  # the page that changes this
    link_label: str  # the page's name, for "Go to <name>"


@dataclass(frozen=True)
class _Spec:
    """The fixed part of a check: its name, and where to change it."""

    key: str
    title: str
    href: str
    link_label: str


_ENCRYPTION = _Spec("encryption", "Encryption", "/settings/privacy", "Data & privacy")
_RECOVERY = _Spec("recovery", "Recovery key", "/settings/privacy", "Data & privacy")
_AUTO_LOCK = _Spec("auto-lock", "Auto-lock", "/settings/privacy", "Data & privacy")
_BACKUP = _Spec("backup", "Backup", "/settings/backup", "Backup & export")
_YOUR_DATA = _Spec("your-data", "Your data", "/settings/connections", "Connections")
_PROBLEMS = _Spec("connections", "Connections", "/settings/connections", "Connections")
_MAILBOX = _Spec("mailbox", "Mailbox", "/settings/mailbox", "Mailbox")
_FOLLOW_UPS = _Spec("follow-ups", "Automatic follow-ups", "/settings/follow-ups", "Follow-ups")
_NAME = _Spec("your-name", "Your name", "/settings/profile", "Profile & region")


def encryption_reading(state: vault.State) -> Reading:
    """On is done; off is worth turning on. Turning either way is shown as in progress."""
    if state is vault.State.ON:
        return Reading(CheckState.DONE, "On. Your files are encrypted on this computer.")
    if state is vault.State.ENCRYPTING:
        return Reading(CheckState.BUSY, "Turning on. Wait for it to finish.")
    if state is vault.State.DECRYPTING:
        return Reading(CheckState.BUSY, "Turning off. Wait for it to finish.")
    return Reading(
        CheckState.TODO,
        "Off. Turn it on so a copy of your files can't be read without your key.",
    )


def recovery_reading(has_recovery_key: bool) -> Reading:
    """Only asked about while encryption is on: a recovery key is the way back in."""
    if has_recovery_key:
        return Reading(CheckState.DONE, "Set. It's the way back in if you forget your password.")
    return Reading(
        CheckState.TODO,
        "Not set. Make one, so you can get back in if you forget your password.",
    )


def auto_lock_reading(minutes: int) -> Reading:
    """Asked about only while encryption is on. 0 means Wardmind never locks itself."""
    if minutes == 0:
        return Reading(
            CheckState.TODO,
            "Never locks. Choose a limit, so Wardmind locks when you step away.",
        )
    return Reading(CheckState.DONE, f"Locks after {minutes} minutes without use.")


def backup_reading(last: datetime | None, today: date) -> Reading:
    """No backup yet, or one older than STALE_BACKUP_DAYS, is worth making again."""
    if last is None:
        return Reading(CheckState.TODO, "No backup yet. Make one before anything goes wrong.")
    day = _local_day(last)
    when = formatting.long_date(day)
    if (today - day).days > STALE_BACKUP_DAYS:
        return Reading(
            CheckState.TODO,
            f"The last backup was made on {when}. Make a new one.",
        )
    return Reading(CheckState.DONE, f"Last made on {when}.")


def your_data_reading(source: DataSource, connections: int) -> Reading:
    """Demo data is invented; your data needs at least one connection to show anything."""
    if source is DataSource.DEMO:
        return Reading(
            CheckState.TODO,
            "Demo data is showing. It's invented, so connect your own data to see your book.",
        )
    if connections == 0:
        return Reading(CheckState.TODO, "Your data is showing, but nothing is connected yet.")
    return Reading(
        CheckState.DONE,
        f"Your data is showing from {_count(connections, 'connection', 'connections')}.",
    )


def problems_reading(failing: int) -> Reading:
    """Connections that failed to load on the last read. None of them failing is done."""
    if failing == 0:
        return Reading(CheckState.DONE, "Every connection loaded.")
    return Reading(
        CheckState.TODO,
        f"{_count(failing, 'connection', 'connections')} couldn't load. Check them on Connections.",
    )


def mailbox_reading(connected: bool) -> Reading:
    if connected:
        return Reading(CheckState.DONE, "Connected. Matching email is logged to each account.")
    return Reading(
        CheckState.TODO,
        "Not connected. Connect your mailbox to log email on each account.",
    )


def follow_ups_reading(settings: Settings) -> Reading:
    """The automatic follow-ups that are on, named in the order Follow-ups lists them."""
    on = [
        phrase
        for enabled, phrase in (
            (settings.rule_at_risk, "At risk accounts"),
            (settings.rule_renewal, "renewals"),
            (settings.rule_champion_left, "a champion leaving"),
        )
        if enabled
    ]
    if not on:
        return Reading(
            CheckState.TODO,
            "None are on. Wardmind can add a follow-up when an account turns At risk, "
            "nears renewal, or loses its champion.",
        )
    return Reading(CheckState.DONE, f"On for {_join(on)}.")


def name_reading(your_name: str) -> Reading:
    if your_name:
        return Reading(
            CheckState.DONE, "Set. Renewal sheets and the weekly brief show who prepared them."
        )
    return Reading(
        CheckState.TODO,
        "Not set. Add it, so renewal sheets and the weekly brief say who prepared them.",
    )


def setup_checks(today: date | None = None) -> list[SetupCheck]:
    """Every check that applies, in the order they are listed, read from the book and the
    settings. A check that can't run says COULDNT_CHECK; one that doesn't apply is left out."""
    today = today if today is not None else date.today()
    settings = load_settings()
    plan: list[tuple[_Spec, Callable[[], Reading | None]]] = [
        (_ENCRYPTION, lambda: encryption_reading(vault.state())),
        (_RECOVERY, _recovery),
        (_AUTO_LOCK, lambda: _auto_lock(settings)),
        (_BACKUP, lambda: backup_reading(backup.last_backup_at(books.active_id()), today)),
        (_YOUR_DATA, lambda: _your_data(settings)),
        (_PROBLEMS, lambda: _problems(settings)),
        (_MAILBOX, lambda: mailbox_reading(service.mailbox_settings() is not None)),
        (_FOLLOW_UPS, lambda: follow_ups_reading(settings)),
        (_NAME, lambda: name_reading(settings.your_name)),
    ]
    checks: list[SetupCheck] = []
    for spec, read in plan:
        check = _run(spec, read)
        if check is not None:
            checks.append(check)
    return checks


def view_order(checks: Sequence[SetupCheck]) -> list[SetupCheck]:
    """Worth a look first, then the checks that couldn't run, then the rest: done ones last."""
    rank = {CheckState.TODO: 0, CheckState.UNKNOWN: 1, CheckState.BUSY: 2, CheckState.DONE: 2}
    return sorted(checks, key=lambda check: rank[check.state])  # sort is stable


def summary_sentence(checks: Sequence[SetupCheck]) -> str:
    """How many things are worth a look, e.g. '2 things are worth a look. 5 are already in
    place.' With nothing to look at it says so."""
    worth = _count_state(checks, CheckState.TODO)
    unknown = _count_state(checks, CheckState.UNKNOWN)
    done = _count_state(checks, CheckState.DONE)
    sentences: list[str] = []
    if worth == 0 and unknown == 0:
        sentences.append("Nothing needs a look.")
    if worth:
        sentences.append(f"{worth} {_word(worth, 'thing is worth', 'things are worth')} a look.")
    if done:
        sentences.append(f"{done} {_word(done, 'is', 'are')} already in place.")
    if unknown:
        sentences.append(f"{unknown} {_word(unknown, 'check', 'checks')} couldn't run.")
    return " ".join(sentences)


def _run(spec: _Spec, read: Callable[[], Reading | None]) -> SetupCheck | None:
    """One check, with its failure contained: an error becomes "Couldn't check this"."""
    try:
        reading = read()
    except Exception as error:  # one check failing must never break the page
        _log.warning("A setup check (%s) couldn't run: %s", spec.key, type(error).__name__)
        reading = Reading(CheckState.UNKNOWN, COULDNT_CHECK)
    if reading is None:
        return None
    return SetupCheck(
        key=spec.key,
        title=spec.title,
        state=reading.state,
        mark=_MARKS[reading.state],
        sentence=reading.sentence,
        href=spec.href,
        link_label=spec.link_label,
    )


def _recovery() -> Reading | None:
    """Asked only while encryption is on."""
    if vault.state() is not vault.State.ON:
        return None
    keys = vault.load_keys()
    has_key = keys is not None and keys.slot(vault.SlotKind.RECOVERY) is not None
    return recovery_reading(has_key)


def _auto_lock(settings: Settings) -> Reading | None:
    """Asked only while encryption is on."""
    if vault.state() is not vault.State.ON:
        return None
    return auto_lock_reading(settings.auto_lock_minutes)


def _your_data(settings: Settings) -> Reading:
    if settings.source is DataSource.DEMO:
        return your_data_reading(DataSource.DEMO, 0)
    enabled = [config for config in service.get_store().connections() if config.enabled]
    return your_data_reading(DataSource.MINE, len(enabled))


def _problems(settings: Settings) -> Reading | None:
    """Only the connections of your data can fail to load; demo data has none."""
    if settings.source is not DataSource.MINE:
        return None
    combined = service.get_combined()
    return problems_reading(0 if combined is None else len(combined.problems))


def _local_day(moment: datetime) -> date:
    """The local calendar day of a moment. A naive one is taken as UTC, as the log stores it."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().date()


def _count_state(checks: Sequence[SetupCheck], state: CheckState) -> int:
    return sum(1 for check in checks if check.state is state)


# --- Find a setting ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingEntry:
    """One setting a person can change, and where it is. `words` are extra words people might
    search for ("dark mode", "pounds"); the title and section are searched too."""

    title: str
    section: str
    href: str
    words: tuple[str, ...] = ()


# The fixed index: one entry per setting, across every Settings page. A page that gains a setting
# adds its entry here (tests check that every Settings page has at least one).
SETTINGS_INDEX: tuple[SettingEntry, ...] = (
    SettingEntry(
        "Your name",
        "Profile & region",
        "/settings/profile",
        ("prepared by", "name", "renewal sheet", "weekly brief", "signature"),
    ),
    SettingEntry(
        "Currency",
        "Profile & region",
        "/settings/profile",
        ("money", "symbol", "pounds", "dollars", "euro", "yen", "rupee", "$", "£", "€", "¥", "₹"),
    ),
    SettingEntry(
        "Date style",
        "Profile & region",
        "/settings/profile",
        ("date", "dates", "format", "day", "month", "year", "iso", "calendar"),
    ),
    SettingEntry(
        "Theme",
        "Appearance",
        "/settings/appearance",
        ("dark mode", "light mode", "dark", "light", "night", "system", "computer"),
    ),
    SettingEntry(
        "Accent colour",
        "Appearance",
        "/settings/appearance",
        ("color", "plum", "blue", "petrol", "graphite", "highlight"),
    ),
    SettingEntry(
        "Density",
        "Appearance",
        "/settings/appearance",
        ("compact", "comfortable", "spacing", "rows", "dense"),
    ),
    SettingEntry(
        "Opens on",
        "Appearance",
        "/settings/appearance",
        ("start", "startup", "home", "landing", "today", "brief", "my book"),
    ),
    SettingEntry(
        "Default due date",
        "Follow-ups",
        "/settings/follow-ups",
        ("follow-up", "follow up", "due", "days", "reminder"),
    ),
    SettingEntry(
        "Skip weekends",
        "Follow-ups",
        "/settings/follow-ups",
        ("weekend", "saturday", "sunday", "monday", "due"),
    ),
    SettingEntry(
        "Quiet days",
        "Follow-ups",
        "/settings/follow-ups",
        ("quiet", "needs attention", "contacted", "recently"),
    ),
    SettingEntry(
        "Reply wait",
        "Follow-ups",
        "/settings/follow-ups",
        ("reply", "unanswered", "email", "waiting", "sent"),
    ),
    SettingEntry(
        "Answer wait",
        "Follow-ups",
        "/settings/follow-ups",
        ("answer", "unanswered", "email", "waiting", "received"),
    ),
    SettingEntry(
        "Automatic follow-ups for at risk accounts",
        "Follow-ups",
        "/settings/follow-ups",
        ("automatic", "rule", "rules", "at risk", "check in"),
    ),
    SettingEntry(
        "Automatic follow-ups before renewals",
        "Follow-ups",
        "/settings/follow-ups",
        ("automatic", "rule", "rules", "renewal", "prepare"),
    ),
    SettingEntry(
        "Automatic follow-ups when a champion leaves",
        "Follow-ups",
        "/settings/follow-ups",
        ("automatic", "rule", "rules", "champion", "left", "new champion"),
    ),
    SettingEntry(
        "Note templates",
        "Note templates",
        "/settings/note-templates",
        ("template", "templates", "add template", "edit template", "note", "text"),
    ),
    SettingEntry(
        "Scoring weights",
        "Scoring",
        "/settings/scoring",
        ("signals", "weights", "weighting", "score", "importance"),
    ),
    SettingEntry(
        "Health thresholds",
        "Scoring",
        "/settings/scoring",
        ("health bands", "bands", "at risk", "watch", "healthy", "threshold", "thresholds"),
    ),
    SettingEntry(
        "Motion windows",
        "Scoring",
        "/settings/scoring",
        ("motion", "window", "windows", "renewal window", "onboarding", "churn"),
    ),
    SettingEntry("Add a book", "Books", "/settings/books", ("new book", "create", "add")),
    SettingEntry("Rename a book", "Books", "/settings/books", ("name", "label", "rename")),
    SettingEntry("Delete a book", "Books", "/settings/books", ("remove", "delete")),
    SettingEntry("Switch book", "Books", "/settings/books", ("open", "switch", "change")),
    SettingEntry(
        "Data source",
        "Connections",
        "/settings/connections",
        ("demo", "sample", "invented", "your data", "source", "show"),
    ),
    SettingEntry(
        "HubSpot access",
        "Connections",
        "/settings/connections",
        ("hubspot", "token", "access", "key", "crm", "connect"),
    ),
    SettingEntry(
        "Spreadsheet connections",
        "Connections",
        "/settings/connections",
        ("spreadsheet", "csv", "excel", "file", "import", "sheet"),
    ),
    SettingEntry(
        "Data feeds",
        "Connections",
        "/settings/connections",
        ("feed", "feeds", "api", "api key", "web"),
    ),
    SettingEntry(
        "Mailbox",
        "Mailbox",
        "/settings/mailbox",
        ("email", "mail", "inbox", "imap", "password", "sync", "connect"),
    ),
    SettingEntry(
        "Back up",
        "Backup & export",
        "/settings/backup",
        ("backup", "back up", "save", "copy", "password", "download"),
    ),
    SettingEntry(
        "Restore a backup",
        "Backup & export",
        "/settings/backup",
        ("restore", "backup", "import", "open", "new book"),
    ),
    SettingEntry(
        "Export accounts as CSV",
        "Backup & export",
        "/settings/backup",
        ("export", "csv", "accounts", "spreadsheet", "download", "excel"),
    ),
    SettingEntry(
        "Export notes as CSV",
        "Backup & export",
        "/settings/backup",
        ("export", "csv", "notes", "journal", "download"),
    ),
    SettingEntry(
        "Web research",
        "Data & privacy",
        "/settings/privacy",
        ("website", "lookup", "look up", "research", "news", "visit"),
    ),
    SettingEntry(
        "AI summaries",
        "Data & privacy",
        "/settings/privacy",
        ("ai", "summary", "summaries", "summarise", "openrouter", "model"),
    ),
    SettingEntry(
        "Encryption",
        "Data & privacy",
        "/settings/privacy",
        ("encrypt", "encrypted", "protect", "security", "lock", "privacy"),
    ),
    SettingEntry(
        "Password",
        "Data & privacy",
        "/settings/privacy",
        ("encryption", "change password", "set password", "lock"),
    ),
    SettingEntry(
        "Recovery key",
        "Data & privacy",
        "/settings/privacy",
        ("recovery", "key", "forgot", "lost", "new key"),
    ),
    SettingEntry(
        "Auto-lock",
        "Data & privacy",
        "/settings/privacy",
        ("lock", "idle", "timeout", "minutes", "inactive", "never"),
    ),
    SettingEntry(
        "Clear local cache",
        "Data & privacy",
        "/settings/privacy",
        ("cache", "clear", "saved", "lookups", "summaries", "temporary"),
    ),
    SettingEntry(
        "Delete all local data",
        "Data & privacy",
        "/settings/privacy",
        ("delete", "erase", "remove", "reset", "wipe", "everything"),
    ),
    SettingEntry(
        "Activity log",
        "Activity log",
        "/settings/activity",
        ("log", "history", "audit", "events", "changes"),
    ),
)

_WORD = re.compile(r"[\w$£€¥₹]+")


def find_settings(query: str) -> list[SettingEntry]:
    """The settings whose title, section or words match every word typed, in index order.

    A typed word matches the start of any word in the entry, so "pass" finds "password", and
    case doesn't matter. Only the first MAX_FIND_CHARS characters are read, and only the first
    MAX_FIND_WORDS words. A query with no words in it finds nothing."""
    wanted = _words(query[:MAX_FIND_CHARS])[:MAX_FIND_WORDS]
    if not wanted:
        return []
    return [
        entry for entry in SETTINGS_INDEX if all(_entry_matches(word, entry) for word in wanted)
    ]


def _entry_matches(word: str, entry: SettingEntry) -> bool:
    text = " ".join((entry.title, entry.section, *entry.words))
    return any(token.startswith(word) for token in _words(text))


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


# --- Profile & region --------------------------------------------------------------------------


def profile_errors(error: ValidationError) -> dict[str, str]:
    """The message for each profile field that failed validation. A name's message is the one
    Settings wrote for it (its length or its characters)."""
    errors: dict[str, str] = {}
    for item in error.errors():
        field = str(item["loc"][0]) if item["loc"] else ""
        if field not in _PROFILE_FIELDS:
            continue
        if field == "your_name":
            cause = item.get("ctx", {}).get("error")
            errors.setdefault(field, str(cause) if cause is not None else "That name isn't valid.")
        else:
            errors.setdefault(field, _PROFILE_MESSAGES[field])
    return errors


def _text(value: object) -> str:
    """A posted field as text. A file where text was expected counts as blank."""
    return value if isinstance(value, str) else ""


def _join(phrases: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(phrases) <= 1:
        return "".join(phrases)
    return f"{', '.join(phrases[:-1])} and {phrases[-1]}"


def _count(count: int, singular: str, plural: str) -> str:
    return f"{count} {_word(count, singular, plural)}"


def _word(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


# --- Pages -------------------------------------------------------------------------------------


def register(app: FastAPI, render: Render, templates: Jinja2Templates) -> None:
    """Add this section's routes to `app`."""

    @app.middleware("http")
    async def display_choices(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Money and dates use the choices in Profile & region for the whole request. This runs
        before the page does, so a figure a route works out before it renders (My book's summary,
        say) is written the same way as the rest of the page. Static files are skipped."""
        if not request.url.path.startswith("/static/"):
            current = await run_in_threadpool(load_settings)
            formatting.use_display(current.currency, current.date_style)
        return await call_next(request)

    @app.get("/settings")
    def settings_home() -> RedirectResponse:
        """Settings opens on Overview."""
        return RedirectResponse("/settings/overview", status_code=303)

    @app.get("/settings/overview")
    def overview(request: Request, q: str = "") -> HTMLResponse:
        """The setup check, and the find box. ?q= lists the matching settings."""
        query = q[:MAX_FIND_CHARS].strip()
        checks = setup_checks()
        return render(
            request,
            "settings/overview.html",
            page="Settings",
            active="settings",
            section="overview",
            checks=view_order(checks),
            summary=summary_sentence(checks),
            find_query=query,
            searched=bool(query),
            matches=find_settings(query) if query else [],
        )

    @app.get("/settings/profile")
    def profile(request: Request, saved: str = "") -> HTMLResponse:
        """The profile form. ?saved=1 shows the notice for a save that just happened."""
        current = load_settings()
        return _profile_page(
            render,
            request,
            values={
                "your_name": current.your_name,
                "currency": current.currency.value,
                "date_style": current.date_style.value,
            },
            errors={},
            notice=PROFILE_SAVED if saved == "1" else "",
        )

    @app.post("/settings/profile")
    async def save_profile(request: Request) -> Response:
        """Save your name, currency and date style. A value that isn't valid re-renders the form
        with a 422 and writes nothing. Other settings are kept as they are."""
        form = await request.form()
        typed = {field: _text(form.get(field)) for field in _PROFILE_FIELDS}
        current = await run_in_threadpool(load_settings)
        try:
            checked = Settings.model_validate({**current.model_dump(), **typed})
        except ValidationError as error:
            errors = profile_errors(error)
            # The name is kept as typed so it can be fixed; a choice that isn't valid goes back
            # to the saved one.
            values = {
                "your_name": typed["your_name"],
                "currency": current.currency.value if "currency" in errors else typed["currency"],
                "date_style": current.date_style.value
                if "date_style" in errors
                else typed["date_style"],
            }
            return await run_in_threadpool(
                _profile_page,
                render,
                request,
                values=values,
                errors=errors,
                status_code=422,
            )
        await run_in_threadpool(save_settings, checked)
        formatting.use_display(checked.currency, checked.date_style)
        return RedirectResponse("/settings/profile?saved=1", status_code=303)


def _profile_page(
    render: Render,
    request: Request,
    *,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    notice: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The Profile & region form. Each date style is shown with today's date."""
    today = date.today()
    return render(
        request,
        "settings/profile.html",
        page="Profile & region",
        active="settings",
        section="profile",
        values=dict(values),
        errors=dict(errors),
        notice=notice,
        status_code=status_code,
        currencies=[(choice.value, choice.label, choice.symbol) for choice in Currency],
        date_styles=[(style.value, formatting.long_date_in(today, style)) for style in DateStyle],
    )
