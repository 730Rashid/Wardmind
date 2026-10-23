"""The look-up-everything job runner, with plain functions in place of the network and the AI.
Threads are controlled with events and a barrier, so the tests don't depend on timing."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from cs_brief.core import bulk
from cs_brief.core.bulk import JobRunner, LookupResult, SummaryResult
from cs_brief.core.models import Account
from cs_brief.core.research import BlockedWebsite, NoWebsite, ResearchError, SiteResearch
from cs_brief.core.summaries import (
    AIUnavailable,
    KeyRejected,
    NoKey,
    OutOfCredit,
    Summary,
)
from research_fake import NOW
from research_fake import account as make_account


def _accounts(count: int) -> list[Account]:
    return [
        make_account(account_id=f"acc-{index}", name=f"Name {index}", domain=f"s{index}.example")
        for index in range(count)
    ]


def _lookup(account: Account) -> SiteResearch:
    return SiteResearch(
        account_id=account.id,
        website=f"{account.id}.example",
        fetched_at=NOW,
        site_title=None,
        site_description=None,
        feed_url=None,
        articles=[],
    )


def _summary(account: Account, cost: float | None = None) -> Summary:
    return Summary(
        account_id=account.id,
        about="About.",
        highlights=[],
        model="m/x",
        content_hash="h",
        created_at=NOW,
        cost_usd=cost,
    )


def _ok_research(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
    return _lookup(account), False


def _ok_summary(account: Account, found: SiteResearch) -> tuple[Summary, bool]:
    return _summary(account), False


def _runner() -> JobRunner:
    return JobRunner(clock=lambda: NOW)


def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the job")
        time.sleep(0.005)


def _by_id(job: Any) -> dict[str, Any]:
    return {outcome.account_id: outcome for outcome in job.outcomes}


# --- Order, concurrency and failures ----------------------------------------------------------


def test_outcomes_stay_in_book_order_however_the_lookups_finish() -> None:
    runner = _runner()
    accounts = _accounts(3)
    release_first = threading.Event()

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        if account.id == "acc-0":
            release_first.wait(timeout=5)  # the first account finishes last
        return _lookup(account), False

    runner.start(accounts, research_one, None, refresh=False)
    _until(lambda: (job := runner.job()) is not None and job.done == 2)
    partial = runner.job()
    assert partial is not None and [o.account_id for o in partial.outcomes] == ["acc-1", "acc-2"]
    release_first.set()
    final = runner.wait(timeout=10)
    assert final is not None
    assert [o.account_id for o in final.outcomes] == ["acc-0", "acc-1", "acc-2"]
    assert final.total == 3 and final.done == 3


def test_accounts_are_looked_up_at_the_same_time() -> None:
    runner = _runner()
    barrier = threading.Barrier(2, timeout=5)

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        if account.id in ("acc-0", "acc-1"):
            barrier.wait()  # only passes when both are running together
        return _lookup(account), False

    runner.start(_accounts(2), research_one, None, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert [o.lookup for o in final.outcomes] == [LookupResult.LOOKED_UP] * 2


def test_each_research_error_maps_to_its_lookup_result_and_message() -> None:
    runner = _runner()
    errors: dict[str, Exception] = {
        "acc-0": NoWebsite("No website here."),
        "acc-1": BlockedWebsite("Points inside."),
        "acc-2": ResearchError("Couldn't read it."),
        "acc-3": RuntimeError("secret internals"),
    }

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        if account.id in errors:
            raise errors[account.id]
        return _lookup(account), False

    runner.start(_accounts(5), research_one, None, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    by_id = _by_id(final)
    assert by_id["acc-0"].lookup is LookupResult.NO_WEBSITE
    assert by_id["acc-0"].message == "No website here."
    assert by_id["acc-1"].lookup is LookupResult.BLOCKED
    assert by_id["acc-1"].message == "Points inside."
    assert by_id["acc-2"].lookup is LookupResult.FAILED
    assert by_id["acc-2"].message == "Couldn't read it."
    assert by_id["acc-3"].lookup is LookupResult.FAILED
    assert "secret internals" not in (by_id["acc-3"].message or "")
    assert by_id["acc-4"].lookup is LookupResult.LOOKED_UP


def test_a_bug_in_one_account_does_not_stop_the_others() -> None:
    runner = _runner()

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        if account.id == "acc-1":
            raise KeyError("oops")
        return _lookup(account), False

    runner.start(_accounts(3), research_one, _ok_summary, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.done == 3
    by_id = _by_id(final)
    assert by_id["acc-1"].lookup is LookupResult.FAILED
    assert by_id["acc-1"].summary is SummaryResult.SKIPPED
    assert by_id["acc-0"].summary is SummaryResult.WRITTEN
    assert by_id["acc-2"].summary is SummaryResult.WRITTEN


def test_a_reused_lookup_is_reported_as_reused() -> None:
    runner = _runner()

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        return _lookup(account), account.id == "acc-0"

    runner.start(_accounts(2), research_one, None, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert _by_id(final)["acc-0"].lookup is LookupResult.REUSED
    assert _by_id(final)["acc-1"].lookup is LookupResult.LOOKED_UP


def test_refresh_is_passed_to_every_lookup_and_kept_on_the_job() -> None:
    runner = _runner()
    seen: list[bool] = []

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        seen.append(refresh)
        return _lookup(account), False

    job = runner.start(_accounts(2), research_one, None, refresh=True)
    runner.wait(timeout=10)
    assert job.refresh is True
    assert seen == [True, True]


# --- The AI part ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [NoKey("No key saved."), KeyRejected("Key rejected."), OutOfCredit("No credit left.")],
)
def test_a_key_problem_stops_the_ai_part_and_the_rest_are_skipped(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(bulk, "MAX_SITE_WORKERS", 1)  # one at a time: the order is fixed
    runner = _runner()
    calls: list[str] = []

    def summarise_one(account: Account, found: SiteResearch) -> tuple[Summary, bool]:
        calls.append(account.id)
        raise error

    runner.start(_accounts(4), _ok_research, summarise_one, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.ai_stopped == error.message
    outcomes = final.outcomes
    assert outcomes[0].summary is SummaryResult.FAILED
    assert outcomes[0].message == error.message
    for outcome in outcomes[1:]:
        assert outcome.summary is SummaryResult.SKIPPED
        assert outcome.message == error.message
    assert calls == ["acc-0"]  # nothing after the first problem asked OpenRouter
    assert all(o.lookup is LookupResult.LOOKED_UP for o in outcomes)


def test_another_summary_error_fails_only_its_account() -> None:
    runner = _runner()

    def summarise_one(account: Account, found: SiteResearch) -> tuple[Summary, bool]:
        if account.id == "acc-0":
            raise AIUnavailable("OpenRouter didn't answer in time. Try again in a minute.")
        return _summary(account), False

    runner.start(_accounts(2), _ok_research, summarise_one, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.ai_stopped is None
    by_id = _by_id(final)
    assert by_id["acc-0"].summary is SummaryResult.FAILED
    assert by_id["acc-0"].message == "OpenRouter didn't answer in time. Try again in a minute."
    assert by_id["acc-1"].summary is SummaryResult.WRITTEN


def test_only_written_summaries_add_to_the_cost() -> None:
    runner = _runner()
    costs = {"acc-0": (0.02, False), "acc-1": (0.5, True), "acc-2": (None, False)}

    def summarise_one(account: Account, found: SiteResearch) -> tuple[Summary, bool]:
        cost, reused = costs[account.id]
        return _summary(account, cost), reused

    runner.start(_accounts(3), _ok_research, summarise_one, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.cost_usd == pytest.approx(0.02)
    by_id = _by_id(final)
    assert by_id["acc-1"].summary is SummaryResult.REUSED
    assert by_id["acc-2"].summary is SummaryResult.WRITTEN


def test_an_account_without_a_lookup_is_not_summarised_and_says_why() -> None:
    runner = _runner()
    asked: list[str] = []

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        if account.id == "acc-0":
            raise NoWebsite("This account has no website.")
        return _lookup(account), False

    def summarise_one(account: Account, found: SiteResearch) -> tuple[Summary, bool]:
        asked.append(account.id)
        return _summary(account), False

    runner.start(_accounts(2), research_one, summarise_one, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    by_id = _by_id(final)
    assert by_id["acc-0"].summary is SummaryResult.SKIPPED
    assert by_id["acc-0"].message == "This account has no website."
    assert asked == ["acc-1"]


def test_without_summarising_every_summary_result_is_none() -> None:
    runner = _runner()
    job = runner.start(_accounts(2), _ok_research, None, refresh=False)
    final = runner.wait(timeout=10)
    assert job.summarise is False
    assert final is not None
    assert [o.summary for o in final.outcomes] == [None, None]


# --- One job at a time, copies and waiting -----------------------------------------------------


def test_a_second_start_while_a_job_runs_returns_that_job_and_starts_nothing() -> None:
    runner = _runner()
    release = threading.Event()
    calls: list[str] = []

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        calls.append(account.id)
        release.wait(timeout=5)
        return _lookup(account), False

    first = runner.start(_accounts(2), research_one, None, refresh=False)
    second = runner.start(_accounts(3), research_one, None, refresh=False)
    assert second.id == first.id
    assert second.total == 2  # the running job, not the new list
    release.set()
    runner.wait(timeout=10)
    assert sorted(calls) == ["acc-0", "acc-1"]


def test_a_new_job_can_start_once_the_last_one_has_finished() -> None:
    runner = _runner()
    first = runner.start(_accounts(1), _ok_research, None, refresh=False)
    runner.wait(timeout=10)
    second = runner.start(_accounts(1), _ok_research, None, refresh=False)
    assert second.id != first.id
    runner.wait(timeout=10)


def test_a_job_id_is_sixteen_hex_characters() -> None:
    job = _runner().start([], _ok_research, None, refresh=False)
    assert len(job.id) == 16
    assert all(char in "0123456789abcdef" for char in job.id)


def test_job_returns_copies_that_cannot_change_the_runner() -> None:
    runner = _runner()
    release = threading.Event()

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        release.wait(timeout=5)
        return _lookup(account), False

    runner.start(_accounts(1), research_one, None, refresh=False)
    snapshot = runner.job()
    assert snapshot is not None
    snapshot.total = 999
    snapshot.outcomes.clear()
    release.set()
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.total == 1
    assert final.done == 1


def test_job_only_answers_for_its_own_id() -> None:
    runner = _runner()
    assert runner.job() is None
    job = runner.start([], _ok_research, None, refresh=False)
    runner.wait(timeout=10)
    assert runner.job("0" * 16) is None
    found = runner.job(job.id)
    assert found is not None and found.id == job.id


def test_wait_returns_the_finished_job_with_its_finish_time() -> None:
    runner = _runner()
    runner.start(_accounts(2), _ok_research, _ok_summary, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.running is False
    assert final.finished_at == NOW
    assert final.started_at == NOW
    assert final.summarise is True


def test_an_empty_book_finishes_straight_away() -> None:
    runner = _runner()
    runner.start([], _ok_research, _ok_summary, refresh=False)
    final = runner.wait(timeout=10)
    assert final is not None
    assert final.total == 0 and final.done == 0
    assert final.finished_at == NOW
    assert final.cost_usd == 0.0


def test_a_job_has_no_finish_time_while_it_runs() -> None:
    runner = _runner()
    release = threading.Event()

    def research_one(account: Account, refresh: bool) -> tuple[SiteResearch, bool]:
        release.wait(timeout=5)
        return _lookup(account), False

    job = runner.start(_accounts(1), research_one, None, refresh=False)
    assert job.running is True
    current = runner.job()
    assert current is not None and current.running is True
    release.set()
    assert runner.wait(timeout=10) is not None
