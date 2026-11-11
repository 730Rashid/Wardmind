"""The activity log (core/audit.py): recording and reading entries, the detail's rules, the book id
rule, trimming to KEEP lines, damaged lines, record() never raising, and clear().

Runs against the temp secrets folder from conftest (WARDMIND_SECRETS_DIR), so the log is never
written to the real %LOCALAPPDATA%\\wardmind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wardmind.core import audit, config
from wardmind.core.secrets import ENV_SECRETS_DIR


def _log() -> Path:
    return audit._log_path()


def _lines() -> list[str]:
    return [line for line in _log().read_text(encoding="utf-8").split("\n") if line]


def test_a_recorded_entry_is_read_back_with_its_fields() -> None:
    audit.record(audit.Event.BOOK_ADDED, book="b-0a1b2c3d", detail="Book added")
    (entry,) = audit.entries()
    assert entry.event is audit.Event.BOOK_ADDED
    assert entry.book == "b-0a1b2c3d"
    assert entry.detail == "Book added"
    assert entry.at.tzinfo is not None
    assert entry.at.utcoffset().total_seconds() == 0  # stored in UTC


def test_entries_are_newest_first_and_limited() -> None:
    audit.record(audit.Event.UNLOCKED)
    audit.record(audit.Event.LOCKED, detail="by you")
    audit.record(audit.Event.SIGN_IN)
    assert [entry.event for entry in audit.entries()] == [
        audit.Event.SIGN_IN,
        audit.Event.LOCKED,
        audit.Event.UNLOCKED,
    ]
    assert [entry.event for entry in audit.entries(2)] == [audit.Event.SIGN_IN, audit.Event.LOCKED]
    assert audit.entries(0) == []


def test_an_empty_or_missing_log_reads_as_no_entries() -> None:
    assert audit.entries() == []
    assert audit.last(audit.Event.UNLOCKED) is None


def test_last_finds_the_newest_entry_of_a_kind_and_of_a_book() -> None:
    audit.record(audit.Event.BOOK_RENAMED, book="b-00000001", detail="Book renamed")
    audit.record(audit.Event.BOOK_RENAMED, book="b-00000002", detail="Book renamed")
    audit.record(audit.Event.LOCKED, detail="by you")
    newest = audit.last(audit.Event.BOOK_RENAMED)
    assert newest is not None and newest.book == "b-00000002"
    first_book = audit.last(audit.Event.BOOK_RENAMED, book="b-00000001")
    assert first_book is not None and first_book.book == "b-00000001"
    assert audit.last(audit.Event.BOOK_DELETED) is None
    assert audit.last(audit.Event.BOOK_RENAMED, book="b-0000ffff") is None


def test_a_book_is_kept_only_as_an_id_never_a_label() -> None:
    audit.record(audit.Event.BOOK_ADDED, book="Sarah's renewals", detail="Book added")
    audit.record(audit.Event.BOOK_ADDED, book="default", detail="Book added")
    newest, oldest = audit.entries()  # newest first
    assert newest.book == "default"
    assert oldest.book is None
    assert "Sarah" not in _log().read_text(encoding="utf-8")


def test_the_detail_loses_control_characters_and_is_cut_to_the_limit() -> None:
    audit.record(audit.Event.SETTINGS_SAVED, detail="Line one\nLine two\tand\x07bell end")
    audit.record(audit.Event.SETTINGS_SAVED, detail="x" * 500)
    newest, first = audit.entries()
    assert newest.detail == "x" * audit.MAX_DETAIL
    assert len(newest.detail) == audit.MAX_DETAIL
    assert first.detail == "Line one Line two and bell end"
    assert all(ch not in _log().read_text(encoding="utf-8") for ch in ("\x07", " "))


def test_every_event_has_a_group_and_plain_words() -> None:
    for event in audit.Event:
        assert isinstance(event.group, audit.Group), event
        assert event.label and event.label[0].isupper(), event
        assert not event.label.endswith("."), event
    assert audit.Event.UNLOCK_FAILED.label == "Unlock failed"
    assert audit.Event.UNLOCK_FAILED.group is audit.Group.SECURITY
    assert audit.Event.BOOK_ADDED.group is audit.Group.DATA
    assert audit.Event.SETTINGS_SAVED.group is audit.Group.SETTINGS


def test_the_log_is_trimmed_to_its_newest_entries_once_it_passes_the_limit() -> None:
    # The file is rewritten when it passes KEEP + 200 lines: the entry that makes it so leaves
    # exactly KEEP lines behind, the newest ones.
    total = audit.KEEP + 201
    for number in range(total):
        audit.record(audit.Event.SETTINGS_SAVED, detail=f"save {number}")
    lines = _lines()
    assert len(lines) == audit.KEEP
    assert audit.entries(1)[0].detail == f"save {total - 1}"
    assert json.loads(lines[0])["detail"] == f"save {total - audit.KEEP}"


def test_a_log_that_already_holds_too_many_lines_is_trimmed_on_the_next_entry() -> None:
    # Written by an older build or by hand: the count isn't known until the file is read.
    _log().parent.mkdir(parents=True, exist_ok=True)
    old = [
        json.dumps({"at": "2026-11-01T09:00:00Z", "event": "unlocked", "detail": f"old {n}"})
        for n in range(audit.KEEP + 500)
    ]
    _log().write_text("\n".join(old) + "\n", encoding="utf-8")
    audit.record(audit.Event.LOCKED, detail="by you")
    assert len(_lines()) == audit.KEEP
    assert audit.entries(1)[0].event is audit.Event.LOCKED


def test_a_damaged_line_is_skipped_and_the_rest_still_read() -> None:
    audit.record(audit.Event.UNLOCKED)
    with _log().open("a", encoding="utf-8") as handle:
        handle.write('{"at": "not a time", "event": "locked"\n')  # cut short
        handle.write('{"event": "no_such_event", "at": "2026-11-11T09:00:00Z"}\n')
        handle.write("\n")
    audit.record(audit.Event.LOCKED, detail="by you")
    events = [entry.event for entry in audit.entries()]
    assert events == [audit.Event.LOCKED, audit.Event.UNLOCKED]


def test_an_entry_after_a_cut_short_line_is_not_glued_to_it() -> None:
    audit.record(audit.Event.UNLOCKED)
    with _log().open("a", encoding="utf-8") as handle:
        handle.write('{"at": "2026-11')  # no line break: a crash mid-write
    audit.record(audit.Event.LOCKED, detail="by you")
    assert [entry.event for entry in audit.entries()] == [audit.Event.LOCKED, audit.Event.UNLOCKED]


def test_record_never_raises_when_the_folder_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    monkeypatch.setenv(ENV_SECRETS_DIR, str(blocker))
    audit.record(audit.Event.UNLOCKED)  # no exception
    assert audit.entries() == []


def test_record_never_raises_when_the_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    audit.record(audit.Event.UNLOCKED)
    with monkeypatch.context() as patch:  # undo() here would also reset the temp folder
        patch.setattr(audit, "_append", refuse)
        audit.record(audit.Event.LOCKED)  # swallowed
    assert [entry.event for entry in audit.entries()] == [audit.Event.UNLOCKED]


def test_record_never_raises_for_anything_unexpected(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Path:
        raise RuntimeError("no folder at all")

    with monkeypatch.context() as patch:
        patch.setattr(audit, "root_dir", broken)
        audit.record(audit.Event.UNLOCKED, detail=object())  # type: ignore[arg-type]
    assert audit.entries() == []


def test_entries_never_raise_when_the_log_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    audit.record(audit.Event.UNLOCKED)

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("locked by another program")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert audit.entries() == []
    assert audit.last(audit.Event.UNLOCKED) is None


def test_clear_empties_the_log_and_records_the_clearing_first() -> None:
    audit.record(audit.Event.UNLOCKED)
    audit.record(audit.Event.LOCKED, detail="by you")
    audit.clear()
    (only,) = audit.entries()
    assert only.event is audit.Event.LOG_CLEARED
    assert len(_lines()) == 1


def test_clear_on_a_failed_write_changes_nothing_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit.record(audit.Event.UNLOCKED)

    def refuse(path: Path, text: str) -> None:
        raise OSError("read-only folder")

    with monkeypatch.context() as patch:
        patch.setattr(config, "write_atomic", refuse)
        audit.clear()  # no exception
    assert [entry.event for entry in audit.entries()] == [audit.Event.UNLOCKED]
