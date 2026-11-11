"""Account logos: shown on My book (in place of the rank number), Today, the brief and the
account page.

Three ways an account gets a logo, best first:
1. **Uploaded** by the CSM on the account page (Your data only).
2. **Found** on the customer's own website (its home page's apple-touch-icon or icon link, else
   /apple-touch-icon.png, else /favicon.ico), only when web research is on, through the same
   guarded research.SiteClient (https only, never this computer or a private network). Nothing
   is sent to a third-party logo service: the customer list never leaves the computer.
3. Otherwise a **monogram**: the account's initials on a tile whose colour comes from its name,
   drawn by the page (no image file).

Files: one per account in store.root / store.LOGOS_DIR, named from a hash of the account id (ids
are never used in paths), with a small JSON index (logos/index.json) mapping account id ->
LogoInfo. Images are checked by their first bytes, not by what a server or upload claims; SVG is
refused (it can carry scripts). The web layer serves them from GET /logos/{account_id} with the
checked content type, nosniff and `Content-Security-Policy: default-src 'none'`.

CONTRACT FILE: models, constants and signatures are fixed; implementers escalate before changing
them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel

from wardmind.core import research
from wardmind.core.config import write_atomic
from wardmind.core.research import SiteClient, SiteUnreachable, website_host
from wardmind.core.store import LOGOS_DIR, WRITE_LOCK, LocalStore

MAX_LOGO_BYTES = 200_000
MIN_LOGO_SIDE = 16  # pixels; smaller images (when the size can be read) are refused
INDEX_FILE = "index.json"  # inside store.LOGOS_DIR

# The tones a monogram can take: each maps to the --tag-<tone>-bg / --tag-<tone>-ink tokens.
TONES: tuple[str, ...] = ("violet", "blue", "green", "pink", "orange", "slate")


class ImageType(StrEnum):
    PNG = "image/png"
    JPEG = "image/jpeg"
    GIF = "image/gif"
    WEBP = "image/webp"
    ICO = "image/x-icon"


class LogoSource(StrEnum):
    UPLOAD = "upload"
    WEBSITE = "website"

    @property
    def label(self) -> str:
        return {"upload": "Uploaded by you", "website": "From their website"}[self.value]


class LogoInfo(BaseModel):
    account_id: str
    file: str  # the file name inside LOGOS_DIR: sha256(account_id)[:24] + extension
    content_type: ImageType
    source: LogoSource
    saved_at: datetime  # naive UTC
    origin_url: str | None = None  # WEBSITE: where it was found


class Monogram(BaseModel):
    initials: str  # 1 or 2 characters, upper-cased
    tone: str  # one of TONES


class LogoError(Exception):
    """An image that can't be used, or a lookup that found none. `message` is plain English."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# File name extension per type, and the magic bytes and frame headers the checks read.
_EXTENSIONS: dict[ImageType, str] = {
    ImageType.PNG: ".png",
    ImageType.JPEG: ".jpg",
    ImageType.GIF: ".gif",
    ImageType.WEBP: ".webp",
    ImageType.ICO: ".ico",
}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# JPEG frame headers SOF0-SOF15, except DHT (C4), JPG (C8) and DAC (CC), which are not frames.
_JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Words that don't name a company, skipped for initials unless nothing else is left. "&" never
# starts a word with a letter or digit, so it needs no entry.
_IGNORED_WORDS = frozenset({"the", "and", "of", "inc", "ltd", "llc", "plc", "gmbh", "co", "corp"})
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""
_APPLE_RELS = frozenset({"apple-touch-icon", "apple-touch-icon-precomposed"})
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_FALLBACK_PATHS = ("/apple-touch-icon.png", "/favicon.ico")
# Icon links of a home page tried before the fallbacks: one lookup has 8 requests in all, the home
# page takes one and the two fallbacks two, so five links leave the budget whole.
_MAX_LINK_ICONS = 5
_CLOSE_LIMIT = 16_384  # html.parser's close() is quadratic on a long leftover on older Pythons
_SIZE = re.compile(r"(\d+)x(\d+)")


def sniff(data: bytes) -> ImageType | None:
    """The image type from the first bytes: PNG (\\x89PNG\\r\\n\\x1a\\n), JPEG (FF D8 FF), GIF
    (GIF87a / GIF89a), WEBP (RIFF....WEBP), ICO (00 00 01 00). Anything else, SVG included,
    gives None."""
    if data.startswith(_PNG_MAGIC):
        return ImageType.PNG
    if data.startswith(b"\xff\xd8\xff"):
        return ImageType.JPEG
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ImageType.GIF
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ImageType.WEBP
    if data.startswith(b"\x00\x00\x01\x00"):
        return ImageType.ICO
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the IHDR chunk that opens every PNG."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _gif_size(data: bytes) -> tuple[int, int] | None:
    """The logical screen size, little-endian, after the 6-byte signature."""
    if len(data) < 10:
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the first frame header (SOF) of a JPEG, walking its marker segments.
    Best effort: a file whose segments don't add up gives None, and is then accepted."""
    position = 2  # after the SOI marker (FF D8)
    end = len(data)
    while position < end:
        if data[position] != 0xFF:
            return None
        while position < end and data[position] == 0xFF:  # fill bytes before a marker
            position += 1
        if position >= end:
            return None
        marker = data[position]
        position += 1
        if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD7:  # no length field
            continue
        if marker in (0xD9, 0xDA):  # end of image, or the scan: no frame header came first
            return None
        if position + 2 > end:
            return None
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2:
            return None
        if marker in _JPEG_FRAME_MARKERS:
            # length (2), sample precision (1), height (2), width (2)
            if position + 7 > end:
                return None
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            return width, height
        position += length
    return None


def _pixel_size(data: bytes, kind: ImageType) -> tuple[int, int] | None:
    """Width and height of a PNG, GIF or JPEG; None for other types or when they can't be read."""
    if kind is ImageType.PNG:
        return _png_size(data)
    if kind is ImageType.GIF:
        return _gif_size(data)
    if kind is ImageType.JPEG:
        return _jpeg_size(data)
    return None


def check_image(data: bytes) -> ImageType:
    """LogoError("That file is empty.") / ("That image is larger than 200 KB. Try a smaller
    one.") / ("That isn't a PNG, JPEG, GIF, WebP or ICO image.") / ("That image is too small to
    use.") when PNG, GIF or JPEG dimensions can be read and either side is under MIN_LOGO_SIDE.
    Returns the sniffed type."""
    if not data:
        raise LogoError("That file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise LogoError("That image is larger than 200 KB. Try a smaller one.")
    kind = sniff(data)
    if kind is None:
        raise LogoError("That isn't a PNG, JPEG, GIF, WebP or ICO image.")
    size = _pixel_size(data, kind)
    if size is not None and min(size) < MIN_LOGO_SIDE:
        raise LogoError("That image is too small to use.")
    return kind


def _is_letter_or_digit(char: str) -> bool:
    return char.isalpha() or char.isdecimal()


def _word_core(word: str) -> str:
    """The word from its first letter or digit on ("(Acme)" gives "Acme"), or "" when it has
    none."""
    for index, char in enumerate(word):
        if _is_letter_or_digit(char):
            return word[index:]
    return ""


def _initial(core: str) -> str:
    """The upper-cased first letter; a letter whose upper case is more than one character (ß)
    keeps its own form."""
    first = core[0]
    upper = first.upper()
    return upper if len(upper) == 1 else first


def monogram(name: str) -> Monogram:
    """Initials: the first letter of the first two words that start with a letter or digit
    (ignoring "the", "and", "&", "of", and company suffixes "inc", "ltd", "llc", "plc", "gmbh",
    "co", "corp", case-insensitively, unless nothing else is left); one word gives its first
    letter only. Letters are upper-cased (Unicode-aware); a name with no letters or digits gives
    "?". Tone: TONES[int(sha256(name.casefold()) hex, 16) % len(TONES)], so the same name always
    gets the same colour."""
    cores = [core for core in (_word_core(word) for word in name.split()) if core]
    kept = [
        core
        for core in cores
        if core.rstrip(_TRAILING_PUNCTUATION).casefold() not in _IGNORED_WORDS
    ]
    chosen = (kept or cores)[:2]
    initials = "".join(_initial(core) for core in chosen) or "?"
    digest = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()
    return Monogram(initials=initials, tone=TONES[int(digest, 16) % len(TONES)])


# --- Files and the index -----------------------------------------------------------------------


def _folder(store: LocalStore) -> Path:
    return store.root / LOGOS_DIR


def _stem(account_id: str) -> str:
    """The part of a file name that stands for the account: a hash, so the id is never a path."""
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]


def _file_name(account_id: str, kind: ImageType) -> str:
    return _stem(account_id) + _EXTENSIONS[kind]


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _load_index(store: LocalStore) -> dict[str, LogoInfo]:
    """The index, or {} when it is missing or unreadable. An entry that doesn't validate is left
    out; the rest still count."""
    try:
        raw = json.loads((_folder(store) / INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    index: dict[str, LogoInfo] = {}
    for account_id, value in raw.items():
        try:
            info = LogoInfo.model_validate(value)
        except ValueError:
            continue
        if info.account_id == account_id:
            index[account_id] = info
    return index


def _save_index(store: LocalStore, index: dict[str, LogoInfo]) -> None:
    payload = {
        account_id: info.model_dump(mode="json") for account_id, info in sorted(index.items())
    }
    write_atomic(
        _folder(store) / INDEX_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


def _write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with `data` so a crash mid-write never leaves a half-written image. The
    same steps as config.write_atomic, which writes text only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_file(folder: Path, info: LogoInfo) -> bytes | None:
    try:
        return (folder / _file_name(info.account_id, info.content_type)).read_bytes()
    except OSError:
        return None


def _remove_files(folder: Path, account_id: str, keep: str | None = None) -> None:
    """Delete the account's image files, whatever their type, except the file named `keep`."""
    for extension in _EXTENSIONS.values():
        name = _stem(account_id) + extension
        if name != keep:
            with contextlib.suppress(OSError):
                (folder / name).unlink(missing_ok=True)


# --- Reading and saving ------------------------------------------------------------------------


def get_info(store: LocalStore, account_id: str) -> LogoInfo | None:
    """The saved logo's info, or None."""
    info = _load_index(store).get(account_id)
    if info is None or not (_folder(store) / _file_name(account_id, info.content_type)).is_file():
        return None
    return info


def all_info(store: LocalStore) -> dict[str, LogoInfo]:
    """Every saved logo, keyed by account id (one read of the index, for list pages)."""
    folder = _folder(store)
    return {
        account_id: info
        for account_id, info in _load_index(store).items()
        if (folder / _file_name(account_id, info.content_type)).is_file()
    }


def read_logo(store: LocalStore, account_id: str) -> tuple[bytes, ImageType] | None:
    """The image bytes and type, or None when there is none or the file is missing."""
    info = _load_index(store).get(account_id)
    if info is None:
        return None
    data = _read_file(_folder(store), info)
    if data is None or len(data) > MAX_LOGO_BYTES or sniff(data) is not info.content_type:
        return None
    return data, info.content_type


def save_logo(
    store: LocalStore,
    account_id: str,
    data: bytes,
    source: LogoSource,
    *,
    now: datetime,
    origin_url: str | None = None,
) -> LogoInfo:
    """check_image, then write the file and the index atomically under store.WRITE_LOCK,
    replacing any earlier logo for the account (its old file removed)."""
    kind = check_image(data)
    name = _file_name(account_id, kind)
    info = LogoInfo(
        account_id=account_id,
        file=name,
        content_type=kind,
        source=source,
        saved_at=_naive_utc(now),
        origin_url=origin_url,
    )
    folder = _folder(store)
    with WRITE_LOCK:
        _write_bytes(folder / name, data)
        index = _load_index(store)
        index[account_id] = info
        _save_index(store, index)
        _remove_files(folder, account_id, keep=name)
    return info


def remove_logo(store: LocalStore, account_id: str) -> bool:
    """True if one was removed (file and index entry)."""
    folder = _folder(store)
    with WRITE_LOCK:
        index = _load_index(store)
        removed = index.pop(account_id, None) is not None
        if removed:
            _save_index(store, index)
        _remove_files(folder, account_id)
    return removed


def move_logo(store: LocalStore, from_id: str, to_id: str) -> None:
    """Linking: to_id keeps its own logo if it has one, else takes from_id's; from_id's entry is
    then removed either way."""
    if from_id == to_id:
        return
    folder = _folder(store)
    with WRITE_LOCK:
        index = _load_index(store)
        source = index.pop(from_id, None)
        if source is None:
            return
        if to_id not in index:
            data = _read_file(folder, source)
            if data is not None:
                name = _file_name(to_id, source.content_type)
                _write_bytes(folder / name, data)
                index[to_id] = source.model_copy(update={"account_id": to_id, "file": name})
        _save_index(store, index)
        _remove_files(folder, from_id)


def forget(store: LocalStore, gone: Callable[[str], bool]) -> int:
    """Remove the logo of every account id for which gone(id) is True; return how many."""
    folder = _folder(store)
    with WRITE_LOCK:
        index = _load_index(store)
        gone_ids = [account_id for account_id in index if gone(account_id)]
        if not gone_ids:
            return 0
        for account_id in gone_ids:
            del index[account_id]
        _save_index(store, index)
        for account_id in gone_ids:
            _remove_files(folder, account_id)
    return len(gone_ids)


# --- Finding a logo on the customer's website --------------------------------------------------


@dataclass(frozen=True)
class _Icon:
    group: int  # 0 for apple-touch-icon(-precomposed), 1 for icon and shortcut icon
    area: int  # the largest WxH in `sizes`; 0 when none is given
    order: int  # document order, the last tie-break
    href: str
    kind: str  # the type attribute, lowercased, without parameters


class _IconParser(HTMLParser):
    """Collects the icon <link> tags of a home page. Nothing is run or fetched."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.icons: list[_Icon] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = {name: value or "" for name, value in attrs}
        rel = set(values.get("rel", "").lower().split())
        if rel & _APPLE_RELS:
            group = 0
        elif "icon" in rel:
            group = 1
        else:
            return
        href = values.get("href", "").strip()
        if not href:
            return
        self.icons.append(
            _Icon(
                group=group,
                area=_largest_area(values.get("sizes", "")),
                order=len(self.icons),
                href=href,
                kind=values.get("type", "").split(";")[0].strip().lower(),
            )
        )


def _largest_area(sizes: str) -> int:
    """The largest area of the WxH entries in a sizes attribute ("32x32 64x64" gives 4096)."""
    return max((int(w) * int(h) for w, h in _SIZE.findall(sizes.lower())), default=0)


def _page_icons(body: bytes) -> list[_Icon]:
    """The icon links of a home page, read the way research.parse_page reads it: at most
    MAX_PARSE_BYTES, fed PARSE_CHUNK at a time, and stopped at PARSE_SECONDS."""
    parser = _IconParser()
    text = body[: research.MAX_PARSE_BYTES].decode("utf-8", errors="replace")
    deadline = time.monotonic() + research.PARSE_SECONDS
    try:
        for start in range(0, len(text), research.PARSE_CHUNK):
            if time.monotonic() >= deadline:
                break
            parser.feed(text[start : start + research.PARSE_CHUNK])
        if len(parser.rawdata) <= _CLOSE_LIMIT:
            parser.close()
    except (AssertionError, ValueError):  # html.parser gives up on some broken markup
        pass
    return parser.icons


def _on_site(url: str, domain: str) -> bool:
    """True when the URL's host is the domain or a subdomain of it (www. included)."""
    host = urlsplit(url).hostname or ""
    return host == domain or host.endswith("." + domain)


def _icon_urls(icons: list[_Icon], page_url: str, domain: str) -> list[str]:
    """The icon URLs to try, best first: apple-touch-icon links, then icon links, larger sizes
    first. Each is resolved against the page's final URL; only https, same-site, non-SVG ones are
    kept, deduplicated."""
    urls: list[str] = []
    for icon in sorted(icons, key=lambda icon: (icon.group, -icon.area, icon.order)):
        try:
            url = urljoin(page_url, icon.href)
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme != "https" or not _on_site(url, domain):
            continue
        if "svg" in icon.kind or parts.path.lower().endswith(".svg"):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def find_on_website(client: SiteClient, domain: str) -> tuple[bytes, ImageType, str]:
    """Fetch https://<domain>/ (domain lower-cased, without "www." or a scheme), read its
    <link rel> icons (apple-touch-icon and apple-touch-icon-precomposed first, then icon / shortcut
    icon, larger `sizes` first, SVG ones skipped), and try each (resolved against the final page
    URL, https only, same site: the host equals the domain or ends with "." + domain, either with
    or without "www.") until one passes check_image; then /apple-touch-icon.png and /favicon.ico
    on the domain. Returns (bytes, type, url). LogoError("Couldn't find a logo on <domain>.") when
    none works. research errors (SiteUnreachable, BlockedWebsite) propagate. Uses at most the
    client's request budget."""
    host = website_host(domain)
    home = client.fetch(f"https://{host}/")
    icons = _page_icons(home.body) if home.content_type in _HTML_TYPES else []
    candidates = _icon_urls(icons, home.url, host)[:_MAX_LINK_ICONS]
    tried: set[str] = set()
    for url in [*candidates, *(f"https://{host}{path}" for path in _FALLBACK_PATHS)]:
        if url in tried:
            continue
        tried.add(url)
        try:
            fetched = client.fetch(url)
        except SiteUnreachable:  # a missing or broken icon: try the next one
            continue
        if not _on_site(fetched.url, host):
            continue
        try:
            kind = check_image(fetched.body)
        except LogoError:
            continue
        return fetched.body, kind, fetched.url
    raise LogoError(f"Couldn't find a logo on {host}.")
