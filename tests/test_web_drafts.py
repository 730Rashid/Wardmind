"""The email draft page (/accounts/{id}/draft), its AI wording POST, and the "Draft an email"
button on the account page. Demo and your own data. The AI is a fake: a model on this computer
behind an httpx.MockTransport, or the OpenRouter fake (openrouter_fake.py). Nothing here opens a
connection; conftest fails a test that reaches a real model or site."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from openrouter_fake import KEY, FakeOpenRouter, completion
from wardmind.core import contacts, drafts, editing, secrets, service, summaries
from wardmind.core.settings import AIProvider, DataSource, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
NOW = datetime(2026, 11, 13, 9, 0)
# Far enough ahead that the page says "on <date>" whenever the suite runs.
RENEWAL = "2099-03-02"
RENEWAL_TEXT = "2 March 2099"
MODEL = "llama3.2"
FREE = "meta-llama/llama-4-scout:free"
PAID = "anthropic/claude-haiku-5.5"
GOOD = (
    "SUBJECT: Checking in on Acme Corporation\n"
    "BODY:\n"
    "Hi Priya,\n\n"
    "I hope you're doing well. I'd love to hear how things are going, and I'm glad to set up a "
    "short call whenever suits you.\n\n"
    "Best,\n"
    "Sam Lee"
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _settings(**changes: object) -> None:
    save_settings(load_settings().model_copy(update=changes))
    service.reset_connectors()


def _page(client: TestClient, path: str) -> str:
    """The page with entities decoded, since Jinja escapes apostrophes and ampersands."""
    response = client.get(path)
    assert response.status_code == 200, path
    return html.unescape(response.text)


def _your_account(*, email: str | None = "priya.shah@acme.example", contact: bool = True) -> str:
    """One account of your own (Your data is the book now), with a champion called Priya Shah,
    and Sam Lee as the CSM's name for the sign-off."""
    account = editing.create_account(
        service.get_store(),
        {
            "name": "Acme Corporation",
            "arr": "$120,000",
            "renewal_date": RENEWAL,
            "domain": "acme.example",
        },
        today=NOW.date(),
    )
    if contact:
        contacts.add_contact(
            service.get_store(),
            account.id,
            contacts.ContactInput(
                name="Priya Shah",
                title="VP Operations",
                email=email or "",
                champion=True,
            ),
            source=contacts.ContactSource.YOU,
            now=NOW,
        )
    _settings(source=DataSource.MINE, your_name="Sam Lee")
    return account.id


def _local_model(
    monkeypatch: pytest.MonkeyPatch, *, answer: str = GOOD, status: int = 200, down: bool = False
) -> list[httpx.Request]:
    """A model on this computer that answers with `answer`. Every request it gets is kept."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if down:
            raise httpx.ConnectError("refused", request=request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "nope"}})
        return httpx.Response(200, json=completion(answer, cost=None))

    def factory(url: str) -> summaries.LocalModelClient:
        return summaries.LocalModelClient(url, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(service, "_local_factory", factory)
    return seen


def _ai_on_this_computer(**changes: object) -> None:
    _settings(ai_drafts=True, ai_provider=AIProvider.LOCAL, local_ai_model=MODEL, **changes)


def _kind_switch(page: str) -> str:
    """The kind switcher's markup. The shared nav marks its own current page too, so counts are
    made on this block alone."""
    match = re.search(r'<nav class="draft-kinds.*?</nav>', page, re.S)
    assert match is not None
    return match.group(0)


def _polish(client: TestClient, account_id: str, **fields: str) -> str:
    response = client.post(
        f"/accounts/{account_id}/draft/polish",
        data={"kind": "check_in", "back": "account", **fields},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    return html.unescape(response.text)


# The account page ------------------------------------------------------------------------------


def test_the_account_page_offers_a_draft_for_demo_data(client: TestClient) -> None:
    account_id = service.get_book()[0].account.id

    page = _page(client, f"/accounts/{account_id}")

    assert f'href="/accounts/{account_id}/draft?back=account">Draft an email</a>' in page


def test_the_account_page_offers_a_draft_for_your_own_account(client: TestClient) -> None:
    account_id = _your_account()

    page = _page(client, f"/accounts/{account_id}")

    assert f'href="/accounts/{account_id}/draft?back=account">Draft an email</a>' in page


# The draft page --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(drafts.DraftKind))
def test_every_kind_renders_on_demo_data(client: TestClient, kind: drafts.DraftKind) -> None:
    account_id = service.get_book()[0].account.id

    page = _page(client, f"/accounts/{account_id}/draft?kind={kind.value}")

    assert f'aria-current="page">{kind.label}</a>' in page
    assert "Your draft" in page
    assert 'id="draft-subject"' in page and 'id="draft-body"' in page
    assert "What this is based on" in page
    assert "Wardmind never sends email. Copy it into your own email." in page


def test_the_draft_page_lists_the_kinds_and_marks_one(client: TestClient) -> None:
    account_id = service.get_book()[0].account.id

    page = _page(client, f"/accounts/{account_id}/draft?kind=renewal")

    kinds = _kind_switch(page)
    assert kinds.count('aria-current="page"') == 1
    assert 'aria-current="page">Renewal</a>' in kinds
    assert f"/accounts/{account_id}/draft?kind=re_engage&back=account" in page


def test_an_unknown_kind_gets_a_suggested_one(client: TestClient) -> None:
    account_id = service.get_book()[0].account.id

    page = _page(client, f"/accounts/{account_id}/draft?kind=nonsense")

    assert _kind_switch(page).count('aria-current="page"') == 1


def test_an_unknown_account_is_not_found(client: TestClient) -> None:
    response = client.get("/accounts/no-such-account/draft")

    assert response.status_code == 404
    assert "No account with id no-such-account." in html.unescape(response.text)


def test_the_back_link_goes_to_today_or_the_account(client: TestClient) -> None:
    account_id = _your_account()

    today = _page(client, f"/accounts/{account_id}/draft?kind=check_in&back=today")
    account = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert 'href="/today">Back to Today</a>' in today
    assert f'href="/accounts/{account_id}">Back to the account</a>' in account


def test_the_greeting_sign_off_and_recipient_come_from_the_champion(client: TestClient) -> None:
    account_id = _your_account()

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Hi Priya," in page
    assert "Best,\nSam Lee" in page
    assert "To</span> Priya Shah, VP Operations, priya.shah@acme.example" in page


def test_the_renewal_draft_names_the_date_and_the_basis_lists_it(client: TestClient) -> None:
    account_id = _your_account()

    page = _page(client, f"/accounts/{account_id}/draft?kind=renewal")

    assert f"Your renewal is on {RENEWAL_TEXT}" in page
    assert f"Renews {RENEWAL_TEXT}" in page  # the basis, for the CSM only


def test_the_mailto_link_is_encoded_and_carries_the_contact_email(client: TestClient) -> None:
    account_id = _your_account()

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert (
        'href="mailto:priya.shah@acme.example?subject=Checking%20in%20on%20Acme%20Corporation'
        "&body=" in page
    )
    assert "%0D%0A" in page  # line breaks as mail apps expect them


def test_the_mailto_link_has_no_address_when_the_contact_has_none(client: TestClient) -> None:
    account_id = _your_account(email=None)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert 'href="mailto:?subject=Checking%20in%20on%20Acme%20Corporation&body=' in page


def test_with_no_contact_the_greeting_is_hi_there(client: TestClient) -> None:
    account_id = _your_account(contact=False)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Hi there," in page
    assert "No contact on this account yet." in page


def test_copy_buttons_are_shown_only_when_scripts_run(client: TestClient) -> None:
    account_id = _your_account()

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert page.count('data-draft-copy="draft-') == 2
    assert page.count('class="button button--small js-only" data-draft-copy=') == 2


# The AI wording: what the page offers -----------------------------------------------------------


def test_the_wording_button_is_hidden_and_says_where_to_turn_it_on(client: TestClient) -> None:
    account_id = _your_account()

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Improve the wording with AI" not in page
    assert "AI wording is off. Turn it on in Settings > Assistant" in page
    assert "Written from a template." in page


def test_demo_data_never_offers_ai_wording(client: TestClient) -> None:
    account_id = service.get_book()[0].account.id
    _ai_on_this_computer(source=DataSource.DEMO)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Improve the wording with AI" not in page
    assert "Demo accounts always use the template, so nothing is sent." in page


def test_a_model_online_needs_the_cloud_setting_and_nothing_is_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    secrets.save_openrouter_key(KEY)
    _settings(ai_drafts=True, ai_drafts_cloud=False, ai_model=PAID)
    fake = FakeOpenRouter(answer=GOOD)
    monkeypatch.setattr(service, "_openrouter_factory", fake.client)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")
    assert "Improve the wording with AI" not in page
    assert "AI wording with a model online is off" in page

    refused = _polish(client, account_id)

    assert "Written from a template." in refused
    assert fake.requests == []


def test_a_model_online_needs_a_saved_key(client: TestClient) -> None:
    account_id = _your_account()
    _settings(ai_drafts=True, ai_drafts_cloud=True, ai_model=PAID)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Improve the wording with AI" not in page
    assert "Add your OpenRouter API key in Settings > Data & privacy first." in page


def test_a_model_on_this_computer_needs_a_chosen_model(client: TestClient) -> None:
    account_id = _your_account()
    _settings(ai_drafts=True, ai_provider=AIProvider.LOCAL, local_ai_model="")

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Improve the wording with AI" not in page
    assert "Choose the model on this computer first" in page


def test_the_wording_button_is_shown_when_it_can_be_used(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    _ai_on_this_computer()
    _local_model(monkeypatch)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")

    assert "Improve the wording with AI" in page
    assert f'action="/accounts/{account_id}/draft/polish"' in page
    assert "Written from a template." in page


def test_a_refused_post_asks_no_model_and_says_it_is_the_template(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    seen = _local_model(monkeypatch)  # the setting is off, so no model is called

    page = _polish(client, account_id)

    assert "Written from a template." in page
    assert seen == []


def test_the_polish_post_is_refused_from_another_site(client: TestClient) -> None:
    account_id = _your_account()

    response = client.post(
        f"/accounts/{account_id}/draft/polish",
        data={"kind": "check_in"},
        headers={"origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_the_polish_post_for_an_unknown_account_is_not_found(client: TestClient) -> None:
    response = client.post(
        "/accounts/no-such-account/draft/polish", data={"kind": "check_in"}, headers=ORIGIN
    )

    assert response.status_code == 404


# The AI wording: what happens -------------------------------------------------------------------


def test_a_model_on_this_computer_improves_the_wording(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    _ai_on_this_computer()
    seen = _local_model(monkeypatch)

    page = _polish(client, account_id)

    assert "Wording improved by llama3.2 on this computer." in page
    assert "Checking in on Acme Corporation" in page
    assert "Hi Priya," in page
    sent = seen[0]
    assert str(sent.url) == "http://127.0.0.1:11434/v1/chat/completions"
    assert "authorization" not in sent.headers
    body = json.loads(sent.content)
    assert body["model"] == MODEL
    everything = json.dumps(body, ensure_ascii=False)
    for private in ("120,000", "120000", "priya.shah@acme.example", "acme.example", "Owen"):
        assert private not in everything, private


def test_a_failed_model_shows_the_template_and_the_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    _ai_on_this_computer()
    _local_model(monkeypatch, answer="Sorry, I can't help with that.")

    page = _polish(client, account_id)

    assert "Written from a template." in page
    assert "The AI's wording didn't come back in the form Wardmind asked for" in page
    assert "Checking in on Acme Corporation" in page  # the template's subject


def test_a_model_that_is_down_shows_the_template(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    _ai_on_this_computer()
    _local_model(monkeypatch, down=True)

    page = _polish(client, account_id)

    assert "Written from a template." in page
    assert "The AI couldn't be reached, so this is the template." in page


def test_a_free_model_asks_first_and_is_sent_only_once_confirmed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    secrets.save_openrouter_key(KEY)
    _settings(ai_drafts=True, ai_drafts_cloud=True, ai_model=FREE)
    fake = FakeOpenRouter(answer=GOOD)
    monkeypatch.setattr(service, "_openrouter_factory", fake.client)

    page = _page(client, f"/accounts/{account_id}/draft?kind=check_in")
    assert f'data-training-confirm data-training-model="{FREE}"' in page

    refused = _polish(client, account_id)
    assert summaries.training_message(FREE) in refused
    assert 'name="allow_training" value="1"' in refused
    assert f"Proceed with {FREE}" in refused
    assert fake.requests == []

    done = _polish(client, account_id, allow_training="1")
    assert f"Wording improved by {FREE}." in done
    assert [body["provider"]["data_collection"] for body in fake.bodies] == ["allow"]


def test_a_paid_model_online_is_sent_with_training_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _your_account()
    secrets.save_openrouter_key(KEY)
    _settings(ai_drafts=True, ai_drafts_cloud=True, ai_model=PAID)
    fake = FakeOpenRouter(answer=GOOD)
    monkeypatch.setattr(service, "_openrouter_factory", fake.client)

    page = _polish(client, account_id)

    assert f"Wording improved by {PAID}." in page
    assert [body["provider"]["data_collection"] for body in fake.bodies] == ["deny"]
    sent = json.dumps(fake.bodies, ensure_ascii=False)
    assert "Acme Corporation" in sent
    assert "120,000" not in sent
    assert "priya.shah@acme.example" not in sent
