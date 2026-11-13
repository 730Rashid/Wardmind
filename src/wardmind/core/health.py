"""Trust checks: how current the data is, and whether the background work is doing its job.

checks(now, today) -> list[Check]: every check for Settings > System status, in the order the page
lists them. Each Check has a state (ok, warn or fail), a plain-English detail, when it last worked,
and the page that fixes it. A check that can't be read says so (warn) rather than breaking the page.

freshness(now, today) -> Freshness: how current Your data is, from its enabled timed feeds. The
page's freshness line is worded from it (web/status_pages.py).

banner(now, today) -> list[Problem]: the few problems that matter on every page: an unreadable
journal, a feed failing for more than a day, and background checks that have stopped.

Nothing here changes anything, and nothing is fetched from the network (the AI check reads the
settings only). Demo data is invented, so its book checks say so and are ok. While the book is
locked, the only check is the lock, since the files can't be read.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from wardmind.core import history, journal, mailbox, rules, runlog, service, vault
from wardmind.core.feeds.models import FeedStatus
from wardmind.core.settings import DataSource, Settings, load_settings
from wardmind.core.store import JOURNAL_FILE, ConnectionConfig, ConnectionKind, LocalStore
from wardmind.core.summaries import SummariesOff

SCHEDULER_WARN = timedelta(minutes=3)  # no tick for this long: needs a look
SCHEDULER_FAIL = timedelta(minutes=15)  # no tick for this long: the background checks have stopped
OVERDUE_INTERVALS = 2  # a timed feed with no success for this many intervals is overdue
BANNER_AFTER_FAILING = timedelta(days=1)  # a feed failing for longer than this is on every page
JOURNAL_COPIES = "journal.unreadable-*.json"  # what journal.py sets a damaged file aside as
UNLOCK_PATH = "/unlock"
STATUS_PATH = "/settings/status"
CONNECTIONS_PATH = "/settings/connections"
MAILBOX_PATH = "/settings/mailbox"
PRIVACY_PATH = "/settings/privacy"
BACKUP_PATH = "/settings/backup"
FOLLOW_UPS_PATH = "/settings/follow-ups"
TODAY_PATH = "/today"
_log = logging.getLogger(__name__)
_cache_lock = threading.Lock()
_journal_cache: dict[tuple[str, int, int], bool] = {}


class State(StrEnum):
    OK = "ok"  # working
    WARN = "warn"  # needs a look
    FAIL = "fail"  # not working


@dataclass(frozen=True)
class Check:
    """One line of System status. `last_success` is naive UTC, or None when there is none."""

    name: str
    state: State
    detail: str
    last_success: datetime | None = None
    fix_url: str | None = None


@dataclass(frozen=True)
class Source:
    """A timed feed as the freshness line sees it."""

    label: str
    last_success: datetime | None
    overdue: bool


@dataclass(frozen=True)
class Freshness:
    """How current Your data is. `as_of` is the oldest last successful sync among the timed feeds,
    or None when there are none or one has never synced. `sources` is empty when there are no timed
    feeds, which means there is no freshness line."""

    as_of: datetime | None
    sources: list[Source]


@dataclass(frozen=True)
class Problem:
    """A problem for the banner on every page: one sentence, and the page that helps."""

    name: str
    message: str
    fix_url: str


# --- Times ---------------------------------------------------------------------------------------


def _local(moment: datetime) -> datetime:
    """A naive value is taken as UTC, as the store keeps them."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone()


def stamp(moment: datetime, today: date) -> str:
    """'14:05 today', or 'Tue 11 Nov, 14:05' for an earlier day, in this computer's time."""
    local = _local(moment)
    if local.date() == today:
        return f"{local:%H:%M} today"
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"


def day_words(moment: datetime, today: date) -> str:
    """'today', 'yesterday' or 'Tue 11 Nov', in this computer's time."""
    day = _local(moment).date()
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{day:%a} {day.day} {day:%b}"


# --- Entry points --------------------------------------------------------------------------------


def checks(now: datetime, today: date | None = None) -> list[Check]:
    """Every check for System status, in the order the page lists them. Never raises."""
    day = today if today is not None else date.today()
    try:
        if not vault.ensure_unlocked():
            return [
                Check(
                    "Your data",
                    State.WARN,
                    "Wardmind is locked. Unlock it to check your data.",
                    fix_url=UNLOCK_PATH,
                )
            ]
        settings = load_settings()
    except Exception as exc:
        _log.warning("The checks couldn't start (%s).", type(exc).__name__)
        return [Check("Your data", State.WARN, "Couldn't check this.")]
    found: list[Check] = []
    found.extend(_guarded("Background checks", lambda: _scheduler(now, day)))
    if settings.source is DataSource.DEMO:
        found.extend(_demo_checks())
    else:
        store = service.get_store()
        found.extend(_guarded("Your notes", lambda: [_journal_check(store)]))
        found.extend(_guarded("Connections", lambda: _connection_checks(store, now, day)))
        found.extend(_guarded("Score history", lambda: _snapshot_check(store, day)))
        found.extend(_guarded("Automatic follow-ups", lambda: _daily_checks(store, day, settings)))
        found.extend(_guarded("Mailbox", lambda: _mailbox_checks(store, day)))
    found.extend(_guarded("AI summaries", lambda: [_ai_check()]))
    return found


def freshness(now: datetime, today: date | None = None) -> Freshness:
    """How current Your data is, from its enabled timed feeds. Demo data and a book with no timed
    feeds have none (empty sources). Never raises."""
    empty = Freshness(as_of=None, sources=[])
    try:
        if not vault.ensure_unlocked():
            return empty
        if load_settings().source is DataSource.DEMO:
            return empty
        timed = [config for config in _feed_configs(service.get_store()) if _timed(config)]
        if not timed:
            return empty
        sources = [
            Source(config.name, _status(config).last_synced_at, feed_overdue(config, now))
            for config in timed
        ]
        stamps = [source.last_success for source in sources]
        as_of = None if any(item is None for item in stamps) else min(stamps)
        return Freshness(as_of=as_of, sources=sources)
    except Exception as exc:
        _log.warning("The data's freshness couldn't be read (%s).", type(exc).__name__)
        return empty


def banner(now: datetime, today: date | None = None) -> list[Problem]:
    """The problems every page shows, in a fixed order. Empty while the book is locked, and for
    the parts that don't apply to demo data. Never raises."""
    day = today if today is not None else date.today()
    try:
        if not vault.ensure_unlocked():
            return []
        problems: list[Problem] = []
        if load_settings().source is not DataSource.DEMO:
            store = service.get_store()
            if _journal_readable(store.root / JOURNAL_FILE) is False:
                problems.append(
                    Problem(
                        "Your notes",
                        "Your notes and follow-ups couldn't be read, so they show as empty.",
                        BACKUP_PATH,
                    )
                )
            for config in _feed_configs(store):
                status = _status(config)
                since = status.first_failed_at or status.last_attempt_at
                if status.last_error is None or since is None:
                    continue
                if now - since > BANNER_AFTER_FAILING:
                    problems.append(
                        Problem(
                            config.name,
                            f"{config.name} hasn't synced for more than a day. {status.last_error}",
                            CONNECTIONS_PATH,
                        )
                    )
        scheduler = _scheduler_check(now, day)
        if scheduler is not None and scheduler.state is State.FAIL:
            problems.append(
                Problem(
                    "Background checks",
                    "Background checks have stopped, so accounts and scores may be out of date. "
                    "Restart Wardmind.",
                    STATUS_PATH,
                )
            )
        return problems
    except Exception as exc:
        _log.warning("The status banner couldn't be read (%s).", type(exc).__name__)
        return []


def feed_overdue(config: ConnectionConfig, now: datetime) -> bool:
    """Whether a timed feed has gone OVERDUE_INTERVALS intervals without a success. ON_CHANGE and
    MANUAL feeds are never overdue (their syncs aren't on a clock). A feed that has never synced is
    measured from when it was created."""
    feed = config.feed
    if feed is None or not config.enabled or feed.schedule.minutes is None:
        return False
    reference = feed.status.last_synced_at or config.created_at
    return now - reference > timedelta(minutes=OVERDUE_INTERVALS * feed.schedule.minutes)


# --- Groups of checks ----------------------------------------------------------------------------


def _guarded(name: str, read: Callable[[], list[Check]]) -> list[Check]:
    """One group of checks, with its failure contained: it says it couldn't be checked."""
    try:
        return read()
    except Exception as exc:
        _log.warning("A check (%s) couldn't run (%s).", name, type(exc).__name__)
        return [Check(name, State.WARN, "Couldn't check this.")]


def _demo_checks() -> list[Check]:
    return [
        Check("Your notes", State.OK, "Demo data. Its notes are kept apart from your own."),
        Check(
            "Score history",
            State.OK,
            "Demo data. Its history is worked out from the sample readings.",
        ),
        Check("Connections", State.OK, "Demo data is showing, so your connections aren't in use."),
    ]


def _scheduler(now: datetime, day: date) -> list[Check]:
    check = _scheduler_check(now, day)
    return [] if check is None else [check]


def _scheduler_check(now: datetime, day: date) -> Check | None:
    """The background checks' state, or None when this process doesn't run them (the CLI, the MCP
    server, tests): that is not a failure."""
    beat = runlog.heartbeat()
    if beat.started_at is None:
        return None
    since = beat.last_tick or beat.started_at
    age = now - since
    minutes = int(age.total_seconds() // 60)
    if age >= SCHEDULER_FAIL:
        return Check(
            "Background checks",
            State.FAIL,
            f"No check for over {minutes} minutes. Restart Wardmind.",
            last_success=beat.last_tick,
        )
    if age >= SCHEDULER_WARN:
        return Check(
            "Background checks",
            State.WARN,
            f"No check for {minutes} minutes. Restart Wardmind if this continues.",
            last_success=beat.last_tick,
        )
    if beat.last_tick is None:
        return Check("Background checks", State.OK, "Starting. The first check runs now.")
    return Check(
        "Background checks",
        State.OK,
        f"Running. Last check {stamp(beat.last_tick, day)}.",
        last_success=beat.last_tick,
    )


def _journal_readable(path: Path) -> bool | None:
    """True when the journal reads, False when it doesn't, None when there is no file yet. The
    answer is kept for the file's time and size, so a page that asks on every load reads it once."""
    try:
        info = path.stat()
    except OSError:
        return None
    key = (str(path), info.st_mtime_ns, info.st_size)
    with _cache_lock:
        cached = _journal_cache.get(key)
    if cached is not None:
        return cached
    try:
        journal.JournalData.model_validate_json(vault.read_text(path))
        readable = True
    except (OSError, ValueError, vault.Damaged):
        readable = False
    with _cache_lock:
        _journal_cache.clear()
        _journal_cache[key] = readable
    return readable


def _journal_check(store: LocalStore) -> Check:
    readable = _journal_readable(store.root / JOURNAL_FILE)
    if readable is False:
        return Check(
            "Your notes",
            State.FAIL,
            "Your notes and follow-ups couldn't be read, so they show as empty. Restore a recent "
            "backup from Backup & export.",
            fix_url=BACKUP_PATH,
        )
    copies = sorted(store.root.glob(JOURNAL_COPIES))
    if copies:
        more = len(copies) - 1
        extra = "" if more == 0 else f" and {more} older {'copy' if more == 1 else 'copies'}"
        return Check(
            "Your notes",
            State.WARN,
            f"Notes from a journal that couldn't be read were set aside as {copies[-1].name}"
            f"{extra}. Open that file in a text editor to find the notes you need, or restore a "
            "backup from Backup & export.",
            fix_url=BACKUP_PATH,
        )
    if readable is None:
        return Check("Your notes", State.OK, "No notes yet.")
    return Check("Your notes", State.OK, "Readable.")


def _feed_configs(store: LocalStore) -> list[ConnectionConfig]:
    """The enabled feed connections, with their settings."""
    return [
        config
        for config in store.connections()
        if config.kind is ConnectionKind.FEED and config.enabled and config.feed is not None
    ]


def _status(config: ConnectionConfig) -> FeedStatus:
    feed = config.feed
    assert feed is not None  # only feeds from _feed_configs reach here
    return feed.status


def _timed(config: ConnectionConfig) -> bool:
    feed = config.feed
    return feed is not None and feed.schedule.minutes is not None


def _connection_checks(store: LocalStore, now: datetime, day: date) -> list[Check]:
    return [_feed_check(config, now, day) for config in _feed_configs(store)]


def _feed_check(config: ConnectionConfig, now: datetime, day: date) -> Check:
    feed = config.feed
    assert feed is not None  # only feeds from _feed_configs reach here
    status = feed.status
    schedule = feed.schedule.label
    if status.last_error is not None:
        since = status.first_failed_at or status.last_attempt_at or now
        return Check(
            config.name,
            State.FAIL,
            f"{status.last_error} Failing since {stamp(since, day)}.",
            last_success=status.last_synced_at,
            fix_url=CONNECTIONS_PATH,
        )
    if feed_overdue(config, now):
        reference = status.last_synced_at or config.created_at
        return Check(
            config.name,
            State.WARN,
            f"No sync since {stamp(reference, day)}. Schedule: {schedule}.",
            last_success=status.last_synced_at,
            fix_url=CONNECTIONS_PATH,
        )
    if status.last_synced_at is None:
        return Check(
            config.name,
            State.OK,
            f"Not synced yet. Schedule: {schedule}.",
            fix_url=CONNECTIONS_PATH,
        )
    return Check(
        config.name,
        State.OK,
        f"Synced {stamp(status.last_synced_at, day)}. Schedule: {schedule}.",
        last_success=status.last_synced_at,
        fix_url=CONNECTIONS_PATH,
    )


def _snapshot_check(store: LocalStore, day: date) -> list[Check]:
    if not any(config.enabled for config in store.connections()):
        return []
    saved = history.HistoryStore(store).load(day)
    last_ok = runlog.last_success("snapshot", store=store)
    last_success = last_ok.finished_at if last_ok is not None else None
    if saved is not None:
        return [
            Check(
                "Score history",
                State.OK,
                f"Recorded {stamp(saved.taken_at, day)}.",
                last_success=saved.taken_at,
            )
        ]
    last = runlog.last("snapshot", store=store)
    if last is not None and not last.ok:
        return [
            Check(
                "Score history",
                State.FAIL,
                f"Today's scores couldn't be recorded. {last.detail}",
                last_success=last_success,
                fix_url=STATUS_PATH,
            )
        ]
    return [
        Check(
            "Score history",
            State.WARN,
            "Not recorded yet today. Wardmind records it while it's open.",
            last_success=last_success,
        )
    ]


def _daily_checks(store: LocalStore, day: date, settings: Settings) -> list[Check]:
    """Automatic follow-ups and suggestions: each runs once a day, after the score history."""
    found: list[Check] = []
    if rules.any_rule_on(settings):
        found.append(_daily_check(store, "rules", "Automatic follow-ups", day, FOLLOW_UPS_PATH))
    if settings.suggestions:
        found.append(_daily_check(store, "suggestions", "Suggestions", day, TODAY_PATH))
    return found


def _daily_check(store: LocalStore, job: str, name: str, day: date, fix_url: str) -> Check:
    last = runlog.last(job, store=store)
    last_ok = runlog.last_success(job, store=store)
    last_success = last_ok.finished_at if last_ok is not None else None
    if last_ok is not None and last_ok.day == day:
        return Check(name, State.OK, f"Ran {stamp(last_ok.finished_at, day)}.", last_success)
    if last is not None and not last.ok:
        return Check(
            name,
            State.WARN,
            f"The last run failed. {last.detail}",
            last_success=last_success,
            fix_url=fix_url,
        )
    if history.HistoryStore(store).load(day) is None:
        return Check(
            name,
            State.OK,
            "Runs once today's scores are recorded.",
            last_success=last_success,
        )
    return Check(
        name,
        State.WARN,
        "Hasn't run for today yet.",
        last_success=last_success,
        fix_url=fix_url,
    )


def _mailbox_checks(store: LocalStore, day: date) -> list[Check]:
    settings = mailbox.load_settings(store)
    if settings is None:
        return []
    last_ok = settings.last_success_at
    if not settings.enabled:
        detail = "Paused. Matching email isn't being logged."
        return [Check("Mailbox", State.OK, detail, last_ok, MAILBOX_PATH)]
    if settings.last_error:
        detail = f"The last check failed. {settings.last_error}"
        return [Check("Mailbox", State.FAIL, detail, last_ok, MAILBOX_PATH)]
    if last_ok is None:
        return [Check("Mailbox", State.OK, "Not checked yet.", fix_url=MAILBOX_PATH)]
    return [Check("Mailbox", State.OK, f"Checked {stamp(last_ok, day)}.", last_ok, MAILBOX_PATH)]


def _ai_check() -> Check:
    problem = service.ai_problem()
    if problem is None:
        return Check("AI summaries", State.OK, "Ready.", fix_url=PRIVACY_PATH)
    if isinstance(problem, SummariesOff):
        return Check(
            "AI summaries",
            State.OK,
            "Off. Summaries are written only when you turn them on.",
            fix_url=PRIVACY_PATH,
        )
    return Check("AI summaries", State.WARN, problem.message, fix_url=PRIVACY_PATH)
