"""A model whose providers may train on what they're sent (an OpenRouter ":free" model): the
warning by the model field, the refusal until each use is confirmed, and what is sent once it is.
Sites are fake (research_fake.py) and OpenRouter is fake (openrouter_fake.py)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from openrouter_fake import KEY, FakeOpenRouter
from research_fake import acme_site, counted
from wardmind.core import editing, secrets, service, summaries
from wardmind.core.settings import DataSource, load_settings, save_settings
from wardmind.web import app as web

FREE = "meta-llama/llama-4-scout:free"
TODAY = date(2026, 10, 22)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _settings(**changes: object) -> None:
    save_settings(load_settings().model_copy(update=changes))
    service.reset_connectors()


def _your_account() -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": "Acme Corporation",
            "arr": "$120,000",
            "renewal_date": "2027-01-10",
            "domain": "acme.example",
        },
        today=TODAY,
    )
    _settings(source=DataSource.MINE, web_research=True, ai_summaries=True)
    secrets.save_openrouter_key(KEY)
    return account.id


def _fakes(monkeypatch: pytest.MonkeyPatch) -> FakeOpenRouter:
    _, factory = counted(acme_site().client)
    monkeypatch.setattr(service, "_site_client_factory", factory)
    fake = FakeOpenRouter()
    monkeypatch.setattr(service, "_openrouter_factory", fake.client)
    return fake


@pytest.mark.parametrize(
    ("model", "trains"),
    [
        (FREE, True),
        ("google/gemma-3-27b-it:FREE ", True),
        ("anthropic/claude-haiku-5.5", False),
        ("openai/gpt-x", False),
        ("free/model", False),
    ],
)
def test_free_models_are_the_ones_that_train(model: str, trains: bool) -> None:
    assert summaries.trains_on_data(model) is trains


def test_a_free_model_is_refused_until_confirmed_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _your_account()
    service.set_ai_model(FREE)
    fake = _fakes(monkeypatch)
    with pytest.raises(summaries.TrainingNotConfirmed) as refused:
        service.summarise_account(account_id)
    assert FREE in refused.value.message
    assert fake.requests == []

    service.summarise_account(account_id, allow_training=True)
    assert [body["provider"]["data_collection"] for body in fake.bodies] == ["allow"]


def test_other_models_still_deny_training(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _your_account()
    fake = _fakes(monkeypatch)
    # Confirming does not loosen anything for a model that doesn't need it.
    service.summarise_account(account_id, allow_training=True)
    assert [body["provider"]["data_collection"] for body in fake.bodies] == ["deny"]


def test_looking_up_everything_with_a_free_model_asks_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _your_account()
    service.set_ai_model(FREE)
    _fakes(monkeypatch)
    with pytest.raises(summaries.TrainingNotConfirmed):
        service.start_bulk_research(summarise=True, refresh=False)
    # Without summarising, nothing is sent to a model, so there is nothing to confirm.
    service.start_bulk_research(summarise=False, refresh=False)


def test_demo_data_sends_nothing_so_asks_nothing() -> None:
    _settings(source=DataSource.DEMO, web_research=True, ai_summaries=True, ai_model=FREE)
    assert service.ai_training_model() is None


def test_the_model_field_warns_about_a_free_model(client: TestClient) -> None:
    page = client.get("/settings/privacy").text
    assert 'id="field-ai-model-training" data-training-warning hidden' in page
    _settings(ai_model=FREE)
    page = client.get("/settings/privacy").text
    assert 'id="field-ai-model-training" data-training-warning>' in page
    assert "may keep what you send and train on it" in page
    assert 'aria-describedby="field-ai-model-training field-ai-model-help"' in page


def test_the_summarise_button_asks_first_and_the_refusal_offers_to_proceed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    service.set_ai_model(FREE)
    fake = _fakes(monkeypatch)
    service.research_account(account_id)
    page = client.get(f"/accounts/{account_id}").text
    assert f'data-training-confirm data-training-model="{FREE}"' in page

    refused = client.post(f"/accounts/{account_id}/summarise", headers={"HX-Request": "true"})
    assert str(escape(summaries.training_message(FREE))) in refused.text
    assert 'name="allow_training" value="1"' in refused.text
    assert f"Proceed with {FREE}" in refused.text
    assert fake.requests == []

    done = client.post(
        f"/accounts/{account_id}/summarise",
        data={"allow_training": "1"},
        headers={"HX-Request": "true"},
    )
    assert done.status_code == 200
    assert len(fake.requests) == 1


def test_the_lookups_form_asks_first_only_when_summarising(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _your_account()
    service.set_ai_model(FREE)
    _fakes(monkeypatch)
    page = client.get("/research").text
    assert f'data-training-confirm="summarise" data-training-model="{FREE}"' in page

    refused = client.post("/research/all", data={"summarise": "1", "refresh": "1"})
    assert str(escape(summaries.training_message(FREE))) in refused.text
    assert 'name="allow_training" value="1"' in refused.text
    assert '<input type="hidden" name="refresh" value="1">' in refused.text
