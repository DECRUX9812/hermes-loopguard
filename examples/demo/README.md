# LoopGuard demo — real cron, real Hermes, zero mocks

Shows stock Hermes waking its cron agent only when a LoopGuard watcher finds
new or changed work: poll 1 gates and routes 2 issues (bug → agent with a
no-tools triage brief; spam → dropped), polls 2–3 change nothing so the shim
prints `{"wakeAgent": false}` and no model is called at all, poll 4 edits one
issue and only that item is re-gated.

Run it (never against `~/.hermes`; needs a CLIProxy endpoint, :8317 by
default):

    export HERMES_HOME=/path/to/fresh/demo-home
    bash examples/demo/run_demo.sh

Uses the `hermes` binary on PATH, or set `HERMES_BIN`. Endpoint, provider,
model and key env var can be overridden with `LG_DEMO_BASE_URL`,
`LG_DEMO_PROVIDER`, `LG_DEMO_MODEL` and `LG_DEMO_KEY_ENV` (defaults:
http://127.0.0.1:8317/v1, cliproxy-antigravity, gemini-3.1-flash-lite,
CLIPROXY_API_KEY). The semantic-gate backend is pinned with
`LG_DEMO_BACKEND` (default `auto`; also `typesafe`, `openai`, `hermes`,
`local`), written into the watcher's `config.backend`: with `typesafe` the
gate goes through the real typesafe-sdk System One call and falls back to
nothing. The key for `LG_DEMO_KEY_ENV` and `TYPESAFE_API_KEY` are read from
`~/.hermes/.env` only when unset and that file exists; they are never
printed.

State is reset each run, so it is safe to repeat. The semantic gate calls the
configured model through the openai-compatible endpoint, falling back to
local heuristics if that fails.
