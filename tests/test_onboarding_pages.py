"""The onboarding prep pages: the picker at /onboarding, the redirect at /onboarding/open, the brief
at /onboarding/{id}, and the helpers behind them."""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import service
from wardmind.core.briefs import SIGNAL_LABELS
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import Account, Health, Milestones, Motion, ScoredAccount
from wardmind.core.onboarding import (
    ONBOARDING_SIGNALS,
    AgendaItem,
    MilestoneStatus,
    OnboardingBrief,
    PlanMilestone,
)
from wardmind.web import app as web
from wardmind.web import formatting

DONE = MilestoneStatus.DONE
OVERDUE = MilestoneStatus.OVERDUE
NEXT = MilestoneStatus.NEXT
UPCOMING = MilestoneStatus.UPCOMING
DOT_STATE = {DONE: "done", OVERDUE: "overdue", NEXT: "open", UPCOMING: "open"}


class _AllComplete(DemoConnector):
    """The demo book with every milestone done, so no account is in onboarding."""

    def list_accounts(self) -> list[Account]:
        accounts = super().list_accounts()
        return [
            account.model_copy(
                update={
                    "milestones": Milestones(
                        done=account.milestones.total, total=account.milestones.total
                    )
                }
            )
            for account in accounts
        ]


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes apostrophes in variables."""
    return html.unescape(response.text)


def _plain(fragment: str) -> str:
    """Text of a fragment: tags removed, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _block(markup: str, pattern: str) -> str:
    match = re.search(pattern, markup, re.S)
    assert match is not None, pattern
    return match.group(1)


def _rows(markup: str) -> list[str]:
    """The body rows of the picker's table, as HTML."""
    body = _block(markup, r'<table class="onboarding-table">.*?<tbody>(.*?)</tbody>')
    return re.findall(r"<tr>(.*?)</tr>", body, re.S)


def _book_onboarding() -> list[ScoredAccount]:
    return service.onboarding_accounts(service.get_book())


def _brief(account_id: str) -> OnboardingBrief:
    brief = service.get_onboarding_brief(account_id)
    assert brief is not None
    return brief


def _steps(statuses: list[MilestoneStatus]) -> list[PlanMilestone]:
    """Plan steps with the given statuses, named by their position."""
    return [
        PlanMilestone(position=index, name=f"Step {index}", status=status)
        for index, status in enumerate(statuses, start=1)
    ]


_NAMED_PLAN = (
    "Kickoff call",
    "Admins set up",
    "Data imported",
    "Integrations connected",
    "Pilot team trained",
    "Onboarding review",
)


def _onboarding_brief(**changes: Any) -> OnboardingBrief:
    """A brief built by hand, so the plain text can be checked line by line."""
    account = Account(
        id="acc-x",
        name="Brindle Studio",
        arr=543_000,
        tier="Growth",
        owner="Corin Vashti",
        start_date=date(2026, 8, 1),
        renewal_date=date(2027, 2, 1),
        milestones=Milestones(done=2, total=6, overdue=1),
    )
    fields: dict[str, Any] = {
        "account": account,
        "health": Health.AT_RISK,
        "motion": Motion.ONBOARDING,
        "in_onboarding": True,
        "day": 80,
        "window_days": 90,
        "milestones": [
            PlanMilestone(position=index, name=name, status=status)
            for index, (name, status) in enumerate(
                zip(_NAMED_PLAN, [DONE, DONE, OVERDUE, NEXT, UPCOMING, UPCOMING], strict=True),
                start=1,
            )
        ],
        "signals": [],
        "risks": [],
        "summary": "Behind on the plan with 11 days left. No one has logged in for 44 days.",
        "agenda": [AgendaItem(topic="Progress on the plan", detail="2 of 6 steps are done.")],
        "questions": ["Who should be logging in this week?"],
        "suggested_action": "Book a check-in with the onboarding lead to review progress.",
    }
    return OnboardingBrief(**{**fields, **changes})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


# The picker


def test_picker_is_the_onboarding_prep_page(client: TestClient) -> None:
    response = client.get("/onboarding")
    assert response.status_code == 200
    nav_link = '<a class="nav-link" href="/onboarding" aria-current="page">Onboarding prep</a>'
    assert nav_link in response.text
    assert '<h1 class="page-title">Onboarding prep</h1>' in response.text
    assert "coming in a later build" not in response.text


def test_picker_sentence_counts_the_accounts_and_the_ones_behind(client: TestClient) -> None:
    items = _book_onboarding()
    behind = sum(formatting.behind_on_plan(_brief(item.account.id).milestones) for item in items)
    expected = formatting.onboarding_summary(len(items), behind)
    assert len(items) == 8
    assert expected.startswith("8 accounts are in onboarding.")
    assert f'<p class="summary">{expected}</p>' in client.get("/onboarding").text


def test_picker_rows_follow_the_book_order(client: TestClient) -> None:
    items = _book_onboarding()
    rows = _rows(_text(client.get("/onboarding")))
    assert len(rows) == len(items) == 8
    ids = [re.search(r'href="/onboarding/([^"]+)"', row).group(1) for row in rows]
    assert ids == [item.account.id for item in items]


def test_each_row_shows_its_day_plan_words_and_dots(client: TestClient) -> None:
    items = _book_onboarding()
    rows = _rows(_text(client.get("/onboarding")))
    for row, item in zip(rows, items, strict=True):
        brief = _brief(item.account.id)
        statuses = [step.status for step in brief.milestones]
        done = statuses.count(DONE)
        overdue = statuses.count(OVERDUE)
        words = f"{done} of {len(statuses)} done"
        if overdue:
            words += f", {overdue} overdue"
        assert f"Day {brief.day} of {brief.window_days}" in _plain(row)
        assert words in _plain(row)
        dots = re.findall(r'class="plan-dot plan-dot--(\w+)"', row)
        assert dots == [DOT_STATE[status] for status in statuses]
        assert dots.count("done") == done
        assert dots.count("overdue") == overdue
        assert dots.count("open") == len(statuses) - done - overdue


def test_each_prepare_link_names_its_account(client: TestClient) -> None:
    items = _book_onboarding()
    rows = _rows(_text(client.get("/onboarding")))
    for row, item in zip(rows, items, strict=True):
        account_id, name = item.account.id, item.account.name
        link = f'<a href="/onboarding/{account_id}" aria-label="Prepare brief for {name}">'
        assert link + "Prepare brief</a>" in row


def test_the_account_picker_has_both_groups(client: TestClient) -> None:
    book = service.get_book()
    items = service.onboarding_accounts(book)
    markup = client.get("/onboarding").text
    assert '<label class="visually-hidden" for="account-choice">Account</label>' in markup
    assert '<form class="open-form" method="get" action="/onboarding/open"' in markup
    select = _block(markup, r'<select id="account-choice" name="account">(.*?)</select>')
    groups = dict(re.findall(r'<optgroup label="([^"]+)">(.*?)</optgroup>', select, re.S))
    assert list(groups) == ["In onboarding", "Other accounts"]

    option = r'<option value="([^"]+)">'
    in_onboarding = re.findall(option, groups["In onboarding"])
    assert in_onboarding == [item.account.id for item in items]
    others = sorted(
        (item for item in book if item.motion is not Motion.ONBOARDING),
        key=lambda item: item.account.name.casefold(),
    )
    other_ids = re.findall(option, groups["Other accounts"])
    assert other_ids == [item.account.id for item in others]
    assert len(in_onboarding) == 8
    assert len(other_ids) == 32
    assert '<button type="submit" class="button">Open brief</button>' in markup


def test_picker_with_nobody_in_onboarding_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: _AllComplete())
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    response = client.get("/onboarding")
    assert response.status_code == 200
    assert '<p class="summary">No accounts are in onboarding right now.</p>' in response.text
    assert '<table class="onboarding-table">' not in response.text
    assert '<optgroup label="In onboarding">' not in response.text
    assert '<optgroup label="Other accounts">' in response.text
    assert '<form class="open-form" method="get" action="/onboarding/open"' in response.text


# Choosing an account


def test_open_sends_a_valid_id_to_its_brief(client: TestClient) -> None:
    response = client.get("/onboarding/open?account=acc-001", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/acc-001"


@pytest.mark.parametrize("query", ["", "?account=", "?account=acc-999"])
def test_open_sends_a_missing_empty_or_unknown_id_back(client: TestClient, query: str) -> None:
    response = client.get(f"/onboarding/open{query}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding?missing=1"


def test_the_picker_asks_for_a_choice_after_a_missing_one(client: TestClient) -> None:
    markup = client.get("/onboarding?missing=1").text
    alert = '<p class="alert" role="alert">Choose an account to open its brief.</p>'
    assert alert in markup
    assert markup.index(alert) < markup.index(
        '<form class="open-form" method="get" action="/onboarding/open"'
    )
    assert 'role="alert"' not in client.get("/onboarding").text


# The brief


def test_brief_page_shows_the_call_brief(client: TestClient) -> None:
    top = _book_onboarding()[0]
    brief = _brief(top.account.id)
    response = client.get(f"/onboarding/{top.account.id}")
    assert response.status_code == 200
    markup = response.text
    name = top.account.name
    assert _plain(_block(markup, r'<h1 class="page-title">(.*?)</h1>')) == (
        f"{name} onboarding brief"
    )
    assert 'role="note"' not in markup

    facts = re.findall(r"<li>(.*?)</li>", _block(markup, r'<ul class="facts">(.*?)</ul>'), re.S)
    assert [_plain(item) for item in facts] == [
        f"Day {brief.day} of {brief.window_days}",
        brief.health.label,
        f"{web.money(top.account.arr)} ARR",
        top.account.owner,
    ]

    summary = _block(markup, r'<p class="brief-summary">(.*?)</p>')
    # On paper the go-live forecast is one more sentence of the summary, in a print-only span.
    summary = re.sub(r'<span class="print-only">.*?</span>', "", summary, flags=re.S)
    assert _plain(summary) == brief.summary

    plan = _block(markup, r'<ol class="plan">(.*?)</ol>')
    steps = re.findall(
        r'<li class="plan-step plan-step--(\w+)" value="(\d+)">(.*?)</li>', plan, re.S
    )
    assert [status for status, _, _ in steps] == [step.status.value for step in brief.milestones]
    assert [int(value) for _, value, _ in steps] == [step.position for step in brief.milestones]
    for (_, _, body), step in zip(steps, brief.milestones, strict=True):
        assert _plain(body) == f"{step.name} {step.status.label}"
        assert f"/static/icons.svg#status-{step.status.value}" in body
    assert "Steps from the standard onboarding plan." in markup

    agenda = re.findall(r"<li>(.*?)</li>", _block(markup, r'<ol class="agenda">(.*?)</ol>'), re.S)
    assert [_plain(item) for item in agenda] == [
        f"{item.topic} {item.detail}" for item in brief.agenda
    ]

    questions = re.findall(
        r"<li>(.*?)</li>", _block(markup, r'<ul class="questions">(.*?)</ul>'), re.S
    )
    assert [_plain(item) for item in questions] == brief.questions

    table = _block(markup, r'<table class="breakdown">(.*?)</table>')
    labels = re.findall(r'<th scope="row" class="cell-signal">([^<]+)</th>', table)
    assert labels == [SIGNAL_LABELS[kind] for kind in ONBOARDING_SIGNALS]
    assert "Signals to watch" in markup

    assert '<h2 class="section-title" id="next-step-heading">Next step</h2>' in markup
    assert brief.suggested_action in _text(response)


def test_brief_buttons_are_js_only_and_wired(client: TestClient) -> None:
    top = _book_onboarding()[0]
    markup = client.get(f"/onboarding/{top.account.id}").text
    print_button = (
        '<button type="button" class="button js-only" data-print>Print or save as PDF</button>'
    )
    copy_button = (
        '<button type="button" class="button js-only" data-copy-target="#brief-text">'
        "Copy as text</button>"
    )
    assert print_button in markup
    assert copy_button in markup
    assert '<p class="copy-status" role="status" aria-live="polite"></p>' in markup


def test_brief_template_holds_the_plain_text(client: TestClient) -> None:
    top = _book_onboarding()[0]
    brief = _brief(top.account.id)
    markup = client.get(f"/onboarding/{top.account.id}").text
    template = _block(markup, r'<template id="brief-text">(.*?)</template>')
    assert html.unescape(template) == formatting.onboarding_plain_text(brief)


def test_a_brief_for_an_account_outside_onboarding_has_the_note(client: TestClient) -> None:
    outside = next(item for item in service.get_book() if item.motion is not Motion.ONBOARDING)
    response = client.get(f"/onboarding/{outside.account.id}")
    assert response.status_code == 200
    note = _block(response.text, r'<p class="brief-note" role="note">(.*?)</p>')
    assert _plain(note) == (
        f"{outside.account.name} isn't in onboarding right now. "
        "This brief shows its plan for reference."
    )


def test_an_unknown_onboarding_id_is_a_404_with_the_existing_copy(client: TestClient) -> None:
    response = client.get("/onboarding/acc-999")
    assert response.status_code == 404
    assert "<title>Account not found - Wardmind</title>" in response.text
    assert "No account with id acc-999." in _text(response)


def test_the_signals_table_is_the_account_pages_table(client: TestClient) -> None:
    """The brief and the account page draw the same rows with the same bars."""
    top = _book_onboarding()[0]
    brief_html = client.get(f"/onboarding/{top.account.id}").text
    account_html = client.get(f"/accounts/{top.account.id}").text
    row_pattern = r'(<tr[^>]*>\s*<th scope="row" class="cell-signal">([^<]+)</th>.*?</tr>)'

    def rows_by_label(markup: str) -> dict[str, str]:
        table = _block(markup, r'<table class="breakdown">(.*?)</table>')
        return {label: row for row, label in re.findall(row_pattern, table, re.S)}

    on_brief = rows_by_label(brief_html)
    on_account = rows_by_label(account_html)
    assert list(on_brief) == [SIGNAL_LABELS[kind] for kind in ONBOARDING_SIGNALS]
    for label, row in on_brief.items():
        assert row == on_account[label], label


def test_the_pages_keep_the_security_setup(client: TestClient) -> None:
    top = _book_onboarding()[0]
    for path in ["/onboarding", "/onboarding?missing=1", f"/onboarding/{top.account.id}"]:
        response = client.get(path)
        assert response.headers["content-security-policy"] == web.CSP, path
        markup = response.text
        assert not re.search(r"\sstyle\s*=", markup), path
        assert not re.search(r"\son[a-z]+\s*=", markup), path
        for tag in re.findall(r"<script\b[^>]*>", markup, flags=re.IGNORECASE):
            assert re.search(r"\ssrc=", tag), tag


def test_copy_as_text_is_wired_in_app_js() -> None:
    script = (web.STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    assert "[data-copy-target]" in script
    assert "navigator.clipboard.writeText" in script
    assert "Copied." in script
    assert "Your browser blocked copying. The text is selected below" in script


def test_the_plan_status_symbols_are_in_the_sprite() -> None:
    sprite = (web.STATIC_DIR / "icons.svg").read_text(encoding="utf-8")
    for name in ("status-done", "status-overdue", "status-next", "status-upcoming"):
        assert f'id="{name}"' in sprite, name


# Formatting helpers


@pytest.mark.parametrize(
    ("in_onboarding", "behind", "expected"),
    [
        (0, 0, "No accounts are in onboarding right now."),
        (0, 3, "No accounts are in onboarding right now."),
        (1, 0, "1 account is in onboarding. None are behind on their plan."),
        (1, 1, "1 account is in onboarding. 1 is behind on its plan."),
        (2, 0, "2 accounts are in onboarding. None are behind on their plan."),
        (2, 2, "2 accounts are in onboarding. 2 are behind on their plan."),
        (8, 1, "8 accounts are in onboarding. 1 is behind on its plan."),
        (8, 3, "8 accounts are in onboarding. 3 are behind on their plan."),
    ],
)
def test_onboarding_summary(in_onboarding: int, behind: int, expected: str) -> None:
    assert formatting.onboarding_summary(in_onboarding, behind) == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([DONE, DONE, OVERDUE, NEXT, UPCOMING, UPCOMING], "2 of 6 done, 1 overdue"),
        ([DONE, NEXT, UPCOMING], "1 of 3 done"),
        ([DONE, OVERDUE, OVERDUE], "1 of 3 done, 2 overdue"),
        ([OVERDUE], "0 of 1 done, 1 overdue"),
        ([], "0 of 0 done"),
    ],
)
def test_plan_summary(statuses: list[MilestoneStatus], expected: str) -> None:
    assert formatting.plan_summary(_steps(statuses)) == expected


@pytest.mark.parametrize(
    ("statuses", "behind"),
    [([DONE, NEXT], False), ([DONE, OVERDUE, NEXT], True), ([], False)],
)
def test_behind_on_plan_means_an_overdue_milestone(
    statuses: list[MilestoneStatus], behind: bool
) -> None:
    assert formatting.behind_on_plan(_steps(statuses)) is behind


def test_plan_dots_are_done_overdue_or_open() -> None:
    steps = _steps([DONE, OVERDUE, NEXT, UPCOMING, DONE])
    assert formatting.plan_dots(steps) == ["done", "overdue", "open", "open", "done"]


def test_onboarding_plain_text_is_the_copyable_brief() -> None:
    expected = "\n".join(
        [
            "Brindle Studio onboarding brief",
            "Day 80 of 90. At risk. $543,000 ARR. Corin Vashti.",
            "",
            "Behind on the plan with 11 days left. No one has logged in for 44 days.",
            "",
            "The plan",
            "1. Kickoff call: Done",
            "2. Admins set up: Done",
            "3. Data imported: Overdue",
            "4. Integrations connected: Next",
            "5. Pilot team trained: Upcoming",
            "6. Onboarding review: Upcoming",
            "",
            "Agenda for the call",
            "1. Progress on the plan: 2 of 6 steps are done.",
            "",
            "Questions to ask",
            "- Who should be logging in this week?",
            "",
            "Next step: Book a check-in with the onboarding lead to review progress.",
        ]
    )
    assert formatting.onboarding_plain_text(_onboarding_brief()) == expected


def test_onboarding_plain_text_keeps_the_plan_heading_when_there_are_no_steps() -> None:
    text = formatting.onboarding_plain_text(_onboarding_brief(milestones=[]))
    assert "The plan\n\nAgenda for the call" in text


def test_copy_has_a_working_fallback_when_the_clipboard_is_refused(client: TestClient) -> None:
    # Embedded or locked-down browsers refuse navigator.clipboard. The text lives in a <template>,
    # which can't be selected, so a hidden, read-only textarea is the fallback: app.js fills it,
    # tries a selection copy, and if that fails too leaves it showing with the text selected.
    page = client.get(f"/onboarding/{_book_onboarding()[0].account.id}").text
    assert re.search(r'<textarea id="brief-text-fallback"[^>]*\breadonly\b[^>]*\bhidden\b', page)
    script = client.get("/static/js/app.js").text
    assert "navigator.clipboard.writeText" in script
    assert 'execCommand("copy")' in script
    assert "press Ctrl+C" in script


def test_static_files_are_revalidated_so_upgrades_never_run_stale_code(client: TestClient) -> None:
    for path in ("/static/js/app.js", "/static/css/app.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    assert client.get("/onboarding").headers["cache-control"] == "no-store"
