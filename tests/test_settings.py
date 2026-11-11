"""Settings storage: defaults, per-field fallback on bad values, unknown keys, and atomic saves."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wardmind.core.settings import (
    Accent,
    DataSource,
    Density,
    Landing,
    Settings,
    Theme,
    load_settings,
    save_settings,
    settings_path,
)

GOOD = {"theme": "dark", "accent": "petrol", "density": "compact", "landing": "brief"}


def test_round_trip_keeps_every_choice(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    chosen = Settings(
        theme=Theme.DARK, accent=Accent.PETROL, density=Density.COMPACT, landing=Landing.BRIEF
    )
    save_settings(chosen, path)
    assert load_settings(path) == chosen
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Wardmind settings")
    positions = [text.index(f"{field}:") for field in GOOD]
    assert positions == sorted(positions), "fields are written in schema order"


def test_defaults_are_light_plum_comfortable_book() -> None:
    assert Settings() == Settings(
        theme=Theme.LIGHT, accent=Accent.PLUM, density=Density.COMFORTABLE, landing=Landing.BOOK
    )


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "settings.yaml") == Settings()


def test_default_path_follows_the_config_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WARDMIND_CONFIG_DIR", str(tmp_path))
    assert settings_path() == tmp_path / "settings.yaml"
    save_settings(Settings(theme=Theme.LIGHT))
    assert load_settings().theme == Theme.LIGHT


@pytest.mark.parametrize("field", list(GOOD))
@pytest.mark.parametrize("bad", ["purple", 3, None, ["dark"]])
def test_an_invalid_field_falls_back_on_its_own(tmp_path: Path, field: str, bad: object) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({**GOOD, field: bad}), encoding="utf-8")
    loaded = load_settings(path)
    assert getattr(loaded, field) == Settings.model_fields[field].default
    for other, value in GOOD.items():
        if other != field:
            assert getattr(loaded, other).value == value


@pytest.mark.parametrize(
    "text",
    ["- dark\n- compact\n", "just some words\n", "42\n", "", "theme: [dark\n"],
)
def test_a_file_that_is_not_a_mapping_gives_defaults(tmp_path: Path, text: str) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_settings(path) == Settings()


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        yaml.safe_dump({"theme": "light", "font_size": 20, "extra": {"a": 1}}), encoding="utf-8"
    )
    assert load_settings(path) == Settings(theme=Theme.LIGHT)


def test_save_overwrites_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    save_settings(Settings(theme=Theme.DARK), path)
    save_settings(Settings(theme=Theme.LIGHT), path)
    assert load_settings(path).theme == Theme.LIGHT
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.yaml"]


@pytest.mark.parametrize(("landing", "path"), [(Landing.BOOK, "/"), (Landing.BRIEF, "/brief")])
def test_landing_path(landing: Landing, path: str) -> None:
    assert landing.path == path


def test_the_data_sources_are_demo_and_your_data() -> None:
    assert [source.value for source in DataSource] == ["demo", "mine"]
    assert DataSource.DEMO.label == "Demo data"
    assert DataSource.MINE.label == "Your data"
    assert Settings().source is DataSource.DEMO


def test_your_data_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    save_settings(Settings(source=DataSource.MINE), path)
    assert load_settings(path).source is DataSource.MINE


def test_a_saved_hubspot_source_loads_as_your_data(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("source: hubspot\n", encoding="utf-8")
    assert load_settings(path).source is DataSource.MINE
    assert Settings.model_validate({"source": "hubspot"}).source is DataSource.MINE


def test_an_unknown_source_falls_back_to_demo_data(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("source: salesforce\ntheme: dark\n", encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.source is DataSource.DEMO
    assert loaded.theme is Theme.DARK
