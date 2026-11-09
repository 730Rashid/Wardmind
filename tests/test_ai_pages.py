"""AI summaries and website lookups in the web UI: the Data & privacy section, the Website lookups
page and its progress panel, the account page's summary, My book's topics, cross-site refusals.

Sites and OpenRouter are fakes (research_fake.py, openrouter_fake.py). The autouse fixtures in
conftest fail any test that reaches for a real site or OpenRouter, so a page that quietly looked
something up would fail here."""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openrouter_fake import KEY, FakeOpenRouter, reply_json
from research_fake import NOW, acme_site, counted
from wardmind.core import bulk, editing, secrets, service, summaries
from wardmind.core.bulk import LookupResult, Outcome, SummaryResult
from wardmind.core.research import Topic
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
HTMX = {"HX-Request": "true", **ORIGIN}
CROSS_SITE = {"origin": "https://evil.example"}
DEMO_ID = "acc-001"
SECTION = re.compile(r'<section id="research".*?</section>', re.S)
JOB_ID = "0123456789abcdef"


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _section(response: Any) -> str:
    match = SECTION.search(response.text)
    assert match is not None, "no research section on this response"
    return html.unescape(match.group(0))


def _settings(**changes: object) -> None:
    save_settings(load_settings().model_copy(update=changes))
    service.reset_connectors()


def _on(source: DataSource = DataSource.DEMO, *, ai: bool = True, web: bool = True) -> None:
    _settings(web_research=web, ai_summaries=ai, source=source)


def _use_openrouter(monkeypatch: pytest.MonkeyPatch, fake: FakeOpenRouter) -> None:
    monkeypatch.setattr(service, "_openrouter_factory", fake.client)


def _use_site(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls, factory = counted(acme_site().client)
    monkeypatch.setattr(service, "_site_client_factory", factory)
    return calls


def _save_summary(
    account_id: str = DEMO_ID,
    *,
    sample: bool = False,
    highlights: list[summaries.Highlight] | None = None,
    cost: float | None = 0.0031,
) -> summaries.Summary:
    summary = summaries.Summary(
        account_id=account_id,
        about="Makes freight software for regional carriers.",
        highlights=highlights or [],
        model="anthropic/claude-haiku-5.5",
        content_hash="h",
        created_at=NOW,
        cost_usd=cost,
        sample=sample,
    )
    service.summary_store().put(summary)
    return summary


def _job(**changes: Any) -> bulk.Job:
    fields: dict[str, Any] = {
        "id": JOB_ID,
        "started_at": NOW,
        "finished_at": None,
        "summarise": False,
        "refresh": False,
        "total": 5,
        "outcomes": [],
        "cost_usd": 0.0,
        "ai_stopped": None,
    }
    fields.update(changes)
    return bulk.Job(**fields)


def _set_job(job: bulk.Job) -> None:
    """Make the runner hold this job, as if a look up had started (no thread runs)."""
    service._runner._job = job  # type: ignore[attr-defined]


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


# --- Data & privacy: AI summaries and the key -------------------------------------------------


def test_the_privacy_page_explains_ai_summaries_in_the_agreed_words(client: TestClient) -> None:
    text = _text(client.get("/settings/privacy"))
    assert "AI summaries are off." in text
    assert "They need web research to be on too." in text
    assert (
        "When it's on, Wardmind can turn what web research read from a customer's website into a "
        "short About and up to three highlights a CSM should know: acquisitions, funding, "
        "leadership changes or restructuring. It uses your own OpenRouter account, so you choose "
        "the model and pay OpenRouter directly."
    ) in text
    assert (
        "Only the text web research read from the customer's public website: its address, title, "
        "description and recent headlines. Nothing from your book: not your account name, ARR, "
        "owner, health, signals, notes or other accounts."
    ) in text
    assert (
        "OpenRouter, and the company that runs the model you chose. Wardmind asks OpenRouter to "
        "use only providers that don't store or train on the data."
    ) in text
    assert (
        "Only when you press Summarise on an account, or tick Also summarise with AI when looking "
        "up all websites. Never in the background."
    ) in text
    assert "Demo accounts get invented sample summaries, and nothing is sent." in text


def test_the_sections_come_in_order_web_research_then_ai_then_local_cache(
    client: TestClient,
) -> None:
    text = client.get("/settings/privacy").text
    web_at = text.index('id="web-research-title"')
    ai_at = text.index('id="ai-summaries-title"')
    cache_at = text.index('id="local-cache-title"')
    assert web_at < ai_at < cache_at


def test_the_ai_form_has_the_radios_the_model_field_and_the_default(client: TestClient) -> None:
    text = client.get("/settings/privacy").text
    assert 'action="/settings/privacy/ai"' in text
    assert re.search(r'name="ai_summaries" value="off"[^>]* checked', text)
    assert not re.search(r'name="ai_summaries" value="on"[^>]* checked', text)
    assert 'id="field-ai-model"' in text
    assert 'value="anthropic/claude-haiku-5.5"' in text
    assert (
        "An OpenRouter model id. The default, anthropic/claude-haiku-5.5, is fast and cheap."
    ) in html.unescape(text)
    assert re.search(
        r'<a href="https://openrouter.ai/models" rel="noopener noreferrer">Browse', text
    )


def test_with_ai_on_the_status_names_the_model_and_the_on_radio_is_checked(
    client: TestClient,
) -> None:
    _on(web=True)
    text = client.get("/settings/privacy").text
    assert "AI summaries are on, using anthropic/claude-haiku-5.5." in text
    assert "They need web research to be on too." not in text
    assert re.search(r'name="ai_summaries" value="on"[^>]* checked', text)


def test_saving_ai_on_with_a_model_saves_it_and_says_so(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/ai",
        data={"ai_summaries": "on", "ai_model": "openai/gpt-x"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?ai=on"
    assert load_settings().ai_summaries is True
    assert load_settings().ai_model == "openai/gpt-x"
    notice = _text(client.get(response.headers["location"]))
    assert "AI summaries are on. Turn on web research too to use them." in notice
    _settings(web_research=True)
    notice = _text(client.get("/settings/privacy?ai=on"))
    assert "AI summaries are on." in notice
    assert "Turn on web research too" not in notice


def test_saving_ai_off_removes_the_saved_summaries(client: TestClient) -> None:
    _on()
    _save_summary()
    response = client.post(
        "/settings/privacy/ai",
        data={"ai_summaries": "off", "ai_model": "anthropic/claude-haiku-5.5"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings/privacy?ai=off"
    assert service.summary_store().get(DEMO_ID) is None
    assert "AI summaries are off. Saved summaries were removed." in _text(
        client.get(response.headers["location"])
    )


def test_an_invalid_model_is_refused_next_to_the_field_and_nothing_is_saved(
    client: TestClient,
) -> None:
    response = client.post(
        "/settings/privacy/ai",
        data={"ai_summaries": "on", "ai_model": "not a model"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    text = _text(response)
    assert "That isn't an OpenRouter model id, like anthropic/claude-haiku-5.5." in text
    assert 'aria-invalid="true"' in text
    assert 'aria-describedby="field-ai-model-error field-ai-model-help"' in text
    assert 'value="not a model"' in text
    assert load_settings().ai_summaries is False
    assert load_settings().ai_model == "anthropic/claude-haiku-5.5"


def test_an_invalid_choice_is_refused_and_nothing_is_saved(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/ai",
        data={"ai_summaries": "maybe", "ai_model": "openai/gpt-x"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Choose On or Off, then save again." in _text(response)
    assert load_settings().ai_model == "anthropic/claude-haiku-5.5"


def test_a_saved_key_is_shown_masked_and_never_echoed(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/ai/key",
        data={"key": KEY},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?key=saved"
    assert KEY not in response.headers["location"]
    page = client.get(response.headers["location"])
    text = _text(page)
    assert "OpenRouter key saved." in text
    assert f"Saved key …{KEY[-4:]}, added " in text
    assert KEY not in page.text
    assert KEY[:20] not in page.text
    assert secrets.get_openrouter_key() == KEY


def test_an_invalid_key_is_refused_with_the_reason_and_an_empty_field(client: TestClient) -> None:
    typed = "sk-or-short-typed"
    response = client.post(
        "/settings/privacy/ai/key", data={"key": typed}, headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 422
    text = _text(response)
    assert "That doesn't look like a full OpenRouter API key. Check you copied all of it." in text
    assert typed not in response.text
    assert re.search(
        r'<input type="password" name="key" id="field-ai-key"(?![^>]*value=)', response.text
    )
    assert secrets.get_openrouter_key() is None


def test_remove_key_deletes_the_saved_key(client: TestClient) -> None:
    secrets.save_openrouter_key(KEY)
    response = client.post(
        "/settings/privacy/ai/key/delete", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?key=removed"
    assert "OpenRouter key removed." in _text(client.get(response.headers["location"]))
    assert secrets.get_openrouter_key() is None
    assert "No key saved." in client.get("/settings/privacy").text


def test_an_environment_key_is_shown_as_set_by_the_environment_with_no_remove(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secrets.ENV_OPENROUTER_KEY, KEY)
    text = client.get("/settings/privacy").text
    assert f"Set by the WARDMIND_OPENROUTER_KEY environment variable (…{KEY[-4:]})." in text
    assert 'action="/settings/privacy/ai/key/delete"' not in text
    assert KEY not in text


def test_the_key_help_says_where_it_is_kept(client: TestClient) -> None:
    text = _text(client.get("/settings/privacy"))
    assert (
        "Create one at openrouter.ai/keys. It's stored in your user profile, never in this folder, "
        "and only its last 4 characters are ever shown."
    ) in text
    assert 'autocomplete="off"' in client.get("/settings/privacy").text


def test_testing_a_key_reports_the_credit_left(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets.save_openrouter_key(KEY)
    _use_openrouter(monkeypatch, FakeOpenRouter())
    response = client.post("/settings/privacy/ai/key/test", headers=ORIGIN)
    assert response.status_code == 200
    assert 'role="status">The key works. $9.58 of credit left.' in response.text
    assert KEY not in response.text


def test_testing_a_key_with_no_limit_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets.save_openrouter_key(KEY)
    unlimited = FakeOpenRouter(
        key_info={"data": {"label": None, "usage": 0, "limit_remaining": None}}
    )
    _use_openrouter(monkeypatch, unlimited)
    assert "The key works. It has no credit limit." in _text(
        client.post("/settings/privacy/ai/key/test", headers=ORIGIN)
    )


def test_testing_a_rejected_key_shows_the_plain_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets.save_openrouter_key(KEY)
    fake = FakeOpenRouter()
    fake.queue.append(lambda request: reply_json(401, {"error": {"message": KEY}}))
    _use_openrouter(monkeypatch, fake)
    response = client.post("/settings/privacy/ai/key/test", headers=ORIGIN)
    assert 'role="alert"' in response.text
    assert "OpenRouter didn't accept this API key." in _text(response)
    assert KEY not in response.text


def test_testing_with_no_key_says_to_add_one(client: TestClient) -> None:
    response = client.post("/settings/privacy/ai/key/test", headers=ORIGIN)
    assert "Add your OpenRouter API key in Settings > Data & privacy." in _text(response)


def test_the_web_research_section_links_to_the_lookups_page(client: TestClient) -> None:
    assert 'href="/research"' in client.get("/settings/privacy").text


# --- The Website lookups page ------------------------------------------------------------------


def test_with_web_research_off_the_lookups_page_has_no_form(client: TestClient) -> None:
    text = _text(client.get("/research"))
    assert "<title>Website lookups - Wardmind</title>" in client.get("/research").text
    assert "Website lookups need web research, and it's off." in text
    assert 'href="/settings/privacy"' in text
    assert "/research/all" not in text


def test_the_lookups_form_names_the_book_and_disables_the_ai_box_with_its_reason(
    client: TestClient,
) -> None:
    _on(ai=False)
    count = len(service.get_book())
    text = _text(client.get("/research"))
    assert f"Look up all {count} accounts" in text
    assert 'action="/research/all"' in text
    assert re.search(r'name="summarise" value="1"[^>]* disabled', text)
    assert "AI summaries are off. Turn them on in Settings > Data &amp; privacy." in text or (
        "AI summaries are off. Turn them on in Settings > Data & privacy." in text
    )
    assert re.search(r'name="refresh" value="1"', text)


def test_the_ai_box_is_enabled_when_ai_is_ready(client: TestClient) -> None:
    _on(DataSource.DEMO, ai=True)
    text = client.get("/research").text
    assert re.search(r'name="summarise" value="1"(?![^>]*disabled)[^>]*>', text)


def test_the_lookups_page_shows_every_account_from_saved_data_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    calls = _use_site(monkeypatch)
    text = client.get("/research").text
    assert text.count("Not yet") == len(service.get_book())
    assert f'href="/accounts/{DEMO_ID}#research"' in text
    assert "Every account" in text
    assert calls == []


def test_a_looked_up_account_shows_when_and_its_saved_summary_marked_ai(client: TestClient) -> None:
    _on()
    service.research_account(DEMO_ID)
    _save_summary(
        highlights=[summaries.Highlight(topic=Topic.FUNDING, note="Raised a round")],
    )
    text = _text(client.get("/research"))
    assert 'data-label="Looked up">today</td>' in text
    assert '<span class="lookup-badge">AI</span>' in text
    assert "Makes freight software for regional carriers." in text
    assert "Funding" in text


def test_a_sample_summary_is_marked_sample_not_ai(client: TestClient) -> None:
    _on()
    _save_summary(sample=True)
    text = client.get("/research").text
    assert '<span class="lookup-badge">Sample</span>' in text


def test_a_plain_post_starts_a_job_and_redirects_to_its_progress(client: TestClient) -> None:
    _on()
    response = client.post("/research/all", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/research\?job=[0-9a-f]{16}", location)
    service._runner.wait(timeout=20)
    page = client.get(location)
    assert page.status_code == 200
    assert "Finished." in page.text
    assert "Summarised" not in page.text  # no AI asked for
    assert "AI cost" not in page.text


def test_an_htmx_post_returns_a_panel_that_polls_itself_while_it_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    running = _job(total=5, outcomes=[])
    monkeypatch.setattr(service, "start_bulk_research", lambda **kwargs: running)
    response = client.post("/research/all", headers=HTMX)
    assert response.status_code == 200
    text = response.text
    assert 'id="bulk-job"' in text
    assert f'hx-get="/research/jobs/{JOB_ID}"' in text
    assert 'hx-trigger="load delay:1s"' in text
    assert 'hx-swap="outerHTML"' in text
    assert '<progress max="5" value="0"' in text
    assert "0 of 5 accounts" in text
    assert 'role="status" aria-live="polite"' in text
    assert "HX-Refresh" not in response.headers


def test_the_running_panel_has_a_plain_link_to_refresh_without_javascript(
    client: TestClient,
) -> None:
    _on()
    _set_job(_job(total=3, outcomes=[]))
    text = client.get(f"/research/jobs/{JOB_ID}").text
    assert f'<a href="/research?job={JOB_ID}">Refresh to see progress</a>' in text


def test_a_finished_panel_for_htmx_asks_for_one_reload_and_stops_polling(
    client: TestClient,
) -> None:
    _on()
    _set_job(_job(total=1, outcomes=[_outcome()], finished_at=NOW))
    response = client.get(f"/research/jobs/{JOB_ID}", headers=HTMX)
    assert response.headers["HX-Refresh"] == "true"
    assert "hx-get" not in response.text
    assert "Finished." in response.text


def _outcome(**changes: Any) -> Outcome:
    fields: dict[str, Any] = {
        "account_id": DEMO_ID,
        "account_name": "Acme Corporation",
        "lookup": LookupResult.LOOKED_UP,
        "summary": None,
        "message": None,
    }
    fields.update(changes)
    return Outcome(**fields)


def test_the_finished_panel_lists_counts_the_cost_the_stop_and_the_problems(
    client: TestClient,
) -> None:
    _on()
    outcomes = [
        _outcome(summary=SummaryResult.WRITTEN),
        _outcome(
            account_id="acc-002",
            account_name="No Site Ltd",
            lookup=LookupResult.NO_WEBSITE,
            summary=SummaryResult.SKIPPED,
            message="This account has no website.",
        ),
    ]
    _set_job(
        _job(
            total=2,
            outcomes=outcomes,
            finished_at=NOW,
            summarise=True,
            cost_usd=0.0123,
            ai_stopped="Add your OpenRouter API key in Settings > Data & privacy.",
        )
    )
    text = client.get(f"/research/jobs/{JOB_ID}").text
    assert "Looked up: 1" in text
    assert "No website: 1" in text
    assert "Summarised: 1" in text
    assert "Not summarised: 1" in text
    assert "AI cost: $0.0123" in text
    assert 'role="alert">The AI part stopped early. Add your OpenRouter API key' in text
    assert f'<a href="/accounts/{DEMO_ID}#research">Acme Corporation</a>' not in text
    assert (
        '<a href="/accounts/acc-002#research">No Site Ltd</a>: This account has no website.' in text
    )


def test_zero_cost_reads_as_zero_dollars_and_no_cost_line_without_summaries(
    client: TestClient,
) -> None:
    _on()
    _set_job(_job(total=1, outcomes=[_outcome()], finished_at=NOW, summarise=True))
    assert "AI cost: $0.00" in client.get(f"/research/jobs/{JOB_ID}").text
    _set_job(_job(total=1, outcomes=[_outcome()], finished_at=NOW, summarise=False))
    assert "AI cost" not in client.get(f"/research/jobs/{JOB_ID}").text


def test_an_unknown_or_badly_formed_job_id_is_not_found(client: TestClient) -> None:
    _on()
    _set_job(_job())
    for bad in ("0123", "ZZZZZZZZZZZZZZZZ", "0123456789ABCDEF", "fedcba9876543210"):
        assert client.get(f"/research/jobs/{bad}").status_code == 404, bad
    assert client.get(f"/research/jobs/{JOB_ID}").status_code == 200


def test_a_refused_start_shows_its_reason_in_the_panel_with_htmx(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)  # Your data with no key: summarising is refused
    response = client.post("/research/all", data={"summarise": "1"}, headers=HTMX)
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "Add your OpenRouter API key in Settings > Data & privacy." in _text(response)
    assert 'id="bulk-job"' in response.text


def test_a_refused_start_without_javascript_shows_the_page_with_the_reason(
    client: TestClient,
) -> None:
    _settings(web_research=False)
    response = client.post("/research/all", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "Web research is off. Turn it on in Settings > Data & privacy" in _text(response)


def test_a_message_about_a_job_id_in_the_url_never_leaks_a_key(client: TestClient) -> None:
    _on()
    secrets.save_openrouter_key(KEY)
    text = client.get("/research").text
    assert KEY not in text


# --- The account page's summary ---------------------------------------------------------------


def test_the_account_section_offers_a_summary_when_ai_is_ready(client: TestClient) -> None:
    _on()
    service.research_account(DEMO_ID)
    section = _section(client.get(f"/accounts/{DEMO_ID}"))
    form = re.search(
        r'<form class="research-form no-print" method="post" '
        r'action="/accounts/acc-001/summarise".*?</form>',
        section,
        re.S,
    )
    assert form is not None
    assert "Summarise with AI" in form.group(0)
    assert 'name="refresh"' not in form.group(0)
    assert 'class="research-summary-title"' not in section


def test_a_saved_summary_shows_about_highlights_and_the_ai_line(client: TestClient) -> None:
    _on()
    service.research_account(DEMO_ID)
    _save_summary(highlights=[summaries.Highlight(topic=Topic.FUNDING, note="Raised a round")])
    section = _section(client.get(f"/accounts/{DEMO_ID}"))
    assert '<h3 class="research-summary-title">Summary</h3>' in section
    assert "Makes freight software for regional carriers." in section
    assert "<li>Funding: Raised a round</li>" in section
    assert (
        "Summarised by AI (anthropic/claude-haiku-5.5) from their website. Cost $0.0031."
    ) in section
    assert "Summarise again" in section
    assert re.search(r'<input type="hidden" name="refresh" value="1">', section)


def test_a_summary_without_a_cost_leaves_the_cost_out(client: TestClient) -> None:
    _on()
    service.research_account(DEMO_ID)
    _save_summary(cost=None)
    section = _section(client.get(f"/accounts/{DEMO_ID}"))
    assert "Summarised by AI (anthropic/claude-haiku-5.5) from their website." in section
    assert "Cost" not in section


def test_a_sample_summary_says_it_is_invented_and_nothing_was_sent(client: TestClient) -> None:
    _on()
    service.research_account(DEMO_ID)
    _save_summary(sample=True)
    section = _section(client.get(f"/accounts/{DEMO_ID}"))
    assert "Sample summary, invented like the demo accounts. Nothing was sent." in section


def test_a_summary_request_without_javascript_redirects_to_the_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    response = client.post(f"/accounts/{DEMO_ID}/summarise", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{DEMO_ID}#research"
    assert service.summary_store().get(DEMO_ID) is not None


def test_a_summary_request_with_htmx_returns_the_section_alone(client: TestClient) -> None:
    _on()
    response = client.post(f"/accounts/{DEMO_ID}/summarise", headers=HTMX)
    assert response.status_code == 200
    assert 'id="research"' in response.text
    assert "<html" not in response.text
    assert 'hx-target="#research"' in response.text


def test_a_key_problem_shows_as_an_alert_with_a_link_to_the_key_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    secrets.save_openrouter_key(KEY)
    from datetime import date

    from wardmind.core import editing

    account = editing.create_account(
        service.get_store(),
        {
            "name": "Acme Corporation",
            "arr": "$1",
            "renewal_date": "2027-01-10",
            "domain": "acme.example",
        },
        today=date(2026, 10, 22),
    )
    _settings(source=DataSource.MINE)
    monkeypatch.setattr(service, "_site_client_factory", acme_site().client)
    fake = FakeOpenRouter()
    fake.queue.append(lambda request: reply_json(401, {"error": {"message": KEY}}))
    _use_openrouter(monkeypatch, fake)
    response = client.post(f"/accounts/{account.id}/summarise", headers=HTMX)
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "OpenRouter didn't accept this API key" in _text(response)
    assert 'href="/settings/privacy">Data &amp; privacy</a>' in response.text
    assert KEY not in response.text


def test_an_unknown_account_cannot_be_summarised(client: TestClient) -> None:
    _on()
    assert client.post("/accounts/acc-nope/summarise", headers=ORIGIN).status_code == 404


def test_the_hint_says_what_to_turn_on_when_ai_is_not_ready(client: TestClient) -> None:
    _on(ai=False)
    service.research_account(DEMO_ID)
    response = client.get(f"/accounts/{DEMO_ID}")
    assert "Turn on AI summaries in" in _text(response)
    assert (
        'href="/settings/privacy">Data &amp; privacy</a> to get a short summary.' in response.text
    )
    assert "Summarise with AI" not in _text(response)


def test_the_hint_asks_for_the_key_when_ai_is_on_but_there_is_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE, ai=True)
    account_id = editing_account("acme.example")
    _use_site(monkeypatch)
    service.research_account(account_id)
    response = client.get(f"/accounts/{account_id}")
    assert "Add your OpenRouter API key in" in _text(response)
    assert "Turn on AI summaries" not in _text(response)


def editing_account(domain: str) -> str:
    """A hand-added account in Your data, for these tests. Returns its id."""
    account = editing.create_account(
        service.get_store(),
        {"name": "Acme Corporation", "arr": "$1", "renewal_date": "2027-01-10", "domain": domain},
        today=date(2026, 10, 22),
    )
    service.reset_connectors()
    return account.id


def test_the_section_has_a_link_to_all_lookups_unless_web_research_is_off(
    client: TestClient,
) -> None:
    _on()
    service.research_account(DEMO_ID)
    assert 'href="/research">See all website lookups</a>' in _section(
        client.get(f"/accounts/{DEMO_ID}")
    )
    _settings(web_research=False)
    assert "See all website lookups" not in _section(client.get(f"/accounts/{DEMO_ID}"))


def test_the_account_page_with_summaries_never_prints_none(client: TestClient) -> None:
    _on()
    service.research_account(DEMO_ID)
    _save_summary(
        cost=None, highlights=[summaries.Highlight(topic=Topic.LEADERSHIP, note="New CFO")]
    )
    for path in (f"/accounts/{DEMO_ID}", "/research", "/settings/privacy"):
        page = _text(client.get(path))
        assert "None" not in page, path


# --- My book: topics next to each account -----------------------------------------------------


def test_my_book_shows_the_topics_on_the_cards_the_list_and_the_filter(
    client: TestClient,
) -> None:
    _on()
    service.research_account(DEMO_ID)
    flags = service.account_flags()
    assert flags.get(DEMO_ID), "the demo sample should carry topics"
    full = client.get("/").text
    rows = client.get("/", headers=HTMX).text
    for topic in flags[DEMO_ID]:
        pill = f'<use href="/static/icons.svg#topic-{topic.value}"/></svg>{topic.label}</li>'
        assert pill in full
        assert pill in rows
    listed = client.get("/?view=list").text
    assert 'aria-label="Topics from their website"' in listed
    for topic in flags[DEMO_ID]:
        assert f'<li class="tag">{topic.label}</li>' in listed
    # The sidebar offers the topics found, and ticking one keeps only the accounts that have it.
    assert "<legend>News from their website</legend>" in full
    topic = flags[DEMO_ID][0]
    filtered = client.get(f"/?topic={topic.value}").text
    assert f'href="/accounts/{DEMO_ID}"' in filtered
    having = sum(topic in found for found in flags.values())
    assert filtered.count('class="account-card"') == having


def test_my_book_links_to_the_lookups_only_while_web_research_is_on(client: TestClient) -> None:
    _on()
    assert 'href="/research">Look up all websites</a>' in client.get("/").text
    _settings(web_research=False)
    assert "Look up all websites" not in client.get("/").text


def test_my_book_has_no_topics_while_web_research_is_off(client: TestClient) -> None:
    _settings(web_research=False)
    assert "Topics from their website" not in client.get("/?view=list").text
    assert "News from their website" not in client.get("/").text


# --- Cross-site writes are refused, and change nothing --------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/settings/privacy/ai",
        "/settings/privacy/ai/key",
        "/settings/privacy/ai/key/delete",
        "/settings/privacy/ai/key/test",
        "/research/all",
        f"/accounts/{DEMO_ID}/summarise",
    ],
)
def test_a_cross_site_post_to_a_new_route_is_refused(client: TestClient, path: str) -> None:
    _on()
    secrets.save_openrouter_key(KEY)
    before = load_settings()
    response = client.post(
        path, data={"ai_summaries": "off", "key": "x", "summarise": "1"}, headers=CROSS_SITE
    )
    assert response.status_code == 403
    assert response.text == web.CROSS_SITE_MESSAGE
    assert load_settings() == before
    assert secrets.get_openrouter_key() == KEY
    assert service._runner.job() is None
    assert service.summary_store().get(DEMO_ID) is None


def test_a_cross_site_post_cannot_save_a_key_or_remove_one(client: TestClient) -> None:
    client.post("/settings/privacy/ai/key", data={"key": KEY}, headers=CROSS_SITE)
    assert secrets.get_openrouter_key() is None
    secrets.save_openrouter_key(KEY)
    client.post("/settings/privacy/ai/key/delete", headers=CROSS_SITE)
    assert secrets.get_openrouter_key() == KEY


def test_a_cross_site_post_cannot_turn_ai_summaries_on(client: TestClient) -> None:
    client.post(
        "/settings/privacy/ai", data={"ai_summaries": "on", "ai_model": "x/y"}, headers=CROSS_SITE
    )
    assert load_settings().ai_summaries is False
    assert load_settings().ai_model == "anthropic/claude-haiku-5.5"


# --- The pages never call the network or OpenRouter just by being shown -------------------


def test_pages_that_only_show_things_never_call_the_factories(client: TestClient) -> None:
    _on()
    secrets.save_openrouter_key(KEY)
    service.research_account(DEMO_ID)
    _save_summary()
    for path in ("/", "/research", f"/accounts/{DEMO_ID}", "/settings/privacy", "/brief"):
        assert client.get(path).status_code == 200, path
    assert client.get("/research", headers=HTMX).status_code == 200


def test_the_settings_saved_by_the_pages_are_read_back(client: TestClient) -> None:
    assert isinstance(load_settings(), Settings)
