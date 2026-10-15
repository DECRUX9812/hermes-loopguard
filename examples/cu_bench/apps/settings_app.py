#!/usr/bin/env python3
"""LoopGuard settings dialog (GTK3) for the bench.

A window with two toggles, a language dropdown and a Save button.  Save writes
a JSON state file (default /tmp/lg-cu/state/settings.json, override with
LG_CU_STATE) — the task judge reads THAT file, never a model's claim.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

STATE_PATH = Path(
    os.environ.get("LG_CU_STATE", "/tmp/lg-cu/state/settings.json")
)


class SettingsApp(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="LoopGuard Settings")
        self.set_default_size(420, 240)
        self.connect("destroy", Gtk.main_quit)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        self.add(box)

        self.dark = Gtk.CheckButton(label="Dark mode")
        box.add(self.dark)

        self.notify = Gtk.CheckButton(label="Notifications")
        self.notify.set_active(True)
        box.add(self.notify)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add(Gtk.Label(label="Language", xalign=0))
        self.language = Gtk.ComboBoxText()
        for lang in ("English", "German", "French"):
            self.language.append_text(lang)
        self.language.set_active(0)
        row.add(self.language)
        box.add(row)

        save = Gtk.Button(label="Save")
        save.connect("clicked", self.on_save)
        box.add(save)

        self.show_all()

    def on_save(self, _button) -> None:
        state = {
            "dark_mode": bool(self.dark.get_active()),
            "notifications": bool(self.notify.get_active()),
            "language": self.language.get_active_text() or "",
        }
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
        print(f"settings saved -> {STATE_PATH}", flush=True)


def main() -> int:
    SettingsApp()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
