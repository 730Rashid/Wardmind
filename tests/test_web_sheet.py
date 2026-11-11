"""The negotiation sheet at /accounts/{id}/renewal-sheet and its link in the account's aside.

The facts on the sheet are worked out in core/negotiation.py (tested in test_negotiation.py). Here
the route, the sections and their order, the empty states, the words for the call and the
disagreement, the money, the footnote, the link in the aside and the rules for the markup are
checked. Some tests build the sheet by hand to pin an exact set of facts; the rest read the demo
book, so their expected values are computed from the same data the page reads."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import negotiation, service
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.forecast import Disagreement
from wardmind.core.journal import CallKind, OutcomeKind, TouchKind
from wardmind.core.models import Health, Motion, ScoredAccount, Signal, SignalKind
from wardmind.core.negotiation import LastRenewal, LikelyAsk, NegotiationSheet, RecentContact
from wardmind.web import app as web
from wardmind.web import formatting

TEMPLATE = Path(web.TEMPLATES_DIR) / "renewal_sheet.html"
PARTIAL = Path(web.TEMPLATES_DIR) / "accounts" / "_sheet_link.html"
CSS = Path(web.STATIC_DIR) / "css" / "app.css"  # the sheet rules were merged into it
DEMO_ID = "acc-014"  # Greyholt Studio: a busy renewal in the demo book
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
FOOTNOTE = (
    "Seat counts and price per seat aren't in the data Wardmind reads, "
    "so this uses ARR and the share of seats in use."
)
SECTION_HEADINGS = [
    "The contract",
    "Where you're strong",
    "Where you're exposed",
    "What they may ask for",
    "Recent contact",
    "Open follow-ups",
]


class _LowSeats(DemoConnector):
    """The demo book with every account at 40% of its seats in use, so unused seats are there to
    be counted and only the route decides whether they are shown."""

    def list_signals(self, account_id: str | None = None) -> list[Signal]:
        return [
            signal.model_copy(update={"value": 40.0})
            if signal.kind is SignalKind.SEAT_UTILISATION
            else signal
            for signal in super().list_signals(account_id)
        ]


def _text(response: Any) -> str:
    """Page text with entities decoded, since Jinja escapes apostrophes in variables."""
    return html.unescape(response.text)


def _plain(fragment: str) -> str:
    """Text of a fragment: tags removed, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _section(markup: str, labelled_by: str) -> str:
    match = re.search(
        rf'<section class="[^"]*" aria-labelledby="{labelled_by}">(.*?)</section>', markup, re.S
    )
    assert match is not None, labelled_by
    return match.group(1)


def _items(section: str) -> list[str]:
    """The plain text of each list item in a section, in order."""
    return [_plain(item) for item in re.findall(r"<li>(.*?)</li>", section, re.S)]


def _headings(markup: str) -> list[str]:
    return [
        _plain(text)
        for text in re.findall(r'<h2 class="section-title"[^>]*>(.*?)</h2>', markup, re.S)
    ]


def _facts(markup: str) -> list[str]:
    """The plain text of the header's and the contract's fact lists."""
    return [
        _plain(block)
        for block in re.findall(r'<ul class="facts sheet-facts">(.*?)</ul>', markup, re.S)
    ]


def _book_account(account_id: str) -> ScoredAccount:
    found = service.find_account(service.get_book(), account_id)
    assert found is not None, account_id
    return found


def _first_with(motion: Motion) -> ScoredAccount:
    return next(item for item in service.get_book() if item.motion is motion)


def _sheet(**changes: Any) -> NegotiationSheet:
    """A sheet built by hand, so an exact set of facts can be checked on the page."""
    fields: dict[str, Any] = {
        "account_id": "acc-x",
        "name": "Brindle Studio",
        "owner": "Corin Vashti",
        "arr": 543_000.0,
        "renewal_date": date(2027, 1, 19),
        "days_to_renewal": 69,
        "health": Health.AT_RISK,
        "motion": Motion.RENEWAL,
        "risk": 60.0,
        "customer_years": 3,
        "seats_used": 62,
        "unused_arr": 31_200.0,
        "last_renewal": LastRenewal(
            kind=OutcomeKind.EXPANDED,
            renewal_date=date(2026, 1, 5),
            decided_on=date(2026, 1, 5),
            arr_before=120_000.0,
            arr_after=150_000.0,
        ),
        "call": CallKind.LIKELY,
        "disagreement": Disagreement.CALL_HIGHER,
        "strengths": ["They expanded at the last renewal."],
        "exposures": ["Champion has left the company."],
        "asks": [
            LikelyAsk(ask="Fewer seats at a lower price", because="Only 62% of seats are in use.")
        ],
        "recent": [
            RecentContact(happened_on=date(2026, 11, 2), kind_label="Call", text="Renewal options")
        ],
        "follow_ups": ["Send the usage report"],
    }
    return NegotiationSheet(**{**fields, **changes})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def _render_with(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, sheet: NegotiationSheet
) -> str:
    """The page for `sheet`, whatever the account. The route builds the sheet from the core
    function, so that function is replaced for this test only."""
    monkeypatch.setattr(negotiation, "negotiation_sheet", lambda *args, **kwargs: sheet)
    response = client.get(f"/accounts/{DEMO_ID}/renewal-sheet")
    assert response.status_code == 200
    return _text(response)


# The route


def test_the_sheet_is_the_my_book_page_for_the_account(client: TestClient) -> None:
    response = client.get(f"/accounts/{DEMO_ID}/renewal-sheet")
    assert response.status_code == 200
    markup = _text(response)
    name = _book_account(DEMO_ID).account.name
    assert f"<title>{name} renewal - Wardmind</title>" in markup
    assert '<a class="nav-link" href="/" aria-current="page">My book</a>' in markup
    assert '<h1 class="page-title">Renewal call prep</h1>' in markup


def test_an_unknown_account_is_a_not_found_page(client: TestClient) -> None:
    response = client.get("/accounts/acc-nope/renewal-sheet")
    assert response.status_code == 404
    markup = _text(response)
    assert "<title>Account not found - Wardmind</title>" in markup
    assert "No account with id acc-nope." in markup


def test_the_back_link_and_the_print_button_stay_off_paper(client: TestClient) -> None:
    markup = re.sub(r">\s+<", "><", _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet")))
    assert (
        f'<p class="brief-back no-print"><a href="/accounts/{DEMO_ID}">Back to the account</a></p>'
        in markup
    )
    assert (
        '<div class="brief-actions no-print">'
        '<button type="button" class="button js-only" data-print>Print or save as PDF</button>'
        "</div>"
    ) in markup


def test_the_sections_come_in_order_and_the_footnote_is_last(client: TestClient) -> None:
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    assert _headings(markup) == SECTION_HEADINGS
    header = markup.index('<h1 class="page-title">')
    assert header < markup.index("The contract")
    assert markup.index("The contract") < markup.index("Where you're strong")
    assert markup.index("What they may ask for") < markup.index("Recent contact")
    assert markup.index("Open follow-ups") < markup.index(FOOTNOTE)


def test_the_header_names_the_account_its_renewal_its_owner_and_its_health(
    client: TestClient,
) -> None:
    item = _book_account(DEMO_ID)
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    assert f'<span class="sheet-name">{item.account.name}</span>' in markup
    facts = _facts(markup)[0]
    days = (item.account.renewal_date - date.today()).days
    renewal = formatting.long_date(item.account.renewal_date)
    assert f"Renews on {renewal}, in {days} days" in facts
    assert item.account.owner in facts
    assert f"Wardmind's health {item.health.label}" in facts
    assert f'<span class="health health--{item.health.value}">' in markup


def test_the_demo_renewal_reads_in_days_and_the_arr_has_separators(client: TestClient) -> None:
    item = _book_account(DEMO_ID)
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    assert f"${item.account.arr:,.0f} ARR" in _facts(markup)[0]


@pytest.mark.parametrize(
    ("days", "phrase"),
    [
        (69, "Renews on 19 January 2027, in 69 days"),
        (1, "Renews on 19 January 2027, tomorrow"),
        (0, "Renews on 19 January 2027, today"),
        (-1, "Renewal date passed 1 day ago, 19 January 2027"),
        (-3, "Renewal date passed 3 days ago, 19 January 2027"),
    ],
)
def test_the_renewal_line_reads_the_days_in_words(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, days: int, phrase: str
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(days_to_renewal=days))
    assert phrase in _facts(markup)[0]


def test_a_call_is_named_in_words(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    markup = _render_with(monkeypatch, client, _sheet(call=CallKind.LIKELY))
    assert '<p class="sheet-call">Your call: Likely.</p>' in markup


def test_no_call_says_so_and_shows_no_disagreement(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(call=None, disagreement=None))
    assert '<p class="sheet-call">You haven\'t called this renewal.</p>' in markup
    assert "sheet-disagreement" not in markup


@pytest.mark.parametrize("disagreement", [Disagreement.CALL_HIGHER, Disagreement.CALL_LOWER])
def test_a_disagreement_is_shown_in_full_under_the_call(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, disagreement: Disagreement
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(disagreement=disagreement))
    assert f'<p class="sheet-disagreement">{disagreement.text}</p>' in markup


def test_a_call_recorded_in_the_journal_is_the_call_line(client: TestClient) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    service.get_journal().make_call(
        DEMO_ID, CallKind.COMMIT, _book_account(DEMO_ID), now=now, today=date.today()
    )
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    assert '<p class="sheet-call">Your call: Commit.</p>' in markup


# The contract


def test_the_contract_lists_arr_seats_unused_money_and_tenure(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet())
    assert _facts(markup)[1] == (
        "$543,000 ARR Seats in use: 62% About $31,200 pays for unused seats A customer for 3 years"
    )


def test_money_over_a_million_keeps_its_separators(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    sheet = _sheet(arr=1_250_000.0, unused_arr=312_500.0)
    markup = _render_with(monkeypatch, client, sheet)
    contract = _facts(markup)[1]
    assert "$1,250,000 ARR" in contract
    assert "About $312,500 pays for unused seats" in contract


@pytest.mark.parametrize(
    ("years", "sentence"),
    [
        (0, "A customer for less than a year"),
        (1, "A customer for 1 year"),
        (7, "A customer for 7 years"),
    ],
)
def test_tenure_is_said_in_years_or_less_than_a_year(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, years: int, sentence: str
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(customer_years=years))
    assert sentence in _facts(markup)[1]


def test_seats_unused_money_and_tenure_are_left_out_when_unknown(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    sheet = _sheet(seats_used=None, unused_arr=None, customer_years=None)
    contract = _facts(_render_with(monkeypatch, client, sheet))[1]
    assert contract == "$543,000 ARR"


def test_the_last_renewal_gives_its_kind_decided_date_and_arr_before_and_after(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet())
    assert _plain(
        re.search(r"<p>(Last renewal:.*?)</p>", markup, re.S).group(1)  # type: ignore[union-attr]
    ) == ("Last renewal: Expanded, decided 5 January 2026. ARR went from $120,000 to $150,000.")


def test_the_last_renewal_without_an_arr_after_gives_the_arr_it_was(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    last = LastRenewal(
        kind=OutcomeKind.RENEWED,
        renewal_date=date(2026, 1, 5),
        decided_on=date(2026, 1, 3),
        arr_before=120_000.0,
        arr_after=None,
    )
    markup = _render_with(monkeypatch, client, _sheet(last_renewal=last))
    assert "Last renewal: Renewed, decided 3 January 2026. ARR was $120,000." in _plain(markup)


def test_no_past_renewal_is_said_plainly(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(last_renewal=None))
    assert "No past renewal recorded." in markup
    assert "Last renewal:" not in markup


# Strengths, exposures, asks


def test_strengths_and_exposures_list_their_sentences_in_order(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    sheet = _sheet(
        strengths=["Their seats are full.", "A customer for 3 years.", "No risk signals."],
        exposures=["Usage is down 30%.", "No contact logged in 40 days."],
    )
    markup = _render_with(monkeypatch, client, sheet)
    assert _items(_section(markup, "strong-heading")) == sheet.strengths
    assert _items(_section(markup, "exposed-heading")) == sheet.exposures


def test_no_strengths_says_nothing_stands_out(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(strengths=[]))
    strong = _section(markup, "strong-heading")
    assert "Nothing stands out in your favour yet." in _plain(strong)
    assert "<li>" not in strong


def test_no_exposures_says_none_were_found(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(exposures=[]))
    exposed = _section(markup, "exposed-heading")
    assert "No exposures found." in _plain(exposed)
    assert "<li>" not in exposed


def test_each_ask_is_bold_and_its_because_follows_it(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    asks = [
        LikelyAsk(ask="Fewer seats at a lower price", because="Only 62% of seats are in use."),
        LikelyAsk(
            ask="A credit or discount for the open issues",
            because="2 severity-1 tickets are still open.",
        ),
    ]
    markup = _render_with(monkeypatch, client, _sheet(asks=asks))
    section = _section(markup, "asks-heading")
    assert (
        "<li><strong>Fewer seats at a lower price.</strong> Only 62% of seats are in use.</li>"
        in section
    )
    assert _items(section) == [
        "Fewer seats at a lower price. Only 62% of seats are in use.",
        "A credit or discount for the open issues. 2 severity-1 tickets are still open.",
    ]


def test_no_likely_asks_says_so_plainly(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(asks=[]))
    section = _section(markup, "asks-heading")
    assert "No likely asks from what Wardmind can see." in _plain(section)
    assert "<li>" not in section


# Recent contact and follow-ups


def test_recent_contact_gives_the_date_the_kind_and_the_text(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet())
    section = _section(markup, "recent-heading")
    assert (
        '<li><span class="sheet-touch-when"><time datetime="2026-11-02">2 November 2026</time>, '
        "call:</span> Renewal options</li>"
    ) in section


def test_no_recent_contact_says_so_plainly(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(recent=[]))
    section = _section(markup, "recent-heading")
    assert "No contact logged yet." in _plain(section)
    assert "<time" not in section


def test_open_follow_ups_are_listed_in_order(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    follow_ups = ["Send the usage report", "Check the invoice with billing"]
    markup = _render_with(monkeypatch, client, _sheet(follow_ups=follow_ups))
    assert _items(_section(markup, "follow-heading")) == follow_ups


def test_no_open_follow_ups_says_so_plainly(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet(follow_ups=[]))
    section = _section(markup, "follow-heading")
    assert "No open follow-ups." in _plain(section)
    assert "<li>" not in section


def test_the_footnote_says_what_the_data_lacks(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    markup = _render_with(monkeypatch, client, _sheet())
    assert (
        _plain(
            re.search(r'<p class="section-note sheet-footnote">(.*?)</p>', markup, re.S).group(1)
        )
        == FOOTNOTE
    )  # type: ignore[union-attr]


def test_a_logged_call_and_a_follow_up_reach_the_sheet(client: TestClient) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    today = date.today()
    service.get_journal().add_activity(
        DEMO_ID,
        TouchKind.CALL,
        "Walked through the renewal options with finance",
        today - timedelta(days=2),
        subject="Renewal options",
        now=now,
        today=today,
    )
    service.get_journal().add_follow_up(
        DEMO_ID,
        "Send the seat usage report to finance",
        today + timedelta(days=3),
        now=now,
        today=today,
    )
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    assert "Renewal options" in _plain(_section(markup, "recent-heading"))
    assert _items(_section(markup, "follow-heading")) == ["Send the seat usage report to finance"]


# The account's aside


def test_the_aside_links_to_the_sheet_for_a_renewal(client: TestClient) -> None:
    item = _first_with(Motion.RENEWAL)
    markup = _text(client.get(f"/accounts/{item.account.id}"))
    link = f'<a href="/accounts/{item.account.id}/renewal-sheet">Open the negotiation sheet</a>'
    assert link in markup
    assert '<h2 class="section-title" id="sheet-link-heading">Renewal call</h2>' in markup
    assert "Strengths, exposures and what they may ask for, on one printable page." in markup


@pytest.mark.parametrize("motion", [Motion.ONBOARDING, Motion.CHURN])
def test_the_aside_has_no_sheet_link_outside_a_renewal(client: TestClient, motion: Motion) -> None:
    item = _first_with(motion)
    markup = _text(client.get(f"/accounts/{item.account.id}"))
    assert "Open the negotiation sheet" not in markup
    assert "renewal-sheet" not in markup
    assert 'id="sheet-link-heading"' not in markup


def test_an_onboarding_account_gets_no_unused_seat_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: _LowSeats())
    seen: dict[str, Any] = {}
    real = negotiation.negotiation_sheet

    def spy(*args: Any, **kwargs: Any) -> NegotiationSheet:
        seen["shelfware"] = args[4]
        return real(*args, **kwargs)

    monkeypatch.setattr(negotiation, "negotiation_sheet", spy)
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    onboarding = _first_with(Motion.ONBOARDING)
    assert client.get(f"/accounts/{onboarding.account.id}/renewal-sheet").status_code == 200
    assert seen["shelfware"] is None


def test_a_renewal_account_with_unused_seats_gets_the_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "get_connector", lambda: _LowSeats())
    client = TestClient(web.create_app(), base_url="http://127.0.0.1")
    renewal = _first_with(Motion.RENEWAL)
    markup = _text(client.get(f"/accounts/{renewal.account.id}/renewal-sheet"))
    assert "pays for unused seats" in markup
    assert "Seats in use: 40%" in markup


# Markup and files


def test_the_sheet_has_no_inline_styles_scripts_or_handlers(client: TestClient) -> None:
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    body = markup[markup.index('<div class="renewal-sheet">') : markup.index("</main>")]
    assert INLINE.search(body) is None
    assert INLINE.search(TEMPLATE.read_text(encoding="utf-8")) is None


def test_the_sheet_uses_no_arrows(client: TestClient) -> None:
    markup = _text(client.get(f"/accounts/{DEMO_ID}/renewal-sheet"))
    assert "→" not in markup
    assert "→" not in TEMPLATE.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [TEMPLATE, PARTIAL, CSS, Path(__file__)])
def test_the_new_files_use_lf_line_endings(path: Path) -> None:
    assert b"\r" not in path.read_bytes()
