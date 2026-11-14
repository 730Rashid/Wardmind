"""core/taste.py: what the CSM accepts and dismisses, per detector. The counting rules, the states
and their sentences, fewer(), and Proposals.reset_taste. Everything is built in memory except the
store round trip, which uses tmp_path (conftest keeps the real book and data folders out of it).
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from wardmind.core import proposals, taste
from wardmind.core.proposals import (
    Detector,
    DismissReason,
    Evidence,
    Proposal,
    ProposalKind,
    ProposalSource,
    ProposalStatus,
)
from wardmind.core.store import LocalStore

TODAY = date(2026, 11, 14)
NOW = datetime(2026, 11, 14, 9, 0)  # naive UTC
RECENT = NOW - timedelta(days=1)
GONE_QUIET = Detector.GONE_QUIET
HEALTH = Detector.HEALTH_DROPPED
RENEWAL = Detector.RENEWAL_NO_CALL
REPLY = Detector.REPLY_OVERDUE
THIN = Detector.BLIND_SPOT
STATE = taste.TasteState
_ids = itertools.count(1)


def _decision(
    detector: Detector | None = GONE_QUIET,
    status: ProposalStatus = ProposalStatus.ACCEPTED,
    *,
    decided_at: datetime | None = RECENT,
    reason: DismissReason | None = None,
    source: ProposalSource = ProposalSource.DETECTOR,
) -> Proposal:
    index = next(_ids)
    return Proposal(
        id=f"p-{index:08x}",
        account_id=f"a-{index}",
        kind=ProposalKind.FOLLOW_UP,
        source=source,
        detector=detector,
        key=f"test:{index}",
        title="Get back in touch with Acme",
        why=[Evidence(text="No contact logged yet")],
        text="Get back in touch with Acme",
        due=TODAY,
        status=status,
        created_at=RECENT,
        decided_at=decided_at,
        basis_day=TODAY,
        dismiss_reason=reason,
    )


def _decisions(
    detector: Detector,
    accepted: int = 0,
    dismissed: int = 0,
    reason: DismissReason | None = DismissReason.NOT_USEFUL,
) -> list[Proposal]:
    return [_decision(detector) for _ in range(accepted)] + [
        _decision(detector, ProposalStatus.DISMISSED, reason=reason) for _ in range(dismissed)
    ]


def _row(found: list[taste.DetectorTaste], detector: Detector) -> taste.DetectorTaste:
    return next(item for item in found if item.detector is detector)


def _open_item(index: int, detector: Detector = GONE_QUIET) -> Proposal:
    """An OPEN detector suggestion for its own account, for the store round trips."""
    return Proposal(
        id="",
        account_id=f"a-{index}",
        kind=ProposalKind.FOLLOW_UP,
        source=ProposalSource.DETECTOR,
        detector=detector,
        key=f"{detector.value}:{index}",
        title="Get back in touch with Acme",
        why=[Evidence(text="No contact logged yet")],
        text="Get back in touch with Acme",
        due=TODAY,
        created_at=RECENT,
        basis_day=TODAY,
    )


# --- what counts ---------------------------------------------------------------------------


def test_accepted_suggestions_count_for_their_detector() -> None:
    found = taste.tastes(_decisions(GONE_QUIET, accepted=3), {}, NOW)
    row = _row(found, GONE_QUIET)
    assert (row.accepted, row.dismissed, row.set_aside, row.rate) == (3, 0, 0, 1.0)


@pytest.mark.parametrize(
    ("reason", "against"),
    [
        (DismissReason.NOT_USEFUL, True),
        (DismissReason.WRONG, True),
        (None, True),  # dismissed with no reason counts against the detector
        (DismissReason.NOT_NOW, False),  # about timing: set aside, not counted
        (DismissReason.ALREADY_ON_IT, False),
    ],
)
def test_a_dismissal_counts_against_unless_it_is_about_timing(
    reason: DismissReason | None, against: bool
) -> None:
    items = [_decision(status=ProposalStatus.DISMISSED, reason=reason)]
    row = _row(taste.tastes(items, {}, NOW), GONE_QUIET)
    expected = (0, 1, 0) if against else (0, 0, 1)
    assert (row.accepted, row.dismissed, row.set_aside) == expected
    if not against:
        assert row.rate is None


def test_timing_dismissals_alone_leave_no_decisions_yet() -> None:
    items = [
        _decision(status=ProposalStatus.DISMISSED, reason=DismissReason.NOT_NOW),
        _decision(status=ProposalStatus.DISMISSED, reason=DismissReason.ALREADY_ON_IT),
    ]
    row = _row(taste.tastes(items, {}, NOW), GONE_QUIET)
    assert (row.set_aside, row.rate, row.state) == (2, None, STATE.LEARNING)
    assert row.sentence == "No decisions yet."


def test_only_decided_suggestions_from_a_detector_are_counted() -> None:
    items = [
        _decision(status=ProposalStatus.OPEN, decided_at=None),  # still open
        _decision(status=ProposalStatus.EXPIRED),  # its reason went
        _decision(status=ProposalStatus.ACCEPTED, decided_at=None),  # no decision time
        _decision(None, ProposalStatus.ACCEPTED, source=ProposalSource.MCP),  # Claude's
        _decision(
            None,
            ProposalStatus.DISMISSED,
            reason=DismissReason.NOT_USEFUL,
            source=ProposalSource.MCP,
        ),
        _decision(REPLY, ProposalStatus.ACCEPTED),  # another detector's
        _decision(GONE_QUIET, ProposalStatus.ACCEPTED),  # the one that counts
    ]
    found = taste.tastes(items, {}, NOW)
    gone = _row(found, GONE_QUIET)
    assert (gone.accepted, gone.dismissed, gone.set_aside) == (1, 0, 0)
    assert _row(found, REPLY).accepted == 1


@pytest.mark.parametrize(
    ("ago", "counted"),
    [
        (timedelta(0), True),  # decided just now
        (timedelta(days=taste.WINDOW_DAYS), True),  # exactly the edge of the window
        (timedelta(days=taste.WINDOW_DAYS, seconds=1), False),  # a second past the edge
        (timedelta(minutes=-1), False),  # after `now`, so not yet
    ],
)
def test_the_window_is_inclusive_at_its_edge(ago: timedelta, counted: bool) -> None:
    items = [_decision(status=ProposalStatus.ACCEPTED, decided_at=NOW - ago)]
    row = _row(taste.tastes(items, {}, NOW), GONE_QUIET)
    assert row.accepted == (1 if counted else 0)


def test_decisions_before_a_reset_are_forgotten_and_the_reset_instant_counts() -> None:
    reset = NOW - timedelta(days=2)
    items = [
        _decision(status=ProposalStatus.ACCEPTED, decided_at=reset - timedelta(seconds=1)),
        _decision(
            status=ProposalStatus.DISMISSED,
            reason=DismissReason.NOT_USEFUL,
            decided_at=reset,  # at the instant of the reset: counted
        ),
        _decision(status=ProposalStatus.ACCEPTED, decided_at=RECENT),
    ]
    row = _row(taste.tastes(items, {GONE_QUIET: reset}, NOW), GONE_QUIET)
    assert (row.accepted, row.dismissed) == (1, 1)
    assert row.reset_at == reset


def test_a_reset_belongs_to_its_own_detector() -> None:
    items = _decisions(GONE_QUIET, accepted=2)
    found = taste.tastes(items, {REPLY: NOW}, NOW)
    assert _row(found, GONE_QUIET).accepted == 2
    assert _row(found, GONE_QUIET).reset_at is None
    assert _row(found, REPLY).reset_at == NOW


def test_a_reset_older_than_the_window_leaves_the_window_in_charge() -> None:
    reset = NOW - timedelta(days=taste.WINDOW_DAYS + 100)
    items = [
        _decision(status=ProposalStatus.ACCEPTED, decided_at=NOW - timedelta(days=181)),
        _decision(status=ProposalStatus.ACCEPTED, decided_at=RECENT),
    ]
    row = _row(taste.tastes(items, {GONE_QUIET: reset}, NOW), GONE_QUIET)
    assert row.accepted == 1


def test_an_aware_now_counts_the_same_as_its_naive_utc_time() -> None:
    plus_one = timezone(timedelta(hours=1))
    items = [
        _decision(decided_at=NOW),  # at now: counted
        _decision(decided_at=NOW + timedelta(minutes=30)),  # later than now: not yet
        # 10:00 at +01:00 is 09:00 UTC, which is now: counted
        _decision(decided_at=datetime(2026, 11, 14, 10, 0, tzinfo=plus_one)),
    ]
    naive = taste.tastes(items, {}, NOW)
    as_utc = taste.tastes(items, {}, NOW.replace(tzinfo=UTC))
    as_plus_two = taste.tastes(
        items, {}, NOW.replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=2)))
    )
    assert naive == as_utc == as_plus_two
    assert _row(naive, GONE_QUIET).accepted == 2


def test_an_aware_reset_is_stored_as_naive_utc() -> None:
    reset_aware = datetime(2026, 11, 13, 11, 0, tzinfo=timezone(timedelta(hours=2)))  # 09:00 UTC
    row = _row(taste.tastes([], {GONE_QUIET: reset_aware}, NOW), GONE_QUIET)
    assert row.reset_at == datetime(2026, 11, 13, 9, 0)


def test_one_row_per_detector_in_detector_order_with_its_label() -> None:
    found = taste.tastes([], {}, NOW)
    assert [row.detector for row in found] == list(Detector)
    assert [row.label for row in found] == [taste.LABELS[detector] for detector in Detector]
    for row in found:
        expected = STATE.PROTECTED if row.detector in taste.PROTECTED else STATE.LEARNING
        assert (row.state, row.rate) == (expected, None)


# --- states ----------------------------------------------------------------------------------


def test_learning_off_marks_every_detector_off_and_still_counts() -> None:
    found = taste.tastes(_decisions(GONE_QUIET, accepted=2, dismissed=8), {}, NOW, learn=False)
    assert [row.detector for row in found] == list(Detector)
    assert all(row.state is STATE.OFF for row in found)
    gone = _row(found, GONE_QUIET)
    assert (gone.accepted, gone.dismissed, gone.rate) == (2, 8, 0.2)
    assert gone.sentence == "Learning is off."
    assert taste.fewer(found) == frozenset()


@pytest.mark.parametrize("detector", [HEALTH, RENEWAL])
def test_protected_detectors_are_always_shown_even_at_a_zero_rate(detector: Detector) -> None:
    found = taste.tastes(_decisions(detector, dismissed=10), {}, NOW)
    row = _row(found, detector)
    assert (row.dismissed, row.rate) == (10, 0.0)
    assert row.state is STATE.PROTECTED
    assert detector not in taste.fewer(found)


@pytest.mark.parametrize(
    ("accepted", "dismissed", "state"),
    [
        (0, 0, STATE.LEARNING),  # no decisions yet
        (taste.MIN_DECISIONS - 1, 0, STATE.LEARNING),  # one short, all accepted
        (0, taste.MIN_DECISIONS - 1, STATE.LEARNING),  # one short, all against
        (0, taste.MIN_DECISIONS, STATE.FEWER),  # exactly MIN_DECISIONS, rate 0
        (2, 8, STATE.FEWER),  # exactly MIN_DECISIONS, 2 of 10 is under LOW_RATE
        (3, 9, STATE.USUAL),  # exactly LOW_RATE (3 of 12 is 0.25): usual, not fewer
        (12, 0, STATE.USUAL),
    ],
)
def test_states_follow_the_counts_and_the_rate(accepted: int, dismissed: int, state: STATE) -> None:
    row = _row(taste.tastes(_decisions(GONE_QUIET, accepted, dismissed), {}, NOW), GONE_QUIET)
    assert row.state is state


def test_state_labels_read_plainly() -> None:
    assert [state.label for state in STATE] == [
        "Not learning",
        "Always shown",
        "Learning",
        "As usual",
        "Showing fewer",
    ]


# --- the sentence on each row ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("detector", "accepted", "dismissed", "learn", "sentence"),
    [
        pytest.param(
            GONE_QUIET,
            2,
            12,
            True,
            "You accepted 2 of 14. Showing fewer: at most 1 a day.",
            id="fewer",
        ),
        pytest.param(
            GONE_QUIET,
            6,
            3,
            True,
            "You accepted 6 of 9. Learning: 1 more decision before it adapts.",
            id="learning-one-more",
        ),
        pytest.param(
            GONE_QUIET,
            1,
            3,
            True,
            "You accepted 1 of 4. Learning: 6 more decisions before it adapts.",
            id="learning-plural",
        ),
        pytest.param(
            GONE_QUIET,
            7,
            5,
            True,
            "You accepted 7 of 12. Shown as usual.",
            id="usual",
        ),
        pytest.param(
            HEALTH,
            0,
            0,
            True,
            "Always shown: Wardmind never holds these back.",
            id="protected-no-decisions",
        ),
        pytest.param(
            HEALTH,
            3,
            1,
            True,
            "You accepted 3 of 4. Always shown: Wardmind never holds these back.",
            id="protected-with-decisions",
        ),
        pytest.param(GONE_QUIET, 0, 0, True, "No decisions yet.", id="none-yet"),
        pytest.param(GONE_QUIET, 2, 8, False, "Learning is off.", id="off"),
    ],
)
def test_each_sentence_form(
    detector: Detector, accepted: int, dismissed: int, learn: bool, sentence: str
) -> None:
    found = taste.tastes(_decisions(detector, accepted, dismissed), {}, NOW, learn=learn)
    assert _row(found, detector).sentence == sentence


def test_the_fewer_sentence_names_a_larger_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(taste, "FEWER_PER_RUN", 2)
    row = _row(taste.tastes(_decisions(GONE_QUIET, 2, 12), {}, NOW), GONE_QUIET)
    assert row.sentence == "You accepted 2 of 14. Showing fewer: at most 2 a day."


# --- fewer() ---------------------------------------------------------------------------------


def test_fewer_lists_only_the_detectors_showing_fewer() -> None:
    items = [
        *_decisions(GONE_QUIET, dismissed=10),  # 0 of 10: showing fewer
        *_decisions(REPLY, accepted=9, dismissed=1),  # 9 of 10: as usual
        *_decisions(HEALTH, dismissed=10),  # 0 of 10, but always shown
        *_decisions(THIN, dismissed=3),  # still learning
    ]
    found = taste.tastes(items, {}, NOW)
    assert taste.fewer(found) == frozenset({GONE_QUIET})
    assert taste.fewer([]) == frozenset()


# --- Proposals: dismiss() keeps the reason, reset_taste() is kept in the file -----------------


@pytest.mark.parametrize(
    "reason",
    [
        None,
        DismissReason.NOT_NOW,
        DismissReason.ALREADY_ON_IT,
        DismissReason.NOT_USEFUL,
        DismissReason.WRONG,
    ],
)
def test_dismiss_stores_the_reason_it_was_given(tmp_path, reason: DismissReason | None) -> None:
    store = proposals.Proposals(LocalStore(tmp_path))
    item = store.add(_open_item(1))
    dismissed = proposals.dismiss(store, item.id, now=NOW, reason=reason)
    assert dismissed.status is ProposalStatus.DISMISSED
    assert dismissed.dismiss_reason is reason
    reopened = proposals.Proposals(LocalStore(tmp_path))
    assert reopened.get(item.id).dismiss_reason is reason


def test_a_plain_dismiss_stores_no_reason(tmp_path) -> None:
    store = proposals.Proposals(LocalStore(tmp_path))
    item = store.add(_open_item(1))
    proposals.dismiss(store, item.id, now=NOW)
    assert store.get(item.id).dismiss_reason is None


def test_reset_taste_is_kept_in_the_file_and_forgets_only_that_detector(tmp_path) -> None:
    store = proposals.Proposals(LocalStore(tmp_path))
    for index in range(10):
        item = store.add(_open_item(index))
        proposals.dismiss(store, item.id, now=RECENT, reason=DismissReason.NOT_USEFUL)
    other = store.add(_open_item(99, detector=REPLY))
    proposals.dismiss(store, other.id, now=RECENT, reason=DismissReason.NOT_USEFUL)

    before = taste.tastes(store.all(), store.load().taste_resets, NOW)
    assert _row(before, GONE_QUIET).state is STATE.FEWER

    store.reset_taste(GONE_QUIET, NOW.replace(tzinfo=UTC))  # stored as naive UTC
    reopened = proposals.Proposals(LocalStore(tmp_path))
    data = reopened.load()
    assert data.taste_resets == {GONE_QUIET: NOW}
    assert len(reopened.all()) == 11  # the decisions themselves are kept

    after = taste.tastes(reopened.all(), data.taste_resets, NOW)
    gone = _row(after, GONE_QUIET)
    assert (gone.state, gone.accepted, gone.dismissed) == (STATE.LEARNING, 0, 0)
    assert (gone.reset_at, gone.sentence) == (NOW, "No decisions yet.")
    assert _row(after, REPLY).dismissed == 1
    assert _row(after, REPLY).reset_at is None
