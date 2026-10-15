#!/usr/bin/env python3
"""Copy small JPEG thumbnails of every FAILING bench row into results/thumbs/.

    PYTHONPATH=src python3 make_thumbs.py results/jev-x4b-final3.jsonl \
        [results/baseline-x4b-final3.jsonl ...]

Frames dirs are git-ignored (large PNGs); BENCH.md links these thumbnails
instead.  One image per failure: the LAST frame of the run (where it ended).
Jev rows keep cu-frames/<task>-rN/<run>/NNN-after.jpg; baseline rows keep
frames/<task>-rN/turn-NN.png (converted to JPEG).
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
THUMBS = HERE / "results" / "thumbs"
MAX_SIDE = 640


def _jpeg_bytes(png: bytes, quality: int = 70) -> bytes:
    img = Image.open(BytesIO(png)).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def _latest_frame(frame_dir: Path) -> tuple[str, bytes] | None:
    """The chronologically last image in a run's frames dir."""
    if not frame_dir.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for sub in sorted(frame_dir.iterdir()):
        if sub.is_dir():  # jev: cu-frames/<task>-rN/<run_id>/*.jpg
            for f in sorted(sub.glob("*.jpg")) + sorted(sub.glob("*.png")):
                candidates.append((f.name, f))
        elif sub.suffix.lower() in (".png", ".jpg"):  # baseline: turn-NN.png
            candidates.append((sub.name, sub))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    name, path = candidates[-1]
    return name, path.read_bytes()


def main(argv: list[str]) -> int:
    THUMBS.mkdir(parents=True, exist_ok=True)
    made = 0
    for arg in argv:
        results = Path(arg)
        if not results.is_absolute():
            results = HERE / arg if not results.exists() else results
        for line in results.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("success"):
                continue
            label = f"{row['arm']}-{row['task']}-r{row.get('repeat', '?')}"
            frame_dir = row.get("frames") or ""
            got = _latest_frame(Path(frame_dir)) if frame_dir else None
            if got is None:
                print(f"  no frames for {label} (dir={frame_dir!r})")
                continue
            name, png = got
            out = THUMBS / f"{label}-{name.rsplit('.', 1)[0]}.jpg"
            out.write_bytes(_jpeg_bytes(png))
            print(f"  {out.name}")
            made += 1
    print(f"{made} thumbnail(s) in {THUMBS}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
