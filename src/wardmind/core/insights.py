"""Insights: four small, explainable read-outs built on top of the score.

- What would it take (`path_down`): the smallest change that moves an account down one band.
- How sure is this score (`score_confidence`): how many signals the score rests on, and how old.
- Go-live forecast (`golive_forecast`): when onboarding finishes at the pace so far.
- Shelfware (`shelfware`): the share of the contract paying for seats no one uses.

Pure and deterministic: no I/O and no clock reads (callers pass `as_of`). Every number comes
from the account, its latest readings and the scoring config, never from a model.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before
changing them.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from datetime import date, timedelta
from enum import StrEnum
from functools import partial

from pydantic import BaseModel

from wardmind.core import signal_info
from wardmind.core.config import MotionWindows, ScoringConfig, SignalRule
from wardmind.core.models import (
    Account,
    Health,
    Motion,
    ScoredAccount,
    Signal,
    SignalKey,
    SignalKind,
)
from wardmind.core.scoring import _spaced, _whole, describe, format_number, score_account

# --- What would it take ----------------------------------------------------------------------


class Fix(BaseModel):
    """One change to one signal's reading."""

    kind: SignalKey
    label: str  # signal_info.label(kind, config)
    now_text: str  # scoring.describe(kind, current value)
    # What to do: sentence case, no full stop, e.g. "Close 1 of the 2 severity-1 tickets".
    goal_text: str
    target_value: float  # the reading that gets there
    points_off: float  # risk now minus the re-scored risk, 1 decimal


class PathDown(BaseModel):
    current: Health  # AT_RISK or WATCH
    target: Health  # one band down: AT_RISK -> WATCH, WATCH -> HEALTHY
    risk: float  # the account's risk now
    threshold: float  # risk must end up strictly below this (thresholds.at_risk or .watch)
    points_needed: float  # round(risk - threshold, 1) plus 0.1: the smallest drop that is enough
    single: list[Fix]  # every one-signal change that is enough alone, smallest change first
    together: list[Fix]  # only when `single` is empty: the fewest signals cleared together
    not_enough: list[str]  # labels of contributing signals that can't do it alone (see below)
    reachable: bool  # False only if clearing every contributing signal still isn't enough
    summary: str  # one sentence, see path_down


def path_down(
    scored: ScoredAccount,
    latest: dict[SignalKey, Signal],
    config: ScoringConfig,
    as_of: date,
) -> PathDown | None:
    """The smallest change that moves the account down one health band. None when Healthy.

    `latest` is the account's latest reading per kind (scoring.latest_signals(...)[account_id]).
    Only kinds in `scored.reasons` (contribution > 0) are candidates.

    Every check re-scores with scoring.score_account on a copy of `latest` with one or more
    readings replaced, so rounding and the 100 cap behave exactly as in the real score. "Enough"
    means the re-scored health is `target` or better.

    single: for each candidate kind, step its reading one whole unit at a time from the current
    value towards rule.start (the first step is the nearest whole number strictly closer to
    rule.start; the last step is rule.start itself), and keep the first step that is enough. That
    step is `target_value`. Order: the share of the rule's range moved,
    abs(target - current) / abs(rule.full - rule.start), ascending; then the kind's book order.

    together: only when no single change is enough. The smallest set of candidate kinds whose
    readings, all set to rule.start, are enough together. Ties: smallest total share moved, then
    the set whose kinds come first in book order. Each Fix has target_value = rule.start.
    Listed in book order (signal_info.order).

    not_enough: labels of candidate kinds that are not in `single`, in `scored.reasons` order.
    When `single` is empty this is every candidate (together still lists what works).

    goal_text, by kind, where n is the current reading and t the target as whole numbers
    (scoring._whole rounding):
      USAGE_TREND_30D: t >= 0 -> "Get active users back to flat or growing";
                       else -> f"Get the 30-day usage drop down to {-t}% or less"
      DAYS_SINCE_LAST_LOGIN: "Get someone to log in"   (any login resets the count)
      OPEN_TICKETS_SEV1: t == 0 and n == 1 -> "Close the severity-1 ticket";
                         t == 0 and n == 2 -> "Close both severity-1 tickets";
                         t == 0 -> f"Close all {n} severity-1 tickets";
                         else -> f"Close {n - t} of the {n} severity-1 tickets"
      NPS_LATEST: f"Get an NPS response of {t} or more"
      CHAMPION_LEFT: "Find and confirm a new champion"
      SEAT_UTILISATION: f"Get seat use up to {t}%"
      BILLING_OVERDUE_DAYS: "Get the overdue invoice paid"
      ONBOARDING_OVERDUE_MILESTONES: same pattern as tickets, with "overdue milestone(s)":
                         "Complete the overdue milestone" / "Complete both overdue milestones"
                         / f"Complete all {n} overdue milestones"
                         / f"Complete {n - t} of the {n} overdue milestones"
      a custom signal (the book's own): f"Get {name} to {t} or more"; f"Get {name} down to {t}
                         or less" when its start is below its full (_custom_goal_text), with the
                         unit after the number

    summary:
      single -> f"{single[0].goal_text} and this drops to {target.label}."
      together -> f"No single fix is enough. {A}, {b} and {c} together would drop it to
                  {target.label}." (goal texts joined with ", " and a final " and ", only the
                  first letter of the whole sentence capitalised)
      not reachable -> f"Clearing every signal still wouldn't drop it to {target.label}."
    """
    if scored.health is Health.HEALTHY:
        return None
    current = scored.health
    target = Health.WATCH if current is Health.AT_RISK else Health.HEALTHY
    threshold = config.thresholds.at_risk if current is Health.AT_RISK else config.thresholds.watch
    risk = scored.risk
    points_needed = round(round(risk - threshold, 1) + 0.1, 1)
    # Each contributing kind with its rule: the built in and the book's own. A kind the book no
    # longer has has no rule and can't be changed, so it is not a candidate.
    rules: dict[SignalKey, SignalRule] = {}
    for reason in scored.reasons:
        rule = config.rule_for(reason.kind)
        if rule is not None:
            rules[reason.kind] = rule
    candidates = list(rules)
    order = {kind: signal_info.order(kind, config) for kind in candidates}

    def rescore(changes: dict[SignalKey, float]) -> ScoredAccount:
        readings = dict(latest)
        for kind, value in changes.items():
            readings[kind] = latest[kind].model_copy(update={"value": value})
        return score_account(scored.account, readings, config, as_of)

    def clears(kind: SignalKey, reading: float) -> bool:
        """True when this one reading alone brings the account to `target` or better."""
        return _enough(rescore({kind: reading}).health, target)

    found: list[tuple[float, int, Fix]] = []
    for kind in candidates:
        rule = rules[kind]
        value = latest[kind].value
        step = _first_enough(value, rule.start, partial(clears, kind))
        if step is not None:
            after = rescore({kind: step})
            fix = _fix(kind, value, step, round(risk - after.risk, 1), config)
            found.append((_share(rule, value, step), order[kind], fix))
    found.sort(key=lambda item: (item[0], item[1]))
    single = [fix for _, _, fix in found]

    together: list[Fix] = []
    if not single:
        ordered = sorted(candidates, key=lambda kind: order[kind])
        for size in range(1, len(ordered) + 1):
            options: list[tuple[float, list[int], tuple[SignalKey, ...], ScoredAccount]] = []
            for combo in itertools.combinations(ordered, size):
                after = rescore({kind: rules[kind].start for kind in combo})
                if _enough(after.health, target):
                    moved = sum(
                        _share(rules[kind], latest[kind].value, rules[kind].start) for kind in combo
                    )
                    options.append((round(moved, 9), [order[kind] for kind in combo], combo, after))
            if options:
                _, _, combo, after = min(options, key=lambda option: (option[0], option[1]))
                together = [
                    _fix(
                        kind,
                        latest[kind].value,
                        rules[kind].start,
                        round(risk - after.risk, 1),
                        config,
                    )
                    for kind in combo
                ]
                break

    single_kinds = {fix.kind for fix in single}
    not_enough = [
        signal_info.label(kind, config) for kind in candidates if kind not in single_kinds
    ]
    if single:
        summary = f"{single[0].goal_text} and this drops to {target.label}."
    elif together:
        goals = [fix.goal_text for fix in together]
        joined = _join([goals[0], *(_lower_first(goal) for goal in goals[1:])])
        summary = f"No single fix is enough. {joined} together would drop it to {target.label}."
    else:
        summary = f"Clearing every signal still wouldn't drop it to {target.label}."
    return PathDown(
        current=current,
        target=target,
        risk=risk,
        threshold=threshold,
        points_needed=points_needed,
        single=single,
        together=together,
        not_enough=not_enough,
        reachable=bool(single or together),
        summary=summary,
    )


# --- How sure is this score ------------------------------------------------------------------

# A reading older than this many days (as_of minus observed_at's date) is out of date. NPS
# surveys and champion checks are naturally less frequent than product data. An alias of
# signal_info.BUILTIN_STALE_DAYS; a custom signal sets its own (signal_info.stale_days).
STALE_AFTER_DAYS: dict[SignalKind, int] = signal_info.BUILTIN_STALE_DAYS


class Confidence(StrEnum):
    FULL = "full"  # every relevant signal has an up-to-date reading
    PARTIAL = "partial"  # more than half do
    THIN = "thin"  # half or fewer do

    @property
    def label(self) -> str:
        return {"full": "Full data", "partial": "Partial data", "thin": "Thin data"}[self.value]


class ReadingAge(BaseModel):
    kind: SignalKey
    label: str  # signal_info.label(kind, config)
    age_days: int | None  # max(0, (as_of - observed_at.date()).days); None when no reading
    stale: bool  # age_days > signal_info.stale_days(kind, config); False when there is no reading


class ScoreConfidence(BaseModel):
    level: Confidence
    relevant: int  # kinds that count: all in onboarding, else all but onboarding milestones
    fresh: int  # relevant kinds with a reading that isn't stale
    missing: list[str]  # labels of relevant kinds with no reading, in the book's kind order
    stale: list[str]  # labels of relevant kinds whose latest reading is stale, same order
    oldest_age_days: int | None  # oldest relevant reading present; None when none are present
    ages: list[ReadingAge]  # every kind the book scores, in that order (relevant or not)
    blind_spot: bool  # health is HEALTHY and level is THIN: it may only look healthy
    sentence: str  # see score_confidence


def score_confidence(
    scored: ScoredAccount,
    latest: dict[SignalKey, Signal],
    as_of: date,
    config: ScoringConfig | None = None,
) -> ScoreConfidence:
    """How much data the score rests on.

    The kinds counted are config.kinds(): the built in and the book's own, each relevant like a
    built-in. Without `config`, the built-in kinds and any custom reading in `latest` count (a
    custom reading then reads by its id, as one the book has removed).

    Level: FULL when fresh == relevant; THIN when fresh * 2 <= relevant; else PARTIAL.

    sentence:
      FULL, oldest 0 -> f"Based on all {relevant} signals, all read today."
      FULL -> f"Based on all {relevant} signals. The oldest reading is {d} day(s) old."
      otherwise -> f"Based on {fresh} of {relevant} signals." then, when any are missing,
        f" No reading for {list}." then, when any are stale, f" Out of date: {list}."
        Lists join labels with ", " and a final " and "; labels keep their own case.
    """
    onboarding = scored.motion is Motion.ONBOARDING
    scoring = config if config is not None else ScoringConfig()
    kinds = scoring.kinds() if config is not None else _kinds_read(latest)
    ages: list[ReadingAge] = []
    for kind in kinds:
        signal = latest.get(kind)
        age_days = None if signal is None else max(0, (as_of - signal.observed_at.date()).days)
        ages.append(
            ReadingAge(
                kind=kind,
                label=signal_info.label(kind, scoring),
                age_days=age_days,
                stale=age_days is not None and age_days > signal_info.stale_days(kind, scoring),
            )
        )
    relevant_ages = [
        age
        for age in ages
        if onboarding or age.kind is not SignalKind.ONBOARDING_OVERDUE_MILESTONES
    ]
    relevant = len(relevant_ages)
    fresh = sum(1 for age in relevant_ages if age.age_days is not None and not age.stale)
    missing = [age.label for age in relevant_ages if age.age_days is None]
    stale_labels = [age.label for age in relevant_ages if age.stale]
    present = [age.age_days for age in relevant_ages if age.age_days is not None]
    oldest = max(present, default=None)

    if fresh == relevant:
        level = Confidence.FULL
    elif fresh * 2 <= relevant:
        level = Confidence.THIN
    else:
        level = Confidence.PARTIAL

    if level is Confidence.FULL and oldest is not None:
        if oldest == 0:
            sentence = f"Based on all {relevant} signals, all read today."
        else:
            sentence = (
                f"Based on all {relevant} signals. "
                f"The oldest reading is {oldest} {_days(oldest)} old."
            )
    else:
        sentence = f"Based on {fresh} of {relevant} signals."
        if missing:
            sentence += f" No reading for {_join(missing)}."
        if stale_labels:
            sentence += f" Out of date: {_join(stale_labels)}."

    return ScoreConfidence(
        level=level,
        relevant=relevant,
        fresh=fresh,
        missing=missing,
        stale=stale_labels,
        oldest_age_days=oldest,
        ages=ages,
        blind_spot=scored.health is Health.HEALTHY and level is Confidence.THIN,
        sentence=sentence,
    )


def book_confidence(
    book: list[ScoredAccount],
    latest_by_account: dict[str, dict[SignalKey, Signal]],
    as_of: date,
    config: ScoringConfig | None = None,
) -> dict[str, ScoreConfidence]:
    """score_confidence for every account in the book, keyed by account id. An account missing
    from `latest_by_account` has no readings. `config` is as for score_confidence."""
    return {
        item.account.id: score_confidence(
            item, latest_by_account.get(item.account.id, {}), as_of, config
        )
        for item in book
    }


# --- Go-live forecast ------------------------------------------------------------------------

MIN_FORECAST_DAYS = 14  # no forecast until onboarding has run this many days


class GoLiveStatus(StrEnum):
    ON_TRACK = "on_track"  # projected to finish on or before the last day of the window
    LATE = "late"  # projected to finish after it
    NO_PACE = "no_pace"  # MIN_FORECAST_DAYS or more in, and no milestone done yet
    TOO_EARLY = "too_early"  # fewer than MIN_FORECAST_DAYS days in

    @property
    def label(self) -> str:
        return {
            "on_track": "On track",
            "late": "Late",
            "no_pace": "No pace yet",
            "too_early": "Too early",
        }[self.value]


class GoLiveForecast(BaseModel):
    status: GoLiveStatus
    day: int  # (as_of - start_date).days + 1, so the start date is day 1
    window_days: int  # MotionWindows.onboarding_days
    done: int
    total: int
    projected_day: int | None  # ON_TRACK / LATE: ceil(day * total / done); else None
    projected_date: date | None  # start_date + (projected_day - 1) days; else None
    overrun_days: int | None  # projected_day - window_days (negative is early); else None
    sentence: str  # see golive_forecast


def golive_forecast(account: Account, as_of: date, windows: MotionWindows) -> GoLiveForecast | None:
    """When onboarding finishes if the remaining milestones go at the average pace so far.

    None when: start_date is None, start_date is after as_of, milestones.total == 0, or
    milestones are complete. Accounts past the window still get a forecast (they are LATE).
    Integer maths only for projected_day: (day * total + done - 1) // done.

    sentence (d = "day" or "days" to match the number):
      LATE -> f"At this pace onboarding ends on day {p} of {w}, {o} {d} late."
      ON_TRACK, overrun 0 -> f"At this pace onboarding ends on day {p} of {w}, right on time."
      ON_TRACK -> f"At this pace onboarding ends on day {p} of {w}, with {-o} {d} to spare."
      NO_PACE -> f"No milestones done after {day} days, so there's no pace to forecast from."
      TOO_EARLY -> f"Day {day} of onboarding. A forecast appears after {MIN_FORECAST_DAYS} days."
    """
    start = account.start_date
    milestones = account.milestones
    if start is None or start > as_of or milestones.total == 0 or milestones.complete:
        return None
    day = (as_of - start).days + 1
    window = windows.onboarding_days
    done, total = milestones.done, milestones.total

    def forecast(
        status: GoLiveStatus,
        sentence: str,
        projected: int | None = None,
        overrun: int | None = None,
    ) -> GoLiveForecast:
        return GoLiveForecast(
            status=status,
            day=day,
            window_days=window,
            done=done,
            total=total,
            projected_day=projected,
            projected_date=None if projected is None else start + timedelta(days=projected - 1),
            overrun_days=overrun,
            sentence=sentence,
        )

    if day < MIN_FORECAST_DAYS:
        return forecast(
            GoLiveStatus.TOO_EARLY,
            f"Day {day} of onboarding. A forecast appears after {MIN_FORECAST_DAYS} days.",
        )
    if done == 0:
        return forecast(
            GoLiveStatus.NO_PACE,
            f"No milestones done after {day} days, so there's no pace to forecast from.",
        )
    projected = (day * total + done - 1) // done
    overrun = projected - window
    if overrun > 0:
        return forecast(
            GoLiveStatus.LATE,
            f"At this pace onboarding ends on day {projected} of {window}, "
            f"{overrun} {_days(overrun)} late.",
            projected,
            overrun,
        )
    if overrun == 0:
        return forecast(
            GoLiveStatus.ON_TRACK,
            f"At this pace onboarding ends on day {projected} of {window}, right on time.",
            projected,
            overrun,
        )
    spare = -overrun
    return forecast(
        GoLiveStatus.ON_TRACK,
        f"At this pace onboarding ends on day {projected} of {window}, "
        f"with {spare} {_days(spare)} to spare.",
        projected,
        overrun,
    )


# --- Shelfware -------------------------------------------------------------------------------

SHELFWARE_MIN_UNUSED = 20  # whole percent of seats unused before it is worth flagging


class Shelfware(BaseModel):
    utilisation: int  # whole % of seats in use (scoring._whole of the latest reading)
    unused_percent: int  # 100 - utilisation
    unused_arr: float  # arr * unused_percent / 100, rounded to the nearest 100
    days_to_renewal: int  # (renewal_date - as_of).days, negative once passed
    renewing_soon: bool  # 0 <= days_to_renewal <= windows.renewal_window_days
    advice: str  # see shelfware


def shelfware(
    account: Account,
    latest: dict[SignalKey, Signal],
    as_of: date,
    windows: MotionWindows,
) -> Shelfware | None:
    """The part of the contract paying for unused seats, assuming a flat price per seat.

    None when: there is no SEAT_UTILISATION reading, account.arr is 0, or unused_percent is
    below SHELFWARE_MIN_UNUSED (so 100% or more in use is always None).

    advice:
      renewing_soon -> f"Renewal is in {n} {day/days}. Expect a downsell ask; offering to
                       right-size first keeps the conversation yours."  (n == 0 -> "Renewal is
                       today. ...")
      otherwise -> "Plan a seat review before the renewal, so a downsell isn't a surprise."
    """
    reading = latest.get(SignalKind.SEAT_UTILISATION)
    if reading is None or account.arr == 0:
        return None
    utilisation = _whole(reading.value)
    unused_percent = 100 - utilisation
    if unused_percent < SHELFWARE_MIN_UNUSED:
        return None
    raw_unused = account.arr * unused_percent / 100
    unused_arr = float(_whole(raw_unused / 100) * 100)
    days_to_renewal = (account.renewal_date - as_of).days
    renewing_soon = 0 <= days_to_renewal <= windows.renewal_window_days
    if renewing_soon:
        lead = (
            "Renewal is today."
            if days_to_renewal == 0
            else f"Renewal is in {days_to_renewal} {_days(days_to_renewal)}."
        )
        advice = (
            f"{lead} Expect a downsell ask; "
            "offering to right-size first keeps the conversation yours."
        )
    else:
        advice = "Plan a seat review before the renewal, so a downsell isn't a surprise."
    return Shelfware(
        utilisation=utilisation,
        unused_percent=unused_percent,
        unused_arr=unused_arr,
        days_to_renewal=days_to_renewal,
        renewing_soon=renewing_soon,
        advice=advice,
    )


class ShelfwareRow(BaseModel):
    account_id: str
    name: str
    health: Health
    motion: Motion
    shelfware: Shelfware


class ShelfwareSummary(BaseModel):
    rows: list[ShelfwareRow]  # at most `limit`, unused_arr desc, then name asc, then id asc
    count: int  # every renewing-soon account with shelfware, before the limit
    total_unused_arr: float  # summed over all `count` accounts, not just the rows shown


def renewal_shelfware(
    book: list[ScoredAccount],
    latest_by_account: dict[str, dict[SignalKey, Signal]],
    as_of: date,
    windows: MotionWindows,
    limit: int = 5,
) -> ShelfwareSummary:
    """Shelfware across the accounts whose renewal is coming up (Shelfware.renewing_soon), for
    the weekly brief. Accounts not renewing soon are left out."""
    rows: list[ShelfwareRow] = []
    for item in book:
        found = shelfware(item.account, latest_by_account.get(item.account.id, {}), as_of, windows)
        if found is not None and found.renewing_soon:
            rows.append(
                ShelfwareRow(
                    account_id=item.account.id,
                    name=item.account.name,
                    health=item.health,
                    motion=item.motion,
                    shelfware=found,
                )
            )
    rows.sort(key=lambda row: (-row.shelfware.unused_arr, row.name, row.account_id))
    return ShelfwareSummary(
        rows=rows[: max(limit, 0)],
        count=len(rows),
        total_unused_arr=sum((row.shelfware.unused_arr for row in rows), 0.0),
    )


# --- Private helpers -------------------------------------------------------------------------

_RANK = {Health.HEALTHY: 0, Health.WATCH: 1, Health.AT_RISK: 2}


def _enough(health: Health, target: Health) -> bool:
    """True when `health` is `target` or better (Healthy beats Watch beats At risk)."""
    return _RANK[health] <= _RANK[target]


def _first_enough(value: float, start: float, clears: Callable[[float], bool]) -> float | None:
    """The first reading on the way from `value` to `start` that `clears` accepts, or None.

    The way is the whole numbers strictly between them, one unit at a time (the first is the
    nearest whole number strictly closer to start), then `start` itself. Moving toward start only
    lowers the risk, so `clears` turns from False to True once along the way: the first True is
    found by bisection. A range of 1,000,000 units then takes about 20 checks, not a million."""
    rising = start > value
    edge = math.floor(value) + 1 if rising else math.ceil(value) - 1  # the first whole step
    # The whole steps run from edge toward start, stopping short of it.
    count = max(0, (math.ceil(start) - edge) if rising else (edge - math.floor(start)))

    def step(index: int) -> float:
        if index >= count:
            return start
        return float(edge + index if rising else edge - index)

    low, high = 0, count
    if not clears(step(high)):
        return None
    while low < high:
        middle = (low + high) // 2
        if clears(step(middle)):
            high = middle
        else:
            low = middle + 1
    return step(low)


def _share(rule: SignalRule, value: float, target: float) -> float:
    """The share of the rule's range that a move from `value` to `target` covers."""
    return round(abs(target - value) / abs(rule.full - rule.start), 9)


def _kinds_read(latest: dict[SignalKey, Signal]) -> list[SignalKey]:
    """Without a config: the built-in kinds, then each custom kind with a reading, by id."""
    custom = sorted((kind for kind in latest if not isinstance(kind, SignalKind)), key=str)
    return [*SignalKind, *custom]


def _fix(
    kind: SignalKey,
    value: float,
    target_value: float,
    points_off: float,
    config: ScoringConfig,
) -> Fix:
    if isinstance(kind, SignalKind):
        goal = _goal_text(kind, _whole(value), _whole(target_value))
    else:
        goal = _custom_goal_text(kind, target_value, config)
    return Fix(
        kind=kind,
        label=signal_info.label(kind, config),
        now_text=describe(kind, value, config.custom_signal(kind)),
        goal_text=goal,
        target_value=target_value,
        points_off=points_off,
    )


def _custom_goal_text(kind: SignalKey, target: float, config: ScoringConfig) -> str:
    """What to do for one of the book's own signals: reach the target reading. A higher reading
    is the safer one when the rule's start is above its full (so "or more"), else a lower one
    ("or less"). The name is the CSM's own, so it is kept as written."""
    signal = config.custom_signal(kind)
    rule = config.rule_for(kind)
    name = signal.name if signal is not None else signal_info.label(kind, config)
    unit = signal.unit if signal is not None else ""
    number = f"{format_number(target)}{_spaced(unit)}"
    if rule is not None and rule.start < rule.full:
        return f"Get {name} down to {number} or less"
    return f"Get {name} to {number} or more"


def _goal_text(kind: SignalKind, n: int, t: int) -> str:
    """What to do, given the current whole reading `n` and the target whole reading `t`."""
    match kind:
        case SignalKind.USAGE_TREND_30D:
            if t >= 0:
                return "Get active users back to flat or growing"
            return f"Get the 30-day usage drop down to {-t}% or less"
        case SignalKind.DAYS_SINCE_LAST_LOGIN:
            return "Get someone to log in"
        case SignalKind.OPEN_TICKETS_SEV1:
            if t == 0 and n == 1:
                return "Close the severity-1 ticket"
            if t == 0 and n == 2:
                return "Close both severity-1 tickets"
            if t == 0:
                return f"Close all {n} severity-1 tickets"
            return f"Close {n - t} of the {n} severity-1 tickets"
        case SignalKind.NPS_LATEST:
            return f"Get an NPS response of {t} or more"
        case SignalKind.CHAMPION_LEFT:
            return "Find and confirm a new champion"
        case SignalKind.SEAT_UTILISATION:
            return f"Get seat use up to {t}%"
        case SignalKind.BILLING_OVERDUE_DAYS:
            return "Get the overdue invoice paid"
        case SignalKind.ONBOARDING_OVERDUE_MILESTONES:
            if t == 0 and n == 1:
                return "Complete the overdue milestone"
            if t == 0 and n == 2:
                return "Complete both overdue milestones"
            if t == 0:
                return f"Complete all {n} overdue milestones"
            return f"Complete {n - t} of the {n} overdue milestones"
    raise ValueError(f"no goal wording for signal kind {kind!r}")


def _join(items: list[str]) -> str:
    """A list as prose: "A", "A and B", "A, B and C"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _days(count: int) -> str:
    return "day" if count == 1 else "days"
