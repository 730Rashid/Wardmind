"""Web research in the web UI: the Data & privacy page, the account page's "From their website"
section in each state (plain form post and htmx), cross-site refusals, and the lookup route.

Sites are fake (research_fake.py). The account page must never look a site up on a GET: the
counted factories below prove it."""

from __future__ import annotations

import html
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from research_fake import NO_FEED_HOME, acme_site, counted, reply
from wardmind.core import service
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.web import app as web

ORIGIN = {"origin": "http://127.0.0.1"}
HTMX = {"HX-Request": "true", **ORIGIN}
CROSS_SITE = {"origin": "https://evil.example"}
DEMO_ID = "acc-001"
SECTION = re.compile(r'<section id="research".*?</section>', re.S)
SETTINGS_LINK = re.compile(r'<a class="settings-link" href="([^"]+)"([^>]*)>')


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes apostrophes in variables."""
    return html.unescape(response.text)


def _section(response: Any) -> str:
    match = SECTION.search(response.text)
    assert match is not None, "no research section on this response"
    return html.unescape(match.group(0))


def _on(source: DataSource = DataSource.DEMO) -> None:
    save_settings(Settings(web_research=True, source=source))
    service.reset_connectors()


def _tail(location: str) -> str:
    return location.split("?", 1)[0].rsplit("/", 1)[-1]


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _site(monkeypatch: pytest.MonkeyPatch, site: Any) -> list[int]:
    """Serve lookups from a fake site; returns the list that grows with every factory call."""
    calls, factory = counted(site.client)
    monkeypatch.setattr(service, "_site_client_factory", factory)
    return calls


def _your_account(
    client: TestClient, domain: str = "acme.example", name: str = "Acme Corporation"
) -> str:
    response = client.post(
        "/accounts/new",
        data={"name": name, "arr": "$120,000", "renewal_date": "2027-01-10", "domain": domain},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, _text(response)
    _set_source(DataSource.MINE)
    return _tail(str(response.headers["location"]))


def _set_source(source: DataSource) -> None:
    save_settings(load_settings().model_copy(update={"source": source}))
    service.reset_connectors()


# --- The Data & privacy page ---------------------------------------------------------------


def test_the_privacy_page_explains_web_research_and_shows_it_is_off(client: TestClient) -> None:
    response = client.get("/settings/privacy")
    assert response.status_code == 200
    text = _text(response)
    assert "Web research is off." in text
    assert "When it's on, each account page gets a Look up their website button." in text
    assert "Only an ordinary visit to the customer's website." in text
    assert "No search engine or other company is involved." in text
    assert "Only when you press the button, for that one account. Never in the background." in text
    assert "The result, on this computer, for 24 hours." in text
    assert "Demo accounts are invented, so they show sample news and nothing is sent." in text
    assert "Wardmind won't visit an address on this computer or your private network" in text
    assert "Clear local cache" in text
    assert "It doesn't touch your connections" in text


def test_the_privacy_page_has_the_two_radios_with_off_checked(client: TestClient) -> None:
    text = client.get("/settings/privacy").text
    assert re.search(r'name="research" value="off"[^>]* checked', text)
    assert not re.search(r'name="research" value="on"[^>]* checked', text)
    assert 'action="/settings/privacy/research"' in text
    assert 'action="/settings/privacy/clear-cache"' in text


def test_the_privacy_page_shows_on_when_it_is_on(client: TestClient) -> None:
    _on()
    text = client.get("/settings/privacy").text
    assert "Web research is on." in text
    assert re.search(r'name="research" value="on"[^>]* checked', text)


def test_the_data_folder_is_shown_in_code(client: TestClient) -> None:
    text = client.get("/settings/privacy").text
    assert f'<code class="data-path">{html.escape(str(service.get_store().root))}</code>' in text


def test_data_and_privacy_is_the_settings_nav_item_after_connections(client: TestClient) -> None:
    for path in ("/settings/appearance", "/settings/scoring", "/settings/connections"):
        links = SETTINGS_LINK.findall(client.get(path).text)
        assert [href for href, _ in links][-1] == "/settings/privacy", path
    privacy = client.get("/settings/privacy").text
    current = [href for href, attrs in SETTINGS_LINK.findall(privacy) if "aria-current" in attrs]
    assert current == ["/settings/privacy"]
    assert "Data &amp; privacy" in privacy


def test_turning_web_research_on_saves_it_and_says_so(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/research",
        data={"research": "on"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?research=on"
    assert load_settings().web_research is True
    page = _text(client.get(response.headers["location"]))
    assert '<p class="settings-status" role="status">Web research is on.</p>' in page


def test_turning_web_research_off_removes_saved_lookups_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    client.post(f"/accounts/{DEMO_ID}/research", headers=HTMX)
    root = service.get_store().root / "research"
    assert len(list(root.glob("*.json"))) == 1

    response = client.post(
        "/settings/privacy/research",
        data={"research": "off"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings/privacy?research=off"
    assert load_settings().web_research is False
    assert list(root.glob("*.json")) == []
    page = _text(client.get(response.headers["location"]))
    assert "Web research is off. Saved lookups were removed." in page


@pytest.mark.parametrize("value", ["maybe", "", "ON", "true"])
def test_a_research_choice_that_is_not_on_or_off_is_refused(client: TestClient, value: str) -> None:
    response = client.post(
        "/settings/privacy/research",
        data={"research": value},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "Choose On or Off, then save again." in _text(response)
    assert load_settings().web_research is False


def test_a_missing_research_choice_is_refused(client: TestClient) -> None:
    response = client.post("/settings/privacy/research", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 422
    assert load_settings().web_research is False


@pytest.mark.parametrize(
    ("query", "notice"),
    [
        ("cleared=1&lookups=0&drafts=0", "There was nothing to clear."),
        ("cleared=1&lookups=1&drafts=0", "Removed 1 saved lookup."),
        ("cleared=1&lookups=2&drafts=0", "Removed 2 saved lookups."),
        ("cleared=1&lookups=0&drafts=1", "Removed 1 unfinished import."),
        ("cleared=1&lookups=0&drafts=2", "Removed 2 unfinished imports."),
        ("cleared=1&lookups=2&drafts=1", "Removed 2 saved lookups and 1 unfinished import."),
        ("cleared=1&lookups=1&drafts=3", "Removed 1 saved lookup and 3 unfinished imports."),
        ("cleared=1&lookups=banana&drafts=", "There was nothing to clear."),
    ],
)
def test_the_clear_cache_notice_has_the_right_wording(
    client: TestClient, query: str, notice: str
) -> None:
    text = _text(client.get(f"/settings/privacy?{query}"))
    assert f'<p class="settings-status" role="status">{notice}</p>' in text


def test_clearing_the_cache_says_what_it_removed(client: TestClient) -> None:
    _on()
    client.post(f"/accounts/{DEMO_ID}/research", headers=HTMX)
    client.post("/accounts/acc-002/research", headers=HTMX)
    response = client.post("/settings/privacy/clear-cache", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/privacy?cleared=1&lookups=2&drafts=0"
    page = _text(client.get(response.headers["location"]))
    assert "Removed 2 saved lookups." in page


def test_clearing_the_cache_says_when_there_was_nothing(client: TestClient) -> None:
    response = client.post("/settings/privacy/clear-cache", headers=ORIGIN, follow_redirects=False)
    assert response.headers["location"] == "/settings/privacy?cleared=1&lookups=0&drafts=0"
    assert "There was nothing to clear." in _text(client.get(response.headers["location"]))


def test_clearing_the_cache_keeps_the_research_setting(client: TestClient) -> None:
    _on()
    client.post("/settings/privacy/clear-cache", headers=ORIGIN, follow_redirects=False)
    assert load_settings().web_research is True


# --- The account page's section ------------------------------------------------------------


def test_with_web_research_off_the_account_page_says_so_and_links_to_settings(
    client: TestClient,
) -> None:
    section = _section(client.get(f"/accounts/{DEMO_ID}"))
    assert "Web research is off. Turn it on in" in section
    assert 'href="/settings/privacy"' in section
    assert "Look up their website" not in section


def test_a_demo_account_offers_a_lookup_that_says_nothing_is_sent(client: TestClient) -> None:
    _on()
    section = _section(client.get(f"/accounts/{DEMO_ID}"))
    assert "Look up their website" in section
    assert "Demo accounts show sample news. Nothing is sent." in section
    assert f'action="/accounts/{DEMO_ID}/research"' in section
    assert f'hx-post="/accounts/{DEMO_ID}/research"' in section
    assert 'hx-target="#research"' in section and 'hx-swap="outerHTML"' in section


def test_a_your_data_account_names_its_website_in_the_hint(client: TestClient) -> None:
    _on()
    account_id = _your_account(client)
    section = _section(client.get(f"/accounts/{account_id}"))
    assert "Visits acme.example only. Nothing about this account is sent." in section
    assert "Look up their website" in section


def test_an_account_without_a_website_offers_to_add_one(client: TestClient) -> None:
    _on()
    account_id = _your_account(client, domain="")
    section = _section(client.get(f"/accounts/{account_id}"))
    assert "This account has no website." in section
    assert f'href="/accounts/{account_id}/edit#field-domain"' in section
    assert ">Add it</a>" in section
    assert "Look up their website" not in section


def test_the_add_it_link_lands_on_the_website_field(client: TestClient) -> None:
    _on()
    account_id = _your_account(client, domain="")
    edit = client.get(f"/accounts/{account_id}/edit")
    assert 'id="field-domain"' in edit.text


def test_a_private_website_says_so_and_offers_to_change_it(client: TestClient) -> None:
    _on()
    account_id = _your_account(client, domain="10.0.0.5")
    section = _section(client.get(f"/accounts/{account_id}"))
    assert "That website points at this computer or a private network" in section
    assert ">Change it</a>" in section
    assert "Look up their website" not in section


def test_the_account_page_never_looks_a_website_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    calls = _site(monkeypatch, acme_site())
    client.get(f"/accounts/{account_id}")
    client.get(f"/accounts/{DEMO_ID}")
    assert calls == []


def test_a_look_up_shows_the_site_title_description_and_articles(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    site = acme_site()
    calls = _site(monkeypatch, site)

    response = client.post(f"/accounts/{account_id}/research", headers=HTMX)

    assert response.status_code == 200
    assert response.headers["vary"] == "HX-Request"
    assert response.text.lstrip().startswith('<section id="research"')
    assert "<html" not in response.text and "<main" not in response.text
    section = _section(response)
    assert '<h3 class="research-site">Acme Corporation | Home</h3>' in section
    assert "Acme builds freight software for regional carriers." in section
    assert '<a href="https://acme.example/news/series-b" rel="noopener noreferrer">' in section
    assert "Acme raises Series B to hire support" in section
    assert "5 October" in section
    assert '<li class="tag">Funding</li>' in section
    assert '<li class="tag">Leadership change</li>' in section
    assert "Looked up today." in section
    assert calls == [1]
    assert site.seen[0] == "https://acme.example/"


def test_a_saved_look_up_shows_on_the_account_page_without_a_new_visit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    site = acme_site()
    calls = _site(monkeypatch, site)
    client.post(f"/accounts/{account_id}/research", headers=HTMX)
    section = _section(client.get(f"/accounts/{account_id}"))
    assert "Acme Corporation | Home" in section
    assert "Look up again" in section
    assert calls == [1]


def test_the_look_up_again_button_asks_for_a_fresh_visit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    site = acme_site()
    _site(monkeypatch, site)
    client.post(f"/accounts/{account_id}/research", headers=HTMX)
    first_visits = len(site.seen)
    section = _section(client.get(f"/accounts/{account_id}"))
    assert re.search(r'<input type="hidden" name="refresh" value="1">', section)
    client.post(f"/accounts/{account_id}/research", headers={**HTMX}, data={"refresh": "1"})
    assert len(site.seen) > first_visits


def test_a_plain_form_post_redirects_back_to_the_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    _site(monkeypatch, acme_site())
    response = client.post(
        f"/accounts/{account_id}/research", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{account_id}#research"
    page = _text(client.get(response.headers["location"]))
    assert "Acme Corporation | Home" in page


def test_a_demo_sample_is_marked_as_sample_news(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    response = client.post(f"/accounts/{DEMO_ID}/research", headers=HTMX)
    section = _section(response)
    assert "Sample news, invented like the demo accounts. Nothing was sent." in section
    assert "Look up again" in section


def test_a_site_without_a_feed_says_so_and_still_shows_its_title(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    site = acme_site(**{"https://acme.example/news/feed.xml": reply(NO_FEED_HOME)})
    _site(monkeypatch, site)
    section = _section(client.post(f"/accounts/{account_id}/research", headers=HTMX))
    assert "Their website doesn't publish a news feed Wardmind could read." in section
    assert "Acme Corporation | Home" in section
    assert '<ul class="research-articles">' not in section


def test_javascript_and_data_feed_links_never_reach_the_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>Hostile</title><link>javascript:alert(1)</link></item>"
        b"<item><title>Also hostile</title><link>data:text/html,boom</link></item>"
        b"<item><title>Good story</title><link>https://acme.example/news/good</link></item>"
        b"</channel></rss>"
    )
    _on(DataSource.MINE)
    account_id = _your_account(client)
    _site(monkeypatch, acme_site(**{"https://acme.example/news/feed.xml": reply(feed)}))
    section = _section(client.post(f"/accounts/{account_id}/research", headers=HTMX))
    assert "Good story" in section
    assert "javascript:" not in section
    assert "data:text" not in section
    assert "Hostile" not in section


def test_a_failed_look_up_over_htmx_shows_the_reason_and_the_button(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    _site(monkeypatch, acme_site(**{"https://acme.example/": reply(b"", status=500)}))
    response = client.post(f"/accounts/{account_id}/research", headers=HTMX)
    assert response.status_code == 200
    section = _section(response)
    assert (
        '<p class="field-error" role="alert">acme.example answered with an error (500).</p>'
        in section
    )
    assert "Look up their website" in section


def test_a_failed_look_up_without_js_shows_the_account_page_with_the_alert(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    _site(monkeypatch, acme_site(**{"https://acme.example/": reply(b"", status=503)}))
    response = client.post(
        f"/accounts/{account_id}/research", headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 200
    assert "<title>Acme Corporation - Wardmind</title>" in response.text
    assert 'role="alert">acme.example answered with an error (503).' in response.text


def test_a_look_up_without_js_redirects_to_the_section_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(DataSource.MINE)
    account_id = _your_account(client)
    _site(monkeypatch, acme_site())
    response = client.post(
        f"/accounts/{account_id}/research", headers=ORIGIN, follow_redirects=False
    )
    assert response.headers["location"] == f"/accounts/{account_id}#research"


def test_web_research_off_turns_a_look_up_into_the_off_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _site(monkeypatch, acme_site())
    response = client.post(f"/accounts/{DEMO_ID}/research", headers=HTMX)
    assert response.status_code == 200
    assert "Web research is off." in _section(response)
    assert calls == []


def test_a_look_up_for_an_unknown_account_is_not_found(client: TestClient) -> None:
    _on()
    response = client.post("/accounts/acc-nope/research", headers=HTMX)
    assert response.status_code == 404
    assert "No account with id acc-nope." in _text(response)


def test_a_cross_site_look_up_is_refused_and_visits_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    calls = _site(monkeypatch, acme_site())
    for headers in (
        CROSS_SITE,
        {"sec-fetch-site": "cross-site"},
        {"origin": "null", "sec-fetch-site": "cross-site"},
    ):
        response = client.post(f"/accounts/{DEMO_ID}/research", headers=headers)
        assert response.status_code == 403
    assert calls == []


def test_a_cross_site_research_setting_change_is_refused(client: TestClient) -> None:
    response = client.post(
        "/settings/privacy/research",
        data={"research": "on"},
        headers=CROSS_SITE,
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert load_settings().web_research is False


def test_a_cross_site_clear_cache_is_refused(client: TestClient) -> None:
    _on()
    response = client.post(
        "/settings/privacy/clear-cache", headers=CROSS_SITE, follow_redirects=False
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "state",
    ["off", "demo", "no-website", "blocked", "result", "no-feed", "error"],
)
def test_no_none_is_printed_in_any_research_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    if state == "off":
        section = _section(client.get(f"/accounts/{DEMO_ID}"))
    elif state == "demo":
        _on()
        section = _section(client.post(f"/accounts/{DEMO_ID}/research", headers=HTMX))
    else:
        _on(DataSource.MINE)
        if state == "no-website":
            account_id = _your_account(client, domain="")
        elif state == "blocked":
            account_id = _your_account(client, domain="10.0.0.5")
        else:
            account_id = _your_account(client)
            site = acme_site()
            if state == "no-feed":
                site = acme_site(**{"https://acme.example/news/feed.xml": reply(NO_FEED_HOME)})
            elif state == "error":
                site = acme_site(**{"https://acme.example/": reply(b"", status=500)})
            _site(monkeypatch, site)
        section = _section(client.post(f"/accounts/{account_id}/research", headers=HTMX))
    assert "None" not in section


def test_the_research_form_is_a_no_print_control(client: TestClient) -> None:
    _on()
    section = client.get(f"/accounts/{DEMO_ID}").text
    assert re.search(r'<form class="research-form no-print"', section)


def test_a_demo_sample_survives_the_no_js_redirect(client: TestClient) -> None:
    # The no-JavaScript look up redirects to GET /accounts/{id}#research, which reads only the
    # saved copy, so a demo sample must be saved too or it would vanish on the redirect.
    _on()
    response = client.post(f"/accounts/{DEMO_ID}/research", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{DEMO_ID}#research"
    section = _section(client.get(response.headers["location"]))
    assert "Sample news, invented like the demo accounts. Nothing was sent." in section
    assert "Look up again" in section
