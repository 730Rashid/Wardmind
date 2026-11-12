"""Settings > Scoring > Your own signals: the form's field names, how a posted form becomes a
config.CustomSignal, the messages, and the id a new signal is given.

The id is made from the name once, when the signal is added, and never changes after: readings
are stored under "custom:<id>", so renaming a signal keeps its readings. Removing a signal takes it
off the list only; adding a signal with the same name later gives the same id again, so its old
readings count again. Pure: no I/O.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from wardmind.core.config import (
    DEFAULT_CUSTOM_STALE_DAYS,
    MAX_CUSTOM_NAME,
    MAX_CUSTOM_SIGNALS,
    MAX_CUSTOM_TEXT,
    MAX_CUSTOM_UNIT,
    CustomSignal,
    SignalRule,
)
from wardmind.core.scoring import format_number
from wardmind.web.scoring_form import NOT_A_NUMBER, RANGE_0_TO_100, WHOLE_DAYS

FORM_ERROR = "form"  # the key for a problem no single field owns

NAME = "custom.name"
UNIT = "custom.unit"
DIRECTION = "custom.direction"
START = "custom.start"
FULL = "custom.full"
WEIGHT = "custom.weight"
QUESTION = "custom.question"
ACTION = "custom.action"
STALE_DAYS = "custom.stale_days"
FIELD_NAMES: tuple[str, ...] = (
    NAME,
    UNIT,
    DIRECTION,
    START,
    FULL,
    WEIGHT,
    QUESTION,
    ACTION,
    STALE_DAYS,
)

LOWER = "lower"  # lower numbers are worse: risk starts high and falls to the maximum
HIGHER = "higher"  # higher numbers are worse: risk starts low and rises to the maximum

MAX_STALE_DAYS = 365  # config.CustomSignal.stale_days allows 1 to 365
DEFAULT_WEIGHT = 10
ID_MAX = 40  # models.CUSTOM_ID: a letter, then up to 39 more letters, digits or underscores

# The field limits the form shows: the maxlength of each box, and the days a reading may age.
LIMITS: dict[str, int] = {
    "name": MAX_CUSTOM_NAME,
    "unit": MAX_CUSTOM_UNIT,
    "text": MAX_CUSTOM_TEXT,
    "days": MAX_STALE_DAYS,
}

NAME_REQUIRED = "Enter a name for the signal."
NAME_TOO_LONG = f"Use {MAX_CUSTOM_NAME} characters or fewer."
NAME_NEEDS_CHARACTERS = "The name needs at least one letter or number (A to Z, 0 to 9)."
NAME_TAKEN = "You already have a signal with this name. Names have to be different."
UNIT_TOO_LONG = f"Use {MAX_CUSTOM_UNIT} characters or fewer."
TEXT_TOO_LONG = f"Use {MAX_CUSTOM_TEXT} characters or fewer."
DIRECTION_REQUIRED = "Choose which way is worse."
LOWER_NEEDS_MAX_BELOW_START = (
    "Lower numbers are worse, so the maximum has to be below where risk starts."
)
HIGHER_NEEDS_MAX_ABOVE_START = (
    "Higher numbers are worse, so the maximum has to be above where risk starts."
)
DAYS_RANGE = f"Use from 1 to {MAX_STALE_DAYS} days."
FULL_MESSAGE = (
    f"You have {MAX_CUSTOM_SIGNALS} of your own signals, the most Wardmind keeps. "
    "Remove one to add another."
)


@dataclass(frozen=True)
class SignalLine:
    """One signal in the list on Settings > Scoring: its name, its range and its reading rules."""

    id: str
    name: str
    rule: str  # "Risk starts at 500 calls, maxed at 50 calls, up to 30 points."
    meta: str  # "Lower numbers are worse. A reading is out of date after 30 days."


def form_values(signal: CustomSignal | None) -> dict[str, str]:
    """The text each field shows for `signal`, or the blank form with its defaults for a new one."""
    if signal is None:
        return {
            NAME: "",
            UNIT: "",
            DIRECTION: "",
            START: "",
            FULL: "",
            WEIGHT: str(DEFAULT_WEIGHT),
            QUESTION: "",
            ACTION: "",
            STALE_DAYS: str(DEFAULT_CUSTOM_STALE_DAYS),
        }
    rule = signal.rule
    return {
        NAME: signal.name,
        UNIT: signal.unit,
        DIRECTION: LOWER if rule.full < rule.start else HIGHER,
        START: _text(rule.start),
        FULL: _text(rule.full),
        WEIGHT: _text(rule.weight),
        QUESTION: signal.question,
        ACTION: signal.action,
        STALE_DAYS: str(signal.stale_days),
    }


def typed(fields: Mapping[str, str]) -> dict[str, str]:
    """The posted values of this form, as typed, so a refused form can show them again."""
    return {name: fields[name] for name in FIELD_NAMES if name in fields}


def index_of(custom: Sequence[CustomSignal], signal_id: str) -> int | None:
    """Where the signal with this id is in the list, or None when this book has no such signal."""
    for index, signal in enumerate(custom):
        if signal.id == signal_id:
            return index
    return None


def signal_id(name: str, taken: Iterable[str] = ()) -> str:
    """The id a new signal gets from its name. Lower case; each run of characters that are not a
    letter or a digit becomes one "_"; the ends are trimmed; "s_" goes in front when the result does
    not start with a letter; it is cut to fit models.CUSTOM_ID. If `taken` already has it, "_2",
    "_3" and so on are added, the stem cut again to fit. The same name always gives the same id."""
    taken_set = set(taken)
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "signal"
    if not slug[0].isalpha():
        slug = f"s_{slug}"
    base = slug[:ID_MAX].rstrip("_")
    candidate, number = base, 1
    while candidate in taken_set:
        number += 1
        suffix = f"_{number}"
        candidate = base[: ID_MAX - len(suffix)].rstrip("_") + suffix
    return candidate


def parse_signal(
    form: Mapping[str, str],
    others: Sequence[CustomSignal],
    *,
    current: CustomSignal | None = None,
) -> tuple[CustomSignal | None, dict[str, str]]:
    """The signal the form describes, or None with a message for each field that is wrong.

    `others` are the book's other signals (not the one being edited): a name one of them has is
    refused. `current` is the signal being edited, whose id is kept; for a new one (None) the id is
    made from the name here. Every field is checked in one pass, so all the messages show at once.
    """
    errors: dict[str, str] = {}
    name = _line(form.get(NAME, ""))
    unit = _line(form.get(UNIT, ""))
    question = _line(form.get(QUESTION, ""))
    action = _line(form.get(ACTION, ""))
    direction = form.get(DIRECTION, "")

    taken = {other.name.casefold() for other in others}
    if not name:
        errors[NAME] = NAME_REQUIRED
    elif len(name) > MAX_CUSTOM_NAME:
        errors[NAME] = NAME_TOO_LONG
    elif not re.search(r"[a-z0-9]", name.lower()):
        errors[NAME] = NAME_NEEDS_CHARACTERS
    elif name.casefold() in taken:
        errors[NAME] = NAME_TAKEN
    if len(unit) > MAX_CUSTOM_UNIT:
        errors[UNIT] = UNIT_TOO_LONG
    if direction not in (LOWER, HIGHER):
        errors[DIRECTION] = DIRECTION_REQUIRED
    if len(question) > MAX_CUSTOM_TEXT:
        errors[QUESTION] = TEXT_TOO_LONG
    if len(action) > MAX_CUSTOM_TEXT:
        errors[ACTION] = TEXT_TOO_LONG

    start = _number(form.get(START))
    full = _number(form.get(FULL))
    weight = _number(form.get(WEIGHT))
    days = _number(form.get(STALE_DAYS))
    if start is None:
        errors[START] = NOT_A_NUMBER
    if full is None:
        errors[FULL] = NOT_A_NUMBER
    if weight is None:
        errors[WEIGHT] = NOT_A_NUMBER
    elif not 0 <= weight <= 100:
        errors[WEIGHT] = RANGE_0_TO_100
    if days is None or not days.is_integer():
        errors[STALE_DAYS] = WHOLE_DAYS
    elif not 1 <= days <= MAX_STALE_DAYS:
        errors[STALE_DAYS] = DAYS_RANGE
    # The direction is checked against the two numbers; it never swaps them.
    if start is not None and full is not None and FULL not in errors:
        if direction == LOWER and not full < start:
            errors[FULL] = LOWER_NEEDS_MAX_BELOW_START
        elif direction == HIGHER and not full > start:
            errors[FULL] = HIGHER_NEEDS_MAX_ABOVE_START

    if (
        errors
        or start is None
        or full is None
        or weight is None
        or days is None
        or direction not in (LOWER, HIGHER)
    ):
        return None, errors
    new_id = current.id if current is not None else signal_id(name, [o.id for o in others])
    try:
        signal = CustomSignal(
            id=new_id,
            name=name,
            unit=unit,
            rule=SignalRule(weight=weight, start=start, full=full),
            question=question,
            action=action,
            stale_days=int(days),
        )
    except ValidationError as problem:  # the checks above match the model's; this is a backstop
        message = str(problem.errors()[0]["msg"]).removeprefix("Value error, ")
        return None, {FORM_ERROR: message}
    return signal, {}


def listing(custom: Sequence[CustomSignal]) -> list[SignalLine]:
    """The list on the Scoring page, in the order the signals were added."""
    return [
        SignalLine(
            id=signal.id,
            name=signal.name,
            rule=rule_sentence(signal),
            meta=meta_sentence(signal),
        )
        for signal in custom
    ]


def rule_sentence(signal: CustomSignal) -> str:
    """What the range does, in the CSM's own units: "Risk starts at 500 calls, maxed at 50 calls,
    up to 30 points."."""
    rule = signal.rule
    points = format_number(rule.weight) + (" point" if rule.weight == 1 else " points")
    return (
        f"Risk starts at {_amount(rule.start, signal.unit)}, "
        f"maxed at {_amount(rule.full, signal.unit)}, up to {points}."
    )


def meta_sentence(signal: CustomSignal) -> str:
    """Which way is worse, and how long a reading counts as current."""
    worse = "Lower" if signal.rule.full < signal.rule.start else "Higher"
    days = signal.stale_days
    unit = "day" if days == 1 else "days"
    return f"{worse} numbers are worse. A reading is out of date after {days} {unit}."


def _amount(value: float, unit: str) -> str:
    """A number with its unit: "500 calls", or "12%" (a symbol sticks to its number, as in
    scoring)."""
    number = format_number(value)
    if not unit:
        return number
    return number + unit if not unit[0].isalnum() else f"{number} {unit}"


def _line(raw: str) -> str:
    """Text as one line with single spaces, the way CustomSignal stores it."""
    return " ".join(raw.split())


def _number(raw: Any) -> float | None:
    """A finite number from the posted text, or None when it is missing, blank or not a number."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _text(value: float) -> str:
    """'25' for a whole number, otherwise the shortest text that reads back as the same float."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)
