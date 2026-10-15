#!/usr/bin/env bash
# LoopGuard real-cron demo: stock Hermes + hermes-loopguard plugin.
#
# Shows that LoopGuard gates a cron job on a file-backed issue queue with a
# cheap semantic gate: wakes the agent only for new/changed items, routes
# each item (agent / drop / review), and costs zero model calls on
# unchanged polls.
#
# Idempotent: safe to re-run; it resets LoopGuard's state in HERMES_HOME and
# recreates the cron job each time.
#
# Usage:
#   export HERMES_HOME=/path/to/fresh/demo-home   # NEVER ~/.hermes
#   bash examples/demo/run_demo.sh
#
# Requires the `hermes` binary (default: ~/Code/ship-test/lg-venv/bin/hermes)
# and a CLIProxy endpoint at http://127.0.0.1:8317/v1. The CLIPROXY_API_KEY
# is read from ~/.hermes/.env and never printed.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:?Set HERMES_HOME to a fresh demo directory (never ~/.hermes)}"
case "$HERMES_HOME" in
  "$HOME/.hermes"|/path/to/home)
    echo "Refusing to run against $HERMES_HOME" >&2; exit 1;;
esac
export HERMES_HOME
mkdir -p $HERMES_HOME

# Pin the hermes that has the loopguard plugin installed (lg-venv). The user's
# ~/.local/bin/hermes is a different install without the plugin on its path.
HERMES_BIN="${HERMES_BIN:-$HOME/Code/ship-test/lg-venv/bin/hermes}"
[ -x "$HERMES_BIN" ] || HERMES_BIN="$(command -v hermes)"
PYBIN="$(dirname "$HERMES_BIN")/python"
JOB_NAME="lg-demo-issues"
WATCHER="demo-issues"

if [ -z "${CLIPROXY_API_KEY:-}" ]; then
  CLIPROXY_API_KEY="$(grep -E '^CLIPROXY_API_KEY=' "$HOME/.hermes/.env" | head -1 | cut -d= -f2- | tr -d '"')"
  export CLIPROXY_API_KEY
fi

section() { echo; echo "==== $* ===="; }

# Model usage / session counters from Hermes' state.db and LoopGuard's ledger.
metrics() {
  "$PYBIN" - "$HERMES_HOME" <<'PYEOF'
import sqlite3, sys
h = sys.argv[1]
def q(path, sql):
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute(sql).fetchone()[0]
    except Exception as e:
        return f"err: {e}"
state = h + "/state.db"
lg = h + "/plugin-data/loopguard/loopguard.db"
print("  state.db:            sessions=%s model_usage_rows=%s model_tokens=%s" % (
    q(state, "select count(*) from sessions"),
    q(state, "select count(*) from session_model_usage"),
    q(state, "select coalesce(sum(input_tokens+output_tokens),0) from session_model_usage")))
print("  loopguard decisions: rows_with_tokens=%s" % (
    q(lg, "select count(*) from decisions where coalesce(tokens_in,0)+coalesce(tokens_out,0) > 0"),))
PYEOF
}

cd /tmp

section "0. Reset demo state (idempotent)"
rm -rf "$HERMES_HOME/plugin-data/loopguard" "$HERMES_HOME/demo"
"$HERMES_BIN" cron remove "$JOB_NAME" >/dev/null 2>&1 || true
mkdir -p "$HERMES_HOME/demo"
cp "$DEMO_DIR/issues.json" "$HERMES_HOME/demo/issues.json"
echo "demo home: $HERMES_HOME"

# Merge the demo model config (direct file write on purpose: `hermes config
# set` ignores HERMES_HOME and would write the default profile's home).
"$PYBIN" - <<'PYEOF'
import os, yaml
p = os.path.join(os.environ["HERMES_HOME"], "config.yaml")
cfg = (yaml.safe_load(open(p)) or {}) if os.path.exists(p) else {}
cfg.setdefault("model", {})
cfg["model"]["provider"] = "cliproxy-antigravity"
cfg["model"]["default"] = "gemini-3.1-flash-lite"
cfg.setdefault("providers", {})["cliproxy-antigravity"] = {
    "api": "http://127.0.0.1:8317/v1",
    "name": "CLIProxyAPI",
    "key_env": "CLIPROXY_API_KEY",
    "transport": "chat_completions",
}
cfg.setdefault("auxiliary", {})["loopguard_gate"] = {
    "provider": "cliproxy-antigravity",
    "model": "gemini-3.1-flash-lite",
}
yaml.safe_dump(cfg, open(p, "w"), sort_keys=False)
print("config.yaml merged (model/provider/auxiliary.loopguard_gate)")
PYEOF

section "1. Plugin enable + CLI (proves the plugin loads inside stock Hermes)"
"$HERMES_BIN" plugins enable loopguard
"$HERMES_BIN" loopguard --help

section "2. Install the watcher and its cron job"
RESOLVED="$(mktemp /tmp/lg-demo-watcher.XXXX.yaml)"
sed "s|@ISSUES_JSON@|$HERMES_HOME/demo/issues.json|" "$DEMO_DIR/issue_watcher.yaml" > "$RESOLVED"
"$HERMES_BIN" loopguard watch install "$RESOLVED" --schedule "every 15m"
rm -f "$RESOLVED"
"$HERMES_BIN" cron list
JOB_ID="$("$HERMES_BIN" cron list 2>/dev/null | awk -v name="$JOB_NAME" '/^[[:space:]]+[0-9a-f]+ \[/{id=$1} $0 ~ "Name: +" name "$" {print id}')"
echo "job id: $JOB_ID"

section "3. Poll 1: two new issues -> gate + routes + agent wake"
"$HERMES_BIN" cron run "$JOB_ID"
echo "--- routes from the decision log:"
"$HERMES_BIN" loopguard decisions --watcher "$WATCHER" --limit 8

section "4. Polls 2 and 3: nothing changed -> no wake, zero model calls"
metrics > /tmp/lg-demo-metrics-before.txt
cat /tmp/lg-demo-metrics-before.txt
"$HERMES_BIN" cron run "$JOB_ID"
"$HERMES_BIN" cron run "$JOB_ID"
echo "--- metrics after polls 2 and 3 (model_usage_rows and model_tokens must be unchanged):"
metrics
echo "--- LoopGuard poll ledger (woke column: 1=woken, 0=skipped):"
"$PYBIN" - "$HERMES_HOME" <<'PYEOF'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}/plugin-data/loopguard/loopguard.db?mode=ro", uri=True)
for r in db.execute("select id, n_changed, n_admitted, woke, gate_tokens from polls order by id"):
    print(f"  poll {r[0]}: changed={r[1]} admitted={r[2]} woke={r[3]} gate_tokens={r[4]}")
PYEOF

section "5. Poll 4: append evidence to ISS-101 -> only that item re-gated"
"$PYBIN" - <<'PYEOF'
import json, os
p = os.path.join(os.environ["HERMES_HOME"], "demo", "issues.json")
items = json.load(open(p))
for it in items:
    if it["id"] == "ISS-101":
        it["body"] += ("\n\nUPDATE: root cause narrowed - settings/views.py:214 calls"
                       " validate(None) when email is blank; the validator does `if '@' in email`.")
json.dump(items, open(p, "w"), indent=2)
print("ISS-101 body updated")
PYEOF
"$HERMES_BIN" cron run "$JOB_ID"
echo "--- routes from the decision log:"
"$HERMES_BIN" loopguard decisions --watcher "$WATCHER" --limit 6

section "6. Savings stats"
"$HERMES_BIN" loopguard stats
echo "--- standalone CLI (lg-venv):"
"$(dirname "$PYBIN")/hermes-loopguard" stats

section "Demo complete"
