#!/usr/bin/env python3
"""Deterministic AT-SPI wiring for the bench stack.

1. runs a private a11y bus (plain dbus-daemon with at-spi2's config)
2. owns org.a11y.Bus on the STACK session bus and answers GetAddress with
   that private bus, so every AT-SPI client AND app (cua-driver, chrome,
   GTK apps) resolves the SAME bus — no bus-launcher activation races
3. starts at-spi2-registryd on the private bus

Everything is inside the lg-cu-stack unit and dies with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib  # requires python3-gi (present)

RUN = Path(os.environ.get("LG_CU_RUN", "/tmp/lg-cu"))
A11Y_SOCK = f"unix:path={RUN / 'a11y' / 'bus'}"

XML = """
<node>
  <interface name="org.a11y.Bus">
    <method name="GetAddress">
      <arg direction="out" type="s"/>
    </method>
  </interface>
</node>
"""


def handle_call(conn, sender, path, iface, method, params, invocation):
    if method == "GetAddress":
        invocation.return_value(GLib.Variant("(s)", (A11Y_SOCK,)))
        return True
    invocation.return_error(
        Gio.DBusError.quark_for_domain_errors(),
        Gio.DBusError.UNKNOWN_METHOD,
        f"no such method {method}",
    )
    return True


def on_bus_acquired(conn, name):
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    conn.register_object("/org/a11y/bus", node.interfaces[0], handle_call, None, None)
    print(f"org.a11y.Bus owned on session bus -> {A11Y_SOCK}", flush=True)


def on_name_lost(conn, name):
    print(f"FAILED to own {name} on the session bus", flush=True)
    loop.quit()


def main() -> int:
    (RUN / "a11y").mkdir(parents=True, exist_ok=True)
    sock_path = str(RUN / "a11y" / "bus")
    # 1. private a11y bus
    if not Path(sock_path).exists():
        subprocess.Popen(
            [
                "dbus-daemon",
                "--config-file=/usr/share/defaults/at-spi2/accessibility.conf",
                f"--address=unix:path={sock_path}",
                "--print-address",
            ],
            stdout=open(RUN / "a11y" / "addr", "w"),
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if Path(sock_path).exists():
                break
            time.sleep(0.2)
    if not Path(sock_path).exists():
        print("a11y bus socket did not appear", file=sys.stderr, flush=True)
        return 1
    print(f"a11y bus up at {sock_path}", flush=True)

    # 2. registryd on that bus
    env = dict(os.environ, AT_SPI_BUS_ADDRESS=A11Y_SOCK)
    subprocess.Popen(
        ["/usr/libexec/at-spi2-registryd", "--use-gnome-session"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("registryd started on the private a11y bus", flush=True)

    # 3. own org.a11y.Bus so ALL clients resolve the same address
    global loop
    loop = GLib.MainLoop()
    Gio.bus_own_name(
        Gio.BusType.SESSION,
        "org.a11y.Bus",
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        None,
        on_name_lost,
    )
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
