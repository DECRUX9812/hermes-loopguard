# LoopGuard demo — real cron, real Hermes, zero mocks

Shows stock Hermes waking its cron agent only when a LoopGuard watcher finds
new or changed work: poll 1 gates and routes 2 issues (bug → agent with a
bounded brief; spam → dropped), polls 2–3 change nothing so the shim prints
`{"wakeAgent": false}` and no model is called at all, poll 4 edits one issue
and only that item is re-gated.

Run it (never against `~/.hermes`; needs a CLIProxy endpoint on :8317):

    export HERMES_HOME=/path/to/fresh/demo-home
    bash examples/demo/run_demo.sh

State is reset each run, so it is safe to repeat. The semantic gate calls
gemini-3.1-flash-lite through the openai-compatible endpoint, falling back
to local heuristics if that fails.
