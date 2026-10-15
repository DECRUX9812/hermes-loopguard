# Hook examples on stock Hermes

`kanban_watcher.yaml` — a file-source watcher whose clear items route to
`handler: kanban`: LoopGuard claims the item, runs `hermes kanban create`
(idempotency key + per-item claim, so one item never makes two tasks) and
does NOT wake the cron agent. Rerun against an unchanged file and the change
gate answers `{"wakeAgent": false}` at zero tokens. Honest limit: stock
Hermes has no dispatch veto, so once a task is on the board the dispatcher
owns it; LoopGuard's token control is admission + the worker-side
attempt/spend caps, not a zero-token veto of a dispatch.

`tool-gate-output.txt` — three `hermes chat -Q --oneshot` transcripts with
`tool_gate: {enabled: true, scope: all}`: `rm -rf` a canary dir is blocked
(canary intact, decision row `watcher=tool_gate`, outcome `block`); an `ls`
runs normally with no LoopGuard row; `curl` is escalated with `approve`, and
in a non-interactive one-shot Hermes' approval gate fail-closes. The gate
can only block or escalate; it never returns an allow directive.

Rerun: set `HERMES_HOME` to a fresh home (never `~/.hermes`), run
`hermes plugins enable loopguard`, copy the `model`/`providers`/`auxiliary`
sections into its `config.yaml`, substitute `@ITEMS_JSON@` in
`kanban_watcher.yaml`, `hermes loopguard watch install <spec> --schedule
"every 15m"`, then run `$HERMES_HOME/scripts/loopguard_<name>.py` with the
`hermes` install's own Python (what cron uses). For the gate, add the
`tool_gate` block to `<home>/loopguard/config.yaml` and run the prompts in
`tool-gate-output.txt`.
