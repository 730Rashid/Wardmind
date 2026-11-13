"""The run log (core/runlog.py): each background job's records, kept per book, the plain-English
reason a failed run gives (never another error's text), the activity-log entry for a failed job
once a day, a log that can't be written never stopping the job, and the scheduler's heartbeat.

Runs against a temp store (conftest). The activity log is the real one, in the temp secrets
folder."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from wardmind.core import audit, runlog, service, vault
from wardmind.core import store as store_module
from wardmind.core.feeds.http import FeedError
from wardmind.core.store import LocalStore


@pytest.fixture
def store(tmp_path) -> LocalStore:
    root = tmp_path / "data"
    root.mkdir(parents=True)
    return LocalStore(root)


@pytest.fixture(autouse=True)
def scheduler_not_running() -> None:
    runlog.scheduler_stopped()
    yield
    runlog.scheduler_stopped()


def _failures() -> list[audit.Entry]:
    return [entry for entry in audit.entries() if entry.event is audit.Event.BACKGROUND_FAILED]


def test_a_run_is_recorded_with_its_counts_and_the_day_it_was_for(store: LocalStore) -> None:
    with runlog.run("rules", day=date(2026, 11, 12), store=store) as job:
        job.counts.update({"added": 2, "held_back": 1})
    last = runlog.last("rules", store=store)
    assert last is not None
    assert last.ok is True
    assert last.detail == ""
    assert last.counts == {"added": 2, "held_back": 1}
    assert last.day == date(2026, 11, 12)
    assert last.finished_at >= last.started_at
    assert runlog.last_success("rules", store=store) == last


def test_each_job_keeps_its_newest_thirty_runs(store: LocalStore) -> None:
    for index in range(35):
        with runlog.run("snapshot", store=store) as job:
            job.counts["n"] = index
    with runlog.run("rules", store=store):
        pass
    kept = runlog.recent("snapshot", limit=100, store=store)
    assert runlog.RUNS_KEPT == 30
    assert len(kept) == 30
    assert kept[0].counts["n"] == 34  # newest first
    assert kept[-1].counts["n"] == 5
    assert len(runlog.recent("rules", limit=100, store=store)) == 1  # its own count
    assert runlog.jobs(store=store) == ["rules", "snapshot"]


def test_a_block_that_raises_is_recorded_as_failed_and_the_error_still_propagates(
    store: LocalStore,
) -> None:
    with pytest.raises(RuntimeError, match="the disk said"):
        with runlog.run("suggestions", store=store):
            raise RuntimeError("the disk said C:/Users/Dana/Acme.csv")
    last = runlog.last("suggestions", store=store)
    assert last is not None and last.ok is False
    assert last.detail == "Unexpected error: RuntimeError."
    assert "Dana" not in last.detail and "Acme" not in last.detail
    assert runlog.last_success("suggestions", store=store) is None


def test_wardminds_own_message_is_kept_as_the_reason(store: LocalStore) -> None:
    with pytest.raises(FeedError):
        with runlog.run("feed:feed-1", store=store):
            raise FeedError("The web address didn't answer.")
    last = runlog.last("feed:feed-1", store=store)
    assert last is not None
    assert last.detail == "The web address didn't answer."


def test_a_run_can_fail_without_raising(store: LocalStore) -> None:
    with runlog.run("feed:feed-2", store=store) as job:
        job.counts["rows"] = 0
        job.fail("The source sent no rows.")
    last = runlog.last("feed:feed-2", store=store)
    assert last is not None
    assert last.ok is False
    assert last.detail == "The source sent no rows."
    assert last.counts == {"rows": 0}


def test_a_log_that_cannot_be_written_never_stops_the_job(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(vault, "write_text", full_disk)
    with runlog.run("housekeeping", store=store) as job:  # must not raise
        job.counts["left"] = 1
    with pytest.raises(ValueError, match="the job's own error"):
        with runlog.run("housekeeping", store=store):
            raise ValueError("the job's own error")


def test_a_failed_job_goes_to_the_activity_log_once_a_day(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(3):
        with pytest.raises(RuntimeError):
            with runlog.run("suggestions", store=store):
                raise RuntimeError("first")
    with pytest.raises(RuntimeError):
        with runlog.run("feed:feed-9", store=store):
            raise RuntimeError("second")
    details = sorted(entry.detail for entry in _failures())
    assert details == ["feed sync", "suggestions"]  # one each, not one per failure
    assert all(entry.book == "default" for entry in _failures())

    tomorrow = date.today() + timedelta(days=1)

    class NextDay(date):
        @classmethod
        def today(cls) -> date:
            return tomorrow

    monkeypatch.setattr(runlog, "date", NextDay)
    with pytest.raises(RuntimeError):
        with runlog.run("suggestions", store=store):
            raise RuntimeError("again")
    assert sorted(entry.detail for entry in _failures()) == [
        "feed sync",
        "suggestions",
        "suggestions",
    ]


def test_a_success_never_reaches_the_activity_log(store: LocalStore) -> None:
    before = len(_failures())
    with runlog.run("mailbox", store=store):
        pass
    assert len(_failures()) == before


def test_a_damaged_or_missing_file_reads_as_no_runs(store: LocalStore) -> None:
    assert runlog.recent("rules", store=store) == []
    assert runlog.last("rules", store=store) is None
    (store.root / runlog.RUNLOG_FILE).write_text("{not json", encoding="utf-8")
    assert runlog.recent("rules", store=store) == []
    assert runlog.last_success("rules", store=store) is None


def test_the_run_log_is_one_of_the_files_clear_all_removes(store: LocalStore) -> None:
    assert runlog.RUNLOG_FILE in store_module._ALL_FILES
    with runlog.run("rules", store=store):
        pass
    store.clear_all()
    assert not (store.root / runlog.RUNLOG_FILE).exists()


def test_the_scheduler_heartbeat_is_only_set_while_the_scheduler_runs_here() -> None:
    assert runlog.heartbeat().started_at is None
    started = datetime(2026, 11, 12, 14, 0)
    runlog.scheduler_started(started)
    assert runlog.heartbeat() == runlog.Heartbeat(started_at=started, last_tick=None)
    tick = started + timedelta(minutes=1)
    runlog.beat(tick)
    assert runlog.heartbeat().last_tick == tick
    runlog.scheduler_stopped()
    assert runlog.heartbeat().started_at is None


def test_the_web_app_and_the_service_share_one_store_for_runs(store: LocalStore) -> None:
    """The default store is the open book's data folder, so the service's and the module's reads
    agree without a store argument."""
    with runlog.run("housekeeping"):
        pass
    assert runlog.last("housekeeping") is not None
    assert runlog.last("housekeeping") == runlog.last("housekeeping", store=service.get_store())
