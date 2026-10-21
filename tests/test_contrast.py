"""WCAG contrast for the colour tokens in app.css, checked in both themes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (
    Path(__file__).resolve().parents[1] / "src" / "cs_brief" / "web" / "static" / "css" / "app.css"
)

# The light values sit in the plain :root block; the dark values sit in two blocks that must agree.
LIGHT_BLOCK = re.compile(r":root\s*\{([^}]*)\}")
DARK_BLOCKS = {
    "prefers-color-scheme": re.compile(r':root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}'),
    "data-theme": re.compile(r':root\[data-theme="dark"\]\s*\{([^}]*)\}'),
}
TOKEN = re.compile(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})\s*;")

TEXT_TOKENS = ["ink", "ink-muted", "accent", "risk", "watch", "healthy"]
UI_TOKENS = ["control", "bar"]
BACKGROUNDS = ["paper", "surface"]


def _block_tokens(pattern: re.Pattern[str]) -> dict[str, str]:
    blocks = pattern.findall(CSS.read_text(encoding="utf-8"))
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


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_colour_token_is_defined(theme: str) -> None:
    expected = {"paper", "surface", "ink", "ink-muted", "line", "control", "accent"}
    expected |= {"risk", "watch", "healthy", "bar"}
    assert set(THEMES[theme]) >= expected


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("foreground", TEXT_TOKENS)
def test_text_meets_4_5_to_1(theme: str, foreground: str, background: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens[foreground], tokens[background])
    assert ratio >= 4.5, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("background", BACKGROUNDS)
@pytest.mark.parametrize("foreground", UI_TOKENS)
def test_ui_strokes_meet_3_to_1(theme: str, foreground: str, background: str) -> None:
    tokens = THEMES[theme]
    ratio = _contrast(tokens[foreground], tokens[background])
    assert ratio >= 3.0, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"
