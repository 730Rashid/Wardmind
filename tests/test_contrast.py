"""WCAG contrast for the colour tokens in app.css, checked in both themes and for every accent."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (
    Path(__file__).resolve().parents[1] / "src" / "wardmind" / "web" / "static" / "css" / "app.css"
)
ACCENTS = ["plum", "blue", "petrol", "graphite"]

# The light palette sits in the plain :root block; the dark palette sits in two blocks that must
# agree. Each accent choice is a selector that points --accent at its own palette token.
LIGHT_BLOCK = re.compile(r":root\s*\{([^}]*)\}")
DARK_BLOCKS = {
    "prefers-color-scheme": re.compile(r':root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}'),
    "data-theme": re.compile(r':root\[data-theme="dark"\]\s*\{([^}]*)\}'),
}
ACCENT_BLOCK = re.compile(r':root\[data-accent="(\w+)"\]\s*\{([^}]*)\}')
ACCENT_POINTER = re.compile(r"--accent:\s*var\(--accent-(\w+)\)\s*;")
PRINT_BLOCK = re.compile(r"@media print\s*\{.*?\bhtml\s*\{([^}]*)\}", re.DOTALL)
TOKEN = re.compile(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})\s*;")
PRINT_TOKEN = re.compile(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})\s*!important\s*;")

TEXT_TOKENS = ["ink", "ink-muted", "risk", "watch", "healthy"]
ACCENT_TOKENS = [f"accent-{name}" for name in ACCENTS]
UI_TOKENS = ["control", "bar"]
BACKGROUNDS = ["paper", "surface"]


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _block_tokens(pattern: re.Pattern[str]) -> dict[str, str]:
    blocks = pattern.findall(_css())
    assert len(blocks) == 1, f"expected one block for {pattern.pattern}"
    return {name: value.upper() for name, value in TOKEN.findall(blocks[0])}


def _theme_tokens() -> dict[str, dict[str, str]]:
    dark_media = _block_tokens(DARK_BLOCKS["prefers-color-scheme"])
    dark_forced = _block_tokens(DARK_BLOCKS["data-theme"])
    assert dark_media == dark_forced, "the two dark blocks must hold the same values"
    return {"light": _block_tokens(LIGHT_BLOCK), "dark": dark_forced}


THEMES = _theme_tokens()


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_accent_has_a_selector_that_points_at_its_palette_token() -> None:
    selectors = dict(ACCENT_BLOCK.findall(_css()))
    assert sorted(selectors) == sorted(ACCENTS)
    for name, body in selectors.items():
        match = ACCENT_POINTER.search(body)
        assert match and match.group(1) == name, f"accent {name} does not point at its token"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_colour_token_is_defined(theme: str) -> None:
    expected = {"paper", "surface", "ink", "ink-muted", "line", "control", "on-accent"}
    expected |= {"risk", "watch", "healthy", "bar"} | set(ACCENT_TOKENS)
    assert set(THEMES[theme]) >= expected


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("foreground", TEXT_TOKENS + ACCENT_TOKENS)
def test_text_meets_4_5_to_1(theme: str, foreground: str, background: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens[foreground], tokens[background])
    assert ratio >= 4.5, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("accent", ACCENT_TOKENS)
def test_button_text_on_every_accent_meets_4_5_to_1(theme: str, accent: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens["on-accent"], tokens[accent])
    assert ratio >= 4.5, f"{theme}: on-accent on {accent} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("foreground", UI_TOKENS)
def test_ui_strokes_meet_3_to_1(theme: str, foreground: str, background: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens[foreground], tokens[background])
    assert ratio >= 3.0, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"


TAG_HUES = ["violet", "blue", "green", "pink", "orange", "red", "slate"]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("hue", TAG_HUES)
def test_tag_text_meets_4_5_to_1_on_its_own_background(theme: str, hue: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens[f"tag-{hue}-ink"], tokens[f"tag-{hue}-bg"])
    assert ratio >= 4.5, f"{theme}: tag-{hue} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("foreground", ["ink", "ink-muted", *ACCENT_TOKENS])
def test_text_on_the_sidebar_meets_4_5_to_1(theme: str, foreground: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens[foreground], tokens["sunken"])
    assert ratio >= 4.5, f"{theme}: {foreground} on sunken is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_checkbox_and_input_borders_meet_3_to_1_on_the_sidebar(theme: str) -> None:
    tokens = THEMES[theme]
    assert _contrast(tokens["control"], tokens["sunken"]) >= 3.0


def test_print_forces_the_light_palette() -> None:
    """Print is always light, so the printed values must be the light palette (paper and surface
    are white). Every accent therefore keeps its light contrast on paper."""
    (body,) = PRINT_BLOCK.findall(_css())
    printed = {name: value.upper() for name, value in PRINT_TOKEN.findall(body)}
    light = THEMES["light"]
    assert set(light) <= set(printed)
    for name in light:
        expected = "#FFFFFF" if name in ("paper", "surface") else light[name]
        assert printed[name] == expected, f"print token --{name} is {printed[name]}"
