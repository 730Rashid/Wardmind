"""Suggestions that learn your taste: what the CSM accepts and dismisses, per detector.

CONTRACT FILE. The constants, enums, models and signatures below are the contract; the lead wrote
them. The function bodies marked "Built against the contract" are workstream A's.

Each of the five detectors (core/proposals.py) keeps its own record: the suggestions it made that
the CSM accepted, and the ones they dismissed as Not useful, Wrong or with no reason. Dismissing
as Not now or Already on it is about timing, so it doesn't count against the detector (it is
counted as set aside). Expired and open suggestions, and Claude's (source MCP), aren't counted.

Once a detector has MIN_DECISIONS counted decisions in the last WINDOW_DAYS (after its reset),
an accept rate under LOW_RATE puts it in FEWER: plan() then makes at most FEWER_PER_RUN of its
suggestions per run, after all the others (proposals.plan's `fewer`). A detector that suggests
something urgent (PROTECTED: health dropped, renewal with no call) is never held back.

Guardrails: nothing is ever silenced (FEWER still suggests), every state is shown with its
numbers in Settings > Assistant, Reset (Proposals.reset_taste) forgets a detector's record, and
Settings' suggestions_learn turns the learning off (every detector shows as usual). Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from wardmind.core.proposals import (
    Detector,
    Proposal,
    ProposalSource,
    ProposalStatus,
    naive_utc,
)

MIN_DECISIONS = 10  # counted decisions (accepted + dismissed against) before it learns anything
WINDOW_DAYS = 180  # decisions older than this aren't counted
LOW_RATE = 0.25  # an accept rate under this (with MIN_DECISIONS) shows the detector less
FEWER_PER_RUN = 1  # suggestions a FEWER detector makes in one run of plan()
PROTECTED: frozenset[Detector] = frozenset({Detector.HEALTH_DROPPED, Detector.RENEWAL_NO_CALL})

LABELS: dict[Detector, str] = {
    Detector.HEALTH_DROPPED: "Health dropped",
    Detector.RENEWAL_NO_CALL: "Renewal with no call",
    Detector.REPLY_OVERDUE: "Reply overdue",
    Detector.GONE_QUIET: "Gone quiet",
    Detector.BLIND_SPOT: "Thin data",
}


class TasteState(StrEnum):
    OFF = "off"  # Settings' suggestions_learn is off: shown as usual, nothing learnt
    PROTECTED = "protected"  # in PROTECTED: always shown as usual
    LEARNING = "learning"  # fewer than MIN_DECISIONS counted decisions
    USUAL = "usual"  # enough decisions, accept rate LOW_RATE or more
    FEWER = "fewer"  # enough decisions, accept rate under LOW_RATE

    @property
    def label(self) -> str:
        return {
            "off": "Not learning",
            "protected": "Always shown",
            "learning": "Learning",
            "usual": "As usual",
            "fewer": "Showing fewer",
        }[self.value]


class DetectorTaste(BaseModel):
    detector: Detector
    label: str  # LABELS[detector]
    accepted: int  # counted: in the window, after the reset
    dismissed: int  # counted against: NOT_USEFUL, WRONG or no reason
    set_aside: int  # dismissed NOT_NOW or ALREADY_ON_IT (not counted against)
    rate: float | None  # accepted / (accepted + dismissed); None when both are 0
    state: TasteState
    reset_at: datetime | None = None  # naive UTC, from ProposalsData.taste_resets
    # One plain sentence, e.g.
    #   "You accepted 2 of 14. Showing fewer: at most 1 a day."
    #   "You accepted 6 of 9. Learning: 1 more decision before it adapts."
    #   "You accepted 7 of 12. Shown as usual."
    #   "Always shown: Wardmind never holds these back."   (PROTECTED, with any counts before it:
    #   "You accepted 3 of 4. Always shown: Wardmind never holds these back.")
    #   "No decisions yet."                                 (LEARNING with 0 counted)
    #   "Learning is off."                                  (OFF)
    sentence: str


# --- Written by the lead with the contract -------------------------------------------------


def tastes(
    items: Sequence[Proposal],
    resets: Mapping[Detector, datetime],
    now: datetime,
    *,
    learn: bool = True,
) -> list[DetectorTaste]:
    """One DetectorTaste per Detector, in Detector order. A decision counts when the suggestion
    came from that detector (source DETECTOR), is ACCEPTED or DISMISSED, and its decided_at is
    within WINDOW_DAYS before `now` and not before the detector's reset. `now` may be aware or
    naive UTC (compare as naive UTC). learn=False: every state is OFF, counts still filled."""
    moment = naive_utc(now)
    oldest = moment - timedelta(days=WINDOW_DAYS)
    found: list[DetectorTaste] = []
    for detector in Detector:
        reset_at = resets.get(detector)
        since = oldest if reset_at is None else max(oldest, naive_utc(reset_at))
        accepted = dismissed = set_aside = 0
        for item in items:
            if item.source is not ProposalSource.DETECTOR or item.detector is not detector:
                continue
            if item.decided_at is None or not since <= naive_utc(item.decided_at) <= moment:
                continue
            if item.status is ProposalStatus.ACCEPTED:
                accepted += 1
            elif item.status is ProposalStatus.DISMISSED:
                reason = item.dismiss_reason
                if reason is None or reason.counts_against:
                    dismissed += 1
                else:
                    set_aside += 1
        counted = accepted + dismissed
        rate = accepted / counted if counted else None
        state = _state(detector, counted, rate, learn)
        found.append(
            DetectorTaste(
                detector=detector,
                label=LABELS[detector],
                accepted=accepted,
                dismissed=dismissed,
                set_aside=set_aside,
                rate=rate,
                state=state,
                reset_at=None if reset_at is None else naive_utc(reset_at),
                sentence=_sentence(state, accepted, counted),
            )
        )
    return found


def fewer(found: Sequence[DetectorTaste]) -> frozenset[Detector]:
    """The detectors in state FEWER (what proposals.plan's `fewer` takes)."""
    return frozenset(item.detector for item in found if item.state is TasteState.FEWER)


def _state(detector: Detector, counted: int, rate: float | None, learn: bool) -> TasteState:
    if not learn:
        return TasteState.OFF
    if detector in PROTECTED:
        return TasteState.PROTECTED
    if counted < MIN_DECISIONS or rate is None:
        return TasteState.LEARNING
    return TasteState.FEWER if rate < LOW_RATE else TasteState.USUAL


def _sentence(state: TasteState, accepted: int, counted: int) -> str:
    if state is TasteState.OFF:
        return "Learning is off."
    tally = f"You accepted {accepted} of {counted}. " if counted else ""
    if state is TasteState.PROTECTED:
        return f"{tally}Always shown: Wardmind never holds these back."
    if state is TasteState.LEARNING:
        if not counted:
            return "No decisions yet."
        left = MIN_DECISIONS - counted
        more = "1 more decision" if left == 1 else f"{left} more decisions"
        return f"{tally}Learning: {more} before it adapts."
    if state is TasteState.FEWER:
        each = "1 a day" if FEWER_PER_RUN == 1 else f"{FEWER_PER_RUN} a day"
        return f"{tally}Showing fewer: at most {each}."
    return f"{tally}Shown as usual."
