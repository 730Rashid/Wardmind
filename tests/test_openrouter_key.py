"""The OpenRouter API key: env var or file, how it is shown, and how it is saved and removed. The
HubSpot token shares the same helpers, so these tests also check the two stay separate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cs_brief.core import secrets
from cs_brief.core.secrets import (
    ENV_OPENROUTER_KEY,
    ENV_SECRETS_DIR,
    ENV_TOKEN,
    OPENROUTER_KEY_FILE,
    TOKEN_FILE,
    TokenSource,
    delete_openrouter_key,
    get_hubspot_token,
    get_openrouter_key,
    openrouter_key_status,
    save_hubspot_token,
    save_openrouter_key,
    validate_openrouter_key,
)

KEY = "sk-or-v1-FAKEKEYFORTESTS0000000000000000000000000000000000000000"
OTHER_KEY = "sk-or-v1-OTHERFAKEKEY00000000000000000000000000000000000000000"
TOKEN = "pat-na1-SECRETTEST-0000-1111-2222-333344445555"


@pytest.fixture
def folder() -> Path:
    return Path(os.environ[ENV_SECRETS_DIR])


def test_with_nothing_saved_there_is_no_key() -> None:
    assert get_openrouter_key() is None
    status = openrouter_key_status()
    assert status.source is TokenSource.NONE
    assert status.masked is None
    assert status.saved_at is None


def test_a_saved_key_is_read_back_from_the_key_file(folder: Path) -> None:
    save_openrouter_key(KEY)
    assert get_openrouter_key() == KEY
    assert (folder / OPENROUTER_KEY_FILE).read_text(encoding="utf-8") == KEY
    status = openrouter_key_status()
    assert status.source is TokenSource.FILE
    assert status.saved_at is not None


def test_the_environment_variable_wins_over_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    save_openrouter_key(KEY)
    monkeypatch.setenv(ENV_OPENROUTER_KEY, f"  {OTHER_KEY}  ")
    assert get_openrouter_key() == OTHER_KEY
    status = openrouter_key_status()
    assert status.source is TokenSource.ENV
    assert status.saved_at is None


def test_a_blank_environment_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPENROUTER_KEY, "   ")
    save_openrouter_key(KEY)
    assert get_openrouter_key() == KEY


def test_only_the_last_four_characters_are_ever_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    save_openrouter_key(KEY)
    assert openrouter_key_status().masked == "…" + KEY[-4:]
    monkeypatch.setenv(ENV_OPENROUTER_KEY, KEY)
    masked = openrouter_key_status().masked or ""
    assert masked == "…" + KEY[-4:]
    assert "sk-or" not in masked and "FAKEKEY" not in masked


def test_a_short_key_shows_only_the_ellipsis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPENROUTER_KEY, "a" * 19)
    assert openrouter_key_status().masked == "…"


def test_delete_removes_the_file_and_leaves_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch, folder: Path
) -> None:
    save_openrouter_key(KEY)
    assert delete_openrouter_key() is True
    assert not (folder / OPENROUTER_KEY_FILE).exists()
    assert delete_openrouter_key() is False
    monkeypatch.setenv(ENV_OPENROUTER_KEY, KEY)
    assert delete_openrouter_key() is False
    assert get_openrouter_key() == KEY


def test_the_hubspot_token_and_the_openrouter_key_are_kept_apart(folder: Path) -> None:
    save_hubspot_token(TOKEN)
    save_openrouter_key(KEY)
    assert get_hubspot_token() == TOKEN
    assert get_openrouter_key() == KEY
    assert (folder / TOKEN_FILE).read_text(encoding="utf-8") == TOKEN
    delete_openrouter_key()
    assert get_hubspot_token() == TOKEN


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "Paste an OpenRouter API key first."),
        ("   ", "Paste an OpenRouter API key first."),
        ("sk-or-v1-a b", "An OpenRouter API key can't contain spaces or line breaks."),
        (
            "sk-or-short",
            "That doesn't look like a full OpenRouter API key. Check you copied all of it.",
        ),
        (
            "x" * 1025,
            "That's longer than any OpenRouter API key. Check you copied only the key.",
        ),
    ],
)
def test_invalid_keys_are_refused_with_a_message_that_never_echoes_them(
    value: str, message: str
) -> None:
    with pytest.raises(ValueError) as caught:
        validate_openrouter_key(value)
    assert str(caught.value) == message
    if value.strip():
        assert value.strip() not in str(caught.value)


def test_a_valid_key_is_stripped() -> None:
    assert validate_openrouter_key(f"  {KEY}\n") == KEY


def test_an_invalid_save_writes_nothing(folder: Path) -> None:
    with pytest.raises(ValueError):
        save_openrouter_key("too short")
    assert not (folder / OPENROUTER_KEY_FILE).exists()
    assert get_openrouter_key() is None


def test_the_key_file_name_is_not_the_hubspot_file(folder: Path) -> None:
    save_openrouter_key(KEY)
    assert OPENROUTER_KEY_FILE != TOKEN_FILE
    assert (folder / OPENROUTER_KEY_FILE).exists()
    assert not (folder / TOKEN_FILE).exists()
    assert secrets.secrets_dir() == folder
    assert ENV_TOKEN != ENV_OPENROUTER_KEY
