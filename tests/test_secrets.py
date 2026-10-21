"""The HubSpot token store: where it lives, what is shown of it, and how it is saved and removed."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from cs_brief.core import secrets
from cs_brief.core.config import config_dir
from cs_brief.core.secrets import (
    ENV_SECRETS_DIR,
    ENV_TOKEN,
    MAX_TOKEN_LENGTH,
    MIN_TOKEN_LENGTH,
    TOKEN_FILE,
    TokenSource,
    delete_hubspot_token,
    get_hubspot_token,
    mask_token,
    save_hubspot_token,
    secrets_dir,
    token_status,
    validate_token,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# A token with the shape HubSpot uses, and the same length as a real one. Never a real token.
TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"
OTHER = "pat-na1-OTHERTOKEN-0000-1111-2222-333344445555"


@pytest.fixture
def store() -> Path:
    """The secrets folder the autouse fixture points at, for the tests that look inside it."""
    return Path(os.environ[ENV_SECRETS_DIR])


# mask_token


@pytest.mark.parametrize(
    ("token", "masked"),
    [
        ("pat-na1-0123abcd-0000-1111-2222-3333444455559f3e", "pat-na1-…9f3e"),
        ("abcdef0123456789xyz1", "…xyz1"),
        ("0123abcd-0000-1111-2222-333344445555", "…5555"),  # long leading segment is not shown
        ("pat-abcdefghijklmnopqrstuvwxyz", "…wxyz"),  # one hyphen only, no shape to show
        ("a" * 19, "…"),  # just under the minimum: only the ellipsis
        ("a" * MIN_TOKEN_LENGTH, "…" + "a" * 4),
        ("", "…"),
    ],
)
def test_mask_shows_the_shape_and_the_last_four_characters(token: str, masked: str) -> None:
    assert mask_token(token) == masked


def test_mask_never_shows_the_middle_of_a_token() -> None:
    masked = mask_token(TOKEN)
    assert masked == "pat-na1-…5555"
    assert "SECRETTEST" not in masked
    assert "0000" not in masked


# Lookup order


def test_no_token_means_none(store: Path) -> None:
    assert get_hubspot_token() is None
    assert token_status().source is TokenSource.NONE


def test_a_saved_token_is_read_back(store: Path) -> None:
    save_hubspot_token(TOKEN)
    assert get_hubspot_token() == TOKEN


def test_the_environment_wins_over_the_saved_file(store: Path, monkeypatch) -> None:
    save_hubspot_token(OTHER)
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    assert get_hubspot_token() == TOKEN
    assert token_status().source is TokenSource.ENV


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_environment_variable_falls_through_to_the_file(
    store: Path, monkeypatch, blank: str
) -> None:
    save_hubspot_token(TOKEN)
    monkeypatch.setenv(ENV_TOKEN, blank)
    assert get_hubspot_token() == TOKEN
    assert token_status().source is TokenSource.FILE


def test_the_environment_token_is_stripped(store: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV_TOKEN, f"  {TOKEN}\n")
    assert get_hubspot_token() == TOKEN


def test_a_blank_file_is_no_token(store: Path) -> None:
    (store / TOKEN_FILE).write_text("   \n", encoding="utf-8")
    assert get_hubspot_token() is None
    assert token_status().source is TokenSource.NONE


def test_an_unreadable_file_is_no_token(store: Path) -> None:
    (store / TOKEN_FILE).write_bytes(b"\xff\xfe\xfd")
    assert get_hubspot_token() is None


# Status


def test_status_for_the_file_has_the_mask_and_the_saved_time(store: Path) -> None:
    before = datetime.now().replace(microsecond=0)
    save_hubspot_token(TOKEN)
    status = token_status()
    assert status.source is TokenSource.FILE
    assert status.masked == "pat-na1-…5555"
    assert status.saved_at is not None
    assert before <= status.saved_at.replace(microsecond=0) <= datetime.now()


def test_status_for_the_environment_has_no_saved_time(store: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    status = token_status()
    assert status.source is TokenSource.ENV
    assert status.masked == "pat-na1-…5555"
    assert status.saved_at is None


def test_status_for_no_token_has_nothing(store: Path) -> None:
    status = token_status()
    assert (status.source, status.masked, status.saved_at) == (TokenSource.NONE, None, None)


# Save and delete


def test_save_writes_the_bare_token_and_creates_the_folder(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "not-yet" / "cs-brief"
    monkeypatch.setenv(ENV_SECRETS_DIR, str(folder))
    save_hubspot_token(f"  {TOKEN}  ")
    assert (folder / TOKEN_FILE).read_text(encoding="utf-8") == TOKEN
    assert (folder / TOKEN_FILE).read_bytes() == TOKEN.encode("utf-8")  # no trailing newline


def test_save_replaces_an_existing_token(store: Path) -> None:
    save_hubspot_token(OTHER)
    save_hubspot_token(TOKEN)
    assert get_hubspot_token() == TOKEN


def test_no_temporary_file_is_left_behind(store: Path) -> None:
    save_hubspot_token(TOKEN)
    save_hubspot_token(OTHER)
    assert sorted(path.name for path in store.iterdir()) == [TOKEN_FILE]
    assert not list(store.rglob("*.tmp"))


def test_a_failed_write_keeps_the_old_token_and_leaves_no_temporary_file(
    store: Path, monkeypatch
) -> None:
    save_hubspot_token(OTHER)

    def refuse(src, dst):
        raise OSError("disk full")

    # A scoped patch: monkeypatch.undo() would also undo the autouse folder settings.
    with monkeypatch.context() as patch:
        patch.setattr(secrets.os, "replace", refuse)
        with pytest.raises(OSError):
            save_hubspot_token(TOKEN)
    assert get_hubspot_token() == OTHER
    assert sorted(path.name for path in store.iterdir()) == [TOKEN_FILE]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_the_token_file_is_private_on_posix(store: Path) -> None:
    save_hubspot_token(TOKEN)
    assert (store / TOKEN_FILE).stat().st_mode & 0o777 == 0o600


def test_delete_removes_the_file_and_reports_it(store: Path) -> None:
    save_hubspot_token(TOKEN)
    assert delete_hubspot_token() is True
    assert not (store / TOKEN_FILE).exists()
    assert get_hubspot_token() is None


def test_delete_when_there_is_no_file_reports_false(store: Path) -> None:
    assert delete_hubspot_token() is False


def test_delete_leaves_the_environment_token_alone(store: Path, monkeypatch) -> None:
    save_hubspot_token(OTHER)
    monkeypatch.setenv(ENV_TOKEN, TOKEN)
    assert delete_hubspot_token() is True
    assert get_hubspot_token() == TOKEN


# Validation


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_a_blank_token_asks_for_one(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        validate_token(value)
    assert str(caught.value) == "Paste an access token first."


@pytest.mark.parametrize(
    "value",
    [f"{TOKEN[:20]} {TOKEN[20:]}", f"{TOKEN[:20]}\n{TOKEN[20:]}", f"{TOKEN[:20]}\t{TOKEN[20:]}"],
)
def test_inner_whitespace_is_refused_and_not_echoed(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        validate_token(value)
    assert str(caught.value) == "An access token can't contain spaces or line breaks."
    assert TOKEN not in str(caught.value)


def test_a_short_token_asks_for_the_whole_token() -> None:
    short = "x" * (MIN_TOKEN_LENGTH - 1)
    with pytest.raises(ValueError) as caught:
        validate_token(short)
    assert str(caught.value) == (
        "That doesn't look like a full access token. Check you copied all of it."
    )
    assert short not in str(caught.value)


def test_a_long_token_asks_for_only_the_token() -> None:
    long = "x" * (MAX_TOKEN_LENGTH + 1)
    with pytest.raises(ValueError) as caught:
        validate_token(long)
    assert str(caught.value) == (
        "That's longer than any HubSpot access token. Check you copied only the token."
    )


def test_the_length_limits_are_inclusive() -> None:
    assert validate_token("x" * MIN_TOKEN_LENGTH) == "x" * MIN_TOKEN_LENGTH
    assert validate_token("x" * MAX_TOKEN_LENGTH) == "x" * MAX_TOKEN_LENGTH


def test_a_valid_token_comes_back_stripped() -> None:
    assert validate_token(f"  {TOKEN}\n") == TOKEN


def test_a_refused_token_is_not_written(store: Path) -> None:
    with pytest.raises(ValueError):
        save_hubspot_token("pat-na1 with spaces in it")
    assert not (store / TOKEN_FILE).exists()


# Where the folder is


def test_the_environment_overrides_the_folder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV_SECRETS_DIR, str(tmp_path))
    assert secrets_dir() == tmp_path


@pytest.mark.skipif(os.name != "nt", reason="the Windows default")
def test_the_windows_default_is_under_local_app_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_SECRETS_DIR)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert secrets_dir() == tmp_path / "cs-brief"


@pytest.mark.skipif(os.name == "nt", reason="the POSIX default")
def test_the_posix_default_follows_xdg_state_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_SECRETS_DIR)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert secrets_dir() == tmp_path / "cs-brief"


def test_the_default_folder_is_never_in_the_repo_or_the_config_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_SECRETS_DIR)
    folder = secrets_dir().resolve()
    assert not folder.is_relative_to(REPO_ROOT)
    assert not folder.is_relative_to(config_dir().resolve())
    assert not folder.is_relative_to(Path.cwd().resolve())
