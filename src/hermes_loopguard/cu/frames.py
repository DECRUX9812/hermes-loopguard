"""Image preparation and perceptual hashing for the vision gates.

Pillow is imported lazily inside each function so that importing this
module (or the cu package) never requires it. Install the extra with
`pip install hermes-loopguard[cu]` to use these helpers.
"""
from __future__ import annotations

from io import BytesIO

# TypeSafe image limits (docs.typesafe.ai/go/images). Out-of-range images are
# rejected, not resized, so every screenshot is prepared to fit.
MAX_SIDE = 1568
MIN_SIDE = 256
MIN_PIXELS = 78_400
MAX_PIXELS = 2_458_624


def _pil_image():
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - depends on env
        raise RuntimeError(
            "Pillow is required for computer-use image handling; "
            "install it with: pip install hermes-loopguard[cu]"
        ) from e
    return Image


def _load(png: bytes):
    Image = _pil_image()
    img = Image.open(BytesIO(png))
    img.load()
    return img


def _save_png(img) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def prepare_image(png: bytes) -> tuple[bytes, int, int]:
    """Downscale `png` so it satisfies the TypeSafe image limits.

    Returns (png_bytes, width, height) of the prepared image. Raises
    ValueError when the limits cannot be satisfied by downscaling (the
    image is already below the minimum side/pixel bounds).
    """
    img = _load(png)
    width, height = img.size
    scale = min(
        1.0,
        MAX_SIDE / max(width, height),
        (MAX_PIXELS / (width * height)) ** 0.5,
    )
    new_w = round(width * scale)
    new_h = round(height * scale)
    if new_w < MIN_SIDE or new_h < MIN_SIDE or new_w * new_h < MIN_PIXELS:
        raise ValueError(
            f"image {width}x{height} cannot meet the minimum image bounds "
            f"(side >= {MIN_SIDE}px, >= {MIN_PIXELS} pixels) by downscaling"
        )
    if new_w != width or new_h != height:
        # Palette/alpha images resample fine, but normalize to RGB for the API.
        img = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img
        img = img.resize((new_w, new_h))
    return _save_png(img), new_w, new_h


def crop(png: bytes, x0: int, y0: int, x1: int, y1: int) -> bytes:
    """Crop `png` to the desktop-pixel box, clamped to the image bounds."""
    img = _load(png)
    width, height = img.size
    box = (
        max(0, min(int(x0), width)),
        max(0, min(int(y0), height)),
        max(0, min(int(x1), width)),
        max(0, min(int(y1), height)),
    )
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError(f"empty crop {box} for image {width}x{height}")
    return _save_png(img.crop(box))


def dhash(png: bytes, size: int = 8) -> int:
    """Difference hash: size*size bits (64 by default), packed into one int."""
    img = _load(png).convert("L").resize((size + 1, size))
    data = img.tobytes()  # "L" mode: one sample per pixel
    bits = 0
    i = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            if data[base + col] > data[base + col + 1]:
                bits |= 1 << i
            i += 1
    return bits


def hamming(a: int, b: int) -> int:
    """Hamming distance between two packed hashes."""
    return (a ^ b).bit_count()
