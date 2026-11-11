"""Note templates: saved starting text for a note, kept per book and offered on every account's
note form (Settings > Note templates, and "Start from a template" on the account page).

Storage: one JSON file in the open book's data folder (store.root / "note-templates.json"),
written through core/vault.py, so it is encrypted with the rest of the book when encryption is on.
Reads never raise: a missing or unreadable file reads as no templates. Changes hold
store.WRITE_LOCK around load -> change -> save.

A template is filled in by fill(): the placeholders below are replaced with str.split over this
fixed list, one pass, so a value (an account named "{owner}") is never searched again and text in
braces that isn't a placeholder ("{0}", "{account.__class__}") is left as typed. Nothing here calls
str.format or a template engine on the template text.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from wardmind.core import vault
from wardmind.core.journal import MAX_NOTE
from wardmind.core.store import WRITE_LOCK, LocalStore

NOTE_TEMPLATES_FILE = "note-templates.json"
MAX_TEMPLATES = 30  # the most templates one book keeps
MAX_NAME = 60  # characters in a template's name (one line)
MAX_BODY = MAX_NOTE  # characters in a template's text: the same limit as a note
PLACEHOLDERS = ("{account}", "{today}", "{renewal_date}", "{owner}", "{arr}")
NO_RENEWAL = "no renewal date"
_TEMPLATE_ID = r"t-[0-9a-f]{8}"
_TEMPLATE_ID_PATTERN = re.compile(_TEMPLATE_ID)
FULL_MESSAGE = f"You can keep {MAX_TEMPLATES} templates. Delete one first."
NAME_TAKEN_MESSAGE = "A template has that name already. Pick another name."
MISSING_MESSAGE = "That template isn't in this book."

# Ready-made templates for a CSM: (name, text). Each is added once, by name, never twice.
STARTERS: tuple[tuple[str, str], ...] = (
    (
        "Call notes",
        "{account}, {today}\n\nAttendees:\n\nWhat they said:\n\nRisks:\n\nNext steps:",
    ),
    (
        "Quarterly business review",
        "{account}, {today}\n\nGoals they set:\n\nResults:\n\nAdoption:\n\nAsks:\n\nNext steps:",
    ),
    (
        "Onboarding kickoff",
        "{account}, {today}\n\nSuccess criteria:\n\nTimeline:\n\nKey people:\n\nOpen questions:",
    ),
    (
        "Renewal conversation",
        "{account}, renewal on {renewal_date}\n\nDecision maker:\n\nBudget:\n\nConcerns:\n\n"
        "Competitors:\n\nNext steps:",
    ),
    (
        "Escalation",
        "{account}, {today}\n\nWhat happened:\n\nImpact:\n\nOwner:\n\nUpdate by:",
    ),
)


class NoteTemplate(BaseModel):
    id: str = Field(pattern=rf"^{_TEMPLATE_ID}$")  # "t-" + 8 hex characters
    name: str = Field(min_length=1, max_length=MAX_NAME)
    body: str = Field(min_length=1, max_length=MAX_BODY)


class _Saved(BaseModel):
    templates: list[NoteTemplate] = Field(default_factory=list)


_SAVED_ADAPTER: TypeAdapter[_Saved] = TypeAdapter(_Saved)


# --- Validation (plain messages, shown next to the field) ---------------------------------


def clean_name(text: str) -> str:
    """The name with its whitespace (line breaks included) collapsed to single spaces. ValueError
    with a plain message when it is empty, longer than MAX_NAME, or holds a control or hidden
    formatting character."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Give the template a name.")
    if len(cleaned) > MAX_NAME:
        raise ValueError(f"Keep the name under {MAX_NAME} characters.")
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in cleaned):
        raise ValueError("The name can't contain control or hidden formatting characters.")
    return cleaned


def clean_body(text: str) -> str:
    """The text cleaned like a note: \\r\\n and \\r become \\n, then stripped. ValueError when it
    is empty, longer than MAX_BODY, or holds a control character other than \\n and \\t, or a
    hidden formatting character."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise ValueError("Type the text for the template.")
    if len(cleaned) > MAX_BODY:
        raise ValueError(f"Keep the text under {MAX_BODY:,} characters.")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in cleaned):
        raise ValueError("The text can't contain control characters.")
    if any(unicodedata.category(char) == "Cf" for char in cleaned):
        raise ValueError("The text can't contain hidden formatting characters.")
    return cleaned


# --- Reading and changing -----------------------------------------------------------------


def _path(store: LocalStore | None) -> Path:
    """The templates file in the open book's data folder (or in the given store's folder)."""
    chosen = store if store is not None else LocalStore()
    return chosen.root / NOTE_TEMPLATES_FILE


def _load(store: LocalStore | None) -> list[NoteTemplate]:
    """The saved templates, in their order; none when the file is missing, unreadable or wrong."""
    try:
        raw = vault.read_text(_path(store))
        return _SAVED_ADAPTER.validate_json(raw).templates
    except (OSError, ValueError):  # ValidationError and JSON errors are ValueErrors
        return []


def _save(store: LocalStore | None, templates: list[NoteTemplate]) -> None:
    text = _SAVED_ADAPTER.dump_json(_Saved(templates=templates), indent=2).decode("utf-8")
    vault.write_text(_path(store), text + "\n")


def _new_id(taken: set[str]) -> str:
    while True:
        candidate = "t-" + secrets.token_hex(4)
        if candidate not in taken:
            return candidate


def _same_name(a: str, b: str) -> bool:
    """Names match ignoring case, so "call notes" and "Call notes" are one template."""
    return a.casefold() == b.casefold()


def _taken(templates: list[NoteTemplate], name: str, *, except_id: str = "") -> bool:
    return any(item.id != except_id and _same_name(item.name, name) for item in templates)


def is_template_id(text: str) -> bool:
    """True for "t-" followed by 8 lowercase hex characters: the only ids a URL may carry."""
    return _TEMPLATE_ID_PATTERN.fullmatch(text) is not None


def all_templates(store: LocalStore | None = None) -> list[NoteTemplate]:
    """Every template of the open book, in the order the CSM set."""
    return _load(store)


def find(template_id: str, store: LocalStore | None = None) -> NoteTemplate | None:
    """The template with this id, or None (also for an id that isn't in the right form)."""
    if not is_template_id(template_id):
        return None
    return next((item for item in _load(store) if item.id == template_id), None)


def add(name: str, body: str, store: LocalStore | None = None) -> NoteTemplate:
    """Add a template at the end. ValueError with the plain message when the name or text is
    refused, the name is taken, or the book already has MAX_TEMPLATES."""
    cleaned_name, cleaned_body = clean_name(name), clean_body(body)
    with WRITE_LOCK:
        templates = _load(store)
        if len(templates) >= MAX_TEMPLATES:
            raise ValueError(FULL_MESSAGE)
        if _taken(templates, cleaned_name):
            raise ValueError(NAME_TAKEN_MESSAGE)
        added = NoteTemplate(
            id=_new_id({item.id for item in templates}), name=cleaned_name, body=cleaned_body
        )
        templates.append(added)
        _save(store, templates)
    return added


def update(template_id: str, name: str, body: str, store: LocalStore | None = None) -> NoteTemplate:
    """Change a template's name and text. LookupError when the id isn't in this book."""
    cleaned_name, cleaned_body = clean_name(name), clean_body(body)
    with WRITE_LOCK:
        templates = _load(store)
        index = next((i for i, item in enumerate(templates) if item.id == template_id), None)
        if index is None:
            raise LookupError(MISSING_MESSAGE)
        if _taken(templates, cleaned_name, except_id=template_id):
            raise ValueError(NAME_TAKEN_MESSAGE)
        changed = NoteTemplate(id=template_id, name=cleaned_name, body=cleaned_body)
        templates[index] = changed
        _save(store, templates)
    return changed


def delete(template_id: str, store: LocalStore | None = None) -> bool:
    """Remove a template. False when there was none with that id."""
    with WRITE_LOCK:
        templates = _load(store)
        kept = [item for item in templates if item.id != template_id]
        if len(kept) == len(templates):
            return False
        _save(store, kept)
    return True


def move(template_id: str, *, up: bool, store: LocalStore | None = None) -> bool:
    """Swap a template with its neighbour above (up=True) or below. False when it is already at
    that end, or when there is no such template."""
    with WRITE_LOCK:
        templates = _load(store)
        index = next((i for i, item in enumerate(templates) if item.id == template_id), None)
        if index is None:
            return False
        other = index - 1 if up else index + 1
        if not 0 <= other < len(templates):
            return False
        templates[index], templates[other] = templates[other], templates[index]
        _save(store, templates)
    return True


def forget(store: LocalStore | None = None) -> None:
    """Delete the book's templates file (Delete all local data). Never raises: a file that is
    already gone, or can't be removed, is left alone."""
    with WRITE_LOCK:
        try:
            _path(store).unlink(missing_ok=True)
        except OSError:
            return


def missing_starters(store: LocalStore | None = None) -> list[str]:
    """The names of the starter templates this book doesn't have yet."""
    have = [item.name for item in _load(store)]
    return [name for name, _ in STARTERS if not any(_same_name(name, old) for old in have)]


def add_starters(store: LocalStore | None = None) -> int:
    """Add each starter whose name the book doesn't have, as far as the cap allows. Returns how
    many were added (0 when all are there, or when the book is full)."""
    added = 0
    with WRITE_LOCK:
        templates = _load(store)
        for name, body in STARTERS:
            if len(templates) >= MAX_TEMPLATES:
                break
            if _taken(templates, name):
                continue
            templates.append(
                NoteTemplate(id=_new_id({item.id for item in templates}), name=name, body=body)
            )
            added += 1
        if added:
            _save(store, templates)
    return added


# --- Filling a template in ----------------------------------------------------------------


def fill(
    body: str,
    *,
    account: str,
    today: str,
    renewal_date: str | None,
    owner: str,
    arr: str,
) -> str:
    """The template's text with its placeholders replaced. The caller passes the values already
    formatted as the app shows them (today as "Tuesday 21 October", arr as "$184,000"); a missing
    renewal date reads "no renewal date" and a blank owner reads "".
    """
    values = {
        "{account}": account,
        "{today}": today,
        "{renewal_date}": renewal_date or NO_RENEWAL,
        "{owner}": owner or "",
        "{arr}": arr,
    }
    return _substitute(body, values)


def _substitute(text: str, values: Mapping[str, str]) -> str:
    """Replace each placeholder in `text` with its value. Each placeholder is found by str.split
    over the template text only: an inserted value is never searched for placeholders, so an
    account named "{owner}" stays as typed. Anything else in braces is left alone."""
    pieces: list[tuple[bool, str]] = [(True, text)]  # (from the template text?, text)
    for token, value in values.items():
        grown: list[tuple[bool, str]] = []
        for from_template, piece in pieces:
            if not from_template:
                grown.append((False, piece))
                continue
            for index, part in enumerate(piece.split(token)):
                if index:
                    grown.append((False, value))
                if part:
                    grown.append((True, part))
        pieces = grown
    return "".join(piece for _, piece in pieces)
