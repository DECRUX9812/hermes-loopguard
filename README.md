# hermes-loopguard

Stop automations from burning frontier model tokens on unchanged state, routine triage, and retry loops.

Cron and kanban automations pay frontier models to inspect empty queues,
reread unchanged issues, and evaluate whether failed tasks should retry.
LoopGuard puts those decisions on a cheap, typed path before the agent wakes.

## How it works

```
cron tick -> loopguard_<watcher>.py shim
               |
               v
      source (file | command | python) -> items[]
               |
               v
     CHANGE GATE (pure code, 0 tokens)
       Unchanged / terminal / claimed-by-live-run -> skip
       No admitted items -> {"wakeAgent": false} (Hermes never calls a model)
               |
               v
     SEMANTIC GATE (cheap, typed classifier)
       Questions: Choice, Noul, Score (one call per item)
       Threshold check: below threshold -> review queue
               |
               v
     ROUTER (route -> handler)
       agent   : claim item, build bounded brief, wake agent with {"wakeAgent": true}
       kanban  : hermes kanban create (idempotent admission)
       review  : enqueue in SQLite (hermes loopguard review)
       drop    : suppress item, log decision
```

When every item is unchanged or filtered out, the runner emits `{"wakeAgent": false}`.
Hermes terminates the job immediately without building a prompt or dispatching an LLM turn.

## Install

LoopGuard is a local package, not on PyPI. Install it from a checkout into the **same Python
environment the `hermes` command runs from** — plugins load inside Hermes' own PM-managed
environment, so an install into some other venv is invisible to Hermes:

```bash
# Find the interpreter behind your `hermes` command:
head -1 "$(command -v hermes)"    # e.g. #!/path/to/hermes-venv/bin/python

# Install from a local checkout into that environment:
/path/to/hermes-venv/bin/pip install -e /path/to/hermes-loopguard
# or: uv pip install --python /path/to/hermes-venv/bin/python -e /path/to/hermes-loopguard
```

The `-e` (editable) install matters: stock Hermes' PM rewires `sys.path` and drops every
`site-packages` entry that is not its own, so a regular (non-editable) install is invisible to the
in-process plugin loader. An editable install survives because its `.pth` adds the source directory
instead. (The cron-script path works either way: the watcher shim embeds the install's site dirs.)

LoopGuard has no runtime dependencies beyond what stock Hermes ships (pyyaml is optional; a
built-in fallback covers YAML), so it loads cleanly inside the PM environment.

## Quickstart

```bash
# 1. Enable the plugin (opt-in; writes plugins.enabled in $HERMES_HOME/config.yaml)
hermes plugins enable loopguard

# 2. Optional: route gate questions through a cheap model instead of local
#    heuristics — any OpenAI-compatible provider. Add to $HERMES_HOME/config.yaml
#    (the API key goes in $HERMES_HOME/.env, never in config.yaml):
```

```yaml
providers:
  my-provider:
    api: http://127.0.0.1:8317/v1   # any OpenAI-compatible endpoint
    name: OpenAI-compatible
    key_env: MY_PROVIDER_API_KEY
    transport: chat_completions
auxiliary:
  loopguard_gate:
    provider: my-provider
    model: a-cheap-fast-model
```

```bash
# 3. Install a watcher (creates $HERMES_HOME/scripts/loopguard_<name>.py
#    and a cron job named lg-<name>)
hermes loopguard watch install examples/issue_triage.yaml --schedule "every 15m"

# 4. Trigger one run, then check what the gate did
hermes cron run lg-issue-triage
hermes loopguard stats

# 5. Optional: dashboard page in the Hermes web UI
hermes loopguard dashboard install
```

A full scripted walkthrough against a real Hermes cron job lives in
[examples/demo/](examples/demo/) (`run_demo.sh`, with a recorded `sample-output.txt`).

## Backends

LoopGuard evaluates gate questions using a fast, cheap backend. `backend: auto` evaluates
backends in order: TypeSafe, OpenAI-compatible, Hermes auxiliary task, then local heuristics.

| Backend | Requirements | Features |
|---|---|---|
| `typesafe` | `TYPESAFE_API_KEY`, `typesafe-sdk` | Calibrated probabilities, fast Jev model |
| `openai` | `base_url`, `model`, `api_key_env` | JSON or logprobs option probabilities |
| `hermes` | `auxiliary.loopguard_gate` in Hermes config.yaml | Routes through any cheap Hermes provider |
| `local` | None | Pure Python keyword and regex heuristics |

Global configuration in `<HERMES_HOME>/loopguard/config.yaml`:

```yaml
backend: auto
shadow: false
on_error: wake  # watcher error policy: wake (fail open) | skip
typesafe:
  api_key_env: TYPESAFE_API_KEY
  timeout: 30
pricing:
  est_tokens_per_run: 25000
  usd_per_mtok: 3.0
```

`on_error` decides what a watcher run does when it fails (a broken decision
store, a source that crashes, a backend error): `wake` (the default) fails
open and wakes the agent so LoopGuard is never worse than not having it;
`skip` keeps the agent asleep (`{"wakeAgent": false}`). Either way the error
is logged and a `LoopGuard: ...` warning goes to stderr.

## Watcher spec reference

Watchers are defined in YAML under `<HERMES_HOME>/loopguard/watchers/<name>.yaml`:

```yaml
name: issue-triage
source:
  command: ["gh", "issue", "list", "--state", "open", "--json", "number,title,body,labels"]
id_field: number
fingerprint_fields: [title, body, labels]
gate:
  route_question: kind
  questions:
    kind:
      type: choice
      instructions: What does this issue need?
      criteria:
        bug: defect in existing behaviour
        feature: request for new behaviour
        duplicate_or_spam: spam or duplicate
    actionable:
      type: noul
      instructions: Is there enough detail to start work?
  routes:
    bug:
      handler: agent
      min_prob: 0.6
      when: {actionable: ">=0.6"}
      brief: "Reproduce first. Propose a minimal fix."
    feature:
      handler: review
      min_prob: 0.5
    duplicate_or_spam:
      handler: drop
      min_prob: 0.75
  default_route: review
```

Questions support `choice` (categorical probabilities), `noul` (scalar probability of true),
and `score` (ordered numeric levels). Items below probability thresholds route to `default_route`.

## Routing, retries, and terminal states

Items transition across states: `new`, `claimed`, `completed`, `blocked`, `budget_exhausted`, `dropped`, `review`, and `failed`.
Items only retry when evidence changes (new content fingerprint) up to `max_attempts`.

For Kanban boards, LoopGuard performs admission before task creation, mapping items to board tasks
with an idempotency key and `--max-retries`. On stock Hermes, LoopGuard cannot veto dispatch at zero
tokens because Hermes lacks a pre-dispatch hook; worker-side spend caps in `pre_tool_call` halt
runaway workers after one short turn by blocking non-kanban tools.

## Tool gate

An optional policy gate in `config.yaml` (`tool_gate.enabled: true`) inspects tool calls via
Hermes's `pre_tool_call` hook. It classifies calls into read, write, network, shell, or destructive
actions. The tool gate can block destructive calls or request human approval, but never weakens
Hermes's built-in confirmation prompts. Scope is constrained to automations.

## Completion check

LoopGuard provides completion verification for goal-driven workflows:

```bash
hermes loopguard check GOAL.md --cwd /path/to/repo
```

When active, `pre_verify` checks criteria in `GOAL.md` and nudges the model to continue if criteria
remain unmet, capped by `completion.max_continuations` (default 3).

## Shadow mode and eval harness

In shadow mode, LoopGuard logs proposed classifications without suppressing runs:

```bash
hermes loopguard shadow on --watcher issue-triage
```

Run offline evaluations against labeled test cases:

```bash
hermes loopguard eval examples/eval/issue_triage_cases.jsonl --watcher examples/issue_triage.yaml
```

## Savings view

Inspect run savings, avoided tokens, and gate overhead:

```
LoopGuard: 41 runs skipped (87%), ~1.0M tokens / $3.08 avoided, gate cost $0.02, $0.41 per completed task
Watcher         Polls  Skipped  Woken  Avoided   Routes (agent/review/drop)
issue-triage       47       41      6     1.0M   6 / 2 / 33
```

Install the dashboard UI plugin:

```bash
hermes loopguard dashboard install
```

The web dashboard displays live KPI cards, sparklines, stacked skipped vs woken bar charts,
per-watcher route distributions, a 25-item decision log, and an interactive review queue.

## When to use Hermes cron monitor mode

Use Hermes's built-in cron monitor mode (`monitor_script` or `monitor_url`) if a whole-output hash
of the entire command output is sufficient to detect change. Use LoopGuard when jobs process
collections of items requiring per-item tracking, semantic triage, claim leases, and review queues.

## Computer-use bench (Phase 2b)

`examples/cu_bench/` holds the Xvfb benchmark that drives the computer with the cheap typed path
(Jev + AT-SPI) and compares it against a big-model computer-use loop. Results and the verdict:
[BENCH.md](BENCH.md).

Run it with the **lg-venv interpreter** (it has `typesafe-sdk`, PyYAML and Pillow — the system
python3 does not, and every Jev call would silently fall back to the CLIProxy model) and
`PYTHONPATH=src`:

```bash
# stack + runs both hold the shared locks; never skip flock
~/Code/ship-test/lg-venv/bin/python -c "import typesafe_sdk"   # interpreter check
flock /tmp/lg-cu/stack.lock examples/cu_bench/stack.sh start    # Xvfb :97 stack (if down)

cd examples/cu_bench
flock /tmp/lg-cu/stack.lock env PYTHONPATH=src \
  ~/Code/ship-test/lg-venv/bin/python run_bench.py --arm jev      --tasks all --repeat 1
flock /tmp/lg-cu/stack.lock env PYTHONPATH=src \
  ~/Code/ship-test/lg-venv/bin/python run_bench.py --arm baseline --tasks all --repeat 1
env PYTHONPATH=src ~/Code/ship-test/lg-venv/bin/python run_bench.py --summary
```

`--arm jev` preflights the interpreter, the `TYPESAFE_API_KEY` and one tiny real `jev-1.14.0`
call, and exits non-zero before any spend when they are missing; `--summary` flags any result
row whose vision calls were served by a non-Jev model.

## Development

Run unit tests:

```bash
PYTHONPATH=src pytest -q
```

End-to-end tests (`tests/test_e2e_hermes.py`) run the real stock `hermes`
binary in subprocesses against a throwaway `HERMES_HOME`. The hermes path is
machine-specific, so it is required via `LOOPGUARD_E2E_HERMES` (the module
skips cleanly when unset). `LOOPGUARD_E2E_INSTALLS` optionally points at an
already-bootstrapped PM `installs/` directory so the first hermes command in
the test home skips the slow (~240 MB) bootstrap:

```bash
LOOPGUARD_E2E_HERMES=/path/to/hermes-venv/bin/hermes \
LOOPGUARD_E2E_INSTALLS=/path/to/prepared/installs \
PYTHONPATH=src pytest -q tests/test_e2e_hermes.py
```

License: MIT.
