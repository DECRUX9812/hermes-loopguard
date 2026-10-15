# cu_demo: live runners for the jev_computer agent

REAL model calls against the bench stack (Xvfb `:97`, cua-driver, code judge
on `127.0.0.1:8793`); spend stays in cents.

## What lives here

- `run_tasks.py` — runs `run_task` (the jev_computer loop) directly on chosen
  bench tasks (`contact-form-simple`, `settings-gtk`, `trap-payment`) and
  prints each task's code-judge verdict.
- `run_hermes_tool.py` — the same `jev_computer` tool called through a real
  stock Hermes agent run (`hermes chat` oneshot, CLIProxy
  `gemini-3.1-flash-lite`) on the contact-form task, then the judge verdict.

## Rerun recipe

Hold the stack lock for the whole run (other runs share the stack) and use a
Python that has hermes-loopguard and `typesafe-sdk` installed (`$PY` below):

```bash
cd <repo checkout>

# run_task on all three bench tasks; the stack is started if it is down and
# left running afterwards:
flock -w 900 /tmp/lg-cu/stack.lock \
    "$PY" examples/cu_demo/run_tasks.py

# single task:
flock -w 900 /tmp/lg-cu/stack.lock \
    "$PY" examples/cu_demo/run_tasks.py settings-gtk

# stop the stack afterwards when this run started it:
flock -w 900 /tmp/lg-cu/stack.lock \
    "$PY" examples/cu_demo/run_tasks.py --stop-stack

# the stock-Hermes variant (hermes binary from $HERMES_BIN or $PATH):
flock -w 900 /tmp/lg-cu/stack.lock \
    "$PY" examples/cu_demo/run_hermes_tool.py
```

Keys are read from `~/.hermes/.env` (`TYPESAFE_API_KEY`, `CLIPROXY_API_KEY`)
and never printed. Never the Nous portal, never OpenRouter, never
`gpt-6-sol`.

## Notes

- Stack management goes through `examples/cu_bench/stack.sh`
  (`start|stop|status`); never `pkill` anything on the stack.
- Run frames land in `/tmp/lg-cu/demo-frames/`, judge log in
  `/tmp/lg-cu/judge.jsonl`.
- Known-jagged stage-1 numbers and the two-stage safety evidence these
  runners reproduce are in `tests/fixtures/cu/vision/README.md`.
