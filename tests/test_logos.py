"""Account logos in the core: image checks, monograms, the saved files and index, finding a logo on
a customer's website, and the service functions. Sites are fake (research_fake.py); the autouse
fixture in conftest fails any test that reaches for a real connection."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from research_fake import NOW, FakeSite, Route, raising, reply
from wardmind.core import editing, logos, research, service
from wardmind.core.logos import ImageType, LogoError, LogoSource, check_image, sniff
from wardmind.core.settings import DataSource, load_settings, save_settings
from wardmind.core.store import LOGOS_DIR, LocalStore

TODAY = date(2026, 10, 22)
MAX = logos.MAX_LOGO_BYTES


# --- Image builders (real headers, so the size readers have something to read) ----------------


def png(width: int = 32, height: int = 32) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (3 * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def jpeg(width: int, height: int) -> bytes:
    """An SOI, a JFIF APP0 segment, then a baseline SOF0 frame header of this size."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03"
        + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


def image(data: bytes) -> Route:
    return reply(data, content_type="image/png")


def page(*links: str) -> bytes:
    body = "".join(links)
    return (
        f"<!doctype html><html><head><title>Acme</title>{body}</head><body></body></html>".encode()
    )


def acme(*links: str, routes: dict[str, Route] | None = None) -> FakeSite:
    """A site whose home page has these head links, plus any extra routes (full URLs)."""
    table: dict[str, Route] = {"https://acme.example/": reply(page(*links))}
    table.update(routes or {})
    return FakeSite(table)


def _files(store: LocalStore) -> list[str]:
    folder = store.root / LOGOS_DIR
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def _stem(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]


def _index_path(store: LocalStore) -> Path:
    return store.root / LOGOS_DIR / logos.INDEX_FILE


# --- Sniffing and checking images --------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (png(), ImageType.PNG),
        (jpeg(16, 16), ImageType.JPEG),
        (gif(16, 16), ImageType.GIF),
        (b"GIF87a" + b"\x00" * 10, ImageType.GIF),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8, ImageType.WEBP),
        (b"\x00\x00\x01\x00\x01\x00" + b"\x00" * 10, ImageType.ICO),
    ],
)
def test_sniff_reads_the_type_from_the_first_bytes(data: bytes, kind: ImageType) -> None:
    assert sniff(data) is kind


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"<!doctype html><html><body>hi</body></html>",
        b"hello, not an image",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"\x89PNG",  # the signature is eight bytes; four are not a PNG
        b"GIF9",
    ],
)
def test_sniff_gives_none_for_anything_else(data: bytes) -> None:
    assert sniff(data) is None


def test_check_image_returns_the_sniffed_type() -> None:
    assert check_image(png(32, 32)) is ImageType.PNG
    assert check_image(jpeg(64, 48)) is ImageType.JPEG
    assert check_image(gif(20, 20)) is ImageType.GIF


def test_check_image_refuses_an_empty_file() -> None:
    with pytest.raises(LogoError) as error:
        check_image(b"")
    assert error.value.message == "That file is empty."


def test_check_image_refuses_a_file_over_the_limit_and_accepts_exactly_the_limit() -> None:
    padded = png(16, 16)
    with pytest.raises(LogoError) as error:
        check_image(padded + bytes(MAX - len(padded) + 1))
    assert error.value.message == "That image is larger than 200 KB. Try a smaller one."
    assert check_image(padded + bytes(MAX - len(padded))) is ImageType.PNG


@pytest.mark.parametrize(
    "data",
    [
        b"hello, not an image",
        b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"></svg>',
        b"<html></html>",
    ],
)
def test_check_image_refuses_what_is_not_a_raster_image(data: bytes) -> None:
    with pytest.raises(LogoError) as error:
        check_image(data)
    assert error.value.message == "That isn't a PNG, JPEG, GIF, WebP or ICO image."


@pytest.mark.parametrize(
    ("data", "width", "height"),
    [
        (png(15, 40), 15, 40),
        (png(40, 8), 40, 8),
        (gif(15, 40), 15, 40),
        (gif(40, 8), 40, 8),
        (jpeg(8, 100), 8, 100),
        (jpeg(100, 15), 100, 15),
        (png(0, 0), 0, 0),
    ],
)
def test_check_image_refuses_a_side_under_the_minimum(data: bytes, width: int, height: int) -> None:
    with pytest.raises(LogoError) as error:
        check_image(data)
    assert error.value.message == "That image is too small to use."


@pytest.mark.parametrize("data", [png(16, 16), png(16, 900), gif(16, 16), jpeg(16, 16)])
def test_check_image_accepts_a_side_at_the_minimum(data: bytes) -> None:
    check_image(data)


def test_check_image_accepts_icons_and_webp_whose_size_is_not_read() -> None:
    assert check_image(b"\x00\x00\x01\x00\x01\x00" + b"\x00" * 10) is ImageType.ICO
    assert check_image(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8) is ImageType.WEBP


def test_a_jpeg_whose_frame_header_cannot_be_found_is_accepted() -> None:
    # A start of scan before any frame header: the size is unreadable, so the image is accepted.
    data = b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xda\x00\x02" + b"\x00" * 20
    assert check_image(data) is ImageType.JPEG


def test_a_truncated_png_header_is_accepted_without_a_size() -> None:
    assert check_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 6) is ImageType.PNG


# --- Monograms ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "initials"),
    [
        ("Acme Corporation", "AC"),
        ("The Acme Group", "AG"),  # "The" is skipped
        ("Acme Inc.", "A"),  # the suffix is skipped, leaving one word
        ("Acme Corp", "A"),
        ("Acme Ltd", "A"),
        ("Acme & Sons", "AS"),  # "&" is skipped
        ("Johnson & Johnson", "JJ"),
        ("Acme, Inc. of Leeds", "AL"),  # "of" is skipped, and so is "Inc."
        ("the and of", "TA"),  # nothing else is left, so the stopwords count
        ("Zeta", "Z"),  # one word: its first letter only
        ("zeta-labs", "Z"),
        ("(Acme) Labs", "AL"),  # leading punctuation is not a letter
        ("3M Company", "3C"),  # a digit starts a word
        ("42", "4"),
        ("Émile Zola", "ÉZ"),  # Unicode letters, upper-cased
        ("Ünïcode Ångström", "ÜÅ"),
        ("acme corporation", "AC"),
    ],
)
def test_monogram_initials(name: str, initials: str) -> None:
    assert logos.monogram(name).initials == initials


@pytest.mark.parametrize("name", ["", "   ", "&", "!!! ???", "—"])
def test_a_name_with_no_letters_or_digits_gives_a_question_mark(name: str) -> None:
    assert logos.monogram(name).initials == "?"


def test_tone_is_one_of_the_tones_and_the_same_for_the_same_name() -> None:
    for name in ["Acme", "Beta Logistics", "Zeta", "Ünïcode", "42"]:
        tone = logos.monogram(name).tone
        assert tone in logos.TONES
        assert logos.monogram(name).tone == tone


def test_tone_comes_from_the_casefolded_name_hash() -> None:
    digest = hashlib.sha256(b"acme corporation").hexdigest()
    expected = logos.TONES[int(digest, 16) % len(logos.TONES)]
    assert logos.monogram("Acme Corporation").tone == expected
    assert logos.monogram("ACME CORPORATION").tone == expected


# --- Saving, reading, replacing and removing -----------------------------------------------------


def test_save_then_get_info_and_read_back(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    data = png(48, 48)
    info = logos.save_logo(
        store,
        "hs-1",
        data,
        LogoSource.WEBSITE,
        now=datetime(2026, 10, 22, 9, 30),
        origin_url="https://acme.example/apple-touch-icon.png",
    )
    assert info.file == _stem("hs-1") + ".png"
    assert info.content_type is ImageType.PNG
    assert info.source is LogoSource.WEBSITE
    assert info.origin_url == "https://acme.example/apple-touch-icon.png"
    assert logos.get_info(store, "hs-1") == info
    assert logos.read_logo(store, "hs-1") == (data, ImageType.PNG)
    assert _files(store) == [info.file, logos.INDEX_FILE]


def test_saved_at_is_naive_utc(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    info = logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    assert info.saved_at == datetime(2026, 10, 22, 9, 0)
    assert info.saved_at.tzinfo is None


def test_a_replacement_of_the_same_type_overwrites_the_file(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "hs-1", png(16, 16), LogoSource.UPLOAD, now=NOW)
    second = png(64, 64)
    logos.save_logo(store, "hs-1", second, LogoSource.UPLOAD, now=NOW)
    assert logos.read_logo(store, "hs-1") == (second, ImageType.PNG)
    assert len(_files(store)) == 2  # the image and the index


def test_a_replacement_of_another_type_removes_the_old_file(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    jpg = jpeg(32, 32)
    info = logos.save_logo(store, "hs-1", jpg, LogoSource.UPLOAD, now=NOW)
    assert info.file == _stem("hs-1") + ".jpg"
    assert _files(store) == [info.file, logos.INDEX_FILE]
    assert logos.read_logo(store, "hs-1") == (jpg, ImageType.JPEG)


def test_a_refused_image_saves_nothing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    with pytest.raises(LogoError):
        logos.save_logo(
            store, "hs-1", b'<svg xmlns="http://www.w3.org/2000/svg"/>', LogoSource.UPLOAD, now=NOW
        )
    assert not (tmp_path / LOGOS_DIR).exists()
    assert logos.get_info(store, "hs-1") is None


def test_read_is_none_without_a_logo_or_with_a_missing_file(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert logos.read_logo(store, "hs-1") is None
    assert logos.get_info(store, "hs-1") is None
    logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    (tmp_path / LOGOS_DIR / (_stem("hs-1") + ".png")).unlink()
    assert logos.read_logo(store, "hs-1") is None
    assert logos.get_info(store, "hs-1") is None
    assert logos.all_info(store) == {}


def test_read_refuses_a_file_that_is_no_longer_the_type_it_was_checked_as(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    (tmp_path / LOGOS_DIR / (_stem("hs-1") + ".png")).write_bytes(b"<svg></svg>")
    assert logos.read_logo(store, "hs-1") is None


def test_all_info_lists_every_saved_logo(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    first = logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    second = logos.save_logo(store, "sp1-x", gif(20, 20), LogoSource.WEBSITE, now=NOW)
    assert logos.all_info(store) == {"hs-1": first, "sp1-x": second}


def test_remove_deletes_the_file_and_the_entry(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    assert logos.remove_logo(store, "hs-1") is True
    assert logos.get_info(store, "hs-1") is None
    assert _files(store) == [logos.INDEX_FILE]
    assert logos.remove_logo(store, "hs-1") is False
    assert logos.remove_logo(store, "never-had-one") is False


def test_file_names_never_contain_the_account_id(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    awkward = "hs-42/../../escape ünï?*"
    info = logos.save_logo(store, awkward, png(), LogoSource.UPLOAD, now=NOW)
    assert info.file == _stem(awkward) + ".png"
    for name in _files(store):
        assert awkward not in name
        assert "/" not in name
    assert sorted(path.name for path in tmp_path.iterdir()) == [LOGOS_DIR]
    assert logos.read_logo(store, awkward) is not None


# --- Linking and forgetting ----------------------------------------------------------------------


def test_move_takes_the_logo_when_the_target_has_none(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    data = png(40, 40)
    logos.save_logo(
        store, "m-old", data, LogoSource.WEBSITE, now=NOW, origin_url="https://acme.example/"
    )
    logos.move_logo(store, "m-old", "hs-new")
    assert logos.get_info(store, "m-old") is None
    moved = logos.get_info(store, "hs-new")
    assert moved is not None
    assert moved.source is LogoSource.WEBSITE
    assert moved.origin_url == "https://acme.example/"
    assert moved.file == _stem("hs-new") + ".png"
    assert logos.read_logo(store, "hs-new") == (data, ImageType.PNG)
    assert _files(store) == [moved.file, logos.INDEX_FILE]


def test_move_keeps_the_target_logo_and_drops_the_source(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    kept = png(40, 40)
    logos.save_logo(store, "hs-new", kept, LogoSource.UPLOAD, now=NOW)
    logos.save_logo(store, "m-old", png(24, 24), LogoSource.UPLOAD, now=NOW)
    logos.move_logo(store, "m-old", "hs-new")
    assert logos.get_info(store, "m-old") is None
    assert logos.read_logo(store, "hs-new") == (kept, ImageType.PNG)
    assert _files(store) == [_stem("hs-new") + ".png", logos.INDEX_FILE]


def test_move_with_nothing_on_the_source_changes_nothing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "hs-new", png(), LogoSource.UPLOAD, now=NOW)
    before = _files(store)
    logos.move_logo(store, "m-none", "hs-new")
    logos.move_logo(store, "hs-new", "hs-new")
    assert _files(store) == before
    assert logos.get_info(store, "hs-new") is not None


def test_forget_removes_the_logos_of_the_accounts_that_are_gone(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "acc-a", png(), LogoSource.UPLOAD, now=NOW)
    logos.save_logo(store, "acc-b", jpeg(20, 20), LogoSource.WEBSITE, now=NOW)
    assert logos.forget(store, lambda account_id: account_id == "acc-a") == 1
    assert logos.get_info(store, "acc-a") is None
    assert logos.get_info(store, "acc-b") is not None
    assert logos.forget(store, lambda account_id: False) == 0
    assert logos.forget(store, lambda account_id: True) == 1
    assert logos.all_info(store) == {}


def test_forget_on_an_empty_store_creates_nothing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert logos.forget(store, lambda account_id: True) == 0
    assert not (tmp_path / LOGOS_DIR).exists()


# --- The index --------------------------------------------------------------------------------


def test_an_unreadable_index_reads_as_empty_and_is_rebuilt_by_the_next_save(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    _index_path(store).write_text("{not json", encoding="utf-8")
    assert logos.get_info(store, "hs-1") is None
    assert logos.all_info(store) == {}
    assert logos.read_logo(store, "hs-1") is None
    assert logos.forget(store, lambda account_id: True) == 0
    assert logos.remove_logo(store, "hs-1") is False
    info = logos.save_logo(store, "hs-2", png(), LogoSource.UPLOAD, now=NOW)
    assert json.loads(_index_path(store).read_text(encoding="utf-8")).keys() == {"hs-2"}
    assert logos.get_info(store, "hs-2") == info


@pytest.mark.parametrize("content", ["[]", '"text"', "null", ""])
def test_an_index_that_is_not_an_object_reads_as_empty(tmp_path: Path, content: str) -> None:
    store = LocalStore(tmp_path)
    (tmp_path / LOGOS_DIR).mkdir()
    _index_path(store).write_text(content, encoding="utf-8")
    assert logos.all_info(store) == {}


def test_an_entry_that_does_not_validate_is_skipped_and_the_rest_count(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    good = logos.save_logo(store, "hs-1", png(), LogoSource.UPLOAD, now=NOW)
    raw = json.loads(_index_path(store).read_text(encoding="utf-8"))
    raw["hs-bad"] = {"account_id": "hs-bad", "content_type": "text/html"}
    raw["hs-mismatch"] = good.model_dump(mode="json") | {"account_id": "someone-else"}
    _index_path(store).write_text(json.dumps(raw), encoding="utf-8")
    assert logos.all_info(store) == {"hs-1": good}


# --- Finding a logo on the website -------------------------------------------------------------


def test_the_apple_touch_icon_is_preferred_to_an_icon_listed_first() -> None:
    site = acme(
        '<link rel="icon" sizes="192x192" href="/icon.png">',
        '<link rel="apple-touch-icon" href="/apple.png">',
        routes={
            "https://acme.example/icon.png": image(png(192, 192)),
            "https://acme.example/apple.png": image(png(180, 180)),
        },
    )
    data, kind, url = logos.find_on_website(site.client(), "acme.example")
    assert (data, kind, url) == (png(180, 180), ImageType.PNG, "https://acme.example/apple.png")
    assert site.seen == ["https://acme.example/", "https://acme.example/apple.png"]


def test_apple_touch_icon_precomposed_counts_as_an_apple_icon() -> None:
    site = acme(
        '<link rel="icon" href="/icon.png">',
        '<link rel="apple-touch-icon-precomposed" href="/pre.png">',
        routes={
            "https://acme.example/icon.png": image(png()),
            "https://acme.example/pre.png": image(png(64, 64)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/pre.png"


def test_larger_sizes_are_tried_first_within_the_icons() -> None:
    site = acme(
        '<link rel="icon" sizes="32x32" href="/small.png">',
        '<link rel="icon" sizes="192x192" href="/big.png">',
        '<link rel="icon" href="/unsized.png">',
        routes={
            "https://acme.example/small.png": image(png(32, 32)),
            "https://acme.example/big.png": image(png(192, 192)),
            "https://acme.example/unsized.png": image(png(64, 64)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/big.png"
    assert site.seen[1] == "https://acme.example/big.png"


def test_rel_is_case_insensitive_and_a_shortcut_icon_counts() -> None:
    site = acme(
        '<link rel="Shortcut Icon" href="/s.png">',
        routes={"https://acme.example/s.png": image(png(32, 32))},
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/s.png"


def test_svg_icons_are_skipped_whatever_they_are_called() -> None:
    site = acme(
        '<link rel="icon" type="image/svg+xml" href="/icon.svg">',
        '<link rel="icon" href="/logo.SVG">',
        routes={
            "https://acme.example/icon.svg": reply(b"<svg/>", content_type="image/svg+xml"),
            "https://acme.example/logo.SVG": reply(b"<svg/>", content_type="image/svg+xml"),
            "https://acme.example/apple-touch-icon.png": image(png()),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/apple-touch-icon.png"
    assert not any(seen.endswith(".svg") or seen.endswith(".SVG") for seen in site.seen)


def test_an_off_site_icon_is_never_fetched() -> None:
    site = acme(
        '<link rel="icon" href="https://cdn.other.example/icon.png">',
        routes={
            "https://cdn.other.example/icon.png": image(png()),
            "https://acme.example/apple-touch-icon.png": image(png()),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/apple-touch-icon.png"
    assert not any("other.example" in seen for seen in site.seen)


def test_an_icon_redirected_off_site_is_refused() -> None:
    site = acme(
        '<link rel="icon" href="/old.png">',
        routes={
            "https://acme.example/old.png": reply(
                b"", status=302, headers={"location": "https://cdn.other.example/x.png"}
            ),
            "https://cdn.other.example/x.png": image(png()),
            "https://acme.example/favicon.ico": image(png(32, 32)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/favicon.ico"


def test_an_http_icon_is_never_fetched() -> None:
    site = acme(
        '<link rel="icon" href="http://acme.example/icon.png">',
        routes={"https://acme.example/favicon.ico": image(png(32, 32))},
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/favicon.ico"
    assert not any(seen.startswith("http://") for seen in site.seen)


def test_a_subdomain_icon_on_the_same_site_is_used() -> None:
    site = acme(
        '<link rel="icon" href="https://www.acme.example/i.png">',
        routes={"https://www.acme.example/i.png": image(png())},
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://www.acme.example/i.png"


def test_a_redirected_icon_is_saved_under_its_final_address() -> None:
    site = acme(
        '<link rel="apple-touch-icon" href="/old.png">',
        routes={
            "https://acme.example/old.png": reply(
                b"", status=301, headers={"location": "/new.png"}
            ),
            "https://acme.example/new.png": image(png(180, 180)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/new.png"


def test_the_fallbacks_are_used_when_the_page_has_no_icons() -> None:
    site = acme(routes={"https://acme.example/apple-touch-icon.png": image(png(180, 180))})
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/apple-touch-icon.png"


def test_favicon_ico_is_the_last_fallback() -> None:
    site = acme(routes={"https://acme.example/favicon.ico": image(png(32, 32))})
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/favicon.ico"
    assert site.seen == [
        "https://acme.example/",
        "https://acme.example/apple-touch-icon.png",
        "https://acme.example/favicon.ico",
    ]


def test_an_icon_that_is_not_an_image_or_is_too_small_is_skipped() -> None:
    site = acme(
        '<link rel="apple-touch-icon" href="/text.png">',
        '<link rel="icon" href="/tiny.png">',
        routes={
            "https://acme.example/text.png": reply(b"<html>not an image</html>"),
            "https://acme.example/tiny.png": image(png(8, 8)),
            "https://acme.example/favicon.ico": image(png(32, 32)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/favicon.ico"


def test_an_icon_that_cannot_be_reached_is_skipped() -> None:
    site = acme(
        '<link rel="apple-touch-icon" href="/broken.png">',
        routes={
            "https://acme.example/broken.png": raising(httpx.ConnectError("connection refused")),
            "https://acme.example/favicon.ico": image(png(32, 32)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/favicon.ico"


def test_no_logo_at_all_is_a_logo_error_naming_the_site() -> None:
    site = acme()
    with pytest.raises(LogoError) as error:
        logos.find_on_website(site.client(), "acme.example")
    assert error.value.message == "Couldn't find a logo on acme.example."


def test_each_address_is_asked_for_once_and_the_request_budget_is_kept() -> None:
    links = [f'<link rel="icon" href="/i{number}.png">' for number in range(10)]
    site = acme(
        *links,
        routes={
            "https://acme.example/apple-touch-icon.png": reply(b"", status=404),
            "https://acme.example/favicon.ico": image(png(32, 32)),
        },
    )
    _, _, url = logos.find_on_website(site.client(), "acme.example")
    assert url == "https://acme.example/favicon.ico"
    assert len(site.seen) <= research.MAX_REQUESTS
    assert len(set(site.seen)) == len(site.seen)
    assert "https://acme.example/i6.png" not in site.seen  # past the link limit


def test_an_unreadable_home_page_is_a_site_error_that_propagates() -> None:
    site = FakeSite({"https://acme.example/": reply(b"", status=500)})
    with pytest.raises(research.SiteUnreachable):
        logos.find_on_website(site.client(), "acme.example")


def test_a_blocked_home_page_propagates() -> None:
    blocked = research.BlockedWebsite("That website points at this computer.")
    site = FakeSite({"https://acme.example/": raising(blocked)})
    with pytest.raises(research.BlockedWebsite):
        logos.find_on_website(site.client(), "acme.example")


def test_a_blocked_icon_propagates_and_nothing_else_is_tried() -> None:
    blocked = research.BlockedWebsite("That website points at this computer.")
    site = acme(
        '<link rel="apple-touch-icon" href="/apple.png">',
        routes={
            "https://acme.example/apple.png": raising(blocked),
            "https://acme.example/favicon.ico": image(png(32, 32)),
        },
    )
    with pytest.raises(research.BlockedWebsite):
        logos.find_on_website(site.client(), "acme.example")
    assert "https://acme.example/favicon.ico" not in site.seen


def test_the_domain_is_normalised_before_the_visit() -> None:
    site = acme(routes={"https://acme.example/favicon.ico": image(png(32, 32))})
    logos.find_on_website(site.client(), "WWW.Acme.example")
    assert site.seen[0] == "https://acme.example/"


@pytest.mark.parametrize(
    ("domain", "error"),
    [
        ("192.168.1.10", research.BlockedWebsite),
        ("localhost", research.NoWebsite),  # no dot in it, so not a website
        ("intranet.corp", research.BlockedWebsite),
        ("", research.NoWebsite),
    ],
)
def test_a_domain_that_is_not_a_public_host_is_refused_before_any_request(
    domain: str, error: type[Exception]
) -> None:
    site = acme(routes={"https://acme.example/favicon.ico": image(png(32, 32))})
    with pytest.raises(error):
        logos.find_on_website(site.client(), domain)
    assert site.seen == []


# --- The service functions --------------------------------------------------------------------


def _your_account(domain: str | None = "acme.example") -> str:
    account = editing.create_account(
        service.get_store(),
        {
            "name": "Acme Corporation",
            "arr": "$120,000",
            "renewal_date": "2027-01-10",
            "domain": domain or "",
        },
        today=TODAY,
    )
    save_settings(load_settings().model_copy(update={"source": DataSource.MINE}))
    service.reset_connectors()
    return account.id


def _web_research(on: bool = True) -> None:
    save_settings(load_settings().model_copy(update={"web_research": on}))


def _site_factory(monkeypatch: pytest.MonkeyPatch, site: FakeSite) -> list[int]:
    calls: list[int] = []

    def factory() -> research.SiteClient:
        calls.append(1)
        return site.client()

    monkeypatch.setattr(service, "_site_client_factory", factory)
    return calls


def test_upload_logo_saves_it_as_an_upload_and_remove_logo_removes_it() -> None:
    account_id = _your_account()
    info = service.upload_logo(account_id, png(32, 32))
    assert info.source is LogoSource.UPLOAD
    assert service.logo_info(account_id) == info
    assert account_id in service.all_logos()
    assert service.remove_logo(account_id) is True
    assert service.logo_info(account_id) is None
    assert service.remove_logo(account_id) is False


def test_find_logo_with_web_research_off_is_refused_before_any_connection() -> None:
    account_id = _your_account()
    with pytest.raises(research.ResearchOff):
        service.find_logo(account_id)


def test_find_logo_for_an_unknown_account_is_a_lookup_error() -> None:
    _web_research()
    _your_account()
    with pytest.raises(LookupError):
        service.find_logo("m-doesnotexist")


def test_find_logo_is_refused_for_demo_accounts_without_a_connection() -> None:
    _web_research()
    save_settings(load_settings().model_copy(update={"source": DataSource.DEMO}))
    with pytest.raises(LogoError):
        service.find_logo("acc-001")


def test_find_logo_without_a_website_is_a_logo_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _web_research()
    account_id = _your_account(domain=None)
    calls = _site_factory(monkeypatch, acme())
    with pytest.raises(LogoError) as error:
        service.find_logo(account_id)
    assert error.value.message == service.LOGO_NO_WEBSITE_MESSAGE
    assert calls == []


def test_find_logo_never_replaces_an_uploaded_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    _web_research()
    account_id = _your_account()
    service.upload_logo(account_id, png(48, 48))
    calls = _site_factory(
        monkeypatch, acme(routes={"https://acme.example/favicon.ico": image(png())})
    )
    with pytest.raises(LogoError) as error:
        service.find_logo(account_id)
    assert error.value.message == service.LOGO_UPLOADED_MESSAGE
    assert calls == []
    assert service.logo_info(account_id).source is LogoSource.UPLOAD  # type: ignore[union-attr]


def test_find_logo_saves_the_logo_from_the_website(monkeypatch: pytest.MonkeyPatch) -> None:
    _web_research()
    account_id = _your_account()
    site = acme(
        '<link rel="apple-touch-icon" href="/apple.png">',
        routes={"https://acme.example/apple.png": image(png(180, 180))},
    )
    calls = _site_factory(monkeypatch, site)
    info = service.find_logo(account_id)
    assert calls == [1]
    assert info.source is LogoSource.WEBSITE
    assert info.origin_url == "https://acme.example/apple.png"
    assert service.logo_info(account_id) == info


def test_find_logo_says_when_no_logo_is_found_and_keeps_the_monogram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _web_research()
    account_id = _your_account()
    _site_factory(monkeypatch, acme())
    with pytest.raises(LogoError) as error:
        service.find_logo(account_id)
    assert error.value.message == "Couldn't find a logo on acme.example."
    assert service.logo_info(account_id) is None


def test_a_blocked_website_propagates_from_find_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    _web_research()
    account_id = _your_account(domain="192.168.4.20")
    calls = _site_factory(monkeypatch, acme())
    with pytest.raises(research.BlockedWebsite):
        service.find_logo(account_id)
    assert calls == []


def test_removing_a_connection_removes_the_logos_of_its_accounts() -> None:
    account_id = _your_account()
    service.upload_logo(account_id, png())
    assert service.logo_info(account_id) is not None
    service.remove_connection("manual")
    assert service.logo_info(account_id) is None
    assert service.all_logos() == {}


def test_delete_all_local_data_removes_every_logo() -> None:
    account_id = _your_account()
    service.upload_logo(account_id, png())
    service.delete_all_local_data()
    assert service.logo_info(account_id) is None
    assert service.all_logos() == {}
    assert not (service.get_store().root / LOGOS_DIR).exists()
