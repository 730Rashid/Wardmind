"""AI summaries written by a model on this computer (Ollama, LM Studio and the like): the address
rule, the client, the service and the Data & privacy page.

The model server is a fake on an httpx.MockTransport; conftest fails any test that reaches for a
real one, so nothing here opens a connection."""

from __future__ import annotations

import html
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from openrouter_fake import ANSWER, FakeOpenRouter, completion
from research_fake import NOW, acme_site, counted
from wardmind.core import editing, service, summaries
from wardmind.core.research import SiteResearch
from wardmind.core.settings import (
    DEFAULT_LOCAL_AI_URL,
    AIProvider,
    DataSource,
    Settings,
    check_local_url,
    load_settings,
    save_settings,
)
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
MODEL = "llama3.2"


class FakeLocalModel:
    """A model server on this computer: answers /models and /chat/completions, and keeps every
    request it was sent. `chat` replaces the chat answer when set."""

    def __init__(self, names: list[str] | None = None) -> None:
        self.names = names if names is not None else [MODEL, "qwen2.5:7b"]
        self.requests: list[httpx.Request] = []
        self.chat: Any = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": name} for name in self.names]})
        if callable(self.chat):
            return self.chat(request)
        return httpx.Response(200, json=completion(ANSWER, cost=None))

    def client(self, base_url: str = DEFAULT_LOCAL_AI_URL) -> summaries.LocalModelClient:
        return summaries.LocalModelClient(base_url, transport=httpx.MockTransport(self.handle))


def _settings(**changes: object) -> None:
    save_settings(load_settings().model_copy(update=changes))
    service.reset_connectors()


def _local(monkeypatch: pytest.MonkeyPatch, fake: FakeLocalModel, *, model: str = MODEL) -> None:
    _settings(
        web_research=True,
        ai_summaries=True,
        ai_provider=AIProvider.LOCAL,
        local_ai_model=model,
    )
    monkeypatch.setattr(service, "_local_factory", fake.client)


def _your_account() -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": "Acme Corporation",
            "arr": "$120,000",
            "renewal_date": "2027-01-10",
            "domain": "acme.example",
        },
        today=NOW.date(),
    )
    _settings(source=DataSource.MINE)
    return account.id


def _use_site(monkeypatch: pytest.MonkeyPatch) -> None:
    _calls, factory = counted(acme_site().client)
    monkeypatch.setattr(service, "_site_client_factory", factory)


# The address rule ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1/",
        "https://127.0.0.1:8443",
        "http://[::1]:11434/v1",
    ],
)
def test_an_address_on_this_computer_is_accepted(url: str) -> None:
    assert check_local_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "http://127.0.0.1.example.com/v1",
        "http://localhost.example.com",
        "http://user:pass@127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/v1?x=1",
        "ftp://127.0.0.1/v1",
        "http://192.168.1.20:11434/v1",
        "http://127.0.0.1:99999/v1",
        "",
    ],
)
def test_an_address_off_this_computer_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="on this computer"):
        check_local_url(url)
    with pytest.raises(ValueError):
        summaries.LocalModelClient(url)


def test_the_defaults_are_openrouter_and_ollamas_address() -> None:
    settings = Settings()
    assert settings.ai_provider is AIProvider.OPENROUTER
    assert settings.local_ai_url == "http://127.0.0.1:11434/v1"
    assert settings.local_ai_model == ""


# The client ------------------------------------------------------------------------------------


def _messages() -> list[dict[str, str]]:
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_a_completion_goes_to_the_local_server_with_no_key() -> None:
    fake = FakeLocalModel()
    content, usage = fake.client().complete(_messages(), MODEL)

    assert json.loads(content) == ANSWER
    assert usage.cost_usd is None
    request = fake.requests[0]
    assert str(request.url) == "http://127.0.0.1:11434/v1/chat/completions"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["response_format"]["type"] == "json_schema"
    assert "provider" not in body  # OpenRouter's routing rules mean nothing here


def test_a_server_that_refuses_the_schema_is_asked_again_without_it() -> None:
    fake = FakeLocalModel()

    def chat(request: httpx.Request) -> httpx.Response:
        if "response_format" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "unsupported"}})
        return httpx.Response(200, json=completion(ANSWER, cost=None))

    fake.chat = chat
    content, _usage = fake.client().complete(_messages(), MODEL)

    assert json.loads(content) == ANSWER
    assert len(fake.requests) == 2


def test_an_unknown_model_says_to_test_the_connection() -> None:
    fake = FakeLocalModel()
    fake.chat = lambda request: httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(summaries.AIUnavailable, match="doesn't have a model called llama3.2"):
        fake.client().complete(_messages(), MODEL)


def test_a_server_that_is_not_running_is_named_with_its_address() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = summaries.LocalModelClient(DEFAULT_LOCAL_AI_URL, transport=httpx.MockTransport(down))
    with pytest.raises(summaries.AIUnavailable, match="Check that Ollama, LM Studio"):
        client.models()


def test_the_model_list_is_sorted_one_line_each_and_capped() -> None:
    names = [f"model-{index:03d}" for index in range(80)] + ["Alpha\nbeta", "  "]
    listed = FakeLocalModel(names).client().models()

    assert listed[0] == "Alpha beta"
    assert len(listed) == summaries.MAX_LOCAL_MODELS
    assert all("\n" not in name for name in listed)


def test_a_local_summary_is_named_as_local_and_never_needs_a_training_confirmation() -> None:
    found = SiteResearch(
        account_id="acc-1",
        website="acme.example",
        fetched_at=NOW,
        site_title="Acme",
        site_description="Freight software",
        feed_url=None,
        articles=[],
    )
    summary = summaries.summarise("acc-1", found, FakeLocalModel().client(), "tiny:free", NOW)

    assert summary.model == "local/tiny:free"
    assert summaries.model_label(summary.model) == "tiny:free on this computer"
    assert summaries.model_label("anthropic/claude-haiku-5.5") == "anthropic/claude-haiku-5.5"


# The service -----------------------------------------------------------------------------------


def test_a_local_model_needs_a_name_but_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(
        web_research=True, ai_summaries=True, source=DataSource.MINE, ai_provider=AIProvider.LOCAL
    )
    assert service.ai_ready() == (
        "Choose which model on this computer writes summaries, in Settings > Data & privacy."
    )

    _settings(local_ai_model=MODEL)

    assert service.ai_ready() is None  # no OpenRouter key saved, and none needed


def test_a_free_openrouter_model_needs_no_confirmation_while_a_local_model_writes() -> None:
    _settings(source=DataSource.MINE, ai_model="meta-llama/llama-4-scout:free")
    assert service.ai_training_model() == "meta-llama/llama-4-scout:free"

    _settings(ai_provider=AIProvider.LOCAL, local_ai_model=MODEL)

    assert service.ai_training_model() is None


def test_your_account_is_summarised_by_the_local_model_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLocalModel()
    _local(monkeypatch, fake)
    account_id = _your_account()
    _use_site(monkeypatch)
    openrouter = FakeOpenRouter()
    monkeypatch.setattr(service, "_openrouter_factory", openrouter.client)

    first = service.summarise_account(account_id)
    again = service.summarise_account(account_id)

    assert first.model == "local/llama3.2"
    assert first.about == ANSWER["about"]
    assert again == first
    chats = [r for r in fake.requests if r.url.path.endswith("/chat/completions")]
    assert len(chats) == 1  # the saved summary was reused
    assert openrouter.requests == []  # nothing went to OpenRouter


def test_switching_back_to_openrouter_writes_a_new_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openrouter_fake import KEY
    from wardmind.core import secrets

    fake = FakeLocalModel()
    _local(monkeypatch, fake)
    account_id = _your_account()
    _use_site(monkeypatch)
    service.summarise_account(account_id)
    secrets.save_openrouter_key(KEY)
    openrouter = FakeOpenRouter()
    monkeypatch.setattr(service, "_openrouter_factory", openrouter.client)

    _settings(ai_provider=AIProvider.OPENROUTER)
    summary = service.summarise_account(account_id)

    assert summary.model == load_settings().ai_model
    assert len(openrouter.requests) == 1


def test_set_ai_provider_saves_the_cleaned_address_and_name() -> None:
    service.set_ai_provider(AIProvider.LOCAL, " http://localhost:1234/v1/ ", "  qwen2.5  ")

    saved = load_settings()
    assert saved.ai_provider is AIProvider.LOCAL
    assert saved.local_ai_url == "http://localhost:1234/v1"
    assert saved.local_ai_model == "qwen2.5"
    with pytest.raises(ValueError):
        service.set_ai_provider(AIProvider.LOCAL, "https://api.example.com", MODEL)


def test_local_models_asks_only_the_server_at_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLocalModel()
    monkeypatch.setattr(service, "_local_factory", fake.client)

    assert service.local_models("http://127.0.0.1:11434/v1") == ["llama3.2", "qwen2.5:7b"]
    assert [str(r.url) for r in fake.requests] == ["http://127.0.0.1:11434/v1/models"]


# The Data & privacy page -----------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _form(**changes: str) -> dict[str, str]:
    form = {
        "ai_summaries": "on",
        "ai_model": "anthropic/claude-haiku-5.5",
        "ai_provider": "local",
        "local_ai_url": "http://127.0.0.1:11434/v1",
        "local_ai_model": MODEL,
    }
    form.update(changes)
    return form


def test_the_page_offers_both_and_marks_the_parts(client: TestClient) -> None:
    page = html.unescape(client.get("/settings/privacy").text)

    assert "A model online, through OpenRouter" in page
    assert "A model on this computer" in page
    assert 'name="local_ai_url"' in page
    assert 'value="http://127.0.0.1:11434/v1"' in page
    assert page.count('data-ai-provider-part="openrouter"') == 2  # the model field, the key
    assert page.count('data-ai-provider-part="local"') == 1
    assert 'formaction="/settings/privacy/ai/local/test"' in page


def test_saving_a_local_model_says_so_in_the_status(client: TestClient) -> None:
    _settings(web_research=True)

    response = client.post("/settings/privacy/ai", data=_form(), headers=ORIGIN)

    assert response.status_code == 200
    saved = load_settings()
    assert saved.ai_provider is AIProvider.LOCAL
    assert saved.local_ai_model == MODEL
    assert "AI summaries are on, using llama3.2 on this computer." in html.unescape(response.text)


def test_turning_on_a_local_model_without_a_name_is_refused(client: TestClient) -> None:
    response = client.post("/settings/privacy/ai", data=_form(local_ai_model=""), headers=ORIGIN)

    assert response.status_code == 422
    assert "Enter the name of the model on this computer" in html.unescape(response.text)
    assert load_settings().ai_summaries is False


def test_an_address_off_this_computer_is_refused_on_save(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/ai",
        data=_form(local_ai_url="https://api.example.com/v1"),
        headers=ORIGIN,
    )

    assert response.status_code == 422
    page = html.unescape(response.text)
    assert "Use an address on this computer" in page
    assert 'value="https://api.example.com/v1"' in page  # what was typed comes back
    assert load_settings().local_ai_url == DEFAULT_LOCAL_AI_URL


def test_a_bad_openrouter_model_does_not_block_saving_a_local_model(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/ai", data=_form(ai_model="not a model"), headers=ORIGIN
    )

    assert response.status_code == 200
    assert load_settings().ai_provider is AIProvider.LOCAL


def test_an_old_form_without_the_choice_keeps_the_saved_provider(client: TestClient) -> None:
    _settings(ai_provider=AIProvider.LOCAL, local_ai_model=MODEL)

    response = client.post(
        "/settings/privacy/ai",
        data={"ai_summaries": "off", "ai_model": "anthropic/claude-haiku-5.5"},
        headers=ORIGIN,
    )

    assert response.status_code == 200
    assert load_settings().ai_provider is AIProvider.LOCAL
    assert load_settings().local_ai_model == MODEL


def test_test_connection_lists_the_models_and_saves_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLocalModel()
    monkeypatch.setattr(service, "_local_factory", fake.client)

    response = client.post(
        "/settings/privacy/ai/local/test",
        data=_form(local_ai_url="http://localhost:1234/v1"),
        headers=ORIGIN,
    )

    page = html.unescape(response.text)
    assert response.status_code == 200
    assert "Connected. It has 2 models. llama3.2 is ready." in page
    assert '<option value="qwen2.5:7b">' in page
    assert 'value="http://localhost:1234/v1"' in page  # the typed address stays in the form
    assert str(fake.requests[0].url) == "http://localhost:1234/v1/models"
    assert load_settings().local_ai_url == DEFAULT_LOCAL_AI_URL  # nothing saved


def test_test_connection_names_a_model_the_server_does_not_have(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_local_factory", FakeLocalModel(["mistral"]).client)

    response = client.post("/settings/privacy/ai/local/test", data=_form(), headers=ORIGIN)

    assert "Connected. It has 1 model. llama3.2 isn't one of them: pick one below." in (
        html.unescape(response.text)
    )


def test_test_connection_refuses_an_address_off_this_computer_without_calling_it(
    client: TestClient,
) -> None:
    # conftest's guard fails the test if any local client were built
    response = client.post(
        "/settings/privacy/ai/local/test",
        data=_form(local_ai_url="https://api.example.com/v1"),
        headers=ORIGIN,
    )

    assert response.status_code == 422
    assert "Use an address on this computer" in html.unescape(response.text)


def test_test_connection_says_when_the_model_app_is_not_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        service,
        "_local_factory",
        lambda url: summaries.LocalModelClient(url, transport=httpx.MockTransport(down)),
    )

    response = client.post("/settings/privacy/ai/local/test", data=_form(), headers=ORIGIN)

    page = html.unescape(response.text)
    assert 'role="alert">Couldn\'t reach the model on this computer at' in page


def test_test_connection_is_refused_from_another_site(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/ai/local/test",
        data=_form(),
        headers={"origin": "https://evil.example"},
    )

    assert response.status_code == 403
