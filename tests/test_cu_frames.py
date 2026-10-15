from __future__ import annotations

import io
from pathlib import Path

import pytest

from hermes_loopguard.cu import frames

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu" / "vision"


def make_png(w: int, h: int, color=(40, 80, 160)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def gradient_png(w: int, h: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h))
    for x in range(w):
        v = int(255 * x / max(1, w - 1))
        for y in range(0, h, max(1, h // 64)):
            img.putpixel((x, y), (v, v, v))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- prepare_image bounds ---------------------------------------------------

def test_prepare_image_downscales_1920x1080_to_1568x882():
    out, w, h = frames.prepare_image(make_png(1920, 1080))
    assert (w, h) == (1568, 882)
    assert out.startswith(b"\x89PNG")  # PNG out
    from PIL import Image

    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1568, 882)


def test_prepare_image_already_in_bounds_is_unchanged():
    out, w, h = frames.prepare_image(make_png(1280, 800))
    assert (w, h) == (1280, 800)
    assert out.startswith(b"\x89PNG")


def test_prepare_image_tiny_image_rejected():
    with pytest.raises(ValueError, match="minimum image bounds"):
        frames.prepare_image(make_png(200, 100))


def test_prepare_image_one_small_side_rejected():
    # 2000x100: the long side scales into range but the short side cannot
    # reach the 256px minimum by downscaling.
    with pytest.raises(ValueError, match="minimum image bounds"):
        frames.prepare_image(make_png(2000, 100))


def test_prepare_image_max_pixels_bound():
    # 4000x4000 exceeds MAX_PIXELS after any side-based scale; still bounded.
    out, w, h = frames.prepare_image(make_png(4000, 4000))
    assert w <= frames.MAX_SIDE and h <= frames.MAX_SIDE
    assert w * h <= frames.MAX_PIXELS


def test_prepare_image_jpeg_in_png_out():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1024, 768), (10, 200, 10)).save(buf, format="JPEG")
    out, w, h = frames.prepare_image(buf.getvalue())
    assert (w, h) == (1024, 768)
    assert out.startswith(b"\x89PNG")


# --- crop ---------------------------------------------------------------------

def test_crop_clamps_to_image_bounds():
    png = make_png(640, 480)
    out = frames.crop(png, -50, -50, 2000, 2000)
    from PIL import Image

    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (640, 480)


def test_crop_inner_region():
    png = make_png(800, 600)
    out = frames.crop(png, 100, 50, 700, 500)
    from PIL import Image

    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (600, 450)


def test_crop_empty_raises():
    with pytest.raises(ValueError):
        frames.crop(make_png(640, 480), 300, 300, 300, 300)


# --- dhash / hamming ------------------------------------------------------------

def test_dhash_hamming_identical_zero():
    png = gradient_png(600, 400)
    assert frames.hamming(frames.dhash(png), frames.dhash(png)) == 0


def test_dhash_different_images_far_apart():
    # REAL recorded screenshots of structurally different pages.
    a = frames.dhash((FIXTURES / "form.png").read_bytes())
    b = frames.dhash((FIXTURES / "payment.png").read_bytes())
    assert frames.hamming(a, b) >= 12


def test_dhash_small_shift_below_unchanged_max():
    # REAL screenshot shifted by 2px stays within the "unchanged" band.
    from PIL import Image

    png = (FIXTURES / "form.png").read_bytes()
    img = Image.open(io.BytesIO(png))
    shifted = img.transform(img.size, Image.Transform.AFFINE, (1, 0, -2, 0, 1, 0))
    buf = io.BytesIO()
    shifted.convert("RGB").save(buf, format="PNG")
    assert frames.hamming(frames.dhash(png), frames.dhash(buf.getvalue())) <= 3


def test_hamming_known_values():
    assert frames.hamming(0, 0) == 0
    assert frames.hamming(0b1011, 0b0100) == 4
    assert frames.hamming(0, 1 << 63) == 1


# --- Pillow-missing error path (module stays importable without it) -------------

def test_missing_pillow_clear_error(monkeypatch):
    png = make_png(300, 300)  # built while PIL is still importable
    monkeypatch.setitem(__import__("sys").modules, "PIL", None)
    with pytest.raises(RuntimeError, match="hermes-loopguard\\[cu\\]"):
        frames.dhash(png)
