"""Your own logo in place of Wardmind's: core/brand.py and the Appearance routes."""

from __future__ import annotations

import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from wardmind.core import brand, logos
from wardmind.core.config import config_dir
from wardmind.web import app as web


def png(width: int = 32, height: int = 32) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (3 * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture
def client() -> TestClient:
    return TestClient(web.create_app(), base_url="http://127.0.0.1")


def test_no_logo_means_wardmind_s_own_mark() -> None:
    assert brand.current() is None
    assert brand.read() is None


def test_a_saved_logo_is_read_back_and_replaces_another_type() -> None:
    brand.save(png())
    saved = brand.read()
    assert saved is not None and saved[1] is logos.ImageType.PNG
    assert brand.current() is not None
    gif = b"GIF89a" + struct.pack("<HH", 32, 32) + b"\x00" * 20
    brand.save(gif)
    files = sorted(path.name for path in config_dir().glob("brand-logo*"))
    assert files == ["brand-logo.gif"]  # the PNG went, and no temporary file was left


@pytest.mark.parametrize("data", [b"", SVG, b"not an image", png(4, 4)])
def test_what_is_not_a_usable_image_is_refused_and_nothing_is_kept(data: bytes) -> None:
    with pytest.raises(logos.LogoError):
        brand.save(data)
    assert list(config_dir().glob("brand-logo*")) == []


def test_removing_brings_the_mark_back() -> None:
    brand.save(png())
    brand.remove()
    assert brand.current() is None


def test_uploading_shows_it_in_the_top_bar_instead_of_wardmind(client: TestClient) -> None:
    page = client.get("/").text
    assert 'class="brand-mark"' in page and "/brand-logo" not in page
    response = client.post(
        "/settings/appearance/logo",
        files={"logo": ("logo.png", png(), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/appearance?logo=saved#logo"
    page = client.get("/").text
    assert '<img class="brand-custom" src="/brand-logo?v=' in page
    assert 'class="brand-mark"' not in page
    assert "Your logo is in the top bar now." in client.get("/settings/appearance?logo=saved").text


def test_the_logo_is_served_with_its_checked_type_and_a_locked_down_policy(
    client: TestClient,
) -> None:
    assert client.get("/brand-logo").status_code == 404
    brand.save(png())
    response = client.get("/brand-logo")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.content == png()


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"logo": ("logo.svg", SVG, "image/svg+xml")}, "refused"),
        (
            {"logo": ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * logos.MAX_LOGO_BYTES, "image/png")},
            "too-big",
        ),
        ({"logo": ("", b"", "application/octet-stream")}, "no-file"),
    ],
)
def test_a_refused_upload_says_why_and_keeps_wardmind_s_mark(
    client: TestClient, files: dict[str, tuple[str, bytes, str]], code: str
) -> None:
    response = client.post("/settings/appearance/logo", files=files, follow_redirects=False)
    assert response.headers["location"] == f"/settings/appearance?logo={code}#logo"
    assert brand.current() is None


def test_removing_from_appearance_brings_the_mark_back(client: TestClient) -> None:
    brand.save(png())
    response = client.post("/settings/appearance/logo/remove", follow_redirects=False)
    assert response.headers["location"] == "/settings/appearance?logo=removed#logo"
    assert brand.current() is None
    assert 'class="brand-mark"' in client.get("/").text


def test_a_cross_site_upload_is_refused(client: TestClient) -> None:
    response = client.post(
        "/settings/appearance/logo",
        files={"logo": ("logo.png", png(), "image/png")},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert brand.current() is None
