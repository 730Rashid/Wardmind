"""Settings > Scoring: the page, the live preview, saving, refusals, and the markup htmx relies on.

Every test runs against the temp config folder from conftest.py, so saves never reach config/.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import briefs, service
from wardmind.core.config import (
    ScoringConfig,
    SignalRule,
    Thresholds,
    load_scoring_config,
    save_scoring_config,
)
from wardmind.core.connectors.demo import DemoConnector
from wardmind.core.models import SignalKind
from wardmind.web import app as web
from wardmind.web.scoring_form import UNIT_HINTS, form_values

TEMPLATES = Path(web.__file__).parent / "templates" / "settings"
ORIGIN = {"origin": "http://127.0.0.1"}
CHANGE = {"signals.champion_left.weight": "50", "thresholds.at_risk": "30"}
SAVED_NOTICE = (
    '<p class="settings-status" role="status">'
    "Scoring saved. Every screen and Claude now use it.</p>"
)
DEFAULTS_NOTICE = (
    '<p class="settings-status" role="status">Showing the defaults. Your own signals are kept. '
    "Save changes to use them.</p>"
)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "use", "wbr"}


class _Tree(HTMLParser):
    """Every start tag with its attributes and the tags open around it."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.found: list[tuple[str, dict[str, str | None], list[tuple[str, dict[str, Any]]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.found.append((tag, attributes, list(self.stack)))
        if tag not in VOID:
            self.stack.append((tag, attributes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.append((tag, dict(attrs), list(self.stack)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def _walk(markup: str) -> list[Any]:
    parser = _Tree()
    parser.feed(markup)
    return parser.found


def _input(markup: str, name: str) -> dict[str, str | None]:
    matches = [
        attrs for tag, attrs, _ in _walk(markup) if tag == "input" and attrs.get("name") == name
    ]
    assert len(matches) == 1, f"expected one input named {name}"
    return matches[0]


def _output_text(markup: str, name: str) -> str:
    field_id = name.replace(".", "-")
    match = re.search(rf'<output for="{re.escape(field_id)}"[^>]*>([^<]*)</output>', markup)
    assert match, f"no output for {name}"
    return match.group(1)


def _movements(markup: str) -> list[str]:
    """The movement text of each line in the preview's top ten."""
    return re.findall(r'class="mover-move">(?:<svg.*?</svg>)?([^<]*)</span>', markup, re.S)


def _rank_one_name(markup: str) -> str:
    match = re.search(
        r'(?:class="cell-rank num">1</th>|rank-tile"><span class="visually-hidden">Rank </span>1<)'
        r'.*?class="account-link" href="[^"]*">([^<]+)</a>',
        markup,
        re.S,
    )
    assert match, "no rank 1 row"
    return html.unescape(match.group(1))


def _form_with(changes: dict[str, str]) -> dict[str, str]:
    """The saved config as a posted form, with some fields changed."""
    return {**form_values(load_scoring_config()), **changes}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(service, "get_connector", lambda: DemoConnector())
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def config_dir(isolated_config_dir: Path) -> Path:
    return isolated_config_dir


def test_get_shows_all_eight_signal_groups_with_their_values(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    groups = [
        attrs for tag, attrs, _ in _walk(text) if tag == "div" and "signal" == attrs.get("class")
    ]
    assert len(groups) == len(SignalKind) == 8
    defaults = form_values(ScoringConfig())
    for kind in SignalKind:
        assert f'id="signal-{kind.value}-title">{briefs.SIGNAL_LABELS[kind]}</h3>' in text
        assert html.escape(UNIT_HINTS[kind]) in text
        for part in ("weight", "start", "full"):
            name = f"signals.{kind.value}.{part}"
            assert _input(text, name)["value"] == defaults[name]
    weight = _input(text, "signals.usage_trend_30d.weight")
    assert (weight["type"], weight["min"], weight["max"], weight["step"]) == (
        "range",
        "0",
        "50",
        "1",
    )
    assert _output_text(text, "signals.usage_trend_30d.weight") == "25 points"
    assert _input(text, "signals.usage_trend_30d.full")["type"] == "number"
    assert _output_text(text, "arr.weight_pct") == "30%"


def test_get_shows_the_saved_values_and_outputs(client: TestClient, config_dir: Path) -> None:
    save_scoring_config(
        ScoringConfig(
            signals={
                **ScoringConfig().signals,
                SignalKind.CHAMPION_LEFT: SignalRule(weight=1, start=0, full=1),
            },
            thresholds=Thresholds(at_risk=60, watch=25),
        )
    )
    text = client.get("/settings/scoring").text
    assert _input(text, "thresholds.at_risk")["value"] == "60"
    assert _input(text, "thresholds.watch")["value"] == "25"
    assert _output_text(text, "thresholds.at_risk") == "60"
    assert _output_text(text, "signals.champion_left.weight") == "1 point"


def test_the_preview_column_is_a_live_region_under_its_heading(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    section = [attrs for tag, attrs, _ in _walk(text) if tag == "section"]
    (preview,) = [attrs for attrs in section if attrs.get("id") == "scoring-preview"]
    assert preview["aria-live"] == "polite"
    assert preview["aria-labelledby"] == "preview-title"
    assert 'id="preview-title"' in text and "What would change" in text


def test_defaults_view_shows_the_defaults_without_saving(
    client: TestClient, config_dir: Path
) -> None:
    save_scoring_config(ScoringConfig(thresholds=Thresholds(at_risk=60, watch=25)))
    response = client.get("/settings/scoring?defaults=1")
    assert DEFAULTS_NOTICE in response.text
    assert _input(response.text, "thresholds.at_risk")["value"] == "45"
    assert load_scoring_config().thresholds.at_risk == 60


def test_the_saved_notice_shows_once(client: TestClient) -> None:
    assert SAVED_NOTICE in client.get("/settings/scoring?saved=1").text
    assert 'role="status"' not in client.get("/settings/scoring").text


def test_an_unchanged_form_previews_no_change(client: TestClient) -> None:
    response = client.post(
        "/settings/scoring/preview", data=_form_with({}), headers={"HX-Request": "true", **ORIGIN}
    )
    assert response.status_code == 200
    assert "<html" not in response.text  # the partial only
    assert "No change to who is at risk." in response.text
    assert _movements(response.text) == ["No change"] * 10
    assert "<ol" in response.text


def test_a_change_to_the_champion_weight_and_at_risk_moves_accounts(client: TestClient) -> None:
    response = client.post("/settings/scoring/preview", data=_form_with(CHANGE), headers=ORIGIN)
    assert response.status_code == 200
    assert "No change to who is at risk." not in response.text
    movements = _movements(response.text)
    assert len(movements) == 10
    assert any(movement != "No change" for movement in movements)
    assert re.search(
        r"\d+ accounts? at risk now; \d+ accounts? with these changes\.", response.text
    )


def test_watch_at_or_above_at_risk_shows_the_alert_and_no_ranking(client: TestClient) -> None:
    response = client.post(
        "/settings/scoring/preview",
        data=_form_with({"thresholds.at_risk": "30", "thresholds.watch": "30"}),
        headers=ORIGIN,
    )
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "Fix these to see the preview:" in response.text
    assert "Watch has to be below At risk." in response.text
    assert "<ol" not in response.text


def test_a_valid_save_writes_the_config_and_the_book_follows(
    client: TestClient, config_dir: Path
) -> None:
    draft = load_scoring_config().model_copy(deep=True)
    draft.signals[SignalKind.CHAMPION_LEFT] = SignalRule(weight=50, start=0, full=1)
    draft.thresholds = Thresholds(at_risk=30, watch=20)
    response = client.post(
        "/settings/scoring", data=form_values(draft), headers=ORIGIN, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/scoring?saved=1"
    assert load_scoring_config(config_dir / "scoring.yaml") == draft
    assert _rank_one_name(client.get("/").text) == service.get_book()[0].account.name


def test_a_saved_page_reflects_the_new_config(client: TestClient, config_dir: Path) -> None:
    client.post(
        "/settings/scoring", data=_form_with(CHANGE), headers=ORIGIN, follow_redirects=False
    )
    assert SAVED_NOTICE in client.get("/settings/scoring?saved=1").text
    assert _input(client.get("/settings/scoring").text, "thresholds.at_risk")["value"] == "30"


def test_an_invalid_save_is_422_marks_the_field_and_writes_nothing(
    client: TestClient, config_dir: Path
) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    response = client.post(
        "/settings/scoring",
        data=_form_with({"thresholds.at_risk": "30", "thresholds.watch": "50"}),
        headers=ORIGIN,
    )
    assert response.status_code == 422
    assert "Nothing was saved." in response.text
    assert 'id="thresholds-watch-error"' in response.text
    assert "Watch has to be below At risk." in response.text
    watch = _input(response.text, "thresholds.watch")
    assert watch["aria-invalid"] == "true"
    assert watch["aria-describedby"] == "thresholds-watch-error"
    assert watch["value"] == "50"  # what was typed stays in the field
    assert _input(response.text, "thresholds.at_risk").get("aria-invalid") is None
    assert 'role="alert"' in response.text  # the preview says why there is no ranking
    assert (config_dir / "scoring.yaml").read_bytes() == before


def test_a_bad_number_is_marked_on_its_own_field(client: TestClient, config_dir: Path) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    response = client.post(
        "/settings/scoring", data=_form_with({"signals.nps_latest.start": "seven"}), headers=ORIGIN
    )
    assert response.status_code == 422
    assert "Enter a number." in response.text
    start = _input(response.text, "signals.nps_latest.start")
    assert start["aria-invalid"] == "true"
    assert "signals-nps_latest-start-error" in str(start["aria-describedby"]).split()
    assert (config_dir / "scoring.yaml").read_bytes() == before


@pytest.mark.parametrize("path", ["/settings/scoring", "/settings/scoring/preview"])
@pytest.mark.parametrize(
    "headers", [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}]
)
def test_a_cross_site_post_is_refused_and_writes_nothing(
    client: TestClient, config_dir: Path, path: str, headers: dict[str, str]
) -> None:
    before = (config_dir / "scoring.yaml").read_bytes()
    response = client.post(path, data=CHANGE, headers=headers)
    assert response.status_code == 403
    assert (config_dir / "scoring.yaml").read_bytes() == before


def test_the_save_button_is_a_plain_submit_outside_the_htmx_wrapper(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    walk = _walk(text)
    # The header's book switcher has forms of its own; only the page's forms are counted here. Your
    # own signals (Settings > Scoring, below the form) have an add form of their own, in their card;
    # test_custom_signals_settings.py checks that one.
    forms = [
        (attrs, ancestors)
        for tag, attrs, ancestors in walk
        if tag == "form"
        and attrs.get("action") != "/books/switch"
        and not any(
            name == "section" and found.get("id") == "custom-signals" for name, found in ancestors
        )
    ]
    assert len(forms) == 1
    form, _ = forms[0]
    assert form["id"] == "scoring-form"
    assert form["method"] == "post"
    assert form["action"] == "/settings/scoring"
    assert not any(key.startswith("hx-") for key in form)

    wrappers = [attrs for tag, attrs, _ in walk if attrs.get("hx-post") is not None]
    assert [wrapper["hx-post"] for wrapper in wrappers] == ["/settings/scoring/preview"]
    assert wrappers[0]["hx-trigger"] == "input changed delay:300ms, change"
    assert wrappers[0]["hx-include"] == "#scoring-form"
    # The swap targets the preview's body, not the section: an innerHTML swap of the section
    # would delete its heading and leave aria-labelledby pointing at nothing.
    assert wrappers[0]["hx-target"] == "#scoring-preview-body"
    assert wrappers[0]["hx-swap"] == "innerHTML"
    partial = client.post("/settings/scoring/preview", data={}).text
    assert 'id="preview-title"' not in partial
    assert 'id="preview-title"' in client.get("/settings/scoring").text

    # Only the scoring form's own buttons: the header's book switcher has submit buttons too.
    (save,) = [
        (attrs, ancestors)
        for tag, attrs, ancestors in walk
        if tag == "button"
        and attrs.get("type") == "submit"
        and any(name == "form" and found.get("id") == "scoring-form" for name, found in ancestors)
    ]
    save_attrs, save_ancestors = save
    assert save_attrs.get("class") == "button-primary"
    assert any(tag == "form" and attrs.get("id") == "scoring-form" for tag, attrs in save_ancestors)
    assert not any(attrs.get("hx-post") is not None for _, attrs in save_ancestors)


def test_the_reset_link_loads_the_defaults_and_is_not_a_submit(client: TestClient) -> None:
    text = client.get("/settings/scoring").text
    links = [
        attrs for tag, attrs, _ in _walk(text) if tag == "a" and attrs.get("class") == "button"
    ]
    assert [link["href"] for link in links] == ["/settings/scoring?defaults=1"]


def test_the_scoring_templates_have_no_inline_style_or_script() -> None:
    for file in TEMPLATES.rglob("*.html"):
        text = file.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", text), f"{file.name} has a style attribute"
        assert not re.search(r"<script\b(?![^>]*\ssrc=)", text, re.IGNORECASE), file.name
        assert not re.search(r"\son[a-z]+\s*=", text), f"{file.name} has an inline handler"
