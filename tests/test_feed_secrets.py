"""Named secrets (each data feed's key, token or password): where they live, the rules for names and
values, what is shown of them, and how they are renamed and deleted.

Every value here is a made-up test string. The secrets folder is the temp one from conftest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cs_brief.core import secrets
from cs_brief.core.secrets import (
    FEED_SECRETS_DIR,
    MAX_SECRET_LENGTH,
    TokenSource,
    delete_secret,
    get_secret,
    mask_token,
    rename_secret,
    save_secret,
    secret_status,
    secrets_dir,
    validate_secret,
)

KEY = "test-key-0123456789abcdef"
OTHER = "test-key-fedcba9876543210"
# Control and separator characters are built with chr() so no editor or tool can alter them.
NUL = chr(0)
LINE_SEPARATOR = chr(0x2028)


def _feeds_folder() -> Path:
    return secrets_dir() / FEED_SECRETS_DIR


@pytest.mark.parametrize("name", ["feed-1", "feed-42", "draft-0123456789abcdef", "a"])
def test_valid_names_round_trip(name: str) -> None:
    save_secret(name, KEY)
    assert get_secret(name) == KEY


def test_the_value_is_saved_in_the_feeds_folder_under_its_name() -> None:
    save_secret("feed-3", KEY)
    assert (_feeds_folder() / "feed-3").read_text(encoding="utf-8") == KEY


def test_saved_value_is_stripped_at_both_ends() -> None:
    save_secret("feed-1", f"  {KEY}\n")
    assert get_secret("feed-1") == KEY


def test_a_value_with_spaces_inside_is_allowed() -> None:
    save_secret("feed-1", "correct horse 123456")
    assert get_secret("feed-1") == "correct horse 123456"


def test_missing_secret_reads_as_none() -> None:
    assert get_secret("feed-9") is None


@pytest.mark.parametrize(
    "name",
    ["Feed-1", "../feed-1", "feed/1", "feed\\1", "", "-feed", "feed 1", "feed.1", "x" * 65],
)
def test_invalid_names_are_refused_without_touching_a_file(name: str) -> None:
    with pytest.raises(ValueError):
        save_secret(name, KEY)
    assert get_secret(name) is None
    assert delete_secret(name) is False
    assert secret_status(name).source is TokenSource.NONE


@pytest.mark.parametrize("value", ["", "   ", "\t \n"])
def test_blank_values_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="Type the key, token or password first."):
        save_secret("feed-1", value)
    assert get_secret("feed-1") is None


@pytest.mark.parametrize(
    "value",
    [
        KEY + "\nmore",
        KEY + "\rmore",
        KEY + NUL,
        KEY + LINE_SEPARATOR + "more",
    ],
)
def test_line_breaks_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="line break"):
        save_secret("feed-1", value)
    assert get_secret("feed-1") is None


def test_longer_than_the_limit_is_refused_and_the_limit_itself_is_fine() -> None:
    with pytest.raises(ValueError):
        save_secret("feed-1", "x" * (MAX_SECRET_LENGTH + 1))
    save_secret("feed-1", "x" * MAX_SECRET_LENGTH)
    assert get_secret("feed-1") == "x" * MAX_SECRET_LENGTH


@pytest.mark.parametrize("value", [KEY + "\nmore", "   ", "x" * (MAX_SECRET_LENGTH + 1)])
def test_error_messages_never_echo_the_value(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        validate_secret(value)
    assert KEY not in str(caught.value)
    assert "xxxxxxxx" not in str(caught.value)


def test_status_is_none_until_a_value_is_saved() -> None:
    status = secret_status("feed-1")
    assert status.source is TokenSource.NONE
    assert status.masked is None
    assert status.saved_at is None


def test_status_shows_the_masked_value_and_the_saved_time() -> None:
    save_secret("feed-1", KEY)
    status = secret_status("feed-1")
    assert status.source is TokenSource.FILE
    assert status.masked == mask_token(KEY)
    assert status.saved_at is not None
    assert KEY not in (status.masked or "")


def test_masking_never_shows_the_middle_of_a_value() -> None:
    value = "pat-na1-MIDDLESECRET-0000-1111-2222-3333"
    save_secret("feed-1", value)
    masked = secret_status("feed-1").masked or ""
    assert "MIDDLESECRET" not in masked
    assert masked.endswith(value[-4:])


def test_rename_moves_the_value_and_frees_the_old_name() -> None:
    save_secret("draft-0123456789abcdef", KEY)
    assert rename_secret("draft-0123456789abcdef", "feed-4") is True
    assert get_secret("feed-4") == KEY
    assert get_secret("draft-0123456789abcdef") is None


def test_rename_of_a_missing_secret_moves_nothing() -> None:
    assert rename_secret("draft-0123456789abcdef", "feed-4") is False
    assert get_secret("feed-4") is None


def test_rename_replaces_an_existing_secret_under_the_new_name() -> None:
    save_secret("feed-4", OTHER)
    save_secret("draft-0123456789abcdef", KEY)
    assert rename_secret("draft-0123456789abcdef", "feed-4") is True
    assert get_secret("feed-4") == KEY


def test_rename_refuses_an_invalid_name() -> None:
    save_secret("feed-1", KEY)
    with pytest.raises(ValueError):
        rename_secret("feed-1", "../escape")
    assert get_secret("feed-1") == KEY


def test_delete_removes_the_file_once() -> None:
    save_secret("feed-1", KEY)
    assert delete_secret("feed-1") is True
    assert get_secret("feed-1") is None
    assert delete_secret("feed-1") is False


def test_named_secrets_do_not_touch_the_hubspot_token_file() -> None:
    hubspot = "pat-na1-HUBSPOTTEST-0000-1111-2222-333344445555"
    secrets.save_hubspot_token(hubspot)
    save_secret("feed-1", KEY)
    delete_secret("feed-1")
    assert secrets.get_hubspot_token() == hubspot


def test_validate_secret_returns_the_stripped_value() -> None:
    assert validate_secret(f"  {KEY} ") == KEY
