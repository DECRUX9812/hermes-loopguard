"""Computer-use vision primitives (Phase 2).

Everything here imports lazily: the package itself pulls in no heavy
dependencies, and Pillow is only imported inside `cu.frames` functions.
Import a submodule to use it, e.g. `from hermes_loopguard.cu import vision`.
"""
from __future__ import annotations

__all__ = ["frames", "pricing", "vision"]
