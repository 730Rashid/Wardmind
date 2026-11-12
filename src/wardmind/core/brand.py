"""Your own logo in place of Wardmind's, in the top bar of every page.

One image for this installation, kept beside settings.yaml as `brand-logo.<ext>`. It is a company
mark, not customer data, so it is stored as it is (not encrypted). Images are checked exactly like
account logos (logos.check_image: PNG, JPEG, GIF, WebP or ICO by their bytes, at most 200 KB, not
tiny). SVG is never taken: an SVG file can carry script.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wardmind.core import logos
from wardmind.core.config import config_dir
from wardmind.core.logos import ImageType

STEM = "brand-logo"
_SUFFIX = {
    ImageType.PNG: ".png",
    ImageType.JPEG: ".jpg",
    ImageType.GIF: ".gif",
    ImageType.WEBP: ".webp",
    ImageType.ICO: ".ico",
}


@dataclass(frozen=True)
class BrandLogo:
    version: int  # the file's change time in nanoseconds: a new upload gets a new image address


def _files(folder: Path) -> list[Path]:
    return [folder / f"{STEM}{suffix}" for suffix in _SUFFIX.values()]


def _saved(folder: Path) -> Path | None:
    for path in _files(folder):
        if path.is_file():
            return path
    return None


def current() -> BrandLogo | None:
    """The saved logo's version, or None when Wardmind's own mark is shown."""
    path = _saved(config_dir())
    if path is None:
        return None
    try:
        return BrandLogo(version=path.stat().st_mtime_ns)
    except OSError:
        return None


def read() -> tuple[bytes, ImageType] | None:
    """The saved image and its type, checked again on the way out (a file changed by hand that is
    no longer a usable image is not served)."""
    path = _saved(config_dir())
    if path is None:
        return None
    try:
        data = path.read_bytes()
        kind = logos.check_image(data)
    except (OSError, logos.LogoError):
        return None
    return data, kind


def save(data: bytes) -> None:
    """Check `data` (logos.LogoError on a refusal) and make it the logo, replacing any other. The
    file is written beside its final name and then moved over it, so a failed write leaves the old
    logo in place."""
    kind = logos.check_image(data)
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{STEM}{_SUFFIX[kind]}"
    handle, temp = tempfile.mkstemp(prefix=f".{STEM}-", dir=folder)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    for other in _files(folder):
        if other != target:
            with contextlib.suppress(FileNotFoundError):
                other.unlink()


def remove() -> None:
    """Show Wardmind's own mark again."""
    for path in _files(config_dir()):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
