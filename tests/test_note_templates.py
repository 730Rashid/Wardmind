"""Note templates: the core rules (names, text, the 30 cap, starters, placeholders), the Settings >
Note templates pages (every write, every refusal, escaping, the 404s), and the account page's
"Start from a template" picker (the note box filled in, nothing saved, escaping)."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from wardmind.core import editing, note_templates, service, vault
from wardmind.core.settings import DataSource, Settings, save_settings
from wardmind.core.store import data_dir
from wardmind.web import app as web
from wardmind.web import renewal_pages

ORIGIN = {"origin": "http://127.0.0.1"}
BASE = "http://127.0.0.1"
PAGE = "/settings/note-templates"
ROW = re.compile(r'<li class="note-template" id="(t-[0-9a-f]{8})">')
TEXTAREA = re.compile(r'<textarea id="note-text"[^>]*>(.*?)</textarea>', re.DOTALL)
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style|\sstyle=|\son[a-z]+=", re.IGNORECASE)
TODAY = date.today()


def _text(response: Any) -> str:
    return html.unescape(response.text)


def _post(client: TestClient, path: str, data: dict[str, str] | None = None) -> Any:
    return client.post(path, data=data or {}, headers=ORIGIN, follow_redirects=False)


def _use(source: DataSource) -> None:
    save_settings(Settings(source=source))
    service.reset_connectors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url=BASE)


@pytest.fixture(autouse=True)
def mine() -> None:
    _use(DataSource.MINE)


def _names() -> list[str]:
    return [item.name for item in note_templates.all_templates()]


def _ids_on_page(markup: str) -> list[str]:
    return ROW.findall(markup)


# --- Core: names and text --------------------------------------------------------------------


def test_a_template_gets_an_id_in_the_right_form_and_is_stored_in_the_book() -> None:
    added = note_templates.add("Call notes", "Attendees:")
    assert re.fullmatch(r"t-[0-9a-f]{8}", added.id)
    assert note_templates.find(added.id) == added
    assert (data_dir() / note_templates.NOTE_TEMPLATES_FILE).exists()
    assert "Call notes" in (data_dir() / note_templates.NOTE_TEMPLATES_FILE).read_text("utf-8")


def test_the_file_goes_through_the_vault_like_the_rest_of_the_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[str] = []
    real = vault.write_text

    def record(path: object, text: str) -> None:
        written.append(str(path))
        real(path, text)  # type: ignore[arg-type]

    monkeypatch.setattr(vault, "write_text", record)
    note_templates.add("Escalation", "What happened:")
    assert written and written[0].endswith(note_templates.NOTE_TEMPLATES_FILE)


def test_a_name_of_sixty_characters_is_kept_and_sixty_one_is_refused() -> None:
    assert note_templates.clean_name("n" * 60) == "n" * 60
    with pytest.raises(ValueError, match="under 60 characters"):
        note_templates.clean_name("n" * 61)


def test_a_blank_name_is_refused_and_line_breaks_in_a_name_become_spaces() -> None:
    with pytest.raises(ValueError, match="Give the template a name"):
        note_templates.clean_name("   \n  ")
    assert note_templates.clean_name("Call\nnotes\tfor\rcustomers") == "Call notes for customers"


def test_a_name_with_a_control_or_hidden_character_is_refused() -> None:
    with pytest.raises(ValueError, match="control or hidden"):
        note_templates.clean_name("Call\x07notes")
    with pytest.raises(ValueError, match="control or hidden"):
        note_templates.clean_name("Call​notes")


def test_text_of_four_thousand_characters_is_kept_and_more_is_refused() -> None:
    assert note_templates.clean_body("b" * 4000) == "b" * 4000
    with pytest.raises(ValueError, match="under 4,000 characters"):
        note_templates.clean_body("b" * 4001)


def test_blank_text_is_refused_and_line_endings_are_made_plain() -> None:
    with pytest.raises(ValueError, match="Type the text"):
        note_templates.clean_body("  \n\t ")
    assert note_templates.clean_body("Attendees:\r\n\r\nRisks:\r") == "Attendees:\n\nRisks:"


def test_control_and_hidden_characters_in_text_are_refused_but_tabs_and_breaks_are_kept() -> None:
    assert note_templates.clean_body("a\tb\nc") == "a\tb\nc"
    with pytest.raises(ValueError, match="control characters"):
        note_templates.clean_body("a\x07b")
    with pytest.raises(ValueError, match="hidden formatting"):
        note_templates.clean_body("a​b")


def test_a_taken_name_is_refused_whatever_its_case() -> None:
    note_templates.add("Call notes", "x")
    with pytest.raises(ValueError, match="already"):
        note_templates.add("CALL NOTES", "y")
    assert _names() == ["Call notes"]


def test_the_book_keeps_at_most_thirty_templates() -> None:
    for number in range(note_templates.MAX_TEMPLATES):
        note_templates.add(f"Template {number}", "text")
    before = note_templates.all_templates()
    with pytest.raises(ValueError, match="keep 30 templates"):
        note_templates.add("One too many", "text")
    assert note_templates.all_templates() == before


def test_update_changes_name_and_text_and_keeps_the_id() -> None:
    original = note_templates.add("Call notes", "Attendees:")
    changed = note_templates.update(original.id, "Call notes v2", "Risks:")
    assert changed.id == original.id
    assert note_templates.find(original.id) == changed
    # Keeping its own name is not a clash.
    assert note_templates.update(original.id, "call notes v2", "Next steps:").body == "Next steps:"


def test_update_refuses_another_templates_name_and_an_unknown_id() -> None:
    note_templates.add("Escalation", "x")
    other = note_templates.add("Renewal", "y")
    with pytest.raises(ValueError, match="already"):
        note_templates.update(other.id, "escalation", "y")
    with pytest.raises(LookupError):
        note_templates.update("t-00000000", "Anything", "text")


def test_delete_removes_a_template_and_reports_an_unknown_one() -> None:
    kept = note_templates.add("Keep me", "x")
    gone = note_templates.add("Remove me", "y")
    assert note_templates.delete(gone.id) is True
    assert note_templates.delete(gone.id) is False
    assert [item.id for item in note_templates.all_templates()] == [kept.id]


def test_move_swaps_with_the_neighbour_and_stops_at_the_ends() -> None:
    first = note_templates.add("First", "1")
    second = note_templates.add("Second", "2")
    third = note_templates.add("Third", "3")
    assert note_templates.move(third.id, up=True) is True
    assert [item.name for item in note_templates.all_templates()] == ["First", "Third", "Second"]
    assert note_templates.move(first.id, up=True) is False  # already at the top
    assert note_templates.move(second.id, up=False) is False  # already at the bottom
    assert note_templates.move("t-00000000", up=True) is False
    assert note_templates.move(first.id, up=False) is True
    assert [item.name for item in note_templates.all_templates()] == ["Third", "First", "Second"]


def test_a_damaged_file_reads_as_no_templates_and_nothing_raises() -> None:
    (data_dir() / note_templates.NOTE_TEMPLATES_FILE).write_text("{not json", encoding="utf-8")
    assert note_templates.all_templates() == []
    assert note_templates.find("t-00000000") is None


def test_find_refuses_an_id_in_the_wrong_form() -> None:
    assert note_templates.is_template_id("t-0a1b2c3d")
    for bad in ("t-0A1B2C3D", "t-0a1b2c3", "x-0a1b2c3d", "t-0a1b2c3d/../", "t-0a1b2c3d\n"):
        assert not note_templates.is_template_id(bad)
        assert note_templates.find(bad) is None


# --- Core: starters --------------------------------------------------------------------------


def test_the_starters_are_five_plain_templates_with_placeholders_and_blank_lines() -> None:
    assert [name for name, _ in note_templates.STARTERS] == [
        "Call notes",
        "Quarterly business review",
        "Onboarding kickoff",
        "Renewal conversation",
        "Escalation",
    ]
    for name, body in note_templates.STARTERS:
        assert note_templates.clean_name(name) == name
        assert note_templates.clean_body(body) == body
        assert "\n\n" in body  # blank lines to fill in


def test_add_starters_adds_each_once_and_only_the_missing_ones() -> None:
    note_templates.add("call notes", "my own")  # the same name, another case: already there
    assert note_templates.missing_starters() == [
        "Quarterly business review",
        "Onboarding kickoff",
        "Renewal conversation",
        "Escalation",
    ]
    assert note_templates.add_starters() == 4
    assert note_templates.add_starters() == 0
    assert note_templates.missing_starters() == []
    assert _names().count("Escalation") == 1
    assert _names()[0] == "call notes"  # the CSM's own text is kept as it was


def test_add_starters_stops_at_the_cap() -> None:
    for number in range(note_templates.MAX_TEMPLATES - 2):
        note_templates.add(f"Mine {number}", "x")
    assert note_templates.add_starters() == 2  # Call notes and the review fit, the rest don't
    assert len(note_templates.all_templates()) == note_templates.MAX_TEMPLATES
    assert note_templates.missing_starters() == [
        "Onboarding kickoff",
        "Renewal conversation",
        "Escalation",
    ]


# --- Core: filling a template in ---------------------------------------------------------------


def test_the_placeholders_are_replaced_with_the_given_values() -> None:
    text = note_templates.fill(
        "{account} on {today}, renews {renewal_date}, owner {owner}, ARR {arr}.",
        account="Acme Robotics",
        today="Tuesday 21 October",
        renewal_date="19 Jan 2027",
        owner="Jo Park",
        arr="$184,000",
    )
    assert text == (
        "Acme Robotics on Tuesday 21 October, renews 19 Jan 2027, owner Jo Park, ARR $184,000."
    )


def test_a_missing_renewal_date_and_a_blank_owner_read_as_plain_words() -> None:
    text = note_templates.fill(
        "{renewal_date} / [{owner}]", account="A", today="T", renewal_date=None, owner="", arr="$0"
    )
    assert text == "no renewal date / []"


def test_text_in_braces_that_is_not_a_placeholder_is_left_as_typed() -> None:
    body = "{account.__class__} {0} {unknown} {account[0]} {owner!r} {{account}}"
    text = note_templates.fill(
        body, account="Acme", today="T", renewal_date="R", owner="Jo", arr="$1"
    )
    assert text == "{account.__class__} {0} {unknown} {account[0]} {owner!r} {Acme}"


def test_a_value_that_looks_like_a_placeholder_is_not_filled_in_again() -> None:
    text = note_templates.fill(
        "{account} / {owner}",
        account="Acme {owner} {arr}",
        today="T",
        renewal_date="R",
        owner="Jo",
        arr="$1",
    )
    assert text == "Acme {owner} {arr} / Jo"


def test_the_placeholder_list_is_the_fixed_one() -> None:
    assert note_templates.PLACEHOLDERS == (
        "{account}",
        "{today}",
        "{renewal_date}",
        "{owner}",
        "{arr}",
    )


# --- Settings > Note templates: the page ------------------------------------------------------


def test_the_empty_page_offers_the_add_form_and_the_starters(client: TestClient) -> None:
    response = client.get(PAGE)
    assert response.status_code == 200
    text = _text(response)
    assert "Note templates" in text
    assert "No templates yet" in text
    assert 'action="/settings/note-templates/add"' in response.text
    assert 'action="/settings/note-templates/starters"' in response.text
    assert "Add the starter templates" in text
    assert "{account}" in text and "{renewal_date}" in text  # the placeholders are listed
    assert not INLINE.search(response.text)


def test_adding_a_template_redirects_with_a_fixed_notice_and_lists_it(client: TestClient) -> None:
    response = _post(client, f"{PAGE}/add", {"name": "Check-in", "body": "How is it going?"})
    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?saved=added"
    shown = client.get(f"{PAGE}?saved=added")
    assert "Template added." in _text(shown)
    assert "Check-in" in _text(shown)
    assert len(_ids_on_page(shown.text)) == 1


def test_an_unknown_saved_code_shows_no_notice(client: TestClient) -> None:
    text = _text(client.get(f"{PAGE}?saved=anything-else"))
    assert "anything-else" not in text
    assert "Template added." not in text
    assert "Starter templates added." not in text


def test_a_refused_add_comes_back_with_the_typed_text_and_writes_nothing(
    client: TestClient,
) -> None:
    response = _post(client, f"{PAGE}/add", {"name": "   ", "body": "Keep this text"})
    assert response.status_code == 422
    text = _text(response)
    assert "Give the template a name." in text
    assert "Keep this text</textarea>" in response.text
    assert note_templates.all_templates() == []


def test_a_refused_add_names_the_field_and_keeps_the_name_typed(client: TestClient) -> None:
    response = _post(client, f"{PAGE}/add", {"name": "Keep the name", "body": "   "})
    assert response.status_code == 422
    text = _text(response)
    assert "Type the text for the template." in text
    assert 'value="Keep the name"' in response.text
    assert note_templates.all_templates() == []


def test_a_name_taken_by_another_template_is_refused_on_the_name(client: TestClient) -> None:
    note_templates.add("Escalation", "x")
    response = _post(client, f"{PAGE}/add", {"name": "escalation", "body": "y"})
    assert response.status_code == 422
    assert "A template has that name already." in _text(response)
    assert len(note_templates.all_templates()) == 1


def test_a_full_book_refuses_an_add_on_the_form(client: TestClient) -> None:
    for number in range(note_templates.MAX_TEMPLATES):
        note_templates.add(f"Template {number}", "text")
    response = _post(client, f"{PAGE}/add", {"name": "Extra", "body": "text"})
    assert response.status_code == 422
    assert "You can keep 30 templates. Delete one first." in _text(response)
    assert "Extra" not in _names()
    assert "You can keep 30 templates" in _text(client.get(PAGE))


def test_hostile_text_is_escaped_on_the_page(client: TestClient) -> None:
    hostile = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    _post(client, f"{PAGE}/add", {"name": "<b>Bold</b> name", "body": hostile})
    page = client.get(PAGE)
    assert "<script>alert" not in page.text
    assert "<img src=x" not in page.text
    assert "&lt;script&gt;alert" in page.text
    assert "&lt;b&gt;Bold&lt;/b&gt; name" in page.text


def test_the_starters_are_added_once_from_the_page(client: TestClient) -> None:
    response = _post(client, f"{PAGE}/starters")
    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?saved=starters"
    assert "Starter templates added." in _text(client.get(f"{PAGE}?saved=starters"))
    assert len(note_templates.all_templates()) == len(note_templates.STARTERS)
    # Nothing left to add: no button, and posting again changes nothing.
    assert "Add the starter templates" not in _text(client.get(PAGE))
    again = _post(client, f"{PAGE}/starters")
    assert again.status_code == 303
    assert len(note_templates.all_templates()) == len(note_templates.STARTERS)


def test_starters_on_a_full_book_say_so_and_change_nothing(client: TestClient) -> None:
    for number in range(note_templates.MAX_TEMPLATES):
        note_templates.add(f"Mine {number}", "x")
    response = _post(client, f"{PAGE}/starters")
    assert response.status_code == 422
    assert "You can keep 30 templates. Delete one first." in _text(response)
    assert len(note_templates.all_templates()) == note_templates.MAX_TEMPLATES


def test_the_edit_page_shows_the_form_filled_in(client: TestClient) -> None:
    item = note_templates.add("Renewal call", "Decision maker:")
    response = client.get(f"{PAGE}/{item.id}")
    assert response.status_code == 200
    text = _text(response)
    assert "Edit template" in text
    assert f'action="{PAGE}/{item.id}/edit"' in response.text
    assert 'value="Renewal call"' in response.text
    assert "Decision maker:</textarea>" in response.text


def test_editing_saves_and_redirects_with_the_updated_notice(client: TestClient) -> None:
    item = note_templates.add("Renewal call", "Decision maker:")
    response = _post(client, f"{PAGE}/{item.id}/edit", {"name": "Renewal", "body": "Budget:"})
    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?saved=updated"
    assert note_templates.find(item.id) is not None
    assert note_templates.find(item.id).name == "Renewal"  # type: ignore[union-attr]


def test_a_refused_edit_comes_back_on_the_edit_form_with_the_typed_values(
    client: TestClient,
) -> None:
    item = note_templates.add("Renewal call", "Decision maker:")
    response = _post(client, f"{PAGE}/{item.id}/edit", {"name": "", "body": "Changed text"})
    assert response.status_code == 422
    text = _text(response)
    assert "Give the template a name." in text
    assert "Changed text</textarea>" in response.text
    assert f'action="{PAGE}/{item.id}/edit"' in response.text
    assert note_templates.find(item.id).body == "Decision maker:"  # type: ignore[union-attr]


def test_move_and_delete_post_back_to_the_list(client: TestClient) -> None:
    first = note_templates.add("First", "1")
    second = note_templates.add("Second", "2")
    up = _post(client, f"{PAGE}/{second.id}/up")
    assert up.status_code == 303
    assert up.headers["location"] == f"{PAGE}#{second.id}"
    assert _names() == ["Second", "First"]
    down = _post(client, f"{PAGE}/{second.id}/down")
    assert down.status_code == 303
    assert _names() == ["First", "Second"]
    gone = _post(client, f"{PAGE}/{first.id}/delete")
    assert gone.status_code == 303
    assert gone.headers["location"] == f"{PAGE}?saved=deleted"
    assert _names() == ["Second"]


def test_the_list_disables_move_up_at_the_top_and_move_down_at_the_bottom(
    client: TestClient,
) -> None:
    note_templates.add("Only", "x")
    markup = client.get(PAGE).text
    assert markup.count(" disabled>") == 2  # one row: both moves are off


def test_a_template_id_in_the_wrong_form_is_a_404(client: TestClient) -> None:
    for path in (f"{PAGE}/t-XYZ", f"{PAGE}/add", f"{PAGE}/t-0a1b2c3d4", f"{PAGE}/..%2Fsecret"):
        response = client.get(path)
        assert response.status_code == 404, path
    for action in ("up", "down", "delete", "edit"):
        response = _post(client, f"{PAGE}/t-XYZ/{action}", {"name": "x", "body": "y"})
        assert response.status_code == 404, action


def test_a_valid_id_that_is_not_in_this_book_is_a_404(client: TestClient) -> None:
    missing = "t-0a1b2c3d"
    assert client.get(f"{PAGE}/{missing}").status_code == 404
    assert _post(client, f"{PAGE}/{missing}/up").status_code == 404
    assert _post(client, f"{PAGE}/{missing}/delete").status_code == 404
    assert _post(client, f"{PAGE}/{missing}/edit", {"name": "x", "body": "y"}).status_code == 404


def test_the_list_shows_each_template_name_and_its_text_escaped(client: TestClient) -> None:
    item = note_templates.add("Plain", "Line one\n<em>not markup</em>")
    markup = client.get(PAGE).text
    assert ROW.findall(markup) == [item.id]
    assert "&lt;em&gt;not markup&lt;/em&gt;" in markup
    assert "<em>not markup</em>" not in markup


# --- The account page's "Start from a template" picker -------------------------------------


def _account(
    name: str = "Acme Robotics", renewal: date | None = None, owner: str = "Jo Park"
) -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": name,
            "arr": "$184,000",
            "owner": owner,
            "renewal_date": (renewal or TODAY + timedelta(days=200)).isoformat(),
        },
        today=TODAY,
    )
    service.reset_connectors()
    return account.id


def _note_box(client: TestClient, account_id: str, query: str = "") -> str:
    response = client.get(f"/accounts/{account_id}{query}")
    assert response.status_code == 200
    return response.text


def _note_text(markup: str) -> str:
    match = TEXTAREA.search(markup)
    assert match is not None, "the notes box has no textarea"
    return html.unescape(match.group(1))


def test_without_templates_the_account_page_has_no_picker(client: TestClient) -> None:
    account_id = _account()
    markup = _note_box(client, account_id)
    assert "Start from a template" not in markup
    assert _note_text(markup) == ""


def test_the_picker_offers_every_template_in_a_get_form(client: TestClient) -> None:
    account_id = _account()
    first = note_templates.add("Call notes", "x")
    second = note_templates.add("Escalation", "y")
    markup = _note_box(client, account_id)
    assert "Start from a template" in markup
    assert f'<option value="{first.id}"' in markup
    assert f'<option value="{second.id}"' in markup
    assert f'method="get" action="/accounts/{account_id}#notes"' in markup
    assert 'name="template"' in markup
    assert ">Use template</button>" in markup


def test_a_chosen_template_fills_the_box_with_the_account_details(client: TestClient) -> None:
    renewal = TODAY + timedelta(days=200)
    account_id = _account(name="Acme Robotics", renewal=renewal, owner="Jo Park")
    item = note_templates.add(
        "Check-in",
        "{account}, {today}, renews {renewal_date}, owner {owner}, ARR {arr}\n\nRisks:",
    )
    markup = _note_box(client, account_id, f"?template={item.id}#notes")
    expected = (
        f"Acme Robotics, {web.format_today(TODAY)}, renews "
        f"{renewal_pages.renewal_day(renewal)}, owner Jo Park, ARR $184,000\n\nRisks:"
    )
    assert _note_text(markup) == expected
    assert f'<option value="{item.id}" selected>' in markup
    assert "Filled in from" in markup
    assert "Nothing is saved until you add the note." in markup


def test_choosing_a_template_saves_no_note(client: TestClient) -> None:
    account_id = _account()
    item = note_templates.add("Call notes", "Attendees:")
    _note_box(client, account_id, f"?template={item.id}")
    assert service.account_journal(account_id).notes == []


def test_a_template_text_with_markup_shows_escaped_in_the_box(client: TestClient) -> None:
    account_id = _account()
    item = note_templates.add("Hostile", '<script>alert("x")</script> {account}')
    markup = _note_box(client, account_id, f"?template={item.id}")
    assert "<script>alert" not in markup
    assert "&lt;script&gt;alert" in markup
    assert _note_text(markup).startswith('<script>alert("x")</script> Acme Robotics')


def test_an_unknown_or_badly_formed_template_leaves_the_box_empty(client: TestClient) -> None:
    account_id = _account()
    note_templates.add("Call notes", "Attendees:")
    for query in ("?template=t-0a1b2c3d", "?template=../../etc", "?template=", "?template=%3Cb%3E"):
        response = client.get(f"/accounts/{account_id}{query}")
        assert response.status_code == 200, query
        assert _note_text(response.text) == "", query
        assert "Filled in from" not in response.text


def test_the_template_text_never_appears_in_a_link_or_the_address(client: TestClient) -> None:
    account_id = _account()
    item = note_templates.add("Secret plan", "Discount of forty percent")
    markup = _note_box(client, account_id, f"?template={item.id}")
    assert "Discount of forty percent" in _note_text(markup)
    assert "forty percent" not in re.sub(r"<textarea[^>]*>.*?</textarea>", "", markup, flags=re.S)


def test_a_filled_box_is_not_saved_until_the_note_is_added(client: TestClient) -> None:
    account_id = _account()
    item = note_templates.add("Call notes", "Attendees:")
    _note_box(client, account_id, f"?template={item.id}")
    assert service.account_journal(account_id).notes == []
    posted = _post(
        client, f"/accounts/{account_id}/notes", {"kind": "note", "text": "Attendees: Sam"}
    )
    assert posted.status_code == 303
    assert [note.text for note in service.account_journal(account_id).notes] == ["Attendees: Sam"]


def test_delete_all_local_data_removes_the_templates(client: TestClient) -> None:
    note_templates.add("Call notes", "Attendees:")
    response = _post(client, "/settings/privacy/delete-all")
    assert response.status_code == 303
    assert note_templates.all_templates() == []
    assert not (data_dir() / note_templates.NOTE_TEMPLATES_FILE).exists()


def test_forgetting_templates_that_are_already_gone_is_quiet() -> None:
    note_templates.forget()
    note_templates.add("Only one", "x")
    note_templates.forget()
    note_templates.forget()
    assert note_templates.all_templates() == []
