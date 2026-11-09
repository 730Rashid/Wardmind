"""AI summaries in the service: what is ready, saving and reusing summaries, the account flags, the
look-up-everything job, the cache and the model and key checks. Sites are fake (research_fake.py)
and OpenRouter is fake (openrouter_fake.py); nothing reaches either."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from openrouter_fake import ANSWER, KEY, FakeOpenRouter, reply_json, research_text
from research_fake import NOW, FakeSite, acme_site, counted, reply
from wardmind.core import bulk, editing, research, secrets, service, summaries
from wardmind.core.bulk import LookupResult, SummaryResult
from wardmind.core.research import Article, SiteResearch, Topic
from wardmind.core.settings import DEFAULT_AI_MODEL, DataSource, load_settings, save_settings

TODAY = date(2026, 10, 22)
DEMO_ACCOUNT = "acc-001"


def _settings(**changes: object) -> None:
    save_settings(load_settings().model_copy(update=changes))
    service.reset_connectors()


def _on(source: DataSource = DataSource.DEMO, *, web: bool = True, ai: bool = True) -> None:
    _settings(web_research=web, ai_summaries=ai, source=source)


def _your_account(domain: str | None = "acme.example", name: str = "Acme Corporation") -> str:
    account = editing.create_account(
        service.get_store(),
        {"name": name, "arr": "$120,000", "renewal_date": "2027-01-10", "domain": domain or ""},
        today=TODAY,
    )
    _settings(source=DataSource.MINE)
    return account.id


def _use_site(monkeypatch: pytest.MonkeyPatch, site: FakeSite) -> list[int]:
    calls, factory = counted(site.client)
    monkeypatch.setattr(service, "_site_client_factory", factory)
    return calls


def _use_openrouter(monkeypatch: pytest.MonkeyPatch, fake: FakeOpenRouter) -> None:
    monkeypatch.setattr(service, "_openrouter_factory", fake.client)


def _store() -> summaries.SummaryStore:
    return service.summary_store()


def _article(title: str, published: date | None, topics: list[Topic]) -> Article:
    return Article(
        title=title, url=f"https://acme.example/{title[:8]}", published=published, topics=topics
    )


def _saved_lookup(
    account_id: str, articles: list[Article], *, website: str = "acme.example"
) -> None:
    research.ResearchCache(service.get_store().root / research.RESEARCH_DIR).put(
        SiteResearch(
            account_id=account_id,
            website=website,
            fetched_at=datetime.now(UTC),
            site_title="Acme",
            site_description="Acme builds things.",
            feed_url=None,
            articles=articles,
        )
    )


def _saved_summary(
    account_id: str, highlights: list[summaries.Highlight], model: str = DEFAULT_AI_MODEL
) -> None:
    _store().put(
        summaries.Summary(
            account_id=account_id,
            about="About.",
            highlights=highlights,
            model=model,
            content_hash="h",
            created_at=NOW,
        )
    )


# --- Whether AI summaries can be used --------------------------------------------------------


def test_the_reason_names_the_first_thing_to_turn_on() -> None:
    _on(web=False)
    assert service.ai_ready() == "Turn on web research first, in Settings > Data & privacy."
    _on(ai=False)
    assert service.ai_ready() == "AI summaries are off. Turn them on in Settings > Data & privacy."


def test_your_data_needs_a_key_and_demo_data_does_not() -> None:
    _on(DataSource.MINE)
    assert service.ai_ready() == "Add your OpenRouter API key in Settings > Data & privacy."
    secrets.save_openrouter_key(KEY)
    assert service.ai_ready() is None


def test_an_environment_key_counts_as_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(DataSource.MINE)
    monkeypatch.setenv(secrets.ENV_OPENROUTER_KEY, KEY)
    assert service.ai_ready() is None


def test_demo_data_is_ready_without_a_key() -> None:
    _on(DataSource.DEMO)
    assert service.ai_ready() is None


def test_saved_summary_is_none_when_ai_or_research_is_off() -> None:
    _on()
    _saved_summary(DEMO_ACCOUNT, [])
    assert service.saved_summary(DEMO_ACCOUNT) is not None
    _settings(ai_summaries=False)
    assert service.saved_summary(DEMO_ACCOUNT) is None
    _settings(ai_summaries=True, web_research=False)
    assert service.saved_summary(DEMO_ACCOUNT) is None


# --- summarise_account -------------------------------------------------------------------------


def test_a_demo_summary_is_invented_saved_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(DataSource.DEMO)
    made: list[int] = []
    real = summaries.sample_summary

    def counted_sample(account, found, now):  # type: ignore[no-untyped-def]
        made.append(1)
        return real(account, found, now)

    monkeypatch.setattr(summaries, "sample_summary", counted_sample)
    summary = service.summarise_account(DEMO_ACCOUNT)
    assert summary.sample is True and summary.model == "sample"
    assert service.saved_summary(DEMO_ACCOUNT) == summary
    service.summarise_account(DEMO_ACCOUNT)
    assert made == [1]  # the second one reuses the saved sample
    service.summarise_account(DEMO_ACCOUNT, refresh=True)
    assert made == [1, 1]


def test_a_summary_is_written_once_and_reused_while_the_text_and_model_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    account_id = _your_account()
    _use_site(monkeypatch, acme_site())
    fake = FakeOpenRouter()
    _use_openrouter(monkeypatch, fake)

    first = service.summarise_account(account_id)
    assert first.sample is False
    assert first.about == ANSWER["about"]
    assert first.cost_usd == pytest.approx(0.0031)
    assert len(fake.requests) == 1
    assert service.saved_summary(account_id) == first

    again = service.summarise_account(account_id)
    assert again == first
    assert len(fake.requests) == 1  # nothing sent again

    refreshed = service.summarise_account(account_id, refresh=True)
    assert refreshed.about == ANSWER["about"]
    assert len(fake.requests) == 2


def test_a_changed_website_text_is_summarised_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    account_id = _your_account()
    site = acme_site()
    _use_site(monkeypatch, site)
    fake = FakeOpenRouter()
    _use_openrouter(monkeypatch, fake)
    service.summarise_account(account_id)
    assert len(fake.requests) == 1

    # The site changes its title; the lookup is refreshed, so the text the model reads changes.
    changed = acme_site(
        **{
            "https://acme.example/": reply(
                b"<html><head><title>Acme Corporation | New home</title></head><body/></html>"
            )
        }
    )
    _use_site(monkeypatch, changed)
    service.research_account(account_id, refresh=True)
    service.summarise_account(account_id)
    assert len(fake.requests) == 2
    assert "New home" in research_text(service.cached_research(account_id))


def test_a_different_model_is_summarised_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    account_id = _your_account()
    _use_site(monkeypatch, acme_site())
    fake = FakeOpenRouter()
    _use_openrouter(monkeypatch, fake)
    service.summarise_account(account_id)
    service.set_ai_model("openai/gpt-x")
    service.summarise_account(account_id)
    assert len(fake.requests) == 2
    assert fake.bodies[-1]["model"] == "openai/gpt-x"
    assert service.saved_summary(account_id).model == "openai/gpt-x"  # type: ignore[union-attr]


def test_your_data_without_a_key_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account()
    calls = _use_site(monkeypatch, acme_site())
    with pytest.raises(summaries.NoKey) as caught:
        service.summarise_account(account_id)
    assert caught.value.message == "Add your OpenRouter API key in Settings > Data & privacy."
    assert calls == []


def test_summarising_with_ai_off_is_refused_with_the_reason() -> None:
    _on(ai=False)
    with pytest.raises(summaries.SummariesOff) as caught:
        service.summarise_account(DEMO_ACCOUNT)
    assert "AI summaries are off" in caught.value.message


def test_a_website_that_cannot_be_read_is_reported_not_summarised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    account_id = _your_account(domain=None)
    fake = FakeOpenRouter()
    _use_openrouter(monkeypatch, fake)
    with pytest.raises(research.NoWebsite):
        service.summarise_account(account_id)
    assert fake.requests == []


def test_a_rejected_key_comes_back_from_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    account_id = _your_account()
    _use_site(monkeypatch, acme_site())
    fake = FakeOpenRouter()
    fake.queue.append(lambda request: reply_json(401, {}))
    _use_openrouter(monkeypatch, fake)
    with pytest.raises(summaries.KeyRejected):
        service.summarise_account(account_id)
    assert service.saved_summary(account_id) is None


# --- Topics next to My book -------------------------------------------------------------------


def test_no_flags_when_web_research_is_off() -> None:
    _on(web=False)
    _saved_summary(DEMO_ACCOUNT, [summaries.Highlight(topic=Topic.FUNDING, note="x")])
    assert service.account_flags() == {}


def test_a_summarys_highlight_topics_are_preferred_to_the_articles() -> None:
    _on(DataSource.MINE)
    account_id = _your_account()
    _saved_lookup(account_id, [_article("Acme names a new CTO", date.today(), [Topic.LEADERSHIP])])
    _saved_summary(account_id, [summaries.Highlight(topic=Topic.FUNDING, note="Raised")])
    assert service.account_flags() == {account_id: [Topic.FUNDING]}


def test_without_a_summary_the_recent_article_topics_show_in_label_order() -> None:
    _on(DataSource.MINE)
    account_id = _your_account()
    _saved_lookup(
        account_id,
        [
            _article("Acme raises a round", date.today() - timedelta(days=3), [Topic.FUNDING]),
            _article("Acme acquires Beta", date.today() - timedelta(days=10), [Topic.ACQUISITION]),
        ],
    )
    assert service.account_flags() == {account_id: [Topic.ACQUISITION, Topic.FUNDING]}


def test_articles_older_than_ninety_days_and_undated_ones_are_ignored() -> None:
    _on(DataSource.MINE)
    account_id = _your_account()
    _saved_lookup(
        account_id,
        [
            _article("Acme acquires old", date.today() - timedelta(days=200), [Topic.ACQUISITION]),
            _article("Acme undated news", None, [Topic.RESTRUCTURING]),
        ],
    )
    assert service.account_flags() == {}


def test_with_ai_summaries_off_the_summary_is_ignored() -> None:
    _on(DataSource.MINE, ai=False)
    account_id = _your_account()
    _saved_lookup(account_id, [_article("Acme names a new CTO", date.today(), [Topic.LEADERSHIP])])
    _store().put(
        summaries.Summary(
            account_id=account_id,
            about="About.",
            highlights=[summaries.Highlight(topic=Topic.FUNDING, note="x")],
            model=DEFAULT_AI_MODEL,
            content_hash="h",
            created_at=NOW,
        )
    )
    assert service.account_flags() == {account_id: [Topic.LEADERSHIP]}


def test_a_demo_lookup_gives_its_sample_topics_to_my_book(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(DataSource.DEMO)
    assert service.account_flags() == {}
    service.research_account(DEMO_ACCOUNT)
    flags = service.account_flags()
    assert flags.get(DEMO_ACCOUNT)
    assert all(isinstance(topic, Topic) for topic in flags[DEMO_ACCOUNT])


# --- Looking up every website ---------------------------------------------------------------


def test_a_bulk_job_needs_web_research_and_the_ai_reason_when_it_summarises() -> None:
    _on(web=False)
    with pytest.raises(research.ResearchOff):
        service.start_bulk_research(summarise=False, refresh=False)
    _on(ai=False)
    with pytest.raises(summaries.SummariesOff) as caught:
        service.start_bulk_research(summarise=True, refresh=False)
    assert (
        caught.value.message == "AI summaries are off. Turn them on in Settings > Data & privacy."
    )
    _on(DataSource.MINE)
    with pytest.raises(summaries.NoKey):
        service.start_bulk_research(summarise=True, refresh=False)


def test_a_demo_run_looks_up_every_account_in_book_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(DataSource.DEMO)
    book = [item.account.id for item in service.get_book()]
    job = service.start_bulk_research(summarise=False, refresh=False)
    assert job.total == len(book)
    final = _runner_wait()
    assert final is not None and final.finished_at is not None
    assert [o.account_id for o in final.outcomes] == book
    assert {o.lookup for o in final.outcomes} == {LookupResult.LOOKED_UP}
    assert all(o.summary is None for o in final.outcomes)
    # A second run reuses what was saved a moment ago, sending nothing.
    service.start_bulk_research(summarise=False, refresh=False)
    again = _runner_wait()
    assert again is not None
    assert {o.lookup for o in again.outcomes} == {LookupResult.REUSED}


def test_a_demo_run_that_summarises_writes_samples_without_a_key() -> None:
    _on(DataSource.DEMO)
    book = [item.account.id for item in service.get_book()]
    service.start_bulk_research(summarise=True, refresh=False)
    final = _runner_wait()
    assert final is not None
    assert final.ai_stopped is None
    assert final.cost_usd == 0.0
    assert {o.summary for o in final.outcomes} == {SummaryResult.WRITTEN}
    assert all(service.saved_summary(account_id) is not None for account_id in book)


def test_a_your_data_run_looks_each_site_up_and_summarises_the_ones_that_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    acme = _your_account("acme.example", "Acme Corporation")
    nowhere = _your_account(None, "No Site Ltd")
    blocked = _your_account("203.0.113.5", "Blocked Ltd")
    missing = _your_account("beta.example", "Beta Ltd")  # not in the fake site: unreachable
    _use_site(monkeypatch, acme_site())
    fake = FakeOpenRouter()
    _use_openrouter(monkeypatch, fake)

    service.start_bulk_research(summarise=True, refresh=False)
    final = _runner_wait()
    assert final is not None
    by_id = {o.account_id: o for o in final.outcomes}
    assert by_id[acme].lookup is LookupResult.LOOKED_UP
    assert by_id[acme].summary is SummaryResult.WRITTEN
    assert by_id[nowhere].lookup is LookupResult.NO_WEBSITE
    assert by_id[nowhere].summary is SummaryResult.SKIPPED
    assert by_id[blocked].lookup is LookupResult.BLOCKED
    assert by_id[missing].lookup is LookupResult.FAILED
    assert by_id[missing].summary is SummaryResult.SKIPPED
    assert final.cost_usd == pytest.approx(0.0031)
    assert len(fake.requests) == 1  # only the site that could be read was sent


def test_a_bulk_run_with_no_credit_stops_the_ai_and_keeps_looking_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    _your_account("acme.example", "Acme Corporation")
    _your_account("acme.example", "Acme Two")
    _use_site(monkeypatch, acme_site())
    fake = FakeOpenRouter()
    fake.queue.append(lambda request: reply_json(402, {}))
    _use_openrouter(monkeypatch, fake)
    service.start_bulk_research(summarise=True, refresh=False)
    final = _runner_wait()
    assert final is not None
    assert (
        final.ai_stopped
        == "This OpenRouter key has no credit left. Add credit at openrouter.ai, then try again."
    )
    assert [o.lookup for o in final.outcomes] == [LookupResult.LOOKED_UP] * 2


def _runner_wait() -> bulk.Job | None:
    return service._runner.wait(timeout=20)


# --- Clear local cache, switching off, the model and the key -----------------------------------


def test_clear_local_cache_counts_the_saved_summaries() -> None:
    _on()
    _saved_summary(DEMO_ACCOUNT, [])
    _saved_summary("acc-002", [])
    cleared = service.clear_local_cache()
    assert cleared.summaries == 2
    assert service.saved_summary(DEMO_ACCOUNT) is None
    assert service.clear_local_cache().summaries == 0


def test_turning_ai_summaries_off_removes_every_saved_summary() -> None:
    _on()
    _saved_summary(DEMO_ACCOUNT, [])
    service.set_ai_summaries(False)
    assert _store().get(DEMO_ACCOUNT) is None
    assert load_settings().ai_summaries is False


def test_a_model_is_checked_and_saved_and_keeps_the_summaries() -> None:
    _on()
    _saved_summary(DEMO_ACCOUNT, [])
    with pytest.raises(ValueError) as caught:
        service.set_ai_model("not a model")
    assert (
        caught.value.args[0]
        == "That isn't an OpenRouter model id, like anthropic/claude-haiku-5.5."
    )
    assert load_settings().ai_model == DEFAULT_AI_MODEL
    service.set_ai_model("  openai/gpt-x  ")
    assert load_settings().ai_model == "openai/gpt-x"
    assert _store().get(DEMO_ACCOUNT) is not None


def test_the_model_check_saves_nothing() -> None:
    _on()
    assert service.checked_ai_model(" anthropic/claude-haiku-5.5 ") == "anthropic/claude-haiku-5.5"
    with pytest.raises(ValueError):
        service.checked_ai_model("oops")
    assert load_settings().ai_model == DEFAULT_AI_MODEL


def test_testing_the_key_without_one_says_to_add_it() -> None:
    _on(DataSource.MINE)
    with pytest.raises(summaries.NoKey) as caught:
        service.check_openrouter_key()
    assert caught.value.message == "Add your OpenRouter API key in Settings > Data & privacy."


def test_testing_the_key_returns_what_openrouter_says(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.save_openrouter_key(KEY)
    fake = FakeOpenRouter()
    _use_openrouter(monkeypatch, fake)
    info = service.check_openrouter_key()
    assert info.limit_remaining_usd == pytest.approx(9.58)
    assert len(fake.requests) == 1


def test_the_key_is_never_in_an_error_the_service_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.save_openrouter_key(KEY)
    fake = FakeOpenRouter()
    fake.queue.append(lambda request: reply_json(401, {"error": {"message": KEY}}))
    _use_openrouter(monkeypatch, fake)
    with pytest.raises(summaries.KeyRejected) as caught:
        service.check_openrouter_key()
    assert KEY not in caught.value.message
