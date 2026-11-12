"""What the pages say about any signal: one of the eight built in, or one the book defines.

Every table keyed by SignalKind (labels, questions, talking points, how soon a reading is out of
date) answers for the built-in kinds; a custom kind (models.CustomKind) answers from its
config.CustomSignal. A custom kind the book has since removed still gets a readable label (its
id), so old readings and history never break a page. Pure: no I/O.

CONTRACT FILE: signatures are fixed; implementers escalate before changing them.
"""

from __future__ import annotations

from wardmind.core.config import ScoringConfig
from wardmind.core.models import CUSTOM_PREFIX, SignalKey, SignalKind

# The short name of each built-in signal, as tables and charts show it.
BUILTIN_LABELS: dict[SignalKind, str] = {
    SignalKind.USAGE_TREND_30D: "Usage trend",
    SignalKind.DAYS_SINCE_LAST_LOGIN: "Last login",
    SignalKind.OPEN_TICKETS_SEV1: "Severity-1 tickets",
    SignalKind.NPS_LATEST: "NPS",
    SignalKind.CHAMPION_LEFT: "Champion",
    SignalKind.SEAT_UTILISATION: "Seats in use",
    SignalKind.BILLING_OVERDUE_DAYS: "Billing",
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: "Onboarding milestones",
}

# Days after which a reading is flagged as out of date on the account page.
BUILTIN_STALE_DAYS: dict[SignalKind, int] = {
    SignalKind.USAGE_TREND_30D: 30,
    SignalKind.DAYS_SINCE_LAST_LOGIN: 30,
    SignalKind.OPEN_TICKETS_SEV1: 30,
    SignalKind.NPS_LATEST: 180,
    SignalKind.CHAMPION_LEFT: 90,
    SignalKind.SEAT_UTILISATION: 30,
    SignalKind.BILLING_OVERDUE_DAYS: 30,
    SignalKind.ONBOARDING_OVERDUE_MILESTONES: 30,
}

_BUILTIN_ORDER: dict[SignalKind, int] = {kind: index for index, kind in enumerate(SignalKind)}
_REMOVED_ORDER = 10_000  # a custom kind the book no longer has sorts after everything else


def in_sentence(name: str) -> str:
    """A signal's name as it reads mid-sentence: the first letter lower case ("Reports
    exported" -> "reports exported"), unless the first word is an acronym or a name written
    with a capital inside it ("API calls", "NPS", "iOS installs") or the name is one letter."""
    first = name.split(" ", 1)[0]
    if len(first) < 2 or any(char.isupper() for char in first[1:]):
        return name
    return name[0].lower() + name[1:]


def label(kind: SignalKey, config: ScoringConfig) -> str:
    """The signal's short name: "Usage trend", or the custom signal's own name."""
    if isinstance(kind, SignalKind):
        return BUILTIN_LABELS[kind]
    custom = config.custom_signal(kind)
    if custom is not None:
        return custom.name
    return kind.value.removeprefix(CUSTOM_PREFIX).replace("_", " ").capitalize()


def order(kind: SignalKey, config: ScoringConfig) -> int:
    """A sort position: the built-ins in SignalKind order, then the book's own as added."""
    if isinstance(kind, SignalKind):
        return _BUILTIN_ORDER[kind]
    for index, custom in enumerate(config.custom):
        if custom.kind == kind:
            return len(_BUILTIN_ORDER) + index
    return _REMOVED_ORDER


def stale_days(kind: SignalKey, config: ScoringConfig) -> int:
    """Days after which a reading of this kind is out of date."""
    if isinstance(kind, SignalKind):
        return BUILTIN_STALE_DAYS[kind]
    custom = config.custom_signal(kind)
    return custom.stale_days if custom is not None else BUILTIN_STALE_DAYS[SignalKind.NPS_LATEST]


def custom_question(kind: SignalKey, config: ScoringConfig) -> str | None:
    """What to ask on a call about a custom signal: its own question, else one made from its
    name. None for a built-in kind (its module's own table answers)."""
    if isinstance(kind, SignalKind):
        return None
    custom = config.custom_signal(kind)
    if custom is not None and custom.question:
        return custom.question
    return f"What is behind the change in {in_sentence(label(kind, config))}?"


def custom_action(kind: SignalKey, config: ScoringConfig) -> str | None:
    """The next step when a custom signal is the account's biggest risk: its own action, else
    one made from its name. None for a built-in kind (briefs' action table answers)."""
    if isinstance(kind, SignalKind):
        return None
    custom = config.custom_signal(kind)
    if custom is not None and custom.action:
        return custom.action
    return (
        f"Find out what is behind the {in_sentence(label(kind, config))} reading and agree a fix."
    )
