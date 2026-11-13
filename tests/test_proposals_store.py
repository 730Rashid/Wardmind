"""core/proposals.py, the contract half: the models and the Proposals store (unique keys, caps,
pruning, demo kept apart). plan/refresh/accept/dismiss are tested in tests/test_proposals.py."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from wardmind.core import proposals, service
from wardmind.core.proposals import (
    Evidence,
    Proposal,
    ProposalKind,
    Proposals,
    ProposalSource,
    ProposalStatus,
)
from wardmind.core.settings import DataSource, load_settings, save_settings
from wardmind.core.store import LocalStore

NOW = datetime(2026, 11, 13, 9, 0)
DAY = date(2026, 11, 13)


def _proposal(account_id: str = "a-1", key: str = "gone_quiet:2026-11-13", **changes) -> Proposal:
    values = {
        "id": "",
        "account_id": account_id,
        "kind": ProposalKind.FOLLOW_UP,
        "source": ProposalSource.DETECTOR,
        "key": key,
        "title": "Check in with Acme",
        "why": [Evidence(text="No contact for 21 days")],
        "text": "Check in: no contact for 21 days",
        "due": DAY,
        "created_at": NOW,
        "basis_day": DAY,
    }
    values.update(changes)
    return Proposal(**values)


@pytest.fixture
def store(tmp_path) -> Proposals:
    return Proposals(LocalStore(tmp_path))


def test_add_gives_an_id_and_keeps_it_open(store: Proposals) -> None:
    added = store.add(_proposal())
    assert added is not None
    assert added.id.startswith("p-") and len(added.id) == 10
    assert store.get(added.id) == added
    assert store.open_count() == 1
    assert store.path.name == proposals.PROPOSALS_FILE


def test_a_key_is_unique_per_account_for_ever(store: Proposals) -> None:
    first = store.add(_proposal())
    assert first is not None
    store.replace(first.model_copy(update={"status": ProposalStatus.DISMISSED, "decided_at": NOW}))
    assert store.add(_proposal()) is None
    assert store.has_key("a-1", first.key)
    assert store.add(_proposal(account_id="a-2")) is not None


def test_caps_per_account_and_book(store: Proposals, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(proposals.MAX_OPEN_PER_ACCOUNT):
        assert store.add(_proposal(key=f"k{index}")) is not None
    assert store.add(_proposal(key="one-too-many")) is None
    monkeypatch.setattr(proposals, "MAX_OPEN", 4)
    assert store.add(_proposal(account_id="a-2", key="x")) is not None
    assert store.add(_proposal(account_id="a-3", key="y")) is None


def test_add_many_counts_what_the_caps_held_back(store: Proposals) -> None:
    batch = [_proposal(key=f"k{index}") for index in range(5)] + [_proposal(key="k0")]
    added, held = store.add_many(batch)
    assert len(added) == proposals.MAX_OPEN_PER_ACCOUNT
    assert held == 5 - proposals.MAX_OPEN_PER_ACCOUNT  # the repeated key isn't counted


def test_open_is_newest_first_and_filters_by_account(store: Proposals) -> None:
    store.add(_proposal(key="old", created_at=datetime(2026, 11, 1)))
    store.add(_proposal(key="new"))
    store.add(_proposal(account_id="a-2", key="other"))
    assert [item.key for item in store.open("a-1")] == ["new", "old"]
    assert len(store.open()) == 3


def test_replace_unknown_id_is_a_lookup_error(store: Proposals) -> None:
    with pytest.raises(LookupError, match="isn't there any more"):
        store.replace(_proposal().model_copy(update={"id": "p-00000000"}))


def test_decided_ones_are_pruned_oldest_first(
    store: Proposals, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proposals, "MAX_KEPT", 2)
    monkeypatch.setattr(proposals, "MAX_OPEN_PER_ACCOUNT", 10)
    stored = [store.add(_proposal(key=f"k{index}")) for index in range(4)]
    for index, item in enumerate(stored):
        assert item is not None
        if index < 3:
            store.replace(
                item.model_copy(
                    update={
                        "status": ProposalStatus.DISMISSED,
                        "decided_at": datetime(2026, 11, 1 + index),
                    }
                )
            )
    keys = {item.key for item in store.all()}
    assert keys == {"k1", "k2", "k3"}  # k0, the oldest decided one, went; k3 is still open


def test_unreadable_file_reads_as_empty(store: Proposals) -> None:
    store.path.write_text("{not json", encoding="utf-8")
    assert store.all() == [] and store.open_count() == 0


def test_last_run_and_clear(store: Proposals) -> None:
    store.set_last_run(DAY)
    assert store.load().last_run == DAY
    store.clear()
    assert not store.path.exists()


def test_follow_up_needs_due_and_snooze_needs_until() -> None:
    with pytest.raises(ValidationError, match="due date"):
        _proposal(due=None)
    with pytest.raises(ValidationError, match="end date"):
        _proposal(kind=ProposalKind.SNOOZE, due=None)
    assert _proposal(kind=ProposalKind.SNOOZE, due=None, until=DAY).until == DAY


def test_every_proposal_says_why() -> None:
    with pytest.raises(ValidationError):
        _proposal(why=[])


def test_title_is_one_line_and_text_is_plain() -> None:
    with pytest.raises(ValidationError):
        _proposal(title="Two\nlines")
    with pytest.raises(ValidationError):
        _proposal(text="bell\x07")
    assert (
        _proposal(kind=ProposalKind.NOTE, due=None, text="line one\nline two").text.count("\n") == 1
    )


def test_demo_suggestions_are_kept_apart() -> None:
    save_settings(load_settings().model_copy(update={"source": DataSource.DEMO}))
    assert service.get_proposals().path.name == proposals.DEMO_PROPOSALS_FILE
    save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))
    assert service.get_proposals().path.name == proposals.PROPOSALS_FILE


def test_open_suggestion_count_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Proposals:
        raise OSError("disk")

    monkeypatch.setattr(service, "get_proposals", broken)
    assert service.open_suggestion_count() == 0
