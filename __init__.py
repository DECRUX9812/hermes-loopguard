"""Directory-plugin wrapper: Hermes loads this directory as a plugin package.

`register()` lives in the importable core package (`hermes_loopguard`, under
src/). When this checkout is used as a directory plugin (cloned into
~/.hermes/plugins/loopguard/), its src/ tree is appended to sys.path so no pip
install is needed; with a pip (entry-point) install, the regular import wins.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"

try:
    from hermes_loopguard.plugin import register
except ImportError:  # directory-plugin shape: not pip-installed
    if _SRC.is_dir() and str(_SRC) not in sys.path:
        sys.path.append(str(_SRC))  # appended: Hermes's own packages keep priority
    from hermes_loopguard.plugin import register  # type: ignore[no-redef]

__all__ = ["register"]
