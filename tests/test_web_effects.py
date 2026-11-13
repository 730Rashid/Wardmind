"""Did it work? in the web (web/effect_pages.py).

Covers the section on Today and its index link, the find box and the cap, the answers (with htmx,
without JavaScript, and refused), the account page's "Steps you took", and Settings > Assistant's
table.

The steps come from service.step_effects and the answers go to service.review_step. Both are
replaced here: the steps by hand-built effects.Effect objects, the answers by a fake that makes the
same checks the core makes and saves the review to the store. So this file doesn't depend on the
core's rules (the core's own tests are in tests/test_effects.py). It runs through the real routes on
hand-added accounts, against the temp folders from conftest.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, effects, proposals, service
from wardmind.core.effects import Check, CheckStatus, Direction, Effect, Reading
from wardmind.core.models import Health
from wardmind.core.proposals import (
    Detector,
    Evidence,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalStatus,
    Review,
    Verdict,
)
from wardmind.core.settings import DataSource, Settings, load_settings, save_settings
from wardmind.web import app as web
from wardmind.web import journal_pages

ORIGIN = {"origin": "http://127.0.0.1"}
HTMX = {"HX-Request": "true"}
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
TODAY = date.today()
NOW = datetime.now(UTC).replace(tzinfo=None)
DONE = TODAY - timedelta(days=20)  # the step was done 20 days ago: the 14-day check is READY
ACCEPTED_KEY = "renewal_no_call:2027-01-14"
DEFAULT_SENTENCE = "Better since: health At risk to Watch, risk 62 to 48."
DEFAULT_READING = Reading(
    signal="usage_trend_30d",
    label="Usage trend",
    before=-38.0,
    after=-12.0,
    before_text="Active users down 38% in 30 days",
    after_text="Active users down 12% in 30 days",
)


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _clean(response: Any) -> None:
    assert not INLINE.search(response.text), f"inline markup on {response.request.url}"
    assert not re.search(r"\bNone\b", _text(response)), f"None printed on {response.request.url}"


def _post(
    client: TestClient, path: str, data: dict[str, str] | None = None, *, htmx: bool = False
) -> Any:
    headers = {**ORIGIN, **(HTMX if htmx else {})}
    return client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _section_of(page: str, pattern: str) -> str:
    match = re.search(pattern, page, re.S)
    assert match is not None, pattern
    return match.group(1)


def _review_section(page: str) -> str:
    return _section_of(page, r'<section[^>]* id="today-review"[^>]*>(.*?)</section>')


def _account_steps(page: str) -> str:
    return _section_of(page, r'<section[^>]* id="account-steps"[^>]*>(.*?)</section>')


def _settings_steps(page: str) -> str:
    return _section_of(
        page, r'<section class="settings-card assistant-group assistant-steps"[^>]*>(.*?)</section>'
    )


def _cards(page: str) -> list[str]:
    # A card ends at its list item's own closing tag, which the template puts at the line start.
    return re.findall(
        r'<li class="today-row step-review">(.*?)\n</li>', _review_section(page), re.S
    )


def _index_count(page: str) -> int | None:
    match = re.search(
        r'<a href="#today-review">Did it work\? <span class="today-index-count">(\d+)</span>', page
    )
    return None if match is None else int(match.group(1))


def _ready(
    days: int,
    sentence: str = DEFAULT_SENTENCE,
    direction: Direction | None = Direction.BETTER,
    readings: tuple[Reading, ...] = (DEFAULT_READING,),
) -> Check:
    return Check(
        days=days,
        due_on=DONE + timedelta(days=days),
        status=CheckStatus.READY,
        before_day=DONE - timedelta(days=2),
        after_day=DONE + timedelta(days=days + 1),
        health_before=Health.AT_RISK,
        health_after=Health.WATCH,
        risk_before=62.0,
        risk_after=48.0,
        direction=direction,
        readings=list(readings),
        sentence=sentence,
    )


def _waiting(days: int) -> Check:
    due = DONE + timedelta(days=days)
    return Check(
        days=days,
        due_on=due,
        status=CheckStatus.WAITING,
        sentence=f"The {days}-day check is on {journal_pages.short_date(due)}.",
    )


def _effect(
    account_id: str,
    proposal_id: str,
    title: str,
    *,
    checks: list[Check] | None = None,
    review: Review | None = None,
    detector: Detector | None = Detector.RENEWAL_NO_CALL,
    done_on: date = DONE,
) -> Effect:
    return Effect(
        proposal_id=proposal_id,
        account_id=account_id,
        title=title,
        detector=detector,
        source="Wardmind",
        done_on=done_on,
        checks=checks if checks is not None else [_ready(14), _waiting(30)],
        review=review,
    )


def _account(name: str) -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$150,000",
            "renewal_date": (TODAY + timedelta(days=400)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _accepted(account_id: str) -> str:
    """A follow-up suggestion the CSM accepted: a step. Returns its id."""
    store = service.get_proposals()
    item = store.add(
        Proposal(
            id="",
            account_id=account_id,
            kind=ProposalKind.FOLLOW_UP,
            source=ProposalSource.DETECTOR,
            detector=Detector.RENEWAL_NO_CALL,
            key=ACCEPTED_KEY,
            title="Make your renewal call",
            why=[Evidence(text="No forecast call yet")],
            text="Make your renewal call",
            due=TODAY,
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert item is not None
    accepted = store.replace(
        item.model_copy(
            update={
                "status": ProposalStatus.ACCEPTED,
                "decided_at": NOW,
                "result_id": "f-0badf00d",
            }
        )
    )
    return accepted.id


def _steps(monkeypatch: pytest.MonkeyPatch, *found: Effect) -> None:
    """service.step_effects answers with these effects (for the account asked for, when one is)."""

    def fake(today: date | None = None, account_id: str | None = None) -> list[Effect]:
        return [effect for effect in found if account_id is None or effect.account_id == account_id]

    monkeypatch.setattr(service, "step_effects", fake)


def _suggestions_off() -> None:
    save_settings(load_settings().model_copy(update={"suggestions": False}))


def _page(client: TestClient, account_id: str) -> str:
    response = client.get(f"/accounts/{account_id}")
    assert response.status_code == 200
    _clean(response)
    return _text(response)


@pytest.fixture(autouse=True)
def mine() -> None:
    save_settings(Settings(source=DataSource.MINE))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Verdict, int]]:
    """service.review_step, faked. It records each call, refuses what the core refuses (an unknown
    id, a suggestion that isn't an accepted step, a check that isn't 14 or 30) and saves the
    review."""
    calls: list[tuple[str, Verdict, int]] = []

    def fake(
        proposal_id: str, verdict: Verdict, check_days: int, now: datetime | None = None
    ) -> Proposal:
        calls.append((proposal_id, verdict, check_days))
        store = service.get_proposals()
        item = store.get(proposal_id)
        if item is None:
            raise LookupError(proposals.NOT_FOUND)
        if item.status is not ProposalStatus.ACCEPTED:
            raise ValueError("Only a step you took can be reviewed.")
        if check_days not in effects.CHECK_DAYS:
            raise ValueError("Pick a check of 14 or 30 days.")
        review = Review(verdict=verdict, check_days=check_days, at=NOW)
        return store.replace(item.model_copy(update={"review": review}))

    monkeypatch.setattr(service, "review_step", fake)
    return calls


# --- Today: the section ---------------------------------------------------------------------------


def test_today_shows_a_due_step_with_its_check_and_three_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(monkeypatch, _effect(acme, "p-aaaaaaaa", "Make your renewal call for Acme"))

    response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    page = _text(response)
    assert "Did it work?" in page
    assert "What moved since steps you took. Moved after isn't proof it worked." in page
    assert _index_count(page) == 1

    cards = _cards(page)
    assert len(cards) == 1
    card = cards[0]
    assert "Acme" in card and "Make your renewal call for Acme" in card
    assert journal_pages.done_label(DONE, TODAY) in card
    assert DEFAULT_SENTENCE in card
    assert "Usage trend" in card
    assert "Before: Active users down 38% in 30 days" in card
    assert "After: Active users down 12% in 30 days" in card
    assert "Helped" in card and "Didn't help" in card and "Too early" in card
    assert 'action="/steps/p-aaaaaaaa/review"' in card
    assert 'hx-post="/steps/p-aaaaaaaa/review"' in card
    assert 'hx-target="closest li"' in card and 'hx-swap="outerHTML"' in card
    assert 'name="check_days" value="14"' in card
    assert 'name="back" value="today"' in card
    for verdict in ("helped", "didnt_help", "too_early"):
        assert f'name="verdict" value="{verdict}"' in card


def test_the_index_lists_did_it_work_right_after_suggested(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(monkeypatch, _effect(acme, "p-aaaaaaaa", "Step for Acme"))
    page = _text(client.get("/today"))
    anchors = re.findall(r'<a href="#(today-[a-z]+)">', page)
    if "today-suggested" in anchors:
        assert anchors.index("today-review") == anchors.index("today-suggested") + 1
    else:
        assert anchors[0] == "today-review"


def test_a_step_with_no_ready_check_yet_is_not_asked_about(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    waiting = _effect(acme, "p-aaaaaaaa", "Step for Acme", checks=[_waiting(14), _waiting(30)])
    _steps(monkeypatch, waiting)
    page = _text(client.get("/today"))
    assert "Did it work?" not in page
    assert 'id="today-review"' not in page


def test_a_step_already_answered_at_the_latest_check_is_not_asked_about_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    answered = Review(verdict=Verdict.HELPED, check_days=14, at=NOW)
    _steps(monkeypatch, _effect(acme, "p-aaaaaaaa", "Step for Acme", review=answered))
    assert 'id="today-review"' not in _text(client.get("/today"))


def test_the_section_is_left_out_when_there_are_no_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account("Acme")
    _steps(monkeypatch)
    page = _text(client.get("/today"))
    assert "Did it work?" not in page and 'id="today-review"' not in page


def test_the_section_is_hidden_when_suggestions_are_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(monkeypatch, _effect(acme, "p-aaaaaaaa", "Step for Acme"))
    _suggestions_off()
    page = _text(client.get("/today"))
    assert "Did it work?" not in page and 'id="today-review"' not in page


def test_a_step_for_an_account_that_left_the_book_is_not_shown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account("Keeper")
    _steps(monkeypatch, _effect("gone-account", "p-aaaaaaaa", "Step for Gone Co"))
    page = _text(client.get("/today"))
    assert "Step for Gone Co" not in page
    assert 'id="today-review"' not in page


def test_the_page_renders_without_the_section_when_the_steps_cannot_be_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the boom with a name in it")

    monkeypatch.setattr(service, "step_effects", broken)
    _account("Acme")
    with caplog.at_level(logging.WARNING):
        response = client.get("/today")
    assert response.status_code == 200
    _clean(response)
    assert "Today" in _text(response)
    assert "Did it work?" not in _text(response)
    assert "(RuntimeError)" in caplog.text
    assert "the boom" not in caplog.text


# --- Today: the find box and the cap ----------------------------------------------------


def test_the_find_box_narrows_the_steps_to_matching_names(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme Corp")
    beacon = _account("Beacon Ltd")
    _steps(
        monkeypatch,
        _effect(acme, "p-aaaaaaaa", "Acme step"),
        _effect(beacon, "p-bbbbbbbb", "Beacon step"),
    )
    page = _text(client.get("/today", params={"q": "acme"}))
    section = _review_section(page)
    assert "Acme step" in section and "Beacon step" not in section
    assert _index_count(page) == 1
    assert 'name="q" value="acme"' in section

    empty = _text(client.get("/today", params={"q": "zzzz"}))
    assert 'id="today-review"' not in empty


def test_no_more_than_three_steps_are_listed_and_the_rest_are_counted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = []
    for number in range(4):
        account = _account(f"Client {number}")
        found.append(
            _effect(
                account,
                f"p-{number:08x}",
                f"Step {number}",
                done_on=DONE - timedelta(days=number),
            )
        )
    _steps(monkeypatch, *found)
    page = _text(client.get("/today"))
    assert len(_cards(page)) == effects.MAX_REVIEW_CARDS == 3
    assert "1 more to review." in _review_section(page)
    assert _index_count(page) == 4


# --- The answers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "check_days", "line"),
    [
        ("helped", "14", "Noted: it helped."),
        ("didnt_help", "30", "Noted: it didn't help."),
        ("too_early", "14", "Noted. Wardmind asks again after the next check."),
        ("too_early", "30", "Noted."),
    ],
)
def test_an_answer_sent_by_htmx_is_one_line_in_place_of_the_card(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[tuple[str, Verdict, int]],
    verdict: str,
    check_days: str,
    line: str,
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": verdict, "check_days": check_days, "back": "today"},
        htmx=True,
    )
    assert response.status_code == 200
    assert "location" not in response.headers
    assert 'class="step-review-done"' in response.text
    assert line in _text(response)
    assert answers == [(step_id, Verdict(verdict), int(check_days))]
    stored = service.get_proposals().get(step_id)
    assert stored is not None and stored.review is not None
    assert stored.review.verdict is Verdict(verdict)


def test_an_answer_without_javascript_goes_back_to_today_with_a_notice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "helped", "check_days": "14", "back": "today", "q": "acme"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/today?journal=step-reviewed&q=acme"
    assert "Saved your answer." in _text(client.get(response.headers["location"]))


def test_an_answer_without_javascript_from_the_account_page_goes_back_to_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "didnt_help", "check_days": "14", "back": "account", "q": "ignored"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/accounts/{acme}?journal=step-reviewed"
    assert "Saved your answer." in _text(client.get(response.headers["location"]))


def test_a_bad_verdict_is_refused_on_the_card_and_nothing_is_saved(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[tuple[str, Verdict, int]],
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "maybe", "check_days": "14", "back": "today"},
        htmx=True,
    )
    assert response.status_code == 200
    assert 'class="today-row step-review"' in response.text
    assert "Pick Helped, Didn't help or Too early." in _text(response)
    assert answers == []
    stored = service.get_proposals().get(step_id)
    assert stored is not None and stored.review is None


def test_a_bad_verdict_without_javascript_is_a_422_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "maybe", "check_days": "14", "back": "today"},
    )
    assert response.status_code == 422
    _clean(response)
    assert "Pick Helped, Didn't help or Too early." in _text(response)
    assert 'href="/today"' in response.text and "Back to Today" in response.text
    assert answers == []


@pytest.mark.parametrize("check_days", ["7", "", "fourteen"])
def test_a_check_that_is_not_14_or_30_days_is_refused(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[tuple[str, Verdict, int]],
    check_days: str,
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "helped", "check_days": check_days, "back": "today"},
        htmx=True,
    )
    assert response.status_code == 200
    assert 'class="today-row step-review"' in response.text
    assert "Pick a check of 14 or 30 days." in _text(response)
    stored = service.get_proposals().get(step_id)
    assert stored is not None and stored.review is None


def test_a_refused_check_without_javascript_is_a_422_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "helped", "check_days": "7", "back": "today"},
    )
    assert response.status_code == 422
    assert "Pick a check of 14 or 30 days." in _text(response)


def test_a_refused_answer_on_the_account_page_is_the_step_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    acme = _account("Acme")
    step_id = _accepted(acme)
    _steps(monkeypatch, _effect(acme, step_id, "Make your renewal call"))
    response = _post(
        client,
        f"/steps/{step_id}/review",
        {"verdict": "maybe", "check_days": "14", "back": "account"},
        htmx=True,
    )
    assert response.status_code == 200
    assert 'class="journal-item step-item"' in response.text
    assert "Pick Helped, Didn't help or Too early." in _text(response)


def test_an_answer_to_a_suggestion_that_is_not_an_accepted_step_gets_its_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    acme = _account("Acme")
    store = service.get_proposals()
    item = store.add(
        Proposal(
            id="",
            account_id=acme,
            kind=ProposalKind.FOLLOW_UP,
            source=ProposalSource.DETECTOR,
            detector=Detector.RENEWAL_NO_CALL,
            key=ACCEPTED_KEY,
            title="Still open",
            why=[Evidence(text="No forecast call yet")],
            text="Still open",
            due=TODAY,
            created_at=NOW,
            basis_day=TODAY,
        )
    )
    assert item is not None
    _steps(monkeypatch)
    response = _post(
        client,
        f"/steps/{item.id}/review",
        {"verdict": "helped", "check_days": "14", "back": "today"},
        htmx=True,
    )
    assert response.status_code == 200
    assert 'class="step-review-done"' in response.text
    assert "Only a step you took can be reviewed." in _text(response)


def test_an_unknown_step_is_a_clear_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, Verdict, int]]
) -> None:
    _steps(monkeypatch)
    data = {"verdict": "helped", "check_days": "14", "back": "today"}
    htmx_reply = _post(client, "/steps/p-deadbeef/review", data, htmx=True)
    assert htmx_reply.status_code == 200
    assert "That suggestion isn't there any more." in _text(htmx_reply)

    page = _post(client, "/steps/p-deadbeef/review", data)
    assert page.status_code == 404
    _clean(page)
    assert "That suggestion isn't there any more." in _text(page)
    assert 'href="/today"' in page.text and "Back to Today" in page.text


# --- The account page -------------------------------------------------------------------


def test_the_account_page_lists_each_step_with_its_checks_and_the_answer_given(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    answered = Review(verdict=Verdict.HELPED, check_days=14, at=NOW)
    _steps(
        monkeypatch,
        _effect(acme, "p-aaaaaaaa", "Make your renewal call", review=answered),
    )
    page = _page(client, acme)
    section = _account_steps(page)
    assert "Steps you took" in page
    assert "Make your renewal call" in section
    assert DEFAULT_SENTENCE in section
    assert f"After 14 days. {DEFAULT_SENTENCE}" in section  # which check it is, once its day came
    assert "The 30-day check is on" in section
    assert "After 30 days." not in section  # a check still to come says its own day
    assert "You said: Helped" in section
    assert 'name="verdict"' not in section  # answered at the latest check: no buttons


def test_the_account_page_offers_the_answers_when_a_check_is_ready_and_unanswered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(monkeypatch, _effect(acme, "p-aaaaaaaa", "Make your renewal call"))
    section = _account_steps(_page(client, acme))
    for verdict in ("helped", "didnt_help", "too_early"):
        assert f'name="verdict" value="{verdict}"' in section
    assert 'name="check_days" value="14"' in section
    assert 'name="back" value="account"' in section
    assert 'hx-post="/steps/p-aaaaaaaa/review"' in section


def test_the_account_page_has_no_steps_section_without_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(monkeypatch)
    page = _page(client, acme)
    assert "Steps you took" not in page
    assert 'id="account-steps"' not in page


def test_the_account_page_lists_at_most_ten_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(
        monkeypatch,
        *[_effect(acme, f"p-{number:08x}", f"Step {number}") for number in range(11)],
    )
    section = _account_steps(_page(client, acme))
    assert section.count('class="journal-item step-item"') == 10


def test_the_account_page_renders_without_steps_when_they_cannot_be_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("a path with a name in it")

    monkeypatch.setattr(service, "step_effects", broken)
    acme = _account("Acme")
    with caplog.at_level(logging.WARNING):
        page = _page(client, acme)
    assert "Steps you took" not in page
    assert "(OSError)" in caplog.text
    assert "a path with" not in caplog.text


# --- Settings > Assistant ---------------------------------------------------------------


def test_settings_shows_what_followed_each_kind_of_step(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(
        monkeypatch,
        _effect(acme, "p-aaaaaaaa", "Health step", detector=Detector.HEALTH_DROPPED),
        _effect(acme, "p-bbbbbbbb", "Renewal step", detector=Detector.RENEWAL_NO_CALL),
    )
    response = client.get("/settings/assistant")
    assert response.status_code == 200
    _clean(response)
    text = _text(response)
    assert "What followed your steps" in text
    assert "Counts of what moved after each step, not what caused it." in text
    section = _settings_steps(text)
    headers = re.findall(r'<th scope="col"[^>]*>([^<]*)</th>', section)
    assert headers == [
        "Suggestion",
        "Steps",
        "Checked",
        "Better",
        "Same",
        "Worse",
        "You said helped",
        "You said didn't help",
    ]
    assert re.findall(r'<th scope="row">([^<]*)</th>', section) == [
        "Health dropped",
        "Renewal with no call",
    ]


def test_settings_hides_the_table_with_no_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _steps(monkeypatch)
    text = _text(client.get("/settings/assistant"))
    assert "What followed your steps" not in text
    assert "assistant-steps" not in text


def test_settings_hides_the_table_when_the_steps_cannot_be_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("a value with a name in it")

    monkeypatch.setattr(service, "step_effects", broken)
    with caplog.at_level(logging.WARNING):
        response = client.get("/settings/assistant")
    assert response.status_code == 200
    _clean(response)
    assert "What followed your steps" not in _text(response)
    assert "(ValueError)" in caplog.text
    assert "a value with" not in caplog.text


# --- The words --------------------------------------------------------------------------


def test_no_step_text_says_because_or_due_to_or_claims_a_cause(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _account("Acme")
    _steps(
        monkeypatch,
        _effect(acme, "p-aaaaaaaa", "Make your renewal call for Acme"),
        _effect(
            acme,
            "p-bbbbbbbb",
            "Second step",
            review=Review(verdict=Verdict.HELPED, check_days=14, at=NOW),
        ),
    )
    today = _review_section(_text(client.get("/today")))
    account = _account_steps(_page(client, acme))
    settings = _settings_steps(_text(client.get("/settings/assistant")))
    for section in (today, account, settings):
        lowered = section.lower()
        assert "because" not in lowered and "due to" not in lowered
    assert "caused" not in today.lower() and "caused" not in account.lower()
    # Settings' table says what it is not: "not what caused it", and nothing else.
    assert settings.lower().count("caused") == 1
