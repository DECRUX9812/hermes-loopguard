#!/bin/bash
# Isolated computer-use bench stack on Xvfb :97, in the user systemd unit
# `lg-cu-stack`.  Stop ONLY via `systemctl --user stop lg-cu-stack` (never pkill).
#
#   stack.sh start|stop|status|launch '<json>'
#
# `launch` runs {"argv": [...], "env": {...}} (JSON as the argument) inside the
# session, so apps share the DBus/AT-SPI bus and DISPLAY=:97.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN=/tmp/lg-cu
UNIT=lg-cu-stack
SOCKET=$RUN/cua.sock
LAUNCHER=$RUN/launcher.sock
JUDGE=http://127.0.0.1:8793

unit_active() {
  systemctl --user is-active --quiet "$UNIT" 2>/dev/null
}

write_inner() {
  mkdir -p "$RUN"
  cat > "$RUN/stack-inner.sh" <<INNER
#!/bin/bash
set -x
export DISPLAY=:97
export GNOME_ACCESSIBILITY=1
export NO_AT_BRIDGE=0
export PATH="\$HOME/.local/bin:\$PATH"
# Private runtime dir: at-spi-bus-launcher puts the a11y bus socket at
# \$XDG_RUNTIME_DIR/at-spi/bus, which it otherwise shares with the real
# desktop and other jobs' stacks (a stopped session deletes that socket out
# from under running apps).  Keep the whole stack's runtime state private.
export XDG_RUNTIME_DIR="$RUN/runtime"
mkdir -p "$XDG_RUNTIME_DIR"
exec >> "$RUN/stack.log" 2>&1

echo "=== lg-cu-stack inner start \$(date -Is) ==="

# 1. Headless display
Xvfb :97 -screen 0 1280x800x24 -nolisten tcp &
for i in \$(seq 150); do
  DISPLAY=:97 xdotool getdisplaygeometry >/dev/null 2>&1 && break
  sleep 0.2
done
echo "Xvfb up: \$(DISPLAY=:97 xdotool getdisplaygeometry 2>&1)"

# 1b. Window manager: cua-driver keyboard delivery (XTEST) needs a focused
# window; bare Xvfb has none, so run xfwm4 and give it a moment.
# (Started AFTER a11y_bus.py — xfwm4 is a GTK app and would otherwise
# dbus-activate the shared bus-launcher and steal org.a11y.Bus first.)

# 1c. AT-SPI: deterministic private a11y bus + registry + org.a11y.Bus owner
# (see a11y_bus.py).  Relying on bus-launcher activation is racy here: apps
# and cua-driver can resolve different buses after a relaunch, and walks
# degrade to x11_property_fallback_partial.
python3 "$ROOT/a11y_bus.py" &
for i in \$(seq 50); do
  [ -S "$RUN/a11y/bus" ] && break
  sleep 0.2
done
sleep 1
echo "AT-SPI ready (bus: $RUN/a11y/bus)"

xfwm4 --daemon 2>/dev/null || xfwm4 &
sleep 2

# 3. Judge server (serves pages, records outcomes)
python3 "$ROOT/judge.py" --port 8793 --log "$RUN/judge.jsonl" --root "$ROOT/pages" &

# 4. App launcher inside the session
python3 "$ROOT/launcher.py" --socket "$RUN/launcher.sock" &

# 5. cua-driver daemon on our display + socket
cua-driver serve --socket "$SOCKET" &

echo "=== all services started, waiting ==="
wait
INNER
  chmod +x "$RUN/stack-inner.sh"
}

wait_ready() {
  for i in $(seq 120); do
    if [ -S "$SOCKET" ] && curl -fsS "$JUDGE/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

cmd_start() {
  if unit_active; then
    echo "stack already running (unit $UNIT)"
    exit 0
  fi
  systemctl --user stop "$UNIT" 2>/dev/null || true
  # A socket left by a previous session makes the inner wait loop pass at
  # once, before a11y_bus.py is up, and the run gets a dead AT-SPI bus.
  rm -f "$SOCKET" "$RUN/judge.jsonl" "$RUN/a11y/bus" "$RUN/a11y/addr"
  mkdir -p "$RUN" "$RUN/state" "$RUN/files"
  write_inner
  systemd-run --user --unit="$UNIT" \
    -p MemoryMax=6G -p MemoryHigh=5G \
    -p Description="LoopGuard computer-use bench stack (Xvfb :97)" \
    dbus-run-session -- /bin/bash "$RUN/stack-inner.sh"
  if wait_ready; then
    echo "stack ready: DISPLAY=:97  cua.sock=$SOCKET  judge=$JUDGE"
  else
    echo "ERROR: stack did not become ready; see $RUN/stack.log" >&2
    exit 1
  fi
}

cmd_stop() {
  systemctl --user stop "$UNIT"
  for i in $(seq 40); do
    unit_active || break
    sleep 0.25
  done
  if unit_active; then
    echo "ERROR: unit still active" >&2
    exit 1
  fi
  echo "stack stopped"
}

cmd_status() {
  if unit_active; then
    echo "unit $UNIT: active"
  else
    echo "unit $UNIT: inactive"
  fi
  [ -S "$SOCKET" ] && echo "cua socket: $SOCKET present" || echo "cua socket: absent"
  curl -fsS "$JUDGE/healthz" >/dev/null 2>&1 && echo "judge: up" || echo "judge: down"
}

cmd_launch() {
  # Run the JSON {"argv": [...], "env": {...}} (arg $1) inside the session.
  python3 - "$LAUNCHER" "${1:-}" <<'PY'
import json, socket, sys
sock, req = sys.argv[1], sys.argv[2].strip() or "{}"
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock)
s.sendall((req + "\n").encode())
s.shutdown(socket.SHUT_WR)
out = b""
while True:
    chunk = s.recv(65536)
    if not chunk:
        break
    out += chunk
print(out.decode().strip())
PY
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  launch) shift; cmd_launch "$@" ;;

  *) echo "usage: $0 start|stop|status|launch" >&2; exit 2 ;;
esac
